"""
sdk/agent/validator.py
======================
Verifier layer for evaluating execution phase results.
Separates stdout checks, file-content checks, stderr checks,
return-code checks, and semantic checks.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

class VerificationResult:
    def __init__(self, success: bool, output: Any = None, error: str | None = None):
        self.success = success
        self.output = output
        self.error = error

class BaseCheck:
    def verify(self, task: Any, res: dict[str, Any], context: dict[str, Any]) -> VerificationResult | None:
        """
        Return VerificationResult if check modifies the outcome.
        Return None to proceed to the next check without modification.
        """
        raise NotImplementedError

class ReturnCodeCheck(BaseCheck):
    def verify(self, task: Any, res: dict[str, Any], context: dict[str, Any]) -> VerificationResult | None:
        # Implicitly handled by res.get("success"), but could be made explicit here
        return None

class ExpectedFailureCheck(BaseCheck):
    def verify(self, task: Any, res: dict[str, Any], context: dict[str, Any]) -> VerificationResult | None:
        repair_policy = getattr(task, "payload", {}).get("repair_policy", {})
        expected_failure = repair_policy.get("expected_failure_string")
        
        stdout_str = context.get("stdout_str", "")
        stderr_str = context.get("stderr_str", "")
        
        if expected_failure and (expected_failure in stdout_str or expected_failure in stderr_str):
            log.info("PhaseValidator: Bug Phase observed expected failure '%s'", expected_failure)
            return VerificationResult(
                success=True,
                output=f"[Bug Phase Success] Observed expected failure: {expected_failure}\n{stdout_str}\n{stderr_str}",
                error=None
            )
        return None

class StdoutCheck(BaseCheck):
    def verify(self, task: Any, res: dict[str, Any], context: dict[str, Any]) -> VerificationResult | None:
        phase_success = context.get("phase_success", False)
        expected_stdout = getattr(task, "payload", {}).get("expected_stdout")
        stdout_str = context.get("stdout_str", "")
        
        if phase_success and expected_stdout:
            norm_stdout = stdout_str.replace('\r\n', '\n')
            missing_parts = []
            for part in expected_stdout.replace('\r\n', '\n').split('\n'):
                part = part.strip()
                if part and part not in norm_stdout:
                    missing_parts.append(part)
            
            if missing_parts:
                return VerificationResult(
                    success=False,
                    output=res.get("output"),
                    error=f"Semantic Verification Failed: Expected standard output to contain these missing lines: {missing_parts}\nActual output: {stdout_str}"
                )
        return None

class FileContentCheck(BaseCheck):
    def verify(self, task: Any, res: dict[str, Any], context: dict[str, Any]) -> VerificationResult | None:
        # Placeholder for explicit file content verification
        return None

class StderrCheck(BaseCheck):
    def verify(self, task: Any, res: dict[str, Any], context: dict[str, Any]) -> VerificationResult | None:
        # Placeholder for explicit stderr checking rules
        return None

class SemanticCheck(BaseCheck):
    def verify(self, task: Any, res: dict[str, Any], context: dict[str, Any]) -> VerificationResult | None:
        # Placeholder for future LLM-based semantic checks
        return None

class PhaseValidator:
    """Orchestrates the sequence of verification checks on an execution result."""
    
    def __init__(self):
        # The order of checks matters. ExpectedFailureCheck can flip a failure to a success.
        self.checks: list[BaseCheck] = [
            ExpectedFailureCheck(),
            ReturnCodeCheck(),
            StdoutCheck(),
            StderrCheck(),
            FileContentCheck(),
            SemanticCheck()
        ]

    def validate(self, task: Any, res: dict[str, Any]) -> VerificationResult:
        """
        Validates the phase result using all configured checks.
        Mutates and returns a VerificationResult.
        """
        phase_success = res.get("success", False)
        output = res.get("output")
        error = res.get("error")
        
        action_taken = res.get("action_taken", "")
        is_execution = action_taken in ("run_python_file", "shell", "route_shell", "execute_shell", "run_pytest")
        
        if not is_execution:
            return VerificationResult(success=phase_success, output=output, error=error)
            
        stdout_str = str(output or "")
        stderr_str = str(error or "")
        
        context = {
            "stdout_str": stdout_str,
            "stderr_str": stderr_str,
            "phase_success": phase_success
        }
        
        current_result = VerificationResult(success=phase_success, output=output, error=error)
        
        for check in self.checks:
            context["phase_success"] = current_result.success
            check_res = check.verify(task, res, context)
            if check_res is not None:
                current_result = check_res
                
        return current_result
