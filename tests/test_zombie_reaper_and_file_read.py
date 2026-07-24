import pytest
from sdk.agent.tools import read_workspace_file
from sdk.agent.swarm_engine import ZombieReaper
import inspect

@pytest.mark.asyncio
async def test_read_workspace_file_str_parameters():
    # Pass string numbers for start_line and end_line
    res = await read_workspace_file("package.json", start_line="1", end_line="5")
    assert res["status"] == "success"
    assert len(res["content"].splitlines()) == 5

def test_zombie_reaper_default_timeout():
    sig = inspect.signature(ZombieReaper.reclaim_zombies)
    param = sig.parameters["timeout_minutes"]
    assert param.default == 30
