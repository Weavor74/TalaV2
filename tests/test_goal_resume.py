"""
tests/test_goal_resume.py
==========================
Regression coverage for auto-resuming BatchPolicy budget-suspended goals
(sdk/agent/goal_engine.py::find_resumable_suspended_goals /
resume_suspended_goals).

Before this, a goal that ran out of its execution budget (BatchPolicy
max_steps/max_duration_minutes) transitioned its parent task to BLOCKED
and just sat there forever -- run_goal already has everything needed to
pick a goal back up where it left off (Ghost-blockchain phase-index
restore, plus prior-phase-output threading), but nothing ever called it
again automatically.

Real DB (tasks + task_event_store), no mocking of BatchQueue/TaskEventStore
-- these tests build fixture rows the same way production code does, via
BatchQueue.enqueue_task/transition_task, not raw SQL.
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from python.harness import db
from python.harness.db import close_pool, init_pool
from sdk.agent.batch_policy import BatchPolicy
from sdk.agent.goal_engine import (
    find_resumable_suspended_goals,
    register_active_task,
    resume_suspended_goals,
    unregister_active_task,
)
from sdk.agent.queue import BatchQueue, TaskStatus

_TEST_TASK_IDS = [
    "test-resume-plain-goal",
    "test-resume-swarm-parent",
    "test-resume-exhausted-goal",
    "test-resume-active-goal",
    "test-resume-relaunch-goal",
]


async def _cleanup():
    for tid in _TEST_TASK_IDS:
        await db.execute("DELETE FROM tasks WHERE id = $1", tid)
        await db.execute("DELETE FROM task_event_store WHERE task_id = $1", tid)


async def _make_blocked_goal(task_id: str, goal_text: str, payload: dict | None = None, blocked_cycles: int = 1) -> None:
    await BatchQueue.enqueue_task(task_id, goal_text, "in_progress", payload=payload or {})
    for _ in range(blocked_cycles):
        await BatchQueue.transition_task(task_id, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, reason="Suspended: budget exhausted")


@pytest.mark.asyncio
async def test_find_resumable_excludes_swarm_exhausted_and_active_goals():
    await init_pool()
    try:
        await _cleanup()

        await _make_blocked_goal("test-resume-plain-goal", "Plain suspended goal")
        await _make_blocked_goal("test-resume-swarm-parent", "Swarm goal", payload={"topology_id": "swarm-code-repair"})
        await _make_blocked_goal("test-resume-exhausted-goal", "Exhausted goal", blocked_cycles=5)
        await _make_blocked_goal("test-resume-active-goal", "Actively running goal")

        register_active_task("test-resume-active-goal")
        try:
            candidates = await find_resumable_suspended_goals(max_resume_attempts=5)
        finally:
            unregister_active_task("test-resume-active-goal")

        ids = {c["task_id"] for c in candidates}
        assert "test-resume-plain-goal" in ids, "an ordinary budget-suspended goal must be resumable"
        assert "test-resume-swarm-parent" not in ids, "swarm runs are human-gated and must never auto-resume"
        assert "test-resume-exhausted-goal" not in ids, "a goal at the retry cap must not resume again"
        assert "test-resume-active-goal" not in ids, "a goal already running must not get a second concurrent run_goal()"

        plain = next(c for c in candidates if c["task_id"] == "test-resume-plain-goal")
        assert plain["goal_text"] == "Plain suspended goal"
        assert plain["attempt"] == 1
    finally:
        await _cleanup()
        await close_pool()


@pytest.mark.asyncio
async def test_resume_suspended_goals_relaunches_run_goal_with_overnight_policy():
    await init_pool()
    try:
        await _cleanup()
        await _make_blocked_goal("test-resume-relaunch-goal", "Goal that needs relaunching")

        with patch("sdk.agent.goal_engine.GoalEngine.run_goal", new_callable=AsyncMock) as mock_run_goal:
            mock_run_goal.return_value = {"success": True}
            count = await resume_suspended_goals(max_resume_attempts=5)
            # resume_suspended_goals launches resumes via asyncio.create_task
            # (fire-and-forget, matching the rest of this codebase's pattern
            # for background work) -- give it a turn to actually run.
            await asyncio.sleep(0.1)

        assert count == 1
        mock_run_goal.assert_awaited_once()
        call_args = mock_run_goal.call_args
        assert call_args[0][0] == "Goal that needs relaunching"
        policy_used = call_args.kwargs["policy"]
        assert policy_used.max_steps == BatchPolicy.overnight().max_steps
        assert policy_used.max_duration_minutes == BatchPolicy.overnight().max_duration_minutes
    finally:
        await _cleanup()
        await close_pool()


@pytest.mark.asyncio
async def test_resume_suspended_goals_no_candidates_returns_zero():
    await init_pool()
    try:
        await _cleanup()
        with patch("sdk.agent.goal_engine.GoalEngine.run_goal", new_callable=AsyncMock) as mock_run_goal:
            count = await resume_suspended_goals(max_resume_attempts=5)
        assert count == 0
        mock_run_goal.assert_not_called()
    finally:
        await close_pool()
