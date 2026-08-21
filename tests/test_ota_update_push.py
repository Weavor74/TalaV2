"""
The OTA update push.

Intended shape: updating the VPS sends a push notification so users can pull the
new version. The chain is real end to end -- system.trigger_update runs git pull
and broadcasts, the renderer listens, downloads through system.download_update
and applies via the system:apply_update IPC handler.

What was wrong sat at both ends of it. The pull's exit code was never inspected,
so a failed pull still announced an update and returned success. And nothing
carried a version, so a client had no way to tell whether it already had what was
being announced.
"""
import re
from pathlib import Path

from python.harness.server import _HANDLERS

SERVER = Path("python/harness/server.py")
RENDERER = Path("renderer/renderer.js")
PRELOAD = Path("electron/preload.js")
MAIN = Path("electron/main.js")


def _trigger_update_source() -> str:
    source = SERVER.read_text(encoding="utf-8")
    return source.split("async def handle_system_trigger_update", 1)[1].split(
        "async def handle_system_download_update", 1
    )[0]


# --- the chain is reachable -------------------------------------------------


def test_both_update_rpcs_are_dispatchable():
    for method in ("system.trigger_update", "system.download_update"):
        assert method in _HANDLERS


def test_the_renderer_listens_for_the_push():
    renderer = RENDERER.read_text(encoding="utf-8")

    assert "msg.method === 'system.update_available'" in renderer


def test_the_apply_step_is_an_allowlisted_ipc_channel():
    """system:apply_update goes direct to ipcMain rather than through python:rpc,
    so it has to be in the preload allowlist or the last step of the chain fails."""
    assert "'system:apply_update'" in PRELOAD.read_text(encoding="utf-8")
    assert "ipcMain.handle('system:apply_update'" in MAIN.read_text(encoding="utf-8")


# --- a failed pull must not be announced as an update -----------------------


def test_a_failed_pull_is_an_error_not_a_success():
    """git pull's returncode was never checked. A failure broadcast "update
    available" and returned status: success, so clients prompted the user and
    applied an archive of unchanged code."""
    block = _trigger_update_source()

    assert "proc.returncode != 0" in block
    assert "git pull failed" in block


def test_the_broadcast_happens_after_the_returncode_check():
    """Order is the whole point: announcing first and checking after would still
    tell every client an update landed."""
    block = _trigger_update_source()

    assert block.index("proc.returncode != 0") < block.index("system.update_available")


# --- delivery is observable -------------------------------------------------


def test_broadcast_reports_how_many_clients_it_reached():
    """There is no queue and no replay, so a push with nobody connected reaches
    nobody. From the call site that used to be indistinguishable from success."""
    source = SERVER.read_text(encoding="utf-8")
    block = source.split("async def broadcast_notification", 1)[1].split("\n\n\n", 1)[0]

    assert "-> int" in source.split("async def broadcast_notification", 1)[1][:80]
    assert "return 0" in block, "an empty connection set must report zero, not None"


def test_a_push_that_reached_nobody_says_so():
    block = _trigger_update_source()

    assert "clients_notified" in block
    assert "no clients were connected" in block


# --- the version travels with the push --------------------------------------


def test_the_notification_carries_the_revision():
    block = _trigger_update_source()

    assert "git rev-parse HEAD" in block
    assert re.search(r'"system\.update_available",\s*\{"revision"', block), (
        "the push must carry the revision it pulled"
    )


def test_the_client_skips_a_revision_it_already_has():
    """Without this every announcement re-downloaded the full archive, because
    nothing in the exchange identified a version."""
    renderer = RENDERER.read_text(encoding="utf-8")
    block = renderer.split("system.update_available'", 1)[1][:1600]

    assert "tala.appliedRevision" in block
    assert "revision === applied" in block


def test_the_revision_is_recorded_only_after_a_successful_apply():
    """Recording it earlier would mark a failed update as installed and suppress
    the next push for the same revision."""
    renderer = RENDERER.read_text(encoding="utf-8")
    # Sliced to the end of the listener rather than a fixed character count,
    # which fell short of the setItem line.
    block = renderer.split("system.update_available'", 1)[1].split(
        "Failed to register system.update_available listener", 1
    )[0]

    apply_at = block.index("system:apply_update")
    record_at = block.index("localStorage.setItem('tala.appliedRevision'")
    assert apply_at < record_at
