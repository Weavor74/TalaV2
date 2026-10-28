"""
What the user sees (and, in a voice turn, hears) of a model's reply.

2026-10-24, the real app: a reply ended with `<call><code><call name="update_user_model" focus="memory" max_turns="3"/></code></call>`
and began with "Session provider 'prov-...' was not found. Using the priority chain." Both were in the chat bubble and both were
read aloud. The first is the model writing a tool call in a shape the harness does not parse (so it was neither run nor removed);
the second is an internal notice that was glued onto the front of the text. Neither belongs in the message.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from python.harness import reply_text

LEAK = ('I am doing well, Weavor. My memory indicates I am running smoothly. '
        '<call><code><call name="update_user_model" focus="memory" max_turns="3"/></code></call>')


def test_the_exact_leak_from_the_chat_bubble_is_removed():
    assert reply_text.strip_stray_calls(LEAK) == "I am doing well, Weavor. My memory indicates I am running smoothly."


@pytest.mark.parametrize("text,expected", [
    ('Sure. <call name="x" a="1"/>', "Sure."),
    ('Sure. <call:read_file path="a.txt" />', "Sure."),
    ('Sure. <call:web_search query="q">some body</call:web_search>', "Sure."),
    ('Sure. <call>\n<code>\n<call name="x"/>\n</code>\n</call>\nDone.', "Sure.\nDone."),
    ("<call><code></code></call>Plain.", "Plain."),
])
def test_stray_call_markup_in_its_several_shapes_is_removed(text, expected):
    assert reply_text.strip_stray_calls(text) == expected


def test_an_unclosed_opening_tag_goes_but_the_words_after_it_stay():
    assert reply_text.strip_stray_calls('Hello <call name="x"> and then more words') == "Hello and then more words"


def test_ordinary_text_code_fences_and_other_tags_are_left_alone():
    text = "Here is code:\n```python\nprint('<b>hi</b>')\n```\nAnd a <b>bold</b> word; 3 < 5 and 7 > 2."
    assert reply_text.strip_stray_calls(text) == text


def test_markup_she_is_quoting_in_code_is_left_alone():
    quoted = """The tag looks like `<call:read_file path="a"/>` and in a block:
```
<call name="x"/>
```
That is all."""
    assert reply_text.strip_stray_calls(quoted) == quoted
    assert reply_text.strip_stray_calls(quoted + ' <call name="y"/>') == quoted


def test_a_reply_that_is_only_a_call_becomes_empty_not_garbage():
    assert reply_text.strip_stray_calls('<call name="x"/>') == ""


def test_stripping_twice_changes_nothing():
    once = reply_text.strip_stray_calls(LEAK)
    assert reply_text.strip_stray_calls(once) == once


def test_the_handler_no_longer_glues_the_notice_into_the_reply_text():
    src = (Path(__file__).resolve().parents[1] / "python" / "harness" / "server.py").read_text(encoding="utf-8")
    assert 'generated_text = f"{fallback_notice}' not in src, "an internal notice must not be part of what she says"
    assert "_reply_text.finish_reply(generated_text" in src  # strip_stray_calls plus the unbacked-claim correction
    assert '"fallback_notice": fallback_notice,' in src, "the notice still travels beside the reply for a UI that wants it"
