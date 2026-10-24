"""
python/harness/voice_preload.py -- the voice's heavy imports, done once and early on the main thread.

The voice warm-up used to load Kokoro on a worker thread, which lazily imported ``transformers``, which imports scikit-learn,
which loads scipy's BLAS DLL. Windows holds its loader lock while a DLL loads and starting any thread waits on that lock, so
the main thread (growing its thread pool), faster-whisper (starting tqdm's monitor thread) and the import itself waited on one
another and the whole harness froze for good. It is a race, so the first launch of that day had worked.

The cure is not to import in the middle of a session. ``voice_rpc`` calls :func:`preload` while the server is still importing its
handlers, before the event loop serves a request and before any worker thread exists. It costs a few seconds of start-up, and
only on an install that has Kokoro set up.
"""
from __future__ import annotations

import importlib
import logging
import time

from python.harness import voice_kokoro

log = logging.getLogger(__name__)


def _touch(module, *names: str) -> None:
    """transformers is a lazy module: reading an attribute is what actually imports the submodule behind it."""
    for name in names:
        getattr(module, name)


def preload() -> bool:
    """Import the libraries Kokoro would otherwise import mid-session. True if it did; never raises."""
    if not (voice_kokoro.module_present() and voice_kokoro.model_installed()):
        return False
    started = time.monotonic()
    try:
        importlib.import_module("scipy.linalg")  # the DLL whose load held the lock
        transformers = importlib.import_module("transformers")  # the route that reaches it
        _touch(transformers, "AlbertConfig", "AlbertModel")
    except Exception as exc:
        log.warning("voice: could not preload the speech libraries (%s); the voice will import them when first used", exc)
        return False
    log.info("voice: speech libraries preloaded in %.1fs", time.monotonic() - started)
    return True
