"""
Ghost and Real are two chains, split by audience.

Steven's design: *"The Ghost chain is only viewable locally. The Real chain is
viewable by all on the chain. They are meant to be separate."* (Q-11) and *"If
you connect with other users, use other skills, transact with AUTH or online
storage, it gets recorded by the Real chain."* (Q-12)

The split existed only as a local variable before this. ``token_counter``
computed ``use_ghost_tokens`` and the answer reached a ``logger.debug`` line
(UI-023); nothing persisted it, so not one of the 2,748 existing transactions
recorded which chain it belonged to.

The four questions that were open are settled here in code: an external
interaction that happens offline is queued and written on reconnect; Ghost
records everything and Real is a strict subset; Real is one shared chain; and
the existing chain is Ghost, kept as it is.
"""
from __future__ import annotations

import json

import pytest

from python.harness import chains


# -- classification -----------------------------------------------------------

@pytest.mark.parametrize("kind,counterparty", [
    ("node-create", "blockchain-ledger"),
    ("node-update", ""),
    ("node-delete", "blockchain-ledger"),
    ("mint", "tala-network"),
    ("thought-state", ""),
    ("behavioral-learning", ""),
])
def test_local_activity_is_ghost(kind, counterparty):
    assert not chains.is_external(kind, counterparty)
    assert chains.classify(kind, counterparty) == ["ghost"]


@pytest.mark.parametrize("kind,counterparty", [
    ("peer-contact", "@alice"),
    ("peer-skill", "@alice"),
    ("skill-shared", "@bob"),
    ("auth-transaction", ""),
    ("remote-storage", ""),
    ("payment", "@carol"),
    ("marketplace", "@dave"),
])
def test_interacting_with_somebody_else_is_real(kind, counterparty):
    assert chains.is_external(kind, counterparty)
    assert chains.classify(kind, counterparty) == ["ghost", "real"]


def test_a_named_second_party_is_external_whatever_it_calls_itself():
    """The kind list cannot be exhaustive, so a counterparty is enough."""
    assert chains.is_external("something-new", "@alice")


def test_a_placeholder_counterparty_names_nobody():
    """
    Every one of the 2,748 existing transactions carries a counterparty --
    2,747 against ``blockchain-ledger`` and one against ``tala-network``. They
    are placeholders for the chain itself, not second parties. Reading them as
    real would have published the machine's entire local history to a chain
    everyone can read.
    """
    for placeholder in chains.PLACEHOLDER_COUNTERPARTIES:
        assert not chains.is_external("unknown-kind", placeholder)


def test_a_declared_local_kind_beats_a_stray_counterparty():
    """A node this machine wrote stays Ghost even carrying a counterparty."""
    assert not chains.is_external("node-create", "@someone")


def test_an_unrecognised_kind_with_nobody_on_the_other_end_is_ghost():
    """The failure modes are not symmetric: a local transaction wrongly
    published cannot be unpublished."""
    assert not chains.is_external("mystery", "")
    assert not chains.is_external("", "")


# -- recording ----------------------------------------------------------------

@pytest.fixture
def ledger(monkeypatch):
    """The ledger in memory, with both chain endpoints unreachable."""
    rows: list[dict] = []
    posts: list[tuple[str, dict]] = []
    state = {"real": "", "accept": False}

    async def index(chain, transaction, pending=False, entry_id=""):
        rows.append({"chain": chain, "tx_id": transaction["id"],
                     "type": transaction["type"], "pending": pending,
                     "entry_id": entry_id or f"e{len(rows)}",
                     "counterparty": transaction.get("counterparty", "")})
        return {"written": True}

    async def post(endpoint, transaction, guard=True):
        posts.append((endpoint, transaction))
        if state["accept"]:
            return {"written": True, "remote_id": "remote-1"}
        return {"written": False, "error": "unreachable"}

    async def real_endpoint():
        return state["real"]

    async def mark(entry_id, remote_id=""):
        for row in rows:
            if row["entry_id"] == entry_id:
                row["pending"] = False
                row["remote_id"] = remote_id

    monkeypatch.setattr(chains, "index", index)
    monkeypatch.setattr(chains, "_post", post)
    monkeypatch.setattr(chains, "real_endpoint", real_endpoint)
    monkeypatch.setattr(chains, "_mark_written", mark)
    return {"rows": rows, "posts": posts, "state": state}


@pytest.mark.asyncio
async def test_a_local_transaction_never_reaches_the_real_chain(ledger):
    result = await chains.record("node-update", {"x": 1})

    assert result["chains"] == ["ghost"]
    assert not result["external"]
    assert [row["chain"] for row in ledger["rows"]] == ["ghost"]
    # posted to the local node, and to nothing else
    assert len(ledger["posts"]) == 1
    assert "127.0.0.1" in ledger["posts"][0][0]


@pytest.mark.asyncio
async def test_ghost_records_everything(ledger):
    """Ghost is the complete local audit trail; Real is a strict subset."""
    ledger["state"]["real"] = "https://chain.example"
    ledger["state"]["accept"] = True

    await chains.record("node-update", {})
    await chains.record("peer-skill", {}, counterparty="@alice")

    ghost = [row for row in ledger["rows"] if row["chain"] == "ghost"]
    real = [row for row in ledger["rows"] if row["chain"] == "real"]
    assert len(ghost) == 2
    assert len(real) == 1
    assert real[0]["tx_id"] in {row["tx_id"] for row in ghost}


@pytest.mark.asyncio
async def test_an_external_transaction_offline_is_queued_not_dropped(ledger):
    result = await chains.record("peer-skill", {}, counterparty="@alice")

    assert result["external"]
    assert result["real"]["queued"] is True
    assert result["real"]["written"] is False
    real = [row for row in ledger["rows"] if row["chain"] == "real"]
    assert real and real[0]["pending"] is True


@pytest.mark.asyncio
async def test_an_external_transaction_online_is_written_immediately(ledger):
    ledger["state"]["real"] = "https://chain.example"
    ledger["state"]["accept"] = True

    result = await chains.record("payment", {"amount": 1}, counterparty="@bob")

    assert result["real"] == {"written": True, "queued": False}
    real = [row for row in ledger["rows"] if row["chain"] == "real"]
    assert real[0]["pending"] is False


@pytest.mark.asyncio
async def test_the_caller_can_override_the_classification(ledger):
    """Paying yourself for your own resources on your own machine is a payment
    that is not an external interaction, and only ``token_counter`` knows."""
    result = await chains.record("payment", {}, external=False)
    assert result["chains"] == ["ghost"]


@pytest.mark.asyncio
async def test_a_local_chain_that_is_down_still_leaves_a_record(ledger):
    """Ghost is the audit trail. Losing the note because the node was down is
    exactly the case the trail exists for."""
    result = await chains.record("node-update", {})

    assert result["ghost"]["written"] is True     # indexed
    assert result["ghost"]["anchored"] is False   # but not anchored
    assert result["ghost"]["error"]


@pytest.mark.asyncio
async def test_a_transaction_needs_a_kind(ledger):
    with pytest.raises(ValueError):
        await chains.record("")


@pytest.mark.asyncio
async def test_the_document_body_is_never_put_on_a_chain(ledger, monkeypatch):
    """A document on the Real chain is a document published to everyone on it."""
    captured = {}

    async def index(chain, transaction, pending=False, entry_id=""):
        captured.update(transaction)
        return {"written": True}
    monkeypatch.setattr(chains, "index", index)

    await chains.record("peer-skill", {"note": "ok"}, counterparty="@a",
                        document_ref="vault-personal-papers")

    assert captured["document_ref"] == "vault-personal-papers"
    assert "document" not in captured


# -- the offline queue --------------------------------------------------------

@pytest.mark.asyncio
async def test_the_queue_drains_on_reconnect(monkeypatch):
    from python.harness import db

    queued = [
        {"entry_id": "e1", "tx_id": "t1", "type": "peer-skill",
         "counterparty": "@a", "document": "", "metadata": "{}",
         "occurred_at": None},
        {"entry_id": "e2", "tx_id": "t2", "type": "payment",
         "counterparty": "@b", "document": "", "metadata": '"{}"',
         "occurred_at": None},
    ]
    cleared: list[str] = []

    async def fetch(query, *args):
        return queued

    async def post(endpoint, transaction, guard=True):
        return {"written": True, "remote_id": "r"}

    async def endpoint():
        return "https://chain.example"

    async def execute(query, *args, caller=""):
        cleared.append(args[0])
        return "UPDATE 1"

    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(chains, "_post", post)
    monkeypatch.setattr(chains, "real_endpoint", endpoint)

    result = await chains.flush()

    assert result == {"flushed": 2, "remaining": 0}
    assert cleared == ["e1", "e2"]


@pytest.mark.asyncio
async def test_a_flush_that_cannot_connect_reports_why(monkeypatch):
    """Silent success is the failure mode this codebase keeps producing. A
    queue that will not drain must say so rather than look drained."""
    from python.harness import db

    async def fetch(query, *args):
        return [{"entry_id": "e1", "tx_id": "t1", "type": "payment",
                 "counterparty": "@a", "document": "", "metadata": "{}",
                 "occurred_at": None}]

    async def post(endpoint, transaction, guard=True):
        return {"written": False, "error": "ConnectError"}

    async def endpoint():
        return "https://chain.example"

    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(chains, "_post", post)
    monkeypatch.setattr(chains, "real_endpoint", endpoint)

    result = await chains.flush()

    assert result["flushed"] == 0
    assert result["remaining"] == 1
    assert result["error"]


@pytest.mark.asyncio
async def test_a_flush_stops_at_the_first_failure(monkeypatch):
    """If the chain is unreachable the rest will fail too."""
    from python.harness import db

    attempts = []

    async def fetch(query, *args):
        return [{"entry_id": f"e{n}", "tx_id": f"t{n}", "type": "payment",
                 "counterparty": "@a", "document": "", "metadata": "{}",
                 "occurred_at": None} for n in range(5)]

    async def post(endpoint, transaction, guard=True):
        attempts.append(transaction["id"])
        return {"written": False, "error": "down"}

    async def endpoint():
        return "https://chain.example"

    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(chains, "_post", post)
    monkeypatch.setattr(chains, "real_endpoint", endpoint)

    await chains.flush()
    assert attempts == ["t0"]


@pytest.mark.asyncio
async def test_nothing_is_submitted_without_an_endpoint(monkeypatch):
    """No endpoint configured is the correct state for an install that never
    goes online, not an error."""
    async def endpoint():
        return ""
    monkeypatch.setattr(chains, "real_endpoint", endpoint)

    result = await chains._submit({"id": "t1"})
    assert result["written"] is False
    assert "no Real chain endpoint" in result["error"]


# -- the guardrails -----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_real_chain_goes_through_the_network_guard(monkeypatch):
    from python.harness import network_guard as ng

    async def refuse(target, principal, purpose=""):
        return ng.NetDecision(False, "blocked by rule", "chain.example",
                              443, "deny:*", {})
    monkeypatch.setattr(ng, "check", refuse)

    result = await chains._post("https://chain.example", {"id": "t1"})
    assert result["written"] is False
    assert "blocked by rule" in result["error"]


@pytest.mark.asyncio
async def test_a_guard_that_cannot_be_consulted_does_not_permit_the_call(monkeypatch):
    from python.harness import network_guard as ng

    async def boom(*a, **k):
        raise RuntimeError("no database")
    monkeypatch.setattr(ng, "check", boom)

    result = await chains._post("https://chain.example", {"id": "t1"})
    assert result["written"] is False
    assert "guardrails unavailable" in result["error"]


@pytest.mark.asyncio
async def test_the_local_node_is_not_gated_by_the_guard(monkeypatch):
    """A firewall rule must not be able to sever the machine from its own
    audit trail. The Ghost node is loopback, which the guard exempts anyway."""
    from python.harness import network_guard as ng

    async def refuse(*a, **k):
        raise AssertionError("the Ghost node was sent through the guard")
    monkeypatch.setattr(ng, "check", refuse)

    async def endpoint():
        return "http://127.0.0.1:8332"
    monkeypatch.setattr(chains, "ghost_endpoint", endpoint)

    # reaches the POST rather than the guard; the connection itself fails
    result = await chains._post("http://127.0.0.1:8332", {"id": "t"}, guard=False)
    assert result["written"] is False
    assert "guardrails" not in result["error"]


@pytest.mark.asyncio
async def test_the_ghost_endpoint_is_loopback():
    endpoint = await chains.ghost_endpoint()
    assert "127.0.0.1" in endpoint or "localhost" in endpoint


# -- double-encoded JSONB -----------------------------------------------------

def test_metadata_encoded_more_than_once_still_decodes():
    """Every JSONB column in this database holds a JSON *string*. A single
    json.loads returns a string, and the caller reads an empty result from a
    row that was never empty."""
    assert chains._unwrap({"a": 1}) == {"a": 1}
    assert chains._unwrap(json.dumps({"a": 1})) == {"a": 1}
    assert chains._unwrap(json.dumps(json.dumps({"a": 1}))) == {"a": 1}
    assert chains._unwrap("not json") == {}
    assert chains._unwrap(None) == {}


# -- the call sites -----------------------------------------------------------

def test_token_counter_records_the_chain_it_used():
    """UI-023: the answer reached a logger.debug line and stopped there."""
    import ast
    import inspect

    from python.harness import token_counter

    source = inspect.getsource(token_counter.count_and_check)
    tree = ast.parse(inspect.cleandoc(source))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "record"]
    assert calls, "the payment is not recorded on a chain"

    keywords = {kw.arg for call in calls for kw in call.keywords}
    assert "external" in keywords, "the ghost/real decision is still discarded"


def test_node_anchoring_indexes_as_ghost():
    """The path that produced all 2,747 existing node-create transactions."""
    import ast
    import inspect

    from sdk.agent import anchor

    tree = ast.parse(inspect.getsource(anchor.canonicalize_and_anchor))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "index_sync"]
    assert calls, "anchored nodes are not indexed on either chain"


def test_the_rpcs_exist():
    from python.harness.server import _HANDLERS

    for method in ("chain.summary", "chain.transactions", "chain.flush",
                   "chain.set_endpoint"):
        assert method in _HANDLERS


def test_something_drains_the_queue_on_reconnect():
    """Queue-on-reconnect needs something that notices the reconnection."""
    import ast
    import inspect

    from python.harness import server

    tree = ast.parse(inspect.getsource(server._real_chain_flush_loop))
    assert any(isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute)
               and node.func.attr == "flush"
               for node in ast.walk(tree))
