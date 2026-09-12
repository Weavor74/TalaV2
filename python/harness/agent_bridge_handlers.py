"""
Agent-bridge RPC handlers, extracted verbatim from server.py (P2 review item 18).

The ``repo.*``, ``build.*``, ``code.*``, ``test.*``, ``shell.run`` and
``task.report`` methods: thin governed wrappers over the sdk.agent bridges
(scanner, linter, recovery, tester, coder, safe_code_bridges, async_shell).
This is the first domain moved out of the 18k-line server.py; the pattern for
the rest is the same — handlers import their response plumbing from
python.harness.jsonrpc, never from server.py, and server.py re-exports the
handler names so its dispatch map and existing tests keep working. The size
ratchet (tests/test_size_ratchet.py) holds the line behind each move.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)

#: Repo root, identical to server.py's _ROOT_DIR (both files live in
#: python/harness/, so the same expression resolves to the checkout root).
_ROOT_DIR = str(Path(__file__).resolve().parent.parent.parent)


async def handle_repo_scan(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.scan`` — Scan repository code structure.
    """
    try:
        from sdk.agent.scanner import RepositoryScanner
        scanner = RepositoryScanner(_ROOT_DIR)
        code_map = scanner.build_codebase_map()
        return _ok(req_id, {"codebase_map": code_map})
    except Exception as exc:
        logger.exception("repo.scan error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_repo_lint(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.lint`` — Lint repository for architectural violations.
    """
    try:
        from sdk.agent.linter import ArchitectureLinter
        linter = ArchitectureLinter(_ROOT_DIR)
        errors = linter.lint_all()
        return _ok(req_id, {"success": len(errors) == 0, "errors": errors})
    except Exception as exc:
        logger.exception("repo.lint error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_repo_checkpoint(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.checkpoint`` — Create repository state checkpoint.
    """
    try:
        from sdk.agent.recovery import RollbackRecovery
        checkpoint_name = str(params.get("checkpoint_name", "checkpoint") or "checkpoint").strip()
        rr = RollbackRecovery(_ROOT_DIR)
        stash_id = rr.create_checkpoint(checkpoint_name)
        # create_checkpoint returns None when no snapshot was written. This
        # reported {"success": True} unconditionally, so the UI showed a
        # checkpoint the workspace did not actually have.
        if not stash_id:
            return _ok(req_id, {
                "success": False,
                "checkpoint_id": None,
                "error": (
                    f"checkpoint {checkpoint_name!r} was not created; see the "
                    "harness log for the snapshot failure"
                ),
            })
        return _ok(req_id, {"success": True, "checkpoint_id": stash_id})
    except Exception as exc:
        logger.exception("repo.checkpoint error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_repo_restore(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.restore`` — Revert to the last checkpoint.
    """
    try:
        from sdk.agent.recovery import RollbackRecovery
        checkpoint_name = str(params.get("checkpoint_name", "checkpoint") or "checkpoint").strip()
        rr = RollbackRecovery(_ROOT_DIR)
        success = rr.restore_checkpoint(checkpoint_name)
        return _ok(req_id, {"success": success, "checkpoint_id": checkpoint_name})
    except Exception as exc:
        logger.exception("repo.restore error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_report(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.report`` — Replay event logs and generate progress report.
    """
    task_id = params.get("task_id", "").strip()
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' is required")
        
    try:
        from sdk.agent.recovery import CompletionReporter
        reporter = CompletionReporter(task_id)
        report = await reporter.generate_report()
        return _ok(req_id, {"report": report})
    except Exception as exc:
        logger.exception("task.report error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_build_diagnose(params: dict[str, Any], req_id: Any) -> str:
    """
    ``build.diagnose`` — Diagnose compiler or terminal failure logs.
    """
    console_output = params.get("console_output", "")
    try:
        from sdk.agent.tester import BuildDiagnosis
        diag = BuildDiagnosis.diagnose_failure(console_output)
        return _ok(req_id, diag)
    except Exception as exc:
        logger.exception("build.diagnose error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_build_repair(params: dict[str, Any], req_id: Any) -> str:
    """
    ``build.repair`` — Triggers IterativeDebugger loop to fix a target file.
    """
    run_cmd = params.get("run_cmd", "")
    target_file = params.get("target_file", "")
    if not run_cmd or not target_file:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'run_cmd' and 'target_file' are required")
        
    try:
        from sdk.agent.tester import IterativeDebugger
        from sdk.agent.coder import CodeRepair
        
        target_code = params.get("target_code", "")
        replacement_code = params.get("replacement_code", "")
        
        def simple_edit(content, diagnostics):
            if target_code and replacement_code:
                return CodeRepair.apply_surgical_patch(content, target_code, replacement_code)
            return content
            
        debugger = IterativeDebugger(run_cmd, _ROOT_DIR)
        success = debugger.debug_loop(target_file, simple_edit, max_attempts=params.get("max_attempts", 3))
        return _ok(req_id, {"success": success})
    except Exception as exc:
        logger.exception("build.repair error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_shell_run(params: dict[str, Any], req_id: Any) -> str:
    """
    ``shell.run`` — Governed async shell execution.
    """
    command = params.get("command", "")
    if not command:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'command' is required")
        
    try:
        from sdk.agent.governance import PolicyEvaluator, RiskClassifier
        from sdk.agent.async_shell import AsyncShellExecutor

        risk = RiskClassifier.classify_action("execute_command", command)
        logger.info("Governance check: command=%r has risk=%s", command, risk)

        # The classifier used to be advisory: it computed a tier and this
        # handler logged it, and nothing anywhere read the result. Local
        # callers are trusted by design, but defense-in-depth still wants the
        # two teeth below — a hard block on the known-destructive patterns,
        # and an explicit approved=true before anything critical-tier runs.
        if not PolicyEvaluator.evaluate_command(command):
            return _ok(req_id, {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": "Command blocked by policy: matches a known-destructive pattern.",
                "timed_out": False,
                "blocked": True,
                "risk_level": "critical",
            })
        if risk == "critical" and not bool(params.get("approved")):
            return _ok(req_id, {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": (
                    "Command classified as critical risk and was not run. "
                    "Re-issue with approved=true after explicit operator approval."
                ),
                "timed_out": False,
                "blocked": True,
                "risk_level": risk,
            })

        # This handler calls the executor directly rather than going through
        # ToolRegistry.run_tool, so it never saw run_tool's halt check: the
        # one RPC that runs arbitrary shell commands was the one the kill
        # switch did not cover. AsyncShellExecutor now refuses on its own, and
        # this returns the refusal as a result rather than an error so the
        # caller can tell "halted" from "failed".
        result = await AsyncShellExecutor.run(command, _ROOT_DIR, timeout=params.get("timeout", 60.0))
        if result.get("halted"):
            return _ok(req_id, {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": result["stderr"],
                "timed_out": False,
                "halted": True,
                "risk_level": risk,
            })
        return _ok(req_id, {
            "success": result["success"],
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "timed_out": result["timed_out"],
            "risk_level": risk,
        })
    except Exception as exc:
        logger.exception("shell.run error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_generate(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.generate`` — Generate file from a template.
    """
    file_type = params.get("file_type", "")
    class_name = params.get("class_name", "")
    if not file_type or not class_name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'file_type' and 'class_name' are required")
        
    try:
        from sdk.agent.coder import CodeGenerator
        code = await CodeGenerator.generate_template(file_type, class_name, params.get("options"))
        return _ok(req_id, {"code": code})
    except Exception as exc:
        logger.exception("code.generate error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_repair(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.repair`` — Apply a surgical line patch to file content.
    """
    file_content = params.get("file_content", "")
    target_code = params.get("target_code", "")
    replacement_code = params.get("replacement_code", "")
    
    try:
        from sdk.agent.coder import CodeRepair
        patched = CodeRepair.apply_surgical_patch(file_content, target_code, replacement_code)
        return _ok(req_id, {"success": True, "patched_content": patched})
    except Exception as exc:
        logger.exception("code.repair error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_test_generate(params: dict[str, Any], req_id: Any) -> str:
    """
    ``test.generate`` — Generate a test suite.
    """
    target_file = params.get("target_file", "")
    file_type = params.get("file_type", "pytest")
    class_name = params.get("class_name", "")
    functions = params.get("functions", [])
    
    if not target_file or not class_name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'target_file' and 'class_name' are required")
        
    try:
        from sdk.agent.tester import TestGenerator
        test_code = TestGenerator.generate_test_suite(target_file, file_type, class_name, functions)
        return _ok(req_id, {"test_code": test_code})
    except Exception as exc:
        logger.exception("test.generate error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_propose_patch(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.propose_patch`` — Propose bounded exact-replacement file edits.
    """
    try:
        from sdk.agent.safe_code_bridges import propose_patch

        result = propose_patch(
            replacements=params.get("replacements"),
            rationale=params.get("rationale", ""),
            validation_commands=params.get("validation_commands"),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("code.propose_patch error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_apply_patch(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.apply_patch`` — Apply bounded exact-replacement file edits.
    """
    try:
        from sdk.agent.safe_code_bridges import apply_patch

        result = apply_patch(
            replacements=params.get("replacements"),
            expected_files=params.get("expected_files"),
            dry_run=bool(params.get("dry_run", False)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("code.apply_patch error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_propose_file_write(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.propose_file_write`` — Propose bounded workspace file creation.
    """
    try:
        from sdk.agent.safe_code_bridges import propose_file_write

        result = propose_file_write(
            file_writes=params.get("file_writes"),
            rationale=params.get("rationale", ""),
            validation_commands=params.get("validation_commands"),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("code.propose_file_write error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_write_files(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.write_files`` — Create bounded text files inside the workspace.
    """
    try:
        from sdk.agent.safe_code_bridges import write_files

        result = write_files(
            file_writes=params.get("file_writes"),
            expected_files=params.get("expected_files"),
            dry_run=bool(params.get("dry_run", False)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("code.write_files error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_registry_get(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.registry_get`` — Retrieve a key from CodeRegistry.
    """
    key = params.get("key")
    if not key or not isinstance(key, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'key' is required")
    try:
        from sdk.agent.code_registry import CodeRegistry
        val = await CodeRegistry.get(key)
        return _ok(req_id, {"key": key, "value": val})
    except Exception as exc:
        logger.exception("code.registry_get error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_registry_set(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.registry_set`` — Set a key in CodeRegistry.
    """
    key = params.get("key")
    value = params.get("value")
    if not key or not isinstance(key, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'key' is required")
    try:
        from sdk.agent.code_registry import CodeRegistry
        await CodeRegistry.set(key, value)
        return _ok(req_id, {"key": key, "value": value})
    except Exception as exc:
        logger.exception("code.registry_set error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_registry_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.registry_list`` — List keys matching prefix in CodeRegistry.
    """
    prefix = params.get("prefix")
    try:
        from sdk.agent.code_registry import CodeRegistry
        keys = await CodeRegistry.list_keys(prefix)
        return _ok(req_id, {"keys": keys})
    except Exception as exc:
        logger.exception("code.registry_list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))



async def handle_test_run_validation(params: dict[str, Any], req_id: Any) -> str:
    """
    ``test.run_validation`` — Run allowlisted validation commands.
    """
    try:
        from sdk.agent.safe_code_bridges import run_validation

        result = await run_validation(
            commands=params.get("commands"),
            timeout=float(params.get("timeout", 60.0)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("test.run_validation error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
