"""
A room that reaches other installs, without the room becoming a relationship.

Steven's rule: **a room participant can only talk to you in that room.**
Membership permits speaking *there*, and grants nothing anywhere else.

Delivering room traffic is where that rule was easiest to break, in a way
that would have looked like it worked. `p2p.receive_message` already exists,
already carries typed packets, and is already relayable -- and it gates on
`_peer_interaction_allowed`, i.e. "have I approved you". Room messages sent
down it would be dropped for every participant who had not been individually
approved, silently turning the hybrid rule into the strict one: rooms that
only work between people who already approved each other, with no error
anywhere to say so.

So `p2p.receive_conversation` is a separate method asking a different
question -- are you in this room, and have I joined it -- and most of this
file is about keeping the two apart.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from python.harness import conversations, db, server, vps_mode

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pool():
    await db.init_pool()
    yield
    await db.close_pool()


@pytest_asyncio.fixture
async def me(pool, monkeypatch):
    """This install's own network identity."""
    name = f"@self-{uuid.uuid4().hex[:8]}"

    async def _self():
        return name

    monkeypatch.setattr(server, "_peer_self_username", _self)
    return name


@pytest_asyncio.fixture
async def peer(pool):
    """A contact who has never been approved. The interesting case."""
    marker = uuid.uuid4().hex[:8]
    username, wallet, secret = f"@asked-{marker}", f"w-{marker}", f"s-{marker}"
    await db.execute(
        """
        INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret,
                              permissions, status)
        VALUES ($1, $2, '', $3, '{}'::jsonb, 'pending')
        """,
        username, wallet, secret,
    )
    yield {"username": username, "wallet": wallet, "secret": secret}
    await db.execute("DELETE FROM contacts WHERE username = $1", username)


@pytest.fixture
def announced(monkeypatch):
    sent = []

    async def fake_broadcast(method, params=None):
        sent.append((method, params or {}))
        return 1

    monkeypatch.setattr(server, "broadcast_notification", fake_broadcast)
    return sent


def conversation_packet(peer, conversation_id, secret=None, **body):
    """A packet as the peer would sign it."""
    return server._sign_packet(
        {
            "protocol": "tala-p2p/1.0",
            "type": "conversation",
            "sender_wallet": peer["wallet"],
            "recipient_wallet": "local-tala",
            "conversation_id": conversation_id,
            "timestamp": "2026-09-06T00:00:00+00:00",
            **body,
        },
        secret or peer["secret"],
    )


async def receive(packet):
    import json

    raw = await server.handle_p2p_receive_conversation({"packet": packet}, 1)
    return json.loads(raw).get("result") or json.loads(raw)


# ---------------------------------------------------------------------------
# The rule the delivery path exists to hold
# ---------------------------------------------------------------------------


async def test_an_unapproved_participant_can_speak_in_the_room(me, peer, announced):
    """
    The whole point. Routed through `p2p.receive_message` this message would
    be refused as `not_yet_approved`, and the hybrid rule would be dead
    without a single failing test to show it.
    """
    convo = await conversations.create(
        title="room",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
        created_by=me,
    )
    try:
        result = await receive(conversation_packet(
            peer, convo["id"], kind="message", body="hello from the room"
        ))
        assert result["accepted"] is True, result

        said = await conversations.messages(convo["id"])
        assert said[-1]["body"] == "hello from the room"
        assert said[-1]["sender_approved"] is False, (
            "an unapproved sender was recorded as approved"
        )
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_the_room_does_not_open_the_direct_channel(me, peer):
    """
    The other half, and the invariant the design rests on. Speaking in a room
    must leave the direct path exactly as refused as it was.
    """
    convo = await conversations.create(
        title="room",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
        created_by=me,
    )
    try:
        await receive(conversation_packet(peer, convo["id"], kind="message", body="hi"))

        row = await db.fetchrow(
            "SELECT status FROM contacts WHERE username = $1", peer["username"]
        )
        assert row["status"] == "pending", "speaking in a room changed their standing"

        allowed, why = server._peer_interaction_allowed({"status": row["status"]})
        assert allowed is False and why == "not_yet_approved", (
            "a room-mate gained the right to interact outside the room"
        )
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_a_participant_cannot_speak_in_a_room_they_are_not_in(me, peer):
    """
    Scoped to *that* room. Membership in one conversation is not standing in
    another, or the rule would be "a participant can talk to you", full stop.
    """
    theirs = await conversations.create(
        title="ours",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
        created_by=me,
    )
    other = await conversations.create(
        title="not theirs",
        participants=[{"participant": me, "kind": "user"}],
        created_by=me,
    )
    try:
        result = await receive(conversation_packet(
            peer, other["id"], kind="message", body="let me in"
        ))
        assert result["accepted"] is False
        assert "not in this conversation" in result["reason"]
    finally:
        await db.execute(
            "DELETE FROM conversations WHERE id = ANY($1::text[])", [theirs["id"], other["id"]]
        )


async def test_nobody_can_speak_into_an_invitation_you_have_not_answered(me, peer):
    """
    Being invited somewhere is not being there. Until the room-level decision
    is made there is no room anybody is in with this install, so there is no
    scope for the rule to grant.
    """
    convo = await conversations.create(
        title="unanswered",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
        created_by=peer["username"],
        status=conversations.STATUS_INVITED,
    )
    try:
        result = await receive(conversation_packet(
            peer, convo["id"], kind="message", body="starting without you"
        ))
        assert result["accepted"] is False
        assert result["reason"] == "conversation_invited"
        assert await conversations.messages(convo["id"]) == []

        await conversations.set_membership(convo["id"], conversations.STATUS_JOINED)
        result = await receive(conversation_packet(
            peer, convo["id"], kind="message", body="now then"
        ))
        assert result["accepted"] is True, "joining the room did not open it"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_a_declined_room_stays_closed(me, peer):
    convo = await conversations.create(
        title="declined",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
        created_by=peer["username"],
        status=conversations.STATUS_DECLINED,
    )
    try:
        result = await receive(conversation_packet(
            peer, convo["id"], kind="message", body="still here"
        ))
        assert result["accepted"] is False
        assert result["reason"] == "conversation_declined"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


# ---------------------------------------------------------------------------
# Who may reach this path at all
# ---------------------------------------------------------------------------


async def test_a_stranger_cannot_reach_the_room_path(me, pool):
    result = await receive({
        "protocol": "tala-p2p/1.0", "type": "conversation",
        "sender_wallet": f"w-nobody-{uuid.uuid4().hex[:8]}",
        "conversation_id": "conv-anything", "kind": "message", "body": "hello",
        "signature": "0" * 64,
    })
    assert result["accepted"] is False
    assert result["reason"] == "unknown_sender"


async def test_a_forged_packet_is_refused(me, peer):
    convo = await conversations.create(
        title="room",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
        created_by=me,
    )
    try:
        result = await receive(conversation_packet(
            peer, convo["id"], secret="not-the-shared-secret",
            kind="message", body="pretending to be them",
        ))
        assert result["accepted"] is False
        assert result["reason"] == "bad_signature"
        assert await conversations.messages(convo["id"]) == []
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


async def test_an_invitation_arrives_as_an_invitation(me, peer, announced):
    """
    Recorded, and never joined. A room that joined itself on arrival would
    make the room-level decision -- the one thing the hybrid rule reserves
    for a person -- happen automatically for whoever asked first.
    """
    conversation_id = f"conv-{uuid.uuid4().hex[:16]}"
    try:
        result = await receive(conversation_packet(
            peer, conversation_id, kind="invitation", title="their room",
            participants=[{"participant": peer["username"], "kind": "user"},
                          {"participant": me, "kind": "user"}],
        ))
        assert result["accepted"] is True

        assert await conversations.status_of(conversation_id) == conversations.STATUS_INVITED
        who = {p["participant"] for p in await conversations.participants(conversation_id)}
        assert who == {peer["username"], me}

        invites = [p for m, p in announced if m == "conversation.invited"]
        assert invites, "an invitation was recorded and nobody was told"
        assert invites[0]["from"] == peer["username"]
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", conversation_id)


async def test_an_invitation_you_are_not_in_is_refused(me, peer):
    """A room somebody else is in is not an invitation to this install."""
    conversation_id = f"conv-{uuid.uuid4().hex[:16]}"
    result = await receive(conversation_packet(
        peer, conversation_id, kind="invitation", title="theirs",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": "@someone-else", "kind": "user"}],
    ))
    assert result["accepted"] is False
    assert result["reason"] == "not_invited"
    assert await conversations.status_of(conversation_id) == ""


async def test_the_inviter_has_to_be_in_the_room_they_invite_you_to(me, peer):
    """
    Otherwise a contact could put this install in a room with people it has
    no relationship with and then not be there itself.
    """
    conversation_id = f"conv-{uuid.uuid4().hex[:16]}"
    result = await receive(conversation_packet(
        peer, conversation_id, kind="invitation", title="not mine",
        participants=[{"participant": me, "kind": "user"},
                      {"participant": "@a-stranger", "kind": "user"}],
    ))
    assert result["accepted"] is False
    assert result["reason"] == "sender_not_in_room"


async def test_the_same_invitation_twice_is_one_room(me, peer, announced):
    conversation_id = f"conv-{uuid.uuid4().hex[:16]}"
    packet = conversation_packet(
        peer, conversation_id, kind="invitation", title="once",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
    )
    try:
        first, second = await receive(packet), await receive(packet)
        assert first["created"] is True and second["created"] is False
        rooms = [c for c in await conversations.listing() if c["id"] == conversation_id]
        assert len(rooms) == 1
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", conversation_id)


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


async def test_a_post_reaches_the_other_people_in_the_room(me, peer, announced, monkeypatch):
    sent = []

    async def fake_deliver(target, conversation_id, packet_body):
        sent.append((target, conversation_id, packet_body))
        return {"peer": target, "delivered": True, "reason": ""}

    monkeypatch.setattr(server, "_deliver_conversation_packet", fake_deliver)

    convo = await conversations.create(
        title="room",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"},
                      {"participant": "tala-rp", "kind": "agent"}],
        created_by=me,
    )
    try:
        await server._fan_out_conversation(
            convo["id"], sender=me, packet_body={"kind": "message", "body": "out"}
        )
        assert [t for t, _, _ in sent] == [peer["username"]], (
            "fan-out went to the wrong set of participants"
        )
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_fan_out_never_delivers_back_to_this_install(me, peer, announced, monkeypatch):
    """
    A message posted on behalf of a local agent carries the agent's name, so
    excluding the sender is not enough to exclude ourselves.
    """
    sent = []

    async def fake_deliver(target, conversation_id, packet_body):
        sent.append(target)
        return {"peer": target, "delivered": True, "reason": ""}

    monkeypatch.setattr(server, "_deliver_conversation_packet", fake_deliver)

    convo = await conversations.create(
        title="room",
        participants=[{"participant": me, "kind": "user"},
                      {"participant": peer["username"], "kind": "user"}],
        created_by=me,
    )
    try:
        await server._fan_out_conversation(
            convo["id"], sender="tala-rp", packet_body={"kind": "message", "body": "from the agent"}
        )
        assert me not in sent, "this install was sent its own room event"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_an_unreachable_participant_does_not_fail_the_others(me, peer, announced, monkeypatch):
    """
    The message is already recorded before fan-out runs. Somebody's shut
    laptop must not be able to fail a post, or take the rest of the room
    down with it.
    """
    marker = uuid.uuid4().hex[:8]
    other = f"@up-{marker}"
    await db.execute(
        """
        INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret,
                              permissions, status)
        VALUES ($1, $2, '', $3, '{}'::jsonb, 'approved')
        """,
        other, f"w-{marker}", f"s-{marker}",
    )

    async def flaky_deliver(target, conversation_id, packet_body):
        if target == peer["username"]:
            return {"peer": target, "delivered": False, "reason": "timed out"}
        return {"peer": target, "delivered": True, "reason": ""}

    monkeypatch.setattr(server, "_deliver_conversation_packet", flaky_deliver)

    convo = await conversations.create(
        title="room",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": other, "kind": "user"},
                      {"participant": me, "kind": "user"}],
        created_by=me,
    )
    try:
        results = await server._fan_out_conversation(
            convo["id"], sender=me, packet_body={"kind": "message", "body": "out"}
        )
        by_peer = {r["peer"]: r for r in results}
        assert by_peer[other]["delivered"] is True
        assert by_peer[peer["username"]]["delivered"] is False

        reports = [p for m, p in announced if m == "conversation.delivery"]
        assert reports, "delivery outcomes were never reported"
        assert reports[0]["delivered"] == [other]
        assert reports[0]["failed"][0]["peer"] == peer["username"]
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])
        await db.execute("DELETE FROM contacts WHERE username = $1", other)


async def test_an_invitation_does_not_publish_who_you_have_approved(me, peer, announced, monkeypatch):
    """
    `participants()` reports which of them this install has approved.
    Forwarding that wholesale would tell every recipient the standing of
    everybody else in this install's contact list.
    """
    sent = []

    async def fake_deliver(target, conversation_id, packet_body):
        sent.append(packet_body)
        return {"peer": target, "delivered": True, "reason": ""}

    monkeypatch.setattr(server, "_deliver_conversation_packet", fake_deliver)

    import json

    raw = await server.handle_conversation_create({
        "title": "room",
        "participants": [{"participant": peer["username"], "kind": "user"},
                         {"participant": me, "kind": "user"}],
    }, 1)
    created = json.loads(raw)["result"]
    try:
        # The handler fans out in the background so posting is not as slow as
        # the least reachable person in the room.
        import asyncio

        for _ in range(50):
            if sent:
                break
            await asyncio.sleep(0.01)

        assert sent, "nobody was told the room exists"
        for person in sent[0]["participants"]:
            assert set(person) == {"participant", "kind"}, (
                f"the invitation carried more than a name: {sorted(person)}"
            )
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", created["id"])


# ---------------------------------------------------------------------------
# The relay
# ---------------------------------------------------------------------------


async def test_the_room_method_is_relayable():
    """
    Most installs are behind NAT and have no `endpoint_url`, so a room that
    is not relayable is a room that only works between people who can dial
    each other -- which is almost nobody.
    """
    assert "p2p.receive_conversation" in vps_mode.RELAYABLE_METHODS


async def test_the_relay_records_the_room_and_not_what_was_said():
    """
    The Real chain is shared, so a body written to it is a body published.
    A relay is carrying other people's conversations.
    """
    subject = server._relayed_document_ref("p2p.receive_conversation", {
        "packet": {"conversation_id": "conv-abc123", "kind": "message",
                   "body": "something private", "title": "a room name"},
    })
    assert subject == "conversation:conv-abc123"
    assert "private" not in subject and "room name" not in subject


# ---------------------------------------------------------------------------
# Leaving
# ---------------------------------------------------------------------------


async def test_leaving_ends_what_the_room_permitted(me, peer):
    """
    The rule scopes somebody's reach to a room. Leaving is how that scope
    ends -- so it has to close the door itself, not merely tidy a tab away.
    """
    convo = await conversations.create(
        title="room",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
        created_by=me,
    )
    try:
        assert (await receive(conversation_packet(
            peer, convo["id"], kind="message", body="while you were here"
        )))["accepted"] is True

        await conversations.set_membership(convo["id"], conversations.STATUS_LEFT)

        result = await receive(conversation_packet(
            peer, convo["id"], kind="message", body="after you left"
        ))
        assert result["accepted"] is False
        assert result["reason"] == "conversation_left"

        said = [m["body"] for m in await conversations.messages(convo["id"])]
        assert said == ["while you were here"], "a message landed in a room nobody is in"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_leaving_a_room_is_not_leaving_it_forever(me, peer, announced):
    """
    A fresh invitation to a room this install walked out of is a fresh
    question. Without this the invitation would land on a closed row and
    silently change nothing, so leaving once would mean never being asked
    back.
    """
    conversation_id = f"conv-{uuid.uuid4().hex[:16]}"
    invitation = conversation_packet(
        peer, conversation_id, kind="invitation", title="come back",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
    )
    try:
        await receive(invitation)
        await conversations.set_membership(conversation_id, conversations.STATUS_JOINED)
        await conversations.set_membership(conversation_id, conversations.STATUS_LEFT)

        again = await receive(invitation)
        assert again["accepted"] is True
        assert await conversations.status_of(conversation_id) == conversations.STATUS_INVITED, (
            "a room that was left could never be offered again"
        )
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", conversation_id)


async def test_leaving_does_not_reopen_on_a_message(me, peer):
    """
    Only an invitation reopens a room, and only to `invited`. A message must
    never be the thing that puts this install back in a room it left.
    """
    convo = await conversations.create(
        title="room",
        participants=[{"participant": peer["username"], "kind": "user"},
                      {"participant": me, "kind": "user"}],
        created_by=me,
    )
    try:
        await conversations.set_membership(convo["id"], conversations.STATUS_LEFT)
        await receive(conversation_packet(peer, convo["id"], kind="message", body="knock"))
        assert await conversations.status_of(convo["id"]) == conversations.STATUS_LEFT
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])
