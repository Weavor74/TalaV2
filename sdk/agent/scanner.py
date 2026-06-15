"""
sdk/agent/scanner.py
====================
Codebase Mapping & Capability Discovery Engine — Tala Cognitive Engine Room
Milestone 2 | Tasks 2.1, 2.2, 2.3, 2.4, 2.5

Implements static analysis to map directory structures, extract symbols,
discover exposed API/IPC routes, group system capabilities, and assess UI integrations.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_EXCLUSIONS = {
    ".git",
    "node_modules",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "build",
    "package-lock.json",
}


class RepositoryScanner:
    """
    Scans the repository to map files, extract metadata, discover API surfaces,
    and analyze dependencies and UI-to-IPC routes.
    """

    def __init__(
        self,
        root_dir: str,
        exclusions: set[str] | None = None,
    ) -> None:
        self.root_dir = os.path.abspath(root_dir)
        self.exclusions = exclusions or DEFAULT_EXCLUSIONS

    def build_codebase_map(self) -> dict[str, Any]:
        """
        Scan repository, map files, extract capabilities and discover routes/APIs.
        """
        files = self.scan_repo()
        mapped_data = self.map_codebase(files)
        capabilities = self.extract_capabilities(mapped_data)
        features = self.discover_features(mapped_data)
        ui_integration = self.assess_ui_integration()
        
        return {
            "capabilities": capabilities,
            "features": features,
            "ui_integration": ui_integration,
            "scanned_files": files[:200]
        }

    def scan_repo(self) -> list[str]:
        """
        Recursively scan the repository directory, returning all relative paths
        excluding directories/files in the exclusions set.
        """
        scanned_files: list[str] = []
        for root, dirs, files in os.walk(self.root_dir):
            # Prune directory search in-place based on exclusions
            dirs[:] = [d for d in dirs if d not in self.exclusions]
            
            for file in files:
                if file in self.exclusions:
                    continue
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, self.root_dir).replace(os.path.sep, "/")
                scanned_files.append(rel_path)
                
        log.info("RepositoryScanner: scanned %d files.", len(scanned_files))
        return sorted(scanned_files)

    def map_codebase(self, files: list[str]) -> dict[str, Any]:
        """
        Build an AST-based codebase map detailing imports, exports, functions, and classes.
        """
        mapping: dict[str, Any] = {}
        for file in files:
            abs_path = os.path.join(self.root_dir, file)
            ext = os.path.splitext(file)[1].lower()
            
            file_meta: dict[str, Any] = {
                "imports": [],
                "classes": [],
                "functions": [],
                "exports": [],
            }
            
            try:
                if ext == ".py":
                    self._parse_python_ast(abs_path, file_meta)
                elif ext in (".js", ".ts", ".jsx", ".tsx"):
                    self._parse_javascript_regex(abs_path, file_meta)
                mapping[file] = file_meta
            except Exception as exc:
                log.warning("Failed to parse file %s: %s", file, exc)
                mapping[file] = {"error": str(exc)}
                
        return mapping

    def discover_features(self, mapped_data: dict[str, Any]) -> dict[str, Any]:
        """
        Discover system features, routes, database tables, JSON-RPC, and Electron IPC handlers.
        """
        features = {
            "json_rpc_handlers": [],
            "electron_ipc_handlers": [],
            "database_tables": [],
        }

        # 1. Scraping JSON-RPC handlers from Python server files
        for filepath, meta in mapped_data.items():
            if "server.py" in filepath:
                for func in meta.get("functions", []):
                    if func.startswith("handle_"):
                        features["json_rpc_handlers"].append(func)

        # 2. Scraping IPC handlers from Electron main/preload/handler files
        for filepath in mapped_data.keys():
            abs_path = os.path.join(self.root_dir, filepath)
            if filepath.endswith((".js", ".ts")):
                try:
                    content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                    # Scrape ipcMain.handle(...)
                    ipc_matches = re.findall(r"ipcMain\.handle\s*\(\s*['\"]([^'\"]+)['\"]", content)
                    features["electron_ipc_handlers"].extend(ipc_matches)
                    # Scrape whitelisted preload channels
                    preload_matches = re.findall(r"['\"](file|vault|python|terminal):[^'\"]+['\"]", content)
                    for pm in preload_matches:
                        # Find full matches like 'file:read'
                        full_matches = re.findall(rf"['\"]({pm}:[^'\"]+)['\"]", content)
                        features["electron_ipc_handlers"].extend(full_matches)
                except Exception:
                    pass

        # 3. Scraping Database DDL Table Definitions from db.py
        db_path = os.path.join(self.root_dir, "python/harness/db.py")
        if os.path.exists(db_path):
            try:
                content = Path(db_path).read_text(encoding="utf-8", errors="replace")
                tables = re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", content, re.IGNORECASE)
                features["database_tables"].extend(list(set(tables)))
            except Exception:
                pass

        # De-duplicate lists
        features["electron_ipc_handlers"] = sorted(list(set(features["electron_ipc_handlers"])))
        features["json_rpc_handlers"] = sorted(list(set(features["json_rpc_handlers"])))
        features["database_tables"] = sorted(list(set(features["database_tables"])))
        
        return features

    def extract_capabilities(self, mapped_data: dict[str, Any]) -> dict[str, Any]:
        """
        Group files and directories into distinct functional capabilities.
        """
        capabilities: dict[str, list[str]] = {
            "persistence": [],
            "inference": [],
            "astrology": [],
            "electron_shell": [],
            "blockchain": [],
            "workflows": [],
            "other": [],
        }

        for file in mapped_data.keys():
            if "db.py" in file or "persistence.py" in file:
                capabilities["persistence"].append(file)
            elif "inference" in file or "vllm_engine" in file:
                capabilities["inference"].append(file)
            elif "astrology" in file or "pyswisseph" in file:
                capabilities["astrology"].append(file)
            elif "electron" in file or "renderer" in file:
                capabilities["electron_shell"].append(file)
            elif "blockchain" in file or "contracts" in file:
                capabilities["blockchain"].append(file)
            elif "workflows" in file or "workflow_compiler" in file:
                capabilities["workflows"].append(file)
            else:
                capabilities["other"].append(file)

        return capabilities

    def assess_ui_integration(self) -> dict[str, Any] | None:
        """
        Scrape renderer directory to map UI elements calling into Backend IPC channels.
        """
        ui_mappings = {
            "ipc_calls": [],
            "events_listened": [],
        }
        renderer_dir = os.path.join(self.root_dir, "renderer")
        if not os.path.exists(renderer_dir):
            return ui_mappings

        for root, _, files in os.walk(renderer_dir):
            for file in files:
                if file.endswith((".js", ".ts", ".html")):
                    abs_path = os.path.join(root, file)
                    try:
                        content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                        # Scrape window.tala.invoke('channel', ...)
                        invokes = re.findall(r"tala\.invoke\s*\(\s*['\"]([^'\"]+)['\"]", content)
                        for inv in invokes:
                            ui_mappings["ipc_calls"].append({
                                "file": os.path.relpath(abs_path, self.root_dir).replace(os.path.sep, "/"),
                                "channel": inv
                            })
                        
                        # Scrape window.tala.on('event', ...)
                        ons = re.findall(r"tala\.on\s*\(\s*['\"]([^'\"]+)['\"]", content)
                        for on_ev in ons:
                            ui_mappings["events_listened"].append({
                                "file": os.path.relpath(abs_path, self.root_dir).replace(os.path.sep, "/"),
                                "event": on_ev
                            })
                    except Exception:
                        pass
        return ui_mappings

    # ------------------------------------------------------------------
    # Private Parsers
    # ------------------------------------------------------------------

    def _parse_python_ast(self, filepath: str, meta: dict[str, Any]) -> None:
        """Parse Python source file to extract symbol metadata."""
        content = Path(filepath).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(content)
        
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                meta["functions"].append(node.name)
            elif isinstance(node, ast.ClassDef):
                meta["classes"].append(node.name)
            elif isinstance(node, ast.Import):
                for name in node.names:
                    meta["imports"].append(name.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    meta["imports"].append(node.module)

    def _parse_javascript_regex(self, filepath: str, meta: dict[str, Any]) -> None:
        """Parse JS/TS source file using simple regex extractors."""
        content = Path(filepath).read_text(encoding="utf-8", errors="replace")
        
        # Scrape require/import
        imports = re.findall(r"require\s*\(\s*['\"]([^'\"]+)['\"]", content)
        imports.extend(re.findall(r"import\s+.*\s+from\s+['\"]([^'\"]+)['\"]", content))
        meta["imports"] = sorted(list(set(imports)))

        # Scrape classes
        classes = re.findall(r"\bclass\s+(\w+)", content)
        meta["classes"] = sorted(list(set(classes)))

        # Scrape functions
        functions = re.findall(r"function\s+(\w+)", content)
        functions.extend(re.findall(r"const\s+(\w+)\s*=\s*\([^)]*\)\s*=>", content))
        functions.extend(re.findall(r"async\s+function\s+(\w+)", content))
        meta["functions"] = sorted(list(set(functions)))

    # ------------------------------------------------------------------
    # Phase 3 extensions — Milestone 9 | Tasks 9.2, 9.3, 9.4
    # ------------------------------------------------------------------

    def detect_broken_imports(self, files: list[str] | None = None) -> list[dict]:
        """
        Walk every Python file in the repository (or the supplied file list)
        and check whether local SDK/harness imports resolve to an actual file
        on disk.  Returns a list of violation dicts.

        Each violation has the shape::

            {"file": "<rel-path>", "import": "<module>", "reason": "<msg>"}
        """
        if files is None:
            files = self.scan_repo()

        violations: list[dict] = []

        for rel_path in files:
            if not rel_path.endswith(".py"):
                continue
            abs_path = os.path.join(self.root_dir, rel_path)
            try:
                source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=rel_path)
            except Exception as exc:
                log.debug("detect_broken_imports: could not parse %s: %s", rel_path, exc)
                continue

            for node in ast.walk(tree):
                module_name: str | None = None
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module_name = alias.name
                        if module_name and (
                            module_name.startswith("sdk.") or module_name.startswith("python.")
                        ):
                            path = module_name.replace(".", "/") + ".py"
                            if not os.path.exists(os.path.join(self.root_dir, path)):
                                violations.append(
                                    {
                                        "file": rel_path,
                                        "import": module_name,
                                        "reason": "module file not found",
                                    }
                                )
                elif isinstance(node, ast.ImportFrom):
                    module_name = node.module or ""
                    if module_name and (
                        module_name.startswith("sdk.") or module_name.startswith("python.")
                    ):
                        path = module_name.replace(".", "/") + ".py"
                        if not os.path.exists(os.path.join(self.root_dir, path)):
                            violations.append(
                                {
                                    "file": rel_path,
                                    "import": module_name,
                                    "reason": "module file not found",
                                }
                            )

        log.info("detect_broken_imports: found %d violations.", len(violations))
        return violations

    def detect_missing_files(self) -> list[str]:
        """
        Check for expected core system files and return any that are missing.
        """
        core_files = [
            "python/harness/server.py",
            "python/harness/db.py",
            "renderer/renderer.js",
            "blockchain/src/app.js",
            "sdk/agent/queue.py",
            "sdk/agent/planner.py",
            "sdk/agent/governance.py",
            "sdk/agent/async_shell.py",
            "sdk/agent/dispatcher.py",
            "sdk/agent/goal_engine.py",
            "sdk/agent/ipc_registry.py",
            "sdk/agent/command_parser.py",
        ]
        
        missing = []
        for file in core_files:
            if not os.path.exists(os.path.join(self.root_dir, file)):
                missing.append(file)
                
        log.info("detect_missing_files: found %d missing files.", len(missing))
        return missing

    def find_integration_point(self, feature_description: str) -> dict:
        """
        Given a plain-English description of a feature, return the most
        appropriate location in the codebase to implement it.

        Returns a dict with keys ``suggested_path``, ``reason``, and
        ``create_new`` (bool — True when a new file should be created).
        """
        desc = feature_description.lower()

        if "test" in desc:
            return {
                "suggested_path": "sdk/agent/tests/",
                "reason": "New tests belong in the test suite",
                "create_new": True,
            }
        if any(k in desc for k in ("rpc", "handler", "endpoint")):
            return {
                "suggested_path": "python/harness/server.py",
                "reason": "All RPC handlers are registered in server.py",
                "create_new": False,
            }
        if any(k in desc for k in ("wiki", "node", "neural")):
            return {
                "suggested_path": "sdk/agent/wiki.py",
                "reason": "Wiki operations extend wiki.py",
                "create_new": False,
            }
        if any(k in desc for k in ("ui", "panel", "renderer")):
            return {
                "suggested_path": "renderer/renderer.js",
                "reason": "UI surfaces are implemented in renderer.js",
                "create_new": False,
            }
        if any(k in desc for k in ("governance", "authority", "risk")):
            return {
                "suggested_path": "sdk/agent/governance.py",
                "reason": "Governance logic lives in governance.py",
                "create_new": False,
            }
        # Default
        return {
            "suggested_path": "sdk/agent/",
            "reason": "New SDK capabilities belong in sdk/agent/",
            "create_new": True,
        }

    def generate_capability_map(self) -> dict:
        """
        Build a comprehensive capability map of the repository by combining:

        * File / module inventory from :meth:`scan_repo`
        * Broken-import violations from :meth:`detect_broken_imports`
        * IPC audit data from :class:`~sdk.agent.ipc_registry.IPCRegistry`

        Returns a dict with keys ``total_files``, ``modules``, ``broken_imports``,
        and ``ipc_audit``.
        """
        modules = self.scan_repo()
        broken = self.detect_broken_imports(modules)
        missing = self.detect_missing_files()

        ipc_data: dict = {}
        try:
            from sdk.agent.ipc_registry import IPCRegistry  # noqa: PLC0415
            ipc = IPCRegistry(self.root_dir)
            ipc_data = ipc.audit()
        except Exception as exc:  # noqa: BLE001
            log.debug("generate_capability_map: IPCRegistry unavailable: %s", exc)

        return {
            "total_files": len(modules),
            "modules": modules,
            "broken_imports": broken,
            "missing_files": missing,
            "ipc_audit": ipc_data,
        }
