"""
The one control a user can reach, and whether it can succeed.

`chain.connect_vps` required endpoint_url, wallet_address and shared_secret.
The network toggle in the renderer sent none of them -- it called the method
with three empty strings -- so the only way a person could join the network
could never work. It did not even fail politely: normalising the empty
endpoint raised before the checks that would have named the missing field,
so the user got an internal error.

Everything now has a default, because there IS a known project node and a
toggle has nowhere to ask.
"""

from __future__ import annotations

import json

import pytest

from python.harness import server


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch):
    monkeypatch.delenv("TALA_VPS_HOST", raising=False)
    yield


@pytest.fixture
def captured(monkeypatch):
    """Intercept the writes so nothing needs a database or a network."""
    calls = {"execute": [], "discovery": []}

    async def fake_execute(sql, *args, **kwargs):
        calls["execute"].append((sql, args))

    async def fake_register(endpoint, payload):
        calls["discovery"].append((endpoint, payload))
        return {"status": "registered"}

    async def fake_fetchrow(sql, *args, **kwargs):
        # The handler writes the contact, then reads it back to shake hands.
        if "FROM contacts" in sql:
            return {
                "username": server.DEFAULT_VPS_PEER_USERNAME,
                "wallet_address": server.DEFAULT_VPS_WALLET,
                "endpoint_url": server._default_vps_endpoint(),
                "shared_secret": "minted-secret",
                "permissions": {},
                "status": "approved",
            }
        return None

    monkeypatch.setattr(server.db, "execute", fake_execute)
    monkeypatch.setattr(server.db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(server, "_register_peer_on_chain_directory", fake_register)
    return calls


def test_the_default_endpoint_is_a_hostname():
    endpoint = server._default_vps_endpoint()
    assert "talacore.app" in endpoint
    assert endpoint.startswith("ws://")
    assert not any(part.isdigit() for part in endpoint.split("//")[1].split(":")[0].split(".")), (
        "the default endpoint is an IP address"
    )


def test_the_default_endpoint_follows_a_move(monkeypatch):
    monkeypatch.setenv("TALA_VPS_HOST", "tala.example.net")
    assert server._default_vps_endpoint() == "ws://tala.example.net:8765"


@pytest.mark.asyncio
async def test_the_toggle_gets_past_parameter_validation(captured):
    """
    Exactly what the renderer sends: no parameters at all.

    This asserts the call is no longer rejected for a *missing argument*.
    It still runs on to the outbound handshake, which cannot complete
    against stubs -- that half is covered live, by enrolling against the
    real node.
    """
    body = json.loads(await server.handle_chain_connect_vps({}, 1))
    message = (body.get("error") or {}).get("message", "")

    for missing in ("endpoint_url", "wallet_address", "shared_secret", "Peer endpoint is required"):
        assert missing not in message, f"still refused for a missing {missing}: {message}"


@pytest.mark.asyncio
async def test_it_mints_the_shared_secret_itself(captured):
    """
    Asking a person to supply one is asking them to invent a shared key.
    """
    await server.handle_chain_connect_vps({}, 1)

    contact_writes = [a for sql, a in captured["execute"] if "INSERT INTO contacts" in sql]
    assert contact_writes, "no contact row was written"
    secret = contact_writes[0][3]
    assert isinstance(secret, str) and len(secret) >= 20, f"weak or missing secret: {secret!r}"


@pytest.mark.asyncio
async def test_the_contact_is_named_for_the_node_not_the_operator(captured):
    """
    `username` names the contact being created. The renderer used to pass
    the operator's own handle, which filed the VPS under the user's name.
    """
    await server.handle_chain_connect_vps({}, 1)

    contact_writes = [a for sql, a in captured["execute"] if "INSERT INTO contacts" in sql]
    username, wallet = contact_writes[0][0], contact_writes[0][1]

    assert username == server.DEFAULT_VPS_PEER_USERNAME == "@tala-vps"
    assert wallet == server.DEFAULT_VPS_WALLET == "tala-vps-main"


@pytest.mark.asyncio
async def test_explicit_values_still_win(captured):
    """Pointing an install at a different node stays possible."""
    await server.handle_chain_connect_vps({
        "endpoint_url": "wss://other.example:9000",
        "wallet_address": "other-wallet",
        "shared_secret": "a-secret-we-already-share",
        "username": "@other-node",
    }, 1)

    contact_writes = [a for sql, a in captured["execute"] if "INSERT INTO contacts" in sql]
    username, wallet, endpoint, secret = contact_writes[0][:4]

    assert username == "@other-node"
    assert wallet == "other-wallet"
    assert endpoint == "wss://other.example:9000"
    assert secret == "a-secret-we-already-share"


@pytest.mark.asyncio
async def test_a_bad_permissions_argument_is_still_refused(captured):
    body = json.loads(await server.handle_chain_connect_vps({"permissions": "everything"}, 1))
    assert "error" in body
    assert "permissions" in body["error"]["message"]


def test_the_renderer_sends_what_the_handler_accepts():
    """
    These are written in different languages in different files; only a test
    makes them one contract. The toggle previously sent three empty strings
    into three mandatory parameters.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "renderer" / "renderer.js"
    text = src.read_text(encoding="utf-8", errors="replace")

    assert "method: 'chain.connect_vps', params: {} }" in text, (
        "the toggle no longer calls connect_vps with empty defaults"
    )
    assert "username: profileUsername, endpoint_url: ''" not in text, (
        "the operator's own handle is being passed as the contact name again"
    )
