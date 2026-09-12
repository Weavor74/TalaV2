"""
vps_mode.py — the connection-node role
======================================

An ordinary Tala is a client: it dials out, and nothing dials it. A Tala
started with ``--vps`` additionally becomes the place other Talas can be
reached *through*.

This is the piece the peer directory could not supply on its own. The chain
directory (``/chain/peers``) already answers "does @alice exist and is she
online" -- but the ``endpoint_url`` it returns is undialable for any client
behind NAT, which is most of them. Nobody can open a socket *to* a laptop.
The one socket that does exist is the one the laptop opened outbound to the
VPS, and until now the server threw that identity away: connections were
tracked in an anonymous ``set``, so there was no way to answer "which socket
is @alice" and therefore no way to forward her anything.

``PeerConnectionRegistry`` is that missing map. With it the VPS can route by
name over connections the peers themselves established, in whichever
direction traffic needs to go.

Two deliberate constraints:

**The mode is off unless the flag says otherwise.** A desktop Tala that
accidentally accepted peer enrolments would be handing strangers a contact
row and a relay endpoint. `--vps` is the only switch; see
``is_enabled()``.

**Only P2P methods are relayable.** The caller names the method, so without
an allowlist any enrolled peer could ask the VPS to invoke `vault.read` on
any other peer -- the relay would be a confused deputy with a directory
attached. ``RELAYABLE_METHODS`` is the whole permitted surface.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

log = logging.getLogger(__name__)

# How long the VPS waits for a relayed call to come back before giving up on
# the target. Slightly under the 15s clients already use for direct peer
# calls, so the caller's own timeout is not the one that fires first.
RELAY_TIMEOUT_SECONDS = 12.0

# The method a relay arrives as on the receiving peer.
RELAY_DELIVER_METHOD = "relay.deliver"

# What one peer may ask the VPS to invoke on another.
#
# The relay forwards a method name chosen by the caller, which makes an
# unrestricted relay a way to run arbitrary RPC inside someone else's
# harness using the VPS's trusted connection to them. Everything here is
# already peer-to-peer surface with its own permission checks on the
# receiving side; nothing that touches local state, secrets or the vault is
# reachable this way.
RELAYABLE_METHODS: frozenset[str] = frozenset({
    "p2p.receive_handshake",
    "p2p.receive_message",
    "p2p.receive_conversation",
    "p2p.resolve_node_reference",
    "p2p.semantic_search",
    "p2p.encrypted_payload",
    "p2p.webrtc_offer",
    "p2p.webrtc_answer",
})


# What a connection admitted as a peer may call.
#
# The relay verbs on top of the peer surface: a peer needs to name itself and
# ask for a hop, and `relay.deliver` is how a hop arrives. Everything else on
# the roughly 180-method surface -- shell execution, source patching, vault
# reads -- stays with the local token, because being another install is not
# the same as being the user at this machine.
PEER_CONNECTION_METHODS: frozenset[str] = RELAYABLE_METHODS | frozenset({
    "relay.announce",
    "relay.send",
    "relay.peers",
    "relay.deliver",
    "p2p.handshake",
})

# What a peer that authenticated but was never APPROVED may call.
#
# "Enrolled grants nothing" has to be enforced where methods are dispatched,
# not per-handler: a connection node auto-enrols any stranger, and that
# stranger then holds a valid shared secret and authenticates as a peer. If
# the peer class alone granted the peer surface, enrolment would be the whole
# gate -- and there is no gate in enrolment. So an unapproved peer gets the
# verbs it needs to exist and to be routed THROUGH, never verbs that execute
# against this harness's own state: the handshakes (asking is how approval
# becomes possible) and the relay verbs (which on a connection node forward
# to peers who enforce their own approval, and on a desktop refuse outright
# because relaying is VPS-only).
PEER_UNAPPROVED_METHODS: frozenset[str] = frozenset({
    "p2p.handshake",
    "p2p.receive_handshake",
    "relay.announce",
    "relay.send",
    "relay.peers",
    "relay.deliver",
})

# What a caller with no credentials at all may call, and only on a connection
# node. Exactly the enrolment handshake: enough to become a contact, which
# grants nothing until a human approves it, and not one method more.
BOOTSTRAP_CONNECTION_METHODS: frozenset[str] = frozenset({
    "p2p.receive_handshake",
})


def methods_for_class(connection_class: str | None) -> frozenset[str] | None:
    """
    The method allowlist for a connection class, or None for no restriction.

    An unrecognised class -- including a connection that never passed the
    handshake gate at all -- gets the empty set rather than the full surface.
    Failing closed here is the difference between a missing label meaning
    "trusted" and meaning "trusted with nothing".
    """
    if connection_class == "local":
        return None
    if connection_class == "peer":
        return PEER_CONNECTION_METHODS
    if connection_class == "bootstrap":
        return BOOTSTRAP_CONNECTION_METHODS
    return frozenset()


class _VPSMode:
    """Whether this process is running as a connection node."""

    def __init__(self) -> None:
        self._enabled = False
        self._enabled_by = ""

    def enable(self, source: str = "--vps") -> None:
        self._enabled = True
        self._enabled_by = source
        log.info("VPS connection-node mode enabled by %s", source)

    def disable(self) -> None:
        self._enabled = False
        self._enabled_by = ""

    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def enabled_by(self) -> str:
        return self._enabled_by


mode = _VPSMode()


def is_enabled() -> bool:
    """True when this process was started as a connection node."""
    return mode.is_enabled()


class PeerConnectionRegistry:
    """
    Which live socket belongs to which peer, and the in-flight relays over it.

    A peer is present exactly as long as its socket is: there is no lease,
    no expiry and no persistence, because the thing being tracked is a TCP
    connection and the connection closing IS the peer going away. Anything
    longer-lived belongs in the chain directory, which already models it.
    """

    def __init__(self) -> None:
        # Every live socket a peer has here, newest last -- not one each.
        #
        # One socket per name was wrong in a way that only showed up under
        # real traffic. A peer holds a socket open so it can be routed to,
        # but it also opens SHORT-LIVED sockets for its own outbound calls,
        # and those authenticate as the same peer. Each one replaced the
        # held socket, and closing it then unbound the name -- so making a
        # call was a way to disappear from the directory while still
        # connected, and the next relay came back "not connected to this
        # node".
        self._by_username: dict[str, list[Any]] = {}
        self._by_socket: dict[int, str] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._bound_at: dict[str, float] = {}
        #: Sockets whose owner said it is serving requests on them.
        self._routable: set[int] = set()

    # -- identity ----------------------------------------------------------

    def bind(self, username: str, socket: Any, routable: bool = False) -> None:
        """
        Record that `socket` belongs to `username`.

        `routable` says the peer is *serving requests* on this socket, not
        merely that it opened it -- and only a peer can know that, which is
        why announcing is a thing a peer does rather than something inferred
        from the connection.

        The distinction is load-bearing. A peer's own outbound calls
        authenticate as that peer, and those sockets are reply channels: the
        peer sends a request and waits for the answer. Routing a relayed
        call down one puts a REQUEST into a channel where a REPLY is
        expected -- the caller skips it as not its own, nobody ever serves
        it, and both sides wait until something times out. That is exactly
        what happened: relayed deliveries were answered in milliseconds and
        then hung on the write.

        Additive: a peer may hold several at once, and only one of them is
        usually the held connection.
        """
        sockets = self._by_username.setdefault(username, [])
        if socket not in sockets:
            sockets.append(socket)
        self._by_socket[id(socket)] = username
        self._bound_at.setdefault(username, time.time())
        if routable:
            self._routable.add(id(socket))

    def release(self, socket: Any) -> str | None:
        """
        Forget one socket. Returns the username it held, if any.

        The peer is only gone once its LAST socket is: releasing one of
        several must not evict the name, or an outbound call ending would
        take the peer offline.
        """
        username = self._by_socket.pop(id(socket), None)
        if username is None:
            return None
        self._routable.discard(id(socket))
        sockets = self._by_username.get(username, [])
        self._by_username[username] = [s for s in sockets if s is not socket]
        if not self._by_username[username]:
            self._by_username.pop(username, None)
            self._bound_at.pop(username, None)
        return username

    def is_connected(self, username: str) -> bool:
        """Whether this peer can be reached -- not merely whether it is here."""
        return self.lookup(username) is not None

    def lookup(self, username: str) -> Any | None:
        """
        A socket to route over: the newest one the peer is serving on.

        Only routable sockets, never a bare connection. Newest among those
        because the most recently established is the one least likely to be
        half-dead.
        """
        sockets = self._by_username.get(username) or []
        for socket in reversed(sockets):
            if id(socket) in self._routable:
                return socket
        return None

    def username_for(self, socket: Any) -> str | None:
        return self._by_socket.get(id(socket))

    def connected_peers(self) -> list[dict[str, Any]]:
        now = time.time()
        return sorted(
            (
                {
                    "username": name,
                    "connected_seconds": round(now - self._bound_at.get(name, now), 1),
                }
                for name in self._by_username
                # Present means reachable. A peer that is connected but
                # serving nothing cannot be routed to, and listing it as
                # online is how a caller ends up relaying into a socket
                # nobody answers.
                if self.lookup(name) is not None
            ),
            key=lambda p: p["username"],
        )

    # -- relay -------------------------------------------------------------

    def is_relay_response(self, message: dict[str, Any]) -> bool:
        """
        Is this frame a reply to something the VPS relayed, rather than a
        fresh request?

        The relay makes the server briefly a client: it sends a request down
        a socket it normally only reads requests from, so the reply arrives
        in the inbound stream where a request is expected. Same shape of
        problem the WebRTC data channel solves in `_setup_channel`, and the
        same test -- an id we are waiting on, and no `method`.
        """
        message_id = message.get("id")
        return bool(message_id) and "method" not in message and message_id in self._pending

    def resolve(self, message: dict[str, Any]) -> bool:
        """Hand a relayed reply back to whoever is awaiting it."""
        future = self._pending.pop(str(message.get("id")), None)
        if future is None or future.done():
            return False
        future.set_result(message)
        return True

    async def relay(
        self,
        target_username: str,
        method: str,
        params: dict[str, Any],
        from_username: str,
        timeout: float = RELAY_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """
        Forward one call to a connected peer and return its reply.

        Raises rather than returning an error dict, so the RPC handler above
        can map each failure to the JSON-RPC error it deserves.
        """
        import json

        if method not in RELAYABLE_METHODS:
            raise PermissionError(
                f"'{method}' is not relayable. Permitted: {', '.join(sorted(RELAYABLE_METHODS))}"
            )

        socket = self.lookup(target_username)
        if socket is None:
            raise LookupError(f"{target_username} is not connected to this node")

        relay_id = f"relay-{uuid.uuid4().hex[:16]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[relay_id] = future

        envelope = json.dumps({
            "jsonrpc": "2.0",
            "id": relay_id,
            "method": RELAY_DELIVER_METHOD,
            "params": {
                "from_username": from_username,
                "method": method,
                "params": params,
            },
        }, ensure_ascii=False)

        try:
            await socket.send(envelope)
        except Exception as exc:
            self._pending.pop(relay_id, None)
            raise ConnectionError(f"could not write to {target_username}: {exc}") from exc

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(relay_id, None)
            raise TimeoutError(f"{target_username} did not answer within {timeout:g}s")
        finally:
            self._pending.pop(relay_id, None)

        if response.get("error"):
            error = response["error"]
            raise RuntimeError(error.get("message") if isinstance(error, dict) else str(error))

        result = response.get("result")
        return result if isinstance(result, dict) else {"result": result}


registry = PeerConnectionRegistry()
