"""
llama_server_manager.py — Tala Cognitive Engine Room: embedded llama.cpp engine
================================================================================
Replaces embedded_engine.py (HuggingFace Transformers) and vllm_engine.py
(never wired into actual generation) with one real local engine: a
``llama-server`` subprocess built from a custom llama.cpp fork
(github.com/thecodacus/llama.cpp @ parallel-decision) that speaks the ordinary
OpenAI-compatible ``/v1/chat/completions`` API plus a second endpoint,
``/v1/decision``, for single forward-pass structured/constrained decisions
("Jev mode").

Both modes run on the same already-running server process -- there is no
separate binary, spawn, or restart to go from one to the other. This module
exposes both as first-class capabilities:

  * ``generate_stream()`` -- ordinary chat completion (normal mode). Every
    existing call site in server.py used this exact signature against
    embedded_engine.py, so only the import needed to change.
  * ``decide()`` -- structured decisions via /v1/decision (Jev mode). Not yet
    called from any Tala call site; wiring specific heuristics (chat-mode
    classification, tool selection, etc.) to use it is a separate task. This
    function is the low-level capability that follow-up work will call.

WHY A SUBPROCESS, NOT A LIBRARY
--------------------------------
The fork's structured-decision feature lives in the C++ server binary itself
(``tools/parallel-decision``), not in any Python binding. Talking to it means
running the compiled ``llama-server`` and speaking HTTP to it, the same way
Tala already does for Ollama and other local/remote providers.

PROCESS LIFECYCLE
------------------
The server is spawned lazily on first use and supervised through
``sdk.gateway.lifecycle``'s process registry (``register_process`` /
``get_process_status`` / ``shutdown_all``) -- until this module, nothing ever
called ``register_process``, so the health RPC that reports
"mcp_servers_tracked" (server.py's ``handle_...`` around line 10285) always
read zero. This is that registry's first real caller.

On an unexpected exit, ``_supervise()`` restarts with exponential backoff and,
if the current GPU variant fails to come up healthy after a few attempts,
falls back down the chain (cuda -> vulkan -> cpu) rather than retrying the
same variant forever.

BINARY + MODEL DISTRIBUTION
-----------------------------
Tala's shipped update zips are small (~6MB, source-only) and the updater
re-downloads the whole zip on every update -- baking a few hundred MB of
native binaries into that zip would multiply every future update's size by
20-100x. So the binary is fetched the same way model weights already are:
downloaded on demand, hash-verified, and versioned independently of the app,
via a small manifest (``BINARIES_MANIFEST_URL``) published alongside
``version.json``. See ``scripts/build_llama_engine.ps1`` for how the binaries
themselves get built and published -- that is a release-engineering step, not
something this module or any running Tala instance does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from python.inference import external_engine as _ext
from python.inference import harmony as _harmony

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

#: The pinned commit of github.com/thecodacus/llama.cpp (branch
#: parallel-decision) that scripts/build_llama_engine.ps1 built. Bump this,
#: and llama_binaries.json, together when the fork moves.
BINARY_VERSION = "ad129b08d9f134cd298d1f8a85efc52b1b66e18e"

#: Hosted alongside version.json -- see scripts/upload_vps.ps1's webroot.
BINARIES_MANIFEST_URL = "https://talacore.app/llama_binaries.json"

#: Default model: a single GGUF file, not a full HF snapshot -- llama.cpp
#: needs one file, not the safetensors/tokenizer set transformers used.
#:
#: Llama 3.2 3B Instruct (Meta, US-origin): the project rule is US-origin base
#: models only. A profile (python/inference/profiles.py) replaces this with its
#: own main model, e.g. gpt-oss-20b on Kagura. 1B is the low-RAM option (TALA_MODEL_SIZE=1b) and TALA_MODEL_PATH loads any local
#: GGUF -- see python/inference/external_engine.py.
#:
#: History, kept for the size reasoning: 1.5B, not 0.5B: measured directly against Jev-mode structured decisions
#: (chat_mode.determine_effective_mode()'s two-boolean classification), 0.5B
#: was chance-level regardless of schema framing (3-way enum, combined
#: booleans, isolated single boolean, with and without few-shot instructions
#: -- none beat 50% on a plain yes/no question). 1.5B scored 10/12 fields
#: correct on the same test set, at ~260ms per decision -- still negligible
#: latency. This also improves ordinary chat quality, not just Jev mode.
DEFAULT_MODEL_REPO, DEFAULT_MODEL_FILENAME = _ext.default_model()

#: Sequence slots reserved for /v1/decision. Below 3 the endpoint refuses
#: outright ("decisions are disabled"); 8 comfortably covers a handful of
#: concurrent structured-decision calls without holding much unified-KV-cache
#: overhead when Jev mode is not being used at all. See
#: tools/parallel-decision/README.md.
DECISION_SEQS = 8

_HEALTH_POLL_INTERVAL_S = 0.5
#: Generous on purpose -- Tala targets modest hardware, and a cold model load
#: competing with other CPU-bound work on the machine can take well past the
#: few seconds it takes on a quiet system.
_HEALTH_POLL_TIMEOUT_S = 60.0
_MAX_RESTARTS_PER_VARIANT = 3
_RESTART_BASE_DELAY_S = 2.0

#: Preference order when nothing pins a variant: best perf first, universal
#: compatibility last.
VARIANT_CHAIN: tuple[str, ...] = ("cuda", "vulkan", "cpu")


def _engine_root() -> Path:
    """<user data>/engines/llama-server -- alongside nodes/ and workflows/."""
    from sdk.agent.documentation_nodes import resolve_user_data_dir

    root = resolve_user_data_dir() / "engines" / "llama-server"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _variant_dir(variant: str) -> Path:
    return _engine_root() / BINARY_VERSION / variant


def _server_exe_name() -> str:
    return "llama-server.exe" if sys.platform.startswith("win") else "llama-server"


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


#: A user-installed llama-server (TALA_LLAMA_SERVER_BIN or PATH); see external_engine.py.
EXTERNAL = "external"


def _binary_path(variant: str) -> Path:
    if variant == EXTERNAL:
        ext = _ext.external_binary()
        return ext if ext is not None else Path("<no external llama-server>")
    return _variant_dir(variant) / _server_exe_name()


def builtin_variants() -> list[str]:
    """Downloaded built-in variants, in preference order."""
    return [v for v in VARIANT_CHAIN if _binary_path(v).exists()]


def available_variants() -> list[str]:
    """Which variants can be started now, in preference order."""
    found = builtin_variants()
    if _ext.external_binary() is not None:
        if _ext.requested_engine() == EXTERNAL or not found:
            found.insert(0, EXTERNAL)
        else:
            found.append(EXTERNAL)
    return found


# ---------------------------------------------------------------------------
# Status -- mirrors embedded_engine.py's public shape so every existing call
# site (server.py, memory.py, queue.py) needed only its import changed.
# ---------------------------------------------------------------------------

_download_status = "not_downloaded"  # "not_downloaded" | "downloading" | "ready" | "error"
_download_progress = 0.0
_download_error = ""
_status_lock = threading.Lock()


def _set_status(status: str, progress: float = 0.0, error: str = "") -> None:
    global _download_status, _download_progress, _download_error
    with _status_lock:
        _download_status = status
        _download_progress = round(progress, 1)
        _download_error = error


def get_status() -> tuple[str, float, str]:
    """(status, progress, error) for the binary+model pair as a whole."""
    with _status_lock:
        return _download_status, _download_progress, _download_error


def check_model_cached() -> bool:
    """Whether the default GGUF model is already downloaded."""
    if _ext.explicit_model_path() is not None:
        return True
    try:
        from huggingface_hub import try_to_load_from_cache

        path = try_to_load_from_cache(*_ext.default_model(), cache_dir=_ext.hf_cache_dir())
        return isinstance(path, str)
    except Exception as exc:
        logger.debug("llama_server_manager: model cache check failed: %s", exc)
        return False


def _model_path() -> Path | None:
    explicit = _ext.explicit_model_path()
    if explicit is not None:
        return explicit
    try:
        from huggingface_hub import try_to_load_from_cache

        path = try_to_load_from_cache(*_ext.default_model(), cache_dir=_ext.hf_cache_dir())
        return Path(path) if isinstance(path, str) else None
    except Exception:
        return None


def init_status_from_cache() -> None:
    """Called at startup: sync in-memory status with what's already on disk."""
    if check_model_cached() and available_variants():
        _set_status("ready", progress=100.0)
    else:
        _set_status("not_downloaded", progress=0.0)


# ---------------------------------------------------------------------------
# Binary download (manifest -> hash-verified fetch into _variant_dir)
# ---------------------------------------------------------------------------


class DBProgressTracker:
    """tqdm-compatible callback used by huggingface_hub's download, forwarding
    aggregate progress into the module's status the same way
    embedded_engine.py's DBProgressTracker did."""

    _active: dict[int, dict[str, Any]] = {}
    _lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        self.n = 0
        self.total = kwargs.get("total", 0) or 0
        with self._lock:
            self._active[id(self)] = {"n": 0, "total": self.total}

    def update(self, n: int = 1) -> None:
        self.n += n
        with self._lock:
            if id(self) in self._active:
                self._active[id(self)]["n"] = self.n
            total_bytes = sum(b["total"] for b in self._active.values() if b["total"] > 0)
            done_bytes = sum(b["n"] for b in self._active.values() if b["total"] > 0)
            if total_bytes > 0:
                pct = min(99.0, (done_bytes / total_bytes) * 100)
                _set_status("downloading", progress=pct)

    def close(self) -> None:
        with self._lock:
            self._active.pop(id(self), None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # huggingface_hub's Xet transfer path (the default for GGUF repos since
    # hf_xet ships with it) drives the bar through more of tqdm's surface:
    # set_postfix_str, refresh, set_description... Missing any one raised
    # AttributeError mid-download and left a fresh install with no model.
    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


async def _fetch_manifest() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(BINARIES_MANIFEST_URL)
        resp.raise_for_status()
        return resp.json()


async def _download_binary(variant: str) -> None:
    """Download + sha256-verify one variant's archive, unpack into _variant_dir."""
    import hashlib
    import tarfile
    import zipfile
    import tempfile

    manifest = await _fetch_manifest()
    entry = (
        manifest.get(_platform_key(), {})
        .get(variant, {})
    )
    url = entry.get("url")
    sha256 = entry.get("sha256")
    if not url or not sha256:
        raise RuntimeError(
            f"llama_binaries.json has no {variant!r} entry for {_platform_key()!r}"
        )

    dest_dir = _variant_dir(variant)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        hasher = hashlib.sha256()
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0) or 0)
                downloaded = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        f.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            _set_status("downloading", progress=min(99.0, downloaded / total * 100))

        digest = hasher.hexdigest()
        if digest != sha256:
            raise RuntimeError(
                f"llama-server ({variant}) hash mismatch: expected {sha256}, got {digest}"
            )

        if url.endswith(".zip"):
            with zipfile.ZipFile(tmp_path) as zf:
                zf.extractall(dest_dir)
        else:
            with tarfile.open(tmp_path) as tf:
                tf.extractall(dest_dir)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not _binary_path(variant).exists():
        raise RuntimeError(f"{variant} archive did not contain {_server_exe_name()}")


def _detect_nvidia_gpu() -> bool:
    try:
        from python.harness.system_info import has_gpu

        return bool(has_gpu())
    except Exception as exc:
        logger.debug("llama_server_manager: NVIDIA GPU detection failed: %s", exc)
        return False


def _detect_vulkan() -> bool:
    """Whether a Vulkan ICD is present (driver-provided runtime, not the SDK)."""
    exe = shutil.which("vulkaninfo")
    if not exe:
        return False
    try:
        import subprocess as _sp

        result = _sp.run([exe, "--summary"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception as exc:
        logger.debug("llama_server_manager: Vulkan detection failed: %s", exc)
        return False


def _preferred_variant_order() -> list[str]:
    """Which variants to try, in order, given this machine's hardware."""
    order: list[str] = []
    if _ext.external_binary() is not None and (
        _ext.requested_engine() == EXTERNAL or not builtin_variants()
    ):
        order.append(EXTERNAL)
    if _detect_nvidia_gpu():
        order.append("cuda")
    if _detect_vulkan():
        order.append("vulkan")
    order.append("cpu")
    # De-dupe while preserving order.
    seen: set[str] = set()
    return [v for v in order if not (v in seen or seen.add(v))]


def _download_task(main_loop: asyncio.AbstractEventLoop | None) -> None:
    """Background thread: download whichever variant fits this machine, then
    the default GGUF model. Mirrors embedded_engine.py's _download_task."""
    from python.harness import db

    def _run_db(coro) -> None:
        if main_loop is not None and main_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, main_loop)
            try:
                future.result(timeout=5.0)
            except Exception as exc:
                logger.warning("llama_server_manager: DB update failed: %s", exc)
        else:
            logger.warning("llama_server_manager: main loop not running; skipping DB update.")

    try:
        _set_status("downloading", progress=0.0)
        _run_db(db.log_to_db("INFO", "Starting background download of the llama.cpp engine", {}))
        _run_db(db.execute("UPDATE inference_providers SET status = 'downloading' WHERE id = 'embedded-builtin'"))

        variant = _preferred_variant_order()[0]
        if not _binary_path(variant).exists():
            if not _ext.builtin_build_published():
                raise RuntimeError(
                    "No built-in llama.cpp build is published for this platform. Install "
                    "Ollama, or set TALA_LLAMA_SERVER_BIN to an existing llama-server."
                )
            asyncio.run(_download_binary(variant))

        if not check_model_cached():
            from huggingface_hub import hf_hub_download

            repo_id, filename = _ext.default_model()
            hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                tqdm_class=DBProgressTracker,
                cache_dir=_ext.hf_cache_dir(),
            )

        _set_status("ready", progress=100.0)
        _run_db(db.log_to_db("INFO", "llama.cpp engine ready.", {}))
        _run_db(db.execute("UPDATE inference_providers SET status = 'online' WHERE id = 'embedded-builtin'"))
    except Exception as exc:
        logger.exception("llama_server_manager: background download failed")
        _set_status("error", progress=0.0, error=str(exc))
        _run_db(db.log_to_db("ERROR", f"Failed to prepare the llama.cpp engine: {exc}", {"error": str(exc)}))
        _run_db(db.execute("UPDATE inference_providers SET status = 'offline' WHERE id = 'embedded-builtin'"))


def start_download_async() -> None:
    """Trigger the background binary+model download if not already running/ready."""
    with _status_lock:
        if _download_status in ("downloading", "ready"):
            return

    try:
        main_loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            main_loop = asyncio.get_event_loop()
        except RuntimeError:
            main_loop = None

    thread = threading.Thread(
        target=_download_task, args=(main_loop,), name="llama-engine-downloader", daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------------------
# Process lifecycle -- built on sdk.gateway.lifecycle's registry
# ---------------------------------------------------------------------------


class _ServerHandle:
    def __init__(self, proc: asyncio.subprocess.Process, port: int, variant: str) -> None:
        self.proc = proc
        self.port = port
        self.variant = variant
        self.base_url = f"http://127.0.0.1:{port}"
        self.restarts = 0


_server: _ServerHandle | None = None
_server_lock = asyncio.Lock()


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _choose_port() -> int:
    """The profile's fixed port when it is free, otherwise any free port."""
    wanted = _ext.server_port()
    if wanted:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", wanted))
                return wanted
            except OSError:
                logger.warning(
                    "llama_server_manager: configured port %d is busy; using a free port.", wanted,
                )
    return _free_port()


async def _wait_healthy(base_url: str, timeout_s: float | None = None) -> bool:
    timeout_s = timeout_s or _ext.startup_timeout() or _HEALTH_POLL_TIMEOUT_S
    deadline = asyncio.get_running_loop().time() + timeout_s
    async with httpx.AsyncClient(timeout=3.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                resp = await client.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(_HEALTH_POLL_INTERVAL_S)
    return False


async def _pump_output(proc: asyncio.subprocess.Process, variant: str) -> None:
    """Drain and log llama-server's stdout/stderr for the life of the process."""
    if proc.stdout is None:
        return
    try:
        async for line in proc.stdout:
            logger.debug("llama-server[%s]: %s", variant, line.decode(errors="replace").rstrip())
    except Exception:
        pass


async def _spawn(variant: str) -> _ServerHandle | None:
    from sdk.gateway import lifecycle

    exe = _binary_path(variant)
    model = _model_path()
    if not exe.exists() or model is None:
        return None

    port = _choose_port()
    args = [
        str(exe), "-m", str(model),
        "--port", str(port),
        "--host", "127.0.0.1",
        "--ctx-size", str(_ext.context_size()),
        "--jinja",
    ]
    # --decision-seqs exists only in the parallel-decision fork; a stock
    # llama-server refuses to start on an unknown flag.
    if variant != EXTERNAL or _ext.external_is_fork():
        args[-1:-1] = ["--decision-seqs", str(DECISION_SEQS)]
    args.extend(_ext.server_extra_args())
    logger.info("llama_server_manager: starting %s variant on port %d", variant, port)
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    lifecycle.register_process(proc, {"name": "llama-server", "variant": variant, "port": port})
    # Must be drained or llama-server's own stdout write() blocks once the OS
    # pipe buffer fills (a few KB of startup logging is enough), which looks
    # exactly like a health-check timeout with no indication why.
    asyncio.create_task(_pump_output(proc, variant))

    handle = _ServerHandle(proc, port, variant)
    if await _wait_healthy(handle.base_url):
        return handle

    logger.warning("llama_server_manager: %s variant did not become healthy in time", variant)
    lifecycle.unregister_process(proc.pid)
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return None


async def _supervise(handle: _ServerHandle) -> None:
    """Background task: watch for a crash, restart with backoff, fall back
    down the variant chain if a variant keeps failing."""
    from sdk.gateway import lifecycle

    await handle.proc.wait()
    lifecycle.unregister_process(handle.proc.pid)

    global _server
    async with _server_lock:
        if _server is not handle:
            return  # already replaced by a newer server
        _server = None

    handle.restarts += 1
    chain = [EXTERNAL] if handle.variant == EXTERNAL else list(VARIANT_CHAIN)
    variant_idx = chain.index(handle.variant) if handle.variant in chain else 0

    if handle.restarts > _MAX_RESTARTS_PER_VARIANT:
        variant_idx += 1
        if variant_idx >= len(chain):
            logger.error("llama_server_manager: all variants exhausted; engine unavailable")
            return
        logger.warning(
            "llama_server_manager: %s crashed %d times; falling back to %s",
            handle.variant, handle.restarts, chain[variant_idx],
        )
    else:
        await asyncio.sleep(_RESTART_BASE_DELAY_S * (2 ** (handle.restarts - 1)))

    await _ensure_running(preferred=chain[variant_idx])


async def _ensure_running(preferred: str | None = None) -> _ServerHandle:
    """Start the server if it is not already running and healthy; return the handle."""
    global _server
    async with _server_lock:
        if _server is not None and _server.proc.returncode is None:
            return _server

        candidates = [preferred] if preferred else _preferred_variant_order()
        candidates = [v for v in candidates if v] or list(VARIANT_CHAIN)

        for variant in candidates:
            handle = await _spawn(variant)
            if handle is not None:
                _server = handle
                asyncio.create_task(_supervise(handle))
                return handle

        raise RuntimeError(
            "No llama.cpp engine variant could be started. Install one from Settings."
        )


async def shutdown() -> None:
    """Stop the managed server, if running. Goes through lifecycle's registry
    shutdown so this doesn't duplicate its SIGTERM->wait->SIGKILL handling."""
    from sdk.gateway import lifecycle

    global _server
    async with _server_lock:
        handle = _server
        _server = None
    if handle is not None:
        await lifecycle.shutdown_all()


async def stop_server() -> None:
    """Stop only this module's llama-server, leaving every other registered
    process alone (shutdown() goes through the whole registry). Clearing
    ``_server`` first is what stops _supervise() from restarting it."""
    from sdk.gateway import lifecycle

    global _server
    async with _server_lock:
        handle = _server
        _server = None
    if handle is None or handle.proc.returncode is not None:
        return
    lifecycle.unregister_process(handle.proc.pid)
    try:
        handle.proc.terminate()
        await asyncio.wait_for(handle.proc.wait(), timeout=10.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        try:
            handle.proc.kill()
        except ProcessLookupError:
            pass


def active_variant() -> str | None:
    """Which GPU/CPU variant is currently serving, or None if not running."""
    return _server.variant if _server is not None else None


# ---------------------------------------------------------------------------
# Normal mode -- ordinary chat completion, same signature embedded_engine.py
# exposed so every existing call site needed only its import changed.
# ---------------------------------------------------------------------------


async def generate_stream(
    prompt: str | list[dict[str, str]],
    max_tokens: int = 512,
    temperature: float = 0.7,
    lora_path: str | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
) -> AsyncIterator[str]:
    """Stream a completion from the local llama-server, one turn at a time."""
    if lora_path:
        # LoRA is NOT implemented: nothing trains an adapter (lora_trainer.py is
        # a stub) and this engine never loads one. Say so rather than pretend.
        logger.warning(
            "llama_server_manager: LoRA adapter %s requested, but LoRA is not "
            "implemented; the adapter is ignored.", lora_path,
        )

    handle = await _ensure_running()

    messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    payload: dict[str, Any] = {
        "model": "local",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    # Reasoning models (gpt-oss) spend completion tokens thinking before the
    # visible reply, so the budget grows with the effort; the reasoning itself
    # is never yielded (reasoning_content is simply not read below).
    profile = _ext.profile()
    effort = profile.reasoning_effort(has_tools=bool(tools)) if profile else None
    if effort:
        payload["chat_template_kwargs"] = {"reasoning_effort": effort}
        payload["max_tokens"] = max_tokens + profile.token_budget(effort)
    harmony_on = bool(profile and profile.main.harmony)
    buffering = False
    raw_chunks: list[str] = []
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

    # Accumulated native tool_calls deltas, keyed by their stream index --
    # OpenAI-shaped streaming splits a call across many chunks (name arrives
    # once, "arguments" arrives as incremental JSON-string fragments to be
    # concatenated, not re-sent whole each time).
    tool_calls_by_index: dict[int, dict[str, Any]] = {}

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", f"{handle.base_url}/v1/chat/completions", json=payload) as resp:
            if getattr(resp, "status_code", 200) >= 400:
                # llama-server says why in the body (context overflow, bad
                # template...). raise_for_status() alone threw that away.
                body = (await resp.aread()).decode(errors="replace")[:500]
                raise RuntimeError(f"llama-server HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = obj.get("choices", [{}])[0].get("delta", {})
                chunk = delta.get("content")
                if chunk:
                    if buffering or (harmony_on and _harmony.has_raw_markers(chunk)):
                        # Raw harmony markers reached the text stream: hold it
                        # back and sort it out once the turn is complete.
                        buffering = True
                        raw_chunks.append(chunk)
                    else:
                        yield chunk
                for tc in delta.get("tool_calls") or []:
                    slot = tool_calls_by_index.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    slot["arguments"] += fn.get("arguments") or ""

    if buffering:
        visible, harmony_calls = _harmony.split_harmony("".join(raw_chunks))
        if visible:
            yield visible
        for call in harmony_calls:
            tool_calls_by_index[len(tool_calls_by_index)] = call

    if tool_calls_by_index:
        # No content was streamed for a tool call (confirmed against the real
        # server: a tool-calling turn's deltas carry only tool_calls, never
        # content) -- convert to the same <call:name attr="v" /> tag the rest
        # of the harness already detects, reusing the existing converter
        # rather than inventing a second tool-call text format.
        from python.harness.server import _tool_calls_to_call_tags

        # _tool_calls_to_call_tags already parses a string "arguments" itself
        # (it's built to accept either shape) -- pass the accumulated raw
        # JSON string straight through rather than parsing it twice.
        calls = [
            {"function": call}
            for call in _harmony.normalize_tool_calls(
                [{"function": slot} for slot in tool_calls_by_index.values()]
            )
        ]
        tag = _tool_calls_to_call_tags(calls)
        if tag:
            yield tag


# ---------------------------------------------------------------------------
# Jev mode -- structured, single-pass decisions via /v1/decision.
#
# Low-level capability only. No Tala call site uses this yet; rewiring
# specific heuristics (chat-mode classification, tool selection, the
# casual-chat gate, etc.) to call this instead of their current logic is a
# separate follow-up task. This function exists so that work is purely
# call-site rewiring, not new plumbing.
# ---------------------------------------------------------------------------


async def decide(
    schema: dict[str, Any],
    contexts: list[str],
    instructions: str = "",
) -> dict[str, Any]:
    """
    One schema-constrained decision per context, scored in a single forward
    pass per tools/parallel-decision/README.md. Each field in `schema` needs
    its own "description" key -- the server rejects fields without one.
    """
    if not contexts:
        raise ValueError("decide() needs at least one context")

    handle = await _ensure_running()
    if handle.variant == EXTERNAL and not _ext.external_is_fork():
        raise RuntimeError(
            "Structured decisions need the parallel-decision llama-server fork; the "
            "external llama-server is a stock build (set TALA_LLAMA_SERVER_FORK=1 if not)."
        )
    payload = {
        "model": "local",
        "instructions": instructions,
        "schema": schema,
        "contexts": contexts,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{handle.base_url}/v1/decision", json=payload)
        resp.raise_for_status()
        return resp.json()
