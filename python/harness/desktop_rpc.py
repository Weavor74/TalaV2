"""
python/harness/desktop_rpc.py -- the desktop loop, reachable without a turn.

Two JSON-RPC handlers and the prompt line that tells a model to prefer them,
moved out of server.py because the size ratchet (tests/test_size_ratchet.py)
pins that file and because they are a self-contained unit: starting and
stopping native desktop work over the authenticated harness channel.

The sdk.agent imports stay inside the functions. This module is imported at
server import time, and sdk.agent.tools pulls the harness back in.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


DESKTOP_RUN_PROMPT = (
    "\nFor native computer-use tasks, prefer desktop_run with the user's task. "
    "It runs its own local Ollama vision/action loop; do not manually manage "
    "observation IDs. When the user specifies exact final confirmation text, "
    "pass it as success_text. Report its success or failure and evidence "
    "accurately. Never turn a failed desktop run into a completion claim."
)


_desktop_rpc_tasks: set[asyncio.Task] = set()


async def handle_desktop_stop(params: dict[str, Any], req_id: Any) -> str:
    """NAME
           desktop.stop - stop native desktop work without an inference turn
    DESCRIPTION
           Available through the authenticated harness RPC channel.
    """
    from sdk.agent.desktop_agent import desktop_stop
    result = await desktop_stop()
    for task in tuple(_desktop_rpc_tasks):
        task.cancel()
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})


async def handle_desktop_run(params: dict[str, Any], req_id: Any) -> str:
    """NAME
           desktop.run - invoke the governed local desktop loop from chat
    DESCRIPTION
           Keeps direct chat commands under the ordinary tool authority gates.
    """
    from sdk.agent.tools import ToolRegistry
    task = asyncio.current_task()
    _desktop_rpc_tasks.add(task)
    try:
        result = await ToolRegistry.run_tool("desktop_run", params, authority_level="auto")
    except asyncio.CancelledError:
        result = {"success": False, "error": "Desktop run stopped", "output": {"success": False, "status": "stopped", "error": "Desktop run stopped"}}
    finally:
        _desktop_rpc_tasks.discard(task)
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})
