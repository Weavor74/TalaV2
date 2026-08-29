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
            f"could not parse a database name out of the configured DSN, so no "
            f"sibling could be derived"
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
