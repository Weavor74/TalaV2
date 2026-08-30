"""
Scoring for the agent benchmark suite.

The suite itself is marked ``llm`` and takes about half an hour against a live
model, so the thing that must be gated cheaply is the *scoring*: if a skipped
case counted as a pass, the fitness function would report 6/6 for a run that
executed nothing, and every later decision built on that number would be wrong
in the same direction.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "run_benchmark_suite", REPO_ROOT / "scripts" / "run_benchmark_suite.py"
)
assert _SPEC and _SPEC.loader
rbs = importlib.util.module_from_spec(_SPEC)
# Registered before exec: @dataclass resolves annotations via
# sys.modules[cls.__module__], which is None for a spec-loaded module that has
# not been registered, and the decorator raises on the first field.
sys.modules[_SPEC.name] = rbs
_SPEC.loader.exec_module(rbs)


def _write_junit(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "report.xml"
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def test_passing_cases_score_one(tmp_path):
    path = _write_junit(
        tmp_path,
        '<testcase name="test_logic_bug" time="12.5"/>'
        '<testcase name="test_index_error" time="8.0"/>',
    )
    run = rbs.SuiteRun(cases=rbs.parse_junit(path))
    assert run.total == 2
    assert run.passed == 2
    assert run.score == 1.0
    assert run.duration == pytest.approx(20.5)


def test_failure_and_error_both_count_as_not_passed(tmp_path):
    path = _write_junit(
        tmp_path,
        '<testcase name="test_a" time="1"><failure message="assert 3 == 4"/></testcase>'
        '<testcase name="test_b" time="1"><error message="Timeout"/></testcase>'
        '<testcase name="test_c" time="1"/>',
    )
    run = rbs.SuiteRun(cases=rbs.parse_junit(path))
    assert (run.passed, run.total) == (1, 3)
    assert run.score == pytest.approx(1 / 3)

    failed = {c.name: c.detail for c in run.cases if not c.passed}
    assert "assert 3 == 4" in failed["test_a"]
    assert "Timeout" in failed["test_b"]


def test_skipped_is_not_a_pass(tmp_path):
    """
    The whole suite is `llm`-marked. A plain `pytest` run deselects it, and a
    scorer that treated skips as passes would report a perfect score for a run
    that never invoked the model -- the exact false-success shape this
    repository keeps finding.
    """
    path = _write_junit(
        tmp_path,
        '<testcase name="test_a" time="0"><skipped message="deselected"/></testcase>'
        '<testcase name="test_b" time="0"><skipped message="deselected"/></testcase>',
    )
    run = rbs.SuiteRun(cases=rbs.parse_junit(path))
    assert run.passed == 0
    assert run.score == 0.0
    assert all(not c.passed for c in run.cases)
    assert "not counted as a pass" in run.cases[0].detail


def test_empty_report_scores_zero_without_dividing(tmp_path):
    path = _write_junit(tmp_path, "")
    run = rbs.SuiteRun(cases=rbs.parse_junit(path))
    assert run.total == 0
    assert run.score == 0.0


def test_malformed_time_does_not_break_scoring(tmp_path):
    path = _write_junit(tmp_path, '<testcase name="test_a" time="not-a-number"/>')
    run = rbs.SuiteRun(cases=rbs.parse_junit(path))
    assert run.passed == 1
    assert run.duration == 0.0


def test_semantic_memory_states_the_score_and_configuration():
    """
    The node has to be retrievable on its content, so the numbers must appear
    in the prose rather than only in the state JSON, which activation does not
    read.
    """
    run = rbs.SuiteRun(
        cases=[
            rbs.CaseResult("test_logic_bug", True, 10.0),
            rbs.CaseResult("test_multi_file_edits", False, 300.0, "gave up after 15 steps"),
        ],
        label="qwen3-abliterated:4b",
        git_sha="abc1234",
        started_at="2026-08-28T12:00:00+00:00",
    )
    prose = rbs._render_semantic_memory(run)

    assert "1 of 2" in prose
    assert "0.50" in prose
    assert "qwen3-abliterated:4b" in prose
    assert "abc1234" in prose
    assert "test_multi_file_edits" in prose
    assert "gave up after 15 steps" in prose


def test_previous_runs_sort_oldest_first(tmp_path, monkeypatch):
    """A comparison against 'the last run' is only meaningful if that is really the latest."""
    import sdk.agent.documentation_nodes as docnodes

    monkeypatch.setattr(rbs, "load_previous_runs", rbs.load_previous_runs)
    monkeypatch.setattr(docnodes, "resolve_active_nodes_dir", lambda: tmp_path)

    for stamp, score in (("2026-08-27T09:00:00", 0.5), ("2026-08-28T09:00:00", 0.83)):
        rbs.write_run_node(
            rbs.SuiteRun(
                cases=[rbs.CaseResult("test_a", score > 0.6, 1.0)],
                label="test-model",
                git_sha="deadbee",
                started_at=stamp,
            )
        )

    runs = rbs.load_previous_runs()
    assert len(runs) == 2
    assert runs[0]["started_at"] < runs[1]["started_at"]


# --- engine attribution ------------------------------------------------------

def test_an_unpinned_run_makes_no_attribution_claim():
    run = rbs.SuiteRun(cases=[rbs.CaseResult("test_a", True, 1.0)])
    assert run.attribution_warning() == ""


def test_a_pinned_run_that_nothing_served_warns():
    """
    Silence is not confirmation. If no provider recorded serving a generation,
    the score belongs to no engine and must not be filed against the one that
    was merely requested.
    """
    run = rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", True, 1.0)],
        engine_requested="embedded-builtin",
    )
    warning = run.attribution_warning()
    assert "cannot be attributed" in warning


def test_a_pinned_run_that_fell_back_warns_and_names_the_other_engine():
    """
    The generation path falls through on error, so a run pinned to one engine
    can complete on another and score well. Without this the number would be
    filed against an engine that never served a token of it.
    """
    run = rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", True, 1.0)],
        engine_requested="embedded-builtin",
        providers_observed=["embedded-builtin", "ollama-qwen3-4b"],
        models_observed=["Qwen/Qwen2.5-0.5B-Instruct", "huihui_ai/qwen3-abliterated:4b"],
    )
    warning = run.attribution_warning()
    assert "ollama-qwen3-4b" in warning
    assert "not attributable" in warning


def test_a_clean_pinned_run_does_not_warn():
    run = rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", True, 1.0)],
        engine_requested="embedded-builtin",
        providers_observed=["embedded-builtin"],
        models_observed=["Qwen/Qwen2.5-0.5B-Instruct"],
    )
    assert run.attribution_warning() == ""


def test_the_node_prose_states_what_served_not_what_was_asked_for():
    run = rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", True, 1.0)],
        label="claimed-engine",
        engine_requested="embedded-builtin",
        providers_observed=["ollama-qwen3-4b"],
        models_observed=["huihui_ai/qwen3-abliterated:4b"],
        started_at="2026-08-29T09:00:00+00:00",
    )
    prose = rbs._render_semantic_memory(run)
    assert "Actually served by: huihui_ai/qwen3-abliterated:4b" in prose
    assert "ATTRIBUTION WARNING" in prose


def test_an_unattributable_run_is_never_used_as_a_baseline(tmp_path, monkeypatch):
    """
    Kept on disk, excluded from comparison. The first real run scored 0/6
    because the embedded provider raised before generating; anything measured
    against that would look like an enormous improvement over a crash.
    """
    import sdk.agent.documentation_nodes as docnodes

    monkeypatch.setattr(docnodes, "resolve_active_nodes_dir", lambda: tmp_path)

    rbs.write_run_node(rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", False, 1.0)],
        label="broken", git_sha="dead", started_at="2026-08-29T09:00:00",
        engine_requested="embedded-builtin",   # nothing observed -> warns
    ))
    rbs.write_run_node(rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", True, 1.0)],
        label="good", git_sha="beef", started_at="2026-08-29T10:00:00",
        engine_requested="embedded-builtin",
        providers_observed=["embedded-builtin"],
        models_observed=["Qwen/Qwen2.5-0.5B-Instruct"],
    ))

    runs = rbs.load_previous_runs()
    assert len(runs) == 1, "the unattributable run must not be offered as a baseline"
    assert runs[0]["model_label"] == "good"


# --- repeats -----------------------------------------------------------------

def test_repeats_score_the_mean_pass_rate_not_one_outcome():
    """
    Two identical embedded runs scored 3/6 each but flipped four of six cases,
    so a single run cannot separate a real change from variance. Flattening
    every attempt into `cases` makes `score` a mean pass rate by construction.
    """
    run = rbs.SuiteRun(
        cases=[
            rbs.CaseResult("test_a", True, 1.0), rbs.CaseResult("test_b", False, 1.0),
            rbs.CaseResult("test_a", False, 1.0), rbs.CaseResult("test_b", False, 1.0),
            rbs.CaseResult("test_a", True, 1.0), rbs.CaseResult("test_b", False, 1.0),
        ],
        repeats=3,
    )
    assert run.total == 6
    assert run.passed == 2
    assert run.score == pytest.approx(2 / 6)


def test_per_case_tallies_passes_over_attempts():
    run = rbs.SuiteRun(
        cases=[
            rbs.CaseResult("flaky", True, 2.0), rbs.CaseResult("solid", True, 1.0),
            rbs.CaseResult("flaky", False, 3.0), rbs.CaseResult("solid", True, 1.0),
        ],
        repeats=2,
    )
    tallies = {name: (p, a, s) for name, p, a, s in run.per_case()}
    assert tallies["flaky"] == (1, 2, 5.0)
    assert tallies["solid"] == (2, 2, 2.0)


def test_per_case_preserves_first_seen_order():
    run = rbs.SuiteRun(cases=[
        rbs.CaseResult("z", True, 1.0), rbs.CaseResult("a", True, 1.0),
        rbs.CaseResult("z", True, 1.0), rbs.CaseResult("a", True, 1.0),
    ], repeats=2)
    assert [n for n, *_ in run.per_case()] == ["z", "a"]


def test_a_single_run_still_reports_one_attempt_per_case():
    run = rbs.SuiteRun(cases=[rbs.CaseResult("only", True, 1.0)])
    assert run.repeats == 1
    assert run.per_case() == [("only", 1, 1, 1.0)]


# --- retention ---------------------------------------------------------------

def test_pruning_keeps_the_newest_and_removes_the_rest(tmp_path, monkeypatch):
    """
    Run nodes share the directory activation globs, so they compete for recall
    against real knowledge. Node ids are timestamped, so lexical order is
    chronological.
    """
    import sdk.agent.documentation_nodes as docnodes

    monkeypatch.setattr(docnodes, "resolve_active_nodes_dir", lambda: tmp_path)
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000"):
        (tmp_path / f"benchmark-run-{stamp}.html").write_text("x", encoding="utf-8")
    (tmp_path / "system-something.html").write_text("x", encoding="utf-8")

    removed = rbs.prune_run_nodes(keep=2)

    assert sorted(removed) == [
        "benchmark-run-20260101-000000.html",
        "benchmark-run-20260102-000000.html",
    ]
    survivors = sorted(p.name for p in tmp_path.glob("*.html"))
    assert survivors == [
        "benchmark-run-20260103-000000.html",
        "benchmark-run-20260104-000000.html",
        "system-something.html",
    ], "pruning must not touch anything that is not a run node"


def test_pruning_is_disabled_by_zero(tmp_path, monkeypatch):
    import sdk.agent.documentation_nodes as docnodes

    monkeypatch.setattr(docnodes, "resolve_active_nodes_dir", lambda: tmp_path)
    (tmp_path / "benchmark-run-20260101-000000.html").write_text("x", encoding="utf-8")

    assert rbs.prune_run_nodes(keep=0) == []
    assert (tmp_path / "benchmark-run-20260101-000000.html").exists()


def test_pruning_below_the_limit_removes_nothing(tmp_path, monkeypatch):
    import sdk.agent.documentation_nodes as docnodes

    monkeypatch.setattr(docnodes, "resolve_active_nodes_dir", lambda: tmp_path)
    (tmp_path / "benchmark-run-20260101-000000.html").write_text("x", encoding="utf-8")

    assert rbs.prune_run_nodes(keep=10) == []


# --- a sha that describes the code that ran ----------------------------------

def test_a_dirty_tree_invalidates_the_run():
    """
    A run records the sha as the configuration it measured, which is a lie
    whenever the tree is dirty -- and it is dirty exactly when the number
    matters, mid-change. Measured once: a 50-minute run stamped a sha while the
    tree held a half-finished edit that crashed every case, and recorded the
    result with nothing to say anything was wrong.
    """
    run = rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", True, 1.0)],
        git_sha="abc1234",
        uncommitted=["sdk/agent/graph_engine.py"],
    )
    warning = run.attribution_warning()
    assert "working tree was not clean" in warning
    assert "sdk/agent/graph_engine.py" in warning
    assert "abc1234" in warning


def test_the_dirty_warning_outranks_the_engine_warning():
    """
    A dirty tree invalidates a run more completely than an unknown engine: the
    engine only makes the score unattributable, while a dirty tree means the
    sha names code that never ran.
    """
    run = rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", True, 1.0)],
        engine_requested="embedded-builtin",
        uncommitted=["a.py"],
    )
    assert "working tree was not clean" in run.attribution_warning()


def test_a_dirty_run_is_never_a_baseline(tmp_path, monkeypatch):
    """It carries a warning, and warned runs are excluded from comparison."""
    import sdk.agent.documentation_nodes as docnodes

    monkeypatch.setattr(docnodes, "resolve_active_nodes_dir", lambda: tmp_path)
    rbs.write_run_node(rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", True, 1.0)],
        label="dirty", git_sha="dead", started_at="2026-08-30T09:00:00",
        uncommitted=["x.py"],
    ))
    assert rbs.load_previous_runs() == []


def test_a_clean_tree_says_nothing():
    run = rbs.SuiteRun(cases=[rbs.CaseResult("test_a", True, 1.0)], uncommitted=[])
    assert run.attribution_warning() == ""


def test_untracked_files_are_not_treated_as_dirty():
    """
    Run nodes and scratch output land in the tree constantly and describe
    nothing about the code under test. Failing on those would train everyone to
    pass --allow-dirty every time, which is worse than not checking.
    """
    import inspect

    src = inspect.getsource(rbs.uncommitted_files)
    assert "--untracked-files=no" in src


def test_the_run_records_which_files_were_dirty():
    run = rbs.SuiteRun(
        cases=[rbs.CaseResult("test_a", True, 1.0)],
        uncommitted=["one.py", "two.py"],
    )
    assert run.uncommitted == ["one.py", "two.py"]


def test_scoring_an_external_report_does_not_inherit_this_trees_state():
    """
    A report produced elsewhere -- possibly on another machine -- is not
    described by the state of this working tree. Attaching a dirty-tree warning
    to it would put a caveat on a result the caveat cannot describe.
    """
    from pathlib import Path

    src = Path("scripts/run_benchmark_suite.py").read_text(encoding="utf-8")
    assert "uncommitted=[] if args.junit else dirty," in src
