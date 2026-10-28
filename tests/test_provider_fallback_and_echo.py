"""
Three faults found when Steven said "Hey Tala, what time is it?" in the live app (2026-10-28), each of which turned a working voice
pipeline into a reply that repeated the system reminder aloud:

1. The preferred provider (gpt-oss through Ollama) returned empty text, again and again, and every turn still tried it first.
2. The fallback chain, in Ollama's own alphabetical order, ran embedding models and a 2B vision model ahead of the chat models, and
   the vision model was the one that answered.
3. That model echoed ``[SYSTEM REMINDER]: ...`` back, and it was shown and read aloud.
"""
from __future__ import annotations

import pytest

from python.harness import provider_selection as ps
from python.harness import reply_text
from python.harness.voice_chunker import SentenceChunker


def _local(name: str, model: str | None = None, priority: int = 5) -> dict:
    return {"id": name, "type": "local", "name": f"OLLAMA - {name}", "priority": priority, "status": "online",
            "config": {"engine": "ollama", "model": model or name}}


# --- 2. which providers a chat turn may fall back to -------------------------------------------------------------------

def test_an_embedding_model_is_never_in_the_chat_chain():
    route = ps.resolve_provider_chain([_local("embeddinggemma:latest"), _local("nomic-embed-text:latest"), _local("llama3.1:8b")])
    names = [p["id"] for p in route.providers]
    assert "llama3.1:8b" in names
    assert "embeddinggemma:latest" not in names and "nomic-embed-text:latest" not in names
    assert names[-1] == "embedded-builtin", "the built-in engine is not an embedding model"


def test_a_vision_model_falls_after_the_general_chat_models_but_before_the_embedded_engine():
    route = ps.resolve_provider_chain([
        _local("granite3.2-vision:2b", priority=1), _local("llama3.1:8b", priority=2), _local("gemma3:4b", priority=3),
    ])
    order = [p["id"] for p in route.providers]
    assert order.index("granite3.2-vision:2b") > order.index("llama3.1:8b")
    assert order.index("granite3.2-vision:2b") > order.index("gemma3:4b")
    assert order[-1] == "embedded-builtin"


def test_a_provider_the_user_named_is_still_tried_first_even_if_it_is_an_embedding_model():
    """Explicit choices are not second-guessed here; the policy is about what the chain adds on its own."""
    providers = [_local("nomic-embed-text:latest"), _local("llama3.1:8b")]
    route = ps.resolve_provider_chain(providers, request_provider_id="nomic-embed-text:latest")
    assert route.providers[0]["id"] == "nomic-embed-text:latest"


# --- 1. a provider that keeps answering with nothing -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_cooldown():
    ps._reset_empty_replies()
    yield
    ps._reset_empty_replies()


def test_two_empty_replies_put_a_provider_on_cooldown_and_it_is_skipped():
    provider = _local("gpt-oss-heretic:20b")
    assert ps.skip_provider_attempt(provider, None, None) is False
    ps.note_empty_reply(provider["id"], "empty", now=1000.0)
    assert ps.skip_provider_attempt(provider, None, None) is False, "one empty reply can be a fluke"
    ps.note_empty_reply(provider["id"], "empty", now=1010.0)
    assert ps._empty_reply_cooldown(provider["id"], now=1011.0) is True
    assert ps.skip_provider_attempt(provider, None, None, now=1011.0) is True


def test_the_cooldown_ends_and_old_empty_replies_do_not_count():
    ps.note_empty_reply("p", "empty", now=1000.0)
    ps.note_empty_reply("p", "empty", now=1010.0)
    assert ps._empty_reply_cooldown("p", now=1010.0 + ps.EMPTY_REPLY_COOLDOWN_S + 1) is False
    ps._reset_empty_replies()
    ps.note_empty_reply("q", "empty", now=1000.0)
    ps.note_empty_reply("q", "empty", now=1000.0 + ps.EMPTY_REPLY_COOLDOWN_S + 5)
    assert ps._empty_reply_cooldown("q", now=1000.0 + ps.EMPTY_REPLY_COOLDOWN_S + 6) is False


def test_a_cooling_provider_is_still_tried_when_it_is_the_one_asked_for():
    provider = _local("gpt-oss-heretic:20b")
    ps.note_empty_reply(provider["id"], "empty", now=1000.0)
    ps.note_empty_reply(provider["id"], "empty", now=1001.0)
    assert ps.skip_provider_attempt(provider, provider, None, now=1002.0) is False
    assert ps.skip_provider_attempt(provider, None, provider, now=1002.0) is False


# --- 3. a model reading its instructions back --------------------------------------------------------------------------

ECHO = ("[SYSTEM REMINDER]: If you decide to invoke a tool, call it directly through the tool interface. Do NOT write the call "
        "out as text or as a code block. If you do NOT need a tool (for example, if you are asking the user a question or "
        "answering them), simply respond with normal text.")


def test_an_echoed_reminder_is_removed_from_a_reply():
    assert reply_text._strip_stray_calls(f"It is 9 pm. {ECHO}") == "It is 9 pm."


def test_a_reply_that_is_only_repeated_reminders_becomes_empty():
    assert reply_text._strip_stray_calls(f"{ECHO} {ECHO} {ECHO} [SYSTEM REMINDER]: If you decide to invoke a tool, call it dire") == ""


def test_a_reminder_mentioned_in_code_is_left_alone():
    text = "The marker looks like `[SYSTEM REMINDER]: x` in the prompt."
    assert reply_text._strip_stray_calls(text) == text


def test_ordinary_text_with_brackets_is_untouched():
    text = "See [the docs](http://x) and [note 1]."
    assert reply_text._strip_stray_calls(text) == text


def _speak(fragments: list[str]) -> str:
    chunker = SentenceChunker()
    out: list[str] = []
    for fragment in fragments:
        out += chunker.feed(fragment)
    out += chunker.flush()
    return " ".join(out)


def test_an_echoed_reminder_is_never_spoken_even_split_across_fragments():
    fragments = ["It is nine ", "o'clock. [SYS", "TEM REMIN", "DER]: If you decide to invoke a tool, call it directly. ",
                 "Do NOT write the call out. If you do NOT need a tool, simply respond with normal text. ", "Anything else?"]
    spoken = _speak(fragments)
    assert "REMINDER" not in spoken.upper()
    assert "invoke a tool" not in spoken
    assert "nine o'clock" in spoken
    assert "Anything else" in spoken


def test_a_reminder_that_never_closes_is_dropped_to_the_end():
    spoken = _speak(["Sure. ", "[SYSTEM REMINDER]: If you decide to invoke a tool, call it dire"])
    assert spoken.strip() == "Sure."
