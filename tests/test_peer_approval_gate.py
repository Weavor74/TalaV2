"""
"All clients can connect to the VPS without approval, but clients cannot
connect to each other until approved."

Two halves, and both were incomplete.

**The gate.** Node resolution and search were already covered incidentally --
both require permissions.nodes, which is False for anyone unapproved -- but
p2p.receive_message checked only the signature, so a contact that merely
existed could talk to you. Relying on a permission to enforce a rule about
status works right up until somebody grants that permission for an
unrelated reason. The rule is now stated once.

**The request.** A desktop refused unknown senders outright, so a stranger
could never ask to be approved. "Cannot connect until approved" collapsed
into "cannot connect", because approval was unreachable. A client now
records the request as pending, which grants nothing.

A request is not a connection. Enrolling costs a peer a row in a queue and
buys it nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
import pytest_asyncio

from python.harness import db, server, vps_mode

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pool():
    await db.init_pool()
    yield
    await db.close_pool()


def signed_handshake(username, wallet, secret):
    packet = {
        "protocol": "tala-p2p/1.0", "type": "handshake",
        "sender_username": username, "sender_wallet": wallet,
        "sender_endpoint": "", "timestamp": "2026-09-06T00:00:00+00:00",
        "enrollment_secret": secret,
    }
    unsigned = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    packet["signature"] = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    return packet


async def _status(username):
    row = await db.fetchrow("SELECT status FROM contacts WHERE username = $1", username)
    return row["status"] if row else ""


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_only_an_approved_peer_may_interact():
    for status, expected in [
        ("approved", True), ("pending", False), ("enrolled", False), ("denied", False), ("", False),
    ]:
        allowed, _why = server._peer_interaction_allowed({"status": status})
        assert allowed is expected, f"{status!r} interaction was {allowed}"


def test_the_two_refusals_are_told_apart():
    """
    "Not yet" and "no" call for different responses from the peer: one is
    worth waiting on, the other is not.
    """
    _ok, pending = server._peer_interaction_allowed({"status": "pending"})
    _ok2, denied = server._peer_interaction_allowed({"status": "denied"})
    assert pending == "not_yet_approved"
    assert denied == "denied"


# ---------------------------------------------------------------------------
# Messaging, which was the gap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,accepted", [("approved", True), ("pending", False), ("denied", False)])
async def test_a_message_is_accepted_only_from_an_approved_peer(pool, status, accepted):
    marker = uuid.uuid4().hex[:12]
    username, wallet, secret = f"@msg-{marker}", f"wallet-{marker}", f"secret-{marker}"
    try:
        await db.execute(
            """
            INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret,
                                  permissions, status)
            VALUES ($1, $2, '', $3, '{}'::jsonb, $4)
            """,
            username, wallet, secret, status,
        )
        packet = {"protocol": "tala-p2p/1.0", "type": "message",
                  "sender_wallet": wallet, "message": "hello"}
        unsigned = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        packet["signature"] = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()

        result = json.loads(await server.handle_p2p_receive_message({"packet": packet}, 1))["result"]

        assert result["accepted"] is accepted, result
        if not accepted:
            assert result["reason"] in ("not_yet_approved", "denied")
    finally:
        await db.execute("DELETE FROM p2p_messages WHERE peer_username = $1", username)
        await db.execute("DELETE FROM p2p_sessions WHERE peer_username = $1", username)
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


# ---------------------------------------------------------------------------
# The request has to be able to arrive
# ---------------------------------------------------------------------------


async def test_a_client_records_a_stranger_as_pending(pool):
    """
    Refusing outright made approval unreachable: nobody could ask, so
    nothing could be approved.
    """
    marker = uuid.uuid4().hex[:12]
    username = f"@asker-{marker}"
    vps_mode.mode.disable()  # a desktop, not a node
    try:
        contact = await server._enroll_unknown_tala_contact(
            signed_handshake(username, f"wallet-{marker}", f"secret-{marker}")
        )

        assert contact is not None, "a stranger could not ask to be approved"
        assert await _status(username) == "pending"
    finally:
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


async def test_asking_grants_nothing(pool):
    """A request is not a connection."""
    marker = uuid.uuid4().hex[:12]
    username = f"@asker-{marker}"
    vps_mode.mode.disable()
    try:
        contact = await server._enroll_unknown_tala_contact(
            signed_handshake(username, f"wallet-{marker}", f"secret-{marker}")
        )

        granted = {k: v for k, v in (contact["permissions"] or {}).items() if not k.startswith("_")}
        assert not any(granted.values()), f"asking granted {granted}"

        allowed, why = server._peer_interaction_allowed({"status": await _status(username)})
        assert allowed is False and why == "not_yet_approved"
    finally:
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


async def test_a_node_and_a_client_mean_different_things_by_enrolling(pool):
    """
    Same call, two roles: a node routes the peer, a client considers it.
    """
    marker = uuid.uuid4().hex[:12]
    node_user, client_user = f"@n-{marker}", f"@c-{marker}"
    try:
        vps_mode.mode.enable("test")
        await server._enroll_unknown_tala_contact(
            signed_handshake(node_user, f"nw-{marker}", f"ns-{marker}")
        )
        assert await _status(node_user) == "enrolled"

        vps_mode.mode.disable()
        await server._enroll_unknown_tala_contact(
            signed_handshake(client_user, f"cw-{marker}", f"cs-{marker}")
        )
        assert await _status(client_user) == "pending"
    finally:
        vps_mode.mode.disable()
        await db.execute("DELETE FROM contacts WHERE username = ANY($1::text[])",
                         [node_user, client_user])


async def test_an_unsigned_request_is_not_recorded(pool):
    """
    The queue is only worth having if entering it costs a valid signature.
    """
    vps_mode.mode.disable()
    contact = await server._enroll_unknown_tala_contact(
        {"protocol": "tala-p2p/1.0", "type": "handshake",
         "sender_username": "@nosig", "sender_wallet": "w", "timestamp": "t"}
    )
    assert contact is None
