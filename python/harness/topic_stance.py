"""
python/harness/topic_stance.py -- turn her chart's stance into one line she can feel.

``python.astrology.stance`` scores every topic from today's sky. This module
decides which topic a message is about, words the stance as a single line, and
wraps it in a short, bounded block for ``system_context_additions``.

Detecting the topic is cheap and local:

1. **Keywords**, matched as whole words (``topics.json``) -- free, deterministic,
   and enough for nearly every message.
2. **Embeddings**, only when no keyword matched and the message is long enough to
   carry meaning: Ollama's ``nomic-embed-text`` (US-origin, already on D:), run on
   the CPU so it never competes with the chat model for VRAM, compared with each
   topic's description. Loopback Ollama only -- a message is never sent anywhere
   else -- and any failure just means "no topic". ``TALA_TOPIC_EMBEDDINGS=0`` turns
   it off.

The server appends the block LAST, after the retrieved node context, so it sits closest to the
user's turn. Appended earlier it was buried under that long context and a small model gave it
almost no weight.

The block says its own limits: the stance colours her disposition and word
choice, she stays honest, and it never changes safety rules, tool permissions or
what she will do. It is only built in the in-character chat modes, like her mood.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import urllib.request
from typing import Sequence
from urllib.parse import urlparse

from python.astrology import stance
from python.astrology.clock import astro_now
from python.astrology.topics import load_table
from python.harness.chat_mode import is_emotional_mode

log = logging.getLogger(__name__)

EMBED_MODEL = "nomic-embed-text"
#: Cosine similarity a message needs against a topic description to count.
SEMANTIC_THRESHOLD = 0.55
SEMANTIC_MAX_TOPICS = 2
#: Shorter messages ("good morning") carry too little to embed; longer ones are trimmed.
SEMANTIC_MIN_WORDS = 4
SEMANTIC_MAX_CHARS = 500
EMBED_TIMEOUT_S = 3.0
_VECTOR_TIMEOUT_S = 20.0
MAX_LINES = 2

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})

_PHRASES = {
    "averse": "makes you averse to {phrase}",
    "wary": "makes you wary of {phrase}",
    "open": "leaves you open to {phrase}",
    "eager": "makes you more open, even assertive, about {phrase}",
}

_BLOCK_HEAD = "=== YOUR CHART'S STANCE TODAY ==="
_BLOCK_FOOT = "=" * len(_BLOCK_HEAD)
_GUARD = ("This colours your disposition and word choice on the topic. Stay honest and in character; "
          "it never changes your safety rules, tool permissions, or what you will do.")


# --- keyword detection ----------------------------------------------------------------------


def _detect_topics(message: object) -> list[str]:
    """Topics a message is plainly about, by whole-word keyword match, in table order."""
    text = message if isinstance(message, str) else ""
    if not text.strip():
        return []
    return [name for name, topic in load_table().topics.items() if topic.pattern.search(text)]


# --- the optional embedding layer -----------------------------------------------------------------

_vectors: dict[str, list[float]] | None = None
_vector_lock: asyncio.Lock | None = None


def _reset_semantic_cache() -> None:
    global _vectors, _vector_lock
    _vectors, _vector_lock = None, None


def _embeddings_enabled() -> bool:
    return os.environ.get("TALA_TOPIC_EMBEDDINGS", "1").strip() != "0"


def _loopback_embed_url(base_url: str) -> str | None:
    """The embed endpoint for ``base_url``, only if it is this machine."""
    try:
        parsed = urlparse(base_url or "")
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "") not in _LOOPBACK:
        return None
    return f"{base_url.rstrip('/')}/api/embed"


def _embed_endpoint() -> str | None:
    from python.inference import profiles

    active = profiles.active()
    return _loopback_embed_url(active.ollama_url if active else "http://localhost:11434")


def _post_embed(endpoint: str, texts: list[str], timeout: float) -> list[list[float]]:
    body = json.dumps({
        "model": EMBED_MODEL, "input": texts, "keep_alive": "10m",
        "options": {"num_gpu": 0},  # CPU: the chat model owns the VRAM
    }).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- loopback only, checked above
        return json.loads(response.read())["embeddings"]


async def _ollama_embed(texts: list[str]) -> list[list[float]]:
    endpoint = _embed_endpoint()
    if endpoint is None:
        raise OSError("the embedding endpoint is not a loopback Ollama")
    timeout = _VECTOR_TIMEOUT_S if len(texts) > 1 else EMBED_TIMEOUT_S
    return await asyncio.to_thread(_post_embed, endpoint, texts, timeout)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


async def _topic_vectors() -> dict[str, list[float]]:
    """One embedding per topic description, computed once per process."""
    global _vectors, _vector_lock
    if _vectors is not None:
        return _vectors
    if _vector_lock is None:
        _vector_lock = asyncio.Lock()
    async with _vector_lock:
        if _vectors is None:
            topics = load_table().topics
            names = list(topics)
            vectors = await _ollama_embed([f"search_document: {topics[n].description}" for n in names])
            _vectors = dict(zip(names, vectors))
    return _vectors


async def _detect_semantic(message: str) -> list[str]:
    vectors = await _topic_vectors()
    (query,) = await _ollama_embed([f"search_query: {message[:SEMANTIC_MAX_CHARS]}"])
    scored = sorted(((_cosine(query, v), name) for name, v in vectors.items()), reverse=True)
    return [name for score, name in scored[:SEMANTIC_MAX_TOPICS] if score >= SEMANTIC_THRESHOLD]


async def detect_topics_async(message: object) -> list[str]:
    """Keywords first; the embedder only when they found nothing. Never raises."""
    found = _detect_topics(message)
    if found or not _embeddings_enabled() or not isinstance(message, str):
        return found
    if len(message.split()) < SEMANTIC_MIN_WORDS:
        return []
    try:
        return await _detect_semantic(message)
    except Exception as exc:
        log.debug("topic embedding unavailable (%s); no topic detected", exc)
        return []


# --- the line and the block ------------------------------------------------------------------------


def _build_line(topic_stance: stance.TopicStance) -> str | None:
    """One sentence for a topic she would voice a stance on, else None."""
    if abs(topic_stance.score) <= stance.STANCE_THRESHOLD:
        return None
    phrase = load_table().topics[topic_stance.topic].phrase
    wording = _PHRASES[topic_stance.label].format(phrase=phrase)
    causes = "; ".join(d.describe() for d in topic_stance.drivers[:2])
    return f"Your chart today {wording} ({causes})." if causes else f"Your chart today {wording}."


def lines_for(topics_found: Sequence[str], stances: dict[str, stance.TopicStance]) -> list[str]:
    """Lines for the detected topics, strongest stance first, at most MAX_LINES."""
    ranked = sorted((stances[t] for t in topics_found if t in stances), key=lambda s: abs(s.score), reverse=True)
    return [line for line in (_build_line(s) for s in ranked) if line][:MAX_LINES]


async def stance_context_block(message: object, chat_mode: str, agent_id: str | None = None,
                               when=None) -> str | None:
    """The stance block for this turn, or None. Never raises: a missing stance costs the reply a nuance, not the reply."""
    if not is_emotional_mode(chat_mode):
        return None
    try:
        found = await detect_topics_async(message)
        if not found:
            return None
        from python.harness.emotion_service import resolve_agent_birthdate

        stances = stance.compute_stances(await resolve_agent_birthdate(agent_id), when or astro_now())
        if stances is None:
            return None
        lines = lines_for(found, stances)
        if not lines:
            return None
        log.info("topic stance injected for this turn: %s", " | ".join(lines))
        return "\n".join([_BLOCK_HEAD, *lines, _GUARD, _BLOCK_FOOT])
    except Exception as exc:
        log.warning("topic stance skipped for this turn: %s", exc)
        return None
