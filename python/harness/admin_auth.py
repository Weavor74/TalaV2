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

import hmac
import os
from pathlib import Path
from typing import Any

_ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def _harness_admin_secret_path() -> Path:
    configured = os.environ.get("TALA_HARNESS_ADMIN_SECRET_FILE")
    if configured:
        return Path(configured).expanduser().resolve()

    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        return Path(user_data).expanduser().resolve() / "vault" / ".keys" / "harness_admin_secret.txt"

    return _ROOT_DIR / "vault" / ".keys" / "harness_admin_secret.txt"


def _restrict_permissions(path: Path, mode: int) -> None:
    """Best-effort chmod. Self-heals a secret file/dir created before this
    hardening existed. No-op on platforms without POSIX permission bits."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _load_or_create_harness_admin_secret() -> str:
    env_secret = os.environ.get("TALA_HARNESS_ADMIN_SECRET")
    if env_secret:
        return env_secret

    path = _harness_admin_secret_path()
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        # Self-heal: earlier versions relied on the process umask (commonly
        # 022 on Linux -> world-readable 644) instead of setting permissions
        # explicitly, so a pre-existing file may still be loosely permissioned.
        _restrict_permissions(path, 0o600)
        return secret

    import secrets
    secret = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(path.parent, 0o700)
    # Open with an explicit owner-only mode instead of Path.write_text(),
    # which creates the file at the umask-derived default (typically 644 —
    # world-readable) rather than anything gated by this file's contents.
    # This secret gates git-pull/git-archive self-modification RPCs and,
    # whenever the MCP server is enabled, its entire tool-call surface, so a
    # world-readable secret file would let any other local user/process on
    # the machine read it straight off disk and bypass the gate entirely.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.encode("utf-8"))
    finally:
        os.close(fd)
    _restrict_permissions(path, 0o600)
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
    expected = _load_or_create_harness_admin_secret()
    if not provided or not expected:
        return False
    # Constant-time comparison — a plain `==` short-circuits on the first
    # mismatched character, which is a textbook timing side-channel (CWE-208)
    # for a secret that gates self-modification RPCs and the full MCP tool
    # surface. hmac.compare_digest is the standard fix.
    return hmac.compare_digest(provided, expected)
