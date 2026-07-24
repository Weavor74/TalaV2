"""
workflows_util.py - Auto-generated documentation base.
"""

import os
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Root directory of the repository (harness/.. is python/.., python/../.. is TalaV2)
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent

def resolve_workflow_file(flow_id: str) -> Path | None:
    """
    Look for a workflow JSON file.
    Checks user/AppData directories first, then falls back to repository workflows.
    """
    # 1. Resolve user data or AppData/Roaming workflows directory
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        workflows_dir = Path(user_data) / "workflows"
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            workflows_dir = Path(appdata) / "tala-engine" / "workflows"
        else:
            workflows_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "workflows"
            
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
    List all workflows from both the AppData/user-data workflows directory
    and the repository workflows directory, deduplicated by flow_id (with AppData taking precedence).
    """
    # 1. Resolve directories
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        appdata_dir = Path(user_data) / "workflows"
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            appdata_dir = Path(appdata) / "tala-engine" / "workflows"
        else:
            appdata_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "workflows"

    repo_dir = _ROOT_DIR / "workflows"

    workflow_files = {}  # flow_id -> Path

    # First load repo defaults
    if repo_dir.exists() and repo_dir.is_dir():
        for f in repo_dir.glob("*.json"):
            # Deduplicate by stem (flow_id)
            workflow_files[f.stem] = f

    # Then overlay with AppData user overrides
    if appdata_dir.exists() and appdata_dir.is_dir():
        for f in appdata_dir.glob("*.json"):
            workflow_files[f.stem] = f

    workflows = []
    for f in workflow_files.values():
        try:
            content = f.read_text(encoding="utf-8")
            workflows.append(json.loads(content))
        except Exception as e:
            logger.warning("Could not load workflow file %s: %s", f, e)

    return workflows


async def save_workflow_data(workflow: dict[str, Any]) -> str:
    """
    Save a workflow configuration JSON, compile it to HTML-NN, and anchor it.
    Returns the flow_id.
    """
    import uuid
    flow_id = workflow.get("flow_id")
    if not flow_id:
        flow_id = str(uuid.uuid4())
        workflow["flow_id"] = flow_id

    name = workflow.get("name", "Unnamed Flow")

    # 1. Resolve directories
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        workflows_dir = Path(user_data) / "workflows"
        nodes_dir = Path(user_data) / "nodes"
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            workflows_dir = Path(appdata) / "tala-engine" / "workflows"
            nodes_dir = Path(appdata) / "tala-engine" / "nodes"
        else:
            workflows_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "workflows"
            nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"

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

