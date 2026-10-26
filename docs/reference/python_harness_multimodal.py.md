# File: `multimodal.py`

## Module Description

multimodal.py — Tala Desktop-Native Multimodality
=================================================
Handles Screen Context (screenshots) and Voice Commands (local whisper)
gracefully failing if TALA_HEADLESS is active.

## Functions

### `async def get_screen_context()`
Capture the active screen, return base64 encoded image.


## Source Code

```python
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

# Fallback imports so the server doesn't crash if optional deps aren't installed yet
try:
    import pyautogui
    from PIL import Image
except ImportError:
    pyautogui = None
    Image = None

try:
    import whisper
    import sounddevice as sd
    import numpy as np
except ImportError:
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



```
