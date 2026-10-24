"""
The settings snapshot must not cost every turn thirteen seconds.

Before a reply, the harness reads four settings nodes (system, inference, runtime, agent) to give the model a live snapshot of
its own configuration. It used to regenerate *all thirteen* settings nodes first, awaiting each (database queries, ledger
anchoring, a file write: about a second apiece), on every single turn. Measured on Kagura that was 13 of the 22 seconds a short
chat turn took, before the model was even called. The nodes are already written at startup, and the settings RPCs update their own
node when something changes, so the refresh now runs in the background, at most once per interval, and the turn reads whatever
is current. A refresh that fails costs nothing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from python.harness import settings_nn

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    settings_nn._REFRESH.update(last=None, task=None)
    yield
    settings_nn._REFRESH.update(last=None, task=None)


def run(coro):
    return asyncio.run(coro)


class Counter:
    def __init__(self, delay=0.0, fail=False):
        self.calls = 0
        self.delay = delay
        self.fail = fail

    async def __call__(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("database unavailable")


def test_asking_for_a_refresh_returns_at_once_and_does_not_wait_for_it(monkeypatch):
    sync = Counter(delay=0.2)
    monkeypatch.setattr(settings_nn, "sync_settings_to_nodes", sync)

    async def go():
        import time

        started = time.perf_counter()
        settings_nn.refresh_in_background(min_interval_s=30, now=lambda: 100.0)
        returned_after = time.perf_counter() - started
        await asyncio.sleep(0.01)
        in_flight = sync.calls
        await asyncio.sleep(0.3)
        return returned_after, in_flight

    returned_after, in_flight = run(go())
    assert returned_after < 0.05, "the caller must not wait for thirteen database round trips"
    assert in_flight == 1


def test_a_second_request_inside_the_interval_does_nothing(monkeypatch):
    sync = Counter()
    monkeypatch.setattr(settings_nn, "sync_settings_to_nodes", sync)
    clock = {"t": 100.0}

    async def go():
        settings_nn.refresh_in_background(min_interval_s=30, now=lambda: clock["t"])
        await asyncio.sleep(0.02)
        clock["t"] = 110.0
        settings_nn.refresh_in_background(min_interval_s=30, now=lambda: clock["t"])
        await asyncio.sleep(0.02)

    run(go())
    assert sync.calls == 1


def test_after_the_interval_it_refreshes_again(monkeypatch):
    sync = Counter()
    monkeypatch.setattr(settings_nn, "sync_settings_to_nodes", sync)
    clock = {"t": 100.0}

    async def go():
        settings_nn.refresh_in_background(min_interval_s=30, now=lambda: clock["t"])
        await asyncio.sleep(0.02)
        clock["t"] = 131.0
        settings_nn.refresh_in_background(min_interval_s=30, now=lambda: clock["t"])
        await asyncio.sleep(0.02)

    run(go())
    assert sync.calls == 2


def test_a_refresh_still_running_is_never_started_twice(monkeypatch):
    sync = Counter(delay=0.2)
    monkeypatch.setattr(settings_nn, "sync_settings_to_nodes", sync)
    clock = {"t": 100.0}

    async def go():
        settings_nn.refresh_in_background(min_interval_s=1, now=lambda: clock["t"])
        await asyncio.sleep(0.02)
        clock["t"] = 500.0  # long past the interval, but the first one has not finished
        settings_nn.refresh_in_background(min_interval_s=1, now=lambda: clock["t"])
        await asyncio.sleep(0.3)

    run(go())
    assert sync.calls == 1


def test_a_failing_refresh_costs_nothing_and_is_retried_later(monkeypatch):
    sync = Counter(fail=True)
    monkeypatch.setattr(settings_nn, "sync_settings_to_nodes", sync)
    clock = {"t": 100.0}

    async def go():
        settings_nn.refresh_in_background(min_interval_s=30, now=lambda: clock["t"])
        await asyncio.sleep(0.02)
        clock["t"] = 200.0
        settings_nn.refresh_in_background(min_interval_s=30, now=lambda: clock["t"])
        await asyncio.sleep(0.02)

    run(go())  # nothing raised out of the task
    assert sync.calls == 2


def test_with_no_event_loop_it_quietly_does_nothing(monkeypatch):
    sync = Counter()
    monkeypatch.setattr(settings_nn, "sync_settings_to_nodes", sync)
    settings_nn.refresh_in_background(min_interval_s=30, now=lambda: 1.0)  # called outside any loop
    assert sync.calls == 0


def test_the_full_synchronisation_is_still_there_for_startup_and_for_the_tool_that_asks(monkeypatch):
    assert callable(settings_nn.sync_settings_to_nodes) and settings_nn.update_all_settings_nodes is settings_nn.sync_settings_to_nodes


# --- the turn no longer waits for it -----------------------------------------------------------------------------------------------------


def server_source():
    return (REPO / "python" / "harness" / "server.py").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_turn_path_refreshes_in_the_background_instead_of_awaiting_a_full_sync():
    src = server_source()
    block = src[src.index("# Dynamically load all 12 Harness Settings Panel Neural Nodes"):][:900]
    assert "refresh_in_background()" in block
    assert "await update_all_settings_nodes()" not in block and "await sync_settings_to_nodes()" not in block


def test_startup_still_does_the_full_synchronisation_once():
    assert server_source().count("await sync_settings_to_nodes()") == 1


def test_the_default_interval_is_long_enough_that_a_refresh_rarely_overlaps_a_turn():
    """At 30 s a refresh overlapped every second or third turn and still slowed those by about 10 s (it shares the event loop and
    the CPU). The nodes are written at startup and by the settings RPCs, so this is only a safety net, and minutes will do."""
    assert 120 <= settings_nn.REFRESH_INTERVAL_S <= 900
