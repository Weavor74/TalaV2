"""
python/astrology/synastry.py — Tala Cognitive Engine Room
===========================================================
Computes planetary synastry aspects and compatibility scores between two birth charts.
Supports Human-to-Agent and Agent-to-Agent relationships.

Milestone 5 — Task 5.2.2
"""

from __future__ import annotations

from dataclasses import dataclass

from .ephemeris import (
    ASPECT_DEGREES,
    ASPECT_ORBS,
    ASPECT_SQUARE,
    ASPECT_OPPOSITION,
    Birthdate,
    EphemerisEngine,
)

@dataclass(frozen=True, slots=True)
class CrossAspect:
    """
    Represents an aspect between a planet in Chart A and a planet in Chart B.
    """
    planet_a: str
    aspect_type: str
    planet_b: str
    orb: float
    is_challenging: bool
    score: float  # Compatibility weight contribution

@dataclass(frozen=True, slots=True)
class SynastryMatrix:
    """
    Result of a synastry comparison between two charts.
    """
    overall_compatibility: float  # Value in [0.0, 1.0]
    aspects: list[CrossAspect]
    dominant_theme: str

class SynastryEngine:
    """
    Computes relational compatibility matrices using geometric chart layouts.
    """
    def __init__(self) -> None:
        self._ephemeris = EphemerisEngine()

    def compute_synastry(self, chart_a: Birthdate, chart_b: Birthdate) -> SynastryMatrix:
        """
        Compare Chart A and Chart B, computing all cross-aspects and overall score.
        """
        pos_a = self._ephemeris.get_chart_positions(chart_a)
        pos_b = self._ephemeris.get_chart_positions(chart_b)
        
        cross_aspects: list[CrossAspect] = []
        harmonious_points = 0.0
        challenging_points = 0.0
        total_aspects_count = 0
        
        for name_a, deg_a in pos_a.items():
            for name_b, deg_b in pos_b.items():
                diff = abs(deg_a - deg_b) % 360.0
                if diff > 180.0:
                    diff = 360.0 - diff
                    
                for aspect, target_deg in ASPECT_DEGREES.items():
                    orb = abs(diff - target_deg)
                    max_orb = ASPECT_ORBS[aspect]
                    
                    if orb <= max_orb:
                        is_challenging = aspect in (ASPECT_SQUARE, ASPECT_OPPOSITION)
                        closeness = 1.0 - (orb / max_orb)
                        
                        # Custom scoring weights for synastry
                        base_weight = 1.0
                        if aspect == "trine" or aspect == "sextile":
                            base_weight = 1.2
                        elif aspect == "conjunction":
                            base_weight = 1.5
                        elif is_challenging:
                            base_weight = 0.8
                            
                        # Planetary combinations influence
                        combination_modifier = 1.0
                        if {name_a, name_b} == {"Venus", "Mars"}:
                            combination_modifier = 1.5  # High attraction
                        elif {name_a, name_b} == {"Sun", "Moon"}:
                            combination_modifier = 1.8  # Deep soul alignment
                        elif {name_a, name_b} == {"Saturn", "Sun"}:
                            combination_modifier = 0.6  # Heavy karmic lesson / friction
                            
                        aspect_score = closeness * base_weight * combination_modifier
                        
                        if is_challenging:
                            challenging_points += aspect_score
                        else:
                            harmonious_points += aspect_score
                            
                        total_aspects_count += 1
                        
                        cross_aspects.append(CrossAspect(
                            planet_a=name_a,
                            aspect_type=aspect,
                            planet_b=name_b,
                            orb=round(orb, 4),
                            is_challenging=is_challenging,
                            score=round(aspect_score, 4)
                        ))
                        
        # Calculate overall compatibility index clamped to [0.0, 1.0]
        # Base compatibility is 0.5 (neutral)
        base_compatibility = 0.5
        if total_aspects_count > 0:
            net_points = harmonious_points - challenging_points
            compatibility_delta = net_points / (total_aspects_count * 1.5)
            overall_compatibility = max(0.0, min(1.0, base_compatibility + compatibility_delta))
        else:
            overall_compatibility = base_compatibility
            
        # Determine dominant theme
        if overall_compatibility >= 0.7:
            dominant_theme = "Deep Resonance and Harmony"
        elif overall_compatibility <= 0.35:
            dominant_theme = "Significant Friction and Structural Growth"
        elif challenging_points > harmonious_points:
            dominant_theme = "Dynamic Tension and Catalyst for Action"
        else:
            dominant_theme = "Cooperative Alignment and Stable Dialogue"
            
        return SynastryMatrix(
            overall_compatibility=round(overall_compatibility, 4),
            aspects=cross_aspects,
            dominant_theme=dominant_theme
        )
