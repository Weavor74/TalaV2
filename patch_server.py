"""
patch_server.py - Auto-generated documentation base.
"""

import re

code_to_inject = """
# ---------------------------------------------------------------------------
# P2P & Multimodal & Browser Methods
# ---------------------------------------------------------------------------

async def handle_p2p_semantic_search(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.retrieval import retrieve_relevant_nodes
    try:
        query = params.get("query", "")
        # Filter internal nodes
        where_filter = {"trust_tier": {"$ne": "internal"}}
        matches = await retrieve_relevant_nodes(query, limit=5, where_filter=where_filter)
        return _ok(req_id, {"matches": matches})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_multimodal_capture_screen(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.multimodal import get_screen_context
    try:
        return _ok(req_id, await get_screen_context())
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_multimodal_transcribe_voice(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.multimodal import transcribe_voice
    try:
        duration = float(params.get("duration_seconds", 5.0))
        return _ok(req_id, await transcribe_voice(duration))
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_browser_get_dom(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.config import settings
    if settings.IS_HEADLESS:
        return _ok(req_id, {"error": "Tala is running in embedded/headless mode and cannot access Browser UI."})
    return _ok(req_id, {"status": "broadcast_sent", "note": "DOM retrieval requires IPC callback setup."})

async def handle_browser_execute_js(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.config import settings
    from python.harness.heartbeat_scheduler import broadcast_notification
    if settings.IS_HEADLESS:
        return _ok(req_id, {"error": "Tala is running in embedded/headless mode and cannot access Browser UI."})
    try:
        code = params.get("code", "")
        await broadcast_notification("browser.execute_js", {"code": code})
        return _ok(req_id, {"status": "broadcast_sent"})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

"""

dictionary_keys = """
    "p2p.semantic_search": handle_p2p_semantic_search,
    "multimodal.capture_screen": handle_multimodal_capture_screen,
    "multimodal.transcribe_voice": handle_multimodal_transcribe_voice,
    "browser.get_dom": handle_browser_get_dom,
    "browser.execute_js": handle_browser_execute_js,
"""

file_path = "d:/src/TalaV2/python/harness/server.py"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Insert functions before _HANDLERS definition
idx = content.find("_HANDLERS: dict[str, Any] = {")
if idx == -1:
    print("Could not find _HANDLERS dict!")
else:
    content = content[:idx] + code_to_inject + content[idx:]
    
    # Now inject keys into the dict
    # Find the next line after _HANDLERS: dict[str, Any] = {
    idx2 = content.find("{", idx) + 1
    content = content[:idx2] + "\n" + dictionary_keys + content[idx2:]
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("Successfully patched server.py!")
