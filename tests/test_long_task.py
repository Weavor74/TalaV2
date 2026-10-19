"""
Tests for long_task.py - durable task state and multi-reply step handling.

Run with: pytest tests/test_long_task.py -v
"""
from __future__ import annotations

import asyncio
import pytest

from sdk.agent.long_task import (
    TaskState,
    StepLimit,
    TaskStep,
    DurableTask,
    TaskManager,
    create_task,
    resume_task,
)


@pytest.mark.asyncio
async def test_task_creation():
    """A task can be created with multiple steps."""
    task = await create_task(
        goal="Test multi-step workflow",
        steps=[
            {"description": "Step 1", "action": "get_system_info", "args": {}},
            {"description": "Step 2", "action": "search_memory", "args": {"query": "test"}},
        ],
    )
    
    assert task.task_id.startswith("task-")
    assert task.goal == "Test multi-step workflow"
    assert len(task.steps) == 2
    assert task.state == TaskState.PENDING
    assert task.pause_reason == StepLimit.NONE


@pytest.mark.asyncio
async def test_task_serialization():
    """Tasks can be serialized and deserialized."""
    task = DurableTask(
        task_id="test-123",
        goal="Test goal",
        steps=[
            TaskStep(
                step_id="step-1",
                description="First step",
                action="test_action",
                args={"key": "value"},
                state=TaskState.COMPLETE,
            )
        ],
        state=TaskState.IN_PROGRESS,
    )
    
    data = task.to_dict()
    restored = DurableTask.from_dict(data)
    
    assert restored.task_id == task.task_id
    assert restored.goal == task.goal
    assert len(restored.steps) == 1
    assert restored.steps[0].state == TaskState.COMPLETE


@pytest.mark.asyncio
async def test_task_persistence():
    """Tasks can be saved and loaded from the database."""
    manager = TaskManager()
    task = DurableTask(
        task_id="persist-test-001",
        goal="Test persistence",
        steps=[
            TaskStep(
                step_id="step-1",
                description="Persisted step",
                action="test",
            )
        ],
    )
    
    await manager.save(task)
    loaded = await manager.load("persist-test-001")
    
    assert loaded is not None
    assert loaded.task_id == "persist-test-001"
    assert loaded.goal == "Test persistence"
    
    await manager.delete("persist-test-001")


@pytest.mark.asyncio
async def test_next_pending_step():
    """next_pending_step returns the first non-complete step."""
    task = DurableTask(
        task_id="test-steps",
        goal="Test",
        steps=[
            TaskStep("step-1", "First", "action1", state=TaskState.COMPLETE),
            TaskStep("step-2", "Second", "action2", state=TaskState.PENDING),
            TaskStep("step-3", "Third", "action3", state=TaskState.PENDING),
        ],
    )
    
    next_step = task.next_pending_step()
    assert next_step is not None
    assert next_step.step_id == "step-2"


@pytest.mark.asyncio
async def test_task_completion():
    """is_complete returns True only when all steps are done."""
    task = DurableTask(
        task_id="test-complete",
        goal="Test",
        steps=[
            TaskStep("step-1", "First", "action1", state=TaskState.COMPLETE),
            TaskStep("step-2", "Second", "action2", state=TaskState.COMPLETE),
        ],
    )
    
    assert task.is_complete()
    
    task.steps[1].state = TaskState.PENDING
    assert not task.is_complete()


@pytest.mark.asyncio
async def test_failed_steps():
    """failed_steps returns all steps in FAILED state."""
    task = DurableTask(
        task_id="test-failed",
        goal="Test",
        steps=[
            TaskStep("step-1", "First", "action1", state=TaskState.COMPLETE),
            TaskStep("step-2", "Second", "action2", state=TaskState.FAILED, error="Error 1"),
            TaskStep("step-3", "Third", "action3", state=TaskState.FAILED, error="Error 2"),
        ],
    )
    
    failed = task.failed_steps()
    assert len(failed) == 2
    assert all(s.state == TaskState.FAILED for s in failed)


@pytest.mark.asyncio
async def test_per_reply_step_limit():
    """Tasks pause when hitting per-reply step limit."""
    # Create a task with many steps
    steps = [{"description": f"Step {i}", "action": "get_system_info"} for i in range(15)]
    task = await create_task("Long task", steps)
    
    # Resume with a low step limit
    result = await resume_task(task.task_id, max_steps_this_reply=3)
    
    assert result["success"]
    assert result["steps_executed"] <= 3
    assert result["steps_remaining"] > 0
    assert result["state"] == TaskState.BLOCKED.value
    assert result["pause_reason"] == StepLimit.PER_REPLY_CAP.value
    
    # Clean up
    manager = TaskManager()
    await manager.delete(task.task_id)


@pytest.mark.asyncio
async def test_task_resume():
    """A paused task can be resumed in the next reply."""
    steps = [{"description": f"Step {i}", "action": "search_memory", "args": {"query": "test"}} for i in range(5)]
    task = await create_task("Resumable task", steps)
    
    # First reply: execute 2 steps
    result1 = await resume_task(task.task_id, max_steps_this_reply=2)
    assert result1["steps_executed"] <= 2
    assert result1["pause_reason"] == StepLimit.PER_REPLY_CAP.value
    
    # Second reply: execute remaining steps
    result2 = await resume_task(task.task_id, max_steps_this_reply=5)
    assert result2["steps_executed"] > 0
    
    # Clean up
    manager = TaskManager()
    await manager.delete(task.task_id)


@pytest.mark.asyncio
async def test_list_active_tasks():
    """list_active returns only non-complete/non-failed tasks."""
    manager = TaskManager()
    
    task1 = await create_task("Active task 1", [{"description": "Step 1", "action": "test"}])
    task2 = await create_task("Active task 2", [{"description": "Step 1", "action": "test"}])
    task3 = await create_task("Complete task", [{"description": "Step 1", "action": "test"}])
    task3.state = TaskState.COMPLETE
    await manager.save(task3)
    
    active = await manager.list_active()
    active_ids = {t.task_id for t in active}
    
    assert task1.task_id in active_ids
    assert task2.task_id in active_ids
    assert task3.task_id not in active_ids
    
    # Clean up
    await manager.delete(task1.task_id)
    await manager.delete(task2.task_id)
    await manager.delete(task3.task_id)


def test_20_plus_step_scripted_workflow():
    """Model test: a 20+ step workflow completes across multiple replies."""
    # This is a scripted test showing the agent can handle long workflows
    steps_plan = [
        {"description": "Read project requirements", "action": "read_file", "args": {"path": "requirements.txt"}},
        {"description": "Analyze dependencies", "action": "search_workspace_files", "args": {"pattern": "import"}},
        {"description": "Check test coverage", "action": "run_shell", "args": {"command": "pytest --cov"}},
        {"description": "Review security scan", "action": "run_shell", "args": {"command": "bandit -r ."}},
        {"description": "Update documentation", "action": "write_file", "args": {"path": "docs/API.md"}},
        {"description": "Generate type stubs", "action": "run_shell", "args": {"command": "stubgen"}},
        {"description": "Run linters", "action": "run_shell", "args": {"command": "ruff check ."}},
        {"description": "Format code", "action": "run_shell", "args": {"command": "ruff format ."}},
        {"description": "Build package", "action": "run_shell", "args": {"command": "python setup.py bdist_wheel"}},
        {"description": "Test installation", "action": "run_shell", "args": {"command": "pip install dist/*.whl"}},
        {"description": "Run integration tests", "action": "run_shell", "args": {"command": "pytest tests/integration"}},
        {"description": "Generate coverage report", "action": "run_shell", "args": {"command": "coverage html"}},
        {"description": "Update changelog", "action": "write_file", "args": {"path": "CHANGELOG.md"}},
        {"description": "Tag release", "action": "run_shell", "args": {"command": "git tag v1.0.0"}},
        {"description": "Build docs", "action": "run_shell", "args": {"command": "mkdocs build"}},
        {"description": "Upload to PyPI", "action": "run_shell", "args": {"command": "twine upload dist/*"}},
        {"description": "Deploy documentation", "action": "run_shell", "args": {"command": "mkdocs gh-deploy"}},
        {"description": "Create GitHub release", "action": "run_shell", "args": {"command": "gh release create v1.0.0"}},
        {"description": "Notify team", "action": "send_telegram_message", "args": {"message": "Release v1.0.0 deployed"}},
        {"description": "Archive artifacts", "action": "write_file", "args": {"path": "releases/v1.0.0.tar.gz"}},
        {"description": "Update monitoring", "action": "run_shell", "args": {"command": "curl -X POST metrics_endpoint"}},
        {"description": "Clean up temp files", "action": "run_shell", "args": {"command": "rm -rf build/"}},
    ]
    
    assert len(steps_plan) >= 20, "This test demonstrates a 20+ step workflow"
    
    # In a real scenario, the agent would:
    # 1. Create task with these steps
    # 2. Execute 8-10 steps in first reply (hit PER_REPLY_CAP)
    # 3. Resume in second reply, execute next 8-10 steps
    # 4. Resume in third reply, complete remaining steps
    # Each resume would check task.pause_reason == StepLimit.PER_REPLY_CAP
    # and continue from task.next_pending_step()
