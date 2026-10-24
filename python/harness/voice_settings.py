"""
python/harness/voice_settings.py -- how Tala's voice is configured, and where its models live.

``<user data>/voice.json``. Everything defaults to the safe, local choice:

* ``allow_cloud_tts`` is **off**. edge-tts sends the text to Microsoft's servers, so it is only ever used when this is
  turned on, and the settings UI says so next to the switch.
* the microphone is never on by default: ``listen_on_start`` is false, and nothing here persists "listening".
* models are looked up under ``TALA_MODELS_DIR`` / ``voice``, or ``<user data>/models/voice`` when it is not set (no drive
  letter: this runs from a USB stick on any OS; Kagura sets it to D:\\models); ``scripts/setup_voice.py`` puts
  them there. Nothing is fetched at run time.
* audio is never written to disk unless ``TALA_VOICE_DEBUG_DIR`` is set, and it must be on D:.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "allow_cloud_tts": False,
    #: A Kokoro voice id (af_heart, am_adam, ...); it must be installed by scripts/setup_voice.py.
    "tts_voice": "af_heart",
    "whisper_model": "base.en",
    "wake_enabled": True,
    "wake_phrase": "Hey Tala",
    #: Empty means "pick the best model present": a trained phrase model if one exists, else the pretrained fallback.
    "wake_model": "",
    #: None means "the model's own measured threshold, else 0.5"; a number is the user's explicit choice.
    "wake_threshold": None,
    "followup_seconds": 6.0,
    "listen_on_start": False,
}

#: What the UI may change. Anything else in the file is ignored rather than trusted.
_TYPES: dict[str, type] = {
    "allow_cloud_tts": bool, "tts_voice": str, "whisper_model": str, "wake_enabled": bool, "wake_phrase": str,
    "wake_model": str, "wake_threshold": float, "followup_seconds": float, "listen_on_start": bool,
}
_NULLABLE = frozenset({"wake_threshold"})
WHISPER_MODELS = ("tiny.en", "base.en", "small.en")


def user_data_dir() -> Path:
    from sdk.agent.documentation_nodes import resolve_user_data_dir

    return resolve_user_data_dir()


def _path() -> Path:
    return user_data_dir() / "voice.json"


def models_dir() -> Path:
    root = os.environ.get("TALA_MODELS_DIR")
    return (Path(root) if root else user_data_dir() / "models") / "voice"


def debug_dir() -> Path | None:
    """Where raw audio may be written, only when ``TALA_VOICE_DEBUG_DIR`` is set -- and only on D:."""
    raw = (os.environ.get("TALA_VOICE_DEBUG_DIR") or "").strip()
    if not raw:
        return None
    if raw.upper().startswith("C:"):
        log.error("voice: TALA_VOICE_DEBUG_DIR=%s is on C:; refusing to write audio there", raw)
        return None
    return Path(raw)


def _coerce(key: str, value: Any) -> Any:
    kind = _TYPES[key]
    if value is None and key in _NULLABLE:
        return None
    if kind is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if kind is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        return float(value)
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError(f"{key} must be a short string")
    return value.strip()


def read() -> dict[str, Any]:
    """The settings in force: defaults, overlaid with whatever valid values the file holds."""
    merged = dict(DEFAULTS)
    try:
        saved = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return merged
    if isinstance(saved, dict):
        for key in _TYPES:
            if key in saved:
                try:
                    merged[key] = _coerce(key, saved[key])
                except ValueError:
                    pass
    if merged["whisper_model"] not in WHISPER_MODELS:
        merged["whisper_model"] = DEFAULTS["whisper_model"]
    return merged


def update(changes: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist ``changes``; unknown keys and bad values raise ValueError and nothing is written."""
    clean: dict[str, Any] = {}
    for key, value in (changes or {}).items():
        if key not in _TYPES:
            raise ValueError(f"unknown voice setting: {key}")
        clean[key] = _coerce(key, value)
    if clean.get("whisper_model", "base.en") not in WHISPER_MODELS:
        raise ValueError(f"whisper_model must be one of {', '.join(WHISPER_MODELS)}")
    if clean.get("wake_threshold") is not None and not 0.05 <= clean["wake_threshold"] <= 0.99:
        raise ValueError("wake_threshold must be between 0.05 and 0.99")
    if "followup_seconds" in clean and not 0.0 <= clean["followup_seconds"] <= 60.0:
        raise ValueError("followup_seconds must be between 0 and 60")
    merged = {**read(), **clean}
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged
