# File: `handshake.py`

## Module Description

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

## Classes

### `class HandshakeVerificationError`
Raised when :class:`HandshakeReceiver` cannot verify a packet's
HMAC signature.

This is a security-critical exception.  Callers MUST NOT process the
packet payload after this error is raised.


### `class HandshakeSettlementError`
Raised when :class:`AtomicSettlement` cannot complete the settlement
step — for example, if the contract is already in a terminal state
or the proof hash does not match the expected value.


### `class HandshakePacket`
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


### `class HandshakeSender`
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

- **`def prepare_packet(self, sender_id, recipient_id, raw_data, private_key_hex, contract_id)`**
  - Build a signed, path-sanitised :class:`HandshakePacket`.
  
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

### `class HandshakeReceiver`
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

- **`def receive_packet(self, packet, known_public_keys)`**
  - Verify *packet*'s HMAC signature using *known_public_keys*.
  
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

### `class AtomicSettlement`
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

- **`def settle(self, contract_id, router, payment_amount_crypto, proof_hash)`**
  - Execute atomic settlement for *contract_id*.
  
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

## Functions

### `def _is_path_like(value)`
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

### `def _strip_paths(data)`
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

### `def _canonical_bytes(sender_id, recipient_id, metadata, timestamp, contract_id)`
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

## Source Code

```python
"""\nsdk/contracts/handshake.py — Tala Cognitive Engine Room\n========================================================\nP2P Handshake Matrix (Milestone 3, Task 3.3.x).\n\nProtocol overview\n-----------------\nThe handshake matrix provides a lightweight, cryptographically verified\nmessage exchange format between Tala agents.  The protocol has three\nroles:\n\n**HandshakeSender**\n    Prepares an outbound :class:`HandshakePacket`.  Strips any file-system\n    paths from the raw data (PII/path firewall), computes an HMAC-SHA256\n    signature over the canonical payload, and returns the signed packet\n    ready for wire transport.\n\n**HandshakeReceiver**\n    Receives an inbound :class:`HandshakePacket`, looks up the sender's\n    public key in a trusted registry, re-computes the expected HMAC, and\n    compares it against the packet's signature.  Returns ``True`` on\n    success; raises :class:`HandshakeVerificationError` on failure.\n\n**AtomicSettlement**\n    After a successful receive, the settlement step releases the crypto\n    payment hold, commits the proof hash to the contract ledger, and\n    transitions the :class:`SmartContract` to ``COMPLETED`` state — all\n    within a single logical operation.\n\nCryptography\n------------\nAll signing uses stdlib ``hmac`` + ``hashlib`` (SHA-256 HMAC).  No\nexternal crypto dependencies are required for the handshake protocol\nitself.  The ``private_key_hex`` acts as the HMAC secret; the matching\nentry in ``known_public_keys`` is used on the receiver side (in this\nsymmetric scheme the "public key" IS the shared HMAC secret — replace\nwith asymmetric signatures when upgrading to real PKI).\n\nPath firewall\n-------------\nThe sender automatically removes any dict values that are filesystem\npaths (str values matching an OS path pattern) before building the\npacket metadata.  This prevents accidental exfiltration of server-side\npaths across agent boundaries.\n\nMilestone 3 — Task 3.3.1 – 3.3.3\n"""\n\nfrom __future__ import annotations\n\nimport hashlib\nimport hmac\nimport json\nimport logging\nimport re\nimport uuid\nfrom dataclasses import dataclass, field\nfrom datetime import datetime, timezone\nfrom typing import Any\n\nfrom .router import ContractRouter, ContractState\n\nlogger = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Errors\n# ---------------------------------------------------------------------------\n\nclass HandshakeVerificationError(Exception):\n    """\n    Raised when :class:`HandshakeReceiver` cannot verify a packet's\n    HMAC signature.\n\n    This is a security-critical exception.  Callers MUST NOT process the\n    packet payload after this error is raised.\n    """\n\n\nclass HandshakeSettlementError(Exception):\n    """\n    Raised when :class:`AtomicSettlement` cannot complete the settlement\n    step — for example, if the contract is already in a terminal state\n    or the proof hash does not match the expected value.\n    """\n\n\n# ---------------------------------------------------------------------------\n# Path firewall helpers\n# ---------------------------------------------------------------------------\n\n# A conservative regex that matches common filesystem path patterns.\n# Matches:\n#   - Windows absolute: C:\…, D:/…\n#   - Unix absolute: /usr/…, /home/…\n#   - UNC: \\server\share\n#   - Relative with explicit separator: ./foo, ../bar\n_PATH_PATTERN = re.compile(\n    r"""\n    (?:\n        [A-Za-z]:[/\\]          # Windows drive-letter path\n      | \\\\                     # UNC path\n      | /(?:[^/\s]+/)+          # Unix absolute path (at least one segment)\n      | \.{1,2}[/\\]            # Relative ./  or ../\n    )\n    """,\n    re.VERBOSE,\n)\n\n\ndef _is_path_like(value: str) -> bool:\n    """\n    Return ``True`` if *value* looks like a filesystem path.\n\n    Deliberately conservative: only flags values that match unambiguous\n    path patterns.  Ordinary text, URLs, and hex strings are not flagged.\n\n    Parameters\n    ----------\n    value:\n        String to inspect.\n\n    Returns\n    -------\n    bool\n    """\n    return bool(_PATH_PATTERN.search(value))\n\n\ndef _strip_paths(data: dict[str, Any]) -> dict[str, Any]:\n    """\n    Recursively remove key-value pairs whose *value* looks like a\n    filesystem path.  Nested dicts are processed depth-first.\n\n    The key is dropped entirely (not replaced with a placeholder) so the\n    receiving agent cannot infer the original server-side directory layout.\n\n    Parameters\n    ----------\n    data:\n        Raw data dict from the sender.\n\n    Returns\n    -------\n    dict[str, Any]\n        Sanitised copy with no path-like string values.\n    """\n    clean: dict[str, Any] = {}\n    stripped_keys: list[str] = []\n\n    for key, value in data.items():\n        if isinstance(value, dict):\n            clean[key] = _strip_paths(value)\n        elif isinstance(value, list):\n            # Process list items: strip path-like strings, keep the rest.\n            cleaned_list: list[Any] = []\n            for item in value:\n                if isinstance(item, dict):\n                    cleaned_list.append(_strip_paths(item))\n                elif isinstance(item, str) and _is_path_like(item):\n                    # Drop the path item entirely (log at debug level).\n                    logger.debug("Handshake path firewall: stripped list item %r", item)\n                else:\n                    cleaned_list.append(item)\n            clean[key] = cleaned_list\n        elif isinstance(value, str) and _is_path_like(value):\n            stripped_keys.append(key)\n            logger.debug("Handshake path firewall: stripped key %r = %r", key, value[:64])\n        else:\n            clean[key] = value\n\n    if stripped_keys:\n        logger.warning(\n            "Handshake path firewall removed %d path-like key(s): %s",\n            len(stripped_keys),\n            stripped_keys,\n        )\n\n    return clean\n\n\n# ---------------------------------------------------------------------------\n# Canonical payload serialisation\n# ---------------------------------------------------------------------------\n\ndef _canonical_bytes(\n    sender_id: str,\n    recipient_id: str,\n    metadata: dict[str, Any],\n    timestamp: str,\n    contract_id: str,\n) -> bytes:\n    """\n    Produce a deterministic byte string over the packet fields that\n    participate in the HMAC computation.\n\n    The canonical form is a JSON object with keys in sorted order so that\n    the same logical packet always produces the same HMAC regardless of\n    the dict insertion order on either side of the wire.\n\n    Parameters\n    ----------\n    sender_id, recipient_id, metadata, timestamp, contract_id:\n        Fields from :class:`HandshakePacket`.\n\n    Returns\n    -------\n    bytes\n        UTF-8 encoded canonical JSON.\n    """\n    canonical = json.dumps(\n        {\n            "contract_id": contract_id,\n            "metadata":    metadata,\n            "recipient_id": recipient_id,\n            "sender_id":   sender_id,\n            "timestamp":   timestamp,\n        },\n        sort_keys=True,\n        ensure_ascii=False,\n    )\n    return canonical.encode("utf-8")\n\n\n# ---------------------------------------------------------------------------\n# HandshakePacket dataclass (Task 3.3.1)\n# ---------------------------------------------------------------------------\n\n@dataclass(slots=True)\nclass HandshakePacket:\n    """\n    Wire-format message exchanged between two Tala agents.\n\n    Attributes\n    ----------\n    sender_id:\n        Opaque identifier of the sending agent.\n    recipient_id:\n        Opaque identifier of the intended recipient.\n    metadata:\n        Sanitised payload dict.  MUST NOT contain filesystem paths\n        (enforced at creation time by :class:`HandshakeSender`).\n    signature:\n        HMAC-SHA256 hex digest computed over the canonical packet fields.\n    timestamp:\n        ISO-8601 UTC datetime string of packet preparation.\n    contract_id:\n        UUID string of the associated :class:`SmartContract`.\n    """\n\n    sender_id:    str\n    recipient_id: str\n    metadata:     dict[str, Any]\n    signature:    str\n    timestamp:    str\n    contract_id:  str\n\n\n# ---------------------------------------------------------------------------\n# HandshakeSender (Task 3.3.2)\n# ---------------------------------------------------------------------------\n\nclass HandshakeSender:\n    """\n    Prepares and signs outbound :class:`HandshakePacket` instances.\n\n    The sender enforces the path firewall and is responsible for computing\n    the HMAC-SHA256 signature before handing the packet to the transport\n    layer.\n\n    Example\n    -------\n    ::\n\n        sender = HandshakeSender()\n        packet = sender.prepare_packet(\n            sender_id="agent-alice",\n            recipient_id="agent-bob",\n            raw_data={"task": "summarise", "file": "/srv/docs/report.pdf"},\n            private_key_hex="deadbeef" * 8,\n        )\n        # packet.metadata will NOT contain the "file" key (path stripped).\n        # packet.signature is the HMAC-SHA256 of the canonical payload.\n    """\n\n    def prepare_packet(\n        self,\n        sender_id: str,\n        recipient_id: str,\n        raw_data: dict[str, Any],\n        private_key_hex: str,\n        contract_id: str | None = None,\n    ) -> HandshakePacket:\n        """\n        Build a signed, path-sanitised :class:`HandshakePacket`.\n\n        Steps\n        -----\n        1. Strip filesystem paths from *raw_data* (path firewall).\n        2. Record the current UTC timestamp.\n        3. Assign a contract reference UUID (or use the provided one).\n        4. Compute HMAC-SHA256 over the canonical payload.\n        5. Return the assembled packet.\n\n        Parameters\n        ----------\n        sender_id:\n            Identifier of the sending agent.\n        recipient_id:\n            Identifier of the intended recipient.\n        raw_data:\n            Arbitrary dict to include in the packet.  Path-like string\n            values will be silently stripped.\n        private_key_hex:\n            Hex-encoded HMAC secret (symmetric scheme).  Must be at least\n            32 hex characters (16 bytes / 128 bits).\n        contract_id:\n            Optional UUID string for an existing contract.  If omitted,\n            a fresh UUID v4 is generated.\n\n        Returns\n        -------\n        HandshakePacket\n            Signed, sanitised packet ready for transmission.\n\n        Raises\n        ------\n        ValueError\n            If *private_key_hex* is shorter than 32 hex characters.\n        """\n        if len(private_key_hex) < 32:\n            raise ValueError(\n                "private_key_hex must be at least 32 hex characters (16 bytes / 128-bit key). "\n                f"Got {len(private_key_hex)} characters."\n            )\n\n        # Step 1: strip paths\n        clean_metadata = _strip_paths(raw_data)\n\n        # Step 2: timestamp\n        timestamp = datetime.now(tz=timezone.utc).isoformat()\n\n        # Step 3: contract reference\n        cid = contract_id or str(uuid.uuid4())\n\n        # Step 4: HMAC-SHA256 signature\n        key_bytes = bytes.fromhex(private_key_hex)\n        message_bytes = _canonical_bytes(sender_id, recipient_id, clean_metadata, timestamp, cid)\n        signature = hmac.new(key_bytes, message_bytes, hashlib.sha256).hexdigest()\n\n        logger.debug(\n            "HandshakeSender: prepared packet  sender=%s  recipient=%s  contract=%s  sig=%s…",\n            sender_id,\n            recipient_id,\n            cid,\n            signature[:16],\n        )\n\n        return HandshakePacket(\n            sender_id=sender_id,\n            recipient_id=recipient_id,\n            metadata=clean_metadata,\n            signature=signature,\n            timestamp=timestamp,\n            contract_id=cid,\n        )\n\n\n# ---------------------------------------------------------------------------\n# HandshakeReceiver (Task 3.3.2)\n# ---------------------------------------------------------------------------\n\nclass HandshakeReceiver:\n    """\n    Verifies inbound :class:`HandshakePacket` instances.\n\n    The receiver looks up the sender's key in *known_public_keys*, re-\n    computes the expected HMAC, and performs a constant-time comparison to\n    prevent timing-oracle attacks.\n\n    Example\n    -------\n    ::\n\n        receiver = HandshakeReceiver()\n        is_valid = receiver.receive_packet(\n            packet=packet,\n            known_public_keys={"agent-alice": "deadbeef" * 8},\n        )\n    """\n\n    def receive_packet(\n        self,\n        packet: HandshakePacket,\n        known_public_keys: dict[str, str],\n    ) -> bool:\n        """\n        Verify *packet*'s HMAC signature using *known_public_keys*.\n\n        Steps\n        -----\n        1. Look up ``packet.sender_id`` in *known_public_keys*.\n        2. Re-compute the HMAC over the canonical payload.\n        3. Compare using :func:`hmac.compare_digest` (constant-time).\n        4. Return ``True`` on success.\n\n        Parameters\n        ----------\n        packet:\n            The inbound packet to verify.\n        known_public_keys:\n            Mapping of ``sender_id → hex_key`` for all trusted senders.\n            In this symmetric scheme the "public key" is the shared HMAC\n            secret.  Replace with asymmetric key lookup when upgrading\n            to real PKI.\n\n        Returns\n        -------\n        bool\n            ``True`` iff the signature is valid.\n\n        Raises\n        ------\n        HandshakeVerificationError\n            If the sender is unknown or the signature does not match.\n        """\n        hex_key = known_public_keys.get(packet.sender_id)\n        if hex_key is None:\n            raise HandshakeVerificationError(\n                f"HandshakeReceiver: unknown sender_id={packet.sender_id!r}.  "\n                f"The sender must be registered in known_public_keys."\n            )\n\n        try:\n            key_bytes = bytes.fromhex(hex_key)\n        except ValueError as exc:\n            raise HandshakeVerificationError(\n                f"HandshakeReceiver: bad hex key for sender {packet.sender_id!r}: {exc}"\n            ) from exc\n\n        message_bytes = _canonical_bytes(\n            packet.sender_id,\n            packet.recipient_id,\n            packet.metadata,\n            packet.timestamp,\n            packet.contract_id,\n        )\n        expected = hmac.new(key_bytes, message_bytes, hashlib.sha256).hexdigest()\n\n        # Constant-time comparison to prevent timing oracles.\n        if not hmac.compare_digest(expected, packet.signature):\n            raise HandshakeVerificationError(\n                f"HandshakeReceiver: signature mismatch for sender={packet.sender_id!r}  "\n                f"contract={packet.contract_id!r}.  "\n                f"The packet may have been tampered with in transit."\n            )\n\n        logger.info(\n            "HandshakeReceiver: packet VERIFIED  sender=%s  contract=%s",\n            packet.sender_id,\n            packet.contract_id,\n        )\n        return True\n\n\n# ---------------------------------------------------------------------------\n# AtomicSettlement (Task 3.3.3)\n# ---------------------------------------------------------------------------\n\nclass AtomicSettlement:\n    """\n    Atomically settles a smart contract after a successful handshake.\n\n    Settlement involves three logical steps that are executed in sequence:\n\n    1. **Payment release** — call :meth:`wallet.Wallet.execute_hold` to\n       transfer *payment_amount_crypto* from the buyer's held funds.\n    2. **Proof commitment** — record *proof_hash* against the contract in\n       PostgreSQL via :class:`ContractRouter`.\n    3. **State transition** — set the contract state to ``COMPLETED``.\n\n    If any step fails the error is propagated and the contract is\n    transitioned to ``FAILED`` (best-effort).\n\n    Example\n    -------\n    ::\n\n        settlement = AtomicSettlement()\n        await settlement.settle(\n            contract_id=str(contract.contract_id),\n            router=router,\n            payment_amount_crypto=0.001,\n            proof_hash="abc123…",\n        )\n    """\n\n    async def settle(\n        self,\n        contract_id: str,\n        router: ContractRouter,\n... (file truncated, showing first 500 lines) ...
```
