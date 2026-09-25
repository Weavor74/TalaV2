"""
sdk/agent/orchestrator_cli.py
=============================
Orchestrator CLI status tool for Fleet Engineering.
Monitors, pauses, resumes, and terminates active background worktrees
and port allocations in real-time.

Docker has been removed entirely.  Execution isolation is handled at the
process/directory level via git worktrees only.
"""

from __future__ import annotations

import sys
import os
import asyncio
import logging
from typing import Any

# Ensure parent directory is on PATH if running directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from python.harness import db
from sdk.agent.port_router import PortRouter
from sdk.agent.async_shell import AsyncShellExecutor

log = logging.getLogger(__name__)

async def get_active_tasks() -> list[dict[str, Any]]:
    """Retrieves all tasks that are currently active or pending in the database."""
    try:
        db.get_pool()
    except RuntimeError:
        return []
        
    query = """
        SELECT id, description, status, worktree_path, branch_name 
        FROM tasks 
        WHERE status IN ('pending', 'in_progress', 'running', 'paused')
        ORDER BY created_at DESC
    """
    try:
        rows = await db.fetch(query)
        return [dict(r) for r in rows]
    except Exception:
        return []

async def get_port_allocations() -> dict[str, int]:
    """Retrieves all active port allocations from the database."""
    try:
        db.get_pool()
    except RuntimeError:
        return {}
        
    try:
        rows = await db.fetch("SELECT task_id, port FROM port_allocations")
        return {r["task_id"]: r["port"] for r in rows}
    except Exception:
        return {}

async def _show_status() -> None:
    """Displays a unified terminal dashboard of all active workspaces and ports."""
    tasks = await get_active_tasks()
    ports = await get_port_allocations()

    print("=" * 80)
    print("                    TALA COGNITIVE ORCHESTRATOR DASHBOARD")
    print("=" * 80)
    
    print("\n[ACTIVE WORKTREES & TASKS]")
    if not tasks:
        print("  No active tasks found in database.")
    else:
        print(f"  {'TASK ID':<25} | {'STATUS':<12} | {'PORT':<6} | {'BRANCH / PATH':<40}")
        print("  " + "-" * 76)
        for t in tasks:
            tid = t["id"]
            status = t["status"]
            port = str(ports.get(tid, "None"))
            branch = t["branch_name"] or "None"
            print(f"  {tid[:23]:<25} | {status:<12} | {port:<6} | {branch:<40}")
            if t["worktree_path"]:
                print(f"    Folder: {t['worktree_path']}")

    print("\n[PORT ALLOCATIONS]")
    if not ports:
        print("  No active port allocations.")
    else:
        for tid, port in ports.items():
            print(f"  Task {tid[:23]}: port {port}")
    print("=" * 80)

async def pause_task(task_id: str) -> None:
    """Pauses a task by updating its DB status (no container to pause)."""
    try:
        await db.execute("UPDATE tasks SET status = 'paused' WHERE id = $1", task_id)
        print(f"Paused task: {task_id}")
    except Exception as exc:
        print(f"Failed to pause task {task_id}: {exc}")

async def resume_task(task_id: str) -> None:
    """Resumes a paused task by updating its DB status."""
    try:
        await db.execute("UPDATE tasks SET status = 'in_progress' WHERE id = $1", task_id)
        print(f"Resumed task: {task_id}")
    except Exception as exc:
        print(f"Failed to resume task {task_id}: {exc}")

async def terminate_task(task_id: str) -> None:
    """Terminates a task: releases its port and removes its git worktree."""
    # 1. Release port
    router = PortRouter()
    await router.release_port(task_id)

    # 2. Clean up git worktree
    try:
        row = await db.fetchrow("SELECT worktree_path FROM tasks WHERE id = $1", task_id)
        if row and row["worktree_path"]:
            wt_path = row["worktree_path"]
            await AsyncShellExecutor.run_exec("git", ["worktree", "remove", "--force", wt_path],
                                              cwd=os.getcwd())
            print(f"Removed Git Worktree folder: {wt_path}")
    except Exception as exc:
        print(f"Failed to remove Git Worktree folder: {exc}")

    # 3. Update DB status
    try:
        await db.execute("UPDATE tasks SET status = 'failed' WHERE id = $1", task_id)
        print(f"Terminated task and set status to failed for: {task_id}")
    except Exception:
        pass

async def main():
    if len(sys.argv) < 2:
        await _show_status()
        return

    cmd = sys.argv[1].lower()
    
    # Initialize DB pool
    try:
        await db.init_pool()
    except Exception:
        pass

    if cmd == "status":
        await _show_status()
    elif cmd == "pause" and len(sys.argv) >= 3:
        await pause_task(sys.argv[2])
    elif cmd == "resume" and len(sys.argv) >= 3:
        await resume_task(sys.argv[2])
    elif cmd == "terminate" and len(sys.argv) >= 3:
        await terminate_task(sys.argv[2])
    else:
        print("Usage: python -m sdk.agent.orchestrator_cli [status|pause|resume|terminate] [task_id]")
        
    try:
        await db.close_pool()
    except Exception:
        pass

if __name__ == "__main__":
    asyncio.run(main())
