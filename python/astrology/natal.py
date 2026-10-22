"""
python/astrology/natal.py -- Tala's complete natal chart.

``ephemeris.py`` knows planetary longitudes and transit aspects. A chart is
more than that: which sign each body is in and how well it sits there
(dignity), which house it occupies, the Ascendant and Midheaven, the chart
ruler, whether the chart is a day or night chart, how the four elements and
three modalities balance, and how her own natal planets aspect each other.
Those are the inputs the topic-stance layer needs ("a person is averse to
conflict when everything in the chart points that way"), and none of them can
be computed from a date alone: houses and angles need the birth time and place.

Tala was born 1977-07-20, 5:00 pm, Clearwater, Florida. Florida observed
daylight time that summer, so that is 17:00 EDT (UTC-4) = 21:00 UT, at
27.9659 N, 82.8001 W. Those facts live in ``ephemeris.py`` (``TALA_NATAL``)
because the transit engine imports them; this module builds the chart from
whatever ``Birthdate`` it is given.

Real Swiss Ephemeris only: houses and speeds are not approximated.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from datetime import datetime

from python.astrology import ephemeris as eph

logger = logging.getLogger(__name__)

SIGNS = (
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
)
ELEMENT_OF_SIGN = ("fire", "earth", "air", "water") * 3
MODALITY_OF_SIGN = ("cardinal", "fixed", "mutable") * 4

#: Traditional rulers (the chart ruler is the ruler of the Ascendant's sign).
TRADITIONAL_RULER = {
    "Aries": "Mars", "Taurus": "Venus", "Gemini": "Mercury", "Cancer": "Moon",
    "Leo": "Sun", "Virgo": "Mercury", "Libra": "Venus", "Scorpio": "Mars",
    "Sagittarius": "Jupiter", "Capricorn": "Saturn", "Aquarius": "Saturn",
    "Pisces": "Jupiter",
}
#: Modern co-rulers, used for dignity only.
_DOMICILE = {
    "Sun": ("Leo",), "Moon": ("Cancer",), "Mercury": ("Gemini", "Virgo"),
    "Venus": ("Taurus", "Libra"), "Mars": ("Aries", "Scorpio"),
    "Jupiter": ("Sagittarius", "Pisces"), "Saturn": ("Capricorn", "Aquarius"),
    "Uranus": ("Aquarius",), "Neptune": ("Pisces",), "Pluto": ("Scorpio",),
}
_EXALTATION = {
    "Sun": "Aries", "Moon": "Taurus", "Mercury": "Virgo", "Venus": "Pisces",
    "Mars": "Capricorn", "Jupiter": "Cancer", "Saturn": "Libra",
}
_OPPOSITE = {SIGNS[i]: SIGNS[(i + 6) % 12] for i in range(12)}

DIGNITY_SCORE = {"domicile": 5.0, "exaltation": 4.0, "peregrine": 0.0, "detriment": -4.0, "fall": -5.0}

#: How much each body weighs when counting elements and modalities: the
#: luminaries and Ascendant define a person; the outer planets are generational.
BALANCE_WEIGHT = {
    "Sun": 3.0, "Moon": 3.0, "Ascendant": 3.0,
    "Mercury": 2.0, "Venus": 2.0, "Mars": 2.0, "Midheaven": 2.0,
    "Jupiter": 1.0, "Saturn": 1.0,
    "Uranus": 0.5, "Neptune": 0.5, "Pluto": 0.5,
}

_HARD_ASPECTS = (eph.ASPECT_SQUARE, eph.ASPECT_OPPOSITION)


def _sign_of(longitude: float) -> str:
    return SIGNS[int(longitude % 360.0 // 30)]


def _dignity_of(planet: str, sign: str) -> str:
    """Essential dignity of ``planet`` in ``sign``."""
    if sign in _DOMICILE.get(planet, ()):
        return "domicile"
    if _EXALTATION.get(planet) == sign:
        return "exaltation"
    if any(_OPPOSITE[s] == sign for s in _DOMICILE.get(planet, ())):
        return "detriment"
    exalted = _EXALTATION.get(planet)
    if exalted and _OPPOSITE[exalted] == sign:
        return "fall"
    return "peregrine"


@dataclass(frozen=True, slots=True)
class Body:
    name: str
    longitude: float
    sign: str
    degree_in_sign: float
    house: int
    retrograde: bool
    dignity: str

    @property
    def dignity_score(self) -> float:
        return DIGNITY_SCORE[self.dignity]


@dataclass(frozen=True, slots=True)
class NatalAspect:
    a: str
    b: str
    aspect: str
    orb: float
    is_hard: bool


@dataclass(frozen=True)
class NatalChart:
    birth: eph.Birthdate
    house_system: str
    bodies: dict[str, Body]
    angles: dict[str, float]            # Ascendant, Midheaven, Descendant, Imum Coeli
    cusps: tuple[float, ...]            # 12 house cusps, house 1 first
    aspects: tuple[NatalAspect, ...]
    elements: dict[str, float] = field(default_factory=dict)
    modalities: dict[str, float] = field(default_factory=dict)
    chart_ruler: str = ""
    sect: str = ""                      # "day" or "night"

    def summary(self) -> dict:
        """A JSON-friendly digest (no raw secrets; it is only astronomy)."""
        return {
            "house_system": self.house_system,
            "ascendant": _fmt(self.angles["Ascendant"]),
            "midheaven": _fmt(self.angles["Midheaven"]),
            "sect": self.sect,
            "chart_ruler": self.chart_ruler,
            "elements": dict(self.elements),
            "modalities": dict(self.modalities),
            "bodies": {
                n: {"sign": b.sign, "degree": round(b.degree_in_sign, 2), "house": b.house,
                    "retrograde": b.retrograde, "dignity": b.dignity}
                for n, b in self.bodies.items()
            },
            "hard_aspects": [f"{a.a} {a.aspect} {a.b} ({a.orb:.1f})" for a in self.aspects if a.is_hard],
        }


def _fmt(longitude: float) -> str:
    return f"{longitude % 30:.2f} {_sign_of(longitude)}"


def _house_of(longitude: float, cusps: tuple[float, ...]) -> int:
    lon = longitude % 360.0
    for i in range(12):
        start, end = cusps[i] % 360.0, cusps[(i + 1) % 12] % 360.0
        if start <= end:
            if start <= lon < end:
                return i + 1
        elif lon >= start or lon < end:
            return i + 1
    return 12


def _aspect_between(lon_a: float, lon_b: float) -> tuple[str, float] | None:
    diff = abs(lon_a - lon_b) % 360.0
    if diff > 180.0:
        diff = 360.0 - diff
    best: tuple[str, float] | None = None
    for aspect, target in eph.ASPECT_DEGREES.items():
        orb = abs(diff - target)
        if orb <= eph.ASPECT_ORBS[aspect] and (best is None or orb < best[1]):
            best = (aspect, orb)
    return best


@functools.lru_cache(maxsize=8)
def _build_natal_chart(birth: eph.Birthdate) -> NatalChart:
    """The full chart for ``birth``. Needs the real Swiss Ephemeris and a place."""
    eph.require_precise_ephemeris("the natal chart (houses and angles)")
    swe = eph.swe
    jd = eph._get_julian_day(birth.year, birth.month, birth.day, birth.hour)

    if abs(birth.latitude) >= 66.0 or (birth.latitude == 0.0 and birth.longitude == 0.0):
        raise ValueError(
            "a natal chart needs a birth place (latitude/longitude); "
            f"got ({birth.latitude}, {birth.longitude})"
        )
    cusps_raw, ascmc = swe.houses_ex(jd, birth.latitude, birth.longitude, b"P")
    cusps = tuple(float(c) % 360.0 for c in cusps_raw[:12])
    asc, mc = float(ascmc[0]) % 360.0, float(ascmc[1]) % 360.0
    angles = {"Ascendant": asc, "Midheaven": mc,
              "Descendant": (asc + 180.0) % 360.0, "Imum Coeli": (mc + 180.0) % 360.0}

    bodies: dict[str, Body] = {}
    for p_id, name in eph.PLANET_NAMES.items():
        res, _flag = swe.calc_ut(jd, p_id, swe.FLG_SWIEPH | swe.FLG_SPEED)
        lon, speed = float(res[0]) % 360.0, float(res[3])
        sign = _sign_of(lon)
        bodies[name] = Body(
            name=name, longitude=lon, sign=sign, degree_in_sign=lon % 30.0,
            house=_house_of(lon, cusps), retrograde=speed < 0.0, dignity=_dignity_of(name, sign),
        )

    points = {n: b.longitude for n, b in bodies.items()}
    points.update({"Ascendant": asc, "Midheaven": mc})
    names = list(points)
    aspects: list[NatalAspect] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            found = _aspect_between(points[a], points[b])
            if found and not {a, b} == {"Ascendant", "Midheaven"}:
                aspects.append(NatalAspect(a, b, found[0], round(found[1], 3), found[0] in _HARD_ASPECTS))

    elements = {"fire": 0.0, "earth": 0.0, "air": 0.0, "water": 0.0}
    modalities = {"cardinal": 0.0, "fixed": 0.0, "mutable": 0.0}
    for name, lon in points.items():
        idx = int(lon // 30)
        weight = BALANCE_WEIGHT[name]
        elements[ELEMENT_OF_SIGN[idx]] += weight
        modalities[MODALITY_OF_SIGN[idx]] += weight

    return NatalChart(
        birth=birth, house_system="Placidus", bodies=bodies, angles=angles, cusps=cusps,
        aspects=tuple(aspects), elements=elements, modalities=modalities,
        chart_ruler=TRADITIONAL_RULER[_sign_of(asc)],
        sect="day" if bodies["Sun"].house >= 7 else "night",
    )


def chart_summary(birth: eph.Birthdate) -> dict | None:
    """The chart digest for ``birth``, or None when it has no birth place (no
    houses are possible) or the real ephemeris is unavailable. For status
    surfaces: it never raises."""
    if not (birth.latitude or birth.longitude):
        return None
    try:
        return _build_natal_chart(birth).summary()
    except Exception as exc:  # a status surface must not fail because a chart cannot be built
        logger.warning("natal chart unavailable: %s", exc)
        return None


def birth_for(date_text: str, hour: float | None = None) -> eph.Birthdate | None:
    """The chart for a configured ``YYYY-MM-DD`` (and optional UT hour).

    A persona that names Tala's birth date and no hour of its own gets her
    complete birth data (time and place) rather than a noon placeholder; every
    other date keeps the legacy noon-UT default, so other agents are unchanged.
    """
    parts = str(date_text or "").strip().split("-")
    if len(parts) != 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        datetime(y, m, d)
    except (TypeError, ValueError):
        return None
    given = _hour_or_none(hour)
    tala = eph.TALA_NATAL
    if given is None and (y, m, d) == (tala.year, tala.month, tala.day):
        return tala
    return eph.Birthdate(y, m, d, given if given is not None else 12.0)


def _hour_or_none(value: object) -> float | None:
    """A configured UT hour, or None when nothing usable was given.

    Only None/"" mean "unset": 0.0 is a real hour (midnight UT). The old
    ``hour or 12.0`` pattern silently turned a configured midnight into noon.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
