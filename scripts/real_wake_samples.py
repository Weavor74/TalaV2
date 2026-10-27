#!/usr/bin/env python
"""
scripts/real_wake_samples.py -- record a few minutes of your real voice, then measure the wake-word models on it.

Every wake-word number so far is on synthetic speech, and two models trained on different synthetic voices each fail on the other's
kind. The only thing that settles whether "Hey Tala" works for you is you saying it. This makes that held-out set:

    python scripts\\real_wake_samples.py devices                 # which microphones there are, and which is the default
    python scripts\\real_wake_samples.py record                  # about 6 minutes, guided; run it again to resume
    python scripts\\real_wake_samples.py measure                 # the installed model (and the retrained candidate) on those takes

``record`` shows what to say, counts you in, beeps, records, and checks the level, so a dead or muted microphone (Windows' default
input is sometimes a virtual device that delivers digital silence) stops the session on the first take. It asks for the phrase said
several different ways, questions that start with it, names that sound like it ("Hey Tyler"), ordinary sentences without it, and
thirty seconds of the room as it normally is. ``measure`` reports, for each model and each threshold, how many of the takes with the
phrase it missed (and by how you said it), and how often it woke on the rest: near misses, ordinary speech and the room.

These recordings are your voice. They are saved only under D:\\models\\voice\\wakeword_real_samples (never on C:, never in the
repository), they never leave this machine, and nothing here uses a network. Delete the folder when you are done with it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import ntpath
import os
import re
import sys
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

REPO = Path(__file__).resolve().parents[1]
SR = 16000
HOP = 1280  # one openWakeWord score per 80 ms
THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)
#: What a model must achieve on your real voice before it is worth using: of your takes of the phrase, at most 10% missed; on everything
#: else, at most one false wake per hour and at most 15% of near misses accepted.
REAL_BAR = {"miss_rate": 0.10, "far_per_hour": 1.0, "near_miss_accept_rate": 0.15}

STYLES = ("normally", "quietly", "from a few steps away", "quickly", "as if tired", "with the TV or music on", "leaning back in the chair")
QUESTIONS = ("Hey Tala, what time is it?", "Hey Tala, what's the weather like?", "Hey Tala, can you help me with something?",
             "Hey Tala, play something quiet.")
NEAR_MISSES = ("Hey Tyler", "Hey Taylor", "Hey there", "Hey Tara", "Hey Talia", "Hey, tell her", "Hey Sarah", "Hey Alexa", "Hey Dad", "Hey Stella")
SENTENCES = (
    "I think I will make some coffee and then see what the day looks like.",
    "The cargo manifest is in order and the route looks quiet tonight.",
    "Can you pass me that cable, the long one on the left?",
    "I was reading about how bridges are built across wide rivers.",
    "We should probably leave a little earlier than we planned.",
    "The meeting moved to three o'clock, so I have some time before it.",
    "Did you see the game last night? The ending was unbelievable.",
    "I need to remember to water the plants and take out the trash.",
    "It is cold in here, maybe I should close the window.",
    "Let me check whether the update finished downloading.",
    "My sister is visiting next week and she wants to see the harbor.",
    "That is a good question, I will have to think about it for a moment.",
)


@dataclass(frozen=True)
class Take:
    kind: str       # hey_tala | hey_tala_question | near_miss | speech | room
    prompt: str     # what to say, with how to say it in brackets
    seconds: float
    name: str       # the file stem

    @property
    def positive(self) -> bool:
        return self.kind.startswith("hey_tala")


def build_plan(phrase_takes: int = 15, question_takes: int = 8, near_miss_takes: int = 10, speech_takes: int = 12,
               room_seconds: float = 30.0) -> list[Take]:
    """The takes to record, in the order they are asked for. Each positive asks for a different way of saying it."""
    plan: list[Take] = []
    for i in range(phrase_takes):
        plan.append(Take("hey_tala", f"Hey Tala   ({STYLES[i % len(STYLES)]})", 3.5, f"hey_tala-{i:02d}"))
    for i in range(question_takes):
        plan.append(Take("hey_tala_question", f"{QUESTIONS[i % len(QUESTIONS)]}   ({STYLES[(i * 3) % len(STYLES)]})", 4.5, f"hey_tala_question-{i:02d}"))
    for i in range(near_miss_takes):
        plan.append(Take("near_miss", f"{NEAR_MISSES[i % len(NEAR_MISSES)]}, what time is it?", 4.0, f"near_miss-{i:02d}"))
    for i in range(speech_takes):
        plan.append(Take("speech", SENTENCES[i % len(SENTENCES)], 6.0, f"speech-{i:02d}"))
    plan.append(Take("room", "Say nothing. Let the room be as it normally is: type, shift in your chair, breathe.", float(room_seconds), "room-00"))
    return plan


# --- where it writes -------------------------------------------------------------------------------------------------------------------


def _drive(text: str) -> str:
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return ntpath.splitdrive(ntpath.normpath(text))[0].upper() if text else ""


def refuse_c_drive(path: Path) -> None:
    if "C:" in (_drive(str(path)), _drive(os.path.abspath(str(path)))):
        raise SystemExit(f"refusing to write to {path}: recordings of your voice go on D: only, never C:")


def default_samples_dir() -> Path:
    sys.path.insert(0, str(REPO))
    from python.harness import voice_settings

    return voice_settings.models_dir() / "wakeword_real_samples"


# --- the microphone ----------------------------------------------------------------------------------------------------------------------


def check_level(pcm: np.ndarray) -> tuple[str, float]:
    """``(label, peak in dBFS)``: silent (nothing arrives), quiet, ok or clipping."""
    peak = float(np.max(np.abs(pcm.astype(np.int32)))) / 32768.0 if len(pcm) else 0.0
    db = 20 * np.log10(max(peak, 1e-6))
    if peak <= 2.0 / 32768.0:
        return "silent", db
    if peak < 0.005:
        return "quiet", db
    if peak >= 0.999:
        return "clipping", db
    return "ok", db


def write_wav(path: Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(pcm.astype("<i2").tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.int16)


def record_take(sd: Any, device: Any, seconds: float) -> np.ndarray:
    """One take from ``device`` at the models' sample rate, as mono 16-bit samples."""
    data = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="int16", device=device)
    sd.wait()
    return np.asarray(data).reshape(-1).astype(np.int16)


def write_manifest(folder: Path, rows: list[dict[str, Any]]) -> None:
    (folder / "manifest.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


def load_manifest(folder: Path) -> list[dict[str, Any]]:
    try:
        rows = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def run_record(plan: list[Take], folder: Path, record: Callable[[Take], np.ndarray], say: Callable[[str], Any],
               pause: Callable[[float], Any], retake: bool = False, dry_run: bool = False, cue: Callable[[], Any] | None = None) -> None:
    """Walk through ``plan``: say what to say, count in, record, check the level, save. Takes already on disk are skipped (that is how a
    session is resumed) unless ``retake``. A silent take stops everything and is not saved; a quiet or clipping one is saved and called out."""
    if not dry_run:
        refuse_c_drive(folder)
        folder.mkdir(parents=True, exist_ok=True)
    for index, take in enumerate(plan, 1):
        target = folder / f"{take.name}.wav"
        if target.exists() and not retake:
            continue
        say(f"\n[{index}/{len(plan)}] Say: {take.prompt}")
        for count in (3, 2, 1):
            say(f"   {count}...")
            pause(0.7)
        if cue:
            cue()
        say("   >>> RECORDING <<<" if take.kind != "room" else f"   >>> RECORDING {take.seconds:.0f} s of the room <<<")
        pcm = record(take)
        label, db = check_level(pcm)
        if label == "silent":
            raise SystemExit("that take is digital silence: the microphone is muted, blocked, or the wrong device. Nothing was saved for it. "
                             "Run the 'devices' command, pick the real microphone with --device, and check Windows Settings > Privacy > Microphone.")
        say(f"   level {db:.0f} dBFS: {label}" + (" (move back or lower the input level, then --retake this one)" if label == "clipping" else
                                                   " (speak up or move closer; --retake to redo it)" if label == "quiet" else ""))
        if not dry_run:
            write_wav(target, pcm)
        pause(0.5)
    if not dry_run:
        have = {p.stem for p in folder.glob("*.wav")}
        write_manifest(folder, [asdict(t) | {"positive": t.positive} for t in plan if t.name in have])


# --- measuring ---------------------------------------------------------------------------------------------------------------------------


def _fires(scores: np.ndarray, threshold: float) -> list[float]:
    sys.path.insert(0, str(REPO))
    from python.harness.voice_wake import WakeGate

    gate = WakeGate(threshold=threshold)
    return [(i + 1) * HOP / SR for i, s in enumerate(scores) if gate.update(float(s), now=(i + 1) * HOP / SR)]


def _real_scorer(wake_dir: Path, model_path: Path, audio: np.ndarray) -> np.ndarray:
    spec = importlib.util.spec_from_file_location("train_wakeword_for_scoring", REPO / "scripts" / "train_wakeword.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["train_wakeword_for_scoring"] = module
    sys.path.insert(0, str(REPO))
    spec.loader.exec_module(module)
    return module.stream_scores(wake_dir, model_path, audio)


def _style(prompt: str) -> str:
    found = re.search(r"\(([^)]*)\)", prompt)
    return found.group(1) if found else "unspecified"


def measure(samples: Path, models: dict[str, Path], wake_dir: Path, thresholds: tuple[float, ...] = THRESHOLDS,
            scorer: Callable[[Path, Path, np.ndarray], np.ndarray] | None = None) -> dict[str, Any]:
    """Each model, on every recorded take, at every threshold: the share of takes with the phrase it missed (and by how it was said),
    and its false wakes on everything else, per hour. ``recommended_threshold`` is the lowest that meets REAL_BAR, else None."""
    rows = load_manifest(samples)
    if not rows:
        raise SystemExit(f"no recordings in {samples}: run the 'record' command first")
    if scorer is None:
        for name, path in models.items():
            if not Path(path).is_file():
                raise SystemExit(f"model '{name}' not found: {path}")
        scorer = _real_scorer
    takes = []
    for row in rows:
        wav = samples / f"{row['name']}.wav"
        if wav.is_file():
            takes.append((row, read_wav(wav).astype(np.float32) / 32768.0))
    if not takes:
        raise SystemExit(f"no recordings in {samples}: the manifest lists takes whose files are gone")
    negative_minutes = sum(len(audio) / SR for row, audio in takes if not row["positive"]) / 60.0
    report: dict[str, Any] = {}
    for name, path in models.items():
        scores = [(row, scorer(wake_dir, Path(path), audio)) for row, audio in takes]
        by_threshold: dict[str, Any] = {}
        for th in thresholds:
            positives = [(row, _fires(s, th)) for row, s in scores if row["positive"]]
            negatives = [(row, _fires(s, th)) for row, s in scores if not row["positive"]]
            missed = [row for row, fires in positives if not fires]
            styles: dict[str, list[int]] = {}
            for row, fires in positives:
                styles.setdefault(_style(row["prompt"]), []).append(0 if fires else 1)
            false_accepts = sum(len(f) for _, f in negatives)
            near = [(row, f) for row, f in negatives if row["kind"] == "near_miss"]
            by_threshold[str(th)] = {
                "miss_rate": round(len(missed) / max(1, len(positives)), 3), "positives": len(positives),
                "miss_by_style": {k: round(sum(v) / len(v), 2) for k, v in sorted(styles.items())},
                "false_accepts": false_accepts, "negative_minutes": round(negative_minutes, 2),
                "far_per_hour": round(false_accepts / (negative_minutes / 60.0), 2) if negative_minutes > 0 else None,
                "near_miss_accept_rate": round(sum(1 for _, f in near if f) / max(1, len(near)), 3),
                "missed_takes": [r["name"] for r in missed][:12],
            }
        good = [float(th) for th, v in by_threshold.items()
                if v["miss_rate"] <= REAL_BAR["miss_rate"] and (v["far_per_hour"] or 0) <= REAL_BAR["far_per_hour"]
                and v["near_miss_accept_rate"] <= REAL_BAR["near_miss_accept_rate"]]
        report[name] = {"by_threshold": by_threshold, "recommended_threshold": min(good) if good else None,
                        "meets_bar_at_any_threshold": bool(good), "bar": REAL_BAR}
    return report


def render(report: dict[str, Any]) -> str:
    lines = [f"{'model':<24}{'thr':>5}{'missed':>9}{'false wakes/h':>15}{'near-miss accepted':>20}   misses by how it was said"]
    for name, data in report.items():
        for th, v in data["by_threshold"].items():
            worst = ", ".join(f"{k}: {int(x * 100)}%" for k, x in v["miss_by_style"].items() if x > 0) or "none"
            lines.append(f"{name:<24}{th:>5}{v['miss_rate'] * 100:>8.0f}%{str(v['far_per_hour']):>15}{v['near_miss_accept_rate'] * 100:>19.0f}%   {worst}")
        rec = data["recommended_threshold"]
        lines.append(f"  -> {name}: " + (f"meets the bar at threshold {rec}" if rec is not None else "does not meet the bar at any threshold")
                     + f" (miss <= {int(REAL_BAR['miss_rate'] * 100)}%, <= {REAL_BAR['far_per_hour']:g} false wake/hour, "
                     f"<= {int(REAL_BAR['near_miss_accept_rate'] * 100)}% of near misses)")
    return "\n".join(lines)


# --- the command line -------------------------------------------------------------------------------------------------------------------------


def _devices(sd: Any) -> list[tuple[int, str, int]]:
    return [(i, d["name"], d["max_input_channels"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def _pick_device(sd: Any, wanted: str | None) -> Any:
    if wanted is None:
        return None  # the system default input
    if wanted.isdigit():
        return int(wanted)
    matches = [i for i, name, _ in _devices(sd) if wanted.lower() in name.lower()]
    if not matches:
        raise SystemExit(f"no input device matches {wanted!r}; run the 'devices' command")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("devices", help="list the input devices and the default")
    rec = sub.add_parser("record", help="record the guided takes (resumes where it stopped)")
    rec.add_argument("--device", default=None, help="input device number or part of its name (default: the system default input)")
    rec.add_argument("--out", default=None, help="folder for the takes (default: <models>/voice/wakeword_real_samples)")
    rec.add_argument("--retake", action="store_true", help="record every take again, replacing what is there")
    rec.add_argument("--quick", action="store_true", help="a shorter session: 8 phrases, 4 questions, 5 near misses, 6 sentences, 15 s of room")
    mea = sub.add_parser("measure", help="score the models on the recorded takes")
    mea.add_argument("--samples", default=None)
    mea.add_argument("--model", action="append", default=[], help="NAME=PATH to a .onnx (default: the installed hey_tala and the retrained candidate)")
    mea.add_argument("--json", default=None, help="also write the full report here")
    args = ap.parse_args(argv)

    if args.command == "devices":
        import sounddevice as sd

        default = sd.default.device[0]
        for index, name, channels in _devices(sd):
            print(f"{'*' if index == default else ' '} {index:>3}  {name}  ({channels} ch)")
        print("\n* is the default input. Virtual devices (Steam Streaming Microphone, Virtual Desktop Audio) often deliver silence.")
        return 0

    sys.path.insert(0, str(REPO))
    from python.harness import voice_settings

    if args.command == "record":
        import sounddevice as sd

        folder = Path(args.out) if args.out else default_samples_dir()
        refuse_c_drive(folder)
        device = _pick_device(sd, args.device)
        plan = build_plan(8, 4, 5, 6, 15.0) if args.quick else build_plan()
        print("These recordings are your voice. They are saved only in", folder, "(never on C:, never in the repository),")
        print("they never leave this machine, and nothing here uses a network. Delete the folder when you are done.")
        print(f"\n{len(plan)} takes, about {sum(t.seconds + 3.2 for t in plan) / 60:.0f} minutes. Use your normal voice and your normal distance, "
              "unless a prompt says otherwise.")
        print("You can stop with Ctrl+C at any time and run the command again to carry on. Press Enter to begin.")
        input()
        try:
            import winsound

            def cue():
                winsound.Beep(880, 140)
        except ImportError:
            cue = None
        run_record(plan, folder, record=lambda take: record_take(sd, device, take.seconds), say=lambda m: print(m, flush=True),
                   pause=time.sleep, retake=args.retake, cue=cue)
        print(f"\nDone. {len(list(folder.glob('*.wav')))} takes in {folder}.\nNext:  python scripts\\real_wake_samples.py measure")
        return 0

    models_root = voice_settings.models_dir()
    wake_dir = models_root / "wakeword"
    models = dict(m.split("=", 1) for m in args.model if "=" in m)
    if not models:
        models = {"installed hey_tala": str(wake_dir / "hey_tala.onnx")}
        candidate = models_root / "wakeword_work" / "hey_tala.onnx"
        if candidate.is_file():
            models["retrained candidate"] = str(candidate)
    samples = Path(args.samples) if args.samples else default_samples_dir()
    report = measure(samples, {k: Path(v) for k, v in models.items()}, wake_dir)
    print(render(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
