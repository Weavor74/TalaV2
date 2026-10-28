"""
What the voice loop does with audio and text, and the proof that none of it leaves the machine.

Four guarantees, each pinned here:

* the modules that hear and answer (wake word, VAD, recogniser, session, runtime, stream, chunker, meter, latency log) import no
  network library at all, so they cannot open a connection;
* audio is never written to disk unless ``TALA_VOICE_DEBUG_DIR`` is set, and then only inside it and never on C:;
* the latency log holds numbers only (tests/test_voice_latency.py), and the only TTS that can leave the machine, edge-tts, is behind
  ``allow_cloud_tts`` (tests/test_voice_backends.py);
* nothing in a spoken turn reaches the text-to-speech engine except plain sentences (no tags, no tool calls, no code).
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import numpy as np
import pytest

from tests.test_voice_runtime import Rig, quiet, tone

HARNESS = Path(__file__).resolve().parents[1] / "python" / "harness"
LISTENING = ("voice_runtime", "voice_session", "voice_vad", "voice_wake", "voice_stt", "voice_stream", "voice_chunker", "voice_meter",
             "voice_latency", "voice_settings", "voice_kokoro", "voice_preload", "voice_rpc")
NETWORK = {"requests", "httpx", "urllib3", "aiohttp", "websockets", "websocket", "socket", "ssl", "http", "ftplib", "smtplib", "edge_tts",
           "huggingface_hub", "openai", "anthropic"}
AUDIO_SUFFIXES = {".wav", ".pcm", ".raw", ".mp3", ".ogg", ".flac", ".opus", ".webm", ".m4a"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.add((node.module or "").split(".")[0])
    return found


@pytest.mark.parametrize("module", LISTENING)
def test_the_listening_and_answering_modules_import_no_network_library(module):
    assert _imports(HARNESS / f"{module}.py") & NETWORK == set(), f"{module}.py could open a connection"


def test_the_only_voice_module_that_can_reach_a_network_is_the_gated_tts_backend_selector():
    reaching = sorted(p.stem for p in HARNESS.glob("voice*.py") if _imports(p) & NETWORK)
    assert reaching == ["voice"], "voice.py holds the provider-table and edge-tts backends, both refused unless explicitly allowed"


# --- audio on disk ----------------------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _world(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr("python.harness.voice_runtime.PARTIAL_EVERY_S", 1e9)
    monkeypatch.delenv("TALA_VOICE_DEBUG_DIR", raising=False)
    monkeypatch.setenv("TALA_VOICE_LOG_DIR", str(tmp_path / "logs"))


def _a_spoken_turn() -> Rig:
    async def go():
        r = Rig("Hey Tala, what time is it")
        await r.rt.start()
        await r.feed(np.concatenate([quiet(0.3), tone(1.5), quiet(1.5)]), wake_at=5)
        turn = r.last("voice.turn")["turn_id"]
        r.rt.stream_for(turn).feed_text("It is noon. ")
        await asyncio.sleep(0.05)
        r.clock.t += 0.2
        await r.rt.playback("started", turn)
        return r

    return asyncio.run(go())


def _audio_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES]


def test_a_whole_spoken_turn_writes_no_audio_anywhere_by_default(tmp_path):
    r = _a_spoken_turn()

    assert "voice.turn" in r.names()
    assert _audio_files(tmp_path) == [], "no recording of the user, and none of her voice, is kept"


def test_with_the_debug_flag_audio_goes_only_into_that_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_VOICE_DEBUG_DIR", str(tmp_path / "debug"))

    _a_spoken_turn()

    saved = _audio_files(tmp_path)
    assert saved and all((tmp_path / "debug") in p.parents for p in saved)


@pytest.mark.parametrize("bad", ["C:\\voice-debug", "c:/voice-debug"])
def test_a_debug_directory_on_c_is_refused_and_nothing_is_recorded(tmp_path, monkeypatch, bad):
    from python.harness import voice_settings

    monkeypatch.setenv("TALA_VOICE_DEBUG_DIR", bad)

    assert voice_settings.debug_dir() is None
    _a_spoken_turn()
    assert _audio_files(tmp_path) == []


# --- what reaches the speech engine -------------------------------------------------------------------------------------------------


def test_nothing_but_plain_sentences_reaches_the_speech_engine():
    from python.harness.voice_stream import TurnStream

    spoken: list[str] = []

    async def synth(sentence):
        spoken.append(sentence)
        return {"audio_base64": "QQ==", "mime_type": "audio/wav", "backend": "fake"}

    async def notify(method, params):
        return None

    async def go():
        stream = TurnStream("t1", notify, synth=synth)
        reply = ("<thinking>I should check the clock tool.</thinking>It is noon on the Nyx run. "
                 '<call:get_time zone="UTC"/>Then I will log it. <call name="note"/> ```python\nprint(1)\n``` '
                 "<call><code><call name='x'/></code></call>All done, **boss**.")
        for i in range(0, len(reply), 7):  # arrives in arbitrary fragments
            stream.feed_text(reply[i:i + 7])
        stream.finish()
        await asyncio.sleep(0.2)

    asyncio.run(go())

    assert spoken, "the plain sentences were spoken"
    joined = " ".join(spoken)
    for banned in ("<", ">", "call", "thinking", "get_time", "print", "```", "check the clock"):
        assert banned not in joined, f"{banned!r} reached the speech engine: {spoken}"
    assert "noon on the Nyx run" in joined and "All done" in joined


def test_the_microphone_is_off_until_the_user_turns_listen_on_start_on_and_the_page_ships_it_unticked():
    from python.harness import voice_settings

    assert voice_settings.DEFAULTS["listen_on_start"] is False
    html = (HARNESS.parents[1] / "renderer" / "index.html").read_text(encoding="utf-8")
    tag = next(line for line in html.splitlines() if 'id="voice-listen-on-start"' in line)
    assert "checked" not in tag.split("id=\"voice-listen-on-start\"")[1].split(">")[0]
