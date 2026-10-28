"""
The harness froze, permanently, three minutes after a voice answer (2026-10-28) -- not the voice stack, the whole process.

py-spy showed the event-loop thread inside ``subprocess.run`` called by ``architecture_facts._git`` from the daily
architecture-contract pass. ``git status --porcelain`` outlasted its 15 s timeout, ``subprocess.run`` killed it, and then --
on Windows -- called ``communicate()`` again *without* a timeout to collect the output. Git for Windows leaves its filesystem-monitor
daemon running, and the daemon inherited git's stdout pipe, so the pipe never closed and the call never returned. Two faults
made that fatal: a pipe that a grandchild can hold open, and a blocking call on the event loop.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap
import time

import pytest

from python.harness import architecture_facts as facts

# A command that behaves like git with a daemon: prints, exits, and leaves a child holding the inherited stdout.
_LEAVES_A_DAEMON = textwrap.dedent("""
    import subprocess, sys
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(25)"])
    print("the answer")
""")
_NEVER_FINISHES = "import time; time.sleep(25)"


def test_output_is_returned_even_when_a_child_keeps_the_stdout_pipe_open():
    started = time.monotonic()
    out = facts._run_capture([sys.executable, "-c", _LEAVES_A_DAEMON], cwd=None, timeout=10)
    assert out == "the answer"
    assert time.monotonic() - started < 8, "it waited for the daemon to exit"


def test_a_command_that_outlasts_its_timeout_gives_up_instead_of_hanging():
    started = time.monotonic()
    assert facts._run_capture([sys.executable, "-c", _NEVER_FINISHES], cwd=None, timeout=1) == ""
    assert time.monotonic() - started < 8


def test_a_missing_command_is_an_empty_answer_not_an_error():
    assert facts._run_capture(["definitely-not-a-real-command-xyz"], cwd=None, timeout=2) == ""


@pytest.mark.asyncio
async def test_building_the_contract_does_not_block_the_event_loop(monkeypatch):
    """Whatever the contract generator does synchronously (git, file counting), the harness must keep answering meanwhile."""
    import sys as _sys
    from pathlib import Path

    from python.harness import doc_maintenance

    root = Path(__file__).resolve().parents[1]
    _sys.path.insert(0, str(root / "scripts"))
    import generate_architecture_contract as gen

    async def slow_and_blocking():
        time.sleep(1.0)  # a blocking call, as git is
        raise RuntimeError("stop here: only the blocking matters")

    monkeypatch.setattr(gen, "build_contract", slow_and_blocking)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    task = asyncio.ensure_future(ticker())
    try:
        await doc_maintenance.regenerate_contract_if_changed()
    finally:
        task.cancel()
    assert ticks >= 10, f"the event loop was blocked while the contract was built (only {ticks} ticks in about a second)"
