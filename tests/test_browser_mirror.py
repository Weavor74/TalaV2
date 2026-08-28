"""
Watching Tala browse.

Her six browser tools drive a persistent headless Chromium. The visible Web
Browser tab is a separate Electron ``<webview>`` she cannot reach, so watching
it told you nothing about what she was doing (UI-013) — the only thing that ever
moved it was ``research.search`` pointing it at a results URL as a side effect.

Steven chose a **mirror**, not a handover: the tab follows her page so you can
watch, and she keeps her own browser. She never navigates out from under you
mid-page, never waits on the window, and never loses a page because somebody
scrolled. The moment you steer, following stops and says so.

The related findings turned out to be already built. ``ui_navigate``,
``ui_describe``, ``ui_click`` and ``ui_reload`` are registered tools over the
same notification channel (UI-119 Route B, shipped as UI-007), and the six
``browser_*`` tools are registered too — so UI-016's "no ``@register_tool``
wraps any of them" was stale. What was genuinely missing is this.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def renderer() -> str:
    return (ROOT / "renderer" / "renderer.js").read_text(encoding="utf-8")


# -- the tools exist and are reachable ----------------------------------------

def test_the_browser_tools_are_registered():
    """UI-016 said no @register_tool wrapped any browser control. Six do."""
    from sdk.agent.tools import ToolRegistry

    names = {t["name"] if isinstance(t, dict) else getattr(t, "name", "")
             for t in ToolRegistry.get_tools()}
    assert {"browser_navigate", "browser_read", "browser_click",
            "browser_type", "browser_screenshot", "browser_close"} <= names


def test_the_ui_tools_are_registered():
    """UI-119's Route B, already shipped: Tala can operate her own window over
    the notification channel rather than through a debugging port."""
    from sdk.agent.tools import ToolRegistry

    names = {t["name"] if isinstance(t, dict) else getattr(t, "name", "")
             for t in ToolRegistry.get_tools()}
    assert {"ui_navigate", "ui_describe", "ui_click", "ui_reload"} <= names


def test_no_debugging_port_is_opened():
    """Chrome DevTools Protocol has no authentication: a port would grant
    arbitrary JavaScript execution and full app control to any local process,
    sidestepping per-agent privileges and lockdown entirely."""
    for name in ("main.js", "package.json"):
        for candidate in (ROOT / "electron" / name, ROOT / name):
            if candidate.exists():
                assert "remote-debugging-port" not in candidate.read_text(
                    encoding="utf-8", errors="ignore"), candidate


# -- the mirror ----------------------------------------------------------------

def test_every_tool_that_moves_the_page_mirrors_it():
    """Navigating is not the only way she changes page — a click or a typed
    Enter does too, and a mirror that only follows navigate goes stale
    silently."""
    from sdk.agent import web_browser

    for name in ("browser_navigate", "browser_click", "browser_type"):
        source = inspect.getsource(getattr(web_browser, name))
        assert "_mirror(page)" in source, f"{name} does not mirror"


@pytest.mark.asyncio
async def test_the_mirror_broadcasts_where_she_went(monkeypatch):
    from sdk.agent import web_browser

    sent = []

    class Page:
        url = "https://example.com/page"

        async def title(self):
            return "Example"

    from python.harness import server

    async def broadcast(method, params=None):
        sent.append((method, params))
        return 1
    monkeypatch.setattr(server, "broadcast_notification", broadcast)

    await web_browser._mirror(Page())

    assert sent == [("browser.mirror",
                     {"url": "https://example.com/page", "title": "Example"})]


@pytest.mark.asyncio
async def test_a_blank_page_is_not_mirrored(monkeypatch):
    """Reflecting about:blank would blank the tab you are looking at for no
    reason."""
    from python.harness import server
    from sdk.agent import web_browser

    sent = []

    async def broadcast(method, params=None):
        sent.append(method)
        return 1
    monkeypatch.setattr(server, "broadcast_notification", broadcast)

    class Blank:
        url = "about:blank"

        async def title(self):
            return ""

    await web_browser._mirror(Blank())
    assert sent == []


@pytest.mark.asyncio
async def test_a_failed_mirror_is_not_a_failed_navigation(monkeypatch):
    """Nobody may be looking at the window, and there may not be a window at
    all. Her browsing must not depend on it."""
    from python.harness import server
    from sdk.agent import web_browser

    async def boom(method, params=None):
        raise RuntimeError("no clients")
    monkeypatch.setattr(server, "broadcast_notification", boom)

    class Page:
        url = "https://example.com"

        async def title(self):
            return "Example"

    await web_browser._mirror(Page())   # must not raise


# -- it follows, it does not take over ----------------------------------------

def test_the_tab_stops_following_when_you_steer():
    """A tab that yanks itself away while you are reading is worse than one
    that never moved."""
    source = renderer()

    assert "takeOver()" in source
    start = source.index("const WebBrowser = {")
    body = source[start:source.index("\n};", start)]
    # the address bar and both navigation buttons hand control back to you
    assert body.count("this.takeOver();") >= 3


def test_following_resumes_only_when_asked():
    source = renderer()
    start = source.index("takeOver() {")
    body = source[start:source.index("_renderFollowState()", start)]

    assert "this._following = false" in body
    assert "this._following = true" not in body, "it resumed by itself"


def test_the_mirror_does_not_drive_her_browser():
    """A mirror, not a handover: she keeps her own page, so nothing the window
    does can reach back into her session."""
    source = renderer()
    start = source.index("mirror({ url, title }) {")
    body = source[start:source.index("takeOver()", start)]

    assert "loadURL" in body or "src =" in body
    assert "python:rpc" not in body, "the mirror is talking back to her browser"
