"""
Her acknowledgement of a fact must not become a permanent learning event.

Found 2026-10-30 in the memory test. She confirmed a fact with "Got it—Quillon is your cat. I've logged that in my memory and will keep it
in mind for future reference." The phrase "for future reference" is one of the chat-turn learning cues, so that reply was written as a
learning-event node at weight 1.00 (node `learning-got-it-quillon-is-your-cat`). It was a second, unretired copy of the fact: after the real
fact was corrected and then forgotten, that node was still recalled, and she kept answering "Quillon", even in a new chat.

The harness has already stored a remembered / corrected / forgotten fact where it belongs, so a turn that carried a memory command never
becomes a learning event, and neither does a reply that only acknowledges ("Got it", "Understood", "Noted", "Sure"). An explicit learning
event, and a real lesson stated with a cue, are captured as before.
"""
from __future__ import annotations

import inspect

import pytest

from python.harness import memory

ACK = "Got it—Quillon is your cat. I’ve logged that in my memory and will keep it in mind for future reference."
LESSON = "From now on, run the focused tests before the full suite, because the full suite takes ten minutes and hides the failing file."


def _payload(content, **params):
    return memory._build_learning_node_payload(session_id="chat-1", role="assistant", content=content, tool_calls=[], params=params)


def test_the_live_acknowledgement_is_not_a_learning_event():
    assert _payload(ACK) is None


@pytest.mark.parametrize("reply", [
    "Understood—I’ll keep that in mind for future reference.",
    "Noted. For future reference I will use the shorter form.",
    "Sure, from now on I will call the cat Pixel.",
    "Okay, I'll remember that for next time.",
    "Done. Lesson learned, I won't do that again.",
    "Will do, going forward I will check first.",
])
def test_replies_that_only_acknowledge_are_not_captured(reply):
    assert _payload(reply) is None


def test_a_turn_that_carried_a_memory_command_is_never_captured():
    assert _payload(LESSON, memory_command=True) is None


def test_a_real_lesson_stated_with_a_cue_is_still_captured():
    payload = _payload(LESSON)
    assert payload is not None and "run the focused tests" in payload["topic"]


def test_an_explicit_learning_event_is_captured_even_on_a_memory_command_turn():
    event = {"topic": "Use the focused tests first", "summary": "The full suite takes ten minutes and hides the failing file, so run the focused tests first."}
    assert _payload("Got it, noted.", memory_command=True, learning_event=event) is not None


def test_user_turns_were_never_captured_by_cue():
    assert memory._build_learning_node_payload(session_id="s", role="user", content=LESSON, tool_calls=[], params={}) is None


def test_the_chat_handler_marks_a_memory_command_turn_when_it_records_it():
    from python.harness import server

    assert '"memory_command": bool(remembered)' in inspect.getsource(server.handle_inference_generate)
