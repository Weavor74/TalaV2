"""
test_goal_engine.py - Auto-generated documentation base.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from sdk.agent.goal_engine import GoalEngine, _format_prior_phase_summary
from sdk.agent.planner import TaskItem
from sdk.agent.batch_policy import BatchPolicy


def test_format_prior_phase_summary_caps_phase_count_and_length():
    """
    The whole point of this summary is to stay bounded regardless of how
    many phases a goal has or how verbose any one phase's output is --
    otherwise "thread prior output forward" just re-creates the unbounded
    context growth it exists to avoid.
    """
    summaries = [
        {"description": f"Phase {i}", "output": f"output-{i} " + ("x" * 1000)}
        for i in range(10)
    ]
    text = _format_prior_phase_summary(summaries)

    # Only the most recent 3 phases should appear.
    assert "Phase 9" in text
    assert "Phase 8" in text
    assert "Phase 7" in text
    assert "Phase 6" not in text
    assert "Phase 0" not in text

    # Each phase's output must be truncated, not dumped in full.
    assert "... (truncated)" in text
    for i in (7, 8, 9):
        segment_start = text.index(f"Phase {i}")
        segment_end = text.index("\n", segment_start) if "\n" in text[segment_start:] else len(text)
        assert segment_end - segment_start < 500


def test_format_prior_phase_summary_handles_non_string_and_missing_output():
    summaries = [
        {"description": "Struct phase", "output": {"file": "a.py", "success": True}},
        {"description": "No-output phase", "output": None},
    ]
    text = _format_prior_phase_summary(summaries)
    assert "Struct phase" in text
    assert "a.py" in text  # dict output gets stringified, not dropped
    assert "No-output phase" in text
    assert "(no output recorded)" in text

@pytest.fixture
def dummy_tasks():
    t1 = TaskItem(description="Task 1", dependencies=[])
    t1.task_id = "task_1"
    t1.payload = {}
    
    t2 = TaskItem(description="Task 2", dependencies=["task_1"])
    t2.task_id = "task_2"
    t2.payload = {}
    
    return [t1, t2]

@pytest.mark.asyncio
@patch("sdk.agent.queue.GoalTreeDecomposer.decompose", new_callable=AsyncMock)
@patch("sdk.agent.validator.PhaseValidator.validate", new_callable=MagicMock)
@patch("sdk.agent.queue.BatchQueue.enqueue_task", new_callable=AsyncMock)
@patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
@patch("sdk.agent.persistence.TaskEventStore.append_event", new_callable=AsyncMock)
@patch("sdk.agent.telemetry.LiveStatusReporter.broadcast_status", new_callable=AsyncMock)
@patch("sdk.agent.goal_engine.GoalEngine._auto_document_skill", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.commit_to_ghost", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.restore_from_ghost", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.check_hash", new_callable=MagicMock)
@patch("sdk.agent.graph_engine.DAGExecutionEngine.execute_task_as_dag", new_callable=AsyncMock)
async def test_run_goal_success(
    mock_dag, mock_check_hash, mock_restore, mock_commit_ghost, mock_auto_doc, mock_broadcast,
    mock_append, mock_transition, mock_enqueue, mock_validate, mock_decompose, dummy_tasks
):
    # This test used to mock TalaExecutionDispatcher.dispatch and assert it
    # ran twice — but run_goal has executed phases through
    # DAGExecutionEngine.execute_task_as_dag since the DAG refactor, so the
    # dispatcher mock sat unused (call_count 0) while the two tasks silently
    # ran through the REAL DAG engine, live LLM calls and all. Mock the
    # phase executor run_goal actually uses, same harness as
    # test_run_goal_threads_prior_phase_output_forward.
    mock_restore.return_value = None
    mock_check_hash.return_value = "none"
    mock_decompose.return_value = dummy_tasks
    mock_dag.return_value = {"success": True, "output": "Success", "error": None}

    val_result = MagicMock()
    val_result.success = True
    val_result.output = "OK"
    val_result.error = None
    mock_validate.return_value = val_result

    engine = GoalEngine()
    policy = BatchPolicy.auto()

    result = await engine.run_goal("Test Goal", policy=policy)

    assert result["success"] is True
    assert len(result["phases_completed"]) == 2
    assert "Task 1" in result["phases_completed"]
    assert "Task 2" in result["phases_completed"]

    # Verify one DAG execution per phase
    assert mock_dag.call_count == 2

    # Verify enqueue and transitions
    assert mock_enqueue.call_count >= 2

@pytest.mark.asyncio
@patch("sdk.agent.queue.GoalTreeDecomposer.decompose", new_callable=AsyncMock)
@patch("sdk.agent.dispatcher.TalaExecutionDispatcher.dispatch", new_callable=AsyncMock)
@patch("sdk.agent.validator.PhaseValidator.validate", new_callable=MagicMock)
@patch("sdk.agent.ghost_persistence.restore_from_ghost", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.check_hash", new_callable=MagicMock)
async def test_run_goal_failure_and_rollback(
    mock_check_hash, mock_restore, mock_validate, mock_dispatch, mock_decompose, dummy_tasks
):
    mock_restore.return_value = None
    mock_check_hash.return_value = "none"
    mock_decompose.return_value = dummy_tasks
    
    call_count = {"dispatch": 0, "validate": 0}
    
    async def mock_dispatch_fn(*args, **kwargs):
        call_count["dispatch"] += 1
        if call_count["dispatch"] == 1:
            return {"command": "echo", "stdout": "Success", "success": True}
        return {"command": "fail", "stderr": "Error", "success": False, "error": "Crash"}
        
    def mock_validate_fn(task, res):
        call_count["validate"] += 1
        if call_count["validate"] == 1:
            return MagicMock(success=True, output="OK", error=None)
        return MagicMock(success=False, output="", error="Crash")

    mock_dispatch.side_effect = mock_dispatch_fn
    mock_validate.side_effect = mock_validate_fn
    
    engine = GoalEngine()
    
    with patch.object(engine.recovery, "create_checkpoint", return_value="chk_123"):
        with patch.object(engine.recovery, "restore_checkpoint") as mock_restore_cp:
            result = await engine.run_goal("Test Goal")

            # Goal fails because second task fails and decomposer fails to generate a fix
            assert result["success"] is False
            assert "Crash" in result["error"]
            assert mock_restore_cp.call_count >= 1


@pytest.mark.asyncio
@patch("sdk.agent.queue.GoalTreeDecomposer.decompose", new_callable=AsyncMock)
@patch("sdk.agent.validator.PhaseValidator.validate", new_callable=MagicMock)
@patch("sdk.agent.queue.BatchQueue.enqueue_task", new_callable=AsyncMock)
@patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
@patch("sdk.agent.persistence.TaskEventStore.append_event", new_callable=AsyncMock)
@patch("sdk.agent.telemetry.LiveStatusReporter.broadcast_status", new_callable=AsyncMock)
@patch("sdk.agent.goal_engine.GoalEngine._auto_document_skill", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.commit_to_ghost", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.restore_from_ghost", new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.check_hash", new_callable=MagicMock)
@patch("sdk.agent.graph_engine.DAGExecutionEngine.execute_task_as_dag", new_callable=AsyncMock)
async def test_run_goal_threads_prior_phase_output_forward(
    mock_dag, mock_check_hash, mock_restore, mock_commit_ghost, mock_auto_doc, mock_broadcast,
    mock_append, mock_transition, mock_enqueue, mock_validate, mock_decompose, dummy_tasks
):
    """
    Regression: downstream phases used to only ever see the original goal
    text and their own description -- never what an earlier phase in the
    same goal actually produced. A "coder" phase had no visibility into
    what a preceding "planner" phase decided, so it re-derived from scratch
    instead of building on prior work. Fixed by threading a curated summary
    of completed phases into task.payload["context"]["prior_phase_summary"]
    before each phase executes.
    """
    mock_restore.return_value = None
    mock_check_hash.return_value = "none"
    mock_decompose.return_value = dummy_tasks

    # Phase 1's DAG result carries a distinctive marker; phase 2's DAG call
    # should receive it via its task's payload context.
    mock_dag.side_effect = [
        {"success": True, "output": "PHASE_ONE_DECIDED_XYZ", "error": None},
        {"success": True, "output": "phase two output", "error": None},
    ]

    def _validate_side_effect(task, res):
        # Echo the DAG's actual output through instead of a fixed stub, so
        # the distinctive marker above survives into phase_output_summaries.
        return MagicMock(success=True, output=res.get("output"), error=None)

    mock_validate.side_effect = _validate_side_effect

    engine = GoalEngine()
    policy = BatchPolicy.auto()
    result = await engine.run_goal("Test Goal", policy=policy)

    assert result["success"] is True
    assert mock_dag.call_count == 2

    # Phase 1 is first -- nothing preceded it, so no summary should be injected.
    phase1_task = mock_dag.call_args_list[0][0][0]
    assert "prior_phase_summary" not in (phase1_task.payload.get("context") or {})

    # Phase 2 must see phase 1's actual output, not just the original goal text.
    phase2_task = mock_dag.call_args_list[1][0][0]
    summary = phase2_task.payload["context"]["prior_phase_summary"]
    assert "PHASE_ONE_DECIDED_XYZ" in summary
    assert "Task 1" in summary
