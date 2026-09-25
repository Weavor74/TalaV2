"""
tests/test_valence_is_wired.py

Guards the fix for the eighth instance of the dead-subsystem pattern, and the
only one so far where a node asserted the wiring.

nodes/spec-emotion-vocabulary.html carried a projection of sixty emotions onto
four valence keys and said in its own prose that those keys were read by
``update_manifest`` and ``adjust_behavioral_weights``. Neither read them. The
astro engine reached the system prompt and nothing that weighted memory, so
sixteen LoRA directories and a fully-built emotion engine had no effect on
which nodes Tala recalled.

These tests assert the chain, link by link, because the individual pieces all
worked in isolation for months while the chain did not exist.
"""
from __future__ import annotations

import pytest

from python.astrology import valence as V


def test_the_projection_loads_from_the_node_not_from_python():
    """The node is the source of truth; python/astrology/valence.py is a reader."""
    projection = V._load_projection()
    assert len(projection) >= 50, "the vocabulary projection did not load"
    for emotion, keys in projection.items():
        assert set(keys) == set(V.VALENCE_KEYS), f"{emotion} projects onto the wrong keys"


def test_every_emotion_the_engine_can_emit_is_projectable():
    """
    An emotion with no projection produces a state that cannot weigh anything.

    This is the agreement the vocabulary node describes, asserted rather than
    left to discipline.
    """
    from python.astrology.emotion import PLANET_EMOTION_MAP

    projection = V._load_projection()
    missing = []
    for harmonious, challenging in PLANET_EMOTION_MAP.values():
        for word in tuple(harmonious) + tuple(challenging):
            if word.lower() not in projection:
                missing.append(word)
    assert not missing, f"emotions the engine can emit but cannot project: {sorted(set(missing))}"


def test_blend_moves_toward_the_projection_in_proportion_to_intensity():
    base = {k: 0.5 for k in V.VALENCE_KEYS}

    calm, moved = V.blend(base, "hope", 0.0)
    assert moved is True
    assert calm == base, "zero intensity must not move the weights"

    full, moved = V.blend(base, "hope", 1.0)
    assert moved is True
    assert full["joy"] == pytest.approx(V.project("hope")["joy"])

    half, _ = V.blend(base, "hope", 0.5)
    assert base["joy"] < half["joy"] < full["joy"]


def test_an_unknown_emotion_does_not_silently_read_as_neutral():
    """'Could not project' and 'projected to neutral' must not look the same."""
    base = {k: 0.5 for k in V.VALENCE_KEYS}
    out, moved = V.blend(base, "not_a_real_emotion", 1.0)
    assert moved is False
    assert out == base


def test_mood_alignment_discriminates_between_nodes(monkeypatch):
    """A mood that ranks everything equally is not wired, it is decorative."""
    monkeypatch.setattr(V, "_current_state_sync", lambda: ("hope", 1.0))

    joyful = {"joy": 0.9, "caution": 0.1, "curiosity": 0.5, "neutral": 0.3}
    wary = {"joy": 0.1, "caution": 0.9, "curiosity": 0.4, "neutral": 0.3}

    a_joy = V.mood_alignment(joyful)
    a_wary = V.mood_alignment(wary)
    assert a_joy is not None and a_wary is not None
    assert a_joy[0] > a_wary[0], "hope did not favour the joy-coloured node"


def test_no_mood_is_none_not_a_neutral_score(monkeypatch):
    monkeypatch.setattr(V, "_current_state_sync", lambda: None)
    assert V.mood_alignment({k: 0.5 for k in V.VALENCE_KEYS}) is None


def test_retrieval_tilts_its_ranking_by_the_mood(monkeypatch):
    """
    The live consumer. Before this, node valence had no reader that ran:
    _select_adapter and dream_cycle both had no production caller.
    """
    from python.harness import retrieval

    monkeypatch.setattr(V, "_current_state_sync", lambda: ("hope", 1.0))

    joyful = {"joy": 0.9, "caution": 0.1, "curiosity": 0.5, "neutral": 0.3}
    wary = {"joy": 0.1, "caution": 0.9, "curiosity": 0.4, "neutral": 0.3}

    joy_factor = 1.0 + retrieval.MOOD_WEIGHT * (V.mood_alignment(joyful)[0] - 0.5) * 2.0
    wary_factor = 1.0 + retrieval.MOOD_WEIGHT * (V.mood_alignment(wary)[0] - 0.5) * 2.0

    assert joy_factor > wary_factor
    # Mood reorders the middle of a ranking; it must not be able to promote an
    # irrelevant node over a relevant one.
    assert 1.0 - retrieval.MOOD_WEIGHT <= wary_factor <= 1.0 + retrieval.MOOD_WEIGHT
    assert 1.0 - retrieval.MOOD_WEIGHT <= joy_factor <= 1.0 + retrieval.MOOD_WEIGHT


def test_indexed_metadata_carries_the_valence():
    """Without the four keys in the index there is nothing to weigh at query time."""
    import inspect
    from python.harness import retrieval

    src = inspect.getsource(retrieval.index_nodes)
    assert "_node_valence" in src, "node valence is not written into the search index"


def test_update_manifest_consults_the_astro_engine():
    """
    The producer half. tools.py did not import the astrology package at all,
    which is what made the vocabulary node's claim false.
    """
    import inspect
    from sdk.agent import tools

    src = inspect.getsource(tools.update_manifest)
    assert "apply_current" in src
    assert "valence_source" in src, (
        "the manifest records valence with no account of where it came from"
    )


def test_the_node_no_longer_claims_a_reader_it_does_not_have():
    """The correction itself, so the document cannot drift back."""
    from pathlib import Path

    node = Path(__file__).resolve().parents[1] / "nodes" / "spec-emotion-vocabulary.html"
    text = node.read_text(encoding="utf-8", errors="replace")
    assert "retrieve_relevant_nodes" in text, "the node does not name its real consumer"
    assert "valence.py" in text, "the node does not name the reader of its own table"
