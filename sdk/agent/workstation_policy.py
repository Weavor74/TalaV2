"""
workstation_policy.py -- where Tala's workstation tools may act, and what needs a human.

The workstation toolset (sdk/agent/workstation.py) gives Tala a computer of her
own: a shell, files, a Python venv and background jobs, rooted in one working
directory (D:\\TalaWorkspace on Windows by default). This module is the policy
half, kept free of I/O so it can be tested exhaustively:

* ``WorkstationConfig`` -- the root, extra writable roots, extra deny patterns.
  Read from environment variables and an optional JSON file that lives OUTSIDE
  the workspace (so Tala cannot edit her own leash with her own write tool).
* Path decisions -- ``classify_path_access`` says allow / approve / deny for a
  read, write or delete of one path.
* Command decisions -- ``classify_shell`` and ``classify_python`` scan a
  PowerShell command or a Python source for risky behaviour.

Three verdicts, in increasing severity:

``allow``    runs immediately (read-only work, or anything inside the root).
``approve``  is held until the owner approves this exact action in Mission
             Control (delete, writes outside the root, installs, network,
             process kill, registry, elevation, obfuscated commands).
``deny``     never runs, approval or not (Trike Trails / Antigravity, C:\\Windows,
             credential stores, the workstation's own audit/config files).

HONEST LIMITS: the shell and Python scanners are pattern matching, not an OS
sandbox. They make the common risky actions visible and gated, and they make
the deny-listed places unreachable through the obvious routes, but a determined
program could still construct a path at runtime that no scanner sees. The real
isolation boundary on Windows would be a separate low-privilege account or an
AppContainer; see docs/workstation.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ALLOW = "allow"
APPROVE = "approve"
DENY = "deny"
_RANK = {ALLOW: 0, APPROVE: 1, DENY: 2}

#: Workstation-internal directory inside the root (audit log, jobs, venv, run
#: logs). Hidden from listings by default and never writable through the tools.
INTERNAL_DIRNAME = ".tala"


def install_anchor() -> str:
    """Root of the drive Tala is installed on (the D drive for D:\\src\\TalaV2); empty on POSIX."""
    return Path(__file__).resolve().anchor if os.name == "nt" else ""


def _default_root() -> str:
    """TalaWorkspace on the same drive as the install on Windows (D: on Kagura;
    the USB stick when she runs from one), the home folder elsewhere."""
    if os.name == "nt" and install_anchor():
        return str(Path(install_anchor()) / "TalaWorkspace")
    return str(Path.home() / "TalaWorkspace")


def _default_config_path() -> Path:
    explicit = os.environ.get("TALA_WORKSTATION_CONFIG")
    if explicit:
        return Path(explicit)
    try:
        from sdk.agent.documentation_nodes import resolve_user_data_dir

        return Path(resolve_user_data_dir()) / "data" / "workstation.json"
    except Exception:
        return Path(__file__).resolve().parents[2] / "data" / "workstation.json"


def _split_env_list(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(";") if p.strip()]


@dataclass
class WorkstationConfig:
    root: Path
    extra_write_roots: list[Path] = field(default_factory=list)
    extra_deny_patterns: list[str] = field(default_factory=list)
    config_path: Path | None = None
    outside_root_writes: str = APPROVE  # "approve" or "deny"
    auto_delete_in_root: bool = False
    default_timeout: float = 60.0
    max_timeout: float = 1800.0
    max_output_chars: int = 12000
    max_jobs: int = 3
    python: str | None = None
    enabled: bool = True

    @property
    def internal_dir(self) -> Path:
        return self.root / INTERNAL_DIRNAME

    @property
    def audit_path(self) -> Path:
        return self.internal_dir / "audit" / "workstation-audit.jsonl"


def load_config() -> WorkstationConfig:
    """Environment first, then the JSON file (outside the root), then defaults."""
    cfg_path = _default_config_path()
    data: dict = {}
    try:
        if cfg_path.is_file():
            data = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # a broken config must not widen anything
        log.warning("workstation: could not read %s (%s); using defaults", cfg_path, exc)
        data = {}

    root = os.environ.get("TALA_WORKSTATION_ROOT") or data.get("root") or _default_root()
    extra_write = _split_env_list(os.environ.get("TALA_WORKSTATION_WRITE_ROOTS")) or list(
        data.get("extra_write_roots") or [])
    extra_deny = _split_env_list(os.environ.get("TALA_WORKSTATION_DENY")) + list(
        data.get("extra_deny_patterns") or [])
    enabled_raw = os.environ.get("TALA_WORKSTATION", str(data.get("enabled", "1")))
    outside = str(data.get("outside_root_writes", APPROVE)).lower()

    def _num(key, default, cast=float):
        try:
            return cast(data.get(key, default))
        except (TypeError, ValueError):
            return default

    return WorkstationConfig(
        root=_resolve(root),
        extra_write_roots=[_resolve(p) for p in extra_write],
        extra_deny_patterns=[str(p) for p in extra_deny],
        config_path=cfg_path,
        outside_root_writes=DENY if outside == DENY else APPROVE,
        auto_delete_in_root=bool(data.get("auto_delete_in_root", False)),
        default_timeout=_num("default_timeout", 60.0),
        max_timeout=_num("max_timeout", 1800.0),
        max_output_chars=_num("max_output_chars", 12000, int),
        max_jobs=_num("max_jobs", 3, int),
        python=os.environ.get("TALA_WORKSTATION_PYTHON") or data.get("python"),
        enabled=str(enabled_raw).strip().lower() not in ("0", "false", "off", "no"),
    )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _resolve(p: str | os.PathLike) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(p)))
    try:
        return Path(expanded).resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(expanded))


def resolve_in_root(cfg: WorkstationConfig, path: str | os.PathLike | None) -> Path:
    """Relative paths are relative to the root; absolute paths stay absolute."""
    raw = os.path.expandvars(os.path.expanduser(str(path or ".")))
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = cfg.root / candidate
    return _resolve(candidate)


def _canon(p: str | os.PathLike) -> str:
    """Lower-case, forward-slash form used for every pattern match."""
    return str(p).replace("\\", "/").lower()


def is_within(child: Path, parent: Path) -> bool:
    c, p = _canon(child).rstrip("/"), _canon(parent).rstrip("/")
    return c == p or c.startswith(p + "/")


#: Never touched by any workstation tool, approval or not. Matched (re.search)
#: against _canon() of a resolved path, and against the _canon() text of shell
#: commands / Python sources.
DEFAULT_DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"trike", "Trike Trails is Antigravity's project on this machine"),
    (r"antigravity", "Antigravity (IDE and its agent) is off limits"),
    (r"/\.gemini(?:/|$)", "Antigravity/Gemini agent state"),
    (r"(?:^|[\s'\"=])[a-z]:/tt-[^/\s]*", "Trike Trails build/test folders (D:\\tt-*)"),
    (r"[a-z]:/windows(?:/|$|[\s'\"])", "the Windows system folder"),
    (r"/system32/config(?:/|$)", "Windows registry hives"),
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
    (r"/appdata/(?:roaming|local)/microsoft/(?:credentials|protect|crypto|vault)", "Windows credential store"),
    (r"/appdata/local/(?:google/chrome|microsoft/edge|bravesoftware/[^/]+)/user data", "browser profile"),
    (r"/appdata/roaming/mozilla/firefox/profiles", "browser profile"),
    (r"/appdata/roaming/(?:code|cursor)/user/globalstorage", "editor credential storage"),
    (r"\bcmdkey\b|\bvaultcmd\b|get-storedcredential|keymgr\.dll|mimikatz|lsass", "credential store access"),
    (r"\bdiskpart\b|format-volume|\bformat\s+[a-z]:|\bbcdedit\b|vssadmin\s+delete|cipher\s+/w",
     "disk / boot destruction"),
    (r"set-mppreference|add-mppreference", "Windows Defender configuration"),
)


def _deny_patterns(cfg: WorkstationConfig) -> list[tuple[re.Pattern[str], str]]:
    pats = [(re.compile(p), why) for p, why in DEFAULT_DENY_PATTERNS]
    for extra in cfg.extra_deny_patterns:
        try:
            pats.append((re.compile(extra, re.IGNORECASE), f"config deny pattern {extra!r}"))
        except re.error:
            pats.append((re.compile(re.escape(_canon(extra))), f"config deny path {extra!r}"))
    if cfg.config_path is not None:
        pats.append((re.compile(re.escape(_canon(_resolve(cfg.config_path)))), "workstation config file"))
    return pats


def deny_reason(cfg: WorkstationConfig, text: str) -> str | None:
    """The first deny-list hit in _canon(text), or None."""
    hay = _canon(text)
    for pat, why in _deny_patterns(cfg):
        if pat.search(hay):
            return why
    return None


@dataclass
class Verdict:
    verdict: str = ALLOW
    reasons: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    def raise_to(self, level: str, category: str, reason: str) -> None:
        if _RANK[level] > _RANK[self.verdict]:
            self.verdict = level
        if category not in self.categories:
            self.categories.append(category)
        if reason not in self.reasons:
            self.reasons.append(reason)

    def merge(self, other: "Verdict") -> "Verdict":
        for cat, reason in zip(other.categories, other.reasons):
            self.raise_to(other.verdict, cat, reason)
        if _RANK[other.verdict] > _RANK[self.verdict]:
            self.verdict = other.verdict
        return self

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "reasons": self.reasons, "categories": self.categories}


def writable_roots(cfg: WorkstationConfig) -> list[Path]:
    return [cfg.root, *cfg.extra_write_roots]


def classify_path_access(cfg: WorkstationConfig, target: Path, mode: str) -> Verdict:
    """mode is 'read', 'write' or 'delete'."""
    v = Verdict()
    why = deny_reason(cfg, str(target))
    if why:
        v.raise_to(DENY, "deny_list", f"{target} is deny-listed ({why})")
        return v
    in_root = is_within(target, cfg.root)
    if mode == "read":
        if not in_root and re.search(r"(?:^|/)\.env(?:\.[\w-]+)?$", _canon(target)):
            v.raise_to(DENY, "secrets", f"{target} looks like a secrets file (.env)")
        return v

    if is_within(target, cfg.internal_dir):
        v.raise_to(DENY, "internal", f"{target} is inside the workstation's own {INTERNAL_DIRNAME} area "
                                     "(audit log, jobs, venv) and is not writable through tools")
        return v
    if mode == "delete":
        if not (in_root and cfg.auto_delete_in_root):
            v.raise_to(APPROVE, "delete", f"deleting {target} needs approval")
        if not any(is_within(target, r) for r in writable_roots(cfg)):
            v.raise_to(cfg.outside_root_writes, "outside_root", f"{target} is outside the workspace")
        if _canon(target).rstrip("/") == _canon(cfg.root).rstrip("/"):
            v.raise_to(DENY, "root", "refusing to delete the workspace root itself")
        return v
    if not any(is_within(target, r) for r in writable_roots(cfg)):
        v.raise_to(cfg.outside_root_writes, "outside_root",
                   f"writing {target} is outside the workspace {cfg.root}")
    return v


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_SHELL_RISK: tuple[tuple[str, str, str], ...] = (
    # (category, regex searched anywhere in the lower-cased command, reason).
    # Short aliases (rm, del, ri, kill, iex ...) are matched only in command
    # position -- see _VERB_RISK -- so a file called ri.txt is not a delete.
    ("delete", r"\bremove-item\b|\bclear-content\b|\brmdir\b|\[io\.(?:file|directory)\]::delete", "deletes files"),
    ("process_kill", r"\bstop-process\b|\btaskkill\b|\bpskill\b|wmic\s+process", "kills processes"),
    ("registry", r"\breg(?:\.exe)?\s+(?:add|delete|import|load|copy|restore|unload)\b|\bhk(?:lm|cu|cr|u|cc):"
                 r"|registry::|\b(?:set|new|remove|rename)-itemproperty\b", "touches the registry"),
    ("install", r"\b(?:pip3?|uv)\s+(?:pip\s+)?(?:install|add|uninstall)\b|-m\s+pip\b|\bnpm\s+(?:i|install|ci|add)\b"
                r"|\byarn\s+add\b|\bpnpm\s+(?:add|i|install)\b|\bwinget\b|\bchoco\b|\bscoop\s+install\b"
                r"|\binstall-(?:module|package|script)\b|\bmsiexec\b|\.msi\b|\badd-appxpackage\b"
                r"|\bcargo\s+install\b|\bgo\s+install\b|\bconda\s+install\b", "installs software"),
    ("network", r"\binvoke-webrequest\b|\binvoke-restmethod\b|\bcurl(?:\.exe)?\s|\bwget\b"
                r"|\bstart-bitstransfer\b|net\.webclient|downloadstring|downloadfile|\bbitsadmin\b"
                r"|\bgit\s+(?:clone|fetch|pull|push|remote|submodule)\b"
                r"|\btest-netconnection\b|\bsend-mailmessage\b|https?://", "uses the network"),
    ("elevation", r"-verb\s+runas|\brunas(?:\.exe)?\s|\bset-executionpolicy\b|\bschtasks\b|\bregister-scheduledtask\b"
                  r"|\bnew-service\b|\bsc\.exe\s|\bnetsh\b|\bnew-netfirewallrule\b|\bshutdown(?:\.exe)?\s"
                  r"|\brestart-computer\b|\bstop-computer\b|\b(?:set|start|stop|restart)-service\b"
                  r"|\bicacls\b|\btakeown\b|\benable-psremoting\b",
     "changes system configuration or needs admin"),
    ("obfuscation", r"\s-enc(?:odedcommand)?\b|frombase64string|\binvoke-expression\b"
                    r"|\b(?:powershell|pwsh|cmd)(?:\.exe)?\s+(?:/c|-c|-command|/k)\b|\bstart-process\b"
                    r"|\bwsl(?:\.exe)?\b|\bbash(?:\.exe)?\s+-c\b|\badd-type\b|\bstart-job\b",
     "spawns another shell or hides what it runs"),
)

#: Short aliases that only mean something in command position.
_VERB_RISK: dict[str, tuple[str, str]] = {
    **{v: ("delete", "deletes files") for v in ("rm", "rd", "del", "erase", "ri", "clc", "rmdir")},
    **{v: ("process_kill", "kills processes") for v in ("kill", "spps")},
    **{v: ("network", "uses the network") for v in ("iwr", "irm", "curl", "curl.exe", "ssh", "scp", "sftp",
                                                    "ftp", "nc", "ncat", "telnet")},
    **{v: ("obfuscation", "spawns another shell or hides what it runs")
       for v in ("iex", "powershell", "powershell.exe", "pwsh", "cmd", "cmd.exe", "bash", "sh", "start", "saps")},
    **{v: ("elevation", "changes system configuration or needs admin") for v in ("sc", "reg", "runas", "shutdown")},
}

_WRITE_MARKERS = re.compile(
    r"(?<![<2*&])>{1,2}(?!&)|\b(?:out-file|set-content|add-content|copy-item|move-item|new-item|rename-item"
    r"|tee-object|expand-archive|compress-archive|export-csv|export-clixml|set-acl|robocopy|xcopy)\b")
_WRITE_VERBS = frozenset("sc ac cpi copy cp mi move mv ni mkdir md ren rni tee attrib git".split())

_READONLY_VERBS = frozenset("""
get-childitem gci dir ls get-content gc type cat test-path get-item gi get-itemproperty gp select-string sls
measure-object measure sort-object sort select-object select format-table ft format-list fl format-wide fw
where-object where ? resolve-path get-filehash get-acl split-path join-path write-output echo write-host
get-date out-string foreach-object % group-object group get-location pwd get-psdrive get-volume
convertto-json convertfrom-json get-command get-help hostname whoami
""".split())

_WIN_ABS = re.compile(r"(?<![\w])([a-zA-Z]:[\\/][^\s'\"`;|<>,()]*)")
_WIN_ABS_QUOTED = re.compile(r"['\"]([a-zA-Z]:[\\/][^'\"]*)['\"]")
_UNC = re.compile(r"(?<![\w])(\\\\[^\s'\"`;|<>]+)")
_POSIX_ABS = re.compile(r"(?<![\w.:/~-])(/[A-Za-z0-9_.][^\s'\"`;|<>()]*)")
_ENV_PATHS = re.compile(r"\$env:\w+|%[a-z_]+%|\$home\b|(?:^|[\s'\"])~[\\/]|\$pshome|\[environment\]::",
                        re.IGNORECASE)
_PARENT_REF = re.compile(r"(?:^|[\s'\"\\/=])\.\.(?:[\\/]|$|[\s'\"])")
_PY_FILE = re.compile(r"\b(?:python[\d.]*|py)(?:\.exe)?\b[^|;&\n]*?([\w./\\:-]+\.py)\b", re.IGNORECASE)
_BENIGN_ABS = {"/dev/null"}


def _stage_verbs(command: str) -> list[str]:
    verbs = []
    for stage in re.split(r"\|\||&&|[|;\n{}]|\$\(|`\(", command):
        words = stage.strip().lstrip("&.( ").split()
        if words:
            verbs.append(words[0].lower().strip("'\""))
    return verbs


def _referenced_paths(command: str) -> list[str]:
    found: list[str] = []
    for rx in (_WIN_ABS_QUOTED, _WIN_ABS, _UNC):
        found += [m.group(1) for m in rx.finditer(command)]
    if os.name != "nt":
        found += [m.group(1) for m in _POSIX_ABS.finditer(command) if m.group(1) not in _BENIGN_ABS]
    return found


def classify_shell(cfg: WorkstationConfig, command: str, cwd: Path) -> Verdict:
    v = Verdict()
    low = (command or "").lower()
    why = deny_reason(cfg, command or "")
    if why:
        v.raise_to(DENY, "deny_list", f"command references something deny-listed ({why})")
        return v
    verbs = _stage_verbs(command or "")
    writes = bool(_WRITE_MARKERS.search(low)) or any(vb in _WRITE_VERBS for vb in verbs)
    deletes = bool(re.search(_SHELL_RISK[0][1], low)) or any(_VERB_RISK.get(vb, ("",))[0] == "delete" for vb in verbs)
    if INTERNAL_DIRNAME in low and (writes or deletes):
        v.raise_to(DENY, "internal", f"command would modify the workstation's {INTERNAL_DIRNAME} area")
        return v
    for category, rx, reason in _SHELL_RISK:
        if re.search(rx, low):
            v.raise_to(APPROVE, category, f"command {reason}")
    for vb in verbs:
        if vb in _VERB_RISK:
            category, reason = _VERB_RISK[vb]
            v.raise_to(APPROVE, category, f"command {reason} ({vb})")

    outside = []
    for p in _referenced_paths(command):
        rp = _resolve(p)
        if deny_reason(cfg, str(rp)):
            v.raise_to(DENY, "deny_list", f"{p} is deny-listed")
            return v
        if not any(is_within(rp, r) for r in writable_roots(cfg)):
            outside.append(p)
    if _ENV_PATHS.search(command):
        outside.append("(environment-variable path)")
    if _PARENT_REF.search(command):
        outside.append("(parent-directory '..' path)")
    if outside:
        read_only = bool(verbs) and all(vb in _READONLY_VERBS for vb in verbs) and not writes
        if not read_only:
            v.raise_to(APPROVE, "outside_root",
                       "command touches paths outside the workspace: " + ", ".join(outside[:5]))

    for m in _PY_FILE.finditer(command):
        script = resolve_in_root(cfg, Path(cwd) / m.group(1)) if not os.path.isabs(m.group(1)) else _resolve(m.group(1))
        if is_within(script, cfg.root) and script.is_file():
            try:
                v.merge(classify_python(cfg, script.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return v


_PY_RISK: tuple[tuple[str, str, str], ...] = (
    ("delete", r"\b(?:os\.(?:remove|unlink|rmdir|removedirs)|shutil\.(?:rmtree|move)|send2trash)\b"
               r"|\.unlink\(|\.rmdir\(", "deletes files"),
    ("process", r"\b(?:subprocess|os\.system|os\.popen|os\.exec\w*|os\.spawn\w*|pty|multiprocessing)\b",
     "starts other processes"),
    ("process_kill", r"\bos\.kill\b|\bpsutil\b|\bsignal\.", "can kill processes"),
    ("network", r"\b(?:socket|urllib|requests|httpx|aiohttp|http\.client|ftplib|smtplib|websockets?|paramiko)\b",
     "uses the network"),
    ("registry", r"\b_?winreg\b", "touches the registry"),
    ("install", r"\bpip\b|\bensurepip\b", "installs packages"),
    ("native", r"\bctypes\b|\bcffi\b", "calls native code"),
    ("obfuscation", r"\bexec\s*\(|\beval\s*\(|__import__|\bimportlib\b|(?<!\.)\bcompile\s*\(|\bmarshal\b|\bpickle\.loads",
     "executes dynamically built code"),
)


def classify_python(cfg: WorkstationConfig, source: str) -> Verdict:
    v = Verdict()
    why = deny_reason(cfg, source or "")
    if why:
        v.raise_to(DENY, "deny_list", f"code references something deny-listed ({why})")
        return v
    for category, rx, reason in _PY_RISK:
        if re.search(rx, source or ""):
            v.raise_to(APPROVE, category, f"code {reason}")
    outside = []
    for p in _referenced_paths(source or ""):
        rp = _resolve(p)
        if deny_reason(cfg, str(rp)):
            v.raise_to(DENY, "deny_list", f"{p} is deny-listed")
            return v
        if not is_within(rp, cfg.root):
            outside.append(p)
    if re.search(r"['\"](?:\.\.[\\/]|\.\.['\"])", source or "") or "expanduser" in (source or ""):
        outside.append("(relative '..' / home / environment path)")
    if outside:
        v.raise_to(APPROVE, "outside_root", "code references paths outside the workspace: " + ", ".join(outside[:5]))
    if INTERNAL_DIRNAME in (source or "") and "open(" in (source or ""):
        v.raise_to(APPROVE, "internal", f"code mentions the workstation's {INTERNAL_DIRNAME} area")
    return v
