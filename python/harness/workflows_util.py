"""
workflows_util.py - Auto-generated documentation base.
"""

import os
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from sdk.agent.documentation_nodes import resolve_active_nodes_dir, resolve_active_workflows_dir

logger = logging.getLogger(__name__)

# Root directory of the repository (harness/.. is python/.., python/../.. is TalaV2)
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent

def resolve_workflow_file(flow_id: str) -> Path | None:
    """
    Look for a workflow JSON file.
    Checks user/AppData directories first, then falls back to repository workflows.
    """
    workflows_dir = resolve_active_workflows_dir()
    appdata_file = workflows_dir / f"{flow_id}.json"
    if appdata_file.exists():
        return appdata_file

    # 2. Fall back to repository workflows directory
    repo_file = _ROOT_DIR / "workflows" / f"{flow_id}.json"
    if repo_file.exists():
        return repo_file

    return None

def get_all_workflows() -> list[dict]:
    """
    Every workflow the editor can open, normalised to one schema.

    Three sources, in increasing precedence: the repository's bundled JSON, the
    user's saved JSON, and the compiled HTML-NN.

    The nodes are read here because ``workflow.execute`` reads the node first
    and this listing read only the JSON sidecars, so the two disagreed about
    what a workflow was: a flow that existed only as a node -- one Tala wrote
    directly, or one whose sidecar was cleaned up -- ran perfectly and never
    appeared in the list, and a flow whose node had been edited listed under
    its stale sidecar's contents. The node wins for the same reason it wins at
    execution time: it is the document the system actually consumes.
    """
    from sdk.agent import flow_schema

    sources: dict[str, dict] = {}

    def _ingest(path: Path, loader) -> None:
        try:
            flow = flow_schema.normalize(loader(path))
        except Exception as exc:
            logger.warning("Could not load workflow %s: %s", path, exc)
            return
        if not flow.get("flow_id"):
            flow["flow_id"] = path.stem.removeprefix("flow-")
        sources[flow["flow_id"]] = flow

    def _json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _node(path: Path) -> dict:
        from sdk.agent.parser import parse_file

        state = parse_file(str(path)).state_data or {}
        if isinstance(state, str):
            state = json.loads(state)
        return state

    repo_dir = _ROOT_DIR / "workflows"
    for directory in (repo_dir, resolve_active_workflows_dir()):
        if directory.exists() and directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                _ingest(path, _json)

    nodes_dir = resolve_active_nodes_dir()
    if nodes_dir.exists() and nodes_dir.is_dir():
        for path in sorted(nodes_dir.glob("flow-*.html")):
            _ingest(path, _node)

    return list(sources.values())


class InvalidWorkflow(ValueError):
    """A workflow that must not be saved, with the reasons attached."""

    def __init__(self, findings: list[Any]) -> None:
        self.findings = findings
        super().__init__("; ".join(f.message for f in findings))


def check_workflow(workflow: dict[str, Any]) -> list[Any]:
    """
    Everything wrong with a workflow, checked against the live system.

    Separate from :func:`save_workflow_data` so the editor can ask the same
    question while the user is still drawing, and get the same answer. A
    validator the UI reimplements is one that disagrees with the server on the
    day it matters.
    """
    from sdk.agent import flow_schema

    known_tools: set[str] | None
    try:
        from sdk.agent.tools import ToolRegistry

        known_tools = set(ToolRegistry._tools.keys())
    except Exception:
        known_tools = None

    known_nodes: set[str] | None
    try:
        nodes_dir = resolve_active_nodes_dir()
        known_nodes = {p.stem for p in nodes_dir.glob("*.html")} if nodes_dir.exists() else None
    except Exception:
        known_nodes = None

    return flow_schema.validate(workflow, known_tools=known_tools, known_nodes=known_nodes)


async def save_workflow_data(workflow: dict[str, Any]) -> str:
    """
    Save a workflow configuration JSON, compile it to HTML-NN, and anchor it.
    Returns the flow_id.

    Validated first. Nothing checked a workflow anywhere before it was written
    -- not the editor, not this function, not the compiler -- so a flow with a
    connection pointing at a step that had been deleted, or a tool step missing
    the argument the tool requires, saved successfully, compiled to a node,
    got anchored on the chain and indexed into memory. The first sign of
    trouble was a run that stopped in the middle, or did not stop.
    """
    from sdk.agent import flow_schema

    import uuid

    # Normalised before anything reads it, so the document on disk, the node
    # compiled from it and the flow handed to the executor are the same shape.
    workflow = flow_schema.normalize(workflow)

    problems = flow_schema.errors(check_workflow(workflow))
    if problems:
        raise InvalidWorkflow(problems)

    flow_id = workflow.get("flow_id")
    if not flow_id:
        flow_id = str(uuid.uuid4())
        workflow["flow_id"] = flow_id

    name = workflow.get("name", "Unnamed Flow")

    # 1. Resolve directories
    user_data = os.environ.get("TALA_USER_DATA")
    workflows_dir = resolve_active_workflows_dir()
    nodes_dir = resolve_active_nodes_dir()

    # Fallbacks for dev env
    if not workflows_dir.exists() and not user_data:
        workflows_dir = _ROOT_DIR / "workflows"
    if not nodes_dir.exists() and not user_data:
        nodes_dir = _ROOT_DIR / "nodes"

    workflows_dir.mkdir(parents=True, exist_ok=True)
    nodes_dir.mkdir(parents=True, exist_ok=True)

    # 2. Save JSON file
    json_path = workflows_dir / f"{flow_id}.json"
    json_path.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")

    # 3. Compile to HTML-NN and save to nodes/
    from sdk.agent.workflow_compiler import compile_workflow_to_html
    html_nn = compile_workflow_to_html(workflow)
    html_path = nodes_dir / f"flow-{flow_id}.html"
    html_path.write_text(html_nn, encoding="utf-8")

    try:
        from sdk.agent.anchor import canonicalize_and_anchor
        canonicalize_and_anchor(
            f"flow-{flow_id}",
            html_nn,
            workflow,
            None,
            creator="tala-system",
            document_type="html-nn",
        )
    except Exception as anchor_exc:
        logger.warning("Failed to anchor workflow %s on blockchain: %s", flow_id, anchor_exc)

    # Log action to DB
    from python.harness import db
    await db.log_to_db("INFO", f"Saved workflow '{name}' (ID: {flow_id}) and compiled HTML-NN", {"flow_id": flow_id})
    try:
        from python.harness.settings_nn import update_settings_workflows_node
        await update_settings_workflows_node()
    except Exception as exc:
        logger.warning("Failed to update workflows settings node: %s", exc)

    return flow_id

