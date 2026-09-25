"""
ws_auth.py — who is allowed to talk to the JSON-RPC socket on 8765.

WHY THIS EXISTS
---------------
``websockets.serve(_handle_connection, ...)`` accepted every connection and
dispatched every one of 247 handlers -- among them shell execution, source
patching and vault reads -- with no authentication of any kind. The socket
binds loopback, which reads like a control until you remember two things:

  1. **WebSocket handshakes are not subject to CORS.** A browser will happily
     open ``ws://127.0.0.1:8765`` from any page on the internet and hand the
     response back to that page's JavaScript. Same-origin policy does not
     apply. So "loopback only" did not mean "software running on this machine";
     it meant "software running on this machine, plus any website the user
     visits". That is drive-by remote code execution.
  2. Every other local process already had it too.

So there are two gates, and they answer different questions:

  * **Origin** answers "is a browser doing this on some page's behalf?" A
    browser always sends ``Origin`` on a WebSocket handshake and page script
    cannot forge it. Non-browser clients send none. An ``Origin`` we do not
    recognise is refused outright -- this is the gate that stops the drive-by.
  * **Token** answers "does this client know a secret only a local user could
    read?" It is a file in the vault, mode 0600. A web page cannot read it; a
    local process running as this user could, but such a process can already
    read the vault directly, so the token concedes nothing it did not have.

Neither gate is sufficient alone. Origin alone falls to any non-browser client.
Token alone falls the moment a token reaches page script -- and the web UI at
8767 hands its token to page script by design.

FAIL-CLOSED
-----------
The token is generated on first use rather than required in configuration.
"Fail-closed" here means a connection without a valid token is refused, not
that the app refuses to start: a gate that bricks first-run gets switched off,
and a gate that is switched off is not a gate.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

_TOKEN_FILE = "ws_token.txt"

#: Header spellings a client may use. The query parameter exists because a
#: browser cannot set headers on a WebSocket handshake -- ``new WebSocket()``
#: takes a URL and a subprotocol list and nothing else.
_TOKEN_HEADERS = ("authorization", "x-tala-token")
_TOKEN_QUERY = "token"

# ---------------------------------------------------------------------------
# Short-lived single-use WS tickets (8767 exchange → 8765 burn)
# ---------------------------------------------------------------------------
#: ticket_id -> expires_monotonic. Minted by the web UI after HTTP auth;
#: accepted once by the WS handshake, then deleted.
_TICKETS: dict[str, float] = {}
TICKET_TTL_SECONDS = 60.0
_TICKET_PREFIX = "tala-ws-ticket:"


def mint_ws_ticket(ttl_seconds: float | None = None) -> dict[str, Any]:
    """
    Issue a short-lived single-use ticket that 8765 will accept in place of
    the long-lived WS token. Called only after 8767 has authenticated the
    HTTP UI token (header preferred).
    """
    import time as _time

    ttl = float(TICKET_TTL_SECONDS if ttl_seconds is None else ttl_seconds)
    now = _time.monotonic()
    # Prune expired entries so a busy UI cannot grow this forever.
    expired = [k for k, exp in _TICKETS.items() if exp <= now]
    for k in expired:
        _TICKETS.pop(k, None)

    ticket = _TICKET_PREFIX + secrets.token_urlsafe(24)
    _TICKETS[ticket] = now + max(5.0, ttl)
    return {"ticket": ticket, "expires_in": int(max(5.0, ttl))}


def _consume_ws_ticket(supplied: str) -> bool:
    """
    True if *supplied* is a live ticket; burns it (single-use). False otherwise.
    """
    import time as _time

    if not supplied or not supplied.startswith(_TICKET_PREFIX):
        return False
    expires = _TICKETS.pop(supplied, None)
    if expires is None:
        return False
    return _time.monotonic() <= expires



def _token_path() -> Path:
    from sdk.agent.documentation_nodes import resolve_user_data_dir
    return resolve_user_data_dir() / "vault" / ".keys" / _TOKEN_FILE


def load_or_create_token() -> str:
    """
    The shared secret for 8765.

    Only ``TALA_WS_TOKEN`` may pin this value. The HTTP UI token
    (``TALA_WEB_TOKEN``) is a different credential — collapsing them meant a
    browser history ``?token=`` for 8767 also unlocked every RPC on 8765
    (UI-audit C3).

    Returns "" only if the token can be neither read nor written, which is
    reported by the caller rather than silently disabling the gate.
    """
    pinned = (os.environ.get("TALA_WS_TOKEN") or "").strip()
    if pinned:
        return pinned

    path = _token_path()
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        path.write_text(token, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best effort; Windows ACLs differ
        logger.info("ws_auth: created a new WebSocket token at %s", path)
        return token
    except OSError as exc:
        logger.error("ws_auth: could not load or create the token: %s", exc)
        return ""


def _allowed_origins() -> set[str]:
    """
    Origins a browser may present.

    Defaults to the web UI's own origin, because that is the only browser page
    that is supposed to be here. ``TALA_WS_ALLOWED_ORIGINS`` takes a
    comma-separated list for anyone serving the renderer from elsewhere.

    ``null`` is deliberately absent: a sandboxed iframe on a hostile page sends
    ``Origin: null``, so allowing it would reopen the hole this closes.
    """
    web_port = (os.environ.get("TALA_WEB_PORT") or "8767").strip()
    origins = {
        f"http://127.0.0.1:{web_port}",
        f"http://localhost:{web_port}",
    }
    extra = (os.environ.get("TALA_WS_ALLOWED_ORIGINS") or "").strip()
    if extra:
        origins |= {o.strip().rstrip("/") for o in extra.split(",") if o.strip()}
    return origins


def _supplied_token(path: str, headers: Any) -> str:
    for name in _TOKEN_HEADERS:
        raw = headers.get(name) if headers is not None else None
        if raw:
            raw = str(raw).strip()
            if raw.lower().startswith("bearer "):
                raw = raw[7:].strip()
            if raw:
                return raw
    query = parse_qs(urlsplit(path or "").query)
    values = query.get(_TOKEN_QUERY) or []
    return values[0].strip() if values else ""


def _origin_refusal(headers: Any) -> tuple[int, str] | None:
    """
    The drive-by gate: a page on the internet opening a socket to a service
    on the user's own machine. One implementation, used by every path in,
    because a rule written twice is a rule that will be fixed once.
    """
    origin = (headers.get("origin") if headers is not None else None) or ""
    origin = str(origin).strip().rstrip("/")
    if origin and origin not in _allowed_origins():
        return 403, f"origin {origin!r} is not allowed"
    return None


def _token_refusal(path: str, headers: Any, token: str) -> tuple[int, str] | None:
    """Why this handshake is not a local client. None means it is one."""
    if not token:
        # No token could be established. Refusing is the safe direction: the
        # alternative is serving 247 handlers to anyone who asks.
        return 500, "no WebSocket token is configured"

    supplied = _supplied_token(path, headers)
    if not supplied:
        return 401, "a token is required"
    # Short-lived single-use tickets from the 8767 exchange (UI-audit C3).
    # Checked before the long-lived compare so a ticket can never be mistaken
    # for a truncated long-lived secret.
    if _consume_ws_ticket(supplied):
        return None
    if not secrets.compare_digest(supplied, token):
        return 401, "invalid token"
    return None


def check(path: str, headers: Any, token: str) -> tuple[int, str] | None:
    """
    Decide one handshake on the token alone. None to accept, or (status, reason).

    The synchronous, token-only view of the gate, kept because peer admission
    needs a database and this does not -- a rule that can be checked with a
    plain dict is a rule that stays tested. :func:`classify` is what the
    server installs; both share the two helpers above rather than each
    carrying their own copy.
    """
    return _origin_refusal(headers) or _token_refusal(path, headers, token)


#: Where the handshake leaves its verdict for the connection handler to read.
#: websockets hands `process_request` the same connection object it later
#: passes to the handler, so an attribute on it survives the trip -- and a
#: connection that never went through the gate has no attribute, which reads
#: as the most restricted class rather than the least.
CONNECTION_CLASS_ATTR = "tala_connection_class"
CONNECTION_PEER_ATTR = "tala_peer_username"


#: Sliding-window handshake counter per remote IP. Loopback is exempt: a
#: local process that wants to hammer the socket can already read the token
#: off disk, and the renderer's reconnect storms must never lock it out.
_HANDSHAKE_HITS: dict[str, list[float]] = {}
HANDSHAKE_RATE_LIMIT = 60
HANDSHAKE_RATE_WINDOW = 60.0


def _handshake_rate_limited(remote_ip: str) -> bool:
    import ipaddress
    import time as _time

    try:
        if ipaddress.ip_address(remote_ip).is_loopback:
            return False
    except ValueError:
        pass
    now = _time.monotonic()
    hits = _HANDSHAKE_HITS.setdefault(remote_ip, [])
    while hits and now - hits[0] > HANDSHAKE_RATE_WINDOW:
        hits.pop(0)
    if len(hits) >= HANDSHAKE_RATE_LIMIT:
        return True
    hits.append(now)
    return False


def make_process_request(token: str, secret_for: Any = None, bootstrap_allowed: bool = False):
    """
    Build the ``process_request`` callback for ``websockets.serve``.

    Two signatures are supported on purpose. websockets 14 moved ``serve`` from
    the legacy protocol to the asyncio one and changed this hook from
    ``(path, headers) -> (status, headers, body) | None`` to
    ``(connection, request) -> Response | None``. Written for the legacy form
    alone, the gate refused *every* connection on websockets 16 -- including
    the app's own -- because the arguments it destructured were not the ones it
    got. That failure was caught by connecting a real socket; the unit tests,
    which call :func:`check` directly, were perfectly green throughout.

    So the form is detected from the arguments rather than assumed from the
    version.
    """
    from http import HTTPStatus

    async def process_request(first: Any, second: Any = None):
        legacy = isinstance(first, str)
        if legacy:
            path, headers = first, second
        else:
            request = second
            path = getattr(request, "path", "") or ""
            headers = getattr(request, "headers", None)
            # Per-IP handshake throttle. Every rejected handshake below is
            # free to retry forever otherwise -- token guessing, peer-claim
            # replay probing, enrolment hammering all start with a handshake.
            remote = getattr(first, "remote_address", None)
            remote_ip = str(remote[0]) if isinstance(remote, (tuple, list)) and remote else ""
            if remote_ip and _handshake_rate_limited(remote_ip):
                logger.warning("ws_auth: rate-limited handshakes from %s", remote_ip)
                return first.respond(HTTPStatus(429), "too many requests\n")

        try:
            connection_class, peer_username, verdict = await classify(
                path, headers, token,
                secret_for=secret_for,
                bootstrap_allowed=bootstrap_allowed,
            )
        except Exception:
            logger.exception("ws_auth: handshake check failed; refusing")
            connection_class, peer_username, verdict = None, "", (500, "auth error")

        if verdict is None:
            # Legacy websockets hands us a path string here, not a connection,
            # so there is nothing to stamp. Peer and bootstrap classes are
            # refused on that path rather than admitted unlabelled: an
            # unlabelled connection would read as LOCAL downstream.
            if legacy:
                if connection_class != CLASS_LOCAL:
                    logger.warning(
                        "ws_auth: refusing a %s connection on the legacy handshake path, "
                        "which cannot carry a connection class",
                        connection_class,
                    )
                    return (HTTPStatus(401), [("Content-Type", "text/plain")], b"forbidden\n")
                return None
            try:
                setattr(first, CONNECTION_CLASS_ATTR, connection_class)
                setattr(first, CONNECTION_PEER_ATTR, peer_username)
            except Exception:
                logger.exception("ws_auth: could not label the connection; refusing")
                return first.respond(HTTPStatus(500), "forbidden\n")
            if connection_class != CLASS_LOCAL:
                logger.info(
                    "ws_auth: admitted a %s connection%s",
                    connection_class,
                    f" for {peer_username}" if peer_username else "",
                )
            return None

        status, reason = verdict
        logger.warning("ws_auth: refused a connection (%s): %s", status, reason)
        # The body is deliberately terse: a rejected client learns that it was
        # rejected, not which of the two gates it failed.
        if legacy:
            return (HTTPStatus(status), [("Content-Type", "text/plain")], b"forbidden\n")
        return first.respond(HTTPStatus(status), "forbidden\n")

    return process_request


# ---------------------------------------------------------------------------
# Peer admission
# ---------------------------------------------------------------------------
#
# The token above answers "is this a local process the user controls", and a
# holder of it gets the whole surface. That is the right answer for the
# renderer and the CLI and exactly the wrong one for another install: a peer
# is not a local user, and nothing about being reachable should confer shell
# execution or vault reads.
#
# So admission here is not binary. A handshake resolves to one of three
# classes, and the class decides which methods the connection may call:
#
#   LOCAL     the vault token. Everything, as before.
#   PEER      a contact proving possession of the shared secret already on
#             its contact row. Peer surface only.
#   BOOTSTRAP no credential at all, admitted ONLY on a connection node, and
#             able to call exactly one method: the enrolment handshake.
#
# BOOTSTRAP exists because of a circularity: a stranger cannot authenticate
# as a contact until it *is* a contact, and it becomes one by enrolling over
# a connection it could not otherwise open. Restricting that class to the
# single method that creates the contact row is what keeps opening the door
# from meaning opening the house -- and enrolment itself grants no
# permissions, so what a bootstrap connection can achieve is to become a
# contact with everything denied, awaiting approval.

CLASS_LOCAL = "local"
CLASS_PEER = "peer"
CLASS_BOOTSTRAP = "bootstrap"

PEER_HEADER = "x-tala-peer"
PEER_TS_HEADER = "x-tala-peer-ts"
PEER_NONCE_HEADER = "x-tala-peer-nonce"
PEER_SIG_HEADER = "x-tala-peer-sig"

#: How far a peer's clock may be from ours. Two minutes tolerates ordinary
#: drift between machines while keeping a captured handshake useless for
#: longer than it takes to notice one.
PEER_CLOCK_SKEW_SECONDS = 120

#: Nonces seen inside the skew window. A signature is otherwise replayable
#: for as long as its timestamp stays fresh -- capture one handshake, open
#: connections as that peer for two minutes.
_seen_nonces: dict[str, float] = {}


#: Domain separator for handshake signatures.
#:
#: The contact's shared secret already signs P2P packets, via
#: `_packet_signature` over canonical JSON. Two different things signed with
#: one key is how a signature obtained for one purpose gets replayed as the
#: other -- and this codebase has met that already, in a proof-of-possession
#: endpoint that signed a caller's nonce with the block-signing key and so
#: handed out valid block signatures.
#:
#: The two encodings cannot collide today, because canonical JSON always
#: begins with "{" and a username cannot. That is an accident of format, not
#: a decision, and it would stop being true the moment either encoding
#: changed. The prefix makes it a decision.
PEER_SIGNATURE_DOMAIN = "tala-ws-handshake/1"


def peer_signature(username: str, timestamp: str, nonce: str, secret: str) -> str:
    """HMAC binding the claimed identity to this one handshake."""
    import hashlib
    import hmac

    canonical = "\n".join((PEER_SIGNATURE_DOMAIN, str(username), str(timestamp), str(nonce)))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _peer_claim(headers: Any) -> dict[str, str] | None:
    """The identity a handshake claims, unverified. None if it claims none."""
    if headers is None:
        return None

    def _get(name: str) -> str:
        return str(headers.get(name) or "").strip()

    username = _get(PEER_HEADER)
    if not username:
        return None
    return {
        "username": username,
        "timestamp": _get(PEER_TS_HEADER),
        "nonce": _get(PEER_NONCE_HEADER),
        "signature": _get(PEER_SIG_HEADER),
    }


def _prune_nonces(now: float) -> None:
    stale = [n for n, seen in _seen_nonces.items() if now - seen > PEER_CLOCK_SKEW_SECONDS * 2]
    for nonce in stale:
        _seen_nonces.pop(nonce, None)


def _verify_peer_claim(claim: dict[str, str], secret: str, now: float | None = None) -> tuple[bool, str]:
    """
    Check one peer claim against the shared secret on its contact row.

    Returns (ok, reason). The reason is for the log, never for the client:
    telling a caller which field failed helps nobody but an attacker.
    """
    import time as _time

    now = _time.time() if now is None else now

    username = claim.get("username") or ""
    timestamp = claim.get("timestamp") or ""
    nonce = claim.get("nonce") or ""
    signature = claim.get("signature") or ""

    if not (timestamp and nonce and signature):
        return False, "incomplete claim"
    if not secret:
        return False, "contact has no shared secret"

    try:
        age = now - float(timestamp)
    except (TypeError, ValueError):
        return False, "unparseable timestamp"
    if abs(age) > PEER_CLOCK_SKEW_SECONDS:
        return False, f"timestamp {age:.0f}s outside the {PEER_CLOCK_SKEW_SECONDS}s window"

    expected = peer_signature(username, timestamp, nonce, secret)
    if not secrets.compare_digest(signature, expected):
        return False, "signature does not verify"

    # Only after the signature checks out, or an unauthenticated caller could
    # fill the nonce table by replaying garbage.
    _prune_nonces(now)
    if nonce in _seen_nonces:
        return False, "nonce already used"
    _seen_nonces[nonce] = now

    return True, ""


async def classify(
    path: str,
    headers: Any,
    token: str,
    secret_for: Any = None,
    bootstrap_allowed: bool = False,
) -> tuple[str | None, str, tuple[int, str] | None]:
    """
    Decide one handshake fully: which class, whose identity, or why not.

    Returns ``(connection_class, username, refusal)`` -- exactly one of the
    first and last being meaningful.

    ``secret_for`` is an async callable taking a username and returning that
    contact's shared secret, or "" if there is no such contact. It is passed
    in rather than imported so this module keeps knowing nothing about the
    database, which is what lets the whole gate be tested with a plain dict.

    Order matters. Origin is checked first and for everyone: it is the gate
    that stops a web page opening this socket, and a peer claim in a header
    is not something page script can produce anyway. Then the local token,
    because that is the common case. A peer claim is considered only when
    there is no valid token, so a local client never accidentally downgrades
    itself to peer surface.
    """
    refused_origin = _origin_refusal(headers)
    if refused_origin:
        return None, "", refused_origin

    if _token_refusal(path, headers, token) is None:
        return CLASS_LOCAL, "", None

    claim = _peer_claim(headers)
    if claim is not None:
        if secret_for is None:
            return None, "", (401, "peer credentials are not accepted here")
        username = claim["username"]
        try:
            secret = await secret_for(username)
        except Exception:
            logger.exception("ws_auth: could not look up the contact for a peer claim")
            return None, "", (500, "auth error")
        # An unknown contact and a bad signature are refused identically:
        # whether a username is known must not be enumerable one guess at a
        # time. They are logged distinctly, because only the log is private.
        rejected = ""
        if not secret:
            rejected = "unknown contact"
        else:
            ok, reason = _verify_peer_claim(claim, secret)
            if not ok:
                rejected = reason

        if rejected:
            logger.warning("ws_auth: peer claim for %r rejected: %s", username, rejected)
            if bootstrap_allowed:
                # Fall back to what an anonymous caller would have got anyway.
                #
                # Refusing here was strictly worse than sending no headers at
                # all, which is incoherent: it made a failed claim more
                # punishing than no claim, so a client whose contact row had
                # been removed at the node -- or whose identity had changed --
                # could never enrol again. It presented credentials for a name
                # the node had never heard of, got 401, and had no way back.
                # Observed doing exactly that against the live node.
                #
                # This concedes nothing: bootstrap reaches one method, the
                # enrolment handshake, and anyone could have reached it by
                # simply omitting the headers.
                logger.info("ws_auth: admitting %r as bootstrap so it can enrol again", username)
                return CLASS_BOOTSTRAP, "", None
            return None, "", (401, "peer credentials rejected")

        return CLASS_PEER, username, None

    if bootstrap_allowed:
        # A connection node, and a caller with nothing to prove yet. It gets
        # in with access to the enrolment handshake and nothing else.
        return CLASS_BOOTSTRAP, "", None

    return None, "", _token_refusal(path, headers, token)
