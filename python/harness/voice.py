"""
python/harness/voice.py — Text-to-speech backends
=================================================

Synthesises speech and hands it back as base64 audio; the caller
(``speak_text`` in sdk/agent/tools.py, ``voice.speak`` over RPC)
broadcasts a ``voice.play`` notification so every connected UI plays it.
Nothing here touches the sound card — the renderer does the playing, which
keeps it working when the harness is headless or remote.

Resolution order, first available wins:

1. An explicitly requested ``provider_id`` from the inference-provider
   table (a cloud TTS with an API key).
2. ``edge-tts`` — good quality, no key, network required.
3. ``pyttsx3`` — offline system voices (SAPI on Windows, NSSpeech on
   macOS, espeak on Linux).

Every path returns a dict rather than raising: a failed synthesis should
cost the reply its audio, not the reply. Callers check for ``error``.

NOTE: Reconstructed 2026-08-12 after an automated rollback deleted it (it
had never been committed). The contract is taken from its call sites:
``synthesize_speech`` returns ``audio_base64`` / ``mime_type`` /
``backend`` / ``voice``, and ``list_backends`` reports availability.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_EDGE_VOICE = "en-GB-SoniaNeural"


def _has_module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


async def list_backends() -> dict[str, Any]:
    """
    Which TTS backends are usable right now, in resolution order.

    Reported to the settings UI so the user can see why speech is or is
    not available without reading logs.
    """
    backends = [
        {
            "id": "edge-tts",
            "name": "Microsoft Edge TTS",
            "available": _has_module("edge_tts"),
            "offline": False,
            "note": "High quality, no API key, requires network.",
        },
        {
            "id": "pyttsx3",
            "name": "System voices (offline)",
            "available": _has_module("pyttsx3"),
            "offline": True,
            "note": "SAPI / NSSpeech / espeak. Always local.",
        },
    ]
    return {
        "backends": backends,
        "default": next((b["id"] for b in backends if b["available"]), None),
        "any_available": any(b["available"] for b in backends),
    }


async def _synthesize_edge(text: str, voice: str | None) -> dict[str, Any]:
    import edge_tts  # noqa: PLC0415

    chosen = voice or DEFAULT_EDGE_VOICE
    communicate = edge_tts.Communicate(text, chosen)
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            audio.extend(chunk["data"])
    if not audio:
        raise RuntimeError("edge-tts produced no audio")
    return {
        "audio_base64": base64.b64encode(bytes(audio)).decode("ascii"),
        "mime_type": "audio/mpeg",
        "backend": "edge-tts",
        "voice": chosen,
    }


def _synthesize_pyttsx3_blocking(text: str, voice: str | None) -> dict[str, Any]:
    import pyttsx3  # noqa: PLC0415

    engine = pyttsx3.init()
    if voice:
        for v in engine.getProperty("voices") or []:
            if voice.lower() in (getattr(v, "name", "") or "").lower():
                engine.setProperty("voice", v.id)
                break

    tmp_dir = Path(tempfile.mkdtemp(prefix="tala-tts-"))
    out = tmp_dir / "speech.wav"
    try:
        engine.save_to_file(text, str(out))
        engine.runAndWait()
        data = out.read_bytes()
        if not data:
            raise RuntimeError("pyttsx3 produced no audio")
        return {
            "audio_base64": base64.b64encode(data).decode("ascii"),
            "mime_type": "audio/wav",
            "backend": "pyttsx3",
            "voice": voice,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _synthesize_provider(text: str, provider_id: str, voice: str | None) -> dict[str, Any]:
    """
    Explicitly requested cloud provider from the inference-provider table.

    Kept narrow on purpose: an OpenAI-compatible ``/audio/speech`` call,
    which is what every configured provider in this project exposes.
    """
    import httpx

    from python.harness import db

    row = await db.fetchrow(
        "SELECT name, config FROM inference_providers WHERE id = $1", provider_id
    )
    if not row:
        raise RuntimeError(f"TTS provider {provider_id!r} not found")
    cfg = row["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    cfg = cfg or {}

    endpoint = str(cfg.get("endpoint", "")).rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("tts_model") or cfg.get("model") or "tts-1"
    if not endpoint:
        raise RuntimeError(f"provider {row['name']!r} has no endpoint configured")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{endpoint}/audio/speech",
            headers=headers,
            json={"model": model, "input": text, "voice": voice or "alloy"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return {
            "audio_base64": base64.b64encode(resp.content).decode("ascii"),
            "mime_type": resp.headers.get("content-type", "audio/mpeg"),
            "backend": f"provider:{row['name']}",
            "voice": voice,
        }


async def synthesize_speech(
    text: str,
    provider_id: str | None = None,
    voice: str | None = None,
) -> dict[str, Any]:
    """
    Synthesise `text` and return base64 audio.

    Returns ``{"audio_base64", "mime_type", "backend", "voice"}`` on
    success, or ``{"error": ...}`` — callers check for the error key
    rather than catching, so a missing backend degrades cleanly to a
    silent reply.
    """
    clean = str(text or "").strip()
    if not clean:
        return {"error": "no text to speak"}

    attempts: list[tuple[str, Any]] = []
    if provider_id:
        attempts.append(("provider", lambda: _synthesize_provider(clean, provider_id, voice)))
    if _has_module("edge_tts"):
        attempts.append(("edge-tts", lambda: _synthesize_edge(clean, voice)))
    if _has_module("pyttsx3"):
        attempts.append(
            ("pyttsx3", lambda: asyncio.to_thread(_synthesize_pyttsx3_blocking, clean, voice))
        )

    if not attempts:
        return {
            "error": (
                "no TTS backend available — install 'edge-tts' for online "
                "synthesis or 'pyttsx3' for offline system voices"
            )
        }

    errors: list[str] = []
    for name, run in attempts:
        try:
            return await run()
        except Exception as exc:
            log.warning("voice: %s backend failed: %s", name, exc)
            errors.append(f"{name}: {exc}")

    return {"error": "all TTS backends failed — " + "; ".join(errors)}
