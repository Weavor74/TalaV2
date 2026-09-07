"""
An agent that is in the room gets a turn in it.

Steven named five shapes -- "User to agent. User to user. Users to users,
users to agent, agents to agents". Four of them worked the moment messages
were delivered. The fifth needs somebody to notice that an agent is a
participant and run it, because an agent nothing invokes is a name in a
list: the room would show `tala-rp` present and silent forever.

Two things make a room different from the pair `_handle_inbound_a2a`
already handled, and both are what this file is about.

A room has more than two speakers, so the transcript handed to the model
cannot be an unlabelled two-role one. Three people become three `user`
turns and it reads as one person contradicting themselves.

And a room can hold two agents. That is a shape Steven asked for, so it is
bounded rather than banned -- otherwise they answer each other until
something falls over, and every turn is a real inference call.
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
async def me(pool, monkeypatch):
    name = f"@self-{uuid.uuid4().hex[:8]}"

    async def _self():
        return name

    monkeypatch.setattr(server, "_peer_self_username", _self)
    return name


@pytest.fixture
def replies(monkeypatch):
    """Capture what the model was asked, and answer for it."""
    asked = []

    async def fake_generate(messages, max_tokens=512, temperature=0.7):
        asked.append(messages)
        return f"reply {len(asked)}"

    monkeypatch.setattr(server, "_generate_reply", fake_generate)
    monkeypatch.setattr(server, "_fan_out_conversation", _no_fan_out)
    return asked


async def _no_fan_out(conversation_id, sender, packet_body):
    return []


@pytest.fixture
def announced(monkeypatch):
    sent = []

    async def fake_broadcast(method, params=None):
        sent.append((method, params or {}))
        return 1

    monkeypatch.setattr(server, "broadcast_notification", fake_broadcast)
    return sent


# ---------------------------------------------------------------------------
# The turn itself
# ---------------------------------------------------------------------------


async def test_an_agent_in_the_room_answers(me, replies, announced):
    convo = await conversations.create(
        title="with tala",
        participants=[{"participant": "tala-core", "kind": "agent"}],
        created_by=me,
    )
    try:
        await conversations.post(convo["id"], me, "are you there?")
        await server._agents_take_a_turn(convo["id"], sender=me)

        said = await conversations.messages(convo["id"])
        assert said[-1]["sender"] == "tala-core", "the agent in the room said nothing"
        assert said[-1]["body"] == "reply 1"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_a_room_with_no_agent_runs_nothing(me, replies):
    convo = await conversations.create(
        title="just people", participants=[], created_by=me,
    )
    try:
        await conversations.post(convo["id"], me, "hello")
        await server._agents_take_a_turn(convo["id"], sender=me)
        assert not replies, "inference ran for a room with no agent in it"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_an_agent_does_not_answer_itself(me, replies):
    convo = await conversations.create(
        title="with tala",
        participants=[{"participant": "tala-core", "kind": "agent"}],
        created_by=me,
    )
    try:
        await conversations.post(convo["id"], "tala-core", "thinking out loud")
        await server._agents_take_a_turn(convo["id"], sender="tala-core")
        assert not replies, "an agent was given a turn to answer its own message"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_an_agent_that_left_does_not_answer(me, replies):
    convo = await conversations.create(
        title="with tala",
        participants=[{"participant": "tala-core", "kind": "agent"}],
        created_by=me,
    )
    try:
        await conversations.set_participant_status(
            convo["id"], "tala-core", conversations.STATUS_LEFT
        )
        await conversations.post(convo["id"], me, "still there?")
        await server._agents_take_a_turn(convo["id"], sender=me)
        assert not replies, "an agent that is not in the room took a turn"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


# ---------------------------------------------------------------------------
# A transcript with more than two speakers in it
# ---------------------------------------------------------------------------


async def test_other_speakers_are_named_in_the_transcript():
    """
    Without a name, three people become three unlabelled `user` turns and
    the room reads to the model as one person changing their mind.
    """
    history = server._room_history_for("tala-core", [
        {"sender": "@alice", "body": "shall we ship it"},
        {"sender": "tala-core", "body": "not yet"},
        {"sender": "@bob", "body": "why not"},
    ])

    assert history[0] == {"role": "user", "content": "@alice: shall we ship it"}
    assert history[1] == {"role": "assistant", "content": "not yet"}, (
        "the agent's own words were handed back to it as somebody else's"
    )
    assert history[2]["content"].startswith("@bob:")


async def test_the_agent_is_told_who_it_is_in_the_room(me, replies, announced):
    convo = await conversations.create(
        title="room",
        participants=[{"participant": "tala-core", "kind": "agent"}],
        created_by=me,
    )
    try:
        await conversations.post(convo["id"], me, "hello")
        await server._agents_take_a_turn(convo["id"], sender=me)

        system = replies[0][0]["content"]
        assert "tala-core" in system
        assert "prefixed with who said it" in system, (
            "the model is not told the transcript is labelled"
        )
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_empty_messages_do_not_reach_the_model():
    history = server._room_history_for("tala-core", [
        {"sender": "@alice", "body": "   "},
        {"sender": "@alice", "body": "real"},
    ])
    assert len(history) == 1


# ---------------------------------------------------------------------------
# Two agents in one room
# ---------------------------------------------------------------------------


async def test_agents_answering_each_other_is_bounded(me, replies, announced):
    """
    Steven asked for agents-to-agents, so this is a bound and not a ban. Two
    agents left alone answer each other indefinitely, and every turn is a
    real inference call.
    """
    convo = await conversations.create(
        title="two agents",
        participants=[{"participant": "tala-core", "kind": "agent"},
                      {"participant": "tala-rp", "kind": "agent"}],
        created_by=me,
    )
    try:
        for _ in range(server.MAX_CONSECUTIVE_AGENT_TURNS):
            await conversations.post(convo["id"], "tala-core", "your turn")

        before = len(replies)
        await server._agents_take_a_turn(convo["id"], sender="tala-core")
        assert len(replies) == before, (
            "the agents kept answering each other past the bound"
        )
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_a_person_speaking_starts_the_count_again(me, replies, announced):
    """
    The bound is on agents talking among themselves. A person saying
    something is the room being used, and must not be refused because the
    agents had been busy.
    """
    convo = await conversations.create(
        title="two agents",
        participants=[{"participant": "tala-core", "kind": "agent"},
                      {"participant": "tala-rp", "kind": "agent"}],
        created_by=me,
    )
    try:
        for _ in range(server.MAX_CONSECUTIVE_AGENT_TURNS + 2):
            await conversations.post(convo["id"], "tala-core", "chatter")
        await conversations.post(convo["id"], me, "stop, listen")

        before = len(replies)
        await server._agents_take_a_turn(convo["id"], sender=me)
        assert len(replies) > before, "a person could not get the agents' attention"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


# ---------------------------------------------------------------------------
# Failure is a missing reply, never a broken room
# ---------------------------------------------------------------------------


async def test_a_model_that_says_nothing_costs_nothing(me, monkeypatch, announced):
    async def silent(messages, max_tokens=512, temperature=0.7):
        return ""

    monkeypatch.setattr(server, "_generate_reply", silent)
    monkeypatch.setattr(server, "_fan_out_conversation", _no_fan_out)

    convo = await conversations.create(
        title="room",
        participants=[{"participant": "tala-core", "kind": "agent"}],
        created_by=me,
    )
    try:
        await conversations.post(convo["id"], me, "hello")
        await server._agents_take_a_turn(convo["id"], sender=me)

        said = await conversations.messages(convo["id"])
        assert [m["sender"] for m in said] == [me], "an empty reply was posted"
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])


async def test_a_broken_model_does_not_break_the_room(me, monkeypatch, announced):
    """
    This runs as a background task after the message is already recorded. An
    exception escaping it would be an unhandled task error and a room that
    looks fine until you read the log.
    """
    async def exploding(messages, max_tokens=512, temperature=0.7):
        raise RuntimeError("no provider")

    monkeypatch.setattr(server, "_generate_reply", exploding)
    monkeypatch.setattr(server, "_fan_out_conversation", _no_fan_out)

    convo = await conversations.create(
        title="room",
        participants=[{"participant": "tala-core", "kind": "agent"}],
        created_by=me,
    )
    try:
        await conversations.post(convo["id"], me, "hello")
        await server._agents_take_a_turn(convo["id"], sender=me)  # must not raise
    finally:
        await db.execute("DELETE FROM conversations WHERE id = $1", convo["id"])
