import pytest
from sdk.agent import tools
from sdk.agent.tools import search_workspace_files, run_workspace_command

@pytest.fixture(autouse=True)
def search_workspace(tmp_path, monkeypatch):
    """Search a known workspace, not gigabytes of installed tools and caches."""
    (tmp_path / "example.py").write_text("search_workspace_files\n", encoding="utf-8")
    monkeypatch.setattr(tools, "_get_workspace_root", lambda: tmp_path)

@pytest.mark.asyncio
async def test_search_workspace_files_tool():
    res = await search_workspace_files("search_workspace_files")
    assert res.get("status") == "success"
    assert res.get("matches_count", 0) > 0

@pytest.mark.asyncio
async def test_grep_interceptor(monkeypatch):
    # Windows with Git Bash uses real grep. Exercise the fallback explicitly;
    # on POSIX this still checks the normal shell path against the same file.
    monkeypatch.setattr(tools, "_preferred_shell", lambda: ("powershell", [], "powershell"))
    res = await run_workspace_command("grep -rn 'search_workspace_files' .")
    assert res.get("status") == "success"
    assert "matches" in res or "stdout" in res
