import pytest
from pathlib import Path
from sdk.agent.tools import get_system_settings_info

def test_harness_settings_snapshots_in_server_py():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert "update_all_settings_nodes()" in server_py
    assert "LIVE HARNESS SETTINGS & SYSTEM SNAPSHOTS" in server_py

@pytest.mark.asyncio
async def test_get_harness_settings_info_node():
    res = await get_system_settings_info(node_id="settings-system")
    assert res.get("status") == "success"
