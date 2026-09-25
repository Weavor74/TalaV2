"""MCP Bridge synthetic plugin must not render as INVALID/UNKNOWN by omission."""
from __future__ import annotations

from sdk.gateway import plugins


def test_bridge_manifest_stamps_valid_and_runtime_status(monkeypatch):
    monkeypatch.setattr(plugins, "mcp_server_permissions", lambda: {})

    class Empty:
        def list_servers(self):
            return []

    monkeypatch.setattr(plugins, "_mcp_bridge_runtime", lambda: {
        "valid": True,
        "runtime_status": "idle",
        "error": None,
        "next_action": "Add an MCP server under Settings → Plugins.",
        "mcp_servers": [],
        "server_count": 0,
    })
    # Prefer testing through public _mcp_bridge_manifest with runtime stubbed.
    manifest = plugins._mcp_bridge_manifest()
    assert manifest["id"] == plugins.MCP_BRIDGE_ID
    assert manifest["valid"] is True
    assert manifest["runtime_status"] == "idle"
    assert manifest["synthetic"] is True
    assert manifest["path"] is None
    assert manifest.get("next_action")


def test_bridge_manifest_ready_when_servers_configured(monkeypatch):
    monkeypatch.setattr(plugins, "mcp_server_permissions", lambda: {"fs": ["fs:read"]})
    monkeypatch.setattr(plugins, "_mcp_bridge_runtime", lambda: {
        "valid": True,
        "runtime_status": "ready",
        "error": None,
        "next_action": None,
        "mcp_servers": ["fs"],
        "server_count": 1,
    })
    manifest = plugins._mcp_bridge_manifest()
    assert manifest["valid"] is True
    assert manifest["runtime_status"] == "ready"
    assert not manifest.get("error")


def test_bridge_manifest_precise_error_when_transport_missing(monkeypatch):
    monkeypatch.setattr(plugins, "mcp_server_permissions", lambda: {"x": []})
    monkeypatch.setattr(plugins, "_mcp_bridge_runtime", lambda: {
        "valid": False,
        "runtime_status": "error",
        "error": "MCP server(s) missing transport (command/url): x",
        "next_action": "Open Settings → Plugins and set transport.",
        "mcp_servers": ["x"],
        "server_count": 1,
    })
    manifest = plugins._mcp_bridge_manifest()
    assert manifest["valid"] is False
    assert manifest["runtime_status"] == "error"
    assert "transport" in (manifest.get("error") or "").lower()
    assert manifest.get("next_action")


def test_scan_includes_honest_bridge_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(plugins, "mcp_server_permissions", lambda: {})
    monkeypatch.setattr(plugins, "_mcp_bridge_runtime", lambda: {
        "valid": True,
        "runtime_status": "idle",
        "error": None,
        "next_action": "Add MCP server",
        "mcp_servers": [],
        "server_count": 0,
    })
    results = plugins.scan_plugins_directory(str(tmp_path))
    bridge = next(r for r in results if r.get("id") == plugins.MCP_BRIDGE_ID)
    assert bridge["valid"] is True
    assert bridge["runtime_status"] in {"idle", "ready"}
    assert bridge.get("runtime_status") != "unknown"
