"""
sdk/agent/lora_pipeline.py -- the weekly, eval-gated train -> deploy loop for Tala's persona adapter.

One run: build a versioned dataset from what Steven approved (sdk/agent/lora_approvals.py, lora_dataset.py), train a QLoRA adapter on
Llama 3.2 3B on the GPU, score it on held-out replies, convert it to a GGUF LoRA, score base / current / candidate on recall, safety
and general sanity through one llama-server (sdk/agent/lora_eval.py), and deploy it only if it beats what is being served and breaks
nothing. Otherwise the old adapter stays, the candidate is deleted, and the run record says why. Deploying means registering the
adapter in python/inference/persona_adapter.py (which keeps the last three and can roll back) and activating it; the next turn that
uses the persona starts its llama-server with ``--lora``.

Why this shape, measured on Kagura (RTX 3050, 6 GB; 2026-10-25) rather than assumed:
  * gpt-oss-20b cannot be QLoRA-trained in 6 GB, and neither can Llama 3.1 8B (its 4-bit weights alone are ~5.4 GB, so it spills into
    shared memory and slows 3-8x). Llama 3.2 3B trains at ~240 tokens/s with 3.9-4.9 GB peak at 512-1024 tokens.
  * The Ollama here refuses ``ADAPTER`` and cannot import safetensors, so the adapter is served by llama-server, which takes
    ``--lora``. llama-server and the main model cannot share the GPU, so vram_arbiter makes them take turns.
  * Training needs the whole GPU: this stops the persona server, makes the arbiter unload Ollama's GPU models and stop the main
    server, and leaves the GPU free afterwards. The next turn that needs a model starts it again.

Nothing here downloads: the base weights come from the local Hugging Face cache, the converter from a local llama.cpp checkout.

Configuration lives in learning.json (the learning settings file), all optional:
    lora_auto_train     false   the scheduled run is off until this is true
    lora_interval_days  7       days between scheduled attempts
    lora_hour           2       local hour after which the scheduled run may start
    lora_idle_minutes   30      how long the system must have been idle first
    lora_min_approved   15      approved items needed before a run is worth it
    lora_epochs         3       training epochs
    lora_rank           16      LoRA rank (every linear layer)
    lora_max_seq        768     tokens per example (a 3B on 6 GB tops out near 1024)
    lora_keep           3       adapters kept for rollback
    lora_hf_home        ""      Hugging Face cache holding the base weights (else HF_HOME, else the profile's)
    lora_train_args     []      extra arguments for the trainer
    llama_cpp_src       ""      a llama.cpp checkout (same commit as the engine) holding convert_lora_to_gguf.py and gguf-py
    lora_routing        "off"   off | fallback | in_character: when a turn tries the persona (see persona_adapter.py)
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_ADAPTER_BYTES = 4096
MIN_GGUF_BYTES = 100_000  # an adapter, not a model
STALE_RUN_HOURS = 24.0
LOCAL_PRIORITY_BASE = 1000  # the server's own band for local providers
RECALL_SAMPLE = 20

DEFAULTS: dict[str, Any] = {
    "lora_auto_train": False, "lora_interval_days": 7, "lora_hour": 2, "lora_idle_minutes": 30.0, "lora_min_approved": 15, "lora_epochs": 3,
    "lora_rank": 16, "lora_max_seq": 768, "lora_keep": 3, "lora_hf_home": "", "lora_train_args": [], "llama_cpp_src": "",
}
#: Per-stage time allowed. A 3B on the GPU trains in minutes; the CPU fallback is hours.
TIMEOUT_S = {"train": 8 * 3600, "nll": 3600, "convert": 1800}

_RUNNING = False


# --- configuration and state ----------------------------------------------------------------------------------------------


def settings() -> dict[str, Any]:
    from sdk.agent import node_learning as nl

    return {**DEFAULTS, **{k: v for k, v in nl.read_config().items() if k in DEFAULTS}}


def _user_data() -> Path:
    from sdk.agent import node_learning as nl

    return nl.user_data_dir()


def _state_path() -> Path:
    return _user_data() / "learning" / "lora.json"


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(updates: dict[str, Any]) -> dict[str, Any]:
    state = {**_read_state(), **updates}
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    os.replace(tmp, path)
    return state


def _halted() -> bool:
    """True when the emergency halt is engaged or cannot be read: a kill switch must fail closed."""
    try:
        from sdk.agent.gates import RevocationHalt

        return bool(RevocationHalt.is_halted())
    except Exception as exc:
        log.error("lora_pipeline: could not read the halt state; refusing to run: %s", exc)
        return True


def _parse(raw: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _marker_active(now: datetime) -> bool:
    started = _parse(_read_state().get("running_since"))
    return started is not None and (now - started) < timedelta(hours=STALE_RUN_HOURS)


def _generations_dir() -> Path:
    return _user_data() / "lora-generations"


def status() -> dict[str, Any]:
    """What a person needs to know: is it on, what did it last do and how did it score, is one running, what is served."""
    from python.inference import persona_adapter

    state, config = _read_state(), settings()
    served = persona_adapter.status()
    return {
        "auto_train": bool(config["lora_auto_train"]), "running": _RUNNING or _marker_active(datetime.now()),
        "stage": state.get("stage") if (_RUNNING or _marker_active(datetime.now())) else None,
        "last_status": state.get("last_status"), "last_stage": state.get("last_stage"), "last_reason": state.get("last_reason"),
        "last_run_id": state.get("last_run_id"), "last_adapter": state.get("last_adapter"), "last_scores": state.get("last_scores"),
        "last_trained_at": state.get("last_trained_at"),
        "last_attempt_date": state.get("last_attempt_date"), "active_adapter": served["active"], "adapters": served["adapters"],
        "serving": served["serving"], "routing": served["routing"],
    }


# --- running things -------------------------------------------------------------------------------------------------------------


async def _exec(args: list[str], *, timeout_s: float, env: dict[str, str] | None = None, low_priority: bool = False,
                tail_lines: int = 60) -> tuple[int, str]:
    """Run a command; ``(return code, the last lines of its output)``. A timeout kills it and returns 124."""
    kwargs: dict[str, Any] = {}
    if low_priority:
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS: training must not take the machine from its user
        else:
            kwargs["preexec_fn"] = lambda: os.nice(10)
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env, **kwargs)
    tail: collections.deque[str] = collections.deque(maxlen=tail_lines)

    async def _pump() -> None:
        assert proc.stdout is not None
        async for line in proc.stdout:
            tail.append(line.decode(errors="replace").rstrip())

    try:
        await asyncio.wait_for(asyncio.gather(_pump(), proc.wait()), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout_s:.0f}s\n" + "\n".join(tail)
    return int(proc.returncode or 0), "\n".join(tail)


def _converter_dir(config: dict[str, Any]) -> Path | None:
    for candidate in (config.get("llama_cpp_src"), os.environ.get("TALA_LLAMA_CPP_SRC")):
        if candidate and (Path(candidate) / "convert_lora_to_gguf.py").is_file() and (Path(candidate) / "gguf-py").is_dir():
            return Path(candidate)
    return None


def _on_c_drive() -> str | None:
    for name in ("TEMP", "TMP", "HF_HOME"):
        value = os.environ.get(name, "")
        if value.upper().startswith("C:"):
            return f"{name}={value}"
    return None


def _profile() -> Any:
    from python.inference import profiles

    return profiles.active()


def _local_base(model_id: str, config: dict[str, Any], profile: Any) -> tuple[Path, str] | None:
    """The base model's weights in a local Hugging Face cache: ``(snapshot dir, HF_HOME that holds it)``. Never downloads."""
    homes = [config.get("lora_hf_home"), os.environ.get("HF_HOME"), str(profile.hf_cache_dir) if getattr(profile, "hf_cache_dir", None) else None]
    for home in dict.fromkeys(h for h in homes if h):
        try:
            from huggingface_hub import snapshot_download

            return Path(snapshot_download(model_id, cache_dir=str(Path(home) / "hub"), local_files_only=True)), str(home)
        except Exception:
            continue
    return None


def _failed(stage: str, reason: str) -> dict[str, Any]:
    return {"status": "failed", "stage": stage, "reason": reason.strip()[-1500:]}


def _skipped(stage: str, reason: str) -> dict[str, Any]:
    return {"status": "skipped", "stage": stage, "reason": reason}


async def _free_gpu(profile: Any) -> None:
    """Training needs the whole card: stop the persona server and have the arbiter unload Ollama's GPU models and stop the main server."""
    from python.inference import persona_adapter, vram_arbiter

    await persona_adapter.stop()
    arbiter = vram_arbiter._arbiters.setdefault(profile.name, vram_arbiter._default_arbiter(profile))
    await arbiter.prepare(vram_arbiter.PERSONA, start=False)


async def _generation_eval(profile: Any, adapters: list[Path], configs: dict[str, Path | None], eval_set: dict[str, Any],
                           recall_items: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """Recall, safety and general scores for each configuration, from one persona server holding every adapter loaded but unapplied."""
    import httpx

    from python.inference import persona_adapter
    from sdk.agent import lora_eval

    port = profile.persona.port + 1  # beside, never on top of, a persona server that is serving
    handle = await persona_adapter._spawn(profile, adapters, port=port, apply=False)
    if handle is None:
        raise RuntimeError("the evaluation server could not be started")
    try:
        async with httpx.AsyncClient(base_url=handle.base_url, timeout=180.0) as client:
            return await lora_eval.generation_scores(client, {k: (str(v) if v else None) for k, v in configs.items()}, eval_set, recall_items)
    finally:
        await persona_adapter._stop_handle(handle)


# --- the run --------------------------------------------------------------------------------------------------------------------


async def run_pipeline(*, reason: str = "manual", force: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """One full run. Never raises; the result says what happened and, if it stopped, at which stage and why. ``force`` skips the
    "enough approved" and "something new" checks, never the safety ones."""
    global _RUNNING
    started = now or datetime.now()
    if _RUNNING or _marker_active(started):
        return {"status": "busy", "stage": "start", "reason": "a training run is already in progress"}
    _RUNNING = True
    run_id = started.strftime("%Y%m%d-%H%M%S")
    _write_state({"running_since": started.isoformat(), "last_reason_started": reason, "stage": "start"})
    scratch = _generations_dir() / ".work" / run_id
    try:
        result = await _run(run_id, scratch, force=force, reason=reason, started=started)
    except Exception as exc:  # a bug in here must not leave the marker set or the scratch files behind
        log.exception("lora_pipeline: unexpected error")
        result = _failed("internal", f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            scratch.parent.rmdir()  # the shared .work folder, once nothing else is using it
        except OSError:
            pass
        _RUNNING = False
    result.setdefault("run_id", run_id)
    state: dict[str, Any] = {"running_since": None, "stage": None, "last_status": result["status"], "last_stage": result.get("stage"),
                             "last_reason": result.get("reason"), "last_run_id": run_id}
    if result["status"] in ("deployed", "rejected"):
        state.update(last_dataset_hash=result.get("dataset_hash"), last_scores=result.get("scores"), last_trained_at=started.astimezone().isoformat())
    if result["status"] == "deployed":
        state["last_adapter"] = result["adapter"]
    _write_state(state)
    _record_run(result, started)
    log.info("lora_pipeline: run %s -> %s (%s)", run_id, result["status"], result.get("reason") or result.get("adapter"))
    return result


def _recall_items(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Approved items she trained on, a stable sample of them: what she should be able to bring back."""
    import hashlib

    trained = [i for i in manifest["items"] if i["split"] == "train"]
    trained.sort(key=lambda i: hashlib.sha256(f"recall\0{i['id']}".encode()).hexdigest())
    return trained[:RECALL_SAMPLE]


async def _run(run_id: str, scratch: Path, *, force: bool, reason: str, started: datetime) -> dict[str, Any]:
    from python.inference import persona_adapter
    from sdk.agent import lora_dataset, lora_eval

    config = settings()
    if _halted():
        return _skipped("guard", "the emergency halt is engaged")
    if (c_drive := _on_c_drive()):
        return _failed("guard", f"refusing to run with {c_drive}: training scratch space must not be on C:")
    profile = _profile()
    if profile is None or profile.persona is None:
        return _failed("guard", "the active inference profile has no persona model to train an adapter for")
    converter = _converter_dir(config)
    if converter is None:
        return _failed("guard", "no llama.cpp source with convert_lora_to_gguf.py was found: set llama_cpp_src in learning.json (or "
                                "TALA_LLAMA_CPP_SRC) to a llama.cpp checkout, ideally the commit your engine binary was built from")
    base = _local_base(profile.persona.base_model, config, profile)
    if base is None:
        return _failed("guard", f"the base weights for {profile.persona.base_model} are not in a local Hugging Face cache (it never downloads): "
                                "set lora_hf_home in learning.json to the cache that holds them")
    base_dir, hf_home = base

    # 1. the dataset: only what was approved
    _write_state({"stage": "dataset"})
    built = await lora_dataset.build_dataset(now=started)
    if not built.get("success"):
        return _skipped("dataset", str(built.get("error")))
    approved = int(built["train"]) + int(built["heldout"])
    if not force:
        if approved < int(config["lora_min_approved"]):
            return _skipped("dataset", f"{approved} approved items; a run needs {config['lora_min_approved']}")
        if built["hash"] == _read_state().get("last_dataset_hash"):
            return _skipped("dataset", "nothing new has been approved since the last run")
    manifest = lora_dataset.load_manifest(built["version"])
    if manifest is None or not any(i["split"] == "heldout" for i in manifest["items"]):
        return _failed("dataset", "the dataset has no held-out items to score the adapter on; approve more")
    dataset_dir = Path(built["dir"])

    # 2. train (the whole GPU is needed)
    _write_state({"stage": "train"})
    await _free_gpu(profile)
    peft_dir = _generations_dir() / f"{built['version']}-{built['hash']}" / "adapter"  # keyed by dataset so an interrupted run resumes
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HOME": hf_home}
    train = [sys.executable, str(REPO_ROOT / "scripts" / "train_voice_adapter.py"), "--train", "--no-real-usage", "--corpus", str(dataset_dir / "train"),
             "--output", str(peft_dir), "--base", str(base_dir), "--quantize", "auto", "--target", "all", "--rank", str(config["lora_rank"]),
             "--epochs", str(config["lora_epochs"]), "--max-seq", str(config["lora_max_seq"]), "--resume", *map(str, config["lora_train_args"])]
    code, output = await _exec(train, timeout_s=TIMEOUT_S["train"], env=env, low_priority=True)
    weights = peft_dir / "adapter_model.safetensors"
    if code != 0:
        return _failed("train", f"the trainer exited with code {code}:\n{output}")
    if not weights.is_file() or weights.stat().st_size < MIN_ADAPTER_BYTES:
        return _failed("train", f"the trainer wrote no real adapter (a placeholder-sized or missing {weights.name})")
    try:
        report = json.loads((peft_dir / "train_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}

    # 3. persona: held-out loss for the base, the served adapter and the candidate
    _write_state({"stage": "persona"})
    current = persona_adapter.active_entry()
    current_peft = Path(str(current.get("peft_dir", ""))) if current else None
    have_current = bool(current and current_peft and (current_peft / "adapter_model.safetensors").is_file() and Path(str(current.get("gguf", ""))).is_file())
    scratch.mkdir(parents=True, exist_ok=True)
    nll_file = scratch / "nll.json"
    nll_cmd = [sys.executable, str(REPO_ROOT / "scripts" / "eval_adapter.py"), "--base", str(base_dir), "--heldout", str(dataset_dir / "heldout"),
               "--max-seq", str(config["lora_max_seq"]), "--out", str(nll_file), "--adapter", f"candidate={peft_dir}"]
    if have_current:
        nll_cmd += ["--adapter", f"current={current_peft}"]
    code, output = await _exec(nll_cmd, timeout_s=TIMEOUT_S["nll"], env=env, low_priority=True)
    try:
        nll = json.loads(nll_file.read_text(encoding="utf-8"))
        candidate_nll = nll["candidate"]["nll"]
        reference_nll = nll["current"]["nll"] if have_current else nll["base"]["nll"]
    except (OSError, ValueError, KeyError):
        return _failed("persona", f"the held-out loss could not be computed (code {code}):\n{output}")
    if candidate_nll is None or reference_nll is None:
        return _failed("persona", "no held-out example could be scored")

    # 4. convert to a GGUF LoRA
    _write_state({"stage": "convert"})
    gguf = scratch / f"{run_id}.gguf"
    convert_env = {**env, "PYTHONPATH": str(converter / "gguf-py") + os.pathsep + env.get("PYTHONPATH", "")}
    code, output = await _exec([sys.executable, str(converter / "convert_lora_to_gguf.py"), str(peft_dir), "--base", str(base_dir), "--outfile", str(gguf),
                                "--outtype", "f16"], timeout_s=TIMEOUT_S["convert"], env=convert_env, low_priority=True)
    if code != 0 or not gguf.is_file() or gguf.stat().st_size < MIN_GGUF_BYTES or gguf.read_bytes()[:4] != b"GGUF":
        return _failed("convert", f"converting the adapter to GGUF failed (code {code}):\n{output}")

    # 5. recall, safety and general sanity, through the persona server with the real GGUFs
    _write_state({"stage": "eval"})
    eval_set = lora_eval.load_eval_set()
    adapters = [gguf, *([Path(current["gguf"])] if have_current else [])]
    configs: dict[str, Path | None] = {"base": None, "candidate": gguf, **({"current": Path(current["gguf"])} if have_current else {})}
    try:
        generated = await _generation_eval(profile, adapters, configs, eval_set, _recall_items(manifest))
    except Exception as exc:
        return _failed("eval", f"the adapter could not be evaluated: {type(exc).__name__}: {exc}")

    # 6. the gate
    _write_state({"stage": "gate"})
    reference_name = "current" if have_current else "base"
    reference = {"persona_nll": reference_nll, **generated[reference_name]}
    candidate = {"persona_nll": candidate_nll, **generated["candidate"]}
    verdict = lora_eval.decide(reference, candidate)
    scores = {"reference": reference_name, "candidate": candidate, "reference_scores": reference, "checks": verdict["checks"],
              "eval_set": eval_set["digest"], "base_nll": nll["base"]["nll"]}
    common = {"dataset_version": built["version"], "dataset_hash": built["hash"], "examples": approved, "scores": scores, "train": report}
    if not verdict["deploy"]:
        shutil.rmtree(peft_dir.parent, ignore_errors=True)
        return {"status": "rejected", "stage": "gate", "reason": "; ".join(verdict["reasons"]), "kept": current["id"] if current else None, **common}

    # 7. deploy: register, activate, keep the newest few, make it a provider
    _write_state({"stage": "deploy"})
    adapter_id = run_id
    root = persona_adapter.adapters_dir()
    root.mkdir(parents=True, exist_ok=True)
    final_gguf, final_peft = root / f"{adapter_id}.gguf", root / f"{adapter_id}.peft"
    shutil.move(str(gguf), final_gguf)
    shutil.rmtree(final_peft, ignore_errors=True)
    shutil.copytree(peft_dir, final_peft)
    shutil.rmtree(peft_dir.parent, ignore_errors=True)
    persona_adapter.add_adapter({"id": adapter_id, "created": started.isoformat(timespec="seconds"), "gguf": str(final_gguf), "peft_dir": str(final_peft),
                                 "base": profile.persona.base_model, "decision": "deployed", **common})
    persona_adapter.activate(adapter_id)
    await persona_adapter.stop()  # the next turn that uses it starts it on the new adapter
    pruned = persona_adapter.prune(keep=int(config["lora_keep"]))
    await sync_provider_row()
    return {"status": "deployed", "stage": "done", "adapter": adapter_id, "pruned": pruned, "reason": reason, **common}


async def sync_provider_row() -> None:
    """Make the inference_providers table agree with the registry: a row for the persona while an adapter is active (at the end of the local
    band, so naming it in the provider picker works and nothing else changes), none otherwise. A database error is logged, not raised."""
    from python.harness import db
    from python.inference import persona_adapter

    try:
        provider = persona_adapter.provider()
        if provider is None:
            await db.execute("DELETE FROM inference_providers WHERE id = $1 AND locked = FALSE", persona_adapter.PROVIDER_ID)
            return
        rows = await db.fetch("SELECT priority FROM inference_providers WHERE type = 'local' AND id <> $1", persona_adapter.PROVIDER_ID)
        priority = (max(int(r["priority"]) for r in rows) + 1) if rows else LOCAL_PRIORITY_BASE
        await db.execute(
            "INSERT INTO inference_providers (id, type, name, priority, status, config, locked) VALUES ($1, 'local', $2, $3, 'online', $4::jsonb, FALSE) "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, status = 'online', config = EXCLUDED.config, updated_at = NOW()",
            provider["id"], provider["name"], priority, json.dumps(provider["config"]),
        )
    except Exception as exc:
        log.warning("lora_pipeline: could not update the persona provider row: %s", exc)


def _record_run(result: dict[str, Any], started: datetime) -> None:
    """Every run, kept or not, is written down as an HTML-NN record: what was tried, how it scored, and why it was or was not served. It
    is documentation of what the system did, not something she learned, so it is not a training source (doc_target ``system.lora-run``)."""
    try:
        from sdk.agent.documentation_nodes import create_documentation_node

        lines = [f"Persona adapter run {result['run_id']} started {started.isoformat(timespec='seconds')}: {result['status']}."]
        for key in ("stage", "reason", "adapter", "dataset_version", "examples", "kept", "pruned"):
            if result.get(key) not in (None, "", []):
                lines.append(f"{key}: {result[key]}")
        scores = result.get("scores") or {}
        for name, check in (scores.get("checks") or {}).items():
            lines.append(f"{name}: candidate {check['candidate']} against {check['reference']} ({'ok' if check['ok'] else 'failed'})")
        create_documentation_node(
            node_id=f"lora-run-{result['run_id']}", title=f"Persona adapter run {result['run_id']}", doc_target="system.lora-run",
            semantic_memory="\n".join(lines), node_type="documentation", purpose="record", tags=["html-nn", "lora", "record"], stage=False,
        )
    except Exception as exc:
        log.warning("lora_pipeline: could not write the run record: %s", exc)


# --- starting a run -------------------------------------------------------------------------------------------------------------


def start_background(*, reason: str = "ui", force: bool = False) -> dict[str, Any]:
    """Start a run without waiting for it (it is minutes on the GPU, hours on the CPU). Refuses while halted or while one is running."""
    if _RUNNING or _marker_active(datetime.now()):
        return {"status": "busy", "reason": "a training run is already in progress"}
    if _halted():
        return {"status": "skipped", "reason": "the emergency halt is engaged"}
    asyncio.get_running_loop().create_task(run_pipeline(reason=reason, force=force))
    return {"status": "started"}


async def retrain_if_due(now: datetime | None = None) -> dict[str, Any] | None:
    """The heartbeat's question: is it time for the weekly run? ``None`` means it did not start one. Never blocks the tick. Attempted at
    most once per ``lora_interval_days`` whatever the outcome, so a failing run is not retried every minute."""
    from sdk.agent import dreaming
    from sdk.agent import node_learning as nl

    config = settings()
    if not config["lora_auto_train"] or not nl.learning_enabled() or _halted():
        return None
    local = now if now is not None else datetime.now()
    if local.tzinfo is not None:
        local = local.astimezone().replace(tzinfo=None)
    if local.hour < int(config["lora_hour"]):
        return None
    last = _parse(_read_state().get("last_attempt_date"))
    if last is not None and (local.date() - last.date()).days < int(config["lora_interval_days"]):
        return None
    if _RUNNING or _marker_active(local):
        return None
    if not await dreaming.system_idle(float(config["lora_idle_minutes"])):
        return None
    _write_state({"last_attempt_date": local.date().isoformat()})
    asyncio.get_running_loop().create_task(run_pipeline(reason="scheduled", now=local))
    return {"status": "started"}
