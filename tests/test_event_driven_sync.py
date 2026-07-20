import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from sdk.agent.goal_engine import GoalEngine
from sdk.agent.planner import TaskItem

@pytest.mark.asyncio
@patch("sdk.agent.queue.GoalTreeDecomposer.decompose", new_callable=AsyncMock)
@patch("sdk.agent.dispatcher.TalaExecutionDispatcher.dispatch", new_callable=AsyncMock)
@patch("sdk.agent.validator.PhaseValidator.validate", new_callable=MagicMock)
@patch("sdk.agent.ghost_persistence.restore_from_ghost", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.pre_flight_reconciliation", new_callable=AsyncMock)
async def test_goal_engine_triggers_pre_flight_reconciliation(
    mock_pre_flight, mock_restore, mock_validate, mock_dispatch, mock_decompose
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
    mock_pre_flight.return_value = True
    
    engine = GoalEngine()
    result = await engine.run_goal("Test Goal")
    
    assert result["success"] is True
    # Ensure pre-flight reconciliation was triggered
    assert mock_pre_flight.call_count == 1
