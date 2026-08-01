# File: `mem0_adapter.py`

## Module Description

mem0_adapter.py — Tala Cognitive Engine Room: mem0 Read-Only Restriction Adapter
=================================================================================

Architectural Invariant
-----------------------
mem0 is NEVER a durable authority for state in the Tala system.
PostgreSQL (via ``db.py``) is the ONLY authoritative store for all persistent
data — nodes, token ledger, wallet, agent logs.

mem0's role is strictly:
  - Semantic / fuzzy candidate matching (read-only ``search()``)
  - Direct id-based retrieval (read-only ``get()``)

This adapter wraps any mem0 client instance and:
  1. Blocks all mutating operations (``add``, ``update``, ``delete``,
     ``reset``, ``add_to_history``, and any method whose name starts with
     ``add_``, ``update_``, ``delete_``, or ``create_``).
  2. Logs every allowed read operation to the ``agent_log`` PostgreSQL table.
  3. Sets a sentinel token (``__tala_mem0_ctx__``) in local frame vars so
     that ``db.assert_postgres_authority()`` can detect and reject any DB
     write attempted from within a mem0 call-chain.

Usage
-----
    from harness.mem0_adapter import Mem0ReadOnlyAdapter

    raw_client = Memory()  # your mem0 client
    safe_client = Mem0ReadOnlyAdapter(raw_client)
    results = await safe_client.search("relevant memories", user_id="u1")

Milestone 1.2 — Task 1.2.4

## Classes

### `class Mem0WriteBlockedError`
Raised when code attempts to call a mutating method on the mem0 client.

This is a hard architectural enforcement.  All durable writes MUST go
through ``db.py`` (PostgreSQL).  mem0 is a read-only semantic cache.

Attributes
----------
method_name : str
    The name of the blocked method.

- **`def __init__(self, method_name)`**

### `class Mem0ReadOnlyAdapter`
Proxy wrapper around a mem0 client that enforces read-only access.

Any call to a mutating method raises :class:`Mem0WriteBlockedError`.
All allowed calls are transparently proxied to the underlying client,
with structured logging to both the Python logger and PostgreSQL
``agent_log`` table.

Parameters
----------
client:
    An instantiated mem0 ``Memory`` (or compatible) object.
log_to_db:
    If ``True`` (default), every search/get call is logged to the
    ``agent_log`` table via ``db.log_to_db()``.

- **`def __init__(self, client)`**
- **`def __getattr__(self, name)`**
- **`def __setattr__(self, name, value)`**
- **`def __repr__(self)`**

## Functions

### `def _is_blocked(name)`
Return True if *name* identifies a mutating mem0 method.

### `def _schedule_log(level, message, trace)`
Fire-and-forget: schedule a ``db.log_to_db()`` call on the running loop.

Swallows all errors so a DB outage never disrupts the mem0 read path.

### `def wrap_mem0_client(client)`
Wrap *client* in a :class:`Mem0ReadOnlyAdapter`.

This is the canonical way to obtain a safe mem0 client throughout the
Tala harness.  Never use a raw mem0 client directly.

Parameters
----------
client:
    An instantiated mem0 ``Memory`` object.
log_to_db:
    Whether to log reads to the ``agent_log`` table (default: ``True``).

Returns
-------
Mem0ReadOnlyAdapter

## Source Code

```python
"""
mem0_adapter.py — Tala Cognitive Engine Room: mem0 Read-Only Restriction Adapter
=================================================================================

Architectural Invariant
-----------------------
mem0 is NEVER a durable authority for state in the Tala system.
PostgreSQL (via ``db.py``) is the ONLY authoritative store for all persistent
data — nodes, token ledger, wallet, agent logs.

mem0's role is strictly:
  - Semantic / fuzzy candidate matching (read-only ``search()``)
  - Direct id-based retrieval (read-only ``get()``)

This adapter wraps any mem0 client instance and:
  1. Blocks all mutating operations (``add``, ``update``, ``delete``,
     ``reset``, ``add_to_history``, and any method whose name starts with
     ``add_``, ``update_``, ``delete_``, or ``create_``).
  2. Logs every allowed read operation to the ``agent_log`` PostgreSQL table.
  3. Sets a sentinel token (``__tala_mem0_ctx__``) in local frame vars so
     that ``db.assert_postgres_authority()`` can detect and reject any DB
     write attempted from within a mem0 call-chain.

Usage
-----
    from harness.mem0_adapter import Mem0ReadOnlyAdapter

    raw_client = Memory()  # your mem0 client
    safe_client = Mem0ReadOnlyAdapter(raw_client)
    results = await safe_client.search("relevant memories", user_id="u1")

Milestone 1.2 — Task 1.2.4
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class Mem0WriteBlockedError(Exception):
    """
    Raised when code attempts to call a mutating method on the mem0 client.

    This is a hard architectural enforcement.  All durable writes MUST go
    through ``db.py`` (PostgreSQL).  mem0 is a read-only semantic cache.

    Attributes
    ----------
    method_name : str
        The name of the blocked method.
    """

    def __init__(self, method_name: str) -> None:
        self.method_name = method_name
        super().__init__(
            f"[TALA INVARIANT VIOLATION] mem0 write operation blocked: "
            f"'{method_name}' is not permitted.  "
            f"Reason: mem0 is a READ-ONLY semantic cache in the Tala system.  "
            f"All durable state MUST be written through PostgreSQL (db.py).  "
            f"If you need to persist information, use db.execute() instead."
        )


# ---------------------------------------------------------------------------
# Blocked method name patterns
# ---------------------------------------------------------------------------

# Exact method names that are always blocked
_BLOCKED_EXACT: frozenset[str] = frozenset({
    "add",
    "update",
    "delete",
    "reset",
    "clear",
    "add_to_history",
    "create",
    "put",
    "set",
    "save",
    "write",
    "insert",
    "upsert",
    "remove",
    "patch",
    "replace",
})

# Prefixes — any method starting with these is blocked
_BLOCKED_PREFIXES: tuple[str, ...] = (
    "add_",
    "update_",
    "delete_",
    "create_",
    "remove_",
    "write_",
    "insert_",
    "upsert_",
    "put_",
    "save_",
)

# Allowed read method names (whitelist for strict mode)
_ALLOWED_EXACT: frozenset[str] = frozenset({
    "search",
    "get",
    "get_all",
    "list",
    "history",
    "get_history",
})


def _is_blocked(name: str) -> bool:
    """Return True if *name* identifies a mutating mem0 method."""
    if name.startswith("_"):
        return False  # private/dunder methods pass through (for proxying)
    if name in _BLOCKED_EXACT:
        return True
    return any(name.startswith(prefix) for prefix in _BLOCKED_PREFIXES)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class Mem0ReadOnlyAdapter:
    """
    Proxy wrapper around a mem0 client that enforces read-only access.

    Any call to a mutating method raises :class:`Mem0WriteBlockedError`.
    All allowed calls are transparently proxied to the underlying client,
    with structured logging to both the Python logger and PostgreSQL
    ``agent_log`` table.

    Parameters
    ----------
    client:
        An instantiated mem0 ``Memory`` (or compatible) object.
    log_to_db:
        If ``True`` (default), every search/get call is logged to the
        ``agent_log`` table via ``db.log_to_db()``.
    """

    def __init__(self, client: Any, *, log_to_db: bool = True) -> None:
        # Store under mangled name to avoid intercepting __getattr__
        object.__setattr__(self, "_Mem0ReadOnlyAdapter__client", client)
        object.__setattr__(self, "_Mem0ReadOnlyAdapter__log_to_db", log_to_db)

    # ------------------------------------------------------------------
    # Core proxy machinery
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        client = object.__getattribute__(self, "_Mem0ReadOnlyAdapter__client")
        log_to_db_flag = object.__getattribute__(self, "_Mem0ReadOnlyAdapter__log_to_db")

        if _is_blocked(name):
            def _blocked(*args: Any, **kwargs: Any) -> None:
                raise Mem0WriteBlockedError(name)
            return _blocked

        # Retrieve the underlying method
        underlying = getattr(client, name)

        if not callable(underlying):
            return underlying

        # Wrap the method to inject sentinel + logging
        def _read_proxy(*args: Any, **kwargs: Any) -> Any:
            # Inject the mem0 context sentinel into this frame's locals.
            # db.assert_postgres_authority() walks the call stack to detect it.
            __tala_mem0_ctx__ = True  # noqa: F841  # sentinel — DO NOT REMOVE

            logger.info(
                "mem0 read call: %s  args=%r kwargs=%r",
                name, args, kwargs,
            )

            result = underlying(*args, **kwargs)

            # Log to PostgreSQL asynchronously (best-effort, non-blocking)
            if log_to_db_flag:
                _schedule_log(
                    level="INFO",
                    message=f"mem0.{name} called",
                    trace={
                        "method": name,
                        "args_repr": repr(args)[:200],
                        "kwargs_repr": repr(kwargs)[:200],
                    },
                )
            return result

        async def _async_read_proxy(*args: Any, **kwargs: Any) -> Any:
            __tala_mem0_ctx__ = True  # noqa: F841  # sentinel — DO NOT REMOVE

            logger.info(
                "mem0 async read call: %s  args=%r kwargs=%r",
                name, args, kwargs,
            )

            result = await underlying(*args, **kwargs)

            if log_to_db_flag:
                _schedule_log(
                    level="INFO",
                    message=f"mem0.{name} called (async)",
                    trace={
                        "method": name,
                        "args_repr": repr(args)[:200],
                        "kwargs_repr": repr(kwargs)[:200],
                    },
                )
            return result

        # Return the appropriate proxy depending on whether underlying is a coroutine function
        if inspect.iscoroutinefunction(underlying):
            return _async_read_proxy
        return _read_proxy

    # Prevent setting attributes on the adapter itself (enforce immutability)
    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_Mem0ReadOnlyAdapter__"):
            object.__setattr__(self, name, value)
        else:
            raise AttributeError(
                f"Mem0ReadOnlyAdapter is immutable — cannot set '{name}'"
            )

    def __repr__(self) -> str:
        client = object.__getattribute__(self, "_Mem0ReadOnlyAdapter__client")
        return f"<Mem0ReadOnlyAdapter wrapping {type(client).__name__}>"


# ---------------------------------------------------------------------------
# DB log scheduling helper
# ---------------------------------------------------------------------------

def _schedule_log(level: str, message: str, trace: dict[str, Any]) -> None:
    """
    Fire-and-forget: schedule a ``db.log_to_db()`` call on the running loop.

    Swallows all errors so a DB outage never disrupts the mem0 read path.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # No running loop — skip DB logging

    async def _do_log() -> None:
        try:
            from . import db
            await db.log_to_db(level, message, trace)
        except Exception as exc:  # noqa: BLE001
            logger.debug("mem0_adapter: failed to write to agent_log: %s", exc)

    loop.create_task(_do_log())


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def wrap_mem0_client(client: Any, *, log_to_db: bool = True) -> Mem0ReadOnlyAdapter:
    """
    Wrap *client* in a :class:`Mem0ReadOnlyAdapter`.

    This is the canonical way to obtain a safe mem0 client throughout the
    Tala harness.  Never use a raw mem0 client directly.

    Parameters
    ----------
    client:
        An instantiated mem0 ``Memory`` object.
    log_to_db:
        Whether to log reads to the ``agent_log`` table (default: ``True``).

    Returns
    -------
    Mem0ReadOnlyAdapter
    """
    adapter = Mem0ReadOnlyAdapter(client, log_to_db=log_to_db)
    logger.info("mem0 client wrapped in Mem0ReadOnlyAdapter (write-blocking active)")
    return adapter

```
