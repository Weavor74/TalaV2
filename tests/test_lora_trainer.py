"""python/inference/lora_trainer.py: what the engine panel is told about LoRA must be true, and the retired per-node queue must stay gone."""
from __future__ import annotations

import pytest

from python.inference import lora_trainer as lt


@pytest.fixture(autouse=True)
def _world(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()


def test_the_per_node_queue_is_gone():
    for name in ("queue_lora_training", "_training_loop", "_perform_training_sync", "LoraTrainingUnavailable"):
        assert not hasattr(lt, name), f"{name} is the retired per-node JIT queue"


def test_until_it_is_verified_end_to_end_the_panel_says_so(monkeypatch):
    monkeypatch.setattr(lt, "IMPLEMENTED", False)

    status = lt.engine_status()

    assert status["implemented"] is False and "not yet been verified" in status["note"]


def test_once_it_works_the_panel_reports_what_is_true(monkeypatch):
    monkeypatch.setattr(lt, "IMPLEMENTED", True)

    off = lt.engine_status()
    assert off["implemented"] is True and off["auto_train"] is False and "scheduled training is off" in off["note"]
    assert "no adapter served yet" in off["note"] and "no run yet" in off["note"]

    from sdk.agent import node_learning as nl

    nl.write_config({"lora_auto_train": True})
    assert "weekly at night" in lt.engine_status()["note"]


def test_the_panel_still_renders_when_the_pipeline_cannot_be_read(monkeypatch):
    from sdk.agent import lora_pipeline

    def boom():
        raise RuntimeError("broken")

    monkeypatch.setattr(lora_pipeline, "status", boom)

    status = lt.engine_status()

    assert status["implemented"] is False and "broken" in status["note"]


def test_the_engine_status_api_carries_it():
    import inspect

    from python.inference import engine_manager

    assert "engine_status" in inspect.getsource(engine_manager.get_status)
