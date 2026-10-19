"""
python/harness/provider_selection.py -- which providers a request may try.

One decision lives here, pulled out of server.py's generate loop because
that file is pinned by the size ratchet (tests/test_size_ratchet.py) and
because the decision is a predicate worth reading on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER_TIER_RANK = {"remote": 0, "local": 1, "embedded": 2}

GEMINI_OPENAI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENAI_ENDPOINT = "https://api.openai.com/v1"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1"

EMBEDDED_PROVIDER: dict[str, Any] = {
    "id": "embedded-builtin",
    "type": "embedded",
    "name": "Tala Embedded Engine",
    "priority": 999,
    "status": "online",
    "config": {"model": "bartowski/Llama-3.2-3B-Instruct-GGUF"},
    "locked": True,
}


@dataclass
class ProviderRoute:
    """
    NAME
           ProviderRoute - ordered chain plus the requested/forced heads

    SYNOPSIS
           route = resolve_provider_chain(providers, request_provider_id=...)

    DESCRIPTION
           ``providers`` is the try-order for this turn. ``request_provider``
           is the session/request override; ``forced_provider`` is a pin or
           FORCE checkbox. ``strict`` means no fallback.
    """

    providers: list[dict[str, Any]]
    request_provider: dict[str, Any] | None = None
    forced_provider: dict[str, Any] | None = None
    strict: bool = False
    notices: list[str] = field(default_factory=list)


def tier_sort_providers(provider_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    NAME
           tier_sort_providers - remote, then local, then embedded

    SYNOPSIS
           tier_sort_providers(provider_list) -> list[dict]

    DESCRIPTION
           Stable within a tier so DB priority still orders peers. Raw
           priority alone cannot be trusted: inference.save_providers
           persists it as a positional index, so a Settings save can drift
           a remote provider past every local model.
    """
    return sorted(
        provider_list,
        key=lambda p: PROVIDER_TIER_RANK.get(p.get("type"), 3),
    )


def ensure_embedded_provider(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    NAME
           ensure_embedded_provider - append the built-in engine if missing

    SYNOPSIS
           ensure_embedded_provider(providers) -> list[dict]
    """
    if any(p.get("id") == "embedded-builtin" for p in providers):
        return list(providers)
    return list(providers) + [dict(EMBEDDED_PROVIDER)]


def skip_provider_attempt(
    provider: dict[str, Any],
    forced_provider: dict[str, Any] | None,
    request_provider: dict[str, Any] | None,
) -> bool:
    """
    Whether this provider should be passed over without being tried.

    A provider the caller asked for by id is exempt from the offline skip,
    exactly as a forced one is.

    `provider_id` put the requested provider first in the chain and then let
    this skip drop it, so naming a provider whose cached status was "offline"
    silently produced an answer from a different model. Every desktop-control
    run aimed at Gemini was served by a local qwen instead, and the reply
    looked like an ordinary success -- I reported one of those to Steven as a
    Gemini result before reading the log and finding otherwise.

    `status` is a cached probe, not ground truth: it goes stale, and it is
    exactly what an explicit choice should be able to override. If the
    provider really is down the request fails against it and the chain carries
    on to the next one, which is the behaviour a caller naming a provider
    expects.
    """
    if provider.get("status") != "offline":
        return False

    # Checked ahead of the embedded-builtin exemption so that the "requested
    # by id" line is logged for exactly the providers it was logged for
    # before this moved out of server.py.
    if bool(request_provider) and provider["id"] == request_provider["id"]:
        logger.info(
            "Provider '%s' is marked offline but was requested by id; trying it.",
            provider["name"],
        )
        return False

    if provider["id"] == "embedded-builtin":
        return False
    if bool(forced_provider) and provider["id"] == forced_provider["id"]:
        return False

    logger.info("Skipping offline provider '%s'", provider["name"])
    return True


def _default_remote_endpoint(service: str) -> str:
    """
    NAME
           _default_remote_endpoint - well-known OpenAI-compat base URL

    SYNOPSIS
           _default_remote_endpoint(service: str) -> str
    """
    key = (service or "").strip().lower()
    if key == "gemini":
        return GEMINI_OPENAI_ENDPOINT
    if key == "openai":
        return OPENAI_ENDPOINT
    if key == "anthropic":
        return ANTHROPIC_ENDPOINT
    return ""


def remote_provider_ready(provider: dict[str, Any]) -> tuple[bool, str]:
    """
    NAME
           remote_provider_ready - Gemini/OpenAI-compat need a URL and a key

    SYNOPSIS
           ok, reason = remote_provider_ready(provider)

    DESCRIPTION
           Gemini over the OpenAI-compatible endpoint needs a non-empty base
           URL plus the API key. An empty URL is filled from the known
           service default; a missing key is a hard failure so we do not
           silently fall through to the embedded engine.
    """
    if provider.get("type") != "remote":
        return True, ""
    cfg = provider.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    service = str(cfg.get("service") or "").strip().lower()
    endpoint = str(cfg.get("endpoint") or "").strip()
    api_key = str(cfg.get("apiKey") or "").strip()
    if not endpoint:
        endpoint = _default_remote_endpoint(service)
        if endpoint:
            cfg["endpoint"] = endpoint
            provider["config"] = cfg
    name = provider.get("name") or provider.get("id") or "remote provider"
    if not endpoint:
        return False, (
            f"Remote provider '{name}' has no endpoint/base URL. "
            "Set it in Settings → Inference Providers."
        )
    if not api_key:
        return False, (
            f"Remote provider '{name}' has no API key. "
            "Add the key in Settings → Inference Providers."
        )
    return True, ""


def resolve_provider_chain(
    providers: list[dict[str, Any]],
    *,
    request_provider_id: str = "",
    pinned_id: str = "",
    pin_strict: bool = False,
) -> ProviderRoute:
    """
    NAME
           resolve_provider_chain - session override first, then fallback chain

    SYNOPSIS
           route = resolve_provider_chain(
               providers,
               request_provider_id=session_id,
               pinned_id=os.environ.get("TALA_PIN_PROVIDER", ""),
               pin_strict=os.environ.get("TALA_PIN_STRICT") == "1",
           )

    DESCRIPTION
           Documented fallback rules:

           1. A session / request ``provider_id`` is tried first on every
              turn of the same generate call (tool loops, web_search
              follow-ups, agent rounds).
           2. Else a FORCE checkbox or ``TALA_PIN_PROVIDER`` is first.
           3. ``TALA_PIN_STRICT=1`` drops the rest of the chain.
           4. Remaining providers stay in remote → local → embedded order.
           5. The embedded engine is last-resort, never a silent substitute
              for a provider the user named. A failure of the named
              provider must surface the real error and a visible notice
              before anyone else answers.
    """
    providers = ensure_embedded_provider(list(providers))
    request_id = str(request_provider_id or "").strip()
    pin = str(pinned_id or "").strip()

    request_provider = None
    forced_provider = None
    for p in providers:
        if request_id and p.get("id") == request_id:
            request_provider = p
        if pin:
            if p.get("id") == pin or p.get("name") == pin:
                forced_provider = p
        elif (p.get("config") or {}).get("is_forced"):
            forced_provider = p

    if pin and not forced_provider:
        raise RuntimeError(
            f"TALA_PIN_PROVIDER={pin!r} matches no configured provider. "
            f"Available: {', '.join(repr(p.get('id')) for p in providers)}"
        )

    if pin and forced_provider and pin_strict:
        logger.info(
            "Provider pinned strictly to '%s'; no fallback.",
            forced_provider["name"],
        )
        return ProviderRoute(
            providers=[forced_provider],
            request_provider=None,
            forced_provider=forced_provider,
            strict=True,
        )

    if request_provider:
        logger.info(
            "Request-selected provider active: '%s' (Preferred First)",
            request_provider["name"],
        )
        others = tier_sort_providers(
            [p for p in providers if p["id"] != request_provider["id"]]
        )
        chain = [request_provider] + others
    elif forced_provider:
        logger.info("Forced provider active: '%s' (Preferred First)", forced_provider["name"])
        others = tier_sort_providers(
            [p for p in providers if p["id"] != forced_provider["id"]]
        )
        chain = [forced_provider] + others
    else:
        chain = tier_sort_providers(providers)
        logger.info(
            "Enqueued %d providers in tier order (remote -> local -> embedded).",
            len(chain),
        )

    return ProviderRoute(
        providers=chain,
        request_provider=request_provider,
        forced_provider=forced_provider,
        strict=False,
    )


def preferred_provider(route: ProviderRoute) -> dict[str, Any] | None:
    """The provider the user asked for, or the forced/pinned one."""
    return route.request_provider or route.forced_provider


def fallback_allowed(route: ProviderRoute, failed: dict[str, Any]) -> bool:
    """
    NAME
           fallback_allowed - whether the chain may continue after a failure

    SYNOPSIS
           fallback_allowed(route, failed_provider) -> bool

    DESCRIPTION
           Strict pins never fall back. Otherwise a failed preferred
           provider may continue down the documented chain, but the caller
           must emit a visible notice (see ``format_fallback_notice``).
    """
    if route.strict:
        return False
    return True


def format_fallback_notice(
    from_provider: dict[str, Any] | str,
    to_provider: dict[str, Any] | str,
    reason: str,
) -> str:
    """
    NAME
           format_fallback_notice - user-visible takeover text

    SYNOPSIS
           format_fallback_notice(from_provider, to_provider, reason) -> str
    """
    def _name(p: dict[str, Any] | str) -> str:
        if isinstance(p, dict):
            return str(p.get("name") or p.get("id") or "unknown")
        return str(p)

    clean = (reason or "failed").strip() or "failed"
    return (
        f"Provider '{_name(from_provider)}' failed ({clean}). "
        f"Fell back to '{_name(to_provider)}'."
    )
