"""
sdk/agent/orchestrator_cli.py
=============================
Orchestrator CLI status tool for Fleet Engineering.
Monitors, pauses, resumes, and terminates active background worktrees, port allocations,
and Docker sandboxes in real-time.
"""

from __future__ import annotations

import sys
import os
import json
import asyncio
import logging
from typing import Any

# Ensure parent directory is on PATH if running directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from python.harness import db
from sdk.agent.sandbox import DockerSandbox
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

async def show_status() -> None:
    """Displays a unified terminal dashboard of all active workspaces, sandboxes, and ports."""
    tasks = await get_active_tasks()
    ports = await get_port_allocations()
    sandbox = DockerSandbox()
    docker_ok = await sandbox.check_docker_available()

    print("=" * 80)
    print("                    TALA COGNITIVE ORCHESTRATOR DASHBOARD")
    print("=" * 80)
    
    # 1. Active Tasks and Worktrees
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

    # 2. Docker Sandboxes
    print("\n[DOCKER SANDBOX STATUS]")
    if not docker_ok:
        print("  Docker daemon is not available/running on host.")
    else:
        # Run docker ps to inspect running tala containers
        res = await AsyncShellExecutor.run_exec("docker", ["ps", "--filter", "name=tala-sandbox-", "--format", "{{.Names}} | {{.Status}}"])
        lines = res["stdout"].strip().split("\n")
        lines = [l.strip() for l in lines if l.strip()]
        if not lines:
            print("  No active tala-sandbox containers running.")
        else:
            for l in lines:
                print(f"  Container: {l}")
    print("=" * 80)

async def pause_task(task_id: str) -> None:
    sandbox = DockerSandbox()
    success = await sandbox.pause_container(task_id)
    if success:
        try:
            await db.execute("UPDATE tasks SET status = 'paused' WHERE id = $1", task_id)
            print(f"Successfully paused sandbox and task state for: {task_id}")
        except Exception:
            print(f"Paused container, but failed to update task state in DB for: {task_id}")
    else:
        print(f"Failed to pause Docker sandbox container for task: {task_id}")

async def resume_task(task_id: str) -> None:
    sandbox = DockerSandbox()
    success = await sandbox.resume_container(task_id)
    if success:
        try:
            await db.execute("UPDATE tasks SET status = 'in_progress' WHERE id = $1", task_id)
            print(f"Successfully resumed sandbox and task state for: {task_id}")
        except Exception:
            print(f"Resumed container, but failed to update task state in DB for: {task_id}")
    else:
        print(f"Failed to resume Docker sandbox container for task: {task_id}")

async def terminate_task(task_id: str) -> None:
    # 1. Stop sandbox
    sandbox = DockerSandbox()
    await sandbox.stop_container(task_id)

    # 2. Release port
    router = PortRouter()
    await router.release_port(task_id)

    # 3. Clean up git worktree
    try:
        # Check path from DB
        row = await db.fetchrow("SELECT worktree_path FROM tasks WHERE id = $1", task_id)
        if row and row["worktree_path"]:
            wt_path = row["worktree_path"]
            # Prune git worktree
            await AsyncShellExecutor.run_exec("git", ["worktree", "remove", "--force", wt_path])
            print(f"Removed Git Worktree folder: {wt_path}")
    except Exception as exc:
        print(f"Failed to remove Git Worktree folder: {exc}")

    # 4. Update DB status
    try:
        await db.execute("UPDATE tasks SET status = 'failed' WHERE id = $1", task_id)
        print(f"Terminated task and set status to failed for: {task_id}")
    except Exception:
        pass

async def main():
    if len(sys.argv) < 2:
        # Default to showing status
        await show_status()
        return

    cmd = sys.argv[1].lower()
    
    # Initialize DB pool
    try:
        await db.init_pool()
    except Exception:
        pass

    if cmd == "status":
        await show_status()
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
