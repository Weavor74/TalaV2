"""
python/astrology/clock.py -- the one place astrology asks what time it is.

``TALA_ASTRO_NOW`` (an ISO-8601 time, e.g. ``2027-06-18T12:00:00Z``) pins that
clock for a debugging or demonstration run, so the same question can be asked
under two different skies and the answers compared. It moves the mood and the
topic stance together, because both read the clock here. It is a development
aid, not a setting: it logs a warning the first time it takes effect, and an
unreadable value is ignored rather than trusted.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_warned = False


def astro_now() -> datetime:
    """The moment the sky is read for: now, unless TALA_ASTRO_NOW pins it."""
    global _warned
    raw = os.environ.get("TALA_ASTRO_NOW", "").strip()
    if raw:
        try:
            pinned = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("TALA_ASTRO_NOW=%r is not an ISO-8601 time; using the real clock", raw)
        else:
            if pinned.tzinfo is None:
                pinned = pinned.replace(tzinfo=UTC)
            if not _warned:
                logger.warning("TALA_ASTRO_NOW is set: astrology is reading the sky for %s, not now", pinned.isoformat())
                _warned = True
            return pinned.astimezone(UTC)
    return datetime.now(UTC)
