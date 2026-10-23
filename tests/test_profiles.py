"""
Inference profiles: what `kagura` and `nano` pin, where paths come from, how a
profile is selected, and that a selected profile actually reaches the
llama-server command line.
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
import types
from pathlib import Path

import pytest

from python.inference import external_engine as ext
from python.inference import llama_server_manager as lsm
from python.inference import profiles


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("TALA_PROFILE", "TALA_PROFILES_FILE", "TALA_ENGINE", "TALA_MODEL_PATH",
                "TALA_LLAMA_SERVER_BIN", "TALA_LLAMA_SERVER_FORK", "TALA_LLAMA_CTX",
                "TALA_MODEL_SIZE", "HF_HOME", "HF_HUB_CACHE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("TALA_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("TALA_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(ext.shutil, "which", lambda name: None)


def test_both_required_profiles_exist():
    assert {"kagura", "nano"} <= set(profiles.available())


def test_the_main_context_holds_a_real_turn():
    """A live turn's assembled prompt (persona, retrieved nodes, self-knowledge, stance) measured
    4,261 tokens. With ``-c 4096`` llama-server answered HTTP 400 "exceeds the available context
    size", so the 20B could not serve one roleplay turn and the router fell back to Ollama. Tala's
    own prompt is ~4.3k before the conversation starts and real ones reach ~11k."""
    assert profiles.get("kagura").main.ctx >= 8192


def test_the_documented_server_command_matches_the_profile():
    from pathlib import Path

    p = profiles.get("kagura")
    docs = (Path(__file__).resolve().parents[1] / "docs" / "inference-profiles.md").read_text(encoding="utf-8")
    assert f"-c {p.main.ctx} " in docs, "docs/inference-profiles.md shows a different context size"
    assert f"--n-cpu-moe {p.main.args[p.main.args.index('--n-cpu-moe') + 1]} " in docs


def test_kagura_pins_the_specified_stack():
    p = profiles.get("kagura")
    assert p.main.args == ("-ngl", "99", "--n-cpu-moe", "20")
    assert p.main.ctx == 16384
    assert p.main.harmony is True
    assert p.main.hf_repo == "mradermacher/gpt-oss-20b-heretic-GGUF"
    assert "MXFP4_MOE" in p.main.hf_file
    assert p.main.server_bin.name == "llama-server.exe"
    assert "llama-cuda-copy" in p.main.server_bin.parts
    assert p.fallback.model == "hf.co/mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated-GGUF:Q4_K_M"
    assert p.vision.model == "gemma3:4b" and p.vision.num_ctx == 8192 and p.vision.vision
    assert p.allow_remote is False
    assert p.vram_exclusive is True


def test_nano_is_a_single_small_llama_with_no_vision():
    p = profiles.get("nano")
    assert "Llama-3.2-3B" in p.main.hf_repo
    assert p.vision is None
    assert p.main.harmony is False
    assert p.fallback.model == "llama3.2:3b"


def test_reasoning_effort_follows_the_turn_type():
    kagura = profiles.get("kagura")
    assert kagura.reasoning_effort(has_tools=True) == "medium"
    assert kagura.reasoning_effort(has_tools=False) == "low"
    assert kagura.token_budget("medium") > kagura.token_budget("low") > 0
    # A non-harmony model has no reasoning setting to send.
    assert profiles.get("nano").reasoning_effort(has_tools=True) is None


def test_no_c_drive_anywhere_in_the_profile_defaults():
    text = profiles.BUILTIN_PROFILES_FILE.read_text(encoding="utf-8")
    assert not re.search(r"(?i)\bc:[\\/]", text)
    source = Path(profiles.__file__).read_text(encoding="utf-8")
    assert not re.search(r"(?i)['\"]c:[\\/]", source)


def test_default_roots_follow_the_install_drive_on_windows(monkeypatch):
    """No drive letter in the source: the defaults are on whichever drive Tala
    is installed on (D: on Kagura), so a USB-stick install works anywhere."""
    monkeypatch.delenv("TALA_TOOLS_DIR")
    monkeypatch.delenv("TALA_MODELS_DIR")
    monkeypatch.setattr(profiles, "sys", types.SimpleNamespace(platform="win32"))
    anchor = Path(profiles.__file__).resolve().anchor
    assert profiles._tools_dir() == f"{anchor}tools"
    assert profiles._models_dir() == f"{anchor}models"


def test_defaults_are_never_the_home_profile_on_windows(monkeypatch):
    monkeypatch.delenv("TALA_TOOLS_DIR")
    monkeypatch.setattr(profiles, "sys", types.SimpleNamespace(platform="win32"))
    assert "users" not in profiles._tools_dir().lower()


def test_paths_follow_the_tools_and_models_dirs(tmp_path):
    p = profiles.get("kagura")
    assert p.main.server_bin == tmp_path / "tools" / "llama-cuda-copy" / "llama-server.exe"
    assert p.main.model_path.parent == tmp_path / "models" / "gguf"
    assert p.hf_cache_dir == tmp_path / "models" / "hf-cache"


def test_selection_order_env_then_saved_choice(monkeypatch):
    assert profiles._requested_name() == "auto"
    profiles.set_active("nano")
    assert profiles._requested_name() == "nano"
    assert profiles.active().name == "nano"
    monkeypatch.setenv("TALA_PROFILE", "kagura")
    assert profiles.active().name == "kagura"
    monkeypatch.setenv("TALA_PROFILE", "legacy")
    assert profiles.active() is None


def test_unknown_profile_runs_without_one_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("TALA_PROFILE", "does-not-exist")
    assert profiles.active() is None
    with pytest.raises(ValueError):
        profiles.set_active("does-not-exist")


def test_auto_needs_the_machines_own_llama_server(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "sys", types.SimpleNamespace(platform="win32"))
    assert profiles.active() is None  # no llama-server on disk: stay on legacy
    exe = tmp_path / "tools" / "llama-cuda-copy" / "llama-server.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    assert profiles.active().name == "kagura"


def test_user_override_is_deep_merged(monkeypatch, tmp_path):
    override = tmp_path / "over.json"
    override.write_text(json.dumps({"kagura": {"main": {"port": 9123}, "vision": {"num_ctx": 4096}}}))
    monkeypatch.setenv("TALA_PROFILES_FILE", str(override))
    p = profiles.get("kagura")
    assert p.main.port == 9123
    assert p.main.args == ("-ngl", "99", "--n-cpu-moe", "20")  # untouched siblings survive
    assert p.vision.num_ctx == 4096 and p.vision.model == "gemma3:4b"


# --- the profile reaches the engine ---------------------------------------------


def _install_kagura_files(tmp_path):
    exe = tmp_path / "tools" / "llama-cuda-copy" / "llama-server.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    gguf = tmp_path / "models" / "gguf" / "gpt-oss-20b-heretic.MXFP4_MOE.gguf"
    gguf.parent.mkdir(parents=True)
    gguf.write_bytes(b"GGUF")
    return exe, gguf


def test_profile_drives_the_engine_settings(monkeypatch, tmp_path):
    exe, gguf = _install_kagura_files(tmp_path)
    monkeypatch.setenv("TALA_PROFILE", "kagura")
    assert ext.external_binary() == exe
    assert ext.explicit_model_path() == gguf
    assert ext.context_size() == 16384
    assert ext.requested_engine() == "external"
    assert ext.server_extra_args() == ["-ngl", "99", "--n-cpu-moe", "20"]
    assert ext.hf_cache_dir() == str(tmp_path / "models" / "hf-cache")
    assert "gpt-oss" in ext.default_model()[0]


def test_an_existing_hf_cache_setting_wins_over_the_profile_directory(monkeypatch):
    monkeypatch.setenv("TALA_PROFILE", "kagura")
    assert ext.hf_cache_dir() is not None
    monkeypatch.setenv("HF_HOME", "D:/my-existing-cache")
    assert ext.hf_cache_dir() is None  # the library honours HF_HOME itself


def test_environment_still_overrides_the_profile(monkeypatch, tmp_path):
    _install_kagura_files(tmp_path)
    other = tmp_path / "other.gguf"
    other.write_bytes(b"GGUF")
    monkeypatch.setenv("TALA_PROFILE", "kagura")
    monkeypatch.setenv("TALA_MODEL_PATH", str(other))
    monkeypatch.setenv("TALA_LLAMA_CTX", "8192")
    assert ext.explicit_model_path() == other
    assert ext.context_size() == 8192


def test_the_llama_server_command_carries_the_profile_flags(monkeypatch, tmp_path):
    exe, gguf = _install_kagura_files(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    override = tmp_path / "over.json"
    override.write_text(json.dumps({"kagura": {"main": {"port": free}}}))
    monkeypatch.setenv("TALA_PROFILE", "kagura")
    monkeypatch.setenv("TALA_PROFILES_FILE", str(override))
    seen: list[list[str]] = []

    class _Proc:
        pid = 1
        returncode = None
        stdout = None

        def kill(self):
            pass

    async def fake_exec(*args, **kwargs):
        seen.append([str(a) for a in args])
        return _Proc()

    async def unhealthy(*a, **k):
        return False

    async def no_pump(*a, **k):
        return None

    monkeypatch.setattr(lsm.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(lsm, "_wait_healthy", unhealthy)
    monkeypatch.setattr(lsm, "_pump_output", no_pump)
    asyncio.run(lsm._spawn(lsm.EXTERNAL))

    argv = seen[-1]
    assert argv[0] == str(exe) and argv[argv.index("-m") + 1] == str(gguf)
    assert argv[argv.index("--port") + 1] == str(free)
    assert argv[argv.index("--ctx-size") + 1] == "16384"
    assert "--jinja" in argv
    assert argv[argv.index("-ngl") + 1] == "99"
    assert argv[argv.index("--n-cpu-moe") + 1] == "20"
    assert "--decision-seqs" not in argv  # stock build, not the fork


def test_a_busy_configured_port_falls_back_to_a_free_one(monkeypatch, tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        override = tmp_path / "over.json"
        override.write_text(json.dumps({"kagura": {"main": {"port": port}}}))
        monkeypatch.setenv("TALA_PROFILE", "kagura")
        monkeypatch.setenv("TALA_PROFILES_FILE", str(override))
        assert lsm._choose_port() != port


def test_the_profile_is_part_of_the_status_surface(monkeypatch, tmp_path):
    _install_kagura_files(tmp_path)
    monkeypatch.setenv("TALA_PROFILE", "kagura")
    summary = ext.summary(builtin_present=False)
    assert summary["profile"]["active"]["name"] == "kagura"
    assert set(summary["profile"]["available"]) >= {"kagura", "nano"}
    assert sys.platform  # keep the import used on every platform
