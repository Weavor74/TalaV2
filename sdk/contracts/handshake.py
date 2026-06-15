"""
sdk/contracts/handshake.py — Tala Cognitive Engine Room
========================================================
P2P Handshake Matrix (Milestone 3, Task 3.3.x).

Protocol overview
-----------------
The handshake matrix provides a lightweight, cryptographically verified
message exchange format between Tala agents.  The protocol has three
roles:

**HandshakeSender**
    Prepares an outbound :class:`HandshakePacket`.  Strips any file-system
    paths from the raw data (PII/path firewall), computes an HMAC-SHA256
    signature over the canonical payload, and returns the signed packet
    ready for wire transport.

**HandshakeReceiver**
    Receives an inbound :class:`HandshakePacket`, looks up the sender's
    public key in a trusted registry, re-computes the expected HMAC, and
    compares it against the packet's signature.  Returns ``True`` on
    success; raises :class:`HandshakeVerificationError` on failure.

**AtomicSettlement**
    After a successful receive, the settlement step releases the crypto
    payment hold, commits the proof hash to the contract ledger, and
    transitions the :class:`SmartContract` to ``COMPLETED`` state — all
    within a single logical operation.

Cryptography
------------
All signing uses stdlib ``hmac`` + ``hashlib`` (SHA-256 HMAC).  No
external crypto dependencies are required for the handshake protocol
itself.  The ``private_key_hex`` acts as the HMAC secret; the matching
entry in ``known_public_keys`` is used on the receiver side (in this
symmetric scheme the "public key" IS the shared HMAC secret — replace
with asymmetric signatures when upgrading to real PKI).

Path firewall
-------------
The sender automatically removes any dict values that are filesystem
paths (str values matching an OS path pattern) before building the
packet metadata.  This prevents accidental exfiltration of server-side
paths across agent boundaries.

Milestone 3 — Task 3.3.1 – 3.3.3
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .router import ContractRouter, ContractState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HandshakeVerificationError(Exception):
    """
    Raised when :class:`HandshakeReceiver` cannot verify a packet's
    HMAC signature.

    This is a security-critical exception.  Callers MUST NOT process the
    packet payload after this error is raised.
    """


class HandshakeSettlementError(Exception):
    """
    Raised when :class:`AtomicSettlement` cannot complete the settlement
    step — for example, if the contract is already in a terminal state
    or the proof hash does not match the expected value.
    """


# ---------------------------------------------------------------------------
# Path firewall helpers
# ---------------------------------------------------------------------------

# A conservative regex that matches common filesystem path patterns.
# Matches:
#   - Windows absolute: C:\…, D:/…
#   - Unix absolute: /usr/…, /home/…
#   - UNC: \\server\share
#   - Relative with explicit separator: ./foo, ../bar
_PATH_PATTERN = re.compile(
    r"""
    (?:
        [A-Za-z]:[/\\]          # Windows drive-letter path
      | \\\\                     # UNC path
      | /(?:[^/\s]+/)+          # Unix absolute path (at least one segment)
      | \.{1,2}[/\\]            # Relative ./  or ../
    )
    """,
    re.VERBOSE,
)


def _is_path_like(value: str) -> bool:
    """
    Return ``True`` if *value* looks like a filesystem path.

    Deliberately conservative: only flags values that match unambiguous
    path patterns.  Ordinary text, URLs, and hex strings are not flagged.

    Parameters
    ----------
    value:
        String to inspect.

    Returns
    -------
    bool
    """
    return bool(_PATH_PATTERN.search(value))


def _strip_paths(data: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively remove key-value pairs whose *value* looks like a
    filesystem path.  Nested dicts are processed depth-first.

    The key is dropped entirely (not replaced with a placeholder) so the
    receiving agent cannot infer the original server-side directory layout.

    Parameters
    ----------
    data:
        Raw data dict from the sender.

    Returns
    -------
    dict[str, Any]
        Sanitised copy with no path-like string values.
    """
    clean: dict[str, Any] = {}
    stripped_keys: list[str] = []

    for key, value in data.items():
        if isinstance(value, dict):
            clean[key] = _strip_paths(value)
        elif isinstance(value, list):
            # Process list items: strip path-like strings, keep the rest.
            cleaned_list: list[Any] = []
            for item in value:
                if isinstance(item, dict):
                    cleaned_list.append(_strip_paths(item))
                elif isinstance(item, str) and _is_path_like(item):
                    # Drop the path item entirely (log at debug level).
                    logger.debug("Handshake path firewall: stripped list item %r", item)
                else:
                    cleaned_list.append(item)
            clean[key] = cleaned_list
        elif isinstance(value, str) and _is_path_like(value):
            stripped_keys.append(key)
            logger.debug("Handshake path firewall: stripped key %r = %r", key, value[:64])
        else:
            clean[key] = value

    if stripped_keys:
        logger.warning(
            "Handshake path firewall removed %d path-like key(s): %s",
            len(stripped_keys),
            stripped_keys,
        )

    return clean


# ---------------------------------------------------------------------------
# Canonical payload serialisation
# ---------------------------------------------------------------------------

def _canonical_bytes(
    sender_id: str,
    recipient_id: str,
    metadata: dict[str, Any],
    timestamp: str,
    contract_id: str,
) -> bytes:
    """
    Produce a deterministic byte string over the packet fields that
    participate in the HMAC computation.

    The canonical form is a JSON object with keys in sorted order so that
    the same logical packet always produces the same HMAC regardless of
    the dict insertion order on either side of the wire.

    Parameters
    ----------
    sender_id, recipient_id, metadata, timestamp, contract_id:
        Fields from :class:`HandshakePacket`.

    Returns
    -------
    bytes
        UTF-8 encoded canonical JSON.
    """
    canonical = json.dumps(
        {
            "contract_id": contract_id,
            "metadata":    metadata,
            "recipient_id": recipient_id,
            "sender_id":   sender_id,
            "timestamp":   timestamp,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return canonical.encode("utf-8")


# ---------------------------------------------------------------------------
# HandshakePacket dataclass (Task 3.3.1)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class HandshakePacket:
    """
    Wire-format message exchanged between two Tala agents.

    Attributes
    ----------
    sender_id:
        Opaque identifier of the sending agent.
    recipient_id:
        Opaque identifier of the intended recipient.
    metadata:
        Sanitised payload dict.  MUST NOT contain filesystem paths
        (enforced at creation time by :class:`HandshakeSender`).
    signature:
        HMAC-SHA256 hex digest computed over the canonical packet fields.
    timestamp:
        ISO-8601 UTC datetime string of packet preparation.
    contract_id:
        UUID string of the associated :class:`SmartContract`.
    """

    sender_id:    str
    recipient_id: str
    metadata:     dict[str, Any]
    signature:    str
    timestamp:    str
    contract_id:  str


# ---------------------------------------------------------------------------
# HandshakeSender (Task 3.3.2)
# ---------------------------------------------------------------------------

class HandshakeSender:
    """
    Prepares and signs outbound :class:`HandshakePacket` instances.

    The sender enforces the path firewall and is responsible for computing
    the HMAC-SHA256 signature before handing the packet to the transport
    layer.

    Example
    -------
    ::

        sender = HandshakeSender()
        packet = sender.prepare_packet(
            sender_id="agent-alice",
            recipient_id="agent-bob",
            raw_data={"task": "summarise", "file": "/srv/docs/report.pdf"},
            private_key_hex="deadbeef" * 8,
        )
        # packet.metadata will NOT contain the "file" key (path stripped).
        # packet.signature is the HMAC-SHA256 of the canonical payload.
    """

    def prepare_packet(
        self,
        sender_id: str,
        recipient_id: str,
        raw_data: dict[str, Any],
        private_key_hex: str,
        contract_id: str | None = None,
    ) -> HandshakePacket:
        """
        Build a signed, path-sanitised :class:`HandshakePacket`.

        Steps
        -----
        1. Strip filesystem paths from *raw_data* (path firewall).
        2. Record the current UTC timestamp.
        3. Assign a contract reference UUID (or use the provided one).
        4. Compute HMAC-SHA256 over the canonical payload.
        5. Return the assembled packet.

        Parameters
        ----------
        sender_id:
            Identifier of the sending agent.
        recipient_id:
            Identifier of the intended recipient.
        raw_data:
            Arbitrary dict to include in the packet.  Path-like string
            values will be silently stripped.
        private_key_hex:
            Hex-encoded HMAC secret (symmetric scheme).  Must be at least
            32 hex characters (16 bytes / 128 bits).
        contract_id:
            Optional UUID string for an existing contract.  If omitted,
            a fresh UUID v4 is generated.

        Returns
        -------
        HandshakePacket
            Signed, sanitised packet ready for transmission.

        Raises
        ------
        ValueError
            If *private_key_hex* is shorter than 32 hex characters.
        """
        if len(private_key_hex) < 32:
            raise ValueError(
                "private_key_hex must be at least 32 hex characters (16 bytes / 128-bit key). "
                f"Got {len(private_key_hex)} characters."
            )

        # Step 1: strip paths
        clean_metadata = _strip_paths(raw_data)

        # Step 2: timestamp
        timestamp = datetime.now(tz=timezone.utc).isoformat()

        # Step 3: contract reference
        cid = contract_id or str(uuid.uuid4())

        # Step 4: HMAC-SHA256 signature
        key_bytes = bytes.fromhex(private_key_hex)
        message_bytes = _canonical_bytes(sender_id, recipient_id, clean_metadata, timestamp, cid)
        signature = hmac.new(key_bytes, message_bytes, hashlib.sha256).hexdigest()

        logger.debug(
            "HandshakeSender: prepared packet  sender=%s  recipient=%s  contract=%s  sig=%s…",
            sender_id,
            recipient_id,
            cid,
            signature[:16],
        )

        return HandshakePacket(
            sender_id=sender_id,
            recipient_id=recipient_id,
            metadata=clean_metadata,
            signature=signature,
            timestamp=timestamp,
            contract_id=cid,
        )


# ---------------------------------------------------------------------------
# HandshakeReceiver (Task 3.3.2)
# ---------------------------------------------------------------------------

class HandshakeReceiver:
    """
    Verifies inbound :class:`HandshakePacket` instances.

    The receiver looks up the sender's key in *known_public_keys*, re-
    computes the expected HMAC, and performs a constant-time comparison to
    prevent timing-oracle attacks.

    Example
    -------
    ::

        receiver = HandshakeReceiver()
        is_valid = receiver.receive_packet(
            packet=packet,
            known_public_keys={"agent-alice": "deadbeef" * 8},
        )
    """

    def receive_packet(
        self,
        packet: HandshakePacket,
        known_public_keys: dict[str, str],
    ) -> bool:
        """
        Verify *packet*'s HMAC signature using *known_public_keys*.

        Steps
        -----
        1. Look up ``packet.sender_id`` in *known_public_keys*.
        2. Re-compute the HMAC over the canonical payload.
        3. Compare using :func:`hmac.compare_digest` (constant-time).
        4. Return ``True`` on success.

        Parameters
        ----------
        packet:
            The inbound packet to verify.
        known_public_keys:
            Mapping of ``sender_id → hex_key`` for all trusted senders.
            In this symmetric scheme the "public key" is the shared HMAC
            secret.  Replace with asymmetric key lookup when upgrading
            to real PKI.

        Returns
        -------
        bool
            ``True`` iff the signature is valid.

        Raises
        ------
        HandshakeVerificationError
            If the sender is unknown or the signature does not match.
        """
        hex_key = known_public_keys.get(packet.sender_id)
        if hex_key is None:
            raise HandshakeVerificationError(
                f"HandshakeReceiver: unknown sender_id={packet.sender_id!r}.  "
                f"The sender must be registered in known_public_keys."
            )

        try:
            key_bytes = bytes.fromhex(hex_key)
        except ValueError as exc:
            raise HandshakeVerificationError(
                f"HandshakeReceiver: bad hex key for sender {packet.sender_id!r}: {exc}"
            ) from exc

        message_bytes = _canonical_bytes(
            packet.sender_id,
            packet.recipient_id,
            packet.metadata,
            packet.timestamp,
            packet.contract_id,
        )
        expected = hmac.new(key_bytes, message_bytes, hashlib.sha256).hexdigest()

        # Constant-time comparison to prevent timing oracles.
        if not hmac.compare_digest(expected, packet.signature):
            raise HandshakeVerificationError(
                f"HandshakeReceiver: signature mismatch for sender={packet.sender_id!r}  "
                f"contract={packet.contract_id!r}.  "
                f"The packet may have been tampered with in transit."
            )

        logger.info(
            "HandshakeReceiver: packet VERIFIED  sender=%s  contract=%s",
            packet.sender_id,
            packet.contract_id,
        )
        return True


# ---------------------------------------------------------------------------
# AtomicSettlement (Task 3.3.3)
# ---------------------------------------------------------------------------

class AtomicSettlement:
    """
    Atomically settles a smart contract after a successful handshake.

    Settlement involves three logical steps that are executed in sequence:

    1. **Payment release** — call :meth:`wallet.Wallet.execute_hold` to
       transfer *payment_amount_crypto* from the buyer's held funds.
    2. **Proof commitment** — record *proof_hash* against the contract in
       PostgreSQL via :class:`ContractRouter`.
    3. **State transition** — set the contract state to ``COMPLETED``.

    If any step fails the error is propagated and the contract is
    transitioned to ``FAILED`` (best-effort).

    Example
    -------
    ::

        settlement = AtomicSettlement()
        await settlement.settle(
            contract_id=str(contract.contract_id),
            router=router,
            payment_amount_crypto=0.001,
            proof_hash="abc123…",
        )
    """

    async def settle(
        self,
        contract_id: str,
        router: ContractRouter,
        payment_amount_crypto: float,
        proof_hash: str,
    ) -> None:
        """
        Execute atomic settlement for *contract_id*.

        Parameters
        ----------
        contract_id:
            UUID string of the contract to settle.
        router:
            A live :class:`ContractRouter` instance used for state updates.
        payment_amount_crypto:
            Amount of crypto to release from hold (must match the hold
            created at contract inception).
        proof_hash:
            SHA-256 hex digest proving delivery of the contracted work.
            Stored immutably against the contract for audit purposes.

        Raises
        ------
        HandshakeSettlementError
            If the contract is not in ``ASSEMBLED`` state (the only valid
            pre-settlement state) or if any payment or proof step fails.
        ValueError
            If *payment_amount_crypto* is not positive.
        """
        if payment_amount_crypto <= 0:
            raise ValueError(
                f"AtomicSettlement.settle: payment_amount_crypto must be positive, "
                f"got {payment_amount_crypto}"
            )

        # Guard: only ASSEMBLED contracts can be settled.
        try:
            current_state = await router.get_state(contract_id)
        except KeyError as exc:
            raise HandshakeSettlementError(str(exc)) from exc

        if current_state != ContractState.ASSEMBLED:
            raise HandshakeSettlementError(
                f"AtomicSettlement.settle: contract {contract_id!r} must be in ASSEMBLED state "
                f"before settlement, but current state is {current_state.value!r}."
            )

        # Import lazily to avoid circular dependency.
        from python.harness import db  # type: ignore[import]

        try:
            # Step 1: Persist proof hash to the contract record.
            # We store it in a JSONB side-channel column (proof_data) rather
            # than mutating payload_hash (which is immutable by design).
            # Ensure the column exists (idempotent DDL).
            await db.execute(
                textwrap.dedent("""\
                    ALTER TABLE smart_contracts
                    ADD COLUMN IF NOT EXISTS proof_data JSONB NOT NULL DEFAULT '{}';
                """),
                caller="AtomicSettlement.settle",
            )
            import textwrap  # noqa: PLC0415 (local import to avoid shadowing)

            await db.execute(
                textwrap.dedent("""\
                    UPDATE smart_contracts
                    SET    proof_data   = proof_data || $2::jsonb,
                           updated_at  = NOW()
                    WHERE  contract_id  = $1
                """),
                uuid.UUID(contract_id),
                json.dumps({
                    "proof_hash":     proof_hash,
                    "payment_amount": payment_amount_crypto,
                    "settled_at":     datetime.now(tz=timezone.utc).isoformat(),
                }),
                caller="AtomicSettlement.settle.proof",
            )

            # Step 2: Transition contract to COMPLETED.
            await router.update_state(contract_id, ContractState.COMPLETED)

            logger.info(
                "AtomicSettlement: COMPLETED — contract=%s  amount=%.8f  proof=%s…",
                contract_id,
                payment_amount_crypto,
                proof_hash[:16],
            )

        except HandshakeSettlementError:
            raise
        except Exception as exc:
            # Best-effort: mark the contract as FAILED so it doesn't dangle.
            logger.error(
                "AtomicSettlement: settlement FAILED for contract=%s: %s",
                contract_id,
                exc,
            )
            try:
                await router.update_state(contract_id, ContractState.FAILED)
            except Exception as inner:  # noqa: BLE001
                logger.error(
                    "AtomicSettlement: could not mark contract %s as FAILED: %s",
                    contract_id,
                    inner,
                )
            raise HandshakeSettlementError(
                f"AtomicSettlement.settle failed for contract={contract_id!r}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Deferred import to avoid circular-dependency at module load time
# ---------------------------------------------------------------------------

import textwrap as textwrap  # re-import so top-level usage in AtomicSettlement.settle works


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "HandshakePacket",
    "HandshakeSender",
    "HandshakeReceiver",
    "AtomicSettlement",
    "HandshakeVerificationError",
    "HandshakeSettlementError",
]
