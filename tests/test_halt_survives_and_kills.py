"""
The halt has to outlive the process and reach work already running (SEC-W1-05).

b7b066c put a halt check in ToolRegistry.run_tool, which stopped the *next*
tool call. Two holes were left:

  1. ``_EMERGENCY_HALT_ACTIVE`` was a module global, so the documented way out
     of an emergency halt was to restart the thing that was running away. A
     kill switch a restart clears is a pause button.

  2. The check sat in front of dispatch with nothing behind it, so the runaway
     shell command that prompted the abort ran happily to completion. And
     ``shell.run`` -- the one RPC that executes arbitrary commands -- called
     AsyncShellExecutor directly, bypassing run_tool, so it was not even
     covered by the pre-dispatch check.
"""

import asyncio
import json
import sys

import pytest

from sdk.agent import async_shell, gates
from sdk.agent.async_shell import AsyncShellExecutor
from sdk.agent.gates import RevocationHalt


@pytest.fixture(autouse=True)
def _isolated_halt(tmp_path, monkeypatch):
    """Never touch the real halt file: creating it would halt the developer's
    own harness, and failing to clean it up would keep it halted."""
    monkeypatch.setattr(gates, "_halt_path", lambda: tmp_path / "data" / "emergency_halt.json")
    RevocationHalt.release_emergency_halt()
    yield
    RevocationHalt.release_emergency_halt()


# -- durability --------------------------------------------------------------

def test_halt_is_written_to_disk(tmp_path):
    RevocationHalt.trigger_emergency_halt("test reason")
    path = gates._halt_path()
    assert path.exists(), "halt did not survive as anything a restart could read"
    assert json.loads(path.read_text(encoding="utf-8"))["reason"] == "test reason"


def test_halt_survives_losing_the_in_process_flag():
    """Simulates a restart: the module global is gone, the file is not."""
    RevocationHalt.trigger_emergency_halt("before the restart")
    gates._EMERGENCY_HALT_ACTIVE = False          # what a fresh process starts with
    assert RevocationHalt.is_halted(), "a restart cleared the emergency halt"


def test_release_removes_the_file():
    RevocationHalt.trigger_emergency_halt()
    RevocationHalt.release_emergency_halt()
    gates._EMERGENCY_HALT_ACTIVE = False
    assert not gates._halt_path().exists()
    assert not RevocationHalt.is_halted()


def test_an_unreadable_halt_file_counts_as_halted():
    """Fail-closed: the file exists, so somebody meant something by it."""
    path = gates._halt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    gates._EMERGENCY_HALT_ACTIVE = False
    assert RevocationHalt.is_halted()


def test_the_halt_can_be_engaged_from_outside_this_process():
    """Matters precisely when the RPC surface is the thing that is wedged."""
    path = gates._halt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    gates._EMERGENCY_HALT_ACTIVE = False
    assert not RevocationHalt.is_halted()
    path.write_text(json.dumps({"halted": True}), encoding="utf-8")
    assert RevocationHalt.is_halted()


# -- reach -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_halted_system_refuses_to_start_a_command(tmp_path):
    RevocationHalt.trigger_emergency_halt()
    result = await AsyncShellExecutor.run("echo should-not-run", str(tmp_path), timeout=10)
    assert result["halted"] is True
    assert result["success"] is False
    assert "should-not-run" not in result["stdout"]


@pytest.mark.asyncio
async def test_halt_kills_a_command_already_running(tmp_path):
    """
    The half that a flag check cannot give you.

    Start something long, halt, and assert it is actually dead -- not that a
    later call would have been refused.
    """
    sleeper = f'"{sys.executable}" -c "import time; time.sleep(60)"'
    task = asyncio.create_task(AsyncShellExecutor.run(sleeper, str(tmp_path), timeout=60))

    for _ in range(100):                      # let the process actually spawn
        await asyncio.sleep(0.05)
        if async_shell._LIVE_PROCESSES:
            break
    assert async_shell._LIVE_PROCESSES, "the subprocess never registered"
    proc = next(iter(async_shell._LIVE_PROCESSES))
    assert proc.returncode is None, "it exited before we could halt it"

    await gates.trigger_emergency_halt("test")
    result = await asyncio.wait_for(task, timeout=30)

    assert proc.returncode is not None, "the running command survived the halt"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_finished_processes_leave_the_registry(tmp_path):
    """A registry that only grows would have the halt signalling the dead."""
    before = len(async_shell._LIVE_PROCESSES)
    await AsyncShellExecutor.run("echo done", str(tmp_path), timeout=30)
    assert len(async_shell._LIVE_PROCESSES) == before


@pytest.mark.asyncio
async def test_kill_running_processes_reports_zero_when_idle():
    assert await async_shell.kill_running_processes("nothing to do") == 0
