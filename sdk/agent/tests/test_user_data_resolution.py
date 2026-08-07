"""
sdk/agent/tests/test_user_data_resolution.py
==============================================
Regression coverage for sdk.agent.documentation_nodes' user-data directory
resolvers: resolve_user_data_dir(), resolve_active_nodes_dir(), and
resolve_active_workflows_dir().

Traced live: this exact hardcoded path -- Path.home()/"AppData"/"Roaming"/
"tala-engine" -- was duplicated across 24 call sites in 7 files (server.py
alone had 16). On any machine/OS/environment where APPDATA isn't set (not
Windows, or a minimal/containerized environment where it's unset), this
would silently construct a nonsensical Windows-only path instead of
resolving anywhere real. Every one of those call sites now goes through
these three functions instead, so this file is the single place that
needs to prove the resolution order actually works.
"""
from __future__ import annotations

from pathlib import Path

from sdk.agent.documentation_nodes import (
    resolve_active_nodes_dir,
    resolve_active_workflows_dir,
    resolve_user_data_dir,
)


def test_tala_user_data_takes_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "should-be-ignored"))

    assert resolve_user_data_dir() == tmp_path
    assert resolve_active_nodes_dir() == tmp_path / "nodes"
    assert resolve_active_workflows_dir() == tmp_path / "workflows"


def test_appdata_ignored_when_tala_user_data_unset(tmp_path, monkeypatch):
    # Tala is meant to be portable: installed on a USB stick or any external
    # drive, its data must travel with the install and behave identically on
    # any machine/OS it's plugged into. APPDATA is set on essentially every
    # real Windows machine, so auto-preferring it (the old behavior) would
    # silently defeat that on exactly the OS most likely to be the USB-boot
    # target -- data would land in the host machine's profile instead of on
    # the drive. APPDATA is therefore no longer auto-detected; the only way
    # to opt into an OS-profile-style location is the explicit
    # TALA_USER_DATA override (see test_tala_user_data_takes_priority above).
    monkeypatch.delenv("TALA_USER_DATA", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))

    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    assert resolve_user_data_dir() == repo_root
    assert resolve_active_nodes_dir() == repo_root / "nodes"
    assert resolve_active_workflows_dir() == repo_root / "workflows"


def test_falls_back_to_application_directory_when_neither_env_var_set(monkeypatch):
    # The actual reported scenario: every install may be on a different
    # machine/OS, and APPDATA specifically may not exist there at all (it's
    # a Windows-only convention). Must not raise, and must not silently
    # construct a bogus path -- it should resolve to somewhere real,
    # relative to the application's own install location.
    monkeypatch.delenv("TALA_USER_DATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    nodes_dir = resolve_active_nodes_dir()
    workflows_dir = resolve_active_workflows_dir()
    base_dir = resolve_user_data_dir()

    assert nodes_dir.name == "nodes"
    assert workflows_dir.name == "workflows"
    assert nodes_dir.parent == base_dir
    assert workflows_dir.parent == base_dir
    # Anchored under the actual repo/install root, not a hardcoded
    # OS-specific profile directory (Path.home()/"AppData"/..., etc.).
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    assert base_dir == repo_root
    # Sanity: this must be a real, absolute, usable path -- not a relative
    # fragment that would resolve differently depending on process cwd.
    assert base_dir.is_absolute()
