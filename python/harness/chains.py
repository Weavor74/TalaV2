"""
chains.py — Ghost and Real are two chains, split by audience.

Steven's design, in his words:

    "The Ghost chain is only viewable locally. The Real chain is viewable by
    all on the chain. They are meant to be separate." (Q-11)

    "If the TALA harness never goes online... the Real chain never gets used.
    If you connect with other users, use other skills, transact with AUTH or
    online storage, it gets recorded by the Real chain." (Q-12)

and, settling the four questions that were left open (2026-08-28):

* **An external interaction while offline is queued and written on reconnect.**
  Nothing is lost, so the Real chain stays a complete record of external
  activity. The consequence is accepted deliberately: a queued transaction's
  block position will not match the moment it happened, and its recorded
  timestamp is the moment of the interaction rather than the moment of the
  write.
* **Ghost records everything.** It is the complete local audit trail -- local
  activity plus a local copy of anything that also went to Real -- so "what did
  this machine do" is answerable from one place, offline. Real is a strict
  subset, not a sibling.
* **The Real chain is one shared chain**, which is what "viewable by all on the
  chain" means and what ``/chain/peers/register`` and ``/chain/reconcile``
  already point at.
* **The existing 2,748-block chain is the Ghost chain**, kept as it is. Its
  2,747 ``node-create`` transactions are local document activity with no
  external party -- Ghost by definition -- and the Real chain starts empty.

The previous implementation had the distinction as a *connection mode*
(UI-023): ``token_counter`` computed ``use_ghost_tokens`` and the only place
the answer appeared was a ``logger.debug`` line. Nothing persisted it, so no
transaction ever recorded which chain it belonged to.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

GHOST = "ghost"
REAL = "real"

#: What makes a transaction external, and therefore Real as well as Ghost.
#: Steven's list: "connect with other users, use other skills, transact with
#: AUTH or online storage".
EXTERNAL_KINDS = frozenset({
    "peer-contact",       # talking to another install
    "peer-skill",         # using someone else's skill
    "skill-shared",       # someone using yours
    "auth-transaction",   # an AUTH-provider transaction
    "remote-storage",     # online storage
    "payment",            # value moving
    "marketplace",        # buying or selling
})

#: Local-only activity. Recorded on Ghost and never sent anywhere. Checked
#: before anything else, so a local transaction cannot be dragged onto the
#: shared chain by a stray counterparty field.
LOCAL_KINDS = frozenset({
    "node-create", "node-update", "node-delete", "thought-state",
    "behavioral-learning", "non-critical-event", "mint",
})

#: Counterparty values that name no actual second party.
#:
#: Every one of the 2,748 existing transactions carries one of these -- 2,747
#: ``node-create`` rows against ``blockchain-ledger`` and the single ``mint``
#: against ``tala-network``. They read like counterparties and are not: they
#: are placeholders for "the chain itself", written by local document activity
#: with nobody on the other end. Treating them as real would have published
#: the entire local history to a chain everybody can read.
PLACEHOLDER_COUNTERPARTIES = frozenset({
    "blockchain-ledger", "tala-network", "tala-system",
    "local-client", "@local-tala", "self",
})


def is_external(kind: str, counterparty: str | None = None) -> bool:
    """
    Whether this belongs on the Real chain as well as the Ghost chain.

    Three passes, in order. A declared local kind is local whatever else it
    carries. A declared external kind is external. Otherwise a real
    counterparty -- one naming somebody, not the chain itself -- makes it
    external, because a transaction with a second party in it is an external
    interaction whatever it calls itself.

    An unrecognised kind with no counterparty is local. The Real chain is for
    interactions that demonstrably involved someone else, and guessing the
    other way publishes private activity to a shared chain: the failure modes
    are not symmetric.
    """
    kind = str(kind or "").strip().lower()
    if kind in LOCAL_KINDS:
        return False
    if kind in EXTERNAL_KINDS:
        return True

    other = str(counterparty or "").strip()
    return bool(other) and other.lower() not in PLACEHOLDER_COUNTERPARTIES


def classify(kind: str, counterparty: str | None = None) -> list[str]:
    """Which chains this transaction is written to. Ghost always."""
    return [GHOST, REAL] if is_external(kind, counterparty) else [GHOST]


async def record(
    kind: str,
    payload: dict[str, Any] | None = None,
    counterparty: str = "",
    document_ref: str = "",
    external: bool | None = None,
) -> dict[str, Any]:
    """
    Record one transaction on the chains it belongs to.

    Always writes Ghost. If the transaction is external it is additionally
    queued for Real, and flushed immediately when a connection is available.

    ``external`` overrides the classification for callers that already know
    the answer better than the kind does. ``token_counter`` is the case: a
    payment is normally external, but paying yourself for your own resources
    on your own machine is not, and only the caller knows which it was.
    """
    kind = str(kind or "").strip().lower()
    if not kind:
        raise ValueError("a transaction needs a kind")

    if external is None:
        external = is_external(kind, counterparty)
    chains = [GHOST, REAL] if external else [GHOST]
    tx_id = f"tx-{uuid.uuid4().hex[:16]}"
    occurred = datetime.now(timezone.utc)

    transaction = {
        "id": tx_id,
        "type": kind,
        "counterparty": str(counterparty or ""),
        # A reference, never content. A document body written to a chain
        # everybody on it can read is a document published, whatever the
        # intent was.
        "document_ref": str(document_ref or ""),
        "metadata": payload or {},
        # The moment the interaction happened, not the moment it was written.
        # A queued transaction is written later and this is the only record of
        # when it actually occurred.
        "timestamp": occurred.isoformat(),
    }

    ghost_result = await _write_ghost(transaction)

    real_result: dict[str, Any] = {"written": False, "queued": False}
    if REAL in chains:
        real_result = await _write_or_queue_real(transaction)

    log.info("chain: recorded %s on %s%s", kind, "+".join(chains),
             " (queued for Real)" if real_result.get("queued") else "")

    return {
        "id": tx_id,
        "chains": chains,
        "ghost": ghost_result,
        "real": real_result,
        "external": REAL in chains,
    }


async def _write_ghost(transaction: dict[str, Any]) -> dict[str, Any]:
    """
    Append to the local chain, then index it.

    The Ghost chain is not a new store: it is the chain that already exists at
    ``blockchain/data/chain.json``, served by the local node, holding all 2,748
    existing blocks. This posts to it exactly as ``anchor.py`` always has. The
    ledger row is an *index* over it -- id, kind, chain, and whether Real has
    accepted it -- and never a second copy of the document.

    Never raises. Ghost is the local audit trail and a failure to reach the
    local node must not stop the thing being recorded from having happened;
    the index row is still written, so the event is not lost even when the
    node is down.
    """
    posted = await _post(await ghost_endpoint(), transaction, guard=False)
    indexed = await index(GHOST, transaction,
                          entry_id=f"ghost-{transaction['id']}")
    return {
        "written": indexed.get("written", False),
        "anchored": posted.get("written", False),
        **({"error": posted["error"]} if posted.get("error") else {}),
    }


async def _write_or_queue_real(transaction: dict[str, Any]) -> dict[str, Any]:
    """Write to the shared Real chain, or queue it for the next connection."""
    entry_id = f"real-{transaction['id']}"
    indexed = await index(REAL, transaction, pending=True, entry_id=entry_id)
    if not indexed.get("written"):
        return {"written": False, "queued": False, "error": indexed.get("error", "")}

    sent = await _submit(transaction)
    if sent.get("written"):
        await _mark_written(entry_id, sent.get("remote_id", ""))
        return {"written": True, "queued": False}
    return {"written": False, "queued": True, "reason": sent.get("error", "")}


async def index(
    chain: str,
    transaction: dict[str, Any],
    pending: bool = False,
    entry_id: str = "",
    origin: str = "local",
) -> dict[str, Any]:
    """
    Record which chain a transaction belongs to.

    Callers that anchor to the local node themselves -- ``anchor.py``,
    ``settings_nn.py`` -- call this after their own POST rather than routing
    through :func:`record`, so the index is complete without their
    transactions being posted twice.

    ``document`` is stored as a reference, never as content: a document body
    on the Real chain would be readable by everyone on it.

    ``origin`` separates what this machine did from what reconcile pulled back
    off the shared chain. Without it "everything on the Real chain" and
    "everything I put on the Real chain" are the same query, and they are not
    the same question.

    ``entry_id`` makes the write idempotent for callers that can name the same
    event twice -- the backfill derives it from the block position. Left empty
    it is generated, because the transaction id cannot serve: it identifies the
    *document*, and re-anchoring a node reuses it.
    """
    from python.harness import db

    try:
        status = await db.execute(
            """
            INSERT INTO chain_ledger (entry_id, tx_id, chain, type,
                                      counterparty, document, metadata,
                                      occurred_at, pending, origin)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
            ON CONFLICT (chain, entry_id) DO NOTHING
            """,
            entry_id or f"e-{uuid.uuid4().hex}",
            transaction["id"], chain, transaction["type"],
            transaction.get("counterparty", ""),
            str(transaction.get("document_ref") or "")[:512],
            json.dumps(transaction.get("metadata") or {}),
            _as_datetime(transaction.get("timestamp")),
            pending,
            "peer" if origin == "peer" else "local",
            caller="chains.index",
        )
        # DO NOTHING is a successful statement that wrote nothing, and
        # reporting it as written is how reconcile came to say it had learned
        # four transactions while writing none of them.
        wrote = not str(status).endswith(" 0")
        return {"written": wrote, "duplicate": not wrote}
    except Exception as exc:
        log.error("chain: could not index %s on the %s chain: %s",
                  transaction.get("id"), chain, exc)
        return {"written": False, "error": str(exc)}


def _unwrap(value: Any) -> dict[str, Any]:
    """
    Decode a JSONB column that may be encoded more than once.

    Every JSONB column in this database holds a JSON *string* -- a jsonb codec
    and ``json.dumps`` at the call sites each encode it once. A single
    ``json.loads`` therefore returns a string rather than a dict, and the
    caller reads an empty result from a row that was never empty.
    """
    for _ in range(3):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return {}
        else:
            break
    return value if isinstance(value, dict) else {}


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def index_sync(chain: str, tx_id: str, kind: str, counterparty: str = "",
               document_ref: str = "", metadata: dict[str, Any] | None = None,
               pending: bool = False, entry_id: str = "") -> bool:
    """
    Index a transaction from synchronous code.

    ``anchor.py`` and ``settings_nn.py`` anchor to the local node themselves,
    from synchronous functions, and have done since long before this module
    existed. Rewriting them to be async to reach :func:`record` would be a
    large change to the busiest write path in the system for no behavioural
    gain, so they keep their POST and call this afterwards.

    Returns whether the row was written. Never raises: an unindexed anchor is
    a gap in the record, but a failed index must not fail the anchor.
    """
    import asyncio

    transaction = {
        "id": tx_id, "type": kind, "counterparty": counterparty,
        "document_ref": document_ref, "metadata": metadata or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return bool(asyncio.run(index(chain, transaction, pending,
                                          entry_id)).get("written"))
        except Exception as exc:
            log.debug("chain: could not index %s: %s", tx_id, exc)
            return False

    # Already inside an event loop: schedule it rather than blocking the loop
    # this is running on, which would deadlock.
    try:
        asyncio.ensure_future(index(chain, transaction, pending, entry_id))
        return True
    except Exception as exc:
        log.debug("chain: could not schedule the index of %s: %s", tx_id, exc)
        return False


async def ghost_endpoint() -> str:
    """The local chain node. Loopback by default, and configurable only so a
    portable install can move its port -- never so Ghost points off-machine."""
    import os
    host = os.environ.get("TALA_CHAIN_BIND_HOST", "127.0.0.1")
    port = os.environ.get("TALA_CHAIN_BIND_PORT", "8332")
    return f"http://{host}:{port}"


async def _submit(transaction: dict[str, Any]) -> dict[str, Any]:
    """POST one transaction to the shared chain."""
    endpoint = await real_endpoint()
    if not endpoint:
        return {"written": False, "error": "no Real chain endpoint is configured"}
    return await _post(endpoint, transaction, guard=True)


async def _post(endpoint: str, transaction: dict[str, Any],
                guard: bool = True) -> dict[str, Any]:
    """
    POST a transaction to a chain node.

    ``guard`` runs the network guardrails first. The Real chain is an outbound
    connection and is checked; the Ghost node is loopback, which the guard
    exempts anyway, and checking it would make a firewall rule able to sever
    the machine from its own audit trail.
    """
    import httpx

    if not endpoint:
        return {"written": False, "error": "no endpoint"}

    if guard:
        try:
            from python.harness.access import AGENT, Principal
            from python.harness.network_guard import check as net_check

            permitted = await net_check(endpoint, Principal(AGENT, "default"),
                                        purpose="real-chain")
            if not permitted.allowed:
                return {"written": False, "error": permitted.reason}
        except Exception as exc:
            # A guard that cannot be consulted must not silently permit the
            # connection it exists to govern.
            return {"written": False, "error": f"guardrails unavailable: {exc}"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{endpoint.rstrip('/')}/chain/tx",
                                         json=transaction)
        if response.status_code >= 400:
            return {"written": False, "error": f"HTTP {response.status_code}"}
        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass
        remote = ""
        for key in ("tx_id", "transaction_id", "hash", "id"):
            if body.get(key):
                remote = str(body[key])
                break
        return {"written": True, "remote_id": remote}
    except Exception as exc:
        return {"written": False, "error": f"{type(exc).__name__}: {exc}"}


async def _mark_written(entry_id: str, remote_id: str = "") -> None:
    """Clear one queued entry.

    Addressed by entry_id rather than tx_id: a document anchored repeatedly
    shares one tx_id across many entries, and marking by that would clear
    queued transactions that were never actually sent.
    """
    from python.harness import db
    try:
        await db.execute(
            "UPDATE chain_ledger SET pending = FALSE, remote_id = $2, "
            "written_at = NOW() WHERE chain = 'real' AND entry_id = $1",
            entry_id, remote_id, caller="chains.flush",
        )
    except Exception as exc:
        log.warning("chain: could not mark %s as written: %s", tx_id, exc)


async def real_endpoint() -> str:
    """The configured shared-chain endpoint, or empty if there is none."""
    from python.harness import db
    try:
        row = await db.fetchrow(
            "SELECT value FROM app_settings WHERE key = 'real_chain'")
        if row and row["value"]:
            return str(_unwrap(row["value"]).get("endpoint") or "")
    except Exception as exc:
        log.debug("chain: no Real endpoint configured (%s)", exc)
    return ""


async def authorities() -> set[str]:
    """
    Every signer whose blocks this install recognises.

    Includes signers that have since been withdrawn or retired, and that is not
    an oversight. Membership is consulted for every block, historical ones
    included, so dropping a key from this set refuses everything it ever wrote
    — the same "retiring a key collapses its history" failure the revocation
    cutoff exists to prevent, arriving through the other door. Whether a signer
    may sign *now* is answered by :func:`cutoffs`, not by this.

    Empty means no restriction *has been configured*, which is not the same as
    no restriction being wanted, so :func:`reconcile` treats the two cases
    differently rather than quietly importing anything on an unconfigured
    install.
    """
    return set((await _authority_record()).keys())


async def cutoffs() -> dict[str, int]:
    """
    Signers that may no longer sign, and the block they stopped at.

    A withdrawn signer keeps its history and loses its future. The block
    recorded is the shared chain's height at the moment of withdrawal, so
    anything they signed while trusted stays valid and anything after does not.
    """
    return {print_: entry["until"]
            for print_, entry in (await _authority_record()).items()
            if entry.get("until") is not None}


async def _authority_record() -> dict[str, dict[str, Any]]:
    """
    The stored signer list, in its current shape.

    Reads the older plain-list form as well, where every entry is an active
    signer with no cutoff — an install that has not withdrawn anybody has
    nothing to migrate.
    """
    from python.harness import db

    try:
        row = await db.fetchrow(
            "SELECT value FROM app_settings WHERE key = 'real_chain'")
        if not (row and row["value"]):
            return {}
        listed = _unwrap(row["value"]).get("authorities") or []
    except Exception as exc:
        log.debug("chain: could not read the authority list (%s)", exc)
        return {}

    record: dict[str, dict[str, Any]] = {}
    for item in listed:
        if isinstance(item, str):
            print_, until = item.strip(), None
        elif isinstance(item, dict):
            print_ = str(item.get("fingerprint") or "").strip()
            until = item.get("until")
        else:
            continue
        if print_:
            record[print_] = {"until": until}
    return record


async def set_authorities(fingerprints: list[str]) -> set[str]:
    """
    Set which signers may sign from now on.

    Anyone dropped is *withdrawn*, not erased: their entry is kept with a
    cutoff at the current chain height, so the blocks they signed while trusted
    stay valid and nothing they sign afterwards does. Erasing them instead
    would refuse their whole history, which is the failure this guards.
    """
    from python.harness import db

    wanted = {str(item).strip() for item in fingerprints if str(item).strip()}
    existing = await _authority_record()
    height = await _shared_height()

    record: dict[str, dict[str, Any]] = {}
    for print_ in wanted:
        # Re-admitting a withdrawn signer clears its cutoff.
        record[print_] = {"until": None}
    for print_, entry in existing.items():
        if print_ in wanted:
            continue
        record[print_] = {"until": entry["until"] if entry["until"] is not None
                          else height}

    stored = _unwrap((await db.fetchrow(
        "SELECT value FROM app_settings WHERE key = 'real_chain'")
        or {"value": "{}"})["value"])
    stored["authorities"] = [
        {"fingerprint": print_, "until": entry["until"]}
        for print_, entry in sorted(record.items())
    ]
    await db.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES ('real_chain', $1::jsonb, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_at = NOW()",
        json.dumps(stored), caller="chains.set_authorities",
    )
    return wanted


async def active_authorities() -> set[str]:
    """Signers that may sign right now."""
    return {print_ for print_, entry in (await _authority_record()).items()
            if entry.get("until") is None}


async def _shared_height() -> int:
    """How far the shared chain has got, for stamping a withdrawal.

    Falling back to 0 would withdraw a signer from the beginning of time and
    refuse their whole history, so an unreadable height falls back to a cutoff
    beyond any real block instead: they keep everything and sign nothing new
    once the height is known again.
    """
    from python.harness import db

    try:
        highest = await db.fetchval(
            "SELECT MAX((metadata->>'block')::int) FROM chain_ledger "
            "WHERE chain = 'real' AND origin = 'peer'")
        return int(highest) if highest is not None else 0
    except Exception as exc:
        log.debug("chain: could not read the shared chain height (%s)", exc)
        return 0


async def flush() -> dict[str, Any]:
    """
    Write every queued Real transaction, oldest first.

    Called on reconnect. Stops at the first failure rather than continuing:
    if the chain is unreachable the rest will fail too, and hammering it with
    a hundred doomed requests helps nobody.
    """
    from python.harness import db

    try:
        rows = await db.fetch(
            "SELECT entry_id, tx_id, type, counterparty, document, metadata, "
            "occurred_at FROM chain_ledger "
            "WHERE chain = 'real' AND pending = TRUE ORDER BY occurred_at ASC"
        )
    except Exception as exc:
        return {"flushed": 0, "remaining": 0, "error": str(exc)}

    flushed = 0
    for row in rows:
        sent = await _submit({
            "id": row["tx_id"], "type": row["type"],
            "counterparty": row["counterparty"],
            "document_ref": row["document"],
            "metadata": _unwrap(row["metadata"]),
            "timestamp": row["occurred_at"].isoformat() if row["occurred_at"] else "",
        })
        if not sent.get("written"):
            return {"flushed": flushed, "remaining": len(rows) - flushed,
                    "error": sent.get("error", "")}
        await _mark_written(row["entry_id"], sent.get("remote_id", ""))
        flushed += 1

    if flushed:
        log.info("chain: flushed %d queued Real transaction(s)", flushed)
    return {"flushed": flushed, "remaining": 0}


async def summary() -> dict[str, Any]:
    """
    Counts for both chains, so the difference between them is visible.

    Steven asked for exactly this: "I think there should be a way to see the
    diffrence in the two."
    """
    from python.harness import db

    out = {
        "ghost": {"total": 0, "by_type": {}},
        "real": {"total": 0, "pending": 0, "mine": 0, "from_peers": 0,
                 "by_type": {}},
        "endpoint": await real_endpoint(),
        # Where the local chain actually is. The viewer used to hardcode
        # 127.0.0.1:8332, which is only right until somebody moves the port --
        # and a portable install is precisely the case where they do (UI-025).
        "ghost_endpoint": await ghost_endpoint(),
    }
    try:
        rows = await db.fetch(
            "SELECT chain, type, origin, COUNT(*) n, "
            "COUNT(*) FILTER (WHERE pending) pending "
            "FROM chain_ledger GROUP BY chain, type, origin"
        )
        for row in rows:
            chain = out.setdefault(row["chain"], {"total": 0, "by_type": {}})
            chain["total"] += row["n"]
            chain["by_type"][row["type"]] = (
                chain["by_type"].get(row["type"], 0) + row["n"])
            if row["chain"] == REAL:
                chain["pending"] = chain.get("pending", 0) + (row["pending"] or 0)
                # Mine and theirs are different questions about the same chain.
                key = "from_peers" if row["origin"] == "peer" else "mine"
                chain[key] = chain.get(key, 0) + row["n"]
    except Exception as exc:
        out["error"] = str(exc)
    return out


# ── reconcile ────────────────────────────────────────────────────────────────
#
# Writing to a shared chain and never reading it back leaves this install with
# a record of what *it* put there and no idea what anyone else did. Reconcile
# is the read-back.
#
# It pulls, and it deliberately does not use the Node server's own
# ``POST /chain/reconcile``. That endpoint takes a whole chain and *overwrites*
# the receiving node's chain whenever the incoming one is the same height or
# longer. Between one machine and its own backup that is a sane
# longest-chain-wins rule. On a chain several people write to it is data loss:
# two peers who each append while the other is offline both hold a chain at
# least as long as the other's, and whichever pushes second silently destroys
# the first one's blocks. Nothing detects it afterwards, because the discarded
# blocks leave no trace. So reconcile here never pushes a chain and never
# overwrites anything.
#
# Three further limits, each deliberate:
#
# * **Ghost is never touched.** Nothing a peer sends can add to, alter or
#   remove local history. Reconcile writes only Real rows.
# * **The chain is verified here**, not taken on the sender's word.
#   ``GET /chain/status`` reports an ``isValid`` that the node being asked
#   computed about itself, which is worth nothing when the question is whether
#   to trust that node.
# * **All or nothing.** A chain that fails verification is rejected whole. A
#   chain with the bad blocks skipped is not a chain.


async def reconcile(endpoint: str = "") -> dict[str, Any]:
    """
    Pull the shared chain and index what is on it that is not indexed here.

    Returns what changed: transactions newly learned from other peers, and
    local transactions the shared chain confirms it accepted.
    """
    import httpx

    endpoint = (endpoint or await real_endpoint()).strip()
    if not endpoint:
        return {"reconciled": False,
                "reason": "no Real chain endpoint is configured"}

    try:
        from python.harness.access import AGENT, Principal
        from python.harness.network_guard import check as net_check

        permitted = await net_check(endpoint, Principal(AGENT, "default"),
                                    purpose="real-chain-reconcile")
        if not permitted.allowed:
            return {"reconciled": False, "reason": permitted.reason}
    except Exception as exc:
        return {"reconciled": False,
                "reason": f"guardrails unavailable: {exc}"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{endpoint.rstrip('/')}/chain/blocks")
        if response.status_code >= 400:
            return {"reconciled": False,
                    "reason": f"HTTP {response.status_code}"}
        blocks = response.json()
    except Exception as exc:
        return {"reconciled": False,
                "reason": f"{type(exc).__name__}: {exc}"}

    from python.harness import chain_verify

    # Who may sign. An empty list means nobody has said yet, and importing a
    # chain from unknown signers because the question was never answered is
    # precisely the failure this check exists to prevent -- so the pull is
    # refused with an explanation rather than performed permissively.
    permitted = await authorities()
    if not permitted:
        signers = sorted({chain_verify.fingerprint(block.get("public_key"))
                          for block in blocks
                          if isinstance(block, dict) and block.get("public_key")})
        return {
            "reconciled": False,
            "reason": ("no permitted signers are configured for the shared "
                       "chain, so there is nothing to check its blocks against"),
            "signers_seen": signers,
            "height": len(blocks) if isinstance(blocks, list) else 0,
        }

    # Revocations are applied before validity is judged, and before anything is
    # imported. This is where revocation propagates: a key that retired itself
    # is dropped from the local authority list on the next reconcile, on every
    # install that pulls the chain, without anybody being told to do it.
    #
    # Acting on them first also keeps the verdict honest -- otherwise a chain
    # containing the very revocation that explains why a signer is refused
    # would be rejected for containing it.
    withdrawn = await _apply_revocations(blocks, permitted)

    # The retired key stays in the recognised set. Its revocation is what stops
    # it signing anything new, and removing it here would refuse every block it
    # signed while it was trusted — which is the whole chain, permanently.
    verdict = chain_verify.verify(blocks, authorities=permitted,
                                  cutoffs=await cutoffs())
    if not verdict.valid:
        # Say which blocks and why. A shared chain that stops verifying is
        # either corrupted or being tampered with, and "reconcile failed" is
        # not enough to tell those apart.
        log.error("chain: refused a shared chain that does not verify: %s",
                  "; ".join(verdict.problems[:3]))
        return {"reconciled": False, "reason": "the shared chain does not verify",
                "problems": verdict.problems, "height": verdict.height,
                "revoked": sorted(withdrawn)}

    result = await _absorb(chain_verify.transactions_of(blocks), verdict)
    result["revoked"] = sorted(withdrawn)
    return result


async def _apply_revocations(blocks: list[dict[str, Any]],
                             permitted: set[str]) -> set[str]:
    """
    Act on every self-revocation carried by the chain.

    A revocation is not a request. It is a signature only the holder could have
    made, saying their key is no longer to be trusted, so it is applied rather
    than queued for approval -- waiting for someone to click something is
    exactly the wrong behaviour for a key its owner has declared compromised.

    Only keys this install actually trusts are touched. A revocation for a key
    that was never an authority is recorded and changes nothing.
    """
    from python.harness import chain_verify, db

    retired = chain_verify.revocations_in(blocks)
    if not retired:
        return set()

    withdrawn = {print_ for print_ in retired if print_ in permitted}


    # Recorded whether or not it was trusted here, so a key that turns up in an
    # offer later is already known to be retired.
    for print_ in retired:
        try:
            await db.execute(
                """
                INSERT INTO chain_key_offers (fingerprint, public_key, source,
                                              status, note, decided_at)
                VALUES ($1, '', 'shared chain', 'rejected',
                        'retired by its holder', NOW())
                ON CONFLICT (fingerprint) DO UPDATE
                    SET status = 'rejected',
                        note = 'retired by its holder',
                        decided_at = NOW()
                """,
                print_, caller="chains.revocations",
            )
        except Exception as exc:
            log.warning("chain: could not record the revocation of %s: %s",
                        print_, exc)

    if withdrawn:
        # Marked inactive, not erased — see set_authorities. The cutoff comes
        # from the revocation itself, which is more precise than the current
        # height and is what every other node will use too.
        await _stamp_cutoffs({print_: retired[print_] for print_ in withdrawn})
        for print_ in sorted(withdrawn):
            await db.log_to_db(
                "WARNING",
                f"{print_} retired itself and is no longer a permitted signer "
                f"on the shared chain. Blocks it signed before retiring remain "
                f"valid.",
                {"fingerprint": print_, "block": retired[print_]},
            )
        log.warning("chain: withdrew %d retired signer(s): %s",
                    len(withdrawn), ", ".join(sorted(withdrawn)))

    return withdrawn


async def _absorb(transactions: list[dict[str, Any]],
                  verdict: Any) -> dict[str, Any]:
    """Index what the shared chain holds, without disturbing what is here."""
    from python.harness import db

    try:
        rows = await db.fetch(
            "SELECT entry_id, tx_id, pending FROM chain_ledger WHERE chain = 'real'")
    except Exception as exc:
        return {"reconciled": False, "reason": f"could not read the index: {exc}"}

    known = {row["entry_id"] for row in rows}
    # Ours are keyed "real-<tx id>" because we wrote them before they had a
    # position on the chain; theirs arrive keyed by block position. Matching
    # on the transaction id is what lets a round trip recognise itself.
    mine_pending = {row["tx_id"]: row["entry_id"]
                    for row in rows if row["pending"]}

    learned = confirmed = 0
    for transaction in transactions:
        entry_id = transaction["_entry_id"]
        tx_id = str(transaction.get("id") or "")
        if not tx_id:
            continue

        # A pending transaction of ours that is on the chain was accepted --
        # including when the POST succeeded and its response never arrived,
        # which would otherwise leave it queued and resent forever.
        if tx_id in mine_pending:
            await _mark_written(mine_pending.pop(tx_id), entry_id)
            confirmed += 1
            continue

        if entry_id in known:
            continue

        written = await index(
            REAL,
            {
                "id": tx_id,
                "type": str(transaction.get("type") or "unknown"),
                "counterparty": str(transaction.get("counterparty") or ""),
                "document_ref": str(transaction.get("hash") or "")[:512],
                "metadata": {
                    "block": transaction.get("_block_index"),
                    "creator": str(transaction.get("creator") or ""),
                },
                "timestamp": _block_time(transaction.get("_block_timestamp")),
            },
            entry_id=entry_id,
            origin="peer",
        )
        if written.get("written"):
            learned += 1

    log.info("chain: reconciled — %d learned from peers, %d confirmed as ours",
             learned, confirmed)
    return {
        "reconciled": True,
        "height": verdict.height,
        "learned": learned,
        "confirmed": confirmed,
        "still_pending": len(mine_pending),
    }


def _block_time(raw: Any) -> datetime:
    """A block timestamp, which the Node server writes in milliseconds."""
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(
            raw / 1000.0 if raw > 1e11 else raw, tz=timezone.utc)
    return _as_datetime(raw)


async def _stamp_cutoffs(at: dict[str, int]) -> None:
    """Record where each named signer stopped being able to sign."""
    from python.harness import db

    record = await _authority_record()
    for print_, block in at.items():
        record.setdefault(print_, {"until": None})["until"] = block

    stored = _unwrap((await db.fetchrow(
        "SELECT value FROM app_settings WHERE key = 'real_chain'")
        or {"value": "{}"})["value"])
    stored["authorities"] = [
        {"fingerprint": print_, "until": entry["until"]}
        for print_, entry in sorted(record.items())
    ]
    await db.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES ('real_chain', $1::jsonb, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_at = NOW()",
        json.dumps(stored), caller="chains.cutoffs",
    )
