import pytest
from sdk.agent.tools import ToolRegistry

@pytest.mark.asyncio
async def test_tool_argument_content_mapping():
    # Test mapping 'content' to 'command' in run_workspace_command
    attrs = {"content": "echo test_arg_mapping"}
    res = await ToolRegistry.run_tool("run_workspace_command", attrs, authority_level="auto")
    
    assert res["success"] is True
    assert res["error"] is None
    assert "test_arg_mapping" in res["output"].get("stdout", "")
