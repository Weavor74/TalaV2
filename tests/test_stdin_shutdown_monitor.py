"""
The harness must not shut itself down because nobody is holding its stdin.

Found on the live VPS, where the service had been reporting `active` while
serving nothing:

    23:49:38  HeartbeatScheduler started -- tick loop running
    23:50:16  Parent process monitor started for PID 1730324
    23:50:16  WARNING  Stdin EOF detected. Triggering shutdown.
    23:50:18  === Tala harness stopped ===

The harness came up correctly and killed itself about forty seconds later,
on every single start. The blockchain is a separate child, so 8332 kept
answering and 8765 never opened -- which is why federation could never have
worked there whatever the peer code did.

`_monitor_stdin` exists for Electron, which spawns the harness with
stdio: ['pipe', 'pipe', 'pipe']. That pipe stays open as long as the app
lives, so EOF really does mean the parent has gone. systemd gives the
service /dev/null instead, which is at EOF from the first read. So does
cron, nohup, a container with no TTY, and a terminal that has since closed.

The distinction is what stdin *is*, not what it returns.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys

import pytest

from python.harness import server


class _FakeStdin:
    def __init__(self, fd: int):
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def read(self, _n: int) -> str:
        return ""


def _as_stdin(monkeypatch, fd: int) -> None:
    monkeypatch.setattr(server.sys, "stdin", _FakeStdin(fd), raising=False)


def test_a_pipe_is_a_parent_holding_the_line(monkeypatch):
    """Electron's case: the monitor is armed."""
    read_fd, write_fd = os.pipe()
    try:
        _as_stdin(monkeypatch, read_fd)
        assert stat.S_ISFIFO(os.fstat(read_fd).st_mode), "precondition: this is a FIFO"
        assert server._stdin_is_a_parent_pipe() is True
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_dev_null_is_not_a_parent(monkeypatch):
    """
    systemd's case, and the one that took the VPS down. /dev/null is at EOF
    from the first read, which is not a parent hanging up.
    """
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        _as_stdin(monkeypatch, fd)
        assert server._stdin_is_a_parent_pipe() is False
    finally:
        os.close(fd)


def test_a_closed_descriptor_is_not_a_parent(monkeypatch):
    """A detached start with no stdin at all must not read as a bereavement."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    _as_stdin(monkeypatch, read_fd)  # now closed; fstat will raise
    assert server._stdin_is_a_parent_pipe() is False


def test_a_regular_file_is_not_a_parent(monkeypatch, tmp_path):
    """`tala start < config.txt` should not arm a shutdown."""
    path = tmp_path / "input.txt"
    path.write_text("", encoding="utf-8")
    fd = os.open(path, os.O_RDONLY)
    try:
        _as_stdin(monkeypatch, fd)
        assert server._stdin_is_a_parent_pipe() is False
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_the_monitor_does_not_fire_without_a_parent_pipe(monkeypatch):
    """
    The behaviour that matters: a service-managed harness stays up.
    Against the pre-fix code this sets the shutdown event immediately.
    """
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        _as_stdin(monkeypatch, fd)
        server._shutdown_event.clear()

        await asyncio.wait_for(server._monitor_stdin(), timeout=5)

        assert not server._shutdown_event.is_set(), (
            "the harness shut itself down because nobody was holding its stdin"
        )
    finally:
        os.close(fd)
        server._shutdown_event.clear()


@pytest.mark.asyncio
async def test_the_monitor_still_fires_when_the_pipe_closes(monkeypatch):
    """
    The Electron case must keep working: closing the write end is the parent
    exiting, and the harness should follow it down rather than linger.
    """
    read_fd, write_fd = os.pipe()
    try:
        _as_stdin(monkeypatch, read_fd)
        server._shutdown_event.clear()

        os.close(write_fd)  # the parent exits
        await asyncio.wait_for(server._monitor_stdin(), timeout=5)

        assert server._shutdown_event.is_set(), "an orphaned harness kept running"
    finally:
        os.close(read_fd)
        server._shutdown_event.clear()
