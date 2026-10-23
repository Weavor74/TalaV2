"""
sdk/agent/learning_tools.py -- the admin action that undoes node learning.

``reset_node_weights`` is registered in ``sdk.agent.tools`` so it runs through ``ToolRegistry.run_tool``: the
AuthorizationGate, the lease policy (it is a file-edit tool) and the risk classifier all apply, exactly as they do for
every other tool that rewrites node files. It needs an explicit ``confirm`` because it discards what has been learned.
"""
from __future__ import annotations

from typing import Any


async def reset_node_weights(node_ids: list[str] | None = None, confirm: bool = False) -> dict[str, Any]:
    """
    NAME
           reset_node_weights - forget what node learning has done

    SYNOPSIS
           reset_node_weights(node_ids: list[str] | None = None, confirm: bool = False) -> dict[str, Any]

    DESCRIPTION
           Admin action. Puts the named nodes (or, with no ids, every node learning has touched) back to the schema
           default weights and removes the usage attributes. Protected nodes are never changed. Refuses unless
           ``confirm`` is true. Each reset is logged and anchored like any other weight change.
    """
    from sdk.agent import node_learning as nl
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    if confirm is not True:
        return {"success": False, "error": "reset_node_weights discards learned weights; pass confirm=true to proceed"}
    if nl._halted():
        return {"success": False, "error": "refused: the emergency halt is engaged"}
    changes = nl.reset_weights(resolve_active_nodes_dir(), node_ids)
    return {"success": True, "nodes_reset": len({c["node_id"] for c in changes}), "changes": len(changes)}
