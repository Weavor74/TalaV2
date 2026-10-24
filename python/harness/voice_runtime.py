"""
python/harness/voice_runtime.py -- the local voice loop: audio in, turns out, speech back.

The renderer owns the microphone (the browser's echo cancellation is what makes talking over her work on speakers) and
streams 16 kHz PCM to ``voice.audio``. This module is everything behind that call::

    audio -> wake word (openWakeWord) --\\
          -> VAD segmenter (Silero) ------> VoiceSession (the rules) -> actions
                                                |        |        |
                              faster-whisper  <-+        |        +-> cancel turn / stop playback
                              (partials, final)          +-> voice.turn -> the UI sends the text to the model
                                                              voice.audio_chunk <- sentence-chunked Kokoro (voice_stream)

Nothing leaves the machine: every model is loaded from D: with no download path, audio is held in memory only (the current
utterance and a short pre-roll), and raw audio is written to disk only when ``TALA_VOICE_DEBUG_DIR`` is set -- on D:.

Latency is measured, not guessed. Each turn keeps a timeline (wake, end of speech, transcription, first text from the model,
first sentence synthesised, first audio sent, playback started) and logs one line of it when playback starts.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from python.harness import voice_settings
from python.harness.voice_session import (
    BEGIN_TURN, CANCEL_TURN, CAPTURE_START, STOP_PLAYBACK, TRANSCRIBE, TRANSCRIPT, State, VoiceConfig, VoiceSession,
)
from python.harness.voice_stream import TurnStream
from python.harness.voice_stt import SttEngine, strip_wake_phrase
from python.harness.voice_vad import SAMPLE_RATE, Segmenter

log = logging.getLogger(__name__)

Notify = Callable[[str, dict[str, Any]], Awaitable[Any]]

VAD_THRESHOLD = 0.5
#: While she is thinking or speaking the bar is higher, so her own voice leaking into the microphone is less likely to count.
BARGE_IN_THRESHOLD = 0.75
PARTIAL_EVERY_S = 1.2
PARTIAL_MIN_S = 0.8
PARTIAL_MAX_S = 10.0
#: Silence that ends an utterance. Shorter is snappier; longer waits through a speaker's pauses.
END_OF_SPEECH_MS = 500.0
#: A partial started this far into trailing silence would only hold the recogniser when the final needs it.
PARTIAL_QUIET_MS = 150.0

_RUNTIME: "VoiceRuntime | None" = None


def peek() -> "VoiceRuntime | None":
    return _RUNTIME


def get(notify: Notify | None = None) -> "VoiceRuntime":
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = VoiceRuntime(notify or _broadcast)
    return _RUNTIME


async def _broadcast(method: str, params: dict[str, Any]) -> Any:
    from python.harness.server import broadcast_notification

    return await broadcast_notification(method, params)


def _halted() -> bool:
    try:
        from python.harness import halt_gate

        return bool(halt_gate.is_halted_fail_closed())
    except Exception:
        return True  # a kill switch that cannot be read counts as engaged


class VoiceRuntime:
    def __init__(self, notify: Notify, *, vad: Any = None, wake: Any = None, stt: SttEngine | None = None,
                 synth: Any = None, clock: Callable[[], float] = time.monotonic, halted: Callable[[], bool] = _halted):
        self._notify = notify
        self._clock = clock
        self._halted = halted
        self._synth = synth
        self._vad = vad
        self.wake = wake
        self.wake_info: dict[str, Any] = {}
        self.stt = stt or SttEngine()
        self._injected = {"vad": vad is not None, "wake": wake is not None}
        self.settings = voice_settings.read()
        self.session = VoiceSession(self._config(), clock=clock)
        self.segmenter: Segmenter | None = (Segmenter(vad, min_silence_ms=END_OF_SPEECH_MS, on_reset=getattr(vad, "reset", None))
                                            if vad else None)
        self.turn_id: str | None = None
        self._streams: dict[str, TurnStream] = {}
        self._woke_capture = False
        self._wake_pending_until = 0.0
        self._whisper_name = self.settings["whisper_model"]
        self._announced: State | None = None
        self._last_partial = 0.0
        self._partial_busy = False
        self.timeline: dict[str, float] = {}
        self.last_latency: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # --- configuration ----------------------------------------------------------------------------------------------------

    def _config(self) -> VoiceConfig:
        return VoiceConfig(wake_required=bool(self.settings["wake_enabled"]),
                           followup_s=float(self.settings["followup_seconds"]))

    def apply_settings(self) -> None:
        self.settings = voice_settings.read()
        self.session.config = self._config()
        if not self._injected["wake"]:
            self.wake = None
            self.wake_info = {}
        if self.settings["whisper_model"] != self._whisper_name:
            self._whisper_name = self.settings["whisper_model"]
            self.stt = SttEngine()

    async def _ensure_loaded(self, mode: str = "continuous") -> None:
        """Load the models (off the event loop). Missing models raise an error that names the setup script."""
        warm = False
        if self._vad is None:
            from python.harness.voice_vad import SileroVad

            self._vad = await asyncio.to_thread(SileroVad)
            self.segmenter = Segmenter(self._vad, min_silence_ms=END_OF_SPEECH_MS, on_reset=self._vad.reset)
            warm = True
        if self.settings["wake_enabled"] and mode == "continuous" and self.wake is None:
            await asyncio.to_thread(self._load_wake)
        await asyncio.to_thread(self.stt.load)
        if warm:
            await asyncio.to_thread(self._warm_up)

    def _warm_up(self) -> None:
        """Pay the first-use costs now (loading the voice, the recogniser's first pass) rather than on the first turn."""
        try:
            from python.harness import voice_kokoro

            voice = voice_settings.read()["tts_voice"]
            if voice_kokoro.module_present() and voice_kokoro.installed(voice):
                voice_kokoro.synthesize_wav("Ready.", voice)
            self.stt.transcribe(np.zeros(SAMPLE_RATE, np.float32))
        except Exception as exc:  # warming is an optimisation; never a reason not to listen
            log.warning("voice: warm-up failed: %s", exc)

    def _load_wake(self) -> None:
        from python.harness.voice_wake import WakeDetector, choose_model, model_threshold

        wake_dir = voice_settings.models_dir() / "wakeword"
        chosen = choose_model(wake_dir, self.settings)
        if chosen is None:
            raise RuntimeError(f"no wake-word model in {wake_dir}; run scripts/setup_voice.py")
        explicit = self.settings["wake_threshold"]
        threshold = float(explicit) if explicit is not None else (model_threshold(chosen.path) or 0.5)
        self.wake = WakeDetector.load(wake_dir, chosen, threshold)
        self.wake_info = {"model": chosen.path.name, "listening_for": chosen.phrase,
                          "trained_for_phrase": chosen.trained_for_phrase, "threshold": threshold}

    # --- control ------------------------------------------------------------------------------------------------------------

    async def start(self, mode: str = "continuous") -> dict[str, Any]:
        if self._halted():
            raise PermissionError("the emergency halt is engaged; voice listening is off")
        self.apply_settings()
        await self._ensure_loaded(mode)
        self.segmenter.reset()
        self.session.start(mode)
        self._woke_capture = False
        await self._announce(force=True)
        return self.status()

    async def stop(self) -> dict[str, Any]:
        actions = self.session.stop()
        await self._apply(actions)
        if self.segmenter:
            self.segmenter.reset()
        await self._announce(force=True)
        return self.status()

    def status(self) -> dict[str, Any]:
        s = self.settings
        return {
            "state": self.session.state.value, "listening": self.session.state is not State.OFF,
            "mode": self.session.mode, "wake_enabled": bool(s["wake_enabled"]),
            "wake_phrase": s["wake_phrase"], **self.wake_info, "turn_id": self.turn_id,
            "last_latency_ms": dict(self.last_latency), "audio_stays_local": True,
        }

    async def _announce(self, force: bool = False) -> None:
        if force or self.session.state is not self._announced:
            self._announced = self.session.state
            await self._notify("voice.state", self.status())

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- audio in -------------------------------------------------------------------------------------------------------------

    async def ingest(self, pcm: bytes) -> dict[str, Any]:
        """One chunk of 16 kHz mono PCM16 from the microphone. Returns the state, which is the UI's back-channel."""
        if self.session.state is State.OFF or self.segmenter is None:
            return self.status()
        if self._halted():
            await self.stop()
            return self.status()
        self.segmenter.threshold = (BARGE_IN_THRESHOLD if self.session.state in (State.THINKING, State.SPEAKING)
                                    else VAD_THRESHOLD)
        woke = bool(self.wake and self.session.config.wake_required and self.session.mode == "continuous"
                    and self.session.state is State.WAITING and self.wake.feed(pcm))
        for event in self.segmenter.feed(pcm):
            await self._on_segment(event)
        if woke:
            await self._on_wake()
        await self._apply(self.session.tick(), forced_audio=True)
        self._maybe_partial()
        await self._announce()
        return self.status()

    async def _on_wake(self) -> None:
        self.timeline["wake"] = self._clock()
        self._wake_pending_until = self._clock() + self.session.config.wake_window_s
        actions = self.session.on_wake(speech_active=self.segmenter.in_speech)
        await self._notify("voice.wake", {"phrase": self.wake_info.get("listening_for", self.settings["wake_phrase"])})
        await self._apply(actions)

    async def _on_segment(self, event) -> None:
        if event.kind == "start":
            await self._apply(self.session.on_speech_start())
        elif event.kind == "progress":
            await self._apply(self.session.on_speech_progress(event.speech_ms))
        elif event.kind == "end":
            self.timeline["speech_end"] = self._clock()
            await self._apply(self.session.on_speech_end(), audio=event.audio)

    # --- acting on the session's decisions ---------------------------------------------------------------------------------------

    async def _apply(self, actions, audio: np.ndarray | None = None, forced_audio: bool = False) -> None:
        for kind, data in actions:
            if kind == CAPTURE_START:
                self._woke_capture = self._clock() < self._wake_pending_until
                self._wake_pending_until = 0.0
                self._last_partial = self._clock()
                await self._announce()
            elif kind == TRANSCRIBE:
                if audio is None and forced_audio and self.segmenter is not None:
                    audio = self.segmenter.current_audio()
                    self.segmenter.reset()
                self._spawn(self._transcribe(audio if audio is not None else np.zeros(0, np.float32), self._woke_capture))
            elif kind == STOP_PLAYBACK:
                await self._notify("voice.interrupt", {"turn_id": self.turn_id})
            elif kind == CANCEL_TURN:
                self._cancel_turn()
            elif kind == BEGIN_TURN:
                await self._begin_turn(data["text"])
            elif kind == TRANSCRIPT:
                await self._notify("voice.transcript", {"text": data["text"], "final": True, "dictate": True})

    async def _transcribe(self, audio: np.ndarray, woke: bool) -> None:
        self._debug_save(audio, "utterance")
        self.timeline["stt_start"] = self._clock()
        text = await asyncio.to_thread(self.stt.transcribe, audio)
        self.timeline["stt_done"] = self._clock()
        if woke:
            text = strip_wake_phrase(text, self.wake_info.get("listening_for", self.settings["wake_phrase"]))
        if text and self.session.mode != "dictate":  # dictation is announced once, by the session's TRANSCRIPT action
            await self._notify("voice.transcript", {"text": text, "final": True, "dictate": False})
        actions = self.session.on_transcript(text)
        if not text and woke:
            # "Hey Tala." and then a pause: the command is coming, so keep the window open for it
            actions += self.session.on_wake(speech_active=False)
        await self._apply(actions)
        await self._announce()

    def _maybe_partial(self) -> None:
        if (self.session.state is not State.CAPTURING or self._partial_busy or self.segmenter is None
                or self._clock() - self._last_partial < PARTIAL_EVERY_S
                or self.segmenter.trailing_silence_ms > PARTIAL_QUIET_MS):
            return
        audio = self.segmenter.current_audio()
        seconds = len(audio) / SAMPLE_RATE
        if not PARTIAL_MIN_S <= seconds <= PARTIAL_MAX_S:
            return
        self._last_partial = self._clock()
        self._partial_busy = True
        self._spawn(self._partial(audio, self._woke_capture))

    async def _partial(self, audio: np.ndarray, woke: bool) -> None:
        try:
            text = await asyncio.to_thread(self.stt.transcribe, audio)
            if woke:
                text = strip_wake_phrase(text, self.wake_info.get("listening_for", self.settings["wake_phrase"]))
            if text and self.session.state is State.CAPTURING:
                await self._notify("voice.partial", {"text": text})
        finally:
            self._partial_busy = False

    # --- turns and playback ----------------------------------------------------------------------------------------------------------

    async def _begin_turn(self, text: str) -> None:
        self._cancel_turn()
        self.turn_id = uuid.uuid4().hex[:12]
        self.timeline["turn_begin"] = self._clock()
        self._streams[self.turn_id] = TurnStream(
            self.turn_id, self._notify, synth=self._synth,
            on_first_text=lambda t: self.timeline.setdefault("first_text", t),
            on_first_audio=self._first_audio,
            on_idle=lambda: self.session.on_turn_finished(), clock=self._clock,
        )
        await self._notify("voice.turn", {"turn_id": self.turn_id, "text": text})

    def _first_audio(self, at: float, sentence_tts_s: float) -> None:
        self.timeline["first_audio"] = at
        self.timeline["first_sentence_tts"] = sentence_tts_s

    def _cancel_turn(self) -> None:
        for stream in self._streams.values():
            stream.cancel()
        self._streams.clear()

    def stream_for(self, turn_id: str) -> TurnStream | None:
        return self._streams.get(turn_id) if turn_id == self.turn_id else None

    def turn_done(self, turn_id: str, text: str | None = None) -> bool:
        """The model finished (or failed) this turn. With ``text`` and no streamed speech, speak the whole reply."""
        stream = self.stream_for(turn_id)
        if stream is None:
            return False
        if not stream.text_seen and text:
            stream.feed_text(text)
        stream.finish()
        return True

    async def playback(self, state: str, turn_id: str | None) -> dict[str, Any]:
        if turn_id and turn_id != self.turn_id:
            return self.status()
        if state == "started":
            self.timeline["playback_start"] = self._clock()
            self.session.on_playback_started()
            self._log_latency()
        elif state == "ended":
            self.session.on_playback_ended()
        await self._announce()
        return self.status()

    async def interrupt(self) -> dict[str, Any]:
        """A deliberate "stop talking" from the UI (not barge-in): cancel the turn and silence playback."""
        self._cancel_turn()
        await self._notify("voice.interrupt", {"turn_id": self.turn_id})
        self.session.on_playback_ended()
        self.session.on_turn_finished()
        await self._announce()
        return self.status()

    def _log_latency(self) -> None:
        t = self.timeline
        ms = lambda a, b: round((t[b] - t[a]) * 1000) if a in t and b in t else None  # noqa: E731
        self.last_latency = {k: v for k, v in {
            "wake_to_first_audio": ms("wake", "first_audio"),
            "wake_to_playback": ms("wake", "playback_start"),
            "end_of_speech_to_first_audio": ms("speech_end", "first_audio"),
            "stt": ms("stt_start", "stt_done"),
            "model_first_text": ms("turn_begin", "first_text"),
            "first_sentence_tts": round(t["first_sentence_tts"] * 1000) if "first_sentence_tts" in t else None,
            "first_audio_to_playback": ms("first_audio", "playback_start"),
        }.items() if v is not None}
        log.info("voice latency (ms): %s", self.last_latency)
        self.timeline = {}

    # --- debug --------------------------------------------------------------------------------------------------------------------------

    def _debug_save(self, audio: np.ndarray, label: str) -> None:
        directory = voice_settings.debug_dir()
        if directory is None or len(audio) == 0:
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = Path(directory) / f"{label}-{int(time.time() * 1000)}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        except OSError as exc:
            log.warning("voice: could not write debug audio: %s", exc)
