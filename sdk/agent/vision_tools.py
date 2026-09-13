"""
sdk/agent/vision_tools.py -- what Tala can see, and what she can draw.

Split out of sdk/agent/tools.py, which the file-size ratchet
(tests/test_size_ratchet.py) pins at 4968 lines. Vision and image
generation are one domain and the largest coherent piece of that file not
already living in a module of its own, so they are what moved.

The three call sites here share one requirement, which is why they travel
together: each needs providers in the normalized shape that
python.harness.server._get_providers returns, and each has to decide which
of those providers can actually accept an image.

Imported for its side effect, like every other tool module -- @register_tool
runs at import time, and tools.py imports this at the bottom of itself so
the register_tool cycle stays resolvable.
"""

from __future__ import annotations

import logging
from typing import Any

from sdk.agent.tools import register_tool

log = logging.getLogger(__name__)


_VISION_MODEL_HINTS = (
    "llava", "vision", "-vl", "vl-", "gemini", "gpt-4o", "gpt-4.1", "gpt-5",
    "pixtral", "moondream", "minicpm", "kimi", "claude", "internvl",
    "gemma-3", "gemma3", "qwen2-vl", "qwen2.5-vl", "o3", "o4",
)


async def _provider_is_vision_capable(provider: dict[str, Any]) -> bool:
    """
    Whether this provider's model can actually accept image input.

    Prefers Ollama's own /api/show capabilities report wherever it's
    reachable, over guessing from the model name. A name hint only
    recognizes models whose name happens to signal vision (qwen2.5vl,
    llava, ...) and silently excludes anything else -- including a locally
    fine-tuned/renamed model (e.g. "muse-glimmer") that IS vision-capable per
    Ollama but matches no entry in _VISION_MODEL_HINTS. Falls back to the
    name heuristic for remote providers (OpenAI/Anthropic/Gemini-style
    endpoints have no equivalent capabilities probe) or if the probe fails.
    """
    cfg = provider.get("config") or {}
    if cfg.get("vision"):
        return True
    model = str(cfg.get("model", "")).lower()

    if provider.get("type") == "local" and cfg.get("engine", "ollama") == "ollama":
        try:
            import httpx
            endpoint = str(cfg.get("endpoint", "http://localhost:11434"))
            endpoint = endpoint.replace("/v1", "").rstrip("/")
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(f"{endpoint}/api/show", json={"name": cfg.get("model", "")})
                if resp.status_code == 200:
                    caps = (resp.json() or {}).get("capabilities") or []
                    return "vision" in caps
        except Exception:
            pass  # Probe failed (endpoint down, timeout, ...); fall through.

    return any(h in model for h in _VISION_MODEL_HINTS)


@register_tool(
    name="analyze_screen",
    description="Capture the user's screen and analyze it with a vision-capable inference provider. Use to see what the user is looking at.",
)
async def analyze_screen(question: str = "Describe what is on the screen.", provider_id: str | None = None) -> dict[str, Any]:
    """
    NAME
           analyze_screen - capture the screen and describe it with a vision model

    SYNOPSIS
           analyze_screen(question: str, provider_id: str | None = None) -> dict[str, Any]

    DESCRIPTION
           Takes a screenshot of the active desktop, sends it to a
           vision-capable provider (explicit provider_id, or the first
           configured provider whose model looks multimodal), and returns
           the model's textual analysis.
    """
    from python.harness.multimodal import get_screen_context

    shot = await get_screen_context()
    return await _analyze_screen_image(shot, question, provider_id)


async def _analyze_screen_image(shot, question, provider_id=None):
    """NAME
           _analyze_screen_image - analyze an already captured desktop image
    DESCRIPTION
           Keeps vision coordinates accountable to the image actually observed.
    """
    from python.harness.server import _get_providers, _normalize_endpoint_url
    if "error" in shot:
        return {"success": False, "error": shot["error"]}

    providers = await _get_providers()
    candidates = [p for p in providers if p.get("type") in ("remote", "local")]
    unknown_provider_note = None
    if provider_id:
        named = [p for p in candidates if p["id"] == provider_id]
        if named:
            candidates = named
        else:
            # An unknown provider_id is a bad hint, not a reason to fail.
            #
            # This is an optional expert knob, and models guess at it: one sent
            # "default", another sent "tool-desktop-observe" -- the id of the
            # tool's own documentation node, which its context had just been
            # given. Each wrong guess killed the whole observation, which is a
            # screenshot and a window list thrown away over a parameter that
            # did not need to be set at all.
            #
            # So: fall back to automatic selection and say so. Silently
            # ignoring a caller's explicit choice would be worse than failing;
            # the returned `provider` names what actually ran, and
            # provider_note says the request was not honoured and why.
            vision = [p["id"] for p in providers if await _provider_is_vision_capable(p)]
            log.warning(
                "vision: provider_id %r does not exist; choosing automatically", provider_id
            )
            unknown_provider_note = (
                f"provider_id {provider_id!r} does not exist and was ignored; "
                f"chose a provider automatically. Valid vision-capable ids: "
                f"{', '.join(vision) or 'none configured'}. Omitting provider_id "
                f"is the normal case."
            )
            provider_id = None

    if not provider_id:
        # Reached both when no provider was named and when a named one did not
        # exist, so the fallback narrows to vision-capable providers exactly
        # like the ordinary path -- rather than leaving every embedding model
        # in the running.
        vision_ready = [p for p in candidates if await _provider_is_vision_capable(p)]
        candidates = vision_ready or candidates

    if not candidates:
        return {"success": False, "error": "No inference providers configured for vision analysis."}

    import httpx

    errors: list[str] = []
    for provider in candidates:
        cfg = provider.get("config") or {}
        endpoint = str(cfg.get("endpoint", "")).strip()
        if not endpoint and provider.get("type") == "local":
            endpoint = "http://localhost:11434/v1"
        if not endpoint:
            errors.append(f"{provider['name']}: no endpoint")
            continue
        model = str(cfg.get("model", "")).strip()
        api_key = str(cfg.get("apiKey", "")).strip()

        headers = {"Content-Type": "application/json"}
        if api_key:
            if "anthropic" in endpoint or cfg.get("service") == "anthropic":
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            # A "thinking" vision model (capabilities include "thinking", e.g.
            # Ollama's muse-glimmer) emits a separate reasoning stream that
            # shares this same token budget and is written before the actual
            # `content` answer. At 768 that reasoning alone can exhaust the
            # budget and finish_reason comes back "length" with content still
            # empty -- the desktop_observe caller then sees "empty response"
            # even though the model never failed, it just never got there.
            "max_tokens": 2048,
            "temperature": 0.2,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{shot.get('mime_type', 'image/jpeg')};base64,{shot['data']}"
                    }},
                ],
            }],
        }
        url = f"{_normalize_endpoint_url(endpoint)}/chat/completions"
        try:
            # 120s was sized for the old 768-token cap; a thinking model given
            # room to reason (max_tokens above) needs proportionally longer
            # wall-clock time to actually finish, or this read times out with
            # an empty httpx.ReadTimeout message before the model gets there.
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0)) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code != 200:
                    errors.append(f"{provider['name']}: HTTP {resp.status_code} {resp.text[:200]}")
                    continue
                data = resp.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                if text:
                    result: dict[str, Any] = {
                        "success": True,
                        "analysis": text,
                        "provider": provider["name"],
                        "model": model,
                    }
                    if unknown_provider_note:
                        # The caller asked for a provider that does not exist.
                        # It got an answer anyway, and is told plainly that its
                        # choice was not the one used.
                        result["provider_note"] = unknown_provider_note
                    return result
                errors.append(f"{provider['name']}: empty response")
        except Exception as exc:
            errors.append(f"{provider['name']}: {exc}")

    return {"success": False, "error": "All vision providers failed: " + " | ".join(errors)}


@register_tool(
    name="generate_image",
    description="Generate an image from a text prompt. Uses an OpenAI-compatible images endpoint if a provider has an imageModel configured, otherwise a local Stable Diffusion WebUI (AUTOMATIC1111) at localhost:7860. Saves the PNG to data/generated_images/ and shows it in the chat UI.",
)
async def generate_image(prompt: str, provider_id: str | None = None, size: str = "1024x1024") -> dict[str, Any]:
    """
    NAME
           generate_image - text-to-image via cloud endpoint or local SD WebUI

    SYNOPSIS
           generate_image(prompt: str, provider_id: str | None = None, size: str = "1024x1024") -> dict[str, Any]
    """
    import base64
    import datetime
    import httpx
    from python.harness.server import _get_providers, _normalize_endpoint_url

    if not prompt.strip():
        return {"success": False, "error": "prompt is required"}

    providers = await _get_providers()
    errors: list[str] = []
    image_b64: str | None = None
    used_backend = ""

    def _image_model(p: dict[str, Any]) -> str | None:
        cfg = p.get("config") or {}
        return cfg.get("imageModel") or cfg.get("image_model") or None

    candidates = [p for p in providers if _image_model(p)]
    if provider_id:
        candidates = [p for p in providers if p["id"] == provider_id]

    for p in candidates:
        cfg = p.get("config") or {}
        endpoint = str(cfg.get("endpoint", "")).strip() or "https://api.openai.com/v1"
        api_key = str(cfg.get("apiKey", "")).strip()
        model = _image_model(p) or "gpt-image-1"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0)) as client:
                resp = await client.post(
                    f"{_normalize_endpoint_url(endpoint)}/images/generations",
                    headers=headers,
                    json={"model": model, "prompt": prompt, "size": size, "n": 1, "response_format": "b64_json"},
                )
                if resp.status_code != 200:
                    errors.append(f"{p['name']}: HTTP {resp.status_code} {resp.text[:200]}")
                    continue
                data = resp.json()
                image_b64 = (data.get("data") or [{}])[0].get("b64_json")
                if image_b64:
                    used_backend = f"{p['name']} ({model})"
                    break
                errors.append(f"{p['name']}: empty image data")
        except Exception as exc:
            errors.append(f"{p['name']}: {exc}")

    if not image_b64:
        # Local Stable Diffusion WebUI (AUTOMATIC1111) fallback
        try:
            w, h = (int(x) for x in size.lower().split("x"))
        except ValueError:
            w = h = 1024
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=3.0)) as client:
                resp = await client.post(
                    "http://localhost:7860/sdapi/v1/txt2img",
                    json={"prompt": prompt, "width": w, "height": h, "steps": 25},
                )
                if resp.status_code == 200:
                    imgs = resp.json().get("images") or []
                    if imgs:
                        image_b64 = imgs[0].split(",", 1)[-1]
                        used_backend = "local-sd-webui"
        except Exception as exc:
            errors.append(f"local-sd-webui: {exc}")

    if not image_b64:
        return {
            "success": False,
            "error": "No image backend available. Add 'imageModel' to a provider config, or run a local "
                     "Stable Diffusion WebUI with --api on port 7860. Attempts: " + (" | ".join(errors) or "none"),
        }

    out_dir = _get_workspace_root() / "data" / "generated_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"img-{stamp}.png"
    out_path.write_bytes(base64.b64decode(image_b64))

    try:
        from python.harness.server import broadcast_notification
        await broadcast_notification("image.generated", {
            "prompt": prompt[:300],
            "path": str(out_path),
            "backend": used_backend,
            "image_base64": image_b64,
        })
    except Exception:
        log.debug("generate_image: UI notify failed", exc_info=True)

    return {"success": True, "path": str(out_path), "backend": used_backend}
