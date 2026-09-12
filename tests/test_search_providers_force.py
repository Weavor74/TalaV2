import pytest
import json
from unittest.mock import patch, MagicMock
from python.harness.search_manager import SearchManager
from python.harness.db import init_pool, close_pool

@pytest.mark.asyncio
async def test_search_provider_locked_routing():
    """
    NAME
           test_search_provider_locked_routing - verifies locked search provider routing behavior
           
    SYNOPSIS
           test_search_provider_locked_routing() -> None
           
    DESCRIPTION
           Verifies that when a search provider is locked (forced), the SearchManager
           routes queries exclusively through that provider, bypassing other active backends
           and skipping crawlers.
    """
    mock_providers = [
        {
            "id": "sprov-1",
            "name": "Standard Provider",
            "endpoint": "https://api.tavily.com/search",
            "api_key": "key-1",
            "priority": 1,
            "locked": False
        },
        {
            "id": "sprov-2",
            "name": "Forced Provider",
            "endpoint": "https://api.searxng.org/search",
            "api_key": "key-2",
            "priority": 2,
            "locked": True
        }
    ]
    
    with patch("python.harness.search_manager.get_search_providers", return_value=mock_providers):
        with patch("httpx.AsyncClient.post") as mock_post, patch("httpx.AsyncClient.get") as mock_get:
            # Mock get response for Searxng (forced provider)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"results": [{"title": "Forced Result", "url": "https://forced.com", "content": "Success"}]}
            mock_get.return_value = mock_resp
            
            results = await SearchManager.query("test query")
            
            # Assert that tavily (sprov-1) was bypassed (post was not called)
            assert not mock_post.called
            # Assert that searxng (sprov-2) was queried
            assert mock_get.called
            assert results[0]["title"] == "Forced Result"


@pytest.mark.asyncio
async def test_search_provider_db_save_and_load():
    """
    NAME
           test_search_provider_db_save_and_load - verifies saving and loading search providers
           
    SYNOPSIS
           test_search_provider_db_save_and_load() -> None
           
    DESCRIPTION
           Tests the JSON-RPC endpoints handle_search_get_providers and handle_search_save_providers
           to ensure provider locked configuration state is correctly persisted to PostgreSQL.
    """
    from python.harness.server import handle_search_save_providers, handle_search_get_providers
    
    # Initialize DB pool
    await init_pool()
    try:
        test_providers = [
            {
                "id": "test-prov-1",
                "name": "Tavily Engine",
                "endpoint": "https://api.tavily.com/search",
                "api_key": "secret",
                "locked": True
            }
        ]
        
        # Save providers via RPC handler
        save_res_str = await handle_search_save_providers({"providers": test_providers}, "req-1")
        save_res = json.loads(save_res_str)
        assert save_res.get("result", {}).get("success") is True
        
        # Load providers via RPC handler
        get_res_str = await handle_search_get_providers({}, "req-2")
        get_res = json.loads(get_res_str)
        
        loaded_providers = get_res.get("result", {}).get("providers", [])
        assert len(loaded_providers) >= 1
        
        # Verify that the locked attribute is correctly saved and loaded
        target = next((p for p in loaded_providers if p["id"] == "test-prov-1"), None)
        assert target is not None
        assert target["locked"] is True
    finally:
        await close_pool()
