"""
persona_adapter.py -- the trained persona adapter: where it is kept, and the llama-server that serves it.

The adapter is a LoRA trained on Llama 3.2 3B (sdk/agent/lora_pipeline.py) and converted to a GGUF LoRA. It is served by a second
llama-server running the profile's ``persona`` model with ``--lora <adapter>``, on its own port. Kagura's 6 GB cannot hold it beside
the main model, so vram_arbiter makes them take turns: preparing the persona provider stops the main server, and preparing the main
server (or an Ollama model that needs the GPU) stops this one. Ollama is not involved: the Ollama here refuses adapters.

The registry (``<user data>/lora/adapters/registry.json``) lists every adapter with how it scored, which one is active, and the order
they were activated in, so a rollback is "activate the one before" and never a retrain. The newest three are kept on disk; the
active one is never removed.

``routing`` (``lora_routing`` in learning.json) says when a turn tries the persona: ``off`` (the default), ``fallback`` (after every
other provider) or ``in_character`` (first, for an in-character turn that uses no tools). A turn that may call tools always keeps
the main model: the persona is a 3B and a roleplay turn that probes the machine must not lose that. The provider can also be named
explicitly in the provider picker, which always wins. ``final_stage`` is different in kind: the persona never answers in place of the
main model; it says the main model's finished reply in her voice afterwards (python/inference/persona_voice.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROVIDER_ID = "persona-lora"
KEEP = 3
ROUTING_MODES = ("off", "fallback", "in_character", "final_stage")


# --- the registry ---------------------------------------------------------------------------------------------------------------


def _user_data() -> Path:
    from sdk.agent import node_learning as nl

    return nl.user_data_dir()


def adapters_dir() -> Path:
    return _user_data() / "lora" / "adapters"


def registry_path() -> Path:
    return adapters_dir() / "registry.json"


def _read_registry() -> dict[str, Any]:
    """``{active, history, adapters}``; a missing or unreadable file is an empty registry, never an error."""
    empty: dict[str, Any] = {"active": None, "history": [], "adapters": []}
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("adapters"), list):
        return empty
    return {"active": data.get("active"), "history": list(data.get("history") or []), "adapters": list(data["adapters"])}


def _write_registry(registry: dict[str, Any]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(registry, handle, indent=2)
    os.replace(tmp, path)


def add_adapter(entry: dict[str, Any]) -> None:
    """Record an adapter (``id``, ``created``, ``gguf`` and whatever else the run knows). It is not served until it is activated."""
    registry = _read_registry()
    registry["adapters"] = [a for a in registry["adapters"] if a.get("id") != entry["id"]] + [entry]
    registry["adapters"].sort(key=lambda a: str(a.get("created", "")))
    _write_registry(registry)


def _entry(registry: dict[str, Any], adapter_id: str | None) -> dict[str, Any] | None:
    return next((a for a in registry["adapters"] if a.get("id") == adapter_id), None) if adapter_id else None


def active_entry() -> dict[str, Any] | None:
    registry = _read_registry()
    return _entry(registry, registry["active"])


def activate(adapter_id: str) -> dict[str, Any]:
    """Make an adapter the one served. KeyError for an id nobody registered, FileNotFoundError when its GGUF is gone; either way
    nothing changes."""
    registry = _read_registry()
    entry = _entry(registry, adapter_id)
    if entry is None:
        raise KeyError(adapter_id)
    if not Path(str(entry.get("gguf", ""))).is_file():
        raise FileNotFoundError(f"the adapter file for {adapter_id} is gone: {entry.get('gguf')}")
    if registry["active"] != adapter_id:
        registry["history"] = [h for h in registry["history"] if h != adapter_id][-20:] + ([registry["active"]] if registry["active"] else [])
        registry["active"] = adapter_id
        _write_registry(registry)
    return entry


def deactivate() -> None:
    registry = _read_registry()
    if registry["active"] is not None:
        registry["history"] = [h for h in registry["history"] if h != registry["active"]][-20:] + [registry["active"]]
        registry["active"] = None
        _write_registry(registry)


def rollback() -> dict[str, Any] | None:
    """Go back to the adapter that was active before this one (skipping any whose file is gone). With none left, the persona is
    switched off and the plain base model is what remains: the answer is None."""
    registry = _read_registry()
    history = list(registry["history"])
    while history:
        candidate = _entry(registry, history.pop())
        if candidate is not None and Path(str(candidate.get("gguf", ""))).is_file():
            registry["active"], registry["history"] = candidate["id"], history
            _write_registry(registry)
            return candidate
    registry["active"], registry["history"] = None, []
    _write_registry(registry)
    return None


def prune(keep: int = KEEP) -> list[str]:
    """Keep the newest ``keep`` adapters (and the active one, whatever its age); delete the rest and their files."""
    registry = _read_registry()
    adapters = registry["adapters"]
    keepers = {registry["active"]} if registry["active"] else set()
    for entry in reversed(adapters):  # newest first, until ``keep`` are held in all (the active one counts as one of them)
        if len(keepers) >= max(1, keep):
            break
        keepers.add(entry["id"])
    removed = [a for a in adapters if a["id"] not in keepers]
    for entry in removed:
        try:
            Path(str(entry.get("gguf", ""))).unlink(missing_ok=True)
            if entry.get("peft_dir"):
                shutil.rmtree(entry["peft_dir"], ignore_errors=True)
        except OSError as exc:
            log.warning("persona_adapter: could not delete %s: %s", entry.get("gguf"), exc)
    if removed:
        gone = {a["id"] for a in removed}
        registry["adapters"] = [a for a in adapters if a["id"] not in gone]
        registry["history"] = [h for h in registry["history"] if h not in gone]
        _write_registry(registry)
    return [a["id"] for a in removed]


def routing_mode() -> str:
    from sdk.agent import node_learning as nl

    mode = str(nl.read_config().get("lora_routing", "off"))
    return mode if mode in ROUTING_MODES else "off"


# --- the server -----------------------------------------------------------------------------------------------------------------


@dataclass
class _Handle:
    proc: Any
    port: int
    adapters: list[Path] = field(default_factory=list)
    adapter_id: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


_handle: _Handle | None = None
_lock = asyncio.Lock()


def _profile() -> Any:
    from python.inference import profiles

    return profiles.active()


def server_argv(profile: Any, adapters: list[Path], *, port: int, apply: bool = True) -> list[str]:
    """llama-server's command line for the persona model. ``--lora`` carries the adapters; ``apply=False`` loads them unapplied so a
    caller (the eval) can switch which one is on over HTTP without restarting."""
    persona = profile.persona
    argv = [str(profile.main.server_bin), "-m", str(persona.model_path), "--port", str(port), "--host", "127.0.0.1",
            "--ctx-size", str(persona.ctx), "--jinja", *persona.args]
    if adapters:
        argv += ["--lora", ",".join(str(a) for a in adapters)]
        if not apply:
            argv.append("--lora-init-without-apply")
    return argv


def _alive(handle: _Handle | None) -> bool:
    return handle is not None and getattr(getattr(handle, "proc", None), "returncode", None) is None


async def _spawn(profile: Any, adapters: list[Path], *, port: int | None = None, apply: bool = True) -> _Handle | None:
    from python.inference import llama_server_manager as lsm
    from sdk.gateway import lifecycle

    persona = profile.persona
    if not profile.main.server_bin.exists() or not persona.model_path.exists():
        log.warning("persona_adapter: the persona server cannot start: %s or %s is missing", profile.main.server_bin, persona.model_path)
        return None
    port = port or persona.port
    argv = server_argv(profile, adapters, port=port, apply=apply)
    log.info("persona_adapter: starting the persona server: %s", " ".join(argv))
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lifecycle.register_process(proc, {"name": "llama-server-persona", "port": port})
    handle = _Handle(proc=proc, port=port, adapters=list(adapters))
    asyncio.create_task(lsm._pump_output(proc, "persona"))
    if await lsm._wait_healthy(handle.base_url, timeout_s=persona.startup_timeout_s, proc=proc):
        return handle
    log.warning("persona_adapter: the persona server did not become healthy")
    await _stop_handle(handle)
    return None


async def _stop_handle(handle: _Handle) -> None:
    from sdk.gateway import lifecycle

    proc = getattr(handle, "proc", None)
    if proc is None or proc.returncode is not None:
        return
    lifecycle.unregister_process(proc.pid)
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=10.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def ensure_running() -> _Handle:
    """The persona server, serving the active adapter: started if it is not up, restarted if the active adapter changed."""
    global _handle
    entry = active_entry()
    if entry is None:
        raise RuntimeError("no active persona adapter: train and approve one first")
    profile = _profile()
    if profile is None or profile.persona is None:
        raise RuntimeError("the active inference profile has no persona model")
    async with _lock:
        if _handle is not None and _alive(_handle) and _handle.adapter_id == entry["id"]:
            return _handle
        if _handle is not None:
            await _stop_handle(_handle)
            _handle = None
        spawned = await _spawn(profile, [Path(entry["gguf"])])
        if spawned is None:
            raise RuntimeError("the persona server could not be started")
        spawned.adapter_id = entry["id"]
        _handle = spawned
        return _handle


async def stop() -> None:
    global _handle
    async with _lock:
        handle, _handle = _handle, None
    if handle is not None:
        await _stop_handle(handle)


def is_running() -> bool:
    return _alive(_handle)


# --- what the rest of the harness sees --------------------------------------------------------------------------------------------


def provider() -> dict[str, Any] | None:
    """The persona as a provider dict (for the router and the picker), or None when no adapter is active or the profile has no persona."""
    entry, profile = active_entry(), _profile()
    if entry is None or profile is None or profile.persona is None:
        return None
    persona = profile.persona
    return {"id": PROVIDER_ID, "type": "local", "name": f"Tala persona ({entry['id']})", "priority": 5, "locked": False, "status": "unknown",
            "config": {"engine": "persona", "endpoint": f"http://127.0.0.1:{persona.port}/v1", "model": persona.label, "adapter": entry["id"]}}


def status() -> dict[str, Any]:
    """What is true right now, for the UI: the active adapter, every adapter with its scores, whether the server is up."""
    registry = _read_registry()
    return {"active": registry["active"], "adapters": len(registry["adapters"]), "serving": is_running(), "routing": routing_mode(),
            "entries": [{k: a.get(k) for k in ("id", "created", "dataset_version", "base", "scores", "decision", "examples")}
                        for a in registry["adapters"]]}

