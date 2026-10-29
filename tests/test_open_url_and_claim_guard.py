"""
"Open a web browser" (live, 2026-10-28): she answered that she had, and nothing opened. No tool ran that turn -- the 8-tool budget
withheld the browser tools -- and the only browser tools she has drive a headless Chromium nobody sees. Two defects:

1. There was no tool that opens a browser the user can see, and nothing made sure one was on offer when someone asks to open one.
2. Nothing noticed a reply claiming an action no tool had performed. A reply like that is now corrected, in the chat and aloud.
"""
from __future__ import annotations

import webbrowser

import pytest

from python.harness import reply_text
from sdk.agent import tools as tools_mod
from sdk.agent.governance import RiskClassifier



@pytest.fixture
def opened(monkeypatch):
    calls: list[tuple] = []

    def fake_open(url, new=0, autoraise=True):
        calls.append((url, new))
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)
    return calls


def _call(url):
    import asyncio

    func = tools_mod.ToolRegistry._tools["open_url"]["func"]
    result = func(url) if url is not None else func()
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


# --- the tool ---------------------------------------------------------------------------------------------------------

def test_open_url_is_registered_and_graded_medium_risk():
    assert "open_url" in tools_mod.ToolRegistry._tools
    assert RiskClassifier.classify_action("execute_tool", "open_url") == "medium"


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com/path?q=1#frag", "about:blank"])
def test_it_opens_web_pages_in_the_default_browser(opened, url):
    result = _call(url)
    assert result["success"] is True and result["url"] == url
    assert opened and opened[0][0] == url


def test_a_bare_domain_is_taken_as_https(opened):
    result = _call("example.com/page")
    assert result["success"] is True and result["url"] == "https://example.com/page"
    assert opened[0][0] == "https://example.com/page"


@pytest.mark.parametrize("nothing", [None, "", "   "])
def test_with_no_address_it_opens_a_blank_page(opened, nothing):
    """The first live attempt failed with 'no address was given': the model passed an empty string for 'no address'."""
    result = _call(nothing)
    assert result["success"] is True
    assert opened[0][0] == "about:blank"


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/System32/cmd.exe", "javascript:alert(1)", "data:text/html,<script>1</script>", "ftp://example.com/x",
    "chrome://settings", "ms-settings:privacy", "mailto:a@b.c", "https://user:pass@example.com/", "https://exa mple.com",
    "https://example.com/\nsecond", "C:\\Windows\\notepad.exe", "calc", "https://" + "a" * 2100 + ".com",
])
def test_anything_that_is_not_a_plain_web_address_is_refused_and_nothing_opens(opened, url):
    result = _call(url)
    assert result["success"] is False and result["error"]
    assert opened == []


def test_if_the_system_cannot_open_a_browser_it_says_so(monkeypatch):
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: False)
    result = _call("https://example.com")
    assert result["success"] is False


def test_the_tool_imports_no_network_library():
    """The page is the browser's request, not Tala's: this module has no HTTP client or socket to make one with."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "sdk" / "agent" / "open_url.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"socket", "httpx", "requests", "aiohttp", "urllib3", "http", "websockets"}, imported


# --- it is on offer when someone asks for it --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Open a web browser.", "open a web browser please", "can you open the browser", "open google.com", "pull up the website for me",
    "open the link", "launch a browser",
])
def test_the_tool_is_offered_when_the_user_asks_to_open_a_browser_or_site(text):
    from python.harness import tool_selection as ts

    registry = [{"name": t["name"], "description": t["description"]} for t in tools_mod.ToolRegistry._tools.values()]
    index = ts.ToolIndex(registry)
    chosen = ts._select_tool_names(text, index, 8, core=ts._core_tools(8), available=[t["name"] for t in registry])
    assert "open_url" in chosen, chosen


@pytest.mark.parametrize("text", [
    "Open the Start menu on my computer.", "click the start button", "move the mouse to the taskbar and open notepad",
    "press the Windows key", "Can you use my computer to open File Explorer?", "type hello into notepad on my screen",
    "open task manager", "take over my mouse and click OK", "Start menu.", "the taskbar", "  Task Manager!  ",
])
def test_computer_control_is_offered_when_the_user_asks_her_to_drive_the_machine(text):
    """Live: 'Open the Start menu on my computer' offered start_job, open_url and start_swarm, so she explained how to press the
    Windows key instead of doing it. She is meant to move the mouse herself (desktop_run)."""
    from python.harness import tool_selection as ts

    registry = [{"name": t["name"], "description": t["description"]} for t in tools_mod.ToolRegistry._tools.values()]
    index = ts.ToolIndex(registry)
    chosen = ts._select_tool_names(text, index, 8, core=ts._core_tools(8), available=[t["name"] for t in registry])
    assert "desktop_run" in chosen, chosen


@pytest.mark.parametrize("text", ["What time is it?", "Tell me about the moon.", "who is the president"])
def test_ordinary_questions_do_not_pull_in_computer_control(text):
    from python.harness import tool_selection as ts

    registry = [{"name": t["name"], "description": t["description"]} for t in tools_mod.ToolRegistry._tools.values()]
    index = ts.ToolIndex(registry)
    chosen = ts._select_tool_names(text, index, 8, core=ts._core_tools(8), available=[t["name"] for t in registry])
    assert "desktop_run" not in chosen and "open_url" not in chosen, chosen


# --- a reply that claims an action no tool took -----------------------------------------------------------------------

def test_a_claim_with_no_tool_run_is_corrected():
    fixed = reply_text.finish_reply("Sure. I've opened the browser for you.", tools_ran=False)
    assert fixed.startswith("Sure. I've opened the browser for you.")
    assert reply_text.CLAIM_CORRECTION in fixed


@pytest.mark.parametrize("claim", [
    "I opened a new browser window.", "I've launched the application.", "Opening your browser now.", "Done, the browser is open.",
    "I just clicked the link and navigated to the page.", "I sent the email.", "Launching the terminal now.",
])
def test_the_usual_ways_of_claiming_are_caught(claim):
    assert reply_text.CLAIM_CORRECTION in reply_text.finish_reply(claim, tools_ran=False)


def test_the_same_claim_after_a_tool_ran_is_left_alone():
    text = "I've opened the browser for you."
    assert reply_text.finish_reply(text, tools_ran=True) == text


@pytest.mark.parametrize("text", [
    "I started by saying hello.", "I opened with a joke.", "It is nine o'clock.", "I can't open a browser from here.",
    "Would you like me to open the browser?", "Here is the syntax:\n```\nI opened the browser\n```", "I ran the numbers: 4.",
])
def test_ordinary_replies_are_not_corrected(text):
    assert reply_text.finish_reply(text, tools_ran=False) == text


def test_correcting_twice_does_not_stack_corrections():
    once = reply_text.finish_reply("I opened the browser.", tools_ran=False)
    assert reply_text.finish_reply(once, tools_ran=False) == once


def test_finish_reply_still_removes_markup_and_echoed_reminders():
    assert reply_text.finish_reply("Hi.  [SYSTEM REMINDER]: x y z", tools_ran=False) == "Hi."


# --- ... and the correction is spoken, not just shown -----------------------------------------------------------------

def test_the_correction_is_spoken_when_the_reply_was_already_streamed():
    from python.harness.voice_runtime import VoiceRuntime

    class Stream:
        text_seen = True
        fed: list[str] = []
        finished = False

        def feed_text(self, text):
            self.fed.append(text)

        def finish(self):
            self.finished = True

    runtime = object.__new__(VoiceRuntime)
    stream = Stream()
    runtime.stream_for = lambda turn_id: stream
    final = reply_text.finish_reply("I opened the browser.", tools_ran=False)
    assert runtime.turn_done("t1", final) is True
    assert any(reply_text.CLAIM_CORRECTION in piece for piece in stream.fed)
    assert stream.finished


def test_nothing_extra_is_spoken_for_an_ordinary_reply():
    from python.harness.voice_runtime import VoiceRuntime

    class Stream:
        text_seen = True
        fed: list[str] = []

        def feed_text(self, text):
            self.fed.append(text)

        def finish(self):
            pass

    runtime = object.__new__(VoiceRuntime)
    stream = Stream()
    runtime.stream_for = lambda turn_id: stream
    runtime.turn_done("t1", "It is nine o'clock.")
    assert stream.fed == []
