"""
Distilling a completed run into a lesson.

The load-bearing test here is ``test_every_built_lesson_passes_the_real_gate``:
this module exists to feed ``create_learning_documentation_node``, and a
distiller whose output the wiki gate rejects is the same dead path as before,
just with more code in front of it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sdk.agent.documentation_nodes import _TASK_IMPERATIVES, assess_learning_quality
from sdk.agent.task_lessons import (
    TaskOutcome,
    assess_task_worth_recording,
    build_task_lesson,
)


def _struggled() -> TaskOutcome:
    return TaskOutcome(
        goal_text="Fix the failing assertion in the payload decoder",
        phases=[
            "a (tool): Tool 'read_workspace_file' executed.",
            "b (tool): Tool 'code_apply_patch' executed.",
            "c (verify): gate_failed -- the patch did not compile",
            "d (tool): Tool 'code_apply_patch' executed.",
            "e (verify): tests passed",
        ],
        tests_status="passed after retry",
        changed_files=["python/harness/db.py"],
    )


def _trivial() -> TaskOutcome:
    return TaskOutcome(
        goal_text="Echo the input payload",
        phases=[
            "t (trigger): Trigger captured input payload.",
            "a (tool): Tool 'echo' executed.",
        ],
        tests_status="Workflow completed successfully",
    )


# --- deciding whether there is a lesson at all -------------------------------

def test_a_trivial_run_is_declined():
    """Completing a task is not learning something -- the whole point of the gate."""
    reason = assess_task_worth_recording(_trivial())
    assert reason is not None
    assert "trivial" in reason
    assert build_task_lesson(_trivial()) is None


def test_a_run_that_failed_and_recovered_is_worth_recording():
    assert assess_task_worth_recording(_struggled()) is None


def test_a_short_run_that_failed_is_still_worth_recording():
    """
    Two phases would normally be declined, but a failure that was overcome is
    the transferable part, so the phase count must not veto it.
    """
    outcome = TaskOutcome(
        goal_text="Install the training extra",
        phases=["a (tool): Tool 'run_workspace_command' executed.",
                "b (verify): failed -- no matching platform tag"],
        tests_status="failed",
    )
    assert assess_task_worth_recording(outcome) is None


def test_a_goal_with_no_text_is_declined():
    assert assess_task_worth_recording(TaskOutcome(goal_text="  ")) is not None


def test_a_long_run_with_no_tools_files_or_failures_is_declined():
    outcome = TaskOutcome(
        goal_text="Think about the architecture",
        phases=["a (think): considered", "b (think): considered", "c (think): considered"],
    )
    reason = assess_task_worth_recording(outcome)
    assert reason is not None
    assert "nothing observable" in reason
    assert build_task_lesson(outcome) is None


# --- the lesson itself -------------------------------------------------------

def test_the_lesson_records_what_failed_and_what_followed():
    lesson = build_task_lesson(_struggled())
    assert lesson is not None
    joined = " ".join(str(d) for d in lesson["details"])

    assert "did not compile" in joined
    assert "code_apply_patch" in joined
    assert "python/harness/db.py" in joined
    assert "Did not work first time" in joined


def test_the_topic_is_a_named_thing_not_an_errand():
    lesson = build_task_lesson(_struggled())
    topic = str(lesson["topic"])

    # The goal was "Fix the failing assertion..."; the lesson must not inherit
    # that leading imperative, which the wiki gate rejects on sight.
    assert not topic.lower().startswith("fix")
    assert "the failing assertion in the payload decoder" in topic.lower()


def test_tools_are_listed_in_order_without_repeats():
    lesson = build_task_lesson(_struggled())
    tools_line = next(d for d in lesson["details"] if str(d).startswith("Tools used"))
    assert tools_line == "Tools used, in order: read_workspace_file, code_apply_patch"


@pytest.mark.parametrize(
    "outcome",
    [
        _struggled(),
        TaskOutcome(
            goal_text="Migrate the settings table",
            phases=[
                "a (tool): Tool 'database_query' executed.",
                "b (tool): Tool 'code_write_files' executed.",
                "c (verify): tests passed",
            ],
            changed_files=["python/harness/db.py"],
            tests_status="passed",
        ),
        TaskOutcome(
            goal_text="Run the nonexistent migration script",
            phases=["a (tool): Tool 'shell' executed.", "b: error -- file not found"],
        ),
    ],
    ids=["recovered", "multi-step", "poison-word-in-goal"],
)
def test_every_built_lesson_passes_the_real_gate(outcome):
    """
    Anything this module emits must survive assess_learning_quality unforced.

    The third case matters on its own: the gate rejects any topic containing
    "none" as a substring, which "nonexistent" satisfies by accident. A lesson
    thrown out for that would be silently lost.
    """
    lesson = build_task_lesson(outcome)
    assert lesson is not None, "expected this run to be worth recording"

    rejection = assess_learning_quality(
        str(lesson["topic"]), str(lesson["summary"]), lesson["details"]
    )
    assert rejection is None, f"the wiki gate rejected our own lesson: {rejection}"


def test_the_summary_is_not_the_old_boilerplate():
    lesson = build_task_lesson(_struggled())
    assert "distilled a reusable learning event" not in str(lesson["summary"])


@pytest.mark.parametrize("verb", sorted(_TASK_IMPERATIVES))
def test_no_imperative_can_ever_lead_the_topic(verb):
    """
    The topic is prefixed with a noun by construction, so this holds for every
    verb the gate rejects -- including ones no list here has to know about. An
    earlier version stripped a hardcoded set of verbs instead, and "repair" was
    not in it.
    """
    outcome = TaskOutcome(
        goal_text=f"{verb.capitalize()} the payload decoder",
        phases=["a (tool): Tool 'shell' executed.", "b: error -- failed"],
    )
    lesson = build_task_lesson(outcome)
    assert lesson is not None
    assert assess_learning_quality(
        str(lesson["topic"]), str(lesson["summary"]), lesson["details"]
    ) is None


# --- end to end through SelfDocumenter ---------------------------------------

def test_a_trivial_goal_writes_a_skill_node_but_no_lesson(tmp_path, monkeypatch):
    """
    The skill node still records that the work happened. Only the "lesson" is
    withheld, because there wasn't one.
    """
    from sdk.agent.self_documenter import SelfDocumenter

    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    result = SelfDocumenter(str(tmp_path)).document_completed_goal(
        goal_text="Summarise the inbound payload for the operator",
        phases_completed=["t (trigger): Trigger captured input payload.",
                          "a (tool): Tool 'echo' executed."],
        task_id="trivial-1",
        tests_status="Workflow completed successfully",
    )

    assert result["skill_node_path"]
    assert result["learning_node_path"] == ""
    assert not list(tmp_path.rglob("learning-*.html"))


def test_a_goal_that_failed_and_recovered_writes_a_lesson(tmp_path, monkeypatch):
    from sdk.agent.self_documenter import SelfDocumenter

    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    result = SelfDocumenter(str(tmp_path)).document_completed_goal(
        goal_text="Repair the payload decoder so the operator sees whole records",
        phases_completed=[
            "a (tool): Tool 'read_workspace_file' executed.",
            "b (tool): Tool 'code_apply_patch' executed.",
            "c (verify): gate_failed -- the patch did not compile",
            "d (tool): Tool 'code_apply_patch' executed.",
            "e (verify): tests passed",
        ],
        task_id="recovered-1",
        changed_files=["python/harness/db.py"],
        tests_status="passed after retry",
    )

    assert result["learning_node_path"], "a run that failed and recovered should leave a lesson"

    written = Path(result["learning_node_path"]).read_text(encoding="utf-8")
    assert "did not compile" in written
    assert "distilled a reusable learning event" not in written


# --- the gate that made this unreachable -------------------------------------

def test_the_engine_records_a_lesson_when_a_phase_was_retried():
    """
    Auto-documentation is gated on `clean_success = success and
    len(phase_retry_counts) == 0`. A run that failed and recovered therefore
    never reached any lesson path -- while the distiller declines every run
    where nothing failed. The two conditions were mutually exclusive, so the
    lesson path could not fire from a real goal run at all.
    """
    src = Path("sdk/agent/goal_engine.py").read_text(encoding="utf-8")

    assert "record_task_lesson" in src, "the engine must reach the lesson path"
    gate = src.index("if phase_retry_counts or (verification_payload")
    documentation = src.index("clean_success and not has_workaround_lang")
    assert gate > documentation, (
        "lesson distillation must be independent of the clean-success gate, "
        "not nested inside it"
    )


def test_record_task_lesson_declines_a_run_with_nothing_to_teach():
    from sdk.agent.task_lessons import record_task_lesson

    result = record_task_lesson(_trivial())
    assert result["status"] == "skipped"
    assert "trivial" in result["reason"]


def test_record_task_lesson_writes_when_a_run_recovered(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    from sdk.agent.task_lessons import record_task_lesson

    result = record_task_lesson(_struggled())
    assert result.get("path"), f"expected a lesson node, got {result}"
    written = Path(result["path"]).read_text(encoding="utf-8")
    assert "did not compile" in written


def test_record_task_lesson_never_raises(monkeypatch):
    """A learning that cannot be filed must not take down the run."""
    import sdk.agent.documentation_nodes as docnodes

    def _boom(**_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(docnodes, "create_learning_documentation_node", _boom)
    from sdk.agent.task_lessons import record_task_lesson

    result = record_task_lesson(_struggled())
    assert result["status"] == "error"
    assert "disk on fire" in result["reason"]
