"""
When the user challenges an answer and she goes and looks, she must say which parts of the earlier answer the lookup does not support.

Found 2026-10-29. Asked for the Starlancer TAC's default components she wrote a sixteen-row table from nothing (MISC "Sphinx" fusion core,
"Talon" thrust jets, a "75 mm" cannon...). Challenged with "where did you get that", she ran starcitizen_lookup for real, and then said the
spec came from the API. The lookup had returned dimensions, mass, cargo, crew and emissions: no components at all. She also called a URL she
had built herself "the link that the lookup returned". She never withdrew the table.

The check is code, not a request. On a challenged turn in which a tool succeeded, the specifics of the answer being challenged (quoted
names, measured quantities, big numbers) are looked for in what the tools returned; the ones that are not there are named in a correction.
"""
from __future__ import annotations

import inspect

import pytest

from python.harness import honesty, reply_text, turn_log

TABLE = (
    "**Starlancer TAC – “default” component load‑out**\n\n"
    "| Category | Component | Notes |\n|---|---|---|\n"
    "| Power | 2 × MISC “Sphinx” fusion core | sized for ~2 MW output |\n"
    "| Propulsion | 4 × MISC “Talon” thrust jets | give the 150 SCM top speed |\n"
    "| Weapons | MISC “25 mm” cannon, MISC “75 mm” cannon | forward and aft |\n"
    "| Hangar | MISC “Snub” hangar | holds 4 × “Luna‑S” light fighters |\n"
)
HISTORY = [{"role": "user", "content": "What is the default component load out"}, {"role": "assistant", "content": TABLE}]

# What the live lookup actually returned: no components anywhere.
LOOKUP = {"resource": "vehicles", "query": "Starlancer TAC", "total_matches": 2, "returned": 1, "results": [{
    "name": "MISC Starlancer TAC", "slug": "misc-starlancer-tac", "manufacturer": {"name": "MISC", "link": "https://api.star-citizen.wiki/api/manufacturers/MISC"},
    "sizes": {"length": 90, "beam": 60, "height": 20}, "mass": 738071, "cargo_capacity": 96, "crew": {"min": 8}, "emission": {"ir": 11218}}]}

CHALLENGE = "where did you get that"
HONEST_START = "I looked it up just now."


def _log(challenge=CHALLENGE, history=HISTORY, results=(("starcitizen_lookup", LOOKUP),)):
    entry = turn_log.TurnLog()
    entry.challenge_note(challenge, history)
    for name, result in results:
        entry.record(name, result)
    return entry


# --- pulling out what an answer asserts -----------------------------------------------------------------------------------------------------


def test_the_specifics_of_a_table_are_its_quoted_names_measurements_and_big_numbers():
    found = turn_log._specifics(TABLE)
    for expected in ("sphinx", "talon", "25 mm", "75 mm", "2 mw", "150 scm", "snub", "luna-s"):
        assert expected in found, (expected, found)


def test_plain_prose_has_no_specifics():
    assert turn_log._specifics("I think it is a decent ship and I like it a lot.") == []


def test_a_hyphen_variant_does_not_hide_a_name():
    assert "luna-s" in turn_log._specifics("the “Luna‑S” fighter")


# --- the correction -------------------------------------------------------------------------------------------------------------------------


def test_a_challenged_answer_the_lookup_does_not_support_is_named_in_a_correction():
    fixed = _log().add_unsupported_note(f"{HONEST_START} It gave dimensions and mass.")
    note = fixed.split("\n\nCorrection: ")[1]
    assert "sphinx" in note.lower() and "talon" in note.lower() and "did not come from it" in note
    assert "unverified" in note


def test_the_correction_is_spoken_when_the_reply_was_already_streamed():
    fixed = reply_text.finish_reply(f"{HONEST_START} It gave dimensions.", True, _log())
    assert reply_text.trailing_correction(fixed).startswith("Correction:")


def test_the_list_is_capped_and_says_how_many_more():
    many = "\n".join(f"“Part{n}”" for n in range(30))
    entry = turn_log.TurnLog()
    entry.challenge_note(CHALLENGE, [{"role": "assistant", "content": many}])
    entry.record("starcitizen_lookup", LOOKUP)
    note = entry.add_unsupported_note("Looked.").split("Correction: ")[1]
    assert "and 2" in note and note.count("part") <= 8


def test_details_the_lookup_does_contain_are_not_reported():
    answer = "It is 90 long and weighs 738,071 kg, the “MISC Starlancer TAC”, plus a “Sphinx” core and a “Talon” jet and a “Snub” hangar."
    entry = turn_log.TurnLog()
    entry.challenge_note(CHALLENGE, [{"role": "assistant", "content": answer}])
    entry.record("starcitizen_lookup", LOOKUP)
    note = entry.add_unsupported_note("Looked.").split("Correction: ")[1]
    assert "738" not in note and "misc starlancer tac" not in note.lower() and "sphinx" in note.lower()


def test_an_answer_the_lookup_fully_supports_gets_no_correction():
    answer = "It is the “MISC Starlancer TAC” with 738071 mass and a 96 cargo capacity."
    entry = turn_log.TurnLog()
    entry.challenge_note(CHALLENGE, [{"role": "assistant", "content": answer}])
    entry.record("starcitizen_lookup", LOOKUP)
    assert entry.add_unsupported_note("Yes.") == "Yes."


def test_no_correction_when_the_user_did_not_challenge():
    entry = _log(challenge="what is the mass of the TAC?")
    assert entry.add_unsupported_note("About 738071.") == "About 738071."


def test_no_correction_when_no_tool_succeeded_this_turn():
    assert _log(results=()).add_unsupported_note("It came from the API.") == "It came from the API."
    failed = _log(results=(("starcitizen_lookup", {"success": False, "error": "HTTP 500"}),))
    assert failed.add_unsupported_note("It came from the API.") == "It came from the API."


def test_a_reply_that_already_withdraws_the_answer_is_left_alone():
    for text in ("I made that table up; none of it is from the lookup.", "Those component names were from memory, not a source.",
                 "That was a guess on my part and it is unverified."):
        assert _log().add_unsupported_note(text) == text


def test_one_or_two_stray_details_are_not_worth_a_correction():
    answer = "The “MISC Starlancer TAC” has a “Sphinx” core."
    entry = turn_log.TurnLog()
    entry.challenge_note(CHALLENGE, [{"role": "assistant", "content": answer}])
    entry.record("starcitizen_lookup", LOOKUP)
    assert entry.add_unsupported_note("Looked.") == "Looked."


def test_the_correction_is_not_added_twice():
    once = _log().add_unsupported_note(HONEST_START)
    assert _log().add_unsupported_note(once) == once


# --- a link she built, called one the lookup returned ---------------------------------------------------------------------------------------


def test_a_link_not_in_the_lookup_result_is_called_out():
    reply = ("I pulled it from the API. Here's the link that the lookup returned: "
             "[https://api.star‑citizen.wiki/api/vehicles/misc‑starlancer‑tac](https://api.star-citizen.wiki/api/vehicles/misc-starlancer-tac)")
    entry = turn_log.TurnLog()
    entry.challenge_note(CHALLENGE, [{"role": "assistant", "content": "A fine ship."}])
    entry.record("starcitizen_lookup", LOOKUP)
    note = entry.add_unsupported_note(reply).split("Correction: ")[1]
    assert "link" in note and "api/vehicles/misc-starlancer-tac" in note and "not in the lookup result" in note


def test_a_link_the_result_does_contain_is_fine():
    reply = "See https://api.star-citizen.wiki/api/manufacturers/MISC for the maker."
    entry = turn_log.TurnLog()
    entry.challenge_note(CHALLENGE, [{"role": "assistant", "content": "A fine ship."}])
    entry.record("starcitizen_lookup", LOOKUP)
    assert entry.add_unsupported_note(reply) == reply


# --- the log itself -------------------------------------------------------------------------------------------------------------------------


def test_the_log_keeps_the_failure_outcomes_the_failed_tool_note_reads():
    entry = turn_log.TurnLog()
    entry.record("starcitizen_lookup", {"error": "HTTP 422"})
    entry.record("search_memory", {"results": []})
    assert entry.outcomes == [("starcitizen_lookup", "HTTP 422"), ("search_memory", None)]


def test_finish_reply_still_takes_a_plain_outcomes_list():
    assert "Correction:" in reply_text.finish_reply("Here is the table.", True, [("starcitizen_lookup", "HTTP 422")])


def test_the_log_returns_the_same_challenge_note_the_module_does():
    entry = turn_log.TurnLog()
    assert entry.challenge_note(CHALLENGE, HISTORY) == honesty.challenge_note(CHALLENGE, HISTORY)
    assert turn_log.TurnLog().challenge_note("what is my cat's name", HISTORY) is None


def test_a_broken_history_never_costs_the_reply():
    entry = turn_log.TurnLog()
    entry.challenge_note(CHALLENGE, None)
    entry.record("starcitizen_lookup", object())
    assert entry.add_unsupported_note("Fine.") == "Fine."


# --- wiring ---------------------------------------------------------------------------------------------------------------------------------


def test_the_chat_handler_uses_one_log_for_outcomes_the_challenge_and_the_finish():
    from python.harness import server

    source = inspect.getsource(server.handle_inference_generate)
    assert "turn_log.TurnLog()" in source and "reply_log.record(" in source
    assert "reply_log.challenge_note(" in source and "None if raw_prompt else reply_log" in source


@pytest.mark.parametrize("text", ["", None])
def test_empty_reply_is_untouched(text):
    assert _log().add_unsupported_note(text) == text
