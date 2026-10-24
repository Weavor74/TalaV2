"""
python/harness/voice_wake.py -- the wake word ("Hey Tala"), detected locally by openWakeWord.

openWakeWord scores each 80 ms frame (1280 samples at 16 kHz). :class:`WakeGate` decides when a run of scores counts as
a wake-up: ``patience`` consecutive frames over the threshold, then ``cooldown_s`` of quiet, so a cough or a television
is not a wake-up and one utterance cannot fire twice. :func:`choose_model` picks the model; :class:`WakeDetector` buffers
arbitrary audio chunks into frames and feeds them through.

Model choice: an explicit ``wake_model`` path, else a model trained for the configured phrase (``hey_tala.onnx`` in the
wakeword folder; ``scripts/train_wakeword.py`` makes it), else the pretrained fallback (``hey_jarvis``). The fallback is
flagged ``trained_for_phrase=False`` so the UI can say the phrase it is actually listening for.

Nothing here touches the network. The runtime models are put on D: by ``scripts/setup_voice.py``.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

FRAME_SAMPLES = 1280  # 80 ms at 16 kHz, what openWakeWord scores per call
FALLBACK_MODEL = "hey_jarvis_v0.1.onnx"
FALLBACK_PHRASE = "Hey Jarvis"


class WakeGate:
    def __init__(self, threshold: float = 0.5, patience: int = 2, cooldown_s: float = 2.0):
        self.threshold = threshold
        self.patience = max(1, patience)
        self.cooldown_s = cooldown_s
        self._streak = 0
        self._quiet_until = float("-inf")

    def update(self, score: float, now: float) -> bool:
        if now < self._quiet_until:
            self._streak = 0
            return False
        if score >= self.threshold:
            self._streak += 1
            if self._streak >= self.patience:
                self._streak = 0
                self._quiet_until = now + self.cooldown_s
                return True
        else:
            self._streak = 0
        return False


@dataclass(frozen=True)
class WakeModel:
    path: Path
    trained_for_phrase: bool
    phrase: str


def model_threshold(model_path: Path) -> float | None:
    """The threshold a trained model was measured at (``<name>.metrics.json`` beside it), if it cleared its bar.

    A model that failed its bar, or has no readable metrics, brings no threshold of its own."""
    try:
        report = json.loads(model_path.with_name(model_path.stem + ".metrics.json").read_text(encoding="utf-8"))
        value = report.get("threshold")
        if report.get("passed_bar") is True and isinstance(value, (int, float)) and not isinstance(value, bool) \
                and 0.05 <= value <= 0.99:
            return float(value)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _slug(phrase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


def choose_model(wake_dir: Path, settings: dict[str, Any]) -> WakeModel | None:
    """The model to listen with, or None when there is nothing to listen with (reported, never faked)."""
    phrase = str(settings.get("wake_phrase") or "Hey Tala")
    explicit = str(settings.get("wake_model") or "").strip()
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return WakeModel(candidate, True, phrase)
        log.warning("voice: wake_model %s does not exist; falling back", explicit)
    trained = wake_dir / f"{_slug(phrase)}.onnx"
    if trained.is_file():
        return WakeModel(trained, True, phrase)
    fallback = wake_dir / FALLBACK_MODEL
    if fallback.is_file():
        return WakeModel(fallback, False, FALLBACK_PHRASE)
    return None


class WakeDetector:
    def __init__(self, model: Any, gate: WakeGate):
        self._model = model
        self._gate = gate
        self._pending = np.zeros(0, np.int16)
        self._clock = 0.0

    @classmethod
    def load(cls, wake_dir: Path, wake_model: WakeModel, threshold: float) -> "WakeDetector":
        from openwakeword.model import Model

        model = Model(
            wakeword_models=[str(wake_model.path)], inference_framework="onnx",
            melspec_model_path=str(wake_dir / "melspectrogram.onnx"),
            embedding_model_path=str(wake_dir / "embedding_model.onnx"),
        )
        return cls(model, WakeGate(threshold=threshold))

    @property
    def threshold(self) -> float:
        return self._gate.threshold

    def feed(self, chunk) -> bool:
        """Feed audio (int16 samples or PCM16 bytes). True if the wake word fired somewhere in this chunk."""
        data = np.frombuffer(chunk, dtype=np.int16) if isinstance(chunk, (bytes, bytearray, memoryview)) else np.asarray(chunk, np.int16)
        self._pending = np.concatenate([self._pending, data])
        fired = False
        while len(self._pending) >= FRAME_SAMPLES:
            frame, self._pending = self._pending[:FRAME_SAMPLES], self._pending[FRAME_SAMPLES:]
            self._clock += FRAME_SAMPLES / 16000
            try:
                scores = self._model.predict(frame)
            except Exception as exc:  # the audio loop outlives any one model error
                log.warning("voice: wake-word model error: %s", exc)
                continue
            if self._gate.update(max(scores.values()) if scores else 0.0, now=self._clock):
                fired = True
        return fired
