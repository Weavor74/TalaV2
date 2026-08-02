"""
python/harness/mcp_server.py
==============================
Exposes Tala's own capabilities as a real MCP server, so any MCP-aware
client (Claude, another Tala instance, a third-party tool) can point at
this process and discover -- and, with the harness admin secret, invoke
-- everything Tala can do.

Complements python/harness/mcp_manager.py, which is the *client* side
(Tala connecting out to other MCP servers). This module is the server
side (other things connecting in to Tala).

Uses the low-level mcp.server.lowlevel.Server rather than FastMCP's
decorator sugar: Tala's ~180 RPC methods take an untyped dict[str, Any]
of params, and agent tools each carry their own already-registered JSON
schema (sdk.agent.tools.ToolRegistry) -- neither fits FastMCP's
signature-inferred schema generation, but both fit naturally into
manually-constructed mcp.types.Tool objects.

Security posture:
  - Binds 127.0.0.1 by default (MCP_HOST / MCP_PORT in config.py),
    matching the main WebSocket RPC server's default -- not exposed
    beyond this machine unless the operator deliberately does so.
  - tools/list (discovery) is unauthenticated and read-only.
  - tools/call (execution) requires a "harness_secret" argument that
    must match vault/.keys/harness_admin_secret.txt (see admin_auth.py).
    Unlike the WS RPC port, reachability alone does not imply the caller
    is the trusted local Electron process, so every call is gated, not
    just the admin-tier ones.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport

from python.harness.admin_auth import require_harness_admin_secret

logger = logging.getLogger(__name__)

_server: Server = Server("tala")


@_server.list_tools()
async def _list_tools() -> list[types.Tool]:
    from python.harness.capabilities import get_capability_manifest

    manifest = get_capability_manifest()
    tools: list[types.Tool] = []

    for entry in manifest["rpc_methods"]:
        tools.append(types.Tool(
            name=entry["method"],
            description=entry["description"] or entry["method"],
            inputSchema={"type": "object", "additionalProperties": True},
        ))

    for entry in manifest["agent_tools"]:
        schema = entry.get("schema") or {"type": "object", "additionalProperties": True}
        tools.append(types.Tool(
            name=entry["name"],
            description=entry.get("description") or entry["name"],
            inputSchema=schema,
        ))

    return tools


@_server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    arguments = dict(arguments or {})
    provided_secret = arguments.pop("harness_secret", "")
    if not require_harness_admin_secret({"harness_secret": provided_secret}):
        raise PermissionError(
            "Unauthorized: this call requires a valid 'harness_secret' argument "
            "matching the local harness admin secret."
        )

    from python.harness.server import _HANDLERS

    if name in _HANDLERS:
        handler = _HANDLERS[name]
        raw = await handler(arguments, "mcp")
        envelope = json.loads(raw)
        if "error" in envelope:
            raise RuntimeError(json.dumps(envelope["error"]))
        return [types.TextContent(type="text", text=json.dumps(envelope.get("result"), default=str))]

    from sdk.agent.tools import ToolRegistry

    result = await ToolRegistry.run_tool(name, arguments)
    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(result.get("error", f"Tool {name!r} failed."))
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


async def start_mcp_server() -> None:
    """Bind and serve the MCP SSE endpoint. Runs until cancelled."""
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    import uvicorn

    from python.harness.config import settings

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await _server.run(streams[0], streams[1], _server.create_initialization_options())

    app = Starlette(routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ])

    config = uvicorn.Config(app, host=settings.MCP_HOST, port=settings.MCP_PORT, log_level="warning")
    uv_server = uvicorn.Server(config)
    logger.info("MCP server listening on http://%s:%s/sse", settings.MCP_HOST, settings.MCP_PORT)
    await uv_server.serve()
