"""
documentation_nodes.py - Auto-generated documentation base.
"""

from __future__ import annotations

import hashlib
import json
import logging
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

log = logging.getLogger(__name__)


ALLOWED_NODE_TYPES = {"documentation", "program-doc", "function-doc"}

#: Recall authority written into every generated documentation node.
#
# A named constant because the value has to agree in four places -- the HTML
# template, create_documentation_node's post-write check, verify_documentation_
# nodes, and the LLM-facing create_documentation_node_tool -- and when it was
# raised from 0.00 to 0.60 only the template and one validator moved. The other
# two kept demanding 0.00, so every node the generator wrote was immediately
# reported invalid by the generator itself, and the tool the model calls could
# not produce a recallable node at all.
#
# Documentation must be recallable (a zero here is a hard gate on activation)
# but must stay below live settings and doctrine at 1.00, so a strongly-matching
# document beats a weakly-matching setting without drowning out configuration.
DOC_NODE_MEMORY_AUTHORITY = 0.60
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
    #: True when the node went to nodes/_staging/ instead of production and
    #: is awaiting promotion. Callers that report "node created" to a user
    #: should say "staged" instead when this is set.
    staged: bool = False

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
            "staged": self.staged,
        }


def _application_root() -> Path:
    """
    Directory the application itself is installed/checked out in --
    sdk/agent/documentation_nodes.py -> up three levels -> repo/install root.
    Used as the final, OS-agnostic fallback for user-data resolution so
    nothing here depends on a Windows-only environment variable existing.
    """
    return Path(__file__).resolve().parent.parent.parent


def resolve_user_data_dir() -> Path:
    """
    Resolve the base directory Tala's per-user runtime data (nodes/,
    workflows/, chat_mode.json, ...) lives under. Priority order:
    1. TALA_USER_DATA env var (explicit override; test isolation uses this,
       and it's the escape hatch for anyone who deliberately wants their
       data somewhere else -- a shared multi-user install, a container
       volume, an OS profile directory, etc.).
    2. The application's own install/checkout location -- the default,
       unconditionally, on every OS.

    Tala is designed to be portable: installed on a USB stick or any
    external drive, it should behave identically no matter which machine
    or OS it's plugged into, with all of its data traveling with the
    install rather than scattering into whichever host machine's per-user
    profile happened to be active. That means this resolver does NOT
    auto-detect OS profile directories (previously: Path.home()/"AppData"/
    "Roaming"/... on Windows, Path.home()/"Library"/"Application Support"/...
    on macOS, XDG/.config on Linux -- and, briefly, an automatic APPDATA
    check here) as anything other than something the caller opts into
    explicitly via TALA_USER_DATA. APPDATA is set on essentially every real
    Windows machine, so auto-preferring it silently defeated portability by
    default there -- exactly the case that matters most, since Windows is
    the common USB-boot target.

    This is the single canonical resolver; every node/workflow-writing path
    in the harness should go through this (or resolve_active_nodes_dir/
    resolve_active_workflows_dir below) instead of re-deriving its own copy
    of this logic.
    """
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        return Path(user_data).expanduser().resolve()

    return _application_root()


def resolve_active_nodes_dir() -> Path:
    """Resolve the active nodes/ directory. See resolve_user_data_dir()."""
    return resolve_user_data_dir() / "nodes"


def resolve_active_workflows_dir() -> Path:
    """Resolve the active workflows/ directory. See resolve_user_data_dir()."""
    return resolve_user_data_dir() / "workflows"


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
    state_extra: dict[str, Any] | None = None,
    stage: bool = False,
) -> dict[str, Any]:
    """
    Write a neural node.

    ``stage=True`` routes the node into ``nodes/_staging/`` instead of
    production: it must then pass verification on several consecutive
    cycles before ``node_staging.run_verification_cycle`` promotes it (see
    that module for why). Callers authoring a node from model output —
    anything Tala decided to write — should stage it. Deterministic
    generators that rebuild a node from source on demand should not: their
    output is reproducible and re-verified every time it is regenerated.
    """
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

    # Reuse the previous timestamp when the content fingerprint is unchanged.
    #
    # The `previous_content == html` check below is meant to skip writing a node
    # that has not changed, but generated_at is stamped with datetime.now() and
    # embedded in the HTML, so the two strings ALWAYS differed by at least the
    # timestamp. The unchanged branch could never be reached: every regeneration
    # rewrote every node and reported it as updated.
    #
    # The visible cost was a permanently dirty working tree -- program-doc-*
    # nodes showing as modified with a two-line diff of nothing but the time,
    # while data-source-fingerprint, which is the actual content identity, was
    # byte-for-byte identical. The reporting cost is the same shape as the rest
    # of this branch: "updated" for work that did not happen.
    if previous_content:
        previous_fingerprint = re.search(
            r'data-source-fingerprint="([^"]*)"', previous_content
        )
        previous_generated = re.search(
            r'data-generated-at="([^"]*)"', previous_content
        )
        if (
            previous_fingerprint
            and previous_generated
            and previous_fingerprint.group(1) == fingerprint
        ):
            generated_at = previous_generated.group(1)

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

    # Structured state a generator needs to carry but that has no home in the
    # fixed fields above -- a tool node's argument schema, for instance, which
    # `references` cannot hold because it is coerced to a list of strings.
    # Reserved keys are not overwritable: a caller must not be able to forge
    # the metadata block or the fingerprint by passing them in here.
    if state_extra:
        for key, value in state_extra.items():
            if key not in ("title", "description", "tags", "metadata"):
                state_data[key] = value

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
  data-memory-authority="0.60"
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

    # Probation. A brand-new node written from model output goes to staging
    # rather than straight into the retrieval corpus; an update to a node
    # that already exists in production writes through, so the self-healing
    # docs path is never blocked behind a promotion cycle.
    if stage and previous_content is None:
        from sdk.agent.node_staging import stage_node, staging_enabled

        if staging_enabled():
            staged_path = stage_node(safe_node_id, html)
            return DocumentationNodeResult(
                created=False,
                updated=False,
                unchanged=False,
                errors=[],
                file_name=staged_path.name,
                node_id=safe_node_id,
                node_type=node_type,
                doc_target=doc_target,
                validation_passed=True,
                path=str(staged_path),
                staged=True,
            ).as_dict()

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
    # Compared against the same constant the template writes, so this cannot
    # reject the file it just produced -- which it did for every node once the
    # value was raised to 0.60 and this check was left at 0.00.
    if parsed.memory_authority != DOC_NODE_MEMORY_AUTHORITY:
        errors.append(
            f"memory_authority mismatch: expected {DOC_NODE_MEMORY_AUTHORITY}, "
            f"got {parsed.memory_authority}"
        )

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


#: Verbs that begin a task instruction rather than a lesson. A node titled
#: "Fix the failing assertion in test_tax.py" is a restatement of work that was
#: requested, not something learned from doing it.
_TASK_IMPERATIVES = (
    "fix", "create", "add", "plan", "implement", "refactor", "write", "run",
    "delete", "remove", "update", "make", "build", "generate", "execute",
    "install", "setup", "set up", "check", "test", "debug", "improve",
    "optimize", "optimise", "rename", "move", "convert", "migrate", "use",
    "try", "give", "show", "tell", "find", "list", "print",
)

#: Titles that carry no information at all.
_DEGENERATE_TITLE_MARKERS = ("unnamed", "untitled", "no title", "none")

#: Openings that mark a topic as running narration rather than a named lesson.
#: A wiki page title is a concept ("Ollama GPU passthrough"); these are
#: sentences lifted out of the middle of a conversation ("I haven't been given
#: a target yet...", "the test will print..."). They pass the imperative check
#: because they don't start with a command verb, so they need their own rule.
_NARRATION_STARTS = (
    "i haven", "i have", "i had", "i could", "i can", "i was", "i am",
    "i will", "i think", "i need", "i should", "i would", "i just",
    "you were", "you are", "you should", "you can", "you have",
    "we were", "we are", "the test will", "it will", "this will",
    "there is", "there was", "here is", "here are", "so ", "okay",
)

#: Summary templates emitted by automatic callers. Matching one of these means
#: the "lesson" is a description of a job having finished, not a lesson.
_BOILERPLATE_SUMMARY_MARKERS = (
    "distilled a reusable learning event from the completed goal",
    "tree-of-thought ideation ranked",
    "you were trying to accomplish the following task",
)

#: A real transferable lesson needs some substance behind it.
_MIN_SUMMARY_WORDS = 12


def assess_learning_quality(
    topic: str, summary: str, details: Any = None
) -> str | None:
    """
    Decide whether a proposed learning is worth a permanent wiki node.

    Returns a rejection reason, or None if it should be written.

    WHY THIS GATE EXISTS
    --------------------
    Learning nodes are pages in Tala's HTML-NN wiki -- the RAG-graph's store of
    record. Whatever lands here becomes retrieval context forever, so a junk
    node is not merely clutter: it is future context poisoning, and it grounds
    the training corpus too.

    Before this gate, of 17 auto-created learning nodes roughly three carried a
    real lesson. The rest were task restatements ("Fix the IndexError in
    index_test.py"), fragments ("*(Executing run_workspace_command"), and one
    with a blank title. The dominant source was not the chat cue path but
    ``self_documenter``, which minted a node for *every completed goal* titled
    with the raw goal text -- completing a task is not learning something.

    The gate lives here rather than in each caller because all six write paths
    funnel through ``create_learning_documentation_node``. One door, one lock.
    """
    topic_text = str(topic or "").strip()
    summary_text = str(summary or "").strip()

    if not topic_text:
        return "empty topic"
    if not summary_text:
        return "empty summary"

    # Topics are extracted by slicing after a learning cue ("I learned that
    # ..."), which routinely leaves a dangling conjunction on the front. That
    # leading word shifts the real first word out of position and lets both the
    # imperative and narration checks miss -- "that I should fix the failing
    # assertion" reads as neither until the "that" is removed.
    low_topic = re.sub(r"^(?:that|this|it|the fact that)\s+", "", topic_text.lower()).strip()
    if not low_topic:
        return f"topic is a bare conjunction: {topic_text[:60]!r}"

    if not topic_text[0].isalnum():
        return f"topic is a fragment, not a statement: {topic_text[:60]!r}"

    if any(marker in low_topic for marker in _DEGENERATE_TITLE_MARKERS):
        return f"degenerate topic: {topic_text[:60]!r}"

    first_word = re.split(r"[^a-z]+", low_topic, maxsplit=1)[0]
    if first_word in _TASK_IMPERATIVES:
        return (
            f"topic reads as a task instruction, not a lesson: {topic_text[:60]!r}"
        )

    if any(low_topic.startswith(p) for p in _NARRATION_STARTS):
        return (
            f"topic is narration, not a named lesson: {topic_text[:60]!r}"
        )

    low_summary = summary_text.lower()
    if any(marker in low_summary for marker in _BOILERPLATE_SUMMARY_MARKERS):
        return "summary is an automatic completion template, not a lesson"

    # Substance check, counting the details too -- a short summary backed by
    # concrete detail lines is fine.
    detail_words = 0
    if isinstance(details, list):
        detail_words = sum(len(str(d).split()) for d in details)
    elif isinstance(details, str):
        detail_words = len(details.split())

    if len(summary_text.split()) + detail_words < _MIN_SUMMARY_WORDS:
        return "too thin to be worth a permanent node"

    return None


#: Filenames/paths in a goal usually mark a one-off fix rather than a skill.
_SPECIFIC_FILE_RE = re.compile(r"[\w\-]+\.(py|js|ts|html|json|txt|md|yaml|yml)\b", re.I)


def assess_skill_quality(
    goal_text: str, phases: Any = None, executable_code: str | None = None
) -> str | None:
    """
    Decide whether a completed goal deserves a permanent skill node.

    Returns a rejection reason, or None if it should be written.

    WHY THIS GATE EXISTS
    --------------------
    ``document_completed_goal`` writes a skill node for *every* completed goal,
    with the node id slugged straight from the goal text. Of the eight that
    accumulated, seven were junk: "Fix the IndexError in index_test.py", "Fix
    the failing assertion in test_tax.py", "Workflow: Unnamed Workflow", and two
    with entirely blank titles.

    Skill nodes are worse than junk learning nodes because their **bodies are
    raw Python**, and the wiki is a retrieval source. Asked to run a self-test,
    Tala retrieved one of these and reproduced its file contents verbatim
    instead of calling any tool. Code-bearing junk does not merely waste space
    in the index -- it hijacks the response.

    The distinction drawn here is *reusable procedure* vs *one-off fix*.
    Repairing a specific assertion in a specific test file teaches nothing
    transferable. A goal that produced real executable code, or that describes a
    general capability rather than a named file, does.
    """
    text = str(goal_text or "").strip()
    if not text:
        return "empty goal text"

    low = text.lower()

    if not text[0].isalnum():
        return f"goal is a fragment: {text[:60]!r}"

    if any(marker in low for marker in _DEGENERATE_TITLE_MARKERS):
        return f"degenerate goal: {text[:60]!r}"

    # The re-planner builds its prompt as "You were trying to accomplish the
    # following task: '<desc>'. However, the execution failed with ...", hands
    # that whole string to the decomposer as the goal, and the model echoes it
    # back as the new task's description. Fail twice and the wrapper wraps
    # itself. On 2026-08-14 that produced
    # skill-you_were_trying_to_accomplish_the_following_task_you_were_tr --
    # a "skill" whose entire content was the engine's own retry scaffolding,
    # keyworded on "accomplish", "following", "trying", which are common enough
    # to surface it as relevant prior experience for unrelated future goals.
    #
    # assess_learning_quality already rejects this shape via _NARRATION_STARTS
    # ("you were"); the skill gate simply never checked. Same constant, same
    # rule -- a goal that is narration is not a named capability.
    if any(low.startswith(p) for p in _NARRATION_STARTS):
        return f"goal is narration, not a named capability: {text[:60]!r}"

    # A skill distilled from a *failure* teaches the failure. These markers are
    # the engine's own error prose, not anything a human wrote as a goal.
    if any(
        marker in low
        for marker in (
            "however, the execution failed",
            "failed validation/criteria",
            "traceback (most recent call last)",
        )
    ):
        return f"goal carries failure narration, not a capability: {text[:60]!r}"

    # NOTE: the presence of `executable_code` is deliberately NOT treated as
    # evidence of quality. synthesize_python_skill runs on every completed goal
    # and returns code regardless of whether the goal was worth distilling, so
    # every junk node already had a Python body -- that is precisely how the
    # clean_broken_skills source came to live in a node titled "Create a new
    # reusable skill by writing a Python file at ...". Judging on code presence
    # would pass all seven junk nodes. The goal text is the only signal that
    # actually separates a capability from a one-off.
    if _SPECIFIC_FILE_RE.search(text):
        return (
            "goal targets a specific file, so it is a one-off fix rather than a "
            f"reusable skill: {text[:60]!r}"
        )

    first_word = re.split(r"[^a-z]+", low, maxsplit=1)[0]
    if first_word in _TASK_IMPERATIVES:
        phase_count = len(phases) if isinstance(phases, (list, tuple)) else 0
        # A multi-phase imperative that produced real code is a plausible
        # procedure; a one-liner with nothing behind it is just the task
        # restated.
        if phase_count < 3 or not (executable_code and str(executable_code).strip()):
            return (
                "goal is a task instruction, not a named capability "
                f"({phase_count} phase(s)): {text[:60]!r}"
            )

    return None


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
    force: bool = False,
) -> dict[str, Any]:
    """
    Write a learning event to the HTML-NN wiki.

    Automatic callers are quality-gated (see ``assess_learning_quality``); pass
    ``force=True`` only when a human explicitly asked for this to be recorded.
    Rejected proposals return ``{"status": "skipped", ...}`` rather than raising,
    so a low-value learning never breaks the flow that produced it.
    """
    topic_text = str(topic or "").strip()
    if not topic_text:
        raise ValueError("topic is required")

    summary_text = str(summary or "").strip()
    if not summary_text:
        raise ValueError("summary is required")

    if not force:
        rejection = assess_learning_quality(topic_text, summary_text, details)
        if rejection:
            log.info(
                "Learning node not written (%s): %s", rejection, topic_text[:80]
            )
            return {
                "status": "skipped",
                "reason": rejection,
                "topic": topic_text,
            }

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
        # Learning nodes are the model deciding something is worth
        # remembering, and historically the dominant source of wiki junk
        # (3 of 17 were real). assess_learning_quality above screens the
        # obvious cases at write time; probation catches the rest by
        # requiring the node to survive several verification cycles before
        # it joins the retrieval corpus.
        stage=True,
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
            # memory_authority is REQUIRED to be non-zero, not required to be zero.
            #
            # This rule used to demand 0.00, which is the same conflation that
            # made the graph mute: execution authority 0.00 is correct (a
            # document must never act), and copying it onto the memory axis means
            # "never recallable" -- applied to nodes whose only purpose is recall.
            # Activation is a product gated on governance, so 94 of 115 nodes
            # could not be injected at any similarity. With the rule inverted,
            # verification now fails a document that has gone mute instead of
            # failing one that can be remembered.
            if parsed.memory_authority <= 0.0:
                node_errors.append(
                    "memory_authority must be greater than 0.00 or the node can never be "
                    f"recalled, got {parsed.memory_authority}"
                )

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
