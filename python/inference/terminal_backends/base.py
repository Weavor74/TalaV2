"""
base.py - Auto-generated documentation base.
"""

import abc
import shlex
import uuid
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class ProcessResult:
    def __init__(self, output: str, returncode: int):
        self.output = output
        self.returncode = returncode

class BaseEnvironment(abc.ABC):
    """
    Stateless, spawn-per-command execution environment.
    Creates an initial shell snapshot, and wraps subsequent commands
    to restore state and capture CWD changes.
    """
    def __init__(self, cwd: str = "/root", timeout: int = 120):
        self.cwd = cwd
        self.timeout = timeout
        self._session_id = uuid.uuid4().hex[:12]
        self._snapshot_path = f"/tmp/tala-snap-{self._session_id}.sh"
        self._cwd_marker = f"__TALA_CWD_{self._session_id}__"
        self._snapshot_ready = False

    @abc.abstractmethod
    def _run_bash(self, cmd_string: str, login: bool = False, timeout: int = 120) -> ProcessResult:
        """Spawn bash process, block until complete, and return ProcessResult. Must be overridden."""
        pass

    @abc.abstractmethod
    def cleanup(self):
        """Release any backend resources."""
        pass

    def init_session(self):
        """Capture the initial environment snapshot (aliases, functions, exports)."""
        quoted_snap = shlex.quote(self._snapshot_path)
        quoted_cwd = shlex.quote(self.cwd)
        
        # NOTE: these separators must be real newlines. They were previously
        # written as "\\n", which in a non-raw Python string is a literal
        # backslash-n, collapsing the whole script onto one line.
        script = (
            f"export -p > {quoted_snap}\n"
            f"declare -f | grep -vE '^_[^_]' >> {quoted_snap}\n"
            f"alias -p >> {quoted_snap}\n"
            f"echo 'shopt -s expand_aliases' >> {quoted_snap}\n"
            f"builtin cd {quoted_cwd} 2>/dev/null || true\n"
        )
        try:
            result = self._run_bash(script, login=True, timeout=30)
            if result.returncode == 0:
                self._snapshot_ready = True
                logger.info(f"Session snapshot ready for session {self._session_id}")
        except Exception as e:
            logger.warning(f"Failed to initialize session snapshot: {e}")

    def execute(self, command: str, cwd: Optional[str] = None) -> ProcessResult:
        """Wrap command with state restoration, run it, and capture new state/CWD."""
        target_cwd = cwd or self.cwd
        
        quoted_snap = shlex.quote(self._snapshot_path)
        quoted_cwd = shlex.quote(target_cwd)
        # The POSIX single-quote escape is '\'' -- close, escaped quote, reopen.
        # This previously emitted an extra backslash ('\\''), which breaks any
        # command containing a single quote.
        escaped_cmd = command.replace("'", "'\\''")
        
        parts = []
        if self._snapshot_ready:
            parts.append(f"source {quoted_snap} >/dev/null 2>&1 || true")
            
        parts.append(f"builtin cd {quoted_cwd} || exit 126")
        parts.append(f"eval '{escaped_cmd}'")
        parts.append("__tala_ec=$?")
        
        if self._snapshot_ready:
            parts.append(f"export -p > {quoted_snap} 2>/dev/null || true")
            
        # Emit CWD marker
        # The \\n here is intentional and must stay escaped: it is consumed by
        # printf as a format escape, not by Python. Do not "fix" it to a real
        # newline -- that would embed a raw newline in the shell literal.
        parts.append(f"printf '\\n{self._cwd_marker}%s{self._cwd_marker}\\n' \"$(pwd -P)\"")
        parts.append("exit $__tala_ec")
        
        # Real newline. As "\\n" this joined every statement onto a single line,
        # so `builtin cd ... || exit 126` short-circuited over the rest and the
        # command never ran -- exit 0, empty output, success reported.
        full_script = "\n".join(parts)
        
        result = self._run_bash(full_script, login=False, timeout=self.timeout)
        self._update_cwd(result)
        return result

    def _update_cwd(self, result: ProcessResult):
        """Extract the __TALA_CWD__ marker from stdout to update self.cwd."""
        output = result.output
        last = output.rfind(self._cwd_marker)
        if last == -1:
            return
            
        first = output.rfind(self._cwd_marker, 0, last)
        if first == -1 or first == last:
            return
            
        new_cwd = output[first + len(self._cwd_marker) : last].strip()
        if new_cwd:
            self.cwd = new_cwd
            
        # Strip marker from output
        # Real newline: the captured output contains actual newlines (printf
        # expands the \n in its format string), not literal backslash-n.
        line_start = output.rfind("\n", 0, first)
        if line_start == -1:
            line_start = first
            
        line_end = output.find("\n", last + len(self._cwd_marker))
        if line_end == -1:
            line_end = len(output)
        else:
            line_end += 1
            
        result.output = output[:line_start] + output[line_end:]
