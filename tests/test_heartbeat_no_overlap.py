"""
A heartbeat does not run on top of itself.

A cron expression says how often to *start* work; it says nothing about how
long the work takes. The nightly corpus job fires every 30 minutes and its own
goal sets an 1800-second timeout, because each story takes over two minutes --
so a slow run was still going when the next one fired, and two GoalEngine runs
could generate into the same corpus at once. Nothing prevented it: _spawn_job
created a task unconditionally.

Every job carries the same exposure, which is why the guard lives in the
scheduler rather than in that job's schedule.
"""
from __future__ import annotations

import asyncio

import pytest

from python.harness.heartbeat_scheduler import HeartbeatJob, HeartbeatScheduler


@pytest.fixture
def scheduler(monkeypatch):
    s = HeartbeatScheduler()
    started: list[str] = []
    release = asyncio.Event()

    async def fake_run(job):
        started.append(job.name)
        await release.wait()

    monkeypatch.setattr(s, "_run_job", fake_run)
    s.started, s.release = started, release
    return s


def _job(name: str = "corpus") -> HeartbeatJob:
    return HeartbeatJob(
        id="11111111-1111-1111-1111-111111111111",
        name=name,
        cron_expr="*/30 22-23,0-6 * * *",
        goal_text="generate stories",
    )


@pytest.mark.asyncio
async def test_a_second_fire_is_skipped_while_the_first_is_running(scheduler):
    job = _job()

    assert scheduler._spawn_job(job) is True
    await asyncio.sleep(0)

    assert scheduler._spawn_job(job) is False, "the job was started twice"
    await asyncio.sleep(0)
    assert scheduler.started == ["corpus"]

    scheduler.release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_it_runs_again_once_the_previous_run_finishes(scheduler):
    """A skipped occurrence must not wedge the job shut forever."""
    job = _job()

    scheduler._spawn_job(job)
    await asyncio.sleep(0)
    scheduler.release.set()
    await asyncio.sleep(0.01)

    scheduler.release.clear()
    assert scheduler._spawn_job(job) is True
    await asyncio.sleep(0)
    assert scheduler.started == ["corpus", "corpus"]

    scheduler.release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_different_jobs_do_not_block_each_other(scheduler):
    """The guard is per job, not a global lock -- a slow job must not stop the
    rest of the schedule."""
    a = _job("a")
    b = _job("b")
    b.id = "22222222-2222-2222-2222-222222222222"

    assert scheduler._spawn_job(a) is True
    assert scheduler._spawn_job(b) is True
    await asyncio.sleep(0)

    assert sorted(scheduler.started) == ["a", "b"]
    scheduler.release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_fire_now_says_so_when_it_did_not_fire(scheduler, monkeypatch):
    """It reported success unconditionally. A caller told 'fired' for a fire
    that did not happen waits on output nothing is producing."""
    job = _job()
    scheduler._jobs = [job]

    first = await scheduler.fire_now(job.id)
    await asyncio.sleep(0)
    second = await scheduler.fire_now(job.id)

    assert first["success"] is True
    assert second["success"] is False
    assert "already running" in second["error"]

    scheduler.release.set()
    await asyncio.sleep(0)
