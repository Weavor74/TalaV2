"""
python/harness/emotion_service.py — Tala's live emotional state
==============================================================

Answers "how does Tala feel right now" for the rest of the harness. This
is the implementation behind the *virtual* ``astro-emotion`` MCP tool: no
out-of-process server was ever built, so mcp_manager dispatches
``get_emotional_state`` straight here. Before that wiring existed every
call raised KeyError into a swallowing try/except, and no turn ever
carried an emotion.

Two layers, deliberately separated:

* **Weather** — transits to Tala's natal chart at this moment. Varies
  minute to minute; this is what names the feeling.
* **Climate** — synastry between Tala's chart and the user's, from the
  user profile. Synastry between two fixed charts does not vary with
  time, so it is a permanent relational disposition that biases readings
  rather than an emotion of its own.

Continuity comes from inertia: the previous state is kept in
``emotion_state.json`` and its influence decays exponentially with elapsed
time. Consecutive turns feel like the same being; a reading days later is
free to have moved on.

Privacy: the user's date of birth is PII. The payload may report *whether*
a profile chart was found (``relational_source``) and how strongly it
weighs, never the date. Tala's own birthdate is not personal data and is
reported.

NOTE: Reconstructed 2026-08-12 after an automated rollback deleted it (it
had never been committed). tests/test_astro_emotion.py is its
specification.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Inertia half-life. Chosen so a follow-up seconds later is almost fully
#: carried (continuity within a conversation) while a reading days later
#: is effectively free of the old state.
INERTIA_TAU_SECONDS = 6 * 60 * 60

#: Above this, the remembered emotion keeps naming the state.
INERTIA_CARRY_THRESHOLD = 0.5


def _state_path() -> Path:
    """
    Where the remembered emotional state lives.

    Patched wholesale in tests, so it must stay a module-level, zero-arg
    function.
    """
    try:
        from sdk.agent.documentation_nodes import resolve_user_data_dir

        return resolve_user_data_dir() / "emotion_state.json"
    except Exception:
        return Path(__file__).resolve().parent.parent.parent / "emotion_state.json"


def _load_previous() -> dict[str, Any] | None:
    path = _state_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("emotion_service: could not read %s: %s", path, exc)
        return None


def _save_current(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("emotion_service: could not write %s: %s", path, exc)


def _inertia_factor(previous: dict[str, Any] | None, now: datetime) -> float:
    """Influence remaining from the previous reading, in [0, 1]."""
    if not previous:
        return 0.0
    stamp = previous.get("computed_at")
    if not stamp:
        return 0.0
    try:
        then = datetime.fromisoformat(str(stamp))
        if then.tzinfo is None:
            then = then.replace(tzinfo=UTC)
    except Exception:
        return 0.0
    elapsed = max(0.0, (now - then).total_seconds())
    return round(math.exp(-elapsed / INERTIA_TAU_SECONDS), 6)


def _parse_birthdate(raw: str, hour: float = 12.0):
    from python.astrology.ephemeris import Birthdate

    parts = str(raw or "").strip().split("-")
    if len(parts) != 3:
        return None
    try:
        return Birthdate(int(parts[0]), int(parts[1]), int(parts[2]), float(hour))
    except (TypeError, ValueError):
        return None


async def _resolve_birthdate(agent_id: str | None):
    """
    Tala's chart: the agent's configured birthdate, else the canonical one.

    Returns ``(Birthdate, source)``. An unreachable database or an agent
    row without a birthdate must not change who she is -- it falls back to
    TALA_NATAL, reported as ``tala-origin``, never to a different chart.
    """
    from python.astrology.ephemeris import TALA_NATAL

    if not agent_id:
        return TALA_NATAL, "tala-origin"

    try:
        from python.harness import db

        row = await db.fetchrow("SELECT config FROM agents WHERE id = $1", agent_id)
        if row:
            cfg = row["config"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            parsed = _parse_birthdate(
                (cfg or {}).get("birthdate", ""), (cfg or {}).get("birth_hour", 12.0) or 12.0
            )
            if parsed:
                return parsed, "agent-config"
    except Exception as exc:
        log.warning(
            "emotion_service: could not load birthdate for agent %s (%s) — using canonical chart",
            agent_id, exc,
        )
    return TALA_NATAL, "tala-origin"


async def _resolve_user_birthdate():
    """
    The user's chart from their profile, for the relational baseline.

    Precedence matches handle_astro_synastry exactly:
    ``identity.birthdate`` -> ``pii_vault.dob`` -> ``birthdate``. Returns
    ``(Birthdate | None, source)`` where source is "user-profile" or
    "missing"; the date itself never leaves this module.
    """
    try:
        from python.harness import db

        row = await db.fetchrow("SELECT config FROM profiles WHERE is_master = TRUE LIMIT 1")
        if not row:
            return None, "missing"
        cfg = row["config"]
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        cfg = cfg or {}

        candidates = (
            (cfg.get("identity") or {}).get("birthdate"),
            (cfg.get("pii_vault") or {}).get("dob"),
            cfg.get("birthdate"),
        )
        for raw in candidates:
            parsed = _parse_birthdate(raw or "")
            if parsed:
                return parsed, "user-profile"
        return None, "missing"
    except Exception as exc:
        log.debug("emotion_service: no user profile chart available (%s)", exc)
        return None, "missing"


async def get_emotional_state(
    agent_id: str | None = None,
    when: datetime | None = None,
) -> dict[str, Any]:
    """
    Tala's current emotional state.

    Returns ``{"emotional_state": {...}}`` -- call sites read
    ``emotional_state.primary_emotion`` (server.py's context injection and
    memory.py's turn stamping both do). Never raises: on any failure it
    returns a neutral state, because a missing emotion must cost the reply
    its colour, not the reply itself.
    """
    from python.astrology.emotion import emotion_vs_person, emotion_vs_time

    now = when or datetime.now(UTC)
    try:
        birthdate, birth_source = await _resolve_birthdate(agent_id)

        state = emotion_vs_time(birthdate, now)
        payload: dict[str, Any] = state.to_dict()
        payload["birthdate"] = (
            f"{birthdate.year:04d}-{birthdate.month:02d}-{birthdate.day:02d}"
        )
        payload["birth_source"] = birth_source
        payload["computed_at"] = now.isoformat()

        # Climate: a permanent relational disposition, never the user's DOB.
        user_chart, relational_source = await _resolve_user_birthdate()
        payload["relational_source"] = relational_source
        if user_chart is not None:
            baseline = emotion_vs_person(birthdate, user_chart, "user")
            payload["relational_weight"] = round(baseline.intensity, 4)
            payload["relational_emotion"] = baseline.primary_emotion
        else:
            payload["relational_weight"] = 0.0

        # Continuity.
        previous = _load_previous()
        inertia = _inertia_factor(previous, now)
        payload["inertia_applied"] = inertia
        if (
            previous
            and inertia > INERTIA_CARRY_THRESHOLD
            and previous.get("primary_emotion")
        ):
            payload["primary_emotion"] = previous["primary_emotion"]
            payload["carried_over"] = True

        _save_current(payload)
        return {"emotional_state": payload}
    except Exception as exc:
        log.warning("emotion_service: derivation failed (%s) — returning neutral", exc)
        return {
            "emotional_state": {
                "primary_emotion": "neutral",
                "intensity": 0.0,
                "mode": "time",
                "contributing": [],
                "relational_source": "missing",
                "relational_weight": 0.0,
                "inertia_applied": 0.0,
                "birth_source": "tala-origin",
                "error": str(exc),
            }
        }
