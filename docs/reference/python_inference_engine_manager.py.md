# File: `engine_manager.py`

## Module Description

engine_manager.py — Tala Cognitive Engine Room: Embedded Engine Manager
========================================================================
Manages the installation and lifecycle of local inference engines 
(Transformers, vLLM, llama.cpp).

## Functions

### `def is_engine_installed(engine_id)`
Checks if the packages for the given engine are importable.

### `def _install_engine_task(engine_id, packages)`
Runs the pip install command for the engine in a background thread.

### `def start_install(engine_id)`
*No docstring provided.*

### `def get_status(engine_id)`
*No docstring provided.*

### `def get_all_engines()`
*No docstring provided.*

## Source Code

```python
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

ENGINES = {
    "transformers": {
        "id": "transformers",
        "name": "HuggingFace Transformers (CPU/GPU)",
        "description": "Standard Python inference. Very compatible, moderate speed.",
        "packages": ["transformers", "accelerate", "sentencepiece"],
    },
    "vllm": {
        "id": "vllm",
        "name": "vLLM (GPU Only)",
        "description": "High-throughput inference for NVIDIA GPUs.",
        "packages": ["vllm"],
    },
    "llamacpp": {
        "id": "llamacpp",
        "name": "llama.cpp (CPU/GPU)",
        "description": "Highly optimized C++ inference for GGUF models.",
        "packages": ["llama-cpp-python"],
    }
}

def is_engine_installed(engine_id: str) -> bool:
    """Checks if the packages for the given engine are importable."""
    if engine_id == "transformers":
        try:
            import transformers
            return True
        except ImportError:
            return False
    elif engine_id == "vllm":
        try:
            import vllm
            return True
        except ImportError:
            return False
    elif engine_id == "llamacpp":
        try:
            import llama_cpp
            return True
        except ImportError:
            return False
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
            
            # Simple simulation of progress for pip (since pip doesn't output clean JSON percentages)
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
    status = _install_status.get(engine_id, "ready" if installed else "not_installed")
    
    return {
        "id": engine_id,
        "name": ENGINES[engine_id]["name"],
        "description": ENGINES[engine_id]["description"],
        "is_installed": installed,
        "status": status,
        "progress": _install_progress.get(engine_id, 100.0 if installed else 0.0),
        "logs": _install_logs.get(engine_id, []),
    }


def get_all_engines() -> List[Dict[str, Any]]:
    return [get_status(eid) for eid in ENGINES.keys()]


```
