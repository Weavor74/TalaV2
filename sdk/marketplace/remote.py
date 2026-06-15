"""
sdk/marketplace/remote.py — Tala Cognitive Engine Room: Remote Pointer Infrastructure
======================================================================================
Provides the infrastructure for safely fetching and registering remote HTML
Neural Nodes from the open marketplace.

Security invariants  (NON-NEGOTIABLE)
--------------------------------------
1. **Authority stamping**: Every node fetched via :class:`RemoteNodeFetcher`
   has ``execution_authority`` forcibly set to ``0.00`` and
   ``context_only = True``, *regardless* of what the remote document declares.
   This prevents a malicious remote node from escalating its own authority
   simply by writing ``data-execution-authority="1.00"`` in its HTML.

2. **Hash verification**: :class:`RemotePointer` carries a SHA-256 hash of
   ``URL + fetch_timestamp``.  The fetcher logs the hash and can be extended
   to verify content hashes in a future milestone.

3. **No eval/exec**: Remote HTML is parsed via lxml only.  ``eval()``,
   ``exec()``, and ``ast.literal_eval()`` are never used on remote content.

4. **Fetch logging**: Every fetch is recorded in the PostgreSQL ``agent_log``
   table (fire-and-forget; a DB outage never blocks a fetch).

Milestone 4 — Identity & Marketplace
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RemotePointer — typed reference to a remote Neural Node
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RemotePointer:
    """
    A trusted reference to a remote HTML Neural Node resource.

    Attributes
    ----------
    pointer_id : str
        Unique stable identifier for this pointer.  Auto-generated as a UUID4
        string if not provided.
    url : str
        Full URL of the remote Neural Node HTML document.
    cryptographic_hash : str
        SHA-256 hex digest of ``url + ":" + str(fetch_timestamp_unix)``.
        Computed at pointer creation time.  Used for audit trails.
    provider_name : str
        Human-readable name of the marketplace provider that hosts this node.
    is_trusted : bool
        Whether this pointer has been manually designated as trusted by a
        :class:`TrustedShareGateway`.  Untrusted pointers can still be
        fetched but carry additional warnings in the log.
    fetch_timestamp_unix : float
        Unix epoch float at the moment the pointer was created.  Used as
        input to the cryptographic hash.
    """

    url: str
    provider_name: str
    pointer_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    is_trusted: bool = False
    fetch_timestamp_unix: float = field(default_factory=time.time)
    cryptographic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        """Compute the SHA-256 hash of ``url + ":" + fetch_timestamp_unix``."""
        raw = f"{self.url}:{self.fetch_timestamp_unix}"
        self.cryptographic_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"RemotePointer(id={self.pointer_id!r}, url={self.url!r}, "
            f"trusted={self.is_trusted}, hash={self.cryptographic_hash[:16]}…)"
        )


# ---------------------------------------------------------------------------
# RemoteNodeFetcher
# ---------------------------------------------------------------------------


class RemoteNodeFetcher:
    """
    Fetches remote HTML Neural Nodes and enforces the authority-stamping invariant.

    The fetcher:
    1. Downloads the raw HTML from the pointer URL (using ``httpx`` or
       ``urllib.request`` as a fallback).
    2. Parses the document with the SDK's standard :func:`sdk.agent.parser.parse_html`.
    3. **Forcibly stamps** the returned :class:`~sdk.agent.parser.NeuralNode`
       with ``execution_authority = 0.00`` and ``context_only = True``.
    4. Logs the fetch event to the PostgreSQL ``agent_log`` table.

    Parameters
    ----------
    http_timeout : float
        Seconds before an HTTP request times out.  Defaults to 10.0.
    """

    def __init__(self, http_timeout: float = 10.0) -> None:
        self._timeout = http_timeout

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    async def fetch(self, pointer: RemotePointer) -> "StampedRemoteNode":
        """
        Fetch a remote Neural Node and return a :class:`StampedRemoteNode`.

        The returned object's ``execution_authority`` is ALWAYS ``0.00`` and
        ``context_only`` is ALWAYS ``True`` regardless of what the remote
        document declares.

        Parameters
        ----------
        pointer : RemotePointer
            The pointer describing the remote resource to fetch.

        Returns
        -------
        StampedRemoteNode
            The fetched and authority-stamped node wrapper.

        Raises
        ------
        RemoteFetchError
            If the HTTP request fails or times out.
        sdk.agent.exceptions.InvalidNodeSchemaError
            If the fetched HTML fails Neural Node schema validation.
        """
        logger.info(
            "RemoteNodeFetcher.fetch: url=%r trusted=%s hash=%s…",
            pointer.url, pointer.is_trusted, pointer.cryptographic_hash[:16],
        )

        if not pointer.is_trusted:
            logger.warning(
                "Fetching UNTRUSTED remote pointer: %r (provider=%r)",
                pointer.url, pointer.provider_name,
            )

        # Step 1: Download HTML
        raw_html: str = await self._download(pointer.url)

        # Step 2: Parse with the standard SDK parser
        try:
            from sdk.agent.parser import parse_html  # type: ignore[import]
            node = parse_html(raw_html)
        except Exception as exc:
            raise RemoteFetchError(
                f"Failed to parse remote node from {pointer.url!r}: {exc}"
            ) from exc

        # Step 3: MANDATORY authority stamping — this is the security invariant
        stamped = StampedRemoteNode(
            # Copy all fields from the parsed node
            node_id=node.node_id,
            node_type=node.node_type,
            # SECURITY: Overwrite authority to 0.00 unconditionally
            execution_authority=0.00,
            context_only=True,
            weight_relevance=node.weight_relevance,
            semantic_memory=node.semantic_memory,
            relationships=node.relationships,
            state_data=node.state_data,
            # Attach provenance metadata
            pointer=pointer,
            fetched_at=datetime.now(timezone.utc),
            original_declared_authority=node.execution_authority,
        )

        logger.info(
            "Remote node stamped: node_id=%r original_authority=%.2f → stamped=0.00",
            stamped.node_id, stamped.original_declared_authority,
        )

        # Step 4: Log fetch to DB (fire-and-forget)
        asyncio.ensure_future(self._log_fetch(pointer, stamped))

        return stamped

    # ----------------------------------------------------------------
    # Private helpers
    # ----------------------------------------------------------------

    async def _download(self, url: str) -> str:
        """
        Download raw HTML from *url*.

        Tries ``httpx`` first (async, preferred), falls back to
        ``urllib.request`` in a thread executor.
        """
        try:
            import httpx  # type: ignore[import]
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                return response.text
        except ImportError:
            # httpx not installed — fall back to synchronous urllib in thread
            logger.debug("httpx not available; falling back to urllib.request")
            return await asyncio.get_event_loop().run_in_executor(
                None, self._download_sync, url
            )
        except Exception as exc:
            raise RemoteFetchError(
                f"HTTP fetch failed for {url!r}: {exc}"
            ) from exc

    def _download_sync(self, url: str) -> str:
        """Synchronous fallback downloader using urllib."""
        import urllib.request
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "TalaCognitiveEngineRoom/2.0 RemoteNodeFetcher"},
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise RemoteFetchError(
                f"urllib fetch failed for {url!r}: {exc}"
            ) from exc

    async def _log_fetch(
        self, pointer: RemotePointer, stamped: "StampedRemoteNode"
    ) -> None:
        """Fire-and-forget: write fetch event to agent_log table."""
        try:
            from python.harness.db import log_to_db  # type: ignore[import]
            await log_to_db(
                level="INFO",
                message=(
                    f"RemoteNodeFetcher: fetched node_id={stamped.node_id!r} "
                    f"from url={pointer.url!r} "
                    f"original_authority={stamped.original_declared_authority:.2f} "
                    f"→ stamped_authority=0.00 context_only=True"
                ),
                trace={
                    "pointer_id": pointer.pointer_id,
                    "url": pointer.url,
                    "provider_name": pointer.provider_name,
                    "is_trusted": pointer.is_trusted,
                    "hash": pointer.cryptographic_hash,
                    "node_id": stamped.node_id,
                    "original_authority": stamped.original_declared_authority,
                    "fetched_at": stamped.fetched_at.isoformat(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            # DB logging must never block or raise — just warn
            logger.warning("Failed to log remote fetch to agent_log: %s", exc)


# ---------------------------------------------------------------------------
# StampedRemoteNode — authority-stamped wrapper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StampedRemoteNode:
    """
    A remotely-fetched Neural Node with mandatory authority stamping applied.

    This class is intentionally NOT a :class:`~sdk.agent.parser.NeuralNode`
    subclass — it is a distinct type to make it impossible to accidentally
    pass a remote node to code that expects a locally-authoritative node.

    The ``execution_authority`` field is ALWAYS ``0.00``.
    The ``context_only`` flag is ALWAYS ``True``.
    These are structural invariants, not runtime checks.
    """

    node_id: str
    node_type: str
    execution_authority: float               # Always 0.00
    context_only: bool                       # Always True
    weight_relevance: float
    semantic_memory: str
    relationships: list[Any]
    state_data: dict[str, Any]
    pointer: RemotePointer
    fetched_at: datetime
    original_declared_authority: float       # What the remote file claimed

    def __post_init__(self) -> None:
        """Enforce structural invariants — these cannot be overridden."""
        # Hard enforcement: even if the caller tried to pass authority != 0.00,
        # we reset it here.  This is the last line of defence.
        object.__setattr__(self, "execution_authority", 0.00)
        object.__setattr__(self, "context_only", True)


# ---------------------------------------------------------------------------
# PointerRegistry — PostgreSQL-backed registry
# ---------------------------------------------------------------------------


class PointerRegistry:
    """
    Maintains a local list of known :class:`RemotePointer` objects in PostgreSQL.

    The registry uses the ``remote_pointers`` table (created on first use via
    ``ensure_table()``).  All operations are async and use the shared db pool.

    Usage::

        registry = PointerRegistry()
        await registry.ensure_table()
        await registry.register(pointer)
        pointers = await registry.list_all()
        ptr = await registry.get(pointer_id)
    """

    # ----------------------------------------------------------------
    # DDL
    # ----------------------------------------------------------------

    _CREATE_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS remote_pointers (
            pointer_id          TEXT        PRIMARY KEY,
            url                 TEXT        NOT NULL,
            provider_name       TEXT        NOT NULL DEFAULT '',
            is_trusted          BOOLEAN     NOT NULL DEFAULT FALSE,
            cryptographic_hash  TEXT        NOT NULL,
            fetch_timestamp     DOUBLE PRECISION NOT NULL,
            registered_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    async def ensure_table(self) -> None:
        """Create the ``remote_pointers`` table if it does not exist."""
        try:
            from python.harness.db import get_pool  # type: ignore[import]
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(self._CREATE_TABLE_SQL)
            logger.debug("PointerRegistry: remote_pointers table ensured.")
        except Exception as exc:
            logger.error("PointerRegistry.ensure_table failed: %s", exc)
            raise

    async def register(self, pointer: RemotePointer) -> None:
        """
        Upsert a :class:`RemotePointer` into the registry.

        If a pointer with the same ``pointer_id`` already exists, it is
        updated (URL, trust status, hash, and timestamp).
        """
        try:
            from python.harness.db import get_pool  # type: ignore[import]
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO remote_pointers
                        (pointer_id, url, provider_name, is_trusted,
                         cryptographic_hash, fetch_timestamp)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (pointer_id) DO UPDATE SET
                        url                = EXCLUDED.url,
                        provider_name      = EXCLUDED.provider_name,
                        is_trusted         = EXCLUDED.is_trusted,
                        cryptographic_hash = EXCLUDED.cryptographic_hash,
                        fetch_timestamp    = EXCLUDED.fetch_timestamp
                    """,
                    pointer.pointer_id,
                    pointer.url,
                    pointer.provider_name,
                    pointer.is_trusted,
                    pointer.cryptographic_hash,
                    pointer.fetch_timestamp_unix,
                )
            logger.info("PointerRegistry: registered pointer_id=%r url=%r", pointer.pointer_id, pointer.url)
        except Exception as exc:
            logger.error("PointerRegistry.register failed: %s", exc)
            raise

    async def get(self, pointer_id: str) -> RemotePointer | None:
        """
        Look up a pointer by its ``pointer_id``.

        Returns ``None`` if not found.
        """
        try:
            from python.harness.db import fetchrow  # type: ignore[import]
            row = await fetchrow(
                "SELECT * FROM remote_pointers WHERE pointer_id = $1",
                pointer_id,
            )
            if row is None:
                return None
            return self._row_to_pointer(row)
        except Exception as exc:
            logger.error("PointerRegistry.get failed: %s", exc)
            raise

    async def list_all(self, trusted_only: bool = False) -> list[RemotePointer]:
        """
        Return all registered pointers.

        Parameters
        ----------
        trusted_only : bool
            If True, only return pointers where ``is_trusted = TRUE``.
        """
        try:
            from python.harness.db import fetch  # type: ignore[import]
            if trusted_only:
                rows = await fetch(
                    "SELECT * FROM remote_pointers WHERE is_trusted = TRUE ORDER BY registered_at DESC"
                )
            else:
                rows = await fetch(
                    "SELECT * FROM remote_pointers ORDER BY registered_at DESC"
                )
            return [self._row_to_pointer(r) for r in rows]
        except Exception as exc:
            logger.error("PointerRegistry.list_all failed: %s", exc)
            raise

    async def set_trusted(self, pointer_id: str, is_trusted: bool) -> None:
        """Update the ``is_trusted`` flag for an existing pointer."""
        try:
            from python.harness.db import execute  # type: ignore[import]
            await execute(
                "UPDATE remote_pointers SET is_trusted = $1 WHERE pointer_id = $2",
                is_trusted, pointer_id,
                caller="PointerRegistry.set_trusted",
            )
            logger.info(
                "PointerRegistry: set pointer_id=%r is_trusted=%s", pointer_id, is_trusted
            )
        except Exception as exc:
            logger.error("PointerRegistry.set_trusted failed: %s", exc)
            raise

    # ----------------------------------------------------------------
    # Private helpers
    # ----------------------------------------------------------------

    @staticmethod
    def _row_to_pointer(row: Any) -> RemotePointer:
        """Reconstruct a :class:`RemotePointer` from a DB row."""
        p = RemotePointer(
            pointer_id=row["pointer_id"],
            url=row["url"],
            provider_name=row["provider_name"],
            is_trusted=row["is_trusted"],
            fetch_timestamp_unix=float(row["fetch_timestamp"]),
        )
        # Restore the persisted hash (override the one computed in __post_init__)
        object.__setattr__(p, "cryptographic_hash", row["cryptographic_hash"])
        return p


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class RemoteFetchError(RuntimeError):
    """Raised when a remote HTML Neural Node cannot be fetched or parsed."""


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "RemotePointer",
    "RemoteNodeFetcher",
    "StampedRemoteNode",
    "PointerRegistry",
    "RemoteFetchError",
]
