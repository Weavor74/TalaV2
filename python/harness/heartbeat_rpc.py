"""
python/harness/heartbeat_rpc.py — the heartbeat scheduler's RPC surface.

Six heartbeat.* handlers, moved out of server.py under the size ratchet
(tests/test_size_ratchet.py) — slice 2 of the cut-down campaign. The
scheduling itself lives in python.harness.heartbeat_scheduler; this module
is the wire format around it: list, create, update, delete, fire, reload.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from python.harness import db
from python.harness.heartbeat_scheduler import HeartbeatScheduler
from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)


async def handle_heartbeat_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.list`` — Return all configured heartbeat jobs.

    Returns
    -------
    { jobs: list[HeartbeatJob] }
    """
    try:
        scheduler = HeartbeatScheduler.get()
        jobs = scheduler.get_jobs()
        return _ok(req_id, {"jobs": jobs})
    except Exception as exc:
        logger.exception("heartbeat.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_create(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.create`` — Create a new heartbeat job.

    Params
    ------
    name      : str  required
    cron_expr : str  required  e.g. '0 9 * * 1' or '@startup'
    goal_text : str  required  natural-language goal for GoalEngine
    enabled   : bool optional  default true
    """
    name: str = params.get("name", "").strip()
    cron_expr: str = params.get("cron_expr", "").strip()
    goal_text: str = params.get("goal_text", "").strip()
    enabled: bool = bool(params.get("enabled", True))

    if not name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'name' is required")
    if not cron_expr:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'cron_expr' is required")
    if not goal_text:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'goal_text' is required")

    # Validate the cron expression before persisting
    try:
        from python.harness.heartbeat_scheduler import CronExpression
        CronExpression(cron_expr)
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, f"Invalid cron_expr: {exc}")

    job_id = str(uuid.uuid4())
    try:
        await db.execute(
            """
            INSERT INTO heartbeats (id, name, cron_expr, goal_text, enabled)
            VALUES ($1, $2, $3, $4, $5)
            """,
            job_id, name, cron_expr, goal_text, enabled,
        )
        await HeartbeatScheduler.get().reload_from_db()
        return _ok(req_id, {"success": True, "id": job_id})
    except Exception as exc:
        logger.exception("heartbeat.create error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_update(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.update`` — Update an existing heartbeat job.

    Params
    ------
    id        : str  required
    name      : str  optional
    cron_expr : str  optional
    goal_text : str  optional
    enabled   : bool optional
    """
    job_id: str = params.get("id", "").strip()
    if not job_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")

    # Validate cron if provided
    cron_expr = params.get("cron_expr")
    if cron_expr is not None:
        try:
            from python.harness.heartbeat_scheduler import CronExpression
            CronExpression(cron_expr)
        except ValueError as exc:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"Invalid cron_expr: {exc}")

    # Build a dynamic SET clause from provided fields
    updates: list[str] = []
    args: list[Any] = []
    idx = 1

    for field_name in ("name", "cron_expr", "goal_text"):
        if field_name in params:
            updates.append(f"{field_name} = ${idx}")
            args.append(str(params[field_name]).strip())
            idx += 1
    if "enabled" in params:
        updates.append(f"enabled = ${idx}")
        args.append(bool(params["enabled"]))
        idx += 1

    if not updates:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "No fields to update")

    updates.append("updated_at = NOW()")
    args.append(job_id)
    sql = f"UPDATE heartbeats SET {', '.join(updates)} WHERE id = ${idx}"

    try:
        await db.execute(sql, *args)
        await HeartbeatScheduler.get().reload_from_db()
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("heartbeat.update error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.delete`` — Delete a heartbeat job by id.

    Params
    ------
    id : str  required
    """
    job_id: str = params.get("id", "").strip()
    if not job_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")
    try:
        await db.execute("DELETE FROM heartbeats WHERE id = $1", job_id)
        await HeartbeatScheduler.get().reload_from_db()
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("heartbeat.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_fire_now(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.fire_now`` — Manually trigger a heartbeat job immediately.

    Params
    ------
    id : str  required
    """
    job_id: str = params.get("id", "").strip()
    if not job_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")
    try:
        result = await HeartbeatScheduler.get().fire_now(job_id)
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("heartbeat.fire_now error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_reload(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.reload`` — Hot-reload all heartbeat jobs from the database
    without restarting the harness.
    """
    try:
        await HeartbeatScheduler.get().reload_from_db()
        jobs = HeartbeatScheduler.get().get_jobs()
        return _ok(req_id, {"success": True, "job_count": len(jobs)})
    except Exception as exc:
        logger.exception("heartbeat.reload error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
