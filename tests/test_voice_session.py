"""
The voice session state machine: wake gating, utterance capture, barge-in, follow-up window.

Pure logic with an injected clock, so every rule is checked without a microphone. The rules:

* listening off means nothing is captured, ever;
* with the wake word required, speech does nothing until the wake word fires, and ambient speech is never captured;
* a wake word spoken mid-utterance claims that utterance (the command usually follows it in the same breath);
* after she has spoken, a follow-up window lets the user answer without saying the wake word again, then it closes;
* user speech while she is thinking or speaking, sustained long enough to not be a cough, stops her and starts a new turn;
* push-to-talk (dictate) captures one utterance without a wake word and then stops listening.
"""
from __future__ import annotations

import pytest

from python.harness.voice_session import (
    BEGIN_TURN, CANCEL_TURN, CAPTURE_START, STOP_PLAYBACK, TRANSCRIBE, TRANSCRIPT, State, VoiceConfig, VoiceSession,
)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


def make(clock, **config):
    return VoiceSession(VoiceConfig(**config), clock=clock)


def kinds(actions):
    return [a[0] for a in actions]


# --- off, and wake gating ---------------------------------------------------------------------------------------------


def test_nothing_is_captured_while_listening_is_off(clock):
    s = make(clock)
    assert s.state is State.OFF
    assert s.on_speech_start() == [] and s.on_wake(speech_active=True) == [] and s.on_speech_end() == []
    assert s.state is State.OFF


def test_ambient_speech_is_ignored_until_the_wake_word(clock):
    s = make(clock)
    s.start()
    assert s.state is State.WAITING
    assert s.on_speech_start() == []
    assert s.on_speech_end() == []
    assert s.state is State.WAITING, "speech without the wake word is never captured"


def test_a_wake_word_mid_utterance_claims_that_utterance(clock):
    s = make(clock)
    s.start()
    s.on_speech_start()
    actions = s.on_wake(speech_active=True)
    assert kinds(actions) == [CAPTURE_START] and actions[0][1] == {"from_segment_start": True}
    assert s.state is State.CAPTURING
    assert kinds(s.on_speech_end()) == [TRANSCRIBE] and s.state is State.TRANSCRIBING


def test_a_wake_word_opens_a_window_for_the_command_that_follows(clock):
    s = make(clock, wake_window_s=8.0)
    s.start()
    assert s.on_wake(speech_active=False) == []
    clock.advance(1.0)
    assert kinds(s.on_speech_start()) == [CAPTURE_START]
    assert s.state is State.CAPTURING


def test_the_wake_window_closes_when_nobody_speaks(clock):
    s = make(clock, wake_window_s=8.0)
    s.start()
    s.on_wake(speech_active=False)
    clock.advance(8.5)
    s.tick()
    assert s.state is State.WAITING
    assert s.on_speech_start() == [], "the window is closed: this is ambient speech again"


def test_without_the_wake_requirement_any_speech_is_a_command(clock):
    s = make(clock, wake_required=False)
    s.start()
    assert kinds(s.on_speech_start()) == [CAPTURE_START]


# --- the turn --------------------------------------------------------------------------------------------------------------


def run_to_thinking(s, text="what time is it"):
    s.on_speech_start()
    s.on_wake(speech_active=True)
    s.on_speech_end()
    return s.on_transcript(text)


def test_a_transcript_begins_a_turn(clock):
    s = make(clock)
    s.start()
    actions = run_to_thinking(s)
    assert actions == [(BEGIN_TURN, {"text": "what time is it"})]
    assert s.state is State.THINKING


def test_an_empty_transcript_begins_no_turn(clock):
    s = make(clock)
    s.start()
    assert run_to_thinking(s, "   ") == []
    assert s.state is State.WAITING


def test_playback_moves_thinking_to_speaking_and_back(clock):
    s = make(clock)
    s.start()
    run_to_thinking(s)
    s.on_playback_started()
    assert s.state is State.SPEAKING
    s.on_playback_ended()
    assert s.state is State.WAITING


def test_a_turn_that_makes_no_sound_returns_to_waiting(clock):
    s = make(clock)
    s.start()
    run_to_thinking(s)
    s.on_turn_finished()
    assert s.state is State.WAITING


# --- follow-up window --------------------------------------------------------------------------------------------------------


def test_the_user_can_answer_without_the_wake_word_right_after_she_speaks(clock):
    s = make(clock, followup_s=6.0)
    s.start()
    run_to_thinking(s)
    s.on_playback_started()
    s.on_playback_ended()
    clock.advance(3.0)
    assert kinds(s.on_speech_start()) == [CAPTURE_START]


def test_the_follow_up_window_closes(clock):
    s = make(clock, followup_s=6.0)
    s.start()
    run_to_thinking(s)
    s.on_playback_started()
    s.on_playback_ended()
    clock.advance(7.0)
    assert s.on_speech_start() == []


# --- barge-in --------------------------------------------------------------------------------------------------------------


def speaking(clock, **config):
    s = make(clock, **config)
    s.start()
    run_to_thinking(s)
    s.on_playback_started()
    return s


def test_sustained_user_speech_while_she_speaks_stops_her_and_starts_a_new_turn(clock):
    s = speaking(clock, barge_in_ms=250)
    actions = s.on_speech_progress(speech_ms=300)
    assert kinds(actions) == [STOP_PLAYBACK, CANCEL_TURN, CAPTURE_START]
    assert actions[2][1] == {"from_segment_start": True}
    assert s.state is State.CAPTURING


def test_a_short_noise_while_she_speaks_does_not_interrupt_her(clock):
    s = speaking(clock, barge_in_ms=250)
    assert s.on_speech_progress(speech_ms=120) == []
    assert s.state is State.SPEAKING


def test_barge_in_while_she_is_still_thinking_cancels_the_turn(clock):
    s = make(clock, barge_in_ms=250)
    s.start()
    run_to_thinking(s)
    actions = s.on_speech_progress(speech_ms=400)
    assert CANCEL_TURN in kinds(actions) and STOP_PLAYBACK in kinds(actions)
    assert s.state is State.CAPTURING


def test_the_interrupting_utterance_becomes_the_next_turn(clock):
    s = speaking(clock)
    s.on_speech_progress(speech_ms=300)
    assert kinds(s.on_speech_end()) == [TRANSCRIBE]
    assert s.on_transcript("actually, never mind that") == [(BEGIN_TURN, {"text": "actually, never mind that"})]


def test_speech_progress_means_nothing_when_she_is_silent(clock):
    s = make(clock)
    s.start()
    assert s.on_speech_progress(speech_ms=5000) == []


# --- push-to-talk ---------------------------------------------------------------------------------------------------------


def test_dictation_captures_one_utterance_without_a_wake_word_then_stops(clock):
    s = make(clock)
    actions = s.start(mode="dictate")
    assert s.state is State.WAITING and actions == []
    assert kinds(s.on_speech_start()) == [CAPTURE_START]
    assert kinds(s.on_speech_end()) == [TRANSCRIBE]
    assert s.on_transcript("remind me to call mum") == [(TRANSCRIPT, {"text": "remind me to call mum"})]
    assert s.state is State.OFF, "dictation stops listening after one utterance"


def test_dictation_gives_up_if_nobody_speaks(clock):
    s = make(clock, wake_window_s=8.0)
    s.start(mode="dictate")
    clock.advance(9.0)
    s.tick()
    assert s.state is State.OFF


# --- limits and stop --------------------------------------------------------------------------------------------------------


def test_an_utterance_cannot_run_forever(clock):
    s = make(clock, max_utterance_s=30.0)
    s.start()
    s.on_speech_start()
    s.on_wake(speech_active=True)
    clock.advance(31.0)
    assert kinds(s.tick()) == [TRANSCRIBE]
    assert s.state is State.TRANSCRIBING


def test_stopping_listening_stops_playback_and_the_turn(clock):
    s = speaking(clock)
    actions = s.stop()
    assert STOP_PLAYBACK in kinds(actions) and CANCEL_TURN in kinds(actions)
    assert s.state is State.OFF


def test_stopping_an_idle_session_does_nothing_audible(clock):
    s = make(clock)
    s.start()
    assert s.stop() == [] and s.state is State.OFF


# --- conversation style: no wake phrase, so her own voice must not become the next utterance -------------------------------


def converse(clock, **config):
    """A session with no wake requirement, one turn into her reply."""
    s = make(clock, wake_required=False, **config)
    s.start()
    s.on_speech_start()
    s.on_speech_end()
    s.on_transcript("what is the weather")
    s.on_playback_started()
    assert s.state is State.SPEAKING
    return s


def test_every_utterance_is_a_turn_in_conversation_style_and_the_mic_stays_open(clock):
    s = converse(clock)
    s.on_playback_ended()
    clock.advance(5.0)  # a long pause; there is no follow-up window to expire
    assert kinds(s.on_speech_start()) == [CAPTURE_START]


def test_speech_in_the_instant_after_she_stops_is_her_own_echo(clock):
    s = converse(clock)
    s.on_playback_ended()
    clock.advance(0.1)
    assert s.on_speech_start() == [], "her own voice, still ringing in the room, is not a new question"
    assert s.state is State.WAITING


def test_speech_after_the_echo_guard_is_heard(clock):
    s = converse(clock, echo_guard_s=0.5)
    s.on_playback_ended()
    clock.advance(0.6)
    assert kinds(s.on_speech_start()) == [CAPTURE_START]


def test_talking_over_her_still_interrupts_her(clock):
    s = converse(clock)
    assert kinds(s.on_speech_progress(400)) == [STOP_PLAYBACK, CANCEL_TURN, CAPTURE_START]


def test_the_echo_guard_does_not_change_how_the_wake_word_flow_works(clock):
    s = make(clock)  # wake required, as before
    s.start()
    run_to_thinking(s)
    s.on_playback_started()
    s.on_playback_ended()
    clock.advance(0.1)
    assert kinds(s.on_speech_start()) == [CAPTURE_START], "the follow-up window opens at once; unchanged"
