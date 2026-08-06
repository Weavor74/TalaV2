"""
test_safe_code_bridges.py

End-to-end coverage for the self-improvement bridge: the bounded-patch +
dry-run + validated-execution pipeline that lets Tala modify its own
codebase. This exercises the real functions against a real (temporary)
workspace on disk rather than mocking them, because the property under
test is "does dry_run actually leave disk untouched / does a real run
actually apply the change" -- a mock would hide exactly the class of bug
this file was written to catch (see execute_self_improvement_goal fix:
dry_run was being silently forced to True on every "real" execution
request, and non-documentation goals could never apply a concrete change
because replacements/file_writes were never threaded through).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sdk.agent import safe_code_bridges as bridges


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(bridges, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def readme(workspace):
    path = workspace / "README.md"
    path.write_text("Hello world\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Workspace containment
# ---------------------------------------------------------------------------

def test_resolve_workspace_path_rejects_absolute_path(workspace):
    with pytest.raises(ValueError, match="absolute paths"):
        bridges.resolve_workspace_path(str(workspace / "x.py"))


def test_resolve_workspace_path_rejects_traversal_escape(workspace):
    with pytest.raises(ValueError, match="escapes workspace"):
        bridges.resolve_workspace_path("../outside.py")


def test_ensure_writable_workspace_path_blocks_dangerous_dirs(workspace):
    with pytest.raises(ValueError, match="blocked workspace segment"):
        bridges._ensure_writable_workspace_path("data/config.py")
    with pytest.raises(ValueError, match="blocked workspace segment"):
        bridges._ensure_writable_workspace_path(".git/hooks/pre-commit.py")


def test_ensure_writable_workspace_path_blocks_disallowed_extension(workspace):
    with pytest.raises(ValueError, match="extension is not allowed"):
        bridges._ensure_writable_workspace_path("run.exe")


# ---------------------------------------------------------------------------
# apply_patch: dry_run must be inert, real run must actually mutate disk
# ---------------------------------------------------------------------------

def test_apply_patch_dry_run_leaves_disk_untouched(readme):
    result = bridges.apply_patch(
        [{"file_path": "README.md", "old_text": "Hello world", "new_text": "Goodbye world"}],
        dry_run=True,
    )
    assert result["success"] is True
    assert readme.read_text(encoding="utf-8") == "Hello world\n"


def test_apply_patch_real_run_writes_file(readme):
    result = bridges.apply_patch(
        [{"file_path": "README.md", "old_text": "Hello world", "new_text": "Goodbye world"}],
        dry_run=False,
    )
    assert result["success"] is True
    assert readme.read_text(encoding="utf-8") == "Goodbye world\n"


def test_apply_patch_rejects_ambiguous_match(workspace):
    path = workspace / "dupe.py"
    path.write_text("x = 1\nx = 1\n", encoding="utf-8")
    result = bridges.apply_patch(
        [{"file_path": "dupe.py", "old_text": "x = 1", "new_text": "x = 2"}],
        dry_run=False,
    )
    assert result["success"] is False
    assert "expected exactly 1" in result["errors"][0]
    assert path.read_text(encoding="utf-8") == "x = 1\nx = 1\n"


# ---------------------------------------------------------------------------
# write_files: dry_run must be inert, overwrite/create_dirs gates must hold
# ---------------------------------------------------------------------------

def test_write_files_dry_run_does_not_create_file(workspace):
    target = workspace / "new_module.py"
    result = bridges.write_files(
        [{"file_path": "new_module.py", "content": "x = 1\n"}],
        dry_run=True,
    )
    assert result["success"] is True
    assert not target.exists()


def test_write_files_real_run_creates_file(workspace):
    target = workspace / "new_module.py"
    result = bridges.write_files(
        [{"file_path": "new_module.py", "content": "x = 1\n"}],
        dry_run=False,
    )
    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_write_files_refuses_overwrite_without_flag(workspace):
    target = workspace / "existing.py"
    target.write_text("original\n", encoding="utf-8")
    result = bridges.write_files(
        [{"file_path": "existing.py", "content": "clobbered\n", "overwrite": False}],
        dry_run=False,
    )
    assert result["success"] is False
    assert target.read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# run_validation: only allowlisted commands may actually execute
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_validation_rejects_non_allowlisted_command(workspace):
    result = await bridges.run_validation([["rm", "-rf", "."]])
    assert result["success"] is False
    assert "command not allowed" in result["errors"][0]


@pytest.mark.asyncio
async def test_run_validation_runs_allowlisted_py_compile(workspace):
    good = workspace / "good.py"
    good.write_text("x = 1\n", encoding="utf-8")
    result = await bridges.run_validation([["python", "-m", "py_compile", "good.py"]])
    assert result["success"] is True
    assert result["results"][0]["exit_code"] == 0


# ---------------------------------------------------------------------------
# execute_self_improvement_plan: the full pipeline, real-run vs dry-run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_plan_dry_run_touches_nothing_and_skips_validation(readme):
    plan = bridges.create_self_improvement_plan(
        goal="fix the readme",
        target_files=["README.md"],
        replacements=[{"file_path": "README.md", "old_text": "Hello world", "new_text": "Patched"}],
        validation_commands=[["python", "-m", "py_compile", "README.md"]],
        dry_run=True,
    )["plan"]

    result = await bridges.execute_self_improvement_plan(plan, dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert readme.read_text(encoding="utf-8") == "Hello world\n"
    # a dry run must not shell out to validation either
    assert result["validation"] is None


@pytest.mark.asyncio
async def test_execute_plan_real_run_applies_and_validates(workspace):
    target = workspace / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    plan = bridges.create_self_improvement_plan(
        goal="fix mod.py",
        target_files=["mod.py"],
        replacements=[{"file_path": "mod.py", "old_text": "x = 1", "new_text": "x = 2"}],
        validation_commands=[["python", "-m", "py_compile", "mod.py"]],
        dry_run=False,
    )["plan"]

    result = await bridges.execute_self_improvement_plan(plan, dry_run=False)

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "x = 2\n"
    assert result["validation"]["success"] is True


# ---------------------------------------------------------------------------
# execute_self_improvement_goal: the "risk gate" the model actually calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goal_dry_run_true_never_touches_disk(readme):
    result = await bridges.execute_self_improvement_goal(
        goal="please patch the readme",
        dry_run=True,
        replacements=[{"file_path": "README.md", "old_text": "Hello world", "new_text": "Changed"}],
    )
    assert result["success"] is True
    assert readme.read_text(encoding="utf-8") == "Hello world\n"


@pytest.mark.asyncio
async def test_goal_real_execution_with_concrete_replacements_actually_writes(readme):
    """Regression test: dry_run used to be silently forced True here even
    when the caller explicitly asked for a real (dry_run=False) run, so a
    model calling this tool believed it had made a change when nothing on
    disk ever moved."""
    result = await bridges.execute_self_improvement_goal(
        goal="please patch the readme",
        dry_run=False,
        replacements=[{"file_path": "README.md", "old_text": "Hello world", "new_text": "Changed"}],
        validation_commands=[],
    )
    assert result["success"] is True
    assert result["execution_result"]["dry_run"] is False
    assert readme.read_text(encoding="utf-8") == "Changed\n"


@pytest.mark.asyncio
async def test_goal_real_execution_without_concrete_changes_is_reported_as_noop(readme):
    """Without replacements/file_writes there is nothing concrete to apply;
    the result must say so explicitly (no_op=True) rather than reporting a
    bare success that implies the goal was actually carried out."""
    result = await bridges.execute_self_improvement_goal(
        goal="improve something vague",
        dry_run=False,
    )
    assert result["no_op"] is True
    assert readme.read_text(encoding="utf-8") == "Hello world\n"


@pytest.mark.asyncio
async def test_goal_documentation_gap_respects_dry_run_gate(readme):
    with patch(
        "sdk.agent.documentation_nodes.generate_and_verify_program_nodes"
    ) as mock_generate:
        mock_generate.return_value = {"success": True, "errors": []}

        dry_result = await bridges.execute_self_improvement_goal(
            goal="update the documentation for the chat panel",
            dry_run=True,
        )
        assert dry_result["success"] is True
        mock_generate.assert_not_called()

        real_result = await bridges.execute_self_improvement_goal(
            goal="update the documentation for the chat panel",
            dry_run=False,
        )
        assert real_result["success"] is True
        mock_generate.assert_called_once()


# ---------------------------------------------------------------------------
# create_self_improvement_plan: goal classification sanity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "goal,expected_gap",
    [
        ("please refresh the node grid UI", "ui-refresh"),
        ("run pytest to validate this change", "validation"),
        ("write documentation for this module", "documentation"),
        ("patch the bug in the parser", "bridge-implementation"),
        ("make things better somehow", "general-self-improvement"),
    ],
)
def test_create_plan_classifies_goal(readme, goal, expected_gap):
    plan_result = bridges.create_self_improvement_plan(goal=goal, dry_run=True)
    assert plan_result["plan"]["classified_gap"] == expected_gap
