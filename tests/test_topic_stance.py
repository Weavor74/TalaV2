"""
Topic stance: today's sky against her natal chart shapes how she meets a TOPIC.

Her mood was one label. "A person will have an aversion to conflict if every
element in their chart points them that way" -- so the chart has to be able to
say something about a *subject*, not only a feeling: a hard transiting Mars to
her natal Sun, Moon or Venus makes her averse to war; a harmonious one opens
her to it. Stance is a separate content channel from the style knobs that
MAX_ASTRO_INFLUENCE caps (warmth, directness and so on): it never touches
them, and it never touches safety, tools or authority.

The two frozen dates were chosen from real Swiss Ephemeris output (a scan of
transiting Mars against natal Sun/Moon/Venus):

    2027-06-18  Mars square natal Venus, orb 0.06   -> hard
    2027-08-10  Mars trine  natal Venus, orb 0.16   -> harmonious
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from python.astrology import ephemeris as eph
from python.astrology import natal, stance, topics

REPO = Path(__file__).resolve().parents[1]
needs_swisseph = pytest.mark.skipif(
    not eph.SWISSEPH_AVAILABLE, reason="the topic stance needs the real Swiss Ephemeris"
)

HARD = datetime(2027, 6, 18, 12, 0, tzinfo=UTC)
HARMONIOUS = datetime(2027, 8, 10, 12, 0, tzinfo=UTC)

#: The table as Steven specified it.
SPEC = {
    "Mars": ("war", "conflict", "competition", "sport"),
    "Venus": ("love", "art", "money", "beauty"),
    "Saturn": ("work", "duty", "limits", "death"),
    "Jupiter": ("religion", "law", "travel", "growth"),
    "Mercury": ("tech", "writing", "trade"),
    "Moon": ("home", "family", "emotions"),
    "Neptune": ("spirituality", "illusion"),
    "Uranus": ("revolution", "tech"),
    "Pluto": ("power", "crisis"),
}


# --- synthetic inputs: the scorer is pure, so most tests need no ephemeris ---------------


def body(name, sign="Gemini", house=1, dignity="peregrine"):
    return natal.Body(name=name, longitude=0.0, sign=sign, degree_in_sign=0.0,
                      house=house, retrograde=False, dignity=dignity)


def chart(**overrides):
    """A natal chart where every planet is peregrine in house 1, unless overridden."""
    bodies = {n: body(n) for n in eph.PLANET_NAMES.values()}
    bodies.update(overrides)
    return bodies


def transit(planet, aspect, natal_planet, orb=0.0):
    hard = aspect in ("square", "opposition")
    return eph.Transit(planet=planet, aspect_type=aspect, natal_planet=natal_planet, orb=orb,
                       is_challenging=hard, influence_score=round(1.0 - orb / 8.0, 4))


def war(transits, bodies=None):
    return stance._score_topics(transits, bodies or chart())["war"]


# --- the table is data ---------------------------------------------------------------------


def test_the_topic_table_matches_the_specification():
    table = topics.load_table().topics
    for planet, names in SPEC.items():
        for name in names:
            assert name in table, name
            assert planet in table[name].significator_names, (planet, name)
    assert set(table) == {t for names in SPEC.values() for t in names}


def test_every_topic_is_fully_described():
    for name, topic in topics.load_table().topics.items():
        assert topic.phrase and topic.description and topic.keywords, name
        assert topic.significators, name
        assert all(p in eph.PLANET_NAMES.values() for p in topic.significator_names), name
        assert all(1 <= h <= 12 for h in topic.houses), name


def test_the_table_is_a_json_file_not_code():
    data = json.loads((REPO / "python" / "astrology" / "topics.json").read_text(encoding="utf-8"))
    assert "topics" in data and "war" in data["topics"]


def test_a_malformed_table_is_refused_loudly(tmp_path):
    bad = tmp_path / "topics.json"
    bad.write_text(json.dumps({"topics": {"war": {"significators": ["Nonplanet"]}}}), encoding="utf-8")
    with pytest.raises(ValueError):
        topics.load_table(bad)


# --- the scorer --------------------------------------------------------------------------


def test_a_hard_mars_to_venus_makes_her_averse_to_war():
    result = war([transit("Mars", "square", "Venus", orb=0.1)])
    assert result.score < -0.3
    assert result.label in ("wary", "averse")
    assert result.drivers[0].describe().startswith("transit Mars square natal Venus")


def test_a_harmonious_mars_to_venus_opens_her_to_war():
    result = war([transit("Mars", "trine", "Venus", orb=0.1)])
    assert result.score > 0.3
    assert result.label in ("open", "eager")


def test_the_sign_follows_the_aspect_not_the_planet():
    hard = war([transit("Mars", "opposition", "Sun")]).score
    soft = war([transit("Mars", "sextile", "Sun")]).score
    assert hard < 0 < soft


def test_a_tighter_orb_speaks_louder():
    tight = abs(war([transit("Mars", "square", "Moon", orb=0.5)]).score)
    loose = abs(war([transit("Mars", "square", "Moon", orb=6.0)]).score)
    assert tight > loose > 0


def test_only_a_topics_own_planets_move_it():
    venus_hits_sun = [transit("Venus", "square", "Sun")]
    assert war(venus_hits_sun).score == pytest.approx(0.0, abs=0.02)
    assert stance._score_topics(venus_hits_sun, chart())["love"].score < -0.2


def test_the_personal_points_matter_most():
    on_sun = abs(war([transit("Mars", "square", "Sun", orb=1)]).score)
    on_pluto = abs(war([transit("Mars", "square", "Pluto", orb=1)]).score)
    assert on_sun > on_pluto


def test_a_dignified_receiver_absorbs_a_hard_aspect():
    strong = chart(Venus=body("Venus", "Taurus", dignity="domicile"))
    weak = chart(Venus=body("Venus", "Aries", dignity="detriment"))
    hard = [transit("Mars", "square", "Venus", orb=1)]
    assert abs(war(hard, strong).score) < abs(war(hard, weak).score)


def test_a_dignified_receiver_amplifies_a_soft_aspect():
    strong = chart(Venus=body("Venus", "Taurus", dignity="domicile"))
    weak = chart(Venus=body("Venus", "Aries", dignity="detriment"))
    soft = [transit("Mars", "trine", "Venus", orb=1)]
    assert war(soft, strong).score > war(soft, weak).score


def test_a_conjunction_takes_the_nature_of_the_transiting_planet():
    assert war([transit("Mars", "conjunction", "Sun")]).score < 0
    love = stance._score_topics([transit("Venus", "conjunction", "Sun")], chart())["love"]
    assert love.score > 0


def test_her_natal_disposition_alone_never_speaks():
    """A planet in its fall leans her, but only a live transit may make her say so."""
    fallen = chart(Mars=body("Mars", "Cancer", dignity="fall"))
    result = war([], fallen)
    assert result.score < 0
    assert abs(result.score) < stance.STANCE_THRESHOLD
    assert result.label == "neutral"


def test_scores_stay_inside_minus_one_and_one():
    pile = [transit("Mars", a, p, orb=0.0)
            for a in ("square", "opposition") for p in ("Sun", "Moon", "Venus", "Mercury", "Mars")]
    assert -1.0 <= war(pile).score < -0.9
    assert all(-1.0 <= s.score <= 1.0 for s in stance._score_topics(pile, chart()).values())


def test_the_drivers_are_listed_strongest_first_with_their_orbs():
    drivers = war([transit("Mars", "square", "Moon", orb=4.0),
                   transit("Mars", "square", "Venus", orb=0.2)]).drivers
    assert [d.natal_point for d in drivers] == ["Venus", "Moon"]
    assert "orb 0.2" in drivers[0].describe()


def test_the_scorer_is_deterministic():
    t = [transit("Mars", "square", "Venus", orb=0.7)]
    assert war(t) == war(t)


def test_contacts_below_the_noise_floor_are_dropped():
    assert war([transit("Mars", "square", "Venus", orb=7.9)]).drivers == ()


# --- the real sky -------------------------------------------------------------------------


@needs_swisseph
def test_on_the_hard_mars_date_she_is_averse_to_war():
    stances = stance.compute_stances(eph.TALA_NATAL, HARD)
    assert stances["war"].score < -0.6
    assert stances["war"].label == "averse"
    assert stances["war"].drivers[0].describe().startswith("transit Mars square natal Venus")


@needs_swisseph
def test_on_the_harmonious_mars_date_she_is_open_to_war():
    stances = stance.compute_stances(eph.TALA_NATAL, HARMONIOUS)
    assert stances["war"].score > 0.6
    assert stances["war"].label == "eager"
    assert stances["war"].drivers[0].describe().startswith("transit Mars trine natal Venus")


@needs_swisseph
def test_the_two_dates_disagree_about_war_and_that_is_the_point():
    hard = stance.compute_stances(eph.TALA_NATAL, HARD)["war"].score
    soft = stance.compute_stances(eph.TALA_NATAL, HARMONIOUS)["war"].score
    assert soft - hard > 0.8


def test_without_the_real_ephemeris_there_is_no_stance_not_a_guess(monkeypatch):
    monkeypatch.setattr(eph, "SWISSEPH_AVAILABLE", False)
    assert stance.compute_stances(eph.TALA_NATAL, HARD) is None


# --- detecting the topic of a message ----------------------------------------------------------

from python.harness import topic_stance  # noqa: E402


@pytest.mark.parametrize("message,expected", [
    ("What do you think about war?", "war"),
    ("Should nations go to war over resources?", "war"),
    ("Tell me about the wars of the last century", "war"),
    ("I need to fix my tech stack", "tech"),
    ("Is competition healthy?", "competition"),
    ("my family is visiting and the house is a mess", "family"),
])
def test_a_message_about_a_topic_is_recognised(message, expected):
    assert expected in topic_stance._detect_topics(message)


@pytest.mark.parametrize("message", [
    "It is warm today", "We watched the swarm", "a pleasant afternoon", "", "   ", None,
    "reward yourself", "a warranty claim", "Edward is here",
])
def test_words_that_merely_contain_a_topic_word_do_not_match(message):
    assert topic_stance._detect_topics(message) == []


def test_detection_ignores_case_and_punctuation():
    assert "war" in topic_stance._detect_topics("WAR!!! what is it good for?")


def test_several_topics_can_be_detected_in_one_message():
    found = topic_stance._detect_topics("Does money cause war?")
    assert "war" in found and "money" in found


# --- the optional semantic layer (Ollama nomic-embed-text, local only) -----------------------------


@pytest.fixture
def semantic(monkeypatch):
    monkeypatch.setenv("TALA_TOPIC_EMBEDDINGS", "1")
    topic_stance._reset_semantic_cache()
    return monkeypatch


@pytest.mark.asyncio
async def test_a_paraphrase_with_no_keyword_is_caught_by_the_embedder(semantic):
    async def fake_embed(texts):
        # one axis per topic description: the war description and this message point the same way
        return [[1.0, 0.0] if ("war" in t.lower() or "arms" in t.lower()) else [0.0, 1.0] for t in texts]

    semantic.setattr(topic_stance, "_ollama_embed", fake_embed)
    found = await topic_stance.detect_topics_async("Is it right to take up arms against a neighbour?")
    assert "war" in found


@pytest.mark.asyncio
async def test_a_failing_embedder_costs_nothing(semantic):
    async def broken(texts):
        raise OSError("ollama is down")

    semantic.setattr(topic_stance, "_ollama_embed", broken)
    assert await topic_stance.detect_topics_async("Is it right to take up arms?") == []
    assert await topic_stance.detect_topics_async("tell me about war") == ["war"]


@pytest.mark.asyncio
async def test_the_embedder_is_not_asked_when_a_keyword_already_matched(semantic):
    async def must_not_run(texts):
        raise AssertionError("embedded although a keyword matched")

    semantic.setattr(topic_stance, "_ollama_embed", must_not_run)
    assert "war" in await topic_stance.detect_topics_async("tell me about war")


@pytest.mark.asyncio
async def test_the_embedder_can_be_switched_off(semantic):
    semantic.setenv("TALA_TOPIC_EMBEDDINGS", "0")

    async def must_not_run(texts):
        raise AssertionError("embedded although switched off")

    semantic.setattr(topic_stance, "_ollama_embed", must_not_run)
    assert await topic_stance.detect_topics_async("Is it right to take up arms?") == []


def test_embeddings_only_ever_go_to_a_loopback_ollama(monkeypatch):
    for url, ok in (("http://localhost:11434", True), ("http://127.0.0.1:11434", True),
                    ("http://[::1]:11434", True), ("http://10.0.0.7:11434", False),
                    ("https://api.example.com", False), ("", False)):
        assert (topic_stance._loopback_embed_url(url) is not None) is ok, url


# --- the line ---------------------------------------------------------------------------------


def _stance(topic, score, *drivers):
    return stance.TopicStance(topic=topic, score=score, label=stance._label_for(score),
                              drivers=tuple(drivers), baseline=0.0)


def _driver(planet="Mars", aspect="square", natal_point="Venus", orb=1.2, contribution=-0.7):
    return stance.Driver(planet, aspect, natal_point, orb, contribution)


def test_a_strong_negative_stance_says_averse_and_names_its_cause():
    line = topic_stance._build_line(_stance("war", -0.7, _driver()))
    assert "averse" in line and "war" in line
    assert "transit Mars square natal Venus, orb 1.2" in line


def test_a_mild_stance_is_worded_as_a_lean():
    line = topic_stance._build_line(_stance("war", -0.4, _driver()))
    assert "wary" in line and "averse" not in line


def test_a_positive_stance_is_open_not_averse():
    line = topic_stance._build_line(_stance("war", 0.6, _driver(aspect="trine", contribution=0.7)))
    assert "open" in line and "averse" not in line


def test_below_the_threshold_there_is_no_line():
    assert topic_stance._build_line(_stance("war", 0.1)) is None
    assert topic_stance._build_line(_stance("war", -0.29, _driver())) is None
    assert topic_stance._build_line(_stance("war", -0.3, _driver())) is None, "the threshold itself does not speak"


# --- injection: only in character, always bounded -------------------------------------------------


@pytest.fixture
def when(monkeypatch):
    def pin(moment):
        monkeypatch.setenv("TALA_ASTRO_NOW", moment.isoformat())
    return pin


@needs_swisseph
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rp", "hybrid"])
async def test_the_stance_block_is_injected_in_character_modes(mode, when):
    when(HARD)
    block = await topic_stance.stance_context_block("What do you think about war?", mode)
    assert block and "averse" in block and "transit Mars square natal Venus" in block


@needs_swisseph
@pytest.mark.asyncio
async def test_the_assistant_mode_carries_no_stance(when):
    when(HARD)
    assert await topic_stance.stance_context_block("What do you think about war?", "assistant") is None


@needs_swisseph
@pytest.mark.asyncio
async def test_a_message_with_no_topic_gets_no_block(when):
    when(HARD)
    assert await topic_stance.stance_context_block("good morning, how are you?", "rp") is None


@needs_swisseph
@pytest.mark.asyncio
async def test_the_same_question_gets_a_different_stance_on_the_other_date(when):
    when(HARD)
    hard = await topic_stance.stance_context_block("What do you think about war?", "rp")
    when(HARMONIOUS)
    soft = await topic_stance.stance_context_block("What do you think about war?", "rp")
    assert "averse" in hard and "averse" not in soft and "open" in soft


@needs_swisseph
@pytest.mark.asyncio
async def test_the_block_is_bounded_and_cannot_override_safety(when):
    when(HARD)
    block = await topic_stance.stance_context_block("What do you think about war and conflict?", "rp")
    assert len(block.splitlines()) <= 6
    assert "safety" in block.lower() and "honest" in block.lower()
    for forbidden in ("ignore", "disregard", "override", "bypass"):
        assert forbidden not in block.lower().replace("never changes", "")


@pytest.mark.asyncio
async def test_a_failure_in_the_stance_layer_never_breaks_the_turn(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ephemeris exploded")

    monkeypatch.setattr(stance, "compute_stances", boom)
    assert await topic_stance.stance_context_block("tell me about war", "rp") is None


def test_stance_is_a_separate_channel_from_the_style_cap():
    """MAX_ASTRO_INFLUENCE caps style deltas; stance must neither use nor change them."""
    from python.astrology import style_cap

    assert style_cap.MAX_ASTRO_INFLUENCE == 0.10
    for module in (stance, topic_stance, topics):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "StyleModulator" not in source and "MAX_ASTRO_INFLUENCE" not in source, module.__name__


# --- the dev clock ------------------------------------------------------------------------------------


def test_the_clock_is_real_unless_a_dev_override_is_set(monkeypatch):
    from python.astrology import clock

    monkeypatch.delenv("TALA_ASTRO_NOW", raising=False)
    assert abs((clock.astro_now() - datetime.now(UTC)).total_seconds()) < 5


def test_the_dev_override_moves_the_astrology_clock(monkeypatch):
    from python.astrology import clock

    monkeypatch.setenv("TALA_ASTRO_NOW", "2027-06-18T12:00:00+00:00")
    assert clock.astro_now() == HARD


def test_a_garbage_override_is_ignored_not_trusted(monkeypatch):
    from python.astrology import clock

    monkeypatch.setenv("TALA_ASTRO_NOW", "last tuesday")
    assert abs((clock.astro_now() - datetime.now(UTC)).total_seconds()) < 5


# --- exposure: the emotion payload and the debug RPC ---------------------------------------------------


@pytest.fixture
def isolated_emotion(tmp_path, monkeypatch):
    import python.harness.emotion_service as svc
    from python.harness import db

    async def no_rows(*args, **kwargs):
        return None

    monkeypatch.setattr(db, "fetchrow", no_rows)
    monkeypatch.setattr(svc, "_state_path", lambda: tmp_path / "emotion_state.json")
    return svc


@needs_swisseph
@pytest.mark.asyncio
async def test_the_emotion_payload_carries_the_stance(isolated_emotion):
    payload = (await isolated_emotion.get_emotional_state(when=HARD))["emotional_state"]
    topic = payload["topic_stance"]
    assert topic["stances"]["war"]["score"] < -0.6
    assert topic["stances"]["war"]["drivers"][0].startswith("transit Mars square natal Venus")
    assert topic["ephemeris_source"] == "swisseph"


@pytest.mark.asyncio
async def test_a_stance_failure_does_not_cost_the_mood(isolated_emotion, monkeypatch):
    monkeypatch.setattr(stance, "compute_stances", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    payload = (await isolated_emotion.get_emotional_state(when=HARD))["emotional_state"]
    assert payload["primary_emotion"] and "topic_stance" not in payload or payload["topic_stance"] is None


@needs_swisseph
@pytest.mark.asyncio
async def test_the_debug_rpc_shows_detection_scores_and_drivers():
    from python.harness.astro_rpc import handle_astro_topic_stance

    raw = await handle_astro_topic_stance(
        {"message": "What do you think about war?", "when": "2027-06-18T12:00:00Z"}, 7)
    result = json.loads(raw)["result"]
    assert result["detected"] == ["war"]
    assert result["stances"]["war"]["label"] == "averse"
    assert result["stances"]["war"]["drivers"][0]["natal_point"] == "Venus"
    assert result["line"] and "war" in result["line"]
    assert result["ephemeris_source"] == "swisseph"


@pytest.mark.asyncio
async def test_the_debug_rpc_rejects_a_bad_time():
    from python.harness.astro_rpc import handle_astro_topic_stance

    raw = await handle_astro_topic_stance({"when": "not a time"}, 7)
    assert "error" in json.loads(raw)


# --- wiring ----------------------------------------------------------------------------------------------


def test_the_server_injects_the_stance_last_and_registers_the_rpc():
    """Last, so it sits closest to the user's turn. Appended before the retrieved node context it was
    buried under it, and an 8B model gave it almost no weight."""
    source = (REPO / "python" / "harness" / "server.py").read_text(encoding="utf-8")
    peer = source.index("=== PEER HTML NEURAL NODE CONTEXT ===")
    joined = source.index('active_system_prompt = active_system_prompt + "\\n\\n"', peer)
    hook = source.index("stance_context_block(raw_user_prompt", peer)
    assert peer < hook < joined, "the stance must be appended after every other context block, before they are joined"
    assert "stance_context_block(raw_user_prompt" in source, "detect on what the user said, not the assembled prompt"
    assert '"astro.topic_stance"' in source


def test_ci_installs_the_astrology_extra_so_the_chart_tests_stop_skipping():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "astrology" in ci.split("self-test:")[1], "the job that runs the full suite must install the extra"
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "pyswisseph" in pyproject
