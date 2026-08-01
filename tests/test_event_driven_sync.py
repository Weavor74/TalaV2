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
@patch("asyncio.to_thread", new_callable=AsyncMock)
async def test_goal_engine_triggers_pre_flight_reconciliation(
    mock_to_thread, mock_pre_flight, mock_restore, mock_validate, mock_dispatch, mock_decompose
):
    """
    Verify that a major blockchain divergence detected during goal execution
    causes GoalEngine to invoke pre_flight_reconciliation before resuming the task.

    check_hash runs inside asyncio.to_thread; mocking to_thread to return "major"
    drives the divergence-detection branch that calls pre_flight_reconciliation.
    """
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
    # asyncio.to_thread wraps check_hash — return "major" to trigger the sync path
    mock_to_thread.return_value = "major"

    engine = GoalEngine()
    result = await engine.run_goal("Test Goal")

    assert result["success"] is True
    # Ensure pre-flight reconciliation was triggered on major divergence
    assert mock_pre_flight.call_count == 1
