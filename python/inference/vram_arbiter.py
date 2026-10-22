"""
vram_arbiter.py -- one big model on the GPU at a time.

Kagura has 6 GB of VRAM. gpt-oss-20b (~5.5 GB with the MoE layers on CPU),
Llama 3.1 8B (~5 GB) and gemma3:4b (~3.5 GB) cannot be resident together, so
before a model is used everything else is moved off the GPU:

* using the main llama-server  -> every loaded Ollama model is unloaded
  (``keep_alive: 0``)
* using an Ollama model        -> the llama-server is stopped, and any *other*
  loaded Ollama model is unloaded

``plan()`` is a pure function from the current state to the actions needed, so
the policy is testable without a GPU. ``VramArbiter`` runs a plan against
injected callbacks; ``_default_arbiter()`` wires the real Ollama and
llama-server ones. A failed unload is logged and never blocks the request: the
worst case is the model spills to the CPU, which is slow but correct.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from python.inference.profiles import Profile

logger = logging.getLogger(__name__)

SERVER = "llama-server"
OLLAMA = "ollama"


@dataclass(frozen=True)
class Action:
    kind: str  # "unload" | "stop"
    target: str  # Ollama model name, or "llama-server"


def plan(
    *,
    using: str,
    model: str = "",
    loaded_ollama: list[str],
    server_running: bool,
    exclusive: bool = True,
) -> list[Action]:
    """Actions to take before ``using`` ("llama-server" or "ollama") runs.

    ``model`` is the Ollama model about to be used; it is never unloaded.
    """
    if not exclusive:
        return []
    if using == SERVER:
        return [Action("unload", m) for m in loaded_ollama]
    if using == OLLAMA:
        actions = [Action("unload", m) for m in loaded_ollama if m != model]
        if server_running:
            actions.append(Action("stop", SERVER))
        return actions
    raise ValueError(f"unknown engine {using!r}")


class VramArbiter:
    def __init__(
        self,
        *,
        exclusive: bool,
        ollama_loaded: Callable[[], Awaitable[list[str]]],
        ollama_unload: Callable[[str], Awaitable[None]],
        server_running: Callable[[], bool],
        server_stop: Callable[[], Awaitable[None]],
    ) -> None:
        self.exclusive = exclusive
        self._ollama_loaded = ollama_loaded
        self._ollama_unload = ollama_unload
        self._server_running = server_running
        self._server_stop = server_stop
        self._lock: asyncio.Lock | None = None

    async def prepare(self, using: str, model: str = "") -> list[Action]:
        """Make room for ``using``; returns the actions that were attempted."""
        if not self.exclusive:
            return []
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            try:
                loaded = await self._ollama_loaded()
            except Exception as exc:
                logger.debug("vram: could not list loaded Ollama models: %s", exc)
                loaded = []
            actions = plan(
                using=using, model=model, loaded_ollama=loaded,
                server_running=self._server_running(), exclusive=self.exclusive,
            )
            for action in actions:
                try:
                    if action.kind == "unload":
                        await self._ollama_unload(action.target)
                    else:
                        await self._server_stop()
                    logger.info("vram: %s %s (making room for %s)", action.kind, action.target, using)
                except Exception as exc:
                    logger.warning("vram: %s %s failed: %s", action.kind, action.target, exc)
            return actions


def _default_arbiter(profile: Profile) -> VramArbiter:
    from python.inference import llama_server_manager as lsm

    base = profile.ollama_url

    async def loaded() -> list[str]:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base}/api/ps")
            resp.raise_for_status()
            return [str(m.get("name") or m.get("model")) for m in resp.json().get("models", [])]

    async def unload(model: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{base}/api/generate", json={"model": model, "keep_alive": 0})
            resp.raise_for_status()

    return VramArbiter(
        exclusive=profile.vram_exclusive,
        ollama_loaded=loaded,
        ollama_unload=unload,
        server_running=lambda: lsm.active_variant() is not None,
        server_stop=lsm.stop_server,
    )


_arbiters: dict[str, VramArbiter] = {}


async def prepare_for(provider: dict[str, Any], profile: Profile | None) -> list[Action]:
    """Server-loop hook: free the GPU for ``provider`` before it is tried."""
    if profile is None or not profile.vram_exclusive:
        return []
    ptype = provider.get("type")
    if ptype == "embedded":
        using, model = SERVER, ""
    elif ptype == "local" and (provider.get("config") or {}).get("engine", "ollama") == "ollama":
        using, model = OLLAMA, str((provider.get("config") or {}).get("model", ""))
    else:
        return []
    arbiter = _arbiters.setdefault(profile.name, _default_arbiter(profile))
    return await arbiter.prepare(using, model)
