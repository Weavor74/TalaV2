# File: `heartbeat_scheduler.py`

## Module Description

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

## Classes

### `class CronExpression`
Parses and evaluates a 5-field cron expression.

Also supports:
  @startup  — matches once on harness boot (handled externally)
  @hourly   — '0 * * * *'
  @daily    — '0 0 * * *'
  @weekly   — '0 0 * * 0'
  @monthly  — '0 0 1 * *'

- **`def __init__(self, expr)`**
- **`def matches(self, dt)`**
  - Return True if *dt* satisfies this cron expression.
- **`def __repr__(self)`**

### `class HeartbeatJob`
Represents one scheduled heartbeat goal loaded from the DB.

- **`def __post_init__(self)`**
- **`def is_startup(self)`**
- **`def is_due(self, now)`**
  - Return True if the job should fire at *now*.

### `class HeartbeatScheduler`
Singleton asyncio-native cron scheduler.

Usage
-----
From server startup::

    await HeartbeatScheduler.get().start()

From server shutdown::

    await HeartbeatScheduler.get().stop()

From RPC handlers::

    scheduler = HeartbeatScheduler.get()
    await scheduler.reload_from_db()

- **`def get(cls)`**
- **`def __init__(self)`**
- **`def start(self)`**
  - Load jobs from DB, fire @startup jobs, then begin the tick loop.
- **`def stop(self)`**
  - Stop the tick loop and wait for any running jobs to finish.
- **`def reload_from_db(self)`**
  - Load (or reload) all heartbeat jobs from PostgreSQL.
- **`def _persist_run_start(self, job)`**
  - Mark job as running in DB.
- **`def _persist_run_result(self, job, status)`**
  - Persist final run status to DB.
- **`def _tick_loop(self)`**
  - Run every 60 seconds; fire any jobs whose cron expression is due.
- **`def _evaluate_tick(self)`**
  - Check which jobs are due and spawn them.
- **`def _spawn_job(self, job)`**
  - Launch a job as a background asyncio Task.
- **`def _run_job(self, job)`**
  - Execute a heartbeat job through GoalEngine and push WS notifications.
- **`def get_jobs(self)`**
  - Return serialisable list of all jobs (used by heartbeat.list handler).
- **`def get_job(self, job_id)`**
  - Find a job by id in the in-memory list.
- **`def fire_now(self, job_id)`**
  - Manually trigger a job immediately (ignores cron schedule + enabled flag).
  
  Returns immediately after spawning the background task.

## Functions

### `def _parse_field(raw, lo, hi, names)`
Parse a single cron field into a set of matching integers.

Supports:
  *           — all values in [lo, hi]
  n           — exact value
  n-m         — inclusive range
  */step      — every step from lo
  n-m/step    — every step within range
  a,b,c       — comma-separated list of any of the above
  name        — named values (month / weekday)

### `def register_broadcast(fn)`
Register the server-level broadcast coroutine factory.

### `async def _push(method, params)`
Push a JSON-RPC notification to all connected clients (best-effort).

## Source Code

```python
"""\nheartbeat_scheduler.py — Tala Cognitive Engine Room: Proactive Heartbeat Loop\n==============================================================================\nImplements a **time-based autonomous agent loop** that fires configured goals\non a cron schedule without waiting for a user message.\n\nArchitecture\n------------\n- CronExpression  — standard 5-field cron parser + matcher (pure Python, zero deps)\n- HeartbeatJob    — dataclass representing one scheduled goal\n- HeartbeatScheduler — singleton that:\n    1. Loads jobs from the `heartbeats` PostgreSQL table on startup\n    2. Runs a 60-second asyncio tick loop\n    3. On each tick, checks which jobs are due via CronExpression.matches_now()\n    4. Fires due jobs through GoalEngine.run_goal()\n    5. Pushes JSON-RPC 2.0 notifications to all connected WS clients\n    6. Persists last_run_at + last_status to the DB after each execution\n\nDesign invariants\n-----------------\n- All durable state (job records, run history) flows through PostgreSQL.\n- Heartbeats fire even when no UI client is connected; notifications are\n  delivered only to live connections and the result is always logged to DB.\n- The scheduler uses asyncio.sleep — it lives entirely within the harness's\n  single event loop and never touches the ThreadPoolExecutor.\n- Each goal runs in its own asyncio.Task so a slow or hanging goal does not\n  block subsequent ticks.\n"""\n\nfrom __future__ import annotations\n\nimport asyncio\nimport json\nimport logging\nimport uuid\nfrom dataclasses import dataclass, field\nfrom datetime import UTC, datetime\nfrom typing import Any, Callable, Coroutine\n\nlog = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Cron expression parser (5-field: minute hour dom month dow)\n# ---------------------------------------------------------------------------\n\n_MONTH_NAMES = {\n    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,\n    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,\n}\n_DOW_NAMES = {\n    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,\n}\n\n\ndef _parse_field(raw: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:\n    """Parse a single cron field into a set of matching integers.\n\n    Supports:\n      *           — all values in [lo, hi]\n      n           — exact value\n      n-m         — inclusive range\n      */step      — every step from lo\n      n-m/step    — every step within range\n      a,b,c       — comma-separated list of any of the above\n      name        — named values (month / weekday)\n    """\n    result: set[int] = set()\n\n    # Resolve named values (case-insensitive)\n    if names:\n        for name, val in names.items():\n            raw = raw.lower().replace(name, str(val))\n\n    for part in raw.split(","):\n        part = part.strip()\n        step = 1\n        if "/" in part:\n            part, step_str = part.split("/", 1)\n            step = int(step_str)\n\n        if part == "*":\n            result.update(range(lo, hi + 1, step))\n        elif "-" in part:\n            a, b = part.split("-", 1)\n            result.update(range(int(a), int(b) + 1, step))\n        else:\n            val = int(part)\n            if step > 1:\n                result.update(range(val, hi + 1, step))\n            else:\n                result.add(val)\n\n    return result\n\n\nclass CronExpression:\n    """Parses and evaluates a 5-field cron expression.\n\n    Also supports:\n      @startup  — matches once on harness boot (handled externally)\n      @hourly   — '0 * * * *'\n      @daily    — '0 0 * * *'\n      @weekly   — '0 0 * * 0'\n      @monthly  — '0 0 1 * *'\n    """\n\n    _SHORTHANDS: dict[str, str] = {\n        "@hourly":  "0 * * * *",\n        "@daily":   "0 0 * * *",\n        "@weekly":  "0 0 * * 0",\n        "@monthly": "0 0 1 * *",\n        "@midnight":"0 0 * * *",\n    }\n\n    def __init__(self, expr: str) -> None:\n        self.raw = expr.strip()\n        self.is_startup = self.raw == "@startup"\n\n        if self.is_startup:\n            self._minutes = self._hours = self._doms = self._months = self._dows = set()\n            return\n\n        expanded = self._SHORTHANDS.get(self.raw.lower(), self.raw)\n        parts = expanded.split()\n        if len(parts) != 5:\n            raise ValueError(\n                f"Invalid cron expression {self.raw!r}: expected 5 fields, got {len(parts)}"\n            )\n        self._minutes = _parse_field(parts[0], 0, 59)\n        self._hours   = _parse_field(parts[1], 0, 23)\n        self._doms    = _parse_field(parts[2], 1, 31)\n        self._months  = _parse_field(parts[3], 1, 12, _MONTH_NAMES)\n        self._dows    = _parse_field(parts[4], 0, 6, _DOW_NAMES)\n\n    def matches(self, dt: datetime) -> bool:\n        """Return True if *dt* satisfies this cron expression."""\n        if self.is_startup:\n            return False\n        return (\n            dt.minute  in self._minutes\n            and dt.hour   in self._hours\n            and dt.day    in self._doms\n            and dt.month  in self._months\n            and dt.weekday() in {(d - 1) % 7 for d in self._dows}  # cron Sun=0, Python Mon=0\n        )\n\n    def __repr__(self) -> str:\n        return f"CronExpression({self.raw!r})"\n\n\n# ---------------------------------------------------------------------------\n# HeartbeatJob dataclass\n# ---------------------------------------------------------------------------\n\n@dataclass\nclass HeartbeatJob:\n    """Represents one scheduled heartbeat goal loaded from the DB."""\n\n    id: str\n    name: str\n    cron_expr: str\n    goal_text: str\n    enabled: bool = True\n    last_run_at: datetime | None = None\n    last_status: str = "pending"  # pending | running | success | failed\n\n    # Parsed expression — populated lazily\n    _parsed: CronExpression = field(init=False, repr=False)\n\n    def __post_init__(self) -> None:\n        self._parsed = CronExpression(self.cron_expr)\n\n    @property\n    def is_startup(self) -> bool:\n        return self._parsed.is_startup\n\n    def is_due(self, now: datetime) -> bool:\n        """Return True if the job should fire at *now*."""\n        if not self.enabled:\n            return False\n        return self._parsed.matches(now)\n\n\n# ---------------------------------------------------------------------------\n# Notification broadcast helper (injected from server.py)\n# ---------------------------------------------------------------------------\n\n# Module-level broadcast function reference.  server.py sets this during\n# startup so the scheduler can push to all connected WS clients without\n# creating a circular import.\n_broadcast_fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]] | None = None\n\n\ndef register_broadcast(fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]) -> None:\n    """Register the server-level broadcast coroutine factory."""\n    global _broadcast_fn\n    _broadcast_fn = fn\n\n\nasync def _push(method: str, params: dict[str, Any]) -> None:\n    """Push a JSON-RPC notification to all connected clients (best-effort)."""\n    if _broadcast_fn is not None:\n        try:\n            await _broadcast_fn(method, params)\n        except Exception as exc:  # noqa: BLE001\n            log.warning("Heartbeat broadcast failed: %s", exc)\n\n\n# ---------------------------------------------------------------------------\n# HeartbeatScheduler — singleton\n# ---------------------------------------------------------------------------\n\nclass HeartbeatScheduler:\n    """Singleton asyncio-native cron scheduler.\n\n    Usage\n    -----\n    From server startup::\n\n        await HeartbeatScheduler.get().start()\n\n    From server shutdown::\n\n        await HeartbeatScheduler.get().stop()\n\n    From RPC handlers::\n\n        scheduler = HeartbeatScheduler.get()\n        await scheduler.reload_from_db()\n    """\n\n    _instance: HeartbeatScheduler | None = None\n\n    @classmethod\n    def get(cls) -> HeartbeatScheduler:\n        if cls._instance is None:\n            cls._instance = cls()\n        return cls._instance\n\n    def __init__(self) -> None:\n        self._jobs: list[HeartbeatJob] = []\n        self._running = False\n        self._tick_task: asyncio.Task | None = None\n        self._running_tasks: set[asyncio.Task] = set()\n\n    # ------------------------------------------------------------------\n    # Lifecycle\n    # ------------------------------------------------------------------\n\n    async def start(self) -> None:\n        """Load jobs from DB, fire @startup jobs, then begin the tick loop."""\n        if self._running:\n            log.warning("HeartbeatScheduler.start() called but already running — ignored")\n            return\n\n        await self.reload_from_db()\n        self._running = True\n\n        # Fire @startup jobs immediately\n        startup_jobs = [j for j in self._jobs if j.is_startup and j.enabled]\n        if startup_jobs:\n            log.info("HeartbeatScheduler: firing %d @startup job(s)", len(startup_jobs))\n            for job in startup_jobs:\n                self._spawn_job(job)\n\n        # Start the tick loop\n        self._tick_task = asyncio.create_task(self._tick_loop(), name="heartbeat-tick")\n        log.info(\n            "HeartbeatScheduler started — %d job(s) loaded, tick loop running",\n            len(self._jobs)\n        )\n\n    async def stop(self) -> None:\n        """Stop the tick loop and wait for any running jobs to finish."""\n        self._running = False\n\n        if self._tick_task is not None:\n            self._tick_task.cancel()\n            try:\n                await self._tick_task\n            except asyncio.CancelledError:\n                pass\n            self._tick_task = None\n\n        # Wait up to 10 s for any in-flight goal tasks\n        if self._running_tasks:\n            log.info(\n                "HeartbeatScheduler: waiting for %d in-flight job(s) …",\n                len(self._running_tasks)\n            )\n            done, pending = await asyncio.wait(self._running_tasks, timeout=10.0)\n            for task in pending:\n                task.cancel()\n\n        log.info("HeartbeatScheduler stopped")\n\n    # ------------------------------------------------------------------\n    # DB persistence\n    # ------------------------------------------------------------------\n\n    async def reload_from_db(self) -> None:\n        """Load (or reload) all heartbeat jobs from PostgreSQL."""\n        try:\n            from python.harness import db\n            rows = await db.fetch(\n                "SELECT id, name, cron_expr, goal_text, enabled, last_run_at, last_status "\n                "FROM heartbeats ORDER BY created_at ASC"\n            )\n            jobs = []\n            for r in rows:\n                try:\n                    job = HeartbeatJob(\n                        id=r["id"],\n                        name=r["name"],\n                        cron_expr=r["cron_expr"],\n                        goal_text=r["goal_text"],\n                        enabled=bool(r["enabled"]),\n                        last_run_at=r["last_run_at"],\n                        last_status=r["last_status"] or "pending",\n                    )\n                    jobs.append(job)\n                except Exception as exc:\n                    log.warning(\n                        "HeartbeatScheduler: skipping invalid job id=%s: %s",\n                        r["id"], exc\n                    )\n            self._jobs = jobs\n            log.info("HeartbeatScheduler: loaded %d job(s) from DB", len(self._jobs))\n        except Exception as exc:\n            log.error("HeartbeatScheduler: failed to load jobs from DB: %s", exc)\n\n    async def _persist_run_start(self, job: HeartbeatJob) -> None:\n        """Mark job as running in DB."""\n        try:\n            from python.harness import db\n            await db.execute(\n                "UPDATE heartbeats SET last_run_at = NOW(), last_status = 'running', "\n                "updated_at = NOW() WHERE id = $1",\n                job.id,\n            )\n        except Exception as exc:\n            log.warning("HeartbeatScheduler: failed to persist run_start for %s: %s", job.id, exc)\n\n    async def _persist_run_result(self, job: HeartbeatJob, status: str) -> None:\n        """Persist final run status to DB."""\n        try:\n            from python.harness import db\n            await db.execute(\n                "UPDATE heartbeats SET last_status = $1, updated_at = NOW() WHERE id = $2",\n                status,\n                job.id,\n            )\n        except Exception as exc:\n            log.warning("HeartbeatScheduler: failed to persist run_result for %s: %s", job.id, exc)\n\n    # ------------------------------------------------------------------\n    # Tick loop\n    # ------------------------------------------------------------------\n\n    async def _tick_loop(self) -> None:\n        """Run every 60 seconds; fire any jobs whose cron expression is due."""\n        log.debug("HeartbeatScheduler: tick loop started")\n        while self._running:\n            try:\n                await self._evaluate_tick()\n            except Exception as exc:  # noqa: BLE001\n                log.error("HeartbeatScheduler: tick evaluation error: %s", exc)\n\n            # Sleep until the start of the next minute\n            now = datetime.now(UTC)\n            seconds_until_next_minute = 60 - now.second\n            try:\n                await asyncio.sleep(seconds_until_next_minute)\n            except asyncio.CancelledError:\n                break\n\n        log.debug("HeartbeatScheduler: tick loop exited")\n\n    async def _evaluate_tick(self) -> None:\n        """Check which jobs are due and spawn them."""\n        try:\n            from sdk.agent.swarm_engine import ZombieReaper\n            count = await ZombieReaper.reclaim_zombies()\n            if count > 0:\n                log.info("HeartbeatScheduler: Reclaimed %d zombie task(s).", count)\n        except Exception as exc:\n            log.error("HeartbeatScheduler: failed to run zombie reaper: %s", exc)\n\n        now = datetime.now(UTC)\n        for job in self._jobs:\n            if job.is_startup:\n                continue  # @startup jobs are only fired in start()\n            if not job.is_due(now):\n                continue\n\n            log.info(\n                "HeartbeatScheduler: job %r (id=%s) is due — spawning",\n                job.name, job.id\n            )\n            self._spawn_job(job)\n\n    def _spawn_job(self, job: HeartbeatJob) -> None:\n        """Launch a job as a background asyncio Task."""\n        task = asyncio.create_task(\n            self._run_job(job),\n            name=f"heartbeat-job-{job.id[:8]}",\n        )\n        self._running_tasks.add(task)\n        task.add_done_callback(self._running_tasks.discard)\n\n    # ------------------------------------------------------------------\n    # Job execution\n    # ------------------------------------------------------------------\n\n    async def _run_job(self, job: HeartbeatJob) -> None:\n        """Execute a heartbeat job through GoalEngine and push WS notifications."""\n        log.info("HeartbeatScheduler: running job %r (id=%s) goal=%r", job.name, job.id, job.goal_text[:80])\n\n        # Mark in-memory as running\n        job.last_status = "running"\n        job.last_run_at = datetime.now(UTC)\n\n        await self._persist_run_start(job)\n\n        # Push fired notification\n        await _push("heartbeat.fired", {\n            "id": job.id,\n            "name": job.name,\n            "goal": job.goal_text,\n            "fired_at": job.last_run_at.isoformat(),\n        })\n\n        # Log to agent_log\n        try:\n            from python.harness import db\n            await db.log_to_db(\n                "INFO",\n                f"Heartbeat fired: {job.name}",\n                {"heartbeat_id": job.id, "goal": job.goal_text},\n            )\n        except Exception:\n            pass\n\n        # Run through GoalEngine\n        status = "success"\n        result: dict[str, Any] = {}\n        try:\n            from sdk.agent.goal_engine import GoalEngine\n            engine = GoalEngine()\n            result = await engine.run_goal(job.goal_text)\n            if not result.get("success", False):\n                status = "failed"\n                log.warning(\n                    "HeartbeatScheduler: job %r failed — %s",\n                    job.name, result.get("error", "unknown")\n                )\n            else:\n                log.info("HeartbeatScheduler: job %r completed successfully", job.name)\n        except Exception as exc:\n            status = "failed"\n            result = {"success": False, "error": str(exc)}\n            log.exception("HeartbeatScheduler: job %r raised an exception", job.name)\n\n        # Update in-memory state\n        job.last_status = status\n\n        # Persist to DB\n        await self._persist_run_result(job, status)\n\n        # Push completion notification\n        await _push("heartbeat.complete", {\n            "id": job.id,\n            "name": job.name,\n            "status": status,\n            "phases_completed": result.get("phases_completed", []),\n            "error": result.get("error"),\n            "completed_at": datetime.now(UTC).isoformat(),\n        })\n\n        # Log completion\n        try:\n            from python.harness import db\n            await db.log_to_db(\n                "INFO" if status == "success" else "WARNING",\n                f"Heartbeat complete: {job.name} — {status}",\n                {\n                    "heartbeat_id": job.id,\n                    "status": status,\n                    "error": result.get("error"),\n                },\n            )\n        except Exception:\n            pass\n\n    # ------------------------------------------------------------------\n    # Public helpers used by RPC handlers\n    # ------------------------------------------------------------------\n\n    def get_jobs(self) -> list[dict[str, Any]]:\n        """Return serialisable list of all jobs (used by heartbeat.list handler)."""\n... (file truncated, showing first 500 lines) ...
```
