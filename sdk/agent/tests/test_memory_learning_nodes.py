from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from python.harness.memory import handle_memory_record_completed_turn


@pytest.mark.asyncio
@patch("python.harness.memory.create_learning_documentation_node")
@patch("python.harness.memory.handle_memory_reindex_node", new_callable=AsyncMock)
@patch("python.harness.memory.sync_manager.append_session_event")
@patch("python.harness.memory.db.execute", new_callable=AsyncMock)
@patch("python.harness.memory.get_manager")
async def test_record_completed_turn_creates_learning_node_from_explicit_payload(
    mock_get_manager,
    mock_db_execute,
    mock_append_session_event,
    mock_reindex_node,
    mock_create_learning_node,
):
    manager = MagicMock()
    manager.store = AsyncMock()
    mock_get_manager.return_value = manager
    mock_create_learning_node.return_value = {
        "path": r"C:\\nodes\\learning-provider-routing.html",
        "node_id": "learning-provider-routing",
        "created": True,
    }

    result = await handle_memory_record_completed_turn(
        {
            "session_id": "session-1",
            "role": "assistant",
            "content": "Neutral response text.",
            "tool_calls": [],
            "learning_event": {
                "topic": "Provider routing",
                "summary": "Tala learned to prefer local inference first.",
                "canonical_node_id": "system-tala-configurable-provider-surface",
                "details": ["Prefer local backends when available."],
                "tags": ["memory", "learning"],
            },
        }
    )

    assert result["status"] == "recorded"
    assert result["learning_node_path"] == r"C:\\nodes\\learning-provider-routing.html"
    assert result["learning_node_id"] == "learning-provider-routing"
    mock_create_learning_node.assert_called_once()
    call_kwargs = mock_create_learning_node.call_args.kwargs
    assert call_kwargs["topic"] == "Provider routing"
    assert call_kwargs["canonical_node_id"] == "system-tala-configurable-provider-surface"
    mock_reindex_node.assert_awaited_once_with({"node_path": r"C:\\nodes\\learning-provider-routing.html"})
    manager.store.assert_awaited_once()
    mock_append_session_event.assert_called_once()
    mock_db_execute.assert_awaited()


@pytest.mark.asyncio
@patch("python.harness.memory.create_learning_documentation_node")
@patch("python.harness.memory.handle_memory_reindex_node", new_callable=AsyncMock)
@patch("python.harness.memory.sync_manager.append_session_event")
@patch("python.harness.memory.db.execute", new_callable=AsyncMock)
@patch("python.harness.memory.get_manager")
async def test_record_completed_turn_creates_learning_node_from_learning_cue(
    mock_get_manager,
    mock_db_execute,
    mock_append_session_event,
    mock_reindex_node,
    mock_create_learning_node,
):
    manager = MagicMock()
    manager.store = AsyncMock()
    mock_get_manager.return_value = manager
    mock_create_learning_node.return_value = {
        "path": r"C:\\nodes\\learning-local-first.html",
        "node_id": "learning-local-first",
        "created": True,
    }

    result = await handle_memory_record_completed_turn(
        {
            "session_id": "session-2",
            "role": "assistant",
            "content": "From now on, prefer local inference backends when they are available.",
            "tool_calls": [],
        }
    )

    assert result["status"] == "recorded"
    assert result["learning_node_path"] == r"C:\\nodes\\learning-local-first.html"
    assert result["learning_node_id"] == "learning-local-first"
    mock_create_learning_node.assert_called_once()
    call_kwargs = mock_create_learning_node.call_args.kwargs
    assert "local inference backends" in call_kwargs["topic"].lower()
    assert "prefer local inference backends" in call_kwargs["summary"].lower()
    mock_reindex_node.assert_awaited_once_with({"node_path": r"C:\\nodes\\learning-local-first.html"})
    mock_append_session_event.assert_called_once()
    mock_db_execute.assert_awaited()


@pytest.mark.asyncio
@patch("python.harness.memory.create_learning_documentation_node")
@patch("python.harness.memory.handle_memory_reindex_node", new_callable=AsyncMock)
@patch("python.harness.memory.sync_manager.append_session_event")
@patch("python.harness.memory.db.execute", new_callable=AsyncMock)
@patch("python.harness.memory.get_manager")
async def test_record_completed_turn_ignores_regular_chatter(
    mock_get_manager,
    mock_db_execute,
    mock_append_session_event,
    mock_reindex_node,
    mock_create_learning_node,
):
    manager = MagicMock()
    manager.store = AsyncMock()
    mock_get_manager.return_value = manager

    result = await handle_memory_record_completed_turn(
        {
            "session_id": "session-3",
            "role": "assistant",
            "content": "Here is the status update with no durable lesson.",
            "tool_calls": [],
        }
    )

    assert result == {"status": "recorded"}
    mock_create_learning_node.assert_not_called()
    mock_reindex_node.assert_not_awaited()
    mock_append_session_event.assert_called_once()
    mock_db_execute.assert_awaited()
