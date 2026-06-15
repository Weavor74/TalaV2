"""
sdk/agent/queue.py
==================
Hierarchical Decomposition & Durable Task Queue — Tala Cognitive Engine Room
Milestone 1 | Tasks 1.1, 1.2, 1.3, 1.4

Provides:
  • GoalTreeDecomposer       — Parses free-form goals into a dependency tree (DAG).
  • BatchQueue               — Persistent PostgreSQL task queue and FSM state manager.
  • PersistentExecutionLoop  — Resumable execution loop with event-sourced persistence.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

import httpx
from python.harness import db
from sdk.agent.exceptions import TalaBaseError
from sdk.agent.persistence import (
    TaskEventStore,
    generate_deterministic_id,
    log_telemetry_event,
    project_task_state,
)
from sdk.agent.planner import (
    BoundedMilestonePersistence,
    EvaluationResult,
    ExecutionLoop,
    ExecutionPlan,
    ExecutionResult,
    InvariantViolationError,
    LedgerAssertionError,
    LoopPhase,
    MaxIterationsExceeded,
    Observation,
    TaskItem,
    TaskStatus,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Goal Tree Decomposer
# ---------------------------------------------------------------------------

class GoalTreeDecomposer:
    """
    Parses a free-form user goal into a hierarchical list of TaskItems (DAG).
    
    Tries to query Ollama/local providers first, falls back to the embedded
    harness inference, and finally falls back to a structural regex splitter
    if no LLM provider is online or if parsing fails.
    """

    @classmethod
    def _get_prompt_template(cls) -> str:
        return """You are the Tala Goal Decomposer.
Decompose the following high-level goal into a hierarchical list of sub-tasks forming a DAG.
Each sub-task must be structured as JSON with:
- id: A short unique identifier string slug (e.g. "read-code", "fix-bug")
- description: A clear, actionable description.
- dependencies: An array of IDs of other sub-tasks in this list that must finish BEFORE this one starts.
- parent_id: Optional ID of the parent task if this is a nested sub-task.
- intentional_runtime_error: Optional boolean. MUST be true if the user explicitly asks you to write code that produces a runtime error (e.g. NameError, ValueError) by misspelling a function or writing bad logic, BUT the Python syntax itself must remain valid.
- intentional_syntax_error: Optional boolean. MUST be true if the user explicitly asks you to write invalid Python syntax (e.g. missing parentheses around print).
- expected_stdout: Optional string. If this specific sub-task MUST print a string to standard output, specify it here.
- repair_policy: Optional JSON object for intentional errors. Define the constraints for the repair phase:
  - expected_failure_string: String that MUST appear in the bug phase's error or output (e.g. "-1" or "AssertionError") to consider the bug successfully observed.
  - final_expected_stdout: The string the fully repaired script MUST output.
  - forbidden_repairs: Array of lazy workarounds to ban (e.g. ["bypassing the assertion", "aliasing the typo"]).
- context: Optional JSON object. ALWAYS prefer structured tools over shell commands. Valid tools are:
  - {{"tool": "write_file", "path": "file.py", "content": "print('hi')"}}
  - {{"tool": "run_python_file", "path": "file.py"}}
  - {{"tool": "shell", "command": "<cross-platform command>"}}

Goal: "{user_goal}"

Output JSON array only, wrapped in a Markdown code block like:
```json
[
  {{
    "id": "task-1",
    "description": "Step 1 description",
    "dependencies": []
  }}
]
```"""

    @classmethod
    def _get_prompt_hash(cls) -> str:
        import hashlib
        return hashlib.sha256(cls._get_prompt_template().encode("utf-8")).hexdigest()

    def _get_semantic_similarity(self, goal_a: str, goal_b: str) -> float:
        """Compute cosine similarity of token occurrences between two goal strings."""
        import re
        import math
        # Extract tokens
        words_a = re.findall(r"[a-z0-9]+", goal_a.lower())
        words_b = re.findall(r"[a-z0-9]+", goal_b.lower())
        if not words_a or not words_b:
            return 0.0
            
        # Frequency dictionaries
        freq_a: dict[str, float] = {}
        for w in words_a:
            freq_a[w] = freq_a.get(w, 0.0) + 1.0
        freq_b: dict[str, float] = {}
        for w in words_b:
            freq_b[w] = freq_b.get(w, 0.0) + 1.0
            
        # Magnitudes (norms)
        norm_a = math.sqrt(sum(v ** 2 for v in freq_a.values()))
        norm_b = math.sqrt(sum(v ** 2 for v in freq_b.values()))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
            
        # Dot product
        dot = sum(freq_a[w] * freq_b.get(w, 0.0) for w in freq_a)
        return dot / (norm_a * norm_b)

    async def decompose(self, user_goal: str, parent_id: str | None = None, bypass_cache: bool = False) -> list[TaskItem]:
        """
        Decompose a goal into a list of TaskItems with correct parent_ids and dependencies.
        """
        if not user_goal or not user_goal.strip():
            raise ValueError("user_goal must not be empty")

        prompt_hash = self._get_prompt_hash()

        # 1. Query semantic cache of decompositions to avoid re-decomposing identical or similar goals
        cached_json = None
        try:
            rows = await db.fetch("SELECT goal, tasks_json FROM goal_decompositions WHERE prompt_hash = $1", prompt_hash)
            best_score = 0.0
            best_tasks = None
            
            for row in rows:
                score = self._get_semantic_similarity(user_goal, row["goal"])
                if score > best_score:
                    best_score = score
                    best_tasks = row["tasks_json"]
            
            if best_score >= 0.95 and best_tasks:
                log.info("GoalTreeDecomposer: Semantic cache HIT (score=%.2f) for goal: %r", best_score, user_goal)
                if isinstance(best_tasks, str):
                    cached_json = json.loads(best_tasks)
                else:
                    cached_json = best_tasks
        except Exception as e:
            log.warning("GoalTreeDecomposer: Semantic cache lookup failed: %s", e)

        json_data = cached_json
        
        if not json_data:
            # 2. Try LLM-based decomposition
            json_data = await self._decompose_via_llm(user_goal)
            
            # 3. Store in semantic cache if LLM succeeded
            if json_data and not bypass_cache:
                try:
                    await db.execute(
                        "INSERT INTO goal_decompositions (goal, tasks_json, prompt_hash) VALUES ($1, $2, $3)",
                        user_goal,
                        json_data,
                        prompt_hash
                    )
                    log.info("GoalTreeDecomposer: Stored decomposition in semantic cache")
                except Exception as e:
                    log.warning("GoalTreeDecomposer: Failed to store decomposition in semantic cache: %s", e)

        if json_data:
            try:
                tasks: list[TaskItem] = []
                id_map: dict[str, str] = {}  # Maps LLM IDs to new deterministic UUIDs

                # Register all tasks first to generate clean UUIDs
                for item in json_data:
                    llm_id = item.get("id")
                    if not llm_id:
                        continue
                    deterministic_id = generate_deterministic_id(
                        {"description": item.get("description"), "goal": user_goal},
                        salt=llm_id,
                    )
                    id_map[llm_id] = deterministic_id

                # Check if any task requested intentional_error
                has_runtime_error = any(item.get("intentional_runtime_error") for item in json_data)
                has_syntax_error = any(item.get("intentional_syntax_error") for item in json_data)

                # Create TaskItem structures
                for item in json_data:
                    llm_id = item.get("id")
                    if not llm_id:
                        continue
                    
                    if has_runtime_error:
                        item["intentional_runtime_error"] = True
                    if has_syntax_error:
                        item["intentional_syntax_error"] = True
                        
                    # Map dependencies
                    deps = [id_map[d] for d in item.get("dependencies", []) if d in id_map]
                    
                    task_parent = item.get("parent_id")
                    mapped_parent = id_map.get(task_parent) if task_parent else parent_id

                    task = TaskItem(
                        task_id=id_map[llm_id],
                        description=item.get("description", ""),
                        dependencies=deps,
                    )
                    # Expose custom attributes
                    task.parent_id = mapped_parent
                    task.payload = item
                    tasks.append(task)

                log.info("GoalTreeDecomposer: LLM decomposition produced %d task(s)", len(tasks))
                return tasks
            except Exception as exc:
                log.warning("GoalTreeDecomposer: LLM parsing or ID mapping failed, falling back: %s", exc)

        # Fallback to regex-based linear sequence
        return self._fallback_decompose(user_goal, parent_id)

    async def _decompose_via_llm(self, user_goal: str) -> list[dict[str, Any]] | None:
        """Call Ollama or local embedded providers to parse the goal into JSON."""
        # 1. Look for configured inference providers in DB
        providers = []
        try:
            db.get_pool()
            rows = await db.fetch(
                "SELECT id, type, name, priority, status, config FROM inference_providers ORDER BY priority ASC"
            )
            providers = [dict(r) for r in rows]
        except Exception:
            pass

        # If no DB providers, add a local Ollama default configuration
        if not providers:
            providers = [
                {
                    "id": "ollama-local",
                    "type": "local",
                    "name": "Ollama Local",
                    "config": {"endpoint": "http://localhost:11434", "model": "llama3.2"},
                }
            ]

        prompt = self._get_prompt_template().format(user_goal=user_goal)

        failed_endpoints = set()
        for provider in providers:
            # Skip offline providers to avoid slow sequential timeouts (10s each)
            if provider.get("status") == "offline" and provider.get("id") != "embedded-builtin":
                log.debug("GoalTreeDecomposer: Skipping offline provider '%s'", provider.get("name"))
                continue

            prov_type = provider.get("type")
            config = provider.get("config", {})
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except Exception:
                    config = {}

            if prov_type == "local":
                endpoint = config.get("endpoint", "http://localhost:11434")
                if endpoint in failed_endpoints:
                    log.debug("GoalTreeDecomposer: Skipping provider '%s' because its endpoint %s failed previously", provider.get("name"), endpoint)
                    continue

                model = config.get("model", "llama3.2")
                try:
                    async with httpx.AsyncClient(timeout=180.0) as client:
                        resp = await client.post(
                            f"{endpoint}/api/generate",
                            json={"model": model, "prompt": prompt, "stream": False},
                        )
                        if resp.status_code == 200:
                            resp_data = resp.json()
                            response_text = resp_data.get("response", "")
                            parsed = self._extract_json_block(response_text)
                            if parsed:
                                return parsed
                        else:
                            log.warning("Ollama provider '%s' returned status %d", provider.get("name"), resp.status_code)
                            if resp.status_code in (500, 503, 504):
                                failed_endpoints.add(endpoint)
                except Exception as e:
                    log.warning("Ollama provider failed: %s", e)
                    failed_endpoints.add(endpoint)
                    try:
                        db.get_pool()
                        await db.execute(
                            "UPDATE inference_providers SET status = 'offline', updated_at = NOW() WHERE id = $1",
                            provider.get("id")
                        )
                    except Exception:
                        pass

            elif prov_type == "embedded":
                try:
                    from python.inference.embedded_engine import check_model_cached, generate_stream
                    if not check_model_cached():
                        raise RuntimeError("Embedded model not fully downloaded / cached yet.")
                    chunks = []
                    async for chunk in generate_stream(prompt, max_tokens=1024, temperature=0.2):
                        chunks.append(chunk)
                    full_text = "".join(chunks)
                    parsed = self._extract_json_block(full_text)
                    if parsed:
                        return parsed
                except Exception as e:
                    log.warning("Embedded generator failed: %s", e)

        return None

    def _extract_json_block(self, text: str) -> list[dict[str, Any]] | None:
        """Helper to extract and parse JSON from LLM responses."""
        try:
            # Look for markdown code blocks
            match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
            json_str = match.group(1) if match else text
            data = json.loads(json_str.strip())
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return None

    def _fallback_decompose(self, goal: str, parent_id: str | None = None) -> list[TaskItem]:
        """Simple split-based fallback when inference fails."""
        # Split on separators
        separators = (".", ";", "\n", "→", "->")
        normalized = goal
        for sep in separators:
            normalized = normalized.replace(sep, "|")
        segments = [p.strip() for p in normalized.split("|") if p.strip()]
        
        if not segments:
            segments = [goal.strip()]

        tasks: list[TaskItem] = []
        prev_id: str | None = None

        for idx, segment in enumerate(segments):
            deterministic_id = generate_deterministic_id(
                {"description": segment, "goal": goal},
                salt=f"fallback-{idx}",
            )
            deps = [prev_id] if prev_id else []
            task = TaskItem(
                task_id=deterministic_id,
                description=segment,
                dependencies=deps,
            )
            task.parent_id = parent_id
            task.payload = {}
            tasks.append(task)
            prev_id = deterministic_id

        return tasks


import re


# ---------------------------------------------------------------------------
# Batch Queue State Manager
# ---------------------------------------------------------------------------

class BatchQueue:
    """
    Manages persistent scheduling and FSM status transitions of tasks in Postgres.
    """

    @classmethod
    async def enqueue(
        cls,
        description: str,
        dependencies: list[str] | None = None,
        parent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """
        Enqueue a new task into the PostgreSQL database queue.
        """
        task_id = generate_deterministic_id(
            {"description": description, "parent_id": parent_id},
            salt=str(datetime.now(UTC).timestamp()),
        )
        deps = dependencies or []
        payload_data = payload or {}
        
        db_available = False
        try:
            db.get_pool()
            db_available = True
        except RuntimeError:
            pass

        if db_available:
            try:
                query = """
                    INSERT INTO tasks (id, parent_id, description, status, dependencies, payload, result, completed_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, '{}'::jsonb, NULL)
                    ON CONFLICT (id) DO UPDATE SET
                        parent_id = EXCLUDED.parent_id,
                        description = EXCLUDED.description,
                        status = EXCLUDED.status,
                        dependencies = EXCLUDED.dependencies,
                        payload = EXCLUDED.payload,
                        result = '{}'::jsonb,
                        completed_at = NULL
                """
                await db.execute(
                    query,
                    task_id,
                    parent_id,
                    description,
                    "pending",
                    deps,
                    json.dumps(payload_data),
                )
            except Exception as exc:
                log.error("Failed to enqueue task in DB: %s", exc)

        # Log transition to event store
        await TaskEventStore.append_event(task_id, "task_init", {"description": description})
        await TaskEventStore.append_event(
            task_id,
            "state_transition",
            {"from_status": None, "to_status": "pending", "timestamp": datetime.now(UTC).isoformat()},
        )
        return task_id

    @classmethod
    async def enqueue_task(
        cls,
        task_id: str,
        description: str,
        status: str = "pending",
        dependencies: list[str] | None = None,
        parent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """
        Directly enqueue a task with a predefined task_id.
        """
        deps = dependencies or []
        payload_data = payload or {}
        if "dependencies" in payload_data and not deps:
            deps = payload_data["dependencies"]
            
        db_available = False
        try:
            db.get_pool()
            db_available = True
        except RuntimeError:
            pass

        if db_available:
            try:
                query = """
                    INSERT INTO tasks (id, parent_id, description, status, dependencies, payload, result, completed_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, '{}'::jsonb, NULL)
                    ON CONFLICT (id) DO UPDATE SET
                        parent_id = EXCLUDED.parent_id,
                        description = EXCLUDED.description,
                        status = EXCLUDED.status,
                        dependencies = EXCLUDED.dependencies,
                        payload = EXCLUDED.payload,
                        result = '{}'::jsonb,
                        completed_at = NULL
                """
                await db.execute(
                    query,
                    task_id,
                    parent_id,
                    description,
                    status,
                    deps,
                    json.dumps(payload_data),
                )
            except Exception as exc:
                log.error("Failed to enqueue task in DB: %s", exc)

        # Log transition to event store
        await TaskEventStore.append_event(task_id, "task_init", {"description": description})
        await TaskEventStore.append_event(
            task_id,
            "state_transition",
            {"from_status": None, "to_status": status, "timestamp": datetime.now(UTC).isoformat()},
        )

    @classmethod
    async def get_next_runnable_task(cls) -> TaskItem | None:
        """
        Query the database to find a pending task whose dependencies are all complete.
        """
        db_available = False
        try:
            db.get_pool()
            db_available = True
        except RuntimeError:
            pass

        if not db_available:
            return None

        try:
            # Query for pending tasks
            query = "SELECT id, parent_id, description, status, dependencies, payload, result FROM tasks WHERE status = 'pending'"
            pending_rows = await db.fetch(query)
            if not pending_rows:
                return None

            # Verify dependencies
            for row in pending_rows:
                deps = row["dependencies"]
                can_run = True
                
                if deps:
                    # Check status of dependency tasks
                    dep_placeholders = ",".join(f"${i+1}" for i in range(len(deps)))
                    dep_query = f"SELECT status FROM tasks WHERE id IN ({dep_placeholders})"
                    dep_rows = await db.fetch(dep_query, *deps)
                    
                    # All dependencies must exist and be COMPLETE
                    completed_count = sum(1 for r in dep_rows if r["status"] == "complete")
                    if completed_count != len(deps):
                        can_run = False

                if can_run:
                    task = TaskItem(
                        task_id=row["id"],
                        description=row["description"],
                        dependencies=row["dependencies"],
                    )
                    task.status = TaskStatus.PENDING
                    task.parent_id = row["parent_id"]
                    
                    payload_val = row["payload"]
                    task.payload = payload_val if isinstance(payload_val, dict) else json.loads(payload_val or "{}")
                    
                    result_val = row["result"]
                    task.result = result_val if isinstance(result_val, dict) else json.loads(result_val or "{}")
                    return task
        except Exception as exc:
            log.error("Failed to query next runnable task: %s", exc)
        
        return None

    @classmethod
    async def transition_task(
        cls,
        task_id: str,
        from_status: TaskStatus,
        to_status: TaskStatus,
        reason: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        """
        Transition a task's FSM state and commit to Postgres and Event Log.
        """
        db_available = False
        try:
            db.get_pool()
            db_available = True
        except RuntimeError:
            pass

        result_data = result or {}
        if isinstance(to_status, str):
            to_status_str = to_status.lower()
            completed_at = datetime.now(UTC) if to_status_str in ("complete", "completed") else None
        else:
            to_status_str = to_status.name.lower()
            completed_at = datetime.now(UTC) if to_status.name == "COMPLETE" else None

        if db_available:
            try:
                query = """
                    UPDATE tasks
                    SET status = $1, result = $2::jsonb, completed_at = $3
                    WHERE id = $4
                """
                await db.execute(
                    query,
                    to_status_str,
                    json.dumps(result_data),
                    completed_at,
                    task_id,
                )
            except Exception as exc:
                log.error("Failed to update task transition in DB: %s", exc)

        # Log transition to event store
        await TaskEventStore.append_event(
            task_id,
            "state_transition",
            {
                "from_status": from_status.lower() if isinstance(from_status, str) else from_status.name.lower(),
                "to_status": to_status_str,
                "reason": reason,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )


# ---------------------------------------------------------------------------
# Persistent Resumable Execution Loop
# ---------------------------------------------------------------------------

class PersistentExecutionLoop(ExecutionLoop):
    """
    Persistent execution loop implementing the Plan->Execute->Observe->Evaluate->Re-Plan
    lifecycle. Integrates FSM persistence, pausing, recovery, and telemetry.
    """

    def __init__(
        self,
        invariants: list[Callable[[], bool]] | None = None,
        ledger: BoundedMilestonePersistence | None = None,
        root_dir: str | None = None,
    ) -> None:
        super().__init__(invariants, ledger)
        self._is_paused = False
        self._is_aborted = False
        self.root_dir = root_dir or os.getcwd()

    async def execute_async(self, plan: ExecutionPlan) -> ExecutionResult:
        """
        EXECUTE phase — carry out the plan steps asynchronously.
        Routes the task/steps to the TalaExecutionDispatcher.
        """
        if self._execute_hook is not None:
            import asyncio as local_asyncio
            if local_asyncio.iscoroutinefunction(self._execute_hook):
                return await self._execute_hook(plan)
            return self._execute_hook(plan)

        from sdk.agent.dispatcher import TalaExecutionDispatcher
        dispatcher = TalaExecutionDispatcher()
        
        success = True
        outputs = []
        errors = []
        
        for step in plan.steps:
            try:
                res = await dispatcher.dispatch(step, self.root_dir)
                if not res.get("success", False):
                    success = False
                    errors.append(res.get("error") or f"Step failed: {step}")
                outputs.append({
                    "step": step,
                    "action_taken": res.get("action_taken"),
                    "output": res.get("output"),
                })
            except Exception as exc:
                success = False
                errors.append(str(exc))
                outputs.append({"step": step, "error": str(exc)})
                
        return ExecutionResult(
            task_id=plan.task_id,
            plan=plan,
            success=success,
            output={"steps_executed": outputs},
            error="; ".join(errors) if errors else None,
        )

    def pause(self) -> None:
        """Request loop pause before the next phase starts."""
        self._is_paused = True
        log.info("Pause requested for persistent loop execution.")

    def abort(self) -> None:
        """Request immediate abort of the loop."""
        self._is_aborted = True
        log.warning("Abort requested for persistent loop execution.")

    async def _check_signals(self, task: TaskItem, iteration: int) -> bool:
        """
        Check for abort or pause signals.
        Returns True if execution should be paused (suspended).
        Raises InvariantViolationError if execution is aborted.
        """
        if self._is_aborted:
            await BatchQueue.transition_task(
                task.task_id, TaskStatus.IN_PROGRESS, TaskStatus.FAILED, reason="Aborted by user"
            )
            task.status = TaskStatus.FAILED
            raise InvariantViolationError("AbortSignal", "Task run aborted by user instruction")

        if self._is_paused:
            task.status = TaskStatus.BLOCKED  # Blocked represents paused in current UI status
            await BatchQueue.transition_task(
                task.task_id, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, reason="Paused by user"
            )
            log.info("Execution paused at iteration %d checkpoint.", iteration)
            return True

        return False

    async def run_async(
        self,
        task: TaskItem,
        max_iterations: int = 5,
        ledger_assertions: list[bool] | None = None,
    ) -> TaskItem:
        """
        Asynchronously runs the execution loop. Saves all state transitions and
        telemetry to the Event Store and updates PostgreSQL.
        """
        if ledger_assertions is None:
            ledger_assertions = [True]

        self._is_paused = False
        self._is_aborted = False

        # --- Reconstruct state if task was previously running/paused ---
        events = await TaskEventStore.fetch_events(task.task_id)
        projected = project_task_state(events)
        
        start_iteration = projected.get("iterations", 0) + 1
        if start_iteration > 1:
            log.info("Resuming task %s from iteration %d", task.task_id[:8], start_iteration)
        else:
            log.info("Starting fresh execution loop for task %s", task.task_id[:8])

        # Transition task to IN_PROGRESS in DB and Event Store
        task.status = TaskStatus.IN_PROGRESS
        await BatchQueue.transition_task(task.task_id, TaskStatus.PENDING, TaskStatus.IN_PROGRESS)

        current_plan: ExecutionPlan | None = None
        evaluation: EvaluationResult | None = None

        for iteration in range(start_iteration, max_iterations + 1):
            if await self._check_signals(task, iteration):
                return task

            log.info("=== Iteration %d / %d [task %s] ===", iteration, max_iterations, task.task_id[:8])
            await TaskEventStore.append_event(task.task_id, "iteration_start", {"iteration": iteration})

            # ── Phase 1: PLAN ──────────────────────────────────────────────
            current_plan = self.plan(task, iteration=iteration, prior_evaluation=evaluation)
            await TaskEventStore.append_event(
                task.task_id,
                "plan_generated",
                {"steps": current_plan.steps, "metadata": current_plan.metadata},
            )

            # ── Phase 2: EXECUTE ───────────────────────────────────────────
            result = await self.execute_async(current_plan)
            for step in current_plan.steps:
                await TaskEventStore.append_event(
                    task.task_id,
                    "step_execution",
                    {"step": step, "success": result.success, "timestamp": datetime.now(UTC).isoformat()},
                )

            # Check signals immediately after execution hook triggers
            if await self._check_signals(task, iteration):
                return task

            # ── Phase 3: OBSERVE ───────────────────────────────────────────
            observation = self.observe(result)
            await TaskEventStore.append_event(
                task.task_id,
                "observation",
                {"facts": observation.facts, "anomalies": observation.anomalies},
            )

            # ── Phase 4: EVALUATE ──────────────────────────────────────────
            evaluation = self.evaluate(observation, self._invariants)
            await TaskEventStore.append_event(
                task.task_id,
                "evaluation",
                {
                    "passed": evaluation.passed,
                    "should_re_plan": evaluation.should_re_plan,
                    "detail": evaluation.detail,
                    "failed_invariant": evaluation.failed_invariant,
                },
            )

            # Record telemetry metrics
            await log_telemetry_event(
                task.task_id, "iteration_duration_sec", 1.0, {"iteration": iteration}
            )
            await log_telemetry_event(
                task.task_id, "anomalies_count", len(observation.anomalies), {"iteration": iteration}
            )

            # Halt on invariant failure
            if not evaluation.passed:
                task.status = TaskStatus.FAILED
                await BatchQueue.transition_task(
                    task.task_id,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.FAILED,
                    reason=f"Invariant violation: {evaluation.failed_invariant}",
                )
                raise InvariantViolationError(
                    invariant_name=evaluation.failed_invariant or "unknown",
                    detail=evaluation.detail,
                )

            # Complete if execution succeeded and no anomalies trigger re-plan
            if result.success and not evaluation.should_re_plan:
                try:
                    self._ledger.halt_on_missing_ledger_assertion(ledger_assertions)
                    task.status = TaskStatus.COMPLETE
                    await BatchQueue.transition_task(
                        task.task_id,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.COMPLETE,
                        result={"steps_completed": current_plan.steps},
                    )
                    return task
                except LedgerAssertionError as exc:
                    await BatchQueue.transition_task(
                        task.task_id,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.FAILED,
                        reason="Ledger assertion validation failed",
                    )
                    raise

            # ── Phase 5: RE-PLAN ───────────────────────────────────────────
            if iteration < max_iterations:
                current_plan = await self.re_plan(task, evaluation)
                await TaskEventStore.append_event(
                    task.task_id,
                    "replanned",
                    {"steps": current_plan.steps, "iteration": iteration + 1},
                )

        # Exhausted iterations
        task.status = TaskStatus.FAILED
        await BatchQueue.transition_task(
            task.task_id,
            TaskStatus.IN_PROGRESS,
            TaskStatus.FAILED,
            reason=f"Max iterations ({max_iterations}) exceeded",
        )
        raise MaxIterationsExceeded(task.task_id, max_iterations)

    async def re_plan(
        self,
        task: TaskItem,
        evaluation: EvaluationResult,
    ) -> ExecutionPlan:
        """
        RE-PLAN phase — produce a revised plan based on evaluation feedback using the LLM.
        """
        log.info(
            "PersistentExecutionLoop.re_plan: task=%s evaluation.detail=%r",
            task.task_id[:8], evaluation.detail,
        )
        
        plan_iteration = evaluation.observation.result.plan.iteration
        prior_steps = evaluation.observation.result.plan.steps
        anomalies = evaluation.observation.anomalies
        facts = evaluation.observation.facts
        error = evaluation.observation.result.error
        
        prompt = f"""You are an autonomous agent re-planning a failed task phase.
Original task description: {task.description}
Current iteration: {plan_iteration}
Prior plan steps: {prior_steps}
Observed facts: {facts}
Observed anomalies: {anomalies}
Execution error: {error}
Evaluation detail: {evaluation.detail}

Generate a new, revised execution plan (list of steps) that fixes the issue and successfully completes the goal.
Output a JSON array only, containing the new plan steps as string elements, wrapped in a Markdown code block like:
```json
[
  "Step 1...",
  "Step 2..."
]
```"""

        # Call LLM
        new_steps = await self._call_llm_for_plan(prompt)
        
        if not new_steps:
            # Fallback: prepend "Retry: " or similar
            log.warning("LLM re-planning failed or returned empty steps. Using fallback plan.")
            new_steps = [f"Retry: {task.description}"]
            if prior_steps:
                new_steps += [s for s in prior_steps if not s.startswith("Retry:")]

        plan = ExecutionPlan(
            task_id=task.task_id,
            steps=new_steps,
            iteration=plan_iteration + 1,
        )
        return plan

    async def _call_llm_for_plan(self, prompt: str) -> list[str] | None:
        """Query LLM via the unified handle_inference_generate provider chain."""
        try:
            from python.harness.server import handle_inference_generate
            params = {
                "prompt": prompt,
                "stream": False,
                "raw_prompt": True,
                "system_prompt": "You are an autonomous agent planner. Fulfill the plan request by returning a JSON array of steps.",
            }
            res_str = await handle_inference_generate(params, ws=None, req_id="sdk-replan")
            import json
            res_dict = json.loads(res_str)
            if "result" in res_dict:
                text = res_dict["result"]["text"]
                return self._extract_json_steps(text)
            elif "error" in res_dict:
                log.warning("PersistentExecutionLoop: re_plan inference error: %s", res_dict["error"])
        except Exception as exc:
            log.warning("PersistentExecutionLoop: re_plan failed to call handle_inference_generate: %s", exc)
        return None

    def _extract_json_steps(self, text: str) -> list[str] | None:
        import re
        try:
            match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
            json_str = match.group(1) if match else text
            data = json.loads(json_str.strip())
            if isinstance(data, list):
                return [str(item) for item in data]
        except Exception:
            pass
        return None
