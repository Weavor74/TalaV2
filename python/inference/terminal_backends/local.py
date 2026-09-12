"""
local.py - Auto-generated documentation base.
"""

import logging
import subprocess
import os
import tempfile
from typing import Optional
from .base import BaseEnvironment, ProcessResult

logger = logging.getLogger(__name__)

class LocalEnvironment(BaseEnvironment):
    def __init__(self, cwd: Optional[str] = None, timeout: int = 120):
        # Shared shell discovery: on Windows this finds Git Bash by its
        # install locations (a bare `bash` on PATH may be the WSL stub), so
        # the env-snapshot/CWD-tracking bash path works there too instead of
        # silently swapping in PowerShell semantics.
        self._bash_exe = self._find_bash()
        self._bash_available = self._bash_exe is not None
        super().__init__(cwd, timeout)
        if not self._bash_available:
            logger.warning(
                "LocalEnvironment: no bash found; falling back to %s. "
                "Env-snapshot and CWD tracking are unavailable in this mode.",
                "PowerShell" if os.name == "nt" else "sh",
            )

    @staticmethod
    def _find_bash() -> Optional[str]:
        try:
            from sdk.agent.shell_platform import bash_executable
            return bash_executable()
        except Exception:
            try:
                result = subprocess.run(
                    ["bash", "-c", "printf ok"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip() == b"ok":
                    return "bash"
            except Exception:
                pass
            return None

    def _make_snapshot_path(self) -> str:
        # The host's real temp directory, not a hardcoded /tmp (absent on
        # Windows). Forward slashes so Git Bash can `source` the path.
        return os.path.join(
            tempfile.gettempdir(), f"tala-snap-{self._session_id}.sh"
        ).replace("\\", "/")

    def init_session(self):
        if not self._bash_available:
            return
        super().init_session()

    def execute(self, command: str, cwd: Optional[str] = None) -> ProcessResult:
        if self._bash_available:
            return super().execute(command, cwd)

        target_cwd = cwd or self.cwd
        if os.name == "nt":
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
        else:
            cmd = ["sh", "-c", command]

        try:
            result = subprocess.run(
                cmd,
                cwd=target_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.timeout
            )
            return ProcessResult(output=result.stdout, returncode=result.returncode)
        except subprocess.TimeoutExpired as e:
            stdout_str = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return ProcessResult(
                output=stdout_str + f"\n[Command timed out after {self.timeout}s]",
                returncode=124
            )
        except Exception as e:
            return ProcessResult(output=f"Error: {e}", returncode=1)

    def _run_bash(self, cmd_string: str, login: bool = False, timeout: int = 120) -> ProcessResult:
        if not self._bash_available:
            return ProcessResult(output="bash is not available for LocalEnvironment.", returncode=127)

        cmd = [self._bash_exe, "-l", "-c", cmd_string] if login else [self._bash_exe, "-c", cmd_string]
        
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
        except Exception as e:
            return ProcessResult(output=f"Error: {e}", returncode=1)

    def cleanup(self):
        # The snapshot is a real file on the host; remove it directly.
        try:
            os.remove(self._snapshot_path)
        except OSError:
            pass
