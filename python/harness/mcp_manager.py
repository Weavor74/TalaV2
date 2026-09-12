"""
mcp_manager.py - Auto-generated documentation base.
"""

from __future__ import annotations

import logging
import json
import shlex
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger("mcp_manager")


class MCPManager:
    """
    Real Model Context Protocol client manager.

    Servers are registered by name and opened on demand for each operation. Supported
    transports are stdio and SSE, matching the MCP Python SDK available in Tala's
    runtime.
    """

    def __init__(self, registry_path: str | Path | None = None) -> None:
        self.servers: dict[str, dict[str, Any]] = {}
        self.registry_path = Path(registry_path) if registry_path else self._default_registry_path()
        self._load_registry()

    async def connect_server(
        self,
        name: str,
        url: str | dict[str, Any] | None = None,
        persist: bool = True,
        **config: Any,
    ) -> bool:
        """
        Register an MCP server configuration.

        Examples
        --------
        await connect_server("fs", {"transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]})
        await connect_server("remote", "https://example.com/sse")
        """
        if not name:
            raise ValueError("MCP server name is required")

        server_config: dict[str, Any]
        if isinstance(url, dict):
            server_config = dict(url)
            server_config.update(config)
        else:
            server_config = dict(config)
            if url:
                server_config.setdefault("url", url)

        if "command" in server_config:
            server_config.setdefault("transport", "stdio")
        elif server_config.get("url"):
            server_config.setdefault("transport", "sse")
        else:
            raise ValueError("MCP server config requires either 'command' or 'url'")

        if "priority" not in server_config:
            server_config["priority"] = len(self.servers) + 1
        if "locked" not in server_config:
            server_config["locked"] = False

        self._validate_server_config(name, server_config)
        self.servers[name] = server_config
        if persist:
            await self._save_registry_async()
        logger.info("Registered MCP server %s via %s", name, server_config["transport"])
        return True

    def list_servers(self) -> list[dict[str, Any]]:
        def sort_key(item):
            name, config = item
            return (config.get("priority", 999), name)

        return [
            {
                "name": name,
                "config": self._redact_config(config),
                "priority": config.get("priority", 999),
                "locked": config.get("locked", False)
            }
            for name, config in sorted(self.servers.items(), key=sort_key)
        ]

    async def save_servers(self, servers_list: list[dict[str, Any]]) -> bool:
        """
        Save the entire MCP servers list (for reordering and setting priority/locked).
        """
        new_servers = {}
        for s in servers_list:
            name = s.get("name")
            config = s.get("config") or {}
            if name:
                existing = self.servers.get(name, {})
                for key, val in list(config.items()):
                    if val == "***":
                        config[key] = existing.get(key, "")
                    elif isinstance(val, dict):
                        existing_sub = existing.get(key, {})
                        for sub_k, sub_v in list(val.items()):
                            if sub_v == "***":
                                val[sub_k] = existing_sub.get(sub_k, "")
                config["priority"] = s.get("priority", 999)
                config["locked"] = s.get("locked", False)
                new_servers[name] = config
        self.servers = new_servers
        await self._save_registry_async()
        return True

    async def remove_server(self, name: str) -> bool:
        existed = name in self.servers
        if existed:
            self.servers.pop(name, None)
            await self._save_registry_async()
        return existed

    async def list_tools(self, server_name: str | None = None) -> list[dict[str, Any]]:
        """List tools from one registered MCP server, or all registered servers."""
        server_names = [server_name] if server_name else list(self.servers)
        tools: list[dict[str, Any]] = []

        # Always inject the sync_blockchain tool as a virtual MCP tool
        if not server_name or server_name == "blockchain":
            tools.append({
                "name": "sync_blockchain",
                "description": "Perform hash-integrity reconciliation between the local Ghost Blockchain and remote Main Blockchain.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                },
                "server_name": "blockchain",
                "qualified_name": "blockchain__sync_blockchain"
            })

        for name in server_names:
            if not name:
                continue
            config = self._get_server_config(name)
            async with self._session_for(config) as session:
                result = await session.list_tools()
                for tool in result.tools:
                    data = self._dump_model(tool)
                    data["server_name"] = name
                    data["qualified_name"] = f"{name}__{data.get('name')}"
                    tools.append(data)

        return tools

    async def call_tool(self, server_name: str, tool_name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool on a registered MCP server using the real MCP SDK."""
        if not tool_name:
            raise ValueError("MCP tool_name is required")

        if server_name == "astro-emotion" and tool_name == "get_emotional_state":
            # Virtual MCP tool. There is no out-of-process astro-emotion server
            # and there never was -- this call used to raise KeyError into
            # try/except blocks that only logged a warning, so Tala's emotional
            # state silently evaluated to nothing on every single turn.
            #
            # Routing it in-process means the emotion always resolves: no
            # registration to forget, no subprocess to crash, no round-trip on a
            # per-turn hot path, and nothing to break USB portability. The
            # MCP-shaped payload is preserved so a real server could take over
            # later without touching any call site.
            from python.harness.emotion_service import get_emotional_state
            logger.info("Calling virtual MCP tool: get_emotional_state")
            return await get_emotional_state(agent_id=(args or {}).get("agent_id"))

        if server_name == "blockchain" and tool_name == "sync_blockchain":
            logger.info("Calling virtual MCP tool: sync_blockchain")
            from sdk.agent.ghost_persistence import pre_flight_reconciliation
            success = await pre_flight_reconciliation()
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Blockchain reconciliation completed. Success: {success}"
                    }
                ],
                "isError": not success
            }

        config = self._get_server_config(server_name)
        logger.info("Calling MCP tool %s on %s", tool_name, server_name)

        async with self._session_for(config) as session:
            result = await session.call_tool(tool_name, args or {})
            return self._dump_model(result)

    def _get_server_config(self, server_name: str) -> dict[str, Any]:
        try:
            return self.servers[server_name]
        except KeyError as exc:
            raise KeyError(f"MCP server '{server_name}' is not registered") from exc

    def _validate_server_config(self, name: str, config: dict[str, Any]) -> None:
        transport = config.get("transport")
        if transport not in {"stdio", "sse"}:
            raise ValueError(f"MCP server '{name}' has unsupported transport: {transport!r}")

        if transport == "stdio" and not config.get("command"):
            raise ValueError(f"MCP stdio server '{name}' requires 'command'")
        if transport == "sse" and not config.get("url"):
            raise ValueError(f"MCP SSE server '{name}' requires 'url'")

    def _default_registry_path(self) -> Path:
        from sdk.agent.documentation_nodes import resolve_user_data_dir
        return resolve_user_data_dir() / "mcp_servers.json"

    def _load_registry(self) -> None:
        if not self.registry_path.exists():
            return

        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
            raw_servers = payload.get("servers", payload if isinstance(payload, dict) else {})
            if not isinstance(raw_servers, dict):
                raise ValueError("MCP registry 'servers' must be an object")

            for name, config in raw_servers.items():
                if not isinstance(config, dict):
                    logger.warning("Skipping MCP server %s: config must be an object", name)
                    continue
                try:
                    server_config = dict(config)
                    if "command" in server_config:
                        server_config.setdefault("transport", "stdio")
                    elif server_config.get("url"):
                        server_config.setdefault("transport", "sse")
                    self._validate_server_config(name, server_config)
                    self.servers[name] = server_config
                except Exception as exc:
                    logger.warning("Skipping invalid persisted MCP server %s: %s", name, exc)
        except Exception as exc:
            logger.warning("Failed to load MCP server registry %s: %s", self.registry_path, exc)

    def _save_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "servers": self.servers,
        }
        tmp_path = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(self.registry_path)

    async def _save_registry_async(self) -> None:
        """
        NAME
               _save_registry_async - Persist registered servers asynchronously
               
        SYNOPSIS
               await self._save_registry_async()
               
        DESCRIPTION
               Asynchronously saves the in-memory MCP servers configuration to the
               JSON registry file using asyncio.to_thread to prevent blocking the
               event loop.
        """
        import asyncio
        await asyncio.to_thread(self._save_registry)

    def _redact_config(self, config: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(config)
        for key in list(redacted):
            if any(token in key.lower() for token in ("token", "secret", "password", "apikey", "api_key", "authorization")):
                redacted[key] = "***"
        headers = redacted.get("headers")
        if isinstance(headers, dict):
            redacted["headers"] = {
                key: "***" if any(token in key.lower() for token in ("token", "secret", "password", "apikey", "api_key", "authorization")) else value
                for key, value in headers.items()
            }
        env = redacted.get("env")
        if isinstance(env, dict):
            redacted["env"] = {
                key: "***" if any(token in key.lower() for token in ("token", "secret", "password", "key")) else value
                for key, value in env.items()
            }
        return redacted

    @asynccontextmanager
    async def _session_for(self, config: dict[str, Any]) -> AsyncIterator[Any]:
        try:
            from mcp.client.session import ClientSession
        except Exception as exc:
            raise RuntimeError(
                "MCP Python SDK is not importable. Install the 'mcp' package and its runtime dependencies."
            ) from exc

        transport = config["transport"]
        if transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            command = str(config["command"])
            args = config.get("args", [])
            if isinstance(args, str):
                args = shlex.split(args)

            cwd = config.get("cwd")
            if cwd is not None:
                cwd = Path(cwd)

            params = StdioServerParameters(
                command=command,
                args=list(args),
                env=config.get("env"),
                cwd=cwd,
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session
            return

        if transport == "sse":
            from mcp.client.sse import sse_client

            async with sse_client(
                str(config["url"]),
                headers=config.get("headers"),
                timeout=float(config.get("timeout", 5)),
                sse_read_timeout=float(config.get("sse_read_timeout", 300)),
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session
            return

        raise ValueError(f"Unsupported MCP transport: {transport!r}")

    def _dump_model(self, value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(value, dict):
            return value
        return {"value": value}


mcp_manager = MCPManager()
