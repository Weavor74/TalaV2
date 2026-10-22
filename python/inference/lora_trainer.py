"""
lora_trainer.py - Background JIT LoRA Training Queue

*** NOT IMPLEMENTED. ***  No LoRA adapter is trained, and no inference path
loads one: this module is a stub that says so (``IMPLEMENTED = False``).
Neither the embedded llama-server nor Ollama applies an adapter, and
``queue_lora_training`` raises ``LoraTrainingUnavailable`` rather than
pretending. The server therefore does not queue jobs while ``IMPLEMENTED`` is
False, and the engine status API reports ``lora.implemented = false``.
"""

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger("tala.inference.lora")

#: Flip only when training AND loading an adapter both genuinely work.
IMPLEMENTED = False
STATUS_NOTE = (
    "LoRA is not implemented: nothing trains an adapter and no engine loads one."
)

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
