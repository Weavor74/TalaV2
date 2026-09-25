"""
agent_tool_grants.py — keeps an agent's execute_tool grants in sync with its
config.tool_allowlist.

Two separate checks gate a room agent's tool call: tool_scoping.py's
allowlist (which tools resolve_agent_tools even offers the model) and
AuthorizationGate's grant model (sdk/agent/tools.py's run_tool, backed by
python.harness.access) -- being offered a tool is not the same as being
allowed to run it. Before this module, the grant half was a manual
access.grant() call (how this feature's own live verification was done).
This is what the agent editor's save calls instead, so an agent configured
through the UI works end to end without a manual DB/grant step.

Trusts the allowlist's spelling exactly as given -- it originates from
tools.catalogue's own tool names (the picker builds its checkboxes from
that RPC), the same names ToolRegistry.run_tool passes as `resource`, and
access.check()'s grant lookup is an exact string match.
"""
from __future__ import annotations


async def sync_tool_grants(agent_id: str, tool_allowlist: list[str]) -> None:
    """Grant execute_tool for every name newly added to the allowlist, and
    revoke it for every name no longer there. Idempotent either way."""
    from python.harness.access import AGENT, Principal, grant, list_grants, revoke

    principal = Principal(AGENT, agent_id)
    wanted = {str(name).strip() for name in tool_allowlist if str(name).strip()}

    current = await list_grants(principal)
    held = {row["resource"] for row in current if row["permission"] == "execute_tool"}

    for tool_name in wanted - held:
        await grant(principal, tool_name, permission="execute_tool",
                    granted_by="owner", note="agent editor: tool_allowlist")

    for tool_name in held - wanted:
        await revoke(principal, tool_name, permission="execute_tool")
