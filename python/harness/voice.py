"""
python/harness/voice.py — Text-to-speech backends
=================================================

Synthesises speech and hands it back as base64 audio; the caller
(``speak_text`` in sdk/agent/tools.py, ``voice.speak`` over RPC)
broadcasts a ``voice.play`` notification so every connected UI plays it.
Nothing here touches the sound card — the renderer does the playing, which
keeps it working when the harness is headless or remote.

Tala is local-only, so the chain is local by construction:

1. **Piper** (default) — a small neural ONNX voice on the CPU. Voice files live
   on D: (``scripts/setup_voice.py``); nothing is downloaded at run time.
2. **pyttsx3** — offline system voices (SAPI on Windows, NSSpeech on macOS,
   espeak on Linux). Always local.
3. **edge-tts** — *only when the user has turned ``allow_cloud_tts`` on*, and
   only after the local voices. It sends the text to Microsoft's servers, so
   it is off by default and is never chosen just because nothing else works.

A provider-table TTS (an OpenAI-style ``/audio/speech`` endpoint with a key) is
tried first only when explicitly requested **and its endpoint is on this
machine**; a remote one is refused without being contacted unless
``TALA_ALLOW_REMOTE_PROVIDERS=1``.

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
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from python.harness import voice_piper, voice_settings

log = logging.getLogger(__name__)

DEFAULT_EDGE_VOICE = "en-GB-SoniaNeural"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _has_module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _endpoint_is_local(endpoint: str) -> bool:
    """True only for a loopback host. A LAN address is not this machine, and neither is ``localhost.evil.example``."""
    try:
        host = urlparse(str(endpoint or "")).hostname
    except ValueError:
        return False
    return host in LOCAL_HOSTS


def _tts_provider_allowed(endpoint: str) -> bool:
    """Whether a provider-table TTS endpoint may be used: loopback, or the explicit remote override."""
    return _endpoint_is_local(endpoint) or os.environ.get("TALA_ALLOW_REMOTE_PROVIDERS", "").strip() == "1"


def _available() -> dict[str, bool]:
    settings = voice_settings.read()
    return {
        "piper": voice_piper.module_present() and voice_piper.installed(settings["piper_voice"]),
        "pyttsx3": _has_module("pyttsx3"),
        "edge-tts": _has_module("edge_tts"),
    }


def _select_backends(settings: dict[str, Any], available: dict[str, bool]) -> list[str]:
    """The ordered backends to try. edge-tts appears only if the user enabled it, and always last."""
    chain = [name for name in ("piper", "pyttsx3") if available.get(name)]
    if settings.get("allow_cloud_tts") and available.get("edge-tts"):
        chain.append("edge-tts")
    return chain


async def list_backends() -> dict[str, Any]:
    """
    Which TTS backends are usable right now, in resolution order.

    Reported to the settings UI so the user can see why speech is or is
    not available without reading logs, and which ones leave the machine.
    """
    settings = voice_settings.read()
    available = _available()
    chain = _select_backends(settings, available)
    backends = [
        {
            "id": "piper",
            "name": f"Piper ({settings['piper_voice']})",
            "available": available["piper"],
            "offline": True,
            "enabled": True,
            "note": "Local neural voice on the CPU. Voice files on D:; nothing leaves this machine.",
        },
        {
            "id": "pyttsx3",
            "name": "System voices (offline)",
            "available": available["pyttsx3"],
            "offline": True,
            "enabled": True,
            "note": "SAPI / NSSpeech / espeak. Always local.",
        },
        {
            "id": "edge-tts",
            "name": "Microsoft Edge TTS",
            "available": available["edge-tts"],
            "offline": False,
            "enabled": bool(settings["allow_cloud_tts"]),
            "note": "CLOUD: sends the text to Microsoft's servers. Off by default; turn on allow_cloud_tts to use it.",
        },
    ]
    return {
        "backends": backends,
        "default": chain[0] if chain else None,
        "any_available": bool(chain),
    }


async def _synthesize_piper(text: str, voice: str | None) -> dict[str, Any]:
    name = voice if voice and voice_piper.installed(voice) else voice_settings.read()["piper_voice"]
    wav, _rate = await asyncio.to_thread(voice_piper.synthesize_wav, text, name)
    return {
        "audio_base64": base64.b64encode(wav).decode("ascii"),
        "mime_type": "audio/wav",
        "backend": "piper",
        "voice": name,
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


async def _synthesize_pyttsx3(text: str, voice: str | None) -> dict[str, Any]:
    return await asyncio.to_thread(_synthesize_pyttsx3_blocking, text, voice)


async def _synthesize_provider(text: str, provider_id: str, voice: str | None) -> dict[str, Any]:
    """
    Explicitly requested provider from the inference-provider table.

    Kept narrow on purpose: an OpenAI-compatible ``/audio/speech`` call, which is what every configured provider in
    this project exposes. The endpoint is checked **before** any client is built, so a remote endpoint is never
    contacted and its key is never sent.
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
    if not _tts_provider_allowed(endpoint):
        raise RuntimeError(
            f"provider {row['name']!r} is not on this machine, and Tala's voice is local-only; "
            "its endpoint was not contacted (TALA_ALLOW_REMOTE_PROVIDERS=1 overrides)"
        )

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

    runners = {"piper": _synthesize_piper, "pyttsx3": _synthesize_pyttsx3, "edge-tts": _synthesize_edge}
    attempts: list[tuple[str, Any]] = []
    if provider_id:
        attempts.append(("provider", lambda: _synthesize_provider(clean, provider_id, voice)))
    for name in _select_backends(voice_settings.read(), _available()):
        attempts.append((name, lambda run=runners[name]: run(clean, voice)))

    if not attempts:
        return {
            "error": (
                "no local TTS backend available — run scripts/setup_voice.py to install the Piper voice "
                "(or install 'pyttsx3' for offline system voices); cloud TTS is off unless allow_cloud_tts is on"
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
