"""
python/harness/provider_probe.py -- is a remote provider really up? Asked only if the policy lets us ask.

This used to live inline in server.py's start-up provider pass. It sent ``GET <endpoint>/models`` with the stored
API key for every remote provider on every launch, whether or not that provider would ever be used, so a database
holding a Moonshot row contacted Moonshot (and handed it the key) each time Tala started. It now consults
``provider_policy`` first: a refused provider is reported offline and nothing is sent.

A remote provider that the policy allows is verified the way the local ones are: ask the endpoint, and check the
configured model is in the answer. An expired key, a wrong endpoint or no network connection must not render the
same green dot as a working one (UI-046).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from python.harness.inference_policy import model_id_matches
from python.harness.provider_policy import refusal

logger = logging.getLogger(__name__)


async def probe_remote_provider(
    provider: dict[str, Any],
    config: dict[str, Any],
    cached_models: dict[str, list[str] | None],
    normalize_endpoint: Callable[[str], str],
    client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> str:
    """"online" or "offline". Makes no request for a provider the policy refuses."""
    blocked = refusal(provider, requested=False)
    if blocked:
        logger.info("Provider '%s' is not probed: %s", provider.get("name"), blocked)
        return "offline"

    api_key = config.get("apiKey", "") or config.get("api_key", "")
    endpoint = config.get("endpoint", "")
    model = config.get("model", "")
    if not api_key:
        return "offline"
    if not endpoint:
        logger.error("Provider %s (%s) has an API key but no endpoint. Configure it in Settings -> Inference.",
                     provider.get("id"), provider.get("name", "Unnamed"))
        return "offline"
    if endpoint in cached_models:
        models = cached_models[endpoint]
        return "online" if models is not None and (not model or model in models) else "offline"

    try:
        url = normalize_endpoint(endpoint)
        async with client_factory(timeout=10.0) as client:
            response = await client.get(f"{url}/models", headers={"Authorization": f"Bearer {api_key}"})
        if response.status_code != 200:
            cached_models[endpoint] = None
            logger.warning("Provider %s returned HTTP %d when verifying its key.", provider.get("id"), response.status_code)
            return "offline"
        models = [m.get("id") for m in response.json().get("data", [])]
        cached_models[endpoint] = models
        if not model or model_id_matches(models, model):
            return "online"
        logger.warning("Provider %s does not list model %r (available: %s).", provider.get("id"), model, ", ".join(models[:5]))
        return "offline"
    except Exception as exc:
        cached_models[endpoint] = None
        logger.warning("Provider %s could not be reached: %s", provider.get("id"), exc)
        return "offline"
