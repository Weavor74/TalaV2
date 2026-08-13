"""
tests/test_astro_emotion.py
===========================
Guards the astro-emotional engine.

The bug these tests exist to prevent recurring: ``astro-emotion`` was never
built as an MCP server, so every
``mcp_manager.call_tool("astro-emotion", "get_emotional_state", ...)`` raised
KeyError into a try/except that only logged a warning. Tala's emotional state
silently evaluated to nothing on every turn for the entire life of the feature,
and nothing surfaced it.

The most important test here is therefore ``test_virtual_mcp_tool_resolves`` --
it asserts the call site actually gets an emotion back, which is the exact
thing that was false before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from python.astrology.emotion import (
    PLANET_EMOTION_MAP,
    Contact,
    contacts_from_synastry,
    contacts_from_transits,
    derive_emotion,
    emotion_vs_event,
    emotion_vs_person,
    emotion_vs_time,
)
from python.astrology.ephemeris import Birthdate, EphemerisEngine
from python.astrology.synastry import SynastryEngine

TALA = Birthdate(1977, 7, 20, 12.0)
WHEN = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Canonical identity
# ---------------------------------------------------------------------------


def test_tala_natal_is_the_permanent_birthdate():
    """
    1977-07-20 is Tala's permanent birthdate. There used to be a second,
    disagreeing hardcoded chart (2014-05-26) as the ``tala-origin`` fallback in
    handle_astro_transits, which meant that whether the database happened to be
    reachable silently decided which natal chart -- and therefore which
    emotional life -- she had.
    """
    from python.astrology.ephemeris import TALA_NATAL

    assert (TALA_NATAL.year, TALA_NATAL.month, TALA_NATAL.day) == (1977, 7, 20)


def test_persona_config_agrees_with_canonical_chart():
    """Her persona node and the code must not drift apart again."""
    import json
    import re
    from pathlib import Path

    from python.astrology.ephemeris import TALA_NATAL

    node = Path(__file__).resolve().parent.parent / "nodes" / "persona-tala-rp.html"
    raw = node.read_text(encoding="utf-8")
    m = re.search(r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', raw, re.DOTALL)
    assert m, "persona-tala-rp.html has no node-state block"
    configured = json.loads(m.group(1).strip())["config"]["birthdate"]

    expected = f"{TALA_NATAL.year:04d}-{TALA_NATAL.month:02d}-{TALA_NATAL.day:02d}"
    assert configured == expected


@pytest.mark.asyncio
async def test_service_falls_back_to_canonical_chart(tmp_path, monkeypatch):
    """With no DB, the emotion service must use 1977-07-20, not some other date."""
    import python.harness.emotion_service as svc

    monkeypatch.setattr(svc, "_state_path", lambda: tmp_path / "emotion_state.json")
    res = await svc.get_emotional_state(agent_id=None)

    assert res["emotional_state"]["birthdate"] == "1977-07-20"


# ---------------------------------------------------------------------------
# Vocabulary integrity
# ---------------------------------------------------------------------------


def test_emotion_vocabulary_matches_spec_node():
    """
    Every emotion the engine can emit must exist in the spec node, because the
    spec node carries the projection onto the four canonical valence keys that
    update_manifest and adjust_behavioral_weights read. An emotion the engine
    emits but the spec doesn't know about produces an unprojectable state.
    """
    import json
    import re
    from pathlib import Path

    node = Path(__file__).resolve().parent.parent / "nodes" / "spec-emotion-vocabulary.html"
    raw = node.read_text(encoding="utf-8")
    m = re.search(r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', raw, re.DOTALL)
    assert m, "spec-emotion-vocabulary.html has no node-state block"
    spec_emotions = set(json.loads(m.group(1).strip())["config"]["emotions"])

    engine_emotions: set[str] = set()
    for harmonious, challenging in PLANET_EMOTION_MAP.values():
        engine_emotions |= set(harmonious) | set(challenging)

    missing = engine_emotions - spec_emotions
    assert not missing, f"engine emits emotions absent from the spec node: {sorted(missing)}"


# ---------------------------------------------------------------------------
# The three comparison modes
# ---------------------------------------------------------------------------


def test_vs_time_returns_named_emotion():
    state = emotion_vs_time(TALA, WHEN)
    assert state.mode == "time"
    assert state.primary_emotion != "neutral"
    assert 0.0 < state.intensity <= 1.0
    assert state.contributing


def test_vs_event_is_labelled():
    state = emotion_vs_event(TALA, WHEN, "the disposal crash")
    assert state.mode == "event"
    assert state.subject == "the disposal crash"


def test_vs_person_uses_other_partys_planets_as_actors():
    """
    In synastry the *other* party's planets act on Tala's. compute_synastry
    puts Tala in planet_a and the other in planet_b, so the acting body must
    come from planet_b -- getting this backwards silently inverts every
    relationship reading.
    """
    other = Birthdate(1990, 3, 14, 9.0)
    matrix = SynastryEngine().compute_synastry(TALA, other)
    contacts = contacts_from_synastry(matrix.aspects)
    assert contacts
    by_pair = {(a.planet_b, a.planet_a) for a in matrix.aspects}
    for c in contacts:
        assert (c.acting, c.receiving) in by_pair

    state = emotion_vs_person(TALA, other, "Test Subject")
    assert state.mode == "person"
    assert state.subject == "Test Subject"


def test_identical_charts_are_harmonious():
    """A chart against itself is all exact conjunctions -- it must not read as
    challenging."""
    state = emotion_vs_person(TALA, TALA, "self")
    assert state.primary_emotion not in {"terror", "rage", "despair", "dread"}


# ---------------------------------------------------------------------------
# Derivation behaviour
# ---------------------------------------------------------------------------


def test_empty_contacts_fall_back_to_neutral():
    state = derive_emotion([], mode="time")
    assert state.primary_emotion == "neutral"
    assert state.intensity == 0.0


def test_weak_contacts_are_filtered_as_noise():
    weak = [Contact("Saturn", "square", "Sun", 7.9, True, 0.01)]
    assert derive_emotion(weak).primary_emotion == "neutral"


def test_challenging_saturn_reads_as_dread_not_resolve():
    """Aspect quality must flip the emotion table, not just scale it."""
    harmonious = derive_emotion([Contact("Saturn", "trine", "Sun", 0.1, False, 0.99)])
    challenging = derive_emotion([Contact("Saturn", "square", "Sun", 0.1, True, 0.99)])
    assert harmonious.primary_emotion == "resolve"
    assert challenging.primary_emotion == "dread"


def test_transit_contacts_preserve_direction():
    transits = EphemerisEngine().compute_transits(TALA, WHEN)
    contacts = contacts_from_transits(transits)
    assert contacts
    for c, t in zip(contacts, transits):
        assert c.acting == t.planet
        assert c.receiving == t.natal_planet


# ---------------------------------------------------------------------------
# The regression that actually happened
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_virtual_mcp_tool_resolves():
    """
    THE regression guard. Before the fix this raised
    ``KeyError: MCP server 'astro-emotion' is not registered`` and every caller
    swallowed it, so no turn ever carried an emotional state.
    """
    from python.harness.mcp_manager import mcp_manager

    result = await mcp_manager.call_tool(
        "astro-emotion", "get_emotional_state", {"agent_id": None}
    )

    assert isinstance(result, dict)
    state = result.get("emotional_state")
    assert isinstance(state, dict), "call sites read result['emotional_state']"
    assert state.get("primary_emotion"), "primary_emotion must be non-empty"
    assert state["primary_emotion"] != ""


@pytest.mark.asyncio
async def test_call_site_parsing_produces_prefix():
    """Reproduce memory.py's exact parsing and assert it yields a usable prefix."""
    from python.harness.mcp_manager import mcp_manager

    res = await mcp_manager.call_tool(
        "astro-emotion", "get_emotional_state", {"agent_id": None}
    )
    emotion_state = res.get("emotional_state") or res.get("value") or res
    primary = emotion_state.get("primary_emotion", "") or str(emotion_state)
    prefix = f"[Emotional State: {primary}]"

    assert primary
    assert prefix != "[Emotional State: ]"


# ---------------------------------------------------------------------------
# Emotional continuity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inertia_decays_with_elapsed_time(tmp_path, monkeypatch):
    """
    Continuity is the point of the inertia layer: consecutive turns must feel
    like the same being, while a reading days later is free to have moved on.
    """
    import python.harness.emotion_service as svc

    monkeypatch.setattr(svc, "_state_path", lambda: tmp_path / "emotion_state.json")

    base = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    await svc.get_emotional_state(when=base)

    soon = await svc.get_emotional_state(when=base + timedelta(seconds=30))
    later = await svc.get_emotional_state(when=base + timedelta(days=5))

    assert soon["emotional_state"]["inertia_applied"] > 0.5
    assert later["emotional_state"]["inertia_applied"] < 0.05


@pytest.mark.asyncio
async def test_state_persists_between_calls(tmp_path, monkeypatch):
    import python.harness.emotion_service as svc

    path = tmp_path / "emotion_state.json"
    monkeypatch.setattr(svc, "_state_path", lambda: path)

    await svc.get_emotional_state(when=datetime(2026, 8, 8, 12, 0, tzinfo=UTC))
    assert path.exists(), "emotional state must persist so continuity survives restart"


# ---------------------------------------------------------------------------
# The gate in front of the engine
# ---------------------------------------------------------------------------


def test_emotional_modes_cover_rp_and_hybrid():
    from python.harness.chat_mode import is_emotional_mode

    assert is_emotional_mode("rp")
    # hybrid is treated as RP-flavoured everywhere else in server.py (retrieval
    # limits, persona injection); excluding it here would silently drop her
    # emotional state from hybrid conversations while keeping the rest.
    assert is_emotional_mode("hybrid")
    assert not is_emotional_mode("assistant")


def test_chat_mode_comes_from_file_not_agent_config(tmp_path, monkeypatch):
    """
    THE regression guard for the blocker.

    All three emotion call sites used to read ``agents.config['chat_mode']``.
    Nothing has ever written that key -- ``agent.set_chat_mode`` writes
    chat_mode.json and flips is_active, and no other writer exists -- so the
    guard was permanently false and no turn was ever stamped, regardless of
    what the UI reported the mode to be.
    """
    import python.harness.chat_mode as cm

    path = tmp_path / "chat_mode.json"
    path.write_text('{"mode": "rp"}', encoding="utf-8")
    monkeypatch.setattr(cm, "get_chat_mode_file_path", lambda: path)

    assert cm.get_current_chat_mode() == "rp"
    assert cm.is_emotional_mode()


def test_emotion_is_injected_into_the_model_context():
    """
    The engine is pointless if its output never reaches the model.

    For the life of the feature, `current_emotion` was used in exactly one
    place: prepended to the *retrieval query*. It never entered `messages`, so
    Tala could not perceive or express her own emotional state -- the engine
    only biased which memories surfaced.

    Source-level assertion rather than a live inference run, because the
    injection sits inline in a very large websocket handler. It catches the
    regression that matters: someone removing the context injection, or
    reinstating the query splice.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent / "python" / "harness" / "server.py"
    ).read_text(encoding="utf-8")

    assert "YOUR CURRENT EMOTIONAL STATE" in src, (
        "emotional state is no longer injected into the model's context"
    )
    assert "system_context_additions.append(\n                            \"=== YOUR CURRENT EMOTIONAL STATE ===" in src or (
        "YOUR CURRENT EMOTIONAL STATE" in src and "system_context_additions" in src
    )

    # And it must NOT go back into the retrieval query: memory_index is
    # Postgres full-text, so the label contributes tokens present in no
    # document and dilutes the real search terms.
    assert 'retrieval_query = f"[Current emotional state:' not in src, (
        "emotional state must not be spliced into the retrieval query"
    )


def test_no_call_site_reads_chat_mode_from_agent_config():
    """
    Guards against the bug being reintroduced. If someone adds
    ``config.get("chat_mode")`` back into a call site, the emotion silently
    stops working again -- exactly the failure this whole area suffered from.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for rel in ("python/harness/memory.py", "python/harness/server.py"):
        text = (root / rel).read_text(encoding="utf-8")
        for m in re.finditer(r'\.get\(\s*["\']chat_mode["\']', text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "chat_mode must come from chat_mode.py, not an agent-config lookup; "
        f"found at {offenders}"
    )


@pytest.mark.asyncio
async def test_recorded_turn_is_stamped_with_emotion(tmp_path, monkeypatch):
    """
    End-to-end: a completed conversation turn carries the emotional prefix that
    python/harness/memory.py was always intended to write.
    """
    import python.harness.chat_mode as cm
    import python.harness.db as db_mod
    import python.harness.emotion_service as svc
    import python.harness.memory as memory

    mode_file = tmp_path / "chat_mode.json"
    mode_file.write_text('{"mode": "rp"}', encoding="utf-8")
    monkeypatch.setattr(cm, "get_chat_mode_file_path", lambda: mode_file)
    monkeypatch.setattr(svc, "_state_path", lambda: tmp_path / "emotion_state.json")

    stored: dict = {}

    async def fake_fetchrow(query, *a):
        return {"id": "tala-rp"} if "agents" in query else None

    async def fake_execute(query, *a):
        if "conversation_turns" in query:
            stored["content"] = a[2]

    class FakeManager:
        async def store(self, **kw):
            stored["meta"] = kw.get("meta")
            return {"id": "x"}

    monkeypatch.setattr(db_mod, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(db_mod, "execute", fake_execute)
    monkeypatch.setattr(memory, "get_manager", lambda: FakeManager())
    monkeypatch.setattr(memory, "_build_learning_node_payload", lambda **kw: None)

    await memory.handle_memory_record_completed_turn(
        {
            "session_id": "s1",
            "role": "assistant",
            "content": "The aft manifold is holding at nominal.",
            "tool_calls": [],
        }
    )

    assert stored.get("content", "").startswith("[Emotional State: ")
    assert stored["meta"]["emotion"]["primary_emotion"]


@pytest.mark.asyncio
async def test_assistant_mode_is_not_stamped(tmp_path, monkeypatch):
    """The gate must still gate -- assistant mode gets no emotional prefix."""
    import python.harness.chat_mode as cm
    import python.harness.db as db_mod
    import python.harness.memory as memory

    mode_file = tmp_path / "chat_mode.json"
    mode_file.write_text('{"mode": "assistant"}', encoding="utf-8")
    monkeypatch.setattr(cm, "get_chat_mode_file_path", lambda: mode_file)

    stored: dict = {}

    async def fake_fetchrow(query, *a):
        return {"id": "tala-core"} if "agents" in query else None

    async def fake_execute(query, *a):
        if "conversation_turns" in query:
            stored["content"] = a[2]

    class FakeManager:
        async def store(self, **kw):
            stored["meta"] = kw.get("meta")
            return {"id": "x"}

    monkeypatch.setattr(db_mod, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(db_mod, "execute", fake_execute)
    monkeypatch.setattr(memory, "get_manager", lambda: FakeManager())
    monkeypatch.setattr(memory, "_build_learning_node_payload", lambda **kw: None)

    await memory.handle_memory_record_completed_turn(
        {"session_id": "s1", "role": "assistant", "content": "Done.", "tool_calls": []}
    )

    assert not stored.get("content", "").startswith("[Emotional State: ")


# ---------------------------------------------------------------------------
# Relational disposition (user profile)
# ---------------------------------------------------------------------------


def test_synastry_is_constant_over_time():
    """
    Synastry between two fixed natal charts does not vary with time. This is
    why it's treated as a permanent disposition biasing every reading rather
    than as a time-varying emotion of its own -- if this ever became
    time-dependent, the whole climate/weather split would be wrong.
    """
    other = Birthdate(1990, 3, 14, 9.0)
    a = emotion_vs_person(TALA, other, "x")
    b = emotion_vs_person(TALA, other, "x")
    assert a.scores == b.scores
    assert a.primary_emotion == b.primary_emotion


@pytest.mark.asyncio
async def test_relational_baseline_reads_profile_precedence(tmp_path, monkeypatch):
    """
    Personal info comes from the user profile, using the same precedence as
    handle_astro_synastry: identity.birthdate -> pii_vault.dob -> birthdate.
    """
    import python.harness.emotion_service as svc

    async def fake_fetchrow(query, *args):
        if "profiles" in query:
            return {"config": {"pii_vault": {"dob": "1990-03-14"}}}
        raise RuntimeError("Database pool is not initialised.")

    monkeypatch.setattr(svc, "_state_path", lambda: tmp_path / "s.json")
    import python.harness.db as db_mod
    monkeypatch.setattr(db_mod, "fetchrow", fake_fetchrow)

    birth, source = await svc._resolve_user_birthdate()
    assert source == "user-profile"
    assert (birth.year, birth.month, birth.day) == (1990, 3, 14)


@pytest.mark.asyncio
async def test_profile_dob_never_appears_in_payload(tmp_path, monkeypatch):
    """
    The user's date of birth is PII. The emotional state may report *whether* a
    profile chart was found, never the date itself.
    """
    import python.harness.emotion_service as svc

    dob = "1990-03-14"

    async def fake_fetchrow(query, *args):
        if "profiles" in query:
            return {"config": {"identity": {"birthdate": dob}}}
        raise RuntimeError("Database pool is not initialised.")

    monkeypatch.setattr(svc, "_state_path", lambda: tmp_path / "s.json")
    import python.harness.db as db_mod
    monkeypatch.setattr(db_mod, "fetchrow", fake_fetchrow)

    res = await svc.get_emotional_state()
    state = res["emotional_state"]

    assert state["relational_source"] == "user-profile"
    assert dob not in json_dumps(state)
    # Tala's own chart is not personal data and may be reported.
    assert state["birthdate"] == "1977-07-20"


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj)


@pytest.mark.asyncio
async def test_missing_profile_still_produces_emotion(tmp_path, monkeypatch):
    """No user profile must not mean no emotion."""
    import python.harness.emotion_service as svc

    monkeypatch.setattr(svc, "_state_path", lambda: tmp_path / "s.json")
    res = await svc.get_emotional_state()
    state = res["emotional_state"]

    assert state["relational_source"] == "missing"
    assert state["relational_weight"] == 0.0
    assert state["primary_emotion"]


@pytest.mark.asyncio
async def test_missing_database_degrades_to_origin_chart(tmp_path, monkeypatch):
    """
    No DB pool (CLI, tests, early startup) must still produce an emotion rather
    than reintroducing the silent-nothing failure.
    """
    import python.harness.emotion_service as svc

    monkeypatch.setattr(svc, "_state_path", lambda: tmp_path / "emotion_state.json")
    res = await svc.get_emotional_state(agent_id="does-not-exist")
    state = res["emotional_state"]

    assert state["birth_source"] == "tala-origin"
    assert state["primary_emotion"]
