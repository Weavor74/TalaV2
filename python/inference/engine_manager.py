"""
engine_manager.py — Tala Cognitive Engine Room: Embedded Engine Manager
========================================================================
Tracks installation status for Tala's one embedded inference engine: a
llama.cpp server binary (github.com/thecodacus/llama.cpp @ parallel-decision),
managed by ``python.inference.llama_server_manager``.

Previously this tracked three pip-installable engines (Transformers, vLLM,
llama-cpp-python) via package installation. Only Transformers ever actually
ran a completion; llama.cpp was a documented stub and vLLM's execution path
was never wired into dispatch at all. All three are gone in favour of one
engine that actually works, shipped as a precompiled binary rather than a
pip package -- see llama_server_manager.py's module docstring for why
("downloaded on demand, hash-verified, versioned independently of the app").
"""
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

ENGINE_ID = "llamacpp"


def is_engine_installed(engine_id: str = ENGINE_ID) -> bool:
    """Whether a usable llama-server binary is present on disk."""
    from python.inference.llama_server_manager import available_variants

    return bool(available_variants())


def start_install(engine_id: str = ENGINE_ID) -> None:
    """Trigger the background binary+model download."""
    if engine_id != ENGINE_ID:
        raise ValueError(f"Unknown engine: {engine_id}")

    from python.inference.llama_server_manager import start_download_async

    start_download_async()


def get_status(engine_id: str = ENGINE_ID) -> Dict[str, Any]:
    if engine_id != ENGINE_ID:
        raise ValueError(f"Unknown engine: {engine_id}")

    from python.inference.llama_server_manager import (
        BINARY_VERSION, active_variant, available_variants, get_status as _dl_status,
    )

    # Translated to the vocabulary the engine-picker UI already polls for
    # ("installing" is what renderer.js's startEnginePolling() checks to
    # decide whether to keep refreshing) rather than changing the UI to match
    # llama_server_manager's own status names.
    _STATUS_MAP = {
        "not_downloaded": "not_installed",
        "downloading": "installing",
        "ready": "ready",
        "error": "error",
    }
    raw_status, progress, error = _dl_status()
    status = _STATUS_MAP.get(raw_status, raw_status)
    installed = bool(available_variants())
    if installed and status == "not_installed":
        status = "ready"
        progress = 100.0

    return {
        "id": ENGINE_ID,
        "name": "llama.cpp",
        "description": (
            "Highly optimized C++ inference for GGUF models, with structured "
            "single-pass decisions (Jev mode). Runs on NVIDIA (CUDA), AMD/Intel "
            "(Vulkan), or CPU."
        ),
        "is_installed": installed,
        "is_supported": True,
        "unsupported_reason": None,
        "status": status,
        "progress": progress,
        "error": error,
        # Independent of Tala's own app version -- the binary updates on its
        # own schedule. See llama_server_manager.BINARY_VERSION.
        "binary_version": BINARY_VERSION,
        "available_variants": available_variants(),
        "active_variant": active_variant(),
    }


def get_all_engines() -> List[Dict[str, Any]]:
    return [get_status(ENGINE_ID)]
