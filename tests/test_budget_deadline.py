"""
The time budget has to be a deadline, not a phase-entry gate.

Measured on the benchmark: a five-minute ceiling overran to 5.3 and then 6.4
minutes, because `is_budget_exceeded()` was only consulted before starting a
phase. Whatever a phase did after that ran to completion however long it took,
and the overshoot grew with the amount of work in the phase -- so the fix that
made a step write all the files it names also made the overrun worse.

Checked between nodes rather than by cancelling: a node in flight finishes, so
nothing is interrupted half-written, and the overrun is bounded by one node
instead of one phase.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sdk.agent.batch_policy import BatchPolicy, BudgetTracker
from sdk.agent.graph_engine import DAGExecutionEngine
from sdk.agent.planner import TaskItem


# --- the tracker knows when to stop -------------------------------------------

def test_remaining_is_the_budget_at_the_start():
    tracker = BudgetTracker(BatchPolicy(max_duration_minutes=5.0))
    assert tracker.remaining_minutes() == pytest.approx(5.0, abs=0.05)


def test_remaining_never_goes_negative():
    tracker = BudgetTracker(BatchPolicy(max_duration_minutes=5.0))
    tracker.start_time = datetime.now(UTC) - timedelta(minutes=90)
    assert tracker.remaining_minutes() == 0.0


def test_the_deadline_is_the_start_plus_the_budget():
    tracker = BudgetTracker(BatchPolicy(max_duration_minutes=5.0))
    assert tracker.deadline() == tracker.start_time + timedelta(minutes=5.0)


def test_a_deadline_in_the_past_is_reported_as_exceeded():
    tracker = BudgetTracker(BatchPolicy(max_duration_minutes=5.0))
    tracker.start_time = datetime.now(UTC) - timedelta(minutes=6)
    assert tracker.deadline() < datetime.now(UTC)
    assert tracker.is_budget_exceeded() is True
    assert "6.0/5.0" in tracker.budget_reason()


# --- the executor honours it --------------------------------------------------

@pytest.mark.asyncio
async def test_a_passed_deadline_stops_before_any_node_runs(tmp_path):
    """
    The whole point: the executor must not start work it has no budget for.
    A run that begins already over its deadline does nothing at all.
    """
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    task = TaskItem(
        task_id="deadline-1",
        description="Create a new file named thing.py with a helper",
        dependencies=[],
    )

    result = await engine.execute_task_as_dag(
        task, None, deadline=datetime.now(UTC) - timedelta(seconds=1)
    )

    assert result["success"] is False
    assert result.get("deadline_reached") is True
    assert "Deadline reached" in result["error"]
    assert not list(tmp_path.rglob("thing.py")), "nothing should have been written"


@pytest.mark.asyncio
async def test_no_deadline_behaves_exactly_as_before(tmp_path, monkeypatch):
    """
    Callers that pass nothing must be unaffected -- the deadline is opt-in, and
    every existing call site relied on the old behaviour.
    """
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    task = TaskItem(task_id="deadline-2", description="Do a thing", dependencies=[])

    seen: dict[str, object] = {}

    async def _fake_inner(self, t, p=None, d=None):
        seen["deadline"] = d
        return {"success": True}

    monkeypatch.setattr(DAGExecutionEngine, "_execute_task_as_dag_inner", _fake_inner)
    result = await engine.execute_task_as_dag(task)

    assert result["success"] is True
    assert seen["deadline"] is None


@pytest.mark.asyncio
async def test_the_deadline_reaches_the_executor(tmp_path, monkeypatch):
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    task = TaskItem(task_id="deadline-3", description="Do a thing", dependencies=[])
    when = datetime.now(UTC) + timedelta(minutes=3)

    seen: dict[str, object] = {}

    async def _fake_inner(self, t, p=None, d=None):
        seen["deadline"] = d
        return {"success": True}

    monkeypatch.setattr(DAGExecutionEngine, "_execute_task_as_dag_inner", _fake_inner)
    await engine.execute_task_as_dag(task, None, deadline=when)

    assert seen["deadline"] == when


def test_the_goal_engine_hands_the_executor_its_deadline():
    """
    The tracker is goal-level; the executor is phase-level. If the goal engine
    stopped passing it, the executor would silently return to overrunning and
    only a live benchmark would notice.
    """
    from pathlib import Path

    src = Path("sdk/agent/goal_engine.py").read_text(encoding="utf-8")
    assert "deadline=tracker.deadline()" in src
