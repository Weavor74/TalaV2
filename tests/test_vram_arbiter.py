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
