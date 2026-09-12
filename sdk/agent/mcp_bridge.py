"""
sdk/agent/mcp_bridge.py
=======================
Register the tools of connected MCP servers into the ToolRegistry, so the model
can actually call them.

WHY THIS EXISTS
---------------
MCP was implemented and unusable. ``MCPManager`` is complete and correct --
``connect_server``, ``list_tools`` and ``call_tool`` all work, and the UI can
connect a server, list it, browse its tools and remove it. But nothing ever
called ``mcp.call_tool``, and no MCP tool was ever registered into the
ToolRegistry. The model never saw one. A server could be connected, its tools
displayed, and not a single one invoked.

Found by auditing all 193 RPC methods for who can reach them, after the same
shape turned up twice already on this branch: a cron scheduler that ran from
boot with no UI calling it, and swarm authoring the operator could do and the
agent could not. A working mechanism with no entry point.

HOW IT WORKS
------------
Registered under the ``qualified_name`` the manager already computes --
``{server}__{tool}`` -- rather than a new convention. Two servers exposing
``search`` stay distinct, and the name says where a tool came from, which
matters when a tool call misbehaves and the cause is somebody else's server.

The registered callable closes over the server and tool name and forwards to
``MCPManager.call_tool``. Nothing else in the dispatch path changes: selection
indexes them like any other tool, the native-tools schema builder serialises
their ``inputSchema`` as-is, and the ``<call:/>`` fallback works because the
name is a plain identifier.

Every tool of every connected server is registered. Connecting a server is
already the trust decision, and per-server enablement would add a config surface
with no UI behind it. Tool selection means an unused MCP tool costs nothing per
turn -- before selection existed, this would have been a worse trade.
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from sdk.agent.tools import ToolRegistry

log = logging.getLogger(__name__)

#: Marks registry entries this module owns, so a refresh can drop the tools of a
#: server that went away without touching hand-written ones.
MCP_PLUGIN_ID = "mcp-bridge"


def _normalise_schema(raw: Any) -> dict[str, Any]:
    """
    An MCP ``inputSchema`` as a JSON Schema the rest of the harness accepts.

    Ollama and the /v1 providers reject a parameters block that is not an object
    schema, and one malformed server would otherwise take down the whole tools
    array for every provider -- so anything unusable becomes an empty object
    rather than being passed through.
    """
    if not isinstance(raw, dict) or raw.get("type") != "object":
        return {"type": "object", "properties": {}, "required": []}
    return {
        "type": "object",
        "properties": raw.get("properties") or {},
        "required": raw.get("required") or [],
    }


def _make_caller(server_name: str, tool_name: str):
    """Build the callable registered for one MCP tool."""

    async def _call(**kwargs: Any) -> dict[str, Any]:
        from python.harness.mcp_manager import mcp_manager

        # The check that actually protects anything.
        #
        # ToolRegistry gates on the bridge's manifest, whose permissions are the
        # *union* of every configured server's. That is right for letting a
        # legitimate call through and wrong as the only check: one server
        # declaring shell:execute would lend it to every other server on the
        # bridge. So the individual server is checked here, against what it
        # alone asked for.
        #
        # A server that declares nothing gets nothing. An MCP server is somebody
        # else's process, and no declaration is not a request for everything.
        try:
            from sdk.gateway.permissions import (
                PermissionDeniedError,
                enforce_tool_permissions,
            )
            from sdk.gateway.plugins import mcp_server_permissions

            declared = mcp_server_permissions().get(server_name, [])
            enforce_tool_permissions(
                {"id": f"mcp:{server_name}", "permissions": declared},
                tool_name,
                kwargs,
            )
        except PermissionDeniedError as exc:
            log.warning("MCP tool %s__%s refused: %s", server_name, tool_name, exc)
            return {
                "success": False,
                "error": (
                    f"{exc}. The MCP server {server_name!r} has not declared "
                    f"this capability; add it to that server's permissions if "
                    f"you intend to grant it."
                ),
            }
        except Exception as exc:
            # A gate that cannot be consulted must not permit what it exists to
            # govern.
            log.error("Could not check MCP permissions for %s__%s: %s",
                      server_name, tool_name, exc)
            return {
                "success": False,
                "error": f"MCP permission check unavailable: {exc}",
            }

        try:
            return await mcp_manager.call_tool(server_name, tool_name, kwargs)
        except Exception as exc:
            # Returned rather than raised: a failing MCP server is a tool result
            # the model can read and react to, not a reason to fail the turn.
            log.warning("MCP tool %s__%s failed: %s", server_name, tool_name, exc)
            return {
                "success": False,
                "error": f"MCP tool {tool_name!r} on server {server_name!r} failed: {exc}",
            }

    _call.__name__ = f"mcp_{server_name}_{tool_name}"
    return _call


def unregister_mcp_tools() -> int:
    """Drop every tool this bridge registered. Returns how many were removed."""
    doomed = [
        key for key, entry in ToolRegistry._tools.items()
        if entry.get("plugin_id") == MCP_PLUGIN_ID
    ]
    for key in doomed:
        ToolRegistry._tools.pop(key, None)
    return len(doomed)


async def register_mcp_tools() -> dict[str, Any]:
    """
    Enumerate every connected MCP server and register its tools.

    Safe to call repeatedly: previously-registered MCP tools are dropped first,
    so a server that has been removed stops being offered rather than lingering
    as a name whose calls will fail.

    A server that cannot be reached is skipped with a warning and does not stop
    the others -- one unreachable server must not cost the model every other
    server's tools.
    """
    from python.harness.mcp_manager import mcp_manager

    removed = unregister_mcp_tools()
    registered: list[str] = []
    failed: dict[str, str] = {}

    server_names = list(mcp_manager.servers) or [None]
    seen_any = False

    for server_name in server_names:
        try:
            tools = await mcp_manager.list_tools(server_name)
        except Exception as exc:
            failed[server_name or "(all)"] = str(exc)
            log.warning("MCP server %r could not be listed: %s", server_name, exc)
            continue

        seen_any = True
        for tool in tools:
            name = tool.get("name")
            origin = tool.get("server_name") or server_name
            if not name or not origin:
                continue
            qualified = tool.get("qualified_name") or f"{origin}__{name}"

            existing = ToolRegistry._tools.get(qualified.lower())
            if existing and existing.get("plugin_id") != MCP_PLUGIN_ID:
                # Never shadow a hand-written tool. The qualified name makes
                # this near-impossible, but silently replacing a real tool with
                # a remote server's would be the worst failure this file could
                # produce.
                log.warning(
                    "MCP tool %r collides with an existing registry tool; skipped", qualified
                )
                continue

            description = (tool.get("description") or f"MCP tool {name} on server {origin}.").strip()
            ToolRegistry.register(
                name=qualified,
                description=f"[MCP:{origin}] {description}",
                schema=_normalise_schema(tool.get("inputSchema")),
                plugin_id=MCP_PLUGIN_ID,
            )(_make_caller(origin, name))
            registered.append(qualified)

    log.info(
        "MCP bridge: %d tool(s) registered from %d server(s)%s",
        len(registered), len(server_names) if seen_any else 0,
        f", {len(failed)} unreachable" if failed else "",
    )
    return {
        "success": True,
        "registered": sorted(registered),
        "count": len(registered),
        "replaced": removed,
        "unreachable": failed,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
#
# MCP servers are usually already configured somewhere on the machine, for
# whichever client introduced them. Rather than making someone retype a command
# and its arguments into Tala's settings, this reads the config files those
# clients keep and reports what it finds.
#
# Read-only and connect-nothing by design. Adopting a server means running a
# subprocess with the invoking user's privileges, which is a decision for a
# person, not a scan. There is deliberately no port probing either: sweeping
# localhost for SSE endpoints is intrusive, and a config file states intent
# where an open port only states that something is listening.

#: Where MCP clients keep their server definitions. Two layouts occur in the
#: wild and both are handled: ``mcpServers`` at the root, and Claude Code's
#: per-project ``projects.<path>.mcpServers``.
# ── where other tools keep their MCP configuration ───────────────────────────
#
# This was a single Windows table: five entries written with ``%APPDATA%`` /
# ``%USERPROFILE%`` and two with backslash separators, expanded through
# ``os.path.expandvars``. On POSIX that expands ``$VAR`` and ``${VAR}`` only —
# ``%APPDATA%`` passes through untouched and becomes a literal first path
# component, while backslashes are ordinary filename characters. So seven of the
# eight locations could never match on Linux or macOS (UI-097).
#
# The failure was invisible, which is the worse half: finding zero servers looks
# exactly like having zero servers installed, so "Scan for servers" appeared to
# work and always came back empty.
#
# Locations are expressed as path *fragments* against a named base, so pathlib
# joins them with the right separator and the per-OS differences live in one
# table. Claude Desktop and VS Code genuinely differ per platform; the rest sit
# under the home directory everywhere.

def _config_base() -> "Path":
    """
    Where this OS keeps per-user application configuration.

    Windows uses ``%APPDATA%``; macOS uses ``~/Library/Application Support``;
    everything else follows the XDG default of ``~/.config``. ``XDG_CONFIG_HOME``
    is honoured where it applies, since a user who sets it means it.
    """
    import os
    import sys
    from pathlib import Path

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


#: (label, base, *fragments). ``base`` is "config", "home" or "cwd".
_DISCOVERY_LOCATIONS: tuple[tuple, ...] = (
    ("Claude Desktop", "config", "Claude", "claude_desktop_config.json"),
    ("Claude Code", "home", ".claude.json"),
    ("Cursor (global)", "home", ".cursor", "mcp.json"),
    ("Windsurf", "home", ".codeium", "windsurf", "mcp_config.json"),
    ("VS Code", "config", "Code", "User", "settings.json"),
    ("project", "cwd", ".mcp.json"),
    ("project (VS Code)", "cwd", ".vscode", "mcp.json"),
    ("project (Cursor)", "cwd", ".cursor", "mcp.json"),
)


def _resolve_location(base: str, fragments: tuple) -> "Path":
    from pathlib import Path

    roots = {"config": _config_base, "home": Path.home, "cwd": Path.cwd}
    return roots[base]().joinpath(*fragments)


def _expand_supplied(raw: str) -> "Path":
    """
    A path a user typed, expanded whichever way they wrote it.

    ``os.path.expandvars`` understands ``%VAR%`` on Windows and ``$VAR`` on
    POSIX, never both — so a Windows-shaped path pasted on Linux, or the
    reverse, silently fails to resolve. Someone moving a portable install
    between machines will do exactly that.
    """
    import os
    import re
    from pathlib import Path

    text = str(raw or "")
    text = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%",
                  lambda m: os.environ.get(m.group(1), m.group(0)), text)
    text = os.path.expandvars(text)
    # A backslash is a separator on Windows and a filename character on POSIX.
    # A path written with backslashes is far more likely to be a Windows path
    # than a POSIX filename containing them.
    if os.sep == "/" and "\\" in text and "/" not in text:
        text = text.replace("\\", "/")
    return Path(text).expanduser()


def _collect_server_blocks(data: Any) -> list[tuple[str, dict]]:
    """Every ``{name: config}`` mapping of MCP servers inside one parsed file."""
    found: list[tuple[str, dict]] = []
    if not isinstance(data, dict):
        return found

    for key in ("mcpServers", "servers"):
        block = data.get(key)
        if isinstance(block, dict):
            found.extend((n, c) for n, c in block.items() if isinstance(c, dict))

    # VS Code nests under an "mcp" object.
    nested = data.get("mcp")
    if isinstance(nested, dict):
        found.extend(_collect_server_blocks(nested))

    # Claude Code keeps them per project.
    projects = data.get("projects")
    if isinstance(projects, dict):
        for project_config in projects.values():
            found.extend(_collect_server_blocks(project_config))

    return found


def discover_mcp_servers(extra_paths: list[str] | None = None) -> dict[str, Any]:
    """
    Find MCP servers configured elsewhere on this machine.

    Returns what was found and where, without connecting to anything. Servers
    already registered with Tala are marked, so the result distinguishes "new,
    could be adopted" from "already have it".
    """
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path

    from python.harness.mcp_manager import mcp_manager

    known = {name.lower() for name in mcp_manager.servers}
    discovered: list[dict[str, Any]] = []
    scanned: list[str] = []
    unreadable: dict[str, str] = {}

    candidates: list[tuple[str, "_Path"]] = []
    for label, base, *fragments in _DISCOVERY_LOCATIONS:
        try:
            candidates.append((label, _resolve_location(base, tuple(fragments))))
        except Exception as exc:
            # A base that cannot be resolved (no home directory, say) is worth
            # reporting rather than dropping: it is a location that was not
            # looked at, and the whole point here is that "nothing found" must
            # not hide "could not look".
            unreadable[f"{label} (unresolved)"] = str(exc)
    for extra in (extra_paths or []):
        candidates.append(("supplied", _expand_supplied(str(extra))))

    # Where discovery looked, whether or not anything was there. Finding zero
    # servers used to look identical to having zero servers installed, and on
    # Linux and macOS it was neither -- seven of eight paths could not match
    # (UI-097). A caller can now tell the two apart.
    looked_in: list[dict[str, Any]] = []

    for label, path in candidates:
        exists = path.is_file()
        looked_in.append({"source": label, "path": str(path), "exists": exists})
        if not exists:
            continue
        scanned.append(str(path))
        try:
            data = _json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            unreadable[str(path)] = str(exc)
            continue

        for name, config in _collect_server_blocks(data):
            # Summarised rather than passed through whole: these files can carry
            # API keys in an `env` block, and this result is shown in a UI and
            # may be handed to a model.
            transport = config.get("transport") or ("sse" if config.get("url") else "stdio")
            discovered.append({
                "name": name,
                "source": label,
                "path": str(path),
                "transport": transport,
                "command": config.get("command"),
                "args": config.get("args") or [],
                "url": config.get("url"),
                "has_env_secrets": bool(config.get("env")),
                "already_registered": name.lower() in known,
            })

    new = [d for d in discovered if not d["already_registered"]]
    log.info(
        "MCP discovery on %s: %d server(s) across %d of %d config location(s), "
        "%d not yet registered",
        _sys.platform, len(discovered), len(scanned), len(looked_in), len(new),
    )
    return {
        "success": True,
        "discovered": discovered,
        "new_count": len(new),
        "total_count": len(discovered),
        "files_scanned": scanned,
        "looked_in": looked_in,
        "platform": _sys.platform,
        "unreadable": unreadable,
    }
