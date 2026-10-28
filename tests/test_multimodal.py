"""
python/harness/multimodal.py: screen context only.

The harness used to have a second way to listen: ``transcribe_voice`` opened the microphone itself for a fixed five seconds and
transcribed whatever was in it. Nothing in the app called it. The microphone belongs to the renderer, and the conversation runs
renderer -> ``voice.audio`` -> voice activity detection -> wake word -> faster-whisper (python/harness/voice_runtime.py), which
hears an utterance when it ends rather than a clock. Two ways to listen meant two places a microphone could be opened and two
places the "audio never leaves this machine" claim had to hold, so the older one is gone and these tests keep it gone.
"""
import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from python.harness import multimodal
from python.harness.multimodal import get_screen_context

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@patch("python.harness.multimodal.settings")
async def test_screen_capture_is_refused_in_headless_mode(mock_settings):
    mock_settings.IS_HEADLESS = True

    res_screen = await get_screen_context()

    assert "error" in res_screen
    assert "headless" in res_screen["error"].lower()


@pytest.mark.asyncio
@patch("python.harness.multimodal.settings")
@patch("python.harness.multimodal.pyautogui", None)  # a missing dependency
async def test_screen_capture_says_so_when_its_dependencies_are_missing(mock_settings):
    mock_settings.IS_HEADLESS = False

    res_screen = await get_screen_context()

    assert "error" in res_screen
    assert "not installed" in res_screen["error"].lower()


def test_the_harness_has_no_fixed_length_recorder_any_more():
    assert not hasattr(multimodal, "transcribe_voice")
    tree = ast.parse((REPO / "python" / "harness" / "multimodal.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "sounddevice" not in imported, "multimodal.py must not open a microphone: the renderer owns it"


def test_the_rpc_that_called_it_is_not_registered():
    from python.harness import server

    assert "multimodal.transcribe_voice" not in server._HANDLERS
    assert not hasattr(server, "handle_multimodal_transcribe_voice")
    assert "multimodal.capture_screen" in server._HANDLERS, "the screen half is untouched"


def test_nothing_in_the_app_asks_for_the_old_capture():
    """The renderer, the agent tools and the harness never named it; if one starts to, it must use voice.audio instead."""
    offenders = []
    for folder in ("python", "sdk", "renderer", "electron"):
        for path in (REPO / folder).rglob("*"):
            if path.suffix not in (".py", ".js", ".html") or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "transcribe_voice" in text:
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
