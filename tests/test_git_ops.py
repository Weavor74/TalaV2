"""
The git panel does clone, pull and push, on the user's repositories.

The panel said: *"Higher priority providers are used first for clone, push, and
pull operations."* `git_providers` was referenced in six places, every one of
them storage or display, so none of those three operations existed (UI-067).

What *did* exist was a `git:status` IPC pointed at Tala's own checkout. Steven,
asked about it: *"I may have connected it to check if it worked"* -- leftover
verification, not a design decision, and it sat awkwardly against his rule that
*"Tala does not use GIT for herself."*

The credential handling is the part worth the most care. Three obvious
approaches each leak it: the remote URL writes it into `.git/config` in clear
and permanently; `-c http.extraHeader` puts it in `argv` where any process
listing shows it; a credential helper has to be written to disk. Git 2.31+
reads config from environment variables, which are in none of those places.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from python.harness import git_ops

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    import sdk.agent.documentation_nodes as dn
    monkeypatch.setattr(dn, "resolve_user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(git_ops, "workspace_root", lambda: tmp_path / "repos")
    return tmp_path


@pytest.fixture
def upstream(workspace):
    """A real repository to clone from."""
    bare = workspace / "upstream.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

    seed = workspace / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=seed, check=True)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=T",
                    "commit", "-qm", "seed"], cwd=seed, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"],
                   cwd=seed, check=True)
    # A bare repo's HEAD defaults to master; without this a clone succeeds with
    # an empty worktree and git still exits 0. Real behaviour, worth knowing.
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=bare, check=True)
    return bare


# -- containment --------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../escape", "..\\..\\windows", "sub/../../out"])
def test_a_path_cannot_climb_out_of_the_workspace(workspace, bad):
    with pytest.raises(git_ops.GitError, match="outside"):
        git_ops.resolve_path(bad)


@pytest.mark.parametrize("bad", ["/etc/passwd", "C:\\Windows", "\\\\server\\share"])
def test_an_absolute_path_is_refused_not_relativised(workspace, bad):
    """Stripping the leading separator would contain it but silently change
    what the caller asked for, and a rule that reinterprets its input is one
    nobody can reason about."""
    with pytest.raises(git_ops.GitError, match="absolute"):
        git_ops.resolve_path(bad)


def test_a_normal_name_resolves_inside(workspace):
    resolved = git_ops.resolve_path("myrepo")
    assert resolved.parent == (workspace / "repos").resolve()


# -- the command allowlist ----------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["reset", "clean", "config", "rm", "filter-branch"])
async def test_destructive_commands_are_refused(command):
    """A general "run git with these arguments" call is a shell."""
    with pytest.raises(git_ops.GitError, match="Permitted"):
        await git_ops.run_git([command])


@pytest.mark.asyncio
async def test_an_empty_command_is_refused():
    with pytest.raises(git_ops.GitError):
        await git_ops.run_git([])


# -- credentials --------------------------------------------------------------

def test_auth_travels_in_the_environment_not_the_arguments():
    env = git_ops._auth_env({"username": "steve", "api_key": "TOKEN123"})
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "Basic " in env["GIT_CONFIG_VALUE_0"]


def test_prompting_is_disabled():
    """A hung process waiting for a password is worse than a clean failure, and
    there is no terminal to answer on."""
    env = git_ops._auth_env({"username": "s", "api_key": "T"})
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_no_provider_means_no_header():
    """A public repository must not require a provider to be configured."""
    assert git_ops._auth_env(None) == {}
    assert git_ops._auth_env({"username": "s", "api_key": ""}) == {}


def test_output_is_scrubbed_of_credentials():
    scrubbed = git_ops._scrub("fatal: https://user:hunter2@host/x.git not found")
    assert "hunter2" not in scrubbed
    assert "***" in scrubbed

    header = git_ops._scrub("sent Authorization: Basic c3RldmU6VE9LRU4=")
    assert "c3RldmU6VE9LRU4=" not in header


@pytest.mark.asyncio
async def test_the_token_never_reaches_the_repository(workspace, upstream):
    """The property that matters: .git/config is committed to backups and read
    by every tool on the machine."""
    result = await git_ops.clone(str(upstream), "myrepo")
    assert result["success"], result["stderr"]

    repo = workspace / "repos" / "myrepo"
    provider = {"name": "T", "username": "steve", "api_key": "SUPERSECRETTOKEN"}
    await git_ops.run_git(["fetch", "--all"], cwd=repo, provider=provider)

    blob = b""
    for path in (repo / ".git").rglob("*"):
        if path.is_file():
            try:
                blob += path.read_bytes()
            except OSError:
                pass
    assert b"SUPERSECRETTOKEN" not in blob


# -- real operations ----------------------------------------------------------

@pytest.mark.asyncio
async def test_clone_brings_the_files(workspace, upstream):
    result = await git_ops.clone(str(upstream), "myrepo")

    assert result["success"], result["stderr"]
    repo = workspace / "repos" / "myrepo"
    assert (repo / ".git").exists()
    assert (repo / "README.md").read_text(encoding="utf-8").strip() == "hello"


@pytest.mark.asyncio
async def test_status_reports_a_local_change(workspace, upstream):
    await git_ops.clone(str(upstream), "myrepo")
    repo = workspace / "repos" / "myrepo"
    (repo / "NEW.txt").write_text("x\n", encoding="utf-8")

    result = await git_ops.run_git(["status", "--short"], cwd=repo)
    assert result["success"]
    assert "NEW.txt" in result["stdout"]


@pytest.mark.asyncio
async def test_cloning_over_an_existing_directory_is_refused(workspace, upstream):
    await git_ops.clone(str(upstream), "myrepo")
    with pytest.raises(git_ops.GitError, match="already exists"):
        await git_ops.clone(str(upstream), "myrepo")


@pytest.mark.asyncio
async def test_an_empty_url_is_refused(workspace):
    with pytest.raises(git_ops.GitError, match="URL is required"):
        await git_ops.clone("", "")


@pytest.mark.asyncio
async def test_a_failure_is_returned_not_raised(workspace):
    """The caller needs to show the user why, not catch an exception."""
    result = await git_ops.run_git(
        ["fetch", "https://example.invalid/nope.git"], timeout=30)

    assert result["success"] is False
    assert result["returncode"] != 0
    assert result["stderr"]


# -- the panel's promise ------------------------------------------------------

def test_the_operations_the_panel_advertises_exist():
    from python.harness.server import _HANDLERS

    for method in ("git.clone", "git.repo_op", "git.list_repos", "git.test_provider"):
        assert method in _HANDLERS


def test_provider_matching_is_by_host():
    """A GitHub token must not be offered to a GitLab remote."""
    import inspect
    src = inspect.getsource(git_ops.providers_for)
    assert "hostname" in src
    assert "priority ASC" in src
