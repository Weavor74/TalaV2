"""
sdk/agent/web_browser.py
========================
Interactive browser automation tools (Playwright/Chromium).

Unlike research.search / research.crawl (fetch-and-read), these tools drive
a real browser session the agent can navigate, click, and type into —
logins, forms, dashboards, JS-rendered apps.

One persistent headless Chromium page is shared across calls so state
(cookies, login sessions, the current page) survives between tool
invocations. `browser_read` tags visible interactive elements with
`data-tala-idx` attributes and returns them as an indexed list; pass an
index to `browser_click` / `browser_type` to interact. Tags are re-applied
on every read, so always read after navigation before interacting.

Playwright is an optional dependency: every tool degrades to a clear
install hint if it (or its Chromium build) is missing.
"""
from __future__ import annotations

import logging
from typing import Any

from sdk.agent.tools import register_tool

log = logging.getLogger(__name__)

_pw = None
_browser = None
_page = None

_INSTALL_HINT = (
    "Playwright is not available. Install with: pip install playwright && python -m playwright install chromium"
)

_TAG_ELEMENTS_JS = """
() => {
  const selector = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [onclick]';
  const els = Array.from(document.querySelectorAll(selector)).filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  return els.slice(0, 150).map((e, i) => {
    e.setAttribute('data-tala-idx', String(i));
    return {
      index: i,
      tag: e.tagName.toLowerCase(),
      type: e.type || '',
      text: (e.innerText || e.value || e.placeholder || e.getAttribute('aria-label') || '').trim().slice(0, 80),
      href: e.href || ''
    };
  });
}
"""


async def _ensure_page():
    """Start Playwright + Chromium once and reuse the page across calls."""
    global _pw, _browser, _page
    if _page is not None and not _page.is_closed():
        return _page
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(_INSTALL_HINT)

    if _pw is None:
        _pw = await async_playwright().start()
    if _browser is None or not _browser.is_connected():
        _browser = await _pw.chromium.launch(headless=True)
    context = await _browser.new_context(
        viewport={"width": 1366, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    )
    _page = await context.new_page()
    return _page


async def _mirror(page) -> None:
    """
    Show the visible browser tab where Tala just went.

    Her tools drive a headless Chromium, so watching the Web Browser view told
    you nothing about what she was doing (UI-013). This reflects her page into
    the visible ``<webview>`` so you can watch.

    It is a mirror, not a handover. She keeps her own browser: the visible tab
    follows her, and she never waits on it, competes with it, or loses a page
    because somebody scrolled. If the mirror cannot be delivered, that is not a
    navigation failure and must not be reported as one — nobody may be looking
    at the window, and there may not be a window at all.
    """
    try:
        url = page.url
        if not url or url == "about:blank":
            return
        from python.harness.server import broadcast_notification

        await broadcast_notification("browser.mirror", {
            "url": url,
            "title": await page.title(),
        })
    except Exception as exc:
        log.debug("Could not mirror the browser page: %s", exc)


async def _page_summary(page, max_chars: int = 3000) -> dict[str, Any]:
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    return {
        "url": page.url,
        "title": await page.title(),
        "text": (text or "").strip()[:max_chars],
    }


@register_tool(
    name="browser_navigate",
    description="Open a URL in the persistent automation browser (headless Chromium). Returns the page title and text. Use browser_read to list clickable/typable elements.",
)
async def browser_navigate(url: str) -> dict[str, Any]:
    """
    NAME
           browser_navigate - load a URL in the interactive automation browser

    SYNOPSIS
           browser_navigate(url: str) -> dict[str, Any]
    """
    try:
        page = await _ensure_page()
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(500)
        summary = await _page_summary(page)
        await _mirror(page)
        return {"success": True, **summary}
    except Exception as exc:
        return {"success": False, "error": f"Navigation failed: {exc}"}


@register_tool(
    name="browser_read",
    description="Read the current automation-browser page: title, visible text, and an indexed list of interactive elements (links, buttons, inputs). Use the indexes with browser_click / browser_type.",
)
async def browser_read(max_chars: int = 4000) -> dict[str, Any]:
    """
    NAME
           browser_read - read page text and enumerate interactive elements

    SYNOPSIS
           browser_read(max_chars: int = 4000) -> dict[str, Any]
    """
    try:
        page = await _ensure_page()
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    try:
        summary = await _page_summary(page, int(max_chars))
        elements = await page.evaluate(_TAG_ELEMENTS_JS)
        return {"success": True, **summary, "elements": elements}
    except Exception as exc:
        return {"success": False, "error": f"Read failed: {exc}"}


@register_tool(
    name="browser_click",
    description="Click an element in the automation browser, addressed by the index from browser_read (or a CSS selector). Returns the resulting page state.",
)
async def browser_click(index: int | None = None, selector: str | None = None) -> dict[str, Any]:
    """
    NAME
           browser_click - click an element by browser_read index or CSS selector

    SYNOPSIS
           browser_click(index: int | None = None, selector: str | None = None) -> dict[str, Any]
    """
    try:
        page = await _ensure_page()
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    target = selector or (f'[data-tala-idx="{int(index)}"]' if index is not None else None)
    if not target:
        return {"success": False, "error": "Provide either 'index' (from browser_read) or 'selector'."}
    try:
        await page.click(target, timeout=10000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(500)
        summary = await _page_summary(page)
        await _mirror(page)
        return {"success": True, **summary}
    except Exception as exc:
        return {"success": False, "error": f"Click failed on {target}: {exc}"}


@register_tool(
    name="browser_type",
    description="Type text into an input in the automation browser, addressed by the index from browser_read (or a CSS selector). Optionally press Enter afterwards.",
)
async def browser_type(
    text: str,
    index: int | None = None,
    selector: str | None = None,
    press_enter: bool = False,
) -> dict[str, Any]:
    """
    NAME
           browser_type - fill an input by browser_read index or CSS selector

    SYNOPSIS
           browser_type(text: str, index: int | None = None, selector: str | None = None, press_enter: bool = False) -> dict[str, Any]
    """
    try:
        page = await _ensure_page()
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    target = selector or (f'[data-tala-idx="{int(index)}"]' if index is not None else None)
    if not target:
        return {"success": False, "error": "Provide either 'index' (from browser_read) or 'selector'."}
    try:
        await page.fill(target, text, timeout=10000)
        if press_enter:
            await page.press(target, "Enter")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
        await page.wait_for_timeout(300)
        summary = await _page_summary(page)
        await _mirror(page)
        return {"success": True, **summary}
    except Exception as exc:
        return {"success": False, "error": f"Type failed on {target}: {exc}"}


@register_tool(
    name="browser_screenshot",
    description="Screenshot the current automation-browser page, save it under data/browser_shots/, and return the path. Pair with analyze_screen-style vision by passing the file to a vision provider.",
)
async def browser_screenshot() -> dict[str, Any]:
    """
    NAME
           browser_screenshot - capture the automation browser viewport to a PNG

    SYNOPSIS
           browser_screenshot() -> dict[str, Any]
    """
    import datetime
    from sdk.agent.tools import _get_workspace_root

    try:
        page = await _ensure_page()
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    try:
        out_dir = _get_workspace_root() / "data" / "browser_shots"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"shot-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        await page.screenshot(path=str(out_path))
        return {"success": True, "path": str(out_path), "url": page.url}
    except Exception as exc:
        return {"success": False, "error": f"Screenshot failed: {exc}"}


@register_tool(
    name="browser_close",
    description="Close the persistent automation browser session, discarding cookies and login state.",
)
async def browser_close() -> dict[str, Any]:
    """
    NAME
           browser_close - shut down the automation browser session

    SYNOPSIS
           browser_close() -> dict[str, Any]
    """
    global _pw, _browser, _page
    try:
        if _page is not None and not _page.is_closed():
            await _page.close()
        if _browser is not None and _browser.is_connected():
            await _browser.close()
        if _pw is not None:
            await _pw.stop()
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        _pw = _browser = _page = None
