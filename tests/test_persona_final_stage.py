"""
The persona as the FINAL stage of a turn ("Jarvis had a personality, but was AI when being an assistant"): the main model does the tool
loop and the facts, then the trained persona adapter says the finished reply in her own voice. The risk is the rephrase losing or
inventing a fact, so a guard compares what the reply states before and after and, on any doubt, the original reply is used.
Nothing here starts a model: the persona's completion is a stub.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from python.harness import server
from python.harness.voice_stream import TapWs, tap_ws
from python.inference import persona_adapter as pa
from python.inference import persona_voice as pv
from python.inference import router

FACTS = "I ran `where docker` and it found C:\\Program Files\\Docker\\docker.exe, version 27.3.1, listening on port 2375."


@pytest.fixture(autouse=True)
def _world(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    return tmp_path


def _active(monkeypatch, mode="final_stage"):
    monkeypatch.setattr(pa, "routing_mode", lambda: mode)
    monkeypatch.setattr(pa, "provider", lambda: {"id": pa.PROVIDER_ID, "type": "local", "config": {"engine": "persona", "endpoint": "http://127.0.0.1:8090/v1"}})


def _stub(monkeypatch, reply, seen=None):
    async def complete(provider, messages):
        if seen is not None:
            seen.append(messages)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def prepare(provider, profile):
        if seen is not None:
            seen.append("prepared")
        return []

    monkeypatch.setattr(pv, "_complete", complete)
    monkeypatch.setattr(pv, "_prepare", prepare)


def run(text, user="is docker installed?", messages=None):
    return asyncio.run(pv.voice_final(text, messages or [{"role": "system", "content": "You are Tala."}], user_text=user))


# --- the mode -------------------------------------------------------------------------------------------------------------------


def test_final_stage_is_a_routing_mode_and_off_stays_the_default():
    assert "final_stage" in pa.ROUTING_MODES and "off" in pa.ROUTING_MODES


def test_the_final_stage_never_leads_or_trails_the_provider_chain():
    persona = {"id": pa.PROVIDER_ID}
    chain = [{"id": "main"}]

    class Route:
        providers, request_provider, forced_provider, strict = chain, "", "", False

    profile = object()
    import python.inference.router as r

    r_build = r._build_chain
    r._build_chain = lambda providers, profile, images: providers
    try:
        out = r.apply(Route, profile, [{"role": "user", "content": "hi"}], persona=persona, persona_mode="final_stage", in_character=True, has_tools=False)
    finally:
        r._build_chain = r_build
    assert out == chain  # the persona voices the finished reply; it is not an alternative way to answer


# --- gating: nothing happens unless it is on and an adapter is active -----------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "fallback", "in_character"])
def test_other_modes_leave_the_reply_alone_and_never_start_the_persona(monkeypatch, mode):
    seen = []
    _active(monkeypatch, mode)
    _stub(monkeypatch, "REWRITTEN", seen)
    assert run(FACTS) == FACTS and seen == []


def test_no_active_adapter_means_no_change(monkeypatch):
    monkeypatch.setattr(pa, "routing_mode", lambda: "final_stage")
    monkeypatch.setattr(pa, "provider", lambda: None)
    seen = []
    _stub(monkeypatch, "REWRITTEN", seen)
    assert run(FACTS) == FACTS and seen == []


def test_the_gpu_is_made_ready_before_the_persona_is_asked(monkeypatch):
    seen = []
    _active(monkeypatch)
    _stub(monkeypatch, FACTS + " Easy.", seen)
    run(FACTS)
    assert seen[0] == "prepared" and isinstance(seen[1], list)


# --- the fact guard -------------------------------------------------------------------------------------------------------------


def test_a_voiced_reply_that_keeps_every_fact_is_used(monkeypatch):
    voiced = "Right, I checked: `where docker` turned up C:\\Program Files\\Docker\\docker.exe. Version 27.3.1, port 2375. All yours."
    _active(monkeypatch)
    _stub(monkeypatch, voiced)
    assert run(FACTS) == voiced


@pytest.mark.parametrize("dropped", ["27.3.1", "2375", "C:\\Program Files\\Docker\\docker.exe", "`where docker`"])
def test_losing_any_fact_falls_back_to_the_original(monkeypatch, dropped):
    voiced = "Right, I checked: `where docker` turned up C:\\Program Files\\Docker\\docker.exe. Version 27.3.1, port 2375. All yours.".replace(dropped, "it")
    _active(monkeypatch)
    _stub(monkeypatch, voiced)
    assert run(FACTS) == FACTS


def test_a_changed_number_is_a_lost_fact(monkeypatch):
    _active(monkeypatch)
    _stub(monkeypatch, FACTS.replace("27.3.1", "27.3.2"))
    assert run(FACTS) == FACTS


def test_a_new_number_the_original_never_stated_is_refused(monkeypatch):
    _active(monkeypatch)
    _stub(monkeypatch, FACTS + " That is 42 percent faster.")
    assert run(FACTS) == FACTS


def test_tool_markup_or_an_echoed_instruction_in_the_voiced_text_is_refused(monkeypatch):
    _active(monkeypatch)
    for bad in (FACTS + " <call:run_shell/>", FACTS + " (Rewrite the reply below in your own voice.)", "   ", ""):
        _stub(monkeypatch, bad)
        assert run(FACTS) == FACTS


def test_a_rewrite_that_balloons_is_refused(monkeypatch):
    _active(monkeypatch)
    _stub(monkeypatch, FACTS + " " + "Honestly, " * 200)
    assert run(FACTS) == FACTS


# --- fail open ------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("error", [RuntimeError("no adapter"), asyncio.TimeoutError(), OSError("refused"), ValueError("bad json")])
def test_any_failure_returns_the_original_reply(monkeypatch, error):
    _active(monkeypatch)
    _stub(monkeypatch, error)
    assert run(FACTS) == FACTS


def test_a_failure_to_free_the_gpu_returns_the_original_reply(monkeypatch):
    _active(monkeypatch)

    async def prepare(provider, profile):
        raise RuntimeError("vram")

    _stub(monkeypatch, "x")
    monkeypatch.setattr(pv, "_prepare", prepare)
    assert run(FACTS) == FACTS


def test_a_halted_harness_never_swaps_models_for_flavour(monkeypatch):
    seen = []
    _active(monkeypatch)
    _stub(monkeypatch, FACTS, seen)
    monkeypatch.setattr(pv, "_halted", lambda: True)
    assert run(FACTS) == FACTS and seen == []


# --- what is left verbatim ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "```python\nprint('hi')\n```",
    "",
    "x" * (pv.MAX_CHARS + 1),
    "<thinking>unfinished",
])
def test_code_empty_overlong_and_unfinished_replies_are_never_sent_to_the_persona(monkeypatch, text):
    seen = []
    _active(monkeypatch)
    _stub(monkeypatch, "REWRITTEN", seen)
    assert run(text) == text and seen == []


def test_thinking_stays_exactly_as_it_was_and_only_the_answer_is_voiced(monkeypatch):
    seen = []
    voiced = "Right, `where docker` found C:\\Program Files\\Docker\\docker.exe, version 27.3.1, port 2375."
    _active(monkeypatch)
    _stub(monkeypatch, voiced, seen)
    out = run("<thinking>he wants a straight answer</thinking>\n" + FACTS)
    assert out.startswith("<thinking>he wants a straight answer</thinking>") and out.endswith(voiced)
    assert "he wants a straight answer" not in str(seen[1])


def test_the_persona_is_shown_the_question_and_the_finished_reply_with_an_instruction_to_keep_the_facts(monkeypatch):
    seen = []
    _active(monkeypatch)
    _stub(monkeypatch, FACTS, seen)
    run(FACTS, user="is docker installed?")
    prompt = " ".join(str(m.get("content")) for m in seen[1])
    assert "is docker installed?" in prompt and FACTS in prompt and re.search(r"exactly as written", prompt)


# --- the fact extractor, on its own ---------------------------------------------------------------------------------------------


def test_facts_cover_numbers_paths_urls_commands_and_file_names():
    found = pv.facts("See https://example.com/a?b=1, run `git status` in D:\\src\\x, edit /etc/hosts or notes.txt: 3 files, 12.5%.")
    for expected in ("https://example.com/a?b=1", "`git status`", "D:\\src\\x", "/etc/hosts", "notes.txt", "3", "12.5"):
        assert expected.lower() in found


def test_a_plain_sentence_has_no_facts_to_lose():
    assert pv.facts("Sure, happy to help with that.") == set()


# --- the voice path -------------------------------------------------------------------------------------------------------------


class _Stream:
    def __init__(self):
        self.fed = []

    def feed_text(self, text):
        self.fed.append(text)

    def flush_text(self):
        pass

    cancelled = False


def test_a_tap_that_does_not_speak_forwards_chunks_but_feeds_nothing():
    sent = []

    class Ws:
        async def send(self, message, *a, **k):
            sent.append(message)

    stream = _Stream()
    tap = TapWs(Ws(), stream, speak=False)
    asyncio.run(tap.send('{"method":"inference.generate.chunk","params":{"chunk":"hello."}}'))
    assert sent and stream.fed == []


def test_a_voice_turn_is_not_streamed_while_the_persona_will_voice_it(monkeypatch):
    from python.harness import voice_runtime

    stream = _Stream()
    runtime = type("R", (), {"stream_for": lambda self, turn_id: stream})()
    monkeypatch.setattr(voice_runtime, "peek", lambda: runtime)
    _active(monkeypatch)
    tapped = tap_ws(object(), {"voice_turn_id": "t1"})
    assert isinstance(tapped, TapWs) and tapped._speak is False


def test_a_voice_turn_streams_as_before_when_the_mode_is_off(monkeypatch):
    from python.harness import voice_runtime

    runtime = type("R", (), {"stream_for": lambda self, turn_id: _Stream()})()
    monkeypatch.setattr(voice_runtime, "peek", lambda: runtime)
    _active(monkeypatch, "off")
    assert tap_ws(object(), {"voice_turn_id": "t1"})._speak is True


# --- wiring ---------------------------------------------------------------------------------------------------------------------


def test_the_router_exposes_the_final_stage_and_the_chat_handler_uses_it_before_guardrails_validate():
    assert inspect.iscoroutinefunction(router.voice_final)
    src = inspect.getsource(server.handle_inference_generate)
    voiced, validated = src.index("_router.voice_final("), src.index("guardrails_mgr.validate_output(")
    assert abs(voiced - validated) < 200 and "raw_user_prompt" in src[voiced:voiced + 200]


def test_the_ui_offers_the_final_stage_and_the_docs_describe_it():
    root = Path(__file__).resolve().parents[1]
    assert "final_stage" in (root / "renderer" / "lora_review.js").read_text(encoding="utf-8")
    assert "final_stage" in (root / "docs" / "lora-pipeline.md").read_text(encoding="utf-8")
