"""
sdk/agent/tool_gates.py
=======================
The pre-execution gate chain for ``ToolRegistry.run_tool``, in the order the
chokepoint applies them: plugin permissions, emergency halt, AuthorizationGate,
leases, and RiskClassifier authority levels.

Moved out of sdk/agent/tools.py wholesale (size-ratchet cut-down, same pattern
as python/harness/provider_rpc.py). run_tool remains the single chokepoint
every tool passes through -- it calls :func:`enforce` before touching the tool
-- but the gate plumbing itself lives here. Every gate fails closed: a check
that cannot decide refuses rather than proceeds (UI-audit C1).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_AUTH_GATE = None


def _gate():
    """Shared AuthorizationGate — one instance so pre-approvals stick."""
    global _AUTH_GATE
    if _AUTH_GATE is None:
        from sdk.agent.gates import AuthorizationGate
        _AUTH_GATE = AuthorizationGate()
    return _AUTH_GATE


async def enforce(
    name: str,
    arguments: dict[str, Any],
    *,
    plugin_id: str | None,
    authority_level: str,
    principal: Any | None,
) -> dict[str, Any] | None:
    """
    Run every pre-execution gate for tool *name*; None means cleared to run.

    A non-None return is the refusal dict run_tool hands straight back to the
    caller. The order is load-bearing and asserted by the wiring tests: halt
    before authorization before lease -- a halted system should not be
    evaluating whether it is allowed to act.
    """
    # --- GATEWAY PLUGIN PERMISSIONS --------------------------------------
    if plugin_id:
        try:
            from sdk.gateway.plugins import scan_plugins_directory
            from sdk.gateway.permissions import enforce_tool_permissions

            manifests = scan_plugins_directory()
            plugin_manifest = next((m for m in manifests if m.get("id") == plugin_id), None)

            if not plugin_manifest:
                return {
                    "success": False,
                    "error": f"Authorization required: Plugin '{plugin_id}' manifest not found."
                }

            enforce_tool_permissions(plugin_manifest, name, arguments)
        except Exception as e:
            log.warning("tool_gates: Gateway permission denial intercepted for tool %r: %s", name, e)
            return {
                "success": False,
                "error": str(e)
            }
    # ---------------------------------------

    # --- EMERGENCY HALT ------------------------------------------------
    # Checked first, before AuthorizationGate / leases / risk: a halted
    # system should not be evaluating whether it is allowed to act.
    # AuthorizationGate also refuses while halted (defense in depth).
    try:
        from sdk.agent.gates import RevocationHalt

        if RevocationHalt.is_halted():
            log.critical(
                "tool_gates: refusing %r -- emergency halt is engaged.", name,
            )
            return {
                "success": False,
                "halted": True,
                "error": (
                    f"Emergency halt is engaged; tool {name!r} was not run. "
                    "Release it with system.release_halt (owner secret)."
                ),
            }
    except Exception as exc:
        # Fail-closed: a kill switch that cannot be read must refuse, not
        # continue. UI-audit C1.
        log.error(
            "tool_gates: could not read halt state for %r: %s -- refusing",
            name, exc,
        )
        return {
            "success": False,
            "halted": True,
            "error": (
                f"Emergency halt state unreadable; tool {name!r} was not run. "
                "Release it with system.release_halt when the gate is healthy."
            ),
        }
    # -------------------------------------------------------------------

    # --- AUTHORIZATION GATE (UI-103 / Highs H1) ------------------------
    # Production caller for AuthorizationGate.request_authorization.
    # Fail-closed: a gate that cannot decide must refuse, not proceed.
    try:
        from python.harness.access import OWNER, Principal
        from sdk.agent.governance import RiskClassifier

        acting = principal
        if acting is None:
            # Authenticated local Electron/WS session = owner. Grant-scoped
            # peers/agents must pass Principal explicitly.
            acting = Principal(OWNER)
        risk_for_gate = RiskClassifier.classify_action("execute_tool", name)
        gate = _gate()
        allowed = await gate.request_authorization(
            "execute_tool",
            name,
            risk_for_gate,
            principal=acting,
            context={
                "plugin_id": plugin_id,
                "authority_level": authority_level,
            },
        )
        if not allowed:
            log.warning(
                "tool_gates: AuthorizationGate refused %r for %s: %s",
                name, acting, gate.last_reason or "denied",
            )
            return {
                "success": False,
                "authorization_denied": True,
                "error": (
                    f"AuthorizationGate refused tool {name!r}: "
                    f"{gate.last_reason or 'not authorized'}."
                ),
            }
    except Exception as exc:
        log.error(
            "tool_gates: authorization check raised for %r: %s -- refusing",
            name, exc,
        )
        return {
            "success": False,
            "authorization_denied": True,
            "error": (
                f"Authorization check failed for tool {name!r}; "
                f"execution refused. ({type(exc).__name__}: {exc})"
            ),
        }
    # -------------------------------------------------------------------

    # --- LEASE GATE ----------------------------------------------------
    # sdk/agent/leases.py has stated since Milestone 6 that "a lease MUST
    # be approved before the guarded operation executes" for ledger, wallet
    # and out-of-sandbox file operations. Nothing ever requested one: the
    # whole gateway had no production caller, so that MUST described a rule
    # no code enforced. run_tool is the single chokepoint every one of the
    # 97 tools passes through, so it is where the rule can actually hold.
    #
    # Default mode is `enforce` (Highs H7): guarded operations block for
    # sign-off. Set TALA_LEASE_MODE=open for first-run / local bootstrapping.
    try:
        from sdk.agent import lease_policy

        verdict = lease_policy.guard(name, requested_by=plugin_id or "agent",
                                     metadata={"arguments": sorted(arguments or {})})
        if not verdict["allowed"]:
            log.warning(
                "tool_gates: lease denied for %r [%s]: %s",
                name, verdict.get("operation"), verdict.get("reason"),
            )
            return {
                "success": False,
                "lease_denied": True,
                "operation": verdict.get("operation"),
                "token_id": verdict.get("token_id"),
                "error": (
                    f"Tool {name!r} performs a guarded "
                    f"{verdict.get('operation')} operation: {verdict.get('reason')}"
                ),
            }
    except Exception as exc:
        # Fail-closed: a lease check that crashes is not permission to act.
        # UI-audit C1.
        log.error(
            "tool_gates: lease check raised for %r: %s -- refusing",
            name, exc,
        )
        return {
            "success": False,
            "lease_denied": True,
            "error": (
                f"Lease check failed for tool {name!r}; execution refused. "
                f"({type(exc).__name__}: {exc})"
            ),
        }
    # -------------------------------------------------------------------

    # Governance gating using RiskClassifier
    from sdk.agent.governance import RiskClassifier

    risk = RiskClassifier.classify_action("execute_tool", name)

    # Check authority level
    if authority_level == "full_confirm":
        return {
            "success": False,
            "requires_confirmation": True,
            "risk_level": risk,
            "error": f"Authorization required: execution of tool {name} blocked.",
        }
    if authority_level == "confirm_writes" and risk in ("high", "critical"):
        return {
            "success": False,
            "requires_confirmation": True,
            "risk_level": risk,
            "error": f"Authorization required: execution of high-risk tool {name} blocked.",
        }
    if authority_level == "auto" and risk == "critical":
        return {
            "success": False,
            "requires_confirmation": True,
            "risk_level": risk,
            "error": f"Authorization required: execution of critical-risk tool {name} blocked.",
        }

    return None
