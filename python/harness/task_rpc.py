"""
python/harness/task_rpc.py — the task lifecycle's RPC surface.

Fourteen task.* handlers plus the retention sweep they share, moved out of
server.py under the size ratchet (tests/test_size_ratchet.py) — slice 3 of
the cut-down campaign. Submission and control talk to the orchestrator and
the tasks table; status/phase_events/artifacts are thin reads over
sdk.agent.status_api; prune_finished_tasks (and its retention constants)
travel with their only caller. task.report is not here: it moved to
agent_bridge_handlers.py long before this file existed.

Heavier harness/sdk imports stay inside the functions, like every *_rpc
module: server imports this at module load, and sdk.agent.tools pulls the
harness back in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection

from python.harness import db
from python.harness.jsonrpc import JsonRpcError, _err, _ok
from sdk.agent.documentation_nodes import resolve_active_nodes_dir

logger = logging.getLogger(__name__)

#: Repo root for worktree fallbacks — the same derivation workflows_util uses.
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent


async def handle_task_submit(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.submit`` — Accept a goal, decompose it, and queue/execute it using GoalEngine.
    """
    goal = params.get("goal", "").strip()
    if not goal:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'goal' is required")
    
    try:
        from sdk.agent.queue import GoalTreeDecomposer
        from sdk.agent.persistence import generate_deterministic_id
        from sdk.agent.goal_engine import GoalEngine
        from sdk.agent.adapter import AdapterCompiler
        from python.harness.retrieval import retrieve_relevant_nodes
        from pathlib import Path
        
        # 0. Retrieve relevant context nodes and compile AdapterPacket
        # user_data_path is an explicit per-request RPC param override; falls
        # back to the standard TALA_USER_DATA/APPDATA/app-relative resolver.
        user_data = params.get("user_data_path")
        if user_data:
            nodes_dir = Path(user_data) / "nodes"
        else:
            nodes_dir = resolve_active_nodes_dir()

        if not nodes_dir.exists():
            repo_nodes = Path(_ROOT_DIR) / "nodes"
            if repo_nodes.exists():
                nodes_dir = repo_nodes

        adapter_packet = None
        if nodes_dir.exists() and nodes_dir.is_dir():
            matched = retrieve_relevant_nodes(goal, nodes_dir, top_k=5, threshold=0.0)
            if matched:
                nodes = [node for node, score in matched]
                
                try:
                    rows = await db.fetch("SELECT access_rights FROM firewall_contacts")
                    contacts = [{"access_rights": dict(r["access_rights"])} for r in rows if r["access_rights"]]
                    
                    from sdk.agent.trust_registry import TrustRegistry
                    registry = TrustRegistry(contacts)
                    nodes = registry.verify_and_cap_nodes(nodes)
                except Exception as e:
                    logger.warning("Failed to apply TrustRegistry: %s", e)
                
                adapter_packet = AdapterCompiler.compile(nodes)
        
        
        # 1. Decompose goal to get count for schema compatibility
        decomposer = GoalTreeDecomposer()
        task_items = await decomposer.decompose(goal)
        if not task_items:
            return _err(req_id, JsonRpcError.INTERNAL_ERROR, "Decomposition produced no tasks")
        
        parent_task_id = generate_deterministic_id(goal, "goal-parent")
        
        # 2. Start Goal Engine in background task
        engine = GoalEngine(_ROOT_DIR)
        
        from sdk.agent.batch_policy import BatchPolicy
        authority_level = params.get("authority_level", "auto")
        max_steps = params.get("max_steps")
        
        policy = BatchPolicy.auto()
        if authority_level:
            policy.authority_level = authority_level
        if max_steps is not None:
            try:
                policy.max_steps = int(max_steps)
            except (ValueError, TypeError):
                pass
        
        async def run_goal_engine():
            try:
                await engine.run_goal(
                    goal,
                    policy=policy,
                    adapter_packet=adapter_packet,
                    pre_decomposed_tasks=task_items,  # avoid a second LLM decomposition round-trip
                )
            except Exception as _bg_exc:
                logger.exception(
                    "task.submit: GoalEngine background task crashed for task_id=%s", parent_task_id
                )
                # Mark the parent task as FAILED so the UI doesn't hang on "in_progress"
                try:
                    _err_payload = json.dumps({"error": str(_bg_exc)})
                    await db.execute(
                        "UPDATE tasks SET status='failed', result=$1::jsonb WHERE id=$2",
                        _err_payload,
                        parent_task_id,
                    )
                except Exception:
                    pass
                # Broadcast failure to the UI
                try:
                    from sdk.agent.telemetry import LiveStatusReporter
                    await LiveStatusReporter.broadcast_status(
                        task_id=parent_task_id,
                        phase="goal_engine",
                        status="failed",
                        message=f"Task engine crashed: {_bg_exc}",
                        details={"error": str(_bg_exc), "traceback": traceback.format_exc()},
                    )
                except Exception:
                    pass

        asyncio.create_task(run_goal_engine())
        
        return _ok(req_id, {
            "success": True,
            "task_id": parent_task_id,
            "subtasks_count": len(task_items)
        })
    except Exception as exc:
        logger.exception("task.submit error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.list`` — Fetch all queued/executing tasks.
    """
    try:
        records = await db.fetch("SELECT id, parent_id, description, status, payload, result, assigned_agent_id, model_override, worktree_path, branch_name FROM tasks ORDER BY created_at DESC LIMIT 50")
        task_list = []
        for r in records:
            task_list.append({
                "id": r["id"],
                "parent_id": r["parent_id"],
                "description": r["description"],
                "status": r["status"],
                "assigned_agent_id": r["assigned_agent_id"],
                "model_override": json.loads(r["model_override"]) if isinstance(r["model_override"], str) else r["model_override"],
                "worktree_path": r["worktree_path"],
                "branch_name": r["branch_name"],
                "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
                "result": json.loads(r["result"]) if isinstance(r["result"], str) else r["result"]
            })
        return _ok(req_id, {"tasks": task_list})
    except Exception as exc:
        logger.exception("task.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_task_assign_worker(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    agent_id = params.get("agent_id")
    if not task_id or not agent_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id and agent_id required")
    try:
        await db.execute("UPDATE tasks SET assigned_agent_id = $1 WHERE id = $2", agent_id, task_id)
        return _ok(req_id, {"success": True, "task_id": task_id, "assigned_agent_id": agent_id})
    except Exception as exc:
        logger.exception("task.assign_worker error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_task_set_model_override(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    model_override = params.get("model_override")
    if not task_id or model_override is None:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id and model_override required")
    try:
        await db.execute("UPDATE tasks SET model_override = $1::jsonb WHERE id = $2", json.dumps(model_override), task_id)
        return _ok(req_id, {"success": True, "task_id": task_id, "model_override": model_override})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_task_create_worktree(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    
    from python.harness.config import settings
    base_dir = Path(settings.LOCAL_WORKSPACE_DIR) if getattr(settings, 'LOCAL_WORKSPACE_DIR', None) else Path(_ROOT_DIR)
    
    scratch_dir = base_dir / "scratch" / "worktrees"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    
    worktree_path = scratch_dir / task_id
    branch_name = f"tala-task/{task_id}"
    
    import asyncio
    try:
        git_check = await asyncio.create_subprocess_shell(
            "git rev-parse --is-inside-work-tree",
            cwd=str(base_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        # Fix: add 3s timeout — git rev-parse can hang indefinitely on Windows with a locked repo
        try:
            stdout, _ = await asyncio.wait_for(git_check.communicate(), timeout=3.0)
        except asyncio.TimeoutError:
            git_check.kill()
            logger.warning("git rev-parse timed out after 3s; treating as non-git workspace.")
            stdout = b""
        is_git = (git_check.returncode == 0 and stdout.decode().strip() == "true")
        
        if is_git:
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", str(worktree_path.absolute()), "-b", branch_name,
                cwd=str(base_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                logger.warning("git worktree command timed out after 3s; falling back to direct workspace mode.")
                return _err(req_id, JsonRpcError.INTERNAL_ERROR, "git worktree timed out")
            if proc.returncode != 0:
                logger.warning(f"git worktree failed: {stderr.decode()}")
                return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"git worktree failed: {stderr.decode()}")
        else:
            import shutil
            # Fix: run copytree off the event loop thread so it doesn't block asyncio.
            # Also skip node_modules and .venv to avoid copying gigabytes of dependencies.
            await asyncio.wait_for(
                asyncio.to_thread(
                    shutil.copytree,
                    str(base_dir),
                    str(worktree_path),
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        '.git', 'scratch', '__pycache__', 'node_modules', '.venv', '*.pyc'
                    ),
                ),
                timeout=30.0,
            )
            branch_name = "copied-scratch"
            
        await db.execute(
            "UPDATE tasks SET worktree_path = $1, branch_name = $2 WHERE id = $3",
            str(worktree_path.absolute()), branch_name, task_id
        )
        return _ok(req_id, {"success": True, "task_id": task_id, "worktree_path": str(worktree_path.absolute()), "branch_name": branch_name})
    except Exception as exc:
        logger.exception("task.create_worktree error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_control(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.control`` — Pause, resume, or abort task loops.
    """
    task_id = params.get("task_id", "").strip()
    action = params.get("action", "").strip() # pause, resume, abort, approve, reject
    if not task_id or not action:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' and 'action' are required")
        
    try:
        from sdk.agent.gates import RevocationHalt, trigger_emergency_halt
        if action in ("approve", "reject"):
            # Self-edit proposals sit in the task queue as status 'review' so
            # Mission Control shows them without a separate approval surface.
            from sdk.agent.self_edit_approval import decide
            outcome = await decide(task_id, approve=(action == "approve"))
            return _ok(req_id, outcome) if outcome.get("success") else _err(
                req_id, JsonRpcError.INVALID_PARAMS, outcome.get("detail", "decision failed"))
        if action == "abort":
            # Retroactive since SEC-W1-05: this also kills shell work already
            # in flight, and persists so a restart does not clear it.
            await trigger_emergency_halt(f"task {task_id} aborted from Mission Control")
            await db.execute("UPDATE tasks SET status = 'failed' WHERE id = $1 OR parent_id = $1", task_id)
        elif action == "pause":
            # 'paused', NOT 'blocked'. resume_suspended_goals selects
            # "status = 'blocked' AND parent_id IS NULL" at startup and
            # relaunches every match, because 'blocked' means "the system
            # suspended this for budget and it should carry on". Writing
            # 'blocked' here made a human pause indistinguishable from that, so
            # pausing a task queued it for automatic relaunch on the next app
            # start -- a task could not be made to stay stopped.
            await db.execute("UPDATE tasks SET status = 'paused' WHERE id = $1 OR parent_id = $1", task_id)
        elif action == "resume":
            # Model A: Resume requeues the task only. It must NOT release the
            # global emergency halt -- that is system.release_halt (owner-gated).
            # Abort and Halt On/Off are separate controls.
            await db.execute("UPDATE tasks SET status = 'pending' WHERE id = $1 OR parent_id = $1", task_id)

        return _ok(req_id, {
            "success": True,
            "action": action,
            "task_id": task_id,
            # So the caller can see the halt state, rather than inferring it
            # from the absence of an error.
            "halted": RevocationHalt.is_halted(),
        })
    except Exception as exc:
        logger.exception("task.control error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_clear(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.clear`` — Wipe all tasks and their associated telemetry.

    UI-audit C5: confirm-only UX was insufficient. Require an explicit
    ``confirm="CLEAR_QUEUE"`` token, refuse while the emergency halt is
    engaged (dispatch also gates this), and require a valid harness_secret.
    """
    confirm = str(params.get("confirm") or "").strip()
    if confirm != "CLEAR_QUEUE":
        return _err(
            req_id,
            JsonRpcError.INVALID_PARAMS,
            "task.clear requires confirm='CLEAR_QUEUE' (not UI confirm alone).",
        )

    try:
        from python.harness.halt_gate import is_halted_fail_closed
        if is_halted_fail_closed():
            return _err(
                req_id,
                JsonRpcError.INVALID_REQUEST,
                "Emergency halt is engaged; task.clear refused. "
                "Release it with system.release_halt first.",
                {"halted": True},
            )
    except Exception as exc:
        return _err(
            req_id,
            JsonRpcError.INVALID_REQUEST,
            f"Halt state unreadable; task.clear refused ({type(exc).__name__}).",
            {"halted": True},
        )

    # Owner gate required (UI-audit C5): confirm alone is insufficient for
    # truncate. Electron injects harness_secret the same way as trigger_update.
    from python.harness.admin_auth import require_harness_admin_secret
    if not require_harness_admin_secret(params):
        return _err(
            req_id,
            JsonRpcError.INVALID_REQUEST,
            "Unauthorized: task.clear requires a valid harness admin secret.",
        )

    try:
        await db.execute("TRUNCATE TABLE tasks CASCADE;")
        return _ok(req_id, {"success": True, "message": "All tasks and telemetry have been cleared."})
    except Exception as exc:
        logger.exception("task.clear error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


#: Statuses where the goal loop is actively holding the row. Editing or deleting
#: one of these races the loop, which writes its own status back afterwards --
#: the edit appears to work, then silently reverts, or the row is recreated.
#: Callers must abort the task first, or pass force to accept the race.
_TASK_LIVE_STATUSES = ("in_progress", "running")


async def handle_task_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.delete`` — Remove one task and its children.

    task.clear TRUNCATEs the whole table, which is the only removal that
    existed; there was no way to drop a single stuck row without wiping every
    task and all of its telemetry.
    """
    task_id: str = params.get("task_id", "").strip()
    force: bool = bool(params.get("force", False))
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' is required")

    try:
        row = await db.fetchrow("SELECT id, status FROM tasks WHERE id = $1", task_id)
        if row is None:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"No task with id {task_id!r}")

        if row["status"] in _TASK_LIVE_STATUSES and not force:
            return _err(
                req_id,
                JsonRpcError.INVALID_PARAMS,
                f"Task is {row['status']}; the goal loop is still writing to it. "
                "Abort it first (task.control action='abort'), or pass force=true.",
            )

        # Children go explicitly rather than via ON DELETE SET NULL, which would
        # orphan them into the queue as parentless rows that nothing ever runs.
        deleted = await db.execute("DELETE FROM tasks WHERE id = $1 OR parent_id = $1", task_id)
        return _ok(req_id, {"success": True, "task_id": task_id, "result": deleted})
    except Exception as exc:
        logger.exception("task.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_update(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.update`` — Edit a task's description or status.

    Deliberately narrow: description and status only. Dependencies, payload and
    parentage describe a plan the goal engine is executing, and rewriting those
    from the UI produces a graph the engine never planned.
    """
    task_id: str = params.get("task_id", "").strip()
    description = params.get("description")
    status = params.get("status")
    force: bool = bool(params.get("force", False))

    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' is required")
    if description is None and status is None:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Nothing to update: pass 'description' or 'status'")

    valid_statuses = {"pending", "in_progress", "complete", "failed", "blocked", "paused"}
    if status is not None and status not in valid_statuses:
        return _err(
            req_id, JsonRpcError.INVALID_PARAMS,
            f"'status' must be one of {sorted(valid_statuses)}",
        )

    try:
        row = await db.fetchrow("SELECT id, status FROM tasks WHERE id = $1", task_id)
        if row is None:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"No task with id {task_id!r}")

        if row["status"] in _TASK_LIVE_STATUSES and not force:
            return _err(
                req_id,
                JsonRpcError.INVALID_PARAMS,
                f"Task is {row['status']}; the goal loop writes its own status back, so an "
                "edit made now would silently revert. Pause or abort it first, or pass force=true.",
            )

        sets, args = [], []
        if description is not None:
            desc = str(description).strip()
            if not desc:
                return _err(req_id, JsonRpcError.INVALID_PARAMS, "'description' cannot be empty")
            args.append(desc)
            sets.append(f"description = ${len(args)}")
        if status is not None:
            args.append(status)
            sets.append(f"status = ${len(args)}")
        args.append(task_id)

        await db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ${len(args)}", *args)
        updated = await db.fetchrow(
            "SELECT id, description, status FROM tasks WHERE id = $1", task_id
        )
        return _ok(req_id, {"success": True, "task": dict(updated) if updated else None})
    except Exception as exc:
        logger.exception("task.update error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_get_telemetry(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.get_telemetry`` — Fetch event telemetry streams.
    """
    task_id = params.get("task_id", "").strip()
    include_children = params.get("include_children", False)
    try:
        query = "SELECT id, task_id, event_type, payload, created_at FROM task_event_store"
        args = []
        if task_id:
            if include_children:
                query += " WHERE task_id = $1 OR task_id IN (SELECT id FROM tasks WHERE parent_id = $1)"
            else:
                query += " WHERE task_id = $1"
            args.append(task_id)
        query += " ORDER BY created_at DESC LIMIT 100"
        
        records = await db.fetch(query, *args)
        events = []
        for r in records:
            events.append({
                "event_id": str(r["id"]),
                "task_id": r["task_id"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None
            })
        return _ok(req_id, {"events": events})
    except Exception as exc:
        logger.exception("task.get_telemetry error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_task_status(params: dict[str, Any], ws: ServerConnection, req_id: Any) -> str:
    from sdk.agent.status_api import AgentStatusAPI
    task_id = params.get("task_id")
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Missing task_id")
    res = await AgentStatusAPI.get_task_status(task_id)
    return _ok(req_id, res)

async def handle_task_phase_events(params: dict[str, Any], ws: ServerConnection, req_id: Any) -> str:
    from sdk.agent.status_api import AgentStatusAPI
    task_id = params.get("task_id")
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Missing task_id")
    res = await AgentStatusAPI.get_task_status(task_id)
    return _ok(req_id, res.get("phases", []))

async def handle_task_artifacts(params: dict[str, Any], ws: ServerConnection, req_id: Any) -> str:
    from sdk.agent.status_api import AgentStatusAPI
    task_id = params.get("task_id")
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Missing task_id")
    res = await AgentStatusAPI.get_task_status(task_id)
    return _ok(req_id, res.get("artifacts", []))


# ---------------------------------------------------------------------------
# Memory & Research wrappers

#: Statuses a task can be in when it is finished with. Only these are ever
#: pruned -- a pending or in-progress task is work, not history, however old
#: it looks.
TERMINAL_TASK_STATUSES = ("complete", "completed", "failed", "aborted", "cancelled")

#: Default retention. Deliberately generous: the complaint was "it stays
#: forever", not "I need this gone tonight", and a retention sweep that is too
#: eager destroys the record of what happened.
DEFAULT_TASK_RETENTION_DAYS = 14


async def prune_finished_tasks(
    older_than_days: int = DEFAULT_TASK_RETENTION_DAYS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Age finished tasks out of the board.

    There was no retention policy anywhere (UI-034) -- Steven's "they dont fade
    out, it stays forever". The only two deletion paths were a user clicking
    Delete on one row and a user clicking Clear Queue, which truncates
    everything including work still running.

    Only terminal statuses are eligible, and a task that never finished is
    never pruned regardless of age: an abandoned in-progress row is a bug worth
    seeing, and quietly deleting it hides the bug rather than the row.
    """
    try:
        days = max(1, int(older_than_days))
    except (TypeError, ValueError):
        days = DEFAULT_TASK_RETENTION_DAYS

    try:
        doomed = await db.fetch(
            f"SELECT id, status, created_at FROM tasks "
            f"WHERE status = ANY($1) "
            f"AND COALESCE(completed_at, created_at) < NOW() - INTERVAL '{days} days'",
            list(TERMINAL_TASK_STATUSES),
        )
    except Exception as exc:
        logger.warning("prune_finished_tasks: could not read tasks: %s", exc)
        return {"success": False, "error": str(exc), "pruned": 0}

    if not doomed:
        return {"success": True, "pruned": 0, "dry_run": dry_run,
                "retention_days": days, "ids": []}

    ids = [r["id"] for r in doomed]
    if not dry_run:
        await db.execute(
            "DELETE FROM tasks WHERE id = ANY($1)", ids,
            caller="task retention sweep",
        )
        # Said out loud rather than swept quietly: a retention policy that
        # deletes without a record is indistinguishable from data loss.
        await db.log_to_db(
            "INFO",
            f"Task retention: removed {len(ids)} finished task(s) older than {days} days.",
            {"count": len(ids), "retention_days": days, "ids": ids[:50]},
        )
        logger.info("Task retention: pruned %d finished task(s) older than %d days.",
                    len(ids), days)

    return {"success": True, "pruned": len(ids), "dry_run": dry_run,
            "retention_days": days, "ids": ids[:50]}


async def handle_task_prune(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.prune`` — remove finished tasks older than a retention window.

    Separate from ``task.clear``, which truncates the whole table including
    work in progress. This only ever removes tasks that finished.
    """
    try:
        result = await prune_finished_tasks(
            older_than_days=params.get("older_than_days", DEFAULT_TASK_RETENTION_DAYS),
            dry_run=bool(params.get("dry_run", False)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("task.prune error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
