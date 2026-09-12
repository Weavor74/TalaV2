"""
The OTA update push — now a wake-up call, not a delivery mechanism.

Intended shape after the update-story consolidation (P2 review item 21): there
is exactly ONE way bytes land on a client — the signed ``app.update`` path in
electron/main.js, pinned to the release host and verified against the sha256
map in version.json. ``system.trigger_update`` still git-pulls the VPS's own
checkout and broadcasts ``system.update_available``, but the client's response
to that push is to run the same version.json check the 2-hour poll runs
(checkForUpdates), never to pull bytes over the WebSocket.

The unsigned second path — ``system.download_update`` repackaging the live
checkout, applied by the ``system:apply_update`` IPC channel — is deliberately
gone, and these tests pin its absence.
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
    block = source.split("async def handle_system_trigger_update", 1)[1]
    return block.split("\nasync def ", 1)[0]


# --- exactly one delivery mechanism ------------------------------------------


def test_the_push_rpc_is_dispatchable_and_the_unsigned_download_is_not():
    assert "system.trigger_update" in _HANDLERS
    assert "system.download_update" not in _HANDLERS, (
        "system.download_update is the unsigned second update path; "
        "it was removed on purpose (P2 item 21)"
    )


def test_the_unsigned_apply_channel_is_gone_everywhere():
    """system:apply_update extracted harness-supplied bytes with no published
    hash to check. Nothing may reference it: not the preload allowlist, not an
    ipcMain handler, not the renderer."""
    for path in (PRELOAD, MAIN, RENDERER):
        source = path.read_text(encoding="utf-8")
        assert "ipcMain.handle('system:apply_update'" not in source
        assert "'system:apply_update'" not in source, (
            f"{path} still references the removed system:apply_update channel"
        )


def test_the_renderer_routes_the_push_into_the_signed_path():
    renderer = RENDERER.read_text(encoding="utf-8")

    assert "msg.method === 'system.update_available'" in renderer
    listener = renderer.split("system.update_available'", 1)[1].split(
        "Failed to register system.update_available listener", 1
    )[0]
    assert "checkForUpdates()" in listener, (
        "the push must trigger the same version.json check the poll uses"
    )
    assert "download_update" not in listener


def test_the_signed_path_pins_host_and_checks_the_published_hash():
    main = MAIN.read_text(encoding="utf-8")

    assert "UPDATE_HOST = 'talacore.app'" in main
    assert "fetchExpectedUpdateHash" in main


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
