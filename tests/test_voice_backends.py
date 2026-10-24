"""
Which text-to-speech backend runs, and the guarantee that no text leaves the machine unless the user said so.

Tala is local-only. edge-tts sends the text to Microsoft, so it is never in the chain by default -- not even when the
package is installed and nothing else works -- and the provider-table TTS (an OpenAI-style endpoint with a key) is only
used when its endpoint is on this machine. Kokoro is the default; the system voice is the offline fallback.
"""
from __future__ import annotations

import asyncio

import pytest

from python.harness import voice, voice_settings


@pytest.fixture(autouse=True)
def userdata(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.delenv("TALA_ALLOW_REMOTE_PROVIDERS", raising=False)
    return tmp_path


ALL = {"kokoro": True, "pyttsx3": True, "edge-tts": True}


def test_kokoro_is_the_default_and_edge_tts_is_not_in_the_chain():
    assert voice._select_backends(voice_settings.read(), ALL) == ["kokoro", "pyttsx3"]


def test_edge_tts_is_never_chosen_by_default_even_when_it_is_all_that_is_installed():
    assert voice._select_backends(voice_settings.read(), {"kokoro": False, "pyttsx3": False, "edge-tts": True}) == []


def test_edge_tts_is_only_ever_the_last_resort_and_only_when_enabled():
    chain = voice._select_backends({**voice_settings.read(), "allow_cloud_tts": True}, ALL)
    assert chain == ["kokoro", "pyttsx3", "edge-tts"]


def test_enabling_cloud_tts_does_not_put_it_ahead_of_a_local_voice():
    chain = voice._select_backends({**voice_settings.read(), "allow_cloud_tts": True}, ALL)
    assert chain.index("edge-tts") > chain.index("kokoro")


def test_cloud_tts_defaults_to_off():
    assert voice_settings.read()["allow_cloud_tts"] is False


def test_an_unavailable_backend_is_skipped():
    assert voice._select_backends(voice_settings.read(), {"kokoro": False, "pyttsx3": True, "edge-tts": True}) == ["pyttsx3"]


def run(coro):
    return asyncio.run(coro)


def test_edge_tts_is_not_called_when_nothing_local_works(monkeypatch):
    called = []

    async def edge(text, voice_name):
        called.append("edge")
        return {"audio_base64": "x"}

    monkeypatch.setattr(voice, "_synthesize_edge", edge)
    monkeypatch.setattr(voice, "_available", lambda: {"kokoro": False, "pyttsx3": False, "edge-tts": True})
    result = run(voice.synthesize_speech("hello"))
    assert called == [] and "error" in result
    assert "kokoro" in result["error"].lower(), "the error says how to get a local voice"


def test_edge_tts_is_called_once_the_user_has_turned_it_on(monkeypatch):
    called = []

    async def edge(text, voice_name):
        called.append("edge")
        return {"audio_base64": "x", "backend": "edge-tts"}

    monkeypatch.setattr(voice, "_synthesize_edge", edge)
    monkeypatch.setattr(voice, "_available", lambda: {"kokoro": False, "pyttsx3": False, "edge-tts": True})
    voice_settings.update({"allow_cloud_tts": True})
    assert run(voice.synthesize_speech("hello"))["backend"] == "edge-tts" and called == ["edge"]


def test_kokoro_is_what_speaks_by_default(monkeypatch):
    monkeypatch.setattr(voice, "_available", lambda: ALL)

    async def kokoro(text, voice_name):
        return {"audio_base64": "p", "mime_type": "audio/wav", "backend": "kokoro", "voice": voice_name}

    async def edge(text, voice_name):
        raise AssertionError("edge-tts must not run")

    monkeypatch.setattr(voice, "_synthesize_kokoro", kokoro)
    monkeypatch.setattr(voice, "_synthesize_edge", edge)
    assert run(voice.synthesize_speech("hello"))["backend"] == "kokoro"


def test_a_failing_local_backend_falls_through_to_the_next_local_one(monkeypatch):
    monkeypatch.setattr(voice, "_available", lambda: ALL)

    async def broken(text, voice_name):
        raise RuntimeError("model missing")

    async def system(text, voice_name):
        return {"audio_base64": "s", "backend": "pyttsx3"}

    monkeypatch.setattr(voice, "_synthesize_kokoro", broken)
    monkeypatch.setattr(voice, "_synthesize_pyttsx3", system)
    assert run(voice.synthesize_speech("hello"))["backend"] == "pyttsx3"


def test_listing_says_which_backends_leave_the_machine():
    listing = run(voice.list_backends())
    by_id = {b["id"]: b for b in listing["backends"]}
    assert by_id["kokoro"]["offline"] is True and by_id["pyttsx3"]["offline"] is True
    assert by_id["edge-tts"]["offline"] is False and by_id["edge-tts"]["enabled"] is False
    assert "microsoft" in by_id["edge-tts"]["note"].lower() and "cloud" in by_id["edge-tts"]["note"].lower()


def test_the_default_is_never_a_cloud_backend(monkeypatch):
    monkeypatch.setattr(voice, "_available", lambda: {"kokoro": False, "pyttsx3": False, "edge-tts": True})
    assert run(voice.list_backends())["default"] is None


# --- the provider table -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint,local", [
    ("http://localhost:8000/v1", True), ("http://127.0.0.1:5002", True), ("http://[::1]:8000", True),
    ("https://api.openai.com/v1", False), ("https://tts.example.com", False), ("http://192.168.1.20:8000", False),
    ("http://localhost.evil.example.com", False), ("", False),
])
def test_only_a_loopback_tts_endpoint_counts_as_local(endpoint, local):
    assert voice._endpoint_is_local(endpoint) is local


def test_a_remote_tts_provider_is_refused_and_never_contacted(monkeypatch):
    class Row(dict):
        pass

    async def fetchrow(*a, **k):
        return Row(name="cloud-tts", config={"endpoint": "https://api.openai.com/v1", "api_key": "sk-test-not-real"})

    contacted = []
    monkeypatch.setattr("python.harness.db.fetchrow", fetchrow)
    monkeypatch.setattr(voice, "_available", lambda: {"kokoro": False, "pyttsx3": False, "edge-tts": False})
    import httpx

    class Boom:
        def __init__(self, *a, **k):
            contacted.append("client")

    monkeypatch.setattr(httpx, "AsyncClient", Boom)
    result = run(voice.synthesize_speech("hello", provider_id="cloud-tts"))
    assert contacted == [] and "error" in result and "local" in result["error"].lower()


def test_remote_is_allowed_only_by_the_explicit_environment_override(monkeypatch):
    monkeypatch.setenv("TALA_ALLOW_REMOTE_PROVIDERS", "1")
    assert voice._tts_provider_allowed("https://api.openai.com/v1") is True
    monkeypatch.delenv("TALA_ALLOW_REMOTE_PROVIDERS")
    assert voice._tts_provider_allowed("https://api.openai.com/v1") is False
    assert voice._tts_provider_allowed("http://127.0.0.1:5002") is True


def test_settings_reject_a_non_boolean_cloud_switch():
    with pytest.raises(ValueError):
        voice_settings.update({"allow_cloud_tts": "yes"})
    assert voice_settings.read()["allow_cloud_tts"] is False


def test_settings_reject_unknown_keys_and_bad_models():
    with pytest.raises(ValueError):
        voice_settings.update({"whisper_model": "large-v3"})
    with pytest.raises(ValueError):
        voice_settings.update({"shell": "rm -rf"})


def test_audio_is_only_written_to_disk_when_a_debug_directory_is_set(monkeypatch):
    monkeypatch.delenv("TALA_VOICE_DEBUG_DIR", raising=False)
    assert voice_settings.debug_dir() is None
    monkeypatch.setenv("TALA_VOICE_DEBUG_DIR", r"C:\Users\x\voice")
    assert voice_settings.debug_dir() is None, "never C:"
    monkeypatch.setenv("TALA_VOICE_DEBUG_DIR", r"D:\tala-audit\voice-debug")
    assert str(voice_settings.debug_dir()).upper().startswith("D:")


def test_the_default_recogniser_is_the_fast_one():
    """small.en measured 2.6 s per utterance whatever its length (it pads to a 30 s window); base.en 0.7 s, same transcripts."""
    assert voice_settings.read()["whisper_model"] == "base.en"
    voice_settings.update({"whisper_model": "small.en"})
    assert voice_settings.read()["whisper_model"] == "small.en"


def test_the_wake_threshold_defaults_to_the_models_own_measured_one():
    assert voice_settings.read()["wake_threshold"] is None
    voice_settings.update({"wake_threshold": 0.7})
    assert voice_settings.read()["wake_threshold"] == 0.7
    voice_settings.update({"wake_threshold": None})
    assert voice_settings.read()["wake_threshold"] is None


def test_the_models_directory_follows_the_install_not_a_drive_letter(monkeypatch, tmp_path):
    """Tala runs from a USB stick on any OS: with nothing configured the models live under the install's own data directory."""
    monkeypatch.delenv("TALA_MODELS_DIR", raising=False)
    assert voice_settings.models_dir() == tmp_path / "models" / "voice"


def test_tala_models_dir_puts_them_wherever_the_user_says(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_MODELS_DIR", str(tmp_path / "elsewhere"))
    assert voice_settings.models_dir() == tmp_path / "elsewhere" / "voice"


def test_the_setup_script_and_the_trainer_use_the_same_models_directory(monkeypatch, tmp_path):
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.delenv("TALA_MODELS_DIR", raising=False)
    path = Path(__file__).resolve().parents[1] / "scripts" / "setup_voice.py"
    spec = importlib.util.spec_from_file_location("setup_voice_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["setup_voice_under_test"] = module
    spec.loader.exec_module(module)
    assert module.models_root() == voice_settings.models_dir()


def test_an_unavailable_kokoro_says_why_in_the_backends_list(monkeypatch, tmp_path):
    from python.harness import voice_kokoro

    monkeypatch.setenv("TALA_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(voice, "_available", lambda: {"kokoro": False, "pyttsx3": True, "edge-tts": False})
    listing = run(voice.list_backends())
    note = {b["id"]: b for b in listing["backends"]}["kokoro"]["note"]
    assert note.startswith("Not available:") and "setup_voice.py" in note
    assert voice_kokoro.missing_reason(voice_settings.read()["tts_voice"]) in note


def test_listening_is_a_conversation_by_default_not_a_wake_word_gate(tmp_path, monkeypatch):
    """Pressing the always-listen button means talk freely. Requiring "Hey Tala" is the opt-in."""
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    assert voice_settings.DEFAULTS["wake_enabled"] is False
    assert voice_settings.read()["wake_enabled"] is False

    from python.harness.voice_runtime import VoiceRuntime

    async def notify(method, params):
        return None

    runtime = VoiceRuntime(notify)
    assert runtime.session.config.wake_required is False
    assert runtime.status()["wake_enabled"] is False


def test_an_explicit_wake_setting_is_still_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    voice_settings.update({"wake_enabled": True})
    assert voice_settings.read()["wake_enabled"] is True
