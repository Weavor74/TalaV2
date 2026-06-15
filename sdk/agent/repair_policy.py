"""
sdk/agent/repair_policy.py
==========================
Explicit, reusable, and phase-aware engine for generating error repair policies.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RepairContext:
    """Encapsulates the state and payload of a failed phase to generate a repair policy."""
    task_description: str
    error_msg: str
    payload: dict[str, Any] = field(default_factory=dict)
    
    @property
    def intentional_runtime_error(self) -> bool:
        return bool(self.payload.get("intentional_runtime_error"))

    @property
    def intentional_syntax_error(self) -> bool:
        return bool(self.payload.get("intentional_syntax_error"))
        
    @property
    def repair_policy_obj(self) -> dict[str, Any]:
        return self.payload.get("repair_policy", {})

    @property
    def target_file(self) -> str | None:
        return self.payload.get("context", {}).get("path")

    @property
    def has_syntax_or_compile_error(self) -> bool:
        error_str = str(self.error_msg)
        return "SyntaxError" in error_str or "py_compile" in error_str


class RepairPolicyEngine:
    """
    Evaluates a RepairContext against rules to generate concrete instructions for the replanner.
    """
    
    @staticmethod
    def generate_instruction(context: RepairContext) -> str:
        instructions: list[str] = []
        
        # 1. Intentional Error Rules
        if context.intentional_syntax_error:
            if context.has_syntax_or_compile_error:
                instructions.append(
                    "Repair policy:\n"
                    "- The syntax error was intentionally observed as part of the test goal.\n"
                    "- Now repair the root cause directly to satisfy the overall goal.\n"
                    "- Do NOT preserve the syntax error anymore. Correct it properly.\n"
                )
            else:
                instructions.append(
                    "CRITICAL: The original task requested an intentional SYNTAX ERROR. "
                    "You MUST preserve this intentional broken syntax in your repair action, but fix the current execution error."
                )
        elif context.intentional_runtime_error:
            if context.has_syntax_or_compile_error:
                instructions.append(
                    "CRITICAL: The original task requested an intentional RUNTIME ERROR or typo (e.g. invalid name). "
                    "You MUST preserve this intentional mistake, but ensure the code is valid Python SYNTAX. DO NOT auto-correct the runtime bug."
                )
            else:
                instructions.append(
                    "Repair policy:\n"
                    "- The runtime error was intentionally observed as part of the test goal.\n"
                    "- Now repair the root cause directly to satisfy the overall goal.\n"
                    "- Do NOT preserve the misspelled call or bug. Correct it properly.\n"
                    "- Do NOT define a shim function, wrapper, or alias just to keep the typo around.\n"
                )
        
        # 2. Repair Constraints
        if context.intentional_syntax_error or context.intentional_runtime_error:
            repair_obj = context.repair_policy_obj
            if repair_obj:
                forbidden = repair_obj.get("forbidden_repairs", [])
                final_out = repair_obj.get("final_expected_stdout", "")
                
                if forbidden or final_out:
                    instructions.append("\nCRITICAL REPAIR CONSTRAINTS:")
                    if forbidden:
                        instructions.append(f"- Forbidden actions: {', '.join(forbidden)}")
                    if final_out:
                        instructions.append(f"- Final script MUST output: '{final_out}'. You MUST specify this in the 'expected_stdout' field of the execution sub-task.")
        
        repair_obj = context.repair_policy_obj or {}
        allow_workarounds = repair_obj.get("allow_workarounds", False)
        allow_target_change = repair_obj.get("allow_target_change", False)

        # 3. Global Constraints
        if not allow_workarounds:
            instructions.append(
                "\nGLOBAL CONSTRAINTS:\n"
                "- Do NOT write a 'workaround' or 'temporary script'.\n"
                "- Do NOT 'circumvent' or 'bypass' the root cause; fix it directly.\n"
            )
        
        # 4. Target File Constraints
        if context.target_file and not allow_target_change:
            instructions.append(f"- You MUST edit the existing file '{context.target_file}'. Do NOT create a new script file or change the target file.\n")
            
        # 5. Timeout Rules
        if "timed out after" in str(context.error_msg).lower():
            instructions.append(
                "CRITICAL: This is a runtime tool execution timeout, NOT a script bug. "
                "Do NOT mutate the target script to fix this unless standard output explicitly proves the script is looping. "
                "It is likely an environment or argument error."
            )
            
        return "\n".join(instructions)
