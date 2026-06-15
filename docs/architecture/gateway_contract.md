# Tala Gateway Contract

The Gateway is the unified entrypoint and security boundary for all Tala cognitive and operational workflows. It treats all internal Core operations as trusted, while enforcing strict boundaries on UI clients, Plugins, and MCP tool servers.

## 1. Endpoints & API Topology
- **Core RPC**: Serves JSON-RPC 2.0 requests over WebSocket.
- **Surface Area**: Exposes limited domains: `auth.*`, `inference.*`, `db.*`, `swarm.*`, `task.*`, `plugins.*`, `gateway.*`.
- **Isolation**: Gateway API handles parameter validation and dispatch. It does not contain domain business logic.

## 2. Trust Boundaries
- **UI Client**: Untrusted until a valid session is established.
- **Core Agents**: Trusted (runs in the primary Python process context).
- **Plugins / MCP Servers**: **Untrusted**. All operations executed by plugins must be declared in a strict `plugin.json` manifest and gated centrally by the Gateway.

## 3. Auth Mode
- **Local Loopback Mode**: By default, only connections from `127.0.0.1` or `localhost` are accepted. The UI bypasses session checks if loopback is validated.
- **LAN / Token Mode**: If remote access is opted-in via configuration, all connections require a Bearer token or signed session key. Expired sessions gracefully degrade UI capabilities.

## 4. Process Ownership
- **Orchestration**: The Gateway is the parent process for all node/python/binary scripts invoked as MCP servers or local plugins.
- **Tracking**: A central `sdk/gateway/lifecycle.py` registry maps PIDs to active tools/plugins.
- **Process Fate Sharing**: If the Gateway process terminates, all registered child processes are forcibly terminated.

## 5. Failure Behavior
- **Hard Disconnects**: If database connectivity or search backend falls offline, the Gateway continues serving the WebSocket connection but reports subsystem failure states via `gateway.status`.
- **Plugin Crashes**: A crashed MCP server drops into a `failed` state and stops advertising its tools. The Gateway does NOT infinitely retry crashing plugins.
- **Command Resolution**: Bare commands (`npx`, `python`) that fail to resolve immediately return JSON-RPC errors instead of triggering silent OS-level `FileNotFound` subprocess crashes.
