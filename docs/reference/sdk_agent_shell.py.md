# File: `shell.py`

## Module Description

sdk/agent/shell.py
==================
Runtime Shell Scaffolding & Sandboxing — Tala Cognitive Engine Room
Milestone 5 | Tasks 5.1, 5.2, 5.5, 5.6

Implements:
  • SecureShellExecutor  — Safe execution of terminal lines with stream outputs.
  • IPCRouteCreator      — Mappings to format custom JSON-RPC WebSocket interfaces.
  • SimulationSandbox    — Staging files inside virtual spaces to test code changes safely.
  • ReadOnlyScaffolding  — Intercepts and mocks write actions while piping read queries.

## Classes

### `class SecureShellExecutor`
Spawns shell subprocesses with environment scrubbing and stream piping.

- **`def run_command(command, cwd, env_override)`**
  - Run command synchronously with clean env profile.

### `class IPCRouteCreator`
Prepares standard routes mapping Electron preload context-bridges to Python handlers.

- **`def format_json_rpc_request(method, params, request_id)`**
  - Format parameters into JSON-RPC 2.0 payload spec.

### `class SimulationSandbox`
Manages non-authoritative simulation staging in temp workspaces.

- **`def __init__(self, workspace_root)`**
- **`def create_sandbox(self)`**
  - Clone code files and structures to a temp folder to test edit/compile iterations.
- **`def cleanup_sandbox(self)`**
  - Delete temp workspace contents.

### `class ReadOnlyScaffolding`
Wraps files or API managers. Mocks modifications/writes while forwarding reads.

- **`def __init__(self, root_dir)`**
- **`def read_file(self, file_path)`**
  - Allows direct read-through to the live filesystem.
- **`def write_file(self, file_path, data)`**
  - Mocks write operations and caches payload instead of modifying source.

## Source Code

```python
"""
sdk/agent/shell.py
==================
Runtime Shell Scaffolding & Sandboxing — Tala Cognitive Engine Room
Milestone 5 | Tasks 5.1, 5.2, 5.5, 5.6

Implements:
  • SecureShellExecutor  — Safe execution of terminal lines with stream outputs.
  • IPCRouteCreator      — Mappings to format custom JSON-RPC WebSocket interfaces.
  • SimulationSandbox    — Staging files inside virtual spaces to test code changes safely.
  • ReadOnlyScaffolding  — Intercepts and mocks write actions while piping read queries.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

log = logging.getLogger(__name__)


class SecureShellExecutor:
    """
    Spawns shell subprocesses with environment scrubbing and stream piping.
    """

    @staticmethod
    def run_command(command: str, cwd: str, env_override: dict[str, str] | None = None) -> dict[str, Any]:
        """
        Run command synchronously with clean env profile.
        """
        clean_env = os.environ.copy()
        
        # Scrub sensitive variables (strip passwords, auth tokens, system credentials)
        for key in list(clean_env.keys()):
            if any(token in key.lower() for token in ("token", "secret", "auth", "password", "key")):
                clean_env.pop(key)

        if env_override:
            clean_env.update(env_override)

        try:
            res = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                env=clean_env,
                capture_output=True,
                text=True,
            )
            return {
                "success": res.returncode == 0,
                "exit_code": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        except Exception as exc:
            log.error("SecureShellExecutor: command execution failed: %s", exc)
            return {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
            }


class IPCRouteCreator:
    """
    Prepares standard routes mapping Electron preload context-bridges to Python handlers.
    """

    @staticmethod
    def format_json_rpc_request(method: str, params: dict[str, Any], request_id: int) -> str:
        """Format parameters into JSON-RPC 2.0 payload spec."""
        import json
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": request_id,
        }
        return json.dumps(payload)


class SimulationSandbox:
    """
    Manages non-authoritative simulation staging in temp workspaces.
    """

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = os.path.abspath(workspace_root)
        self.active_sandbox: str | None = None

    def create_sandbox(self) -> str:
        """
        Clone code files and structures to a temp folder to test edit/compile iterations.
        """
        self.active_sandbox = tempfile.mkdtemp(prefix="tala_sim_")
        
        # Copy critical items (skip massive node_modules and .venv)
        for item in os.listdir(self.workspace_root):
            if item in (".git", "node_modules", ".venv", ".pytest_cache", "__pycache__"):
                continue
            src_path = os.path.join(self.workspace_root, item)
            dest_path = os.path.join(self.active_sandbox, item)
            
            if os.path.isdir(src_path):
                shutil.copytree(src_path, dest_path, symlinks=True, ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))
            else:
                shutil.copy2(src_path, dest_path)
                
        log.info("SimulationSandbox: cloned staging workspace to: %s", self.active_sandbox)
        return self.active_sandbox

    def cleanup_sandbox(self) -> None:
        """Delete temp workspace contents."""
        if self.active_sandbox and os.path.exists(self.active_sandbox):
            shutil.rmtree(self.active_sandbox, ignore_errors=True)
            log.info("SimulationSandbox: cleaned up staging workspace.")
            self.active_sandbox = None


class ReadOnlyScaffolding:
    """
    Wraps files or API managers. Mocks modifications/writes while forwarding reads.
    """

    def __init__(self, root_dir: str) -> None:
        self.root_dir = os.path.abspath(root_dir)
        # Mock tracker for hijacked writes
        self.mocked_writes: list[dict[str, Any]] = []

    def read_file(self, file_path: str) -> str:
        """Allows direct read-through to the live filesystem."""
        abs_path = os.path.join(self.root_dir, file_path)
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def write_file(self, file_path: str, data: str) -> bool:
        """Mocks write operations and caches payload instead of modifying source."""
        self.mocked_writes.append({
            "action": "write",
            "file": file_path,
            "data": data,
        })
        log.info("ReadOnlyScaffolding: Intercepted and mocked write operation to %s", file_path)
        return True

```
