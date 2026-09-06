"""
node_admission.py — whether a node carrying code is allowed to run it.

A workflow node names steps that existing Python interprets, so the worst a
hostile one can do is ask for a bad combination of things already
implemented. A function node carries the code itself. That is a different
kind of object, and it needs a different kind of permission.

The rule, and the reason for every part of this module:

    **A node's own claim about itself never grants execution.**

Not ``data-execution-authority``, not a flag in ``state_data``, not a field
called ``trusted``. Those are all written by whoever wrote the node, which
in the case that matters is not the person running it. Permission lives
outside the file, in a record the node cannot reach.

So admission is a row in the database keyed by node id *and content hash*.
The hash is the load-bearing half: admitting a node admits **that code**.
Edit one line and the hash moves, the admission no longer matches, and the
node is inert again until somebody looks at what changed. Without it,
"admitted once" would mean "admitted forever, including for code substituted
afterwards", which is the whole attack.

Steven: "Never a start on receive, only a start after review." Arrival
records provenance and nothing else. Review is a separate act, and it is a
person's, informed by an analysis the model writes.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

log = logging.getLogger(__name__)

#: Where a node came from. Recorded on arrival, never inferred later.
ORIGIN_LOCAL = "local"


def content_hash(source: str) -> str:
    """
    The identity of this exact code.

    Over the whole node file rather than the code field alone: a function
    node's behaviour is not only its ``code``. Its inputs, its declared type
    and its authority all shape what running it does, and a change to any of
    them deserves another look.
    """
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def record_arrival(
    node_id: str,
    source: str,
    origin: str,
    *,
    admitted: bool = False,
    review: str = "",
    reviewed_by: str = "",
) -> dict[str, Any]:
    """
    Note that a node exists and where it came from. Grants nothing by default.

    ``admitted`` is only ever True for something authored here, and even then
    it is the caller stating it rather than the node claiming it.
    """
    from python.harness import db

    digest = content_hash(source)
    await db.execute(
        """
        INSERT INTO node_admissions (node_id, content_hash, origin, admitted, review, reviewed_by)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (node_id, content_hash) DO UPDATE SET
            origin = EXCLUDED.origin,
            admitted = EXCLUDED.admitted,
            review = EXCLUDED.review,
            reviewed_by = EXCLUDED.reviewed_by,
            updated_at = NOW()
        """,
        node_id, digest, origin, bool(admitted), review, reviewed_by,
        caller="node_admission.record_arrival",
    )
    log.info(
        "node_admission: recorded %s from %s (%s)",
        node_id, origin, "admitted" if admitted else "awaiting review",
    )
    return {"node_id": node_id, "content_hash": digest, "origin": origin, "admitted": bool(admitted)}


async def admit(node_id: str, source: str, *, review: str, reviewed_by: str) -> dict[str, Any]:
    """
    Permit this exact code to run, on the record.

    A review is required rather than optional. An admission with no account
    of what was read is indistinguishable from one nobody thought about, and
    the point of the gate is that somebody looked.
    """
    from python.harness import db

    review = str(review or "").strip()
    if not review:
        return {"admitted": False, "reason": "an admission needs a review saying what was read"}

    # Keep where it came from. Admitting a node received from a peer must not
    # relabel it as locally authored -- provenance is the thing you would want
    # most if the code later turned out to be a problem, and overwriting it at
    # the moment of admission destroys it exactly when it starts to matter.
    digest = content_hash(source)
    prior = await db.fetchrow(
        "SELECT origin FROM node_admissions WHERE node_id = $1 AND content_hash = $2",
        node_id, digest,
    )
    origin = (prior["origin"] if prior else None) or ORIGIN_LOCAL

    return await record_arrival(
        node_id, source, origin,
        admitted=True, review=review, reviewed_by=reviewed_by or "unknown",
    ) | {"admitted": True}


async def is_admitted(node_id: str, source: str) -> tuple[bool, str]:
    """
    May this exact code run? Returns (ok, why_not).

    The reason distinguishes the three failures a caller needs to tell
    apart, because they call for different responses: never seen, seen and
    not yet reviewed, and reviewed but since edited. A single "refused"
    would leave a person guessing which.
    """
    from python.harness import db

    digest = content_hash(source)
    row = await db.fetchrow(
        "SELECT admitted, origin FROM node_admissions WHERE node_id = $1 AND content_hash = $2",
        node_id, digest,
    )
    if row is not None:
        if row["admitted"]:
            return True, ""
        return False, "this node is recorded but has not been admitted; it needs review"

    prior = await db.fetchrow(
        "SELECT admitted FROM node_admissions WHERE node_id = $1 ORDER BY updated_at DESC LIMIT 1",
        node_id,
    )
    if prior is not None:
        return False, (
            "the code has changed since this node was admitted. An admission covers "
            "the exact code that was read, so the edited version needs reviewing again"
        )

    return False, "this node has never been admitted; nothing has reviewed its code"


async def status(node_id: str) -> dict[str, Any]:
    """Everything recorded about a node, for a person deciding about it."""
    from python.harness import db

    rows = await db.fetch(
        """
        SELECT content_hash, origin, admitted, review, reviewed_by, created_at, updated_at
        FROM node_admissions WHERE node_id = $1 ORDER BY updated_at DESC
        """,
        node_id,
    )
    return {
        "node_id": node_id,
        "records": [
            {
                "content_hash": r["content_hash"][:16],
                "origin": r["origin"],
                "admitted": r["admitted"],
                "review": r["review"],
                "reviewed_by": r["reviewed_by"],
                "recorded_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }
