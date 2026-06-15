"""
sdk/gateway/permissions.py
==========================
Centralized permission enforcement for Plugins and MCP servers.
"""
import logging
from typing import Dict, Any, List

log = logging.getLogger(__name__)

class PermissionDeniedError(Exception):
    pass

def check_permission(plugin_manifest: Dict[str, Any], required_permission: str):
    """
    Check if a loaded plugin has the required permission.
    Raises PermissionDeniedError if not.
    """
    permissions = plugin_manifest.get("permissions", [])
    if required_permission not in permissions:
        plugin_id = plugin_manifest.get("id", "unknown")
        log.warning(f"Permission denied: Plugin '{plugin_id}' attempted to use '{required_permission}' without declaring it.")
        raise PermissionDeniedError(f"Plugin '{plugin_id}' lacks required permission: '{required_permission}'")

def enforce_tool_permissions(plugin_manifest: Dict[str, Any], tool_name: str, args: Dict[str, Any]):
    """
    Given a tool invocation, enforce that the plugin holds the necessary capability.
    """
    if tool_name.startswith("fs_") or tool_name in ("read_file", "write_file", "list_dir", "write_workspace_file", "read_neural_node", "update_neural_node"):
        if "write" in tool_name or tool_name == "update_neural_node":
            check_permission(plugin_manifest, "fs:write")
        else:
            check_permission(plugin_manifest, "fs:read")
            
    elif tool_name.startswith("shell_") or tool_name in ("run_command", "run_workspace_command"):
        check_permission(plugin_manifest, "shell:execute")
        
    elif tool_name.startswith("http_") or tool_name in ("fetch", "request", "read_url", "web_search", "send_telegram_message"):
        check_permission(plugin_manifest, "network:outbound")
        
    elif tool_name.startswith("db_") or tool_name == "query":
        check_permission(plugin_manifest, "database:read")
        
    elif tool_name.startswith("memory_"):
        check_permission(plugin_manifest, "memory:read")
        
    elif tool_name.startswith("profile_"):
        check_permission(plugin_manifest, "profile:read")
