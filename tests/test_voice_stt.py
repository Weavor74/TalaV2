"""
Speech-to-text details that are logic rather than a model: removing the wake phrase from the front of a transcript, and
refusing to turn silence into words.

Whisper hears "Tala" as "Tullah", "Talia", "Tallah", so the wake phrase is stripped by a fuzzy match on the leading
words, and only when the whole phrase matches; anything that does not match is left alone, because dropping the first
words of a real command is worse than keeping "hey tyler".
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from python.harness.voice_stt import SttEngine, strip_wake_phrase


@pytest.mark.parametrize("heard,expected", [
    ("Hey Tala, what time is it?", "what time is it?"),
    ("Hey Tullah, what time is it?", "what time is it?"),
    ("hey talia what's the weather", "what's the weather"),
    ("Hey, Tala. Turn it up.", "Turn it up."),
    ("HEY TALA what is two plus two", "what is two plus two"),
    ("Hey Tala", ""),
    ("Hey Tala.", ""),
])
def test_the_wake_phrase_is_removed_from_the_front(heard, expected):
    assert strip_wake_phrase(heard, "Hey Tala") == expected


@pytest.mark.parametrize("heard", [
    "what time is it", "Hey what time is it", "Hey Tyler, what time is it?", "Say hey Tala to him",
    "Hey there Tala, what time is it?",
])
def test_text_that_does_not_start_with_the_phrase_is_left_alone(heard):
    assert strip_wake_phrase(heard, "Hey Tala") == heard


def test_a_one_word_phrase_is_stripped_too():
    assert strip_wake_phrase("Tala, what time is it", "Tala") == "what time is it"
    assert strip_wake_phrase("what time is it", "Tala") == "what time is it"


def test_an_empty_phrase_changes_nothing():
    assert strip_wake_phrase("Hey Tala what", "") == "Hey Tala what"


# --- the engine, with a stand-in model ----------------------------------------------------------------------------------------------------


class FakeModel:
    def __init__(self, segments):
        self.segments = segments
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter(self.segments), SimpleNamespace(language="en")


def seg(text, no_speech=0.01, logprob=-0.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob)


def audio(seconds=2.0):
    return np.ones(int(16000 * seconds), np.float32) * 0.1


def test_segments_are_joined_into_one_transcript():
    engine = SttEngine(model=FakeModel([seg(" Hey Tala,"), seg(" what time is it?")]))
    assert engine.transcribe(audio()) == "Hey Tala, what time is it?"


def test_silence_that_whisper_hallucinates_into_words_is_dropped():
    engine = SttEngine(model=FakeModel([seg(" Thank you.", no_speech=0.9, logprob=-1.4)]))
    assert engine.transcribe(audio()) == ""


def test_a_confident_segment_is_kept_even_if_another_is_not():
    engine = SttEngine(model=FakeModel([seg(" Real words here."), seg(" you", no_speech=0.8, logprob=-1.5)]))
    assert engine.transcribe(audio()) == "Real words here."


def test_audio_too_short_to_be_speech_is_not_sent_to_the_model():
    model = FakeModel([seg("ghost")])
    assert SttEngine(model=model).transcribe(np.zeros(1000, np.float32)) == ""
    assert model.calls == []


def test_the_model_is_asked_in_english_without_a_cloud_fallback():
    model = FakeModel([seg("ok")])
    SttEngine(model=model).transcribe(audio())
    assert model.calls[0]["language"] == "en" and model.calls[0]["beam_size"] == 1


def test_a_hint_about_the_assistants_name_is_given_to_the_model():
    model = FakeModel([seg("ok")])
    SttEngine(model=model, hint="Tala").transcribe(audio())
    assert "Tala" in model.calls[0]["initial_prompt"]


def test_a_model_error_becomes_an_empty_transcript_not_a_crash():
    class Broken:
        def transcribe(self, audio, **kw):
            raise RuntimeError("ctranslate2 failed")

    assert SttEngine(model=Broken()).transcribe(audio()) == ""


def test_loading_refuses_to_download(monkeypatch, tmp_path):
    """The engine loads from D: only; a missing model is an error naming the setup script, never a network fetch."""
    monkeypatch.setenv("TALA_MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    engine = SttEngine()
    with pytest.raises(RuntimeError, match="setup_voice"):
        engine.load()
