"""
skill_signing.py — a marketplace item is signed, and the signature is checked.

``marketplace.download`` logged
``"Verifying marketplace payload against public key %s..."`` and then verified
nothing at all: the whole check was that ``public_key`` was a non-empty string,
after which the payload was written into ``skills/`` regardless (UI-042). The
key it checked was ``"local-node-pubkey-0xTALA"`` — a hardcoded placeholder the
serving side put there.

That matters because a downloaded skill is not data. It is a YAML file of
``steps`` with ``action`` and ``args``, which the graph engine executes. So the
path took executable capability from another machine, wrote a log line shaped
like a security control, and ran it.

There is real signing infrastructure now — the chain's Ed25519 identity, its
fingerprints, and a list of signers the owner has admitted — so this uses it
rather than inventing a parallel one. A skill is signed by the node that
publishes it and verified against the same permitted-signer list that governs
which keys may write to the shared chain.

**Domain separation**, for the same reason as everywhere else in this codebase:
a block signature is made over a bare 64-character hex hash, so a signing
routine over unprefixed input hands out valid block signatures to anyone who
submits a block hash. Skill signatures are made over ``tala-skill:v1:`` plus the
payload digest, which can never be one.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Must not collide with the block, proof-of-possession, or revocation domains.
SKILL_PREFIX = "tala-skill:v1:"


def payload_digest(payload: Any) -> str:
    """
    A stable digest of what is being signed.

    Keys are sorted, so a payload that survives a round trip through YAML or
    JSON in a different order still verifies. Without that, a signature would
    break for reasons that look like tampering and are not — and anything that
    cries tamper too often stops being read.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _keys() -> dict[str, str]:
    """The node's own chain keypair."""
    import os

    configured = os.environ.get("TALA_CHAIN_DATA_DIR")
    base = (Path(configured) if configured
            else Path(__file__).resolve().parents[2] / "blockchain" / "data")
    return json.loads((base / "poa_keys.json").read_text(encoding="utf-8"))


def sign(payload: Any) -> dict[str, str]:
    """
    Sign a skill payload with this node's chain identity.

    Reusing the chain key rather than minting a marketplace-specific one is
    deliberate: it means a publisher a user has already admitted as a chain
    signer is the same publisher they trust for skills, with one list to
    manage and one fingerprint to compare out of band.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    from python.harness.chain_verify import fingerprint

    keys = _keys()
    digest = payload_digest(payload)
    private = load_pem_private_key(keys["privateKey"].encode("utf-8"), password=None)
    return {
        "algorithm": "ed25519",
        "digest": digest,
        "public_key": keys["publicKey"],
        "fingerprint": fingerprint(keys["publicKey"]),
        "signature": private.sign(f"{SKILL_PREFIX}{digest}".encode("utf-8")).hex(),
    }


def verify(payload: Any, item: dict[str, Any]) -> tuple[bool, str]:
    """
    Whether this payload really was signed by the key the item names.

    Returns ``(ok, reason)``. The reason is filled in on failure, so a refused
    download can say which check failed rather than merely being refused.

    This establishes that the payload has not changed since whoever holds that
    key signed it. It says nothing about whether that key should be trusted —
    :func:`is_trusted_signer` is a separate question with a separate answer,
    because conflating them is how "verified" came to mean nothing here.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from python.harness.chain_verify import fingerprint

    public_key = str(item.get("public_key") or "").strip()
    signature = str(item.get("signature") or "")
    if not public_key:
        return False, "the item carries no public key"
    if not signature:
        return False, "the item carries no signature"
    if "BEGIN PUBLIC KEY" not in public_key:
        # What the serving side used to send: a hardcoded placeholder string.
        return False, ("the item's public key is not a key — it looks like a "
                       "placeholder, which is what this check exists to catch")

    digest = payload_digest(payload)
    claimed = str(item.get("digest") or "")
    if claimed and claimed != digest:
        return False, "the payload does not match the digest that was signed"

    try:
        load_pem_public_key(public_key.encode("utf-8")).verify(
            bytes.fromhex(signature),
            f"{SKILL_PREFIX}{digest}".encode("utf-8"),
        )
    except Exception as exc:
        return False, f"the signature does not verify ({type(exc).__name__})"

    return True, f"signed by {fingerprint(public_key)}"


async def is_trusted_signer(public_key: str) -> tuple[bool, str]:
    """
    Whether the owner has admitted this signer.

    A valid signature proves who wrote the skill, not that you want to run it.
    The permitted-signer list is the same one that governs the shared chain, so
    admitting a publisher is one decision rather than two.

    With no signers configured this refuses. That is the same rule reconcile
    uses: importing from unknown signers because nobody answered the question
    is precisely what the check exists to prevent.

    A signer who has been withdrawn, or who has retired their own key, is not
    active and cannot publish — even though the blocks they already signed on
    the chain remain valid.
    """
    from python.harness.chain_verify import fingerprint
    from python.harness.chains import active_authorities

    # active_authorities, not authorities. The latter includes signers that
    # have been withdrawn or have retired themselves — deliberately, because
    # their chain history stays valid and striking them out would refuse every
    # block they ever wrote. Publishing is a *future* act, so a withdrawn
    # signer must not be able to install anything new.
    print_ = fingerprint(public_key)
    permitted = await active_authorities()
    if not permitted:
        return False, ("no permitted signers are configured, so there is "
                       f"nothing to check {print_} against")
    if print_ not in permitted:
        return False, f"{print_} is not a permitted signer"
    return True, print_
