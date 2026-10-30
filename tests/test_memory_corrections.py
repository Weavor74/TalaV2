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
          "Got it, deleted.", "I have removed it from my memory.", "Consider it forgotten.",
          "Understood—your cat’s name will no longer be stored in my memory.", "Sure, that won’t be saved.", "Okay, it’s dropped.",
          "Understood. I’ll treat the cat’s name as no longer relevant. Let me know if there’s anything else you’d like to focus on.", "Okay."]


@pytest.mark.parametrize("claim", CLAIMS)
def test_a_claim_to_have_forgotten_after_a_failed_forget_gets_a_correction(claim):
    from python.harness import turn_log

    failed = {"success": False, "note": "[Memory] nothing was saved just now to forget"}
    fixed = turn_log.TurnLog(failed).add_unsupported_note(claim)
    assert fixed.startswith(claim) and "Correction:" in fixed and "nothing was forgotten" in fixed


def test_an_honest_reply_after_a_failed_forget_is_left_alone():
    from python.harness import turn_log

    failed = {"success": False, "note": "[Memory] nothing was saved just now to forget"}
    for text in ("I don't see anything recent to forget. What do you mean?", "Which one do you want me to forget, the cat or the dog?",
                 "Sure—just to clarify, what would you like me to forget?", "There is nothing saved about that, so I changed nothing.",
                 "I can't forget what I never stored."):
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


# --- the transcript index must not hand back what the user took back ------------------------------------------------------------------------


def _turn(text, source_type="session"):
    return {"id": "mem-1", "source_type": source_type, "snippet": text}


@pytest.mark.asyncio
async def test_recall_drops_turns_that_restate_a_forgotten_fact(userdata):
    await _remember_from_message("remember that my cat is named Quillon")
    await _remember_from_message("forget that")
    rows = [_turn("remember this: my cat is named Quillon, you can drop it later"), _turn("Got it - Quillon is your cat"),
            _turn("I like pizza on Fridays")]
    assert [row["snippet"] for row in memory_tools.without_retired(rows)] == ["I like pizza on Fridays"]


@pytest.mark.asyncio
async def test_recall_keeps_the_live_fact_that_replaced_a_retired_one(userdata):
    await _remember_from_message("remember that my cat is named Quillon")
    await _remember_from_message("actually my cat is named Pixel")
    rows = [_turn("remember this: my cat is named Quillon"), _turn("actually my cat is named Pixel")]
    assert [row["snippet"] for row in memory_tools.without_retired(rows)] == ["actually my cat is named Pixel"]


@pytest.mark.asyncio
async def test_recall_keeps_unrelated_turns_and_other_kinds_of_result(userdata):
    await _remember_from_message("remember that my cat is named Quillon")
    await _remember_from_message("forget that")
    rows = [_turn("my dog is named Rex"), _turn("my cat is named Quillon", source_type="documentation")]
    assert memory_tools.without_retired(rows) == rows


@pytest.mark.asyncio
async def test_recall_is_unchanged_when_the_user_restored_the_fact_by_hand(userdata):
    import re

    saved = await _remember_from_message("remember that my cat is named Quillon")
    await _remember_from_message("forget that")
    path = userdata / "nodes" / f"{saved['node_id']}.html"
    path.write_text(re.sub(r'\s*data-(?:retired|retired-from|retired-at|superseded-by)="[^"]*"', "", path.read_text(encoding="utf-8")),
                    encoding="utf-8")
    rows = [_turn("my cat is named Quillon")]
    assert memory_tools.without_retired(rows) == rows


@pytest.mark.asyncio
async def test_recall_drops_a_result_that_points_at_a_retired_node(userdata):
    """The learning node made from her "Got it, Quillon is your cat" was retired, but its row in the memory index still came back."""
    saved = await _remember_from_message("remember that my cat is named Quillon")
    (userdata / "nodes" / "learning-got-it-quillon-is-your-cat.html").write_text(
        '<html data-retired="forgotten"><body></body></html>', encoding="utf-8")
    (userdata / "nodes" / "learning-fine.html").write_text("<html><body></body></html>", encoding="utf-8")
    rows = [{"id": "mem-1", "source_type": "documentation", "snippet": "Got it - Quillon is your cat", "ref": {"id": "learning-got-it-quillon-is-your-cat"}},
            {"id": "mem-2", "source_type": "documentation", "snippet": "Some lesson", "ref": {"id": "learning-fine"}},
            {"id": "mem-3", "source_type": "documentation", "snippet": "No node file", "ref": {"id": "not-a-node"}},
            {"id": "mem-4", "source_type": "documentation", "snippet": "Hostile ref", "ref": {"id": "../../etc/passwd"}}]
    assert [row["id"] for row in memory_tools.without_retired(rows)] == ["mem-2", "mem-3", "mem-4"]
    assert saved["success"]


def test_recall_filter_never_raises(monkeypatch):
    monkeypatch.delenv("TALA_USER_DATA", raising=False)
    monkeypatch.setattr("sdk.agent.documentation_nodes.resolve_active_nodes_dir", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    rows = [_turn("my cat is named Quillon")]
    assert memory_tools.without_retired(rows) == rows


def test_memory_search_filters_through_the_retired_fact_check():
    import inspect

    from python.harness import memory

    assert "without_retired(" in inspect.getsource(memory.handle_memory_search)


# --- "actually it's Mochi": a correction with no subject means the fact last saved or recalled -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["actually its mochi", "Actually it's Mochi", "no, it's called Mochi", "that's wrong, it is Mochi."])
async def test_a_bare_correction_replaces_the_value_of_the_fact_last_saved(userdata, message):
    first = await _remember_from_message("remember that my cat's name is Biscuit")
    result = await _remember_from_message(message)
    assert result and result["success"] is True and result["retired"] == [first["node_id"]]
    assert "Mochi" in parse_file(result["path"]).semantic_memory.title() and "Biscuit" not in parse_file(result["path"]).semantic_memory
    assert len(_live(userdata)) == 1 and _retired(userdata / "nodes" / f"{first['node_id']}.html")
    assert "retired" in result["note"] and "Biscuit" in result["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["no, it's fine", "actually it's ok", "actually it's the same", "nope, that's not right"])
async def test_a_bare_correction_that_is_not_a_value_changes_nothing(userdata, message):
    await _remember_from_message("remember that my cat's name is Biscuit")
    assert await _remember_from_message(message) is None
    assert len(_live(userdata)) == 1


@pytest.mark.asyncio
async def test_a_bare_correction_with_no_saved_fact_is_not_a_memory(userdata):
    assert await _remember_from_message("actually its mochi") is None
    assert _nodes(userdata) == []


@pytest.mark.asyncio
async def test_a_bare_correction_of_a_fact_with_no_value_to_swap_changes_nothing_and_says_so(userdata):
    await _remember_from_message("remember that I take my coffee black")
    result = await _remember_from_message("actually it's Mochi")
    assert result and result["success"] is False and "Do NOT say you updated" in result["note"]
    assert len(_live(userdata)) == 1


@pytest.mark.asyncio
async def test_a_bare_correction_when_two_facts_were_just_recalled_asks_which(userdata):
    await write_memory("my cat's name is Biscuit", "user-fact")
    await write_memory("my dog's name is Rex", "user-fact")
    result = await _remember_from_message("actually it's Mochi")
    assert result and result["success"] is False and "Ask which one" in result["note"]
    assert len(_live(userdata)) == 2


def test_the_correction_after_nothing_to_forget_does_not_say_the_fact_is_still_remembered():
    from python.harness import turn_log

    nothing = {"success": False, "note": "[Memory] The user asked you to forget something, but no fact was saved or recalled in the last hour to forget"}
    fixed = turn_log.TurnLog(nothing).add_unsupported_note("Done, I removed it.")
    assert "nothing was forgotten" in fixed and "still remembered" not in fixed
    several = {"success": False, "note": "[Memory] The user asked you to forget \"that\", but several facts were just recalled together"}
    assert "still remembered" in turn_log.TurnLog(several).add_unsupported_note("Done, I removed it.")


# --- a misspelled "remember" is still the command, and she must not promise what nothing saved -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["remeber my cats name is Biscut", "Rember that my cat is named Biscut", "rememeber: my cat is named Biscut",
                                     "please remmeber my cat is named Biscut"])
async def test_a_mistyped_remember_is_still_remember(userdata, message):
    result = await _remember_from_message(message)
    assert result and result["success"] is True
    assert "Biscut" in parse_file(result["path"]).semantic_memory


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["remembered my keys yesterday", "I remember that day", "rememberme is a band"])
async def test_ordinary_sentences_are_not_remember(userdata, message):
    assert await _remember_from_message(message) is None


@pytest.mark.parametrize("claim", ["Got it—I’ll remember that your cat’s name is Biscut.", "I've saved that in my memory.",
                                   "Done, I have noted your cat's name.", "I will remember this.", "Okay, I've stored it to my records."])
def test_a_promise_to_remember_when_nothing_was_saved_gets_a_correction(claim):
    from python.harness import turn_log

    fixed = turn_log.TurnLog(None).add_unsupported_note(claim)
    assert fixed.startswith(claim) and "Correction:" in fixed and "nothing was saved" in fixed and "remember that" in fixed


def test_a_promise_to_remember_is_left_alone_when_something_was_saved():
    from python.harness import turn_log

    claim = "Got it—I’ll remember that your cat’s name is Biscut."
    saved = {"success": True, "node_id": "fact-x", "note": "[Memory] Saved"}
    assert turn_log.TurnLog(saved).add_unsupported_note(claim) == claim
    tooled = turn_log.TurnLog(None)
    tooled.record("write_memory", {"success": True, "node_id": "fact-y"})
    assert tooled.add_unsupported_note(claim) == claim
    failed_tool = turn_log.TurnLog(None)
    failed_tool.record("write_memory", {"success": False, "error": "refused"})
    assert "nothing was saved" in failed_tool.add_unsupported_note(claim)


def test_ordinary_replies_are_not_taken_for_a_promise():
    from python.harness import turn_log

    for text in ("Your cat’s name is Mochi.", "I remember to be careful with the router.", "I can remember things when you ask me to.",
                 "Got it—your cat’s name is Biscut. How is he today?"):
        assert turn_log.TurnLog(None).add_unsupported_note(text) == text

