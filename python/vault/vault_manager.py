"""
python/vault/vault_manager.py — Tala Cognitive Engine Room: Vault Isolation Manager
====================================================================================
The VaultManager provides a strictly isolated read/write interface for the
Tala vault directory.

Isolation guarantees
--------------------
1. **Path containment**: Every read and write validates that the resolved
   absolute path is a child of ``VAULT_PATH``.  ``..`` traversal, symlinks
   pointing outside the vault, and absolute overrides are all rejected.

2. **PII intercept**: Before any read or write, ``_check_for_pii_access_intent``
   scans the current call-stack trace for PII-related keywords.  If any are
   found, ``PiiAccessBlockedError`` is raised and the operation is aborted.

3. **Network isolation**: Vault operations assert that no new socket connections
   are opened during execution.  This is enforced by temporarily patching
   ``socket.socket`` with a sentinel that raises ``RuntimeError`` if called.

4. **Character context load**: ``load_character_context()`` reads the backstory
   Neural Node as a raw string.  It is explicitly stateless — the string is
   NEVER written to the database or any persistent store.

Milestone 5 — Protected Core Vault & Chrono-Astrology
"""

from __future__ import annotations

import inspect
import logging
import socket
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sdk.agent.exceptions import PiiAccessBlockedError  # type: ignore[import]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PII keyword blocklist
# ---------------------------------------------------------------------------

_PII_KEYWORDS: frozenset[str] = frozenset({
    "ssn",
    "password",
    "private_key",
    "dob",
    "credit_card",
    # Additional high-risk terms
    "social_security",
    "date_of_birth",
    "bank_account",
    "routing_number",
    "passport",
    "drivers_license",
})

# ---------------------------------------------------------------------------
# Character backstory path (relative to vault root)
# ---------------------------------------------------------------------------

_CHARACTER_BACKSTORY_RELPATH = Path("character") / "backstory.html"


# ---------------------------------------------------------------------------
# VaultManager
# ---------------------------------------------------------------------------


class VaultManager:
    """
    Isolated read/write interface for the Tala vault directory.

    Parameters
    ----------
    vault_path : Path | str | None
        Absolute path to the vault directory.  Defaults to the ``VAULT_PATH``
        from :mod:`python.harness.config` if not provided.
    """

    def __init__(self, vault_path: Path | str | None = None) -> None:
        if vault_path is not None:
            self._vault_path = Path(vault_path).expanduser().resolve()
        else:
            # Deferred import — config loads .env; keep this module importable
            # even if config is not yet bootstrapped.
            try:
                from python.harness.config import settings  # type: ignore[import]
                self._vault_path = Path(settings.VAULT_PATH).expanduser().resolve()
            except Exception:  # noqa: BLE001
                # Fallback: repo_root/vault
                self._vault_path = (
                    Path(__file__).resolve().parent.parent.parent / "vault"
                )
                logger.warning(
                    "VaultManager: could not load config; defaulting vault_path to %s",
                    self._vault_path,
                )

        if not self._vault_path.exists():
            self._vault_path.mkdir(parents=True, exist_ok=True)
            logger.info("VaultManager: created vault directory at %s", self._vault_path)

        logger.debug("VaultManager: initialized with vault_path=%s", self._vault_path)

    @property
    def vault_path(self) -> Path:
        """Absolute path to the vault root directory."""
        return self._vault_path

    # ----------------------------------------------------------------
    # Public API: read / write
    # ----------------------------------------------------------------

    def read(self, key: str) -> str:
        """
        Read the content of a vault file identified by *key*.

        Parameters
        ----------
        key : str
            Relative path within the vault (e.g. ``"character/backstory.html"``).
            Must not escape the vault root.

        Returns
        -------
        str
            UTF-8 decoded file content.

        Raises
        ------
        PiiAccessBlockedError
            If the execution trace contains PII-related keywords.
        PermissionError
            If *key* resolves to a path outside the vault root.
        FileNotFoundError
            If the vault file does not exist.
        """
        trace = self._get_call_trace()
        self._check_for_pii_access_intent(trace)

        target = self._resolve_safe_path(key)

        if not target.exists():
            raise FileNotFoundError(
                f"VaultManager.read: vault file not found: {target}"
            )

        with _network_isolated_context():
            content = target.read_text(encoding="utf-8")

        logger.debug("VaultManager.read: key=%r bytes=%d", key, len(content))
        return content

    def write(self, key: str, value: str) -> None:
        """
        Write *value* to a vault file identified by *key*.

        The parent directory is created if it does not exist.
        Existing files are overwritten atomically (write to temp → rename).

        Parameters
        ----------
        key : str
            Relative path within the vault.
        value : str
            UTF-8 string content to write.

        Raises
        ------
        PiiAccessBlockedError
            If the execution trace contains PII-related keywords.
        PermissionError
            If *key* resolves to a path outside the vault root.
        """
        trace = self._get_call_trace()
        self._check_for_pii_access_intent(trace)

        target = self._resolve_safe_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to a temp file then rename
        tmp = target.with_suffix(target.suffix + ".tmp")
        with _network_isolated_context():
            tmp.write_text(value, encoding="utf-8")
            tmp.rename(target)

        logger.info("VaultManager.write: key=%r bytes=%d", key, len(value))

    # ----------------------------------------------------------------
    # Character context load (stateless — never written to DB)
    # ----------------------------------------------------------------

    def load_character_context(self) -> str:
        """
        Load Tala's 12-year character backstory as a stateless string.

        The backstory is read from ``vault/character/backstory.html`` and
        returned as a plain string.

        IMPORTANT: This method is explicitly stateless.  The returned string
        MUST NOT be written to PostgreSQL, mem0, or any other persistent store.
        It is intended to be injected into a context window for a single
        inference pass and then discarded.

        Returns
        -------
        str
            Raw HTML content of the backstory Neural Node.

        Raises
        ------
        FileNotFoundError
            If the backstory file does not exist in the vault.
        PiiAccessBlockedError
            If the execution trace contains PII keywords (this method has
            the same PII intercept as all other vault operations).
        """
        logger.debug("VaultManager.load_character_context: loading backstory")
        content = self.read(str(_CHARACTER_BACKSTORY_RELPATH))
        logger.info(
            "VaultManager.load_character_context: loaded backstory (%d chars) — stateless",
            len(content),
        )
        return content

    # ----------------------------------------------------------------
    # PII intercept
    # ----------------------------------------------------------------

    def _check_for_pii_access_intent(self, trace: str) -> None:
        """
        Scan *trace* for PII-related keywords.

        Parameters
        ----------
        trace : str
            Stringified call-stack trace or any execution context string.

        Raises
        ------
        PiiAccessBlockedError
            If any PII keyword is found (case-insensitive).

        Notes
        -----
        This check is intentionally conservative: a keyword appearing anywhere
        in the trace — even in a variable name, comment, or local variable —
        will trigger the block.  This errs on the side of safety.
        """
        trace_lower = trace.lower()
        for keyword in _PII_KEYWORDS:
            if keyword in trace_lower:
                logger.warning(
                    "VaultManager: PII keyword %r detected in execution trace — blocking.",
                    keyword,
                )
                raise PiiAccessBlockedError(
                    f"VaultManager: Vault access blocked — PII-related keyword "
                    f"{keyword!r} detected in execution trace.  "
                    f"No data was read or written.",
                    data_field=keyword,
                    node_id=None,
                    classification=keyword,
                )

    # ----------------------------------------------------------------
    # Private helpers
    # ----------------------------------------------------------------

    def _resolve_safe_path(self, key: str) -> Path:
        """
        Resolve *key* to an absolute path and assert it is inside the vault.

        Raises
        ------
        PermissionError
            If the resolved path escapes the vault root.
        ValueError
            If *key* is empty.
        """
        if not key or not str(key).strip():
            raise ValueError("VaultManager: key must be a non-empty string.")

        # Resolve against vault root — this neutralises '../' traversal
        resolved = (self._vault_path / key).resolve()

        # Strict prefix check: resolved must start with vault_path
        try:
            resolved.relative_to(self._vault_path)
        except ValueError:
            raise PermissionError(
                f"VaultManager: Path traversal attempt blocked.  "
                f"Key {key!r} resolves to {resolved}, which is outside "
                f"the vault root {self._vault_path}."
            )

        return resolved

    @staticmethod
    def _get_call_trace() -> str:
        """Return a string representation of the current call stack."""
        # Format as a flat string — easy to grep for keywords
        frames = traceback.format_stack()
        return " ".join(frames)


# ---------------------------------------------------------------------------
# Network isolation context manager
# ---------------------------------------------------------------------------


@contextmanager
def _network_isolated_context() -> Generator[None, None, None]:
    """
    Context manager that blocks any new outbound socket connections.

    While active, ``socket.socket.__init__`` is replaced with a sentinel
    that raises ``RuntimeError`` if called.  This ensures that vault I/O
    operations cannot initiate network calls — even if a bug or dependency
    accidentally tries to phone home during a vault read/write.

    Note: existing open sockets are NOT closed.  Only NEW socket creation
    is blocked.
    """
    original_socket_init = socket.socket.__init__

    def _blocked_socket_init(self_sock: socket.socket, *args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "VaultManager: Network call attempted inside a vault-isolated context.  "
            "Vault operations must not make outbound connections."
        )

    socket.socket.__init__ = _blocked_socket_init  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.__init__ = original_socket_init  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "VaultManager",
]
