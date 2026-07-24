"""
heartbeat_scheduler.py — Tala Cognitive Engine Room: Proactive Heartbeat Loop
==============================================================================
Implements a **time-based autonomous agent loop** that fires configured goals
on a cron schedule without waiting for a user message.

Architecture
------------
- CronExpression  — standard 5-field cron parser + matcher (pure Python, zero deps)
- HeartbeatJob    — dataclass representing one scheduled goal
- HeartbeatScheduler — singleton that:
    1. Loads jobs from the `heartbeats` PostgreSQL table on startup
    2. Runs a 60-second asyncio tick loop
    3. On each tick, checks which jobs are due via CronExpression.matches_now()
    4. Fires due jobs through GoalEngine.run_goal()
    5. Pushes JSON-RPC 2.0 notifications to all connected WS clients
    6. Persists last_run_at + last_status to the DB after each execution

Design invariants
-----------------
- All durable state (job records, run history) flows through PostgreSQL.
- Heartbeats fire even when no UI client is connected; notifications are
  delivered only to live connections and the result is always logged to DB.
- The scheduler uses asyncio.sleep — it lives entirely within the harness's
  single event loop and never touches the ThreadPoolExecutor.
- Each goal runs in its own asyncio.Task so a slow or hanging goal does not
  block subsequent ticks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Coroutine

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cron expression parser (5-field: minute hour dom month dow)
# ---------------------------------------------------------------------------

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


def _parse_field(raw: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    """Parse a single cron field into a set of matching integers.

    Supports:
      *           — all values in [lo, hi]
      n           — exact value
      n-m         — inclusive range
      */step      — every step from lo
      n-m/step    — every step within range
      a,b,c       — comma-separated list of any of the above
      name        — named values (month / weekday)
    """
    result: set[int] = set()

    # Resolve named values (case-insensitive)
    if names:
        for name, val in names.items():
            raw = raw.lower().replace(name, str(val))

    for part in raw.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)

        if part == "*":
            result.update(range(lo, hi + 1, step))
        elif "-" in part:
            a, b = part.split("-", 1)
            result.update(range(int(a), int(b) + 1, step))
        else:
            val = int(part)
            if step > 1:
                result.update(range(val, hi + 1, step))
            else:
                result.add(val)

    return result


class CronExpression:
    """Parses and evaluates a 5-field cron expression.

    Also supports:
      @startup  — matches once on harness boot (handled externally)
      @hourly   — '0 * * * *'
      @daily    — '0 0 * * *'
      @weekly   — '0 0 * * 0'
      @monthly  — '0 0 1 * *'
    """

    _SHORTHANDS: dict[str, str] = {
        "@hourly":  "0 * * * *",
        "@daily":   "0 0 * * *",
        "@weekly":  "0 0 * * 0",
        "@monthly": "0 0 1 * *",
        "@midnight":"0 0 * * *",
    }

    def __init__(self, expr: str) -> None:
        self.raw = expr.strip()
        self.is_startup = self.raw == "@startup"

        if self.is_startup:
            self._minutes = self._hours = self._doms = self._months = self._dows = set()
            return

        expanded = self._SHORTHANDS.get(self.raw.lower(), self.raw)
        parts = expanded.split()
        if len(parts) != 5:
            raise ValueError(
                f"Invalid cron expression {self.raw!r}: expected 5 fields, got {len(parts)}"
            )
        self._minutes = _parse_field(parts[0], 0, 59)
        self._hours   = _parse_field(parts[1], 0, 23)
        self._doms    = _parse_field(parts[2], 1, 31)
        self._months  = _parse_field(parts[3], 1, 12, _MONTH_NAMES)
        self._dows    = _parse_field(parts[4], 0, 6, _DOW_NAMES)

    def matches(self, dt: datetime) -> bool:
        """Return True if *dt* satisfies this cron expression."""
        if self.is_startup:
            return False
        return (
            dt.minute  in self._minutes
            and dt.hour   in self._hours
            and dt.day    in self._doms
            and dt.month  in self._months
            and dt.weekday() in {(d - 1) % 7 for d in self._dows}  # cron Sun=0, Python Mon=0
        )

    def __repr__(self) -> str:
        return f"CronExpression({self.raw!r})"


# ---------------------------------------------------------------------------
# HeartbeatJob dataclass
# ---------------------------------------------------------------------------

@dataclass
class HeartbeatJob:
    """Represents one scheduled heartbeat goal loaded from the DB."""

    id: str
    name: str
    cron_expr: str
    goal_text: str
    enabled: bool = True
    last_run_at: datetime | None = None
    last_status: str = "pending"  # pending | running | success | failed

    # Parsed expression — populated lazily
    _parsed: CronExpression = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._parsed = CronExpression(self.cron_expr)

    @property
    def is_startup(self) -> bool:
        return self._parsed.is_startup

    def is_due(self, now: datetime) -> bool:
        """Return True if the job should fire at *now*."""
        if not self.enabled:
            return False
        return self._parsed.matches(now)


# ---------------------------------------------------------------------------
# Notification broadcast helper (injected from server.py)
# ---------------------------------------------------------------------------

# Module-level broadcast function reference.  server.py sets this during
# startup so the scheduler can push to all connected WS clients without
# creating a circular import.
_broadcast_fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]] | None = None


def register_broadcast(fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
    """Register the server-level broadcast coroutine factory."""
    global _broadcast_fn
    _broadcast_fn = fn


async def _push(method: str, params: dict[str, Any]) -> None:
    """Push a JSON-RPC notification to all connected clients (best-effort)."""
    if _broadcast_fn is not None:
        try:
            await _broadcast_fn(method, params)
        except Exception as exc:  # noqa: BLE001
            log.warning("Heartbeat broadcast failed: %s", exc)


# ---------------------------------------------------------------------------
# HeartbeatScheduler — singleton
# ---------------------------------------------------------------------------

class HeartbeatScheduler:
    """Singleton asyncio-native cron scheduler.

    Usage
    -----
    From server startup::

        await HeartbeatScheduler.get().start()

    From server shutdown::

        await HeartbeatScheduler.get().stop()

    From RPC handlers::

        scheduler = HeartbeatScheduler.get()
        await scheduler.reload_from_db()
    """

    _instance: HeartbeatScheduler | None = None

    @classmethod
    def get(cls) -> HeartbeatScheduler:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._jobs: list[HeartbeatJob] = []
        self._running = False
        self._tick_task: asyncio.Task | None = None
        self._running_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load jobs from DB, fire @startup jobs, then begin the tick loop."""
        if self._running:
            log.warning("HeartbeatScheduler.start() called but already running — ignored")
            return

        await self.reload_from_db()
        self._running = True

        # Fire @startup jobs immediately
        startup_jobs = [j for j in self._jobs if j.is_startup and j.enabled]
        if startup_jobs:
            log.info("HeartbeatScheduler: firing %d @startup job(s)", len(startup_jobs))
            for job in startup_jobs:
                self._spawn_job(job)

        # Start the tick loop
        self._tick_task = asyncio.create_task(self._tick_loop(), name="heartbeat-tick")
        log.info(
            "HeartbeatScheduler started — %d job(s) loaded, tick loop running",
            len(self._jobs)
        )

    async def stop(self) -> None:
        """Stop the tick loop and wait for any running jobs to finish."""
        self._running = False

        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None

        # Wait up to 10 s for any in-flight goal tasks
        if self._running_tasks:
            log.info(
                "HeartbeatScheduler: waiting for %d in-flight job(s) …",
                len(self._running_tasks)
            )
            done, pending = await asyncio.wait(self._running_tasks, timeout=10.0)
            for task in pending:
                task.cancel()

        log.info("HeartbeatScheduler stopped")

    # ------------------------------------------------------------------
    # DB persistence
    # ------------------------------------------------------------------

    async def reload_from_db(self) -> None:
        """Load (or reload) all heartbeat jobs from PostgreSQL."""
        try:
            from python.harness import db
            rows = await db.fetch(
                "SELECT id, name, cron_expr, goal_text, enabled, last_run_at, last_status "
                "FROM heartbeats ORDER BY created_at ASC"
            )
            jobs = []
            for r in rows:
                try:
                    job = HeartbeatJob(
                        id=r["id"],
                        name=r["name"],
                        cron_expr=r["cron_expr"],
                        goal_text=r["goal_text"],
                        enabled=bool(r["enabled"]),
                        last_run_at=r["last_run_at"],
                        last_status=r["last_status"] or "pending",
                    )
                    jobs.append(job)
                except Exception as exc:
                    log.warning(
                        "HeartbeatScheduler: skipping invalid job id=%s: %s",
                        r["id"], exc
                    )
            self._jobs = jobs
            log.info("HeartbeatScheduler: loaded %d job(s) from DB", len(self._jobs))
        except Exception as exc:
            log.error("HeartbeatScheduler: failed to load jobs from DB: %s", exc)

    async def _persist_run_start(self, job: HeartbeatJob) -> None:
        """Mark job as running in DB."""
        try:
            from python.harness import db
            await db.execute(
                "UPDATE heartbeats SET last_run_at = NOW(), last_status = 'running', "
                "updated_at = NOW() WHERE id = $1",
                job.id,
            )
        except Exception as exc:
            log.warning("HeartbeatScheduler: failed to persist run_start for %s: %s", job.id, exc)

    async def _persist_run_result(self, job: HeartbeatJob, status: str) -> None:
        """Persist final run status to DB."""
        try:
            from python.harness import db
            await db.execute(
                "UPDATE heartbeats SET last_status = $1, updated_at = NOW() WHERE id = $2",
                status,
                job.id,
            )
        except Exception as exc:
            log.warning("HeartbeatScheduler: failed to persist run_result for %s: %s", job.id, exc)

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        """Run every 60 seconds; fire any jobs whose cron expression is due."""
        log.debug("HeartbeatScheduler: tick loop started")
        while self._running:
            try:
                await self._evaluate_tick()
            except Exception as exc:  # noqa: BLE001
                log.error("HeartbeatScheduler: tick evaluation error: %s", exc)

            # Sleep until the start of the next minute
            now = datetime.now(UTC)
            seconds_until_next_minute = 60 - now.second
            try:
                await asyncio.sleep(seconds_until_next_minute)
            except asyncio.CancelledError:
                break

        log.debug("HeartbeatScheduler: tick loop exited")

    async def _evaluate_tick(self) -> None:
        """Check which jobs are due and spawn them."""
        try:
            from sdk.agent.swarm_engine import ZombieReaper
            count = await ZombieReaper.reclaim_zombies(timeout_minutes=30)
            if count > 0:
                log.info("HeartbeatScheduler: Reclaimed %d zombie task(s).", count)
        except Exception as exc:
            log.error("HeartbeatScheduler: failed to run zombie reaper: %s", exc)

        now = datetime.now(UTC)
        for job in self._jobs:
            if job.is_startup:
                continue  # @startup jobs are only fired in start()
            if not job.is_due(now):
                continue

            log.info(
                "HeartbeatScheduler: job %r (id=%s) is due — spawning",
                job.name, job.id
            )
            self._spawn_job(job)

    def _spawn_job(self, job: HeartbeatJob) -> None:
        """Launch a job as a background asyncio Task."""
        task = asyncio.create_task(
            self._run_job(job),
            name=f"heartbeat-job-{job.id[:8]}",
        )
        self._running_tasks.add(task)
        task.add_done_callback(self._running_tasks.discard)

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    async def _run_job(self, job: HeartbeatJob) -> None:
        """Execute a heartbeat job through GoalEngine and push WS notifications."""
        log.info("HeartbeatScheduler: running job %r (id=%s) goal=%r", job.name, job.id, job.goal_text[:80])

        # Mark in-memory as running
        job.last_status = "running"
        job.last_run_at = datetime.now(UTC)

        await self._persist_run_start(job)

        # Push fired notification
        await _push("heartbeat.fired", {
            "id": job.id,
            "name": job.name,
            "goal": job.goal_text,
            "fired_at": job.last_run_at.isoformat(),
        })

        # Log to agent_log
        try:
            from python.harness import db
            await db.log_to_db(
                "INFO",
                f"Heartbeat fired: {job.name}",
                {"heartbeat_id": job.id, "goal": job.goal_text},
            )
        except Exception:
            pass

        # Run through GoalEngine
        status = "success"
        result: dict[str, Any] = {}
        try:
            from sdk.agent.goal_engine import GoalEngine
            engine = GoalEngine()
            result = await engine.run_goal(job.goal_text)
            if not result.get("success", False):
                status = "failed"
                log.warning(
                    "HeartbeatScheduler: job %r failed — %s",
                    job.name, result.get("error", "unknown")
                )
            else:
                log.info("HeartbeatScheduler: job %r completed successfully", job.name)
        except Exception as exc:
            status = "failed"
            result = {"success": False, "error": str(exc)}
            log.exception("HeartbeatScheduler: job %r raised an exception", job.name)

        # Update in-memory state
        job.last_status = status

        # Persist to DB
        await self._persist_run_result(job, status)

        # Push completion notification
        await _push("heartbeat.complete", {
            "id": job.id,
            "name": job.name,
            "status": status,
            "phases_completed": result.get("phases_completed", []),
            "error": result.get("error"),
            "completed_at": datetime.now(UTC).isoformat(),
        })

        # Log completion
        try:
            from python.harness import db
            await db.log_to_db(
                "INFO" if status == "success" else "WARNING",
                f"Heartbeat complete: {job.name} — {status}",
                {
                    "heartbeat_id": job.id,
                    "status": status,
                    "error": result.get("error"),
                },
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public helpers used by RPC handlers
    # ------------------------------------------------------------------

    def get_jobs(self) -> list[dict[str, Any]]:
        """Return serialisable list of all jobs (used by heartbeat.list handler)."""
        return [
            {
                "id": j.id,
                "name": j.name,
                "cron_expr": j.cron_expr,
                "goal_text": j.goal_text,
                "enabled": j.enabled,
                "last_run_at": j.last_run_at.isoformat() if j.last_run_at else None,
                "last_status": j.last_status,
            }
            for j in self._jobs
        ]

    def get_job(self, job_id: str) -> HeartbeatJob | None:
        """Find a job by id in the in-memory list."""
        for j in self._jobs:
            if j.id == job_id:
                return j
        return None

    async def fire_now(self, job_id: str) -> dict[str, Any]:
        """Manually trigger a job immediately (ignores cron schedule + enabled flag).

        Returns immediately after spawning the background task.
        """
        job = self.get_job(job_id)
        if job is None:
            return {"success": False, "error": f"Job {job_id!r} not found"}
        self._spawn_job(job)
        return {"success": True, "message": f"Job {job.name!r} fired manually"}
