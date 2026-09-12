"""
engine_manager.py — Tala Cognitive Engine Room: Embedded Engine Manager
========================================================================
Manages the installation and lifecycle of local inference engines 
(Transformers, vLLM, llama.cpp).
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Track active installations
_install_status: Dict[str, str] = {}  # engine_id -> status ("installing", "ready", "error")
_install_progress: Dict[str, float] = {}
_install_logs: Dict[str, List[str]] = {}
_install_locks: Dict[str, threading.Lock] = {
    "transformers": threading.Lock(),
    "vllm": threading.Lock(),
    "llamacpp": threading.Lock(),
}

# `platforms` lists the sys.platform values an engine can actually be installed
# on. It is not decoration: vLLM publishes no Windows wheels, so offering it in
# the engine list on Windows sends the user into a pip build that fails on a
# missing CUDA toolchain, with no indication beforehand that it was never going
# to work. Tala is Windows-first and USB-portable, so "importable" and
# "installable here" are different questions and both have to be answered.
#
# An empty/absent `platforms` means "everywhere".
ENGINES = {
    "transformers": {
        "id": "transformers",
        "name": "HuggingFace Transformers (CPU/GPU)",
        "description": "Standard Python inference. Very compatible, moderate speed.",
        "packages": ["transformers", "accelerate", "sentencepiece"],
        "platforms": ["win32", "linux", "darwin"],
    },
    "vllm": {
        "id": "vllm",
        "name": "vLLM (GPU Only)",
        "description": "High-throughput inference for NVIDIA GPUs. Linux or WSL only — "
                       "there are no Windows wheels.",
        "packages": ["vllm"],
        "platforms": ["linux"],
    },
    "llamacpp": {
        "id": "llamacpp",
        "name": "llama.cpp (CPU/GPU)",
        "description": "Highly optimized C++ inference for GGUF models. Supports GBNF "
                       "grammar-constrained sampling.",
        "packages": ["llama-cpp-python"],
        "platforms": ["win32", "linux", "darwin"],
    }
}


def is_engine_supported(engine_id: str) -> bool:
    """Whether this engine can be installed on the current platform at all."""
    platforms = ENGINES.get(engine_id, {}).get("platforms")
    if not platforms:
        return True
    return sys.platform in platforms


def unsupported_reason(engine_id: str) -> Optional[str]:
    """Why the engine is unavailable here, or None if it is available."""
    if is_engine_supported(engine_id):
        return None
    supported = ", ".join(ENGINES.get(engine_id, {}).get("platforms", []))
    return f"Not available on {sys.platform} - requires {supported or 'another platform'}."


def is_engine_installed(engine_id: str) -> bool:
    """Checks if the packages for the given engine are importable without fully loading them."""
    import importlib.util

    if engine_id == "transformers":
        return importlib.util.find_spec("transformers") is not None
    elif engine_id == "vllm":
        return importlib.util.find_spec("vllm") is not None
    elif engine_id == "llamacpp":
        return importlib.util.find_spec("llama_cpp") is not None
    return False


def _install_engine_task(engine_id: str, packages: List[str]) -> None:
    """Runs the pip install command for the engine in a background thread."""
    with _install_locks[engine_id]:
        _install_status[engine_id] = "installing"
        _install_progress[engine_id] = 0.0
        _install_logs[engine_id] = []
        
        try:
            # We use the executable that is running Tala (should be the venv python)
            python_exe = sys.executable

            # Prefer uv, but don't hard-depend on it: a packaged app launched
            # from Finder/Explorer gets a minimal PATH where uv is usually
            # absent. Fall back to this interpreter's own pip (same pattern as
            # scripts/first_run.py).
            if shutil.which("uv"):
                cmd = ["uv", "pip", "install", "--python", python_exe] + packages
            else:
                cmd = [python_exe, "-m", "pip", "install"] + packages
            
            logger.info("Installing engine %s: %s", engine_id, " ".join(cmd))
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            lines_processed = 0
            # Rough estimate of total lines for a pip install to drive the progress bar
            estimated_lines = 100 
            
            if process.stdout:
                for line in iter(process.stdout.readline, ''):
                    line = line.strip()
                    if not line:
                        continue
                    
                    _install_logs[engine_id].append(line)
                    # Keep only last 100 lines of log to prevent memory leaks
                    if len(_install_logs[engine_id]) > 100:
                        _install_logs[engine_id].pop(0)
                        
                    lines_processed += 1
                    # advance progress up to 95% based on lines
                    pct = min(95.0, (lines_processed / estimated_lines) * 100)
                    _install_progress[engine_id] = pct
            
            process.wait()
            
            if process.returncode == 0:
                _install_status[engine_id] = "ready"
                _install_progress[engine_id] = 100.0
                _install_logs[engine_id].append("Installation successful.")
                logger.info("Engine %s installed successfully.", engine_id)
            else:
                _install_status[engine_id] = "error"
                _install_logs[engine_id].append(f"Installation failed with code {process.returncode}")
                logger.error("Engine %s installation failed.", engine_id)
                
        except Exception as exc:
            _install_status[engine_id] = "error"
            _install_logs[engine_id].append(str(exc))
            logger.exception("Error installing engine %s", engine_id)


def start_install(engine_id: str) -> None:
    if engine_id not in ENGINES:
        raise ValueError(f"Unknown engine: {engine_id}")
        
    # Refuse here rather than letting pip deliver the news several minutes into a
    # doomed source build. The UI should already be showing this engine as
    # unsupported; this is the backstop for a direct RPC call.
    if not is_engine_supported(engine_id):
        raise RuntimeError(f"Cannot install '{engine_id}': {unsupported_reason(engine_id)}")

    status = _install_status.get(engine_id)
    if status == "installing":
        return

    thread = threading.Thread(
        target=_install_engine_task,
        args=(engine_id, ENGINES[engine_id]["packages"]),
        daemon=True
    )
    thread.start()


def get_status(engine_id: str) -> Dict[str, Any]:
    if engine_id not in ENGINES:
        raise ValueError(f"Unknown engine: {engine_id}")
        
    installed = is_engine_installed(engine_id)
    supported = is_engine_supported(engine_id)
    # An engine that cannot be installed here reports "unsupported" rather than
    # "not_installed", so the UI can grey it out instead of offering a button
    # whose only outcome is a pip failure.
    if not supported and not installed:
        status = "unsupported"
    else:
        status = _install_status.get(engine_id, "ready" if installed else "not_installed")

    return {
        "id": engine_id,
        "name": ENGINES[engine_id]["name"],
        "description": ENGINES[engine_id]["description"],
        "is_installed": installed,
        "is_supported": supported,
        "unsupported_reason": unsupported_reason(engine_id),
        "status": status,
        "progress": _install_progress.get(engine_id, 100.0 if installed else 0.0),
        "logs": _install_logs.get(engine_id, []),
    }


def get_all_engines() -> List[Dict[str, Any]]:
    return [get_status(eid) for eid in ENGINES.keys()]

