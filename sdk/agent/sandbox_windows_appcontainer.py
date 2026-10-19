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
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12
WAIT_OBJECT_0 = 0
STILL_ACTIVE = 259
GENERIC_ALL_DIR = "(OI)(CI)(F)"
GENERIC_RX_DIR = "(OI)(CI)(RX)"
GENERIC_RX_FILE = "(RX)"
ERROR_ACCESS_DENIED = 5
FALLBACK_MARK = "TALA_SANDBOX: job-object-only"
ACCESS_DENIED_MARK = "TALA_SANDBOX: access-denied"
CAP_INTERNET_CLIENT = "S-1-15-3-1"
CAP_PRIVATE_NETWORK = "S-1-15-3-3"
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class AccessDenied(OSError):
    """AppContainer SID cannot read or execute a specific path."""

    def __init__(self, path: str, winerr: int = ERROR_ACCESS_DENIED):
        self.path = path
        self.winerr = winerr
        super().__init__(
            f"AppContainer ACCESS DENIED on {path} (Win32 {winerr}). "
            "The sandboxed process has no read+execute ACL on this specific path. "
            "Non-system drives (for example D:) usually lack 'ALL APPLICATION PACKAGES'; "
            "Tala grants only the interpreter/venv/tool paths it runs, never a whole drive."
        )


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
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.GetStdHandle.restype = wintypes.HANDLE
    return k32


def _advapi():
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.FreeSid.argtypes = [ctypes.c_void_p]
    adv.IsValidSid.argtypes = [ctypes.c_void_p]
    adv.IsValidSid.restype = wintypes.BOOL
    return adv


def _userenv():
    ue = ctypes.WinDLL("userenv", use_last_error=True)
    ue.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    # ctypes.HRESULT restype RAISES OSError on FAILED(hr). ERROR_ALREADY_EXISTS
    # then never reaches our handler and the launcher falls back to unsandboxed.
    ue.CreateAppContainerProfile.restype = ctypes.c_long
    ue.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
    ]
    ue.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    ue.GetAppContainerFolderPath.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR),
    ]
    ue.GetAppContainerFolderPath.restype = ctypes.c_long
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


_HR_ALREADY_EXISTS = 0x800700B7
_HR_FILE_EXISTS = 0x80070050


def _hr_ok(hr: int) -> bool:
    return int(hr) >= 0


def _hr_already(hr: int) -> bool:
    code = int(hr) & 0xFFFFFFFF
    return code in (_HR_ALREADY_EXISTS, _HR_FILE_EXISTS, 183)


def _appcontainer_folder_path(sid_str: str) -> tuple[int, str]:
    """GetAppContainerFolderPath; empty path if the API or folder lookup fails."""
    ue = _userenv()
    out = wintypes.LPWSTR()
    hr = ue.GetAppContainerFolderPath(sid_str, ctypes.byref(out))
    path = ""
    try:
        if out.value:
            path = out.value
    finally:
        if out.value:
            ctypes.windll.ole32.CoTaskMemFree(out)
    return int(hr) & 0xFFFFFFFF, path


def _create_appcontainer_profile() -> tuple[int, str]:
    """Always call CreateAppContainerProfile. Derive SID only if the profile already exists.

    DeriveAppContainerSidFromAppContainerName succeeds without creating the
    package folder or registry hive. CreateProcess then returns Win32 2.
    """
    ue = _userenv()
    sid = ctypes.c_void_p()
    try:
        hr = ue.CreateAppContainerProfile(
            PROFILE_NAME, "Tala workstation", "Tala workstation AppContainer",
            None, 0, ctypes.byref(sid),
        )
    except OSError as exc:
        winerr = getattr(exc, "winerror", 0) or 0
        if _hr_already(winerr) or "already exists" in str(exc).lower():
            hr = ctypes.c_long(_HR_ALREADY_EXISTS).value
            sid = ctypes.c_void_p()
        else:
            raise
    hr_u = int(hr) & 0xFFFFFFFF
    if _hr_ok(hr) and sid.value:
        try:
            return hr_u, _sid_to_string(sid.value)
        finally:
            _advapi().FreeSid(sid)
    if not _hr_already(hr):
        raise OSError(f"CreateAppContainerProfile failed hr=0x{hr_u:08X}")
    sid2 = ctypes.c_void_p()
    hr2 = ue.DeriveAppContainerSidFromAppContainerName(PROFILE_NAME, ctypes.byref(sid2))
    if not _hr_ok(hr2) or not sid2.value:
        raise OSError(
            f"DeriveAppContainerSidFromAppContainerName failed hr=0x{int(hr2) & 0xFFFFFFFF:08X}"
        )
    try:
        return hr_u, _sid_to_string(sid2.value)
    finally:
        _advapi().FreeSid(sid2)


def ensure_profile() -> str:
    """Create the AppContainer profile if needed; return its SID string."""
    _hr, sid_str = _create_appcontainer_profile()
    return sid_str


def _is_volume_root(path: Path | str) -> bool:
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return resolved.parent == resolved


def _is_windows_system_path(path: Path | str) -> bool:
    """Skip ACL grants on %SystemRoot% / Program Files, not any path containing 'windows'.

    ``C:\\hostedtoolcache\\windows\\Python`` is a CI interpreter home, not System32.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    low = str(resolved).replace("/", "\\").rstrip("\\").lower()
    roots: list[str] = []
    for key in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        val = os.environ.get(key)
        if val:
            roots.append(str(Path(val)).replace("/", "\\").rstrip("\\").lower())
    extra: list[str] = []
    for root in roots:
        extra.extend((f"{root}\\system32", f"{root}\\syswow64", f"{root}\\sysnative"))
    for root in (*roots, *extra):
        if low == root or low.startswith(root + "\\"):
            return True
    return False


def rx_paths_for_executable(executable: str) -> list[str]:
    """Read+execute grants for a specific interpreter or tool; never a whole drive."""
    if not executable:
        return []
    raw = Path(executable)
    if not raw.exists():
        found = shutil.which(str(executable))
        raw = Path(found) if found else raw
    out: list[str] = []

    def add(candidate: Path) -> None:
        try:
            candidate = candidate.resolve()
        except OSError:
            return
        if not candidate.exists() or _is_volume_root(candidate) or _is_windows_system_path(candidate):
            return
        text = str(candidate)
        if text not in out:
            out.append(text)

    if raw.is_file():
        add(raw)
        add(raw.parent)
        if raw.parent.name.lower() in ("scripts", "bin"):
            venv = raw.parent.parent
            add(venv)
            cfg = venv / "pyvenv.cfg"
            if cfg.is_file():
                try:
                    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.lower().startswith("home"):
                            home = Path(line.split("=", 1)[-1].strip().strip('"'))
                            add(home)
                            py_name = "python.exe" if os.name == "nt" else "python"
                            add(home / py_name)
                            add(home / "DLLs")
                            add(home / "Lib")
                except OSError:
                    pass
        add(raw.parent / "DLLs")
        add(raw.parent / "Lib")
    elif raw.is_dir():
        add(raw)
    return out


def _ace_for(target: Path, write: bool) -> str:
    if write:
        return GENERIC_ALL_DIR if target.is_dir() else "(F)"
    return GENERIC_RX_DIR if target.is_dir() else GENERIC_RX_FILE


def _grant_paths(sid_str: str, write_paths: Sequence[str], read_paths: Sequence[str] = ()) -> None:
    icacls = shutil.which("icacls") or "icacls"
    items: list[tuple[str, bool]] = [(p, True) for p in write_paths] + [(p, False) for p in read_paths]
    for path, write in items:
        if not path:
            continue
        try:
            target = Path(path).resolve()
        except OSError:
            target = Path(path)
        if _is_volume_root(target) or _is_windows_system_path(target):
            log.warning("TALA_SANDBOX: refusing ACL grant on %s", target)
            continue
        if write:
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        if not target.exists():
            continue
        spec = f"*{sid_str}:{_ace_for(target, write)}"
        result = subprocess.run(
            [icacls, str(target), "/grant:r", spec, "/Q"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("icacls grant failed for %s: %s %s", target, result.stdout, result.stderr)


def _revoke_paths(sid_str: str, paths: Sequence[str]) -> None:
    icacls = shutil.which("icacls") or "icacls"
    for path in paths:
        if not path:
            continue
        try:
            target = Path(path).resolve()
        except OSError:
            target = Path(path)
        if _is_volume_root(target) or _is_windows_system_path(target) or not target.exists():
            continue
        result = subprocess.run(
            [icacls, str(target), "/remove", f"*{sid_str}", "/Q"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("icacls revoke failed for %s: %s %s", target, result.stdout, result.stderr)


def refresh_workspace_acls(old_paths: Sequence[str], new_paths: Sequence[str]) -> None:
    """Idempotent: revoke the SID on workspaces we left, grant:r on the new ones."""
    if os.name != "nt":
        return
    try:
        sid = ensure_profile()
    except Exception as exc:
        log.warning("TALA_SANDBOX: cannot refresh workspace ACLs (%s)", exc)
        return
    old_n, new_n, seen_old, seen_new = [], [], set(), set()
    for raw in old_paths or []:
        if not raw:
            continue
        text = str(Path(raw))
        if text not in seen_old:
            seen_old.add(text)
            old_n.append(text)
    for raw in new_paths or []:
        if not raw:
            continue
        text = str(Path(raw))
        if text not in seen_new:
            seen_new.add(text)
            new_n.append(text)
    _revoke_paths(sid, [p for p in old_n if p not in seen_new])
    _grant_paths(sid, new_n, ())


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


def _system_directory() -> Path:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    k32.GetSystemDirectoryW.restype = wintypes.UINT
    buf = ctypes.create_unicode_buffer(4096)
    n = k32.GetSystemDirectoryW(buf, 4096)
    if n:
        return Path(buf.value)
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or ""
    return Path(root) / "System32"


def _resolve_launch_exe(argv0: str) -> str:
    """Canonical System32 path for cmd/PowerShell; shutil.which otherwise.

    AppContainer CreateProcess looks up the image from the container's view.
    A bare ``cmd.exe`` depends on PATH inside that view; the System32 path
    does not. System32 is expected to already grant ALL APPLICATION PACKAGES;
    this helper does not ACL-grant Windows directories.
    """
    name = Path(argv0).name.lower()
    if os.name == "nt" and name in ("cmd.exe", "powershell.exe"):
        sys32 = _system_directory()
        candidate = (
            sys32 / "cmd.exe" if name == "cmd.exe"
            else sys32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        )
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(argv0)
    if found:
        try:
            return str(Path(found).resolve())
        except OSError:
            return found
    return argv0


def spawn_appcontainer(
    argv: Sequence[str],
    workdir: str,
    write_paths: Sequence[str],
    allow_network: bool = False,
    read_paths: Sequence[str] = (),
) -> subprocess.CompletedProcess:
    if not argv:
        raise ValueError("argv is empty")
    try:
        workdir = str(Path(workdir).resolve())
    except OSError:
        workdir = str(workdir)
    sid_str = ensure_profile()
    exe = _resolve_launch_exe(str(argv[0]))
    merged_read: list[str] = []
    seen_read: set[str] = set()
    for raw in [*list(read_paths), *rx_paths_for_executable(str(exe))]:
        if not raw:
            continue
        text = str(raw)
        if text in seen_read:
            continue
        seen_read.add(text)
        merged_read.append(text)
    _grant_paths(sid_str, list(write_paths) + [workdir], merged_read)

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
    # Microsoft AppContainer samples: one attribute (SECURITY_CAPABILITIES),
    # lpApplicationName=NULL, parent std handles (not new pipes). New pipes
    # plus HANDLE_LIST still returned Win32 2 on Windows Server CI for cmd,
    # PowerShell, and a granted D: python.exe.
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attr = (ctypes.c_ubyte * max(int(size.value), 1))()
    if not k32.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(size)):
        raise OSError(f"InitializeProcThreadAttributeList failed ({ctypes.get_last_error()})")
    try:
        if not k32.UpdateProcThreadAttribute(
            attr, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(sc), ctypes.sizeof(sc), None, None,
        ):
            raise OSError(f"UpdateProcThreadAttribute failed ({ctypes.get_last_error()})")
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        # LPWSTR field; a c_wchar array raises "incompatible types" on assign.
        si.StartupInfo.lpDesktop = "WinSta0\\Default"
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        si.StartupInfo.wShowWindow = SW_HIDE
        si.StartupInfo.hStdInput = k32.GetStdHandle(STD_INPUT_HANDLE)
        si.StartupInfo.hStdOutput = k32.GetStdHandle(STD_OUTPUT_HANDLE)
        si.StartupInfo.hStdError = k32.GetStdHandle(STD_ERROR_HANDLE)
        si.lpAttributeList = ctypes.c_void_p(ctypes.addressof(attr))

        quoted = subprocess.list2cmdline([exe, *list(argv[1:])])
        flags = EXTENDED_STARTUPINFO_PRESENT

        def _create(app_name, cwd):
            # CreateProcessW mutates the command-line buffer; allocate a fresh one.
            cmdbuf = ctypes.create_unicode_buffer(quoted)
            procinfo = PROCESS_INFORMATION()
            ok_ = k32.CreateProcessW(
                app_name, cmdbuf, None, None, True, flags, None, cwd,
                ctypes.byref(si), ctypes.byref(procinfo),
            )
            return ok_, procinfo, 0 if ok_ else ctypes.get_last_error()

        # Diagnostic on windows-latest: same STARTUPINFOEXW without
        # SECURITY_CAPABILITIES starts cmd (exit 0). With the attribute,
        # CreateProcessW returns Win32 2 for cwd=System32 and a granted
        # workspace. That is AppContainer image lookup, not struct plumbing.
        ok, pi, err = _create(None, str(workdir))
        if not ok and err == 2:
            ok, pi, err = _create(None, None)
        if not ok and err == 2 and os.path.isabs(exe):
            ok, pi, err = _create(exe, str(workdir))
        if not ok:
            if err == ERROR_ACCESS_DENIED:
                raise AccessDenied(str(exe), err)
            raise OSError(
                f"CreateProcessW(AppContainer) Win32 {err} for {exe!r} "
                f"cmdline={quoted!r} cwd={workdir!r} "
                f"SI={ctypes.sizeof(STARTUPINFOW)} EX={ctypes.sizeof(STARTUPINFOEXW)}. "
                "Tried lpApplicationName=NULL (WinSta0\\Default, then no desktop, "
                "then cwd=NULL) then the absolute path. "
                "Win32 2 is the AppContainer's path lookup (not the caller's); "
                "System32 is expected to already grant ALL APPLICATION PACKAGES "
                "so Tala does not icacls it. If cmd/PowerShell still cannot start, "
                "the sandboxed shell cannot run on this host."
            )
        job = _job(k32)
        if job:
            k32.AssignProcessToJobObject(job, pi.hProcess)
        k32.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        if job:
            k32.CloseHandle(job)
        return subprocess.CompletedProcess(
            args=list(argv), returncode=int(code.value), stdout=b"", stderr=b"",
        )
    finally:
        k32.DeleteProcThreadAttributeList(attr)
        if app_sid:
            adv.FreeSid(app_sid)
        for cap in cap_sids:
            adv.FreeSid(cap)


def _diagnose_createprocess() -> int:
    """CI diagnostic: ctypes sizes, attribute list, SID, cwd, control spawns.

    Prints TALA_AC_DIAG lines and always returns 0 so the log is complete even
    when AppContainer CreateProcess returns Win32 2.
    """
    def emit(msg: str) -> None:
        sys.stdout.write(f"TALA_AC_DIAG: {msg}\n")
        sys.stdout.flush()

    if os.name != "nt":
        emit("skip not-windows")
        return 0
    import tempfile
    k32 = _k32()
    adv = _advapi()
    emit(f"sizeof STARTUPINFOW={ctypes.sizeof(STARTUPINFOW)} STARTUPINFOEXW={ctypes.sizeof(STARTUPINFOEXW)}")
    emit(f"sizeof SECURITY_CAPABILITIES={ctypes.sizeof(SECURITY_CAPABILITIES)}")
    emit(f"PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES=0x{PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES:08X}")
    emit(f"EXTENDED_STARTUPINFO_PRESENT=0x{EXTENDED_STARTUPINFO_PRESENT:08X}")
    cmd = _resolve_launch_exe("cmd.exe")
    emit(f"cmd={cmd!r} exists={os.path.isfile(cmd)}")
    sys32 = str(_system_directory())
    emit(f"System32={sys32!r}")
    quoted = subprocess.list2cmdline([cmd, "/c", "echo tala-diag-ok"])
    flags = EXTENDED_STARTUPINFO_PRESENT
    emit(f"flags=0x{flags:08X} EX_PRESENT_set={bool(flags & EXTENDED_STARTUPINFO_PRESENT)}")

    def last() -> int:
        return int(ctypes.get_last_error())

    def spawn(label: str, si, cwd, app_name=None, use_flags=None) -> None:
        fl = flags if use_flags is None else use_flags
        cmdbuf = ctypes.create_unicode_buffer(quoted)
        pi = PROCESS_INFORMATION()
        ok = k32.CreateProcessW(
            app_name, cmdbuf, None, None, True, fl, None, cwd,
            ctypes.byref(si), ctypes.byref(pi),
        )
        err = 0 if ok else last()
        cb = int(si.StartupInfo.cb)
        emit(
            f"{label} ok={bool(ok)} Win32={err} cb={cb} flags=0x{fl:08X} "
            f"EX_PRESENT_set={bool(fl & EXTENDED_STARTUPINFO_PRESENT)} "
            f"cb_vs_EX={cb == ctypes.sizeof(STARTUPINFOEXW)} cwd={cwd!r} "
            f"lpAttributeList={si.lpAttributeList}"
        )
        if ok:
            k32.WaitForSingleObject(pi.hProcess, 8000)
            code = wintypes.DWORD()
            k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
            emit(f"{label} exit={int(code.value)}")
            k32.CloseHandle(pi.hThread)
            k32.CloseHandle(pi.hProcess)

    sip = STARTUPINFOW()
    sip.cb = ctypes.sizeof(STARTUPINFOW)
    cmdbuf = ctypes.create_unicode_buffer(quoted)
    pi0 = PROCESS_INFORMATION()
    ok_plain = k32.CreateProcessW(
        None, cmdbuf, None, None, True, 0, None, sys32,
        ctypes.byref(sip), ctypes.byref(pi0),
    )
    err_plain = 0 if ok_plain else last()
    emit(f"control_plain_startupinfo ok={bool(ok_plain)} Win32={err_plain} cb={sip.cb} flags=0")
    if ok_plain:
        k32.WaitForSingleObject(pi0.hProcess, 8000)
        code0 = wintypes.DWORD()
        k32.GetExitCodeProcess(pi0.hProcess, ctypes.byref(code0))
        emit(f"control_plain_startupinfo exit={int(code0.value)}")
        k32.CloseHandle(pi0.hThread)
        k32.CloseHandle(pi0.hProcess)

    # Same STARTUPINFOEXW + EXTENDED_STARTUPINFO_PRESENT, no SECURITY_CAPABILITIES.
    si_null = STARTUPINFOEXW()
    si_null.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si_null.lpAttributeList = None
    spawn("control_no_sc_null_attr", si_null, sys32)

    size0 = ctypes.c_size_t()
    ok0 = k32.InitializeProcThreadAttributeList(None, 0, 0, ctypes.byref(size0))
    emit(f"init_list count=0 probe ok={bool(ok0)} last={last()} size={int(size0.value)}")
    attr0 = (ctypes.c_ubyte * max(int(size0.value), 1))()
    ok0b = k32.InitializeProcThreadAttributeList(attr0, 0, 0, ctypes.byref(size0))
    emit(f"init_list count=0 alloc ok={bool(ok0b)} last={last()} addr={ctypes.addressof(attr0)}")
    si0 = STARTUPINFOEXW()
    si0.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si0.lpAttributeList = ctypes.c_void_p(ctypes.addressof(attr0))
    emit(f"control_no_sc_empty_list cb={si0.StartupInfo.cb} attr={si0.lpAttributeList}")
    spawn("control_no_sc_empty_list", si0, sys32)
    try:
        k32.DeleteProcThreadAttributeList(attr0)
    except OSError as exc:
        emit(f"delete empty list {exc}")

    try:
        hr_create, sid_str = _create_appcontainer_profile()
    except Exception as exc:
        emit(f"CreateAppContainerProfile FAIL {type(exc).__name__}: {exc}")
        emit("done")
        return 0
    emit(
        f"CreateAppContainerProfile hr=0x{hr_create:08X} "
        f"S_OK={hr_create == 0} ALREADY_EXISTS={hr_create == _HR_ALREADY_EXISTS}"
    )
    folder_hr, folder = _appcontainer_folder_path(sid_str)
    folder_exists = bool(folder) and Path(folder).exists()
    emit(
        f"GetAppContainerFolderPath hr=0x{folder_hr:08X} path={folder!r} exists={folder_exists}"
    )
    emit(f"sid={sid_str}")
    app_sid = ctypes.c_void_p()
    ok_sid = adv.ConvertStringSidToSidW(sid_str, ctypes.byref(app_sid))
    emit(f"ConvertStringSidToSidW ok={bool(ok_sid)} last={last()} ptr={app_sid.value}")
    valid = bool(adv.IsValidSid(app_sid)) if ok_sid else False
    emit(f"IsValidSid={valid} last={last()}")
    sc = SECURITY_CAPABILITIES()
    sc.AppContainerSid = app_sid
    sc.CapabilityCount = 0
    emit(f"SC.AppContainerSid={sc.AppContainerSid} CapabilityCount={sc.CapabilityCount}")

    size = ctypes.c_size_t()
    okp = k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    emit(f"init_list count=1 probe ok={bool(okp)} last={last()} size={int(size.value)}")
    attr = (ctypes.c_ubyte * max(int(size.value), 1))()
    oka = k32.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(size))
    emit(f"init_list count=1 alloc ok={bool(oka)} last={last()} addr={ctypes.addressof(attr)}")
    oku = k32.UpdateProcThreadAttribute(
        attr, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        ctypes.byref(sc), ctypes.sizeof(sc), None, None,
    )
    emit(
        f"UpdateProcThreadAttribute(SECURITY_CAPABILITIES=0x{PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES:08X}) "
        f"ok={bool(oku)} last={last()} sc_size={ctypes.sizeof(sc)}"
    )
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si.lpAttributeList = ctypes.c_void_p(ctypes.addressof(attr))
    emit(
        f"si.cb={si.StartupInfo.cb} sizeofEX={ctypes.sizeof(STARTUPINFOEXW)} "
        f"attr_field={si.lpAttributeList} addrof_attr={ctypes.addressof(attr)}"
    )
    spawn("ac_cwd_system32", si, sys32)
    tmp = tempfile.mkdtemp(prefix="tala-ac-diag-")
    emit(f"workspace={tmp}")
    try:
        _grant_paths(sid_str, [tmp], [])
        here = Path(tmp)
        while True:
            ic = subprocess.run(
                ["icacls", str(here)], capture_output=True, text=True, timeout=20,
            )
            blob = ((ic.stdout or "") + (ic.stderr or "")).replace("\r", " ").strip()
            emit(f"icacls {here} rc={ic.returncode} {blob[:400]}")
            if here.parent == here:
                break
            here = here.parent
        spawn("ac_cwd_workspace", si, tmp)
    finally:
        k32.DeleteProcThreadAttributeList(attr)
        if app_sid:
            adv.FreeSid(app_sid)
        shutil.rmtree(tmp, ignore_errors=True)
    emit("done")
    return 0


def guarded_run(argv: Sequence[str], workdir: str, reason: str) -> subprocess.CompletedProcess:
    msg = (
        f"{FALLBACK_MARK}: AppContainer unavailable ({reason}). "
        "Job object only — filesystem and network are NOT OS-enforced "
        "(not AppContainer).\n"
    )
    sys.stderr.write(msg)
    sys.stderr.flush()
    log.error("%s", msg.strip())
    exe = shutil.which(argv[0]) or argv[0]
    return subprocess.run(
        [exe, *list(argv[1:])], cwd=workdir, capture_output=True, timeout=None,
    )
