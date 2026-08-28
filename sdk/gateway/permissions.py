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

#: MCP tools are registered as ``mcp_<server>_<tool>`` so two servers can offer
#: a tool of the same name. The mapping below matches on what a tool *does*,
#: which it reads from the name -- so the prefix hid every one of them.
#:
#: ``mcp_files_write_file`` matched no rule and was permitted against a manifest
#: declaring nothing, while a bare ``write_file`` was correctly denied. The gate
#: was real and simply never applied to the one source of tools that arrives
#: from outside this codebase.
MCP_TOOL_PREFIX = "mcp_"


def underlying_tool_name(tool_name: str) -> str:
    """
    The tool being called, with any MCP server prefix removed.

    ``mcp_files_write_file`` -> ``write_file``. The server segment is dropped
    with the prefix: a permission is about the action, not about who is
    offering it.
    """
    name = str(tool_name or "")
    if not name.startswith(MCP_TOOL_PREFIX):
        return name
    rest = name[len(MCP_TOOL_PREFIX):]
    # mcp_<server>_<tool> -- the server name may itself contain underscores, so
    # the shortest split is not necessarily right. Every suffix is checked, and
    # the first that matches a known action wins; failing that the whole
    # remainder is used, which is the conservative answer because an unmatched
    # name is one no rule protects.
    parts = rest.split("_")
    for cut in range(1, len(parts)):
        candidate = "_".join(parts[cut:])
        if _required_permission(candidate):
            return candidate
    return rest


def _required_permission(tool_name: str) -> str:
    """Which permission a tool needs, or "" if the map does not cover it."""
    if tool_name.startswith("fs_") or tool_name in (
            "read_file", "write_file", "list_dir", "write_workspace_file",
            "read_workspace_file", "read_neural_node", "update_neural_node"):
        return "fs:write" if ("write" in tool_name or tool_name == "update_neural_node") else "fs:read"
    if tool_name.startswith("shell_") or tool_name in ("run_command", "run_workspace_command"):
        return "shell:execute"
    if tool_name.startswith("http_") or tool_name in (
            "fetch", "request", "read_url", "web_search", "send_telegram_message"):
        return "network:outbound"
    if tool_name.startswith("db_") or tool_name == "query":
        return "database:read"
    if tool_name.startswith("memory_"):
        return "memory:read"
    if tool_name.startswith("profile_"):
        return "profile:read"
    return ""


def enforce_tool_permissions(plugin_manifest: Dict[str, Any], tool_name: str, args: Dict[str, Any]):
    """
    Given a tool invocation, enforce that the plugin holds the necessary capability.

    The name is resolved through :func:`underlying_tool_name` first, so a tool
    reached over MCP is judged by what it does rather than by the prefix it
    arrives under.
    """
    tool_name = underlying_tool_name(tool_name)
    required = _required_permission(tool_name)
    if required:
        check_permission(plugin_manifest, required)
