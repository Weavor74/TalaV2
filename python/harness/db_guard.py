"""
python/harness/db_guard.py -- a test run must never connect to the application's own database.

conftest.py redirects the suite to ``<db>_test``, but only when it can read the production URL (the
gitignored ``.env``). A fresh git worktree has none, so the redirect quietly did nothing and a full run went to
the live ``tala`` database: 19 test tasks on the Active Task Queue, 115 events, swarm and log rows, and the
provider tables overwritten. The only thing that noticed was a test that failed after the damage was done.

This check sits where the harm happens, when a connection is about to be opened, and fails closed: under pytest
the database name must end in ``_test``. A name that is missing or unreadable is refused too. The running
application is never affected (pytest is not loaded), and the opt-out is explicit.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse


def _database_name(url: str) -> str:
    try:
        path = urlparse(url or "").path
    except ValueError:
        return ""
    return path.rsplit("/", 1)[-1]


def refuse_live_database_under_pytest(url: str, under_pytest: bool | None = None) -> None:
    """Raise unless this is not a test run, the database is a ``*_test`` one, or the opt-out is set.

    The message names the database but never the URL, so a password cannot leak into a log.
    """
    if under_pytest is None:
        under_pytest = "pytest" in sys.modules
    if not under_pytest or os.environ.get("TALA_ALLOW_PRODUCTION_DB") == "1":
        return
    name = _database_name(url)
    if name.endswith("_test"):
        return
    raise RuntimeError(
        f"refusing to connect: this is a test run and the database is {name!r}, not a *_test database, so "
        "it would write test rows into the application's own data. conftest.py normally redirects to "
        "<database>_test but can only do that when it can read TALA_DB_URL (a fresh git worktree has no .env). "
        "Set TALA_TEST_DB_URL to a test database. TALA_ALLOW_PRODUCTION_DB=1 overrides this deliberately."
    )
