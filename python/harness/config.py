"""
config.py — Tala Cognitive Engine Room: Configuration Management
================================================================
Loads all runtime configuration from a .env file and environment variables.
Validates paths, provides a typed Config dataclass, and is the single source
of truth for all harness settings.

Milestone 1.2 — Task 1.2.1
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap: locate and load .env before any other module reads os.environ
# ---------------------------------------------------------------------------

# Walk up from this file's directory to find the repo root .env
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent  # python/harness -> python -> repo root
_DOT_ENV_PATH = Path(os.environ["TALA_ENV_PATH"]).expanduser().resolve() if os.environ.get("TALA_ENV_PATH") else _REPO_ROOT / ".env"

if _DOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=_DOT_ENV_PATH, override=False)
else:
    # Fall back: try CWD
    load_dotenv(override=False)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _env(key: str, default: str | None = None, required: bool = False) -> str:
    """Read an env var, raising ValueError if required and absent."""
    value = os.environ.get(key, default)
    if required and not value:
        raise ValueError(
            f"Required environment variable '{key}' is not set. "
            f"Check your .env file or shell environment."
        )
    return value or ""


def _validate_path(raw: str, must_exist: bool = False, create: bool = False) -> Path:
    """
    Resolve *raw* to an absolute Path.  Optionally assert existence or create
    the directory tree so writes will not fail at runtime.
    """
    p = Path(raw).expanduser().resolve()
    if must_exist and not p.exists():
        raise FileNotFoundError(
            f"Required path does not exist: {p}  "
            f"Set the correct value in your .env file."
        )
    if create and not p.exists():
        p.mkdir(parents=True, exist_ok=True)
        logger.debug("Created directory: %s", p)
    return p


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Config:
    """
    Immutable runtime configuration for the Tala harness.

    All fields are populated by :func:`load_config` which reads from the
    environment (after loading .env).  Do **not** instantiate directly —
    use ``load_config()`` or the module-level singleton ``settings``.
    """

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    DB_URL: str
    """Full asyncpg connection string, e.g. postgresql://tala:tala@localhost:5432/tala"""

    # ------------------------------------------------------------------
    # vLLM inference engine
    # ------------------------------------------------------------------
    VLLM_HOST: str
    VLLM_PORT: int
    VLLM_MODEL: str
    VLLM_TENSOR_PARALLEL_SIZE: int

    # ------------------------------------------------------------------
    # WebSocket server
    # ------------------------------------------------------------------
    WS_HOST: str
    WS_PORT: int

    # ------------------------------------------------------------------
    # MCP server (exposes Tala's capability manifest + gated tool
    # execution to external MCP clients over SSE)
    # ------------------------------------------------------------------
    MCP_HOST: str
    MCP_PORT: int

    # ------------------------------------------------------------------
    # Vault (encrypted secret store path)
    # ------------------------------------------------------------------
    VAULT_PATH: Path

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str

    # ------------------------------------------------------------------
    # Token economy defaults
    # ------------------------------------------------------------------
    TOKEN_COST_PER_TOKEN: float
    """USD cost per token (default: 0.0000001, i.e. one ten-millionth)."""

    INITIAL_AI_TOKEN_BALANCE: float
    """Starting AI token balance written to wallet on first boot."""

    # ------------------------------------------------------------------
    # Environment execution context
    # ------------------------------------------------------------------
    IS_HEADLESS: bool
    """True if running in an embedded/headless system where Desktop OS UI features (screenshots, audio) are unavailable."""

    # ------------------------------------------------------------------
    # Derived / computed
    # ------------------------------------------------------------------
    REPO_ROOT: Path = field(default_factory=lambda: _REPO_ROOT)

    # Class-level marker so other modules can verify they imported real config
    _LOADED: ClassVar[bool] = False

    @property
    def vllm_base_url(self) -> str:
        """Convenience property for the vLLM OpenAI-compatible base URL."""
        return f"http://{self.VLLM_HOST}:{self.VLLM_PORT}/v1"

    @property
    def ws_uri(self) -> str:
        return f"ws://{self.WS_HOST}:{self.WS_PORT}"


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def load_config() -> Config:
    """
    Build and return a :class:`Config` instance from the current environment.

    Validates all path fields; raises :class:`ValueError` or
    :class:`FileNotFoundError` on misconfiguration.
    """
    vault_raw = _env("VAULT_PATH", default=str(_REPO_ROOT / "vault"))
    vault_path = _validate_path(vault_raw, must_exist=False, create=True)

    log_level = _env("LOG_LEVEL", default="INFO").upper()
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in valid_levels:
        raise ValueError(
            f"LOG_LEVEL must be one of {valid_levels}, got: {log_level!r}"
        )

    cfg = Config(
        DB_URL=_env(
            "TALA_DB_URL",
            default="postgresql://tala:tala@localhost:5432/tala",
        ),
        VLLM_HOST=_env("VLLM_HOST", default="127.0.0.1"),
        VLLM_PORT=int(_env("VLLM_PORT", default="8000")),
        VLLM_MODEL=_env("VLLM_MODEL", default="meta-llama/Llama-3.2-3B-Instruct"),
        VLLM_TENSOR_PARALLEL_SIZE=int(
            _env("VLLM_TENSOR_PARALLEL_SIZE", default="1")
        ),
        WS_HOST=_env("WS_HOST", default="127.0.0.1"),
        WS_PORT=int(_env("WS_PORT", default="8765")),
        MCP_HOST=_env("MCP_HOST", default="127.0.0.1"),
        MCP_PORT=int(_env("MCP_PORT", default="8766")),
        VAULT_PATH=vault_path,
        LOG_LEVEL=log_level,
        TOKEN_COST_PER_TOKEN=float(
            _env("TOKEN_COST_PER_TOKEN", default="0.0000001")
        ),
        INITIAL_AI_TOKEN_BALANCE=float(
            _env("INITIAL_AI_TOKEN_BALANCE", default="1000.0")
        ),
        IS_HEADLESS=_env("TALA_HEADLESS", default="false").lower() in ("true", "1", "yes"),
    )

    Config._LOADED = True
    
    # Initialize the cryptographic wallet and set TALA_WALLET_ADDRESS env var
    from . import wallet
    wallet.init_wallet(cfg.VAULT_PATH)
    
    logger.debug("Config loaded: WS=%s DB=%s vLLM=%s", cfg.ws_uri, cfg.DB_URL, cfg.vllm_base_url)
    return cfg


# ---------------------------------------------------------------------------
# Module-level singleton — import this wherever you need settings
# ---------------------------------------------------------------------------

settings: Config = load_config()
