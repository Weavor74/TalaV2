"""
sdk/agent/goal_engine.py
========================
Goal Engine Orchestrator — Tala Cognitive Engine Room
Milestone 7 | Phase 2 Goal Execution
"""

from __future__ import annotations

import logging
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sdk.agent.adapter import AdapterPacket

from sdk.agent.queue import GoalTreeDecomposer, BatchQueue
from sdk.agent.dispatcher import TalaExecutionDispatcher
from sdk.agent.recovery import RollbackRecovery, CompletionReporter
from sdk.agent.planner import TaskStatus, PhasePlan
from sdk.agent.persistence import generate_deterministic_id, TaskEventStore
from sdk.agent.telemetry import LiveStatusReporter
from sdk.agent.self_documenter import SelfDocumenter
from sdk.agent.repair_policy import RepairPolicyEngine, RepairContext

log = logging.getLogger(__name__)


#: Opening of the re-planner prompt. When a phase fails, the whole prompt is
#: handed to the decomposer as its goal, and the model tends to echo this
#: opening back as the new task's description.
_REPLAN_PREFIX = "You were trying to accomplish the following task:"


def strip_replan_wrapper(description: str, max_depth: int = 6) -> str:
    """
    Return the original task description, with any re-plan scaffolding removed.

    A failed phase is re-planned by wrapping its description in
    ``You were trying to accomplish the following task: '<desc>'. However, the
    execution failed with ...``. That string is the decomposer's *goal*, so the
    model echoes it into the new task's description -- and if that task fails
    too, the wrapper wraps itself. Observed in the wild as a wiki node titled
    ``You were trying to accomplish the following task: 'You were trying to
    accomplish the following task: 'Modify the code to correct the addition
    logic'''``, with the real task buried two layers down.

    Nested scaffolding is not cosmetic: it becomes the phase name, the next
    retry's input, the slug of any node distilled from it, and the keywords that
    node is retrieved by. Unwrapping is bounded rather than recursive so a
    pathological string cannot spin here.
    """
    text = str(description or "").strip()
    for _ in range(max_depth):
        if not text.startswith(_REPLAN_PREFIX):
            break
        rest = text[len(_REPLAN_PREFIX):].strip()
        # Drop the failure narration the re-planner appends after the task.
        cut = rest.find(". However,")
        if cut != -1:
            rest = rest[:cut]
        rest = rest.strip().rstrip(".").strip()
        # The task itself is quoted; unwrap one matching pair.
        if len(rest) >= 2 and rest[0] in "\"'" and rest[-1] == rest[0]:
            rest = rest[1:-1]
        rest = rest.strip()
        if not rest:
            break
        text = rest
    return text or str(description or "").strip()

_ACTIVE_TASK_IDS: set[str] = set()

def register_active_task(task_id: str) -> None:
    if task_id:
        _ACTIVE_TASK_IDS.add(task_id)

def unregister_active_task(task_id: str) -> None:
    if task_id:
        _ACTIVE_TASK_IDS.discard(task_id)

def get_active_task_ids() -> set[str]:
    return set(_ACTIVE_TASK_IDS)


# Bound on how many prior phases feed into the next phase's prompt, and how
# much of each one's output survives truncation. Deliberately small: the
# point of threading prior-phase output forward is to give the next phase
# real visibility into what already happened without reintroducing the
# unbounded-context-growth problem this exists to avoid -- so this stays a
# curated pointer, not a full transcript. Full detail always remains
# recoverable from the tasks table / TaskEventStore regardless of what's
# folded into any single prompt.
_PRIOR_PHASE_SUMMARY_MAX_PHASES = 3
_PRIOR_PHASE_SUMMARY_MAX_CHARS_PER_PHASE = 400


async def find_resumable_suspended_goals(max_resume_attempts: int = 5) -> list[dict[str, Any]]:
    """
    Find goal-parent tasks BLOCKED specifically by BatchPolicy budget
    exhaustion (the `suspended` path in GoalEngine.run_goal) that are still
    eligible for automatic resume.

    Regression this closes: a goal that outran its BatchPolicy step/time
    budget transitioned to BLOCKED and just sat there forever -- nothing
    ever resumed it. run_goal already has everything needed to pick a goal
    back up where it left off (Ghost-blockchain phase-index restore, plus
    the prior-phase-output threading above); the only missing piece was
    something to actually call it again.

    Deliberately excludes:
      - swarm topology runs (payload carries "topology_id") -- those block
        for gate/human-review reasons via a completely different mechanism
        (SwarmEngine.evaluate_edges' requires_human policy) and must stay
        human-gated, never auto-resumed.
      - a goal whose task_id is already registered as actively executing
        (get_active_task_ids()) -- avoids launching a second concurrent
        run_goal() for the same goal on an overlapping tick.
      - a goal that has already been suspended >= max_resume_attempts times
        (counted from its own persisted state_transition events, not an
        in-memory counter, so this survives a process restart) -- a goal
        that can never finish must not retry forever.
    """
    from python.harness import db
    import json as _json

    try:
        db.get_pool()
    except RuntimeError:
        return []

    try:
        rows = await db.fetch(
            "SELECT id, description, payload FROM tasks WHERE status = 'blocked' AND parent_id IS NULL"
        )
    except Exception as exc:
        log.warning("find_resumable_suspended_goals: query failed: %s", exc)
        return []

    active_ids = get_active_task_ids()
    resumable: list[dict[str, Any]] = []

    for row in rows:
        task_id = row["id"]
        if task_id in active_ids:
            continue

        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        payload = payload or {}
        if "topology_id" in payload:
            continue  # swarm run -- human-gated, must not auto-resume

        try:
            events = await TaskEventStore.fetch_events(task_id)
        except Exception as exc:
            log.warning("find_resumable_suspended_goals: failed to fetch events for %s: %s", task_id[:8], exc)
            continue

        blocked_count = sum(
            1 for e in events
            if e.get("event_type") == "state_transition" and e.get("payload", {}).get("to_status") == "blocked"
        )
        if blocked_count >= max_resume_attempts:
            log.debug(
                "find_resumable_suspended_goals: goal %s exhausted %d/%d resume attempts, leaving blocked",
                task_id[:8], blocked_count, max_resume_attempts,
            )
            continue

        goal_text = row["description"]
        if not goal_text:
            continue

        resumable.append({"task_id": task_id, "goal_text": goal_text, "attempt": blocked_count})

    return resumable


async def _resume_one_goal(goal_text: str, task_id: str) -> None:
    """Fire-and-forget re-entry into run_goal() for one suspended goal."""
    try:
        from sdk.agent.batch_policy import BatchPolicy
        engine = GoalEngine()
        # overnight() is the preset built for exactly this: long, bounded,
        # unattended continuation, rather than the tighter interactive-chat
        # default budget that likely triggered the original suspension.
        await engine.run_goal(goal_text, policy=BatchPolicy.overnight())
    except Exception:
        log.exception("GoalEngine: auto-resume crashed for suspended goal %s", task_id[:8])


async def resume_suspended_goals(max_resume_attempts: int = 5) -> int:
    """
    Auto-resume every eligible budget-suspended goal (see
    find_resumable_suspended_goals). Each resume re-enters run_goal() with
    the exact same goal text -- via the semantic decomposition cache and
    the existing Ghost-blockchain phase-index restore, this picks the goal
    back up at the phase it left off on with a fresh execution budget,
    instead of replaying everything from scratch or requiring a human to
    manually retrigger it.

    Intended to be called periodically (see
    HeartbeatScheduler._evaluate_tick, which already runs a comparable
    per-tick sweep for ZombieReaper) so a goal that outruns a single
    BatchPolicy budget keeps making real progress across multiple bounded
    runs -- segmented long-running execution driven by the persisted
    task-styled state, not by holding everything in one context.

    Returns the number of goals handed off for resume this call.
    """
    import asyncio

    candidates = await find_resumable_suspended_goals(max_resume_attempts)
    for candidate in candidates:
        log.info(
            "GoalEngine: auto-resuming suspended goal %s (attempt %d/%d): %r",
            candidate["task_id"][:8], candidate["attempt"] + 1, max_resume_attempts,
            candidate["goal_text"][:80],
        )
        asyncio.create_task(_resume_one_goal(candidate["goal_text"], candidate["task_id"]))

    return len(candidates)


def _format_prior_phase_summary(phase_output_summaries: list[dict[str, Any]]) -> str:
    """Render the most recent completed phases' outputs as a compact block
    for injection into the next phase's prompt (via task.payload["context"]
    -> DAGExecutionEngine.execute_task_as_dag's full_goal)."""
    recent = phase_output_summaries[-_PRIOR_PHASE_SUMMARY_MAX_PHASES:]
    lines = ["Context from earlier phases of this same goal (for continuity -- build on this, don't redo it):"]
    for entry in recent:
        output = entry.get("output")
        text = output if isinstance(output, str) else str(output) if output is not None else "(no output recorded)"
        text = text.strip()
        if len(text) > _PRIOR_PHASE_SUMMARY_MAX_CHARS_PER_PHASE:
            text = text[:_PRIOR_PHASE_SUMMARY_MAX_CHARS_PER_PHASE] + "... (truncated)"
        lines.append(f"- Phase \"{entry.get('description', 'unknown')}\" produced: {text}")
    return "\n".join(lines)


class GoalEngine:
    """
    Orchestrates the decomposition, checkpointing, dispatching, verification,
    rollback, and reporting of multi-phase goals.

    When receiving user feedback:
    Evaluate if the error is due to a misaligned html-nn adapter (behavioral) or a logic error (factual).
    If behavioral: Analyze the emotional_valence and semantic_tags of the used adapter.
    Propose a weight shift: Determine if the relevance_weight should increase to prevent future use or if the emotional_valence needs to be recalibrated to match the user's intent.
    Decision: If you conclude a change is necessary, execute the update_manifest tool and log the reasoning to the Ghost Ledger. If you conclude the error was an outlier, log the feedback as a 'Non-Critical-Event' and do not adjust weights.
    """

    def __init__(self, root_dir: str | None = None, is_fallback: bool = False) -> None:
        self.root_dir = root_dir or os.getcwd()
        self.is_fallback = is_fallback
        self.dispatcher = TalaExecutionDispatcher()
        self.recovery = RollbackRecovery(self.root_dir)

    async def run_goal(
        self,
        goal_text: str,
        policy: BatchPolicy | None = None,
        adapter_packet: "AdapterPacket | None" = None,
        pre_decomposed_tasks: list | None = None,
        fresh_start: bool = False,
    ) -> dict[str, Any]:
        """
        Decompose a high-level goal, execute it phase-by-phase with checkpoints,
        and verify completion.

        Parameters
        ----------
        pre_decomposed_tasks:
            Optional list of already-decomposed ``TaskItem`` objects.  When
            provided the internal ``GoalTreeDecomposer`` call is skipped,
            saving a full LLM round-trip (critical on CPU-only inference).
        """
        log.info("GoalEngine: starting goal=%r with policy=%s", goal_text, policy)
        
        # Select and load active adapter (Query-to-Adapter Router)
        self.active_adapter_buffer = None
        try:
            selector = AdapterSelector(os.path.join(self.root_dir or "", "nodes"))
            best_adapter = selector.select_adapter(goal_text)
            if best_adapter:
                self.active_adapter_buffer = best_adapter
                log.info(
                    "GoalEngine: Selected and loaded active adapter %s with score %.3f",
                    best_adapter["node_id"],
                    best_adapter["score"]
                )
                print(f"[Router] Selected active adapter: {best_adapter['node_id']} (Score: {best_adapter['score']:.3f})")
        except Exception as router_exc:
            log.warning("GoalEngine: AdapterSelector failed: %s", router_exc)

        from sdk.agent.batch_policy import BatchPolicy, BudgetTracker
        policy = policy or BatchPolicy.auto()
        tracker = BudgetTracker(policy)

        # 1. Decompose goal (skip if pre-decomposed tasks were supplied by the caller)
        if pre_decomposed_tasks is not None:
            tasks = pre_decomposed_tasks
            log.info("GoalEngine: using %d pre-decomposed task(s) (skipping LLM decomposition)", len(tasks))
        else:
            decomposer = GoalTreeDecomposer()
            try:
                tasks = await decomposer.decompose(goal_text)
            except Exception as exc:
                log.error("GoalEngine: decomposition failed: %s", exc)
                return {"success": False, "error": f"Decomposition failed: {exc}"}

        if not tasks:
            log.warning("GoalEngine: decomposition produced no tasks")
            return {"success": False, "error": "No tasks produced from decomposition"}

        parent_task_id = generate_deterministic_id(goal_text, "goal-parent")

        # Ensure tasks are enqueued in DB
        bq = BatchQueue()
        try:
            await bq.enqueue_task(parent_task_id, goal_text, "in_progress")
            for idx, task in enumerate(tasks):
                task_payload = getattr(task, "payload", {}) or {}
                if not isinstance(task_payload, dict):
                    task_payload = {}
                task_payload.update({
                    "dependencies": task.dependencies,
                    "priority": idx,
                    "goal": goal_text,
                })
                task.payload = task_payload
                await bq.enqueue_task(
                    task.task_id,
                    task.description,
                    "pending",
                    parent_id=parent_task_id,
                    payload=task_payload,
                )

        except Exception as exc:
            log.error("GoalEngine: database enqueuing failed: %s", exc)

        log.info("GoalEngine: decomposed into %d phase(s)", len(tasks))

        phases_completed = []
        checkpoint_ids = []
        # Curated, size-bounded record of what each completed phase actually
        # produced (not just its name) -- threaded into the next phase's
        # prompt below so a "coder" phase can see what a preceding "planner"
        # phase decided instead of re-deriving from scratch. Survives a
        # Ghost-blockchain resume the same way phases_completed/checkpoint_ids
        # do, so this doesn't reset on restart either.
        phase_output_summaries: list[dict[str, Any]] = []
        success = True
        error_msg = None
        suspended = False

        # 2. Iterate through phases
        # Restore thought-state from Ghost Blockchain if present
        try:
            from sdk.agent.ghost_persistence import restore_from_ghost
            restored_state = None
            for t in tasks:
                state = await restore_from_ghost(t.task_id)
                if state:
                    if not restored_state or state["process_context"].get("idx", 0) > restored_state["process_context"].get("idx", 0):
                        restored_state = state

            # Resume is for a run that was INTERRUPTED, never for one that
            # already reached the end.
            #
            # Task ids are deterministic from goal text, so every re-run of
            # the same goal restores the previous run's thought-state. When
            # that state pointed at (or past) the last phase, the while-loop
            # below had nothing to execute and the goal returned success
            # having done absolutely nothing -- confirmed live 2026-08-11:
            # "decomposed into 2 phase(s)" immediately followed by "Resuming
            # at phase index 2", then straight to auto-documentation. Asking
            # for the same goal again is a request to run it again; only a
            # genuinely partial run (0 < idx < len) is resumable, and the
            # caller can force a clean run with fresh_start.
            _resume_idx = None
            if restored_state:
                _resume_idx = restored_state["process_context"].get("idx", 0) + 1

            if restored_state and fresh_start:
                log.info(
                    "GoalEngine: ignoring restored thought-state (fresh_start): would have "
                    "resumed at phase %d of %d.", _resume_idx, len(tasks),
                )
                restored_state = None
            elif restored_state and _resume_idx >= len(tasks):
                log.warning(
                    "GoalEngine: discarding stale thought-state -- it resumes at phase %d of "
                    "only %d, which would skip every phase and report success without "
                    "executing anything. Starting this run from the beginning.",
                    _resume_idx, len(tasks),
                )
                restored_state = None

            if restored_state:
                log.info("GoalEngine: Restoring thought-state from local Ghost Blockchain.")
                idx = _resume_idx
                phase_retry_counts = restored_state["process_context"].get("phase_retry_counts", {})
                phases_completed = restored_state["variable_state"].get("phases_completed", [])
                checkpoint_ids = restored_state["variable_state"].get("checkpoint_ids", [])
                phase_output_summaries = restored_state["variable_state"].get("phase_output_summaries", [])
                print(f"[Sync] Restored thought-state. Resuming at phase index {idx}...")
            else:
                idx = 0
                phase_retry_counts = {}
        except Exception as e:
            log.warning("GoalEngine: Thought-state restoration failed: %s", e)
            idx = 0
            phase_retry_counts = {}

        while idx < len(tasks):
            task = tasks[idx]
            
            # Check budget tracker before starting the phase
            if tracker.is_budget_exceeded():
                suspended = True
                error_msg = tracker.budget_reason()
                log.warning("GoalEngine: budget exceeded: %s. Suspending execution.", error_msg)
                
                # Suspend phase task
                await bq.transition_task(
                    task.task_id,
                    TaskStatus.PENDING,
                    TaskStatus.BLOCKED, # Paused/Blocked signifies suspension in UI
                    reason=f"Suspended: {error_msg}",
                )
                await TaskEventStore.append_event(
                    task.task_id,
                    "suspended",
                    {"reason": error_msg, "status": tracker.status()},
                )
                await LiveStatusReporter.broadcast_status(
                    task_id=task.task_id,
                    phase=task.description,
                    status="suspended",
                    message=f"Execution suspended: {error_msg}",
                    details={"blocked_reason": error_msg}
                )
                success = False
                break

            phase_name = f"Phase {idx+1}: {task.description}"
            log.info("GoalEngine: starting %s", phase_name)

            # Event-Driven Reconciliation Loop: Divergence Detection & Conflict Evaluation before state-write
            try:
                from sdk.agent.ghost_persistence import check_hash, pre_flight_reconciliation
                from python.harness.db import log_to_db
                import asyncio

                divergence = await asyncio.to_thread(check_hash)
                if divergence == "minor":
                    log.info("GoalEngine: Minor divergence detected (unreachable/missed heartbeat). Continuing with Ghost chain primary.")
                    await log_to_db(
                        level="WARNING",
                        message="Minor Divergence: Remote node missed heartbeat. Ghost chain remains primary.",
                        trace={"event_type": "Minor-Divergence", "status": "unreachable"}
                    )
                elif divergence == "major":
                    log.warning("GoalEngine: Major divergence detected (height/hash mismatch). Pausing task and triggering Automatic Sync Bridge.")
                    await log_to_db(
                        level="WARNING",
                        message="Major Divergence: Height/hash mismatch detected. Triggering Automatic Sync.",
                        trace={"event_type": "Major-Divergence"}
                    )
                    # Pause active task
                    await bq.transition_task(
                        task.task_id,
                        TaskStatus.PENDING,
                        TaskStatus.BLOCKED,
                        reason="Paused for Sync"
                    )
                    
                    # Execute sync_blockchain.py (Local -> Remote overwrite)
                    sync_success = await pre_flight_reconciliation()
                    
                    # Resume agent task (unblock regardless of remote VPS status to maintain offline-first execution)
                    if sync_success:
                        log.info("GoalEngine: Remote sync completed successfully.")
                    else:
                        log.warning("GoalEngine: Remote sync failed or timed out; unblocking for local-primary execution.")

                    try:
                        await bq.transition_task(
                            task.task_id,
                            TaskStatus.BLOCKED,
                            TaskStatus.PENDING,
                            reason="Sync completed or bypassed for local execution"
                        )
                    except Exception as tr_exc:
                        log.warning("GoalEngine: Transition from BLOCKED to PENDING failed: %s", tr_exc)
            except Exception as recon_exc:
                log.warning("GoalEngine: Divergence check/resolution error: %s", recon_exc)

            # Transition task status to in_progress
            await bq.transition_task(task.task_id, TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
            await TaskEventStore.append_event(task.task_id, "phase_start", {"phase": phase_name})
            await LiveStatusReporter.broadcast_status(
                task_id=task.task_id,
                phase=task.description,
                status="in_progress",
                message=f"Starting phase: {task.description}"
            )

            # Create checkpoint before executing phase
            checkpoint_id = None
            try:
                import asyncio
                checkpoint_id = await asyncio.to_thread(self.recovery.create_checkpoint)
                if checkpoint_id:
                    checkpoint_ids.append((task.task_id, checkpoint_id))
                    log.info("GoalEngine: created checkpoint %s", checkpoint_id)
                else:
                    log.warning(
                        "GoalEngine: no checkpoint created for phase %r — this phase "
                        "runs WITHOUT rollback protection.",
                        task.description,
                    )
            except Exception as exc:
                log.warning("GoalEngine: failed to create checkpoint: %s", exc)

            # 3. Dispatch task to dispatcher
            # We construct a PhasePlan to model this step
            expected_stdout = getattr(task, "payload", {}).get("expected_stdout")
            criteria = ["Execution succeeds with exit code 0"]
            if expected_stdout:
                criteria.append(f"Standard output MUST contain: '{expected_stdout}'")
                
            phase_plan = PhasePlan(
                name=task.description,
                steps=[task.description],
                acceptance_criteria=criteria,
                rollback_on_failure=True,
            )

            # We execute the step. Mutate task.payload["context"] directly --
            # DAGExecutionEngine.execute_task_as_dag reads payload straight off
            # the task object, not a local copy, so anything set here must land
            # there or it never actually reaches the DAG (a local `task_context`
            # copy used to be built here and silently discarded, unused).
            if not isinstance(task.payload, dict):
                task.payload = {}
            task_context = task.payload.setdefault("context", {})
            if not isinstance(task_context, dict):
                task_context = {}
                task.payload["context"] = task_context
            if self.is_fallback:
                task_context["is_fallback"] = True
            if phase_output_summaries:
                task_context["prior_phase_summary"] = _format_prior_phase_summary(phase_output_summaries)

            # Carry the compiled governance packet into the executing phase.
            #
            # `adapter_packet` was accepted as a parameter by this method and
            # then never read -- it appeared exactly once in this file, on the
            # signature. So AdapterCompiler faithfully merged forbidden_actions
            # (documented as an "absolute kill-switch"), tool affordances and
            # acceptance criteria from every activated node, handed the result
            # here, and it was dropped on the floor. Nothing has ever been
            # blocked by it.
            if adapter_packet is not None:
                forbidden = list(getattr(adapter_packet, "forbidden_actions", []) or [])
                affordances = list(getattr(adapter_packet, "tool_affordances", []) or [])
                if forbidden:
                    task_context["forbidden_actions"] = forbidden
                if affordances:
                    task_context["tool_affordances"] = affordances
                if forbidden or affordances:
                    log.info(
                        "GoalEngine: governance active -- %d forbidden, %d afforded",
                        len(forbidden), len(affordances),
                    )
            # Execute the step using the DAG-Engineered execution engine
            from sdk.agent.graph_engine import DAGExecutionEngine
            graph_engine = DAGExecutionEngine(self.root_dir)
            graph_res = await graph_engine.execute_task_as_dag(task, policy)
            
            res = {
                "command": task.description,
                "success": graph_res["success"],
                "output": graph_res.get("output"),
                "error": graph_res.get("error"),
                "stdout": graph_res.get("stdout", ""),
                "stderr": graph_res.get("stderr", ""),
            }
            
            # Increment steps taken in budget tracker
            tracker.tick()

            # 4. Verify outcome
            from sdk.agent.validator import PhaseValidator
            validator = PhaseValidator()
            verification_result = validator.validate(task, res)
            
            phase_success = verification_result.success
            res["success"] = verification_result.success
            res["output"] = verification_result.output
            res["error"] = verification_result.error

            if phase_success:
                log.info("GoalEngine: %s completed successfully", phase_name)
                
                # Check for dynamically generated subtasks in output
                import json
                output_val = res.get("output")
                dyn_subtasks = None
                
                if isinstance(output_val, dict):
                    dyn_subtasks = output_val.get("subtasks")
                elif isinstance(output_val, str):
                    try:
                        parsed = json.loads(output_val)
                        if isinstance(parsed, dict):
                            dyn_subtasks = parsed.get("subtasks")
                    except Exception:
                        pass
                
                if isinstance(dyn_subtasks, list) and dyn_subtasks:
                    log.info("GoalEngine: enqueuing %d dynamic subtasks from phase output", len(dyn_subtasks))
                    for sub_idx, sub_desc in enumerate(dyn_subtasks):
                        sub_id = generate_deterministic_id(sub_desc, f"dyn-sub-{task.task_id}-{sub_idx}")
                        deps = [task.task_id] if sub_idx == 0 else [generate_deterministic_id(dyn_subtasks[sub_idx-1], f"dyn-sub-{task.task_id}-{sub_idx-1}")]
                        
                        try:
                            await bq.enqueue_task(
                                sub_id,
                                sub_desc,
                                "pending",
                                parent_id=parent_task_id,
                                dependencies=deps,
                                payload={"priority": idx * 100 + sub_idx + 1}
                            )
                        except Exception as exc:
                            log.error("GoalEngine: failed to enqueue dynamic subtask: %s", exc)
                        
                        from sdk.agent.planner import TaskItem
                        dyn_item = TaskItem(description=sub_desc, dependencies=deps)
                        dyn_item.task_id = sub_id
                        tasks.insert(idx + 1 + sub_idx, dyn_item)

                await bq.transition_task(
                    task.task_id,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.COMPLETE,
                    result={"output": res.get("output")},
                )
                # Compact event bundle for AgentStatusAPI
                stdout_val = res.get("stdout", "") or res.get("output", "")
                stderr_val = res.get("stderr", "") or res.get("error", "")
                
                stdout_lines = stdout_val.splitlines() if isinstance(stdout_val, str) else []
                stderr_lines = stderr_val.splitlines() if isinstance(stderr_val, str) else []
                
                stdout_summary = stdout_lines[-10:] if stdout_lines else []
                stderr_summary = stderr_lines[-10:] if stderr_lines else []

                await TaskEventStore.append_event(
                    task.task_id,
                    "phase_complete",
                    {
                        "phase": phase_name,
                        "status": "complete",
                        "command": res.get("command", "None"),
                        "validator_result": phase_success,
                        "stdout_summary": stdout_summary,
                        "stderr_summary": stderr_summary,
                        "artifacts_touched": [], # Populated separately by artifact_generated events
                    },
                )
                await LiveStatusReporter.broadcast_status(
                    task_id=task.task_id,
                    phase=task.description,
                    status="complete",
                    message=f"Phase completed successfully: {task.description}",
                    details={
                        "validator_result": True,
                        "evidence": {
                            "stdout": "\n".join(stdout_summary),
                            "stderr": "\n".join(stderr_summary)
                        }
                    }
                )
                phases_completed.append(task.description)
                phase_output_summaries.append({
                    "description": task.description,
                    "output": res.get("output"),
                })

                # Commit to Ghost Blockchain
                try:
                    from sdk.agent.ghost_persistence import commit_to_ghost
                    import hashlib
                    context = {
                        "goal_text": goal_text,
                        "parent_task_id": parent_task_id,
                        "idx": idx,
                        "phase_retry_counts": phase_retry_counts,
                    }
                    var_state = {
                        "phases_completed": phases_completed,
                        "checkpoint_ids": checkpoint_ids,
                        "phase_output_summaries": phase_output_summaries,
                    }
                    next_step = tasks[idx + 1].description if idx + 1 < len(tasks) else "goal-complete"
                    next_step_hash = hashlib.sha256(next_step.encode("utf-8")).hexdigest()
                    
                    await commit_to_ghost(
                        task_id=task.task_id,
                        process_context=context,
                        variable_state=var_state,
                        intended_next_step_hash=next_step_hash,
                    )
                except Exception as commit_exc:
                    log.warning("GoalEngine: Commit-to-Ghost failed: %s", commit_exc)

                # Advance to next phase
                idx += 1
            else:
                log.warning("GoalEngine: %s failed. Error: %s", phase_name, res.get("error"))
                error_msg = res.get("error") or f"Phase failed: {task.description}"
                
                stdout_val = res.get("stdout", "") or res.get("output", "")
                stderr_val = res.get("stderr", "") or res.get("error", "")
                stdout_lines = stdout_val.splitlines() if isinstance(stdout_val, str) else []
                stderr_lines = stderr_val.splitlines() if isinstance(stderr_val, str) else []
                
                await TaskEventStore.append_event(
                    task.task_id,
                    "phase_complete",
                    {
                        "phase": phase_name,
                        "status": "failed",
                        "command": res.get("command", "None"),
                        "validator_result": phase_success,
                        "stdout_summary": stdout_lines[-10:] if stdout_lines else [],
                        "stderr_summary": stderr_lines[-10:] if stderr_lines else [],
                        "artifacts_touched": [],
                    },
                )
                
                # Rollback partial attempt before retrying or failing
                if checkpoint_id:
                    log.info("GoalEngine: rolling back to checkpoint %s", checkpoint_id)
                    try:
                        import asyncio
                        # Restore the checkpoint THIS phase created. The
                        # no-argument call restored whatever sat under the
                        # default name "checkpoint" -- which could be, and on
                        # 2026-08-11 was, a snapshot days older than the run.
                        # Report what actually happened. restore_checkpoint
                        # returns False when the snapshot is missing or empty,
                        # and that False used to be discarded -- so a rollback
                        # that restored nothing still announced itself as a
                        # completed rollback in the log and the telemetry feed.
                        restored = await asyncio.to_thread(
                            self.recovery.restore_checkpoint, checkpoint_id
                        )
                        await TaskEventStore.append_event(
                            task.task_id,
                            "phase_rollback",
                            {
                                "phase": phase_name,
                                "checkpoint_id": checkpoint_id,
                                "restored": bool(restored),
                            },
                        )
                        if restored:
                            message = (
                                f"Rolled back phase {task.description} "
                                f"to checkpoint {checkpoint_id}"
                            )
                        else:
                            log.error(
                                "GoalEngine: rollback of phase %r did NOT restore "
                                "anything (checkpoint %s missing or empty); the "
                                "workspace still holds this phase's partial edits.",
                                task.description, checkpoint_id,
                            )
                            message = (
                                f"Rollback FAILED for phase {task.description}: "
                                f"checkpoint {checkpoint_id} restored nothing — "
                                "partial edits remain in the workspace"
                            )
                        await LiveStatusReporter.broadcast_status(
                            task_id=task.task_id,
                            phase=task.description,
                            status="rollback",
                            message=message,
                        )
                    except Exception as rollback_exc:
                        log.error("GoalEngine: rollback failed: %s", rollback_exc)
                
                lineage_id = task.payload.get("lineage_id", task.task_id)
                retries = phase_retry_counts.get(lineage_id, 0)
                if retries < 3:
                    retries += 1
                    phase_retry_counts[lineage_id] = retries
                    log.info("GoalEngine: Re-Planning phase %s (Retry %d/3)", phase_name, retries)
                    await LiveStatusReporter.broadcast_status(
                        task_id=task.task_id,
                        phase=task.description,
                        status="in_progress",
                        message=f"Re-Planning fix for error (Retry {retries}/3)..."
                    )
                    
                    # Always re-plan against the ORIGINAL task, never against a
                    # previous re-plan's echo of this same prompt. See
                    # strip_replan_wrapper: without this the phase description
                    # accumulates a layer of scaffolding per retry.
                    original_description = strip_replan_wrapper(task.description)

                    # Generate explicit repair policy rules via engine
                    repair_context = RepairContext(
                        task_description=original_description,
                        error_msg=str(error_msg),
                        payload=getattr(task, "payload", {})
                    )
                    intent_instruction = RepairPolicyEngine.generate_instruction(repair_context)

                    replanner_prompt = (
                        f"You were trying to accomplish the following task: '{original_description}'. "
                        f"However, the execution failed with the following error: '{error_msg}'. "
                        "Please generate a single, concrete repair action that circumvents the problem. "
                        "Do NOT generate tasks to 'analyze', 'review', or 'determine the root cause'. "
                        "Focus strictly on a direct alternative or workaround to accomplish the original task. "
                        f"\n{intent_instruction}"
                    )
                    
                    try:
                        fix_tasks = await decomposer.decompose(replanner_prompt, bypass_cache=True)
                        if fix_tasks:
                            fix_task = fix_tasks[0]
                            # The decomposer is handed the whole replanner
                            # prompt as its goal, and the model routinely echoes
                            # the prompt's opening back as the task description.
                            # Left alone, that echo becomes the phase name, the
                            # next retry's input, and eventually a wiki node
                            # title. Strip it here, at the one point where a
                            # re-planned description enters the task graph.
                            fix_task.description = strip_replan_wrapper(fix_task.description)
                            fix_id = generate_deterministic_id(fix_task.description, f"fix-{task.task_id}-{retries}")
                            fix_task.task_id = fix_id
                            
                            fix_task.payload["intentional_runtime_error"] = repair_context.intentional_runtime_error
                            fix_task.payload["intentional_syntax_error"] = repair_context.intentional_syntax_error
                            if expected_stdout:
                                fix_task.payload["expected_stdout"] = expected_stdout
                            
                            # Propagate the repair policy to the fix task lineage
                            if task.payload.get("repair_policy"):
                                fix_task.payload["repair_policy"] = task.payload.get("repair_policy")
                            
                            fix_task.payload["lineage_id"] = lineage_id
                            
                            await bq.enqueue_task(
                                fix_id,
                                fix_task.description,
                                "pending",
                                parent_id=parent_task_id,
                                payload={"priority": idx * 100 + 50}
                            )
                            
                            # Insert the fix task exactly at current index so it executes immediately next
                            tasks.insert(idx, fix_task)
                            # We deliberately do NOT advance idx, so the fix task runs next.
                            continue
                    except Exception as exc:
                        log.error("GoalEngine: Replanner decomposition failed: %s", exc)

                # If we exhausted retries or decomposer failed to generate a fix
                success = False
                await LiveStatusReporter.broadcast_status(
                    task_id=task.task_id,
                    phase=task.description,
                    status="failed",
                    message=f"Phase failed after retries: {error_msg}",
                    details={
                        "validator_result": False,
                        "evidence": {
                            "error": str(error_msg),
                            "stdout": "\n".join(stdout_lines[-10:] if stdout_lines else [])
                        }
                    }
                )

                await bq.transition_task(
                    task.task_id,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.FAILED,
                    reason=error_msg,
                )

                # Break out of the execution loop
                break

        # Transition parent task status
        final_status = TaskStatus.COMPLETE if success else (TaskStatus.BLOCKED if suspended else TaskStatus.FAILED)
        await bq.transition_task(
            parent_task_id,
            TaskStatus.IN_PROGRESS,
            final_status,
            reason=error_msg,
            result={"phases_completed": phases_completed},
        )

        # 6. Produce report
        reporter = CompletionReporter(parent_task_id)
        report = ""
        try:
            report = await reporter.generate_report()
        except Exception as exc:
            log.warning("GoalEngine: failed to generate report: %s", exc)

        # 7. Auto-document skill node (Hermes feature — fire-and-forget)
        clean_success = success and len(phase_retry_counts) == 0
        
        workaround_terms = ["workaround", "hack", "bypass", "temporary", "skip", "override", "hardcode"]
        has_workaround_lang = any(
            any(term in text.lower() for term in workaround_terms)
            for text in [goal_text] + phases_completed
        )
        
        is_reusable = len(phases_completed) >= policy.min_skill_phases

        if clean_success and not has_workaround_lang and is_reusable:
            log.info(
                "GoalEngine: auto-documenting skill node for %d-phase goal (threshold=%d)",
                len(phases_completed), policy.min_skill_phases,
            )
            await self._auto_document_skill(goal_text, phases_completed, parent_task_id)
        else:
            log.info(
                "GoalEngine: skipping auto-documentation. clean_success=%s, has_workaround=%s, is_reusable=%s",
                clean_success, has_workaround_lang, is_reusable
            )

        return {
            "success": success,
            "suspended": suspended,
            "task_id": parent_task_id,
            "phases_completed": phases_completed,
            "error": error_msg,
            "report": report,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _auto_document_skill(
        self,
        goal_text: str,
        phases_completed: list[str],
        task_id: str,
    ) -> None:
        """
        Auto-extract a *skill* HTML Neural Node from a completed goal and
        register it in the PostgreSQL ``nodes`` table so it surfaces in
        semantic search and context injection.

        This is intentionally fire-and-forget: any error is logged and
        swallowed.  A documentation failure must never mark the goal as
        failed.
        """
        try:
            # 1. Synthesize python code
            from sdk.agent.skill_synthesis import synthesize_python_skill
            executable_code = await synthesize_python_skill(goal_text, phases_completed)
            
            # 2. Document to HTML Neural Node
            documenter = SelfDocumenter(self.root_dir)
            node_paths = documenter.document_completed_goal(
                goal_text=goal_text,
                phases_completed=phases_completed,
                task_id=task_id,
                executable_code=executable_code,
            )
            node_path = node_paths.get("skill_node_path")
            learning_node_path = node_paths.get("learning_node_path")

            # Trigger JIT LoRA weight compilation and HTML-NN serialization
            for p_val in (node_path, learning_node_path):
                if p_val:
                    try:
                        from sdk.agent.tools import compile_lora_adapter
                        await compile_lora_adapter(p_val, task_id)
                        log.info("GoalEngine: JIT LoRA weights compiled and serialized to %s", p_val)
                    except Exception as compile_exc:
                        log.warning("GoalEngine: failed to compile and serialize LoRA weights: %s", compile_exc)

            try:
                from python.harness.memory import handle_memory_reindex_node
                if node_path:
                    await handle_memory_reindex_node({"node_path": node_path})
                if learning_node_path:
                    await handle_memory_reindex_node({"node_path": learning_node_path})
            except Exception as reindex_exc:
                log.warning("GoalEngine: failed to reindex documentation nodes: %s", reindex_exc)

            try:
                from sdk.agent.persistence import TaskEventStore
                await TaskEventStore.append_event(
                    task_id,
                    "artifact_generated",
                    {"type": "skill_node", "path": node_path, "learning_node_path": learning_node_path}
                )
            except Exception as e:
                log.warning("GoalEngine: failed to log artifact event: %s", e)

            # Register the skill node in PostgreSQL so activation.py can find it
            try:
                import json as _json
                from python.harness import db

                content_json = _json.dumps({
                    "goal": goal_text,
                    "phases": phases_completed,
                    "task_id": task_id,
                    "node_path": node_path,
                    "learning_node_path": learning_node_path,
                })
                await db.execute(
                    """
                    INSERT INTO nodes (node_type, execution_authority, content_json)
                    VALUES ('skill', 'agent', $1::jsonb)
                    """,
                    content_json,
                    caller="GoalEngine._auto_document_skill",
                )
                log.info(
                    "GoalEngine: skill node registered in DB for goal=%r",
                    goal_text[:60],
                )
            except Exception as db_exc:
                # DB unavailable or not initialised (e.g. unit-test context) — OK
                log.debug(
                    "GoalEngine: could not register skill node in DB: %s", db_exc
                )

            # Append to agent_log for operator visibility
            try:
                from python.harness import db as _db
                await _db.log_to_db(
                    "INFO",
                    f"Skill node auto-saved: {node_path}",
                    {"goal": goal_text[:200], "task_id": task_id, "phases": len(phases_completed)},
                )
            except Exception:
                pass

            # Force auto-discovery to pick up the new node
            try:
                from sdk.agent.tools import ToolRegistry
                ToolRegistry.auto_discover_skills()
            except Exception as tr_exc:
                log.warning("GoalEngine: failed to auto-discover new skill: %s", tr_exc)

        except Exception as exc:
            log.warning(
                "GoalEngine: auto-skill documentation failed (non-fatal): %s", exc
            )

    def should_i_adjust(self, feedback: str, usage_count: int, is_recurring: bool) -> bool:
        """
        NAME
               should_i_adjust - Gating logic for critique self-correction
               
        SYNOPSIS
               needs_adjust = should_i_adjust(feedback, usage_count, is_recurring)
               
        DESCRIPTION
               Determines if the critique is significant enough (severity > 0.4)
               or recurring to trigger a behavioral update.
        """
        import re
        lower_feedback = feedback.lower()
        neg_keywords = ["bad", "fail", "wrong", "cautious", "error", "unacceptable", "terrible", "poor", "issue", "misaligned", "dislike", "cautious"]
        match_count = sum(1 for kw in neg_keywords if kw in lower_feedback)
        severity = min(1.0, float(match_count) * 0.25)
        
        error_index = severity * (1.0 - 1.0 / (usage_count + 1))
        
        if is_recurring or error_index > 0.4:
            return True
        return False

    async def process_feedback(
        self,
        feedback: str,
        node_path: str,
        usage_count: int = 1,
        is_recurring: bool = False
    ) -> str:
        """
        NAME
               process_feedback - Process user feedback and self-correct weights
               
        SYNOPSIS
               result = await process_feedback(feedback, node_path, usage_count, is_recurring)
               
        DESCRIPTION
               Gates critique adjustments. If it meets criteria, runs adjust_behavioral_weights.
               Otherwise logs a Non-Critical-Event to the Ghost Ledger.
        """
        if self.should_i_adjust(feedback, usage_count, is_recurring):
            log.info("GoalEngine: Feedback meets self-correction gate. Triggering weight shift.")
            from sdk.agent.tools import adjust_behavioral_weights
            res = await adjust_behavioral_weights(node_path, feedback)
            return res
        else:
            log.info("GoalEngine: Feedback classified as outlier / low-severity. Logging Non-Critical-Event.")
            from sdk.agent.anchor import canonicalize_and_anchor
            from datetime import datetime, UTC
            event_data = {
                "timestamp": datetime.now(UTC).isoformat(),
                "event_type": "Non-Critical-Event",
                "feedback": feedback,
                "node_path": node_path,
                "reasoning": "Feedback did not meet pattern recurrence or severity threshold."
            }
            anchor_id = f"non_critical_{int(datetime.now(UTC).timestamp())}"
            anchor_html = f"<div>Non-Critical Feedback Event logged</div>"
            canonicalize_and_anchor(
                node_id=anchor_id,
                content_html=anchor_html,
                state_data=event_data,
                document_type="non-critical-event",
                creator="tala-feedback-auditor"
            )
            return "Feedback processed and logged as a Non-Critical-Event."

    async def check_dream_trigger(self, N_minutes: float = 5.0) -> bool:
        """
        NAME
               check_dream_trigger - Verify if Dream trigger conditions are met
               
        SYNOPSIS
               is_idle = await check_dream_trigger(N_minutes)
               
        DESCRIPTION
               Verifies if self.active_adapter_buffer is empty, there are no active
               tasks (pending or in progress) in the database, and the last task update
               was more than N_minutes ago.
        """
        if self.active_adapter_buffer is not None:
            return False
            
        from python.harness import db
        if db.get_pool() is not None:
            try:
                active_rows = await db.fetch(
                    "SELECT id, updated_at FROM tasks WHERE status IN ('pending', 'in_progress')"
                )
                if active_rows:
                    return False
                
                row = await db.fetchrow("SELECT MAX(updated_at) as last_update FROM tasks")
                if row and row["last_update"]:
                    from datetime import datetime, UTC
                    delta = datetime.now(UTC) - row["last_update"]
                    if (delta.total_seconds() / 60.0) < N_minutes:
                        return False
            except Exception:
                pass
                
        return True

    def dream_report_filter(self, dream_changes: list[dict[str, Any]]) -> bool:
        """
        NAME
               dream_report_filter - Decides if a dream consolidation should be reported
               
        SYNOPSIS
               should_report = dream_report_filter(dream_changes)
               
        DESCRIPTION
               Returns True if any change is on a Core node or has risk_delta > 0.6.
        """
        for change in dream_changes:
            node_id = change.get("node_id", "")
            risk_delta = change.get("risk_delta", 0.0)
            is_core = change.get("node_type") == "Core"
            if risk_delta > 0.6 or is_core:
                return True
        return False

    def generate_dream_journal_entry(
        self,
        dream_changes: list[dict[str, Any]],
        status: str,
        reasoning: str
    ) -> str:
        """
        NAME
               generate_dream_journal_entry - Generate concise A2UI commentary
               
        SYNOPSIS
               entry = generate_dream_journal_entry(dream_changes, status, reasoning)
               
        DESCRIPTION
               Constructs a formatted commentary log for the A2UI showing status,
               decision reported/silent status, reasoning, and security circuit-breakers.
        """
        from datetime import datetime
        date_str = datetime.now().strftime("%m/%d/%Y")
        
        reported = "REPORTED" if self.dream_report_filter(dream_changes) else "SILENT"
        
        changes_summary = []
        for c in dream_changes:
            node_id = c.get("node_id", "")
            shifts = c.get("shifts", {})
            shifts_str = ", ".join(f"{k}: {v}" for k, v in shifts.items())
            changes_summary.append(f"{node_id} ({shifts_str})")
            
        changes_joined = "; ".join(changes_summary) if changes_summary else "No behavioral shifts."
        
        entry = f"""[Dream Journal Entry: {date_str}]
Status: {status}.
Decision: [{reported}]
Reasoning: "{reasoning} Changes: {changes_joined}"
Note: I have implemented a 'Circuit-Breaker' to prevent this risk-increase from affecting the blockchain-sync logic."""
        return entry

    async def dream_cycle(self) -> str:
        """
        NAME
               dream_cycle - Programmatic Experience Consolidation
               
        SYNOPSIS
               log_summary = await dream_cycle()
               
        DESCRIPTION
               Tala enters the Dream state: extracts critique logs from local
               ledger, runs pattern synthesis across nodes, self-corrects manifests,
               re-compiles weights, and registers a Dream-Log transaction.
        """
        log.info("Tala is entering the Dream state...")
        
        import json
        import re
        from pathlib import Path
        from datetime import datetime, UTC
        
        repo_root = Path(self.root_dir or os.getcwd())
        chain_file = repo_root / "blockchain" / "data" / "chain.json"
        
        feedbacks_by_node = {}
        
        if chain_file.exists():
            try:
                with open(chain_file, "r", encoding="utf-8") as f:
                    blocks = json.load(f)
                for block in blocks:
                    for tx in block.get("transactions", []):
                        if tx.get("type") in ("behavioral-learning", "non-critical-event", "node-create"):
                            doc = tx.get("document", {})
                            state_data = tx.get("state_data")
                            if not state_data and doc.get("encrypted") is False:
                                state_data = doc.get("state_data")
                            
                            if isinstance(state_data, str):
                                try:
                                    state_data = json.loads(state_data)
                                except Exception:
                                    pass
                                    
                            if isinstance(state_data, dict):
                                node_id = state_data.get("node_id") or state_data.get("node_path")
                                feedback = state_data.get("feedback_critique") or state_data.get("feedback")
                                if node_id and feedback:
                                    node_id = Path(node_id).stem
                                    feedbacks_by_node.setdefault(node_id, []).append(feedback)
            except Exception as exc:
                log.warning("GoalEngine: failed to extract dream experience: %s", exc)
                
        refactored_nodes = []
        dream_changes = []
        nodes_dir = repo_root / "nodes"
        
        for node_id, feedbacks in feedbacks_by_node.items():
            node_file = nodes_dir / f"{node_id}.html"
            if node_file.exists():
                synthesis = " ".join(feedbacks)
                if any(kw in synthesis.lower() for kw in ("caution", "cautious", "relevance", "accurate")):
                    try:
                        # Save old state for rollback recovery
                        old_content = node_file.read_text(encoding="utf-8")
                        
                        # Read old caution value
                        old_caution = 0.5
                        old_metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', old_content, re.DOTALL)
                        if old_metadata_m:
                            try:
                                old_data = json.loads(old_metadata_m.group(1).strip())
                                old_caution = old_data.get("emotional_valence", {}).get("caution", 0.5)
                            except Exception:
                                pass
                                
                        from sdk.agent.tools import adjust_behavioral_weights
                        res = await adjust_behavioral_weights(str(node_file), synthesis)
                        
                        # Read new caution value
                        new_content = node_file.read_text(encoding="utf-8")
                        new_caution = 0.5
                        new_metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', new_content, re.DOTALL)
                        shift_details = {}
                        if new_metadata_m:
                            try:
                                new_data = json.loads(new_metadata_m.group(1).strip())
                                new_caution = new_data.get("emotional_valence", {}).get("caution", 0.5)
                                relevance = new_data.get("relevance_weight", 1.0)
                                shift_details["caution"] = f"{old_caution:.2f} -> {new_caution:.2f}"
                            except Exception:
                                pass
                                
                        risk_delta = abs(old_caution - new_caution)
                        node_type = "Core" if (node_id.startswith("system-") or node_id.startswith("program-doc-")) else "Standard"
                        
                        # Risk Assessment Circuit Breaker check (Tier 3 Break simulation)
                        is_break = False
                        break_reason = ""
                        if node_type == "Core" or "safety" in node_id or "vault" in node_id:
                            if new_caution < 0.3:
                                is_break = True
                                break_reason = f"Caution value {new_caution:.2f} is below safety threshold 0.3 on core safety node."
                                
                        if is_break:
                            log.error("[ALERT] Catastrophic Failure simulated during Dream State on node %s: %s. Initiating Dream-Rollback.", node_id, break_reason)
                            node_file.write_text(old_content, encoding="utf-8")
                            from sdk.agent.tools import compile_lora_adapter
                            await compile_lora_adapter(str(node_file))
                            
                            from sdk.agent.anchor import canonicalize_and_anchor
                            rollback_data = {
                                "timestamp": datetime.now(UTC).isoformat(),
                                "event_type": "Dream-Rollback-Alert",
                                "node_id": node_id,
                                "break_reason": break_reason,
                                "reverted_to_caution": old_caution
                            }
                            try:
                                canonicalize_and_anchor(
                                    node_id=f"rollback_{node_id}_{int(datetime.now(UTC).timestamp())}",
                                    content_html=f"<div>[ALERT] Dream-Rollback on Node {node_id} due to safety violation: {break_reason}</div>",
                                    state_data=rollback_data,
                                    document_type="dream-rollback",
                                    creator="tala-dream-controller"
                                )
                            except Exception:
                                pass
                            continue
                            
                        refactored_nodes.append(f"{node_id} (Critique count: {len(feedbacks)})")
                        dream_changes.append({
                            "node_id": node_id,
                            "node_type": node_type,
                            "risk_delta": risk_delta,
                            "shifts": shift_details
                        })
                    except Exception as adjust_exc:
                        log.warning("GoalEngine: dream weight adjustment failed for %s: %s", node_id, adjust_exc)
                        
        summary_log = f"Refactored {len(refactored_nodes)} nodes based on feedback: {', '.join(refactored_nodes)}" if refactored_nodes else "No nodes required optimization during this consolidation cycle."
        
        # Determine if we should generate/share a Dream Journal Entry
        reported = self.dream_report_filter(dream_changes)
        journal_entry = ""
        if dream_changes:
            status = "Consolidated"
            reasoning = "I have recalibrated the cautious weight in node manifests based on user critiques."
            journal_entry = self.generate_dream_journal_entry(dream_changes, status, reasoning)
            if reported:
                log.warning("\n*** A2UI NOTIFICATION (High-Risk Evolution) ***\n%s\n************************************************\n", journal_entry)
            else:
                log.info("\n*** Silent Dream Journal Logged ***\n%s\n***********************************\n", journal_entry)
                
        from sdk.agent.anchor import canonicalize_and_anchor
        dream_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": "Dream-Log",
            "refactored_nodes": refactored_nodes,
            "dream_changes": dream_changes,
            "dream_journal": journal_entry,
            "reported": reported,
            "summary": summary_log
        }
        
        dream_id = f"dream_log_{int(datetime.now(UTC).timestamp())}"
        dream_html = f"<div>Dream-Log: {summary_log}</div>"
        
        try:
            core_hash, chaincode_id, anchoring_status, _, _ = canonicalize_and_anchor(
                node_id=dream_id,
                content_html=dream_html,
                state_data=dream_data,
                document_type="dream-log",
                creator="tala-dream-controller"
            )
            log.info("GoalEngine: Dream cycle complete. Chaincode ID: %s", chaincode_id)
        except Exception as anchor_exc:
            log.warning("GoalEngine: failed to anchor dream log: %s", anchor_exc)
            
        return summary_log


def infer_emotional_state(query: str) -> dict[str, float]:
    """
    NAME
           infer_emotional_state - Infer emotional valence state from text query
           
    SYNOPSIS
           state = infer_emotional_state(query)
           
    DESCRIPTION
           Analyzes keywords to return emotional scores for joy, caution, curiosity,
           and neutral.
    """
    import re
    lower_query = query.lower()
    state = {
        "joy": 0.1,
        "caution": 0.1,
        "curiosity": 0.1,
        "neutral": 0.1
    }
    
    words = set(re.findall(r'\w+', lower_query))
    
    curiosity_kws = {"how", "why", "what", "query", "explain", "detail", "understand", "explore", "search"}
    caution_kws = {"safe", "secure", "gating", "block", "divergence", "prevent", "lock", "restrict", "auth"}
    joy_kws = {"success", "complete", "passed", "happy", "win", "good", "great", "done"}
    
    if any(kw in words for kw in curiosity_kws):
        state["curiosity"] = 0.9
    if any(kw in words for kw in caution_kws):
        state["caution"] = 0.9
    if any(kw in words for kw in joy_kws):
        state["joy"] = 0.9
        
    if all(state[k] == 0.1 for k in ["curiosity", "caution", "joy"]):
        state["neutral"] = 0.9
        
    return state


class AdapterSelector:
    """
    NAME
           AdapterSelector - Metadata-aware neural adapter selector
           
    SYNOPSIS
           selector = AdapterSelector(nodes_dir)
           best_adapter = selector.select_adapter(query)
           
    DESCRIPTION
           Parses JSON-LD metadata and ranks nodes based on semantic, emotional,
           and authority metrics.
    """
    def __init__(self, nodes_dir: str | None = None) -> None:
        from pathlib import Path
        self.nodes_dir = Path(nodes_dir or os.path.join(os.getcwd(), "nodes"))

    def select_adapter(self, query: str) -> dict[str, Any] | None:
        import re
        import json
        
        if not self.nodes_dir.exists():
            return None
            
        html_files = list(self.nodes_dir.glob("*.html"))
        if not html_files:
            return None
            
        query_state = infer_emotional_state(query)
        query_words = set(re.findall(r'\w+', query.lower()))
        
        ranked_nodes = []
        
        for f in html_files:
            try:
                content = f.read_text(encoding="utf-8")
                
                metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content, re.DOTALL)
                if not metadata_m:
                    continue
                    
                data = json.loads(metadata_m.group(1).strip())
                semantic_tags = data.get("semantic_tags", [])
                emotional_valence = data.get("emotional_valence", {})
                relevance_weight = float(data.get("relevance_weight", 1.0))
                usage_count = int(data.get("usage_count", 0))
                
                # Jaccard Semantic Similarity
                tag_words = set(tag.lower() for tag in semantic_tags)
                intersection = query_words.intersection(tag_words)
                union = query_words.union(tag_words)
                similarity = len(intersection) / len(union) if union else 0.0
                
                # Emotional Alignment
                alignment = sum(query_state[k] * emotional_valence.get(k, 0.0) for k in ["joy", "caution", "curiosity", "neutral"])
                
                # Authority Weight (Usage Citation)
                usage_citation = usage_count / (usage_count + 1)
                
                # Ranking score: S = 0.5 * Similarity + 0.3 * Alignment + 0.2 * Usage_Citation
                score = (0.5 * similarity) + (0.3 * alignment) + (0.2 * usage_citation)
                
                weights_payload = None
                weights_m = re.search(r'<script[^>]*id="lora-weights"[^>]*>(.*?)</script>', content, re.DOTALL)
                if weights_m:
                    weights_payload = weights_m.group(1).strip()
                    
                ranked_nodes.append({
                    "file_path": str(f),
                    "node_id": f.stem,
                    "score": score,
                    "payload": weights_payload,
                    "metadata": data
                })
            except Exception as exc:
                log.warning("AdapterSelector: failed to parse node %s: %s", f.name, exc)
                
        if not ranked_nodes:
            return None
            
        ranked_nodes.sort(key=lambda x: x["score"], reverse=True)
        return ranked_nodes[0]

