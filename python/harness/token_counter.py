"""
token_counter.py — Tala Cognitive Engine Room: Token Counting Interceptor
=========================================================================
Every prompt MUST pass through this module before reaching vLLM.

Responsibilities
----------------
1. Count tokens using ``tiktoken`` (cl100k_base encoding as universal fallback).
2. Compute cost: ``cost_usd = token_count * TOKEN_COST_PER_TOKEN``
   (default: 0.0000001 USD per token — one ten-millionth).
3. Determine token mode based on system connection state:

   **Ghost Token mode** (local-only system)
     When the system connection mode is ``"local"`` (i.e. the operator chose
     "Stay Local" and has not connected to a VPS peer), ALL interactions use
     ghost tokens — an unlimited internal accounting unit that never touches
     the blockchain.  This gives a completely friction-free offline experience.
     The token_ledger still records every call for local audit purposes.

   **Real Token mode** (connected system)
     When the system connection mode is ``"vps"`` AND the inference call
     consumes a remote peer's resource (``resource_owner`` refers to a
     different wallet address), a signed payment transaction is submitted to
     the Tala PoA blockchain node running at ``TALA_CHAIN_BIND_HOST:PORT``.
     The counterparty receives the payment minus the configured house fee.
     If the real blockchain balance is insufficient an
     :class:`InsufficientTokenBalanceError` is raised.

     Local-origin calls (where ``resource_owner`` matches the operator's own
     wallet, or is absent) still use ghost tokens even in VPS mode — only
     cross-node resource consumption settles on-chain.

4. Raise :class:`InsufficientTokenBalanceError` if a real-token balance check
   fails (never raised in ghost-token mode).
5. Log every count to the ``token_ledger`` table (append-only accounting).
6. Return a :class:`TokenCheckResult` to the caller.

Milestone 1.2 — Task 1.2.3
"""

from __future__ import annotations

import asyncio
import logging
import time
import os
import json
import urllib.request
import uuid
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
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
    Raised when the wallet's real TalaCoin balance cannot cover the cost of
    a cross-node inference call.

    This error is never raised in ghost-token (local) mode — it only fires
    when the system is connected to a peer network and the requested resource
    is owned by a different wallet.

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
# Ghost token sentinel balance
# ---------------------------------------------------------------------------

# Any balance returned in ghost-token mode.  Effectively unlimited — the
# value is large enough to never trigger InsufficientTokenBalanceError while
# remaining representable as a normal float.
_GHOST_BALANCE: float = 999_999.0


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
        Computed cost in USD (or equivalent ghost-token units).
    balance_before:
        Wallet balance *before* deducting this cost.
        ``999999.0`` indicates ghost-token mode.
    balance_after:
        Wallet balance *after* deducting this cost (written to DB).
        Equal to ``balance_before`` in ghost-token mode.
    session_id:
        Caller-supplied session identifier.
    encoding:
        Name of the encoding used (e.g. ``'cl100k_base'`` or ``'estimate'``).
    elapsed_ms:
        Wall-clock time taken to complete the check, in milliseconds.
    ledger_id:
        Primary key of the inserted ``token_ledger`` row.
    ghost_mode:
        ``True`` when ghost tokens were used (local system or own-wallet call);
        ``False`` when a real blockchain payment was submitted.
    """

    prompt_tokens: int
    cost_usd: float
    balance_before: float
    balance_after: float
    session_id: str
    encoding: str
    elapsed_ms: float
    ledger_id: int = field(default=0)
    ghost_mode: bool = field(default=True)


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


async def _get_system_connection_mode() -> str:
    """
    Return the current system connection mode (``"local"`` or ``"vps"``).

    Reads from the ``client_settings`` PostgreSQL table (key
    ``"chain.connection"``).  Falls back to ``"local"`` on any error so that
    a DB outage never accidentally enables real-token billing.
    """
    try:
        from . import db
        row = await db.fetchrow(
            "SELECT value FROM client_settings WHERE key = 'chain.connection'"
        )
        if not row:
            return "local"
        value = row["value"]
        if isinstance(value, str):
            value = json.loads(value)
        return str(value.get("mode", "local")) if isinstance(value, dict) else "local"
    except Exception as exc:
        logger.debug("Could not read system connection mode: %s — defaulting to local", exc)
        return "local"


async def count_and_check(
    prompt: str,
    session_id: str,
    model: str = "",
    completion_tokens: int = 0,
    resource_owner: str | None = None,
) -> TokenCheckResult:
    """
    Count tokens in *prompt*, determine token mode, optionally deduct cost,
    and log to the immutable ``token_ledger``.

    This is the **mandatory gate** before any inference call.

    Token Mode Decision
    -------------------
    Ghost tokens are used when **either** of the following is true:

    * The system connection mode is ``"local"`` (operator chose Stay Local).
    * ``resource_owner`` is absent or matches the operator's own wallet
      address (consuming your own resources, even on a connected system).

    Real tokens (blockchain payment) are used **only** when:

    * The system connection mode is ``"vps"``, **AND**
    * ``resource_owner`` refers to a different peer's wallet address.

    Parameters
    ----------
    prompt:
        The raw text that will be sent to the model.
    session_id:
        Caller-supplied session or request identifier for ledger traceability.
    model:
        Model name to record in the ledger (informational only).
    completion_tokens:
        If known in advance, include for accurate cost accounting.
    resource_owner:
        Wallet address or ``@username`` of the peer whose resource is being
        consumed.  ``None`` or own wallet → ghost tokens.

    Returns
    -------
    TokenCheckResult

    Raises
    ------
    InsufficientTokenBalanceError
        Only in real-token mode, if the blockchain balance is insufficient.
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

    # -- 2. Determine token mode ------------------------------------------
    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")
    chain_port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")
    chain_host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")
    base_url = f"http://{chain_host}:{chain_port}"

    # Resolve connection mode first (defaults to local on any DB error).
    system_mode = await _get_system_connection_mode()
    system_is_local = (system_mode != "vps")

    # A call is "own resource" when resource_owner is absent, matches the
    # operator's wallet, or is a @-reference to the operator's own username.
    owner = (resource_owner or "").strip()
    own_wallet_refs = {wallet_address, f"@{wallet_address}", "", "local-client", "@local-tala"}
    resource_is_own = owner in own_wallet_refs or not owner

    # Ghost mode: local system OR consuming own resources.
    use_ghost_tokens = system_is_local or resource_is_own

    logger.debug(
        "Token mode: %s (system_mode=%s, resource_owner=%r)",
        "ghost" if use_ghost_tokens else "real",
        system_mode,
        resource_owner,
    )

    # -- 3. Check and optionally deduct balance ---------------------------
    cost_tokens = cost_usd
    loop = asyncio.get_running_loop()

    def _check_balance_sync() -> float:
        req = urllib.request.Request(f"{base_url}/chain/wallet/{wallet_address}/balance")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            return float(data.get("balance", 0.0))

    def _submit_payment_sync() -> dict:
        tx_id = str(uuid.uuid4())
        amount = cost_tokens

        counterparty = owner
        house_fee_percent = 0.0

        # Resolve @username → wallet address via chain peer directory
        if owner.startswith("@"):
            try:
                import urllib.parse
                peer_req = urllib.request.Request(
                    f"{base_url}/chain/peers/{urllib.parse.quote(owner)}"
                )
                with urllib.request.urlopen(peer_req) as response:
                    peer_data = json.loads(response.read().decode("utf-8"))
                    if peer_data.get("peer") and peer_data["peer"].get("wallet_address"):
                        counterparty = peer_data["peer"]["wallet_address"]
            except Exception as e:
                logger.warning("Failed to resolve peer %s: %s", owner, e)

        # Apply 10 % house cut for using another peer's resources
        if counterparty != wallet_address:
            house_fee_percent = 0.10

        raw_str = f"payment:{wallet_address}:{counterparty}:{amount}:{int(time.time() * 1000)}"
        tx_hash = hashlib.sha256(raw_str.encode()).hexdigest()

        from . import wallet as _wallet
        private_key = _wallet.get_private_key()
        signature_bytes = private_key.sign(tx_hash.encode("utf-8"))
        signature_hex = signature_bytes.hex()

        payload = {
            "id": tx_id,
            "type": "payment",
            "hash": tx_hash,
            "creator": wallet_address,
            "counterparty": counterparty,
            "metadata": {"amount": amount, "house_fee_percent": house_fee_percent},
            "signature": signature_hex,
            "public_key": wallet_address,
        }
        req = urllib.request.Request(
            f"{base_url}/chain/tx",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))

    if use_ghost_tokens:
        # Ghost mode: unlimited local balance, no blockchain call.
        balance_before = _GHOST_BALANCE
        balance_after = _GHOST_BALANCE
    else:
        # Real mode: read live blockchain balance.
        try:
            balance_before = await loop.run_in_executor(None, _check_balance_sync)
        except Exception as exc:
            logger.warning(
                "Failed to read blockchain balance — falling back to ghost tokens: %s", exc
            )
            balance_before = _GHOST_BALANCE
            balance_after = _GHOST_BALANCE
            use_ghost_tokens = True  # Degrade gracefully

        if not use_ghost_tokens:
            if balance_before < cost_tokens:
                raise InsufficientTokenBalanceError(
                    required_cost=cost_tokens,
                    current_balance=balance_before,
                    token_count=total_tokens,
                    session_id=session_id,
                )
            try:
                await loop.run_in_executor(None, _submit_payment_sync)
                balance_after = balance_before - cost_tokens
            except Exception as exc:
                logger.warning("Failed to submit blockchain payment: %s", exc)
                balance_after = balance_before

    # -- 4. Append to local ledger (immutable audit trail) ----------------
    async with db.transaction() as conn:
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
            cost_tokens,
            model or "",
        )
        ledger_id = ledger_row["id"] if ledger_row else 0

    # -- 5. Record which chain this belonged to ---------------------------
    # Until now the answer existed only in the logger.debug line above and in
    # the returned ghost_mode flag, neither of which is a record (UI-023). The
    # classification is passed explicitly: paying for your own resources on
    # your own machine is a payment that is not an external interaction, and
    # this is the only place that knows the difference.
    try:
        from python.harness import chains
        await chains.record(
            "payment",
            payload={
                "session_id": session_id,
                "tokens": total_tokens,
                "cost": cost_tokens,
                "model": model or "",
                "ledger_id": ledger_id,
            },
            counterparty="" if use_ghost_tokens else (resource_owner or ""),
            external=not use_ghost_tokens,
        )
    except Exception as exc:
        # The tokens have already been spent and the local ledger already
        # written; failing to note the chain must not undo that.
        logger.warning("Could not record the token payment on a chain: %s", exc)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    logger.info(
        "Token gate PASSED  session=%s tokens=%d cost=%.4f mode=%s "
        "balance=%.4f->%.4f  ledger_id=%d  elapsed=%.1fms",
        session_id, total_tokens, cost_tokens,
        "ghost" if use_ghost_tokens else "real",
        balance_before, balance_after,
        ledger_id, elapsed_ms,
    )

    return TokenCheckResult(
        prompt_tokens=token_count,
        cost_usd=cost_tokens,
        balance_before=balance_before,
        balance_after=balance_after,
        session_id=session_id,
        encoding=encoding_name,
        elapsed_ms=elapsed_ms,
        ledger_id=ledger_id,
        ghost_mode=use_ghost_tokens,
    )


async def get_wallet_balance() -> float:
    """
    Return the current TalaCoin balance from the local blockchain node.

    Returns ``999999.0`` (ghost balance) when the system is in local mode
    or when the blockchain node is unreachable.
    """
    system_mode = await _get_system_connection_mode()
    if system_mode != "vps":
        return _GHOST_BALANCE

    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")
    chain_port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")
    chain_host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")
    base_url = f"http://{chain_host}:{chain_port}"

    loop = asyncio.get_running_loop()

    def _check_balance_sync() -> float:
        req = urllib.request.Request(f"{base_url}/chain/wallet/{wallet_address}/balance")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            return float(data.get("balance", 0.0))

    try:
        return await loop.run_in_executor(None, _check_balance_sync)
    except Exception as exc:
        logger.warning("Failed to check blockchain balance: %s", exc)
        return _GHOST_BALANCE  # Ghost tokens as graceful fallback


async def wallet_state() -> dict[str, Any]:
    """
    The wallet balance, and which of three situations produced it.

    ``get_wallet_balance()`` returns the ghost sentinel both when tokens really
    are unlimited *and* when the chain could not be reached, so a caller cannot
    tell "you have infinite local tokens" from "nobody could ask". The status
    bar reported the first while the second was true. These are separated here:

    ``ghost``        local mode, tokens are unlimited and no chain is involved.
    ``real``         a balance read from the chain.
    ``unreachable``  the chain should have answered and did not. Not a balance.
    """
    mode = await _get_system_connection_mode()
    if mode != "vps":
        return {"balance": _GHOST_BALANCE, "mode": "ghost", "unlimited": True}

    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")
    chain_port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")
    chain_host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")
    base_url = f"http://{chain_host}:{chain_port}"
    loop = asyncio.get_running_loop()

    def _read_balance() -> float:
        req = urllib.request.Request(
            f"{base_url}/chain/wallet/{wallet_address}/balance")
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            return float(data.get("balance", 0.0))

    try:
        return {"balance": await loop.run_in_executor(None, _read_balance),
                "mode": "real", "unlimited": False}
    except Exception as exc:
        logger.warning("Could not read the wallet balance: %s", exc)
        return {"balance": None, "mode": "unreachable", "unlimited": False,
                "reason": f"{type(exc).__name__}: {exc}"}


async def top_up_wallet(amount: float, caller: str = "system") -> float:
    """
    Mint new TalaCoins on the blockchain (operator top-up).

    This calls the ``/chain/mint`` endpoint on the local blockchain node and
    updates the ``agent_log`` for auditing.  Returns ``0.0`` if the mint
    fails or the system is in local mode.
    """
    from .db import log_to_db

    system_mode = await _get_system_connection_mode()
    if system_mode != "vps":
        logger.info("top_up_wallet called in local mode — no-op (ghost tokens are unlimited)")
        return _GHOST_BALANCE

    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")
    chain_port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")
    chain_host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")
    base_url = f"http://{chain_host}:{chain_port}"

    loop = asyncio.get_running_loop()

    def _mint_sync() -> dict:
        payload = {"address": wallet_address, "amount": amount}
        secret = os.environ.get("TALA_CHAIN_ADMIN_SECRET", "")
        if not secret:
            secret_path = Path(__file__).resolve().parents[2] / "blockchain" / "data" / "admin_secret.txt"
            try:
                secret = secret_path.read_text(encoding="utf-8").strip()
            except OSError:
                secret = ""
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Tala-Chain-Secret"] = secret
        req = urllib.request.Request(
            f"{base_url}/chain/mint",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        result = await loop.run_in_executor(None, _mint_sync)
        new_balance = float(result.get("new_balance", 0.0))
        tx_id = result.get("tx_id") or result.get("tx_hash") or result.get("hash")

        await log_to_db(
            "INFO",
            f"Wallet topped up by {amount:.4f} by '{caller}' via Blockchain mint. "
            f"New balance: {new_balance:.4f}",
            {"caller": caller, "amount": amount, "new_balance": new_balance},
            tx_id=tx_id,
        )
        return new_balance
    except Exception as exc:
        logger.warning("Failed to mint tokens on blockchain: %s", exc)
        return 0.0
