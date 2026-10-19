"""
Windows process confinement via native job objects (ctypes).

What this DOES when the APIs succeed:
- CreateJobObjectW
- JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (the tree dies when we close the job)
- JOB_OBJECT_LIMIT_ACTIVE_PROCESS (caps how many processes the job may run)
- AssignProcessToJobObject on the spawned pid

What this DOES NOT do:
- Filesystem confinement. Job objects do not sandbox paths. Writes outside the
  workspace are still only blocked by workstation_policy.py before spawn.
- Network confinement. No WFP / AppContainer here.
- Restricted tokens / CreateRestrictedToken / CreateProcessAsUser. Privilege
  drop is not implemented. A process in this job still runs as the Tala user.

If the ctypes calls fail (common when the Python process is already in a job,
as on many CI runners), attach() returns False and enforcement is
guarded-policy-only: the command still runs, policy deny-lists still apply,
nothing OS-level is enforced.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
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


def enforcement_label(assigned: bool) -> str:
    return "job-object" if assigned else "guarded-policy-only"
