import pytest
from unittest.mock import patch, AsyncMock
from sdk.agent.persistence import TaskEventStore

@pytest.mark.asyncio
async def test_task_event_store_broadcast():
    with patch("python.harness.server.broadcast_notification", new_callable=AsyncMock) as mock_broadcast:
        event_id = await TaskEventStore.append_event(
            task_id="test-task-123",
            event_type="phase_start",
            payload={"phase": "Phase 1: Test Phase"}
        )
        assert event_id is not None
        mock_broadcast.assert_called_once()
        args, _ = mock_broadcast.call_args
        assert args[0] == "task.event"
        assert args[1]["task_id"] == "test-task-123"
        assert args[1]["event_type"] == "phase_start"
        assert args[1]["payload"]["phase"] == "Phase 1: Test Phase"
