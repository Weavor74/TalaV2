"""
A child of the harness must not inherit the pipe the harness is blocked reading.

The harness reads its stdin in a thread to learn when Electron has gone (server._monitor_stdin). On Windows a child that inherits that
same pipe hangs at start-up: the C runtime queries the handle, and the query waits behind the parent's pending read. Found live on
2026-10-29 when the LoRA trainer, started from "Train now", sat for 13 minutes at 0% CPU and 10 MB with no Python frame (py-spy: idle in
lseek), the same signature as the desktop_run accessibility helper that "took 25 s + 15 s with nothing running" a day earlier. Run by
hand, the same commands finish in seconds, which is why nothing reproduced it.

The fix takes a private copy of the pipe for the monitor and points fd 0 at NUL, so every child inherits NUL.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from python.harness import server, stdin_guard

REPO = Path(__file__).resolve().parents[1]

# A stand-in harness: stdin is a pipe held open by the test, a thread is blocked reading it, then a child is started.
_HARNESS = textwrap.dedent("""
    import subprocess, sys, threading, time
    sys.path.insert(0, {repo!r})
    if {detach!r}:
        from python.harness import stdin_guard
        assert stdin_guard.detach() is True
        reader = stdin_guard.wait_for_eof
    else:
        reader = lambda: sys.stdin.read(1)
    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.5)
    try:
        out = subprocess.run([sys.executable, "-c", "print('child ok')"], capture_output=True, text=True, timeout={timeout})
        print(out.stdout.strip())
    except subprocess.TimeoutExpired:
        print("HUNG")
    sys.stdout.flush()
    import os; os._exit(0)
""")


def _run_stand_in(detach: bool, timeout: int) -> str:
    code = _HARNESS.format(repo=str(REPO), detach=detach, timeout=timeout)
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        return proc.stdout.read().strip()  # stdin stays open, as Electron's does
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(sys.platform != "win32", reason="the inherited-pipe hang is a Windows behaviour")
def test_without_the_guard_a_child_of_a_harness_blocked_on_stdin_hangs():
    """The precondition, so the next test proves something: this is the bug."""
    assert _run_stand_in(detach=False, timeout=6) == "HUNG"


def test_with_the_guard_a_child_starts_normally():
    assert _run_stand_in(detach=True, timeout=30) == "child ok"


def test_the_monitor_still_hears_the_parent_go(monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(stdin_guard, "_private", read_fd)
    done = threading.Event()
    threading.Thread(target=lambda: (stdin_guard.wait_for_eof(), done.set()), daemon=True).start()
    assert not done.wait(0.3)  # the parent is still there
    os.close(write_fd)  # the parent exits
    assert done.wait(5), "an orphaned harness would never notice"
    os.close(read_fd)


def test_an_stdin_that_is_not_a_pipe_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(stdin_guard, "_private", None)
    path = tmp_path / "in.txt"
    path.write_text("", encoding="utf-8")
    saved = os.dup(0)
    try:
        os.dup2(os.open(path, os.O_RDONLY), 0)
        assert stdin_guard.detach() is False
        assert stdin_guard.holds_parent_pipe() is False
    finally:
        os.dup2(saved, 0)
        os.close(saved)


def test_without_a_private_pipe_the_monitor_falls_back_to_reading_stdin(monkeypatch):
    monkeypatch.setattr(stdin_guard, "_private", None)

    class Fake:
        def read(self, n):
            return ""

    monkeypatch.setattr(sys, "stdin", Fake())
    assert stdin_guard.wait_for_eof() == ""


def test_a_private_pipe_makes_the_server_treat_stdin_as_a_parent(monkeypatch):
    monkeypatch.setattr(stdin_guard, "_private", 123)
    assert server._stdin_is_a_parent_pipe() is True


def test_the_harness_detaches_stdin_before_anything_starts_a_child():
    source = inspect.getsource(server.main)
    assert "stdin_guard.detach()" in source
    assert source.index("stdin_guard.detach()") < source.index("loop.run_until_complete(_run_server())")
    assert "stdin_guard" in inspect.getsource(server._monitor_stdin)


def test_every_child_the_harness_starts_inherits_nul_not_the_pipe():
    """The one place that matters most: the LoRA run's trainer (and every stage after it) is started by _exec."""
    from sdk.agent import lora_pipeline

    assert "stdin=asyncio.subprocess.DEVNULL" in inspect.getsource(lora_pipeline._exec)
