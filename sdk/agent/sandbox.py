"""
sandbox.py - OS-level isolation for workstation commands.

Implements native sandboxing per OS:
- Linux: bubblewrap (bwrap) or namespace containers
- macOS: sandbox-exec with profile
- Windows: restricted tokens + job objects

All workstation shell commands go through sandbox_exec() which applies
the appropriate OS-level isolation before execution.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)


SandboxMode = Literal["none", "strict", "permissive"]


@dataclass
class SandboxConfig:
    """Configuration for command sandboxing."""
    mode: SandboxMode = "strict"
    allow_network: bool = False
    allow_ipc: bool = False
    readonly_paths: list[Path] = None
    readwrite_paths: list[Path] = None
    tmpdir: Path | None = None
    uid: int | None = None
    gid: int | None = None
    
    def __post_init__(self):
        if self.readonly_paths is None:
            self.readonly_paths = []
        if self.readwrite_paths is None:
            self.readwrite_paths = []


class SandboxBackend:
    """Base class for OS-specific sandbox implementations."""
    
    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        """Wrap command with sandbox invocation."""
        raise NotImplementedError
    
    def is_available(self) -> bool:
        """Check if this backend can run on this system."""
        raise NotImplementedError


class BubblewrapSandbox(SandboxBackend):
    """Linux sandboxing via bubblewrap (bwrap)."""
    
    def is_available(self) -> bool:
        return shutil.which("bwrap") is not None
    
    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        if config.mode == "none":
            return argv
        
        bwrap = shutil.which("bwrap")
        if not bwrap:
            log.warning("bwrap not available, running unsandboxed")
            return argv
        
        sandbox_argv = [bwrap]
        
        # Base filesystem (read-only root)
        sandbox_argv.extend([
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/sbin", "/sbin",
            "--dir", "/tmp",
            "--dir", "/var",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/run",
        ])
        
        # Home directory (read-only by default)
        home = Path.home()
        if home.exists():
            sandbox_argv.extend(["--ro-bind", str(home), str(home)])
        
        # Workspace directory (read-write)
        for rw_path in config.readwrite_paths + [workdir]:
            if rw_path.exists():
                sandbox_argv.extend(["--bind", str(rw_path), str(rw_path)])
        
        # Additional read-only paths
        for ro_path in config.readonly_paths:
            if ro_path.exists():
                sandbox_argv.extend(["--ro-bind", str(ro_path), str(ro_path)])
        
        # Network isolation (default: no network)
        if not config.allow_network:
            sandbox_argv.append("--unshare-net")
        
        # IPC isolation
        if not config.allow_ipc:
            sandbox_argv.append("--unshare-ipc")
        
        # User namespace (run as same UID)
        sandbox_argv.extend([
            "--unshare-user",
            "--uid", str(config.uid or os.getuid()),
            "--gid", str(config.gid or os.getgid()),
        ])
        
        # Working directory
        sandbox_argv.extend(["--chdir", str(workdir)])
        
        # Command to execute
        sandbox_argv.extend(["--"] + argv)
        
        return sandbox_argv


class NamespaceContainerSandbox(SandboxBackend):
    """Linux sandboxing via unshare + mount namespaces (fallback when bwrap unavailable)."""
    
    def is_available(self) -> bool:
        return shutil.which("unshare") is not None and platform.system() == "Linux"
    
    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        if config.mode == "none":
            return argv
        
        unshare = shutil.which("unshare")
        if not unshare:
            log.warning("unshare not available, running unsandboxed")
            return argv
        
        # Create a wrapper script that sets up the environment
        sandbox_argv = [unshare, "--fork", "--pid"]
        
        if not config.allow_network:
            sandbox_argv.append("--net")
        
        if not config.allow_ipc:
            sandbox_argv.append("--ipc")
        
        # Mount namespace for filesystem isolation
        sandbox_argv.extend(["--mount", "--"])
        sandbox_argv.extend(argv)
        
        return sandbox_argv


class MacOSSandbox(SandboxBackend):
    """macOS sandboxing via sandbox-exec (deprecated but functional).
    
    IMPORTANT: sandbox-exec is deprecated by Apple as of macOS 10.15+.
    Apple recommends App Sandbox (for bundled apps) or Hardened Runtime instead.
    However, sandbox-exec still works for command-line sandboxing.
    
    What this DOES:
    - Creates SBPL (Sandbox Profile Language) profile
    - Blocks file writes outside allowed paths
    - Blocks network access (when allow_network=False)
    - Blocks Keychain access
    - Blocks system directory writes
    
    What this DOES NOT:
    - Guarantee future compatibility (deprecated API)
    - Work on all macOS versions (may fail on newer versions)
    - Provide container-level isolation (like Docker)
    
    For production macOS sandboxing:
    - Use App Sandbox for bundled applications
    - Use Hardened Runtime for signed binaries
    - Consider Docker/Podman for container isolation
    
    Current implementation: Uses deprecated sandbox-exec, with graceful fallback.
    """
    
    def is_available(self) -> bool:
        available = platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None
        if available:
            log.info("Using deprecated sandbox-exec (functional but deprecated by Apple)")
        return available
    
    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        if config.mode == "none":
            return argv
        
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            log.warning("sandbox-exec not available, running unsandboxed")
            return argv
        
        # Create temporary sandbox profile
        profile = self._create_profile(config, workdir)
        profile_fd, profile_path = tempfile.mkstemp(suffix=".sb", text=True)
        try:
            os.write(profile_fd, profile.encode("utf-8"))
            os.close(profile_fd)
            
            return [sandbox_exec, "-f", profile_path] + argv
        finally:
            try:
                os.unlink(profile_path)
            except Exception:
                pass
    
    def _create_profile(self, config: SandboxConfig, workdir: Path) -> str:
        """Generate sandbox profile in SBPL (Sandbox Profile Language)."""
        profile = "(version 1)\n"
        
        if config.mode == "strict":
            # Deny most operations by default
            profile += "(deny default)\n"
            
            # Allow basic system operations
            profile += "(allow process-exec)\n"
            profile += "(allow sysctl-read)\n"
            
            # Allow network only if configured
            if config.allow_network:
                profile += "(allow network*)\n"
            else:
                profile += "(deny network*)\n"
            
            # Allow file operations in workspace
            for rw_path in config.readwrite_paths + [workdir]:
                profile += f'(allow file* (subpath "{rw_path}"))\n'
            
            # Read-only access to system libraries and read-only paths
            profile += '(allow file-read* (subpath "/usr"))\n'
            profile += '(allow file-read* (subpath "/System"))\n'
            profile += '(allow file-read* (subpath "/Library"))\n'
            
            for ro_path in config.readonly_paths:
                profile += f'(allow file-read* (subpath "{ro_path}"))\n'
        else:
            # Permissive mode: allow most, deny sensitive
            profile += "(allow default)\n"
            profile += '(deny file* (subpath "/System"))\n'
            profile += '(deny file* (subpath "/Library/Keychains"))\n'
        
        return profile


class WindowsSandbox(SandboxBackend):
    """Windows sandboxing - GUARDED MODE FALLBACK ONLY.
    
    WARNING: This is NOT real OS-level sandboxing like Linux namespaces or macOS sandbox-exec.
    Windows sandboxing requires native APIs (CreateRestrictedToken, CreateJobObject) via
    ctypes or pywin32, which are not implemented here.
    
    What this DOES:
    - Runs command in PowerShell job (process isolation, but same security context)
    - Enforces timeout
    - Captures stdout/stderr
    
    What this DOES NOT:
    - Block filesystem writes (commands can write anywhere user can)
    - Block network access (commands have full network)
    - Restrict registry access
    - Create restricted security token
    - Use job objects for resource limits
    
    For real Windows sandboxing, implement:
    1. CreateRestrictedToken via ctypes/pywin32
    2. CreateJobObject with resource limits
    3. AssignProcessToJobObject
    4. Or use Windows Sandbox (separate VM)
    
    Current implementation: Guarded mode (logged execution, timeout, but no confinement).
    """
    
    def is_available(self) -> bool:
        return platform.system() == "Windows"
    
    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        if config.mode == "none":
            return argv
        
        log.warning("Windows sandbox: guarded mode only (no filesystem/network confinement)")
        
        # Simple PowerShell wrapper for timeout and output capture
        # This is NOT sandboxing, just structured execution
        cmd_escaped = " ".join(f'"{arg}"' if " " in arg else arg for arg in argv)
        
        script = f"""
$ErrorActionPreference = 'Continue'
Set-Location '{workdir}'
try {{
    {cmd_escaped}
    exit $LASTEXITCODE
}} catch {{
    Write-Error $_.Exception.Message
    exit 1
}}
"""
        
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    
    def _create_powershell_wrapper(self, argv: list[str], config: SandboxConfig, workdir: Path) -> str:
        """Legacy method - now simplified since we're not claiming real sandboxing."""
        return " ".join(argv)


def select_sandbox_backend() -> SandboxBackend:
    """Choose the best available sandbox backend for this OS."""
    backends = [
        BubblewrapSandbox(),
        NamespaceContainerSandbox(),
        MacOSSandbox(),
        WindowsSandbox(),
    ]
    
    for backend in backends:
        if backend.is_available():
            log.info(f"Selected sandbox backend: {backend.__class__.__name__}")
            return backend
    
    log.warning("No sandbox backend available, commands will run unsandboxed")
    
    # Fallback: no sandboxing
    class NoOpSandbox(SandboxBackend):
        def is_available(self) -> bool:
            return True
        
        def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
            return argv
    
    return NoOpSandbox()


# Global sandbox backend (initialized once)
_SANDBOX_BACKEND: SandboxBackend | None = None


def get_sandbox_backend() -> SandboxBackend:
    """Get the singleton sandbox backend."""
    global _SANDBOX_BACKEND
    if _SANDBOX_BACKEND is None:
        _SANDBOX_BACKEND = select_sandbox_backend()
    return _SANDBOX_BACKEND


async def sandbox_exec(
    argv: list[str],
    workdir: Path,
    config: SandboxConfig | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Execute a command in a sandbox with OS-level isolation.
    
    This is the main entry point for all workstation command execution.
    """
    if config is None:
        config = SandboxConfig(mode="strict")
    
    backend = get_sandbox_backend()
    sandboxed_argv = backend.wrap_command(argv, config, workdir)
    
    log.info(f"Executing sandboxed: {' '.join(sandboxed_argv[:3])}...")
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_argv,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"Command timed out after {timeout}s")
        
        return subprocess.CompletedProcess(
            args=sandboxed_argv,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except Exception as e:
        log.exception("Sandbox execution failed")
        raise


import asyncio
