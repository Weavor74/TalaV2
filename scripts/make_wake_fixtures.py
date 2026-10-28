#!/usr/bin/env python
"""
scripts/make_wake_fixtures.py -- the four short WAVs tests/test_voice_wake_fixture.py runs the wake-word gate against.

Two that should wake her and two that should not, all in voices the wake-word trainer holds out of training:

    hey_tala_female.wav    "Hey Tala, what time is it?"      a held-out female Kokoro voice
    hey_tala_male.wav      "Hey Tala, what time is it?"      a held-out male Kokoro voice
    near_miss.wav          "Hey Tyler, what time is it?"     a name that sounds like hers
    speech.wav             an ordinary sentence              no wake phrase anywhere

16 kHz mono 16-bit, half a second of silence either side, a little noise under them. ``fixtures.json`` beside them records which voice
said what and where the phrase ends. Made with Kokoro from the models on D: (nothing is downloaded), and committed, because the test
has to run where Kokoro is not installed.

    python scripts/make_wake_fixtures.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "tests" / "fixtures" / "voice"
SR = 16000
LEAD_S = 0.6
TAIL_S = 0.8

CLIPS = [
    # (file, text, voice gender, speed, says the wake phrase, how many words of the text are the phrase)
    ("hey_tala_female.wav", "Hey Tala, what time is it?", "af", 1.0, True),
    ("hey_tala_male.wav", "Hey Tala, what time is it?", "am", 0.95, True),
    ("near_miss.wav", "Hey Tyler, what time is it?", "am", 1.0, False),
    ("speech.wav", "The cargo manifest is in order and the route to Stanton looks quiet tonight, so I would not worry.", "af", 1.0, False),
]


def _trainer():
    spec = importlib.util.spec_from_file_location("train_wakeword", REPO / "scripts" / "train_wakeword.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["train_wakeword"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def main() -> int:
    sys.path.insert(0, str(REPO))
    tw = _trainer()
    _, test_speakers = tw.split_speakers(7)
    held_out = [s for s in test_speakers if s.kind == "kokoro"]
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(11)
    index = {}
    for name, text, gender, speed, wakes in CLIPS:
        speaker = next(s for s in held_out if s.name.startswith(gender))
        speech = tw.trim_silence(tw.synth_kokoro(speaker, text, speed))
        speech = speech / (float(np.max(np.abs(speech))) or 1.0) * 0.6
        lead, tail = int(LEAD_S * SR), int(TAIL_S * SR)
        clip = np.concatenate([np.zeros(lead, np.float32), speech, np.zeros(tail, np.float32)])
        clip = clip + rng.normal(0, 0.002, len(clip)).astype(np.float32)  # a faint room under it
        _write(OUT / name, clip)
        phrase_end = None
        if wakes:  # the phrase is the first two words; measure where they end by synthesising them alone at the same speed
            first = tw.trim_silence(tw.synth_kokoro(speaker, "Hey Tala", speed))
            phrase_end = round((lead + len(first)) / SR, 2)
        index[name] = {"text": text, "voice": speaker.name, "speed": speed, "wakes": wakes, "phrase_end_s": phrase_end,
                       "seconds": round(len(clip) / SR, 2)}
        print(f"{name}: {index[name]}")
    (OUT / "fixtures.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
