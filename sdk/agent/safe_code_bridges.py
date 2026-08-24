"""
safe_code_bridges.py - Auto-generated documentation base.
"""

from __future__ import annotations

import asyncio
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_VALIDATION_PREFIXES = (
    ("python", "-m", "py_compile"),
    ("python", "-m", "pytest"),
    ("uv", "run", "pytest"),
    ("pytest",),
    # JavaScript had no syntax check at all, so a broken renderer/*.js write
    # reported success and only failed at the next window load (UI-121).
    ("node", "--check"),
)
ALLOWED_WRITE_SUFFIXES = {
    ".py",
    ".js",
    ".json",
    ".html",
    ".css",
    ".md",
    ".txt",
    ".yml",
    ".yaml",
}
BLOCKED_WRITE_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "data",
    "vault",
}
MAX_WRITTEN_FILE_BYTES = 250_000


@dataclass(frozen=True)
class ReplacementPatch:
    file_path: str
    old_text: str
    new_text: str


@dataclass(frozen=True)
class FileWrite:
    file_path: str
    content: str
    overwrite: bool = False
    create_dirs: bool = False


def workspace_root() -> Path:
    return WORKSPACE_ROOT


def resolve_workspace_path(file_path: str) -> Path:
    if not file_path or not isinstance(file_path, str):
        raise ValueError("file_path is required")
    raw = Path(file_path)
    if raw.is_absolute():
        raise ValueError("absolute paths are not allowed")
    resolved = (WORKSPACE_ROOT / raw).resolve()
    try:
        resolved.relative_to(WORKSPACE_ROOT)
    except ValueError as exc:
        raise ValueError("path escapes workspace")
    return resolved


def _ensure_writable_workspace_path(file_path: str) -> Path:
    path = resolve_workspace_path(file_path)
    relative_parts = path.relative_to(WORKSPACE_ROOT).parts
    lowered_parts = {part.lower() for part in relative_parts}
    blocked = sorted(lowered_parts & BLOCKED_WRITE_PARTS)
    if blocked:
        raise ValueError(f"path contains blocked workspace segment: {blocked[0]}")
    if path.suffix.lower() not in ALLOWED_WRITE_SUFFIXES:
        raise ValueError(f"file extension is not allowed: {path.suffix or '<none>'}")
    return path


def _normalize_replacements(replacements: Any) -> list[ReplacementPatch]:
    if not isinstance(replacements, list) or not replacements:
        raise ValueError("replacements must be a non-empty list")

    normalized: list[ReplacementPatch] = []
    for item in replacements:
        if not isinstance(item, dict):
            raise ValueError("each replacement must be an object")
        file_path = str(item.get("file_path", "")).strip()
        old_text = item.get("old_text")
        new_text = item.get("new_text")
        if not file_path:
            raise ValueError("replacement.file_path is required")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError(f"{file_path}: old_text must be a non-empty string")
        if not isinstance(new_text, str):
            raise ValueError(f"{file_path}: new_text must be a string")
        normalized.append(ReplacementPatch(file_path, old_text, new_text))
    return normalized


def _normalize_file_writes(file_writes: Any) -> list[FileWrite]:
    if file_writes in (None, []):
        return []
    if not isinstance(file_writes, list):
        raise ValueError("file_writes must be a list")

    normalized: list[FileWrite] = []
    for item in file_writes:
        if not isinstance(item, dict):
            raise ValueError("each file_write must be an object")
        file_path = str(item.get("file_path", "")).strip()
        content = item.get("content")
        if not file_path:
            raise ValueError("file_write.file_path is required")
        if not isinstance(content, str):
            raise ValueError(f"{file_path}: content must be a string")
        if "\x00" in content:
            raise ValueError(f"{file_path}: content cannot contain NUL bytes")
        if len(content.encode("utf-8")) > MAX_WRITTEN_FILE_BYTES:
            raise ValueError(f"{file_path}: content exceeds {MAX_WRITTEN_FILE_BYTES} bytes")
        normalized.append(
            FileWrite(
                file_path=file_path,
                content=content,
                overwrite=bool(item.get("overwrite", False)),
                create_dirs=bool(item.get("create_dirs", False)),
            )
        )
    return normalized


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def propose_patch(
    replacements: Any,
    rationale: str = "",
    validation_commands: Any = None,
) -> dict[str, Any]:
    patch_set = _normalize_replacements(replacements)
    commands = validation_commands if isinstance(validation_commands, list) else []
    files = []

    for patch in patch_set:
        path = resolve_workspace_path(patch.file_path)
        exists = path.exists()
        current = path.read_text(encoding="utf-8") if exists else ""
        occurrences = current.count(patch.old_text) if exists else 0
        files.append(
            {
                "file_path": patch.file_path,
                "exists": exists,
                "old_text_occurrences": occurrences,
                "will_change": occurrences == 1,
                "bytes_old": len(patch.old_text.encode("utf-8")),
                "bytes_new": len(patch.new_text.encode("utf-8")),
            }
        )

    return {
        "success": True,
        "rationale": rationale,
        "files": files,
        "replacements": [
            {
                "file_path": patch.file_path,
                "old_text": patch.old_text,
                "new_text": patch.new_text,
            }
            for patch in patch_set
        ],
        "validation_commands": commands,
        "requires_apply": True,
    }


def propose_file_write(
    file_writes: Any,
    rationale: str = "",
    validation_commands: Any = None,
) -> dict[str, Any]:
    write_set = _normalize_file_writes(file_writes)
    if not write_set:
        raise ValueError("file_writes must be a non-empty list")

    commands = validation_commands if isinstance(validation_commands, list) else []
    files = []
    for write in write_set:
        path = _ensure_writable_workspace_path(write.file_path)
        exists = path.exists()
        parent_exists = path.parent.exists()
        files.append(
            {
                "file_path": write.file_path,
                "exists": exists,
                "parent_exists": parent_exists,
                "will_write": (not exists or write.overwrite) and (parent_exists or write.create_dirs),
                "overwrite": write.overwrite,
                "create_dirs": write.create_dirs,
                "bytes_new": len(write.content.encode("utf-8")),
            }
        )

    return {
        "success": True,
        "rationale": rationale,
        "files": files,
        "file_writes": [
            {
                "file_path": write.file_path,
                "content": write.content,
                "overwrite": write.overwrite,
                "create_dirs": write.create_dirs,
            }
            for write in write_set
        ],
        "validation_commands": commands,
        "requires_apply": True,
    }


def _syntax_error(path: Path, content: str) -> str | None:
    """
    Return a human-readable syntax error for *content*, or None if it parses.

    Runs **before** anything is written, so a file that would not parse never
    reaches disk. Previously only Python was validated, and that only after the
    fact — a broken .js write reported success and surfaced at the next window
    load, by which point the UI was dead (UI-121).

    An unavailable checker is not a failure: if node is not installed we say so
    and allow the write, rather than blocking every JavaScript edit on a
    toolchain that may not be present.
    """
    suffix = path.suffix.lower()

    if suffix == ".py":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return f"Python syntax error at line {exc.lineno}: {exc.msg}"
        return None

    if suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            return f"JSON syntax error at line {exc.lineno}: {exc.msg}"
        return None

    if suffix == ".js":
        if shutil.which("node") is None:
            return None
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                ["node", "--check", tmp_path],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                first = next((ln for ln in detail if "SyntaxError" in ln), detail[0] if detail else "")
                return f"JavaScript syntax error: {first.strip()}"
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"could not run node --check: {exc}"
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return None

    return None


def write_files(
    file_writes: Any,
    expected_files: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    write_set = _normalize_file_writes(file_writes)
    allowed_files = set(str(path) for path in expected_files) if isinstance(expected_files, list) else None
    changed: list[dict[str, Any]] = []
    errors: list[str] = []

    prepared: list[tuple[FileWrite, Path, bool]] = []
    for write in write_set:
        if allowed_files is not None and write.file_path not in allowed_files:
            errors.append(f"{write.file_path}: not listed in expected_files")
            continue
        try:
            path = _ensure_writable_workspace_path(write.file_path)
            exists = path.exists()
            if exists and not write.overwrite:
                errors.append(f"{write.file_path}: file already exists and overwrite is false")
                continue
            if not path.parent.exists() and not write.create_dirs:
                errors.append(f"{write.file_path}: parent directory does not exist")
                continue
            syntax_problem = _syntax_error(path, write.content)
            if syntax_problem:
                errors.append(f"{write.file_path}: {syntax_problem}")
                continue
            prepared.append((write, path, exists))
        except Exception as exc:
            errors.append(f"{write.file_path}: {exc}")

    if errors:
        return {
            "success": False,
            "dry_run": dry_run,
            "changed": [],
            "errors": errors,
        }

    for write, path, existed in prepared:
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(write.content, encoding="utf-8")
        changed.append(
            {
                "file_path": write.file_path,
                "created": not existed,
                "overwritten": existed,
                "bytes_after": len(write.content.encode("utf-8")),
            }
        )

    return {
        "success": True,
        "dry_run": dry_run,
        "changed": changed,
        "errors": [],
    }



def write_and_verify(
    file_writes: Any,
    validation_commands: Any = None,
    expected_files: Any = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """
    Write files, run validation, and revert **only what was written** if it fails.

    Deliberately narrow. This project has lost 427 tracked files to an automated
    rollback that restored a directory tree, and ``RollbackRecovery``'s own
    docstring records a failed snapshot later being "restored" with a reassuring
    success message. So this function:

    * never restores a tree, never calls rmtree, never touches a file it did not
      itself write;
    * captures the exact prior bytes of every target *before* writing, and puts
      those bytes back on failure — a file that did not exist is unlinked;
    * verifies the revert afterwards and reports any file it could **not** put
      back, rather than reporting a clean rollback it did not achieve.

    Syntax errors are caught earlier still, by ``write_files``, so a file that
    would not parse never reaches disk in the first place.
    """
    write_set = _normalize_file_writes(file_writes)
    commands = _normalize_string_list(validation_commands)

    # Snapshot prior state, per file, in memory. Bytes, not a tree.
    prior: list[tuple[Path, str, bytes | None]] = []
    for write in write_set:
        try:
            path = _ensure_writable_workspace_path(write.file_path)
        except Exception as exc:
            return {"success": False, "stage": "resolve",
                    "errors": [f"{write.file_path}: {exc}"], "reverted": []}
        before = path.read_bytes() if path.exists() else None
        prior.append((path, write.file_path, before))

    # Pass the caller original payload: write_files normalises again, and a
    # FileWrite dataclass is not what _normalize_file_writes accepts.
    written = write_files(file_writes, expected_files=expected_files)
    if not written.get("success"):
        return {"success": False, "stage": "write", "errors": written.get("errors", []),
                "reverted": [], "detail": "nothing was written, so nothing needed reverting"}

    if not commands:
        return {"success": True, "stage": "write", "changed": written["changed"],
                "verified": False,
                "detail": "no validation_commands given, so the write was not verified"}

    validation = run_validation_sync(commands, timeout=timeout)
    if validation.get("success"):
        return {"success": True, "stage": "verify", "changed": written["changed"],
                "verified": True, "validation": validation}

    # Failed verification: put back exactly what we replaced.
    reverted: list[str] = []
    revert_failures: list[str] = []
    for path, rel, before in prior:
        try:
            if before is None:
                if path.exists():
                    path.unlink()
                reverted.append(f"{rel} (removed; it did not exist before)")
            else:
                path.write_bytes(before)
                if path.read_bytes() != before:
                    revert_failures.append(f"{rel}: content after revert does not match the original")
                else:
                    reverted.append(rel)
        except Exception as exc:
            revert_failures.append(f"{rel}: {exc}")

    result = {
        "success": False,
        "stage": "verify",
        "verified": False,
        "validation": validation,
        "reverted": reverted,
        "detail": "validation failed; the written files were restored to their prior contents",
    }
    if revert_failures:
        result["revert_failures"] = revert_failures
        result["detail"] = (
            "validation failed AND the revert was incomplete — see revert_failures. "
            "These files are left in a modified state and need manual attention."
        )
    return result


def _read_default_noop_replacement(file_path: str) -> dict[str, str]:
    path = resolve_workspace_path(file_path)
    if not path.exists():
        raise ValueError(f"{file_path}: file does not exist")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            return {
                "file_path": file_path,
                "old_text": stripped,
                "new_text": stripped,
            }
    raise ValueError(f"{file_path}: no non-empty text found for no-op replacement")


def create_self_improvement_plan(
    goal: str,
    target_files: Any = None,
    replacements: Any = None,
    file_writes: Any = None,
    validation_commands: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    goal_text = str(goal or "").strip()
    if not goal_text:
        raise ValueError("goal is required")

    lowered = goal_text.lower()
    if any(word in lowered for word in ("refresh", "reload", "ui", "node grid")):
        classified_gap = "ui-refresh"
    elif any(word in lowered for word in ("validate", "test", "compile", "pytest")):
        classified_gap = "validation"
    elif any(word in lowered for word in ("document", "documentation", "node", "html-nn")):
        classified_gap = "documentation"
    elif any(word in lowered for word in ("patch", "code", "implement", "fix")):
        classified_gap = "bridge-implementation"
    else:
        classified_gap = "general-self-improvement"

    target_file_list = _normalize_string_list(target_files) or ["README.md"]
    if replacements:
        replacement_list = _normalize_replacements(replacements)
        replacement_dicts = [
            {
                "file_path": patch.file_path,
                "old_text": patch.old_text,
                "new_text": patch.new_text,
            }
            for patch in replacement_list
        ]
    elif file_writes:
        replacement_dicts = []
    else:
        replacement_dicts = [_read_default_noop_replacement(target_file_list[0])]

    write_list = _normalize_file_writes(file_writes)
    file_write_dicts = [
        {
            "file_path": write.file_path,
            "content": write.content,
            "overwrite": write.overwrite,
            "create_dirs": write.create_dirs,
        }
        for write in write_list
    ]

    commands = validation_commands if isinstance(validation_commands, list) else [
        ["python", "-m", "py_compile", "sdk/agent/safe_code_bridges.py"]
    ]
    expected_files = sorted(
        {item["file_path"] for item in replacement_dicts}
        | {item["file_path"] for item in file_write_dicts}
    )

    plan = {
        "classified_gap": classified_gap,
        "rationale": goal_text,
        "expected_files": expected_files,
        "replacements": replacement_dicts,
        "file_writes": file_write_dicts,
        "validation_commands": commands,
        "dry_run": bool(dry_run),
    }

    return {
        "success": True,
        "plan": plan,
        "requires_review": not bool(dry_run),
        "next_step": "Call self_improvement.execute_plan with this plan.",
    }


def apply_patch(
    replacements: Any,
    expected_files: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    patch_set = _normalize_replacements(replacements)
    allowed_files = set(str(path) for path in expected_files) if isinstance(expected_files, list) else None
    changed: list[dict[str, Any]] = []
    errors: list[str] = []

    prepared: list[tuple[ReplacementPatch, Path, str, str]] = []
    for patch in patch_set:
        if allowed_files is not None and patch.file_path not in allowed_files:
            errors.append(f"{patch.file_path}: not listed in expected_files")
            continue
        try:
            path = resolve_workspace_path(patch.file_path)
            if not path.exists():
                errors.append(f"{patch.file_path}: file does not exist")
                continue
            current = path.read_text(encoding="utf-8")
            count = current.count(patch.old_text)
            if count != 1:
                errors.append(f"{patch.file_path}: old_text matched {count} times; expected exactly 1")
                continue
            updated = current.replace(patch.old_text, patch.new_text, 1)
            prepared.append((patch, path, current, updated))
        except Exception as exc:
            errors.append(f"{patch.file_path}: {exc}")

    if errors:
        return {
            "success": False,
            "dry_run": dry_run,
            "changed": [],
            "errors": errors,
        }

    for patch, path, current, updated in prepared:
        if not dry_run:
            path.write_text(updated, encoding="utf-8")
        changed.append(
            {
                "file_path": patch.file_path,
                "bytes_before": len(current.encode("utf-8")),
                "bytes_after": len(updated.encode("utf-8")),
            }
        )

    return {
        "success": True,
        "dry_run": dry_run,
        "changed": changed,
        "errors": [],
    }


def _split_command(command: Any) -> list[str]:
    if isinstance(command, list):
        parts = [str(part) for part in command]
    elif isinstance(command, str):
        import shlex

        parts = shlex.split(command, posix=os.name != "nt")
    else:
        raise ValueError("command must be a string or list")
    if not parts:
        raise ValueError("command cannot be empty")
    return parts


def _is_allowed_validation(parts: list[str]) -> bool:
    lowered = tuple(part.lower() for part in parts)
    return any(lowered[: len(prefix)] == prefix for prefix in ALLOWED_VALIDATION_PREFIXES)



def _resolve_interpreter(parts: list[str]) -> list[str]:
    """
    Point a bare ``python`` at the interpreter the harness is actually running.

    ``python`` on PATH is not necessarily the project venv. On a machine where
    it is not, ``python -m pytest`` fails with "No module named pytest" — a
    failure about the environment that reads exactly like a failure about the
    code. Anything that reverts on failed verification would then revert every
    correct write.

    Only the leading ``python`` token is rewritten; the command still has to
    pass the allowlist first.
    """
    if parts and parts[0] == "python":
        return [sys.executable, *parts[1:]]
    return parts


async def run_validation(commands: Any, timeout: float = 60.0) -> dict[str, Any]:
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands must be a non-empty list")

    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for command in commands:
        parts = _split_command(command)
        if not _is_allowed_validation(parts):
            errors.append(f"command not allowed: {' '.join(parts)}")
            continue
        proc = await asyncio.create_subprocess_exec(
            *_resolve_interpreter(parts),
            cwd=str(WORKSPACE_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            timed_out = True
        results.append(
            {
                "command": parts,
                "exit_code": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "timed_out": timed_out,
                "success": proc.returncode == 0 and not timed_out,
            }
        )

    return {
        "success": not errors and all(result["success"] for result in results),
        "errors": errors,
        "results": results,
    }


async def execute_self_improvement_plan(plan: Any, dry_run: bool = False) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")

    result: dict[str, Any] = {
        "classified_gap": plan.get("classified_gap"),
        "dry_run": dry_run,
        "proposal": None,
        "file_write_proposal": None,
        "file_write": None,
        "apply": None,
        "validation": None,
        "success": False,
        "errors": [],
    }

    replacements = plan.get("replacements", [])
    file_writes = plan.get("file_writes", [])
    validation_commands = plan.get("validation_commands", [])
    if not replacements and not file_writes:
        raise ValueError("plan must include replacements or file_writes")

    if file_writes:
        try:
            result["file_write_proposal"] = propose_file_write(
                file_writes,
                rationale=str(plan.get("rationale", "")),
                validation_commands=validation_commands,
            )
            file_write_result = write_files(
                file_writes,
                expected_files=plan.get("expected_files"),
                dry_run=dry_run,
            )
            result["file_write"] = file_write_result
            if not file_write_result.get("success"):
                result["errors"].extend(file_write_result.get("errors", []))
                return result
        except Exception as exc:
            result["errors"].append(str(exc))
            return result

    if replacements:
        result["proposal"] = propose_patch(
            replacements,
            rationale=str(plan.get("rationale", "")),
            validation_commands=validation_commands,
        )
        apply_result = apply_patch(
            replacements,
            expected_files=plan.get("expected_files"),
            dry_run=dry_run,
        )
        result["apply"] = apply_result
        if not apply_result.get("success"):
            result["errors"].extend(apply_result.get("errors", []))
            return result

    if validation_commands and not dry_run:
        validation_result = await run_validation(validation_commands)
        result["validation"] = validation_result
        if not validation_result.get("success"):
            result["errors"].extend(validation_result.get("errors", []))
            for item in validation_result.get("results", []):
                if not item.get("success"):
                    result["errors"].append(f"validation failed: {' '.join(item.get('command', []))}")

    result["success"] = not result["errors"]
    return result


def _documentation_targets_for_goal(goal: str) -> list[str]:
    lowered = goal.lower()
    targets: list[str] = []
    if any(
        term in lowered
        for term in (
            "entire codebase",
            "whole codebase",
            "full codebase",
            "codebase",
            "project documentation",
            "code documentation",
            "document tala herself",
            "document herself",
            "document itself",
        )
    ):
        return ["codebase"]
    if any(term in lowered for term in ("chat", "conversation", "slash command", "tool call")):
        targets.append("chat.panel")
    if any(term in lowered for term in ("workflow", "builder", "executor")):
        targets.append("workflow")
    if any(term in lowered for term in ("bridge", "tool contract", "self-improvement bridge", "patch", "validation", "refresh")):
        targets.append("bridges")
    if "inference" in lowered or "provider" in lowered or "model" in lowered:
        targets.append("settings.inference")
    if any(term in lowered for term in ("self", "herself", "itself", "tala")):
        targets.extend(["chat.panel", "workflow", "bridges"])
    return sorted(set(targets)) or ["chat.panel"]


async def execute_self_improvement_goal(
    goal: str,
    dry_run: bool = False,
    targets: Any = None,
    target_files: Any = None,
    replacements: Any = None,
    file_writes: Any = None,
    validation_commands: Any = None,
) -> dict[str, Any]:
    goal_text = str(goal or "").strip()
    if not goal_text:
        raise ValueError("goal is required")

    plan_result = create_self_improvement_plan(
        goal=goal_text,
        target_files=target_files or ["README.md"],
        replacements=replacements,
        file_writes=file_writes,
        validation_commands=validation_commands,
        dry_run=dry_run,
    )
    classified_gap = plan_result["plan"]["classified_gap"]
    plan_is_noop = not replacements and not file_writes

    result: dict[str, Any] = {
        "success": False,
        "goal": goal_text,
        "classified_gap": classified_gap,
        "selected_bridge": None,
        "targets": [],
        "plan_result": plan_result,
        "execution_result": None,
        "refresh_result": None,
        "errors": [],
    }

    if classified_gap == "documentation":
        from sdk.agent.documentation_nodes import generate_and_verify_program_nodes

        selected_targets = _normalize_string_list(targets) or _documentation_targets_for_goal(goal_text)
        result["selected_bridge"] = "docs.generate_and_verify_program_nodes"
        result["targets"] = selected_targets
        if dry_run:
            result["execution_result"] = {
                "dry_run": True,
                "would_call": "docs.generate_and_verify_program_nodes",
                "targets": selected_targets,
            }
            result["success"] = True
            return result

        docs_result = generate_and_verify_program_nodes(selected_targets)
        result["execution_result"] = docs_result
        result["errors"].extend(docs_result.get("errors", []))
        result["refresh_result"] = {
            "success": True,
            "refresh_required": True,
            "target": "node-grid",
        }
        result["success"] = not result["errors"]
        return result

    execution_result = await execute_self_improvement_plan(
        plan=plan_result["plan"],
        dry_run=dry_run,
    )
    result["selected_bridge"] = "self_improvement.execute_plan"
    result["execution_result"] = execution_result
    result["no_op"] = plan_is_noop
    result["errors"].extend(execution_result.get("errors", []))
    result["success"] = not result["errors"] and bool(execution_result.get("success"))
    return result


def to_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def run_validation_sync(commands: Any, timeout: float = 120.0) -> dict[str, Any]:
    """Run the async validation helper from sync code."""
    import asyncio as _asyncio
    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        return _asyncio.run(run_validation(commands=commands, timeout=timeout))
    raise RuntimeError(
        "run_validation_sync cannot be called from inside a running event loop; "
        "await run_validation() directly instead."
    )

