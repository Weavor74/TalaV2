"""
The suite must never open a connection to the application's own database.

conftest.py redirects the suite to ``<db>_test`` -- but only when it can read the production URL, which in a
fresh git worktree (no gitignored ``.env``) it cannot. It then quietly fell through to the config default, the
live ``tala`` database: a full run left 19 test tasks on the Active Task Queue, 115 events, swarm and log rows,
and overwrote the provider tables, and the only thing that noticed was a test that failed after the damage was
done. The guard sits where the harm happens, at the moment a connection is opened under pytest, and fails closed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from python.harness import db, db_guard


@pytest.fixture
def pool_spy(monkeypatch):
    """Replace the pool factory so no real connection is ever made, and record whether it was reached."""
    calls = []

    async def fake_create_pool(**kwargs):
        calls.append(kwargs["dsn"])
        return object()

    async def no_migrations():
        return None

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(db, "_run_migrations", no_migrations)
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.delenv("TALA_ALLOW_PRODUCTION_DB", raising=False)
    return calls


@pytest.mark.parametrize("url", [
    "postgresql://u:p@localhost:5432/tala",
    "postgresql://u:p@localhost/tala?sslmode=require",
    "postgresql://u:p@localhost:5432/tala_standalone",
    "postgresql://u:p@localhost:5432",              # no database name at all
    "not a url",
])
def test_a_live_looking_database_is_refused_before_any_connection(url, pool_spy, monkeypatch):
    monkeypatch.setattr(db, "settings", SimpleNamespace(DB_URL=url))
    import asyncio

    with pytest.raises(RuntimeError, match="refusing to connect"):
        asyncio.run(db.init_pool())
    assert pool_spy == [], "a connection was attempted before the guard refused"


@pytest.mark.parametrize("url", [
    "postgresql://u:p@localhost:5432/tala_test",
    "postgresql://u:p@localhost/tala_test?sslmode=require",
    "postgresql://u:p@localhost:5432/scratch_test",
])
def test_a_test_database_is_allowed(url, pool_spy, monkeypatch):
    monkeypatch.setattr(db, "settings", SimpleNamespace(DB_URL=url))
    import asyncio

    asyncio.run(db.init_pool())
    assert pool_spy == [url]


def test_the_refusal_names_the_database_and_the_way_out(pool_spy, monkeypatch):
    monkeypatch.setattr(db, "settings", SimpleNamespace(DB_URL="postgresql://u:secretpw@localhost/tala"))
    import asyncio

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(db.init_pool())
    message = str(caught.value)
    assert "'tala'" in message and "TALA_TEST_DB_URL" in message and "TALA_ALLOW_PRODUCTION_DB" in message
    assert "secretpw" not in message, "the refusal must never echo the password"


def test_the_explicit_opt_out_still_works(pool_spy, monkeypatch):
    monkeypatch.setattr(db, "settings", SimpleNamespace(DB_URL="postgresql://u:p@localhost/tala"))
    monkeypatch.setenv("TALA_ALLOW_PRODUCTION_DB", "1")
    import asyncio

    asyncio.run(db.init_pool())
    assert pool_spy == ["postgresql://u:p@localhost/tala"]


def test_outside_pytest_the_guard_does_nothing():
    """The running application uses the live database by design; only a test run is held to `_test`."""
    db_guard.refuse_live_database_under_pytest("postgresql://u:p@localhost/tala", under_pytest=False)
