"""Workstation tools end to end: approval routing, audit, timeouts, jobs, registry wiring."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from sdk.agent import workstation as ws
from sdk.agent import workstation_policy as pol

IS_WIN = os.name == "nt"


class MemoryStore:
    def __init__(self, fail: bool = False):
        self.rows: dict[str, dict] = {}
        self.fail = fail

    async def latest(self, digest):
        if self.fail:
            raise RuntimeError("db down")
        hits = [(tid, r) for tid, r in self.rows.items() if r["digest"] == digest]
        if not hits:
            return None
        tid, r = hits[-1]
        return tid, r["status"], r["payload"]

    async def create(self, digest, summary, payload):
        tid = f"wsapprove-{len(self.rows):04d}"
        self.rows[tid] = {"digest": digest, "status": "review", "payload": dict(payload), "summary": summary}
        return tid

    async def consume(self, tid):
        self.rows[tid]["status"] = "done"

    def approve(self, tid):
        self.rows[tid]["status"] = "ready"

    def reject(self, tid):
        self.rows[tid]["status"] = "failed"
        self.rows[tid]["payload"]["decision"] = "rejected"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "TalaWorkspace"
    root.mkdir()
    monkeypatch.setenv("TALA_WORKSTATION_ROOT", str(root))
    monkeypatch.setenv("TALA_WORKSTATION_CONFIG", str(tmp_path / "workstation.json"))
    for var in ("TALA_WORKSTATION_WRITE_ROOTS", "TALA_WORKSTATION_DENY", "TALA_WORKSTATION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ws, "_halted", lambda: False)
    store = MemoryStore()
    monkeypatch.setattr(ws, "_STORE", store)
    # run_python uses the current interpreter instead of building a venv in every test.
    monkeypatch.setattr(ws, "ensure_venv", lambda cfg: asyncio.sleep(0))
    monkeypatch.setattr(ws, "venv_python", lambda cfg: Path(sys.executable))
    return root, store, tmp_path


def _audit(root):
    path = root / ".tala" / "audit" / "workstation-audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_write_then_read_in_root(env):
    root, _, _ = env
    w = await ws.write_file("project-test/hello.txt", "hi there")
    assert w["success"] and (root / "project-test" / "hello.txt").read_text() == "hi there"
    r = await ws.read_file("project-test/hello.txt")
    assert r["success"] and r["content"] == "hi there"
    tools = [e["tool"] for e in _audit(root)]
    assert tools[-2:] == ["write_file", "read_file"]


@pytest.mark.asyncio
async def test_outside_root_write_is_held_then_runs_once_after_approval(env):
    root, store, tmp = env
    target = str(tmp / "outside.txt")
    first = await ws.write_file(target, "payload")
    assert first["approval_required"] and first["approval_status"] == "pending"
    assert not Path(target).exists()
    tid = first["approval_task_id"]
    again = await ws.write_file(target, "payload")
    assert again["approval_task_id"] == tid  # no duplicate request while pending

    different = await ws.write_file(target, "other payload")
    assert different["approval_task_id"] != tid  # approval is bound to the exact content

    store.approve(tid)
    done = await ws.write_file(target, "payload")
    assert done["success"] and Path(target).read_text() == "payload"
    assert store.rows[tid]["status"] == "done"

    reuse = await ws.write_file(target, "payload")
    assert reuse["approval_required"]  # single use


@pytest.mark.asyncio
async def test_rejected_action_stays_rejected(env):
    _, store, tmp = env
    first = await ws.write_file(str(tmp / "x.txt"), "a")
    store.reject(first["approval_task_id"])
    second = await ws.write_file(str(tmp / "x.txt"), "a")
    assert second["approval_status"] == "rejected" and "rejected" in second["error"]


@pytest.mark.asyncio
async def test_approval_store_down_fails_closed(env, monkeypatch):
    _, _, tmp = env
    monkeypatch.setattr(ws, "_STORE", MemoryStore(fail=True))
    res = await ws.write_file(str(tmp / "x.txt"), "a")
    assert res["approval_required"] and res["approval_status"] == "unavailable"
    assert not (tmp / "x.txt").exists()


@pytest.mark.asyncio
async def test_deny_listed_write_is_blocked_and_audited(env):
    root, store, tmp = env
    res = await ws.write_file(str(tmp / "Trike Trails" / "game.gd"), "x")
    assert res["denied"] and not store.rows
    assert not (tmp / "Trike Trails").exists()
    assert _audit(root)[-1]["decision"]["verdict"] == "deny"


@pytest.mark.asyncio
async def test_halt_refuses_everything(env, monkeypatch):
    monkeypatch.setattr(ws, "_halted", lambda: True)
    res = await ws.run_shell("echo hi")
    assert res["halted"]


@pytest.mark.asyncio
async def test_run_shell_captures_output_in_root(env):
    root, _, _ = env
    res = await ws.run_shell("echo workstation-ok")
    assert res["success"] and "workstation-ok" in res["stdout"]
    assert Path(res["cwd"]) == root.resolve()


@pytest.mark.asyncio
async def test_run_shell_delete_is_held(env):
    root, store, _ = env
    (root / "keep.txt").write_text("x")
    res = await ws.run_shell("Remove-Item keep.txt" if IS_WIN else "rm keep.txt")
    assert res["approval_required"] and (root / "keep.txt").exists()
    assert len(store.rows) == 1


@pytest.mark.asyncio
async def test_run_shell_timeout_kills(env):
    res = await ws.run_shell("Start-Sleep -Seconds 20" if IS_WIN else "sleep 20", timeout_seconds=2)
    assert res["timed_out"] and not res["success"] and res["duration_s"] < 15


@pytest.mark.asyncio
async def test_run_shell_truncates_long_output(env):
    root, _, tmp = env
    (tmp / "workstation.json").write_text(json.dumps({"max_output_chars": 2000}))
    cmd = "1..3000 | ForEach-Object { 'line' }" if IS_WIN else "for i in $(seq 3000); do echo line; done"
    res = await ws.run_shell(cmd)
    assert res["truncated"] and len(res["stdout"]) < 2200 and Path(res["full_output_path"]).is_file()


@pytest.mark.asyncio
async def test_cwd_outside_root_is_held(env):
    _, _, tmp = env
    res = await ws.run_shell("echo hi", cwd=str(tmp))
    assert res["approval_required"]


@pytest.mark.asyncio
async def test_list_dir_sorts_by_size_and_hides_internal(env):
    root, _, _ = env
    (root / "a").mkdir()
    (root / "a" / "big.bin").write_bytes(b"x" * 5000)
    (root / "small.txt").write_text("x")
    await ws.write_file("mid.txt", "x" * 100)  # also creates .tala/audit
    res = await ws.list_dir(".", recursive=True, sort_by="size", files_only=True, limit=2)
    names = [Path(e["path"]).name for e in res["entries"]]
    assert names == ["big.bin", "mid.txt"]
    assert all(".tala" not in e["path"] for e in res["entries"])


@pytest.mark.asyncio
async def test_search_files_finds_text(env):
    root, _, _ = env
    (root / "notes.md").write_text("alpha\nneedle here\n")
    res = await ws.search_files("needle")
    assert res["count"] == 1 and res["matches"][0]["line"] == 2


@pytest.mark.asyncio
async def test_delete_needs_approval_then_runs(env):
    root, store, _ = env
    (root / "old.txt").write_text("x")
    res = await ws.delete_path("old.txt")
    assert res["approval_required"] and (root / "old.txt").exists()
    store.approve(res["approval_task_id"])
    assert (await ws.delete_path("old.txt"))["success"] and not (root / "old.txt").exists()


@pytest.mark.asyncio
async def test_run_python_primes_and_saves(env):
    root, _, _ = env
    code = ("ps=[n for n in range(2,100) if all(n%d for d in range(2,int(n**.5)+1))][:20]\n"
            "open('primes.txt','w').write(' '.join(map(str,ps)))\nprint(ps)")
    res = await ws.run_python(code=code)
    assert res["success"], res
    assert (root / "primes.txt").read_text().split()[-1] == "71"


@pytest.mark.asyncio
async def test_run_python_network_code_is_held(env):
    res = await ws.run_python(code="import urllib.request\nurllib.request.urlopen('http://example.com')")
    assert res["approval_required"] and "network" in " ".join(res["reasons"])


@pytest.mark.asyncio
async def test_install_always_needs_approval(env):
    res = await ws.install_python_package(["requests"])
    assert res["approval_required"]
    assert (await ws.install_python_package(["--index-url", "x"]))["success"] is False


@pytest.mark.asyncio
async def test_background_job_lifecycle(env):
    cmd = "Start-Sleep -Seconds 30" if IS_WIN else "sleep 30"
    started = await ws.start_job(cmd, name="sleeper")
    assert started["success"] and started["status"] == "running"
    status = await ws.job_status(started["job_id"])
    assert status["status"] == "running"
    stopped = await ws.stop_job(started["job_id"])
    assert stopped["status"] == "stopped"


@pytest.mark.asyncio
async def test_job_limit(env, monkeypatch):
    _, _, tmp = env
    (tmp / "workstation.json").write_text(json.dumps({"max_jobs": 1}))
    cmd = "Start-Sleep -Seconds 30" if IS_WIN else "sleep 30"
    first = await ws.start_job(cmd)
    try:
        second = await ws.start_job(cmd)
        assert not second["success"] and "max 1" in second["error"]
    finally:
        await ws.stop_job(first["job_id"])


def test_tools_registered_and_in_core(env, monkeypatch):
    from python.harness.tool_selection import DEFAULT_CORE_TOOLS, core_tools
    from sdk.agent.tools import ToolRegistry
    from sdk.agent.workstation_tools import WORKSTATION_CORE_TOOLS, WORKSTATION_TOOLS

    for name in WORKSTATION_TOOLS:
        assert name in ToolRegistry._tools, name
    assert set(WORKSTATION_CORE_TOOLS) <= set(core_tools())
    monkeypatch.setenv("TALA_WORKSTATION", "0")
    assert core_tools() == DEFAULT_CORE_TOOLS


def test_workstation_tools_are_not_lease_gated_or_shadowed():
    """They carry their own argument-level gate; a tool-level 120s lease would
    block every harmless read. Also make sure no existing tool shares a name."""
    from sdk.agent import lease_policy
    from sdk.agent.workstation_tools import WORKSTATION_TOOLS

    for name in WORKSTATION_TOOLS:
        assert lease_policy._classify_tool(name) is None, name
    assert pol.INTERNAL_DIRNAME == ".tala"
