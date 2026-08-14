"""
era_availability in the emotion vocabulary node.

This map was destroyed by the 2026-08-11 rollback and re-authored, not
recovered. It is now derived from a stated rule rather than stored as four
hand-written lists, so the invented part is small enough to argue with. These
tests guard the two ways it has actually broken: drifting out of step with the
rule, and going narrow enough that the generator cannot build an arc.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path("scripts").resolve()))

from scripts.derive_era_availability import (  # noqa: E402
    EXCLUDED_ALWAYS,
    REQUIRES_INTERLOCUTOR,
    derive,
    exclusion_reasons,
)

VOCAB_NODE = Path("nodes/spec-emotion-vocabulary.html")
STATE_RE = re.compile(r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', re.S)


@pytest.fixture(scope="module")
def state():
    return json.loads(STATE_RE.search(VOCAB_NODE.read_text(encoding="utf-8")).group(1).strip())


@pytest.fixture(scope="module")
def eras():
    from generate_stories import ERAS

    return ERAS


def test_node_matches_the_derivation_rule(state, eras):
    """The node is generated output. Hand-editing it, or changing the rule
    without re-running --write, puts the two out of step -- which is how the
    original map went missing without anything noticing for two days."""
    assert state["config"]["era_availability"] == derive(state["config"]["emotions"], eras)


def test_check_mode_passes_as_a_gate(state):
    """--check is the CI-callable form of the test above; it must agree."""
    result = subprocess.run(
        [sys.executable, "scripts/derive_era_availability.py", "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_era_permits_enough_emotions_to_build_an_arc(state):
    """generate_stories picks 3 distinct-in-sequence emotions from this list.
    Below 2 it spins forever rather than failing, so the floor is load-bearing."""
    for era, emotions in state["config"]["era_availability"].items():
        assert len(emotions) >= 2, f"{era} permits only {len(emotions)}"


def test_permitted_emotions_all_exist_in_the_vocabulary(state):
    vocab = set(state["config"]["emotions"])
    for era, emotions in state["config"]["era_availability"].items():
        unknown = sorted(set(emotions) - vocab)
        assert not unknown, f"{era} permits emotions absent from the vocabulary: {unknown}"


def test_every_era_in_the_generator_has_an_entry(state, eras):
    """A new era added to generate_stories.ERAS with no entry here makes
    _era_emotions raise on that era only -- an intermittent failure that looks
    like a bad window rather than a missing config."""
    assert set(state["config"]["era_availability"]) == set(eras)


def test_exclusions_follow_from_era_properties_not_era_names(eras):
    """The interlocutor rule is applied from each era's own `interlocutor`
    field, so a new era inherits it rather than having to be remembered."""
    reasons = exclusion_reasons(eras)
    for era_name, era in eras.items():
        excluded = set(reasons[era_name])
        assert set(EXCLUDED_ALWAYS) <= excluded
        if era.get("interlocutor") is None:
            assert set(REQUIRES_INTERLOCUTOR) <= excluded
        else:
            assert not (set(REQUIRES_INTERLOCUTOR) & excluded)


def test_every_exclusion_carries_a_reason(state):
    """The map is unreviewed invented data. A bare list cannot be argued with;
    a reason can. Provenance in the node must cover every excluded emotion."""
    recorded = state["metadata"]["era_availability_exclusions"]
    vocab = set(state["config"]["emotions"])
    for era, emotions in state["config"]["era_availability"].items():
        excluded = vocab - set(emotions)
        assert set(recorded[era]) == excluded, f"{era}: undocumented exclusions"
        for emotion, reason in recorded[era].items():
            assert reason.strip(), f"{era}/{emotion} has an empty reason"


def test_provenance_still_says_the_map_is_unrecovered(state):
    """If this ever reads as authored data, the next person to touch it will
    trust values that were guessed. It was never recovered and cannot be."""
    source = state["metadata"]["era_availability_source"].lower()
    assert "not authored" in source or "not recovered" in source
    assert "unrecoverable" in source
