"""
houdini_live_mcp.py
===================
MCP server that bridges Claude Desktop to a LIVE Houdini session through the
embedded hwebserver bridge (houdini_mcp_bridge.py running inside Houdini).

Runs as a stdio MCP server. It must NEVER write to stdout -- that corrupts the
JSON-RPC stream. All diagnostics are returned as tool text instead.

Pairs with houdini_docs_mcp.py:
  * houdini-docs  -> "how does X work / what is the signature of Y"
  * houdini-live  -> "what is my actual scene doing right now"

Architecture: every curated tool builds a tiny Python snippet that assigns a
variable `_result`, and POSTs it (base64) to the bridge's single /mcp/exec
endpoint. New capabilities = new snippets here, no Houdini restart required.
run_python is the general escape hatch.

REQUIRES the main-thread bridge (houdini_mcp_bridge.py that marshals _exec to
Houdini's main thread). The DOP tools and capture_viewport assume they already
run on the main thread and DO NOT self-wrap in hdefereval. Ship both files
together.

Flipbook tools (range playblasts) come in two flavours because the bridge lives
on Houdini's main thread and a native flipbook is a blocking main-thread loop:

  * flipbook_start  -- fire-and-forget. Resolves the requested settings against
    the LIVE FlipbookSettings API (unknown knobs are skipped, enum strings degrade
    gracefully), then posts sv.flipbook() as a deferred event callback so the tool
    returns "starting..." immediately. The bridge goes DARK for the render's
    duration (no ping/set_frame until it finishes) -- fine for short look-checks.
  * flipbook_frames -- pollable per-frame driver. Renders one frame (or a small
    chunk) per call via set_frame -> cook -> single-frame flipbook, returning
    progress each call. Keeps the bridge responsive; the client loops. Use this
    for anything long enough that a dark bridge would hurt.

Both share a cache guard (flipbook_status / _CACHE_GUARD) that refuses a heavy
uncached solve unless force=True, so you don't re-drive a sim every frame.
Both also, by default (encode_mp4=True), write an H.264 mp4 beside the PNG
sequence via Houdini's bundled hffmpeg ($HFS/bin) -- the sequence stays the
source of truth, the mp4 is the deliverable. The flipbook's background is
transparent, so the encode composites over the current viewport colour-scheme
background (background="viewport") to match what you see, rather than going
black. The bundled hffmpeg has no libx264 (use libopenh264 / h264_nvenc /
h264_amf), no gif encoder, and no lavfi input device (the composite colour is a
filtergraph source). encode_sequence does the same encode on an existing on-disk
sequence with no re-render.
For genuinely long renders (day-long water sims) neither belongs here -- cache the
solver and dispatch a headless hython ROP out-of-process instead.
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP, Image

# ---------------------------------------------------------------------------
# Config -- must match houdini_mcp_bridge.py inside Houdini.
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8008
TIMEOUT = 30.0
MAX_RESULT_CHARS = 20000        # guard the model's context from huge dumps

BASE_URL = "http://%s:%d" % (HOST, PORT)

# Shared secret with the bridge. Auto-generated on first use and stored in the
# user's home dir; both sides read the same file, so no manual setup. Delete
# the file to rotate (then restart Houdini AND Claude Desktop).
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".houdini_mcp_token")


def _token():
    """Read the shared token, creating it with a random value if absent.
    Read fresh on every use (no caching) so bridge and server stay consistent
    regardless of which side started first."""
    try:
        with open(TOKEN_FILE) as fp:
            tok = fp.read().strip()
    except OSError:
        tok = ""
    if not tok:
        import secrets
        tok = secrets.token_hex(16)
        with open(TOKEN_FILE, "w") as fp:
            fp.write(tok)
    return tok


mcp = FastMCP("houdini-live")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _conn_hint(exc):
    return ("Could not reach the Houdini bridge at %s. Is Houdini open with "
            "houdini_mcp_bridge started? (Check the Houdini Python Shell for a "
            "'[mcp-bridge] listening' line, or run its start() manually.) "
            "Underlying error: %s" % (BASE_URL, getattr(exc, "reason", exc)))


def _http_get(path):
    try:
        with urllib.request.urlopen(BASE_URL + path, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return {"ok": False, "error": _conn_hint(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _exec(code):
    """POST a Python snippet to the live session. The snippet must assign
    `_result`. Returns the decoded bridge response dict."""
    payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
    data = urllib.parse.urlencode(
        {"token": _token(), "payload": payload}).encode("ascii")
    req = urllib.request.Request(
        BASE_URL + "/mcp/exec", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "HTTP %s: %s"
                % (exc.code, exc.read().decode("utf-8", "replace"))}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": _conn_hint(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run(code):
    """Exec a snippet and format the outcome for the model."""
    res = _exec(code)
    if res.get("ok"):
        text = json.dumps(res.get("result"), indent=2, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = (text[:MAX_RESULT_CHARS]
                    + "\n... [truncated -- sample fewer elements or narrow the query]")
        return text
    out = "ERROR: " + str(res.get("error", "unknown"))
    if res.get("traceback"):
        out += "\n\n" + res["traceback"]
    return out


def _targeted(path, body):
    """Prefix a snippet body with TARGET = <path> to avoid brace-escaping."""
    return "TARGET = %r\n" % path + body


# ===========================================================================
# Tools
# ===========================================================================
@mcp.tool()
def ping() -> str:
    """Confirm the live Houdini bridge is reachable and report the session
    (Houdini version, current .hip file, frame, fps). Call this first whenever
    another live tool errors."""
    return json.dumps(_http_get("/mcp/ping"), indent=2, ensure_ascii=False)


@mcp.tool()
def run_python(code: str) -> str:
    """Execute arbitrary Python in the live Houdini session, with `hou` already
    imported, and return the value assigned to `_result`.

    The snippet MUST assign its output to `_result`, e.g.:
        n = hou.node("/obj/geo1")
        _result = [c.name() for c in n.children()]

    This is the escape hatch for anything the specialized tools don't cover:
    setting parameters, creating/wiring nodes, custom geometry queries, reading
    solver state, etc. hou.* return values are JSON-encoded automatically;
    unknown types fall back to str(). Keep `_result` reasonably small -- large
    payloads are truncated.
    """
    return _run(code)


@mcp.tool()
def get_node(path: str) -> str:
    """Summarize a node: type, category, flags (bypass/display/render/template),
    inputs, outputs, child count, and current cook errors/warnings. `path` is an
    absolute op path, e.g. /obj/geo1/attribwrangle1. Accessing errors may force
    a cook."""
    body = '''
n = hou.node(TARGET)
if n is None:
    _result = {"error": "node not found: " + TARGET}
else:
    info = {
        "path": n.path(),
        "name": n.name(),
        "type": n.type().name(),
        "category": n.type().category().name(),
        "inputs": [i.path() if i else None for i in n.inputs()],
        "outputs": [o.path() for o in n.outputs()],
        "num_children": len(n.children()),
        "errors": list(n.errors()),
        "warnings": list(n.warnings()),
    }
    for flag in ("isBypassed", "isDisplayFlagSet", "isRenderFlagSet",
                 "isTemplateFlagSet"):
        f = getattr(n, flag, None)
        if callable(f):
            try:
                info[flag] = f()
            except Exception:
                pass
    _result = info
'''
    return _run(_targeted(path, body))


@mcp.tool()
def get_wrangle_code(path: str) -> str:
    """Read a wrangle's VEX plus its execution context: the `snippet` code, the
    Run Over class (point/prim/vertex/detail/numbers), group/group-type, and any
    cook errors/warnings. Falls back to scanning string parms if the code parm
    is named unusually. This is the go-to for debugging a specific wrangle."""
    body = '''
n = hou.node(TARGET)
if n is None:
    _result = {"error": "node not found: " + TARGET}
else:
    info = {"path": n.path(), "type": n.type().name()}
    sp = n.parm("snippet")
    if sp is None:
        for p in n.parms():
            nm = p.name().lower()
            t = p.parmTemplate()
            if t.type() == hou.parmTemplateType.String and (
                    "snippet" in nm or "vexpression" in nm or nm == "code"):
                sp = p
                break
    info["vex_parm"] = sp.name() if sp is not None else None
    info["vex"] = sp.eval() if sp is not None else None
    cls = n.parm("class")
    if cls is not None:
        try:
            info["run_over"] = cls.evalAsString()
        except Exception:
            info["run_over"] = cls.eval()
    for pname in ("group", "grouptype", "vex_precision", "autobind"):
        p = n.parm(pname)
        if p is not None:
            try:
                info[pname] = p.evalAsString()
            except Exception:
                info[pname] = p.eval()
    info["errors"] = list(n.errors())
    info["warnings"] = list(n.warnings())
    _result = info
'''
    return _run(_targeted(path, body))


@mcp.tool()
def get_parms(path: str, names: str = "") -> str:
    """Read parameter values on a node. `names` is an optional comma-separated
    list to filter (e.g. "tx,ty,tz"); empty returns all parms. Returns
    name -> evaluated value."""
    filt = [x.strip() for x in names.split(",") if x.strip()]
    body = "NAMES = %r\n" % filt + '''
n = hou.node(TARGET)
if n is None:
    _result = {"error": "node not found: " + TARGET}
else:
    parms = [n.parm(x) for x in NAMES] if NAMES else list(n.parms())
    out = {}
    for p in parms:
        if p is None:
            continue
        try:
            out[p.name()] = p.eval()
        except Exception as e:
            out[p.name()] = "<eval error: %s>" % e
    _result = out
'''
    return _run(_targeted(path, body))


@mcp.tool()
def get_geo_stats(path: str) -> str:
    """Report geometry stats for a SOP: point/prim/vertex counts, bounding box,
    the full attribute schema per class (name/type/size), and group names. Cheap
    -- no per-element values. Cooks the node if needed."""
    body = '''
n = hou.node(TARGET)
if n is None:
    _result = {"error": "node not found: " + TARGET}
elif not isinstance(n, hou.SopNode):
    _result = {"error": TARGET + " is not a SOP; geometry stats need a SOP"}
else:
    g = n.geometry()
    if g is None:
        _result = {"error": "no geometry (cook the node / check the display flag)"}
    else:
        def sch(attribs):
            return [{"name": a.name(), "type": str(a.dataType()),
                     "size": a.size()} for a in attribs]
        bb = g.boundingBox()
        _result = {
            "point_count": g.intrinsicValue("pointcount"),
            "prim_count": g.intrinsicValue("primitivecount"),
            "vertex_count": g.intrinsicValue("vertexcount"),
            "bbox_min": bb.minvec(),
            "bbox_max": bb.maxvec(),
            "point_attribs": sch(g.pointAttribs()),
            "prim_attribs": sch(g.primAttribs()),
            "vertex_attribs": sch(g.vertexAttribs()),
            "detail_attribs": sch(g.globalAttribs()),
            "point_groups": [x.name() for x in g.pointGroups()],
            "prim_groups": [x.name() for x in g.primGroups()],
            "vertex_groups": [x.name() for x in g.vertexGroups()],
        }
'''
    return _run(_targeted(path, body))


@mcp.tool()
def get_attributes(path: str, sample: int = 8) -> str:
    """Inspect actual attribute VALUES on a SOP's geometry: schema plus up to
    `sample` sampled point and primitive values per attribute, plus all detail
    attributes. `sample` is clamped to 0..64. Sampling is bounded (islice), so
    it is safe on heavy geometry."""
    sample = max(0, min(int(sample), 64))
    body = "SAMPLE = %d\n" % sample + '''
import itertools
n = hou.node(TARGET)
if n is None:
    _result = {"error": "node not found: " + TARGET}
elif not isinstance(n, hou.SopNode):
    _result = {"error": TARGET + " is not a SOP"}
else:
    g = n.geometry()
    if g is None:
        _result = {"error": "no geometry on node"}
    else:
        def sch(attribs):
            return [{"name": a.name(), "type": str(a.dataType()),
                     "size": a.size()} for a in attribs]
        def samp(iterator, attribs):
            cols = {a.name(): [] for a in attribs}
            for el in itertools.islice(iterator, SAMPLE):
                for a in attribs:
                    cols[a.name()].append(el.attribValue(a))
            return cols
        pa, ra = g.pointAttribs(), g.primAttribs()
        detail = {}
        for a in g.globalAttribs():
            detail[a.name()] = g.attribValue(a)
        _result = {
            "sampled": SAMPLE,
            "point": {"schema": sch(pa), "values": samp(g.iterPoints(), pa)},
            "prim":  {"schema": sch(ra), "values": samp(g.iterPrims(), ra)},
            "detail": detail,
        }
'''
    return _run(_targeted(path, body))


@mcp.tool()
def get_cook_errors(path: str) -> str:
    """Return the current cook errors and warnings for a node (plus cook count).
    The fastest way to see why a node is red/yellow."""
    body = '''
n = hou.node(TARGET)
if n is None:
    _result = {"error": "node not found: " + TARGET}
else:
    info = {
        "path": n.path(),
        "errors": list(n.errors()),
        "warnings": list(n.warnings()),
    }
    cc = getattr(n, "cookCount", None)
    if callable(cc):
        try:
            info["cook_count"] = cc()
        except Exception:
            pass
    _result = info
'''
    return _run(_targeted(path, body))


@mcp.tool()
def cook_node(path: str, force: bool = True) -> str:
    """Force a node to cook at the current frame and report resulting
    errors/warnings. Use after changing upstream parms via run_python, or to
    surface errors that only appear on cook."""
    body = ("FORCE = %s\n" % ("True" if force else "False")) + '''
n = hou.node(TARGET)
if n is None:
    _result = {"error": "node not found: " + TARGET}
else:
    n.cook(force=FORCE)
    _result = {
        "path": n.path(),
        "cooked": True,
        "errors": list(n.errors()),
        "warnings": list(n.warnings()),
    }
'''
    return _run(_targeted(path, body))


@mcp.tool()
def list_selected() -> str:
    """List the nodes currently selected in the Houdini UI (path, type,
    category). Handy when the user says "this node" without giving a path."""
    body = '''
_result = [{"path": n.path(), "type": n.type().name(),
            "category": n.type().category().name()}
           for n in hou.selectedNodes()]
'''
    return _run(body)


# ---------------------------------------------------------------------------
# DOP / simulation tools
# ---------------------------------------------------------------------------
# A dopnet ObjNode exposes .simulation() -> hou.DopSimulation. Objects are
# hou.DopObject; each object's .geometry() is a live hou.Geometry at the current
# frame, so the SOP attribute path works directly on sim state. Reading these
# cooks the sim; use set_frame to move to a different frame first.

@mcp.tool()
def list_dopnets() -> str:
    """List every DOP network in the scene: path, current sim time, and object
    names. Start here for sim work -- get a dopnet path for the other dop_ tools."""
    body = '''
res = []
for n in hou.node("/").allSubChildren():
    try:
        if n.type().name() != "dopnet":
            continue
        d = {"path": n.path()}
        try:
            sim = n.simulation()
            d["sim_time"] = sim.time()
            d["objects"] = [o.name() for o in sim.objects()]
        except Exception as e:
            d["sim_error"] = "%s: %s" % (type(e).__name__, e)
        res.append(d)
    except Exception:
        pass
_result = res
'''
    return _run(body)


@mcp.tool()
def list_dop_objects(dopnet: str) -> str:
    """List the simulation objects in a DOP network with per-object geometry
    counts. `dopnet` is the path from list_dopnets (e.g. /obj/dopnet1). Reveals
    RBD pieces, fluid/smoke/pyro objects, and the constraintnetwork object among
    them. Cooks the sim at the current frame."""
    body = '''
n = hou.node(TARGET)
if n is None:
    _result = {"error": "dopnet not found: " + TARGET}
elif not hasattr(n, "simulation"):
    _result = {"error": TARGET + " is not a DOP network"}
else:
    sim = n.simulation()
    objs = []
    for o in sim.objects():
        rec = {"name": o.name()}
        try:
            g = o.geometry()
            if g is None:
                rec["geo"] = None
            else:
                rec["points"] = g.intrinsicValue("pointcount")
                rec["prims"] = g.intrinsicValue("primitivecount")
                rec["vertices"] = g.intrinsicValue("vertexcount")
        except Exception as e:
            rec["geo_error"] = "%s: %s" % (type(e).__name__, e)
        objs.append(rec)
    _result = {"dopnet": n.path(), "sim_time": sim.time(), "objects": objs}
'''
    return _run(_targeted(dopnet, body))


@mcp.tool()
def get_dop_object(dopnet: str, object_name: str, sample: int = 8) -> str:
    """Inspect a simulation object's live state at the current frame: geometry
    schema, up to `sample` sampled point and prim attribute values, all detail
    attributes, the object's Options record, and its subdata names. This is the
    sim-state analogue of get_attributes -- point it at an RBD packed object to
    read per-piece active/v/w/orient, or at the constraintnetwork object to read
    constraint_name/type/restlength and any broken flags. `sample` clamps 0..64.
    Cooks the sim at the current frame; use set_frame first for a different frame."""
    sample = max(0, min(int(sample), 64))
    body = "OBJNAME = %r\nSAMPLE = %d\n" % (object_name, sample) + '''
import itertools
n = hou.node(TARGET)
if n is None:
    _result = {"error": "dopnet not found: " + TARGET}
elif not hasattr(n, "simulation"):
    _result = {"error": TARGET + " is not a DOP network"}
else:
    sim = n.simulation()
    o = sim.findObject(OBJNAME)
    if o is None:
        _result = {"error": "object %r not found; objects: %s"
                   % (OBJNAME, [x.name() for x in sim.objects()])}
    else:
        try:
            opts = {fn: o.options().field(fn) for fn in o.options().fieldNames()}
        except Exception as e:
            opts = {"_error": "%s: %s" % (type(e).__name__, e)}
        info = {"object": o.name(), "sim_time": sim.time(), "options": opts,
                "subdata": list(o.findAllSubData("*"))}
        g = o.geometry()
        if g is None:
            info["geometry"] = None
        else:
            def sch(attribs):
                return [{"name": a.name(), "type": str(a.dataType()),
                         "size": a.size()} for a in attribs]
            def samp(iterator, attribs):
                cols = {a.name(): [] for a in attribs}
                for el in itertools.islice(iterator, SAMPLE):
                    for a in attribs:
                        cols[a.name()].append(el.attribValue(a))
                return cols
            pa, ra = g.pointAttribs(), g.primAttribs()
            detail = {}
            for a in g.globalAttribs():
                detail[a.name()] = g.attribValue(a)
            info["geometry"] = {
                "point_count": g.intrinsicValue("pointcount"),
                "prim_count": g.intrinsicValue("primitivecount"),
                "point": {"schema": sch(pa), "values": samp(g.iterPoints(), pa)},
                "prim":  {"schema": sch(ra), "values": samp(g.iterPrims(), ra)},
                "detail": detail,
            }
        _result = info
'''
    return _run(_targeted(dopnet, body))


@mcp.tool()
def get_dop_data(dopnet: str, object_name: str) -> str:
    """Read a sim object's scalar state: its Options record fields, plus every
    subdata name and the record fields of each subdata where readable
    (SolverParms, PhysicalParms, Position, etc.). Use for solver/force settings
    baked into the sim that aren't geometry attributes. For deeper traversal use
    run_python with sim.findData('object/SubName')."""
    body = "OBJNAME = %r\n" % object_name + '''
n = hou.node(TARGET)
if n is None:
    _result = {"error": "dopnet not found: " + TARGET}
elif not hasattr(n, "simulation"):
    _result = {"error": TARGET + " is not a DOP network"}
else:
    sim = n.simulation()
    o = sim.findObject(OBJNAME)
    if o is None:
        _result = {"error": "object %r not found; objects: %s"
                   % (OBJNAME, [x.name() for x in sim.objects()])}
    else:
        def rec_fields(rec):
            return {fn: rec.field(fn) for fn in rec.fieldNames()}
        out = {"object": o.name(), "sim_time": sim.time()}
        try:
            out["options"] = rec_fields(o.options())
        except Exception as e:
            out["options_error"] = "%s: %s" % (type(e).__name__, e)
        subs = {}
        for nm in o.findAllSubData("*"):
            entry = {}
            try:
                d = o.findSubData(nm)
                entry["type"] = None if d is None else type(d).__name__
                if d is not None and hasattr(d, "options"):
                    try:
                        entry["fields"] = rec_fields(d.options())
                    except Exception:
                        pass
            except Exception as e:
                entry["error"] = "%s: %s" % (type(e).__name__, e)
            subs[nm] = entry
        out["subdata"] = subs
        _result = out
'''
    return _run(_targeted(dopnet, body))


@mcp.tool()
def set_frame(frame: float, dopnet: str = "") -> str:
    """Set the playbar to `frame`. If `dopnet` is given, cook it to realize the
    sim at that frame -- required before reading sim state at a new frame with the
    dop_ tools. Sims are sequential: jumping forward cooks every intermediate
    substep (can be slow); the DOP cache serves frames already simulated, and
    jumping backward uses the cache."""
    body = ("FRAME = %r\nDOPNET = %r\n" % (float(frame), dopnet)) + '''
hou.setFrame(FRAME)
info = {"frame": hou.frame()}
if DOPNET:
    n = hou.node(DOPNET)
    if n is None:
        info["cook_error"] = "dopnet not found: " + DOPNET
    else:
        n.cook(force=False)
        info["cooked"] = n.path()
        try:
            info["sim_time"] = n.simulation().time()
        except Exception:
            pass
_result = info
'''
    return _run(body)


# ---------------------------------------------------------------------------
# Viewport eyes
# ---------------------------------------------------------------------------
@mcp.tool()
def capture_viewport(width: int = 1280, height: int = 720,
                     viewport: str | None = None) -> Image:
    """Render the current viewport to an image so Claude can SEE the scene at this frame.
    WYSIWYG -- reflects the viewport's current shading/display state (displayed SOP,
    wireframe/shaded, ghosting, background). Reach for this when the question is about
    visual appearance, sim look, shading, or spatial layout: things the structural tools
    (get_geo_stats, get_attributes, get_dop_object) can't answer. Costs image tokens +
    a flipbook render, so use it when the look matters, not on every step. `viewport`
    picks a named view e.g. "persp1"; omit for the currently selected viewport.

    Bypasses _run() on purpose: a base64 PNG dwarfs MAX_RESULT_CHARS, so it reads the raw
    bridge result via _exec() to avoid truncation.
    """
    prefix = "W = %d\nH = %d\nVP = %r\n" % (int(width), int(height), viewport)
    body = r'''
import os, glob, base64

def _grab():
    if not hou.isUIAvailable():
        raise hou.Error("no UI / GL context (headless session)")
    sv = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if sv is None:
        raise hou.Error("no Scene Viewer pane is open")
    vp = sv.findViewport(VP) if VP else sv.curViewport()   # quad layout: curViewport = selected view
    if vp is None:
        raise hou.Error("viewport %r not found" % (VP,))
    f = hou.frame()
    st = sv.flipbookSettings().stash()          # copy: don't clobber interactive flipbook settings
    st.frameRange((f, f))
    st.outputToMPlay(False)                      # file-only; default True would pop MPlay / write nothing
    st.useResolution(True)
    st.resolution((int(W), int(H)))
    st.cropOutMaskOverlay(False)
    d = os.path.join(hou.text.expandString("$HIP") or os.path.expanduser("~"), ".claude_eyes")
    os.makedirs(d, exist_ok=True)
    st.output(os.path.join(d, "snap.$F4.png"))   # $F token REQUIRED or flipbook writes no file
    sv.flipbook(vp, st)                          # blocks until the frame is on disk
    p = os.path.join(d, "snap.%04d.png" % int(round(f)))
    if not os.path.exists(p):
        pngs = glob.glob(os.path.join(d, "snap.*.png"))
        if not pngs:
            raise hou.Error("flipbook wrote no file to %s" % d)
        p = max(pngs, key=os.path.getmtime)
    with open(p, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")

# the bridge marshals exec to Houdini's main thread, so call directly (no self-wrap)
_result = _grab()
'''
    res = _exec(prefix + body)
    if not res.get("ok"):
        out = "ERROR: " + str(res.get("error", "capture failed"))
        if res.get("traceback"):
            out += "\n\n" + res["traceback"]
        return out
    return Image(data=base64.b64decode(res["result"]), format="png")


# ---------------------------------------------------------------------------
# Flipbook (range playblast)
# ---------------------------------------------------------------------------
# Two entry points, one shared settings-resolver. The resolver runs INSIDE the
# session and maps a loose spec dict onto whatever the live FlipbookSettings
# object actually exposes -- so the same request works across Houdini builds and
# degrades instead of throwing when a knob is absent. See module docstring for
# the blocking-vs-pollable tradeoff.

# Snippet fragment (shared): given a `spec` dict already defined in-scope, build a
# stashed FlipbookSettings `st` on scene viewer `sv` for viewport `vp`, applying
# only the settings that exist. Leaves `applied` / `skipped` lists for reporting.
_FLIPBOOK_RESOLVE = r'''
def _resolve_flipbook(sv, spec):
    st = sv.flipbookSettings().stash()   # copy: never clobber interactive settings
    applied, skipped = {}, []

    def _try(label, fn):
        try:
            fn(); applied[label] = spec.get(label, True)
        except Exception as e:
            skipped.append("%s (%s)" % (label, e))

    # --- viewport selection ---
    vpname = spec.get("viewport") or ""
    vp = sv.findViewport(vpname) if vpname else sv.curViewport()
    if vp is None:
        raise hou.Error("viewport %r not found" % (vpname,))

    # --- frame range (required) ---
    fr = spec.get("frame_range")
    if fr:
        _try("frame_range", lambda: st.frameRange((float(fr[0]), float(fr[1]))))

    # --- session label ---
    if spec.get("session_label") and hasattr(st, "sessionLabel"):
        _try("session_label", lambda: st.sessionLabel(str(spec["session_label"])))

    # --- antialias: accept loose string, resolve against hou.flipbookAntialias ---
    aa = spec.get("antialias")
    if aa is not None and hasattr(st, "antialias") and hasattr(hou, "flipbookAntialias"):
        enum = getattr(hou.flipbookAntialias, str(aa), None)
        if enum is None:   # case-insensitive fallback
            for nm in dir(hou.flipbookAntialias):
                if nm.lower() == str(aa).lower():
                    enum = getattr(hou.flipbookAntialias, nm); break
        if enum is None:
            skipped.append("antialias (no such value %r; have %s)"
                           % (aa, [n for n in dir(hou.flipbookAntialias) if not n.startswith('_')]))
        else:
            _try("antialias", lambda: st.antialias(enum))

    # --- resolution: explicit [w,h] wins; else viewport_res True => 100% viewport size ---
    res = spec.get("resolution")
    if res and hasattr(st, "useResolution") and hasattr(st, "resolution"):
        _try("useResolution(True)", lambda: st.useResolution(True))
        _try("resolution", lambda: st.resolution((int(res[0]), int(res[1]))))
    elif spec.get("viewport_res", True) and hasattr(st, "useResolution"):
        # False -> flipbook renders at the viewport's current pixel size (== 100%)
        _try("useResolution(False)", lambda: st.useResolution(False))

    # --- output routing: MPlay vs file sequence ---
    out = spec.get("output") or ""
    if hasattr(st, "outputToMPlay"):
        _try("outputToMPlay", lambda: st.outputToMPlay(not out))
    if out and hasattr(st, "output"):
        # Inject a frame token if none is present, else flipbook overwrites one
        # file. CRITICAL: hou.text.expandString expands $F against the CURRENT
        # frame, so running the whole path through it freezes every frame to one
        # literal filename. Expand ONLY the directory (no frame token lives there)
        # and keep the filename raw so the flipbook resolves $F per frame itself.
        raw = out
        if "$F" not in raw and "%" not in raw and "#" not in raw:
            base, ext = os.path.splitext(raw)
            raw = base + ".$F4" + (ext or ".png")
        d, fname = os.path.split(raw)
        d = hou.text.expandString(d) if d else ""
        outp = (d + "/" + fname) if d else fname     # forward slash: Houdini-native
        if d:
            os.makedirs(d, exist_ok=True)
        _try("output", lambda: st.output(outp))

    # --- optional passthrough of any other stashable knobs the caller named ---
    for k, v in (spec.get("extra") or {}).items():
        fn = getattr(st, k, None)
        if callable(fn):
            _try(k, (lambda f=fn, val=v: f(val)))
        else:
            skipped.append("%s (no such setting)" % k)

    return vp, st, applied, skipped
'''

# Snippet fragment (shared): cache guard. Walks upstream from the displayed SOP
# looking for an Otis/Vellum/DOP solver whose result isn't file-cached; returns a
# warning string (or "") so the tools can refuse unless force=True.
_CACHE_GUARD = r'''
def _display_sop():
    sv = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if sv is None:
        return None
    try:
        return sv.pwd().displayNode()
    except Exception:
        return None

def _heavy_uncached_warning():
    n = _display_sop()
    if n is None:
        return ""
    SOLVERS = ("otissolver", "vellumsolver", "dopnet", "flipsolver",
               "pyrosolver", "rbdbulletsolver", "smokesolver")
    CACHES  = ("filecache", "rop_geometry", "file")
    seen, stack, found_solver, found_cache = set(), [n], False, False
    while stack:
        cur = stack.pop()
        if cur is None or cur.path() in seen:
            continue
        seen.add(cur.path())
        tn = cur.type().name().split("::")[0].lower()
        if any(s in tn for s in SOLVERS):
            found_solver = True
        if any(c == tn for c in CACHES):
            # treat as cache only if it's actually loading from disk
            lp = cur.parm("loadfromdisk") or cur.parm("filemode")
            if lp is None or lp.eval():
                found_cache = True
        for i in cur.inputs():
            if i is not None:
                stack.append(i)
    if found_solver and not found_cache:
        return ("displayed chain contains an uncached solver -- a range flipbook "
                "will re-drive the solve per frame. Cache it first, or pass force=True.")
    return ""
'''

# Snippet fragment (shared): encode an already-written image sequence to H.264
# mp4 with Houdini's bundled hffmpeg. Defines _encode_mp4().
#
# EXACT-MATCH LOGIC. The SceneViewer flipbook bakes the true viewport (geometry,
# lighting, AND the scheme background -- which is a per-pixel GRADIENT) into the
# RGB *only when the output format has no alpha* (e.g. .jpg). Alpha formats
# (.png/.tga) instead write the background transparent-over-black. So:
#   * opaque source (no alpha) -> the frames are ALREADY exact; straight encode,
#     no compositing. This is the WYSIWYG path (marked exact=True).
#   * alpha source (.png) -> the background is genuinely absent. We can composite
#     over a solid colour as a BEST EFFORT, but a flat colour cannot reproduce a
#     gradient, so this is flagged exact=False. background="viewport" samples the
#     live viewport's background colour (median of a tiny opaque grab); a hex/
#     colour name forces one; ""/None keeps straight RGB (black).
# Encoder: prefer the requested codec, fall back to libopenh264 (the bundled
# hffmpeg has no libx264, no gif, no lavfi input device). yuv420p + even dims so
# it plays everywhere. Never raises.
_ENCODE_MP4 = r'''
import os as _os, re as _re, subprocess as _sp

def _hffmpeg():
    hfs = hou.text.expandString("$HFS")
    for nm in ("hffmpeg.exe", "hffmpeg"):
        p = _os.path.join(hfs, "bin", nm)
        if _os.path.exists(p):
            return p
    return None

def _seq_to_printf(fname):
    # $F4 -> %04d, $F -> %d ; leave existing %0Nd / # untouched
    return _re.sub(r'\$F(\d*)',
                   lambda m: ('%0' + m.group(1) + 'd') if m.group(1) else '%d',
                   fname)

def _strip_token(stem):
    # "fb.$F4" -> "fb", "shot_$F4" -> "shot", "img.%04d" -> "img"
    return _re.sub(r'[._]?(\$F\d*|%0?\d*d|#+)', '', stem) or "preview"

def _first_frame(inpat, start):
    try:
        p = inpat % int(start)
    except Exception:
        import glob as _glob
        hits = sorted(_glob.glob(_re.sub(r'%0?\d*d|#+', '*', inpat)))
        p = hits[0] if hits else None
    return p if (p and _os.path.exists(p)) else None

def _frame_info(path):
    # (w, h, has_alpha) or None
    try:
        from PIL import Image as _PILImage
        im = _PILImage.open(path)
        return im.size[0], im.size[1], (im.mode in ("RGBA", "LA", "PA"))
    except Exception:
        return None

def _viewport_bg_hex():
    # colour scheme's flat BackgroundColor (last-resort fallback; ignores gradient/gamma)
    try:
        sv = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
        c = sv.curViewport().settings().colorFromName("BackgroundColor").rgb()
        return "0x%02X%02X%02X" % tuple(max(0, min(255, int(round(x * 255)))) for x in c)
    except Exception:
        return None

def _sample_viewport_bg():
    # true displayed background: tiny opaque grab of the current frame, median of
    # corners. Captures gamma correctly; a single value (flat) for gradients.
    try:
        import glob as _glob, tempfile as _tf
        from PIL import Image as _PILImage
        sv = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
        if sv is None:
            return None
        d = _tf.gettempdir()
        st = sv.flipbookSettings().stash()
        f = hou.frame()
        st.frameRange((f, f)); st.outputToMPlay(False)
        st.useResolution(True); st.resolution((160, 120))
        st.output(_os.path.join(d, "_hbg.$F4.jpg"))
        sv.flipbook(sv.curViewport(), st)
        hits = _glob.glob(_os.path.join(d, "_hbg.*.jpg"))
        if not hits:
            return None
        im = _PILImage.open(hits[0]).convert("RGB"); L = im.load(); w, h = im.size
        cor = [L[1, 1], L[w - 2, 1], L[1, h - 2], L[w - 2, h - 2]]
        med = tuple(sorted(c[i] for c in cor)[1] for i in range(3))
        for hh in hits:
            try: _os.remove(hh)
            except Exception: pass
        return "0x%02X%02X%02X" % med
    except Exception:
        return None

def _encode_mp4(raw_output, start, stop, fps, codec="libopenh264",
                bitrate="12M", background="__VIEWPORT__"):
    ff = _hffmpeg()
    if ff is None:
        return {"encoded": False, "error": "hffmpeg not found under $HFS/bin"}
    d, fname = _os.path.split(raw_output)
    d = hou.text.expandString(d) if d else ""
    inpat = (d + "/" + _seq_to_printf(fname)) if d else _seq_to_printf(fname)
    stem = _strip_token(_os.path.splitext(fname)[0])
    mp4 = (d + "/" + stem + ".mp4") if d else stem + ".mp4"
    even = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    info = _frame_info(_first_frame(inpat, start) or "")
    has_alpha = bool(info[2]) if info else True   # assume alpha if unknown (safe)
    wh = (info[0], info[1]) if info else None

    exact = False
    bg_used = None
    if not has_alpha:
        # opaque source: the flipbook already baked the exact viewport -> straight
        vargs = ["-vf", even + ",format=yuv420p"]
        exact = True
    else:
        # alpha source: reconstruct a background (best effort; flat, not a gradient)
        bg = background
        if bg == "__VIEWPORT__":
            bg = _sample_viewport_bg() or _viewport_bg_hex()
        if bg and wh:
            w, h = wh
            fc = ("color=c=%s:s=%dx%d[bg];[bg][0:v]overlay=shortest=1,%s,format=yuv420p[v]"
                  % (bg, w, h, even))
            vargs = ["-filter_complex", fc, "-map", "[v]"]
            bg_used = bg
        else:
            vargs = ["-vf", even + ",format=yuv420p"]   # straight -> black background

    def _try_codec(cv):
        cmd = ([ff, "-y", "-hide_banner", "-framerate", str(fps),
                "-start_number", str(int(start)), "-i", inpat]
               + vargs
               + ["-c:v", cv, "-pix_fmt", "yuv420p", "-b:v", str(bitrate), mp4])
        r = _sp.run(cmd, capture_output=True, text=True)
        return r.returncode, (r.stderr.strip().splitlines()[-3:] if r.returncode else [])

    rc, err = _try_codec(codec)
    used = codec
    if rc != 0 and codec != "libopenh264":       # graceful fallback to software H.264
        rc, err = _try_codec("libopenh264"); used = "libopenh264"
    ok = (rc == 0 and _os.path.exists(mp4))
    return {"encoded": ok, "mp4": mp4 if ok else None, "codec": used,
            "exact": exact, "source_alpha": has_alpha, "background": bg_used,
            "frames": int(stop) - int(start) + 1,
            "kb": round(_os.path.getsize(mp4) / 1024, 1) if ok else None,
            "match_note": (None if exact else
                           "source has alpha; background is a flat approximation. "
                           "Render stills as .jpg (opaque) for an exact viewport match."),
            "error": None if ok else ("hffmpeg rc=%s: %s" % (rc, err))}
'''


@mcp.tool()
def flipbook_status() -> str:
    """Pre-flight for the flipbook tools. Reports the current Scene Viewer,
    available viewports, playbar range, fps, the resolved 'current viewport'
    name, and -- crucially -- whether the displayed chain has an UNCACHED solver
    upstream (which a range flipbook would re-drive every frame). Call this before
    flipbook_start on anything non-trivial; it's the cache-guard readout."""
    body = _CACHE_GUARD + r'''
if not hou.isUIAvailable():
    _result = {"error": "no UI / GL context (headless session)"}
else:
    sv = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if sv is None:
        _result = {"error": "no Scene Viewer pane is open"}
    else:
        vps = [v.name() for v in sv.viewports()]
        cur = sv.curViewport()
        disp = _display_sop()
        _result = {
            "scene_viewer": sv.name(),
            "viewports": vps,
            "current_viewport": cur.name() if cur else None,
            "playbar_range": list(hou.playbar.frameRange()),
            "current_frame": hou.frame(),
            "fps": hou.fps(),
            "display_sop": disp.path() if disp else None,
            "cache_warning": _heavy_uncached_warning(),
            "antialias_values": [n for n in dir(hou.flipbookAntialias)
                                 if not n.startswith("_")]
                                if hasattr(hou, "flipbookAntialias") else [],
        }
'''
    return _run(body)


@mcp.tool()
def flipbook_start(start: int, stop: int,
                   session_label: str = "",
                   antialias: str = "HighQuality",
                   viewport_res: bool = True,
                   resolution: list | None = None,
                   output: str = "",
                   viewport: str = "",
                   encode_mp4: bool = True,
                   codec: str = "libopenh264",
                   mp4_bitrate: str = "12M",
                   background: str = "viewport",
                   force: bool = False) -> str:
    """Fire-and-forget range flipbook of the CURRENT viewport display, returning
    immediately with a "starting" message. By default writes BOTH the PNG
    sequence AND an H.264 mp4 beside it.

    Maps the request onto the live FlipbookSettings API dynamically: named knobs
    that don't exist on this build are skipped (reported), and `antialias` is a
    loose string resolved against hou.flipbookAntialias (e.g. "HighQuality",
    "Medium", "Off") with graceful fallback. `viewport_res=True` renders at 100%
    of the viewport's pixel size; pass `resolution=[w,h]` to force an explicit
    size instead.

    OUTPUT / EXACT MATCH: the flipbook bakes the exact viewport (geometry,
    lighting, and the scheme background -- a per-pixel gradient) into the frames
    ONLY for opaque formats. So the default output is .jpg, which is a pixel-exact
    WYSIWYG match of your viewport, and the mp4 is a straight encode of it. Use a
    .png output instead if you want the alpha matte (transparent background, for
    compositing) -- but a .png can't hold the baked gradient, so for .png the mp4
    background is a flat best-effort (see `background`) and the return marks
    exact=False. With encode_mp4=True the render always goes to a file sequence
    (MPlay can't be encoded), defaulting to $HIP/preview/<label|preview>.$F4.jpg
    when `output` is empty; the mp4 is the same basename with .mp4. `codec`
    defaults to libopenh264 (the bundled hffmpeg has no libx264); "h264_nvenc"/
    "h264_amf" use the GPU with a libopenh264 fallback. gif is unsupported.

    BACKGROUND (alpha sources only): default "viewport" samples the live viewport
    background colour so a .png mp4 is as close as a flat fill gets; pass a colour
    ("0xRRGGBB"/name) to force one, or "none" for straight RGB (black). Ignored for
    opaque (.jpg) sources, which are already exact.

    DISPATCH: sv.flipbook() blocks Houdini's main thread, so this posts the render
    (and, on completion, the mp4 encode) as a deferred callback and returns right
    away. The bridge is UNRESPONSIVE until both finish; the returned mp4 path is
    where the file WILL be -- re-run ping, then check disk. For pollable progress
    and a synchronous encode report, use flipbook_frames.

    CACHE GUARD: refuses if the displayed chain has an uncached solver upstream
    unless force=True. Run flipbook_status first.
    """
    # encoding needs files on disk; force a sequence output if none was given
    if encode_mp4 and not output:
        stem = session_label if session_label else "preview"
        output = "$HIP/preview/%s.$F4.jpg" % stem
    bg_arg = ("__VIEWPORT__" if background.lower() == "viewport"
              else ("" if background.lower() in ("none", "") else background))
    spec = {
        "frame_range": [int(start), int(stop)],
        "session_label": session_label,
        "antialias": antialias,
        "viewport_res": bool(viewport_res),
        "resolution": [int(resolution[0]), int(resolution[1])] if resolution else None,
        "output": output,
        "viewport": viewport,
    }
    prefix = ("SPEC = %r\nFORCE = %s\nENCODE = %s\nCODEC = %r\nBITRATE = %r\nBG = %r\n"
              % (spec, "True" if force else "False",
                 "True" if encode_mp4 else "False", codec, mp4_bitrate, bg_arg))
    body = prefix + "import os\n" + _FLIPBOOK_RESOLVE + _CACHE_GUARD + _ENCODE_MP4 + r'''
if not hou.isUIAvailable():
    _result = {"error": "no UI / GL context (headless session)"}
else:
    sv = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if sv is None:
        _result = {"error": "no Scene Viewer pane is open"}
    else:
        warn = "" if FORCE else _heavy_uncached_warning()
        if warn:
            _result = {"dispatched": False, "refused": warn,
                       "hint": "cache the solver, or call again with force=True"}
        else:
            vp, st, applied, skipped = _resolve_flipbook(sv, SPEC)
            do_enc = ENCODE and bool(SPEC["output"])
            # planned mp4 path (deterministic) for the return message
            planned_mp4 = None
            if do_enc:
                _d, _fn = os.path.split(SPEC["output"])
                _d = hou.text.expandString(_d) if _d else ""
                _stem = _strip_token(os.path.splitext(_fn)[0])
                planned_mp4 = (_d + "/" + _stem + ".mp4") if _d else _stem + ".mp4"
            # defer the blocking render (+ encode) so THIS exec returns first
            def _go(_sv=sv, _vp=vp, _st=st, _enc=do_enc):
                _sv.flipbook(_vp, _st)                      # blocks until frames on disk
                if _enc:
                    _encode_mp4(SPEC["output"], SPEC["frame_range"][0],
                                SPEC["frame_range"][1], hou.fps(), CODEC, BITRATE, BG)
            import hdefereval
            hdefereval.executeDeferred(_go)
            _result = {
                "dispatched": True,
                "message": "Flipbook %d-%d starting on %s%s"
                           % (SPEC["frame_range"][0], SPEC["frame_range"][1],
                              vp.name(),
                              (" [%s]" % SPEC["session_label"]) if SPEC["session_label"] else ""),
                "applied": applied,
                "skipped": skipped,
                "sequence": "MPlay" if not SPEC["output"] else SPEC["output"],
                "mp4_will_be": planned_mp4,
                "note": "bridge is unresponsive until render+encode finish; "
                        "re-run ping, then check the mp4 path.",
            }
'''
    return _run(body)


@mcp.tool()
def flipbook_frames(start: int, stop: int, cursor: int = -1, chunk: int = 1,
                    session_label: str = "",
                    antialias: str = "HighQuality",
                    viewport_res: bool = True,
                    resolution: list | None = None,
                    output: str = "$HIP/preview/fb.$F4.jpg",
                    viewport: str = "",
                    encode_mp4: bool = True,
                    codec: str = "libopenh264",
                    mp4_bitrate: str = "12M",
                    background: str = "viewport",
                    force: bool = False) -> str:
    """Pollable per-frame range flipbook: renders up to `chunk` frames per call and
    returns progress, keeping the bridge responsive so you (the client) can loop,
    show progress, or cancel. This is the driver to use on anything long enough
    that flipbook_start's dark-bridge window would hurt. By default writes BOTH
    the PNG sequence AND an H.264 mp4.

    Protocol: first call with cursor=-1 renders from `start`. Each return includes
    `next_cursor`; pass it back on the next call. `done` is True when the range is
    complete. Because it advances the playbar with set_frame -> cook per frame, a
    DOP/solver chain solves sequentially and correctly, one frame at a time.

    ENCODE / EXACT MATCH: with encode_mp4=True, once the LAST frame lands (done),
    the sequence is encoded to an mp4 (same basename, .mp4) and reported in
    `encode` on that final return. The flipbook bakes the exact viewport (incl. the
    gradient background) only into opaque formats, so the default output is .jpg --
    a pixel-exact WYSIWYG match, encoded straight (encode.exact=True). Use a .png
    output for the alpha matte instead (transparent bg); .png can't hold the baked
    gradient, so its mp4 background is a flat best-effort and encode.exact=False.
    `codec` defaults to libopenh264 (no libx264/gif in the bundle); "h264_nvenc"/
    "h264_amf" use the GPU with a libopenh264 fallback. `background` (alpha sources
    only) defaults to "viewport" (sampled live); pass "0xRRGGBB"/name to force, or
    "none" for black. Ignored for opaque .jpg (already exact).

    Settings resolve exactly like flipbook_start. `output` MUST be a file path with
    a frame token (default $HIP/preview/fb.$F4.png). Same force/cache-guard, checked
    once on the first call (cursor=-1).
    """
    if not output:
        output = "$HIP/preview/fb.$F4.jpg"
    bg_arg = ("__VIEWPORT__" if background.lower() == "viewport"
              else ("" if background.lower() in ("none", "") else background))
    spec = {
        "frame_range": None,   # set per-frame below
        "session_label": session_label,
        "antialias": antialias,
        "viewport_res": bool(viewport_res),
        "resolution": [int(resolution[0]), int(resolution[1])] if resolution else None,
        "output": output,
        "viewport": viewport,
    }
    prefix = ("SPEC = %r\nSTART = %d\nSTOP = %d\nCURSOR = %d\nCHUNK = %d\nFORCE = %s\n"
              "ENCODE = %s\nCODEC = %r\nBITRATE = %r\nBG = %r\n"
              % (spec, int(start), int(stop), int(cursor), max(1, int(chunk)),
                 "True" if force else "False",
                 "True" if encode_mp4 else "False", codec, mp4_bitrate, bg_arg))
    body = prefix + "import os\n" + _FLIPBOOK_RESOLVE + _CACHE_GUARD + _ENCODE_MP4 + r'''
if not hou.isUIAvailable():
    _result = {"error": "no UI / GL context (headless session)"}
else:
    sv = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if sv is None:
        _result = {"error": "no Scene Viewer pane is open"}
    else:
        first = (CURSOR < 0)
        warn = "" if (FORCE or not first) else _heavy_uncached_warning()
        if warn:
            _result = {"done": False, "refused": warn,
                       "hint": "cache the solver, or call again with force=True"}
        else:
            f0 = START if first else CURSOR
            if f0 > STOP:
                _result = {"done": True, "rendered": [], "next_cursor": f0,
                           "message": "range already complete"}
            else:
                f1 = min(f0 + CHUNK - 1, STOP)
                rendered, errs = [], []
                vp = None
                for f in range(f0, f1 + 1):
                    hou.setFrame(f)
                    disp = _display_sop()
                    if disp is not None:
                        try:
                            disp.cook(force=False)     # realize sim at this frame
                            errs.extend([e[:160] for e in disp.errors()])
                        except Exception as e:
                            errs.append("cook %d: %s" % (f, e))
                    spec_f = dict(SPEC); spec_f["frame_range"] = [f, f]
                    vp, st, applied, skipped = _resolve_flipbook(sv, spec_f)
                    sv.flipbook(vp, st)                # blocks for ONE frame only
                    rendered.append(f)
                nxt = f1 + 1
                done = nxt > STOP
                enc = None
                if done and ENCODE:
                    enc = _encode_mp4(SPEC["output"], START, STOP, hou.fps(),
                                      CODEC, BITRATE, BG)
                _result = {
                    "done": done,
                    "rendered": rendered,
                    "next_cursor": nxt,
                    "remaining": max(0, STOP - f1),
                    "viewport": vp.name() if vp else None,
                    "output_pattern": hou.text.expandString(SPEC["output"]),
                    "cook_errors": errs[:8],
                    "encode": enc,     # populated only on the final (done) call
                }
'''
    return _run(body)


@mcp.tool()
def encode_sequence(output: str, start: int, stop: int,
                    fps: float = 0.0,
                    codec: str = "libopenh264",
                    mp4_bitrate: str = "12M",
                    background: str = "viewport") -> str:
    """Encode an EXISTING PNG (or other image) sequence on disk to an H.264 mp4
    with Houdini's bundled hffmpeg -- no re-render. Use for sequences already
    written (e.g. by an earlier flipbook) or to re-encode at a different codec.

    `output` is the sequence path with a frame token ($F4 / $F, or a %04d / #
    pattern), e.g. "$HIP/preview/fb.$F4.png"; the mp4 is the same basename with a
    .mp4 extension. `start`/`stop` bound the frames. `fps` 0 uses the scene fps.
    `codec` defaults to libopenh264 (the bundle has no libx264); "h264_nvenc" /
    "h264_amf" use the GPU and fall back to libopenh264 if unavailable. gif is
    not supported by the bundled build. `background` defaults to "viewport"
    (composite alpha over the current viewport colour scheme background); pass
    "0xRRGGBB"/colour name to force one, or "none" to keep straight RGB (black)."""
    bg_arg = ("__VIEWPORT__" if background.lower() == "viewport"
              else ("" if background.lower() in ("none", "") else background))
    prefix = ("OUT = %r\nSTART = %d\nSTOP = %d\nFPS = %r\nCODEC = %r\nBITRATE = %r\nBG = %r\n"
              % (output, int(start), int(stop), float(fps), codec, mp4_bitrate, bg_arg))
    body = prefix + _ENCODE_MP4 + r'''
fps = FPS if FPS and FPS > 0 else hou.fps()
_result = _encode_mp4(OUT, START, STOP, fps, CODEC, BITRATE, BG)
'''
    return _run(body)


if __name__ == "__main__":
    mcp.run(transport="stdio")
