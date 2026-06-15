"""
Tala Cognitive Engine Room — inference package
===============================================
LLM inference engine wrappers for the Tala harness.

Sub-modules
-----------
vllm_engine   — vLLM AsyncLLMEngine wrapper with stub fallback
"""

from .vllm_engine import (
    generate,
    get_model_info,
    is_vllm_available,
)

__all__ = [
    "generate",
    "get_model_info",
    "is_vllm_available",
]
