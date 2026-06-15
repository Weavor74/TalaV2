"""
sdk/contracts/preflight.py — Tala Cognitive Engine Room
========================================================
Micro-Token Pre-Flight Engine (Task 3.1.1 – 3.1.3).

Responsibility
--------------
Before any LLM inference call is dispatched, the Pre-Flight Engine MUST
be invoked to verify that the user's wallet holds enough crypto to cover
the estimated token cost.  If the escrow check fails, the engine raises
:class:`InsufficientEscrowError` with full accounting detail so the caller
can surface a human-readable funding prompt.

Token Economy Constants
-----------------------
- 1 AI token  = 1 / CRYPTO_TO_AI_TOKENS crypto
- 1 AI token  costs AI_TOKEN_COST_USD USD
- token count ≈ character count / 4  (GPT-family heuristic)

Durability
----------
Every preflight check is journalled to TWO sinks for belt-and-suspenders
reliability:

1. **PostgreSQL** (primary authority) — via :mod:`python.harness.db`
2. **SQLite** at ``data/preflight_log.db`` (local backup) — survives
   even a Postgres outage and is useful for offline auditing.

The SQLite journal is written synchronously (no asyncpg dependency) so
it works even during early startup before the asyncpg pool is ready.

Milestone 3 — Task 3.1.1 – 3.1.3
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # Keep forward-reference imports clean

# Re-export InsufficientEscrowError from the canonical exceptions module so
# callers can import it directly from this module without hunting for it.
from sdk.agent.exceptions import InsufficientEscrowError  # noqa: F401

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token Economy Constants (Task 3.1.1)
# ---------------------------------------------------------------------------

CRYPTO_TO_AI_TOKENS: int = 10_000_000
"""
Conversion rate: 1 crypto unit = 10,000,000 AI tokens.
Adjust this constant when the token economy is re-calibrated — never
hard-code the literal number elsewhere in the codebase.
"""

AI_TOKEN_COST_USD: float = 0.0000001
"""
USD cost per single AI token (one ten-millionth of a dollar).
Corresponds to roughly $0.10 per million tokens — competitive with
commodity LLM pricing at time of writing (2026-Q2).
"""

# ---------------------------------------------------------------------------
# SQLite journal path (local backup sink)
# ---------------------------------------------------------------------------

# Resolve relative to the repo root (two levels up from this file).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SQLITE_PATH = _REPO_ROOT / "data" / "preflight_log.db"


# ---------------------------------------------------------------------------
# PreflightResult dataclass (Task 3.1.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PreflightResult:
    """
    Immutable result record produced by :class:`PreflightEngine.run_preflight`.

    Fields
    ------
    approved : bool
        True iff the wallet holds sufficient escrow for the request.
    required_tokens : int
        Estimated AI tokens needed for the prompt.
    required_crypto : float
        Crypto units required to cover ``required_tokens``.
    wallet_balance : float
        Crypto balance at the moment of the check.
    deficit : float
        Shortfall amount (0.0 when ``approved`` is True).
    prompt_char_count : int
        Raw character count of the prompt fed into the estimate.
    checked_at : str
        ISO-8601 UTC timestamp of the check.
    """

    approved: bool
    required_tokens: int
    required_crypto: float
    wallet_balance: float
    deficit: float
    prompt_char_count: int
    checked_at: str


# ---------------------------------------------------------------------------
# Pure calculation helpers (Task 3.1.1 / 3.1.2)
# ---------------------------------------------------------------------------

def count_characters(text: str) -> int:
    """
    Return an exact character tally for *text*.

    This is intentionally a character map tally (``len``) rather than a
    Unicode grapheme cluster count so that the result is deterministic and
    language-agnostic — emoji, CJK, and multi-byte sequences are counted as
    single characters by Python's ``len()``, which is appropriate for the
    GPT-family token-estimation heuristic.

    Parameters
    ----------
    text:
        The raw prompt text whose length is to be measured.

    Returns
    -------
    int
        Exact number of Unicode code points in *text*.
    """
    return len(text)


def estimate_tokens(text: str) -> int:
    """
    Estimate the number of AI tokens required to process *text*.

    Uses the widely-cited GPT-family heuristic:
        token_count ≈ character_count / 4

    The result is rounded **up** (ceiling integer division) so we never
    under-provision the escrow check.  This is a conservative estimate —
    actual token usage may be lower, especially for highly-compressible
    inputs such as structured JSON.

    Parameters
    ----------
    text:
        Raw prompt text to estimate.

    Returns
    -------
    int
        Conservative upper-bound token estimate ( ≥ 1 ).
    """
    char_count = count_characters(text)
    # Ceiling division: (n + d - 1) // d
    return max(1, (char_count + 3) // 4)


def check_escrow(wallet_balance_crypto: float, required_ai_tokens: int) -> bool:
    """
    Return ``True`` iff *wallet_balance_crypto* is sufficient to fund
    *required_ai_tokens* at the current :data:`CRYPTO_TO_AI_TOKENS` rate.

    Parameters
    ----------
    wallet_balance_crypto:
        The wallet's current balance in native crypto units.
    required_ai_tokens:
        Number of AI tokens the upcoming request needs.

    Returns
    -------
    bool
        ``True`` = escrow is adequate; ``False`` = wallet is underfunded.
    """
    required_crypto = required_ai_tokens / CRYPTO_TO_AI_TOKENS
    return wallet_balance_crypto >= required_crypto


# ---------------------------------------------------------------------------
# SQLite journal helpers
# ---------------------------------------------------------------------------

def _ensure_sqlite_journal() -> sqlite3.Connection:
    """
    Open (or create) the local SQLite preflight journal and ensure the
    schema is initialised.  Thread-safe via the SQLite library's internal
    locking — not intended for concurrent process access.

    Returns
    -------
    sqlite3.Connection
        An open connection to ``data/preflight_log.db``.
    """
    _SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_SQLITE_PATH), check_same_thread=False)
    conn.execute(textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS preflight_log (
            id              INTEGER  PRIMARY KEY AUTOINCREMENT,
            checked_at      TEXT     NOT NULL,
            approved        INTEGER  NOT NULL,
            prompt_chars    INTEGER  NOT NULL,
            required_tokens INTEGER  NOT NULL,
            required_crypto REAL     NOT NULL,
            wallet_balance  REAL     NOT NULL,
            deficit         REAL     NOT NULL,
            prompt_hash     TEXT     NOT NULL,
            extra_json      TEXT     NOT NULL DEFAULT '{}'
        );
    """))
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pfl_checked_at ON preflight_log (checked_at DESC);"
    )
    conn.commit()
    return conn


def _write_sqlite_journal(result: PreflightResult, prompt_hash: str) -> None:
    """
    Append *result* to the local SQLite preflight journal.

    Failures are swallowed and re-logged as warnings so a SQLite error
    never prevents an approved request from proceeding.

    Parameters
    ----------
    result:
        The :class:`PreflightResult` to persist.
    prompt_hash:
        SHA-256 hex digest of the prompt (store hash, not plain text,
        for privacy compliance).
    """
    try:
        conn = _ensure_sqlite_journal()
        conn.execute(
            textwrap.dedent("""\
                INSERT INTO preflight_log (
                    checked_at, approved, prompt_chars,
                    required_tokens, required_crypto,
                    wallet_balance, deficit, prompt_hash, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """),
            (
                result.checked_at,
                int(result.approved),
                result.prompt_char_count,
                result.required_tokens,
                result.required_crypto,
                result.wallet_balance,
                result.deficit,
                prompt_hash,
                "{}",
            ),
        )
        conn.commit()
        conn.close()
        logger.debug("Preflight result written to SQLite journal: %s", _SQLITE_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SQLite preflight journal write failed (non-fatal): %s", exc)


async def _write_postgres_journal(result: PreflightResult, prompt_hash: str) -> None:
    """
    Append *result* to the ``preflight_log`` PostgreSQL table.

    Uses the project's shared asyncpg pool via :mod:`python.harness.db`.
    Failures are swallowed so a DB outage doesn't block an approved request.

    Parameters
    ----------
    result:
        The :class:`PreflightResult` to persist.
    prompt_hash:
        SHA-256 hex digest of the prompt.
    """
    try:
        # Import lazily to avoid circular imports and to tolerate the pool
        # not being initialised during unit tests.
        from python.harness import db  # type: ignore[import]

        # Ensure the preflight_log table exists (idempotent DDL).
        await db.execute(
            textwrap.dedent("""\
                CREATE TABLE IF NOT EXISTS preflight_log (
                    id              BIGSERIAL    PRIMARY KEY,
                    checked_at      TIMESTAMPTZ  NOT NULL,
                    approved        BOOLEAN      NOT NULL,
                    prompt_chars    INTEGER      NOT NULL,
                    required_tokens BIGINT       NOT NULL,
                    required_crypto NUMERIC(28,8) NOT NULL,
                    wallet_balance  NUMERIC(28,8) NOT NULL,
                    deficit         NUMERIC(28,8) NOT NULL,
                    prompt_hash     TEXT         NOT NULL,
                    extra_json      JSONB        NOT NULL DEFAULT '{}'
                );
            """),
            caller="preflight._write_postgres_journal",
        )
        await db.execute(
            textwrap.dedent("""\
                INSERT INTO preflight_log (
                    checked_at, approved, prompt_chars,
                    required_tokens, required_crypto,
                    wallet_balance, deficit, prompt_hash
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """),
            result.checked_at,
            result.approved,
            result.prompt_char_count,
            result.required_tokens,
            result.required_crypto,
            result.wallet_balance,
            result.deficit,
            prompt_hash,
            caller="preflight._write_postgres_journal",
        )
        logger.debug("Preflight result written to PostgreSQL journal")
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL preflight journal write failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# PreflightEngine (Task 3.1.3)
# ---------------------------------------------------------------------------

class PreflightEngine:
    """
    Micro-Token Pre-Flight Engine.

    The engine is the *gateway* for every LLM call in the Tala harness.
    It must be invoked — and must pass — before any tokens are dispatched
    to the inference engine.

    Typical usage (async context)
    ------------------------------
    ::

        engine = PreflightEngine()
        result = await engine.run_preflight(
            prompt="Summarise the following document …",
            wallet_balance_crypto=0.005,
        )
        # result.approved is True here — InsufficientEscrowError raised otherwise

    Typical usage (sync / test context)
    ------------------------------------
    ::

        engine = PreflightEngine()
        result = engine.run_preflight_sync(
            prompt="Hello world",
            wallet_balance_crypto=0.001,
        )
    """

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def run_preflight(
        self,
        prompt: str,
        wallet_balance_crypto: float,
    ) -> PreflightResult:
        """
        Perform a full pre-flight check for *prompt* against *wallet_balance_crypto*.

        Steps
        -----
        1. Count characters in *prompt*.
        2. Estimate required AI token count.
        3. Convert to required crypto.
        4. Compare against *wallet_balance_crypto*.
        5. Journal result to SQLite (always) and PostgreSQL (best-effort).
        6. If insufficient: raise :class:`InsufficientEscrowError`.
        7. If sufficient: return :class:`PreflightResult`.

        Parameters
        ----------
        prompt:
            The full prompt text that will be sent to the LLM.
        wallet_balance_crypto:
            Current wallet balance in native crypto units.

        Returns
        -------
        PreflightResult
            Fully populated result record (``approved=True``).

        Raises
        ------
        InsufficientEscrowError
            When ``wallet_balance_crypto < required_crypto``.
        """
        result = self._compute_result(prompt, wallet_balance_crypto)

        # --- Journal to SQLite (synchronous, always succeeds or warns) ---
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        _write_sqlite_journal(result, prompt_hash)

        # --- Journal to PostgreSQL (async, best-effort) ---
        await _write_postgres_journal(result, prompt_hash)

        if not result.approved:
            raise InsufficientEscrowError(
                f"Preflight failed: wallet holds {result.wallet_balance:.8f} crypto but "
                f"{result.required_crypto:.8f} is required for {result.required_tokens:,} tokens "
                f"(deficit: {result.deficit:.8f} crypto).",
                required=result.required_crypto,
                escrowed=result.wallet_balance,
                transaction_id=prompt_hash[:16],  # short reference for tracing
            )

        logger.info(
            "Preflight APPROVED — tokens=%d  required_crypto=%.8f  balance=%.8f",
            result.required_tokens,
            result.required_crypto,
            result.wallet_balance,
        )
        return result

    # ------------------------------------------------------------------
    # Public sync API (for tests and non-async callers)
    # ------------------------------------------------------------------

    def run_preflight_sync(
        self,
        prompt: str,
        wallet_balance_crypto: float,
    ) -> PreflightResult:
        """
        Synchronous wrapper around :meth:`run_preflight`.

        Journals to SQLite only (PostgreSQL journal is skipped in sync
        mode to avoid blocking the event loop).

        Parameters
        ----------
        prompt:
            Full prompt text.
        wallet_balance_crypto:
            Current wallet balance in crypto units.

        Returns
        -------
        PreflightResult
            Approved result.

        Raises
        ------
        InsufficientEscrowError
            When escrow is insufficient.
        """
        result = self._compute_result(prompt, wallet_balance_crypto)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        _write_sqlite_journal(result, prompt_hash)

        if not result.approved:
            raise InsufficientEscrowError(
                f"Preflight failed: wallet holds {result.wallet_balance:.8f} crypto but "
                f"{result.required_crypto:.8f} is required for {result.required_tokens:,} tokens "
                f"(deficit: {result.deficit:.8f} crypto).",
                required=result.required_crypto,
                escrowed=result.wallet_balance,
                transaction_id=prompt_hash[:16],
            )

        logger.info(
            "Preflight APPROVED (sync) — tokens=%d  required_crypto=%.8f  balance=%.8f",
            result.required_tokens,
            result.required_crypto,
            result.wallet_balance,
        )
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_result(prompt: str, wallet_balance_crypto: float) -> PreflightResult:
        """
        Pure calculation — no I/O.  Builds the :class:`PreflightResult`
        from first principles.

        Parameters
        ----------
        prompt:
            Raw prompt text.
        wallet_balance_crypto:
            Current wallet balance.

        Returns
        -------
        PreflightResult
            Fully populated (may have ``approved=False``).
        """
        char_count = count_characters(prompt)
        required_tokens = estimate_tokens(prompt)
        required_crypto = required_tokens / CRYPTO_TO_AI_TOKENS
        approved = wallet_balance_crypto >= required_crypto
        deficit = max(0.0, required_crypto - wallet_balance_crypto)
        checked_at = datetime.now(tz=timezone.utc).isoformat()

        return PreflightResult(
            approved=approved,
            required_tokens=required_tokens,
            required_crypto=required_crypto,
            wallet_balance=wallet_balance_crypto,
            deficit=deficit,
            prompt_char_count=char_count,
            checked_at=checked_at,
        )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "CRYPTO_TO_AI_TOKENS",
    "AI_TOKEN_COST_USD",
    "count_characters",
    "estimate_tokens",
    "check_escrow",
    "PreflightResult",
    "PreflightEngine",
    "InsufficientEscrowError",
]
