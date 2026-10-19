"""
NAME
       self_test.py - repeatable self-test suite for the Tala Cognitive Engine Room

SYNOPSIS
       python scripts/tala_self_test.py [--no-audit]
       npm run self-test
       /self-test [--no-audit]  (from Tala chat)

DESCRIPTION
       Runs a fixed sequence of health checks against the current checkout and
       reports a structured JSON result. Each step is run as a subprocess so
       failures are isolated and clearly attributed. Steps executed (in order):

       1. python dependency import smoke — imports asyncpg, websockets, mcp,
          httpx_sse, and pytest to confirm the virtualenv is correctly populated.
       2. python syntax compile — py_compile checks on server.py, self_test.py,
          sdk/agent/command_parser.py, and sdk/agent/recovery.py.
       3. renderer syntax check — node --check on renderer/renderer.js.
       4. electron syntax check — node --check on electron/main.js.
       5. python tests — pytest over tests/ (excluding tests/benchmarks).
       6. javascript tests — jest --runInBand. A broken testMatch now fails
          the step instead of "passing" with zero tests collected.
       7. npm audit (optional) — npm audit --audit-level=moderate.

       Options:
         --no-audit    Skip step 7 (npm audit) for a faster inner-loop run.

       Exit codes:
         0   All steps passed.
         1   One or more steps failed (JSON result contains per-step details).

SEE ALSO
       scripts/first_run.py(1), INSTALL.md, RELEASE_CHECKLIST.md
"""

from __future__ import annotations

import json
import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


ROOT_DIR = Path(__file__).resolve().parents[2]


@dataclass
class SelfTestStep:
    name: str
    command: list[str]
    exit_code: int
    duration_seconds: float
    passed: bool
    stdout: str
    stderr: str


def _run_step(name: str, command: Sequence[str], timeout: float) -> SelfTestStep:
    started = time.perf_counter()
    resolved_command = list(command)
    executable = shutil.which(resolved_command[0])
    if executable:
        resolved_command[0] = executable
    try:
        completed = subprocess.run(
            resolved_command,
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        # TimeoutExpired may carry bytes even with text=True. Keep the report
        # serializable and preserve the last named test instead of crashing.
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        stderr += f"\nTimed out after {timeout:.0f}s"

    duration = time.perf_counter() - started
    return SelfTestStep(
        name=name,
        command=resolved_command,
        exit_code=exit_code,
        duration_seconds=round(duration, 2),
        passed=exit_code == 0,
        stdout=stdout[-6000:],
        stderr=stderr[-6000:],
    )


def _build_self_test_plan(include_audit: bool = True) -> list[tuple[str, list[str], float]]:
    python_exe = sys.executable
    plan: list[tuple[str, list[str], float]] = [
        (
            "python dependency import smoke",
            [
                python_exe,
                "-c",
                "import asyncpg, websockets, mcp, httpx_sse, pytest; print('python deps ok')",
            ],
            30.0,
        ),
        (
            "python syntax compile",
            [
                python_exe,
                "-m",
                "py_compile",
                "python/harness/server.py",
                "python/harness/self_test.py",
                "sdk/agent/command_parser.py",
                "sdk/agent/recovery.py",
            ],
            60.0,
        ),
        ("renderer syntax check", ["node", "--check", "renderer/renderer.js"], 30.0),
        ("electron syntax check", ["node", "--check", "electron/main.js"], 30.0),
        (
            "python tests",
            [python_exe, "-u", "-m", "pytest", "tests", "--ignore=tests/benchmarks",
             f"--basetemp={os.path.join(tempfile.gettempdir(), 'tala-pytest')}", "-v", "--tb=short", "--durations=10"],
            600.0,
        ),
        ("javascript tests", ["npx", "jest", "--runInBand"], 120.0),
    ]
    if include_audit:
        plan.append(("npm audit", ["npm", "audit", "--audit-level=moderate"], 120.0))
    return plan


def run_self_test(include_audit: bool = True) -> dict[str, object]:
    steps = [
        _run_step(name, command, timeout)
        for name, command, timeout in _build_self_test_plan(include_audit=include_audit)
    ]
    passed = all(step.passed for step in steps)
    return {
        "success": passed,
        "status": "passed" if passed else "failed",
        "root_dir": str(ROOT_DIR),
        "steps": [asdict(step) for step in steps],
        "summary": {
            "passed": sum(1 for step in steps if step.passed),
            "failed": sum(1 for step in steps if not step.passed),
            "total": len(steps),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Tala's repeatable self-test suite.")
    parser.add_argument("--no-audit", action="store_true", help="Skip npm audit for a faster inner loop.")
    args = parser.parse_args()
    result = run_self_test(include_audit=not args.no_audit)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
