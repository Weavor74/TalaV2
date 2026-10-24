"""
Wake-word gating: when a stream of model scores counts as "someone said it", and which model is used.

openWakeWord scores every 80 ms frame. A single frame over the threshold is how a cough or a TV becomes a wake-up, so
the gate asks for ``patience`` consecutive frames over it, and then stays quiet for ``cooldown_s`` so one utterance
cannot fire twice. Model choice is a rule too: an explicit path wins, then a model trained for the configured phrase,
then the pretrained fallback, and when none exists the detector says so instead of pretending to listen.
"""
from __future__ import annotations

import numpy as np
import pytest

from python.harness import voice_wake
from python.harness.voice_wake import FRAME_SAMPLES, WakeDetector, WakeGate, choose_model


def fire_times(gate, scores, step=0.08):
    return [round(i * step, 2) for i, s in enumerate(scores) if gate.update(s, now=i * step)]


def test_one_frame_over_the_threshold_is_not_a_wake_word():
    gate = WakeGate(threshold=0.5, patience=2)
    assert fire_times(gate, [0.1, 0.9, 0.1, 0.1]) == []


def test_consecutive_frames_over_the_threshold_fire_once():
    gate = WakeGate(threshold=0.5, patience=2)
    assert fire_times(gate, [0.1, 0.8, 0.9, 0.95, 0.9, 0.2]) == [0.16]


def test_the_cooldown_stops_one_utterance_firing_twice():
    gate = WakeGate(threshold=0.5, patience=2, cooldown_s=2.0)
    times = fire_times(gate, [0.9] * 6 + [0.0] * 5 + [0.9] * 4, step=0.08)
    assert len(times) == 1


def test_it_can_fire_again_after_the_cooldown():
    gate = WakeGate(threshold=0.5, patience=2, cooldown_s=2.0)
    first = [gate.update(0.9, now=t) for t in (0.0, 0.08)]
    second = [gate.update(0.9, now=t) for t in (3.0, 3.08)]
    assert first == [False, True] and second == [False, True]


def test_a_dip_resets_the_streak():
    gate = WakeGate(threshold=0.5, patience=3)
    assert fire_times(gate, [0.9, 0.9, 0.1, 0.9, 0.9, 0.1]) == []


def test_the_threshold_is_inclusive_and_configurable():
    assert fire_times(WakeGate(threshold=0.7, patience=1), [0.69, 0.7]) == [0.08]


# --- which model ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def wake_dir(tmp_path):
    d = tmp_path / "wakeword"
    d.mkdir()
    for name in ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx", "alexa_v0.1.onnx"):
        (d / name).write_bytes(b"x")
    return d


def test_an_explicit_model_path_wins(wake_dir, tmp_path):
    mine = tmp_path / "mine.onnx"
    mine.write_bytes(b"x")
    assert choose_model(wake_dir, {"wake_model": str(mine), "wake_phrase": "Hey Tala"}).path == mine


def test_a_model_trained_for_the_phrase_beats_the_fallback(wake_dir):
    (wake_dir / "hey_tala.onnx").write_bytes(b"x")
    chosen = choose_model(wake_dir, {"wake_model": "", "wake_phrase": "Hey Tala"})
    assert chosen.path.name == "hey_tala.onnx" and chosen.trained_for_phrase is True


def test_the_pretrained_fallback_is_used_and_flagged_as_not_the_phrase(wake_dir):
    chosen = choose_model(wake_dir, {"wake_model": "", "wake_phrase": "Hey Tala"})
    assert chosen.path.name == "hey_jarvis_v0.1.onnx" and chosen.trained_for_phrase is False


def test_no_model_at_all_is_reported_not_faked(tmp_path):
    empty = tmp_path / "none"
    empty.mkdir()
    assert choose_model(empty, {"wake_model": "", "wake_phrase": "Hey Tala"}) is None


def test_a_missing_explicit_path_falls_through_rather_than_crashing(wake_dir):
    chosen = choose_model(wake_dir, {"wake_model": "D:/nope/missing.onnx", "wake_phrase": "Hey Tala"})
    assert chosen is not None and chosen.path.name == "hey_jarvis_v0.1.onnx"


# --- the detector, with a stand-in model ------------------------------------------------------------------------------------------


class FakeModel:
    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = 0

    def predict(self, frame):
        assert len(frame) == FRAME_SAMPLES and frame.dtype == np.int16
        self.calls += 1
        return {"hey_tala": self.scores.pop(0) if self.scores else 0.0}


def test_the_detector_buffers_arbitrary_chunks_into_model_frames():
    model = FakeModel([0.0] * 10)
    detector = WakeDetector(model, WakeGate(threshold=0.5, patience=1))
    detector.feed(np.zeros(FRAME_SAMPLES // 2, np.int16))
    assert model.calls == 0
    detector.feed(np.zeros(FRAME_SAMPLES * 2, np.int16))
    assert model.calls == 2


def test_the_detector_reports_a_fire():
    detector = WakeDetector(FakeModel([0.1, 0.9, 0.9]), WakeGate(threshold=0.5, patience=2))
    assert detector.feed(np.zeros(FRAME_SAMPLES * 3, np.int16)) is True


def test_the_detector_accepts_pcm_bytes():
    detector = WakeDetector(FakeModel([0.9, 0.9]), WakeGate(threshold=0.5, patience=2))
    assert detector.feed(np.zeros(FRAME_SAMPLES * 2, np.int16).tobytes()) is True


def test_a_model_error_does_not_take_the_audio_loop_down():
    class Broken:
        def predict(self, frame):
            raise RuntimeError("onnx exploded")

    detector = WakeDetector(Broken(), WakeGate())
    assert detector.feed(np.zeros(FRAME_SAMPLES, np.int16)) is False


def test_the_module_never_downloads_anything():
    import inspect

    source = inspect.getsource(voice_wake)
    assert "download_models" not in source and "http" not in source.replace("https://github.com/dscripka", "")


# --- the model's own threshold ------------------------------------------------------------------------------------------------------------


def test_a_trained_model_brings_the_threshold_it_was_measured_at(tmp_path):
    from python.harness.voice_wake import model_threshold

    model = tmp_path / "hey_tala.onnx"
    model.write_bytes(b"x")
    (tmp_path / "hey_tala.metrics.json").write_text('{"threshold": 0.8, "passed_bar": true}')
    assert model_threshold(model) == 0.8


@pytest.mark.parametrize("content", ['{"threshold": 0.8, "passed_bar": false}', "not json", '{"threshold": "high"}', '{"threshold": 7}', "{}"])
def test_a_missing_or_untrustworthy_metrics_file_gives_no_threshold(tmp_path, content):
    from python.harness.voice_wake import model_threshold

    model = tmp_path / "hey_tala.onnx"
    model.write_bytes(b"x")
    (tmp_path / "hey_tala.metrics.json").write_text(content)
    assert model_threshold(model) is None


def test_a_model_without_metrics_has_no_threshold_of_its_own(tmp_path):
    from python.harness.voice_wake import model_threshold

    model = tmp_path / "hey_jarvis_v0.1.onnx"
    model.write_bytes(b"x")
    assert model_threshold(model) is None
