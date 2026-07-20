import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import patch, AsyncMock
from sdk.agent.tools import sync_blockchain

@pytest.mark.asyncio
@patch("sdk.agent.ghost_persistence.get_local_status")
@patch("sdk.agent.ghost_persistence.get_remote_status")
async def test_sync_blockchain_tool_denied_when_in_sync(mock_remote, mock_local):
    # Set matching hashes
    mock_local.return_value = {"latestBlockHash": "hash123", "height": 5}
    mock_remote.return_value = {"latestBlockHash": "hash123", "height": 5}
    
    with pytest.raises(PermissionError) as exc_info:
        await sync_blockchain()
        
    assert "No 'Divergence-Detected' signal present" in str(exc_info.value)

@pytest.mark.asyncio
@patch("sdk.agent.ghost_persistence.get_local_status")
@patch("sdk.agent.ghost_persistence.get_remote_status")
@patch("sdk.agent.ghost_persistence.pre_flight_reconciliation", new_callable=AsyncMock)
async def test_sync_blockchain_tool_allowed_on_divergence(mock_reconcile, mock_remote, mock_local):
    # Set diverging hashes
    mock_local.return_value = {"latestBlockHash": "hash_local", "height": 5}
    mock_remote.return_value = {"latestBlockHash": "hash_remote", "height": 1}
    mock_reconcile.return_value = True
    
    res = await sync_blockchain()
    assert "reconciliation successful" in res
    assert mock_reconcile.call_count == 1
