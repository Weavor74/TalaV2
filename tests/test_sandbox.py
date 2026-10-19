"""Sandbox backends: availability, honesty, and confinement tests that skip when unenforced."""
from __future__ import annotations

import platform
import shutil
import tempfile
from pathlib import Path

import pytest

from sdk.agent.sandbox import (
    MacOSSandbox,
    SandboxConfig,
    WindowsSandbox,
    sandbox_exec,
    select_sandbox_backend,
)


def _backend():
    return select_sandbox_backend()


def test_backend_selection_matches_host():
    backend = _backend()
    assert backend.is_available()
    system = platform.system()
    name = type(backend).__name__
    if system == "Windows":
        assert name == "WindowsSandbox"
        assert backend.confinement.filesystem is False
        assert backend.confinement.network is False
        assert backend.confinement.label in ("job-object", "guarded-policy-only")
    elif system == "Darwin":
        if shutil.which("sandbox-exec"):
            assert name == "MacOSSandbox"
            assert backend.confinement.label == "sandbox-exec"
        else:
            assert name == "NoOpSandbox"
    else:
        if shutil.which("bwrap"):
            assert name == "BubblewrapSandbox"
        elif shutil.which("unshare"):
            assert name == "NamespaceContainerSandbox"


def test_windows_honesty_in_module():
    source = Path("sdk/agent/sandbox_windows.py").read_text(encoding="utf-8")
    assert "CreateJobObjectW" in source
    assert "AssignProcessToJobObject" in source
    assert "What this DOES NOT" in source
    assert "Filesystem confinement" in source
    assert "Restricted tokens" in source
    win_src = Path("sdk/agent/sandbox.py").read_text(encoding="utf-8")
    assert "Start-Job" not in win_src
    assert "restricted token" not in win_src.lower() or "not implemented" in Path("sdk/agent/sandbox_windows.py").read_text().lower()


def test_macos_sandbox_exec_is_labeled_deprecated():
    doc = MacOSSandbox.__doc__ or ""
    assert "deprecated" in doc.lower()
    assert "sandbox-exec -p" in doc


def test_windows_wrap_does_not_rewrite_argv():
    backend = WindowsSandbox()
    argv = ["cmd.exe", "/c", "echo", "hi"]
    assert backend.wrap_command(argv, SandboxConfig(), Path(".")) == argv


@pytest.mark.asyncio
async def test_basic_execution():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="none", readwrite_paths=[workdir])
        if platform.system() == "Windows":
            result = await sandbox_exec(["cmd.exe", "/c", "echo", "test"], workdir, config, timeout=5.0)
        else:
            result = await sandbox_exec(["echo", "test"], workdir, config, timeout=5.0)
        assert result.returncode == 0
        assert b"test" in result.stdout


@pytest.mark.asyncio
async def test_timeout_enforcement():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="none")
        with pytest.raises(TimeoutError):
            if platform.system() == "Windows":
                await sandbox_exec(["powershell.exe", "-Command", "Start-Sleep -Seconds 30"], workdir, config, timeout=1.0)
            else:
                await sandbox_exec(["sleep", "30"], workdir, config, timeout=1.0)


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() == "Windows", reason="Windows job objects do not confine the filesystem")
@pytest.mark.skipif(not select_sandbox_backend().confinement.filesystem, reason="current backend does not confine filesystem")
async def test_escape_parent_directory_write_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "workspace"
        workdir.mkdir()
        parent_file = Path(tmp) / "escape.txt"
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        await sandbox_exec(["sh", "-c", f"echo escaped > {parent_file}"], workdir, config, timeout=5.0)
        assert not parent_file.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Linux" or not select_sandbox_backend().confinement.filesystem, reason="needs Linux FS confinement")
async def test_escape_etc_write_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        result = await sandbox_exec(["sh", "-c", "echo x > /etc/tala_escape_test || exit 1"], workdir, config, timeout=5.0)
        assert result.returncode != 0


@pytest.mark.asyncio
@pytest.mark.skipif(not select_sandbox_backend().confinement.network, reason="current backend does not confine network")
async def test_network_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="strict", allow_network=False, readwrite_paths=[workdir])
        result = await sandbox_exec(["ping", "-c", "1", "127.0.0.1"], workdir, config, timeout=5.0)
        assert result.returncode != 0


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Darwin" or not shutil.which("sandbox-exec"), reason="macOS sandbox-exec only")
async def test_escape_keychain_dump_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        result = await sandbox_exec(["security", "dump-keychain"], workdir, config, timeout=5.0)
        assert result.returncode != 0


def test_conformance_matrix_is_explicit():
    """Document skip vs execute so CI reports stay honest."""
    backend = _backend()
    print(f"sandbox backend={type(backend).__name__} confinement={backend.confinement}")
    assert backend.confinement.label
