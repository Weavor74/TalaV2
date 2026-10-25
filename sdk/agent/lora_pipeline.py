"""
sdk/agent/lora_pipeline.py -- the autonomous train -> deploy loop for Tala's voice adapter.

One run: sync the training corpus (her chat sessions and her HTML-NN skills and memories), train a LoRA adapter, merge it into its
base model, convert the merged model to GGUF, import it into Ollama, smoke-test it, register it as a local provider, keep one
earlier version, and write a record of the run as an HTML-NN node. No person approves a run; what stands in for a glance is that
every stage must produce what the next one needs, the new model must give a sane answer before it is registered, and a failure at
any point registers nothing and cleans up.

Why this shape, measured on the real machine (2026-10-25) rather than assumed:
  * Ollama here refuses ``ADAPTER`` ("LoRA adapters are no longer supported") and cannot import safetensors on Windows (it wants
    Apple's MLX), so a trained adapter cannot be loaded as an adapter. It has to be merged into the base and imported as a GGUF.
  * The embedded main model on Kagura is a 20B that cannot take a LoRA on a 6 GB card, and the original plan's Qwen base is out
    under the US-origin rule. The base is Llama-3.2-1B-Instruct (Meta), so v1 deploys to Ollama only.
  * A 1B model must not silently replace the 20B. By default the new model is registered at the END of the local band, exactly as
    the server's own auto-detect would register any new Ollama model. ``lora_promote: "primary"`` is the opt-in to put it first.

Configuration lives in learning.json (the learning settings file), all optional:
    lora_auto_train     false   the scheduled run is off until this is true (a run is hours of CPU, so it is opt-in)
    lora_hour           2       local hour after which the scheduled run may start
    lora_idle_minutes   30      how long the system must have been idle first
    lora_min_total      100     examples needed for a first training
    lora_min_new        50      new examples since the last training needed for another
    lora_promote        "register" | "primary"
    lora_template_from  "llama3.2:3b"   an installed Ollama model whose chat template the new model borrows
    lora_base           ""      a base other than the trainer's default (must be US-origin)
    lora_keep           2       model versions kept (the newest plus earlier ones)
    lora_train_args     []      extra arguments for the trainer (e.g. ["--epochs", "1"])
    llama_cpp_src       ""      a llama.cpp checkout (same commit as the engine) holding convert_hf_to_gguf.py and gguf-py
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_PREFIX = "tala-voice-"
OLLAMA_ENDPOINT = "http://localhost:11434"
MIN_ADAPTER_BYTES = 4096
MIN_GGUF_BYTES = 1_000_000
STALE_RUN_HOURS = 24.0
LOCAL_PRIORITY_BASE = 1000  # the server's own band for auto-detected local models
SMOKE_PROMPT = "Hey Tala, how are you doing tonight?"
TEMPLATE_FALLBACKS = ("llama3.2:3b", "llama3.2:1b", "llama3.1:8b", "llama3:8b")

DEFAULTS: dict[str, Any] = {
    "lora_auto_train": False, "lora_hour": 2, "lora_idle_minutes": 30.0, "lora_min_total": 100, "lora_min_new": 50,
    "lora_promote": "register", "lora_template_from": "llama3.2:3b", "lora_base": "", "lora_keep": 2, "lora_train_args": [],
    "llama_cpp_src": "",
}
#: Per-stage time allowed. Training is hours of CPU on a 1B model; everything else is minutes.
TIMEOUT_S = {"train": 16 * 3600, "merge": 1800, "convert": 1800, "ollama": 900}

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
    """What a person needs to know: is it on, what did it last do, is one running."""
    state, config = _read_state(), settings()
    return {
        "auto_train": bool(config["lora_auto_train"]), "promote": config["lora_promote"], "running": _RUNNING or _marker_active(datetime.now()),
        "last_status": state.get("last_status"), "last_stage": state.get("last_stage"), "last_reason": state.get("last_reason"),
        "last_model": state.get("last_model"), "last_trained_at": state.get("last_trained_at"),
        "last_attempt_date": state.get("last_attempt_date"),
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


async def _chat(model: str, prompt: str, timeout_s: float = 300.0) -> str:
    """One short reply from an Ollama model, over the local API."""
    body = json.dumps({"model": model, "stream": False, "keep_alive": "30s", "messages": [{"role": "user", "content": prompt}],
                       "options": {"num_predict": 80, "temperature": 0.7}}).encode("utf-8")

    def call() -> str:
        request = urllib.request.Request(f"{OLLAMA_ENDPOINT}/api/chat", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 -- loopback, fixed scheme
            return str((json.loads(response.read()).get("message") or {}).get("content", ""))

    return await asyncio.to_thread(call)


def _sane_reply(text: str) -> bool:
    """A cheap bar for "this model answers": some words, no error text, not one word on repeat, not a wall of characters.
    It does not judge quality: a 1B model's replies are what they are. It catches a model that is broken."""
    text = (text or "").strip()
    if re.match(r"(?i)^(error|exception|traceback)\b", text):
        return False
    words = text.split()
    if len(words) < 3 or sum(c.isalpha() for c in text) < 0.5 * len(text):
        return False
    if len({w.lower() for w in words}) / len(words) < 0.3 or sum(map(len, words)) / len(words) > 20:
        return False
    return True


def _modelfile_text(gguf: Path, reference: str) -> str:
    """A Modelfile for the new GGUF that borrows another model's chat template and stop tokens (an imported GGUF has none of its
    own). The reference's header comments and its own FROM are dropped."""
    lines = reference.replace("\r", "").split("\n")
    while lines and (lines[0].startswith("#") or not lines[0].strip()):
        lines.pop(0)
    rest = [line for line in lines if not line.startswith("FROM ")]
    return f"FROM {gguf.as_posix()}\n" + "\n".join(rest).rstrip() + "\n"


def _converter_dir(config: dict[str, Any]) -> Path | None:
    for candidate in (config.get("llama_cpp_src"), os.environ.get("TALA_LLAMA_CPP_SRC")):
        if candidate and (Path(candidate) / "convert_hf_to_gguf.py").is_file() and (Path(candidate) / "gguf-py").is_dir():
            return Path(candidate)
    return None


def _on_c_drive() -> str | None:
    for name in ("TEMP", "TMP", "HF_HOME"):
        value = os.environ.get(name, "")
        if value.upper().startswith("C:"):
            return f"{name}={value}"
    return None


def _failed(stage: str, reason: str) -> dict[str, Any]:
    return {"status": "failed", "stage": stage, "reason": reason.strip()[-1500:]}


def _skipped(stage: str, reason: str) -> dict[str, Any]:
    return {"status": "skipped", "stage": stage, "reason": reason}


# --- the run --------------------------------------------------------------------------------------------------------------------


async def run_pipeline(*, reason: str = "manual", force: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """One full train -> deploy run. Never raises; the result says what happened and, if it stopped, at which stage and why."""
    global _RUNNING
    started = now or datetime.now()
    if _RUNNING or _marker_active(started):
        return {"status": "busy", "stage": "start", "reason": "a training run is already in progress"}
    _RUNNING = True
    run_id = started.strftime("%Y%m%d-%H%M%S")
    _write_state({"running_since": started.isoformat(), "last_reason_started": reason})
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
    state: dict[str, Any] = {"running_since": None, "last_status": result["status"], "last_stage": result.get("stage"),
                             "last_reason": result.get("reason"), "last_run_id": run_id}
    if result["status"] == "deployed":
        state.update(last_trained_at=started.astimezone().isoformat(), last_model=result["model"])
    _write_state(state)
    _record_run(result, started)
    log.info("lora_pipeline: run %s -> %s (%s)", run_id, result["status"], result.get("reason") or result.get("model"))
    return result


async def _run(run_id: str, scratch: Path, *, force: bool, reason: str, started: datetime) -> dict[str, Any]:
    from sdk.agent import real_usage_corpus as ruc

    config = settings()
    if _halted():
        return _skipped("guard", "the emergency halt is engaged")
    if (c_drive := _on_c_drive()):
        return _failed("guard", f"refusing to run with {c_drive}: training scratch space must not be on C:")

    # 1. the corpus: what is there, and is it enough (or new enough) to be worth hours of CPU
    last = _parse(_read_state().get("last_trained_at"))
    chats = await ruc.sync_real_usage_corpus(since=last)
    nodes = await ruc.sync_learning_events_corpus(since=last)
    if not chats.get("success"):
        return _failed("corpus", f"could not read her chat sessions: {chats.get('error')}")
    total = int(chats.get("included", 0)) + int(nodes.get("included", 0))
    new = int(chats.get("new_since", 0)) + int(nodes.get("new_since", 0))
    if not force:
        if last is None and total < int(config["lora_min_total"]):
            return _skipped("corpus", f"{total} examples; a first training needs {config['lora_min_total']}")
        if last is not None and new < int(config["lora_min_new"]):
            return _skipped("corpus", f"{new} new examples since the last training; another needs {config['lora_min_new']}")

    # 2. train
    adapter_dir = _generations_dir() / run_id
    _write_state({"stage": "train"})
    train = [sys.executable, str(REPO_ROOT / "scripts" / "train_voice_adapter.py"), "--train", "--no-real-usage", "--sources", "real",
             "--output", str(adapter_dir), *(["--base", config["lora_base"]] if config["lora_base"] else []), *map(str, config["lora_train_args"])]
    env = {**os.environ, "HF_HUB_OFFLINE": "1"}
    code, output = await _exec(train, timeout_s=TIMEOUT_S["train"], env=env, low_priority=True)
    weights = adapter_dir / "adapter_model.safetensors"
    if code != 0:
        return _failed("train", f"the trainer exited with code {code}:\n{output}")
    if not weights.is_file() or weights.stat().st_size < MIN_ADAPTER_BYTES:
        return _failed("train", f"the trainer wrote no real adapter (a placeholder-sized or missing {weights.name})")

    # 3. merge
    _write_state({"stage": "merge"})
    merged = scratch / "merged"
    code, output = await _exec([sys.executable, str(REPO_ROOT / "scripts" / "merge_adapter.py"), str(adapter_dir), str(merged)],
                               timeout_s=TIMEOUT_S["merge"], env=env, low_priority=True)
    if code != 0 or not (merged / "model.safetensors").is_file():
        return _failed("merge", f"merging the adapter failed (code {code}):\n{output}")

    # 4. convert to GGUF
    _write_state({"stage": "convert"})
    converter = _converter_dir(config)
    if converter is None:
        return _failed("convert", "no llama.cpp source with convert_hf_to_gguf.py was found: set llama_cpp_src in learning.json (or "
                                  "TALA_LLAMA_CPP_SRC) to a llama.cpp checkout, ideally the commit your engine binary was built from")
    gguf = scratch / f"{MODEL_PREFIX}{run_id}.gguf"
    convert_env = {**env, "PYTHONPATH": str(converter / "gguf-py") + os.pathsep + env.get("PYTHONPATH", "")}
    code, output = await _exec([sys.executable, str(converter / "convert_hf_to_gguf.py"), str(merged), "--outfile", str(gguf), "--outtype", "q8_0"],
                               timeout_s=TIMEOUT_S["convert"], env=convert_env, low_priority=True)
    if code != 0 or not gguf.is_file() or gguf.stat().st_size < MIN_GGUF_BYTES or gguf.read_bytes()[:4] != b"GGUF":
        return _failed("convert", f"converting to GGUF failed (code {code}):\n{output}")
    shutil.rmtree(merged, ignore_errors=True)  # 2.5 GB that is no longer needed

    # 5. import into Ollama
    _write_state({"stage": "import"})
    model = f"{MODEL_PREFIX}{run_id}"
    reference = await _reference_modelfile(config)
    if reference is None:
        return _failed("import", "no Llama 3 family model is installed in Ollama to borrow a chat template from; "
                                 "run `ollama pull llama3.2:1b` or set lora_template_from")
    modelfile = scratch / "Modelfile"
    modelfile.write_text(_modelfile_text(gguf, reference), encoding="utf-8")
    code, output = await _exec(["ollama", "create", model, "-f", str(modelfile)], timeout_s=TIMEOUT_S["ollama"])
    if code != 0:
        await _remove_model(model)
        return _failed("import", f"ollama create failed (code {code}):\n{output}")

    # 6. the model must answer before it is registered
    _write_state({"stage": "smoke"})
    try:
        answer = await _chat(model, SMOKE_PROMPT)
    except Exception as exc:
        answer = ""
        log.warning("lora_pipeline: the smoke test could not reach %s: %s", model, exc)
    if not _sane_reply(answer):
        await _remove_model(model)
        return _failed("smoke", f"{model} did not give a usable answer to the smoke prompt (got {answer[:120]!r}); it was removed and not registered")

    # 7. register, then keep only the newest versions
    priority = await _register(model, config, run_id)
    pruned = await _prune(config)
    return {"status": "deployed", "stage": "done", "model": model, "priority": priority, "examples": total, "new_examples": new,
            "pruned": pruned, "adapter_dir": str(adapter_dir), "smoke_reply": answer.strip()[:200], "reason": reason}


async def _reference_modelfile(config: dict[str, Any]) -> str | None:
    for name in dict.fromkeys([config["lora_template_from"], *TEMPLATE_FALLBACKS]):
        code, output = await _exec(["ollama", "show", name, "--modelfile"], timeout_s=60, tail_lines=2000)
        if code == 0 and "TEMPLATE" in output:
            return output
    return None


async def _remove_model(model: str) -> None:
    code, output = await _exec(["ollama", "rm", model], timeout_s=120)
    if code != 0:
        log.warning("lora_pipeline: could not remove %s: %s", model, output[-200:])


async def _register(model: str, config: dict[str, Any], run_id: str) -> int:
    """Make the new model a local provider, exactly as the server's own auto-detect would (same id and config), at the end of the
    local band, or first if ``lora_promote`` is "primary"."""
    from python.harness import db

    rows = await db.fetch("SELECT priority, type FROM inference_providers")
    priorities = [int(r["priority"]) for r in rows]
    local = [int(r["priority"]) for r in rows if r["type"] == "local"]
    if config["lora_promote"] == "primary":
        priority = (min(priorities) - 1) if priorities else LOCAL_PRIORITY_BASE
    else:
        priority = (max(local) + 1) if local else LOCAL_PRIORITY_BASE
    provider_id = "ollama-" + model.replace(":", "-").replace("/", "-").replace(".", "-")
    cfg = {"engine": "ollama", "endpoint": OLLAMA_ENDPOINT, "model": model, "tala_voice_run": run_id}
    await db.execute(
        "INSERT INTO inference_providers (id, type, name, priority, status, config, locked) VALUES ($1, 'local', $2, $3, 'online', $4::jsonb, FALSE) "
        "ON CONFLICT (id) DO UPDATE SET status = 'online', priority = EXCLUDED.priority, config = EXCLUDED.config, updated_at = NOW()",
        provider_id, f"OLLAMA - {model}", priority, json.dumps(cfg),
    )
    return priority


async def _prune(config: dict[str, Any]) -> list[str]:
    """Keep the newest ``lora_keep`` versions (the new one plus earlier ones as a way back); remove the rest, their provider rows and
    their adapters. A locked row is never touched."""
    from python.harness import db

    rows = await db.fetch("SELECT id, locked, config FROM inference_providers WHERE id LIKE 'ollama-tala-voice-%' ORDER BY id DESC")
    removed: list[str] = []
    for row in rows[max(1, int(config["lora_keep"])):]:
        if row["locked"]:
            continue
        model = row["id"][len("ollama-"):]
        await _remove_model(model)
        await db.execute("DELETE FROM inference_providers WHERE id = $1", row["id"])
        shutil.rmtree(_generations_dir() / model[len(MODEL_PREFIX):], ignore_errors=True)
        removed.append(model)
    return removed


def _record_run(result: dict[str, Any], started: datetime) -> None:
    """Every run, kept or not, is written down as an HTML-NN record. It is documentation of what the system did, not something she
    learned, so it is not a training source (doc_target ``system.lora-run``)."""
    try:
        from sdk.agent.documentation_nodes import create_documentation_node

        lines = [f"Voice adapter run {result['run_id']} started {started.isoformat(timespec='seconds')}: {result['status']}."]
        for key in ("stage", "reason", "model", "examples", "new_examples", "priority", "pruned"):
            if result.get(key) not in (None, "", []):
                lines.append(f"{key}: {result[key]}")
        create_documentation_node(
            node_id=f"lora-run-{result['run_id']}", title=f"Voice adapter run {result['run_id']}", doc_target="system.lora-run",
            semantic_memory="\n".join(lines), node_type="documentation", purpose="record", tags=["html-nn", "lora", "record"], stage=False,
        )
    except Exception as exc:
        log.warning("lora_pipeline: could not write the run record: %s", exc)


# --- the scheduler's question ---------------------------------------------------------------------------------------------------


async def retrain_if_due(now: datetime | None = None) -> dict[str, Any] | None:
    """The heartbeat's question: is it time to train? ``None`` means it did not start one. Never blocks the tick: a run is hours, so
    it is started in the background. Attempted once a day whatever the outcome, so a failing run is not retried every minute."""
    from sdk.agent import dreaming
    from sdk.agent import node_learning as nl

    config = settings()
    if not config["lora_auto_train"] or not nl.learning_enabled() or _halted():
        return None
    local = now if now is not None else datetime.now()
    if local.tzinfo is not None:
        local = local.astimezone().replace(tzinfo=None)
    if local.hour < int(config["lora_hour"]) or _read_state().get("last_attempt_date") == local.date().isoformat():
        return None
    if _RUNNING or _marker_active(local):
        return None
    if not await dreaming.system_idle(float(config["lora_idle_minutes"])):
        return None
    _write_state({"last_attempt_date": local.date().isoformat()})
    asyncio.get_running_loop().create_task(run_pipeline(reason="scheduled", now=local))
    return {"status": "started"}
