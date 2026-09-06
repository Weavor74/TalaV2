"""
What the chain records when two installs interact.

The intended ledger reads "@alice requested node:doc66666 from @bob", not
"@alice called @bob". Before this, `chains.record` had exactly two callers
in the repo -- token_counter for payments, and the relay hop -- so almost
none of a peer exchange left a trace.

Two rules run through all of it, and neither is new here:

**Ghost never publishes to Real.** A node copied locally stays node-create
on Ghost. What belongs on the shared chain is the interaction, not the
document, so these kinds are declared external and carry a reference only.

**A refusal is the interesting half.** "@alice was denied node:doc66666" is
the line somebody comes looking for, so the record exists whether or not
the request was permitted.
"""

from __future__ import annotations

import pytest

from python.harness import chains

PEER_EVENT_KINDS = (
    "peer-document", "document-shared", "peer-index", "index-shared",
    "relay", "peer-contact",
)


@pytest.fixture
def recorded(monkeypatch):
    """Capture what would be anchored, without touching a chain."""
    calls = []

    async def fake_record(kind, payload=None, counterparty="", document_ref="", external=None):
        calls.append({
            "kind": kind,
            "payload": payload or {},
            "counterparty": counterparty,
            "document_ref": document_ref,
        })
        return {"chains": chains.classify(kind, counterparty)}

    monkeypatch.setattr(chains, "record", fake_record)
    return calls


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_peer_exchange_reaches_the_shared_chain():
    """
    These are interactions with a second party, so they are Real as well as
    Ghost. Relying on the counterparty fallback would have worked and left
    the intent undeclared.
    """
    for kind in PEER_EVENT_KINDS:
        assert chains.is_external(kind), f"{kind} would stay local"
        assert chains.classify(kind, "@bob") == [chains.GHOST, chains.REAL]


def test_a_document_that_never_left_stays_local():
    """
    The copy A makes of C's document is still node-create, and node-create
    is Ghost-only by an existing deliberate decision. Publishing local
    document history to a chain everybody can read is the failure this
    guards.
    """
    for kind in ("node-create", "node-update", "node-delete"):
        assert not chains.is_external(kind)
        assert chains.classify(kind, "@bob") == [chains.GHOST], (
            "a stray counterparty dragged local document activity onto the shared chain"
        )


# ---------------------------------------------------------------------------
# The events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serving_a_document_names_the_document(recorded):
    from python.harness import server

    await server._record_peer_event(
        "document-shared", "@alice", "node:doc66666", outcome="served",
    )

    (event,) = recorded
    assert event["kind"] == "document-shared"
    assert event["counterparty"] == "@alice"
    assert event["document_ref"] == "node:doc66666"
    assert event["payload"]["outcome"] == "served"


@pytest.mark.asyncio
async def test_a_refusal_is_recorded_too(recorded):
    from python.harness import server

    await server._record_peer_event(
        "document-shared", "@alice", "node:private", outcome="denied", reason="firewall_denied",
    )

    (event,) = recorded
    assert event["payload"]["outcome"] == "denied"
    assert event["payload"]["reason"] == "firewall_denied"
    assert event["document_ref"] == "node:private"


@pytest.mark.asyncio
async def test_both_ends_of_one_exchange_are_separate_facts(recorded):
    """
    Each install keeps its own ledger, and either end may be the one asked
    to account for the exchange.
    """
    from python.harness import server

    await server._record_peer_event("peer-document", "@bob", "node:doc1", outcome="received")
    await server._record_peer_event("document-shared", "@alice", "node:doc1", outcome="served")

    assert [e["kind"] for e in recorded] == ["peer-document", "document-shared"]
    assert recorded[0]["counterparty"] == "@bob"
    assert recorded[1]["counterparty"] == "@alice"


@pytest.mark.asyncio
async def test_the_ledger_never_takes_down_the_operation(monkeypatch):
    """
    A record that can fail the request it is recording is worse than a gap
    in the record.
    """
    from python.harness import server

    async def exploding_record(*args, **kwargs):
        raise RuntimeError("the chain is down")

    monkeypatch.setattr(chains, "record", exploding_record)
    await server._record_peer_event("document-shared", "@alice", "node:doc1")


# ---------------------------------------------------------------------------
# References, never content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_search_records_its_shape_and_not_its_words(recorded):
    """
    The query is the peer's own words and the Real chain is shared. What
    makes it auditable is who searched and how much they saw.
    """
    from python.harness import server

    await server._record_peer_event(
        "index-shared", "@alice", "query:27chars", visible=2, withheld=5,
    )

    (event,) = recorded
    assert event["document_ref"] == "query:27chars"
    assert event["payload"]["visible"] == 2
    assert event["payload"]["withheld"] == 5

    serialized = repr(event)
    assert "my private" not in serialized


@pytest.mark.asyncio
async def test_no_node_body_can_reach_a_peer_event(recorded):
    """
    The handlers pass a reference built from the node name. Nothing in the
    recording path takes a body, so there is no argument through which one
    could arrive.
    """
    import inspect

    from python.harness import server

    signature = inspect.signature(server._record_peer_event)
    assert list(signature.parameters) == ["kind", "counterparty", "document_ref", "facts"]

    src = inspect.getsource(server.handle_p2p_resolve_node_reference)
    assert "_record_peer_event" in src
    assert 'resolved["node"]' not in src.split("_record_peer_event")[1].split("return")[0], (
        "a node body was passed into the chain record"
    )


@pytest.mark.asyncio
async def test_a_contact_request_is_recorded_before_any_decision(recorded):
    """
    'When did @alice first turn up' is where an audit starts, and a contact
    that appears already approved leaves no trace of having asked.
    """
    from python.harness import server

    await server._record_peer_event(
        "peer-contact", "@alice", "contact:@alice",
        decision="requested", requested=["nodes"],
    )

    (event,) = recorded
    assert event["payload"]["decision"] == "requested"
    assert event["payload"]["requested"] == ["nodes"]


@pytest.mark.asyncio
async def test_a_decision_records_which_permissions_not_what_they_unlock(recorded):
    from python.harness import server

    await server._record_contact_decision(
        "@alice", "approved", {"nodes": True, "identity": False, "_requested": {"nodes": True}},
    )

    (event,) = recorded
    assert event["kind"] == "peer-contact"
    assert event["payload"]["granted"] == ["nodes"]
    assert "_requested" not in event["payload"]["granted"], (
        "the peer's own request was recorded as if it were a grant"
    )
