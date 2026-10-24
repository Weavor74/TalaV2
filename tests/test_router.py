"""
The per-turn router: which provider answers (vision when an image is attached,
main otherwise, fallback on failure or timeout), and what each one is shown.
"""
from __future__ import annotations

import dataclasses
import types

import pytest

from python.harness.provider_selection import resolve_provider_chain
from python.inference import profiles, router

IMAGE = "data:image/png;base64,iVBORw0KGgo="
TEXT_TURN = [{"role": "user", "content": "hello"}]
IMAGE_TURN = [{"role": "user", "content": [
    {"type": "text", "text": "what is this?"},
    {"type": "image_url", "image_url": {"url": IMAGE}},
]}]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("TALA_PROFILE", "TALA_PROFILES_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))


@pytest.fixture
def kagura():
    return profiles.get("kagura")


def _ids(chain):
    return [p["id"] for p in chain]


def _row(pid, ptype, model="", status="online", **cfg):
    return {"id": pid, "type": ptype, "name": pid, "priority": 1, "status": status,
            "config": {"model": model, **cfg}, "locked": False}


# --- selection ---------------------------------------------------------------------


def test_a_text_turn_goes_main_then_fallback(kagura):
    chain = router._build_chain([], kagura, images=False)
    assert _ids(chain) == ["embedded-builtin", "profile-fallback"]
    assert chain[0]["type"] == "embedded"
    assert chain[1]["config"]["model"] == kagura.fallback.model


def test_an_image_turn_goes_vision_first(kagura):
    chain = router._build_chain([], kagura, images=True)
    assert _ids(chain) == ["profile-vision", "embedded-builtin", "profile-fallback"]
    vision = chain[0]["config"]
    assert vision["model"] == "gemma3:4b"
    assert vision["num_ctx"] == 8192  # the 131k default spills onto the CPU
    assert vision["engine"] == "ollama" and vision["vision"] is True
    assert vision["endpoint"] == "http://localhost:11434/v1"


def test_other_installed_models_are_not_in_an_automatic_chain(kagura):
    installed = [_row("ollama-gpt-oss-heretic-20b", "local", "gpt-oss-heretic:20b", engine="ollama")]
    assert "ollama-gpt-oss-heretic-20b" not in _ids(router._build_chain(installed, kagura, images=False))


def test_a_configured_row_is_reused_and_its_stale_status_cleared(kagura):
    row = _row("ollama-gemma3-4b", "local", "gemma3:4b", status="offline", engine="ollama")
    chain = router._build_chain([row], kagura, images=True)
    vision = chain[0]
    assert vision["id"] == "ollama-gemma3-4b"
    assert vision["status"] == "unknown"  # a stale "offline" must not skip a profile role
    assert vision["config"]["num_ctx"] == 8192


def test_remote_providers_are_held_back_unless_the_profile_allows_them(kagura):
    gemini = _row("gemini", "remote", "gemini-x", service="gemini", apiKey="k")
    assert "gemini" not in _ids(router._build_chain([gemini], kagura, images=False))
    allowed = dataclasses.replace(kagura, allow_remote=True)
    assert _ids(router._build_chain([gemini], allowed, images=False))[-1] == "gemini"


def test_nano_has_no_vision_so_an_image_turn_is_main_then_fallback():
    nano = profiles.get("nano")
    assert _ids(router._build_chain([], nano, images=True)) == ["embedded-builtin", "profile-fallback"]


def test_without_a_profile_the_chain_is_untouched():
    chain = [_row("a", "local", "x")]
    assert router._build_chain(chain, None, images=True) is chain


def test_an_explicit_choice_is_never_overridden(kagura):
    providers = [_row("ollama-llama", "local", "llama3.2:3b", engine="ollama")]
    named = resolve_provider_chain(providers, request_provider_id="ollama-llama")
    assert router.apply(named, kagura, TEXT_TURN) == named.providers
    assert named.providers[0]["id"] == "ollama-llama"

    pinned = resolve_provider_chain(providers, pinned_id="ollama-llama", pin_strict=True)
    assert router.apply(pinned, kagura, TEXT_TURN) == [pinned.forced_provider]

    free = resolve_provider_chain(providers)
    assert _ids(router.apply(free, kagura, IMAGE_TURN))[0] == "profile-vision"


# --- what each provider is shown ---------------------------------------------------


def test_only_vision_capable_providers_see_the_image():
    embedded = {"type": "embedded", "config": {}}
    text_only = _row("t", "local", "llama3.1:8b", engine="ollama")
    gemma = _row("g", "local", "gemma3:4b", engine="ollama")
    marked = _row("m", "local", "my-custom", engine="ollama", vision=True)
    remote = _row("r", "remote", "gemini-x")
    assert [router._provider_sees_images(p) for p in (embedded, text_only, gemma, marked, remote)] == [
        False, False, True, True, True,
    ]


def test_a_text_only_provider_gets_text_and_a_note_not_the_image():
    out = router.messages_for({"type": "embedded", "config": {}}, IMAGE_TURN)
    assert out[0]["content"].startswith("what is this?")
    assert router.IMAGE_NOTE in out[0]["content"]
    assert not router._has_images(out)
    # a vision provider and a text-only turn both pass through unchanged
    assert router.messages_for(_row("g", "local", "gemma3:4b"), IMAGE_TURN) is IMAGE_TURN
    assert router.messages_for({"type": "embedded", "config": {}}, TEXT_TURN) is TEXT_TURN


def test_ollama_native_messages_carry_images_as_bare_base64():
    out = router.to_ollama_native_messages(IMAGE_TURN + TEXT_TURN)
    assert out[0] == {"role": "user", "content": "what is this?", "images": ["iVBORw0KGgo="]}
    assert out[1] == TEXT_TURN[0]


# --- fallback on failure or timeout --------------------------------------------------


class _FakeResponse:
    status_code = 200

    def __init__(self, text):
        self._text = text

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": self._text}}]}


class _FakeClient:
    def __init__(self, record):
        self._record = record

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self._record.append((url, json["model"]))
        return _FakeResponse("answer from the fallback")


@pytest.mark.parametrize("failure", [RuntimeError("llama-server HTTP 500"), TimeoutError("timed out")])
@pytest.mark.asyncio
async def test_a_failing_main_model_falls_back_to_the_fallback(monkeypatch, failure):
    import httpx

    from python.harness import server
    from python.inference import llama_server_manager as lsm

    monkeypatch.setenv("TALA_PROFILE", "kagura")
    attempts: list[str] = []
    posts: list[tuple[str, str]] = []

    async def no_rows():
        return []

    async def no_vram(provider, profile):
        attempts.append(provider["id"])

    async def failing_stream(*a, **k):
        raise failure
        yield ""  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(server, "load_inference_providers", no_rows)
    monkeypatch.setattr(server, "_vram_prepare_for", no_vram)
    monkeypatch.setattr(lsm, "check_model_cached", lambda: True)
    monkeypatch.setattr(lsm, "generate_stream", failing_stream)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(posts))

    result = await server._generate_reply(TEXT_TURN)

    assert result == "answer from the fallback"
    assert attempts == ["embedded-builtin", "profile-fallback"]  # main first, then fallback
    assert posts and posts[0][1] == profiles.get("kagura").fallback.model


@pytest.mark.asyncio
async def test_a_working_main_model_answers_without_touching_the_fallback(monkeypatch):
    import httpx

    from python.harness import server
    from python.inference import llama_server_manager as lsm

    monkeypatch.setenv("TALA_PROFILE", "kagura")
    posts: list[tuple[str, str]] = []

    async def no_rows():
        return []

    async def no_vram(provider, profile):
        return None

    async def good_stream(*a, **k):
        yield "main answer"

    monkeypatch.setattr(server, "load_inference_providers", no_rows)
    monkeypatch.setattr(server, "_vram_prepare_for", no_vram)
    monkeypatch.setattr(lsm, "check_model_cached", lambda: True)
    monkeypatch.setattr(lsm, "generate_stream", good_stream)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(posts))

    assert await server._generate_reply(TEXT_TURN) == "main answer"
    assert posts == []


def test_profile_summary_is_json_friendly(kagura):
    import json

    json.dumps(kagura.summary())
    assert isinstance(types.SimpleNamespace(**kagura.summary()), types.SimpleNamespace)


# --- vision on the CPU ------------------------------------------------------------------------------------------------------------------


def test_a_cpu_vision_role_tells_ollama_to_use_no_gpu_layers(kagura):
    cpu = dataclasses.replace(kagura, vision=dataclasses.replace(kagura.vision, gpu=False))
    vision = router._build_chain([], cpu, images=True)[0]
    assert vision["id"] == router.VISION_ID and vision["config"]["num_gpu"] == 0


def test_a_gpu_vision_role_sends_no_num_gpu(kagura):
    gpu = dataclasses.replace(kagura, vision=dataclasses.replace(kagura.vision, gpu=True))
    vision = router._build_chain([], gpu, images=True)[0]
    assert "num_gpu" not in vision["config"] or vision["config"]["num_gpu"] is None


def test_cpu_vision_does_not_change_the_order_of_the_chain(kagura):
    cpu = dataclasses.replace(kagura, vision=dataclasses.replace(kagura.vision, gpu=False))
    gpu = dataclasses.replace(kagura, vision=dataclasses.replace(kagura.vision, gpu=True))
    assert _ids(router._build_chain([], cpu, images=True)) == _ids(router._build_chain([], gpu, images=True))
    assert _ids(router._build_chain([], cpu, images=False)) == ["embedded-builtin", "profile-fallback"]


def test_a_text_turn_never_includes_the_vision_model_even_on_the_cpu(kagura):
    cpu = dataclasses.replace(kagura, vision=dataclasses.replace(kagura.vision, gpu=False))
    assert router.VISION_ID not in _ids(router._build_chain([], cpu, images=False))
