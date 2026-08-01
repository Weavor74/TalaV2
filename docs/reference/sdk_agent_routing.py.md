# File: `routing.py`

## Module Description

sdk/agent/routing.py
====================
Provider Routing & Local Fallback Scaffolding — Tala Cognitive Engine Room
Milestone 5 | Tasks 5.3, 5.4

Implements:
  • ProviderRouter       — Dynamically matches queries to active inference backends.
  • LocalFallbackCache   — Offline fallback cache to service queries during outages.

## Classes

### `class ProviderRouter`
Selects the highest priority online inference/search provider from PostgreSQL.

- **`def get_active_inference_provider()`**
  - Query database table 'inference_providers' to retrieve the highest
  priority online backend. Falls back to embedded local default if database is empty.

### `class LocalFallbackCache`
Key-Value store caching online request payloads for offline fallback playback.

- **`def __init__(self)`**
- **`def save_response(self, key, payload)`**
  - Cache response data.
- **`def retrieve_cached_response(self, key)`**
  - Fetch cached data or fallback to offline stub values.

## Source Code

```python
"""
sdk/agent/routing.py
====================
Provider Routing & Local Fallback Scaffolding — Tala Cognitive Engine Room
Milestone 5 | Tasks 5.3, 5.4

Implements:
  • ProviderRouter       — Dynamically matches queries to active inference backends.
  • LocalFallbackCache   — Offline fallback cache to service queries during outages.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from python.harness import db

log = logging.getLogger(__name__)


class ProviderRouter:
    """
    Selects the highest priority online inference/search provider from PostgreSQL.
    """

    @staticmethod
    async def get_active_inference_provider() -> dict[str, Any]:
        """
        Query database table 'inference_providers' to retrieve the highest
        priority online backend. Falls back to embedded local default if database is empty.
        """
        db_available = False
        try:
            db.get_pool()
            db_available = True
        except RuntimeError:
            pass

        if db_available:
            try:
                # First check for a forced provider
                query_forced = """
                    SELECT id, type, name, priority, status, config
                    FROM inference_providers
                    WHERE config->>'is_forced' = 'true'
                    LIMIT 1
                """
                row = await db.fetchrow(query_forced)
                if row:
                    return dict(row)

                # Otherwise fallback to priority ordering
                query = """
                    SELECT id, type, name, priority, status, config
                    FROM inference_providers
                    WHERE status = 'online' OR status = 'unknown'
                    ORDER BY priority ASC
                    LIMIT 1
                """
                row = await db.fetchrow(query)
                if row:
                    return dict(row)
            except Exception as exc:
                log.warning("ProviderRouter: database query failed, using default: %s", exc)

        # Canonical fallback to the embedded builtin engine
        return {
            "id": "embedded-builtin",
            "type": "embedded",
            "name": "Tala Embedded Engine",
            "priority": 100,
            "status": "online",
            "config": {"model": "Qwen/Qwen2.5-0.5B-Instruct"},
        }


class LocalFallbackCache:
    """
    Key-Value store caching online request payloads for offline fallback playback.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def save_response(self, key: str, payload: Any) -> None:
        """Cache response data."""
        self._cache[key] = payload
        log.debug("LocalFallbackCache: cached response for key: %s", key)

    def retrieve_cached_response(self, key: str) -> Any | None:
        """Fetch cached data or fallback to offline stub values."""
        if key in self._cache:
            log.info("LocalFallbackCache: cache hit for key: %s", key)
            return self._cache[key]
            
        # Return fallback offline stub response
        log.warning("LocalFallbackCache: cache miss. Returning offline fallback response stub for %s", key)
        return {
            "status": "offline-fallback-active",
            "timestamp": "2026-05-30T00:00:00Z",
            "text": "Fallback content generated from local offline storage cache.",
        }

```
