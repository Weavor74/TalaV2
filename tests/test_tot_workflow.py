import pytest
from sdk.agent.tools import generate_workflow_from_tot_tool
from python.harness.workflows_util import resolve_workflow_file

@pytest.mark.asyncio
async def test_generate_workflow_from_tot(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    # Test target prompt
    prompt = "Create modular python microservice and build container"

    result = await generate_workflow_from_tot_tool(prompt, name="Test Microservice Flow")
    
    assert result.get("success") is True
    flow_id = result.get("flow_id")
    assert flow_id is not None
    assert result.get("name") == "Test Microservice Flow"
    
    # Assert workflow structure
    workflow = result.get("workflow")
    assert workflow is not None
    assert workflow["flow_id"] == flow_id
    
    nodes = workflow["nodes"]
    assert len(nodes) >= 3  # Trigger, at least one step, and output
    
    # Assert nodes are connected sequentially
    trigger_node = nodes[0]
    assert trigger_node["type"] == "trigger"
    assert trigger_node["connections"]["onSuccess"] == "step-0"
    
    last_step_node = nodes[-2]
    assert last_step_node["type"] == "custom_script"
    assert last_step_node["connections"]["onSuccess"] == "output-node"
    
    output_node = nodes[-1]
    assert output_node["type"] == "output"
    assert "onSuccess" not in output_node["connections"]
    
    # Verify the workflow files exist on disk
    json_path = resolve_workflow_file(flow_id)
    assert json_path is not None
    assert json_path.exists()
