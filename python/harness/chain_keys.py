"""
chain_keys.py — obtaining a peer's signing key, and deciding whether to trust it.

Admitting a signer needs their public key, and the hard part is not fetching it.
It is knowing the key is theirs. There is no certificate authority here, no web
of trust, and no channel this system controls that an attacker in the middle
would not also control. Any mechanism that fetches a key over the network and
admits it is not key distribution; it is the same defect as trusting whatever
signature a block carries, moved one layer out.

So fetching and trusting are separate acts, and this module never performs the
second one:

* **Fetch** pulls a key from a peer's node and records it as *offered*.
* **Proof of possession** asks that node to sign a nonce this side chose. It
  proves whoever answers at that address holds the private key, which stops a
  directory advertising somebody else's key and having it admitted on their
  behalf. It says nothing about who they are.
* **Identity** is established by a human comparing the fingerprint over a
  channel neither node controls — a phone call, a message on another service,
  a piece of paper. That step cannot be automated away, and pretending
  otherwise is the failure this design exists to avoid.
* **Trusting** is a deliberate act by the owner, and only then does the key
  become an authority.

The fingerprint is what makes the out-of-band step usable: sixteen hex
characters is short enough to read aloud and long enough that finding a second
key with the same one is not a thing anybody can do.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

log = logging.getLogger(__name__)

#: Must match ``POP_PREFIX`` in ``blockchain/src/app.js``.
#:
#: Domain separation, and the reason the proof endpoint is safe to expose.
#: Block signatures are made over a bare 64-character hex hash, so a signing
#: oracle over unprefixed input would let anyone obtain a valid block signature
#: by submitting a block hash as their nonce. Prefixed messages cannot collide
#: with block hashes.
POP_PREFIX = "tala-proof-of-possession:v1:"

OFFERED, TRUSTED, REJECTED = "offered", "trusted", "rejected"


async def identity() -> dict[str, Any]:
    """This node's own signing key, for handing to a peer."""
    import httpx

    from python.harness import chain_verify, chains

    endpoint = await chains.ghost_endpoint()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{endpoint.rstrip('/')}/chain/identity")
        if response.status_code >= 400:
            return {"available": False, "reason": f"HTTP {response.status_code}"}
        body = response.json()
        return {
            "available": True,
            "public_key": body.get("public_key", ""),
            "fingerprint": body.get("fingerprint")
            or chain_verify.fingerprint(body.get("public_key", "")),
        }
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


async def fetch(endpoint: str, label: str = "") -> dict[str, Any]:
    """
    Obtain a peer's signing key and prove they hold it.

    Records the result as an offer. Never trusts it: that is the owner's call,
    and it should be made after comparing the fingerprint with the peer over a
    channel neither node controls.
    """
    import httpx

    from python.harness import chain_verify

    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return {"fetched": False, "reason": "an endpoint is required"}
    if not endpoint.startswith(("http://", "https://")):
        return {"fetched": False,
                "reason": "the endpoint must be an http:// or https:// URL"}

    try:
        from python.harness.access import AGENT, Principal
        from python.harness.network_guard import check as net_check

        permitted = await net_check(endpoint, Principal(AGENT, "default"),
                                    purpose="chain-key-exchange")
        if not permitted.allowed:
            return {"fetched": False, "reason": permitted.reason}
    except Exception as exc:
        return {"fetched": False, "reason": f"guardrails unavailable: {exc}"}

    base = endpoint.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{base}/chain/identity")
            if response.status_code >= 400:
                return {"fetched": False,
                        "reason": f"HTTP {response.status_code} asking for its key"}
            offered = response.json()

            public_key = str(offered.get("public_key") or "").strip()
            if not public_key:
                return {"fetched": False, "reason": "it advertised no signing key"}

            # Their claimed fingerprint is not taken at face value: it is
            # recomputed here, since a node that misreports its own fingerprint
            # would otherwise get an out-of-band comparison to succeed against
            # a key that is not the one it will sign with.
            fingerprint = chain_verify.fingerprint(public_key)

            nonce = secrets.token_hex(32)
            proof = await client.post(f"{base}/chain/prove", json={"nonce": nonce})
    except Exception as exc:
        return {"fetched": False, "reason": f"{type(exc).__name__}: {exc}"}

    proven, note = _check_proof(proof, nonce, public_key)

    await _record(fingerprint, public_key, label=label, source=endpoint,
                  proven=proven, note=note)

    return {
        "fetched": True,
        "fingerprint": fingerprint,
        "proven": proven,
        "note": note,
        "status": OFFERED,
        "next": (
            "Compare this fingerprint with the peer over a channel neither of "
            "you controls, then trust it. Nothing on the network can do that "
            "step for you."
        ),
    }


def _check_proof(response: Any, nonce: str, public_key: str) -> tuple[bool, str]:
    """Whether the holder demonstrated possession of the private key."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    if response.status_code >= 400:
        return False, f"it would not prove possession (HTTP {response.status_code})"
    try:
        body = response.json()
    except Exception:
        return False, "its proof was not readable"

    if str(body.get("nonce") or "") != nonce:
        # A reused or substituted nonce is a replayed proof, which proves
        # nothing about who is answering now.
        return False, "its proof answered a different challenge"

    if str(body.get("public_key") or "").strip() != public_key.strip():
        return False, "it signed with a different key than it advertised"

    try:
        load_pem_public_key(public_key.encode("utf-8")).verify(
            bytes.fromhex(str(body.get("signature") or "")),
            f"{POP_PREFIX}{nonce}".encode("utf-8"),
        )
    except Exception as exc:
        return False, f"its proof did not verify ({type(exc).__name__})"

    return True, "it proved it holds the private key"


async def offer(public_key: str, label: str = "",
                source: str = "pasted") -> dict[str, Any]:
    """
    Record a key handed over directly rather than fetched.

    A key read off a screen or sent through another channel has no proof of
    possession attached, and is recorded as such rather than being quietly
    treated as though it had one.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from python.harness import chain_verify

    public_key = str(public_key or "").strip()
    try:
        load_pem_public_key(public_key.encode("utf-8"))
    except Exception as exc:
        return {"offered": False, "reason": f"not a usable public key: {exc}"}

    fingerprint = chain_verify.fingerprint(public_key)
    await _record(fingerprint, public_key, label=label, source=source,
                  proven=False, note="supplied directly; possession not tested")
    return {"offered": True, "fingerprint": fingerprint, "proven": False}


async def _record(fingerprint: str, public_key: str, label: str, source: str,
                  proven: bool, note: str) -> None:
    from python.harness import db

    await db.execute(
        """
        INSERT INTO chain_key_offers (fingerprint, public_key, label, source,
                                      proven, status, note)
        VALUES ($1, $2, $3, $4, $5, 'offered', $6)
        ON CONFLICT (fingerprint) DO UPDATE
            SET public_key = EXCLUDED.public_key,
                label  = CASE WHEN EXCLUDED.label <> '' THEN EXCLUDED.label
                              ELSE chain_key_offers.label END,
                source = EXCLUDED.source,
                proven = EXCLUDED.proven,
                note   = EXCLUDED.note
        """,
        fingerprint, public_key, label, source, proven, note,
        caller="chain_keys.record",
    )


async def offers(status: str = "") -> list[dict[str, Any]]:
    """Keys that have been obtained, and what was decided about each."""
    from python.harness import db

    query = ("SELECT fingerprint, label, source, proven, status, note, "
             "offered_at, decided_at FROM chain_key_offers")
    args: list[Any] = []
    if status:
        query += " WHERE status = $1"
        args.append(status)
    query += " ORDER BY offered_at DESC"

    try:
        rows = await db.fetch(query, *args)
    except Exception as exc:
        log.error("chain: could not read the key offers: %s", exc)
        return []

    return [{
        "fingerprint": row["fingerprint"], "label": row["label"],
        "source": row["source"], "proven": row["proven"],
        "status": row["status"], "note": row["note"],
        "offered_at": row["offered_at"].isoformat() if row["offered_at"] else "",
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else "",
    } for row in rows]


async def trust(fingerprint: str) -> dict[str, Any]:
    """
    Admit an offered key as a permitted signer.

    The deliberate act. It is separate from fetching precisely so that it can
    be, and it refuses a fingerprint that was never offered — trusting a key
    nobody has seen is not a thing to make convenient.
    """
    from python.harness import chains, db

    fingerprint = str(fingerprint or "").strip()
    row = await db.fetchrow(
        "SELECT fingerprint, proven, label FROM chain_key_offers "
        "WHERE fingerprint = $1", fingerprint)
    if not row:
        return {"trusted": False,
                "reason": "that key has not been offered, so there is nothing "
                          "to compare it against"}

    permitted = await chains.authorities()
    permitted.add(fingerprint)
    await chains.set_authorities(sorted(permitted))

    await db.execute(
        "UPDATE chain_key_offers SET status = 'trusted', decided_at = NOW() "
        "WHERE fingerprint = $1", fingerprint, caller="chain_keys.trust")
    await db.log_to_db(
        "INFO",
        f"Admitted {fingerprint} as a permitted signer on the shared chain"
        + (f" ({row['label']})" if row["label"] else ""),
        {"fingerprint": fingerprint, "possession_proven": row["proven"]},
    )
    log.info("chain: %s may now sign blocks on the shared chain", fingerprint)
    return {"trusted": True, "fingerprint": fingerprint,
            "authorities": sorted(permitted)}


async def reject(fingerprint: str) -> dict[str, Any]:
    """
    Refuse an offered key, and withdraw it if it was previously trusted.

    The record is kept rather than deleted: a key that was offered and refused
    is worth still knowing about when it turns up again.
    """
    from python.harness import chains, db

    fingerprint = str(fingerprint or "").strip()
    permitted = await chains.authorities()
    was_trusted = fingerprint in permitted
    if was_trusted:
        permitted.discard(fingerprint)
        await chains.set_authorities(sorted(permitted))

    await db.execute(
        "UPDATE chain_key_offers SET status = 'rejected', decided_at = NOW() "
        "WHERE fingerprint = $1", fingerprint, caller="chain_keys.reject")

    if was_trusted:
        await db.log_to_db(
            "WARNING",
            f"Withdrew {fingerprint} as a permitted signer. Blocks it already "
            f"signed remain on the chain.",
            {"fingerprint": fingerprint},
        )
    return {"rejected": True, "fingerprint": fingerprint,
            "was_trusted": was_trusted,
            "note": ("Blocks it already signed stay on the chain; removing "
                     "them would be a whole-chain rewrite."
                     if was_trusted else "")}
