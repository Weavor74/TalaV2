"""
Emotional continuity must carry a mood briefly, never freeze it.

The inertia layer exists so consecutive turns feel like one being: a follow-up
seconds later keeps the label, while a reading hours later is free to move.
It used to re-save the carried label with a fresh ``computed_at``, so each turn
renewed the window that was supposed to expire. At any cadence under about
4.2 hours the label never changed again: a simulated 20 days of hourly turns
reported the same emotion 386 times in 480 while the engine itself produced 15
different ones.

Also covered: the live mood path says so, loudly, when the real ephemeris is
unavailable (the fallback is wrong by up to ~170 degrees for Mercury and Venus).
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

import pytest

import python.harness.emotion_service as svc
from python.astrology import ephemeris as eph

#: The point at which the carry stops: tau * ln(1 / threshold).
CARRY_WINDOW_H = svc.INERTIA_TAU_SECONDS * math.log(1 / svc.INERTIA_CARRY_THRESHOLD) / 3600.0

START = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from python.harness import db

    async def no_rows(*args, **kwargs):
        return None

    monkeypatch.setattr(db, "fetchrow", no_rows)
    monkeypatch.setattr(svc, "_state_path", lambda: tmp_path / "emotion_state.json")
    monkeypatch.setattr(svc, "_last_degraded_alarm", None)


async def _read(when):
    return (await svc.get_emotional_state(when=when))["emotional_state"]


@pytest.mark.asyncio
async def test_a_follow_up_seconds_later_keeps_the_mood():
    first = await _read(START)
    soon = await _read(START + timedelta(seconds=30))
    assert soon["carried_over"] is True
    assert soon["primary_emotion"] == first["primary_emotion"]


@pytest.mark.asyncio
async def test_a_carry_does_not_extend_its_own_window():
    first = await _read(START)
    carried = await _read(START + timedelta(hours=1))
    assert carried["carried_over"] is True
    assert carried["computed_at"] == first["computed_at"], "a carry renewed its own window"

    after = await _read(START + timedelta(hours=CARRY_WINDOW_H + 0.5))
    assert not after.get("carried_over")
    assert after["computed_at"] == (START + timedelta(hours=CARRY_WINDOW_H + 0.5)).isoformat()
    assert after["primary_emotion"] == after["engine_emotion"]


@pytest.mark.asyncio
async def test_hourly_turns_for_ten_days_still_track_the_engine():
    """The audit's scenario: one turn an hour. The label must never be older
    than the carry window, and must move as the engine moves."""
    labels, engine_labels = [], []
    for hour in range(10 * 24):
        now = START + timedelta(hours=hour)
        state = await _read(now)
        reading = datetime.fromisoformat(state["computed_at"])
        assert (now - reading).total_seconds() / 3600.0 <= CARRY_WINDOW_H + 1e-6, (
            f"hour {hour}: the label is {(now - reading).total_seconds() / 3600:.1f} h old"
        )
        labels.append(state["primary_emotion"])
        engine_labels.append(state["engine_emotion"])

    assert len(set(engine_labels)) >= 6, "sanity: the engine itself should move over ten days"
    assert len(set(labels)) >= 5, "the reported mood froze while the engine moved"
    # at most the carried turns of each 5-turn cycle may disagree with the engine
    assert sum(1 for a, b in zip(labels, engine_labels) if a != b) <= len(labels) * 0.8


# --- the degraded ephemeris is loud on the live path ---------------------------------------


@pytest.mark.asyncio
async def test_a_healthy_ephemeris_is_not_flagged(caplog):
    if not eph.SWISSEPH_AVAILABLE:
        pytest.skip("needs the real Swiss Ephemeris")
    with caplog.at_level(logging.ERROR, logger=svc.log.name):
        state = await _read(START)
    assert state["degraded"] is False and state["ephemeris_source"] == "swisseph"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_the_fallback_ephemeris_is_flagged_and_logged_as_an_error(monkeypatch, caplog):
    monkeypatch.setattr(eph, "SWISSEPH_AVAILABLE", False)
    with caplog.at_level(logging.ERROR, logger=svc.log.name):
        state = await _read(START)
    assert state["degraded"] is True and state["ephemeris_source"] == "approximation"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "pyswisseph" in errors[0].getMessage()
    assert "NOT derived from her chart" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_the_alarm_repeats_but_does_not_flood(monkeypatch, caplog):
    monkeypatch.setattr(eph, "SWISSEPH_AVAILABLE", False)
    with caplog.at_level(logging.ERROR, logger=svc.log.name):
        for minute in range(5):
            await _read(START + timedelta(minutes=minute))
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1

    # once the interval has passed it speaks again
    monkeypatch.setattr(svc, "_last_degraded_alarm", svc.time.monotonic() - svc._DEGRADED_ALARM_INTERVAL_S - 1)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger=svc.log.name):
        await _read(START + timedelta(minutes=10))
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
