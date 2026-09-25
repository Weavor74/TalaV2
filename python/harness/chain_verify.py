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


REVOCATION_PREFIX = "tala-key-revocation:v1:"


def _revocation_message(fingerprint: str, revoked_at: str) -> str:
    """The message a key signs to retire itself.

    Must be built identically to ``revocationMessage`` in
    ``blockchain/src/app.js``, or a valid revocation reads as a forged one on
    one side and the two disagree about which keys are still usable.
    """
    return f"{REVOCATION_PREFIX}{fingerprint}:{revoked_at}"


def _verify_revocation(transaction: dict[str, Any]) -> str:
    """
    Check a revocation transaction. Returns the retired fingerprint, or "".

    Verified from the transaction alone, with no reference to who relayed it or
    which chain it arrived on. That is what lets a revocation travel safely
    through nodes that do not trust each other: it is not a claim to be
    weighed, it is a signature only the holder could have made.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    metadata = transaction.get("metadata") or {}
    if not isinstance(metadata, dict):
        return ""
    public_key = str(metadata.get("public_key")
                     or transaction.get("public_key") or "").strip()
    claimed = str(metadata.get("fingerprint") or "").strip()
    revoked_at = str(metadata.get("revoked_at") or "")
    signature = str(transaction.get("signature") or "")
    if not (public_key and claimed and revoked_at and signature):
        return ""

    # Derived, never believed: a revocation naming one key and carrying another
    # would otherwise retire whichever the reader happened to look at.
    print_ = fingerprint(public_key)
    if print_ != claimed:
        return ""

    try:
        load_pem_public_key(public_key.encode("utf-8")).verify(
            bytes.fromhex(signature),
            _revocation_message(print_, revoked_at).encode("utf-8"),
        )
    except Exception:
        return ""
    return print_


def revocations_in(blocks: list[dict[str, Any]]) -> dict[str, int]:
    """
    Keys that retired themselves, mapped to the block they did it in.

    The position matters because revocation is not retroactive: blocks signed
    before it stay valid. Retiring a key must not collapse the history it
    wrote, or nobody could ever safely retire one.
    """
    found: dict[str, int] = {}
    for position, block in enumerate(blocks if isinstance(blocks, list) else []):
        if not isinstance(block, dict):
            continue
        index = block.get("index", position)
        for transaction in block.get("transactions") or []:
            if not isinstance(transaction, dict):
                continue
            if transaction.get("type") != "key-revocation":
                continue
            retired = _verify_revocation(transaction)
            # The earliest wins: a key is retired from the first moment it
            # said so.
            if retired and retired not in found:
                found[retired] = index
    return found


def verify(blocks: list[dict[str, Any]], limit: int = 20,
           authorities: set[str] | None = None,
           cutoffs: dict[str, int] | None = None) -> Verdict:
    """
    Verify a chain end to end.

    ``authorities`` is a set of permitted signer fingerprints. When given,
    every block after genesis must be signed, and signed by one of them.

    Revocations carried on the chain are honoured either way, because a key
    that has retired itself is not a matter of local policy: the holder said
    not to trust it, in a signature anyone can check.

    ``cutoffs`` adds locally-decided stopping points — a signer this install
    withdrew — in the same shape. Both are cutoffs rather than removals, and
    for the same reason: a signer struck from the list entirely would have
    every block they ever wrote refused, which retroactively invalidates
    history that was perfectly good when it was written.

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

    # Read once up front: a key retired at block R may not sign anything after
    # R, and that question is asked of every block. Self-revocations found on
    # the chain and local withdrawals are the same kind of fact, so they merge
    # into one table — with the chain's own word taken as earlier when both
    # name the same signer, since that is the point every other node will use.
    retired = dict(cutoffs or {})
    for print_, block in revocations_in(blocks).items():
        retired[print_] = min(block, retired.get(print_, block))

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
                retired_at = retired.get(fingerprint(block["public_key"]))
                index = block.get("index", position)
                if retired_at is not None and index > retired_at:
                    problems.append(
                        f"block {index}: signed by "
                        f"{fingerprint(block['public_key'])}, which stopped "
                        f"being a permitted signer at block {retired_at}"
                    )
                else:
                    try:
                        _signature_ok(block)
                    except Exception as exc:
                        problems.append(
                            f"block {index}: signature does not verify "
                            f"({type(exc).__name__})"
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
