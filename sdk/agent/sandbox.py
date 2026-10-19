"""
OS-level isolation adapters for workstation commands.

Linux: bubblewrap, else unshare namespaces.
macOS: sandbox-exec (deprecated by Apple, still functional) with an inline -p profile.
Windows: native job objects via ctypes (see sandbox_windows.py). Job objects do
not confine filesystem or network; those stay policy-only.

Callers wrap argv (POSIX) and/or attach the spawned process (Windows).
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
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
        return [exe, "-p", self._profile(config, workdir), *argv]

    def _profile(self, config: SandboxConfig, workdir: Path) -> str:
        lines = ["(version 1)"]
        if config.mode == "strict":
            lines += ["(deny default)", "(allow process-exec)", "(allow process-fork)", "(allow sysctl-read)"]
            lines.append("(allow network*)" if config.allow_network else "(deny network*)")
            for rw in list(config.readwrite_paths) + [workdir]:
                lines.append(f'(allow file* (subpath "{rw}"))')
            lines += [
                '(allow file-read* (subpath "/usr"))',
                '(allow file-read* (subpath "/bin"))',
                '(allow file-read* (subpath "/sbin"))',
                '(allow file-read* (subpath "/System"))',
                '(allow file-read* (subpath "/Library"))',
                '(allow file-read* (subpath "/private/tmp"))',
                '(allow file-read* (subpath "/dev"))',
            ]
            for ro in config.readonly_paths:
                lines.append(f'(allow file-read* (subpath "{ro}"))')
        else:
            lines += [
                "(allow default)",
                '(deny file-write* (subpath "/System"))',
                '(deny file-write* (subpath "/Library/Keychains"))',
            ]
        return "\n".join(lines)


class WindowsSandbox(SandboxBackend):
    """See sandbox_windows.py. Job objects only; no FS/network confinement."""

    confinement = Confinement(filesystem=False, network=False, process_job=True, label="job-object")

    def __init__(self) -> None:
        self._session = None

    def is_available(self) -> bool:
        return platform.system() == "Windows"

    def wrap_command(self, argv: list[str], config: SandboxConfig, workdir: Path) -> list[str]:
        # argv is unchanged: confinement is AssignProcessToJobObject after spawn.
        if config.mode == "none":
            self.confinement = Confinement(label="none")
        return argv

    def attach(self, proc: Any) -> bool:
        pid = getattr(proc, "pid", None)
        if not pid:
            return False
        try:
            from sdk.agent.sandbox_windows import WindowsJobSession, _enforcement_label
        except Exception as exc:
            log.warning("Windows job module unavailable: %s", exc)
            self.confinement = Confinement(label="guarded-policy-only")
            return False
        session = WindowsJobSession()
        if not session.create():
            log.warning("Windows job create failed: %s", session.error)
            self.confinement = Confinement(label=_enforcement_label(False))
            return False
        if not session.assign_pid(pid):
            log.warning("Windows job assign failed: %s", session.error)
            session.cleanup()
            self.confinement = Confinement(label=_enforcement_label(False))
            return False
        self._session = session
        self.confinement = Confinement(process_job=True, label=_enforcement_label(True))
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

    def attach(self, proc: Any) -> None:
        if self.enabled:
            self.backend.attach(proc)

    def cleanup(self) -> None:
        if self.enabled:
            self.backend.cleanup()


def prepare_sandbox(cfg: Any, argv: list[str], workdir: Path) -> SandboxSession:
    enabled = bool(getattr(cfg, "enable_sandbox", True))
    if not enabled:
        return SandboxSession(argv=argv, backend=NoOpSandbox(), enabled=False)
    backend = _select_sandbox_backend()
    config = SandboxConfig(
        mode="strict",
        allow_network=bool(getattr(cfg, "allow_network", False)),
        readwrite_paths=[p for p in [getattr(cfg, "root", None)] if p],
        tmpdir=getattr(cfg, "internal_dir", None),
    )
    wrapped = backend.wrap_command(list(argv), config, workdir)
    return SandboxSession(argv=wrapped, backend=backend, enabled=True)


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
