"""
sdk/agent/dreaming.py -- the nightly dream: consolidation that runs by itself.

``GoalEngine.dream_cycle`` existed, and so did ``process_feedback``, and nothing ever called either. This module is
what runs the dream, riding the heartbeat tick the way the reflection loop does (``dream_if_due`` is the sweep, the
tick only asks).

When it runs
------------
Once a day, after a configurable local hour (``learning.json``: ``dream_hour``, default 3; ``dream_enabled``), when the
system has been idle, learning is on and the emergency halt is not engaged. Halted or off means nothing is read or
written at all.

What a dream does
-----------------
1. Snapshots every node file.
2. Applies the slow decay to nodes learning has used and left idle (``node_learning.decay_unused``).
3. Runs the existing ``GoalEngine.dream_cycle``.
4. **Checks what it did.** No protected node changed; every weight is still inside [0.05, 1.0]; only nodes learning had
   touched were modified; no node was deleted. Any exception or violated check restores every file to the snapshot (a
   rollback), noted in the log and the ledger.
5. Writes a dream-journal node either way, at a low weight so a journal never outranks real memory, and records the run.

``dream_if_due`` marks today as attempted whether or not the dream was rolled back: a failing dream must not be retried
every minute for the rest of the night.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_DREAM_HOUR = 3
IDLE_MINUTES = 5.0
#: A journal is a record of what happened, not something to recall ahead of what was learned.
JOURNAL_WEIGHT = 0.30


def _nodes_dir() -> Path:
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    return resolve_active_nodes_dir()


def _engine():
    """A GoalEngine for the dream. It has run no goal, so it holds no adapter: ``check_dream_trigger`` reads
    ``active_adapter_buffer``, which only ``run_goal`` ever sets, so a fresh engine raised AttributeError when asked
    whether the system was idle (nothing had ever asked). Said here because goal_engine.py is at its size pin."""
    from sdk.agent import node_learning as nl
    from sdk.agent.goal_engine import GoalEngine

    engine = GoalEngine(root_dir=str(nl.user_data_dir()))
    engine.active_adapter_buffer = None
    return engine


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _violation(snapshot: dict[Path, bytes]) -> str | None:
    """Why a finished dream must be rolled back, or None if what it did is acceptable."""
    from sdk.agent import node_learning as nl

    for path, before in snapshot.items():
        try:
            after = path.read_bytes()
        except OSError:
            return f"a node file was deleted: {path.name}"
        if after == before:
            continue
        old, new = nl.describe_bytes(before), nl.describe_bytes(after)
        if old is None or new is None:
            return f"a node file stopped being a readable node: {path.name}"
        if old["protected"]:
            return f"a protected node was changed: {old['node_id']}"
        if not old["touched"]:
            return f"a node learning had never touched was changed: {old['node_id']}"
        for field in ("relevance", "confidence"):
            if not nl.MIN_WEIGHT - 1e-9 <= new[field] <= nl.MAX_WEIGHT + 1e-9:
                return f"a weight went out of bounds on {old['node_id']}: {field}={new[field]}"
    return None


def _journal_text(date: str, *, uses: int, positive: int, negative: int, nodes: list[str], decayed: int,
                  legacy: str, error: str | None) -> str:
    feedback = (f"{_plural(positive, 'piece', 'pieces')} of positive feedback and "
                f"{_plural(negative, 'piece', 'pieces')} of negative feedback")
    parts = [
        f"Dream journal for {date}.",
        f"Since the last dream: {_plural(uses, 'use', 'uses')} raised a node's relevance (a use past the daily cap changes nothing, "
        f"so it is not counted here), {feedback}.",
    ]
    if nodes:
        parts.append("Nodes involved: " + ", ".join(nodes[:10]) + (" and more." if len(nodes) > 10 else "."))
    parts.append(f"Idle decay: {_plural(decayed, 'node', 'nodes')} decayed.")
    if legacy:
        parts.append(f"Consolidation: {legacy}")
    parts.append(f"This dream was rolled back ({error}). Every node is as it was before it began." if error
                 else "Result: consolidated; no check failed.")
    return "\n\n".join(parts)


def _write_journal(now: datetime, text: str) -> str | None:
    from sdk.agent import node_learning as nl
    from sdk.agent.documentation_nodes import create_documentation_node

    nodes_dir = _nodes_dir()
    base = f"dream-journal-{now.date().isoformat()}"
    node_id, n = base, 1
    while (nodes_dir / f"{node_id}.html").exists():
        n += 1
        node_id = f"{base}-{n}"
    try:
        result = create_documentation_node(
            node_id=node_id, title=f"Dream journal {now.date().isoformat()}", doc_target="memory.dream-journal",
            semantic_memory=text, node_type="documentation", tags=["html-nn", "memory", "dream-journal"], stage=False,
            purpose="record",  # what happened on a night, not a description of code: it answers to no source file
        )
        if result.get("path"):
            nl.seed_new_node(Path(result["path"]), relevance=JOURNAL_WEIGHT, confidence=JOURNAL_WEIGHT, now=now)
        return result.get("node_id")
    except Exception as exc:
        log.warning("dreaming: could not write the dream journal: %s", exc)
        return None


async def run_dream(*, force: bool = False, now: datetime | None = None) -> dict[str, Any] | None:
    """Run one dream. None when learning is off or halted. ``force`` skips only the schedule, never the halt."""
    from sdk.agent import node_learning as nl

    if not nl.learning_active():
        return None
    now = (now or datetime.now(UTC)).astimezone(UTC)
    nodes_dir = _nodes_dir()
    since = _parse_moment(nl.read_config().get("last_dream_at"))
    snapshot = {p: p.read_bytes() for p in sorted(nodes_dir.glob("*.html"))}

    error: str | None = None
    decayed = 0
    legacy = ""
    try:
        decayed = len(nl.decay_unused(nodes_dir, now=now))
        legacy = str(await _engine().dream_cycle() or "")
        error = _violation(snapshot)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    if error is not None:
        changed = [p for p, before in snapshot.items() if not p.exists() or p.read_bytes() != before]
        nl.restore({str(p): snapshot[p] for p in changed})
        nl.note_rollback(error, [p.stem for p in changed], now=now)
        log.error("dreaming: the dream was rolled back (%s); %d node file(s) restored", error, len(changed))
        decayed = 0

    events = nl.events_since(since)
    uses = sum(1 for e in events if e.get("kind") == "use")
    positive = sum(1 for e in events if e.get("kind") == "feedback_up")
    negative = sum(1 for e in events if str(e.get("kind", "")).startswith("feedback_") and e.get("kind") != "feedback_up")
    involved = list(dict.fromkeys(e["node_id"] for e in events if e.get("kind") in ("use",) or
                                  str(e.get("kind", "")).startswith("feedback_")))
    journal_id = _write_journal(now, _journal_text(
        now.date().isoformat(), uses=uses, positive=positive, negative=negative, nodes=involved, decayed=decayed,
        legacy=legacy, error=error))
    nl.write_config({"last_dream_at": now.isoformat()})
    return {"status": "rolled-back" if error else "consolidated", "decayed": decayed, "uses": uses,
            "feedback": positive + negative, "journal_node_id": journal_id, "legacy": legacy,
            **({"error": error} if error else {})}


def _parse_moment(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw)).astimezone(UTC) if raw else None
    except ValueError:
        return None


async def system_idle(minutes: float) -> bool:
    """No task is pending or running and the last one finished more than ``minutes`` ago: the system has nothing to do. Shared by the
    nightly dream and the voice-adapter training trigger."""
    return await _engine().check_dream_trigger(minutes)


async def dream_if_due(now: datetime | None = None) -> dict[str, Any] | None:
    """The heartbeat's question: is it time to dream? None means "did not run" -- distinct from "ran and found nothing"."""
    from sdk.agent import node_learning as nl

    if not nl.learning_active():
        return None
    config = nl.read_config()
    if not config.get("dream_enabled", True):
        return None
    local = now if now is not None else datetime.now()
    if local.tzinfo is not None:
        local = local.astimezone().replace(tzinfo=None)
    if local.hour < int(config.get("dream_hour", DEFAULT_DREAM_HOUR)):
        return None
    if config.get("last_dream_date") == local.date().isoformat():
        return None

    if not await system_idle(IDLE_MINUTES):
        return None
    outcome = await run_dream(now=local.astimezone(UTC))
    # Attempted today whether or not it was rolled back, so a failing dream is not retried every minute.
    nl.write_config({"last_dream_date": local.date().isoformat()})
    return outcome
