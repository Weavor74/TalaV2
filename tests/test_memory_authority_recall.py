"""
Node recall authority.

Activation is a pure product, and the engine's own docstring calls governance
"a hard gate: setting it to 0.0 blocks activation entirely". Documentation nodes
are correctly generated with execution_authority 0.00 -- a document must never
execute anything -- and the same 0.00 was copied into memory_authority, which
does not mean "cannot act" but "cannot be recalled".

94 of 115 nodes were therefore unrecallable at any similarity. Retrieval never
errored; it answered every query from the ~18% of the graph that had authority,
which is mostly settings. "How do I write an HTML neural node" returned
settings-code-registry at 0.26 similarity while system-html-nn-authoring-guide,
at 0.43, scored exactly zero.
"""
import re
from pathlib import Path

import pytest

from sdk.agent.activation import ActivationEngine, ActivationInputs
from sdk.agent.parser import parse_file

NODES_DIR = Path("nodes")
GENERATORS = (
    Path("sdk/agent/documentation_nodes.py"),
    Path("sdk/agent/function_documenter.py"),
    Path("sdk/agent/wiki.py"),
)
_MEM_RE = re.compile(r'data-memory-authority="([^"]*)"')
_EXEC_RE = re.compile(r'data-execution-authority="([^"]*)"')

# workflow-self-extension is a workflow, not memory: it is executed by the DAG
# engine rather than recalled into a prompt, so zero recall authority is correct.
EXPECTED_UNRECALLABLE = {"workflow-self-extension"}


@pytest.fixture(scope="module")
def nodes():
    parsed = []
    for path in sorted(NODES_DIR.glob("*.html")):
        try:
            parsed.append(parse_file(str(path)))
        except Exception:
            pass
    assert parsed, "no nodes parsed"
    return parsed


def _governance(node):
    """The gate the retrieval path applies."""
    return max(node.execution_authority, node.memory_authority)


# --- the graph can remember itself ------------------------------------------


def test_documentation_nodes_can_be_recalled(nodes):
    """The regression itself. These nodes exist only to be retrieved; zero
    governance makes that impossible, silently."""
    mute = [n.node_id for n in nodes
            if n.node_type in ("documentation", "function-doc", "specification")
            and _governance(n) == 0.0
            and n.node_id not in EXPECTED_UNRECALLABLE]

    assert not mute, f"{len(mute)} node(s) can never be injected at any similarity: {mute[:8]}"


def test_almost_nothing_in_the_graph_is_unrecallable(nodes):
    """A broad backstop: if this count climbs, some generator is writing zero
    recall authority again and the graph is quietly going mute."""
    mute = {n.node_id for n in nodes if _governance(n) == 0.0}

    assert mute <= EXPECTED_UNRECALLABLE, f"unexpectedly unrecallable: {sorted(mute - EXPECTED_UNRECALLABLE)}"


def test_documentation_still_cannot_execute(nodes):
    """The zero that was always correct. Recall authority was granted; execution
    authority must stay at zero or a document could act."""
    for node in nodes:
        if node.node_type in ("documentation", "function-doc"):
            assert node.execution_authority == 0.0, f"{node.node_id} can execute"


def test_recall_authority_stays_below_live_settings(nodes):
    """Documentation should be recallable without outranking current
    configuration, or the model answers from docs about settings rather than
    from the settings themselves."""
    docs = [n for n in nodes if n.node_type in ("documentation", "function-doc") and n.memory_authority > 0]
    assert docs
    for node in docs:
        assert node.memory_authority < 1.0, f"{node.node_id} outranks live settings nodes"


# --- generators ------------------------------------------------------------


@pytest.mark.parametrize("generator", GENERATORS, ids=lambda p: p.name)
def test_generators_emit_recallable_nodes(generator):
    """Backfilling the 93 existing nodes is worthless if the next generated node
    is mute again."""
    source = generator.read_text(encoding="utf-8")
    authorities = _MEM_RE.findall(source)

    assert authorities, f"{generator} emits no data-memory-authority"
    for value in authorities:
        assert float(value) > 0.0, f"{generator} still generates unrecallable nodes"


@pytest.mark.parametrize("generator", GENERATORS, ids=lambda p: p.name)
def test_generators_still_withhold_execution_authority(generator):
    """Scoped to the document templates specifically: wiki.py also emits a
    `resource` node at 0.25, which is a different kind of thing and legitimately
    allowed to act. What must never happen is a document granted both."""
    source = generator.read_text(encoding="utf-8")

    paired = re.findall(
        r'data-execution-authority="([^"]*)"\s*\n\s*data-memory-authority="([^"]*)"', source
    )
    assert paired, f"{generator} has no document template with both authorities"
    for execution, memory in paired:
        assert float(execution) == 0.0, f"{generator} grants execution authority to a document"
        assert float(memory) > 0.0, f"{generator} still generates unrecallable nodes"


# --- the gate itself --------------------------------------------------------


def test_zero_governance_annihilates_a_perfect_match():
    """Documents the mechanism, so nobody has to rediscover that a strong
    similarity can score zero."""
    engine = ActivationEngine()
    inputs = ActivationInputs(
        semantic_match=1.0, relevance_weight=1.0, confidence_weight=1.0,
        freshness_weight=1.0, provider_trust_multiplier=1.0, governance_multiplier=0.0,
    )

    assert engine.score(inputs) == 0.0


def test_recall_authority_alone_is_enough_to_activate():
    """A document has no execution authority, so recall has to come from the
    memory side of the gate or the grant does nothing."""
    engine = ActivationEngine()
    inputs = ActivationInputs(
        semantic_match=0.5, relevance_weight=1.0, confidence_weight=1.0,
        freshness_weight=1.0, provider_trust_multiplier=1.0,
        governance_multiplier=max(0.0, 0.60),
    )

    assert engine.score(inputs) > 0.01


def test_reported_activation_matches_the_gate_recall_uses():
    """graph.query_activations is the instrument for debugging what got
    recalled. It reported execution_authority alone while retrieval gated on
    max(execution, memory), so it disagreed with the thing it describes."""
    source = Path("python/harness/server.py").read_text(encoding="utf-8")

    # The retrieval path assigns it to gov_mult first; the reporting path passes
    # it inline. Match the expression itself rather than either spelling.
    assert source.count(
        "max(node.execution_authority, node.memory_authority)"
    ) >= 2, "the reporting path and the retrieval path use different gates"


def test_gated_but_matching_nodes_are_logged():
    """A node that matches well and scores zero must say so. Silence here is
    what let 94 nodes stay unreachable without anything noticing."""
    source = Path("python/harness/server.py").read_text(encoding="utf-8")

    assert "they can never be recalled" in source
    assert "backfill_memory_authority" in source
