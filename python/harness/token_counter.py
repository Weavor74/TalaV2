"""
token_counter.py — Tala Cognitive Engine Room: Token Counting Interceptor
=========================================================================
Every prompt MUST pass through this module before reaching vLLM.

Responsibilities
----------------
1. Count tokens using ``tiktoken`` (cl100k_base encoding as universal fallback).
2. Compute cost: ``cost_usd = token_count * TOKEN_COST_PER_TOKEN``
   (default: 0.0000001 USD per token — one ten-millionth).
3. Check the wallet balance in PostgreSQL against the required cost.
4. Raise :class:`InsufficientTokenBalanceError` if balance is too low.
5. Log every count to the ``token_ledger`` table (append-only accounting).
6. Return a :class:`TokenCheckResult` to the caller.

Milestone 1.2 — Task 1.2.3
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tiktoken initialisation (with graceful fallback)
# ---------------------------------------------------------------------------

try:
    import tiktoken

    _ENCODING = tiktoken.get_encoding("cl100k_base")
    _TIKTOKEN_AVAILABLE = True
    logger.debug("tiktoken loaded with cl100k_base encoding")
except ImportError:  # pragma: no cover
    _TIKTOKEN_AVAILABLE = False
    _ENCODING = None  # type: ignore[assignment]
    logger.warning(
        "tiktoken is not installed — falling back to whitespace-split token estimation. "
        "Install tiktoken for accurate counts: pip install tiktoken"
    )


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class InsufficientTokenBalanceError(Exception):
    """
    Raised when the wallet's ``ai_token_balance`` cannot cover the cost of
    the requested inference.

    Attributes
    ----------
    required_cost : float
        USD cost of the rejected request.
    current_balance : float
        Current wallet balance at the time of the check.
    token_count : int
        Number of tokens in the prompt that triggered this error.
    """

    def __init__(
        self,
        required_cost: float,
        current_balance: float,
        token_count: int,
        session_id: str = "",
    ) -> None:
        self.required_cost = required_cost
        self.current_balance = current_balance
        self.token_count = token_count
        self.session_id = session_id
        super().__init__(
            f"Insufficient AI token balance.  "
            f"Required: ${required_cost:.10f} USD ({token_count} tokens)  "
            f"Available: ${current_balance:.10f} USD  "
            f"Session: {session_id or 'unknown'}"
        )


class TokenCountError(Exception):
    """Raised when token counting itself fails in an unrecoverable way."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TokenCheckResult:
    """
    Immutable result returned by :func:`count_and_check`.

    Fields
    ------
    prompt_tokens:
        Number of tokens counted in the prompt.
    cost_usd:
        Computed cost in USD.
    balance_before:
        Wallet balance *before* deducting this cost.
    balance_after:
        Wallet balance *after* deducting this cost (written to DB).
    session_id:
        Caller-supplied session identifier.
    encoding:
        Name of the encoding used (e.g. ``'cl100k_base'`` or ``'estimate'``).
    elapsed_ms:
        Wall-clock time taken to complete the check, in milliseconds.
    ledger_id:
        Primary key of the inserted ``token_ledger`` row.
    """

    prompt_tokens: int
    cost_usd: float
    balance_before: float
    balance_after: float
    session_id: str
    encoding: str
    elapsed_ms: float
    ledger_id: int = field(default=0)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> tuple[int, str]:
    """
    Count the number of tokens in *text*.

    Returns
    -------
    (token_count, encoding_name)
        Where *encoding_name* is ``'cl100k_base'`` when tiktoken is available,
        or ``'estimate'`` when falling back to word-split heuristic.
    """
    if _TIKTOKEN_AVAILABLE and _ENCODING is not None:
        tokens = _ENCODING.encode(text)
        return len(tokens), "cl100k_base"

    # Heuristic fallback: split on whitespace, count words, multiply by 1.3
    # (rough approximation of sub-word tokenisation overhead)
    estimated = max(1, int(len(text.split()) * 1.3))
    return estimated, "estimate"


async def count_and_check(
    prompt: str,
    session_id: str,
    model: str = "",
    completion_tokens: int = 0,
) -> TokenCheckResult:
    """
    Count tokens in *prompt*, verify wallet balance, deduct cost, and log.

    This is the **mandatory gate** before any vLLM inference call.

    Parameters
    ----------
    prompt:
        The raw text that will be sent to the model.
    session_id:
        Caller-supplied session or request identifier for ledger traceability.
    model:
        Model name to record in the ledger (informational only).
    completion_tokens:
        If known in advance (e.g. from a budget parameter), include for
        accurate cost accounting.  Usually 0 at prompt-time.

    Returns
    -------
    TokenCheckResult

    Raises
    ------
    InsufficientTokenBalanceError
        If the wallet balance cannot cover the prompt cost.
    TokenCountError
        If counting fails for an unexpected reason.
    """
    from . import db  # local import to avoid circular dependency at module level

    t0 = time.perf_counter()

    # -- 1. Count tokens --------------------------------------------------
    try:
        token_count, encoding_name = count_tokens(prompt)
    except Exception as exc:
        raise TokenCountError(f"Token counting failed: {exc}") from exc

    total_tokens = token_count + completion_tokens
    from .config import settings
    cost_usd = total_tokens * settings.TOKEN_COST_PER_TOKEN

    logger.debug(
        "Token count: %d tokens (encoding=%s) cost=$%.10f session=%s",
        total_tokens, encoding_name, cost_usd, session_id,
    )

    # -- 2. Check and deduct wallet balance (atomic via DB transaction) ----
    async with db.transaction() as conn:
        wallet_row = await conn.fetchrow(
            "SELECT ai_token_balance FROM wallet WHERE id = 1 FOR UPDATE"
        )
        if wallet_row is None:
            raise TokenCountError(
                "Wallet row not found in database.  "
                "Run db.init_pool() to seed the wallet."
            )

        balance_before = float(wallet_row["ai_token_balance"])

        if balance_before < cost_usd:
            raise InsufficientTokenBalanceError(
                required_cost=cost_usd,
                current_balance=balance_before,
                token_count=total_tokens,
                session_id=session_id,
            )

        balance_after = balance_before - cost_usd

        # Deduct from wallet
        await conn.execute(
            """
            UPDATE wallet
            SET ai_token_balance = $1,
                updated_at = NOW()
            WHERE id = 1
            """,
            balance_after,
        )

        # Append to ledger (immutable audit trail)
        ledger_row = await conn.fetchrow(
            """
            INSERT INTO token_ledger
                (session_id, prompt_tokens, completion_tokens, cost_usd, model)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            session_id,
            token_count,
            completion_tokens,
            cost_usd,
            model or "",
        )
        ledger_id = ledger_row["id"] if ledger_row else 0

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    logger.info(
        "Token gate PASSED  session=%s tokens=%d cost=$%.10f "
        "balance=%.4f->%.4f  ledger_id=%d  elapsed=%.1fms",
        session_id, total_tokens, cost_usd,
        balance_before, balance_after,
        ledger_id, elapsed_ms,
    )

    return TokenCheckResult(
        prompt_tokens=token_count,
        cost_usd=cost_usd,
        balance_before=balance_before,
        balance_after=balance_after,
        session_id=session_id,
        encoding=encoding_name,
        elapsed_ms=elapsed_ms,
        ledger_id=ledger_id,
    )


async def get_wallet_balance() -> float:
    """Return the current ``ai_token_balance`` from the wallet table."""
    from . import db
    row = await db.fetchrow("SELECT ai_token_balance FROM wallet WHERE id = 1")
    if row is None:
        return 0.0
    return float(row["ai_token_balance"])


async def top_up_wallet(amount: float, caller: str = "system") -> float:
    """
    Add *amount* to the wallet's AI token balance.

    Returns the new balance.
    """
    from . import db
    from .db import log_to_db

    row = await db.fetchrow(
        """
        UPDATE wallet
        SET ai_token_balance = ai_token_balance + $1,
            updated_at = NOW()
        WHERE id = 1
        RETURNING ai_token_balance
        """,
        float(amount),
    )
    new_balance = float(row["ai_token_balance"]) if row else 0.0
    await log_to_db(
        "INFO",
        f"Wallet topped up by {amount:.4f} by '{caller}'.  New balance: {new_balance:.4f}",
        {"caller": caller, "amount": amount, "new_balance": new_balance},
    )
    return new_balance
