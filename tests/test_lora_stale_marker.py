"""
A LoRA run that died must not leave the pipeline "busy" for a day.

Found 2026-10-29: Train now hung (a child inheriting the harness's stdin pipe, see test_stdin_guard.py) and the app was closed. The run's
marker, ``running_since`` in learning/lora.json, stayed behind, and the pipeline treats any marker younger than 24 hours as a live run:
the next Train now would have been turned away as busy, and Roll back and the settings (both refused while a run is active) with it.

The marker now carries the pid of the harness that wrote it, and is live only while that process is.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from sdk.agent import lora_pipeline as lp


@pytest.fixture(autouse=True)
def _world(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(lp, "_RUNNING", False)
    return tmp_path


def _mark(**extra):
    lp._write_state({"running_since": (datetime.now() - timedelta(minutes=10)).isoformat(), "stage": "train", **extra})


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_a_marker_from_a_process_that_is_gone_is_not_a_run_in_progress():
    _mark(running_pid=_dead_pid())
    assert lp.is_running() is False


def test_a_marker_from_a_live_process_still_is():
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _mark(running_pid=sleeper.pid)
        assert lp.is_running() is True
    finally:
        sleeper.kill()
        sleeper.wait()


def test_a_marker_this_process_left_but_is_not_running_is_stale():
    """The harness restarted and was handed the same pid: nothing in this process is running a pipeline."""
    _mark(running_pid=os.getpid())
    assert lp.is_running() is False


def test_a_run_in_this_process_is_running(monkeypatch):
    _mark(running_pid=os.getpid())
    monkeypatch.setattr(lp, "_RUNNING", True)
    assert lp.is_running() is True


def test_a_marker_from_before_pids_were_recorded_behaves_as_it_always_did():
    _mark()
    assert lp.is_running() is True


def test_a_day_old_marker_is_stale_whoever_wrote_it():
    lp._write_state({"running_since": (datetime.now() - timedelta(hours=25)).isoformat(), "running_pid": os.getppid()})
    assert lp.is_running() is False


@pytest.mark.asyncio
async def test_a_starting_run_records_whose_marker_it_is(monkeypatch):
    seen = {}

    async def stop_here(run_id, scratch, **kw):
        seen.update(lp._read_state())
        return {"status": "skipped", "stage": "dataset", "reason": "test"}

    monkeypatch.setattr(lp, "_run", stop_here)
    await lp._run_pipeline(reason="test", force=True)
    assert seen["running_pid"] == os.getpid()
