"""
python/harness/memory_rpc.py — the memory system's RPC surface.

Ten memory.* handlers, moved out of server.py under the size ratchet
(tests/test_size_ratchet.py) — slice 3 of the cut-down campaign. Each is a
thin wire wrapper; the memory system itself (HTML-NN store, Chroma index,
profiles) lives where it always did, behind the functions these import.
"""

from __future__ import annotations

import logging
from typing import Any

from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)


async def handle_memory_search(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_search as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.search error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_session_search(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_session_search as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.session_search error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_browse(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_browse as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.browse error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_record_completed_turn(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_record_completed_turn as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.record_completed_turn error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_get_user_model(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_get_user_model as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.get_user_model error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_scan_context(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_scan_context as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.scan_context error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_reindex_node(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_reindex_node as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.reindex_node error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_save_node(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_save_node as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.save_node error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_delete_node(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_delete_node as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.delete_node error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_reindex_profiles(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_reindex_profiles as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.reindex_profiles error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
