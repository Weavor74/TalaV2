"""
Speaking while the model is still writing: the tap on the streamed reply, barge-in cancellation, and the hooks that connect it.

A voice turn's ``inference.generate`` request carries the ``voice_turn_id`` the harness issued. Its websocket is wrapped so each
streamed chunk is forwarded to the UI unchanged and also turned into spoken sentences, the first one as soon as it ends. The
tests pin: ordering, that reasoning and tool tags are never spoken, that cancelling stops audio and tells the stream loop to
stop reading, and that the hooks are really wired into the generation handler (this codebase's failure mode is well-built
code nothing calls).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


from python.harness import voice_runtime, voice_stream
from python.harness.voice_stream import TapWs, TurnStream, tap_ws

REPO = Path(__file__).resolve().parents[1]


class FakeWs:
    def __init__(self):
        self.sent = []
        self.remote_address = ("127.0.0.1", 1)

    async def send(self, message, *a, **k):
        self.sent.append(message)


def chunk(text):
    return json.dumps({"jsonrpc": "2.0", "method": "inference.generate.chunk", "params": {"chunk": text, "session_id": "s"}})


class Rig:
    def __init__(self, synth_delay=0.0):
        self.events = []
        self.spoken = []

        async def notify(method, params):
            self.events.append((method, params))

        async def synth(sentence):
            self.spoken.append(sentence)
            if synth_delay:
                await asyncio.sleep(synth_delay)
            return {"audio_base64": "QQ==", "mime_type": "audio/wav"}

        self.metrics = {}
        self.stream = TurnStream(
            "t1", notify, synth=synth, on_first_text=lambda t: self.metrics.setdefault("text", t),
            on_first_audio=lambda t, d: self.metrics.update(audio=t, sentence=d), on_idle=lambda: self.metrics.update(idle=True))
        self.ws = FakeWs()
        self.tap = TapWs(self.ws, self.stream)

    def audio(self):
        return [p for m, p in self.events if m == "voice.audio_chunk"]


async def drain(r):
    for _ in range(5):
        await asyncio.sleep(0.01)
    if r.stream._worker:
        await asyncio.wait([r.stream._worker], timeout=1)


def run(coro):
    return asyncio.run(coro)


def test_every_chunk_is_forwarded_to_the_ui_untouched():
    async def go():
        r = Rig()
        await r.tap.send(chunk("Hello there. "))
        await r.tap.send("not json at all")
        assert r.ws.sent == [chunk("Hello there. "), "not json at all"]

    run(go())


def test_the_first_sentence_is_spoken_before_the_reply_is_finished():
    async def go():
        r = Rig()
        await r.tap.send(chunk("It is five thirty. "))
        await r.tap.send(chunk("It is a lovely afternoon"))
        await drain(r)
        assert r.spoken == ["It is five thirty."], "the open sentence has not been spoken yet"
        assert r.audio()[0]["seq"] == 0 and r.audio()[0]["final"] is False and r.audio()[0]["text"] == "It is five thirty."

    run(go())


def test_sentences_arrive_in_order_with_increasing_sequence_numbers():
    async def go():
        r = Rig(synth_delay=0.01)
        await r.tap.send(chunk("One is first here. Two is second here. Three is third here. "))
        r.stream.finish()
        await drain(r)
        await asyncio.sleep(0.1)
        parts = [a for a in r.audio() if not a["final"]]
        assert [a["seq"] for a in parts] == [0, 1, 2]
        assert [a["text"] for a in parts] == ["One is first here.", "Two is second here.", "Three is third here."]

    run(go())


def test_reasoning_and_tool_tags_are_never_synthesised():
    async def go():
        r = Rig()
        await r.tap.send(chunk("<thinking>\nThe user wants the time. "))
        await r.tap.send(chunk("I should check.\n</thinking>\n\nLet me look. <call:get_system_info />"))
        await r.tap.send(chunk(" It is noon. "))
        r.tap.voice_flush()
        await drain(r)
        assert not any("user wants" in s or "call" in s for s in r.spoken)
        assert r.spoken[0] == "Let me look."

    run(go())


def test_the_open_sentence_is_spoken_when_a_generation_round_ends():
    async def go():
        r = Rig()
        await r.tap.send(chunk("And that is all"))
        r.tap.voice_flush()
        await drain(r)
        assert r.spoken == ["And that is all"]

    run(go())


def test_finishing_sends_a_final_marker_after_the_last_audio():
    async def go():
        r = Rig()
        await r.tap.send(chunk("Done now. "))
        r.stream.finish()
        await drain(r)
        await asyncio.sleep(0.05)
        assert r.audio()[-1]["final"] is True and r.audio()[-1]["seq"] == 1

    run(go())


def test_a_reply_with_nothing_to_say_reports_idle_instead_of_going_silent_forever():
    async def go():
        r = Rig()
        await r.tap.send(chunk("<thinking>\nonly thoughts\n</thinking>"))
        r.stream.finish()
        await drain(r)
        await asyncio.sleep(0.05)
        assert r.metrics.get("idle") is True and r.spoken == []

    run(go())


def test_cancelling_stops_audio_and_tells_the_stream_loop_to_stop_reading():
    async def go():
        r = Rig(synth_delay=0.05)
        await r.tap.send(chunk("First sentence is long enough. Second sentence is also long. "))
        assert r.tap.voice_cancelled is False
        r.stream.cancel()
        assert r.tap.voice_cancelled is True
        await asyncio.sleep(0.2)
        assert len(r.audio()) <= 1, "nothing after the interruption is spoken"
        await r.tap.send(chunk("Third sentence arrives late. "))
        await asyncio.sleep(0.1)
        assert "Third sentence arrives late." not in r.spoken

    run(go())


def test_a_synthesis_failure_skips_that_sentence_and_keeps_going():
    async def go():
        events, calls = [], []

        async def notify(m, p):
            events.append((m, p))

        async def synth(sentence):
            calls.append(sentence)
            return {"error": "boom"} if len(calls) == 1 else {"audio_base64": "QQ==", "mime_type": "audio/wav"}

        s = TurnStream("t", notify, synth=synth)
        s.feed_text("This one fails to speak. This one works fine. ")
        s.finish()
        await asyncio.sleep(0.1)
        assert [p["text"] for m, p in events if m == "voice.audio_chunk" and not p["final"]] == ["This one works fine."]

    run(go())


def test_the_time_to_first_audio_hooks_fire_once():
    async def go():
        r = Rig()
        await r.tap.send(chunk("Hello world today. "))
        await r.tap.send(chunk("Second sentence follows here. "))
        await drain(r)
        await asyncio.sleep(0.05)
        assert "text" in r.metrics and "audio" in r.metrics and r.metrics["sentence"] >= 0

    run(go())


# --- tap_ws -----------------------------------------------------------------------------------------------------------------------


def test_a_request_without_a_voice_turn_id_is_not_tapped():
    ws = FakeWs()
    assert tap_ws(ws, {"prompt": "hi"}) is ws


def test_an_unknown_voice_turn_id_is_not_tapped(monkeypatch):
    ws = FakeWs()
    monkeypatch.setattr(voice_runtime, "peek", lambda: None)
    assert tap_ws(ws, {"voice_turn_id": "nope"}) is ws


def test_the_issued_voice_turn_id_is_tapped(monkeypatch):
    class RT:
        def stream_for(self, turn_id):
            return stream if turn_id == "t1" else None

    async def notify(m, p):
        pass

    stream = TurnStream("t1", notify)
    monkeypatch.setattr(voice_runtime, "peek", lambda: RT())
    ws = FakeWs()
    assert isinstance(tap_ws(ws, {"voice_turn_id": "t1"}), TapWs)
    assert tap_ws(ws, {"voice_turn_id": "t2"}) is ws


def test_the_tap_passes_other_attributes_through_to_the_real_connection():
    r = Rig()
    assert r.tap.remote_address == ("127.0.0.1", 1)


# --- the hooks are really in the generation handler ------------------------------------------------------------------------------------


def server_source():
    return (REPO / "python" / "harness" / "server.py").read_text(encoding="utf-8")


def test_the_generation_handler_taps_the_websocket_for_voice_turns():
    src = server_source()
    start = src.index("async def handle_inference_generate")
    body = src[start:start + 12000]
    assert "ws = tap_ws(ws, params)" in body


def test_the_stream_loop_stops_reading_when_the_turn_is_cancelled():
    src = server_source().replace(chr(13) + chr(10), chr(10))
    indent = ' ' * 40
    nl = chr(10)
    expected = ('async for line in response.aiter_lines():' + nl + indent
                + 'if getattr(ws, "voice_cancelled", False):' + nl + indent + '    break')
    assert expected in src


def test_each_generation_round_flushes_the_open_sentence():
    assert 'getattr(ws, "voice_flush", lambda: None)()' in server_source()


# --- cancelling a generation that has not produced a token yet ----------------------------------------------------------------------------


def test_cancelling_a_turn_cancels_the_request_that_is_generating_it():
    """A model still reading its prompt streams nothing, so the stream loop's own check never runs. Found by the live smoke: the
    reply after a barge-in waited 59 s behind the generation she had been talked over in."""
    async def go():
        async def notify(m, p):
            pass

        stream = TurnStream("t1", notify)
        seen = []

        async def generating():
            stream.task = asyncio.current_task()
            try:
                await asyncio.sleep(30)  # waiting on the model's first token
            except asyncio.CancelledError as exc:
                seen.append(exc.args)
                raise

        task = asyncio.ensure_future(generating())
        await asyncio.sleep(0.01)
        stream.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert seen == [(voice_stream.CANCEL_REASON,)]

    run(go())


def test_cancelling_from_inside_the_generating_task_does_not_cancel_itself():
    async def go():
        async def notify(m, p):
            pass

        stream = TurnStream("t1", notify)

        async def generating():
            stream.task = asyncio.current_task()
            stream.cancel()
            await asyncio.sleep(0)
            return "survived"

        assert await asyncio.ensure_future(generating()) == "survived"

    run(go())


def test_a_finished_request_is_not_cancelled_later():
    async def go():
        async def notify(m, p):
            pass

        stream = TurnStream("t1", notify)

        async def quick():
            stream.task = asyncio.current_task()

        task = asyncio.ensure_future(quick())
        await task
        stream.cancel()
        assert task.cancelled() is False

    run(go())


def test_tapping_a_voice_turn_remembers_the_task_that_is_generating_it(monkeypatch):
    class RT:
        def stream_for(self, turn_id):
            return stream

    async def notify(m, p):
        pass

    stream = TurnStream("t1", notify)
    monkeypatch.setattr(voice_runtime, "peek", lambda: RT())

    async def go():
        tap_ws(FakeWs(), {"voice_turn_id": "t1"})
        return asyncio.current_task()

    assert run(go()) is not None
    assert stream.task is not None


def test_the_dispatcher_answers_a_request_that_was_cancelled_by_barge_in():
    src = server_source().replace(chr(13) + chr(10), chr(10))
    assert "except asyncio.CancelledError as exc:" in src
    block = src[src.index("except asyncio.CancelledError as exc:"):][:600]
    assert "voice_stream.CANCEL_REASON" in block or '"voice-barge-in"' in block
    assert "uncancel()" in block and "_err(req_id" in block
