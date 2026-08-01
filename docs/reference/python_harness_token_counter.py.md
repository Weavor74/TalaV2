# File: `token_counter.py`

## Module Description

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

## Classes

### `class InsufficientTokenBalanceError`
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

- **`def __init__(self, required_cost, current_balance, token_count, session_id)`**

### `class TokenCountError`
Raised when token counting itself fails in an unrecoverable way.


### `class TokenCheckResult`
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


## Functions

### `def count_tokens(text)`
Count the number of tokens in *text*.

Returns
-------
(token_count, encoding_name)
    Where *encoding_name* is ``'cl100k_base'`` when tiktoken is available,
    or ``'estimate'`` when falling back to word-split heuristic.

### `async def _get_system_connection_mode()`
Return the current system connection mode (``"local"`` or ``"vps"``).

Reads from the ``client_settings`` PostgreSQL table (key
``"chain.connection"``).  Falls back to ``"local"`` on any error so that
a DB outage never accidentally enables real-token billing.

### `async def count_and_check(prompt, session_id, model, completion_tokens, resource_owner)`
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

### `async def get_wallet_balance()`
Return the current TalaCoin balance from the local blockchain node.

Returns ``999999.0`` (ghost balance) when the system is in local mode
or when the blockchain node is unreachable.

### `async def top_up_wallet(amount, caller)`
Mint new TalaCoins on the blockchain (operator top-up).

This calls the ``/chain/mint`` endpoint on the local blockchain node and
updates the ``agent_log`` for auditing.  Returns ``0.0`` if the mint
fails or the system is in local mode.

## Source Code

```python
"""\ntoken_counter.py — Tala Cognitive Engine Room: Token Counting Interceptor\n=========================================================================\nEvery prompt MUST pass through this module before reaching vLLM.\n\nResponsibilities\n----------------\n1. Count tokens using ``tiktoken`` (cl100k_base encoding as universal fallback).\n2. Compute cost: ``cost_usd = token_count * TOKEN_COST_PER_TOKEN``\n   (default: 0.0000001 USD per token — one ten-millionth).\n3. Determine token mode based on system connection state:\n\n   **Ghost Token mode** (local-only system)\n     When the system connection mode is ``"local"`` (i.e. the operator chose\n     "Stay Local" and has not connected to a VPS peer), ALL interactions use\n     ghost tokens — an unlimited internal accounting unit that never touches\n     the blockchain.  This gives a completely friction-free offline experience.\n     The token_ledger still records every call for local audit purposes.\n\n   **Real Token mode** (connected system)\n     When the system connection mode is ``"vps"`` AND the inference call\n     consumes a remote peer's resource (``resource_owner`` refers to a\n     different wallet address), a signed payment transaction is submitted to\n     the Tala PoA blockchain node running at ``TALA_CHAIN_BIND_HOST:PORT``.\n     The counterparty receives the payment minus the configured house fee.\n     If the real blockchain balance is insufficient an\n     :class:`InsufficientTokenBalanceError` is raised.\n\n     Local-origin calls (where ``resource_owner`` matches the operator's own\n     wallet, or is absent) still use ghost tokens even in VPS mode — only\n     cross-node resource consumption settles on-chain.\n\n4. Raise :class:`InsufficientTokenBalanceError` if a real-token balance check\n   fails (never raised in ghost-token mode).\n5. Log every count to the ``token_ledger`` table (append-only accounting).\n6. Return a :class:`TokenCheckResult` to the caller.\n\nMilestone 1.2 — Task 1.2.3\n"""\n\nfrom __future__ import annotations\n\nimport asyncio\nimport logging\nimport time\nimport os\nimport json\nimport urllib.request\nimport uuid\nimport hashlib\nfrom dataclasses import dataclass, field\nfrom typing import Any\n\nlogger = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Tiktoken initialisation (with graceful fallback)\n# ---------------------------------------------------------------------------\n\ntry:\n    import tiktoken\n\n    _ENCODING = tiktoken.get_encoding("cl100k_base")\n    _TIKTOKEN_AVAILABLE = True\n    logger.debug("tiktoken loaded with cl100k_base encoding")\nexcept ImportError:  # pragma: no cover\n    _TIKTOKEN_AVAILABLE = False\n    _ENCODING = None  # type: ignore[assignment]\n    logger.warning(\n        "tiktoken is not installed — falling back to whitespace-split token estimation. "\n        "Install tiktoken for accurate counts: pip install tiktoken"\n    )\n\n\n# ---------------------------------------------------------------------------\n# Custom exceptions\n# ---------------------------------------------------------------------------\n\nclass InsufficientTokenBalanceError(Exception):\n    """\n    Raised when the wallet's real TalaCoin balance cannot cover the cost of\n    a cross-node inference call.\n\n    This error is never raised in ghost-token (local) mode — it only fires\n    when the system is connected to a peer network and the requested resource\n    is owned by a different wallet.\n\n    Attributes\n    ----------\n    required_cost : float\n        USD cost of the rejected request.\n    current_balance : float\n        Current wallet balance at the time of the check.\n    token_count : int\n        Number of tokens in the prompt that triggered this error.\n    """\n\n    def __init__(\n        self,\n        required_cost: float,\n        current_balance: float,\n        token_count: int,\n        session_id: str = "",\n    ) -> None:\n        self.required_cost = required_cost\n        self.current_balance = current_balance\n        self.token_count = token_count\n        self.session_id = session_id\n        super().__init__(\n            f"Insufficient AI token balance.  "\n            f"Required: ${required_cost:.10f} USD ({token_count} tokens)  "\n            f"Available: ${current_balance:.10f} USD  "\n            f"Session: {session_id or 'unknown'}"\n        )\n\n\nclass TokenCountError(Exception):\n    """Raised when token counting itself fails in an unrecoverable way."""\n\n\n# ---------------------------------------------------------------------------\n# Ghost token sentinel balance\n# ---------------------------------------------------------------------------\n\n# Any balance returned in ghost-token mode.  Effectively unlimited — the\n# value is large enough to never trigger InsufficientTokenBalanceError while\n# remaining representable as a normal float.\n_GHOST_BALANCE: float = 999_999.0\n\n\n# ---------------------------------------------------------------------------\n# Result dataclass\n# ---------------------------------------------------------------------------\n\n@dataclass(frozen=True, slots=True)\nclass TokenCheckResult:\n    """\n    Immutable result returned by :func:`count_and_check`.\n\n    Fields\n    ------\n    prompt_tokens:\n        Number of tokens counted in the prompt.\n    cost_usd:\n        Computed cost in USD (or equivalent ghost-token units).\n    balance_before:\n        Wallet balance *before* deducting this cost.\n        ``999999.0`` indicates ghost-token mode.\n    balance_after:\n        Wallet balance *after* deducting this cost (written to DB).\n        Equal to ``balance_before`` in ghost-token mode.\n    session_id:\n        Caller-supplied session identifier.\n    encoding:\n        Name of the encoding used (e.g. ``'cl100k_base'`` or ``'estimate'``).\n    elapsed_ms:\n        Wall-clock time taken to complete the check, in milliseconds.\n    ledger_id:\n        Primary key of the inserted ``token_ledger`` row.\n    ghost_mode:\n        ``True`` when ghost tokens were used (local system or own-wallet call);\n        ``False`` when a real blockchain payment was submitted.\n    """\n\n    prompt_tokens: int\n    cost_usd: float\n    balance_before: float\n    balance_after: float\n    session_id: str\n    encoding: str\n    elapsed_ms: float\n    ledger_id: int = field(default=0)\n    ghost_mode: bool = field(default=True)\n\n\n# ---------------------------------------------------------------------------\n# Core functions\n# ---------------------------------------------------------------------------\n\ndef count_tokens(text: str) -> tuple[int, str]:\n    """\n    Count the number of tokens in *text*.\n\n    Returns\n    -------\n    (token_count, encoding_name)\n        Where *encoding_name* is ``'cl100k_base'`` when tiktoken is available,\n        or ``'estimate'`` when falling back to word-split heuristic.\n    """\n    if _TIKTOKEN_AVAILABLE and _ENCODING is not None:\n        tokens = _ENCODING.encode(text)\n        return len(tokens), "cl100k_base"\n\n    # Heuristic fallback: split on whitespace, count words, multiply by 1.3\n    # (rough approximation of sub-word tokenisation overhead)\n    estimated = max(1, int(len(text.split()) * 1.3))\n    return estimated, "estimate"\n\n\nasync def _get_system_connection_mode() -> str:\n    """\n    Return the current system connection mode (``"local"`` or ``"vps"``).\n\n    Reads from the ``client_settings`` PostgreSQL table (key\n    ``"chain.connection"``).  Falls back to ``"local"`` on any error so that\n    a DB outage never accidentally enables real-token billing.\n    """\n    try:\n        from . import db\n        row = await db.fetchrow(\n            "SELECT value FROM client_settings WHERE key = 'chain.connection'"\n        )\n        if not row:\n            return "local"\n        value = row["value"]\n        if isinstance(value, str):\n            value = json.loads(value)\n        return str(value.get("mode", "local")) if isinstance(value, dict) else "local"\n    except Exception as exc:\n        logger.debug("Could not read system connection mode: %s — defaulting to local", exc)\n        return "local"\n\n\nasync def count_and_check(\n    prompt: str,\n    session_id: str,\n    model: str = "",\n    completion_tokens: int = 0,\n    resource_owner: str | None = None,\n) -> TokenCheckResult:\n    """\n    Count tokens in *prompt*, determine token mode, optionally deduct cost,\n    and log to the immutable ``token_ledger``.\n\n    This is the **mandatory gate** before any inference call.\n\n    Token Mode Decision\n    -------------------\n    Ghost tokens are used when **either** of the following is true:\n\n    * The system connection mode is ``"local"`` (operator chose Stay Local).\n    * ``resource_owner`` is absent or matches the operator's own wallet\n      address (consuming your own resources, even on a connected system).\n\n    Real tokens (blockchain payment) are used **only** when:\n\n    * The system connection mode is ``"vps"``, **AND**\n    * ``resource_owner`` refers to a different peer's wallet address.\n\n    Parameters\n    ----------\n    prompt:\n        The raw text that will be sent to the model.\n    session_id:\n        Caller-supplied session or request identifier for ledger traceability.\n    model:\n        Model name to record in the ledger (informational only).\n    completion_tokens:\n        If known in advance, include for accurate cost accounting.\n    resource_owner:\n        Wallet address or ``@username`` of the peer whose resource is being\n        consumed.  ``None`` or own wallet → ghost tokens.\n\n    Returns\n    -------\n    TokenCheckResult\n\n    Raises\n    ------\n    InsufficientTokenBalanceError\n        Only in real-token mode, if the blockchain balance is insufficient.\n    TokenCountError\n        If counting fails for an unexpected reason.\n    """\n    from . import db  # local import to avoid circular dependency at module level\n\n    t0 = time.perf_counter()\n\n    # -- 1. Count tokens --------------------------------------------------\n    try:\n        token_count, encoding_name = count_tokens(prompt)\n    except Exception as exc:\n        raise TokenCountError(f"Token counting failed: {exc}") from exc\n\n    total_tokens = token_count + completion_tokens\n    from .config import settings\n    cost_usd = total_tokens * settings.TOKEN_COST_PER_TOKEN\n\n    logger.debug(\n        "Token count: %d tokens (encoding=%s) cost=$%.10f session=%s",\n        total_tokens, encoding_name, cost_usd, session_id,\n    )\n\n    # -- 2. Determine token mode ------------------------------------------\n    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")\n    chain_port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")\n    chain_host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")\n    base_url = f"http://{chain_host}:{chain_port}"\n\n    # Resolve connection mode first (defaults to local on any DB error).\n    system_mode = await _get_system_connection_mode()\n    system_is_local = (system_mode != "vps")\n\n    # A call is "own resource" when resource_owner is absent, matches the\n    # operator's wallet, or is a @-reference to the operator's own username.\n    owner = (resource_owner or "").strip()\n    own_wallet_refs = {wallet_address, f"@{wallet_address}", "", "local-client", "@local-tala"}\n    resource_is_own = owner in own_wallet_refs or not owner\n\n    # Ghost mode: local system OR consuming own resources.\n    use_ghost_tokens = system_is_local or resource_is_own\n\n    logger.debug(\n        "Token mode: %s (system_mode=%s, resource_owner=%r)",\n        "ghost" if use_ghost_tokens else "real",\n        system_mode,\n        resource_owner,\n    )\n\n    # -- 3. Check and optionally deduct balance ---------------------------\n    cost_tokens = cost_usd\n    loop = asyncio.get_running_loop()\n\n    def check_balance_sync() -> float:\n        req = urllib.request.Request(f"{base_url}/chain/wallet/{wallet_address}/balance")\n        with urllib.request.urlopen(req) as response:\n            data = json.loads(response.read().decode("utf-8"))\n            return float(data.get("balance", 0.0))\n\n    def submit_payment_sync() -> dict:\n        tx_id = str(uuid.uuid4())\n        amount = cost_tokens\n\n        counterparty = owner\n        house_fee_percent = 0.0\n\n        # Resolve @username → wallet address via chain peer directory\n        if owner.startswith("@"):\n            try:\n                import urllib.parse\n                peer_req = urllib.request.Request(\n                    f"{base_url}/chain/peers/{urllib.parse.quote(owner)}"\n                )\n                with urllib.request.urlopen(peer_req) as response:\n                    peer_data = json.loads(response.read().decode("utf-8"))\n                    if peer_data.get("peer") and peer_data["peer"].get("wallet_address"):\n                        counterparty = peer_data["peer"]["wallet_address"]\n            except Exception as e:\n                logger.warning("Failed to resolve peer %s: %s", owner, e)\n\n        # Apply 10 % house cut for using another peer's resources\n        if counterparty != wallet_address:\n            house_fee_percent = 0.10\n\n        raw_str = f"payment:{wallet_address}:{counterparty}:{amount}:{int(time.time() * 1000)}"\n        tx_hash = hashlib.sha256(raw_str.encode()).hexdigest()\n\n        from . import wallet as _wallet\n        private_key = _wallet.get_private_key()\n        signature_bytes = private_key.sign(tx_hash.encode("utf-8"))\n        signature_hex = signature_bytes.hex()\n\n        payload = {\n            "id": tx_id,\n            "type": "payment",\n            "hash": tx_hash,\n            "creator": wallet_address,\n            "counterparty": counterparty,\n            "metadata": {"amount": amount, "house_fee_percent": house_fee_percent},\n            "signature": signature_hex,\n            "public_key": wallet_address,\n        }\n        req = urllib.request.Request(\n            f"{base_url}/chain/tx",\n            data=json.dumps(payload).encode("utf-8"),\n            headers={"Content-Type": "application/json"},\n            method="POST",\n        )\n        with urllib.request.urlopen(req) as response:\n            return json.loads(response.read().decode("utf-8"))\n\n    if use_ghost_tokens:\n        # Ghost mode: unlimited local balance, no blockchain call.\n        balance_before = _GHOST_BALANCE\n        balance_after = _GHOST_BALANCE\n    else:\n        # Real mode: read live blockchain balance.\n        try:\n            balance_before = await loop.run_in_executor(None, check_balance_sync)\n        except Exception as exc:\n            logger.warning(\n                "Failed to read blockchain balance — falling back to ghost tokens: %s", exc\n            )\n            balance_before = _GHOST_BALANCE\n            balance_after = _GHOST_BALANCE\n            use_ghost_tokens = True  # Degrade gracefully\n\n        if not use_ghost_tokens:\n            if balance_before < cost_tokens:\n                raise InsufficientTokenBalanceError(\n                    required_cost=cost_tokens,\n                    current_balance=balance_before,\n                    token_count=total_tokens,\n                    session_id=session_id,\n                )\n            try:\n                await loop.run_in_executor(None, submit_payment_sync)\n                balance_after = balance_before - cost_tokens\n            except Exception as exc:\n                logger.warning("Failed to submit blockchain payment: %s", exc)\n                balance_after = balance_before\n\n    # -- 4. Append to local ledger (immutable audit trail) ----------------\n    async with db.transaction() as conn:\n        ledger_row = await conn.fetchrow(\n            """\n            INSERT INTO token_ledger\n                (session_id, prompt_tokens, completion_tokens, cost_usd, model)\n            VALUES ($1, $2, $3, $4, $5)\n            RETURNING id\n            """,\n            session_id,\n            token_count,\n            completion_tokens,\n            cost_tokens,\n            model or "",\n        )\n        ledger_id = ledger_row["id"] if ledger_row else 0\n\n    elapsed_ms = (time.perf_counter() - t0) * 1000.0\n\n    logger.info(\n        "Token gate PASSED  session=%s tokens=%d cost=%.4f mode=%s "\n        "balance=%.4f->%.4f  ledger_id=%d  elapsed=%.1fms",\n        session_id, total_tokens, cost_tokens,\n        "ghost" if use_ghost_tokens else "real",\n        balance_before, balance_after,\n        ledger_id, elapsed_ms,\n    )\n\n    return TokenCheckResult(\n        prompt_tokens=token_count,\n        cost_usd=cost_tokens,\n        balance_before=balance_before,\n        balance_after=balance_after,\n        session_id=session_id,\n        encoding=encoding_name,\n        elapsed_ms=elapsed_ms,\n        ledger_id=ledger_id,\n        ghost_mode=use_ghost_tokens,\n    )\n\n\nasync def get_wallet_balance() -> float:\n    """\n    Return the current TalaCoin balance from the local blockchain node.\n\n    Returns ``999999.0`` (ghost balance) when the system is in local mode\n    or when the blockchain node is unreachable.\n    """\n    system_mode = await _get_system_connection_mode()\n    if system_mode != "vps":\n        return _GHOST_BALANCE\n\n    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")\n    chain_port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")\n    chain_host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")\n    base_url = f"http://{chain_host}:{chain_port}"\n\n    loop = asyncio.get_running_loop()\n\n    def check_balance_sync() -> float:\n        req = urllib.request.Request(f"{base_url}/chain/wallet/{wallet_address}/balance")\n        with urllib.request.urlopen(req) as response:\n            data = json.loads(response.read().decode("utf-8"))\n            return float(data.get("balance", 0.0))\n\n    try:\n        return await loop.run_in_executor(None, check_balance_sync)\n    except Exception as exc:\n        logger.warning("Failed to check blockchain balance: %s", exc)\n        return _GHOST_BALANCE  # Ghost tokens as graceful fallback\n\n\nasync def top_up_wallet(amount: float, caller: str = "system") -> float:\n    """\n    Mint new TalaCoins on the blockchain (operator top-up).\n\n    This calls the ``/chain/mint`` endpoint on the local blockchain node and\n    updates the ``agent_log`` for auditing.  Returns ``0.0`` if the mint\n    fails or the system is in local mode.\n    """\n    from .db import log_to_db\n\n    system_mode = await _get_system_connection_mode()\n    if system_mode != "vps":\n        logger.info("top_up_wallet called in local mode — no-op (ghost tokens are unlimited)")\n        return _GHOST_BALANCE\n\n    wallet_address = os.environ.get("TALA_WALLET_ADDRESS", "local-client")\n... (file truncated, showing first 500 lines) ...
```
