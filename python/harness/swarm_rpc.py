"""
python/harness/swarm_rpc.py — the swarm orchestrator's RPC surface.

Sixteen swarm.* handlers, moved out of server.py under the size ratchet
(tests/test_size_ratchet.py) — slice 2 of the cut-down campaign. Topology
authoring lives in sdk.agent.agent_authoring, execution in the orchestrator;
this module is the wire format around them. Heavier harness/sdk imports stay
inside the functions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from python.harness import db
from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)


async def handle_swarm_workers_active(params: dict[str, Any], req_id: Any) -> str:
    try:
        records = await db.fetch("SELECT id, name, role, config FROM agents WHERE is_active = TRUE")
        workers = [{"id": r["id"], "name": r["name"], "role": r["role"], "config": json.loads(r["config"]) if isinstance(r["config"], str) else r["config"]} for r in records]
        return _ok(req_id, {"workers": workers})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_topology_list(params: dict[str, Any], req_id: Any) -> str:
    try:
        from sdk.agent.topology import TopologyRegistry

        if params.get("reload"):
            # reset(), not reload(): reload only overwrites, so a topology
            # whose node was deleted or renamed on disk would linger.
            TopologyRegistry.reset()
        from sdk.agent.gated_verifier import GatedVerifier

        return _ok(req_id, {
            "topologies": [t.to_wire() for t in TopologyRegistry.list_all()],
            # Sent so the editor's gate dropdown is generated from the rules
            # the verifier actually implements. A hardcoded UI list would let
            # someone author an edge on a rule that can never pass, which
            # stalls that branch silently on every run.
            "known_gate_conditions": sorted(GatedVerifier.KNOWN_RULES),
        })
    except Exception as exc:
        # Logged like its siblings (topology.create, topology.delete). With
        # reload=true this calls TopologyRegistry.reset(), which does node-file
        # I/O, HTML parsing and spec validation -- any of which can raise. The
        # client used to get a bare str(exc) with no file name and no traceback
        # anywhere in the log.
        logger.exception("swarm.topology.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_topology_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``swarm.topology.delete`` — remove a hand-authored swarm's HTML-NN.

    A topology that also has a built-in fallback reappears in its original
    shape afterwards; the response says so explicitly rather than reporting a
    clean delete the user can see did not happen.
    """
    topology_id = params.get("topology_id")
    if not topology_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "topology_id is required")

    try:
        from sdk.agent.topology_nodes import delete_topology_node

        result = delete_topology_node(str(topology_id))
    except Exception as exc:
        logger.exception("swarm.topology.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

    if not result["node_deleted"]:
        return _err(
            req_id, JsonRpcError.INVALID_PARAMS,
            f"No authored definition to delete for topology {topology_id!r} "
            "(it is a built-in, which has no node file behind it)",
        )
    return _ok(req_id, result)

async def handle_swarm_topology_create(params: dict[str, Any], req_id: Any) -> str:
    """
    Create (or update) a swarm topology by writing its HTML-NN definition.

    The node file *is* the swarm: this is the only creation path, so a
    topology that ran once is on disk to be listed, inspected, edited and run
    again rather than existing only in memory for one process lifetime.
    Validation happens before anything is written, and the file is re-parsed
    after writing, so a topology can never be "saved" in a state the loader
    would refuse to read back.
    """
    from sdk.agent.topology import TopologySpecError, topology_from_spec
    from sdk.agent.topology_nodes import write_topology_node

    spec = params.get("topology") or params
    try:
        topology = topology_from_spec(spec)
    except TopologySpecError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))

    try:
        result = write_topology_node(
            topology,
            semantic_memory=params.get("semantic_memory"),
            created_by=str(params.get("created_by") or "tala-system"),
            overwrite=bool(params.get("overwrite", True)),
        )
    except FileExistsError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("swarm.topology.create error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

    return _ok(req_id, {**result, "topology": topology.to_wire()})

async def handle_swarm_start(params: dict[str, Any], req_id: Any) -> str:
    topology_id = params.get("topology_id")
    goal = params.get("goal")
    if not topology_id or not goal:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "topology_id and goal are required")
        
    try:
        from sdk.agent.swarm_engine import SwarmEngine
        parent_id = await SwarmEngine.spawn_topology(goal, topology_id)
        return _ok(req_id, {"success": True, "task_id": parent_id})
    except Exception as exc:
        logger.exception("swarm.start error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

def _task_control():
    # Lazy: task.control still lives in server.py (it belongs to the task.*
    # slice of the cut-down); at call time server is fully imported, so this
    # cannot cycle. Swap to `from python.harness.task_rpc import ...` when
    # that slice lands.
    from python.harness.server import handle_task_control
    return handle_task_control

async def handle_swarm_pause(params: dict[str, Any], req_id: Any) -> str:
    params["action"] = "pause"
    return await _task_control()(params, req_id)

async def handle_swarm_resume(params: dict[str, Any], req_id: Any) -> str:
    params["action"] = "resume"
    return await _task_control()(params, req_id)

async def handle_swarm_cancel(params: dict[str, Any], req_id: Any) -> str:
    params["action"] = "abort"
    return await _task_control()(params, req_id)

async def handle_swarm_status(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    row = await db.fetchrow("SELECT status, payload, result FROM tasks WHERE id = $1", task_id)
    if not row: return _err(req_id, JsonRpcError.INVALID_PARAMS, "Task not found")
    
    children = await db.fetch("SELECT id, status, payload FROM tasks WHERE parent_id = $1", task_id)
    nodes = []
    for c in children:
        p = c["payload"]
        if isinstance(p, str): p = json.loads(p)
        nodes.append({"id": c["id"], "status": c["status"], "node_id": p.get("node_id")})
        
    p_dict = row["payload"]
    if isinstance(p_dict, str): p_dict = json.loads(p_dict)
    
    return _ok(req_id, {
        "task_id": task_id,
        "status": row["status"],
        "topology_id": p_dict.get("topology_id"),
        "nodes": nodes
    })

async def handle_swarm_runs_list(params: dict[str, Any], req_id: Any) -> str:
    rows = await db.fetch("SELECT id, status, description, created_at, completed_at FROM tasks WHERE payload::text LIKE '%topology_id%' AND parent_id IS NULL ORDER BY created_at DESC LIMIT 50")
    runs = []
    for r in rows:
        runs.append({
            "task_id": r["id"],
            "status": r["status"],
            "description": r["description"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None
        })
    return _ok(req_id, {"runs": runs})

async def handle_swarm_runs_get(params: dict[str, Any], req_id: Any) -> str:
    return await handle_swarm_status(params, req_id)

async def handle_swarm_artifact_inspect(params: dict[str, Any], req_id: Any) -> str:
    artifact_id = params.get("artifact_id")
    if not artifact_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "artifact_id required")
    row = await db.fetchrow("SELECT result FROM tasks WHERE id = $1", artifact_id)
    if not row: return _err(req_id, JsonRpcError.INVALID_PARAMS, "Artifact not found")
    return _ok(req_id, {"artifact": row["result"]})

async def handle_swarm_fleet_status(params: dict[str, Any], req_id: Any) -> str:
    try:
        from sdk.agent.orchestrator_cli import get_active_tasks, get_port_allocations

        tasks = await get_active_tasks()
        ports = await get_port_allocations()

        result_tasks = [
            {
                "id": t["id"],
                "description": t["description"],
                "status": t["status"],
                "worktree_path": t["worktree_path"],
                "branch_name": t["branch_name"],
                "port": ports.get(t["id"]),
            }
            for t in tasks
        ]

        return _ok(req_id, {"success": True, "tasks": result_tasks})
    except Exception as exc:
        logger.exception("swarm.fleet_status error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_fleet_pause(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    try:
        from sdk.agent.orchestrator_cli import pause_task
        await pause_task(task_id)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_fleet_resume(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    try:
        from sdk.agent.orchestrator_cli import resume_task
        await resume_task(task_id)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_fleet_terminate(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    try:
        from sdk.agent.orchestrator_cli import terminate_task
        await terminate_task(task_id)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
