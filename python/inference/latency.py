"""
latency.py -- how fast the last reply was, for the status bar.

Every provider already knows: llama-server sends ``timings`` with its reply (and the streamed path measures the wall-clock time
to the first token), and Ollama's native API returns ``eval_count`` / ``eval_duration`` plus the prompt-eval and load durations.
:func:`from_llama_timings` and :func:`_from_ollama` turn either into one small ``timing`` dict; :func:`record` / :func:`take`
carry it from the provider layer to the reply payload in a context variable, so the generate handler's many provider branches
need no plumbing of their own (each request runs in its own task, so requests never see each other's numbers).

A turn no provider reported on has no timing: nothing here guesses.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_TIMING: ContextVar[dict[str, Any] | None] = ContextVar("tala_turn_timing", default=None)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def record(*, source: str, model: str | None = None, ttft_ms: float | None = None, tokens_per_second: float | None = None,
           generated_tokens: int | None = None, prompt_tokens: int | None = None, prompt_per_second: float | None = None) -> dict[str, Any]:
    """Remember this turn's timing (the last generation of a turn wins). Values are rounded for display."""
    timing = {
        "source": source, "model": model,
        "ttft_ms": None if ttft_ms is None else round(ttft_ms),
        "tokens_per_second": None if tokens_per_second is None else round(tokens_per_second, 1),
        "generated_tokens": generated_tokens, "prompt_tokens": prompt_tokens,
        "prompt_per_second": None if prompt_per_second is None else round(prompt_per_second, 1),
    }
    _TIMING.set(timing)
    return timing


def take() -> dict[str, Any] | None:
    """The timing recorded in this request, once; None if no provider reported one."""
    timing = _TIMING.get()
    _TIMING.set(None)
    return timing


def from_llama_timings(timings: Any, ttft_s: float | None, model: str | None = None) -> dict[str, Any] | None:
    """llama-server's ``timings`` object. ``ttft_s`` is the measured wall-clock time to the first token, if the caller streamed."""
    if not isinstance(timings, dict):
        return None
    rate = _number(timings.get("predicted_per_second"))
    if not rate or rate <= 0:
        return None
    prompt_ms = _number(timings.get("prompt_ms"))
    if ttft_s is not None:
        ttft_ms = ttft_s * 1000
    elif prompt_ms is not None:
        ttft_ms = prompt_ms + 1000 / rate  # prompt processing, then the first token
    else:
        ttft_ms = None
    return {"source": "llama-server", "model": model, "ttft_ms": ttft_ms, "tokens_per_second": rate,
            "generated_tokens": timings.get("predicted_n"), "prompt_tokens": timings.get("prompt_n"),
            "prompt_per_second": _number(timings.get("prompt_per_second"))}


def _from_ollama(response: Any) -> dict[str, Any] | None:
    """Ollama's native ``/api/chat`` reply, whose durations are in nanoseconds."""
    if not isinstance(response, dict):
        return None
    count, duration = _number(response.get("eval_count")), _number(response.get("eval_duration"))
    if not count or not duration or count <= 0 or duration <= 0:
        return None
    rate = count / (duration / 1e9)
    prompt_n, prompt_ns = _number(response.get("prompt_eval_count")), _number(response.get("prompt_eval_duration"))
    load_ns = _number(response.get("load_duration")) or 0.0
    ttft_ms = (load_ns + (prompt_ns or 0.0)) / 1e6 + 1000 / rate if prompt_ns is not None else None
    return {"source": "ollama", "model": response.get("model"), "ttft_ms": ttft_ms, "tokens_per_second": rate,
            "generated_tokens": int(count), "prompt_tokens": None if prompt_n is None else int(prompt_n),
            "prompt_per_second": prompt_n / (prompt_ns / 1e9) if prompt_n and prompt_ns and prompt_ns > 0 else None}


def note_ollama(response: Any) -> None:
    """Record an Ollama reply's timing if it carries one; otherwise leave the turn without."""
    parsed = _from_ollama(response)
    if parsed:
        record(**parsed)
