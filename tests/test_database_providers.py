"""
User databases are a capability, not a policy.

Steven: *"This is much like git or storage. It does not need to be used. You
can have 0 databases or 1000 databases. How you use them is up to the user not
to the develouper."*

That settles UI-071, UI-072 and UI-110 together, and differently from how the
findings framed them. The panel asserted *"Priority Order"* and *"Automatic
Failover"*; neither existed -- there is one pool from one DSN, so failover had
nowhere to live. UI-110 recorded that Steven wanted content partitioning
instead, but that was his example of a *use*, not a specification: building it
in would replace one imposed policy with another.

So this does what git and storage do -- connect, prove the connection is real,
get out of the way -- and the panel describes what it is instead of asserting
behaviour nobody implemented and nobody asked for.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from python.harness import database_clients as dc

INDEX = Path("renderer/index.html")


# -- the panel no longer claims what it cannot do -----------------------------

def test_the_failover_and_priority_claims_are_gone():
    html = INDEX.read_text(encoding="utf-8")
    # Comments stripped: the comment recording the removal quotes what was removed.
    rendered = re.sub(r"<!--.*?-->", "", html, flags=re.S)

    assert "Automatic Failover" not in rendered
    assert "REPLICATION LAWS" not in rendered
    assert "DB connections are queried in the sequence defined" not in rendered


def test_the_panel_says_the_connections_are_optional():
    html = INDEX.read_text(encoding="utf-8")
    rendered = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    assert "Optional" in rendered
    assert "0 databases" in rendered or "Add none" in rendered


def test_the_panel_says_a_saved_connection_is_not_a_working_one():
    html = INDEX.read_text(encoding="utf-8")
    rendered = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    assert "TEST" in rendered
    assert "CONFIGURED" in rendered


# -- capabilities -------------------------------------------------------------

def test_postgres_and_sqlite_need_nothing_extra():
    """asyncpg is already here because Tala's own store is Postgres, and
    sqlite3 is in the standard library."""
    available = {c["kind"] for c in dc.capabilities() if c["available"]}
    assert {"postgres", "sqlite"} <= available


def test_an_unavailable_driver_names_its_package_and_extra():
    for entry in dc.capabilities():
        if entry["available"]:
            continue
        assert entry["dependency"] in entry["reason"]
        assert entry["install_extra"] in entry["reason"]


def test_the_extras_are_declared():
    toml = Path("pyproject.toml").read_text(encoding="utf-8")
    for entry in dc.capabilities():
        if entry["install_extra"]:
            assert f"{entry['install_extra']} = [" in toml


# -- inference ----------------------------------------------------------------

@pytest.mark.parametrize("provider,expected", [
    ({"endpoint": "postgresql://h/db"}, "postgres"),
    ({"endpoint": "postgres://h/db"}, "postgres"),
    ({"endpoint": "mysql://h/db"}, "mysql"),
    ({"endpoint": "mongodb://h/db"}, "mongodb"),
    ({"endpoint": "/data/notes.sqlite"}, "sqlite"),
    ({"endpoint": "C:/x/app.db"}, "sqlite"),
    ({"endpoint": "", "name": "My MongoDB"}, "mongodb"),
])
def test_the_kind_is_inferred(provider, expected):
    assert dc.infer_kind(provider) == expected


def test_an_unguessable_connection_is_not_assumed_to_be_postgres():
    """Guessing is stated rather than hidden: silently treating an unknown
    connection as Postgres would produce a confident, wrong error."""
    assert dc.infer_kind({"endpoint": "host:5432", "name": "mystery"}) == ""


@pytest.mark.parametrize("raw,expected", [
    ("PostgreSQL", "postgres"), ("MariaDB", "mysql"),
    ("SQL Server", "mssql"), ("Mongo", "mongodb"),
])
def test_aliases_fold(raw, expected):
    assert dc.normalise_kind(raw) == expected


# -- probing ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_real_sqlite_file_connects(tmp_path):
    path = tmp_path / "notes.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE note (body TEXT)")
    conn.commit()
    conn.close()

    detail = await dc.probe({"endpoint": str(path), "name": "Notes"})
    assert "SQLite" in detail


@pytest.mark.asyncio
async def test_a_missing_endpoint_is_refused():
    with pytest.raises(dc.DatabaseError):
        await dc.probe({"endpoint": "", "name": "Empty"})


@pytest.mark.asyncio
async def test_an_unguessable_kind_says_what_it_saw():
    with pytest.raises(dc.DatabaseError, match="what kind"):
        await dc.probe({"endpoint": "host:1234", "name": "mystery"})


@pytest.mark.asyncio
async def test_an_uninstalled_driver_names_the_package():
    with pytest.raises(dc.DatabaseError, match="aiomysql"):
        await dc.probe({"endpoint": "mysql://h/db", "name": "M"})


@pytest.mark.asyncio
async def test_bad_credentials_report_the_reason():
    """"password authentication failed" and "connection refused" need
    different responses from the user."""
    with pytest.raises(dc.DatabaseError) as exc:
        await dc.probe({
            "endpoint": "postgresql://nobody:wrong@127.0.0.1:5432/nope",
            "name": "Bad",
        })
    assert str(exc.value)


# -- separation from Tala's own store -----------------------------------------

def _code_only(module) -> str:
    """
    The module's executable text, with docstrings and comments removed.

    Both checks below are about what the code *does*. The module docstring
    necessarily names failover, replication and settings.DB_URL in order to
    explain that it deliberately does none of them, and a plain substring
    search cannot tell the explanation from the behaviour.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    parts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                parts.append(node.value)
    return " ".join(parts)


def test_a_user_connection_does_not_touch_the_harness_pool():
    """A user database going away must not be able to stop the app."""
    code = _code_only(dc)

    assert "get_pool" not in code
    assert "init_pool" not in code
    assert "DB_URL" not in code


def test_no_routing_or_failover_is_imposed():
    """Building partitioning in would replace one imposed policy with another,
    and failover was never implementable: one pool, one DSN."""
    code = _code_only(dc).lower()

    for word in ("failover", "standby", "replica", "priority"):
        assert word not in code, (
            f"{word!r} appears in the code rather than only in the notes"
        )


def test_the_capability_rpc_exists():
    from python.harness.server import _HANDLERS
    assert "database.capabilities" in _HANDLERS
    assert "database.test_provider" in _HANDLERS
