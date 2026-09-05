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


async def _kill_process_tree(proc: "asyncio.subprocess.Process | None") -> None:
    """
    Kill a timed-out subprocess *and everything it spawned*.

    proc.kill() alone is not enough for create_subprocess_shell: it kills the
    shell, and the shell's child -- the command the caller actually asked for
    -- keeps running, detached, forever. That is how a 30-second timeout on
    `python scripts/generate_stories.py` left a long-running generator alive
    with no parent; the DAG then retried the node three more times and each
    attempt added another one. Six of them ended up queued against the same
    local model, which is what starved the chat path into ReadTimeouts.
    """
    if proc is None or proc.returncode is not None:
        return
    pid = proc.pid
    try:
        if os.name == "nt":
            # taskkill /T walks the child tree; there are no process groups
            # to signal here.
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=10)
        else:
            import signal
            try:
                pgid = os.getpgid(pid)
                # Only signal the group if the child really got its own. A
                # child that shares our group would mean killpg takes the
                # harness down with it.
                if pgid != os.getpgid(0):
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception as exc:
        log.warning(
            "AsyncShellExecutor: could not kill process tree for pid %s: %s", pid, exc
        )
    finally:
        try:
            proc.kill()
        except Exception:
            pass


#: Every subprocess this module currently has in flight.
#:
#: Exists so the emergency halt can reach work that has *already started*.
#: The halt used to be a gate in front of dispatch and nothing behind it, so
#: aborting a runaway job refused the next command while the runaway one ran
#: to completion -- which is the opposite of what the button is for.
_LIVE_PROCESSES: "set[asyncio.subprocess.Process]" = set()


def _register(proc: "asyncio.subprocess.Process") -> None:
    _LIVE_PROCESSES.add(proc)


def _unregister(proc: "asyncio.subprocess.Process | None") -> None:
    if proc is not None:
        _LIVE_PROCESSES.discard(proc)


async def kill_running_processes(reason: str = "halt") -> int:
    """
    Kill every subprocess this module currently has running.

    Returns how many were still alive and got signalled. Uses the same
    process-tree kill as the timeout path, because killing the shell without
    its children leaves the actual workload orphaned -- the exact failure that
    left six generators queued against one local model.
    """
    victims = [p for p in list(_LIVE_PROCESSES) if p.returncode is None]
    for proc in victims:
        log.critical(
            "AsyncShellExecutor: killing pid %s -- %s", getattr(proc, "pid", "?"), reason
        )
        try:
            await _kill_process_tree(proc)
        except Exception as exc:
            log.error("AsyncShellExecutor: failed to kill pid %s: %s",
                      getattr(proc, "pid", "?"), exc)
        finally:
            _unregister(proc)
    return len(victims)


def _halt_refusal(what: str) -> dict[str, Any]:
    return {
        "success": False,
        "exit_code": -1,
        "stdout": "",
        "stderr": (
            f"Emergency halt is engaged; {what} was not started. "
            "Resume the task, or remove data/emergency_halt.json, to continue."
        ),
        "timed_out": False,
        "halted": True,
    }


def _is_halted() -> bool:
    """Late import: gates imports this module to kill processes on halt."""
    try:
        from sdk.agent.gates import RevocationHalt

        return RevocationHalt.is_halted()
    except Exception as exc:
        # Refusing to run because the halt is unreadable is the safe direction.
        log.error("AsyncShellExecutor: could not read halt state (%s); refusing.", exc)
        return True


class AsyncShellExecutor:
    """
    Non-blocking asyncio-based shell executor.

    Provides the same secure environment scrubbing as SecureShellExecutor while
    executing commands via asyncio.create_subprocess_shell so it can be awaited
    inside async task loops without blocking the event loop.
    """

    # Variables whose *exact names* should be stripped when spawning untrusted
    # subprocesses.  This list intentionally targets only credentials that are
    # truly secret at the OS/CI level and must NEVER leak to child processes.
    # Do NOT use substring matching here — that approach incorrectly strips
    # variables like GEMINI_KEY, OPENAI_KEY that Tala's own subprocesses need.
    _STRIP_EXACT: frozenset[str] = frozenset({
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "COHERE_API_KEY",
        "HUGGINGFACE_TOKEN",
        "HF_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "STRIPE_SECRET_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_SECRET",
        "GCP_SERVICE_ACCOUNT_JSON",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "SMTP_PASSWORD",
        "JWT_SECRET",
        "DJANGO_SECRET_KEY",
    })

    @staticmethod
    def _build_clean_env(env_override: dict[str, str] | None = None) -> dict[str, str]:
        """
        Build a sanitised copy of the current environment.

        Strips only the explicitly listed known-secret variables (see
        ``_STRIP_EXACT``).  Preserves all other variables — including API keys
        such as ``GEMINI_KEY`` or ``OPENAI_KEY`` that Tala's own inference
        subprocesses require.
        Applies ``env_override`` values on top of the scrubbed env.
        """
        clean_env = os.environ.copy()

        for key in list(clean_env.keys()):
            if key.upper() in AsyncShellExecutor._STRIP_EXACT:
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
        if _is_halted():
            log.critical("AsyncShellExecutor: refusing exec %r -- emergency halt is engaged.", executable)
            return _halt_refusal(f"exec {executable!r}")

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
                _register(proc)
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
            await _kill_process_tree(proc)
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
        finally:
            _unregister(proc)

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
        if _is_halted():
            log.critical("AsyncShellExecutor: refusing %r -- emergency halt is engaged.", command)
            return _halt_refusal(f"command {command!r}")

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
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # Own session on POSIX so a timeout can signal the whole
                    # group without touching the harness's own process group.
                    **({"start_new_session": True} if os.name != "nt" else {}),
                )
                _register(proc)
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
            # Kill the shell AND whatever it spawned, or the real workload
            # outlives the timeout as an orphan.
            await _kill_process_tree(proc)
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
        finally:
            # A registry that only ever grows would have the halt trying to
            # kill processes that exited hours ago.
            _unregister(proc)
