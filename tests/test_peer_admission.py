"""
Admission to 8765 for another install.

The socket was gated so that a web page could not open it and a stray local
process could not drive 247 handlers. Correct -- and it also shut out the
one caller federation depends on, because a peer has no way to obtain a
token that lives 0600 in someone else's vault. Peer calls dialled out with
no credentials at all, so every one of them was refused as anonymous.

The fix is not to widen the token. A peer is not the user at this machine,
and nothing about being reachable should confer shell execution or vault
reads. So a handshake now resolves to a class, and the class decides the
method surface:

    LOCAL      the vault token; everything, unchanged
    PEER       a contact proving the shared secret already on its row
    BOOTSTRAP  no credential, connection node only, one method

BOOTSTRAP exists to break a circularity: a stranger cannot authenticate as a
contact until it is one, and it becomes one by enrolling over a connection
it could not otherwise open. What it can achieve is to become a contact with
every permission denied, waiting for a human to approve it.
"""

from __future__ import annotations

import time

import pytest

from python.harness import ws_auth
from python.harness.vps_mode import (
    BOOTSTRAP_CONNECTION_METHODS,
    PEER_CONNECTION_METHODS,
    methods_for_class,
)

SECRET = "shared-secret-between-alice-and-bob"
PEER = "@alice"


def claim_headers(username=PEER, secret=SECRET, timestamp=None, nonce="nonce-1"):
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    return {
        ws_auth.PEER_HEADER: username,
        ws_auth.PEER_TS_HEADER: timestamp,
        ws_auth.PEER_NONCE_HEADER: nonce,
        ws_auth.PEER_SIG_HEADER: ws_auth.peer_signature(username, timestamp, nonce, secret),
    }


async def secret_for_alice(username):
    return SECRET if username == PEER else ""


@pytest.fixture(autouse=True)
def fresh_nonces():
    ws_auth._seen_nonces.clear()
    yield
    ws_auth._seen_nonces.clear()


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_local_token_is_unchanged_and_unrestricted():
    cls, who, refusal = await ws_auth.classify(
        "/", {"x-tala-token": "tok"}, "tok", secret_for=secret_for_alice
    )
    assert (cls, who, refusal) == (ws_auth.CLASS_LOCAL, "", None)
    assert methods_for_class(cls) is None, "a local client keeps the whole surface"


@pytest.mark.asyncio
async def test_a_signed_contact_is_admitted_as_a_peer():
    cls, who, refusal = await ws_auth.classify(
        "/", claim_headers(), "tok", secret_for=secret_for_alice
    )
    assert refusal is None
    assert (cls, who) == (ws_auth.CLASS_PEER, PEER)


@pytest.mark.asyncio
async def test_a_peer_gets_peer_surface_and_nothing_else():
    allowed = methods_for_class(ws_auth.CLASS_PEER)
    assert allowed == PEER_CONNECTION_METHODS
    for reachable in ("p2p.receive_message", "relay.send", "relay.deliver"):
        assert reachable in allowed
    for withheld in ("vault.read", "task.submit", "self_improvement.execute_goal",
                     "inference.generate", "contacts.delete", "system.capabilities"):
        assert withheld not in allowed


@pytest.mark.asyncio
async def test_an_unlabelled_connection_gets_nothing():
    """
    A connection that never passed the gate has no class. Failing closed is
    the difference between a missing label meaning 'trusted' and meaning
    'trusted with nothing'.
    """
    assert methods_for_class(None) == frozenset()
    assert methods_for_class("something-else") == frozenset()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_forged_signature_is_refused():
    headers = claim_headers()
    headers[ws_auth.PEER_SIG_HEADER] = "0" * 64
    cls, _who, refusal = await ws_auth.classify("/", headers, "tok", secret_for=secret_for_alice)
    assert cls is None
    assert refusal[0] == 401


@pytest.mark.asyncio
async def test_a_claim_signed_with_the_wrong_secret_is_refused():
    cls, _who, refusal = await ws_auth.classify(
        "/", claim_headers(secret="not-the-shared-secret"), "tok", secret_for=secret_for_alice
    )
    assert cls is None
    assert refusal[0] == 401


@pytest.mark.asyncio
async def test_an_unknown_contact_is_refused_indistinguishably():
    """
    Whether a username is a known contact must not be enumerable one guess
    at a time, so this is the same refusal a bad signature gets.
    """
    _cls, _who, unknown = await ws_auth.classify(
        "/", claim_headers(username="@mallory"), "tok", secret_for=secret_for_alice
    )
    headers = claim_headers()
    headers[ws_auth.PEER_SIG_HEADER] = "0" * 64
    _cls2, _who2, bad_sig = await ws_auth.classify(
        "/", headers, "tok", secret_for=secret_for_alice
    )
    assert unknown == bad_sig


@pytest.mark.asyncio
async def test_a_stale_handshake_is_refused():
    old = int(time.time()) - (ws_auth.PEER_CLOCK_SKEW_SECONDS + 60)
    cls, _who, refusal = await ws_auth.classify(
        "/", claim_headers(timestamp=old), "tok", secret_for=secret_for_alice
    )
    assert cls is None and refusal[0] == 401


@pytest.mark.asyncio
async def test_a_captured_handshake_cannot_be_replayed():
    """
    The signature stays valid for as long as its timestamp is fresh, so
    without a nonce table one captured handshake is reusable for the whole
    skew window.
    """
    headers = claim_headers(nonce="nonce-replay")
    first, _who, refusal = await ws_auth.classify("/", headers, "tok", secret_for=secret_for_alice)
    assert (first, refusal) == (ws_auth.CLASS_PEER, None)

    replayed_cls, _who2, replay_refusal = await ws_auth.classify(
        "/", headers, "tok", secret_for=secret_for_alice
    )
    assert replayed_cls is None
    assert replay_refusal[0] == 401


@pytest.mark.asyncio
async def test_a_bad_signature_does_not_burn_the_nonce():
    """
    The nonce is recorded only after the signature verifies, or an
    unauthenticated caller could fill the table -- or pre-burn the nonce a
    legitimate peer is about to use.
    """
    headers = claim_headers(nonce="nonce-contested", secret="wrong-secret")
    await ws_auth.classify("/", headers, "tok", secret_for=secret_for_alice)
    assert "nonce-contested" not in ws_auth._seen_nonces

    good = claim_headers(nonce="nonce-contested")
    cls, _who, refusal = await ws_auth.classify("/", good, "tok", secret_for=secret_for_alice)
    assert (cls, refusal) == (ws_auth.CLASS_PEER, None)


@pytest.mark.asyncio
async def test_a_browser_origin_is_refused_before_anything_else():
    """
    The origin gate stops the drive-by, and a peer claim must not be a way
    around it -- though page script cannot set these headers anyway.
    """
    headers = claim_headers()
    headers["origin"] = "https://evil.example"
    cls, _who, refusal = await ws_auth.classify("/", headers, "tok", secret_for=secret_for_alice)
    assert cls is None
    assert refusal[0] == 403


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stranger_is_refused_on_a_desktop():
    """The default, and the behaviour that existed before this change."""
    cls, _who, refusal = await ws_auth.classify(
        "/", {}, "tok", secret_for=secret_for_alice, bootstrap_allowed=False
    )
    assert cls is None
    assert refusal[0] == 401


@pytest.mark.asyncio
async def test_a_stranger_reaches_exactly_one_method_on_a_connection_node():
    cls, who, refusal = await ws_auth.classify(
        "/", {}, "tok", secret_for=secret_for_alice, bootstrap_allowed=True
    )
    assert (cls, who, refusal) == (ws_auth.CLASS_BOOTSTRAP, "", None)

    allowed = methods_for_class(cls)
    assert allowed == BOOTSTRAP_CONNECTION_METHODS
    assert allowed == frozenset({"p2p.receive_handshake"})
    for withheld in ("relay.send", "p2p.semantic_search", "vault.read", "p2p.resolve_node_reference"):
        assert withheld not in allowed


@pytest.mark.asyncio
async def test_bootstrap_does_not_weaken_the_token_for_local_callers():
    """Opening the door for enrolment must not open it for everything else."""
    cls, _who, refusal = await ws_auth.classify(
        "/", {"x-tala-token": "wrong"}, "tok",
        secret_for=secret_for_alice, bootstrap_allowed=True,
    )
    # A wrong token is not a local client; it falls through to bootstrap,
    # which is one method and not the surface the token would have given.
    assert cls == ws_auth.CLASS_BOOTSTRAP
    assert methods_for_class(cls) == frozenset({"p2p.receive_handshake"})


# ---------------------------------------------------------------------------
# The two ends agree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_what_a_client_sends_is_what_a_server_accepts():
    """
    The credentials the dialling side builds must satisfy the gate the
    receiving side runs. These were written in different modules; nothing
    but a test makes them one protocol.
    """
    from python.harness import server

    headers = server._peer_handshake_headers(PEER, SECRET)
    cls, who, refusal = await ws_auth.classify("/", headers, "tok", secret_for=secret_for_alice)

    assert refusal is None
    assert (cls, who) == (ws_auth.CLASS_PEER, PEER)


@pytest.mark.asyncio
async def test_each_dial_is_signed_afresh():
    from python.harness import server

    first = server._peer_handshake_headers(PEER, SECRET)
    second = server._peer_handshake_headers(PEER, SECRET)

    assert first[ws_auth.PEER_NONCE_HEADER] != second[ws_auth.PEER_NONCE_HEADER]
    assert first[ws_auth.PEER_SIG_HEADER] != second[ws_auth.PEER_SIG_HEADER]


@pytest.mark.asyncio
async def test_the_server_asks_the_database_for_the_secret(monkeypatch):
    from python.harness import server

    async def contact(**kwargs):
        assert kwargs.get("username") == PEER
        return {"username": PEER, "shared_secret": SECRET}

    monkeypatch.setattr(server, "_get_contact", contact)
    assert await server._peer_shared_secret(PEER) == SECRET


@pytest.mark.asyncio
async def test_an_unknown_contact_yields_no_secret(monkeypatch):
    from python.harness import server

    async def no_contact(**kwargs):
        return None

    monkeypatch.setattr(server, "_get_contact", no_contact)
    assert await server._peer_shared_secret("@mallory") == ""


# ---------------------------------------------------------------------------
# Against a real socket
# ---------------------------------------------------------------------------
#
# ws_auth's own docstring records why this is not optional: the gate once
# refused 100% of real connections while every unit test calling check()
# directly stayed green, because the callback around it destructured the
# wrong argument shape. classify() being right proves nothing about the
# handshake actually working, and the class has to survive the trip from
# process_request to the connection handler for any of it to mean anything.


async def _serve(bootstrap_allowed=False):
    """A socket that reports back the class it stamped on each connection."""
    import websockets

    async def report(ws, path=""):
        stamped = getattr(ws, ws_auth.CONNECTION_CLASS_ATTR, "<unlabelled>")
        who = getattr(ws, ws_auth.CONNECTION_PEER_ATTR, "")
        async for _message in ws:
            await ws.send(f"{stamped}:{who}")

    return await websockets.serve(
        report, "127.0.0.1", 0,
        process_request=ws_auth.make_process_request(
            "local-token", secret_for=secret_for_alice, bootstrap_allowed=bootstrap_allowed,
        ),
    )


async def _dial(base, **kw):
    """Returns the class the server stamped, or None if refused."""
    import asyncio

    import websockets

    try:
        async with websockets.connect(base, open_timeout=5, **kw) as ws:
            await ws.send("who am i")
            return await asyncio.wait_for(ws.recv(), 5)
    except Exception:
        return None


@pytest.mark.asyncio
async def test_a_real_peer_handshake_is_admitted_and_labelled():
    server = await _serve()
    port = next(iter(server.sockets)).getsockname()[1]
    base = f"ws://127.0.0.1:{port}"
    try:
        assert await _dial(base, additional_headers=claim_headers(nonce="real-1")) == f"peer:{PEER}"

        # The local token still works and is still unrestricted.
        assert await _dial(f"{base}?token=local-token") == "local:"

        # And a stranger is still refused when this is not a connection node.
        assert await _dial(base) is None

        headers = claim_headers(nonce="real-forged")
        headers[ws_auth.PEER_SIG_HEADER] = "0" * 64
        assert await _dial(base, additional_headers=headers) is None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_real_bootstrap_handshake_is_admitted_only_on_a_node():
    server = await _serve(bootstrap_allowed=True)
    port = next(iter(server.sockets)).getsockname()[1]
    base = f"ws://127.0.0.1:{port}"
    try:
        assert await _dial(base) == "bootstrap:"
        assert await _dial(base, additional_headers=claim_headers(nonce="real-2")) == f"peer:{PEER}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_replay_fails_against_a_real_socket_too():
    server = await _serve()
    port = next(iter(server.sockets)).getsockname()[1]
    base = f"ws://127.0.0.1:{port}"
    headers = claim_headers(nonce="real-replay")
    try:
        assert await _dial(base, additional_headers=headers) == f"peer:{PEER}"
        assert await _dial(base, additional_headers=headers) is None, "a captured handshake was reusable"
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# One key, two purposes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_handshake_signature_is_domain_separated_from_a_packet_signature():
    """
    The contact's shared secret signs P2P packets as well as handshakes. Two
    different things signed with one key is how a signature obtained for one
    purpose is replayed as the other -- this codebase has met that before, in
    a proof-of-possession endpoint that signed a caller's nonce with the
    block-signing key and handed out valid block signatures.

    The encodings could not collide anyway, because canonical JSON starts
    with "{" and a username cannot. That is an accident of format; the
    prefix is the decision.
    """
    from python.harness import server

    handshake = ws_auth.peer_signature(PEER, "1700000000", "n1", SECRET)

    # The same fields, signed the other way, must not produce the handshake.
    packet_shaped = server._packet_signature(
        {"sender_username": PEER, "timestamp": "1700000000", "nonce": "n1"}, SECRET
    )
    assert handshake != packet_shaped

    # And a packet legitimately signed by this peer is not a handshake for it.
    signed_packet = server._sign_packet(
        {"protocol": "tala-p2p/1.0", "type": "handshake", "sender_username": PEER}, SECRET
    )
    claim = {
        "username": PEER, "timestamp": str(int(time.time())),
        "nonce": "from-a-packet", "signature": signed_packet["signature"],
    }
    ok, reason = ws_auth._verify_peer_claim(claim, SECRET)
    assert ok is False
    assert "signature" in reason


@pytest.mark.asyncio
async def test_the_domain_is_part_of_what_is_signed():
    """Changing the domain must change every signature it produces."""
    import hashlib
    import hmac

    undomained = hmac.new(
        SECRET.encode(), "\n".join((PEER, "1700000000", "n1")).encode(), hashlib.sha256
    ).hexdigest()

    assert ws_auth.peer_signature(PEER, "1700000000", "n1", SECRET) != undomained
    assert ws_auth.PEER_SIGNATURE_DOMAIN in "\n".join(
        (ws_auth.PEER_SIGNATURE_DOMAIN, PEER, "1700000000", "n1")
    )


# ---------------------------------------------------------------------------
# A claim that does not stand up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_identity_can_still_enrol_again_on_a_node():
    """
    Refusing a failed claim outright was strictly worse than sending no
    headers at all, which is incoherent -- it made presenting credentials
    more punishing than presenting none.

    Observed live: the desktop still held a contact row for the node and so
    signed its handshake, the node's matching row had been removed, and the
    client got HTTP 401 with no way back. It could not enrol, because
    enrolling is what it needed the connection for.
    """
    cls, who, refusal = await ws_auth.classify(
        "/", claim_headers(username="@forgotten", nonce="stale-1"), "tok",
        secret_for=secret_for_alice, bootstrap_allowed=True,
    )

    assert refusal is None
    assert cls == ws_auth.CLASS_BOOTSTRAP
    assert who == "", "a rejected claim must not carry an identity forward"
    assert methods_for_class(cls) == frozenset({"p2p.receive_handshake"})


@pytest.mark.asyncio
async def test_a_forged_signature_also_lands_in_bootstrap_on_a_node():
    """
    Same treatment for a wrong signature. It concedes nothing: the one
    method bootstrap reaches was reachable by omitting the headers.
    """
    headers = claim_headers(nonce="stale-2")
    headers[ws_auth.PEER_SIG_HEADER] = "0" * 64

    cls, who, refusal = await ws_auth.classify(
        "/", headers, "tok", secret_for=secret_for_alice, bootstrap_allowed=True,
    )

    assert (cls, who, refusal) == (ws_auth.CLASS_BOOTSTRAP, "", None)


@pytest.mark.asyncio
async def test_a_desktop_still_refuses_a_failed_claim_outright():
    """
    The fallback exists because a connection node enrols strangers. A
    desktop does not, so there is nothing to fall back to.
    """
    cls, _who, refusal = await ws_auth.classify(
        "/", claim_headers(username="@forgotten", nonce="stale-3"), "tok",
        secret_for=secret_for_alice, bootstrap_allowed=False,
    )

    assert cls is None
    assert refusal[0] == 401


@pytest.mark.asyncio
async def test_a_good_claim_is_still_a_peer_not_a_bootstrap():
    """The fallback must not swallow the success case."""
    cls, who, refusal = await ws_auth.classify(
        "/", claim_headers(nonce="good-after-fallback"), "tok",
        secret_for=secret_for_alice, bootstrap_allowed=True,
    )

    assert (cls, who, refusal) == (ws_auth.CLASS_PEER, PEER, None)
