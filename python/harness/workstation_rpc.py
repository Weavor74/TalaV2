"""
python/harness/workstation_rpc.py -- Settings > Workspace.

``workstation.get_config``         the live workspace, its source, a suggestion.
``workstation.validate_workspace`` dry-run check of a folder (nothing created).
``workstation.set_workspace``      validate, create, save and apply immediately.

These are owner settings, reached from the Settings UI over the local socket
(peers never get them: vps_mode only relays its peer surface). They are not
tools, so the model cannot move its own sandbox. Kept out of server.py, which
the size ratchet pins.
"""

from __future__ import annotations

import logging
from typing import Any

from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)


async def _handle_workstation_get_config(params: dict[str, Any], req_id: Any) -> str:
    from sdk.agent import workstation

    return _ok(req_id, workstation.workspace_status())


async def _handle_workstation_validate_workspace(params: dict[str, Any], req_id: Any) -> str:
    from sdk.agent import workstation

    path = (params or {}).get("path") or (params or {}).get("root") or ""
    return _ok(req_id, workstation.validate_folder(str(path)))


async def _handle_workstation_set_workspace(params: dict[str, Any], req_id: Any) -> str:
    from sdk.agent import workstation

    params = params or {}
    root = params.get("root") or params.get("path")
    if not isinstance(root, str) or not root.strip():
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "root (a folder path) is required")
    extra = params.get("extra_roots")
    if extra is not None and not isinstance(extra, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "extra_roots must be a list of folder paths")
    result = workstation.set_workspace(root, extra)
    if result.get("success"):
        logger.info("workstation: workspace set to %s", result.get("root"))
    return _ok(req_id, result)


WORKSTATION_RPC_HANDLERS: dict[str, Any] = {
    "workstation.get_config": _handle_workstation_get_config,
    "workstation.validate_workspace": _handle_workstation_validate_workspace,
    "workstation.set_workspace": _handle_workstation_set_workspace,
}
