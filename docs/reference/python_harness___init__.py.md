# File: `__init__.py`

## Module Description

Tala Cognitive Engine Room — harness package
============================================
Python backend harness for the Tala Cognitive Engine Room.

Sub-modules
-----------
config        — Typed configuration via dataclass + dotenv
db            — asyncpg PostgreSQL pool, migrations, query helpers
token_counter — Token counting interceptor and wallet gate
mem0_adapter  — Read-only mem0 wrapper (write-blocking enforcement)
server        — JSON-RPC 2.0 WebSocket server (entry point)

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
"""
Tala Cognitive Engine Room — harness package
============================================
Python backend harness for the Tala Cognitive Engine Room.

Sub-modules
-----------
config        — Typed configuration via dataclass + dotenv
db            — asyncpg PostgreSQL pool, migrations, query helpers
token_counter — Token counting interceptor and wallet gate
mem0_adapter  — Read-only mem0 wrapper (write-blocking enforcement)
server        — JSON-RPC 2.0 WebSocket server (entry point)
"""

from .config import settings, Config, load_config
from .db import (
    init_pool,
    close_pool,
    get_pool,
    execute,
    fetch,
    fetchrow,
    fetchval,
    transaction,
    log_to_db,
    assert_postgres_authority,
)
from .token_counter import (
    count_and_check,
    count_tokens,
    get_wallet_balance,
    top_up_wallet,
    InsufficientTokenBalanceError,
    TokenCheckResult,
)
from .mem0_adapter import (
    Mem0ReadOnlyAdapter,
    wrap_mem0_client,
    Mem0WriteBlockedError,
)

__all__ = [
    # config
    "settings",
    "Config",
    "load_config",
    # db
    "init_pool",
    "close_pool",
    "get_pool",
    "execute",
    "fetch",
    "fetchrow",
    "fetchval",
    "transaction",
    "log_to_db",
    "assert_postgres_authority",
    # token_counter
    "count_and_check",
    "count_tokens",
    "get_wallet_balance",
    "top_up_wallet",
    "InsufficientTokenBalanceError",
    "TokenCheckResult",
    # mem0_adapter
    "Mem0ReadOnlyAdapter",
    "wrap_mem0_client",
    "Mem0WriteBlockedError",
]

```
