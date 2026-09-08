"""
generate_tool_nodes.py — make the tool registry a collection of HTML-NN.

Answers UI-107, which is Steven's own requirement (Q-33):

    "The tool registry should be a collection of html-nn. If they are not we
    need to address that."

They were not. Before this script there were 88 ``@register_tool`` functions
and **zero** ``tool-*.html`` nodes. The 62 ``sdk_agent_*.html`` nodes document
*modules* — ``sdk_agent_planner``, ``sdk_agent_shell`` — which is a different
axis entirely: a module node cannot be retrieved by capability, cannot be
referenced by a workflow, and cannot populate the workflow palette.

What this does and does not change
----------------------------------

The Python registry stays the **executable** truth. HTML cannot be called, and
pretending otherwise would mean inventing an interpreter for something that
already works. What becomes HTML-NN is the **catalogue**: one node per tool,
carrying the name, the real argument schema, and the prose — so a tool is a
first-class thing Tala can retrieve, cite, and chain, rather than a line in a
prompt string.

Deterministic, and regenerated from the registry on demand. It is therefore not
staged: staging exists for nodes authored from model output, where the content
is a judgement that needs verifying. Nothing here is a judgement.

Usage::

    python scripts/generate_tool_nodes.py            # write nodes
    python scripts/generate_tool_nodes.py --dry-run  # list, write nothing
    python scripts/generate_tool_nodes.py --prune    # also remove orphans
"""

from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

NODE_PREFIX = "tool-"

#: Directories whose registrations are not real capabilities.
_SKIP_PARTS = ("tests", "test_", "__pycache__", "_staging")


def discover_tool_modules(root: Path | None = None) -> list[str]:
    """
    Find every module that registers a tool, by scanning rather than listing.

    A hardcoded list was the first version of this, and it was wrong within an
    hour: ``sdk/agent/skills/`` registers tools and was not on it, so those
    tools had no node *and* -- because registration is an import side effect --
    were invisible to the planner's live catalogue too. A capability that
    exists but is never imported is indistinguishable from one that does not
    exist, which is the same silent-gap shape this whole audit keeps finding.

    Test fixtures are excluded deliberately: they register throwaway tools that
    must never reach the catalogue.
    """
    root = root or (_ROOT / "sdk")
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(_ROOT)
        if any(part.startswith(_SKIP_PARTS) or part in _SKIP_PARTS
               for part in rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "@register_tool" in text or "ToolRegistry.register" in text:
            found.append(".".join(rel.with_suffix("").parts))
    return found


def _import_all() -> list[str]:
    problems = []
    for name in discover_tool_modules():
        try:
            __import__(name)
        except Exception as exc:  # pragma: no cover - environment dependent
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
    return problems


def _signature(schema: dict) -> str:
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    return ", ".join(
        f"{n}*" if n in required else n for n in props
    )


def _source_files_for(tool: dict) -> list[str]:
    """
    The file a registered tool is defined in, relative to the repository.

    Empty when the function cannot be located -- a tool bridged from an MCP
    server has no local source, and claiming one would make a description
    look checkable when nothing can check it.
    """
    func = tool.get("func")
    if func is None:
        return []
    try:
        source_file = inspect.getsourcefile(func) or inspect.getfile(func)
    except (TypeError, OSError):
        return []
    if not source_file:
        return []
    try:
        return [Path(source_file).resolve().relative_to(_ROOT).as_posix()]
    except ValueError:
        return []


def _prose(tool: dict) -> str:
    """
    The node's semantic memory: what retrieval will match on.

    Written as prose rather than a schema dump because this is the text that
    has to answer "which tool does X" when Tala searches for a capability she
    has not used before.
    """
    name = tool["name"]
    desc = " ".join(str(tool.get("description") or "").split())
    schema = tool.get("schema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    parts = [f"<p><strong>{name}</strong> is a callable tool in Tala's registry. {desc}</p>"]

    if props:
        rows = []
        for arg, spec in props.items():
            typ = (spec or {}).get("type", "any")
            need = "required" if arg in required else "optional"
            note = " ".join(str((spec or {}).get("description") or "").split())
            rows.append(
                f"<li><code>{arg}</code> ({typ}, {need}){': ' + note if note else ''}</li>"
            )
        parts.append(
            f"<p>Call it as <code>{name}({_signature(schema)})</code>, "
            f"where <code>*</code> marks a required argument:</p>"
            f"<ul>{''.join(rows)}</ul>"
        )
    else:
        parts.append(f"<p>It takes no arguments: <code>{name}()</code>.</p>")

    func = tool.get("func")
    doc = inspect.getdoc(func) if func else None
    if doc:
        # The docstrings follow a man-page convention; keep the DESCRIPTION
        # body, which is the part that explains behaviour rather than repeating
        # the name and signature already rendered above.
        body = doc
        if "DESCRIPTION" in doc:
            body = doc.split("DESCRIPTION", 1)[1]
            for stop in ("PARAMETERS", "RETURNS", "EXAMPLES", "SEE ALSO"):
                if stop in body:
                    body = body.split(stop, 1)[0]
        body = " ".join(body.split())
        if body:
            parts.append(f"<p>{body[:1200]}</p>")

    module = getattr(func, "__module__", "") if func else ""
    if module:
        parts.append(
            f"<p>Implemented in <code>{module}</code>. The Python registry "
            f"remains what actually executes; this node is the catalogue entry "
            f"that makes the tool retrievable and referenceable from a "
            f"workflow.</p>"
        )
    return "".join(parts)


def build(dry_run: bool = False, prune: bool = False) -> int:
    problems = _import_all()
    for p in problems:
        print(f"  ! could not import {p}")

    from sdk.agent.documentation_nodes import (
        _safe_node_id,
        create_documentation_node,
        resolve_active_nodes_dir,
    )
    from sdk.agent.tools import ToolRegistry

    registry = ToolRegistry._tools
    tools = sorted(registry.values(), key=lambda t: t["name"])
    nodes_dir = resolve_active_nodes_dir()
    print(f"documenting {len(tools)} tool(s) into {nodes_dir}")

    written = failed = 0
    expected: set[str] = set()

    for tool in tools:
        name = tool["name"]
        node_id = f"{NODE_PREFIX}{name.replace('_', '-')}"
        # Record the *normalised* id: create_documentation_node sanitises the
        # id before using it as a filename (a tool named "workflow.generate_x"
        # lands as tool-workflow-generate-x). Comparing raw ids against the
        # names on disk made --prune delete the very nodes it had just written.
        expected.add(_safe_node_id(node_id))
        if dry_run:
            print(f"  would write {node_id}")
            continue
        try:
            schema = tool.get("schema") or {}
            create_documentation_node(
                node_id=node_id,
                title=f"Tool: {name}",
                doc_target=name,
                node_type="function-doc",
                semantic_memory=_prose(tool),
                # Where the tool actually lives, so this description can be
                # checked against it later. These nodes carried an empty
                # source-files attribute, which put 99 of them -- the largest
                # block of current operational documentation in the store --
                # permanently beyond any freshness check. The registry knows
                # the function; the function knows its file.
                source_files=_source_files_for(tool),
                purpose="description",
                tags=["tool", "registry", "capability", name],
                state_extra={
                    # The machine-readable half. The prose above is what
                    # retrieval matches on; this is what a workflow step or the
                    # palette needs in order to actually build a call.
                    "tool": {
                        "name": name,
                        # The tool's own description, not the generic
                        # "Program documentation for X" that create_documentation_node
                        # puts in the description field.
                        "description": " ".join(str(tool.get("description") or "").split()),
                        "signature": f"{name}({_signature(schema)})",
                        "schema": schema,
                        "module": getattr(tool.get("func"), "__module__", ""),
                        "required": list((schema or {}).get("required") or []),
                    },
                },
            )
            written += 1
        except Exception as exc:
            print(f"  ! {name}: {type(exc).__name__}: {exc}")
            failed += 1

    removed = 0
    if prune and not dry_run:
        for path in nodes_dir.glob(f"{NODE_PREFIX}*.html"):
            if path.stem not in expected:
                # A tool that no longer exists must not keep a node: an
                # unreachable capability in the catalogue is worse than a
                # missing one, because the planner will try to call it.
                print(f"  - pruning orphan {path.stem}")
                path.unlink()
                removed += 1

    print(f"\nwritten: {written}   failed: {failed}"
          + (f"   pruned: {removed}" if prune else ""))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="list, write nothing")
    ap.add_argument("--prune", action="store_true",
                    help="remove tool nodes whose tool no longer exists")
    args = ap.parse_args()
    return build(dry_run=args.dry_run, prune=args.prune)


if __name__ == "__main__":
    raise SystemExit(main())
