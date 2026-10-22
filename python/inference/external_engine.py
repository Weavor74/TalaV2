"""
external_engine.py -- use an inference engine that is already on the machine.

The built-in engine (llama_server_manager.py) downloads a llama-server build
from the talacore.app manifest, and that manifest only carries Windows builds
today. On Linux, macOS and ARM boards (the Jetson) there is nothing to
download, so without this module Tala had no local brain at all there.

Three engines, one choice:

* ``builtin``  -- the downloaded fork build (Windows today). Supports the
  /v1/decision endpoint Jev mode uses.
* ``external`` -- a llama-server binary the user already has (stock upstream
  llama.cpp, a distro package, a Jetson build). Found via TALA_LLAMA_SERVER_BIN
  or on PATH. Stock builds lack --decision-seqs, so it is only passed when
  TALA_LLAMA_SERVER_FORK=1.
* ``ollama``   -- an Ollama daemon. Tala already auto-registers every chat
  model Ollama serves as a ``local`` provider (server.py
  _auto_detect_and_verify_providers) ahead of the embedded fallback, so this
  module only has to detect it and say which engine should be preferred.

Selection is ``TALA_ENGINE`` = auto | builtin | external | ollama (default
auto): builtin when its binary is present, else Ollama when it answers, else
an external llama-server, else builtin (which triggers the download where a
build exists).

Models (US-origin bases only: Meta Llama, OpenAI gpt-oss, Google Gemma):
``TALA_MODEL_PATH`` points at any local GGUF; otherwise ``TALA_MODEL_SIZE``
picks Llama 3.2 3B (default) or 1B (low-RAM boards).
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

from python.inference import profiles as _profiles

ENGINES = ("auto", "builtin", "external", "ollama")

#: US-origin defaults (Meta Llama 3.2), ungated GGUF conversions.
MODEL_CHOICES: dict[str, tuple[str, str]] = {
    "3b": ("bartowski/Llama-3.2-3B-Instruct-GGUF", "Llama-3.2-3B-Instruct-Q4_K_M.gguf"),
    "1b": ("bartowski/Llama-3.2-1B-Instruct-GGUF", "Llama-3.2-1B-Instruct-Q4_K_M.gguf"),
}
DEFAULT_MODEL_SIZE = "3b"
#: The matching Ollama tags, used when Ollama is the engine.
OLLAMA_MODEL_FOR_SIZE = {"3b": "llama3.2:3b", "1b": "llama3.2:1b"}


def _model_size() -> str:
    size = os.environ.get("TALA_MODEL_SIZE", DEFAULT_MODEL_SIZE).strip().lower()
    return size if size in MODEL_CHOICES else DEFAULT_MODEL_SIZE


def profile() -> _profiles.Profile | None:
    """The inference profile in force (kagura, nano, ...), or None for legacy env-driven setup."""
    return _profiles.active()


def default_model() -> tuple[str, str]:
    """(hf_repo, filename) of the GGUF the built-in/external server loads."""
    active = profile()
    if active is not None and active.main.hf_repo and active.main.hf_file:
        return active.main.hf_repo, active.main.hf_file
    return MODEL_CHOICES[_model_size()]


def context_size() -> int:
    """llama-server --ctx-size. TALA_LLAMA_CTX overrides; 4096 on the 1B
    low-RAM profile, 16384 otherwise. Measured on Kagura: Tala's system
    prompt plus 8 tool schemas plus one system.capabilities result is ~8.5k
    tokens, so 4096 (and 8192) rejected the second turn of an ordinary
    tool-using chat. Llama 3.2 3B's KV cache at 16k is ~1.8 GB."""
    raw = os.environ.get("TALA_LLAMA_CTX", "").strip()
    if raw.isdigit() and int(raw) >= 512:
        return int(raw)
    active = profile()
    if active is not None:
        return active.main.ctx
    return 4096 if _model_size() == "1b" else 16384


def explicit_model_path() -> Path | None:
    raw = os.environ.get("TALA_MODEL_PATH", "").strip()
    if not raw:
        active = profile()
        return active.main.model_path if active is not None and active.main.model_path.is_file() else None
    path = Path(raw).expanduser()
    return path if path.is_file() else None


def external_binary() -> Path | None:
    """A user-installed llama-server, or None."""
    raw = os.environ.get("TALA_LLAMA_SERVER_BIN", "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_file() else None
    active = profile()
    if active is not None and active.main.server_bin.is_file():
        return active.main.server_bin
    found = shutil.which("llama-server")
    return Path(found) if found else None


def external_is_fork() -> bool:
    """Whether the external binary is the parallel-decision fork."""
    raw = os.environ.get("TALA_LLAMA_SERVER_FORK", "").strip()
    if raw:
        return raw in ("1", "true", "yes")
    active = profile()
    return bool(active and active.main.fork)


def server_port() -> int:
    """Fixed llama-server port from the profile, or 0 to take any free one."""
    active = profile()
    return active.main.port if active is not None else 0


def server_extra_args() -> list[str]:
    """Profile flags appended to the llama-server command (``-ngl``, ``--n-cpu-moe``...)."""
    active = profile()
    return list(active.main.args) if active is not None else []


def startup_timeout() -> float | None:
    """Seconds to wait for llama-server's /health, or None for the module default."""
    active = profile()
    return active.main.startup_timeout_s if active is not None else None


def hf_cache_dir() -> str | None:
    """Where Hugging Face downloads land (D: on Kagura), or None for the library default.

    An HF_HOME / HF_HUB_CACHE the user already set wins (None: the library
    honours it itself); otherwise the profile's directory, so downloads never
    fall back to the C: profile default."""
    if os.environ.get("HF_HUB_CACHE", "").strip() or os.environ.get("HF_HOME", "").strip():
        return None
    active = profile()
    return str(active.hf_cache_dir) if active is not None and active.hf_cache_dir else None


def builtin_build_published() -> bool:
    """Whether talacore.app publishes a build for this OS/CPU (Windows x64 today)."""
    machine = platform.machine().lower()
    return sys.platform.startswith("win") and machine in ("amd64", "x86_64")


def _ollama_base_url() -> str:
    raw = os.environ.get("OLLAMA_HOST", "").strip()
    if not raw:
        return "http://localhost:11434"
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    if raw.count(":") < 2:
        raw = raw.rstrip("/") + ":11434"
    return raw.rstrip("/")


def _ollama_running(timeout: float = 1.5) -> bool:
    import httpx

    try:
        resp = httpx.get(f"{_ollama_base_url()}/api/tags", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def requested_engine() -> str:
    raw = os.environ.get("TALA_ENGINE", "").strip().lower()
    if raw in ENGINES:
        return raw
    # A profile names its own llama-server, so that is the engine.
    active = profile()
    if active is not None and active.main.server_bin.is_file():
        return "external"
    return "auto"


def _resolve_engine(builtin_present: bool, ollama_up: bool | None = None) -> str:
    """Which engine Tala should prefer right now. Pure apart from detection."""
    engine = requested_engine()
    if engine != "auto":
        return engine
    if builtin_present:
        return "builtin"
    if ollama_up is None:
        ollama_up = _ollama_running()
    if ollama_up:
        return "ollama"
    if external_binary() is not None:
        return "external"
    return "builtin"


def summary(builtin_present: bool) -> dict:
    """What the settings/status surface shows."""
    ext = external_binary()
    return {
        "requested": requested_engine(),
        "resolved": _resolve_engine(builtin_present),
        "builtin_present": builtin_present,
        "builtin_build_published": builtin_build_published(),
        "external_binary": str(ext) if ext else None,
        "ollama_url": _ollama_base_url(),
        "model_size": _model_size(),
        "model": "/".join(default_model()),
        "model_path": str(explicit_model_path() or "") or None,
        "profile": _profiles.describe(),
    }
