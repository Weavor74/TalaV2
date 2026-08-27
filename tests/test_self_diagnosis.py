"""
Failures reach the log, Tala can read it, and the board stops hoarding.

Four findings, and two of them are Steven's own words.

UI-085 — "Logging. This should log everything. From commands sent to RPC
failed." It did not. agent_log held 44,464 rows of which exactly **one** was an
ERROR: the audit log recorded everything except things going wrong, which is
the one thing an audit log is for.

UI-086 — "It should also be part of the primary method for self diagnosis on
Tala." All four logging RPCs were UI-only, so every signal on that screen was
visible to the operator and invisible to the agent whose diagnosis it was.

UI-034 — "they dont fade out. It stays forever." No retention policy existed.

UI-035 — "they come back." Already fixed by the conftest database redirect;
this pins it so it cannot regress.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

SERVER = Path("python/harness/server.py")


# -- UI-085: failures reach the log ------------------------------------------

def test_the_audit_hook_is_on_err_not_the_dispatcher():
    """Most handlers catch their own exceptions and return _err directly, so a
    hook in the dispatcher's except block would miss nearly everything."""
    from python.harness import server

    src = inspect.getsource(server._err)
    assert "_audit_rpc_failure" in src


@pytest.mark.asyncio
async def test_an_internal_error_is_logged_at_error(monkeypatch):
    from python.harness import server

    written = []

    async def fake_log(level, message, trace=None, tx_id=None):
        written.append((level, message, trace))

    monkeypatch.setattr(server.db, "log_to_db", fake_log)
    server._CURRENT_RPC.set("probe.method")
    server._err(1, server.JsonRpcError.INTERNAL_ERROR, "it broke", {"traceback": "tb"})
    await asyncio.sleep(0)

    assert written, "the failure was not audited"
    level, message, trace = written[0]
    assert level == "ERROR"
    assert "probe.method" in message
    assert trace["method"] == "probe.method"


@pytest.mark.asyncio
async def test_a_caller_mistake_is_a_warning_not_an_error(monkeypatch):
    """An audit log where a typo'd parameter looks the same as a crashed
    subsystem is one nobody reads."""
    from python.harness import server

    written = []

    async def fake_log(level, message, trace=None, tx_id=None):
        written.append(level)

    monkeypatch.setattr(server.db, "log_to_db", fake_log)
    server._err(1, server.JsonRpcError.INVALID_PARAMS, "missing 'id'")
    await asyncio.sleep(0)

    assert written == ["WARNING"]


def test_auditing_a_failure_cannot_itself_fail():
    """No running loop -- a synchronous test, or startup -- must not raise. A
    failure to log a failure must not become a second failure."""
    from python.harness import server

    server._audit_rpc_failure(server.JsonRpcError.INTERNAL_ERROR, "no loop here")


def test_the_dispatcher_records_the_method():
    src = SERVER.read_text(encoding="utf-8")
    assert "_CURRENT_RPC.set(method)" in src, "_err cannot name what failed"


# -- log levels have one spelling --------------------------------------------

def test_levels_are_normalised_on_write():
    """WARN and WARNING both existed -- 98 rows against 16,589 -- which is a
    filter that looks like it works and silently misses half the rows."""
    from python.harness.db import _LOG_LEVEL_ALIASES

    assert _LOG_LEVEL_ALIASES["WARN"] == "WARNING"
    src = Path("python/harness/db.py").read_text(encoding="utf-8")
    assert "_LOG_LEVEL_ALIASES.get(" in src


def test_either_spelling_still_filters():
    """Existing callers, including the panel, pass WARN."""
    src = SERVER.read_text(encoding="utf-8")
    block = src.split("def handle_logging_get_events", 1)[1][:1200]
    assert "_LOG_LEVEL_ALIASES" in block


# -- UI-086: Tala can read, and only read ------------------------------------

@pytest.mark.parametrize("name", ["read_own_health", "read_own_logs", "diagnose_self"])
def test_the_diagnosis_tools_are_registered(name):
    import sdk.agent.self_diagnosis  # noqa: F401
    from sdk.agent.tools import ToolRegistry

    assert name in ToolRegistry._tools


def test_she_cannot_clear_her_own_logs():
    """Steven, Q-19: "She cannot clear her logs but I should be able too." An
    agent that can erase the record of what it did is not auditable."""
    import ast

    tree = ast.parse(Path("sdk/agent/self_diagnosis.py").read_text(encoding="utf-8"))

    # Names actually referenced in code, not names mentioned in prose: the
    # module docstring necessarily lists the RPCs it deliberately does not call.
    referenced = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    for forbidden in ("clear_events", "archive_events",
                      "handle_logging_clear_events", "handle_logging_archive_events"):
        assert forbidden not in referenced, f"self-diagnosis calls {forbidden}"

    # and no destructive SQL in anything it could execute. Docstrings are
    # excluded: this module's own docstring says it never truncates, which
    # would otherwise trip the check it exists to pass.
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
    literals = " ".join(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ).upper()
    for forbidden in ("DELETE FROM AGENT_LOG", "TRUNCATE"):
        assert forbidden not in literals, f"self-diagnosis can {forbidden}"


@pytest.mark.asyncio
async def test_health_puts_the_problems_first(monkeypatch):
    """Twelve rows of green with one amber buried in them is not a diagnosis."""
    import json

    from python.harness import server
    from sdk.agent import self_diagnosis

    async def fake_health(params, req_id):
        return json.dumps({"result": {
            "services": {"A": "HEALTHY", "B": "DEGRADED", "C": "UNKNOWN"},
            "details": {"A": "fine", "B": "because reasons", "C": "not measured"},
            "overall": "DEGRADED", "measured": 2, "unmeasured": ["C"],
        }})

    monkeypatch.setattr(server, "handle_logging_get_health", fake_health)
    result = await self_diagnosis.read_own_health()

    assert result["success"] is True
    names = [p["service"] for p in result["needs_attention"]]
    assert names == ["B", "C"]
    assert result["needs_attention"][0]["why"] == "because reasons"


@pytest.mark.asyncio
async def test_no_matching_events_is_not_reported_as_nothing_wrong(monkeypatch):
    """The exact failure UI-085 was: 44,464 rows and one ERROR. An empty log
    read must not be mistaken for a clean bill of health."""
    from python.harness import db
    from sdk.agent import self_diagnosis

    async def empty(*a, **k):
        return []

    monkeypatch.setattr(db, "fetch", empty)
    result = await self_diagnosis.read_own_logs()

    assert result["success"] is True
    assert result["count"] == 0
    assert "not necessarily that nothing went wrong" in result["note"]


@pytest.mark.asyncio
async def test_an_unreadable_log_says_so(monkeypatch):
    from python.harness import db
    from sdk.agent import self_diagnosis

    async def boom(*a, **k):
        raise RuntimeError("table gone")

    monkeypatch.setattr(db, "fetch", boom)
    result = await self_diagnosis.read_own_logs()

    assert result["success"] is False
    assert "table gone" in result["error"]


@pytest.mark.asyncio
async def test_the_log_read_is_capped(monkeypatch):
    from python.harness import db
    from sdk.agent import self_diagnosis

    seen = {}

    async def capture(sql, *args):
        seen["limit"] = args[-1]
        return []

    monkeypatch.setattr(db, "fetch", capture)
    await self_diagnosis.read_own_logs(limit=99999)
    assert seen["limit"] == self_diagnosis.MAX_LIMIT


# -- UI-034: finished work ages out, running work does not -------------------

@pytest.mark.asyncio
async def test_only_finished_tasks_are_eligible(monkeypatch):
    """An abandoned in-progress row is a bug worth seeing; deleting it hides
    the bug rather than the row."""
    from python.harness import server

    captured = {}

    async def fake_fetch(sql, *args):
        captured["sql"] = sql
        captured["statuses"] = args[0]
        return []

    monkeypatch.setattr(server.db, "fetch", fake_fetch)
    await server.prune_finished_tasks()

    for running in ("pending", "in-progress", "ready", "paused", "blocked"):
        assert running not in captured["statuses"]
    for finished in ("complete", "failed"):
        assert finished in captured["statuses"]


@pytest.mark.asyncio
async def test_a_dry_run_deletes_nothing(monkeypatch):
    from python.harness import server

    async def fake_fetch(sql, *args):
        return [{"id": "t1", "status": "complete", "created_at": None}]

    async def fail_execute(*a, **k):
        raise AssertionError("a dry run must not delete")

    monkeypatch.setattr(server.db, "fetch", fake_fetch)
    monkeypatch.setattr(server.db, "execute", fail_execute)

    result = await server.prune_finished_tasks(dry_run=True)
    assert result["pruned"] == 1
    assert result["dry_run"] is True


@pytest.mark.asyncio
async def test_a_sweep_records_what_it_removed(monkeypatch):
    """A retention policy that deletes without a record is indistinguishable
    from data loss."""
    from python.harness import server

    logged = []

    async def fake_fetch(sql, *args):
        return [{"id": "t1", "status": "complete", "created_at": None}]

    async def fake_execute(*a, **k):
        return None

    async def fake_log(level, message, trace=None, tx_id=None):
        logged.append(message)

    monkeypatch.setattr(server.db, "fetch", fake_fetch)
    monkeypatch.setattr(server.db, "execute", fake_execute)
    monkeypatch.setattr(server.db, "log_to_db", fake_log)

    await server.prune_finished_tasks()
    assert logged and "retention" in logged[0].lower()


def test_retention_is_separate_from_clear_queue():
    """task.clear truncates everything including running work. Retention must
    never be able to do that."""
    from python.harness import server

    src = inspect.getsource(server.prune_finished_tasks)
    assert "TRUNCATE" not in src
    assert "status = ANY($1)" in src


# -- UI-035: the suite does not touch the live database ----------------------

def test_the_suite_runs_against_its_own_database():
    """Steven's "they come back": tests wrote real task rows into the running
    application's Postgres, so every run repopulated his Active Task Queue."""
    from python.harness.config import settings

    name = settings.DB_URL.rsplit("/", 1)[-1].split("?")[0]
    assert name.endswith("_test"), (
        f"the suite is pointed at {name!r}, which is the application's own "
        f"database -- test task rows will appear on the live board"
    )
