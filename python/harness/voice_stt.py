"""
python/harness/voice_stt.py -- speech-to-text with faster-whisper, on the CPU.

faster-whisper (CTranslate2) runs ``base.en`` (``small.en`` is selectable) in int8 on the CPU, so the 6 GB GPU stays free for the language model. The
model is loaded from ``<models>/voice/whisper/<name>`` on D:, with no download path: a missing model is an error that names
``scripts/setup_voice.py``. ``transcribe`` is blocking and meant for a worker thread.

Two things this module does beyond calling the model:

* it refuses to turn silence into words. Whisper invents "Thank you." from a quiet segment; segments the model itself
  rates as probably-not-speech and low-confidence are dropped;
* :func:`strip_wake_phrase` takes the wake phrase off the front of a transcript. The wake word was already detected
  acoustically, so this is cosmetic, but Whisper hears "Tala" as "Tullah" or "Talia", so the match is fuzzy, and anything
  that does not match is left exactly as heard.
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MIN_SAMPLES = int(0.25 * SAMPLE_RATE)
NO_SPEECH_PROB = 0.6
LOW_CONFIDENCE = -1.0
WORD_MATCH = 0.55


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text)


def strip_wake_phrase(text: str, phrase: str) -> str:
    """Remove ``phrase`` from the start of ``text`` if every word of it matches the leading words, fuzzily."""
    wanted = [w.lower() for w in _words(phrase)]
    if not wanted:
        return text
    spans = list(re.finditer(r"[A-Za-z0-9']+", text))
    if len(spans) < len(wanted):
        return text
    for span, want in zip(spans, wanted):
        if difflib.SequenceMatcher(None, span.group(0).lower(), want).ratio() < WORD_MATCH:
            return text
    rest = text[spans[len(wanted) - 1].end():]
    return rest.lstrip(" \t,.;:!?-").strip()


class SttEngine:
    def __init__(self, model: Any = None, hint: str = "Tala"):
        self._model = model
        self._hint = hint
        self._lock = threading.Lock()

    def _quiet_progress(self) -> None:
        """tqdm starts a monitor thread the first time a bar is made, and faster-whisper makes one per transcription from a worker
        thread; starting a thread while another is loading a DLL is what froze the harness (see voice_preload)."""
        try:
            import tqdm

            tqdm.tqdm.monitor_interval = 0
        except ImportError:
            pass

    def load(self) -> None:
        if self._model is not None:
            return
        self._quiet_progress()
        from python.harness import voice_settings

        name = voice_settings.read()["whisper_model"]
        path = voice_settings.models_dir() / "whisper" / name
        if not (path / "model.bin").is_file():
            raise RuntimeError(f"whisper model {name!r} is not installed at {path}; run scripts/setup_voice.py")
        from faster_whisper import WhisperModel

        started = time.monotonic()
        self._model = WhisperModel(str(path), device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
        log.info("voice: loaded faster-whisper %s (int8, CPU) in %.1fs", name, time.monotonic() - started)

    def transcribe(self, audio: np.ndarray) -> str:
        """Text for 16 kHz mono float32 audio, or "" for silence, noise, or a model error. Blocking."""
        if audio is None or len(audio) < MIN_SAMPLES:
            return ""
        try:
            self.load()
            with self._lock:
                segments, _info = self._model.transcribe(
                    audio.astype(np.float32, copy=False), language="en", beam_size=1, vad_filter=False,
                    condition_on_previous_text=False, without_timestamps=True,
                    initial_prompt=f"{self._hint}." if self._hint else None,
                )
                kept = [s.text.strip() for s in segments
                        if not (s.no_speech_prob > NO_SPEECH_PROB and s.avg_logprob < LOW_CONFIDENCE)]
        except Exception as exc:
            log.warning("voice: transcription failed: %s", exc)
            return ""
        return " ".join(t for t in kept if t).strip()
