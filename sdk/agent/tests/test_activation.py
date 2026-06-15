from __future__ import annotations

import pytest

from sdk.agent.activation import ActivationEngine, ActivationInputs
from sdk.agent.exceptions import AuthorityBoundaryViolation
from sdk.agent.parser import NeuralNode

@pytest.fixture
def dummy_node():
    return NeuralNode(
        node_id="test_node",
        node_type="concept",
        governance_status="context-only",
        execution_authority=0.8,
        user_approval_strength=1.0,
        weight_relevance=0.9,
        weight_confidence=0.9,
        weight_behavior_influence=0.0,
        weight_governance_importance=0.0,
        memory_authority=0.5,
        provider_authority=1.0,
        wallet_authority=0.0,
        semantic_memory="Memory",
        relationships=[],
        state_data={}
    )

def test_activation_inputs_clamped():
    engine = ActivationEngine()
    inputs = ActivationInputs(
        semantic_match=1.5,
        relevance_weight=-0.5,
        confidence_weight=0.8,
        freshness_weight=2.0,
        provider_trust_multiplier=1.0,
        governance_multiplier=1.0
    )
    score = engine.score(inputs)
    # semantic_match becomes 1.0, relevance becomes 0.0, so product is 0.0
    assert score == 0.0

def test_activation_formula():
    engine = ActivationEngine()
    inputs = ActivationInputs(
        semantic_match=0.9,
        relevance_weight=0.8,
        confidence_weight=0.95,
        freshness_weight=0.7,
        provider_trust_multiplier=1.0,
        governance_multiplier=1.0
    )
    score = engine.score(inputs)
    expected = 0.9 * 0.8 * 0.95 * 0.7 * 1.0 * 1.0
    assert score == pytest.approx(expected)

def test_propagation():
    engine = ActivationEngine(decay_factor=0.85)
    propagated = engine.propagate(source_activation=0.9, relationship_weight=0.8)
    expected = 0.9 * 0.8 * 0.85
    assert propagated == pytest.approx(expected)

def test_authority_boundary_violation(dummy_node):
    engine = ActivationEngine()
    with pytest.raises(AuthorityBoundaryViolation):
        engine.enforce_authority_boundary(dummy_node, new_activation=0.9)

def test_score_from_node(dummy_node):
    engine = ActivationEngine()
    score = engine.score_from_node(
        dummy_node,
        semantic_match=0.9,
        confidence_weight=0.95,
        freshness_weight=0.8
    )
    # node.weight_relevance = 0.9
    # node.execution_authority = 0.8 (used as governance multiplier)
    expected = 0.9 * 0.9 * 0.95 * 0.8 * 1.0 * 0.8
    assert score == pytest.approx(expected)
