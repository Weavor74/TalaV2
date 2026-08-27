"""
self_diagnosis.py — Tala reads her own logs and health.

Steven's stated purpose for the logging panel:

    "Logging. This should log everything. From commands sent to RPC failed, to
    undetected port access. It should also be part of the primary method for
    self diagnosis on Tala"

The second sentence had no mechanism (UI-086). Four logging RPCs existed --
``logging.get_events``, ``logging.get_health``, ``logging.archive_events``,
``logging.clear_events`` -- and every one was UI-only. None was a registered
tool, and there is no generic RPC-invoke tool, so every signal on that screen
was visible to the operator and invisible to the agent whose diagnosis it was
supposed to be.

One asymmetry is deliberate and is Steven's rule (Q-19): **she can read the
logs; she cannot clear them.** Nothing here archives, deletes or truncates.
An agent that can erase the record of what it did is not auditable, and the
clear path stays where it is -- behind a person clicking a button.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sdk.agent.tools import register_tool

log = logging.getLogger(__name__)

#: Levels worth surfacing by default. DEBUG and INFO are 27,776 rows of noise
#: on a real install and drown the thing being looked for.
DEFAULT_LEVELS = ("ERROR", "CRITICAL", "WARNING")

MAX_LIMIT = 200


@register_tool(
    name="read_own_health",
    description=(
        "Read Tala's own measured subsystem health: which of the twelve "
        "services are healthy, degraded, critical or unmeasured, and the "
        "evidence behind each verdict. Use this before claiming a subsystem "
        "works or does not."
    ),
)
async def read_own_health() -> dict:
    """
    NAME
           read_own_health - what is actually wrong with this system right now

    SYNOPSIS
           read_own_health() -> dict

    DESCRIPTION
           Returns the same measured state the LOGGING panel shows, including
           the reason for each verdict -- "8 of 8 providers online", "no MCP
           servers configured" -- so a diagnosis can cite evidence rather than
           a colour.

           A subsystem nothing can measure reports UNKNOWN, never HEALTHY, and
           an UNKNOWN present makes the overall verdict PARTIAL rather than
           rounding up to healthy.
    """
    from python.harness.server import handle_logging_get_health

    raw = await handle_logging_get_health({}, "self-diagnosis")
    payload = json.loads(raw)
    if "error" in payload:
        return {"success": False, "error": payload["error"].get("message", "unknown")}

    result = payload.get("result", {})
    services = result.get("services", {})
    details = result.get("details", {})

    problems = [
        {"service": name, "status": status, "why": details.get(name, "")}
        for name, status in services.items()
        if status not in ("HEALTHY",)
    ]
    return {
        "success": True,
        "overall": result.get("overall"),
        "measured": result.get("measured"),
        "unmeasured": result.get("unmeasured", []),
        # Ordered so the thing to act on is first rather than buried in twelve
        # rows of green.
        "needs_attention": problems,
        "all_services": {
            name: {"status": status, "why": details.get(name, "")}
            for name, status in services.items()
        },
    }


@register_tool(
    name="read_own_logs",
    description=(
        "Read Tala's own event log: RPC failures, warnings and errors recorded "
        "by the harness. Filter by level, by substring, or by how far back to "
        "look. Read-only -- Tala cannot clear or archive her own logs."
    ),
)
async def read_own_logs(
    levels: str = "",
    search: str = "",
    minutes: int = 60,
    limit: int = 50,
) -> dict:
    """
    NAME
           read_own_logs - the harness event log, from the agent's side

    SYNOPSIS
           read_own_logs(levels, search, minutes, limit) -> dict

    DESCRIPTION
           Defaults to errors and warnings from the last hour, because INFO is
           tens of thousands of rows on a real install and drowns whatever is
           being looked for.

           Each entry carries the failing RPC method and its trace where one
           was recorded, so "something broke" becomes "auth.get_credentials
           raised KeyError".

           Read-only by design (Q-19: "She cannot clear her logs but I should
           be able too"). An agent that can erase the record of what it did is
           not auditable.

    PARAMETERS
           levels  - comma-separated, e.g. "ERROR,WARNING". Default: errors,
                     criticals and warnings.
           search  - substring to match in the message
           minutes - how far back to look. 0 means no time limit.
           limit   - maximum rows, capped at 200
    """
    from python.harness import db

    wanted = [
        lvl.strip().upper() for lvl in str(levels or "").split(",") if lvl.strip()
    ] or list(DEFAULT_LEVELS)
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = 50
    try:
        minutes = max(0, int(minutes))
    except (TypeError, ValueError):
        minutes = 60

    clauses = ["level = ANY($1)"]
    args: list[Any] = [wanted]
    if minutes:
        clauses.append(f"created_at > NOW() - INTERVAL '{minutes} minutes'")
    if search:
        args.append(f"%{search}%")
        clauses.append(f"message ILIKE ${len(args)}")
    args.append(limit)

    sql = (
        "SELECT level, message, trace, created_at FROM agent_log WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY created_at DESC LIMIT ${len(args)}"
    )

    try:
        rows = await db.fetch(sql, *args)
    except Exception as exc:
        # An error reading the error log is worth saying plainly rather than
        # returning an empty list that reads as "nothing went wrong".
        return {
            "success": False,
            "error": f"Could not read agent_log: {type(exc).__name__}: {exc}",
        }

    events = []
    for row in rows:
        trace = row["trace"]
        if isinstance(trace, str):
            try:
                trace = json.loads(trace)
            except (ValueError, json.JSONDecodeError):
                trace = {"raw": trace}
        events.append({
            "level": row["level"],
            "message": row["message"],
            "method": (trace or {}).get("method", ""),
            "trace": trace or {},
            "at": str(row["created_at"]),
        })

    return {
        "success": True,
        "count": len(events),
        "window_minutes": minutes or None,
        "levels": wanted,
        "events": events,
        # Distinguishes "nothing went wrong" from "nothing is being recorded",
        # which is the failure UI-085 was: 44,464 rows and one ERROR.
        "note": (
            "No matching events. This means nothing matched the filter, not "
            "necessarily that nothing went wrong."
            if not events else ""
        ),
    }


@register_tool(
    name="diagnose_self",
    description=(
        "Run a full self-diagnosis: measured subsystem health plus the recent "
        "errors and warnings behind it, in one call. Use this when asked what "
        "is wrong, or before reporting that something works."
    ),
)
async def diagnose_self(minutes: int = 60) -> dict:
    """
    NAME
           diagnose_self - health and the evidence for it, together

    SYNOPSIS
           diagnose_self(minutes: int = 60) -> dict

    DESCRIPTION
           Health alone says a subsystem is degraded; the log says why. Asking
           for both separately is the common case, so this does it in one call
           and returns them side by side.

           Read-only, like the two tools it composes.
    """
    health = await read_own_health()
    logs = await read_own_logs(minutes=minutes, limit=25)

    return {
        "success": bool(health.get("success")),
        "overall": health.get("overall"),
        "needs_attention": health.get("needs_attention", []),
        "unmeasured": health.get("unmeasured", []),
        "recent_errors": logs.get("events", []),
        "log_note": logs.get("note", "") or logs.get("error", ""),
    }
