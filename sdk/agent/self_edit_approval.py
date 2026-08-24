"""
self_edit_approval.py — Tala proposes changes to her own source; Steven approves.

Answers Q-68. She can already write to ``renderer/``, ``sdk/`` and
``python/harness/`` unsupervised — ``_PROTECTED_WRITE_PATTERNS`` guards her
*identity*, not her code. This puts an approval step in front of her own source
while leaving her nodes, docs and scratch work alone.

**A proposal is an ordinary task with status ``review``.** No parallel queue and
no bespoke store: Mission Control already renders every task and ``review`` is
already a status the board understands, so a pending change shows up on the
screen the operator is already watching. Approval is ``task.control`` with
``action="approve"``, alongside the existing pause / resume / abort.

Two properties worth keeping:

* **Approving is not a tool.** Under UI-118 the owner holds administrative
  standing; agents hold assigned privileges. Approval happens over RPC from the
  UI, so an agent cannot approve itself.
* **A proposal is content, not a promise.** The full intended file content is
  stored at propose time and matched by hash at write time, so what Steven sees
  is exactly what lands — an agent cannot show one thing and write another.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

#: Paths that are Tala's own machinery. Writes here need approval.
#: Deliberately excludes nodes/, docs/ and scratch/ — her memory and her working
#: output are hers to change, and gating those would make her useless.
SELF_SOURCE_PATTERNS: tuple[str, ...] = (
    "renderer/*",
    "electron/*",
    "sdk/*",
    "python/*",
    "scripts/*",
    "blockchain/src/*",
    "package.json",
    "pyproject.toml",
)

#: Marks a task as a self-edit proposal rather than ordinary work.
PAYLOAD_KIND = "self_edit"


def is_self_source(rel_path: str) -> bool:
    """True if *rel_path* is part of Tala's own machinery."""
    posix = str(rel_path).replace("\\", "/").lstrip("./")
    return any(fnmatch.fnmatch(posix, pat) for pat in SELF_SOURCE_PATTERNS)


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _payload(row: Any) -> dict[str, Any]:
    raw = row["payload"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw or {}


async def propose(rel_path: str, content: str, reason: str, agent_id: str = "") -> dict[str, Any]:
    """Queue a change to Tala's own source as a task awaiting review."""
    from python.harness import db

    posix = str(rel_path).replace("\\", "/")
    task_id = f"selfedit-{uuid.uuid4().hex[:10]}"
    payload = {
        "kind": PAYLOAD_KIND,
        "path": posix,
        "content": content,
        "content_sha256": digest(content),
        "reason": reason,
        "agent_id": agent_id or "unknown",
    }
    await db.execute(
        """
        INSERT INTO tasks (id, description, status, payload)
        VALUES ($1, $2, 'review', $3::jsonb)
        """,
        task_id,
        f"Approve change to {posix}: {reason}"[:300],
        json.dumps(payload),
        caller="self_edit_approval.propose",
    )
    return {
        "success": True,
        "task_id": task_id,
        "path": posix,
        "status": "review",
        "detail": (
            "Queued for approval. This file is part of Tala's own source, so the change "
            "does not land until it is approved in Mission Control."
        ),
    }


async def approved_task_for(rel_path: str, content: str) -> str | None:
    """Return the id of an approved proposal matching this exact write, if any."""
    from python.harness import db

    posix = str(rel_path).replace("\\", "/")
    want = digest(content)
    rows = await db.fetch(
        "SELECT id, payload FROM tasks WHERE status = 'ready' AND id LIKE 'selfedit-%'"
    )
    for row in rows:
        p = _payload(row)
        if p.get("kind") == PAYLOAD_KIND and p.get("path") == posix \
                and p.get("content_sha256") == want:
            return row["id"]
    return None


async def consume(task_id: str) -> None:
    """Mark an approval used, so it cannot authorise a second write."""
    from python.harness import db

    await db.execute(
        "UPDATE tasks SET status = 'done', completed_at = NOW() WHERE id = $1",
        task_id,
        caller="self_edit_approval.consume",
    )


async def decide(task_id: str, approve: bool, note: str = "") -> dict[str, Any]:
    """
    Operator decision. Called over RPC from Mission Control, never by a tool.

    ``review`` -> ``ready`` means approved and awaiting application;
    ``review`` -> ``failed`` means rejected.
    """
    from python.harness import db

    row = await db.fetchrow("SELECT id, status, payload FROM tasks WHERE id = $1", task_id)
    if row is None:
        return {"success": False, "error": "unknown_proposal",
                "detail": f"No proposal {task_id!r}."}
    if row["status"] != "review":
        return {"success": False, "error": "already_decided",
                "detail": f"{task_id} is {row['status']}, not awaiting review."}

    payload = _payload(row)
    if note:
        payload["operator_note"] = note
    payload["decision"] = "approved" if approve else "rejected"

    await db.execute(
        "UPDATE tasks SET status = $1, payload = $2::jsonb WHERE id = $3",
        "ready" if approve else "failed",
        json.dumps(payload),
        task_id,
        caller="self_edit_approval.decide",
    )
    return {"success": True, "task_id": task_id,
            "status": "approved" if approve else "rejected",
            "path": payload.get("path")}
