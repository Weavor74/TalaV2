"""
8765 must not answer to anyone who asks (SEC-W0-03).

The harness dispatched 247 JSON-RPC handlers -- shell execution and source
patching among them -- to any client that completed a WebSocket handshake, with
no authentication. Binding loopback was mistaken for a control: WebSocket
handshakes are exempt from CORS, so any page the user visited could open a
socket to their own machine and drive the whole surface.

These assert both gates independently, because each covers a case the other
misses: Origin stops a browser that has somehow obtained a token, and the token
stops a local client that sends no Origin at all.
"""

import pytest

from python.harness import ws_auth

TOKEN = "test-token-abcdef"


def _headers(**kw):
    """Case-insensitive-enough stand-in for websockets' Headers."""
    return {k.replace("_", "-").lower(): v for k, v in kw.items()}


# -- the drive-by case -------------------------------------------------------

def test_a_web_page_is_refused_even_with_a_valid_token(monkeypatch):
    """The whole point. A page on the internet can open a socket to loopback
    and CORS will not stop it, so Origin has to."""
    monkeypatch.delenv("TALA_WS_ALLOWED_ORIGINS", raising=False)
    verdict = ws_auth.check(
        f"/?token={TOKEN}",
        _headers(origin="https://evil.example"),
        TOKEN,
    )
    assert verdict is not None
    assert verdict[0] == 403


def test_null_origin_is_refused(monkeypatch):
    """A sandboxed iframe sends Origin: null. Allowing it would reopen the
    hole."""
    monkeypatch.delenv("TALA_WS_ALLOWED_ORIGINS", raising=False)
    verdict = ws_auth.check(f"/?token={TOKEN}", _headers(origin="null"), TOKEN)
    assert verdict is not None and verdict[0] == 403


def test_the_web_ui_origin_is_allowed(monkeypatch):
    monkeypatch.setenv("TALA_WEB_PORT", "8767")
    monkeypatch.delenv("TALA_WS_ALLOWED_ORIGINS", raising=False)
    assert ws_auth.check(
        f"/?token={TOKEN}",
        _headers(origin="http://127.0.0.1:8767"),
        TOKEN,
    ) is None


def test_extra_origins_can_be_configured(monkeypatch):
    monkeypatch.setenv("TALA_WS_ALLOWED_ORIGINS", "https://tala.example")
    assert ws_auth.check(
        f"/?token={TOKEN}", _headers(origin="https://tala.example"), TOKEN
    ) is None


# -- the token ---------------------------------------------------------------

def test_no_token_is_refused():
    assert ws_auth.check("/", _headers(), TOKEN)[0] == 401


def test_wrong_token_is_refused():
    assert ws_auth.check("/?token=nope", _headers(), TOKEN)[0] == 401


@pytest.mark.parametrize("path,headers", [
    ("/?token=" + TOKEN, {}),
    ("/", {"authorization": f"Bearer {TOKEN}"}),
    ("/", {"authorization": TOKEN}),
    ("/", {"x-tala-token": TOKEN}),
])
def test_every_supported_token_spelling_is_accepted(path, headers):
    assert ws_auth.check(path, headers, TOKEN) is None


def test_a_missing_server_token_refuses_rather_than_opens(monkeypatch):
    """Fail-closed. If no token could be established, the answer is 'no',
    not 'everyone'."""
    verdict = ws_auth.check(f"/?token={TOKEN}", _headers(), "")
    assert verdict is not None and verdict[0] == 500


# -- the token file ----------------------------------------------------------

def test_token_is_pinned_by_env(monkeypatch):
    monkeypatch.setenv("TALA_WEB_TOKEN", "pinned-value")
    assert ws_auth.load_or_create_token() == "pinned-value"


def test_token_is_created_once_and_reused(monkeypatch, tmp_path):
    monkeypatch.delenv("TALA_WEB_TOKEN", raising=False)
    monkeypatch.delenv("TALA_WS_TOKEN", raising=False)
    monkeypatch.setattr(ws_auth, "_token_path", lambda: tmp_path / "vault" / ".keys" / "ws_token.txt")

    first = ws_auth.load_or_create_token()
    assert len(first) >= 32
    assert ws_auth.load_or_create_token() == first, "a new token each call would lock out the running renderer"


# -- the wiring --------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_request_rejects_with_an_http_response():
    handler = ws_auth.make_process_request(TOKEN)
    assert await handler("/?token=" + TOKEN, _headers()) is None

    refused = await handler("/", _headers())
    assert refused is not None
    status, _headers_out, body = refused
    assert int(status) == 401
    # A rejected client learns that it was rejected, not which gate it failed.
    assert b"token" not in body.lower()


def test_the_server_actually_installs_the_gate():
    """The gate existing is not the gate being wired in -- this codebase's
    most common failure is a subsystem nothing calls."""
    import inspect
    from python.harness import server

    src = inspect.getsource(server)
    idx = src.find("websockets.serve(")
    assert idx != -1, "could not find the websockets.serve() call"
    call = src[idx:idx + 600]
    assert "process_request=" in call, (
        "websockets.serve() is not passing the auth gate"
    )


# -- against a real socket ---------------------------------------------------

@pytest.mark.asyncio
async def test_the_gate_works_on_a_real_handshake():
    """
    Bind a socket and actually connect to it.

    This is here because every test above passed while the gate was refusing
    100% of connections, valid ones included. ``check()`` was correct; the
    callback around it destructured the legacy ``(path, headers)`` arguments
    while websockets 16 was calling it with ``(connection, request)``. Only a
    real handshake could tell the difference -- and the failure was
    fail-*closed*, so it would have surfaced as "the app cannot start" rather
    than as a security hole.
    """
    import asyncio

    import websockets

    async def echo(ws, path=""):
        async for message in ws:
            await ws.send(message)

    server = await websockets.serve(
        echo, "127.0.0.1", 0,
        process_request=ws_auth.make_process_request(TOKEN),
    )
    port = next(iter(server.sockets)).getsockname()[1]
    base = f"ws://127.0.0.1:{port}"

    async def connects(url, **kw) -> bool:
        try:
            async with websockets.connect(url, open_timeout=5, **kw) as ws:
                await ws.send("ping")
                return await asyncio.wait_for(ws.recv(), 5) == "ping"
        except Exception:
            return False

    try:
        assert await connects(f"{base}?token={TOKEN}"), "a valid token was refused"
        assert await connects(base, additional_headers={"Authorization": f"Bearer {TOKEN}"})
        assert await connects(f"{base}?token={TOKEN}", origin="http://127.0.0.1:8767")

        assert not await connects(base), "no token was accepted"
        assert not await connects(f"{base}?token=wrong")
        assert not await connects(f"{base}?token={TOKEN}", origin="https://evil.example"), (
            "a hostile page was allowed to drive the harness"
        )
    finally:
        server.close()
        await server.wait_closed()
