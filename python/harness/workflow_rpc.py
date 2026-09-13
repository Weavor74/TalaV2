"""
python/harness/workflow_rpc.py — the workflow editor's RPC surface.

Seven handlers (workflow.list/save/delete/execute/generate_from_tot and
flow.node_types/validate), moved out of server.py because the size ratchet
(tests/test_size_ratchet.py) pins that file and because they are one domain:
everything the visual builder and the trigger service need to list, check,
persist, run and generate flows. The heavy lifting stays where it always was
— workflows_util owns persistence and loading, flow_schema owns the registry
and validation, workflow_executor owns running — this module is the wire
format around them, exactly like desktop_rpc.py before it.

Like desktop_rpc.py, harness and sdk imports stay inside the functions: this
module is imported at server import time, and sdk.agent.tools pulls the
harness back in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection

from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)

#: Repo root for the dev-environment fallbacks below — the same derivation
#: workflows_util uses (python/harness/ -> python/ -> repo).
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent


async def handle_workflow_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``workflow.list`` — Retrieve all configured workflows.
    """
    try:
        from python.harness.workflows_util import get_all_workflows
        workflows = get_all_workflows()
        return _ok(req_id, {"workflows": workflows})
    except Exception as exc:
        logger.exception("workflow.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_workflow_save(params: dict[str, Any], req_id: Any) -> str:
    """
    ``workflow.save`` — Create or update a workflow configuration.
    """
    workflow = params.get("workflow")
    if not workflow or not isinstance(workflow, dict):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'workflow' (dict) is required")

    try:
        from python.harness.workflows_util import InvalidWorkflow, save_workflow_data
        flow_id = await save_workflow_data(workflow)
        return _ok(req_id, {"success": True, "flow_id": flow_id})
    except InvalidWorkflow as invalid:
        # Refused, with the reasons, rather than a 500. The renderer shows
        # these against the steps they belong to, so "it would not save" is
        # never the whole of what the user is told.
        return _ok(req_id, {
            "success": False,
            "findings": [f.as_dict() for f in invalid.findings],
        })
    except Exception as exc:
        logger.exception("workflow.save error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_flow_node_types(params: dict[str, Any], req_id: Any) -> str:
    """
    ``flow.node_types`` — The palette, from the registry both engines dispatch on.

    Served rather than hardcoded in the renderer because the two lists drifted
    in both directions when they were kept separately: types with executor
    branches and no palette entry, and a palette entry with no executor branch.
    """
    try:
        import re as _re

        from sdk.agent import flow_schema
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir

        payload = flow_schema.describe_node_types()

        # The nodes a step may point at, so the Node/Decision/State fields can
        # offer real candidates. A free-text box invites a name that does not
        # exist, and a step naming a node that is not there fails at run time
        # having looked perfectly fine on the canvas.
        # Only the types a step can actually name: something runnable for a
        # Node or Decision step, and state for a State step. Tools come from
        # the catalogue, and the rest of the wiki is documentation a flow has
        # no way to invoke.
        from sdk.agent.node_execution import EXECUTABLE_TYPES, STATE_TYPE

        wanted = set(EXECUTABLE_TYPES) | {STATE_TYPE}
        referenceable: list[dict[str, Any]] = []
        try:
            nodes_dir = resolve_active_nodes_dir()
            for path in sorted(nodes_dir.glob("*.html")):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                # Both spellings, and the whole file. A node declares its type
                # on the <article> and again as a meta tag, and which comes
                # first depends on how long the document's <style> block is --
                # reading a fixed prefix of one attribute found exactly one
                # node out of three hundred.
                match = (
                    _re.search(r'data-node-type="([^"]*)"', text)
                    or _re.search(r'name="tala-node-type"\s+content="([^"]*)"', text)
                )
                node_type = (match.group(1).strip().lower() if match else "")
                if node_type in wanted:
                    referenceable.append({"id": path.stem, "type": node_type})
        except Exception as exc:
            logger.warning("flow.node_types: could not list referenceable nodes: %s", exc)
        payload["nodes"] = referenceable

        return _ok(req_id, payload)
    except Exception as exc:
        logger.exception("flow.node_types error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_flow_validate(params: dict[str, Any], req_id: Any) -> str:
    """
    ``flow.validate`` — What is wrong with a workflow, without saving it.

    The same call ``workflow.save`` makes, so the editor's warnings and the
    server's refusals cannot disagree.
    """
    workflow = params.get("workflow")
    if not isinstance(workflow, dict):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'workflow' (dict) is required")
    try:
        from sdk.agent import flow_schema
        from python.harness.workflows_util import check_workflow

        findings = check_workflow(workflow)
        return _ok(req_id, {
            "findings": [f.as_dict() for f in findings],
            "ok": not flow_schema.errors(findings),
            "flow": flow_schema.normalize(workflow),
        })
    except Exception as exc:
        logger.exception("flow.validate error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_workflow_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``workflow.delete`` — Delete a workflow configuration.
    """
    flow_id = params.get("flow_id")
    if not flow_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'flow_id' is required")

    try:
        from python.harness import db
        from python.harness.settings_nn import update_settings_workflows_node
        from sdk.agent.documentation_nodes import (
            resolve_active_nodes_dir,
            resolve_active_workflows_dir,
        )

        user_data = os.environ.get("TALA_USER_DATA")
        workflows_dir = resolve_active_workflows_dir()
        nodes_dir = resolve_active_nodes_dir()

        # Fallbacks for dev env
        if not workflows_dir.exists() and not user_data:
            workflows_dir = Path(_ROOT_DIR) / "workflows"
        if not nodes_dir.exists() and not user_data:
            nodes_dir = Path(_ROOT_DIR) / "nodes"

        # Delete JSON
        json_path = workflows_dir / f"{flow_id}.json"
        if json_path.exists():
            json_path.unlink()

        # Delete HTML-NN
        html_path = nodes_dir / f"flow-{flow_id}.html"
        if html_path.exists():
            html_path.unlink()

        await db.log_to_db("INFO", f"Deleted workflow ID: {flow_id} and its HTML-NN", {"flow_id": flow_id})
        try:
            await update_settings_workflows_node()
        except Exception as exc:
            logger.warning("Failed to update workflows settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("workflow.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_workflow_execute(
    params: dict[str, Any],
    ws: ServerConnection,
    req_id: Any,
) -> str:
    """
    ``workflow.execute`` — Execute a workflow node-by-node.
    """
    flow_id = params.get("flow_id")
    inputs = params.get("inputs", {})

    if not flow_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'flow_id' is required")

    try:
        # Node first, sidecar fallback — the shared loader in workflows_util
        # owns that rule so this handler and the trigger service can never
        # disagree about what a flow is.
        from python.harness.workflows_util import load_flow_document
        workflow_data, source = await asyncio.to_thread(load_flow_document, flow_id)
        if workflow_data is None:
            return _err(
                req_id, JsonRpcError.INVALID_PARAMS,
                f"Workflow '{flow_id}' does not exist as a node or a saved file.",
            )

        # Define step callback to stream updates
        async def step_callback(node_id: str, status: str, message: str, logs: list[str]) -> None:
            notification = json.dumps({
                "jsonrpc": "2.0",
                "method": "workflow.execute.step",
                "params": {
                    "flow_id": flow_id,
                    "node_id": node_id,
                    "status": status,
                    "message": message,
                    "logs": logs
                }
            })
            await ws.send(notification)

        from python.harness.workflow_executor import WorkflowExecutor
        executor = WorkflowExecutor(workflow_data, step_callback)
        result = await executor.execute(inputs)
        result["executed_from"] = source

        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("workflow.execute error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_workflow_generate_from_tot(params: dict[str, Any], req_id: Any) -> str:
    """
    ``workflow.generate_from_tot`` — Generate a workflow from ToT steps.
    """
    prompt = params.get("prompt")
    if not prompt:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'prompt' is required")
        
    try:
        from sdk.agent.tools import generate_workflow_from_tot_tool
        result = await generate_workflow_from_tot_tool(
            prompt=prompt,
            name=params.get("name"),
            description=params.get("description")
        )
        if result.get("success"):
            return _ok(req_id, result)
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, result.get("error", "Failed to generate workflow"))
    except Exception as exc:
        logger.exception("workflow.generate_from_tot error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
