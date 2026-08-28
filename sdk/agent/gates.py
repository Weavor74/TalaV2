"""
sdk/agent/gates.py
==================
Authorization Gates & Emergency Halt Revocation — Tala Cognitive Engine Room
Milestone 4 | Tasks 4.5, 4.6, 4.7

Implements:
  • AuthorizationGate         — Gatekeeper checking approval signatures or ledger assertions.
  • RevocationHalt            — Global emergency kill-switch to shut down loop runners.
  • PrivilegedCommandHandler  — Handles credential challenges and sudo escalation flows.
"""

from __future__ import annotations

import logging
from typing import Any

from python.harness import db

log = logging.getLogger(__name__)

# Global runtime halt indicator
_EMERGENCY_HALT_ACTIVE = False


class AuthorizationGate:
    """
    Interceptors enforcing user approvals and cryptographic ledger checkpoints.
    """

    def __init__(self) -> None:
        # Pre-approved signatures: a signed intent from the owner for one
        # specific action.
        self._approved_actions: set[str] = set()
        # Why the last refusal happened. A denial a caller cannot explain is
        # the same problem as a silent failure.
        self.last_reason: str = ""

    def register_pre_approval(self, action_signature: str) -> None:
        """Register pre-approved signature token."""
        self._approved_actions.add(action_signature)

    async def request_authorization(
        self,
        action_type: str,
        resource: str,
        risk_class: str,
        principal: "Principal | None" = None,
        context: dict | None = None,
    ) -> bool:
        """
        Whether this action is permitted, for this principal, on this resource.

        This gate had no callers anywhere (UI-103), which is why the
        ``priority = 1`` bug it used to carry was latent rather than active:
        nothing was being wrongly authorised because nothing asked. It now
        resolves against the grant model in ``python.harness.access``, which is
        the model Steven specified -- by invitation, per resource, default
        deny, and with the agent a principal like any peer (Q-63: "An agent,
        should be assigned just like a peer").

        The old ``auth_credentials WHERE priority = 1`` lookup is gone. Any
        credential that happened to sort first authorised any action, which was
        never the intent and is not recoverable by tightening the query.

        Defaults to the *agent* principal when none is given. There is no
        "locally initiated" exemption: whoever asked, the agent is the actor.
        """
        from python.harness.access import AGENT, Principal, check

        if RevocationHalt.is_halted():
            log.critical("AuthorizationGate: blocked -- emergency halt is active.")
            return False

        if principal is None:
            principal = Principal(AGENT, "default")

        # A pre-approval is a signed intent from the owner for one action, and
        # still cannot survive the halt above.
        action_signature = f"{action_type}:{resource}"
        if action_signature in self._approved_actions:
            log.info("AuthorizationGate: %s is pre-authorized.", action_signature)
            return True

        # Low risk auto-allows only for the owner or a principal that already
        # holds a grant; it is not a bypass for anyone who asks.
        decision = await check(principal, resource, action_type, context)
        if decision.allowed:
            return True

        if risk_class == "low" and principal.is_owner:
            return True

        log.warning(
            "AuthorizationGate: refused %s on %s for %s -- %s",
            action_type, resource, principal, decision.reason,
        )
        self.last_reason = decision.reason
        return False


class RevocationHalt:
    """
    Global control switch to trigger emergency halt and revoke active workspace leases.
    """

    @classmethod
    def trigger_emergency_halt(cls) -> None:
        """Globally activates emergency halt, freezing loop executions."""
        global _EMERGENCY_HALT_ACTIVE
        _EMERGENCY_HALT_ACTIVE = True
        log.critical("!!! EMERGENCY HALT TRIGGERED !!! System execution locked down.")

    @classmethod
    def release_emergency_halt(cls) -> None:
        """Resets emergency halt state."""
        global _EMERGENCY_HALT_ACTIVE
        _EMERGENCY_HALT_ACTIVE = False
        log.info("Emergency halt released.")

    @classmethod
    def is_halted(cls) -> bool:
        """Check if halt is currently engaged."""
        return _EMERGENCY_HALT_ACTIVE


class PrivilegedCommandHandler:
    """
    Escalation pathways requiring signed authorization confirmations.
    """

    @staticmethod
    def verify_privileged_token(passcode: str) -> bool:
        """Verify password signature or cryptographic nonce."""
        # Simple verification rule for demonstration and testing
        return passcode == "tala-sudo-sig"


async def trigger_emergency_halt() -> None:
    """Globally triggers emergency halt, freezing loop executions."""
    RevocationHalt.trigger_emergency_halt()

