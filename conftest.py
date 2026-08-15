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

    Not just os.environ: TALA_DB_URL is normally unset in the shell and comes
    from .env, which config.py loads with override=False. Reading only the
    environment made an earlier version of this isolation a silent no-op --
    it derived a test DSN from an empty string and returned without doing
    anything, which is precisely the class of bug it was written to stop.
    """
    from_env = os.environ.get("TALA_DB_URL")
    if from_env:
        return from_env

    dot_env = Path(__file__).resolve().parent / ".env"
    if not dot_env.exists():
        return ""
    for line in dot_env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("TALA_DB_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


_PRODUCTION_DB_URL = _configured_db_url()
_USING_PRODUCTION_DB = False


def _isolate_database() -> None:
    global _USING_PRODUCTION_DB

    if os.environ.get("TALA_ALLOW_PRODUCTION_DB") == "1":
        _USING_PRODUCTION_DB = bool(_PRODUCTION_DB_URL)
        return

    explicit = os.environ.get("TALA_TEST_DB_URL")
    if explicit:
        os.environ["TALA_DB_URL"] = explicit
        return

    if not _PRODUCTION_DB_URL:
        return

    # Derive a sibling database rather than inventing a DSN: same host, port,
    # user and password, different database name.
    base, sep, database = _PRODUCTION_DB_URL.rpartition("/")
    if not sep or not database:
        _USING_PRODUCTION_DB = True
        return
    database = database.split("?", 1)[0]
    if database.endswith("_test"):
        return
    os.environ["TALA_DB_URL"] = f"{base}/{database}_test"


_isolate_database()


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
    """Say plainly what was not run."""
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
