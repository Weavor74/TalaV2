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

def test_is_architectural_or_decomposition_prompt_ignores_generic_task_framing():
    """
    Regression: caught live -- a two-sentence request to "Plan a simple CLI
    calculator ... as a short design note" was classified as architectural
    purely because it said "plan"/"design", hijacking a trivial task into
    the full ToT ideation pipeline and producing wildly irrelevant
    "blueprint process" output. Generic planning/design verbs must not
    trigger this on their own -- only when paired with an actual technical
    scope noun (module, schema, microservices, etc.), which the existing
    True-case assertions above already exercise.
    """
    assert is_architectural_or_decomposition_prompt(
        "Plan a simple CLI calculator (add, subtract, multiply, divide) as a short "
        "design note — pick specific function names and file layout, then implement "
        "exactly that design in scratch/calculator.py."
    ) is False
    assert is_architectural_or_decomposition_prompt(
        "Design a short note about how this function works."
    ) is False
    assert is_architectural_or_decomposition_prompt(
        "Let's plan out a strategy for the roadmap of this blueprint."
    ) is False, "generic connector words stacked together still aren't a technical scope signal"

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
async def test_goal_tree_decomposer_intercept(tmp_path, monkeypatch):
    # Isolate the HTML-NN learning node this now also creates (see
    # sdk/agent/queue.py) -- without this, create_learning_documentation_node
    # resolves its nodes/ directory via TALA_USER_DATA/APPDATA and would
    # write straight into the real live %APPDATA%\tala-engine\nodes\ on
    # every test run, same class of live-pollution bug fixed elsewhere this
    # session (see self-documenter-learning-node-root-dir-gap).
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    decomposer = GoalTreeDecomposer()

    # A *reasoned* shortlist, because a fallback one is deliberately not filed
    # as memory any more: its critiques are hardcoded per cognitive frame and
    # its scores are arithmetic over those constants. This test's subject is
    # the shortlist reaching the wiki -- staging, promotion, content -- so it
    # supplies reasoning rather than asserting that scaffolding gets kept.
    reasoned = [
        {
            "approach_name": f"Approach for frame {frame}",
            "frame": frame,
            "score": 9.0 - index,
            "architectural_strategy": "A considered strategy for replication.",
            "tasks": ["Implement replication", "Verify failover"],
            "risk_matrix": [
                {"risk": "Split brain", "impact": "High", "likelihood": "Low",
                 "mitigation": "Quorum writes."},
                {"risk": "Lag", "impact": "Medium", "likelihood": "Medium",
                 "mitigation": "Backpressure."},
                {"risk": "Data loss", "impact": "High", "likelihood": "Low",
                 "mitigation": "Synchronous commit."},
            ],
            "trap_list": ["Do not assume the network is reliable."],
        }
        for index, frame in enumerate(COGNITIVE_FRAMES)
    ]

    # The shortlist follows TALA_USER_DATA, so it lands in tmp_path with the
    # nodes. It used to be written with a bare relative path -- straight into
    # whatever directory the process started from, which under pytest is the
    # checkout, leaving `git status` dirty after every run. The cleanup this
    # test used to perform here (deleting tot_shortlist.json from the cwd
    # before asserting) was itself the symptom.
    shortlist_file = tmp_path / "tot_shortlist.json"
    cwd_shortlist_before = os.path.exists("tot_shortlist.json")

    user_goal = "Design a fault-tolerant database replication module"

    with patch("sdk.agent.tot_ideation.run_tot_ideation", return_value=reasoned):
        tasks = await decomposer.decompose(user_goal)

    # Verify tasks are returned representing the top ToT approach
    assert isinstance(tasks, list)
    assert len(tasks) > 0

    # Verify tot_shortlist.json was written, under the data root and not the
    # working directory.
    assert shortlist_file.exists(), f"expected the shortlist at {shortlist_file}"
    # Asserted as "did not appear during this test", not "is absent from the
    # working directory". When TALA_USER_DATA is unset the working directory
    # *is* the data root -- that is the portable design -- so the file
    # legitimately lives there in ordinary use, and anything another process
    # left behind would otherwise fail this.
    assert os.path.exists("tot_shortlist.json") == cwd_shortlist_before, (
        "the shortlist was written to the process working directory; it belongs "
        "under the resolved user-data root so it travels with the install"
    )
    shortlist_data = json.loads(shortlist_file.read_text(encoding="utf-8"))
    assert isinstance(shortlist_data, list)
    assert len(shortlist_data) == len(COGNITIVE_FRAMES)

    # Verify the shortlist was ALSO persisted as a durable HTML-NN learning
    # node -- not just the disposable scratch JSON file.
    #
    # Learning nodes now serve their probation in nodes/_staging first (see
    # sdk/agent/node_staging.py), so "durable" means staged here and
    # "searchable" means promoted after the required passing cycles. Both
    # halves are asserted: landing in staging alone would satisfy durability
    # while quietly never reaching the wiki, which is the failure this test
    # exists to catch.
    from sdk.agent.node_staging import promotion_passes, run_verification_cycle

    staged = list((tmp_path / "nodes" / "_staging").glob("learning-*.html"))
    assert len(staged) == 1, f"expected exactly one staged learning node, found {staged}"

    # Dwell is a wall-clock policy tested in tests/test_node_staging.py; this
    # test is about the shortlist reaching the wiki at all, so waive it here
    # rather than sleeping out the probation period.
    monkeypatch.setenv("TALA_NODE_PROMOTION_MIN_MINUTES", "0")
    for _ in range(promotion_passes()):
        run_verification_cycle()

    learning_nodes = list((tmp_path / "nodes").glob("learning-*.html"))
    assert len(learning_nodes) == 1, (
        f"staged learning node never reached the wiki, found {learning_nodes}"
    )

    html = learning_nodes[0].read_text(encoding="utf-8")
    # Assert the node's substance, not one exact sentence. It must read as a
    # decision record -- the shortlist and its scores -- rather than a
    # restatement of the goal, which is what assess_learning_quality rejects.
    assert "Architectural approaches weighed for" in html
    assert "decision-record" in html
    assert "cognitive frames" in html

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



def test_fallback_shortlist_is_stamped_as_such():
    """
    The fallback critiques are hardcoded per cognitive frame and its scores are
    arithmetic over those constants -- nothing looked at the problem. Callers
    have to be able to tell that apart from reasoning.
    """
    from sdk.agent.tot_ideation import (
        _generate_fallback_approaches,
        _generate_fallback_critic_eval,
        _generate_fallback_shortlist,
        is_fallback_shortlist,
    )

    approaches = _generate_fallback_approaches("Design a cache")
    evaluated = [
        {"approach": a, "frame": a["frame"], "evaluation": _generate_fallback_critic_eval(a)}
        for a in approaches
    ]
    shortlist = _generate_fallback_shortlist("Design a cache", evaluated)

    assert shortlist, "the fallback must still produce a usable plan"
    assert all(entry["source"] == "fallback" for entry in shortlist)
    assert is_fallback_shortlist(shortlist) is True


def test_a_reasoned_shortlist_is_not_treated_as_fallback():
    from sdk.agent.tot_ideation import is_fallback_shortlist

    assert is_fallback_shortlist([{"approach_name": "Real", "score": 8}]) is False
    assert is_fallback_shortlist(
        [{"approach_name": "Real", "score": 8}, {"source": "fallback"}]
    ) is False, "a mixed shortlist contains real reasoning and should be kept"


def test_an_empty_shortlist_counts_as_fallback():
    from sdk.agent.tot_ideation import is_fallback_shortlist

    assert is_fallback_shortlist([]) is True


def test_the_decomposer_does_not_file_a_fallback_shortlist_as_memory():
    """
    Confirmed live: a run against an unreachable model promoted four invented
    architectures with invented scores into production, beside the genuine
    lesson from the same goal, competing with it for recall.
    """
    from pathlib import Path

    src = Path("sdk/agent/queue.py").read_text(encoding="utf-8")
    assert "shortlist_is_reasoned = not is_fallback_shortlist(shortlist)" in src
    assert src.index("if shortlist_is_reasoned:") < src.index("create_learning_documentation_node(\n")


@pytest.mark.asyncio
async def test_a_fallback_shortlist_still_plans_but_writes_no_memory(tmp_path, monkeypatch):
    """
    The counterpart to the test above. With no model reachable, ToT falls back
    to a fixed template -- and the plan still has to work, because that is what
    the fallback is for. What must not happen is the template being filed as a
    durable learning event, which is how four invented architectures with
    invented scores ended up promoted into production beside a real lesson.
    """
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    decomposer = GoalTreeDecomposer()
    tasks = await decomposer.decompose(
        "Design a fault-tolerant database replication module"
    )

    assert tasks, "the fallback must still produce a usable plan"

    staged = list((tmp_path / "nodes" / "_staging").glob("learning-*.html"))
    promoted = list((tmp_path / "nodes").glob("learning-*.html"))
    assert staged == [], f"fallback scaffolding was staged as a learning node: {staged}"
    assert promoted == [], f"fallback scaffolding reached the wiki: {promoted}"
