"""
A request that comes and finds you.

Steven's framing: a person makes the request and the VPS notifies the person
one has been made.

Recording it was not enough. A request visible only in a queue is a request
nobody sees -- you would have to already suspect it to go looking -- and the
peer waits on a decision that is not being made, because the person who
could make it does not know there is anything to decide. The approval flow
was reachable and silent, which in practice is most of the way to
unreachable.

The node carries the request; this harness raises it to its own UI. A relay
that could push notifications into somebody's app would be a relay with
opinions about their screen.
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


@pytest.fixture
def announced(monkeypatch):
    sent = []

    async def fake_broadcast(method, params=None):
        sent.append((method, params or {}))
        return 1

    monkeypatch.setattr(server, "broadcast_notification", fake_broadcast)
    return sent


def signed_handshake(username, wallet, secret, requested=None):
    packet = {
        "protocol": "tala-p2p/1.0", "type": "handshake",
        "sender_username": username, "sender_wallet": wallet,
        "sender_endpoint": "", "timestamp": "2026-09-06T00:00:00+00:00",
        "enrollment_secret": secret,
    }
    if requested:
        packet["requested_permissions"] = requested
    unsigned = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    packet["signature"] = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    return packet


async def test_a_request_to_a_client_is_announced(pool, announced):
    marker = uuid.uuid4().hex[:12]
    username = f"@asks-{marker}"
    vps_mode.mode.disable()
    try:
        await server._enroll_unknown_tala_contact(
            signed_handshake(username, f"w-{marker}", f"s-{marker}",
                             requested={"nodes": True, "identity": True, "skills": False})
        )

        requests = [p for m, p in announced if m == "contacts.request_received"]
        assert requests, "the request was recorded and nobody was told"
        assert requests[0]["username"] == username
        assert sorted(requests[0]["requested"]) == ["identity", "nodes"]
        assert "skills" not in requests[0]["requested"], (
            "a permission they did not ask for was reported as requested"
        )
    finally:
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


async def test_a_connection_node_announces_nothing(pool, announced):
    """
    A node enrols everyone by design. Announcing each one would be noise
    about a decision nobody there makes.
    """
    marker = uuid.uuid4().hex[:12]
    username = f"@routed-{marker}"
    vps_mode.mode.enable("test")
    try:
        await server._enroll_unknown_tala_contact(
            signed_handshake(username, f"w-{marker}", f"s-{marker}")
        )

        assert not [p for m, p in announced if m == "contacts.request_received"], (
            "a connection node announced a request it will never decide"
        )
    finally:
        vps_mode.mode.disable()
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


async def test_a_failed_announcement_does_not_lose_the_request(pool, monkeypatch):
    """
    The row is the durable half. A notification nobody received must not cost
    the peer its place in the queue.
    """
    async def exploding(method, params=None):
        raise RuntimeError("no clients connected")

    monkeypatch.setattr(server, "broadcast_notification", exploding)

    marker = uuid.uuid4().hex[:12]
    username = f"@quiet-{marker}"
    vps_mode.mode.disable()
    try:
        contact = await server._enroll_unknown_tala_contact(
            signed_handshake(username, f"w-{marker}", f"s-{marker}")
        )

        assert contact is not None
        row = await db.fetchrow("SELECT status FROM contacts WHERE username = $1", username)
        assert row["status"] == "pending"
    finally:
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


def test_the_ui_surfaces_it_and_says_nothing_is_granted():
    """
    A notification that reads like an announcement invites approving on
    reflex. It should say who asked, what for, and that asking is not
    getting.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "renderer" / "renderer.js").read_text(
        encoding="utf-8", errors="replace"
    )

    assert "contacts.request_received" in src, "nothing in the UI listens for a request"

    handler = src.split("contacts.request_received")[1][:900]
    assert "loadPendingRequests" in handler, "the queue is not refreshed when one arrives"
    assert "until you approve" in handler, (
        "the notice does not say that asking grants nothing"
    )
