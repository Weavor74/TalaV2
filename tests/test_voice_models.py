"""
The real local models, end to end: Piper speaks, faster-whisper hears it, Silero finds the utterance, openWakeWord loads.

Everything else in the voice suite uses stand-ins so it runs anywhere. These run only where the models are installed on D:
(scripts/setup_voice.py) and are skipped, with the reason, everywhere else -- including CI. They are what shows the wiring
survives contact with the actual models: sample rates, frame sizes, model file layouts.
"""
from __future__ import annotations

import io
import os
import wave
from pathlib import Path

import numpy as np
import pytest

from python.harness import voice_piper, voice_settings
from python.harness.voice_vad import SAMPLE_RATE, Segmenter


def _have(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


# Where the models are on this machine: TALA_MODELS_DIR, else the Kagura location if it exists, else wherever the harness looks.
_KAGURA = Path(r"D:\models")
_ROOT = os.environ.get("TALA_MODELS_DIR") or (str(_KAGURA) if _KAGURA.is_dir() else "")
MODELS = Path(_ROOT) / "voice" if _ROOT else voice_settings.models_dir()


@pytest.fixture(autouse=True)
def _models_where_they_are(monkeypatch):
    if _ROOT:
        monkeypatch.setenv("TALA_MODELS_DIR", _ROOT)
VOICE = voice_settings.DEFAULTS["piper_voice"]
HAVE_PIPER = _have("piper") and (MODELS / "piper" / f"{VOICE}.onnx").is_file() and (MODELS / "piper" / f"{VOICE}.onnx.json").is_file()
HAVE_WHISPER = _have("faster_whisper") and (MODELS / "whisper" / voice_settings.DEFAULTS["whisper_model"] / "model.bin").is_file()
HAVE_VAD = _have("silero_vad")
HAVE_WAKE = _have("openwakeword") and (MODELS / "wakeword" / "hey_jarvis_v0.1.onnx").is_file()

needs_piper = pytest.mark.skipif(not HAVE_PIPER, reason=f"Piper voice {VOICE} is not installed (scripts/setup_voice.py)")


def speech_16k(text: str) -> np.ndarray:
    wav, rate = voice_piper.synthesize_wav(text, VOICE)
    with wave.open(io.BytesIO(wav), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    n = int(len(pcm) * SAMPLE_RATE / rate)
    return np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype(np.float32)


@needs_piper
def test_piper_produces_a_playable_wav():
    wav, rate = voice_piper.synthesize_wav("The time is five thirty.", VOICE)
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == rate
        assert w.getnframes() / rate > 1.0


@needs_piper
@pytest.mark.skipif(not HAVE_WHISPER, reason="faster-whisper base.en is not installed")
def test_what_piper_says_faster_whisper_hears():
    from python.harness.voice_stt import SttEngine

    heard = SttEngine().transcribe(speech_16k("What time is it in the afternoon?")).lower()
    assert "time" in heard and "afternoon" in heard


@needs_piper
@pytest.mark.skipif(not HAVE_VAD, reason="silero-vad is not installed")
def test_silero_finds_one_utterance_in_real_speech_and_ignores_the_silence():
    from python.harness.voice_vad import SileroVad

    speech = speech_16k("Please tell me what the weather is like today.")
    audio = np.concatenate([np.zeros(SAMPLE_RATE), speech, np.zeros(2 * SAMPLE_RATE)])
    seg = Segmenter(SileroVad(), on_reset=None)
    events = []
    for i in range(0, len(audio), 1600):
        events += seg.feed(audio[i:i + 1600])
    kinds = [e.kind for e in events if e.kind != "progress"]
    assert kinds == ["start", "end"]
    end = [e for e in events if e.kind == "end"][0]
    assert 0.8 * len(speech) < len(end.audio) < 1.5 * len(speech) + SAMPLE_RATE


@pytest.mark.skipif(not HAVE_WAKE, reason="openWakeWord models are not installed (scripts/setup_voice.py)")
def test_the_wake_detector_loads_the_pretrained_model_and_stays_quiet_on_ordinary_speech():
    from python.harness.voice_wake import WakeDetector, choose_model

    wake_dir = MODELS / "wakeword"
    chosen = choose_model(wake_dir, {"wake_model": "", "wake_phrase": "Hey Nobody"})
    assert chosen is not None
    detector = WakeDetector.load(wake_dir, chosen, threshold=0.5)
    fired = False
    if HAVE_PIPER:
        audio = np.concatenate([np.zeros(SAMPLE_RATE), speech_16k("What is the capital of France? I would like some tea."),
                                np.zeros(SAMPLE_RATE)])
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        for i in range(0, len(pcm), 1600):
            fired = detector.feed(pcm[i:i + 1600]) or fired
    assert fired is False
