"""
Windows AppContainer spawn (ctypes only; no sdk imports).

Used by sandbox_windows_launch.py as a script so wrapping a command does not
import the rest of the SDK.

What this DOES (no admin):
- CreateAppContainerProfile / DeriveAppContainerSidFromAppContainerName
- ACL-grant the workspace (and approved extra roots) to that SID
- CreateProcessW with PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES
- Omit internetClient / privateNetworkClientServer unless allow_network
- Job object KILL_ON_JOB_CLOSE around the child when AssignProcessToJobObject works

What this DOES NOT:
- LPAC. LPAC cannot read most of Program Files / the Python install, so cmd
  and the workstation venv would not start. Standard AppContainer still blocks
  writes outside granted paths and blocks Winsock without capabilities.
- Restricted tokens / CreateRestrictedToken / Windows Sandbox / Hyper-V /
  WFP. Those need admin or a Pro optional-feature.
- Hide world-readable files. AppContainer may still READ Tala's install.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

PROFILE_NAME = "Tala.Workstation"
SE_GROUP_ENABLED = 0x00000004
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0
HANDLE_FLAG_INHERIT = 0x00000001
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
WAIT_OBJECT_0 = 0
STILL_ACTIVE = 259
GENERIC_ALL_DIR = "(OI)(CI)(F)"
GENERIC_READ_DIR = "(OI)(CI)(R)"
CAP_INTERNET_CLIENT = "S-1-15-3-1"
CAP_PRIVATE_NETWORK = "S-1-15-3-3"
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


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


def appcontainer_available() -> bool:
    if os.name != "nt":
        return False
    try:
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        return bool(getattr(userenv, "CreateAppContainerProfile", None))
    except Exception:
        return False


def _k32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION),
    ]
    k32.CreateProcessW.restype = wintypes.BOOL
    k32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    k32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
    ]
    k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
    ]
    k32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    k32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _advapi():
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.FreeSid.argtypes = [ctypes.c_void_p]
    return adv


def _userenv():
    ue = ctypes.WinDLL("userenv", use_last_error=True)
    ue.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    ue.CreateAppContainerProfile.restype = ctypes.HRESULT
    ue.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
    ]
    ue.DeriveAppContainerSidFromAppContainerName.restype = ctypes.HRESULT
    return ue


def _sid_to_string(sid) -> str:
    adv = _advapi()
    out = wintypes.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(out)):
        raise OSError(f"ConvertSidToStringSidW failed ({ctypes.get_last_error()})")
    try:
        return out.value
    finally:
        ctypes.windll.kernel32.LocalFree(out)


def ensure_profile() -> str:
    """Return the AppContainer SID string, creating the profile if needed."""
    ue = _userenv()
    sid = ctypes.c_void_p()
    hr = ue.CreateAppContainerProfile(
        PROFILE_NAME, "Tala workstation", "Tala workstation AppContainer",
        None, 0, ctypes.byref(sid),
    )
    # 0 == S_OK; 0x800700B7 == HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
    already = (hr & 0xFFFFFFFF) in (0x800700B7, 0x80070050)
    if hr >= 0 and sid.value:
        try:
            return _sid_to_string(sid.value)
        finally:
            _advapi().FreeSid(sid)
    if already or hr >= 0:
        sid2 = ctypes.c_void_p()
        hr2 = ue.DeriveAppContainerSidFromAppContainerName(PROFILE_NAME, ctypes.byref(sid2))
        if hr2 < 0 or not sid2.value:
            raise OSError(f"DeriveAppContainerSidFromAppContainerName failed hr=0x{hr2 & 0xFFFFFFFF:08X}")
        try:
            return _sid_to_string(sid2.value)
        finally:
            _advapi().FreeSid(sid2)
    raise OSError(f"CreateAppContainerProfile failed hr=0x{hr & 0xFFFFFFFF:08X}")


def _grant_paths(sid_str: str, write_paths: Sequence[str], read_paths: Sequence[str] = ()) -> None:
    icacls = shutil.which("icacls") or "icacls"
    for path, ace in [*((p, GENERIC_ALL_DIR) for p in write_paths), *((p, GENERIC_READ_DIR) for p in read_paths)]:
        if not path:
            continue
        target = Path(path)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not target.exists():
            continue
        spec = f"*{sid_str}:{ace}"
        result = subprocess.run(
            [icacls, str(target), "/grant", spec, "/Q"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("icacls grant failed for %s: %s %s", target, result.stdout, result.stderr)


def _capability_sids(allow_network: bool) -> list:
    if not allow_network:
        return []
    adv = _advapi()
    sids = []
    for text in (CAP_INTERNET_CLIENT, CAP_PRIVATE_NETWORK):
        sid = ctypes.c_void_p()
        if not adv.ConvertStringSidToSidW(text, ctypes.byref(sid)):
            raise OSError(f"ConvertStringSidToSidW({text}) failed ({ctypes.get_last_error()})")
        sids.append(sid)
    return sids


def _make_pipes(k32):
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = True
    r_out, w_out = wintypes.HANDLE(), wintypes.HANDLE()
    r_err, w_err = wintypes.HANDLE(), wintypes.HANDLE()
    if not k32.CreatePipe(ctypes.byref(r_out), ctypes.byref(w_out), ctypes.byref(sa), 0):
        raise OSError(f"CreatePipe stdout failed ({ctypes.get_last_error()})")
    if not k32.CreatePipe(ctypes.byref(r_err), ctypes.byref(w_err), ctypes.byref(sa), 0):
        raise OSError(f"CreatePipe stderr failed ({ctypes.get_last_error()})")
    k32.SetHandleInformation(r_out, HANDLE_FLAG_INHERIT, 0)
    k32.SetHandleInformation(r_err, HANDLE_FLAG_INHERIT, 0)
    return r_out, w_out, r_err, w_err


def _drain(k32, handle, bucket: list[bytes]) -> None:
    while True:
        buf = ctypes.create_string_buffer(4096)
        n = wintypes.DWORD()
        ok = k32.ReadFile(handle, buf, 4096, ctypes.byref(n), None)
        if not ok or n.value == 0:
            break
        bucket.append(buf.raw[:n.value])


def _job(k32):
    handle = k32.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    )
    info.BasicLimitInformation.ActiveProcessLimit = 32
    if not k32.SetInformationJobObject(
        handle, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info),
    ):
        k32.CloseHandle(handle)
        return None
    return handle


def spawn_appcontainer(
    argv: Sequence[str],
    workdir: str,
    write_paths: Sequence[str],
    allow_network: bool = False,
    read_paths: Sequence[str] = (),
) -> subprocess.CompletedProcess:
    if not argv:
        raise ValueError("argv is empty")
    sid_str = ensure_profile()
    _grant_paths(sid_str, list(write_paths) + [workdir], read_paths)

    adv = _advapi()
    app_sid = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(sid_str, ctypes.byref(app_sid)):
        raise OSError(f"ConvertStringSidToSidW(profile) failed ({ctypes.get_last_error()})")
    cap_sids = _capability_sids(allow_network)
    caps_array = None
    if cap_sids:
        caps_array = (SID_AND_ATTRIBUTES * len(cap_sids))()
        for i, cap in enumerate(cap_sids):
            caps_array[i].Sid = cap
            caps_array[i].Attributes = SE_GROUP_ENABLED
    sc = SECURITY_CAPABILITIES()
    sc.AppContainerSid = app_sid
    if caps_array is not None:
        sc.Capabilities = ctypes.cast(caps_array, ctypes.POINTER(SID_AND_ATTRIBUTES))
    sc.CapabilityCount = len(cap_sids)

    k32 = _k32()
    size = ctypes.c_size_t()
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attr = ctypes.create_string_buffer(size.value)
    if not k32.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(size)):
        raise OSError(f"InitializeProcThreadAttributeList failed ({ctypes.get_last_error()})")
    try:
        if not k32.UpdateProcThreadAttribute(
            attr, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(sc), ctypes.sizeof(sc), None, None,
        ):
            raise OSError(f"UpdateProcThreadAttribute failed ({ctypes.get_last_error()})")
        r_out, w_out, r_err, w_err = _make_pipes(k32)
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        si.StartupInfo.wShowWindow = SW_HIDE
        si.StartupInfo.hStdOutput = w_out
        si.StartupInfo.hStdError = w_err
        si.lpAttributeList = ctypes.cast(attr, ctypes.c_void_p)

        exe = shutil.which(argv[0]) or argv[0]
        cmdline = subprocess.list2cmdline([exe, *list(argv[1:])])
        cmdbuf = ctypes.create_unicode_buffer(cmdline)
        pi = PROCESS_INFORMATION()
        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
        ok = k32.CreateProcessW(
            exe if os.path.isabs(exe) else None,
            cmdbuf, None, None, True, flags, None, str(workdir),
            ctypes.byref(si), ctypes.byref(pi),
        )
        k32.CloseHandle(w_out)
        k32.CloseHandle(w_err)
        if not ok:
            k32.CloseHandle(r_out)
            k32.CloseHandle(r_err)
            raise OSError(f"CreateProcessW(AppContainer) failed ({ctypes.get_last_error()})")
        job = _job(k32)
        if job:
            k32.AssignProcessToJobObject(job, pi.hProcess)
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        t_out = threading.Thread(target=_drain, args=(k32, r_out, out_chunks), daemon=True)
        t_err = threading.Thread(target=_drain, args=(k32, r_err, err_chunks), daemon=True)
        t_out.start()
        t_err.start()
        k32.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        k32.CloseHandle(r_out)
        k32.CloseHandle(r_err)
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        if job:
            k32.CloseHandle(job)
        return subprocess.CompletedProcess(
            args=list(argv), returncode=int(code.value),
            stdout=b"".join(out_chunks), stderr=b"".join(err_chunks),
        )
    finally:
        k32.DeleteProcThreadAttributeList(attr)
        if app_sid:
            adv.FreeSid(app_sid)
        for cap in cap_sids:
            adv.FreeSid(cap)


def guarded_run(argv: Sequence[str], workdir: str, reason: str) -> subprocess.CompletedProcess:
    msg = (
        f"TALA_SANDBOX: guarded-policy-only: AppContainer unavailable ({reason}). "
        "Filesystem and network are NOT OS-enforced.\n"
    )
    sys.stderr.write(msg)
    sys.stderr.flush()
    log.error("%s", msg.strip())
    exe = shutil.which(argv[0]) or argv[0]
    return subprocess.run(
        [exe, *list(argv[1:])], cwd=workdir, capture_output=True, timeout=None,
    )
