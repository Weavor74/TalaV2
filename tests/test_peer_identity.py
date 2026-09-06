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

from python.harness import server

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


async def test_nothing_still_reads_the_bare_environment_variable():
    """
    The regression that mattered: four call sites each resolving identity
    their own way is how they drift apart.
    """
    import inspect

    src = inspect.getsource(server)
    occurrences = src.count('os.environ.get("TALA_USERNAME"')

    assert occurrences == 1, (
        f"{occurrences} places read TALA_USERNAME directly; only _peer_self_username should"
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
