import pytest

from python.harness.mcp_server import _call_tool, _list_tools


@pytest.mark.asyncio
async def test_list_tools_includes_known_methods():
    tools = await _list_tools()
    names = {t.name for t in tools}
    assert "harness.health" in names
    assert "system.capabilities" in names


@pytest.mark.asyncio
async def test_call_tool_rejects_missing_secret(monkeypatch):
    monkeypatch.setenv("TALA_HARNESS_ADMIN_SECRET", "correct-secret")
    with pytest.raises(PermissionError):
        await _call_tool("harness.health", {})


@pytest.mark.asyncio
async def test_call_tool_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("TALA_HARNESS_ADMIN_SECRET", "correct-secret")
    with pytest.raises(PermissionError):
        await _call_tool("harness.health", {"harness_secret": "wrong-secret"})


@pytest.mark.asyncio
async def test_call_tool_succeeds_with_correct_secret(monkeypatch):
    monkeypatch.setenv("TALA_HARNESS_ADMIN_SECRET", "correct-secret")
    result = await _call_tool("harness.health", {"harness_secret": "correct-secret"})
    assert len(result) == 1
    assert result[0].type == "text"
    assert "status" in result[0].text


@pytest.mark.asyncio
async def test_call_tool_unknown_method_raises(monkeypatch):
    monkeypatch.setenv("TALA_HARNESS_ADMIN_SECRET", "correct-secret")
    with pytest.raises(RuntimeError):
        await _call_tool("nonexistent.method", {"harness_secret": "correct-secret"})
