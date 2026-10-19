"""Sandbox backends: availability, honesty, and confinement tests that skip when unenforced."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
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


_CMD_SYNTAX_NEEDLES = (
    b"syntax is incorrect",
    b"filename, directory name, or volume label syntax",
    b"the syntax of the command is incorrect",
)


def _combined(result) -> bytes:
    return (result.stdout or b"") + (result.stderr or b"")


def _cmd_redirect_target(path: Path) -> str:
    """Unquoted cmd redirect target. Inner quotes break after list2cmdline."""
    path = Path(path)
    parent = path.parent
    name = path.name
    if platform.system() == "Windows" and parent.exists():
        import ctypes
        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        get_short.restype = ctypes.c_uint
        buf = ctypes.create_unicode_buffer(520)
        n = get_short(str(parent.resolve()), buf, 520)
        if n:
            parent = Path(buf.value)
    target = str(parent / name)
    if platform.system() == "Windows":
        target = target.replace("/", "\\")
    if any(ch in target for ch in ' "&<>|^'):
        raise AssertionError(f"cmd redirect target still needs quoting: {target!r}")
    return target


def _windows_write_argv(path: Path) -> list[str]:
    # /s strips the outer quotes list2cmdline adds around the /c payload.
    # Do not quote the redirect target: cmd does not treat backslash as an
    # escape, so a quoted path becomes ERROR_INVALID_NAME after list2cmdline.
    return ["cmd.exe", "/s", "/c", f"(echo escaped)>{_cmd_redirect_target(path)}"]


def _write_argv(path: Path) -> list[str]:
    if platform.system() == "Windows":
        return _windows_write_argv(path)
    return ["sh", "-c", f"echo escaped > '{path}'"]


def _assert_not_syntax_error(combined: bytes) -> None:
    low = combined.lower()
    for needle in _CMD_SYNTAX_NEEDLES:
        assert needle not in low, combined


def _assert_write_succeeded(result, target: Path) -> None:
    combined = _combined(result)
    _assert_not_syntax_error(combined)
    assert b"spawn-failed" not in combined, combined
    assert target.exists(), (result.returncode, result.stdout, result.stderr, combined)
    assert result.returncode == 0, combined


def _assert_write_denied(result, target: Path) -> None:
    combined = _combined(result)
    _assert_not_syntax_error(combined)
    assert b"spawn-failed" not in combined, combined
    text = combined.decode("utf-8", "replace")
    print("TALA_SANDBOX_DENY:", text.strip() or f"returncode={result.returncode}")
    assert not target.exists(), (result.returncode, combined)
    if platform.system() == "Windows":
        low = combined.lower()
        assert b"access is denied" in low or b"access denied" in low, combined
    else:
        assert result.returncode != 0, combined


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
    assert "CreateAppContainerProfile.restype = ctypes.HRESULT" not in ac
    assert "DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long" in ac
    assert "GetAppContainerFolderPath" in ac
    body = ac.split("def ensure_profile", 1)[1].split("def _is_volume_root", 1)[0]
    create_fn = ac.split("def _create_appcontainer_profile", 1)[1].split("def ensure_profile", 1)[0]
    assert create_fn.find("CreateAppContainerProfile") < create_fn.find(
        "DeriveAppContainerSidFromAppContainerName"
    )
    assert "_create_appcontainer_profile()" in body
    assert "CreateProcessW" in ac
    assert "PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES" in ac
    assert "S-1-15-3-1" in ac
    assert "S-1-15-3-3" in ac
    assert "LPAC" in ac
    assert "CreateJobObjectW" in win
    assert "AssignProcessToJobObject" in win
    assert "Restricted tokens" in win or "CreateRestrictedToken" in win
    assert "job-object-only" in ac
    assert "IsValidSid" in ac
    assert "TALA_AC_DIAG" in ac
    assert "/grant:r" in ac
    assert "/remove" in ac
    assert "(RX)" in ac
    assert "ACCESS DENIED" in ac
    assert "lpApplicationName=NULL" in ac
    assert "GetSystemDirectoryW" in ac
    assert "CreateProcessW.argtypes" in ac
    assert "wintypes.LPCWSTR, wintypes.LPWSTR" in ac
    assert "WinSta0\\\\Default" in ac or "WinSta0\\Default" in ac
    assert "GetStdHandle" in ac
    assert "_is_windows_system_path" in ac
    assert "refusing ACL grant" in ac
    assert "hostedtoolcache" in ac
    assert '"\\\\windows\\\\" in low' not in ac
    assert "Start-Job" not in Path("sdk/agent/sandbox.py").read_text(encoding="utf-8")
    write_fn = Path("tests/test_sandbox.py").read_text(encoding="utf-8").split(
        "def _windows_write_argv", 1
    )[1].split("def _write_argv", 1)[0]
    returned = [ln for ln in write_fn.splitlines() if ln.strip().startswith("return ")]
    assert returned and 'echo escaped>"' not in returned[0]
    assert "(echo escaped)>" in write_fn
    src = Path("sdk/agent/sandbox.py").read_text(encoding="utf-8")
    assert '(allow file-read-data (literal "/"))' in src
    assert '(allow file-read* (subpath "/"))' not in src
    launch = Path("sdk/agent/sandbox_windows_launch.py").read_text(encoding="utf-8")
    assert "--diagnose" in launch
    assert "_diagnose_createprocess" in launch


def test_macos_profile_lists_resolved_and_raw_paths(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    backend = MacOSSandbox()
    profile = backend._profile(SandboxConfig(readwrite_paths=[workdir]), workdir)
    for sub in backend._subpaths(workdir):
        assert f'(allow file-write* (subpath "{sub}"))' in profile
        assert f'(allow file-read* (subpath "{sub}"))' in profile
    src = Path("sdk/agent/sandbox.py").read_text(encoding="utf-8")
    assert ".tala-sandbox.sb" in src
    assert '"-f"' in src or "', '-f'," in src
    assert "(debug deny)" in profile
    assert "(allow file-map-executable)" in profile
    assert '(allow file-read-data (literal "/"))' in profile
    assert '(allow file-read* (subpath "/"))' not in profile
    write_rules = [ln.strip() for ln in profile.splitlines() if "file-write*" in ln]
    assert write_rules
    for rule in write_rules:
        inner = rule.split('(subpath "', 1)[1].rsplit('")', 1)[0].replace("\\\\", "\\")
        assert inner.rstrip("/\\").endswith("workspace"), rule
        assert inner.replace("\\", "/") not in ("/var", "/private/var", "/tmp")


def test_macos_file_write_scoped_to_workspace_under_var():
    """file-write* is the workspace folder under /var and /private/var, never all of /var."""
    backend = MacOSSandbox()
    raw = "/var/folders/zz/test/T/workspace"
    priv = "/private/var/folders/zz/test/T/workspace"
    backend._subpaths = lambda _path: [raw, priv]  # noqa: ARG005
    profile = backend._profile(SandboxConfig(readwrite_paths=[Path(raw)]), Path(raw))
    quoted = [
        '(allow file-write* (subpath "/var/folders/zz/test/T/workspace"))',
        '(allow file-write* (subpath "/private/var/folders/zz/test/T/workspace"))',
    ]
    for rule in quoted:
        assert rule in profile, profile
    write_rules = [
        ln.strip() for ln in profile.splitlines()
        if ln.strip().startswith("(allow file-write*")
    ]
    assert write_rules, profile
    assert set(write_rules) == set(quoted), write_rules
    assert '(allow file-write* (subpath "/var"))' not in profile
    assert '(allow file-write* (subpath "/private/var"))' not in profile
    assert '(allow file-read-data (literal "/"))' in profile
    assert '(allow file-read* (subpath "/"))' not in profile
    aliases = MacOSSandbox()._prefix_aliases("/var/folders/zz/test/T/workspace")
    assert "/private/var/folders/zz/test/T/workspace" in aliases
    assert "/var" not in aliases
    assert "/private/var" not in aliases


def test_windows_write_argv_survives_list2cmdline(tmp_path):
    """list2cmdline must not inject backslash-escaped quotes into the /c payload."""
    path = tmp_path / "workspace" / "ok.txt"
    path.parent.mkdir()
    argv = _windows_write_argv(path)
    assert argv[:3] == ["cmd.exe", "/s", "/c"]
    payload = argv[3]
    assert payload.startswith("(echo escaped)>")
    assert '"' not in payload
    cmdline = subprocess.list2cmdline(argv)
    assert '\\"' not in cmdline
    assert 'echo escaped>"' not in cmdline


def test_syntax_error_is_not_a_denied_write(tmp_path):
    class Result:
        returncode = 1
        stdout = b""
        stderr = b"The filename, directory name, or volume label syntax is incorrect.\r\n"

    with pytest.raises(AssertionError):
        _assert_write_denied(Result(), tmp_path / "escape.txt")


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
        timeout = 12.0 if platform.system() == "Windows" else 5.0
        inner_result = await _sandbox_exec(_write_argv(inner), workdir, config, timeout=timeout)
        combined = _combined(inner_result)
        if not inner.exists() and platform.system() == "Darwin":
            deny = combined.decode("utf-8", "replace")
            try:
                shown = subprocess.run(
                    [
                        "log", "show", "--last", "45s", "--style", "compact",
                        "--predicate",
                        'eventMessage CONTAINS[c] "deny"',
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                deny += "\nLOG_SHOW:\n" + ((shown.stdout or "") + (shown.stderr or ""))[-4000:]
            except (OSError, subprocess.TimeoutExpired) as extra:
                deny += f"\nLOG_SHOW_FAIL: {extra}"
            print("TALA_SANDBOX_DENY:", deny)
            assert inner.exists(), (
                inner_result.returncode, inner_result.stdout, inner_result.stderr, deny,
            )
        _assert_write_succeeded(inner_result, inner)
        parent_result = await _sandbox_exec(_write_argv(parent_file), workdir, config, timeout=timeout)
        if (
            platform.system() == "Darwin"
            and not parent_file.exists()
            and not _combined(parent_result).strip()
        ):
            try:
                shown = subprocess.run(
                    [
                        "log", "show", "--last", "45s", "--style", "compact",
                        "--predicate",
                        'eventMessage CONTAINS[c] "deny"',
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                print("TALA_SANDBOX_DENY:", ((shown.stdout or "") + (shown.stderr or ""))[-2000:])
            except (OSError, subprocess.TimeoutExpired) as extra:
                print("TALA_SANDBOX_DENY: LOG_SHOW_FAIL", extra)
        _assert_write_denied(parent_result, parent_file)


@pytest.mark.asyncio
@pytest.mark.skipif(not _select_sandbox_backend().confinement.filesystem, reason="current backend does not confine filesystem")
async def test_escape_outside_workspace_write_blocked():
    with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as other:
        workdir = Path(ws)
        target = Path(other) / "escape.txt"
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        timeout = 12.0 if platform.system() == "Windows" else 5.0
        result = await _sandbox_exec(_write_argv(target), workdir, config, timeout=timeout)
        _assert_write_denied(result, target)


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Linux" or not _select_sandbox_backend().confinement.filesystem, reason="needs Linux FS confinement")
async def test_escape_etc_write_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        result = await _sandbox_exec(["sh", "-c", "echo x > /etc/tala_escape_test || exit 1"], workdir, config, timeout=5.0)
        _assert_not_syntax_error(_combined(result))
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
                workdir, config, timeout=12.0,
            )
            combined = (result.stdout or b"") + (result.stderr or b"")
            assert b"spawn-failed" not in combined, combined
            assert result.returncode != 0, result.stderr
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


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Windows", reason="Windows cmd.exe AppContainer spawn")
async def test_windows_cmd_echo_sandboxed():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        result = await _sandbox_exec(
            ["cmd.exe", "/c", "echo tala-cmd-ok"], workdir, config, timeout=12.0,
        )
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        assert b"tala-cmd-ok" in (result.stdout or b"")


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Windows", reason="Windows PowerShell AppContainer spawn")
async def test_windows_powershell_oneliner_sandboxed():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        result = await _sandbox_exec(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Write-Output 'tala-ps-ok'"],
            workdir, config, timeout=20.0,
        )
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        assert b"tala-ps-ok" in (result.stdout or b"")


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Windows", reason="Windows cmd.exe outside-workspace write")
async def test_windows_cmd_outside_workspace_write_denied():
    with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as other:
        workdir = Path(ws)
        target = Path(other) / "escape.txt"
        config = SandboxConfig(mode="strict", readwrite_paths=[workdir])
        result = await _sandbox_exec(_write_argv(target), workdir, config, timeout=12.0)
        _assert_write_denied(result, target)


def test_renderer_surfaces_sandbox_fallback_like_inference():
    src = Path("renderer/renderer.js").read_text(encoding="utf-8")
    assert "workstation.sandbox_fallback" in src
    assert "inference.fallback" in src


def test_guarded_run_writes_loud_marker(capsys, tmp_path):
    from sdk.agent.sandbox_windows_appcontainer import FALLBACK_MARK, guarded_run
    cmd = ["cmd.exe", "/c", "echo", "hi"] if platform.system() == "Windows" else ["echo", "hi"]
    guarded_run(cmd, str(tmp_path), "unit-test")
    captured = capsys.readouterr()
    assert FALLBACK_MARK in captured.err
    assert "job-object-only" in captured.err
    assert "NOT OS-enforced" in captured.err


def test_access_denied_error_names_path():
    from sdk.agent.sandbox_windows_appcontainer import AccessDenied
    path = r"D:\TalaWorkspace\.tala\venv\Scripts\python.exe"
    exc = AccessDenied(path)
    assert path in str(exc)
    assert "ACCESS DENIED" in str(exc)


def test_system_path_skips_real_windows_not_hostedtoolcache():
    from sdk.agent.sandbox_windows_appcontainer import _is_windows_system_path
    assert not _is_windows_system_path(r"C:\hostedtoolcache\windows\Python\3.11.9\x64")
    assert not _is_windows_system_path(r"D:\TalaWorkspace\.tala\venv\Scripts\python.exe")
    if platform.system() == "Windows":
        root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if root:
            assert _is_windows_system_path(str(Path(root) / "System32" / "cmd.exe"))


def test_rx_paths_never_grant_a_volume_root(tmp_path):
    from sdk.agent.sandbox_windows_appcontainer import _is_volume_root, rx_paths_for_executable
    assert _is_volume_root(Path(tmp_path.anchor))
    assert not _is_volume_root(tmp_path)
    paths = rx_paths_for_executable(sys.executable)
    assert paths
    assert sys.executable in paths or any(Path(p) == Path(sys.executable).resolve() for p in paths)
    for raw in paths:
        assert not _is_volume_root(Path(raw)), raw


def test_wrap_grants_rx_on_interpreter_not_drive(tmp_path):
    from sdk.agent.sandbox import SandboxConfig
    from sdk.agent.sandbox_windows import wrap_appcontainer_argv
    wrapped = wrap_appcontainer_argv([sys.executable, "-c", "print(1)"], SandboxConfig(), tmp_path)
    assert "--grant-read" in wrapped
    grant_reads = [wrapped[i + 1] for i, tok in enumerate(wrapped) if tok == "--grant-read"]
    assert grant_reads
    for path in grant_reads:
        assert Path(path).parent != Path(path), path  # not a volume root
    assert "--grant" in wrapped
    assert tmp_path.anchor not in wrapped and tmp_path.anchor.rstrip("\\") not in wrapped


def test_windows_strict_wrap_includes_status_file(tmp_path):
    from sdk.agent.sandbox import SandboxConfig
    from sdk.agent.sandbox_windows import wrap_appcontainer_argv
    status = tmp_path / "status.json"
    cfg = SandboxConfig(status_file=status)
    wrapped = wrap_appcontainer_argv(["cmd.exe", "/c", "echo", "hi"], cfg, tmp_path)
    assert "--status-file" in wrapped
    assert str(status) in wrapped


def _writable_nonsystem_root(tmp_path: Path) -> Path | None:
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")).anchor.upper()
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:/")
        if not root.exists() or root.anchor.upper() == system:
            continue
        probe = root / f".tala-ac-{os.getpid()}"
        try:
            probe.mkdir(parents=True, exist_ok=True)
            (probe / "w.txt").write_text("ok", encoding="utf-8")
            return probe
        except OSError:
            continue
    return None


def _copy_interpreter(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(sys.executable)
    target = dest / src.name
    shutil.copy2(src, target)
    for pattern in ("python*.dll", "vcruntime*.dll"):
        for dll in src.parent.glob(pattern):
            shutil.copy2(dll, dest / dll.name)
    return target


def _strip_package_acls(path: Path) -> bool:
    icacls = shutil.which("icacls")
    if not icacls:
        return False
    import subprocess
    subprocess.run([icacls, str(path), "/inheritance:d", "/Q"], capture_output=True, text=True)
    for sid in ("*S-1-15-2-1", "*S-1-15-2-2", "ALL APPLICATION PACKAGES", "ALL RESTRICTED APPLICATION PACKAGES"):
        subprocess.run([icacls, str(path), "/remove", sid, "/Q"], capture_output=True, text=True)
    return True


@pytest.mark.asyncio
async def test_nonsystem_volume_workspace_and_interpreter(tmp_path):
    """Workspace + interpreter on a non-system path (real D: if present, else stripped ACL)."""
    if platform.system() != "Windows":
        pytest.skip("Windows non-system volume ACL")
    from sdk.agent.sandbox import SandboxConfig, _sandbox_exec

    root = _writable_nonsystem_root(tmp_path)
    simulated = False
    if root is None:
        root = tmp_path / "nonsystem"
        root.mkdir()
        simulated = True
    workspace = root / "workspace"
    interp_home = root / "interp"
    workspace.mkdir()
    try:
        python = _copy_interpreter(interp_home)
        if simulated and not _strip_package_acls(interp_home):
            pytest.skip("icacls unavailable to simulate a non-system volume")
        if simulated:
            _strip_package_acls(workspace)
        old_home = os.environ.get("PYTHONHOME")
        os.environ["PYTHONHOME"] = sys.prefix
        try:
            result = await _sandbox_exec(
                [str(python), "-c", "print('tala-ac-ok')"],
                workspace,
                SandboxConfig(
                    mode="strict",
                    readwrite_paths=[workspace],
                    readonly_paths=[interp_home, python, Path(sys.prefix)],
                ),
                timeout=15.0,
            )
        finally:
            if old_home is None:
                os.environ.pop("PYTHONHOME", None)
            else:
                os.environ["PYTHONHOME"] = old_home
        out = (result.stdout or b"") + (result.stderr or b"")
        assert result.returncode == 0, out
        assert b"tala-ac-ok" in (result.stdout or b"")
    finally:
        if not simulated and root is not None:
            shutil.rmtree(root, ignore_errors=True)


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
        ("test_macos_profile_lists_resolved_and_raw_paths", "EXECUTE"),
        ("test_macos_file_write_scoped_to_workspace_under_var", "EXECUTE"),
        ("test_windows_write_argv_survives_list2cmdline", "EXECUTE"),
        ("test_syntax_error_is_not_a_denied_write", "EXECUTE"),
        ("test_system_path_skips_real_windows_not_hostedtoolcache", "EXECUTE"),
        ("test_windows_none_mode_does_not_rewrite_argv", "EXECUTE"),
        (
            "test_windows_strict_wrap_uses_launcher",
            "EXECUTE" if system == "Windows" else "SKIP",
        ),
        ("test_renderer_surfaces_sandbox_fallback_like_inference", "EXECUTE"),
        ("test_guarded_run_writes_loud_marker", "EXECUTE"),
        ("test_access_denied_error_names_path", "EXECUTE"),
        ("test_rx_paths_never_grant_a_volume_root", "EXECUTE"),
        ("test_wrap_grants_rx_on_interpreter_not_drive", "EXECUTE"),
        ("test_windows_strict_wrap_includes_status_file", "EXECUTE"),
        (
            "test_nonsystem_volume_workspace_and_interpreter",
            "EXECUTE" if system == "Windows" else "SKIP",
        ),
        (
            "test_windows_cmd_echo_sandboxed",
            "EXECUTE" if system == "Windows" else "SKIP",
        ),
        (
            "test_windows_powershell_oneliner_sandboxed",
            "EXECUTE" if system == "Windows" else "SKIP",
        ),
        (
            "test_windows_cmd_outside_workspace_write_denied",
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
    by_name = dict(rows)
    if system == "Windows":
        assert by_name["test_escape_parent_directory_write_blocked"] == "EXECUTE"
        assert by_name["test_escape_outside_workspace_write_blocked"] == "EXECUTE"
        assert by_name["test_network_isolation"] == "EXECUTE"
        assert by_name["test_nonsystem_volume_workspace_and_interpreter"] == "EXECUTE"
        assert by_name["test_windows_cmd_echo_sandboxed"] == "EXECUTE"
        assert by_name["test_windows_powershell_oneliner_sandboxed"] == "EXECUTE"
        assert by_name["test_windows_cmd_outside_workspace_write_denied"] == "EXECUTE"
    assert executed >= 7
