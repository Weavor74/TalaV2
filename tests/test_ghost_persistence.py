import pytest
import os
import json
from pathlib import Path

from sdk.agent.ghost_persistence import commit_to_ghost, restore_from_ghost
from python.harness.db import init_pool, close_pool

@pytest.mark.asyncio
async def test_ghost_persistence():
    # Initialize DB pool
    await init_pool()
    try:
        task_id = "test-task-id-12345"
        context = {"idx": 0, "parent": "goal-parent"}
        var_state = {"phases": ["phase 1"]}
        next_step_hash = "abc123nextstep"
        
        # Commit to ghost
        tx_id = await commit_to_ghost(task_id, context, var_state, next_step_hash)
        assert tx_id != "unanchored"
        
        # Restore from ghost
        restored = await restore_from_ghost(task_id)
        assert restored is not None
        assert restored["task_id"] == task_id
        assert restored["process_context"]["parent"] == "goal-parent"
        assert restored["variable_state"]["phases"] == ["phase 1"]
        assert restored["intended_next_step_hash"] == next_step_hash
    finally:
        await close_pool()
