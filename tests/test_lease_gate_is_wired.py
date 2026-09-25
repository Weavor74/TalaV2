"""
tests/test_lease_gate_is_wired.py

sdk/agent/leases.py stated since Milestone 6 that "a lease MUST be approved
before the guarded operation executes" for ledger, wallet and out-of-sandbox
file operations, and nothing ever requested one -- request_lease,
approve_lease, deny_lease, revoke_lease and await_approval had no production
caller anywhere in the tree.

These tests assert that the gate is in the path, that it classifies correctly,
and -- the part that actually matters -- that enforce mode can say no.
"""
from __future__ import annotations

import inspect
import threading

import pytest

from sdk.agent import lease_policy as LP


@pytest.fixture(autouse=True)
def _open_mode(monkeypatch):
    monkeypatch.setenv("TALA_LEASE_MODE", "open")
    yield


def test_the_gate_is_in_the_tool_chokepoint():
    """run_tool is the one path all 97 tools take; the gate must sit in it."""
    from sdk.agent.tools import ToolRegistry

    src = inspect.getsource(ToolRegistry.run_tool)
    assert "lease_policy" in src, "no lease is taken when a tool runs"


@pytest.mark.parametrize("tool,expected", [
    ("write_workspace_file", "file_edit"),
    ("code.apply_patch", "file_edit"),
    ("run_workspace_command", "file_edit"),
    ("update_neural_node", "file_edit"),
    ("sync_blockchain", "wallet"),
    ("read_neural_node", None),
    ("web_search", None),
    ("list_agents", None),
])
def test_classification(tool, expected):
    assert LP._classify_tool(tool) == expected


def test_read_only_tools_take_no_lease():
    """Most of the registry only reads; gating those would be noise."""
    verdict = LP.guard("read_neural_node")
    assert verdict["allowed"] is True
    assert verdict["operation"] is None
    assert verdict["token_id"] is None


def test_open_mode_allows_but_records():
    """
    The default. Behaviour is unchanged; what changes is that the operation is
    visible and the machinery runs. A gate that has never executed is not a
    safety feature, it is untested code.
    """
    verdict = LP.guard("write_workspace_file")
    assert verdict["allowed"] is True
    assert verdict["operation"] == "file_edit"
    assert verdict["token_id"], "an auto-approved lease left no record"


def test_off_mode_takes_no_lease_at_all(monkeypatch):
    monkeypatch.setenv("TALA_LEASE_MODE", "off")
    verdict = LP.guard("write_workspace_file")
    assert verdict["allowed"] is True
    assert verdict["token_id"] is None


def test_enforce_mode_can_actually_say_no(monkeypatch):
    """
    The assertion the whole module exists for. A gate that cannot refuse is
    decoration -- which is precisely what this one was for two milestones.
    """
    monkeypatch.setenv("TALA_LEASE_MODE", "enforce")
    monkeypatch.setattr(LP, "ENFORCE_TIMEOUT_SECONDS", 1)
    verdict = LP.guard("write_workspace_file")
    assert verdict["allowed"] is False
    assert verdict["operation"] == "file_edit"
    assert "not approved" in verdict["reason"]


def test_enforce_mode_allows_an_approved_lease(monkeypatch):
    monkeypatch.setenv("TALA_LEASE_MODE", "enforce")
    monkeypatch.setattr(LP, "ENFORCE_TIMEOUT_SECONDS", 5)

    gw = LP.gateway()
    original = gw.request_lease

    def approve_shortly(*args, **kwargs):
        token = original(*args, **kwargs)
        threading.Timer(0.1, lambda: gw.approve_lease(token.token_id, "test")).start()
        return token

    monkeypatch.setattr(gw, "request_lease", approve_shortly)
    verdict = LP.guard("write_workspace_file")
    assert verdict["allowed"] is True
    assert verdict["reason"] == "approved"


def test_a_broken_gate_fails_closed_when_enforcing(monkeypatch):
    """
    If the machinery breaks, an enforcing deployment must not quietly become a
    permissive one -- that is how a gate stops existing without anyone noticing.
    """
    def boom(*a, **k):
        raise RuntimeError("gateway is broken")

    monkeypatch.setattr(LP, "gateway", boom)

    monkeypatch.setenv("TALA_LEASE_MODE", "enforce")
    assert LP.guard("write_workspace_file")["allowed"] is False

    monkeypatch.setenv("TALA_LEASE_MODE", "open")
    assert LP.guard("write_workspace_file")["allowed"] is True


def test_an_unknown_mode_fails_closed_to_enforce(monkeypatch):
    monkeypatch.setenv("TALA_LEASE_MODE", "banana")
    assert LP.current_mode() == LP.MODE_ENFORCE


def test_startup_registers_the_broadcast():
    """An enforce-mode lease must be able to reach a human."""
    from python.harness import server

    src = ""
    for name in ("_startup", "_startup_background"):
        fn = getattr(server, name, None)
        if fn is not None:
            src += inspect.getsource(fn)
    assert "register_broadcast" in src and "lease_policy" in src, (
        "pending leases have no route to the UI; enforce mode would block on "
        "a prompt nobody can see"
    )
