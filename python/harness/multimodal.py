"""
multimodal.py — Tala Desktop-Native Multimodality
=================================================
Handles Screen Context (screenshots) and a fixed-length voice capture (local faster-whisper)
gracefully failing if TALA_HEADLESS is active.

The conversational voice loop -- wake word, voice-activity detection, streaming transcripts, spoken replies and barge-in --
is python/harness/voice_runtime.py; the renderer owns that microphone. ``transcribe_voice`` here is the older harness-side
recorder, kept for callers that want one fixed-length capture, and it now uses the same local engine.
"""
import base64
import io
import logging
from typing import Any

from python.harness.config import settings

logger = logging.getLogger(__name__)

# Fallback imports so the server doesn't crash if optional deps aren't
# installed yet.
#
# These catch Exception rather than ImportError on purpose. A missing package
# is only one of the two ways these imports fail: on a headless host
# `import pyautogui` reaches mouseinfo, which evaluates
# Display(os.environ['DISPLAY']) at import time and raises KeyError, and
# sounddevice raises OSError when PortAudio finds no device. An
# ImportError-only guard let the "gracefully failing if TALA_HEADLESS is
# active" path in this module's own docstring crash outright instead --
# which is what kept CI red.
try:
    import pyautogui
    from PIL import Image
except Exception as exc:  # noqa: BLE001 - optional dep, degrade don't crash
    logger.warning(
        "Screen-context dependencies unavailable (%s: %s); screen capture disabled.",
        type(exc).__name__, exc,
    )
    pyautogui = None
    Image = None

try:
    import sounddevice as sd
    import numpy as np
except Exception as exc:  # noqa: BLE001 - optional dep, degrade don't crash
    logger.warning(
        "Voice dependencies unavailable (%s: %s); transcription disabled.",
        type(exc).__name__, exc,
    )
    sd = None
    np = None

# The recogniser is loaded once, on first use, from D: (python/harness/voice_stt.py)
_stt = None


async def get_screen_context() -> dict[str, Any]:
    """Capture the active screen, return base64 encoded image."""
    if settings.IS_HEADLESS:
        return {"error": "Tala is running in embedded/headless mode and cannot access the Desktop UI."}
    
    if not pyautogui or not Image:
        return {"error": "Multimodal dependencies (pyautogui, Pillow) are not installed."}

    try:
        screenshot = pyautogui.screenshot()
        # Resize or compress if necessary, but for now just send JPEG
        buf = io.BytesIO()
        screenshot.save(buf, format="JPEG", quality=80)
        img_bytes = buf.getvalue()
        base64_str = base64.b64encode(img_bytes).decode("utf-8")
        
        return {
            "mime_type": "image/jpeg",
            "data": base64_str
        }
    except Exception as e:
        logger.exception("Failed to capture screen context")
        return {"error": str(e)}


async def transcribe_voice(duration_seconds: int = 5) -> dict[str, Any]:
    """Record audio for duration_seconds and transcribe it locally with faster-whisper."""
    global _stt

    if settings.IS_HEADLESS:
        return {"error": "Tala is running in embedded/headless mode and cannot access the Microphone."}

    if not sd or not np:
        return {"error": "Voice dependencies (sounddevice, numpy) are not installed."}

    try:
        import asyncio

        from python.harness.voice_stt import SAMPLE_RATE, SttEngine

        if _stt is None:
            _stt = SttEngine()
            await asyncio.to_thread(_stt.load)

        logger.info("Recording audio for %s seconds...", duration_seconds)
        recording = sd.rec(int(duration_seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        sd.wait()
        logger.info("Recording finished. Transcribing...")

        text = await asyncio.to_thread(_stt.transcribe, np.squeeze(recording))
        return {"text": text}
    except Exception as e:
        logger.exception("Failed to transcribe voice")
        return {"error": str(e)}
