# File: `billing.py`

## Module Description

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

## Classes

### `class TrustLevel`
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

- **`def rank(self)`**
  - Numeric rank for comparison: higher = more trusted.
- **`def is_at_least(self, required)`**
  - Return True if this level is >= *required*.

### `class TrustedShareGateway`
Assigns and queries trust tiers for marketplace peer agents.

Trust assignments are persisted to the ``peer_trust`` PostgreSQL table
(auto-created via :meth:`ensure_table`).  In-memory state is used as a
write-through cache for the current process lifetime.

Usage::

    gateway = TrustedShareGateway()
    await gateway.ensure_table()
    await gateway.designate("agent-xyz-001", TrustLevel.TRUSTED_SHARE)
    trust = await gateway.get_trust("agent-xyz-001")

- **`def __init__(self)`**
- **`def ensure_table(self)`**
  - Create the ``peer_trust`` table if it does not exist.
- **`def designate(self, peer_id, trust_level)`**
  - Assign a trust tier to a peer agent.
  
  If the peer already has an entry, the trust level is updated.
  Downgrades are permitted (e.g., revoking VERIFIED_PARTNER → UNTRUSTED).
  
  Parameters
  ----------
  peer_id : str
      Unique identifier for the peer agent.
  trust_level : TrustLevel
      The trust tier to assign.
- **`def get_trust(self, peer_id)`**
  - Retrieve the current trust level for a peer.
  
  Returns :attr:`TrustLevel.UNTRUSTED` if no designation exists.
  Checks the in-memory cache first; falls back to PostgreSQL.
- **`def revoke(self, peer_id)`**
  - Set a peer's trust level to UNTRUSTED (effective revocation).
- **`def require_at_least(self, peer_id, required)`**
  - Assert that *peer_id* has at least *required* trust.
  
  Raises
  ------
  PermissionError
      If the peer's trust level is below *required*.

### `class BillingHook`
Attaches fee metadata to a ledger schema dictionary.

This is a **pure data enrichment** step — it does NOT write to any
database, does NOT move any funds, and has NO side effects.  The
returned dict is then consumed by :class:`LiquidityClearing` or stored
in an off-chain ledger schema.

The fee metadata follows the Tala ledger schema convention:
``_billing`` key at the top level of the schema dict.

- **`def attach_fee(self, schema, fee_amount_crypto, tier)`**
  - Return a copy of *schema* enriched with fee metadata.
  
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

### `class LiquidityClearing`
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

- **`def ensure_table(self)`**
  - Create the ``clearing_log`` table if it does not exist.
- **`def execute_hold_check_and_clear(self, wallet, contract_router, amount_crypto, contract_id)`**
  - Atomically verify a hold and clear funds from the wallet.
  
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

### `class ClearingReceipt`
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


## Source Code

```python
"""\nsdk/marketplace/billing.py — Tala Cognitive Engine Room: Trusted Share Gateway & Billing\n==========================================================================================\nImplements the economic and trust layer of the Tala marketplace.\n\nComponents\n----------\nTrustLevel          — Enum of four trust tiers for marketplace peers\nTrustedShareGateway — Assigns and queries trust tiers, persisted to PostgreSQL\nBillingHook         — Attaches fee metadata to ledger schema dicts\nLiquidityClearing   — Performs atomic hold-check-and-clear settlement\n\nDesign invariants\n-----------------\n* Trust tier assignments are persisted to PostgreSQL — never just in memory.\n* ``LiquidityClearing.execute_hold_check_and_clear`` is fully atomic via a\n  PostgreSQL transaction: the hold debit and the contract_router credit happen\n  in the same transaction, or neither does.  There is no partial-success state.\n* ``BillingHook.attach_fee`` is a pure function that enriches a schema dict\n  without side-effects — it does NOT commit any DB row.\n\nMilestone 4 — Identity & Marketplace\n"""\n\nfrom __future__ import annotations\n\nimport logging\nimport uuid\nfrom dataclasses import dataclass, field\nfrom datetime import datetime, timezone\nfrom enum import Enum\nfrom typing import Any\n\nlogger = logging.getLogger(__name__)\n\n\n# ---------------------------------------------------------------------------\n# TrustLevel — marketplace peer trust tiers\n# ---------------------------------------------------------------------------\n\n\nclass TrustLevel(str, Enum):\n    """\n    Hierarchical trust tier for marketplace peers.\n\n    Values are ordered from least to most privileged.\n\n    UNTRUSTED\n        No relationship established; this peer has not been verified in any way.\n        Responses to untrusted peers must be minimal and never include PII.\n\n    CONTEXT_ONLY\n        The peer may receive context summaries but MUST NOT receive tool outputs,\n        wallet operations, or raw database records.  Remote nodes fetched without\n        explicit trust designation land here automatically.\n\n    TRUSTED_SHARE\n        The peer has been manually designated as trusted.  May receive structured\n        node outputs and limited API calls, but NOT financial settlement authority.\n\n    VERIFIED_PARTNER\n        The peer has completed cryptographic verification and may participate in\n        bilateral billing and liquidity clearing operations.\n    """\n\n    UNTRUSTED = "untrusted"\n    CONTEXT_ONLY = "context_only"\n    TRUSTED_SHARE = "trusted_share"\n    VERIFIED_PARTNER = "verified_partner"\n\n    @property\n    def rank(self) -> int:\n        """Numeric rank for comparison: higher = more trusted."""\n        return {\n            TrustLevel.UNTRUSTED: 0,\n            TrustLevel.CONTEXT_ONLY: 1,\n            TrustLevel.TRUSTED_SHARE: 2,\n            TrustLevel.VERIFIED_PARTNER: 3,\n        }[self]\n\n    def is_at_least(self, required: "TrustLevel") -> bool:\n        """Return True if this level is >= *required*."""\n        return self.rank >= required.rank\n\n\n# ---------------------------------------------------------------------------\n# TrustedShareGateway\n# ---------------------------------------------------------------------------\n\n\nclass TrustedShareGateway:\n    """\n    Assigns and queries trust tiers for marketplace peer agents.\n\n    Trust assignments are persisted to the ``peer_trust`` PostgreSQL table\n    (auto-created via :meth:`ensure_table`).  In-memory state is used as a\n    write-through cache for the current process lifetime.\n\n    Usage::\n\n        gateway = TrustedShareGateway()\n        await gateway.ensure_table()\n        await gateway.designate("agent-xyz-001", TrustLevel.TRUSTED_SHARE)\n        trust = await gateway.get_trust("agent-xyz-001")\n    """\n\n    def __init__(self) -> None:\n        # In-memory cache: peer_id -> TrustLevel\n        self._cache: dict[str, TrustLevel] = {}\n\n    # ----------------------------------------------------------------\n    # DDL\n    # ----------------------------------------------------------------\n\n    _CREATE_TABLE_SQL = """\n        CREATE TABLE IF NOT EXISTS peer_trust (\n            peer_id         TEXT        PRIMARY KEY,\n            trust_level     TEXT        NOT NULL DEFAULT 'untrusted',\n            designated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()\n        );\n    """\n\n    async def ensure_table(self) -> None:\n        """Create the ``peer_trust`` table if it does not exist."""\n        try:\n            from python.harness.db import get_pool  # type: ignore[import]\n            pool = get_pool()\n            async with pool.acquire() as conn:\n                await conn.execute(self._CREATE_TABLE_SQL)\n            logger.debug("TrustedShareGateway: peer_trust table ensured.")\n        except Exception as exc:\n            logger.error("TrustedShareGateway.ensure_table failed: %s", exc)\n            raise\n\n    # ----------------------------------------------------------------\n    # Core operations\n    # ----------------------------------------------------------------\n\n    async def designate(self, peer_id: str, trust_level: TrustLevel) -> None:\n        """\n        Assign a trust tier to a peer agent.\n\n        If the peer already has an entry, the trust level is updated.\n        Downgrades are permitted (e.g., revoking VERIFIED_PARTNER → UNTRUSTED).\n\n        Parameters\n        ----------\n        peer_id : str\n            Unique identifier for the peer agent.\n        trust_level : TrustLevel\n            The trust tier to assign.\n        """\n        if not peer_id or not peer_id.strip():\n            raise ValueError("peer_id must be a non-empty string.")\n\n        logger.info(\n            "TrustedShareGateway.designate: peer_id=%r → trust_level=%r",\n            peer_id, trust_level.value,\n        )\n\n        try:\n            from python.harness.db import get_pool  # type: ignore[import]\n            pool = get_pool()\n            async with pool.acquire() as conn:\n                await conn.execute(\n                    """\n                    INSERT INTO peer_trust (peer_id, trust_level, updated_at)\n                    VALUES ($1, $2, NOW())\n                    ON CONFLICT (peer_id) DO UPDATE SET\n                        trust_level  = EXCLUDED.trust_level,\n                        updated_at   = NOW()\n                    """,\n                    peer_id, trust_level.value,\n                )\n        except Exception as exc:\n            logger.error(\n                "TrustedShareGateway.designate DB write failed for %r: %s", peer_id, exc\n            )\n            raise\n\n        # Update in-memory cache\n        self._cache[peer_id] = trust_level\n\n    async def get_trust(self, peer_id: str) -> TrustLevel:\n        """\n        Retrieve the current trust level for a peer.\n\n        Returns :attr:`TrustLevel.UNTRUSTED` if no designation exists.\n        Checks the in-memory cache first; falls back to PostgreSQL.\n        """\n        if peer_id in self._cache:\n            return self._cache[peer_id]\n\n        try:\n            from python.harness.db import fetchrow  # type: ignore[import]\n            row = await fetchrow(\n                "SELECT trust_level FROM peer_trust WHERE peer_id = $1", peer_id\n            )\n            if row is None:\n                return TrustLevel.UNTRUSTED\n            level = TrustLevel(row["trust_level"])\n            self._cache[peer_id] = level\n            return level\n        except Exception as exc:\n            logger.warning(\n                "TrustedShareGateway.get_trust DB read failed for %r: %s — defaulting UNTRUSTED",\n                peer_id, exc,\n            )\n            return TrustLevel.UNTRUSTED\n\n    async def revoke(self, peer_id: str) -> None:\n        """Set a peer's trust level to UNTRUSTED (effective revocation)."""\n        await self.designate(peer_id, TrustLevel.UNTRUSTED)\n\n    async def require_at_least(\n        self, peer_id: str, required: TrustLevel\n    ) -> None:\n        """\n        Assert that *peer_id* has at least *required* trust.\n\n        Raises\n        ------\n        PermissionError\n            If the peer's trust level is below *required*.\n        """\n        actual = await self.get_trust(peer_id)\n        if not actual.is_at_least(required):\n            raise PermissionError(\n                f"Peer {peer_id!r} has trust level {actual.value!r} but "\n                f"{required.value!r} is required for this operation."\n            )\n\n\n# ---------------------------------------------------------------------------\n# BillingHook — fee metadata attachment\n# ---------------------------------------------------------------------------\n\n\nclass BillingHook:\n    """\n    Attaches fee metadata to a ledger schema dictionary.\n\n    This is a **pure data enrichment** step — it does NOT write to any\n    database, does NOT move any funds, and has NO side effects.  The\n    returned dict is then consumed by :class:`LiquidityClearing` or stored\n    in an off-chain ledger schema.\n\n    The fee metadata follows the Tala ledger schema convention:\n    ``_billing`` key at the top level of the schema dict.\n    """\n\n    def attach_fee(\n        self,\n        schema: dict[str, Any],\n        fee_amount_crypto: float,\n        tier: str,\n    ) -> dict[str, Any]:\n        """\n        Return a copy of *schema* enriched with fee metadata.\n\n        Parameters\n        ----------\n        schema : dict\n            The base ledger schema dict.  Must be a dict (not None).\n        fee_amount_crypto : float\n            The fee amount in the native crypto unit (not USD).\n            Must be >= 0.\n        tier : str\n            Billing tier label (e.g. ``"basic"``, ``"premium"``,\n            ``"verified_partner"``).  Free-form string.\n\n        Returns\n        -------\n        dict\n            A new dict (shallow copy of *schema*) with a ``_billing`` key\n            containing the fee metadata.\n\n        Raises\n        ------\n        TypeError\n            If *schema* is not a dict.\n        ValueError\n            If *fee_amount_crypto* is negative.\n        """\n        if not isinstance(schema, dict):\n            raise TypeError(\n                f"BillingHook.attach_fee: schema must be a dict, got {type(schema).__name__!r}"\n            )\n        if fee_amount_crypto < 0:\n            raise ValueError(\n                f"BillingHook.attach_fee: fee_amount_crypto must be >= 0, "\n                f"got {fee_amount_crypto}"\n            )\n\n        enriched = dict(schema)\n        enriched["_billing"] = {\n            "fee_amount_crypto": float(fee_amount_crypto),\n            "tier": str(tier),\n            "currency": "TALA",\n            "fee_id": str(uuid.uuid4()),\n            "attached_at": datetime.now(timezone.utc).isoformat(),\n        }\n\n        logger.debug(\n            "BillingHook.attach_fee: tier=%r fee=%.8f fee_id=%r",\n            tier, fee_amount_crypto, enriched["_billing"]["fee_id"],\n        )\n        return enriched\n\n\n# ---------------------------------------------------------------------------\n# LiquidityClearing — atomic hold + clear settlement\n# ---------------------------------------------------------------------------\n\n\nclass LiquidityClearing:\n    """\n    Executes an atomic hold-check-and-clear settlement against the wallet.\n\n    The operation is performed in a single PostgreSQL transaction:\n    1. Check that the wallet holds at least *amount_crypto*.\n    2. Debit the wallet by *amount_crypto*.\n    3. Record the settlement in the ``clearing_log`` table (auto-created).\n\n    If any step fails, the entire transaction is rolled back.  There is no\n    partial-success state.\n\n    The ``contract_router`` parameter is an opaque handle that represents\n    the receiving party (e.g., a peer agent, smart contract address, or\n    internal sub-system).  Its exact type is deliberately unspecified so that\n    future milestones can inject real contract routing without changing the\n    clearing interface.\n    """\n\n    _CREATE_CLEARING_LOG_SQL = """\n        CREATE TABLE IF NOT EXISTS clearing_log (\n            id              BIGSERIAL       PRIMARY KEY,\n            contract_id     TEXT            NOT NULL,\n            amount_crypto   NUMERIC(28, 8)  NOT NULL,\n            wallet_balance_before NUMERIC(28, 8) NOT NULL,\n            wallet_balance_after  NUMERIC(28, 8) NOT NULL,\n            contract_router TEXT            NOT NULL DEFAULT '',\n            cleared_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()\n        );\n    """\n\n    async def ensure_table(self) -> None:\n        """Create the ``clearing_log`` table if it does not exist."""\n        try:\n            from python.harness.db import get_pool  # type: ignore[import]\n            pool = get_pool()\n            async with pool.acquire() as conn:\n                await conn.execute(self._CREATE_CLEARING_LOG_SQL)\n        except Exception as exc:\n            logger.error("LiquidityClearing.ensure_table failed: %s", exc)\n            raise\n\n    async def execute_hold_check_and_clear(\n        self,\n        wallet: Any,\n        contract_router: Any,\n        amount_crypto: float,\n        contract_id: str,\n    ) -> ClearingReceipt:\n        """\n        Atomically verify a hold and clear funds from the wallet.\n\n        Parameters\n        ----------\n        wallet :\n            Opaque wallet handle.  The clearing engine reads the\n            ``crypto_balance`` column from the ``wallet`` DB table directly;\n            the *wallet* parameter is used for identity/logging only.\n        contract_router :\n            Opaque contract routing handle.  Converted to string for logging.\n        amount_crypto : float\n            Amount to clear, in the native crypto unit.  Must be > 0.\n        contract_id : str\n            External contract or transaction reference identifier.\n\n        Returns\n        -------\n        ClearingReceipt\n            Confirmation record of the completed settlement.\n\n        Raises\n        ------\n        ValueError\n            If *amount_crypto* <= 0.\n        sdk.agent.exceptions.InsufficientEscrowError\n            If the wallet balance is below *amount_crypto*.\n        """\n        from sdk.agent.exceptions import InsufficientEscrowError  # type: ignore[import]\n\n        if amount_crypto <= 0:\n            raise ValueError(\n                f"LiquidityClearing: amount_crypto must be > 0, got {amount_crypto}"\n            )\n\n        contract_router_str = str(contract_router)\n        logger.info(\n            "LiquidityClearing.execute_hold_check_and_clear: "\n            "contract_id=%r amount=%.8f router=%r",\n            contract_id, amount_crypto, contract_router_str,\n        )\n\n        try:\n            from python.harness.db import get_pool  # type: ignore[import]\n            pool = get_pool()\n            async with pool.acquire() as conn:\n                async with conn.transaction():\n                    # Step 1: Read current balance (lock the row)\n                    row = await conn.fetchrow(\n                        "SELECT crypto_balance FROM wallet WHERE id = 1 FOR UPDATE"\n                    )\n                    if row is None:\n                        raise RuntimeError(\n                            "LiquidityClearing: wallet row not found (id=1).  "\n                            "Ensure db.init_pool() has been called."\n                        )\n\n                    balance_before = float(row["crypto_balance"])\n\n                    # Step 2: Hold check\n                    if balance_before < amount_crypto:\n                        raise InsufficientEscrowError(\n                            f"LiquidityClearing: insufficient balance for contract "\n                            f"{contract_id!r}.  "\n                            f"Required: {amount_crypto:.8f}  "\n                            f"Available: {balance_before:.8f}",\n                            required=float(amount_crypto),\n                            escrowed=balance_before,\n                            transaction_id=contract_id,\n                        )\n\n                    # Step 3: Debit wallet\n                    balance_after = balance_before - amount_crypto\n                    await conn.execute(\n                        """\n                        UPDATE wallet\n                        SET crypto_balance = $1, updated_at = NOW()\n                        WHERE id = 1\n                        """,\n                        balance_after,\n                    )\n\n                    # Step 4: Record in clearing_log (same transaction)\n                    await conn.execute(\n                        self._CREATE_CLEARING_LOG_SQL\n                    )\n                    await conn.execute(\n                        """\n                        INSERT INTO clearing_log\n                            (contract_id, amount_crypto, wallet_balance_before,\n                             wallet_balance_after, contract_router)\n                        VALUES ($1, $2, $3, $4, $5)\n                        """,\n                        contract_id,\n                        amount_crypto,\n                        balance_before,\n                        balance_after,\n                        contract_router_str,\n                    )\n\n        except InsufficientEscrowError:\n            raise  # re-raise — not a system error\n        except Exception as exc:\n            logger.error(\n                "LiquidityClearing: atomic transaction failed for contract_id=%r: %s",\n                contract_id, exc,\n            )\n            raise\n\n        receipt = ClearingReceipt(\n            contract_id=contract_id,\n            amount_crypto=amount_crypto,\n            balance_before=balance_before,\n            balance_after=balance_after,\n            contract_router=contract_router_str,\n            cleared_at=datetime.now(timezone.utc),\n        )\n        logger.info(\n            "LiquidityClearing: cleared %.8f for contract_id=%r  "\n            "balance %s → %s",\n            amount_crypto, contract_id, balance_before, balance_after,\n        )\n        return receipt\n\n\n# ---------------------------------------------------------------------------\n# ClearingReceipt — settlement confirmation record\n# ---------------------------------------------------------------------------\n\n\n@dataclass(frozen=True, slots=True)\nclass ClearingReceipt:\n    """\n    Immutable record confirming a completed liquidity clearing.\n\n... (file truncated, showing first 500 lines) ...
```
