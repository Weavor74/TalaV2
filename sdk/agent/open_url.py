"""
sdk/agent/open_url.py -- open a web page in the user's own, visible browser.

Her other browser tools (``browser_navigate`` and friends) drive a headless Chromium that nobody sees, so "open a web browser" had no
tool that did what the person meant, and she said she had done it anyway. This one hands a plain web address to the operating system's
default browser and nothing else.

What it will and will not open: ``http`` and ``https`` addresses, a bare domain such as ``example.com`` (taken as https), and
``about:blank``. Everything else is refused: ``file:``, ``javascript:``, ``data:`` and every other scheme, which would launch some
other program or run script, addresses carrying a username and password, anything with whitespace or control characters, and
anything over 2048 characters. Nothing is fetched by this process: the page is the browser's request, not hers.

It is graded medium risk in ``sdk/agent/governance.py`` and, like every tool, passes the halt gate, the lease gate and the
authority level in ``ToolRegistry.run_tool``.
"""
from __future__ import annotations

import re
import webbrowser
from typing import Any
from urllib.parse import urlsplit

from sdk.agent.tools import register_tool

MAX_URL_CHARS = 2048
_BARE_DOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}(?:[/?#].*)?$")


def _address(raw: str | None) -> tuple[str | None, str]:
    """``(address, "")`` for something safe to open, or ``(None, why not)``."""
    # "No address" arrives as None or as an empty string (the first live attempt sent ""): both mean a blank page.
    text = str(raw).strip() if raw is not None else ""
    text = text or "about:blank"
    if len(text) > MAX_URL_CHARS:
        return None, f"the address is longer than {MAX_URL_CHARS} characters"
    if re.search(r"[\s\x00-\x1f\x7f]", text):
        return None, "the address contains spaces or control characters"
    if text == "about:blank":
        return text, ""
    if "://" not in text and _BARE_DOMAIN.match(text):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None, "that is not a valid web address"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None, "only http and https web addresses are opened"
    if parts.username or parts.password:
        return None, "addresses with a username or password are not opened"
    return text, ""


@register_tool(
    name="open_url",
    description=(
        "Open a web page or website in the user's own visible default web browser: use this when asked to open a browser, a website, "
        "a link or a page. Takes a web address (https://...); with none it opens a blank page. Unlike browser_navigate, which is "
        "headless and shows nothing, the user sees this one."
    ),
)
async def open_url(url: str | None = None) -> dict[str, Any]:
    """
    NAME
           open_url - show a web page in the user's default browser

    SYNOPSIS
           open_url(url: str | None = None) -> dict[str, Any]
    """
    address, why = _address(url)
    if address is None:
        return {"success": False, "error": f"Not opened: {why}."}
    try:
        opened = webbrowser.open(address, new=2)
    except Exception as exc:  # noqa: BLE001 - the operating system's answer, reported as is
        return {"success": False, "error": f"The browser could not be started: {exc}"}
    if not opened:
        return {"success": False, "error": "The system reported that no browser could be opened."}
    return {"success": True, "url": address, "opened_in": "the default web browser"}
