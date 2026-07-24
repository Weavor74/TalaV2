import os
import json
import pytest
from unittest.mock import patch, MagicMock
from sdk.agent.tot_ideation import (
    is_architectural_or_decomposition_prompt,
    run_tot_ideation,
    COGNITIVE_FRAMES
)
from sdk.agent.queue import GoalTreeDecomposer
from sdk.agent.planner import TaskItem

def test_is_architectural_or_decomposition_prompt():
    # Architectural prompts
    assert is_architectural_or_decomposition_prompt("Design a high-throughput cache module for TalaV2") is True
    assert is_architectural_or_decomposition_prompt("Plan the database schema migration strategy") is True
    assert is_architectural_or_decomposition_prompt("Let's decompose this big system refactoring") is True
    
    # Non-architectural prompts
    assert is_architectural_or_decomposition_prompt("Write a quick greeting function") is False
    assert is_architectural_or_decomposition_prompt("run pytest on tests/test_vault_sync.py") is False
    assert is_architectural_or_decomposition_prompt("") is False

@pytest.mark.asyncio
async def test_run_tot_ideation_fallback():
    # Test fallback behavior when LLM is unavailable
    shortlist = await run_tot_ideation("Design a caching system")
    assert isinstance(shortlist, list)
    assert len(shortlist) == len(COGNITIVE_FRAMES)
    
    # Assert schema of items in the shortlist
    for item in shortlist:
        assert "approach_name" in item
        assert "frame" in item
        assert "score" in item
        assert "architectural_strategy" in item
        assert "tasks" in item
        assert "risk_matrix" in item
        assert "trap_list" in item
        
        # Verify tasks is a list of strings
        assert isinstance(item["tasks"], list)
        assert len(item["tasks"]) > 0
        
        # Verify risk matrix structure
        assert isinstance(item["risk_matrix"], list)
        assert len(item["risk_matrix"]) >= 3
        for risk in item["risk_matrix"]:
            assert "risk" in risk
            assert "impact" in risk
            assert "likelihood" in risk
            assert "mitigation" in risk
            
        # Verify trap list
        assert isinstance(item["trap_list"], list)
        assert len(item["trap_list"]) > 0

@pytest.mark.asyncio
async def test_goal_tree_decomposer_intercept(tmp_path):
    decomposer = GoalTreeDecomposer()
    
    # Ensure any cleanup from previous tests
    if os.path.exists("tot_shortlist.json"):
        os.remove("tot_shortlist.json")
        
    user_goal = "Design a fault-tolerant database replication module"
    
    tasks = await decomposer.decompose(user_goal)
    
    # Verify tasks are returned representing the top ToT approach
    assert isinstance(tasks, list)
    assert len(tasks) > 0
    
    # Verify tot_shortlist.json was written
    assert os.path.exists("tot_shortlist.json")
    with open("tot_shortlist.json", "r", encoding="utf-8") as f:
        shortlist_data = json.load(f)
    assert isinstance(shortlist_data, list)
    assert len(shortlist_data) == len(COGNITIVE_FRAMES)
    
    # Clean up
    if os.path.exists("tot_shortlist.json"):
        os.remove("tot_shortlist.json")

@pytest.mark.asyncio
async def test_tot_mode_switching():
    from sdk.agent.tot_ideation import get_tot_mode, run_tot_ideation, COGNITIVE_FRAMES
    import os

    # 1. Test manual environment variables
    with patch.dict(os.environ, {"TALA_TOT_MODE": "disabled"}):
        mode = await get_tot_mode()
        assert mode == "disabled"
        res = await run_tot_ideation("Refactor database schema")
        assert res == []

    with patch.dict(os.environ, {"TALA_TOT_MODE": "programmatic"}):
        mode = await get_tot_mode()
        assert mode == "programmatic"
        res = await run_tot_ideation("Refactor database schema")
        assert len(res) == len(COGNITIVE_FRAMES)
        assert res[0]["frame"] == "Extreme Modularity"

    # 2. Test auto-detection mapping
    with patch.dict(os.environ, {"TALA_TOT_MODE": "auto"}):
        # Mock DB returning a 7B model
        mock_row = {"config": json.dumps({"model": "Qwen/Qwen2.5-7B-Instruct-AWQ"})}
        with patch("python.harness.db.fetchrow", return_value=mock_row):
            mode = await get_tot_mode()
            assert mode == "fast"

        # Mock DB returning a tiny 0.5B model
        mock_row_tiny = {"config": json.dumps({"model": "Qwen/Qwen2.5-0.5B-Instruct"})}
        with patch("python.harness.db.fetchrow", return_value=mock_row_tiny):
            mode = await get_tot_mode()
            assert mode == "programmatic"

