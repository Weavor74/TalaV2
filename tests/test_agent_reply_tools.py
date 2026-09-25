"""
Tool-calling for room-agent replies.

`_agents_take_a_turn` has always generated a reply with `_generate_reply` --
a plain-text-only completion helper -- so a room agent has never had tool
access, regardless of anything in its config. These tests cover the new
pieces that change that: `_native_specs_for_allowlist` (the tool catalog to
offer) and `run_agent_tool_round` (detect and execute one tool call), plus
the safety boundary that keeps federation-triggered replies tool-less no
matter what.
"""
from __future__ import annotations

import json

import pytest

from python.harness.access import AGENT, Principal
from sdk.agent.agent_reply_tools import _native_specs_for_allowlist, run_agent_tool_round


def test_native_specs_for_allowlist_filters_to_named_tools(monkeypatch):
    from sdk.agent.tools import ToolRegistry

    async def fake_tool():
        return {}

    monkeypatch.setattr(ToolRegistry, "get_tools", lambda: [
        {"name": "get_system_info", "description": "d1", "schema": {"type": "object", "properties": {}, "required": []}},
        {"name": "web_search", "description": "d2", "schema": {"type": "object", "properties": {}, "required": []}},
        {"name": "vault_read", "description": "d3", "schema": {"type": "object", "properties": {}, "required": []}},
    ])

    specs = _native_specs_for_allowlist(["get_system_info", "web_search"])

    names = {s["function"]["name"] for s in specs}
    assert names == {"get_system_info", "web_search"}
    assert all(s["type"] == "function" for s in specs)


def test_native_specs_for_empty_allowlist_is_empty(monkeypatch):
    from sdk.agent.tools import ToolRegistry

    monkeypatch.setattr(ToolRegistry, "get_tools", lambda: [
        {"name": "get_system_info", "description": "d", "schema": {}},
    ])

    assert _native_specs_for_allowlist([]) == []


TOOLS = [{"type": "function", "function": {"name": "get_system_info", "description": "", "parameters": {}}}]


@pytest.mark.asyncio
async def test_no_tool_call_in_text_returns_none():
    result = await run_agent_tool_round("Just a normal reply, no tags here.", TOOLS, Principal(AGENT, "a1"))
    assert result is None


@pytest.mark.asyncio
async def test_an_allowed_tool_call_is_executed(monkeypatch):
    from sdk.agent.tools import ToolRegistry

    captured = {}

    async def fake_run_tool(name, attrs, principal=None, **kw):
        captured["name"] = name
        captured["attrs"] = attrs
        captured["principal"] = principal
        return {"success": True, "output": {"ok": True}}

    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    principal = Principal(AGENT, "agent-123")
    result = await run_agent_tool_round('<call:get_system_info />', TOOLS, principal)

    assert result is not None
    assert "get_system_info" in result
    assert captured["name"] == "get_system_info"
    assert captured["principal"] is principal


@pytest.mark.asyncio
async def test_a_tool_call_not_in_the_offered_set_is_refused_locally(monkeypatch):
    """Belt-and-suspenders: this check runs before ToolRegistry.run_tool is
    even called, on top of tool_scoping.py's own enforcement inside it."""
    from sdk.agent.tools import ToolRegistry

    called = False

    async def fake_run_tool(*a, **k):
        nonlocal called
        called = True
        return {"success": True, "output": "should not happen"}

    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    result = await run_agent_tool_round('<call:vault_read doc_id="x" />', TOOLS, Principal(AGENT, "agent-123"))

    assert result is not None
    assert "not offered" in result
    assert called is False


@pytest.mark.asyncio
async def test_a_failed_tool_execution_is_reported_not_raised(monkeypatch):
    from sdk.agent.tools import ToolRegistry

    async def fake_run_tool(*a, **k):
        return {"success": False, "error": "boom"}

    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    result = await run_agent_tool_round('<call:get_system_info />', TOOLS, Principal(AGENT, "agent-123"))

    assert result is not None
    assert "boom" in result


def test_handle_inbound_a2a_never_passes_tools_or_principal():
    """Locks in the federation safety boundary: a remote peer's message must
    never be able to reach local tool execution through _generate_reply."""
    import inspect
    from python.harness import server

    src = inspect.getsource(server._handle_inbound_a2a)
    assert "tools=" not in src
    assert "principal=" not in src
