"""
The listen meter: what the harness can say about the microphone it is being given.

A person saying "Hey Tala" to a running app and getting nothing back has no way to tell, from the log, whether audio is not
arriving, is arriving silent, is arriving quiet, or is arriving fine and simply not scoring as the wake phrase. The meter logs
those four facts every few seconds while listening, as numbers only: no audio and no words ever go into the log.
"""
from __future__ import annotations

import asyncio
import logging

import numpy as np

from python.harness import voice_meter
from python.harness.voice_wake import FRAME_SAMPLES, WakeDetector, WakeGate
from tests.test_voice_runtime import Rig, pcm, quiet, run, tone

LOGGER = "python.harness.voice_meter"


def chunk(level, seconds=0.1):
    return pcm(np.full(int(16000 * seconds), level, np.float32))


def test_nothing_is_logged_before_the_interval(caplog):
    meter = voice_meter.ListenMeter(interval_s=5.0)
    meter.reset(0.0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        meter.add(chunk(0.2), in_speech=False, wake_peak=0.0, wake_threshold=0.8, now=1.0)
    assert caplog.text == ""


def test_a_window_reports_seconds_loudest_level_speech_and_best_wake_score(caplog):
    meter = voice_meter.ListenMeter(interval_s=5.0)
    meter.reset(0.0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for i in range(50):
            meter.add(chunk(0.25 if i == 10 else 0.05), in_speech=i < 20, wake_peak=0.4 if i == 30 else 0.01,
                      wake_threshold=0.8, now=0.1 * (i + 1))
    line = caplog.text
    assert "heard 5.0 s" in line
    assert "loudest -12 dBFS" in line  # 0.25 of full scale
    assert "speech 2.0 s" in line
    assert "best wake score 0.40" in line and "fires at 0.80" in line


def test_the_window_resets_after_a_report(caplog):
    meter = voice_meter.ListenMeter(interval_s=1.0)
    meter.reset(0.0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for i in range(10):
            meter.add(chunk(0.5), in_speech=True, wake_peak=0.9, wake_threshold=0.8, now=0.1 * (i + 1))
        caplog.clear()
        for i in range(10):
            meter.add(chunk(0.01), in_speech=False, wake_peak=0.0, wake_threshold=0.8, now=1.1 + 0.1 * (i + 1))
    assert "speech 0.0 s" in caplog.text and "best wake score 0.00" in caplog.text
    assert "loudest -40 dBFS" in caplog.text


def test_digital_silence_is_called_out(caplog):
    meter = voice_meter.ListenMeter(interval_s=1.0)
    meter.reset(0.0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for i in range(10):
            meter.add(chunk(0.0), in_speech=False, wake_peak=0.0, wake_threshold=0.8, now=0.1 * (i + 1))
    assert "silence" in caplog.text and "microphone" in caplog.text


def test_a_very_quiet_microphone_is_called_out(caplog):
    meter = voice_meter.ListenMeter(interval_s=1.0)
    meter.reset(0.0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for i in range(10):
            meter.add(chunk(0.004), in_speech=False, wake_peak=0.0, wake_threshold=0.8, now=0.1 * (i + 1))
    assert "very quiet" in caplog.text


def test_the_wake_score_is_left_out_when_nothing_is_listening_for_it(caplog):
    meter = voice_meter.ListenMeter(interval_s=1.0)
    meter.reset(0.0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for i in range(10):
            meter.add(chunk(0.2), in_speech=False, wake_peak=None, wake_threshold=None, now=0.1 * (i + 1))
    assert "heard" in caplog.text and "wake" not in caplog.text


def test_no_log_line_carries_audio_or_words(caplog):
    meter = voice_meter.ListenMeter(interval_s=1.0)
    meter.reset(0.0)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for i in range(10):
            meter.add(chunk(0.2), in_speech=True, wake_peak=0.1, wake_threshold=0.8, now=0.1 * (i + 1))
    assert all(len(r.getMessage()) < 200 for r in caplog.records)


# --- the wake detector's peak -----------------------------------------------------------------------------------------


class ScriptedModel:
    def __init__(self, *scores):
        self.scores = list(scores)

    def predict(self, frame):
        return {"hey_tala": self.scores.pop(0) if self.scores else 0.0}


def test_the_detector_remembers_its_best_score_until_asked():
    detector = WakeDetector(ScriptedModel(0.1, 0.6, 0.2), WakeGate(threshold=0.8))
    detector.feed(np.zeros(3 * FRAME_SAMPLES, np.int16))
    assert detector.take_peak() == 0.6
    assert detector.take_peak() == 0.0  # taking it resets the window


# --- wired into the runtime ----------------------------------------------------------------------------------------------


def test_a_listening_runtime_logs_what_the_microphone_gives_it(caplog):
    async def go():
        r = Rig()
        await r.rt.start()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await r.feed(tone(6.0, 0.3))

    run(go())
    assert "heard" in caplog.text and "loudest" in caplog.text


def test_listening_with_no_audio_at_all_is_warned_about_once(caplog, monkeypatch):
    monkeypatch.setattr(voice_meter, "NO_AUDIO_AFTER_S", 0.05)

    async def go():
        r = Rig()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await r.rt.start()
            await asyncio.sleep(0.3)
            await r.rt.stop()

    run(go())
    assert caplog.text.count("no microphone audio") == 1
    assert "Privacy" in caplog.text  # says where Windows keeps the switch


def test_no_warning_when_audio_does_arrive(caplog, monkeypatch):
    monkeypatch.setattr(voice_meter, "NO_AUDIO_AFTER_S", 0.3)

    async def go():
        r = Rig()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await r.rt.start()
            await r.feed(quiet(0.5))
            await asyncio.sleep(0.5)
            await r.rt.stop()

    run(go())
    assert "no microphone audio" not in caplog.text
