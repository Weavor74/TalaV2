"""
python/harness/voice_rpc.py -- the renderer's way into the local voice loop.

``voice.listen_start`` / ``voice.listen_stop`` turn listening on and off; ``voice.audio`` carries the microphone (16 kHz mono
PCM16 in short base64 chunks) and returns the loop's state, which is the UI's back-channel; ``voice.playback`` and
``voice.turn_done`` report what the UI heard and when the model finished; ``voice.interrupt`` is a deliberate "stop talking";
``voice.get_settings`` / ``voice.set_settings`` configure the voice. Pushed to the UI: ``voice.state``, ``voice.wake``,
``voice.partial``, ``voice.transcript``, ``voice.turn``, ``voice.audio_chunk``, ``voice.interrupt``.

Listening is a UI action. None of these is a registered tool, so the agent cannot switch the microphone on, and none is on the
emergency halt's allow-list, so a halt refuses all of them (and ``start`` refuses on its own as well).
"""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Any

from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)

MODES = ("continuous", "dictate")
PLAYBACK_STATES = ("started", "ended")
#: A chunk is a fraction of a second of audio. 256 KB is about eight seconds, the most a stalled UI would batch.
MAX_AUDIO_BYTES = 256 * 1024
MAX_REPLY_CHARS = 20_000
CLOUD_WARNING = "edge-tts sends the text to Microsoft's servers. Speech then leaves this machine."


def _runtime():
    from python.harness import voice_runtime

    return voice_runtime.get()


async def _handle_listen_start(params: dict[str, Any], req_id: Any) -> str:
    mode = (params or {}).get("mode", "continuous")
    if mode not in MODES:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, f"mode must be one of {', '.join(MODES)}")
    try:
        return _ok(req_id, await _runtime().start(mode))
    except Exception as exc:
        logger.warning("voice.listen_start refused: %s", exc)
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_listen_stop(params: dict[str, Any], req_id: Any) -> str:
    try:
        return _ok(req_id, await _runtime().stop())
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_audio(params: dict[str, Any], req_id: Any) -> str:
    raw = (params or {}).get("pcm_base64")
    if not isinstance(raw, str) or not raw or len(raw) > MAX_AUDIO_BYTES * 4 // 3 + 8:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "pcm_base64 must be a short base64 string of 16-bit audio")
    try:
        pcm = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "pcm_base64 is not valid base64")
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_AUDIO_BYTES:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "audio must be whole 16-bit samples and at most a few seconds")
    try:
        return _ok(req_id, await _runtime().ingest(pcm))
    except Exception as exc:
        logger.exception("voice.audio error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_playback(params: dict[str, Any], req_id: Any) -> str:
    state, turn_id = (params or {}).get("state"), (params or {}).get("turn_id")
    if state not in PLAYBACK_STATES or not isinstance(turn_id, str) or not turn_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "state must be started or ended, with a turn_id")
    try:
        return _ok(req_id, await _runtime().playback(state, turn_id))
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_turn_done(params: dict[str, Any], req_id: Any) -> str:
    turn_id, text = (params or {}).get("turn_id"), (params or {}).get("text")
    if not isinstance(turn_id, str) or not turn_id or (text is not None and (not isinstance(text, str) or len(text) > MAX_REPLY_CHARS)):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "turn_id (string) is required; text is optional and bounded")
    try:
        return _ok(req_id, {"ok": bool(_runtime().turn_done(turn_id, text))})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_interrupt(params: dict[str, Any], req_id: Any) -> str:
    try:
        return _ok(req_id, await _runtime().interrupt())
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def _handle_status(params: dict[str, Any], req_id: Any) -> str:
    from python.harness import voice_runtime

    runtime = voice_runtime.peek()
    return _ok(req_id, runtime.status() if runtime else {"state": "off", "listening": False, "audio_stays_local": True})


async def _handle_get_settings(params: dict[str, Any], req_id: Any) -> str:
    from python.harness import voice, voice_settings

    return _ok(req_id, {"settings": voice_settings.read(), "backends": await voice.list_backends(),
                        "models_dir": str(voice_settings.models_dir())})


async def _handle_set_settings(params: dict[str, Any], req_id: Any) -> str:
    from python.harness import voice_runtime, voice_settings

    changes = (params or {}).get("settings")
    if not isinstance(changes, dict) or not changes:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "settings must be an object of changes")
    try:
        merged = voice_settings.update(changes)
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    runtime = voice_runtime.peek()
    if runtime:
        runtime.apply_settings()
    warnings = [CLOUD_WARNING] if merged.get("allow_cloud_tts") else []
    return _ok(req_id, {**merged, "warnings": warnings})


VOICE_RPC_HANDLERS = {
    "voice.listen_start": _handle_listen_start,
    "voice.listen_stop": _handle_listen_stop,
    "voice.audio": _handle_audio,
    "voice.playback": _handle_playback,
    "voice.turn_done": _handle_turn_done,
    "voice.interrupt": _handle_interrupt,
    "voice.status": _handle_status,
    "voice.get_settings": _handle_get_settings,
    "voice.set_settings": _handle_set_settings,
}
