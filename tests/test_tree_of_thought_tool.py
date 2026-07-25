import pytest
from sdk.agent.tools import tree_of_thought_ideation

@pytest.mark.asyncio
async def test_tree_of_thought_ideation_tool():
    res = await tree_of_thought_ideation("Add interactive new chat session button")
    assert isinstance(res, dict)
    assert res.get("status") in ("success", "error")
