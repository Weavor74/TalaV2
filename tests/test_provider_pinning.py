"""
Pinning a provider for a measurement.

TALA_PIN_PROVIDER exists so a benchmark can measure one engine without writing
`is_forced` into the user's stored configuration -- a benchmark that mutates
what it measures leaves the application pinned if the run dies partway.

The bug these guard: `embedded-builtin` is not a row the handler loads with the
others. It is appended in the branch that handles "no provider selected", so a
pinned request was resolved against a list it was not in and raised "matches no
configured provider" on every generation. Ollama was unaffected, being a real
row, so the failure stayed invisible for a day -- until a 96-minute benchmark
of the embedded engine returned 0/18 having made 231 generation attempts and
completed none of them.

Asserted structurally because the resolution is inline in a very large handler
and the alternative is a live model call. The property that matters is an
ordering one: the list must be complete before anything resolves against it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

SERVER = Path("python/harness/server.py").read_text(encoding="utf-8")

#: Both generation paths resolve a pin, and both had the same gap. The agent
#: path goes through handle_inference_generate; execute_llm_generation is the
#: research path. Instrumenting only one of them is a mistake already made
#: twice in this file's history.
_PIN_RESOLUTIONS = SERVER.count('os.environ.get("TALA_PIN_PROVIDER", "")')
_EMBEDDED_INSERTS = SERVER.count('"id": "embedded-builtin",')


def test_both_generation_paths_resolve_a_pin():
    assert _PIN_RESOLUTIONS == 2, (
        f"expected the chat and research paths to resolve TALA_PIN_PROVIDER, "
        f"found {_PIN_RESOLUTIONS} resolution(s)"
    )


def test_the_embedded_engine_is_in_the_list_before_any_pin_is_resolved():
    """
    The ordering that broke. If the fallback insertion moves back below the
    pin resolution, pinning to the embedded engine raises again and every
    generation on that engine fails.
    """
    for index in range(_PIN_RESOLUTIONS):
        pin_at = _nth_index(SERVER, 'os.environ.get("TALA_PIN_PROVIDER", "")', index)
        preceding = SERVER[:pin_at]
        assert '"id": "embedded-builtin",' in preceding, (
            f"pin resolution #{index + 1} happens before the embedded engine is "
            f"added to the provider list; pinning to it will raise"
        )


def test_a_pin_that_matches_nothing_still_fails_loudly():
    """
    The guard must not be softened into silence. An unknown pin is a
    configuration error and has to say so -- falling back quietly would
    reintroduce exactly the mis-attribution the pin exists to prevent.
    """
    assert "matches no configured provider" in SERVER


def test_strict_pinning_removes_the_fallback_chain():
    """
    Without this a run pinned to one engine that errors completes on another
    and is scored as the first.
    """
    assert 'os.environ.get("TALA_PIN_STRICT") == "1"' in SERVER
    assert "no fallback" in SERVER


def _nth_index(haystack: str, needle: str, n: int) -> int:
    start = -1
    for _ in range(n + 1):
        start = haystack.index(needle, start + 1)
    return start


@pytest.mark.parametrize("marker", [
    "inference.served",
    "_record_serving_provider",
])
def test_what_actually_served_is_still_recorded(marker):
    """
    The readback that caught this run, and three before it. Without it a 0/18
    from a provider that never ran would have been filed as the embedded
    engine's capability baseline.
    """
    assert marker in SERVER
