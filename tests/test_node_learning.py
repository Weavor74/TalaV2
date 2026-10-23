"""
HTML-NN node weights learn from use and feedback.

Retrieval multiplies ``similarity x weight_relevance x weight_confidence``, but the weights were parsed once and
nothing ever wrote them back, so no amount of use or feedback could change what Tala recalled. The existing
"learning" heuristics (adjust_behavioral_weights, process_feedback, dream_cycle) edited a ``node-metadata`` JSON
block that 358 of the 361 real nodes do not have and retrieval never reads.

``sdk/agent/node_learning.py`` writes the attributes retrieval does read (``data-weight-relevance`` and
``data-weight-confidence``), plus ``data-usage-count`` / ``data-last-used`` / ``data-last-decay``:

* using a node nudges its relevance up (small step, diminishing at the ceiling, capped per day);
* explicit feedback moves its confidence (up a little, down more, "that's wrong" most);
* a node learning has used and then abandoned decays slowly, to a floor, never to nothing;
* everything is clamped to [0.05, 1.0], bounded per step, logged, anchored, and written atomically;
* protected nodes (governance-critical, core, safety, vault, persona) are never touched;
* disabled or halted means no write at all.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sdk.agent import node_learning as nl
from sdk.agent.parser import parse_file

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def make_node(directory: Path, node_id: str, *, relevance="0.80", confidence="0.80", node_type="concept",
              governance="0.50", extra="", text="Apples are a delicious red fruit that grow on trees."):
    path = directory / f"{node_id}.html"
    path.write_text(f'''<!DOCTYPE html>
<html>
<head><meta name="tala-version" content="neural-node/v2"></head>
<body>
<article
    data-node-id="{node_id}"
    data-node-type="{node_type}"
    data-execution-authority="0.50"
    data-schema-version="neural-node/v2"
    data-weight-relevance="{relevance}"
    data-weight-confidence="{confidence}"
    data-weight-governance-importance="{governance}"{extra}>
    <section id="semantic-memory">{text}</section>
    <script type="application/json" id="node-state">{{"title": "{node_id}"}}</script>
</article>
</body>
</html>
''', encoding="utf-8", newline="\n")
    return path


def weights(path: Path) -> dict:
    node = parse_file(str(path))
    return {"relevance": round(node.weight_relevance, 3), "confidence": round(node.weight_confidence, 3),
            "usage": node.usage_count, "last_used": node.last_used}


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    """Learning on, not halted, nothing anchored for real, the log in a sandbox."""
    monkeypatch.delenv("TALA_LEARNING", raising=False)
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setattr(nl, "_halted", lambda: False)
    anchored = []
    monkeypatch.setattr(nl, "_ANCHOR_RUNNER", lambda fn: fn())
    monkeypatch.setattr("sdk.agent.anchor.canonicalize_and_anchor", lambda **kw: anchored.append(kw) or ("h", "id", "anchored", "", None))
    return anchored


@pytest.fixture
def nodes(tmp_path):
    d = tmp_path / "nodes"
    d.mkdir()
    return d


# --- use ------------------------------------------------------------------------------------------


def test_using_a_node_raises_its_relevance_and_counts_the_use(nodes):
    path = make_node(nodes, "apples")
    changes = nl.record_use(["apples"], nodes, now=NOW)
    assert weights(path) == {"relevance": 0.82, "confidence": 0.80, "usage": 1, "last_used": NOW.isoformat()}
    assert [c["field"] for c in changes] == ["weight_relevance"]


def test_the_gain_from_use_is_capped_per_day(nodes):
    path = make_node(nodes, "apples", relevance="0.50")
    for i in range(12):
        nl.record_use(["apples"], nodes, now=NOW + timedelta(minutes=i))
    got = weights(path)
    assert got["relevance"] == pytest.approx(0.60, abs=0.001), "at most +0.10 in one day"
    assert got["usage"] == 12, "every use is still counted"


def test_the_cap_resets_the_next_day(nodes):
    path = make_node(nodes, "apples", relevance="0.50")
    for i in range(8):
        nl.record_use(["apples"], nodes, now=NOW + timedelta(minutes=i))
    nl.record_use(["apples"], nodes, now=NOW + timedelta(days=1))
    assert weights(path)["relevance"] == pytest.approx(0.62, abs=0.001)


def test_a_node_at_the_ceiling_stays_there_but_is_still_counted(nodes):
    path = make_node(nodes, "apples", relevance="1.00")
    nl.record_use(["apples"], nodes, now=NOW)
    assert weights(path)["relevance"] == 1.0 and weights(path)["usage"] == 1


def test_a_node_named_twice_in_one_call_is_used_once(nodes):
    path = make_node(nodes, "apples")
    nl.record_use(["apples", "apples"], nodes, now=NOW)
    assert weights(path)["usage"] == 1


def test_an_unknown_node_is_skipped_without_error(nodes):
    assert nl.record_use(["no-such-node"], nodes, now=NOW) == []


# --- feedback --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind,expected", [("up", 0.85), ("down", 0.70), ("wrong", 0.60)])
def test_feedback_moves_confidence_and_leaves_relevance(nodes, kind, expected):
    path = make_node(nodes, "apples")
    nl.apply_feedback(["apples"], kind, nodes, now=NOW)
    got = weights(path)
    assert got["confidence"] == pytest.approx(expected, abs=0.001) and got["relevance"] == 0.80


def test_negative_feedback_lowers_confidence_every_time_down_to_the_floor(nodes):
    path = make_node(nodes, "apples")
    seen = []
    for _ in range(6):
        nl.apply_feedback(["apples"], "wrong", nodes, now=NOW)
        seen.append(weights(path)["confidence"])
    assert seen == sorted(seen, reverse=True) and seen[-1] == nl.MIN_WEIGHT


def test_positive_feedback_stops_at_the_ceiling(nodes):
    path = make_node(nodes, "apples", confidence="0.98")
    nl.apply_feedback(["apples"], "up", nodes, now=NOW)
    assert weights(path)["confidence"] == 1.0


def test_an_unknown_feedback_kind_is_refused(nodes):
    make_node(nodes, "apples")
    with pytest.raises(ValueError):
        nl.apply_feedback(["apples"], "love-it", nodes, now=NOW)


# --- decay ------------------------------------------------------------------------------------------


def used_long_ago(days, relevance="0.80"):
    stamp = (NOW - timedelta(days=days)).isoformat()
    return f'\n    data-usage-count="3"\n    data-last-used="{stamp}"', relevance


def test_a_node_used_then_abandoned_decays_slowly(nodes):
    extra, rel = used_long_ago(10)
    path = make_node(nodes, "apples", relevance=rel, extra=extra)
    nl.decay_unused(nodes, now=NOW)
    assert weights(path)["relevance"] == pytest.approx(0.79, abs=0.001)


def test_decay_is_not_applied_twice_for_the_same_idle_period(nodes):
    extra, rel = used_long_ago(10)
    path = make_node(nodes, "apples", relevance=rel, extra=extra)
    nl.decay_unused(nodes, now=NOW)
    nl.decay_unused(nodes, now=NOW + timedelta(hours=1))
    assert weights(path)["relevance"] == pytest.approx(0.79, abs=0.001)
    nl.decay_unused(nodes, now=NOW + timedelta(days=8))
    assert weights(path)["relevance"] == pytest.approx(0.78, abs=0.001)


def test_a_recently_used_node_does_not_decay(nodes):
    extra, rel = used_long_ago(3)
    path = make_node(nodes, "apples", relevance=rel, extra=extra)
    assert nl.decay_unused(nodes, now=NOW) == [] and weights(path)["relevance"] == 0.8


def test_a_node_learning_never_used_never_decays(nodes):
    """The system's own nodes carry no usage attributes, so time alone cannot erode them."""
    path = make_node(nodes, "apples", relevance="1.00")
    assert nl.decay_unused(nodes, now=NOW + timedelta(days=400)) == []
    assert weights(path)["relevance"] == 1.0


def test_decay_never_goes_below_the_floor(nodes):
    extra, _ = used_long_ago(30)
    path = make_node(nodes, "apples", relevance="0.50", extra=extra)
    nl.decay_unused(nodes, now=NOW)
    assert weights(path)["relevance"] == nl.DECAY_FLOOR


# --- protection, disable, halt -----------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [
    {"governance": "0.95"},
    {"node_type": "Core"},
    {"node_type": "safety"},
    {"node_type": "vault"},
])
def test_protected_nodes_are_never_changed(nodes, kwargs):
    path = make_node(nodes, "guarded", **kwargs)
    before = path.read_bytes()
    assert nl.record_use(["guarded"], nodes, now=NOW) == []
    assert nl.apply_feedback(["guarded"], "wrong", nodes, now=NOW) == []
    assert path.read_bytes() == before


def test_persona_nodes_are_protected_by_name(nodes):
    path = make_node(nodes, "persona-tala-core")
    before = path.read_bytes()
    assert nl.apply_feedback(["persona-tala-core"], "down", nodes, now=NOW) == []
    assert path.read_bytes() == before


def test_disabled_means_no_write_at_all(nodes, monkeypatch):
    path = make_node(nodes, "apples", extra='\n    data-usage-count="2"\n    data-last-used="2026-01-01T00:00:00+00:00"')
    before = path.read_bytes()
    monkeypatch.setenv("TALA_LEARNING", "0")
    assert nl.learning_enabled() is False
    assert nl.record_use(["apples"], nodes, now=NOW) == []
    assert nl.apply_feedback(["apples"], "down", nodes, now=NOW) == []
    assert nl.decay_unused(nodes, now=NOW) == []
    assert path.read_bytes() == before


def test_the_setting_persists_and_the_environment_wins(nodes, monkeypatch):
    nl.set_learning_enabled(False)
    assert nl.learning_enabled() is False
    nl.set_learning_enabled(True)
    assert nl.learning_enabled() is True
    nl.set_learning_enabled(True)
    monkeypatch.setenv("TALA_LEARNING", "0")
    assert nl.learning_enabled() is False


def test_halted_means_no_write_at_all(nodes, monkeypatch):
    path = make_node(nodes, "apples")
    before = path.read_bytes()
    monkeypatch.setattr(nl, "_halted", lambda: True)
    assert nl.record_use(["apples"], nodes, now=NOW) == []
    assert nl.apply_feedback(["apples"], "wrong", nodes, now=NOW) == []
    assert nl.reset_weights(nodes, ["apples"]) == []
    assert path.read_bytes() == before


def test_an_unreadable_halt_state_counts_as_halted(monkeypatch):
    from sdk.agent import gates

    monkeypatch.undo()  # the autouse fixture stubbed _halted; this test wants the real one

    def boom():
        raise RuntimeError("cannot read halt state")

    monkeypatch.setattr(gates.RevocationHalt, "is_halted", staticmethod(boom))
    assert nl._halted() is True


# --- atomic writes ------------------------------------------------------------------------------------


def test_a_crash_while_writing_leaves_the_original_untouched(nodes, monkeypatch):
    path = make_node(nodes, "apples")
    before = path.read_bytes()

    def crash(src, dst):
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        nl._atomic_write_text(path, "replacement that must never be seen")
    assert path.read_bytes() == before
    assert [p.name for p in nodes.iterdir()] == ["apples.html"], "no half-written temp file left behind"


def test_a_failed_learning_write_does_not_break_the_caller(nodes, monkeypatch):
    path = make_node(nodes, "apples")
    before = path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("disk full")))
    assert nl.record_use(["apples"], nodes, now=NOW) == []
    assert path.read_bytes() == before


def test_the_rest_of_the_node_is_preserved_byte_for_byte(nodes):
    path = make_node(nodes, "apples")
    before = path.read_text(encoding="utf-8")
    nl.record_use(["apples"], nodes, now=NOW)
    after = path.read_text(encoding="utf-8")
    assert before.split("<article")[0] == after.split("<article")[0]
    assert before.split("</article>")[1] == after.split("</article>")[1]
    assert 'id="semantic-memory">Apples are a delicious red fruit' in after


# --- audit trail ---------------------------------------------------------------------------------------


def test_every_change_is_logged_with_before_and_after(nodes):
    make_node(nodes, "apples")
    nl.record_use(["apples"], nodes, now=NOW)
    nl.apply_feedback(["apples"], "down", nodes, reason="user said no", now=NOW)
    events = [json.loads(line) for line in nl._log_path().read_text(encoding="utf-8").splitlines()]
    assert [(e["kind"], e["field"], e["before"], e["after"]) for e in events] == [
        ("use", "weight_relevance", 0.8, 0.82), ("feedback_down", "weight_confidence", 0.8, 0.7)]
    assert events[1]["reason"] == "user said no" and all(e["node_id"] == "apples" for e in events)


def test_a_batch_is_anchored_once_with_no_key_material(nodes, quiet):
    make_node(nodes, "apples")
    make_node(nodes, "pears")
    nl.record_use(["apples", "pears"], nodes, now=NOW)
    assert len(quiet) == 1, "one ledger transaction per batch, not one per node"
    call = quiet[0]
    assert call["document_type"] == "node-weight-update" and call["creator"] == "tala-node-learning"
    assert {c["node_id"] for c in call["state_data"]["changes"]} == {"apples", "pears"}
    flat = json.dumps(call["state_data"]).lower()
    assert not any(word in flat for word in ("key", "secret", "token", "password", "seed"))


def test_nothing_is_anchored_when_nothing_changed(nodes, quiet):
    make_node(nodes, "apples", governance="0.95")
    nl.record_use(["apples"], nodes, now=NOW)
    assert quiet == []


def test_a_ledger_failure_never_breaks_learning(nodes, monkeypatch):
    path = make_node(nodes, "apples")

    def broken(**kw):
        raise RuntimeError("chain is down")

    monkeypatch.setattr("sdk.agent.anchor.canonicalize_and_anchor", broken)
    nl.record_use(["apples"], nodes, now=NOW)
    assert weights(path)["usage"] == 1


# --- reset, snapshot ------------------------------------------------------------------------------------


def test_reset_returns_learned_nodes_to_the_schema_default(nodes):
    extra, _ = used_long_ago(2)
    learned = make_node(nodes, "learned", relevance="0.60", confidence="0.40", extra=extra)
    untouched = make_node(nodes, "plain", relevance="0.30", confidence="0.30")
    nl.reset_weights(nodes)
    assert weights(learned) == {"relevance": 1.0, "confidence": 1.0, "usage": 0, "last_used": None}
    assert weights(untouched)["relevance"] == 0.3, "a node learning never touched is left alone"


def test_reset_can_name_nodes_explicitly(nodes):
    plain = make_node(nodes, "plain", relevance="0.30", confidence="0.30")
    nl.reset_weights(nodes, ["plain"])
    assert weights(plain)["relevance"] == 1.0


def test_a_snapshot_can_be_restored_exactly(nodes):
    path = make_node(nodes, "apples")
    before = path.read_bytes()
    snap = nl.snapshot(nodes, ["apples"])
    nl.apply_feedback(["apples"], "wrong", nodes, now=NOW)
    assert path.read_bytes() != before
    nl.restore(snap)
    assert path.read_bytes() == before


def test_a_new_learned_node_can_be_seeded_below_the_ceiling(nodes):
    path = make_node(nodes, "fact", relevance="1.00", confidence="1.00")
    nl.seed_new_node(path, relevance=nl.NEW_FACT_WEIGHT, confidence=nl.NEW_FACT_WEIGHT, now=NOW)
    assert weights(path) == {"relevance": 0.8, "confidence": 0.8, "usage": 0, "last_used": NOW.isoformat()}


# --- the parser and retrieval ------------------------------------------------------------------------------


def test_the_parser_reads_the_usage_attributes(nodes):
    used = make_node(nodes, "used", extra='\n    data-usage-count="7"\n    data-last-used="2026-10-01T00:00:00+00:00"')
    plain = make_node(nodes, "plain")
    assert (parse_file(str(used)).usage_count, parse_file(str(used)).last_used) == (7, "2026-10-01T00:00:00+00:00")
    assert (parse_file(str(plain)).usage_count, parse_file(str(plain)).last_used) == (0, None)


@pytest.fixture
def isolated_collection(monkeypatch):
    import chromadb
    from python.harness import retrieval

    col = chromadb.EphemeralClient().get_or_create_collection(name="learning_isolated", metadata={"hnsw:space": "cosine"})
    monkeypatch.setattr(retrieval, "node_collection", col)
    monkeypatch.setattr(retrieval, "_INDEXED_MTIMES", {})
    return col


def test_a_node_retrieved_and_used_n_times_outranks_an_identical_one(nodes, isolated_collection):
    from python.harness import retrieval

    make_node(nodes, "note-aa", relevance="0.70", confidence="0.80")
    make_node(nodes, "note-ab", relevance="0.70", confidence="0.80")
    query = "apples are a delicious red fruit"

    before = {n.node_id: s for n, s in retrieval.retrieve_relevant_nodes(query, nodes, top_k=5, threshold=0.0)}
    for i in range(5):
        nl.record_use(["note-ab"], nodes, now=NOW + timedelta(minutes=i))
    after = {n.node_id: s for n, s in retrieval.retrieve_relevant_nodes(query, nodes, top_k=5, threshold=0.0)}

    assert after["note-ab"] > before["note-ab"], "using it raised its retrieval score"
    assert after["note-ab"] > after["note-aa"], "and it now outranks the identical node that was never used"
    assert after["note-aa"] == pytest.approx(before["note-aa"], rel=1e-6), "the other node did not move"


def test_negative_feedback_lowers_a_nodes_retrieval_score(nodes, isolated_collection):
    from python.harness import retrieval

    make_node(nodes, "note-aa")
    query = "apples are a delicious red fruit"
    before = dict((n.node_id, s) for n, s in retrieval.retrieve_relevant_nodes(query, nodes, top_k=5, threshold=0.0))
    nl.apply_feedback(["note-aa"], "wrong", nodes, now=NOW)
    after = dict((n.node_id, s) for n, s in retrieval.retrieve_relevant_nodes(query, nodes, top_k=5, threshold=0.0))
    assert after["note-aa"] == pytest.approx(before["note-aa"] * (0.60 / 0.80), rel=1e-6)


def test_updating_weights_does_not_force_a_re_embed(nodes, isolated_collection, monkeypatch):
    from python.harness import retrieval

    path = make_node(nodes, "note-aa")
    retrieval.index_nodes(nodes)
    upserts = []

    class Spy:
        """Delegates to the real collection but records every upsert (a re-embed)."""

        def __init__(self, inner):
            self._inner = inner

        def upsert(self, **kw):
            upserts.append(kw)
            return self._inner.upsert(**kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(retrieval, "node_collection", Spy(isolated_collection))
    nl.record_use(["note-aa"], nodes, now=NOW)
    retrieval.index_nodes(nodes)
    assert upserts == [], "the file changed but its Chroma metadata was updated in place, so it is not re-embedded"
    assert isolated_collection.get(ids=["note-aa"], include=["metadatas"])["metadatas"][0]["weight_relevance"] == pytest.approx(0.82)
    assert retrieval._INDEXED_MTIMES[str(path)] == path.stat().st_mtime
