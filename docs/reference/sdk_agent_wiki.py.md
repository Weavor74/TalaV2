# File: `wiki.py`

## Module Description

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

## Classes

### `class CorrectionSeverity`
How badly out-of-date a documented field is.


### `class Correction`
Describes a single discrepancy between documentation and reality.

- **`def __str__(self)`**

### `class NodeUpdateResult`
Result of a WikiUpdater.update_node() call.


### `class WikiUpdater`
Rewrites specific named sections inside an HTML Neural Node file.

Neural Nodes are structured HTML files where each logical block of content
is wrapped in ``<section id="...">`` tags.  WikiUpdater targets only the
sections that have changed, leaving the rest of the document intact.

Parameters
----------
backup:
    When True (default), a ``.bak`` copy of the original file is saved
    alongside the updated file before any change is applied.

- **`def __init__(self, backup)`**
- **`def update_node(self, node_path, new_content_sections)`**
  - Rewrite specific sections of an HTML Neural Node.
  
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
- **`def get_section(self, node_path, section_id)`**
  - Extract the inner content of a section by id.
  
  Returns None if the node file or section does not exist.
- **`def list_sections(self, node_path)`**
  - Return all section ids present in the given HTML node.
- **`def _scaffold_node(title, sections)`**
  - Build a minimal HTML Neural Node scaffold.

### `class SelfHealingDocs`
Compares actual resource structure to its documented Neural Node and
produces a list of Corrections.

The 'resource' can be any inspectable Python module, JSON config, or
directory.  The 'node' is the HTML file that documents it.

Comparison strategy:
  1. Extract declared structure from the resource (functions, classes, etc.)
  2. Extract documented structure from the HTML node (section contents)
  3. Diff the two and return Correction objects for every discrepancy.

- **`def __init__(self, updater)`**
- **`def run_comparative_check(self, resource_path, node_path)`**
  - Compare the actual structure of *resource_path* to its documentation
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
- **`def _check_python_module(self, py_file, node_path)`**
  - Parse *py_file* via AST and compare to the docs section.
- **`def _check_directory(self, directory, node_path)`**
  - Check that all Python files in *directory* are referenced in the node.
- **`def _check_generic_file(self, resource, node_path)`**
  - Minimal check for non-Python resources.

### `class HTMLNNParser`
Parses and serializes HTML Neural Nodes, extracting metadata and relations.

- **`def parse_node_metadata(html_content)`**
  - Parse and return custom article tags, JSON state script tags, and links.

### `class WikiGenerator`
Compiles maps, capabilities, and system schemas into self-documenting wiki nodes.

- **`def compile_codebase_map_node(node_id, title, capabilities, ipc_handlers)`**
  - Compiles codebase structure into a structured HTML Neural Node.

## Functions

### `def _anchor_html_document(node_id, html, state_data)`
*No docstring provided.*

### `def _extract_section(html, section_id)`
Locate a ``<section id="section_id">...</section>`` block in *html*.

Returns (start_idx, end_idx, inner_content) or (-1, -1, '') if not found.
The indices span the opening tag through the closing ``</section>``.

### `def _replace_section_content(html, section_id, new_inner)`
Replace the inner content of ``<section id="section_id">`` in *html*.

Returns the modified HTML string.  Raises ValueError if the section
is not found.

### `def _build_section_block(section_id, content)`
Return a new section HTML block.

### `def apply_corrections(corrections, node_path, updater)`
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

## Source Code

```python
"""\nsdk/agent/wiki.py\n=================\nKnowledge Graph & Self-Healing Docs — Tala Cognitive Engine Room\nMilestone 6 | Task 6.4.1\n\nProvides:\n  • WikiUpdater      — rewrites specific sections of HTML Neural Nodes\n  • SelfHealingDocs  — compares actual resource structure to docs, finds gaps\n  • Correction       — dataclass describing a documentation discrepancy\n  • apply_corrections() — writes corrections to the node before code changes\n\nDesign principle:\n  Documentation is updated BEFORE executing a technical change, never after.\n  This ensures the knowledge graph always leads implementation rather than\n  lagging behind it.\n"""\n\nfrom __future__ import annotations\n\nimport logging\nimport re\nfrom dataclasses import dataclass, field\nfrom datetime import UTC, datetime\nfrom enum import Enum, auto\nfrom pathlib import Path\nfrom typing import Any\n\nfrom sdk.agent.anchor import canonicalize_and_anchor\n\nlog = logging.getLogger(__name__)\n\n\ndef _anchor_html_document(node_id: str, html: str, state_data: dict[str, Any]) -> None:\n    try:\n        canonicalize_and_anchor(\n            node_id,\n            html,\n            state_data,\n            None,\n            creator="tala-system",\n            document_type="html-nn",\n        )\n    except Exception as exc:\n        log.debug("WikiUpdater: failed to anchor %s on blockchain: %s", node_id, exc)\n\n\n# ---------------------------------------------------------------------------\n# Enums\n# ---------------------------------------------------------------------------\n\n\nclass CorrectionSeverity(Enum):\n    """How badly out-of-date a documented field is."""\n    INFO = "INFO"          # Minor — cosmetic or additive\n    WARNING = "WARNING"    # Moderate — missing or stale reference\n    CRITICAL = "CRITICAL"  # Severe — structural mismatch or missing node\n\n\n# ---------------------------------------------------------------------------\n# Data classes\n# ---------------------------------------------------------------------------\n\n\n@dataclass\nclass Correction:\n    """Describes a single discrepancy between documentation and reality."""\n\n    field: str                         # Dotted path: e.g. 'functions.decompose'\n    expected: str                      # What the docs say\n    actual: str                        # What reality shows\n    severity: CorrectionSeverity = CorrectionSeverity.WARNING\n    node_path: str = ""               # Path to the HTML node to correct\n    suggested_fix: str = ""           # Auto-generated correction text\n    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n\n    def __str__(self) -> str:\n        return (\n            f"[{self.severity.value}] {self.field}: "\n            f"expected={self.expected!r}, actual={self.actual!r}"\n        )\n\n\n@dataclass\nclass NodeUpdateResult:\n    """Result of a WikiUpdater.update_node() call."""\n\n    node_path: str\n    success: bool\n    sections_updated: list[str] = field(default_factory=list)\n    error: str | None = None\n    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n\n\n# ---------------------------------------------------------------------------\n# HTML Section Manipulation Helpers\n# ---------------------------------------------------------------------------\n\n\ndef _extract_section(html: str, section_id: str) -> tuple[int, int, str]:\n    """\n    Locate a ``<section id="section_id">...</section>`` block in *html*.\n\n    Returns (start_idx, end_idx, inner_content) or (-1, -1, '') if not found.\n    The indices span the opening tag through the closing ``</section>``.\n    """\n    # Match opening tag with the given id (attributes may appear in any order)\n    open_pattern = re.compile(\n        r'<section[^>]*\bid=["\']' + re.escape(section_id) + r'["\'][^>]*>',\n        re.IGNORECASE | re.DOTALL,\n    )\n    m_open = open_pattern.search(html)\n    if not m_open:\n        return -1, -1, ""\n\n    start = m_open.start()\n    # Find the matching </section> — handle nesting\n    depth = 1\n    pos = m_open.end()\n    while pos < len(html) and depth > 0:\n        next_open = html.find("<section", pos)\n        next_close = html.find("</section>", pos)\n        if next_close == -1:\n            break\n        if next_open != -1 and next_open < next_close:\n            depth += 1\n            pos = next_open + len("<section")\n        else:\n            depth -= 1\n            if depth == 0:\n                end = next_close + len("</section>")\n                inner = html[m_open.end(): next_close]\n                return start, end, inner\n            pos = next_close + len("</section>")\n\n    return -1, -1, ""\n\n\ndef _replace_section_content(html: str, section_id: str, new_inner: str) -> str:\n    """\n    Replace the inner content of ``<section id="section_id">`` in *html*.\n\n    Returns the modified HTML string.  Raises ValueError if the section\n    is not found.\n    """\n    start, end, old_inner = _extract_section(html, section_id)\n    if start == -1:\n        raise ValueError(\n            f"Section with id={section_id!r} not found in HTML content."\n        )\n    # Reconstruct: keep opening tag, inject new inner, add closing tag\n    open_tag_end = html.find(">", start) + 1\n    open_tag = html[start:open_tag_end]\n    return html[:open_tag_end] + new_inner + "</section>" + html[end:]\n\n\ndef _build_section_block(section_id: str, content: str) -> str:\n    """Return a new section HTML block."""\n    return f'<section id="{section_id}">\n{content}\n</section>'\n\n\n# ---------------------------------------------------------------------------\n# WikiUpdater\n# ---------------------------------------------------------------------------\n\n\nclass WikiUpdater:\n    """\n    Rewrites specific named sections inside an HTML Neural Node file.\n\n    Neural Nodes are structured HTML files where each logical block of content\n    is wrapped in ``<section id="...">`` tags.  WikiUpdater targets only the\n    sections that have changed, leaving the rest of the document intact.\n\n    Parameters\n    ----------\n    backup:\n        When True (default), a ``.bak`` copy of the original file is saved\n        alongside the updated file before any change is applied.\n    """\n\n    def __init__(self, backup: bool = True) -> None:\n        self._backup = backup\n\n    def update_node(\n        self,\n        node_path: str,\n        new_content_sections: dict[str, str],\n    ) -> NodeUpdateResult:\n        """\n        Rewrite specific sections of an HTML Neural Node.\n\n        Parameters\n        ----------\n        node_path:\n            Absolute path to the ``.html`` Neural Node file.\n        new_content_sections:\n            Mapping of ``{section_id: new_inner_html}``.  Only sections whose\n            id appears in this dict are modified.\n\n        Returns\n        -------\n        NodeUpdateResult\n        """\n        path = Path(node_path)\n        result = NodeUpdateResult(node_path=node_path, success=False)\n\n        if not path.exists():\n            # If the node doesn't exist yet, create a minimal scaffold\n            log.info("WikiUpdater: node does not exist — creating scaffold: %s", node_path)\n            html = self._scaffold_node(path.stem, new_content_sections)\n            path.parent.mkdir(parents=True, exist_ok=True)\n            path.write_text(html, encoding="utf-8")\n            _anchor_html_document(path.stem, html, {"sections": new_content_sections, "source": "wiki-scaffold"})\n            result.success = True\n            result.sections_updated = list(new_content_sections.keys())\n            log.info("WikiUpdater: scaffolded new node %s", node_path)\n            return result\n\n        original_html = path.read_text(encoding="utf-8", errors="replace")\n\n        if self._backup:\n            backup_path = path.with_suffix(".html.bak")\n            backup_path.write_text(original_html, encoding="utf-8")\n            log.debug("WikiUpdater: backup written to %s", backup_path)\n\n        html = original_html\n        updated: list[str] = []\n        errors: list[str] = []\n\n        for section_id, new_inner in new_content_sections.items():\n            try:\n                html = _replace_section_content(html, section_id, new_inner)\n                updated.append(section_id)\n                log.debug("WikiUpdater: updated section '%s' in %s", section_id, node_path)\n            except ValueError:\n                # Section doesn't exist — append it before </body> or at end\n                log.warning(\n                    "WikiUpdater: section '%s' not found in %s — appending",\n                    section_id, node_path,\n                )\n                new_block = _build_section_block(section_id, new_inner)\n                if "</body>" in html:\n                    html = html.replace("</body>", f"{new_block}\n</body>", 1)\n                else:\n                    html += f"\n{new_block}"\n                updated.append(section_id)\n\n        if errors:\n            result.error = "; ".join(errors)\n\n        path.write_text(html, encoding="utf-8")\n        _anchor_html_document(path.stem, html, {"sections": new_content_sections, "source": "wiki-update"})\n        result.success = True\n        result.sections_updated = updated\n        log.info(\n            "WikiUpdater: updated %d section(s) in %s",\n            len(updated), node_path,\n        )\n        return result\n\n\n    def get_section(self, node_path: str, section_id: str) -> str | None:\n        """\n        Extract the inner content of a section by id.\n\n        Returns None if the node file or section does not exist.\n        """\n        path = Path(node_path)\n        if not path.exists():\n            return None\n        html = path.read_text(encoding="utf-8", errors="replace")\n        _, _, inner = _extract_section(html, section_id)\n        return inner if inner else None\n\n    def list_sections(self, node_path: str) -> list[str]:\n        """Return all section ids present in the given HTML node."""\n        path = Path(node_path)\n        if not path.exists():\n            return []\n        html = path.read_text(encoding="utf-8", errors="replace")\n        return re.findall(r'<section[^>]+\bid=["\']([^"\']+)["\']', html, re.I)\n\n    # ------------------------------------------------------------------\n    # Private helpers\n    # ------------------------------------------------------------------\n\n    @staticmethod\n    def _scaffold_node(title: str, sections: dict[str, str]) -> str:\n        """Build a minimal HTML Neural Node scaffold."""\n        section_html = "\n".join(\n            _build_section_block(sid, content)\n            for sid, content in sections.items()\n        )\n        return f"""<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8">\n  <meta name="generator" content="Tala WikiUpdater">\n  <meta name="created" content="{datetime.now(UTC).isoformat()}">\n  <title>Neural Node: {title}</title>\n</head>\n<body>\n<h1>{title}</h1>\n{section_html}\n</body>\n</html>\n"""\n\n\n# ---------------------------------------------------------------------------\n# SelfHealingDocs\n# ---------------------------------------------------------------------------\n\n\nclass SelfHealingDocs:\n    """\n    Compares actual resource structure to its documented Neural Node and\n    produces a list of Corrections.\n\n    The 'resource' can be any inspectable Python module, JSON config, or\n    directory.  The 'node' is the HTML file that documents it.\n\n    Comparison strategy:\n      1. Extract declared structure from the resource (functions, classes, etc.)\n      2. Extract documented structure from the HTML node (section contents)\n      3. Diff the two and return Correction objects for every discrepancy.\n    """\n\n    def __init__(self, updater: WikiUpdater | None = None) -> None:\n        self._updater = updater or WikiUpdater()\n\n    def run_comparative_check(\n        self,\n        resource_path: str,\n        node_path: str,\n    ) -> list[Correction]:\n        """\n        Compare the actual structure of *resource_path* to its documentation\n        in *node_path*.\n\n        Parameters\n        ----------\n        resource_path:\n            Path to a Python source file or directory to inspect.\n        node_path:\n            Path to the HTML Neural Node that documents it.\n\n        Returns\n        -------\n        list[Correction]\n            Empty list means docs are in sync with reality.\n        """\n        corrections: list[Correction] = []\n\n        resource = Path(resource_path)\n        node = Path(node_path)\n\n        # ── Node existence check ───────────────────────────────────────────\n        if not node.exists():\n            corrections.append(Correction(\n                field="node_file",\n                expected=node_path,\n                actual="[missing]",\n                severity=CorrectionSeverity.CRITICAL,\n                node_path=node_path,\n                suggested_fix=f"Create Neural Node at {node_path}",\n            ))\n            return corrections  # Can't do further comparison without the node\n\n        # ── Resource existence check ───────────────────────────────────────\n        if not resource.exists():\n            corrections.append(Correction(\n                field="resource_file",\n                expected=resource_path,\n                actual="[missing]",\n                severity=CorrectionSeverity.CRITICAL,\n                node_path=node_path,\n                suggested_fix=f"Create resource at {resource_path}",\n            ))\n            return corrections\n\n        # ── Python module comparison ───────────────────────────────────────\n        if resource.suffix == ".py":\n            corrections.extend(\n                self._check_python_module(resource, node_path)\n            )\n        elif resource.is_dir():\n            corrections.extend(\n                self._check_directory(resource, node_path)\n            )\n        else:\n            log.debug(\n                "SelfHealingDocs: resource type %r not specifically handled — "\n                "performing generic check",\n                resource.suffix,\n            )\n            corrections.extend(\n                self._check_generic_file(resource, node_path)\n            )\n\n        if not corrections:\n            log.info(\n                "SelfHealingDocs: no corrections needed — %s is in sync with %s",\n                resource_path, node_path,\n            )\n        else:\n            log.warning(\n                "SelfHealingDocs: %d correction(s) needed for %s",\n                len(corrections), resource_path,\n            )\n\n        return corrections\n\n    # ------------------------------------------------------------------\n\n    def _check_python_module(\n        self,\n        py_file: Path,\n        node_path: str,\n    ) -> list[Correction]:\n        """Parse *py_file* via AST and compare to the docs section."""\n        import ast\n\n        corrections: list[Correction] = []\n        source = py_file.read_text(encoding="utf-8", errors="replace")\n\n        try:\n            tree = ast.parse(source)\n        except SyntaxError as exc:\n            corrections.append(Correction(\n                field="syntax",\n                expected="valid Python",\n                actual=str(exc),\n                severity=CorrectionSeverity.CRITICAL,\n                node_path=node_path,\n                suggested_fix="Fix syntax error before updating docs",\n            ))\n            return corrections\n\n        # Collect real symbols\n        real_functions = [\n            n.name for n in ast.walk(tree)\n            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))\n        ]\n        real_classes = [\n            n.name for n in ast.walk(tree)\n            if isinstance(n, ast.ClassDef)\n        ]\n\n        # Extract documented symbols from node\n        doc_functions_raw = self._updater.get_section(node_path, "functions") or ""\n        doc_classes_raw = self._updater.get_section(node_path, "classes") or ""\n\n        doc_functions = set(re.findall(r'\b([a-z_][a-zA-Z0-9_]*)\b', doc_functions_raw))\n        doc_classes = set(re.findall(r'\b([A-Z][a-zA-Z0-9_]*)\b', doc_classes_raw))\n\n        # Functions missing from docs\n        for fn in real_functions:\n            if fn not in doc_functions and not fn.startswith("_"):\n                corrections.append(Correction(\n                    field=f"functions.{fn}",\n                    expected=f"documented in node section 'functions'",\n                    actual="[not documented]",\n                    severity=CorrectionSeverity.WARNING,\n                    node_path=node_path,\n                    suggested_fix=f"Add <code>{fn}</code> to the 'functions' section",\n                ))\n\n        # Classes missing from docs\n        for cls in real_classes:\n            if cls not in doc_classes:\n                corrections.append(Correction(\n                    field=f"classes.{cls}",\n                    expected=f"documented in node section 'classes'",\n                    actual="[not documented]",\n                    severity=CorrectionSeverity.WARNING,\n                    node_path=node_path,\n                    suggested_fix=f"Add <code>{cls}</code> to the 'classes' section",\n                ))\n\n        return corrections\n\n    def _check_directory(\n        self,\n        directory: Path,\n        node_path: str,\n    ) -> list[Correction]:\n        """Check that all Python files in *directory* are referenced in the node."""\n        corrections: list[Correction] = []\n        doc_content = Path(node_path).read_text(encoding="utf-8", errors="replace")\n\n        for py_file in sorted(directory.glob("*.py")):\n            stem = py_file.stem\n            if stem.startswith("_"):\n                continue\n            if stem not in doc_content:\n                corrections.append(Correction(\n                    field=f"module.{stem}",\n                    expected=f"referenced in {node_path}",\n... (file truncated, showing first 500 lines) ...
```
