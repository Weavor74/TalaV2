"""
Contact approval: the decision a connection node does not get to make.

A connection node enrols anyone -- it is a directory and a relay, and needs
no opinion about who you are. Whether that peer may see anything is a
separate question, answered here, by the person whose documents they are.

Enrolment already started every peer from deny, so this does not close a
permission hole. What it adds is that a request is *visible* -- a queue
somebody can act on rather than a row that silently appeared -- and that a
refusal is remembered instead of being forgotten the next time the peer
handshakes.

Against the real database. `tests/test_memory.py` mocks db.fetch and
db.execute everywhere, which proved the SQL text looked right and never that
a decision survives being written down; these use the dev Postgres with
unique markers and explicit cleanup, matching test_memory_persistence_recall.
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

from python.harness import db, server

pytestmark = pytest.mark.asyncio


def _ok(raw):
    body = json.loads(raw)
    assert "error" not in body, body.get("error")
    return body["result"]


def _error(raw):
    body = json.loads(raw)
    assert "error" in body, f"expected a refusal, got {body}"
    return body["error"]


@pytest_asyncio.fixture
async def pool():
    await db.init_pool()
    yield
    await db.close_pool()


@pytest_asyncio.fixture
async def peer(pool):
    """A self-enrolled peer, cleaned up whatever the test does to it."""
    marker = uuid.uuid4().hex[:12]
    username = f"@test-peer-{marker}"
    wallet = f"wallet-{marker}"
    try:
        await db.execute(
            """
            INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret,
                                  permissions, status)
            VALUES ($1, $2, '', $3, $4::jsonb, 'pending')
            """,
            username, wallet, f"secret-{marker}",
            {"nodes": False, "identity": False, "_requested": {"nodes": True, "identity": True}},
        )
        yield {"username": username, "wallet_address": wallet, "secret": f"secret-{marker}"}
    finally:
        await db.execute("DELETE FROM firewall_contacts WHERE id = $1", wallet)
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


async def _status(username: str) -> str:
    row = await db.fetchrow("SELECT status FROM contacts WHERE username = $1", username)
    return row["status"] if row else ""


async def _permissions(username: str) -> dict:
    row = await db.fetchrow("SELECT permissions FROM contacts WHERE username = $1", username)
    value = row["permissions"]
    return json.loads(value) if isinstance(value, str) else (value or {})


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


async def test_a_self_enrolled_peer_waits_in_the_queue(peer):
    result = _ok(await server.handle_contacts_pending({}, 1))
    mine = [p for p in result["pending"] if p["username"] == peer["username"]]

    assert len(mine) == 1, "a self-enrolled peer should be waiting for a decision"
    assert mine[0]["wallet_address"] == peer["wallet_address"]


async def test_the_queue_shows_what_was_asked_for(peer):
    """
    The request is surfaced so the decision is made against it rather than
    in the dark. It is a request and never a grant.
    """
    result = _ok(await server.handle_contacts_pending({}, 1))
    mine = next(p for p in result["pending"] if p["username"] == peer["username"])

    assert mine["requested"] == {"nodes": True, "identity": True}
    granted = await _permissions(peer["username"])
    assert granted["nodes"] is False and granted["identity"] is False


async def test_a_pending_peer_can_read_nothing_before_a_decision(peer):
    """
    The status makes the request visible; enrolment already made it
    powerless. Both should be true.
    """
    granted = await _permissions(peer["username"])
    assert not any(v for k, v in granted.items() if not k.startswith("_"))


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------


async def test_approval_grants_only_what_was_granted(peer):
    result = _ok(await server.handle_contacts_approve({
        "username": peer["username"],
        "permissions": {"nodes": True, "identity": False},
    }, 1))

    assert result["status"] == "approved"
    assert await _status(peer["username"]) == "approved"

    granted = await _permissions(peer["username"])
    assert granted["nodes"] is True
    assert granted["identity"] is False, "asking for it is not being given it"


async def test_approval_writes_the_access_rights_the_firewall_reads(peer):
    _ok(await server.handle_contacts_approve({
        "username": peer["username"],
        "permissions": {"nodes": True},
        "access_rights": {"clearance_level": 3, "allowed_categories": ["rp"]},
    }, 1))

    row = await db.fetchrow(
        "SELECT access_rights FROM firewall_contacts WHERE id = $1", peer["wallet_address"]
    )
    assert row is not None, "approval did not reach the firewall table"
    rights = row["access_rights"]
    rights = json.loads(rights) if isinstance(rights, str) else rights
    assert rights["clearance_level"] == 3
    assert rights["allowed_categories"] == ["rp"]


async def test_approval_keeps_the_original_request_on_the_record(peer):
    _ok(await server.handle_contacts_approve({
        "username": peer["username"], "permissions": {"nodes": True},
    }, 1))

    granted = await _permissions(peer["username"])
    assert granted["_requested"] == {"nodes": True, "identity": True}, (
        "what the peer asked for is part of the audit trail of the decision"
    )


async def test_a_peer_cannot_grant_itself_anything_through_the_request(peer):
    """
    `_requested` is the peer's own words. It must never be readable as a
    permission, whatever it contains.
    """
    _ok(await server.handle_contacts_approve({
        "username": peer["username"],
        "permissions": {"_requested": {"nodes": True}, "nodes": False},
    }, 1))

    granted = await _permissions(peer["username"])
    assert granted["nodes"] is False
    assert granted["_requested"] == {"nodes": True, "identity": True}, "the record was overwritten"


# ---------------------------------------------------------------------------
# Denying
# ---------------------------------------------------------------------------


async def test_denial_is_remembered(peer):
    _ok(await server.handle_contacts_deny({"username": peer["username"]}, 1))
    assert await _status(peer["username"]) == "denied"

    result = _ok(await server.handle_contacts_pending({}, 1))
    assert not [p for p in result["pending"] if p["username"] == peer["username"]], (
        "a denied peer is still sitting in the queue"
    )


async def test_denial_closes_the_socket_not_just_the_documents(peer):
    """
    A denied peer that could still authenticate would hold a connection, sit
    in the relay registry and keep asking -- refused every time, but present.
    """
    assert await server._peer_shared_secret(peer["username"]) == peer["secret"]

    _ok(await server.handle_contacts_deny({"username": peer["username"]}, 1))

    assert await server._peer_shared_secret(peer["username"]) == "", (
        "a denied contact can still authenticate to the socket"
    )


async def test_denial_revokes_access_rights_too(peer):
    _ok(await server.handle_contacts_approve({
        "username": peer["username"],
        "permissions": {"nodes": True},
        "access_rights": {"clearance_level": 5},
    }, 1))
    _ok(await server.handle_contacts_deny({"username": peer["username"]}, 1))

    row = await db.fetchrow(
        "SELECT access_rights FROM firewall_contacts WHERE id = $1", peer["wallet_address"]
    )
    assert row is None, "a denied contact kept its firewall clearance"
    assert not any(v for k, v in (await _permissions(peer["username"])).items() if not k.startswith("_"))


async def test_re_enrolling_cannot_walk_a_denial_back_to_pending(peer):
    """
    Otherwise a refused peer reapplies on every handshake until somebody
    clicks the wrong button.
    """
    _ok(await server.handle_contacts_deny({"username": peer["username"]}, 1))

    await db.execute(
        """
        INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret, permissions, status)
        VALUES ($1, $2, '', 'new-secret', '{}'::jsonb, 'pending')
        ON CONFLICT (username) DO UPDATE SET
            shared_secret = EXCLUDED.shared_secret,
            updated_at = NOW()
        """,
        peer["username"], peer["wallet_address"],
    )

    assert await _status(peer["username"]) == "denied"


async def test_approval_does_not_resurrect_on_re_enrolment(peer):
    """The mirror case: a grant already made must survive the peer reconnecting."""
    _ok(await server.handle_contacts_approve({
        "username": peer["username"], "permissions": {"nodes": True},
    }, 1))

    await db.execute(
        """
        INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret, permissions, status)
        VALUES ($1, $2, '', 'rotated', '{}'::jsonb, 'pending')
        ON CONFLICT (username) DO UPDATE SET
            shared_secret = EXCLUDED.shared_secret,
            updated_at = NOW()
        """,
        peer["username"], peer["wallet_address"],
    )

    assert await _status(peer["username"]) == "approved"
    assert (await _permissions(peer["username"]))["nodes"] is True


# ---------------------------------------------------------------------------
# Existing installs
# ---------------------------------------------------------------------------


async def test_a_contact_added_by_hand_is_not_suspended_by_the_migration(pool):
    """
    Rows already on disk exist because the operator added them deliberately.
    A migration that silently moved them to 'pending' would break every
    existing peer on upgrade.
    """
    marker = uuid.uuid4().hex[:12]
    username = f"@legacy-{marker}"
    try:
        await db.execute(
            """
            INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret, permissions)
            VALUES ($1, $2, '', 'legacy-secret', '{"nodes": true}'::jsonb)
            """,
            username, f"wallet-{marker}",
        )
        assert await _status(username) == "approved"
        assert await server._peer_shared_secret(username) == "legacy-secret"
    finally:
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


async def test_deciding_about_a_stranger_is_refused(pool):
    for handler in (server.handle_contacts_approve, server.handle_contacts_deny):
        error = _error(await handler({"username": "@nobody-at-all"}, 1))
        assert "Unknown contact" in error["message"]

    assert _error(await server.handle_contacts_approve({}, 1))["message"].startswith("'username'")


# ---------------------------------------------------------------------------
# Enrolling without an address
# ---------------------------------------------------------------------------


async def test_a_peer_with_no_endpoint_can_still_enrol(pool, monkeypatch):
    """
    A connection node exists for peers that cannot be dialled. Requiring an
    endpoint to enrol excluded exactly them: a desktop behind NAT has no
    address to advertise, so it could never become a contact, never
    authenticate as a peer, and never use the relay -- the one route by
    which it could have been reached.

    Found against the live VPS, where enrolment came back as
    "Internal error: Peer endpoint is required".
    """
    import hashlib
    import hmac

    marker = uuid.uuid4().hex[:12]
    username = f"@no-endpoint-{marker}"
    wallet = f"wallet-{marker}"
    secret = f"enrol-{marker}"

    from python.harness import vps_mode
    vps_mode.mode.enable("test")

    packet = {
        "protocol": "tala-p2p/1.0",
        "type": "handshake",
        "sender_username": username,
        "sender_wallet": wallet,
        "sender_endpoint": "",          # the whole point
        "timestamp": "2026-09-06T00:00:00+00:00",
        "enrollment_secret": secret,
    }
    unsigned = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    packet["signature"] = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()

    try:
        contact = await server._enroll_unknown_tala_contact(packet)

        assert contact is not None, "a peer with no endpoint was refused enrolment"
        assert contact["endpoint_url"] == ""
        assert contact["username"] == username
        assert await _status(username) == "pending"
    finally:
        vps_mode.mode.disable()
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


async def test_an_endpoint_is_still_normalised_when_given(pool, monkeypatch):
    """A peer that does have an address keeps it, in ws:// form."""
    import hashlib
    import hmac

    marker = uuid.uuid4().hex[:12]
    username = f"@with-endpoint-{marker}"
    secret = f"enrol-{marker}"

    from python.harness import vps_mode
    vps_mode.mode.enable("test")

    packet = {
        "protocol": "tala-p2p/1.0",
        "type": "handshake",
        "sender_username": username,
        "sender_wallet": f"wallet-{marker}",
        "sender_endpoint": "https://peer.example:8765",
        "timestamp": "2026-09-06T00:00:00+00:00",
        "enrollment_secret": secret,
    }
    unsigned = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    packet["signature"] = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()

    try:
        contact = await server._enroll_unknown_tala_contact(packet)
        assert contact["endpoint_url"] == "wss://peer.example:8765"
    finally:
        vps_mode.mode.disable()
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


# ---------------------------------------------------------------------------
# What "pending" costs a peer, which on a connection node is nothing
# ---------------------------------------------------------------------------


async def test_pending_does_not_stop_a_peer_connecting(peer):
    """
    Steven's decision, so that everyone can connect: a connection node
    enrols and relays, it does not judge. Approval governs access to
    documents, and a node serves none.

    So `pending` must gate nothing. Only a denial closes the door -- and
    that asymmetry is the whole design, not an oversight to be tidied up
    later by making the states symmetrical.
    """
    assert await _status(peer["username"]) == "pending"
    assert await server._peer_shared_secret(peer["username"]) == peer["secret"], (
        "a pending peer was refused its credentials, so it could not connect"
    )


async def test_only_a_denial_closes_the_door(peer):
    """The one status that gates, stated as a rule rather than a side effect."""
    for status in ("pending", "approved"):
        await db.execute(
            "UPDATE contacts SET status = $2 WHERE username = $1", peer["username"], status
        )
        assert await server._peer_shared_secret(peer["username"]) == peer["secret"], status

    await db.execute(
        "UPDATE contacts SET status = 'denied' WHERE username = $1", peer["username"]
    )
    assert await server._peer_shared_secret(peer["username"]) == ""


@pytest.mark.asyncio
async def test_nothing_else_gates_on_contact_status():
    """
    Guard against a future change quietly making `pending` mean "refused".
    If a second gate is genuinely wanted, this test should be updated
    deliberately rather than discovered by a peer who cannot connect.
    """
    import inspect
    import re

    src = inspect.getsource(server)
    gates = re.findall(r'contact\.get\("status"\)[^\n]*', src)

    assert len(gates) == 1, f"contact status is gated in {len(gates)} places: {gates}"
    assert "denied" in gates[0], "the single status gate no longer checks for a denial"
