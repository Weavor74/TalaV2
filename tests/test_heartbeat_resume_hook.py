"""
tests/test_heartbeat_resume_hook.py
=====================================
Confirms HeartbeatScheduler's tick loop actually calls
sdk.agent.goal_engine.resume_suspended_goals() every tick, alongside the
existing ZombieReaper sweep -- this is the mechanism that turns
resume_suspended_goals from a callable-but-never-called function into a
real, running 24/7 behavior.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from python.harness.heartbeat_scheduler import HeartbeatScheduler


@pytest.mark.asyncio
async def test_evaluate_tick_calls_resume_suspended_goals():
    scheduler = HeartbeatScheduler()

    with patch("sdk.agent.swarm_engine.ZombieReaper.reclaim_zombies", new_callable=AsyncMock) as mock_reaper, \
         patch("sdk.agent.goal_engine.resume_suspended_goals", new_callable=AsyncMock) as mock_resume:
        mock_reaper.return_value = 0
        mock_resume.return_value = 0

        await scheduler._evaluate_tick()

        mock_reaper.assert_awaited_once()
        mock_resume.assert_awaited_once_with(max_resume_attempts=5)


@pytest.mark.asyncio
async def test_evaluate_tick_survives_resume_sweep_failure():
    """A crash in the resume sweep must not prevent the rest of the tick
    (zombie reclaim already ran; due heartbeat jobs must still fire)."""
    scheduler = HeartbeatScheduler()

    with patch("sdk.agent.swarm_engine.ZombieReaper.reclaim_zombies", new_callable=AsyncMock) as mock_reaper, \
         patch("sdk.agent.goal_engine.resume_suspended_goals", new_callable=AsyncMock) as mock_resume:
        mock_reaper.return_value = 0
        mock_resume.side_effect = RuntimeError("boom")

        # Must not raise.
        await scheduler._evaluate_tick()

        mock_resume.assert_awaited_once()
