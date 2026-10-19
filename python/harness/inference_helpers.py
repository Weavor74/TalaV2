"""
python.harness.inference_helpers — tool tag cleanup and fallback notification

Extracted from server.py to meet size ratchet.
Handles stripping raw tool tags from responses and notifying about fallback.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def strip_tool_tags_if_no_tools(text: str, find_tool_call_func: Any) -> str:
    """
    Remove raw <call:...> tags that should not reach the user.
    
    Used when tools are not available or step limit is reached.
    """
    if not text:
        return text
    match = find_tool_call_func(text)
    if match:
        stripped = text[:match.start()].strip()
        if stripped:
            return stripped + "\n\n[Note: Tool execution was not available for this request.]"
        return "[Tool execution was not available for this request.]"
    return text


async def notify_inference_fallback(
    broadcast_func: Any,
    from_provider: str,
    to_provider: str,
    reason: str,
) -> None:
    """
    Broadcast inference.fallback notification when provider fallback occurs.
    
    Makes fallback visible to user in UI.
    """
    try:
        await broadcast_func("inference.fallback", {
            "from": from_provider,
            "to": to_provider,
            "reason": reason
        })
    except Exception as exc:
        logger.debug("Could not broadcast fallback notification: %s", exc)
