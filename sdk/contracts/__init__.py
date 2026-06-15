"""
sdk/contracts/__init__.py — Tala Cognitive Engine Room
=======================================================
Smart Contract & Messaging Protocol layer (Milestone 3).

Public surface
--------------
- :class:`PreflightEngine`   — Micro-Token Pre-Flight engine (Task 3.1.x)
- :class:`ContractRouter`    — Smart Contract Proxy Router  (Task 3.2.x)
- :class:`HandshakeProtocol` — P2P Handshake Matrix         (Task 3.3.x)

All three classes work together to form the contract lifecycle:

    1. ``PreflightEngine.run_preflight()``  → verify wallet before any LLM call
    2. ``ContractRouter.broadcast()``       → create & hash a new smart contract
    3. ``HandshakeSender.prepare_packet()`` → strip PII, sign, and send
    4. ``HandshakeReceiver.receive_packet()``→ verify and reconstruct locally
    5. ``AtomicSettlement.settle()``        → release payment, close contract

The ``HandshakeProtocol`` convenience alias bundles the sender + receiver
into a single import for callers that need the full handshake pair.

Milestone 3 — Tasks 3.1.1 – 3.3.3
"""

from __future__ import annotations

from .handshake import HandshakePacket, HandshakeReceiver, HandshakeSender, AtomicSettlement
from .preflight import PreflightEngine, PreflightResult, InsufficientEscrowError
from .router import ContractRouter, ContractState, SmartContract


# ---------------------------------------------------------------------------
# Convenience alias: HandshakeProtocol groups sender + receiver pair
# ---------------------------------------------------------------------------

class HandshakeProtocol:
    """
    Convenience namespace exposing the full P2P handshake surface.

    Attributes
    ----------
    Sender : HandshakeSender
        Prepares and signs outbound packets.
    Receiver : HandshakeReceiver
        Verifies and reconstructs inbound packets.
    Settlement : AtomicSettlement
        Atomically settles a contract after successful delivery.
    Packet : HandshakePacket
        The wire-format dataclass for handshake messages.
    """

    Sender = HandshakeSender
    Receiver = HandshakeReceiver
    Settlement = AtomicSettlement
    Packet = HandshakePacket


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    # Primary exports requested by task spec
    "PreflightEngine",
    "ContractRouter",
    "HandshakeProtocol",
    # Supporting types (available on the package namespace for convenience)
    "PreflightResult",
    "InsufficientEscrowError",
    "ContractState",
    "SmartContract",
    "HandshakePacket",
    "HandshakeSender",
    "HandshakeReceiver",
    "AtomicSettlement",
]
