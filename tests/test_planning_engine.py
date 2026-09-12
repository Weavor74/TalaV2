import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from core.planning_engine import (
    Goal, GoalStatus, DependencyMapper, PlanningEngine, DependencyError
)

def test_topological_sort_success():
    g1 = Goal("g1", "First")
    g2 = Goal("g2", "Second", dependencies=["g1"])
    g3 = Goal("g3", "Third", dependencies=["g2"])
    g4 = Goal("g4", "Fourth", dependencies=["g1"])
    
    # Mix them up
    goals = [g3, g1, g4, g2]
    
    sorted_goals = DependencyMapper.topological_sort(goals)
    
    # g1 must be first
    assert sorted_goals[0].goal_id == "g1"
    
    # g2 and g4 must come before g3, and after g1
    # Check index of g2 and g3
    g2_idx = next(i for i, g in enumerate(sorted_goals) if g.goal_id == "g2")
    g3_idx = next(i for i, g in enumerate(sorted_goals) if g.goal_id == "g3")
    assert g2_idx < g3_idx

def test_topological_sort_cycle():
    g1 = Goal("g1", "First", dependencies=["g3"])
    g2 = Goal("g2", "Second", dependencies=["g1"])
    g3 = Goal("g3", "Third", dependencies=["g2"])
    
    with pytest.raises(DependencyError, match="Circular dependency"):
        DependencyMapper.topological_sort([g1, g2, g3])

def test_missing_dependency():
    g1 = Goal("g1", "First", dependencies=["nonexistent"])
    
    with pytest.raises(DependencyError, match="depends on missing goal"):
        DependencyMapper.topological_sort([g1])

def test_evaluate_state():
    engine = PlanningEngine()
    g1 = Goal("g1", "First")
    g2 = Goal("g2", "Second", dependencies=["g1"])
    g3 = Goal("g3", "Third", dependencies=["g2"])
    
    plan = engine.create_plan("Test", [g2, g3, g1])
    
    # Initially g1 should be chosen because it has no dependencies
    next_goal = engine.evaluate_state(plan, {})
    assert next_goal.goal_id == "g1"
    
    # Complete g1, next should be g2
    plan.get_goal("g1").status = GoalStatus.COMPLETE
    next_goal = engine.evaluate_state(plan, {})
    assert next_goal.goal_id == "g2"
    
    # Complete g2, next should be g3
    plan.get_goal("g2").status = GoalStatus.COMPLETE
    next_goal = engine.evaluate_state(plan, {})
    assert next_goal.goal_id == "g3"
    
    # Complete g3, no goals left
    plan.get_goal("g3").status = GoalStatus.COMPLETE
    next_goal = engine.evaluate_state(plan, {})
    assert next_goal is None

def test_update_plan():
    engine = PlanningEngine()
    g1 = Goal("g1", "First")
    g2 = Goal("g2", "Second", dependencies=["g1"])
    
    plan = engine.create_plan("Test", [g1, g2])
    
    g3 = Goal("g3", "Third", dependencies=["g1"])
    
    # Update plan by adding g3 and removing g2
    updated_plan = engine.update_plan(plan, new_goals=[g3], remove_goal_ids=["g2"])
    
    assert len(updated_plan.goals) == 2
    assert updated_plan.get_goal("g2") is None
    assert updated_plan.get_goal("g3") is not None
