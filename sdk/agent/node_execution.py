"""
node_execution.py — the verb that was missing: acting on an HTML-NN.

Steven, Q-34:

    "A workflow is a series of tool calls, that pass one bit of info from one
    tool to the next until desired outcome. Tala has the ability to call an
    html-nn and act upon that. Or at least she should."

She could not. The registry had ``read_neural_node``, ``list_neural_nodes`` and
``update_neural_node`` — read, enumerate, write — and nothing that *runs* one
(UI-106). Meanwhile ``workflow.save`` compiled a flow into a node whose own
semantic memory said "this workflow can be executed by invoking the RPC method
``workflow.execute``" — an instruction Tala could read and had no mechanism to
follow, because ``workflow.execute`` is a JSON-RPC method and the agent can
only call registered tools (UI-060).

The gap was one verb wide. This is the verb.

Two design decisions worth stating:

* **The node is the source of truth.** ``workflow.execute`` resolves a flow by
  reading a JSON sidecar file, which made the compiled HTML-NN decorative —
  written, indexed, and never the thing that ran. ``execute_node`` reads the
  spec out of the node's own ``node-state`` block, so the document Tala
  retrieved is the document that executes.
* **Execution authority is checked, not assumed.** A node carries
  ``data-execution-authority``. Running a node that declares 0.0 — every
  documentation and tool-catalogue node, for instance — would turn a reference
  work into a program. It is refused.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sdk.agent.tools import register_tool

log = logging.getLogger(__name__)

#: Node types that describe something runnable.
EXECUTABLE_TYPES = frozenset({"workflow", "flow"})

#: Below this, a node is a document rather than a program.
MIN_EXECUTION_AUTHORITY = 0.5


def _nodes_dir() -> Path:
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir
    return resolve_active_nodes_dir()


def _find_node(node_id: str) -> Path | None:
    """
    Locate a node file by id, tolerating the ``flow-`` prefix.

    A compiled workflow's node id is ``flow-<uuid>`` but people and models both
    reach for the bare id, and the panel shows the flow name. Trying the
    obvious variants here is cheaper than a wrong "not found" that sends the
    caller hunting.
    """
    directory = _nodes_dir()
    candidates = [node_id, f"flow-{node_id}"]
    if node_id.startswith("flow-"):
        candidates.append(node_id[5:])
    for candidate in candidates:
        path = directory / f"{candidate}.html"
        if path.exists():
            return path
    return None


def _authority_of(html: str) -> float:
    import re
    match = re.search(r'data-execution-authority="([^"]*)"', html)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _node_type_of(html: str) -> str:
    import re
    match = re.search(r'data-node-type="([^"]*)"', html)
    return match.group(1).strip() if match else ""


def load_workflow_spec(node_id: str) -> dict[str, Any] | None:
    """
    The runnable workflow inside a node, or None if there is not one.

    Shared by ``execute_node`` and the ``workflow.execute`` RPC so both run the
    same document. Returns None rather than raising for every "cannot run this"
    case -- missing, wrong type, no authority, no spec -- because the RPC path
    has a JSON sidecar to fall back to and should take it quietly.
    """
    path = _find_node(str(node_id or "").strip())
    if path is None:
        return None
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if _node_type_of(html) not in EXECUTABLE_TYPES:
        return None
    if _authority_of(html) < MIN_EXECUTION_AUTHORITY:
        log.warning(
            "load_workflow_spec: %s declares authority %.2f, below the %.2f "
            "required to run.", path.stem, _authority_of(html), MIN_EXECUTION_AUTHORITY,
        )
        return None

    from sdk.agent.parser import parse_html
    try:
        spec = parse_html(html).state_data
    except Exception:
        return None
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError:
            return None
    if not isinstance(spec, dict) or not spec.get("nodes"):
        return None
    return spec


async def execute_node_by_id(
    node_id: str,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the workflow a node describes. See :func:`execute_node`."""
    node_id = str(node_id or "").strip()
    if not node_id:
        return {"success": False, "error": "node_id is required."}

    path = _find_node(node_id)
    if path is None:
        return {
            "success": False,
            "error": (
                f"No node named {node_id!r} was found. Use list_neural_nodes to "
                f"see what exists."
            ),
        }

    try:
        html = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"success": False, "error": f"Could not read {path.name}: {exc}"}

    node_type = _node_type_of(html)
    if node_type not in EXECUTABLE_TYPES:
        # Naming the type matters: "this is a documentation node" tells the
        # caller to stop looking for a way to run it, where a bare refusal
        # invites a retry.
        return {
            "success": False,
            "error": (
                f"{path.stem} is a {node_type or 'untyped'} node, which describes "
                f"something rather than doing it. Only {sorted(EXECUTABLE_TYPES)} "
                f"nodes can be executed. Use read_neural_node to read it instead."
            ),
        }

    authority = _authority_of(html)
    if authority < MIN_EXECUTION_AUTHORITY:
        return {
            "success": False,
            "error": (
                f"{path.stem} declares execution authority {authority:.2f}, below "
                f"the {MIN_EXECUTION_AUTHORITY:.2f} required to run. A node that "
                f"does not claim the authority to act must not be made to act."
            ),
        }

    from sdk.agent.parser import parse_html
    try:
        parsed = parse_html(html)
    except Exception as exc:
        return {"success": False, "error": f"Could not parse {path.name}: {exc}"}

    spec = parsed.state_data
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError:
            spec = None
    if not isinstance(spec, dict) or not spec.get("nodes"):
        return {
            "success": False,
            "error": (
                f"{path.stem} carries no runnable workflow in its node-state "
                f"block. The node documents a flow but does not contain one."
            ),
        }

    from python.harness.workflow_executor import WorkflowExecutor

    log.info(
        "execute_node: running %s (%d step(s)) from its HTML-NN",
        path.stem, len(spec.get("nodes") or []),
    )
    executor = WorkflowExecutor(spec)
    result = await executor.execute(dict(inputs or {}))

    status = str(result.get("status") or "").upper()
    succeeded = status in ("COMPLETED", "SUCCESS")
    return {
        # The executor reports FAILED in-band. Surfacing that as success -- the
        # "status: failed read as success" pattern -- is what this whole audit
        # keeps finding, so it is mapped explicitly here.
        "success": succeeded,
        "node_id": path.stem,
        "status": status,
        "executed_from": "html-nn",
        "execution_path": result.get("execution_path") or [],
        "variables": result.get("variables") or {},
        "error": None if succeeded else f"Workflow finished with status {status}.",
    }


@register_tool(
    name="execute_node",
    description=(
        "Run the workflow described by an HTML Neural Node, passing optional "
        "inputs. Only workflow nodes that declare execution authority can be "
        "run; documentation and tool-catalogue nodes cannot. Use "
        "list_neural_nodes to find one and read_neural_node to inspect it first."
    ),
)
async def execute_node(node_id: str, inputs: dict | None = None) -> dict:
    """
    NAME
           execute_node - act on an HTML Neural Node

    SYNOPSIS
           execute_node(node_id: str, inputs: dict = None) -> dict

    DESCRIPTION
           Completes the read/enumerate/write/act set. Tala could already read
           a node, list nodes and update one, but had no way to run what a node
           described -- so a compiled workflow node documented how to execute
           itself to a reader with no mechanism to do so.

           The workflow specification is read from the node's own node-state
           block rather than from a JSON sidecar, so the document that was
           retrieved is the document that runs.

           Refuses any node whose type is not a workflow, and any node
           declaring execution authority below 0.5. Returns success=False with
           the reason rather than raising, and never reports a failed workflow
           as a successful call.

    PARAMETERS
           node_id - the node to run, with or without its "flow-" prefix
           inputs  - optional values available to the first step
    """
    return await execute_node_by_id(node_id, inputs)


@register_tool(
    name="list_executable_nodes",
    description=(
        "List the HTML Neural Nodes that can actually be executed -- compiled "
        "workflows with execution authority -- with their names and step counts."
    ),
)
async def list_executable_nodes() -> dict:
    """
    NAME
           list_executable_nodes - what can be run, as opposed to read

    SYNOPSIS
           list_executable_nodes() -> dict

    DESCRIPTION
           list_neural_nodes returns everything, and the overwhelming majority
           of nodes are documentation that cannot be run. This narrows it to
           what execute_node will accept, so a plan is not built around a node
           that will be refused.
    """
    directory = _nodes_dir()
    found = []
    for path in sorted(directory.glob("*.html")):
        try:
            html = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _node_type_of(html) not in EXECUTABLE_TYPES:
            continue
        if _authority_of(html) < MIN_EXECUTION_AUTHORITY:
            continue
        entry: dict[str, Any] = {"node_id": path.stem, "authority": _authority_of(html)}
        try:
            from sdk.agent.parser import parse_html
            spec = parse_html(html).state_data
            if isinstance(spec, dict):
                entry["name"] = spec.get("name") or path.stem
                entry["steps"] = len(spec.get("nodes") or [])
                entry["description"] = spec.get("description") or ""
        except Exception:
            entry["name"] = path.stem
        found.append(entry)

    return {"success": True, "count": len(found), "nodes": found}
