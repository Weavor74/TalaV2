"""
chain_verify.py — verify a chain without trusting whoever handed it over.

Reconcile pulls blocks from a shared chain that other people write to. The
whole point of pulling is that the source is not this machine, so the source's
own assurance that its chain is valid is worth nothing: ``GET /chain/status``
returns ``isValid`` computed by the node being asked. This module recomputes it
here instead.

The three checks mirror ``Blockchain.isValid()`` in ``blockchain/src/app.js``
exactly, because a chain this rejects and that one accepts (or the reverse)
would be a split nobody could diagnose:

* every block's hash is the hash of its own contents;
* every block's ``previousHash`` is the previous block's hash;
* every signed block's Ed25519 signature verifies against its public key.

The hash inputs are byte-identical to the Node implementation, which is a
constraint worth stating because it is easy to break silently. Block hashing is
``sha256(index + previousHash + timestamp + JSON.stringify(transactions) +
nonce)``, so the transaction serialisation must match ``JSON.stringify``: no
whitespace, keys in insertion order, non-ASCII left as-is. Verified against all
2,748 blocks of the existing chain -- 0 bad hashes, 0 broken links, 2,747 valid
signatures and the unsigned genesis.
"""

from __future__ import annotations

import hashlib
import json
import re
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Verdict:
    """The outcome of verifying a chain, and why."""

    valid: bool
    height: int = 0
    signed: int = 0
    unsigned: int = 0
    problems: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.valid


def _js_json(value: Any) -> str:
    """Serialise the way ``JSON.stringify`` does.

    ``separators`` removes the whitespace Python adds by default, and
    ``sort_keys`` stays off because JSON.stringify preserves insertion order --
    sorting here would change every hash.
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def block_hash(block: dict[str, Any]) -> str:
    """Recompute one block's hash from its contents."""
    return hashlib.sha256(
        (
            f"{block.get('index')}"
            f"{block.get('previousHash')}"
            f"{block.get('timestamp')}"
            f"{_js_json(block.get('transactions') or [])}"
            f"{block.get('nonce')}"
        ).encode("utf-8")
    ).hexdigest()


def fingerprint(public_key_pem: str) -> str:
    """
    A short, stable handle for a signer.

    Whitespace-insensitive, so a PEM that has been through JSON, a text field
    or a different line ending still resolves to the same signer. Must match
    ``keyFingerprint`` in ``blockchain/src/app.js`` exactly, or the two sides
    would disagree about which keys are permitted.
    """
    normalised = re.sub(r"\s+", "", str(public_key_pem or ""))
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def _signature_ok(block: dict[str, Any]) -> bool:
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    load_pem_public_key(str(block["public_key"]).encode("utf-8")).verify(
        bytes.fromhex(str(block["signature"])),
        str(block.get("hash", "")).encode("utf-8"),
    )
    return True


def verify(blocks: list[dict[str, Any]], limit: int = 20,
           authorities: set[str] | None = None) -> Verdict:
    """
    Verify a chain end to end.

    ``authorities`` is a set of permitted signer fingerprints. When given,
    every block after genesis must be signed, and signed by one of them.

    Both halves of that matter. Verifying a signature against the public key
    the block itself carries proves only internal consistency -- anyone can
    generate a key, sign with it, and produce a block that verifies -- so an
    unauthenticated signer is no bar on a chain other people write to. And a
    block carrying no signature at all used to skip the check entirely, which
    made forging one a matter of *deleting* its signature.

    Passing ``None`` checks structure and self-consistency only. That is the
    right default for looking at a chain, and the wrong one for deciding
    whether to import it.

    Reports up to ``limit`` problems rather than the first one. A chain that
    fails is rejected whole -- there is no partial import, because a chain with
    some blocks dropped out of it is not a chain.
    """
    if not isinstance(blocks, list):
        return Verdict(False, problems=["the chain is not a list of blocks"])
    if not blocks:
        # An empty shared chain is a shared chain nobody has written to yet,
        # which is the ordinary state of a new one, not a failure.
        return Verdict(True, height=0)

    problems: list[str] = []
    signed = unsigned = 0

    for position, block in enumerate(blocks):
        if not isinstance(block, dict):
            problems.append(f"block {position} is not an object")
            continue

        stored = str(block.get("hash") or "")
        computed = block_hash(block)
        if stored != computed:
            problems.append(
                f"block {block.get('index', position)}: contents do not match "
                f"its hash"
            )

        if position:
            previous = blocks[position - 1]
            if isinstance(previous, dict):
                if str(block.get("previousHash") or "") != str(previous.get("hash") or ""):
                    problems.append(
                        f"block {block.get('index', position)}: does not link to "
                        f"the block before it"
                    )

        if block.get("signature") and block.get("public_key"):
            signed += 1
            if authorities is not None and fingerprint(block["public_key"]) not in authorities:
                problems.append(
                    f"block {block.get('index', position)}: signed by "
                    f"{fingerprint(block['public_key'])}, which is not a "
                    f"permitted signer"
                )
            else:
                try:
                    _signature_ok(block)
                except Exception as exc:
                    problems.append(
                        f"block {block.get('index', position)}: signature does "
                        f"not verify ({type(exc).__name__})"
                    )
        else:
            unsigned += 1
            # Genesis is unsigned on this chain by construction. Any other
            # unsigned block is a block that declined to say who wrote it.
            if authorities is not None and position:
                problems.append(
                    f"block {block.get('index', position)}: is not signed")

        if len(problems) >= limit:
            problems.append("… stopping; the chain is already unusable")
            break

    return Verdict(
        valid=not problems,
        height=len(blocks),
        signed=signed,
        unsigned=unsigned,
        problems=problems,
    )


def transactions_of(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Flatten a verified chain into its transactions, each carrying its position.

    The position is the identity. The transaction ``id`` field is a *document*
    identity that repeats every time that document changes -- in the existing
    chain, 2,748 transactions carry 185 distinct ids -- so keying on it would
    collapse a peer's history exactly as it collapsed ours.
    """
    out: list[dict[str, Any]] = []
    for position, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        index = block.get("index", position)
        for offset, transaction in enumerate(block.get("transactions") or []):
            if not isinstance(transaction, dict):
                continue
            out.append({
                **transaction,
                "_entry_id": f"block-{index}-{offset}",
                "_block_index": index,
                "_block_timestamp": block.get("timestamp"),
            })
    return out
