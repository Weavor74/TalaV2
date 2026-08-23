"""
sdk/agent/trajectory.py
=======================
Export what actually happened, in a form you can train on or study.

WHY THIS EXISTS
---------------
Both halves of a trajectory were already being recorded and neither could be got
out. ``conversation_turns`` holds role, content and a ``tool_calls`` column with
the real calls and their results; ``task_event_store`` holds task_init,
phase_start/complete, state_transition and gate_failed. Nothing assembled either
into a run, and nothing exported them.

That matters more here than it would elsewhere. The voice adapter trains on
generated stories, which is defensible for voice -- curated, era-consistent,
emotion-labelled -- but a poor substitute for behaviour. When to reach for a
tool, which one, and how to recover from a failed gate are things only real runs
can teach, and the raw material has been accumulating all along.

TWO STREAMS, NOT ONE
--------------------
There is no join between them and this module does not invent one.
``conversation_turns.session_id`` is a chat session; ``tasks.id`` is an
autonomous run, and the task payload carries no session reference. They are
exported as separate kinds so a consumer can tell which is which, rather than
being stitched together on a guess.

PRIVACY
-------
``conversation_turns`` contains everything ever said to her. Export is therefore
scoped by default and never unbounded: a caller states a session, a task, or a
time window, and ``include_content=False`` produces the structure and tool calls
with the prose stripped -- enough to study tool behaviour without copying the
conversation out of the database.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Rows returned when a caller does not say otherwise. An export is a deliberate
#: act with a scope, not a table dump.
DEFAULT_LIMIT = 50


def _decode(value: Any) -> Any:
    """
    Decode a column that may be JSON, or JSON inside a JSON string.

    Several columns here are double-encoded: a jsonb codec is registered per
    connection while callers pass ``json.dumps(...)`` strings, so the stored
    value is a JSON *string* containing JSON. Decoding once leaves you holding
    a str and quietly iterating its characters.
    """
    for _ in range(3):
        if isinstance(value, (str, bytes)):
            try:
                value = json.loads(value)
            except Exception:
                return value
        else:
            break
    return value


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (value or None)


def _normalise_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """The tool_calls column as a list of {name, args, result} dicts."""
    decoded = _decode(raw)
    if not isinstance(decoded, list):
        return []
    calls = []
    for call in decoded:
        if not isinstance(call, dict):
            continue
        calls.append({
            "name": call.get("name"),
            "args": call.get("args") or {},
            # Truncated: a crawl result can be tens of kilobytes and would
            # dominate the export without adding signal about the decision.
            "result": str(call.get("result", ""))[:2000],
        })
    return calls


async def export_conversation_trajectories(
    *,
    session_id: str | None = None,
    since_hours: int | None = None,
    limit: int = DEFAULT_LIMIT,
    include_content: bool = True,
    tool_calls_only: bool = False,
) -> dict[str, Any]:
    """
    Assemble chat sessions into trajectories.

    ``tool_calls_only`` keeps just the turns that called something, which is the
    subset worth studying for tool behaviour and a small fraction of the volume.
    """
    from python.harness import db

    clauses, args = [], []
    if session_id:
        args.append(session_id)
        clauses.append(f"session_id = ${len(args)}")
    if since_hours:
        args.append(datetime.now(timezone.utc) - timedelta(hours=int(since_hours)))
        clauses.append(f"created_at >= ${len(args)}")
    if tool_calls_only:
        clauses.append("tool_calls IS NOT NULL AND tool_calls::text NOT IN ('null', '[]', '\"[]\"')")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(int(limit))
    sql = (
        "SELECT session_id, role, content, tool_calls, created_at "
        f"FROM conversation_turns {where} ORDER BY created_at DESC LIMIT ${len(args)}"
    )

    try:
        rows = await db.fetch(sql, *args)
    except Exception as exc:
        return {"success": False, "error": f"Could not read conversation turns: {exc}"}

    sessions: dict[str, list[dict[str, Any]]] = {}
    for row in reversed(rows):          # oldest first inside each session
        turn: dict[str, Any] = {
            "role": row["role"],
            "at": _iso(row["created_at"]),
            "tool_calls": _normalise_tool_calls(row["tool_calls"]),
        }
        if include_content:
            turn["content"] = row["content"]
        else:
            # Structure without prose: enough to study which tools were reached
            # for, without copying the conversation out of the database.
            turn["content_chars"] = len(row["content"] or "")
        sessions.setdefault(row["session_id"], []).append(turn)

    trajectories = [
        {
            "kind": "conversation",
            "session_id": sid,
            "turn_count": len(turns),
            "tool_call_count": sum(len(t["tool_calls"]) for t in turns),
            "turns": turns,
        }
        for sid, turns in sessions.items()
    ]
    return {
        "success": True,
        "kind": "conversation",
        "count": len(trajectories),
        "trajectories": trajectories,
        "content_included": include_content,
    }


async def export_task_trajectories(
    *,
    task_id: str | None = None,
    since_hours: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """
    Assemble autonomous runs: the task, its outcome, and its ordered events.

    No conversation is attached, because no link between the two exists in the
    schema. Saying so is better than joining on a guess.
    """
    from python.harness import db

    clauses, args = [], []
    if task_id:
        args.append(task_id)
        clauses.append(f"id = ${len(args)}")
    if since_hours:
        args.append(datetime.now(timezone.utc) - timedelta(hours=int(since_hours)))
        clauses.append(f"created_at >= ${len(args)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(int(limit))

    try:
        tasks = await db.fetch(
            "SELECT id, parent_id, description, status, payload, result, created_at, completed_at "
            f"FROM tasks {where} ORDER BY created_at DESC LIMIT ${len(args)}",
            *args,
        )
    except Exception as exc:
        return {"success": False, "error": f"Could not read tasks: {exc}"}

    trajectories = []
    for task in tasks:
        try:
            events = await db.fetch(
                "SELECT event_type, payload, created_at FROM task_event_store "
                "WHERE task_id = $1 ORDER BY created_at ASC",
                task["id"],
            )
        except Exception:
            events = []

        payload = _decode(task["payload"])
        trajectories.append({
            "kind": "task",
            "task_id": task["id"],
            "parent_id": task["parent_id"],
            "goal": (payload or {}).get("goal") if isinstance(payload, dict) else None,
            "description": task["description"],
            "status": task["status"],
            "started_at": _iso(task["created_at"]),
            "completed_at": _iso(task["completed_at"]),
            "result": _decode(task["result"]),
            "event_count": len(events),
            "events": [
                {
                    "type": e["event_type"],
                    "at": _iso(e["created_at"]),
                    "payload": _decode(e["payload"]),
                }
                for e in events
            ],
        })

    return {"success": True, "kind": "task", "count": len(trajectories), "trajectories": trajectories}


def write_jsonl(trajectories: list[dict[str, Any]], out_path: str | Path) -> dict[str, Any]:
    """
    Write trajectories as JSONL, one per line.

    JSONL because that is what training pipelines read, and because a partial
    write stays parseable up to the last complete line.
    """
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for trajectory in trajectories:
            handle.write(json.dumps(trajectory, ensure_ascii=False, default=str) + "\n")
    return {"success": True, "path": str(path), "count": len(trajectories), "bytes": path.stat().st_size}


def summarise(trajectories: list[dict[str, Any]]) -> dict[str, Any]:
    """
    What the export actually contains.

    The tool histogram is the point: which tools were reached for in real runs,
    and how often a call came back an error. Tool selection was tuned by hand
    against invented prompts -- this is the same question answered from
    behaviour instead.
    """
    from collections import Counter

    tools: Counter = Counter()
    failures: Counter = Counter()
    statuses: Counter = Counter()
    turns = 0

    for trajectory in trajectories:
        if trajectory.get("kind") == "task":
            statuses[trajectory.get("status") or "unknown"] += 1
            continue
        turns += trajectory.get("turn_count", 0)
        for turn in trajectory.get("turns", []):
            for call in turn.get("tool_calls", []):
                name = call.get("name") or "(unnamed)"
                tools[name] += 1
                result = str(call.get("result", "")).lower()
                if "fail" in result or "error" in result:
                    failures[name] += 1

    return {
        "trajectories": len(trajectories),
        "conversation_turns": turns,
        "tool_calls": sum(tools.values()),
        "tools_used": dict(tools.most_common()),
        "tool_failures": dict(failures.most_common()),
        "task_statuses": dict(statuses),
    }
