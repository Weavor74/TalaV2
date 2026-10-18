"""
grant_workstation_tools.py -- give a room agent Tala's workstation tools.

The main chat already offers the workstation tools (she acts as the owner
there). Room agents only see the tools in their config.tool_allowlist, and
AuthorizationGate only lets them run tools they hold an execute_tool grant
for. This adds the workstation tools to both, the same way the agent editor's
save does (sdk/agent/agent_tool_grants.py).

    python scripts/grant_workstation_tools.py --list
    python scripts/grant_workstation_tools.py --agent Tala
    python scripts/grant_workstation_tools.py --agent Tala --core-only
    python scripts/grant_workstation_tools.py --agent Tala --revoke
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", help="agent id or name")
    ap.add_argument("--list", action="store_true", help="list agents and their allowlists")
    ap.add_argument("--core-only", action="store_true", help="grant only run_shell/read/write/list/run_python")
    ap.add_argument("--revoke", action="store_true", help="remove the workstation tools instead")
    opts = ap.parse_args()

    from python.harness import db
    from sdk.agent.agent_tool_grants import sync_tool_grants

    names = ("run_shell", "read_file", "write_file", "list_dir", "run_python") if opts.core_only else (
        "run_shell", "read_file", "write_file", "list_dir", "search_files", "run_python",
        "delete_path", "install_python_package", "start_job", "job_status", "stop_job")

    await db.init_pool()
    try:
        if opts.list or not opts.agent:
            for row in await db.fetch("SELECT id, name, config FROM agents ORDER BY name"):
                cfg = row["config"]
                cfg = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
                print(f"{row['id']}  {row['name']}  tools={cfg.get('tool_allowlist')}")
            return 0
        row = await db.fetchrow("SELECT id, config FROM agents WHERE id = $1 OR name = $1", opts.agent)
        if row is None:
            print(f"No agent {opts.agent!r}; run with --list.", file=sys.stderr)
            return 1
        cfg = row["config"]
        cfg = json.loads(cfg) if isinstance(cfg, str) else dict(cfg or {})
        allow = [str(t) for t in (cfg.get("tool_allowlist") or [])]
        if opts.revoke:
            allow = [t for t in allow if t not in names]
        else:
            allow += [t for t in names if t not in allow]
        cfg["tool_allowlist"] = allow
        await db.execute("UPDATE agents SET config = $1::jsonb WHERE id = $2", json.dumps(cfg), row["id"],
                         caller="grant_workstation_tools")
        await sync_tool_grants(row["id"], allow)
        print(f"{row['id']}: tool_allowlist = {allow}")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
