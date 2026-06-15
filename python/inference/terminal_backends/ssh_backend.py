import subprocess
import logging
from typing import Optional
from .base import BaseEnvironment, ProcessResult

logger = logging.getLogger(__name__)

class SSHEnvironment(BaseEnvironment):
    def __init__(self, host: str, user: str, cwd: str = "~", timeout: int = 120):
        super().__init__(cwd, timeout)
        self.host = host
        self.user = user
        # Note: True Hermes uses an explicit ControlMaster socket.
        # For parity, we rely on the host's ~/.ssh/config multiplexing.
        self.init_session()

    def _run_bash(self, cmd_string: str, login: bool = False, timeout: int = 120) -> ProcessResult:
        ssh_cmd = [
            "ssh", "-o", "StrictHostKeyChecking=accept-new", 
            f"{self.user}@{self.host}"
        ]
        
        # We need to properly escape the command string for passing it to ssh.
        escaped_cmd = cmd_string.replace("'", "'\\\\''")
        if login:
            ssh_cmd.extend(["bash", "-l", "-c", f"'{escaped_cmd}'"])
        else:
            ssh_cmd.extend(["bash", "-c", f"'{escaped_cmd}'"])
            
        try:
            result = subprocess.run(
                ssh_cmd,
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
            return ProcessResult(output=f"Error executing ssh: {e}", returncode=1)

    def cleanup(self):
        pass
