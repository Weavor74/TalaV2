import pytest
from sdk.agent.tools import search_workspace_files, run_workspace_command

@pytest.mark.asyncio
async def test_search_workspace_files_tool():
    res = await search_workspace_files("search_workspace_files")
    assert res.get("status") == "success"
    assert res.get("matches_count", 0) > 0

@pytest.mark.asyncio
async def test_grep_interceptor():
    res = await run_workspace_command("grep -rn 'search_workspace_files' .")
    assert res.get("status") == "success"
    assert "matches" in res or "stdout" in res
