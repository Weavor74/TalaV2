"""
python/harness/reply_text.py -- what the user sees, and in a voice turn hears, of a model's reply.

A reply ended with ``<call><code><call name="update_user_model" focus="memory" max_turns="3"/></code></call>``: the model writing
a tool call in a shape the harness does not parse, so it was neither run nor removed, and it sat in the chat bubble and was read
aloud. Markup like that is a protocol, not speech. This removes it, and only it: tags inside backticks or a code fence are left
alone, because she may be explaining the syntax.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_PROTECTED = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_SELF_CLOSING = re.compile(r"<call\b[^>]*?/>", re.IGNORECASE)
_PAIRED = re.compile(r"<call\b[^>]*>.*?</call(?::[\w.]+)?>", re.IGNORECASE | re.DOTALL)
_LONE_TAG = re.compile(r"</?call\b[^>]*>", re.IGNORECASE)
_EMPTY_CODE = re.compile(r"<code>\s*</code>", re.IGNORECASE)


def _strip_segment(segment: str) -> str:
    found = False
    for pattern in (_SELF_CLOSING, _PAIRED, _LONE_TAG, _EMPTY_CODE):
        segment, n = pattern.subn("", segment)
        found = found or bool(n)
    if not found:
        return segment
    segment = re.sub(r"[ \t]{2,}", " ", segment)           # the gap a removed tag leaves
    return re.sub(r"[ \t]+\n", "\n", segment)


def strip_stray_calls(text: str) -> str:
    """``text`` without tool-call markup that was never parsed; unchanged (not even re-trimmed) if there is none."""
    if "<call" not in text.lower() and "</call" not in text.lower():
        return text
    parts = _PROTECTED.split(text)
    cleaned = [part if index % 2 else _strip_segment(part) for index, part in enumerate(parts)]
    result = "".join(cleaned)
    if result == text:
        return text
    log.info("reply_text: removed tool-call markup that was not parsed from a reply")
    return result.strip()
