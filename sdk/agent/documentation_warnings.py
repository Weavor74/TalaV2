"""Preserve behavioral uncertainty separately from generated documentation prose."""
import re
from html import unescape

UNVERIFIED_MARK = "[Behaviour unverified]"

UNVERIFIED_NOTICE = (
    f"  <p>{UNVERIFIED_MARK} The source of this module has changed since this "
    "description was derived, and re-deriving produced the same text — the "
    "change was inside code this description does not cover (a function body, "
    "not a name or a docstring). Treat the behaviour described here as "
    "unverified and read the source before relying on it.</p>\n"
)


def _strip_unverified_notice(html: str) -> str:
    html = re.sub(rf"&lt;p&gt;\s*{re.escape(UNVERIFIED_MARK)}.*?&lt;/p&gt;\s*", "", html, flags=re.DOTALL)
    return re.sub(rf"\s*<p>\s*{re.escape(UNVERIFIED_MARK)}.*?</p>", "", html, flags=re.DOTALL)


def _carried_unverified_notice(previous_content: str | None) -> bool:
    return bool(previous_content) and UNVERIFIED_MARK in previous_content


def _semantic_memory_text(html: str) -> str:
    """
    The stored prose as plain words, for comparing against what was just
    derived.

    Tags have to come out. What is passed in is raw prose; what is stored has
    been rendered into paragraphs, so comparing them with the markup left in
    never matches — which silently disabled the notice-preservation below
    until a test caught it.
    """
    match = re.search(r'<section id="semantic-memory">(.*?)</section>', html, re.DOTALL)
    if not match:
        return ""
    text = _strip_unverified_notice(match.group(1))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(unescape(text).split())


def description_baseline(html: str) -> str:
    """Source revision before an unverified change, retained across refreshes."""
    baseline = re.search(r'data-description-baseline-digest="(sha256:[a-fA-F0-9]{64})"', html)
    source = re.search(r'data-source-digest="(sha256:[a-fA-F0-9]{64})"', html)
    return baseline.group(1) if baseline else source.group(1) if source else ""


def carried_notice(previous: str | None, prose: str, digest: str) -> tuple[str, str]:
    """Keep the warning until prose changes or source returns to its baseline."""
    baseline = description_baseline(previous or "")
    if (_carried_unverified_notice(previous)
            and _semantic_memory_text(previous or "") == " ".join(prose.split())
            and (not baseline or digest != baseline)):
        return UNVERIFIED_NOTICE, baseline
    return "", ""
