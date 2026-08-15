"""
The node documenting how activation and weights actually work.

Tala answered "how do the html-nn weights work" by inventing training that never
happened and model-selection the weights have nothing to do with. Ranking was
not the cause: the node describing ActivationEngine ranked 9th for that question
AND did not contain the formula. The mechanism was absent from the graph, so she
supplied one.

sdk_agent_* nodes are AST-derived -- they carry function and class names, not
behaviour. This node carries behaviour, and these tests keep it honest: a
document that drifts from the code is worse than no document, because it answers
confidently.
"""
import json
import re
from pathlib import Path

import pytest

from sdk.agent.activation import _ACTIVATION_EPSILON, ActivationEngine
from sdk.agent.parser import parse_file

NODE_PATH = Path("nodes/system-html-nn-activation-weights.html")
SERVER = Path("python/harness/server.py")


@pytest.fixture(scope="module")
def node():
    assert NODE_PATH.exists(), "the activation-weights node is missing"
    return parse_file(str(NODE_PATH))


@pytest.fixture(scope="module")
def config(node):
    return node.state_data["config"] if node.state_data else {}


# --- the node is a valid, recallable document -------------------------------


def test_node_is_recallable_and_cannot_execute(node):
    assert node.memory_authority > 0.0, "a document that cannot be recalled answers nothing"
    assert node.execution_authority == 0.0, "documentation must never be able to act"


def test_node_is_typed_as_documentation(node):
    assert node.node_type == "documentation"


# --- the documented constants match the code --------------------------------


def test_decay_factor_matches_the_engine(config):
    assert config["decay_factor"] == ActivationEngine()._decay_factor


def test_activation_epsilon_matches_the_engine(config):
    assert config["activation_epsilon"] == _ACTIVATION_EPSILON


def test_injection_threshold_matches_the_retrieval_path(config):
    """server.py filters activations below this before sorting."""
    source = SERVER.read_text(encoding="utf-8")

    assert f"score >= {config['injection_threshold']}" in source


def test_injected_node_counts_match_the_chat_mode_branch(config):
    """max_injected_nodes = 2 if chat_mode in ("rp", "hybrid") else 5"""
    source = SERVER.read_text(encoding="utf-8")
    match = re.search(
        r"max_injected_nodes = (\d+) if chat_mode in \(\"rp\", \"hybrid\"\) else (\d+)", source
    )

    assert match, "the chat-mode injection branch has moved"
    assert config["injected_node_count"]["rp"] == int(match.group(1))
    assert config["injected_node_count"]["assistant"] == int(match.group(2))


def test_propagation_hop_count_matches_the_loop(config):
    source = SERVER.read_text(encoding="utf-8")

    assert f"for _ in range({config['propagation_hops']})" in source


def test_documented_activation_formula_matches_the_engine(config):
    """Every factor named in the node must be a real argument to score()."""
    import inspect

    signature = inspect.signature(ActivationEngine.score)
    documented = {t.strip() for t in config["activation_formula"].split("*")}
    from sdk.agent.activation import ActivationInputs

    fields = set(ActivationInputs.__dataclass_fields__)

    assert "inputs" in signature.parameters
    assert documented - {"semantic_match"} <= fields, (
        f"the node names factors the engine does not take: {documented - fields}"
    )
    assert fields <= documented, f"the engine takes factors the node omits: {fields - documented}"


def test_governance_is_documented_as_a_gate(config):
    """It was changed from a ranking multiplier to a gate; the node has to say
    which, because that is the difference between a doc outranking a setting and
    not."""
    source = SERVER.read_text(encoding="utf-8")

    assert config["governance_is_a_gate"] is True
    assert "1.0 if max(node.execution_authority, node.memory_authority) > 0.0 else 0.0" in source


# --- the known defect stays documented until it is fixed --------------------


def test_the_double_weighting_defect_is_recorded(node):
    """retrieve_relevant_nodes returns raw_similarity * relevance * confidence,
    and server.py passes that as semantic_match while ALSO passing both weights
    to score() -- so they act as squares. Masked while nearly every node sits at
    1.00, and it makes weight_relevance behave surprisingly the moment anyone
    uses it for priority."""
    retrieval = Path("python/harness/retrieval.py").read_text(encoding="utf-8")
    server = SERVER.read_text(encoding="utf-8")
    still_doubled = (
        "final_score = raw_similarity * weight_relevance * weight_confidence" in retrieval
        and "relevance_weight=node.weight_relevance" in server
    )

    metadata = node.state_data.get("metadata", {}) if node.state_data else {}
    if still_doubled:
        assert "known_defect" in metadata, (
            "the weights are still applied twice but the node no longer says so"
        )
        assert "twice" in metadata["known_defect"] or "square" in metadata["known_defect"]
    else:
        assert "known_defect" not in metadata, (
            "the double weighting was fixed; remove it from the node so the "
            "documentation stops describing a defect that is gone"
        )


def test_node_states_that_weights_are_not_learned(config):
    """The original wrong answer. LoRA adapters are a separate mechanism and
    every one on disk is a placeholder."""
    assert config["weights_are_learned"] is False


def test_node_lists_the_answers_that_were_confabulated(node):
    # Inline markup (<em>not</em>) leaves extra whitespace once stripped, so
    # collapse it rather than matching the parser's spacing.
    lowered = re.sub(r"\s+", " ", node.semantic_memory.lower())

    for claim in ("not learned", "do not select", "never labels"):
        assert claim in lowered, f"the node no longer corrects: {claim!r}"
