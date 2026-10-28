"""
python/harness/voice_session.py -- the rules of a voice conversation, with no audio in them.

The microphone side feeds this events (speech started / progressed / ended, the wake word fired, a transcript arrived,
playback started / ended) and it answers with actions for the caller to carry out (start capturing, transcribe, begin a
turn, stop playback, cancel the turn). It owns no thread, no model and no clock of its own, so every rule here is
checked in tests/test_voice_session.py without a microphone.

States::

    OFF            listening is not on; every event is ignored
    WAITING        listening, nobody has been addressed: speech is ignored unless the wake window or the follow-up
                   window is open (or the wake word is switched off)
    CAPTURING      an utterance is being recorded
    TRANSCRIBING   the utterance ended and is being turned into text
    THINKING       a turn is running and nothing has been said yet
    SPEAKING       her audio is playing

Barge-in: user speech that lasts ``barge_in_ms`` while she is THINKING or SPEAKING stops playback, cancels the turn
and starts capturing the interruption as the next utterance. A shorter sound (a cough, a click) does nothing.

Follow-up: when she finishes speaking the follow-up window opens for ``followup_s``, so the user can answer without the
wake word. It closes on its own; ambient speech after that is ignored again.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

CAPTURE_START = "capture_start"
TRANSCRIBE = "transcribe"
BEGIN_TURN = "begin_turn"
TRANSCRIPT = "transcript"
STOP_PLAYBACK = "stop_playback"
CANCEL_TURN = "cancel_turn"

Action = tuple[str, dict[str, Any]]


class State(str, Enum):
    OFF = "off"
    WAITING = "waiting"
    CAPTURING = "capturing"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass(frozen=True)
class VoiceConfig:
    wake_required: bool = True
    wake_window_s: float = 8.0
    followup_s: float = 6.0
    barge_in_ms: float = 250.0
    #: Conversation style (no wake phrase): speech this soon after she stops is her own voice still in the room, not you.
    echo_guard_s: float = 0.6
    max_utterance_s: float = 30.0


class VoiceSession:
    def __init__(self, config: VoiceConfig | None = None, clock: Callable[[], float] = time.monotonic):
        self.config = config or VoiceConfig()
        self._clock = clock
        self.state = State.OFF
        self.mode = "continuous"
        self._engaged_until = 0.0
        self._deaf_until = 0.0
        self._capture_started = 0.0

    # --- helpers ---------------------------------------------------------------------------------------------

    def _engaged(self) -> bool:
        return (not self.config.wake_required) or self.mode == "dictate" or self._clock() < self._engaged_until

    def _to_waiting(self, followup: bool = False) -> None:
        self.state = State.WAITING
        if followup:
            self._engaged_until = self._clock() + self.config.followup_s

    def _begin_capture(self, from_segment_start: bool = True) -> list[Action]:
        self.state = State.CAPTURING
        self._capture_started = self._clock()
        return [(CAPTURE_START, {"from_segment_start": from_segment_start})]

    # --- control ---------------------------------------------------------------------------------------------

    def start(self, mode: str = "continuous") -> list[Action]:
        """Turn listening on. ``dictate`` captures one utterance with no wake word and then turns itself off."""
        self.mode = mode
        self._engaged_until = self._clock() + self.config.wake_window_s if mode == "dictate" else 0.0
        self._deaf_until = 0.0
        self.state = State.WAITING
        return []

    def stop(self) -> list[Action]:
        actions: list[Action] = []
        if self.state in (State.THINKING, State.SPEAKING):
            actions = [(STOP_PLAYBACK, {}), (CANCEL_TURN, {})]
        self.state = State.OFF
        self._engaged_until = 0.0
        return actions

    # --- microphone events -------------------------------------------------------------------------------------------

    def on_wake(self, speech_active: bool = False) -> list[Action]:
        if self.state is not State.WAITING:
            return []
        if speech_active:
            return self._begin_capture(from_segment_start=True)
        self._engaged_until = self._clock() + self.config.wake_window_s
        return []

    def on_speech_start(self) -> list[Action]:
        if self.state is State.WAITING and self._engaged() and self._clock() >= self._deaf_until:
            return self._begin_capture(from_segment_start=True)
        return []

    def on_speech_progress(self, speech_ms: float) -> list[Action]:
        """Called as an ongoing stretch of speech grows. Only matters while she is thinking or speaking."""
        if self.state in (State.THINKING, State.SPEAKING) and speech_ms >= self.config.barge_in_ms:
            return [(STOP_PLAYBACK, {}), (CANCEL_TURN, {}), *self._begin_capture(from_segment_start=True)]
        return []

    def on_speech_end(self) -> list[Action]:
        if self.state is State.CAPTURING:
            self.state = State.TRANSCRIBING
            return [(TRANSCRIBE, {})]
        return []

    # --- results and playback ------------------------------------------------------------------------------------------

    def on_transcript(self, text: str) -> list[Action]:
        if self.state is not State.TRANSCRIBING:
            return []
        clean = (text or "").strip()
        if self.mode == "dictate":
            self.state = State.OFF
            return [(TRANSCRIPT, {"text": clean})] if clean else []
        if not clean:
            self._to_waiting()
            return []
        self.state = State.THINKING
        return [(BEGIN_TURN, {"text": clean})]

    def on_playback_started(self) -> list[Action]:
        if self.state is State.THINKING:
            self.state = State.SPEAKING
        return []

    def on_playback_ended(self) -> list[Action]:
        if self.state is State.SPEAKING:
            self._to_waiting(followup=True)
            if not self.config.wake_required:
                self._deaf_until = self._clock() + self.config.echo_guard_s
        return []

    def on_turn_finished(self) -> list[Action]:
        """The turn produced no audio (speech off, or nothing to say)."""
        if self.state is State.THINKING:
            self._to_waiting(followup=True)
        return []

    def tick(self) -> list[Action]:
        """Timeouts. Called regularly by the audio loop."""
        now = self._clock()
        if self.state is State.CAPTURING and now - self._capture_started > self.config.max_utterance_s:
            self.state = State.TRANSCRIBING
            return [(TRANSCRIBE, {})]
        if self.state is State.WAITING and self.mode == "dictate" and now >= self._engaged_until:
            self.state = State.OFF
        return []
