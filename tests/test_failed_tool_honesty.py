"""
When her tool failed, she must not answer as though it had worked.

Found 2026-10-29 in a live test on main. Asked for the Starlancer TAC's default components she called starcitizen_lookup with a guessed
slug; the wiki answered HTTP 422, the tool returned ``{"error": ...}`` (which the registry counted as a success, because the tool never
said ``success: False``), and she then wrote a sixteen-row component table from nothing, with no word about the failure. A turn before,
the model had leaked its chat-template tokens (``|im_end|>`` ...) and carried on writing both sides of a conversation.

Three guards:
* a reply whose every tool call failed, and that does not say so, gets a correction appended (shown and spoken);
* starcitizen_lookup reports a failure as a failure, and a guessed slug falls back to a name search instead of dead-ending;
* chat-template tokens end the reply: whatever follows them is the model talking to itself.
"""
from __future__ import annotations

import inspect
import io
import json
import urllib.error

import pytest

from python.harness import honesty, reply_text

# --- recognising a failed tool result -------------------------------------------------------------------------------------------------------


def test_an_in_band_error_dict_is_a_failure():
    assert "422" in honesty.tool_failure({"error": "HTTPError: HTTP Error 422: Unprocessable Entity", "url": "https://x"})


def test_success_false_is_a_failure_with_its_error_or_a_default():
    assert honesty.tool_failure({"success": False, "error": "boom"}) == "boom"
    assert honesty.tool_failure({"success": False})


def test_the_registrys_failure_strings_are_failures():
    for text in ("Tool execution failed: no such file | Details (the tool's own output): {}", "Error running search: timeout",
                 "Error executing registry tool: oops", "Tool 'nope' is not supported in this chat environment."):
        assert honesty.tool_failure(text), text


def test_a_normal_result_is_not_a_failure():
    assert honesty.tool_failure({"resource": "vehicles", "results": []}) is None
    assert honesty.tool_failure({"success": True, "error": "ignored warning"}) is None
    assert honesty.tool_failure({"error": None, "ok": 1}) is None
    assert honesty.tool_failure("Found 3 matches") is None
    assert honesty.tool_failure(None) is None


# --- the correction on a reply --------------------------------------------------------------------------------------------------------------

TABLE = "**Starlancer TAC** default load-out:\n\n| Category | Component |\n|---|---|\n| Power | 2 x MISC Sphinx fusion core |"
FAILED = [("starcitizen_lookup", "HTTPError: HTTP Error 422: Unprocessable Entity")]


def test_a_reply_after_only_failed_tools_that_says_nothing_gets_a_correction():
    fixed = reply_text.finish_reply(TABLE, True, FAILED)
    assert fixed.startswith(TABLE)
    note = fixed[len(TABLE):]
    assert "Correction:" in note and "starcitizen_lookup" in note and "422" in note and "not checked" in note


def test_the_correction_is_spoken_when_the_reply_was_already_streamed():
    fixed = reply_text.finish_reply(TABLE, True, FAILED)
    assert reply_text.trailing_correction(fixed).startswith("Correction:")


@pytest.mark.parametrize("admission", [
    "The lookup failed, so this is from memory:",
    "I couldn't reach the wiki, so treat this as a guess.",
    "That search returned an error. Here is what I think:",
    "I was unable to verify any of this.",
    "The tool didn't work, but from what I recall:",
])
def test_a_reply_that_admits_the_failure_is_left_alone(admission):
    text = f"{admission}\n{TABLE}"
    assert reply_text.finish_reply(text, True, FAILED) == text


def test_a_retry_that_worked_means_no_correction():
    outcomes = [("starcitizen_lookup", "HTTP 422"), ("web_search", None)]
    assert reply_text.finish_reply(TABLE, True, outcomes) == TABLE


def test_an_earlier_success_does_not_excuse_a_final_failure():
    """The live case: search_memory worked (and found nothing about ships), then both lookups failed."""
    outcomes = [("search_memory", None), ("starcitizen_lookup", "unknown resource"), ("starcitizen_lookup", "HTTP 422")]
    note = reply_text.finish_reply(TABLE, True, outcomes)[len(TABLE):]
    assert "starcitizen_lookup" in note and "search_memory" not in note and "not checked" in note


def test_no_tool_calls_means_no_failure_note():
    assert reply_text.finish_reply(TABLE, False, []) == TABLE
    assert reply_text.finish_reply(TABLE, False) == TABLE


def test_the_correction_is_not_added_twice():
    once = reply_text.finish_reply(TABLE, True, FAILED)
    assert reply_text.finish_reply(once, True, FAILED) == once


def test_two_failed_tools_are_both_named_once():
    outcomes = [("web_search", "timeout"), ("starcitizen_lookup", "HTTP 422"), ("web_search", "timeout")]
    note = reply_text.finish_reply(TABLE, True, outcomes)[len(TABLE):]
    assert note.count("web_search") == 1 and "starcitizen_lookup" in note


def test_an_empty_reply_stays_empty():
    assert reply_text.finish_reply("", True, FAILED) == ""


# --- chat-template tokens end the reply -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["<|im_end|>", "|im_end|>", "<|im_start|>", "|im_start|>", "<|endoftext|>", "<|eot_id|>"])
def test_everything_from_a_template_token_on_is_dropped(marker):
    text = f"I can look that up for you. {marker}\n\nAs a highly advanced AI, here is more.\n|im_start|>assistant\nInvented."
    assert reply_text.finish_reply(text, True) == "I can look that up for you."


def test_a_template_token_inside_a_code_span_is_left_alone():
    text = "The stop token is `<|im_end|>` and it ends a turn."
    assert reply_text.finish_reply(text, True) == text


def test_a_reply_with_no_template_token_is_untouched():
    assert reply_text.finish_reply("A | pipe | table is fine.", True) == "A | pipe | table is fine."


# --- starcitizen_lookup ---------------------------------------------------------------------------------------------------------------------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "Unprocessable Entity", {}, io.BytesIO(b""))


@pytest.fixture
def wiki(monkeypatch):
    """A fake wiki: a slug fetch 422s, a name search finds the ship, anything else 500s. Records the URLs asked for."""
    asked: list[str] = []

    def fake_urlopen(request, timeout=0):
        url = request.full_url
        asked.append(url)
        if "/vehicles/" in url or "/galactapedia/" in url and "filter" not in url:
            raise _http_error(url, 422)
        if "filter" in url:
            return _Resp(json.dumps({"data": [{"name": "Starlancer TAC", "slug": "starlancer-tac", "crew": {"min": 2}}], "meta": {"total": 1}}).encode())
        raise _http_error(url, 500)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return asked


@pytest.mark.asyncio
async def test_a_guessed_slug_falls_back_to_a_name_search(wiki):
    from sdk.agent.tools import starcitizen_lookup

    result = await starcitizen_lookup(resource="vehicles", identifier="starlancer-tac")
    assert result.get("success") is not False and result["results"][0]["name"] == "Starlancer TAC"
    assert "starlancer%20tac" in wiki[-1].lower() or "starlancer+tac" in wiki[-1].lower()


@pytest.mark.asyncio
async def test_a_failure_is_reported_as_a_failure(monkeypatch):
    from sdk.agent.tools import starcitizen_lookup

    def always_500(request, timeout=0):
        raise _http_error(request.full_url, 500)

    monkeypatch.setattr("urllib.request.urlopen", always_500)
    result = await starcitizen_lookup(resource="vehicles", query="anything")
    assert result["success"] is False and "500" in result["error"]
    both = await starcitizen_lookup(resource="vehicles", identifier="a-slug")
    assert both["success"] is False


@pytest.mark.asyncio
async def test_a_query_that_fails_is_not_retried_as_a_search(monkeypatch):
    from sdk.agent.tools import starcitizen_lookup

    asked = []

    def always_422(request, timeout=0):
        asked.append(request.full_url)
        raise _http_error(request.full_url, 422)

    monkeypatch.setattr("urllib.request.urlopen", always_422)
    result = await starcitizen_lookup(resource="vehicles", query="x")
    assert result["success"] is False and len(asked) == 1


@pytest.mark.asyncio
async def test_an_empty_search_says_where_ships_are_kept(monkeypatch):
    """The live case: galactapedia has no article on the TAC (it is a vehicle), so the search came back empty and she was left to guess."""
    from sdk.agent.tools import starcitizen_lookup

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=0: _Resp(json.dumps({"data": [], "meta": {"total": 0}}).encode()))
    result = await starcitizen_lookup(resource="galactapedia", query="starlancer tac")
    assert result["returned"] == 0 and "vehicles" in result["hint"]


def test_the_description_says_to_search_by_name_first():
    from sdk.agent.tools import ToolRegistry

    entry = next(t for t in ToolRegistry.get_tools() if t["name"] == "starcitizen_lookup")
    description = entry["description"]
    assert "query" in description and "identifier" in description


# --- wiring ---------------------------------------------------------------------------------------------------------------------------------


def test_the_chat_handler_records_every_tool_outcome_and_passes_them_on():
    from python.harness import server

    source = inspect.getsource(server.handle_inference_generate)
    assert "reply_log.record(" in source and "finish_reply(generated_text, bool(sticky_tools), reply_log)" in source
