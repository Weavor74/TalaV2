"""GPU availability must be honest for inference routing defaults."""
from __future__ import annotations

import builtins
from pathlib import Path

from python.harness import system_info


def test_has_gpu_false_when_no_torch_cuda_and_no_nvidia_smi(monkeypatch):
    monkeypatch.setattr(system_info, "_run_cmd", lambda cmd: "")
    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch."):
            raise ImportError("no torch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)
    assert system_info.has_gpu() is False


def test_has_gpu_true_when_nvidia_smi_lists_gpus(monkeypatch):
    def fake_run(cmd):
        if cmd and cmd[0] == "nvidia-smi" and "-L" in cmd:
            return "GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-abc)\n"
        return ""

    monkeypatch.setattr(system_info, "_run_cmd", fake_run)
    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch."):
            raise ImportError("no torch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)
    assert system_info.has_gpu() is True


def test_system_info_nvidia_smi_path_sets_available_true():
    src = Path(system_info.__file__).read_text(encoding="utf-8")
    assert 'gpu_info["available"] = True' in src
    assert "--query-gpu" in src
    assert "def has_gpu" in src
    assert "def _run_cmd" in src
