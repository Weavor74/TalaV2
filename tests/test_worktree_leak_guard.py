"""
The suite's guard against tests leaving git worktrees in the real checkout.

DAGExecutionEngine asks python.harness.task_rpc.handle_task_create_worktree for a worktree whenever its root is the repo root and
no path was supplied, and that handler runs `git worktree add` against the repo itself. A test that lets it through left a
`scratch/worktrees/<id>` directory and a `tala-task/<id>` branch behind on every full run (five of each, found 2026-10-24 after
deleting them and watching them reappear). conftest.py now compares that directory before and after every test, removes
what is new, and fails the test that made it. These tests run the guard's helpers against a throwaway repository.
"""
from __future__ import annotations

import subprocess

import pytest

import conftest


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init")
    (root / "scratch" / "worktrees").mkdir(parents=True)
    return root


def _leak(repo, name):
    _git(repo, "worktree", "add", "-q", str(repo / "scratch" / "worktrees" / name), "-b", f"tala-task/{name}")


def test_a_missing_directory_means_nothing_leaked(tmp_path):
    assert conftest._worktree_names(tmp_path / "does-not-exist") == set()


def test_a_test_that_leaves_nothing_passes(repo):
    root = repo / "scratch" / "worktrees"
    before = conftest._worktree_names(root)
    conftest._check_worktrees(before, "tests/test_x.py::test_clean", root)


def test_a_leaked_worktree_is_removed_with_its_branch_and_the_test_fails(repo):
    root = repo / "scratch" / "worktrees"
    before = conftest._worktree_names(root)
    _leak(repo, "task_1")
    _leak(repo, "task_2")

    with pytest.raises(pytest.fail.Exception) as raised:
        conftest._check_worktrees(before, "tests/test_x.py::test_leaky", root)

    message = str(raised.value)
    assert "tests/test_x.py::test_leaky" in message and "task_1" in message and "task_2" in message
    assert conftest._worktree_names(root) == set()
    assert "tala-task/task_1" not in _git(repo, "branch", "--list")
    assert "scratch" not in _git(repo, "worktree", "list")


def test_what_was_already_there_is_not_blamed_on_this_test(repo):
    root = repo / "scratch" / "worktrees"
    _leak(repo, "left-by-someone-else")
    before = conftest._worktree_names(root)
    conftest._check_worktrees(before, "tests/test_x.py::test_clean", root)
    assert conftest._worktree_names(root) == {"left-by-someone-else"}  # not ours to delete


@pytest.mark.asyncio
async def test_the_worktree_handler_cannot_see_the_real_repo_from_a_test():
    """GoalEngine().run_goal() reaches this handler through DAGExecutionEngine with no patching in the test, so the base
    directory it runs `git worktree add` in is aimed away from the checkout for every test (conftest.py)."""
    import json
    from pathlib import Path

    from python.harness import task_rpc

    assert Path(task_rpc._ROOT_DIR).resolve() != Path(conftest.__file__).resolve().parent

    reply = json.loads(await task_rpc.handle_task_create_worktree({"task_id": "guard-probe-1"}, 1))

    assert "result" in reply or "error" in reply  # answered either way; what matters is where it worked
    assert not (conftest._WORKTREE_DIR / "guard-probe-1").exists()
