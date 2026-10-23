"""
python/astrology/topics.py -- the planet-to-topic table behind topic stance.

The table is data (``topics.json`` beside this file), not code, so how the sky
maps onto subjects can be tuned without touching the scorer:

* ``significators`` -- the transiting planets that move a topic. Mars moves war,
  conflict, competition and sport; Venus moves love, art, money and beauty; and
  so on. A topic can have several (technology is Mercury and Uranus), each with
  its own weight.
* ``houses`` -- natal houses that make a natal planet matter more for the
  topic. A natal planet living in the 8th house is more affected by a transit
  that bears on war than one in the 3rd. No topic uses house 1.
* ``keywords`` -- matched as whole words, so "war" never fires inside "warm",
  "reward" or "software". A trailing s/es/ed/ing is allowed.
* ``description`` -- what the optional embedding layer compares a message with.
* ``receiver_weights`` -- how much a natal point matters as the thing a transit
  lands on: the personal points (Sun, Moon, Venus) most, the outer planets least.
* ``conjunction_nature`` -- a conjunction is neither hard nor soft; it takes the
  nature of the transiting planet (Venus and Jupiter warm it, Mars and Saturn
  chill it).

A malformed table raises ``ValueError`` at load time: a stance layer running on
a half-read table would be wrong in a way that looks like a mood.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from python.astrology import ephemeris as eph

DEFAULT_PATH = Path(__file__).with_name("topics.json")

_PLANETS = frozenset(eph.PLANET_NAMES.values())
_SUFFIX = r"(?:s|es|ed|ing)?"


@dataclass(frozen=True)
class Topic:
    name: str
    significators: tuple[tuple[str, float], ...]
    houses: tuple[int, ...]
    phrase: str
    keywords: tuple[str, ...]
    description: str
    pattern: re.Pattern = field(repr=False, compare=False, default=None)  # type: ignore[assignment]

    @property
    def significator_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.significators)


@dataclass(frozen=True)
class TopicTable:
    topics: dict[str, Topic]
    receiver_weights: dict[str, float]
    conjunction_nature: dict[str, float]


def _keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern:
    alternatives = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatives}){_SUFFIX}\b", re.IGNORECASE)


def _parse_significators(name: str, raw: object) -> tuple[tuple[str, float], ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"topic {name!r}: significators must be a non-empty list")
    parsed = []
    for item in raw:
        planet, weight = (item, 1.0) if isinstance(item, str) else (item[0], item[1]) if (
            isinstance(item, list) and len(item) == 2) else (None, None)
        if planet not in _PLANETS:
            raise ValueError(f"topic {name!r}: {item!r} is not a planet")
        if not isinstance(weight, (int, float)) or not 0.0 < float(weight) <= 2.0:
            raise ValueError(f"topic {name!r}: bad weight for {planet}")
        parsed.append((planet, float(weight)))
    return tuple(parsed)


def _parse_topic(name: str, raw: object) -> Topic:
    if not isinstance(raw, dict):
        raise ValueError(f"topic {name!r} must be an object")
    significators = _parse_significators(name, raw.get("significators"))
    houses = tuple(raw.get("houses") or ())
    if not all(isinstance(h, int) and 1 <= h <= 12 for h in houses):
        raise ValueError(f"topic {name!r}: houses must be integers 1-12")
    keywords = tuple(str(k).strip() for k in (raw.get("keywords") or ()) if str(k).strip())
    phrase, description = str(raw.get("phrase") or "").strip(), str(raw.get("description") or "").strip()
    if not (keywords and phrase and description):
        raise ValueError(f"topic {name!r} needs keywords, a phrase and a description")
    return Topic(name, significators, houses, phrase, keywords, description, _keyword_pattern(keywords))


def _number_map(raw: object, label: str, low: float, high: float) -> dict[str, float]:
    if not isinstance(raw, dict) or set(raw) != _PLANETS:
        raise ValueError(f"{label} must give a number for each of the ten planets")
    out = {}
    for planet, value in raw.items():
        if not isinstance(value, (int, float)) or not low <= float(value) <= high:
            raise ValueError(f"{label}[{planet}] must be a number in [{low}, {high}]")
        out[planet] = float(value)
    return out


def load_table(path: Path | str | None = None) -> TopicTable:
    """Read and validate the table. Cached for the default file."""
    if path is None:
        return _default_table()
    return _read(Path(path))


@functools.lru_cache(maxsize=1)
def _default_table() -> TopicTable:
    return _read(DEFAULT_PATH)


def _read(path: Path) -> TopicTable:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_topics = data.get("topics")
    if not isinstance(raw_topics, dict) or not raw_topics:
        raise ValueError("topics.json has no topics")
    if "receiver_weights" not in data and "conjunction_nature" not in data:
        # A table that only names topics is for tests of the topic rows alone.
        receivers = {p: 0.5 for p in _PLANETS}
        natures = {p: 0.0 for p in _PLANETS}
    else:
        receivers = _number_map(data.get("receiver_weights"), "receiver_weights", 0.0, 2.0)
        natures = _number_map(data.get("conjunction_nature"), "conjunction_nature", -1.0, 1.0)
    return TopicTable({name: _parse_topic(name, raw) for name, raw in raw_topics.items()}, receivers, natures)
