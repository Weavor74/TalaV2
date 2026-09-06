"""
Who this install says it is on the network.

Four separate places needed a peer identity and each read a TALA_USERNAME
environment variable that nothing ever set, so every install answered to
`@local-tala`. Two of them meeting is not a cosmetic problem:
`contacts.username` is a primary key and `wallet_address` is unique, so the
second install to arrive collides with the first. Confirmed live -- the
desktop and the VPS both claimed exactly that name.

The identity belongs in the user profile, where the person set it. Steven's
framing: "my real name is Steven Pollard, but my user name is weavor ...
that's what's shared on the blockchain." A callsign, not a legal name.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from python.harness import db, server

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_pinned_username(monkeypatch):
    monkeypatch.delenv("TALA_USERNAME", raising=False)
    yield


def _profile(monkeypatch, identity: dict):
    async def load_config():
        return {"identity": identity}

    monkeypatch.setattr("python.harness.user_profile.load_config", load_config)


async def test_the_callsign_comes_from_the_profile(monkeypatch):
    _profile(monkeypatch, {"username": "weavor", "name": "Steven Pollard"})
    assert await server._peer_self_username() == "@weavor"


async def test_the_legal_name_is_never_the_network_identity(monkeypatch):
    """
    This value is published to a chain other people read. A profile with a
    real name and no handle must not broadcast the real name.
    """
    _profile(monkeypatch, {"name": "Steven Pollard", "pronouns": "he/him"})

    identity = await server._peer_self_username()

    assert identity == server.DEFAULT_PEER_USERNAME
    assert "Steven" not in identity and "Pollard" not in identity


async def test_a_nickname_serves_when_there_is_no_handle(monkeypatch):
    _profile(monkeypatch, {"nickname": "weavor", "name": "Steven Pollard"})
    assert await server._peer_self_username() == "@weavor"


async def test_the_handle_wins_over_the_nickname(monkeypatch):
    _profile(monkeypatch, {"username": "weavor", "nickname": "Steve-o"})
    assert await server._peer_self_username() == "@weavor"


async def test_the_at_sign_is_added_once_and_only_once(monkeypatch):
    for stored in ("weavor", "@weavor"):
        _profile(monkeypatch, {"username": stored})
        assert await server._peer_self_username() == "@weavor"


async def test_a_handle_with_spaces_is_made_addressable(monkeypatch):
    """It is used as a routing key, so it cannot carry whitespace."""
    _profile(monkeypatch, {"username": "weavor  the   builder"})
    assert await server._peer_self_username() == "@weavor-the-builder"


async def test_an_operator_can_pin_an_identity(monkeypatch):
    """
    A connection node is headless and has no profile to read, which is the
    one case the environment variable still exists for.
    """
    _profile(monkeypatch, {"username": "weavor"})
    monkeypatch.setenv("TALA_USERNAME", "@tala-vps")
    assert await server._peer_self_username() == "@tala-vps"

    monkeypatch.setenv("TALA_USERNAME", "tala-vps")
    assert await server._peer_self_username() == "@tala-vps"


async def test_no_profile_and_no_pin_falls_back_rather_than_failing(monkeypatch):
    async def exploding():
        raise RuntimeError("no profile node, no database")

    monkeypatch.setattr("python.harness.user_profile.load_config", exploding)
    assert await server._peer_self_username() == server.DEFAULT_PEER_USERNAME


async def test_nothing_resolves_the_identity_on_its_own():
    """
    Call sites each resolving identity their own way is how they drift apart.

    The first version of this counted only `os.environ.get("TALA_USERNAME")`
    and passed while a fifth site quietly hardcoded the default instead:

        "sender_username": params.get("sender_username") or "@local-tala"

    That was the live path for `chain.connect_vps`, so a real enrolment
    against talacore.app filed the desktop as @local-tala while the profile
    said "steve". Counting the literal too is what makes this a guard rather
    than a description of one spelling.
    """
    import inspect

    src = inspect.getsource(server)

    env_reads = src.count('os.environ.get("TALA_USERNAME"')
    assert env_reads == 1, (
        f"{env_reads} places read TALA_USERNAME directly; only _peer_self_username should"
    )

    # The constant may be defined once and returned by the resolver; anywhere
    # else it is a second, silent answer to "who are we".
    literals = src.count('"@local-tala"')
    assert literals <= 1, (
        f"{literals} hardcoded @local-tala defaults; identity must come from "
        "_peer_self_username, which owns DEFAULT_PEER_USERNAME"
    )


@pytest_asyncio.fixture
async def pool():
    await db.init_pool()
    yield
    await db.close_pool()


@pytest.mark.asyncio
async def test_the_enrolment_packet_carries_the_profile_identity(monkeypatch, pool):
    """
    The exact path chain.connect_vps takes, which is where this went wrong
    live: it passes no sender_username, so the fallback IS the behaviour.
    """
    import json

    _profile(monkeypatch, {"username": "steve", "nickname": "Weavor"})

    captured = {}

    async def fake_get_contact(**kwargs):
        return {
            "username": "@tala-vps", "wallet_address": "tala-vps-main",
            "endpoint_url": "ws://talacore.app:8765", "shared_secret": "s3cret",
            "permissions": {}, "status": "approved",
        }

    async def fake_profile_config():
        return {}

    async def fake_call_peer_rpc(endpoint, method, params):
        captured["packet"] = params.get("packet") or {}
        return {"accepted": True}

    monkeypatch.setattr(server, "_get_contact", fake_get_contact)
    monkeypatch.setattr(server, "_get_master_profile_config", fake_profile_config)
    monkeypatch.setattr(server, "_call_peer_rpc", fake_call_peer_rpc)

    await server.handle_p2p_handshake(
        {"username": "@tala-vps", "endpoint_url": "ws://talacore.app:8765"}, 1
    )

    assert captured["packet"]["sender_username"] == "@steve", (
        f"enrolled as {captured['packet'].get('sender_username')!r} rather than the profile handle"
    )


async def test_the_signed_handshake_carries_the_profile_identity(monkeypatch):
    """
    End to end: the credential a peer presents names the callsign, so the
    far side files the contact under the handle the person chose.
    """
    from python.harness import ws_auth

    _profile(monkeypatch, {"username": "weavor"})
    who = await server._peer_self_username()
    headers = server._peer_handshake_headers(who, "shared-secret")

    assert headers[ws_auth.PEER_HEADER] == "@weavor"

    async def secret_for(username):
        return "shared-secret" if username == "@weavor" else ""

    cls, name, refusal = await ws_auth.classify("/", headers, "tok", secret_for=secret_for)
    assert refusal is None
    assert (cls, name) == (ws_auth.CLASS_PEER, "@weavor")
