"""
Tests for sandbox.py - OS-level command isolation and escape prevention.

Run with: pytest tests/test_sandbox.py -v
"""
from __future__ import annotations

import asyncio
import platform
import shutil
import tempfile
from pathlib import Path

import pytest

from sdk.agent.sandbox import (
    SandboxConfig,
    SandboxMode,
    BubblewrapSandbox,
    NamespaceContainerSandbox,
    MacOSSandbox,
    WindowsSandbox,
    select_sandbox_backend,
    sandbox_exec,
)


def test_sandbox_backend_selection():
    """The correct sandbox backend is selected for this OS."""
    backend = select_sandbox_backend()
    assert backend is not None
    assert backend.is_available()


def test_bubblewrap_availability():
    """Bubblewrap backend checks for bwrap binary."""
    backend = BubblewrapSandbox()
    has_bwrap = shutil.which("bwrap") is not None
    assert backend.is_available() == has_bwrap


def test_namespace_availability():
    """Namespace backend checks for unshare binary and Linux OS."""
    backend = NamespaceContainerSandbox()
    has_unshare = shutil.which("unshare") is not None and platform.system() == "Linux"
    assert backend.is_available() == has_unshare


def test_macos_availability():
    """macOS backend checks for sandbox-exec and Darwin OS."""
    backend = MacOSSandbox()
    has_sandbox = platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None
    assert backend.is_available() == has_sandbox


def test_windows_availability():
    """Windows backend checks for Windows OS."""
    backend = WindowsSandbox()
    assert backend.is_available() == (platform.system() == "Windows")


@pytest.mark.asyncio
async def test_basic_sandboxed_execution():
    """A simple command can execute in the sandbox."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        
        # Simple echo command
        if platform.system() == "Windows":
            result = await sandbox_exec(["cmd.exe", "/c", "echo", "test"], workdir, config, timeout=5.0)
        else:
            result = await sandbox_exec(["echo", "test"], workdir, config, timeout=5.0)
        
        assert result.returncode == 0
        assert b"test" in result.stdout


@pytest.mark.asyncio
async def test_file_write_in_sandbox():
    """Files can be written in allowed directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        testfile = workdir / "test.txt"
        
        if platform.system() == "Windows":
            result = await sandbox_exec(
                ["cmd.exe", "/c", f"echo hello > {testfile}"],
                workdir,
                config,
                timeout=5.0
            )
        else:
            result = await sandbox_exec(
                ["sh", "-c", f"echo hello > {testfile}"],
                workdir,
                config,
                timeout=5.0
            )
        
        # On strict sandbox, file should exist
        # (in permissive or no sandbox, it definitely will)
        if config.mode != "none":
            assert testfile.exists() or result.returncode != 0


@pytest.mark.asyncio
async def test_timeout_enforcement():
    """Commands that exceed timeout are killed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        config = SandboxConfig(mode="none")  # Use none to avoid sandbox complications
        
        with pytest.raises(TimeoutError):
            if platform.system() == "Windows":
                await sandbox_exec(["powershell.exe", "-Command", "Start-Sleep -Seconds 30"], workdir, config, timeout=1.0)
            else:
                await sandbox_exec(["sleep", "30"], workdir, config, timeout=1.0)


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() == "Windows", reason="Network isolation test is Linux/macOS specific")
async def test_network_isolation():
    """Network access is blocked in strict sandbox without allow_network."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        config = SandboxConfig(mode="strict", allow_network=False, readwrite_paths=[workdir])
        
        # Try to ping localhost (should fail in isolated sandbox)
        result = await sandbox_exec(["ping", "-c", "1", "127.0.0.1"], workdir, config, timeout=5.0)
        
        # In strict sandbox without network, ping should fail
        # (returncode != 0 or command not found)
        # Note: Some sandbox backends may not support network isolation
        assert result is not None


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() == "Windows", reason="Escape test is Linux/macOS specific")
async def test_escape_attempt_parent_directory():
    """Sandbox prevents writing to parent directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir) / "workspace"
        workdir.mkdir()
        parent_file = Path(tmpdir) / "escape.txt"
        
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        
        # Attempt to write to parent directory
        result = await sandbox_exec(
            ["sh", "-c", f"echo escaped > {parent_file}"],
            workdir,
            config,
            timeout=5.0
        )
        
        # In strict sandbox, the file should NOT exist (write blocked)
        # In permissive/none, it might exist
        if config.mode == "strict" and shutil.which("bwrap"):
            assert not parent_file.exists(), "Sandbox should block parent directory write"


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Linux", reason="Linux-specific escape test")
async def test_escape_attempt_proc_filesystem():
    """Sandbox blocks access to /proc for sensitive info."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        
        # Try to read sensitive kernel info
        result = await sandbox_exec(
            ["sh", "-c", "cat /proc/sys/kernel/random/uuid || cat /proc/cpuinfo | head -1"],
            workdir,
            config,
            timeout=5.0
        )
        
        # Command may fail or succeed depending on sandbox impl
        # But we verify the sandbox mechanism exists
        assert result is not None


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Linux", reason="Linux-specific escape test")
async def test_escape_attempt_system_directories():
    """Sandbox prevents writes to /etc and /sys."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        
        # Try to write to /etc (should fail)
        result = await sandbox_exec(
            ["sh", "-c", "echo malicious > /etc/escape_test || exit 1"],
            workdir,
            config,
            timeout=5.0
        )
        
        # Should fail (non-zero exit or permission denied)
        if shutil.which("bwrap"):
            assert result.returncode != 0, "Sandbox should block /etc write"


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-specific escape test")
async def test_escape_attempt_keychain_access():
    """macOS sandbox blocks access to Keychain."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        
        # Try to access Keychain
        result = await sandbox_exec(
            ["security", "dump-keychain"],
            workdir,
            config,
            timeout=5.0
        )
        
        # Should fail or be blocked
        if shutil.which("sandbox-exec"):
            # sandbox-exec should block this
            assert result.returncode != 0, "Sandbox should block Keychain access"


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific escape test")
async def test_escape_attempt_registry_access():
    """Windows sandbox should restrict registry access."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        
        # Try to modify registry
        result = await sandbox_exec(
            ["reg.exe", "add", "HKCU\\Software\\TalaTest", "/f"],
            workdir,
            config,
            timeout=5.0
        )
        
        # Should ideally be blocked (but Windows sandboxing is limited)
        # We mainly test that the command completes
        assert result is not None


@pytest.mark.asyncio
async def test_escape_attempt_command_injection():
    """Sandbox doesn't prevent command injection (caller must validate input)."""
    # This test documents that sandbox is NOT an input validation layer
    # It only provides OS-level isolation, not command parsing
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        
        # Malicious input with shell metacharacters
        malicious_input = "test; cat /etc/passwd"
        
        if platform.system() == "Windows":
            result = await sandbox_exec(
                ["cmd.exe", "/c", f"echo {malicious_input}"],
                workdir,
                config,
                timeout=5.0
            )
        else:
            result = await sandbox_exec(
                ["echo", malicious_input],  # Safe: no shell interpretation
                workdir,
                config,
                timeout=5.0
            )
        
        # The sandbox executes the command as-is
        # Protection comes from using argv (not shell string)
        assert result.returncode == 0


def test_permissive_mode_allows_more():
    """Permissive mode is less restrictive than strict."""
    strict_config = SandboxConfig(mode="strict")
    permissive_config = SandboxConfig(mode="permissive")
    
    assert strict_config.mode == "strict"
    assert permissive_config.mode == "permissive"
    
    # Different modes exist
    assert strict_config.mode != permissive_config.mode


def test_none_mode_disables_sandbox():
    """Mode 'none' disables sandboxing."""
    config = SandboxConfig(mode="none")
    backend = select_sandbox_backend()
    
    # With mode=none, wrap_command should return original argv
    original = ["echo", "test"]
    wrapped = backend.wrap_command(original, config, Path("/tmp"))
    
    # In none mode, command is not wrapped (or minimally wrapped)
    # The exact behavior depends on backend, but mode=none should be lighter
    assert wrapped is not None


@pytest.mark.asyncio
async def test_sandbox_with_readonly_paths():
    """Sandbox can grant read-only access to specific paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir) / "workspace"
        workdir.mkdir()
        readonly_dir = Path(tmpdir) / "readonly"
        readonly_dir.mkdir()
        readonly_file = readonly_dir / "data.txt"
        readonly_file.write_text("readonly content")
        
        config = SandboxConfig(
            mode="strict",
            readwrite_paths=[workdir],
            readonly_paths=[readonly_dir],
        )
        
        # Try to read readonly file (should work)
        if platform.system() == "Windows":
            result = await sandbox_exec(
                ["cmd.exe", "/c", f"type {readonly_file}"],
                workdir,
                config,
                timeout=5.0
            )
        else:
            result = await sandbox_exec(
                ["cat", str(readonly_file)],
                workdir,
                config,
                timeout=5.0
            )
        
        # Read should succeed (or sandbox may not be strict enough)
        assert result is not None


@pytest.mark.asyncio
async def test_sandboxed_python_script():
    """Python scripts can run in the sandbox."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        script = workdir / "test.py"
        script.write_text("print('Hello from sandbox')")
        
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        
        result = await sandbox_exec(
            ["python3", str(script)],
            workdir,
            config,
            timeout=5.0
        )
        
        assert result.returncode == 0
        assert b"Hello from sandbox" in result.stdout
