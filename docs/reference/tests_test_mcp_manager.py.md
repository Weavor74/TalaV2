# File: `test_mcp_manager.py`

## Functions

### `def _write_fake_mcp_server(tmp_path)`
*No docstring provided.*

### `async def test_mcp_manager_calls_real_stdio_server(tmp_path)`
*No docstring provided.*

### `async def test_mcp_manager_persists_registered_servers(tmp_path)`
*No docstring provided.*

### `async def test_workflow_mcp_tool_uses_inline_real_server(tmp_path)`
*No docstring provided.*

## Source Code

```python
import sys
import textwrap
import json

import pytest

from python.harness.mcp_manager import MCPManager
from python.harness.workflow_executor import WorkflowExecutor


def _write_fake_mcp_server(tmp_path):
    server_path = tmp_path / "fake_mcp_server.py"
    server_path.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            PROTOCOL_VERSION = "2024-11-05"

            def send(payload):
                sys.stdout.write(json.dumps(payload) + "\\n")
                sys.stdout.flush()

            for line in sys.stdin:
                if not line.strip():
                    continue
                request = json.loads(line)
                method = request.get("method")
                request_id = request.get("id")

                if method == "notifications/initialized":
                    continue

                if method == "initialize":
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake-mcp", "version": "1.0.0"}
                        }
                    })
                elif method == "tools/list":
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "tools": [{
                                "name": "echo",
                                "description": "Echo a message",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"message": {"type": "string"}}
                                }
                            }]
                        }
                    })
                elif method == "tools/call":
                    message = request.get("params", {}).get("arguments", {}).get("message", "")
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": message}],
                            "isError": False
                        }
                    })
                else:
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": f"Unknown method: {method}"}
                    })
            """
        ),
        encoding="utf-8",
    )
    return server_path


@pytest.mark.asyncio
async def test_mcp_manager_calls_real_stdio_server(tmp_path):
    server_path = _write_fake_mcp_server(tmp_path)
    manager = MCPManager(registry_path=tmp_path / "mcp_servers.json")

    await manager.connect_server(
        "fake",
        {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(server_path)],
        },
    )

    tools = await manager.list_tools("fake")
    assert tools[0]["name"] == "echo"
    assert tools[0]["server_name"] == "fake"

    result = await manager.call_tool("fake", "echo", {"message": "mcp-ok"})
    assert result["content"][0]["text"] == "mcp-ok"
    assert result["isError"] is False


@pytest.mark.asyncio
async def test_mcp_manager_persists_registered_servers(tmp_path):
    server_path = _write_fake_mcp_server(tmp_path)
    registry_path = tmp_path / "mcp_servers.json"

    manager = MCPManager(registry_path=registry_path)
    await manager.connect_server(
        "persisted",
        {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(server_path)],
            "env": {"SAFE_VALUE": "ok", "API_KEY": "secret"},
        },
    )

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["servers"]["persisted"]["command"] == sys.executable

    reloaded = MCPManager(registry_path=registry_path)
    tools = await reloaded.list_tools("persisted")
    assert tools[0]["name"] == "echo"
    assert reloaded.list_servers()[0]["config"]["env"]["API_KEY"] == "***"

    assert reloaded.remove_server("persisted") is True
    after_remove = MCPManager(registry_path=registry_path)
    assert after_remove.list_servers() == []


@pytest.mark.asyncio
async def test_workflow_mcp_tool_uses_inline_real_server(tmp_path):
    server_path = _write_fake_mcp_server(tmp_path)
    workflow = {
        "flow_id": "mcp-smoke",
        "name": "MCP Smoke",
        "nodes": [
            {"id": "trigger", "type": "trigger", "connections": {"onSuccess": "tool"}},
            {
                "id": "tool",
                "type": "mcp_tool",
                "config": {
                    "server_name": "workflow_fake",
                    "server": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(server_path)],
                    },
                    "tool_name": "echo",
                    "args": {"message": "workflow-mcp-ok"},
                },
            },
        ],
    }

    result = await WorkflowExecutor(workflow).execute({})

    step = result["variables"]["step_tool"]
    assert result["status"] == "COMPLETED"
    assert step["success"] is True
    assert step["result"]["mcp_result"]["content"][0]["text"] == "workflow-mcp-ok"

```
