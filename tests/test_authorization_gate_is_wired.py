"""
UI-103 / Highs H1: AuthorizationGate must have a real production caller.

ToolRegistry.run_tool is the chokepoint every tool takes. These tests assert
the gate is on that path, fails closed, and still respects halt + grants.
"""
from __future__ import annotations

import inspect

import pytest

from python.harness.access import AGENT, OWNER, Principal
from sdk.agent.gates import RevocationHalt
from sdk.agent.tools import ToolRegistry


@pytest.fixture(autouse=True)
def _always_release_halt():
    yield
    RevocationHalt.release_emergency_halt()


def test_authorization_gate_is_in_the_tool_chokepoint():
    src = inspect.getsource(ToolRegistry.run_tool)
    assert "AuthorizationGate" in src or "_authorization_gate" in src
    assert "request_authorization" in src
    halt_at = src.find("RevocationHalt")
    auth_at = src.find("request_authorization")
    lease_at = src.find("lease_policy")
    assert halt_at != -1 and auth_at != -1 and lease_at != -1
    assert halt_at < auth_at < lease_at


@pytest.mark.asyncio
async def test_owner_local_tool_run_is_allowed_when_not_halted():
    RevocationHalt.release_emergency_halt()
    result = await ToolRegistry.run_tool("get_system_info", {})
    assert result.get("authorization_denied") is not True
    assert result.get("halted") is not True


@pytest.mark.asyncio
async def test_agent_without_grant_is_refused(monkeypatch):
    """Grant model: agent principal with no invitation fails closed."""
    from python.harness import access
    from python.harness import environment as env

    async def no_grants(*a, **k):
        return access.Decision(
            False,
            "no grant in test",
            "agent:nobody",
            "get_system_info",
            "execute_tool",
        )

    monkeypatch.setattr(access, "check", no_grants)

    async def state():
        return {"lockdown": False, "environment": "local"}

    monkeypatch.setattr(env, "get_state", state)

    RevocationHalt.release_emergency_halt()
    result = await ToolRegistry.run_tool(
        "get_system_info",
        {},
        principal=Principal(AGENT, "nobody"),
    )
    assert result.get("success") is False
    assert result.get("authorization_denied") is True


@pytest.mark.asyncio
async def test_gate_fail_closed_when_check_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("gate exploded")

    gate = ToolRegistry._authorization_gate()
    monkeypatch.setattr(gate, "request_authorization", boom)

    result = await ToolRegistry.run_tool("get_system_info", {})
    assert result.get("success") is False
    assert result.get("authorization_denied") is True


@pytest.mark.asyncio
async def test_halt_still_wins_before_authorization():
    RevocationHalt.trigger_emergency_halt()
    result = await ToolRegistry.run_tool(
        "get_system_info",
        {},
        principal=Principal(OWNER),
    )
    assert result.get("success") is False
    assert result.get("halted") is True
