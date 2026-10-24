"""
The voice RPCs: what the renderer may ask of the voice loop, and what it may not.

Every parameter is validated before anything runs (a known mode, a bounded base64 audio chunk, a known playback state);
listening cannot be started while the emergency halt is engaged and none of these methods is on the halt allow-list, so a
halt refuses them all; and nothing here is a tool, so the agent cannot switch the microphone on.
"""
from __future__ import annotations

import base64
import json

import pytest

from python.harness import halt_gate, voice_rpc, voice_runtime

METHODS = ("voice.listen_start", "voice.listen_stop", "voice.audio", "voice.playback", "voice.turn_done", "voice.interrupt",
           "voice.status", "voice.get_settings", "voice.set_settings")


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.fail = None

    def status(self):
        return {"state": "waiting", "listening": True}

    async def start(self, mode="continuous"):
        if self.fail:
            raise self.fail
        self.calls.append(("start", mode))
        return {"state": "waiting", "mode": mode}

    async def stop(self):
        self.calls.append(("stop",))
        return {"state": "off"}

    async def ingest(self, pcm):
        self.calls.append(("ingest", len(pcm)))
        return {"state": "waiting"}

    async def playback(self, state, turn_id):
        self.calls.append(("playback", state, turn_id))
        return {"state": "speaking"}

    def turn_done(self, turn_id, text=None):
        self.calls.append(("turn_done", turn_id, text))
        return True

    async def interrupt(self):
        self.calls.append(("interrupt",))
        return {"state": "waiting"}

    def apply_settings(self):
        self.calls.append(("apply_settings",))


@pytest.fixture
def rt(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    fake = FakeRuntime()
    monkeypatch.setattr(voice_runtime, "get", lambda notify=None: fake)
    monkeypatch.setattr(voice_runtime, "peek", lambda: fake)
    return fake


async def call(method, params):
    return json.loads(await voice_rpc.VOICE_RPC_HANDLERS[method](params, 1))


def pcm_b64(n=3200):
    return base64.b64encode(b"\x00\x01" * (n // 2)).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("params,mode", [({}, "continuous"), ({"mode": "dictate"}, "dictate"), ({"mode": "continuous"}, "continuous")])
async def test_listening_starts_in_a_known_mode(rt, params, mode):
    reply = await call("voice.listen_start", params)
    assert reply["result"]["mode"] == mode and ("start", mode) in rt.calls


@pytest.mark.asyncio
async def test_an_unknown_mode_is_refused(rt):
    assert "error" in await call("voice.listen_start", {"mode": "always-on-forever"})
    assert rt.calls == []


@pytest.mark.asyncio
async def test_listening_cannot_start_while_halted(rt):
    rt.fail = PermissionError("the emergency halt is engaged; voice listening is off")
    reply = await call("voice.listen_start", {})
    assert "error" in reply and "halt" in reply["error"]["message"].lower()


@pytest.mark.asyncio
async def test_missing_models_say_how_to_get_them(rt):
    rt.fail = RuntimeError("whisper model 'small.en' is not installed at D:/x; run scripts/setup_voice.py")
    reply = await call("voice.listen_start", {})
    assert "setup_voice" in reply["error"]["message"]


@pytest.mark.asyncio
async def test_audio_is_decoded_and_ingested(rt):
    reply = await call("voice.audio", {"pcm_base64": pcm_b64(3200)})
    assert ("ingest", 3200) in rt.calls and reply["result"]["state"] == "waiting"


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {}, {"pcm_base64": 7}, {"pcm_base64": "!!!not base64!!!"}, {"pcm_base64": ""},
    {"pcm_base64": base64.b64encode(b"abc").decode()},  # an odd byte count cannot be 16-bit samples
    {"pcm_base64": pcm_b64(2_000_000)},               # a chunk is a fraction of a second, not a recording
])
async def test_bad_audio_is_refused_before_it_reaches_the_models(rt, params):
    assert "error" in await call("voice.audio", params)
    assert not any(c[0] == "ingest" for c in rt.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"state": "exploded", "turn_id": "t"}, {"turn_id": "t"}, {"state": "started", "turn_id": 5}])
async def test_playback_reports_must_name_a_known_state_and_a_turn(rt, params):
    assert "error" in await call("voice.playback", params)


@pytest.mark.asyncio
async def test_playback_started_and_ended_are_passed_on(rt):
    await call("voice.playback", {"state": "started", "turn_id": "t1"})
    await call("voice.playback", {"state": "ended", "turn_id": "t1"})
    assert ("playback", "started", "t1") in rt.calls and ("playback", "ended", "t1") in rt.calls


@pytest.mark.asyncio
async def test_turn_done_passes_the_reply_text_for_the_non_streaming_case(rt):
    reply = await call("voice.turn_done", {"turn_id": "t1", "text": "Hello."})
    assert reply["result"] == {"ok": True} and ("turn_done", "t1", "Hello.") in rt.calls


@pytest.mark.asyncio
async def test_turn_done_needs_a_turn_id_and_a_bounded_text(rt):
    assert "error" in await call("voice.turn_done", {})
    assert "error" in await call("voice.turn_done", {"turn_id": "t", "text": "x" * 100_000})


@pytest.mark.asyncio
async def test_settings_round_trip_and_reach_the_runtime(rt):
    changed = await call("voice.set_settings", {"settings": {"piper_voice": "en_US-ryan-medium", "wake_enabled": False}})
    assert changed["result"]["piper_voice"] == "en_US-ryan-medium" and ("apply_settings",) in rt.calls
    shown = await call("voice.get_settings", {})
    assert shown["result"]["settings"]["wake_enabled"] is False
    assert "allow_cloud_tts" in shown["result"]["settings"] and shown["result"]["settings"]["allow_cloud_tts"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [{"allow_cloud_tts": "yes"}, {"whisper_model": "huge"}, {"nope": 1}, {"wake_threshold": 5}, "x", None])
async def test_bad_settings_are_refused_and_nothing_is_saved(rt, settings):
    assert "error" in await call("voice.set_settings", {"settings": settings})
    assert (await call("voice.get_settings", {}))["result"]["settings"]["allow_cloud_tts"] is False


@pytest.mark.asyncio
async def test_turning_on_cloud_tts_is_a_deliberate_boolean_and_is_reported_as_cloud(rt):
    reply = await call("voice.set_settings", {"settings": {"allow_cloud_tts": True}})
    assert reply["result"]["allow_cloud_tts"] is True
    assert "microsoft" in reply["result"]["warnings"][0].lower()


@pytest.mark.parametrize("method", METHODS)
def test_the_emergency_halt_refuses_every_voice_method(method, monkeypatch):
    monkeypatch.setattr(halt_gate, "is_halted_fail_closed", lambda: True)
    assert halt_gate.check_rpc(method, {}, 1) is not None


@pytest.mark.parametrize("method", METHODS)
def test_every_voice_method_is_registered_with_the_server(method):
    from python.harness import server

    assert method in server._HANDLERS


def test_no_voice_control_is_an_agent_tool():
    """The agent can speak (speak_text) but cannot switch the microphone on: listening is a UI action only."""
    from sdk.agent.tools import ToolRegistry

    names = {t["name"] for t in ToolRegistry.get_tools()}
    assert not {n for n in names if "listen" in n or "microphone" in n or n.startswith("voice")}
