"""
The voice loop end to end, with fake models: audio in, wake gating, transcripts, turns, barge-in, follow-up, dictation.

The VAD is an energy detector, the wake word and the recogniser are stand-ins, and the clock is advanced by the audio fed,
so each flow runs deterministically. What is under test is the wiring: that ambient speech is never transcribed, that a wake
word claims the utterance it was spoken in, that talking over her stops her and starts a new turn, and that the halt and the
off switch really stop it listening.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from python.harness.voice_runtime import VoiceRuntime
from python.harness.voice_session import State
from python.harness.voice_vad import SAMPLE_RATE

CHUNK = 1600  # 100 ms


def tone(seconds, amp=0.3):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * (np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 540 * t))).astype(np.float32)


def quiet(seconds):
    return np.zeros(int(seconds * SAMPLE_RATE), np.float32)


def pcm(samples):
    return (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()


class Energy:
    def __call__(self, frame):
        return 0.95 if float(np.sqrt(np.mean(frame ** 2))) > 0.05 else 0.02

    def reset(self):
        pass


class FakeWake:
    def __init__(self):
        self.fire = False
        self.fed = 0

    def feed(self, chunk):
        self.fed += 1
        fired, self.fire = self.fire, False
        return fired


class FakeStt:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.heard = []
        self._hint = ""

    def transcribe(self, audio):
        self.heard.append(len(audio) / SAMPLE_RATE)
        return self.texts.pop(0) if self.texts else ""

    def load(self):
        pass


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def userdata(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    # The wake-word flows below are the subject of most of these tests. Conversation style (no wake phrase) is the default now,
    # so they ask for the phrase explicitly, and the conversation tests turn it back off.
    from python.harness import voice_settings

    voice_settings.update({"wake_enabled": True})
    # partial transcripts call the recogniser too; only the test that is about them turns them on
    monkeypatch.setattr("python.harness.voice_runtime.PARTIAL_EVERY_S", 1e9)


class Rig:
    def __init__(self, *texts, halted=False):
        self.events = []
        self.clock = Clock()
        self.wake = FakeWake()
        self.stt = FakeStt(*texts)
        self.halted = halted

        async def notify(method, params):
            self.events.append((method, params))

        async def synth(sentence):
            return {"audio_base64": "QQ==", "mime_type": "audio/wav", "backend": "fake"}

        self.rt = VoiceRuntime(notify, vad=Energy(), wake=self.wake, stt=self.stt, synth=synth, clock=self.clock,
                               halted=lambda: self.halted)
        self.rt.wake_info = {"listening_for": "Hey Tala", "trained_for_phrase": True, "model": "fake"}

    def names(self):
        return [m for m, _ in self.events]

    def last(self, method):
        return [p for m, p in self.events if m == method][-1]

    async def feed(self, samples, wake_at=None):
        data = pcm(samples)
        for i in range(0, len(data), CHUNK * 2):
            self.clock.t += 0.1
            if wake_at is not None and i // (CHUNK * 2) == wake_at:
                self.wake.fire = True
            await self.rt.ingest(data[i:i + CHUNK * 2])
            await asyncio.sleep(0.01)  # real audio arrives over time; let worker threads finish
        await self.settle()

    async def settle(self):
        for _ in range(3):
            await asyncio.sleep(0)
            if self.rt._tasks:
                await asyncio.gather(*list(self.rt._tasks))


@pytest.fixture
def rig():
    return Rig


def run(coro):
    return asyncio.run(coro)


def test_ambient_speech_is_never_transcribed_without_the_wake_word():
    async def go():
        r = Rig("this should never be heard")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.5), tone(1.5), quiet(1.5)]))
        assert r.stt.heard == [] and "voice.turn" not in r.names()
        assert r.rt.session.state is State.WAITING

    run(go())


def test_the_wake_word_mid_utterance_claims_it_and_begins_a_turn_without_the_phrase():
    async def go():
        r = Rig("Hey Tala, what time is it?")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.5), tone(2.0), quiet(1.5)]), wake_at=12)
        assert r.last("voice.turn")["text"] == "what time is it?"
        assert r.rt.session.state is State.THINKING
        assert "voice.wake" in r.names()

    run(go())


def test_the_transcript_is_announced_before_the_turn():
    async def go():
        r = Rig("Hey Tala, what time is it?")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.5), tone(2.0), quiet(1.5)]), wake_at=12)
        names = r.names()
        assert names.index("voice.transcript") < names.index("voice.turn")

    run(go())


def test_the_wake_word_alone_then_a_pause_then_the_command_works():
    async def go():
        r = Rig("Hey Tala.", "what time is it")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(0.8), quiet(1.2)]), wake_at=6)
        assert "voice.turn" not in r.names(), "just the wake phrase is not a command"
        await r.feed(np.concatenate([tone(1.5), quiet(1.5)]))
        assert r.last("voice.turn")["text"] == "what time is it"

    run(go())


def test_partial_transcripts_stream_while_the_user_is_still_talking(monkeypatch):
    monkeypatch.setattr("python.harness.voice_runtime.PARTIAL_EVERY_S", 1.2)

    async def go():
        r = Rig("Hey Tala, tell me", "Hey Tala, tell me about the weather today", "Hey Tala, tell me about the weather today")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(4.0), quiet(1.5)]), wake_at=5)
        partials = [p["text"] for m, p in r.events if m == "voice.partial"]
        assert partials and partials[0].startswith("tell me")

    run(go())


def test_barge_in_stops_her_cancels_the_turn_and_takes_the_interruption_as_the_next_turn():
    async def go():
        r = Rig("Hey Tala, tell me a story", "actually stop")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        first = r.last("voice.turn")["turn_id"]
        stream = r.rt.stream_for(first)
        await r.rt.playback("started", first)
        assert r.rt.session.state is State.SPEAKING

        await r.feed(np.concatenate([quiet(0.2), tone(1.0), quiet(1.5)]))
        assert any(m == "voice.interrupt" for m, _ in r.events)
        assert stream.cancelled is True
        second = r.last("voice.turn")
        assert second["text"] == "actually stop" and second["turn_id"] != first

    run(go())


def test_a_short_noise_while_she_speaks_does_not_interrupt():
    async def go():
        r = Rig("Hey Tala, tell me a story")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        turn = r.last("voice.turn")["turn_id"]
        await r.rt.playback("started", turn)
        await r.feed(np.concatenate([quiet(0.3), tone(0.1), quiet(1.0)]))
        assert not any(m == "voice.interrupt" for m, _ in r.events)
        assert r.rt.session.state is State.SPEAKING

    run(go())


def test_the_user_can_answer_without_the_wake_word_just_after_she_finishes():
    async def go():
        r = Rig("Hey Tala, what time is it", "and tomorrow")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        turn = r.last("voice.turn")["turn_id"]
        await r.rt.playback("started", turn)
        await r.rt.playback("ended", turn)
        await r.feed(np.concatenate([quiet(0.5), tone(1.2), quiet(1.5)]))
        assert r.last("voice.turn")["text"] == "and tomorrow"

    run(go())


def test_dictation_returns_one_transcript_without_a_wake_word_and_stops_listening():
    async def go():
        r = Rig("remind me to call mum")
        await r.rt.start(mode="dictate")
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]))
        transcripts = [p for m, p in r.events if m == "voice.transcript"]
        assert transcripts == [{"text": "remind me to call mum", "final": True, "dictate": True}]
        assert "voice.turn" not in r.names() and r.rt.session.state is State.OFF
        assert r.wake.fed == 0, "dictation does not need the wake word"

    run(go())


def test_the_halt_stops_listening_and_audio_is_not_processed():
    async def go():
        r = Rig("never")
        await r.rt.start()
        r.halted = True
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        assert r.rt.session.state is State.OFF and r.stt.heard == []

    run(go())


def test_it_will_not_start_listening_while_halted():
    async def go():
        r = Rig(halted=True)
        with pytest.raises(PermissionError):
            await r.rt.start()
        assert r.rt.session.state is State.OFF

    run(go())


def test_audio_is_ignored_when_listening_is_off():
    async def go():
        r = Rig("never")
        await r.feed(tone(2.0))
        assert r.stt.heard == [] and r.wake.fed == 0

    run(go())


def test_turn_done_speaks_the_whole_reply_when_nothing_was_streamed():
    async def go():
        r = Rig("Hey Tala, hi")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        turn = r.last("voice.turn")["turn_id"]
        assert r.rt.turn_done(turn, "Hello there. How can I help?") is True
        await asyncio.sleep(0.05)
        chunks = [p for m, p in r.events if m == "voice.audio_chunk"]
        assert [c.get("text") for c in chunks if not c["final"]] == ["Hello there.", "How can I help?"]
        assert chunks[-1]["final"] is True

    run(go())


def test_a_turn_that_makes_no_sound_returns_to_waiting():
    async def go():
        r = Rig("Hey Tala, hi")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        turn = r.last("voice.turn")["turn_id"]
        r.rt.turn_done(turn, None)
        await asyncio.sleep(0.05)
        assert r.rt.session.state is State.WAITING

    run(go())


def test_turn_done_for_a_stale_turn_is_refused():
    async def go():
        r = Rig("Hey Tala, hi")
        await r.rt.start()
        assert r.rt.turn_done("not-a-turn", "text") is False

    run(go())


def test_latency_is_measured_and_logged_when_playback_starts():
    async def go():
        r = Rig("Hey Tala, what time is it")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        turn = r.last("voice.turn")["turn_id"]
        stream = r.rt.stream_for(turn)
        stream.feed_text("It is noon. ")
        await asyncio.sleep(0.05)
        r.clock.t += 0.2
        await r.rt.playback("started", turn)
        latency = r.rt.last_latency
        assert {"wake_to_first_word", "stt_latency", "time_to_first_audio"} <= set(latency)
        assert all(isinstance(v, int) and v >= 0 for v in latency.values())

    run(go())


def test_the_status_says_the_audio_stays_local_and_what_it_listens_for():
    async def go():
        r = Rig()
        await r.rt.start()
        status = r.rt.status()
        assert status["audio_stays_local"] is True and status["listening_for"] == "Hey Tala"

    run(go())


def test_debug_audio_is_not_written_unless_a_debug_directory_is_set(tmp_path, monkeypatch):
    async def go():
        monkeypatch.delenv("TALA_VOICE_DEBUG_DIR", raising=False)
        r = Rig("Hey Tala, hi")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        assert not list(tmp_path.rglob("*.wav"))
        monkeypatch.setenv("TALA_VOICE_DEBUG_DIR", str(tmp_path / "dbg").replace("C:", "D:"))

    run(go())


def test_no_partial_transcription_starts_once_the_speaker_has_gone_quiet(monkeypatch):
    """A partial begun in the trailing silence would hold the recogniser when the final needs it (found by the live smoke:
    transcription took 2.7-4.7 s for a short sentence)."""
    monkeypatch.setattr("python.harness.voice_runtime.PARTIAL_EVERY_S", 0.05)

    async def go():
        r = Rig("Hey Tala, tell me a story")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(2.5)]), wake_at=5)
        speaking = len(r.stt.heard)
        assert speaking >= 1, "partials do run while the speaker is talking"
        await r.feed(quiet(0.2))  # the first 150 ms of quiet may still start one
        settled = len(r.stt.heard)
        assert settled - speaking <= 1
        await r.feed(quiet(0.2))  # the utterance has not ended yet (it needs 500 ms), but the speaker has stopped
        assert len(r.stt.heard) == settled, "none start once the quiet has lasted longer than that"
        assert r.rt.session.state is State.CAPTURING

    run(go())


def test_the_end_of_an_utterance_is_half_a_second_of_silence():
    from python.harness import voice_runtime

    assert voice_runtime.END_OF_SPEECH_MS == 500.0
    assert Rig().rt.segmenter._silence_frames == round(500.0 / 32)


def test_warming_up_never_stops_listening_from_starting():
    async def go():
        r = Rig()
        import python.harness.voice_kokoro as kokoro

        original = kokoro.module_present
        kokoro.module_present = lambda: (_ for _ in ()).throw(RuntimeError("no kokoro"))
        try:
            r.rt._warm_up()  # must swallow the error
        finally:
            kokoro.module_present = original

    run(go())



# --- conversation style ----------------------------------------------------------------------------------------------


def test_in_conversation_style_speech_alone_begins_a_turn_and_the_mic_stays_open():
    from python.harness import voice_settings

    voice_settings.update({"wake_enabled": False})

    async def go():
        r = Rig("what is the weather")
        await r.rt.start()
        assert r.rt.status()["wake_enabled"] is False
        await r.feed(quiet(0.5))
        await r.feed(tone(1.2))
        await r.feed(quiet(1.2))
        assert "voice.turn" in r.names(), r.names()
        assert r.last("voice.turn")["text"] == "what is the weather"
        assert r.rt.session.state is not State.OFF  # still listening: a conversation, not one command
        return r

    run(go())


def test_in_conversation_style_the_wake_detector_is_not_consulted():
    from python.harness import voice_settings

    voice_settings.update({"wake_enabled": False})

    async def go():
        r = Rig("hello")
        await r.rt.start()
        await r.feed(tone(1.0))
        await r.feed(quiet(1.2))
        assert r.wake.fed == 0, "with no phrase required there is nothing to listen for"

    run(go())


def test_the_wake_phrase_is_on_by_default_and_hey_tala_is_the_phrase():
    """She answers after "Hey Tala" out of the box. The no-phrase conversation style is a choice, not the default."""
    from python.harness import voice_settings

    assert voice_settings.DEFAULTS["wake_enabled"] is True and voice_settings.DEFAULTS["wake_phrase"] == "Hey Tala"
    assert voice_settings.DEFAULTS["listen_on_start"] is False, "the microphone is never on without the user having said so"
