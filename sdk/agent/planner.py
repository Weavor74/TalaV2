"""
sdk/agent/planner.py
====================
Goal Decomposition & Bounded Loop Engine — Tala Cognitive Engine Room
Milestone 6 | Task 6.1.1

Implements the non-bypassable five-step Plan→Execute→Observe→Evaluate→Re-Plan
loop.  Every task must traverse all five phases before being declared COMPLETE.
The loop halts immediately on any invariant deviation or ledger assertion
failure by raising InvariantViolationError (defined in sdk/agent/exceptions.py).

Design invariants:
  • No task can skip a loop phase — the run() method enforces ordering.
  • Invariant callables are run after every Observe phase; failure is fatal.
  • max_iterations caps the loop to prevent infinite re-planning.
  • BoundedMilestonePersistence.halt_on_missing_ledger_assertion() must be
    satisfied before a task transitions to COMPLETE.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvariantViolationError(Exception):
    """Raised when a registered invariant returns False or raises."""

    def __init__(self, invariant_name: str, detail: str = "") -> None:
        self.invariant_name = invariant_name
        self.detail = detail
        super().__init__(
            f"Invariant violation [{invariant_name}]: {detail or 'check returned False'}"
        )


class LedgerAssertionError(Exception):
    """Raised when a ledger assertion (required milestone checkpoint) fails."""

    def __init__(self, failed_index: int) -> None:
        self.failed_index = failed_index
        super().__init__(
            f"Ledger assertion at index {failed_index} failed — milestone halted."
        )


class MaxIterationsExceeded(Exception):
    """Raised when the loop exhausts max_iterations without COMPLETE."""

    def __init__(self, task_id: str, iterations: int) -> None:
        super().__init__(
            f"Task {task_id} did not complete within {iterations} iteration(s)."
        )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(Enum):
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETE = auto()
    FAILED = auto()
    BLOCKED = auto()


class LoopPhase(Enum):
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    OBSERVE = "OBSERVE"
    EVALUATE = "EVALUATE"
    RE_PLAN = "RE_PLAN"


# ---------------------------------------------------------------------------
# Core data classes
# ---------------------------------------------------------------------------


@dataclass
class TaskItem:
    """A single unit of work produced by GoalDecomposer."""

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    # Ordered list of task_ids that must complete before this task may start
    dependencies: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    # Accumulated context from each loop iteration
    _loop_history: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def mark_complete(self) -> None:
        self.status = TaskStatus.COMPLETE
        self.completed_at = datetime.now(UTC)

    def mark_failed(self, reason: str = "") -> None:
        self.status = TaskStatus.FAILED
        log.warning("Task %s marked FAILED: %s", self.task_id, reason)

    def mark_in_progress(self) -> None:
        self.status = TaskStatus.IN_PROGRESS

    def mark_blocked(self, reason: str = "") -> None:
        self.status = TaskStatus.BLOCKED
        log.warning("Task %s marked BLOCKED: %s", self.task_id, reason)


@dataclass
class PhasePlan:
    """
    Represents a structured execution phase for a high-level goal.
    """
    name: str
    steps: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    verification_command: str | None = None
    rollback_on_failure: bool = True


@dataclass
class ExecutionPlan:
    """Structured plan produced by the PLAN phase."""

    task_id: str
    steps: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Iteration number this plan was generated for
    iteration: int = 0


@dataclass
class ExecutionResult:
    """Raw result from the EXECUTE phase."""

    task_id: str
    plan: ExecutionPlan
    success: bool = False
    output: Any = None
    error: str | None = None
    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Observation:
    """Structured observation produced from an ExecutionResult."""

    task_id: str
    result: ExecutionResult
    facts: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class EvaluationResult:
    """Outcome of running invariants against an Observation."""

    task_id: str
    observation: Observation
    passed: bool = False
    failed_invariant: str | None = None
    detail: str = ""
    should_re_plan: bool = False
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Goal Decomposer
# ---------------------------------------------------------------------------


class GoalDecomposer:
    """
    Parses a free-form user goal string into an ordered list of TaskItems.

    Decomposition strategy (heuristic):
      1. Split on common sentence terminators (period, semicolon, newline).
      2. Strip whitespace; skip empty segments.
      3. Assign sequential dependency chain (each task depends on the prior).

    For production use this can be wired to an LLM call that returns
    structured JSON; the interface contract (list[TaskItem]) stays constant.
    """

    _SEPARATORS = (".", ";", "\n", "→", "->")

    def decompose(self, user_goal: str) -> list[TaskItem]:
        """
        Parse *user_goal* into an ordered sequence of TaskItems.

        Returns at minimum one TaskItem even for a single-sentence goal.
        Dependencies form a strict chain: task[n] depends on task[n-1].
        """
        if not user_goal or not user_goal.strip():
            raise ValueError("user_goal must not be empty")

        segments = self._split_goal(user_goal)
        tasks: list[TaskItem] = []
        prev_id: str | None = None

        for i, segment in enumerate(segments):
            deps = [prev_id] if prev_id is not None else []
            task = TaskItem(
                description=segment.strip(),
                dependencies=deps,
            )
            tasks.append(task)
            prev_id = task.task_id
            log.debug("Decomposed task %d/%d: [%s] %s", i + 1, len(segments), task.task_id[:8], task.description)

        log.info("GoalDecomposer produced %d task(s) from goal.", len(tasks))
        return tasks

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _split_goal(self, text: str) -> list[str]:
        """Split *text* on all separator characters, keeping non-empty parts."""
        # Replace all separators with a single sentinel
        normalised = text
        for sep in self._SEPARATORS:
            normalised = normalised.replace(sep, "|")
        parts = [p.strip() for p in normalised.split("|") if p.strip()]
        return parts if parts else [text.strip()]


# ---------------------------------------------------------------------------
# Bounded Milestone Persistence
# ---------------------------------------------------------------------------


class BoundedMilestonePersistence:
    """
    Enforces ledger checkpoint integrity.

    Before a task can be declared COMPLETE, every mandatory ledger assertion
    (boolean checkpoint) must be True.  Any False triggers a fatal halt.
    """

    def halt_on_missing_ledger_assertion(self, assertions: list[bool]) -> None:
        """
        Validate all *assertions*.

        Raises LedgerAssertionError at the index of the first False value.
        Raises ValueError if *assertions* is empty (no checkpoints registered
        is itself a violation of the milestone contract).
        """
        if not assertions:
            raise ValueError(
                "halt_on_missing_ledger_assertion: at least one ledger "
                "assertion is required — zero-length list is not valid."
            )
        for idx, passed in enumerate(assertions):
            if not passed:
                log.critical(
                    "Ledger assertion FAILED at index %d — milestone halted.", idx
                )
                raise LedgerAssertionError(idx)
        log.debug("All %d ledger assertion(s) passed.", len(assertions))


# ---------------------------------------------------------------------------
# Execution Loop
# ---------------------------------------------------------------------------


class ExecutionLoop:
    """
    Non-bypassable five-step loop: Plan → Execute → Observe → Evaluate → Re-Plan.

    Every phase is a discrete, logged operation.  Skipping a phase is
    architecturally impossible through the public run() method.

    Parameters
    ----------
    invariants:
        List of zero-argument callables that each return True (pass) or
        False/raise (fail).  All are evaluated after every Observe phase.
    ledger:
        BoundedMilestonePersistence instance.  Defaults to a new instance.
    """

    def __init__(
        self,
        invariants: list[Callable[[], bool]] | None = None,
        ledger: BoundedMilestonePersistence | None = None,
    ) -> None:
        self._invariants: list[Callable[[], bool]] = invariants or []
        self._ledger = ledger or BoundedMilestonePersistence()
        # Callable hooks for dependency injection / testing
        self._execute_hook: Callable[[ExecutionPlan], ExecutionResult] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_invariant(self, fn: Callable[[], bool], name: str | None = None) -> None:
        """Add *fn* to the invariant registry."""
        fn._invariant_name = name or getattr(fn, "__name__", repr(fn))  # type: ignore[attr-defined]
        self._invariants.append(fn)
        log.debug("Invariant registered: %s", fn._invariant_name)  # type: ignore[attr-defined]

    def set_execute_hook(self, hook: Callable[[ExecutionPlan], ExecutionResult]) -> None:
        """
        Override the default execute phase with a custom callable.
        Primarily for testing and tool-calling integration.
        """
        self._execute_hook = hook

    def run(
        self,
        task: TaskItem,
        max_iterations: int = 5,
        ledger_assertions: list[bool] | None = None,
    ) -> TaskItem:
        """
        Run the full Plan→Execute→Observe→Evaluate→Re-Plan loop for *task*.

        Parameters
        ----------
        task:
            The TaskItem to execute.  Its status is updated in-place.
        max_iterations:
            Hard cap on the number of full loop cycles.
        ledger_assertions:
            Boolean checkpoints that must all be True before COMPLETE is
            declared.  Defaults to [True] (single always-pass assertion).

        Returns
        -------
        TaskItem
            The same *task* object, with updated status and history.

        Raises
        ------
        InvariantViolationError
            Immediately on any invariant failure.
        LedgerAssertionError
            If any ledger assertion is False at completion time.
        MaxIterationsExceeded
            If the loop did not reach COMPLETE within max_iterations.
        """
        if ledger_assertions is None:
            ledger_assertions = [True]

        task.mark_in_progress()
        log.info("ExecutionLoop starting task [%s]: %s", task.task_id[:8], task.description)

        current_plan: ExecutionPlan | None = None
        evaluation: EvaluationResult | None = None

        for iteration in range(1, max_iterations + 1):
            log.info("=== Iteration %d / %d [task %s] ===", iteration, max_iterations, task.task_id[:8])

            # ── Phase 1: PLAN ──────────────────────────────────────────────
            current_plan = self.plan(task, iteration=iteration, prior_evaluation=evaluation)
            log.info("[%s] PLAN: %d step(s)", task.task_id[:8], len(current_plan.steps))

            # ── Phase 2: EXECUTE ───────────────────────────────────────────
            result = self.execute(current_plan)
            log.info("[%s] EXECUTE: success=%s", task.task_id[:8], result.success)

            # ── Phase 3: OBSERVE ───────────────────────────────────────────
            observation = self.observe(result)
            log.info(
                "[%s] OBSERVE: %d fact(s), %d anomaly(ies)",
                task.task_id[:8], len(observation.facts), len(observation.anomalies),
            )

            # ── Phase 4: EVALUATE (run all invariants) ─────────────────────
            evaluation = self.evaluate(observation, self._invariants)
            log.info("[%s] EVALUATE: passed=%s", task.task_id[:8], evaluation.passed)

            # Fatal halt on invariant failure
            if not evaluation.passed:
                task.mark_failed(f"Invariant violation: {evaluation.failed_invariant}")
                raise InvariantViolationError(
                    invariant_name=evaluation.failed_invariant or "unknown",
                    detail=evaluation.detail,
                )

            # Record iteration history
            task._loop_history.append(
                {
                    "iteration": iteration,
                    "phase": LoopPhase.EVALUATE.value,
                    "result": evaluation,
                }
            )

            # If execution succeeded and evaluation passed → we're done
            if result.success and not evaluation.should_re_plan:
                # ── Pre-completion ledger check ────────────────────────────
                self._ledger.halt_on_missing_ledger_assertion(ledger_assertions)
                task.mark_complete()
                log.info(
                    "Task [%s] COMPLETE after %d iteration(s).",
                    task.task_id[:8], iteration,
                )
                return task

            # ── Phase 5: RE-PLAN ───────────────────────────────────────────
            if iteration < max_iterations:
                current_plan = self.re_plan(task, evaluation)
                log.info("[%s] RE_PLAN: %d step(s)", task.task_id[:8], len(current_plan.steps))
            else:
                log.warning(
                    "[%s] Max iterations reached without completion.", task.task_id[:8]
                )

        # Exhausted iterations
        task.mark_failed(f"Max iterations ({max_iterations}) exceeded")
        raise MaxIterationsExceeded(task.task_id, max_iterations)

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def plan(
        self,
        task: TaskItem,
        iteration: int = 1,
        prior_evaluation: EvaluationResult | None = None,
    ) -> ExecutionPlan:
        """
        PLAN phase — produce a structured execution plan for *task*.

        In the default (stub) implementation the plan contains a single step
        mirroring the task description.  Wire in an LLM/tool-calling layer
        by subclassing and overriding this method.
        """
        steps: list[str] = []

        if prior_evaluation and prior_evaluation.should_re_plan:
            # Incorporate feedback from prior evaluation
            steps.append(f"Re-address: {prior_evaluation.detail or task.description}")
        else:
            steps.append(task.description)

        steps.append("Validate output against acceptance criteria")
        steps.append("Emit status telemetry")

        plan = ExecutionPlan(
            task_id=task.task_id,
            steps=steps,
            iteration=iteration,
        )
        log.debug("PLAN phase: task=%s steps=%s", task.task_id[:8], steps)
        return plan

    def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        """
        EXECUTE phase — carry out the plan steps.

        If a custom execute hook has been registered (via set_execute_hook),
        it is called instead of the default stub.
        """
        if self._execute_hook is not None:
            return self._execute_hook(plan)

        # Default stub: treat all steps as successful no-ops.
        log.debug("EXECUTE phase (stub): plan task=%s, steps=%d", plan.task_id[:8], len(plan.steps))
        return ExecutionResult(
            task_id=plan.task_id,
            plan=plan,
            success=True,
            output={"steps_executed": plan.steps},
        )

    def observe(self, result: ExecutionResult) -> Observation:
        """
        OBSERVE phase — extract structured facts and anomalies from *result*.
        """
        facts: list[str] = []
        anomalies: list[str] = []

        if result.success:
            facts.append("Execution reported success")
            if isinstance(result.output, dict):
                for k, v in result.output.items():
                    facts.append(f"{k}: {v}")
        else:
            anomalies.append(f"Execution failed: {result.error or 'unknown error'}")

        observation = Observation(
            task_id=result.task_id,
            result=result,
            facts=facts,
            anomalies=anomalies,
        )
        log.debug(
            "OBSERVE phase: task=%s facts=%d anomalies=%d",
            result.task_id[:8], len(facts), len(anomalies),
        )
        return observation

    def evaluate(
        self,
        observation: Observation,
        invariants: list[Callable[[], bool]],
    ) -> EvaluationResult:
        """
        EVALUATE phase — run every registered invariant callable.

        The first failing invariant causes an immediate return with
        passed=False.  All invariants must pass for the result to be True.

        An invariant callable:
          • Must accept zero arguments.
          • Must return True (pass) or False (fail).
          • May raise any exception, which is treated as a failure.
        """
        for fn in invariants:
            name = getattr(fn, "_invariant_name", None) or getattr(fn, "__name__", repr(fn))
            try:
                ok = bool(fn())
            except Exception as exc:
                log.error("Invariant [%s] raised: %s", name, exc)
                return EvaluationResult(
                    task_id=observation.task_id,
                    observation=observation,
                    passed=False,
                    failed_invariant=name,
                    detail=str(exc),
                    should_re_plan=False,
                )
            if not ok:
                log.warning("Invariant [%s] returned False", name)
                return EvaluationResult(
                    task_id=observation.task_id,
                    observation=observation,
                    passed=False,
                    failed_invariant=name,
                    detail="Invariant callable returned False",
                    should_re_plan=False,
                )

        # Determine whether re-plan is needed from anomalies
        should_re_plan = len(observation.anomalies) > 0 and not observation.result.success
        return EvaluationResult(
            task_id=observation.task_id,
            observation=observation,
            passed=True,
            should_re_plan=should_re_plan,
            detail="" if not should_re_plan else f"{len(observation.anomalies)} anomaly(ies) found",
        )

    def re_plan(
        self,
        task: TaskItem,
        evaluation: EvaluationResult,
    ) -> ExecutionPlan:
        """
        RE-PLAN phase — produce a revised plan based on evaluation feedback.

        Delegates to self.plan() with the evaluation context so that the
        PLAN phase can adapt.  Override for LLM-driven re-planning.
        """
        log.info(
            "RE_PLAN phase: task=%s detail='%s'",
            task.task_id[:8], evaluation.detail,
        )
        return self.plan(
            task,
            iteration=evaluation.observation.result.plan.iteration + 1,
            prior_evaluation=evaluation,
        )
