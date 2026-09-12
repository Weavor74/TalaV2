"""
docker_backend.py - Auto-generated documentation base.
"""

import subprocess
import uuid
import logging
from .base import BaseEnvironment, ProcessResult

logger = logging.getLogger(__name__)

class DockerEnvironment(BaseEnvironment):
    def __init__(self, image: str = "ubuntu:latest", cwd: str = "/root", timeout: int = 120):
        super().__init__(cwd, timeout)
        self.image = image
        self.container_id = None
        self._start_container()

    def _start_container(self):
        container_name = f"tala-{uuid.uuid4().hex[:8]}"
        cmd = [
            "docker", "run", "-d", "--init", "--name", container_name,
            "-w", self.cwd,
            self.image, "sleep", "infinity"
        ]
        logger.info(f"Starting Docker container {container_name}...")
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
            self.container_id = container_name
            self.init_session()
        except Exception as e:
            logger.error(f"Failed to start Docker container: {e}")

    def _run_bash(self, cmd_string: str, login: bool = False, timeout: int = 120) -> ProcessResult:
        if not self.container_id:
            return ProcessResult(output="Container not running.", returncode=1)
            
        cmd = ["docker", "exec", self.container_id]
        if login:
            cmd.extend(["bash", "-l", "-c", cmd_string])
        else:
            cmd.extend(["bash", "-c", cmd_string])
            
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout
            )
            return ProcessResult(output=result.stdout, returncode=result.returncode)
        except subprocess.TimeoutExpired as e:
            stdout_str = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return ProcessResult(
                output=stdout_str + f"\\n[Command timed out after {timeout}s]",
                returncode=124
            )

    def cleanup(self):
        if self.container_id:
            logger.info(f"Cleaning up Docker container {self.container_id}...")
            subprocess.run(["docker", "rm", "-f", self.container_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.container_id = None
