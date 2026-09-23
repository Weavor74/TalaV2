"""
sdk/gateway/plugins.py
======================
Plugin Manifest schema loader and validator.
"""
import json
import logging
import os
from typing import Dict, Any, List

log = logging.getLogger(__name__)

ALLOWED_PERMISSIONS = {
    "fs:read", "fs:write", "shell:execute", "network:outbound",
    "database:read", "memory:read", "profile:read"
}

class PluginValidationError(Exception):
    pass

def load_and_validate_manifest(manifest_path: str) -> Dict[str, Any]:
    """
    Load a plugin.json file and validate its schema.
    """
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        raise PluginValidationError(f"Invalid JSON in manifest: {e}")
    except IOError as e:
        raise PluginValidationError(f"Could not read manifest file: {e}")
        
    required_fields = ["id", "name", "version", "entrypoint"]
    for field in required_fields:
        if field not in manifest:
            raise PluginValidationError(f"Missing required field: '{field}'")
            
    # Validate ID (alphanumeric and dashes/dots only)
    plugin_id = manifest["id"]
    if not isinstance(plugin_id, str) or not all(c.isalnum() or c in "-._" for c in plugin_id):
        raise PluginValidationError(f"Invalid plugin ID: '{plugin_id}'. Must be alphanumeric with dashes/dots/underscores.")
        
    # Validate Permissions
    permissions = manifest.get("permissions", [])
    if not isinstance(permissions, list):
        raise PluginValidationError("'permissions' must be a list of strings")
        
    for p in permissions:
        if p not in ALLOWED_PERMISSIONS:
            raise PluginValidationError(f"Undeclared or invalid permission requested: '{p}'")
            
    # Normalize entrypoint
    entrypoint = manifest["entrypoint"]
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise PluginValidationError("Entrypoint must be a non-empty string")
        
    # Set default trust level
    manifest.setdefault("trust_level", "untrusted")
    manifest.setdefault("tools", [])
    manifest.setdefault("mcp_servers", [])
    
    return manifest

def scan_plugins_directory(plugins_dir: str = None) -> List[Dict[str, Any]]:
    """
    Scans the plugins directory and parses all manifests.
    Returns a list of parsed manifests, including a 'valid' boolean and any 'error' string.
    """
    if plugins_dir is None:
        # Default to root plugins folder
        plugins_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "plugins")
        
    plugins_dir = os.path.abspath(plugins_dir)
    results = []
    if not os.path.exists(plugins_dir):
        log.warning(f"Plugins directory not found: {plugins_dir}")
        return [mcp_bridge_manifest()]
        
    seen_ids = set()
        
    for entry in os.listdir(plugins_dir):
        plugin_path = os.path.abspath(os.path.join(plugins_dir, entry))
        
        # Enforce that the plugin directory does not escape the plugins folder (e.g. via symlinks)
        if not plugin_path.startswith(plugins_dir):
            log.warning(f"Plugin path escape detected and ignored: {plugin_path}")
            continue
            
        if os.path.isdir(plugin_path):
            manifest_path = os.path.join(plugin_path, "plugin.json")
            if os.path.isfile(manifest_path):
                try:
                    manifest = load_and_validate_manifest(manifest_path)
                    
                    if manifest["id"] in seen_ids:
                        raise PluginValidationError(f"Duplicate plugin ID detected: '{manifest['id']}'")
                    seen_ids.add(manifest["id"])
                    
                    manifest["valid"] = True
                    manifest["path"] = plugin_path
                    manifest["runtime_status"] = "loaded"
                    results.append(manifest)
                except PluginValidationError as e:
                    results.append({
                        "id": entry,
                        "name": entry,
                        "valid": False,
                        "error": str(e),
                        "path": plugin_path,
                        "version": "unknown",
                        "permissions": [],
                        "trust_level": "untrusted",
                        "runtime_status": "crashed"
                    })
    # The bridge is a plugin without a directory: its tools come from
    # servers configured at runtime, so there is no plugin.json to scan.
    # Without it here, run_tool refuses every MCP tool for a missing
    # manifest — which is exactly what it had been doing.
    results.append(mcp_bridge_manifest())
    return results

# ── MCP servers are plugins too ─────────────────────────────────────────────
#
# `run_tool` looks a tool's plugin_id up among the manifests scanned from
# plugins/. The bridge registers everything under "mcp-bridge", which has no
# directory and therefore no manifest, so every MCP tool was refused with
# "manifest not found" — not ungated but *dead*. MCP was made "actually usable"
# in 2c58af1 and the gateway has been refusing every call since.
#
# The manifest is derived rather than shipped as a file, because it has to
# reflect the servers actually configured: a fixed declaration would either
# over-grant (every server gets shell:execute) or under-grant (a legitimate
# server refused).

MCP_BRIDGE_ID = "mcp-bridge"


def mcp_server_permissions() -> Dict[str, List[str]]:
    """
    What each configured MCP server declares it needs.

    Read from the runtime registry rather than a manifest file, since MCP
    servers are configured while the app runs and cannot be known when a
    manifest is written.

    **A server that declares nothing gets nothing.** An MCP server is somebody
    else's process, and the absence of a declaration is not a request for
    everything.
    """
    try:
        from python.harness.mcp_manager import mcp_manager

        servers = mcp_manager.list_servers()
    except Exception as exc:
        log.debug("Could not read MCP server permissions: %s", exc)
        return {}

    out: Dict[str, List[str]] = {}
    for server in servers or []:
        if not isinstance(server, dict):
            continue
        name = str(server.get("name") or server.get("id") or "").strip()
        if not name:
            continue
        declared = server.get("permissions")
        out[name] = ([p for p in declared if p in ALLOWED_PERMISSIONS]
                     if isinstance(declared, list) else [])
    return out


def _mcp_bridge_runtime() -> Dict[str, Any]:
    """
    Honest runtime status for the synthetic MCP Bridge plugin card.

    The bridge has no plugin.json on disk, so a scan that only stamped
    ``id``/``permissions`` left the UI with falsy ``valid`` and missing
    ``runtime_status`` → INVALID / UNKNOWN even when the bridge itself was
    fine (smoke 2026-09-17: Test MCP Provider VALID, MCP Bridge INVALID).
    """
    try:
        from python.harness.mcp_manager import mcp_manager

        servers = mcp_manager.list_servers() or []
    except Exception as exc:
        return {
            "valid": False,
            "runtime_status": "error",
            "error": f"MCP manager unavailable: {type(exc).__name__}: {exc}",
            "next_action": "Restart the harness, then reopen Settings → Plugins.",
            "mcp_servers": [],
            "server_count": 0,
        }

    names: List[str] = []
    missing_transport: List[str] = []
    for server in servers:
        if not isinstance(server, dict):
            continue
        name = str(server.get("name") or server.get("id") or "").strip()
        if not name:
            continue
        names.append(name)
        cfg = server.get("config") if isinstance(server.get("config"), dict) else server
        has_transport = bool(
            (cfg or {}).get("command")
            or (cfg or {}).get("url")
            or (cfg or {}).get("transport")
        )
        if not has_transport:
            missing_transport.append(name)

    if missing_transport:
        listed = ", ".join(missing_transport)
        return {
            "valid": False,
            "runtime_status": "error",
            "error": f"MCP server(s) missing transport (command/url): {listed}",
            "next_action": (
                "Open Settings → Plugins → MCP servers; edit each listed server "
                "and set a stdio command or SSE URL, then re-test."
            ),
            "mcp_servers": sorted(names),
            "server_count": len(names),
        }

    if not names:
        return {
            "valid": True,
            "runtime_status": "idle",
            "error": None,
            "next_action": (
                "Bridge is registered. Add an MCP server under Settings → Plugins "
                "(Discover or Add Server) to expose tools."
            ),
            "mcp_servers": [],
            "server_count": 0,
        }

    return {
        "valid": True,
        "runtime_status": "ready",
        "error": None,
        "next_action": None,
        "mcp_servers": sorted(names),
        "server_count": len(names),
    }


def mcp_bridge_manifest() -> Dict[str, Any]:
    """
    A manifest for the bridge, so its tools are gated rather than refused.

    Its permissions are the union of what the configured servers declare, which
    is what lets a legitimate call past the registry-level gate. The check that
    matters is the per-call one in the bridge, which tests the *individual*
    server rather than the union — otherwise one server declaring
    ``shell:execute`` would lend it to every other.

    Also stamps ``valid`` / ``runtime_status`` / ``next_action`` so Settings →
    Plugins never renders the synthetic entry as INVALID/UNKNOWN by omission.
    """
    per_server = mcp_server_permissions()
    runtime = _mcp_bridge_runtime()
    # Prefer permission-map server names when present; fall back to runtime list.
    mcp_servers = sorted(per_server) or list(runtime.get("mcp_servers") or [])
    return {
        "id": MCP_BRIDGE_ID,
        "name": "MCP Bridge",
        "version": "1.0.0",
        "entrypoint": "internal",
        "trust_level": "standard",
        "permissions": sorted({p for perms in per_server.values() for p in perms}),
        "tools": [],
        "mcp_servers": mcp_servers,
        # Synthetic: no on-disk plugin.json. Validate must not require a path.
        "path": None,
        "synthetic": True,
        "valid": bool(runtime["valid"]),
        "runtime_status": runtime["runtime_status"],
        "error": runtime.get("error"),
        "next_action": runtime.get("next_action"),
        "server_count": runtime.get("server_count", len(mcp_servers)),
    }
