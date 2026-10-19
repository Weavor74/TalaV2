"""Settings > Workspace: no silent default, validation on save, live switching, RPCs."""
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
REPO = Path(pol.__file__).resolve().parents[2]


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A fresh data dir: no saved workspace, no env fallback."""
    cfg_file = tmp_path / "data" / "workstation.json"
    monkeypatch.setenv("TALA_WORKSTATION_CONFIG", str(cfg_file))
    for var in ("TALA_WORKSTATION_ROOT", "TALA_WORKSTATION_WRITE_ROOTS", "TALA_WORKSTATION_DENY",
                "TALA_WORKSTATION"):
        monkeypatch.delenv(var, raising=False)
    suggested = tmp_path / "SuggestedWorkspace"
    monkeypatch.setattr(pol, "suggested_root", lambda: str(suggested))
    monkeypatch.setattr(ws, "_halted", lambda: False)
    monkeypatch.setattr(ws, "_ensure_venv", lambda cfg: asyncio.sleep(0))
    monkeypatch.setattr(ws, "_venv_python", lambda cfg: Path(sys.executable))
    return tmp_path, cfg_file, suggested


def _audit(root: Path) -> list[dict]:
    path = root / ".tala" / "audit" / "workstation-audit.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# --- unconfigured ----------------------------------------------------------

@pytest.mark.asyncio
async def test_unconfigured_tools_refuse_and_never_create_the_suggestion(fresh):
    _, _, suggested = fresh
    cfg = ws.config()
    assert not cfg.configured and cfg.root is None
    calls = [ws.run_shell("echo hi"), ws.read_file("a.txt"), ws.write_file("a.txt", "x"), ws.list_dir("."),
             ws.search_files("x"), ws.delete_path("a.txt"), ws.run_python(code="print(1)"),
             ws.install_python_package("requests"), ws.start_job("echo hi")]
    for result in await asyncio.gather(*calls):
        assert result["success"] is False and result["workspace_configured"] is False
        assert "not configured" in result["error"] and "Settings > Workspace" in result["error"]
    assert not suggested.exists()


def test_status_offers_the_suggestion_only(fresh):
    _, _, suggested = fresh
    st = ws.workspace_status()
    assert st["configured"] is False and st["root"] is None
    assert st["suggested_root"] == str(suggested)


@pytest.mark.asyncio
async def test_invalid_saved_root_is_unconfigured_with_the_reason(fresh):
    _, cfg_file, _ = fresh
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text(json.dumps({"root": str(REPO / "scratch")}))
    res = await ws.write_file("a.txt", "x")
    assert res["workspace_configured"] is False and "overlaps Tala's own" in res["error"]
    assert ws.workspace_status()["problem"]


# --- validation --------------------------------------------------------------

def _bad(path, needle):
    check = pol.validate_workspace(str(path))
    assert not check.ok, path
    assert needle.lower() in check.reason.lower(), check.reason
    return check


def test_validation_rejects_dangerous_places(fresh, tmp_path):
    _bad("", "no folder")
    _bad("relative/folder", "full path")
    _bad(Path(tmp_path.anchor), "whole drive")
    home = Path.home()
    _bad(home, "user profile")
    _bad(home.parent, "contains your user profile")
    _bad(home / ".ssh" / "work", "ssh keys")
    _bad(home / ".config" / "tala", "app settings and credentials")
    if IS_WIN:
        _bad(home / "AppData" / "Roaming" / "x", "app settings and credentials")
        _bad(os.environ.get("SystemRoot", "C:/Windows"), "windows")
    else:
        _bad("/etc/tala", "system folder")
    _bad(tmp_path / "src" / "Trike Trails", "off limits")
    _bad(tmp_path / "antigravity-work", "off limits")
    _bad(tmp_path / ".gemini", "off limits")
    _bad(REPO, "overlaps Tala's own")
    _bad(REPO / "workspace", "overlaps Tala's own")
    _bad(REPO.parent, "overlaps Tala's own")
    (tmp_path / "afile").write_text("x")
    _bad(tmp_path / "afile", "is a file")


def test_validation_accepts_and_creates_only_on_save(fresh, tmp_path):
    target = tmp_path / "Work" / "Tala"
    dry = pol.validate_workspace(str(target))
    assert dry.ok and not dry.exists and not target.exists()
    saved = pol.validate_workspace(str(target), create=True)
    assert saved.ok and saved.created and target.is_dir()


# --- saving and live switching ---------------------------------------------

@pytest.mark.asyncio
async def test_set_workspace_persists_and_applies_without_restart(fresh, tmp_path):
    _, cfg_file, _ = fresh
    a, b = tmp_path / "WsA", tmp_path / "WsB"
    res = ws.set_workspace(str(a))
    assert res["success"] and res["configured"] and res["source"] == "settings" and res["created"]
    assert json.loads(cfg_file.read_text())["root"] == str(a.resolve())
    assert (await ws.write_file("note.txt", "in A"))["success"]
    assert (a / "note.txt").read_text() == "in A"
    assert ws._venv_dir(ws.config()) == a.resolve() / ".tala" / "venv"

    res = ws.set_workspace(str(b))
    assert res["success"] and res["root"] == str(b.resolve())
    assert (await ws.write_file("note.txt", "in B"))["success"]
    assert (b / "note.txt").read_text() == "in B" and (a / "note.txt").read_text() == "in A"
    assert ws._venv_dir(ws.config()) == b.resolve() / ".tala" / "venv"
    assert ws.config().audit_path == b.resolve() / ".tala" / "audit" / "workstation-audit.jsonl"
    # the switch is recorded in both workspaces' audit logs
    for root in (a, b):
        changes = [e for e in _audit(root) if e["tool"] == "workspace_change"]
        assert changes[-1]["args"]["to"] == str(b.resolve())
        assert changes[-1]["args"]["from"] == str(a.resolve())
    # the old workspace is now outside the sandbox: writing there needs approval
    held = await ws.write_file(str(a / "late.txt"), "x")
    assert held["success"] is False


def test_set_workspace_refreshes_sandbox_acls(fresh, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "sdk.agent.sandbox.refresh_workspace_isolation",
        lambda old, new: seen.append((list(old), list(new))),
    )
    a, b = tmp_path / "WsA", tmp_path / "WsB"
    assert ws.set_workspace(str(a))["success"]
    assert seen and str(a.resolve()) in seen[-1][1]
    assert ws.set_workspace(str(b))["success"]
    old, new = seen[-1]
    assert str(a.resolve()) in old
    assert str(b.resolve()) in new
    assert str(a.resolve()) not in new


@pytest.mark.asyncio
async def test_sandbox_fallback_is_loud_in_result_ui_and_audit(fresh, tmp_path, monkeypatch):
    a = tmp_path / "WsA"
    assert ws.set_workspace(str(a))["success"]
    notices = []

    async def fake_broadcast(reason):
        notices.append(reason)

    monkeypatch.setattr(ws, "_broadcast_sandbox_fallback", fake_broadcast)
    result = {"success": True, "exit_code": 0}
    await ws._apply_sandbox_status(
        ws.config(), result,
        "TALA_SANDBOX: job-object-only: AppContainer unavailable (missing APIs).\n",
        None, tool="run_shell",
    )
    assert result["sandbox_fallback"] is True
    assert result["sandbox"] == "job-object-only"
    assert "job-object-only" in result["warning"]
    assert "NOT OS-enforced" in result["warning"]
    assert notices and "NOT OS-enforced" in notices[0]
    entries = _audit(a)
    assert any(e["tool"] == "sandbox_fallback" for e in entries)


@pytest.mark.asyncio
async def test_sandbox_access_denied_names_the_path_in_result_and_audit(fresh, tmp_path):
    a = tmp_path / "WsA"
    assert ws.set_workspace(str(a))["success"]
    path = r"D:\venv\Scripts\python.exe"
    result = {"success": True, "exit_code": 5}
    await ws._apply_sandbox_status(
        ws.config(), result,
        f"TALA_SANDBOX: access-denied path={path}\n",
        None, tool="run_python",
    )
    assert result["success"] is False
    assert result["sandbox"] == "access-denied"
    assert path in result["error"]
    assert any(e["tool"] == "sandbox_access_denied" for e in _audit(a))


def test_set_workspace_rejects_bad_paths_and_keeps_the_old_one(fresh, tmp_path):
    good = tmp_path / "Good"
    assert ws.set_workspace(str(good))["success"]
    res = ws.set_workspace(str(Path(tmp_path.anchor)))
    assert res["success"] is False and res["field"] == "root" and "whole drive" in res["error"]
    assert ws.config().root == good.resolve()


def test_saved_setting_wins_over_env_fallback(fresh, tmp_path, monkeypatch):
    env_root = tmp_path / "FromEnv"
    monkeypatch.setenv("TALA_WORKSTATION_ROOT", str(env_root))
    cfg = ws.config()
    assert cfg.root == env_root.resolve() and cfg.root_source == "env"
    saved = tmp_path / "FromSettings"
    assert ws.set_workspace(str(saved))["success"]
    cfg = ws.config()
    assert cfg.root == saved.resolve() and cfg.root_source == "settings"


@pytest.mark.asyncio
async def test_extra_folders_are_writable_without_approval(fresh, tmp_path):
    root, extra = tmp_path / "Main", tmp_path / "Shared"
    res = ws.set_workspace(str(root), [str(extra)])
    assert res["success"] and res["extra_roots"] == [str(extra.resolve())]
    w = await ws.write_file(str(extra / "x.txt"), "ok")
    assert w["success"] and (extra / "x.txt").read_text() == "ok"
    bad = ws.set_workspace(str(root), [str(REPO)])
    assert bad["success"] is False and bad["field"] == "extra_roots"


@pytest.mark.asyncio
async def test_running_jobs_keep_running_across_a_switch(fresh, tmp_path):
    a, b = tmp_path / "JobA", tmp_path / "JobB"
    assert ws.set_workspace(str(a))["success"]
    cmd = "Start-Sleep -Seconds 30" if IS_WIN else "sleep 30"
    job = await ws.start_job(cmd, name="sleeper")
    try:
        res = ws.set_workspace(str(b))
        assert res["success"] and "keep running" in res["note"]
        assert job["job_id"] in [j["job_id"] for j in res["running_jobs"]]
        status = await ws.job_status(job["job_id"])
        assert status["status"] == "running" and status["workspace"] == str(a.resolve())
    finally:
        stopped = await ws.stop_job(job["job_id"])
    assert stopped["status"] == "stopped"
    assert any(e["tool"] == "stop_job" for e in _audit(a))  # audited where it ran


# --- RPCs ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rpcs(fresh, tmp_path):
    from python.harness import workstation_rpc as rpc

    async def call(handler, params):
        return json.loads(await handler(params, 1))

    got = await call(rpc._handle_workstation_get_config, {})
    assert got["result"]["configured"] is False
    bad = await call(rpc._handle_workstation_validate_workspace, {"path": str(Path(tmp_path.anchor))})
    assert bad["result"]["ok"] is False and "whole drive" in bad["result"]["reason"]
    refused = await call(rpc._handle_workstation_set_workspace, {"root": str(Path(tmp_path.anchor))})
    assert refused["result"]["success"] is False
    missing = await call(rpc._handle_workstation_set_workspace, {})
    assert "error" in missing
    ok = await call(rpc._handle_workstation_set_workspace, {"root": str(tmp_path / "ViaRpc")})
    assert ok["result"]["success"] and ok["result"]["root"] == str((tmp_path / "ViaRpc").resolve())
    assert "workstation.set_workspace" in rpc.WORKSTATION_RPC_HANDLERS
