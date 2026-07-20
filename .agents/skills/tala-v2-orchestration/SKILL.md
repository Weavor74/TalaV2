---
name: tala-v2-orchestration
description: Use when tasked with remote service orchestration, secure desktop operations, or analyzing/modifying the TalaV2 local AI engine (including P2P mesh and MCP integrations).
---

# TalaV2 Orchestration Skill

This skill provides the operational knowledge and imperative instructions required to orchestrate, analyze, or modify the local TalaV2 Cognitive Engine.

## Capabilities

1. **Local Orchestration Patterns**
   - TalaV2 separates its presentation layer (Electron/Node.js) from its cognitive backend (Python).
   - The primary API communication occurs via WebSockets (managed in `python/harness/server.py`).
   - Node graphs and agent workflows are executed natively through `workflow_executor.py`.

2. **P2P Mesh Network Protocols**
   - Secure remote desktop operations and peer service orchestration are routed through WebRTC DataChannels using `aiortc` (`python/harness/webrtc_manager.py`).
   - Standard JSON-RPC 2.0 payloads are multiplexed over the `tala-rpc` data channel.
   - Always ensure SDP signaling (offer/answer exchange) completes before attempting RPC dispatches.

3. **MCP Server Integrations**
   - External capabilities are integrated via the Model Context Protocol using `mcp_manager.py`.
   - Both `stdio` (local subprocess) and `sse` (remote HTTP) transports are supported via the official MCP Python SDK.
   - Use `mcp_manager.list_tools()` to discover available operations and `call_tool()` to invoke them.

## Operational Constraints

- **Directory Independence**: TalaV2 strictly isolates application code from user data. Do not hardcode absolute paths. Always respect dynamic environment variables: `TALA_INSTALL_ROOT` (for codebase) and `TALA_USER_DATA_ROOT` (for `.env`, `.venv`, `vault/`, and `data/`).
- **Dependency Sandboxing**: Backend Python dependencies are strictly managed by `uv`. Never use `pip install` directly; always utilize `uv sync` or `uv pip`.
- **System Elevation**: Installing or bootstrapping system dependencies (such as PostgreSQL) on Windows requires explicit Administrator privileges. Do not attempt silent Winget installations without verifying elevation.

## Tooling

When modifying or orchestrating TalaV2, execute the following tools and commands:

- **Validation**: Run the self-test lane using `uv run python scripts/tala_self_test.py` to continuously validate backend logic and dependency health.
- **Documentation**: Consult the Neural Node schema files in the `nodes/` directory (e.g., `nodes/system-*.html`) for system architecture references and semantic tags.
- **Backend Rebuilding**: If dependencies change, execute `uv sync --all-extras` to update the isolated virtual environment before restarting the backend harness.
