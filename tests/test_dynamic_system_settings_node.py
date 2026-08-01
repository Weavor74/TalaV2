import pytest
from pathlib import Path
from sdk.agent.tools import get_system_settings_info

def test_no_hardcoded_os_prompt_in_server_py():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert "=== HOST OPERATING SYSTEM & ENVIRONMENT ===" not in server_py
    assert "=== LIVE HARNESS SETTINGS & SYSTEM SNAPSHOTS ===" in server_py

@pytest.mark.asyncio
async def test_get_system_settings_info_tool():
    res = await get_system_settings_info()
    assert res.get("status") == "success"
    assert "semantic_memory" in res
