# File: `preflight.py`

## Module Description

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

## Classes

### `class PreflightResult`
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


### `class PreflightEngine`
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

- **`def run_preflight(self, prompt, wallet_balance_crypto)`**
  - Perform a full pre-flight check for *prompt* against *wallet_balance_crypto*.
  
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
- **`def run_preflight_sync(self, prompt, wallet_balance_crypto)`**
  - Synchronous wrapper around :meth:`run_preflight`.
  
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
- **`def _compute_result(prompt, wallet_balance_crypto)`**
  - Pure calculation — no I/O.  Builds the :class:`PreflightResult`
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

## Functions

### `def count_characters(text)`
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

### `def estimate_tokens(text)`
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

### `def check_escrow(wallet_balance_crypto, required_ai_tokens)`
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

### `def _ensure_sqlite_journal()`
Open (or create) the local SQLite preflight journal and ensure the
schema is initialised.  Thread-safe via the SQLite library's internal
locking — not intended for concurrent process access.

Returns
-------
sqlite3.Connection
    An open connection to ``data/preflight_log.db``.

### `def _write_sqlite_journal(result, prompt_hash)`
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

### `async def _write_postgres_journal(result, prompt_hash)`
Append *result* to the ``preflight_log`` PostgreSQL table.

Uses the project's shared asyncpg pool via :mod:`python.harness.db`.
Failures are swallowed so a DB outage doesn't block an approved request.

Parameters
----------
result:
    The :class:`PreflightResult` to persist.
prompt_hash:
    SHA-256 hex digest of the prompt.

## Source Code

```python
"""\nsdk/contracts/preflight.py — Tala Cognitive Engine Room\n========================================================\nMicro-Token Pre-Flight Engine (Task 3.1.1 – 3.1.3).\n\nResponsibility\n--------------\nBefore any LLM inference call is dispatched, the Pre-Flight Engine MUST\nbe invoked to verify that the user's wallet holds enough crypto to cover\nthe estimated token cost.  If the escrow check fails, the engine raises\n:class:`InsufficientEscrowError` with full accounting detail so the caller\ncan surface a human-readable funding prompt.\n\nToken Economy Constants\n-----------------------\n- 1 AI token  = 1 / CRYPTO_TO_AI_TOKENS crypto\n- 1 AI token  costs AI_TOKEN_COST_USD USD\n- token count ≈ character count / 4  (GPT-family heuristic)\n\nDurability\n----------\nEvery preflight check is journalled to TWO sinks for belt-and-suspenders\nreliability:\n\n1. **PostgreSQL** (primary authority) — via :mod:`python.harness.db`\n2. **SQLite** at ``data/preflight_log.db`` (local backup) — survives\n   even a Postgres outage and is useful for offline auditing.\n\nThe SQLite journal is written synchronously (no asyncpg dependency) so\nit works even during early startup before the asyncpg pool is ready.\n\nMilestone 3 — Task 3.1.1 – 3.1.3\n"""\n\nfrom __future__ import annotations\n\nimport asyncio\nimport hashlib\nimport json\nimport logging\nimport sqlite3\nimport textwrap\nfrom dataclasses import asdict, dataclass\nfrom datetime import datetime, timezone\nfrom pathlib import Path\nfrom typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    pass  # Keep forward-reference imports clean\n\n# Re-export InsufficientEscrowError from the canonical exceptions module so\n# callers can import it directly from this module without hunting for it.\nfrom sdk.agent.exceptions import InsufficientEscrowError  # noqa: F401\n\nlogger = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Token Economy Constants (Task 3.1.1)\n# ---------------------------------------------------------------------------\n\nCRYPTO_TO_AI_TOKENS: int = 10_000_000\n"""\nConversion rate: 1 crypto unit = 10,000,000 AI tokens.\nAdjust this constant when the token economy is re-calibrated — never\nhard-code the literal number elsewhere in the codebase.\n"""\n\nAI_TOKEN_COST_USD: float = 0.0000001\n"""\nUSD cost per single AI token (one ten-millionth of a dollar).\nCorresponds to roughly $0.10 per million tokens — competitive with\ncommodity LLM pricing at time of writing (2026-Q2).\n"""\n\n# ---------------------------------------------------------------------------\n# SQLite journal path (local backup sink)\n# ---------------------------------------------------------------------------\n\n# Resolve relative to the repo root (two levels up from this file).\n_REPO_ROOT = Path(__file__).resolve().parent.parent.parent\n_SQLITE_PATH = _REPO_ROOT / "data" / "preflight_log.db"\n\n\n# ---------------------------------------------------------------------------\n# PreflightResult dataclass (Task 3.1.3)\n# ---------------------------------------------------------------------------\n\n@dataclass(frozen=True, slots=True)\nclass PreflightResult:\n    """\n    Immutable result record produced by :class:`PreflightEngine.run_preflight`.\n\n    Fields\n    ------\n    approved : bool\n        True iff the wallet holds sufficient escrow for the request.\n    required_tokens : int\n        Estimated AI tokens needed for the prompt.\n    required_crypto : float\n        Crypto units required to cover ``required_tokens``.\n    wallet_balance : float\n        Crypto balance at the moment of the check.\n    deficit : float\n        Shortfall amount (0.0 when ``approved`` is True).\n    prompt_char_count : int\n        Raw character count of the prompt fed into the estimate.\n    checked_at : str\n        ISO-8601 UTC timestamp of the check.\n    """\n\n    approved: bool\n    required_tokens: int\n    required_crypto: float\n    wallet_balance: float\n    deficit: float\n    prompt_char_count: int\n    checked_at: str\n\n\n# ---------------------------------------------------------------------------\n# Pure calculation helpers (Task 3.1.1 / 3.1.2)\n# ---------------------------------------------------------------------------\n\ndef count_characters(text: str) -> int:\n    """\n    Return an exact character tally for *text*.\n\n    This is intentionally a character map tally (``len``) rather than a\n    Unicode grapheme cluster count so that the result is deterministic and\n    language-agnostic — emoji, CJK, and multi-byte sequences are counted as\n    single characters by Python's ``len()``, which is appropriate for the\n    GPT-family token-estimation heuristic.\n\n    Parameters\n    ----------\n    text:\n        The raw prompt text whose length is to be measured.\n\n    Returns\n    -------\n    int\n        Exact number of Unicode code points in *text*.\n    """\n    return len(text)\n\n\ndef estimate_tokens(text: str) -> int:\n    """\n    Estimate the number of AI tokens required to process *text*.\n\n    Uses the widely-cited GPT-family heuristic:\n        token_count ≈ character_count / 4\n\n    The result is rounded **up** (ceiling integer division) so we never\n    under-provision the escrow check.  This is a conservative estimate —\n    actual token usage may be lower, especially for highly-compressible\n    inputs such as structured JSON.\n\n    Parameters\n    ----------\n    text:\n        Raw prompt text to estimate.\n\n    Returns\n    -------\n    int\n        Conservative upper-bound token estimate ( ≥ 1 ).\n    """\n    char_count = count_characters(text)\n    # Ceiling division: (n + d - 1) // d\n    return max(1, (char_count + 3) // 4)\n\n\ndef check_escrow(wallet_balance_crypto: float, required_ai_tokens: int) -> bool:\n    """\n    Return ``True`` iff *wallet_balance_crypto* is sufficient to fund\n    *required_ai_tokens* at the current :data:`CRYPTO_TO_AI_TOKENS` rate.\n\n    Parameters\n    ----------\n    wallet_balance_crypto:\n        The wallet's current balance in native crypto units.\n    required_ai_tokens:\n        Number of AI tokens the upcoming request needs.\n\n    Returns\n    -------\n    bool\n        ``True`` = escrow is adequate; ``False`` = wallet is underfunded.\n    """\n    required_crypto = required_ai_tokens / CRYPTO_TO_AI_TOKENS\n    return wallet_balance_crypto >= required_crypto\n\n\n# ---------------------------------------------------------------------------\n# SQLite journal helpers\n# ---------------------------------------------------------------------------\n\ndef _ensure_sqlite_journal() -> sqlite3.Connection:\n    """\n    Open (or create) the local SQLite preflight journal and ensure the\n    schema is initialised.  Thread-safe via the SQLite library's internal\n    locking — not intended for concurrent process access.\n\n    Returns\n    -------\n    sqlite3.Connection\n        An open connection to ``data/preflight_log.db``.\n    """\n    _SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)\n    conn = sqlite3.connect(str(_SQLITE_PATH), check_same_thread=False)\n    conn.execute(textwrap.dedent("""\\n        CREATE TABLE IF NOT EXISTS preflight_log (\n            id              INTEGER  PRIMARY KEY AUTOINCREMENT,\n            checked_at      TEXT     NOT NULL,\n            approved        INTEGER  NOT NULL,\n            prompt_chars    INTEGER  NOT NULL,\n            required_tokens INTEGER  NOT NULL,\n            required_crypto REAL     NOT NULL,\n            wallet_balance  REAL     NOT NULL,\n            deficit         REAL     NOT NULL,\n            prompt_hash     TEXT     NOT NULL,\n            extra_json      TEXT     NOT NULL DEFAULT '{}'\n        );\n    """))\n    conn.execute(\n        "CREATE INDEX IF NOT EXISTS idx_pfl_checked_at ON preflight_log (checked_at DESC);"\n    )\n    conn.commit()\n    return conn\n\n\ndef _write_sqlite_journal(result: PreflightResult, prompt_hash: str) -> None:\n    """\n    Append *result* to the local SQLite preflight journal.\n\n    Failures are swallowed and re-logged as warnings so a SQLite error\n    never prevents an approved request from proceeding.\n\n    Parameters\n    ----------\n    result:\n        The :class:`PreflightResult` to persist.\n    prompt_hash:\n        SHA-256 hex digest of the prompt (store hash, not plain text,\n        for privacy compliance).\n    """\n    try:\n        conn = _ensure_sqlite_journal()\n        conn.execute(\n            textwrap.dedent("""\\n                INSERT INTO preflight_log (\n                    checked_at, approved, prompt_chars,\n                    required_tokens, required_crypto,\n                    wallet_balance, deficit, prompt_hash, extra_json\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n            """),\n            (\n                result.checked_at,\n                int(result.approved),\n                result.prompt_char_count,\n                result.required_tokens,\n                result.required_crypto,\n                result.wallet_balance,\n                result.deficit,\n                prompt_hash,\n                "{}",\n            ),\n        )\n        conn.commit()\n        conn.close()\n        logger.debug("Preflight result written to SQLite journal: %s", _SQLITE_PATH)\n    except Exception as exc:  # noqa: BLE001\n        logger.warning("SQLite preflight journal write failed (non-fatal): %s", exc)\n\n\nasync def _write_postgres_journal(result: PreflightResult, prompt_hash: str) -> None:\n    """\n    Append *result* to the ``preflight_log`` PostgreSQL table.\n\n    Uses the project's shared asyncpg pool via :mod:`python.harness.db`.\n    Failures are swallowed so a DB outage doesn't block an approved request.\n\n    Parameters\n    ----------\n    result:\n        The :class:`PreflightResult` to persist.\n    prompt_hash:\n        SHA-256 hex digest of the prompt.\n    """\n    try:\n        # Import lazily to avoid circular imports and to tolerate the pool\n        # not being initialised during unit tests.\n        from python.harness import db  # type: ignore[import]\n\n        # Ensure the preflight_log table exists (idempotent DDL).\n        await db.execute(\n            textwrap.dedent("""\\n                CREATE TABLE IF NOT EXISTS preflight_log (\n                    id              BIGSERIAL    PRIMARY KEY,\n                    checked_at      TIMESTAMPTZ  NOT NULL,\n                    approved        BOOLEAN      NOT NULL,\n                    prompt_chars    INTEGER      NOT NULL,\n                    required_tokens BIGINT       NOT NULL,\n                    required_crypto NUMERIC(28,8) NOT NULL,\n                    wallet_balance  NUMERIC(28,8) NOT NULL,\n                    deficit         NUMERIC(28,8) NOT NULL,\n                    prompt_hash     TEXT         NOT NULL,\n                    extra_json      JSONB        NOT NULL DEFAULT '{}'\n                );\n            """),\n            caller="preflight._write_postgres_journal",\n        )\n        await db.execute(\n            textwrap.dedent("""\\n                INSERT INTO preflight_log (\n                    checked_at, approved, prompt_chars,\n                    required_tokens, required_crypto,\n                    wallet_balance, deficit, prompt_hash\n                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)\n            """),\n            result.checked_at,\n            result.approved,\n            result.prompt_char_count,\n            result.required_tokens,\n            result.required_crypto,\n            result.wallet_balance,\n            result.deficit,\n            prompt_hash,\n            caller="preflight._write_postgres_journal",\n        )\n        logger.debug("Preflight result written to PostgreSQL journal")\n    except Exception as exc:  # noqa: BLE001\n        logger.warning("PostgreSQL preflight journal write failed (non-fatal): %s", exc)\n\n\n# ---------------------------------------------------------------------------\n# PreflightEngine (Task 3.1.3)\n# ---------------------------------------------------------------------------\n\nclass PreflightEngine:\n    """\n    Micro-Token Pre-Flight Engine.\n\n    The engine is the *gateway* for every LLM call in the Tala harness.\n    It must be invoked — and must pass — before any tokens are dispatched\n    to the inference engine.\n\n    Typical usage (async context)\n    ------------------------------\n    ::\n\n        engine = PreflightEngine()\n        result = await engine.run_preflight(\n            prompt="Summarise the following document …",\n            wallet_balance_crypto=0.005,\n        )\n        # result.approved is True here — InsufficientEscrowError raised otherwise\n\n    Typical usage (sync / test context)\n    ------------------------------------\n    ::\n\n        engine = PreflightEngine()\n        result = engine.run_preflight_sync(\n            prompt="Hello world",\n            wallet_balance_crypto=0.001,\n        )\n    """\n\n    # ------------------------------------------------------------------\n    # Public async API\n    # ------------------------------------------------------------------\n\n    async def run_preflight(\n        self,\n        prompt: str,\n        wallet_balance_crypto: float,\n    ) -> PreflightResult:\n        """\n        Perform a full pre-flight check for *prompt* against *wallet_balance_crypto*.\n\n        Steps\n        -----\n        1. Count characters in *prompt*.\n        2. Estimate required AI token count.\n        3. Convert to required crypto.\n        4. Compare against *wallet_balance_crypto*.\n        5. Journal result to SQLite (always) and PostgreSQL (best-effort).\n        6. If insufficient: raise :class:`InsufficientEscrowError`.\n        7. If sufficient: return :class:`PreflightResult`.\n\n        Parameters\n        ----------\n        prompt:\n            The full prompt text that will be sent to the LLM.\n        wallet_balance_crypto:\n            Current wallet balance in native crypto units.\n\n        Returns\n        -------\n        PreflightResult\n            Fully populated result record (``approved=True``).\n\n        Raises\n        ------\n        InsufficientEscrowError\n            When ``wallet_balance_crypto < required_crypto``.\n        """\n        result = self._compute_result(prompt, wallet_balance_crypto)\n\n        # --- Journal to SQLite (synchronous, always succeeds or warns) ---\n        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()\n        _write_sqlite_journal(result, prompt_hash)\n\n        # --- Journal to PostgreSQL (async, best-effort) ---\n        await _write_postgres_journal(result, prompt_hash)\n\n        if not result.approved:\n            raise InsufficientEscrowError(\n                f"Preflight failed: wallet holds {result.wallet_balance:.8f} crypto but "\n                f"{result.required_crypto:.8f} is required for {result.required_tokens:,} tokens "\n                f"(deficit: {result.deficit:.8f} crypto).",\n                required=result.required_crypto,\n                escrowed=result.wallet_balance,\n                transaction_id=prompt_hash[:16],  # short reference for tracing\n            )\n\n        logger.info(\n            "Preflight APPROVED — tokens=%d  required_crypto=%.8f  balance=%.8f",\n            result.required_tokens,\n            result.required_crypto,\n            result.wallet_balance,\n        )\n        return result\n\n    # ------------------------------------------------------------------\n    # Public sync API (for tests and non-async callers)\n    # ------------------------------------------------------------------\n\n    def run_preflight_sync(\n        self,\n        prompt: str,\n        wallet_balance_crypto: float,\n    ) -> PreflightResult:\n        """\n        Synchronous wrapper around :meth:`run_preflight`.\n\n        Journals to SQLite only (PostgreSQL journal is skipped in sync\n        mode to avoid blocking the event loop).\n\n        Parameters\n        ----------\n        prompt:\n            Full prompt text.\n        wallet_balance_crypto:\n            Current wallet balance in crypto units.\n\n        Returns\n        -------\n        PreflightResult\n            Approved result.\n\n        Raises\n        ------\n        InsufficientEscrowError\n            When escrow is insufficient.\n        """\n        result = self._compute_result(prompt, wallet_balance_crypto)\n        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()\n        _write_sqlite_journal(result, prompt_hash)\n\n        if not result.approved:\n            raise InsufficientEscrowError(\n                f"Preflight failed: wallet holds {result.wallet_balance:.8f} crypto but "\n                f"{result.required_crypto:.8f} is required for {result.required_tokens:,} tokens "\n                f"(deficit: {result.deficit:.8f} crypto).",\n                required=result.required_crypto,\n                escrowed=result.wallet_balance,\n                transaction_id=prompt_hash[:16],\n            )\n\n        logger.info(\n            "Preflight APPROVED (sync) — tokens=%d  required_crypto=%.8f  balance=%.8f",\n            result.required_tokens,\n            result.required_crypto,\n            result.wallet_balance,\n        )\n        return result\n\n    # ------------------------------------------------------------------\n    # Private helpers\n    # ------------------------------------------------------------------\n\n    @staticmethod\n    def _compute_result(prompt: str, wallet_balance_crypto: float) -> PreflightResult:\n        """\n        Pure calculation — no I/O.  Builds the :class:`PreflightResult`\n        from first principles.\n\n... (file truncated, showing first 500 lines) ...
```
