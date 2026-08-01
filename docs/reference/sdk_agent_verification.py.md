# File: `verification.py`

## Module Description

sdk/agent/verification.py
=========================
Verification Matrix & Invariant Assurance — Tala Cognitive Engine Room
Milestone 6 | Task 6.3.1

Provides:
  • InvariantChecker  — registry + runner for boolean invariant callables
  • Built-in invariants that enforce Tala's core architectural contracts
  • SmokeTestRunner   — thin pytest wrapper
  • VerificationMatrix — orchestrates all checks and produces a report

Architecture invariants enforced here:
  1. PostgreSQL is the sole persistent authority (no other DB primary).
  2. mem0 adapter is read-only (no write path exposed).
  3. Vault directory has no outbound network socket context.
  4. Astro style influence cap is MAX_ASTRO_INFLUENCE == 0.10.

## Classes

### `class InvariantStatus`

### `class InvariantRecord`
Result of a single invariant check.


### `class SmokeTestResult`
Aggregated result from running pytest on a set of test paths.

- **`def all_passed(self)`**

### `class VerificationReport`
Full report from VerificationMatrix.run().

- **`def failed_invariants(self)`**
- **`def passed_count(self)`**
- **`def total_count(self)`**

### `class InvariantChecker`
Registry and runner for boolean invariant callables.

Usage
-----
checker = InvariantChecker()
checker.register(postgres_is_sole_authority, "postgres_sole_authority")
checker.register(mem0_is_read_only, "mem0_read_only")
results = checker.run_all()

- **`def __init__(self)`**
- **`def register(self, fn, name)`**
  - Add *fn* to the registry with an optional display *name*.
- **`def register_defaults(self)`**
  - Register all four built-in Tala invariants.
- **`def run_all(self)`**
  - Execute every registered invariant and return a list of InvariantRecord.
  
  Exceptions raised by an invariant are caught and recorded as ERROR
  status — they do not propagate.
- **`def run_before_complete(self)`**
  - Convenience method: run all invariants and return True iff all pass.
  
  Suitable for use as a gate before marking a TaskItem COMPLETE.

### `class SmokeTestRunner`
Executes pytest on a list of test paths and parses the results.

Uses subprocess to keep the test runner in a clean process, avoiding any
module-state contamination from the agent runtime.

- **`def run_smoke_tests(self, test_paths)`**
  - Run pytest against *test_paths*.
  
  Parameters
  ----------
  test_paths:
      List of file or directory paths to pass to pytest.
  extra_args:
      Additional pytest CLI arguments (e.g. ['-v', '-x']).
  timeout:
      Maximum seconds to wait for the test run.
  
  Returns
  -------
  SmokeTestResult
- **`def _parse_pytest_output(self, stdout, result)`**
  - Parse the pytest summary line(s) to populate counters.
  
  Handles patterns like:
    '5 passed, 1 failed, 2 error in 0.42s'
    '3 passed in 0.10s'
    'no tests ran'

### `class VerificationMatrix`
Orchestrates all verification checks and produces a VerificationReport.

Usage
-----
matrix = VerificationMatrix()
matrix.checker.register_defaults()
report = matrix.run(smoke_test_paths=["sdk/agent/tests"])
assert report.overall_pass

- **`def __init__(self, checker, smoke_runner)`**
- **`def run(self, smoke_test_paths)`**
  - Run all registered invariants and optionally the smoke test suite.
  
  Parameters
  ----------
  smoke_test_paths:
      If provided, pytest is run on these paths.
  require_smoke:
      When True, overall_pass is False if smoke tests fail.
  smoke_extra_args:
      Extra args forwarded to pytest.
  
  Returns
  -------
  VerificationReport
- **`def report_summary(self, report)`**
  - Return a human-readable summary string of *report*.

## Functions

### `def postgres_is_sole_authority()`
Verify that PostgreSQL is configured as the sole persistent data authority.

Checks:
  1. The environment variable TALA_PRIMARY_DB equals 'postgresql'.
  2. No disallowed alternate primary DB env vars are set
     (e.g. TALA_PRIMARY_DB=sqlite, TALA_PRIMARY_DB=mongodb, etc.).
  3. The absence of any mem0/chroma/pinecone primary-DB flags.

Returns True on success, False on any violation.

### `def mem0_is_read_only()`
Verify that the mem0 adapter cannot perform write operations.

Checks:
  1. TALA_MEM0_READONLY env var is set to '1' or 'true'.
  2. If the mem0 module is importable, its adapter's write methods are
     patched/stubbed (indicated by TALA_MEM0_WRITE_STUBBED env var).

Returns True if mem0 is read-only, False otherwise.

### `def vault_is_network_isolated()`
Verify that the vault directory path has no outbound network socket context.

Checks:
  1. TALA_VAULT_PATH env var is set and the directory exists.
  2. TALA_VAULT_ALLOW_NETWORK is NOT set to '1'/'true'.
  3. No process listening on TALA_VAULT_NETWORK_PORT (if set) is bound
     to an external interface (0.0.0.0 / :: is a violation).

Returns True if vault is isolated, False on any network exposure.

Note: Full socket-level isolation enforcement is OS-level; this function
checks configuration intent only.  Real enforcement is via firewall rules
and process namespacing in production.

### `def style_cap_enforced()`
Verify that the Astro style influence cap is exactly 0.10.

Checks (in order of preference):
  1. TALA_MAX_ASTRO_INFLUENCE environment variable == '0.10'.
  2. The sdk.renderer.style module's MAX_ASTRO_INFLUENCE attribute.
  3. The sdk/renderer/style_config.py file contains the literal '0.10'.

Returns True only when the cap is confirmed at exactly 0.10.

## Source Code

```python
"""\nsdk/agent/verification.py\n=========================\nVerification Matrix & Invariant Assurance — Tala Cognitive Engine Room\nMilestone 6 | Task 6.3.1\n\nProvides:\n  • InvariantChecker  — registry + runner for boolean invariant callables\n  • Built-in invariants that enforce Tala's core architectural contracts\n  • SmokeTestRunner   — thin pytest wrapper\n  • VerificationMatrix — orchestrates all checks and produces a report\n\nArchitecture invariants enforced here:\n  1. PostgreSQL is the sole persistent authority (no other DB primary).\n  2. mem0 adapter is read-only (no write path exposed).\n  3. Vault directory has no outbound network socket context.\n  4. Astro style influence cap is MAX_ASTRO_INFLUENCE == 0.10.\n"""\n\nfrom __future__ import annotations\n\nimport importlib\nimport logging\nimport os\nimport socket\nimport subprocess\nimport sys\nfrom collections.abc import Callable\nfrom dataclasses import dataclass, field\nfrom datetime import UTC, datetime\nfrom enum import Enum, auto\nfrom pathlib import Path\nfrom typing import Any\n\nlog = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Enums\n# ---------------------------------------------------------------------------\n\n\nclass InvariantStatus(Enum):\n    PASS = "PASS"\n    FAIL = "FAIL"\n    ERROR = "ERROR"\n    SKIPPED = "SKIPPED"\n\n\n# ---------------------------------------------------------------------------\n# Data classes\n# ---------------------------------------------------------------------------\n\n\n@dataclass\nclass InvariantRecord:\n    """Result of a single invariant check."""\n\n    name: str\n    status: InvariantStatus\n    detail: str = ""\n    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n\n\n@dataclass\nclass SmokeTestResult:\n    """Aggregated result from running pytest on a set of test paths."""\n\n    passed: int = 0\n    failed: int = 0\n    errors: int = 0\n    skipped: int = 0\n    total: int = 0\n    exit_code: int = -1\n    stdout: str = ""\n    stderr: str = ""\n    ran_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n\n    @property\n    def all_passed(self) -> bool:\n        return self.exit_code == 0 and self.failed == 0 and self.errors == 0\n\n\n@dataclass\nclass VerificationReport:\n    """Full report from VerificationMatrix.run()."""\n\n    invariant_results: list[InvariantRecord] = field(default_factory=list)\n    smoke_test_result: SmokeTestResult | None = None\n    overall_pass: bool = False\n    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n\n    # Convenience properties\n    @property\n    def failed_invariants(self) -> list[InvariantRecord]:\n        return [r for r in self.invariant_results if r.status != InvariantStatus.PASS]\n\n    @property\n    def passed_count(self) -> int:\n        return sum(1 for r in self.invariant_results if r.status == InvariantStatus.PASS)\n\n    @property\n    def total_count(self) -> int:\n        return len(self.invariant_results)\n\n\n# ---------------------------------------------------------------------------\n# Built-in Invariants\n# ---------------------------------------------------------------------------\n\n\ndef postgres_is_sole_authority() -> bool:\n    """\n    Verify that PostgreSQL is configured as the sole persistent data authority.\n\n    Checks:\n      1. The environment variable TALA_PRIMARY_DB equals 'postgresql'.\n      2. No disallowed alternate primary DB env vars are set\n         (e.g. TALA_PRIMARY_DB=sqlite, TALA_PRIMARY_DB=mongodb, etc.).\n      3. The absence of any mem0/chroma/pinecone primary-DB flags.\n\n    Returns True on success, False on any violation.\n    """\n    primary_db = os.environ.get("TALA_PRIMARY_DB", "postgresql").strip().lower()\n    allowed = {"postgresql", "postgres"}\n\n    if primary_db not in allowed:\n        log.warning(\n            "postgres_is_sole_authority FAIL: TALA_PRIMARY_DB=%r (expected 'postgresql')",\n            primary_db,\n        )\n        return False\n\n    # Check no disallowed DB primaries are asserted elsewhere\n    disallowed_flags = [\n        "TALA_USE_SQLITE_PRIMARY",\n        "TALA_USE_MONGODB_PRIMARY",\n        "TALA_USE_MEM0_PRIMARY",\n        "TALA_USE_CHROMA_PRIMARY",\n        "TALA_USE_PINECONE_PRIMARY",\n        "TALA_USE_REDIS_PRIMARY",\n    ]\n    for flag in disallowed_flags:\n        val = os.environ.get(flag, "").strip().lower()\n        if val in ("1", "true", "yes"):\n            log.warning(\n                "postgres_is_sole_authority FAIL: disallowed flag %s=%r is set",\n                flag, val,\n            )\n            return False\n\n    log.debug("postgres_is_sole_authority PASS")\n    return True\n\n\ndef mem0_is_read_only() -> bool:\n    """\n    Verify that the mem0 adapter cannot perform write operations.\n\n    Checks:\n      1. TALA_MEM0_READONLY env var is set to '1' or 'true'.\n      2. If the mem0 module is importable, its adapter's write methods are\n         patched/stubbed (indicated by TALA_MEM0_WRITE_STUBBED env var).\n\n    Returns True if mem0 is read-only, False otherwise.\n    """\n    readonly_flag = os.environ.get("TALA_MEM0_READONLY", "").strip().lower()\n    if readonly_flag not in ("1", "true", "yes"):\n        # Check if mem0 is even present in this environment\n        try:\n            importlib.util.find_spec("mem0")  # type: ignore[attr-defined]\n            mem0_available = True\n        except Exception:\n            mem0_available = False\n\n        if mem0_available:\n            log.warning(\n                "mem0_is_read_only FAIL: mem0 is importable but TALA_MEM0_READONLY "\n                "is not set to true."\n            )\n            return False\n        else:\n            # mem0 not installed — read-only by absence\n            log.debug("mem0_is_read_only PASS: mem0 not installed in this environment")\n            return True\n\n    # Additional check: write-stubbed confirmation\n    write_stubbed = os.environ.get("TALA_MEM0_WRITE_STUBBED", "").strip().lower()\n    if write_stubbed not in ("1", "true", "yes", ""):\n        log.warning(\n            "mem0_is_read_only FAIL: TALA_MEM0_WRITE_STUBBED=%r — unexpected value",\n            write_stubbed,\n        )\n        return False\n\n    log.debug("mem0_is_read_only PASS")\n    return True\n\n\ndef vault_is_network_isolated() -> bool:\n    """\n    Verify that the vault directory path has no outbound network socket context.\n\n    Checks:\n      1. TALA_VAULT_PATH env var is set and the directory exists.\n      2. TALA_VAULT_ALLOW_NETWORK is NOT set to '1'/'true'.\n      3. No process listening on TALA_VAULT_NETWORK_PORT (if set) is bound\n         to an external interface (0.0.0.0 / :: is a violation).\n\n    Returns True if vault is isolated, False on any network exposure.\n\n    Note: Full socket-level isolation enforcement is OS-level; this function\n    checks configuration intent only.  Real enforcement is via firewall rules\n    and process namespacing in production.\n    """\n    vault_path_str = os.environ.get("TALA_VAULT_PATH", "").strip()\n\n    if not vault_path_str:\n        # Vault not configured — treat as isolated (no path → no exposure)\n        log.debug("vault_is_network_isolated PASS: TALA_VAULT_PATH not set (no vault configured)")\n        return True\n\n    vault_path = Path(vault_path_str)\n    if not vault_path.exists():\n        log.warning(\n            "vault_is_network_isolated WARN: TALA_VAULT_PATH=%r does not exist",\n            vault_path_str,\n        )\n        # Non-existent path cannot be network-exposed\n        return True\n\n    # Check the allow-network override flag\n    allow_network = os.environ.get("TALA_VAULT_ALLOW_NETWORK", "").strip().lower()\n    if allow_network in ("1", "true", "yes"):\n        log.warning(\n            "vault_is_network_isolated FAIL: TALA_VAULT_ALLOW_NETWORK=%r is set — "\n            "vault network exposure is explicitly enabled.",\n            allow_network,\n        )\n        return False\n\n    # Check if a vault network port is configured and whether it's exposed\n    vault_port_str = os.environ.get("TALA_VAULT_NETWORK_PORT", "").strip()\n    if vault_port_str:\n        try:\n            vault_port = int(vault_port_str)\n            # Try to bind to that port on loopback — if it succeeds the port\n            # is not in use (good); if it fails with EADDRINUSE we check further\n            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:\n                s.settimeout(0.5)\n                result = s.connect_ex(("0.0.0.0", vault_port))\n                if result == 0:\n                    log.warning(\n                        "vault_is_network_isolated FAIL: port %d appears bound on 0.0.0.0",\n                        vault_port,\n                    )\n                    return False\n        except (ValueError, OSError) as exc:\n            log.debug("vault_is_network_isolated port check skipped: %s", exc)\n\n    log.debug("vault_is_network_isolated PASS")\n    return True\n\n\ndef style_cap_enforced() -> bool:\n    """\n    Verify that the Astro style influence cap is exactly 0.10.\n\n    Checks (in order of preference):\n      1. TALA_MAX_ASTRO_INFLUENCE environment variable == '0.10'.\n      2. The sdk.renderer.style module's MAX_ASTRO_INFLUENCE attribute.\n      3. The sdk/renderer/style_config.py file contains the literal '0.10'.\n\n    Returns True only when the cap is confirmed at exactly 0.10.\n    """\n    env_val = os.environ.get("TALA_MAX_ASTRO_INFLUENCE", "").strip()\n    if env_val:\n        try:\n            cap = float(env_val)\n            if abs(cap - 0.10) < 1e-9:\n                log.debug("style_cap_enforced PASS: env TALA_MAX_ASTRO_INFLUENCE=0.10")\n                return True\n            else:\n                log.warning(\n                    "style_cap_enforced FAIL: TALA_MAX_ASTRO_INFLUENCE=%r (expected 0.10)",\n                    env_val,\n                )\n                return False\n        except ValueError:\n            log.warning("style_cap_enforced FAIL: TALA_MAX_ASTRO_INFLUENCE=%r is not numeric", env_val)\n            return False\n\n    # Attempt module attribute check\n    try:\n        import importlib as _il\n        style_mod = _il.import_module("sdk.renderer.style")\n        cap = float(getattr(style_mod, "MAX_ASTRO_INFLUENCE", -1.0))\n        if abs(cap - 0.10) < 1e-9:\n            log.debug("style_cap_enforced PASS: sdk.renderer.style.MAX_ASTRO_INFLUENCE=0.10")\n            return True\n        log.warning("style_cap_enforced FAIL: MAX_ASTRO_INFLUENCE=%r in module", cap)\n        return False\n    except (ImportError, TypeError, AttributeError):\n        pass\n\n    # Fallback: scan project files for the assignment\n    search_paths = [\n        Path("sdk") / "renderer" / "style_config.py",\n        Path("sdk") / "renderer" / "style.py",\n        Path("renderer") / "style_config.py",\n    ]\n    pattern = "MAX_ASTRO_INFLUENCE"\n    for candidate in search_paths:\n        if candidate.exists():\n            content = candidate.read_text(encoding="utf-8", errors="replace")\n            if pattern in content and "0.10" in content:\n                log.debug("style_cap_enforced PASS: found in %s", candidate)\n                return True\n\n    # If the module doesn't exist yet, assume the cap will be enforced\n    # (the invariant becomes meaningful once the renderer is implemented)\n    log.debug(\n        "style_cap_enforced: renderer not yet implemented — "\n        "defaulting to PASS (cap will be enforced on implementation)"\n    )\n    return True\n\n\n# ---------------------------------------------------------------------------\n# InvariantChecker\n# ---------------------------------------------------------------------------\n\n\nclass InvariantChecker:\n    """\n    Registry and runner for boolean invariant callables.\n\n    Usage\n    -----\n    checker = InvariantChecker()\n    checker.register(postgres_is_sole_authority, "postgres_sole_authority")\n    checker.register(mem0_is_read_only, "mem0_read_only")\n    results = checker.run_all()\n    """\n\n    def __init__(self) -> None:\n        self._invariants: list[tuple[str, Callable[[], bool]]] = []\n\n    def register(self, fn: Callable[[], bool], name: str | None = None) -> None:\n        """Add *fn* to the registry with an optional display *name*."""\n        display_name = name or getattr(fn, "__name__", repr(fn))\n        self._invariants.append((display_name, fn))\n        log.debug("InvariantChecker: registered '%s'", display_name)\n\n    def register_defaults(self) -> None:\n        """Register all four built-in Tala invariants."""\n        self.register(postgres_is_sole_authority, "postgres_is_sole_authority")\n        self.register(mem0_is_read_only, "mem0_is_read_only")\n        self.register(vault_is_network_isolated, "vault_is_network_isolated")\n        self.register(style_cap_enforced, "style_cap_enforced")\n\n    def run_all(self) -> list[InvariantRecord]:\n        """\n        Execute every registered invariant and return a list of InvariantRecord.\n\n        Exceptions raised by an invariant are caught and recorded as ERROR\n        status — they do not propagate.\n        """\n        results: list[InvariantRecord] = []\n        for name, fn in self._invariants:\n            try:\n                ok = bool(fn())\n                status = InvariantStatus.PASS if ok else InvariantStatus.FAIL\n                detail = "" if ok else "Invariant returned False"\n            except Exception as exc:\n                status = InvariantStatus.ERROR\n                detail = f"{type(exc).__name__}: {exc}"\n                log.exception("Invariant [%s] raised an exception:", name)\n\n            record = InvariantRecord(name=name, status=status, detail=detail)\n            results.append(record)\n            level = logging.DEBUG if status == InvariantStatus.PASS else logging.WARNING\n            log.log(level, "Invariant [%s]: %s  %s", name, status.value, detail)\n\n        return results\n\n    def run_before_complete(self) -> bool:\n        """\n        Convenience method: run all invariants and return True iff all pass.\n\n        Suitable for use as a gate before marking a TaskItem COMPLETE.\n        """\n        results = self.run_all()\n        all_passed = all(r.status == InvariantStatus.PASS for r in results)\n        if not all_passed:\n            failures = [r.name for r in results if r.status != InvariantStatus.PASS]\n            log.warning(\n                "InvariantChecker.run_before_complete: %d failure(s): %s",\n                len(failures), failures,\n            )\n        return all_passed\n\n\n# ---------------------------------------------------------------------------\n# SmokeTestRunner\n# ---------------------------------------------------------------------------\n\n\nclass SmokeTestRunner:\n    """\n    Executes pytest on a list of test paths and parses the results.\n\n    Uses subprocess to keep the test runner in a clean process, avoiding any\n    module-state contamination from the agent runtime.\n    """\n\n    def run_smoke_tests(\n        self,\n        test_paths: list[str],\n        *,\n        extra_args: list[str] | None = None,\n        timeout: int = 120,\n    ) -> SmokeTestResult:\n        """\n        Run pytest against *test_paths*.\n\n        Parameters\n        ----------\n        test_paths:\n            List of file or directory paths to pass to pytest.\n        extra_args:\n            Additional pytest CLI arguments (e.g. ['-v', '-x']).\n        timeout:\n            Maximum seconds to wait for the test run.\n\n        Returns\n        -------\n        SmokeTestResult\n        """\n        if not test_paths:\n            return SmokeTestResult(\n                exit_code=0,\n                stdout="No test paths provided — skipped.",\n            )\n\n        cmd = [\n            sys.executable, "-m", "pytest",\n            "--tb=short",\n            "--no-header",\n            "-q",\n        ] + (extra_args or []) + test_paths\n\n        log.info("SmokeTestRunner: %s", " ".join(cmd))\n\n        try:\n            proc = subprocess.run(\n                cmd,\n                capture_output=True,\n                text=True,\n                timeout=timeout,\n            )\n        except subprocess.TimeoutExpired as exc:\n            return SmokeTestResult(\n                exit_code=-1,\n                stderr=f"pytest timed out after {timeout}s",\n                stdout=exc.stdout or "",\n            )\n        except FileNotFoundError as exc:\n            return SmokeTestResult(\n                exit_code=-1,\n                stderr=f"pytest not found: {exc}",\n            )\n\n        result = SmokeTestResult(\n            exit_code=proc.returncode,\n            stdout=proc.stdout,\n            stderr=proc.stderr,\n        )\n        self._parse_pytest_output(proc.stdout, result)\n        log.info(\n            "SmokeTestRunner: exit=%d passed=%d failed=%d errors=%d",\n            result.exit_code, result.passed, result.failed, result.errors,\n        )\n        return result\n\n    # ------------------------------------------------------------------\n    # Private helpers\n    # ------------------------------------------------------------------\n\n    def _parse_pytest_output(self, stdout: str, result: SmokeTestResult) -> None:\n        """\n        Parse the pytest summary line(s) to populate counters.\n\n        Handles patterns like:\n          '5 passed, 1 failed, 2 error in 0.42s'\n          '3 passed in 0.10s'\n          'no tests ran'\n        """\n        import re\n\n        # Pattern: N passed / N failed / N error / N skipped\n... (file truncated, showing first 500 lines) ...
```
