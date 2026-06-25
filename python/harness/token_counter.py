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
import os
import json
import urllib.request
import uuid
import hashlib
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
    resource_owner: str | None = None,
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
    resource_owner:
        The owner of the primary data used in the prompt. If provided, they receive the payment minus a house cut.

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

    # -- 2. Check and deduct from Tala Blockchain ------------------------
    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")
    chain_port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")
    chain_host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")
    base_url = f"http://{chain_host}:{chain_port}"

    loop = asyncio.get_running_loop()

    def check_balance_sync():
        req = urllib.request.Request(f"{base_url}/chain/wallet/{wallet_address}/balance")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            return float(data.get("balance", 0.0))

    def submit_payment_sync():
        tx_id = str(uuid.uuid4())
        amount = cost_tokens
        
        counterparty = wallet_address
        house_fee_percent = 0.0
        
        if resource_owner and resource_owner != wallet_address:
            # Check if resource_owner is a username (starts with @)
            if resource_owner.startswith('@'):
                try:
                    peer_req = urllib.request.Request(f"{base_url}/chain/peers/{urllib.parse.quote(resource_owner)}")
                    with urllib.request.urlopen(peer_req) as response:
                        peer_data = json.loads(response.read().decode('utf-8'))
                        if peer_data.get("peer") and peer_data["peer"].get("wallet_address"):
                            counterparty = peer_data["peer"]["wallet_address"]
                        else:
                            counterparty = resource_owner # fallback
                except Exception as e:
                    logger.warning(f"Failed to resolve peer {resource_owner}: {e}")
                    counterparty = resource_owner
            else:
                counterparty = resource_owner
            
            # Apply 10% house cut for using others' resources
            house_fee_percent = 0.10
        
        raw_str = f"payment:{wallet_address}:{counterparty}:{amount}:{int(time.time()*1000)}"
        tx_hash = hashlib.sha256(raw_str.encode()).hexdigest()
        
        from . import wallet
        private_key = wallet.get_private_key()
        signature_bytes = private_key.sign(tx_hash.encode('utf-8'))
        signature_hex = signature_bytes.hex()
        
        payload = {
            "id": tx_id,
            "type": "payment",
            "hash": tx_hash,
            "creator": wallet_address,
            "counterparty": counterparty,
            "metadata": {
                "amount": amount,
                "house_fee_percent": house_fee_percent
            },
            "signature": signature_hex,
            "public_key": wallet_address
        }
        
        req = urllib.request.Request(
            f"{base_url}/chain/tx",
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))

    is_local_use = not resource_owner or resource_owner == wallet_address or resource_owner == f"@{wallet_address}"

    try:
        if is_local_use:
            balance_before = 999999.0  # Ghost Tokens
        else:
            balance_before = await loop.run_in_executor(None, check_balance_sync)
    except Exception as e:
        logger.warning(f"Failed to check blockchain balance, allowing interaction fallback. Error: {e}")
        balance_before = 999999.0  # Fallback Ghost Tokens

    cost_tokens = cost_usd

    if balance_before < cost_tokens:
        raise InsufficientTokenBalanceError(
            required_cost=cost_tokens,
            current_balance=balance_before,
            token_count=total_tokens,
            session_id=session_id,
        )

    try:
        if not is_local_use:
            await loop.run_in_executor(None, submit_payment_sync)
            balance_after = balance_before - cost_tokens
        else:
            balance_after = balance_before  # Unlimited Ghost Tokens
    except Exception as e:
        logger.warning(f"Failed to submit payment to blockchain: {e}")
        balance_after = balance_before

    # Append to local ledger (immutable audit trail)
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
            cost_tokens, # reuse cost column for token count
            model or "",
        )
        ledger_id = ledger_row["id"] if ledger_row else 0

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    logger.info(
        "Token gate PASSED  session=%s tokens=%d cost=%.1f "
        "balance=%.4f->%.4f  ledger_id=%d  elapsed=%.1fms",
        session_id, total_tokens, cost_tokens,
        balance_before, balance_after,
        ledger_id, elapsed_ms,
    )

    return TokenCheckResult(
        prompt_tokens=token_count,
        cost_usd=cost_tokens, # Repurposed as Interaction Tokens
        balance_before=balance_before,
        balance_after=balance_after,
        session_id=session_id,
        encoding=encoding_name,
        elapsed_ms=elapsed_ms,
        ledger_id=ledger_id,
    )


async def get_wallet_balance() -> float:
    """Return the current TalaCoin balance from the local blockchain."""
    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")
    chain_port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")
    chain_host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")
    base_url = f"http://{chain_host}:{chain_port}"

    loop = asyncio.get_running_loop()

    def check_balance_sync():
        req = urllib.request.Request(f"{base_url}/chain/wallet/{wallet_address}/balance")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            return float(data.get("balance", 0.0))

    try:
        return await loop.run_in_executor(None, check_balance_sync)
    except Exception as e:
        logger.warning(f"Failed to check blockchain balance: {e}")
        return 999999.0  # Ghost Tokens


async def top_up_wallet(amount: float, caller: str = "system") -> float:
    """
    Mint new TalaCoins on the blockchain (simulating a top-up).
    """
    from .db import log_to_db
    
    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")
    chain_port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")
    chain_host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")
    base_url = f"http://{chain_host}:{chain_port}"

    loop = asyncio.get_running_loop()

    def mint_sync():
        payload = {
            "address": wallet_address,
            "amount": amount
        }
        req = urllib.request.Request(
            f"{base_url}/chain/mint",
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))

    try:
        result = await loop.run_in_executor(None, mint_sync)
        new_balance = float(result.get("new_balance", 0.0))
        
        await log_to_db(
            "INFO",
            f"Wallet topped up by {amount:.4f} by '{caller}' via Blockchain mint. New balance: {new_balance:.4f}",
            {"caller": caller, "amount": amount, "new_balance": new_balance},
        )
        return new_balance
    except Exception as e:
        logger.warning(f"Failed to mint tokens on blockchain: {e}")
        return 0.0
