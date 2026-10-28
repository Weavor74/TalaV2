"""
python/harness/voice_kokoro.py -- Tala's local voice (Kokoro-82M, Apache-2.0), replacing Piper (GPL-3.0).

The network is vendored (python/harness/kokoro_model) because `pip install kokoro` drags in phonemizer-fork (GPL-3.0) and
espeakng-loader. English G2P is misaki's own dictionary with no fallback, so a word it does not know has to be added to the
lexicon or it is silent -- the tests pin that, and that the names that matter are in it. Everything here runs against a fake
engine; the one real-synthesis test is skipped when the weights are not on this machine.
"""
from __future__ import annotations

import io
import json
import logging
import wave

import numpy as np
import pytest
import torch

from python.harness import voice_kokoro as vk


@pytest.fixture()
def models(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(vk, "_spacy_model_present", lambda: True)
    vk._ENGINE = None
    yield tmp_path / "voice" / "kokoro"
    vk._ENGINE = None


def _install(root, voice="af_heart"):
    (root / "voices").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "kokoro-v1_0.pth").write_bytes(b"x")
    (root / "voices" / f"{voice}.pt").write_bytes(b"x")


# --- what counts as installed -------------------------------------------------------------------------------------


def test_nothing_is_installed_in_an_empty_models_dir(models):
    assert vk.installed("af_heart") is False
    assert "setup_voice.py" in vk.missing_reason("af_heart")


def test_installed_needs_the_config_the_weights_and_the_voice(models):
    _install(models)
    assert vk.installed("af_heart") is True
    assert vk.installed("am_adam") is False  # a different voice that was never fetched
    (models / "config.json").unlink()
    assert vk.installed("af_heart") is False


def test_a_missing_spacy_model_is_reported_not_downloaded(models, monkeypatch):
    _install(models)
    monkeypatch.setattr(vk, "_spacy_model_present", lambda: False)
    assert vk.installed("af_heart") is False
    assert "en_core_web_sm" in vk.missing_reason("af_heart")


def test_a_voice_name_cannot_walk_out_of_the_voices_folder(models):
    _install(models)
    assert vk.installed("../config") is False
    assert vk.installed("..\\kokoro-v1_0") is False


# --- the lexicon: names the dictionary does not know ----------------------------------------------------------------


def test_the_names_that_matter_are_in_the_built_in_lexicon():
    for name in ("weavor", "starlancer", "kagura"):
        assert name in vk.BUILTIN_LEXICON


def test_lexicon_entries_are_registered_under_both_cases():
    golds: dict[str, str] = {}
    vk._register_lexicon(golds, {"weavor": "wˈivɔɹ"})
    assert golds == {"weavor": "wˈivɔɹ", "Weavor": "wˈivɔɹ"}  # mid-sentence proper nouns look up the capitalised form


def test_a_users_lexicon_file_adds_to_and_overrides_the_built_in_one(models):
    models.mkdir(parents=True)
    (models / "lexicon.json").write_text(json.dumps({"zorblax": "zˈɔɹblæks", "weavor": "wˈeɪvɔɹ"}), encoding="utf-8")
    merged = vk.lexicon()
    assert merged["zorblax"] == "zˈɔɹblæks"
    assert merged["weavor"] == "wˈeɪvɔɹ"
    assert "kagura" in merged


def test_a_broken_lexicon_file_is_ignored(models):
    models.mkdir(parents=True)
    (models / "lexicon.json").write_text("{ not json", encoding="utf-8")
    assert "weavor" in vk.lexicon()


# --- splitting text so every chunk fits the model's context -----------------------------------------------------------


def test_short_text_is_one_chunk():
    assert vk._chunk_text("Okay, I hear you.") == ["Okay, I hear you."]


def test_sentences_are_split_but_not_mid_word():
    text = "First sentence here. Second one follows! And a third? Yes."
    chunks = vk._chunk_text(text, limit=30)
    assert "".join(c.replace(" ", "") for c in chunks) == text.replace(" ", "")
    assert all(len(c) <= 30 for c in chunks)
    assert all(c == c.strip() for c in chunks)


def test_an_unpunctuated_wall_of_text_splits_on_spaces():
    text = " ".join(["word"] * 400)
    chunks = vk._chunk_text(text, limit=100)
    assert len(chunks) > 10 and all(len(c) <= 100 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_one_unbreakable_token_longer_than_the_limit_is_cut():
    chunks = vk._chunk_text("x" * 250, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]


# --- synthesis, against a fake engine ---------------------------------------------------------------------------------


class FakeEngine:
    """Phonemes are the text itself; each phoneme character is 100 samples of a steady tone."""

    def __init__(self):
        self.spoken: list[tuple[str, str]] = []

    def _phonemize(self, text):
        return text

    def speak(self, phonemes, voice):
        self.spoken.append((phonemes, voice))
        return torch.full((100 * len(phonemes),), 0.5)


@pytest.fixture()
def fake(models, monkeypatch):
    _install(models)
    engine = FakeEngine()
    monkeypatch.setattr(vk, "_load", lambda: engine)
    return engine


def _read_wav(data: bytes):
    with wave.open(io.BytesIO(data)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def test_a_reply_becomes_a_mono_16_bit_24khz_wav(fake):
    wav, rate = vk.synthesize_wav("Hello there.", "af_heart")
    channels, width, framerate, samples = _read_wav(wav)
    assert (channels, width, framerate, rate) == (1, 2, 24000, 24000)
    assert len(samples) == 100 * len("Hello there.")
    assert samples.max() == int(0.5 * 32767)


def test_loud_samples_are_clipped_not_wrapped(fake, monkeypatch):
    monkeypatch.setattr(fake, "speak", lambda phonemes, voice: torch.tensor([2.0, -2.0, 0.0]))
    _wav, _rate = vk.synthesize_wav("Hi.", "af_heart")
    _c, _w, _r, samples = _read_wav(_wav)
    assert samples.tolist() == [32767, -32767, 0]


def test_chunks_are_joined_with_a_short_pause(fake):
    long_text = "One short sentence. " * 60
    wav, _rate = vk.synthesize_wav(long_text, "af_heart")
    _c, _w, _r, samples = _read_wav(wav)
    assert len(fake.spoken) > 1
    spoken_samples = sum(100 * len(p) for p, _v in fake.spoken)
    assert len(samples) == spoken_samples + vk.PAUSE_SAMPLES * (len(fake.spoken) - 1)


def test_the_requested_voice_is_used_when_it_is_installed_otherwise_the_default(fake, models):
    _install(models, "am_adam")
    vk.synthesize_wav("Hi there.", "am_adam")
    vk.synthesize_wav("Hi there.", "not_a_voice")
    assert [v for _p, v in fake.spoken] == ["am_adam", vk.DEFAULT_VOICE]


def test_synthesis_without_the_models_says_how_to_install_them(models):
    with pytest.raises(RuntimeError, match="setup_voice.py"):
        vk.synthesize_wav("Hello.", "af_heart")


def test_nothing_to_say_is_an_error_not_a_silent_wav(fake):
    with pytest.raises(RuntimeError, match="no audio"):
        vk.synthesize_wav("   ", "af_heart")


# --- words the dictionary does not know ----------------------------------------------------------------------------------


def test_unknown_words_are_logged_once_each(models, caplog):
    vk._WARNED.clear()
    with caplog.at_level(logging.WARNING, logger=vk.log.name):
        vk._note_unknown("hello ❓ world", "hello Zorblax world")
        vk._note_unknown("hello ❓ world", "hello Zorblax world")
    assert caplog.text.count("Zorblax") == 1
    assert "lexicon.json" in caplog.text


def test_text_the_dictionary_fully_knows_logs_nothing(models, caplog):
    vk._WARNED.clear()
    with caplog.at_level(logging.WARNING, logger=vk.log.name):
        vk._note_unknown("həlˈO wˈɜɹld", "hello world")
    assert caplog.text == ""


# --- the real thing, when the weights are here ----------------------------------------------------------------------------


def _real_models_present():
    from python.harness import voice_settings

    root = voice_settings.models_dir() / "kokoro"
    return (root / "kokoro-v1_0.pth").is_file() and vk.module_present() and vk._spacy_model_present()


@pytest.mark.skipif(not _real_models_present(), reason="Kokoro weights not installed (scripts/setup_voice.py)")
def test_real_synthesis_is_audible_and_faster_than_real_time():
    import time

    vk._ENGINE = None
    vk.synthesize_wav("Warm up.", vk.DEFAULT_VOICE)
    started = time.perf_counter()
    wav, rate = vk.synthesize_wav("I think the garden needs water, honestly, so we should go and do that before the heat sets in.",
                                  vk.DEFAULT_VOICE)
    took = time.perf_counter() - started
    _c, _w, _r, samples = _read_wav(wav)
    seconds = len(samples) / rate
    assert 3.0 < seconds < 12.0
    assert np.abs(samples).max() > 3000, "the audio is silent"
    assert took < seconds * 1.5, f"{took:.1f}s to say {seconds:.1f}s of speech"
    import sys

    assert not {"phonemizer", "espeakng_loader", "misaki.espeak", "piper", "kokoro"} & set(sys.modules), "a GPL path was imported"


# --- the point of the whole exercise: nothing GPL in the voice that ships -------------------------------------------------------


def test_no_runtime_source_imports_a_gpl_speech_package():
    """piper-tts, phonemizer-fork and espeak-ng are GPL-3.0. scripts/train_wakeword.py may use Piper (a developer's training
    tool, in its own extra); nothing under python/ or sdk/ may, or the voice that ships is GPL again."""
    import re
    from pathlib import Path

    banned = re.compile(r"^\s*(?:import|from)\s+(?:piper|phonemizer|espeakng_loader|espeakng|misaki\.espeak|kokoro)\b", re.M)
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for tree in ("python", "sdk"):
        for path in (root / tree).rglob("*.py"):
            if "kokoro_model" in path.parts:
                continue  # the vendored network imports only torch/transformers; checked by the line below
            if banned.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(path.relative_to(root).as_posix())
    assert offenders == []


def test_the_vendored_network_imports_no_gpl_package():
    from pathlib import Path

    gpl = ("phonemizer", "espeak", "piper", "from kokoro", "import kokoro")
    folder = Path(__file__).resolve().parents[1] / "python" / "harness" / "kokoro_model"
    for path in folder.glob("*.py"):
        imports = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith(("import ", "from "))]
        assert not [ln for ln in imports if any(g in ln for g in gpl)], path.name
    assert (folder / "LICENSE").is_file(), "the Apache-2.0 licence text must travel with the vendored code"


def test_the_voice_extra_does_not_depend_on_a_gpl_package():
    import tomllib
    from pathlib import Path

    extras = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    voice = " ".join(extras["voice"]).lower()
    for gpl in ("piper", "phonemizer", "espeak", "kokoro"):
        assert gpl not in voice, f"the voice extra depends on {gpl}"
    assert "misaki[" not in voice, "misaki's `en` extra is what carries phonemizer-fork and espeakng-loader"
    # the wake-word trainer speaks with Kokoro now, so Piper (GPL) is not needed even as a developer tool
    assert not any(d.lower().startswith("piper") for d in extras["wakeword-training"])
