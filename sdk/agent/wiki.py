"""
sdk/agent/wiki.py
=================
Knowledge Graph & Self-Healing Docs — Tala Cognitive Engine Room
Milestone 6 | Task 6.4.1

Provides:
  • WikiUpdater      — rewrites specific sections of HTML Neural Nodes
  • SelfHealingDocs  — compares actual resource structure to docs, finds gaps
  • Correction       — dataclass describing a documentation discrepancy
  • apply_corrections() — writes corrections to the node before code changes

Design principle:
  Documentation is updated BEFORE executing a technical change, never after.
  This ensures the knowledge graph always leads implementation rather than
  lagging behind it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any

from sdk.agent.anchor import canonicalize_and_anchor

log = logging.getLogger(__name__)


def _anchor_html_document(node_id: str, html: str, state_data: dict[str, Any]) -> None:
    try:
        canonicalize_and_anchor(
            node_id,
            html,
            state_data,
            None,
            creator="tala-system",
            document_type="html-nn",
        )
    except Exception as exc:
        log.debug("WikiUpdater: failed to anchor %s on blockchain: %s", node_id, exc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CorrectionSeverity(Enum):
    """How badly out-of-date a documented field is."""
    INFO = "INFO"          # Minor — cosmetic or additive
    WARNING = "WARNING"    # Moderate — missing or stale reference
    CRITICAL = "CRITICAL"  # Severe — structural mismatch or missing node


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Correction:
    """Describes a single discrepancy between documentation and reality."""

    field: str                         # Dotted path: e.g. 'functions.decompose'
    expected: str                      # What the docs say
    actual: str                        # What reality shows
    severity: CorrectionSeverity = CorrectionSeverity.WARNING
    node_path: str = ""               # Path to the HTML node to correct
    suggested_fix: str = ""           # Auto-generated correction text
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __str__(self) -> str:
        return (
            f"[{self.severity.value}] {self.field}: "
            f"expected={self.expected!r}, actual={self.actual!r}"
        )


@dataclass
class NodeUpdateResult:
    """Result of a WikiUpdater.update_node() call."""

    node_path: str
    success: bool
    sections_updated: list[str] = field(default_factory=list)
    error: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# HTML Section Manipulation Helpers
# ---------------------------------------------------------------------------


def _extract_section(html: str, section_id: str) -> tuple[int, int, str]:
    """
    Locate a ``<section id="section_id">...</section>`` block in *html*.

    Returns (start_idx, end_idx, inner_content) or (-1, -1, '') if not found.
    The indices span the opening tag through the closing ``</section>``.
    """
    # Match opening tag with the given id (attributes may appear in any order)
    open_pattern = re.compile(
        r'<section[^>]*\bid=["\']' + re.escape(section_id) + r'["\'][^>]*>',
        re.IGNORECASE | re.DOTALL,
    )
    m_open = open_pattern.search(html)
    if not m_open:
        return -1, -1, ""

    start = m_open.start()
    # Find the matching </section> — handle nesting
    depth = 1
    pos = m_open.end()
    while pos < len(html) and depth > 0:
        next_open = html.find("<section", pos)
        next_close = html.find("</section>", pos)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + len("<section")
        else:
            depth -= 1
            if depth == 0:
                end = next_close + len("</section>")
                inner = html[m_open.end(): next_close]
                return start, end, inner
            pos = next_close + len("</section>")

    return -1, -1, ""


def _replace_section_content(html: str, section_id: str, new_inner: str) -> str:
    """
    Replace the inner content of ``<section id="section_id">`` in *html*.

    Returns the modified HTML string.  Raises ValueError if the section
    is not found.
    """
    start, end, old_inner = _extract_section(html, section_id)
    if start == -1:
        raise ValueError(
            f"Section with id={section_id!r} not found in HTML content."
        )
    # Reconstruct: keep opening tag, inject new inner, add closing tag
    open_tag_end = html.find(">", start) + 1
    open_tag = html[start:open_tag_end]
    return html[:open_tag_end] + new_inner + "</section>" + html[end:]


def _build_section_block(section_id: str, content: str) -> str:
    """Return a new section HTML block."""
    return f'<section id="{section_id}">\n{content}\n</section>'


# ---------------------------------------------------------------------------
# WikiUpdater
# ---------------------------------------------------------------------------


class WikiUpdater:
    """
    Rewrites specific named sections inside an HTML Neural Node file.

    Neural Nodes are structured HTML files where each logical block of content
    is wrapped in ``<section id="...">`` tags.  WikiUpdater targets only the
    sections that have changed, leaving the rest of the document intact.

    Parameters
    ----------
    backup:
        When True (default), a ``.bak`` copy of the original file is saved
        alongside the updated file before any change is applied.
    """

    def __init__(self, backup: bool = True) -> None:
        self._backup = backup

    def update_node(
        self,
        node_path: str,
        new_content_sections: dict[str, str],
    ) -> NodeUpdateResult:
        """
        Rewrite specific sections of an HTML Neural Node.

        Parameters
        ----------
        node_path:
            Absolute path to the ``.html`` Neural Node file.
        new_content_sections:
            Mapping of ``{section_id: new_inner_html}``.  Only sections whose
            id appears in this dict are modified.

        Returns
        -------
        NodeUpdateResult
        """
        path = Path(node_path)
        result = NodeUpdateResult(node_path=node_path, success=False)

        if not path.exists():
            # If the node doesn't exist yet, create a minimal scaffold
            log.info("WikiUpdater: node does not exist — creating scaffold: %s", node_path)
            html = self._scaffold_node(path.stem, new_content_sections)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
            _anchor_html_document(path.stem, html, {"sections": new_content_sections, "source": "wiki-scaffold"})
            result.success = True
            result.sections_updated = list(new_content_sections.keys())
            log.info("WikiUpdater: scaffolded new node %s", node_path)
            return result

        original_html = path.read_text(encoding="utf-8", errors="replace")

        if self._backup:
            backup_path = path.with_suffix(".html.bak")
            backup_path.write_text(original_html, encoding="utf-8")
            log.debug("WikiUpdater: backup written to %s", backup_path)

        html = original_html
        updated: list[str] = []
        errors: list[str] = []

        for section_id, new_inner in new_content_sections.items():
            try:
                html = _replace_section_content(html, section_id, new_inner)
                updated.append(section_id)
                log.debug("WikiUpdater: updated section '%s' in %s", section_id, node_path)
            except ValueError:
                # Section doesn't exist — append it before </body> or at end
                log.warning(
                    "WikiUpdater: section '%s' not found in %s — appending",
                    section_id, node_path,
                )
                new_block = _build_section_block(section_id, new_inner)
                if "</body>" in html:
                    html = html.replace("</body>", f"{new_block}\n</body>", 1)
                else:
                    html += f"\n{new_block}"
                updated.append(section_id)

        if errors:
            result.error = "; ".join(errors)

        path.write_text(html, encoding="utf-8")
        _anchor_html_document(path.stem, html, {"sections": new_content_sections, "source": "wiki-update"})
        result.success = True
        result.sections_updated = updated
        log.info(
            "WikiUpdater: updated %d section(s) in %s",
            len(updated), node_path,
        )
        return result


    def get_section(self, node_path: str, section_id: str) -> str | None:
        """
        Extract the inner content of a section by id.

        Returns None if the node file or section does not exist.
        """
        path = Path(node_path)
        if not path.exists():
            return None
        html = path.read_text(encoding="utf-8", errors="replace")
        _, _, inner = _extract_section(html, section_id)
        return inner if inner else None

    def list_sections(self, node_path: str) -> list[str]:
        """Return all section ids present in the given HTML node."""
        path = Path(node_path)
        if not path.exists():
            return []
        html = path.read_text(encoding="utf-8", errors="replace")
        return re.findall(r'<section[^>]+\bid=["\']([^"\']+)["\']', html, re.I)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _scaffold_node(title: str, sections: dict[str, str]) -> str:
        """Build a minimal HTML Neural Node scaffold."""
        section_html = "\n".join(
            _build_section_block(sid, content)
            for sid, content in sections.items()
        )
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="generator" content="Tala WikiUpdater">
  <meta name="created" content="{datetime.now(UTC).isoformat()}">
  <title>Neural Node: {title}</title>
</head>
<body>
<h1>{title}</h1>
{section_html}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# SelfHealingDocs
# ---------------------------------------------------------------------------


class SelfHealingDocs:
    """
    Compares actual resource structure to its documented Neural Node and
    produces a list of Corrections.

    The 'resource' can be any inspectable Python module, JSON config, or
    directory.  The 'node' is the HTML file that documents it.

    Comparison strategy:
      1. Extract declared structure from the resource (functions, classes, etc.)
      2. Extract documented structure from the HTML node (section contents)
      3. Diff the two and return Correction objects for every discrepancy.
    """

    def __init__(self, updater: WikiUpdater | None = None) -> None:
        self._updater = updater or WikiUpdater()

    def run_comparative_check(
        self,
        resource_path: str,
        node_path: str,
    ) -> list[Correction]:
        """
        Compare the actual structure of *resource_path* to its documentation
        in *node_path*.

        Parameters
        ----------
        resource_path:
            Path to a Python source file or directory to inspect.
        node_path:
            Path to the HTML Neural Node that documents it.

        Returns
        -------
        list[Correction]
            Empty list means docs are in sync with reality.
        """
        corrections: list[Correction] = []

        resource = Path(resource_path)
        node = Path(node_path)

        # ── Node existence check ───────────────────────────────────────────
        if not node.exists():
            corrections.append(Correction(
                field="node_file",
                expected=node_path,
                actual="[missing]",
                severity=CorrectionSeverity.CRITICAL,
                node_path=node_path,
                suggested_fix=f"Create Neural Node at {node_path}",
            ))
            return corrections  # Can't do further comparison without the node

        # ── Resource existence check ───────────────────────────────────────
        if not resource.exists():
            corrections.append(Correction(
                field="resource_file",
                expected=resource_path,
                actual="[missing]",
                severity=CorrectionSeverity.CRITICAL,
                node_path=node_path,
                suggested_fix=f"Create resource at {resource_path}",
            ))
            return corrections

        # ── Python module comparison ───────────────────────────────────────
        if resource.suffix == ".py":
            corrections.extend(
                self._check_python_module(resource, node_path)
            )
        elif resource.is_dir():
            corrections.extend(
                self._check_directory(resource, node_path)
            )
        else:
            log.debug(
                "SelfHealingDocs: resource type %r not specifically handled — "
                "performing generic check",
                resource.suffix,
            )
            corrections.extend(
                self._check_generic_file(resource, node_path)
            )

        if not corrections:
            log.info(
                "SelfHealingDocs: no corrections needed — %s is in sync with %s",
                resource_path, node_path,
            )
        else:
            log.warning(
                "SelfHealingDocs: %d correction(s) needed for %s",
                len(corrections), resource_path,
            )

        return corrections

    # ------------------------------------------------------------------

    def _check_python_module(
        self,
        py_file: Path,
        node_path: str,
    ) -> list[Correction]:
        """Parse *py_file* via AST and compare to the docs section."""
        import ast

        corrections: list[Correction] = []
        source = py_file.read_text(encoding="utf-8", errors="replace")

        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            corrections.append(Correction(
                field="syntax",
                expected="valid Python",
                actual=str(exc),
                severity=CorrectionSeverity.CRITICAL,
                node_path=node_path,
                suggested_fix="Fix syntax error before updating docs",
            ))
            return corrections

        # Collect real symbols
        real_functions = [
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        real_classes = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
        ]

        # Extract documented symbols from node
        doc_functions_raw = self._updater.get_section(node_path, "functions") or ""
        doc_classes_raw = self._updater.get_section(node_path, "classes") or ""

        doc_functions = set(re.findall(r'\b([a-z_][a-zA-Z0-9_]*)\b', doc_functions_raw))
        doc_classes = set(re.findall(r'\b([A-Z][a-zA-Z0-9_]*)\b', doc_classes_raw))

        # Functions missing from docs
        for fn in real_functions:
            if fn not in doc_functions and not fn.startswith("_"):
                corrections.append(Correction(
                    field=f"functions.{fn}",
                    expected=f"documented in node section 'functions'",
                    actual="[not documented]",
                    severity=CorrectionSeverity.WARNING,
                    node_path=node_path,
                    suggested_fix=f"Add <code>{fn}</code> to the 'functions' section",
                ))

        # Classes missing from docs
        for cls in real_classes:
            if cls not in doc_classes:
                corrections.append(Correction(
                    field=f"classes.{cls}",
                    expected=f"documented in node section 'classes'",
                    actual="[not documented]",
                    severity=CorrectionSeverity.WARNING,
                    node_path=node_path,
                    suggested_fix=f"Add <code>{cls}</code> to the 'classes' section",
                ))

        return corrections

    def _check_directory(
        self,
        directory: Path,
        node_path: str,
    ) -> list[Correction]:
        """Check that all Python files in *directory* are referenced in the node."""
        corrections: list[Correction] = []
        doc_content = Path(node_path).read_text(encoding="utf-8", errors="replace")

        for py_file in sorted(directory.glob("*.py")):
            stem = py_file.stem
            if stem.startswith("_"):
                continue
            if stem not in doc_content:
                corrections.append(Correction(
                    field=f"module.{stem}",
                    expected=f"referenced in {node_path}",
                    actual="[not mentioned]",
                    severity=CorrectionSeverity.INFO,
                    node_path=node_path,
                    suggested_fix=f"Add reference to module <code>{stem}.py</code>",
                ))

        return corrections

    def _check_generic_file(
        self,
        resource: Path,
        node_path: str,
    ) -> list[Correction]:
        """Minimal check for non-Python resources."""
        corrections: list[Correction] = []
        doc_content = Path(node_path).read_text(encoding="utf-8", errors="replace")
        if resource.name not in doc_content:
            corrections.append(Correction(
                field="resource_reference",
                expected=f"{resource.name} referenced in docs",
                actual="[not mentioned]",
                severity=CorrectionSeverity.INFO,
                node_path=node_path,
                suggested_fix=f"Add reference to {resource.name} in the node",
            ))
        return corrections


# ---------------------------------------------------------------------------
# apply_corrections — top-level function
# ---------------------------------------------------------------------------


def apply_corrections(
    corrections: list[Correction],
    node_path: str,
    updater: WikiUpdater | None = None,
) -> None:
    """
    Write all *corrections* into the HTML node at *node_path*.

    This function must be called BEFORE executing any technical change so
    that the knowledge graph leads implementation.

    Parameters
    ----------
    corrections:
        List of Correction objects from SelfHealingDocs.run_comparative_check().
    node_path:
        Path to the HTML Neural Node to update.
    updater:
        WikiUpdater instance (a new one is created if not provided).
    """
    if not corrections:
        log.debug("apply_corrections: no corrections to apply — skipping")
        return

    _updater = updater or WikiUpdater()

    # Group corrections by field into a section update
    timestamp = datetime.now(UTC).isoformat()
    corrections_html_lines = [
        f"<p><em>Last auto-correction run: {timestamp}</em></p>",
        "<ul>",
    ]
    for correction in corrections:
        level_class = correction.severity.value.lower()
        corrections_html_lines.append(
            f'  <li class="correction-{level_class}">'
            f"<strong>[{correction.severity.value}]</strong> "
            f"<code>{correction.field}</code>: "
            f"expected {correction.expected!r}, found {correction.actual!r}. "
            f"<em>{correction.suggested_fix}</em>"
            f"</li>"
        )
    corrections_html_lines.append("</ul>")
    corrections_html = "\n".join(corrections_html_lines)

    result = _updater.update_node(
        node_path,
        {"corrections": corrections_html},
    )

    if result.success:
        log.info(
            "apply_corrections: wrote %d correction(s) to %s",
            len(corrections), node_path,
        )
    else:
        log.error(
            "apply_corrections: failed to write to %s — %s",
            node_path, result.error,
        )


# ---------------------------------------------------------------------------
# HTML Neural Node Parser (Milestone 6)
# ---------------------------------------------------------------------------

class HTMLNNParser:
    """
    Parses and serializes HTML Neural Nodes, extracting metadata and relations.
    """

    @staticmethod
    def parse_node_metadata(html_content: str) -> dict[str, Any]:
        """
        Parse and return custom article tags, JSON state script tags, and links.
        """
        meta = {
            "node_id": "",
            "node_type": "",
            "execution_authority": "",
            "title": "",
            "relations": [],
            "memory_type": "",
        }

        # 1. Parse article attributes
        article_match = re.search(
            r"<article[^>]*data-node-id=[\"']([^\"']+)[\"'][^>]*>",
            html_content,
            re.IGNORECASE
        )
        if article_match:
            meta["node_id"] = article_match.group(1)

        type_match = re.search(
            r"data-node-type=[\"']([^\"']+)[\"']",
            html_content,
            re.IGNORECASE
        )
        if type_match:
            meta["node_type"] = type_match.group(1)

        auth_match = re.search(
            r"data-execution-authority=[\"']([^\"']+)[\"']",
            html_content,
            re.IGNORECASE
        )
        if auth_match:
            meta["execution_authority"] = auth_match.group(1)

        mem_type_match = re.search(
            r"data-memory-type=[\"']([^\"']+)[\"']",
            html_content,
            re.IGNORECASE
        )
        if mem_type_match:
            meta["memory_type"] = mem_type_match.group(1)
        else:
            # Fallback to plaintext header Type: ...
            plain_match = re.search(r"(?i)Type:\s*([^\n<]+)", html_content)
            if plain_match:
                meta["memory_type"] = plain_match.group(1).strip()

        # 2. Parse title
        title_match = re.search(r"<title>(.*?)</title>", html_content, re.IGNORECASE)
        if title_match:
            meta["title"] = title_match.group(1)

        # 3. Parse relationship links (looks for file scheme links)
        links = re.findall(r"<a[^>]*href=[\"'](file:///[^\"']+)[\"'][^>]*>(.*?)</a>", html_content, re.IGNORECASE)
        for link_url, link_text in links:
            meta["relations"].append({
                "target": link_url,
                "text": link_text.strip(),
            })

        return meta



class WikiGenerator:
    """
    Compiles maps, capabilities, and system schemas into self-documenting wiki nodes.
    """

    @staticmethod
    def compile_codebase_map_node(
        node_id: str,
        title: str,
        capabilities: dict[str, list[str]],
        ipc_handlers: list[str],
    ) -> str:
        """
        Compiles codebase structure into a structured HTML Neural Node.
        """
        caps_html = []
        for cap_name, files in capabilities.items():
            file_links = "".join(f"<li><code>{f}</code></li>" for f in files)
            caps_html.append(f"""
      <h3>Capability: {cap_name.capitalize()}</h3>
      <ul>
        {file_links}
      </ul>
            """)

        handlers_links = "".join(f"<li><code>{h}</code></li>" for h in ipc_handlers)
        
        timestamp = datetime.now(UTC).isoformat()
        
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Tala Wiki: {title}</title>
</head>
<body>
  <article data-node-id="{node_id}" data-node-type="resource" data-execution-authority="0.25">
    <h1>{title}</h1>
    
    <section id="node-identity">
      <p><strong>Last Compiled:</strong> {timestamp}</p>
    </section>

    <section id="capabilities-map">
      <h2>System Capabilities</h2>
      {"".join(caps_html)}
    </section>

    <section id="ipc-endpoints">
      <h2>Exposed IPC Endpoints</h2>
      <ul>
        {handlers_links}
      </ul>
    </section>

  </article>
</body>
</html>
"""
