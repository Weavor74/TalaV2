"""
tests/test_emergency_halt_is_effective.py

The most serious instance of the dead-subsystem pattern found so far, because
it is the control you reach for when something is already going wrong.

Mission Control's abort calls ``trigger_emergency_halt()``, which sets
``_EMERGENCY_HALT_ACTIVE = True``. Exactly one place in the tree read that
flag -- ``AuthorizationGate.request_authorization`` -- and that method had no
callers anywhere. So abort marked rows failed in the database, logged
"!!! EMERGENCY HALT TRIGGERED !!!", and tool execution carried straight on.
The kill switch set a variable nobody looked at.

These tests assert that the halt stops things, that it can be released, and
that its state is visible. All three matter: a halt with no release is a
working kill switch with no key, and an invisible one is an hour of debugging
"why is nothing happening".
"""
from __future__ import annotations

import json

import pytest

from sdk.agent.gates import RevocationHalt
from sdk.agent.tools import ToolRegistry


@pytest.fixture(autouse=True)
def _always_release():
    """No test may leave the process halted."""
    yield
    RevocationHalt.release_emergency_halt()


@pytest.mark.asyncio
async def test_a_halt_actually_stops_a_tool():
    RevocationHalt.release_emergency_halt()
    assert RevocationHalt.is_halted() is False

    RevocationHalt.trigger_emergency_halt()
    result = await ToolRegistry.run_tool("get_system_info", {})
    assert result.get("success") is False
    assert result.get("halted") is True


@pytest.mark.asyncio
async def test_releasing_the_halt_restores_execution():
    RevocationHalt.trigger_emergency_halt()
    RevocationHalt.release_emergency_halt()

    result = await ToolRegistry.run_tool("get_system_info", {})
    assert result.get("halted") is not True, (
        "a released halt still blocked a tool"
    )


@pytest.mark.asyncio
async def test_the_halt_is_checked_before_anything_else():
    """
    A halted system should not be evaluating whether it is allowed to act.

    Asserted by ordering in the source: the halt check must precede the lease
    and permission gates, so that a lockdown short-circuits rather than
    queueing work behind an approval prompt.
    """
    import inspect

    src = inspect.getsource(ToolRegistry.run_tool)
    halt_at = src.find("RevocationHalt")
    lease_at = src.find("lease_policy")
    assert halt_at != -1, "run_tool does not check the emergency halt"
    assert lease_at != -1, "run_tool does not take a lease"
    assert halt_at < lease_at, "the halt is checked after the lease gate"


@pytest.mark.asyncio
async def test_resume_releases_the_halt(monkeypatch):
    """
    The other half of abort being real.

    Without a release path the first abort locks the agent out of every tool
    permanently, with nothing in the UI able to undo it.
    """
    from python.harness import server

    async def fake_execute(*a, **k):
        return None

    monkeypatch.setattr(server.db, "execute", fake_execute)

    RevocationHalt.trigger_emergency_halt()
    assert RevocationHalt.is_halted() is True

    raw = await server.handle_task_control(
        {"task_id": "some-task", "action": "resume"}, 1
    )
    payload = json.loads(raw)["result"]

    assert payload["success"] is True
    assert payload["halted"] is False
    assert RevocationHalt.is_halted() is False


@pytest.mark.asyncio
async def test_the_health_panel_reports_the_halt():
    """An invisible global lockdown is its own kind of outage."""
    from python.harness import server

    RevocationHalt.release_emergency_halt()
    status, detail = await server._probe_halt()
    assert status == "HEALTHY"
    assert detail

    RevocationHalt.trigger_emergency_halt()
    status, detail = await server._probe_halt()
    assert status == "CRITICAL"
    assert "halt" in detail.lower()


def test_the_demo_passcode_is_gone_entirely():
    """
    PrivilegedCommandHandler.verify_privileged_token returned True for the
    literal "tala-sudo-sig".

    The previous version of this test asserted only that nothing *called* it,
    which is a guard that holds until someone writes the first caller. The
    passcode was published in this repository and shipped in release bundles,
    so it could never have authenticated anyone. It is now deleted, and this
    asserts it stays deleted -- including that no implementation is quietly
    reintroduced under the same name.
    """
    import pathlib

    from sdk.agent import gates

    assert not hasattr(gates, "PrivilegedCommandHandler")

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list((root / "python").rglob("*.py")) + list((root / "sdk").rglob("*.py")):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # gates.py names both in a comment explaining the removal; an
        # implementation would be an assignment or a def, not prose.
        if "def verify_privileged_token" in text or "class PrivilegedCommandHandler" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        f"the hardcoded demo passcode has been reintroduced in: {offenders}"
    )
