"""
The wake-word gate on real audio: four short WAVs (tests/fixtures/voice, made by scripts/make_wake_fixtures.py in voices the wake-word
trainer holds out), two that say "Hey Tala" and two that do not ("Hey Tyler", and an ordinary sentence).

Two layers, because the model itself lives on D: and not in the repository:

* **always**: the detector's framing and the gate's patience and cooldown, run over those WAVs with a stand-in model that scores the
  frames where the labelled phrase ends. This is the plumbing: chunk sizes that do not line up with 80 ms frames, a quiet room, two
  utterances close together.
* **on a machine that has the trained model** (Kagura): the real ``hey_tala.onnx`` and openWakeWord on the same WAVs. Positives must
  fire as the phrase ends, negatives must not fire at all. Skipped, saying why, anywhere else.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from python.harness.voice_wake import FRAME_SAMPLES, WakeDetector, WakeGate

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "voice"
SR = 16000
INDEX = json.loads((FIXTURES / "fixtures.json").read_text(encoding="utf-8"))
POSITIVES = [name for name, meta in INDEX.items() if meta["wakes"]]
NEGATIVES = [name for name, meta in INDEX.items() if not meta["wakes"]]


def load(name: str) -> np.ndarray:
    with wave.open(str(FIXTURES / name), "rb") as handle:
        assert handle.getframerate() == SR and handle.getnchannels() == 1 and handle.getsampwidth() == 2
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)


class Spotter:
    """A model stand-in that scores high for a quarter of a second after the labelled end of the phrase, and low everywhere else."""

    def __init__(self, phrase_end_s: float | None):
        self.end = phrase_end_s
        self.frames = 0

    def predict(self, frame):
        self.frames += 1
        t = self.frames * FRAME_SAMPLES / SR
        return {"hey_tala": 0.9 if self.end is not None and self.end <= t <= self.end + 0.25 else 0.02}


def fire_times(detector, pcm: np.ndarray, chunk: int) -> list[float]:
    """Feed ``pcm`` in chunks of ``chunk`` samples and return the stream time (seconds) at the end of each chunk that fired."""
    fired = []
    for start in range(0, len(pcm), chunk):
        piece = pcm[start:start + chunk]
        if detector.feed(piece):
            fired.append((start + len(piece)) / SR)
    return fired


# --- the fixtures themselves ---------------------------------------------------------------------------------------------------------------


def test_there_are_two_positive_and_two_negative_fixtures_and_they_are_what_they_say():
    assert len(POSITIVES) == 2 and len(NEGATIVES) == 2
    for name, meta in INDEX.items():
        pcm = load(name)
        assert abs(len(pcm) / SR - meta["seconds"]) < 0.05 and np.abs(pcm).max() > 3000, f"{name} has speech in it"
        assert (meta["phrase_end_s"] is not None) == meta["wakes"]
        if meta["wakes"]:
            assert 0.5 < meta["phrase_end_s"] < meta["seconds"] - 0.5


def test_the_fixtures_use_voices_the_trainer_never_trained_on():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("tw_fixture_check", Path(__file__).resolve().parents[1] / "scripts" / "train_wakeword.py")
    tw = importlib.util.module_from_spec(spec)
    sys.modules["tw_fixture_check"] = tw
    spec.loader.exec_module(tw)
    train, test = tw.split_speakers(7)
    trained = {part for s in train for part in s.name.split("+")}
    held_out = {s.name for s in test}
    for name, meta in INDEX.items():
        assert meta["voice"] in held_out and meta["voice"] not in trained, name


# --- the gate and the framing, with a stand-in model ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [1600, 777, 4096, FRAME_SAMPLES])
@pytest.mark.parametrize("name", POSITIVES)
def test_a_positive_fires_once_as_the_phrase_ends_whatever_the_chunk_size(name, chunk):
    detector = WakeDetector(Spotter(INDEX[name]["phrase_end_s"]), WakeGate(threshold=0.5, patience=2))

    fired = fire_times(detector, load(name), chunk)

    end = INDEX[name]["phrase_end_s"]
    assert len(fired) == 1 and end <= fired[0] <= end + 0.6, f"fired at {fired}, phrase ends at {end}"


@pytest.mark.parametrize("chunk", [1600, 777])
@pytest.mark.parametrize("name", NEGATIVES)
def test_a_negative_never_fires(name, chunk):
    detector = WakeDetector(Spotter(None), WakeGate(threshold=0.5, patience=2))

    assert fire_times(detector, load(name), chunk) == []
    assert detector.take_peak() < 0.5


def test_two_utterances_close_together_wake_her_once_and_far_apart_twice():
    name = POSITIVES[0]
    clip = load(name)
    end = INDEX[name]["phrase_end_s"]

    class Twice(Spotter):
        def __init__(self, second_after_s):
            super().__init__(end)
            self.second = end + second_after_s

        def predict(self, frame):
            result = super().predict(frame)
            t = self.frames * FRAME_SAMPLES / SR
            if self.second <= t <= self.second + 0.25:
                return {"hey_tala": 0.9}
            return result

    # the second phrase ends 1 s after the first (inside the 2 s cooldown: one fire) or 3.5 s after it (outside: two)
    for second_after, expected in ((1.0, 1), (3.5, 2)):
        stream = np.concatenate([clip, np.zeros(int((second_after + 1.5) * SR), np.int16)])
        detector = WakeDetector(Twice(second_after), WakeGate(threshold=0.5, patience=2, cooldown_s=2.0))
        assert len(fire_times(detector, stream, 1600)) == expected, f"second phrase {second_after}s after the first"


def test_a_single_loud_frame_in_a_quiet_room_is_not_a_wake_word():
    class Blip(Spotter):
        def predict(self, frame):
            self.frames += 1
            return {"hey_tala": 0.95 if self.frames == 10 else 0.02}

    detector = WakeDetector(Blip(None), WakeGate(threshold=0.5, patience=2))

    assert fire_times(detector, load(NEGATIVES[1]), 1600) == []


# --- the real model, where there is one ----------------------------------------------------------------------------------------------------


def _real_detector():
    pytest.importorskip("openwakeword")
    from python.harness import voice_settings
    from python.harness.voice_wake import choose_model, model_threshold

    wake_dir = voice_settings.models_dir() / "wakeword"
    chosen = choose_model(wake_dir, {"wake_phrase": "Hey Tala", "wake_model": ""})
    if chosen is None or not chosen.trained_for_phrase:
        pytest.skip(f"no trained hey_tala.onnx in {wake_dir}: the real-model check runs where the model is installed")
    if not (wake_dir / "embedding_model.onnx").is_file():
        pytest.skip("the openWakeWord runtime models are not installed")
    return WakeDetector.load(wake_dir, chosen, model_threshold(chosen.path) or 0.5)


@pytest.mark.parametrize("name", POSITIVES)
def test_the_real_model_wakes_on_hey_tala(name):
    detector = _real_detector()
    # openWakeWord wants a little audio before its first score: the fixtures start with 0.6 s of quiet, as a room does
    fired = fire_times(detector, load(name), 1600)

    end = INDEX[name]["phrase_end_s"]
    assert fired and end - 0.3 <= fired[0] <= end + 1.5, f"{name}: fired at {fired}, the phrase ends at {end}"


@pytest.mark.parametrize("name", NEGATIVES)
def test_the_real_model_stays_quiet_for_a_near_miss_and_for_ordinary_speech(name):
    detector = _real_detector()

    assert fire_times(detector, load(name), 1600) == [], f"{name} woke her"
