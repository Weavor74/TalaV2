"""
Sequential VRAM loading: 6 GB holds one big model at a time, so the main
llama-server and the Ollama models are swapped, never co-resident.
"""
from __future__ import annotations

import pytest

from python.inference import profiles, vram_arbiter
from python.inference.vram_arbiter import SERVER, OLLAMA, Action, VramArbiter, plan


def test_using_the_main_server_unloads_every_ollama_model():
    assert plan(using=SERVER, loaded_ollama=["gemma3:4b", "llama3.1:8b"], server_running=False) == [
        Action("unload", "gemma3:4b"), Action("unload", "llama3.1:8b"),
    ]


def test_using_an_ollama_model_stops_the_server_and_unloads_the_others():
    actions = plan(using=OLLAMA, model="gemma3:4b",
                   loaded_ollama=["gemma3:4b", "llama3.1:8b"], server_running=True)
    assert Action("unload", "llama3.1:8b") in actions
    assert Action("stop", SERVER) in actions
    assert Action("unload", "gemma3:4b") not in actions  # the model about to be used stays


def test_nothing_to_do_when_the_gpu_is_already_clear():
    assert plan(using=SERVER, loaded_ollama=[], server_running=True) == []
    assert plan(using=OLLAMA, model="m", loaded_ollama=["m"], server_running=False) == []


def test_a_non_exclusive_profile_never_swaps():
    assert plan(using=SERVER, loaded_ollama=["m"], server_running=True, exclusive=False) == []


def test_an_unknown_engine_is_refused():
    with pytest.raises(ValueError):
        plan(using="vllm", loaded_ollama=[], server_running=False)


def _arbiter(*, loaded, running, log, fail=None):
    state = {"loaded": list(loaded), "running": running}

    async def ollama_loaded():
        return list(state["loaded"])

    async def ollama_unload(model):
        if fail == model:
            raise RuntimeError("ollama refused")
        log.append(("unload", model))
        state["loaded"].remove(model)

    async def server_stop():
        log.append(("stop", SERVER))
        state["running"] = False

    return VramArbiter(
        exclusive=True, ollama_loaded=ollama_loaded, ollama_unload=ollama_unload,
        server_running=lambda: state["running"], server_stop=server_stop,
    ), state


@pytest.mark.asyncio
async def test_main_then_vision_then_main_swaps_one_model_at_a_time():
    log: list = []
    arbiter, state = _arbiter(loaded=["gemma3:4b"], running=False, log=log)

    await arbiter.prepare(SERVER)  # main chat turn: vision must go
    assert log == [("unload", "gemma3:4b")] and state["loaded"] == []

    state["running"] = True
    state["loaded"] = []
    log.clear()
    await arbiter.prepare(OLLAMA, "gemma3:4b")  # image turn: the server must go
    assert log == [("stop", SERVER)] and state["running"] is False


@pytest.mark.asyncio
async def test_a_failed_unload_is_survived_and_the_rest_still_run():
    log: list = []
    arbiter, _ = _arbiter(loaded=["a", "b"], running=False, log=log, fail="a")
    actions = await arbiter.prepare(SERVER)
    assert [a.target for a in actions] == ["a", "b"]
    assert log == [("unload", "b")]  # b was still unloaded after a failed


@pytest.mark.asyncio
async def test_prepare_for_routes_providers_to_the_right_engine(monkeypatch):
    seen: list = []

    class _Spy:
        async def prepare(self, using, model=""):
            seen.append((using, model))
            return []

    monkeypatch.setattr(vram_arbiter, "_default_arbiter", lambda profile: _Spy())
    vram_arbiter._arbiters.clear()
    kagura = profiles.get("kagura")

    await vram_arbiter.prepare_for({"type": "embedded", "config": {}}, kagura)
    await vram_arbiter.prepare_for(
        {"type": "local", "config": {"engine": "ollama", "model": "gemma3:4b"}}, kagura)
    await vram_arbiter.prepare_for({"type": "remote", "config": {}}, kagura)
    await vram_arbiter.prepare_for({"type": "embedded", "config": {}}, None)

    assert seen == [(SERVER, ""), (OLLAMA, "gemma3:4b")]
    vram_arbiter._arbiters.clear()


# --- vision on the CPU: an image turn must not cost the main model its place ------------------------------------------------------------


def test_a_cpu_only_ollama_model_needs_no_gpu_room():
    """An image turn on the CPU takes no VRAM, so the main server stays up and nothing is unloaded."""
    assert plan(using=OLLAMA, model="gemma3:4b", loaded_ollama=["llama3.1:8b"], server_running=True, cpu_only=True) == []


def test_the_main_server_leaves_a_cpu_resident_model_alone():
    actions = plan(using=SERVER, loaded_ollama=["gemma3:4b", "llama3.1:8b"], server_running=True, cpu_resident={"gemma3:4b"})
    assert actions == [Action("unload", "llama3.1:8b")]


def test_a_model_not_known_to_be_on_the_cpu_is_unloaded_as_before():
    assert plan(using=SERVER, loaded_ollama=["gemma3:4b"], server_running=True, cpu_resident=set()) == [Action("unload", "gemma3:4b")]
    assert plan(using=SERVER, loaded_ollama=["gemma3:4b"], server_running=True, cpu_resident=None) == [Action("unload", "gemma3:4b")]


def test_cpu_only_does_not_loosen_a_gpu_swap():
    assert Action("stop", SERVER) in plan(using=OLLAMA, model="llama3.1:8b", loaded_ollama=[], server_running=True, cpu_only=False)


def _arbiter_with_cpu(*, loaded, cpu, running, log):
    state = {"loaded": list(loaded), "running": running}

    async def ollama_loaded():
        return list(state["loaded"])

    async def cpu_resident():
        return set(cpu)

    async def ollama_unload(model):
        log.append(("unload", model))
        state["loaded"].remove(model)

    async def server_stop():
        log.append(("stop", SERVER))
        state["running"] = False

    return VramArbiter(exclusive=True, ollama_loaded=ollama_loaded, ollama_unload=ollama_unload,
                       server_running=lambda: state["running"], server_stop=server_stop, ollama_cpu_resident=cpu_resident), state


@pytest.mark.asyncio
async def test_an_image_turn_on_the_cpu_followed_by_a_text_turn_never_stops_the_server():
    """The point of the whole change: image turn, then text turn, and llama-server is never restarted."""
    log: list = []
    arbiter, state = _arbiter_with_cpu(loaded=[], cpu=["gemma3:4b"], running=True, log=log)

    assert await arbiter.prepare(OLLAMA, "gemma3:4b", cpu_only=True) == []   # the image turn
    state["loaded"] = ["gemma3:4b"]                                           # the vision model is now resident, on the CPU
    assert await arbiter.prepare(SERVER) == []                                # the next text turn
    assert log == [] and state["running"] is True and state["loaded"] == ["gemma3:4b"]


@pytest.mark.asyncio
async def test_a_gpu_model_still_makes_way_for_the_server_while_a_cpu_one_stays():
    log: list = []
    arbiter, state = _arbiter_with_cpu(loaded=["gemma3:4b", "llama3.1:8b"], cpu=["gemma3:4b"], running=False, log=log)
    await arbiter.prepare(SERVER)
    assert log == [("unload", "llama3.1:8b")] and state["loaded"] == ["gemma3:4b"]


@pytest.mark.asyncio
async def test_an_arbiter_that_cannot_tell_where_models_live_behaves_as_before():
    log: list = []
    arbiter, _ = _arbiter(loaded=["gemma3:4b"], running=False, log=log)  # no ollama_cpu_resident callback
    await arbiter.prepare(SERVER)
    assert log == [("unload", "gemma3:4b")]


@pytest.mark.asyncio
async def test_prepare_for_treats_a_num_gpu_zero_provider_as_cpu_only(monkeypatch):
    seen: list = []

    class _Spy:
        async def prepare(self, using, model="", cpu_only=False):
            seen.append((using, model, cpu_only))
            return []

    monkeypatch.setattr(vram_arbiter, "_default_arbiter", lambda profile: _Spy())
    vram_arbiter._arbiters.clear()
    kagura = profiles.get("kagura")
    cpu = {"type": "local", "config": {"engine": "ollama", "model": "gemma3:4b", "num_gpu": 0}}
    gpu = {"type": "local", "config": {"engine": "ollama", "model": "llama3.1:8b"}}
    await vram_arbiter.prepare_for(cpu, kagura)
    await vram_arbiter.prepare_for(gpu, kagura)
    assert seen == [(OLLAMA, "gemma3:4b", True), (OLLAMA, "llama3.1:8b", False)]
    vram_arbiter._arbiters.clear()


@pytest.mark.asyncio
async def test_the_real_arbiter_reads_where_ollama_models_are_resident(monkeypatch):
    """/api/ps reports size_vram per model: 0 means the model is in system memory."""
    import httpx

    ps = {"models": [{"name": "gemma3:4b", "size_vram": 0}, {"name": "llama3.1:8b", "size_vram": 4_800_000_000},
                     {"model": "other:1b"}]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return ps

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    arbiter = vram_arbiter._default_arbiter(profiles.get("kagura"))
    assert await arbiter._ollama_cpu_resident() == {"gemma3:4b"}, "a model with no VRAM figure is not assumed to be on the CPU"
