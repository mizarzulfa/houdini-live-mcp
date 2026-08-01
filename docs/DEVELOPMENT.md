# Development notes

The technical companion to the [README](../README.md): architecture, full tool reference, and the design decisions that will bite you if you change them.

This project is two [MCP](https://modelcontextprotocol.io) servers connecting **Claude Desktop** to **SideFX Houdini**:

| Server | Answers | How |
|---|---|---|
| **houdini-docs** | *"How does X work? What's the signature of Y?"* | Searches and reads your **local** Houdini help server (offline, version-exact docs) |
| **houdini-live** | *"What is my actual scene doing right now?"* | Talks to a small HTTP bridge running **inside** your live Houdini session — reads nodes, VEX, geometry, DOP state, captures the viewport, drives flipbooks |

Together they let Claude debug a wrangle by *reading the real code and real geometry*, inspect live sim state per frame, look at your viewport, and cross-check everything against the exact docs for your Houdini build — without you copy-pasting anything.

Built and tested on **Houdini 21.0.440 (Windows)**, Python 3.13, `mcp >= 1.28`. MIT licensed.

---

## Architecture

```
┌────────────────┐  stdio (JSON-RPC)  ┌─────────────────────┐
│ Claude Desktop │◄──────────────────►│ houdini_docs_mcp.py │──── HTTP ──► Houdini help server
│                │                    └─────────────────────┘             (embedded, port 48626,
│  (MCP client)  │                                                         auto-runs with the app)
│                │  stdio (JSON-RPC)  ┌─────────────────────┐
│                │◄──────────────────►│ houdini_live_mcp.py │
└────────────────┘                    └──────────┬──────────┘
                                                 │ HTTP POST /mcp/exec
                                                 │ (base64 Python snippet + token,
                                                 │  127.0.0.1:8008)
                                      ┌──────────▼──────────────────────────┐
                                      │ houdini_mcp_bridge.py               │
                                      │ inside Houdini, on hwebserver.      │
                                      │ Marshals every snippet onto         │
                                      │ Houdini's MAIN thread, runs it,     │
                                      │ JSON-serializes `_result` back.     │
                                      │ Auto-started by uiready.py.         │
                                      └─────────────────────────────────────┘
```

Key idea: every curated tool in `houdini_live_mcp.py` builds a tiny Python snippet that assigns `_result`, and POSTs it to the bridge's single `/mcp/exec` endpoint. **New capabilities are new snippets in the server file — no Houdini restart required.** `run_python` is the general escape hatch.

## Repository layout & where each file goes

```
houdini-live-mcp/
├── README.md
├── LICENSE                                  MIT
├── .gitignore
├── server/                                  ← runs OUTSIDE Houdini (launched by Claude Desktop via uv)
│   ├── pyproject.toml                         uv project; deps: mcp[cli], httpx, beautifulsoup4
│   ├── uv.lock
│   ├── .python-version                        3.13
│   ├── houdini_docs_mcp.py                    the "houdini-docs" MCP server
│   └── houdini_live_mcp.py                    the "houdini-live" MCP server
├── houdini/                                 ← copy contents INTO $HOUDINI_USER_PREF_DIR
│   ├── scripts/python/houdini_mcp_bridge.py   the in-Houdini HTTP bridge
│   └── python3.11libs/uiready.py              auto-starts the bridge when the UI is ready
└── docs/DEVELOPMENT.md                      this file
```

| File | Install location | Why there |
|---|---|---|
| `server/*` | Anywhere you like (wherever you clone this repo) | Claude Desktop launches it by absolute path via `uv` |
| `houdini/scripts/python/houdini_mcp_bridge.py` | `$HOUDINI_USER_PREF_DIR/scripts/python/` <br>e.g. `C:\Users\<you>\Documents\houdini21.0\scripts\python\` | That directory is on Houdini's Python path, so `import houdini_mcp_bridge` works |
| `houdini/python3.11libs/uiready.py` | `$HOUDINI_USER_PREF_DIR/python3.11libs/` <br>e.g. `C:\Users\<you>\Documents\houdini21.0\python3.11libs\` | Houdini executes `uiready.py` from `python3.11libs` when the interactive UI finishes loading — this is what auto-starts the bridge every session |

> `$HOUDINI_USER_PREF_DIR` on Windows defaults to `Documents\houdini21.0` (match the folder to your major.minor version). On Linux it's `~/houdini21.0`, on macOS `~/Library/Preferences/houdini/21.0`. If you already have a `uiready.py`, merge the callback registration into yours instead of overwriting.

## Installation

### 1. Clone and resolve the server environment

```bash
git clone https://github.com/<you>/houdini-live-mcp.git
cd houdini-live-mcp/server
uv sync
```

Requires [uv](https://docs.astral.sh/uv/). Python 3.13 is fetched automatically if missing.

### 2. Install the Houdini-side files

Copy the two files under `houdini/` into your `$HOUDINI_USER_PREF_DIR` as per the table above.

### 3. Set the shared secret

The bridge only executes snippets carrying the right token. Change it **in both files** (they must match):

- `houdini/scripts/python/houdini_mcp_bridge.py` → `AUTH_TOKEN = "..."`
- `server/houdini_live_mcp.py` → `AUTH_TOKEN = "..."`

Port defaults to `8008` on `127.0.0.1` (`PORT` / `BIND_ADDRESS` in the bridge, `HOST` / `PORT` in the server — keep in sync too).

### 4. Register the servers with Claude Desktop

Edit `claude_desktop_config.json` (Claude Desktop → Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "houdini-docs": {
      "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
      "args": ["--directory", "C:\\path\\to\\houdini-live-mcp\\server", "run", "houdini_docs_mcp.py"]
    },
    "houdini-live": {
      "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
      "args": ["--directory", "C:\\path\\to\\houdini-live-mcp\\server", "run", "houdini_live_mcp.py"]
    }
  }
}
```

Use the full path to `uv` — Claude Desktop doesn't inherit your shell `PATH`.

> **Windows Store install note:** if Claude Desktop was installed from the Microsoft Store, `%APPDATA%\Claude\claude_desktop_config.json` is virtualized to
> `C:\Users\<you>\AppData\Local\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`.
> Editing through the app's Settings UI always lands in the right place.

### 5. Restart both apps and verify

1. Restart Claude Desktop (it spawns the two servers).
2. Start Houdini — the Python Shell should print:
   ```
   [mcp-bridge] listening on http://127.0.0.1:8008  (endpoints: /mcp/ping, /mcp/exec)
   ```
   If it didn't auto-start: `import houdini_mcp_bridge; houdini_mcp_bridge.start()`
3. In Claude, ask it to run the `ping` tool → should report your Houdini version, hip file, frame, and fps.
4. `search_docs` works whenever Houdini is open (the embedded help server auto-runs on port 48626). Headless fallback: `hhelp serve --host=127.0.0.1 --port=8080`.

## Tool reference

### houdini-docs (3 tools)

| Tool | Purpose |
|---|---|
| `search_docs(query, category="")` | Search local docs via `/_search`. Returns instant matches (VEX signatures inline) + categorized hits with exact paths. Categories: `vex`, `node/sop`, `node/dop`, `node/vop`, `_` (user guide), `tool`, `example`, `hscript`, `hommethod` |
| `get_doc_page(path)` | Fetch a doc page by the exact path `search_docs` returned (never guess paths) |
| `get_vex_function(name)` | VEX function docs — direct path first, search fallback |

Doc server resolution: probes `127.0.0.1:48626` (embedded) then `:8080` (`hhelp serve`); override with env var `HOUDINI_DOCS_URLS` (comma-separated base URLs). The resolved base is cached and self-heals if Houdini is closed/reopened.

### houdini-live (20 tools)

**Session & escape hatch**

| Tool | Purpose |
|---|---|
| `ping()` | Bridge reachable? Reports version / hip / frame / fps. Call first when anything errors |
| `run_python(code)` | Arbitrary Python in the live session, `hou` pre-imported; snippet must assign `_result` |

**Nodes, VEX & parameters**

| Tool | Purpose |
|---|---|
| `get_node(path)` | Type, flags, inputs/outputs, child count, cook errors/warnings |
| `get_wrangle_code(path)` | A wrangle's VEX snippet + Run Over class, group, precision, errors — the go-to for debugging a wrangle |
| `get_parms(path, names="")` | Evaluated parameter values (optionally filtered) |
| `get_cook_errors(path)` | Why is this node red/yellow |
| `cook_node(path, force=True)` | Force-cook and report resulting errors |
| `list_selected()` | Nodes selected in the UI — resolves "this node" |

**Geometry**

| Tool | Purpose |
|---|---|
| `get_geo_stats(path)` | Counts, bbox, full attribute schema per class, groups — cheap, no per-element values |
| `get_attributes(path, sample=8)` | Actual sampled attribute values (bounded, safe on heavy geo) |

**DOP / simulation**

| Tool | Purpose |
|---|---|
| `list_dopnets()` | Every DOP network + sim time + object names — start here |
| `list_dop_objects(dopnet)` | Sim objects with per-object geo counts |
| `get_dop_object(dopnet, object_name, sample=8)` | Live sim-state geometry: schema, sampled values, Options record, subdata names. Point at an RBD packed object for per-piece `active/v/w/orient`, or the constraintnetwork for constraint state |
| `get_dop_data(dopnet, object_name)` | Scalar solver state: Options + every subdata's record fields |
| `set_frame(frame, dopnet="")` | Move the playbar (and optionally cook the dopnet) before reading sim state at another frame |

**Viewport & flipbook**

| Tool | Purpose |
|---|---|
| `capture_viewport(width, height, viewport=None)` | Single-frame viewport render returned as an image — Claude can *see* the scene |
| `flipbook_status()` | Pre-flight: viewports, playbar, fps, display SOP, and the **uncached-solver warning** |
| `flipbook_start(...)` | Fire-and-forget range flipbook + H.264 mp4. Bridge goes dark until done — for short look-checks |
| `flipbook_frames(...)` | Pollable per-frame driver (`cursor` protocol) — keeps the bridge responsive for long ranges; encodes mp4 on the final call |
| `encode_sequence(...)` | Encode an existing on-disk sequence to mp4, no re-render |

## Design notes (the things that will bite you if you change them)

- **Main-thread execution.** `hwebserver` dispatches requests on a worker thread; scene mutation, DOP cooks, and substepping are HOM-unsound off the main thread. The bridge marshals every snippet through `hdefereval.executeInMainThreadWithResult`. Consequently **snippets must NOT self-wrap in hdefereval** — a nested main-thread marshal can deadlock. Ship bridge + server as a pair.
- **hwebserver on H21.0.440 is Server-instance based.** The module-level `@urlHandler` decorator registers into a table the running server never consults (routes 404 while the socket binds). Handlers must be registered on the instance: `srv.urlHandler("/mcp/exec")(_exec)`. That's why the bridge looks the way it does.
- **Deferred auto-start.** `uiready.py` starts the bridge on a deferred event-loop tick, not inline — starting inline races hwebserver's own init and leaves a route-less server up.
- **stdio discipline.** `houdini_live_mcp.py` must never print to stdout; that corrupts the JSON-RPC stream. Diagnostics come back as tool text.
- **Flipbook exact-match rule.** The flipbook bakes the true viewport (including the gradient background) into the frames only for **opaque** formats — so `.jpg` output is pixel-exact WYSIWYG and encodes straight; `.png` keeps the alpha matte but the mp4 background becomes a flat best-effort (sampled from the live viewport by default).
- **Bundled hffmpeg** (`$HFS/bin`) has **no libx264, no gif encoder, no lavfi**. Default codec is `libopenh264`; `h264_nvenc` / `h264_amf` are tried with graceful fallback.
- **Cache guard.** Range flipbooks refuse to run if the displayed chain contains an uncached solver (it would re-drive the solve per frame) unless `force=True`. `flipbook_status` is the readout.
- **Result size guard.** Bridge results are truncated at 20k chars to protect the model's context; sampling tools clamp at 64 elements.

## Security

The bridge executes arbitrary Python inside your Houdini session — that is its job. It is guarded by (a) binding to `127.0.0.1` only, (b) a best-effort `allowed_hosts` restriction, and (c) the shared `AUTH_TOKEN`. **Change the token from the placeholder**, and don't port-forward 8008.

## Extending

Add a curated tool = add an `@mcp.tool()` function in `houdini_live_mcp.py` that builds a snippet assigning `_result`, and `return _run(snippet)`. Restart Claude Desktop (it respawns the server); Houdini keeps running — the bridge doesn't change.

## License

[MIT](../LICENSE)
