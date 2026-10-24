"""
Voice-activity segmentation: where an utterance starts and ends in a stream of microphone audio.

The segmenter turns a stream of 16 kHz samples (arriving in whatever chunk sizes the transport delivers) into events:
``start`` when speech has been sustained long enough to not be a click, ``progress`` while it continues (barge-in reads
this), and ``end`` carrying the utterance audio once enough silence has passed. The probability function is injected, so
these tests run on a fixture WAV with an energy detector and need no model; tests/test_voice_models.py runs the same
segmenter with the real Silero model.
"""
from __future__ import annotations

import wave

import numpy as np
import pytest

from python.harness.voice_vad import SAMPLE_RATE, Segmenter, SegEvent

SR = SAMPLE_RATE


def speechlike(seconds, amp=0.3):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * (np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 540 * t))).astype(np.float32)


def silence(seconds):
    return np.zeros(int(seconds * SR), np.float32)


def energy_prob(frame):
    return 0.95 if float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))) > 0.05 else 0.02


def write_wav(path, samples):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


@pytest.fixture
def two_utterances_wav(tmp_path):
    audio = np.concatenate([silence(1.0), speechlike(1.5), silence(1.0), speechlike(0.8), silence(1.5)])
    path = tmp_path / "two.wav"
    write_wav(path, audio)
    return path


def run(segmenter, samples, chunk=1600):
    events: list[SegEvent] = []
    for i in range(0, len(samples), chunk):
        events += segmenter.feed(samples[i:i + chunk])
    return events


def make(**kw):
    return Segmenter(energy_prob, **kw)


def kinds(events):
    return [e.kind for e in events]


def test_a_fixture_wav_yields_two_utterances_in_order(two_utterances_wav):
    events = run(make(), read_wav(two_utterances_wav))
    assert kinds([e for e in events if e.kind != "progress"]) == ["start", "end", "start", "end"]
    ends = [e for e in events if e.kind == "end"]
    assert 1.4 < len(ends[0].audio) / SR < 2.6
    assert 0.7 < len(ends[1].audio) / SR < 1.9


def test_the_utterance_audio_carries_the_speech_and_not_only_silence(two_utterances_wav):
    ends = [e for e in run(make(), read_wav(two_utterances_wav)) if e.kind == "end"]
    for end in ends:
        assert float(np.sqrt(np.mean(end.audio ** 2))) > 0.1


def test_chunk_size_does_not_change_the_result(two_utterances_wav):
    samples = read_wav(two_utterances_wav)
    reference = [e.kind for e in run(make(), samples, chunk=1600) if e.kind != "progress"]
    for chunk in (160, 513, 4097):
        assert [e.kind for e in run(make(), samples, chunk=chunk) if e.kind != "progress"] == reference


def test_pcm16_bytes_are_accepted_as_the_transport_delivers_them(two_utterances_wav):
    samples = read_wav(two_utterances_wav)
    as_bytes = (samples * 32767).astype(np.int16).tobytes()
    seg = make()
    events = []
    for i in range(0, len(as_bytes), 3200):
        events += seg.feed(as_bytes[i:i + 3200])
    assert kinds([e for e in events if e.kind != "progress"]) == ["start", "end", "start", "end"]


def test_a_click_is_not_speech():
    events = run(make(), np.concatenate([silence(0.5), speechlike(0.04), silence(1.5)]))
    assert kinds(events) == []


def test_silence_alone_produces_nothing():
    assert run(make(), silence(5.0)) == []


def test_a_brief_pause_inside_an_utterance_does_not_split_it():
    audio = np.concatenate([silence(0.5), speechlike(1.0), silence(0.3), speechlike(1.0), silence(1.5)])
    events = run(make(min_silence_ms=600), audio)
    assert kinds([e for e in events if e.kind != "progress"]) == ["start", "end"]


def test_a_longer_pause_does_split_it():
    audio = np.concatenate([silence(0.5), speechlike(1.0), silence(1.0), speechlike(1.0), silence(1.5)])
    events = run(make(min_silence_ms=600), audio)
    assert kinds([e for e in events if e.kind != "progress"]) == ["start", "end", "start", "end"]


def test_the_start_of_the_word_is_not_clipped():
    """Speech is only recognised after start_ms, so the audio before that must still be in the utterance."""
    audio = np.concatenate([silence(1.0), speechlike(1.0), silence(1.5)])
    end = [e for e in run(make(preroll_ms=500), audio) if e.kind == "end"][0]
    assert len(end.audio) / SR > 1.0, "the full second of speech plus pre-roll and trailing silence"


def test_progress_grows_while_speech_continues():
    events = run(make(), np.concatenate([silence(0.5), speechlike(1.2), silence(1.5)]))
    progress = [e.speech_ms for e in events if e.kind == "progress"]
    assert progress == sorted(progress) and progress[-1] >= 1000


def test_an_utterance_is_cut_at_the_maximum_length():
    events = run(make(max_segment_s=2.0), np.concatenate([silence(0.3), speechlike(5.0), silence(1.5)]))
    ends = [e for e in events if e.kind == "end"]
    assert ends and len(ends[0].audio) / SR <= 2.5


def test_the_current_segment_can_be_claimed_while_it_is_still_in_progress():
    seg = make()
    run(seg, np.concatenate([silence(0.5), speechlike(0.8)]))
    assert seg.in_speech
    claimed = seg.current_audio()
    assert len(claimed) / SR > 0.7


def test_threshold_can_be_raised_while_she_is_speaking():
    """The runtime raises it so her own voice leaking into the microphone is less likely to count as the user."""
    quiet = speechlike(1.0, amp=0.06)

    def graded(frame):
        return float(min(1.0, np.sqrt(np.mean(frame.astype(np.float32) ** 2)) * 8))

    seg = Segmenter(graded, threshold=0.3)
    assert kinds(run(seg, np.concatenate([silence(0.3), quiet, silence(1.5)]))) != []
    strict = Segmenter(graded, threshold=0.9)
    assert kinds(run(strict, np.concatenate([silence(0.3), quiet, silence(1.5)]))) == []


def test_reset_clears_a_half_finished_utterance():
    seg = make()
    run(seg, np.concatenate([silence(0.5), speechlike(0.8)]))
    seg.reset()
    assert not seg.in_speech and run(seg, silence(2.0)) == []


def test_trailing_silence_is_tracked_so_the_caller_can_tell_an_utterance_is_ending():
    seg = make()
    run(seg, np.concatenate([silence(0.3), speechlike(1.0)]))
    assert seg.in_speech and seg.trailing_silence_ms == 0
    run(seg, silence(0.3))
    assert 200 < seg.trailing_silence_ms < 400
    run(seg, speechlike(0.2))
    assert seg.trailing_silence_ms == 0


def test_no_trailing_silence_is_reported_when_nothing_is_in_progress():
    assert make().trailing_silence_ms == 0
