"""
embedded_engine.py — Tala Cognitive Engine Room: Embedded Local CPU Engine
========================================================================
Manages the download, status tracking, and CPU execution of the default
local model: Qwen/Qwen2.5-0.5B-Instruct.

Milestone 15 — Task 15.1.2
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import tqdm
from huggingface_hub import try_to_load_from_cache, snapshot_download

logger = logging.getLogger(__name__)

# Model details
MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"

# Global state for download progress
_download_status = "not_downloaded"  # "not_downloaded" | "downloading" | "ready" | "error"
_download_progress = 0.0
_download_error = ""
_lock = threading.Lock()

# Model and Tokenizer singletons
_model: Any = None
_tokenizer: Any = None


def get_status() -> tuple[str, float, str]:
    """
    NAME
           get_status - Return the current embedded model download and engine status
           
    SYNOPSIS
           get_status() -> tuple[str, float, str]
           
    DESCRIPTION
           Checks the current download status of the embedded model, and verifies
           if at least one local inference engine package is installed.
           If the model is cached but no engine is installed, returns an error status.
    """
    with _lock:
        status = _download_status
        error = _download_error
        
        if status == "ready":
            from python.inference.engine_manager import is_engine_installed
            any_installed = is_engine_installed("transformers") or is_engine_installed("vllm") or is_engine_installed("llamacpp")
            if not any_installed:
                return "error", 100.0, "No embedded inference engines are installed. Please install one from Settings."
                
        return status, _download_progress, error


def set_status(status: str, progress: float = 0.0, error: str = "") -> None:
    """Set the current download status in a thread-safe manner."""
    with _lock:
        global _download_status, _download_progress, _download_error
        _download_status = status
        _download_progress = round(progress, 1)
        _download_error = error


def check_model_cached() -> bool:
    """Verify if the default embedded model config file is cached locally."""
    try:
        path = try_to_load_from_cache(MODEL_ID, "config.json")
        return path is not None and isinstance(path, str)
    except Exception as exc:
        logger.debug("Error checking cache for model %s: %s", MODEL_ID, exc)
        return False


class DBProgressTracker(tqdm.tqdm):
    """
    Custom tqdm progress tracker that intercept updates during snapshot_download
    and stores the progress percentage in the global state.
    """
    _active_bars: dict[int, dict[str, Any]] = {}
    _bar_lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with self._bar_lock:
            desc = kwargs.get("desc", "") or ""
            self._active_bars[id(self)] = {
                "desc": desc,
                "n": 0,
                "total": kwargs.get("total", 0) or 0
            }

    def update(self, n=1):
        super().update(n)
        with self._bar_lock:
            if id(self) in self._active_bars:
                self._active_bars[id(self)]["n"] = self.n

            # Aggregate total progress across all active bars
            total_bytes = 0
            downloaded_bytes = 0
            for bar in self._active_bars.values():
                if bar["total"] > 0:
                    total_bytes += bar["total"]
                    downloaded_bytes += bar["n"]

            if total_bytes > 0:
                # Clamp to 99% until complete
                pct = min(99.0, (downloaded_bytes / total_bytes) * 100)
                set_status("downloading", progress=pct)

    def close(self):
        super().close()
        with self._bar_lock:
            if id(self) in self._active_bars:
                del self._active_bars[id(self)]


def _download_task(main_loop: asyncio.AbstractEventLoop | None) -> None:
    """Background thread target to execute model file downloads."""
    from python.harness import db

    def run_db(coro) -> None:
        if main_loop is not None and main_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, main_loop)
            try:
                future.result(timeout=5.0)
            except Exception as e:
                logger.warning("Failed to run DB operation on main loop: %s", e)
        else:
            logger.warning("Main loop not running; skipping DB update.")

    try:
        set_status("downloading", progress=0.0)
        
        run_db(db.log_to_db("INFO", f"Starting background download of embedded model {MODEL_ID}", {}))
        run_db(db.execute("UPDATE inference_providers SET status = 'downloading' WHERE id = 'embedded-builtin'"))

        # Run huggingface download
        snapshot_download(
            repo_id=MODEL_ID,
            repo_type="model",
            tqdm_class=DBProgressTracker,
            local_files_only=False,
        )

        set_status("ready", progress=100.0)
        
        run_db(db.log_to_db("INFO", f"Embedded model {MODEL_ID} download complete and ready.", {}))
        run_db(db.execute("UPDATE inference_providers SET status = 'online' WHERE id = 'embedded-builtin'"))

    except Exception as exc:
        logger.exception("Error in background model downloader")
        set_status("error", progress=0.0, error=str(exc))
        
        run_db(db.log_to_db("ERROR", f"Failed to download embedded model: {exc}", {"error": str(exc)}))
        run_db(db.execute("UPDATE inference_providers SET status = 'offline' WHERE id = 'embedded-builtin'"))


def start_download_async() -> None:
    """Triggers the async background model download thread if not already running or ready."""
    with _lock:
        if _download_status in ("downloading", "ready"):
            return
    
    # Auto-resolve the main thread's event loop
    try:
        main_loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            main_loop = asyncio.get_event_loop()
        except RuntimeError:
            main_loop = None

    thread = threading.Thread(
        target=_download_task,
        args=(main_loop,),
        name="embedded-model-downloader",
        daemon=True
    )
    thread.start()


def init_status_from_cache() -> None:
    """Initialize download state and update database based on local cache existence."""
    if check_model_cached():
        set_status("ready", progress=100.0)
    else:
        set_status("not_downloaded", progress=0.0)


def _get_model_and_tokenizer() -> tuple[Any, Any]:
    """Lazy load tokenizer and model on CPU for inference."""
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer

    from python.inference.engine_manager import is_engine_installed
    
    if is_engine_installed("transformers"):
        # Set OpenMP/MKL thread limits BEFORE importing torch to prevent CPU starvation
        try:
            import os
            import multiprocessing
            cores = max(1, min(4, multiprocessing.cpu_count() - 1))
            os.environ["OMP_NUM_THREADS"] = str(cores)
            os.environ["MKL_NUM_THREADS"] = str(cores)
        except Exception as e:
            logger.warning("Could not set OS thread limits: %s", e)
    
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM
    
        try:
            torch.set_num_threads(cores)
            logger.info("Set PyTorch CPU threads to %d to prevent event loop starvation.", cores)
        except Exception as e:
            logger.warning("Could not set PyTorch CPU thread limits: %s", e)
    
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("Loading Qwen 0.5B model using Transformers on %s...", device)
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        _model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            torch_dtype=torch.float16 if device == "cuda" else torch.float32,
            device_map="auto" if device == "cuda" else "cpu"
        )
        logger.info("Transformers embedded model loaded successfully on %s.", device)
        return _model, _tokenizer

    elif is_engine_installed("vllm"):
        from vllm import LLM
        logger.info("Loading Qwen 0.5B model using vLLM...")
        # For simplicity, we just use the LLM object as both model and tokenizer representation
        _model = LLM(model=MODEL_ID, enforce_eager=True, enable_lora=True, max_loras=1, max_lora_rank=16)
        _tokenizer = _model.get_tokenizer()
        logger.info("vLLM embedded model loaded successfully.")
        return _model, _tokenizer
        
    elif is_engine_installed("llamacpp"):
        from llama_cpp import Llama
        # Note: In a full implementation, we'd need a GGUF file here instead of MODEL_ID.
        # Since we are sticking with the default huggingface repo, llama-cpp is just a stub for now.
        raise RuntimeError("llama.cpp requires a .gguf model file. Please use transformers or vLLM.")
    else:
        raise RuntimeError("No embedded inference engines are installed. Please install one from Settings.")


def _apply_template(tokenizer, prompt, tools):
    """
    Render the chat template, passing `tools` when there are any.

    Qwen2.5-Instruct's bundled chat template natively supports tool calling: give
    it `tools` and it renders the schemas itself and instructs the model to reply
    with <tool_call>{"name": ..., "arguments": {...}}</tool_call>. That is a
    structured, template-enforced protocol -- exactly what a 0.5B can follow, and
    the opposite of asking it to reproduce a tag from prose documentation, which
    it cannot.

    This argument was simply never passed, so the embedded model received several
    thousand tokens of XML tool documentation it had no way to act on and no tool
    definitions it could. Same shape as the unreachable tool_calls converter in
    server.py: the mechanism was present and the argument was missing.

    Falls back to a tool-less render if the template does not understand `tools`
    (a non-Qwen embedded model, or an older template revision) -- raising there
    would take generation down over an optional capability.
    """
    if tools:
        try:
            return tokenizer.apply_chat_template(
                prompt, tokenize=False, add_generation_prompt=True, tools=tools,
            )
        except Exception as exc:
            logger.warning(
                "Embedded engine: chat template rejected `tools` (%s: %s); "
                "falling back to a prompt with no tool definitions",
                type(exc).__name__, exc,
            )
    return tokenizer.apply_chat_template(
        prompt, tokenize=False, add_generation_prompt=True,
    )


# The embedded model is a single shared, in-process instance. Two concurrent
# turns run `model.generate` on the same object from two background threads and
# corrupt its state: the symptom is a `probability tensor contains inf/nan`
# crash or -- worse, because it is silent -- persistent gibberish on every later
# call, since the damage outlives the request that caused it. Fan-out (a crew or
# a swarm running several agents at once) makes exactly those concurrent calls.
# Generation is therefore serialized here: turns run one at a time against the
# one model. This does not make the orchestration sequential -- callers still
# dispatch concurrently -- it just means the single local model is a shared
# resource with a queue. True token-level parallelism needs more than one
# provider (several embedded workers, or remote APIs), which this does not block.
_GENERATION_LOCK: "asyncio.Lock | None" = None


def _generation_lock() -> "asyncio.Lock":
    """The process-wide generation lock, created lazily on the running loop."""
    global _GENERATION_LOCK
    if _GENERATION_LOCK is None:
        import asyncio as _asyncio

        _GENERATION_LOCK = _asyncio.Lock()
    return _GENERATION_LOCK


async def generate_stream(
    prompt: str | list[dict[str, str]],
    max_tokens: int = 512,
    temperature: float = 0.7,
    lora_path: str | None = None,
    tools: list[dict] | None = None,
) -> AsyncIterator[str]:
    """
    Stream a completion from the local engine, one turn at a time.

    Serializing wrapper around :func:`_generate_stream_impl`. It holds a
    process-wide lock for the whole turn so concurrent callers cannot run two
    ``model.generate`` calls on the shared model at once. See _GENERATION_LOCK.
    """
    async with _generation_lock():
        async for chunk in _generate_stream_impl(
            prompt, max_tokens, temperature, lora_path=lora_path, tools=tools
        ):
            yield chunk


async def _generate_stream_impl(
    prompt: str | list[dict[str, str]],
    max_tokens: int = 512,
    temperature: float = 0.7,
    lora_path: str | None = None,
    tools: list[dict] | None = None,
) -> AsyncIterator[str]:
    """Generates a text completion stream using the installed local engine."""
    import asyncio
    import threading
    
    from python.inference.engine_manager import is_engine_installed
    
    # Initialize model if not already done
    loop = asyncio.get_running_loop()
    model, tokenizer = await loop.run_in_executor(None, _get_model_and_tokenizer)

    if is_engine_installed("vllm") and not is_engine_installed("transformers"):
        # vLLM generation
        from vllm import SamplingParams
        
        if isinstance(prompt, list):
            formatted_prompt = _apply_template(tokenizer, prompt, tools)
        else:
            formatted_prompt = prompt

        sampling_params = SamplingParams(temperature=temperature, max_tokens=max_tokens)
        
        # vLLM generate() is blocking, so we run in executor.
        # Note: vllm's AsyncLLMEngine would be better for streaming, but LLM() is synchronous.
        def _generate():
            lora_request = None
            if lora_path:
                from vllm.lora.request import LoRARequest  # type: ignore[import]
                lora_id = hash(lora_path) % 100000
                lora_request = LoRARequest(str(lora_id), lora_id, lora_path)
            return model.generate([formatted_prompt], sampling_params, use_tqdm=False, lora_request=lora_request)
            
        outputs = await loop.run_in_executor(None, _generate)
        text = outputs[0].outputs[0].text
        # Yield in one chunk for synchronous vllm
        yield text
        return

    # Transformers generation (Default fallback)
    from transformers import TextIteratorStreamer

    # Apply the LoRA adapter, if one was selected for this turn.
    #
    # This branch ignored `lora_path` entirely, and it is the branch that
    # actually runs: the vLLM path above is gated on
    # `vllm installed AND transformers NOT installed`, so whenever transformers
    # is present -- which is the normal install -- vLLM is unreachable. Combined
    # with Ollama's API having no adapter parameter at all, the result was that
    # **no LoRA has ever affected a single token of output**, on any path,
    # despite adapters being compiled and written to disk for every node.
    #
    # peft is used rather than vLLM deliberately. vLLM requires CUDA; peft +
    # transformers runs on CPU and GPU alike, which is the only option
    # compatible with "installs and works with no internet, GPU or not".
    if lora_path:
        try:
            from pathlib import Path as _Path

            if _Path(lora_path).exists():
                from peft import PeftModel

                active = getattr(model, "_tala_active_lora", None)
                if active != lora_path:
                    # Drop a previously attached adapter before swapping, or
                    # they stack silently and the output is governed by every
                    # node that has ever activated this session.
                    if hasattr(model, "unload") and active:
                        model = model.unload()
                    model = PeftModel.from_pretrained(model, lora_path)
                    model._tala_active_lora = lora_path
                    logger.info("Embedded engine: applied LoRA adapter %s", lora_path)
            else:
                logger.warning("Embedded engine: LoRA path does not exist: %s", lora_path)
        except Exception as exc:
            # Never let adapter trouble take down generation -- a missing or
            # incompatible adapter should degrade to the base model, loudly.
            logger.warning(
                "Embedded engine: could not apply LoRA %s (%s: %s); using base model",
                lora_path, type(exc).__name__, exc,
            )

    streamer = TextIteratorStreamer(
        tokenizer,
        skip_prompt=True,
        skip_special_tokens=True,
    )

    if isinstance(prompt, list):
        formatted_prompt = _apply_template(tokenizer, prompt, tools)
    else:
        formatted_prompt = prompt

    inputs = tokenizer(formatted_prompt, return_tensors="pt").to(model.device)
    input_ids = inputs["input_ids"]
    attention_mask = inputs.get("attention_mask")

    gen_kwargs = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "max_new_tokens": max_tokens,
        "temperature": max(temperature, 1e-4),
        "do_sample": temperature > 0.01,
        "streamer": streamer,
        "pad_token_id": tokenizer.eos_token_id,
    }

    exception_holder: list[Exception] = []

    def safe_generate() -> None:
        try:
            model.generate(**gen_kwargs)
        except Exception as exc:
            exception_holder.append(exc)
            logger.exception("Error in background generation thread")
        finally:
            streamer.end()

    thread = threading.Thread(
        target=safe_generate,
        daemon=True,
    )
    thread.start()

    while True:
        try:
            text_chunk = await asyncio.to_thread(lambda: next(streamer, None))
            if text_chunk is None:
                break
            if text_chunk:
                yield text_chunk
        except Exception as exc:
            logger.exception("Error during streamer extraction: %s", exc)
            break

    await loop.run_in_executor(None, thread.join)
    if exception_holder:
        raise exception_holder[0]

