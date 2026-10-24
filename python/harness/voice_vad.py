"""
python/harness/voice_vad.py -- where an utterance starts and ends in a stream of microphone audio.

``Segmenter`` takes 16 kHz mono audio in whatever chunk sizes the transport delivers, cuts it into the 512-sample frames
Silero VAD wants, and emits ``SegEvent``s:

* ``start``     speech has been sustained for ``start_ms`` (so a click is not an utterance);
* ``progress``  speech continues; ``speech_ms`` is how long it has lasted (the barge-in rule reads this);
* ``end``       ``min_silence_ms`` of silence followed it; ``audio`` is the whole utterance as float32, including
                ``preroll_ms`` from before the speech was recognised, so the first word is not clipped.

The speech probability is a plain callable, so the segmentation logic is tested with an energy detector on a fixture
WAV, and :class:`SileroVad` is the real one. Nothing is written to disk; the only audio held is the current utterance
(at most ``max_segment_s``) and the pre-roll.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
TRAILING_KEEP_FRAMES = 6  # about 190 ms of silence stays on the end of an utterance
FRAME = 512  # Silero VAD at 16 kHz takes exactly 512 samples (32 ms) per call


@dataclass
class SegEvent:
    kind: str
    speech_ms: float = 0.0
    audio: np.ndarray | None = None


class Segmenter:
    def __init__(self, prob_fn: Callable[[np.ndarray], float], *, threshold: float = 0.5, start_ms: float = 96.0,
                 min_silence_ms: float = 600.0, preroll_ms: float = 500.0, max_segment_s: float = 30.0,
                 on_reset: Callable[[], None] | None = None):
        self._prob = prob_fn
        self._on_reset = on_reset
        self.threshold = threshold
        self._start_frames = max(1, round(start_ms / (1000 * FRAME / SAMPLE_RATE)))
        self._silence_frames = max(1, round(min_silence_ms / (1000 * FRAME / SAMPLE_RATE)))
        self._preroll = deque(maxlen=max(1, round(preroll_ms / (1000 * FRAME / SAMPLE_RATE))))
        self._max_frames = round(max_segment_s * SAMPLE_RATE / FRAME)
        self._pending = np.zeros(0, np.float32)
        self.reset()

    # --- state ---------------------------------------------------------------------------------------------

    def reset(self) -> None:
        self._pending = np.zeros(0, np.float32)
        self._preroll.clear()
        self._segment: list[np.ndarray] = []
        self._candidates: list[np.ndarray] = []
        self.in_speech = False
        self._speech_frames = 0
        self._silent = 0
        if self._on_reset:
            self._on_reset()

    @property
    def trailing_silence_ms(self) -> float:
        """How long the current utterance has been quiet at its end (0 when no utterance is in progress)."""
        return self._silent * 1000 * FRAME / SAMPLE_RATE if self.in_speech else 0.0

    def current_audio(self) -> np.ndarray:
        """The utterance so far, so a caller that decides late (the wake word fired mid-sentence) can claim it."""
        return np.concatenate(self._segment) if self._segment else np.zeros(0, np.float32)

    # --- input ---------------------------------------------------------------------------------------------

    @staticmethod
    def _to_float(chunk) -> np.ndarray:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            return np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        arr = np.asarray(chunk)
        if arr.dtype == np.int16:
            return arr.astype(np.float32) / 32768.0
        return arr.astype(np.float32, copy=False)

    def feed(self, chunk) -> list[SegEvent]:
        self._pending = np.concatenate([self._pending, self._to_float(chunk)])
        events: list[SegEvent] = []
        while len(self._pending) >= FRAME:
            frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
            events += self._frame(frame)
        return events

    def _frame(self, frame: np.ndarray) -> list[SegEvent]:
        speech = self._prob(frame) >= self.threshold
        if not self.in_speech:
            if speech:
                self._candidates.append(frame)
                if len(self._candidates) >= self._start_frames:
                    self.in_speech = True
                    self._segment = [*self._preroll, *self._candidates]
                    self._preroll.clear()
                    self._speech_frames = len(self._candidates)
                    self._silent = 0
                    self._candidates = []
                    return [SegEvent("start", speech_ms=self._speech_frames * 1000 * FRAME / SAMPLE_RATE)]
            else:
                # a click that did not last: it is background, so it goes to the pre-roll like any other quiet audio
                for held in self._candidates:
                    self._preroll.append(held)
                self._candidates = []
                self._preroll.append(frame)
            return []

        self._segment.append(frame)
        events: list[SegEvent] = []
        if speech:
            self._speech_frames += 1
            self._silent = 0
            events.append(SegEvent("progress", speech_ms=self._speech_frames * 1000 * FRAME / SAMPLE_RATE))
        else:
            self._silent += 1
        if self._silent >= self._silence_frames or len(self._segment) >= self._max_frames:
            # keep a little trailing silence for the recogniser, not the whole end-of-speech wait
            audio = np.concatenate(self._segment[:len(self._segment) - max(0, self._silent - TRAILING_KEEP_FRAMES)])
            self._segment, self.in_speech, self._speech_frames, self._silent = [], False, 0, 0
            if self._on_reset:
                self._on_reset()
            events.append(SegEvent("end", audio=audio))
        return events


class SileroVad:
    """Silero VAD (ONNX, CPU) as a probability function. Loaded from the pip package: no download."""

    def __init__(self):
        import torch
        from silero_vad import load_silero_vad

        self._torch = torch
        self._model = load_silero_vad(onnx=True)

    def __call__(self, frame: np.ndarray) -> float:
        return float(self._model(self._torch.from_numpy(frame), SAMPLE_RATE).item())

    def reset(self) -> None:
        self._model.reset_states()
