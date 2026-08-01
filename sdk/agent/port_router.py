"""
sdk/agent/port_router.py
========================
Port-allocation and collision-prevention router for Fleet Engineering.
Prevents parallel local development servers or test runners from port fighting.
"""

from __future__ import annotations

import socket
import logging
from python.harness import db

log = logging.getLogger(__name__)

class PortRouter:
    _in_memory_allocations: dict[str, int] = {}

    def __init__(self, base_port: int = 9000) -> None:
        self.base_port = base_port

    def _is_db_available(self) -> bool:
        try:
            db.get_pool()
            return True
        except RuntimeError:
            return False

    def is_port_physically_occupied(self, port: int) -> bool:
        """Checks if a port is in use on the local system."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return False
            except socket.error:
                return True

    async def allocate_port(self, task_id: str, preferred_port: int | None = None) -> int:
        """
        Allocates a unique, collision-free local port for a given task.
        Uses Postgres table for persistent coordination, falling back to in-memory.
        """
        # 1. Check existing allocation
        if self._is_db_available():
            try:
                row = await db.fetchrow("SELECT port FROM port_allocations WHERE task_id = $1", task_id)
                if row:
                    return row["port"]
            except Exception as exc:
                log.warning("PortRouter: DB read failed, using fallback check: %s", exc)
                
        if task_id in self._in_memory_allocations:
            return self._in_memory_allocations[task_id]

        # 2. Try preferred port first
        if preferred_port and not self.is_port_physically_occupied(preferred_port):
            # Check DB collision
            conflict = False
            if self._is_db_available():
                try:
                    row = await db.fetchrow("SELECT 1 FROM port_allocations WHERE port = $1", preferred_port)
                    conflict = row is not None
                except Exception:
                    pass
            if preferred_port in self._in_memory_allocations.values():
                conflict = True
                
            if not conflict:
                await self._persist_allocation(task_id, preferred_port)
                return preferred_port

        # 3. Find next available port starting from base_port
        candidate = preferred_port or self.base_port
        while True:
            # Check physical bind
            if self.is_port_physically_occupied(candidate):
                candidate += 1
                continue

            # Check DB collision
            conflict = False
            if self._is_db_available():
                try:
                    row = await db.fetchrow("SELECT 1 FROM port_allocations WHERE port = $1", candidate)
                    conflict = row is not None
                except Exception:
                    pass
            if candidate in self._in_memory_allocations.values():
                conflict = True

            if conflict:
                candidate += 1
                continue

            # Successfully allocated
            await self._persist_allocation(task_id, candidate)
            return candidate

    async def _persist_allocation(self, task_id: str, port: int) -> None:
        self._in_memory_allocations[task_id] = port
        if self._is_db_available():
            try:
                # Ensure schema exists just in case
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS port_allocations (
                        task_id      TEXT        PRIMARY KEY,
                        port         INTEGER     NOT NULL UNIQUE,
                        allocated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                await db.execute(
                    "INSERT INTO port_allocations (task_id, port) VALUES ($1, $2) ON CONFLICT (task_id) DO UPDATE SET port = EXCLUDED.port",
                    task_id,
                    port
                )
                log.info("PortRouter: Persisted port %d to DB for task %s", port, task_id)
            except Exception as exc:
                log.warning("PortRouter: Failed to persist allocation in DB: %s", exc)

    async def release_port(self, task_id: str) -> None:
        """Releases the port mapping for the specified task."""
        if task_id in self._in_memory_allocations:
            del self._in_memory_allocations[task_id]

        if self._is_db_available():
            try:
                await db.execute("DELETE FROM port_allocations WHERE task_id = $1", task_id)
                log.info("PortRouter: Released port allocation for task %s", task_id)
            except Exception as exc:
                log.warning("PortRouter: Failed to release port allocation in DB: %s", exc)
