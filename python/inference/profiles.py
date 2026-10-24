"""
profiles.py -- named inference profiles (``kagura``, ``nano``, ...).

A profile pins the whole local stack for one machine: which llama-server
binary and GGUF serve the main model, its port, context and flags, the
reasoning effort per turn type, and the two Ollama-served roles (fallback chat
and vision). Defaults live in ``profiles.json`` next to this file; nothing
here hardcodes a drive letter. Paths are templates over two directories:

* ``TALA_TOOLS_DIR``  (default ``<install drive>/tools`` on Windows, ``~/tala/tools`` elsewhere)
* ``TALA_MODELS_DIR`` (default ``<install drive>/models`` on Windows, ``~/tala/models`` elsewhere)

Selecting a profile, highest priority first:

1. ``TALA_PROFILE`` environment variable (``kagura``, ``nano``, ``legacy``, ``auto``)
2. ``<user data>/inference-profile.json`` written by ``python -m python.inference.profiles use <name>``
3. ``auto``: ``kagura`` on Windows when its llama-server exists, ``nano`` on
   Linux/ARM when its llama-server exists, otherwise no profile.

``legacy`` (or ``none``) means no profile: the pre-profile behaviour driven by
the ``TALA_*`` environment variables in ``external_engine.py``. Those
variables still override a profile field-for-field.

Overrides: ``TALA_PROFILES_FILE`` (or ``<user data>/inference-profiles.json``)
is deep-merged over the built-ins, so a machine can change one port or path
without copying the file.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BUILTIN_PROFILES_FILE = Path(__file__).with_name("profiles.json")
LEGACY_NAMES = ("legacy", "none", "off")
EFFORTS = ("off", "low", "medium", "high")


@dataclass(frozen=True)
class MainModel:
    label: str
    server_bin: Path
    model_path: Path
    hf_repo: str
    hf_file: str
    port: int
    ctx: int
    args: tuple[str, ...]
    fork: bool
    harmony: bool
    startup_timeout_s: float


@dataclass(frozen=True)
class OllamaRole:
    model: str
    num_ctx: int
    keep_alive: str
    vision: bool = False
    #: False pins the model to the CPU (Ollama ``num_gpu: 0``): it takes no VRAM, so it never displaces the main model.
    gpu: bool = True

    @property
    def num_gpu(self) -> int | None:
        return None if self.gpu else 0


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    main: MainModel
    reasoning: dict[str, str] = field(default_factory=dict)
    reasoning_token_budget: dict[str, int] = field(default_factory=dict)
    fallback: OllamaRole | None = None
    vision: OllamaRole | None = None
    ollama_url: str = "http://localhost:11434"
    allow_remote: bool = False
    vram_total_mb: int = 6144
    vram_exclusive: bool = True
    hf_cache_dir: Path | None = None

    def reasoning_effort(self, *, has_tools: bool) -> str | None:
        """Reasoning level for this turn, or None when the model has none.

        Tool-using turns get the ``tools`` level (medium on Kagura), casual
        chat the ``chat`` level (low). Only harmony models take the setting.
        """
        if not self.main.harmony:
            return None
        level = self.reasoning.get("tools" if has_tools else "chat", "off")
        return level if level in EFFORTS and level != "off" else None

    def token_budget(self, effort: str | None) -> int:
        """Extra completion tokens reasoning may spend before the visible reply."""
        return int(self.reasoning_token_budget.get(effort or "", 0))

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "main": {
                "label": self.main.label,
                "server_bin": str(self.main.server_bin),
                "model_path": str(self.main.model_path),
                "port": self.main.port,
                "ctx": self.main.ctx,
                "args": list(self.main.args),
                "harmony": self.main.harmony,
            },
            "reasoning": dict(self.reasoning),
            "fallback": self.fallback.model if self.fallback else None,
            "vision": self.vision.model if self.vision else None,
            "vision_on_gpu": self.vision.gpu if self.vision else None,
            "ollama_url": self.ollama_url,
            "allow_remote": self.allow_remote,
            "vram": {"total_mb": self.vram_total_mb, "exclusive": self.vram_exclusive},
        }


# ---------------------------------------------------------------------------
# Directory templates
# ---------------------------------------------------------------------------


def _default_root(kind: str) -> str:
    """``<drive Tala is installed on>/<kind>`` on Windows, ``~/tala/<kind>`` elsewhere.

    The drive comes from where this code lives, not from a literal: Tala runs
    from a USB stick on any OS, and on Kagura it is installed on D:, so the
    defaults land on D: without a drive letter in the source."""
    if sys.platform.startswith("win"):
        return f"{Path(__file__).resolve().anchor}{kind}"
    return str(Path.home() / "tala" / kind)


def _tools_dir() -> str:
    return os.environ.get("TALA_TOOLS_DIR", "").strip() or _default_root("tools")


def _models_dir() -> str:
    return os.environ.get("TALA_MODELS_DIR", "").strip() or _default_root("models")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("{tools_dir}", _tools_dir()).replace("{models_dir}", _models_dir())
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _user_data_dir() -> Path:
    from sdk.agent.documentation_nodes import resolve_user_data_dir

    return resolve_user_data_dir()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("profiles: could not read %s: %s", path, exc)
        return {}


def _raw_profiles() -> dict[str, dict[str, Any]]:
    raw = _read_json(BUILTIN_PROFILES_FILE)
    override_path = os.environ.get("TALA_PROFILES_FILE", "").strip()
    override = _read_json(Path(override_path)) if override_path else _read_json(
        _user_data_dir() / "inference-profiles.json"
    )
    for name, fields in override.items():
        if isinstance(fields, dict):
            raw[name] = _deep_merge(raw.get(name, {}), fields)
    return raw


def _role(data: Any, *, vision: bool = False) -> OllamaRole | None:
    if not isinstance(data, dict) or not data.get("model"):
        return None
    return OllamaRole(
        model=str(data["model"]),
        num_ctx=int(data.get("num_ctx", 8192)),
        keep_alive=str(data.get("keep_alive", "0")),
        vision=vision,
        gpu=bool(data.get("gpu", True)),
    )


def _build(name: str, data: dict[str, Any]) -> Profile:
    data = _expand(data)
    main = data["main"]
    hf_cache = data.get("hf_cache_dir")
    vram = data.get("vram") or {}
    return Profile(
        name=name,
        description=str(data.get("description", "")),
        main=MainModel(
            label=str(main.get("label", name)),
            server_bin=Path(main["server_bin"]),
            model_path=Path(main["model_path"]),
            hf_repo=str(main.get("hf_repo", "")),
            hf_file=str(main.get("hf_file", "")),
            port=int(main.get("port", 0)),
            ctx=int(main.get("ctx", 4096)),
            args=tuple(str(a) for a in main.get("args", [])),
            fork=bool(main.get("fork", False)),
            harmony=bool(main.get("harmony", False)),
            startup_timeout_s=float(main.get("startup_timeout_s", 120)),
        ),
        reasoning={str(k): str(v) for k, v in (data.get("reasoning") or {}).items()},
        reasoning_token_budget={
            str(k): int(v) for k, v in (data.get("reasoning_token_budget") or {}).items()
        },
        fallback=_role(data.get("fallback")),
        vision=_role(data.get("vision"), vision=True),
        ollama_url=str(data.get("ollama_url", "http://localhost:11434")).rstrip("/"),
        allow_remote=bool(data.get("allow_remote", False)),
        vram_total_mb=int(vram.get("total_mb", 6144)),
        vram_exclusive=bool(vram.get("exclusive", True)),
        hf_cache_dir=Path(hf_cache) if hf_cache else None,
    )


def available() -> list[str]:
    return sorted(_raw_profiles())


def get(name: str) -> Profile | None:
    raw = _raw_profiles().get(name)
    if raw is None:
        return None
    try:
        return _build(name, raw)
    except Exception as exc:
        logger.warning("profiles: profile %r is malformed: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _persisted_name() -> str:
    try:
        return str(_read_json(_user_data_dir() / "inference-profile.json").get("active", "")).strip()
    except Exception:
        return ""


def _requested_name() -> str:
    env = os.environ.get("TALA_PROFILE", "").strip().lower()
    return env or _persisted_name().lower() or "auto"


def _auto() -> Profile | None:
    machine = platform.machine().lower()
    candidates: list[str] = []
    if sys.platform.startswith("win"):
        candidates = ["kagura"]
    elif sys.platform.startswith("linux") and machine in ("aarch64", "arm64"):
        candidates = ["nano"]
    for name in candidates:
        profile = get(name)
        if profile is not None and profile.main.server_bin.is_file():
            return profile
    return None


def active() -> Profile | None:
    """The profile in force, or None for the legacy env-driven behaviour."""
    name = _requested_name()
    if name in LEGACY_NAMES:
        return None
    if name == "auto":
        return _auto()
    profile = get(name)
    if profile is None:
        logger.warning(
            "profiles: TALA_PROFILE=%r matches no profile (have: %s); running without one.",
            name, ", ".join(available()),
        )
    return profile


def set_active(name: str) -> None:
    """Persist the choice so it survives restarts. ``auto``/``legacy`` are valid."""
    name = name.strip().lower()
    if name not in ("auto", *LEGACY_NAMES) and name not in available():
        raise ValueError(f"unknown profile {name!r}; available: {', '.join(available())}")
    path = _user_data_dir() / "inference-profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"active": name}, indent=2), encoding="utf-8")


def describe() -> dict[str, Any]:
    """What the status surface shows: the request, what it resolved to, and the options."""
    profile = active()
    return {
        "requested": _requested_name(),
        "active": profile.summary() if profile else None,
        "available": available(),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m python.inference.profiles")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("name", nargs="?")
    use = sub.add_parser("use")
    use.add_argument("name")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        current = active()
        for name in available():
            print(f"{'*' if current and current.name == name else ' '} {name}")
        print(f"requested: {_requested_name()}")
        return 0
    if args.cmd == "show":
        profile = get(args.name) if args.name else active()
        if profile is None:
            print("no profile in force" if not args.name else f"unknown profile {args.name!r}")
            return 1
        print(json.dumps(profile.summary(), indent=2))
        return 0
    try:
        set_active(args.name)
    except ValueError as exc:
        print(exc)
        return 1
    print(f"active profile set to {args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
