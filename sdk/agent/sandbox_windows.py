"""
Windows isolation adapter surface used by sandbox.py.

Job objects (this file) still wrap the launcher so the tree dies on close.
Filesystem and network confinement live in sandbox_windows_appcontainer.py:
CreateAppContainerProfile + ACL grants + CreateProcess SECURITY_CAPABILITIES.

What AppContainer DOES (no admin at runtime):
- Writes only where we ACL-grant the AppContainer SID (workspace + extra roots)
- No writes to Tala's install or user-data folder (those paths are never granted)
- No Winsock unless internetClient / privateNetworkClientServer are passed
- First-run CreateAppContainerProfile is per-user; no elevated setup step

What this DOES NOT:
- Restricted tokens / CreateRestrictedToken (not implemented)
- LPAC (would break Python/cmd on a normal desktop)
- Windows Sandbox / Hyper-V / WFP (admin or optional-feature)
- Hide world-readable files (the child may still READ the install)

If AppContainer APIs fail, the launcher prints TALA_SANDBOX: job-object-only
and runs with a job object only (filesystem and network are NOT OS-enforced).
That fallback is written to a status file so the workstation tool result, UI
notice, and audit log can surface it. Job-object attach() failing is still
job-object-only for the process tree.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_ASSIGN = PROCESS_SET_QUOTA | PROCESS_TERMINATE


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


class WindowsJobSession:
    """One job object around a single spawned process. Close in cleanup()."""

    def __init__(self) -> None:
        self.handle: Any = None
        self.assigned = False
        self.error: str | None = None

    def create(self, active_process_limit: int = 32) -> bool:
        try:
            k32 = _kernel32()
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                self.error = f"CreateJobObjectW failed ({ctypes.get_last_error()})"
                return False
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            )
            info.BasicLimitInformation.ActiveProcessLimit = active_process_limit
            ok = k32.SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                self.error = f"SetInformationJobObject failed ({ctypes.get_last_error()})"
                k32.CloseHandle(handle)
                return False
            self.handle = handle
            return True
        except Exception as exc:
            self.error = str(exc)
            log.warning("Windows job object create failed: %s", exc)
            return False

    def assign_pid(self, pid: int) -> bool:
        if not self.handle:
            return False
        try:
            k32 = _kernel32()
            proc = k32.OpenProcess(PROCESS_ASSIGN, False, int(pid))
            if not proc:
                self.error = f"OpenProcess failed ({ctypes.get_last_error()})"
                return False
            try:
                ok = k32.AssignProcessToJobObject(self.handle, proc)
                if not ok:
                    self.error = f"AssignProcessToJobObject failed ({ctypes.get_last_error()})"
                    return False
                self.assigned = True
                return True
            finally:
                k32.CloseHandle(proc)
        except Exception as exc:
            self.error = str(exc)
            log.warning("Windows job assign failed: %s", exc)
            return False

    def cleanup(self) -> None:
        if not self.handle:
            return
        try:
            _kernel32().CloseHandle(self.handle)
        except Exception:
            pass
        self.handle = None


def _enforcement_label(assigned: bool) -> str:
    return "job-object" if assigned else "job-object-only"


def command_rx_paths(executable: str | Path) -> list[Path]:
    """Specific interpreter/venv/tool paths to RX-grant. Never a volume root."""
    from sdk.agent.sandbox_windows_appcontainer import rx_paths_for_executable
    return [Path(p) for p in rx_paths_for_executable(str(executable or ""))]


def wrap_appcontainer_argv(argv: list[str], config: Any, workdir: Path) -> list[str]:
    """Prepend the AppContainer launcher. sandbox.py wrap_command calls this."""
    launch = Path(__file__).resolve().parent / "sandbox_windows_launch.py"
    out = [sys.executable, str(launch), "--workdir", str(workdir)]
    if getattr(config, "allow_network", False):
        out.append("--allow-network")
    status = getattr(config, "status_file", None)
    if status:
        out.extend(["--status-file", str(status)])
    seen: set[str] = set()
    for raw in list(getattr(config, "readwrite_paths", None) or []) + [workdir]:
        if not raw:
            continue
        path = str(Path(raw))
        if path in seen:
            continue
        seen.add(path)
        out.extend(["--grant", path])
    read_seen: set[str] = set()
    extra_read: list[Path] = list(getattr(config, "readonly_paths", None) or [])
    if argv:
        extra_read.extend(command_rx_paths(argv[0]))
    for raw in extra_read:
        if not raw:
            continue
        path = str(Path(raw))
        if path in read_seen or path in seen:
            continue
        read_seen.add(path)
        out.extend(["--grant-read", path])
    out.append("--")
    out.extend(argv)
    return out
