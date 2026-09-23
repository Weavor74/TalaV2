"""
python/harness/health_rpc.py — harness.health RPC.

Extracted from server.py under the size ratchet so Highs H5 can add a
``halted`` field without growing the god file. Electron's network_isolation
reads result.halted on harness.health to keep haltActive in sync.
"""

from __future__ import annotations

import logging
from typing import Any

from python.harness import db
from python.harness.jsonrpc import _ok

logger = logging.getLogger(__name__)


async def handle_harness_health(params: dict[str, Any], req_id: Any) -> str:
    """``harness.health`` — server health, wallet, and emergency-halt flag."""
    try:
        row = await db.fetchrow(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) as total FROM token_ledger"
        )
        tokens_used = int(row["total"]) if row else 0
    except Exception:
        tokens_used = 0

    try:
        from python.harness.token_counter import wallet_state

        wallet = await wallet_state()
    except Exception as exc:
        logger.debug("harness.health: could not read the wallet (%s)", exc)
        wallet = {
            "balance": None,
            "mode": "unreachable",
            "unlimited": False,
            "reason": str(exc),
        }

    active_agent = "Tala Engine"
    try:
        agent_row = await db.fetchrow("SELECT name FROM agents WHERE is_active = TRUE")
        if agent_row:
            # Late import: get_current_chat_mode lives on server; avoid cycle at load.
            from python.harness.server import get_current_chat_mode

            chat_mode = get_current_chat_mode()
            if chat_mode == "hybrid":
                active_agent = f"{agent_row['name']} (Hybrid)"
            else:
                active_agent = agent_row["name"]
    except Exception:
        pass

    try:
        from python.harness.halt_gate import is_halted_fail_closed

        halted = is_halted_fail_closed()
    except Exception:
        halted = True  # fail-closed for Electron isolation

    return _ok(
        req_id,
        {
            "status": "ok",
            "active_agent": active_agent,
            "tokens_used": tokens_used,
            "token_balance": wallet["balance"],
            "token_mode": wallet["mode"],
            "token_unlimited": wallet["unlimited"],
            "last_latency_ms": 1,
            "halted": halted,
        },
    )
