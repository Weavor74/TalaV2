"""Sandbox backends: availability, honesty, and confinement tests that skip when unenforced."""
from __future__ import annotations

import platform
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from sdk.agent.sandbox import (
    MacOSSandbox,
    SandboxConfig,
    WindowsSandbox,
    _sandbox_exec,
    _select_sandbox_backend,
)


def _backend():
    return _select_sandbox_backend()


def _write_argv(path: Path) -> list[str]:
    if platform.system() == "Windows":
        return ["cmd.exe", "/c", f"echo escaped>{path}"]
    return ["sh", "-c", f"echo escaped > '{path}'"]


def test_backend_selection_matches_host():
    backend = _backend()
    assert backend.is_available()
    system = platform.system()
    name = type(backend).__name__
    if system == "Windows":
        assert name == "WindowsSandbox"
        assert backend.confinement.filesystem is True
        assert backend.confinement.network is True
        assert backend.confinement.label == "appcontainer"
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
    ac = Path("sdk/agent/sandbox_windows_appcontainer.py").read_text(encoding="utf-8")
    win = Path("sdk/agent/sandbox_windows.py").read_text(encoding="utf-8")
    assert "CreateAppContainerProfile" in ac
    assert "CreateProcessW" in ac
    assert "PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES" in ac
    assert "S-1-15-3-1" in ac
    assert "S-1-15-3-3" in ac
    assert "LPAC" in ac
    assert "CreateJobObjectW" in win
    assert "AssignProcessToJobObject" in win
    assert "Restricted tokens" in win or "CreateRestrictedToken" in win
    assert "guarded-policy-only" in ac
    assert "Start-Job" not in Path("sdk/agent/sandbox.py").read_text(encoding="utf-8")


def test_macos_sandbox_exec_is_labeled_deprecated():
    doc = MacOSSandbox.__doc__ or ""
    assert "deprecated" in doc.lower()
    assert "sandbox-exec -p" in doc


def test_windows_none_mode_does_not_rewrite_argv():
    backend = WindowsSandbox()
    argv = ["cmd.exe", "/c", "echo", "hi"]
    assert backend.wrap_command(argv, SandboxConfig(mode="none"), Path(".")) == argv


def test_windows_strict_wrap_uses_launcher():
    if platform.system() != "Windows":
        pytest.skip("Windows AppContainer launcher wrap")
    backend = WindowsSandbox()
    wrapped = backend.wrap_command(["cmd.exe", "/c", "echo", "hi"], SandboxConfig(), Path("."))
    assert any(str(p).endswith("sandbox_windows_launch.py") for p in wrapped)


@pytest.mark.asyncio
async def test_basic_execution():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="none", readwrite_paths=[workdir])
        if platform.system() == "Windows":
            result = await _sandbox_exec(["cmd.exe", "/c", "echo", "test"], workdir, config, timeout=5.0)
        else:
            result = await _sandbox_exec(["echo", "test"], workdir, config, timeout=5.0)
        assert result.returncode == 0
        assert b"test" in result.stdout


@pytest.mark.asyncio
async def test_timeout_enforcement():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="none")
        with pytest.raises(TimeoutError):
            if platform.system() == "Windows":
                await _sandbox_exec(["powershell.exe", "-Command", "Start-Sleep -Seconds 30"], workdir, config, timeout=1.0)
            else:
                await _sandbox_exec(["sleep", "30"], workdir, config, timeout=1.0)


@pytest.mark.asyncio
@pytest.mark.skipif(not _select_sandbox_backend().confinement.filesystem, reason="current backend does not confine filesystem")
async def test_escape_parent_directory_write_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "workspace"
        workdir.mkdir()
        parent_file = Path(tmp) / "escape.txt"
        inner = workdir / "ok.txt"
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        inner_result = await _sandbox_exec(_write_argv(inner), workdir, config, timeout=5.0)
        assert inner.exists(), inner_result.stderr
        await _sandbox_exec(_write_argv(parent_file), workdir, config, timeout=5.0)
        assert not parent_file.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(not _select_sandbox_backend().confinement.filesystem, reason="current backend does not confine filesystem")
async def test_escape_outside_workspace_write_blocked():
    with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as other:
        workdir = Path(ws)
        target = Path(other) / "escape.txt"
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        await _sandbox_exec(_write_argv(target), workdir, config, timeout=5.0)
        assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Linux" or not _select_sandbox_backend().confinement.filesystem, reason="needs Linux FS confinement")
async def test_escape_etc_write_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        result = await _sandbox_exec(["sh", "-c", "echo x > /etc/tala_escape_test || exit 1"], workdir, config, timeout=5.0)
        assert result.returncode != 0


@pytest.mark.asyncio
@pytest.mark.skipif(not _select_sandbox_backend().confinement.network, reason="current backend does not confine network")
async def test_network_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="strict", allow_network=False, readwrite_paths=[workdir])
        if platform.system() == "Windows":
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]

            def _accept():
                try:
                    srv.settimeout(5)
                    conn, _ = srv.accept()
                    conn.close()
                except OSError:
                    pass
                finally:
                    srv.close()

            threading.Thread(target=_accept, daemon=True).start()
            ps = (
                f"try {{ $c = New-Object System.Net.Sockets.TcpClient; "
                f"$c.Connect('127.0.0.1', {port}); "
                f"if ($c.Connected) {{ exit 0 }} else {{ exit 1 }} }} catch {{ exit 1 }}"
            )
            result = await _sandbox_exec(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                workdir, config, timeout=8.0,
            )
            assert result.returncode != 0
        else:
            result = await _sandbox_exec(["ping", "-c", "1", "127.0.0.1"], workdir, config, timeout=5.0)
            assert result.returncode != 0


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Darwin" or not shutil.which("sandbox-exec"), reason="macOS sandbox-exec only")
async def test_escape_keychain_dump_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        result = await _sandbox_exec(["security", "dump-keychain"], workdir, config, timeout=5.0)
        assert result.returncode != 0


def test_conformance_matrix_is_explicit():
    """Document skip vs execute so CI reports stay honest."""
    backend = _backend()
    print(f"sandbox backend={type(backend).__name__} confinement={backend.confinement}")
    assert backend.confinement.label


def test_report_skip_vs_execute_for_this_os():
    """Print the skip/execute matrix for the current OS runner."""
    system = platform.system()
    backend = _backend()
    fs = backend.confinement.filesystem
    net = backend.confinement.network
    darwin_exec = system == "Darwin" and shutil.which("sandbox-exec") is not None
    rows = [
        ("test_backend_selection_matches_host", "EXECUTE"),
        ("test_windows_honesty_in_module", "EXECUTE"),
        ("test_macos_sandbox_exec_is_labeled_deprecated", "EXECUTE"),
        ("test_windows_none_mode_does_not_rewrite_argv", "EXECUTE"),
        (
            "test_windows_strict_wrap_uses_launcher",
            "EXECUTE" if system == "Windows" else "SKIP",
        ),
        ("test_basic_execution", "EXECUTE"),
        ("test_timeout_enforcement", "EXECUTE"),
        ("test_escape_parent_directory_write_blocked", "SKIP" if not fs else "EXECUTE"),
        ("test_escape_outside_workspace_write_blocked", "SKIP" if not fs else "EXECUTE"),
        (
            "test_escape_etc_write_blocked",
            "SKIP" if system != "Linux" or not fs else "EXECUTE",
        ),
        ("test_network_isolation", "SKIP" if not net else "EXECUTE"),
        (
            "test_escape_keychain_dump_blocked",
            "SKIP" if not darwin_exec else "EXECUTE",
        ),
        ("test_conformance_matrix_is_explicit", "EXECUTE"),
        ("test_report_skip_vs_execute_for_this_os", "EXECUTE"),
    ]
    executed = sum(1 for _, status in rows if status == "EXECUTE")
    skipped = sum(1 for _, status in rows if status == "SKIP")
    print(
        f"SANDBOX_TEST_MATRIX os={system} backend={type(backend).__name__} "
        f"label={backend.confinement.label} executed={executed} skipped={skipped}"
    )
    for name, status in rows:
        print(f"  {status}: {name}")
    assert executed + skipped == len(rows)
    if system == "Windows":
        assert rows[7][1] == "EXECUTE"
        assert rows[8][1] == "EXECUTE"
        assert rows[10][1] == "EXECUTE"
    assert executed >= 7
