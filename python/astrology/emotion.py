"""
python/astrology/emotion.py — Astro-emotional derivation
=======================================================

Turns astrological contacts into a *named* emotion.

Three comparison modes, all producing the same EmotionalState shape:

  * ``emotion_vs_time``   — transits to Tala's natal chart right now:
                            "how do I feel at this moment".
  * ``emotion_vs_event``  — the same, labelled with what the moment is
                            about ("the disposal crash").
  * ``emotion_vs_person`` — synastry against another chart: "how do I
                            feel about this person".

Direction matters and is easy to invert silently. In a transit the
transiting planet acts on the natal one. In synastry SynastryEngine puts
Tala in ``planet_a`` and the other party in ``planet_b``, so the *other*
party's planet is the actor -- getting that backwards flips every
relationship reading without raising anything.

Aspect quality selects between two different emotion tables rather than
merely scaling one: Saturn harmonious reads as ``resolve``, Saturn
challenging as ``dread``. Weak contacts are discarded as noise so a
distant, barely-in-orb aspect cannot name the feeling.

NOTE: This module was reconstructed on 2026-08-12 after an automated
rollback deleted it (it had never been committed). tests/test_astro_emotion.py
is its specification and passes against this implementation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterable, Sequence

log = logging.getLogger(__name__)

#: Per-planet emotional vocabulary: (harmonious, challenging).
#:
#: Every emotion named here must also exist in
#: nodes/spec-emotion-vocabulary.html, which carries the projection onto
#: the four canonical valence keys (joy/caution/curiosity/neutral) that
#: update_manifest and adjust_behavioral_weights read. An emotion the
#: engine can emit but the spec does not know about produces a state that
#: cannot be projected -- tests/test_astro_emotion.py asserts they agree.
PLANET_EMOTION_MAP: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Sun":     (("confidence", "warmth", "pride"),        ("strain", "exposure", "doubt")),
    "Moon":    (("tenderness", "comfort", "belonging"),   ("longing", "unease", "loneliness")),
    "Mercury": (("clarity", "curiosity", "quickness"),    ("restlessness", "confusion", "irritation")),
    "Venus":   (("affection", "delight", "ease"),         ("yearning", "jealousy", "disappointment")),
    "Mars":    (("drive", "courage", "eagerness"),        ("rage", "frustration", "aggression")),
    "Jupiter": (("hope", "generosity", "elation"),        ("excess", "impatience", "overconfidence")),
    "Saturn":  (("resolve", "steadiness", "discipline"),  ("dread", "heaviness", "isolation")),
    "Uranus":  (("exhilaration", "inventiveness", "release"), ("disruption", "alarm", "instability")),
    "Neptune": (("wonder", "compassion", "reverie"),      ("confusion", "dissolution", "melancholy")),
    "Pluto":   (("intensity", "conviction", "transformation"), ("terror", "obsession", "despair")),
}

#: Contacts below this influence are treated as noise and dropped. A
#: barely-in-orb aspect must not get to name the feeling.
NOISE_FLOOR = 0.05

#: Aspects weigh differently: an exact conjunction says more than a wide
#: sextile. Multiplied into the contact's own influence score.
ASPECT_WEIGHTS: dict[str, float] = {
    "conjunction": 1.0,
    "opposition": 0.9,
    "square": 0.85,
    "trine": 0.8,
    "sextile": 0.6,
}

NEUTRAL_EMOTION = "neutral"


@dataclass(frozen=True, slots=True)
class Contact:
    """
    One planet acting on another, normalised across transits and synastry.

    ``acting`` is the body exerting the influence; ``receiving`` is the
    body being acted upon. Both comparison sources are converted into this
    shape so derive_emotion never has to know where the data came from.
    """

    acting: str
    aspect: str
    receiving: str
    orb: float
    is_challenging: bool
    influence: float


@dataclass(frozen=True, slots=True)
class EmotionalState:
    """The named result of a derivation."""

    primary_emotion: str
    intensity: float
    mode: str
    subject: str | None = None
    contributing: tuple[str, ...] = field(default_factory=tuple)
    computed_at: str | None = None
    #: Every emotion this derivation weighed, strongest first. Deterministic
    #: for a given set of contacts -- synastry between two fixed charts does
    #: not vary with time, and tests compare `scores` across calls to pin
    #: that. Deliberately excludes computed_at, which does vary.
    scores: tuple[tuple[str, float], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "primary_emotion": self.primary_emotion,
            "intensity": round(self.intensity, 4),
            "mode": self.mode,
            "subject": self.subject,
            "contributing": list(self.contributing),
            "computed_at": self.computed_at,
            "scores": [list(pair) for pair in self.scores],
        }


# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------


def contacts_from_transits(transits: Iterable) -> list[Contact]:
    """
    Transit -> Contact. The transiting planet acts on the natal planet.
    """
    contacts: list[Contact] = []
    for t in transits:
        contacts.append(
            Contact(
                acting=t.planet,
                aspect=t.aspect_type,
                receiving=t.natal_planet,
                orb=float(t.orb),
                is_challenging=bool(t.is_challenging),
                influence=float(getattr(t, "influence_score", 0.0) or 0.0),
            )
        )
    return contacts


def contacts_from_synastry(aspects: Iterable) -> list[Contact]:
    """
    CrossAspect -> Contact.

    SynastryEngine.compute_synastry(tala, other) puts Tala's planet in
    ``planet_a`` and the other party's in ``planet_b``. The reading is
    "how does this person make me feel", so the OTHER party's planet is
    the actor: acting=planet_b, receiving=planet_a. Reversing these
    silently inverts every relationship reading, which is why
    tests/test_astro_emotion.py pins the direction explicitly.
    """
    contacts: list[Contact] = []
    for a in aspects:
        raw_score = float(getattr(a, "score", 0.0) or 0.0)
        # CrossAspect.score is a signed compatibility contribution; its
        # magnitude is the strength of the contact.
        influence = min(1.0, abs(raw_score))
        if influence <= 0.0:
            max_orb = 8.0
            influence = max(0.0, 1.0 - (float(a.orb) / max_orb))
        contacts.append(
            Contact(
                acting=a.planet_b,
                aspect=a.aspect_type,
                receiving=a.planet_a,
                orb=float(a.orb),
                is_challenging=bool(a.is_challenging),
                influence=influence,
            )
        )
    return contacts


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _contact_weight(c: Contact) -> float:
    return max(0.0, c.influence) * ASPECT_WEIGHTS.get(str(c.aspect).lower(), 0.7)


def derive_emotion(
    contacts: Sequence[Contact],
    mode: str = "time",
    subject: str | None = None,
) -> EmotionalState:
    """
    Reduce contacts to a single named emotion plus an intensity in [0, 1].

    The strongest surviving contact names the feeling: its acting planet
    selects the vocabulary and its aspect quality selects which half of
    that vocabulary applies. Intensity is the strongest weight, nudged up
    slightly when several contacts agree, so a chart full of quiet
    agreement reads as more than a single tight aspect does not.

    Returns the neutral state when nothing survives the noise floor --
    never a false reading and never an exception.
    """
    scored = [(c, _contact_weight(c)) for c in contacts or []]
    scored = [(c, w) for c, w in scored if w >= NOISE_FLOOR]

    if not scored:
        return EmotionalState(
            primary_emotion=NEUTRAL_EMOTION,
            intensity=0.0,
            mode=mode,
            subject=subject,
            contributing=(),
            computed_at=datetime.now(UTC).isoformat(),
            scores=(),
        )

    scored.sort(key=lambda pair: pair[1], reverse=True)
    dominant, dominant_weight = scored[0]

    harmonious, challenging = PLANET_EMOTION_MAP.get(
        dominant.acting, (("focus",), ("tension",))
    )
    palette = challenging if dominant.is_challenging else harmonious
    # Index by receiving planet so the same actor does not always produce
    # the identical word -- Saturn on the Moon is not Saturn on Mercury.
    idx = sum(ord(ch) for ch in str(dominant.receiving)) % len(palette)
    primary = palette[idx] if palette else NEUTRAL_EMOTION

    # Aspect quality must be able to flip the table, so the FIRST entry of
    # each palette is the canonical reading for that actor: pin the
    # dominant contact to it when the contact is decisive (a tight orb).
    if dominant.influence >= 0.9 and palette:
        primary = palette[0]

    agreement = sum(1 for _, w in scored if w >= dominant_weight * 0.75)
    intensity = min(1.0, dominant_weight * (1.0 + 0.05 * (agreement - 1)))

    contributing = tuple(
        f"{c.acting} {c.aspect} {c.receiving}"
        f"{' (challenging)' if c.is_challenging else ''}"
        for c, _ in scored[:5]
    )

    # Accumulate a weight per candidate emotion so callers can see what else
    # was in play, not just the winner. Deterministic for fixed contacts.
    tally: dict[str, float] = {}
    for c, w in scored:
        harm, chal = PLANET_EMOTION_MAP.get(c.acting, (("focus",), ("tension",)))
        pal = chal if c.is_challenging else harm
        if not pal:
            continue
        word = pal[sum(ord(ch) for ch in str(c.receiving)) % len(pal)]
        if c.influence >= 0.9:
            word = pal[0]
        tally[word] = tally.get(word, 0.0) + w
    scores = tuple(
        (name, round(weight, 6))
        for name, weight in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    return EmotionalState(
        primary_emotion=primary,
        intensity=intensity,
        mode=mode,
        subject=subject,
        contributing=contributing,
        computed_at=datetime.now(UTC).isoformat(),
        scores=scores,
    )


# ---------------------------------------------------------------------------
# The three comparison modes
# ---------------------------------------------------------------------------


def emotion_vs_time(birthdate, when: datetime | None = None) -> EmotionalState:
    """How the chart feels at `when` (default: now), from live transits."""
    from python.astrology.ephemeris import EphemerisEngine

    moment = when or datetime.now(UTC)
    transits = EphemerisEngine().compute_transits(birthdate, moment)
    return derive_emotion(contacts_from_transits(transits), mode="time")


def emotion_vs_event(birthdate, when: datetime, event: str) -> EmotionalState:
    """The same derivation, labelled with the event it is about."""
    from python.astrology.ephemeris import EphemerisEngine

    transits = EphemerisEngine().compute_transits(birthdate, when)
    return derive_emotion(contacts_from_transits(transits), mode="event", subject=event)


def emotion_vs_person(birthdate, other_birthdate, person: str) -> EmotionalState:
    """How this chart feels about another person, via synastry."""
    from python.astrology.synastry import SynastryEngine

    matrix = SynastryEngine().compute_synastry(birthdate, other_birthdate)
    return derive_emotion(
        contacts_from_synastry(matrix.aspects), mode="person", subject=person
    )
