"""
Tala must not point the user at a file that is not there, and must not defend an answer by reflex when it is challenged.

Found 2026-10-29 in a live test. Asked for "the config for the TAC", she wrote a spec table from general knowledge and said to check
`nodes/ship‑profile.html` (that node does not exist, and the hyphen was a non-breaking one, so a naive path match would miss it). Asked
where she got it, she said "public schematics" she had seen, with no tool call behind it. Told "how can I trust you if you're lying", she
said the ship's configuration, "including the Wi-Fi password", lives in that same node. Each reply defended the one before.

Two guards, both in code because a prompt rule is only a request:
* a reply that cites a project file or node that does not exist gets a correction appended, shown and spoken (reply_text.finish_reply);
* when the user challenges an answer, the harness checks the files that answer cited and tells the model what it found, with an instruction
  not to defend or invent a source (honesty.challenge_note, added to the turn's system context).
"""
from __future__ import annotations

import inspect

import pytest

from python.harness import honesty, reply_text

NB = "\u2011"  # the non-breaking hyphen the model actually wrote


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "nodes").mkdir()
    (tmp_path / "nodes" / "real-node.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "sdk").mkdir()
    (tmp_path / "sdk" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(honesty, "_root", lambda: tmp_path)
    return tmp_path


# --- finding the citations --------------------------------------------------------------------------------------------------------


def test_a_cited_node_is_found_even_with_a_non_breaking_hyphen_and_backticks():
    assert honesty._cited_paths(f"you can check `nodes/ship{NB}profile.html` for that") == ["nodes/ship-profile.html"]


def test_windows_separators_and_trailing_punctuation_are_handled():
    assert honesty._cited_paths("It is in nodes\\real-node.html.") == ["nodes/real-node.html"]
    assert honesty._cited_paths("(see sdk/thing.py), then stop") == ["sdk/thing.py"]


def test_urls_prose_and_paths_outside_the_project_are_not_citations():
    text = "See https://example.com/nodes/x.html and C:\\Users\\me\\nodes\\y.html and the nodes folder and a.b/c.html"
    assert honesty._cited_paths(text) == []


def test_code_she_is_writing_is_not_a_citation():
    assert honesty._cited_paths("```\nopen('nodes/new-thing.html')\n```") == []


def test_a_path_in_a_sentence_about_creating_it_is_not_a_citation_of_something_existing():
    for sentence in ("I'll create nodes/new-thing.html for that.", "I saved it to nodes/new-thing.html.", "You could add nodes/new-thing.html.",
                     "For example, nodes/new-thing.html might hold it.", "Should I write nodes/new-thing.html?"):
        assert honesty._cited_paths(sentence) == [], sentence


def test_the_same_path_is_reported_once():
    assert honesty._cited_paths("nodes/a.html, then nodes/a.html again") == ["nodes/a.html"]


# --- the correction on a reply ------------------------------------------------------------------------------------------------------


def test_a_reply_citing_a_missing_file_gets_a_correction(root):
    fixed = reply_text.finish_reply(f"Check `nodes/ship{NB}profile.html` to adjust it.", tools_ran=True)
    assert fixed.startswith(f"Check `nodes/ship{NB}profile.html` to adjust it.")
    assert "Correction:" in fixed and "nodes/ship-profile.html" in fixed and "doesn't exist" in fixed


def test_a_reply_citing_files_that_exist_is_left_alone(root):
    text = "It is in `nodes/real-node.html` and `sdk/thing.py`."
    assert reply_text.finish_reply(text, tools_ran=True) == text


def test_only_the_missing_file_is_named(root):
    fixed = reply_text.finish_reply("See nodes/real-node.html and nodes/ghost.html.", tools_ran=True)
    assert "nodes/ghost.html" in fixed.split("Correction:")[1] and "real-node" not in fixed.split("Correction:")[1]


def test_the_correction_is_not_added_twice(root):
    once = reply_text.finish_reply("Look at nodes/ghost.html.", tools_ran=True)
    assert reply_text.finish_reply(once, tools_ran=True) == once


def test_a_reply_with_no_citations_is_untouched(root):
    assert reply_text.finish_reply("Sure, happy to help.", tools_ran=True) == "Sure, happy to help."


def test_a_failure_to_check_never_costs_the_reply(monkeypatch):
    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(honesty, "_root", boom)
    assert reply_text.finish_reply("See nodes/ghost.html.", tools_ran=True) == "See nodes/ghost.html."


def test_the_trailing_correction_is_found_for_both_kinds(root):
    claim = reply_text.finish_reply("I opened the browser.", tools_ran=False)
    cite = reply_text.finish_reply("See nodes/ghost.html.", tools_ran=True)
    assert reply_text.trailing_correction(claim) == reply_text.CLAIM_CORRECTION
    assert reply_text.trailing_correction(cite).startswith("Correction:") and "ghost" in reply_text.trailing_correction(cite)
    assert reply_text.trailing_correction("Just a reply.") is None


def test_the_citation_correction_is_spoken_when_the_reply_was_already_streamed(root):
    from python.harness.voice_runtime import VoiceRuntime

    class Stream:
        text_seen = True

        def __init__(self):
            self.fed = []

        def feed_text(self, text):
            self.fed.append(text)

        def finish(self):
            pass

    runtime = object.__new__(VoiceRuntime)
    stream = Stream()
    runtime.stream_for = lambda turn_id: stream
    runtime.turn_done("t1", reply_text.finish_reply("See nodes/ghost.html.", tools_ran=True))
    assert any("ghost" in piece and "doesn't exist" in piece for piece in stream.fed)


# --- recognising a challenge ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    "Tala this doesnt exist in your databanks where did you pull the information from? nodes/ship-profile.html",
    "No Tala, how can I trust you if your lying to me",
    "that doesn't exist",
    "Are you sure about that?",
    "where did you get that?",
    "how do you know that",
    "you made that up",
    "Is that true?",
    "that's wrong",
    "prove it",
])
def test_challenges_are_recognised(message):
    assert honesty._is_challenge(message)


@pytest.mark.parametrize("message", [
    "what's my cat's name?",
    "open the browser",
    "where did I park?",
    "where is the config file",
    "remember this: my cat is named Quillon",
    "",
])
def test_ordinary_messages_are_not_challenges(message):
    assert not honesty._is_challenge(message)


# --- what the model is told -------------------------------------------------------------------------------------------------------------


def _history(*pairs):
    return [{"role": role, "content": content} for role, content in pairs]


def test_a_challenge_tells_the_model_what_was_checked_and_not_to_defend_or_invent(root):
    history = _history(("user", "config for the TAC?"), ("assistant", f"Check `nodes/ship{NB}profile.html` for the details."))
    note = honesty.challenge_note("where did you pull that from?", history)
    assert "nodes/ship-profile.html" in note and "does not exist" in note
    assert "Do not defend" in note and "invent" in note


def test_the_note_also_checks_a_path_the_user_pasted(root):
    note = honesty.challenge_note("this doesn't exist: nodes/ghost.html", [])
    assert "nodes/ghost.html" in note and "does not exist" in note


def test_a_cited_file_that_does_exist_is_reported_as_existing(root):
    history = _history(("assistant", "It is in nodes/real-node.html."))
    note = honesty.challenge_note("are you sure?", history)
    assert "nodes/real-node.html exists" in note


def test_no_note_for_an_ordinary_message(root):
    assert honesty.challenge_note("what's my cat's name?", _history(("assistant", "See nodes/ghost.html."))) is None


def test_a_challenge_with_no_citations_still_gets_the_rule(root):
    note = honesty.challenge_note("how can I trust you", _history(("assistant", "The sky is green.")))
    assert note and "Do not defend" in note and "does not exist" not in note


def test_the_note_survives_odd_history(root):
    assert honesty.challenge_note("are you sure?", None)
    assert honesty.challenge_note("are you sure?", ["not a dict", {"role": "assistant"}, {"role": "assistant", "content": None}])


# --- wiring -----------------------------------------------------------------------------------------------------------------------------


def test_the_chat_handler_adds_the_challenge_note_to_the_turns_context():
    from python.harness import server

    source = inspect.getsource(server.handle_inference_generate)
    assert "challenge_note(" in source and "system_context_additions" in source


def test_ship_questions_offer_the_star_citizen_lookup_tool():
    from python.harness.tool_selection import ToolIndex, _select_tool_names

    index = ToolIndex([{"name": "starcitizen_lookup", "description": "x"}, {"name": "web_search", "description": "y"}])
    for text in ("what are the specs of the Starlancer TAC?", "tell me about the hull on this ship", "what quantum drive does it use", "galactapedia entry for Stanton"):
        assert "starcitizen_lookup" in _select_tool_names(text, index, 8, available=["starcitizen_lookup", "web_search"]), text
    assert "starcitizen_lookup" not in _select_tool_names("my relationship with the ownership", index, 8, available=["starcitizen_lookup", "web_search"])
