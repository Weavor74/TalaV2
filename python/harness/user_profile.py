"""
user_profile.py — one gated way to read who the user is.

Steven's correction, and the design this module implements:

    "You cant inject the profile cluster in its entirety. You can inject the
    profile name and DOB but all other stuff should be looked up. The DOB is
    only for astrology comparison."

So the split is:

* **Name** is injected into the system prompt. It is the minimum needed for
  Tala to know who she is talking to, and it is not sensitive.
* **Date of birth** stays exactly where it already is — the astrology handlers
  read it straight from ``profiles.config`` (``identity.birthdate`` →
  ``pii_vault.dob`` → ``birthdate``). It is not persona colour and does not
  belong in prompt text.
* **Everything else** — about, roles, skills, projects, connections,
  preferences — is *looked up on demand* through :func:`lookup`, never
  preloaded.

Doing it as a lookup rather than as retrieval over the profile node is what
makes UI-058 possible at all. The six Privacy & Visibility selectors are
per-section, and a full-text hit returns an undifferentiated blob that cannot
honour "skills: public" while withholding "connections: connections". A single
function with a section argument is a chokepoint those settings can govern.

Three rules here, each closing a finding:

* **PII is never returned.** ``pii_vault`` holds a legal name, DOB, email,
  phone and identity-document registries. No visibility setting exposes it and
  no section name reaches it (UI-054). The astrology path reads the database
  directly and is unaffected.
* **Visibility is enforced, not decorative.** ``config.privacy`` was written as
  an empty dict and read by nothing (UI-058). It is read here.
* **Unknown means denied.** A section with no visibility setting is withheld
  rather than served, so adding a profile section cannot silently publish it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

PROFILE_ID = "master-user"

#: Sections a lookup may return, and nothing else. ``pii_vault`` is absent by
#: design and ``privacy`` itself is not readable through this path.
LOOKUPABLE = (
    "identity", "about", "roles", "skills", "projects",
    "connections", "preferences", "rp_persona",
)

#: Never returned by lookup, at any visibility, to any caller.
FORBIDDEN = frozenset({"pii_vault", "privacy"})

#: Identity keys safe to surface. Deliberately an allowlist rather than a
#: blocklist: identity is the section most likely to gain a sensitive field
#: later, and an allowlist fails closed when it does.
IDENTITY_PUBLIC = (
    "name", "username", "type", "headline", "location",
    "timezone", "language", "pronouns", "nickname", "motto",
)

#: Visibility values that permit the *local* agent to read a section.
#: "connections" means peers the user has connected with — Tala is not one, so
#: it withholds, matching Steven's invite-only model ("I have to invite her to
#: access"). "private" is the user alone.
AGENT_READABLE = frozenset({"public"})

DEFAULT_VISIBILITY = "private"


def _decode(raw: Any) -> dict[str, Any]:
    """
    Return ``config`` as a dict.

    ``profiles.config`` is double-encoded: a jsonb codec plus ``json.dumps`` at
    the call sites means one ``loads`` yields a string again. Unwraps until it
    is a mapping rather than assuming a fixed depth, because the encodings are
    not applied consistently across writers.
    """
    seen = 0
    while isinstance(raw, (str, bytes)) and seen < 5:
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        seen += 1
    return raw if isinstance(raw, dict) else {}


PROFILE_NODE = "concept-user-profile"


def _load_config_from_node() -> dict[str, Any]:
    """
    Read the profile out of its HTML-NN, which is the authored source of truth.

    Returns ``{}`` — not a partial dict — when the node is missing, unparseable,
    or in the legacy flat format that carries no ``config``. That distinction is
    the whole point: UI-099 was a node stuck in exactly that legacy shape, and a
    reader that accepted it silently served a profile with most sections
    missing. Empty here means "ask the database", and is deliberately
    indistinguishable from "no node", because both are equally unusable.
    """
    try:
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir
        from sdk.agent.parser import parse_file

        path = resolve_active_nodes_dir() / f"{PROFILE_NODE}.html"
        if not path.exists():
            return {}
        state = parse_file(str(path)).state_data or {}
    except Exception as exc:
        log.warning("user_profile: could not parse %s: %s", PROFILE_NODE, exc)
        return {}

    config = _decode(state.get("config"))
    if not config:
        log.info(
            "user_profile: %s carries no config section (legacy flat node); "
            "falling back to the database.", PROFILE_NODE,
        )
    return config


async def _load_config_from_db() -> dict[str, Any]:
    """Read the master user's profile config from Postgres."""
    from python.harness import db
    try:
        row = await db.fetchrow(
            "SELECT config FROM profiles WHERE id = $1", PROFILE_ID
        )
    except Exception as exc:
        log.warning("user_profile: could not read the profile row: %s", exc)
        return {}
    if not row:
        return {}
    # PII is encrypted at rest (UI-055); decrypt so the lookup gate sees real
    # values and can make a real decision about them.
    from python.harness.credential_crypto import decrypt_section
    config = _decode(row["config"])
    if isinstance(config.get("pii_vault"), dict):
        config["pii_vault"] = decrypt_section(config["pii_vault"])
    return config


async def load_config() -> dict[str, Any]:
    """
    The user's profile, node first.

    Steven's standing requirement is that everything the harness consumes is an
    HTML-NN, so the node is read first and the database is the fallback rather
    than the source. The fallback is not optional: the node write can fail, and
    when it did (UI-056) the data looked lost because every reader trusted the
    node alone.
    """
    config = _load_config_from_node()
    if config:
        return config
    return await _load_config_from_db()


def _name_from(config: dict[str, Any]) -> str:
    """
    The user's preferred display name.

    Nickname first: someone who set one is telling you what to call them. Falls
    back to the identity name, then the username. Never the legal name — that
    lives in ``pii_vault`` and is not for conversation.
    """
    identity = config.get("identity") if isinstance(config.get("identity"), dict) else {}
    for key in ("nickname", "name", "username"):
        value = identity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = config.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _visibility_of(config: dict[str, Any], section: str) -> str:
    """
    The configured visibility for *section*.

    Absent means :data:`DEFAULT_VISIBILITY` — denied. A new profile section must
    be published deliberately, never by having been forgotten.
    """
    privacy = config.get("privacy")
    if not isinstance(privacy, dict):
        return DEFAULT_VISIBILITY
    value = privacy.get(section)
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_VISIBILITY
    return value.strip().lower()


def _filter_identity(section_data: Any) -> Any:
    """Reduce identity to its allowlisted keys."""
    if not isinstance(section_data, dict):
        return section_data
    return {k: v for k, v in section_data.items() if k in IDENTITY_PUBLIC and v}


async def system_prompt_line() -> str:
    """
    The single line injected into the system prompt, or ``""``.

    Name only, by design. Everything else is a lookup away, and the model is
    told so explicitly — otherwise it has no way to know the capability exists
    and will simply assume it knows nothing about the user.
    """
    config = await load_config()
    name = _name_from(config)
    if not name:
        return ""
    readable = [s for s, vis in (await available_sections()).items() if vis == "readable"]
    if not readable:
        return f"[User] You are speaking with {name}."
    return (
        f"[User] You are speaking with {name}. "
        f"Look up anything else you need about them with the "
        f"`user_profile_lookup` tool (sections: "
        f"{', '.join(readable)}) rather than guessing or asking them to "
        f"repeat what they have already told you."
    )


async def lookup(section: str, actor: str = "default") -> dict[str, Any]:
    """
    Read one section of the user's profile, subject to its visibility.

    Returns ``{"section", "allowed", "data", "reason"}``. A withheld section
    returns ``allowed: False`` and says why rather than returning empty data,
    because "you have no skills recorded" and "you may not read their skills"
    must not look the same to the caller.

    *actor* names the agent asking, so a grant can be made to one agent rather
    than to every agent at once -- Steven's model treats an agent as a
    principal assigned like a peer (Q-63).
    """
    requested = str(section or "").strip().lower()

    if not requested:
        return {
            "section": "", "allowed": False, "data": None,
            "reason": f"Name a section: {', '.join(LOOKUPABLE)}.",
        }

    if requested in FORBIDDEN:
        log.warning("user_profile: refused a lookup of %r", requested)
        reason = (
            "That section holds personally identifying material -- legal name, "
            "date of birth, contact details, identity documents -- and is not "
            "readable by any agent."
            if requested == "pii_vault" else
            "The visibility settings are access-control configuration, not "
            "profile content, and are not readable through this tool."
        )
        return {
            "section": requested, "allowed": False, "data": None, "reason": reason,
        }

    if requested not in LOOKUPABLE:
        return {
            "section": requested, "allowed": False, "data": None,
            "reason": f"Unknown section. Available: {', '.join(LOOKUPABLE)}.",
        }

    config = await load_config()
    if not config:
        return {
            "section": requested, "allowed": False, "data": None,
            "reason": "The user profile could not be read.",
        }

    vis = _visibility_of(config, requested)

    # The visibility setting is the subsystem's own rule; an explicit grant
    # supersedes it, and lockdown overrides both (UI-058, UI-101). Composed
    # rather than replaced: a user who never opens the access panel keeps
    # exactly the behaviour their privacy dropdowns describe.
    from python.harness.access import AGENT, Principal, resolve

    decision = await resolve(
        Principal(AGENT, actor or "default"),
        resource=f"profile:{requested}",
        permission="read",
        fallback_allowed=vis in AGENT_READABLE,
        fallback_reason=(
            f"visibility is '{vis}'"
            if vis in AGENT_READABLE else
            f"The user has set {requested} to '{vis}', which does not include "
            f"this agent. They can change it in Settings › User Profile › "
            f"Preferences & Privacy, or grant access to this section directly."
        ),
    )

    if not decision.allowed:
        log.info("user_profile: withheld %r -- %s", requested, decision.reason)
        return {
            "section": requested, "allowed": False, "data": None, "visibility": vis,
            "reason": decision.reason,
        }

    data = config.get(requested)
    if requested == "identity":
        data = _filter_identity(data)

    if data in (None, {}, []):
        return {
            "section": requested, "allowed": True, "data": data,
            "visibility": vis,
            "reason": "The user has not filled in this section.",
        }

    return {
        "section": requested, "allowed": True, "data": data,
        "visibility": vis, "reason": "",
    }


async def available_sections() -> dict[str, str]:
    """``{section: 'readable' | visibility}`` — what a lookup would allow."""
    config = await load_config()
    out: dict[str, str] = {}
    for section in LOOKUPABLE:
        vis = _visibility_of(config, section)
        out[section] = "readable" if vis in AGENT_READABLE else vis
    return out
