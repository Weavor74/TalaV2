"""
system_info.py — host hardware, OS, and installed-software collection.

The body of the ``system.info`` RPC, moved out of server.py wholesale when the
handler pushed that file past its size ratchet. The handler there is now a
thin wrapper around :func:`collect_system_info`; the shape of the returned
dict is consumed directly by the frontend System panel and by
``sdk/agent/tools.py::get_system_info_tool``.
"""

import os
from pathlib import Path
from typing import Any

from python.harness import db

_ROOT_DIR = str(Path(__file__).resolve().parent.parent.parent)


async def collect_system_info() -> dict[str, Any]:
    import platform
    import subprocess
    import multiprocessing
    import importlib.metadata

    def _run(cmd: list[str]) -> str:
        """Run a subprocess, return stdout stripped, or empty string on failure."""
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _pkg_version(pkg: str) -> str:
        """Return installed package version via importlib.metadata."""
        try:
            return importlib.metadata.version(pkg)
        except Exception:
            return "not installed"

    # ── OS / platform ──────────────────────────────────────────────────────
    os_info = {
        "system":    platform.system(),
        "release":   platform.release(),
        "version":   platform.version(),
        "machine":   platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "node":      platform.node(),
    }

    # ── CPU ────────────────────────────────────────────────────────────────
    cpu_count_logical = multiprocessing.cpu_count()
    try:
        import psutil  # optional
        cpu_count_physical = psutil.cpu_count(logical=False) or cpu_count_logical
        cpu_freq = psutil.cpu_freq()
        cpu_freq_mhz = round(cpu_freq.current) if cpu_freq else 0
        cpu_percent = psutil.cpu_percent(interval=0.2)
        vm = psutil.virtual_memory()
        ram_total_gb = round(vm.total / (1024 ** 3), 2)
        ram_used_gb  = round(vm.used  / (1024 ** 3), 2)
        ram_percent  = vm.percent
        # Measure the drive Tala's data lives on. "/" on Windows resolves to
        # whichever drive the process was launched from, which for a portable
        # install (USB stick) is usually not the drive that matters.
        _data_root = os.environ.get("TALA_USER_DATA") or _ROOT_DIR
        disk = psutil.disk_usage(
            _data_root if os.path.isdir(_data_root) else os.path.abspath(os.sep)
        )
        disk_total_gb = round(disk.total / (1024 ** 3), 1)
        disk_used_gb  = round(disk.used  / (1024 ** 3), 1)
        disk_percent  = disk.percent
    except ImportError:
        cpu_count_physical = cpu_count_logical
        cpu_freq_mhz = 0
        cpu_percent = 0
        ram_total_gb = ram_used_gb = ram_percent = 0
        disk_total_gb = disk_used_gb = disk_percent = 0

    cpu_info = {
        "logical_cores":  cpu_count_logical,
        "physical_cores": cpu_count_physical,
        "freq_mhz":       cpu_freq_mhz,
        "usage_percent":  cpu_percent,
    }

    ram_info = {
        "total_gb":   ram_total_gb,
        "used_gb":    ram_used_gb,
        "percent":    ram_percent,
    }

    disk_info = {
        "total_gb":  disk_total_gb,
        "used_gb":   disk_used_gb,
        "percent":   disk_percent,
    }

    # ── Software versions ──────────────────────────────────────────────────
    python_version = platform.python_version()

    node_version = _run(["node", "--version"]).lstrip("v") or "not found"
    npm_version  = _run(["npm",  "--version"]) or "not found"

    # Electron version (read from local package.json if available)
    electron_version = "not found"
    try:
        import json as _json
        _pkg_path = Path(__file__).resolve().parent.parent.parent / "node_modules" / "electron" / "package.json"
        if _pkg_path.exists():
            _data = _json.loads(_pkg_path.read_text(encoding="utf-8"))
            electron_version = _data.get("version", "unknown")
    except Exception:
        pass

    # PostgreSQL client version
    pg_client = _run(["psql", "--version"]).replace("psql (PostgreSQL) ", "") or "not found"
    # Try pg server version via DB pool if available
    pg_server = "not connected"
    try:
        pool = db.get_pool()
        if pool:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT version()")
                if row:
                    raw = row["version"]
                    # e.g. 'PostgreSQL 16.2 on x86_64-pc-linux-gnu ...'
                    pg_server = raw.split(" ")[1] if raw else "unknown"
    except Exception:
        pass

    # Python package versions
    py_packages = {
        "asyncpg":          _pkg_version("asyncpg"),
        "websockets":       _pkg_version("websockets"),
        "tiktoken":         _pkg_version("tiktoken"),
        "pydantic":         _pkg_version("pydantic"),
        "httpx":            _pkg_version("httpx"),
        "lxml":             _pkg_version("lxml"),
        # pyswisseph, not ephem (UI-066). The panel raised a red "not
        # installed" for a library this project neither declares nor imports,
        # while ignoring the one the astrology engine actually needs -- whose
        # absence does not fail loudly but *biases* emotion labels, collapsing
        # roughly half of them onto one value.
        "pyswisseph":       _pkg_version("pyswisseph"),
        "cryptography":     _pkg_version("cryptography"),
        "python-dotenv":    _pkg_version("python-dotenv"),
    }

    # ── GPU ────────────────────────────────────────────────────────────────
    # This section was missing entirely before -- system.info reported CPU/RAM/
    # disk/software but nothing about GPU hardware or whether local inference
    # is actually using it, so the model's own self-description of "no GPU
    # mentioned" was technically honest but misleading about what's actually
    # available. torch.cuda is authoritative for whether Tala's embedded
    # engine can/does use a GPU; nvidia-smi is a fallback so a real GPU still
    # shows up here even in an environment where torch itself isn't installed.
    gpu_info: dict[str, Any] = {"available": False, "devices": []}
    try:
        import torch as _torch
        gpu_info["torch_version"] = _torch.__version__
        if _torch.cuda.is_available():
            gpu_info["available"] = True
            gpu_info["backend"] = "cuda"
            gpu_info["devices"] = [
                {
                    "index": i,
                    "name": _torch.cuda.get_device_name(i),
                    "total_vram_gb": round(_torch.cuda.get_device_properties(i).total_memory / (1024 ** 3), 2),
                    "compute_capability": ".".join(str(x) for x in _torch.cuda.get_device_capability(i)),
                }
                for i in range(_torch.cuda.device_count())
            ]
        else:
            gpu_info["backend"] = "cpu"
    except ImportError:
        gpu_info["backend"] = "torch not installed"
    except Exception as exc:
        gpu_info["error"] = str(exc)

    if not gpu_info["devices"]:
        smi_out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
        if smi_out:
            gpu_info["nvidia_smi_detected"] = smi_out.splitlines()

    # If Tala's embedded model is already loaded in this process, report the
    # actual device it landed on -- the single most direct answer to "is
    # local inference using the GPU right now", not just "could it".
    try:
        from python.inference import embedded_engine as _ee
        loaded_model = getattr(_ee, "_model", None)
        if loaded_model is not None:
            gpu_info["embedded_model_device"] = str(next(loaded_model.parameters()).device)
    except Exception:
        pass

    # ── Runtime service status ─────────────────────────────────────────────
    services = {
        "harness_ws":  "online",   # If we're responding, the harness is running
        "postgresql":  "online" if pg_server != "not connected" else "offline",
        "blockchain":  "unknown",  # Blockchain node is external; check via its port
    }
    # Quick TCP ping to blockchain node port 8332
    try:
        import asyncio as _asyncio
        _r, _w = await _asyncio.wait_for(
            _asyncio.open_connection("127.0.0.1", 8332), timeout=1.0
        )
        _w.close()
        services["blockchain"] = "online"
    except Exception:
        services["blockchain"] = "offline"

    return {
        "os":          os_info,
        "cpu":         cpu_info,
        "ram":         ram_info,
        "disk":        disk_info,
        "gpu":         gpu_info,
        "software": {
            "python":     python_version,
            "node":       node_version,
            "npm":        npm_version,
            "electron":   electron_version,
            "postgresql_client": pg_client,
            "postgresql_server": pg_server,
        },
        "python_packages": py_packages,
        "services":    services,
    }
