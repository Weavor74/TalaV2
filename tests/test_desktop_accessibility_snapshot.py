"""
"Open the Start menu on my computer" reached desktop_run for the first time (live, 2026-10-28) and failed within eight seconds:
"Cannot inspect desktop controls: TimeoutExpired". The Windows UI Automation helper is a fresh subprocess; the first time it asks a
Chromium/Electron window for its accessibility tree the window has to build one, which can take far longer than eight seconds, and
the next ask is quick. One deadline of eight seconds with no second try turned that first slow ask into a failed task.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from sdk.agent import desktop_accessibility as da


@pytest.fixture(autouse=True)
def _as_windows(monkeypatch):
    monkeypatch.setattr(da.sys, "platform", "win32")


def _ok(elements=3):
    return subprocess.CompletedProcess(["x"], 0, stdout=json.dumps({"elements": list(range(elements))}), stderr="")


def test_a_slow_first_inspection_is_retried_and_succeeds(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(kw["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return _ok()

    monkeypatch.setattr(da.subprocess, "run", run)
    result = da.snapshot(1234)
    assert result == {"elements": [0, 1, 2]}
    assert len(calls) == 2
    assert calls[0] > 8, "the first deadline is longer than the eight seconds that failed live"


def test_two_timeouts_report_how_long_it_waited(monkeypatch):
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(da.subprocess, "run", run)
    result = da.snapshot(1234)
    assert result["elements"] == []
    assert "TimeoutExpired" in result["error"] and "after" in result["error"]


def test_a_helper_that_fails_is_not_retried(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(1)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(da.subprocess, "run", run)
    assert da.snapshot(1234) == {"elements": [], "error": "Accessibility helper failed"}
    assert len(calls) == 1


def test_without_a_window_or_off_windows_nothing_is_run(monkeypatch):
    def run(*a, **k):
        raise AssertionError("ran a subprocess")

    monkeypatch.setattr(da.subprocess, "run", run)
    assert da.snapshot(0)["error"] == "Windows accessibility unavailable"
    monkeypatch.setattr(da.sys, "platform", "linux")
    assert da.snapshot(1234)["error"] == "Windows accessibility unavailable"


# --- the endpoint a model writes when it has none ---------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["local", "localhost", "ollama", "default", "Local", "", "  "])
def test_desktop_run_takes_a_word_for_the_local_endpoint(monkeypatch, word):
    """Live: three desktop_run calls in a row were refused with 'endpoint must be a loopback address' because the model wrote 'local'."""
    import asyncio

    from sdk.agent import desktop_agent

    monkeypatch.setattr(desktop_agent, "_native_supported", lambda: True)
    # max_steps=0 stops the call right after the endpoint check, before anything touches the desktop
    result = asyncio.run(desktop_agent.desktop_run("open the start menu", endpoint=word, max_steps=0))
    assert result["error"] == "Provide a task and max_steps between 1 and 40", result


def test_desktop_run_still_refuses_an_endpoint_that_is_not_this_machine(monkeypatch):
    import asyncio

    from sdk.agent import desktop_agent

    monkeypatch.setattr(desktop_agent, "_native_supported", lambda: True)
    result = asyncio.run(desktop_agent.desktop_run("x", endpoint="http://example.com:11434", max_steps=0))
    assert "loopback" in result["error"]
