"""
python/harness/astro_rpc.py -- debug RPC for the topic-stance layer.

``astro.topic_stance`` shows what the chart says about a subject right now: the
topics detected in an optional ``message``, the stance on every topic with the
transit aspects that drove it, and the exact line that would be injected. It
exists so a stance can be inspected and traced back to the sky rather than taken
on trust. ``when`` (ISO-8601) asks about another moment.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)


def _parse_when(raw: object) -> datetime:
    text = str(raw or "").strip()
    if not text:
        from python.astrology.clock import astro_now

        return astro_now()
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


async def handle_astro_topic_stance(params: dict[str, Any], req_id: Any) -> str:
    """
    ``astro.topic_stance`` -- {message?, when?, agent_id?} -> detected topics, every stance with
    its drivers, the injected line and the ephemeris source.
    """
    try:
        try:
            when = _parse_when((params or {}).get("when"))
        except ValueError:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "when must be an ISO-8601 time")

        from python.astrology import ephemeris as eph
        from python.astrology import stance
        from python.harness import topic_stance
        from python.harness.emotion_service import resolve_agent_birthdate

        message = str((params or {}).get("message") or "")
        birth = await resolve_agent_birthdate((params or {}).get("agent_id"))
        stances = stance.compute_stances(birth, when)
        if stances is None:
            return _err(req_id, JsonRpcError.INTERNAL_ERROR,
                        "topic stance unavailable: it needs the real Swiss Ephemeris and a complete natal chart")
        detected = await topic_stance.detect_topics_async(message)
        lines = topic_stance.lines_for(detected, stances)
        return _ok(req_id, {
            "when": when.isoformat(),
            "ephemeris_source": eph.ephemeris_source(),
            "detected": detected,
            "line": " ".join(lines) or None,
            "threshold": stance.STANCE_THRESHOLD,
            "stances": {name: s.as_dict() for name, s in stances.items()},
        })
    except Exception as exc:
        logger.exception("astro.topic_stance error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
