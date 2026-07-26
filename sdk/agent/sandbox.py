"""
sdk/agent/sandbox.py
====================
Docker Sandbox integration layer using container isolation boundaries.
Executes test runs, service runs, and installation commands in fully isolated micro-environments.
"""

from __future__ import annotations

import os
import logging
import asyncio
import shutil
from typing import Any
from pathlib import Path

from python.harness.config import settings
from sdk.agent.async_shell import AsyncShellExecutor

log = logging.getLogger(__name__)

class DockerSandbox:
    def __init__(self) -> None:
        self._docker_available: bool | None = None

    async def _run_docker(self, args: list[str]) -> dict[str, Any]:
        """Wrapper around AsyncShellExecutor.run_exec to invoke docker with cwd specified."""
        return await AsyncShellExecutor.run_exec("docker", args, cwd=os.getcwd())

    async def check_docker_available(self) -> bool:
        """Determines if Docker is installed and running on the host machine."""
        if self._docker_available is not None:
            return self._docker_available

        # Check if docker command is on PATH
        if not shutil.which("docker"):
            log.warning("DockerSandbox: docker executable not found on PATH. Defaulting to host execution fallback.")
            self._docker_available = False
            return False

        try:
            res = await asyncio.wait_for(self._run_docker(["info"]), timeout=3.0)
            self._docker_available = res["success"]
            if not self._docker_available:
                log.warning("DockerSandbox: docker daemon is not running. Defaulting to host execution fallback.")
        except asyncio.TimeoutError:
            log.warning("DockerSandbox: docker daemon check timed out after 3.0s. Defaulting to host execution fallback.")
            self._docker_available = False
        except Exception as exc:
            log.warning("DockerSandbox: Failed to verify docker availability: %s", exc)
            self._docker_available = False

        return self._docker_available

    def get_container_name(self, task_id: str) -> str:
        # Sanitize container name for docker standard [a-zA-Z0-9][a-zA-Z0-9_.-]
        sanitized = "".join(c if c.isalnum() or c in "_.-" else "-" for c in task_id)
        return f"tala-sandbox-{sanitized}"

    async def start_container(
        self,
        task_id: str,
        worktree_path: str,
        port_mappings: dict[int, int] | None = None
    ) -> bool:
        """
        Spins up an isolated container for the task, mounting the worktree directory.
        """
        if not await self.check_docker_available():
            return False

        c_name = self.get_container_name(task_id)

        # 1. Check if already running
        chk = await self._run_docker(["inspect", "-f", "{{.State.Running}}", c_name])
        if chk["success"] and chk["stdout"].strip().lower() == "true":
            log.info("DockerSandbox: Sandbox container %s is already running.", c_name)
            return True

        # Ensure container is clean
        await self._run_docker(["rm", "-f", c_name])

        # 2. Prepare run args
        # Mount the absolute worktree path to /workspace in the container
        abs_worktree = os.path.abspath(worktree_path)
        run_args = [
            "run", "-d",
            "--name", c_name,
            "-v", f"{abs_worktree}:/workspace",
            "-w", "/workspace"
        ]

        # Add port mappings
        if port_mappings:
            for host_port, container_port in port_mappings.items():
                run_args.extend(["-p", f"127.0.0.1:{host_port}:{container_port}"])

        # Run with fallback tail -f /dev/null to keep container alive
        run_args.extend(["python:3.12-slim", "tail", "-f", "/dev/null"])

        log.info("DockerSandbox: Starting container %s with volumes mounted...", c_name)
        res = await self._run_docker(run_args)
        
        if res["success"]:
            # Perform dependency setups if python image lacks pytest/etc.
            # We can install requirements asynchronously
            asyncio.create_task(self._install_essential_packages(c_name))
            return True
        else:
            log.error("DockerSandbox: Failed to start container %s: %s", c_name, res.get("stderr"))
            return False

    async def _install_essential_packages(self, container_name: str) -> None:
        """Install essential test tools inside the sandbox container for verification."""
        await self._run_docker(
            ["exec", container_name, "pip", "install", "--quiet", "pytest", "pytest-asyncio", "anyio"]
        )

    async def execute_command(
        self,
        task_id: str,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None
    ) -> dict[str, Any]:
        """
        Executes a command inside the task's Docker container.
        Falls back to local host execution if Docker is unavailable.
        """
        c_name = self.get_container_name(task_id)
        if await self.check_docker_available():
            # Check if container is running
            chk = await self._run_docker(["inspect", "-f", "{{.State.Running}}", c_name])
            is_running = chk["success"] and chk["stdout"].strip().lower() == "true"

            if is_running:
                log.info("DockerSandbox: Executing command inside docker container %s: %s", c_name, command)
                # Build exec command
                exec_args = ["exec", "-w", "/workspace"]
                if env:
                    for k, v in env.items():
                        exec_args.extend(["-e", f"{k}={v}"])
                exec_args.extend([c_name, "sh", "-c", command])

                res = await self._run_docker(exec_args)
                return res

        # Graceful fallback: Run locally on the host
        log.warning("DockerSandbox: Executing command directly on host fallback: %s", command)
        # Find absolute path of worktree if cwd is not specified
        if not cwd:
            from python.harness.server import _ROOT_DIR
            base_dir = Path(settings.LOCAL_WORKSPACE_DIR) if getattr(settings, 'LOCAL_WORKSPACE_DIR', None) else Path(_ROOT_DIR)
            worktree_path = base_dir / "scratch" / "worktrees" / task_id
            cwd = str(worktree_path) if worktree_path.exists() else str(base_dir)
        
        return await AsyncShellExecutor.run(command, cwd=str(cwd), env_override=env)

    async def pause_container(self, task_id: str) -> bool:
        """Pauses execution within the docker sandbox."""
        if not await self.check_docker_available():
            return False
        c_name = self.get_container_name(task_id)
        res = await self._run_docker(["pause", c_name])
        return res["success"]

    async def resume_container(self, task_id: str) -> bool:
        """Unpauses execution within the docker sandbox."""
        if not await self.check_docker_available():
            return False
        c_name = self.get_container_name(task_id)
        res = await self._run_docker(["unpause", c_name])
        return res["success"]

    async def stop_container(self, task_id: str) -> bool:
        """Stops and destroys the docker sandbox container."""
        if not await self.check_docker_available():
            return False
        c_name = self.get_container_name(task_id)
        log.info("DockerSandbox: Tearing down container %s...", c_name)
        res = await self._run_docker(["rm", "-f", c_name])
        return res["success"]
