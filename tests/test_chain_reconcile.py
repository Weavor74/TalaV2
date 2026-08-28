"""
Reading the shared chain back.

Writing to a chain everybody shares and never reading it leaves this install
with a record of what *it* put there and no idea what anyone else did. That was
the gap left after the Ghost/Real split: ``/chain/reconcile`` existed on the
Node server and had no caller.

Reconcile pulls. It deliberately does not use that endpoint, which overwrites
the receiving node's chain whenever the incoming one is as long or longer --
fine between a machine and its own backup, data loss on a chain several people
write to, since two peers who each appended while the other was offline both
hold a chain at least as long as the other's and the second to push destroys
the first one's blocks.

The chains built here are real: hashed and Ed25519-signed the same way
``blockchain/src/app.js`` does it, so a tampered chain is genuinely tampered
rather than merely flagged.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from python.harness import chain_verify, chains

KEY = Ed25519PrivateKey.generate()
PUBLIC_PEM = KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()


def make_transaction(tx_id, kind="peer-skill", counterparty="@peer",
                     creator="@peer"):
    """A transaction with the field order the Node Transaction class uses."""
    body = {
        "id": tx_id, "type": kind, "hash": hashlib.sha256(tx_id.encode()).hexdigest(),
        "creator": creator, "counterparty": counterparty,
        "timestamp": 1756000000000, "document": None, "metadata": None,
        "signature": None, "public_key": None,
    }
    return body


def make_chain(transactions_per_block, sign=True, start=1756000000000):
    """Build a chain that verifies, block hashes and signatures included."""
    blocks = []
    previous = "0"
    for index, transactions in enumerate(transactions_per_block):
        block = {
            "index": index, "previousHash": previous,
            "timestamp": start + index * 1000,
            "transactions": transactions, "nonce": 0,
        }
        block["hash"] = chain_verify.block_hash(block)
        if sign and index:
            block["signature"] = KEY.sign(block["hash"].encode()).hex()
            block["public_key"] = PUBLIC_PEM
        else:
            block["signature"] = None
            block["public_key"] = None
        previous = block["hash"]
        blocks.append(block)
    return blocks


# -- verification -------------------------------------------------------------

def test_the_existing_chain_verifies():
    """The check must agree with the Node implementation byte for byte; a
    chain one accepts and the other rejects is a split nobody can diagnose."""
    from pathlib import Path

    chain_file = Path(__file__).resolve().parents[1] / "blockchain" / "data" / "chain.json"
    if not chain_file.exists():
        pytest.skip("no local chain to check against")

    data = json.loads(chain_file.read_text(encoding="utf-8"))
    blocks = data.get("blocks", data) if isinstance(data, dict) else data

    verdict = chain_verify.verify(blocks)
    assert verdict.valid, verdict.problems[:3]
    assert verdict.height == len(blocks)
    assert verdict.unsigned == 1, "only genesis should be unsigned"


def test_a_chain_built_here_verifies():
    blocks = make_chain([[make_transaction("genesis")],
                         [make_transaction("t1")],
                         [make_transaction("t2")]])
    assert chain_verify.verify(blocks)


def test_an_empty_shared_chain_is_not_a_failure():
    """A shared chain nobody has written to yet is the ordinary state of a new
    one."""
    verdict = chain_verify.verify([])
    assert verdict.valid
    assert verdict.height == 0


def test_altered_contents_are_caught():
    blocks = make_chain([[make_transaction("genesis")], [make_transaction("t1")]])
    blocks[1]["transactions"][0]["counterparty"] = "@attacker"

    verdict = chain_verify.verify(blocks)
    assert not verdict.valid
    assert any("do not match its hash" in problem for problem in verdict.problems)


def test_a_broken_link_is_caught():
    blocks = make_chain([[make_transaction("genesis")],
                         [make_transaction("t1")],
                         [make_transaction("t2")]])
    # re-hash the block so only the link is wrong, not the contents
    blocks[2]["previousHash"] = "0" * 64
    blocks[2]["hash"] = chain_verify.block_hash(blocks[2])
    blocks[2]["signature"] = KEY.sign(blocks[2]["hash"].encode()).hex()

    verdict = chain_verify.verify(blocks)
    assert not verdict.valid
    assert any("does not link" in problem for problem in verdict.problems)


def test_a_forged_signature_is_caught():
    """Contents and links both check out; only the signature is wrong."""
    other = Ed25519PrivateKey.generate()
    blocks = make_chain([[make_transaction("genesis")], [make_transaction("t1")]])
    blocks[1]["signature"] = other.sign(blocks[1]["hash"].encode()).hex()

    verdict = chain_verify.verify(blocks)
    assert not verdict.valid
    assert any("signature does not verify" in problem for problem in verdict.problems)


def test_rubbish_is_rejected_rather_than_crashing():
    assert not chain_verify.verify("not a chain")
    assert not chain_verify.verify([{"index": 0}, "nonsense"])


def test_problems_are_reported_together_not_one_at_a_time():
    blocks = make_chain([[make_transaction(f"t{n}")] for n in range(6)])
    for block in blocks[1:]:
        block["nonce"] = 99  # invalidates every hash after genesis

    verdict = chain_verify.verify(blocks)
    assert len(verdict.problems) > 1


def test_transactions_keep_their_position_not_their_id():
    """The transaction id is a document identity that repeats -- 2,748
    transactions carry 185 distinct ids in the existing chain -- so keying on
    it would collapse a peer's history exactly as it collapsed ours."""
    blocks = make_chain([
        [make_transaction("settings-workflows")],
        [make_transaction("settings-workflows")],
    ])
    flat = chain_verify.transactions_of(blocks)

    assert len(flat) == 2
    assert {t["_entry_id"] for t in flat} == {"block-0-0", "block-1-0"}


# -- reconcile ----------------------------------------------------------------

@pytest.fixture
def shared(monkeypatch):
    """A shared chain to pull from, and an in-memory Real index."""
    state = {"blocks": [], "status": 200, "endpoint": "https://chain.example"}
    rows: list[dict] = []
    marked: list[tuple[str, str]] = []

    class Response:
        status_code = 200

        def json(self_inner):
            return state["blocks"]

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            response = Response()
            response.status_code = state["status"]
            return response

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def endpoint():
        return state["endpoint"]
    monkeypatch.setattr(chains, "real_endpoint", endpoint)

    async def net_check(target, principal, purpose=""):
        from python.harness import network_guard as ng
        return ng.NetDecision(True, "permitted", "chain.example", 443, "", {})
    from python.harness import network_guard as ng
    monkeypatch.setattr(ng, "check", net_check)

    from python.harness import db

    async def fetch(query, *args):
        return rows
    monkeypatch.setattr(db, "fetch", fetch)

    indexed: list[dict] = []

    async def index(chain, transaction, pending=False, entry_id="", origin="local"):
        indexed.append({"chain": chain, "entry_id": entry_id,
                        "tx_id": transaction["id"], "origin": origin,
                        "type": transaction["type"]})
        return {"written": True}
    monkeypatch.setattr(chains, "index", index)

    async def mark(entry_id, remote_id=""):
        marked.append((entry_id, remote_id))
    monkeypatch.setattr(chains, "_mark_written", mark)

    return {"state": state, "rows": rows, "indexed": indexed, "marked": marked}


@pytest.mark.asyncio
async def test_peers_transactions_are_learned(shared):
    shared["state"]["blocks"] = make_chain([
        [make_transaction("genesis", "mint", "tala-network")],
        [make_transaction("skill-a")],
        [make_transaction("skill-b")],
    ])

    result = await chains.reconcile()

    assert result["reconciled"]
    assert result["learned"] == 3
    assert {row["origin"] for row in shared["indexed"]} == {"peer"}
    assert {row["chain"] for row in shared["indexed"]} == {"real"}


@pytest.mark.asyncio
async def test_ghost_is_never_touched(shared):
    """Nothing a peer sends can add to, alter or remove local history."""
    shared["state"]["blocks"] = make_chain([[make_transaction("genesis")],
                                            [make_transaction("theirs")]])

    await chains.reconcile()

    assert all(row["chain"] == "real" for row in shared["indexed"])


@pytest.mark.asyncio
async def test_what_is_already_indexed_is_not_learned_twice(shared):
    shared["state"]["blocks"] = make_chain([[make_transaction("genesis")],
                                            [make_transaction("skill-a")]])
    shared["rows"].append({"entry_id": "block-1-0", "tx_id": "skill-a",
                           "pending": False})

    result = await chains.reconcile()

    assert result["learned"] == 1  # genesis only
    assert [row["entry_id"] for row in shared["indexed"]] == ["block-0-0"]


@pytest.mark.asyncio
async def test_our_own_queued_transaction_is_confirmed_not_duplicated(shared):
    """The case that would otherwise resend forever: the POST succeeded and its
    response never arrived, so the transaction is on the chain and still marked
    pending here."""
    shared["state"]["blocks"] = make_chain([[make_transaction("genesis")],
                                            [make_transaction("mine-1")]])
    shared["rows"].append({"entry_id": "real-mine-1", "tx_id": "mine-1",
                           "pending": True})

    result = await chains.reconcile()

    assert result["confirmed"] == 1
    assert result["still_pending"] == 0
    assert shared["marked"] == [("real-mine-1", "block-1-0")]
    assert not any(row["tx_id"] == "mine-1" for row in shared["indexed"])


@pytest.mark.asyncio
async def test_a_queued_transaction_the_chain_does_not_have_stays_queued(shared):
    shared["state"]["blocks"] = make_chain([[make_transaction("genesis")]])
    shared["rows"].append({"entry_id": "real-mine-1", "tx_id": "mine-1",
                           "pending": True})

    result = await chains.reconcile()

    assert result["confirmed"] == 0
    assert result["still_pending"] == 1
    assert shared["marked"] == []


@pytest.mark.asyncio
async def test_a_chain_that_does_not_verify_is_rejected_whole(shared):
    """No partial import. A chain with the bad blocks skipped is not a chain."""
    blocks = make_chain([[make_transaction("genesis")],
                         [make_transaction("good")],
                         [make_transaction("tampered")]])
    blocks[2]["transactions"][0]["counterparty"] = "@attacker"
    shared["state"]["blocks"] = blocks

    result = await chains.reconcile()

    assert not result["reconciled"]
    assert result["reason"] == "the shared chain does not verify"
    assert result["problems"]
    assert shared["indexed"] == [], "nothing from an unverified chain is kept"


@pytest.mark.asyncio
async def test_nothing_happens_without_an_endpoint(shared):
    shared["state"]["endpoint"] = ""

    result = await chains.reconcile()

    assert not result["reconciled"]
    assert "no Real chain endpoint" in result["reason"]
    assert shared["indexed"] == []


@pytest.mark.asyncio
async def test_a_refused_connection_says_so(shared):
    shared["state"]["status"] = 503

    result = await chains.reconcile()

    assert not result["reconciled"]
    assert "503" in result["reason"]


@pytest.mark.asyncio
async def test_the_guardrails_govern_the_pull(shared, monkeypatch):
    from python.harness import network_guard as ng

    async def refuse(target, principal, purpose=""):
        return ng.NetDecision(False, "blocked by rule", "chain.example", 443,
                              "deny:*", {})
    monkeypatch.setattr(ng, "check", refuse)

    result = await chains.reconcile()

    assert not result["reconciled"]
    assert "blocked by rule" in result["reason"]
    assert shared["indexed"] == []


@pytest.mark.asyncio
async def test_a_guard_that_cannot_be_consulted_does_not_permit_the_pull(
        shared, monkeypatch):
    from python.harness import network_guard as ng

    async def boom(*a, **k):
        raise RuntimeError("no database")
    monkeypatch.setattr(ng, "check", boom)

    result = await chains.reconcile()

    assert not result["reconciled"]
    assert "guardrails unavailable" in result["reason"]


# -- the wiring ---------------------------------------------------------------

def test_reconcile_never_pushes():
    """The Node server's POST /chain/reconcile overwrites the receiving chain
    whenever the incoming one is as long or longer. On a chain several people
    write to, the second peer to push destroys the first one's blocks."""
    import ast
    import inspect

    source = inspect.getsource(chains.reconcile)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "post", "reconcile must not push a chain"
    assert "/chain/blocks" in source


def test_the_rpcs_exist():
    from python.harness.server import _HANDLERS

    assert "chain.reconcile" in _HANDLERS
    assert "chain.verify" in _HANDLERS


def test_reconnecting_reads_back_as_well_as_writing():
    """A flush leaves this install knowing what it sent and nothing about what
    anyone else appended meanwhile."""
    import ast
    import inspect

    from python.harness import server

    tree = ast.parse(inspect.getsource(server._real_chain_flush_loop))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)}
    assert {"flush", "reconcile"} <= called
