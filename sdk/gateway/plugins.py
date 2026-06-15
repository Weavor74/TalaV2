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
        return results
        
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
    return results
