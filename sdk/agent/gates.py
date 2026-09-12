"""
sdk/agent/gates.py
==================
Authorization Gates & Emergency Halt Revocation — Tala Cognitive Engine Room
Milestone 4 | Tasks 4.5, 4.6, 4.7

Implements:
  • AuthorizationGate         — Gatekeeper checking approval signatures or ledger assertions.
  • RevocationHalt            — Global emergency kill-switch to shut down loop runners.

PrivilegedCommandHandler was removed. Its verify_privileged_token() returned
True for the literal "tala-sudo-sig" -- a hardcoded passcode, published in this
repository and shipped in release bundles. It had no callers, so it granted
nothing; but a demo passcode sitting in a module named "gates" is a privilege
escalation waiting for its first caller, and the safest version of it is the
one that does not exist.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from python.harness.access import Principal


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


def _halt_path() -> "Path":
    from pathlib import Path

    from sdk.agent.documentation_nodes import resolve_user_data_dir

    return Path(resolve_user_data_dir()) / "data" / "emergency_halt.json"


class RevocationHalt:
    """
    Global control switch to trigger emergency halt and revoke active workspace leases.

    The halt is held in two places on purpose.

    ``_EMERGENCY_HALT_ACTIVE`` is the fast path, read on every tool call.
    A file under the data root is the durable one, and it is what makes the
    halt mean anything: the in-process flag died with the process, so the
    documented way to escape an emergency halt was to restart the thing that
    was running away. A kill switch that a restart clears is a pause button.

    Reading the file on every check also makes the halt reachable from outside
    this process. Creating ``data/emergency_halt.json`` by any means -- another
    process, a shell, a file manager -- halts the harness, which matters
    precisely when the RPC surface is the thing that is wedged.
    """

    @classmethod
    def _persist(cls, reason: str) -> None:
        import json
        from datetime import UTC, datetime

        path = _halt_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({
                    "halted": True,
                    "reason": reason,
                    "at": datetime.now(UTC).isoformat(),
                }, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            # The in-memory flag is still set, so this process is halted. Say
            # loudly that the halt will not survive a restart rather than
            # letting the caller believe it will.
            log.critical(
                "EMERGENCY HALT could not be persisted to %s (%s). This process "
                "is halted, but a restart will clear it.", path, exc,
            )

    @classmethod
    def trigger_emergency_halt(cls, reason: str = "manual") -> None:
        """Globally activates emergency halt, freezing loop executions."""
        global _EMERGENCY_HALT_ACTIVE
        _EMERGENCY_HALT_ACTIVE = True
        cls._persist(reason)
        log.critical("!!! EMERGENCY HALT TRIGGERED !!! System execution locked down.")

    @classmethod
    def release_emergency_halt(cls) -> None:
        """Resets emergency halt state."""
        global _EMERGENCY_HALT_ACTIVE
        _EMERGENCY_HALT_ACTIVE = False
        path = _halt_path()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # Worse than it looks: the flag is clear but the file still says
            # halted, so the next is_halted() re-halts. Better to leave the
            # process halted and say so than to report a release that the next
            # check silently undoes.
            _EMERGENCY_HALT_ACTIVE = True
            log.critical(
                "Emergency halt could NOT be released: %s is still present (%s). "
                "Remove it by hand to resume.", path, exc,
            )
            return
        log.info("Emergency halt released.")

    @classmethod
    def is_halted(cls) -> bool:
        """Check if halt is currently engaged, in this process or on disk."""
        if _EMERGENCY_HALT_ACTIVE:
            return True
        path = _halt_path()
        try:
            if not path.exists():
                return False
        except OSError as exc:
            # Cannot tell. The safe answer to "should I run this?" when the
            # kill switch is unreadable is no.
            log.error("Halt state unreadable at %s (%s); treating as halted.", path, exc)
            return True
        try:
            import json

            return bool(json.loads(path.read_text(encoding="utf-8")).get("halted", True))
        except Exception as exc:
            # The file exists, so somebody meant something by it. An
            # unparseable halt file is not permission to carry on.
            log.error("Halt file at %s is unreadable (%s); treating as halted.", path, exc)
            return True


async def trigger_emergency_halt(reason: str = "manual") -> None:
    """
    Trigger the halt and stop what is already running.

    Refusing the *next* tool call was only half of an abort. A shell command
    already in flight -- the runaway training job, the loop that will not
    stop -- carried on to completion, because the halt was a gate in front of
    dispatch and nothing behind it. Abort is now retroactive.
    """
    RevocationHalt.trigger_emergency_halt(reason)

    from sdk.agent.async_shell import kill_running_processes

    killed = await kill_running_processes(f"emergency halt: {reason}")
    if killed:
        log.critical("Emergency halt killed %d running process(es).", killed)

