"""
tool_scoping.py — per-agent tool allowlists, enforced at ToolRegistry.run_tool.

Phase 1 of the multi-agent feature (build named agents, each with their own
tools, that can talk to each other): this is the enforcement mechanism only.
Nothing currently *sets* a tool allowlist on any agent --
sdk/agent/agent_authoring.py's create_agent deliberately grants "no tools, no
model, no execution authority" (its own docstring, enforced by
tests/test_agent_authoring.py::test_create_agent_cannot_grant_authority), and
that is unchanged here. A future authoring tool/admin RPC would be what
actually writes agents.config['tool_allowlist']; this module is what would
respect it once something does.

Why this is safe to ship alone: every current caller of ToolRegistry.run_tool
passes a placeholder agent-principal id ("default", "nobody", ...) that
matches no real `agents` row (see python.harness.access.Principal --
Principal(AGENT, "default") is the standard pattern throughout the
codebase). A lookup miss is not an error state, it is today's normal case,
so it falls through to "no restriction" -- the same code path handles both
old and new usage with zero special-casing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


async def check_agent_tool_scope(principal: Any, tool_name: str) -> dict[str, Any] | None:
    """
    None if the call may proceed; a refusal dict if `principal`'s agent
    record has a tool_allowlist that does not include `tool_name`.
    """
    from python.harness.access import AGENT

    if principal is None or getattr(principal, "kind", None) != AGENT or not principal.id:
        return None

    from python.harness import db

    agent_row = None
    try:
        agent_row = await db.fetchrow("SELECT config FROM agents WHERE id = $1", principal.id)
    except Exception as exc:
        # DB trouble must not block every agent-principal tool call --
        # AuthorizationGate (the caller's next step) still applies its own
        # fail-closed checks regardless. This fails open specifically
        # because an unscoped agent principal is the normal case, not an
        # error state.
        log.debug("tool_scoping: could not look up agent %r: %s", principal.id, exc)
        return None

    if not agent_row:
        return None

    cfg = agent_row["config"]
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            cfg = {}
    if not isinstance(cfg, dict):
        # Malformed config must not crash a tool call -- same fail-open
        # reasoning as the DB lookup above.
        return None
    allowlist = cfg.get("tool_allowlist")
    if isinstance(allowlist, list) and tool_name.lower() not in {str(t).lower() for t in allowlist}:
        return {
            "success": False,
            "tool_scope_denied": True,
            "error": f"Agent {principal.id!r} is not permitted to use tool {tool_name!r}.",
        }
    return None
