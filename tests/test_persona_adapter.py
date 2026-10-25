"""
The trained persona adapter: a registry that keeps the last three and can roll back, the llama-server that serves the active one with
``--lora``, and how that server shares the 6 GB GPU with everything else (vram_arbiter) and joins a turn's provider chain (router).
Nothing here starts a real process: the spawn is a spy, the profile is built in memory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from python.inference import persona_adapter as pa
from python.inference import profiles, router, vram_arbiter
from python.inference.vram_arbiter import PERSONA, SERVER, Action, plan


@pytest.fixture(autouse=True)
def _world(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    return tmp_path


def _gguf(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.gguf"
    path.write_bytes(b"GGUF" + b"\0" * 5000)
    return path


def _add(tmp_path, adapter_id, **extra):
    entry = {"id": adapter_id, "created": f"2026-10-{int(adapter_id[-2:]):02d}T02:00:00", "gguf": str(_gguf(tmp_path, adapter_id)),
             "base": "unsloth/Llama-3.2-3B-Instruct", "dataset_version": "v0001", "scores": {}, **extra}
    pa.add_adapter(entry)
    return entry


# --- the registry ---------------------------------------------------------------------------------------------------------------


def test_a_new_adapter_is_registered_but_not_active(_world):
    _add(_world, "a-01")

    reg = pa._read_registry()

    assert reg["active"] is None and [a["id"] for a in reg["adapters"]] == ["a-01"]
    assert pa.active_entry() is None


def test_activating_makes_it_the_one_served_and_a_missing_file_is_refused(_world):
    entry = _add(_world, "a-01")
    pa.activate("a-01")

    assert pa.active_entry()["id"] == "a-01" and pa.active_entry()["gguf"] == entry["gguf"]

    Path(entry["gguf"]).unlink()
    _add(_world, "a-02")
    Path(pa._read_registry()["adapters"][-1]["gguf"]).unlink()
    with pytest.raises(FileNotFoundError):
        pa.activate("a-02")
    assert pa.active_entry()["id"] == "a-01", "a refused activation changes nothing"
    with pytest.raises(KeyError):
        pa.activate("nope")


def test_rollback_returns_to_the_adapter_that_was_active_before(_world):
    _add(_world, "a-01")
    _add(_world, "a-02")
    _add(_world, "a-03")
    pa.activate("a-01")
    pa.activate("a-02")
    pa.activate("a-03")

    assert pa.rollback()["id"] == "a-02"
    assert pa.active_entry()["id"] == "a-02"
    assert pa.rollback()["id"] == "a-01"
    assert pa.rollback() is None and pa.active_entry() is None, "nothing earlier: back to the plain base model"


def test_rollback_skips_an_adapter_whose_file_is_gone(_world):
    _add(_world, "a-01")
    second = _add(_world, "a-02")
    _add(_world, "a-03")
    pa.activate("a-01")
    pa.activate("a-02")
    pa.activate("a-03")
    Path(second["gguf"]).unlink()

    assert pa.rollback()["id"] == "a-01"


def test_only_the_newest_three_are_kept_and_the_active_one_never_goes(_world):
    for n in range(1, 6):
        _add(_world, f"a-0{n}")
    pa.activate("a-01")

    removed = pa.prune(keep=3)

    ids = [a["id"] for a in pa._read_registry()["adapters"]]
    assert sorted(removed) == ["a-02", "a-03"] and ids == ["a-01", "a-04", "a-05"]
    assert not any(Path(pa._read_registry()["adapters"][0]["gguf"]).parent.glob("a-02.gguf"))


def test_the_registry_survives_a_corrupt_file(_world):
    pa.registry_path().parent.mkdir(parents=True, exist_ok=True)
    pa.registry_path().write_text("{not json", encoding="utf-8")

    assert pa._read_registry() == {"active": None, "history": [], "adapters": []}


# --- serving it -----------------------------------------------------------------------------------------------------------------


def _persona_profile(tmp_path):
    base = _gguf(tmp_path, "base-3b")
    data = {"main": {"server_bin": str(tmp_path / "llama-server.exe"), "model_path": str(tmp_path / "main.gguf")},
            "persona": {"label": "llama-3.2-3b-persona", "model_path": str(base), "port": 8090, "ctx": 4096, "args": ["-ngl", "99"],
                        "base_model": "unsloth/Llama-3.2-3B-Instruct", "startup_timeout_s": 30}}
    return profiles._build("test", data)


def test_the_server_command_line_loads_the_adapter_with_lora(_world):
    profile = _persona_profile(_world)
    adapter = _gguf(_world, "a-01")

    argv = pa.server_argv(profile, [adapter], port=8090)

    assert argv[0].endswith("llama-server.exe") and "-m" in argv and str(profile.persona.model_path) in argv
    assert argv[argv.index("--lora") + 1] == str(adapter)
    assert "--lora-init-without-apply" not in argv and argv[argv.index("--port") + 1] == "8090" and "--jinja" in argv


def test_an_eval_server_loads_several_adapters_unapplied_so_they_can_be_switched(_world):
    profile = _persona_profile(_world)
    one, two = _gguf(_world, "a-01"), _gguf(_world, "a-02")

    argv = pa.server_argv(profile, [one, two], port=8091, apply=False)

    assert argv[argv.index("--lora") + 1] == f"{one},{two}" and "--lora-init-without-apply" in argv


def test_no_adapter_means_no_lora_flag(_world):
    argv = pa.server_argv(_persona_profile(_world), [], port=8090)

    assert "--lora" not in argv


@pytest.mark.asyncio
async def test_ensure_running_starts_the_active_adapter_once_and_restarts_when_it_changes(_world, monkeypatch):
    profile = _persona_profile(_world)
    monkeypatch.setattr(pa, "_profile", lambda: profile)
    started: list[list[Path]] = []

    class Handle:
        port = 8090
        base_url = "http://127.0.0.1:8090"

        def __init__(self, adapters):
            self.adapters = adapters
            self.stopped = False

    async def fake_spawn(prof, adapters, *, port=None, apply=True):
        started.append(list(adapters))
        return Handle(list(adapters))

    async def fake_stop(handle):
        handle.stopped = True

    monkeypatch.setattr(pa, "_spawn", fake_spawn)
    monkeypatch.setattr(pa, "_stop_handle", fake_stop)
    _add(_world, "a-01")
    _add(_world, "a-02")
    pa.activate("a-01")

    first = await pa.ensure_running()
    again = await pa.ensure_running()
    assert first is again and len(started) == 1

    pa.activate("a-02")
    second = await pa.ensure_running()

    assert second is not first and first.stopped and len(started) == 2 and started[1][0].name == "a-02.gguf"
    await pa.stop()
    assert second.stopped and pa.is_running() is False


@pytest.mark.asyncio
async def test_ensure_running_with_nothing_active_refuses_rather_than_serving_the_plain_base(_world, monkeypatch):
    monkeypatch.setattr(pa, "_profile", lambda: _persona_profile(_world))

    with pytest.raises(RuntimeError, match="no active"):
        await pa.ensure_running()


def test_the_provider_is_offered_only_when_an_adapter_is_active_and_the_profile_has_a_persona(_world, monkeypatch):
    profile = _persona_profile(_world)
    monkeypatch.setattr(pa, "_profile", lambda: profile)
    _add(_world, "a-01")
    assert pa.provider() is None

    pa.activate("a-01")
    provider = pa.provider()
    assert provider["type"] == "local" and provider["config"]["engine"] == "persona"
    assert provider["config"]["endpoint"] == "http://127.0.0.1:8090/v1" and provider["id"] == pa.PROVIDER_ID

    monkeypatch.setattr(pa, "_profile", lambda: None)
    assert pa.provider() is None


def test_status_is_what_is_true(_world, monkeypatch):
    monkeypatch.setattr(pa, "_profile", lambda: _persona_profile(_world))
    assert pa.status()["active"] is None and pa.status()["serving"] is False and pa.status()["adapters"] == 0

    _add(_world, "a-01", scores={"persona_nll": 1.9})
    pa.activate("a-01")
    status = pa.status()

    assert status["active"] == "a-01" and status["adapters"] == 1 and status["serving"] is False
    assert status["entries"][0]["scores"] == {"persona_nll": 1.9}


# --- sharing the GPU ------------------------------------------------------------------------------------------------------------


def test_the_persona_server_needs_the_main_server_and_gpu_ollama_models_out_of_the_way():
    actions = plan(using=PERSONA, loaded_ollama=["llama3.1:8b"], server_running=True)

    assert Action("unload", "llama3.1:8b") in actions and Action("stop", SERVER) in actions


def test_the_main_server_and_ollama_make_the_persona_server_leave():
    assert Action("stop", PERSONA) in plan(using=SERVER, loaded_ollama=[], server_running=False, persona_running=True)
    assert Action("stop", PERSONA) in plan(using="ollama", model="m", loaded_ollama=[], server_running=False, persona_running=True)
    assert Action("stop", PERSONA) not in plan(using=SERVER, loaded_ollama=[], server_running=False, persona_running=False)


@pytest.mark.asyncio
async def test_preparing_for_the_persona_provider_starts_its_server_after_making_room(monkeypatch):
    log: list[str] = []

    async def loaded():
        return ["gemma3:4b"]

    async def unload(model):
        log.append(f"unload {model}")

    async def server_stop():
        log.append("stop main")

    async def persona_start():
        log.append("start persona")

    async def persona_stop():
        log.append("stop persona")

    arbiter = vram_arbiter.VramArbiter(exclusive=True, ollama_loaded=loaded, ollama_unload=unload, server_running=lambda: True,
                                       server_stop=server_stop, persona_running=lambda: False, persona_stop=persona_stop,
                                       persona_start=persona_start)

    await arbiter.prepare(PERSONA)

    assert log == ["unload gemma3:4b", "stop main", "start persona"], "room is made first, the server started last"


@pytest.mark.asyncio
async def test_prepare_for_routes_a_persona_provider_to_the_persona_engine(monkeypatch):
    seen = []

    class Spy:
        async def prepare(self, using, model="", cpu_only=False):
            seen.append(using)
            return []

    monkeypatch.setattr(vram_arbiter, "_default_arbiter", lambda profile: Spy())
    vram_arbiter._arbiters.clear()
    profile = profiles.get("kagura")

    await vram_arbiter.prepare_for({"type": "local", "config": {"engine": "persona"}}, profile)

    assert seen == [PERSONA]


# --- joining a turn ---------------------------------------------------------------------------------------------------------------


def _chain(persona, mode, *, in_character, has_tools, profile):
    providers = [{"id": "embedded-builtin", "type": "embedded", "name": "e", "priority": 1, "config": {}}]
    route = type("R", (), {"providers": providers, "request_provider": None, "forced_provider": None, "strict": False})()
    return [p["id"] for p in router.apply(route, profile, [{"role": "user", "content": "hi"}], persona=persona, persona_mode=mode,
                                           in_character=in_character, has_tools=has_tools)]


def test_the_persona_leads_only_an_in_character_turn_without_tools_when_asked_to(_world):
    profile = _persona_profile(_world)
    persona = {"id": pa.PROVIDER_ID, "type": "local", "name": "persona", "priority": 3, "config": {"engine": "persona"}}

    assert _chain(persona, "in_character", in_character=True, has_tools=False, profile=profile)[0] == pa.PROVIDER_ID
    assert _chain(persona, "in_character", in_character=True, has_tools=True, profile=profile)[0] == "embedded-builtin"
    assert _chain(persona, "in_character", in_character=False, has_tools=False, profile=profile)[0] == "embedded-builtin"


def test_off_means_the_chain_is_exactly_what_it_was(_world):
    profile = _persona_profile(_world)
    persona = {"id": pa.PROVIDER_ID, "type": "local", "name": "persona", "priority": 3, "config": {"engine": "persona"}}

    off = _chain(persona, "off", in_character=True, has_tools=False, profile=profile)

    assert pa.PROVIDER_ID not in off and off == _chain(None, "off", in_character=True, has_tools=False, profile=profile)


def test_fallback_puts_the_persona_after_everything_else(_world):
    profile = _persona_profile(_world)
    persona = {"id": pa.PROVIDER_ID, "type": "local", "name": "persona", "priority": 3, "config": {"engine": "persona"}}

    chain = _chain(persona, "fallback", in_character=True, has_tools=False, profile=profile)

    assert chain[-1] == pa.PROVIDER_ID and chain[0] == "embedded-builtin"


def test_a_named_provider_is_never_overridden(_world):
    profile = _persona_profile(_world)
    persona = {"id": pa.PROVIDER_ID, "type": "local", "name": "persona", "priority": 3, "config": {"engine": "persona"}}
    named = {"id": "x", "type": "local", "name": "x", "priority": 1, "config": {}}
    route = type("R", (), {"providers": [named], "request_provider": named, "forced_provider": None, "strict": False})()

    out = router.apply(route, profile, [], persona=persona, persona_mode="in_character", in_character=True, has_tools=False)

    assert [p["id"] for p in out] == ["x"]


def test_a_turn_asks_the_router_for_the_persona_with_what_it_knows_about_itself(_world, monkeypatch):
    profile = _persona_profile(_world)
    monkeypatch.setattr(pa, "_profile", lambda: profile)

    assert router.persona_turn(in_character=True, has_tools=False) == {
        "persona": None, "persona_mode": "off", "in_character": True, "has_tools": False}, "nothing active: nothing offered"

    _add(_world, "a-01")
    pa.activate("a-01")
    from sdk.agent import node_learning as nl

    nl.write_config({"lora_routing": "in_character"})
    turn = router.persona_turn(in_character=True, has_tools=True)

    assert turn["persona"]["id"] == pa.PROVIDER_ID and turn["persona_mode"] == "in_character" and turn["has_tools"] is True


def test_both_provider_loops_in_the_server_ask_for_it():
    import inspect

    from python.harness import server

    source = inspect.getsource(server)
    assert source.count("_router.persona_turn(") == 2, "the main chat turn and _generate_reply both pass the persona to the router"
    assert "in_character=not raw_prompt and chat_mode in" in source, (
        "chat_mode is only assigned on the full-context path; a raw_prompt request (every autonomous goal) must not read it")
