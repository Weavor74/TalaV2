"""
stdin_guard.py -- keep the harness's stdin pipe out of its children.

Electron starts the harness with stdin as a pipe that stays open as long as the app lives, and ``server._monitor_stdin`` reads it in a
thread: EOF means the app has gone. On Windows a child that inherits that same pipe hangs at start-up, because the C runtime queries
the handle and the query waits behind the parent's pending read. The LoRA trainer started from "Train now" sat for 13 minutes at 0% CPU
with no Python frame in it; the desktop_run accessibility helper had done the same a day earlier. Started by hand, neither ever hung.

``detach()`` therefore takes a private copy of the pipe for the monitor and points fd 0 at NUL, so every child inherits NUL. The copy is
not inheritable, so no child can ever hold it.
"""
from __future__ import annotations

import os
import stat
import sys

_private: int | None = None


def detach() -> bool:
    """Move a parent's stdin pipe to a private descriptor and give fd 0 (and so every child) NUL. True when there was a pipe to move;
    anything else (a service's /dev/null, a terminal, a file, no stdin at all) is left exactly as it is."""
    global _private
    if _private is not None:
        return True
    try:
        if not stat.S_ISFIFO(os.fstat(0).st_mode):
            return False
        keep = os.dup(0)
        nul = os.open(os.devnull, os.O_RDONLY)
        os.dup2(nul, 0)
        os.close(nul)
    except OSError:
        return False
    _private = keep
    return True


def holds_parent_pipe() -> bool:
    return _private is not None


def wait_for_eof() -> str:
    """Block until the parent closes the pipe (or writes), reading the private copy when there is one. Never raises."""
    try:
        if _private is not None:
            return os.read(_private, 1).decode("latin-1")
        return sys.stdin.read(1)
    except Exception:
        return ""
