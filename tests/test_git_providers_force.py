import pytest
import json
from python.harness.db import init_pool, close_pool
from python.harness.server import handle_git_save_providers, handle_git_get_providers

@pytest.mark.asyncio
async def test_git_provider_db_save_and_load():
    """
    NAME
           test_git_provider_db_save_and_load - verifies saving and loading git providers
           
    SYNOPSIS
           test_git_provider_db_save_and_load() -> None
           
    DESCRIPTION
           Tests the JSON-RPC endpoints handle_git_get_providers and handle_git_save_providers
           to ensure git provider locked configuration state is correctly persisted to PostgreSQL.
    """
    # Initialize DB pool
    await init_pool()
    try:
        test_providers = [
            {
                "id": "gitprov-test-1",
                "name": "GitHub Corporate",
                "endpoint": "https://github.com/my-org",
                "username": "my-user",
                "api_key": "ghp_secret",
                "locked": True
            },
            {
                "id": "gitprov-test-2",
                "name": "GitLab Backup",
                "endpoint": "https://gitlab.com/backup",
                "username": "my-user-backup",
                "api_key": "glpat_secret",
                "locked": False
            }
        ]
        
        # Save providers via RPC handler
        save_res_str = await handle_git_save_providers({"providers": test_providers}, "req-git-1")
        save_res = json.loads(save_res_str)
        assert save_res.get("result", {}).get("success") is True
        
        # Load providers via RPC handler
        get_res_str = await handle_git_get_providers({}, "req-git-2")
        get_res = json.loads(get_res_str)
        
        loaded_providers = get_res.get("result", {}).get("providers", [])
        assert len(loaded_providers) >= 2
        
        # Verify locked attribute is correctly saved and loaded
        p1 = next((p for p in loaded_providers if p["id"] == "gitprov-test-1"), None)
        assert p1 is not None
        assert p1["locked"] is True
        
        p2 = next((p for p in loaded_providers if p["id"] == "gitprov-test-2"), None)
        assert p2 is not None
        assert p2["locked"] is False
    finally:
        await close_pool()
