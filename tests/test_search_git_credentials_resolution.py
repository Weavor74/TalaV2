import pytest
from unittest.mock import patch
from python.harness.search_manager import get_search_providers
from python.harness.server import handle_git_get_providers

@pytest.mark.asyncio
async def test_search_provider_credential_resolution():
    """
    NAME
           test_search_provider_credential_resolution - verifies resolving search provider API key from credential
           
    SYNOPSIS
           test_search_provider_credential_resolution() -> None
           
    DESCRIPTION
           Mocks search_providers joined with auth_credentials and asserts that the API key
           is resolved correctly from the credential config.
    """
    mock_rows = [
        {
            "id": "sp-1",
            "name": "Tavily Search",
            "endpoint": "https://api.tavily.com/v1/search",
            "api_key": "original-key",
            "priority": 1,
            "locked": False,
            "credential_id": "cred-search",
            "cred_config": {"api_key": "resolved-api-key"}
        }
    ]

    with patch("python.harness.db.fetch", return_value=mock_rows):
        providers = await get_search_providers()
        assert len(providers) == 1
        assert providers[0]["api_key"] == "resolved-api-key"
        assert providers[0]["credential_id"] == "cred-search"

@pytest.mark.asyncio
async def test_git_provider_credential_resolution():
    """
    NAME
           test_git_provider_credential_resolution - verifies resolving git provider API key from credential
           
    SYNOPSIS
           test_git_provider_credential_resolution() -> None
           
    DESCRIPTION
           Mocks git_providers joined with auth_credentials and asserts that the API key
           is resolved correctly from the credential config.
    """
    mock_rows = [
        {
            "id": "git-1",
            "name": "My GitHub",
            "endpoint": "https://github.com",
            "username": "user1",
            "api_key": "original-token",
            "priority": 1,
            "locked": False,
            "credential_id": "cred-git",
            "cred_config": {"api_key": "resolved-git-token"}
        }
    ]

    with patch("python.harness.db.fetch", return_value=mock_rows):
        import json
        res_str = await handle_git_get_providers({}, None)
        res_data = json.loads(res_str)
        assert "error" not in res_data
        providers = res_data["result"]["providers"]
        assert len(providers) == 1
        assert providers[0]["api_key"] == "resolved-git-token"
        assert providers[0]["credential_id"] == "cred-git"
