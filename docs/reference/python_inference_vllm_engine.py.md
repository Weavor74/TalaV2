# File: `vllm_engine.py`

## Module Description

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

## Classes

### `class DeviceInfo`
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


### `class DeviceInfoProxy`
- **`def _info(self)`**
- **`def __getattr__(self, name)`**
- **`def __repr__(self)`**

## Functions

### `def get_device_info()`
Auto-detect the best available compute device.

Checks in order: CUDA → MPS → CPU.  Never raises; always returns a
valid :class:`DeviceInfo`.

### `def is_vllm_available()`
*No docstring provided.*

### `def is_transformers_available()`
*No docstring provided.*

### `async def _get_vllm_engine()`
Lazily create and return the vLLM AsyncLLMEngine singleton.

### `def _get_hf_pipeline()`
Lazily create and return the HuggingFace text-generation pipeline.

### `async def generate(prompt, max_tokens, temperature, session_id, top_p, top_k, stop)`
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

### `async def _generate_vllm(prompt, max_tokens, temperature, session_id, top_p, top_k, stop)`
Stream tokens from vLLM AsyncLLMEngine.

### `async def _generate_transformers(prompt, max_tokens, temperature, session_id)`
Run HuggingFace text-generation pipeline in a thread pool executor and
yield the result as a single chunk (transformers pipeline is not natively
streaming-friendly without TextIteratorStreamer, which is added here).

### `def _stub_generate(prompt, max_tokens)`
Return stub text chunks when no real inference backend is available.

Clearly marked so developers know they are in stub mode.

### `async def get_model_info()`
Return metadata about the configured model and hardware.

Safe to call at any time; never raises.

### `async def log_device_info_to_db()`
Write the detected device info to the ``agent_log`` table.

Called once during server startup.

## Source Code

```python
"""\nvllm_engine.py — Tala Cognitive Engine Room: vLLM Inference Wrapper\n====================================================================\nWraps the vLLM ``AsyncLLMEngine`` with:\n\n  - Hardware auto-detection: CUDA → Apple Silicon (MPS) → CPU fallback\n  - Graceful fallback to ``transformers`` pipeline on CPU if vLLM is absent\n  - Mandatory token gate call via :mod:`harness.token_counter` before\n    any generation (enforced at the server level; this module does NOT\n    double-deduct)\n  - Streaming output via ``AsyncIterator[str]``\n  - Structured startup logging to the ``agent_log`` PostgreSQL table\n  - Model name and tensor parallelism sourced from :class:`Config`\n\nDevice priority order\n---------------------\n1. CUDA (NVIDIA GPU) — vLLM AsyncLLMEngine with PagedAttention, fp16\n2. MPS  (Apple Silicon) — transformers pipeline on mps device\n3. CPU  — transformers pipeline, fp32, slow-inference WARNING\n\nMilestone 1.3 (partial) — vLLM inference wrapper\n"""\n\nfrom __future__ import annotations\n\nimport asyncio\nimport logging\nimport uuid\nfrom collections.abc import AsyncIterator\nfrom dataclasses import dataclass\nfrom typing import Any\n\nlogger = logging.getLogger(__name__)\n\n\n# ---------------------------------------------------------------------------\n# Hardware detection\n# ---------------------------------------------------------------------------\n\n@dataclass(frozen=True, slots=True)\nclass DeviceInfo:\n    """\n    Hardware profile for the inference engine.\n\n    Attributes\n    ----------\n    device_type:\n        One of ``'cuda'``, ``'mps'``, or ``'cpu'``.\n    device_name:\n        Human-readable device string (e.g. ``'NVIDIA RTX 4090'`` or ``'cpu'``).\n    vram_gb:\n        Total VRAM in gigabytes, or ``None`` for CPU.\n    recommended_max_tokens:\n        Conservative token budget appropriate for the detected hardware.\n    """\n\n    device_type: str   # 'cuda' | 'mps' | 'cpu'\n    device_name: str\n    vram_gb: float | None\n    recommended_max_tokens: int\n\n\n_device_info: DeviceInfo | None = None\n\ndef get_device_info() -> DeviceInfo:\n    """\n    Auto-detect the best available compute device.\n\n    Checks in order: CUDA → MPS → CPU.  Never raises; always returns a\n    valid :class:`DeviceInfo`.\n    """\n    global _device_info\n    if _device_info is not None:\n        return _device_info\n\n    try:\n        import torch\n\n        # -- CUDA (NVIDIA) ------------------------------------------------\n        if torch.cuda.is_available():\n            idx = torch.cuda.current_device()\n            name = torch.cuda.get_device_name(idx)\n            vram_bytes = torch.cuda.get_device_properties(idx).total_memory\n            vram_gb = round(vram_bytes / (1024 ** 3), 2)\n            # Conservative token budget: ~2k tokens per GB VRAM for 7B models\n            recommended = min(4096, max(512, int(vram_gb * 500)))\n            _device_info = DeviceInfo(\n                device_type="cuda",\n                device_name=name,\n                vram_gb=vram_gb,\n                recommended_max_tokens=recommended,\n            )\n        # -- MPS (Apple Silicon) ------------------------------------------\n        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():\n            import platform\n            machine = platform.machine()\n            _device_info = DeviceInfo(\n                device_type="mps",\n                device_name=f"Apple Silicon ({machine})",\n                vram_gb=None,   # Unified memory — not separately reported\n                recommended_max_tokens=1024,\n            )\n\n    except ImportError:\n        pass  # torch not installed — fall through to CPU stub\n\n    if _device_info is None:\n        # -- CPU fallback -------------------------------------------------------\n        import platform\n        cpu_name = platform.processor() or platform.machine() or "Unknown CPU"\n        _device_info = DeviceInfo(\n            device_type="cpu",\n            device_name=cpu_name,\n            vram_gb=None,\n            recommended_max_tokens=256,\n        )\n\n    logger.info(\n        "Detected compute device: type=%s  name=%s  vram=%s GB  max_tokens=%d",\n        _device_info.device_type,\n        _device_info.device_name,\n        _device_info.vram_gb,\n        _device_info.recommended_max_tokens,\n    )\n\n    if _device_info.device_type == "cpu":\n        logger.warning(\n            "No GPU detected — inference will run on CPU.  "\n            "This is extremely slow for large language models.  "\n            "Install CUDA drivers and a compatible GPU for production use."\n        )\n\n    return _device_info\n\n\nclass DeviceInfoProxy:\n    @property\n    def _info(self) -> DeviceInfo:\n        return get_device_info()\n\n    def __getattr__(self, name: str) -> Any:\n        return getattr(self._info, name)\n\n    def __repr__(self) -> str:\n        return repr(self._info)\n\n\nDEVICE_INFO: Any = DeviceInfoProxy()\n\n\n# ---------------------------------------------------------------------------\n# Lazy vLLM and Transformers Import Functions\n# ---------------------------------------------------------------------------\n\n_VLLM_AVAILABLE: bool | None = None\n_AsyncLLMEngine: Any = None\n_SamplingParams: Any = None\n_AsyncEngineArgs: Any = None\n\ndef is_vllm_available() -> bool:\n    global _VLLM_AVAILABLE, _AsyncLLMEngine, _SamplingParams, _AsyncEngineArgs\n    if _VLLM_AVAILABLE is not None:\n        return _VLLM_AVAILABLE\n\n    device = get_device_info()\n    if device.device_type == "cuda":\n        try:\n            from vllm import AsyncLLMEngine, SamplingParams  # type: ignore[import]\n            from vllm.engine.arg_utils import AsyncEngineArgs  # type: ignore[import]\n\n            _AsyncLLMEngine = AsyncLLMEngine\n            _SamplingParams = SamplingParams\n            _AsyncEngineArgs = AsyncEngineArgs\n            _VLLM_AVAILABLE = True\n            logger.info("vLLM imported successfully — PagedAttention GPU inference active")\n            return True\n        except ImportError:\n            logger.warning(\n                "CUDA GPU detected but vLLM is not installed.  "\n                "Falling back to transformers pipeline.  "\n                "Install vLLM for maximum throughput:  pip install vllm"\n            )\n    \n    _VLLM_AVAILABLE = False\n    return False\n\n\n_TRANSFORMERS_AVAILABLE: bool | None = None\n_transformers_pipeline: Any = None\n\ndef is_transformers_available() -> bool:\n    global _TRANSFORMERS_AVAILABLE, _transformers_pipeline\n    if _TRANSFORMERS_AVAILABLE is not None:\n        return _TRANSFORMERS_AVAILABLE\n\n    if is_vllm_available():\n        _TRANSFORMERS_AVAILABLE = False\n        return False\n\n    try:\n        import transformers  # type: ignore[import]\n        _TRANSFORMERS_AVAILABLE = True\n        logger.info(\n            "transformers %s available — will use pipeline for inference on %s",\n            transformers.__version__,\n            get_device_info().device_type,\n        )\n        return True\n    except ImportError:\n        logger.warning(\n            "Neither vLLM nor transformers is installed.  "\n            "Inference engine will run in ECHO STUB mode.  "\n            "Install transformers:  pip install transformers"\n        )\n    \n    _TRANSFORMERS_AVAILABLE = False\n    return False\n\n\n# ---------------------------------------------------------------------------\n# Engine singletons\n# ---------------------------------------------------------------------------\n\nimport threading\n_vllm_engine: Any = None           # AsyncLLMEngine, lazily initialised\n_hf_pipeline: Any = None           # HuggingFace pipeline, lazily initialised\n_hf_pipeline_lock = threading.Lock()\n\n\nasync def _get_vllm_engine() -> Any:\n    """Lazily create and return the vLLM AsyncLLMEngine singleton."""\n    global _vllm_engine\n\n    if _vllm_engine is not None:\n        return _vllm_engine\n\n    from python.harness.config import settings  # type: ignore[import]\n\n    logger.info(\n        "Initialising vLLM AsyncLLMEngine  model=%s  tensor_parallel=%d  dtype=float16",\n        settings.VLLM_MODEL,\n        settings.VLLM_TENSOR_PARALLEL_SIZE,\n    )\n\n    engine_args = _AsyncEngineArgs(\n        model=settings.VLLM_MODEL,\n        tensor_parallel_size=settings.VLLM_TENSOR_PARALLEL_SIZE,\n        dtype="float16",          # fp16 on CUDA for efficiency\n        device="cuda",\n        trust_remote_code=True,\n    )\n\n    _vllm_engine = _AsyncLLMEngine.from_engine_args(engine_args)\n    logger.info("vLLM AsyncLLMEngine initialised (PagedAttention active)")\n    return _vllm_engine\n\n\ndef _get_hf_pipeline() -> Any:\n    """Lazily create and return the HuggingFace text-generation pipeline."""\n    global _hf_pipeline\n\n    with _hf_pipeline_lock:\n        if _hf_pipeline is not None:\n            return _hf_pipeline\n\n        if not _TRANSFORMERS_AVAILABLE:\n            return None\n\n        from python.harness.config import settings  # type: ignore[import]\n        import torch  # type: ignore[import]\n        from transformers import pipeline  # type: ignore[import]\n\n        device_map: str | int\n        dtype_arg: Any\n\n        if DEVICE_INFO.device_type == "cuda":\n            device_map = "cuda"\n            dtype_arg = torch.float16\n        elif DEVICE_INFO.device_type == "mps":\n            device_map = "mps"\n            dtype_arg = torch.float16\n        else:\n            device_map = "cpu"\n            dtype_arg = torch.float32\n            \n            # Set PyTorch threads to avoid CPU starvation of the asyncio event loop\n            try:\n                import os\n                import multiprocessing\n                cores = max(1, min(4, multiprocessing.cpu_count() - 1))\n                torch.set_num_threads(cores)\n                os.environ["OMP_NUM_THREADS"] = str(cores)\n                os.environ["MKL_NUM_THREADS"] = str(cores)\n                logger.info("Set PyTorch CPU threads to %d to prevent event loop starvation.", cores)\n            except Exception as e:\n                logger.warning("Could not set PyTorch CPU thread limits: %s", e)\n\n        # For CPU fallback, force local_files_only=True to prevent blocking downloads of 15GB models\n        local_files_only = (DEVICE_INFO.device_type == "cpu")\n\n        logger.info(\n            "Initialising HuggingFace pipeline  model=%s  device=%s  dtype=%s  local_files_only=%s",\n            settings.VLLM_MODEL,\n            DEVICE_INFO.device_type,\n            dtype_arg,\n            local_files_only,\n        )\n\n        _hf_pipeline = pipeline(\n            task="text-generation",\n            model=settings.VLLM_MODEL,\n            device_map=device_map,\n            torch_dtype=dtype_arg,\n            trust_remote_code=True,\n            local_files_only=local_files_only,\n        )\n\n        logger.info("HuggingFace pipeline initialised on %s", DEVICE_INFO.device_type)\n        return _hf_pipeline\n\n\n# ---------------------------------------------------------------------------\n# Public generate API\n# ---------------------------------------------------------------------------\n\nasync def generate(\n    prompt: str,\n    max_tokens: int = 512,\n    temperature: float = 0.7,\n    session_id: str | None = None,\n    top_p: float = 0.95,\n    top_k: int = -1,\n    stop: list[str] | None = None,\n) -> AsyncIterator[str]:\n    """\n    Generate a completion for *prompt*, streaming output token-by-token.\n\n    Engine selection order:\n      1. vLLM AsyncLLMEngine   — if vLLM installed and CUDA present\n      2. HuggingFace pipeline  — if transformers installed (any device)\n      3. Echo stub             — if neither is available (dev fallback)\n\n    The token gate is applied by the server *before* calling this function.\n    This function does NOT call it again to avoid double-deduction.\n\n    Parameters\n    ----------\n    prompt:\n        Full text prompt.\n    max_tokens:\n        Maximum tokens to generate.\n    temperature:\n        Sampling temperature.\n    session_id:\n        Tracing / ledger identifier. Auto-generated if not provided.\n    top_p:\n        Nucleus sampling probability mass.\n    top_k:\n        Top-K sampling (-1 = disabled).\n    stop:\n        Optional list of stop strings.\n\n    Yields\n    ------\n    str\n        Text chunks as they become available.\n\n    Raises\n    ------\n    InsufficientTokenBalanceError\n        (Raised upstream by the server before this is called.)\n    RuntimeError\n        If engine initialisation fails.\n    """\n    if session_id is None:\n        session_id = str(uuid.uuid4())\n\n    from python.harness import db  # type: ignore[import]\n\n    await db.log_to_db(\n        "INFO",\n        f"vllm_engine.generate start  session={session_id}  device={DEVICE_INFO.device_type}",\n        {\n            "session_id": session_id,\n            "prompt_length": len(prompt),\n            "max_tokens": max_tokens,\n            "temperature": temperature,\n            "device_type": DEVICE_INFO.device_type,\n            "device_name": DEVICE_INFO.device_name,\n            "vllm_available": is_vllm_available(),\n            "transformers_available": is_transformers_available(),\n        },\n    )\n\n    try:\n        if is_vllm_available():\n            async for chunk in _generate_vllm(prompt, max_tokens, temperature, session_id, top_p, top_k, stop):\n                yield chunk\n        elif is_transformers_available():\n            async for chunk in _generate_transformers(prompt, max_tokens, temperature, session_id):\n                yield chunk\n        else:\n            for chunk in _stub_generate(prompt, max_tokens):\n                yield chunk\n    except Exception as exc:\n        logger.exception("generate error  session=%s", session_id)\n        await db.log_to_db(\n            "ERROR",\n            f"vllm_engine.generate error: {exc}",\n            {"session_id": session_id, "error": str(exc)},\n        )\n        raise\n\n    await db.log_to_db(\n        "INFO",\n        f"vllm_engine.generate complete  session={session_id}",\n        {"session_id": session_id},\n    )\n\n\n# ---------------------------------------------------------------------------\n# Engine-specific internal generators\n# ---------------------------------------------------------------------------\n\nasync def _generate_vllm(\n    prompt: str,\n    max_tokens: int,\n    temperature: float,\n    session_id: str,\n    top_p: float,\n    top_k: int,\n    stop: list[str] | None,\n) -> AsyncIterator[str]:\n    """Stream tokens from vLLM AsyncLLMEngine."""\n    engine = await _get_vllm_engine()\n\n    sampling_params = _SamplingParams(\n        max_tokens=max_tokens,\n        temperature=temperature,\n        top_p=top_p,\n        top_k=top_k if top_k > 0 else None,\n        stop=stop or [],\n    )\n\n    request_id = f"{session_id}-{uuid.uuid4().hex[:8]}"\n    previous_text = ""\n\n    async for request_output in engine.generate(\n        prompt=prompt,\n        sampling_params=sampling_params,\n        request_id=request_id,\n    ):\n        for output in request_output.outputs:\n            new_text = output.text[len(previous_text):]\n            if new_text:\n                previous_text = output.text\n                yield new_text\n\n\nasync def _generate_transformers(\n    prompt: str,\n    max_tokens: int,\n    temperature: float,\n    session_id: str,\n) -> AsyncIterator[str]:\n    """\n    Run HuggingFace text-generation pipeline in a thread pool executor and\n    yield the result as a single chunk (transformers pipeline is not natively\n    streaming-friendly without TextIteratorStreamer, which is added here).\n    """\n    import asyncio\n    import threading\n\n    loop = asyncio.get_running_loop()\n    try:\n        pipe = await loop.run_in_executor(None, _get_hf_pipeline)\n    except Exception as exc:\n        logger.warning("Failed to initialize HuggingFace pipeline, falling back to stub: %s", exc)\n        pipe = None\n\n    if pipe is None:\n        for chunk in _stub_generate(prompt, max_tokens):\n            yield chunk\n        return\n\n    # Use TextIteratorStreamer for live token-by-token output\n    try:\n        from transformers import TextIteratorStreamer  # type: ignore[import]\n\n        streamer = TextIteratorStreamer(\n            pipe.tokenizer,\n            skip_prompt=True,\n            skip_special_tokens=True,\n        )\n\n        gen_kwargs = {\n            "text_inputs": prompt,\n            "max_new_tokens": max_tokens,\n            "temperature": max(temperature, 1e-4),  # transformers rejects 0.0\n            "do_sample": temperature > 0.01,\n... (file truncated, showing first 500 lines) ...
```
