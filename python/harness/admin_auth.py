"""
python/harness/admin_auth.py
=============================
Shared harness admin-secret gate.

A single local secret (auto-generated on first use, stored under
vault/.keys/harness_admin_secret.txt) gates admin-tier self-modification
RPC methods (code pull/redeploy) as well as the MCP server's tool-call
surface. Only a process that can read that file on the same machine
(the local Electron main process, or a local script/test) can supply it.

Kept as a standalone module (no dependency on the rest of server.py) so
other local-only surfaces -- like mcp_server.py -- can reuse the exact
same secret/logic without importing the entire harness.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def harness_admin_secret_path() -> Path:
    configured = os.environ.get("TALA_HARNESS_ADMIN_SECRET_FILE")
    if configured:
        return Path(configured).expanduser().resolve()

    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        return Path(user_data).expanduser().resolve() / "vault" / ".keys" / "harness_admin_secret.txt"

    return _ROOT_DIR / "vault" / ".keys" / "harness_admin_secret.txt"


def load_or_create_harness_admin_secret() -> str:
    env_secret = os.environ.get("TALA_HARNESS_ADMIN_SECRET")
    if env_secret:
        return env_secret

    path = harness_admin_secret_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()

    import secrets
    secret = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    return secret


def require_harness_admin_secret(params: dict[str, Any]) -> bool:
    """
    Gate for admin-level self-modification RPC methods (code pull/redeploy)
    and for the MCP server's tool-call surface.  Only the local Electron
    main process (which reads the same file from the same machine) knows
    this secret -- any other caller, including a remote peer on a shared/VPS
    deployment or an external MCP client, is rejected.
    """
    provided = str(params.get("harness_secret", ""))
    expected = load_or_create_harness_admin_secret()
    return bool(provided) and provided == expected
