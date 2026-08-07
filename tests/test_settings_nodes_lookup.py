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


@pytest.mark.asyncio
async def test_read_settings_code_registry_node_roaming():
    # CodeRegistry (sdk/agent/code_registry.py) persists reliably in the
    # client_settings table, but unlike every other settings category
    # (system, inference, runtime, ...) it previously had no HTML-NN
    # representation -- see html-nn-universal-data-mandate in project
    # memory. This mirrors it the same way the other 12 settings panels are
    # mirrored.
    from python.harness.settings_nn import sync_settings_to_nodes
    await sync_settings_to_nodes()

    nodes_dir = resolve_active_nodes_dir()
    node_file = nodes_dir / "settings-code-registry.html"
    assert node_file.exists()

    result = await read_neural_node("settings-code-registry")
    assert result["status"] == "success"
    assert result["node_id"] == "settings-code-registry"
    assert result["state_data"]["title"] == "Code Registry"
    assert "registry_entries" in result["state_data"]
    # Default registry keys (sdk/agent/code_registry.py::DEFAULT_REGISTRY_KEYS)
    # should always be present.
    assert "tala.code.default.language" in result["state_data"]["registry_entries"]
