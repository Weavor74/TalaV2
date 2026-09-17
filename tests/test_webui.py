"""
tests/test_webui.py — the renderer, served to a browser.

The web UI is not a second implementation of the interface. It is the same
renderer/ directory with window.tala supplied by a shim instead of by Electron
preload, so what needs guarding is the seam: that the shim is delivered, that
the bridge performs the filesystem work Electron's main process used to do
within the same confinement, and that the server refuses to publish 247 RPC
handlers to a network without a token.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from python.harness import webui

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Path confinement — the rule Electron's safePath() enforced
# ---------------------------------------------------------------------------

def test_traversal_is_refused_not_clamped(tmp_path):
    (tmp_path / "inside.txt").write_text("ok", encoding="utf-8")
    with pytest.raises(PermissionError):
        webui._safe_path(tmp_path, "../../../etc/passwd")


def test_absolute_paths_outside_the_root_are_refused(tmp_path):
    with pytest.raises(PermissionError):
        webui._safe_path(tmp_path, str(Path(tmp_path).anchor or "/"))


def test_a_path_inside_the_root_resolves(tmp_path):
    (tmp_path / "a").mkdir()
    resolved = webui._safe_path(tmp_path, "a")
    assert resolved == (tmp_path / "a").resolve()


# ---------------------------------------------------------------------------
# Bridge channels return the shapes the renderer already expects
# ---------------------------------------------------------------------------

def test_file_read_matches_the_electron_shape(tmp_path):
    (tmp_path / "hello.txt").write_text("world", encoding="utf-8")
    out = webui._bridge_file_read(tmp_path, {"filePath": "hello.txt"})
    assert out["ok"] is True
    assert out["data"] == "world"
    assert out["size"] == 5
    assert set(out) == {"ok", "data", "size", "absolutePath", "fileUrl"}


def test_file_write_then_read_round_trips(tmp_path):
    written = webui._bridge_file_write(tmp_path, {"filePath": "x/y.txt", "data": "abc"})
    assert written["ok"] is True
    back = webui._bridge_file_read(tmp_path, {"filePath": "x/y.txt"})
    assert back["data"] == "abc"


def test_file_write_requires_data(tmp_path):
    with pytest.raises(ValueError):
        webui._bridge_file_write(tmp_path, {"filePath": "a.txt"})


def test_file_list_shape_and_ignores(tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")

    out = webui._bridge_file_list(tmp_path, {"dir": ".", "withSizes": True})
    names = {e["name"] for e in out["entries"]}
    assert "keep" in names and "f.txt" in names
    assert "node_modules" not in names, "ignored directories leaked into the listing"

    entry = next(e for e in out["entries"] if e["name"] == "f.txt")
    assert set(entry) >= {"name", "relativePath", "type", "absolutePath", "fileUrl"}
    assert entry["type"] == "file"
    assert entry["size"] == 1


def test_file_list_on_a_missing_directory_is_empty_not_an_error(tmp_path):
    """Matches ipc-handlers.js, which returns [] rather than throwing."""
    assert webui._bridge_file_list(tmp_path, {"dir": "nope"}) == {"ok": True, "entries": []}


def test_vault_read_is_refused_over_http(tmp_path):
    """
    Electron guarded vault reads by checking no outbound connections were open.
    A web server is itself a connection, so that check does not transfer and a
    weaker reimplementation would be worse than none.
    """
    with pytest.raises(PermissionError):
        webui._bridge_vault_read(tmp_path, {"name": "anything"})


# ---------------------------------------------------------------------------
# Refusing to become a remote shell
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TALA_WEB_UI", raising=False)
    assert await webui.start() is None


@pytest.mark.asyncio
async def test_non_loopback_without_a_token_refuses_to_start(monkeypatch):
    """
    This UI reaches shell execution, source patching and the vault. Binding it
    to a reachable interface without a token would publish a remote shell, so
    the server declines rather than starting insecurely.
    """
    monkeypatch.setenv("TALA_WEB_UI", "1")
    monkeypatch.setenv("TALA_WEB_HOST", "0.0.0.0")
    monkeypatch.delenv("TALA_WEB_TOKEN", raising=False)
    assert await webui.start() is None


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------

def test_the_shim_is_not_injected_inline():
    """
    index.html sets script-src 'self', so an inline <script> is refused by the
    browser and never runs. That failed almost invisibly the first time: the
    shim fell back to its default WebSocket URL and connected anyway, so the UI
    came up working while the config -- including the auth token -- had never
    been delivered.
    """
    assert "<script>window.__TALA_WEB_CONFIG__" not in webui._SHIM_TAG
    assert "/tala-web-config.js" in webui._SHIM_TAG
    assert "/tala-web-shim.js" in webui._SHIM_TAG


def test_the_shim_file_exists_and_defines_window_tala():
    shim = (ROOT / "renderer" / "tala-web-shim.js").read_text(encoding="utf-8")
    assert "window.tala" in shim
    for member in ("invoke", "request", "on", "allowedChannels"):
        assert member in shim, f"the shim does not provide window.tala.{member}"


def test_the_shim_covers_every_preload_channel():
    """
    If preload.js grows a channel the shim does not know, the desktop app gains
    a capability the web UI silently lacks. Better to fail here than to have a
    button that does nothing in the browser.
    """
    preload = (ROOT / "electron" / "preload.js").read_text(encoding="utf-8")
    shim = (ROOT / "renderer" / "tala-web-shim.js").read_text(encoding="utf-8")

    import re

    block = preload.split("ALLOWED_CHANNELS")[1].split("]")[0]
    channels = set(re.findall(r"['\"]([a-z]+:[a-z_]+)['\"]", block))
    assert channels, "could not parse the preload channel whitelist"

    missing = sorted(c for c in channels if c not in shim)
    assert not missing, (
        f"preload.js exposes channels the web shim never mentions: {missing}"
    )


def test_bridge_and_rpc_channels_together_cover_the_whitelist():
    """Every channel is either served by the bridge or is a JSON-RPC method."""
    shim = (ROOT / "renderer" / "tala-web-shim.js").read_text(encoding="utf-8")
    for channel in webui._BRIDGE:
        assert channel in shim, f"{channel} is served but the shim never asks for it"


def test_ports_do_not_collide_with_the_harness():
    """
    8766 is MCP_PORT. Defaulting there produced a web UI that appeared to start
    and then 404'd every request, because the MCP SSE server owned the socket
    and was answering.
    """
    from python.harness.config import settings

    src = (ROOT / "python" / "harness" / "webui.py").read_text(encoding="utf-8")
    default_port = int(src.split('TALA_WEB_PORT") or ')[1].split(")")[0])
    assert default_port != int(settings.WS_PORT)
    assert default_port != int(settings.MCP_PORT)


# ---------------------------------------------------------------------------
# Availability — the failure that made the UI look broken
# ---------------------------------------------------------------------------

def test_electron_starts_the_harness_with_the_web_ui_enabled():
    """
    Without this the web UI existed only when the harness was launched by
    `npm run web`, so opening the URL while the desktop app was running gave
    connection-refused. Nothing was serving it, which on screen is
    indistinguishable from a broken page.
    """
    main_js = (ROOT / "electron" / "main.js").read_text(encoding="utf-8")
    env_block = main_js.split("function firstRunEnv")[1].split("}")[0]
    assert "TALA_WEB_UI" in env_block, (
        "the desktop app spawns its harness without TALA_WEB_UI, so the web UI "
        "is dead whenever Tala is running normally"
    )
    assert "process.env.TALA_WEB_UI ??" in env_block, (
        "TALA_WEB_UI is forced rather than defaulted; setting it to 0 must "
        "still turn the web UI off"
    )




def test_electron_first_run_env_splits_http_and_ws_tokens():
    """UI-audit C3: desktop spawn must pin distinct TALA_WS_TOKEN and TALA_WEB_TOKEN."""
    main_js = (ROOT / "electron" / "main.js").read_text(encoding="utf-8")
    env_block = main_js.split("function firstRunEnv")[1].split("function ", 1)[0]
    assert "TALA_WS_TOKEN: wsToken" in env_block
    assert "TALA_WEB_TOKEN: webUiToken" in env_block
    assert "ensureWebUiToken" in env_block
    assert "TALA_WEB_TOKEN: ensureWsToken" not in env_block
    ensure_ws_body = (
        main_js.split("function ensureWsToken(appPath)", 1)[1]
        .split("{", 1)[1]
        .split("}", 1)[0]
    )
    assert "ensureWsTokenFile" in ensure_ws_body
    assert "TALA_WEB_TOKEN" not in ensure_ws_body


def test_web_ui_can_be_turned_off():
    """`TALA_WEB_UI=0` must mean off, not 'any value is truthy'."""
    import os

    original = os.environ.get("TALA_WEB_UI")
    try:
        os.environ["TALA_WEB_UI"] = "0"
        import asyncio
        assert asyncio.run(webui.start()) is None
    finally:
        if original is None:
            os.environ.pop("TALA_WEB_UI", None)
        else:
            os.environ["TALA_WEB_UI"] = original


def test_launcher_reuses_a_running_harness():
    """
    Starting a second harness when one is already up collides on the WebSocket
    port. The launcher probes for one and serves the UI alone if it finds it.
    """
    src = (ROOT / "scripts" / "start-web-ui.js").read_text(encoding="utf-8")
    assert "harnessIsRunning" in src
    assert "python.harness.webui" in src, "no UI-only mode to fall back to"
    assert "python.harness.server" in src, "no way to start a harness when none exists"


def test_webui_has_a_standalone_entry_point():
    """The UI-only mode the launcher depends on must actually be runnable."""
    src = (ROOT / "python" / "harness" / "webui.py").read_text(encoding="utf-8")
    assert '__name__ == "__main__"' in src
    assert "async def _main" in src


def test_standalone_mode_also_refuses_a_tokenless_public_bind():
    """The safety rule must not be weaker in the mode people reach for."""
    src = (ROOT / "python" / "harness" / "webui.py").read_text(encoding="utf-8")
    main_body = src.split("async def _main")[1]
    assert "TALA_WEB_TOKEN" in main_body
    assert "Refusing to serve" in main_body


def test_file_read_refuses_vault_paths(tmp_path):
    """UI-audit C4: vault:read is gated; file:read must not bypass it."""
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "key.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(PermissionError):
        webui._bridge_file_read(tmp_path, {"filePath": "vault/key.txt"})


def test_config_endpoint_omits_secrets():
    """UI-audit C3: /tala-web-config.js must not embed HTTP or WS secrets."""
    src = (ROOT / "python" / "harness" / "webui.py").read_text(encoding="utf-8")
    # The config payload construction must not include these keys.
    config_fn = src.split("async def config(", 1)[1].split("async def ", 1)[0]
    assert '"wsToken"' not in config_fn
    assert '"token": token' not in config_fn
    assert "wsTicketUrl" in config_fn
