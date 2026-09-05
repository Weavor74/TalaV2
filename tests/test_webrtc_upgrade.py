"""
Regression coverage for the WebRTC relay-upgrade handshake.

Two bugs lived here, and both were silent: the offer RPC succeeded, nothing
logged an error, and every peer call simply kept using the VPS relay the
upgrade exists to escape.

1. The SDP answer returned by `p2p.webrtc_offer` was discarded, so
   `handle_answer` was never called and the local peer stayed in
   `have-local-offer` forever. `handle_p2p_webrtc_answer` was registered in
   the dispatch table with zero callers anywhere in the repo -- a handler
   nothing could reach.

2. The offer carried `peer_username` (the callee) where the receiver expects
   the caller's identity, so the two ends keyed the same channel under
   different names.

The first three tests drive the real aiortc stack over loopback -- no mocks,
an actual ICE connection and an actual data channel -- because the bug was
precisely that a handshake which *looked* fine never completed. The rest
drive `_negotiate_webrtc_upgrade` with a stand-in for the network leg.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from python.harness.webrtc_manager import WebRTCManager

A_NAME = "@alice"
B_NAME = "@bob"

# Loopback ICE settles in well under a second; this is headroom for a loaded
# CI box, not an expected wait.
CONNECT_TIMEOUT = 20.0


async def _wait_for_channel(manager: WebRTCManager, key: str, timeout: float = CONNECT_TIMEOUT):
    """Poll until the named data channel opens, or give up."""
    async def _poll():
        while key not in manager.data_channels:
            await asyncio.sleep(0.05)
        return manager.data_channels[key]

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def _shutdown(*managers: WebRTCManager) -> None:
    for manager in managers:
        for peer in list(manager.connections):
            await manager.close_connection(peer)


@pytest.mark.asyncio
async def test_full_exchange_opens_a_data_channel():
    """
    The fixed sequence, end to end: offer, answer, apply answer.

    Against the pre-fix code the third step never happened, so this fails at
    the `_wait_for_channel` on Alice's side.
    """
    alice, bob = WebRTCManager(), WebRTCManager()
    try:
        offer = await alice.create_offer(B_NAME)
        answer = await bob.handle_offer(A_NAME, offer)
        await alice.handle_answer(B_NAME, answer)

        await _wait_for_channel(alice, B_NAME)
        await _wait_for_channel(bob, A_NAME)

        assert alice.data_channels[B_NAME].readyState == "open"
        assert bob.data_channels[A_NAME].readyState == "open"
    finally:
        await _shutdown(alice, bob)


@pytest.mark.asyncio
async def test_without_the_answer_no_channel_ever_opens():
    """
    Pins the actual defect: dropping the answer is not a slow path, it is a
    dead one. Alice stays in `have-local-offer` indefinitely.
    """
    alice, bob = WebRTCManager(), WebRTCManager()
    try:
        offer = await alice.create_offer(B_NAME)
        await bob.handle_offer(A_NAME, offer)
        # ...and the answer goes nowhere, as it used to.

        with pytest.raises(asyncio.TimeoutError):
            await _wait_for_channel(alice, B_NAME, timeout=3.0)

        assert alice.data_channels == {}
        assert alice.connections[B_NAME].signalingState == "have-local-offer"
    finally:
        await _shutdown(alice, bob)


@pytest.mark.asyncio
async def test_each_end_keys_the_channel_under_the_other_peers_name():
    """
    Bug 2's invariant. Alice files the channel under Bob and Bob files it
    under Alice; if either end is handed the wrong identity, the reverse
    lookup in `send_rpc` misses and routing falls back to the relay.
    """
    alice, bob = WebRTCManager(), WebRTCManager()
    try:
        offer = await alice.create_offer(B_NAME)
        answer = await bob.handle_offer(A_NAME, offer)
        await alice.handle_answer(B_NAME, answer)

        await _wait_for_channel(alice, B_NAME)
        await _wait_for_channel(bob, A_NAME)

        assert set(alice.data_channels) == {B_NAME}
        assert set(bob.data_channels) == {A_NAME}
        assert A_NAME not in alice.data_channels
        assert B_NAME not in bob.data_channels
    finally:
        await _shutdown(alice, bob)


@pytest.mark.asyncio
async def test_rpc_round_trips_over_the_upgraded_channel():
    """The point of the upgrade: real traffic, no relay."""
    alice, bob = WebRTCManager(), WebRTCManager()
    try:
        async def bob_handles(peer: str, message: dict) -> None:
            channel = bob.data_channels[peer]
            channel.send(json.dumps({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"echo": message["params"], "seen_peer_as": peer},
            }))

        bob.set_message_handler(bob_handles)

        offer = await alice.create_offer(B_NAME)
        answer = await bob.handle_offer(A_NAME, offer)
        await alice.handle_answer(B_NAME, answer)
        await _wait_for_channel(alice, B_NAME)
        await _wait_for_channel(bob, A_NAME)

        result = await alice.send_rpc(B_NAME, "p2p.ping", {"n": 1})

        assert result["echo"] == {"n": 1}
        # Bob answered addressing Alice by her name, not his own.
        assert result["seen_peer_as"] == A_NAME
    finally:
        await _shutdown(alice, bob)


# ---------------------------------------------------------------------------
# _negotiate_webrtc_upgrade: the orchestration around the manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_sends_our_identity_not_the_callees(monkeypatch):
    """
    Bug 2 at its source. The field carries who we are; the pre-fix code put
    the callee's name in it.
    """
    from python.harness import server

    monkeypatch.setenv("TALA_USERNAME", A_NAME)
    manager = WebRTCManager()
    monkeypatch.setattr("python.harness.webrtc_manager.webrtc_manager", manager)

    sent: dict = {}

    async def fake_call(endpoint, method, params):
        sent["endpoint"] = endpoint
        sent["method"] = method
        sent["params"] = params
        # A real peer answers with its own manager; borrow one.
        responder = WebRTCManager()
        answer = await responder.handle_offer(A_NAME, params["offer"])
        sent["responder"] = responder
        return {"answer": answer}

    try:
        ok = await server._negotiate_webrtc_upgrade("ws://peer.example:8765", B_NAME, fake_call)

        assert ok is True
        assert sent["method"] == "p2p.webrtc_offer"
        assert sent["params"]["peer_username"] == A_NAME, (
            "the offer must identify the sender, not the peer being called"
        )
        assert sent["params"]["peer_username"] != B_NAME
    finally:
        await _shutdown(manager, sent.get("responder") or WebRTCManager())


@pytest.mark.asyncio
async def test_upgrade_applies_the_returned_answer(monkeypatch):
    """
    Bug 1 at its source: the answer must leave `have-local-offer` behind.
    """
    from python.harness import server

    monkeypatch.setenv("TALA_USERNAME", A_NAME)
    manager = WebRTCManager()
    monkeypatch.setattr("python.harness.webrtc_manager.webrtc_manager", manager)
    responder = WebRTCManager()

    async def fake_call(endpoint, method, params):
        return {"answer": await responder.handle_offer(A_NAME, params["offer"])}

    try:
        assert await server._negotiate_webrtc_upgrade("ws://peer.example:8765", B_NAME, fake_call)

        pc = manager.connections[B_NAME]
        assert pc.signalingState == "stable", "answer was never applied"
        await _wait_for_channel(manager, B_NAME)
    finally:
        await _shutdown(manager, responder)


@pytest.mark.asyncio
async def test_a_failed_upgrade_does_not_block_the_next_attempt(monkeypatch):
    """
    The guard at the top treats any entry in `connections` as "already
    connected". A failed negotiation that leaves its half-open peer
    connection behind therefore disables every future upgrade for the life
    of the process -- so failure has to clean up after itself.
    """
    from python.harness import server

    monkeypatch.setenv("TALA_USERNAME", A_NAME)
    manager = WebRTCManager()
    monkeypatch.setattr("python.harness.webrtc_manager.webrtc_manager", manager)

    async def failing_call(endpoint, method, params):
        raise RuntimeError("peer unreachable")

    try:
        assert await server._negotiate_webrtc_upgrade("ws://peer.example:8765", B_NAME, failing_call) is False
        assert B_NAME not in manager.connections, "half-open connection was left behind"

        # And a later attempt gets through rather than short-circuiting.
        responder = WebRTCManager()

        async def good_call(endpoint, method, params):
            return {"answer": await responder.handle_offer(A_NAME, params["offer"])}

        assert await server._negotiate_webrtc_upgrade("ws://peer.example:8765", B_NAME, good_call) is True
        await _shutdown(responder)
    finally:
        await _shutdown(manager)


@pytest.mark.asyncio
async def test_an_answerless_response_is_treated_as_failure(monkeypatch):
    """
    A peer that returns 200-with-no-answer must not leave a permanently
    half-open connection either. This is the shape the old code silently
    accepted on every single call.
    """
    from python.harness import server

    monkeypatch.setenv("TALA_USERNAME", A_NAME)
    manager = WebRTCManager()
    monkeypatch.setattr("python.harness.webrtc_manager.webrtc_manager", manager)

    async def answerless(endpoint, method, params):
        return {"success": True}

    try:
        assert await server._negotiate_webrtc_upgrade("ws://peer.example:8765", B_NAME, answerless) is False
        assert B_NAME not in manager.connections
    finally:
        await _shutdown(manager)
