"""
python/astrology/valence.py — the emotion → valence projection, wired.

WHY THIS EXISTS
---------------
``nodes/spec-emotion-vocabulary.html`` has always carried a projection of every
emotion the engine can emit onto the four canonical valence keys — joy,
caution, curiosity, neutral — and stated, in its own prose, that those keys are
"read by ``update_manifest`` and ``adjust_behavioral_weights``".

Neither read them. ``update_manifest`` derived valence by keyword-matching a
node's own text; ``adjust_behavioral_weights`` shifted it from words in a
feedback string; and ``sdk/agent/tools.py`` never imported the astrology
package at all. The astro engine reached Tala's *voice* — the
``=== YOUR CURRENT EMOTIONAL STATE ===`` block in the system prompt — and
reached nothing that weighted her memory.

That made it the eighth instance of this codebase's dominant defect, and the
first where a node asserted the wiring: the store that is meant to be the
source of truth described a connection the code did not make, so every future
reader, human or model, would have believed it.

WHY THE TABLE LIVES IN THE NODE
-------------------------------
It is not duplicated here. The node is the source of truth for the projection,
exactly as ``PLANET_EMOTION_MAP`` is the source of truth for the words, and
this module is a reader. Copying sixty rows into Python would create precisely
the drift the emotion-vocabulary test exists to prevent.

HOW A MOOD MOVES A WEIGHT
-------------------------
Linear interpolation from the content-derived base toward the projected
valence, scaled by the emotional intensity the engine reported::

    blended = base + (projected - base) * intensity

At intensity 0 nothing moves and the base stands. At intensity 1 the projection
wins outright. Today's live state — ``hope`` at 0.96 — carries a base joy of
0.5 to 0.79. The mood colours the weighting; it does not replace it.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The four canonical keys. Anything outside this set is not a valence.
VALENCE_KEYS: tuple[str, ...] = ("joy", "caution", "curiosity", "neutral")

_SPEC_NODE = "spec-emotion-vocabulary.html"

_PROJECTION_CACHE: dict[str, dict[str, float]] | None = None
_PROJECTION_MTIME: float | None = None


def _spec_node_path() -> Path:
    """Locate the vocabulary node under whichever data root is in force."""
    try:
        from python.harness.config import settings
        root = Path(settings.REPO_ROOT)
    except Exception:
        root = Path(__file__).resolve().parents[2]
    return root / "nodes" / _SPEC_NODE


def load_projection(force: bool = False) -> dict[str, dict[str, float]]:
    """
    Return ``{emotion: {joy, caution, curiosity, neutral}}`` from the spec node.

    Re-reads when the node changes on disk, so editing the projection in the
    document takes effect without a restart — the node is the source of truth,
    and a cache that outlived an edit would quietly make that false.

    Returns an empty mapping if the node is missing or unreadable. Callers must
    treat empty as "could not look", never as "no emotions are projected"; the
    difference is the whole subject of the architecture contract.
    """
    global _PROJECTION_CACHE, _PROJECTION_MTIME

    path = _spec_node_path()
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        log.warning("valence: emotion vocabulary node unreadable at %s: %s", path, exc)
        return {}

    if not force and _PROJECTION_CACHE is not None and _PROJECTION_MTIME == mtime:
        return _PROJECTION_CACHE

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(
            r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', text, re.DOTALL
        )
        if not match:
            log.warning("valence: %s carries no node-state block", _SPEC_NODE)
            return {}
        state = json.loads(match.group(1).strip())
        raw = state.get("config", {}).get("valence_projection", {})
    except Exception as exc:
        log.warning("valence: could not parse %s: %s", _SPEC_NODE, exc)
        return {}

    projection: dict[str, dict[str, float]] = {}
    for emotion, keys in raw.items():
        if not isinstance(keys, dict):
            continue
        projection[str(emotion).lower()] = {
            k: float(keys.get(k, 0.5)) for k in VALENCE_KEYS
        }

    _PROJECTION_CACHE = projection
    _PROJECTION_MTIME = mtime
    log.debug("valence: loaded %d emotion projections from %s", len(projection), _SPEC_NODE)
    return projection


def project(emotion: str) -> dict[str, float] | None:
    """Valence for one emotion, or None if the vocabulary does not name it."""
    if not emotion:
        return None
    return load_projection().get(str(emotion).strip().lower())


def blend(base: dict[str, float],
          emotion: str,
          intensity: float) -> tuple[dict[str, float], bool]:
    """
    Carry *base* toward the projection of *emotion* in proportion to *intensity*.

    Returns ``(valence, moved)``. ``moved`` is False when the emotion is not in
    the vocabulary or the projection could not be loaded — the caller keeps its
    base values, and can record that the mood was unavailable rather than
    recording a neutral mood that was never measured.
    """
    projected = project(emotion)
    if not projected:
        return dict(base), False

    try:
        scale = max(0.0, min(1.0, float(intensity)))
    except (TypeError, ValueError):
        scale = 0.0

    out = dict(base)
    for key in VALENCE_KEYS:
        start = float(base.get(key, 0.5))
        out[key] = round(start + (projected[key] - start) * scale, 4)
    return out, True


def current_state_sync() -> tuple[str, float] | None:
    """
    The live astro-emotional state, without an event loop.

    Retrieval scoring is synchronous and sits in the hot path of every RAG
    lookup, so it cannot await the MCP tool. It reads the state
    ``emotion_service`` already persists — the same file the async path writes,
    including the inertia that keeps a mood from jumping between calls.

    Returns None when no state has been computed yet. That is not neutral, and
    callers must not treat it as such.
    """
    try:
        from python.harness.emotion_service import _load_previous
        state = _load_previous()
    except Exception as exc:
        log.debug("valence: could not read persisted emotional state: %s", exc)
        return None

    if not isinstance(state, dict):
        return None
    emotion = state.get("primary_emotion")
    if not emotion:
        return None
    try:
        intensity = float(state.get("intensity", 0.0))
    except (TypeError, ValueError):
        intensity = 0.0
    return str(emotion), intensity


def mood_alignment(node_valence: dict[str, float]) -> tuple[float, str] | None:
    """
    How well a node's valence suits the mood Tala is in, in ``[0, 1]``.

    Cosine-style affinity between the current emotion's projection and the
    node's own valence vector, so a node coloured the way she currently feels
    scores above one coloured against it.

    Returns None when there is no mood to compare against — "could not look",
    which the caller must keep distinct from an alignment of zero.
    """
    state = current_state_sync()
    if state is None:
        return None
    emotion, intensity = state
    projected = project(emotion)
    if not projected:
        return None

    dot = sum(projected[k] * float(node_valence.get(k, 0.0)) for k in VALENCE_KEYS)
    norm_a = sum(projected[k] ** 2 for k in VALENCE_KEYS) ** 0.5
    norm_b = sum(float(node_valence.get(k, 0.0)) ** 2 for k in VALENCE_KEYS) ** 0.5
    if norm_a <= 0 or norm_b <= 0:
        return None

    affinity = max(0.0, min(1.0, dot / (norm_a * norm_b)))
    # A faint mood should barely tilt anything; pull toward the neutral 0.5 in
    # proportion to how weakly it is felt.
    scale = max(0.0, min(1.0, intensity))
    affinity = 0.5 + (affinity - 0.5) * scale
    return affinity, emotion


async def current_state() -> tuple[str, float] | None:
    """
    The live astro-emotional state as ``(emotion, intensity)``.

    Routed through the same virtual ``astro-emotion`` MCP tool the system
    prompt uses, so there is one path to the engine and not two that can
    disagree. Returns None when no state is available — which is different
    from a neutral state, and is reported as such.
    """
    try:
        from python.harness.mcp_manager import mcp_manager
        result = await mcp_manager.call_tool(
            "astro-emotion", "get_emotional_state", {"agent_id": None}
        )
    except Exception as exc:
        log.debug("valence: no emotional state available: %s", exc)
        return None

    if not isinstance(result, dict):
        return None

    state: Any = result.get("emotional_state") or result.get("value") or result
    if not isinstance(state, dict):
        return None

    emotion = state.get("primary_emotion")
    if not emotion:
        return None
    try:
        intensity = float(state.get("intensity", 0.0))
    except (TypeError, ValueError):
        intensity = 0.0
    return str(emotion), intensity


async def apply_current(base: dict[str, float]) -> tuple[dict[str, float], dict[str, Any]]:
    """
    Blend *base* with whatever the engine feels right now.

    Returns the valence plus a provenance record, so a node carries how its
    weights were arrived at rather than four numbers with no account of
    themselves. ``source`` is ``"astro"`` when the mood moved the values and
    ``"content"`` when it did not, and the two are never conflated.
    """
    state = await current_state()
    if state is None:
        return dict(base), {"source": "content", "reason": "no emotional state available"}

    emotion, intensity = state
    blended, moved = blend(base, emotion, intensity)
    if not moved:
        return blended, {
            "source": "content",
            "reason": f"emotion {emotion!r} is not in the vocabulary projection",
        }
    return blended, {
        "source": "astro",
        "emotion": emotion,
        "intensity": round(intensity, 4),
    }
