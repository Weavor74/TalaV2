"""
Voice latency: three numbers per spoken turn, written to a log on D:, and shown in the status bar.

The numbers (all milliseconds, from the runtime's own clock):

* ``wake_to_first_word``  the wake word is detected -> the first sound of her answer starts playing
* ``stt_latency``         you stop speaking -> the final transcript exists
* ``time_to_first_audio`` you stop speaking -> the first piece of her answer has been synthesised

plus the parts they are made of. The log holds numbers and configuration names only: never a transcript, never audio.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import numpy as np
import pytest

from python.harness import voice_latency as vl

TIMELINE = {"wake": 10.0, "speech_end": 12.0, "stt_start": 12.05, "stt_done": 12.85, "turn_begin": 12.9, "first_text": 15.6,
            "first_sentence_tts": 0.4, "first_audio": 16.2, "playback_start": 16.3}


@pytest.fixture(autouse=True)
def _world(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    monkeypatch.delenv("TALA_VOICE_LOG_DIR", raising=False)


# --- what the numbers mean --------------------------------------------------------------------------------------------------------


def test_the_three_numbers_have_the_definitions_the_docs_give():
    m = vl.measure(TIMELINE)

    assert m["wake_to_first_word"] == 6300, "wake detected (10.0) -> first sound playing (16.3)"
    assert m["stt_latency"] == 850, "end of speech (12.0) -> final transcript (12.85)"
    assert m["time_to_first_audio"] == 4200, "end of speech (12.0) -> first audio synthesised (16.2)"


def test_the_parts_they_are_made_of_are_reported_too():
    m = vl.measure(TIMELINE)

    assert m["stt_compute"] == 800 and m["model_first_text"] == 2700 and m["first_sentence_tts"] == 400
    assert m["first_audio_to_playback"] == 100


def test_a_follow_up_with_no_wake_word_has_no_wake_number():
    follow_up = {k: v for k, v in TIMELINE.items() if k != "wake"}

    m = vl.measure(follow_up)

    assert "wake_to_first_word" not in m and m["stt_latency"] == 850


def test_nothing_measured_is_an_empty_report_not_zeros():
    assert vl.measure({}) == {}
    assert vl.measure({"wake": 5.0}) == {}


def test_a_clock_that_ran_backwards_does_not_produce_a_negative_latency():
    m = vl.measure({"speech_end": 20.0, "stt_done": 19.0, "first_audio": 30.0})

    assert "stt_latency" not in m and m["time_to_first_audio"] == 10000


def test_the_medians_are_taken_per_number_over_the_turns_that_have_it():
    entries = [{"ms": {"stt_latency": 800, "wake_to_first_word": 6000}}, {"ms": {"stt_latency": 900}},
               {"ms": {"stt_latency": 700, "wake_to_first_word": 7000}}, {"ms": {"stt_latency": 1000, "wake_to_first_word": 5000}},
               {"ms": {"stt_latency": 850, "wake_to_first_word": 6500}}]

    med = vl.medians(entries)

    assert med["stt_latency"] == 850 and med["wake_to_first_word"] == 6250 and med["turns"] == 5


# --- the log ----------------------------------------------------------------------------------------------------------------------


def test_each_turn_is_one_json_line_in_the_log_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_VOICE_LOG_DIR", str(tmp_path / "logs"))

    path = vl.append({"turn_id": "abc123", "ms": vl.measure(TIMELINE), "whisper_model": "base.en", "wake_model": "hey_tala.onnx"})
    vl.append({"turn_id": "def456", "ms": {"stt_latency": 500}})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert path == tmp_path / "logs" / "voice-latency.jsonl" and len(lines) == 2
    first = json.loads(lines[0])
    assert first["turn_id"] == "abc123" and first["ms"]["wake_to_first_word"] == 6300 and first["at"]


def test_the_log_holds_numbers_and_names_only_never_words(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_VOICE_LOG_DIR", str(tmp_path / "logs"))

    path = vl.append({"turn_id": "abc", "ms": vl.measure(TIMELINE), "text": "what time is it", "transcript": "secret", "audio": b"xx"})

    entry = json.loads(path.read_text(encoding="utf-8"))
    assert set(entry) <= {"at", "turn_id", "ms", "whisper_model", "wake_model", "tts_voice"}
    assert "what time" not in path.read_text(encoding="utf-8") and "secret" not in path.read_text(encoding="utf-8")
    assert all(isinstance(v, int) for v in entry["ms"].values())


def test_without_a_setting_the_log_goes_under_the_user_data_root(tmp_path):
    path = vl.append({"turn_id": "t", "ms": {"stt_latency": 1}})

    assert path == tmp_path / "data" / "logs" / "voice" / "voice-latency.jsonl" and path.is_file()


@pytest.mark.parametrize("bad", ["C:\\voice-logs", "c:/voice-logs", "\\\\?\\C:\\voice-logs"])
def test_a_log_directory_on_c_is_refused_and_nothing_is_written(bad, monkeypatch):
    monkeypatch.setenv("TALA_VOICE_LOG_DIR", bad)

    assert vl.append({"turn_id": "t", "ms": {"stt_latency": 1}}) is None
    assert vl.log_dir() is None


def test_a_log_that_cannot_be_written_never_breaks_the_voice_loop(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("TALA_VOICE_LOG_DIR", str(blocker / "inside"))

    assert vl.append({"turn_id": "t", "ms": {"stt_latency": 1}}) is None


def test_a_big_log_is_rotated_not_grown_forever(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_VOICE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(vl, "MAX_BYTES", 200)
    for n in range(10):
        vl.append({"turn_id": f"turn-{n}", "ms": {"stt_latency": n}})

    logs = tmp_path / "logs"
    assert (logs / "voice-latency.jsonl.1").is_file()
    assert sum(len(f.read_text(encoding="utf-8").splitlines()) for f in logs.glob("voice-latency.jsonl*")) <= 10
    assert (logs / "voice-latency.jsonl").stat().st_size < 400


def test_reading_back_returns_the_most_recent_turns_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_VOICE_LOG_DIR", str(tmp_path / "logs"))
    for n in range(6):
        vl.append({"turn_id": f"t{n}", "ms": {"stt_latency": n}})

    recent = vl.read_recent(3)

    assert [e["turn_id"] for e in recent] == ["t3", "t4", "t5"]
    assert vl.read_recent(3) == recent and vl.read_recent(0) == []


# --- wired into the runtime --------------------------------------------------------------------------------------------------------


def test_a_spoken_turn_writes_its_latency_and_tells_the_ui(tmp_path, monkeypatch):
    from tests.test_voice_runtime import Rig, quiet, tone

    monkeypatch.setenv("TALA_VOICE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr("python.harness.voice_runtime.PARTIAL_EVERY_S", 1e9)  # partial transcripts would spend the fake recogniser's one line

    async def go():
        r = Rig("Hey Tala, what time is it")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        turn = r.last("voice.turn")["turn_id"]
        r.rt.stream_for(turn).feed_text("It is noon. ")
        await asyncio.sleep(0.05)
        r.clock.t += 0.2
        await r.rt.playback("started", turn)
        return r, turn

    r, turn = asyncio.run(go())

    sent = r.last("voice.latency")
    assert sent["turn_id"] == turn and {"wake_to_first_word", "stt_latency", "time_to_first_audio"} <= set(sent["latency_ms"])
    logged = [json.loads(line) for line in (tmp_path / "logs" / "voice-latency.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(logged) == 1 and logged[0]["turn_id"] == turn and logged[0]["ms"] == sent["latency_ms"]
    assert "what time is it" not in (tmp_path / "logs" / "voice-latency.jsonl").read_text(encoding="utf-8").lower()
    assert r.rt.status()["last_latency_ms"] == sent["latency_ms"]


def test_playback_that_starts_for_a_turn_with_no_marks_logs_nothing(tmp_path, monkeypatch):
    from tests.test_voice_runtime import Rig

    monkeypatch.setenv("TALA_VOICE_LOG_DIR", str(tmp_path / "logs"))

    async def go():
        r = Rig()
        await r.rt.start()
        await r.rt.playback("started", None)
        return r

    r = asyncio.run(go())

    assert "voice.latency" not in r.names() and not (tmp_path / "logs").exists()


def test_the_report_script_summarises_the_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TALA_VOICE_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(vl, "_on_c_drive", lambda text: False)
    for ms in (100, 300, 200):
        vl.append({"turn_id": "t", "ms": {"stt_latency": ms}})
    import importlib.util
    spec = importlib.util.spec_from_file_location("voice_latency_report", Path(__file__).parent.parent / "scripts" / "voice_latency_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--last", "3"]) == 0
    out = capsys.readouterr().out
    assert "last 3 turn" in out and "200" in out
