"""
Pre-captioning: the vision model reads the image, the main model answers.

Measured on Kagura: an image turn used to hand gemma3:4b Tala's whole assembled prompt (8,807 tokens against its 8,192-token
window). It rejected it (HTTP 400), the request fell back to the main model -- which meant stopping Ollama's model, restarting
llama-server (53 s) and answering without ever seeing the picture. So the vision model is now asked one small question (the
image and what the user asked) and its description goes into the main model's prompt. The vision call runs on the CPU when the
profile says so, so it takes no VRAM and the main model is never stopped, and a failure of any kind leaves the turn exactly as
it was before: nothing here may raise.
"""
from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import httpx
import pytest

from python.inference import profiles, vision

REPO = Path(__file__).resolve().parents[1]
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("TALA_PROFILE", "TALA_PROFILES_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))


@pytest.fixture
def kagura():
    return profiles.get("kagura")


def run(coro):
    return asyncio.run(coro)


class FakeOllama:
    """Stands in for httpx.AsyncClient: records the request, answers with ``reply`` or raises ``fail``."""

    def __init__(self, reply=None, status=200, fail=None):
        self.reply, self.status, self.fail = reply, status, fail
        self.calls: list[dict] = []

    def client(self):
        outer = self

        class _Client:
            def __init__(self, *a, **k):
                outer.timeout = k.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, **k):
                outer.calls.append({"url": url, "json": json})
                if outer.fail:
                    raise outer.fail
                return type("R", (), {"status_code": outer.status, "text": "boom", "json": lambda s: outer.reply})()

        return _Client


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama(reply={"message": {"content": "A red circle on a cream background, with the text TALA 42."}})
    monkeypatch.setattr(httpx, "AsyncClient", fake.client())

    async def no_room_needed(provider, profile):
        fake.prepared = provider
        return []

    fake.prepared = None
    monkeypatch.setattr("python.inference.vram_arbiter.prepare_for", no_room_needed)
    return fake


def cpu(kagura):
    return dataclasses.replace(kagura, vision=dataclasses.replace(kagura.vision, gpu=False))


def gpu(kagura):
    return dataclasses.replace(kagura, vision=dataclasses.replace(kagura.vision, gpu=True))


# --- when it applies -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("images", [None, [], "not a list", [""], [None], [7]])
def test_no_usable_image_means_no_caption_and_no_request(ollama, kagura, images):
    assert run(vision.caption_images(images, "what is this?", kagura)) is None
    assert ollama.calls == []


def test_a_profile_without_a_vision_model_gets_no_caption(ollama, kagura):
    none = dataclasses.replace(kagura, vision=None)
    assert run(vision.caption_images([PNG], "what is this?", none)) is None
    assert run(vision.caption_images([PNG], "what is this?", None)) is None
    assert ollama.calls == []


# --- the request ------------------------------------------------------------------------------------------------------------------


def test_the_vision_model_is_asked_a_small_question_not_given_talas_whole_prompt(ollama, kagura):
    run(vision.caption_images([PNG], "What number is written in this image?", cpu(kagura)))
    call = ollama.calls[0]
    assert call["url"] == "http://localhost:11434/api/chat"
    body = call["json"]
    assert body["model"] == "gemma3:4b" and body["stream"] is False
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    assert "What number is written in this image?" in body["messages"][1]["content"]
    assert len(body["messages"][0]["content"]) + len(body["messages"][1]["content"]) < 1500, "a small prompt"
    assert body["options"]["num_ctx"] <= 4096, "its window need only hold the image and one question"


def test_the_image_goes_as_bare_base64_in_ollamas_own_field(ollama, kagura):
    run(vision.caption_images([f"data:image/png;base64,{PNG}", PNG], "?", cpu(kagura)))
    assert ollama.calls[0]["json"]["messages"][1]["images"] == [PNG, PNG]


def test_a_cpu_role_asks_ollama_for_no_gpu_layers_and_keeps_the_model_resident(ollama, kagura):
    run(vision.caption_images([PNG], "?", cpu(kagura)))
    body = ollama.calls[0]["json"]
    assert body["options"]["num_gpu"] == 0 and body["keep_alive"] == "30m"


def test_a_gpu_role_sends_no_num_gpu(ollama, kagura):
    run(vision.caption_images([PNG], "?", gpu(kagura)))
    assert "num_gpu" not in ollama.calls[0]["json"]["options"]


def test_room_is_made_through_the_arbiter_as_a_cpu_model_for_cpu_vision(ollama, kagura):
    run(vision.caption_images([PNG], "?", cpu(kagura)))
    assert ollama.prepared["config"]["num_gpu"] == 0 and ollama.prepared["config"]["model"] == "gemma3:4b"


def test_the_gpu_role_still_makes_room_the_old_way(ollama, kagura):
    run(vision.caption_images([PNG], "?", gpu(kagura)))
    assert "num_gpu" not in ollama.prepared["config"]


def test_at_most_four_images_are_sent_and_a_long_question_is_cut(ollama, kagura):
    run(vision.caption_images([PNG] * 9, "x" * 5000, cpu(kagura)))
    message = ollama.calls[0]["json"]["messages"][1]
    assert len(message["images"]) == 4 and len(message["content"]) < 800


def test_an_empty_question_still_asks_for_a_description(ollama, kagura):
    run(vision.caption_images([PNG], "   ", cpu(kagura)))
    assert "describe" in ollama.calls[0]["json"]["messages"][1]["content"].lower()


def test_it_talks_only_to_the_profiles_own_local_ollama(ollama, kagura):
    run(vision.caption_images([PNG], "?", cpu(kagura)))
    assert ollama.calls[0]["url"].startswith(kagura.ollama_url)
    assert ollama.calls[0]["url"].startswith("http://localhost")


# --- the result ---------------------------------------------------------------------------------------------------------------------


def test_the_description_comes_back_as_text_for_the_main_models_prompt(ollama, kagura):
    text = run(vision.caption_images([PNG], "?", cpu(kagura)))
    assert "TALA 42" in text and "vision model" in text and "1 image" in text


def test_two_images_say_so(ollama, kagura):
    assert "2 images" in run(vision.caption_images([PNG, PNG], "?", cpu(kagura)))


@pytest.mark.parametrize("reply", [{}, {"message": {}}, {"message": {"content": "   "}}, {"message": {"content": None}}, "text", None])
def test_an_empty_or_malformed_answer_gives_no_caption(ollama, kagura, reply):
    ollama.reply = reply
    assert run(vision.caption_images([PNG], "?", cpu(kagura))) is None


@pytest.mark.parametrize("fail", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), RuntimeError("boom"), asyncio.TimeoutError()])
def test_a_failure_of_any_kind_leaves_the_turn_as_it_was(ollama, kagura, fail):
    ollama.fail = fail
    assert run(vision.caption_images([PNG], "?", cpu(kagura))) is None


def test_an_http_error_gives_no_caption(ollama, kagura):
    ollama.status = 500
    assert run(vision.caption_images([PNG], "?", cpu(kagura))) is None


def test_a_failure_to_make_room_does_not_stop_the_attempt(monkeypatch, ollama, kagura):
    async def broken(provider, profile):
        raise RuntimeError("arbiter down")

    monkeypatch.setattr("python.inference.vram_arbiter.prepare_for", broken)
    assert "TALA 42" in run(vision.caption_images([PNG], "?", cpu(kagura)))


def test_the_description_is_length_limited(ollama, kagura):
    ollama.reply = {"message": {"content": "word " * 5000}}
    assert len(run(vision.caption_images([PNG], "?", cpu(kagura)))) < 3000


def test_a_cpu_caption_is_given_time_to_finish(ollama, kagura):
    run(vision.caption_images([PNG], "?", cpu(kagura)))
    assert ollama.timeout >= 120


def test_the_caption_does_not_overwrite_the_turns_speed_readout():
    """The status bar reports the main model's reply; a vision pass must not stand in for it."""
    assert "latency" not in (REPO / "python" / "inference" / "vision.py").read_text(encoding="utf-8")


# --- wired in ---------------------------------------------------------------------------------------------------------------------------


def server_source():
    return (REPO / "python" / "harness" / "server.py").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_generate_handler_captions_before_it_builds_the_message():
    src = server_source()
    at = src.index("_vision.caption_images(images, raw_user_prompt")
    assert at < src.index("if images and isinstance(images, list):", at) and src.count("_vision.caption_images(") == 1


def test_a_successful_caption_turns_the_turn_into_a_plain_text_one():
    src = server_source()
    block = src[src.index("_vision.caption_images(images, raw_user_prompt"):][:400]
    assert "prompt, images = " in block and "[]" in block


def test_the_main_model_is_what_answers_a_captioned_turn():
    """With the image consumed the message is plain text, so the router builds the text chain (main, then fallback)."""
    from python.inference import router

    chain = router._build_chain([], profiles.get("kagura"), images=False)
    assert [p["id"] for p in chain] == ["embedded-builtin", "profile-fallback"]


def test_the_server_still_compiles_with_the_hook_in():
    """The wiring tests above read the file as text, so a broken edit to it would slip past them."""
    import ast

    ast.parse(server_source())


def test_the_description_is_appended_to_the_prompt_after_a_blank_line():
    src = server_source()
    block = src[src.index("_vision.caption_images(images, raw_user_prompt"):][:400]
    assert 'f"{prompt}' + chr(92) + "n" + chr(92) + 'n{_caption}"' in block


def test_how_long_the_description_took_is_logged_with_where_it_ran(ollama, kagura, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="python.inference.vision"):
        run(vision.caption_images([PNG], "?", cpu(kagura)))
        run(vision.caption_images([PNG], "?", gpu(kagura)))
    lines = [r.getMessage() for r in caplog.records if "described" in r.getMessage()]
    assert any("gemma3:4b" in ln and "1 image" in ln and " CPU" in ln for ln in lines)
    assert any(" GPU" in ln for ln in lines)
