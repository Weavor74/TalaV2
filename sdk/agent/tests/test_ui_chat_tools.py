from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from typing import Any

from sdk.agent.tools import ToolRegistry, register_tool

# Reset registry between tests to ensure a clean slate
@pytest.fixture(autouse=True)
def reset_registry():
    ToolRegistry._tools = {}
    yield

def test_tool_registration():
    @register_tool(name="test_tool", description="A test tool")
    def my_tool(arg: str) -> str:
        return f"Hello {arg}"
        
    tools = ToolRegistry.get_tools()
    tool = next((t for t in tools if t["name"] == "test_tool"), None)
    assert tool is not None
    assert tool["description"] == "A test tool"
    assert tool["schema"]["properties"]["arg"]["type"] == "string"

@pytest.mark.asyncio
async def test_run_tool_success():
    @register_tool(name="async_tool", description="Async tool")
    async def async_tool(val: int) -> int:
        return val * 2
        
    # Mock RiskClassifier to allow
    with patch("sdk.agent.governance.RiskClassifier.classify_action", return_value="low"):
        result = await ToolRegistry.run_tool("async_tool", {"val": 5})
        assert result["success"] is True
        assert result["output"] == 10

@pytest.mark.asyncio
async def test_run_tool_blocked_by_governance():
    @register_tool(name="dangerous_tool", description="High risk")
    def dangerous_tool() -> str:
        return "Boom"
        
    # Mock RiskClassifier to return critical
    with patch("sdk.agent.governance.RiskClassifier.classify_action", return_value="critical"):
        # If authority is auto, critical risk should block and require confirmation
        result = await ToolRegistry.run_tool("dangerous_tool", {}, authority_level="auto")
        assert result["success"] is False
        assert result.get("requires_confirmation") is True
        assert result["risk_level"] == "critical"
