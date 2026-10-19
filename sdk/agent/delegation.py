"""
Sub-task delegation: spawn a real persona, assign a goal, fold results back.

Uses create_agent (persona record) plus submit_agent_goal (GoalEngine). A small
swarm is N of those children under one parent DurableTask. Callers may inject
the spawn/assign functions so tests can run with a fake model.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Sequence

from sdk.agent.long_task import TaskManager, TaskState, create_task

log = logging.getLogger(__name__)

CreateAgent = Callable[..., Awaitable[dict[str, Any]]]
AssignGoal = Callable[..., Awaitable[dict[str, Any]]]
StartSwarm = Callable[..., Awaitable[dict[str, Any]]]


def _ok(result: dict[str, Any]) -> bool:
    if result.get("success") is True:
        return True
    if result.get("status") in ("success", "ok"):
        return True
    return False


async def _default_create_agent(name: str, system_prompt: str, role: str = "") -> dict[str, Any]:
    from sdk.agent.agent_authoring import create_agent
    return await create_agent(name=name, system_prompt=system_prompt, role=role)


async def _default_assign_goal(goal: str) -> dict[str, Any]:
    from sdk.agent.tools import submit_agent_goal
    return await submit_agent_goal(goal)


async def _default_start_swarm(goal: str, topology_id: str) -> dict[str, Any]:
    from sdk.agent.agent_authoring import start_swarm
    return await start_swarm(goal, topology_id)


async def spawn_and_assign(
    parent_task_id: str,
    sub_goal: str,
    *,
    persona_name: str,
    system_prompt: str,
    role: str = "sub-agent",
    create_agent: CreateAgent | None = None,
    assign_goal: AssignGoal | None = None,
) -> dict[str, Any]:
    """Create a bot, assign a goal, store the result on a child DurableTask."""
    create = create_agent or _default_create_agent
    assign = assign_goal or _default_assign_goal
    child = await create_task(sub_goal, parent_id=parent_task_id)
    bot = await create(persona_name, system_prompt, role)
    if not _ok(bot):
        child.state = TaskState.FAILED
        child.context["delegation_error"] = bot.get("error") or "create_agent failed"
        await TaskManager().save(child)
        return {"success": False, "error": child.context["delegation_error"], "sub_task_id": child.task_id}

    agent_id = bot.get("agent_id") or bot.get("id")
    child.agent_id = agent_id
    child.context["spawned_agent"] = True
    child.context["persona_name"] = persona_name
    child.state = TaskState.DELEGATED
    await TaskManager().save(child)

    assigned = await assign(sub_goal)
    child.context["goal_result"] = assigned
    child.context["goal_task_id"] = assigned.get("task_id") or assigned.get("execution_id")
    if _ok(assigned):
        child.state = TaskState.COMPLETE
        child.context["report"] = assigned.get("report") or assigned.get("message") or ""
    else:
        child.state = TaskState.FAILED
        child.context["delegation_error"] = assigned.get("error") or "submit_agent_goal failed"
    await TaskManager().save(child)

    parent = await TaskManager().load(parent_task_id)
    if parent:
        children = list(parent.context.get("children") or [])
        children.append({
            "sub_task_id": child.task_id,
            "agent_id": agent_id,
            "goal": sub_goal,
            "state": child.state.value,
            "report": child.context.get("report"),
            "error": child.context.get("delegation_error"),
        })
        parent.context["children"] = children
        await TaskManager().save(parent)

    return {
        "success": child.state == TaskState.COMPLETE,
        "sub_task_id": child.task_id,
        "agent_id": agent_id,
        "parent_task_id": parent_task_id,
        "goal_task_id": child.context.get("goal_task_id"),
        "report": child.context.get("report"),
        "error": child.context.get("delegation_error"),
        "state": child.state.value,
    }


async def run_small_swarm(
    parent_goal: str,
    members: Sequence[dict[str, str]],
    *,
    create_agent: CreateAgent | None = None,
    assign_goal: AssignGoal | None = None,
) -> dict[str, Any]:
    """Spawn each member as a bot, assign its goal, collect reports on the parent."""
    parent = await create_task(parent_goal, steps=[])
    results = []
    for i, member in enumerate(members):
        name = member.get("name") or f"swarm-bot-{i+1}"
        goal = member.get("goal") or parent_goal
        prompt = member.get("system_prompt") or f"You are {name}. Complete: {goal}"
        results.append(await spawn_and_assign(
            parent.task_id,
            goal,
            persona_name=name,
            system_prompt=prompt,
            role=member.get("role") or "swarm-worker",
            create_agent=create_agent,
            assign_goal=assign_goal,
        ))
    parent = await TaskManager().load(parent.task_id)
    assert parent is not None
    failed = [r for r in results if not r.get("success")]
    parent.state = TaskState.FAILED if failed else TaskState.COMPLETE
    parent.context["swarm_results"] = results
    await TaskManager().save(parent)
    return {
        "success": not failed,
        "parent_task_id": parent.task_id,
        "children": results,
        "reports": [r.get("report") for r in results],
    }


async def run_topology_swarm(
    goal: str,
    topology_id: str,
    *,
    start_swarm: StartSwarm | None = None,
) -> dict[str, Any]:
    """Dispatch an authored topology through SwarmEngine.start_swarm."""
    starter = start_swarm or _default_start_swarm
    parent = await create_task(goal, steps=[])
    result = await starter(goal, topology_id)
    parent.context["swarm_dispatch"] = result
    parent.state = TaskState.DELEGATED if _ok(result) else TaskState.FAILED
    if _ok(result):
        parent.context["engine_parent_task_id"] = result.get("parent_task_id")
    else:
        parent.context["delegation_error"] = result.get("error")
    await TaskManager().save(parent)
    return {
        "success": _ok(result),
        "durable_task_id": parent.task_id,
        "engine_parent_task_id": result.get("parent_task_id"),
        "error": result.get("error"),
        "result": result,
    }


async def check_delegated_task_status(sub_task_id: str) -> dict[str, Any]:
    task = await TaskManager().load(sub_task_id)
    if not task:
        return {"success": False, "error": f"Task {sub_task_id} not found"}
    return {
        "success": True,
        "sub_task_id": sub_task_id,
        "state": task.state.value,
        "goal": task.goal,
        "agent_id": task.agent_id,
        "report": task.context.get("report"),
        "goal_task_id": task.context.get("goal_task_id"),
        "context": task.context,
    }
