"""
sdk/contracts/router.py — Tala Cognitive Engine Room
=====================================================
Smart Contract Proxy Router (Milestone 3, Task 3.2.x).

Architecture
------------
The :class:`ContractRouter` treats PostgreSQL as the **primary smart
contract ledger** — every contract creation, state transition, and hash
lookup is a direct, non-cached database call.  This upholds the zero-trust
principle: the router *never* reads from an in-process cache.

Blockchain integration
----------------------
Every new contract is **anchored on Tala's own custom Node.js blockchain**
by broadcasting the payload hash to the chain RPC node running locally at
``http://127.0.0.1:8332``.  The chain node is implemented as a separate
Node.js package in ``blockchain/`` (see ``blockchain/README.md``).

If the chain node is unreachable the router falls back gracefully to
PostgreSQL-only mode, logs a WARNING, and continues — so that development
and testing work without the full blockchain stack running.

Chain RPC endpoints used
------------------------
- ``POST /chain/tx``            — broadcast a new contract transaction
- ``GET  /chain/tx/{id}``       — read live on-chain ledger state

Contract Lifecycle
------------------

    PENDING → STREAMING → ASSEMBLED → COMPLETED
                                    → FAILED      (any time on error)

Each transition is an atomic UPDATE with a timestamp.  The SHA-256
payload hash is computed at creation time and is thereafter immutable —
``get_hash()`` always returns the original value regardless of state.

Milestone 3 — Task 3.2.1 – 3.2.4
"""

from __future__ import annotations

import hashlib
import json
import logging
import textwrap
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blockchain RPC configuration
# ---------------------------------------------------------------------------

#: Base URL of the Tala custom Node.js blockchain RPC node.
#: Override via ``TALA_CHAIN_RPC_URL`` environment variable for staging/prod.
import os as _os
CHAIN_RPC_URL: str = _os.environ.get("TALA_CHAIN_RPC_URL", "http://127.0.0.1:8332")

#: Timeout in seconds for all chain RPC calls.  If the node does not respond
#: within this window, the router falls back to PostgreSQL-only mode.
CHAIN_RPC_TIMEOUT: float = 5.0

# ---------------------------------------------------------------------------
# DDL — contract ledger table (applied lazily on first use)
# ---------------------------------------------------------------------------

_DDL_CONTRACTS = textwrap.dedent("""\
    CREATE TABLE IF NOT EXISTS smart_contracts (
        contract_id     UUID         PRIMARY KEY,
        creator         TEXT         NOT NULL,
        counterparty    TEXT         NOT NULL,
        state           TEXT         NOT NULL DEFAULT 'PENDING',
        payload_hash    TEXT         NOT NULL,
        chain_tx_id     TEXT,
        created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        completed_at    TIMESTAMPTZ,
        updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
""")

_DDL_CONTRACTS_IDX_STATE = textwrap.dedent("""\
    CREATE INDEX IF NOT EXISTS idx_smart_contracts_state
        ON smart_contracts (state);
""")

_DDL_CONTRACTS_IDX_CREATOR = textwrap.dedent("""\
    CREATE INDEX IF NOT EXISTS idx_smart_contracts_creator
        ON smart_contracts (creator);
""")

# ---------------------------------------------------------------------------
# ContractState enum (Task 3.2.1)
# ---------------------------------------------------------------------------

class ContractState(str, Enum):
    """
    Lifecycle states for a :class:`SmartContract`.

    Using ``str`` as the mixin base means enum values serialise cleanly to
    JSON (``json.dumps(ContractState.PENDING)`` → ``'"PENDING"'``) and
    compare equal to plain strings, which simplifies database round-trips.
    """

    PENDING    = "PENDING"
    """Contract created; awaiting first chunk of work."""

    STREAMING  = "STREAMING"
    """LLM is actively streaming tokens for this contract."""

    ASSEMBLED  = "ASSEMBLED"
    """All chunks received; document assembled, pending settlement."""

    COMPLETED  = "COMPLETED"
    """Payment released; proof hash committed; contract closed."""

    FAILED     = "FAILED"
    """An unrecoverable error occurred; contract is terminal."""


# ---------------------------------------------------------------------------
# SmartContract dataclass (Task 3.2.1)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SmartContract:
    """
    Represents a single smart contract record.

    Instances are snapshots — they reflect the database state at the moment
    they were fetched.  Do NOT mutate them in-process; use
    :meth:`ContractRouter.update_state` for all state changes.

    Attributes
    ----------
    contract_id:
        Universally unique identifier (UUID v4) assigned at creation.
    creator:
        Opaque identifier of the agent / user that initiated the contract.
    counterparty:
        Opaque identifier of the receiving agent / service.
    state:
        Current lifecycle state (see :class:`ContractState`).
    payload_hash:
        Immutable SHA-256 hex digest of the creation payload.
    chain_tx_id:
        Transaction ID returned by the Tala blockchain node after anchoring.
        ``None`` if the chain node was unreachable at creation time (the
        contract still exists in PostgreSQL, chain anchoring can be retried).
    created_at:
        UTC datetime when the contract was first written to the ledger.
    completed_at:
        UTC datetime when the contract reached COMPLETED or FAILED state.
        ``None`` while the contract is still active.
    """

    contract_id:  uuid.UUID
    creator:      str
    counterparty: str
    state:        ContractState
    payload_hash: str
    created_at:   datetime
    chain_tx_id:  str | None = None
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Blockchain RPC helpers
# ---------------------------------------------------------------------------

async def _broadcast_to_chain(contract: SmartContract) -> str:
    """
    Broadcast a new contract to the Tala custom Node.js blockchain node.

    Makes a POST request to ``{CHAIN_RPC_URL}/chain/tx`` with the contract
    summary as JSON.  The node returns a chain transaction ID (hex string)
    that acts as a tamper-evident proof-of-existence anchor.

    Fails **gracefully**: if the chain node is unreachable (connection
    refused, timeout, non-2xx status), the error is logged as a WARNING and
    the empty string ``""`` is returned — signalling PostgreSQL-only mode to
    the caller.  The contract is still persisted in PostgreSQL and can be
    re-anchored when the chain node comes back online.

    Parameters
    ----------
    contract:
        The newly created :class:`SmartContract` to anchor.

    Returns
    -------
    str
        Chain transaction ID hex string on success, ``""`` on fallback.
    """
    import httpx  # type: ignore[import]  # httpx>=0.27 required

    body = {
        "type":         "contract",
        "id":           str(contract.contract_id),
        "hash":         contract.payload_hash,
        "creator":      contract.creator,
        "counterparty": contract.counterparty,
    }

    try:
        async with httpx.AsyncClient(timeout=CHAIN_RPC_TIMEOUT) as client:
            response = await client.post(
                f"{CHAIN_RPC_URL}/chain/tx",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            tx_id: str = data.get("tx_id") or data.get("txId") or data.get("id") or ""
            logger.info(
                "Contract anchored on-chain — contract=%s  chain_tx_id=%s",
                contract.contract_id,
                tx_id,
            )
            return tx_id

    except httpx.ConnectError:
        logger.warning(
            "Tala chain node not reachable at %s — running in PostgreSQL-only mode "
            "(contract %s not yet anchored on-chain, retry later).",
            CHAIN_RPC_URL,
            contract.contract_id,
        )
    except httpx.TimeoutException:
        logger.warning(
            "Tala chain node timed out after %.1fs — running in PostgreSQL-only mode "
            "(contract %s not yet anchored on-chain).",
            CHAIN_RPC_TIMEOUT,
            contract.contract_id,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Tala chain node returned HTTP %s for contract %s — PostgreSQL-only mode: %s",
            exc.response.status_code,
            contract.contract_id,
            exc,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Unexpected error broadcasting contract %s to chain: %s — PostgreSQL-only mode.",
            contract.contract_id,
            exc,
        )

    return ""  # fallback: PostgreSQL-only mode


async def _verify_on_chain(contract_id: str) -> dict[str, Any]:
    """
    Read the live ledger state for *contract_id* from the Tala blockchain node.

    Makes a GET request to ``{CHAIN_RPC_URL}/chain/tx/{contract_id}``.
    Returns the JSON response body as a dict on success, or an empty dict
    on any error (network, timeout, 404, etc.) so the caller can always
    proceed using the PostgreSQL record as the canonical truth.

    Parameters
    ----------
    contract_id:
        UUID string of the contract to look up on-chain.

    Returns
    -------
    dict[str, Any]
        On-chain ledger record, or ``{}`` when unavailable.
    """
    import httpx  # type: ignore[import]

    try:
        async with httpx.AsyncClient(timeout=CHAIN_RPC_TIMEOUT) as client:
            response = await client.get(
                f"{CHAIN_RPC_URL}/chain/tx/{contract_id}",
                headers={"Accept": "application/json"},
            )
            if response.status_code == 404:
                logger.debug(
                    "_verify_on_chain: contract %s not yet on-chain (404)", contract_id
                )
                return {}
            response.raise_for_status()
            return response.json()  # type: ignore[return-value]

    except httpx.ConnectError:
        logger.warning(
            "_verify_on_chain: chain node unreachable at %s — skipping on-chain verification "
            "for contract %s.",
            CHAIN_RPC_URL,
            contract_id,
        )
    except httpx.TimeoutException:
        logger.warning(
            "_verify_on_chain: chain node timed out for contract %s.", contract_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_verify_on_chain: unexpected error for contract %s: %s", contract_id, exc
        )

    return {}  # fallback


# ---------------------------------------------------------------------------
# ContractRouter (Task 3.2.2 – 3.2.4)
# ---------------------------------------------------------------------------

class ContractRouter:
    """
    Smart Contract Proxy Router — PostgreSQL-backed ledger with Tala
    custom-blockchain anchoring.

    All public methods are **async** (they interact with the asyncpg pool
    and optionally the Tala blockchain RPC node).  Each method acquires a
    fresh connection from the pool on every call (zero-trust: never reads
    from an in-process cache).

    Blockchain anchoring
    --------------------
    On ``broadcast()``, the contract's payload hash is sent to the Tala
    Node.js blockchain node at ``CHAIN_RPC_URL``.  If the node is offline,
    the router continues in PostgreSQL-only mode.  The returned
    ``chain_tx_id`` will be ``None`` in that case, which is acceptable for
    development.  Use ``_verify_on_chain()`` to check anchoring status.

    Example
    -------
    ::

        router = ContractRouter()
        contract = await router.broadcast(
            creator_id="agent-alice",
            counterparty_id="agent-bob",
            payload={"task": "summarise", "doc_id": "abc123"},
        )
        state = await router.get_state(str(contract.contract_id))
        await router.update_state(str(contract.contract_id), ContractState.STREAMING)
    """

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    async def _ensure_schema(self) -> None:
        """
        Apply DDL idempotently before the first database operation.

        Called internally by each public method.  After the first call the
        table exists and subsequent calls are near-instant no-ops.
        """
        from python.harness import db  # type: ignore[import]

        for stmt in (_DDL_CONTRACTS, _DDL_CONTRACTS_IDX_STATE, _DDL_CONTRACTS_IDX_CREATOR):
            await db.execute(stmt, caller="ContractRouter._ensure_schema")

    # ------------------------------------------------------------------
    # broadcast (Task 3.2.2)
    # ------------------------------------------------------------------

    async def broadcast(
        self,
        creator_id: str,
        counterparty_id: str,
        payload: dict[str, Any],
    ) -> SmartContract:
        """
        Create a new smart contract, assign a UUID, compute the SHA-256
        payload hash, persist it to the PostgreSQL ledger, and anchor it
        on the Tala custom blockchain.

        The payload hash is computed *before* writing to the database and
        is thereafter immutable — it cannot be changed via
        :meth:`update_state` or any other method.

        Blockchain anchoring is attempted after the PostgreSQL write
        succeeds.  If the chain node is unreachable, anchoring is skipped
        and ``contract.chain_tx_id`` will be ``None``; the contract is still
        fully functional using PostgreSQL as the authority.

        Parameters
        ----------
        creator_id:
            Identifier of the initiating agent / user.
        counterparty_id:
            Identifier of the receiving agent / service.
        payload:
            Arbitrary dict describing the contract terms.  Must be
            JSON-serialisable.  No file paths should appear here — use
            handshake packets for binary/path references.

        Returns
        -------
        SmartContract
            The newly created contract in ``PENDING`` state, with
            ``chain_tx_id`` populated if anchoring succeeded.

        Raises
        ------
        ValueError
            If *payload* is not JSON-serialisable.
        """
        await self._ensure_schema()
        from python.harness import db  # type: ignore[import]

        # Deterministic serialisation: sort keys so identical dicts always
        # produce the same hash regardless of insertion order.
        try:
            payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"ContractRouter.broadcast: payload must be JSON-serialisable. {exc}"
            ) from exc

        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        contract_id = uuid.uuid4()
        created_at = datetime.now(tz=timezone.utc)

        # --- Step 1: Persist to PostgreSQL ledger ---
        await db.execute(
            textwrap.dedent("""\
                INSERT INTO smart_contracts (
                    contract_id, creator, counterparty,
                    state, payload_hash, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $6)
            """),
            contract_id,
            creator_id,
            counterparty_id,
            ContractState.PENDING.value,
            payload_hash,
            created_at,
            caller="ContractRouter.broadcast",
        )

        logger.info(
            "Contract BROADCAST (PostgreSQL) — id=%s  creator=%s  counterparty=%s  hash=%s…",
            contract_id,
            creator_id,
            counterparty_id,
            payload_hash[:16],
        )

        # Build the contract object so we can pass it to the chain helper.
        contract = SmartContract(
            contract_id=contract_id,
            creator=creator_id,
            counterparty=counterparty_id,
            state=ContractState.PENDING,
            payload_hash=payload_hash,
            created_at=created_at,
            chain_tx_id=None,
        )

        # --- Step 2: Anchor on Tala custom blockchain (graceful fallback) ---
        chain_tx_id = await _broadcast_to_chain(contract)

        if chain_tx_id:
            # Store the chain TX ID back into PostgreSQL for cross-reference.
            await db.execute(
                "UPDATE smart_contracts SET chain_tx_id = $2 WHERE contract_id = $1",
                contract_id,
                chain_tx_id,
                caller="ContractRouter.broadcast.chain_tx_update",
            )
            contract.chain_tx_id = chain_tx_id  # update the returned snapshot

        return contract

    # ------------------------------------------------------------------
    # get_state (Task 3.2.3 — zero-trust, always live)
    # ------------------------------------------------------------------

    async def get_state(self, contract_id: str) -> ContractState:
        """
        Return the **live** state of *contract_id* from PostgreSQL.

        Zero-trust invariant: this method **always** queries the database
        and **never** returns a locally cached value.  A single-row read
        is cheap; correctness is not.

        Parameters
        ----------
        contract_id:
            UUID string of the contract to look up.

        Returns
        -------
        ContractState
            Current lifecycle state.

        Raises
        ------
        KeyError
            If no contract with *contract_id* exists in the ledger.
        """
        await self._ensure_schema()
        from python.harness import db  # type: ignore[import]

        row = await db.fetchrow(
            "SELECT state FROM smart_contracts WHERE contract_id = $1",
            uuid.UUID(contract_id),
        )
        if row is None:
            raise KeyError(f"ContractRouter.get_state: unknown contract_id={contract_id!r}")

        return ContractState(row["state"])

    # ------------------------------------------------------------------
    # update_state (Task 3.2.3 — atomic with timestamp)
    # ------------------------------------------------------------------

    async def update_state(
        self,
        contract_id: str,
        new_state: ContractState,
    ) -> None:
        """
        Atomically transition *contract_id* to *new_state*.

        The ``updated_at`` column is always refreshed.  When *new_state*
        is ``COMPLETED`` or ``FAILED``, ``completed_at`` is also set.

        Parameters
        ----------
        contract_id:
            UUID string of the contract to update.
        new_state:
            Target lifecycle state.

        Raises
        ------
        KeyError
            If *contract_id* does not exist.
        ValueError
            If the transition is logically invalid (e.g. COMPLETED → PENDING).
        """
        await self._ensure_schema()
        from python.harness import db  # type: ignore[import]

        # Guard: fetch current state for transition validation.
        current_state = await self.get_state(contract_id)
        _validate_transition(current_state, new_state)

        now = datetime.now(tz=timezone.utc)
        is_terminal = new_state in (ContractState.COMPLETED, ContractState.FAILED)

        result = await db.execute(
            textwrap.dedent("""\
                UPDATE smart_contracts
                SET    state        = $2,
                       updated_at   = $3,
                       completed_at = CASE WHEN $4 THEN $3 ELSE completed_at END
                WHERE  contract_id  = $1
            """),
            uuid.UUID(contract_id),
            new_state.value,
            now,
            is_terminal,
            caller="ContractRouter.update_state",
        )

        if result == "UPDATE 0":
            raise KeyError(
                f"ContractRouter.update_state: no rows updated for contract_id={contract_id!r}"
            )

        logger.info(
            "Contract STATE UPDATE — id=%s  %s → %s",
            contract_id,
            current_state.value,
            new_state.value,
        )

    # ------------------------------------------------------------------
    # get_hash (Task 3.2.4 — immutable, always from DB)
    # ------------------------------------------------------------------

    async def get_hash(self, contract_id: str) -> str:
        """
        Return the **immutable** SHA-256 payload hash for *contract_id*.

        The hash is computed at creation time and is stored once in the
        ``payload_hash`` column.  It is never updated by any other method.
        This makes it suitable as a proof-of-existence anchor for both the
        PostgreSQL ledger and the Tala blockchain.

        Parameters
        ----------
        contract_id:
            UUID string of the contract.

        Returns
        -------
        str
            64-character lowercase hex SHA-256 digest.

        Raises
        ------
        KeyError
            If *contract_id* does not exist in the ledger.
        """
        await self._ensure_schema()
        from python.harness import db  # type: ignore[import]

        row = await db.fetchrow(
            "SELECT payload_hash FROM smart_contracts WHERE contract_id = $1",
            uuid.UUID(contract_id),
        )
        if row is None:
            raise KeyError(f"ContractRouter.get_hash: unknown contract_id={contract_id!r}")

        return row["payload_hash"]

    # ------------------------------------------------------------------
    # Convenience: fetch full contract snapshot
    # ------------------------------------------------------------------

    async def get_contract(self, contract_id: str) -> SmartContract:
        """
        Fetch a full :class:`SmartContract` snapshot from the ledger.

        Parameters
        ----------
        contract_id:
            UUID string of the contract.

        Returns
        -------
        SmartContract
            A dataclass snapshot reflecting current ledger state.

        Raises
        ------
        KeyError
            If *contract_id* does not exist.
        """
        await self._ensure_schema()
        from python.harness import db  # type: ignore[import]

        row = await db.fetchrow(
            textwrap.dedent("""\
                SELECT contract_id, creator, counterparty, state,
                       payload_hash, chain_tx_id, created_at, completed_at
                FROM   smart_contracts
                WHERE  contract_id = $1
            """),
            uuid.UUID(contract_id),
        )
        if row is None:
            raise KeyError(f"ContractRouter.get_contract: unknown contract_id={contract_id!r}")

        return SmartContract(
            contract_id=row["contract_id"],
            creator=row["creator"],
            counterparty=row["counterparty"],
            state=ContractState(row["state"]),
            payload_hash=row["payload_hash"],
            chain_tx_id=row["chain_tx_id"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    # ------------------------------------------------------------------
    # On-chain verification utility
    # ------------------------------------------------------------------

    async def verify_on_chain(self, contract_id: str) -> dict[str, Any]:
        """
        Query the Tala blockchain node for the live on-chain state of
        *contract_id*.

        Returns the raw JSON dict from the chain node, or an empty dict
        if the contract has not been anchored yet or the node is offline.
        The PostgreSQL record remains the canonical authority regardless.

        Parameters
        ----------
        contract_id:
            UUID string of the contract.

        Returns
        -------
        dict[str, Any]
            On-chain ledger record from ``GET /chain/tx/{contract_id}``.
        """
        return await _verify_on_chain(contract_id)


# ---------------------------------------------------------------------------
# State machine transition guard
# ---------------------------------------------------------------------------

# Valid directed transitions.  Any edge not in this set is illegal.
_VALID_TRANSITIONS: frozenset[tuple[ContractState, ContractState]] = frozenset({
    (ContractState.PENDING,    ContractState.STREAMING),
    (ContractState.PENDING,    ContractState.FAILED),
    (ContractState.STREAMING,  ContractState.ASSEMBLED),
    (ContractState.STREAMING,  ContractState.FAILED),
    (ContractState.ASSEMBLED,  ContractState.COMPLETED),
    (ContractState.ASSEMBLED,  ContractState.FAILED),
    # Allow re-entry for idempotent FAILED marking
    (ContractState.FAILED,     ContractState.FAILED),
})


def _validate_transition(
    current: ContractState,
    target: ContractState,
) -> None:
    """
    Raise :class:`ValueError` if the ``current → target`` transition is
    not in the allowed state machine graph.

    Parameters
    ----------
    current:
        The contract's current state.
    target:
        The requested new state.

    Raises
    ------
    ValueError
        On an illegal transition.
    """
    if (current, target) not in _VALID_TRANSITIONS:
        raise ValueError(
            f"Illegal contract state transition: {current.value} → {target.value}.  "
            f"Allowed transitions from {current.value}: "
            f"{[t.value for (s, t) in _VALID_TRANSITIONS if s == current]}"
        )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "ContractState",
    "SmartContract",
    "ContractRouter",
    "CHAIN_RPC_URL",
    "CHAIN_RPC_TIMEOUT",
    "_broadcast_to_chain",   # exported for integration tests
    "_verify_on_chain",      # exported for integration tests
]
