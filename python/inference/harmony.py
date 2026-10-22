"""
harmony.py -- reading gpt-oss's harmony output.

gpt-oss speaks the harmony format: a reply is a series of channel messages,
``analysis`` (hidden reasoning), ``commentary`` (tool calls, addressed
``to=functions.<name>``) and ``final`` (what the user sees). llama-server with
``--jinja`` parses that for us and returns OpenAI-shaped output: visible text in
``content``, reasoning in ``reasoning_content`` and calls in ``tool_calls``.
At low reasoning effort a tool-calling turn arrives as native ``tool_calls``
with empty text.

Two things live here:

* ``normalize_tool_calls`` -- the native ``tool_calls`` shape, with the
  ``functions.`` recipient prefix removed and empty arguments made ``{}``.
* ``split_harmony`` -- a fallback for the case where raw harmony markers reach
  the text stream anyway (a server build whose parser is not engaged). It
  drops the analysis channel, lifts tool calls out of the commentary channel
  and returns only the final text.

Hidden reasoning is never returned from either function.
"""

from __future__ import annotations

import json
import re
from typing import Any

_MARKER = re.compile(r"<\|(start|end|message|channel|constrain|call|return)\|>")
_MESSAGE = re.compile(
    r"(?P<header>(?:<\|start\|>|<\|channel\|>).*?)<\|message\|>"
    r"(?P<body>.*?)(?:<\|end\|>|<\|call\|>|<\|return\|>|\Z)",
    re.S,
)
_CHANNEL = re.compile(r"<\|channel\|>\s*([a-z]+)")
_RECIPIENT = re.compile(r"to=(?:functions\.)?([A-Za-z0-9_.\-]+)")


def has_raw_markers(text: str) -> bool:
    return bool(text) and _MARKER.search(text) is not None


def _clean_name(name: str) -> str:
    return name[len("functions."):] if name.startswith("functions.") else name


def normalize_tool_calls(calls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI-shaped ``tool_calls`` -> ``[{"name": str, "arguments": str}]``."""
    out: list[dict[str, Any]] = []
    for call in calls or []:
        fn = call.get("function") or call
        name = _clean_name(str(fn.get("name") or "").strip())
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args)
        out.append({"name": name, "arguments": (args or "").strip() or "{}"})
    return out


def split_harmony(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Raw harmony text -> (visible text, tool calls). Reasoning is discarded.

    Text with no harmony markers is returned unchanged with no calls.
    """
    if not has_raw_markers(text):
        return text, []

    visible: list[str] = []
    calls: list[dict[str, Any]] = []
    for m in _MESSAGE.finditer(text):
        header = m.group("header") or ""
        channel_match = _CHANNEL.search(header)
        channel = channel_match.group(1).lower() if channel_match else ""
        body = (m.group("body") or "").strip()
        recipient = _RECIPIENT.search(header)
        if recipient and channel in ("commentary", ""):
            calls.append({"name": _clean_name(recipient.group(1)), "arguments": body or "{}"})
        elif channel == "analysis":
            continue
        elif channel in ("final", "") and body:
            visible.append(body)
    return "\n".join(visible).strip(), calls
