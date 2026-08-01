"""
documentation_nodes.py - Auto-generated documentation base.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from lxml import html as lxml_html

from sdk.agent.anchor import canonicalize_and_anchor
from sdk.agent.parser import parse_file


ALLOWED_NODE_TYPES = {"documentation", "program-doc", "function-doc"}
DEFAULT_REFERENCE_NODES = (
    "system-neural-node-v2-schema",
    "system-html-nn-authoring-guide",
)
CODEBASE_DOCUMENTATION_TARGETS = (
    "project.overview",
    "electron.shell",
    "renderer.ui",
    "python.harness",
    "tool.registry",
    "html-nn.runtime",
    "documentation.generator",
    "self-improvement.system",
    "settings.inference",
    "workflow",
    "blockchain.ledger",
    "vault.profile",
    "tests.validation",
)
CODEBASE_TARGET_ALIASES = {
    "all",
    "codebase",
    "entire-codebase",
    "project",
    "project.documentation",
    "code.documentation",
}
BUILTIN_DOCUMENTATION_TARGETS = CODEBASE_DOCUMENTATION_TARGETS + (
    "chat.panel",
    "workflow.builder",
    "workflow.executor",
    "bridges",
    "tool-contracts",
    "self-improvement.bridges",
)
LEARNING_DOCUMENTATION_TARGET = "learning.event"


@dataclass(frozen=True)
class DocumentationNodeResult:
    created: bool
    updated: bool
    unchanged: bool
    errors: list[str]
    file_name: str
    node_id: str
    node_type: str
    doc_target: str
    validation_passed: bool
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "file_name": self.file_name,
            "node_id": self.node_id,
            "node_type": self.node_type,
            "doc_target": self.doc_target,
            "validation_passed": self.validation_passed,
            "path": self.path,
        }


def resolve_active_nodes_dir() -> Path:
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        return Path(user_data).expanduser().resolve() / "nodes"

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata).expanduser().resolve() / "tala-engine" / "nodes"

    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"

    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config).expanduser().resolve() / "tala-engine" / "nodes"

    if os.uname().sysname == "Darwin":
        return Path.home() / "Library" / "Application Support" / "tala-engine" / "nodes"

    return Path.home() / ".config" / "tala-engine" / "nodes"


def _safe_node_id(node_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", node_id.strip()).strip("-_").lower()
    if not cleaned:
        raise ValueError("node_id must contain at least one alphanumeric character")
    return cleaned


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalize_tags_list(value: Any, fallback: list[str] | None = None) -> list[str]:
    tags = _normalize_string_list(value)
    if tags:
        return tags
    return list(fallback or [])


def _normalize_relationships(relationships: Any) -> list[dict[str, Any]]:
    if not isinstance(relationships, list):
        return []

    normalized: list[dict[str, Any]] = []
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        target_id = _safe_node_id(str(rel.get("target_node_id", "")))
        label = str(rel.get("label") or target_id).strip()
        relationship_type = str(rel.get("relationship_type") or "references").strip()
        navigation_role = str(rel.get("navigation_role") or "related-context").strip()
        try:
            weight = float(rel.get("relationship_weight", 0.80))
        except (TypeError, ValueError):
            weight = 0.80
        weight = min(1.0, max(0.0, weight))
        normalized.append(
            {
                "target_node_id": target_id,
                "label": label,
                "relationship_type": relationship_type,
                "relationship_weight": weight,
                "navigation_role": navigation_role,
            }
        )
    return normalized


def _safe_doc_target(doc_target: str) -> str:
    cleaned = str(doc_target or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", cleaned):
        raise ValueError("doc_target must use lowercase letters, numbers, dots, dashes, or underscores")
    return cleaned


def _registry_path() -> Path:
    return resolve_active_nodes_dir().parent / "documentation-targets.json"


def _load_registered_documentation_targets() -> dict[str, dict[str, Any]]:
    path = _registry_path()
    if not path.exists():
        return {}

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("documentation target registry must be a JSON object")

    targets = payload.get("targets", {})
    if not isinstance(targets, dict):
        raise ValueError("documentation target registry must contain a targets object")

    registered: dict[str, dict[str, Any]] = {}
    for doc_target, spec in targets.items():
        if not isinstance(spec, dict):
            continue
        safe_target = _safe_doc_target(doc_target)
        registered[safe_target] = {
            "node_id": _safe_node_id(str(spec.get("node_id") or f"program-doc-{safe_target.replace('.', '-')}")),
            "title": str(spec.get("title") or safe_target.replace(".", " ").replace("-", " ").title()).strip(),
            "doc_target": safe_target,
            "semantic_memory": str(spec.get("semantic_memory") or "").strip(),
            "source_files": _normalize_string_list(spec.get("source_files")),
            "references": _normalize_string_list(spec.get("references")),
            "relationships": _normalize_relationships(spec.get("relationships")),
            "include_in_codebase": bool(spec.get("include_in_codebase", True)),
            "registered": True,
            "registered_at": str(spec.get("registered_at") or ""),
        }
    return registered


def _save_registered_documentation_targets(targets: dict[str, dict[str, Any]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "documentation-target-registry/v1",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "targets": targets,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _documentation_create_kwargs(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": spec.get("node_id", ""),
        "title": spec.get("title", ""),
        "doc_target": spec.get("doc_target", ""),
        "semantic_memory": spec.get("semantic_memory", ""),
        "source_files": spec.get("source_files"),
        "references": spec.get("references"),
        "relationships": spec.get("relationships"),
    }


def register_program_documentation_target(
    *,
    doc_target: str,
    title: str,
    semantic_memory: str,
    node_id: str | None = None,
    source_files: Any = None,
    references: Any = None,
    relationships: Any = None,
    include_in_codebase: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    safe_target = _safe_doc_target(doc_target)
    if safe_target in BUILTIN_DOCUMENTATION_TARGETS or safe_target in CODEBASE_TARGET_ALIASES:
        raise ValueError(f"Cannot register built-in documentation target: {safe_target}")

    semantic_memory = str(semantic_memory or "").strip()
    if not semantic_memory:
        raise ValueError("semantic_memory is required")

    title = str(title or "").strip() or safe_target.replace(".", " ").replace("-", " ").title()
    safe_node_id = _safe_node_id(node_id or f"program-doc-{safe_target.replace('.', '-')}")
    registered = _load_registered_documentation_targets()
    if safe_target in registered and not overwrite:
        raise ValueError(f"Documentation target already registered: {safe_target}")

    spec = {
        "node_id": safe_node_id,
        "title": title,
        "doc_target": safe_target,
        "semantic_memory": semantic_memory,
        "source_files": _normalize_string_list(source_files),
        "references": _normalize_string_list(references),
        "relationships": _normalize_relationships(relationships),
        "include_in_codebase": bool(include_in_codebase),
        "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    registered[safe_target] = spec
    _save_registered_documentation_targets(registered)

    result = create_documentation_node(**_documentation_create_kwargs(spec))
    return {
        "success": result.get("validation_passed", False),
        "registered": True,
        "registry_path": str(_registry_path()),
        "target": spec,
        "node": result,
        "errors": result.get("errors", []),
    }


def list_program_documentation_targets() -> dict[str, Any]:
    registered = _load_registered_documentation_targets()
    builtin_targets = sorted(set(BUILTIN_DOCUMENTATION_TARGETS))
    return {
        "success": True,
        "registry_path": str(_registry_path()),
        "aliases": sorted(CODEBASE_TARGET_ALIASES),
        "builtin_targets": builtin_targets,
        "registered_targets": sorted(registered.keys()),
        "codebase_targets": list(CODEBASE_DOCUMENTATION_TARGETS)
        + sorted(target for target, spec in registered.items() if spec.get("include_in_codebase", True)),
        "targets": [
            {"doc_target": target, "source": "builtin", "include_in_codebase": target in CODEBASE_DOCUMENTATION_TARGETS}
            for target in builtin_targets
        ]
        + [
            {
                "doc_target": target,
                "source": "registered",
                "node_id": spec.get("node_id"),
                "title": spec.get("title"),
                "include_in_codebase": bool(spec.get("include_in_codebase", True)),
            }
            for target, spec in sorted(registered.items())
        ],
    }


def _render_paragraphs(text: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    if not paragraphs:
        paragraphs = ["This documentation node has no semantic content yet."]
    return "\n".join(f"    <p>{escape(paragraph)}</p>" for paragraph in paragraphs)


def _source_fingerprint(source_files: list[str], semantic_memory: str, doc_target: str) -> str:
    payload = json.dumps(
        {
            "source_files": source_files,
            "semantic_memory": semantic_memory,
            "doc_target": doc_target,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest().upper()}"


def _render_relationships(relationships: list[dict[str, Any]]) -> str:
    if not relationships:
        relationships = [
            {
                "target_node_id": "system-neural-node-v2-schema",
                "label": "HTML Neural Node v2 Schema",
                "relationship_type": "bounded-by",
                "relationship_weight": 1.0,
                "navigation_role": "schema-contract",
            },
            {
                "target_node_id": "system-html-nn-authoring-guide",
                "label": "HTML-NN Authoring Guide",
                "relationship_type": "depends-on",
                "relationship_weight": 1.0,
                "navigation_role": "authoring-guide",
            },
        ]

    items = []
    for rel in relationships:
        target_id = rel["target_node_id"]
        items.append(
            "      <li>"
            f'<a href="./{escape(target_id)}.html" '
            f'data-target-node-id="{escape(target_id)}" '
            f'data-relationship-type="{escape(rel["relationship_type"])}" '
            f'data-relationship-weight="{rel["relationship_weight"]:.2f}" '
            f'data-navigation-role="{escape(rel["navigation_role"])}" '
            'data-authority-required="none">'
            f"{escape(rel['label'])}</a></li>"
        )
    return "\n".join(items)


def _render_director(node_id: str, doc_target: str, references: list[str]) -> str:
    payload = {
        "when": {"intent": "reference_documentation_node", "doc_target": doc_target},
        "reference": [
            {"type": "html-nn", "node_id": reference, "required": True}
            for reference in references
        ],
        "then": {
            "instruction": "Use these reference nodes as canonical context before updating or validating this documentation node."
        },
    }
    return (
        '  <script type="application/tala-director+json"\n'
        f'          id="director-reference-{escape(node_id)}"\n'
        '          data-script-kind="reference"\n'
        '          data-side-effects="none">\n'
        f"{json.dumps(payload, indent=2)}\n"
        "  </script>"
    )


def _validate_director_blocks(path: Path) -> None:
    doc = lxml_html.fromstring(path.read_text(encoding="utf-8"))
    for script in doc.xpath('.//script[@type="application/tala-director+json"]'):
        payload = json.loads((script.text or "").strip())
        if not isinstance(payload, dict):
            raise ValueError("director block must contain a JSON object")
        side_effects = script.get("data-side-effects")
        if side_effects not in {"none", "request-only"}:
            raise ValueError(f"invalid director side effects: {side_effects}")


def create_documentation_node(
    *,
    node_id: str,
    title: str,
    doc_target: str,
    semantic_memory: str,
    node_type: str = "documentation",
    source_files: Any = None,
    relationships: Any = None,
    references: Any = None,
    tags: Any = None,
) -> dict[str, Any]:
    safe_node_id = _safe_node_id(node_id)
    node_type = str(node_type or "documentation").strip()
    if node_type not in ALLOWED_NODE_TYPES:
        raise ValueError(f"node_type must be one of {sorted(ALLOWED_NODE_TYPES)}")

    doc_target = str(doc_target or "").strip()
    if not doc_target:
        raise ValueError("doc_target is required")

    title = str(title or "").strip() or safe_node_id.replace("-", " ").title()
    semantic_memory = str(semantic_memory or "").strip()
    if not semantic_memory:
        raise ValueError("semantic_memory is required")

    source_files_list = _normalize_string_list(source_files)
    reference_nodes = _normalize_string_list(references) or list(DEFAULT_REFERENCE_NODES)
    for default_reference in DEFAULT_REFERENCE_NODES:
        if default_reference not in reference_nodes:
            reference_nodes.append(default_reference)

    relationship_list = _normalize_relationships(relationships)
    fingerprint = _source_fingerprint(source_files_list, semantic_memory, doc_target)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    nodes_dir = resolve_active_nodes_dir()
    nodes_dir.mkdir(parents=True, exist_ok=True)
    node_path = nodes_dir / f"{safe_node_id}.html"
    previous_content = node_path.read_text(encoding="utf-8") if node_path.exists() else None

    state_data = {
        "title": title,
        "description": f"Program documentation for {doc_target}.",
        "tags": _normalize_tags_list(tags, ["html-nn", "documentation", "program-doc"]),
        "metadata": {
            "author": "tala-system",
            "created_or_updated": generated_at,
            "schema": "neural-node/v2",
            "doc_target": doc_target,
            "source_fingerprint": fingerprint,
        },
    }

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="tala-schema" content="neural-node/v2">
  <title>Neural Node: {escape(title)}</title>
</head>
<body>
<article
  data-node-id="{escape(safe_node_id)}"
  data-node-type="{escape(node_type)}"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.00"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-weight-behavior-influence="0.00"
  data-weight-governance-importance="0.80"
  data-governance-status="context-only"
  data-doc-target="{escape(doc_target)}"
  data-source-files="{escape(';'.join(source_files_list))}"
  data-source-fingerprint="{escape(fingerprint)}"
  data-generated-at="{generated_at}"
  data-source="local"
  data-trust-tier="system">

  <header id="node-identity">
    <h1 id="node-title">{escape(title)}</h1>
  </header>

  <section id="semantic-memory">
{_render_paragraphs(semantic_memory)}
  </section>

  <section id="source-evidence">
    <h2>Source Evidence</h2>
    <ul>
{''.join(f'      <li><code>{escape(source)}</code></li>' + chr(10) for source in source_files_list) if source_files_list else '      <li>No source files were provided for this documentation node.</li>'}
    </ul>
  </section>

  <section id="audit-activation-trace">
    <h2>Audit / Activation Trace</h2>
    <p>This documentation node was generated through an approved Tala documentation-node bridge and validated after writing.</p>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
{_render_relationships(relationship_list)}
    </ul>
  </nav>

{_render_director(safe_node_id, doc_target, reference_nodes)}

  <script type="application/json" id="node-state" data-cognitive-block="state">
{json.dumps(state_data, indent=2)}
  </script>
</article>
</body>
</html>
"""

    if previous_content == html:
        created = False
        updated = False
        unchanged = True
    else:
        node_path.write_text(html, encoding="utf-8")
        try:
            canonicalize_and_anchor(
                safe_node_id,
                html,
                state_data,
                None,
                creator="tala-system",
                document_type="html-nn",
            )
        except Exception:
            pass
        created = previous_content is None
        updated = previous_content is not None
        unchanged = False

    parsed = parse_file(node_path)
    _validate_director_blocks(node_path)

    errors: list[str] = []
    if parsed.node_id != safe_node_id:
        errors.append(f"parsed node_id mismatch: {parsed.node_id}")
    if parsed.node_type != node_type:
        errors.append(f"parsed node_type mismatch: {parsed.node_type}")
    if parsed.schema_version != "neural-node/v2":
        errors.append(f"schema_version mismatch: {parsed.schema_version}")
    if parsed.execution_authority != 0.0:
        errors.append(f"execution_authority mismatch: {parsed.execution_authority}")
    if parsed.memory_authority != 0.0:
        errors.append(f"memory_authority mismatch: {parsed.memory_authority}")

    return DocumentationNodeResult(
        created=created,
        updated=updated,
        unchanged=unchanged,
        errors=errors,
        file_name=node_path.name,
        node_id=safe_node_id,
        node_type=node_type,
        doc_target=doc_target,
        validation_passed=not errors,
        path=str(node_path),
    ).as_dict()


def _learning_topic_slug(topic: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", topic.strip().lower()).strip("-")
    return cleaned or "learning-event"


def _learning_node_id(topic: str, semantic_memory: str) -> str:
    return f"learning-{_learning_topic_slug(topic)[:60]}"


def create_learning_documentation_node(
    *,
    topic: str,
    summary: str,
    canonical_node_id: str | None = None,
    details: Any = None,
    source_files: Any = None,
    references: Any = None,
    relationships: Any = None,
    tags: Any = None,
    node_id: str | None = None,
) -> dict[str, Any]:
    topic_text = str(topic or "").strip()
    if not topic_text:
        raise ValueError("topic is required")

    summary_text = str(summary or "").strip()
    if not summary_text:
        raise ValueError("summary is required")

    detail_lines: list[str] = []
    if isinstance(details, list):
        for item in details:
            item_text = str(item).strip()
            if item_text and item_text not in detail_lines:
                detail_lines.append(item_text)
    elif isinstance(details, str):
        for item in _normalize_string_list(details):
            if item and item not in detail_lines:
                detail_lines.append(item)
        raw_detail = details.strip()
        if raw_detail and raw_detail not in detail_lines:
            detail_lines.append(raw_detail)

    semantic_parts = [
        "Durable learning event captured by Tala.",
        f"Topic: {topic_text}.",
        summary_text,
    ]
    if detail_lines:
        semantic_parts.append("Details:")
        semantic_parts.extend(f"- {line}" for line in detail_lines)
    if canonical_node_id:
        semantic_parts.append(f"Canonical topic node: {canonical_node_id}.")

    relationship_list = list(relationships or [])
    if canonical_node_id:
        relationship_list.append(
            {
                "target_node_id": canonical_node_id,
                "label": "Canonical Topic Node",
                "relationship_type": "relates-to",
                "relationship_weight": 0.95,
                "navigation_role": "canonical-topic",
            }
        )
    relationship_list.append(
        {
            "target_node_id": "system-tala-html-nn-memory-runtime",
            "label": "Tala HTML-NN Memory Runtime",
            "relationship_type": "supports",
            "relationship_weight": 0.90,
            "navigation_role": "memory-runtime",
        }
    )

    learning_node_id = node_id or _learning_node_id(topic_text, "\n".join(semantic_parts))
    learning_tags = _normalize_tags_list(tags, ["html-nn", "documentation", "learning"])
    if "learning" not in learning_tags:
        learning_tags.append("learning")

    return create_documentation_node(
        node_id=learning_node_id,
        title=f"Learning: {topic_text}",
        doc_target=LEARNING_DOCUMENTATION_TARGET,
        semantic_memory="\n\n".join(semantic_parts),
        node_type="documentation",
        source_files=source_files,
        relationships=relationship_list,
        references=references,
        tags=learning_tags,
    )


def generate_program_nodes(targets: Any = None) -> dict[str, Any]:
    requested_targets = _normalize_string_list(targets) or ["chat.panel"]
    registered_templates = _load_registered_documentation_targets()
    registered_codebase_targets = [
        target
        for target, spec in registered_templates.items()
        if spec.get("include_in_codebase", True)
    ]
    expanded_targets: list[str] = []
    for target in requested_targets:
        if target in CODEBASE_TARGET_ALIASES:
            expanded_targets.extend(CODEBASE_DOCUMENTATION_TARGETS)
            expanded_targets.extend(registered_codebase_targets)
        else:
            expanded_targets.append(target)
    requested_targets = list(dict.fromkeys(expanded_targets))
    errors: list[str] = []
    results: list[dict[str, Any]] = []

    bridge_contract_specs = [
        {
            "node_id": "system-code-patch-bridge",
            "title": "Code Patch Bridge",
            "doc_target": "code.patch.bridge",
            "semantic_memory": (
                "The Code Patch Bridge documents Tala's bounded code patch tools. "
                "code.propose_patch drafts exact-replacement patches without mutating files. "
                "code.apply_patch applies exact replacements inside the workspace only. "
                "code.propose_file_write and code.write_files support bounded creation of "
                "new text files inside the workspace with extension, size, absolute-path, "
                "blocked-directory, and overwrite guards. "
                "Absolute paths are blocked. Each old_text value must match exactly once before "
                "mutation is allowed. dry_run should be used before mutation, and validation "
                "should run after mutation. This bridge must not be used to bypass approved "
                "workspace, validation, or audit boundaries."
            ),
            "source_files": [
                "sdk/agent/safe_code_bridges.py",
                "sdk/agent/tools.py",
                "python/harness/server.py",
            ],
            "references": [
                "system-neural-node-v2-schema",
                "system-html-nn-authoring-guide",
                "system-self-improvement-loop",
            ],
        },
        {
            "node_id": "system-validation-bridge",
            "title": "Validation Bridge",
            "doc_target": "test.validation.bridge",
            "semantic_memory": (
                "The Validation Bridge documents test.run_validation. It runs allowlisted "
                "validation commands such as python -m py_compile and pytest-style commands. "
                "Arbitrary shell commands are not allowed. Validation reports command, exit code, "
                "stdout, stderr, timeout status, and success. Tala should use this bridge after "
                "code mutation or documentation generation whenever a validation command is relevant."
            ),
            "source_files": [
                "sdk/agent/safe_code_bridges.py",
                "sdk/agent/tools.py",
                "python/harness/server.py",
            ],
            "references": [
                "system-neural-node-v2-schema",
                "system-html-nn-authoring-guide",
                "system-self-improvement-loop",
            ],
        },
        {
            "node_id": "system-node-refresh-bridge",
            "title": "Node Refresh Bridge",
            "doc_target": "docs.refresh_node_grid",
            "semantic_memory": (
                "The Node Refresh Bridge documents docs.refresh_node_grid. It requests the "
                "renderer to reload visible HTML Neural Node cards after node generation, update, "
                "or deletion. It does not mutate files. If the UI still appears stale after this "
                "bridge runs, the renderer or backend may need to be restarted so newly patched "
                "code is loaded."
            ),
            "source_files": [
                "renderer/renderer.js",
                "sdk/agent/tools.py",
                "python/harness/server.py",
            ],
            "references": [
                "system-neural-node-v2-schema",
                "system-html-nn-authoring-guide",
                "system-self-improvement-loop",
            ],
        },
        {
            "node_id": "system-self-improvement-executor",
            "title": "Self-Improvement Executor",
            "doc_target": "self_improvement.execute_plan",
            "semantic_memory": (
                "The Self-Improvement Executor documents self_improvement.execute_plan. It "
                "orchestrates bounded improvement plans by proposing exact replacements, creating "
                "approved file_writes, applying or dry-running approved changes, running allowlisted "
                "validation, and reporting audited results. It should use dry_run first unless the "
                "user explicitly approves mutation. It must report changed files, validation results, "
                "and errors honestly, and it must not bypass approved bridges."
            ),
            "source_files": [
                "sdk/agent/safe_code_bridges.py",
                "sdk/agent/tools.py",
                "python/harness/server.py",
                "nodes/system-self-improvement-loop.html",
            ],
            "references": [
                "system-neural-node-v2-schema",
                "system-html-nn-authoring-guide",
                "system-self-improvement-loop",
            ],
        },
    ]

    templates = {
        "project.overview": {
            "node_id": "program-doc-project-overview",
            "title": "Project Overview",
            "doc_target": "project.overview",
            "semantic_memory": (
                "Tala is an offline-first desktop cognitive engine room built from an "
                "Electron shell, renderer workspace, Python harness, SDK agent layer, "
                "HTML Neural Node memory graph, workflow definitions, local tools, and "
                "validation bridges. Project documentation should orient maintainers to "
                "the major folders, runtime boundaries, node lifecycle, approved bridge "
                "execution, and release verification flow without claiming unverified file "
                "or tool results."
            ),
            "source_files": [
                "README.md",
                "INSTALL.md",
                "RELEASE_CHECKLIST.md",
                "package.json",
                "electron",
                "renderer",
                "python",
                "sdk",
                "nodes",
            ],
        },
        "electron.shell": {
            "node_id": "program-doc-electron-shell",
            "title": "Electron Shell",
            "doc_target": "electron.shell",
            "semantic_memory": (
                "The Electron shell owns the desktop process boundary. It starts the UI, "
                "loads preload and IPC handlers, resolves app user-data paths, and bridges "
                "renderer requests to backend RPC surfaces. Documentation for this area "
                "should distinguish main-process authority from renderer display behavior "
                "and should preserve cross-platform user-data path resolution."
            ),
            "source_files": ["electron/main.js", "electron/preload.js", "electron/ipc-handlers.js"],
        },
        "renderer.ui": {
            "node_id": "program-doc-renderer-ui",
            "title": "Renderer UI",
            "doc_target": "renderer.ui",
            "semantic_memory": (
                "The renderer UI implements Tala's visible workspace panels, chat surface, "
                "node grid, command palette, terminal display, settings views, and workflow "
                "interactions. Renderer documentation should describe display refresh, "
                "slash-command handling, RPC invocation, and the difference between visible "
                "chat text and verified bridge output."
            ),
            "source_files": ["renderer/index.html", "renderer/renderer.js", "renderer/styles.css"],
        },
        "python.harness": {
            "node_id": "program-doc-python-harness",
            "title": "Python Harness",
            "doc_target": "python.harness",
            "semantic_memory": (
                "The Python harness hosts Tala's backend RPC server, command handlers, "
                "tool bridges, workflow utilities, database access, and integration logic. "
                "Harness documentation should explain method dispatch, approved mutation "
                "boundaries, JSON-RPC result reporting, and error handling expectations."
            ),
            "source_files": ["python/harness/server.py", "python/harness/workflows_util.py", "python/requirements.txt"],
        },
        "tool.registry": {
            "node_id": "program-doc-tool-registry",
            "title": "Tool Registry",
            "doc_target": "tool.registry",
            "semantic_memory": (
                "The Tool Registry exposes approved agent tools such as documentation-node "
                "creation, disk verification, safe code patching, validation, refresh hints, "
                "and self-improvement orchestration. Tool documentation should list each "
                "bridge's authority limits, expected arguments, return shape, and proof "
                "requirements."
            ),
            "source_files": ["sdk/agent/tools.py", "sdk/agent/command_parser.py", "python/harness/server.py"],
        },
        "html-nn.runtime": {
            "node_id": "program-doc-html-nn-runtime",
            "title": "HTML Neural Node Runtime",
            "doc_target": "html-nn.runtime",
            "semantic_memory": (
                "The HTML Neural Node runtime parses, validates, indexes, and displays "
                "HTML-NN files. Runtime documentation should explain schema v2 metadata, "
                "authority weights, semantic-memory sections, relationship anchors, inert "
                "director blocks, active node directory resolution, and parser validation."
            ),
            "source_files": [
                "sdk/agent/parser.py",
                "sdk/agent/indexer.py",
                "sdk/agent/runtime.py",
                "nodes/system-neural-node-v2-schema.html",
                "nodes/system-html-nn-authoring-guide.html",
            ],
        },
        "documentation.generator": {
            "node_id": "program-doc-documentation-generator",
            "title": "Documentation Generator",
            "doc_target": "documentation.generator",
            "semantic_memory": (
                "The Documentation Generator creates and updates program documentation "
                "HTML-NNs through approved bridges, writes to the active user-data nodes "
                "directory, validates the written file, and can verify actual files from "
                "disk before reporting success. It must never invent filenames, paths, "
                "schema versions, or validation results."
            ),
            "source_files": [
                "sdk/agent/documentation_nodes.py",
                "sdk/agent/tools.py",
                "python/harness/server.py",
                "nodes/system-program-documentation-generator.html",
            ],
        },
        "self-improvement.system": {
            "node_id": "program-doc-self-improvement-system",
            "title": "Self-Improvement System",
            "doc_target": "self-improvement.system",
            "semantic_memory": (
                "The Self-Improvement System classifies user goals, creates bounded plans, "
                "selects approved bridges, performs dry runs before mutation, applies exact "
                "text replacements only when authorized, validates changes with allowlisted "
                "commands, and reports audited results. It cannot execute arbitrary embedded "
                "node scripts or mutate outside approved bridges."
            ),
            "source_files": [
                "sdk/agent/safe_code_bridges.py",
                "sdk/agent/tools.py",
                "nodes/system-self-improvement-loop.html",
            ],
        },
        "settings.inference": {
            "node_id": "program-doc-settings-inference",
            "title": "Inference Settings",
            "doc_target": "settings.inference",
            "semantic_memory": (
                "Inference Settings document configured model providers, priority order, "
                "online status, local and embedded inference backends, and how Tala uses "
                "available models for cognitive operations. Documentation for this panel "
                "should avoid hardcoding environment-specific model availability unless it "
                "comes from a verified settings bridge result."
            ),
            "source_files": ["renderer/renderer.js", "python/harness/server.py", "sdk/agent/tools.py"],
        },
        "chat.panel": {
            "node_id": "program-doc-chat-panel",
            "title": "Chat Panel",
            "doc_target": "chat.panel",
            "semantic_memory": (
                "The Chat panel is Tala's conversational surface for user requests, "
                "assistant responses, slash commands, and approved tool-call workflows. "
                "It should display normal dialogue separately from execution results, "
                "and it must not claim that files or nodes were created unless an approved "
                "bridge writes and validates those artifacts."
            ),
            "source_files": ["python/harness/server.py", "renderer/renderer.js", "sdk/agent/tools.py"],
        },
        "workflow": {
            "node_id": "program-doc-workflow",
            "title": "Workflow Builder and Executor",
            "doc_target": "workflow",
            "semantic_memory": (
                "The Workflow Builder and Executor area documents Tala's structured "
                "workflow surface. It is used to list workflows, save workflow definitions, "
                "delete workflows, execute workflow graphs, and inspect workflow progress. "
                "Workflow documentation should explain triggers, steps, validation, execution "
                "status, and result reporting generically. It must not claim a workflow ran "
                "unless the approved workflow bridge returns a verified execution result."
            ),
            "source_files": [
                "python/harness/server.py",
                "python/harness/workflows_util.py",
                "renderer/renderer.js",
                "workflows",
            ],
        },
        "workflow.builder": {
            "node_id": "program-doc-workflow",
            "title": "Workflow Builder and Executor",
            "doc_target": "workflow",
            "semantic_memory": (
                "The Workflow Builder and Executor area documents Tala's structured "
                "workflow surface. It is used to list workflows, save workflow definitions, "
                "delete workflows, execute workflow graphs, and inspect workflow progress. "
                "Workflow documentation should explain triggers, steps, validation, execution "
                "status, and result reporting generically. It must not claim a workflow ran "
                "unless the approved workflow bridge returns a verified execution result."
            ),
            "source_files": [
                "python/harness/server.py",
                "python/harness/workflows_util.py",
                "renderer/renderer.js",
                "workflows",
            ],
        },
        "workflow.executor": {
            "node_id": "program-doc-workflow",
            "title": "Workflow Builder and Executor",
            "doc_target": "workflow",
            "semantic_memory": (
                "The Workflow Builder and Executor area documents Tala's structured "
                "workflow surface. It is used to list workflows, save workflow definitions, "
                "delete workflows, execute workflow graphs, and inspect workflow progress. "
                "Workflow documentation should explain triggers, steps, validation, execution "
                "status, and result reporting generically. It must not claim a workflow ran "
                "unless the approved workflow bridge returns a verified execution result."
            ),
            "source_files": [
                "python/harness/server.py",
                "python/harness/workflows_util.py",
                "renderer/renderer.js",
                "workflows",
            ],
        },
        "blockchain.ledger": {
            "node_id": "program-doc-blockchain-ledger",
            "title": "Blockchain and Ledger",
            "doc_target": "blockchain.ledger",
            "semantic_memory": (
                "The Blockchain and Ledger area documents local ledger records, smart "
                "contract interactions, transaction summaries, and related diagnostics. "
                "Documentation should separate displayed ledger state from verified backend "
                "queries and should not invent transaction results."
            ),
            "source_files": ["blockchain", "python/harness/server.py", "renderer/renderer.js"],
        },
        "vault.profile": {
            "node_id": "program-doc-vault-profile",
            "title": "Vault and Profile",
            "doc_target": "vault.profile",
            "semantic_memory": (
                "The Vault and Profile area documents user profile storage, local vault "
                "boundaries, private data handling, and profile-aware behavior. Documentation "
                "should emphasize privacy, local storage, approved access paths, and avoidance "
                "of leaking absolute machine-specific paths into generated nodes."
            ),
            "source_files": ["vault", "renderer/renderer.js", "python/harness/server.py"],
        },
        "tests.validation": {
            "node_id": "program-doc-tests-validation",
            "title": "Tests and Validation",
            "doc_target": "tests.validation",
            "semantic_memory": (
                "Tests and Validation document Tala's pytest suites, SDK agent tests, "
                "benchmark checks, py_compile smoke tests, installer checks, and release "
                "checklist expectations. Validation documentation should report exact "
                "commands and results only after an approved validation bridge executes them."
            ),
            "source_files": ["tests", "sdk/agent/tests", "RELEASE_CHECKLIST.md", "sdk/agent/safe_code_bridges.py"],
        },
        "bridges": bridge_contract_specs,
        "tool-contracts": bridge_contract_specs,
        "self-improvement.bridges": bridge_contract_specs,
    }
    for target, spec in registered_templates.items():
        templates[target] = _documentation_create_kwargs(spec)

    for target in requested_targets:
        spec = templates.get(target)
        if not spec:
            errors.append(f"Unsupported documentation target: {target}")
            continue
        specs = spec if isinstance(spec, list) else [spec]
        try:
            for item in specs:
                results.append(create_documentation_node(**item))
        except Exception as exc:
            errors.append(f"{target}: {exc}")

    return {
        "created": sum(1 for result in results if result.get("created")),
        "updated": sum(1 for result in results if result.get("updated")),
        "unchanged": sum(1 for result in results if result.get("unchanged")),
        "errors": errors + [error for result in results for error in result.get("errors", [])],
        "nodes": results,
    }


def _get_article_attribute(path: Path, attribute: str) -> str:
    doc = lxml_html.fromstring(path.read_text(encoding="utf-8"))
    value = doc.xpath(f"string(.//article/@{attribute})")
    return str(value or "").strip()


def verify_documentation_nodes(
    *,
    targets: Any = None,
    node_ids: Any = None,
) -> dict[str, Any]:
    requested_targets = set(_normalize_string_list(targets))
    requested_node_ids = set(_safe_node_id(node_id) for node_id in _normalize_string_list(node_ids))
    nodes_dir = resolve_active_nodes_dir()
    errors: list[str] = []
    verified_nodes: list[dict[str, Any]] = []

    if not nodes_dir.exists():
        return {
            "success": False,
            "errors": [f"Active nodes directory does not exist: {nodes_dir}"],
            "nodes": [],
            "verified": 0,
        }

    for path in sorted(nodes_dir.glob("*.html")):
        try:
            parsed = parse_file(path)
            doc_target = _get_article_attribute(path, "data-doc-target")
            node_type = _get_article_attribute(path, "data-node-type") or parsed.node_type
            schema_version = _get_article_attribute(path, "data-schema-version") or parsed.schema_version

            if requested_targets and doc_target not in requested_targets:
                continue
            if requested_node_ids and parsed.node_id not in requested_node_ids:
                continue
            if node_type not in ALLOWED_NODE_TYPES:
                continue

            node_errors: list[str] = []
            if node_type not in ALLOWED_NODE_TYPES:
                node_errors.append(f"node_type must be one of {sorted(ALLOWED_NODE_TYPES)}, got {node_type}")
            if schema_version != "neural-node/v2":
                node_errors.append(f"schema_version must be neural-node/v2, got {schema_version}")
            if parsed.execution_authority != 0.0:
                node_errors.append(f"execution_authority must be 0.00, got {parsed.execution_authority}")
            if parsed.memory_authority != 0.0:
                node_errors.append(f"memory_authority must be 0.00, got {parsed.memory_authority}")

            verified_nodes.append(
                {
                    "file_name": path.name,
                    "node_id": parsed.node_id,
                    "node_type": node_type,
                    "doc_target": doc_target,
                    "schema_version": schema_version,
                    "validation_passed": not node_errors,
                    "errors": node_errors,
                    "path": str(path),
                }
            )
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")

    if requested_targets:
        found_targets = {node["doc_target"] for node in verified_nodes}
        missing_targets = sorted(requested_targets - found_targets)
        errors.extend(f"No verified documentation node found for target: {target}" for target in missing_targets)

    if requested_node_ids:
        found_node_ids = {node["node_id"] for node in verified_nodes}
        missing_node_ids = sorted(requested_node_ids - found_node_ids)
        errors.extend(f"No verified documentation node found for node_id: {node_id}" for node_id in missing_node_ids)

    validation_errors = [
        error
        for node in verified_nodes
        for error in node.get("errors", [])
    ]

    return {
        "success": not errors and not validation_errors,
        "errors": errors + validation_errors,
        "nodes": verified_nodes,
        "verified": sum(1 for node in verified_nodes if node.get("validation_passed")),
    }


def generate_and_verify_program_nodes(targets: Any = None) -> dict[str, Any]:
    generation = generate_program_nodes(targets=targets)
    generated_targets = [
        node.get("doc_target")
        for node in generation.get("nodes", [])
        if node.get("doc_target")
    ]
    verification = verify_documentation_nodes(targets=generated_targets)
    return {
        "created": generation.get("created", 0),
        "updated": generation.get("updated", 0),
        "unchanged": generation.get("unchanged", 0),
        "errors": generation.get("errors", []) + verification.get("errors", []),
        "nodes": verification.get("nodes", []),
        "generation": generation,
        "verification": verification,
        "success": not generation.get("errors") and verification.get("success", False),
    }
