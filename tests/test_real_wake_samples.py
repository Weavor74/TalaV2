"""
scripts/real_wake_samples.py -- record a few minutes of the user's real voice and measure the wake-word models on it.

Every wake-word number so far is on synthetic speech, and two models trained on different synthetic voices each fail on the other's
kind. This script is how a real held-out set gets made. These tests pin what matters about it: the plan covers what the measurement
needs, nothing is written on C: or into the repository, a dead microphone is caught before twenty takes are wasted, a recording can be
resumed, and the measurement is arithmetic over scores that is checked here without a microphone or a model.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "real_wake_samples.py"
spec = importlib.util.spec_from_file_location("real_wake_samples", SCRIPT)
rws = importlib.util.module_from_spec(spec)
sys.modules["real_wake_samples"] = rws
spec.loader.exec_module(rws)


def tone(seconds, amp=0.3):
    t = np.arange(int(seconds * rws.SR)) / rws.SR
    return (amp * np.sin(2 * np.pi * 200 * t) * 32767).astype(np.int16)


# --- the plan ---------------------------------------------------------------------------------------------------------------------


def test_the_plan_has_phrases_questions_near_misses_ordinary_speech_and_a_quiet_room():
    plan = rws.build_plan()
    kinds = {}
    for take in plan:
        kinds[take.kind] = kinds.get(take.kind, 0) + 1

    assert kinds == {"hey_tala": 15, "hey_tala_question": 8, "near_miss": 10, "speech": 12, "room": 1}
    assert len({t.name for t in plan}) == len(plan), "every take has its own file"
    assert all(t.positive for t in plan if t.kind.startswith("hey_tala")) and not any(t.positive for t in plan if not t.kind.startswith("hey_tala"))


def test_the_positives_ask_for_different_ways_of_saying_it_not_the_same_take_fifteen_times():
    styles = {t.prompt.split("(")[-1] for t in rws.build_plan() if t.kind == "hey_tala"}

    assert len(styles) >= 4 and any("quiet" in s for s in styles) and any("away" in s for s in styles)


def test_the_near_misses_are_names_that_sound_like_hers_and_never_her_name():
    near = [t for t in rws.build_plan() if t.kind == "near_miss"]

    assert all("tala" not in t.prompt.lower() for t in near)
    assert any("Tyler" in t.prompt for t in near) and any("Talia" in t.prompt for t in near)


def test_the_ordinary_sentences_do_not_contain_the_phrase():
    assert not any("tala" in t.prompt.lower() for t in rws.build_plan() if t.kind == "speech")


def test_takes_are_long_enough_for_what_is_said_in_them():
    for t in rws.build_plan():
        assert t.seconds >= (3.0 if t.kind.startswith(("hey_tala", "near")) else 4.0)


# --- where it writes ----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["C:\\voice-samples", "c:/voice-samples"])
def test_a_folder_on_c_is_refused(bad):
    with pytest.raises(SystemExit, match="C:"):
        rws.refuse_c_drive(Path(bad))


def test_the_default_folder_is_under_the_models_directory_and_never_inside_the_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))

    folder = rws.default_samples_dir()

    assert (tmp_path / "models") in folder.parents
    assert rws.REPO not in folder.parents and folder != rws.REPO


def test_the_script_has_no_network_library_and_says_the_recordings_stay_here():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported.isdisjoint({"requests", "httpx", "urllib3", "aiohttp", "websockets", "socket", "http", "huggingface_hub"})
    assert "never leave this machine" in SCRIPT.read_text(encoding="utf-8")


# --- the microphone -----------------------------------------------------------------------------------------------------------------


def test_levels_are_told_apart():
    assert rws.check_level(np.zeros(16000, np.int16))[0] == "silent"
    assert rws.check_level(tone(1, 0.001))[0] == "quiet"
    assert rws.check_level(tone(1, 0.3))[0] == "ok"
    assert rws.check_level(np.full(16000, 32767, np.int16))[0] == "clipping"


def test_a_wav_round_trips(tmp_path):
    pcm = tone(0.5)
    rws.write_wav(tmp_path / "x.wav", pcm)

    assert np.array_equal(rws.read_wav(tmp_path / "x.wav"), pcm)


def test_a_take_is_recorded_from_the_chosen_device_at_the_models_sample_rate():
    asked = {}

    class FakeSd:
        def rec(self, frames, samplerate, channels, dtype, device):
            asked.update(frames=frames, samplerate=samplerate, channels=channels, dtype=dtype, device=device)
            return np.full((frames, 1), 1000, np.int16)

        def wait(self):
            asked["waited"] = True

    pcm = rws.record_take(FakeSd(), device=14, seconds=2.0)

    assert asked == {"frames": 32000, "samplerate": 16000, "channels": 1, "dtype": "int16", "device": 14, "waited": True}
    assert pcm.shape == (32000,) and pcm.dtype == np.int16


def test_a_dead_microphone_stops_the_session_at_the_first_take_not_after_forty(tmp_path):
    plan = rws.build_plan()
    said = []

    with pytest.raises(SystemExit, match="silence"):
        rws.run_record(plan, tmp_path, record=lambda take: np.zeros(int(take.seconds * rws.SR), np.int16), say=said.append, pause=lambda s: None)

    assert not list(tmp_path.glob("*.wav")), "nothing silent was kept"


def test_a_session_saves_every_take_and_a_manifest_and_can_be_resumed(tmp_path):
    plan = rws.build_plan()
    made = []

    def record(take):
        made.append(take.name)
        return tone(take.seconds)

    rws.run_record(plan[:5], tmp_path, record=record, say=lambda m: None, pause=lambda s: None)
    assert sorted(p.stem for p in tmp_path.glob("*.wav")) == sorted(t.name for t in plan[:5])

    made.clear()
    rws.run_record(plan[:8], tmp_path, record=record, say=lambda m: None, pause=lambda s: None)
    assert made == [t.name for t in plan[5:8]], "the five already recorded were not asked for again"
    manifest = rws.load_manifest(tmp_path)
    assert [m["name"] for m in manifest] == [t.name for t in plan[:8]] and manifest[0]["positive"] is True


def test_retake_replaces_what_is_there(tmp_path):
    plan = rws.build_plan()[:2]
    rws.run_record(plan, tmp_path, record=lambda t: tone(t.seconds, 0.1), say=lambda m: None, pause=lambda s: None)

    rws.run_record(plan, tmp_path, record=lambda t: tone(t.seconds, 0.4), say=lambda m: None, pause=lambda s: None, retake=True)

    assert rws.check_level(rws.read_wav(tmp_path / f"{plan[0].name}.wav"))[0] == "ok"


def test_a_take_that_clips_is_kept_but_called_out():
    said = []
    rws.run_record(rws.build_plan()[:1], Path(__file__).parent / "_unused", record=lambda t: np.full(int(t.seconds * rws.SR), 32767, np.int16),
                   say=said.append, pause=lambda s: None, dry_run=True)

    assert any("clipping" in m for m in said)


# --- the measurement -----------------------------------------------------------------------------------------------------------------


def _samples(tmp_path, positives=10, negatives=4, room_seconds=60):
    rows = []
    for i in range(positives):
        rows.append({"name": f"hey_tala-{i:02d}", "kind": "hey_tala", "prompt": "Hey Tala", "seconds": 3.5, "positive": True})
        rws.write_wav(tmp_path / f"hey_tala-{i:02d}.wav", tone(1.0))
    for i in range(negatives):
        rows.append({"name": f"near_miss-{i:02d}", "kind": "near_miss", "prompt": "Hey Tyler", "seconds": 3.5, "positive": False})
        rws.write_wav(tmp_path / f"near_miss-{i:02d}.wav", tone(1.0))
    rows.append({"name": "room-00", "kind": "room", "prompt": "silence", "seconds": float(room_seconds), "positive": False})
    rws.write_wav(tmp_path / "room-00.wav", np.zeros(int(room_seconds * rws.SR), np.int16))
    rws.write_manifest(tmp_path, rows)
    return rows


def scripted(peaks):
    """A stand-in for the model: each take's scores are a flat 0.05 with one frame at the take's scripted peak."""
    def scorer(wake_dir, model_path, audio):
        n = max(8, len(audio) // 1280)
        scores = np.full(n, 0.05, np.float32)
        key = peaks[model_path.name]
        scores[n // 2:n // 2 + 3] = key.pop(0) if key else 0.05
        return scores

    return scorer


def test_miss_rate_and_false_accepts_are_counted_per_threshold(tmp_path):
    _samples(tmp_path)
    peaks = {"good.onnx": [0.95] * 8 + [0.4] * 2 + [0.6, 0.05, 0.05, 0.05] + [0.05]}  # 10 positives, 4 near misses, the room

    report = rws.measure(tmp_path, {"good": Path("good.onnx")}, wake_dir=tmp_path, thresholds=(0.5, 0.7, 0.9), scorer=scripted(peaks))

    by = report["good"]["by_threshold"]
    assert by["0.5"]["miss_rate"] == pytest.approx(0.2) and by["0.5"]["false_accepts"] == 1
    assert by["0.7"]["false_accepts"] == 0 and by["0.9"]["miss_rate"] == pytest.approx(0.2)
    assert by["0.5"]["negative_minutes"] == pytest.approx((4 * 1.0 + 60) / 60, abs=0.05)
    assert by["0.5"]["far_per_hour"] == pytest.approx(1 / (by["0.5"]["negative_minutes"] / 60), rel=0.01)


def test_the_recommended_threshold_is_the_lowest_that_meets_the_bar_or_none(tmp_path):
    _samples(tmp_path)
    good = {"m.onnx": [0.95] * 10 + [0.6, 0.05, 0.05, 0.05] + [0.05]}

    report = rws.measure(tmp_path, {"m": Path("m.onnx")}, wake_dir=tmp_path, thresholds=(0.5, 0.7), scorer=scripted(good))
    assert report["m"]["recommended_threshold"] == 0.7, "0.5 lets the near miss through, 0.7 does not"

    bad = {"m.onnx": [0.3] * 10 + [0.05] * 5}
    report = rws.measure(tmp_path, {"m": Path("m.onnx")}, wake_dir=tmp_path, thresholds=(0.5, 0.7), scorer=scripted(bad))
    assert report["m"]["recommended_threshold"] is None and report["m"]["meets_bar_at_any_threshold"] is False


def test_models_are_compared_on_the_same_takes(tmp_path):
    _samples(tmp_path)
    peaks = {"a.onnx": [0.95] * 10 + [0.05] * 5, "b.onnx": [0.2] * 10 + [0.05] * 5}

    report = rws.measure(tmp_path, {"a": Path("a.onnx"), "b": Path("b.onnx")}, wake_dir=tmp_path, thresholds=(0.5,), scorer=scripted(peaks))

    assert report["a"]["by_threshold"]["0.5"]["miss_rate"] == 0.0 and report["b"]["by_threshold"]["0.5"]["miss_rate"] == 1.0
    assert "table" in rws.render(report) or "model" in rws.render(report)


def test_measuring_nothing_or_a_missing_model_is_a_clear_refusal(tmp_path):
    with pytest.raises(SystemExit, match="no recordings"):
        rws.measure(tmp_path, {"a": Path("a.onnx")}, wake_dir=tmp_path, scorer=scripted({}))
    _samples(tmp_path)
    with pytest.raises(SystemExit, match="not found"):
        rws.measure(tmp_path, {"a": tmp_path / "missing.onnx"}, wake_dir=tmp_path)


def test_the_report_names_the_misses_by_how_they_were_said(tmp_path):
    rows = _samples(tmp_path)
    for row in rows:
        if row["kind"] == "hey_tala":
            row["prompt"] = "Hey Tala (quietly)" if int(row["name"][-2:]) % 2 else "Hey Tala (normally)"
    rws.write_manifest(tmp_path, rows)
    peaks = {"m.onnx": [0.95, 0.2] * 5 + [0.05] * 5}

    report = rws.measure(tmp_path, {"m": Path("m.onnx")}, wake_dir=tmp_path, thresholds=(0.5,), scorer=scripted(peaks))

    styles = report["m"]["by_threshold"]["0.5"]["miss_by_style"]
    assert styles["quietly"] == 1.0 and styles["normally"] == 0.0
