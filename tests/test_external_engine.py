"""External engine support: use an installed Ollama or llama-server instead of
the talacore.app download (which only ships Windows builds), and US-origin
default models only."""
from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from python.inference import external_engine as ext
from python.inference import llama_server_manager as lsm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("TALA_ENGINE", "TALA_LLAMA_SERVER_BIN", "TALA_LLAMA_SERVER_FORK",
                "TALA_MODEL_SIZE", "TALA_MODEL_PATH", "OLLAMA_HOST", "TALA_LLAMA_CTX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ext.shutil, "which", lambda name: None)


def _fake_bin(tmp_path: Path) -> Path:
    exe = tmp_path / "llama-server"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_default_model_is_us_origin():
    repo, filename = ext.default_model()
    assert "Llama-3.2-3B" in repo and filename.endswith("Q4_K_M.gguf")
    for repo, filename in ext.MODEL_CHOICES.values():
        low = (repo + filename).lower()
        assert not any(bad in low for bad in ("qwen", "deepseek", "mistral"))
    assert "qwen" not in lsm.DEFAULT_MODEL_REPO.lower()


def test_low_ram_size_picks_1b(monkeypatch):
    monkeypatch.setenv("TALA_MODEL_SIZE", "1b")
    assert "1B" in ext.default_model()[0]
    monkeypatch.setenv("TALA_MODEL_SIZE", "nonsense")
    assert "3B" in ext.default_model()[0]


def test_explicit_model_path_wins(monkeypatch, tmp_path):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    monkeypatch.setenv("TALA_MODEL_PATH", str(gguf))
    assert lsm._model_path() == gguf
    assert lsm.check_model_cached()


def test_external_binary_from_env_and_path(monkeypatch, tmp_path):
    assert ext.external_binary() is None
    exe = _fake_bin(tmp_path)
    monkeypatch.setattr(ext.shutil, "which", lambda name: str(exe))
    assert ext.external_binary() == exe
    monkeypatch.setenv("TALA_LLAMA_SERVER_BIN", str(tmp_path / "missing"))
    assert ext.external_binary() is None


def test_resolution_order(monkeypatch, tmp_path):
    assert ext._resolve_engine(builtin_present=True, ollama_up=True) == "builtin"
    assert ext._resolve_engine(builtin_present=False, ollama_up=True) == "ollama"
    assert ext._resolve_engine(builtin_present=False, ollama_up=False) == "builtin"
    monkeypatch.setenv("TALA_LLAMA_SERVER_BIN", str(_fake_bin(tmp_path)))
    assert ext._resolve_engine(builtin_present=False, ollama_up=False) == "external"
    monkeypatch.setenv("TALA_ENGINE", "ollama")
    assert ext._resolve_engine(builtin_present=True, ollama_up=False) == "ollama"


def test_external_variant_is_preferred_when_no_builtin(monkeypatch, tmp_path):
    exe = _fake_bin(tmp_path)
    monkeypatch.setenv("TALA_LLAMA_SERVER_BIN", str(exe))
    monkeypatch.setattr(lsm, "builtin_variants", lambda: [])
    monkeypatch.setattr(lsm, "_detect_nvidia_gpu", lambda: False)
    monkeypatch.setattr(lsm, "_detect_vulkan", lambda: False)
    assert lsm._preferred_variant_order()[0] == lsm.EXTERNAL
    assert lsm._binary_path(lsm.EXTERNAL) == exe


def test_builtin_stays_first_when_present(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_LLAMA_SERVER_BIN", str(_fake_bin(tmp_path)))
    monkeypatch.setattr(lsm, "builtin_variants", lambda: ["cpu"])
    monkeypatch.setattr(lsm, "_detect_nvidia_gpu", lambda: False)
    monkeypatch.setattr(lsm, "_detect_vulkan", lambda: False)
    assert lsm._preferred_variant_order() == ["cpu"]


def test_stock_external_build_is_not_given_fork_flags(monkeypatch, tmp_path):
    exe = _fake_bin(tmp_path)
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    monkeypatch.setenv("TALA_LLAMA_SERVER_BIN", str(exe))
    monkeypatch.setenv("TALA_MODEL_PATH", str(gguf))
    seen: list[list[str]] = []

    class _Proc:
        pid = 1
        returncode = None
        stdout = None

        def kill(self):
            pass

    async def fake_exec(*args, **kwargs):
        seen.append(list(args))
        return _Proc()

    async def unhealthy(*a, **k):
        return False

    async def no_pump(*a, **k):
        return None

    monkeypatch.setattr(lsm.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(lsm, "_wait_healthy", unhealthy)
    monkeypatch.setattr(lsm, "_pump_output", no_pump)
    asyncio.run(lsm._spawn(lsm.EXTERNAL))
    assert "--decision-seqs" not in seen[-1] and "--jinja" in seen[-1]
    monkeypatch.setenv("TALA_LLAMA_SERVER_FORK", "1")
    asyncio.run(lsm._spawn(lsm.EXTERNAL))
    assert "--decision-seqs" in seen[-1]


def test_no_download_attempted_where_no_build_is_published(monkeypatch):
    monkeypatch.setattr(ext, "builtin_build_published", lambda: False)
    monkeypatch.setattr(lsm, "_preferred_variant_order", lambda: ["cpu"])
    monkeypatch.setattr(lsm, "_binary_path", lambda v: Path("/nonexistent/llama-server"))

    async def boom(variant):
        raise AssertionError("must not hit talacore.app")

    monkeypatch.setattr(lsm, "_download_binary", boom)
    lsm._download_task(None)
    status, _, error = lsm.get_status()
    assert status == "error" and "Ollama" in error
    lsm._set_status("not_downloaded")


def test_ollama_host_normalisation(monkeypatch):
    assert ext._ollama_base_url() == "http://localhost:11434"
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0")
    assert ext._ollama_base_url() == "http://0.0.0.0:11434"
    monkeypatch.setenv("OLLAMA_HOST", "http://box:9000/")
    assert ext._ollama_base_url() == "http://box:9000"


def test_summary_shape(monkeypatch):
    monkeypatch.setattr(ext, "_ollama_running", lambda timeout=1.5: False)
    s = ext.summary(builtin_present=False)
    assert s["resolved"] == "builtin" and s["model_size"] == "3b"
    assert "Llama-3.2-3B" in s["model"]


def test_progress_tracker_tolerates_the_rest_of_tqdms_surface():
    bar = lsm.DBProgressTracker(total=10)
    bar.set_postfix_str("x")
    bar.refresh()
    bar.set_description("y")
    bar.update(5)
    bar.close()


def test_context_size_profiles(monkeypatch):
    assert ext.context_size() == 16384
    monkeypatch.setenv("TALA_MODEL_SIZE", "1b")
    assert ext.context_size() == 4096
    monkeypatch.setenv("TALA_LLAMA_CTX", "8192")
    assert ext.context_size() == 8192
