"""
python/harness/learning_rpc.py -- the renderer's way into node learning.

``learning.feedback`` is the thumbs up / thumbs down / "that's wrong" on a reply: it moves the confidence of the nodes
the reply was built from. ``learning.status`` and ``learning.set_enabled`` back the settings toggle and the debug view;
``learning.dream_now`` runs the consolidation by hand; ``learning.reset_weights`` is the admin reset and goes through the
tool registry, so the AuthorizationGate and the leases apply. None of these is on the emergency halt's allow-list, so a
halt refuses all of them.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)

FEEDBACK_KINDS = ("up", "down", "wrong")
MAX_FEEDBACK_NODES = 8
_NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _nodes_dir():
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    return resolve_active_nodes_dir()


async def _handle_learning_feedback(params: dict[str, Any], req_id: Any) -> str:
    """``learning.feedback`` -- {node_ids: [id, ...] (1-8), kind: up|down|wrong} -> {changes, skipped?}."""
    try:
        from sdk.agent import node_learning as nl

        ids, kind = (params or {}).get("node_ids"), (params or {}).get("kind")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_FEEDBACK_NODES
                or not all(isinstance(i, str) and _NODE_ID.match(i) for i in ids)):
            return _err(req_id, JsonRpcError.INVALID_PARAMS,
                        f"node_ids must be a list of 1-{MAX_FEEDBACK_NODES} node ids")
        if kind not in FEEDBACK_KINDS:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"kind must be one of {', '.join(FEEDBACK_KINDS)}")
        if not nl.learning_active():
            reason = "learning is disabled" if not nl.learning_enabled() else "the emergency halt is engaged"
            return _ok(req_id, {"changes": [], "skipped": reason})
        return _ok(req_id, {"changes": nl.apply_feedback(ids, kind, _nodes_dir(), reason=f"user feedback: {kind}")})
    except Exception as exc:
        logger.exception("learning.feedback error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_learning_status(params: dict[str, Any], req_id: Any) -> str:
    """``learning.status`` -- the setting, halt state, how many nodes learning has touched, the recent log."""
    try:
        from sdk.agent import node_learning as nl

        return _ok(req_id, nl.status(_nodes_dir()))
    except Exception as exc:
        logger.exception("learning.status error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_learning_set_enabled(params: dict[str, Any], req_id: Any) -> str:
    """``learning.set_enabled`` -- {enabled: bool} persists the toggle."""
    try:
        from sdk.agent import node_learning as nl

        enabled = (params or {}).get("enabled")
        if not isinstance(enabled, bool):
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "enabled must be true or false")
        nl.set_learning_enabled(enabled)
        return _ok(req_id, {"enabled": nl.learning_enabled()})
    except Exception as exc:
        logger.exception("learning.set_enabled error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_learning_dream_now(params: dict[str, Any], req_id: Any) -> str:
    """``learning.dream_now`` -- run the dream immediately (skips the schedule, never the halt or the off switch)."""
    try:
        from sdk.agent.dreaming import run_dream

        outcome = await run_dream(force=True)
        if outcome is None:
            return _err(req_id, JsonRpcError.INTERNAL_ERROR, "the dream did not run: learning is disabled or halted")
        return _ok(req_id, outcome)
    except Exception as exc:
        logger.exception("learning.dream_now error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_learning_reset_weights(params: dict[str, Any], req_id: Any) -> str:
    """``learning.reset_weights`` -- {confirm: true, node_ids?} through the gated tool registry."""
    try:
        from sdk.agent.tools import ToolRegistry

        if (params or {}).get("confirm") is not True:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "reset needs an explicit confirm=true")
        result = await ToolRegistry.run_tool(
            "reset_node_weights", {"confirm": True, "node_ids": (params or {}).get("node_ids")})
        if not isinstance(result, dict) or result.get("success") is False:
            message = result.get("error") if isinstance(result, dict) else str(result)
            return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"reset refused: {message}")
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("learning.reset_weights error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


LEARNING_RPC_HANDLERS = {
    "learning.feedback": _handle_learning_feedback,
    "learning.status": _handle_learning_status,
    "learning.set_enabled": _handle_learning_set_enabled,
    "learning.dream_now": _handle_learning_dream_now,
    "learning.reset_weights": _handle_learning_reset_weights,
}
