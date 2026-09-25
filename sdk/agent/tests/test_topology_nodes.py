"""
test_topology_nodes.py — the HTML-NN *is* the swarm definition.

Covers the two halves of that claim: a topology written through the creation
path can be read back and run, and a topology whose node has been edited into
an invalid state is refused with a reason rather than half-registered.

Every test pins TALA_USER_DATA. resolve_active_nodes_dir() reads it, and
without it these tests would write topology nodes straight into the running
application's live nodes/ directory.
"""

from __future__ import annotations

import re

import pytest

from sdk.agent.topology import (
    TopologyRegistry,
    TopologySpecError,
    topology_from_spec,
)


@pytest.fixture(autouse=True)
def isolate_registry():
    """
    TopologyRegistry is process-global. Several tests here write, delete and
    reset it against a tmp_path nodes dir; without restoring it afterwards the
    registry would keep pointing at a deleted temp directory and leak into
    whatever test runs next.
    """
    saved = dict(TopologyRegistry._topologies)
    saved_loaded = TopologyRegistry._nodes_loaded
    yield
    TopologyRegistry._topologies = saved
    TopologyRegistry._nodes_loaded = saved_loaded


@pytest.fixture
def nodes_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    target = tmp_path / "nodes"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _spec(**overrides):
    base = {
        "topology_id": "test-audit",
        "name": "Test Audit Swarm",
        "nodes": [
            {"id": "scout", "role": "planner", "description": "Scope"},
            {"id": "left", "role": "researcher", "description": "Left"},
            {"id": "right", "role": "researcher", "description": "Right"},
            {"id": "reconcile", "role": "reviewer", "description": "Join"},
        ],
        "edges": [
            {"from_node": "scout", "to_node": "left", "condition": "success"},
            {"from_node": "scout", "to_node": "right", "condition": "success"},
            {"from_node": "left", "to_node": "reconcile", "condition": "success"},
            {"from_node": "right", "to_node": "reconcile", "condition": "success"},
        ],
        "failure_policy": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------


def test_valid_spec_round_trips_shape():
    topology = topology_from_spec(_spec())
    assert [n.id for n in topology.start_nodes()] == ["scout"]
    assert len(topology.outgoing_edges("scout")) == 2, "scout must fan out"
    assert len(topology.incoming_edges("reconcile")) == 2, "reconcile must be a join"


def test_short_edge_keys_are_accepted():
    """'from'/'to' is the terser authored form and must load identically."""
    spec = _spec(edges=[{"from": "scout", "to": "left", "condition": "success"}])
    topology = topology_from_spec(spec)
    assert topology.edges[0].from_node == "scout"
    assert topology.edges[0].to_node == "left"


def test_edge_to_unknown_node_is_rejected():
    spec = _spec(edges=[{"from_node": "scout", "to_node": "ghost", "condition": "success"}])
    with pytest.raises(TopologySpecError, match="unknown node 'ghost'"):
        topology_from_spec(spec)


def test_cycle_is_rejected():
    """
    The engine advances the graph by following edges, so a cycle in the edges
    is an unbounded run. Retries move backwards through failure_policy.route_to
    instead, which is bounded by max_retries.
    """
    spec = _spec(edges=[
        {"from_node": "scout", "to_node": "left", "condition": "success"},
        {"from_node": "left", "to_node": "right", "condition": "success"},
        {"from_node": "right", "to_node": "scout", "condition": "success"},
    ])
    with pytest.raises(TopologySpecError, match="cycle"):
        topology_from_spec(spec)


def test_unknown_gate_condition_is_rejected():
    """
    An edge gated on a rule GatedVerifier does not implement falls through to
    its `return False`, so that branch could never advance on any run — a
    swarm that silently stalls rather than failing.
    """
    spec = _spec(edges=[{"from_node": "scout", "to_node": "left", "condition": "vibes_ok"}])
    with pytest.raises(TopologySpecError, match="unknown gate condition 'vibes_ok'"):
        topology_from_spec(spec)


def test_every_builtin_edge_condition_is_implemented():
    """Guards against a built-in topology drifting away from the verifier."""
    from sdk.agent.gated_verifier import GatedVerifier

    for topology in TopologyRegistry.list_all():
        for edge in topology.edges:
            assert edge.condition in GatedVerifier.KNOWN_RULES, (
                f"{topology.topology_id}: edge {edge.from_node} -> {edge.to_node} is gated on "
                f"{edge.condition!r}, which GatedVerifier does not implement"
            )


def test_failure_policy_route_to_must_name_a_real_node():
    """
    The shipped code-repair topology used to route the 'missing_artifact' gate
    to a node called 'worker' that does not exist in it — a retry that could
    never land anywhere.
    """
    spec = _spec(failure_policy={"success": {"route_to": "worker", "max_retries": 2}})
    with pytest.raises(TopologySpecError, match="route_to references unknown node 'worker'"):
        topology_from_spec(spec)


def test_duplicate_node_ids_are_rejected():
    spec = _spec(nodes=[
        {"id": "scout", "role": "planner"},
        {"id": "scout", "role": "researcher"},
    ], edges=[])
    with pytest.raises(TopologySpecError, match="duplicate node id"):
        topology_from_spec(spec)


# ---------------------------------------------------------------------------
# HTML-NN persistence
# ---------------------------------------------------------------------------


def test_created_swarm_is_written_as_a_node_and_loads_back(nodes_dir):
    """
    Creating a swarm writes an HTML-NN, and that file — not any in-memory
    registration — is what a later load reads. This is the whole contract: a
    topology that ran once is on disk to be run again.
    """
    from sdk.agent.parser import parse_file
    from sdk.agent.topology_nodes import load_topology_nodes, write_topology_node

    topology = topology_from_spec(_spec())
    result = write_topology_node(topology)

    assert result["created"] is True
    written = nodes_dir / f"{result['node_id']}.html"
    assert written.exists(), "creating a swarm must leave a node file behind"

    parsed = parse_file(str(written))
    assert parsed.node_type == "swarm-topology"
    assert parsed.state_data["topology"]["topology_id"] == "test-audit"

    loaded = {t.topology_id: t for t in load_topology_nodes()}
    assert "test-audit" in loaded
    reloaded = loaded["test-audit"]
    assert reloaded.source_node_id == result["node_id"]
    assert [e.to_node for e in reloaded.incoming_edges("reconcile")] == ["reconcile", "reconcile"]
    assert {e.from_node for e in reloaded.incoming_edges("reconcile")} == {"left", "right"}


def test_written_node_carries_real_prose(nodes_dir):
    """
    An auto-created topology node still has to be a memory, not a config file
    in an HTML costume — the wiki's doc-comparison heuristics treat an empty
    or placeholder #semantic-memory as a defect.
    """
    from sdk.agent.topology_nodes import _render_topology_node

    _, html = _render_topology_node(topology_from_spec(_spec()))
    body = re.search(r'<section id="semantic-memory">(.*?)</section>', html, re.S)
    assert body, "node must have a #semantic-memory section"
    text = re.sub(r"<[^>]+>", " ", body.group(1))
    assert len(text.split()) > 60, "semantic memory must be real prose, not a stub"
    assert "reconcile" in text, "the prose must describe this swarm, not a generic one"
    assert "join" in text.lower(), "the prose must explain the join, since that is what makes it a swarm"


def test_hand_edited_invalid_node_is_skipped_not_half_registered(nodes_dir, caplog):
    """
    A bad edit must revert the shape, not break the registry. The loader skips
    the node and logs why, leaving any previously-known definition in place.
    """
    from sdk.agent.topology_nodes import load_topology_nodes, write_topology_node

    result = write_topology_node(topology_from_spec(_spec()))
    path = nodes_dir / f"{result['node_id']}.html"

    html = path.read_text(encoding="utf-8")
    broken = html.replace('"to_node": "reconcile"', '"to_node": "does-not-exist"')
    assert broken != html, "test fixture did not actually break the spec"
    path.write_text(broken, encoding="utf-8")

    with caplog.at_level("WARNING"):
        loaded = {t.topology_id for t in load_topology_nodes()}

    assert "test-audit" not in loaded, "an invalid topology must not be registered"
    assert "Rejecting topology node" in caplog.text
    assert "does-not-exist" in caplog.text, "the rejection reason must name the offending node"


def test_write_is_rejected_before_it_can_persist_an_unrunnable_spec(nodes_dir):
    """Validation happens on the way in, so a broken spec never reaches disk."""

    with pytest.raises(TopologySpecError):
        topology_from_spec(_spec(edges=[{"from_node": "scout", "to_node": "ghost"}]))

    assert not list(nodes_dir.glob("swarm-topology-*.html")), "nothing should have been written"


def test_persona_defaults_to_none_and_is_omitted_when_unset():
    """
    No persona is the default. An unset node must not serialise `agent_id` at
    all — a spec full of `"agent_id": null` claims involvement with a feature
    the swarm does not use.
    """
    topology = topology_from_spec(_spec())
    assert all(n.agent_id is None for n in topology.nodes)
    assert all("agent_id" not in n for n in topology.to_dict()["nodes"])


def test_persona_round_trips_through_the_node(nodes_dir):
    from sdk.agent.topology_nodes import load_topology_nodes, write_topology_node

    spec = _spec()
    spec["nodes"][1]["agent_id"] = "agent-zara-9f2c"
    write_topology_node(topology_from_spec(spec))

    loaded = {t.topology_id: t for t in load_topology_nodes()}["test-audit"]
    assert loaded.node("left").agent_id == "agent-zara-9f2c"
    assert loaded.node("right").agent_id is None, "personas are per node, not inherited"


def test_blank_persona_is_normalised_to_none():
    """The editor's "none" option submits an empty string."""
    spec = _spec()
    spec["nodes"][0]["agent_id"] = ""
    spec["nodes"][1]["agent_id"] = "   "
    topology = topology_from_spec(spec)
    assert topology.node("scout").agent_id is None
    assert topology.node("left").agent_id is None


def test_authored_prose_survives_a_round_trip(nodes_dir):
    """
    Editing a swarm must not silently replace what the author wrote about it
    with generated text — the editor reloads this to prefill its description
    box, so losing it here loses it for good.
    """
    from sdk.agent.topology_nodes import load_topology_nodes, write_topology_node

    prose = (
        "This swarm exists to settle an argument about the ingest path.\n\n"
        "It deliberately splits the work three ways so the disagreements surface."
    )
    write_topology_node(topology_from_spec(_spec()), semantic_memory=prose)

    loaded = {t.topology_id: t for t in load_topology_nodes()}["test-audit"]
    assert "settle an argument" in loaded.semantic_memory
    assert "disagreements surface" in loaded.semantic_memory


def test_to_wire_annotations_never_leak_into_the_saved_spec(nodes_dir):
    """
    `to_wire` adds where-it-came-from fields for the editor. Those are load-time
    annotations: writing them back into #node-state would make the file claim
    to define things the spec has no business defining.
    """
    from sdk.agent.parser import parse_file
    from sdk.agent.topology_nodes import write_topology_node

    result = write_topology_node(topology_from_spec(_spec()), semantic_memory="Prose.")
    spec = parse_file(str(nodes_dir / f"{result['node_id']}.html")).state_data["topology"]

    for annotation in ("source_node_id", "semantic_memory", "editable"):
        assert annotation not in spec, f"{annotation} must not be written into the authored spec"


def test_deleting_a_swarm_removes_its_node(nodes_dir):
    """Delete is a real deletion of the definition, not just an unregistration."""
    from sdk.agent.topology import TopologyRegistry
    from sdk.agent.topology_nodes import delete_topology_node, write_topology_node

    result = write_topology_node(topology_from_spec(_spec()))
    path = nodes_dir / f"{result['node_id']}.html"
    assert path.exists()

    outcome = delete_topology_node("test-audit")

    assert outcome["node_deleted"] is True
    assert not path.exists(), "the neural node must be gone from disk"
    assert TopologyRegistry.get("test-audit") is None, "and dropped from the registry"
    assert outcome["reverted_to_builtin"] is False


def test_deleting_a_topology_with_a_builtin_says_it_came_back(nodes_dir):
    """
    Deleting an *edited* built-in reverts it rather than removing it, because
    topology.py still registers the fallback. Reporting a clean delete would be
    a lie the user can see through the moment the list refreshes.
    """
    from sdk.agent.topology import TopologyRegistry
    from sdk.agent.topology_nodes import delete_topology_node, write_topology_node

    builtin = TopologyRegistry.get("swarm-code-repair")
    assert builtin is not None, "fixture assumes the code-repair built-in exists"

    edited = topology_from_spec({**builtin.to_dict(), "name": "Edited Code Repair"})
    write_topology_node(edited)
    assert TopologyRegistry.get("swarm-code-repair").name == "Edited Code Repair"

    outcome = delete_topology_node("swarm-code-repair")

    assert outcome["node_deleted"] is True
    assert outcome["reverted_to_builtin"] is True
    assert TopologyRegistry.get("swarm-code-repair").name == "Code Repair Swarm", (
        "deleting the edit must restore the built-in shape"
    )


def test_reset_drops_topologies_whose_node_is_gone(nodes_dir):
    """
    `reload` only overwrites, so it can never remove an entry. Delete and
    rename both need `reset`, or a swarm keeps appearing in the list after its
    node is gone.
    """
    from sdk.agent.topology import TopologyRegistry
    from sdk.agent.topology_nodes import write_topology_node

    result = write_topology_node(topology_from_spec(_spec()))
    TopologyRegistry.reset()
    assert TopologyRegistry.get("test-audit") is not None

    (nodes_dir / f"{result['node_id']}.html").unlink()

    TopologyRegistry.reload()
    assert TopologyRegistry.get("test-audit") is not None, "reload alone cannot remove — that is the point"

    TopologyRegistry.reset()
    assert TopologyRegistry.get("test-audit") is None


def test_rewriting_the_same_topology_is_idempotent(nodes_dir):
    from sdk.agent.topology_nodes import write_topology_node

    topology = topology_from_spec(_spec())
    first = write_topology_node(topology, semantic_memory="Stable prose for this swarm.")
    second = write_topology_node(topology, semantic_memory="Stable prose for this swarm.")

    assert first["created"] is True
    assert second["unchanged"] is True, "an unchanged topology must not churn the node file"
