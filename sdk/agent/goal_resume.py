"""
sdk/agent/goal_resume.py -- auto-resume of budget-suspended goals.

Moved out of goal_engine.py (which had a size budget) with no change to what the sweep does, except the retry cap: it now holds
however the resumed run behaves. See ``_find_resumable_suspended_goals``. ``goal_engine`` re-exports the three public names,
because the scheduler and the tests reach them there.
"""
from __future__ import annotations

import logging
from typing import Any

from sdk.agent.persistence import TaskEventStore

log = logging.getLogger(__name__)


def _engine():
    """goal_engine, imported when needed: it imports this module at its top, and owns the active-goal registry and GoalEngine."""
    from sdk.agent import goal_engine

    return goal_engine


async def _find_resumable_suspended_goals(max_resume_attempts: int = 5) -> list[dict[str, Any]]:
    """
    Find goal-parent tasks BLOCKED specifically by BatchPolicy budget
    exhaustion (the `suspended` path in GoalEngine.run_goal) that are still
    eligible for automatic resume.

    Regression this closes: a goal that outran its BatchPolicy step/time
    budget transitioned to BLOCKED and just sat there forever -- nothing
    ever resumed it. run_goal already has everything needed to pick a goal
    back up where it left off (Ghost-blockchain phase-index restore, plus
    the prior-phase-output threading above); the only missing piece was
    something to actually call it again.

    Deliberately excludes:
      - swarm topology runs (payload carries "topology_id") -- those block
        for gate/human-review reasons via a completely different mechanism
        (SwarmEngine.evaluate_edges' requires_human policy) and must stay
        human-gated, never auto-resumed.
      - a goal whose task_id is already registered as actively executing
        (get_active_task_ids()) -- avoids launching a second concurrent
        run_goal() for the same goal on an overlapping tick.
      - a goal that has already been suspended >= max_resume_attempts times
        (counted from its own persisted events, not an in-memory counter, so
        this survives a process restart) -- a goal that can never finish must
        not retry forever.

    The count is the larger of two records, because neither alone holds:
    ``state_transition -> blocked`` events on the row (each time a run of this
    same root is suspended again) and ``resume_attempt`` events written when
    a resume is launched. The first is not enough: a resume re-enters
    run_goal(description), which builds its own root from a hash of the text,
    so the suspended row may never be touched again and its count never rises
    -- observed 2026-10-24 as "attempt 2/5" every three minutes, forever. The
    second is written on the row being resumed whatever the run does. The
    larger, not the sum: a run that re-blocks the same row would otherwise
    count twice and stop at half the cap.
    """
    from python.harness import db
    import json as _json

    try:
        db.get_pool()
    except RuntimeError:
        return []

    try:
        # 'blocked' ONLY -- never 'paused'. The two statuses exist to be
        # different: 'blocked' means the system suspended this goal (budget
        # exhausted) and it should carry on, 'paused' means a human stopped it
        # via task.control and it must stay stopped. task.control used to write
        # 'blocked' for a pause, so this query relaunched human-paused work on
        # every app start and there was no way to make a task stay stopped.
        rows = await db.fetch(
            "SELECT id, description, payload FROM tasks WHERE status = 'blocked' AND parent_id IS NULL"
        )
    except Exception as exc:
        log.warning("find_resumable_suspended_goals: query failed: %s", exc)
        return []

    active_ids = _engine().get_active_task_ids()
    resumable: list[dict[str, Any]] = []

    for row in rows:
        task_id = row["id"]
        if task_id in active_ids:
            continue

        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        payload = payload or {}
        if "topology_id" in payload:
            continue  # swarm run -- human-gated, must not auto-resume

        try:
            events = await TaskEventStore.fetch_events(task_id)
        except Exception as exc:
            log.warning("find_resumable_suspended_goals: failed to fetch events for %s: %s", task_id[:8], exc)
            continue

        blocked_count = sum(
            1 for e in events
            if e.get("event_type") == "state_transition" and e.get("payload", {}).get("to_status") == "blocked"
        )
        launches = sum(1 for e in events if e.get("event_type") == "resume_attempt")
        attempts = max(blocked_count, 1 + launches)
        if attempts >= max_resume_attempts:
            if not any(e.get("event_type") == "resume_exhausted" for e in events):
                # Said once, when it happens: this used to be a debug line, which is how a goal retried for an hour unseen.
                log.warning(
                    "GoalEngine: goal %s stopped retrying after %d/%d resume attempts and is left blocked for a person to "
                    "look at: %r", task_id[:8], attempts, max_resume_attempts, (row["description"] or "")[:80],
                )
                try:
                    await TaskEventStore.append_event(task_id, "resume_exhausted", {"attempts": attempts, "max": max_resume_attempts})
                except Exception as exc:
                    log.warning("find_resumable_suspended_goals: could not record the stop for %s: %s", task_id[:8], exc)
            continue

        goal_text = row["description"]
        if not goal_text:
            continue

        resumable.append({"task_id": task_id, "goal_text": goal_text, "attempt": attempts})

    return resumable


async def _resume_one_goal(goal_text: str, task_id: str) -> None:
    """
    Fire-and-forget re-entry into run_goal() for one suspended goal.

    The ROOT goal id is registered as active for the whole run. Without this,
    find_resumable_suspended_goals never saw a resumed goal as running:
    register_active_task is called by the DAG executor with the *phase* task
    id, and the root's id is never registered by anyone. The row also stays at
    'blocked' until the run finishes, so every scheduler tick re-selected the
    same goal and launched another concurrent copy.

    Measured 2026-08-14: three goals relaunched every 60s -- twice inside two
    seconds at one point -- each spawning a fresh DAG with its own synthesis
    LLM calls. Ollama serialises, so within minutes an interactive chat message
    queued behind a dozen goal executions and timed out, then cascaded through
    the exhausted remote providers. The user saw "one or two chat rounds, then
    it fails"; the cause was the resume loop multiplying, not the chat path.

    resume_suspended_goals registers the id before create_task; this call is
    the idempotent backstop for any other caller, and the finally clause is
    what actually releases it.
    """
    _engine().register_active_task(task_id)
    try:
        from sdk.agent.batch_policy import BatchPolicy
        engine = _engine().GoalEngine()
        # overnight() is the preset built for exactly this: long, bounded,
        # unattended continuation, rather than the tighter interactive-chat
        # default budget that likely triggered the original suspension.
        await engine.run_goal(goal_text, policy=BatchPolicy.overnight())
    except Exception:
        log.exception("GoalEngine: auto-resume crashed for suspended goal %s", task_id[:8])
    finally:
        _engine().unregister_active_task(task_id)


async def resume_suspended_goals(max_resume_attempts: int = 5) -> int:
    """
    Auto-resume every eligible budget-suspended goal (see
    find_resumable_suspended_goals). Each resume re-enters run_goal() with
    the exact same goal text -- via the semantic decomposition cache and
    the existing Ghost-blockchain phase-index restore, this picks the goal
    back up at the phase it left off on with a fresh execution budget,
    instead of replaying everything from scratch or requiring a human to
    manually retrigger it.

    Intended to be called periodically (see
    HeartbeatScheduler._evaluate_tick, which already runs a comparable
    per-tick sweep for ZombieReaper) so a goal that outruns a single
    BatchPolicy budget keeps making real progress across multiple bounded
    runs -- segmented long-running execution driven by the persisted
    task-styled state, not by holding everything in one context.

    Returns the number of goals handed off for resume this call.
    """
    import asyncio

    candidates = await _find_resumable_suspended_goals(max_resume_attempts)
    launched = 0
    for candidate in candidates:
        task_id = candidate["task_id"]
        # Mark active HERE, not inside the coroutine. create_task only schedules
        # it -- the body may not run before the next scheduler tick queries
        # get_active_task_ids(), and that window was wide enough in practice to
        # launch the same goal twice within two seconds.
        if task_id in _engine().get_active_task_ids():
            log.debug("GoalEngine: goal %s already running, not resuming again", task_id[:8])
            continue
        _engine().register_active_task(task_id)
        try:
            # Written on the row being resumed, before the run starts, so the cap counts launches however the run behaves.
            await TaskEventStore.append_event(
                task_id, "resume_attempt", {"attempt": candidate["attempt"] + 1, "max": max_resume_attempts})
        except Exception as exc:
            _engine().unregister_active_task(task_id)
            log.warning("GoalEngine: not resuming goal %s: could not record the attempt (%s)", task_id[:8], exc)
            continue
        log.info(
            "GoalEngine: auto-resuming suspended goal %s (attempt %d/%d): %r",
            task_id[:8], candidate["attempt"] + 1, max_resume_attempts,
            candidate["goal_text"][:80],
        )
        asyncio.create_task(_resume_one_goal(candidate["goal_text"], task_id))
        launched += 1

    return launched
