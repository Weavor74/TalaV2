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


import pytest

from python.harness.access import AGENT, Principal
from sdk.agent.agent_reply_tools import (
    _CALLING_PRINCIPAL,
    _native_specs_for_allowlist,
    run_agent_tool_round,
    send_to_agent,
)


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


# -- send_to_agent (Phase 3: agent-to-agent delegation) ----------------------

@pytest.mark.asyncio
async def test_send_to_agent_refuses_an_unknown_target(monkeypatch):
    from python.harness import db

    async def fake_fetchrow(*a, **k):
        return None

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)

    result = await send_to_agent(target_agent="no-such-agent", message="hi")

    assert result["success"] is False
    assert "no-such-agent" in result["error"]


@pytest.mark.asyncio
async def test_send_to_agent_refuses_a_self_target(monkeypatch):
    from python.harness import db

    async def fake_fetchrow(*a, **k):
        return {"id": "agent-a"}

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)

    token = _CALLING_PRINCIPAL.set(Principal(AGENT, "agent-a"))
    try:
        result = await send_to_agent(target_agent="agent-a", message="hi")
    finally:
        _CALLING_PRINCIPAL.reset(token)

    assert result["success"] is False
    assert "itself" in result["error"]


@pytest.mark.asyncio
async def test_send_to_agent_calls_the_target_tool_free_and_returns_its_reply(monkeypatch):
    from python.harness import db, server

    async def fake_fetchrow(*a, **k):
        return {"id": "agent-b"}

    async def fake_system_prompt(agent_id):
        assert agent_id == "agent-b"
        return "You are Agent B, a specialist."

    captured = {}

    async def fake_generate_reply(messages, *args, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "Agent B's answer."

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(server, "_agent_system_prompt", fake_system_prompt)
    monkeypatch.setattr(server, "_generate_reply", fake_generate_reply)

    token = _CALLING_PRINCIPAL.set(Principal(AGENT, "agent-a"))
    try:
        result = await send_to_agent(target_agent="agent-b", message="What's the status?")
    finally:
        _CALLING_PRINCIPAL.reset(token)

    assert result == {"success": True, "agent": "agent-b", "reply": "Agent B's answer."}
    # Bare call: no tools/principal kwargs, the identical tool-free path
    # _handle_inbound_a2a already uses -- this is what makes B's reply
    # structurally incapable of delegating further.
    assert "tools" not in captured["kwargs"]
    assert "principal" not in captured["kwargs"]
    assert captured["messages"][0] == {"role": "system", "content": "You are Agent B, a specialist."}
    assert captured["messages"][1] == {"role": "user", "content": "What's the status?"}


@pytest.mark.asyncio
async def test_send_to_agent_reports_a_total_provider_failure_as_in_band(monkeypatch):
    """_generate_reply returns "" rather than raising when every provider
    failed -- that must not look like a silent, empty success."""
    from python.harness import db, server

    async def fake_fetchrow(*a, **k):
        return {"id": "agent-b"}

    async def fake_system_prompt(agent_id):
        return "prompt"

    async def fake_generate_reply(*a, **k):
        return ""

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(server, "_agent_system_prompt", fake_system_prompt)
    monkeypatch.setattr(server, "_generate_reply", fake_generate_reply)

    result = await send_to_agent(target_agent="agent-b", message="hi")

    assert result["success"] is False
    assert "agent-b" in result["error"]


@pytest.mark.asyncio
async def test_run_agent_tool_round_sets_and_resets_the_calling_principal(monkeypatch):
    from sdk.agent.tools import ToolRegistry

    seen = {}

    async def fake_run_tool(name, attrs, principal=None, **kw):
        import sdk.agent.agent_reply_tools as art
        seen["during_call"] = art._CALLING_PRINCIPAL.get()
        return {"success": True, "output": "ok"}

    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    principal = Principal(AGENT, "agent-123")
    assert _CALLING_PRINCIPAL.get() is None  # nothing set before the call

    await run_agent_tool_round('<call:get_system_info />', TOOLS, principal)

    assert seen["during_call"] is principal
    assert _CALLING_PRINCIPAL.get() is None  # reset afterward


@pytest.mark.asyncio
async def test_send_to_agent_outside_any_tool_round_degrades_gracefully(monkeypatch):
    """Called with no _CALLING_PRINCIPAL set at all (e.g. a stray direct
    invocation), the self-target check is simply skipped rather than raising."""
    from python.harness import db, server

    async def fake_fetchrow(*a, **k):
        return {"id": "agent-b"}

    async def fake_system_prompt(agent_id):
        return "prompt"

    async def fake_generate_reply(*a, **k):
        return "reply text"

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(server, "_agent_system_prompt", fake_system_prompt)
    monkeypatch.setattr(server, "_generate_reply", fake_generate_reply)

    assert _CALLING_PRINCIPAL.get() is None
    result = await send_to_agent(target_agent="agent-b", message="hi")

    assert result == {"success": True, "agent": "agent-b", "reply": "reply text"}
