import pytest
from sdk.agent.tools import get_system_info_tool

@pytest.mark.asyncio
async def test_get_system_info_tool():
    info = await get_system_info_tool()
    assert isinstance(info, dict)
    assert "os" in info or "cpu" in info or "services" in info
    if "os" in info:
        assert info["os"]["system"] == "Windows"
