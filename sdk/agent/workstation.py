"""
workstation.py -- Tala's own computer: shell, files, Python and background jobs.

Everything here acts inside one working root, the workspace the user chose in
Settings > Workspace (``workstation.set_workspace``; ``TALA_WORKSTATION_ROOT`` is
the headless fallback). With no workspace chosen, every tool refuses with
"workspace not configured" -- there is no silent default folder. The policy -- what is allowed,
what waits for the owner, what never happens -- lives in
``sdk/agent/workstation_policy.py``; this module executes, audits and routes
approvals. The registry wrappers the model sees are in
``sdk/agent/workstation_tools.py``.

Approval routing
----------------
A risky action (delete, outside-root write, install, network, process kill,
registry, elevation) is not run. Instead a task with status ``review`` is
queued -- the same mechanism self-edit proposals use -- so it shows in Mission
Control with Approve / Reject buttons (``task.control``). The task stores a
SHA-256 digest of the exact tool + arguments. When the agent repeats the
identical call after approval, the digest matches, the approval is consumed
(single use) and the action runs. Approving is an RPC from the UI, never a
tool, so an agent cannot approve itself; a different command needs a new
approval. The emergency halt (RevocationHalt) refuses everything first.

Audit
-----
Every call -- allowed, held, denied, its outcome and duration -- is appended
as one JSON line to ``<root>/.tala/audit/workstation-audit.jsonl``. The
``.tala`` area is not writable through the workstation tools.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sdk.agent import workstation_platform as plat
from sdk.agent import workstation_policy as pol

log = logging.getLogger(__name__)

_SECRET_ENV = ("token", "secret", "auth", "password", "passwd", "key", "credential", "cookie")
_TEXT_LIMIT_BYTES = 2_000_000
_HIDDEN_DIRS = {pol.INTERNAL_DIRNAME, ".git", "node_modules", "__pycache__", ".venv", "venv"}


def config() -> pol.WorkstationConfig:
    return pol.load_config()


def _ensure_root(cfg: pol.WorkstationConfig) -> None:
    for sub in ("audit", "jobs", "runs"):
        (cfg.internal_dir / sub).mkdir(parents=True, exist_ok=True)


#: Worded for small local models, which retry a failing tool unless told plainly to stop and what to say.
NOT_CONFIGURED_USER = ("I don't have a workspace folder yet, so I can't use my computer tools. Please choose one in "
                       "Settings > Workspace, then ask me again.")
NOT_CONFIGURED = ("Workspace not configured: nothing was run, and no workstation tool will work until the user "
                  "chooses a folder in Settings > Workspace. Do not call workstation tools again in this turn. "
                  f"Reply to the user: \"{NOT_CONFIGURED_USER}\"")


def _ready() -> tuple[pol.WorkstationConfig, dict | None]:
    """The live config (re-read on every call, so a workspace change applies to the
    very next tool call) and a refusal when the tools cannot run at all."""
    cfg = config()
    if not cfg.enabled:
        return cfg, {"success": False, "error": "The workstation tools are disabled (TALA_WORKSTATION=0)."}
    if not cfg.configured:
        msg = NOT_CONFIGURED
        if cfg.root_problem:
            msg += f" (The configured folder was refused: {cfg.root_problem})"
        return cfg, {"success": False, "workspace_configured": False, "error": msg,
                     "user_message": NOT_CONFIGURED_USER}
    try:
        _ensure_root(cfg)
    except OSError as exc:
        return cfg, {"success": False, "error": f"The workspace {cfg.root} is not usable ({exc}). "
                                                "Choose another one in Settings > Workspace."}
    return cfg, None


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _clip(value: Any, limit: int = 400) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[{len(value) - limit} more chars]"
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value[:20]]
    return value


def audit(cfg: pol.WorkstationConfig, tool: str, args: dict, decision: dict, outcome: dict | None = None,
          started: float | None = None) -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "tool": tool,
        "args": _clip(args),
        "decision": decision,
    }
    if outcome is not None:
        entry["outcome"] = _clip(outcome, 300)
    if started is not None:
        entry["duration_ms"] = int((time.monotonic() - started) * 1000)
    try:
        cfg.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with cfg.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        log.error("workstation: could not write audit log %s: %s", cfg.audit_path, exc)



_SANDBOX_FALLBACK_MARK = "TALA_SANDBOX: job-object-only"
_SANDBOX_FALLBACK_MARK_LEGACY = "TALA_SANDBOX: guarded-policy-only"
_SANDBOX_DENIED_MARK = "TALA_SANDBOX: access-denied"


def _sandbox_status_from(stderr: str, status_path: Path | None) -> dict | None:
    if status_path:
        try:
            if status_path.exists():
                raw = status_path.read_text(encoding="utf-8").strip().splitlines()[0]
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("mode"):
                    return data
        except (OSError, json.JSONDecodeError, IndexError):
            pass
    text = stderr or ""
    if "No module named 'encodings'" in text:
        base = Path(getattr(sys, "base_prefix", sys.prefix))
        encodings = base / "Lib" / "encodings"
        path = str(encodings if encodings.exists() else base / "Lib")
        return {"mode": "access-denied", "path": path}
    if _SANDBOX_DENIED_MARK in text:
        path = ""
        for line in text.splitlines():
            if _SANDBOX_DENIED_MARK in line and "path=" in line:
                path = line.split("path=", 1)[-1].strip()
                break
        return {"mode": "access-denied", "path": path}
    mark = _SANDBOX_FALLBACK_MARK if _SANDBOX_FALLBACK_MARK in text else (
        _SANDBOX_FALLBACK_MARK_LEGACY if _SANDBOX_FALLBACK_MARK_LEGACY in text else ""
    )
    if mark:
        reason = text.split(mark, 1)[-1].split("\n", 1)[0].strip(" :")
        return {"mode": "job-object-only", "reason": reason or "AppContainer unavailable"}
    return None


async def _broadcast_sandbox_fallback(reason: str) -> None:
    try:
        from python.harness.server import broadcast_notification
        await broadcast_notification("workstation.sandbox_fallback", {
            "from": "appcontainer",
            "to": "job-object-only",
            "reason": reason,
        })
    except Exception:
        pass


async def _apply_sandbox_status(cfg: pol.WorkstationConfig, result: dict, stderr: str,
                                status_path: Path | None, *, tool: str) -> None:
    status = _sandbox_status_from(stderr, status_path)
    if not status:
        return
    mode = status.get("mode")
    if mode in ("job-object-only", "guarded-policy-only"):
        reason = status.get("reason") or "AppContainer unavailable"
        warning = (
            f"Windows AppContainer isolation is not active ({reason}). "
            "This command ran with a job-object-only fallback: filesystem and "
            "network are NOT OS-enforced."
        )
        result["sandbox"] = "job-object-only"
        result["sandbox_fallback"] = True
        result["warning"] = warning
        audit(cfg, "sandbox_fallback", {"tool": tool, "reason": reason},
              {"verdict": "job-object-only"}, {"warning": warning})
        await _broadcast_sandbox_fallback(warning)
    elif mode == "access-denied":
        path = status.get("path") or "unknown path"
        error = (
            f"AppContainer cannot read or execute {path}: access denied. "
            "On non-system drives the sandboxed SID needs an explicit read+execute "
            "ACL on that specific path (not the whole drive)."
        )
        result["success"] = False
        result["sandbox"] = "access-denied"
        result["error"] = error
        audit(cfg, "sandbox_access_denied", {"tool": tool, "path": path},
              {"verdict": "deny"}, {"error": error})


# ---------------------------------------------------------------------------
# Approvals (Mission Control review queue)
# ---------------------------------------------------------------------------

APPROVAL_PREFIX = "wsapprove-"
PAYLOAD_KIND = "workstation_action"


def _action_digest(tool: str, args: dict) -> str:
    blob = json.dumps({"tool": tool, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DbApprovalStore:
    """Approvals as `tasks` rows -- the queue Mission Control already renders."""

    async def latest(self, digest: str) -> tuple[str, str, dict] | None:
        from python.harness import db

        row = await db.fetchrow(
            "SELECT id, status, payload FROM tasks WHERE id LIKE $1 AND payload->>'digest' = $2 "
            "AND status IN ('review', 'ready', 'failed') ORDER BY created_at DESC LIMIT 1",
            APPROVAL_PREFIX + "%", digest,
        )
        if row is None:
            return None
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        return row["id"], row["status"], payload or {}

    async def create(self, digest: str, summary: str, payload: dict) -> str:
        from python.harness import db

        task_id = APPROVAL_PREFIX + uuid.uuid4().hex[:10]
        await db.execute(
            "INSERT INTO tasks (id, description, status, payload) VALUES ($1, $2, 'review', $3::jsonb)",
            task_id, summary[:300], json.dumps({**payload, "kind": PAYLOAD_KIND, "digest": digest}),
            caller="workstation.request_approval",
        )
        return task_id

    async def consume(self, task_id: str) -> None:
        from python.harness import db

        await db.execute("UPDATE tasks SET status = 'done', completed_at = NOW() WHERE id = $1", task_id,
                         caller="workstation.consume_approval")


_STORE: Any = DbApprovalStore()


async def _broadcast_pending(task_id: str, summary: str) -> None:
    try:
        from python.harness.server import broadcast_notification

        await broadcast_notification("workstation.approval_pending", {"task_id": task_id, "summary": summary})
    except Exception:
        pass


async def _check_approval(tool: str, args: dict, verdict: pol.Verdict) -> dict:
    """{'approved': bool, 'task_id', 'status'} -- creates the review task if none exists."""
    digest = _action_digest(tool, args)
    summary = f"Tala workstation: {tool} -- " + "; ".join(verdict.reasons)[:220]
    try:
        found = await _STORE.latest(digest)
        if found is not None:
            task_id, status, payload = found
            if status == "ready":
                await _STORE.consume(task_id)
                return {"approved": True, "task_id": task_id, "status": "approved"}
            if status == "review":
                return {"approved": False, "task_id": task_id, "status": "pending"}
            if payload.get("decision") == "rejected":
                return {"approved": False, "task_id": task_id, "status": "rejected"}
        task_id = await _STORE.create(digest, summary, {
            "tool": tool, "args": _clip(args, 2000), "reasons": verdict.reasons, "categories": verdict.categories,
        })
        await _broadcast_pending(task_id, summary)
        return {"approved": False, "task_id": task_id, "status": "pending"}
    except Exception as exc:
        # No approval store means no way to approve: fail closed.
        log.error("workstation: approval store unavailable: %s", exc)
        return {"approved": False, "task_id": None, "status": "unavailable", "error": str(exc)}


def _halted() -> bool:
    try:
        from sdk.agent.gates import RevocationHalt

        return RevocationHalt.is_halted()
    except Exception:
        return True


async def gate(cfg: pol.WorkstationConfig, tool: str, args: dict, verdict: pol.Verdict,
               digest_args: dict | None = None) -> dict | None:
    """None if the action may run now; otherwise the refusal to return to the model."""
    if not cfg.enabled:
        return {"success": False, "error": "The workstation tools are disabled (TALA_WORKSTATION=0)."}
    if _halted():
        audit(cfg, tool, args, {"verdict": "halted"})
        return {"success": False, "halted": True, "error": "Emergency halt is engaged; nothing was run."}
    if verdict.verdict == pol.DENY:
        audit(cfg, tool, args, verdict.as_dict())
        return {"success": False, "denied": True, "reasons": verdict.reasons,
                "error": "Blocked by the workstation safety policy: " + "; ".join(verdict.reasons)
                         + ". This cannot be approved; choose a different approach."}
    if verdict.verdict == pol.APPROVE:
        appr = await _check_approval(tool, digest_args if digest_args is not None else args, verdict)
        decision = {**verdict.as_dict(), "approval": appr}
        if appr["approved"]:
            args["_approval_task"] = appr["task_id"]
            return None
        audit(cfg, tool, args, decision)
        if appr["status"] == "rejected":
            msg = f"The owner rejected this exact action (approval {appr['task_id']}). Do not retry it."
        elif appr["status"] == "unavailable":
            msg = "This action needs the owner's approval, but the approval queue is unavailable, so it was refused."
        else:
            msg = (f"Held for approval: {'; '.join(verdict.reasons)}. Approval request {appr['task_id']} is "
                   "waiting in Mission Control. Tell the user what you want to do and why; once they approve it, "
                   "call this tool again with exactly the same arguments.")
        return {"success": False, "approval_required": True, "approval_task_id": appr["task_id"],
                "approval_status": appr["status"], "reasons": verdict.reasons, "error": msg}
    return None


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

def _child_env(cfg: pol.WorkstationConfig) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not any(t in k.lower() for t in _SECRET_ENV)}
    env["TALA_WORKSPACE"] = str(cfg.root)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # Cache/temp defaults come from the platform adapter. TEMP/TMP/TMPDIR always
    # point into the workspace; caches only fill in what the environment leaves unset.
    for key, value in plat.current().child_env_defaults(cfg.internal_dir).items():
        if key in ("TEMP", "TMP", "TMPDIR"):
            env[key] = value
        else:
            env.setdefault(key, value)
    (cfg.internal_dir / "tmp").mkdir(parents=True, exist_ok=True)
    return env


def _shell_argv(command: str) -> list[str]:
    """PowerShell on Windows, bash/sh elsewhere, at below-normal priority."""
    osp = plat.current()
    return osp.low_priority_argv(osp.shell_argv(command))


def _spawn_kwargs() -> dict:
    return plat.current().spawn_kwargs()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return text[:half] + f"\n...[{len(text) - limit} chars truncated]...\n" + text[-half:], True


async def _run_process(cfg: pol.WorkstationConfig, argv: list[str], cwd: Path, timeout: float,
                       *, allow_network: bool | None = None) -> dict:
    from sdk.agent.async_shell import _kill_process_tree, _register, _unregister
    from sdk.agent.sandbox import prepare_sandbox

    timeout = max(1.0, min(float(timeout or cfg.default_timeout), cfg.max_timeout))
    started = time.monotonic()
    session = prepare_sandbox(cfg, argv, cwd, allow_network=allow_network)
    argv = session.argv
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=_child_env(cfg), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **_spawn_kwargs())
    session.attach(proc)
    _register(proc)
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_process_tree(proc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            out, err = b"", b""
    finally:
        _unregister(proc)
        session.cleanup()
    stdout = (out or b"").decode("utf-8", errors="replace")
    stderr = (err or b"").decode("utf-8", errors="replace")
    result: dict[str, Any] = {"exit_code": proc.returncode, "timed_out": timed_out,
                              "duration_s": round(time.monotonic() - started, 2)}
    full_needed = False
    result["stdout"], t1 = truncate(stdout, cfg.max_output_chars)
    result["stderr"], t2 = truncate(stderr, max(2000, cfg.max_output_chars // 3))
    full_needed = t1 or t2
    if full_needed:
        log_path = cfg.internal_dir / "runs" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"$ {argv[-1]}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}", encoding="utf-8")
        result["truncated"] = True
        result["full_output_path"] = str(log_path)
    if timed_out:
        result["error"] = f"Timed out after {timeout:.0f}s and was killed. Use start_job for long-running work."
    result["success"] = (not timed_out) and proc.returncode == 0
    try:
        await _apply_sandbox_status(cfg, result, stderr, getattr(session, "status_path", None), tool="process")
    finally:
        sp = getattr(session, "status_path", None)
        if sp:
            try:
                Path(sp).unlink(missing_ok=True)
            except OSError:
                pass
    return result


def _resolve_cwd(cfg: pol.WorkstationConfig, cwd: str | None) -> tuple[Path, pol.Verdict]:
    v = pol.Verdict()
    target = pol.resolve_in_root(cfg, cwd or ".")
    why = pol.deny_reason(cfg, str(target))
    if why:
        v.raise_to(pol.DENY, "deny_list", f"working directory {target} is deny-listed ({why})")
    elif not any(pol.is_within(target, r) for r in pol.writable_roots(cfg)):
        v.raise_to(pol.APPROVE, "outside_root", f"working directory {target} is outside the workspace")
    return target, v


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

async def run_shell(command: str, cwd: str | None = None, timeout_seconds: float | None = None) -> dict:
    cfg, refusal = _ready()
    if refusal:
        return refusal
    started = time.monotonic()
    args = {"command": command, "cwd": cwd, "timeout_seconds": timeout_seconds}
    if not (command or "").strip():
        return {"success": False, "error": "command is empty"}
    workdir, v = _resolve_cwd(cfg, cwd)
    v.merge(pol.classify_shell(cfg, command, workdir))
    refusal = await gate(cfg, "run_shell", args, v, {"command": command, "cwd": str(workdir)})
    if refusal:
        return refusal
    if not workdir.is_dir():
        return {"success": False, "error": f"working directory {workdir} does not exist"}
    need_net = cfg.allow_network or "network" in v.categories or "install" in v.categories
    result = await _run_process(
        cfg, _shell_argv(command), workdir, timeout_seconds or cfg.default_timeout, allow_network=need_net,
    )
    result["cwd"] = str(workdir)
    result["shell"] = "powershell" if plat.current().is_windows else "bash"
    audit(cfg, "run_shell", args, v.as_dict(), {k: result.get(k) for k in ("exit_code", "timed_out", "success")},
          started)
    return result


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _rel(cfg: pol.WorkstationConfig, p: Path) -> str:
    return os.path.relpath(p, cfg.root) if pol.is_within(p, cfg.root) else str(p)


async def read_file(path: str, start_line: int | None = None, end_line: int | None = None,
                    max_chars: int | None = None) -> dict:
    cfg, refusal = _ready()
    if refusal:
        return refusal
    args = {"path": path, "start_line": start_line, "end_line": end_line}
    target = pol.resolve_in_root(cfg, path)
    v = pol.classify_path_access(cfg, target, "read")
    refusal = await gate(cfg, "read_file", args, v)
    if refusal:
        return refusal
    if not target.is_file():
        return {"success": False, "error": f"{target} does not exist or is not a file"}
    size = target.stat().st_size
    with target.open("rb") as fh:
        raw = fh.read(_TEXT_LIMIT_BYTES)
    if b"\x00" in raw[:8192]:
        audit(cfg, "read_file", args, v.as_dict(), {"binary": True})
        return {"success": False, "path": str(target), "size": size,
                "error": "binary file; use run_python or run_shell to inspect it"}
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start_line or end_line:
        s = max(int(start_line or 1) - 1, 0)
        e = int(end_line) if end_line else len(lines)
        text = "\n".join(lines[s:e])
    text, truncated = truncate(text, int(max_chars or cfg.max_output_chars))
    audit(cfg, "read_file", args, v.as_dict(), {"size": size})
    return {"success": True, "path": str(target), "size": size, "total_lines": len(lines),
            "content": text, "truncated": truncated or size > _TEXT_LIMIT_BYTES}


async def write_file(path: str, content: str, append: bool = False) -> dict:
    cfg, refusal = _ready()
    if refusal:
        return refusal
    content = "" if content is None else str(content)
    args = {"path": path, "append": append, "content": content}
    target = pol.resolve_in_root(cfg, path)
    v = pol.classify_path_access(cfg, target, "write")
    digest_args = {"path": str(target), "append": bool(append),
                   "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    refusal = await gate(cfg, "write_file", args, v, digest_args)
    if refusal:
        return refusal
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a" if append else "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    out = {"success": True, "path": str(target), "relative_path": _rel(cfg, target),
           "bytes_written": len(content.encode("utf-8")), "appended": bool(append)}
    audit(cfg, "write_file", args, v.as_dict(), {"bytes": out["bytes_written"]})
    return out


_MAX_WALK = 50_000


def _walk(cfg: pol.WorkstationConfig, base: Path, recursive: bool, include_hidden: bool):
    """Entries under *base*. Deny-listed folders are pruned, never entered."""
    if not recursive:
        for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if (include_hidden or child.name not in _HIDDEN_DIRS) and not pol.deny_reason(cfg, str(child)):
                yield child
        return
    seen = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if (include_hidden or d not in _HIDDEN_DIRS)
                       and not pol.deny_reason(cfg, os.path.join(dirpath, d))]
        for name in dirnames + filenames:
            seen += 1
            if seen > _MAX_WALK:
                return
            yield Path(dirpath) / name


def _list_entries(cfg, target: Path, recursive: bool, include_hidden: bool, pattern: str,
                  files_only: bool) -> list[dict]:
    entries = []
    for p in _walk(cfg, target, recursive, include_hidden):
        if not fnmatch.fnmatch(p.name.lower(), (pattern or "*").lower()):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        is_dir = p.is_dir()
        if files_only and is_dir:
            continue
        entries.append({"path": os.path.relpath(p, target), "type": "dir" if is_dir else "file",
                        "size": 0 if is_dir else st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    return entries


async def list_dir(path: str = ".", recursive: bool = False, pattern: str = "*", sort_by: str = "name",
                   limit: int = 200, files_only: bool = False, include_hidden: bool = False) -> dict:
    cfg, refusal = _ready()
    if refusal:
        return refusal
    args = {"path": path, "recursive": recursive, "pattern": pattern, "sort_by": sort_by, "limit": limit}
    target = pol.resolve_in_root(cfg, path)
    v = pol.classify_path_access(cfg, target, "read")
    refusal = await gate(cfg, "list_dir", args, v)
    if refusal:
        return refusal
    if not target.is_dir():
        return {"success": False, "error": f"{target} is not a directory"}
    entries = await asyncio.to_thread(_list_entries, cfg, target, bool(recursive), bool(include_hidden), pattern,
                                      bool(files_only))
    key = {"size": lambda e: -e["size"], "modified": lambda e: e["modified"]}.get(
        (sort_by or "name").lower(), lambda e: e["path"].lower())
    if (sort_by or "").lower() == "modified":
        entries.sort(key=key, reverse=True)
    else:
        entries.sort(key=key)
    limit = max(1, min(int(limit or 200), 1000))
    audit(cfg, "list_dir", args, v.as_dict(), {"count": len(entries)})
    return {"success": True, "path": str(target), "total": len(entries), "shown": min(limit, len(entries)),
            "entries": entries[:limit]}


def _search(cfg, target: Path, rx, query: str, pattern: str, names_only: bool, max_results: int) -> list[dict]:
    matches: list[dict] = []
    for p in _walk(cfg, target, True, False):
        if len(matches) >= max_results:
            break
        if not p.is_file() or pol.deny_reason(cfg, str(p)) or not fnmatch.fnmatch(p.name.lower(),
                                                                                 (pattern or "*").lower()):
            continue
        rel = os.path.relpath(p, target)
        if names_only or not query:
            if rx.search(p.name):
                matches.append({"path": rel})
            continue
        try:
            if p.stat().st_size > _TEXT_LIMIT_BYTES:
                continue
            data = p.read_bytes()
            if b"\x00" in data[:4096]:
                continue
            for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    matches.append({"path": rel, "line": i, "text": line.strip()[:200]})
                    if len(matches) >= max_results:
                        break
        except OSError:
            continue
    return matches


async def search_files(query: str = "", path: str = ".", pattern: str = "*", regex: bool = False,
                       names_only: bool = False, max_results: int = 100) -> dict:
    cfg, refusal = _ready()
    if refusal:
        return refusal
    args = {"query": query, "path": path, "pattern": pattern, "regex": regex, "names_only": names_only}
    target = pol.resolve_in_root(cfg, path)
    v = pol.classify_path_access(cfg, target, "read")
    refusal = await gate(cfg, "search_files", args, v)
    if refusal:
        return refusal
    try:
        rx = re.compile(query if regex else re.escape(query or ""), re.IGNORECASE)
    except re.error as exc:
        return {"success": False, "error": f"bad regex: {exc}"}
    max_results = max(1, min(int(max_results or 100), 500))
    matches = await asyncio.to_thread(_search, cfg, target, rx, query, pattern, bool(names_only), max_results)
    audit(cfg, "search_files", args, v.as_dict(), {"matches": len(matches)})
    return {"success": True, "path": str(target), "count": len(matches), "matches": matches}


async def delete_path(path: str, recursive: bool = False) -> dict:
    import shutil

    cfg, refusal = _ready()
    if refusal:
        return refusal
    args = {"path": path, "recursive": recursive}
    target = pol.resolve_in_root(cfg, path)
    v = pol.classify_path_access(cfg, target, "delete")
    refusal = await gate(cfg, "delete_path", args, v, {"path": str(target), "recursive": bool(recursive)})
    if refusal:
        return refusal
    if not target.exists():
        return {"success": False, "error": f"{target} does not exist"}
    if target.is_dir():
        if not recursive:
            return {"success": False, "error": f"{target} is a directory; pass recursive=true"}
        shutil.rmtree(target)
    else:
        target.unlink()
    audit(cfg, "delete_path", args, v.as_dict(), {"deleted": str(target)})
    return {"success": True, "deleted": str(target)}


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

def _venv_dir(cfg: pol.WorkstationConfig) -> Path:
    return cfg.internal_dir / "venv"


def _venv_python(cfg: pol.WorkstationConfig) -> Path:
    return plat.current().venv_python(_venv_dir(cfg))


async def _ensure_venv(cfg: pol.WorkstationConfig) -> dict | None:
    py = _venv_python(cfg)
    if py.is_file():
        return None
    base = cfg.python or sys.executable
    res = await _run_process(cfg, [base, "-m", "venv", str(_venv_dir(cfg))], cfg.root, 300)
    if not py.is_file():
        return {"success": False, "error": f"could not create the workstation venv: {res.get('stderr') or res}"}
    audit(cfg, "ensure_venv", {"base": base}, {"verdict": "allow"}, {"venv": str(_venv_dir(cfg))})
    return None


async def run_python(code: str | None = None, script: str | None = None, args: list | None = None,
                     cwd: str | None = None, timeout_seconds: float | None = None) -> dict:
    cfg, refusal = _ready()
    if refusal:
        return refusal
    started = time.monotonic()
    call_args = {"code": code, "script": script, "args": args, "cwd": cwd}
    if not code and not script:
        return {"success": False, "error": "pass either code or script"}
    workdir, v = _resolve_cwd(cfg, cwd)
    script_path = None
    if code and script:
        # Small models pass both meaning "save this as <script> and run it".
        # Do exactly that, through write_file so the write policy applies.
        written = await write_file(str(Path(workdir) / script) if not os.path.isabs(script) else script, code)
        if not written.get("success"):
            return written
        code = None
    if script:
        script_path = pol.resolve_in_root(cfg, Path(workdir) / script if not os.path.isabs(script) else script)
        sv = pol.classify_path_access(cfg, script_path, "read")
        if not pol.is_within(script_path, cfg.root):
            sv.raise_to(pol.APPROVE, "outside_root", f"script {script_path} is outside the workspace")
        v.merge(sv)
        if script_path.is_file() and v.verdict != pol.DENY:
            v.merge(pol.classify_python(cfg, script_path.read_text(encoding="utf-8", errors="replace")))
    else:
        v.merge(pol.classify_python(cfg, code))
    if isinstance(args, str):  # models often send '[]' or 'a b' as a string
        try:
            args = json.loads(args)
        except ValueError:
            args = args.split()
    extra = [str(a) for a in (args or [])] if isinstance(args, list) else []
    for a in extra:
        v.merge(pol.classify_python(cfg, json.dumps(a)))
    digest_args = {"code": code, "script": str(script_path) if script_path else None, "args": extra,
                   "cwd": str(workdir)}
    refusal = await gate(cfg, "run_python", call_args, v, digest_args)
    if refusal:
        return refusal
    if script_path is not None and not script_path.is_file():
        return {"success": False, "error": f"script {script_path} does not exist"}
    if (err := await _ensure_venv(cfg)) is not None:
        return err
    if script_path is None:
        script_path = cfg.internal_dir / "runs" / f"snippet-{datetime.now().strftime('%Y%m%d-%H%M%S')}-" \
                                                  f"{uuid.uuid4().hex[:6]}.py"
        script_path.write_text(code, encoding="utf-8")
    need_net = cfg.allow_network or "network" in v.categories or "install" in v.categories
    result = await _run_process(cfg, [str(_venv_python(cfg)), str(script_path), *extra], workdir,
                               timeout_seconds or cfg.default_timeout, allow_network=need_net)
    result["script"] = str(script_path)
    audit(cfg, "run_python", call_args, v.as_dict(),
          {k: result.get(k) for k in ("exit_code", "timed_out", "success")}, started)
    return result


async def install_python_package(packages: list | str) -> dict:
    """pip install into the workstation venv. Always needs approval (installs + network)."""
    cfg, refusal = _ready()
    if refusal:
        return refusal
    pkgs = packages.split() if isinstance(packages, str) else [str(p) for p in (packages or [])]
    pkgs = [p for p in pkgs if p.strip()]
    if not pkgs or any(p.startswith("-") for p in pkgs):
        return {"success": False, "error": "pass package names only (no pip options)"}
    v = pol.Verdict()
    v.raise_to(pol.APPROVE, "install", "installs Python packages from the network: " + " ".join(pkgs))
    refusal = await gate(cfg, "install_python_package", {"packages": pkgs}, v)
    if refusal:
        return refusal
    if (err := await _ensure_venv(cfg)) is not None:
        return err
    result = await _run_process(
        cfg, [str(_venv_python(cfg)), "-m", "pip", "install", *pkgs], cfg.root, 900, allow_network=True,
    )
    audit(cfg, "install_python_package", {"packages": pkgs}, v.as_dict(),
          {k: result.get(k) for k in ("exit_code", "success")})
    return result


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict[str, Any]] = {}


def _job_view(job: dict, tail_lines: int = 40) -> dict:
    proc = job.get("proc")
    rc = proc.returncode if proc is not None else job.get("exit_code")
    view = {k: job[k] for k in ("job_id", "name", "command", "cwd", "workspace", "log_path", "started_at")
            if k in job}
    view["status"] = "running" if (proc is not None and rc is None) else ("stopped" if job.get("stopped")
                                                                         else "finished")
    view["exit_code"] = rc
    try:
        lines = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace").splitlines()
        view["output_tail"] = "\n".join(lines[-max(1, min(int(tail_lines), 400)):])
        view["output_lines"] = len(lines)
    except OSError:
        view["output_tail"] = ""
    return view


async def start_job(command: str, name: str = "", cwd: str | None = None) -> dict:
    from sdk.agent.async_shell import _register

    cfg, refusal = _ready()
    if refusal:
        return refusal
    args = {"command": command, "name": name, "cwd": cwd}
    if not (command or "").strip():
        return {"success": False, "error": "command is empty"}
    running = [j for j in _JOBS.values() if j["proc"].returncode is None]
    if len(running) >= cfg.max_jobs:
        return {"success": False, "error": f"{len(running)} jobs already running (max {cfg.max_jobs}); "
                                           "stop one or wait for it to finish"}
    workdir, v = _resolve_cwd(cfg, cwd)
    v.merge(pol.classify_shell(cfg, command, workdir))
    refusal = await gate(cfg, "start_job", args, v, {"command": command, "cwd": str(workdir)})
    if refusal:
        return refusal
    job_id = "job-" + uuid.uuid4().hex[:8]
    log_path = cfg.internal_dir / "jobs" / f"{job_id}.log"
    fh = log_path.open("wb")
    from sdk.agent.sandbox import prepare_sandbox
    need_net = cfg.allow_network or "network" in v.categories or "install" in v.categories
    session = prepare_sandbox(cfg, _shell_argv(command), workdir, allow_network=need_net)
    proc = await asyncio.create_subprocess_exec(
        *session.argv, cwd=str(workdir), env=_child_env(cfg), stdin=asyncio.subprocess.DEVNULL,
        stdout=fh, stderr=asyncio.subprocess.STDOUT, **_spawn_kwargs())
    session.attach(proc)
    fh.close()
    _register(proc)
    job = {"job_id": job_id, "name": name or command[:40], "command": command, "cwd": str(workdir),
           "log_path": str(log_path), "started_at": datetime.now().isoformat(timespec="seconds"), "proc": proc,
           "workspace": str(cfg.root), "cfg": cfg, "sandbox": session}
    _JOBS[job_id] = job
    audit(cfg, "start_job", args, v.as_dict(), {"job_id": job_id, "pid": proc.pid})
    view = {"success": True, **_job_view(job, 5)}
    status_path = getattr(session, "status_path", None)
    if status_path:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not Path(status_path).exists():
            await asyncio.sleep(0.05)
        await _apply_sandbox_status(cfg, view, "", Path(status_path), tool="start_job")
        if view.get("sandbox") == "access-denied":
            view["success"] = False
    return view


async def job_status(job_id: str = "", tail_lines: int = 40) -> dict:
    if job_id:
        job = _JOBS.get(job_id)
        if not job:
            return {"success": False, "error": f"no job {job_id!r} (jobs do not survive a restart)"}
        return {"success": True, **_job_view(job, tail_lines)}
    return {"success": True, "jobs": [_job_view(j, 3) for j in _JOBS.values()]}


async def stop_job(job_id: str) -> dict:
    """Stopping one of Tala's own jobs needs no approval; arbitrary processes are run_shell + approval."""
    from sdk.agent.async_shell import _kill_process_tree, _unregister

    job = _JOBS.get(job_id)
    if not job:
        return {"success": False, "error": f"no job {job_id!r}"}
    cfg = job["cfg"]  # audited in the workspace the job was started in, even after a switch
    proc = job["proc"]
    if proc.returncode is None:
        await _kill_process_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        job["stopped"] = True
    _unregister(proc)
    session = job.get("sandbox")
    if session is not None:
        session.cleanup()
    audit(cfg, "stop_job", {"job_id": job_id}, {"verdict": "allow"}, {"exit_code": proc.returncode})
    return {"success": True, **_job_view(job, 10)}


# ---------------------------------------------------------------------------
# Choosing the workspace (Settings > Workspace, workstation.* RPCs)
# ---------------------------------------------------------------------------

def _running_jobs() -> list[dict]:
    return [_job_view(j, 0) for j in _JOBS.values() if j["proc"].returncode is None]


def workspace_status() -> dict:
    """What Settings > Workspace shows: the live workspace, where it came from, and a suggestion."""
    cfg = config()
    return {
        "configured": cfg.configured,
        "root": str(cfg.root) if cfg.root else None,
        "source": cfg.root_source,
        "problem": cfg.root_problem,
        "extra_roots": [str(p) for p in cfg.extra_roots],
        "suggested_root": pol.suggested_root(),
        "env_fallback": os.environ.get("TALA_WORKSTATION_ROOT") or None,
        "config_path": str(cfg.config_path) if cfg.config_path else None,
        "audit_path": str(cfg.audit_path) if cfg.root else None,
        "enabled": cfg.enabled,
        "running_jobs": _running_jobs(),
    }


def validate_folder(path: str) -> dict:
    """Dry-run check for the UI: nothing is created."""
    return pol.validate_workspace(path, extra_deny=pol.read_config_file().get("extra_deny_patterns") or []).as_dict()


def _write_settings(cfg_path: Path, data: dict) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_name(cfg_path.name + f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, cfg_path)


def set_workspace(root: str, extra_roots: list | None = None) -> dict:
    """Validate (creating the folder if needed), save to data/workstation.json and
    apply immediately: the next tool call re-reads the config, so the sandbox,
    audit log and venv all move with it. Running jobs keep running where they
    started; they stay visible in job_status and stoppable with stop_job."""
    before = config()
    data = pol.read_config_file(before.config_path)
    extra_deny = data.get("extra_deny_patterns") or []
    check = pol.validate_workspace(root, create=True, extra_deny=extra_deny)
    if not check.ok:
        return {"success": False, "field": "root", "error": check.reason, "check": check.as_dict()}
    extras: list[str] = []
    for raw in [str(x) for x in (extra_roots or []) if str(x or "").strip()]:
        ec = pol.validate_workspace(raw, create=True, extra_deny=extra_deny)
        if not ec.ok:
            return {"success": False, "field": "extra_roots", "error": f"Extra folder refused: {ec.reason}",
                    "check": ec.as_dict()}
        if pol.is_within(ec.path, check.path):
            continue  # already inside the workspace
        if str(ec.path) not in extras:
            extras.append(str(ec.path))
    data.update({"root": str(check.path), "extra_roots": extras,
                 "updated_at": datetime.now(UTC).isoformat(timespec="seconds")})
    try:
        _write_settings(before.config_path, data)
    except OSError as exc:
        return {"success": False, "field": "root", "error": f"Could not save the setting: {exc}"}
    after = config()
    if not after.configured or after.root != check.path:
        reason = after.root_problem or "the saved setting did not take effect"
        if after.root_source == "env":
            reason = "TALA_WORKSTATION_ROOT is in use instead of the saved setting"
        return {"success": False, "field": "root", "error": f"Saved, but not applied: {reason}"}
    running = _running_jobs()
    change = {"from": str(before.root) if before.root else None, "to": str(after.root),
              "extra_roots": extras, "created": check.created, "running_jobs_kept": [j["job_id"] for j in running]}
    _ensure_root(after)
    from sdk.agent.sandbox import refresh_workspace_isolation
    old_paths = []
    if before.configured and before.root:
        old_paths = [str(before.root), *[str(p) for p in (before.extra_roots or [])]]
    new_paths = [str(after.root), *extras]
    refresh_workspace_isolation(old_paths, new_paths)
    audit(after, "workspace_change", change, {"verdict": "owner_settings"})
    if before.configured and before.root != after.root:
        try:
            audit(before, "workspace_change", change, {"verdict": "owner_settings"})
        except Exception:
            pass
    status = workspace_status()
    note = ""
    if running and change["from"] != change["to"]:
        note = (f"{len(running)} background job(s) started in the old workspace keep running there; "
                "see job_status, stop them with stop_job.")
    return {"success": True, "created": check.created, "note": note, **status}
