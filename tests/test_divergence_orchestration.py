import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from sdk.agent.goal_engine import GoalEngine
from sdk.agent.planner import TaskItem, TaskStatus

@pytest.mark.asyncio
@patch("sdk.agent.queue.GoalTreeDecomposer.decompose", new_callable=AsyncMock)
@patch("sdk.agent.dispatcher.TalaExecutionDispatcher.dispatch", new_callable=AsyncMock)
@patch("sdk.agent.validator.PhaseValidator.validate", new_callable=MagicMock)
@patch("sdk.agent.ghost_persistence.restore_from_ghost", new_callable=AsyncMock)
@patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.check_hash", new_callable=MagicMock)
@patch("sdk.agent.ghost_persistence.pre_flight_reconciliation", new_callable=AsyncMock)
async def test_minor_divergence_continues_immediately(
    mock_pre_flight, mock_check_hash, mock_transition, mock_restore, mock_validate, mock_dispatch, mock_decompose
):
    t1 = TaskItem(description="Task 1", dependencies=[])
    t1.task_id = "task_1"
    t1.payload = {}
    mock_decompose.return_value = [t1]
    mock_dispatch.return_value = {"command": "echo", "stdout": "Success", "success": True}
    
    val_result = MagicMock()
    val_result.success = True
    val_result.output = "OK"
    val_result.error = None
    mock_validate.return_value = val_result
    
    mock_restore.return_value = None
    
    # Configure minor divergence
    mock_check_hash.return_value = "minor"
    
    engine = GoalEngine()
    result = await engine.run_goal("Test Goal")
    
    assert result["success"] is True
    # Ensure pre-flight sync was NOT triggered for minor divergence
    assert mock_pre_flight.call_count == 0
    # Ensure the task was never paused/blocked
    for call in mock_transition.call_args_list:
        args = call[0]
        # args[2] is to_status
        assert args[2] != TaskStatus.BLOCKED

@pytest.mark.asyncio
@patch("sdk.agent.queue.GoalTreeDecomposer.decompose", new_callable=AsyncMock)
@patch("sdk.agent.dispatcher.TalaExecutionDispatcher.dispatch", new_callable=AsyncMock)
@patch("sdk.agent.validator.PhaseValidator.validate", new_callable=MagicMock)
@patch("sdk.agent.ghost_persistence.restore_from_ghost", new_callable=AsyncMock)
@patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.check_hash", new_callable=MagicMock)
@patch("sdk.agent.ghost_persistence.pre_flight_reconciliation", new_callable=AsyncMock)
async def test_major_divergence_pauses_syncs_resumes(
    mock_pre_flight, mock_check_hash, mock_transition, mock_restore, mock_validate, mock_dispatch, mock_decompose
):
    t1 = TaskItem(description="Task 1", dependencies=[])
    t1.task_id = "task_1"
    t1.payload = {}
    mock_decompose.return_value = [t1]
    mock_dispatch.return_value = {"command": "echo", "stdout": "Success", "success": True}
    
    val_result = MagicMock()
    val_result.success = True
    val_result.output = "OK"
    val_result.error = None
    mock_validate.return_value = val_result
    
    mock_restore.return_value = None
    
    # Configure major divergence
    mock_check_hash.return_value = "major"
    mock_pre_flight.return_value = True
    
    engine = GoalEngine()
    result = await engine.run_goal("Test Goal")
    
    assert result["success"] is True
    # Ensure pre-flight sync WAS triggered
    assert mock_pre_flight.call_count == 1
    
    # Verify transitions included Paused for Sync (BLOCKED) and Sync completed (PENDING)
    paused_found = False
    resumed_found = False
    for call in mock_transition.call_args_list:
        args = call[0]
        kwargs = call[1]
        to_status = args[2]
        reason = kwargs.get("reason", "")
        if to_status == TaskStatus.BLOCKED and reason == "Paused for Sync":
            paused_found = True
        if to_status == TaskStatus.PENDING and reason == "Sync completed":
            resumed_found = True
            
    assert paused_found is True
    assert resumed_found is True
