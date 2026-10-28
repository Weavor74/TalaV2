"""
scripts/setup_voice.py -- fetch the local voice models. The only place voice touches the network, and only when run.

Tala's voice is fully local: Kokoro (speech), faster-whisper (hearing), Silero VAD (end of utterance, ships inside its
pip package) and openWakeWord (the wake phrase). At runtime every one of them loads from disk and the harness sets
HF_HUB_OFFLINE=1; nothing is fetched. This script is what puts the files there, once, and it refuses any target that is
not on D:.

    python scripts/setup_voice.py              # Kokoro voice, whisper base.en, wake-word runtime models, spaCy model
    python scripts/setup_voice.py --whisper small.en --voice am_adam
    python scripts/setup_voice.py --check      # report what is present, download nothing
    python scripts/setup_voice.py --training-voices   # plus Kokoro's other US voices, which the wake-word trainer speaks with

Layout under ``TALA_MODELS_DIR`` (default ``<user data>/models``; set it to D:\\models on Kagura)::

    voice/kokoro/config.json, kokoro-v1_0.pth, voices/<voice>.pt   (Kokoro-82M, Apache-2.0, pinned revision, hash-checked)
    voice/kokoro/lexicon.json         (optional: your own word -> phonemes, for words Kokoro cannot pronounce)
    voice/whisper/<model>/            (CTranslate2 weights)
    voice/wakeword/                   (openWakeWord melspectrogram/embedding + pretrained phrases)
    voice/kokoro/voices/<voice>.pt    (all twenty US voices with --training-voices: scripts/train_wakeword.py trains on sixteen and
                                       measures on four it never heard)

The spaCy model ``en_core_web_sm`` (misaki's tagger) is installed into the active Python environment, not into the models
folder, because that is where spaCy looks for it.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_VOICE = "af_heart"
DEFAULT_WHISPER = "base.en"
KOKORO_REPO = "hexgrad/Kokoro-82M"
#: Pinned so the weights cannot change under an install; the hashes are checked after every download.
KOKORO_REVISION = "f3ff3571791e39611d31c381e3a41a3af07b4987"
KOKORO_SHA256 = {
    "config.json": "5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f",
    "kokoro-v1_0.pth": "496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4",
    "voices/af_heart.pt": "0ab5709b8ffab19bfd849cd11d98f75b60af7733253ad0d67b12382a102cb4ff",
}
SPACY_MODEL = "en_core_web_sm"
SPACY_WHEEL = "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
#: Kokoro's US-English voices, used to synthesise training and test clips for a custom wake-word model (scripts/train_wakeword.py).
KOKORO_US_VOICES = ("af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah",
                    "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa")
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


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def spacy_model_present() -> bool:
    import importlib.util

    return importlib.util.find_spec(SPACY_MODEL) is not None


def check(root: Path, voice: str, whisper: str) -> dict[str, bool]:
    kokoro = root / "kokoro"
    return {
        f"kokoro voice {voice}": all((kokoro / f).is_file() for f in ("config.json", "kokoro-v1_0.pth", f"voices/{voice}.pt")),
        f"spaCy model {SPACY_MODEL}": spacy_model_present(),
        f"whisper {whisper}": (root / "whisper" / whisper / "model.bin").is_file(),
        "wake-word runtime models": any((root / "wakeword").glob("embedding_model*")) and any((root / "wakeword").glob("melspectrogram*")),
    }


def fetch_kokoro(root: Path, voice: str) -> None:
    """The network, the config and one voice, at the pinned revision; each file is checked against its published hash
    (voices other than the default have none recorded here, so they are verified only by the revision pin)."""
    from huggingface_hub import hf_hub_download

    target = root / "kokoro"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "kokoro-v1_0.pth", f"voices/{voice}.pt"):
        path = Path(hf_hub_download(KOKORO_REPO, name, revision=KOKORO_REVISION, local_dir=str(target)))
        expected = KOKORO_SHA256.get(name)
        if expected and sha256_of(path) != expected:
            path.unlink()
            raise SystemExit(f"{name} does not match its expected SHA-256; it was deleted. Do not use it.")
    print(f"kokoro voice {voice} -> {target}")


def fetch_spacy_model() -> None:
    """misaki tags words with spaCy before it looks them up. Installed into this interpreter's environment."""
    if spacy_model_present():
        print(f"spaCy model {SPACY_MODEL} already installed")
        return
    uv = shutil.which("uv")
    command = [uv, "pip", "install", "--python", sys.executable, SPACY_WHEEL] if uv else [sys.executable, "-m", "pip", "install", SPACY_WHEEL]
    subprocess.run(command, check=True)
    print(f"spaCy model {SPACY_MODEL} installed")


def fetch_kokoro_voices(root: Path, voices: tuple[str, ...]) -> None:
    """More Kokoro voices (about half a megabyte each), at the same pinned revision as the model."""
    from huggingface_hub import hf_hub_download

    target = root / "kokoro"
    for voice in voices:
        hf_hub_download(KOKORO_REPO, f"voices/{voice}.pt", revision=KOKORO_REVISION, local_dir=str(target))
    print(f"{len(voices)} kokoro voices (wake-word training) -> {target / 'voices'}")


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
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="Kokoro voice id (af_heart by default; am_adam, bf_emma, ...)")
    ap.add_argument("--whisper", default=DEFAULT_WHISPER, choices=["small.en", "base.en", "tiny.en"])
    ap.add_argument("--check", action="store_true", help="report what is present and download nothing")
    ap.add_argument("--training-voices", action="store_true",
                    help="also fetch Kokoro's other US voices that scripts/train_wakeword.py speaks with (about 10 MB)")
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

    fetch_kokoro(root, args.voice)
    fetch_spacy_model()
    fetch_whisper(root, args.whisper)
    fetch_wakeword(root)
    if args.training_voices:
        fetch_kokoro_voices(root, KOKORO_US_VOICES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
