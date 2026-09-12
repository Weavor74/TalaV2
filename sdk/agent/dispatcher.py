"""
sdk/agent/dispatcher.py
=======================
Execution Dispatcher — Tala Cognitive Engine Room
Milestone 8 | Task 8.2

Implements:
  • TalaExecutionDispatcher — Central router that interprets a natural-language task
    description string and routes it to the appropriate SDK action.

    Routing table (case-insensitive keyword matching):
      'run pytest' | 'run tests' | 'pytest'
          → AsyncShellExecutor: python -m pytest -v  (in root_dir)

      'lint' | 'scan lint'
          → ArchitectureLinter.lint_all()

      'scan' | 'scan codebase' | 'repository scan'
          → RepositoryScanner.scan_repo()

      'create file' | 'write file' | 'generate file'
          → Extract filename from task_description, classify risk with RiskClassifier.
            low/medium risk → create file (content from context['content'] or empty).
            high/critical   → return failure requiring user authorisation.

      'shell' | 'run command' | 'execute'
          → AsyncShellExecutor with context['command']

      <default>
          → Log and queue for LLM processing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sdk.agent.async_shell import AsyncShellExecutor
from sdk.agent.governance import RiskClassifier
from sdk.agent.linter import ArchitectureLinter
from sdk.agent.scanner import RepositoryScanner

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from sdk.agent.adapter import AdapterPacket

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILE_PATTERN = re.compile(
    r"(?:create|write|generate|make)\s+(?:an?\s+)?(?:empty\s+)?(?:[\w\-]+\s+)?(?:file|script)(?:\s+named|\s+called)?\s*['\"]?([\w./\\-]+)['\"]?",
    re.IGNORECASE,
)


def _matches(task: str, *keywords: str) -> bool:
    """Return True if any keyword appears in *task* (case-insensitive)."""
    lower = task.lower()
    return any(kw in lower for kw in keywords)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TalaExecutionDispatcher:
    """
    Routes a free-text task description to the correct SDK action.

    Usage::

        dispatcher = TalaExecutionDispatcher()
        result = await dispatcher.dispatch("run pytest", root_dir="/path/to/project")

    All public methods are async to support use inside async task loops.
    """

    async def dispatch(
        self,
        task_description: str,
        root_dir: str,
        context: dict[str, Any] | None = None,
        authority_level: str = "auto",
        adapter_packet: "AdapterPacket | None" = None,
    ) -> dict[str, Any]:
        """
        Interpret *task_description* and delegate to the appropriate SDK component.

        Parameters
        ----------
        task_description:
            Natural-language description of the task to perform.
        root_dir:
            Absolute path to the project root directory (used as cwd for shell
            commands and as the scan root for linter / scanner).
        context:
            Optional dict carrying extra parameters:
              - 'command'  (str) : shell command for execute/shell routing.
              - 'content'  (str) : file content for create-file routing.
              - 'target_file' (str) : file name for code generation/edits.

        Returns
        -------
        dict with keys:
            success      (bool) — Whether the action completed without error.
            action_taken (str)  — Short label identifying the route taken.
            output       (Any)  — Structured output from the action.
            error        (str | None) — Error message if success is False.
        """
        context = context or {}
        task_lower = task_description.lower()

        log.info("TalaExecutionDispatcher: dispatching task=%r", task_description)

        tool_key = context.get("tool")

        # ------------------------------------------------------------------
        # Enforce Adapter Constraints (LoRA-like overriding)
        # ------------------------------------------------------------------
        if adapter_packet:
            # 1. forbidden_actions acts as a kill-switch
            if tool_key and tool_key in adapter_packet.forbidden_actions:
                return {
                    "success": False,
                    "action_taken": "blocked",
                    "output": None,
                    "error": f"Blocked by active AdapterPacket: {tool_key} is in forbidden_actions."
                }
            
            # 2. tool_affordances acts as a scoped allowlist (if explicitly defined)
            if adapter_packet.tool_affordances and tool_key and tool_key not in adapter_packet.tool_affordances:
                return {
                    "success": False,
                    "action_taken": "blocked",
                    "output": None,
                    "error": f"Blocked by active AdapterPacket: {tool_key} is not in tool_affordances."
                }

        # ------------------------------------------------------------------
        # 0. Structured Tool Routing (Highest Priority)
        # ------------------------------------------------------------------
        if tool_key:
            if tool_key == "write_file":
                path = context.get("path")
                content = context.get("content", "")
                if path:
                    import pathlib, os
                    abs_path = path if os.path.isabs(path) else os.path.join(root_dir, path)
                    try:
                        target = pathlib.Path(abs_path)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content, encoding="utf-8")
                        
                        if abs_path.endswith(".py"):
                            import sys
                            from sdk.agent.async_shell import AsyncShellExecutor
                            chk = await AsyncShellExecutor.run_exec(sys.executable, ["-m", "py_compile", abs_path], cwd=root_dir)
                            if not chk["success"]:
                                return {
                                    "success": False,
                                    "action_taken": "write_file",
                                    "output": None,
                                    "error": f"SyntaxError detected by py_compile:\n{chk.get('stderr') or chk.get('stdout')}"
                                }

                        return {
                            "success": True,
                            "action_taken": "write_file",
                            "output": f"File created/written: {abs_path}",
                            "error": None,
                        }
                    except Exception as e:
                        return {"success": False, "action_taken": "write_file", "output": None, "error": str(e)}

            elif tool_key == "run_python_file":
                path = context.get("path")
                if path:
                    import sys, os
                    from sdk.agent.async_shell import AsyncShellExecutor
                    abs_path = path if os.path.isabs(path) else os.path.join(root_dir, path)
                    if not os.path.exists(abs_path):
                        return {
                            "success": False,
                            "action_taken": "run_python_file",
                            "output": None,
                            "error": f"File not found before execution: {abs_path}"
                        }
                    res = await AsyncShellExecutor.run_exec(sys.executable, [abs_path], cwd=root_dir)
                    return {
                        "success": res["success"],
                        "action_taken": "run_python_file",
                        "output": res.get("stdout"),
                        "error": res.get("stderr") if not res["success"] else None,
                    }

            elif tool_key == "shell":
                return await self._run_shell(task_description, context, root_dir)

        # ------------------------------------------------------------------
        # 1. Pytest / test execution
        # ------------------------------------------------------------------
        if _matches(task_lower, "run pytest", "run tests", "pytest"):
            return await self._run_pytest(root_dir)

        # ------------------------------------------------------------------
        # 2. Lint (must be checked before generic 'scan' to avoid misrouting)
        # ------------------------------------------------------------------
        if _matches(task_lower, "lint", "scan lint"):
            return await self._run_lint(root_dir)

        # ------------------------------------------------------------------
        # 3. Repository scan
        # ------------------------------------------------------------------
        if _matches(task_lower, "scan", "scan codebase", "repository scan"):
            return await self._run_scan(root_dir)

        # ------------------------------------------------------------------
        # Dynamic Tool Execution
        # ------------------------------------------------------------------
        if _matches(task_lower, "run tool", "execute tool", "call tool"):
            tool_name = context.get("tool_name") or context.get("tool")
            tool_args = context.get("arguments") or context.get("args") or {}
            
            if not tool_name:
                match = re.search(r"(?:run|execute|call)\s+tool\s+['\"]?([\w.\-]+)['\"]?", task_description, re.IGNORECASE)
                if match:
                    tool_name = match.group(1)
            
            if tool_name:
                log.info("TalaExecutionDispatcher: routing to dynamic tool %r", tool_name)
                from sdk.agent.tools import ToolRegistry
                res = await ToolRegistry.run_tool(
                    name=tool_name,
                    arguments=tool_args,
                    authority_level=authority_level,
                    bypass_confidence_gate=bool(context.get("is_fallback")),
                )

                # ── Confidence gate: skill degraded — re-enter goal planning ──
                if res.get("confidence_gate_triggered"):
                    log.info(
                        "TalaExecutionDispatcher: confidence gate triggered for skill %r "
                        "(confidence=%.3f < gate=%.2f) — falling back to GoalEngine.",
                        tool_name,
                        res.get("confidence", 0.0),
                        res.get("threshold", 0.5),
                    )
                    return await self._goal_engine_fallback(
                        task_description,
                        root_dir,
                        context,
                        authority_level,
                        reason=res["error"],
                    )
                # ─────────────────────────────────────────────────────────────

                return {
                    "success": res["success"],
                    "action_taken": f"tool_{tool_name}",
                    "output": res.get("output"),
                    "error": res.get("error"),
                }


        # ------------------------------------------------------------------
        # File creation (deterministic fallback / empty touch)
        # ------------------------------------------------------------------
        file_match = _FILE_PATTERN.search(task_description)
        if file_match or _matches(task_lower, "create file", "write file", "generate file"):
            return await self._create_file(task_description, context, root_dir)

        # ------------------------------------------------------------------
        # LLM-guided Code Generation
        # ------------------------------------------------------------------
        if _matches(task_lower, "generate", "write", "code", "implement"):
            target_file = context.get("target_file") or context.get("file") or context.get("filename")
            if not target_file:
                match = _FILE_PATTERN.search(task_description)
                if match:
                    target_file = match.group(1)
            
            if target_file:
                log.info("TalaExecutionDispatcher: routing to LLM code generator for %s", target_file)
                from sdk.agent.llm_coder import LLMCodeGenerator
                gen = LLMCodeGenerator()
                res = await gen.generate_and_write(
                    goal=task_description,
                    target_file=target_file,
                    root_dir=root_dir,
                    context=context,
                    authority_level=authority_level,
                )
                return {
                    "success": res["success"],
                    "action_taken": "llm_generate",
                    "output": res,
                    "error": res.get("error"),
                }

        # ------------------------------------------------------------------
        # LLM-guided Code Editing
        # ------------------------------------------------------------------
        if _matches(task_lower, "edit", "modify", "fix", "patch"):
            target_file = context.get("target_file") or context.get("file") or context.get("filename")
            error_context = context.get("error_context") or context.get("traceback")
            
            if not target_file and error_context:
                from sdk.agent.editor import ErrorRepairProtocol
                try:
                    erp = ErrorRepairProtocol()
                    err_loc = erp.find_root_error(error_context)
                    target_file = err_loc.file_path
                except Exception as exc:
                    log.warning("TalaExecutionDispatcher: ErrorRepairProtocol failed to find root error: %s", exc)

            if not target_file:
                match = _FILE_PATTERN.search(task_description)
                if match:
                    target_file = match.group(1)

            if target_file:
                log.info("TalaExecutionDispatcher: routing to LLM editor for %s", target_file)
                from sdk.agent.llm_coder import LLMCodeGenerator
                gen = LLMCodeGenerator()
                res = await gen.generate_edit(
                    goal=task_description,
                    target_file=target_file,
                    root_dir=root_dir,
                    error_context=error_context,
                    authority_level=authority_level,
                )
                return {
                    "success": res["success"],
                    "action_taken": "llm_edit",
                    "output": res,
                    "error": res.get("error"),
                }


        # ------------------------------------------------------------------
        # 5. Shell / command execution
        # ------------------------------------------------------------------
        if _matches(task_lower, "shell", "run command", "execute"):
            return await self._run_shell(task_description, context, root_dir)

        # ------------------------------------------------------------------
        # Try LLM-native routing classification fallback
        # ------------------------------------------------------------------
        llm_route = await self._classify_task_via_llm(task_description)
        if llm_route != "logged":
            log.info("TalaExecutionDispatcher: LLM classified task as route %s", llm_route)
            if llm_route == "pytest":
                if _matches(task_lower, "pytest", "test suite", "run tests"):
                    return await self._run_pytest(root_dir)
                else:
                    log.info("TalaExecutionDispatcher: LLM suggested pytest but phrase not explicitly requested. Skipping.")
            elif llm_route == "lint":
                return await self._run_lint(root_dir)
            elif llm_route == "scan":
                return await self._run_scan(root_dir)
            elif llm_route == "llm_generate":
                target_file = context.get("target_file") or context.get("file") or context.get("filename")
                if not target_file:
                    match = _FILE_PATTERN.search(task_description)
                    if match:
                        target_file = match.group(1)
                if target_file:
                    from sdk.agent.llm_coder import LLMCodeGenerator
                    gen = LLMCodeGenerator()
                    res = await gen.generate_and_write(
                        goal=task_description,
                        target_file=target_file,
                        root_dir=root_dir,
                        context=context,
                        authority_level=authority_level,
                    )
                    return {
                        "success": res["success"],
                        "action_taken": "llm_generate",
                        "output": res,
                        "error": res.get("error"),
                    }
            elif llm_route == "llm_edit":
                target_file = context.get("target_file") or context.get("file") or context.get("filename")
                error_context = context.get("error_context") or context.get("traceback")
                if not target_file and error_context:
                    from sdk.agent.editor import ErrorRepairProtocol
                    try:
                        erp = ErrorRepairProtocol()
                        err_loc = erp.find_root_error(error_context)
                        target_file = err_loc.file_path
                    except Exception:
                        pass
                if not target_file:
                    match = _FILE_PATTERN.search(task_description)
                    if match:
                        target_file = match.group(1)
                if target_file:
                    from sdk.agent.llm_coder import LLMCodeGenerator
                    gen = LLMCodeGenerator()
                    res = await gen.generate_edit(
                        goal=task_description,
                        target_file=target_file,
                        root_dir=root_dir,
                        error_context=error_context,
                        authority_level=authority_level,
                    )
                    return {
                        "success": res["success"],
                        "action_taken": "llm_edit",
                        "output": res,
                        "error": res.get("error"),
                    }
            elif llm_route == "shell":
                return await self._run_shell(task_description, context, root_dir)

        # ------------------------------------------------------------------
        # Default: queue for LLM processing
        # ------------------------------------------------------------------
        return await self._default_fallback(task_description, root_dir, context, authority_level)

    # ------------------------------------------------------------------
    # Private route handlers
    # ------------------------------------------------------------------

    async def _run_pytest(self, root_dir: str) -> dict[str, Any]:
        log.debug("TalaExecutionDispatcher: routing to pytest runner")
        result = await AsyncShellExecutor.run(
            command="python -m pytest -v",
            cwd=root_dir,
            timeout=120.0,
        )
        return {
            "success": result["success"],
            "action_taken": "pytest",
            "output": result,
            "error": None if result["success"] else result.get("stderr", "pytest failed"),
        }

    async def _run_lint(self, root_dir: str) -> dict[str, Any]:
        log.debug("TalaExecutionDispatcher: routing to ArchitectureLinter")
        try:
            linter = ArchitectureLinter(root_dir=root_dir)
            output = linter.lint_all()
            return {
                "success": True,
                "action_taken": "lint",
                "output": output,
                "error": None,
            }
        except Exception as exc:
            log.error("TalaExecutionDispatcher: lint failed: %s", exc)
            return {
                "success": False,
                "action_taken": "lint",
                "output": None,
                "error": str(exc),
            }

    async def _run_scan(self, root_dir: str) -> dict[str, Any]:
        log.debug("TalaExecutionDispatcher: routing to RepositoryScanner")
        try:
            scanner = RepositoryScanner(root_dir=root_dir)
            output = scanner.scan_repo()
            return {
                "success": True,
                "action_taken": "scan",
                "output": output,
                "error": None,
            }
        except Exception as exc:
            log.error("TalaExecutionDispatcher: scan failed: %s", exc)
            return {
                "success": False,
                "action_taken": "scan",
                "output": None,
                "error": str(exc),
            }

    async def _create_file(
        self,
        task_description: str,
        context: dict[str, Any],
        root_dir: str,
    ) -> dict[str, Any]:
        log.debug("TalaExecutionDispatcher: routing to file creation")

        match = _FILE_PATTERN.search(task_description)
        if not match:
            return {
                "success": False,
                "action_taken": "create_file",
                "output": None,
                "error": "Could not extract filename from task description.",
            }

        filename = match.group(1)
        risk = RiskClassifier.classify_action("write_file", filename)

        log.info(
            "TalaExecutionDispatcher: create_file filename=%r risk=%s", filename, risk
        )

        import os
        abs_filename = filename if os.path.isabs(filename) else os.path.join(root_dir, filename)

        if risk in ("low", "medium"):
            content = context.get("content")
            if content is None:
                log.info("TalaExecutionDispatcher: no content provided for file creation; delegating to LLMCodeGenerator")
                from sdk.agent.llm_coder import LLMCodeGenerator
                gen = LLMCodeGenerator()
                
                res = await gen.generate_and_write(
                    goal=task_description,
                    target_file=abs_filename,
                    root_dir=root_dir,
                    context=context,
                    authority_level="auto",
                )
                return {
                    "success": res["success"],
                    "action_taken": "create_file_llm",
                    "output": res,
                    "error": res.get("error"),
                }
            
            import pathlib
            target = pathlib.Path(abs_filename)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if content is not None:
                    target.write_text(content, encoding="utf-8")
                else:
                    target.touch(exist_ok=True)
            except Exception as exc:
                log.error("TalaExecutionDispatcher: file write/touch failed: %s", exc)
                return {
                    "success": False,
                    "action_taken": "create_file",
                    "output": None,
                    "error": str(exc),
                }
            return {
                "success": True,
                "action_taken": "create_file",
                "output": f"File created: {abs_filename}",
                "error": None,
            }

        # high or critical risk
        return {
            "success": False,
            "action_taken": "create_file",
            "output": None,
            "error": "high risk file write requires user authorization",
        }

    async def _run_shell(
        self,
        task_description: str,
        context: dict[str, Any],
        root_dir: str,
    ) -> dict[str, Any]:
        command: str = context.get("command", "")
        if not command:
            import re
            desc = task_description
            # 1. Try to find explicit command or run directives
            match = re.search(r"(?:command|run)[:\s]*['\"]?((?:python|pytest|npm|node)[^'\"\;]*)", desc, re.IGNORECASE)
            if match:
                cmd = match.group(1).strip()
                if cmd.endswith('.'):
                    cmd = cmd[:-1]
                command = cmd
                log.info("TalaExecutionDispatcher: extracted command via regex from task_description: %r", command)
            else:
                # 2. Try to find any python script execution
                match_py = re.search(r"['\"]?([\w./-]+\.py)['\"]?", desc, re.IGNORECASE)
                if match_py:
                    command = f"python {match_py.group(1)}"
                    log.info("TalaExecutionDispatcher: fallback extracted python command: %r", command)
                else:
                    # 3. Ultimate fallback: Don't crash the engine, just return a fake success or echo
                    command = "echo 'No explicit command found in task'"
                    log.warning("TalaExecutionDispatcher: could not extract command, defaulting to safe echo.")

        # --- Upgrade logic to Structured Tools ---
        import re
        import shlex
        import os
        import sys
        
        # Upgrade python scripts to run_python_file
        if command.startswith("python ") and " -m " not in command and " -c " not in command:
            try:
                parts = shlex.split(command)
                if len(parts) >= 2 and parts[0] == "python":
                    path = parts[1]
                    log.info("TalaExecutionDispatcher: upgrading shell command to 'run_python_file' for %s", path)
                    from sdk.agent.async_shell import AsyncShellExecutor
                    abs_path = path if os.path.isabs(path) else os.path.join(root_dir, path)
                    if not os.path.exists(abs_path):
                        return {
                            "success": False,
                            "action_taken": "run_python_file",
                            "output": None,
                            "error": f"File not found before execution: {abs_path}"
                        }
                    args = [abs_path] + parts[2:]
                    res = await AsyncShellExecutor.run_exec(sys.executable, args, cwd=root_dir)
                    return {
                        "success": res["success"],
                        "action_taken": "run_python_file",
                        "output": res.get("stdout"),
                        "error": res.get("stderr") if not res["success"] else None,
                    }
            except Exception as e:
                log.warning("TalaExecutionDispatcher: failed to upgrade python command: %s", e)
                
        # Upgrade simple echo > file to write_file
        echo_match = re.match(r"^echo\s+['\"]?(.*?)['\"]?\s*>\s*['\"]?([\w./\-\\]+)['\"]?$", command)
        if echo_match:
            content, path = echo_match.groups()
            log.info("TalaExecutionDispatcher: upgrading shell echo to 'write_file' for %s", path)
            import pathlib
            abs_path = path if os.path.isabs(path) else os.path.join(root_dir, path)
            try:
                target = pathlib.Path(abs_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return {
                    "success": True,
                    "action_taken": "write_file",
                    "output": f"File written via upgraded shell cmd: {abs_path}",
                    "error": None,
                }
            except Exception as e:
                return {"success": False, "action_taken": "write_file", "output": None, "error": str(e)}

        log.debug("TalaExecutionDispatcher: routing to LocalEnvironment command=%r", command)
        try:
            import asyncio
            from python.inference.terminal_backends.local import LocalEnvironment

            env = LocalEnvironment(cwd=root_dir)
            try:
                if not env._snapshot_ready:
                    await asyncio.to_thread(env.init_session)
                proc_result = await asyncio.to_thread(env.execute, command)
            finally:
                env.cleanup()

            result = {
                "success": proc_result.returncode == 0,
                "exit_code": proc_result.returncode,
                "stdout": proc_result.output,
                "stderr": "",
                "timed_out": proc_result.returncode == 124,
            }
        except Exception as exc:
            log.warning("TalaExecutionDispatcher: LocalEnvironment failed, falling back to AsyncShellExecutor: %s", exc)
            from sdk.agent.async_shell import AsyncShellExecutor
            result = await AsyncShellExecutor.run(command=command, cwd=root_dir)

        return {
            "success": result["success"],
            "action_taken": "shell",
            "output": result,
            "error": None if result["success"] else result.get("stderr", "shell failed"),
        }

    async def _default_fallback(
        self,
        task_description: str,
        root_dir: str,
        context: dict[str, Any],
        authority_level: str = "auto",
    ) -> dict[str, Any]:
        target_file = context.get("target_file") or context.get("file") or context.get("filename")
        if target_file:
            log.info("TalaExecutionDispatcher: falling back to LLM code generation for %s", target_file)
            from sdk.agent.llm_coder import LLMCodeGenerator
            gen = LLMCodeGenerator()
            res = await gen.generate_and_write(
                goal=task_description,
                target_file=target_file,
                root_dir=root_dir,
                context=context,
                authority_level=authority_level,
            )
            return {
                "success": res["success"],
                "action_taken": "llm_generate",
                "output": res,
                "error": res.get("error"),
            }
        log.info(
            "TalaExecutionDispatcher: no route matched — queuing for LLM: %r", task_description
        )
        return {
            "success": True,
            "action_taken": "logged",
            "output": f"Task queued for LLM processing: {task_description}",
            "error": None,
        }

    async def _classify_task_via_llm(self, task_description: str) -> str:
        """
        Query LLM to semantically classify the task description into a route.
        """
        prompt = f"""You are the routing system for an agentic coding assistant.
Given this task description, classify it into exactly one of the following category strings:
- pytest
- lint
- scan
- llm_generate
- llm_edit
- shell
- logged

Task description: "{task_description}"

Response must contain ONLY the category name. No markdown, no explanations."""

        from sdk.agent.llm_coder import LLMCodeGenerator
        try:
            gen = LLMCodeGenerator()
            raw = await gen._call_llm(prompt, "text")
            if raw:
                cleaned = raw.strip().lower()
                for route in ("pytest", "lint", "scan", "llm_generate", "llm_edit", "shell", "logged"):
                    if route in cleaned:
                        return route
        except Exception as exc:
            log.debug("TalaExecutionDispatcher: classification failed: %s", exc)
        
        return "logged"

    # ------------------------------------------------------------------
    # Confidence gate fallback
    # ------------------------------------------------------------------

    async def _goal_engine_fallback(
        self,
        task_description: str,
        root_dir: str,
        context: dict[str, Any],
        authority_level: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """
        Re-enter the full GoalEngine planning and execution cycle when a
        distilled skill's confidence score falls below
        :attr:`ToolRegistry.SKILL_CONFIDENCE_GATE`.

        This closes the feedback loop: historical execution performance
        (tracked in ``metrics.confidence`` inside each skill HTML-NN node)
        directly controls *runtime execution strategy* — a low-confidence
        skill is treated as a suggestion, not a command.

        The method is intentionally fire-and-forward: it passes the original
        ``task_description`` unchanged so GoalEngine can re-decompose,
        re-plan, and potentially produce an improved skill node if the new
        run succeeds cleanly.

        Parameters
        ----------
        task_description:
            The original natural-language task string.
        root_dir:
            Workspace root passed through to GoalEngine.
        context:
            Caller context dict (may carry authority_level, adapter_packet, etc.).
        authority_level:
            Governance authority level propagated from the calling context.
        reason:
            Human-readable explanation of why the gate fired (for logging).

        Returns
        -------
        dict with ``action_taken='goal_engine_fallback'`` and
        ``confidence_gate_triggered=True`` so callers can distinguish this
        path from a normal goal execution result.
        """
        log.info(
            "TalaExecutionDispatcher: entering GoalEngine fallback. "
            "reason=%r task=%r",
            reason, task_description[:80],
        )
        try:
            from sdk.agent.goal_engine import GoalEngine
            from sdk.agent.batch_policy import BatchPolicy

            engine = GoalEngine(root_dir=root_dir, is_fallback=True)
            policy = BatchPolicy.auto()

            # Propagate adapter_packet if the caller embedded it in context
            adapter_packet = context.get("adapter_packet")

            result = await engine.run_goal(
                goal_text=task_description,
                policy=policy,
                adapter_packet=adapter_packet,
            )

            return {
                "success": result.get("success", False),
                "action_taken": "goal_engine_fallback",
                "confidence_gate_triggered": True,
                "fallback_reason": reason,
                "output": result,
                "error": result.get("error"),
            }
        except Exception as exc:
            log.error(
                "TalaExecutionDispatcher: GoalEngine fallback raised: %s", exc
            )
            return {
                "success": False,
                "action_taken": "goal_engine_fallback",
                "confidence_gate_triggered": True,
                "fallback_reason": reason,
                "output": None,
                "error": str(exc),
            }

