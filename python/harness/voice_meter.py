"""
python/harness/voice_meter.py -- what the harness can say about the microphone it is being given.

Someone who says "Hey Tala" to a running app and gets nothing back cannot tell, from the log, whether audio is not arriving,
is arriving as silence, is arriving too quiet, or is arriving fine and not scoring as the wake phrase. Every few seconds while
listening this logs which of those it is, as numbers only: seconds heard, loudest level, seconds the voice detector called
speech, and the best wake score against the score it needs. No audio and no words ever reach the log.
"""
from __future__ import annotations

import logging
import math

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
#: A window is reported after this many seconds of the runtime's clock.
REPORT_EVERY_S = 5.0
#: Listening with not one chunk of audio this long after it started is worth a warning.
NO_AUDIO_AFTER_S = 6.0
#: Below this (about -46 dBFS) the microphone is too quiet for the voice detector to work with.
VERY_QUIET = 0.005
SILENT = 1.0 / 32768  # a peak of one sample step or less


class ListenMeter:
    def __init__(self, interval_s: float = REPORT_EVERY_S):
        self.interval_s = interval_s
        self.total_chunks = 0
        self._window_start = 0.0
        self._clear()

    def _clear(self) -> None:
        self._seconds = 0.0
        self._speech = 0.0
        self._peak = 0.0
        self._wake_peak: float | None = None

    def reset(self, now: float) -> None:
        """Start counting afresh, as when listening is switched on."""
        self.total_chunks = 0
        self._window_start = now
        self._clear()

    def add(self, pcm: bytes, in_speech: bool, wake_peak: float | None, wake_threshold: float | None, now: float) -> None:
        samples = np.frombuffer(pcm, dtype=np.int16)
        if not len(samples):
            return
        seconds = len(samples) / SAMPLE_RATE
        self.total_chunks += 1
        self._seconds += seconds
        if in_speech:
            self._speech += seconds
        self._peak = max(self._peak, float(np.max(np.abs(samples.astype(np.int32)))) / 32768.0)
        if wake_peak is not None:
            self._wake_peak = max(self._wake_peak or 0.0, wake_peak)
        if now - self._window_start >= self.interval_s:
            self._report(wake_threshold)
            self._window_start = now
            self._clear()

    def _report(self, wake_threshold: float | None) -> None:
        level = 20 * math.log10(max(self._peak, 1e-5))
        parts = [f"heard {self._seconds:.1f} s of microphone audio", f"loudest {level:.0f} dBFS", f"speech {self._speech:.1f} s"]
        if self._wake_peak is not None:
            fires = f" (fires at {wake_threshold:.2f})" if wake_threshold is not None else ""
            parts.append(f"best wake score {self._wake_peak:.2f}{fires}")
        note = ""
        if self._peak <= SILENT:
            note = " -- that is silence: the microphone is muted, the wrong device, or blocked"
        elif self._peak < VERY_QUIET:
            note = " -- very quiet: raise the microphone level in Windows sound settings"
        log.info("voice: %s%s", ", ".join(parts), note)


def warn_no_audio() -> None:
    log.warning("voice: listening, but no microphone audio has arrived; check Windows Settings > Privacy > Microphone "
                "(allow desktop apps), and that the right input device is selected")
