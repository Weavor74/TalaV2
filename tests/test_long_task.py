"""Durable task store and per-reply step limits."""
from __future__ import annotations

import pytest

from sdk.agent.long_task import (
    DurableTask,
    StepLimit,
    TaskManager,
    TaskState,
    TaskStep,
    create_task,
    resume_task,
)


async def _ok(_step):
    return {"success": True}


@pytest.mark.asyncio
async def test_task_creation():
    task = await create_task(
        goal="Test multi-step workflow",
        steps=[
            {"description": "Step 1", "action": "get_system_info", "args": {}},
            {"description": "Step 2", "action": "search_memory", "args": {"query": "test"}},
        ],
    )
    assert task.task_id.startswith("task-")
    assert len(task.steps) == 2
    assert task.state == TaskState.PENDING
    await TaskManager().delete(task.task_id)


@pytest.mark.asyncio
async def test_task_serialization():
    task = DurableTask(
        task_id="test-123",
        goal="Test goal",
        steps=[TaskStep("step-1", "First step", "test_action", {"key": "value"}, state=TaskState.COMPLETE)],
        state=TaskState.IN_PROGRESS,
    )
    restored = DurableTask.from_dict(task.to_dict())
    assert restored.task_id == task.task_id
    assert restored.steps[0].state == TaskState.COMPLETE


@pytest.mark.asyncio
async def test_task_persistence():
    manager = TaskManager()
    task = DurableTask(task_id="persist-test-001", goal="Test persistence", steps=[TaskStep("step-1", "Persisted", "test")])
    await manager.save(task)
    loaded = await manager.load("persist-test-001")
    assert loaded is not None
    assert loaded.goal == "Test persistence"
    await manager.delete("persist-test-001")


@pytest.mark.asyncio
async def test_next_pending_step():
    task = DurableTask(
        task_id="test-steps",
        goal="Test",
        steps=[
            TaskStep("step-1", "First", "action1", state=TaskState.COMPLETE),
            TaskStep("step-2", "Second", "action2", state=TaskState.PENDING),
        ],
    )
    assert task.next_pending_step().step_id == "step-2"


@pytest.mark.asyncio
async def test_per_reply_step_limit():
    steps = [{"description": f"Step {i}", "action": "noop"} for i in range(15)]
    task = await create_task("Long task", steps)
    result = await resume_task(task.task_id, max_steps_this_reply=3, execute_step=_ok)
    assert result["success"]
    assert result["steps_executed"] == 3
    assert result["steps_remaining"] == 12
    assert result["state"] == TaskState.BLOCKED.value
    assert result["pause_reason"] == StepLimit.PER_REPLY_CAP.value
    await TaskManager().delete(task.task_id)


@pytest.mark.asyncio
async def test_task_resume_across_replies():
    steps = [{"description": f"Step {i}", "action": "noop"} for i in range(5)]
    task = await create_task("Resumable task", steps)
    first = await resume_task(task.task_id, max_steps_this_reply=2, execute_step=_ok)
    assert first["pause_reason"] == StepLimit.PER_REPLY_CAP.value
    second = await resume_task(task.task_id, max_steps_this_reply=5, execute_step=_ok)
    assert second["steps_remaining"] == 0
    assert second["state"] == TaskState.COMPLETE.value
    await TaskManager().delete(task.task_id)


def test_20_plus_step_scripted_workflow():
    plan = [{"description": f"step {i}", "action": "run_shell"} for i in range(22)]
    assert len(plan) >= 20
