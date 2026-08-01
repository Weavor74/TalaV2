# File: `__init__.py`

## Module Description

sdk/identity/__init__.py — Tala Cognitive Engine Room: Identity Layer
=====================================================================
Public surface of the Identity SDK package.

Exports
-------
WhoamiNode        — Typed wrapper around a parsed whoami Neural Node
ConnectionRouter  — Alias for the ConnectionRouter class in routing.py

Milestone 4 — Identity & Marketplace

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
"""
sdk/identity/__init__.py — Tala Cognitive Engine Room: Identity Layer
=====================================================================
Public surface of the Identity SDK package.

Exports
-------
WhoamiNode        — Typed wrapper around a parsed whoami Neural Node
ConnectionRouter  — Alias for the ConnectionRouter class in routing.py

Milestone 4 — Identity & Marketplace
"""

from __future__ import annotations

from .routing import ConnectionRouter, FormattingConfig, PeerIdentity
from .whoami import WhoamiNode

__all__: list[str] = [
    "WhoamiNode",
    "ConnectionRouter",
    "PeerIdentity",
    "FormattingConfig",
]

```
