"""
The nightly dream: consolidation that runs by itself.

``GoalEngine.dream_cycle`` existed and nothing ever called it. ``sdk/agent/dreaming.py`` is what runs it, riding the
heartbeat tick the way the reflection loop does:

* due once a day, after a configurable local hour, when learning is on and the emergency halt is not engaged;
* each run snapshots every node, applies the slow decay, runs the existing ``dream_cycle``, and then CHECKS what it did:
  no protected node changed, every weight is still inside [0.05, 1.0], and only nodes learning had touched were
  modified. Any exception, or any violated check, restores every file to the snapshot (a rollback);
* every run, rolled back or not, writes a dream-journal node and marks today as attempted, so a failing dream is not
  retried every minute.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sdk.agent import dreaming
from sdk.agent import node_learning as nl
from sdk.agent.parser import parse_file

NOW = datetime(2026, 10, 24, 3, 30, tzinfo=UTC)          # what the nodes' clocks are told
LOCAL_NIGHT = datetime(2026, 10, 24, 3, 30)               # naive = local, what dream_if_due is told


def make_node(directory, node_id, *, relevance="0.80", confidence="0.80", extra="", node_type="concept", governance="0.50"):
    path = directory / f"{node_id}.html"
    path.write_text(f'''<!DOCTYPE html>
<html><head><meta name="tala-version" content="neural-node/v2"></head><body>
<article
    data-node-id="{node_id}"
    data-node-type="{node_type}"
    data-execution-authority="0.50"
    data-schema-version="neural-node/v2"
    data-weight-relevance="{relevance}"
    data-weight-confidence="{confidence}"
    data-weight-governance-importance="{governance}"{extra}>
    <section id="semantic-memory">Apples are a delicious red fruit that grow on trees.</section>
    <script type="application/json" id="node-state">{{"title": "{node_id}"}}</script>
</article>
</body></html>
''', encoding="utf-8", newline="\n")
    return path


def idle_since(days):
    return f'\n    data-usage-count="3"\n    data-last-used="{(NOW - timedelta(days=days)).isoformat()}"'


@pytest.fixture
def world(tmp_path, monkeypatch):
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.delenv("TALA_LEARNING", raising=False)
    monkeypatch.setattr(nl, "_halted", lambda: False)
    monkeypatch.setattr(nl, "_ANCHOR_RUNNER", lambda fn: fn())
    monkeypatch.setattr("sdk.agent.anchor.canonicalize_and_anchor", lambda **kw: ("h", "id", "anchored", "", None))
    ran = []

    async def legacy_cycle(self):
        ran.append("legacy")
        return "legacy dream_cycle ran"

    monkeypatch.setattr("sdk.agent.goal_engine.GoalEngine.dream_cycle", legacy_cycle)

    async def idle(self, N_minutes=5.0):
        return True

    monkeypatch.setattr("sdk.agent.goal_engine.GoalEngine.check_dream_trigger", idle)
    return {"nodes": nodes, "ran": ran, "root": tmp_path}


# --- when it is due ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_is_not_due_before_the_dream_hour(world):
    nl.write_config({"dream_hour": 3})
    assert await dreaming.dream_if_due(now=datetime(2026, 10, 24, 2, 59)) is None
    assert world["ran"] == []


@pytest.mark.asyncio
async def test_it_runs_once_a_day_after_the_hour(world):
    first = await dreaming.dream_if_due(now=LOCAL_NIGHT)
    assert first is not None and first["status"] == "consolidated"
    assert nl.read_config()["last_dream_date"] == "2026-10-24"
    assert await dreaming.dream_if_due(now=LOCAL_NIGHT + timedelta(minutes=1)) is None, "not twice in a day"
    next_day = await dreaming.dream_if_due(now=LOCAL_NIGHT + timedelta(days=1))
    assert next_day is not None and world["ran"] == ["legacy", "legacy"]


@pytest.mark.asyncio
async def test_it_waits_for_the_system_to_be_idle(world, monkeypatch):
    async def busy(self, N_minutes=5.0):
        return False

    monkeypatch.setattr("sdk.agent.goal_engine.GoalEngine.check_dream_trigger", busy)
    assert await dreaming.dream_if_due(now=LOCAL_NIGHT) is None
    assert "last_dream_date" not in nl.read_config(), "waiting is not an attempt"


@pytest.mark.asyncio
async def test_it_can_be_switched_off(world):
    nl.write_config({"dream_enabled": False})
    assert await dreaming.dream_if_due(now=LOCAL_NIGHT) is None and world["ran"] == []


@pytest.mark.asyncio
async def test_learning_off_means_no_dream(world, monkeypatch):
    monkeypatch.setenv("TALA_LEARNING", "0")
    assert await dreaming.dream_if_due(now=LOCAL_NIGHT) is None and world["ran"] == []


@pytest.mark.asyncio
async def test_halted_means_no_dream_and_no_writes(world, monkeypatch):
    path = make_node(world["nodes"], "apples", extra=idle_since(20))
    before = path.read_bytes()
    monkeypatch.setattr(nl, "_halted", lambda: True)
    assert await dreaming.dream_if_due(now=LOCAL_NIGHT) is None
    assert await dreaming.run_dream(force=True, now=NOW) is None
    assert world["ran"] == [] and path.read_bytes() == before
    assert not list(world["nodes"].glob("dream-journal-*"))


# --- what a dream does --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dream_decays_idle_nodes_and_writes_a_journal(world):
    path = make_node(world["nodes"], "apples", extra=idle_since(20))
    outcome = await dreaming.run_dream(force=True, now=NOW)
    assert outcome["status"] == "consolidated" and outcome["decayed"] == 1
    assert parse_file(str(path)).weight_relevance == pytest.approx(0.79)
    journal = parse_file(str(world["nodes"] / f"{outcome['journal_node_id']}.html"))
    assert outcome["journal_node_id"] == "dream-journal-2026-10-24"
    assert "decayed" in journal.semantic_memory.lower() and "legacy dream_cycle ran" in journal.semantic_memory
    assert (journal.weight_relevance, journal.weight_confidence) == (0.3, 0.3), "a journal must not outrank real memory"


@pytest.mark.asyncio
async def test_the_journal_is_a_record_not_an_accountable_description(world):
    """A journal says what happened on a night. Left to default it was stamped 'description', which makes it accountable to
    source files it has none of: the live app's first dream wrote one and two repo invariants failed on it
    (test_document_accountability, test_document_purpose)."""
    outcome = await dreaming.run_dream(force=True, now=NOW)
    html = (world["nodes"] / f"{outcome['journal_node_id']}.html").read_text(encoding="utf-8")
    assert 'data-document-purpose="record"' in html


@pytest.mark.asyncio
async def test_a_journal_leaves_no_accountable_document_without_a_source(world):
    from sdk.agent.documentation_nodes import documentation_staleness

    await dreaming.run_dream(force=True, now=NOW)
    assert documentation_staleness()["source_coverage"]["without_reference"] == 0


@pytest.mark.asyncio
async def test_the_journal_summarises_what_learning_did_since_the_last_dream(world):
    make_node(world["nodes"], "apples")
    nl.record_use(["apples"], world["nodes"], now=NOW - timedelta(hours=5))
    nl.apply_feedback(["apples"], "down", world["nodes"], reason="not what I meant", now=NOW - timedelta(hours=4))
    outcome = await dreaming.run_dream(force=True, now=NOW)
    text = parse_file(str(world["nodes"] / f"{outcome['journal_node_id']}.html")).semantic_memory
    assert "1 use raised a node's relevance" in text and "1 piece of negative feedback" in text and "apples" in text
    assert outcome["uses"] == 1 and outcome["feedback"] == 1


@pytest.mark.asyncio
async def test_a_second_dream_the_same_day_gets_its_own_journal(world):
    make_node(world["nodes"], "apples", extra=idle_since(20))
    first = await dreaming.run_dream(force=True, now=NOW)
    second = await dreaming.run_dream(force=True, now=NOW + timedelta(minutes=5))
    assert (first["journal_node_id"], second["journal_node_id"]) == ("dream-journal-2026-10-24", "dream-journal-2026-10-24-2")
    assert len(list(world["nodes"].glob("dream-journal-*"))) == 2


# --- the safety net ------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failure_part_way_restores_every_node_and_still_journals(world, monkeypatch):
    path = make_node(world["nodes"], "apples", extra=idle_since(20))
    other = make_node(world["nodes"], "pears", extra=idle_since(30))
    before = {p: p.read_bytes() for p in (path, other)}
    real_decay = nl.decay_unused

    def decay_then_crash(nodes_dir, **kw):
        real_decay(nodes_dir, **kw)
        raise RuntimeError("disk went away mid-dream")

    monkeypatch.setattr(nl, "decay_unused", decay_then_crash)
    outcome = await dreaming.run_dream(force=True, now=NOW)
    assert outcome["status"] == "rolled-back" and "disk went away" in outcome["error"]
    assert {p: p.read_bytes() for p in before} == before, "every weight is back where it started"
    journal = parse_file(str(world["nodes"] / f"{outcome['journal_node_id']}.html"))
    assert "rolled back" in journal.semantic_memory.lower()


@pytest.mark.asyncio
async def test_a_failing_dream_is_not_retried_every_minute(world, monkeypatch):
    make_node(world["nodes"], "apples", extra=idle_since(20))
    monkeypatch.setattr(nl, "decay_unused", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert (await dreaming.dream_if_due(now=LOCAL_NIGHT))["status"] == "rolled-back"
    assert nl.read_config()["last_dream_date"] == "2026-10-24"
    assert await dreaming.dream_if_due(now=LOCAL_NIGHT + timedelta(minutes=1)) is None


@pytest.mark.asyncio
async def test_a_dream_that_touches_a_protected_node_is_rolled_back(world, monkeypatch):
    guarded = make_node(world["nodes"], "persona-tala-core")
    plain = make_node(world["nodes"], "apples", extra=idle_since(20))
    before = {p: p.read_bytes() for p in (guarded, plain)}
    real_decay = nl.decay_unused

    def decay_and_tamper(nodes_dir, **kw):
        real_decay(nodes_dir, **kw)
        guarded.write_bytes(guarded.read_bytes().replace(b'0.80"', b'0.10"', 1))
        return []

    monkeypatch.setattr(nl, "decay_unused", decay_and_tamper)
    outcome = await dreaming.run_dream(force=True, now=NOW)
    assert outcome["status"] == "rolled-back" and "protected" in outcome["error"]
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.asyncio
async def test_a_dream_that_pushes_a_weight_out_of_bounds_is_rolled_back(world, monkeypatch):
    path = make_node(world["nodes"], "apples", extra=idle_since(20))
    before = path.read_bytes()

    def decay_out_of_bounds(nodes_dir, **kw):
        path.write_bytes(path.read_bytes().replace(b'data-weight-relevance="0.80"', b'data-weight-relevance="0.01"'))
        return []

    monkeypatch.setattr(nl, "decay_unused", decay_out_of_bounds)
    outcome = await dreaming.run_dream(force=True, now=NOW)
    assert outcome["status"] == "rolled-back" and "bounds" in outcome["error"]
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_a_dream_that_edits_a_node_learning_never_touched_is_rolled_back(world, monkeypatch):
    system_node = make_node(world["nodes"], "system-doc", relevance="1.00", confidence="1.00")
    before = system_node.read_bytes()

    def decay_wrongly(nodes_dir, **kw):
        system_node.write_bytes(system_node.read_bytes().replace(b'data-weight-relevance="1.00"', b'data-weight-relevance="0.90"'))
        return []

    monkeypatch.setattr(nl, "decay_unused", decay_wrongly)
    outcome = await dreaming.run_dream(force=True, now=NOW)
    assert outcome["status"] == "rolled-back"
    assert system_node.read_bytes() == before


# --- it rides the heartbeat -------------------------------------------------------------------------------------------


@pytest.fixture
def quiet_tick(monkeypatch):
    """The other things a tick does (zombie reaper, goal resume, reflection) are not under test and must not run."""
    async def zero(*a, **k):
        return 0

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr("sdk.agent.swarm_engine.ZombieReaper.reclaim_zombies", classmethod(lambda cls, *a, **k: zero()))
    monkeypatch.setattr("sdk.agent.goal_engine.resume_suspended_goals", zero)
    monkeypatch.setattr("sdk.agent.reflection.reflect_if_due", nothing)


@pytest.mark.asyncio
async def test_the_heartbeat_tick_fires_the_dream(monkeypatch, quiet_tick):
    from python.harness.heartbeat_scheduler import HeartbeatScheduler

    fired = []

    async def spy(**kwargs):
        fired.append(kwargs)
        return {"status": "consolidated"}

    monkeypatch.setattr(dreaming, "dream_if_due", spy)
    await HeartbeatScheduler()._evaluate_tick()
    assert fired == [{}]


@pytest.mark.asyncio
async def test_a_dream_error_never_breaks_the_tick(monkeypatch, quiet_tick):
    from python.harness.heartbeat_scheduler import HeartbeatScheduler

    async def broken(**kwargs):
        raise RuntimeError("dream exploded")

    monkeypatch.setattr(dreaming, "dream_if_due", broken)
    await HeartbeatScheduler()._evaluate_tick()  # must not raise


@pytest.mark.asyncio
async def test_the_real_idle_check_runs_on_a_fresh_engine(tmp_path, monkeypatch):
    """GoalEngine.check_dream_trigger read an attribute only run_goal ever set, so asking a fresh engine raised
    AttributeError. Found by the live smoke, because every other test here replaces the idle check."""
    (tmp_path / "nodes").mkdir()
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.delenv("TALA_LEARNING", raising=False)
    monkeypatch.setattr(nl, "_halted", lambda: False)
    monkeypatch.setattr(nl, "_ANCHOR_RUNNER", lambda fn: fn())
    monkeypatch.setattr("sdk.agent.anchor.canonicalize_and_anchor", lambda **kw: ("h", "id", "anchored", "", None))
    monkeypatch.setattr("python.harness.db.get_pool", lambda: None)

    async def legacy_cycle(self):
        return "legacy dream_cycle ran"

    monkeypatch.setattr("sdk.agent.goal_engine.GoalEngine.dream_cycle", legacy_cycle)
    outcome = await dreaming.dream_if_due(now=LOCAL_NIGHT)
    assert outcome is not None and outcome["status"] == "consolidated"


@pytest.mark.asyncio
async def test_the_journal_counts_what_changed_a_weight_not_every_use(world):
    """Found by the live smoke: six uses of one node, the sixth capped by the daily limit (it changes nothing, so it is not logged).
    The journal said "5 uses of nodes in conversation", which claimed more than it knew."""
    make_node(world["nodes"], "apples")
    for minute in range(7):
        nl.record_use(["apples"], world["nodes"], now=NOW - timedelta(hours=3, minutes=minute))
    outcome = await dreaming.run_dream(force=True, now=NOW)
    text = parse_file(str(world["nodes"] / f"{outcome['journal_node_id']}.html")).semantic_memory
    assert "5 uses raised a node's relevance" in text and "7 uses" not in text
    assert "daily cap" in text, "and says what it leaves out"
