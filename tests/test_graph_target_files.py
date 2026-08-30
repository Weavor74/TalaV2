"""
Which files a plan step is asking to be written.

These regexes have caused two separate real incidents: "run
scripts/generate_stories.py" produced a plan to *overwrite* that script with a
hello-world (twice, destroying a 400-line generator), and a goal naming a bare
filename fell through to scratch/output.py so the task went green while the
requested file never existed.

The third, fixed here: a step reading "refactor both user.py and order.py"
resolved to one target and wrote one file, so the benchmark asking for exactly
that failed on every engine no matter how good the plan was.

Testable without building or running a graph, which is the point of extracting
it.
"""
from __future__ import annotations

import pytest

from sdk.agent.graph_engine import DAGExecutionEngine

derive = DAGExecutionEngine.derive_target_files
DEFAULT = DAGExecutionEngine.DEFAULT_TARGET


# --- the single-file case, which is most of them ------------------------------

def test_a_named_file_is_the_target():
    assert derive("Create a new file named common.py with a Logger class") == ["common.py"]


def test_a_path_is_the_target():
    assert derive("Fix the bug in python/harness/db.py") == ["python/harness/db.py"]


def test_an_explicit_context_path_wins_outright():
    """A planned path is a decision; scraping prose is a guess."""
    assert derive("refactor user.py and order.py", explicit_path="chosen.py") == ["chosen.py"]


def test_nothing_nameable_falls_back_to_the_default():
    assert derive("Think carefully about the architecture") == [DEFAULT]


# --- the incident this must never repeat --------------------------------------

@pytest.mark.parametrize("verb", ["run", "execute", "invoke", "call"])
def test_running_a_script_never_targets_it_for_rewriting(verb):
    """
    "run scripts/generate_stories.py" produced a plan to overwrite it. That
    happened twice for real, replacing a 400-line generator with a hello-world,
    and validation compiled the placeholder and reported success.
    """
    assert derive(f"{verb} scripts/generate_stories.py and report the output") == [DEFAULT]


def test_execute_intent_also_suppresses_the_extra_targets():
    """The multi-target pass must not reintroduce what the execute guard blocks."""
    targets = derive("run scripts/a.py then run scripts/b.py")
    assert targets == [DEFAULT]


# --- the case this change adds ------------------------------------------------

def test_a_step_naming_two_files_yields_both():
    targets = derive(
        "Then refactor both user.py and order.py to import the Logger from common.py"
    )
    assert "user.py" in targets
    assert "order.py" in targets


def test_the_first_target_is_unchanged_by_the_multi_pass():
    """
    The primary is still whatever the original scrape chose, so a single-file
    step builds exactly the graph it always did.
    """
    assert derive("Create a new file named common.py with a Logger class")[0] == "common.py"


def test_extra_targets_come_from_the_step_not_the_whole_goal():
    """
    Pulling extras from the goal would make an early phase write files a later
    phase owns -- the create-then-refactor plan would have phase one touching
    all three.
    """
    goal = ("Create a new file named common.py with a Logger class. Then refactor "
            "both user.py and order.py to import the Logger from common.py.")
    targets = derive("Create a new file named common.py with a Logger class", goal)
    assert targets == ["common.py"], f"phase one should own one file, got {targets}"


def test_no_duplicates():
    targets = derive("update config.json, then re-check config.json")
    assert targets.count("config.json") == 1


def test_scratch_paths_are_not_added_as_extra_targets():
    targets = derive("Write report.py using the data in scratch/tmp.py")
    assert targets[0] == "report.py"
    assert not any(t.startswith("scratch/") for t in targets[1:])


def test_every_target_is_a_plausible_filename():
    """A stray capture with no extension would be written as a real file."""
    for text in (
        "Create a new file named common.py with a Logger class",
        "Then refactor both user.py and order.py to import the Logger",
        "Fix python/harness/db.py and tests/test_db.py",
    ):
        for target in derive(text):
            assert "." in target, f"{target!r} from {text!r} is not a filename"
            assert " " not in target, f"{target!r} contains a space"


# --- the import probe must be able to see the files the goal wrote ------------

def test_the_import_probe_puts_the_files_own_directory_on_sys_path():
    """
    The probe loads the file from the worktree but only put root_dir on
    sys.path, so a module importing a sibling the same goal had just written
    could never resolve it. "Refactor user.py to import the Logger from
    common.py" wrote both into the worktree and then imported user.py with
    only the checkout importable -- it failed however correct the code was,
    and each failure triggered a rollback and a fresh synthesis, which is what
    exhausted the time budget.
    """
    from pathlib import Path

    src = Path("sdk/agent/graph_engine.py").read_text(encoding="utf-8")
    assert "sys.path.insert(0,r'{target_path.parent}')" in src

    # And ahead of root_dir, which is inserted first and therefore ends up
    # behind it -- the sibling the goal just wrote must win over a same-named
    # file in the checkout.
    root_insert = src.index("sys.path.insert(0,r'{self.root_dir or '.'}')")
    own_insert = src.index("sys.path.insert(0,r'{target_path.parent}')")
    assert root_insert < own_insert, "the file's own directory must be inserted last to sit first"


# --- a file the step reads is not a file the step writes ----------------------

def test_an_imported_dependency_is_not_a_write_target():
    """
    "refactor both user.py and order.py to import the Logger from common.py"
    names three files and modifies two. Treating common.py as a target made
    this phase rewrite what the previous phase had just created, and when
    validation failed the rollback deleted it -- so the dependency the refactor
    needed vanished and every retry failed the same way. Measured: common.py
    absent from the workspace after the run.
    """
    targets = derive(
        "Then refactor both user.py and order.py to import the Logger from common.py"
    )
    assert "user.py" in targets
    assert "order.py" in targets
    assert "common.py" not in targets, "the file being imported from must not be overwritten"


@pytest.mark.parametrize("phrase", [
    "refactor app.py using helpers.py",
    "update app.py reading config.json",
    "rewrite app.py importing shared.py",
])
def test_reading_contexts_do_not_create_targets(phrase):
    targets = derive(phrase)
    assert targets[0] == "app.py"
    assert len(targets) == 1, f"{phrase!r} produced {targets}"
