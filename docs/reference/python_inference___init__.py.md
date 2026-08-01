# File: `__init__.py`

## Module Description

Tala Cognitive Engine Room — inference package
===============================================
LLM inference engine wrappers for the Tala harness.

Sub-modules
-----------
vllm_engine   — vLLM AsyncLLMEngine wrapper with stub fallback

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
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

```
