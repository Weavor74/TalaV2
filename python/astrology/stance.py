"""
python/astrology/stance.py -- how today's sky shapes her stance on a TOPIC.

``emotion.py`` turns the sky into a mood label. This turns it into a disposition
toward subjects: transiting Mars hard against her natal Sun, Moon or Venus makes
her averse to war and conflict; Mars in harmony with them opens her to it. It is
"a person is averse to conflict when the chart points that way" made computable.

For each topic in ``topics.json`` the score is the sum over contacts of

    polarity x orb tightness x aspect weight x receiver weight x dignity factor

where a contact is a *transiting significator of the topic* (Mars for war) landing
on a natal point, and

* **polarity** is -1 for a square or opposition and +1 for a trine or sextile; a
  conjunction takes the nature of the transiting planet (``topics.json``);
* **orb tightness** is the engine's own influence score (1 at exact, 0 at the edge
  of the orb); contacts under ``emotion.NOISE_FLOOR`` are dropped;
* **receiver weight** is how much the natal point matters (Sun and Moon most, the
  outer planets least), plus a bonus when that natal planet lives in one of the
  topic's houses and another when it is itself a significator of the topic;
* **dignity factor** is the receiver's natal dignity: a well-placed planet absorbs
  a hard aspect and makes more of a soft one, a badly placed one the reverse.

Her natal disposition adds a small baseline (a significator in its fall leans her
away from the topic) that is deliberately too small to speak on its own: only a
live transit makes her say so. The total is squashed through ``tanh`` into
[-1, +1], and the aspects that drove it come with it.

Stance is its own channel. It is not a style delta and is not under the 0.10
style cap that governs those (warmth, directness and the like); it never changes safety, tools or
authority, and the prompt line that carries it says so. Real Swiss Ephemeris only:
without it there is no stance rather than an approximate one.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from python.astrology import ephemeris as eph
from python.astrology import natal
from python.astrology.emotion import ASPECT_WEIGHTS, NOISE_FLOOR
from python.astrology.topics import Topic, TopicTable, load_table

logger = logging.getLogger(__name__)

#: A topic gets no line unless its score is beyond this; at or beyond STRONG it is "averse"/"eager".
#: Calibrated against a year of her own sky (2026-10-23 onward): with these values she has a stance on
#: war on roughly a third of days and a strong one on about one in ten. At 0.15 she had one on 78% of
#: days, which is not a stance, it is a constant.
STANCE_THRESHOLD = 0.30
STRONG_THRESHOLD = 0.55

#: Natal disposition per point of dignity score (-5 fall .. +5 domicile). Five points at
#: 0.03 is 0.15, half the speaking threshold, so natal disposition alone never speaks.
BASELINE_PER_DIGNITY_POINT = 0.03
#: How much a receiver's dignity changes what a contact does to her.
DIGNITY_RECEIVER_FACTOR = 0.06
#: Orb tightness is the engine's influence score raised to this power. Linear (1.0) let any contact
#: inside the 8-degree orb count, so some wide Mars contact existed on most days. Cubed, tight contacts
#: stay loud and wide ones fade to almost nothing.
ORB_FALLOFF_POWER = 3.0
HOUSE_BONUS = 0.4
SIGNIFICATOR_BONUS = 0.4
MAX_DRIVERS = 5

_POLARITY = {"square": -1.0, "opposition": -1.0, "trine": 1.0, "sextile": 1.0}


@dataclass(frozen=True, slots=True)
class Driver:
    """One transit-to-natal contact and how much it moved a topic (signed)."""

    transit_planet: str
    aspect: str
    natal_point: str
    orb: float
    contribution: float

    def describe(self) -> str:
        return f"transit {self.transit_planet} {self.aspect} natal {self.natal_point}, orb {self.orb:.1f}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "transit_planet": self.transit_planet, "aspect": self.aspect, "natal_point": self.natal_point,
            "orb": round(self.orb, 2), "contribution": round(self.contribution, 4), "text": self.describe(),
        }


@dataclass(frozen=True, slots=True)
class TopicStance:
    topic: str
    score: float
    label: str
    drivers: tuple[Driver, ...]
    baseline: float

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "label": self.label, "baseline": round(self.baseline, 4),
                "drivers": [d.as_dict() for d in self.drivers]}


def _label_for(score: float) -> str:
    if score <= -STRONG_THRESHOLD:
        return "averse"
    if score < -STANCE_THRESHOLD:
        return "wary"
    if score >= STRONG_THRESHOLD:
        return "eager"
    if score > STANCE_THRESHOLD:
        return "open"
    return "neutral"


def _polarity(transit: eph.Transit, table: TopicTable) -> float:
    if transit.aspect_type == eph.ASPECT_CONJUNCTION:
        return table.conjunction_nature.get(transit.planet, 0.0)
    return _POLARITY.get(transit.aspect_type, 0.0)


def _score_topic(topic: Topic, transits: Sequence[eph.Transit], bodies: Mapping[str, Any],
                 table: TopicTable) -> TopicStance:
    significators = dict(topic.significators)

    weight_total = sum(significators.values())
    baseline = BASELINE_PER_DIGNITY_POINT * sum(
        w * bodies[p].dignity_score for p, w in significators.items() if p in bodies
    ) / weight_total

    drivers: list[Driver] = []
    for t in transits:
        sig_weight = significators.get(t.planet)
        receiver = bodies.get(t.natal_planet)
        if sig_weight is None or receiver is None or t.influence_score < NOISE_FLOOR:
            continue
        polarity = _polarity(t, table)
        if polarity == 0.0:
            continue
        weight = table.receiver_weights[t.natal_planet]
        weight += HOUSE_BONUS if receiver.house in topic.houses else 0.0
        weight += SIGNIFICATOR_BONUS if t.natal_planet in significators else 0.0
        dignity = max(0.4, 1.0 + DIGNITY_RECEIVER_FACTOR * receiver.dignity_score * (1.0 if polarity > 0 else -1.0))
        contribution = (polarity * t.influence_score ** ORB_FALLOFF_POWER * ASPECT_WEIGHTS.get(t.aspect_type, 0.5)
                        * weight * sig_weight * dignity)
        drivers.append(Driver(t.planet, t.aspect_type, t.natal_planet, t.orb, contribution))

    drivers.sort(key=lambda d: abs(d.contribution), reverse=True)
    score = round(math.tanh(baseline + sum(d.contribution for d in drivers)), 4)
    return TopicStance(topic.name, score, _label_for(score), tuple(drivers[:MAX_DRIVERS]), baseline)


def _score_topics(transits: Sequence[eph.Transit], bodies: Mapping[str, Any],
                 table: TopicTable | None = None) -> dict[str, TopicStance]:
    """The stance on every topic, from transit contacts and the natal bodies they land on.

    Pure: ``bodies`` is any mapping of planet name to something with
    ``dignity_score`` and ``house`` (``natal.Body`` in production).
    """
    table = table or load_table()
    return {name: _score_topic(topic, transits, bodies, table) for name, topic in table.topics.items()}


def compute_stances(birth: eph.Birthdate, when: datetime) -> dict[str, TopicStance] | None:
    """Today's stance on every topic for the chart ``birth``, or None when it cannot be computed.

    None means no precise ephemeris or no complete chart (a birth place is
    needed for houses); it is never an approximation.
    """
    if not eph.SWISSEPH_AVAILABLE:
        return None
    try:
        chart = natal.natal_chart(birth)
    except Exception as exc:
        logger.info("topic stance unavailable: no complete natal chart (%s)", exc)
        return None
    transits = eph.EphemerisEngine().compute_transits(birth, when)
    return _score_topics(transits, chart.bodies)


def speaking(stances: Mapping[str, TopicStance]) -> dict[str, TopicStance]:
    """Only the topics she would actually voice a stance on, strongest first."""
    loud = [s for s in stances.values() if abs(s.score) > STANCE_THRESHOLD]
    return {s.topic: s for s in sorted(loud, key=lambda s: abs(s.score), reverse=True)}


def payload_for(birth: eph.Birthdate, when: datetime) -> dict[str, Any] | None:
    """The slice of stance that rides in the emotion payload: only what she would voice."""
    stances = compute_stances(birth, when)
    if stances is None:
        return None
    return {
        "computed_for": when.isoformat(),
        "ephemeris_source": eph.ephemeris_source(),
        "threshold": STANCE_THRESHOLD,
        "stances": {
            name: {"score": s.score, "label": s.label, "drivers": [d.describe() for d in s.drivers]}
            for name, s in speaking(stances).items()
        },
    }
