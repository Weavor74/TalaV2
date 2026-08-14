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
import sys
from typing import Any

from sdk.agent.persistence import project_task_state

log = logging.getLogger(__name__)


def _force_rmtree(path: str) -> bool:
    """
    Remove a tree, clearing the read-only bit on anything that blocks it.

    shutil.rmtree(ignore_errors=True) is a trap on Windows: a read-only file
    raises PermissionError, the flag swallows it, and the *directory survives*
    while the caller believes it is gone. The corpus pipeline scripts are
    deliberately chmod'd read-only, so their copies inside the snapshot
    staging directory made every later rmtree a no-op -- and the copytree
    that followed then died with WinError 183 ("cannot create a file when
    that file already exists"). Every checkpoint from 2026-08-12 19:02
    onward failed that way, so no goal phase has had rollback protection
    since.

    Returns True if the path is gone afterwards.
    """
    import shutil
    import stat

    def _on_error(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except Exception:
            pass

    if not os.path.exists(path):
        return True
    try:
        # onexc replaced onerror in 3.12; keep working on both.
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=lambda f, t, e: _on_error(f, t, e))
        else:
            shutil.rmtree(path, onerror=_on_error)
    except Exception as exc:
        log.warning("RollbackRecovery: could not remove %s: %s", path, exc)
    return not os.path.exists(path)


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

    def create_checkpoint(self, checkpoint_name: str = "checkpoint") -> str | None:
        """
        Return the checkpoint name, or None if no checkpoint was created.

        This used to return the string "failed" on failure. That is a truthy
        value the caller stored as a checkpoint *id*, so a failed snapshot
        was logged as "created checkpoint failed", and the rollback path
        later "restored" a checkpoint named "failed" -- printing a reassuring
        "Rolled back phase ... to checkpoint failed" while restoring nothing.
        None makes the absence of protection unmistakable to the caller.
        """
        success = self.create_snapshot(self.root_dir, checkpoint_name)
        return checkpoint_name if success else None

    def restore_checkpoint(self, checkpoint_name: str = "checkpoint") -> bool:
        return self.restore_snapshot(self.root_dir, checkpoint_name)

    @staticmethod
    def create_snapshot(cwd: str, checkpoint_name: str) -> bool:
        """
        Creates a directory snapshot by copying files to a .tala_snapshots directory.

        The new snapshot is built beside the old one and swapped in only after
        it completes. Previously this rmtree'd the existing snapshot FIRST and
        then copied -- so any failure mid-copy (a locked file, a full disk)
        destroyed the last known-good checkpoint and left a partial one in its
        place, which the restore path would then happily treat as the truth.
        """
        import shutil
        snapshot_root = os.path.join(cwd, ".tala_snapshots")
        snapshot_dir = os.path.join(snapshot_root, checkpoint_name)
        staging_dir = os.path.join(snapshot_root, f".{checkpoint_name}.incoming")
        previous_dir = os.path.join(snapshot_root, f".{checkpoint_name}.previous")
        try:
            def ignore_dirs(dir_path, contents):
                return [c for c in contents if c in SNAPSHOT_IGNORE_NAMES]

            if not _force_rmtree(staging_dir):
                # Bail out loudly rather than letting copytree fail with an
                # opaque WinError 183 on every single run from here on.
                log.error(
                    "RollbackRecovery: stale staging dir %s could not be removed; "
                    "no checkpoint will be created until it is cleared by hand.",
                    staging_dir,
                )
                return False
            shutil.copytree(cwd, staging_dir, ignore=ignore_dirs)

            # Swap: keep the old snapshot until the new one is in place.
            _force_rmtree(previous_dir)
            if os.path.exists(snapshot_dir):
                os.rename(snapshot_dir, previous_dir)
            os.rename(staging_dir, snapshot_dir)
            _force_rmtree(previous_dir)

            log.info("RollbackRecovery: created directory snapshot for %s", checkpoint_name)
            return True
        except Exception as exc:
            log.error("RollbackRecovery: snapshot failed: %s", exc)
            _force_rmtree(staging_dir)
            # If the swap died between the rename steps, put the old one back.
            if not os.path.exists(snapshot_dir) and os.path.exists(previous_dir):
                try:
                    os.rename(previous_dir, snapshot_dir)
                except Exception:
                    pass
            return False

    @staticmethod
    def restore_snapshot(cwd: str, checkpoint_name: str) -> bool:
        """
        Restore files from a .tala_snapshots checkpoint by copying the
        snapshot OVER the workspace.

        This must never delete the workspace first. It used to: the old
        implementation walked the repo root and rmtree'd/removed every entry
        not on the ignore list, then copied the snapshot back. That made an
        automatic, unattended rollback the single most destructive operation
        in the system, and on 2026-08-11 it destroyed this repo for real --
        an overnight goal phase failed, rollback fired, the delete loop got
        partway through the alphabet (…nodes, package.json, python, renderer)
        and then died, leaving 427 of 706 tracked files gone and nothing
        restored. Only the git history made it recoverable.

        Copy-over is strictly weaker: files CREATED since the checkpoint
        survive the rollback rather than being removed. That is the correct
        trade. A rollback that leaves an extra file behind is a nuisance; a
        rollback that deletes the user's working tree is a catastrophe, and
        no automatic recovery path should be able to cause one.
        """
        import shutil
        try:
            snapshot_dir = os.path.join(cwd, ".tala_snapshots", checkpoint_name)
            if not os.path.isdir(snapshot_dir):
                log.warning("RollbackRecovery: snapshot %s not found.", checkpoint_name)
                return False

            entries = [e for e in os.listdir(snapshot_dir) if e not in SNAPSHOT_IGNORE_NAMES]
            if not entries:
                log.error(
                    "RollbackRecovery: refusing to restore from empty snapshot %s.",
                    checkpoint_name,
                )
                return False

            restored = 0
            for item in entries:
                src_path = os.path.join(snapshot_dir, item)
                dst_path = os.path.join(cwd, item)
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dst_path) or cwd, exist_ok=True)
                    shutil.copy2(src_path, dst_path)
                restored += 1

            log.info(
                "RollbackRecovery: restored %d top-level entr%s from snapshot %s "
                "(copy-over; files created since the checkpoint were left in place).",
                restored, "y" if restored == 1 else "ies", checkpoint_name,
            )
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
