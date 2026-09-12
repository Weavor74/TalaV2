"""
JSON-RPC 2.0 response plumbing, extracted from server.py (P2 review item 18).

Every handler — in server.py or in an extracted per-domain module — builds its
responses through ``_ok``/``_err`` here. This module exists so an extracted
handler module never has to import server.py (which would be circular: server
imports the handler modules to build its dispatch map). server.py re-exports
these names, so its own thousands of call sites and the tests that reach for
``server.JsonRpcError`` are unaffected.

``_err`` carries the audit hook: every RPC error response funnels through it,
which is why the hook is here rather than in the dispatcher — most handlers
catch their own exceptions and return ``_err`` directly, so the dispatcher's
``except`` never sees them. Before this, ``agent_log`` held 44,464 rows of
which exactly **one** was an ERROR (UI-085).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
from typing import Any

from python.harness import db

JSONRPC_VERSION = "2.0"


class JsonRpcError:
    """Standard JSON-RPC 2.0 error codes."""
    PARSE_ERROR      = -32700
    INVALID_REQUEST  = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS   = -32602
    INTERNAL_ERROR   = -32603


def _ok(request_id: Any, result: Any) -> str:
    return json.dumps({
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "result": result,
    })


#: The RPC method currently being handled, so _err can say what failed without
#: every handler having to pass its own name in. Set by server.py's dispatcher.
_CURRENT_RPC: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_CURRENT_RPC", default=""
)

#: Failures that are the caller getting it wrong rather than the harness
#: breaking. Still logged, but at WARNING: an audit log where a typo'd
#: parameter looks the same as a crashed subsystem is one nobody reads.
_CALLER_FAULT_CODES = frozenset({
    JsonRpcError.INVALID_PARAMS,
    JsonRpcError.METHOD_NOT_FOUND,
    JsonRpcError.INVALID_REQUEST,
    JsonRpcError.PARSE_ERROR,
})


def _audit_rpc_failure(code: int, message: str, data: Any = None) -> None:
    """
    Record an RPC failure in ``agent_log``.

    Fire-and-forget by design: a failure to log a failure must not become a
    second failure. If there is no running loop -- a synchronous test, or
    ``_err`` called during startup -- it is skipped rather than raised.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    level = "WARNING" if code in _CALLER_FAULT_CODES else "ERROR"
    method = _CURRENT_RPC.get() or "unknown"
    trace: dict[str, Any] = {"method": method, "code": code}
    if isinstance(data, dict):
        # Keep the traceback, drop anything large enough to bloat the table.
        for key in ("traceback", "reason", "detail"):
            if key in data:
                trace[key] = str(data[key])[:2000]

    try:
        loop.create_task(
            db.log_to_db(level, f"{method}: {message}"[:1000], trace)
        )
    except Exception:  # pragma: no cover - never let logging break a response
        pass


def _err(request_id: Any, code: int, message: str, data: Any = None) -> str:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _audit_rpc_failure(code, message, data)
    return json.dumps({
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": error,
    })
