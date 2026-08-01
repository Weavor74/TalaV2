# File: `router.py`

## Module Description

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

## Classes

### `class ContractState`
Lifecycle states for a :class:`SmartContract`.

Using ``str`` as the mixin base means enum values serialise cleanly to
JSON (``json.dumps(ContractState.PENDING)`` → ``'"PENDING"'``) and
compare equal to plain strings, which simplifies database round-trips.


### `class SmartContract`
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


### `class ContractRouter`
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

- **`def _ensure_schema(self)`**
  - Apply DDL idempotently before the first database operation.
  
  Called internally by each public method.  After the first call the
  table exists and subsequent calls are near-instant no-ops.
- **`def broadcast(self, creator_id, counterparty_id, payload)`**
  - Create a new smart contract, assign a UUID, compute the SHA-256
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
- **`def get_state(self, contract_id)`**
  - Return the **live** state of *contract_id* from PostgreSQL.
  
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
- **`def update_state(self, contract_id, new_state)`**
  - Atomically transition *contract_id* to *new_state*.
  
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
- **`def get_hash(self, contract_id)`**
  - Return the **immutable** SHA-256 payload hash for *contract_id*.
  
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
- **`def get_contract(self, contract_id)`**
  - Fetch a full :class:`SmartContract` snapshot from the ledger.
  
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
- **`def verify_on_chain(self, contract_id)`**
  - Query the Tala blockchain node for the live on-chain state of
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

## Functions

### `async def _broadcast_to_chain(contract)`
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

### `async def _verify_on_chain(contract_id)`
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

### `def _validate_transition(current, target)`
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

## Source Code

```python
"""\nsdk/contracts/router.py — Tala Cognitive Engine Room\n=====================================================\nSmart Contract Proxy Router (Milestone 3, Task 3.2.x).\n\nArchitecture\n------------\nThe :class:`ContractRouter` treats PostgreSQL as the **primary smart\ncontract ledger** — every contract creation, state transition, and hash\nlookup is a direct, non-cached database call.  This upholds the zero-trust\nprinciple: the router *never* reads from an in-process cache.\n\nBlockchain integration\n----------------------\nEvery new contract is **anchored on Tala's own custom Node.js blockchain**\nby broadcasting the payload hash to the chain RPC node running locally at\n``http://127.0.0.1:8332``.  The chain node is implemented as a separate\nNode.js package in ``blockchain/`` (see ``blockchain/README.md``).\n\nIf the chain node is unreachable the router falls back gracefully to\nPostgreSQL-only mode, logs a WARNING, and continues — so that development\nand testing work without the full blockchain stack running.\n\nChain RPC endpoints used\n------------------------\n- ``POST /chain/tx``            — broadcast a new contract transaction\n- ``GET  /chain/tx/{id}``       — read live on-chain ledger state\n\nContract Lifecycle\n------------------\n\n    PENDING → STREAMING → ASSEMBLED → COMPLETED\n                                    → FAILED      (any time on error)\n\nEach transition is an atomic UPDATE with a timestamp.  The SHA-256\npayload hash is computed at creation time and is thereafter immutable —\n``get_hash()`` always returns the original value regardless of state.\n\nMilestone 3 — Task 3.2.1 – 3.2.4\n"""\n\nfrom __future__ import annotations\n\nimport hashlib\nimport json\nimport logging\nimport textwrap\nimport uuid\nfrom dataclasses import dataclass\nfrom datetime import datetime, timezone\nfrom enum import Enum\nfrom typing import Any\n\nlogger = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Blockchain RPC configuration\n# ---------------------------------------------------------------------------\n\n#: Base URL of the Tala custom Node.js blockchain RPC node.\n#: Override via ``TALA_CHAIN_RPC_URL`` environment variable for staging/prod.\nimport os as _os\nCHAIN_RPC_URL: str = _os.environ.get("TALA_CHAIN_RPC_URL", "http://127.0.0.1:8332")\n\n#: Timeout in seconds for all chain RPC calls.  If the node does not respond\n#: within this window, the router falls back to PostgreSQL-only mode.\nCHAIN_RPC_TIMEOUT: float = 5.0\n\n# ---------------------------------------------------------------------------\n# DDL — contract ledger table (applied lazily on first use)\n# ---------------------------------------------------------------------------\n\n_DDL_CONTRACTS = textwrap.dedent("""\\n    CREATE TABLE IF NOT EXISTS smart_contracts (\n        contract_id     UUID         PRIMARY KEY,\n        creator         TEXT         NOT NULL,\n        counterparty    TEXT         NOT NULL,\n        state           TEXT         NOT NULL DEFAULT 'PENDING',\n        payload_hash    TEXT         NOT NULL,\n        chain_tx_id     TEXT,\n        created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),\n        completed_at    TIMESTAMPTZ,\n        updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()\n    );\n""")\n\n_DDL_CONTRACTS_IDX_STATE = textwrap.dedent("""\\n    CREATE INDEX IF NOT EXISTS idx_smart_contracts_state\n        ON smart_contracts (state);\n""")\n\n_DDL_CONTRACTS_IDX_CREATOR = textwrap.dedent("""\\n    CREATE INDEX IF NOT EXISTS idx_smart_contracts_creator\n        ON smart_contracts (creator);\n""")\n\n# ---------------------------------------------------------------------------\n# ContractState enum (Task 3.2.1)\n# ---------------------------------------------------------------------------\n\nclass ContractState(str, Enum):\n    """\n    Lifecycle states for a :class:`SmartContract`.\n\n    Using ``str`` as the mixin base means enum values serialise cleanly to\n    JSON (``json.dumps(ContractState.PENDING)`` → ``'"PENDING"'``) and\n    compare equal to plain strings, which simplifies database round-trips.\n    """\n\n    PENDING    = "PENDING"\n    """Contract created; awaiting first chunk of work."""\n\n    STREAMING  = "STREAMING"\n    """LLM is actively streaming tokens for this contract."""\n\n    ASSEMBLED  = "ASSEMBLED"\n    """All chunks received; document assembled, pending settlement."""\n\n    COMPLETED  = "COMPLETED"\n    """Payment released; proof hash committed; contract closed."""\n\n    FAILED     = "FAILED"\n    """An unrecoverable error occurred; contract is terminal."""\n\n\n# ---------------------------------------------------------------------------\n# SmartContract dataclass (Task 3.2.1)\n# ---------------------------------------------------------------------------\n\n@dataclass(slots=True)\nclass SmartContract:\n    """\n    Represents a single smart contract record.\n\n    Instances are snapshots — they reflect the database state at the moment\n    they were fetched.  Do NOT mutate them in-process; use\n    :meth:`ContractRouter.update_state` for all state changes.\n\n    Attributes\n    ----------\n    contract_id:\n        Universally unique identifier (UUID v4) assigned at creation.\n    creator:\n        Opaque identifier of the agent / user that initiated the contract.\n    counterparty:\n        Opaque identifier of the receiving agent / service.\n    state:\n        Current lifecycle state (see :class:`ContractState`).\n    payload_hash:\n        Immutable SHA-256 hex digest of the creation payload.\n    chain_tx_id:\n        Transaction ID returned by the Tala blockchain node after anchoring.\n        ``None`` if the chain node was unreachable at creation time (the\n        contract still exists in PostgreSQL, chain anchoring can be retried).\n    created_at:\n        UTC datetime when the contract was first written to the ledger.\n    completed_at:\n        UTC datetime when the contract reached COMPLETED or FAILED state.\n        ``None`` while the contract is still active.\n    """\n\n    contract_id:  uuid.UUID\n    creator:      str\n    counterparty: str\n    state:        ContractState\n    payload_hash: str\n    created_at:   datetime\n    chain_tx_id:  str | None = None\n    completed_at: datetime | None = None\n\n\n# ---------------------------------------------------------------------------\n# Blockchain RPC helpers\n# ---------------------------------------------------------------------------\n\nasync def _broadcast_to_chain(contract: SmartContract) -> str:\n    """\n    Broadcast a new contract to the Tala custom Node.js blockchain node.\n\n    Makes a POST request to ``{CHAIN_RPC_URL}/chain/tx`` with the contract\n    summary as JSON.  The node returns a chain transaction ID (hex string)\n    that acts as a tamper-evident proof-of-existence anchor.\n\n    Fails **gracefully**: if the chain node is unreachable (connection\n    refused, timeout, non-2xx status), the error is logged as a WARNING and\n    the empty string ``""`` is returned — signalling PostgreSQL-only mode to\n    the caller.  The contract is still persisted in PostgreSQL and can be\n    re-anchored when the chain node comes back online.\n\n    Parameters\n    ----------\n    contract:\n        The newly created :class:`SmartContract` to anchor.\n\n    Returns\n    -------\n    str\n        Chain transaction ID hex string on success, ``""`` on fallback.\n    """\n    import httpx  # type: ignore[import]  # httpx>=0.27 required\n\n    body = {\n        "type":         "contract",\n        "id":           str(contract.contract_id),\n        "hash":         contract.payload_hash,\n        "creator":      contract.creator,\n        "counterparty": contract.counterparty,\n    }\n\n    try:\n        async with httpx.AsyncClient(timeout=CHAIN_RPC_TIMEOUT) as client:\n            response = await client.post(\n                f"{CHAIN_RPC_URL}/chain/tx",\n                json=body,\n                headers={"Content-Type": "application/json"},\n            )\n            response.raise_for_status()\n            data: dict[str, Any] = response.json()\n            tx_id: str = data.get("tx_id") or data.get("txId") or data.get("id") or ""\n            logger.info(\n                "Contract anchored on-chain — contract=%s  chain_tx_id=%s",\n                contract.contract_id,\n                tx_id,\n            )\n            return tx_id\n\n    except httpx.ConnectError:\n        logger.warning(\n            "Tala chain node not reachable at %s — running in PostgreSQL-only mode "\n            "(contract %s not yet anchored on-chain, retry later).",\n            CHAIN_RPC_URL,\n            contract.contract_id,\n        )\n    except httpx.TimeoutException:\n        logger.warning(\n            "Tala chain node timed out after %.1fs — running in PostgreSQL-only mode "\n            "(contract %s not yet anchored on-chain).",\n            CHAIN_RPC_TIMEOUT,\n            contract.contract_id,\n        )\n    except httpx.HTTPStatusError as exc:\n        logger.warning(\n            "Tala chain node returned HTTP %s for contract %s — PostgreSQL-only mode: %s",\n            exc.response.status_code,\n            contract.contract_id,\n            exc,\n        )\n    except Exception as exc:  # noqa: BLE001\n        logger.warning(\n            "Unexpected error broadcasting contract %s to chain: %s — PostgreSQL-only mode.",\n            contract.contract_id,\n            exc,\n        )\n\n    return ""  # fallback: PostgreSQL-only mode\n\n\nasync def _verify_on_chain(contract_id: str) -> dict[str, Any]:\n    """\n    Read the live ledger state for *contract_id* from the Tala blockchain node.\n\n    Makes a GET request to ``{CHAIN_RPC_URL}/chain/tx/{contract_id}``.\n    Returns the JSON response body as a dict on success, or an empty dict\n    on any error (network, timeout, 404, etc.) so the caller can always\n    proceed using the PostgreSQL record as the canonical truth.\n\n    Parameters\n    ----------\n    contract_id:\n        UUID string of the contract to look up on-chain.\n\n    Returns\n    -------\n    dict[str, Any]\n        On-chain ledger record, or ``{}`` when unavailable.\n    """\n    import httpx  # type: ignore[import]\n\n    try:\n        async with httpx.AsyncClient(timeout=CHAIN_RPC_TIMEOUT) as client:\n            response = await client.get(\n                f"{CHAIN_RPC_URL}/chain/tx/{contract_id}",\n                headers={"Accept": "application/json"},\n            )\n            if response.status_code == 404:\n                logger.debug(\n                    "_verify_on_chain: contract %s not yet on-chain (404)", contract_id\n                )\n                return {}\n            response.raise_for_status()\n            return response.json()  # type: ignore[return-value]\n\n    except httpx.ConnectError:\n        logger.warning(\n            "_verify_on_chain: chain node unreachable at %s — skipping on-chain verification "\n            "for contract %s.",\n            CHAIN_RPC_URL,\n            contract_id,\n        )\n    except httpx.TimeoutException:\n        logger.warning(\n            "_verify_on_chain: chain node timed out for contract %s.", contract_id\n        )\n    except Exception as exc:  # noqa: BLE001\n        logger.warning(\n            "_verify_on_chain: unexpected error for contract %s: %s", contract_id, exc\n        )\n\n    return {}  # fallback\n\n\n# ---------------------------------------------------------------------------\n# ContractRouter (Task 3.2.2 – 3.2.4)\n# ---------------------------------------------------------------------------\n\nclass ContractRouter:\n    """\n    Smart Contract Proxy Router — PostgreSQL-backed ledger with Tala\n    custom-blockchain anchoring.\n\n    All public methods are **async** (they interact with the asyncpg pool\n    and optionally the Tala blockchain RPC node).  Each method acquires a\n    fresh connection from the pool on every call (zero-trust: never reads\n    from an in-process cache).\n\n    Blockchain anchoring\n    --------------------\n    On ``broadcast()``, the contract's payload hash is sent to the Tala\n    Node.js blockchain node at ``CHAIN_RPC_URL``.  If the node is offline,\n    the router continues in PostgreSQL-only mode.  The returned\n    ``chain_tx_id`` will be ``None`` in that case, which is acceptable for\n    development.  Use ``_verify_on_chain()`` to check anchoring status.\n\n    Example\n    -------\n    ::\n\n        router = ContractRouter()\n        contract = await router.broadcast(\n            creator_id="agent-alice",\n            counterparty_id="agent-bob",\n            payload={"task": "summarise", "doc_id": "abc123"},\n        )\n        state = await router.get_state(str(contract.contract_id))\n        await router.update_state(str(contract.contract_id), ContractState.STREAMING)\n    """\n\n    # ------------------------------------------------------------------\n    # Schema bootstrap\n    # ------------------------------------------------------------------\n\n    async def _ensure_schema(self) -> None:\n        """\n        Apply DDL idempotently before the first database operation.\n\n        Called internally by each public method.  After the first call the\n        table exists and subsequent calls are near-instant no-ops.\n        """\n        from python.harness import db  # type: ignore[import]\n\n        for stmt in (_DDL_CONTRACTS, _DDL_CONTRACTS_IDX_STATE, _DDL_CONTRACTS_IDX_CREATOR):\n            await db.execute(stmt, caller="ContractRouter._ensure_schema")\n\n    # ------------------------------------------------------------------\n    # broadcast (Task 3.2.2)\n    # ------------------------------------------------------------------\n\n    async def broadcast(\n        self,\n        creator_id: str,\n        counterparty_id: str,\n        payload: dict[str, Any],\n    ) -> SmartContract:\n        """\n        Create a new smart contract, assign a UUID, compute the SHA-256\n        payload hash, persist it to the PostgreSQL ledger, and anchor it\n        on the Tala custom blockchain.\n\n        The payload hash is computed *before* writing to the database and\n        is thereafter immutable — it cannot be changed via\n        :meth:`update_state` or any other method.\n\n        Blockchain anchoring is attempted after the PostgreSQL write\n        succeeds.  If the chain node is unreachable, anchoring is skipped\n        and ``contract.chain_tx_id`` will be ``None``; the contract is still\n        fully functional using PostgreSQL as the authority.\n\n        Parameters\n        ----------\n        creator_id:\n            Identifier of the initiating agent / user.\n        counterparty_id:\n            Identifier of the receiving agent / service.\n        payload:\n            Arbitrary dict describing the contract terms.  Must be\n            JSON-serialisable.  No file paths should appear here — use\n            handshake packets for binary/path references.\n\n        Returns\n        -------\n        SmartContract\n            The newly created contract in ``PENDING`` state, with\n            ``chain_tx_id`` populated if anchoring succeeded.\n\n        Raises\n        ------\n        ValueError\n            If *payload* is not JSON-serialisable.\n        """\n        await self._ensure_schema()\n        from python.harness import db  # type: ignore[import]\n\n        # Deterministic serialisation: sort keys so identical dicts always\n        # produce the same hash regardless of insertion order.\n        try:\n            payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)\n        except (TypeError, ValueError) as exc:\n            raise ValueError(\n                f"ContractRouter.broadcast: payload must be JSON-serialisable. {exc}"\n            ) from exc\n\n        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()\n        contract_id = uuid.uuid4()\n        created_at = datetime.now(tz=timezone.utc)\n\n        # --- Step 1: Persist to PostgreSQL ledger ---\n        await db.execute(\n            textwrap.dedent("""\\n                INSERT INTO smart_contracts (\n                    contract_id, creator, counterparty,\n                    state, payload_hash, created_at, updated_at\n                ) VALUES ($1, $2, $3, $4, $5, $6, $6)\n            """),\n            contract_id,\n            creator_id,\n            counterparty_id,\n            ContractState.PENDING.value,\n            payload_hash,\n            created_at,\n            caller="ContractRouter.broadcast",\n        )\n\n        logger.info(\n            "Contract BROADCAST (PostgreSQL) — id=%s  creator=%s  counterparty=%s  hash=%s…",\n            contract_id,\n            creator_id,\n            counterparty_id,\n            payload_hash[:16],\n        )\n\n        # Build the contract object so we can pass it to the chain helper.\n        contract = SmartContract(\n            contract_id=contract_id,\n            creator=creator_id,\n            counterparty=counterparty_id,\n            state=ContractState.PENDING,\n            payload_hash=payload_hash,\n            created_at=created_at,\n            chain_tx_id=None,\n        )\n\n        # --- Step 2: Anchor on Tala custom blockchain (graceful fallback) ---\n        chain_tx_id = await _broadcast_to_chain(contract)\n\n        if chain_tx_id:\n            # Store the chain TX ID back into PostgreSQL for cross-reference.\n            await db.execute(\n                "UPDATE smart_contracts SET chain_tx_id = $2 WHERE contract_id = $1",\n                contract_id,\n                chain_tx_id,\n                caller="ContractRouter.broadcast.chain_tx_update",\n            )\n            contract.chain_tx_id = chain_tx_id  # update the returned snapshot\n\n        return contract\n\n    # ------------------------------------------------------------------\n    # get_state (Task 3.2.3 — zero-trust, always live)\n    # ------------------------------------------------------------------\n\n    async def get_state(self, contract_id: str) -> ContractState:\n        """\n        Return the **live** state of *contract_id* from PostgreSQL.\n\n        Zero-trust invariant: this method **always** queries the database\n        and **never** returns a locally cached value.  A single-row read\n        is cheap; correctness is not.\n\n        Parameters\n        ----------\n        contract_id:\n            UUID string of the contract to look up.\n\n        Returns\n        -------\n        ContractState\n            Current lifecycle state.\n\n        Raises\n        ------\n... (file truncated, showing first 500 lines) ...
```
