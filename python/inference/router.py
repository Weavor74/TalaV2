"""
router.py -- which providers a turn tries, and what each one is shown.

With a profile in force the chain for a turn is built from the profile's
roles rather than from every model Ollama happens to have installed:

* an image is attached  -> vision, then main, then fallback
* otherwise             -> main, then fallback

"main" is the embedded llama-server (gpt-oss on Kagura); "vision" and
"fallback" are Ollama models named by the profile. A failure or timeout in one
simply moves the existing generate loop on to the next, so fallback needs no
extra machinery here -- only the right order.

If the caller named a provider (``provider_id``), pinned one, or FORCE-checked
one, that choice is respected exactly as ``resolve_provider_chain`` ordered it.
The router never overrides an explicit choice.

Remote (cloud) providers are left out of an automatic chain unless the profile
sets ``allow_remote``; they are still reachable by naming them.

Images only go to providers that can see them. Everything else gets the text
with a one-line note, because a text-only model handed an ``image_url`` part
either errors or silently ignores it.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from python.inference.profiles import OllamaRole, Profile

logger = logging.getLogger(__name__)

EMBEDDED_ID = "embedded-builtin"
VISION_ID = "profile-vision"
FALLBACK_ID = "profile-fallback"

IMAGE_NOTE = "[An image was attached, but this model cannot see images; answer from the text alone.]"

#: Model-name hints for a local model that is vision-capable but not marked so.
_VISION_HINTS = ("llava", "gemma3", "gemma-3", "vision", "-vl", "granite3.2-vision", "llama3.2-vision")


def _has_images(messages: list[dict[str, Any]]) -> bool:
    for m in messages:
        content = m.get("content")
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        ):
            return True
    return False


def _provider_sees_images(provider: dict[str, Any]) -> bool:
    ptype = provider.get("type")
    if ptype == "remote":
        return True
    if ptype == "embedded":
        return False
    cfg = provider.get("config") or {}
    if "vision" in cfg:
        return bool(cfg["vision"])
    model = str(cfg.get("model", "")).lower()
    return any(h in model for h in _VISION_HINTS)


def _strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace image parts with a text note; plain-text messages are untouched."""
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        text = "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
        dropped = any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)
        text = f"{text}\n{IMAGE_NOTE}".strip() if dropped else text
        out.append({**m, "content": text})
    return out


def messages_for(provider: dict[str, Any], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages this provider should receive."""
    if _has_images(messages) and not _provider_sees_images(provider):
        return _strip_images(messages)
    return messages


def to_ollama_native_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI content lists -> Ollama /api/chat (``content`` text + ``images`` base64).

    /api/chat rejects a ``content`` array; images ride in a separate field and
    must be bare base64, not a data URL.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        texts: list[str] = []
        images: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                images.append(url.split(",", 1)[1] if url.startswith("data:") and "," in url else url)
        native = {**m, "content": "\n".join(t for t in texts if t)}
        if images:
            native["images"] = images
        out.append(native)
    return out


def _ollama_provider(role: OllamaRole, profile: Profile, *, provider_id: str, name: str,
                     existing: dict[str, Any] | None, priority: int) -> dict[str, Any]:
    """A provider dict for an Ollama-served role, reusing a configured row if there is one."""
    provider = copy.deepcopy(existing) if existing else {
        "id": provider_id,
        "type": "local",
        "name": name,
        "priority": priority,
        "locked": False,
    }
    cfg = dict(provider.get("config") or {})
    cfg.update({
        "engine": "ollama",
        "endpoint": f"{profile.ollama_url}/v1",
        "model": role.model,
        "num_ctx": role.num_ctx,
        "keep_alive": role.keep_alive,
        "vision": role.vision,
    })
    if role.num_gpu is not None:
        cfg["num_gpu"] = role.num_gpu  # CPU-only: no VRAM, so no GPU room has to be made for it
    else:
        cfg.pop("num_gpu", None)
    provider["config"] = cfg
    # status is a cached probe that goes stale (Ollama restarts, a model gets
    # pulled); a profile role must be tried, not skipped on yesterday's result.
    provider["status"] = "unknown"
    return provider


def _main_provider(providers: list[dict[str, Any]], profile: Profile) -> dict[str, Any]:
    existing = next((p for p in providers if p.get("id") == EMBEDDED_ID), None)
    main = copy.deepcopy(existing) if existing else {
        "id": EMBEDDED_ID, "type": "embedded", "name": "Tala Embedded Engine",
        "priority": 1, "locked": True,
    }
    main["name"] = f"Tala main ({profile.main.label})"
    main["config"] = {**(main.get("config") or {}), "model": profile.main.label}
    main["status"] = "online"
    return main


def apply(route: Any, profile: Profile | None, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The providers to try for this turn.

    ``route`` is a ``ProviderRoute``. An explicit choice (a named provider, a
    pin, FORCE, or a strict pin) is returned exactly as ``resolve_provider_chain``
    ordered it; otherwise the profile's roles decide.
    """
    if profile is None or route.request_provider or route.forced_provider or route.strict:
        return route.providers
    return _build_chain(route.providers, profile, images=_has_images(messages))


def _build_chain(
    providers: list[dict[str, Any]],
    profile: Profile | None,
    *,
    images: bool,
) -> list[dict[str, Any]]:
    """The provider order for an unpinned turn under ``profile``.

    Callers must skip this when the request named, pinned or forced a provider.
    With no profile the chain is returned unchanged.
    """
    if profile is None:
        return providers

    def configured(model: str) -> dict[str, Any] | None:
        return next(
            (p for p in providers
             if p.get("type") == "local" and (p.get("config") or {}).get("model") == model),
            None,
        )

    chain: list[dict[str, Any]] = []
    if images and profile.vision is not None:
        chain.append(_ollama_provider(
            profile.vision, profile, provider_id=VISION_ID, name=f"Vision ({profile.vision.model})",
            existing=configured(profile.vision.model), priority=0,
        ))
    chain.append(_main_provider(providers, profile))
    if profile.fallback is not None:
        chain.append(_ollama_provider(
            profile.fallback, profile, provider_id=FALLBACK_ID,
            name=f"Fallback ({profile.fallback.model})",
            existing=configured(profile.fallback.model), priority=2,
        ))
    if profile.allow_remote:
        chain.extend(p for p in providers if p.get("type") == "remote")
    elif any(p.get("type") == "remote" for p in providers):
        logger.info(
            "Profile %r is local-only; remote providers are held back unless named "
            "(set allow_remote to include them).", profile.name,
        )
    return chain
