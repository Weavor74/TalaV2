"""
sdk/agent/llm_coder.py
======================
LLM-Guided Code Generation & File Writing — Tala Cognitive Engine Room
Milestone 11 | Task 11.1

Provides:
  LLMCodeGenerator — Calls local LLM (Ollama/embedded) to generate or edit code
  for a given goal, then writes it to the target file with authority-level gating.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from sdk.agent.coder import CodeGenerator
from sdk.agent.editor import SurgicalEditor
from sdk.agent.governance import RiskClassifier
from sdk.agent.prompt import PromptAssembler

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from sdk.agent.adapter import AdapterPacket

log = logging.getLogger(__name__)

_LANG_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".html": "html",
    ".css": "css",
    ".json": "json",
    ".md": "markdown",
}

_OLLAMA_ENDPOINT = "http://localhost:11434"
_OLLAMA_MODEL = "llama3.2"
_OLLAMA_TIMEOUT = 30.0


class LLMCodeGenerator:
    """
    Generates or edits source code via the local LLM and writes it to disk.

    Authority levels:
      "auto"          — auto-execute low/medium/high risk, block critical only
      "confirm_writes" — auto-execute low/medium, require confirmation for high/critical
      "full_confirm"  — require confirmation for any file write
    """

    def __init__(self) -> None:
        self._editor = SurgicalEditor()

    async def generate_and_write(
        self,
        goal: str,
        target_file: str,
        root_dir: str,
        context: dict[str, Any] | None = None,
        authority_level: str = "auto",
        adapter_packet: "AdapterPacket | None" = None,
    ) -> dict[str, Any]:
        """
        Generate code for *goal* and write it to *target_file*.

        Returns dict with keys:
            success (bool), file_path (str), content_written (int),
            lines_written (int), risk_level (str),
            requires_confirmation (bool), error (str | None)
        """
        context = context or {}
        target = Path(target_file)
        ext = target.suffix.lower()
        language = _LANG_MAP.get(ext, "text")

        # Read existing content for context injection
        existing_content: str | None = None
        if target.exists():
            try:
                existing_content = target.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                log.warning("LLMCodeGenerator: could not read existing %s: %s", target_file, exc)

        # Build prompt
        base_prompt = PromptAssembler.assemble_prompt(goal, adapter_packet=adapter_packet)
        prompt_parts = [
            base_prompt,
            "",
            f"=== CODE GENERATION REQUEST ===",
            f"Language: {language}",
            f"Target file: {target.name}",
        ]
        if existing_content is not None:
            prompt_parts += [
                f"\nExisting file content (modify as needed):",
                f"```{language}",
                existing_content[:4000],  # cap at 4k chars
                "```",
            ]
        else:
            prompt_parts.append("(new file — generate from scratch)")

        prompt_parts += [
            "",
            "Output ONLY a single fenced code block with the complete file content. No explanation.",
        ]
        prompt = "\n".join(prompt_parts)

        # Authority / risk gate
        risk = RiskClassifier.classify_action("write_file", target_file)
        blocked = self._is_blocked(authority_level, risk)
        if blocked:
            return {
                "success": False,
                "requires_confirmation": True,
                "content_preview": "",
                "risk_level": risk,
                "file_path": target_file,
                "error": f"Authorization required: risk={risk}, authority={authority_level}",
            }

        # Call LLM
        code = await self._call_llm(prompt, language)

        if code and re.search(r'\[Placeholder.*?\]', code, re.IGNORECASE):
            return {
                "success": False,
                "requires_confirmation": False,
                "file_path": target_file,
                "risk_level": risk,
                "error": "Code rejected: LLM generated a placeholder instead of complete code."
            }

        # Fallback: template generation
        if not code:
            log.info("LLMCodeGenerator: LLM unavailable, falling back to template for %s", target.name)
            class_name = target.stem.replace("_", " ").title().replace(" ", "")
            try:
                code = CodeGenerator.generate_template(language if language in ("python", "javascript", "typescript", "html-nn") else "python", class_name)
            except ValueError:
                code = f"# {goal}\n"

        # Write file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
            log.info("LLMCodeGenerator: wrote %d chars to %s (risk=%s)", len(code), target_file, risk)

            if target.suffix.lower() == ".py":
                import sys
                from sdk.agent.async_shell import AsyncShellExecutor
                chk = await AsyncShellExecutor.run_exec(sys.executable, ["-m", "py_compile", target_file], cwd=root_dir)
                if not chk["success"]:
                    return {
                        "success": False,
                        "requires_confirmation": False,
                        "file_path": target_file,
                        "risk_level": risk,
                        "error": f"SyntaxError detected by py_compile:\n{chk.get('stderr') or chk.get('stdout')}"
                    }
        except Exception as exc:
            log.error("LLMCodeGenerator: write failed: %s", exc)
            return {
                "success": False,
                "requires_confirmation": False,
                "file_path": target_file,
                "risk_level": risk,
                "error": str(exc),
            }

        # Test-driven Verify-Repair Loop
        test_file = context.get("test_file") or context.get("test")
        if test_file:
            log.info("LLMCodeGenerator: running test suite %s for verification", test_file)
            from sdk.agent.async_shell import AsyncShellExecutor
            test_command = f"python -m pytest {test_file} -v"
            
            test_res = await AsyncShellExecutor.run(
                command=test_command,
                cwd=root_dir,
                timeout=60.0,
            )
            
            if not test_res.get("success", False):
                log.warning("LLMCodeGenerator: initial test execution failed. Entering self-repair loop.")
                
                # Run self-repair loop (up to 3 retries)
                repair_success = False
                for attempt in range(1, 4):
                    log.info("LLMCodeGenerator: self-repair attempt %d/3", attempt)
                    error_context = (test_res.get("stdout") or "") + "\n" + (test_res.get("stderr") or "")
                    
                    edit_res = await self.generate_edit(
                        goal=goal,
                        target_file=target_file,
                        root_dir=root_dir,
                        error_context=error_context,
                        authority_level=authority_level,
                        adapter_packet=adapter_packet,
                    )
                    
                    if not edit_res.get("success", False):
                        log.warning("LLMCodeGenerator: self-repair edit failed on attempt %d: %s", attempt, edit_res.get("error"))
                        continue
                        
                    # Re-run tests to verify the fix
                    test_res = await AsyncShellExecutor.run(
                        command=test_command,
                        cwd=root_dir,
                        timeout=60.0,
                    )
                    
                    if test_res.get("success", False):
                        log.info("LLMCodeGenerator: self-repair succeeded on attempt %d!", attempt)
                        repair_success = True
                        break
                
                if not repair_success:
                    return {
                        "success": False,
                        "requires_confirmation": False,
                        "file_path": target_file,
                        "risk_level": risk,
                        "error": f"Verify-repair loop failed. Tests still failing: {test_res.get('stdout') or test_res.get('stderr')}",
                    }

        return {
            "success": True,
            "file_path": target_file,
            "content_written": len(code),
            "lines_written": code.count("\n") + 1,
            "risk_level": risk,
            "requires_confirmation": False,
            "error": None,
        }

    async def generate_edit(
        self,
        goal: str,
        target_file: str,
        root_dir: str,
        error_context: str | None = None,
        authority_level: str = "auto",
        adapter_packet: "AdapterPacket | None" = None,
    ) -> dict[str, Any]:
        """
        Ask the LLM for a targeted line-level fix for *target_file* and apply it.
        LLM must respond with a block in the format:
            FILE: filename
            LINE: N
            OLD: old code
            NEW: new code
        Falls back to a no-op if LLM is unavailable or format is unrecognized.
        """
        target = Path(target_file)
        if not target.exists():
            return {"success": False, "error": f"File not found: {target_file}"}

        # Authority / risk gate
        risk = RiskClassifier.classify_action("write_file", target_file)
        if self._is_blocked(authority_level, risk):
            return {
                "success": False,
                "requires_confirmation": True,
                "risk_level": risk,
                "error": f"Authorization required: risk={risk}",
            }

        content = target.read_text(encoding="utf-8", errors="replace")
        ext = target.suffix.lower()
        language = _LANG_MAP.get(ext, "text")

        prompt_parts = [
            PromptAssembler.assemble_prompt(goal, adapter_packet=adapter_packet),
            "",
            "=== FILE EDIT REQUEST ===",
            f"File: {target.name}",
            f"Language: {language}",
            "Current content:",
            f"```{language}",
            content[:4000],
            "```",
        ]
        if error_context:
            prompt_parts += ["", "Error to fix:", "```", error_context[:1000], "```"]

        prompt_parts += [
            "",
            "Respond with one of the following two formats (no other text).",
            "",
            "Format A (Pinpoint Single Line replacement):",
            "FILE: <filename>",
            "LINE: <line number>",
            "OLD: <exact existing line content>",
            "NEW: <replacement line content>",
            "",
            "Format B (Multiline Block replacement):",
            "<<<<<<< OLD",
            "<exact multi-line existing code block to replace>",
            "=======",
            "<replacement multi-line code block>",
            ">>>>>>> NEW",
        ]
        prompt = "\n".join(prompt_parts)

        raw = await self._call_llm(prompt, language)
        if not raw:
            return {"success": False, "error": "LLM unavailable, cannot generate edit"}

        if re.search(r'\[Placeholder.*?\]', raw, re.IGNORECASE):
            return {"success": False, "error": "Edit rejected: LLM generated a placeholder instead of real code."}

        # 1. Try parsing Format B (multiline conflict-marker style)
        marker_match = re.search(
            r"<<<<<<<\s*OLD\r?\n([\s\S]+?)\r?\n=======\r?\n([\s\S]*?)\r?\n>>>>>>>\s*NEW",
            raw,
            re.MULTILINE
        )
        if marker_match:
            old_code = marker_match.group(1)
            new_code = marker_match.group(2)
            log.info("LLMCodeGenerator: parsed multiline block patch")
            
            result = self._editor.apply_patch(
                file_path=target_file,
                target_line=1,
                old_content=old_code,
                new_content=new_code,
                force=True,
            )
            if result.success and target.suffix.lower() == ".py":
                import sys
                from sdk.agent.async_shell import AsyncShellExecutor
                chk = await AsyncShellExecutor.run_exec(sys.executable, ["-m", "py_compile", target_file], cwd=root_dir)
                if not chk["success"]:
                    return {
                        "success": False,
                        "file_path": target_file,
                        "lines_changed": result.lines_changed,
                        "error": f"SyntaxError after patch detected by py_compile:\n{chk.get('stderr') or chk.get('stdout')}"
                    }

            return {
                "success": result.success,
                "file_path": target_file,
                "lines_changed": result.lines_changed,
                "error": result.error,
            }

        # 2. Fall back to Format A (single-line replacement)
        line_match = re.search(r"LINE:\s*(\d+)", raw)
        old_match = re.search(r"OLD:\s*(.+)", raw)
        new_match = re.search(r"NEW:\s*(.+)", raw)

        if not (line_match and old_match and new_match):
            return {"success": False, "error": f"LLM response did not match expected format: {raw[:200]}"}

        line_num = int(line_match.group(1))
        old_code = old_match.group(1).rstrip("\r")
        new_code = new_match.group(1).rstrip("\r")

        result = self._editor.apply_patch(
            file_path=target_file,
            target_line=line_num,
            old_content=old_code,
            new_content=new_code,
            force=True,
        )
        if result.success and target.suffix.lower() == ".py":
            import sys
            from sdk.agent.async_shell import AsyncShellExecutor
            chk = await AsyncShellExecutor.run_exec(sys.executable, ["-m", "py_compile", target_file], cwd=root_dir)
            if not chk["success"]:
                return {
                    "success": False,
                    "file_path": target_file,
                    "lines_changed": result.lines_changed,
                    "error": f"SyntaxError after single-line patch detected by py_compile:\n{chk.get('stderr') or chk.get('stdout')}"
                }

        return {
            "success": result.success,
            "file_path": target_file,
            "lines_changed": result.lines_changed,
            "error": result.error,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _call_llm(self, prompt: str, language: str) -> str | None:
        """Query LLM via the unified handle_inference_generate provider chain."""
        try:
            from python.harness.server import handle_inference_generate
            params = {
                "prompt": prompt,
                "stream": False,
                "raw_prompt": True,
                "system_prompt": "You are a precise, helpful software engineering assistant. Output code blocks or clean answers as requested.",
            }
            res_str = await handle_inference_generate(params, ws=None, req_id="sdk-llm-coder")
            import json
            res_dict = json.loads(res_str)
            if "result" in res_dict:
                text = res_dict["result"]["text"]
                return self._extract_code(text, language)
            elif "error" in res_dict:
                log.warning("LLMCodeGenerator: inference error: %s", res_dict["error"])
        except Exception as exc:
            log.warning("LLMCodeGenerator: failed to call handle_inference_generate: %s", exc)
        return None

    @staticmethod
    def _extract_code(text: str, language: str) -> str:
        """Strip markdown fences from LLM response, return raw code."""
        # Try specific language fence first, then generic
        for pattern in (
            rf"```{re.escape(language)}\s*([\s\S]+?)\s*```",
            r"```(?:\w+)?\s*([\s\S]+?)\s*```",
        ):
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        # No fence found — return trimmed response if it looks like code
        stripped = text.strip()
        if stripped:
            return stripped
        return ""

    @staticmethod
    def _is_blocked(authority_level: str, risk: str) -> bool:
        """Return True if this risk level should be blocked under authority_level."""
        if authority_level == "full_confirm":
            return True
        if authority_level == "confirm_writes" and risk in ("high", "critical"):
            return True
        if authority_level == "auto" and risk == "critical":
            return True
        return False
