"""
sdk/agent/agent_authoring.py
============================
Tools that let Tala compose multi-agent work rather than only execute it.

WHY THIS EXISTS
---------------
The execution half was already built and correctly wired: a ``TopologyNode``
carries an ``agent_id``, ``SwarmEngine._dispatch_node`` writes it onto the task,
and ``DAGExecutionEngine._resolve_persona_prompt`` loads that agent's record and
runs the node under its system prompt. Swarms are genuinely heterogeneous, and
the log even warns when a node names a persona that no longer exists.

What was missing was the authoring half, and only for her. ``agent.save_agent``,
``swarm.topology.create`` and ``swarm.start`` are RPC methods Mission Control
calls; none appeared in the tool registry, so composing a swarm and defining a
persona were things the operator did and Tala could not. That is the same shape
as the cron scheduler that ran from boot for months with no UI calling it -- a
working mechanism with no entry point for the party who should drive it. Here
that party is the agent, which matters more, because a self-improvement loop the
agent cannot reach is not closed.

WHAT A PERSONA IS, AND IS NOT
-----------------------------
An agent record holds ``name``, ``role``, and a config of ``system_prompt`` and
``tag``. It grants no model override, no tool access and no execution authority.
A persona Tala writes changes a voice, not a permission -- the node still runs
through the same engine, with the same tool selection and the same guardrails.
That is what makes direct creation defensible here rather than propose-and-
confirm.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from sdk.agent.tools import register_tool


@register_tool(
    name="list_agents",
    description="List the agent personas available to run swarm nodes as, with their ids and roles.",
)
async def list_agents() -> dict[str, Any]:
    """
    NAME
           list_agents - list available agent personas

    SYNOPSIS
           list_agents() -> dict[str, Any]

    DESCRIPTION
           Every persona a swarm node can be assigned to through ``agent_id``.
           Check this before creating one: reusing an existing persona is nearly
           always better than adding a near-duplicate.
    """
    from python.harness import db

    try:
        rows = await db.fetch("SELECT id, name, type, role FROM agents ORDER BY name")
    except Exception as exc:
        return {"success": False, "error": f"Could not read agents: {exc}"}

    return {
        "success": True,
        "count": len(rows),
        "agents": [
            {"id": r["id"], "name": r["name"], "type": r["type"], "role": r["role"] or ""}
            for r in rows
        ],
    }


@register_tool(
    name="create_agent",
    description=(
        "Define a new agent persona that swarm nodes can run as. Sets a name, role and system "
        "prompt only - it grants no tools, no model and no authority."
    ),
)
async def create_agent(
    name: str,
    system_prompt: str,
    role: str = "",
    tag: str = "",
) -> dict[str, Any]:
    """
    NAME
           create_agent - define a new agent persona

    SYNOPSIS
           create_agent(name, system_prompt, role, tag) -> dict[str, Any]

    DESCRIPTION
           Creates a persona a swarm node can be assigned to. It sets voice and
           framing; it does not widen what the node may do.

           An existing name is refused rather than overwritten. A persona may be
           mid-use by a running swarm, and silently replacing its system prompt
           would change that run's behaviour underneath it.
    """
    from python.harness import db

    name = (name or "").strip()
    system_prompt = (system_prompt or "").strip()
    if not name:
        return {"success": False, "error": "'name' is required"}
    if not system_prompt:
        return {
            "success": False,
            "error": "'system_prompt' is required; a persona with no prompt changes nothing",
        }

    try:
        existing = await db.fetchrow(
            "SELECT id FROM agents WHERE lower(name) = lower($1)", name
        )
        if existing:
            return {
                "success": False,
                "error": (
                    f"An agent named {name!r} already exists (id {existing['id']}). "
                    f"Use that id, or choose a different name."
                ),
                "existing_id": existing["id"],
            }

        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        config = {"system_prompt": system_prompt, "tag": tag.strip()}
        await db.execute(
            """
            INSERT INTO agents (id, name, type, role, crew_type, config, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW())
            """,
            agent_id, name, "standalone", role.strip(), "", json.dumps(config),
        )
    except Exception as exc:
        return {"success": False, "error": f"Could not create agent: {exc}"}

    return {"success": True, "agent_id": agent_id, "name": name, "role": role.strip()}


#: Ceiling on nodes in one authored topology. Every node becomes a real task and
#: a real model call, so a runaway graph is expensive rather than dangerous --
#: but it is still worth refusing rather than discovering in the task queue.
MAX_TOPOLOGY_NODES = 12


@register_tool(
    name="create_swarm_topology",
    description=(
        "Author a swarm topology: a DAG of named nodes with roles and gate conditions on the "
        "edges. Each node may name an agent_id to run as."
    ),
)
async def create_swarm_topology(
    topology_id: str,
    name: str,
    nodes: list,
    edges: list,
) -> dict[str, Any]:
    """
    NAME
           create_swarm_topology - author a swarm topology as an HTML-NN

    SYNOPSIS
           create_swarm_topology(topology_id, name, nodes, edges) -> dict[str, Any]

    DESCRIPTION
           nodes: [{"id": "scout", "role": "planner", "description": "...",
                    "agent_id": "agent-xxxx"}]   -- agent_id is optional
           edges: [{"from_node": "scout", "to_node": "survey", "condition": "success"}]

           Validated through the same ``topology_from_spec`` the loader uses, so
           a topology that would not load back is refused rather than written.

           Edge conditions are checked against the rules GatedVerifier actually
           implements. An unknown condition can never pass, and that branch would
           stall silently on every run.
    """
    from sdk.agent.gated_verifier import GatedVerifier
    from sdk.agent.topology import TopologySpecError, topology_from_spec
    from sdk.agent.topology_nodes import write_topology_node

    if not isinstance(nodes, list) or not nodes:
        return {"success": False, "error": "'nodes' must be a non-empty list"}
    if not isinstance(edges, list):
        return {"success": False, "error": "'edges' must be a list"}
    if len(nodes) > MAX_TOPOLOGY_NODES:
        return {
            "success": False,
            "error": f"{len(nodes)} nodes is too many for one topology (max {MAX_TOPOLOGY_NODES})",
        }

    known = sorted(GatedVerifier.KNOWN_RULES)
    for edge in edges:
        condition = (edge or {}).get("condition", "success")
        if condition not in known:
            return {
                "success": False,
                "error": (
                    f"Edge condition {condition!r} is not a rule the verifier implements, so "
                    f"that branch could never pass. Available: {known}"
                ),
            }

    spec = {
        "topology_id": topology_id,
        "name": name,
        "nodes": nodes,
        "edges": edges,
        "failure_policy": {},
    }
    try:
        topology = topology_from_spec(spec)
    except TopologySpecError as exc:
        return {"success": False, "error": f"Invalid topology: {exc}"}

    try:
        result = write_topology_node(topology, created_by="tala-agent")
    except Exception as exc:
        # write_topology_node refuses when the node id would collide with a
        # different topology; that message names both ids and is worth passing
        # through verbatim.
        return {"success": False, "error": f"Could not write the topology node: {exc}"}

    return {
        "success": True,
        "topology_id": topology.topology_id,
        "node_count": len(topology.nodes),
        "edge_count": len(topology.edges),
        "result": result,
    }


@register_tool(
    name="start_swarm",
    description="Run a goal through an existing swarm topology, dispatching its start nodes in parallel.",
)
async def start_swarm(goal: str, topology_id: str) -> dict[str, Any]:
    """
    NAME
           start_swarm - launch a swarm run

    SYNOPSIS
           start_swarm(goal: str, topology_id: str) -> dict[str, Any]

    DESCRIPTION
           Creates the parent task and dispatches every start node at once. The
           run finalises itself when the last node retires and the synthesised
           result lands on the parent task.

           Returns as soon as the run is dispatched -- it does not wait. Follow
           it with list_active_tasks.
    """
    from sdk.agent.swarm_engine import SwarmEngine
    from sdk.agent.topology import TopologyRegistry

    goal = (goal or "").strip()
    if not goal:
        return {"success": False, "error": "'goal' is required"}

    # A topology authored moments ago is on disk but not yet in the registry,
    # which is loaded at startup. Reload once before giving up, so
    # create_swarm_topology followed by start_swarm works in a single turn.
    if not TopologyRegistry.get(topology_id):
        try:
            TopologyRegistry.reset()
        except Exception as exc:
            return {"success": False, "error": f"Could not reload topologies: {exc}"}

    topology = TopologyRegistry.get(topology_id)
    if not topology:
        return {
            "success": False,
            "error": f"No topology {topology_id!r} is registered. Author it first with "
                     f"create_swarm_topology.",
        }

    try:
        parent_id = await SwarmEngine.spawn_topology(goal, topology_id)
    except Exception as exc:
        return {"success": False, "error": f"Could not start the swarm: {exc}"}

    return {
        "success": True,
        "parent_task_id": parent_id,
        "topology_id": topology_id,
        "dispatched_nodes": len(topology.nodes),
    }


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------
#
# MCPManager was complete and unusable: a server could be connected and its
# tools listed, but nothing ever called mcp.call_tool and no MCP tool reached
# the registry, so the model never saw one. These two make the subsystem
# reachable -- discover what is configured on the machine, and refresh the
# registry after adopting something.


@register_tool(
    name="discover_mcp_servers",
    description=(
        "Scan this machine's other MCP clients for servers already configured there. "
        "Read-only: reports what it finds and connects nothing."
    ),
)
async def discover_mcp_servers_tool() -> dict[str, Any]:
    """
    NAME
           discover_mcp_servers - find MCP servers configured elsewhere on this machine

    SYNOPSIS
           discover_mcp_servers() -> dict[str, Any]

    DESCRIPTION
           Reads the config files kept by Claude Desktop, Claude Code, Cursor,
           Windsurf and VS Code, plus project-local .mcp.json, and reports the
           servers defined in them. Each result says whether Tala already has
           that server registered.

           Connects to nothing. Adopting a server runs a subprocess with the
           invoking user's privileges, which is a decision for Weavor rather
           than something a scan should do on its own. Environment blocks are
           reported only as a has_env_secrets flag, never echoed.
    """
    from sdk.agent.mcp_bridge import discover_mcp_servers

    return discover_mcp_servers()


@register_tool(
    name="refresh_mcp_tools",
    description="Re-register the tools of every connected MCP server so they can be called.",
)
async def refresh_mcp_tools_tool() -> dict[str, Any]:
    """
    NAME
           refresh_mcp_tools - re-register connected MCP servers' tools

    SYNOPSIS
           refresh_mcp_tools() -> dict[str, Any]

    DESCRIPTION
           Registers every connected server's tools under {server}__{tool}, and
           drops the tools of any server that has gone away. Runs at startup and
           whenever the server set changes, so this is mostly for recovering
           after a server that was down comes back.
    """
    from sdk.agent.mcp_bridge import register_mcp_tools

    return await register_mcp_tools()


@register_tool(
    name="control_swarm",
    description=(
        "Pause, resume or cancel a running swarm by its parent task id. Cascades to every "
        "node the swarm dispatched."
    ),
)
async def control_swarm(parent_task_id: str, action: str) -> dict[str, Any]:
    """
    NAME
           control_swarm - stop or hold a running swarm

    SYNOPSIS
           control_swarm(parent_task_id: str, action: str) -> dict[str, Any]

    DESCRIPTION
           action is one of pause, resume, abort.

           start_swarm could launch work that nothing could then stop: the
           swarm.pause / resume / cancel methods existed and had no caller
           anywhere. Being able to start something without being able to stop it
           is worse for the agent than for a person, because she cannot see the
           task queue.

           Cascades. task.control acts on the parent and every child, so a swarm
           parent takes all of its dispatched nodes with it.
    """
    from python.harness.server import handle_task_control

    valid = {"pause", "resume", "abort"}
    action = (action or "").strip().lower()
    if action not in valid:
        return {"success": False, "error": f"'action' must be one of {sorted(valid)}"}
    if not (parent_task_id or "").strip():
        return {"success": False, "error": "'parent_task_id' is required"}

    import json as _json

    raw = await handle_task_control(
        {"task_id": parent_task_id.strip(), "action": action}, req_id=None
    )
    try:
        payload = _json.loads(raw)
    except Exception:
        return {"success": False, "error": "Unreadable response from task.control"}

    if "error" in payload:
        return {"success": False, "error": payload["error"].get("message", "unknown")}
    return {"success": True, "action": action, "parent_task_id": parent_task_id, **(payload.get("result") or {})}
