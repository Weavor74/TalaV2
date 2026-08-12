"""
The self-extension loop is defined by an executable HTML-NN
(nodes/workflow-self-extension.html) rather than hardcoded in
DAGExecutionEngine. These tests pin that contract:

- the repo's shipped node parses and its spec loads;
- a missing or invalid node falls back to the built-in chain (a bad edit
  can only revert the process, never break it);
- the chain is genuinely built FROM the spec — custom step ids, omitted
  steps, and narrowed validation checks all change real execution;
- the heal policy (feedback target) is honored from the spec.

TALA_USER_DATA is pinned in every test: resolve_active_nodes_dir() is the
single canonical load path for the node, and without the pin these tests
would read (or miss) the real install's node depending on the host.
"""

import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, patch

from sdk.agent.graph_engine import DAGExecutionEngine, GraphNode
from sdk.agent.planner import TaskItem

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_workflow_node(user_data_dir: Path, workflow: dict) -> Path:
    """Write a minimal schema-valid workflow node carrying `workflow` as its spec."""
    nodes_dir = user_data_dir / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    state = json.dumps({"title": "Self-Extension Workflow", "workflow": workflow})
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="tala-schema" content="neural-node/v2"><title>Neural Node: Self-Extension Workflow</title></head>
<body>
<article
  data-node-id="workflow-self-extension"
  data-node-type="workflow"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00">
  <header id="node-identity"><h1 id="node-title">Self-Extension Workflow</h1></header>
  <section id="semantic-memory"><p>Test copy of the self-extension workflow node.</p></section>
  <script type="application/json" id="node-state" data-cognitive-block="state">
{state}
  </script>
</article>
</body>
</html>
"""
    path = nodes_dir / "workflow-self-extension.html"
    path.write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def test_shipped_workflow_node_spec_loads(monkeypatch, tmp_path):
    """The node shipped in the repo must parse and yield the four-step chain."""
    monkeypatch.setenv("TALA_USER_DATA", str(REPO_ROOT))
    engine = DAGExecutionEngine(root_dir=str(tmp_path))

    spec = engine._load_workflow_spec()

    assert spec is not None, "repo nodes/workflow-self-extension.html did not load"
    steps = [s["step"] for s in spec["steps"]]
    assert steps == ["research", "synthesis", "writing", "validation"]
    val = spec["steps"][-1]
    assert val["config"]["checks"] == ["py_compile", "import_probe", "register_assert", "pytest"]
    assert spec["on_failure"]["feedback_to"] == "synthesis"
    assert spec["on_success"]["promote_to_workspace"] is True


def test_missing_workflow_node_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    assert engine._load_workflow_spec() is None


@pytest.mark.parametrize(
    "bad_workflow",
    [
        # unsupported node_type
        {"steps": [{"step": "a", "node_type": "teleport", "depends_on": []}]},
        # forward/unknown dependency
        {"steps": [
            {"step": "a", "node_type": "synthesis", "depends_on": ["b"]},
            {"step": "b", "node_type": "writing", "depends_on": []},
        ]},
        # duplicate step id
        {"steps": [
            {"step": "a", "node_type": "synthesis", "depends_on": []},
            {"step": "a", "node_type": "writing", "depends_on": []},
        ]},
        # bad step id
        {"steps": [{"step": "Not A Token!", "node_type": "synthesis", "depends_on": []}]},
        # no steps at all
        {"steps": []},
    ],
    ids=["unsupported-type", "forward-dep", "duplicate-id", "bad-id", "empty"],
)
def test_invalid_spec_rejected(monkeypatch, tmp_path, bad_workflow):
    """Every malformed spec falls back to the built-in chain instead of raising."""
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    _write_workflow_node(tmp_path, bad_workflow)
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    assert engine._load_workflow_spec() is None


# ---------------------------------------------------------------------------
# The spec drives real execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_built_from_custom_spec(monkeypatch, tmp_path):
    """
    A custom spec with its own step ids, no research step, and validation
    narrowed to py_compile only must change actual execution:

    - the chain runs design -> write -> check (no research node exists);
    - code with a hallucinated import PASSES, because the spec disabled the
      import probe -- proof the checks config is honored, not decorative.
    """
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    _write_workflow_node(tmp_path, {
        "flow_id": "custom",
        "steps": [
            {"step": "design", "node_type": "synthesis", "depends_on": [],
             "description": "Design for: {goal}"},
            {"step": "write", "node_type": "writing", "depends_on": ["design"]},
            {"step": "check", "node_type": "validation", "depends_on": ["write"],
             "config": {"checks": ["py_compile"]}},
        ],
        "on_failure": {"feedback_to": "design", "reset_steps": ["write"]},
        "on_success": {"promote_to_workspace": True},
    })

    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    (tmp_path / "scratch").mkdir(exist_ok=True)

    task = TaskItem(description="Write code to scratch/custom_out.py")
    task.task_id = "test-custom-spec-chain"
    task.payload = {"context": {"path": "scratch/custom_out.py"}}

    # Parses fine, but would fail the import probe if it ran.
    code = "import a_library_that_does_not_exist_xyz\n\ndef run():\n    return True\n"
    mock_llm = json.dumps({"result": {"text": f"```python\n{code}```"}})

    with patch("python.harness.server.handle_task_create_worktree",
               return_value=json.dumps({"result": {"success": True, "worktree_path": str(tmp_path)}})), \
         patch("python.harness.server.handle_inference_generate",
               new_callable=AsyncMock, return_value=mock_llm), \
         patch("sdk.agent.port_router.PortRouter.allocate_port", return_value=15000), \
         patch("sdk.agent.port_router.PortRouter.release_port", return_value=True):
        res = await engine.execute_task_as_dag(task)

    assert res["success"] is True, f"custom-spec chain failed: {res.get('error')}"
    assert (tmp_path / "scratch" / "custom_out.py").exists()

    nodes = await engine.load_nodes(task.task_id)
    node_ids = sorted(n.id for n in nodes)
    assert node_ids == [
        "check-test-custom-spec-chain",
        "design-test-custom-spec-chain",
        "write-test-custom-spec-chain",
    ]
    assert not any(n.node_type == "research" for n in nodes)


@pytest.mark.asyncio
async def test_validation_checks_config_gates_import_probe(monkeypatch, tmp_path):
    """Directly on the validation executor: same file, different checks config."""
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    target = tmp_path / "bad_import.py"
    target.write_text("import a_library_that_does_not_exist_xyz\n", encoding="utf-8")

    def _node(inputs):
        return GraphNode(id="validation-x", task_id="t-checks", node_type="validation",
                         description="validate", inputs=inputs)

    # Default (no checks key): the import probe runs and rejects the file.
    node_default = _node({"target_file": str(target), "test_file": None})
    assert await engine._execute_node(node_default, {}, {}, str(tmp_path), 0) is False
    assert "fails to import" in node_default.outputs["error"]

    # Spec-narrowed to py_compile: the same file passes, and the output says
    # plainly that the gate was narrowed.
    node_narrow = _node({"target_file": str(target), "test_file": None,
                         "checks": ["py_compile"]})
    assert await engine._execute_node(node_narrow, {}, {}, str(tmp_path), 0) is True
    assert "narrowed by workflow spec" in node_narrow.outputs["output"]
    assert "no automated tests were run" in node_narrow.outputs["output"].lower()


@pytest.mark.asyncio
async def test_heal_policy_feedback_target_from_spec(monkeypatch, tmp_path):
    """
    The heal step must honor the spec's on_failure block: error feedback
    lands on the step named by feedback_to (custom id, resolved by step id),
    and reset_steps are re-armed.
    """
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    engine._active_workflow_spec = {
        "steps": [],  # unused by _heal_node
        "on_failure": {
            "rollback_written_files": True,
            "feedback_to": "design",
            "reset_steps": ["write"],
        },
    }

    task_id = "t-heal"
    design = GraphNode(id=f"design-{task_id}", task_id=task_id, node_type="synthesis",
                       description="design", status="completed")
    write = GraphNode(id=f"write-{task_id}", task_id=task_id, node_type="writing",
                      description="write", status="completed")
    check = GraphNode(id=f"check-{task_id}", task_id=task_id, node_type="validation",
                      description="check", status="failed",
                      outputs={"error": "ModuleNotFoundError: no such module"})
    nodes = {n.id: n for n in (design, write, check)}

    # A written file that must be rolled back to its backup content.
    target_rel = "scratch/healme.py"
    target_path = tmp_path / target_rel
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("broken new content", encoding="utf-8")
    backups = {target_rel: "original content"}

    await engine._heal_node(check, nodes, backups, str(tmp_path))

    assert design.inputs.get("error_feedback") == "ModuleNotFoundError: no such module"
    assert design.status == "pending" and design.retry_count == 1
    assert write.status == "pending" and write.retry_count == 1
    assert check.status == "pending"
    assert target_path.read_text(encoding="utf-8") == "original content"


# ---------------------------------------------------------------------------
# Re-running a goal must actually re-run it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_ghost_state_does_not_skip_every_phase(monkeypatch, tmp_path):
    """
    Regression: task ids are deterministic from goal text, so every re-run of
    the same goal restored the PREVIOUS run's ghost thought-state. When that
    state pointed at (or past) the final phase, run_goal's phase loop had
    nothing left to execute and returned success having done nothing --
    confirmed live 2026-08-11: "decomposed into 2 phase(s)" immediately
    followed by "Resuming at phase index 2", then straight to
    auto-documentation, with no file written and no tool called.

    Resume exists for an INTERRUPTED run; a restored index at or beyond the
    phase count is stale and must be discarded.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from sdk.agent.goal_engine import GoalEngine
    from sdk.agent.planner import TaskItem

    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    t1 = TaskItem(description="Phase one", dependencies=[])
    t1.task_id, t1.payload = "ghost_t1", {}
    t2 = TaskItem(description="Phase two", dependencies=["ghost_t1"])
    t2.task_id, t2.payload = "ghost_t2", {}

    # Ghost state claiming the last phase (index 1) already completed, so a
    # naive resume starts at index 2 == len(tasks) and executes nothing.
    stale_state = {
        "process_context": {"idx": 1, "phase_retry_counts": {}},
        "variable_state": {
            "phases_completed": ["Phase one", "Phase two"],
            "checkpoint_ids": [],
            "phase_output_summaries": [],
        },
    }

    with patch("sdk.agent.queue.GoalTreeDecomposer.decompose", new_callable=AsyncMock, return_value=[t1, t2]), \
         patch("sdk.agent.ghost_persistence.restore_from_ghost", new_callable=AsyncMock, return_value=stale_state), \
         patch("sdk.agent.ghost_persistence.commit_to_ghost", new_callable=AsyncMock), \
         patch("sdk.agent.ghost_persistence.check_hash", new_callable=MagicMock, return_value="none"), \
         patch("sdk.agent.queue.BatchQueue.enqueue_task", new_callable=AsyncMock), \
         patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock), \
         patch("sdk.agent.persistence.TaskEventStore.append_event", new_callable=AsyncMock), \
         patch("sdk.agent.telemetry.LiveStatusReporter.broadcast_status", new_callable=AsyncMock), \
         patch("sdk.agent.goal_engine.GoalEngine._auto_document_skill", new_callable=AsyncMock), \
         patch("sdk.agent.validator.PhaseValidator.validate", new_callable=MagicMock,
               return_value=MagicMock(success=True, output="OK", error=None)), \
         patch("sdk.agent.graph_engine.DAGExecutionEngine.execute_task_as_dag",
               new_callable=AsyncMock, return_value={"success": True, "output": "ran", "error": None}) as mock_dag:
        engine = GoalEngine(root_dir=str(tmp_path))
        result = await engine.run_goal("Test Goal")

    assert result["success"] is True
    assert mock_dag.call_count == 2, (
        "stale ghost state skipped every phase — the goal reported success "
        f"having executed {mock_dag.call_count} of 2 phases"
    )
