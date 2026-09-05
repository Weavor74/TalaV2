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


def _token_path() -> Path:
    from sdk.agent.documentation_nodes import resolve_user_data_dir
    return resolve_user_data_dir() / "vault" / ".keys" / _TOKEN_FILE


def load_or_create_token() -> str:
    """
    The shared secret for 8765.

    ``TALA_WEB_TOKEN`` wins when set, so an operator who pins one gets the one
    they pinned. Otherwise it is read from -- or created in -- the vault
    alongside ``credential_key.bin``, which is already the convention for
    key material and is already excluded from git.

    Returns "" only if the token can be neither read nor written, which is
    reported by the caller rather than silently disabling the gate.
    """
    pinned = (os.environ.get("TALA_WEB_TOKEN") or os.environ.get("TALA_WS_TOKEN") or "").strip()
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


def check(path: str, headers: Any, token: str) -> tuple[int, str] | None:
    """
    Decide one handshake. Returns None to accept, or (status, reason).

    Kept free of websockets types so it can be unit-tested with a plain dict,
    which is the difference between this being covered and being "covered".
    """
    origin = (headers.get("origin") if headers is not None else None) or ""
    origin = str(origin).strip().rstrip("/")
    if origin and origin not in _allowed_origins():
        # The drive-by case: a page on the internet opening a socket to a
        # service on the user's own machine.
        return 403, f"origin {origin!r} is not allowed"

    if not token:
        # No token could be established. Refusing is the safe direction: the
        # alternative is serving 247 handlers to anyone who asks.
        return 500, "no WebSocket token is configured"

    supplied = _supplied_token(path, headers)
    if not supplied:
        return 401, "a token is required"
    if not secrets.compare_digest(supplied, token):
        return 401, "invalid token"
    return None


def make_process_request(token: str):
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

        try:
            verdict = check(path, headers, token)
        except Exception:
            logger.exception("ws_auth: handshake check failed; refusing")
            verdict = (500, "auth error")

        if verdict is None:
            return None

        status, reason = verdict
        logger.warning("ws_auth: refused a connection (%s): %s", status, reason)
        # The body is deliberately terse: a rejected client learns that it was
        # rejected, not which of the two gates it failed.
        if legacy:
            return (HTTPStatus(status), [("Content-Type", "text/plain")], b"forbidden\n")
        return first.respond(HTTPStatus(status), "forbidden\n")

    return process_request
