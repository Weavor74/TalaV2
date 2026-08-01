# File: `editor.py`

## Module Description

sdk/agent/editor.py
===================
Codebase Discovery & Surgical Editor — Tala Cognitive Engine Room
Milestone 6 | Task 6.2.1

Provides:
  • CodeIndex    — fast recursive scanner that builds a CodebaseSnapshot
  • SurgicalEditor — pinpoint line-level patch applicator
  • ErrorRepairProtocol — traceback parser + minimal-patch applier

Design mandate:
  All code changes MUST route through SurgicalEditor.apply_patch().
  Full-file rewrites are permitted ONLY when force=True is explicitly passed,
  and a WARNING is logged every time this escape hatch is used.

## Classes

### `class FileIndex`
Parsed index for a single source file.


### `class CodebaseSnapshot`
Full indexed view of a codebase directory.

- **`def get(self, path)`**
- **`def search_function(self, name)`**
  - Return all FileIndex objects that declare a function named *name*.
- **`def search_class(self, name)`**
  - Return all FileIndex objects that declare a class named *name*.

### `class PatchResult`
Result of a SurgicalEditor.apply_patch() call.


### `class ErrorLocation`
Parsed location of the root cause from a Python traceback.


### `class CodeIndex`
Recursively scans a directory tree and produces a CodebaseSnapshot.

Supported extraction:
  Python  — functions, classes, imports (via ast)
  JS/TS   — function declarations, class declarations, import/export
             statements (via regex; no full AST parser required)
  HTML    — script tags, link tags noted as imports

- **`def scan(self, root_path)`**
  - Index all source files under *root_path*.
  
  Parameters
  ----------
  root_path:
      Absolute path to the directory root to scan.
  
  Returns
  -------
  CodebaseSnapshot
- **`def _index_file(self, abs_path, lang)`**
- **`def _parse_python(self, source, fi)`**
- **`def _parse_js(self, source, fi)`**
- **`def _parse_html(self, source, fi)`**

### `class SurgicalEditor`
Applies pinpoint, line-level edits to source files.

Rules
-----
• apply_patch() NEVER rewrites a full file unless force=True.
• When force=True, a WARNING is logged — this is an escape hatch.
• The target line's existing content is verified against old_content
  before the patch is applied (fail-safe).
• All operations are atomic: the original file is only modified after
  the complete patch is built in memory.

- **`def apply_patch(self, file_path, target_line, old_content, new_content)`**
  - Replace *old_content* at *target_line* with *new_content*.
  
  Parameters
  ----------
  file_path:
      Absolute path to the file to edit.
  target_line:
      1-based line number where the replacement begins.
  old_content:
      The exact text that must be present at that line.
      Acts as a safety guard — if not found, the patch is rejected.
  new_content:
      Replacement text.  May span multiple lines.
  force:
      When True, if old_content is not found at target_line the editor
      will attempt a full-file search-and-replace.  Logs WARNING.
  context_lines:
      Not used in the current implementation; reserved for future diff
      context display.
  
  Returns
  -------
  PatchResult
- **`def insert_after(self, file_path, after_line, content)`**
  - Insert *content* after *after_line* without disturbing any other lines.
- **`def delete_line(self, file_path, target_line)`**
  - Remove a single line from *file_path*.

### `class ErrorRepairProtocol`
Parses Python tracebacks to identify the root error location,
then applies the smallest possible patch to fix it.

Strategy:
  1. Parse the traceback to find the innermost (deepest) frame.
  2. Locate the corresponding source file in the CodebaseSnapshot.
  3. Produce a PatchResult via SurgicalEditor targeting only the
     offending line.

This class does NOT guess at the correct fix — it identifies WHERE the
error is.  Actual fix content must be provided by the caller (or an LLM).

- **`def __init__(self, editor)`**
- **`def find_root_error(self, traceback_str)`**
  - Parse *traceback_str* and return an ErrorLocation for the root cause.
  
  The "root error" is the deepest frame in the traceback (last File/line
  reference before the exception type line).
  
  Raises
  ------
  ValueError
      If no traceback frame can be found in *traceback_str*.
- **`def apply_minimal_fix(self, error, codebase, fix_content)`**
  - Apply *fix_content* as a surgical patch at the error location.
  
  Parameters
  ----------
  error:
      ErrorLocation returned by find_root_error().
  codebase:
      CodebaseSnapshot used to verify the file exists in the index.
  fix_content:
      The replacement text for the offending line.
  
  Returns
  -------
  PatchResult

## Source Code

```python
"""\nsdk/agent/editor.py\n===================\nCodebase Discovery & Surgical Editor — Tala Cognitive Engine Room\nMilestone 6 | Task 6.2.1\n\nProvides:\n  • CodeIndex    — fast recursive scanner that builds a CodebaseSnapshot\n  • SurgicalEditor — pinpoint line-level patch applicator\n  • ErrorRepairProtocol — traceback parser + minimal-patch applier\n\nDesign mandate:\n  All code changes MUST route through SurgicalEditor.apply_patch().\n  Full-file rewrites are permitted ONLY when force=True is explicitly passed,\n  and a WARNING is logged every time this escape hatch is used.\n"""\n\nfrom __future__ import annotations\n\nimport ast\nimport logging\nimport os\nimport re\nimport traceback as tb_module\nimport warnings\nfrom dataclasses import dataclass, field\nfrom datetime import UTC, datetime\nfrom pathlib import Path\nfrom typing import Any\n\nlog = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Language detection\n# ---------------------------------------------------------------------------\n\n_EXT_LANGUAGE: dict[str, str] = {\n    ".py": "python",\n    ".js": "javascript",\n    ".mjs": "javascript",\n    ".cjs": "javascript",\n    ".ts": "typescript",\n    ".tsx": "typescript",\n    ".jsx": "javascript",\n    ".html": "html",\n    ".htm": "html",\n    ".css": "css",\n    ".json": "json",\n    ".md": "markdown",\n}\n\n_SKIP_DIRS = {\n    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",\n    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",\n}\n\n\n# ---------------------------------------------------------------------------\n# Data classes\n# ---------------------------------------------------------------------------\n\n\n@dataclass\nclass FileIndex:\n    """Parsed index for a single source file."""\n\n    path: str\n    language: str\n    functions: list[str] = field(default_factory=list)\n    classes: list[str] = field(default_factory=list)\n    imports: list[str] = field(default_factory=list)\n    exports: list[str] = field(default_factory=list)  # JS/TS only\n    line_count: int = 0\n    parse_error: str | None = None\n\n\n@dataclass\nclass CodebaseSnapshot:\n    """Full indexed view of a codebase directory."""\n\n    root_path: str\n    files: dict[str, FileIndex] = field(default_factory=dict)\n    indexed_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n    total_files: int = 0\n    total_lines: int = 0\n\n    def get(self, path: str) -> FileIndex | None:\n        return self.files.get(path)\n\n    def search_function(self, name: str) -> list[FileIndex]:\n        """Return all FileIndex objects that declare a function named *name*."""\n        return [fi for fi in self.files.values() if name in fi.functions]\n\n    def search_class(self, name: str) -> list[FileIndex]:\n        """Return all FileIndex objects that declare a class named *name*."""\n        return [fi for fi in self.files.values() if name in fi.classes]\n\n\n@dataclass\nclass PatchResult:\n    """Result of a SurgicalEditor.apply_patch() call."""\n\n    file_path: str\n    success: bool\n    lines_changed: int = 0\n    old_content: str = ""\n    new_content: str = ""\n    error: str | None = None\n    forced_full_rewrite: bool = False\n    patched_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n\n\n@dataclass\nclass ErrorLocation:\n    """Parsed location of the root cause from a Python traceback."""\n\n    file_path: str\n    line_number: int\n    function_name: str\n    error_type: str\n    error_message: str\n    raw_frame: str = ""\n\n\n# ---------------------------------------------------------------------------\n# CodeIndex\n# ---------------------------------------------------------------------------\n\n\nclass CodeIndex:\n    """\n    Recursively scans a directory tree and produces a CodebaseSnapshot.\n\n    Supported extraction:\n      Python  — functions, classes, imports (via ast)\n      JS/TS   — function declarations, class declarations, import/export\n                 statements (via regex; no full AST parser required)\n      HTML    — script tags, link tags noted as imports\n    """\n\n    def scan(self, root_path: str) -> CodebaseSnapshot:\n        """\n        Index all source files under *root_path*.\n\n        Parameters\n        ----------\n        root_path:\n            Absolute path to the directory root to scan.\n\n        Returns\n        -------\n        CodebaseSnapshot\n        """\n        root = Path(root_path).resolve()\n        if not root.is_dir():\n            raise NotADirectoryError(f"CodeIndex.scan: '{root_path}' is not a directory")\n\n        snapshot = CodebaseSnapshot(root_path=str(root))\n        log.info("CodeIndex scanning: %s", root)\n\n        for dirpath, dirnames, filenames in os.walk(root):\n            # Prune skip dirs in-place so os.walk won't descend into them\n            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]\n\n            for filename in filenames:\n                ext = Path(filename).suffix.lower()\n                lang = _EXT_LANGUAGE.get(ext)\n                if lang is None:\n                    continue\n\n                abs_path = str(Path(dirpath) / filename)\n                fi = self._index_file(abs_path, lang)\n                snapshot.files[abs_path] = fi\n                snapshot.total_lines += fi.line_count\n\n        snapshot.total_files = len(snapshot.files)\n        log.info(\n            "CodeIndex complete: %d file(s), %d line(s)",\n            snapshot.total_files, snapshot.total_lines,\n        )\n        return snapshot\n\n    # ------------------------------------------------------------------\n    # Private indexing helpers\n    # ------------------------------------------------------------------\n\n    def _index_file(self, abs_path: str, lang: str) -> FileIndex:\n        fi = FileIndex(path=abs_path, language=lang)\n        try:\n            content = Path(abs_path).read_text(encoding="utf-8", errors="replace")\n            fi.line_count = content.count("\n") + 1\n            if lang == "python":\n                self._parse_python(content, fi)\n            elif lang in ("javascript", "typescript"):\n                self._parse_js(content, fi)\n            elif lang == "html":\n                self._parse_html(content, fi)\n        except Exception as exc:\n            fi.parse_error = str(exc)\n            log.debug("Failed to index %s: %s", abs_path, exc)\n        return fi\n\n    def _parse_python(self, source: str, fi: FileIndex) -> None:\n        try:\n            tree = ast.parse(source)\n        except SyntaxError as exc:\n            fi.parse_error = f"SyntaxError: {exc}"\n            return\n\n        for node in ast.walk(tree):\n            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):\n                fi.functions.append(node.name)\n            elif isinstance(node, ast.ClassDef):\n                fi.classes.append(node.name)\n            elif isinstance(node, ast.Import):\n                for alias in node.names:\n                    fi.imports.append(alias.name)\n            elif isinstance(node, ast.ImportFrom):\n                module = node.module or ""\n                names = ", ".join(a.name for a in node.names)\n                fi.imports.append(f"from {module} import {names}")\n\n    def _parse_js(self, source: str, fi: FileIndex) -> None:\n        # Named functions: function foo(...) and arrow/const foo = (...)\n        for m in re.finditer(\n            r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)",\n            source,\n        ):\n            fi.functions.append(m.group(1))\n\n        # Arrow functions assigned to const/let/var\n        for m in re.finditer(\n            r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?"\n            r"(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>",\n            source,\n        ):\n            fi.functions.append(m.group(1))\n\n        # Classes\n        for m in re.finditer(r"class\s+([A-Za-z_$][A-Za-z0-9_$]*)", source):\n            fi.classes.append(m.group(1))\n\n        # Imports\n        for m in re.finditer(r"import\s+.+?\s+from\s+['\"]([^'\"]+)['\"]", source):\n            fi.imports.append(m.group(1))\n\n        # Exports\n        for m in re.finditer(\n            r"export\s+(?:default\s+)?(?:function|class|const|let|var)\s+"\n            r"([A-Za-z_$][A-Za-z0-9_$]*)",\n            source,\n        ):\n            fi.exports.append(m.group(1))\n        # module.exports\n        for m in re.finditer(r"module\.exports\s*=\s*\{([^}]+)\}", source):\n            for name in re.findall(r"([A-Za-z_$][A-Za-z0-9_$]*)", m.group(1)):\n                fi.exports.append(name)\n\n    def _parse_html(self, source: str, fi: FileIndex) -> None:\n        # Script src references as "imports"\n        for m in re.finditer(r'<script[^>]+src=[\'"]([^\'"]+)[\'"]', source, re.I):\n            fi.imports.append(m.group(1))\n        # Link href references\n        for m in re.finditer(r'<link[^>]+href=[\'"]([^\'"]+)[\'"]', source, re.I):\n            fi.imports.append(m.group(1))\n\n\n# ---------------------------------------------------------------------------\n# SurgicalEditor\n# ---------------------------------------------------------------------------\n\n\nclass SurgicalEditor:\n    """\n    Applies pinpoint, line-level edits to source files.\n\n    Rules\n    -----\n    • apply_patch() NEVER rewrites a full file unless force=True.\n    • When force=True, a WARNING is logged — this is an escape hatch.\n    • The target line's existing content is verified against old_content\n      before the patch is applied (fail-safe).\n    • All operations are atomic: the original file is only modified after\n      the complete patch is built in memory.\n    """\n\n    def apply_patch(\n        self,\n        file_path: str,\n        target_line: int,\n        old_content: str,\n        new_content: str,\n        *,\n        force: bool = False,\n        context_lines: int = 0,\n    ) -> PatchResult:\n        """\n        Replace *old_content* at *target_line* with *new_content*.\n\n        Parameters\n        ----------\n        file_path:\n            Absolute path to the file to edit.\n        target_line:\n            1-based line number where the replacement begins.\n        old_content:\n            The exact text that must be present at that line.\n            Acts as a safety guard — if not found, the patch is rejected.\n        new_content:\n            Replacement text.  May span multiple lines.\n        force:\n            When True, if old_content is not found at target_line the editor\n            will attempt a full-file search-and-replace.  Logs WARNING.\n        context_lines:\n            Not used in the current implementation; reserved for future diff\n            context display.\n\n        Returns\n        -------\n        PatchResult\n        """\n        path = Path(file_path)\n        if not path.exists():\n            return PatchResult(\n                file_path=file_path,\n                success=False,\n                error=f"File does not exist: {file_path}",\n            )\n\n        original_text = path.read_text(encoding="utf-8", errors="replace")\n        lines = original_text.splitlines(keepends=True)\n\n        # Convert to 0-based index\n        idx = target_line - 1\n        if idx < 0 or idx >= len(lines):\n            return PatchResult(\n                file_path=file_path,\n                success=False,\n                error=f"target_line {target_line} out of range (file has {len(lines)} lines)",\n            )\n\n        # Verify the target line contains old_content\n        target_text = lines[idx].rstrip("\n").rstrip("\r")\n        old_stripped = old_content.rstrip("\n").rstrip("\r")\n\n        if old_stripped in target_text:\n            # Surgical replacement within the line\n            updated_line = lines[idx].replace(old_stripped, new_content, 1)\n            new_lines = lines[:idx] + [updated_line] + lines[idx + 1:]\n            new_file_content = "".join(new_lines)\n            path.write_text(new_file_content, encoding="utf-8")\n            log.info(\n                "SurgicalEditor: patched %s line %d (%d char replacement)",\n                file_path, target_line, len(old_content),\n            )\n            return PatchResult(\n                file_path=file_path,\n                success=True,\n                lines_changed=1,\n                old_content=old_content,\n                new_content=new_content,\n            )\n\n        if force:\n            # Full-file search-and-replace — escape hatch, always logged\n            log.warning(\n                "SurgicalEditor: force=True triggered full-file search-replace on %s. "\n                "This should be exceptional — prefer pinpoint patches.",\n                file_path,\n            )\n            if old_content not in original_text:\n                return PatchResult(\n                    file_path=file_path,\n                    success=False,\n                    error=f"old_content not found anywhere in {file_path}",\n                    forced_full_rewrite=True,\n                )\n            count = original_text.count(old_content)\n            new_file_content = original_text.replace(old_content, new_content)\n            path.write_text(new_file_content, encoding="utf-8")\n            log.info(\n                "SurgicalEditor (force): replaced %d occurrence(s) in %s",\n                count, file_path,\n            )\n            return PatchResult(\n                file_path=file_path,\n                success=True,\n                lines_changed=count,\n                old_content=old_content,\n                new_content=new_content,\n                forced_full_rewrite=True,\n            )\n\n        return PatchResult(\n            file_path=file_path,\n            success=False,\n            error=(\n                f"old_content not found at line {target_line} of {file_path}. "\n                "Pass force=True to attempt full-file search-replace."\n            ),\n        )\n\n    def insert_after(\n        self,\n        file_path: str,\n        after_line: int,\n        content: str,\n    ) -> PatchResult:\n        """\n        Insert *content* after *after_line* without disturbing any other lines.\n        """\n        path = Path(file_path)\n        if not path.exists():\n            return PatchResult(\n                file_path=file_path,\n                success=False,\n                error=f"File not found: {file_path}",\n            )\n\n        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)\n        if after_line < 0 or after_line > len(lines):\n            return PatchResult(\n                file_path=file_path,\n                success=False,\n                error=f"after_line {after_line} out of range",\n            )\n\n        insertion = content if content.endswith("\n") else content + "\n"\n        new_lines = lines[:after_line] + [insertion] + lines[after_line:]\n        path.write_text("".join(new_lines), encoding="utf-8")\n        log.info("SurgicalEditor: inserted after line %d in %s", after_line, file_path)\n        return PatchResult(\n            file_path=file_path,\n            success=True,\n            lines_changed=1,\n            new_content=content,\n        )\n\n    def delete_line(self, file_path: str, target_line: int) -> PatchResult:\n        """Remove a single line from *file_path*."""\n        path = Path(file_path)\n        if not path.exists():\n            return PatchResult(file_path=file_path, success=False, error="File not found")\n\n        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)\n        idx = target_line - 1\n        if idx < 0 or idx >= len(lines):\n            return PatchResult(\n                file_path=file_path,\n                success=False,\n                error=f"target_line {target_line} out of range",\n            )\n\n        removed = lines.pop(idx)\n        path.write_text("".join(lines), encoding="utf-8")\n        log.info("SurgicalEditor: deleted line %d from %s", target_line, file_path)\n        return PatchResult(\n            file_path=file_path,\n            success=True,\n            lines_changed=1,\n            old_content=removed,\n        )\n\n\n# ---------------------------------------------------------------------------\n# ErrorRepairProtocol\n# ---------------------------------------------------------------------------\n\n\n# Regex to parse Python traceback frames\n_TRACEBACK_RE = re.compile(\n    r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)'\n)\n_EXCEPTION_RE = re.compile(r'^(?P<etype>[A-Za-z_][A-Za-z0-9_.]*Error[^\n]*): (?P<emsg>.+)$', re.M)\n\n\nclass ErrorRepairProtocol:\n    """\n    Parses Python tracebacks to identify the root error location,\n    then applies the smallest possible patch to fix it.\n\n    Strategy:\n      1. Parse the traceback to find the innermost (deepest) frame.\n      2. Locate the corresponding source file in the CodebaseSnapshot.\n      3. Produce a PatchResult via SurgicalEditor targeting only the\n         offending line.\n\n    This class does NOT guess at the correct fix — it identifies WHERE the\n    error is.  Actual fix content must be provided by the caller (or an LLM).\n    """\n\n    def __init__(self, editor: SurgicalEditor | None = None) -> None:\n        self._editor = editor or SurgicalEditor()\n\n    def find_root_error(self, traceback_str: str) -> ErrorLocation:\n        """\n        Parse *traceback_str* and return an ErrorLocation for the root cause.\n\n        The "root error" is the deepest frame in the traceback (last File/line\n        reference before the exception type line).\n... (file truncated, showing first 500 lines) ...
```
