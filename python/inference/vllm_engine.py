"""
vllm_engine.py — Tala Cognitive Engine Room: vLLM Inference Wrapper
====================================================================
Wraps the vLLM ``AsyncLLMEngine`` with:

  - Hardware auto-detection: CUDA → Apple Silicon (MPS) → CPU fallback
  - Graceful fallback to ``transformers`` pipeline on CPU if vLLM is absent
  - Mandatory token gate call via :mod:`harness.token_counter` before
    any generation (enforced at the server level; this module does NOT
    double-deduct)
  - Streaming output via ``AsyncIterator[str]``
  - Structured startup logging to the ``agent_log`` PostgreSQL table
  - Model name and tensor parallelism sourced from :class:`Config`

Device priority order
---------------------
1. CUDA (NVIDIA GPU) — vLLM AsyncLLMEngine with PagedAttention, fp16
2. MPS  (Apple Silicon) — transformers pipeline on mps device
3. CPU  — transformers pipeline, fp32, slow-inference WARNING

Milestone 1.3 (partial) — vLLM inference wrapper
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """
    Hardware profile for the inference engine.

    Attributes
    ----------
    device_type:
        One of ``'cuda'``, ``'mps'``, or ``'cpu'``.
    device_name:
        Human-readable device string (e.g. ``'NVIDIA RTX 4090'`` or ``'cpu'``).
    vram_gb:
        Total VRAM in gigabytes, or ``None`` for CPU.
    recommended_max_tokens:
        Conservative token budget appropriate for the detected hardware.
    """

    device_type: str   # 'cuda' | 'mps' | 'cpu'
    device_name: str
    vram_gb: float | None
    recommended_max_tokens: int


_device_info: DeviceInfo | None = None

def _get_device_info() -> DeviceInfo:
    """
    Auto-detect the best available compute device.

    Checks in order: CUDA → MPS → CPU.  Never raises; always returns a
    valid :class:`DeviceInfo`.
    """
    global _device_info
    if _device_info is not None:
        return _device_info

    try:
        import torch

        # -- CUDA (NVIDIA) ------------------------------------------------
        if torch.cuda.is_available():
            idx = torch.cuda.current_device()
            name = torch.cuda.get_device_name(idx)
            vram_bytes = torch.cuda.get_device_properties(idx).total_memory
            vram_gb = round(vram_bytes / (1024 ** 3), 2)
            # Conservative token budget: ~2k tokens per GB VRAM for 7B models
            recommended = min(4096, max(512, int(vram_gb * 500)))
            _device_info = DeviceInfo(
                device_type="cuda",
                device_name=name,
                vram_gb=vram_gb,
                recommended_max_tokens=recommended,
            )
        # -- MPS (Apple Silicon) ------------------------------------------
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            import platform
            machine = platform.machine()
            _device_info = DeviceInfo(
                device_type="mps",
                device_name=f"Apple Silicon ({machine})",
                vram_gb=None,   # Unified memory — not separately reported
                recommended_max_tokens=1024,
            )

    except ImportError:
        pass  # torch not installed — fall through to CPU stub

    if _device_info is None:
        # -- CPU fallback -------------------------------------------------------
        import platform
        cpu_name = platform.processor() or platform.machine() or "Unknown CPU"
        _device_info = DeviceInfo(
            device_type="cpu",
            device_name=cpu_name,
            vram_gb=None,
            recommended_max_tokens=256,
        )

    logger.info(
        "Detected compute device: type=%s  name=%s  vram=%s GB  max_tokens=%d",
        _device_info.device_type,
        _device_info.device_name,
        _device_info.vram_gb,
        _device_info.recommended_max_tokens,
    )

    if _device_info.device_type == "cpu":
        logger.warning(
            "No GPU detected — inference will run on CPU.  "
            "This is extremely slow for large language models.  "
            "Install CUDA drivers and a compatible GPU for production use."
        )

    return _device_info


class DeviceInfoProxy:
    @property
    def _info(self) -> DeviceInfo:
        return _get_device_info()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._info, name)

    def __repr__(self) -> str:
        return repr(self._info)


DEVICE_INFO: Any = DeviceInfoProxy()


# ---------------------------------------------------------------------------
# Lazy vLLM and Transformers Import Functions
# ---------------------------------------------------------------------------

_VLLM_AVAILABLE: bool | None = None
_AsyncLLMEngine: Any = None
_SamplingParams: Any = None
_AsyncEngineArgs: Any = None

def is_vllm_available() -> bool:
    global _VLLM_AVAILABLE, _AsyncLLMEngine, _SamplingParams, _AsyncEngineArgs
    if _VLLM_AVAILABLE is not None:
        return _VLLM_AVAILABLE

    device = _get_device_info()
    if device.device_type == "cuda":
        try:
            from vllm import AsyncLLMEngine, SamplingParams  # type: ignore[import]
            from vllm.engine.arg_utils import AsyncEngineArgs  # type: ignore[import]

            _AsyncLLMEngine = AsyncLLMEngine
            _SamplingParams = SamplingParams
            _AsyncEngineArgs = AsyncEngineArgs
            _VLLM_AVAILABLE = True
            logger.info("vLLM imported successfully — PagedAttention GPU inference active")
            return True
        except ImportError:
            logger.warning(
                "CUDA GPU detected but vLLM is not installed.  "
                "Falling back to transformers pipeline.  "
                "Install vLLM for maximum throughput:  pip install vllm"
            )
    
    _VLLM_AVAILABLE = False
    return False


_TRANSFORMERS_AVAILABLE: bool | None = None
_transformers_pipeline: Any = None

def _is_transformers_available() -> bool:
    global _TRANSFORMERS_AVAILABLE, _transformers_pipeline
    if _TRANSFORMERS_AVAILABLE is not None:
        return _TRANSFORMERS_AVAILABLE

    if is_vllm_available():
        _TRANSFORMERS_AVAILABLE = False
        return False

    try:
        import transformers  # type: ignore[import]
        _TRANSFORMERS_AVAILABLE = True
        logger.info(
            "transformers %s available — will use pipeline for inference on %s",
            transformers.__version__,
            _get_device_info().device_type,
        )
        return True
    except ImportError:
        logger.warning(
            "Neither vLLM nor transformers is installed.  "
            "Inference engine will run in ECHO STUB mode.  "
            "Install transformers:  pip install transformers"
        )
    
    _TRANSFORMERS_AVAILABLE = False
    return False


# ---------------------------------------------------------------------------
# Engine singletons
# ---------------------------------------------------------------------------

import threading
_vllm_engine: Any = None           # AsyncLLMEngine, lazily initialised
_hf_pipeline: Any = None           # HuggingFace pipeline, lazily initialised
_hf_pipeline_lock = threading.Lock()


async def _get_vllm_engine() -> Any:
    """Lazily create and return the vLLM AsyncLLMEngine singleton."""
    global _vllm_engine

    if _vllm_engine is not None:
        return _vllm_engine

    from python.harness.config import settings  # type: ignore[import]

    logger.info(
        "Initialising vLLM AsyncLLMEngine  model=%s  tensor_parallel=%d  dtype=float16",
        settings.VLLM_MODEL,
        settings.VLLM_TENSOR_PARALLEL_SIZE,
    )

    engine_args = _AsyncEngineArgs(
        model=settings.VLLM_MODEL,
        tensor_parallel_size=settings.VLLM_TENSOR_PARALLEL_SIZE,
        dtype="float16",          # fp16 on CUDA for efficiency
        device="cuda",
        trust_remote_code=True,
        enable_lora=True,
        max_loras=1,
        max_lora_rank=16,
    )

    _vllm_engine = _AsyncLLMEngine.from_engine_args(engine_args)
    logger.info("vLLM AsyncLLMEngine initialised (PagedAttention active)")
    return _vllm_engine


def _get_hf_pipeline() -> Any:
    """Lazily create and return the HuggingFace text-generation pipeline."""
    global _hf_pipeline

    with _hf_pipeline_lock:
        if _hf_pipeline is not None:
            return _hf_pipeline

        if not _TRANSFORMERS_AVAILABLE:
            return None

        from python.harness.config import settings  # type: ignore[import]
        import torch  # type: ignore[import]
        from transformers import pipeline  # type: ignore[import]

        device_map: str | int
        dtype_arg: Any

        if DEVICE_INFO.device_type == "cuda":
            device_map = "cuda"
            dtype_arg = torch.float16
        elif DEVICE_INFO.device_type == "mps":
            device_map = "mps"
            dtype_arg = torch.float16
        else:
            device_map = "cpu"
            dtype_arg = torch.float32
            
            # Set PyTorch threads to avoid CPU starvation of the asyncio event loop
            try:
                import os
                import multiprocessing
                cores = max(1, min(4, multiprocessing.cpu_count() - 1))
                torch.set_num_threads(cores)
                os.environ["OMP_NUM_THREADS"] = str(cores)
                os.environ["MKL_NUM_THREADS"] = str(cores)
                logger.info("Set PyTorch CPU threads to %d to prevent event loop starvation.", cores)
            except Exception as e:
                logger.warning("Could not set PyTorch CPU thread limits: %s", e)

        # For CPU fallback, force local_files_only=True to prevent blocking downloads of 15GB models
        local_files_only = (DEVICE_INFO.device_type == "cpu")

        logger.info(
            "Initialising HuggingFace pipeline  model=%s  device=%s  dtype=%s  local_files_only=%s",
            settings.VLLM_MODEL,
            DEVICE_INFO.device_type,
            dtype_arg,
            local_files_only,
        )

        _hf_pipeline = pipeline(
            task="text-generation",
            model=settings.VLLM_MODEL,
            device_map=device_map,
            torch_dtype=dtype_arg,
            trust_remote_code=True,
            local_files_only=local_files_only,
        )

        logger.info("HuggingFace pipeline initialised on %s", DEVICE_INFO.device_type)
        return _hf_pipeline


# ---------------------------------------------------------------------------
# Public generate API
# ---------------------------------------------------------------------------

async def generate(
    prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.7,
    session_id: str | None = None,
    top_p: float = 0.95,
    top_k: int = -1,
    stop: list[str] | None = None,
    lora_path: str | None = None,
) -> AsyncIterator[str]:
    """
    Generate a completion for *prompt*, streaming output token-by-token.

    Engine selection order:
      1. vLLM AsyncLLMEngine   — if vLLM installed and CUDA present
      2. HuggingFace pipeline  — if transformers installed (any device)
      3. Echo stub             — if neither is available (dev fallback)

    The token gate is applied by the server *before* calling this function.
    This function does NOT call it again to avoid double-deduction.

    Parameters
    ----------
    prompt:
        Full text prompt.
    max_tokens:
        Maximum tokens to generate.
    temperature:
        Sampling temperature.
    session_id:
        Tracing / ledger identifier. Auto-generated if not provided.
    top_p:
        Nucleus sampling probability mass.
    top_k:
        Top-K sampling (-1 = disabled).
    stop:
        Optional list of stop strings.

    Yields
    ------
    str
        Text chunks as they become available.

    Raises
    ------
    InsufficientTokenBalanceError
        (Raised upstream by the server before this is called.)
    RuntimeError
        If engine initialisation fails.
    """
    if session_id is None:
        session_id = str(uuid.uuid4())

    from python.harness import db  # type: ignore[import]

    await db.log_to_db(
        "INFO",
        f"vllm_engine.generate start  session={session_id}  device={DEVICE_INFO.device_type}",
        {
            "session_id": session_id,
            "prompt_length": len(prompt),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "device_type": DEVICE_INFO.device_type,
            "device_name": DEVICE_INFO.device_name,
            "vllm_available": is_vllm_available(),
            "transformers_available": _is_transformers_available(),
        },
    )

    try:
        if is_vllm_available():
            async for chunk in _generate_vllm(prompt, max_tokens, temperature, session_id, top_p, top_k, stop, lora_path):
                yield chunk
        elif _is_transformers_available():
            async for chunk in _generate_transformers(prompt, max_tokens, temperature, session_id):
                yield chunk
        else:
            for chunk in _stub_generate(prompt, max_tokens):
                yield chunk
    except Exception as exc:
        logger.exception("generate error  session=%s", session_id)
        await db.log_to_db(
            "ERROR",
            f"vllm_engine.generate error: {exc}",
            {"session_id": session_id, "error": str(exc)},
        )
        raise

    await db.log_to_db(
        "INFO",
        f"vllm_engine.generate complete  session={session_id}",
        {"session_id": session_id},
    )


# ---------------------------------------------------------------------------
# Engine-specific internal generators
# ---------------------------------------------------------------------------

async def _generate_vllm(
    prompt: str,
    max_tokens: int,
    temperature: float,
    session_id: str,
    top_p: float,
    top_k: int,
    stop: list[str] | None,
    lora_path: str | None = None,
) -> AsyncIterator[str]:
    """Stream tokens from vLLM AsyncLLMEngine."""
    engine = await _get_vllm_engine()
    
    lora_request = None
    if lora_path:
        from vllm.lora.request import LoRARequest  # type: ignore[import]
        # Generate a unique LoRA ID for the request
        lora_id = hash(lora_path) % 100000
        lora_request = LoRARequest(str(lora_id), lora_id, lora_path)

    sampling_params = _SamplingParams(
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k if top_k > 0 else None,
        stop=stop or [],
    )

    request_id = f"{session_id}-{uuid.uuid4().hex[:8]}"
    previous_text = ""

    async for request_output in engine.generate(
        prompt=prompt,
        sampling_params=sampling_params,
        request_id=request_id,
        lora_request=lora_request,
    ):
        for output in request_output.outputs:
            new_text = output.text[len(previous_text):]
            if new_text:
                previous_text = output.text
                yield new_text


async def _generate_transformers(
    prompt: str,
    max_tokens: int,
    temperature: float,
    session_id: str,
) -> AsyncIterator[str]:
    """
    Run HuggingFace text-generation pipeline in a thread pool executor and
    yield the result as a single chunk (transformers pipeline is not natively
    streaming-friendly without TextIteratorStreamer, which is added here).
    """
    import threading

    loop = asyncio.get_running_loop()
    try:
        pipe = await loop.run_in_executor(None, _get_hf_pipeline)
    except Exception as exc:
        logger.warning("Failed to initialize HuggingFace pipeline, falling back to stub: %s", exc)
        pipe = None

    if pipe is None:
        for chunk in _stub_generate(prompt, max_tokens):
            yield chunk
        return

    # Use TextIteratorStreamer for live token-by-token output
    try:
        from transformers import TextIteratorStreamer  # type: ignore[import]

        streamer = TextIteratorStreamer(
            pipe.tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
        )

        gen_kwargs = {
            "text_inputs": prompt,
            "max_new_tokens": max_tokens,
            "temperature": max(temperature, 1e-4),  # transformers rejects 0.0
            "do_sample": temperature > 0.01,
            "streamer": streamer,
        }

        exception_holder: list[Exception] = []

        def safe_generate() -> None:
            try:
                pipe(**gen_kwargs)
            except Exception as exc:
                exception_holder.append(exc)
                logger.exception("Error in background transformers generation thread")
            finally:
                streamer.end()

        loop = asyncio.get_running_loop()
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
                logger.exception("Error during transformers streamer extraction: %s", exc)
                break

        # Ensure thread completes
        await loop.run_in_executor(None, thread.join)
        if exception_holder:
            raise exception_holder[0]

    except ImportError:
        # TextIteratorStreamer unavailable — fall back to single-shot generation
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: pipe(
                prompt,
                max_new_tokens=max_tokens,
                temperature=max(temperature, 1e-4),
                do_sample=temperature > 0.01,
            ),
        )
        generated = result[0]["generated_text"][len(prompt):]
        yield generated


# ---------------------------------------------------------------------------
# Echo stub (neither vLLM nor transformers available)
# ---------------------------------------------------------------------------

def _stub_generate(prompt: str, max_tokens: int) -> list[str]:
    """
    Return stub text chunks when no real inference backend is available.

    Clearly marked so developers know they are in stub mode.
    """
    stub_text = (
        f"[TALA STUB — no inference backend installed]\n"
        f"Device: {DEVICE_INFO.device_type} ({DEVICE_INFO.device_name})\n"
        f"Install vLLM (CUDA) or transformers for real inference.\n\n"
        f"Echo (first 500 chars): {prompt[:500]}{'…' if len(prompt) > 500 else ''}"
    )
    return [word + " " for word in stub_text.split(" ")]


# ---------------------------------------------------------------------------
# Public utilities
# ---------------------------------------------------------------------------

async def get_model_info() -> dict[str, Any]:
    """
    Return metadata about the configured model and hardware.

    Safe to call at any time; never raises.
    """
    from python.harness.config import settings  # type: ignore[import]

    info: dict[str, Any] = {
        "model": settings.VLLM_MODEL,
        "tensor_parallel_size": settings.VLLM_TENSOR_PARALLEL_SIZE,
        "device_type": DEVICE_INFO.device_type,
        "device_name": DEVICE_INFO.device_name,
        "vram_gb": DEVICE_INFO.vram_gb,
        "recommended_max_tokens": DEVICE_INFO.recommended_max_tokens,
        "vllm_available": is_vllm_available(),
        "transformers_available": _is_transformers_available(),
        "mode": (
            "vllm" if is_vllm_available() else
            "transformers" if _is_transformers_available() else
            "stub"
        ),
    }

    if is_vllm_available() and _vllm_engine is not None:
        try:
            model_config = await _vllm_engine.get_model_config()
            info.update({
                "max_model_len": model_config.max_model_len,
                "dtype": str(model_config.dtype),
            })
        except Exception as exc:  # noqa: BLE001
            info["vllm_config_error"] = str(exc)

    return info


async def log_device_info_to_db() -> None:
    """
    Write the detected device info to the ``agent_log`` table.

    Called once during server startup.
    """
    try:
        from python.harness import db  # type: ignore[import]
        await db.log_to_db(
            "INFO",
            f"Inference engine startup: device={DEVICE_INFO.device_type} "
            f"name={DEVICE_INFO.device_name} vram={DEVICE_INFO.vram_gb} GB",
            {
                "device_type": DEVICE_INFO.device_type,
                "device_name": DEVICE_INFO.device_name,
                "vram_gb": DEVICE_INFO.vram_gb,
                "recommended_max_tokens": DEVICE_INFO.recommended_max_tokens,
                "vllm_available": is_vllm_available(),
                "transformers_available": _is_transformers_available(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not log device info to DB: %s", exc)
