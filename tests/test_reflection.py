"""
The reflection loop's guardrails, which are the whole reason it is allowed to
run unattended.

Every test here is a rule that, if it broke, would let a scheduled loop do
damage at 3am while nobody is reading: acting on a finding nothing knows how
to fix, acting after a dry run said not to, retrying a repair that never
works, or running through an emergency halt.
"""
from __future__ import annotations

import pytest

from sdk.agent import reflection


@pytest.fixture(autouse=True)
def clean_state():
    reflection.reset_state()
    yield
    reflection.reset_state()


def _finding(key: str) -> dict:
    return {"key": key, "kind": "test", "summary": key, "evidence": "because"}


@pytest.fixture
def observed(monkeypatch):
    """Replace observation so the tests drive findings directly."""
    def _set(*findings):
        async def fake(_minutes):
            return [_finding(k) for k in findings]
        monkeypatch.setattr(reflection, "_observe", fake)
    return _set


@pytest.fixture
def repair(monkeypatch):
    """Register a repair and record how it was called."""
    calls: list[dict] = []

    def _set(key="test:fix", *, dry_ok=True, would_act=True, applies=True):
        async def fn(finding, *, dry_run):
            calls.append({"dry_run": dry_run, "finding": finding["key"]})
            if dry_run:
                return {"success": dry_ok, "would_act": would_act}
            return {"success": applies}
        monkeypatch.setitem(reflection.REPAIRS, key, ("test-repair", fn))
        return calls
    return _set


@pytest.mark.asyncio
async def test_a_finding_with_no_registered_repair_is_recorded_not_acted_on(observed, repair):
    """The safety property: there is no path from 'something is wrong' to
    'improvise a fix'."""
    repair()
    observed("health:SOMETHING_UNKNOWN")

    out = await reflection._reflect_once()

    assert len(out["findings"]) == 1
    assert out["actions"] == []


@pytest.mark.asyncio
async def test_a_registered_repair_rehearses_then_applies(observed, repair):
    calls = repair()
    observed("test:fix")

    out = await reflection._reflect_once()

    assert [c["dry_run"] for c in calls] == [True, False], "dry run must come first"
    assert out["actions"][0]["outcome"] == "applied"


@pytest.mark.asyncio
async def test_a_failed_dry_run_cancels_the_real_one(observed, repair):
    calls = repair(dry_ok=False)
    observed("test:fix")

    out = await reflection._reflect_once()

    assert [c["dry_run"] for c in calls] == [True], "the repair ran despite a failed rehearsal"
    assert out["actions"][0]["outcome"].startswith("cancelled")


@pytest.mark.asyncio
async def test_a_dry_run_with_nothing_to_do_does_not_act(observed, repair):
    calls = repair(would_act=False)
    observed("test:fix")

    out = await reflection._reflect_once()

    assert [c["dry_run"] for c in calls] == [True]
    assert out["actions"][0]["outcome"].startswith("skipped")


@pytest.mark.asyncio
async def test_one_action_per_cycle(observed, repair, monkeypatch):
    repair("test:fix")
    repair("test:fix2")
    observed("test:fix", "test:fix2")

    out = await reflection._reflect_once()

    assert len(out["findings"]) == 2
    assert len(out["actions"]) == 1, "a bad repair must not cascade through a cycle"


@pytest.mark.asyncio
async def test_a_repair_that_never_clears_its_finding_is_abandoned(observed, repair):
    """Otherwise the loop runs the same failing repair every hour, forever."""
    calls = repair()
    observed("test:fix")

    for _ in range(reflection.MAX_REPAIR_ATTEMPTS):
        await reflection._reflect_once()
    attempts_before = len([c for c in calls if not c["dry_run"]])

    out = await reflection._reflect_once()

    assert len([c for c in calls if not c["dry_run"]]) == attempts_before, (
        "the repair ran again after being abandoned"
    )
    assert "test:fix" in out["abandoned"]


@pytest.mark.asyncio
async def test_a_cleared_finding_resets_its_attempts(observed, repair):
    """A problem that comes back next week deserves a fresh attempt, not the
    abandonment recorded for the last occurrence."""
    calls = repair()
    observed("test:fix")
    await reflection._reflect_once()

    observed()  # finding cleared
    await reflection._reflect_once()

    observed("test:fix")  # and returns
    await reflection._reflect_once()

    assert len([c for c in calls if not c["dry_run"]]) == 2


@pytest.mark.asyncio
async def test_the_emergency_halt_stops_the_cycle(observed, repair, monkeypatch):
    calls = repair()
    observed("test:fix")
    monkeypatch.setattr(reflection, "_halted", lambda: True)

    out = await reflection._reflect_once()

    assert calls == [], "a halted system still ran a repair"
    assert "halt" in out["last_status"]


@pytest.mark.asyncio
async def test_the_interval_gate_returns_none_when_not_due(observed, repair, monkeypatch):
    """None means 'did not run', which a caller must be able to tell from
    'ran and found nothing'."""
    repair()
    observed()
    monkeypatch.setattr(reflection, "_last_cycle_monotonic", None)

    first = await reflection.reflect_if_due(interval_minutes=60)
    second = await reflection.reflect_if_due(interval_minutes=60)

    assert first is not None
    assert second is None
