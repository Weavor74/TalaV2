"""
python.harness.inference_policy — max_tokens and model matching policy

Extracted from server.py to meet size ratchet.
Handles provider-specific token budgets and model ID matching logic.
"""
from __future__ import annotations

import re
from typing import Any


def adjust_max_tokens_for_provider(
    max_tokens: int,
    provider_type: str,
    model: str,
    provider_config: dict[str, Any],
) -> int:
    """
    Adjust max_tokens based on provider type and model characteristics.
    
    Remote providers get 4096 default (safe for thinking models).
    Local thinking models get 2048 minimum.
    Per-provider max_tokens config overrides everything.
    """
    model_lower = model.lower()
    
    # Word-bounded pattern matching for o1/o3 to avoid false matches
    is_reasoning_model = (
        re.search(r'\b(o1|o3)\b', model_lower) or
        any(k in model_lower for k in ("gemma4", "gemini", "reasoning", "deepseek", "thought", "thinking"))
    )
    
    # Provider-specific max_tokens override
    provider_max_tokens = provider_config.get("max_tokens")
    if provider_max_tokens:
        return int(provider_max_tokens)
    elif provider_type == "remote":
        # Remote providers default to 4096 to handle thinking models safely
        return max(max_tokens, 4096)
    elif is_reasoning_model:
        # Local thinking models get 2048 minimum
        return max(max_tokens, 2048)
    else:
        return max_tokens


def model_id_matches(api_model_ids: list[str], config_model: str) -> bool:
    """
    Check if config_model matches any ID from the API's /models list.
    
    Handles prefix patterns like Gemini's 'models/gemini-3.8-flash'
    where config has bare 'gemini-3.8-flash'.
    """
    if not config_model or not api_model_ids:
        return False
    
    # Exact match
    if config_model in api_model_ids:
        return True
    
    # Prefix match (e.g. 'models/gemini-3.8-flash' matches 'gemini-3.8-flash')
    config_base = config_model.split(':')[0]
    return any(
        mid.endswith(f"/{config_model}") or mid.endswith(f"/{config_base}")
        for mid in api_model_ids
    )
