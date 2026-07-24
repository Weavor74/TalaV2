import pytest
from sdk.agent.tools import read_workspace_file

@pytest.mark.asyncio
async def test_read_workspace_file():
    # Test reading package.json
    res = await read_workspace_file("package.json")
    assert res["status"] == "success"
    assert "tala-engine" in res["content"]
    assert res["total_lines"] > 0

    # Test range reading
    res_range = await read_workspace_file("package.json", start_line=1, end_line=5)
    assert res_range["status"] == "success"
    assert len(res_range["content"].splitlines()) == 5

    # Test non-existent file
    res_missing = await read_workspace_file("non_existent_file_abc.txt")
    assert res_missing["status"] == "failed"

    # Test boundary violation
    res_boundary = await read_workspace_file("../package.json")
    assert res_boundary["status"] == "failed"
