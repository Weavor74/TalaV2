import pytest
from sdk.agent.tools import read_neural_node
from sdk.agent.documentation_nodes import resolve_active_nodes_dir

@pytest.mark.asyncio
async def test_read_settings_system_node_roaming():
    # Force sync / create settings nodes
    from python.harness.settings_nn import sync_settings_to_nodes
    await sync_settings_to_nodes()
    
    # Verify the settings-system.html file exists in roaming nodes dir
    nodes_dir = resolve_active_nodes_dir()
    node_file = nodes_dir / "settings-system.html"
    assert node_file.exists()
    
    # Retrieve via read_neural_node tool
    result = await read_neural_node("settings-system")
    assert result["status"] == "success"
    assert result["node_id"] == "settings-system"
    assert result["state_data"]["title"] == "System Specs"
    assert "os" in result["state_data"]
