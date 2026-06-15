"""
sdk/agent/linter.py
==================
Architecture Enforcement Linter — Tala Cognitive Engine Room
Milestone 2 | Task 2.6

Implements static constraints checking to ensure system layers (Renderer,
Blockchain, and Python Harness) remain securely separated and adhere to
strict boundaries.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ArchitectureLinter:
    """
    Validates architectural boundaries across the codebase.
    """

    def __init__(self, root_dir: str) -> None:
        self.root_dir = os.path.abspath(root_dir)

    def check_renderer_isolation(self, files: list[str]) -> list[dict[str, Any]]:
        """
        Enforce that renderer files do not import Node.js built-ins, python modules,
        or attempt direct filesystem/database calls.
        """
        violations: list[dict[str, Any]] = []
        forbidden_patterns = [
            (re.compile(r"require\s*\(\s*['\"](fs|path|os|child_process)['\"]"), "Direct Node.js built-in import in renderer"),
            (re.compile(r"import\s+.*\s+from\s+['\"](fs|path|os|child_process)['\"]"), "Direct Node.js built-in import in renderer"),
            (re.compile(r"asyncpg"), "PostgreSQL driver dependency in renderer"),
            # Match actual code references to python/harness (require/import/string literals),
            # but skip pure comment lines to avoid false positives on documentation comments.
            (re.compile(r"^(?!\s*/{1,2}).*(?:require|import|from|['\"]).*python/harness"), "Harness direct import in renderer"),
        ]

        for file in files:
            if not file.startswith("renderer/"):
                continue
            abs_path = os.path.join(self.root_dir, file)
            if not os.path.exists(abs_path) or os.path.isdir(abs_path):
                continue

            try:
                content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                for line_idx, line in enumerate(lines, 1):
                    for pattern, rule in forbidden_patterns:
                        if pattern.search(line):
                            violations.append({
                                "file": file,
                                "line": line_idx,
                                "code": line.strip(),
                                "rule": rule,
                            })
            except Exception as exc:
                log.warning("Linter failed reading %s: %s", file, exc)

        return violations

    def check_blockchain_isolation(self, files: list[str]) -> list[dict[str, Any]]:
        """
        Enforce that blockchain modules do not depend on python scripts/packages.
        """
        violations: list[dict[str, Any]] = []
        # Blockchain is node.js based; shouldn't import python harnesses or directories
        forbidden_patterns = [
            # Skip pure comment lines; only flag actual code references.
            (re.compile(r"^(?!\s*/{1,2}).*(?:require|import|from|['\"]).*python/harness"), "Python harness direct reference in blockchain module"),
            (re.compile(r"^(?!\s*/{1,2}).*(?:require|import|from|['\"]).*python/inference"), "Python inference reference in blockchain module"),
        ]

        for file in files:
            if not file.startswith("blockchain/"):
                continue
            abs_path = os.path.join(self.root_dir, file)
            if not os.path.exists(abs_path) or os.path.isdir(abs_path):
                continue

            try:
                content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                for line_idx, line in enumerate(lines, 1):
                    for pattern, rule in forbidden_patterns:
                        if pattern.search(line):
                            violations.append({
                                "file": file,
                                "line": line_idx,
                                "code": line.strip(),
                                "rule": rule,
                            })
            except Exception as exc:
                log.warning("Linter failed reading %s: %s", file, exc)

        return violations

    def check_database_sovereignty(self, files: list[str]) -> list[dict[str, Any]]:
        """
        Enforce that no non-harness file directly writes to Postgres or uses asyncpg
        drivers without going through db.py.
        """
        violations: list[dict[str, Any]] = []
        forbidden_patterns = [
            (re.compile(r"import\s+asyncpg"), "Direct asyncpg connection import outside harness"),
            (re.compile(r"from\s+asyncpg\s+import"), "Direct asyncpg driver import outside harness"),
        ]

        for file in files:
            # Exclude harness code itself and test modules that mock db connection
            if file.startswith(("python/harness/", "sdk/agent/tests/")) or file == "python/harness/db.py":
                continue
            abs_path = os.path.join(self.root_dir, file)
            if not os.path.exists(abs_path) or os.path.isdir(abs_path):
                continue

            try:
                content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                for line_idx, line in enumerate(lines, 1):
                    for pattern, rule in forbidden_patterns:
                        if pattern.search(line):
                            violations.append({
                                "file": file,
                                "line": line_idx,
                                "code": line.strip(),
                                "rule": rule,
                            })
            except Exception as exc:
                log.warning("Linter failed reading %s: %s", file, exc)

        return violations

    def check_wiki_first(self, files: list[str]) -> list[dict[str, Any]]:
        """
        Check if any new service file is created without corresponding wiki/documentation
        first (or duplicate wiki concept checking).
        Tala architectural boundary rule: Docs are updated BEFORE executing a technical change.
        """
        violations: list[dict[str, Any]] = []

        LEGACY_FILES = {
            # python/harness legacy files
            "python/harness/config.py",
            "python/harness/db.py",
            "python/harness/guardrails_manager.py",
            "python/harness/mem0_adapter.py",
            "python/harness/retrieval.py",
            "python/harness/search_manager.py",
            "python/harness/server.py",
            "python/harness/settings_nn.py",
            "python/harness/token_counter.py",
            "python/harness/workflow_executor.py",
            "python/harness/workflows_util.py",
            
            # sdk/agent legacy files
            "sdk/agent/activation.py",
            "sdk/agent/coder.py",
            "sdk/agent/editor.py",
            "sdk/agent/exceptions.py",
            "sdk/agent/gates.py",
            "sdk/agent/governance.py",
            "sdk/agent/ingestion.py",
            "sdk/agent/leases.py",
            "sdk/agent/linter.py",
            "sdk/agent/parser.py",
            "sdk/agent/persistence.py",
            "sdk/agent/planner.py",
            "sdk/agent/prompt.py",
            "sdk/agent/queue.py",
            "sdk/agent/recovery.py",
            "sdk/agent/routing.py",
            "sdk/agent/scanner.py",
            "sdk/agent/shell.py",
            "sdk/agent/telemetry.py",
            "sdk/agent/tester.py",
            "sdk/agent/verification.py",
            "sdk/agent/wiki.py",
            "sdk/agent/workflow_compiler.py",
        }
        
        # We search for HTML files (wiki nodes) in the codebase
        from sdk.agent.wiki import HTMLNNParser
        from sdk.agent.scanner import RepositoryScanner
        
        scanner = RepositoryScanner(self.root_dir)
        repo_files = scanner.scan_repo()
        
        wiki_node_ids = set()
        for f in repo_files:
            if f.endswith(".html"):
                abs_path = os.path.join(self.root_dir, f)
                try:
                    content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                    if "data-node-id" in content:
                        meta = HTMLNNParser.parse_node_metadata(content)
                        if meta.get("node_id"):
                            wiki_node_ids.add(meta["node_id"].lower())
                except Exception:
                    pass
                    
        # Verify that for any new python/harness service file or sdk/agent module,
        # there is a matching documented node id in the wiki.
        for file in files:
            normalized_file = file.replace("\\", "/")
            if normalized_file in LEGACY_FILES:
                continue
            if normalized_file.startswith(("python/harness/", "sdk/agent/")) and normalized_file.endswith(".py"):
                if normalized_file.endswith("__init__.py") or "tests/" in normalized_file:
                    continue
                # Construct expected node id, e.g., 'queue', 'async_shell', etc.
                stem = Path(normalized_file).stem.lower()
                expected_node_id = f"{stem}-node"
                
                # If neither the stem nor stem-node is in the wiki node IDs, it's a violation
                if stem not in wiki_node_ids and expected_node_id not in wiki_node_ids:
                    # Let's check if it's one of the legacy files. If not, it's a violation.
                    # For safety in tests, let's log the warning but only add to violations
                    # if the file was created recently or we enforce it strictly.
                    violations.append({
                        "file": file,
                        "line": 1,
                        "code": f"# file: {file}",
                        "rule": "Wiki-first architectural policy violation: new module created without corresponding wiki documentation node first",
                    })
                    
        return violations

    def run_all_checks(self, files: list[str]) -> list[dict[str, Any]]:
        """Run all architectural checks and return aggregated list of violations."""
        violations = []
        violations.extend(self.check_renderer_isolation(files))
        violations.extend(self.check_blockchain_isolation(files))
        violations.extend(self.check_database_sovereignty(files))
        violations.extend(self.check_wiki_first(files))
        return violations

    def lint_all(self) -> list[str]:
        """Scan repository files and run all architectural checks. Returns formatted error strings."""
        from sdk.agent.scanner import RepositoryScanner
        scanner = RepositoryScanner(self.root_dir)
        files = scanner.scan_repo()
        violations = self.run_all_checks(files)
        
        errors = []
        for v in violations:
            errors.append(f"{v['file']}:{v['line']} - {v['rule']}: {v['code']}")
        return errors

