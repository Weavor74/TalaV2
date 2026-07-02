"""
planning_engine.py - Autonomous planning, goal sequencing, and decision-making.

Houses the central logic for autonomous planning, advanced diagnostics (Dependency Mapping),
and orchestrating self-improvement plans.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

log = logging.getLogger(__name__)


class GoalStatus(Enum):
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETE = auto()
    FAILED = auto()
    BLOCKED = auto()


@dataclass
class Goal:
    """An atomic objective to be achieved."""
    goal_id: str
    description: str
    dependencies: list[str] = field(default_factory=list)
    priority: int = 0  # Higher number = higher priority
    status: GoalStatus = GoalStatus.PENDING
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    """A structured plan containing sequenced goals."""
    objective: str
    goals: list[Goal] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_goal(self, goal_id: str) -> Goal | None:
        for g in self.goals:
            if g.goal_id == goal_id:
                return g
        return None


class DependencyError(Exception):
    """Raised when there is a circular dependency or missing dependency."""
    pass


class DependencyMapper:
    """
    Advanced diagnostics for mapping and sorting dependencies using a directed acyclic graph (DAG).
    """

    @staticmethod
    def topological_sort(goals: list[Goal]) -> list[Goal]:
        """
        Sorts goals such that all dependencies for a goal appear before the goal itself.
        Raises DependencyError if a cycle is detected or a dependency is missing.
        """
        goal_map = {g.goal_id: g for g in goals}
        
        # Build adjacency list (dependents) and indegree map
        indegree = {g.goal_id: 0 for g in goals}
        adj = {g.goal_id: [] for g in goals}
        
        for g in goals:
            for dep in g.dependencies:
                if dep not in goal_map:
                    raise DependencyError(f"Goal '{g.goal_id}' depends on missing goal '{dep}'.")
                adj[dep].append(g.goal_id)
                indegree[g.goal_id] += 1
                
        # Kahn's algorithm for topological sorting
        queue = [g_id for g_id, count in indegree.items() if count == 0]
        sorted_ids = []
        
        while queue:
            # We don't sort by priority here to keep it strictly topological,
            # but we could break ties using priority.
            # To break ties by priority, we pick the highest priority item from the queue:
            queue.sort(key=lambda gid: goal_map[gid].priority, reverse=True)
            current_id = queue.pop(0)
            sorted_ids.append(current_id)
            
            for dependent_id in adj[current_id]:
                indegree[dependent_id] -= 1
                if indegree[dependent_id] == 0:
                    queue.append(dependent_id)
                    
        if len(sorted_ids) != len(goals):
            raise DependencyError("Circular dependency detected among goals.")
            
        return [goal_map[g_id] for g_id in sorted_ids]


class GoalSequencer:
    """
    Sequences goals safely, respecting dependencies and priorities.
    """
    
    def __init__(self, mapper: DependencyMapper | None = None) -> None:
        self.mapper = mapper or DependencyMapper()

    def sequence(self, goals: list[Goal]) -> list[Goal]:
        """
        Sequence goals in optimal execution order.
        """
        log.debug("Sequencing %d goals...", len(goals))
        return self.mapper.topological_sort(goals)


class PlanningEngine:
    """
    The Operational Brain: Coordinates goal creation, sequencing, and autonomous decision-making.
    Designed to integrate with `self_improvement.create_plan` and higher-level orchestrators.
    """
    
    def __init__(self, sequencer: GoalSequencer | None = None) -> None:
        self.sequencer = sequencer or GoalSequencer()

    def create_plan(self, objective: str, raw_goals: list[Goal], context: dict[str, Any] | None = None) -> Plan:
        """
        Creates and sequences a formal Plan for a given objective.
        """
        log.info("PlanningEngine: Creating plan for objective: %s", objective)
        try:
            sequenced_goals = self.sequencer.sequence(raw_goals)
        except DependencyError as e:
            log.error("Failed to sequence goals: %s", e)
            raise
            
        plan = Plan(
            objective=objective,
            goals=sequenced_goals,
            metadata=context or {}
        )
        return plan

    def evaluate_state(self, plan: Plan, current_state: dict[str, Any]) -> Goal | None:
        """
        Inspects the current progress and context to decide the *next optimal goal* to execute.
        Returns None if the plan is fully complete or permanently blocked.
        """
        log.debug("PlanningEngine: Evaluating state for plan: %s", plan.objective)
        
        # Determine status of all goals
        for goal in plan.goals:
            if goal.status == GoalStatus.PENDING or goal.status == GoalStatus.FAILED:
                # Check if dependencies are met
                deps_met = True
                for dep_id in goal.dependencies:
                    dep_goal = plan.get_goal(dep_id)
                    if not dep_goal or dep_goal.status != GoalStatus.COMPLETE:
                        deps_met = False
                        break
                        
                if deps_met:
                    # Found the highest priority unblocked goal (since they are topologically sorted
                    # and tie-broken by priority).
                    return goal
                    
        return None

    def update_plan(self, plan: Plan, new_goals: list[Goal], remove_goal_ids: list[str]) -> Plan:
        """
        Dynamically injects or removes goals as the autonomous agent discovers new requirements.
        """
        # Remove goals
        remove_set = set(remove_goal_ids)
        filtered_goals = [g for g in plan.goals if g.goal_id not in remove_set]
        
        # Check if removing goals breaks dependencies for remaining goals
        remaining_ids = {g.goal_id for g in filtered_goals}
        for g in filtered_goals:
            for dep in g.dependencies:
                if dep not in remaining_ids and dep not in [n.goal_id for n in new_goals]:
                    # We might need to prune or warn, but DependencyMapper will catch it below
                    pass

        # Add new goals
        combined_goals = filtered_goals + new_goals
        
        # Resequence
        try:
            sequenced_goals = self.sequencer.sequence(combined_goals)
            plan.goals = sequenced_goals
            return plan
        except DependencyError as e:
            log.error("Failed to update plan due to dependency issue: %s", e)
            raise

