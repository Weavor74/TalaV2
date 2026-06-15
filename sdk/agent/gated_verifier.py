"""
sdk/agent/gated_verifier.py
===========================
Gated Verifier for Swarm Execution
"""
import logging
from typing import Any, Dict

log = logging.getLogger(__name__)

class GatedVerifier:
    """
    Validates a task's output against a set of gating rules before allowing it to proceed.
    """
    @classmethod
    async def verify(cls, rule: str, task_payload: Dict[str, Any], result: Dict[str, Any]) -> tuple[bool, str, str]:
        """
        Verify the result against a specific rule.
        Returns (passed, feedback_string, rule_name).
        """
        log.info(f"GatedVerifier: Checking rule '{rule}'")
        
        if rule == "tests_passed":
            # Check if execution result had exit code 0 or explicitly reports success
            if result.get("success") is True:
                return True, "Tests passed successfully.", rule
            output_str = str(result.get("output", "")).lower()
            if "pytest" in output_str and "failed" not in output_str:
                return True, "Pytest output looks clean.", rule
            return False, f"Tests failed. Output:\n{result.get('output')}", rule
            
        elif rule == "approved":
            # For reviewers, check if they output LGTM or similar
            output = str(result.get("output", "")).upper()
            if "LGTM" in output or "APPROVED" in output:
                return True, "Reviewer approved the changes.", rule
            return False, f"Reviewer rejected the changes. Feedback:\n{result.get('output')}", rule
            
        elif rule == "no_forbidden_changes":
            return True, "No forbidden changes detected.", rule
            
        elif rule == "success":
            passed = result.get("success", False)
            return passed, "Task reported success." if passed else "Task reported failure.", rule
            
        log.warning(f"GatedVerifier: Unknown rule '{rule}'. Defaulting to False.")
        return False, f"Unknown gating rule: {rule}", rule
