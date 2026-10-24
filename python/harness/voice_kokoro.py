"""
python/harness/voice_kokoro.py -- Kokoro-82M, the local neural voice.

Kokoro is an 82-million-parameter text-to-speech network (Apache-2.0 weights, StyleTTS 2 architecture) that runs faster than
real time on the CPU, so the 6 GB GPU stays free for the language model. It replaced Piper because piper-tts is GPL-3.0.

Why this is not ``pip install kokoro``: that package depends on phonemizer-fork (GPL-3.0) and espeakng-loader, and imports
them unconditionally, so even a build that never calls them is GPL. The network itself is four Apache-2.0 files, vendored
in ``python.harness.kokoro_model``; the text-to-phoneme step is misaki's own English dictionary with ``fallback=None``
(the Apache-2.0 half of misaki). The cost of no fallback: a word the dictionary does not know is silent. The names that
matter are in ``BUILTIN_LEXICON``, and anything else goes in ``<models>/voice/kokoro/lexicon.json`` (word -> phonemes).

Files live under ``<models>/voice/kokoro`` (``scripts/setup_voice.py`` puts them there): ``config.json``,
``kokoro-v1_0.pth`` and ``voices/<voice>.pt``. Nothing is downloaded here and the text never leaves the process.

``synthesize_wav`` is blocking (callers run it in a worker thread). The engine is loaded once and cached: loading takes tens
of seconds the first time, a sentence takes a fraction of its own length after that.
"""
from __future__ import annotations

import io
import json
import logging
import re
import threading
import wave
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000
DEFAULT_VOICE = "af_heart"
#: The network's context is 512 tokens; stay well inside it so a long sentence is split instead of asserting.
MAX_PHONEMES = 400
#: Characters per synthesis chunk. English phonemes run about one per character, so this keeps a chunk under MAX_PHONEMES.
CHUNK_CHARS = 300
#: Silence between joined chunks (150 ms).
PAUSE_SAMPLES = SAMPLE_RATE * 15 // 100
UNKNOWN_WORD = "❓"  # what misaki emits for a word it cannot pronounce

#: Words the dictionary does not know that this system says. Respellings are IPA in misaki's notation.
BUILTIN_LEXICON: dict[str, str] = {
    "weavor": "wˈivɔɹ",
    "starlancer": "stˈɑɹlænsəɹ",
    "kagura": "kɑɡˈuɹɑ",
}

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_VOICE_NAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_ENGINE: Any = None
_LOCK = threading.Lock()
_WARNED: set[str] = set()


def _root() -> Path:
    from python.harness.voice_settings import models_dir

    return models_dir() / "kokoro"


def _spacy_model_present() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("en_core_web_sm") is not None
    except Exception:
        return False


def module_present() -> bool:
    """The Python packages Kokoro needs. No GPL package is among them."""
    import importlib.util

    try:
        return all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers", "misaki", "spacy", "num2words"))
    except Exception:
        return False


def _voice_file(voice: str) -> Path | None:
    return _root() / "voices" / f"{voice}.pt" if _VOICE_NAME.match(voice or "") else None


def model_installed() -> bool:
    """The network's config and weights are on disk (a voice and the spaCy model are checked separately)."""
    root = _root()
    return (root / "config.json").is_file() and (root / "kokoro-v1_0.pth").is_file()


def missing_reason(voice: str) -> str:
    """Why Kokoro cannot speak with ``voice`` right now, or an empty string when it can."""
    root = _root()
    voice_file = _voice_file(voice)
    if not (root / "config.json").is_file() or not (root / "kokoro-v1_0.pth").is_file():
        return f"the Kokoro model is not installed under {root}; run scripts/setup_voice.py"
    if voice_file is None or not voice_file.is_file():
        return f"Kokoro voice {voice!r} is not installed; run scripts/setup_voice.py --voice {voice}"
    if not _spacy_model_present():
        return "the spaCy model en_core_web_sm is not installed (misaki needs it); run scripts/setup_voice.py"
    return ""


def installed(voice: str) -> bool:
    return not missing_reason(voice)


def _register_lexicon(golds: dict[str, str], words: dict[str, str]) -> None:
    """Add words to misaki's dictionary under both cases: a capitalised word in a sentence is tagged a proper noun and looked
    up under its capitalised form, so a lowercase-only entry is silent mid-sentence."""
    for word, phonemes in words.items():
        golds[word] = phonemes
        golds[word.capitalize()] = phonemes


def lexicon() -> dict[str, str]:
    """The built-in names plus ``lexicon.json`` from the Kokoro folder (the user's entries win). A broken file is ignored."""
    words = dict(BUILTIN_LEXICON)
    try:
        saved = json.loads((_root() / "lexicon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return words
    if isinstance(saved, dict):
        words.update({str(k).strip().lower(): str(v) for k, v in saved.items() if str(k).strip() and str(v).strip()})
    return words


def _chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split on sentence ends, then spaces, then (for a single enormous token) characters, packing up to ``limit``."""
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split((text or "").strip()):
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        for word in sentence.split():
            while len(word) > limit:
                pieces.append(word[:limit])
                word = word[limit:]
            pieces.append(word)
    chunks: list[str] = []
    for piece in (p for p in pieces if p):
        if chunks and len(chunks[-1]) + 1 + len(piece) <= limit:
            chunks[-1] += " " + piece
        else:
            chunks.append(piece)
    return chunks


def _note_unknown(phonemes: str, text: str) -> None:
    """Say, once per word, which words came out silent and how to teach them."""
    if UNKNOWN_WORD not in phonemes:
        return
    spoken, written = phonemes.split(), text.split()
    if len(spoken) == len(written):
        words = [re.sub(r"^\W+|\W+$", "", w) for w, p in zip(written, spoken) if UNKNOWN_WORD in p]
    else:
        words = [text[:40]]
    fresh = [w for w in words if w and w not in _WARNED]
    if fresh:
        _WARNED.update(fresh)
        log.warning("voice: Kokoro does not know how to say %s and will be silent there; add %s to %s",
                    ", ".join(repr(w) for w in fresh), "it" if len(fresh) == 1 else "them", _root() / "lexicon.json")


class _Engine:
    """The network, the phonemiser and the voice rows, loaded once."""

    def __init__(self) -> None:
        import torch
        from misaki import en

        from python.harness.kokoro_model.model import KModel

        root = _root()
        self._torch = torch
        self.model = KModel(root / "config.json", root / "kokoro-v1_0.pth").eval()
        self.g2p = en.G2P(trf=False, british=False, fallback=None)
        _register_lexicon(self.g2p.lexicon.golds, lexicon())
        self._voices: dict[str, Any] = {}

    def _phonemize(self, text: str) -> str:
        phonemes, _tokens = self.g2p(text)
        return phonemes

    def _voice(self, name: str):
        if name not in self._voices:
            voice_file = _voice_file(name)
            self._voices[name] = self._torch.load(str(voice_file), map_location="cpu", weights_only=True)
        return self._voices[name]

    def speak(self, phonemes: str, voice: str):
        ref_s = self._voice(voice)[len(phonemes) - 1]
        with self._torch.no_grad():
            return self.model(phonemes, ref_s, 1.0)


def _load() -> Any:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = _Engine()
            log.info("voice: loaded Kokoro")
        return _ENGINE


def _say(engine: Any, text: str, voice: str, limit: int = CHUNK_CHARS) -> list:
    """Audio for one chunk; a chunk that phonemises past the model's context is split and spoken as two."""
    phonemes = engine._phonemize(text)
    if len(phonemes) <= MAX_PHONEMES or limit <= 20:
        _note_unknown(phonemes, text)
        return [engine.speak(phonemes[:MAX_PHONEMES], voice)] if phonemes else []
    audio: list = []
    for part in _chunk_text(text, limit // 2):
        audio.extend(_say(engine, part, voice, limit // 2))
    return audio


def synthesize_wav(text: str, voice: str = DEFAULT_VOICE) -> tuple[bytes, int]:
    """``(wav_bytes, sample_rate)`` for ``text``. Raises, saying why, if Kokoro is not installed."""
    import numpy as np

    name = voice if voice and installed(voice) else DEFAULT_VOICE
    reason = missing_reason(name)
    if reason:
        raise RuntimeError(f"Kokoro cannot speak: {reason}")
    engine = _load()
    parts: list[np.ndarray] = []
    for chunk in _chunk_text(text):
        for audio in _say(engine, chunk, name):
            if len(parts):
                parts.append(np.zeros(PAUSE_SAMPLES, dtype=np.float32))
            parts.append(np.asarray(audio, dtype=np.float32).reshape(-1))
    if not parts:
        raise RuntimeError("Kokoro produced no audio")
    pcm = (np.clip(np.concatenate(parts), -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue(), SAMPLE_RATE
