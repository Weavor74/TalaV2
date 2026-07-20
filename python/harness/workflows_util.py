"""
workflows_util.py - Auto-generated documentation base.
"""

import os
import json
import logging
from pathlib import Path

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
