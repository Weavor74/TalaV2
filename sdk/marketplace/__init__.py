"""
sdk/marketplace/__init__.py — Tala Cognitive Engine Room: Marketplace Layer
============================================================================
Public surface of the Marketplace SDK package.

Exports
-------
RemotePointer         — Dataclass for a trusted remote node pointer
RemoteNodeFetcher     — Fetches and stamps remote HTML Neural Nodes
PointerRegistry       — PostgreSQL-backed registry of known remote pointers
TrustLevel            — Enum of trust tiers for peer designation
TrustedShareGateway   — Assigns trust tiers to peer agents
BillingHook           — Attaches fee metadata to ledger schemas
LiquidityClearing     — Atomic hold + clear settlement

Milestone 4 — Identity & Marketplace
"""

from __future__ import annotations

from .billing import BillingHook, LiquidityClearing, TrustLevel, TrustedShareGateway
from .remote import PointerRegistry, RemoteNodeFetcher, RemotePointer

__all__: list[str] = [
    "RemotePointer",
    "RemoteNodeFetcher",
    "PointerRegistry",
    "TrustLevel",
    "TrustedShareGateway",
    "BillingHook",
    "LiquidityClearing",
]
