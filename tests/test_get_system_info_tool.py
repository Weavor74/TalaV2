import pytest
from sdk.agent.tools import get_system_info_tool

@pytest.mark.asyncio
async def test_get_system_info_tool():
    info = await get_system_info_tool()
    assert isinstance(info, dict)
    assert "os" in info or "cpu" in info or "services" in info
    if "os" in info:
        assert info["os"]["system"] == "Windows"


@pytest.mark.asyncio
async def test_get_system_info_tool_reports_gpu():
    """
    Regression: system.info (and the get_system_info tool built on top of it)
    used to have no gpu section at all -- CPU/RAM/disk/software only. Asked
    directly "what GPU are you running on", the model had no data to answer
    from and reported "no GPU mentioned", which was an honest read of a blind
    tool, not evidence local inference wasn't actually using a GPU. This
    doesn't assert a GPU is present (CI/other machines may not have one) --
    it asserts the *shape* is always there so the model always has real data
    to answer from instead of silence, and checks the CUDA fields make sense
    together when a GPU is actually detected.
    """
    info = await get_system_info_tool()
    assert "gpu" in info, "system.info must always report a gpu section, even when unavailable"
    gpu = info["gpu"]
    assert "available" in gpu
    assert "devices" in gpu
    assert isinstance(gpu["devices"], list)

    if gpu["available"]:
        assert gpu["backend"] == "cuda"
        assert len(gpu["devices"]) >= 1
        for device in gpu["devices"]:
            assert device["name"]
            assert device["total_vram_gb"] > 0
            assert "." in device["compute_capability"]
