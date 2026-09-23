"""
UI-audit Criticals C1–C6 (2026-09-17).

Focused regression coverage for the security-hardening slice:
  C1 fail-closed halt/lease exceptions
  C2 Model A halt gate at RPC dispatch + release_halt
  C3 no wsToken embed; ticket exchange; split HTTP/WS secrets
  C4 file:* must not read vault while vault:read is gated
  C5 task.clear confirm + halt gate
  C6 memory rebuild from disk recovers health
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk.agent.gates import RevocationHalt


@pytest.fixture(autouse=True)
def _always_release_halt():
    yield
    RevocationHalt.release_emergency_halt()


# --- C1 --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c1_lease_exception_fail_closed(monkeypatch):
    from sdk.agent import lease_policy
    from sdk.agent.tools import ToolRegistry

    RevocationHalt.release_emergency_halt()

    def boom(*a, **k):
        raise RuntimeError("lease exploded")

    monkeypatch.setattr(lease_policy, "guard", boom)
    result = await ToolRegistry.run_tool("get_system_info", {})
    assert result.get("success") is False
    assert result.get("lease_denied") is True


# --- C2 --------------------------------------------------------------------

def test_c2_halt_gate_blocks_destructive_when_halted():
    from python.harness import halt_gate

    RevocationHalt.trigger_emergency_halt()
    assert halt_gate.check_rpc("task.clear", {"confirm": "CLEAR_QUEUE"}, 1) is not None
    assert halt_gate.check_rpc("db.insert", {"sql": "SELECT 1"}, 1) is not None
    assert halt_gate.check_rpc("vault.read", {"id": "x"}, 1) is not None
    assert halt_gate.check_rpc("environment.set", {"preset": "lockdown"}, 1) is not None
    # resume blocked while halted
    assert halt_gate.check_rpc("task.control", {"task_id": "t", "action": "resume"}, 1) is not None
    # allowlist (status reads + abort/pause + release_halt only)
    assert halt_gate.check_rpc("harness.health", {}, 1) is None
    assert halt_gate.check_rpc("task.control", {"task_id": "t", "action": "abort"}, 1) is None
    assert halt_gate.check_rpc("task.control", {"task_id": "t", "action": "pause"}, 1) is None
    assert halt_gate.check_rpc("system.release_halt", {}, 1) is None
    # Tightened: operational surfaces are refused while halted
    assert halt_gate.check_rpc("system.self_test", {}, 1) is not None
    assert halt_gate.check_rpc("system.capabilities", {}, 1) is not None


def test_c2_halt_gate_fail_closed_on_read_error(monkeypatch):
    from python.harness import halt_gate

    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(RevocationHalt, "is_halted", boom)
    assert halt_gate.is_halted_fail_closed() is True
    assert halt_gate.check_rpc("db.insert", {}, 1) is not None


def test_c2_health_copy_does_not_say_resume_releases():
    from python.harness import server
    import inspect
    src = inspect.getsource(server._probe_halt)
    assert "Resume a task" not in src
    assert "system.release_halt" in src


def test_c2_dispatch_wires_halt_gate():
    import inspect
    from python.harness import server
    src = inspect.getsource(server._handle_connection)
    assert "halt_gate" in src or "_halt_check_rpc" in src
    webrtc = inspect.getsource(server._handle_webrtc_message)
    assert "halt_gate" in webrtc or "_halt_check_rpc" in webrtc


def test_c2_electron_injects_release_halt_secret():
    """Mission Control can call release_halt; Resume must not silently unlock."""
    main = Path("electron/main.js").read_text(encoding="utf-8")
    admin = Path("electron/harness_admin.js").read_text(encoding="utf-8")
    ops = Path("renderer/mission_control_ops.js").read_text(encoding="utf-8")
    assert "injectOwnerSecret" in main
    assert "system.release_halt" in admin
    assert "system.release_halt" in ops
    assert "release_emergency_halt" not in ops
    html = Path("renderer/index.html").read_text(encoding="utf-8")
    assert "btn-mc-release-halt" in html


# --- C3 --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c3_config_js_has_no_tokens():
    from python.harness import webui
    from aiohttp.test_utils import TestClient, TestServer

    app = webui._make_app(8765, "http-secret")
    async with TestClient(TestServer(app)) as client:
        res = await client.get(
            "/tala-web-config.js",
            headers={"X-Tala-Token": "http-secret"},
        )
        body = await res.text()
        assert res.status == 200
        assert "wsUrl" in body
        assert "wsTicketUrl" in body
        assert "http-secret" not in body
        assert '"token"' not in body
        assert '"wsToken"' not in body


@pytest.mark.asyncio
async def test_c3_ws_ticket_exchange_origin_and_auth():
    from python.harness import webui, ws_auth
    from aiohttp.test_utils import TestClient, TestServer

    ws_auth._TICKETS.clear()
    app = webui._make_app(8765, "http-secret")
    async with TestClient(TestServer(app)) as client:
        # No Origin → refuse before auth (ticket mint requires Origin)
        bare = await client.post("/api/ws-ticket")
        assert bare.status == 403

        # Origin present but unauthorised → 401
        unauth = await client.post(
            "/api/ws-ticket",
            headers={"Origin": "http://127.0.0.1:8767"},
        )
        assert unauth.status == 401

        # Absent Origin must not mint even with a valid token
        no_origin = await client.post(
            "/api/ws-ticket",
            headers={"X-Tala-Token": "http-secret"},
        )
        assert no_origin.status == 403

        bad_origin = await client.post(
            "/api/ws-ticket",
            headers={
                "X-Tala-Token": "http-secret",
                "Origin": "https://evil.example",
            },
        )
        assert bad_origin.status == 403

        ok = await client.post(
            "/api/ws-ticket",
            headers={
                "X-Tala-Token": "http-secret",
                "Origin": "http://127.0.0.1:8767",
            },
        )
        assert ok.status == 200
        assert ok.headers.get("Cache-Control") == "no-store"
        data = await ok.json()
        assert data["ticket"].startswith("tala-ws-ticket:")
        assert data["expires_in"] >= 5


def test_c3_shim_fetches_ticket_not_config_wstoken():
    shim = Path("renderer/tala-web-shim.js").read_text(encoding="utf-8")
    assert "CONFIG.wsToken" not in shim
    assert "ws-ticket" in shim or "wsTicketUrl" in shim
    assert "X-Tala-Token" in shim
    assert "history.replaceState" in shim


def test_c3_electron_splits_ws_and_http_tokens():
    """Architect C3: Electron must not copy WS secret into TALA_WEB_TOKEN."""
    main = Path("electron/main.js").read_text(encoding="utf-8")
    token_mod = Path("electron/token_files.js").read_text(encoding="utf-8")

    ensure_ws_body = (
        main.split("function ensureWsToken(appPath)", 1)[1]
        .split("{", 1)[1]
        .split("}", 1)[0]
    )
    assert "ensureWsTokenFile" in ensure_ws_body
    assert "TALA_WEB_TOKEN" not in ensure_ws_body

    env_fn = main.split("function firstRunEnv(appPath)", 1)[1].split("function ", 1)[0]
    assert "TALA_WS_TOKEN: wsToken" in env_fn
    assert "TALA_WEB_TOKEN: webUiToken" in env_fn
    assert "TALA_WEB_TOKEN: ensureWsToken" not in env_fn
    assert "ensureWebUiToken" in env_fn

    # Main-process WS client authenticates with the WS credential via header.
    connect = main.split("function connectWithRetry", 1)[1].split("candidate.once('open'", 1)[0]
    assert "ensureWsToken" in connect
    assert "Authorization" in connect
    assert "ensureWebUiToken" not in connect

    assert "web_ui_token.txt" in token_mod
    assert "ws_token.txt" in token_mod
    # Stop before the next JSDoc so we only assert on ensureWsToken itself.
    ws_helper = token_mod.split("function ensureWsToken", 1)[1].split("/**", 1)[0]
    assert "env.TALA_WS_TOKEN" in ws_helper
    assert "env.TALA_WEB_TOKEN" not in ws_helper
    web_helper = token_mod.split("function ensureWebUiToken", 1)[1]
    assert "env.TALA_WEB_TOKEN" in web_helper
    assert "env.TALA_WS_TOKEN" not in web_helper.split("{", 1)[1].split("}", 1)[0]


# --- C4 --------------------------------------------------------------------

def test_c4_webui_file_read_refuses_vault(tmp_path):
    from python.harness import webui
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "secret.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(PermissionError):
        webui._bridge_file_read(tmp_path, {"filePath": "vault/secret.txt"})


def test_c4_electron_refuse_secret_path_exported():
    src = Path("electron/ipc-handlers.js").read_text(encoding="utf-8")
    assert "function refuseSecretPath" in src
    assert "refuseSecretPath(userData, resolved)" in src


# --- C5 --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c5_task_clear_requires_confirm_and_refuses_when_halted(monkeypatch):
    from python.harness import task_rpc

    async def boom_db(*a, **k):
        raise AssertionError("TRUNCATE must not run")

    monkeypatch.setattr(task_rpc.db, "execute", boom_db)

    raw = await task_rpc.handle_task_clear({}, 1)
    err = json.loads(raw)["error"]
    assert "CLEAR_QUEUE" in err["message"]

    # confirm alone is not enough — harness_secret required
    raw_sec = await task_rpc.handle_task_clear({"confirm": "CLEAR_QUEUE"}, 9)
    err_sec = json.loads(raw_sec)["error"]
    assert "secret" in err_sec["message"].lower() or "unauthorized" in err_sec["message"].lower()

    RevocationHalt.trigger_emergency_halt()
    raw2 = await task_rpc.handle_task_clear(
        {"confirm": "CLEAR_QUEUE", "harness_secret": "x"}, 2
    )
    err2 = json.loads(raw2)["error"]
    assert "halt" in err2["message"].lower()

    # Renderer must send CLEAR_QUEUE
    ops = Path("renderer/mission_control_ops.js").read_text(encoding="utf-8")
    assert "CLEAR_QUEUE" in ops
    assert "task.clear" in ops


# --- C6 --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c6_rebuild_from_disk_indexes_html(tmp_path, monkeypatch):
    from python.harness import memory, retrieval

    # Minimal valid-enough HTML-NN stubs: parse_file may be strict; if so,
    # mock reindex_node and assert chroma path still runs.
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    sample = nodes / "skill-demo.html"
    sample.write_text(
        '<!DOCTYPE html><html><head>'
        '<meta name="tala-node-id" content="skill-demo"/>'
        '<meta name="tala-node-type" content="skill"/>'
        '</head><body><article data-semantic-memory="hello memory">'
        '<h1>Demo</h1></article></body></html>',
        encoding="utf-8",
    )

    # Point chroma at a temp dir so we do not touch the real store.
    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    monkeypatch.setattr(retrieval, "CHROMA_DATA_DIR", chroma_dir)

    import chromadb
    client = chromadb.PersistentClient(path=str(chroma_dir))
    coll = client.get_or_create_collection(name="tala_neural_nodes")
    monkeypatch.setattr(retrieval, "node_collection", coll)
    retrieval._INDEXED_MTIMES.clear()

    async def fake_reindex(params):
        return {"status": "success", "node_id": "skill-demo"}

    monkeypatch.setattr(memory, "handle_memory_reindex_node", fake_reindex)

    class FakeNode:
        node_id = "skill-demo"
        node_type = "skill"
        semantic_memory = "hello memory"
        governance_status = "active"
        weight_relevance = 1.0
        weight_confidence = 1.0
        trust_tier = "verified"
        source = "local"
        state_data = {"title": "Demo"}
        metadata = {}
        relationships = []

    # retrieval imports parse_file at module level — patch that binding.
    monkeypatch.setattr(retrieval, "parse_file", lambda p: FakeNode())

    from dataclasses import replace as _dc_replace
    from python.harness import config as harness_config
    monkeypatch.setattr(
        harness_config,
        "settings",
        _dc_replace(harness_config.settings, REPO_ROOT=tmp_path),
    )

    result = await memory.handle_memory_rebuild_from_disk({"force_all": True})
    assert result["status"] == "success"
    assert result["disk_html"] >= 1
    assert result["chroma_indexed"] >= 1
    assert result["fts_failed"] == 0
    # nodes_dir is pinned — caller cannot redirect it
    assert Path(result["nodes_dir"]).resolve() == nodes.resolve()

    status, detail = await __import__(
        "python.harness.server", fromlist=["_probe_memory"]
    )._probe_memory()
    assert retrieval.node_collection is not None
    assert retrieval.node_collection.count() > 0
    # Honest recovery: must not still report empty-index CRITICAL.
    assert "no HTML-NN indexed" not in detail
    assert status in ("HEALTHY", "UNKNOWN", "DEGRADED")



@pytest.mark.asyncio
async def test_c6_rebuild_fails_when_fts_fails(tmp_path, monkeypatch):
    from python.harness import memory, retrieval, config as harness_config

    nodes = tmp_path / "nodes"
    nodes.mkdir()
    (nodes / "skill-demo.html").write_text("<html></html>", encoding="utf-8")
    from dataclasses import replace as _dc_replace
    monkeypatch.setattr(
        harness_config,
        "settings",
        _dc_replace(harness_config.settings, REPO_ROOT=tmp_path),
    )

    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    import chromadb
    coll = chromadb.PersistentClient(path=str(chroma_dir)).get_or_create_collection(
        name="tala_neural_nodes"
    )
    monkeypatch.setattr(retrieval, "node_collection", coll)
    retrieval._INDEXED_MTIMES.clear()

    class FakeNode:
        node_id = "skill-demo"
        node_type = "skill"
        semantic_memory = "x"
        governance_status = "active"
        weight_relevance = 1.0
        weight_confidence = 1.0
        trust_tier = "verified"
        source = "local"
        state_data = {"title": "Demo"}
        metadata = {}
        relationships = []

    monkeypatch.setattr(retrieval, "parse_file", lambda p: FakeNode())

    async def fail_reindex(params):
        return {"status": "failed", "error": "boom"}

    monkeypatch.setattr(memory, "handle_memory_reindex_node", fail_reindex)
    result = await memory.handle_memory_rebuild_from_disk({"force_all": True})
    assert result["status"] == "failed"
    assert result["fts_failed"] >= 1


@pytest.mark.asyncio
async def test_c6_empty_disk_is_not_silent_success(tmp_path, monkeypatch):
    from python.harness import memory, retrieval, config as harness_config

    nodes = tmp_path / "nodes"
    nodes.mkdir()
    from dataclasses import replace as _dc_replace
    monkeypatch.setattr(
        harness_config,
        "settings",
        _dc_replace(harness_config.settings, REPO_ROOT=tmp_path),
    )
    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    import chromadb
    coll = chromadb.PersistentClient(path=str(chroma_dir)).get_or_create_collection(
        name="tala_neural_nodes"
    )
    monkeypatch.setattr(retrieval, "node_collection", coll)
    result = await memory.handle_memory_rebuild_from_disk({})
    assert result["status"] == "failed"
    empty = await memory.handle_memory_rebuild_from_disk({"allow_empty": True})
    assert empty["status"] == "empty"
