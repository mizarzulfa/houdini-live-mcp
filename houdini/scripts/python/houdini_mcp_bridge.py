"""
houdini_mcp_bridge.py
=====================
Embedded HTTP bridge exposing the LIVE Houdini session to the MCP server
(houdini_live_mcp.py) over Houdini's hwebserver.

Place at:
    $HOUDINI_USER_PREF_DIR/scripts/python/houdini_mcp_bridge.py
    (e.g. Windows: C:\\Users\\<you>\\Documents\\houdini21.0\\scripts\\python\\houdini_mcp_bridge.py)

WHY THIS WORKS ON H21.0.440 (and the old decorator version didn't):
hwebserver on this build is Server-INSTANCE based. The module-level @urlHandler
decorator registers into a table the running server does not consult, so routes
404 even though the socket binds. The fix: get a Server instance, register
handlers as bound methods on THAT instance (server.urlHandler(...)), then run it.
The instance is held in a module global so it can be shut down / re-registered.

MAIN-THREAD EXECUTION:
hwebserver dispatches requests on a worker thread. Running exec() there is only
safe for read-only queries; scene mutation, DOP cooks, and substepping are HOM-
unsound off the main thread. _exec() therefore marshals the snippet onto
Houdini's main thread via hdefereval.executeInMainThreadWithResult. Because of
this, snippets in houdini_live_mcp.py must NOT self-wrap in hdefereval (a nested
main-thread marshal can deadlock). Ship the two files together.

Starting (once per session):
  A) Auto: python3.11libs/uiready.py defers a start() call.
  B) Manual: import houdini_mcp_bridge; houdini_mcp_bridge.start()
"""

import base64
import json
import threading
import traceback

import hdefereval
import hou
import hwebserver

# ---------------------------------------------------------------------------
# Config -- keep in sync with houdini_live_mcp.py.
# ---------------------------------------------------------------------------
PORT = 8008
BIND_ADDRESS = "127.0.0.1"
AUTH_TOKEN = "change-me-to-a-random-secret"     # must match the MCP server

_server = None       # the live hwebserver.Server instance
_STARTED = False


# ---------------------------------------------------------------------------
# JSON serialization for hou.* objects.
# ---------------------------------------------------------------------------
def _to_jsonable(obj, _depth=0):
    if _depth > 12:
        return repr(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x, _depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v, _depth + 1) for k, v in obj.items()}
    for meth in ("asTuple", "asTupleOfTuples"):
        f = getattr(obj, meth, None)
        if callable(f):
            try:
                return _to_jsonable(f(), _depth + 1)
            except Exception:
                pass
    if isinstance(obj, hou.Node):
        return obj.path()
    if isinstance(obj, hou.Parm):
        try:
            return {"parm": obj.name(), "value": _to_jsonable(obj.eval(), _depth + 1)}
        except Exception:
            return obj.name()
    try:
        return str(obj)
    except Exception:
        return repr(obj)


# ---------------------------------------------------------------------------
# Handler functions (plain -- registered on the Server instance in start()).
# ---------------------------------------------------------------------------
def _ping(request):
    payload = {
        "service": "houdini-mcp-bridge",
        "houdini_version": hou.applicationVersionString(),
        "hip_file": hou.hipFile.path(),
        "frame": hou.frame(),
        "fps": hou.fps(),
    }
    return hwebserver.Response(json.dumps(payload), content_type="application/json")


def _run_code_on_main(code):
    """Exec a snippet in a fresh namespace and return _result. Runs on the MAIN
    thread (see _exec), so scene mutation, DOP cooks, and substepping are HOM-
    correct. Exceptions propagate to _exec for traceback capture."""
    ns = {"hou": hou, "hwebserver": hwebserver, "_result": None}
    exec(code, ns)
    return ns.get("_result")


def _exec(request):
    if request.method() != "POST":
        return hwebserver.errorResponse(request, "POST required", 405)
    form = request.POST()
    if form.get("token", "") != AUTH_TOKEN:
        return hwebserver.errorResponse(request, "bad token", 403)
    try:
        code = base64.b64decode(form.get("payload", "")).decode("utf-8")
    except Exception as exc:
        return hwebserver.Response(json.dumps({"ok": False, "error": "payload decode failed: %s" % exc}),
                                   content_type="application/json")
    try:
        # hwebserver dispatches on a worker thread; marshal to Houdini's main
        # thread. Guard the (unlikely) case where a handler already runs on main
        # so we never self-deadlock.
        if threading.current_thread() is threading.main_thread():
            result = _run_code_on_main(code)
        else:
            result = hdefereval.executeInMainThreadWithResult(_run_code_on_main, code)
        body = {"ok": True, "result": _to_jsonable(result)}
    except Exception as exc:
        body = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "traceback": traceback.format_exc()}
    return hwebserver.Response(json.dumps(body), content_type="application/json")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
SERVER_NAME = "mcp_bridge"


def _make_server():
    """Return a named hwebserver.Server instance.

    Confirmed on 21.0.440: the constructor requires a name string
    (PyServer(arg0: str)). Registering handlers on THIS instance and running
    it is the path that actually serves routes -- module-scope @urlHandler
    writes to a table the running server never reads.
    """
    return hwebserver.Server(SERVER_NAME)


def start(port=PORT, address=BIND_ADDRESS):
    """Start the bridge. Registers handlers on the Server INSTANCE (the working
    path on 21.0.440), then runs it in the background."""
    global _server, _STARTED
    if _STARTED and _server is not None:
        print("[mcp-bridge] already started on port %d" % port)
        return

    srv = _make_server()

    # Register on the INSTANCE. server.urlHandler is a bound-method decorator
    # factory; applying it registers into THIS server's table -- the one run()
    # actually serves from.
    srv.urlHandler("/mcp/ping")(_ping)
    srv.urlHandler("/mcp/exec")(_exec)

    # Best-effort loopback restriction via the instance's security API.
    try:
        srv.setSecurity(allowed_hosts=["127.0.0.1", "localhost"])
    except Exception:
        # Signature/kw differs on some builds; token remains the guard.
        pass

    try:
        # in_background defaults True in a graphical session -> returns at once.
        srv.run(port)
    except hwebserver.OperationFailed as exc:
        print("[mcp-bridge] bind failed on %d: %s\n"
              "  Another server may hold the port -- restart Houdini clean." % (port, exc))
        return
    except Exception as exc:
        print("[mcp-bridge] run() raised: %s" % exc)
        return

    _server = srv
    _STARTED = True
    print("[mcp-bridge] listening on http://%s:%d  "
          "(endpoints: /mcp/ping, /mcp/exec)" % (address, port))


def stop():
    """Shut the bridge down (for clean re-register without restarting Houdini)."""
    global _server, _STARTED
    if _server is not None:
        try:
            _server.requestShutdown()
        except Exception:
            pass
    _server = None
    _STARTED = False
    print("[mcp-bridge] stopped")


if __name__ == "__main__":
    start()
