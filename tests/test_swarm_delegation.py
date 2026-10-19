"""Fake-model tests: create-bot, assign-goal, result returns, small swarm."""
from __future__ import annotations

import pytest

from sdk.agent.delegation import (
    check_delegated_task_status,
    run_small_swarm,
    run_topology_swarm,
    spawn_and_assign,
)
from sdk.agent.long_task import TaskManager, TaskState, create_task


async def fake_create_agent(name, system_prompt, role=""):
    return {"success": True, "agent_id": f"agent-{name}", "name": name, "role": role}


async def fake_assign_goal(goal):
    return {
        "status": "success",
        "task_id": f"goal-{abs(hash(goal)) % 10000}",
        "phases_completed": ["phase-1"],
        "report": f"completed:{goal}",
    }


async def fake_assign_fail(goal):
    return {"status": "failed", "error": "GoalEngine is offline"}


@pytest.mark.asyncio
async def test_create_bot_assign_goal_result_returns():
    parent = await create_task("Parent: ship the app", steps=[])
    result = await spawn_and_assign(
        parent.task_id,
        "Build the backend API",
        persona_name="backend-bot",
        system_prompt="You own the API.",
        create_agent=fake_create_agent,
        assign_goal=fake_assign_goal,
    )
    assert result["success"]
    assert result["agent_id"] == "agent-backend-bot"
    assert result["report"] == "completed:Build the backend API"
    status = await check_delegated_task_status(result["sub_task_id"])
    assert status["state"] == TaskState.COMPLETE.value
    assert status["agent_id"] == "agent-backend-bot"
    parent = await TaskManager().load(parent.task_id)
    assert parent.context["children"][0]["agent_id"] == "agent-backend-bot"
    await TaskManager().delete(parent.task_id)
    await TaskManager().delete(result["sub_task_id"])


@pytest.mark.asyncio
async def test_assign_failure_marks_child_failed():
    parent = await create_task("parent", steps=[])
    result = await spawn_and_assign(
        parent.task_id,
        "will fail",
        persona_name="doomed",
        system_prompt="x",
        create_agent=fake_create_agent,
        assign_goal=fake_assign_fail,
    )
    assert not result["success"]
    assert "offline" in result["error"]
    status = await check_delegated_task_status(result["sub_task_id"])
    assert status["state"] == TaskState.FAILED.value
    await TaskManager().delete(parent.task_id)
    await TaskManager().delete(result["sub_task_id"])


@pytest.mark.asyncio
async def test_small_swarm_end_to_end():
    result = await run_small_swarm(
        "Deploy full stack",
        [
            {"name": "api-bot", "goal": "deploy API", "system_prompt": "API owner"},
            {"name": "web-bot", "goal": "deploy web", "system_prompt": "Web owner"},
        ],
        create_agent=fake_create_agent,
        assign_goal=fake_assign_goal,
    )
    assert result["success"]
    assert len(result["children"]) == 2
    assert result["reports"] == ["completed:deploy API", "completed:deploy web"]
    parent = await TaskManager().load(result["parent_task_id"])
    assert parent.state == TaskState.COMPLETE
    assert parent.context["children"][0]["state"] == "complete"
    for child in result["children"]:
        await TaskManager().delete(child["sub_task_id"])
    await TaskManager().delete(result["parent_task_id"])


@pytest.mark.asyncio
async def test_topology_swarm_uses_start_swarm():
    async def fake_start(goal, topology_id):
        return {"success": True, "parent_task_id": "swarm-parent-1", "topology_id": topology_id}

    result = await run_topology_swarm("launch", "demo-topo", start_swarm=fake_start)
    assert result["success"]
    assert result["engine_parent_task_id"] == "swarm-parent-1"
    await TaskManager().delete(result["durable_task_id"])
