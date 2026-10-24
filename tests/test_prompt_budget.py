"""
Prompt budget: what a turn carries into the model, and what it should not.

A measured default prompt was ~25k tokens at the token gate, of which the model itself received 5-7k; the rest of the gap
was tool documentation swapped out before the request is sent. What genuinely varied was uncapped: one retrieved node could
be 4,500 tokens, the settings snapshot rode along on every voice turn, and ten turns of history went into a spoken reply.
These tests pin the caps and, as importantly, the things that must NOT be trimmed (roleplay memory, a settings question).
"""
from __future__ import annotations

import logging

import pytest

from python.harness import prompt_budget as pb


# --- per-node cap -------------------------------------------------------------------------------------------------


def test_a_short_node_is_untouched():
    text = "Tala's cat is called Zorblax."
    assert pb.cap_memory(text) == text


def test_a_huge_node_is_cut_to_the_cap_and_says_so():
    text = "The harness keeps a conversation in memory. " * 2000  # ~18k tokens
    capped = pb.cap_memory(text)
    assert len(capped) < len(text) / 4
    assert capped.startswith("The harness keeps")
    assert "truncated" in capped.rsplit("\n", 1)[-1]


def test_the_cut_falls_on_a_line_or_word_boundary_not_mid_word():
    text = ("alpha beta gamma delta " * 1000).strip()
    capped = pb.cap_memory(text)
    body = capped.rsplit("\n", 1)[0]
    assert body.split()[-1] in {"alpha", "beta", "gamma", "delta"}


def test_a_text_exactly_at_the_cap_is_not_marked_truncated():
    text = "x" * pb.NODE_MEMORY_CHARS
    assert pb.cap_memory(text) == text


# --- settings snapshot on voice turns -----------------------------------------------------------------------------

ALL_SETTINGS = ("settings-system", "settings-inference", "settings-runtime", "settings-agent")


def test_a_text_turn_always_gets_the_whole_snapshot():
    assert pb.settings_node_ids("hello there", voice=False) == ALL_SETTINGS


def test_a_plain_voice_turn_skips_the_snapshot():
    assert pb.settings_node_ids("what's a good name for a cat", voice=True) == ()


@pytest.mark.parametrize("said", [
    "what model are you running", "which provider are you using", "how much VRAM do you have",
    "show me your settings", "what's your context size", "are you on the GPU", "what version are you",
])
def test_a_voice_turn_that_asks_about_the_system_keeps_the_snapshot(said):
    assert pb.settings_node_ids(said, voice=True) == ALL_SETTINGS


def test_voice_is_read_from_the_turn_id():
    assert pb.is_voice_turn({"voice_turn_id": "t-1"}) is True
    assert pb.is_voice_turn({"voice_turn_id": ""}) is False
    assert pb.is_voice_turn({}) is False


# --- history ------------------------------------------------------------------------------------------------------


def _turns(n, size=50):
    return [{"role": "user" if i % 2 == 0 else "agent", "content": f"turn {i} " + "w" * size} for i in range(n)]


def test_text_history_is_unchanged_ten_turns_and_older_big_ones_cut_at_4000():
    history = _turns(12)
    history[5]["content"] = "z" * 9000  # inside the last ten, not the newest
    out = pb.history_messages(history, voice=False)
    assert len(out) == 10
    assert out[0]["content"].startswith("turn 2")
    big = next(m for m in out if m["content"].startswith("z"))
    assert len(big["content"]) < 4100 and "truncated" in big["content"]


def test_the_newest_history_turn_is_never_cut():
    history = _turns(3)
    history[-1]["content"] = "n" * 9000
    assert len(pb.history_messages(history, voice=False)[-1]["content"]) == 9000
    assert len(pb.history_messages(history, voice=True)[-1]["content"]) == 9000


def test_voice_history_is_the_last_six_with_shorter_older_turns():
    history = _turns(12)
    history[7]["content"] = "v" * 5000
    out = pb.history_messages(history, voice=True)
    assert len(out) == 6
    assert out[0]["content"].startswith("turn 6")
    older = next(m for m in out if m["content"].startswith("v"))
    assert len(older["content"]) <= pb.VOICE_OLDER_TURN_CHARS + 80


def test_roles_are_mapped_and_empty_turns_dropped():
    out = pb.history_messages([{"role": "agent", "content": " hi "}, {"role": "user", "content": "   "}, {"role": "user", "content": "yo"}], voice=False)
    assert out == [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "yo"}]


def test_a_non_list_history_is_ignored():
    assert pb.history_messages(None, voice=True) == []
    assert pb.history_messages("nope", voice=False) == []


# --- the gate's number against what the engine processed ----------------------------------------------------------


def test_the_engines_count_is_attached_beside_the_gate_estimate(caplog):
    timing = {"source": "llama-server", "prompt_tokens": 6100}
    with caplog.at_level(logging.INFO, logger=pb.logger.name):
        out = pb.reconcile(25013, timing, "s1")
    assert out["gate_estimate_tokens"] == 25013 and out["prompt_tokens"] == 6100
    assert "25013" in caplog.text and "6100" in caplog.text


def test_no_timing_means_nothing_to_reconcile():
    assert pb.reconcile(100, None, "s1") is None
    assert pb.reconcile(100, {"prompt_tokens": None}, "s1") == {"prompt_tokens": None, "gate_estimate_tokens": 100}
