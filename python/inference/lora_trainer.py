"""
lora_trainer.py - the old per-node, per-turn LoRA training queue. SUPERSEDED, and deliberately off.

*** ``IMPLEMENTED = False`` is intentional and must stay False. *** It gates server.py's per-turn JIT queue (one adapter per recalled
node, trained in the middle of a chat turn), which was found low-value: only one adapter can be attached per turn, so N-1 of N were
discarded, and most nodes are generated documentation, so a per-node adapter learned the register of API prose.

What replaced it is real and lives in ``sdk/agent/lora_pipeline.py``: one adapter trained over her chat sessions and her HTML-NN skills
and memories together, merged into its base, converted to GGUF and imported into Ollama as a new local provider, on a schedule, when
enough is new. ``engine_status()`` below reports that to the engine panel; ``queue_lora_training`` still raises
``LoraTrainingUnavailable`` rather than pretend.
"""

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger("tala.inference.lora")

#: Flip only when training AND loading an adapter both genuinely work.
IMPLEMENTED = False
STATUS_NOTE = (
    "Per-node LoRA training is not implemented (superseded): adapters are trained over her chat sessions and HTML-NN skills and "
    "memories on a schedule by sdk/agent/lora_pipeline.py."
)


def engine_status() -> dict[str, Any]:
    """What the engine panel shows about LoRA: the scheduled pipeline's state, not this retired queue's."""
    try:
        from sdk.agent import lora_pipeline

        state = lora_pipeline.status()
    except Exception as exc:  # the panel must render even if the pipeline cannot be imported
        return {"implemented": False, "note": f"{STATUS_NOTE} (pipeline unavailable: {exc})"}
    scheduled = "scheduled nightly" if state["auto_train"] else "scheduled training is off (lora_auto_train)"
    last = f"last run {state['last_status']}" + (f" ({state['last_model']})" if state["last_model"] else "") if state["last_status"] else "no run yet"
    return {"implemented": True, "auto_train": state["auto_train"], "running": state["running"], "last_status": state["last_status"],
            "last_model": state["last_model"], "note": f"Voice adapter training: {scheduled}; {last}."}

_training_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
_trainer_task: asyncio.Task[None] | None = None
_training_lock = threading.Lock()

class LoraTrainingUnavailable(RuntimeError):
    """
    Raised when a training job could not actually train anything.

    Exists because the alternative was returning normally: the queue logged
    "Successfully trained LoRA for node X" whether the job wrote a real adapter,
    wrote a 51-byte placeholder, or executed a bare `pass`. Every adapter in the
    repository was produced by one of the latter two.
    """


def _is_peft_available() -> bool:
    """Check if the necessary PEFT/TRL training libraries are installed."""
    try:
        import peft  # noqa: F401
        import trl  # noqa: F401
        return True
    except ImportError:
        return False

async def queue_lora_training(node_id: str, semantic_memory: str, output_path: str) -> None:
    """
    Queue an HTML-NN for background JIT LoRA training.
    """
    await _training_queue.put({
        "node_id": node_id,
        "semantic_memory": semantic_memory,
        "output_path": output_path
    })
    logger.info("Queued JIT LoRA training for node %s", node_id)
    
    global _trainer_task
    if _trainer_task is None or _trainer_task.done():
        _trainer_task = asyncio.create_task(_training_loop())

def _perform_training_sync(job: dict[str, Any]) -> None:
    """Synchronous heavy lifting for LoRA training, meant for a background thread."""
    node_id = job["node_id"]
    output_path = job["output_path"]
    semantic_memory = job["semantic_memory"]
    
    if not _is_peft_available():
        # Nothing is written to disk. An earlier version of this branch kept
        # writing DUMMY_LORA_WEIGHTS_FOR_<node_id> to the real .safetensors
        # path on the reasoning that downstream existence checks depend on the
        # path being there. That reasoning was backwards.
        #
        # The JIT selector in server.py gates on a plain Path.exists():
        #
        #     if Path(out_path).exists():   best_lora_path = out_path
        #     elif combined:                queue_lora_training(...)
        #
        # so the placeholder permanently satisfies the first branch. Training is
        # never queued for that node again -- not after peft is installed, not
        # ever -- and every turn hands a 40-byte text file to peft as an adapter.
        # It also rebuilt exactly the files commit 16e93a5 deleted, on first
        # activation.
        #
        # Writing nothing keeps exists() false, so activation re-queues training
        # until something can genuinely produce weights.
        logger.warning(
            "PEFT/TRL not installed: no adapter trained for node %s, and nothing written. "
            "Install the training extra to train for real: uv pip install -e \".[training]\"",
            node_id,
        )
        raise LoraTrainingUnavailable(
            f"peft/trl not installed; no adapter produced for {node_id}"
        )

    logger.info("Starting real JIT LoRA training for %s", node_id)
    try:
        # NOT IMPLEMENTED. peft and trl being importable is not the same as this
        # function training anything -- the body below is the plan, never the
        # work, and the caller used to log "Successfully trained LoRA" over the
        # top of it either way.
        #
        # 1. Convert semantic_memory to a HuggingFace Dataset
        # 2. Load the base model (unsloth/Llama-3.2-1B-Instruct, the ungated Meta Llama 3.2 1B -- US-origin bases only)
        # 3. LoraConfig(r=8, lora_alpha=16)
        # 4. Train, then model.save_pretrained(output_path)
        raise LoraTrainingUnavailable(
            f"JIT LoRA training is not implemented; no adapter was produced for {node_id}"
        )
    except LoraTrainingUnavailable:
        raise
    except Exception:
        logger.exception("Failed to train LoRA for %s", node_id)
        raise

async def _training_loop() -> None:
    """Background loop that processes training jobs one at a time."""
    logger.info("JIT LoRA Training loop started.")
    while True:
        try:
            job = await _training_queue.get()
            
            # Offload heavy training to a thread so we don't block the asyncio event loop
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, _perform_training_sync, job)
                logger.info(
                    "Successfully trained LoRA for node %s. Saved to %s",
                    job["node_id"], job["output_path"],
                )
            except LoraTrainingUnavailable as exc:
                # Reported once per job at warning level rather than as an
                # exception trace: it is a missing capability, not a crash, and
                # the loop must keep draining the queue. What it must NOT do is
                # log success, which is what it did for every adapter on disk.
                logger.warning("LoRA training did not run for %s: %s", job["node_id"], exc)
            finally:
                _training_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("Error in JIT LoRA training loop: %s", exc)
