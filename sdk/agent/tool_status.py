"""
sdk/agent/tool_status.py -- the chat's live "Worked for Ns" card, started and (now) finished.

ToolRegistry.run_tool broadcast "in progress" when a tool started and never said it had finished, so the card stayed blue and its
timer kept counting up for as long as the window was open, under an answer that had already arrived. The card ends on a status of
``complete`` or ``failed``.

A burst of tool calls is one card. It is closed ``CLOSE_AFTER_S`` after the LAST running tool finishes, so that:
  * a long tool (a 30-minute command) never reads "complete" while it is still running,
  * several quick tools in one reply do not each leave a card behind, and
  * a failure anywhere in the burst shows as failed, which is the more useful colour to see.
Everything here is fire-and-forget and silent on failure: a status card must never be able to break a tool call.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

TASK_ID = "active_tool"
#: How long after the last tool finishes the card is closed; a following tool in the same reply cancels it.
CLOSE_AFTER_S = 2.0

_running = 0
_failed: str | None = None
_close_handle: asyncio.TimerHandle | None = None


def _reset_for_tests() -> None:
    global _running, _failed, _close_handle
    if _close_handle is not None:
        _close_handle.cancel()
    _running, _failed, _close_handle = 0, None, None


def _action_type(name: str) -> str:
    n = name.lower()
    if "read" in n or "explore" in n:
        return "read_file"
    if "write" in n or "edit" in n:
        return "write_file"
    if "search" in n or "query" in n:
        return "search_workspace"
    if "command" in n:
        return "run_command"
    return "thinking"


def _target(args: dict[str, Any]) -> str:
    for key in ("query", "path", "file_path", "command"):
        if key in args:
            return str(args[key])
    return str(next(iter(args.values()))) if args else ""


def _broadcast(phase: str, status: str, message: str, details: dict[str, Any] | None = None) -> None:
    try:
        from sdk.agent.telemetry import LiveStatusReporter

        asyncio.get_running_loop().create_task(
            LiveStatusReporter.broadcast_status(task_id=TASK_ID, phase=phase, status=status, message=message, details=details)
        )
    except Exception as exc:  # no loop, no reporter: the tool call carries on regardless
        log.debug("tool status not sent: %s", exc)


def started(name: str, args: dict[str, Any]) -> None:
    """A tool began. Announced as in progress, exactly as before; cancels a pending close."""
    global _running, _close_handle
    _running += 1
    if _close_handle is not None:
        _close_handle.cancel()
        _close_handle = None
    _broadcast(f"Executing {name}", "in_progress", f"Executing tool {name}",
               {"action_type": _action_type(name), "target": _target(args)})


def finished(name: str, ok: bool, error: str | None = None) -> None:
    """A tool ended. When none are left running the card is closed shortly afterwards."""
    global _running, _failed, _close_handle
    _running = max(0, _running - 1)
    if not ok and _failed is None:
        _failed = f"{name} failed: {error}" if error else f"{name} failed"
    if _running:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _close_handle is not None:
        _close_handle.cancel()
    _close_handle = loop.call_later(CLOSE_AFTER_S, _close)


def _close() -> None:
    global _failed, _close_handle
    failure, _failed, _close_handle = _failed, None, None
    if failure:
        _broadcast("Finished", "failed", failure)
    else:
        _broadcast("Finished", "complete", "Finished")
