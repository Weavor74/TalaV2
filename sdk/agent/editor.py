"""
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
"""

from __future__ import annotations

import ast
import logging
import os
import re
import traceback as tb_module
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".json": "json",
    ".md": "markdown",
}

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FileIndex:
    """Parsed index for a single source file."""

    path: str
    language: str
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)  # JS/TS only
    line_count: int = 0
    parse_error: str | None = None


@dataclass
class CodebaseSnapshot:
    """Full indexed view of a codebase directory."""

    root_path: str
    files: dict[str, FileIndex] = field(default_factory=dict)
    indexed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    total_files: int = 0
    total_lines: int = 0

    def get(self, path: str) -> FileIndex | None:
        return self.files.get(path)

    def search_function(self, name: str) -> list[FileIndex]:
        """Return all FileIndex objects that declare a function named *name*."""
        return [fi for fi in self.files.values() if name in fi.functions]

    def search_class(self, name: str) -> list[FileIndex]:
        """Return all FileIndex objects that declare a class named *name*."""
        return [fi for fi in self.files.values() if name in fi.classes]


@dataclass
class PatchResult:
    """Result of a SurgicalEditor.apply_patch() call."""

    file_path: str
    success: bool
    lines_changed: int = 0
    old_content: str = ""
    new_content: str = ""
    error: str | None = None
    forced_full_rewrite: bool = False
    patched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ErrorLocation:
    """Parsed location of the root cause from a Python traceback."""

    file_path: str
    line_number: int
    function_name: str
    error_type: str
    error_message: str
    raw_frame: str = ""


# ---------------------------------------------------------------------------
# CodeIndex
# ---------------------------------------------------------------------------


class CodeIndex:
    """
    Recursively scans a directory tree and produces a CodebaseSnapshot.

    Supported extraction:
      Python  — functions, classes, imports (via ast)
      JS/TS   — function declarations, class declarations, import/export
                 statements (via regex; no full AST parser required)
      HTML    — script tags, link tags noted as imports
    """

    def scan(self, root_path: str) -> CodebaseSnapshot:
        """
        Index all source files under *root_path*.

        Parameters
        ----------
        root_path:
            Absolute path to the directory root to scan.

        Returns
        -------
        CodebaseSnapshot
        """
        root = Path(root_path).resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"CodeIndex.scan: '{root_path}' is not a directory")

        snapshot = CodebaseSnapshot(root_path=str(root))
        log.info("CodeIndex scanning: %s", root)

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune skip dirs in-place so os.walk won't descend into them
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

            for filename in filenames:
                ext = Path(filename).suffix.lower()
                lang = _EXT_LANGUAGE.get(ext)
                if lang is None:
                    continue

                abs_path = str(Path(dirpath) / filename)
                fi = self._index_file(abs_path, lang)
                snapshot.files[abs_path] = fi
                snapshot.total_lines += fi.line_count

        snapshot.total_files = len(snapshot.files)
        log.info(
            "CodeIndex complete: %d file(s), %d line(s)",
            snapshot.total_files, snapshot.total_lines,
        )
        return snapshot

    # ------------------------------------------------------------------
    # Private indexing helpers
    # ------------------------------------------------------------------

    def _index_file(self, abs_path: str, lang: str) -> FileIndex:
        fi = FileIndex(path=abs_path, language=lang)
        try:
            content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            fi.line_count = content.count("\n") + 1
            if lang == "python":
                self._parse_python(content, fi)
            elif lang in ("javascript", "typescript"):
                self._parse_js(content, fi)
            elif lang == "html":
                self._parse_html(content, fi)
        except Exception as exc:
            fi.parse_error = str(exc)
            log.debug("Failed to index %s: %s", abs_path, exc)
        return fi

    def _parse_python(self, source: str, fi: FileIndex) -> None:
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            fi.parse_error = f"SyntaxError: {exc}"
            return

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fi.functions.append(node.name)
            elif isinstance(node, ast.ClassDef):
                fi.classes.append(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    fi.imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = ", ".join(a.name for a in node.names)
                fi.imports.append(f"from {module} import {names}")

    def _parse_js(self, source: str, fi: FileIndex) -> None:
        # Named functions: function foo(...) and arrow/const foo = (...)
        for m in re.finditer(
            r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)",
            source,
        ):
            fi.functions.append(m.group(1))

        # Arrow functions assigned to const/let/var
        for m in re.finditer(
            r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?"
            r"(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>",
            source,
        ):
            fi.functions.append(m.group(1))

        # Classes
        for m in re.finditer(r"class\s+([A-Za-z_$][A-Za-z0-9_$]*)", source):
            fi.classes.append(m.group(1))

        # Imports
        for m in re.finditer(r"import\s+.+?\s+from\s+['\"]([^'\"]+)['\"]", source):
            fi.imports.append(m.group(1))

        # Exports
        for m in re.finditer(
            r"export\s+(?:default\s+)?(?:function|class|const|let|var)\s+"
            r"([A-Za-z_$][A-Za-z0-9_$]*)",
            source,
        ):
            fi.exports.append(m.group(1))
        # module.exports
        for m in re.finditer(r"module\.exports\s*=\s*\{([^}]+)\}", source):
            for name in re.findall(r"([A-Za-z_$][A-Za-z0-9_$]*)", m.group(1)):
                fi.exports.append(name)

    def _parse_html(self, source: str, fi: FileIndex) -> None:
        # Script src references as "imports"
        for m in re.finditer(r'<script[^>]+src=[\'"]([^\'"]+)[\'"]', source, re.I):
            fi.imports.append(m.group(1))
        # Link href references
        for m in re.finditer(r'<link[^>]+href=[\'"]([^\'"]+)[\'"]', source, re.I):
            fi.imports.append(m.group(1))


# ---------------------------------------------------------------------------
# SurgicalEditor
# ---------------------------------------------------------------------------


class SurgicalEditor:
    """
    Applies pinpoint, line-level edits to source files.

    Rules
    -----
    • apply_patch() NEVER rewrites a full file unless force=True.
    • When force=True, a WARNING is logged — this is an escape hatch.
    • The target line's existing content is verified against old_content
      before the patch is applied (fail-safe).
    • All operations are atomic: the original file is only modified after
      the complete patch is built in memory.
    """

    def apply_patch(
        self,
        file_path: str,
        target_line: int,
        old_content: str,
        new_content: str,
        *,
        force: bool = False,
        context_lines: int = 0,
    ) -> PatchResult:
        """
        Replace *old_content* at *target_line* with *new_content*.

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
        """
        path = Path(file_path)
        if not path.exists():
            return PatchResult(
                file_path=file_path,
                success=False,
                error=f"File does not exist: {file_path}",
            )

        original_text = path.read_text(encoding="utf-8", errors="replace")
        lines = original_text.splitlines(keepends=True)

        # Convert to 0-based index
        idx = target_line - 1
        if idx < 0 or idx >= len(lines):
            return PatchResult(
                file_path=file_path,
                success=False,
                error=f"target_line {target_line} out of range (file has {len(lines)} lines)",
            )

        # Verify the target line contains old_content
        target_text = lines[idx].rstrip("\n").rstrip("\r")
        old_stripped = old_content.rstrip("\n").rstrip("\r")

        if old_stripped in target_text:
            # Surgical replacement within the line
            updated_line = lines[idx].replace(old_stripped, new_content, 1)
            new_lines = lines[:idx] + [updated_line] + lines[idx + 1:]
            new_file_content = "".join(new_lines)
            path.write_text(new_file_content, encoding="utf-8")
            log.info(
                "SurgicalEditor: patched %s line %d (%d char replacement)",
                file_path, target_line, len(old_content),
            )
            return PatchResult(
                file_path=file_path,
                success=True,
                lines_changed=1,
                old_content=old_content,
                new_content=new_content,
            )

        if force:
            # Full-file search-and-replace — escape hatch, always logged
            log.warning(
                "SurgicalEditor: force=True triggered full-file search-replace on %s. "
                "This should be exceptional — prefer pinpoint patches.",
                file_path,
            )
            if old_content not in original_text:
                return PatchResult(
                    file_path=file_path,
                    success=False,
                    error=f"old_content not found anywhere in {file_path}",
                    forced_full_rewrite=True,
                )
            count = original_text.count(old_content)
            new_file_content = original_text.replace(old_content, new_content)
            path.write_text(new_file_content, encoding="utf-8")
            log.info(
                "SurgicalEditor (force): replaced %d occurrence(s) in %s",
                count, file_path,
            )
            return PatchResult(
                file_path=file_path,
                success=True,
                lines_changed=count,
                old_content=old_content,
                new_content=new_content,
                forced_full_rewrite=True,
            )

        return PatchResult(
            file_path=file_path,
            success=False,
            error=(
                f"old_content not found at line {target_line} of {file_path}. "
                "Pass force=True to attempt full-file search-replace."
            ),
        )

    def insert_after(
        self,
        file_path: str,
        after_line: int,
        content: str,
    ) -> PatchResult:
        """
        Insert *content* after *after_line* without disturbing any other lines.
        """
        path = Path(file_path)
        if not path.exists():
            return PatchResult(
                file_path=file_path,
                success=False,
                error=f"File not found: {file_path}",
            )

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        if after_line < 0 or after_line > len(lines):
            return PatchResult(
                file_path=file_path,
                success=False,
                error=f"after_line {after_line} out of range",
            )

        insertion = content if content.endswith("\n") else content + "\n"
        new_lines = lines[:after_line] + [insertion] + lines[after_line:]
        path.write_text("".join(new_lines), encoding="utf-8")
        log.info("SurgicalEditor: inserted after line %d in %s", after_line, file_path)
        return PatchResult(
            file_path=file_path,
            success=True,
            lines_changed=1,
            new_content=content,
        )

    def delete_line(self, file_path: str, target_line: int) -> PatchResult:
        """Remove a single line from *file_path*."""
        path = Path(file_path)
        if not path.exists():
            return PatchResult(file_path=file_path, success=False, error="File not found")

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        idx = target_line - 1
        if idx < 0 or idx >= len(lines):
            return PatchResult(
                file_path=file_path,
                success=False,
                error=f"target_line {target_line} out of range",
            )

        removed = lines.pop(idx)
        path.write_text("".join(lines), encoding="utf-8")
        log.info("SurgicalEditor: deleted line %d from %s", target_line, file_path)
        return PatchResult(
            file_path=file_path,
            success=True,
            lines_changed=1,
            old_content=removed,
        )


# ---------------------------------------------------------------------------
# ErrorRepairProtocol
# ---------------------------------------------------------------------------


# Regex to parse Python traceback frames
_TRACEBACK_RE = re.compile(
    r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)'
)
_EXCEPTION_RE = re.compile(r'^(?P<etype>[A-Za-z_][A-Za-z0-9_.]*Error[^\n]*): (?P<emsg>.+)$', re.M)


class ErrorRepairProtocol:
    """
    Parses Python tracebacks to identify the root error location,
    then applies the smallest possible patch to fix it.

    Strategy:
      1. Parse the traceback to find the innermost (deepest) frame.
      2. Locate the corresponding source file in the CodebaseSnapshot.
      3. Produce a PatchResult via SurgicalEditor targeting only the
         offending line.

    This class does NOT guess at the correct fix — it identifies WHERE the
    error is.  Actual fix content must be provided by the caller (or an LLM).
    """

    def __init__(self, editor: SurgicalEditor | None = None) -> None:
        self._editor = editor or SurgicalEditor()

    def find_root_error(self, traceback_str: str) -> ErrorLocation:
        """
        Parse *traceback_str* and return an ErrorLocation for the root cause.

        The "root error" is the deepest frame in the traceback (last File/line
        reference before the exception type line).

        Raises
        ------
        ValueError
            If no traceback frame can be found in *traceback_str*.
        """
        frames = list(_TRACEBACK_RE.finditer(traceback_str))
        if not frames:
            raise ValueError("No traceback frames found in provided string.")

        # The innermost (deepest) frame is the root cause
        root_frame = frames[-1]
        file_path = root_frame.group("file")
        line_number = int(root_frame.group("line"))
        function_name = root_frame.group("func")
        raw_frame = root_frame.group(0)

        # Extract exception type and message
        exc_match = _EXCEPTION_RE.search(traceback_str)
        error_type = exc_match.group("etype") if exc_match else "UnknownError"
        error_message = exc_match.group("emsg") if exc_match else traceback_str.strip()

        log.info(
            "ErrorRepairProtocol.find_root_error: %s at %s:%d in %s()",
            error_type, file_path, line_number, function_name,
        )
        return ErrorLocation(
            file_path=file_path,
            line_number=line_number,
            function_name=function_name,
            error_type=error_type,
            error_message=error_message,
            raw_frame=raw_frame,
        )

    def apply_minimal_fix(
        self,
        error: ErrorLocation,
        codebase: CodebaseSnapshot,
        fix_content: str,
    ) -> PatchResult:
        """
        Apply *fix_content* as a surgical patch at the error location.

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
        """
        fi = codebase.get(error.file_path)
        if fi is None:
            # Attempt a case-insensitive lookup (paths can differ by case on Windows)
            lower_map = {k.lower(): v for k, v in codebase.files.items()}
            fi = lower_map.get(error.file_path.lower())

        if fi is None:
            return PatchResult(
                file_path=error.file_path,
                success=False,
                error=(
                    f"File {error.file_path!r} not found in CodebaseSnapshot. "
                    "Re-scan with CodeIndex.scan() first."
                ),
            )

        # Read the current content of the offending line
        path = Path(error.file_path)
        if not path.exists():
            return PatchResult(
                file_path=error.file_path,
                success=False,
                error=f"Source file not found on disk: {error.file_path}",
            )

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        idx = error.line_number - 1
        if idx < 0 or idx >= len(lines):
            return PatchResult(
                file_path=error.file_path,
                success=False,
                error=f"Line {error.line_number} out of range in {error.file_path}",
            )

        old_line_content = lines[idx].strip()

        return self._editor.apply_patch(
            file_path=error.file_path,
            target_line=error.line_number,
            old_content=old_line_content,
            new_content=fix_content,
        )
