"""
Conversations: one model for five shapes of chat, and the hybrid rule.

Steven named the shapes -- user to agent, user to user, users to users,
users to agent, agents to agents -- and they are the same object with a
different participant list. p2p_messages could not hold any of the plural
ones: peer_username plus direction is structurally a pair.

On membership he chose the hybrid, between approving every participant
individually (strict, and one unapproved person blocks the room) and
approving nothing (simple, and joining a room becomes a way to reach
somebody who never approved you):

    You approve the ROOM. Messages from participants you have not
    individually approved arrive marked as such, and the marking grants them
    nothing else.

Which makes one invariant load-bearing, and most of this file is about it:

    Being in a room with somebody does not upgrade them.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from python.harness import conversations, db, server

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pool():
    await db.init_pool()
    yield
    await db.close_pool()


@pytest_asyncio.fixture
async def people(pool):
    """One approved contact and one who only ever asked."""
    marker = uuid.uuid4().hex[:8]
    approved, pending = f"@ok-{marker}", f"@asked-{marker}"
    for username, status in ((approved, "approved"), (pending, "pending")):
        await db.execute(
            """
            INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret,
                                  permissions, status)
            VALUES ($1, $2, '', $3, '{}'::jsonb, $4)
            """,
            username, f"w-{username}", f"s-{username}", status,
        )
    yield {"approved": approved, "pending": pending}
    await db.execute("DELETE FROM contacts WHERE username = ANY($1::text[])", [approved, pending])


@pytest_asyncio.fixture
async def room(people):
    convo = await conversations.create(
        title="test room",
        participants=[
            {"participant": people["approved"], "kind": "user"},
            {"participant": people["pending"], "kind": "user"},
            {"participant": "tala-rp", "kind": "agent"},
        ],
        created_by="@me",
    )
    yield convo["id"]
    await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


# ---------------------------------------------------------------------------
# One model, any participants
# ---------------------------------------------------------------------------


async def test_a_conversation_holds_more_than_two_people(room, people):
    """Three named in the fixture, plus @me, who started it and is in it."""
    who = await conversations.participants(room)
    assert len(who) == 4
    assert {p["participant"] for p in who} >= {"@me", "tala-rp"}


async def test_users_and_agents_are_both_participants(room):
    """
    The five shapes differ only by who is in the list, so a participant has
    to be able to be either.
    """
    kinds = {p["participant"]: p["kind"] for p in await conversations.participants(room)}
    assert kinds["tala-rp"] == "agent"
    assert any(k == "user" for k in kinds.values())


async def test_messages_belong_to_the_conversation_not_a_pair(room, people):
    await conversations.post(room, people["approved"], "one")
    await conversations.post(room, people["pending"], "two")
    await conversations.post(room, "tala-rp", "three")

    said = await conversations.messages(room)
    assert [m["body"] for m in said] == ["one", "two", "three"]
    assert len({m["sender"] for m in said}) == 3


# ---------------------------------------------------------------------------
# The hybrid rule
# ---------------------------------------------------------------------------


async def test_an_unapproved_participant_can_speak_and_is_marked(room, people):
    """
    Both halves. They are not silenced -- that would be the strict rule --
    and they are not passed off as approved either.
    """
    await conversations.post(room, people["approved"], "from someone you approved")
    await conversations.post(room, people["pending"], "from someone you did not")

    by_sender = {m["sender"]: m for m in await conversations.messages(room)}
    assert by_sender[people["approved"]]["sender_approved"] is True
    assert by_sender[people["pending"]]["sender_approved"] is False


async def test_being_in_a_room_does_not_upgrade_a_contact(room, people):
    """
    The invariant the whole design rests on. Sharing a conversation must not
    let somebody message you directly or touch your nodes -- a room is a
    place you can both speak, not a relationship.
    """
    await conversations.post(room, people["pending"], "hello from the room")

    row = await db.fetchrow(
        "SELECT status FROM contacts WHERE username = $1", people["pending"]
    )
    assert row["status"] == "pending", "the room changed their standing"

    allowed, why = server._peer_interaction_allowed({"status": row["status"]})
    assert allowed is False and why == "not_yet_approved", (
        "a room-mate gained the right to interact outside the room"
    )


async def test_the_marking_is_what_was_true_when_it_was_said(room, people):
    """
    Stored rather than computed at read time. Approving somebody tomorrow
    must not silently rewrite what was true about what they said today.
    """
    await conversations.post(room, people["pending"], "said while unapproved")

    await db.execute(
        "UPDATE contacts SET status = 'approved' WHERE username = $1", people["pending"]
    )

    said = await conversations.messages(room)
    assert said[-1]["sender_approved"] is False, (
        "history was rewritten by a later approval"
    )


async def test_an_unapproved_participant_is_visible_not_hidden(room, people):
    """
    Hiding them would be worse than marking them: you would be in a room
    with somebody you could not see.
    """
    listed = {p["participant"]: p for p in await conversations.participants(room)}
    assert people["pending"] in listed
    assert listed[people["pending"]]["approved"] is False
    assert listed[people["approved"]]["approved"] is True


# ---------------------------------------------------------------------------
# The room-level decision
# ---------------------------------------------------------------------------


async def test_a_conversation_you_started_is_one_you_are_in(room):
    listed = {c["id"]: c for c in await conversations.listing()}
    assert listed[room]["status"] == conversations.STATUS_JOINED


async def test_an_invitation_is_not_membership(pool):
    """
    The hybrid rule approves the room, so there has to be a moment where
    that happens -- which means a state before it.
    """
    convo = await conversations.create(title="invite", status=conversations.STATUS_INVITED)
    try:
        listed = {c["id"]: c for c in await conversations.listing()}
        assert listed[convo["id"]]["status"] == conversations.STATUS_INVITED

        await conversations.set_membership(convo["id"], conversations.STATUS_JOINED)
        listed = {c["id"]: c for c in await conversations.listing()}
        assert listed[convo["id"]]["status"] == conversations.STATUS_JOINED
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_someone_outside_the_room_cannot_speak_in_it(room, people):
    """
    Room membership is what permits speaking, so being an approved contact
    is not standing in a conversation you are not in.
    """
    result = await conversations.post(room, "@stranger", "let me in")

    assert result["posted"] is False
    assert "not in this conversation" in result["reason"]


async def test_an_empty_message_is_not_a_message(room, people):
    result = await conversations.post(room, people["approved"], "   ")
    assert result["posted"] is False


async def test_you_are_in_the_conversation_you_started(pool, people):
    """
    Not merely joined -- *in* it. `status` said you were in the room while
    the participant list did not, so the first thing the person who started
    a conversation tried to do in it was refused: `post` checks membership,
    and the creator was the one identity never added.
    """
    convo = await conversations.create(
        title="mine",
        participants=[{"participant": people["approved"], "kind": "user"}],
        created_by="@me",
    )
    try:
        who = {p["participant"] for p in await conversations.participants(convo["id"])}
        assert "@me" in who, "the person who started the room was not put in it"

        said = await conversations.post(convo["id"], "@me", "first thing I said")
        assert said["posted"] is True, said.get("reason")
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])
