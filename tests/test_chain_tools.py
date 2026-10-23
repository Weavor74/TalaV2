"""
The chain tools Tala can call: chain_status, chain_recent, chain_read, chain_verify.

They are read-only, never raise, and never return a document body. These run
against a real scratch node: the failure modes that matter (a payload the node
rejects, a route that does not match, a timeout shorter than the node's own
validity recompute) are exactly the ones a mocked HTTP call cannot show.
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
from sdk.agent import chain_tools
from sdk.agent.tools import ToolRegistry

APP = Path(__file__).resolve().parents[1] / "blockchain" / "src" / "app.js"
TOOLS = ("chain_status", "chain_recent", "chain_read", "chain_verify")


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
    data = tmp_path_factory.mktemp("chain-tools-data")
    port = _free_port()
    env = {**os.environ, "TALA_CHAIN_DATA_DIR": str(data), "TALA_CHAIN_BIND_PORT": str(port)}
    # The node exits when its stdin closes (it treats that as its parent dying).
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
        yield {"base": base, "port": port, "data": data}
        mp.undo()
    finally:
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def anchored(node):
    """A few real transactions on the scratch chain, written the way the harness writes them."""
    import asyncio

    async def write():
        for i in range(3):
            result = await chains._post(node["base"], {
                "id": f"tx-tools-{i}", "type": "task-complete", "counterparty": "",
                "document_ref": f"node-{i}", "metadata": {"n": i, "document_type": "thought-state"},
                "timestamp": "2026-10-22T12:00:00+00:00"}, guard=False)
            assert result["written"], result

    asyncio.run(write())
    return ["tx-tools-0", "tx-tools-1", "tx-tools-2"]


def test_the_tools_are_registered_with_schemas():
    registered = {t["name"]: t for t in ToolRegistry.get_tools()}
    for name in TOOLS:
        assert name in registered, name
        assert registered[name]["description"]
    assert "limit" in json.dumps(registered["chain_recent"]["schema"])
    assert "tx_id" in json.dumps(registered["chain_read"]["schema"])


@pytest.mark.asyncio
async def test_status_reports_a_healthy_node(node, anchored):
    status = await chain_tools.chain_status()
    assert status["success"] is True and status["reachable"] is True
    assert status["is_valid"] is True
    assert status["height"] >= 1 + len(anchored)
    assert len(status["latest_block_hash"]) == 12
    assert status["endpoint"] == node["base"]


@pytest.mark.asyncio
async def test_recent_returns_the_newest_blocks_without_document_bodies(node, anchored):
    result = await chain_tools.chain_recent(limit=2)
    assert result["success"] is True and result["count"] == 2
    ids = [t["id"] for b in result["blocks"] for t in b["transactions"]]
    assert ids == anchored[-2:], "the tail, oldest first"
    assert "document" not in json.dumps(result).replace("has_document", "").replace("document_type", "")
    assert all(b["signer"] for b in result["blocks"]), "a signed block names its signer"


@pytest.mark.asyncio
async def test_recent_clamps_its_limit(node, anchored):
    assert (await chain_tools.chain_recent(limit=0))["count"] == 1
    assert (await chain_tools.chain_recent(limit=10_000))["count"] <= 50
    assert (await chain_tools.chain_recent(limit="banana"))["success"] is True


@pytest.mark.asyncio
async def test_read_finds_a_transaction_and_summarises_it(node, anchored):
    result = await chain_tools.chain_read(anchored[1])
    assert result["found"] is True
    assert result["transaction"]["type"] == "task-complete"
    assert result["transaction"]["creator"] == "tala-harness"
    assert result["metadata"]["document_ref"] == "node-1"
    assert result["transaction"]["has_document"] is False


@pytest.mark.asyncio
async def test_read_handles_an_id_with_a_colon(node):
    import asyncio

    wallet = "wallet-tools-1"
    await asyncio.to_thread(
        urllib.request.urlopen,
        urllib.request.Request(f"{node['base']}/chain/peers/register", method="POST",
                               headers={"Content-Type": "application/json"},
                               data=json.dumps({"username": "@tools", "wallet_address": wallet,
                                                "endpoint_url": "ws://203.0.113.5:8765"}).encode()),
    )
    result = await chain_tools.chain_read(f"peer-directory:{wallet}")
    assert result["found"] is True and result["transaction"]["type"] == "peer-directory-register"


@pytest.mark.asyncio
async def test_read_of_an_unknown_id_is_an_answer_not_an_error(node):
    result = await chain_tools.chain_read("no-such-transaction")
    assert result == {"success": True, "reachable": True, "found": False, "tx_id": "no-such-transaction"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "../../etc/passwd", "a b", "x" * 201, None])
async def test_read_refuses_a_malformed_id_without_asking_the_node(bad):
    result = await chain_tools.chain_read(bad)
    assert result["success"] is False and "tx_id" in result["error"]


@pytest.mark.asyncio
async def test_verify_audits_the_ledger_file_independently(node, anchored):
    result = await chain_tools.chain_verify()
    assert result["success"] is True
    assert result["valid"] is True, result["problems"]
    assert result["height"] >= 1 + len(anchored)
    assert result["signed_blocks"] >= len(anchored)
    assert result["signers_checked_against_authority_set"] is True


@pytest.mark.asyncio
async def test_verify_reports_a_tampered_ledger(node, anchored, tmp_path, monkeypatch):
    blocks = json.loads((node["data"] / "chain.json").read_text(encoding="utf-8"))
    blocks[-1]["transactions"][0]["creator"] = "someone-else"
    (tmp_path / "chain.json").write_text(json.dumps(blocks), encoding="utf-8")
    shutil.copy(node["data"] / "authorities.json", tmp_path / "authorities.json")
    monkeypatch.setenv("TALA_CHAIN_DATA_DIR", str(tmp_path))
    result = await chain_tools.chain_verify()
    assert result["success"] is True and result["valid"] is False
    assert any("do not match its hash" in p for p in result["problems"])


@pytest.mark.asyncio
async def test_verify_without_a_ledger_file_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_CHAIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.setattr(chain_tools, "_chain_dir", lambda: tmp_path)
    result = await chain_tools.chain_verify()
    assert result["success"] is False and "no chain.json" in result["error"]


@pytest.mark.asyncio
async def test_a_node_that_is_down_is_an_answer_never_an_exception(monkeypatch):
    monkeypatch.setenv("TALA_CHAIN_BIND_PORT", "1")
    for outcome in (await chain_tools.chain_status(), await chain_tools.chain_recent(),
                    await chain_tools.chain_read("tx-anything")):
        assert outcome["success"] is False and outcome["reachable"] is False
        assert "TALA_CHAIN_BIND_PORT" in outcome["hint"]
