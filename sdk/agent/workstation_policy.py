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

from sdk.agent import workstation_platform as plat

log = logging.getLogger(__name__)

ALLOW = "allow"
APPROVE = "approve"
DENY = "deny"
_RANK = {ALLOW: 0, APPROVE: 1, DENY: 2}

#: Workstation-internal directory inside the root (audit log, jobs, venv, run
#: logs). Hidden from listings by default and never writable through the tools.
INTERNAL_DIRNAME = ".tala"


def install_anchor() -> str:
    """Root of the drive Tala is installed on (Windows); empty elsewhere. See workstation_platform."""
    return plat.current().install_anchor()


def suggested_root() -> str:
    """The folder the Settings > Workspace picker OFFERS first (platform-aware:
    <install drive>\\TalaWorkspace on Windows, ~/TalaWorkspace on Linux/macOS).
    Only a suggestion -- nothing uses it until the user saves it."""
    return plat.current().suggested_root()


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
    root: Path | None  # None = no workspace chosen yet: every tool refuses
    extra_write_roots: list[Path] = field(default_factory=list)
    extra_deny_patterns: list[str] = field(default_factory=list)
    config_path: Path | None = None
    outside_root_writes: str = APPROVE  # "approve" or "deny"
    enable_sandbox: bool = True  # Enable OS-level sandboxing
    allow_network: bool = False  # Allow network access in sandbox
    auto_delete_in_root: bool = False
    default_timeout: float = 60.0
    max_timeout: float = 1800.0
    max_output_chars: int = 12000
    max_jobs: int = 3
    python: str | None = None
    enabled: bool = True
    root_source: str = ""  # "settings" (Settings > Workspace), "env" (TALA_WORKSTATION_ROOT) or ""
    root_problem: str = ""  # why a configured root was refused, if it was
    extra_roots: list[Path] = field(default_factory=list)  # the user's extra folders (settings)

    @property
    def configured(self) -> bool:
        return self.root is not None

    @property
    def internal_dir(self) -> Path:
        return self.root / INTERNAL_DIRNAME

    @property
    def audit_path(self) -> Path:
        return self.internal_dir / "audit" / "workstation-audit.jsonl"


def read_config_file(cfg_path: Path | None = None) -> dict:
    """The saved settings (data/workstation.json); {} when missing or unreadable."""
    cfg_path = cfg_path or _default_config_path()
    try:
        if cfg_path.is_file():
            data = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
            return data if isinstance(data, dict) else {}
    except Exception as exc:  # a broken config must not widen anything
        log.warning("workstation: could not read %s (%s); using defaults", cfg_path, exc)
    return {}


def load_config() -> WorkstationConfig:
    """The workspace saved in Settings > Workspace (data/workstation.json) wins;
    TALA_WORKSTATION_ROOT is the fallback for headless installs. With neither,
    there is NO workspace -- the tools refuse instead of picking a folder."""
    cfg_path = _default_config_path()
    data = read_config_file(cfg_path)

    root_raw, source = (data.get("root"), "settings") if data.get("root") else (
        os.environ.get("TALA_WORKSTATION_ROOT"), "env")
    root, problem = None, ""
    if root_raw:
        check = validate_workspace(str(root_raw), extra_deny=data.get("extra_deny_patterns") or [])
        if check.ok:
            root = check.path
        else:
            problem = f"{root_raw}: {check.reason}"
            log.warning("workstation: configured workspace refused (%s)", problem)
    extra_roots = []
    for raw in data.get("extra_roots") or []:
        check = validate_workspace(str(raw), extra_deny=data.get("extra_deny_patterns") or [])
        if check.ok:
            extra_roots.append(check.path)
        else:
            log.warning("workstation: extra folder %s refused (%s)", raw, check.reason)
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
        root=root,
        root_source=source if root is not None else "",
        root_problem=problem,
        extra_roots=extra_roots,
        extra_write_roots=[*extra_roots, *(_resolve(p) for p in extra_write)],
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


#: Never touched by any workstation tool, approval or not: the COMMON set plus
#: the host OS's set, both in workstation_platform. Matched (re.search) against
#: _canon() of a resolved path, and against the _canon() text of commands / sources.
def _default_deny_patterns() -> tuple[tuple[str, str], ...]:
    return plat.current().deny_patterns


def _deny_patterns(cfg: WorkstationConfig) -> list[tuple[re.Pattern[str], str]]:
    pats = [(re.compile(p), why) for p, why in _default_deny_patterns()]
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


# ---------------------------------------------------------------------------
# Choosing the workspace (Settings > Workspace)
# ---------------------------------------------------------------------------

@dataclass
class WorkspaceCheck:
    ok: bool
    path: Path | None = None
    reason: str = ""
    exists: bool = False
    created: bool = False

    def as_dict(self) -> dict:
        return {"ok": self.ok, "path": str(self.path) if self.path else None, "reason": self.reason,
                "exists": self.exists, "created": self.created}


def _tala_dirs() -> list[Path]:
    """Tala's own checkout/install and her data folder: a workspace may be neither,
    nor contain either, or she could rewrite herself with her own write tool."""
    dirs = [Path(__file__).resolve().parents[2]]
    try:
        from sdk.agent.documentation_nodes import resolve_user_data_dir

        dirs.append(_resolve(resolve_user_data_dir()))
    except Exception:
        pass
    return dirs


def _system_dirs() -> list[tuple[Path, str]]:
    return [(_resolve(p), why) for p, why in plat.current().system_dirs()]


def _deny_text(text: str, extra_deny) -> str | None:
    hay = _canon(text)
    for pat, why in _default_deny_patterns():
        if re.search(pat, hay):
            return why
    for extra in extra_deny or ():
        try:
            if re.search(extra, hay, re.IGNORECASE):
                return f"config deny pattern {extra!r}"
        except re.error:
            if _canon(extra) in hay:
                return f"config deny path {extra!r}"
    return None


def validate_workspace(raw: str | None, *, create: bool = False, extra_deny=()) -> WorkspaceCheck:
    """May *raw* be Tala's workspace (or an extra folder)? Exists or can be created,
    and is not a drive root, a system folder, the user profile itself, an app-data
    or credential folder, a deny-listed place, or Tala's own install/data folder.
    ``create=True`` (saving) also creates it."""
    text = str(raw or "").strip().strip('"').strip("'").strip()
    if not text:
        return WorkspaceCheck(False, reason="No folder given. Pick or type a folder.")
    expanded = os.path.expandvars(os.path.expanduser(text))
    if not Path(expanded).is_absolute():
        return WorkspaceCheck(False, reason=f"Use a full path (for example {suggested_root()}), not a relative one.")
    path = _resolve(expanded)

    def no(reason: str) -> WorkspaceCheck:
        return WorkspaceCheck(False, path=path, reason=reason)

    if path.parent == path or (path.exists() and os.path.ismount(path)):
        return no(f"{path} is a whole drive (a drive root, mount point or the filesystem root). Pick or create a folder inside it, "
                  f"such as {Path(path) / 'TalaWorkspace'}.")
    why = _deny_text(str(path), extra_deny)
    if why:
        return no(f"{path} is off limits ({why}).")
    for sysdir, label in _system_dirs():
        if is_within(path, sysdir):
            return no(f"{path} is inside {label} ({sysdir}).")
    home = _resolve(Path.home())
    if _canon(path).rstrip("/") == _canon(home).rstrip("/"):
        return no(f"{path} is your whole user profile. Pick or create a folder inside it, such as "
                  f"{home / 'TalaWorkspace'}.")
    if is_within(home, path):
        return no(f"{path} contains your user profile ({home}).")
    if is_within(path, home):
        first = Path(os.path.relpath(path, home)).parts[0]
        if first.startswith(".") or first.lower() == "appdata":
            return no(f"{path} is inside {home / first}, which holds app settings and credentials.")
    for tala_dir in _tala_dirs():
        if is_within(path, tala_dir) or is_within(tala_dir, path):
            return no(f"{path} overlaps Tala's own install/data folder ({tala_dir}). Her workspace must be "
                      "separate so she cannot edit herself.")
    cfg_file = _resolve(_default_config_path())
    if is_within(cfg_file, path):
        return no(f"{path} contains Tala's workstation settings file ({cfg_file}).")
    if path.exists():
        if not path.is_dir():
            return no(f"{path} exists but is a file, not a folder.")
        if not os.access(path, os.W_OK):
            return no(f"{path} is not writable by Tala.")
        return WorkspaceCheck(True, path=path, exists=True)
    anchor = path.parent
    while not anchor.exists() and anchor.parent != anchor:
        anchor = anchor.parent
    if not anchor.is_dir() or not os.access(anchor, os.W_OK):
        return no(f"{path} does not exist and cannot be created ({anchor} is not a writable folder).")
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return no(f"Could not create {path}: {exc}")
        return WorkspaceCheck(True, path=path, exists=True, created=True)
    return WorkspaceCheck(True, path=path)


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

# Command-risk tables, verbs and path forms are per OS: see workstation_platform
# (COMMON_SHELL_RISK + WINDOWS_/POSIX_SHELL_RISK, *_VERB_RISK, *_READONLY ...).
# Short aliases (rm, del, ri, kill, iex ...) are matched only in command
# position, so a file called ri.txt is not a delete.

_PARENT_REF = re.compile(r"(?:^|[\s'\"\\/=])\.\.(?:[\\/]|$|[\s'\"])")
_PY_FILE = re.compile(r"\b(?:python[\d.]*|py)(?:\.exe)?\b[^|;&\n]*?([\w./\\:-]+\.py)\b", re.IGNORECASE)


def _stage_verbs(command: str) -> list[str]:
    verbs = []
    for stage in re.split(r"\|\||&&|[|;\n{}]|\$\(|`\(", command):
        words = stage.strip().lstrip("&.( ").split()
        if words:
            verbs.append(words[0].lower().strip("'\""))
    return verbs


def _referenced_paths(command: str) -> list[str]:
    found: list[str] = []
    for rx in plat.current().path_patterns:
        found += [m.group(1) for m in rx.finditer(command) if m.group(1) not in plat.BENIGN_PATHS]
    return found


def classify_shell(cfg: WorkstationConfig, command: str, cwd: Path) -> Verdict:
    v = Verdict()
    low = (command or "").lower()
    why = deny_reason(cfg, command or "")
    if why:
        v.raise_to(DENY, "deny_list", f"command references something deny-listed ({why})")
        return v
    osp = plat.current()
    verbs = _stage_verbs(command or "")
    writes = bool(re.search(osp.write_markers, low)) or any(vb in osp.write_verbs for vb in verbs)
    deletes = any(cat == "delete" and re.search(rx, low) for cat, rx, _ in osp.shell_risk) or any(
        osp.verb_risk.get(vb, ("",))[0] == "delete" for vb in verbs)
    if INTERNAL_DIRNAME in low and (writes or deletes):
        v.raise_to(DENY, "internal", f"command would modify the workstation's {INTERNAL_DIRNAME} area")
        return v
    for category, rx, reason in osp.shell_risk:
        if re.search(rx, low):
            v.raise_to(APPROVE, category, f"command {reason}")
    for vb in verbs:
        if vb in osp.verb_risk:
            category, reason = osp.verb_risk[vb]
            v.raise_to(APPROVE, category, f"command {reason} ({vb})")

    outside = []
    for p in _referenced_paths(command):
        rp = _resolve(p)
        if deny_reason(cfg, str(rp)):
            v.raise_to(DENY, "deny_list", f"{p} is deny-listed")
            return v
        if not any(is_within(rp, r) for r in writable_roots(cfg)):
            outside.append(p)
    if osp.env_paths.search(command):
        outside.append("(environment-variable path)")
    if _PARENT_REF.search(command):
        outside.append("(parent-directory '..' path)")
    if outside:
        read_only = bool(verbs) and all(vb in osp.readonly_verbs for vb in verbs) and not writes
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
