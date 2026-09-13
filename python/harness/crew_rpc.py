"""
python/harness/crew_rpc.py — the crew domain's RPC surface.

Eleven crew.* handlers, moved out of server.py under the size ratchet
(tests/test_size_ratchet.py) — slice 2 of the cut-down campaign. The domain
logic lives in python.harness.crews (definitions, membership, runs); this
module is the wire format around it, exactly like desktop_rpc and
workflow_rpc before it. Heavier harness/sdk imports stay inside the
functions for the same cycle-dodging reason.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from websockets.asyncio.server import ServerConnection

from python.harness import db
from python.harness.jsonrpc import JSONRPC_VERSION, JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)


async def handle_crew_list(params: dict[str, Any], req_id: Any) -> str:
    """``crew.list`` — every crew, with its headcount."""
    try:
        from python.harness import crews

        return _ok(req_id, {"crews": await crews.listing()})
    except Exception as exc:
        logger.exception("crew.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_crew_create(params: dict[str, Any], req_id: Any) -> str:
    """
    ``crew.create`` — a team, or a department to hold teams.

    A crew is an entity. ``agents.crew_type`` is a *category* — two agents
    labelled "Business" share a classification, not a colleague (UI-050) — and
    it stays as the classification it is.
    """
    try:
        from python.harness import crews

        return _ok(req_id, await crews.create(
            str(params.get("name") or ""),
            str(params.get("kind") or crews.TEAM),
            str(params.get("parent_crew_id") or ""),
            str(params.get("purpose") or ""),
            str(params.get("description") or ""),
        ))
    except Exception as exc:
        from python.harness.crews import CrewError

        if isinstance(exc, CrewError):
            return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
        logger.exception("crew.create error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_crew_update(params: dict[str, Any], req_id: Any) -> str:
    """``crew.update`` — rename a crew, move it, or change what it is for."""
    crew_id = str(params.get("crew_id") or "").strip()
    if not crew_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'crew_id' is required")
    try:
        from python.harness import crews

        fields = {key: params[key] for key in
                  ("name", "kind", "parent_crew_id", "purpose", "description")
                  if key in params}
        return _ok(req_id, await crews.update(crew_id, **fields))
    except Exception as exc:
        from python.harness.crews import CrewError

        if isinstance(exc, CrewError):
            return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
        logger.exception("crew.update error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_crew_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``crew.delete`` — disband a crew.

    Members are released, not deleted, and sub-crews are lifted to this crew's
    own parent. Disbanding a department does not delete the people in it.
    """
    crew_id = str(params.get("crew_id") or "").strip()
    if not crew_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'crew_id' is required")
    try:
        from python.harness import crews

        result = await crews.delete(crew_id)
        await db.log_to_db(
            "INFO",
            f"Crew {crew_id} disbanded: {result['released']} member(s) "
            f"released, {result['lifted']} sub-crew(s) lifted",
        )
        return _ok(req_id, result)
    except Exception as exc:
        from python.harness.crews import CrewError

        if isinstance(exc, CrewError):
            return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
        logger.exception("crew.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_crew_set_membership(params: dict[str, Any], req_id: Any) -> str:
    """
    ``crew.set_membership`` — put an agent in a crew and under a manager.

    Refuses a reporting loop rather than storing one: a cycle makes every walk
    up the line run forever, so work would stop rather than misroute.
    """
    agent_id = str(params.get("agent_id") or "").strip()
    if not agent_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'agent_id' is required")
    try:
        from python.harness import crews

        return _ok(req_id, await crews.set_membership(
            agent_id,
            str(params.get("crew_id") or ""),
            str(params.get("reports_to") or ""),
        ))
    except Exception as exc:
        from python.harness.crews import CrewError

        if isinstance(exc, CrewError):
            return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
        logger.exception("crew.set_membership error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_crew_chart(params: dict[str, Any], req_id: Any) -> str:
    """
    ``crew.chart`` — the whole organisation, for drawing.

    Reports any reporting cycles it finds rather than repairing them: which
    link to break is the owner's decision, and guessing would silently
    restructure their organisation.
    """
    try:
        from python.harness import crews

        return _ok(req_id, await crews.chart())
    except Exception as exc:
        logger.exception("crew.chart error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_crew_assign(params: dict[str, Any], req_id: Any) -> str:
    """
    ``crew.assign`` — route a task into a crew.

    The consumer the crew fields never had (UI-051): it writes
    ``tasks.assigned_agent_id``, which is what the executors read. Marking an
    agent as crew previously changed a chip on a card and nothing else.
    """
    task_id = str(params.get("task_id") or "").strip()
    crew_id = str(params.get("crew_id") or "").strip()
    if not (task_id and crew_id):
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'task_id' and 'crew_id' are required")
    try:
        from python.harness import crews

        result = await crews.assign(task_id, crew_id,
                                    str(params.get("role") or ""))
        if not result["active"]:
            await db.log_to_db(
                "WARNING",
                f"Task {task_id} assigned to {result['assigned_to']}, who is "
                f"not active — every member of that crew is inactive",
            )
        return _ok(req_id, result)
    except Exception as exc:
        from python.harness.crews import CrewError

        if isinstance(exc, CrewError):
            return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
        logger.exception("crew.assign error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_crew_escalate(params: dict[str, Any], req_id: Any) -> str:
    """
    ``crew.escalate`` — send a task up the reporting line.

    Reporting is both ownership and upward work routing; this is the upward
    half, and the reason a hierarchy is worth having rather than drawing. One
    step per call: escalating straight to the top would skip whoever is most
    likely to be able to help.
    """
    task_id = str(params.get("task_id") or "").strip()
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' is required")
    try:
        from python.harness import crews

        return _ok(req_id, await crews.escalate(
            task_id, str(params.get("reason") or "")))
    except Exception as exc:
        from python.harness.crews import CrewError

        if isinstance(exc, CrewError):
            return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
        logger.exception("crew.escalate error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_crew_run(params: dict[str, Any], ws: ServerConnection,
                         req_id: Any) -> str:
    """
    ``crew.run`` — run every member of a crew on one goal, at once.

    Unlike ``crew.assign`` (which routes a single task to a single member), this
    runs the whole team concurrently and, when the crew has a manager, has that
    manager synthesise the members' answers into one. Returns immediately with a
    ``run_id``; progress is streamed as ``crew.run.progress`` notifications and
    the finished run is readable via ``crew.run.get`` / ``crew.runs.list``.
    """
    crew_id = str(params.get("crew_id") or "").strip()
    goal = str(params.get("goal") or "").strip()
    if not (crew_id and goal):
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'crew_id' and 'goal' are required")

    try:
        max_tokens = int(params.get("max_tokens") or 512)
    except (TypeError, ValueError):
        max_tokens = 512

    run_id = f"crewrun-{uuid.uuid4().hex[:12]}"

    async def _notify(event: dict[str, Any]) -> None:
        try:
            await ws.send(json.dumps({
                "jsonrpc": JSONRPC_VERSION,
                "method": "crew.run.progress",
                "params": event,
            }))
        except Exception:
            pass

    async def _background() -> None:
        from python.harness import crews

        try:
            await crews.run_crew(crew_id, goal, notifier=_notify,
                                 run_id=run_id, max_tokens=max_tokens)
        except Exception as exc:
            logger.exception("crew.run background failure")
            await _notify({"event": "failed", "run_id": run_id, "error": str(exc)})

    # Validate the crew before we promise a run, so an obvious mistake comes
    # back as an error rather than a silent background failure.
    try:

        crew_row = await db.fetchrow("SELECT id FROM crews WHERE id = $1", crew_id)
        if not crew_row:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "that crew does not exist")
        member_count = await db.fetchval(
            "SELECT COUNT(*) FROM agents WHERE crew_id = $1", crew_id) or 0
        if member_count == 0:
            return _err(req_id, JsonRpcError.INVALID_PARAMS,
                        "that crew has no members to run")
    except Exception as exc:
        logger.exception("crew.run validation error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

    asyncio.create_task(_background())
    return _ok(req_id, {"run_id": run_id, "crew_id": crew_id, "status": "running",
                        "members": int(member_count)})


async def handle_crew_runs_list(params: dict[str, Any], req_id: Any) -> str:
    """``crew.runs.list`` — recent crew runs, newest first."""
    try:
        from python.harness import crews

        runs = await crews.list_runs(
            crew_id=str(params.get("crew_id") or ""),
            limit=int(params.get("limit") or 50),
        )
        return _ok(req_id, {"runs": runs})
    except Exception as exc:
        logger.exception("crew.runs.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_crew_run_get(params: dict[str, Any], req_id: Any) -> str:
    """``crew.run.get`` — one crew run in full."""
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'run_id' is required")
    try:
        from python.harness import crews

        run = await crews.get_run(run_id)
        if run is None:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "no such run")
        return _ok(req_id, run)
    except Exception as exc:
        logger.exception("crew.run.get error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
