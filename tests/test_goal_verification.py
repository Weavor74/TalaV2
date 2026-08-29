"""
Goal-level verification.

The case that motivated this is ``test_the_benchmark_case_is_caught``: a goal
that asked for one new file and two refactors, where only the new file appeared
and the run reported success anyway.

The rest of these exist to stop the cure being worse than the disease. A
verifier that guesses turns correct runs red, so most of the cases below assert
that something is *not* flagged.
"""
from __future__ import annotations

from sdk.agent.goal_verification import (
    GoalVerification,
    named_artifacts,
    snapshot,
    verify,
)


# --- what the goal names -----------------------------------------------------

def test_it_finds_the_files_a_goal_names():
    goal = ("Create a new file named common.py with a Logger class. Then refactor "
            "both user.py and order.py to import the Logger from common.py.")
    assert named_artifacts(goal) == ["common.py", "user.py", "order.py"]


def test_it_does_not_mistake_a_version_for_a_file():
    assert named_artifacts("Target the 3.11 runtime and bump to 2.5 everywhere") == []


def test_it_normalises_paths_and_drops_repeats():
    goal = "Update ./src/app.py and src\\app.py, then read src/app.py again"
    assert named_artifacts(goal) == ["src/app.py"]


# --- the failure this was built for ------------------------------------------

def test_the_benchmark_case_is_caught(tmp_path):
    """
    common.py created, user.py and order.py untouched, and the old code called
    that success.
    """
    (tmp_path / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")
    (tmp_path / "order.py").write_text("class Order:\n    pass\n", encoding="utf-8")

    goal = ("Create a new file named common.py with a Logger class. Then refactor "
            "both user.py and order.py to import the Logger from common.py.")
    names = named_artifacts(goal)
    before = snapshot(tmp_path, names)

    # what the agent actually did
    (tmp_path / "common.py").write_text("class Logger:\n    pass\n", encoding="utf-8")

    report = verify(goal, before, snapshot(tmp_path, names))

    assert report.ok is False
    assert report.created == ["common.py"]
    assert sorted(report.untouched) == ["order.py", "user.py"]
    assert "byte-identical" in report.reason
    assert "user.py" in report.reason and "order.py" in report.reason


def test_the_same_goal_done_properly_passes(tmp_path):
    (tmp_path / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")
    (tmp_path / "order.py").write_text("class Order:\n    pass\n", encoding="utf-8")

    goal = ("Create a new file named common.py with a Logger class. Then refactor "
            "both user.py and order.py to import the Logger from common.py.")
    names = named_artifacts(goal)
    before = snapshot(tmp_path, names)

    (tmp_path / "common.py").write_text("class Logger:\n    pass\n", encoding="utf-8")
    (tmp_path / "user.py").write_text("from common import Logger\n\nclass User:\n    pass\n", encoding="utf-8")
    (tmp_path / "order.py").write_text("from common import Logger\n\nclass Order:\n    pass\n", encoding="utf-8")

    report = verify(goal, before, snapshot(tmp_path, names))

    assert report.ok is True
    assert report.created == ["common.py"]
    assert sorted(report.modified) == ["order.py", "user.py"]
    assert report.untouched == []


def test_a_file_the_goal_asked_to_create_that_never_appeared_fails(tmp_path):
    goal = "Write a new file called report.py that prints a summary"
    names = named_artifacts(goal)
    before = snapshot(tmp_path, names)

    report = verify(goal, before, snapshot(tmp_path, names))

    assert report.ok is False
    assert report.missing == ["report.py"]
    assert "does not exist" in report.reason


# --- not flagging correct runs -----------------------------------------------

def test_a_read_only_goal_leaves_files_untouched_without_failing(tmp_path):
    """
    "Read user.py and explain it" names a file that *should* be unchanged.
    Flagging that would turn a correct run red -- the failure mode that makes a
    verifier worse than none.
    """
    (tmp_path / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")

    goal = "Read user.py and explain what the class is for"
    names = named_artifacts(goal)
    before = snapshot(tmp_path, names)

    report = verify(goal, before, snapshot(tmp_path, names))

    assert report.ok is True
    assert report.untouched == ["user.py"]
    assert report.reason == ""


def test_a_goal_naming_no_files_is_never_failed():
    goal = "Design a low-latency cache and explain the trade-offs"
    report = verify(goal, {}, {})
    assert report.ok is True
    assert report.named == []


def test_creation_is_checked_before_mutation(tmp_path):
    """
    A goal that creates and mutates, where the creation failed, should say the
    file is missing rather than complain that something else is unchanged --
    the missing artifact is the cause and the more useful thing to report.
    """
    (tmp_path / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")

    goal = "Create common.py then refactor user.py to import from it"
    names = named_artifacts(goal)
    before = snapshot(tmp_path, names)

    report = verify(goal, before, snapshot(tmp_path, names))

    assert report.ok is False
    assert "does not exist" in report.reason
    assert "common.py" in report.reason


def test_a_deleted_file_counts_as_missing(tmp_path):
    (tmp_path / "old.py").write_text("x = 1\n", encoding="utf-8")

    goal = "Create new.py and delete old.py"
    names = named_artifacts(goal)
    before = snapshot(tmp_path, names)
    (tmp_path / "old.py").unlink()

    report = verify(goal, before, snapshot(tmp_path, names))

    assert "old.py" in report.missing
    assert report.ok is False


def test_the_report_serialises_for_the_result_payload():
    report = GoalVerification(named=["a.py"], untouched=["a.py"], ok=False, reason="because")
    payload = report.as_dict()
    assert payload["named"] == ["a.py"]
    assert payload["ok"] is False
    assert payload["reason"] == "because"


def test_an_unreadable_root_does_not_raise(tmp_path):
    """Verification must never be the thing that fails a run."""
    result = snapshot(tmp_path / "does-not-exist", ["a.py"])
    assert result == {"a.py": None}


# --- wired into the engine ---------------------------------------------------

def test_the_engine_runs_verification_before_documenting_a_skill():
    """
    `clean_success` gates skill auto-documentation. If verification ran after
    it, a goal that half-completed would still mint a skill node claiming the
    work is reusable -- the junk-node problem, from a new direction.
    """
    from pathlib import Path

    src = Path("sdk/agent/goal_engine.py").read_text(encoding="utf-8")
    assert src.index("verification_payload = None") < src.index("clean_success = success"), (
        "goal verification must run before auto-documentation decides the goal is reusable"
    )


def test_the_engine_reports_verification_in_its_result():
    from pathlib import Path

    src = Path("sdk/agent/goal_engine.py").read_text(encoding="utf-8")
    assert '"verification": verification_payload' in src
    assert 'error_msg = f"Goal not verified: {goal_check.reason}"' in src
