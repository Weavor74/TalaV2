"""
What she remembers must be what the user last said, and must be forgettable.

Found 2026-10-29/30 testing memory live. The harness saved "remember this: my cat is named Quillon" as the fact "this: my cat is named
Quillon" (the lead-in kept), ignored "you can drop it", and had no way to take a fact back: tell her a cat's name, then correct it, and both
nodes sit in the store with the same weights, so retrieval can hand the model either one. The model was also never told a fact had been
saved (only the UI got the node id), so her "got it" was a guess.

Three changes, all in the deterministic capture path (`sdk.agent.memory_tools`):
* the saved fact is the user's statement, without "this:" / "the following -" lead-ins or a "you can drop it" tail;
* a new fact about the same subject as a saved one (an explicit "remember", or an "actually ..." / "no, it's ..." correction) retires the old
  one: its weights go to the floor so retrieval no longer surfaces it, it is marked superseded and names its successor, and the file stays
  (restorable, and the ledger is append-only anyway);
* "forget that" / "you can drop it" / "forget my cat's name" retires the matching fact, never guessing between equally good matches.

A correction cue alone never saves anything (no fact to correct means no node), so "no, I don't want that" is not a memory. Every outcome
is handed to the model as a short note so she says what actually happened.
"""
from __future__ import annotations

import pytest

from sdk.agent import memory_tools
from sdk.agent import node_learning as nl
from sdk.agent.memory_tools import _remember_from_message, write_memory
from sdk.agent.parser import parse_file


@pytest.fixture
def userdata(tmp_path, monkeypatch):
    (tmp_path / "nodes").mkdir()
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.delenv("TALA_LEARNING", raising=False)
    monkeypatch.setattr(nl, "_halted", lambda: False)
    monkeypatch.setattr(nl, "_ANCHOR_RUNNER", lambda fn: fn())
    monkeypatch.setattr("sdk.agent.anchor.canonicalize_and_anchor", lambda **kw: ("h", "id", "anchored", "", None))
    return tmp_path


def _nodes(userdata):
    return sorted((userdata / "nodes").glob("fact-*.html"))


def _live(userdata):
    return [p for p in _nodes(userdata) if parse_file(str(p)).weight_confidence > nl.MIN_WEIGHT]


def _retired(path):
    return 'data-retired="' in path.read_text(encoding="utf-8")


# --- the fact is what the user said ------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("message,fact", [
    ("remember this: my cat is named Quillon", "my cat is named Quillon"),
    ("Remember this - my locker is number 214", "my locker is number 214"),
    ("please remember the following: the router lives in the hall closet", "the router lives in the hall closet"),
    ("remember that my cat is named Quillon, you can drop it later", "my cat is named Quillon"),
    ("remember my favourite colour is teal (feel free to forget it afterwards)", "my favourite colour is teal"),
])
async def test_lead_ins_and_drop_it_tails_are_not_part_of_the_fact(userdata, message, fact):
    result = await _remember_from_message(message)
    assert result and result["success"] is True
    text = parse_file(result["path"]).semantic_memory
    assert fact in text
    assert "this:" not in text.lower() and "the following" not in text.lower() and "drop it" not in text.lower() and "forget it" not in text.lower()
    assert result["node_id"].startswith("fact-" + fact.lower().split()[0])


@pytest.mark.asyncio
async def test_plain_facts_are_untouched(userdata):
    result = await _remember_from_message("remember that I take my coffee black")
    assert "I take my coffee black" in parse_file(result["path"]).semantic_memory


# --- a correction retires the old fact ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", [
    "Actually my cat is named Pixel",
    "no, my cat is named Pixel",
    "remember that my cat is named Pixel",
    "that's wrong, my cat is called Pixel",
    "correction: my cat is named Pixel",
    "I meant my cat is named Pixel",
])
async def test_a_correction_retires_the_fact_it_replaces(userdata, correction):
    old = await write_memory("my cat is named Quillon", "user-fact")
    result = await _remember_from_message(correction)
    assert result and result["success"] is True and result["node_id"] != old["node_id"]
    assert "Pixel" in parse_file(result["path"]).semantic_memory
    old_node = parse_file(old["path"])
    assert old_node.weight_confidence == nl.MIN_WEIGHT and old_node.weight_relevance == nl.MIN_WEIGHT
    text = (userdata / "nodes" / f"{old['node_id']}.html").read_text(encoding="utf-8")
    assert 'data-retired="superseded"' in text and f'data-superseded-by="{result["node_id"]}"' in text
    assert [p.stem for p in _live(userdata)] == [result["node_id"]]
    assert "Quillon" in result["note"] and "Pixel" in result["note"] and "replace" in result["note"].lower()


@pytest.mark.asyncio
async def test_the_old_fact_stays_on_disk_with_its_original_weights_recorded(userdata):
    old = await write_memory("my cat is named Quillon", "user-fact")
    await _remember_from_message("actually my cat is named Pixel")
    text = (userdata / "nodes" / f"{old['node_id']}.html").read_text(encoding="utf-8")
    assert "Quillon" in text and 'data-retired-from="0.80/0.80"' in text


@pytest.mark.asyncio
async def test_a_fact_about_something_else_retires_nothing(userdata):
    await write_memory("my cat is named Quillon", "user-fact")
    result = await _remember_from_message("remember my dog is named Biscuit")
    assert result["success"] is True and len(_live(userdata)) == 2 and "replace" not in result["note"].lower()


@pytest.mark.asyncio
async def test_the_same_fact_again_is_not_a_correction(userdata):
    old = await write_memory("my cat is named Quillon", "user-fact")
    again = await _remember_from_message("remember that my cat is named Quillon")
    assert again["node_id"] == old["node_id"] and len(_live(userdata)) == 1 and not _retired(userdata / "nodes" / f"{old['node_id']}.html")


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "no, I don't want that",
    "actually let's talk about something else",
    "that's wrong",
    "no",
    "Actually, what is the weather like?",
])
async def test_a_cue_with_no_fact_to_correct_saves_nothing(userdata, message):
    await write_memory("my cat is named Quillon", "user-fact")
    assert await _remember_from_message(message) is None
    assert len(_nodes(userdata)) == 1


@pytest.mark.asyncio
async def test_a_correction_with_nothing_saved_yet_is_not_a_memory(userdata):
    assert await _remember_from_message("actually my cat is named Pixel") is None
    assert _nodes(userdata) == []


@pytest.mark.asyncio
async def test_a_correction_that_is_a_secret_is_refused(userdata):
    await write_memory("my wifi network is named HomeNet", "user-fact")
    result = await _remember_from_message("actually my wifi network password is hunter2xyz")
    assert result is None or result["success"] is False
    assert len(_live(userdata)) == 1


# --- forgetting ----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["forget that", "you can drop it", "Please forget that.", "drop that", "forget what I just told you", "ok you can drop it"])
async def test_forget_that_retires_the_fact_just_saved(userdata, message):
    saved = await _remember_from_message("remember that my cat is named Quillon")
    result = await _remember_from_message(message)
    assert result and result["success"] is True and result["node_id"] == saved["node_id"]
    assert _retired(userdata / "nodes" / f"{saved['node_id']}.html") and _live(userdata) == []
    assert 'data-retired="forgotten"' in (userdata / "nodes" / f"{saved['node_id']}.html").read_text(encoding="utf-8")
    assert "Quillon" in result["note"] and "forgot" in result["note"].lower()


@pytest.mark.asyncio
async def test_forget_by_subject_retires_the_matching_fact_only(userdata):
    await write_memory("my cat is named Quillon", "user-fact")
    dog = await write_memory("my dog is named Biscuit", "user-fact")
    result = await _remember_from_message("forget my cat's name")
    assert result["success"] is True
    assert [p.stem for p in _live(userdata)] == [dog["node_id"]]


@pytest.mark.asyncio
async def test_an_ambiguous_forget_changes_nothing_and_says_so(userdata):
    await write_memory("my cat is named Quillon", "user-fact")
    await write_memory("my cat is a grey tabby", "user-fact")
    result = await _remember_from_message("forget my cat")
    assert result["success"] is False and "which" in result["note"].lower()
    assert len(_live(userdata)) == 2


@pytest.mark.asyncio
async def test_forget_that_with_nothing_recent_changes_nothing_and_says_so(userdata, monkeypatch):
    saved = await write_memory("my cat is named Quillon", "user-fact")
    monkeypatch.setattr(memory_tools, "RECENT_SECONDS", -1)  # nothing counts as just saved
    result = await _remember_from_message("forget that")
    assert result["success"] is False and "nothing" in result["note"].lower()
    assert not _retired(userdata / "nodes" / f"{saved['node_id']}.html")


@pytest.mark.asyncio
async def test_forget_with_no_facts_at_all_is_a_quiet_no_op(userdata):
    result = await _remember_from_message("forget that")
    assert result["success"] is False and "nothing" in result["note"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "I forgot my keys",
    "don't forget the milk",
    "forget it, it's fine",
    "forget about the weather",
    "what did you forget?",
    "drop it like it's hot",
    "I'll forget that I asked",
    "Please forget to mention the budget in the email draft I am writing for my manager tomorrow morning, thanks",
])
async def test_ordinary_sentences_are_not_forget_commands(userdata, message):
    saved = await write_memory("my cat is named Quillon", "user-fact")
    assert await _remember_from_message(message) is None
    assert not _retired(userdata / "nodes" / f"{saved['node_id']}.html")


@pytest.mark.asyncio
async def test_with_learning_off_nothing_is_retired_and_she_is_told(userdata, monkeypatch):
    saved = await write_memory("my cat is named Quillon", "user-fact")
    monkeypatch.setenv("TALA_LEARNING", "0")
    result = await _remember_from_message("forget that")
    assert result["success"] is False and "off" in result["note"].lower()
    assert not _retired(userdata / "nodes" / f"{saved['node_id']}.html")


@pytest.mark.asyncio
async def test_a_retired_fact_is_not_matched_again(userdata):
    await write_memory("my cat is named Quillon", "user-fact")
    await _remember_from_message("forget that")
    again = await _remember_from_message("forget that")
    assert again["success"] is False and "nothing" in again["note"].lower()


# --- what the model is told -------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_saved_fact_comes_with_a_note_the_model_can_repeat_truthfully(userdata):
    result = await _remember_from_message("remember that my cat is named Quillon")
    assert "my cat is named Quillon" in result["note"] and "saved" in result["note"].lower()


def test_the_chat_handler_hands_the_note_to_the_model():
    import inspect

    from python.harness import server

    source = inspect.getsource(server.handle_inference_generate)
    assert '(remembered or {}).get("note")' in source and "system_context_additions" in source


# --- the nodes are hers AND yours: editing, restoring and deleting them by hand ---------------------------------------------------------------
# Facts are HTML-NN nodes so the user can open, edit and delete them. Nothing here may depend on a cache of what was saved.


@pytest.mark.asyncio
async def test_a_fact_edited_by_hand_is_what_a_correction_is_compared_with(userdata):
    old = await write_memory("my cat is named Quillon", "user-fact")
    path = userdata / "nodes" / f"{old['node_id']}.html"
    path.write_text(path.read_text(encoding="utf-8").replace("Quillon", "Whiskers"), encoding="utf-8")
    result = await _remember_from_message("actually my cat is named Pixel")
    assert "Whiskers" in result["note"] and _retired(path)


@pytest.mark.asyncio
async def test_a_fact_the_user_already_edited_to_say_the_same_thing_is_not_saved_twice(userdata):
    old = await write_memory("my cat is named Quillon", "user-fact")
    path = userdata / "nodes" / f"{old['node_id']}.html"
    path.write_text(path.read_text(encoding="utf-8").replace("Quillon", "Pixel"), encoding="utf-8")
    again = await _remember_from_message("remember that my cat is named Pixel")
    assert again["node_id"] == old["node_id"] and len(_nodes(userdata)) == 1 and not _retired(path)


@pytest.mark.asyncio
async def test_a_node_the_user_deleted_is_simply_gone(userdata):
    old = await write_memory("my cat is named Quillon", "user-fact")
    (userdata / "nodes" / f"{old['node_id']}.html").unlink()
    assert await _remember_from_message("actually my cat is named Pixel") is None, "nothing left to correct"
    assert (await _remember_from_message("forget that"))["success"] is False
    result = await _remember_from_message("remember that my cat is named Pixel")
    assert result["success"] is True and len(_nodes(userdata)) == 1


@pytest.mark.asyncio
async def test_a_retired_node_the_user_restores_by_hand_is_live_again(userdata):
    old = await write_memory("my cat is named Quillon", "user-fact")
    await _remember_from_message("forget that")
    path = userdata / "nodes" / f"{old['node_id']}.html"
    assert _retired(path)
    import re

    text = path.read_text(encoding="utf-8")
    text = re.sub(r'\s*data-(?:retired|retired-from|superseded-by)="[^"]*"', "", text)
    text = text.replace(f'data-weight-relevance="{nl._fmt(nl.MIN_WEIGHT)}"', 'data-weight-relevance="0.80"')
    text = text.replace(f'data-weight-confidence="{nl._fmt(nl.MIN_WEIGHT)}"', 'data-weight-confidence="0.80"')
    path.write_text(text, encoding="utf-8")
    assert [p.stem for p in _live(userdata)] == [old["node_id"]]
    result = await _remember_from_message("forget that")
    assert result["success"] is True and result["node_id"] == old["node_id"]


@pytest.mark.asyncio
async def test_the_retired_marks_are_plain_attributes_on_the_node_a_person_can_read(userdata):
    old = await write_memory("my cat is named Quillon", "user-fact")
    await _remember_from_message("actually my cat is named Pixel")
    article = (userdata / "nodes" / f"{old['node_id']}.html").read_text(encoding="utf-8").split("<article", 1)[1].split(">", 1)[0]
    for attribute in ('data-retired="superseded"', "data-superseded-by=", 'data-retired-from="0.80/0.80"'):
        assert attribute in article


# --- found live 2026-10-30: "forget that" 17 minutes after the cat was last discussed retired nothing, and she said it was forgotten ---------------


def _age(userdata, node_id, seconds):
    import os
    import time

    path = userdata / "nodes" / f"{node_id}.html"
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


@pytest.mark.asyncio
async def test_forget_that_means_the_fact_last_saved_or_recalled_within_the_hour(userdata):
    saved = await write_memory("my cat is named Pixel", "user-fact")
    _age(userdata, saved["node_id"], 17 * 60)  # discussed 17 minutes ago: the live case
    result = await _remember_from_message("forget that")
    assert result["success"] is True and result["node_id"] == saved["node_id"]


@pytest.mark.asyncio
async def test_a_fact_untouched_for_hours_is_not_that(userdata):
    saved = await write_memory("my cat is named Pixel", "user-fact")
    _age(userdata, saved["node_id"], 3 * 3600)
    result = await _remember_from_message("forget that")
    assert result["success"] is False and "nothing" in result["note"].lower()
    assert not _retired(userdata / "nodes" / f"{saved['node_id']}.html")


@pytest.mark.asyncio
async def test_two_facts_recalled_together_make_that_ambiguous(userdata):
    await write_memory("my cat is named Pixel", "user-fact")
    await write_memory("my dog is named Biscuit", "user-fact")
    result = await _remember_from_message("forget that")
    assert result["success"] is False and "which" in result["note"].lower()
    assert len(_live(userdata)) == 2


@pytest.mark.asyncio
async def test_the_newer_of_two_facts_is_that_when_they_were_not_touched_together(userdata):
    older = await write_memory("my dog is named Biscuit", "user-fact")
    _age(userdata, older["node_id"], 40 * 60)
    newer = await write_memory("my cat is named Pixel", "user-fact")
    result = await _remember_from_message("forget that")
    assert result["success"] is True and result["node_id"] == newer["node_id"]


# --- she must not claim a forget that did not happen ----------------------------------------------------------------------------------------------

CLAIMS = ["Understood—I’ll no longer reference Pixel.", "Okay, I’ve forgotten that.", "Done, I forgot it.", "Sure, I won’t remember that anymore.",
          "Got it, deleted.", "I have removed it from my memory.", "Consider it forgotten."]


@pytest.mark.parametrize("claim", CLAIMS)
def test_a_claim_to_have_forgotten_after_a_failed_forget_gets_a_correction(claim):
    from python.harness import turn_log

    failed = {"success": False, "note": "[Memory] nothing was saved just now to forget"}
    fixed = turn_log.TurnLog(failed).add_unsupported_note(claim)
    assert fixed.startswith(claim) and "Correction:" in fixed and "nothing was forgotten" in fixed


def test_an_honest_reply_after_a_failed_forget_is_left_alone():
    from python.harness import turn_log

    failed = {"success": False, "note": "[Memory] nothing was saved just now to forget"}
    for text in ("I don't see anything recent to forget. What do you mean?", "Which one do you want me to forget, the cat or the dog?"):
        assert turn_log.TurnLog(failed).add_unsupported_note(text) == text


def test_a_claim_after_a_real_forget_is_left_alone():
    from python.harness import turn_log

    done = {"success": True, "node_id": "fact-x", "retired": ["fact-x"], "note": "[Memory] Forgotten at the user's request"}
    claim = "Understood, I'll no longer reference Pixel."
    assert turn_log.TurnLog(done).add_unsupported_note(claim) == claim


def test_no_correction_when_there_was_no_memory_command():
    from python.harness import turn_log

    claim = "I'll no longer mention the weather."
    assert turn_log.TurnLog(None).add_unsupported_note(claim) == claim
    assert turn_log.TurnLog({"success": True, "node_id": "fact-y", "note": "[Memory] Saved"}).add_unsupported_note(claim) == claim


def test_the_correction_says_why_and_is_spoken():
    from python.harness import reply_text, turn_log

    failed = {"success": False, "note": "[Memory] The user asked you to forget something, but nothing was saved just now to forget"}
    fixed = reply_text.finish_reply("I’ll no longer reference Pixel.", False, turn_log.TurnLog(failed))
    assert reply_text.trailing_correction(fixed).startswith("Correction:") and "nothing was forgotten" in fixed


def test_the_chat_handler_gives_the_log_the_memory_outcome():
    import inspect

    from python.harness import server

    assert "TurnLog(remembered)" in inspect.getsource(server.handle_inference_generate)


# --- found live 2026-10-30: after the fact was retired she still said "Quillon", from earlier in the same chat ------------------------------------
# A retired node is no longer recalled, but the conversation already on screen still holds the words, and the model repeats them. So for the
# next few hours every turn carries a note naming what the user took back, with the instruction not to use or repeat it.


@pytest.mark.asyncio
async def test_a_forgotten_fact_is_named_in_a_do_not_repeat_note(userdata):
    await write_memory("my cat is named Pixel", "user-fact")
    await _remember_from_message("forget that")
    note = memory_tools.forgotten_note()
    assert "my cat is named Pixel" in note and "forgotten" in note and "do not use or repeat" in note.lower()


@pytest.mark.asyncio
async def test_a_replaced_fact_is_named_with_what_replaced_it(userdata):
    await write_memory("my cat is named Quillon", "user-fact")
    await _remember_from_message("actually my cat is named Pixel")
    note = memory_tools.forgotten_note()
    assert "Quillon" in note and "replaced" in note and "Pixel" in note


@pytest.mark.asyncio
async def test_nothing_retired_means_no_note(userdata):
    await write_memory("my cat is named Pixel", "user-fact")
    assert memory_tools.forgotten_note() is None


@pytest.mark.asyncio
async def test_a_fact_retired_long_ago_is_no_longer_mentioned(userdata, monkeypatch):
    await write_memory("my cat is named Pixel", "user-fact")
    await _remember_from_message("forget that")
    monkeypatch.setattr(memory_tools, "RETIRED_NOTE_HOURS", -1)
    assert memory_tools.forgotten_note() is None


@pytest.mark.asyncio
async def test_a_fact_the_user_restored_by_hand_is_not_mentioned(userdata):
    import re

    saved = await write_memory("my cat is named Pixel", "user-fact")
    await _remember_from_message("forget that")
    path = userdata / "nodes" / f"{saved['node_id']}.html"
    text = re.sub(r'\s*data-(?:retired|retired-from|retired-at|superseded-by)="[^"]*"', "", path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")
    assert memory_tools.forgotten_note() is None


@pytest.mark.asyncio
async def test_a_deleted_node_is_not_mentioned(userdata):
    saved = await write_memory("my cat is named Pixel", "user-fact")
    await _remember_from_message("forget that")
    (userdata / "nodes" / f"{saved['node_id']}.html").unlink()
    assert memory_tools.forgotten_note() is None


def test_the_note_never_raises(monkeypatch):
    monkeypatch.setattr(memory_tools, "_saved_facts", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.delenv("TALA_USER_DATA", raising=False)
    assert memory_tools.forgotten_note() is None or isinstance(memory_tools.forgotten_note(), str)


def test_the_chat_handler_adds_the_do_not_repeat_note_to_every_turn():
    import inspect

    from python.harness import server

    source = inspect.getsource(server.handle_inference_generate)
    assert "forgotten_note()" in source and "system_context_additions" in source
