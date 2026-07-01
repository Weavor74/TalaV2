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
    """Return the current download status: (status, progress_percent, error_message)."""
    with _lock:
        return _download_status, _download_progress, _download_error


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
        _model = LLM(model=MODEL_ID, enforce_eager=True)
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


async def generate_stream(
    prompt: str | list[dict[str, str]],
    max_tokens: int = 512,
    temperature: float = 0.7,
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
            formatted_prompt = tokenizer.apply_chat_template(
                prompt,
                tokenize=False,
                add_generation_prompt=True,
            )
        else:
            formatted_prompt = prompt
            
        sampling_params = SamplingParams(temperature=temperature, max_tokens=max_tokens)
        
        # vLLM generate() is blocking, so we run in executor.
        # Note: vllm's AsyncLLMEngine would be better for streaming, but LLM() is synchronous.
        def _generate():
            return model.generate([formatted_prompt], sampling_params, use_tqdm=False)
            
        outputs = await loop.run_in_executor(None, _generate)
        text = outputs[0].outputs[0].text
        # Yield in one chunk for synchronous vllm
        yield text
        return

    # Transformers generation (Default fallback)
    from transformers import TextIteratorStreamer

    streamer = TextIteratorStreamer(
        tokenizer,
        skip_prompt=True,
        skip_special_tokens=True,
    )

    if isinstance(prompt, list):
        formatted_prompt = tokenizer.apply_chat_template(
            prompt,
            tokenize=False,
            add_generation_prompt=True,
        )
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

