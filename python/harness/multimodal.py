"""
multimodal.py — Tala Desktop-Native Multimodality
=================================================
Handles Screen Context (screenshots), gracefully failing if TALA_HEADLESS is active.

Listening is not here. The conversational voice loop -- wake word, voice-activity detection, streaming transcripts, spoken replies
and barge-in -- is python/harness/voice_runtime.py, and the renderer owns that microphone. The fixed five-second harness-side
recorder that used to live in this file was removed: nothing called it, and it was a second place a microphone could be opened.
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
