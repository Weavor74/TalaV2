"""
Per-turn latency: what the status bar shows after a reply (tokens per second and time to first token).

Each provider already knows how fast it was: llama-server sends ``timings`` with its reply (and the streamed path measures
the wall-clock time to the first token), Ollama's native API returns ``eval_count`` / ``eval_duration`` and the prompt-eval
and load durations. This module turns either into one small ``timing`` dict and carries it from the provider layer to the reply
payload in a context variable, so the many provider branches in the generate handler need no plumbing of their own. A turn
that no provider reported on has no timing: nothing is invented.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from python.inference import latency

REPO = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


# --- llama-server ---------------------------------------------------------------------------------------------------------------


LLAMA = {"prompt_n": 8000, "prompt_ms": 13_000.0, "prompt_per_second": 615.4, "predicted_n": 64, "predicted_ms": 3265.0,
         "predicted_per_second": 19.6}


def test_llama_timings_become_tokens_per_second_and_prompt_speed():
    t = latency.from_llama_timings(LLAMA, ttft_s=13.4)
    assert t["tokens_per_second"] == 19.6 and t["prompt_per_second"] == 615.4
    assert t["generated_tokens"] == 64 and t["prompt_tokens"] == 8000 and t["source"] == "llama-server"


def test_a_measured_time_to_first_token_is_preferred():
    assert latency.from_llama_timings(LLAMA, ttft_s=13.4)["ttft_ms"] == 13400


def test_without_a_measured_one_it_is_prompt_processing_plus_one_token():
    t = latency.from_llama_timings(LLAMA, ttft_s=None)
    assert t["ttft_ms"] == pytest.approx(13_000.0 + 1000 / 19.6, abs=1)


def test_timings_missing_the_rate_give_nothing_rather_than_zero():
    assert latency.from_llama_timings({"prompt_n": 5}, ttft_s=1.0) is None
    assert latency.from_llama_timings({}, ttft_s=1.0) is None
    assert latency.from_llama_timings(None, ttft_s=1.0) is None


# --- Ollama ----------------------------------------------------------------------------------------------------------------------


OLLAMA = {"model": "gemma3:4b", "done": True, "eval_count": 100, "eval_duration": 20_000_000_000,
          "prompt_eval_count": 1500, "prompt_eval_duration": 3_000_000_000, "load_duration": 1_500_000_000}


def test_ollama_durations_become_rates_in_seconds():
    t = latency._from_ollama(OLLAMA)
    assert t["tokens_per_second"] == pytest.approx(5.0) and t["generated_tokens"] == 100
    assert t["prompt_per_second"] == pytest.approx(500.0) and t["prompt_tokens"] == 1500
    assert t["source"] == "ollama" and t["model"] == "gemma3:4b"


def test_ollama_time_to_first_token_includes_loading_the_model():
    assert latency._from_ollama(OLLAMA)["ttft_ms"] == pytest.approx(1500 + 3000 + 200, abs=1)


@pytest.mark.parametrize("response", [{}, {"done": True}, {"eval_count": 0, "eval_duration": 0}, {"eval_count": 5}, None, "text"])
def test_an_ollama_reply_without_usable_numbers_gives_nothing(response):
    assert latency._from_ollama(response) is None


def test_an_ollama_reply_with_no_prompt_figures_still_reports_the_rate():
    t = latency._from_ollama({"eval_count": 50, "eval_duration": 5_000_000_000})
    assert t["tokens_per_second"] == pytest.approx(10.0) and t["prompt_per_second"] is None


# --- carrying it to the reply ------------------------------------------------------------------------------------------------------


def test_nothing_recorded_means_no_timing():
    async def go():
        return latency.take()

    assert run(go()) is None


def test_a_recorded_timing_is_taken_once_by_the_request_that_recorded_it():
    async def go():
        latency.record(source="llama-server", tokens_per_second=19.6, ttft_ms=2100.0)
        first = latency.take()
        return first, latency.take()

    first, second = run(go())
    assert first["tokens_per_second"] == 19.6 and second is None


def test_requests_do_not_see_each_others_timings():
    async def one():
        latency.record(source="a", tokens_per_second=1.0)
        await asyncio.sleep(0.02)
        return latency.take()

    async def other():
        await asyncio.sleep(0.01)
        return latency.take()

    async def go():
        return await asyncio.gather(asyncio.ensure_future(one()), asyncio.ensure_future(other()))

    mine, theirs = run(go())
    assert mine["tokens_per_second"] == 1.0 and theirs is None


def test_the_last_generation_of_a_turn_wins():
    async def go():
        latency.record(source="llama-server", tokens_per_second=10.0)
        latency.record(source="llama-server", tokens_per_second=20.0)
        return latency.take()

    assert run(go())["tokens_per_second"] == 20.0


def test_values_are_rounded_for_display_and_unknowns_stay_none():
    async def go():
        latency.record(source="ollama", tokens_per_second=19.63456, ttft_ms=2101.987)
        return latency.take()

    t = run(go())
    assert t["tokens_per_second"] == 19.6 and t["ttft_ms"] == 2102
    assert t["prompt_per_second"] is None and t["generated_tokens"] is None


def test_note_ollama_records_only_what_it_can_read():
    async def go():
        latency.note_ollama({"nothing": "useful"})
        empty = latency.take()
        latency.note_ollama(OLLAMA)
        return empty, latency.take()

    empty, got = run(go())
    assert empty is None and got["source"] == "ollama"


# --- the hooks are really wired -----------------------------------------------------------------------------------------------------


def server_source():
    return (REPO / "python" / "harness" / "server.py").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_reply_carries_the_timing():
    src = server_source()
    tail = src[src.index('"fallback_notice": fallback_notice,'):][:400]
    assert '"timing": _latency.take()' in tail


def test_ollama_replies_are_noted_where_they_are_parsed():
    src = server_source()
    at = src.index("resp_json = response.json()")
    assert "_latency.note_ollama(resp_json" in src[at:at + 200]


def test_the_embedded_engine_reports_its_own_timing():
    manager = (REPO / "python" / "inference" / "llama_server_manager.py").read_text(encoding="utf-8")
    assert "latency.record(" in manager and "from_llama_timings" in manager


def test_the_status_bar_has_a_place_for_it():
    html = (REPO / "renderer" / "index.html").read_text(encoding="utf-8")
    footer = html[html.index('<footer id="status-bar"'):html.index("</footer>", html.index('<footer id="status-bar"'))]
    assert re.search(r'id="status-latency"', footer)
