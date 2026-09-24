"""GPU availability must be honest for inference routing defaults.

``has_gpu()`` itself was deleted (reachability audit 2026-09-23): the GPU chat
gate probes ``system.info`` and reads ``gpu.available`` from
``collect_system_info``, so what needs guarding is that fallback path.
"""
from __future__ import annotations

from pathlib import Path

from python.harness import system_info


def test_system_info_nvidia_smi_path_sets_available_true():
    """collect_system_info must keep the nvidia-smi fallback that flips
    gpu.available when torch cannot see CUDA — the renderer's GPU chat gate
    reads exactly that field."""
    src = Path(system_info.__file__).read_text(encoding="utf-8")
    assert 'gpu_info["available"] = True' in src
    assert "--query-gpu" in src
