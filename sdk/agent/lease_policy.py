"""
sdk/agent/lease_policy.py — the leases, wired.

WHY THIS EXISTS
---------------
``sdk/agent/leases.py`` has carried a complete lease/approval gateway since
Milestone 6, with a module docstring stating plainly:

    Gate-controlled operations:
      'ledger'    — any write to the PostgreSQL financial ledger
      'file_edit' — any filesystem mutation outside the agent's sandbox
      'wallet'    — any wallet balance or transaction operation

    A lease MUST be approved before the guarded operation executes.

Nothing called it. ``request_lease``, ``approve_lease``, ``deny_lease``,
``revoke_lease`` and ``await_approval`` had no production caller anywhere in
the tree, so that MUST described a rule no code enforced, and every ledger
write, wallet operation and out-of-sandbox file edit went through ungated.

WHAT THIS MODULE ADDS
---------------------
The mapping the gateway never had: which tool counts as which guarded
operation, and what to do about it. The gateway itself is unchanged -- it was
never the broken part.

MODES
-----
``enforce`` (default — Highs H7)
    Guarded operations block until a human approves them, surfaced the way
    self-edits already are. Unattended goals touching the ledger, the wallet
    or files outside the workspace stall rather than proceed. This is the
    fail-closed advanced-tool posture.

``open``
    Every guarded operation is auto-approved and recorded. Behaviour matches
    the historical ungated path, except leases are visible and the machinery
    runs. Use for first-run / local CPU-only bootstrapping.

``off``
    No leases requested at all. Present so the mechanism can be taken out of
    the path entirely when diagnosing something else, and so that "off" is an
    explicit state rather than something achieved by deleting a call.

Set with ``TALA_LEASE_MODE``. Default is ``enforce``. For first-run / dev on
a CPU-only box, set ``TALA_LEASE_MODE=open`` in ``.env`` (documented in
``.env.example`` and Settings → Guardrails). An unknown mode value fails
closed to ``enforce``, not open.

**Migration (Highs H7):** installs that previously relied on the implicit
``open`` default must set ``TALA_LEASE_MODE=open`` (or ``off``) explicitly.
New installs and production should leave the default (or set ``enforce``).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

MODE_OPEN = "open"
MODE_ENFORCE = "enforce"
MODE_OFF = "off"

_VALID_MODES = (MODE_OPEN, MODE_ENFORCE, MODE_OFF)

#: How long a pending lease waits for a human in enforce mode before it is
#: treated as unanswered. Short enough that a stalled unattended run is
#: obvious rather than mysterious.
ENFORCE_TIMEOUT_SECONDS = 120


# ---------------------------------------------------------------------------
# Which tools touch what
# ---------------------------------------------------------------------------

#: Exact tool names, checked first.
_WALLET_TOOLS: frozenset[str] = frozenset({
    "sync_blockchain",
})

_LEDGER_TOOLS: frozenset[str] = frozenset({
    "sync_blockchain",
    "update_manifest",
})

#: Any tool whose name matches these mutates the filesystem. Patterns rather
#: than a fixed list because the tool registry grows: a new write tool should
#: arrive already guarded, not wait to be noticed and added here.
_FILE_EDIT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in (
        r"^write_workspace_file$",
        r"^code\.(apply_patch|write_files|write_and_verify|propose_self_edit)$",
        r"^run_workspace_command$",
        r"^docs\.(create_documentation_node|create_learning_node|generate.*)$",
        r"^generate_detailed_neural_node$",
        r"^update_neural_node$",
        r"^compile_lora_adapter$",
        r"^import_skill_md$",
        r"^export_skill_md$",
    )
)

#: Wallet-ish names, matched loosely for the same reason.
_WALLET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in (r"wallet", r"^chain\.", r"transaction", r"payment")
)


def _classify_tool(tool_name: str) -> str | None:
    """
    The guarded operation a tool performs, or None if it is not gated.

    Order matters: wallet is checked before ledger before file_edit, because a
    tool that moves money and also writes a file should be reported as the
    former. A tool that matches nothing returns None and runs ungated, which
    is the common case -- most of the 97 tools only read.
    """
    if not tool_name:
        return None
    name = tool_name.strip().lower()

    if name in _WALLET_TOOLS or any(p.search(name) for p in _WALLET_PATTERNS):
        return "wallet"
    if name in _LEDGER_TOOLS:
        return "ledger"
    if any(p.match(name) for p in _FILE_EDIT_PATTERNS):
        return "file_edit"
    return None


def current_mode() -> str:
    """The active lease mode, defaulting to ``enforce`` (fail-closed)."""
    raw = (os.environ.get("TALA_LEASE_MODE") or "").strip().lower()
    if raw in _VALID_MODES:
        return raw
    if raw:
        log.warning(
            "lease_policy: TALA_LEASE_MODE=%r is not one of %s; using %r",
            raw, list(_VALID_MODES), MODE_ENFORCE,
        )
        return MODE_ENFORCE
    return MODE_ENFORCE


# ---------------------------------------------------------------------------
# The gateway, as a process-wide singleton
# ---------------------------------------------------------------------------

_GATEWAY: Any = None
_BROADCAST: Any = None


def register_broadcast(fn: Any) -> None:
    """
    Let pending leases reach the UI, the way self-edit approvals already do.

    Without this an ``enforce``-mode lease would block on a prompt nobody could
    see, which is a worse failure than not gating at all.
    """
    global _BROADCAST
    _BROADCAST = fn


def gateway():
    """The shared LeaseGateway, created on first use."""
    global _GATEWAY
    if _GATEWAY is None:
        from sdk.agent.leases import LeaseGateway

        def _announce(token: Any) -> None:
            if _BROADCAST is None:
                return
            try:
                import asyncio
                req = token.request
                coro = _BROADCAST("lease.pending", {
                    "token_id": token.token_id,
                    "operation": getattr(req, "operation_type", "?"),
                    "description": getattr(req, "description", ""),
                    "requested_by": getattr(req, "requested_by", ""),
                    "expires_at": token.expires_at.isoformat() if token.expires_at else None,
                })
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.ensure_future(coro, loop=loop)
                except RuntimeError:
                    pass
            except Exception as exc:
                log.debug("lease_policy: could not announce lease: %s", exc)

        _GATEWAY = LeaseGateway(approval_callback=_announce)
    return _GATEWAY


def guard(tool_name: str, requested_by: str = "agent",
          metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Take a lease for *tool_name* if it needs one.

    Returns a verdict dict: ``{"allowed": bool, "operation": str | None,
    "mode": str, "token_id": str | None, "reason": str}``.

    Never raises. A gate that can take the caller down with it gets removed
    the first time it does, and then there is no gate.
    """
    operation = _classify_tool(tool_name)
    if operation is None:
        return {"allowed": True, "operation": None, "mode": current_mode(),
                "token_id": None, "reason": "tool performs no guarded operation"}

    mode = current_mode()
    if mode == MODE_OFF:
        return {"allowed": True, "operation": operation, "mode": mode,
                "token_id": None, "reason": "lease checks disabled"}

    try:
        from sdk.agent.leases import LeaseRequest

        request = LeaseRequest(
            operation_type=operation,
            description=f"tool {tool_name!r}",
            requested_by=requested_by or "agent",
            metadata=metadata or {},
        )
        gw = gateway()
        gw.purge_expired()
        token = gw.request_lease(request)
    except Exception as exc:
        # Failing open is the right call in `open` mode and the wrong one in
        # `enforce`: if the gate itself is broken, an enforcing deployment must
        # not silently become a permissive one.
        log.error("lease_policy: could not take a lease for %r: %s", tool_name, exc)
        if mode == MODE_ENFORCE:
            return {"allowed": False, "operation": operation, "mode": mode,
                    "token_id": None,
                    "reason": f"lease machinery failed and mode is enforce: {exc}"}
        return {"allowed": True, "operation": operation, "mode": mode,
                "token_id": None, "reason": f"lease machinery failed, mode is open: {exc}"}

    if mode == MODE_OPEN:
        try:
            gw.approve_lease(token.token_id, approved_by="auto (mode=open)")
        except Exception as exc:  # pragma: no cover - approval of a fresh token
            log.warning("lease_policy: auto-approval failed for %s: %s", token.token_id, exc)
        log.info(
            "lease: %s [%s] auto-approved (mode=open) token=%s",
            tool_name, operation, token.token_id[:8],
        )
        return {"allowed": True, "operation": operation, "mode": mode,
                "token_id": token.token_id, "reason": "auto-approved and recorded"}

    # enforce. await_approval takes the token itself, not its id.
    try:
        approved = gw.await_approval(token, timeout_seconds=ENFORCE_TIMEOUT_SECONDS)
    except Exception as exc:
        log.error("lease_policy: await_approval failed for %s: %s", token.token_id, exc)
        approved = False

    if approved:
        return {"allowed": True, "operation": operation, "mode": mode,
                "token_id": token.token_id, "reason": "approved"}
    return {"allowed": False, "operation": operation, "mode": mode,
            "token_id": token.token_id,
            "reason": f"{operation} operation was not approved within "
                      f"{ENFORCE_TIMEOUT_SECONDS}s"}
