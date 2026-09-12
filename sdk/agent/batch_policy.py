"""
sdk/agent/batch_policy.py
=========================
Batch Policy & Execution Budget — Tala Cognitive Engine Room
Milestone 11 | Task 11.2

Provides:
  BatchPolicy  — Configuration dataclass governing autonomous task execution
                 budgets, authority levels, and suspension behavior.
  BudgetTracker — Stateful tracker that checks policy limits during execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class BatchPolicy:
    """
    Governs how autonomously the GoalEngine may execute task phases.

    authority_level:
        "auto"          — auto-execute low/medium/high risk, block critical only
        "confirm_writes" — auto low/medium, confirm high/critical writes
        "full_confirm"  — require confirmation for every file write
    """

    max_steps: int = 50
    max_duration_minutes: float = 60.0
    suspend_on_budget_hit: bool = True
    authority_level: str = "auto"
    checkpoint_between_phases: bool = False
    min_skill_phases: int = 2  # Minimum phases for auto-skill node generation (0 = always)

    @classmethod
    def overnight(cls) -> "BatchPolicy":
        """Pre-configured policy for long unattended overnight runs."""
        return cls(
            max_steps=200,
            max_duration_minutes=480.0,
            suspend_on_budget_hit=True,
            authority_level="confirm_writes",
            min_skill_phases=1,  # Document every goal during overnight runs
        )

    @classmethod
    def strict(cls) -> "BatchPolicy":
        """Pre-configured policy requiring confirmation for all writes."""
        return cls(
            max_steps=20,
            max_duration_minutes=15.0,
            suspend_on_budget_hit=True,
            authority_level="full_confirm",
        )

    @classmethod
    def auto(cls) -> "BatchPolicy":
        """Default auto-execute policy (mirrors dataclass defaults)."""
        return cls(
            max_steps=50,
            max_duration_minutes=60.0,
            suspend_on_budget_hit=True,
            authority_level="auto",
        )


class BudgetTracker:
    """
    Tracks step count and wall-clock time against a BatchPolicy budget.
    """

    def __init__(self, policy: BatchPolicy) -> None:
        self.policy = policy
        self.steps_taken: int = 0
        self.start_time: datetime = datetime.now(UTC)

    def tick(self) -> None:
        """Increment the step counter by one."""
        self.steps_taken += 1

    def elapsed_minutes(self) -> float:
        """Return elapsed time in minutes since tracker was created."""
        return (datetime.now(UTC) - self.start_time).total_seconds() / 60.0

    def is_budget_exceeded(self) -> bool:
        """Return True if either the step or time budget has been exceeded."""
        if self.steps_taken >= self.policy.max_steps:
            return True
        if self.elapsed_minutes() >= self.policy.max_duration_minutes:
            return True
        return False

    def budget_reason(self) -> str:
        """Return a human-readable reason explaining why budget is exhausted."""
        if self.steps_taken >= self.policy.max_steps:
            return (
                f"Step budget exhausted "
                f"({self.steps_taken}/{self.policy.max_steps} steps used)"
            )
        elapsed = self.elapsed_minutes()
        return (
            f"Time budget exhausted "
            f"({elapsed:.1f}/{self.policy.max_duration_minutes} minutes elapsed)"
        )

    def remaining_minutes(self) -> float:
        """Budget left, never negative."""
        return max(0.0, self.policy.max_duration_minutes - self.elapsed_minutes())

    def deadline(self) -> datetime:
        """
        The wall-clock instant this run must stop by.

        Needed because the budget was only ever consulted *between* phases. A
        phase that made several model calls ran to completion however long it
        took, so a five-minute ceiling was measured overrunning to 6.4 minutes
        -- and the more work a phase does, the worse the overshoot. Handing the
        executor an absolute instant lets it check between nodes, which is a
        boundary it already has, rather than needing its own clock or a
        cancellation that could interrupt a half-written file.
        """
        return self.start_time + timedelta(minutes=self.policy.max_duration_minutes)

    def status(self) -> dict:
        """Return a snapshot of current budget utilization."""
        return {
            "steps_taken": self.steps_taken,
            "max_steps": self.policy.max_steps,
            "elapsed_minutes": round(self.elapsed_minutes(), 2),
            "max_duration_minutes": self.policy.max_duration_minutes,
            "authority_level": self.policy.authority_level,
            "budget_exceeded": self.is_budget_exceeded(),
        }
