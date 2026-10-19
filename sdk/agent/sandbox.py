"""
OS-level isolation adapters for workstation commands.

Linux: bubblewrap, else unshare namespaces.
macOS: sandbox-exec (deprecated by Apple, still functional) with an inline -p profile.
Windows: AppContainer (filesystem + network) plus a job object; see
sandbox_windows.py / sandbox_windows_appcontainer.py. Job objects alone do
not confine paths or Winsock.

Callers wrap argv (POSIX and the Windows AppContainer launcher) and/or attach
the spawned process (Windows job object).
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sdk.agent.async_shell import AsyncShellExecutor

log = logging.getLogger(__name__)

SandboxMode = Literal["none", "strict", "permissive"]


@dataclass
class SandboxConfig:
    mode: SandboxMode = "strict"
    allow_network: bool = False
    allow_ipc: bool = False
    readonly_paths: list[Path] = field(default_factory=list)
    readwrite_paths: list[Path] = field(default_factory=list)
    tmpdir: Path | None = None
    uid: int | None = None
    gid: int | None = None
    status_file: Path | None = None


@dataclass
class Confinement:
    filesystem: bool = False
    network: bool = False
    process_job: bool = False
    label: str = "none"


class SandboxBackend:
    confinement = Confinement()

    def is_available(self) -> bool:
        return False

    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        return argv

    def attach(self, proc: Any) -> bool:
        return False

    def cleanup(self) -> None:
        return None


class BubblewrapSandbox(SandboxBackend):
    confinement = Confinement(filesystem=True, network=True, process_job=True, label="bwrap")

    def is_available(self) -> bool:
        return shutil.which("bwrap") is not None

    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        if config.mode == "none":
            return argv
        bwrap = shutil.which("bwrap")
        if not bwrap:
            return argv
        out = [
            bwrap,
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--dir", "/tmp",
            "--proc", "/proc",
            "--dev", "/dev",
        ]
        if Path("/lib64").exists():
            out.extend(["--ro-bind", "/lib64", "/lib64"])
        if Path("/bin").exists():
            out.extend(["--ro-bind", "/bin", "/bin"])
        home = Path.home()
        if home.exists():
            out.extend(["--ro-bind", str(home), str(home)])
        for rw in list(config.readwrite_paths) + [workdir]:
            if rw and Path(rw).exists():
                out.extend(["--bind", str(rw), str(rw)])
        for ro in config.readonly_paths:
            if Path(ro).exists():
                out.extend(["--ro-bind", str(ro), str(ro)])
        if not config.allow_network:
            out.append("--unshare-net")
        if not config.allow_ipc:
            out.append("--unshare-ipc")
        out.extend(["--chdir", str(workdir), "--", *argv])
        return out


class NamespaceContainerSandbox(SandboxBackend):
    confinement = Confinement(filesystem=False, network=True, process_job=True, label="unshare")

    def is_available(self) -> bool:
        return platform.system() == "Linux" and shutil.which("unshare") is not None

    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        if config.mode == "none":
            return argv
        unshare = shutil.which("unshare")
        if not unshare:
            return argv
        flags = [unshare, "--fork", "--pid"]
        if not config.allow_network:
            flags.append("--net")
        if not config.allow_ipc:
            flags.append("--ipc")
        return [*flags, "--", *argv]


class MacOSSandbox(SandboxBackend):
    """sandbox-exec is deprecated (macOS 10.15+) but still runs.

    What it DOES: SBPL profile via ``sandbox-exec -p``. In strict mode it
    denies default, allows process-exec, allows file* under the workspace,
    read-only /usr /System /Library, and denies network unless allow_network.
    Keychain writes are not allowed under deny-default.

    What it DOES NOT: future-proof API, container isolation, or a guarantee
    on every macOS version. Missing sandbox-exec falls through to unsandboxed.
    """

    confinement = Confinement(filesystem=True, network=True, process_job=False, label="sandbox-exec")

    def is_available(self) -> bool:
        return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None

    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        if config.mode == "none":
            return argv
        exe = shutil.which("sandbox-exec")
        if not exe:
            return argv
        profile = self._profile(config, workdir)
        # -p with an inline profile can drop everything after the first
        # newline on some macOS versions, leaving deny-default with no
        # workspace writes (SIGABRT -6, empty stderr). -f keeps the full
        # profile, including both /var and /private/var workspace rules.
        sb = Path(workdir) / ".tala-sandbox.sb"
        try:
            sb.write_text(profile, encoding="utf-8")
        except OSError:
            return [exe, "-p", profile, *argv]
        return [exe, "-f", str(sb), *argv]

    def _prefix_aliases(self, path: str) -> list[str]:
        """Same workspace folder under /var and /private/var (and /tmp), never the prefix root."""
        variants = [path]
        if path.startswith("/private/var/"):
            variants.append("/var/" + path[len("/private/var/"):])
        elif path.startswith("/var/"):
            variants.append("/private" + path)
        if path.startswith("/private/tmp"):
            variants.append(path[len("/private"):])
        elif path.startswith("/tmp/") or path == "/tmp":
            variants.append("/private" + path)
        return variants

    def _subpaths(self, path: Path | str) -> list[str]:
        """sandbox-exec matches vnode paths; /var is a symlink to /private/var."""
        raw = str(path)
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            resolved = raw
        out: list[str] = []
        for item in (raw, resolved):
            if not item:
                continue
            for variant in self._prefix_aliases(item):
                escaped = variant.replace("\\", "\\\\").replace('"', '\\"')
                if escaped not in out:
                    out.append(escaped)
        return out

    def _profile(self, config: SandboxConfig, workdir: Path) -> str:
        lines = ["(version 1)"]
        if config.mode == "strict":
            # deny default. Denied writes on modern macOS SIGABRT the child
            # (empty stderr, returncode -6), so workspace paths must match the
            # vnode (/var is a symlink to /private/var). Writes are the
            # workspace folder only, never all of /var:
            #   (allow file-write* (subpath "/var/folders/.../workspace"))
            #   (allow file-write* (subpath "/private/var/folders/.../workspace"))
            lines += [
                "(deny default)",
                "(allow process-exec*)",
                "(allow process-fork)",
                "(allow signal)",
                "(allow sysctl-read)",
                "(allow file-read-metadata)",
                "(allow file-map-executable)",
                '(allow file-ioctl (literal "/dev/dtracehelper"))',
                '(allow file-write-data (literal "/dev/dtracehelper"))',
                '(allow file-write-data (literal "/dev/null"))',
            ]
            lines.append("(allow network*)" if config.allow_network else "(deny network*)")
            for rw in list(config.readwrite_paths) + [workdir]:
                for sub in self._subpaths(rw):
                    lines.append(f'(allow file-read* (subpath "{sub}"))')
                    lines.append(f'(allow file-write* (subpath "{sub}"))')
            lines += [
                '(allow file-read* (subpath "/usr"))',
                '(allow file-read* (subpath "/bin"))',
                '(allow file-read* (subpath "/sbin"))',
                '(allow file-read* (subpath "/System"))',
                '(allow file-read* (subpath "/Library"))',
                '(allow file-read* (subpath "/tmp"))',
                '(allow file-read* (subpath "/private/tmp"))',
                '(allow file-read* (subpath "/var/folders"))',
                '(allow file-read* (subpath "/private/var/folders"))',
                '(allow file-read* (subpath "/dev"))',
            ]
            for ro in config.readonly_paths:
                for sub in self._subpaths(ro):
                    lines.append(f'(allow file-read* (subpath "{sub}"))')
        else:
            lines += [
                "(allow default)",
                '(deny file-write* (subpath "/System"))',
                '(deny file-write* (subpath "/Library/Keychains"))',
            ]
        return "\n".join(lines)


class WindowsSandbox(SandboxBackend):
    """AppContainer for FS/network; job objects for the process tree.

    See sandbox_windows_appcontainer.py for the CreateProcess path.
    cmd.exe / powershell.exe are resolved with GetSystemDirectoryW, not a
    hardcoded "C:\\Windows\\System32" path (that tree already grants
    ALL APPLICATION PACKAGES, so Tala does not icacls it).
    """

    confinement = Confinement(filesystem=True, network=True, process_job=True, label="appcontainer")

    def __init__(self) -> None:
        self._session = None
        if platform.system() != "Windows":
            return
        try:
            from sdk.agent.sandbox_windows_appcontainer import appcontainer_available
            if appcontainer_available():
                self.confinement = Confinement(
                    filesystem=True, network=True, process_job=True, label="appcontainer",
                )
            else:
                self.confinement = Confinement(process_job=True, label="job-object-only")
        except Exception:
            self.confinement = Confinement(process_job=True, label="job-object-only")

    def is_available(self) -> bool:
        return platform.system() == "Windows"

    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        if config.mode == "none" or platform.system() != "Windows":
            if config.mode == "none":
                self.confinement = Confinement(label="none")
            return argv
        from sdk.agent.sandbox_windows import wrap_appcontainer_argv
        return wrap_appcontainer_argv(argv, config, workdir)

    def attach(self, proc: Any) -> bool:
        pid = getattr(proc, "pid", None)
        if not pid:
            return False
        try:
            from sdk.agent.sandbox_windows import WindowsJobSession
        except Exception as exc:
            log.warning("Windows job module unavailable: %s", exc)
            return False
        session = WindowsJobSession()
        if not session.create():
            log.warning("Windows job create failed: %s", session.error)
            return False
        if not session.assign_pid(pid):
            log.warning("Windows job assign failed: %s", session.error)
            session.cleanup()
            return False
        self._session = session
        return True

    def cleanup(self) -> None:
        if self._session is not None:
            self._session.cleanup()
            self._session = None


class LocalSandbox:
    """Host-local command executor. Same interface as the old DockerSandbox."""

    async def start_container(self, task_id: str, worktree_path: str, port_mappings: dict[int, int] | None = None) -> bool:
        log.debug("LocalSandbox: start_container called for task %s (no-op)", task_id)
        return True

    async def stop_container(self, task_id: str) -> bool:
        log.debug("LocalSandbox: stop_container called for task %s (no-op)", task_id)
        return True

    async def pause_container(self, task_id: str) -> bool:
        return True

    async def resume_container(self, task_id: str) -> bool:
        return True

    async def execute_command(
        self,
        task_id: str,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        effective_cwd = cwd or os.getcwd()
        log.info("LocalSandbox: executing command for task %s in %s: %s", task_id, effective_cwd, command)
        return await AsyncShellExecutor.run(command, cwd=effective_cwd, env_override=env)


DockerSandbox = LocalSandbox


class NoOpSandbox(SandboxBackend):
    confinement = Confinement(label="none")

    def is_available(self) -> bool:
        return True


def _select_sandbox_backend() -> SandboxBackend:
    ordered: list[SandboxBackend] = [
        BubblewrapSandbox(),
        NamespaceContainerSandbox(),
        MacOSSandbox(),
        WindowsSandbox(),
    ]
    for backend in ordered:
        if backend.is_available():
            log.info("Selected sandbox backend: %s", type(backend).__name__)
            return backend
    log.warning("No sandbox backend available")
    return NoOpSandbox()


@dataclass
class SandboxSession:
    argv: list[str]
    backend: SandboxBackend
    enabled: bool
    status_path: Path | None = None

    def attach(self, proc: Any) -> None:
        if self.enabled:
            self.backend.attach(proc)

    def cleanup(self) -> None:
        if self.enabled:
            self.backend.cleanup()


def _protected_write_paths() -> list[Path]:
    """Tala install and user-data folders must never receive a write grant."""
    out: list[Path] = []
    for key in ("TALA_INSTALL_ROOT", "TALA_USER_DATA", "TALA_USER_DATA_ROOT"):
        raw = os.environ.get(key)
        if raw:
            out.append(Path(raw))
    try:
        from sdk.agent.documentation_nodes import resolve_user_data_dir
        out.append(Path(resolve_user_data_dir()))
    except Exception:
        pass
    out.append(Path(__file__).resolve().parents[2])
    return out


def _is_protected_write(path: Path) -> bool:
    child = str(path).replace("\\", "/").lower().rstrip("/")
    for parent in _protected_write_paths():
        base = str(parent).replace("\\", "/").lower().rstrip("/")
        if child == base or child.startswith(base + "/") or base.startswith(child + "/"):
            return True
    return False


def refresh_workspace_isolation(old_paths: list | tuple | None, new_paths: list | tuple | None) -> None:
    """Revoke AppContainer ACLs on workspaces we left; grant on the new ones.

    No-op on non-Windows. Grants are also applied on spawn; this exists so a
    workspace change does not leave the old folder writable by Tala.Workstation.
    """
    if platform.system() != "Windows":
        return
    try:
        from sdk.agent.sandbox_windows_appcontainer import refresh_workspace_acls
        refresh_workspace_acls(old_paths or [], new_paths or [])
    except Exception as exc:
        log.warning("TALA_SANDBOX: could not refresh workspace ACLs: %s", exc)


def prepare_sandbox(
    cfg: Any, argv: list[str], workdir: Path, *, allow_network: bool | None = None,
) -> SandboxSession:
    enabled = bool(getattr(cfg, "enable_sandbox", True))
    if not enabled:
        return SandboxSession(argv=argv, backend=NoOpSandbox(), enabled=False)
    backend = _select_sandbox_backend()
    net = bool(getattr(cfg, "allow_network", False) if allow_network is None else allow_network)
    rw: list[Path] = []
    for raw in [getattr(cfg, "root", None), *list(getattr(cfg, "extra_write_roots", None) or [])]:
        if not raw:
            continue
        path = Path(raw)
        if _is_protected_write(path):
            log.error("TALA_SANDBOX: refusing write grant on protected path %s", path)
            continue
        rw.append(path)
    if workdir and not _is_protected_write(Path(workdir)):
        rw.append(Path(workdir))
    ro: list[Path] = []
    if platform.system() == "Windows":
        try:
            from sdk.agent.sandbox_windows import command_rx_paths
            if argv:
                ro.extend(command_rx_paths(argv[0]))
            extra_py = getattr(cfg, "python", None)
            if extra_py:
                ro.extend(command_rx_paths(extra_py))
        except Exception as exc:
            log.warning("TALA_SANDBOX: could not collect interpreter RX paths: %s", exc)
    status_path = None
    if platform.system() == "Windows":
        tmp = Path(getattr(cfg, "internal_dir", None) or workdir) / "tmp"
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            status_path = tmp / f"sandbox-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        except OSError:
            status_path = None
    config = SandboxConfig(
        mode="strict",
        allow_network=net,
        readwrite_paths=rw,
        readonly_paths=ro,
        tmpdir=getattr(cfg, "internal_dir", None),
        status_file=status_path,
    )
    wrapped = backend.wrap_command(list(argv), config, workdir)
    return SandboxSession(argv=wrapped, backend=backend, enabled=True, status_path=status_path)


async def _sandbox_exec(
    argv: list[str],
    workdir: Path,
    config: SandboxConfig | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    config = config or SandboxConfig()
    backend = _select_sandbox_backend()
    wrapped = backend.wrap_command(list(argv), config, workdir)
    proc = await asyncio.create_subprocess_exec(
        *wrapped,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    backend.attach(proc)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        backend.cleanup()
        raise TimeoutError(f"Command timed out after {timeout}s")
    backend.cleanup()
    return subprocess.CompletedProcess(
        args=wrapped, returncode=proc.returncode, stdout=stdout, stderr=stderr
    )
