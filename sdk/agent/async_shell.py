"""
sdk/agent/async_shell.py
========================
Async Shell Executor — Tala Cognitive Engine Room
Milestone 8 | Task 8.1

Implements:
  • AsyncShellExecutor — Non-blocking asyncio-based shell executor for use inside
    async task loops. Wraps the SecureShellExecutor pattern from shell.py but uses
    asyncio.create_subprocess_shell instead of subprocess.run, enabling concurrent
    shell operations without blocking the event loop.

    Key behaviours:
      - Inherits the same environment scrubbing logic as SecureShellExecutor (strips
        keys containing 'token', 'secret', 'auth', 'password', 'key').
      - Applies asyncio.wait_for to enforce a configurable timeout ceiling.
      - Returns a structured dict with success, exit_code, stdout, stderr, timed_out.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


class AsyncShellExecutor:
    """
    Non-blocking asyncio-based shell executor.

    Provides the same secure environment scrubbing as SecureShellExecutor while
    executing commands via asyncio.create_subprocess_shell so it can be awaited
    inside async task loops without blocking the event loop.
    """

    @staticmethod
    def _build_clean_env(env_override: dict[str, str] | None = None) -> dict[str, str]:
        """
        Build a sanitised copy of the current environment.

        Strips keys that contain any of the sensitive tokens:
        'token', 'secret', 'auth', 'password', 'key'.
        Applies env_override values on top of the scrubbed env.
        """
        clean_env = os.environ.copy()

        for key in list(clean_env.keys()):
            if any(token in key.lower() for token in ("token", "secret", "auth", "password", "key")):
                clean_env.pop(key)

        if env_override:
            clean_env.update(env_override)

        return clean_env

    @staticmethod
    async def run_exec(
        executable: str,
        args: list[str],
        cwd: str,
        timeout: float = 30.0,
        env_override: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a binary natively via create_subprocess_exec (no shell)."""
        clean_env = AsyncShellExecutor._build_clean_env(env_override)
        log.debug("AsyncShellExecutor: running exec=%r args=%r cwd=%r timeout=%s", executable, args, cwd, timeout)

        proc: asyncio.subprocess.Process | None = None
        try:
            async def _run() -> tuple[str, str, int]:
                nonlocal proc
                proc = await asyncio.create_subprocess_exec(
                    executable,
                    *args,
                    cwd=cwd,
                    env=clean_env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await proc.communicate()
                return (
                    stdout_bytes.decode("utf-8", errors="replace"),
                    stderr_bytes.decode("utf-8", errors="replace"),
                    proc.returncode if proc.returncode is not None else -1,
                )

            stdout, stderr, exit_code = await asyncio.wait_for(_run(), timeout=timeout)
            success = exit_code == 0
            return {
                "success": success,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": False,
            }
        except asyncio.TimeoutError:
            log.warning("AsyncShellExecutor: exec timed out after %.1fs — exec=%r args=%r", timeout, executable, args)
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Exec timed out after {timeout}s",
                "timed_out": True,
            }
        except Exception as exc:
            log.error("AsyncShellExecutor: unexpected error executing exec=%r: %s", executable, exc)
            return {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "timed_out": False,
            }

    @staticmethod
    async def run(
        command: str,
        cwd: str,
        timeout: float = 30.0,
        env_override: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Execute *command* asynchronously inside *cwd*.

        Parameters
        ----------
        command:
            Shell command string to execute.
        cwd:
            Working directory for the subprocess.
        timeout:
            Maximum seconds to wait before cancelling the process.
            Defaults to 30 seconds.
        env_override:
            Optional extra environment variables to inject after scrubbing.

        Returns
        -------
        dict with keys:
            success    (bool)  — True if exit_code == 0 and not timed out.
            exit_code  (int)   — Process return code, or -1 on error.
            stdout     (str)   — Captured standard output.
            stderr     (str)   — Captured standard error.
            timed_out  (bool)  — True if the process was killed due to timeout.
        """
        clean_env = AsyncShellExecutor._build_clean_env(env_override)

        log.debug("AsyncShellExecutor: running command=%r cwd=%r timeout=%s", command, cwd, timeout)

        proc: asyncio.subprocess.Process | None = None
        try:
            async def _run() -> tuple[str, str, int]:
                nonlocal proc
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=cwd,
                    env=clean_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await proc.communicate()
                return (
                    stdout_bytes.decode("utf-8", errors="replace"),
                    stderr_bytes.decode("utf-8", errors="replace"),
                    proc.returncode if proc.returncode is not None else -1,
                )

            stdout, stderr, exit_code = await asyncio.wait_for(_run(), timeout=timeout)

            success = exit_code == 0
            log.debug(
                "AsyncShellExecutor: finished command=%r exit_code=%d success=%s",
                command,
                exit_code,
                success,
            )
            return {
                "success": success,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": False,
            }

        except asyncio.TimeoutError:
            log.warning(
                "AsyncShellExecutor: command timed out after %.1fs — command=%r", timeout, command
            )
            # Attempt to kill the lingering subprocess
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "timed_out": True,
            }

        except Exception as exc:
            log.error("AsyncShellExecutor: unexpected error executing command=%r: %s", command, exc)
            return {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "timed_out": False,
            }
