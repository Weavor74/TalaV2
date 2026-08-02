"""
python/harness/capabilities.py
================================
Live capability introspection for Tala's RPC surface and agent tools.

Replaces hand-written, drift-prone capability docs with a single source
that reads the actual registries at call time:
  - python.harness.server._HANDLERS  (the WebSocket JSON-RPC method table)
  - sdk.agent.tools.ToolRegistry     (dynamically discovered agent tools)

Nothing here is cached -- get_capability_manifest() re-reads both
registries on every call, so it can never go stale the way a
hand-written reference document can.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any


def get_capability_manifest() -> dict[str, Any]:
    """Build a fresh snapshot of everything Tala can currently do."""
    from python.harness.server import _HANDLERS
    from sdk.agent.tools import ToolRegistry

    rpc_methods: list[dict[str, Any]] = []
    categories: dict[str, list[str]] = {}

    for method, handler_fn in _HANDLERS.items():
        category = method.split(".", 1)[0]
        rpc_methods.append({
            "method": method,
            "category": category,
            "description": inspect.getdoc(handler_fn) or "",
            "source": f"{handler_fn.__module__}.{handler_fn.__qualname__}",
        })
        categories.setdefault(category, []).append(method)

    rpc_methods.sort(key=lambda m: m["method"])
    for methods in categories.values():
        methods.sort()

    agent_tools = ToolRegistry.get_tools()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rpc_methods": rpc_methods,
        "agent_tools": agent_tools,
        "categories": {k: categories[k] for k in sorted(categories)},
    }
