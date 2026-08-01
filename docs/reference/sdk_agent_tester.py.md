# File: `tester.py`

## Module Description

sdk/agent/tester.py
==================
Self-Healing Testing & Iterative Debugger — Tala Cognitive Engine Room
Milestone 3 | Tasks 3.3, 3.4, 3.5, 3.6, 3.7

Provides tools to generate test templates, parse terminal build/test logs
for error diagnostics, map TypeScript/compiler errors, and run an automated
self-healing test-and-repair loop.

## Classes

### `class TestGenerator`
Generates test files (Jest or pytest) for target modules.

- **`def generate_test_suite(target_file, file_type, class_name, functions)`**
  - Generate test suite boilerplate.

### `class BuildDiagnosis`
Parses compiler and terminal outputs to isolate failing lines and tracebacks.

- **`def diagnose_failure(console_output)`**
  - Parse console output to extract:
  - error_file: Path of the file causing the build error.
  - error_line: Line number where error occurred.
  - message: Direct compiler message.
  - type: 'typescript', 'python', 'jest', or 'generic'.

### `class TypeScriptErrorRepair`
TypeScript compiler error diagnostic and repair module.

- **`def suggest_ts_fix(error_code, message)`**
  - Suggest TS bug-fixes based on compiler warnings.

### `class IterativeDebugger`
Self-healing edit-compile-test-repair loop.

- **`def __init__(self, run_cmd, cwd)`**
- **`def debug_loop(self, target_file, edit_action, max_attempts)`**
  - Iterative compile/test loop that executes the run command, diagnoses
  failures, and triggers the edit_action patcher until success.
  
  Parameters
  ----------
  target_file:
      Absolute or relative path to the file under repair.
  edit_action:
      Callable taking (file_content, failure_diagnostics) -> new_content.

## Source Code

```python
"""
sdk/agent/tester.py
==================
Self-Healing Testing & Iterative Debugger — Tala Cognitive Engine Room
Milestone 3 | Tasks 3.3, 3.4, 3.5, 3.6, 3.7

Provides tools to generate test templates, parse terminal build/test logs
for error diagnostics, map TypeScript/compiler errors, and run an automated
self-healing test-and-repair loop.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from sdk.agent.coder import CodeRepair, ImplementationPromptWriter

log = logging.getLogger(__name__)


class TestGenerator:
    """
    Generates test files (Jest or pytest) for target modules.
    """

    @staticmethod
    def generate_test_suite(target_file: str, file_type: str, class_name: str, functions: list[str]) -> str:
        """
        Generate test suite boilerplate.
        """
        module_name = Path(target_file).stem
        
        if file_type == "pytest":
            test_funcs = ""
            for func in functions:
                test_funcs += f"\n\ndef test_{module_name}_{func}():\n    # TODO: implement test for {func}\n    pass\n"
            return f"""from {module_name} import {class_name}\n{test_funcs}"""
            
        elif file_type == "jest":
            test_cases = ""
            for func in functions:
                test_cases += f"    test('{func} should work', () => {{\n        // TODO: implement test\n    }});\n\n"
            return f"""import {{ {class_name} }} from './{module_name}';\n\ndescribe('{class_name} Suite', () => {{\n{test_cases}}});\n"""
            
        else:
            raise ValueError(f"Unsupported test type: {file_type}")


class BuildDiagnosis:
    """
    Parses compiler and terminal outputs to isolate failing lines and tracebacks.
    """

    @staticmethod
    def diagnose_failure(console_output: str) -> dict[str, Any]:
        """
        Parse console output to extract:
        - error_file: Path of the file causing the build error.
        - error_line: Line number where error occurred.
        - message: Direct compiler message.
        - type: 'typescript', 'python', 'jest', or 'generic'.
        """
        diag = {
            "error_file": None,
            "error_line": None,
            "message": "",
            "type": "generic",
        }

        # 1. Check for TypeScript compiler errors (e.g. error TS2304: Cannot find name 'x')
        ts_match = re.search(r"([a-zA-Z0-9_\-\./]+)\((\d+),(\d+)\): error (TS\d+): (.*)", console_output)
        if ts_match:
            diag["error_file"] = ts_match.group(1)
            diag["error_line"] = int(ts_match.group(2))
            diag["message"] = ts_match.group(5)
            diag["type"] = "typescript"
            return diag

        # 2. Check for Python Traceback (e.g. File "x.py", line 12, in y)
        py_match = re.findall(r'File "([^"]+)", line (\d+), in (\w+)', console_output)
        if py_match:
            # Take the last traceback line (deepest failure)
            file, line, func = py_match[-1]
            diag["error_file"] = file
            diag["error_line"] = int(line)
            # Find the actual exception name at the bottom of traceback
            lines = console_output.strip().splitlines()
            if lines:
                diag["message"] = lines[-1]
            diag["type"] = "python"
            return diag

        # 3. Check for Jest test failure logs
        jest_match = re.search(r"FAIL\s+([a-zA-Z0-9_\-\./]+)\s*\n.*●\s+.*", console_output)
        if jest_match:
            diag["error_file"] = jest_match.group(1)
            diag["type"] = "jest"
            # Attempt to extract line number (e.g. at Object.<anonymous> (x.test.js:14:5))
            line_match = re.search(r"\(([^:]+):(\d+):(\d+)\)", console_output)
            if line_match:
                diag["error_line"] = int(line_match.group(2))
            return diag

        return diag


class TypeScriptErrorRepair:
    """
    TypeScript compiler error diagnostic and repair module.
    """

    @staticmethod
    def suggest_ts_fix(error_code: str, message: str) -> str:
        """
        Suggest TS bug-fixes based on compiler warnings.
        """
        if "TS2304" in error_code or "Cannot find name" in message:
            return "Import or declare the missing variable/class name."
        if "TS2339" in error_code or "Property" in message and "does not exist on type" in message:
            return "Declare property on interface/class or cast object to 'any'."
        if "TS2322" in error_code or "Type" in message and "is not assignable to type" in message:
            return "Update variable type, match signature return type, or cast type."
        return "Verify types and import statements."


class IterativeDebugger:
    """
    Self-healing edit-compile-test-repair loop.
    """

    def __init__(self, run_cmd: str, cwd: str) -> None:
        self.run_cmd = run_cmd
        self.cwd = cwd

    def debug_loop(
        self,
        target_file: str,
        edit_action: Callable[[str, dict[str, Any]], str],
        max_attempts: int = 3,
    ) -> bool:
        """
        Iterative compile/test loop that executes the run command, diagnoses
        failures, and triggers the edit_action patcher until success.
        
        Parameters
        ----------
        target_file:
            Absolute or relative path to the file under repair.
        edit_action:
            Callable taking (file_content, failure_diagnostics) -> new_content.
        """
        abs_path = os.path.join(self.cwd, target_file)
        
        for attempt in range(1, max_attempts + 1):
            log.info("IterativeDebugger: Attempt %d/%d to execute: %s", attempt, max_attempts, self.run_cmd)
            
            # Execute compiler/test command
            res = subprocess.run(
                self.run_cmd,
                shell=True,
                cwd=self.cwd,
                capture_output=True,
                text=True,
            )
            
            if res.returncode == 0:
                log.info("IterativeDebugger: success! Test/build compiled and passed cleanly.")
                return True
                
            # Compile or tests failed — diagnose console output
            console = res.stdout + "\n" + res.stderr
            diag = BuildDiagnosis.diagnose_failure(console)
            diag["console_raw"] = console
            
            log.warning(
                "IterativeDebugger: Build failed. Diagnosed file=%s, line=%s, message='%s'",
                diag["error_file"], diag["error_line"], diag["message"]
            )
            
            # Read current content and invoke patcher
            try:
                content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                patched_content = edit_action(content, diag)
                
                if patched_content == content:
                    log.warning("IterativeDebugger: Edit action produced no modifications. Stopping debugger.")
                    return False
                    
                Path(abs_path).write_text(patched_content, encoding="utf-8")
            except Exception as e:
                log.error("IterativeDebugger: Failed applying surgical patch: %s", e)
                return False

        log.error("IterativeDebugger: Failed to resolve build after %d attempts.", max_attempts)
        return False

```
