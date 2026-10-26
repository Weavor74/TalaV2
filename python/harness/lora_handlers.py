"""
python/harness/lora_handlers.py -- the renderer's way into the LoRA review queue and the persona adapter.

``lora.candidates`` and ``lora.decide`` are the queue (sdk/agent/lora_approvals.py): nothing is trained on unless it was approved
there. ``lora.status`` is one call for the whole panel. ``lora.train`` starts a run in the background and returns at once,
``lora.rollback`` restores the previous adapter, and ``lora.settings`` switches scheduled training and turn routing.

None of these is on the emergency halt's allow-list, so the dispatch refuses every one while halted; each mutating handler also
re-reads the halt itself (fail closed), so a caller that does not go through the dispatch cannot start a run or change a decision.
Validation problems come back as ``{"success": false, "error": ...}`` rather than a protocol error: the panel shows the sentence.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any

from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)

STATUSES = ("pending", "approved", "rejected", "blocked")
KINDS = ("chat", "memory")
MAX_PAGE = 500
MAX_DECIDE_IDS = 500


def _refused_for_halt() -> dict[str, Any] | None:
    from python.harness.halt_gate import is_halted_fail_closed

    if is_halted_fail_closed():
        return {"success": False, "halted": True, "error": "the emergency halt is engaged; release it first"}
    return None


def _bad(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


def _refused_while_running() -> dict[str, Any] | None:
    """A run in progress is reading the registry and the settings and is about to write the registry: changing either under it
    would make its result describe a state that no longer exists. Wait for it to finish."""
    from sdk.agent import lora_pipeline

    if lora_pipeline.is_running():
        return _bad("a training run is in progress; wait for it to finish before changing this")
    return None


def _int(value: Any, default: int, low: int, high: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _settings() -> dict[str, Any]:
    from python.inference import persona_adapter

    from sdk.agent import lora_pipeline

    return {"auto_train": bool(lora_pipeline.settings()["lora_auto_train"]), "routing": persona_adapter.routing_mode()}


async def _handle_lora_candidates(params: dict[str, Any], req_id: Any) -> str:
    """``lora.candidates`` -- {status?, kind?, limit?, offset?} -> {items, total, counts}."""
    try:
        from sdk.agent import lora_approvals

        params = params or {}
        status, kind = params.get("status"), params.get("kind")
        if status is not None and status not in STATUSES:
            return _ok(req_id, _bad(f"status must be one of {', '.join(STATUSES)}"))
        if kind is not None and kind not in KINDS:
            return _ok(req_id, _bad(f"kind must be one of {', '.join(KINDS)}"))
        limit, offset = _int(params.get("limit"), 200, 1, MAX_PAGE), _int(params.get("offset"), 0, 0, 10**7)
        if limit is None or offset is None:
            return _ok(req_id, _bad(f"limit must be 1-{MAX_PAGE} and offset 0 or more"))
        return _ok(req_id, await lora_approvals.list_candidates(status=status, kind=kind, limit=limit, offset=offset))
    except Exception as exc:
        logger.exception("lora.candidates error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_lora_decide(params: dict[str, Any], req_id: Any) -> str:
    """``lora.decide`` -- {ids: [id, ...], decision: approved|rejected|pending} -> {success, updated, refused}."""
    try:
        from sdk.agent import lora_approvals

        if (refusal := _refused_for_halt()) is not None:
            return _ok(req_id, refusal)
        params = params or {}
        ids, decision = params.get("ids"), params.get("decision")
        if decision not in lora_approvals.DECISIONS:
            return _ok(req_id, _bad(f"decision must be one of {', '.join(lora_approvals.DECISIONS)}"))
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_DECIDE_IDS or not all(isinstance(i, str) and i for i in ids):
            return _ok(req_id, _bad(f"ids must be a list of 1-{MAX_DECIDE_IDS} item ids"))
        return _ok(req_id, {"success": True, **await lora_approvals.decide(ids, decision)})
    except Exception as exc:
        logger.exception("lora.decide error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_lora_status(params: dict[str, Any], req_id: Any) -> str:
    """``lora.status`` -- {pipeline, persona, counts, settings}: everything the panel shows, in one call. A database that is down
    costs the counts (``counts`` None, ``counts_error`` says why) and nothing else."""
    try:
        from python.inference import persona_adapter
        from sdk.agent import lora_approvals, lora_pipeline

        result: dict[str, Any] = {"pipeline": lora_pipeline.status(), "persona": persona_adapter.status(), "settings": _settings()}
        try:
            result["counts"] = (await lora_approvals.list_candidates(limit=1))["counts"]
        except Exception as exc:
            result["counts"], result["counts_error"] = None, str(exc)
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("lora.status error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_lora_train(params: dict[str, Any], req_id: Any) -> str:
    """``lora.train`` -- start a run now, in the background -> {started, reason}. The run is hours long; this returns at once."""
    try:
        from sdk.agent import lora_pipeline

        if (refusal := _refused_for_halt()) is not None:
            return _ok(req_id, {"started": False, **refusal})
        outcome = lora_pipeline.start_background(reason="ui", force=True)  # a plain function: it schedules the run and returns a dict
        started = outcome.get("status") == "started"
        return _ok(req_id, {"started": started, "reason": "started" if started else (outcome.get("reason") or outcome.get("status"))})
    except Exception as exc:
        logger.exception("lora.train error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_lora_rollback(params: dict[str, Any], req_id: Any) -> str:
    """``lora.rollback`` -- activate the adapter before the current one -> {success, active}. The persona server is stopped so the next
    turn serves the restored adapter, and the persona provider row is brought in line. No earlier adapter means the persona is off."""
    try:
        from python.inference import persona_adapter
        from sdk.agent import lora_pipeline

        if (refusal := _refused_for_halt()) is not None:
            return _ok(req_id, refusal)
        if (busy := _refused_while_running()) is not None:
            return _ok(req_id, busy)
        entry = persona_adapter.rollback()
        await persona_adapter.stop()
        synced = lora_pipeline.sync_provider_row()
        if inspect.isawaitable(synced):
            await synced
        return _ok(req_id, {"success": True, "active": entry["id"] if entry else None})
    except Exception as exc:
        logger.exception("lora.rollback error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_lora_settings(params: dict[str, Any], req_id: Any) -> str:
    """``lora.settings`` -- {auto_train?: bool, routing?: off|fallback|in_character}; with neither, just reads -> {success, settings}."""
    try:
        from python.inference import persona_adapter
        from sdk.agent import node_learning as nl

        params = params or {}
        updates: dict[str, Any] = {}
        if "auto_train" in params:
            if not isinstance(params["auto_train"], bool):
                return _ok(req_id, _bad("auto_train must be true or false"))
            updates["lora_auto_train"] = params["auto_train"]
        if "routing" in params:
            if params["routing"] not in persona_adapter.ROUTING_MODES:
                return _ok(req_id, _bad(f"routing must be one of {', '.join(persona_adapter.ROUTING_MODES)}"))
            updates["lora_routing"] = params["routing"]
        if updates:
            if (refusal := _refused_for_halt()) is not None:
                return _ok(req_id, refusal)
            if (busy := _refused_while_running()) is not None:
                return _ok(req_id, busy)
            nl.write_config(updates)
        return _ok(req_id, {"success": True, "settings": _settings()})
    except Exception as exc:
        logger.exception("lora.settings error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


LORA_RPC_HANDLERS = {
    "lora.candidates": _handle_lora_candidates,
    "lora.decide": _handle_lora_decide,
    "lora.status": _handle_lora_status,
    "lora.train": _handle_lora_train,
    "lora.rollback": _handle_lora_rollback,
    "lora.settings": _handle_lora_settings,
}
