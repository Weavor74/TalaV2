import pytest
import json
from unittest.mock import patch, MagicMock
from python.harness.memory import handle_research_unified_search
from python.harness.server import handle_research_search
from sdk.agent.tools import web_search

@pytest.mark.asyncio
async def test_unified_search_passes_provider_id():
    """
    NAME
           test_unified_search_passes_provider_id - verifies provider_id passing in unified search
           
    SYNOPSIS
           test_unified_search_passes_provider_id() -> None
           
    DESCRIPTION
           Verifies that handle_research_unified_search correctly retrieves the search_provider_id
           from incoming RPC params and propagates it down to SearchManager.query().
    """
    with patch("python.harness.search_manager.SearchManager.query") as mock_query:
        mock_query.return_value = [{"title": "Unified Result", "url": "https://test.com", "snippet": "Text"}]
        
        params = {
            "query": "artificial intelligence",
            "sources": ["web"],
            "search_provider_id": "sprov-custom-id"
        }
        
        res = await handle_research_unified_search(params)
        
        # Verify SearchManager.query was called with the correct parameters
        mock_query.assert_called_once_with("artificial intelligence", provider_id="sprov-custom-id")
        assert len(res["results"]) == 1
        assert res["results"][0]["title"] == "Unified Result"


@pytest.mark.asyncio
async def test_research_search_passes_provider_id():
    """
    NAME
           test_research_search_passes_provider_id - verifies provider_id passing in research search
           
    SYNOPSIS
           test_research_search_passes_provider_id() -> None
           
    DESCRIPTION
           Verifies that the handle_research_search RPC endpoint handles search_provider_id
           correctly and invokes SearchManager.query with the correct parameters.
    """
    with patch("python.harness.search_manager.SearchManager.query") as mock_query, \
         patch("python.harness.server.broadcast_notification") as mock_broadcast:
        mock_query.return_value = [{"title": "Search Result", "url": "https://search.com", "snippet": "Info"}]
        
        params = {
            "query": "deep learning",
            "search_provider_id": "sprov-another-id"
        }
        
        res_str = await handle_research_search(params, "req-999")
        res = json.loads(res_str)
        
        # Verify query propagation
        mock_query.assert_called_once_with("deep learning", provider_id="sprov-another-id")
        assert res.get("result", {}).get("results")[0]["title"] == "Search Result"


@pytest.mark.asyncio
async def test_web_search_tool_passes_provider_id():
    """
    NAME
           test_web_search_tool_passes_provider_id - verifies tool-level provider_id passing
           
    SYNOPSIS
           test_web_search_tool_passes_provider_id() -> None
           
    DESCRIPTION
           Verifies that calling the web_search tool with a search_provider_id propagates
           the value to SearchManager.query.
    """
    with patch("python.harness.search_manager.SearchManager.query") as mock_query:
        mock_query.return_value = []
        
        await web_search("neural network", search_provider_id="sprov-tool-id")
        
        mock_query.assert_called_once_with("neural network", provider_id="sprov-tool-id")
