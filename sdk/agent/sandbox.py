"""
sdk/agent/sandbox.py
====================
Local Execution Sandbox — Tala Cognitive Engine Room

Executes compilation checks, test runs, and shell commands directly on the
host using ``AsyncShellExecutor``.  Docker has been removed entirely; it added
significant infrastructure overhead with no meaningful benefit for Tala's
self-build workflow.

``LocalSandbox`` exposes the same public interface that the old
``DockerSandbox`` did so all existing call-sites continue to work without
modification.
"""

from __future__ import annotations

import logging
from typing import Any

from sdk.agent.async_shell import AsyncShellExecutor

log = logging.getLogger(__name__)


class LocalSandbox:
    """
    Lightweight host-local command executor.

    Replaces the former Docker-based sandbox.  All methods retain the same
    signatures as the old ``DockerSandbox`` so existing callers require no
    changes.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle stubs (no-ops — kept for API compatibility)
    # ------------------------------------------------------------------ #

    async def start_container(
        self,
        task_id: str,
        worktree_path: str,
        port_mappings: dict[int, int] | None = None,
    ) -> bool:
        """No-op: no container to start."""
        log.debug("LocalSandbox: start_container called for task %s (no-op)", task_id)
        return True

    async def stop_container(self, task_id: str) -> bool:
        """No-op: no container to stop."""
        log.debug("LocalSandbox: stop_container called for task %s (no-op)", task_id)
        return True

    async def pause_container(self, task_id: str) -> bool:
        """No-op: pause not applicable to local execution."""
        log.debug("LocalSandbox: pause_container called for task %s (no-op)", task_id)
        return True

    async def resume_container(self, task_id: str) -> bool:
        """No-op: resume not applicable to local execution."""
        log.debug("LocalSandbox: resume_container called for task %s (no-op)", task_id)
        return True

    # ------------------------------------------------------------------ #
    # Command execution
    # ------------------------------------------------------------------ #

    async def execute_command(
        self,
        task_id: str,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute *command* directly on the host inside *cwd*.

        Parameters
        ----------
        task_id:
            Identifier of the owning task (used for logging only).
        command:
            Shell command string to execute.
        env:
            Optional extra environment variables to inject.
        cwd:
            Working directory for the subprocess.  Falls back to the current
            working directory when ``None``.
        """
        import os
        effective_cwd = cwd or os.getcwd()
        log.info("LocalSandbox: executing command for task %s in %s: %s", task_id, effective_cwd, command)
        return await AsyncShellExecutor.run(command, cwd=effective_cwd, env_override=env)


# ---------------------------------------------------------------------------
# Public alias — kept so any ``from sdk.agent.sandbox import DockerSandbox``
# import continues to resolve without touching every call-site.
# ---------------------------------------------------------------------------
DockerSandbox = LocalSandbox
