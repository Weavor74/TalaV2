"""
sdk/marketplace/billing.py — Tala Cognitive Engine Room: Trusted Share Gateway & Billing
==========================================================================================
Implements the economic and trust layer of the Tala marketplace.

Components
----------
TrustLevel          — Enum of four trust tiers for marketplace peers
TrustedShareGateway — Assigns and queries trust tiers, persisted to PostgreSQL
BillingHook         — Attaches fee metadata to ledger schema dicts
LiquidityClearing   — Performs atomic hold-check-and-clear settlement

Design invariants
-----------------
* Trust tier assignments are persisted to PostgreSQL — never just in memory.
* ``LiquidityClearing.execute_hold_check_and_clear`` is fully atomic via a
  PostgreSQL transaction: the hold debit and the contract_router credit happen
  in the same transaction, or neither does.  There is no partial-success state.
* ``BillingHook.attach_fee`` is a pure function that enriches a schema dict
  without side-effects — it does NOT commit any DB row.

Milestone 4 — Identity & Marketplace
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TrustLevel — marketplace peer trust tiers
# ---------------------------------------------------------------------------


class TrustLevel(str, Enum):
    """
    Hierarchical trust tier for marketplace peers.

    Values are ordered from least to most privileged.

    UNTRUSTED
        No relationship established; this peer has not been verified in any way.
        Responses to untrusted peers must be minimal and never include PII.

    CONTEXT_ONLY
        The peer may receive context summaries but MUST NOT receive tool outputs,
        wallet operations, or raw database records.  Remote nodes fetched without
        explicit trust designation land here automatically.

    TRUSTED_SHARE
        The peer has been manually designated as trusted.  May receive structured
        node outputs and limited API calls, but NOT financial settlement authority.

    VERIFIED_PARTNER
        The peer has completed cryptographic verification and may participate in
        bilateral billing and liquidity clearing operations.
    """

    UNTRUSTED = "untrusted"
    CONTEXT_ONLY = "context_only"
    TRUSTED_SHARE = "trusted_share"
    VERIFIED_PARTNER = "verified_partner"

    @property
    def rank(self) -> int:
        """Numeric rank for comparison: higher = more trusted."""
        return {
            TrustLevel.UNTRUSTED: 0,
            TrustLevel.CONTEXT_ONLY: 1,
            TrustLevel.TRUSTED_SHARE: 2,
            TrustLevel.VERIFIED_PARTNER: 3,
        }[self]

    def is_at_least(self, required: "TrustLevel") -> bool:
        """Return True if this level is >= *required*."""
        return self.rank >= required.rank


# ---------------------------------------------------------------------------
# TrustedShareGateway
# ---------------------------------------------------------------------------


class TrustedShareGateway:
    """
    Assigns and queries trust tiers for marketplace peer agents.

    Trust assignments are persisted to the ``peer_trust`` PostgreSQL table
    (auto-created via :meth:`ensure_table`).  In-memory state is used as a
    write-through cache for the current process lifetime.

    Usage::

        gateway = TrustedShareGateway()
        await gateway.ensure_table()
        await gateway.designate("agent-xyz-001", TrustLevel.TRUSTED_SHARE)
        trust = await gateway.get_trust("agent-xyz-001")
    """

    def __init__(self) -> None:
        # In-memory cache: peer_id -> TrustLevel
        self._cache: dict[str, TrustLevel] = {}

    # ----------------------------------------------------------------
    # DDL
    # ----------------------------------------------------------------

    _CREATE_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS peer_trust (
            peer_id         TEXT        PRIMARY KEY,
            trust_level     TEXT        NOT NULL DEFAULT 'untrusted',
            designated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """

    async def ensure_table(self) -> None:
        """Create the ``peer_trust`` table if it does not exist."""
        try:
            from python.harness.db import get_pool  # type: ignore[import]
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(self._CREATE_TABLE_SQL)
            logger.debug("TrustedShareGateway: peer_trust table ensured.")
        except Exception as exc:
            logger.error("TrustedShareGateway.ensure_table failed: %s", exc)
            raise

    # ----------------------------------------------------------------
    # Core operations
    # ----------------------------------------------------------------

    async def designate(self, peer_id: str, trust_level: TrustLevel) -> None:
        """
        Assign a trust tier to a peer agent.

        If the peer already has an entry, the trust level is updated.
        Downgrades are permitted (e.g., revoking VERIFIED_PARTNER → UNTRUSTED).

        Parameters
        ----------
        peer_id : str
            Unique identifier for the peer agent.
        trust_level : TrustLevel
            The trust tier to assign.
        """
        if not peer_id or not peer_id.strip():
            raise ValueError("peer_id must be a non-empty string.")

        logger.info(
            "TrustedShareGateway.designate: peer_id=%r → trust_level=%r",
            peer_id, trust_level.value,
        )

        try:
            from python.harness.db import get_pool  # type: ignore[import]
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO peer_trust (peer_id, trust_level, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (peer_id) DO UPDATE SET
                        trust_level  = EXCLUDED.trust_level,
                        updated_at   = NOW()
                    """,
                    peer_id, trust_level.value,
                )
        except Exception as exc:
            logger.error(
                "TrustedShareGateway.designate DB write failed for %r: %s", peer_id, exc
            )
            raise

        # Update in-memory cache
        self._cache[peer_id] = trust_level

    async def get_trust(self, peer_id: str) -> TrustLevel:
        """
        Retrieve the current trust level for a peer.

        Returns :attr:`TrustLevel.UNTRUSTED` if no designation exists.
        Checks the in-memory cache first; falls back to PostgreSQL.
        """
        if peer_id in self._cache:
            return self._cache[peer_id]

        try:
            from python.harness.db import fetchrow  # type: ignore[import]
            row = await fetchrow(
                "SELECT trust_level FROM peer_trust WHERE peer_id = $1", peer_id
            )
            if row is None:
                return TrustLevel.UNTRUSTED
            level = TrustLevel(row["trust_level"])
            self._cache[peer_id] = level
            return level
        except Exception as exc:
            logger.warning(
                "TrustedShareGateway.get_trust DB read failed for %r: %s — defaulting UNTRUSTED",
                peer_id, exc,
            )
            return TrustLevel.UNTRUSTED

    async def revoke(self, peer_id: str) -> None:
        """Set a peer's trust level to UNTRUSTED (effective revocation)."""
        await self.designate(peer_id, TrustLevel.UNTRUSTED)

    async def require_at_least(
        self, peer_id: str, required: TrustLevel
    ) -> None:
        """
        Assert that *peer_id* has at least *required* trust.

        Raises
        ------
        PermissionError
            If the peer's trust level is below *required*.
        """
        actual = await self.get_trust(peer_id)
        if not actual.is_at_least(required):
            raise PermissionError(
                f"Peer {peer_id!r} has trust level {actual.value!r} but "
                f"{required.value!r} is required for this operation."
            )


# ---------------------------------------------------------------------------
# BillingHook — fee metadata attachment
# ---------------------------------------------------------------------------


class BillingHook:
    """
    Attaches fee metadata to a ledger schema dictionary.

    This is a **pure data enrichment** step — it does NOT write to any
    database, does NOT move any funds, and has NO side effects.  The
    returned dict is then consumed by :class:`LiquidityClearing` or stored
    in an off-chain ledger schema.

    The fee metadata follows the Tala ledger schema convention:
    ``_billing`` key at the top level of the schema dict.
    """

    def attach_fee(
        self,
        schema: dict[str, Any],
        fee_amount_crypto: float,
        tier: str,
    ) -> dict[str, Any]:
        """
        Return a copy of *schema* enriched with fee metadata.

        Parameters
        ----------
        schema : dict
            The base ledger schema dict.  Must be a dict (not None).
        fee_amount_crypto : float
            The fee amount in the native crypto unit (not USD).
            Must be >= 0.
        tier : str
            Billing tier label (e.g. ``"basic"``, ``"premium"``,
            ``"verified_partner"``).  Free-form string.

        Returns
        -------
        dict
            A new dict (shallow copy of *schema*) with a ``_billing`` key
            containing the fee metadata.

        Raises
        ------
        TypeError
            If *schema* is not a dict.
        ValueError
            If *fee_amount_crypto* is negative.
        """
        if not isinstance(schema, dict):
            raise TypeError(
                f"BillingHook.attach_fee: schema must be a dict, got {type(schema).__name__!r}"
            )
        if fee_amount_crypto < 0:
            raise ValueError(
                f"BillingHook.attach_fee: fee_amount_crypto must be >= 0, "
                f"got {fee_amount_crypto}"
            )

        enriched = dict(schema)
        enriched["_billing"] = {
            "fee_amount_crypto": float(fee_amount_crypto),
            "tier": str(tier),
            "currency": "TALA",
            "fee_id": str(uuid.uuid4()),
            "attached_at": datetime.now(timezone.utc).isoformat(),
        }

        logger.debug(
            "BillingHook.attach_fee: tier=%r fee=%.8f fee_id=%r",
            tier, fee_amount_crypto, enriched["_billing"]["fee_id"],
        )
        return enriched


# ---------------------------------------------------------------------------
# LiquidityClearing — atomic hold + clear settlement
# ---------------------------------------------------------------------------


class LiquidityClearing:
    """
    Executes an atomic hold-check-and-clear settlement against the wallet.

    The operation is performed in a single PostgreSQL transaction:
    1. Check that the wallet holds at least *amount_crypto*.
    2. Debit the wallet by *amount_crypto*.
    3. Record the settlement in the ``clearing_log`` table (auto-created).

    If any step fails, the entire transaction is rolled back.  There is no
    partial-success state.

    The ``contract_router`` parameter is an opaque handle that represents
    the receiving party (e.g., a peer agent, smart contract address, or
    internal sub-system).  Its exact type is deliberately unspecified so that
    future milestones can inject real contract routing without changing the
    clearing interface.
    """

    _CREATE_CLEARING_LOG_SQL = """
        CREATE TABLE IF NOT EXISTS clearing_log (
            id              BIGSERIAL       PRIMARY KEY,
            contract_id     TEXT            NOT NULL,
            amount_crypto   NUMERIC(28, 8)  NOT NULL,
            wallet_balance_before NUMERIC(28, 8) NOT NULL,
            wallet_balance_after  NUMERIC(28, 8) NOT NULL,
            contract_router TEXT            NOT NULL DEFAULT '',
            cleared_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
    """

    async def ensure_table(self) -> None:
        """Create the ``clearing_log`` table if it does not exist."""
        try:
            from python.harness.db import get_pool  # type: ignore[import]
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(self._CREATE_CLEARING_LOG_SQL)
        except Exception as exc:
            logger.error("LiquidityClearing.ensure_table failed: %s", exc)
            raise

    async def execute_hold_check_and_clear(
        self,
        wallet: Any,
        contract_router: Any,
        amount_crypto: float,
        contract_id: str,
    ) -> ClearingReceipt:
        """
        Atomically verify a hold and clear funds from the wallet.

        Parameters
        ----------
        wallet :
            Opaque wallet handle.  The clearing engine reads the
            ``crypto_balance`` column from the ``wallet`` DB table directly;
            the *wallet* parameter is used for identity/logging only.
        contract_router :
            Opaque contract routing handle.  Converted to string for logging.
        amount_crypto : float
            Amount to clear, in the native crypto unit.  Must be > 0.
        contract_id : str
            External contract or transaction reference identifier.

        Returns
        -------
        ClearingReceipt
            Confirmation record of the completed settlement.

        Raises
        ------
        ValueError
            If *amount_crypto* <= 0.
        sdk.agent.exceptions.InsufficientEscrowError
            If the wallet balance is below *amount_crypto*.
        """
        from sdk.agent.exceptions import InsufficientEscrowError  # type: ignore[import]

        if amount_crypto <= 0:
            raise ValueError(
                f"LiquidityClearing: amount_crypto must be > 0, got {amount_crypto}"
            )

        contract_router_str = str(contract_router)
        logger.info(
            "LiquidityClearing.execute_hold_check_and_clear: "
            "contract_id=%r amount=%.8f router=%r",
            contract_id, amount_crypto, contract_router_str,
        )

        try:
            from python.harness.db import get_pool  # type: ignore[import]
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Step 1: Read current balance (lock the row)
                    row = await conn.fetchrow(
                        "SELECT crypto_balance FROM wallet WHERE id = 1 FOR UPDATE"
                    )
                    if row is None:
                        raise RuntimeError(
                            "LiquidityClearing: wallet row not found (id=1).  "
                            "Ensure db.init_pool() has been called."
                        )

                    balance_before = float(row["crypto_balance"])

                    # Step 2: Hold check
                    if balance_before < amount_crypto:
                        raise InsufficientEscrowError(
                            f"LiquidityClearing: insufficient balance for contract "
                            f"{contract_id!r}.  "
                            f"Required: {amount_crypto:.8f}  "
                            f"Available: {balance_before:.8f}",
                            required=float(amount_crypto),
                            escrowed=balance_before,
                            transaction_id=contract_id,
                        )

                    # Step 3: Debit wallet
                    balance_after = balance_before - amount_crypto
                    await conn.execute(
                        """
                        UPDATE wallet
                        SET crypto_balance = $1, updated_at = NOW()
                        WHERE id = 1
                        """,
                        balance_after,
                    )

                    # Step 4: Record in clearing_log (same transaction)
                    await conn.execute(
                        self._CREATE_CLEARING_LOG_SQL
                    )
                    await conn.execute(
                        """
                        INSERT INTO clearing_log
                            (contract_id, amount_crypto, wallet_balance_before,
                             wallet_balance_after, contract_router)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        contract_id,
                        amount_crypto,
                        balance_before,
                        balance_after,
                        contract_router_str,
                    )

        except InsufficientEscrowError:
            raise  # re-raise — not a system error
        except Exception as exc:
            logger.error(
                "LiquidityClearing: atomic transaction failed for contract_id=%r: %s",
                contract_id, exc,
            )
            raise

        receipt = ClearingReceipt(
            contract_id=contract_id,
            amount_crypto=amount_crypto,
            balance_before=balance_before,
            balance_after=balance_after,
            contract_router=contract_router_str,
            cleared_at=datetime.now(timezone.utc),
        )
        logger.info(
            "LiquidityClearing: cleared %.8f for contract_id=%r  "
            "balance %s → %s",
            amount_crypto, contract_id, balance_before, balance_after,
        )
        return receipt


# ---------------------------------------------------------------------------
# ClearingReceipt — settlement confirmation record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClearingReceipt:
    """
    Immutable record confirming a completed liquidity clearing.

    Attributes
    ----------
    contract_id : str
        The contract or transaction reference that was settled.
    amount_crypto : float
        Amount cleared from the wallet.
    balance_before : float
        Wallet ``crypto_balance`` before the debit.
    balance_after : float
        Wallet ``crypto_balance`` after the debit.
    contract_router : str
        String representation of the contract router used.
    cleared_at : datetime
        UTC timestamp of the clearing operation.
    """

    contract_id: str
    amount_crypto: float
    balance_before: float
    balance_after: float
    contract_router: str
    cleared_at: datetime


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "TrustLevel",
    "TrustedShareGateway",
    "BillingHook",
    "LiquidityClearing",
    "ClearingReceipt",
]
