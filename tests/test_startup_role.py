"""
One artifact, one flag, one place that decides the role.

The same code is a client or a connection node depending on how it was
started. That only stays true if every way of starting it reaches the same
parser -- and there are six ways: the Electron app, three shell scripts, the
web-UI launcher, and the `tala` console script.

The interesting failures here are quiet ones. A client that enrols strangers
because a role leaked into it, or a VPS whose `--vps` went to an entry point
that never parsed it and came up as a client, both look like a working
process.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# The role is off unless asked for
# ---------------------------------------------------------------------------


def test_a_plain_start_is_a_client():
    from python.harness import vps_mode

    assert vps_mode.is_enabled() is False


def test_only_the_vps_script_asks_for_the_role():
    """
    A desktop launcher that passed --vps would turn every install into a
    connection node that enrols strangers.
    """
    assert "--vps" in _read("scripts", "start-tala-vps.sh")

    for launcher in ("start-tala.sh", "start-tala.ps1"):
        assert "--vps" not in _read("scripts", launcher), f"{launcher} starts a connection node"

    assert "--vps" not in _read("scripts", "start-web-ui.js")
    assert "--vps" not in _read("electron", "main.js")


def test_the_role_does_not_travel_in_the_environment():
    """
    The old switch was an env var, which lingers in a shell or a copied .env.
    The launcher must not set it back.
    """
    vps_script = _read("scripts", "start-tala-vps.sh")
    assert "export TALA_VPS_ACCEPT_ANY_TALA" not in vps_script


# ---------------------------------------------------------------------------
# Every entry point reaches the parser
# ---------------------------------------------------------------------------


def test_the_server_module_is_runnable_as_a_script():
    """Electron spawns server.py by path, so __main__ has to be wired."""
    src = _read("python", "harness", "server.py")
    assert 'if __name__ == "__main__":' in src
    assert re.search(r'if __name__ == "__main__":\s*\n\s*main\(\)', src)


def test_the_console_script_actually_starts_something():
    """
    `tala start` used to print a line and start nothing -- the launch was a
    comment. A published entry point that reports success and leaves no
    server running is worse than one that fails.
    """
    src = _read("python", "cli", "main.py")

    assert "harness_main()" in src, "tala start does not reach the harness"
    assert "# asyncio.run(start_server" not in src, "the launch is still commented out"


def test_the_console_script_can_select_the_role():
    src = _read("python", "cli", "main.py")
    assert '"--vps"' in src


def test_the_console_script_delegates_rather_than_deciding():
    """
    Two places that each decide what --vps means is how they come to
    disagree. The CLI hands the flag to the server's parser; it does not
    call vps_mode itself.
    """
    src = _read("python", "cli", "main.py")
    assert "vps_mode" not in src, "the CLI is deciding the role a second time"


@pytest.mark.asyncio
async def test_the_flag_is_what_opens_enrolment(monkeypatch):
    """
    The property the whole split rests on: the same process refuses
    strangers or accepts them purely on the flag.
    """
    from python.harness import server, vps_mode

    monkeypatch.delenv("TALA_VPS_ACCEPT_ANY_TALA", raising=False)
    vps_mode.mode.disable()
    try:
        assert server._accept_any_tala_client_enabled() is False
        vps_mode.mode.enable("--vps")
        assert server._accept_any_tala_client_enabled() is True
    finally:
        vps_mode.mode.disable()


@pytest.mark.asyncio
async def test_relay_surface_follows_the_same_flag(monkeypatch):
    """A client must not answer relay verbs even if something asks."""
    import json

    from python.harness import server, vps_mode

    vps_mode.mode.disable()
    try:
        body = json.loads(await server.handle_relay_peers({}, 1))
        assert "not running as a connection node" in body["error"]["message"]
    finally:
        vps_mode.mode.disable()
