"""
The connection node is named, not addressed.

`_call_peer_rpc` decides whether a peer is the project's VPS -- which gates
the write state machine -- and it used to do that by matching "34.60." in
the endpoint: the current Google Cloud instance's IP prefix.

That breaks on any move: a new instance, a different provider, a CDN in
front. And it breaks silently, because the only symptom is that write
gating stops applying to a node that is still the VPS. A hostname survives
all of those.
"""

from __future__ import annotations

import pytest

from python.harness import server


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch):
    monkeypatch.delenv("TALA_VPS_HOST", raising=False)
    yield


def test_the_project_vps_is_recognised_by_name():
    for endpoint in (
        "wss://talacore.app:8765",
        "ws://talacore.app",
        "https://TALACORE.APP/chain",
    ):
        assert server._endpoint_is_project_vps(endpoint), endpoint


def test_the_old_hardcoded_ip_is_gone():
    """
    The instance may still answer on it, but the address must not be what
    identifies it. Pinning this so it cannot creep back.
    """
    import inspect

    assert "34.60." not in inspect.getsource(server), "an instance IP is hardcoded again"


def test_an_unrelated_endpoint_is_not_the_vps():
    for endpoint in ("wss://peer.example:8765", "ws://127.0.0.1:8765", ""):
        assert not server._endpoint_is_project_vps(endpoint), endpoint


def test_the_host_can_be_moved(monkeypatch):
    """The point of the change: pointing an install at a different service."""
    monkeypatch.setenv("TALA_VPS_HOST", "tala.example.net")

    assert server._endpoint_is_project_vps("wss://tala.example.net:8765")
    assert not server._endpoint_is_project_vps("wss://talacore.app:8765"), (
        "the old host still matches after being reconfigured"
    )


def test_several_hosts_can_be_named(monkeypatch):
    """A migration means both are live for a while."""
    monkeypatch.setenv("TALA_VPS_HOST", "talacore.app, tala.example.net")

    assert server._endpoint_is_project_vps("wss://talacore.app:8765")
    assert server._endpoint_is_project_vps("wss://tala.example.net:8765")
    assert not server._endpoint_is_project_vps("wss://somewhere.else")


def test_an_empty_setting_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("TALA_VPS_HOST", "   ")
    assert server._vps_hosts() == (server.DEFAULT_VPS_HOST,)
