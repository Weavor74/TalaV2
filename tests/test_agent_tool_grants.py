"""
agent_tool_grants.sync_tool_grants -- keeps an agent's execute_tool grants
lined up with its config.tool_allowlist.

tool_scoping.py only decides which tools resolve_agent_tools offers a room
agent; AuthorizationGate's grant model is a separate check that still has to
say yes before ToolRegistry.run_tool executes anything. Before this module
existed, that grant was a manual access.grant() call (how this feature's own
live verification was done) -- these tests cover the sync taking its place:
new allowlist entries get granted, dropped ones get revoked, and entries
that are already granted are left alone.
"""
from __future__ import annotations

import pytest

from python.harness.access import AGENT, Principal
from sdk.agent.agent_tool_grants import sync_tool_grants


def _fake_access(monkeypatch, existing_grants):
    from python.harness import access

    granted, revoked = [], []

    async def fake_list_grants(principal):
        return existing_grants

    async def fake_grant(principal, resource, permission="read", **kw):
        granted.append((principal, resource, permission))
        return {}

    async def fake_revoke(principal, resource, permission=""):
        revoked.append((principal, resource, permission))
        return {}

    monkeypatch.setattr(access, "list_grants", fake_list_grants)
    monkeypatch.setattr(access, "grant", fake_grant)
    monkeypatch.setattr(access, "revoke", fake_revoke)
    return granted, revoked


@pytest.mark.asyncio
async def test_a_new_allowlist_grants_each_tool(monkeypatch):
    granted, revoked = _fake_access(monkeypatch, existing_grants=[])

    await sync_tool_grants("agent-1", ["get_system_info", "web_search"])

    resources = {r for (_, r, p) in granted}
    assert resources == {"get_system_info", "web_search"}
    assert all(p == "execute_tool" for (_, _, p) in granted)
    assert all(pr == Principal(AGENT, "agent-1") for (pr, _, _) in granted)
    assert revoked == []


@pytest.mark.asyncio
async def test_a_tool_dropped_from_the_allowlist_is_revoked(monkeypatch):
    existing = [{"resource": "get_system_info", "permission": "execute_tool"},
                {"resource": "web_search", "permission": "execute_tool"}]
    granted, revoked = _fake_access(monkeypatch, existing)

    await sync_tool_grants("agent-1", ["get_system_info"])

    assert granted == []
    assert revoked == [(Principal(AGENT, "agent-1"), "web_search", "execute_tool")]


@pytest.mark.asyncio
async def test_an_already_granted_tool_is_left_alone(monkeypatch):
    existing = [{"resource": "get_system_info", "permission": "execute_tool"}]
    granted, revoked = _fake_access(monkeypatch, existing)

    await sync_tool_grants("agent-1", ["get_system_info"])

    assert granted == []
    assert revoked == []


@pytest.mark.asyncio
async def test_an_empty_allowlist_revokes_everything(monkeypatch):
    existing = [{"resource": "get_system_info", "permission": "execute_tool"}]
    granted, revoked = _fake_access(monkeypatch, existing)

    await sync_tool_grants("agent-1", [])

    assert granted == []
    assert revoked == [(Principal(AGENT, "agent-1"), "get_system_info", "execute_tool")]


@pytest.mark.asyncio
async def test_a_non_execute_tool_grant_is_left_untouched(monkeypatch):
    """sync_tool_grants owns execute_tool grants only -- a grant for some
    other permission on the same agent must not be revoked as a side effect."""
    existing = [{"resource": "some_resource", "permission": "read"}]
    granted, revoked = _fake_access(monkeypatch, existing)

    await sync_tool_grants("agent-1", [])

    assert granted == []
    assert revoked == []
