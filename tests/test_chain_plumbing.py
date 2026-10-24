"""
The plumbing between the harness and the local chain node.

Three things were wrong, each invisible because the failure was swallowed:

* ``chains.record`` / ``flush`` sent no ``hash`` or ``creator``, so the node
  answered every one with a 400 and only the database index row survived;
* callers wrote ``127.0.0.1:8332`` into themselves, so a node on any other
  port (the configuration the portable install exists for) got no anchors;
* the Electron anchors sent no admin secret (tests/electron/chain_endpoint.test.js).

The node tests drive a real scratch node: the earlier chain tests mocked
``chains._post``, which is exactly how a payload the node rejects went
unnoticed.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from python.harness import chains
from sdk.agent import ghost_persistence as gp

APP = Path(__file__).resolve().parents[1] / "blockchain" / "src" / "app.js"


# --- one place that knows where the node is -----------------------------------------------


def test_the_endpoint_follows_the_same_variables_the_node_reads(monkeypatch):
    for var in ("TALA_CHAIN_BIND_HOST", "TALA_CHAIN_BIND_PORT", "TALA_CHAIN_PORT"):
        monkeypatch.delenv(var, raising=False)
    assert gp.local_chain_base_url() == "http://127.0.0.1:8332"

    monkeypatch.setenv("TALA_CHAIN_BIND_PORT", "9411")
    assert gp.local_chain_host_port() == ("127.0.0.1", 9411)
    monkeypatch.setenv("TALA_CHAIN_PORT", "9999")  # the node prefers BIND_PORT, so do we
    assert gp.local_chain_host_port()[1] == 9411
    monkeypatch.delenv("TALA_CHAIN_BIND_PORT")
    assert gp.local_chain_host_port()[1] == 9999


def test_a_wildcard_bind_address_is_not_somewhere_to_connect(monkeypatch):
    monkeypatch.setenv("TALA_CHAIN_BIND_HOST", "0.0.0.0")
    assert gp.local_chain_host_port()[0] == "127.0.0.1"


def test_a_nonsense_port_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("TALA_CHAIN_BIND_PORT", "not-a-port")
    assert gp.local_chain_host_port()[1] == 8332


@pytest.mark.asyncio
async def test_chains_ghost_endpoint_uses_it(monkeypatch):
    monkeypatch.setenv("TALA_CHAIN_BIND_PORT", "9412")
    assert await chains.ghost_endpoint() == "http://127.0.0.1:9412"


def test_the_anchor_follows_a_moved_port(monkeypatch):
    """canonicalize_and_anchor hardcoded the port; capture the URL it posts to."""
    from sdk.agent import anchor

    seen: list[str] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"tx_id": "abc"}'

    def fake_urlopen(request, timeout=None):
        seen.append(request.full_url)
        return _Response()

    monkeypatch.setenv("TALA_CHAIN_BIND_PORT", "9413")
    monkeypatch.setattr(anchor.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(anchor, "_encrypt_document", lambda document, aad: {"encrypted": True})
    monkeypatch.setattr(chains, "index_sync", lambda *a, **k: True)

    anchor.canonicalize_and_anchor("node-x", "<p>hi</p>", {}, "tester", "html-nn")
    assert seen == ["http://127.0.0.1:9413/chain/tx"]


# --- what is sent -----------------------------------------------------------------------------------


def _tx(**over):
    base = {"id": "tx-abc", "type": "task-complete", "counterparty": "",
            "document_ref": "node-1", "metadata": {"k": 1}, "timestamp": "2026-10-22T12:00:00+00:00"}
    return {**base, **over}


def test_the_wire_form_has_everything_the_node_requires():
    wired = chains._wire(_tx())
    for field in ("id", "type", "hash", "creator", "counterparty"):
        assert wired[field], field
    assert len(wired["hash"]) == 64 and int(wired["hash"], 16) >= 0
    assert wired["counterparty"] == "blockchain-ledger"  # an empty one is a 400
    assert wired["metadata"]["document_ref"] == "node-1"  # a reference, never lost
    assert wired["metadata"]["occurred_at"] == "2026-10-22T12:00:00+00:00"


def test_the_hash_is_of_the_content_so_a_retry_sends_the_same_one():
    assert chains._wire(_tx())["hash"] == chains._wire(_tx())["hash"]
    assert chains._wire(_tx())["hash"] != chains._wire(_tx(metadata={"k": 2}))["hash"]


@pytest.mark.asyncio
async def test_a_malformed_transaction_is_reported_not_raised():
    """_post never raises: a transaction with no type comes back as an error."""
    result = await chains._post("http://127.0.0.1:1", {"id": "t"}, guard=False)
    assert result["written"] is False and "id and a type" in result["error"]


def test_a_supplied_hash_creator_and_counterparty_are_respected():
    wired = chains._wire(_tx(hash="f" * 64, creator="@me", counterparty="@them"))
    assert (wired["hash"], wired["creator"], wired["counterparty"]) == ("f" * 64, "@me", "@them")


# --- against a real node -----------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node.js is not available")
    data = tmp_path_factory.mktemp("chain-data")
    port = _free_port()
    env = {**os.environ, "TALA_CHAIN_DATA_DIR": str(data), "TALA_CHAIN_BIND_PORT": str(port)}
    # The node exits when its stdin closes (it treats that as its parent dying), so keep a pipe open.
    proc = subprocess.Popen(["node", str(APP)], env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                _get(f"{base}/chain/status")
                break
            except Exception:
                time.sleep(0.2)
        else:
            pytest.fail("the scratch chain node never came up")
        mp = pytest.MonkeyPatch()
        mp.setenv("TALA_CHAIN_DATA_DIR", str(data))
        mp.setenv("TALA_CHAIN_BIND_PORT", str(port))
        yield {"base": base, "port": port}
        mp.undo()
    finally:
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_a_record_style_transaction_is_accepted_by_a_real_node(node):
    result = await chains._post(node["base"], _tx(id="tx-plumbing-1"), guard=False)
    assert result["written"] is True, result
    stored = _get(f"{node['base']}/chain/tx/tx-plumbing-1")["transaction"]
    assert stored["creator"] == "tala-harness" and stored["counterparty"] == "blockchain-ledger"


@pytest.mark.asyncio
async def test_record_anchors_on_the_ghost_chain_end_to_end(node, monkeypatch):
    async def no_index(*args, **kwargs):
        return {"written": True}

    monkeypatch.setattr(chains, "index", no_index)
    result = await chains.record("task-complete", {"what": "plumbing"}, counterparty="")
    assert result["ghost"]["anchored"] is True, result
    assert not result["ghost"].get("error")


@pytest.mark.asyncio
async def test_a_moved_port_is_followed_end_to_end(node):
    """The node is on a random port, not 8332 -- the old hardcoding would have missed it."""
    assert node["port"] != 8332
    assert await chains.ghost_endpoint() == node["base"]


# --- a node that is down must not tax every turn -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_down_loopback_node_is_not_retried_on_every_post(monkeypatch):
    """Windows takes ~2 s to refuse a loopback connect, and every turn records a payment: with no node
    running that was a 2 s wait per turn. After a connect failure the next posts back off briefly."""
    import httpx

    attempts: list[object] = []

    class Client:
        def __init__(self, *a, timeout=None, **kw):
            attempts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    chains._node_down_until.clear()
    endpoint = "http://127.0.0.1:59999"
    first = await chains._post(endpoint, _tx(), guard=False)
    second = await chains._post(endpoint, _tx(), guard=False)
    assert first["written"] is False and second["written"] is False
    assert len(attempts) == 1, "the second post must not try to connect again inside the back-off window"
    assert "backing off" in second["error"]
    assert attempts[0].connect <= 1.0, "a loopback connect never needs the 10 s default"

    chains._node_down_until[endpoint] = 0.0  # window over
    await chains._post(endpoint, _tx(), guard=False)
    assert len(attempts) == 2
    chains._node_down_until.clear()


@pytest.mark.asyncio
async def test_a_remote_chain_is_never_backed_off(monkeypatch):
    import httpx

    attempts: list[int] = []

    class Client:
        def __init__(self, *a, **kw):
            attempts.append(1)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    chains._node_down_until.clear()
    for _ in range(2):
        await chains._post("https://chain.example", _tx(signature="s" * 8), guard=False)
    assert len(attempts) == 2
