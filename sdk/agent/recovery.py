"""
sdk/agent/recovery.py
====================
Rollback Recovery & Completion Reporting — Tala Cognitive Engine Room
Milestone 7 | Tasks 7.2, 7.3

Implements:
  • RollbackRecovery   — Creates git-based or snapshot checkpoints to revert file edits.
  • CompletionReporter — Replays events to compile structured markdown completion summaries.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from sdk.agent.persistence import project_task_state

log = logging.getLogger(__name__)

SNAPSHOT_IGNORE_NAMES = {
    ".tala_snapshots",
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "out",
}


class RollbackRecovery:
    """
    Creates and restores directory checkpoints to recover workspaces from execution failures.
    """

    def __init__(self, root_dir: str) -> None:
        self.root_dir = root_dir

    def create_checkpoint(self, checkpoint_name: str = "checkpoint") -> str:
        success = self.create_snapshot(self.root_dir, checkpoint_name)
        if success:
            return checkpoint_name
        return "failed"

    def restore_checkpoint(self, checkpoint_name: str = "checkpoint") -> bool:
        return self.restore_snapshot(self.root_dir, checkpoint_name)

    @staticmethod
    def create_snapshot(cwd: str, checkpoint_name: str) -> bool:
        """
        Creates a directory snapshot by copying files to a .tala_snapshots directory.
        """
        import shutil
        try:
            snapshot_dir = os.path.join(cwd, ".tala_snapshots", checkpoint_name)
            if os.path.exists(snapshot_dir):
                shutil.rmtree(snapshot_dir)
            
            # Copy workspace files while excluding generated/dependency/cache folders.
            def ignore_dirs(dir_path, contents):
                return [c for c in contents if c in SNAPSHOT_IGNORE_NAMES]
            
            shutil.copytree(cwd, snapshot_dir, ignore=ignore_dirs)
            log.info("RollbackRecovery: created directory snapshot for %s", checkpoint_name)
            return True
        except Exception as exc:
            log.error("RollbackRecovery: snapshot failed: %s", exc)
            return False

    @staticmethod
    def restore_snapshot(cwd: str, checkpoint_name: str) -> bool:
        """
        Restores files from the .tala_snapshots directory.
        """
        import shutil
        try:
            snapshot_dir = os.path.join(cwd, ".tala_snapshots", checkpoint_name)
            if not os.path.exists(snapshot_dir):
                log.warning("RollbackRecovery: snapshot %s not found.", checkpoint_name)
                return False

            # Delete current contents except generated/dependency/cache folders.
            for item in os.listdir(cwd):
                if item in SNAPSHOT_IGNORE_NAMES:
                    continue
                item_path = os.path.join(cwd, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)

            # Copy snapshot contents back
            for item in os.listdir(snapshot_dir):
                src_path = os.path.join(snapshot_dir, item)
                dst_path = os.path.join(cwd, item)
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path)
                else:
                    shutil.copy2(src_path, dst_path)

            log.info("RollbackRecovery: restored directory snapshot for %s", checkpoint_name)
            return True
        except Exception as exc:
            log.error("RollbackRecovery: snapshot restore failed: %s", exc)
            return False


import re


class CompletionReporter:
    """
    Synthesizes historical event lists into final execution summary reports.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id

    async def generate_report(self) -> str:
        from sdk.agent.persistence import TaskEventStore
        events = await TaskEventStore.fetch_events(self.task_id)
        return self.generate_completion_report(self.task_id, events)

    @staticmethod
    def generate_completion_report(task_id: str, events: list[dict[str, Any]]) -> str:
        """
        Produce a markdown summary report of the run.
        """
        state = project_task_state(events)
        
        telemetry_metrics = []
        for event in events:
            if event["event_type"] == "telemetry":
                payload = event["payload"]
                telemetry_metrics.append(f"- **{payload.get('metric_name')}**: {payload.get('metric_value')}")

        facts_block = "".join(f"  - {f}\n" for f in state.get("facts", []))
        anomalies_block = "".join(f"  - {a}\n" for a in state.get("anomalies", []))
        steps_block = "".join(f"  - {s}\n" for s in state.get("steps_executed", []))

        return f"""# Tala Task Execution Report: Task {task_id[:8]}

## Run Profile
* **Task ID:** {state.get('task_id')}
* **Goal Description:** {state.get('description')}
* **Final Status:** {state.get('status').upper()}
* **Total Iterations Run:** {state.get('iterations')}
* **Completed Timestamp:** {state.get('completed_at', 'N/A')}
* **Failed Reason:** {state.get('failed_reason', 'N/A')}

## Chronological Steps Run
{steps_block or "  *None recorded.*"}
## Extracted Facts
{facts_block or "  *None recorded.*"}
## Anomalies/Errors Detected
{anomalies_block or "  *None recorded.*"}
## Telemetry Summary
{chr(10).join(telemetry_metrics) or "  *No metrics logged.*"}
"""
