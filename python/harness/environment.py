"""
environment.py — the USB / LOCAL / REMOTE presets, and lockdown.

Steven, on the three buttons (Q-25):

    "If you select the USB button you are running from a USB and all data
    stored is on USB. The embedded engine is all we have access to. Think of it
    as a troubleshooting mode. If you selected local then you are using local
    non-usb storage, inference engine such as ollama or lmstudio. And cloud
    means everything may be remote... It's also okay to think of it as presets."

and on lockdown (Q-25 follow-up):

    "This should be a separate thing. Lockdown is a security override. This
    should probably include the strictest guardrail settings possible. Local is
    just the operational environment and these are the best settings for it."

They were a CSS class toggle and a log line (UI-044). That mattered more than a
dead button usually does, because **USB (ISOLATED) reads as a security
control**: someone selecting it would reasonably believe they had switched the
system into an isolated mode, and nothing changed.

Two orthogonal axes, deliberately kept apart:

* **Environment** is where you are working — which storage and which engines
  make sense here. It is a preset, chosen for convenience.
* **Lockdown** is a security override, chosen because something is wrong. It
  restricts inference independently of the environment and does not change the
  data root, because moving a user's data is not a safety measure.

They can land on the same inference restriction by different routes. That is
fine and is not a reason to merge them: the reason you are restricted is part
of what the state means.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

USB = "usb"
LOCAL = "local"
REMOTE = "remote"

ENVIRONMENTS = (USB, LOCAL, REMOTE)
DEFAULT_ENVIRONMENT = LOCAL

#: Which inference provider types each environment permits.
#:
#: USB is the narrowest configuration the system can run in -- Steven calls it
#: a troubleshooting mode -- so it is embedded only, not merely local.
PERMITTED_INFERENCE: dict[str, frozenset[str]] = {
    USB: frozenset({"embedded"}),
    LOCAL: frozenset({"embedded", "local"}),
    REMOTE: frozenset({"embedded", "local", "remote"}),
}

#: Lockdown's own restriction, applied on top of whatever the environment says.
LOCKDOWN_INFERENCE = frozenset({"embedded", "local"})

#: Which storage each environment permits, for the panels that consume it.
PERMITTED_STORAGE: dict[str, str] = {
    USB: "usb",
    LOCAL: "local",
    REMOTE: "any",
}

DESCRIPTIONS = {
    USB: "Running from removable media. Embedded engine only, storage on the "
         "stick. The narrowest configuration the system can run in.",
    LOCAL: "Local, non-removable storage with local engines such as Ollama or "
           "LM Studio. No cloud inference.",
    REMOTE: "Anything may be remote, including hosted inference and cloud "
            "storage.",
}

_SETTINGS_ID = "environment"


def normalise(value: Any) -> str:
    name = str(value or "").strip().lower()
    return name if name in ENVIRONMENTS else DEFAULT_ENVIRONMENT


async def get_state() -> dict[str, Any]:
    """
    The current environment and lockdown state.

    Reads a single row rather than a config blob so that the value cannot be
    lost inside an unrelated settings save -- which is how the profile data
    appeared to vanish (UI-056).
    """
    from python.harness import db

    environment, lockdown = DEFAULT_ENVIRONMENT, False
    try:
        row = await db.fetchrow(
            "SELECT value FROM app_settings WHERE key = $1", _SETTINGS_ID
        )
        if row and row["value"]:
            import json
            stored = row["value"]
            if isinstance(stored, str):
                stored = json.loads(stored)
            if isinstance(stored, dict):
                environment = normalise(stored.get("environment"))
                lockdown = bool(stored.get("lockdown"))
    except Exception as exc:
        # Default rather than fail: an unreadable setting must not stop
        # inference, and LOCAL is the conservative middle.
        log.warning("environment: could not read the stored state (%s); "
                    "defaulting to %s.", exc, DEFAULT_ENVIRONMENT)

    return build_state(environment, lockdown)


def build_state(environment: str, lockdown: bool) -> dict[str, Any]:
    environment = normalise(environment)
    permitted = PERMITTED_INFERENCE[environment]
    if lockdown:
        permitted = permitted & LOCKDOWN_INFERENCE

    return {
        "environment": environment,
        "lockdown": bool(lockdown),
        "description": DESCRIPTIONS[environment],
        "permitted_inference": sorted(permitted),
        "permitted_storage": PERMITTED_STORAGE[environment],
        # Which of the two is actually doing the restricting, so the panel can
        # explain a blocked provider rather than just greying it out.
        "restricted_by": (
            "lockdown" if lockdown and permitted != PERMITTED_INFERENCE[environment]
            else "environment" if permitted != PERMITTED_INFERENCE[REMOTE]
            else "nothing"
        ),
    }


async def set_state(environment: str | None = None,
                    lockdown: bool | None = None) -> dict[str, Any]:
    """Change the environment, lockdown, or both. Returns the resulting state."""
    from python.harness import db
    import json

    current = await get_state()
    new_env = normalise(environment) if environment is not None else current["environment"]
    new_lock = bool(lockdown) if lockdown is not None else current["lockdown"]

    await db.execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        _SETTINGS_ID,
        json.dumps({"environment": new_env, "lockdown": new_lock}),
        caller="environment.set",
    )

    state = build_state(new_env, new_lock)
    log.info("environment: %s, lockdown=%s, inference limited to %s",
             new_env, new_lock, state["permitted_inference"])
    return state


def filter_providers(providers: list[dict[str, Any]],
                     state: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """
    Split providers into permitted and blocked.

    Returns both halves rather than silently dropping the blocked ones: a
    provider that vanishes from the chain with no explanation is
    indistinguishable from one that was never configured, and that is the
    failure mode this codebase keeps producing.
    """
    permitted_types = set(state.get("permitted_inference") or [])
    allowed, blocked = [], []
    for provider in providers:
        if str(provider.get("type", "")).lower() in permitted_types:
            allowed.append(provider)
        else:
            blocked.append(provider)
    return allowed, blocked
