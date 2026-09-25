"""
python/harness/chat_mode.py — Active chat mode (rp / hybrid / assistant)
=======================================================================

The single source of truth for which conversational mode Tala is in.

This lives in its own module, rather than in server.py, so that
python/harness/memory.py can read the mode without importing server.py
back (a circular import). It is deliberately dependency-free.

Storage: a small JSON file, ``chat_mode.json``, in the resolved user-data
root — the same root nodes/ and workflows/ live under, so the mode travels
with a portable install instead of scattering into an OS profile
directory. See sdk/agent/documentation_nodes.resolve_user_data_dir().

Historical note: the mode used to be read from two disagreeing places --
this file and ``agents.config['chat_mode']`` in Postgres. Nothing ever
wrote the latter, so any code consulting it saw a permanently empty value
and silently took the wrong branch (most visibly, the persona never
received an emotional state at inference time). Every consumer now goes
through get_current_chat_mode() here.

NOTE: This module was reconstructed on 2026-08-12 after an automated
rollback deleted it (it had never been committed to git). The behaviour
below is derived from its call sites in server.py and memory.py; if
anything about mode handling looks subtly off, this file is the first
place to check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

#: The modes the UI offers. "rp" and "hybrid" are in-character modes;
#: "assistant" is the plain, tool-forward working mode.
VALID_CHAT_MODES: tuple[str, ...] = ("rp", "hybrid", "assistant")

#: Falling back to roleplay preserves the historical default: Tala's
#: persona is the product, and an unreadable/missing mode file should not
#: silently drop her into a different character.
DEFAULT_CHAT_MODE = "rp"

#: Modes in which the astrology-derived emotional state is computed and
#: injected into context. The assistant mode deliberately does not carry
#: an emotional overlay.
EMOTIONAL_CHAT_MODES: frozenset[str] = frozenset({"rp", "hybrid"})

_CHAT_MODE_FILENAME = "chat_mode.json"


def _resolve_user_data_dir() -> Path:
    """
    Resolve the user-data root, preferring the canonical resolver.

    Imported lazily and guarded so this module stays usable (and testable)
    even if the sdk package is unavailable; the fallback mirrors the same
    TALA_USER_DATA-first, install-directory-second policy.
    """
    try:
        from sdk.agent.documentation_nodes import resolve_user_data_dir

        return resolve_user_data_dir()
    except Exception:
        user_data = os.environ.get("TALA_USER_DATA")
        if user_data:
            return Path(user_data).expanduser().resolve()
        return Path(__file__).resolve().parent.parent.parent


def _get_chat_mode_file_path() -> Path:
    """Absolute path of chat_mode.json under the active user-data root."""
    return _resolve_user_data_dir() / _CHAT_MODE_FILENAME


def _normalize_chat_mode(mode: object) -> str:
    """Coerce arbitrary input to a valid mode, falling back to the default."""
    candidate = str(mode or "").strip().lower()
    return candidate if candidate in VALID_CHAT_MODES else DEFAULT_CHAT_MODE


def get_current_chat_mode() -> str:
    """
    Current chat mode, always one of VALID_CHAT_MODES.

    Never raises: a missing, empty, or corrupt chat_mode.json yields the
    default rather than propagating an error into an inference request.
    """
    path = _get_chat_mode_file_path()
    try:
        if not path.is_file():
            return DEFAULT_CHAT_MODE
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return _normalize_chat_mode(data.get("mode"))
        return _normalize_chat_mode(data)
    except Exception as exc:
        log.warning("chat_mode: could not read %s (%s) — using %r", path, exc, DEFAULT_CHAT_MODE)
        return DEFAULT_CHAT_MODE


def save_current_chat_mode(mode: str) -> str:
    """
    Persist `mode` and return the normalized value actually written.

    Raises ValueError on an unrecognised mode: a caller explicitly setting
    the mode (the UI's mode buttons, agent.set_chat_mode) should be told it
    asked for something invalid rather than silently getting the default.
    """
    candidate = str(mode or "").strip().lower()
    if candidate not in VALID_CHAT_MODES:
        raise ValueError(f"mode must be one of {list(VALID_CHAT_MODES)}, got {mode!r}")

    path = _get_chat_mode_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": candidate}, indent=2), encoding="utf-8")
    log.info("chat_mode: set to %r (%s)", candidate, path)
    return candidate


def is_emotional_mode(mode: str | None = None) -> bool:
    """
    Whether the given mode (default: the current one) carries the
    astrology-derived emotional state.
    """
    resolved = _normalize_chat_mode(mode) if mode is not None else get_current_chat_mode()
    return resolved in EMOTIONAL_CHAT_MODES


async def determine_effective_mode(raw_user_prompt: str) -> str:
    """
    The mode this turn should use: the persisted setting, or a per-turn
    override from the embedded engine's Jev-mode structured decision.

    Ephemeral by design: this never calls save_current_chat_mode() and never
    touches which persona/agent is active -- the manual mode buttons remain
    the only way to actually change identity or clear history. A single
    ambiguous message should not flip who Tala is for the rest of the
    conversation.

    The llama_server_manager import stays inside this function (the module
    docstring's "dependency-free" is about top-level imports memory.py
    relies on to avoid a circular import; this is a lazy, call-time-only
    import, same pattern as _resolve_user_data_dir() above). Deliberately
    does not attempt a decision at all -- not even under a timeout -- unless
    the engine is already running: decide() calls an internal ensure_running()
    that can take up to a minute on a cold start (model load), and cancelling
    that mid-spawn via a short timeout risks an orphaned subprocess that is
    registered with the process lifecycle but never adopted as the active
    server. Skipping cleanly here means this optional enhancement engages
    automatically once the engine is warm for any other reason, and never
    costs a chat turn a cold start.
    """
    persisted = get_current_chat_mode()

    from python.inference import llama_server_manager as _lsm

    if _lsm.active_variant() is None:
        return persisted

    # Two booleans, not a single three-way enum. Measured directly: a 3-way
    # enum locked onto one label regardless of message content (chance-level,
    # confirmed on both 0.5B and 1.5B models) -- the model wasn't
    # discriminating on the input at all. Decomposing into two independent
    # yes/no questions and deriving the mode ourselves scored 10/12 fields
    # correct on the same test set. Neither signal firing means "not
    # confident either way" -- defer to the persisted mode rather than guess.
    try:
        result = await asyncio.wait_for(
            _lsm.decide(
                schema={
                    "in_character": {
                        "type": "boolean",
                        "description": (
                            "true if the message uses roleplay action text like "
                            "*smiles* or *leans in*, or talks to Tala as a "
                            "companion/crewmate rather than a software tool; "
                            "false for plain direct speech"
                        ),
                    },
                    "wants_technical_help": {
                        "type": "boolean",
                        "description": (
                            "true if the message asks for code, a bug fix, a "
                            "debugging session, or any concrete computer/software "
                            "task; false otherwise"
                        ),
                    },
                },
                contexts=[raw_user_prompt],
            ),
            timeout=5.0,
        )
        decision = result["results"][0]["decision"]
        in_character = bool(decision["in_character"])
        wants_technical_help = bool(decision["wants_technical_help"])
        if in_character and wants_technical_help:
            return "hybrid"
        if in_character:
            return "rp"
        if wants_technical_help:
            return "assistant"
        return persisted
    except Exception as exc:
        log.debug(
            "chat_mode: per-turn determination failed, using persisted mode %r: %s",
            persisted, exc,
        )
        return persisted
