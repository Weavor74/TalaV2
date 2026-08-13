"""
multimodal.py — Tala Desktop-Native Multimodality
=================================================
Handles Screen Context (screenshots) and Voice Commands (local whisper)
gracefully failing if TALA_HEADLESS is active.
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
    import whisper
    import sounddevice as sd
    import numpy as np
except Exception as exc:  # noqa: BLE001 - optional dep, degrade don't crash
    logger.warning(
        "Voice dependencies unavailable (%s: %s); transcription disabled.",
        type(exc).__name__, exc,
    )
    whisper = None
    sd = None
    np = None

# We can cache the whisper model in memory once loaded
_whisper_model = None


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
    """Record audio for duration_seconds and transcribe using local whisper."""
    global _whisper_model

    if settings.IS_HEADLESS:
        return {"error": "Tala is running in embedded/headless mode and cannot access the Microphone."}
    
    if not whisper or not sd or not np:
        return {"error": "Multimodal dependencies (openai-whisper, sounddevice, numpy) are not installed."}

    try:
        # Load model lazily
        if _whisper_model is None:
            logger.info("Loading local Whisper model (tiny)...")
            _whisper_model = whisper.load_model("tiny")
        
        fs = 16000  # Sample rate required by whisper
        logger.info(f"Recording audio for {duration_seconds} seconds...")
        recording = sd.rec(int(duration_seconds * fs), samplerate=fs, channels=1, dtype='float32')
        sd.wait()
        logger.info("Recording finished. Transcribing...")
        
        # Whisper expects 1D array of float32
        audio_data = np.squeeze(recording)
        
        result = _whisper_model.transcribe(audio_data, fp16=False)
        text = result.get("text", "").strip()
        return {"text": text}
    except Exception as e:
        logger.exception("Failed to transcribe voice")
        return {"error": str(e)}
