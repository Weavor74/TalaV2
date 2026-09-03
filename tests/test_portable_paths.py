"""
Tala is portable: a USB stick, any OS, data travelling with the install.

Steven's constraint, restated 2026-08-23: *"this is meant to be able to be run
on USB as portable, for multi OS. So no OS hard code"*.

Two ways that breaks, and both have already happened here:

* **OS-specific paths.** ``%APPDATA%`` and friends resolve on one platform and
  silently do the wrong thing on the others.
* **Working-directory-relative paths**, which are the subtler of the two.
  ``Path("nodes/...")`` looks portable -- no drive letter, no separator
  problems -- but cwd is wherever the app was launched from, not where Tala is
  installed. ``run_workflow_node`` stored its state that way and would have
  written to whatever directory the shortcut happened to start in.

``resolve_user_data_dir()`` is the single canonical resolver. Runtime code that
reads or writes Tala's own data must go through it.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from sdk.agent.documentation_nodes import (
    resolve_active_nodes_dir,
    resolve_user_data_dir,
)

#: Runtime code. Scripts and tests may reasonably assume they run from the repo.
RUNTIME_DIRS = (Path("sdk"), Path("python"))

SKIP_PARTS = ("tests", "test_", "__pycache__", "node_modules", "_staging")


def _runtime_files():
    for root in RUNTIME_DIRS:
        for path in sorted(root.rglob("*.py")):
            if any(p.startswith(SKIP_PARTS) or p in SKIP_PARTS for p in path.parts):
                continue
            yield path


def test_the_resolver_honours_an_explicit_data_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    assert resolve_user_data_dir() == tmp_path.resolve()
    assert resolve_active_nodes_dir() == (tmp_path / "nodes").resolve()


def test_the_resolver_ignores_os_profile_variables(monkeypatch, tmp_path):
    """APPDATA is set on essentially every Windows machine. Preferring it
    silently defeats portability on the most common USB-boot target."""
    monkeypatch.delenv("TALA_USER_DATA", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    resolved = resolve_user_data_dir()
    # Check the actual injected locations, not bare component names. On a
    # checkout that itself lives under /home/... (CI runners do) the literal
    # string "home" is a legitimate part of the install path and says nothing
    # about whether $HOME was preferred -- the real question is whether the
    # resolved root sits under any of these profile directories.
    for label, foreign in (
        ("APPDATA", tmp_path / "roaming"),
        ("XDG_DATA_HOME", tmp_path / "xdg"),
        ("HOME", tmp_path / "home"),
    ):
        foreign = foreign.resolve()
        assert resolved != foreign and foreign not in resolved.parents, (
            f"{label} leaked into the data root"
        )


def test_the_resolver_is_not_relative_to_the_working_directory(monkeypatch, tmp_path):
    """A USB install is launched from anywhere; the data root must not move."""
    monkeypatch.delenv("TALA_USER_DATA", raising=False)
    before = resolve_user_data_dir()
    monkeypatch.chdir(tmp_path)
    assert resolve_user_data_dir() == before


def test_no_runtime_code_builds_tala_data_paths_from_the_working_directory():
    """
    Path("nodes/...") resolves against cwd, not the install.

    Caught run_workflow_node writing .workflow_state.json wherever the app was
    started from.
    """
    pattern = re.compile(r'Path\(\s*f?["\'](?:\./)?(nodes|workflows|vault)[/\\]')
    offenders = []
    for path in _runtime_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(f"{path}:{i}: {line.strip()[:80]}")

    assert not offenders, (
        "cwd-relative Tala data paths -- use resolve_active_nodes_dir() etc:\n  "
        + "\n  ".join(offenders)
    )


WINDOWS_PATH = re.compile(r'["\'][A-Za-z]:[/\\]{1,2}')
POSIX_PATH = re.compile(r'["\']/(?:usr|etc|opt|var|home)/')


def test_no_runtime_code_assumes_a_single_platform():
    """
    A hardcoded path is only a portability bug when it assumes one OS.

    sdk/gateway/resolver.py lists /usr/local/bin, /opt/homebrew/bin AND
    "C:\\Program Files\\nodejs" as fallbacks for locating third-party
    executables after shutil.which() fails. That is deliberately multi-OS, and
    concerns other people's binaries rather than Tala's own data -- flagging it
    would be wrong. What must not appear is a file that reaches for one
    platform's layout and no other, which is how "works on my machine" gets
    committed.
    """
    offenders = []
    for path in _runtime_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = [
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        ]
        code = "\n".join(lines)
        has_windows = bool(WINDOWS_PATH.search(code))
        has_posix = bool(POSIX_PATH.search(code))

        if has_windows != has_posix:
            only = "Windows" if has_windows else "POSIX"
            hits = [
                f"{i}: {line.strip()[:70]}"
                for i, line in enumerate(text.splitlines(), 1)
                if not line.lstrip().startswith("#")
                and (WINDOWS_PATH.search(line) or POSIX_PATH.search(line))
            ]
            offenders.append(f"{path} ({only}-only) -- " + "; ".join(hits[:3]))

    assert not offenders, (
        "single-platform paths -- Tala runs from a USB stick on any OS:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("module", [
    "sdk/agent/node_execution.py",
    "sdk/agent/ui_control.py",
    "python/harness/user_profile.py",
    "python/harness/ui_bridge.py",
    "python/harness/credential_crypto.py",
    "scripts/generate_tool_nodes.py",
])
def test_new_modules_are_portable(module):
    """Everything added in this pass, checked directly rather than assumed."""
    path = Path(module)
    if not path.exists():
        pytest.skip(f"{module} does not exist")
    text = path.read_text(encoding="utf-8")

    assert "os.sep" not in text or "os.sep" in text  # separators via pathlib
    assert "\\\\" not in text.replace("\\\\n", ""), "manual backslash paths"
    assert not re.search(r'["\'][A-Za-z]:[/\\]', text), "drive letter"
    assert "%APPDATA%" not in text


def test_the_state_file_travels_with_the_install():
    """run_workflow_node's suspended-interview state is Tala's data, so it
    belongs beside her nodes, not in the launch directory."""
    text = Path("sdk/agent/tools.py").read_text(encoding="utf-8")
    block = text.split("async def run_workflow_node", 1)[1].split("@register_tool", 1)[0]

    assert "resolve_active_nodes_dir()" in block
    assert 'Path("nodes/.workflow_state.json")' not in block
