# File: `planner.py`

## Module Description

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

## Classes

### `class InvariantViolationError`
Raised when a registered invariant returns False or raises.

- **`def __init__(self, invariant_name, detail)`**

### `class LedgerAssertionError`
Raised when a ledger assertion (required milestone checkpoint) fails.

- **`def __init__(self, failed_index)`**

### `class MaxIterationsExceeded`
Raised when the loop exhausts max_iterations without COMPLETE.

- **`def __init__(self, task_id, iterations)`**

### `class TaskStatus`

### `class LoopPhase`

### `class TaskItem`
A single unit of work produced by GoalDecomposer.

- **`def mark_complete(self)`**
- **`def mark_failed(self, reason)`**
- **`def mark_in_progress(self)`**
- **`def mark_blocked(self, reason)`**

### `class PhasePlan`
Represents a structured execution phase for a high-level goal.


### `class ExecutionPlan`
Structured plan produced by the PLAN phase.


### `class ExecutionResult`
Raw result from the EXECUTE phase.


### `class Observation`
Structured observation produced from an ExecutionResult.


### `class EvaluationResult`
Outcome of running invariants against an Observation.


### `class GoalDecomposer`
Parses a free-form user goal string into an ordered list of TaskItems.

Decomposition strategy (heuristic):
  1. Split on common sentence terminators (period, semicolon, newline).
  2. Strip whitespace; skip empty segments.
  3. Assign sequential dependency chain (each task depends on the prior).

For production use this can be wired to an LLM call that returns
structured JSON; the interface contract (list[TaskItem]) stays constant.

- **`def decompose(self, user_goal)`**
  - Parse *user_goal* into an ordered sequence of TaskItems.
  
  Returns at minimum one TaskItem even for a single-sentence goal.
  Dependencies form a strict chain: task[n] depends on task[n-1].
- **`def _split_goal(self, text)`**
  - Split *text* on all separator characters, keeping non-empty parts.

### `class BoundedMilestonePersistence`
Enforces ledger checkpoint integrity.

Before a task can be declared COMPLETE, every mandatory ledger assertion
(boolean checkpoint) must be True.  Any False triggers a fatal halt.

- **`def halt_on_missing_ledger_assertion(self, assertions)`**
  - Validate all *assertions*.
  
  Raises LedgerAssertionError at the index of the first False value.
  Raises ValueError if *assertions* is empty (no checkpoints registered
  is itself a violation of the milestone contract).

### `class ExecutionLoop`
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

- **`def __init__(self, invariants, ledger)`**
- **`def register_invariant(self, fn, name)`**
  - Add *fn* to the invariant registry.
- **`def set_execute_hook(self, hook)`**
  - Override the default execute phase with a custom callable.
  Primarily for testing and tool-calling integration.
- **`def run(self, task, max_iterations, ledger_assertions)`**
  - Run the full Plan→Execute→Observe→Evaluate→Re-Plan loop for *task*.
  
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
- **`def plan(self, task, iteration, prior_evaluation)`**
  - PLAN phase — produce a structured execution plan for *task*.
  
  In the default (stub) implementation the plan contains a single step
  mirroring the task description.  Wire in an LLM/tool-calling layer
  by subclassing and overriding this method.
- **`def execute(self, plan)`**
  - EXECUTE phase — carry out the plan steps.
  
  If a custom execute hook has been registered (via set_execute_hook),
  it is called instead of the default stub.
- **`def observe(self, result)`**
  - OBSERVE phase — extract structured facts and anomalies from *result*.
- **`def evaluate(self, observation, invariants)`**
  - EVALUATE phase — run every registered invariant callable.
  
  The first failing invariant causes an immediate return with
  passed=False.  All invariants must pass for the result to be True.
  
  An invariant callable:
    • Must accept zero arguments.
    • Must return True (pass) or False (fail).
    • May raise any exception, which is treated as a failure.
- **`def re_plan(self, task, evaluation)`**
  - RE-PLAN phase — produce a revised plan based on evaluation feedback.
  
  Delegates to self.plan() with the evaluation context so that the
  PLAN phase can adapt.  Override for LLM-driven re-planning.

## Source Code

```python
"""\nsdk/agent/planner.py\n====================\nGoal Decomposition & Bounded Loop Engine — Tala Cognitive Engine Room\nMilestone 6 | Task 6.1.1\n\nImplements the non-bypassable five-step Plan→Execute→Observe→Evaluate→Re-Plan\nloop.  Every task must traverse all five phases before being declared COMPLETE.\nThe loop halts immediately on any invariant deviation or ledger assertion\nfailure by raising InvariantViolationError (defined in sdk/agent/exceptions.py).\n\nDesign invariants:\n  • No task can skip a loop phase — the run() method enforces ordering.\n  • Invariant callables are run after every Observe phase; failure is fatal.\n  • max_iterations caps the loop to prevent infinite re-planning.\n  • BoundedMilestonePersistence.halt_on_missing_ledger_assertion() must be\n    satisfied before a task transitions to COMPLETE.\n"""\n\nfrom __future__ import annotations\n\nimport logging\nimport uuid\nfrom collections.abc import Callable\nfrom dataclasses import dataclass, field\nfrom datetime import UTC, datetime\nfrom enum import Enum, auto\nfrom typing import Any\n\n# ---------------------------------------------------------------------------\n# Logging\n# ---------------------------------------------------------------------------\n\nlog = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Exceptions\n# ---------------------------------------------------------------------------\n\n\nclass InvariantViolationError(Exception):\n    """Raised when a registered invariant returns False or raises."""\n\n    def __init__(self, invariant_name: str, detail: str = "") -> None:\n        self.invariant_name = invariant_name\n        self.detail = detail\n        super().__init__(\n            f"Invariant violation [{invariant_name}]: {detail or 'check returned False'}"\n        )\n\n\nclass LedgerAssertionError(Exception):\n    """Raised when a ledger assertion (required milestone checkpoint) fails."""\n\n    def __init__(self, failed_index: int) -> None:\n        self.failed_index = failed_index\n        super().__init__(\n            f"Ledger assertion at index {failed_index} failed — milestone halted."\n        )\n\n\nclass MaxIterationsExceeded(Exception):\n    """Raised when the loop exhausts max_iterations without COMPLETE."""\n\n    def __init__(self, task_id: str, iterations: int) -> None:\n        super().__init__(\n            f"Task {task_id} did not complete within {iterations} iteration(s)."\n        )\n\n\n# ---------------------------------------------------------------------------\n# Enums\n# ---------------------------------------------------------------------------\n\n\nclass TaskStatus(Enum):\n    PENDING = auto()\n    IN_PROGRESS = auto()\n    COMPLETE = auto()\n    FAILED = auto()\n    BLOCKED = auto()\n\n\nclass LoopPhase(Enum):\n    PLAN = "PLAN"\n    EXECUTE = "EXECUTE"\n    OBSERVE = "OBSERVE"\n    EVALUATE = "EVALUATE"\n    RE_PLAN = "RE_PLAN"\n\n\n# ---------------------------------------------------------------------------\n# Core data classes\n# ---------------------------------------------------------------------------\n\n\n@dataclass\nclass TaskItem:\n    """A single unit of work produced by GoalDecomposer."""\n\n    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))\n    description: str = ""\n    status: TaskStatus = TaskStatus.PENDING\n    # Ordered list of task_ids that must complete before this task may start\n    dependencies: list[str] = field(default_factory=list)\n    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n    completed_at: datetime | None = None\n    # Accumulated context from each loop iteration\n    _loop_history: list[dict[str, Any]] = field(default_factory=list, repr=False)\n\n    def mark_complete(self) -> None:\n        self.status = TaskStatus.COMPLETE\n        self.completed_at = datetime.now(UTC)\n\n    def mark_failed(self, reason: str = "") -> None:\n        self.status = TaskStatus.FAILED\n        log.warning("Task %s marked FAILED: %s", self.task_id, reason)\n\n    def mark_in_progress(self) -> None:\n        self.status = TaskStatus.IN_PROGRESS\n\n    def mark_blocked(self, reason: str = "") -> None:\n        self.status = TaskStatus.BLOCKED\n        log.warning("Task %s marked BLOCKED: %s", self.task_id, reason)\n\n\n@dataclass\nclass PhasePlan:\n    """\n    Represents a structured execution phase for a high-level goal.\n    """\n    name: str\n    steps: list[str] = field(default_factory=list)\n    acceptance_criteria: list[str] = field(default_factory=list)\n    verification_command: str | None = None\n    rollback_on_failure: bool = True\n\n\n@dataclass\nclass ExecutionPlan:\n    """Structured plan produced by the PLAN phase."""\n\n    task_id: str\n    steps: list[str] = field(default_factory=list)\n    metadata: dict[str, Any] = field(default_factory=dict)\n    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n    # Iteration number this plan was generated for\n    iteration: int = 0\n\n\n@dataclass\nclass ExecutionResult:\n    """Raw result from the EXECUTE phase."""\n\n    task_id: str\n    plan: ExecutionPlan\n    success: bool = False\n    output: Any = None\n    error: str | None = None\n    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n\n\n@dataclass\nclass Observation:\n    """Structured observation produced from an ExecutionResult."""\n\n    task_id: str\n    result: ExecutionResult\n    facts: list[str] = field(default_factory=list)\n    anomalies: list[str] = field(default_factory=list)\n    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n\n\n@dataclass\nclass EvaluationResult:\n    """Outcome of running invariants against an Observation."""\n\n    task_id: str\n    observation: Observation\n    passed: bool = False\n    failed_invariant: str | None = None\n    detail: str = ""\n    should_re_plan: bool = False\n    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n\n\n# ---------------------------------------------------------------------------\n# Goal Decomposer\n# ---------------------------------------------------------------------------\n\n\nclass GoalDecomposer:\n    """\n    Parses a free-form user goal string into an ordered list of TaskItems.\n\n    Decomposition strategy (heuristic):\n      1. Split on common sentence terminators (period, semicolon, newline).\n      2. Strip whitespace; skip empty segments.\n      3. Assign sequential dependency chain (each task depends on the prior).\n\n    For production use this can be wired to an LLM call that returns\n    structured JSON; the interface contract (list[TaskItem]) stays constant.\n    """\n\n    _SEPARATORS = (".", ";", "\n", "→", "->")\n\n    def decompose(self, user_goal: str) -> list[TaskItem]:\n        """\n        Parse *user_goal* into an ordered sequence of TaskItems.\n\n        Returns at minimum one TaskItem even for a single-sentence goal.\n        Dependencies form a strict chain: task[n] depends on task[n-1].\n        """\n        if not user_goal or not user_goal.strip():\n            raise ValueError("user_goal must not be empty")\n\n        segments = self._split_goal(user_goal)\n        tasks: list[TaskItem] = []\n        prev_id: str | None = None\n\n        for i, segment in enumerate(segments):\n            deps = [prev_id] if prev_id is not None else []\n            task = TaskItem(\n                description=segment.strip(),\n                dependencies=deps,\n            )\n            tasks.append(task)\n            prev_id = task.task_id\n            log.debug("Decomposed task %d/%d: [%s] %s", i + 1, len(segments), task.task_id[:8], task.description)\n\n        log.info("GoalDecomposer produced %d task(s) from goal.", len(tasks))\n        return tasks\n\n    # ------------------------------------------------------------------\n    # Private helpers\n    # ------------------------------------------------------------------\n\n    def _split_goal(self, text: str) -> list[str]:\n        """Split *text* on all separator characters, keeping non-empty parts."""\n        # Replace all separators with a single sentinel\n        normalised = text\n        for sep in self._SEPARATORS:\n            normalised = normalised.replace(sep, "|")\n        parts = [p.strip() for p in normalised.split("|") if p.strip()]\n        return parts if parts else [text.strip()]\n\n\n# ---------------------------------------------------------------------------\n# Bounded Milestone Persistence\n# ---------------------------------------------------------------------------\n\n\nclass BoundedMilestonePersistence:\n    """\n    Enforces ledger checkpoint integrity.\n\n    Before a task can be declared COMPLETE, every mandatory ledger assertion\n    (boolean checkpoint) must be True.  Any False triggers a fatal halt.\n    """\n\n    def halt_on_missing_ledger_assertion(self, assertions: list[bool]) -> None:\n        """\n        Validate all *assertions*.\n\n        Raises LedgerAssertionError at the index of the first False value.\n        Raises ValueError if *assertions* is empty (no checkpoints registered\n        is itself a violation of the milestone contract).\n        """\n        if not assertions:\n            raise ValueError(\n                "halt_on_missing_ledger_assertion: at least one ledger "\n                "assertion is required — zero-length list is not valid."\n            )\n        for idx, passed in enumerate(assertions):\n            if not passed:\n                log.critical(\n                    "Ledger assertion FAILED at index %d — milestone halted.", idx\n                )\n                raise LedgerAssertionError(idx)\n        log.debug("All %d ledger assertion(s) passed.", len(assertions))\n\n\n# ---------------------------------------------------------------------------\n# Execution Loop\n# ---------------------------------------------------------------------------\n\n\nclass ExecutionLoop:\n    """\n    Non-bypassable five-step loop: Plan → Execute → Observe → Evaluate → Re-Plan.\n\n    Every phase is a discrete, logged operation.  Skipping a phase is\n    architecturally impossible through the public run() method.\n\n    Parameters\n    ----------\n    invariants:\n        List of zero-argument callables that each return True (pass) or\n        False/raise (fail).  All are evaluated after every Observe phase.\n    ledger:\n        BoundedMilestonePersistence instance.  Defaults to a new instance.\n    """\n\n    def __init__(\n        self,\n        invariants: list[Callable[[], bool]] | None = None,\n        ledger: BoundedMilestonePersistence | None = None,\n    ) -> None:\n        self._invariants: list[Callable[[], bool]] = invariants or []\n        self._ledger = ledger or BoundedMilestonePersistence()\n        # Callable hooks for dependency injection / testing\n        self._execute_hook: Callable[[ExecutionPlan], ExecutionResult] | None = None\n\n    # ------------------------------------------------------------------\n    # Public API\n    # ------------------------------------------------------------------\n\n    def register_invariant(self, fn: Callable[[], bool], name: str | None = None) -> None:\n        """Add *fn* to the invariant registry."""\n        fn._invariant_name = name or getattr(fn, "__name__", repr(fn))  # type: ignore[attr-defined]\n        self._invariants.append(fn)\n        log.debug("Invariant registered: %s", fn._invariant_name)  # type: ignore[attr-defined]\n\n    def set_execute_hook(self, hook: Callable[[ExecutionPlan], ExecutionResult]) -> None:\n        """\n        Override the default execute phase with a custom callable.\n        Primarily for testing and tool-calling integration.\n        """\n        self._execute_hook = hook\n\n    def run(\n        self,\n        task: TaskItem,\n        max_iterations: int = 5,\n        ledger_assertions: list[bool] | None = None,\n    ) -> TaskItem:\n        """\n        Run the full Plan→Execute→Observe→Evaluate→Re-Plan loop for *task*.\n\n        Parameters\n        ----------\n        task:\n            The TaskItem to execute.  Its status is updated in-place.\n        max_iterations:\n            Hard cap on the number of full loop cycles.\n        ledger_assertions:\n            Boolean checkpoints that must all be True before COMPLETE is\n            declared.  Defaults to [True] (single always-pass assertion).\n\n        Returns\n        -------\n        TaskItem\n            The same *task* object, with updated status and history.\n\n        Raises\n        ------\n        InvariantViolationError\n            Immediately on any invariant failure.\n        LedgerAssertionError\n            If any ledger assertion is False at completion time.\n        MaxIterationsExceeded\n            If the loop did not reach COMPLETE within max_iterations.\n        """\n        if ledger_assertions is None:\n            ledger_assertions = [True]\n\n        task.mark_in_progress()\n        log.info("ExecutionLoop starting task [%s]: %s", task.task_id[:8], task.description)\n\n        current_plan: ExecutionPlan | None = None\n        evaluation: EvaluationResult | None = None\n\n        for iteration in range(1, max_iterations + 1):\n            log.info("=== Iteration %d / %d [task %s] ===", iteration, max_iterations, task.task_id[:8])\n\n            # ── Phase 1: PLAN ──────────────────────────────────────────────\n            current_plan = self.plan(task, iteration=iteration, prior_evaluation=evaluation)\n            log.info("[%s] PLAN: %d step(s)", task.task_id[:8], len(current_plan.steps))\n\n            # ── Phase 2: EXECUTE ───────────────────────────────────────────\n            result = self.execute(current_plan)\n            log.info("[%s] EXECUTE: success=%s", task.task_id[:8], result.success)\n\n            # ── Phase 3: OBSERVE ───────────────────────────────────────────\n            observation = self.observe(result)\n            log.info(\n                "[%s] OBSERVE: %d fact(s), %d anomaly(ies)",\n                task.task_id[:8], len(observation.facts), len(observation.anomalies),\n            )\n\n            # ── Phase 4: EVALUATE (run all invariants) ─────────────────────\n            evaluation = self.evaluate(observation, self._invariants)\n            log.info("[%s] EVALUATE: passed=%s", task.task_id[:8], evaluation.passed)\n\n            # Fatal halt on invariant failure\n            if not evaluation.passed:\n                task.mark_failed(f"Invariant violation: {evaluation.failed_invariant}")\n                raise InvariantViolationError(\n                    invariant_name=evaluation.failed_invariant or "unknown",\n                    detail=evaluation.detail,\n                )\n\n            # Record iteration history\n            task._loop_history.append(\n                {\n                    "iteration": iteration,\n                    "phase": LoopPhase.EVALUATE.value,\n                    "result": evaluation,\n                }\n            )\n\n            # If execution succeeded and evaluation passed → we're done\n            if result.success and not evaluation.should_re_plan:\n                # ── Pre-completion ledger check ────────────────────────────\n                self._ledger.halt_on_missing_ledger_assertion(ledger_assertions)\n                task.mark_complete()\n                log.info(\n                    "Task [%s] COMPLETE after %d iteration(s).",\n                    task.task_id[:8], iteration,\n                )\n                return task\n\n            # ── Phase 5: RE-PLAN ───────────────────────────────────────────\n            if iteration < max_iterations:\n                current_plan = self.re_plan(task, evaluation)\n                log.info("[%s] RE_PLAN: %d step(s)", task.task_id[:8], len(current_plan.steps))\n            else:\n                log.warning(\n                    "[%s] Max iterations reached without completion.", task.task_id[:8]\n                )\n\n        # Exhausted iterations\n        task.mark_failed(f"Max iterations ({max_iterations}) exceeded")\n        raise MaxIterationsExceeded(task.task_id, max_iterations)\n\n    # ------------------------------------------------------------------\n    # Phase implementations\n    # ------------------------------------------------------------------\n\n    def plan(\n        self,\n        task: TaskItem,\n        iteration: int = 1,\n        prior_evaluation: EvaluationResult | None = None,\n    ) -> ExecutionPlan:\n        """\n        PLAN phase — produce a structured execution plan for *task*.\n\n        In the default (stub) implementation the plan contains a single step\n        mirroring the task description.  Wire in an LLM/tool-calling layer\n        by subclassing and overriding this method.\n        """\n        steps: list[str] = []\n\n        if prior_evaluation and prior_evaluation.should_re_plan:\n            # Incorporate feedback from prior evaluation\n            steps.append(f"Re-address: {prior_evaluation.detail or task.description}")\n        else:\n            steps.append(task.description)\n\n        steps.append("Validate output against acceptance criteria")\n        steps.append("Emit status telemetry")\n\n        plan = ExecutionPlan(\n            task_id=task.task_id,\n            steps=steps,\n            iteration=iteration,\n        )\n        log.debug("PLAN phase: task=%s steps=%s", task.task_id[:8], steps)\n        return plan\n\n    def execute(self, plan: ExecutionPlan) -> ExecutionResult:\n        """\n        EXECUTE phase — carry out the plan steps.\n\n        If a custom execute hook has been registered (via set_execute_hook),\n        it is called instead of the default stub.\n        """\n        if self._execute_hook is not None:\n            return self._execute_hook(plan)\n\n        # Default stub: treat all steps as successful no-ops.\n        log.debug("EXECUTE phase (stub): plan task=%s, steps=%d", plan.task_id[:8], len(plan.steps))\n        return ExecutionResult(\n            task_id=plan.task_id,\n            plan=plan,\n            success=True,\n            output={"steps_executed": plan.steps},\n        )\n\n    def observe(self, result: ExecutionResult) -> Observation:\n        """\n        OBSERVE phase — extract structured facts and anomalies from *result*.\n        """\n        facts: list[str] = []\n        anomalies: list[str] = []\n\n        if result.success:\n            facts.append("Execution reported success")\n            if isinstance(result.output, dict):\n... (file truncated, showing first 500 lines) ...
```
