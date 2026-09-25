"""
A removed inference provider must stay removed (Steven: "I have tried to
remove many of the models from the list" -- and they kept coming back).

inference.save_providers replaces the whole inference_providers table with
whatever the client currently holds, so deleting a provider in the UI did
delete its row -- but on the next app start, _auto_detect_and_verify_providers
re-scans every locally-running engine (Ollama/LM Studio/vLLM/local-OpenAI)
and re-registers any model it finds that isn't already a row, with a
deterministic id derived from engine+model name. A still-installed Ollama
model is indistinguishable, to that scan, from one that was never seen
before, so it always came back.

The fix is a small tombstone table: a save that drops a provider records its
id there, and the auto-detect scan skips any id it holds. These tests cover
both halves without touching a real database -- save_providers uses a fake
asyncpg-shaped pool/connection, and auto-detect fakes db.fetch/db.execute
and the httpx scan.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# handle_inference_save_providers: tombstoning
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self, existing_rows):
        self._existing_rows = existing_rows
        self.executed = []       # (sql, args)
        self.executemany_calls = []  # (sql, seq_of_args)

    async def fetch(self, sql, *args):
        assert "SELECT id, config FROM inference_providers" in sql
        return self._existing_rows

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"

    async def executemany(self, sql, seq_of_args):
        self.executemany_calls.append((sql, list(seq_of_args)))

    def transaction(self):
        return _NullAsyncCtx()


class _NullAsyncCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


def _row(id_, config):
    return {"id": id_, "config": json.dumps(config)}


@pytest.mark.asyncio
async def test_a_dropped_provider_is_tombstoned(monkeypatch):
    from python.harness import db
    from python.harness.provider_rpc import handle_inference_save_providers

    conn = _FakeConn([_row("ollama-llama3-2-3b", {"model": "llama3.2:3b"}),
                       _row("embedded-builtin", {"model": "qwen"})])
    monkeypatch.setattr(db, "get_pool", lambda: _FakePool(conn))

    # Only embedded-builtin is kept -- the Ollama one was removed by the user.
    result = await handle_inference_save_providers(
        {"providers": [{"id": "embedded-builtin", "type": "embedded",
                         "name": "Tala Embedded Engine", "priority": 1,
                         "status": "online", "locked": True, "config": {}}]},
        "req-1",
    )

    assert json.loads(result)["result"]["success"] is True
    tombstoned = [args[0] for _, seq in conn.executemany_calls for args in seq]
    assert "ollama-llama3-2-3b" in tombstoned
    assert "embedded-builtin" not in tombstoned


@pytest.mark.asyncio
async def test_a_readded_provider_clears_its_tombstone(monkeypatch):
    from python.harness import db
    from python.harness.provider_rpc import handle_inference_save_providers

    conn = _FakeConn([_row("ollama-llama3-2-3b", {"model": "llama3.2:3b"})])
    monkeypatch.setattr(db, "get_pool", lambda: _FakePool(conn))

    await handle_inference_save_providers(
        {"providers": [{"id": "ollama-llama3-2-3b", "type": "local",
                         "name": "OLLAMA - llama3.2:3b", "priority": 1000,
                         "status": "online", "locked": False, "config": {}}]},
        "req-2",
    )

    clear_calls = [args for sql, args in conn.executed
                   if "DELETE FROM dismissed_inference_providers" in sql]
    assert clear_calls, "expected a clear of the tombstone table"
    assert clear_calls[0][0] == ["ollama-llama3-2-3b"]


@pytest.mark.asyncio
async def test_nothing_is_tombstoned_when_nothing_is_removed(monkeypatch):
    from python.harness import db
    from python.harness.provider_rpc import handle_inference_save_providers

    conn = _FakeConn([_row("embedded-builtin", {"model": "qwen"})])
    monkeypatch.setattr(db, "get_pool", lambda: _FakePool(conn))

    await handle_inference_save_providers(
        {"providers": [{"id": "embedded-builtin", "type": "embedded",
                         "name": "Tala Embedded Engine", "priority": 1,
                         "status": "online", "locked": True, "config": {}}]},
        "req-3",
    )

    assert conn.executemany_calls == []


# ---------------------------------------------------------------------------
# _auto_detect_and_verify_providers: honoring the tombstone
# ---------------------------------------------------------------------------

class _FakeOllamaResponse:
    status_code = 200

    def json(self):
        return {"models": [{"name": "llama3.2:3b"}]}


class _FakeHttpClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        if "11434" in url:
            return _FakeOllamaResponse()
        raise ConnectionError("nothing listening")


def _patch_common(monkeypatch, dismissed_ids):
    from python.harness import server, db
    import httpx

    async def fake_load():
        return []  # nothing registered yet

    async def fake_fetch(sql, *args):
        assert "dismissed_inference_providers" in sql
        return [{"id": i} for i in dismissed_ids]

    inserted = []

    async def fake_execute(sql, *args, **kw):
        if "INSERT INTO inference_providers" in sql:
            inserted.append(args[0])
        return "OK"

    monkeypatch.setattr(server, "load_inference_providers", fake_load)
    monkeypatch.setattr(db, "fetch", fake_fetch)
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeHttpClient())
    return inserted


@pytest.mark.asyncio
async def test_a_dismissed_model_is_not_reregistered(monkeypatch):
    from python.harness import server

    inserted = _patch_common(monkeypatch, dismissed_ids={"ollama-llama3-2-3b"})

    await server._auto_detect_and_verify_providers()

    assert "ollama-llama3-2-3b" not in inserted


@pytest.mark.asyncio
async def test_a_non_dismissed_model_is_still_registered(monkeypatch):
    from python.harness import server

    inserted = _patch_common(monkeypatch, dismissed_ids=set())

    await server._auto_detect_and_verify_providers()

    assert "ollama-llama3-2-3b" in inserted
