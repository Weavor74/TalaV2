"""
Sentence chunking for streamed speech: speak the first sentence while the rest is still being written.

Text arrives from the model in arbitrary fragments. The chunker emits a sentence as soon as it is complete, never speaks
the model's reasoning (``<thinking>`` blocks, which can arrive split across fragments) or a tool-call tag, does not break
on "Dr." or "5.30" or "a.m.", reads markdown as plain words, and makes sure a long unpunctuated run still starts being
spoken.
"""
from __future__ import annotations

import pytest

from python.harness.voice_chunker import SentenceChunker


def speak(fragments, **kw):
    c = SentenceChunker(**kw)
    out = []
    for f in fragments:
        out += c.feed(f)
    return out + c.flush()


def test_the_first_sentence_is_ready_before_the_reply_is_finished():
    c = SentenceChunker()
    assert c.feed("The time is five thirty. ") == ["The time is five thirty."]
    assert c.feed("It is a lovely") == []
    assert c.flush() == ["It is a lovely"]


def test_fragments_that_split_a_sentence_are_joined():
    assert speak(["It is ", "five thir", "ty in the after", "noon. Anything else?"]) == [
        "It is five thirty in the afternoon.", "Anything else?"]


def test_a_decimal_or_a_time_is_not_a_sentence_end():
    assert speak(["It is 5.30 in the afternoon. Pi is 3.14159, roughly."]) == [
        "It is 5.30 in the afternoon.", "Pi is 3.14159, roughly."]


@pytest.mark.parametrize("text", [
    "Dr. Smith will see you now. Please wait.", "Mr. Jones left at 5 p.m. today. He will call.",
    "Use a tool, e.g. a hammer. That works.",
])
def test_abbreviations_are_not_sentence_ends(text):
    sentences = speak([text])
    assert len(sentences) == 2 and sentences[0].endswith(("now.", "today.", "hammer."))


def test_reasoning_is_never_spoken_even_when_a_tag_is_split_across_fragments():
    out = speak(["<thin", "king>\nThe user wants the time. I should check.\n</thin", "king>\n\nIt is five thirty. Anything else?"])
    assert out == ["It is five thirty.", "Anything else?"]
    assert not any("user wants" in s for s in out)


def test_a_reply_that_is_only_reasoning_says_nothing():
    assert speak(["<thinking>\nhmm, let me think\n</thinking>\n"]) == []


def test_an_unclosed_reasoning_block_says_nothing():
    assert speak(["<thinking>\nstill thinking about it. And more thoughts. "]) == []


def test_tool_call_tags_are_not_spoken():
    out = speak(["Let me check. <call:get_system_info detail=\"time\" />", " Done. It is noon."])
    assert out == ["Let me check.", "Done. It is noon."]


def test_a_tool_tag_split_across_fragments_is_not_spoken():
    out = speak(["One moment. <call:get_sys", "tem_info />", " Okay, it is noon."])
    assert not any("call" in s or "get_sys" in s for s in out)


def test_markdown_is_read_as_words():
    out = speak(["**Sure!** Here is the [docs page](https://example.com/x) and `code`. # Heading\n"])
    assert out[0] == "Sure!"
    joined = " ".join(out)
    assert "docs page" in joined and "https" not in joined and "*" not in joined and "`" not in joined


def test_code_blocks_are_skipped():
    out = speak(["Here you go.\n```python\nprint('hi')\n```\nThat prints hi."])
    assert out == ["Here you go.", "That prints hi."]


def test_a_bullet_list_is_spoken_item_by_item():
    out = speak(["Three things:\n- first thing\n- second thing\n- third thing\n"])
    assert out[0] == "Three things:" and out[1:] == ["first thing", "second thing", "third thing"]


def test_a_long_unpunctuated_run_still_starts_being_spoken():
    run = "and then " * 40
    c = SentenceChunker(max_chars=120)
    first = c.feed(run)
    assert first and all(len(s) <= 130 for s in first)


def test_very_short_fragments_are_merged_into_the_next_sentence_after_the_first():
    out = speak(["Hello there friend. Ok. That is the plan for today."])
    assert out == ["Hello there friend.", "Ok. That is the plan for today."]


def test_the_first_sentence_is_spoken_even_when_it_is_short():
    assert speak(["Sure. I can do that for you right away."])[0] == "Sure."


def test_empty_and_whitespace_produce_nothing():
    assert speak(["", "   ", "\n\n"]) == []


def test_flush_is_idempotent():
    c = SentenceChunker()
    c.feed("Almost done")
    assert c.flush() == ["Almost done"] and c.flush() == []


def test_a_question_mark_ends_a_sentence_and_a_tiny_one_joins_the_next():
    assert speak(["Really? Yes! Absolutely."]) == ["Really?", "Yes! Absolutely."]


# --- markup is never spoken, in whatever shape the model wrote it ----------------------------------------------------------
#
# 2026-10-24, the real app: a reply ending `<call><code><call name="update_user_model" focus="memory" max_turns="3"/></code></call>`
# was read aloud. The chunker only knew the `<call:tool ...>` form.

LEAK = ('I am doing well, Weavor. My memory indicates I am running smoothly. '
        '<call><code><call name="update_user_model" focus="memory" max_turns="3"/></code></call>')


def _spoken(fragments):
    return " ".join(speak(fragments))


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 400])
def test_the_leaked_call_markup_is_never_spoken_however_the_fragments_fall(size):
    fragments = [LEAK[i:i + size] for i in range(0, len(LEAK), size)]
    heard = _spoken(fragments)
    assert "I am doing well, Weavor." in heard and "running smoothly" in heard
    for forbidden in ("<", ">", "call", "code", "update_user_model", "name=", "focus", "max_turns"):
        assert forbidden not in heard, f"{forbidden!r} was spoken: {heard!r}"


@pytest.mark.parametrize("markup", [
    '<call name="x" a="1"/>',
    '<call:read_file path="a.txt" />',
    '<call:web_search query="q">a body that is code, not speech</call:web_search>',
    "<code>print('this is code, not speech')</code>",
    "<b>bold</b>",
])
def test_markup_in_other_shapes_is_not_spoken_either(markup):
    heard = _spoken(["Sure thing. ", markup, " All done."])
    assert "Sure thing." in heard and "All done." in heard
    for forbidden in ("<", ">", "call", "print", "this is code", "a body", "path="):
        assert forbidden not in heard, f"{forbidden!r} was spoken: {heard!r}"


def test_a_comparison_is_not_mistaken_for_a_tag():
    assert speak(["Is 3 < 5 true? Yes, and 7 > 2 as well."]) == ["Is 3 < 5 true?", "Yes, and 7 > 2 as well."]


def test_a_tag_split_across_fragments_is_held_until_it_is_whole():
    c = SentenceChunker()
    assert c.feed("Okay. <ca") == ["Okay."]
    assert c.feed('ll name="x"') == []
    assert c.feed("/> Next sentence. ") == ["Next sentence."]
