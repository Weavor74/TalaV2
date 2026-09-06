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
EXECUTABLE_TYPES = frozenset({"workflow", "flow", "function"})

#: The subset that carries code rather than naming steps somebody else
#: implemented.
#:
#: A workflow names steps existing Python interprets, so the worst a hostile
#: one can do is ask for a bad arrangement of things already written. A
#: function node carries the code itself, which is a different kind of
#: object and needs a different kind of permission -- see node_admission.
CODE_BEARING_TYPES = frozenset({"function"})

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

    if node_type in CODE_BEARING_TYPES:
        return await _run_function_node(path, html, parsed, dict(inputs or {}))

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


async def _run_function_node(
    path: Path, html: str, parsed: Any, inputs: dict[str, Any]
) -> dict[str, Any]:
    """
    Run the code a function node carries, if it has been admitted.

    Two checks the workflow path does not need, because a workflow only ever
    asks for arrangements of code somebody already wrote:

    * **Admission.** The node's own fields cannot grant this. Permission is a
      row keyed by node id and content hash, so an admission covers the exact
      code that was read; editing the node makes it inert again rather than
      inheriting the old decision. See sdk/agent/node_admission.py.
    * **Something to run.** A function node with no ``code`` is a document
      that declared the wrong type, and saying so beats a confusing failure
      further in.

    The code itself runs through the same machinery as ``run_tool_script``:
    an async body with ``call_tool`` in scope and a timeout. Reusing it means
    a function node can do exactly what a script could and nothing more --
    one execution surface to reason about rather than two.
    """
    from sdk.agent.node_admission import is_admitted

    state = parsed.state_data
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except json.JSONDecodeError:
            state = {}
    state = state or {}

    code = str(state.get("code") or "").strip()
    if not code:
        return {
            "success": False,
            "error": (
                f"{path.stem} is a function node with no code in its node-state "
                f"block. It documents a function rather than carrying one."
            ),
        }

    ok, why_not = await is_admitted(parsed.node_id or path.stem, html)
    if not ok:
        log.warning("execute_node: refused function node %s: %s", path.stem, why_not)
        return {
            "success": False,
            "error": f"{path.stem} is not admitted to run: {why_not}",
            "admitted": False,
        }

    timeout = float(state.get("timeout_seconds") or 120.0)
    log.info("execute_node: running function node %s (%d chars of code)", path.stem, len(code))

    from sdk.agent.tools import run_tool_script

    # inputs reach the script as `inputs`, so a function node is a callable
    # unit rather than a fixed script: the workflow that coordinates them
    # passes each one what the previous produced.
    prelude = f"inputs = {inputs!r}\n"
    result = await run_tool_script(prelude + code, timeout_seconds=timeout)

    # run_tool_script names these the other way round: `output` is captured
    # stdout and `result` is what the script returned. Renamed here so a
    # function node's caller reads its value from `output`, matching every
    # other execute_node result rather than the script runner's convention.
    return {
        "success": bool(result.get("success")),
        "node": path.stem,
        "node_type": "function",
        "output": result.get("result"),
        "stdout": result.get("output", ""),
        "error": result.get("error"),
    }


@register_tool(
    name="create_function_node",
    description=(
        "Author an HTML-NN that carries executable Python. The node is written and "
        "recorded but NOT admitted to run: a person reviews the code and admits it "
        "before it can execute."
    ),
)
async def create_function_node(
    node_id: str,
    title: str,
    code: str,
    description: str = "",
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """
    NAME
           create_function_node - write a node that carries code

    DESCRIPTION
           Writes a `function` node whose node-state block holds `code`, and
           records its arrival as locally authored. It cannot run yet.

           Authoring does not admit, even here. The obvious shortcut is to
           treat locally written code as reviewed -- you wrote it, after all
           -- but this tool is in the registry, so the writer is as often
           the model as the person, and self-admission would make "never a
           start without review" true only for code that came from
           elsewhere. That is precisely backwards: code Tala writes for
           herself is the case the review exists for.

           So the flow is the same for everyone. Write, read, admit.

           The code runs with `call_tool(name, **kwargs)` in scope, `inputs`
           bound to whatever the caller passed, and `return` for its value --
           the same contract as run_tool_script.
    """
    from sdk.agent.node_admission import ORIGIN_LOCAL, record_arrival

    node_id = str(node_id or "").strip()
    code = str(code or "").strip()
    if not node_id:
        return {"success": False, "error": "node_id is required."}
    if not code:
        return {"success": False, "error": "A function node with no code is a document."}

    # Written here rather than through generate_detailed_neural_node, which
    # resolves its own nodes directory (workspace_root/"nodes", or
    # TALA_USER_DATA) instead of resolve_active_nodes_dir() -- the one
    # execute_node reads. Two resolvers that can disagree put the file
    # somewhere the reader does not look, which is exactly what happened:
    # "written but cannot be found again". The writer must target the
    # directory the reader uses.
    directory = _nodes_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{node_id}.html"

    state = json.dumps(
        {"title": title or node_id, "code": code, "timeout_seconds": float(timeout_seconds)},
        indent=2, ensure_ascii=False,
    )
    body = description or f"Executable function node {node_id}."
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="tala-schema" content="neural-node/v2">
<title>Neural Node: {title or node_id}</title></head>
<body>
<article
  data-node-id="{node_id}"
  data-node-type="function"
  data-schema-version="neural-node/v2"
  data-execution-authority="{MIN_EXECUTION_AUTHORITY:.2f}"
  data-memory-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity"><h1 id="node-title">{title or node_id}</h1></header>

  <section id="semantic-memory">
    <p>{body}</p>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul></ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{state}
  </script>
</article>
</body>
</html>
"""
    try:
        path.write_text(html, encoding="utf-8")
    except OSError as exc:
        log.exception("create_function_node: could not write %s", node_id)
        return {"success": False, "error": str(exc)}

    record = await record_arrival(
        node_id, path.read_text(encoding="utf-8"), ORIGIN_LOCAL, admitted=False,
    )

    return {
        "success": True,
        "node": path.name,
        "content_hash": record["content_hash"][:16],
        "admitted": False,
        "next_step": (
            f"{node_id} is written and cannot run yet. Review the code, then admit it "
            f"with a note saying what it does."
        ),
    }
