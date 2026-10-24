"""
scripts/setup_voice.py -- fetch the local voice models. The only place voice touches the network, and only when run.

Tala's voice is fully local: Piper (speech), faster-whisper (hearing), Silero VAD (end of utterance, ships inside its
pip package) and openWakeWord (the wake phrase). At runtime every one of them loads from disk and the harness sets
HF_HUB_OFFLINE=1; nothing is fetched. This script is what puts the files there, once, and it refuses any target that is
not on D:.

    python scripts/setup_voice.py              # piper voice, whisper base.en, wake-word runtime models
    python scripts/setup_voice.py --whisper small.en --voice en_US-ryan-medium
    python scripts/setup_voice.py --check      # report what is present, download nothing
    python scripts/setup_voice.py --training-voices   # plus the voices the wake-word trainer uses

Layout under ``TALA_MODELS_DIR`` (default ``<user data>/models``; set it to D:\\models on Kagura)::

    voice/piper/<voice>.onnx + <voice>.onnx.json
    voice/whisper/<model>/            (CTranslate2 weights)
    voice/wakeword/                   (openWakeWord melspectrogram/embedding + pretrained phrases)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_VOICE = "en_US-lessac-medium"
DEFAULT_WHISPER = "base.en"
#: More US-English voices, used only to synthesise training clips for a custom wake-word model (scripts/train_wakeword.py).
TRAINING_VOICES = ("en_US-amy-medium", "en_US-ryan-medium", "en_US-hfc_female-medium", "en_US-joe-medium",
                   "en_US-kusal-medium", "en_US-libritts_r-medium")
PIPER_REPO = "rhasspy/piper-voices"
WHISPER_REPO = "Systran/faster-whisper-{model}"


REPO = Path(__file__).resolve().parents[1]


def models_root() -> Path:
    """The one definition of where voice models live, shared with the harness."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from python.harness import voice_settings

    return voice_settings.models_dir()


def refuse_c_drive(path: Path) -> None:
    if str(path.resolve()).upper().startswith("C:"):
        raise SystemExit(f"refusing to write to {path}: voice models must not go on C:. Set TALA_MODELS_DIR to a folder on another drive")


def piper_voice_path(voice: str) -> str:
    """The voice's path inside the piper-voices repo: en_US-lessac-medium -> en/en_US/lessac/medium/<voice>."""
    lang, name, quality = voice.split("-")
    return f"{lang.split('_')[0]}/{lang}/{name}/{quality}/{voice}"


def check(root: Path, voice: str, whisper: str) -> dict[str, bool]:
    return {
        f"piper voice {voice}": (root / "piper" / f"{voice}.onnx").is_file() and (root / "piper" / f"{voice}.onnx.json").is_file(),
        f"whisper {whisper}": (root / "whisper" / whisper / "model.bin").is_file(),
        "wake-word runtime models": any((root / "wakeword").glob("embedding_model*")) and any((root / "wakeword").glob("melspectrogram*")),
    }


def fetch_piper(root: Path, voice: str) -> None:
    from huggingface_hub import hf_hub_download

    target = root / "piper"
    target.mkdir(parents=True, exist_ok=True)
    base = piper_voice_path(voice)
    for suffix in (".onnx", ".onnx.json"):
        hf_hub_download(PIPER_REPO, base + suffix, local_dir=str(target / "_dl"))
        src = target / "_dl" / (base + suffix)
        src.replace(target / f"{voice}{suffix}")
    print(f"piper voice -> {target / (voice + '.onnx')}")


def fetch_whisper(root: Path, model: str) -> None:
    from huggingface_hub import snapshot_download

    target = root / "whisper" / model
    snapshot_download(WHISPER_REPO.format(model=model), local_dir=str(target))
    print(f"whisper {model} -> {target}")


def fetch_wakeword(root: Path) -> None:
    import openwakeword

    target = root / "wakeword"
    target.mkdir(parents=True, exist_ok=True)
    openwakeword.utils.download_models(target_directory=str(target))
    print(f"wake-word models -> {target}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="Piper voice name (US English by default)")
    ap.add_argument("--whisper", default=DEFAULT_WHISPER, choices=["small.en", "base.en", "tiny.en"])
    ap.add_argument("--check", action="store_true", help="report what is present and download nothing")
    ap.add_argument("--training-voices", action="store_true",
                    help="also fetch the extra Piper voices that scripts/train_wakeword.py uses (about 400 MB)")
    args = ap.parse_args(argv)

    root = models_root()
    refuse_c_drive(root)
    for env in ("HF_HOME", "TEMP", "TMP"):
        value = os.environ.get(env, "")
        if value.upper().startswith("C:"):
            raise SystemExit(f"refusing to run with {env}={value}: set it to a D: path first")

    if args.check:
        status = check(root, args.voice, args.whisper)
        for name, ok in status.items():
            print(f"{'ok     ' if ok else 'MISSING'} {name}")
        return 0 if all(status.values()) else 1

    fetch_piper(root, args.voice)
    fetch_whisper(root, args.whisper)
    fetch_wakeword(root)
    if args.training_voices:
        for name in TRAINING_VOICES:
            fetch_piper(root, name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
