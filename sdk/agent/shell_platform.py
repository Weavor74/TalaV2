"""NAME
       shell_platform - one declared shell per platform for agent commands
DESCRIPTION
       Agent-generated command strings used to be handed to the *platform
       default* shell (cmd.exe on Windows, /bin/sh via the C runtime on
       POSIX), so the same tool produced POSIX semantics on one machine and
       cmd.exe semantics on another, and the model had no way to know which
       it was writing for. This module makes the choice explicit and
       consistent everywhere a command string is executed:

         - POSIX: bash when present, else sh.
         - Windows: Git Bash when installed (POSIX semantics, matching what
           the model overwhelmingly writes), else PowerShell.

       shell_description() states the choice in prose so tool descriptions
       can tell the model which dialect to write.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def bash_executable() -> str | None:
    """Absolute path of a working bash, or None.

    On Windows, `bash` on PATH may be the System32 WSL launcher, which starts
    (or fails to start) a whole Linux distribution rather than running a
    command in this environment -- so explicit Git for Windows locations are
    preferred and the System32 stub is rejected.
    """
    candidates: list[str] = []
    if os.name == "nt":
        for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            base = os.environ.get(env_var)
            if not base:
                continue
            candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
            candidates.append(os.path.join(base, "Programs", "Git", "bin", "bash.exe"))
        on_path = shutil.which("bash")
        if on_path and "system32" not in on_path.lower():
            candidates.append(on_path)
    else:
        on_path = shutil.which("bash")
        if on_path:
            candidates.append(on_path)

    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "printf ok"], capture_output=True, timeout=5
            )
            if probe.returncode == 0 and probe.stdout.strip() == b"ok":
                return candidate
        except Exception:
            continue
    return None


@lru_cache(maxsize=1)
def preferred_shell() -> tuple[str, tuple[str, ...], str]:
    """(name, argv prefix, syntax) of the one shell agent commands run under.

    syntax is "posix" or "powershell"; append the command string to the argv
    prefix to execute it.
    """
    bash = bash_executable()
    if bash:
        return ("bash", (bash, "-c"), "posix")
    if os.name == "nt":
        return (
            "powershell",
            ("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"),
            "powershell",
        )
    return ("sh", ("sh", "-c"), "posix")


def wrap_command(command: str) -> list[str]:
    """argv that runs *command* under the declared shell (no shell=True)."""
    _, prefix, _ = preferred_shell()
    return [*prefix, command]


def shell_description() -> str:
    """One sentence for tool descriptions: which dialect to write."""
    name, _, syntax = preferred_shell()
    if syntax == "posix":
        return f"Commands run under {name} (POSIX syntax)."
    return "Commands run under PowerShell (no Git Bash found on this Windows host) -- write PowerShell syntax, not POSIX."
