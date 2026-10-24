import pytest
from pathlib import Path
from sdk.agent.tools import get_system_settings_info

def test_harness_settings_snapshots_in_server_py():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    # regenerating all 13 nodes on every turn cost 13 s of each reply; the snapshot now reads the current nodes and refreshes them
    # in the background (tests/test_settings_refresh.py), and startup still does the full synchronisation
    assert "refresh_in_background()" in server_py and "sync_settings_to_nodes()" in server_py
    assert "LIVE HARNESS SETTINGS & SYSTEM SNAPSHOTS" in server_py

@pytest.mark.asyncio
async def test_get_harness_settings_info_node():
    res = await get_system_settings_info(node_id="settings-system")
    assert res.get("status") == "success"
