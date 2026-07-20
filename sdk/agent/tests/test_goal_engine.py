"""
test_goal_engine.py - Auto-generated documentation base.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from sdk.agent.goal_engine import GoalEngine
from sdk.agent.planner import TaskItem
from sdk.agent.batch_policy import BatchPolicy

@pytest.fixture
def dummy_tasks():
    t1 = TaskItem(description="Task 1", dependencies=[])
    t1.task_id = "task_1"
    t1.payload = {}
    
    t2 = TaskItem(description="Task 2", dependencies=["task_1"])
    t2.task_id = "task_2"
    t2.payload = {}
    
    return [t1, t2]

@pytest.mark.asyncio
@patch("sdk.agent.queue.GoalTreeDecomposer.decompose", new_callable=AsyncMock)
@patch("sdk.agent.dispatcher.TalaExecutionDispatcher.dispatch", new_callable=AsyncMock)
@patch("sdk.agent.validator.PhaseValidator.validate", new_callable=MagicMock)
@patch("sdk.agent.queue.BatchQueue.enqueue_task", new_callable=AsyncMock)
@patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
@patch("sdk.agent.persistence.TaskEventStore.append_event", new_callable=AsyncMock)
@patch("sdk.agent.telemetry.LiveStatusReporter.broadcast_status", new_callable=AsyncMock)
@patch("sdk.agent.goal_engine.GoalEngine._auto_document_skill", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.restore_from_ghost", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.check_hash", new_callable=MagicMock)
async def test_run_goal_success(
    mock_check_hash, mock_restore, mock_auto_doc, mock_broadcast, mock_append, 
    mock_transition, mock_enqueue, mock_validate, mock_dispatch, mock_decompose, dummy_tasks
):
    # Setup mock returns
    mock_restore.return_value = None
    mock_check_hash.return_value = "none"
    mock_decompose.return_value = dummy_tasks
    mock_dispatch.return_value = {"command": "echo", "stdout": "Success", "success": True}
    
    val_result = MagicMock()
    val_result.success = True
    val_result.output = "OK"
    val_result.error = None
    mock_validate.return_value = val_result
    
    engine = GoalEngine()
    policy = BatchPolicy.auto()
    
    result = await engine.run_goal("Test Goal", policy=policy)
    
    assert result["success"] is True
    assert len(result["phases_completed"]) == 2
    assert "Task 1" in result["phases_completed"]
    assert "Task 2" in result["phases_completed"]
    
    # Verify dispatcher called twice
    assert mock_dispatch.call_count == 2
    
    # Verify enqueue and transitions
    assert mock_enqueue.call_count >= 2

@pytest.mark.asyncio
@patch("sdk.agent.queue.GoalTreeDecomposer.decompose", new_callable=AsyncMock)
@patch("sdk.agent.dispatcher.TalaExecutionDispatcher.dispatch", new_callable=AsyncMock)
@patch("sdk.agent.validator.PhaseValidator.validate", new_callable=MagicMock)
@patch("sdk.agent.ghost_persistence.restore_from_ghost", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.check_hash", new_callable=MagicMock)
async def test_run_goal_failure_and_rollback(
    mock_check_hash, mock_restore, mock_validate, mock_dispatch, mock_decompose, dummy_tasks
):
    mock_restore.return_value = None
    mock_check_hash.return_value = "none"
    mock_decompose.return_value = dummy_tasks
    
    call_count = {"dispatch": 0, "validate": 0}
    
    async def mock_dispatch_fn(*args, **kwargs):
        call_count["dispatch"] += 1
        if call_count["dispatch"] == 1:
            return {"command": "echo", "stdout": "Success", "success": True}
        return {"command": "fail", "stderr": "Error", "success": False, "error": "Crash"}
        
    def mock_validate_fn(task, res):
        call_count["validate"] += 1
        if call_count["validate"] == 1:
            return MagicMock(success=True, output="OK", error=None)
        return MagicMock(success=False, output="", error="Crash")

    mock_dispatch.side_effect = mock_dispatch_fn
    mock_validate.side_effect = mock_validate_fn
    
    engine = GoalEngine()
    
    with patch.object(engine.recovery, "create_checkpoint", return_value="chk_123"):
        with patch.object(engine.recovery, "restore_checkpoint") as mock_restore_cp:
            result = await engine.run_goal("Test Goal")
            
            # Goal fails because second task fails and decomposer fails to generate a fix
            assert result["success"] is False
            assert "Crash" in result["error"]
            assert mock_restore_cp.call_count >= 1
