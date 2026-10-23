"""
python/harness/provider_policy.py -- which inference providers Tala may use at all.

Tala is local and US-origin only. Two rules, enforced here rather than hoped for in whatever rows the
database happens to hold:

1. **A model from a non-US lab is never used**, even if the caller names it: Qwen (and its derivatives named
   "gwen"), DeepSeek, Mistral/Mixtral, Kimi/Moonshot and the other families listed in ``NON_US``. The marker is
   looked for in the provider's name, id, model, service and endpoint, so a custom endpoint on a banned host is
   caught as well as a banned model name.
2. **A remote provider is used only when remote is allowed**: the active profile sets ``allow_remote``, or
   ``TALA_ALLOW_REMOTE_PROVIDERS=1``, or the caller named that provider by id (the same rule the router applies to
   a profile: "reachable by naming them").

The decision is made in :func:`refusal`. ``provider_selection.skip_provider_attempt`` asks it before every
generation attempt and ``provider_probe`` asks it before every start-up probe, so a refused provider is neither
tried nor contacted, and no API key is sent to it.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from python.inference import profiles

#: Families whose origin is not the US. The test suite (tests/test_no_banned_models.py) holds the repository to
#: the Qwen/DeepSeek/Mistral subset; this list is the runtime superset. Matched at the start of a word.
NON_US = re.compile(
    r"(?<![a-z0-9])(qwen|gwen|deepseek|mistral|mixtral|kimi|moonshot|zhipu|baichuan|internlm|hunyuan|minimax|ernie)",
    re.IGNORECASE,
)

_UNSET: Any = object()


def _remote_allowed(profile: Any = _UNSET) -> bool:
    """Whether remote providers may be tried without being named."""
    if os.environ.get("TALA_ALLOW_REMOTE_PROVIDERS", "").strip() == "1":
        return True
    active = profiles.active() if profile is _UNSET else profile
    return bool(active is not None and getattr(active, "allow_remote", False))


def _config(provider: dict[str, Any]) -> dict[str, Any]:
    cfg = provider.get("config") or {}
    if isinstance(cfg, str):  # JSONB columns hold a JSON string in this codebase
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def refusal(provider: dict[str, Any], *, requested: bool = False, profile: Any = _UNSET) -> str | None:
    """Why this provider may not be used, or None if it may. Never contains a key."""
    cfg = _config(provider)
    haystack = " ".join(str(x) for x in (
        provider.get("name"), provider.get("id"), cfg.get("model"), cfg.get("service"), cfg.get("endpoint"),
    ) if x)
    marker = NON_US.search(haystack)
    if marker:
        return f"{marker.group(1)} is a non-US model family, and Tala uses US-origin models only"
    if provider.get("type") == "remote" and not requested and not _remote_allowed(profile):
        return ("it is a remote provider and Tala is local-only; name it explicitly, set allow_remote in the "
                "profile, or set TALA_ALLOW_REMOTE_PROVIDERS=1")
    return None
