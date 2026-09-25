"""
tests/test_invariants_are_wired.py

Guards the fix for the ninth instance of this codebase's dominant defect: a
subsystem that is built, correct, tested, and never called.

``sdk/agent/verification.py`` has carried four architectural invariants since
Milestone 6 -- PostgreSQL as sole persistent authority, the mem0 read-only
guard, vault network isolation, and the astro style cap. ``register_defaults``
and ``run_all`` had no production caller anywhere in the tree. Every one of
those guarantees was enforced by a module that never executed, and the unit
tests for the module passed the whole time, because they tested the component
rather than its reachability.

These tests assert the *wiring*, not the logic. The logic is covered elsewhere;
what was missing was anything that would notice if the wiring went away again.
"""
from __future__ import annotations

import inspect
import json

import pytest

from python.harness import server
from sdk.agent.verification import InvariantChecker


def test_register_defaults_registers_all_four():
    """A checker that registers nothing passes everything."""
    checker = InvariantChecker()
    checker.register_defaults()
    names = [n for n, _ in checker._invariants]
    assert set(names) == {
        "postgres_is_sole_authority",
        "mem0_is_read_only",
        "vault_is_network_isolated",
        "style_cap_enforced",
    }


@pytest.mark.asyncio
async def test_health_panel_reports_the_invariants():
    """The probe must be reachable from the panel an operator actually reads."""
    status, detail = await server._probe_invariants()
    assert status in {"HEALTHY", "DEGRADED", "CRITICAL", server.UNKNOWN}
    assert detail, "an invariant verdict with no basis cannot be acted on"


@pytest.mark.asyncio
async def test_invariant_probe_is_registered_in_the_health_handler(monkeypatch):
    """
    ARCHITECTURAL_INVARIANTS must appear in the health payload.

    Asserted against the handler's output rather than its source, so that
    renaming the probe function does not quietly drop the indicator.
    """
    async def fail(*a, **k):
        raise RuntimeError("no database in this test")

    monkeypatch.setattr(server.db, "fetchval", fail)
    monkeypatch.setattr(server.db, "fetchrow", fail)
    monkeypatch.setattr(server.db, "get_pool", lambda: None)

    result = json.loads(await server.handle_logging_get_health({}, 1))["result"]
    assert "ARCHITECTURAL_INVARIANTS" in result["services"]
    assert result["details"]["ARCHITECTURAL_INVARIANTS"]


@pytest.mark.asyncio
async def test_an_empty_invariant_set_is_unknown_not_healthy(monkeypatch):
    """
    'Nothing failed' and 'nothing ran' must not look the same.

    This is the exact confusion that let the subsystem stay dead: no failures
    were ever reported, because no checks were ever executed.
    """
    monkeypatch.setattr(InvariantChecker, "register_defaults", lambda self: None)
    status, detail = await server._probe_invariants()
    assert status == server.UNKNOWN
    assert "no invariants registered" in detail


@pytest.mark.asyncio
async def test_a_violated_invariant_is_critical(monkeypatch):
    def one_false(self):
        self.register(lambda: False, "deliberately_false")

    monkeypatch.setattr(InvariantChecker, "register_defaults", one_false)
    status, detail = await server._probe_invariants()
    assert status == "CRITICAL"
    assert "deliberately_false" in detail


def test_startup_runs_the_invariants():
    """
    The panel is not enough on its own: a violation must reach the log at boot,
    whether or not anyone opens the health screen.
    """
    # _startup delegates the slower work to _startup_background; the invariants
    # run there. Check both, so moving the call between them stays passing and
    # deleting it from both does not.
    src = ""
    for name in ("_startup", "_startup_background"):
        fn = getattr(server, name, None)
        if fn is not None:
            src += inspect.getsource(fn)
    assert src, "neither _startup nor _startup_background exists to check"
    assert "register_defaults" in src, (
        "nothing runs the architectural invariants at startup; they are only "
        "checked if an operator happens to open the health panel"
    )


def test_the_mem0_invariant_probes_behaviour_not_configuration():
    """
    The architecture contract's rule, applied to the checker itself.

    The original implementation asked whether TALA_MEM0_READONLY was set. That
    is configuration, and configuration is not health -- it reported FAIL on a
    system whose writes were structurally blocked, and would have reported PASS
    on one where the guard had been replaced by a passthrough.
    """
    import python.harness.mem0_adapter as adapter
    from sdk.agent import verification

    assert verification._mem0_is_read_only() is True

    original = adapter._is_blocked
    try:
        adapter._is_blocked = lambda name: False
        assert verification._mem0_is_read_only() is False, (
            "the invariant passed while nothing was classifying writes"
        )
        adapter._is_blocked = lambda name: True
        assert verification._mem0_is_read_only() is False, (
            "a guard that blocks reads too is inert, not read-only"
        )
    finally:
        adapter._is_blocked = original

    assert verification._mem0_is_read_only() is True


def test_the_mem0_probe_does_not_construct_the_adapter():
    """
    Companion to tests/test_ui_claims.py::test_nothing_constructs_the_mem0_adapter.

    A probe must not be the thing that makes a dormant capability look live. If
    the verification module ever constructs the adapter, the grep that guards
    the README's semantic-memory claim starts finding a construction site that
    exists only for measurement.
    """
    src = inspect.getsource(
        __import__("sdk.agent.verification", fromlist=["verification"])
    )
    assert "Mem0ReadOnlyAdapter(" not in src
