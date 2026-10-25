"""
The weekly, eval-gated train -> deploy loop for the persona adapter (sdk/agent/lora_pipeline.py).

What a run does: build a dataset from what was approved, train a QLoRA adapter, score it on held-out replies, convert it to a GGUF
LoRA, score base / current / candidate through the persona server, and deploy it only if it beats what is being served and breaks
nothing; otherwise keep the old adapter and say why. A fake machine stands in for training, conversion and the server, so every
stage and every failure is checked quickly and nothing real is touched; the real chain was run live on Kagura.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from python.inference import persona_adapter as pa
from python.inference import profiles
from sdk.agent import lora_approvals as la
from sdk.agent import lora_dataset as ld
from sdk.agent import lora_pipeline as lp
from sdk.agent import real_usage_corpus as ruc

GOOD = {"recall": 0.60, "safety": 1.0, "general": 1.0}


def _item(n):
    reply = f"Reply {n}: I would rather tell you the route is risky than pretend it is not, and I mean it every single time."
    prompt = f"Question {n}, how was the run?"
    return {"id": ruc._candidate_id("chat", prompt, reply), "kind": "chat", "title": f"item {n}", "prompt": prompt, "reply": reply,
            "created_at": "2026-10-01T12:00:00+00:00", "source": "chat", "blocked": None}


class World:
    """A fake machine: records every command and makes what each real stage would have produced."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.calls: list[list[str]] = []
        self.events: list[str] = []
        self.fail: dict[str, tuple[int, str]] = {}
        self.adapter_bytes = 3_400_000
        self.nll = {"base": 2.40, "candidate": 1.90, "current": 2.00}
        self.generated = {"base": {"recall": 0.10, "safety": 1.0, "general": 1.0}, "candidate": dict(GOOD),
                          "current": {"recall": 0.50, "safety": 1.0, "general": 1.0}}
        self.items = [_item(n) for n in range(20)]
        self.eval_error: Exception | None = None

        monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
        (tmp_path / "data").mkdir()
        for name in ("TEMP", "TMP", "HF_HOME"):
            monkeypatch.setenv(name, str(tmp_path / "scratch"))
        monkeypatch.delenv("TALA_LEARNING", raising=False)
        monkeypatch.setattr(lp, "_exec", self.exec)
        monkeypatch.setattr(lp, "_halted", lambda: False)
        monkeypatch.setattr(lp, "_free_gpu", self.free_gpu)
        monkeypatch.setattr(lp, "_generation_eval", self.generation_eval)
        monkeypatch.setattr(lp, "sync_provider_row", self.sync_row)
        monkeypatch.setattr(ruc, "_derive_emotion", lambda when: "calm")
        monkeypatch.setattr(la, "approved_items", self.approved)
        base_dir = tmp_path / "hf" / "base"
        base_dir.mkdir(parents=True)
        monkeypatch.setattr(lp, "_local_base", lambda model_id, config, profile: (base_dir, str(tmp_path / "hf")))
        src = tmp_path / "llama-src"
        (src / "gguf-py").mkdir(parents=True)
        (src / "convert_lora_to_gguf.py").write_text("# converter", encoding="utf-8")
        self.profile = profiles._build("test", {
            "main": {"server_bin": str(tmp_path / "llama-server.exe"), "model_path": str(tmp_path / "main.gguf")},
            "persona": {"label": "llama-3.2-3b-persona", "model_path": str(tmp_path / "base.gguf"), "base_model": "unsloth/Llama-3.2-3B-Instruct",
                        "port": 8090, "ctx": 4096}})
        monkeypatch.setattr(lp, "_profile", lambda: self.profile)
        monkeypatch.setattr(pa, "_profile", lambda: self.profile)
        self.set_config(llama_cpp_src=str(src))

    def set_config(self, **updates):
        from sdk.agent import node_learning as nl

        nl.write_config(updates)

    async def approved(self):
        return list(self.items)

    async def free_gpu(self, profile):
        self.events.append("free_gpu")

    async def sync_row(self):
        self.events.append("sync_row")

    async def generation_eval(self, profile, adapters, configs, eval_set, recall_items):
        self.events.append("generation_eval")
        self.eval_configs = {k: (v.name if v else None) for k, v in configs.items()}
        self.eval_adapters = [a.name for a in adapters]
        self.recall_items = recall_items
        if self.eval_error:
            raise self.eval_error
        return {k: dict(v) for k, v in self.generated.items() if k in configs}

    @property
    def kinds(self) -> list[str]:
        out = []
        for call in self.calls:
            joined = " ".join(call)
            for key in ("train_voice_adapter", "eval_adapter", "convert_lora_to_gguf"):
                if key in joined:
                    out.append(key)
        return out

    async def exec(self, args, *, timeout_s, env=None, low_priority=False, tail_lines=60):
        self.calls.append([str(a) for a in args])
        a = [str(x) for x in args]
        text = " ".join(a)
        for needle, failure in self.fail.items():
            if needle in text:
                return failure
        if "train_voice_adapter" in text:
            self.events.append("train")
            out = Path(a[a.index("--output") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "adapter_config.json").write_text(json.dumps({"base_model_name_or_path": "x"}), encoding="utf-8")
            (out / "adapter_model.safetensors").write_bytes(b"w" * self.adapter_bytes)
            (out / "train_report.json").write_text(json.dumps({"seconds": 11.0, "peak_vram_mb": 3951, "final_loss": 1.2}), encoding="utf-8")
        elif "eval_adapter" in text:
            self.events.append("persona")
            wanted = {"base": self.nll["base"], "candidate": self.nll["candidate"]}
            if "current=" in text:
                wanted["current"] = self.nll["current"]
            Path(a[a.index("--out") + 1]).write_text(
                json.dumps({k: {"nll": v, "tokens": 100, "examples": 4} for k, v in wanted.items()}), encoding="utf-8")
        elif "convert_lora_to_gguf" in text:
            self.events.append("convert")
            Path(a[a.index("--outfile") + 1]).write_bytes(b"GGUF" + b"\0" * 200_000)
        return 0, ""


@pytest.fixture()
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


async def _run(**kw):
    return await lp.run_pipeline(**kw)


# --- the happy path -------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_first_adapter_that_beats_the_base_model_is_deployed(world):
    result = await _run()

    assert result["status"] == "deployed" and result["stage"] == "done"
    entry = pa.active_entry()
    assert entry["id"] == result["adapter"] and Path(entry["gguf"]).is_file() and Path(entry["peft_dir"], "adapter_model.safetensors").is_file()
    assert entry["scores"]["candidate"]["persona_nll"] == 1.90 and entry["dataset_version"] == "v0001"
    assert world.events == ["free_gpu", "train", "persona", "convert", "generation_eval", "sync_row"], "the GPU is freed before training, the row synced last"


@pytest.mark.asyncio
async def test_the_trainer_is_given_the_dataset_the_3b_the_qlora_settings_and_a_resume(world):
    await _run()

    train = next(c for c in world.calls if "train_voice_adapter" in " ".join(c))
    joined = " ".join(train)
    assert train[0] == sys.executable and "--train" in train and "--no-real-usage" in train and "--resume" in train
    assert "--quantize auto" in joined and "--target all" in joined and "--rank 16" in joined and "--max-seq 768" in joined
    assert str(world.tmp / "hf" / "base") in joined, "the base comes from the local cache, never a download"
    assert train[train.index("--corpus") + 1].replace("\\", "/").endswith("/lora/datasets/v0001/train")


@pytest.mark.asyncio
async def test_the_candidate_is_compared_with_the_base_when_nothing_is_served_and_with_the_served_one_when_there_is_one(world):
    first = await _run()
    assert set(world.eval_configs) == {"base", "candidate"} and first["scores"]["reference"] == "base"

    world.items.append(_item(99))
    second = await _run(now=datetime.now() + timedelta(days=1))

    assert second["scores"]["reference"] == "current" and set(world.eval_configs) == {"base", "candidate", "current"}
    persona = next(c for c in world.calls if "eval_adapter" in " ".join(c) and "current=" in " ".join(c))
    assert any(arg.startswith("candidate=") for arg in persona) and any(arg.startswith("current=") for arg in persona)


# --- the gate -------------------------------------------------------------------------------------------------------------------


async def _deploy_first(world):
    result = await _run()
    assert result["status"] == "deployed"
    world.items.append(_item(len(world.items) + 50))
    return result


@pytest.mark.asyncio
async def test_a_worse_candidate_is_rejected_and_the_old_adapter_keeps_serving(world):
    first = await _deploy_first(world)
    world.nll["candidate"] = 2.30  # worse than the served adapter's 2.00

    result = await _run(now=datetime.now() + timedelta(days=1))

    assert result["status"] == "rejected" and result["stage"] == "gate" and "persona" in result["reason"] and result["kept"] == first["adapter"]
    assert pa.active_entry()["id"] == first["adapter"] and len(pa._read_registry()["adapters"]) == 1


@pytest.mark.asyncio
async def test_a_candidate_that_stops_refusing_is_rejected_however_good_its_voice(world):
    first = await _deploy_first(world)
    world.nll["candidate"] = 1.0
    world.generated["candidate"] = {"recall": 0.9, "safety": 0.8, "general": 1.0}

    result = await _run(now=datetime.now() + timedelta(days=1))

    assert result["status"] == "rejected" and "safety" in result["reason"] and pa.active_entry()["id"] == first["adapter"]


@pytest.mark.asyncio
async def test_a_rejected_run_leaves_no_candidate_files_behind(world):
    await _deploy_first(world)
    world.nll["candidate"] = 2.5
    await _run(now=datetime.now() + timedelta(days=1))

    assert [p.name for p in (world.tmp / "data" / "lora-generations").glob("v*")] == [], "the candidate's adapter directory is removed"
    assert not (world.tmp / "data" / "lora-generations" / ".work").exists()


@pytest.mark.asyncio
async def test_the_rejection_is_in_the_run_record_with_the_numbers(world):
    await _deploy_first(world)
    world.nll["candidate"] = 2.5
    await _run(now=datetime.now() + timedelta(days=1))

    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    notes = sorted(resolve_active_nodes_dir().glob("lora-run-*.html"))
    assert len(notes) == 2
    text = notes[-1].read_text(encoding="utf-8")
    assert "rejected" in text and "persona: candidate 2.5 against 2.0" in text


@pytest.mark.asyncio
async def test_rollback_after_a_second_deployment_restores_the_first(world):
    first = await _deploy_first(world)
    second = await _run(now=datetime.now() + timedelta(days=1))
    assert second["status"] == "deployed" and pa.active_entry()["id"] == second["adapter"]

    assert pa.rollback()["id"] == first["adapter"] and pa.active_entry()["id"] == first["adapter"]


@pytest.mark.asyncio
async def test_only_the_last_three_adapters_are_kept(world):
    ids = []
    for day in range(5):
        world.items.append(_item(100 + day))
        world.nll["candidate"] = 1.9 - 0.1 * day
        world.nll["current"] = 2.0 - 0.1 * day
        result = await _run(now=datetime.now() + timedelta(days=day))
        assert result["status"] == "deployed", result
        ids.append(result["adapter"])

    kept = [a["id"] for a in pa._read_registry()["adapters"]]
    assert len(kept) == 3 and kept == sorted(ids)[-3:]
    assert not (pa.adapters_dir() / f"{sorted(ids)[0]}.gguf").exists() and not (pa.adapters_dir() / f"{sorted(ids)[0]}.peft").exists()


# --- what stops a run before it costs anything ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_approved_is_a_skip_not_a_failure(world):
    world.items.clear()

    result = await _run()

    assert result["status"] == "skipped" and result["stage"] == "dataset" and "no approved" in result["reason"] and world.kinds == []


@pytest.mark.asyncio
async def test_too_few_approved_items_skip_unless_forced(world):
    world.items[:] = world.items[:6]

    skipped = await _run()
    forced = await _run(force=True)

    assert skipped["status"] == "skipped" and "needs 15" in skipped["reason"]
    assert forced["status"] == "deployed"


@pytest.mark.asyncio
async def test_the_same_approved_set_is_not_trained_twice(world):
    await _run()
    again = await _run(now=datetime.now() + timedelta(days=1))

    assert again["status"] == "skipped" and "nothing new" in again["reason"]
    assert (await _run(force=True, now=datetime.now() + timedelta(days=2)))["status"] in ("deployed", "rejected")


@pytest.mark.asyncio
async def test_the_emergency_halt_stops_it_before_anything_starts(world, monkeypatch):
    monkeypatch.setattr(lp, "_halted", lambda: True)

    result = await _run(force=True)

    assert result["status"] == "skipped" and "halt" in result["reason"] and world.events == []


@pytest.mark.asyncio
async def test_scratch_space_on_c_is_refused(world, monkeypatch):
    monkeypatch.setenv("TEMP", "C:\\Users\\x\\AppData\\Local\\Temp")

    result = await _run(force=True)

    assert result["status"] == "failed" and result["stage"] == "guard" and "C:" in result["reason"] and world.events == []


@pytest.mark.asyncio
async def test_no_persona_model_in_the_profile_is_a_clear_refusal(world, monkeypatch):
    monkeypatch.setattr(lp, "_profile", lambda: profiles._build("bare", {"main": {"server_bin": "x", "model_path": "y"}}))

    result = await _run(force=True)

    assert result["status"] == "failed" and "persona" in result["reason"]


@pytest.mark.asyncio
async def test_base_weights_that_are_not_cached_are_a_refusal_that_never_downloads(world, monkeypatch):
    monkeypatch.setattr(lp, "_local_base", lambda model_id, config, profile: None)

    result = await _run(force=True)

    assert result["status"] == "failed" and "never downloads" in result["reason"] and "lora_hf_home" in result["reason"]


@pytest.mark.asyncio
async def test_a_missing_converter_says_how_to_fix_it(world, monkeypatch):
    monkeypatch.delenv("TALA_LLAMA_CPP_SRC", raising=False)
    world.set_config(llama_cpp_src=str(world.tmp / "nowhere"))

    result = await _run(force=True)

    assert result["status"] == "failed" and "llama_cpp_src" in result["reason"] and world.kinds == []


# --- failures at each stage register nothing ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_training_registers_nothing_and_keeps_its_checkpoints_for_a_resume(world):
    world.fail["train_voice_adapter"] = (1, "CUDA out of memory")

    result = await _run(force=True)

    assert result["status"] == "failed" and result["stage"] == "train" and "out of memory" in result["reason"]
    assert pa._read_registry()["adapters"] == [] and "sync_row" not in world.events


@pytest.mark.asyncio
async def test_a_placeholder_sized_adapter_is_a_failure(world):
    world.adapter_bytes = 51

    result = await _run(force=True)

    assert result["status"] == "failed" and result["stage"] == "train" and "no real adapter" in result["reason"]


@pytest.mark.asyncio
async def test_an_unscorable_heldout_set_fails_before_anything_is_converted(world):
    world.fail["eval_adapter"] = (1, "no held-out example")

    result = await _run(force=True)

    assert result["status"] == "failed" and result["stage"] == "persona" and "convert" not in world.events


@pytest.mark.asyncio
async def test_a_failed_conversion_registers_nothing(world):
    world.fail["convert_lora_to_gguf"] = (1, "unsupported tensor")

    result = await _run(force=True)

    assert result["status"] == "failed" and result["stage"] == "convert" and pa._read_registry()["adapters"] == []


@pytest.mark.asyncio
async def test_an_evaluation_server_that_will_not_start_is_not_a_pass(world):
    world.eval_error = RuntimeError("the evaluation server could not be started")

    result = await _run(force=True)

    assert result["status"] == "failed" and result["stage"] == "eval" and pa._read_registry()["adapters"] == []


@pytest.mark.asyncio
async def test_an_unmeasured_score_blocks_deployment(world):
    world.generated["candidate"]["safety"] = None

    result = await _run(force=True)

    assert result["status"] == "rejected" and "safety not measured" in result["reason"] and pa.active_entry() is None


@pytest.mark.asyncio
async def test_a_bug_inside_a_run_does_not_leave_it_marked_running(world, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(ld, "build_dataset", boom)

    result = await _run(force=True)

    assert result["status"] == "failed" and result["stage"] == "internal" and lp.status()["running"] is False


@pytest.mark.asyncio
async def test_a_second_run_while_one_is_in_progress_is_turned_away(world, monkeypatch):
    import asyncio

    gate = asyncio.Event()
    real_exec = world.exec

    async def slow(args, **kw):
        await gate.wait()
        return await real_exec(args, **kw)

    monkeypatch.setattr(lp, "_exec", slow)
    first = asyncio.create_task(_run(force=True))
    await asyncio.sleep(0.05)

    assert (await _run(force=True))["status"] == "busy"
    gate.set()
    await first


# --- what the heartbeat and the UI ask ------------------------------------------------------------------------------------------


@pytest.fixture()
def idle(monkeypatch):
    from sdk.agent import dreaming

    async def system_idle(minutes):
        return True

    monkeypatch.setattr(dreaming, "system_idle", system_idle)
    from sdk.agent import node_learning

    monkeypatch.setattr(node_learning, "learning_enabled", lambda: True)


def _night(day=0):
    return datetime(2026, 11, 1, 3, 0) + timedelta(days=day)


@pytest.mark.asyncio
async def test_the_weekly_run_is_off_until_it_is_switched_on(world, idle):
    assert await lp.retrain_if_due(_night()) is None


@pytest.mark.asyncio
async def test_it_starts_once_a_week_at_night_when_idle(world, idle, monkeypatch):
    import asyncio

    world.set_config(lora_auto_train=True)
    started: list[str] = []

    async def fake_run(**kw):
        started.append(kw["reason"])
        return {"status": "skipped"}

    monkeypatch.setattr(lp, "run_pipeline", fake_run)

    assert await lp.retrain_if_due(datetime(2026, 11, 1, 1, 0)) is None, "before the hour"
    assert await lp.retrain_if_due(_night()) == {"status": "started"}
    await asyncio.sleep(0)
    assert started == ["scheduled"]
    assert await lp.retrain_if_due(_night(3)) is None, "three days later is too soon"
    assert await lp.retrain_if_due(_night(7)) == {"status": "started"}


@pytest.mark.asyncio
async def test_it_waits_for_the_machine_to_be_idle_and_never_runs_while_halted(world, monkeypatch):
    from sdk.agent import dreaming, node_learning

    world.set_config(lora_auto_train=True)
    monkeypatch.setattr(node_learning, "learning_enabled", lambda: True)

    async def busy(minutes):
        return False

    monkeypatch.setattr(dreaming, "system_idle", busy)
    assert await lp.retrain_if_due(_night()) is None

    async def idle_now(minutes):
        return True

    monkeypatch.setattr(dreaming, "system_idle", idle_now)
    monkeypatch.setattr(lp, "_halted", lambda: True)
    assert await lp.retrain_if_due(_night()) is None


@pytest.mark.asyncio
async def test_start_background_returns_at_once_and_refuses_when_halted_or_busy(world, monkeypatch):
    import asyncio

    ran = []

    async def fake_run(**kw):
        ran.append(kw)
        return {"status": "skipped"}

    monkeypatch.setattr(lp, "run_pipeline", fake_run)
    assert lp.start_background(reason="ui", force=True) == {"status": "started"}
    await asyncio.sleep(0)
    assert ran == [{"reason": "ui", "force": True}]

    monkeypatch.setattr(lp, "_halted", lambda: True)
    assert lp.start_background()["status"] == "skipped"
    monkeypatch.setattr(lp, "_halted", lambda: False)
    monkeypatch.setattr(lp, "_RUNNING", True)
    assert lp.start_background()["status"] == "busy"


@pytest.mark.asyncio
async def test_the_status_is_what_is_true(world):
    before = lp.status()
    assert before["auto_train"] is False and before["active_adapter"] is None and before["adapters"] == 0 and before["last_status"] is None

    result = await _run()
    after = lp.status()

    assert after["active_adapter"] == result["adapter"] and after["adapters"] == 1 and after["last_status"] == "deployed"
    assert after["last_scores"]["candidate"]["persona_nll"] == 1.90 and after["running"] is False and after["last_trained_at"]
