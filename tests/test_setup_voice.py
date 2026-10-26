"""
scripts/setup_voice.py -- the one place the voice touches the network.

Kokoro's files are fetched at a pinned revision and checked against their published SHA-256: this script is what puts model
weights on the machine, so a file that is not the one that was reviewed must be deleted, not loaded.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_voice.py"


@pytest.fixture()
def sv(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_MODELS_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("setup_voice_kokoro_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["setup_voice_kokoro_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _fake_download(contents: dict[str, bytes]):
    calls = []

    def download(repo, name, revision=None, local_dir=None):
        calls.append((repo, name, revision))
        path = Path(local_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents[name])
        return str(path)

    return download, calls


def _patch_hub(monkeypatch, download):
    import types

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=download))


def test_the_kokoro_files_are_fetched_at_the_pinned_revision_and_hash_checked(sv, monkeypatch, tmp_path):
    files = {"config.json": b"{}", "kokoro-v1_0.pth": b"weights", "voices/af_heart.pt": b"voice"}
    monkeypatch.setattr(sv, "KOKORO_SHA256", {n: hashlib.sha256(b).hexdigest() for n, b in files.items()})
    download, calls = _fake_download(files)
    _patch_hub(monkeypatch, download)

    sv.fetch_kokoro(tmp_path / "voice", "af_heart")

    assert sorted(n for _r, n, _v in calls) == sorted(files)
    assert {r for r, _n, _v in calls} == {"hexgrad/Kokoro-82M"}
    assert {v for _r, _n, v in calls} == {sv.KOKORO_REVISION} and len(sv.KOKORO_REVISION) == 40


def test_a_file_that_does_not_match_its_hash_is_deleted_and_the_setup_stops(sv, monkeypatch, tmp_path):
    files = {"config.json": b"{}", "kokoro-v1_0.pth": b"TAMPERED", "voices/af_heart.pt": b"voice"}
    monkeypatch.setattr(sv, "KOKORO_SHA256", {"kokoro-v1_0.pth": hashlib.sha256(b"the reviewed weights").hexdigest()})
    download, _calls = _fake_download(files)
    _patch_hub(monkeypatch, download)

    with pytest.raises(SystemExit, match="SHA-256"):
        sv.fetch_kokoro(tmp_path / "voice", "af_heart")

    assert not (tmp_path / "voice" / "kokoro" / "kokoro-v1_0.pth").exists()


def test_a_voice_with_no_recorded_hash_is_accepted_on_the_revision_pin_alone(sv, monkeypatch, tmp_path):
    files = {"config.json": b"{}", "kokoro-v1_0.pth": b"w", "voices/am_adam.pt": b"another voice"}
    monkeypatch.setattr(sv, "KOKORO_SHA256", {})
    download, _calls = _fake_download(files)
    _patch_hub(monkeypatch, download)
    sv.fetch_kokoro(tmp_path / "voice", "am_adam")
    assert (tmp_path / "voice" / "kokoro" / "voices" / "am_adam.pt").is_file()


def test_the_published_hashes_cover_the_weights_the_config_and_the_default_voice(sv):
    assert set(sv.KOKORO_SHA256) == {"config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"}
    assert all(len(h) == 64 and set(h) <= set("0123456789abcdef") for h in sv.KOKORO_SHA256.values())
    assert sv.DEFAULT_VOICE == "af_heart"


def test_check_reports_what_is_present(sv, monkeypatch, tmp_path):
    root = tmp_path / "voice"
    monkeypatch.setattr(sv, "spacy_model_present", lambda: False)
    status = sv.check(root, "af_heart", "base.en")
    assert status["kokoro voice af_heart"] is False and status["spaCy model en_core_web_sm"] is False

    for name in ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"):
        path = root / "kokoro" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    monkeypatch.setattr(sv, "spacy_model_present", lambda: True)
    status = sv.check(root, "af_heart", "base.en")
    assert status["kokoro voice af_heart"] is True and status["spaCy model en_core_web_sm"] is True


def test_the_spacy_model_is_installed_from_its_pinned_wheel_only_when_missing(sv, monkeypatch):
    ran = []
    monkeypatch.setattr(sv.subprocess, "run", lambda command, check: ran.append(command))
    monkeypatch.setattr(sv.shutil, "which", lambda name: "uv-exe")

    monkeypatch.setattr(sv, "spacy_model_present", lambda: True)
    sv.fetch_spacy_model()
    assert ran == []

    monkeypatch.setattr(sv, "spacy_model_present", lambda: False)
    sv.fetch_spacy_model()
    assert ran[0][:4] == ["uv-exe", "pip", "install", "--python"] and ran[0][-1] == sv.SPACY_WHEEL
    assert "en_core_web_sm-3.8.0" in sv.SPACY_WHEEL

    monkeypatch.setattr(sv.shutil, "which", lambda name: None)
    sv.fetch_spacy_model()
    assert ran[1][1:4] == ["-m", "pip", "install"]


def _quiet_main(sv, monkeypatch, **fetchers):
    monkeypatch.setattr(sv, "refuse_c_drive", lambda path: None)  # the refusal has its own test; the temp dir may be on C:
    monkeypatch.setattr(sv, "models_root", lambda: Path("models"))
    monkeypatch.setenv("HF_HOME", "D:/hf-cache")
    monkeypatch.setenv("TEMP", "D:/caches/tmp")
    monkeypatch.setenv("TMP", "D:/caches/tmp")
    for name in ("fetch_kokoro", "fetch_spacy_model", "fetch_whisper", "fetch_wakeword", "fetch_kokoro_voices"):
        monkeypatch.setattr(sv, name, fetchers.get(name, lambda *a, **k: None))


def test_a_default_setup_fetches_one_voice_not_the_training_set(sv, monkeypatch):
    called = []
    _quiet_main(sv, monkeypatch, fetch_kokoro=lambda *a, **k: called.append("fetch_kokoro"),
                fetch_kokoro_voices=lambda *a, **k: called.append("fetch_kokoro_voices"))

    assert sv.main([]) == 0

    assert called == ["fetch_kokoro"]


def test_the_other_us_voices_are_fetched_only_for_wake_word_training(sv, monkeypatch):
    fetched = []
    _quiet_main(sv, monkeypatch, fetch_kokoro_voices=lambda root, voices: fetched.extend(voices))

    assert sv.main(["--training-voices"]) == 0

    assert set(fetched) == set(sv.KOKORO_US_VOICES) and len(fetched) == 20
    assert {"af_heart", "am_adam", "am_puck"} <= set(fetched)


def test_the_training_voices_are_the_ones_the_wake_word_trainer_holds_out_and_trains_on():
    import importlib.util

    spec = importlib.util.spec_from_file_location("tw_for_voices", Path(__file__).resolve().parents[1] / "scripts" / "train_wakeword.py")
    tw = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["tw_for_voices"] = tw
    spec.loader.exec_module(tw)
    import scripts.setup_voice as setup

    assert tuple(tw.KOKORO_VOICES) == tuple(setup.KOKORO_US_VOICES)


def test_voices_come_from_the_pinned_revision_into_the_voices_folder(sv, monkeypatch, tmp_path):
    import huggingface_hub

    asked = []

    def fake_download(repo, name, revision=None, local_dir=None):
        asked.append((repo, name, revision))
        target = Path(local_dir) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"voice")
        return str(target)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    sv.fetch_kokoro_voices(tmp_path, ("af_nova", "am_puck"))

    assert asked == [(sv.KOKORO_REPO, "voices/af_nova.pt", sv.KOKORO_REVISION), (sv.KOKORO_REPO, "voices/am_puck.pt", sv.KOKORO_REVISION)]
    assert (tmp_path / "kokoro" / "voices" / "af_nova.pt").is_file()


def test_piper_is_gone_from_setup(sv):
    """Piper is GPL-3.0; the wake-word trainer now makes its clips with Kokoro, so nothing here fetches it."""
    assert not hasattr(sv, "fetch_piper") and not hasattr(sv, "PIPER_REPO") and not hasattr(sv, "piper_voice_path")


def test_a_target_on_c_drive_is_refused(sv):
    with pytest.raises(SystemExit, match="C:"):
        sv.refuse_c_drive(Path("C:/models/voice"))
