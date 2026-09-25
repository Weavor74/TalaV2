"""
sdk/contracts/tests/test_preflight.py — Pytest suite for preflight module
"""

from __future__ import annotations

import pytest
from sdk.contracts.preflight import (
    CRYPTO_TO_AI_TOKENS,
    AI_TOKEN_COST_USD,
    _count_characters,
    _estimate_tokens,
    check_escrow,
    PreflightEngine,
    InsufficientEscrowError,
)

def test_constants():
    assert CRYPTO_TO_AI_TOKENS == 10_000_000
    assert AI_TOKEN_COST_USD == 0.0000001

def test_count_characters():
    assert _count_characters("hello") == 5
    assert _count_characters("") == 0
    assert _count_characters("こんにちは") == 5  # Char count, not bytes

def test_estimate_tokens():
    assert _estimate_tokens("1234") == 1  # 4 chars -> 1 token
    assert _estimate_tokens("12345") == 2 # 5 chars -> 2 tokens
    assert _estimate_tokens("") == 1      # min 1 token

def test_check_escrow():
    # 10 tokens requires 10 / 10_000_000 = 0.000001 crypto
    assert check_escrow(0.000001, 10) is True
    assert check_escrow(0.0000009, 10) is False

def test_preflight_engine_approved():
    engine = PreflightEngine()
    result = engine.run_preflight_sync("hello", 1.0)
    assert result.approved is True
    assert result.required_tokens == 2 # 5 chars -> 2 tokens
    assert result.required_crypto == 2 / 10_000_000
    assert result.wallet_balance == 1.0
    assert result.deficit == 0.0

def test_preflight_engine_insufficient_throws():
    engine = PreflightEngine()
    with pytest.raises(InsufficientEscrowError) as exc_info:
        engine.run_preflight_sync("a" * 100, 0.0) # 100 chars -> 25 tokens -> 0.0000025 crypto
    
    assert exc_info.value.payload.get("required") == 25 / 10_000_000
    assert exc_info.value.payload.get("escrowed") == 0.0
