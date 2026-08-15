"""
Getting Tala right about herself.

Asked how her own weights work, she answered that they are "learned through
iterative training on the Tala Training Stories corpus" and that they select
inference providers. Both invented. Two separate causes, and only one of them is
ranking:

  ranking   settings-runtime (similarity 0.29, authority 1.00) tied
            program-doc-html-nn-runtime (similarity 0.49, authority 0.60) and
            took one of only two RP-mode slots, because ActivationEngine
            multiplies authority in and a node's permission to be recalled
            doubled as its priority once recalled.

  coverage  the node documenting ActivationEngine ranked 9th for that question
            and does not contain the formula either. The mechanism is absent
            from the graph, so no ranking would have produced a right answer.

Coverage is a larger job. What is achievable now is that an absent mechanism
produces an honest gap instead of a confident reconstruction.
"""
import re
from pathlib import Path

from sdk.agent.activation import ActivationEngine, ActivationInputs

SERVER = Path("python/harness/server.py")


# --- governance gates, it does not rank -------------------------------------


def test_retrieval_passes_governance_as_a_gate_not_a_weight():
    source = SERVER.read_text(encoding="utf-8")

    assert "gov_mult = 1.0 if max(node.execution_authority, node.memory_authority) > 0.0 else 0.0" in source, (
        "authority must be reduced to the hard gate the engine documents, not "
        "multiplied into the score as a priority"
    )


def test_the_reporting_endpoint_gates_the_same_way():
    """graph.query_activations is what you would reach for to debug recall; it
    has to describe the recall that actually happens."""
    source = SERVER.read_text(encoding="utf-8")
    gates = re.findall(
        r"1\.0 if max\(node\.execution_authority, node\.memory_authority\) > 0\.0 else 0\.0", source
    )

    assert len(gates) >= 2, "the retrieval and reporting paths use different gates"


def test_zero_authority_still_blocks_completely():
    """Reducing authority to a gate must not weaken the gate."""
    engine = ActivationEngine()
    blocked = engine.score(ActivationInputs(
        semantic_match=1.0, relevance_weight=1.0, confidence_weight=1.0,
        freshness_weight=1.0, provider_trust_multiplier=1.0, governance_multiplier=0.0,
    ))

    assert blocked == 0.0


def test_authority_no_longer_separates_two_equally_authorised_nodes():
    """The defect: a 0.60 node needed 1.67x the relevance to draw with a 1.00
    node. Past the gate, only relevance and the node's own weights count."""
    engine = ActivationEngine()

    def score(similarity):
        return engine.score(ActivationInputs(
            semantic_match=similarity, relevance_weight=1.0, confidence_weight=1.0,
            freshness_weight=1.0, provider_trust_multiplier=1.0, governance_multiplier=1.0,
        ))

    # program-doc-html-nn-runtime vs settings-runtime, real measured similarities
    assert score(0.4874) > score(0.2925)


def test_ranking_fields_exist_for_priority_if_it_is_ever_wanted():
    """weight_relevance and weight_confidence are the schema's own priority
    fields and are still passed through, so 'settings should outrank docs' stays
    expressible -- just not by borrowing the authority field to say it."""
    source = SERVER.read_text(encoding="utf-8")

    assert "relevance_weight=node.weight_relevance" in source
    assert "confidence_weight=node.weight_confidence" in source


# --- grounding ---------------------------------------------------------------


def test_grounding_ships_with_every_injected_node_block():
    source = SERVER.read_text(encoding="utf-8")

    assert "_SELF_KNOWLEDGE_GROUNDING" in source
    assert "context_parts.append(_SELF_KNOWLEDGE_GROUNDING)" in source


def test_grounding_asks_for_an_honest_gap_not_a_reconstruction():
    from python.harness.server import _SELF_KNOWLEDGE_GROUNDING as grounding

    lowered = grounding.lower()
    assert "not in the memory you can see" in lowered
    assert "offer to read the source" in lowered
    # The failure was filling a gap with what would plausibly be true.
    assert "plausibly" in lowered


def test_grounding_is_scoped_to_her_own_internals():
    """A blanket 'only say what is in context' would make her refuse ordinary
    conversation, and a directive that fires on everything gets ignored."""
    from python.harness.server import _SELF_KNOWLEDGE_GROUNDING as grounding

    lowered = grounding.lower()
    assert "your own internals only" in lowered
    assert "unaffected" in lowered


def test_grounding_is_also_restated_at_the_tail_of_the_turn():
    """Placement, not wording. With the directive only in the system prompt the
    4B still invented data-execution-authority="high" and a priority pipeline.
    Restated directly above its own turn, it answered "not explicitly detailed
    in the nodes above" instead -- the same asymmetry that put
    _XML_CALL_REMINDER here."""
    source = SERVER.read_text(encoding="utf-8")

    assert "_SELF_KNOWLEDGE_REMINDER" in source
    assert 'prompt = f"{prompt}\\n\\n{_SELF_KNOWLEDGE_REMINDER}"' in source


def test_the_tool_protocol_stays_last():
    """The tail position is scarce. Grounding goes above the tool reminder so
    the protocol the model must emit is still the closest thing to its turn."""
    source = SERVER.read_text(encoding="utf-8")

    assert source.index("_SELF_KNOWLEDGE_REMINDER}\"") < source.index("_XML_CALL_REMINDER}\"")


def test_self_referential_detection_matches_questions_about_her_internals():
    from python.harness.server import _is_self_referential

    for prompt in (
        "how does the html-nn weights work?",
        "how do your nodes get activated",
        "what is memory authority",
        "explain the lora adapters",
        "how does the heartbeat scheduler work",
    ):
        assert _is_self_referential(prompt), f"missed: {prompt!r}"


def test_ordinary_conversation_is_not_flagged():
    """A false positive costs only a sentence, but hedging about things she has
    no need to hedge about is still a cost."""
    from python.harness.server import _is_self_referential

    for prompt in (
        "How are you feeling today?",
        "what do you think about docker as a way to run services?",
        "Search the web for the Webb telescope status",
        "write me a function that reverses a string",
    ):
        assert not _is_self_referential(prompt), f"false positive: {prompt!r}"


def test_detection_uses_word_boundaries():
    """Bare substring gates have short-circuited turns here three times --
    'doc' inside 'docker', 'hi' inside 'everything'."""
    from python.harness.server import _is_self_referential

    # 'activation' must not fire on 'deactivated tenant', 'weight' not on
    # 'weightlifting', via the \b anchors.
    assert not _is_self_referential("tell me about weightlifting")
    assert not _is_self_referential("the loraine account")


def test_grounding_names_the_mechanisms_that_were_confabulated():
    """weights and activation specifically -- those are what she invented."""
    from python.harness.server import _SELF_KNOWLEDGE_GROUNDING as grounding

    lowered = grounding.lower()
    for mechanism in ("weights", "activation", "memory", "nodes"):
        assert mechanism in lowered, f"grounding does not cover {mechanism}"
