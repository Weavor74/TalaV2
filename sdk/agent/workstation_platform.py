"""
workstation_platform.py -- every OS-specific detail of the workstation tools, in one place.

The workstation (sdk/agent/workstation.py) and its policy
(sdk/agent/workstation_policy.py) are OS-neutral: they work on pathlib paths
and lower-cased, forward-slash text. Everything that differs between Windows,
Linux (including the Jetson, aarch64) and macOS lives here, as data on one
``Platform`` object per OS family:

* deny patterns: a COMMON set plus one set per OS (Windows system folder and
  credential stores; Linux /etc, /boot, /proc, /sys and keyrings; macOS
  /System and Keychains), matched against canonical paths and command text;
* system folders that can never be a workspace;
* command-risk patterns and command-position aliases for that OS's shell;
* which absolute-path / environment-variable forms to look for in commands;
* how to start a shell (PowerShell on Windows, bash or sh elsewhere), how to
  lower a child's priority, cache/TEMP defaults, the venv python location and
  the suggested default workspace.

``current()`` picks the host's family. Tests select another family from
``ADAPTERS`` (monkeypatching ``current``), so the Linux and macOS
adapters are exercised on any CI host. Porting to a new OS means adding one
``Platform`` here; nothing else should need an ``os.name`` check.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

WINDOWS, LINUX, MACOS = "windows", "linux", "macos"

# Pattern fragment: start of a path token (start of text, whitespace, quote, = or ().
_B = r"(?:^|[\s'\"=(])"
# ... and its end (separator, end of text, whitespace or quote).
_E = r"(?:/|$|[\s'\"])"

# ---------------------------------------------------------------------------
# Deny patterns: (regex searched in canonical lower-case forward-slash text, why)
# ---------------------------------------------------------------------------

COMMON_DENY: tuple[tuple[str, str], ...] = (
    (r"trike", "Trike Trails is Antigravity's project on this machine"),
    (r"antigravity", "Antigravity (IDE and its agent) is off limits"),
    (r"/\.gemini(?:/|$)", "Antigravity/Gemini agent state"),
    (r"/\.ssh(?:/|$)", "SSH keys"),
    (r"/\.aws(?:/|$)", "AWS credentials"),
    (r"/\.azure(?:/|$)", "Azure credentials"),
    (r"/\.gnupg(?:/|$)", "GPG keys"),
    (r"/\.kube(?:/|$)", "Kubernetes credentials"),
    (r"/\.docker/config\.json", "Docker credentials"),
    (r"\.git-credentials", "git credential store"),
    (r"/\.npmrc(?:$|[\s'\"])", "npm token file"),
    (r"/\.pypirc(?:$|[\s'\"])", "PyPI token file"),
    (r"/_?\.?netrc(?:$|[\s'\"])", "netrc credentials"),
    (r"/(?:code|cursor)/user/globalstorage", "editor credential storage"),
    (r"\bmimikatz\b", "credential dumping"),
)

WINDOWS_DENY: tuple[tuple[str, str], ...] = (
    (r"(?:^|[\s'\"=])[a-z]:/tt-[^/\s]*", "Trike Trails build/test folders (tt-* at a drive root)"),
    (r"[a-z]:/windows(?:/|$|[\s'\"])", "the Windows system folder"),
    (r"/system32/config(?:/|$)", "Windows registry hives"),
    (r"/appdata/(?:roaming|local)/microsoft/(?:credentials|protect|crypto|vault)", "Windows credential store"),
    (r"/appdata/local/(?:google/chrome|microsoft/edge|bravesoftware/[^/]+)/user data", "browser profile"),
    (r"/appdata/roaming/mozilla/firefox/profiles", "browser profile"),
    (r"\bcmdkey\b|\bvaultcmd\b|get-storedcredential|keymgr\.dll|\blsass\b", "credential store access"),
    (r"\bdiskpart\b|format-volume|\bformat\s+[a-z]:|\bbcdedit\b|vssadmin\s+delete|cipher\s+/w",
     "disk / boot destruction"),
    (r"set-mppreference|add-mppreference", "Windows Defender configuration"),
)

LINUX_DENY: tuple[tuple[str, str], ...] = (
    (_B + r"/(?:etc|boot|proc|sys)" + _E, "a Linux system folder (/etc, /boot, /proc, /sys)"),
    (r"/\.local/share/keyrings(?:/|$)", "the desktop keyring"),
    (r"/\.password-store(?:/|$)", "the pass password store"),
    (r"/\.config/(?:google-chrome|chromium|bravesoftware|microsoft-edge)(?:/|$)", "browser profile"),
    (r"/\.mozilla/firefox(?:/|$)", "browser profile"),
    (r"\bsecret-tool\b|\bgnome-keyring|\bkwallet", "credential store access"),
    (r"\bmkfs(?:\.\w+)?\b|\bwipefs\b|\bdd\b[^|;\n]*\bof=/dev/|\bfdisk\b|\bparted\b|>\s*/dev/(?:sd|nvme|mmcblk)",
     "disk destruction"),
)

MACOS_DENY: tuple[tuple[str, str], ...] = (
    (_B + r"/(?:system|private/etc|etc)" + _E, "a macOS system folder (/System, /etc)"),
    (r"/library/keychains(?:/|$)", "the macOS Keychain"),
    (r"\bsecurity\s+(?:find-[\w-]*password|dump-keychain|export)\b", "credential store access"),
    (r"/library/application support/(?:google/chrome|bravesoftware|microsoft edge)(?:/|$)", "browser profile"),
    (r"/library/application support/firefox/profiles", "browser profile"),
    (r"/library/(?:cookies|safari)(?:/|$)", "browser data"),
    (r"\bdiskutil\s+(?:erase\w*|partitiondisk|zerodisk|secureerase)\b|\bcsrutil\b|\bnvram\b",
     "disk / boot / SIP changes"),
)

# ---------------------------------------------------------------------------
# Command risk: (category, regex searched anywhere in the lower-cased command, reason)
# ---------------------------------------------------------------------------

_ADMIN = "changes system configuration or needs admin"
_SPAWN = "spawns another shell or hides what it runs"

COMMON_SHELL_RISK: tuple[tuple[str, str, str], ...] = (
    ("install", r"\b(?:pip3?|uv)\s+(?:pip\s+)?(?:install|add|uninstall)\b|-m\s+pip\b|\bnpm\s+(?:i|install|ci|add)\b"
                r"|\byarn\s+add\b|\bpnpm\s+(?:add|i|install)\b|\bcargo\s+install\b|\bgo\s+install\b"
                r"|\bconda\s+install\b", "installs software"),
    ("network", r"\bcurl(?:\.exe)?\s|\bwget\b|\bgit\s+(?:clone|fetch|pull|push|remote|submodule)\b|https?://",
     "uses the network"),
)

WINDOWS_SHELL_RISK: tuple[tuple[str, str, str], ...] = (
    ("delete", r"\bremove-item\b|\bclear-content\b|\brmdir\b|\[io\.(?:file|directory)\]::delete", "deletes files"),
    ("process_kill", r"\bstop-process\b|\btaskkill\b|\bpskill\b|wmic\s+process", "kills processes"),
    ("registry", r"\breg(?:\.exe)?\s+(?:add|delete|import|load|copy|restore|unload)\b|\bhk(?:lm|cu|cr|u|cc):"
                 r"|registry::|\b(?:set|new|remove|rename)-itemproperty\b", "touches the registry"),
    ("install", r"\bwinget\b|\bchoco\b|\bscoop\s+install\b|\binstall-(?:module|package|script)\b|\bmsiexec\b"
                r"|\.msi\b|\badd-appxpackage\b", "installs software"),
    ("network", r"\binvoke-webrequest\b|\binvoke-restmethod\b|\bstart-bitstransfer\b|net\.webclient"
                r"|downloadstring|downloadfile|\bbitsadmin\b|\btest-netconnection\b|\bsend-mailmessage\b",
     "uses the network"),
    ("elevation", r"-verb\s+runas|\brunas(?:\.exe)?\s|\bset-executionpolicy\b|\bschtasks\b|\bregister-scheduledtask\b"
                  r"|\bnew-service\b|\bsc\.exe\s|\bnetsh\b|\bnew-netfirewallrule\b|\bshutdown(?:\.exe)?\s"
                  r"|\brestart-computer\b|\bstop-computer\b|\b(?:set|start|stop|restart)-service\b"
                  r"|\bicacls\b|\btakeown\b|\benable-psremoting\b", _ADMIN),
    ("obfuscation", r"\s-enc(?:odedcommand)?\b|frombase64string|\binvoke-expression\b"
                    r"|\b(?:powershell|pwsh|cmd)(?:\.exe)?\s+(?:/c|-c|-command|/k)\b|\bstart-process\b"
                    r"|\bwsl(?:\.exe)?\b|\bbash(?:\.exe)?\s+-c\b|\badd-type\b|\bstart-job\b", _SPAWN),
)

POSIX_SHELL_RISK: tuple[tuple[str, str, str], ...] = (
    ("delete", r"\brm(?:\s|$)|\brmdir\b|\bunlink\b|\bshred\b|\bfind\b[^|;\n]*\s-delete\b|\btruncate\b", "deletes files"),
    ("process_kill", r"\bkill(?:all)?(?:\s|$)|\bpkill\b|\bxkill\b", "kills processes"),
    ("install", r"\bapt(?:-get)?\s+(?:install|remove|purge)\b|\b(?:dnf|yum|zypper)\s+(?:install|remove)\b"
                r"|\bpacman\s+-s|\bsnap\s+install\b|\bflatpak\s+install\b|\bdpkg\s+-i\b|\brpm\s+-i"
                r"|\bbrew\s+(?:install|uninstall|tap)\b|\bport\s+install\b", "installs software"),
    ("network", r"\brsync\b[^|;\n]*:|\bscp\s|\bssh\s|\bnc\s|\bncat\b|\bsocat\b|\btelnet\b", "uses the network"),
    ("elevation", r"\bsudo\b|\bdoas\b|\bpkexec\b|\bsu\s+-|\bchmod\b|\bchown\b|\bchattr\b|\bsetcap\b"
                  r"|\bsystemctl\b|\bservice\s+\S+\s+(?:start|stop|restart)|\bcrontab\b|\biptables\b|\bufw\b"
                  r"|\bmount\b|\bumount\b|\bmodprobe\b|\binsmod\b|\blaunchctl\b|\bdefaults\s+write\b"
                  r"|\bspctl\b|\bshutdown\b|\breboot\b", _ADMIN),
    ("obfuscation", r"\bbase64\s+(?:-d|--decode)\b|\beval\s|\b(?:ba|z|da|k)?sh\s+-c\b|\bpython[\d.]*\s+-c\b"
                    r"|\bperl\s+-e\b|\bxargs\b|\s-exec\s|\bnohup\b|\bsetsid\b|\bdisown\b", _SPAWN),
)

_DELETE = ("delete", "deletes files")
_KILL = ("process_kill", "kills processes")
_NET = ("network", "uses the network")
_OBF = ("obfuscation", _SPAWN)
_ELEV = ("elevation", _ADMIN)

WINDOWS_VERB_RISK: dict[str, tuple[str, str]] = {
    **{v: _DELETE for v in ("rm", "rd", "del", "erase", "ri", "clc", "rmdir")},
    **{v: _KILL for v in ("kill", "spps")},
    **{v: _NET for v in ("iwr", "irm", "curl", "curl.exe", "ssh", "scp", "sftp", "ftp", "nc", "ncat", "telnet")},
    **{v: _OBF for v in ("iex", "powershell", "powershell.exe", "pwsh", "cmd", "cmd.exe", "bash", "sh", "start",
                         "saps")},
    **{v: _ELEV for v in ("sc", "reg", "runas", "shutdown")},
}

POSIX_VERB_RISK: dict[str, tuple[str, str]] = {
    **{v: _DELETE for v in ("rm", "rmdir", "unlink", "shred")},
    **{v: _KILL for v in ("kill", "pkill", "killall")},
    **{v: _NET for v in ("curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "ncat", "telnet", "rsync")},
    **{v: _OBF for v in ("bash", "sh", "zsh", "dash", "ksh", "fish", "eval", "exec", "source", "nohup", "setsid",
                         "pwsh")},
    **{v: _ELEV for v in ("sudo", "su", "doas", "pkexec", "chmod", "chown", "systemctl", "service", "crontab",
                          "mount", "umount", "launchctl", "shutdown", "reboot")},
}

COMMON_READONLY = frozenset("ls cat echo pwd whoami hostname sort date".split())
WINDOWS_READONLY = COMMON_READONLY | frozenset("""
get-childitem gci dir gc type test-path get-content get-item gi get-itemproperty gp select-string sls
measure-object measure sort-object select-object select format-table ft format-list fl format-wide fw
where-object where ? resolve-path get-filehash get-acl split-path join-path write-output write-host
get-date out-string foreach-object % group-object group get-location get-psdrive get-volume
convertto-json convertfrom-json get-command get-help
""".split())
POSIX_READONLY = COMMON_READONLY | frozenset("""
head tail grep egrep fgrep rg find wc du df stat file less more tree uname which realpath readlink basename
dirname cut uniq nl od hexdump md5sum sha256sum shasum id printf true test [ lscpu lsblk free uptime
""".split())

COMMON_WRITE_VERBS = frozenset("cp mv mkdir tee git".split())
WINDOWS_WRITE_VERBS = COMMON_WRITE_VERBS | frozenset("sc ac cpi copy mi move ni md ren rni attrib".split())
POSIX_WRITE_VERBS = COMMON_WRITE_VERBS | frozenset("touch ln install truncate rsync dd".split())

_REDIRECT = r"(?<![<2*&])>{1,2}(?!&)"
WINDOWS_WRITE_MARKERS = (_REDIRECT + r"|\b(?:out-file|set-content|add-content|copy-item|move-item|new-item"
                         r"|rename-item|tee-object|expand-archive|compress-archive|export-csv|export-clixml|set-acl"
                         r"|robocopy|xcopy)\b")
POSIX_WRITE_MARKERS = _REDIRECT + r"|\bsed\s+(?:-\w*\s+)*-i|\bdd\b[^|;\n]*\bof=|\bunzip\b|\btar\s+-?\w*x"

# Absolute paths and environment-variable paths in a command line.
WIN_PATH_PATTERNS = (
    re.compile(r"['\"]([a-zA-Z]:[\\/][^'\"]*)['\"]"),
    re.compile(r"(?<![\w])([a-zA-Z]:[\\/][^\s'\"`;|<>,()]*)"),
    re.compile(r"(?<![\w])(\\\\[^\s'\"`;|<>]+)"),
)
POSIX_PATH_PATTERNS = (re.compile(r"(?<![\w.:/~-])(/[A-Za-z0-9_.][^\s'\"`;|<>()]*)"),)
WINDOWS_ENV_PATHS = re.compile(r"\$env:\w+|%[a-z_]+%|\$home\b|(?:^|[\s'\"])~[\\/]|\$pshome|\[environment\]::",
                               re.IGNORECASE)
POSIX_ENV_PATHS = re.compile(r"\$\{?(?:home|user|tmpdir|pwd|oldpwd|xdg_\w+)\}?|(?:^|[\s'\"=])~[\w/]?",
                             re.IGNORECASE)
BENIGN_PATHS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"})


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Platform:
    family: str
    deny_patterns: tuple[tuple[str, str], ...]
    shell_risk: tuple[tuple[str, str, str], ...]
    verb_risk: dict[str, tuple[str, str]]
    readonly_verbs: frozenset[str]
    write_verbs: frozenset[str]
    write_markers: str
    path_patterns: tuple[re.Pattern[str], ...]
    env_paths: re.Pattern[str]
    system_dir_candidates: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def is_windows(self) -> bool:
        return self.family == WINDOWS

    # -- places -------------------------------------------------------------

    def install_anchor(self) -> str:
        """The drive Tala runs from on Windows (D:\\ for D:\\src\\TalaV2, the USB stick from one); '' elsewhere."""
        return Path(__file__).resolve().anchor if self.is_windows else ""

    def suggested_root(self) -> str:
        """Offered in Settings > Workspace, never used on its own."""
        if self.is_windows and self.install_anchor():
            return str(Path(self.install_anchor()) / "TalaWorkspace")
        return str(Path.home() / "TalaWorkspace")

    def system_dirs(self) -> list[tuple[Path, str]]:
        """Folders a workspace may not be or be inside."""
        found = []
        for raw, why in self.system_dir_candidates:
            value = os.environ.get(raw[1:]) if raw.startswith("$") else raw
            if value:
                found.append((Path(value), why))
        return found

    # -- processes ----------------------------------------------------------

    def shell_argv(self, command: str) -> list[str]:
        """One interface: PowerShell on Windows, bash (or sh) elsewhere."""
        if self.is_windows:
            return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-Command", _PS_PREFIX + command]
        shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        return [shell, "-c", command]

    def low_priority_argv(self, argv: list[str]) -> list[str]:
        """Below-normal priority: Windows does it with a creation flag, POSIX with nice."""
        if self.is_windows:
            return argv
        nice = shutil.which("nice")
        return [nice, "-n", "10", *argv] if nice else argv

    def spawn_kwargs(self) -> dict:
        if self.is_windows:
            # New process group (tree kill) + BELOW_NORMAL_PRIORITY_CLASS.
            return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | 0x00004000}
        return {"start_new_session": True}

    def child_env_defaults(self, internal_dir: Path) -> dict[str, str]:
        """Cache and temp locations for child processes (only set where unset)."""
        tmp = str(internal_dir / "tmp")
        if self.is_windows:
            # On the drive Tala is installed on (D: on Kagura), never the system drive by accident.
            drive = Path(self.install_anchor())
            caches = Path(os.environ.get("TALA_CACHE_ROOT") or (drive / "caches"))
            return {"PIP_CACHE_DIR": str(caches / "pip"), "UV_CACHE_DIR": str(caches / "uv"),
                    "npm_config_cache": str(caches / "npm"), "HF_HOME": str(drive / "hf-cache"),
                    "TEMP": tmp, "TMP": tmp}
        env = {"TMPDIR": tmp}
        if os.environ.get("TALA_CACHE_ROOT"):
            caches = Path(os.environ["TALA_CACHE_ROOT"])
            env.update({"PIP_CACHE_DIR": str(caches / "pip"), "UV_CACHE_DIR": str(caches / "uv"),
                        "npm_config_cache": str(caches / "npm")})
        return env

    def venv_python(self, venv_dir: Path) -> Path:
        return venv_dir / ("Scripts/python.exe" if self.is_windows else "bin/python")


_PS_PREFIX = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; "
              "$ProgressPreference='SilentlyContinue'; ")

_WINDOWS = Platform(
    family=WINDOWS,
    deny_patterns=COMMON_DENY + WINDOWS_DENY,
    shell_risk=COMMON_SHELL_RISK + WINDOWS_SHELL_RISK,
    verb_risk=WINDOWS_VERB_RISK,
    readonly_verbs=WINDOWS_READONLY,
    write_verbs=WINDOWS_WRITE_VERBS,
    write_markers=WINDOWS_WRITE_MARKERS,
    path_patterns=WIN_PATH_PATTERNS,
    env_paths=WINDOWS_ENV_PATHS,
    system_dir_candidates=(("$SystemRoot", "the Windows system folder"), ("$windir", "the Windows system folder"),
                           ("$ProgramFiles", "Program Files"), ("$ProgramFiles(x86)", "Program Files (x86)"),
                           ("$ProgramW6432", "Program Files"), ("$ProgramData", "ProgramData")),
)

_POSIX_SYSTEM = tuple((p, "a system folder") for p in
                      ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc", "/sbin", "/sys", "/usr",
                       "/snap"))

_LINUX = Platform(
    family=LINUX,
    deny_patterns=COMMON_DENY + LINUX_DENY,
    shell_risk=COMMON_SHELL_RISK + POSIX_SHELL_RISK,
    verb_risk=POSIX_VERB_RISK,
    readonly_verbs=POSIX_READONLY,
    write_verbs=POSIX_WRITE_VERBS,
    write_markers=POSIX_WRITE_MARKERS,
    path_patterns=POSIX_PATH_PATTERNS,
    env_paths=POSIX_ENV_PATHS,
    system_dir_candidates=_POSIX_SYSTEM,
)

_MACOS = Platform(
    family=MACOS,
    deny_patterns=COMMON_DENY + MACOS_DENY,
    shell_risk=COMMON_SHELL_RISK + POSIX_SHELL_RISK,
    verb_risk=POSIX_VERB_RISK,
    readonly_verbs=POSIX_READONLY,
    write_verbs=POSIX_WRITE_VERBS,
    write_markers=POSIX_WRITE_MARKERS,
    path_patterns=POSIX_PATH_PATTERNS,
    env_paths=POSIX_ENV_PATHS,
    system_dir_candidates=_POSIX_SYSTEM + (("/System", "the macOS system folder"), ("/Library", "the system Library"),
                                           ("/Applications", "Applications")),
)

#: Every adapter by family. Tests pick one with ``ADAPTERS[LINUX]`` and monkeypatch ``current``.
ADAPTERS: dict[str, Platform] = {WINDOWS: _WINDOWS, LINUX: _LINUX, MACOS: _MACOS}


def _host_family() -> str:
    if os.name == "nt":
        return WINDOWS
    return MACOS if sys.platform == "darwin" else LINUX


def current() -> Platform:
    """The adapter for this machine. Tests monkeypatch this to exercise another OS."""
    return ADAPTERS[_host_family()]
