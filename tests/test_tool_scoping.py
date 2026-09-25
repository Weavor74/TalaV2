"""
Per-agent tool scoping -- Phase 1 of the multi-agent feature.

ToolRegistry.run_tool is the chokepoint every tool call takes (see
test_authorization_gate_is_wired.py). check_agent_tool_scope() is the first
check in that chokepoint: an agent principal whose `agents` row carries a
tool_allowlist is refused before any heavier gate machinery runs. Every
current caller passes a placeholder id ("default", "nobody", ...) that
matches no real row, and nothing sets tool_allowlist yet -- these tests
assert both that behavior is unchanged for that existing usage, and that the
mechanism works once something does set an allowlist.
"""
from __future__ import annotations

import inspect
import json

import pytest

from python.harness.access import AGENT, OWNER, Principal
from sdk.agent.tool_scoping import check_agent_tool_scope
from sdk.agent.tools import ToolRegistry


def test_run_tool_calls_the_scoping_check():
    """Assert the wiring, not just the logic -- a check nothing calls is the
    exact shape of defect this codebase keeps producing."""
    src = inspect.getsource(ToolRegistry.run_tool)
    assert "check_agent_tool_scope" in src


@pytest.mark.asyncio
async def test_owner_principal_is_never_scoped():
    assert await check_agent_tool_scope(Principal(OWNER), "anything") is None


@pytest.mark.asyncio
async def test_no_principal_is_never_scoped():
    assert await check_agent_tool_scope(None, "anything") is None


@pytest.mark.asyncio
async def test_placeholder_agent_ids_with_no_matching_row_are_unrestricted(monkeypatch):
    """Today's universal usage: Principal(AGENT, "default") everywhere in the
    codebase matches no real `agents` row."""
    from python.harness import db

    async def no_row(*a, **k):
        return None

    monkeypatch.setattr(db, "fetchrow", no_row)

    result = await check_agent_tool_scope(Principal(AGENT, "default"), "web_search")

    assert result is None


@pytest.mark.asyncio
async def test_a_real_agent_with_no_allowlist_is_unrestricted(monkeypatch):
    """Every real agent row today has no tool_allowlist key."""
    from python.harness import db

    async def row(*a, **k):
        return {"config": json.dumps({"system_prompt": "You are Tala."})}

    monkeypatch.setattr(db, "fetchrow", row)

    result = await check_agent_tool_scope(Principal(AGENT, "tala-core"), "web_search")

    assert result is None


@pytest.mark.asyncio
async def test_a_tool_in_the_allowlist_is_permitted(monkeypatch):
    from python.harness import db

    async def row(*a, **k):
        return {"config": json.dumps({"tool_allowlist": ["get_system_info", "web_search"]})}

    monkeypatch.setattr(db, "fetchrow", row)

    result = await check_agent_tool_scope(Principal(AGENT, "scoped-agent"), "web_search")

    assert result is None


@pytest.mark.asyncio
async def test_a_tool_not_in_the_allowlist_is_denied(monkeypatch):
    from python.harness import db

    async def row(*a, **k):
        return {"config": json.dumps({"tool_allowlist": ["get_system_info"]})}

    monkeypatch.setattr(db, "fetchrow", row)

    result = await check_agent_tool_scope(Principal(AGENT, "scoped-agent"), "vault_read")

    assert result == {
        "success": False,
        "tool_scope_denied": True,
        "error": "Agent 'scoped-agent' is not permitted to use tool 'vault_read'.",
    }


@pytest.mark.asyncio
async def test_allowlist_matching_is_case_insensitive(monkeypatch):
    from python.harness import db

    async def row(*a, **k):
        return {"config": json.dumps({"tool_allowlist": ["Web_Search"]})}

    monkeypatch.setattr(db, "fetchrow", row)

    result = await check_agent_tool_scope(Principal(AGENT, "scoped-agent"), "web_search")

    assert result is None


@pytest.mark.asyncio
async def test_a_malformed_config_fails_open(monkeypatch):
    """Confirmed live: a config value that is not a dict (found via a
    corrupted jsonb `||` update during manual testing -- config had become a
    2-element list) must not crash the call with an AttributeError."""
    from python.harness import db

    async def row(*a, **k):
        return {"config": ["not", "a", "dict"]}

    monkeypatch.setattr(db, "fetchrow", row)

    result = await check_agent_tool_scope(Principal(AGENT, "scoped-agent"), "anything")

    assert result is None


@pytest.mark.asyncio
async def test_a_db_lookup_failure_fails_open(monkeypatch):
    """DB trouble must not block every agent-principal tool call -- the
    AuthorizationGate that runs right after this still applies its own
    fail-closed checks regardless."""
    from python.harness import db

    async def boom(*a, **k):
        raise RuntimeError("connection pool exhausted")

    monkeypatch.setattr(db, "fetchrow", boom)

    result = await check_agent_tool_scope(Principal(AGENT, "scoped-agent"), "anything")

    assert result is None
