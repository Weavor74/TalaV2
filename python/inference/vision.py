"""
vision.py -- the vision model reads the image; the main model answers.

An image turn used to hand gemma3:4b Tala's whole assembled prompt (persona, retrieved nodes, tool schemas, history: 8,807 tokens
measured on Kagura against its 8,192-token window). It rejected it (HTTP 400), the request fell back to the main model, and that
meant stopping Ollama's model, restarting llama-server (53 s) and answering without ever seeing the picture.

Now the vision model is asked one small question: the image and what the user asked. Its description goes into the main model's
prompt, and the main model answers as itself, with its persona, its tools and its 16k window. The vision call runs on the CPU
when the profile says so (``vision.gpu: false``), so it takes no VRAM and the main model is never stopped.

Every failure (Ollama down, a timeout, an empty answer) returns None and the turn carries on exactly as it did before: the image
stays attached for the router, and a main model that cannot see it says so. Nothing here raises.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from python.inference.profiles import Profile

logger = logging.getLogger(__name__)

MAX_IMAGES = 4
MAX_QUESTION_CHARS = 500
MAX_CAPTION_CHARS = 2000
#: The vision model only holds the image and one question, so a small window keeps CPU prefill short.
CAPTION_NUM_CTX = 4096
CAPTION_NUM_PREDICT = 320
#: A CPU pass over an image is slow (seconds to a minute); give it room rather than falling back to a blind main model.
CAPTION_TIMEOUT_S = 240.0

SYSTEM = (
    "You describe images for an assistant that cannot see them. Report only what is actually visible: the objects, people and "
    "layout, the colours, and any text or numbers transcribed exactly as written. Do not guess beyond what is visible. Be concise "
    "(under 150 words) but include whatever the user's question needs."
)


def _bare_base64(image: Any) -> str | None:
    if not isinstance(image, str) or not image.strip():
        return None
    return image.split(",", 1)[1] if image.startswith("data:") and "," in image else image


async def caption_images(images: Any, question: str, profile: Profile | None) -> str | None:
    """A text description of ``images`` for the main model's prompt, or None when there is nothing to add."""
    if not isinstance(images, list) or profile is None or profile.vision is None:
        return None
    encoded = [b for b in (_bare_base64(i) for i in images) if b][:MAX_IMAGES]
    if not encoded:
        return None
    role = profile.vision
    ask = (question or "").strip()[:MAX_QUESTION_CHARS] or "Please describe the image."
    options: dict[str, Any] = {"num_ctx": min(role.num_ctx, CAPTION_NUM_CTX), "temperature": 0.1, "num_predict": CAPTION_NUM_PREDICT}
    cfg: dict[str, Any] = {"engine": "ollama", "model": role.model}
    if role.num_gpu is not None:
        options["num_gpu"] = role.num_gpu  # 0: the CPU, so no VRAM and no GPU room to make
        cfg["num_gpu"] = role.num_gpu
    body = {
        "model": role.model, "stream": False, "keep_alive": role.keep_alive, "options": options,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"The user asked: {ask}\nDescribe what you see so they can be answered.", "images": encoded}],
    }
    started = time.perf_counter()
    try:
        from python.inference import vram_arbiter

        try:
            await vram_arbiter.prepare_for({"type": "local", "config": cfg}, profile)
        except Exception as exc:
            logger.warning("vision: could not make room for %s: %s", role.model, exc)
        async with httpx.AsyncClient(timeout=CAPTION_TIMEOUT_S) as client:
            resp = await client.post(f"{profile.ollama_url}/api/chat", json=body)
            if resp.status_code != 200:
                logger.warning("vision: %s answered HTTP %s: %s", role.model, resp.status_code, str(resp.text)[:200])
                return None
            data = resp.json()
        text = ((data.get("message") or {}).get("content") or "").strip() if isinstance(data, dict) else ""
    except Exception as exc:  # a vision failure must never cost the turn its answer
        logger.warning("vision: could not describe the image(s): %s: %s", type(exc).__name__, exc)
        return None
    if not text:
        return None
    count = len(encoded)
    logger.info("vision: %s described %d image%s in %.1fs on the %s", role.model, count, "s" if count != 1 else "",
                time.perf_counter() - started, "GPU" if role.gpu else "CPU")
    return (f"[The user attached {count} image{'s' if count != 1 else ''}. Description from the vision model: "
            f"{text[:MAX_CAPTION_CHARS]}]")
