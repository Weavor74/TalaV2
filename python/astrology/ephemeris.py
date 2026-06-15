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
from typing import Any

logger = logging.getLogger(__name__)

# Try importing pyswisseph. If it fails, we fall back gracefully.
SWISSEPH_AVAILABLE = False
try:
    import swisseph as swe
    # Try to set the ephemeris path. Standard is current directory or local path.
    swe.set_ephe_path('.')
    SWISSEPH_AVAILABLE = True
except ImportError:
    logger.warning("pyswisseph not installed. Falling back to deterministic ephemeris approximation.")

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

def datetime_to_decimal_ut(dt: datetime) -> tuple[int, int, int, float]:
    """
    Convert a datetime to UTC year, month, day, and decimal hour.
    """
    utc_dt = dt.astimezone(timezone.utc)
    hour_decimal = utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0
    return utc_dt.year, utc_dt.month, utc_dt.day, hour_decimal

def get_julian_day(year: int, month: int, day: int, hour: float) -> float:
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

def get_planetary_position_approx(jd: float, planet: int) -> float:
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

def get_planetary_position(jd: float, planet: int) -> float:
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
            
    return get_planetary_position_approx(jd, planet)

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
        jd = get_julian_day(birth.year, birth.month, birth.day, birth.hour)
        positions = {}
        for p_id, p_name in PLANET_NAMES.items():
            positions[p_name] = get_planetary_position(jd, p_id)
        return positions

    def compute_transits(self, birthdate: Birthdate, current_time: datetime) -> list[Transit]:
        """
        Compares current transiting planetary positions against natal positions,
        returning a list of active aspect transits.
        """
        # 1. Compute natal positions
        natal_positions = self.get_chart_positions(birthdate)
        
        # 2. Compute transiting positions
        y, m, d, h = datetime_to_decimal_ut(current_time)
        transiting_jd = get_julian_day(y, m, d, h)
        transiting_positions = {}
        for p_id, p_name in PLANET_NAMES.items():
            transiting_positions[p_name] = get_planetary_position(transiting_jd, p_id)
            
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
