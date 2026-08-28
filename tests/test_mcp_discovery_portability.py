"""
MCP discovery on a machine that is not Windows.

Steven's constraint is that Tala runs from a USB stick across operating
systems. Discovery was written as a single Windows table: five entries using
``%APPDATA%`` / ``%USERPROFILE%`` and two using backslash separators, expanded
through ``os.path.expandvars``.

On POSIX ``expandvars`` expands ``$VAR`` and ``${VAR}`` only. ``%APPDATA%``
passes through untouched and becomes a *literal* first path component, and
backslashes are ordinary filename characters — so seven of the eight locations
could never match on Linux or macOS (UI-097).

The invisible half was worse than the broken half: finding zero servers looks
exactly like having zero servers installed, so "Scan for servers" appeared to
work and always came back empty. The result now reports where it *looked*, so
the two are distinguishable.

These tests assert on path **components** rather than rendered strings, because
a rendered path is separator-dependent and would only ever be checked on
whichever machine happens to run the suite.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sdk.agent import mcp_bridge


@pytest.fixture
def platform(monkeypatch):
    """Pretend to be another operating system."""
    def become(name, home, env=None):
        monkeypatch.setattr(sys, "platform", name)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path(home)))
        for key in ("APPDATA", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(key, raising=False)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
    return become


def parts_of(base, fragments):
    return mcp_bridge._resolve_location(base, tuple(fragments)).parts


# -- the config base per platform ---------------------------------------------

def test_macos_uses_application_support(platform):
    platform("darwin", "/Users/steve")

    assert mcp_bridge._config_base().parts[-2:] == ("Library", "Application Support")


def test_linux_uses_dot_config(platform):
    platform("linux", "/home/steve")

    assert mcp_bridge._config_base().parts[-1] == ".config"


def test_linux_honours_xdg_config_home(platform):
    """A user who sets it means it."""
    platform("linux", "/home/steve", {"XDG_CONFIG_HOME": "/home/steve/.elsewhere"})

    assert mcp_bridge._config_base().parts[-1] == ".elsewhere"


def test_windows_uses_appdata(platform):
    platform("win32", "C:/Users/steve", {"APPDATA": "C:/Users/steve/AppData/Roaming"})

    assert mcp_bridge._config_base().parts[-1] == "Roaming"


def test_windows_falls_back_when_appdata_is_unset(platform):
    """A missing APPDATA should not resolve to a relative path whose first
    component is the literal text of a variable name — which is precisely how
    this broke on POSIX."""
    platform("win32", "C:/Users/steve")

    parts = mcp_bridge._config_base().parts
    assert parts[-2:] == ("AppData", "Roaming")
    assert not any("%" in part for part in parts)


# -- the table resolves to real places on every platform ----------------------

@pytest.mark.parametrize("name,home", [
    ("darwin", "/Users/steve"),
    ("linux", "/home/steve"),
    ("win32", "C:/Users/steve"),
])
def test_no_location_contains_an_unexpanded_variable(platform, name, home):
    """The defect in one assertion: on POSIX every %APPDATA% entry became a
    literal path component."""
    platform(name, home)

    for label, base, *fragments in mcp_bridge._DISCOVERY_LOCATIONS:
        if base == "cwd":
            continue
        for part in parts_of(base, fragments):
            assert "%" not in part, f"{label}: {part}"
            assert "$" not in part, f"{label}: {part}"


def test_no_table_entry_contains_a_separator():
    """A backslash is a filename character on POSIX, so a fragment written with
    one is a single path component that will never exist. Fragments are checked
    rather than resolved paths: a resolved path carries the running platform's
    root, which says nothing about the table."""
    for label, base, *fragments in mcp_bridge._DISCOVERY_LOCATIONS:
        for fragment in fragments:
            assert "\\" not in fragment, f"{label}: {fragment}"
            assert "/" not in fragment, f"{label}: {fragment}"


def test_claude_desktop_is_in_the_right_place_per_platform(platform):
    """The paths genuinely differ, so syntax repair alone would not have been
    enough."""
    platform("darwin", "/Users/steve")
    assert "Application Support" in parts_of("config", ("Claude", "x.json"))

    platform("linux", "/home/steve")
    assert ".config" in parts_of("config", ("Claude", "x.json"))


def test_the_home_based_locations_are_the_same_everywhere(platform):
    """Cursor, Windsurf and Claude Code keep config under the home directory on
    every platform, so they need no per-OS entry."""
    for name, home in (("darwin", "/Users/s"), ("linux", "/home/s"), ("win32", "C:/U/s")):
        platform(name, home)
        assert parts_of("home", (".cursor", "mcp.json"))[-2:] == (".cursor", "mcp.json")


# -- a path the user typed ----------------------------------------------------

def test_a_windows_style_variable_expands_anywhere(platform, monkeypatch):
    """Someone moving a portable install between machines will paste a path in
    the syntax of the machine they came from."""
    monkeypatch.setenv("MYCONF", "/tmp/conf")

    resolved = mcp_bridge._expand_supplied("%MYCONF%/mcp.json")

    assert "MYCONF" not in str(resolved)
    assert "conf" in resolved.parts


def test_a_posix_style_variable_still_expands(monkeypatch):
    monkeypatch.setenv("MYCONF", "/tmp/conf")

    resolved = mcp_bridge._expand_supplied("$MYCONF/mcp.json")

    assert "conf" in resolved.parts


def test_an_unset_variable_is_left_alone_rather_than_emptied():
    """Expanding an unknown name to nothing would silently change which path is
    checked."""
    resolved = mcp_bridge._expand_supplied("%NOT_A_REAL_VARIABLE%/mcp.json")

    assert "%NOT_A_REAL_VARIABLE%" in str(resolved)


# -- nothing found is not the same as could not look --------------------------

def test_the_result_says_where_it_looked():
    result = mcp_bridge.discover_mcp_servers()

    assert result["success"]
    assert result["looked_in"], "the scan does not report its locations"
    assert result["platform"]
    for entry in result["looked_in"]:
        assert {"source", "path", "exists"} <= set(entry)


def test_every_configured_location_is_reported_whether_or_not_it_exists():
    result = mcp_bridge.discover_mcp_servers()

    assert len(result["looked_in"]) >= len(mcp_bridge._DISCOVERY_LOCATIONS)


def test_a_supplied_path_is_reported_too():
    result = mcp_bridge.discover_mcp_servers(extra_paths=["/nowhere/mcp.json"])

    supplied = [e for e in result["looked_in"] if e["source"] == "supplied"]
    assert len(supplied) == 1
    assert supplied[0]["exists"] is False


def test_the_panel_distinguishes_the_two_cases():
    """A button that always comes back empty must not read the same when it
    found nothing as when it could not look."""
    renderer = (Path(__file__).resolve().parents[1] / "renderer" / "renderer.js"
                ).read_text(encoding="utf-8")

    assert "looked_in" in renderer
    assert "nothing went wrong" in renderer
