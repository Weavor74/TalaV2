"""
conversations.py — one model for every shape of chat.

Steven named five:

    user to agent, user to user, users to users, users to agent, agents to agents

They are the same object with a different participant list. A participant is
an identity -- a peer username, or a local agent id -- and a message belongs
to the conversation rather than to a pair. That is the whole difference from
``p2p_messages``, whose ``peer_username`` + ``direction`` columns make it
structurally incapable of holding a third party.

THE HYBRID RULE
---------------

Steven's choice, between approving every participant individually (strict,
and one unapproved person blocks the room) and approving nothing at all
(simple, and joining a room becomes a way to reach somebody who never
approved you):

    You approve the ROOM. Messages from participants you have not
    individually approved arrive marked as such. The marking grants them
    nothing else.

So the invariant this module exists to hold:

    **Being in a room with somebody does not upgrade them.**

Sharing a conversation must not let a peer message you directly, resolve
your nodes, or do anything `_peer_interaction_allowed` would refuse. A room
is a place you can both speak, not a relationship. Everything outside the
room still runs through pairwise approval, unchanged.

``sender_approved`` is stored on the message rather than computed when it is
read, because it is a fact about the message. Approving somebody tomorrow
should not silently rewrite what was true about what they said today.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

#: A conversation somebody was invited to is not one they are in.
STATUS_INVITED = "invited"
STATUS_JOINED = "joined"
STATUS_DECLINED = "declined"

KIND_USER = "user"
KIND_AGENT = "agent"


def _new_id() -> str:
    return f"conv-{uuid.uuid4().hex[:16]}"


async def create(
    title: str = "",
    participants: list[dict[str, str]] | None = None,
    created_by: str = "",
    status: str = STATUS_JOINED,
) -> dict[str, Any]:
    """
    Start a conversation.

    Defaults to ``joined`` because a conversation you started is one you are
    in; an incoming invitation is recorded as ``invited`` by the receive path
    instead, which is where the room-level decision actually happens.
    """
    from python.harness import db

    conversation_id = _new_id()
    await db.execute(
        "INSERT INTO conversations (id, title, created_by, status) VALUES ($1, $2, $3, $4)",
        conversation_id, title, created_by, status,
        caller="conversations.create",
    )
    for participant in participants or []:
        await _add_participant(
            conversation_id,
            participant["participant"],
            participant.get("kind", KIND_USER),
        )
    log.info("conversation %s created with %d participant(s)", conversation_id, len(participants or []))
    return {"id": conversation_id, "title": title, "status": status}


async def _add_participant(conversation_id: str, participant: str, kind: str = KIND_USER) -> None:
    from python.harness import db

    await db.execute(
        """
        INSERT INTO conversation_participants (conversation_id, participant, participant_kind)
        VALUES ($1, $2, $3)
        ON CONFLICT (conversation_id, participant) DO NOTHING
        """,
        conversation_id, participant, kind,
        caller="conversations.add_participant",
    )


async def participants(conversation_id: str) -> list[dict[str, Any]]:
    """
    Who is in this conversation, and which of them you have approved.

    The approval is reported alongside rather than filtered on: the hybrid
    rule means an unapproved participant is present and visible, not hidden.
    Hiding them would be worse than marking them -- you would be in a room
    with somebody you could not see.
    """
    from python.harness import db

    rows = await db.fetch(
        """
        SELECT p.participant, p.participant_kind, c.status AS contact_status
        FROM conversation_participants p
        LEFT JOIN contacts c ON c.username = p.participant
        WHERE p.conversation_id = $1
        ORDER BY p.joined_at
        """,
        conversation_id,
    )
    return [
        {
            "participant": r["participant"],
            "kind": r["participant_kind"],
            "approved": (r["contact_status"] == "approved"),
            "contact_status": r["contact_status"],
        }
        for r in rows
    ]


async def _is_participant(conversation_id: str, participant: str) -> bool:
    from python.harness import db

    row = await db.fetchrow(
        "SELECT 1 FROM conversation_participants WHERE conversation_id = $1 AND participant = $2",
        conversation_id, participant,
    )
    return row is not None


async def _sender_is_approved(sender: str) -> bool:
    from python.harness import db

    row = await db.fetchrow("SELECT status FROM contacts WHERE username = $1", sender)
    return bool(row and row["status"] == "approved")


async def post(conversation_id: str, sender: str, body: str) -> dict[str, Any]:
    """
    Record a message in a conversation.

    Refuses a sender who is not a participant. Room membership is what
    permits speaking here -- that is the hybrid rule -- so somebody outside
    the room has no standing in it, approved contact or not.
    """
    from python.harness import db

    body = str(body or "").strip()
    if not body:
        return {"posted": False, "reason": "there is no message to post"}

    if not await _is_participant(conversation_id, sender):
        return {"posted": False, "reason": f"{sender} is not in this conversation"}

    approved = await _sender_is_approved(sender)
    row = await db.fetchrow(
        """
        INSERT INTO conversation_messages (conversation_id, sender, body, sender_approved)
        VALUES ($1, $2, $3, $4) RETURNING id, created_at
        """,
        conversation_id, sender, body, approved,
    )
    await db.execute(
        "UPDATE conversations SET updated_at = NOW() WHERE id = $1", conversation_id
    )
    return {
        "posted": True,
        "id": row["id"],
        "sender": sender,
        "sender_approved": approved,
        "at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def messages(conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
    from python.harness import db

    rows = await db.fetch(
        """
        SELECT id, sender, body, sender_approved, created_at
        FROM conversation_messages WHERE conversation_id = $1
        ORDER BY created_at DESC LIMIT $2
        """,
        conversation_id, int(limit),
    )
    return [
        {
            "id": r["id"],
            "sender": r["sender"],
            "body": r["body"],
            # The marking the hybrid rule turns on. A reader can tell whose
            # words they are trusting.
            "sender_approved": r["sender_approved"],
            "at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in reversed(rows)
    ]


async def listing(include_invited: bool = True) -> list[dict[str, Any]]:
    from python.harness import db

    rows = await db.fetch(
        """
        SELECT c.id, c.title, c.status, c.updated_at,
               (SELECT count(*) FROM conversation_participants p WHERE p.conversation_id = c.id) AS people,
               (SELECT count(*) FROM conversation_messages m WHERE m.conversation_id = c.id) AS said
        FROM conversations c
        ORDER BY c.updated_at DESC
        """
    )
    return [
        {
            "id": r["id"], "title": r["title"], "status": r["status"],
            "participants": r["people"], "messages": r["said"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
        if include_invited or r["status"] != STATUS_INVITED
    ]


async def set_membership(conversation_id: str, status: str) -> dict[str, Any]:
    """
    Join or decline a conversation you were invited to.

    This is the room-level decision the hybrid rule rests on: the one place
    a person says yes to being somewhere, as distinct from saying yes to a
    person.
    """
    from python.harness import db

    if status not in (STATUS_INVITED, STATUS_JOINED, STATUS_DECLINED):
        return {"ok": False, "reason": f"unknown status {status!r}"}

    await db.execute(
        "UPDATE conversations SET status = $2, updated_at = NOW() WHERE id = $1",
        conversation_id, status,
        caller="conversations.set_membership",
    )
    return {"ok": True, "id": conversation_id, "status": status}
