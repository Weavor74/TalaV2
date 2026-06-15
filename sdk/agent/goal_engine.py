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


class GoalEngine:
    """
    Orchestrates the decomposition, checkpointing, dispatching, verification,
    rollback, and reporting of multi-phase goals.
    """

    def __init__(self, root_dir: str | None = None) -> None:
        self.root_dir = root_dir or os.getcwd()
        self.dispatcher = TalaExecutionDispatcher()
        self.recovery = RollbackRecovery(self.root_dir)

    async def run_goal(self, goal_text: str, policy: BatchPolicy | None = None, adapter_packet: "AdapterPacket | None" = None) -> dict[str, Any]:
        """
        Decompose a high-level goal, execute it phase-by-phase with checkpoints,
        and verify completion.
        """
        log.info("GoalEngine: starting goal=%r with policy=%s", goal_text, policy)
        
        from sdk.agent.batch_policy import BatchPolicy, BudgetTracker
        policy = policy or BatchPolicy.auto()
        tracker = BudgetTracker(policy)

        # 1. Decompose goal
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
                await bq.enqueue_task(
                    task.task_id,
                    task.description,
                    "pending",
                    parent_id=parent_task_id,
                    payload={"dependencies": task.dependencies, "priority": idx},
                )
        except Exception as exc:
            log.error("GoalEngine: database enqueuing failed: %s", exc)

        log.info("GoalEngine: decomposed into %d phase(s)", len(tasks))

        phases_completed = []
        checkpoint_ids = []
        success = True
        error_msg = None
        suspended = False

        # 2. Iterate through phases
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
                checkpoint_ids.append((task.task_id, checkpoint_id))
                log.info("GoalEngine: created checkpoint %s", checkpoint_id)
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

            # We execute the step
            res = await self.dispatcher.dispatch(
                task.description,
                self.root_dir,
                context=getattr(task, "payload", {}).get("context", {}),
                authority_level=policy.authority_level,
                adapter_packet=adapter_packet,
            )
            
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
                        await asyncio.to_thread(self.recovery.restore_checkpoint)
                        await TaskEventStore.append_event(
                            task.task_id,
                            "phase_rollback",
                            {"phase": phase_name, "checkpoint_id": checkpoint_id},
                        )
                        await LiveStatusReporter.broadcast_status(
                            task_id=task.task_id,
                            phase=task.description,
                            status="rollback",
                            message=f"Rolled back phase {task.description} to checkpoint {checkpoint_id}"
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
                    
                    # Generate explicit repair policy rules via engine
                    repair_context = RepairContext(
                        task_description=task.description,
                        error_msg=str(error_msg),
                        payload=getattr(task, "payload", {})
                    )
                    intent_instruction = RepairPolicyEngine.generate_instruction(repair_context)
                    
                    replanner_prompt = (
                        f"You were trying to accomplish the following task: '{task.description}'. "
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
            executable_code = await self._synthesize_python_skill(goal_text, phases_completed)
            
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

    async def _synthesize_python_skill(self, goal_text: str, phases_completed: list[str]) -> str | None:
        """
        Synthesize an executable Python skill from a successful goal trajectory.
        Uses the LLM to write an async function with @register_tool and returns the code string.
        """
        try:
            from sdk.agent.llm_coder import LLMCodeGenerator
            from pathlib import Path
            import os
            import re
            
            coder = LLMCodeGenerator()
            
            # Format the phases to help the LLM understand what to automate
            trajectory = "\n".join(f"{i+1}. {phase}" for i, phase in enumerate(phases_completed))
            
            prompt = f"""You are the Tala Skill Synthesizer.
A complex task was just successfully completed. Your job is to convert this trajectory into a reusable Python tool.

Goal that was solved:
{goal_text}

Steps taken to solve it:
{trajectory}

Write a standalone async Python function using the `@register_tool(name="...", description="...")` decorator from `sdk.agent.tools`.
The function should automate the core intent of the goal in a generalized way, parameterizing inputs where appropriate.

Requirements:
- Only output the raw python code (wrapped in a markdown python code block).
- Import `register_tool` using `from sdk.agent.tools import register_tool`.
- Return a `dict[str, Any]` with status, error, and relevant result data.
- Catch exceptions and return them gracefully.
- Include a descriptive docstring.
- Ensure the name is snake_case and descriptive.

Example format:
```python
import typing
from typing import Any
from sdk.agent.tools import register_tool

@register_tool(name="example_skill", description="Does something useful")
async def example_skill(input_text: str) -> dict[str, Any]:
    try:
        # Implementation
        return {{"status": "success", "result": "..."}}
    except Exception as e:
        return {{"status": "error", "error": str(e)}}
```
"""
            log.info("GoalEngine: Prompting LLM to synthesize Python skill...")
            code_response = await coder._call_llm(prompt, language="python")
            if not code_response:
                log.warning("GoalEngine: LLM failed to return synthesized skill code.")
                return

            clean_code = LLMCodeGenerator._extract_code(code_response, "python")

            # Validate tool name extraction
            match = re.search(r'name=["\']([a-zA-Z0-9_]+)["\']', clean_code)
            if not match:
                log.warning("GoalEngine: Could not extract skill name from generated code.")
                return None
                
            log.info("GoalEngine: Successfully synthesized skill code for: %s", match.group(1).lower())
            return clean_code
            
        except Exception as e:
            log.warning("GoalEngine: failed to synthesize python skill: %s", e)
            return None
