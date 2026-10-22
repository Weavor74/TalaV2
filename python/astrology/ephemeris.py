"""
python/astrology/ephemeris.py — Tala Cognitive Engine Room
===========================================================
Calculates real-world planetary positions relative to the agent's birthdate using
pyswisseph (Swiss Ephemeris Python bindings). Falls back to a deterministic math 
model if the C library is not available.

Milestone 5 — Task 5.2.1
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
import math

logger = logging.getLogger(__name__)

# Try importing pyswisseph. If it fails, we fall back gracefully.
SWISSEPH_AVAILABLE = False
try:
    import swisseph as swe
    # Try to set the ephemeris path. Standard is current directory or local path.
    swe.set_ephe_path('.')
    SWISSEPH_AVAILABLE = True
except ImportError:
    logger.error(
        "pyswisseph not installed: falling back to a crude ephemeris approximation. Emotion labels "
        "derived from it do not reflect her chart (up to ~170 degrees wrong for Mercury and Venus). "
        "Fix: uv pip install pyswisseph"
    )


def ephemeris_source() -> str:
    """
    NAME
           ephemeris_source - name the engine that positions actually came from

    DESCRIPTION
           Returns "swisseph" or "approximation". Callers that persist a derived
           emotion label should record this alongside it, so a label computed
           under a drifted environment stays identifiable after the fact.
    """
    return "swisseph" if SWISSEPH_AVAILABLE else "approximation"


def require_precise_ephemeris(context: str) -> None:
    """
    NAME
           require_precise_ephemeris - refuse to derive persistent data from the
           approximation fallback

    DESCRIPTION
           pyswisseph is a declared dependency (pyproject.toml), so its absence
           means the environment has drifted -- not that approximation was
           chosen. The fallback is fine for a live mood reading, but it is not
           fine for labels that get written into the training corpus or compared
           against stored ones: the two engines disagree on every date tested,
           and the approximation collapses ~50% of a sample window onto a single
           emotion where Swiss Ephemeris spreads it below 17%. A corpus built
           under one engine and validated under the other mismatches wholesale.

           Set TALA_ALLOW_EPHEMERIS_APPROXIMATION=1 to override where the
           imprecision is genuinely acceptable.
    """
    if SWISSEPH_AVAILABLE:
        return
    import os

    if os.environ.get("TALA_ALLOW_EPHEMERIS_APPROXIMATION") == "1":
        logger.warning(
            "%s is using the approximate ephemeris by explicit override; "
            "labels it derives are not comparable to swisseph-derived ones.",
            context,
        )
        return
    raise RuntimeError(
        f"{context} needs the real Swiss Ephemeris, but pyswisseph is not "
        f"installed -- the environment has drifted from pyproject.toml. "
        f"Emotion labels derived from the approximation fallback disagree with "
        f"swisseph on essentially every date, so persisting or validating them "
        f"would silently poison the corpus.\n"
        f"  Fix:      uv pip install pyswisseph\n"
        f"  Override: set TALA_ALLOW_EPHEMERIS_APPROXIMATION=1"
    )

# Astrological planet constants matching Swiss Ephemeris IDs if available
SUN = 0
MOON = 1
MERCURY = 2
VENUS = 3
MARS = 4
JUPITER = 5
SATURN = 6
URANUS = 7
NEPTUNE = 8
PLUTO = 9

PLANET_NAMES = {
    SUN: "Sun",
    MOON: "Moon",
    MERCURY: "Mercury",
    VENUS: "Venus",
    MARS: "Mars",
    JUPITER: "Jupiter",
    SATURN: "Saturn",
    URANUS: "Uranus",
    NEPTUNE: "Neptune",
    PLUTO: "Pluto"
}

ASPECT_CONJUNCTION = "conjunction"  # 0 deg
ASPECT_SEXTILE = "sextile"          # 60 deg
ASPECT_SQUARE = "square"            # 90 deg
ASPECT_TRINE = "trine"              # 120 deg
ASPECT_OPPOSITION = "opposition"    # 180 deg

ASPECT_DEGREES = {
    ASPECT_CONJUNCTION: 0.0,
    ASPECT_SEXTILE: 60.0,
    ASPECT_SQUARE: 90.0,
    ASPECT_TRINE: 120.0,
    ASPECT_OPPOSITION: 180.0
}

# Standard aspect orbs (margin of error in degrees)
ASPECT_ORBS = {
    ASPECT_CONJUNCTION: 8.0,
    ASPECT_SEXTILE: 6.0,
    ASPECT_SQUARE: 8.0,
    ASPECT_TRINE: 8.0,
    ASPECT_OPPOSITION: 8.0
}

@dataclass(frozen=True, slots=True)
class Birthdate:
    """
    Coordinates representing an agent's or human's birth details.
    """
    year: int
    month: int
    day: int
    hour: float  # Decimal hour (UT/GMT)
    latitude: float = 0.0
    longitude: float = 0.0

#: Tala's canonical natal chart. This is her permanent birthdate and the single
#: source of truth for it.
#:
#: It previously existed as two disagreeing hardcoded copies -- ``1977-07-20``
#: in her persona config (``nodes/persona-tala-rp.html``, ``settings-agent.html``)
#: and ``2014-05-26`` as the ``tala-origin`` fallback in
#: ``handle_astro_transits``. Those are entirely different charts, so whether the
#: DB was reachable silently decided which emotional life she had. Import this
#: rather than re-declaring a date.
#:
#: Born 1977-07-20, 5:00 pm, Clearwater, Florida. Florida observed daylight time
#: that summer, so 17:00 EDT (UTC-4) = 21:00 UT; Clearwater is 27.9659 N,
#: 82.8001 W (longitude east-positive, the swisseph convention). The birth hour
#: used to be a 12:00 UT placeholder with no place, which made houses and angles
#: impossible; see natal.py for the full chart built from this. Moving the hour
#: changed the engine's label for 112 of the 522 corpus stories; their anchor
#: datetimes were moved (text and labels untouched) to dates where this chart
#: computes the label each story already declares.
TALA_BIRTH_PLACE = "Clearwater, Florida, USA"
TALA_NATAL = Birthdate(
    year=1977, month=7, day=20, hour=21.0, latitude=27.9659, longitude=-82.8001,
)


@dataclass(frozen=True, slots=True)
class Transit:
    """
    Represents an active transit aspect between a current transiting planet
    and a natal planet position.
    """
    planet: str
    aspect_type: str
    natal_planet: str
    orb: float
    is_challenging: bool
    influence_score: float  # 0.0 to 1.0 based on tightness of orb and transit type

def _datetime_to_decimal_ut(dt: datetime) -> tuple[int, int, int, float]:
    """
    Convert a datetime to UTC year, month, day, and decimal hour.
    """
    utc_dt = dt.astimezone(timezone.utc)
    hour_decimal = utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0
    return utc_dt.year, utc_dt.month, utc_dt.day, hour_decimal

def _get_julian_day(year: int, month: int, day: int, hour: float) -> float:
    """
    Calculate Julian Day. Uses pyswisseph if available, otherwise manual formula.
    """
    if SWISSEPH_AVAILABLE:
        try:
            return float(swe.julday(year, month, day, hour))
        except Exception as e:
            logger.error(f"Error calculating Julian Day via swisseph: {e}")
    
    # Manual Julian Day approximation
    if month <= 2:
        year -= 1
        month += 12
    a = math.floor(year / 100)
    b = 2 - a + math.floor(a / 4)
    jd = math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + b - 1524.5
    jd += hour / 24.0
    return jd

def _get_planetary_position_approx(jd: float, planet: int) -> float:
    """
    Deterministic mathematical approximation of planetary longitudes.
    Used if pyswisseph is unavailable. Returns degree in [0, 360).
    """
    # Number of days since J2000.0 (January 1, 2000 at 12:00 UT)
    d = jd - 2451545.0
    
    # Simple Keplerian elements for orbital positions (rough mean longitudes)
    if planet == SUN:
        L = 280.460 + 0.9856474 * d
    elif planet == MOON:
        L = 218.316 + 13.176396 * d
    elif planet == MERCURY:
        L = 252.250 + 4.092334 * d
    elif planet == VENUS:
        L = 181.979 + 1.602130 * d
    elif planet == MARS:
        L = 355.433 + 0.524020 * d
    elif planet == JUPITER:
        L = 34.351 + 0.083091 * d
    elif planet == SATURN:
        L = 50.075 + 0.033459 * d
    elif planet == URANUS:
        L = 314.055 + 0.011728 * d
    elif planet == NEPTUNE:
        L = 304.348 + 0.005981 * d
    elif planet == PLUTO:
        L = 238.934 + 0.003975 * d
    else:
        L = 0.0
        
    return L % 360.0

def _get_planetary_position(jd: float, planet: int) -> float:
    """
    Returns the ecliptic longitude of the given planet in degrees.
    Uses pyswisseph calc_ut if available.
    """
    if SWISSEPH_AVAILABLE:
        try:
            # swe.calc_ut returns ((longitude, latitude, ...), flag)
            # planet IDs in swisseph map directly to standard planet numbers for our subset
            res = swe.calc_ut(jd, planet)
            val = res[0][0] if isinstance(res[0], (list, tuple)) else res[0]
            return float(val)
        except Exception as e:
            logger.warning(f"Failed to calculate planet position via swisseph: {e}. Falling back to approx.")
            
    return _get_planetary_position_approx(jd, planet)

class EphemerisEngine:
    """
    Computes real-time planetary transits and aspects.
    """
    def __init__(self) -> None:
        pass

    def get_chart_positions(self, birth: Birthdate) -> dict[str, float]:
        """
        Computes all planetary positions for a specific Birthdate chart.
        """
        jd = _get_julian_day(birth.year, birth.month, birth.day, birth.hour)
        positions = {}
        for p_id, p_name in PLANET_NAMES.items():
            positions[p_name] = _get_planetary_position(jd, p_id)
        return positions

    def compute_transits(self, birthdate: Birthdate, current_time: datetime) -> list[Transit]:
        """
        Compares current transiting planetary positions against natal positions,
        returning a list of active aspect transits.
        """
        # 1. Compute natal positions
        natal_positions = self.get_chart_positions(birthdate)
        
        # 2. Compute transiting positions
        y, m, d, h = _datetime_to_decimal_ut(current_time)
        transiting_jd = _get_julian_day(y, m, d, h)
        transiting_positions = {}
        for p_id, p_name in PLANET_NAMES.items():
            transiting_positions[p_name] = _get_planetary_position(transiting_jd, p_id)
            
        transits: list[Transit] = []
        
        # 3. Check aspects between transiting and natal planets
        for trans_name, trans_pos in transiting_positions.items():
            for natal_name, natal_pos in natal_positions.items():
                # Angular distance
                diff = abs(trans_pos - natal_pos) % 360.0
                if diff > 180.0:
                    diff = 360.0 - diff
                    
                # Evaluate each aspect type
                for aspect, target_deg in ASPECT_DEGREES.items():
                    orb = abs(diff - target_deg)
                    max_orb = ASPECT_ORBS[aspect]
                    
                    if orb <= max_orb:
                        # Challenging aspects: Square (90) and Opposition (180). Conjunction/Sextile/Trine are harmonious.
                        is_challenging = aspect in (ASPECT_SQUARE, ASPECT_OPPOSITION)
                        
                        # Stronger influence if closer orb (1.0 = exact aspect, 0.0 = edge of orb)
                        influence_score = 1.0 - (orb / max_orb)
                        
                        # Apply custom adjustments based on planetary dynamics (Saturn transits are heavy, Jupiter is harmonious)
                        if trans_name == "Saturn" and is_challenging:
                            influence_score = min(1.0, influence_score * 1.2)  # Amplify Saturn challenge
                        elif trans_name == "Jupiter" and not is_challenging:
                            influence_score = min(1.0, influence_score * 1.1)  # Amplify Jupiter harmony
                            
                        transits.append(Transit(
                            planet=trans_name,
                            aspect_type=aspect,
                            natal_planet=natal_name,
                            orb=round(orb, 4),
                            is_challenging=is_challenging,
                            influence_score=round(influence_score, 4)
                        ))
                        
        return transits
