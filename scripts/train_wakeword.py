"""
scripts/train_wakeword.py -- train a custom openWakeWord model for the wake phrase, entirely locally.

openWakeWord ships pretrained models for a handful of phrases ("hey jarvis", "alexa", "hey mycroft") and none for "Hey
Tala". This builds one the way openWakeWord's own pipeline does, in a lighter form that needs no multi-gigabyte download:

1. **Synthesise speech.** The phrase (and a few spellings of how it is pronounced) is rendered by Kokoro, the voice Tala
   already speaks with (Apache-2.0, US-origin): sixteen of its twenty US-English voices plus blends of pairs of them, at
   several speeds, then mixed with noise at several signal-to-noise ratios. Negatives are ordinary sentences and near misses
   ("hey Tyler", "hey Taylor", "hey there", "hey Alexa", ...), spoken the same way.
2. **Embed.** openWakeWord's own frozen feature models turn each clip into 96-number embeddings, one per 80 ms.
3. **Train** a small classifier over 16 embeddings (1.28 s) that should fire as the phrase ends, and export it as ONNX in
   the shape openWakeWord loads.
4. **Measure on voices it never trained on** (four Kokoro voices held out of training and of every blend, and the two
   Windows voices), through the real streaming detector: false rejects at several noise levels, and false accepts per hour of ordinary speech.

The model is installed only if it clears the bar (``BAR``) and only with ``--install``. **What this cannot measure is real
human voices and real rooms**: every number is on synthetic speech. ``<name>.metrics.json`` says so, and the real test is
saying the phrase to it. Everything stays on D: (models, work files); nothing is downloaded by this script (voices come from
``scripts/setup_voice.py --training-voices``).

    python scripts/train_wakeword.py --phrase "Hey Tala" --train-clips 1500 --install
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SR = 16000
HOP = 1280                  # one embedding per 80 ms
WINDOW = 16                 # the classifier looks at 16 embeddings (1.28 s)
EMBED_TAIL = 12640          # an embedding's audio window ends about this many samples after its frame start
CLIP_S = 3.4
#: What a trained model must achieve on held-out synthetic voices before it is installed over the pretrained fallback.
BAR = {"frr_clean": 0.10, "frr_snr10": 0.15, "frr_snr5": 0.40, "far_per_hour": 1.0, "min_negative_minutes": 20.0}

ADVERSARIAL = [
    "hey tyler", "hey taylor", "hey there", "hey alexa", "hey google", "hey tell her", "hey sarah", "hey talia",
    "hey tanya", "hey carla", "hey buddy", "hey dad", "hey stella", "hey tara", "hey tallulah", "hey tell us",
    "hey teller", "hey lola", "hey now", "hey man", "tala", "a la carte", "hey, let's go", "hey tonight",
]
#: Kokoro's US-English voices (scripts/setup_voice.py --training-voices puts them on D:). Four are held out for measuring.
KOKORO_VOICES = ("af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah",
                 "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa")
HELD_OUT_PER_GENDER = 2
TRAIN_BLENDS = 24  # extra training speakers: a random mix of two training voices each
KOKORO_RATE = 24000
KOKORO_SPEEDS = (0.8, 0.9, 1.0, 1.1, 1.25)
SAPI_TEST = ("Microsoft David Desktop", "Microsoft Zira Desktop")


# --- pure helpers (unit-tested) --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Speaker:
    kind: str          # "kokoro" | "sapi"
    name: str          # a Kokoro voice id, "first+second" for a blend, or a Windows voice name
    blend: float | None = None   # for a blend: the weight of the first voice

    @property
    def label(self) -> str:
        return f"{self.name}@{self.blend:.2f}" if self.blend is not None else self.name


def split_speakers(seed: int = 7) -> tuple[list[Speaker], list[Speaker]]:
    """Disjoint train and test speakers. Whole voices are held out, never clips: HELD_OUT_PER_GENDER of each gender stay out of
    training and out of every blend, and the Windows voices (a different engine entirely) are test speakers too."""
    rng = random.Random(seed)
    held = set(rng.sample([v for v in KOKORO_VOICES if v.startswith("af_")], HELD_OUT_PER_GENDER)
               + rng.sample([v for v in KOKORO_VOICES if v.startswith("am_")], HELD_OUT_PER_GENDER))
    trained = [v for v in KOKORO_VOICES if v not in held]
    blends: list[Speaker] = []
    while len(blends) < TRAIN_BLENDS:
        first, second = rng.sample(trained, 2)
        blends.append(Speaker("kokoro", f"{first}+{second}", blend=round(rng.uniform(0.25, 0.75), 2)))
    train = [Speaker("kokoro", v) for v in trained] + blends
    test = [Speaker("kokoro", v) for v in sorted(held)] + [Speaker("sapi", v) for v in SAPI_TEST]
    return train, test


def positive_frame_range(end_sample: int, tail_s: float = 0.4) -> tuple[int, int]:
    """Embedding frames at which the classifier window has just seen the phrase end (the frames it should fire on)."""
    first = max(WINDOW - 1, math.ceil((end_sample - EMBED_TAIL) / HOP))
    last = max(first, int((end_sample - EMBED_TAIL + tail_s * SR) / HOP))
    return first, last


def label_windows(n_frames: int, end_sample: int | None) -> list[tuple[int, int]]:
    """(frame, label) for every classifier window position in a clip: 1 where the phrase has just ended, else 0."""
    first, last = positive_frame_range(end_sample) if end_sample is not None else (n_frames, n_frames)
    return [(f, 1 if first <= f <= last else 0) for f in range(WINDOW - 1, n_frames)]


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-12))


def mix_at_snr(signal: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    """``signal`` plus ``noise`` scaled so the signal-to-noise ratio is ``snr_db``."""
    noise = np.resize(noise, len(signal)).astype(np.float32)
    gain = rms(signal) / (rms(noise) * (10 ** (snr_db / 20)))
    return (signal + noise * gain).astype(np.float32)


def meets_bar(metrics: dict) -> tuple[bool, list[str]]:
    """Whether measured results clear ``BAR``, and what fell short."""
    failures = []
    for key, limit in (("frr_clean", BAR["frr_clean"]), ("frr_snr10", BAR["frr_snr10"]), ("frr_snr5", BAR["frr_snr5"])):
        if metrics.get(key, 1.0) > limit:
            failures.append(f"{key} {metrics.get(key, 1.0):.2f} > {limit}")
    if metrics.get("negative_minutes", 0) < BAR["min_negative_minutes"]:
        failures.append(f"only {metrics.get('negative_minutes', 0):.1f} min of negative audio measured (< {BAR['min_negative_minutes']})")
    if metrics.get("far_per_hour", 99) > BAR["far_per_hour"]:
        failures.append(f"far_per_hour {metrics.get('far_per_hour', 99):.2f} > {BAR['far_per_hour']} (false accepts per hour)")
    return (not failures, failures)


def refuse_c_drive(*paths: Path) -> None:
    for path in paths:
        if str(Path(path).resolve()).upper().startswith("C:"):
            raise SystemExit(f"refusing to write to {path}: voice models and work files live on D: only")


def pronunciations(phrase: str) -> list[str]:
    """The phrase and a few spellings that make a synthetic voice say it differently (a name has no single spelling)."""
    base = phrase.lower().strip()
    variants = {base, base.replace(" ", ", ")}
    if base == "hey tala":
        variants |= {"hey tahla", "hey taala", "hey, tah la", "hay tala"}
    return sorted(variants)


# --- synthesis (needs the voices on D:) ---------------------------------------------------------------------------------------------------------

_ENGINE: object | None = None


def _resample(audio: np.ndarray, rate: int) -> np.ndarray:
    if rate == SR:
        return audio.astype(np.float32)
    from scipy.signal import resample_poly

    g = math.gcd(rate, SR)
    return resample_poly(audio, SR // g, rate // g).astype(np.float32)


def _kokoro_engine():
    """Tala's own Kokoro engine (python/harness/voice_kokoro.py), moved to the GPU when there is one: clips are made by the thousand.
    The voice files and the model come from D:; nothing is downloaded."""
    global _ENGINE
    if _ENGINE is None:
        import torch

        from python.harness import voice_kokoro

        engine = voice_kokoro._load()
        engine.device = "cuda" if torch.cuda.is_available() else "cpu"
        engine.model.to(engine.device)
        _ENGINE = engine
    return _ENGINE


def synth_kokoro(speaker: Speaker, text: str, speed: float) -> np.ndarray:
    """Speech for ``text`` in a Kokoro voice, or in a blend of two (``speaker.blend`` of the first, the rest of the second)."""
    import torch

    engine = _kokoro_engine()
    phonemes = engine._phonemize(text)[:510]
    index = len(phonemes) - 1
    if speaker.blend is None:
        ref = engine._voice(speaker.name)[index]
    else:
        first, second = speaker.name.split("+")
        ref = speaker.blend * engine._voice(first)[index] + (1.0 - speaker.blend) * engine._voice(second)[index]
    with torch.no_grad():
        audio = engine.model(phonemes, ref.to(getattr(engine, "device", "cpu")), speed)
    return _resample(np.asarray(audio.detach().cpu(), dtype=np.float32).reshape(-1), KOKORO_RATE)


def synth_sapi(work: Path, speaker: Speaker, text: str, rate_wpm: int) -> np.ndarray:
    import pyttsx3

    # A fresh engine per clip: reusing one deadlocks runAndWait on the second clip (found by a hung evaluation).
    engine = pyttsx3.init()
    for v in engine.getProperty("voices") or []:
        if speaker.name.lower() in (v.name or "").lower():
            engine.setProperty("voice", v.id)
    engine.setProperty("rate", rate_wpm)
    out = work / f"sapi-{os.getpid()}-{random.getrandbits(32):08x}.wav"
    engine.save_to_file(text, str(out))
    engine.runAndWait()
    try:
        with wave.open(str(out), "rb") as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
            rate = w.getframerate()
    finally:
        out.unlink(missing_ok=True)
    return _resample(pcm, rate)


def trim_silence(audio: np.ndarray, margin_s: float = 0.04, rel: float = 0.02) -> np.ndarray:
    """Cut the silence off both ends. Voices pad differently (the Windows ones leave ~0.8 s after the last word, Kokoro ~0.2 s),
    and "where the phrase ends" is what both the training labels and the scoring window are anchored to."""
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak <= 0:
        return audio
    loud = np.where(np.abs(audio) > rel * peak)[0]
    margin = int(margin_s * SR)
    return audio[max(0, loud[0] - margin): min(len(audio), loud[-1] + margin)]


def render(models: Path, work: Path, speaker: Speaker, text: str, rng: random.Random) -> np.ndarray:
    if speaker.kind == "sapi":
        audio = synth_sapi(work, speaker, text, rate_wpm=rng.choice((150, 175, 200)))
    else:
        audio = synth_kokoro(speaker, text, speed=rng.choice(KOKORO_SPEEDS))
    audio = trim_silence(audio)
    peak = float(np.max(np.abs(audio))) or 1.0
    return audio / peak * rng.uniform(0.35, 0.9)


def noise_clip(kind: str, n: int, rng: np.random.Generator) -> np.ndarray:
    white = rng.standard_normal(n).astype(np.float32)
    if kind == "white":
        return white
    spectrum = np.fft.rfft(white)
    freqs = np.maximum(np.fft.rfftfreq(n, 1 / SR), 20.0)
    if kind == "pink":
        return np.fft.irfft(spectrum / np.sqrt(freqs), n).astype(np.float32)
    if kind == "brown":
        return np.fft.irfft(spectrum / freqs, n).astype(np.float32)
    t = np.arange(n) / SR  # hum
    return sum(np.sin(2 * np.pi * 60 * k * t + rng.uniform(0, 6)) / k for k in range(1, 6)).astype(np.float32)


def place(phrase: np.ndarray, rng: random.Random) -> tuple[np.ndarray, int]:
    """A fixed-length clip with the phrase ending somewhere in the middle; returns the clip and the phrase's end sample."""
    total = int(CLIP_S * SR)
    end = rng.randint(int(1.9 * SR), int(2.7 * SR))
    start = max(0, end - len(phrase))
    clip = np.zeros(total, np.float32)
    piece = phrase[: end - start]
    clip[start:start + len(piece)] = piece
    return clip, start + len(piece)


def fit(clip: np.ndarray) -> np.ndarray:
    total = int(CLIP_S * SR)
    out = np.zeros(total, np.float32)
    out[: min(total, len(clip))] = clip[:total]
    return out


# --- pipeline -----------------------------------------------------------------------------------------------------------------------------------


def sentences(repo: Path, rng: random.Random, n: int) -> list[str]:
    import re

    pool: list[str] = []
    for md in sorted((repo / "docs").glob("*.md")) + [repo / "README.md", repo / "INSTALL.md"]:
        if md.is_file():
            text = re.sub(r"[`*#>|_\[\]()]", " ", md.read_text(encoding="utf-8", errors="ignore"))
            pool += [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 25 <= len(s.strip()) <= 110 and s.strip()[0].isalpha()]
    rng.shuffle(pool)
    return pool[:n]


def build_examples(speakers: list[Speaker], phrase: str, n_pos: int, n_neg: int, seed: int, models: Path, work: Path,
                   repo: Path, label: str) -> tuple[np.ndarray, np.ndarray]:
    """Clips (int16) and the phrase end sample for each (-1 for negatives)."""
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    variants = pronunciations(phrase)
    texts = sentences(repo, rng, n_neg) or ["The quick brown fox jumps over the lazy dog today."]
    clips, ends = [], []
    started = time.time()
    for i in range(n_pos):
        sp = rng.choice(speakers)
        clip, end = place(render(models, work, sp, rng.choice(variants), rng), rng)
        snr = rng.choice((30, 20, 15, 10, 5))
        clip = mix_at_snr(clip, noise_clip(rng.choice(("white", "pink", "brown", "hum")), len(clip), nrng), snr)
        clips.append(clip)
        ends.append(end)
        if i % 200 == 0:
            print(f"  [{label}] positives {i}/{n_pos}  ({time.time() - started:.0f}s)", flush=True)
    for i in range(n_neg):
        sp = rng.choice(speakers)
        text = rng.choice(ADVERSARIAL) if rng.random() < 0.35 else texts[i % len(texts)]
        audio = render(models, work, sp, text, rng)
        if len(audio) > CLIP_S * SR:
            start = rng.randint(0, len(audio) - int(CLIP_S * SR))
            audio = audio[start:start + int(CLIP_S * SR)]
        offset = rng.randint(0, max(0, int(CLIP_S * SR) - len(audio)))
        clip = np.zeros(int(CLIP_S * SR), np.float32)
        clip[offset:offset + len(audio)] = audio
        clips.append(mix_at_snr(clip, noise_clip(rng.choice(("white", "pink", "brown", "hum")), len(clip), nrng), rng.choice((30, 20, 10, 5))))
        ends.append(-1)
        if i % 200 == 0:
            print(f"  [{label}] negatives {i}/{n_neg}  ({time.time() - started:.0f}s)", flush=True)
    pcm = np.stack([(np.clip(c, -1, 1) * 32767).astype(np.int16) for c in clips])
    return pcm, np.array(ends)


def embed(pcm: np.ndarray, wake_dir: Path) -> np.ndarray:
    from openwakeword.utils import AudioFeatures

    features = AudioFeatures(melspec_model_path=str(wake_dir / "melspectrogram.onnx"),
                             embedding_model_path=str(wake_dir / "embedding_model.onnx"), inference_framework="onnx", ncpu=2)
    return features.embed_clips(pcm, batch_size=64, ncpu=2)


def windows_from(feats: np.ndarray, ends: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    for clip_feats, end in zip(feats, ends):
        for frame, label in label_windows(len(clip_feats), None if end < 0 else int(end)):
            xs.append(clip_feats[frame - WINDOW + 1:frame + 1])
            ys.append(label)
    return np.asarray(xs, np.float32), np.asarray(ys, np.float32)


def train_model(x: np.ndarray, y: np.ndarray, epochs: int, seed: int):
    import torch
    from torch import nn

    torch.manual_seed(seed)
    model = nn.Sequential(nn.Flatten(), nn.Linear(WINDOW * 96, 128), nn.LayerNorm(128), nn.ReLU(),
                          nn.Linear(128, 128), nn.LayerNorm(128), nn.ReLU(), nn.Linear(128, 1), nn.Sigmoid())
    perm = np.random.default_rng(seed).permutation(len(x))
    cut = int(len(x) * 0.9)
    tr, va = perm[:cut], perm[cut:]
    xt, yt = torch.from_numpy(x), torch.from_numpy(y).unsqueeze(1)
    pos = float(y[tr].sum()) or 1.0
    weight = torch.where(yt == 1, torch.tensor((len(tr) - pos) / pos) ** 0.5, torch.tensor(1.0))
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, epochs)
    best, best_state = 1e9, None
    for epoch in range(epochs):
        model.train()
        order = torch.from_numpy(np.random.default_rng(seed + epoch).permutation(tr))
        for i in range(0, len(order), 512):
            idx = order[i:i + 512]
            loss = nn.functional.binary_cross_entropy(model(xt[idx]), yt[idx], weight=weight[idx])
            opt.zero_grad()
            loss.backward()
            opt.step()
        sched.step()
        model.eval()
        with torch.no_grad():
            val = float(nn.functional.binary_cross_entropy(model(xt[va]), yt[va]))
        if val < best:
            best, best_state = val, {k: v.clone() for k, v in model.state_dict().items()}
    model.load_state_dict(best_state)
    print(f"  trained: best validation loss {best:.4f}", flush=True)
    return model


def export_onnx(model, path: Path) -> None:
    import torch

    model.eval()
    torch.onnx.export(model, torch.zeros(1, WINDOW, 96), str(path), input_names=["input"], output_names=["output"],
                      opset_version=17, dynamo=False)


def stream_scores(wake_dir: Path, model_path: Path, audio: np.ndarray) -> np.ndarray:
    """The model's score for every 80 ms frame of ``audio``, through the same streaming path the harness uses."""
    from python.harness.voice_wake import FRAME_SAMPLES, WakeDetector, WakeModel

    detector = WakeDetector.load(wake_dir, WakeModel(model_path, True, "x"), 0.5)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    scores = []
    for i in range(0, len(pcm) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        frame = pcm[i:i + FRAME_SAMPLES]
        out = detector._model.predict(frame)
        scores.append(max(out.values()) if out else 0.0)
    return np.asarray(scores, np.float32)


def fires_from(scores: np.ndarray, threshold: float = 0.5) -> list[float]:
    """Seconds at which the harness's gate (patience and cooldown included) fires on a score stream."""
    from python.harness.voice_wake import WakeGate

    gate = WakeGate(threshold=threshold)
    return [(i + 1) * HOP / SR for i, s in enumerate(scores) if gate.update(float(s), now=(i + 1) * HOP / SR)]


def mine_hard_negatives(model, speakers: list[Speaker], n_clips: int, seed: int, models: Path, work: Path, repo: Path,
                        wake_dir: Path, floor: float = 0.15) -> tuple[np.ndarray, np.ndarray]:
    """New negative speech the current model scores above ``floor``: the windows it is actually confused by."""
    import torch

    pcm, ends = build_examples(speakers, "", 0, n_clips, seed + 1000, models, work, repo, "mine")
    x, y = windows_from(embed(pcm, wake_dir), ends)
    with torch.no_grad():
        scores = model(torch.from_numpy(x)).squeeze(1).numpy()
    hard = scores > floor
    print(f"  mined {int(hard.sum())} confusing windows out of {len(x)}", flush=True)
    return x[hard], y[hard]


THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)


def choose_threshold(sweep: dict[float, dict]) -> tuple[float, dict]:
    """The lowest threshold whose measurements clear the bar; if none does, 0.5 and its (failing) measurements."""
    for threshold in sorted(sweep):
        if meets_bar(sweep[threshold])[0]:
            return threshold, sweep[threshold]
    default = 0.5 if 0.5 in sweep else min(sweep)
    return default, sweep[default]


def evaluate(model_path: Path, wake_dir: Path, test_speakers: list[Speaker], phrase: str, n_pos: int, neg_minutes: float,
             seed: int, models: Path, work: Path, repo: Path) -> dict:
    """Score held-out speech once, then measure every threshold from the stored scores and pick the lowest that clears the bar."""
    rng = random.Random(seed + 99)
    nrng = np.random.default_rng(seed + 99)
    variants = pronunciations(phrase)
    positives: list[dict] = []
    for i in range(n_pos):
        sp = rng.choice(test_speakers)
        variant = rng.choice(variants)
        clip, end = place(render(models, work, sp, variant, rng), rng)
        level = ("clean", "snr10", "snr5")[i % 3]
        if level != "clean":
            clip = mix_at_snr(clip, noise_clip(rng.choice(("white", "pink", "brown", "hum")), len(clip), nrng), 10 if level == "snr10" else 5)
        audio = np.concatenate([np.zeros(SR, np.float32), clip, np.zeros(SR, np.float32)])
        positives.append({"scores": stream_scores(wake_dir, model_path, audio), "end_s": end / SR + 1.0, "level": level,
                          "speaker": sp, "said": variant})
    texts = sentences(repo, random.Random(seed + 5), 4000)

    def stream_minutes(pool: list[str], minutes: float) -> tuple[list[np.ndarray], float]:
        streams, total, k = [], 0.0, 0
        while total < minutes:
            batch, bt = [], 0.0
            while len(batch) < 40 and total + bt < minutes:
                sp = rng.choice(test_speakers)
                audio = render(models, work, sp, pool[k % len(pool)], rng)
                k += 1
                gap = np.zeros(int(rng.uniform(0.3, 1.2) * SR), np.float32)
                batch += [audio, gap]
                bt += (len(audio) + len(gap)) / SR / 60
            stream = np.concatenate(batch)
            stream = mix_at_snr(stream, noise_clip("pink", len(stream), nrng), 20)
            streams.append(stream_scores(wake_dir, model_path, stream))
            total += bt
        return streams, total

    ordinary, ordinary_min = stream_minutes(texts, neg_minutes)
    near, near_min = stream_minutes(ADVERSARIAL, max(2.0, neg_minutes / 8))
    near_utterances = max(1, int(near_min * 60 / 2.5))

    def measure(threshold: float) -> dict:
        rejected = {"clean": [0, 0], "snr10": [0, 0], "snr5": [0, 0]}
        by_speaker: dict[str, list[int]] = {}
        misses: list[dict] = []
        for rec in positives:
            hit = any(rec["end_s"] - 0.3 <= t <= rec["end_s"] + 1.5 for t in fires_from(rec["scores"], threshold))
            rejected[rec["level"]][0] += 0 if hit else 1
            rejected[rec["level"]][1] += 1
            sp = rec["speaker"]
            tally = by_speaker.setdefault(f"{sp.kind}:{sp.name}", [0, 0])
            tally[0] += 0 if hit else 1
            tally[1] += 1
            if not hit and len(misses) < 20:
                lo, hi = int((rec["end_s"] - 0.3) * SR / HOP), int((rec["end_s"] + 1.5) * SR / HOP) + 1
                misses.append({"speaker": sp.label, "said": rec["said"], "level": rec["level"],
                               "peak_score": round(float(rec["scores"][lo:hi].max()), 3) if hi > lo else None})
        fires = sum(len(fires_from(s, threshold)) for s in ordinary)
        near_fires = sum(len(fires_from(s, threshold)) for s in near)
        rate = lambda lvl: rejected[lvl][0] / max(1, rejected[lvl][1])  # noqa: E731
        return {"frr_clean": rate("clean"), "frr_snr10": rate("snr10"), "frr_snr5": rate("snr5"),
                "frr_by_speaker": {k: round(v[0] / max(1, v[1]), 2) for k, v in sorted(by_speaker.items())},
                "sample_misses": misses, "negative_minutes": round(ordinary_min, 1), "false_accepts": fires,
                "far_per_hour": round(fires / (ordinary_min / 60), 3), "near_miss_fires": near_fires,
                "near_miss_accept_rate_approx": round(min(1.0, near_fires / near_utterances), 2)}

    sweep = {th: measure(th) for th in THRESHOLDS}
    threshold, chosen = choose_threshold(sweep)
    return {**chosen, "threshold": threshold, "positives_per_level": n_pos // 3, "near_miss_minutes": round(near_min, 1),
            "by_threshold": {str(th): {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()
                                       if k in ("frr_clean", "frr_snr10", "frr_snr5", "far_per_hour", "false_accepts", "near_miss_accept_rate_approx")}
                             for th, m in sweep.items()},
            "near_miss_note": "ordinary speech sets the bar; names that sound like the phrase (Talia, Tara, ...) are reported separately"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phrase", default="Hey Tala")
    ap.add_argument("--models-dir", default=None, help="parent of the voice folder (default: where the harness looks, TALA_MODELS_DIR)")
    ap.add_argument("--work-dir", default=None, help="default: <voice models>/wakeword_work")
    ap.add_argument("--train-clips", type=int, default=1500, help="positive clips (negatives are 2x)")
    ap.add_argument("--test-clips", type=int, default=240)
    ap.add_argument("--negative-minutes", type=float, default=25.0)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--mine", type=int, default=2000, help="extra negative clips to mine for windows the first model is confused by (0 = off)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--eval-only", action="store_true", help="measure the model already in the work folder; train nothing")
    ap.add_argument("--install", action="store_true", help="copy the model into the wakeword folder if it clears the bar")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    from python.harness import voice_settings

    models = Path(args.models_dir) / "voice" if args.models_dir else voice_settings.models_dir()
    wake_dir, work = models / "wakeword", Path(args.work_dir) if args.work_dir else models / "wakeword_work"
    refuse_c_drive(models, work)
    for var in ("TEMP", "TMP", "HF_HOME"):
        if os.environ.get(var, "").upper().startswith("C:"):
            raise SystemExit(f"refusing to run with {var}={os.environ[var]}: point it at D: first")
    work.mkdir(parents=True, exist_ok=True)
    slug = "_".join(args.phrase.lower().split())
    train, test = split_speakers(args.seed)
    print(f"training speakers: {len(train)}   held-out speakers: {len(test)}", flush=True)

    candidate = work / f"{slug}.onnx"
    if args.eval_only:
        if not candidate.is_file():
            raise SystemExit(f"--eval-only needs an existing {candidate}")
    else:
        pcm, ends = build_examples(train, args.phrase, args.train_clips, args.train_clips * 2, args.seed, models, work, repo, "train")
        print(f"embedding {len(pcm)} clips ...", flush=True)
        x, y = windows_from(embed(pcm, wake_dir), ends)
        print(f"{len(x)} windows, {int(y.sum())} positive", flush=True)
        model = train_model(x, y, args.epochs, args.seed)
        if args.mine:
            print("mining hard negatives ...", flush=True)
            hx, hy = mine_hard_negatives(model, train, args.mine, args.seed, models, work, repo, wake_dir)
            if len(hx):
                x = np.concatenate([x] + [hx] * 3)
                y = np.concatenate([y] + [hy] * 3)
                model = train_model(x, y, args.epochs, args.seed)
        candidate = work / f"{slug}.onnx"
        export_onnx(model, candidate)

    print("measuring on held-out speakers ...", flush=True)
    metrics = evaluate(candidate, wake_dir, test, args.phrase, args.test_clips, args.negative_minutes, args.seed, models, work, repo)
    passed, failures = meets_bar(metrics)
    report = {"phrase": args.phrase, "model": f"{slug}.onnx", "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "train_speakers": [s.label for s in train], "held_out_speakers": [s.label for s in test], **metrics,
              "bar": BAR, "passed_bar": passed, "shortfalls": failures,
              "caveat": "measured on synthetic speech only; real human voices and rooms are not represented"}
    (work / f"{slug}.metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("threshold", "by_threshold", "frr_clean", "frr_snr10", "frr_snr5", "frr_by_speaker", "negative_minutes", "false_accepts", "far_per_hour", "near_miss_accept_rate_approx", "passed_bar", "shortfalls", "sample_misses")}, indent=2))

    if args.install:
        if not passed:
            print("NOT installed: the model did not clear the bar; the pretrained fallback stays in use.")
            return 1
        (wake_dir / f"{slug}.onnx").write_bytes(candidate.read_bytes())
        (wake_dir / f"{slug}.metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"installed {wake_dir / (slug + '.onnx')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
