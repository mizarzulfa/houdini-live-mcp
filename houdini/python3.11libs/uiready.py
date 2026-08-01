# =============================================================================
# uiready.py
# -----------------------------------------------------------------------------
# Auto-starts the houdini-live MCP bridge when the interactive Houdini app is
# ready. Place at:
#     $HOUDINI_USER_PREF_DIR/python3.11libs/uiready.py
#     (e.g. Windows: C:\Users\<you>\Documents\houdini21.0\python3.11libs\uiready.py)
#
# Starts the bridge on a deferred idle tick (not inline) so hwebserver's own
# init has fully settled before we register /mcp handlers -- calling start()
# inline during uiready races the embedded server and leaves it up with no
# routes (server answers, /mcp/ping returns 404).
# =============================================================================

import hou


def _start_mcp_bridge():
    try:
        import houdini_mcp_bridge
    except ImportError as exc:
        print("[mcp-bridge] auto-start skipped: import failed (%s)" % exc)
        return
    try:
        houdini_mcp_bridge.start()
    except Exception:
        import traceback
        print("[mcp-bridge] auto-start FAILED:")
        traceback.print_exc()


def _deferred_start():
    # Remove ourselves first so this only ever runs once, then start.
    try:
        hou.ui.removeEventLoopCallback(_deferred_start)
    except hou.OperationFailed:
        pass
    _start_mcp_bridge()


hou.ui.addEventLoopCallback(_deferred_start)
