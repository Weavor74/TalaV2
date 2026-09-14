"""
Text-to-speech reads the answer, not the thinking.

The harness normalises every model's chain-of-thought into
``<thinking>...</thinking>`` so downstream consumers can strip it, and
``appendMessage`` does: reasoning goes to the collapsible "Thought Process"
section, the reply goes in the bubble. ``speakText`` was the one consumer that
skipped the split, so with TTS enabled Tala read her deliberation aloud --
often at length, before answering, and sometimes instead of answering.

Asserted against the source because renderer.js makes top-level DOM and
Electron-IPC calls and cannot be imported outside a browser context, which is
the same reason parseThinkingBlocks lives in its own file. The parser's own
behaviour is covered by tests/renderer/thinking_blocks.test.js; what is pinned
here is that the speech path actually uses it.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "renderer" / "renderer.js"


def _speak_text_body() -> str:
    source = RENDERER.read_text(encoding="utf-8")
    start = source.index("async speakText(")
    # Up to the next sibling method at the same indentation.
    rest = source[start:]
    end = re.search(r"\n  \},\n", rest)
    assert end, "could not find the end of speakText()"
    return rest[: end.end()]


def test_speak_text_strips_thinking_before_synthesis():
    body = _speak_text_body()
    assert "parseThinkingBlocks" in body, (
        "speakText does not split thinking out of the text it sends to "
        "voice.speak, so TTS will read the model's reasoning aloud"
    )
    strip_at = body.index("parseThinkingBlocks")
    rpc_at = body.index("voice.speak")
    assert strip_at < rpc_at, "the split must happen before the text is synthesised"


def test_the_raw_text_is_not_what_gets_synthesised():
    """The strip is worthless if the original string is still the payload."""
    body = _speak_text_body()
    payload = re.search(r"params:\s*\{\s*text:\s*(\w+)\s*\}", body)
    assert payload, "could not find the voice.speak payload"
    assert payload.group(1) != "text", (
        "voice.speak is still sent the unsplit argument; send the stripped "
        "value instead"
    )


def test_a_reasoning_only_turn_says_nothing():
    """Falling back to the raw text when the split leaves nothing would
    reinstate the bug for exactly the turns that suffer it worst."""
    body = _speak_text_body()
    assert re.search(r"if\s*\(\s*!\s*\w+\s*\)\s*return;", body), (
        "speakText should return without speaking when the stripped text is "
        "empty, rather than speaking the reasoning"
    )
