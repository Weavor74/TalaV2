import subprocess
import os
from typing import Optional
from .base import BaseEnvironment, ProcessResult

class LocalEnvironment(BaseEnvironment):
    def __init__(self, cwd: str = ".", timeout: int = 120):
        super().__init__(cwd, timeout)
        self._bash_available = self._check_bash_available()

    def _check_bash_available(self) -> bool:
        try:
            result = subprocess.run(
                ["bash", "-c", "printf ok"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5
            )
            return result.returncode == 0 and result.stdout.strip() == b"ok"
        except Exception:
            return False

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

        cmd = ["bash", "-l", "-c", cmd_string] if login else ["bash", "-c", cmd_string]
        
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
        pass
