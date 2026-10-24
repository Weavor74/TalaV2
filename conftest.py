"""
Root pytest configuration.

Exists for one reason: the suite is tiered (see `[tool.pytest.ini_options]` in
pyproject.toml), and a tier that is skipped quietly is worse than a tier that is
slow. The default `pytest` run deselects the `llm`-marked tests -- the ones that
drive a real model or the running harness -- because those 11 tests held
essentially the entire 41-minute runtime of the old full suite.

Deselecting them silently would be the same shape as the bugs this branch was
opened to remove: a path that turns "did not happen" into "looks fine". So every
run ends with an explicit count of what did not run and the command to run it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Highs H7: production default is lease enforce. The suite must not stall on
# human approval for every file/ledger tool — force open unless a test sets
# the mode itself.
os.environ.setdefault("TALA_LEASE_MODE", "open")

# The inference profile (python/inference/profiles.py) auto-selects `kagura` on
# a machine that has its llama-server. The suite asserts the profile-less
# defaults, so pin it off unless a test sets a profile itself.
os.environ.setdefault("TALA_PROFILE", "legacy")

# --- database isolation ------------------------------------------------------
#
# The suite wrote into the running application's Postgres. Its fixtures create
# real task rows -- test-task-retry, test-task-success, test-fleet-integrate,
# task_1, "Test Goal" -- so every run repopulated the live Active Task Queue.
# Observed as "I clear the queue and after about twelve minutes they all come
# back": they were not coming back, each test run was recreating them.
#
# Redirected before any import can read settings.DB_URL, which resolves
# TALA_DB_URL once at import time. Same server and credentials, a separate
# database, so migrations create their own tables and nothing a test does is
# visible to the app.
#
# Set TALA_TEST_DB_URL to point somewhere else. Set TALA_ALLOW_PRODUCTION_DB=1
# only if you genuinely mean to test against live data; the summary says so
# loudly when you do, because a suite that quietly mutates production is the
# same shape as the bugs this branch exists to remove.
def _configured_db_url() -> str:
    """
    The DSN the application would actually use.

    Resolved exactly the way python/harness/config.py resolves it, because any
    divergence makes this guard a silent no-op and puts the suite back on the
    production database with nothing to show for it.

    Two divergences were found by review in the hand-rolled parser this
    replaces. It matched on a literal ``TALA_DB_URL=`` prefix, so the
    shell-sourceable ``export TALA_DB_URL=...`` form was skipped entirely; and
    it hardcoded the repo-root .env, while config.py honours TALA_ENV_PATH.
    Either one produced an empty string, which _isolate_database treats as
    "nothing configured" and returns without touching os.environ.

    dotenv_values is used rather than load_dotenv: it parses without mutating
    the environment, so reading here cannot itself pre-empt the redirect below.
    """
    from_env = os.environ.get("TALA_DB_URL")
    if from_env:
        return from_env

    dot_env = (
        Path(os.environ["TALA_ENV_PATH"]).expanduser().resolve()
        if os.environ.get("TALA_ENV_PATH")
        else Path(__file__).resolve().parent / ".env"
    )
    if not dot_env.exists():
        return ""
    try:
        from dotenv import dotenv_values

        return (dotenv_values(dot_env) or {}).get("TALA_DB_URL") or ""
    except Exception:
        return ""


_PRODUCTION_DB_URL = _configured_db_url()
#: Why the suite is pointed at the application's own database, or None when it
#: is properly isolated. Read by pytest_terminal_summary -- an earlier version
#: set this in two branches and never read it anywhere, so the docstring's
#: promise that the summary "says so loudly" was unimplemented and the
#: malformed-DSN path was completely silent.
_PRODUCTION_DB_REASON: str | None = None


def _isolate_database() -> None:
    global _PRODUCTION_DB_REASON

    if os.environ.get("TALA_ALLOW_PRODUCTION_DB") == "1":
        if _PRODUCTION_DB_URL:
            _PRODUCTION_DB_REASON = "TALA_ALLOW_PRODUCTION_DB=1 was set explicitly"
        return

    explicit = os.environ.get("TALA_TEST_DB_URL")
    if explicit:
        os.environ["TALA_DB_URL"] = explicit
        return

    if not _PRODUCTION_DB_URL:
        return

    # Derive a sibling database rather than inventing a DSN: same host, port,
    # user, password and connection options, different database name.
    base, sep, database = _PRODUCTION_DB_URL.rpartition("/")
    if not sep or not database:
        _PRODUCTION_DB_REASON = (
            "could not parse a database name out of the configured DSN, so no "
            "sibling could be derived"
        )
        return

    # The query string carries sslmode, connect_timeout and friends. Dropping it
    # broke every managed Postgres that requires TLS, at import time, with
    # nothing to say the test framework had rewritten the DSN.
    database, _, query = database.partition("?")
    if database.endswith("_test"):
        return
    suffix = f"?{query}" if query else ""
    os.environ["TALA_DB_URL"] = f"{base}/{database}_test{suffix}"


_isolate_database()


# --- user-data isolation -----------------------------------------------------
#
# The same bug as the database one, through a different door. Completing a
# workflow distils a skill node, and that write resolves its own destination
# via resolve_user_data_dir(), which -- correctly, for a portable install --
# defaults to the application root. Under pytest that is the checkout, so every
# run rewrote the run history inside the repository's own
# nodes/skill-workflow_test.html, and `git status` was dirty after a green
# suite.
#
# Per-fixture pins do not close this: only tests that happen to request the
# fixture are covered, and the writes come from production code paths that any
# test can reach. It belongs here, once, ahead of everything.
#
# Set TALA_USER_DATA yourself to point somewhere specific. Set
# TALA_ALLOW_REPO_USER_DATA=1 to deliberately let the suite write into the
# checkout, which only the node-authoring tools should ever want.
def _isolate_user_data() -> None:
    if os.environ.get("TALA_ALLOW_REPO_USER_DATA") == "1":
        return
    if os.environ.get("TALA_USER_DATA"):
        return

    import shutil
    import tempfile

    sandbox = Path(tempfile.mkdtemp(prefix="tala-test-userdata-"))
    repo_root = Path(__file__).resolve().parent

    # Seeded, not empty. Plenty of tests legitimately *read* the real graph
    # through this resolver -- the architecture contract, the tool catalogue,
    # documentation drift -- and handing them an empty directory does not
    # isolate those tests, it makes them pass over nothing, which is the same
    # false success as the writes this guard exists to stop. Copying gives
    # reads the real content and sends writes to the copy.
    for name in ("nodes", "workflows"):
        source = repo_root / name
        target = sandbox / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.mkdir(parents=True, exist_ok=True)

    os.environ["TALA_USER_DATA"] = str(sandbox)


_isolate_user_data()


# Nothing the suite does may reach the real chain node.
#
# The block above moved the data directory but left the chain endpoint alone.
# The document key follows TALA_USER_DATA, while the node's URL and admin
# secret do not, so every test that anchored through an unstubbed caller
# (about 40 sites reach canonicalize_and_anchor) encrypted under a throwaway
# key minted in its sandbox and posted the result to the live node. 4,126 of
# the live chain's 7,966 encrypted documents are exactly that: sealed under
# keys that were deleted with their sandboxes, none of them ever readable.
#
# Pointing the endpoint at a port nothing listens on, and the node's data
# directory at the sandbox, closes every one of those doors at once: the post
# is refused before it leaves the machine and no admin secret is found to send.
# Tests that need a node start their own on a free port and set both variables
# for it. Set TALA_ALLOW_LIVE_CHAIN=1 to deliberately let the suite talk to the
# real node.
def _isolate_chain_endpoint() -> None:
    if os.environ.get("TALA_ALLOW_LIVE_CHAIN") == "1":
        return

    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    os.environ["TALA_CHAIN_BIND_HOST"] = "127.0.0.1"
    os.environ["TALA_CHAIN_BIND_PORT"] = str(dead_port)
    os.environ.pop("TALA_CHAIN_PORT", None)
    os.environ.pop("TALA_CHAIN_ADMIN_SECRET", None)
    os.environ["TALA_CHAIN_DATA_DIR"] = str(Path(os.environ["TALA_USER_DATA"]) / "blockchain" / "data")


_isolate_chain_endpoint()

# The topic-stance layer embeds an unmatched message through Ollama when it can. No test may
# reach a real Ollama by accident (a developer machine has one running); tests of the layer turn
# it back on with a fake embedder.
os.environ["TALA_TOPIC_EMBEDDINGS"] = "0"
# And no ambient sky override: tests pin the astrology clock themselves.
os.environ.pop("TALA_ASTRO_NOW", None)


@pytest.fixture(autouse=True)
def isolate_documentation_anchoring(monkeypatch):
    """Document tests must not submit their temporary nodes to the live chain.

    Database and filesystem isolation alone left this HTTP write untouched.
    With the chain offline, bulk regeneration also waited once per document.
    Tests of anchoring itself exercise sdk.agent.anchor separately.
    """
    from sdk.agent import documentation_nodes

    monkeypatch.setattr(documentation_nodes, "canonicalize_and_anchor", lambda **kwargs: None)


# --- worktree isolation ------------------------------------------------------
#
# DAGExecutionEngine asks handle_task_create_worktree for a worktree whenever its root is the repo root and no path was
# supplied, and that handler runs `git worktree add` against the repo itself. Most tests patch it; the ones that did not left
# scratch/worktrees/<id> and a tala-task/<id> branch in the real checkout on every full run -- five of each, noticed on
# 2026-10-24 when deleting them and watching them come back. Watching the directory is a listdir per test, where asking git
# would cost ~30 ms each across thousands of tests.
_WORKTREE_DIR = Path(__file__).resolve().parent / "scratch" / "worktrees"


def _worktree_names(root: Path = _WORKTREE_DIR) -> set[str]:
    try:
        return set(os.listdir(root))
    except OSError:
        return set()


def _remove_leaked_worktree(name: str, root: Path = _WORKTREE_DIR) -> None:
    import subprocess

    repo = root.parent.parent
    for command in (["git", "worktree", "remove", "--force", str(root / name)],
                    ["git", "worktree", "prune"],
                    ["git", "branch", "-D", f"tala-task/{name}"]):
        try:
            subprocess.run(command, cwd=repo, capture_output=True, timeout=30)
        except Exception:  # the failure below is what matters; a stuck cleanup must not hide it
            pass


def _check_worktrees(before: set[str], nodeid: str, root: Path = _WORKTREE_DIR) -> None:
    """Remove any worktree the test left behind and fail it. What was there before the test is not its doing."""
    leaked = sorted(_worktree_names(root) - before)
    if not leaked:
        return
    for name in leaked:
        _remove_leaked_worktree(name, root)
    pytest.fail(
        f"{nodeid} left git worktree(s) {leaked} under {root} (and tala-task/ branches). It reached the real "
        "handle_task_create_worktree: patch python.harness.server.handle_task_create_worktree, or give the engine a "
        "root_dir under tmp_path. They have been removed.",
        pytrace=False,
    )


@pytest.fixture(scope="session")
def _worktree_base(tmp_path_factory):
    return tmp_path_factory.mktemp("worktree-base")


@pytest.fixture(autouse=True)
def isolate_task_worktree_base(monkeypatch, _worktree_base):
    """Aim handle_task_create_worktree at a scratch directory instead of the checkout.

    GoalEngine().run_goal() reaches it through DAGExecutionEngine with nothing in the test to patch, and it runs
    `git worktree add` in task_rpc._ROOT_DIR -- the repo itself. Elsewhere that handler then fails on its database update
    and the engine falls back to the root workspace, so the outcome is unchanged; only the worktree and the tala-task/
    branch it had already created are no longer left behind in the real repository.
    """
    from python.harness import task_rpc

    monkeypatch.setattr(task_rpc, "_ROOT_DIR", _worktree_base)


@pytest.fixture(autouse=True)
def no_worktree_leaks(request):
    before = _worktree_names()
    yield
    _check_worktrees(before, request.node.nodeid)


def pytest_configure(config: pytest.Config) -> None:
    # Stash the deselected count where the summary hook can reach it. pytest
    # reports deselection through the hook below rather than on the config.
    config._llm_deselected = 0  # type: ignore[attr-defined]


def pytest_deselected(items: list[pytest.Item]) -> None:
    if not items:
        return
    config = items[0].config
    current = getattr(config, "_llm_deselected", 0)
    config._llm_deselected = current + len(items)  # type: ignore[attr-defined]


def pytest_terminal_summary(
    terminalreporter, exitstatus, config: pytest.Config
) -> None:
    """Say plainly what was not run, and what was written to."""
    # Reported first and unconditionally: a suite that mutated the running
    # application's database is the more important thing to know, and it must
    # not be hidden behind the deselection early-return below.
    if _PRODUCTION_DB_REASON:
        terminalreporter.write_sep("=", "PRODUCTION DATABASE", red=True, bold=True)
        terminalreporter.write_line(
            f"This run used the application's own database: {_PRODUCTION_DB_REASON}."
        )
        terminalreporter.write_line(
            "  Test fixtures create real rows, which is how the live task queue "
            "used to repopulate itself."
        )
        terminalreporter.write_line("  Set TALA_TEST_DB_URL to point the suite elsewhere.")

    deselected = getattr(config, "_llm_deselected", 0)
    if not deselected:
        return

    # Only shout when the deselection came from the default tiering. If the
    # caller passed their own -m expression they already know what they asked
    # for, and a nag would just be noise.
    markexpr = config.getoption("-m", default="")
    if markexpr != "not llm":
        return

    terminalreporter.write_sep("=", "NOT RUN", yellow=True, bold=True)
    terminalreporter.write_line(
        f"{deselected} test(s) marked `llm` were deselected. They drive a real "
        "model or the running harness and take minutes each."
    )
    terminalreporter.write_line("  pytest -m llm    run only those")
    terminalreporter.write_line("  pytest -m \"\"     run everything")
