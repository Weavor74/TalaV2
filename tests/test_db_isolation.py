"""
The test suite must not write into the running application's database.

Reported as "I clear the queue and after about twelve minutes they all come
back". They were not coming back. Every test run recreated them: the suite's
fixtures insert real task rows -- test-task-retry, test-task-success,
test-fleet-integrate, task_1, "Test Goal" -- straight into the live Postgres, so
the Active Task Queue in Mission Control repopulated itself on the cadence
someone happened to be running pytest.
"""
import os
import re
from pathlib import Path

import conftest

CONFTEST = Path("conftest.py")


def test_the_suite_is_pointed_at_a_different_database_than_the_app():
    """The whole point. If these ever match again, tests are mutating live data."""
    if not conftest._PRODUCTION_DB_URL:
        return  # nothing configured; nothing to isolate
    if os.environ.get("TALA_ALLOW_PRODUCTION_DB") == "1":
        return  # deliberately overridden

    assert os.environ.get("TALA_DB_URL") != conftest._PRODUCTION_DB_URL, (
        "the test suite is connected to the application's own database"
    )


def test_the_test_database_is_a_sibling_not_a_guess():
    """Same host, port, user and password; only the database name differs. A
    hand-written DSN would drift from the real one."""
    if not conftest._PRODUCTION_DB_URL or os.environ.get("TALA_TEST_DB_URL"):
        return

    prod_base, _, prod_db = conftest._PRODUCTION_DB_URL.rpartition("/")
    test_base, _, test_db = os.environ["TALA_DB_URL"].rpartition("/")

    assert test_base == prod_base
    assert test_db == f"{prod_db.split('?')[0]}_test"


def test_the_configured_url_is_read_from_dotenv_not_just_the_environment():
    """TALA_DB_URL is normally unset in the shell and comes from .env, which
    config.py loads with override=False. An earlier version of this isolation
    read only os.environ, derived a test DSN from an empty string, and returned
    without doing anything -- a silent no-op guarding against silent writes."""
    source = CONFTEST.read_text(encoding="utf-8")

    assert "_configured_db_url" in source
    assert ".env" in source
    assert "TALA_DB_URL=" in source, "the .env fallback must actually parse the key"


def test_isolation_runs_before_pytest_collects_anything():
    """settings.DB_URL is resolved once at import time, so the redirect has to
    happen at conftest module scope -- not in a fixture, which runs too late."""
    source = CONFTEST.read_text(encoding="utf-8")
    call = source.index("_isolate_database()\n")
    configure = source.index("def pytest_configure")

    assert call < configure, "the redirect must happen at import, before collection"


def test_there_is_a_documented_way_to_opt_back_in():
    """Testing against live data is occasionally what you want; it must be
    explicit and it must be loud."""
    source = CONFTEST.read_text(encoding="utf-8")

    assert "TALA_ALLOW_PRODUCTION_DB" in source
    assert "TALA_TEST_DB_URL" in source


def test_fixture_task_ids_that_polluted_production_are_still_fixtures():
    """These are the ids that appeared in the live queue. If a test ever stops
    using a fixed id and starts using a real one, isolation is the only thing
    standing between the suite and production."""
    tests_dir = Path("tests")
    joined = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for p in tests_dir.glob("test_*.py")
    )

    for fixture_id in ("test-task-success", "test-task-retry", "test-fleet-integrate"):
        assert re.search(rf'["\']{re.escape(fixture_id)}["\']', joined), (
            f"{fixture_id} no longer appears in the suite; if it moved, confirm it "
            "is still a fixture id and not something written to a real queue"
        )
