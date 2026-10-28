"""
Heavy imports for the voice, done once and early on the main thread.

2026-10-24, the real app, hung solid for minutes. The voice warm-up loaded Kokoro on a worker thread; that lazily imported
`transformers`, which imports scikit-learn, which loads scipy's BLAS DLL. Windows holds its loader lock while a DLL loads, and
starting any thread waits on that lock -- so the main thread (submitting to its thread pool), faster-whisper (starting
tqdm's monitor thread) and the import itself all waited on each other. It is a race: the first launch of the day had gone
through the same code and worked. The cure is to not import in the middle of a session: load those libraries serially,
before any request is being served.
"""
from __future__ import annotations

import importlib
import logging

import pytest

from python.harness import voice_kokoro, voice_preload


@pytest.fixture()
def recorder(monkeypatch):
    seen: list[str] = []
    real = importlib.import_module

    def fake_import(name, package=None):
        seen.append(name)
        return real("os")  # a harmless stand-in module; the attribute lookups below are what the caller does with it

    monkeypatch.setattr(voice_preload.importlib, "import_module", fake_import)
    return seen


def test_nothing_is_loaded_when_kokoro_is_not_installed(recorder, monkeypatch):
    monkeypatch.setattr(voice_kokoro, "module_present", lambda: True)
    monkeypatch.setattr(voice_kokoro, "model_installed", lambda: False)
    assert voice_preload.preload() is False
    assert recorder == []


def test_nothing_is_loaded_when_the_python_packages_are_missing(recorder, monkeypatch):
    monkeypatch.setattr(voice_kokoro, "module_present", lambda: False)
    monkeypatch.setattr(voice_kokoro, "model_installed", lambda: True)
    assert voice_preload.preload() is False
    assert recorder == []


def test_the_libraries_that_deadlocked_are_loaded_up_front(recorder, monkeypatch):
    monkeypatch.setattr(voice_kokoro, "module_present", lambda: True)
    monkeypatch.setattr(voice_kokoro, "model_installed", lambda: True)
    monkeypatch.setattr(voice_preload, "_touch", lambda module, *names: None)
    assert voice_preload.preload() is True
    # scipy's BLAS is the DLL whose load held the lock; transformers is what reaches it
    assert "scipy.linalg" in recorder and "transformers" in recorder


def test_a_failed_preload_does_not_stop_the_harness_starting(monkeypatch, caplog):
    monkeypatch.setattr(voice_kokoro, "module_present", lambda: True)
    monkeypatch.setattr(voice_kokoro, "model_installed", lambda: True)

    def broken(name, package=None):
        raise ImportError("no such library")

    monkeypatch.setattr(voice_preload.importlib, "import_module", broken)
    with caplog.at_level(logging.WARNING, logger=voice_preload.log.name):
        assert voice_preload.preload() is False
    assert "no such library" in caplog.text


def test_the_preload_runs_when_the_server_imports_the_voice_handlers():
    """voice_rpc is imported by server.py at start-up, on the main thread, before the event loop serves a request."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "python" / "harness" / "voice_rpc.py").read_text(encoding="utf-8")
    assert "voice_preload.preload()" in src


def test_the_speech_recogniser_does_not_start_a_progress_bar_monitor_thread():
    """tqdm starts a monitor thread the first time a bar is made; faster-whisper makes one per transcription, from a worker
    thread -- one of the three threads caught in the hang."""
    import tqdm

    from python.harness.voice_stt import SttEngine

    previous = tqdm.tqdm.monitor_interval
    try:
        tqdm.tqdm.monitor_interval = 10
        engine = SttEngine()
        engine._quiet_progress()
        assert tqdm.tqdm.monitor_interval == 0
    finally:
        tqdm.tqdm.monitor_interval = previous
