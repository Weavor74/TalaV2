"""
halt_gate.py — Model A emergency-halt enforcement at RPC dispatch.

One check, before ``_HANDLERS`` runs, on every WebSocket and WebRTC path.
Fail-closed: if halt state cannot be read, the call is refused.

While halted, only:
  * health / status reads on the allowlist
  * ``task.control`` with action ``abort`` or ``pause``
  * ``system.release_halt`` (owner-gated inside its handler)

Resume does **not** release the halt. Abort does **not** require a later
Resume to unlock the system. Halt On/Off is a separate control.
"""
from __future__ import annotations

import logging
from typing import Any

from python.harness.jsonrpc import JsonRpcError, _err

logger = logging.getLogger(__name__)

#: Read-only health / status surfaces permitted while the kill switch is on.
HALTED_STATUS_METHODS: frozenset[str] = frozenset({
    # Read-only status surfaces only. system.self_test / system.capabilities
    # are operational (and can be expensive); they are refused while halted.
    "harness.health",
    "system.info",
    "system.architecture_contract",
    "task.status",
    "task.list",
    "vault.status",
    "inference.get_embedded_status",
})

#: Explicit owner unlock — never via task Resume.
HALTED_RELEASE_METHOD = "system.release_halt"

#: task.control actions that remain reachable while halted.
HALTED_TASK_CONTROL_ACTIONS: frozenset[str] = frozenset({"abort", "pause"})


def is_halted_fail_closed() -> bool:
    """
    True when the emergency halt is engaged, or when the question cannot be
    answered. A kill switch that fails open is not a kill switch.
    """
    try:
        from sdk.agent.gates import RevocationHalt
        return bool(RevocationHalt.is_halted())
    except Exception as exc:
        logger.error("halt_gate: could not read halt state; refusing: %s", exc)
        return True


def allowed_while_halted(method: str, params: dict[str, Any] | None = None) -> bool:
    """Whether *method* may run while the emergency halt is engaged."""
    if method == HALTED_RELEASE_METHOD:
        return True
    if method in HALTED_STATUS_METHODS:
        return True
    if method == "task.control":
        action = str((params or {}).get("action") or "").strip().lower()
        return action in HALTED_TASK_CONTROL_ACTIONS
    return False


def refusal_response(method: str, req_id: Any) -> str:
    """JSON-RPC error body for a call blocked by the halt."""
    return _err(
        req_id,
        JsonRpcError.INVALID_REQUEST,
        (
            f"Emergency halt is engaged; '{method}' was refused. "
            "Release it with system.release_halt (owner secret required)."
        ),
        {"halted": True},
    )


def check_rpc(method: str, params: dict[str, Any] | None, req_id: Any) -> str | None:
    """
    Return a JSON-RPC error string if the call must not proceed, else None.

    Fail-closed on halt-state read errors (treated as halted).
    """
    if not is_halted_fail_closed():
        return None
    if allowed_while_halted(method, params if isinstance(params, dict) else None):
        return None
    logger.critical("halt_gate: refusing %r -- emergency halt is engaged", method)
    return refusal_response(method, req_id)
