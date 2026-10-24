"""
python/harness/voice_piper.py -- Piper, the local neural voice.

Piper is a small ONNX text-to-speech model that runs on the CPU, so the 6 GB GPU stays free for the language model. The
voice files (``<name>.onnx`` and ``<name>.onnx.json``) live under ``<models>/voice/piper`` on D:; ``scripts/setup_voice.py``
puts them there. Nothing is downloaded here, and the text never leaves the process.

``synthesize_wav`` is blocking (it is run in a worker thread by the callers); a loaded voice is cached because loading
costs about two seconds and a sentence costs a fraction of one.
"""
from __future__ import annotations

import io
import logging
import threading
import wave
from pathlib import Path

log = logging.getLogger(__name__)

_VOICES: dict[str, object] = {}
_LOCK = threading.Lock()


def _voice_file(name: str) -> Path:
    from python.harness.voice_settings import models_dir

    return models_dir() / "piper" / f"{name}.onnx"


def installed(name: str) -> bool:
    return _voice_file(name).is_file() and Path(str(_voice_file(name)) + ".json").is_file()


def module_present() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("piper") is not None
    except Exception:
        return False


def _load(name: str):
    from piper import PiperVoice

    with _LOCK:
        if name not in _VOICES:
            _VOICES[name] = PiperVoice.load(str(_voice_file(name)))
            log.info("voice: loaded Piper voice %s", name)
        return _VOICES[name]


def synthesize_wav(text: str, name: str) -> tuple[bytes, int]:
    """``(wav_bytes, sample_rate)`` for ``text``. Raises if the voice is not installed."""
    if not installed(name):
        raise RuntimeError(f"Piper voice {name!r} is not installed; run scripts/setup_voice.py")
    voice = _load(name)
    pcm = bytearray()
    rate = voice.config.sample_rate
    for chunk in voice.synthesize(text):
        pcm.extend(chunk.audio_int16_bytes)
    if not pcm:
        raise RuntimeError("Piper produced no audio")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    return buf.getvalue(), rate
