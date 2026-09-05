"""
Connection-node mode: the `--vps` gate, peer identity, and the relay.

The gap this covers is the one the peer directory could not close on its
own. `/chain/peers` already answers "does @alice exist and is she online",
but the endpoint_url it returns is undialable for a client behind NAT, and
the server tracked connections in an anonymous set -- so there was no way to
ask "which socket is @alice" and therefore no way to reach her at all.

Two things are load-bearing here and both are tested directly:

  * the role is OFF unless --vps was passed, because a desktop Tala that
    enrolled strangers would be handing out contact rows and a relay; and
  * only P2P methods are relayable, because the CALLER names the method --
    an unrestricted relay is a confused deputy with a peer directory
    attached.
"""

from __future__ import annotations

import asyncio

import pytest

from python.harness import vps_mode
from python.harness.vps_mode import (
    RELAYABLE_METHODS,
    PeerConnectionRegistry,
)


class FakeSocket:
    """Stands in for a WebSocketServerProtocol: it only has to accept sends."""

    def __init__(self, name: str = "sock") -> None:
        self.name = name
        self.sent: list[str] = []
        self.fail_with: Exception | None = None

    async def send(self, payload: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(payload)


@pytest.fixture
def clean_mode():
    """VPS mode is process-global; never leak it between tests."""
    vps_mode.mode.disable()
    yield vps_mode.mode
    vps_mode.mode.disable()


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_mode_is_off_by_default(clean_mode):
    assert vps_mode.is_enabled() is False


def test_flag_enables_and_records_its_source(clean_mode):
    clean_mode.enable("--vps")
    assert vps_mode.is_enabled() is True
    assert clean_mode.enabled_by == "--vps"


@pytest.mark.asyncio
async def test_env_var_alone_still_works_but_warns(clean_mode, monkeypatch, caplog):
    """
    The deployed VPS must survive the upgrade, so the old env var is still
    honoured -- loudly, because an ambient setting that opens enrolment to
    strangers should never be quiet.
    """
    from python.harness import server

    monkeypatch.setenv("TALA_VPS_ACCEPT_ANY_TALA", "1")
    with caplog.at_level("WARNING"):
        assert server._accept_any_tala_client_enabled() is True
    assert any("--vps" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_flag_needs_no_env_var_and_does_not_warn(clean_mode, monkeypatch, caplog):
    from python.harness import server

    monkeypatch.delenv("TALA_VPS_ACCEPT_ANY_TALA", raising=False)
    clean_mode.enable("--vps")
    with caplog.at_level("WARNING"):
        assert server._accept_any_tala_client_enabled() is True
    assert not [r for r in caplog.records if "--vps" in r.message]


@pytest.mark.asyncio
async def test_neither_flag_nor_env_means_no_enrolment(clean_mode, monkeypatch):
    """The default a desktop Tala must have."""
    from python.harness import server

    monkeypatch.delenv("TALA_VPS_ACCEPT_ANY_TALA", raising=False)
    assert server._accept_any_tala_client_enabled() is False


# ---------------------------------------------------------------------------
# Identity: which socket is whom
# ---------------------------------------------------------------------------


def test_bind_and_look_up_by_name():
    registry = PeerConnectionRegistry()
    alice = FakeSocket("alice")
    registry.bind("@alice", alice)

    assert registry.lookup("@alice") is alice
    assert registry.username_for(alice) == "@alice"
    assert [p["username"] for p in registry.connected_peers()] == ["@alice"]


def test_disconnect_makes_a_peer_unreachable():
    """Presence is the socket. There is no lease to expire."""
    registry = PeerConnectionRegistry()
    alice = FakeSocket("alice")
    registry.bind("@alice", alice)

    assert registry.release(alice) == "@alice"
    assert registry.lookup("@alice") is None
    assert registry.connected_peers() == []
    assert registry.release(alice) is None  # idempotent


def test_reconnect_replaces_the_stale_socket():
    """
    A peer that dropped and came back is the common case, and its old socket
    is already dead -- so the new one wins rather than being refused.
    """
    registry = PeerConnectionRegistry()
    old, new = FakeSocket("old"), FakeSocket("new")
    registry.bind("@alice", old)
    registry.bind("@alice", new)

    assert registry.lookup("@alice") is new
    assert registry.username_for(old) is None
    assert len(registry.connected_peers()) == 1


def test_a_late_close_of_the_old_socket_does_not_evict_the_new_one():
    """
    The stale connection's `finally` block runs after the reconnect. If
    release were keyed by name alone it would unregister the peer that is
    currently online.
    """
    registry = PeerConnectionRegistry()
    old, new = FakeSocket("old"), FakeSocket("new")
    registry.bind("@alice", old)
    registry.bind("@alice", new)

    registry.release(old)

    assert registry.lookup("@alice") is new, "the live socket was evicted by a dead one"


# ---------------------------------------------------------------------------
# The relay
# ---------------------------------------------------------------------------


async def _answer(registry: PeerConnectionRegistry, socket: FakeSocket, result: dict) -> None:
    """Play the far peer: read what the relay wrote, reply to that id."""
    import json

    for _ in range(200):
        if socket.sent:
            break
        await asyncio.sleep(0.01)
    envelope = json.loads(socket.sent[-1])
    registry.resolve({"jsonrpc": "2.0", "id": envelope["id"], "result": result})


@pytest.mark.asyncio
async def test_relay_forwards_and_returns_the_reply():
    import json

    registry = PeerConnectionRegistry()
    bob = FakeSocket("bob")
    registry.bind("@bob", bob)

    task = asyncio.create_task(
        registry.relay("@bob", "p2p.receive_message", {"body": "hi"}, "@alice")
    )
    await _answer(registry, bob, {"delivered": True})
    result = await task

    assert result == {"delivered": True}
    envelope = json.loads(bob.sent[-1])
    assert envelope["method"] == "relay.deliver"
    assert envelope["params"]["from_username"] == "@alice"
    assert envelope["params"]["method"] == "p2p.receive_message"
    assert envelope["params"]["params"] == {"body": "hi"}


@pytest.mark.asyncio
async def test_a_reply_is_recognised_as_a_reply_not_a_new_request():
    """
    The relay makes the server a client for one round trip, so the answer
    lands in the inbound stream where requests are expected. Misreading it
    as a request is how the WebRTC path stalled; this pins the distinction.
    """
    registry = PeerConnectionRegistry()
    bob = FakeSocket("bob")
    registry.bind("@bob", bob)

    task = asyncio.create_task(registry.relay("@bob", "p2p.semantic_search", {}, "@alice"))
    import json

    for _ in range(200):
        if bob.sent:
            break
        await asyncio.sleep(0.01)
    relay_id = json.loads(bob.sent[-1])["id"]

    assert registry.is_relay_response({"id": relay_id, "result": {}}) is True
    # A genuine inbound request that happens to carry an id is not a reply.
    assert registry.is_relay_response({"id": relay_id, "method": "p2p.ping"}) is False
    # Nor is a reply to something we never sent.
    assert registry.is_relay_response({"id": "relay-not-ours", "result": {}}) is False

    registry.resolve({"id": relay_id, "result": {"ok": True}})
    assert await task == {"ok": True}


@pytest.mark.asyncio
async def test_relaying_a_non_allowlisted_method_is_refused():
    """
    The caller chooses the method, so without this the relay would run
    arbitrary RPC inside another peer's harness over the node's trusted
    connection to them.
    """
    registry = PeerConnectionRegistry()
    bob = FakeSocket("bob")
    registry.bind("@bob", bob)

    for forbidden in ("vault.read", "inference.generate", "contacts.delete", "task.submit"):
        with pytest.raises(PermissionError):
            await registry.relay("@bob", forbidden, {}, "@alice")

    assert bob.sent == [], "nothing may reach the peer before the check"


def test_the_allowlist_is_only_peer_surface():
    assert all(m.startswith("p2p.") for m in RELAYABLE_METHODS)


@pytest.mark.asyncio
async def test_relaying_to_someone_not_connected_fails_clearly():
    registry = PeerConnectionRegistry()
    with pytest.raises(LookupError, match="not connected"):
        await registry.relay("@nobody", "p2p.receive_message", {}, "@alice")


@pytest.mark.asyncio
async def test_a_dead_socket_reports_as_a_connection_error():
    registry = PeerConnectionRegistry()
    bob = FakeSocket("bob")
    bob.fail_with = RuntimeError("socket closed")
    registry.bind("@bob", bob)

    with pytest.raises(ConnectionError):
        await registry.relay("@bob", "p2p.receive_message", {}, "@alice")


@pytest.mark.asyncio
async def test_a_silent_peer_times_out_without_leaking_the_pending_entry():
    registry = PeerConnectionRegistry()
    bob = FakeSocket("bob")
    registry.bind("@bob", bob)

    with pytest.raises(TimeoutError):
        await registry.relay("@bob", "p2p.receive_message", {}, "@alice", timeout=0.15)

    assert registry._pending == {}, "a timed-out relay must not accumulate futures"


@pytest.mark.asyncio
async def test_an_error_from_the_far_peer_propagates():
    registry = PeerConnectionRegistry()
    bob = FakeSocket("bob")
    registry.bind("@bob", bob)

    task = asyncio.create_task(registry.relay("@bob", "p2p.receive_message", {}, "@alice"))
    import json

    for _ in range(200):
        if bob.sent:
            break
        await asyncio.sleep(0.01)
    registry.resolve({
        "id": json.loads(bob.sent[-1])["id"],
        "error": {"code": -32000, "message": "peer refused"},
    })

    with pytest.raises(RuntimeError, match="peer refused"):
        await task


# ---------------------------------------------------------------------------
# The RPC surface refuses to act when the flag is absent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["relay.announce", "relay.send", "relay.peers"])
@pytest.mark.asyncio
async def test_relay_rpcs_are_inert_without_the_flag(clean_mode, method):
    import json

    from python.harness import server

    handler = server._HANDLERS[method]
    socket = FakeSocket()
    if method == "relay.peers":
        raw = await handler({}, 1)
    else:
        raw = await handler({}, socket, 1)

    assert "not running as a connection node" in json.loads(raw)["error"]["message"]


@pytest.mark.asyncio
async def test_relay_send_requires_an_announced_socket(clean_mode):
    import json

    from python.harness import server

    clean_mode.enable("--vps")
    raw = await server.handle_relay_send({"to": "@bob", "method": "p2p.receive_message"}, FakeSocket(), 1)
    assert "Announce this connection first" in json.loads(raw)["error"]["message"]


@pytest.mark.asyncio
async def test_relay_deliver_enforces_the_allowlist_on_the_receiving_side(clean_mode):
    """
    The forwarding node is trusted to route, not to choose what runs, so the
    client checks again rather than taking the method on faith.
    """
    import json

    from python.harness import server

    raw = await server.handle_relay_deliver(
        {"from_username": "@alice", "method": "vault.read", "params": {}}, 1
    )
    assert "not relayable" in json.loads(raw)["error"]["message"]


# ---------------------------------------------------------------------------
# Authority lives on the receiving client, not on the relay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_relay_envelope_is_not_an_identity_claim(clean_mode, monkeypatch):
    """
    The forwarding node names the sender in its envelope, but it does not
    hold the shared secret between two contacts -- so the receiving client
    must establish who is asking from the signed packet, and must not fall
    back to the envelope when the packet is absent or forged.
    """
    import json

    from python.harness import server

    # An envelope that claims @alice, with no signed packet behind it.
    raw = await server.handle_p2p_semantic_search({"query": "secrets"}, 1)
    body = json.loads(raw)

    assert "error" in body, "an unsigned request must not be answered on the envelope's word"
    assert "packet" in body["error"]["message"]


@pytest.mark.asyncio
async def test_semantic_search_refuses_an_unknown_or_unsigned_sender(monkeypatch):
    """The two ways a caller fails to prove who they are."""
    import json

    from python.harness import server

    async def no_such_contact(**kwargs):
        return None

    monkeypatch.setattr(server, "_get_contact", no_such_contact)
    raw = await server.handle_p2p_semantic_search(
        {"packet": {"sender_wallet": "w-mallory", "query": "secrets"}}, 1
    )
    assert json.loads(raw)["result"]["reason"] == "unknown_sender"

    async def known_contact(**kwargs):
        return {"username": "@alice", "wallet_address": "w-alice",
                "shared_secret": "s3cret", "permissions": {"nodes": True}}

    monkeypatch.setattr(server, "_get_contact", known_contact)
    raw = await server.handle_p2p_semantic_search(
        {"packet": {"sender_wallet": "w-alice", "query": "secrets", "signature": "forged"}}, 1
    )
    assert json.loads(raw)["result"]["reason"] == "bad_signature"


@pytest.mark.asyncio
async def test_semantic_search_withholds_nodes_the_clients_firewall_denies(monkeypatch):
    """
    The client decides what this specific contact may see -- per node, via
    the same firewall that gates direct node resolution. Previously this
    handler made one blanket `trust_tier != internal` decision for every
    contact alike.
    """
    import json
    from types import SimpleNamespace

    from python.harness import server

    contact = {"username": "@alice", "wallet_address": "w-alice",
               "shared_secret": "s3cret", "permissions": {"nodes": True}}

    async def known_contact(**kwargs):
        return contact

    monkeypatch.setattr(server, "_get_contact", known_contact)
    monkeypatch.setattr(server, "_verify_packet", lambda packet, secret: True)
    monkeypatch.setattr(server, "resolve_active_nodes_dir", lambda: __import__("pathlib").Path("."))
    monkeypatch.setattr(
        "python.harness.retrieval.retrieve_relevant_nodes",
        lambda *a, **k: [
            (SimpleNamespace(node_id="public-doc", node_type="doc"), 0.9),
            (SimpleNamespace(node_id="private-doc", node_type="doc"), 0.8),
        ],
    )

    async def firewall(contact_row, node_names):
        return ("private-doc" not in node_names), "firewall_denied"

    monkeypatch.setattr(server, "_peer_can_resolve_node_reference", firewall)

    result = json.loads(await server.handle_p2p_semantic_search(
        {"packet": {"sender_wallet": "w-alice", "query": "anything"}}, 1
    ))["result"]

    assert [m["node_id"] for m in result["matches"]] == ["public-doc"]
    assert result["withheld"] == 1


@pytest.mark.asyncio
async def test_a_contact_with_nodes_permission_off_sees_nothing(monkeypatch):
    import json

    from python.harness import server

    async def known_contact(**kwargs):
        return {"username": "@alice", "wallet_address": "w-alice",
                "shared_secret": "s3cret", "permissions": {"nodes": False}}

    monkeypatch.setattr(server, "_get_contact", known_contact)
    monkeypatch.setattr(server, "_verify_packet", lambda packet, secret: True)

    result = json.loads(await server.handle_p2p_semantic_search(
        {"packet": {"sender_wallet": "w-alice", "query": "anything"}}, 1
    ))["result"]

    assert result["matches"] == []
    assert result["reason"] == "contact_nodes_permission_disabled"


def test_a_peer_connection_cannot_reach_the_admin_surface():
    """
    A WebRTC data channel used to dispatch anything in _HANDLERS, and the
    offer that establishes one carries no signature. Peers get peer surface.
    """
    from python.harness import server
    from python.harness.vps_mode import RELAYABLE_METHODS

    for dangerous in ("vault.read", "contacts.delete", "task.submit",
                      "self_improvement.execute_goal", "inference.get_providers"):
        assert dangerous not in RELAYABLE_METHODS
        assert dangerous in server._HANDLERS, "still a real method, just not peer-reachable"


# ---------------------------------------------------------------------------
# What the chain records
# ---------------------------------------------------------------------------


def test_a_document_request_is_recorded_as_the_document_it_asked_for():
    """`@alice requested node:doc66666 from @bob`, not `@alice called @bob`."""
    from python.harness.server import _relayed_document_ref

    ref = _relayed_document_ref(
        "p2p.resolve_node_reference",
        {"packet": {"reference": "@bob/doc66666", "sender_wallet": "w-alice"}},
    )
    assert ref == "node:@bob/doc66666"


def test_each_relayed_method_names_its_own_subject():
    from python.harness.server import _relayed_document_ref

    assert _relayed_document_ref("p2p.receive_handshake", {}) == "handshake"
    assert _relayed_document_ref("p2p.webrtc_offer", {"packet": {}}) == "signalling:sdp"
    assert _relayed_document_ref(
        "p2p.receive_message", {"packet": {"type": "a2a"}}
    ) == "message:a2a"


def test_the_reference_never_carries_the_content():
    """
    A relay carries other people's messages; the ledger is public. The
    search subject is recorded by shape, and an unknown method contributes
    no subject rather than a guess at one.
    """
    from python.harness.server import _relayed_document_ref

    ref = _relayed_document_ref(
        "p2p.semantic_search",
        {"packet": {"query": "my private medical question"}},
    )
    assert ref == "query:27chars"
    assert "medical" not in ref

    assert _relayed_document_ref("p2p.something_new", {"packet": {"secret": "x"}}) == ""
