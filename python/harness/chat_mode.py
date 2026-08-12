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


def get_chat_mode_file_path() -> Path:
    """Absolute path of chat_mode.json under the active user-data root."""
    return _resolve_user_data_dir() / _CHAT_MODE_FILENAME


def normalize_chat_mode(mode: object) -> str:
    """Coerce arbitrary input to a valid mode, falling back to the default."""
    candidate = str(mode or "").strip().lower()
    return candidate if candidate in VALID_CHAT_MODES else DEFAULT_CHAT_MODE


def get_current_chat_mode() -> str:
    """
    Current chat mode, always one of VALID_CHAT_MODES.

    Never raises: a missing, empty, or corrupt chat_mode.json yields the
    default rather than propagating an error into an inference request.
    """
    path = get_chat_mode_file_path()
    try:
        if not path.is_file():
            return DEFAULT_CHAT_MODE
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return normalize_chat_mode(data.get("mode"))
        return normalize_chat_mode(data)
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

    path = get_chat_mode_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": candidate}, indent=2), encoding="utf-8")
    log.info("chat_mode: set to %r (%s)", candidate, path)
    return candidate


def is_emotional_mode(mode: str | None = None) -> bool:
    """
    Whether the given mode (default: the current one) carries the
    astrology-derived emotional state.
    """
    resolved = normalize_chat_mode(mode) if mode is not None else get_current_chat_mode()
    return resolved in EMOTIONAL_CHAT_MODES
