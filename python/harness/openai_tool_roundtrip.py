"""
python.harness.openai_tool_roundtrip — native tool_calls across turns.

NAME
       openai_tool_roundtrip - preserve OpenAI-shaped tool_calls (Gemini extras)

SYNOPSIS
       from python.harness.openai_tool_roundtrip import (
           accumulate_streamed_tool_calls,
           tool_calls_to_call_tags,
           normalize_tool_calls,
           native_tool_followup_messages,
           is_thought_signature_error,
       )

DESCRIPTION
       The harness dispatches on ``<call:name attr="v" />`` text tags. Native
       providers still speak ``tool_calls``. Gemini 3 (OpenAI-compatible
       ``/v1/chat/completions``) attaches ``extra_content.google.thought_signature``
       to those tool_calls and returns HTTP 400 on the next turn unless that
       object is sent back verbatim on the assistant message.

       Converting a tool call to a tag and feeding the next turn only the tag
       drops the signature, so a web_search follow-up silently fails and the
       chain falls through to the embedded CPU engine. This module is the
       adapter: keep extras on the way in, rebuild a native assistant +
       role=tool pair on the way out, and still render the tag the turn loop
       already understands.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _copy_tool_call_extras(source: dict[str, Any], dest: dict[str, Any]) -> None:
    """
    NAME
           _copy_tool_call_extras - copy vendor fields a follow-up turn must echo

    SYNOPSIS
           _copy_tool_call_extras(source: dict, dest: dict) -> None

    DESCRIPTION
           Gemini 3 puts the thought signature on ``extra_content``. Other
           vendors use ``extra_body`` or a top-level ``thought_signature``.
           Copy whatever arrived; do not invent a signature.
    """
    for key in ("extra_content", "extra_body", "thought_signature"):
        if key in source and source[key] not in (None, "", {}, []):
            dest[key] = source[key]


def accumulate_streamed_tool_calls(
    delta_tool_calls: Any,
    accumulator: dict[int, dict[str, Any]],
) -> None:
    """
    NAME
           accumulate_streamed_tool_calls - join streamed tool_call fragments

    SYNOPSIS
           accumulate_streamed_tool_calls(delta_tool_calls, accumulator) -> None

    DESCRIPTION
           OpenAI-style streaming splits a tool call across deltas: the name
           arrives once, then ``arguments`` as string fragments keyed by
           ``index``. Vendor extras (Gemini thought_signature) may arrive on
           the first delta or a later one; they are merged, not replaced by
           an empty later fragment.
    """
    if not isinstance(delta_tool_calls, list):
        return
    for tc in delta_tool_calls:
        if not isinstance(tc, dict):
            continue
        idx = tc.get("index", 0)
        if not isinstance(idx, int):
            idx = 0
        slot = accumulator.setdefault(
            idx,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if tc.get("id"):
            slot["id"] = tc["id"]
        if tc.get("type"):
            slot["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        fragment = fn.get("arguments")
        if isinstance(fragment, str):
            slot["function"]["arguments"] += fragment
        elif isinstance(fragment, dict):
            slot["function"]["arguments"] = json.dumps(fragment)
        _copy_tool_call_extras(tc, slot)


def normalize_tool_calls(tool_calls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """
    NAME
           normalize_tool_calls - copy tool_calls including vendor extras

    SYNOPSIS
           normalize_tool_calls(tool_calls) -> list[dict]

    DESCRIPTION
           Produce a JSON-serialisable list safe to put back on the next
           request. Empty or malformed entries are dropped. A missing ``id``
           is synthesised so a follow-up ``role=tool`` message can point at it.
    """
    out: list[dict[str, Any]] = []
    for i, tc in enumerate(tool_calls or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        arguments = fn.get("arguments", "")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments)
        elif arguments is None:
            arguments = ""
        item: dict[str, Any] = {
            "id": tc.get("id") or f"call_{i}",
            "type": tc.get("type") or "function",
            "function": {"name": name, "arguments": arguments},
        }
        _copy_tool_call_extras(tc, item)
        out.append(item)
    return out


def tool_calls_to_call_tags(tool_calls: list[dict[str, Any]]) -> str:
    """
    NAME
           tool_calls_to_call_tags - render native tool_calls as <call:/> tags

    SYNOPSIS
           tool_calls_to_call_tags(tool_calls: list[dict]) -> str

    DESCRIPTION
           The turn loop dispatches on ``<call:name attr="v" />``. Native
           function-calling is an input/output adapter at the provider
           boundary; everything after this stays protocol-agnostic.
    """
    tags = []
    for tc in tool_calls or []:
        fn = tc.get("function", {}) or {}
        fname = fn.get("name", "")
        if not fname:
            continue
        fargs_raw = fn.get("arguments", "")
        try:
            fargs = json.loads(fargs_raw) if isinstance(fargs_raw, str) else fargs_raw
            if isinstance(fargs, dict):
                parts = []
                for k, v in fargs.items():
                    if isinstance(v, str):
                        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
                        parts.append(f'{k}="{escaped}"')
                    else:
                        parts.append(f'{k}={json.dumps(v, separators=(",", ":"))}')
                args_str = " ".join(parts)
            else:
                args_str = str(fargs_raw)
        except Exception:
            args_str = str(fargs_raw)
        tags.append(f'<call:{fname} {args_str} />')
    return "\n".join(tags)


def native_tool_followup_messages(
    content: str,
    tool_calls: list[dict[str, Any]],
    tool_name: str,
    tool_result: Any,
) -> list[dict[str, Any]]:
    """
    NAME
           native_tool_followup_messages - assistant tool_calls + role=tool result

    SYNOPSIS
           native_tool_followup_messages(content, tool_calls, tool_name, tool_result)

    DESCRIPTION
           Gemini 3 (and any OpenAI-compat model that attached extras) must
           see the original ``tool_calls`` objects, including
           ``extra_content.google.thought_signature``, plus a ``role=tool``
           result. A ``<call:/>`` / ``<response:/>`` text pair is not enough
           and produces HTTP 400 on turn 2.
    """
    calls = normalize_tool_calls(tool_calls)
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": content or None,
        "tool_calls": calls,
    }
    call_id = ""
    for tc in calls:
        fn = tc.get("function") or {}
        if fn.get("name") == tool_name:
            call_id = str(tc.get("id") or "")
            break
    if not call_id and calls:
        call_id = str(calls[0].get("id") or "call_0")
    if not call_id:
        call_id = "call_0"
    if isinstance(tool_result, str):
        result_text = tool_result
    else:
        result_text = json.dumps(tool_result, default=str)
    return [
        assistant,
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool_name,
            "content": result_text,
        },
    ]


def is_thought_signature_error(err_text: str) -> bool:
    """
    NAME
           is_thought_signature_error - detect a Gemini thought_signature 400

    SYNOPSIS
           is_thought_signature_error(err_text: str) -> bool

    DESCRIPTION
           A 400 that mentions thought_signature is not "this provider does
           not support tools". Retrying without the tools array still 400s
           and then the chain silently falls through to the embedded engine.
    """
    lower = (err_text or "").lower()
    return "thought_signature" in lower or "thought signature" in lower
