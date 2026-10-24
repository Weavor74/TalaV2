"""
python/harness/voice_stream.py -- speak a reply while it is still being written.

``inference.generate`` with ``stream: true`` already pushes ``inference.generate.chunk`` notifications through the client's
websocket as the model produces text. For a voice turn (the request carries the ``voice_turn_id`` the harness issued),
``handle_inference_generate`` wraps that websocket in :class:`TapWs`. Every chunk is forwarded to the UI untouched and also
fed to a :class:`~python.harness.voice_chunker.SentenceChunker`; each sentence the moment it completes is synthesised by
Piper and pushed as a ``voice.audio_chunk`` notification, so the first sentence plays while the rest is generated.

Barge-in reaches back the other way: when the user talks over her, :meth:`TurnStream.cancel` marks the turn cancelled. The
stream loop checks ``ws.voice_cancelled`` and stops reading, which closes the connection to the model so it stops
generating, and no further audio is produced for that turn.

Everything here is local: the text goes from the model to Piper in-process, and the audio goes to the UI on this machine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from python.harness.voice_chunker import SentenceChunker

log = logging.getLogger(__name__)

Notify = Callable[[str, dict[str, Any]], Awaitable[Any]]

#: The message a cancelled generation's task is cancelled with; the dispatcher answers that request instead of leaving it hanging.
CANCEL_REASON = "voice-barge-in"
Synth = Callable[[str], Awaitable[dict[str, Any]]]


async def _default_synth(sentence: str) -> dict[str, Any]:
    from python.harness.voice import synthesize_speech

    return await synthesize_speech(sentence)


class TurnStream:
    """One spoken reply: text in as it is generated, ordered audio chunks out."""

    def __init__(self, turn_id: str, notify: Notify, synth: Synth | None = None,
                 on_first_text: Callable[[float], None] | None = None,
                 on_first_audio: Callable[[float, float], None] | None = None,
                 on_idle: Callable[[], None] | None = None, clock: Callable[[], float] = time.monotonic):
        self.turn_id = turn_id
        self._clock = clock
        self._notify = notify
        self._synth = synth or _default_synth
        self._on_first_text = on_first_text
        self._on_first_audio = on_first_audio
        self._on_idle = on_idle
        self._chunker = SentenceChunker()
        self._queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self.cancelled = False
        #: The task generating this reply. Cancelling it stops a model that is still reading its prompt and has streamed
        #: nothing the stream loop could check, which otherwise left the next turn queued behind it for a minute.
        self.task: asyncio.Task | None = None
        self.text_seen = False
        self.sentences = 0
        self._seq = 0
        self._first_sentence_at: float | None = None
        self._first_audio_sent = False
        self._finished = False

    # --- input ---------------------------------------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None and not self.cancelled:
            self._worker = asyncio.ensure_future(self._run())

    def feed_text(self, text: str) -> None:
        if self.cancelled or not text:
            return
        if not self.text_seen and self._on_first_text:
            self._on_first_text(self._clock())
        self.text_seen = True
        self._enqueue(self._chunker.feed(text))

    def flush_text(self) -> None:
        """A generation round ended: speak whatever sentence is still open."""
        if not self.cancelled:
            self._enqueue(self._chunker.flush())

    def _enqueue(self, sentences: list[str]) -> None:
        for sentence in sentences:
            self.sentences += 1
            if self._first_sentence_at is None:
                self._first_sentence_at = self._clock()
            self._queue.put_nowait(("say", sentence))
        if sentences:
            self._ensure_worker()

    def finish(self) -> None:
        """The turn is over: tell the UI the last chunk has been sent (idempotent)."""
        if self._finished or self.cancelled:
            return
        self._finished = True
        self.flush_text()
        self._queue.put_nowait(("final", None))
        self._ensure_worker()

    def cancel(self) -> None:
        self.cancelled = True
        while not self._queue.empty():
            self._queue.get_nowait()
        if self._worker and not self._worker.done():
            self._worker.cancel()
        if self.task is not None and not self.task.done() and self.task is not asyncio.current_task():
            self.task.cancel(CANCEL_REASON)

    # --- the worker ---------------------------------------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while True:
                kind, sentence = await self._queue.get()
                if self.cancelled:
                    return
                if kind == "final":
                    await self._notify("voice.audio_chunk", {"turn_id": self.turn_id, "seq": self._seq, "final": True})
                    if not self._first_audio_sent and self._on_idle:
                        self._on_idle()
                    return
                result = await self._synth(sentence or "")
                if self.cancelled:
                    return
                if "error" in result:
                    log.warning("voice: could not speak %r: %s", (sentence or "")[:40], result["error"])
                    continue
                now = self._clock()
                await self._notify("voice.audio_chunk", {
                    "turn_id": self.turn_id, "seq": self._seq, "final": False, "text": sentence,
                    "audio_base64": result["audio_base64"], "mime_type": result.get("mime_type", "audio/wav"),
                })
                self._seq += 1
                if not self._first_audio_sent:
                    self._first_audio_sent = True
                    if self._on_first_audio:
                        self._on_first_audio(now, now - (self._first_sentence_at or now))
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("voice: speech worker failed")


class TapWs:
    """A websocket stand-in that forwards everything and listens for the model's text chunks."""

    def __init__(self, ws: Any, stream: TurnStream):
        self._ws = ws
        self._stream = stream

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    @property
    def voice_cancelled(self) -> bool:
        return self._stream.cancelled

    def voice_flush(self) -> None:
        self._stream.flush_text()

    async def send(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        result = await self._ws.send(message, *args, **kwargs)
        try:
            if isinstance(message, str) and "inference.generate.chunk" in message:
                params = json.loads(message).get("params") or {}
                self._stream.feed_text(str(params.get("chunk") or ""))
        except Exception:  # speech must never break the reply that is being delivered
            log.debug("voice: could not read a chunk for speech", exc_info=True)
        return result


def tap_ws(ws: Any, params: dict[str, Any]) -> Any:
    """``ws`` itself, or a tap on it when this request is the voice turn the harness issued."""
    turn_id = (params or {}).get("voice_turn_id")
    if not turn_id:
        return ws
    from python.harness import voice_runtime

    runtime = voice_runtime.peek()
    stream = runtime.stream_for(str(turn_id)) if runtime else None
    if stream is None:
        return ws
    try:
        stream.task = asyncio.current_task()
    except RuntimeError:
        stream.task = None
    return TapWs(ws, stream)
