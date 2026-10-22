"""
Tala's complete natal chart: 1977-07-20, 5:00 pm, Clearwater, Florida.

The geometry is checked against facts that do not depend on the implementation
(the Sun's longitude on that date, day vs night chart, house cusps wrapping the
zodiac once), not against numbers copied from the output.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from python.astrology import ephemeris as eph
from python.astrology import natal
from python.astrology.emotion import contacts_from_transits, derive_emotion

pytestmark = pytest.mark.skipif(
    not eph.SWISSEPH_AVAILABLE, reason="the natal chart needs the real Swiss Ephemeris"
)


@pytest.fixture(scope="module")
def chart():
    return natal._build_natal_chart(eph.TALA_NATAL)


# --- the birth facts ------------------------------------------------------------------


def test_birth_hour_is_5pm_florida_daylight_time_in_ut():
    assert eph.TALA_NATAL.hour == 17.0 + 4.0  # 17:00 EDT (UTC-4) = 21:00 UT
    assert (eph.TALA_NATAL.year, eph.TALA_NATAL.month, eph.TALA_NATAL.day) == (1977, 7, 20)


def test_florida_was_on_daylight_time_that_day():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        tz = zoneinfo.ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("no time-zone database on this machine")
    local = datetime(1977, 7, 20, 17, 0, tzinfo=tz)
    assert local.utcoffset().total_seconds() == -4 * 3600
    assert local.astimezone(eph.timezone.utc).hour == 21


def test_clearwater_coordinates_are_north_and_west():
    assert 27.9 < eph.TALA_NATAL.latitude < 28.1
    assert -83.0 < eph.TALA_NATAL.longitude < -82.6  # east-positive: west is negative


# --- chart geometry -----------------------------------------------------------------------


def test_the_sun_is_where_astronomy_puts_it_on_that_date(chart):
    sun = chart.bodies["Sun"]
    assert sun.sign == "Cancer" and 27.0 < sun.degree_in_sign < 29.0


def test_it_is_a_day_chart_because_the_sun_is_above_the_horizon(chart):
    assert chart.bodies["Sun"].house in range(7, 13)
    assert chart.sect == "day"


def test_angles_are_consistent(chart):
    asc, mc = chart.angles["Ascendant"], chart.angles["Midheaven"]
    assert chart.cusps[0] == pytest.approx(asc)
    assert chart.cusps[9] == pytest.approx(mc, abs=1e-6)
    assert chart.angles["Descendant"] == pytest.approx((asc + 180) % 360)
    assert chart.angles["Imum Coeli"] == pytest.approx((mc + 180) % 360)


def test_house_cusps_wrap_the_zodiac_exactly_once(chart):
    total = sum((chart.cusps[(i + 1) % 12] - chart.cusps[i]) % 360 for i in range(12))
    assert total == pytest.approx(360.0)


def test_every_body_sits_in_the_house_its_longitude_falls_in(chart):
    for body in chart.bodies.values():
        assert 1 <= body.house <= 12
        assert body.house == natal._house_of(body.longitude, chart.cusps)
    assert natal._house_of(chart.angles["Ascendant"] + 0.01, chart.cusps) == 1


def test_known_placements(chart):
    assert chart.bodies["Moon"].sign == "Virgo"
    assert chart.bodies["Neptune"].retrograde is True  # Neptune was retrograde that summer
    assert natal._sign_of(chart.angles["Ascendant"]) == "Sagittarius"
    assert chart.chart_ruler == "Jupiter"  # ruler of Sagittarius


def test_every_longitude_agrees_with_the_transit_engine(chart):
    positions = eph.EphemerisEngine().get_chart_positions(eph.TALA_NATAL)
    for name, lon in positions.items():
        assert chart.bodies[name].longitude == pytest.approx(lon, abs=1e-6)


# --- dignity, balance, aspects ------------------------------------------------------------------


@pytest.mark.parametrize("planet,sign,expected", [
    ("Mars", "Aries", "domicile"), ("Mars", "Capricorn", "exaltation"),
    ("Mars", "Libra", "detriment"), ("Mars", "Cancer", "fall"),
    ("Moon", "Cancer", "domicile"), ("Moon", "Taurus", "exaltation"),
    ("Venus", "Pisces", "exaltation"), ("Venus", "Virgo", "fall"),
    ("Jupiter", "Gemini", "detriment"), ("Saturn", "Leo", "detriment"),
    ("Sun", "Gemini", "peregrine"),
])
def test_essential_dignity(planet, sign, expected):
    assert natal._dignity_of(planet, sign) == expected


def test_dignity_scores_order_strength():
    s = natal.DIGNITY_SCORE
    assert s["domicile"] > s["exaltation"] > s["peregrine"] > s["detriment"] > s["fall"]


def test_balance_counts_every_weighted_point_once(chart):
    total = sum(natal.BALANCE_WEIGHT.values())
    assert sum(chart.elements.values()) == pytest.approx(total)
    assert sum(chart.modalities.values()) == pytest.approx(total)


def test_natal_aspects_use_the_transit_orbs(chart):
    for a in chart.aspects:
        assert a.orb <= eph.ASPECT_ORBS[a.aspect]
        assert a.is_hard == (a.aspect in ("square", "opposition"))
        assert {a.a, a.b} != {"Ascendant", "Midheaven"}
    pairs = {(a.a, a.b, a.aspect) for a in chart.aspects}
    assert ("Moon", "Venus", "square") in pairs


def test_a_chart_needs_a_birth_place():
    natal._build_natal_chart.cache_clear()
    with pytest.raises(ValueError, match="birth place"):
        natal._build_natal_chart(eph.Birthdate(1977, 7, 20, 21.0))


def test_chart_summary_needs_a_place_and_never_raises(monkeypatch):
    assert natal.chart_summary(eph.Birthdate(1977, 7, 20, 21.0)) is None
    summary = natal.chart_summary(eph.TALA_NATAL)
    assert summary["ascendant"].endswith("Sagittarius") and summary["sect"] == "day"

    def boom(context):
        raise RuntimeError("pyswisseph is not installed")

    natal._build_natal_chart.cache_clear()
    monkeypatch.setattr(eph, "require_precise_ephemeris", boom)
    assert natal.chart_summary(eph.TALA_NATAL) is None  # a status surface must not fail
    natal._build_natal_chart.cache_clear()


def test_summary_is_json_friendly(chart):
    import json

    json.dumps(chart.summary())


# --- one resolver for every date-only persona config -----------------------------------------------


def test_her_date_alone_resolves_to_her_complete_birth_data():
    assert natal.birth_for("1977-07-20") is eph.TALA_NATAL
    assert natal.birth_for("1977-07-20", None) is eph.TALA_NATAL


def test_midnight_is_a_real_hour_not_an_unset_one():
    """birth_hour 0.0 used to read as unset and silently became noon."""
    assert natal.birth_for("1977-07-20", 0.0) == eph.Birthdate(1977, 7, 20, 0.0)
    assert natal.birth_for("1990-03-14", 0) == eph.Birthdate(1990, 3, 14, 0.0)
    assert natal.birth_for("1990-03-14", "") == eph.Birthdate(1990, 3, 14, 12.0)
    assert natal.birth_for("1990-03-14", "junk") == eph.Birthdate(1990, 3, 14, 12.0)


def test_an_explicit_hour_or_another_date_keeps_the_old_default():
    explicit = natal.birth_for("1977-07-20", 6.5)
    assert explicit == eph.Birthdate(1977, 7, 20, 6.5)
    assert natal.birth_for("1990-03-14") == eph.Birthdate(1990, 3, 14, 12.0)


@pytest.mark.parametrize("bad", ["", "1977-07", "not-a-date", "1977-13-40", None])
def test_unreadable_dates_resolve_to_nothing(bad):
    assert natal.birth_for(bad) is None


@pytest.mark.asyncio
async def test_a_date_only_agent_config_gets_the_complete_chart(monkeypatch):
    from python.harness import db, emotion_service

    async def fake_fetchrow(query, *args):
        return {"config": {"birthdate": "1977-07-20"}}

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    birth, source = await emotion_service._resolve_birthdate("tala-core")
    assert birth is eph.TALA_NATAL and source == "agent-config"


# --- the corpus agrees with her real chart ---------------------------------------------------------


def test_every_story_label_is_what_her_real_chart_computes_at_its_anchor():
    """The corpus validator's rule, asserted directly: moving her birth hour
    must not leave a story claiming an emotion her chart does not produce."""
    corpus = Path(__file__).resolve().parents[1] / "data" / "training_corpus" / "pre-weavor"
    files = sorted(corpus.glob("*.html"))
    if not files:
        pytest.skip("no training corpus checked out")
    engine = eph.EphemerisEngine()
    checked, mismatched = 0, []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        anchor = re.search(r'data-anchor-datetime="([^"]+)"', text)
        label = re.search(r'data-engine-emotion="([^"]+)"', text)
        if not (anchor and label):
            continue
        when = datetime.fromisoformat(anchor.group(1))
        derived = derive_emotion(contacts_from_transits(engine.compute_transits(eph.TALA_NATAL, when)))
        checked += 1
        if derived.primary_emotion != label.group(1):
            mismatched.append(path.name)
    assert checked, "no story declared an anchor"
    assert not mismatched, f"{len(mismatched)} stories disagree with her chart, e.g. {mismatched[:3]}"


# --- the persona nodes state the same birth data the code uses ------------------------------------------------

PERSONA_NODES = ("persona-tala-core", "persona-tala-rp", "persona-tala-origin")


def _persona_config(name: str) -> dict:
    import json

    path = Path(__file__).resolve().parents[1] / "nodes" / f"{name}.html"
    text = path.read_text(encoding="utf-8")
    state = re.search(r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', text, re.S)
    return json.loads(state.group(1))["config"]


@pytest.mark.parametrize("name", PERSONA_NODES)
def test_persona_node_birth_data_matches_the_code(name):
    cfg, tala = _persona_config(name), eph.TALA_NATAL
    assert cfg["birthdate"] == "1977-07-20"
    assert cfg["birth_time_local"] == "17:00"
    assert "Clearwater" in cfg["birth_place"]
    assert cfg["birth_hour"] == tala.hour
    assert cfg["birth_latitude"] == pytest.approx(tala.latitude)
    assert cfg["birth_longitude"] == pytest.approx(tala.longitude)


@pytest.mark.parametrize("name", PERSONA_NODES)
def test_persona_node_config_resolves_to_her_complete_chart(name):
    cfg = _persona_config(name)
    full = natal.birth_for(cfg["birthdate"], cfg["birth_hour"], cfg["birth_latitude"], cfg["birth_longitude"])
    assert full == eph.TALA_NATAL
    # the hour alone (a reader that ignores the place keys) still lands on the full chart
    assert natal.birth_for(cfg["birthdate"], cfg["birth_hour"]) == eph.TALA_NATAL


def test_a_configured_place_is_kept_for_any_persona():
    born = natal.birth_for("1990-03-14", 6.5, 40.7128, -74.0060)
    assert (born.hour, born.latitude, born.longitude) == (6.5, 40.7128, -74.0060)
    assert natal.birth_for("1977-07-20", 6.5) == eph.Birthdate(1977, 7, 20, 6.5)  # her date, a different hour


@pytest.mark.asyncio
async def test_an_agent_config_with_her_hour_and_place_gets_the_complete_chart(monkeypatch):
    from python.harness import db, emotion_service

    cfg = _persona_config("persona-tala-rp")

    async def fake_fetchrow(query, *args):
        return {"config": cfg}

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    birth, source = await emotion_service._resolve_birthdate("tala-rp")
    assert birth == eph.TALA_NATAL and source == "agent-config"
