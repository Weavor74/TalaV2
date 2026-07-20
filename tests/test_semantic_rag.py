"""
test_semantic_rag.py - Auto-generated documentation base.
"""

import pytest
import json
from python.harness.server import handle_p2p_semantic_search

@pytest.mark.asyncio
async def test_peer_semantic_search_response_format():
    """
    Ensure the new P2P RPC handler for semantic search returns a valid JSON-RPC 2.0 structure.
    """
    params = {
        "query": "test query",
        "peer_wallet": "mock_wallet"
    }
    req_id = 1
    
    response = await handle_p2p_semantic_search(params, req_id)
    res_data = json.loads(response)
    
    assert res_data["jsonrpc"] == "2.0"
    assert res_data["id"] == 1
    assert "result" in res_data or "error" in res_data
    
    if "result" in res_data:
        assert "matches" in res_data["result"]
        assert isinstance(res_data["result"]["matches"], list)
