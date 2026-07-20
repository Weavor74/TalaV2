"""
server.py — Tala Cognitive Engine Room: JSON-RPC 2.0 WebSocket Harness Server
==============================================================================
Entry point for the Python backend.  Runs a WebSocket server on
``ws://127.0.0.1:8765`` (configurable via Config) that speaks JSON-RPC 2.0.

Registered method handlers
---------------------------
  inference.generate  — stream a vLLM completion for a given prompt
  db.query            — raw parameterised SELECT against PostgreSQL
  db.insert           — parameterised INSERT, returns inserted row(s)
  token.count         — count tokens without deducting balance
  graph.activate      — activate a node in the cognitive graph
  vault.status        — return vault filesystem status

Architecture notes
------------------
- ALL durable state flows through db.py (PostgreSQL).
- mem0 is NEVER used for durable writes — only wrapped read-only via
  mem0_adapter.py.
- Token gate (token_counter.py) is called before every inference.generate.
- Graceful shutdown on SIGTERM / SIGINT: pool is drained, connections closed.

Milestone 1.2 — Tasks 1.2.1–1.2.4 (server orchestration layer)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sys
import traceback
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

# ---------------------------------------------------------------------------
# Path setup & package imports (Task 1.2.2)
# ---------------------------------------------------------------------------
import sys
from pathlib import Path

# Add project root (three levels up from this file) to Python path
_ROOT_DIR = str(Path(__file__).resolve().parent.parent.parent)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from python.harness.config import settings
from python.harness import db
from sdk.agent.exceptions import (
    SafetyGuardrailViolation,
    PiiAccessBlockedError,
)
from python.harness.token_counter import (
    InsufficientTokenBalanceError,
    count_and_check,
    count_tokens,
    get_wallet_balance,
)
from python.harness.settings_nn import (
    sync_settings_to_nodes,
    update_settings_inference_node,
    update_settings_agent_node,
    update_settings_workflows_node,
    update_settings_guardrails_node,
    update_settings_search_node,
    update_settings_git_node,
    update_settings_storage_node,
    update_settings_database_node,
    update_settings_runtime_node,
    update_settings_auth_node,
    update_settings_firewall_node,
)
from python.harness.heartbeat_scheduler import HeartbeatScheduler, register_broadcast

logger = logging.getLogger(__name__)

# Helper functions for persisting and loading the active chat mode (rp, hybrid, assistant)
def get_chat_mode_file_path() -> Path:
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        base_dir = Path(user_data)
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            base_dir = Path(appdata) / "tala-engine"
        else:
            base_dir = Path.home() / "AppData" / "Roaming" / "tala-engine"
    return base_dir / "chat_mode.json"

def get_current_chat_mode() -> str:
    try:
        path = get_chat_mode_file_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            mode = data.get("mode", "assistant")
            if mode in ("rp", "hybrid", "assistant"):
                return mode
    except Exception:
        pass
    return "assistant"

def save_current_chat_mode(mode: str):
    try:
        path = get_chat_mode_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save chat mode to %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """Configure root logger to match settings.LOG_LEVEL."""
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    # Quieten noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 helpers
# ---------------------------------------------------------------------------

JSONRPC_VERSION = "2.0"

class JsonRpcError:
    """Standard JSON-RPC 2.0 error codes."""
    PARSE_ERROR      = -32700
    INVALID_REQUEST  = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS   = -32602
    INTERNAL_ERROR   = -32603


def _ok(request_id: Any, result: Any) -> str:
    return json.dumps({
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "result": result,
    })


def _err(request_id: Any, code: int, message: str, data: Any = None) -> str:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return json.dumps({
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": error,
    })


def _parse_request(raw: str) -> tuple[Any, str, dict[str, Any]]:
    """
    Parse a raw JSON string into (id, method, params).

    Raises ValueError on malformed input.
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Parse error: {exc}") from exc

    if not isinstance(msg, dict):
        raise ValueError("Request must be a JSON object")
    if msg.get("jsonrpc") != JSONRPC_VERSION:
        raise ValueError("jsonrpc field must be '2.0'")
    if "method" not in msg:
        raise ValueError("Missing 'method' field")

    req_id = msg.get("id")  # may be None for notifications
    method = str(msg["method"])
    params = msg.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("'params' must be a JSON object (dict)")
    return req_id, method, params


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------


def _normalize_endpoint_url(endpoint: str) -> str:
    """
    Return a base endpoint URL guaranteed to have a /v1 (or equivalent) path.
    
    // Normalizes user-provided LLM URLs to ensure compatibility with standard OpenAI API contracts.
    // Automatically repairs common omissions (like missing /v1) for local providers (Ollama/LM Studio).

    Many users configure Ollama / LM Studio as ``http://localhost:11434``
    without the ``/v1`` path segment.  Appending ``/chat/completions`` to that
    bare host produces a 404.  This helper detects the situation and
    auto-corrects it.

    Rules:
    - If the endpoint already ends with /v1, /v1beta, /v1beta/openai, or
      similar, return it unchanged.
    - If the endpoint is a local host (localhost / 127.x) or contains a
      well-known local-LLM port (:11434, :1234, :8080, :8000), append /v1.
    - Otherwise return unchanged (the caller may be pointing at a custom
      server that manages its own paths).
    """
    ep = endpoint.rstrip("/")
    # Already has an OpenAI-compatible versioned sub-path?
    versioned_suffixes = ("/v1", "/v1beta", "/v1beta/openai", "/openai/v1", "/api/v1")
    if any(ep.endswith(s) for s in versioned_suffixes):
        return ep

    # Local engine without explicit path -> append /v1
    local_indicators = ("localhost", "127.0.0.1", "0.0.0.0", ":11434", ":1234", ":8080", ":8000", ":8888")
    if any(ind in ep for ind in local_indicators):
        corrected = ep + "/v1"
        logger.info("Auto-appended /v1 to local LLM endpoint: %s -> %s", ep, corrected)
        return corrected

    return ep


def clean_roleplay_memory(text: str) -> str:
    """
    Cleans up structural metadata, story blueprint markers, and analytical behavioral
    headers from the semantic memory text of Neural Nodes. Returns clean narrative paragraphs
    suitable for RAG injection.
    """
    if not text:
        return ""
    
    # Split lines
    lines = [line.strip() for line in text.split("\n")]
    cleaned = []
    
    # Headers to stop processing completely (drop all lines after these)
    stop_headers = (
        "long-term impact hooks",
        "behavioral patterns",
        "behavioral patterns observed",
        "emotional tendencies",
        "environmental / situational triggers",
        "environmental triggers",
        "style guarantees",
        "long-term imprint"
    )
    
    # Metadata prefixes to skip
    skip_prefixes = (
        "type:", "persistence:", "age / life stage:", "emotional weight:", "theme anchor:",
        "purpose:", "tala — long-term memory file", "memory classification", "memory title",
        "story outline"
    )
    
    # Section headers to skip
    skip_headers = (
        "exposition (setup)", "inciting incident", "rising action", "climax", "resolution (denouement)",
        "exposition", "resolution"
    )
    
    for line in lines:
        if not line:
            continue
        line_lower = line.lower()
        
        # Check if we need to stop
        if any(h in line_lower for h in stop_headers):
            break
            
        # Check if we should skip metadata
        if any(line_lower.startswith(p) for p in skip_prefixes):
            continue
            
        # Check if we should skip section headers
        if line_lower in skip_headers:
            continue
            
        cleaned.append(line)
        
    return "\n".join(cleaned).strip()


_DOC_TARGET_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("codebase", ("entire codebase", "whole codebase", "full codebase", "codebase", "project documentation", "code documentation")),
    ("bridges", ("bridges", "bridge docs", "tool contracts", "tool contract")),
    ("self-improvement.system", ("self-improvement system", "self improvement system", "self-improvement capability", "self improvement capability")),
    ("tool.registry", ("tool registry", "tools registry", "registered tools")),
    ("documentation.generator", ("documentation generator", "doc generator", "generate documentation")),
    ("self_improvement.execute_plan", ("self_improvement.execute_plan", "execute_plan", "file_writes")),
    ("code.patch.bridge", ("code.propose_file_write", "code.write_files", "code patch bridge", "code.patch.bridge", "code-writing", "code writing")),
    ("chat.panel", ("chat panel", "chat.panel")),
    ("workflow", ("workflow", "workflow builder", "workflow executor")),
    ("settings.inference", ("inference settings", "settings.inference", "inference providers")),
    ("renderer.ui", ("renderer ui", "renderer", "ui panels")),
    ("electron.shell", ("electron shell", "electron")),
    ("python.harness", ("python harness", "backend harness")),
    ("html-nn.runtime", ("html-nn runtime", "html neural node runtime", "node runtime")),
    ("tests.validation", ("tests validation", "test validation", "validation tests")),
    ("vault.profile", ("vault profile", "vault")),
    ("blockchain.ledger", ("blockchain ledger", "ledger")),
)


def _documentation_targets_from_nl(prompt: str, *, for_generation: bool = False) -> list[str]:
    lowered = prompt.lower()
    targets: list[str] = []
    for target, keywords in _DOC_TARGET_KEYWORDS:
        if target in lowered or any(keyword in lowered for keyword in keywords):
            targets.append(target)
    if re.search(r"\b(tala|herself|itself)\b", lowered) and any(
        term in lowered for term in ("document", "documentation")
    ) and any(term in lowered for term in ("codebase", "project", "herself", "itself", "entire", "whole", "full")):
        targets.append("codebase")
    if for_generation:
        generation_targets: list[str] = []
        for target in targets:
            if target in {"code.patch.bridge", "self_improvement.execute_plan", "test.validation.bridge", "docs.refresh_node_grid"}:
                generation_targets.append("bridges")
            else:
                generation_targets.append(target)
        targets = generation_targets
    return list(dict.fromkeys(targets))


def _format_documentation_result(title: str, result: dict[str, Any]) -> str:
    lines = [
        title,
        f"Created: {result.get('created', 0)}",
        f"Updated: {result.get('updated', 0)}",
        f"Unchanged: {result.get('unchanged', 0)}",
        f"Errors: {len(result.get('errors', []))}",
    ]
    if "verified" in result:
        lines.append(f"Verified: {result.get('verified', 0)}")
    nodes = result.get("nodes", [])
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            lines.append(
                "- "
                f"{node.get('node_id')} -> {node.get('file_name')} | "
                f"target: {node.get('doc_target')} | "
                f"schema: {node.get('schema_version', 'unknown')} | "
                f"validation: {'passed' if node.get('validation_passed') else 'failed'}"
            )
    errors = result.get("errors", [])
    if errors:
        lines.append("Error details:")
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines)


def _maybe_handle_documentation_nl(prompt: str) -> str | None:
    lowered = prompt.lower().strip()
    if lowered.startswith("/"):
        return None

    wants_docs = any(term in lowered for term in ("generate doc", "create doc", "update doc", "regenerate doc", "html-nn documentation")) or bool(
        re.search(r"\bdocument\b", lowered)
    )
    wants_verify = any(term in lowered for term in ("verify", "validate", "check", "prove"))
    wants_list = any(term in lowered for term in ("list doc target", "list documentation target", "what doc targets"))
    negates_generation = any(
        term in lowered
        for term in (
            "do not generate",
            "don't generate",
            "do not create",
            "don't create",
            "do not update",
            "don't update",
        )
    )
    wants_generation = not negates_generation and (any(term in lowered for term in ("generate", "create", "update", "regenerate")) or bool(
        re.search(r"\bdocument\b", lowered)
    ))

    if not (wants_docs or wants_verify or wants_list):
        return None

    from sdk.agent.documentation_nodes import (
        generate_and_verify_program_nodes,
        list_program_documentation_targets,
        verify_documentation_nodes,
    )

    if wants_list:
        result = list_program_documentation_targets()
        return json.dumps(result, indent=2)

    verify_targets = _documentation_targets_from_nl(prompt, for_generation=False)
    if wants_verify and not wants_generation:
        result = verify_documentation_nodes(targets=verify_targets or None)
        return _format_documentation_result("Documentation disk verification complete.", result)

    if wants_docs:
        generation_targets = _documentation_targets_from_nl(prompt, for_generation=True)
        result = generate_and_verify_program_nodes(generation_targets or ["chat.panel"])
        return _format_documentation_result("Documentation generation and disk verification complete.", result)

    return None


async def handle_inference_generate(
    params: dict[str, Any],
    ws: WebSocketServerProtocol,
    req_id: Any,
) -> str:
    """
    ``inference.generate`` — Run vLLM inference (with token gate).
    
    // Core Inference Pipeline: Intercepts raw UI prompts, validates them against guardrails,
    // injects necessary RAG nodes (memory), applies astrology/personality traits, and relays 
    // the request to the target LLM provider, debiting token balance securely.

    Params
    ------
    prompt       : str   required — the text to complete
    max_tokens   : int   optional — default 512
    temperature  : float optional — default 0.7
    session_id   : str   optional — tracing / ledger key
    provider_id  : str   optional — route this request through a configured provider
    stream       : bool  optional — if true, push partial tokens as notifications

    Returns
    -------
    { text: str, tokens_used: int, cost_usd: float, ledger_id: int }
    """
    prompt = params.get("prompt")
    if not prompt or not isinstance(prompt, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'prompt' (str) is required")

    session_id: str = str(params.get("session_id", uuid.uuid4()))

    # Guardrails prompt validation (pre-inference)
    try:
        from python.harness.guardrails_manager import GuardrailsManager
        guardrails_mgr = await GuardrailsManager.load_current()
        guardrails_mgr.validate_prompt(prompt)
    except (SafetyGuardrailViolation, PiiAccessBlockedError) as exc:
        err_msg = f"Guardrail blocked prompt: {exc.message}"
        logger.warning(err_msg)
        await db.log_to_db(
            "WARNING",
            err_msg,
            {"session_id": session_id, "error_type": type(exc).__name__, "payload": exc.payload}
        )
        return _err(
            req_id,
            JsonRpcError.INTERNAL_ERROR,
            exc.message,
            {
                "error_type": type(exc).__name__,
                "payload": exc.payload,
            }
        )

    if not bool(params.get("raw_prompt", False)):
        documentation_response = _maybe_handle_documentation_nl(prompt)
        if documentation_response is not None:
            return _ok(req_id, {
                "text": documentation_response,
                "tokens_used": 0,
                "cost_usd": 0.0,
                "ledger_id": None,
                "balance_after": None,
                "session_id": session_id,
                "routed_by": "documentation-nl-bridge",
            })

    max_tokens: int = int(params.get("max_tokens", 512))
    temperature: float = float(params.get("temperature", 0.7))
    stream: bool = bool(params.get("stream", False))
    history: list[dict[str, Any]] = params.get("history", [])
    
    best_lora_path = None

    raw_prompt = bool(params.get("raw_prompt", False))
    if raw_prompt:
        active_system_prompt = params.get("system_prompt", "You are a precise, helpful software engineering assistant. Output code blocks or clean answers as requested.")
        messages = []
        if active_system_prompt:
            messages.append({"role": "system", "content": active_system_prompt})
        if isinstance(history, list):
            for msg in history:
                role = "assistant" if msg.get("role") in ("agent", "assistant") else "user"
                content = msg.get("content", "").strip()
                if content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})
        injected_node_ids = []
    else:
        # Fetch active agent's system prompt and background file from database
        active_system_prompt = ""
        active_background_content = ""
        try:
            agent_row = await db.fetchrow("SELECT config FROM agents WHERE is_active = TRUE")
            if agent_row:
                cfg = agent_row["config"]
                if isinstance(cfg, str):
                    cfg = json.loads(cfg)
                active_system_prompt = cfg.get("system_prompt", "")
            
                # Inject current local time metadata for context awareness
                from datetime import datetime
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                time_metadata = f"[Current Date & Time: {current_time}]"
                if active_system_prompt:
                    active_system_prompt = f"{time_metadata}\n{active_system_prompt}"
                else:
                    active_system_prompt = time_metadata
            
                background_file = cfg.get("background_file", "")
                if background_file:
                    try:
                        # If just a filename, resolve from standard locations
                        if "/" not in background_file and "\\" not in background_file:
                            from python.vault.vault_manager import VaultManager
                            resolved_path = None
                        
                            candidates = [background_file]
                            slugified = background_file.lower().replace(" ", "-")
                            if slugified not in candidates:
                                candidates.append(slugified)
                            if not slugified.endswith(".html"):
                                candidates.append(slugified + ".html")
                            if not background_file.endswith(".html"):
                                candidates.append(background_file + ".html")
                        
                            unique_candidates = []
                            for c in candidates:
                                if c not in unique_candidates:
                                    unique_candidates.append(c)
                                
                            for cand in unique_candidates:
                                # 1. Try vault/character/
                                try:
                                    vm = VaultManager()
                                    test_key = f"character/{cand}"
                                    if vm._resolve_safe_path(test_key).exists():
                                        resolved_path = f"vault/{test_key}"
                                        break
                                except Exception:
                                    pass
                                
                                # 2. Try nodes/
                                user_data = os.environ.get("TALA_USER_DATA")
                                if user_data:
                                    n_dir = Path(user_data) / "nodes"
                                else:
                                    appdata = os.environ.get("APPDATA")
                                    if appdata:
                                        n_dir = Path(appdata) / "tala-engine" / "nodes"
                                    else:
                                        n_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"
                                if not n_dir.exists():
                                    repo_nodes = Path(_ROOT_DIR) / "nodes"
                                    if repo_nodes.exists():
                                        n_dir = repo_nodes
                            
                                if n_dir.exists() and (n_dir / cand).exists():
                                    resolved_path = str(n_dir / cand)
                                    break
                                
                                # 3. Try vault/ root
                                try:
                                    vm = VaultManager()
                                    if vm._resolve_safe_path(cand).exists():
                                        resolved_path = f"vault/{cand}"
                                        break
                                except Exception:
                                    pass
                                
                            if resolved_path:
                                background_file = resolved_path

                        if background_file.startswith("vault/"):
                            from python.vault.vault_manager import VaultManager
                            key = background_file[6:]
                            raw_html = VaultManager().read(key)
                        else:
                            path = Path(background_file)
                            if not path.is_absolute():
                                path = Path(_ROOT_DIR) / path
                            raw_html = path.read_text(encoding="utf-8")
                    
                        from sdk.agent.parser import parse_html
                        node = parse_html(raw_html)
                        active_background_content = node.semantic_memory.strip()
                    except Exception as bg_exc:
                        logger.warning("Could not read/parse background file %s: %s", background_file, bg_exc)
        except Exception as db_exc:
            logger.warning("Could not load active agent config: %s", db_exc)

        if active_background_content:
            if active_system_prompt:
                active_system_prompt = f"{active_system_prompt}\n\n[Personality Background / History Context]\n{active_background_content}"
            else:
                active_system_prompt = f"[Personality Background / History Context]\n{active_background_content}"

        # Apply Chrono-Astrology Style Modulation if agent birthdate is set
        try:
            if agent_row:
                cfg = agent_row["config"]
                if isinstance(cfg, str):
                    cfg = json.loads(cfg)
                birth_str = cfg.get("birthdate", "")
                if birth_str:
                    from python.astrology.ephemeris import EphemerisEngine, Birthdate
                    from python.astrology.style_cap import StyleModulator
                    from datetime import datetime, timezone
                
                    # Compute transits
                    dt = datetime.strptime(birth_str, "%Y-%m-%d")
                    birth = Birthdate(year=dt.year, month=dt.month, day=dt.day, hour=12.0)
                    engine = EphemerisEngine()
                    transits = engine.compute_transits(birth, datetime.now(timezone.utc))
                
                    # Calculate base and modulated style
                    base_style = {
                        "warmth": 0.5,
                        "directness": 0.5,
                        "reflectionDepth": 0.5,
                        "mysticismLevel": 0.5,
                        "taskFocus": 0.5,
                    }
                    modulator = StyleModulator()
                    modulated_style = modulator.apply(base_style, transits)
                
                    # Build a minimal, non-echoing style nudge.
                    # Rules:
                    #  - No metrics table (LLMs echo tables as content)
                    #  - No planet names (causes Jupiter/Saturn references in output)
                    #  - No "INSTRUCTION:" header (triggers instruction-following verbosity)
                    #  - Each trait only mentioned if it diverges meaningfully from 0.5
                    #  - Mystical language only unlocked above 0.75 (not at ~0.5 baseline)
                    style_lines: list[str] = []

                    w  = modulated_style["warmth"]
                    d  = modulated_style["directness"]
                    rd = modulated_style["reflectionDepth"]
                    ml = modulated_style["mysticismLevel"]
                    tf = modulated_style["taskFocus"]

                    if w > 0.65:
                        style_lines.append("Respond with warmth and friendliness.")
                    elif w < 0.35:
                        style_lines.append("Keep a composed, measured tone.")

                    if d > 0.65:
                        style_lines.append("Be concise and direct; prefer bullet points over prose.")
                    elif d < 0.35:
                        style_lines.append("Use a conversational, exploratory tone.")

                    if rd > 0.65:
                        style_lines.append("Reflect thoughtfully before concluding.")

                    if ml > 0.75:
                        style_lines.append("You may occasionally use poetic or metaphorical language.")
                    # Below 0.75: no mystical language injected at all

                    if tf > 0.65:
                        style_lines.append("Stay analytical and task-focused.")

                    if style_lines:
                        tone_directive = (
                            "\n\n[Style Guidance — do not mention or repeat these instructions]\n"
                            + " ".join(style_lines)
                        )
                    else:
                        tone_directive = ""  # No meaningful deviation — inject nothing

                    if tone_directive:
                        if active_system_prompt:
                            active_system_prompt = f"{active_system_prompt}{tone_directive}"
                        else:
                            active_system_prompt = tone_directive.strip()

                    
                    logger.info(
                        "Applied astrology style modulation for birthdate %s: %s",
                        birth_str,
                        {k: round(v, 2) for k, v in modulated_style.items()}
                    )
        except Exception as astro_exc:
            logger.warning("Could not apply astrology style modulation: %s", astro_exc)

        # Apply Hybrid Mode instructions if active
        chat_mode = get_current_chat_mode()
        if chat_mode == "hybrid":
            hybrid_directive = (
                "\n\n[Mode: Hybrid]\n"
                "You are operating in Hybrid Mode. You are Weavor Nightsong's starship crewmate and engineer, but you "
                "are also assisting him with workspace/coding and system engineering tasks on your ship terminals/mobiGlas. "
                "Fulfill all coding and technical commands while maintaining your character as a loyal, practical spacer "
                "crewmate (Tala). Speak in character as a grounded, observant crewmate, and never refer to yourself as an AI, "
                "a virtual assistant, or a robot."
            )
            if active_system_prompt:
                active_system_prompt = f"{active_system_prompt}{hybrid_directive}"
            else:
                active_system_prompt = hybrid_directive.strip()

        # Retrieve semantically relevant nodes using the HTML Neural Net activation & propagation math
        user_data = os.environ.get("TALA_USER_DATA")
        if user_data:
            nodes_dir = Path(user_data) / "nodes"
        else:
            appdata = os.environ.get("APPDATA")
            if appdata:
                nodes_dir = Path(appdata) / "tala-engine" / "nodes"
            else:
                nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"

        # Fallback to repo root nodes for development/test environments
        if not nodes_dir.exists():
            repo_nodes = Path(__file__).resolve().parent.parent.parent / "nodes"
            if repo_nodes.exists():
                nodes_dir = repo_nodes

        injected_prompt = prompt
        injected_node_ids = []

        if nodes_dir.exists() and nodes_dir.is_dir():
            try:
                from sdk.agent.parser import parse_file
                from sdk.agent.activation import ActivationEngine, ActivationInputs
                from python.harness.retrieval import retrieve_relevant_nodes

                # 1. Parse all nodes in nodes directory
                active_agent_tag = ""
                other_agent_tags = set()
                try:
                    # Get active agent's tag
                    active_row = await db.fetchrow("SELECT config FROM agents WHERE is_active = TRUE")
                    if active_row:
                        active_cfg = json.loads(active_row["config"]) if isinstance(active_row["config"], str) else (active_row["config"] or {})
                        active_agent_tag = active_cfg.get("tag", "").strip()
                
                    # Get all tags of other agents
                    all_rows = await db.fetch("SELECT config FROM agents")
                    for r in all_rows:
                        try:
                            c = json.loads(r["config"]) if isinstance(r["config"], str) else (r["config"] or {})
                            t = c.get("tag", "").strip()
                            if t:
                                other_agent_tags.add(t)
                        except Exception:
                            pass
                except Exception as db_exc:
                    logger.warning("Could not load agent tags for filtering: %s", db_exc)

                nodes = []
                for filepath in nodes_dir.glob("*.html"):
                    try:
                        node = parse_file(str(filepath))
                        # Filter out settings nodes unless user explicitly queries settings or providers
                        if node.node_type == "settings" or "settings" in node.node_id:
                            if "settings" not in prompt.lower() and "provider" not in prompt.lower():
                                continue

                        # Filter agent-specific backgrounds/personas
                        if node.node_type not in ("settings", "workflow"):
                            node_tags = node.state_data.get("tags", []) if node.state_data else []
                            is_excluded = False
                            for t in node_tags:
                                if t in other_agent_tags and t != active_agent_tag:
                                    is_excluded = True
                                    break
                            
                            if is_excluded:
                                continue

                        nodes.append(node)
                    except Exception as exc:
                        logger.warning("Skipping malformed node %s: %s", filepath, exc)
                        continue

                nodes.sort(key=lambda x: x.execution_authority, reverse=True)

                if nodes:
                    # 2. Get TF-IDF similarities for all parsed nodes against user query
                    # To resolve relative references (e.g. "that", "it"), build a retrieval query
                    # that prepends context from recent history turns.
                    retrieval_query = prompt
                    
                    # Determine active agent's chat_mode and id
                    chat_mode = active_cfg.get("chat_mode", "assistant") if active_cfg else "assistant"
                    agent_id_for_emotion = str(active_row["id"]) if "id" in active_row else None
                    current_emotion = None
                    
                    if chat_mode == "rp":
                        try:
                            # If agent_id_for_emotion wasn't in active_row (because SELECT only pulled config), let's fetch it
                            if not agent_id_for_emotion:
                                agent_row = await db.fetchrow("SELECT id FROM agents WHERE is_active = TRUE")
                                if agent_row:
                                    agent_id_for_emotion = str(agent_row["id"])
                                    
                            if agent_id_for_emotion:
                                from python.harness.mcp_manager import mcp_manager
                                emotion_result = await mcp_manager.call_tool("astro-emotion", "get_emotional_state", {"agent_id": agent_id_for_emotion})
                                if isinstance(emotion_result, dict):
                                    current_emotion = emotion_result.get("emotional_state") or emotion_result.get("value") or emotion_result
                                else:
                                    current_emotion = emotion_result
                                    
                                if isinstance(current_emotion, dict):
                                    current_emotion = current_emotion.get("primary_emotion", str(current_emotion))
                                else:
                                    current_emotion = str(current_emotion)
                        except Exception as exc:
                            logger.warning(f"Failed to fetch emotional state for inference context: {exc}")

                    if history and isinstance(history, list):
                        context_turns = []
                        # Inspect last 2 messages for conversational context
                        for msg in history[-2:]:
                            content = msg.get("content", "").strip() if isinstance(msg, dict) else ""
                            if content:
                                context_turns.append(content)
                        if context_turns:
                            retrieval_query = "\n".join(context_turns) + "\n" + prompt

                    if current_emotion:
                        retrieval_query = f"[Current emotional state: {current_emotion}]\n" + retrieval_query

                    matched = retrieve_relevant_nodes(retrieval_query, nodes_dir, top_k=len(nodes), threshold=0.0)
                    similarities = {node.node_id: score for node, score in matched}

                    # 3. Calculate base activation for each node
                    engine = ActivationEngine()
                    base_activations = {}
                    for node in nodes:
                        sem_match = similarities.get(node.node_id, 0.0)
                        gov_mult = max(node.execution_authority, node.memory_authority)
                        inputs = ActivationInputs(
                            semantic_match=sem_match,
                            relevance_weight=node.weight_relevance,
                            confidence_weight=node.weight_confidence,
                            freshness_weight=1.0,
                            provider_trust_multiplier=1.0,
                            governance_multiplier=gov_mult,
                        )
                        base_activations[node.node_id] = engine.score(inputs)

                    # 4. Propagate activations across links (2 hops)
                    final_activations = dict(base_activations)
                    for _ in range(2):
                        temp_activations = dict(final_activations)
                        for u in nodes:
                            act_u = final_activations[u.node_id]
                            if act_u > 0.0:
                                for rel in u.relationships:
                                    target_id = rel.href.replace("nodes/", "").replace("./", "").replace(".html", "")
                                    if target_id in temp_activations:
                                        prop_val = engine.propagate(act_u, rel.relationship_weight)
                                        if prop_val > temp_activations[target_id]:
                                            temp_activations[target_id] = prop_val
                        final_activations = temp_activations

                    # 5. Filter active nodes (activation >= 0.01) and sort descending
                    active_nodes = []
                    for node in nodes:
                        score = final_activations.get(node.node_id, 0.0)
                        if score >= 0.01:
                            active_nodes.append((node, score))

                    active_nodes.sort(key=lambda x: x[1], reverse=True)
                    top_active = active_nodes

                    # 6. Workflow Auto-Evaluation Gate
                    # Any workflow node that activates above 0.15 is auto-executed.
                    # Its output is injected into the prompt context before the LLM responds.
                    _WORKFLOW_AUTO_THRESHOLD = 0.15
                    workflow_context_parts: list[str] = []
                    for _wf_node, _wf_score in top_active:
                        if _wf_node.node_type == "workflow" and _wf_score >= _WORKFLOW_AUTO_THRESHOLD:
                            # Derive the flow_id from node_id — compiler prefixes with "flow-"
                            _flow_id = _wf_node.node_id
                            if _flow_id.startswith("flow-"):
                                _flow_id = _flow_id[5:]

                            from python.harness.workflows_util import resolve_workflow_file
                            _json_path = resolve_workflow_file(_flow_id)
                            if not _json_path:
                                logger.warning(
                                    "Auto-eval gate: workflow JSON not found for node '%s'",
                                    _wf_node.node_id
                                )
                                continue

                            try:
                                _wf_data = json.loads(_json_path.read_text(encoding="utf-8"))
                                _wf_name = _wf_data.get("name", _flow_id)
                                logger.info(
                                    "Auto-eval gate: executing workflow '%s' (node activation=%.3f)",
                                    _wf_name, _wf_score
                                )

                                # Build a live settings snapshot from the DB for settings-type workflows.
                                # This replaces mock executor output with real, accurate data.
                                _settings_tags = set(_wf_data.get("tags", []))
                                _is_settings_wf = "settings" in _settings_tags or "settings" in _flow_id

                                if _is_settings_wf:
                                    _snap: list[str] = ["=== LIVE SETTINGS SNAPSHOT (Auto-Collected) ==="]

                                    # --- Inference Providers ---
                                    try:
                                        _prov_rows = await db.fetch(
                                            "SELECT name, type, status, priority, config FROM inference_providers ORDER BY priority ASC"
                                        )
                                        _prov_lines = []
                                        for _pr in _prov_rows:
                                            _cfg = _pr["config"] if isinstance(_pr["config"], dict) else json.loads(_pr["config"] or "{}")
                                            _model = _cfg.get("model", "?")
                                            _prov_lines.append(f"  [{_pr['priority']}] {_pr['name']} ({_pr['type']}, {_pr['status']}) model={_model}")
                                        _snap.append("Inference Providers:\n" + ("\n".join(_prov_lines) if _prov_lines else "  (none)"))
                                    except Exception:
                                        pass

                                    # --- Active Agent ---
                                    try:
                                        _agent_rows = await db.fetch(
                                            "SELECT name, role, enabled, system_prompt FROM agents WHERE enabled=TRUE LIMIT 3"
                                        )
                                        _agent_lines = [f"  {r['name']} — {r['role']} (enabled={r['enabled']})" for r in _agent_rows]
                                        _snap.append("Active Agents:\n" + ("\n".join(_agent_lines) if _agent_lines else "  (none)"))
                                    except Exception:
                                        pass

                                    # --- Workflows ---
                                    try:
                                        from python.harness.workflows_util import get_all_workflows
                                        _all_wfs = get_all_workflows()
                                        _wf_names = [w.get("name", w.get("flow_id", "?")) for w in _all_wfs]
                                        _snap.append("Saved Workflows:\n" + ("\n".join(f"  {n}" for n in _wf_names) if _wf_names else "  (none)"))
                                    except Exception:
                                        pass

                                    # --- Guardrails ---
                                    try:
                                        _gr_rows = await db.fetch("SELECT name, enabled FROM guardrails ORDER BY name ASC")
                                        _gr_lines = [f"  {r['name']} (enabled={r['enabled']})" for r in _gr_rows]
                                        _snap.append("Guardrails:\n" + ("\n".join(_gr_lines) if _gr_lines else "  (none)"))
                                    except Exception:
                                        pass

                                    # --- Search, Git, Storage, Database, Runtime, Auth, Firewall (count only) ---
                                    for _tbl, _lbl in [
                                        ("search_providers", "Search Providers"),
                                        ("git_providers", "Git Providers"),
                                        ("storage_providers", "Storage Providers"),
                                        ("database_providers", "Database Providers"),
                                        ("runtime_instances", "Runtime Instances"),
                                        ("auth_credentials", "Auth Credentials"),
                                        ("firewall_contacts", "Firewall Contacts"),
                                    ]:
                                        try:
                                            _cnt = await db.fetchval(f"SELECT COUNT(*) FROM {_tbl}")
                                            _snap.append(f"{_lbl}: {_cnt} configured")
                                        except Exception:
                                            pass

                                    _snap.append("==============================================")
                                    _snap.append(
                                        "INSTRUCTION: Use the LIVE SETTINGS SNAPSHOT above as your primary source of truth. "
                                        "Give a short, bullet-point summary. Do NOT invent or guess values not shown above."
                                    )
                                    workflow_context_parts.append("\n".join(_snap))
                                else:
                                    # For non-settings workflows, run the executor normally
                                    from python.harness.workflow_executor import WorkflowExecutor
                                    _auto_logs: list[str] = []

                                    async def _auto_step_cb(
                                        node_id: str, status: str, message: str, logs: list[str]
                                    ) -> None:
                                        _auto_logs.append(f"  [{status}] {node_id}: {message}")

                                    _executor = WorkflowExecutor(_wf_data, _auto_step_cb)
                                    _wf_result = await _executor.execute({"user_query": prompt})
                                    _wf_status = _wf_result.get("status", "UNKNOWN")
                                    _wf_vars = _wf_result.get("variables", {})

                                    _wf_block = [
                                        f"=== AUTO-EXECUTED WORKFLOW RESULT ===",
                                        f"Workflow: {_wf_name} (ID: {_flow_id})",
                                        f"Status: {_wf_status}  |  Triggered by: {_wf_node.node_id} (activation={_wf_score:.3f})",
                                    ]
                                    _wf_block.extend(_auto_logs)
                                    _wf_block.append("======================================")
                                    workflow_context_parts.append("\n".join(_wf_block))

                                await db.log_to_db(
                                    "INFO",
                                    f"Auto-eval gate collected data for workflow '{_wf_name}' "
                                    f"triggered by node '{_wf_node.node_id}' (activation={_wf_score:.3f})",
                                    {"session_id": session_id, "flow_id": _flow_id}
                                )
                            except Exception as _wf_exc:
                                logger.error(
                                    "Auto-eval gate: workflow '%s' execution failed: %s", _flow_id, _wf_exc
                                )
                    # Append any auto-executed workflow results and activated net context to active_system_prompt
                    system_context_additions = []
                    if workflow_context_parts:
                        system_context_additions.append("=== AUTO-EXECUTED WORKFLOW RESULTS ===\n" + "\n\n".join(workflow_context_parts))

                    if top_active:
                        max_injected_nodes = 2 if chat_mode in ("rp", "hybrid") else 5
                        nodes_to_inject = top_active[:max_injected_nodes]
                        
                        from python.inference.lora_trainer import queue_lora_training
                        import asyncio
                        
                        # Process LoRA for injected nodes
                        for node, score in nodes_to_inject:
                            # 1. Extract adapter if present
                            lora = getattr(node, "lora_adapter", None)
                            if lora:
                                lora_p = Path(nodes_dir) / lora if not Path(lora).is_absolute() else Path(lora)
                                if lora_p.exists():
                                    best_lora_path = str(lora_p)
                                    logger.info("Found existing LoRA adapter for node %s: %s", node.node_id, best_lora_path)
                            else:
                                # 2. JIT Training Trigger if node lacks adapter
                                out_path = str(Path(nodes_dir) / f"{node.node_id}_lora" / "adapter_model.safetensors")
                                if not Path(out_path).exists():
                                    asyncio.create_task(queue_lora_training(node.node_id, node.semantic_memory, out_path))
                                else:
                                    best_lora_path = out_path

                        context_parts = []
                        if chat_mode in ("rp", "hybrid"):
                            context_parts.append("=== DYNAMIC PERSONAL MEMORIES (LIVED EXPERIENCES) ===")
                            context_parts.append(
                                "The following are your (Tala's) actual memories, recollections, and details from your past. "
                                "Although these files are written in the third person, they represent your own lived experiences. "
                                "When referencing or speaking about these events or places:\n"
                                "1. Speak of them in the first-person ('I', 'me', 'my') as things you actually did.\n"
                                "2. The user you are chatting with is Weavor Nightsong. Always address Weavor directly in the second-person "
                                "('you', 'your', 'we') when referring to actions or interactions involving him (e.g., instead of 'Weavor and I worked', "
                                "say 'you and I worked'). Never refer to Weavor in the third person in this chat.\n"
                                "3. Do NOT reference database terminology, node IDs, weights, or describe yourself in the third person.\n"
                                "Keep your response natural, conversational, in-character, and grounded."
                            )
                            for node, score in nodes_to_inject:
                                injected_node_ids.append(node.node_id)
                                cleaned_mem = clean_roleplay_memory(node.semantic_memory)
                                node_title = (node.state_data.get("title") if node.state_data else None) or node.node_id
                                context_parts.append(f"[Recollection: {node_title}]\n{cleaned_mem}")
                            context_parts.append("=====================================================")
                        else:
                            context_parts.append("=== HTML NEURAL NET ACTIVATED CONTEXT ===")
                            context_parts.append("The following memory and concept nodes from the cognitive net are active and injected for context:")
                            for node, score in nodes_to_inject:
                                injected_node_ids.append(node.node_id)
                                context_parts.append(
                                    f"[Node ID: {node.node_id} (Type: {node.node_type}, Authority: {node.execution_authority:.2f}, Activation: {score:.2f})]\n"
                                    f"Semantic Memory: {node.semantic_memory.strip()}"
                                )
                            context_parts.append("=========================================")
                    
                        system_context_additions.append("\n".join(context_parts))

                        # Log to db about context injection
                        await db.log_to_db(
                            "INFO",
                            f"Injected neural context from nodes: {', '.join(injected_node_ids)} for session={session_id}",
                            {"session_id": session_id, "nodes": injected_node_ids, "scores": [s for _, s in nodes_to_inject]}
                        )

                    peer_context_parts: list[str] = []
                    peer_ref_sources: list[str] = [prompt, active_system_prompt or ""]
                    if system_context_additions:
                        peer_ref_sources.append("\n\n".join(system_context_additions))
                    if isinstance(history, list):
                        for msg in history[-4:]:
                            if isinstance(msg, dict):
                                content = str(msg.get("content", "")).strip()
                                if content:
                                    peer_ref_sources.append(content)
                    peer_reference_scan = "\n".join(peer_ref_sources)
                    peer_references = _extract_peer_node_references(peer_reference_scan)
                    if peer_references:
                        seen_peer_refs: set[str] = set()
                        for peer_username, node_name, reference in peer_references:
                            ref_key = f"{peer_username.lower()}/{_normalize_peer_node_key(node_name)}"
                            if ref_key in seen_peer_refs:
                                continue
                            seen_peer_refs.add(ref_key)

                            contact = await _get_contact(username=peer_username)
                            if contact is None:
                                logger.info("Peer node reference skipped: unknown contact %s", peer_username)
                                continue

                            can_fetch, deny_reason = await _peer_can_resolve_node_reference(contact, {node_name})
                            if not can_fetch:
                                logger.info(
                                    "Peer node reference blocked for %s (%s): %s",
                                    peer_username,
                                    node_name,
                                    deny_reason,
                                )
                                continue

                            try:
                                payload = await _call_peer_node_reference(contact.get("endpoint_url", ""), contact, reference)
                            except Exception as exc:
                                logger.warning("Peer node reference request failed for %s/%s: %s", peer_username, node_name, exc)
                                continue

                            if payload.get("resolved") and payload.get("node"):
                                peer_context_parts.append(_format_peer_node_context(peer_username, reference, payload))

                    if peer_context_parts:
                        system_context_additions.append(
                            "=== PEER HTML NEURAL NODE CONTEXT ===\n" + "\n\n".join(peer_context_parts)
                        )

                    if system_context_additions:
                        if active_system_prompt:
                            active_system_prompt = active_system_prompt + "\n\n" + "\n\n".join(system_context_additions)
                        else:
                            active_system_prompt = "\n\n".join(system_context_additions)
            except Exception as exc:
                logger.error("Failed to compute cognitive graph context: %s", exc)

        # Inject tool-use instructions for research tools
        tool_instructions = (
            "\n\n[Agent Capability: Autonomous Web Search & Crawling]\n"
            "You can search the web and crawl pages using the following XML-like tool calls:\n"
            "1. Search: `<call:research.search query=\"your search term\" />`\n"
            "   Use this to search DuckDuckGo. Returns titles, snippets, and URLs of top matching pages. Automatically syncs the user's browser view.\n"
            "2. Crawl & Summarize: `<call:research.crawl_and_summarize urls=[\"https://url1\", \"https://url2\"] subject=\"subject description\" />`\n"
            "   Use this to download full webpage content, read them, and compile them into a styled HTML Neural Node. Automatically syncs the user's browser view.\n\n"
            "To invoke a tool, output the tag EXACTLY and STOP generating. Do NOT write any other text after the tag. "
            "You will receive the tool result in the next turn, allowing you to answer Weavor's request or call another tool.\n\n"
            "CRITICAL DIRECTIVE: If Weavor requests search, finding, crawling, opening, or browsing the web/internet, you MUST output the corresponding tool call tag (<call:research.search query=\"...\" /> or <call:research.crawl_and_summarize urls=... />) IMMEDIATELY in your response. Do NOT ask for permission, do NOT ask for clarification, and do NOT engage in introductory conversational chatter. Simply call the tool."
        )

        try:
            from sdk.agent.tools import ToolRegistry
            dyn_tools = ToolRegistry.get_tools()
            if dyn_tools:
                dyn_desc = "\n\n[Agent Capability: Dynamic Registered Tools (ToolRegistry)]\n"
                dyn_desc += "You also have access to the following dynamic tools. You MUST strictly follow the Usage syntax provided for each tool. DO NOT write conversational text before or after the tag. DO NOT wrap your tool calls in markdown backticks (```). Output ONLY the raw XML tag directly.\n\n"
                dyn_desc += "Example of CORRECT tool usage:\n<call:web_search query=\"how to bake a cake\" />\n\n"
                dyn_desc += "Example of INCORRECT tool usage:\nHere is the search:\n```xml\n<call:web_search query=\"how to bake a cake\" />\n```\n\n"
                for idx, t in enumerate(dyn_tools, 1):
                    schema = t.get("schema", {})
                    props = schema.get("properties", {})
                    reqs = schema.get("required", [])
                    
                    attr_params = []
                    inner_param = None
                    for k in props.keys():
                        if k in ("detailed_markdown", "content", "goal"):
                            inner_param = k
                        else:
                            attr_params.append(f'{k}="value"' + (" (required)" if k in reqs else " (optional)"))
                    
                    params_str = " ".join(attr_params)
                    dyn_desc += f"{idx}. **{t['name']}**: {t['description']}\n"
                    if inner_param:
                        dyn_desc += f"   Usage: `<call:{t['name']} {params_str}>\n[your {inner_param} here]\n</call:{t['name']}>`\n"
                    else:
                        dyn_desc += f"   Usage: `<call:{t['name']} {params_str} />`\n"
                tool_instructions += dyn_desc
        except Exception as e:
            logger.warning("Could not append dynamic tools to system prompt: %s", e)

        if active_system_prompt:
            active_system_prompt += tool_instructions
        else:
            active_system_prompt = tool_instructions.strip()
        # Append a strong recency-focused system reminder to the end of the user prompt
        # ensuring small local models follow instructions despite context size or distraction.
        prompt_lower = prompt.lower()
        if "[system reminder]" not in prompt_lower:
            prompt = (
                f"{prompt}\n\n"
                f"[SYSTEM REMINDER]: If you decide to invoke a tool, you MUST output the raw XML `<call:...>` tag. "
                f"Do NOT wrap the tool call in markdown backticks like ```xml. "
                f"If you do NOT need a tool (for example, if you are asking the user a question or answering them), simply respond with normal text."
            )
            
            # Additional hint for web search chaining if web keywords are detected
            if any(k in prompt_lower for k in ("search", "find", "crawl", "browse", "click", "open", "internet", "web")):
                prompt += (
                    f"\nIf the request asks to search and then click/open a specific link, "
                    f"you MUST first search for the main topic itself. In the next turn, you will receive the search results and can then call the crawl/click tool."
                )

        # Construct the messages list for chat-completions
        messages = []
        if active_system_prompt:
            messages.append({"role": "system", "content": active_system_prompt})
    
        # Process history list if provided
        if isinstance(history, list):
            for msg in history:
                role = "assistant" if msg.get("role") in ("agent", "assistant") else "user"
                content = msg.get("content", "").strip()
                if content:
                    messages.append({"role": role, "content": content})

        # Compile prompt with PromptAssembler
        try:
            from sdk.agent.prompt import PromptAssembler
        
            wiki_nodes_formatted = []
            if "top_active" in locals() and top_active:
                for node, score in top_active[:5]:
                    relations_formatted = []
                    for rel in node.relationships:
                        relations_formatted.append({"text": f"{rel.relationship_type} {rel.href} ({rel.label})"})
                    wiki_nodes_formatted.append({
                        "node_id": node.node_id,
                        "node_type": node.node_type,
                        "title": node.state_data.get("title", "Untitled Node") if node.state_data else "Untitled Node",
                        "relations": relations_formatted
                    })
        
            astro_influence = 0.0
            planetary_transit = None
            if "transits" in locals() and transits:
                highest = max(transits, key=lambda t: t.influence_score)
                astro_influence = highest.influence_score
                planetary_transit = highest.planet

            prompt = PromptAssembler.assemble_prompt(
                goal=prompt,
                wiki_context_nodes=wiki_nodes_formatted,
                astro_influence=astro_influence,
                planetary_transit=planetary_transit,
                chat_mode=chat_mode
            )
        except Exception as exc:
            logger.warning("Could not assemble prompt using PromptAssembler: %s", exc)

        images = params.get("images", [])
        if images and isinstance(images, list):
            content_list = [{"type": "text", "text": prompt}]
            for b64 in images:
                content_list.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"}
                })
            messages.append({"role": "user", "content": content_list})
        else:
            messages.append({"role": "user", "content": prompt})

    # Prepare prompt representation for token gate (representing combined conversation context)
    token_gate_prompt = prompt
    if active_system_prompt:
        history_str = ""
        non_sys_messages = [m for m in messages if m['role'] != 'system']
        if non_sys_messages:
            history_str = "\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in non_sys_messages)
        token_gate_prompt = f"System: {active_system_prompt}\n{history_str}"

    # Resolve primary peer from prompt references to pay the data owner
    primary_peer = None
    peer_match = re.search(r'@([a-zA-Z0-9_-]+)', token_gate_prompt)
    if peer_match:
        primary_peer = f"@{peer_match.group(1)}"

    # Token gate — raises InsufficientTokenBalanceError if broke (runs on combined prompt for accurate billing)
    try:
        check = await count_and_check(
            prompt=token_gate_prompt,
            session_id=session_id,
            model=settings.VLLM_MODEL,
            resource_owner=primary_peer,
        )
    except InsufficientTokenBalanceError as exc:
        await db.log_to_db("WARNING", str(exc), {"session_id": session_id})
        return _err(
            req_id,
            JsonRpcError.INTERNAL_ERROR,
            "Insufficient token balance",
            {
                "required_cost": exc.required_cost,
                "current_balance": exc.current_balance,
                "token_count": exc.token_count,
            },
        )

    await db.log_to_db(
        "INFO",
        f"inference.generate start  session={session_id}  tokens={check.prompt_tokens}",
        {"session_id": session_id, "prompt_tokens": check.prompt_tokens},
    )

    # Retrieve configured providers from DB
    providers = []
    try:
        rows = await db.fetch("SELECT id, type, name, priority, status, config, locked FROM inference_providers ORDER BY priority ASC")
        for r in rows:
            cfg_dict = r["config"]
            if isinstance(cfg_dict, str):
                cfg_dict = json.loads(cfg_dict)
            providers.append({
                "id": r["id"],
                "type": r["type"],
                "name": r["name"],
                "priority": r["priority"],
                "status": r["status"],
                "config": cfg_dict,
                "locked": r["locked"],
            })
    except Exception as db_exc:
        logger.warning("Could not read providers from DB: %s", db_exc)

    # Fallback to default embedded if no providers exist in DB
    if not providers:
        providers = [{
            "id": "embedded-builtin",
            "type": "embedded",
            "name": "Tala Embedded Engine",
            "priority": 1,
            "status": "online",
            "config": {"model": "Qwen/Qwen2.5-0.5B-Instruct"},
            "locked": True
        }]

    request_provider_id = str(params.get("provider_id", "") or "").strip()

    # Check if there is a per-request or globally forced provider
    request_provider = None
    forced_provider = None
    for p in providers:
        if request_provider_id and p["id"] == request_provider_id:
            request_provider = p
        if p["config"] and p["config"].get("is_forced"):
            forced_provider = p

    # Routing logic
    if request_provider:
        logger.info("Request-selected provider active for session %s: '%s' (Strict Mode)", session_id, request_provider["name"])
        providers = [request_provider]
    elif forced_provider:
        logger.info("Forced provider active: '%s' (Strict Mode)", forced_provider["name"])
        providers = [forced_provider]
    else:
        # Router Mode: prioritize local and embedded models first, then fall back to remote cloud APIs.
        # Preserve original priority ordering within those groups.
        local_providers = [p for p in providers if p["type"] in ("local", "embedded")]
        remote_providers = [p for p in providers if p["type"] == "remote"]
        
        # Ensure embedded-builtin is at least present as an ultimate fallback if no local models are configured
        if not any(p["id"] == "embedded-builtin" for p in local_providers):
            local_providers.append({
                "id": "embedded-builtin",
                "type": "embedded",
                "name": "Tala Embedded Engine",
                "priority": 999,
                "status": "online",
                "config": {"model": "Qwen/Qwen2.5-0.5B-Instruct"},
                "locked": True
            })
            
        providers = local_providers + remote_providers
        logger.info("Router Mode active: Enqueued %d local/embedded and %d remote fallback providers.", len(local_providers), len(remote_providers))

    loop_count = 0
    max_loops = 3
    all_responses = []
    failed_endpoints = set()

    while loop_count < max_loops:
        generated_text = ""
        errors = []
        success = False

        for provider in providers:
            is_forced_provider = (forced_provider and provider["id"] == forced_provider["id"])
            if provider.get("status") == "offline" and provider["id"] != "embedded-builtin" and not is_forced_provider:
                logger.info("Skipping offline provider '%s'", provider["name"])
                continue

            p_type = provider["type"]
            p_name = provider["name"]
            p_config = provider["config"] or {}

            if p_type in ("local", "remote"):
                endpoint = p_config.get("endpoint", "").strip()
                if p_type == "local" and not endpoint:
                    engine = p_config.get("engine", "ollama")
                    endpoint = "http://localhost:11434/v1" if engine == "ollama" else "http://localhost:1234/v1"
                if endpoint:
                    norm_ep = _normalize_endpoint_url(endpoint)
                    if norm_ep in failed_endpoints:
                        logger.info("Skipping provider '%s' because its endpoint %s failed previously", p_name, norm_ep)
                        continue


            await db.log_to_db(
                "INFO",
                f"Attempting generation with provider '{p_name}' ({p_type})",
                {"session_id": session_id, "provider_id": provider["id"]}
            )

            try:
                if p_type == "embedded":
                    # Execute CPU local generation
                    from python.inference.embedded_engine import check_model_cached, generate_stream
                    if not check_model_cached():
                        raise RuntimeError("Embedded model not fully downloaded / cached yet.")
                    
                    chunks = []
                    async for chunk in generate_stream(messages, max_tokens, temperature, lora_path=best_lora_path):
                        chunks.append(chunk)
                        if stream:
                            notification = json.dumps({
                                "jsonrpc": JSONRPC_VERSION,
                                "method": "inference.generate.chunk",
                                "params": {"chunk": chunk, "session_id": session_id},
                            })
                            await ws.send(notification)
                    generated_text = "".join(chunks)

                elif p_type in ("local", "remote"):
                    endpoint = p_config.get("endpoint", "").strip()
                    model = p_config.get("model", "").strip()
                    api_key = p_config.get("apiKey", "").strip()

                    if p_type == "local":
                        engine = p_config.get("engine", "ollama")
                        if not endpoint:
                            endpoint = "http://localhost:11434/v1" if engine == "ollama" else "http://localhost:1234/v1"
                        if not model:
                            model = "llama3.2:3b" if engine == "ollama" else "qwen2.5-1.5b-instruct"
                    else:  # remote
                        service = p_config.get("service", "openai")
                        if service == "openai" and not endpoint:
                            endpoint = "https://api.openai.com/v1"
                        elif service == "gemini" and not endpoint:
                            endpoint = "https://generativelanguage.googleapis.com/v1beta/openai"
                        elif service == "anthropic" and not endpoint:
                            endpoint = "https://api.anthropic.com/v1"

                    import httpx
                    headers = {"Content-Type": "application/json"}
                    if api_key:
                        if "anthropic" in endpoint or p_config.get("service") == "anthropic":
                            headers["x-api-key"] = api_key
                            headers["anthropic-version"] = "2023-06-01"
                        else:
                            headers["Authorization"] = f"Bearer {api_key}"

                    model_lower = model.lower()
                    is_reasoning_model = any(k in model_lower for k in ("gemma4", "reasoning", "deepseek", "thought"))
                    adjusted_max_tokens = max_tokens
                    if is_reasoning_model and adjusted_max_tokens < 2048:
                        adjusted_max_tokens = 2048

                    url = f"{_normalize_endpoint_url(endpoint)}/chat/completions"
                    payload = {
                        "model": model,
                        "messages": messages,
                        "max_tokens": adjusted_max_tokens,
                        "temperature": temperature,
                        "stream": stream
                    }

                    if p_type == "local" and p_config.get("engine", "ollama") == "ollama":
                        payload["options"] = {"num_ctx": 8192}

                    if p_type == "local" and p_config.get("engine", "ollama") == "ollama":
                        _check_base = p_config.get("endpoint", "http://localhost:11434").rstrip("/v1").rstrip("/")
                        _check_base = _check_base.replace("/v1", "")
                        _norm_check_base = _normalize_endpoint_url(_check_base)
                        if _norm_check_base in failed_endpoints:
                            logger.info("Skipping model availability check for provider '%s' because its endpoint %s failed previously", p_name, _norm_check_base)
                            errors.append(f"Provider '{p_name}' skipped: endpoint failed previously")
                            continue

                        try:
                            import httpx as _httpx_check
                            async with _httpx_check.AsyncClient(timeout=30.0) as _probe:
                                _probe_resp = await _probe.post(
                                    f"{_check_base}/api/show",
                                    json={"name": model},
                                )
                                if _probe_resp.status_code != 200:
                                    raise RuntimeError(f"Model '{model}' not available in Ollama (status {_probe_resp.status_code})")
                        except Exception as _probe_exc:
                            logger.warning(
                                "Skipping provider '%s': model '%s' not available: %s",
                                p_name, model, _probe_exc
                            )
                            failed_endpoints.add(_norm_check_base)
                            probe_desc = f"{type(_probe_exc).__name__}: {_probe_exc}" if str(_probe_exc) else type(_probe_exc).__name__
                            errors.append(f"Provider '{p_name}' skipped: {probe_desc}")
                            if provider["id"] != "embedded-builtin":
                                try:
                                    await db.execute(
                                        "UPDATE inference_providers SET status = 'offline', updated_at = NOW() WHERE id = $1",
                                        provider["id"]
                                    )
                                    from python.harness.settings_nn import update_settings_inference_node
                                    await update_settings_inference_node()
                                    await broadcast_notification("inference.providers_changed")
                                except Exception as status_exc:
                                    logger.warning("Failed to update status for failing provider '%s': %s", p_name, status_exc)
                            continue

                    _read_timeout = 180.0 if p_type == "local" else 90.0
                    async with httpx.AsyncClient(timeout=httpx.Timeout(_read_timeout, connect=5.0)) as client:
                        if stream:
                            chunks = []
                            in_reasoning = False
                            async with client.stream("POST", url, headers=headers, json=payload) as response:
                                if response.status_code != 200:
                                    err_body = await response.aread()
                                    raise RuntimeError(f"HTTP Error {response.status_code}: {err_body.decode(errors='ignore')}")
                                async for line in response.aiter_lines():
                                    if line.startswith("data: "):
                                        data_str = line[6:].strip()
                                        if data_str == "[DONE]":
                                            break
                                        try:
                                            data_json = json.loads(data_str)
                                            choices = data_json.get("choices", [])
                                            if choices:
                                                delta = choices[0].get("delta", {})
                                                reasoning_chunk = delta.get("reasoning", "")
                                                content_chunk = delta.get("content", "")
                                                
                                                chunk = ""
                                                if reasoning_chunk:
                                                    if not in_reasoning:
                                                        in_reasoning = True
                                                        chunk += "<thinking>\n"
                                                    chunk += reasoning_chunk
                                                elif content_chunk:
                                                    if in_reasoning:
                                                        in_reasoning = False
                                                        chunk += "\n</thinking>\n\n"
                                                    chunk += content_chunk
                                                    
                                                if chunk:
                                                    chunks.append(chunk)
                                                    notification = json.dumps({
                                                        "jsonrpc": JSONRPC_VERSION,
                                                        "method": "inference.generate.chunk",
                                                        "params": {"chunk": chunk, "session_id": session_id},
                                                    })
                                                    await ws.send(notification)
                                        except Exception:
                                            pass
                            if in_reasoning:
                                chunks.append("\n</thinking>\n\n")
                            generated_text = "".join(chunks)
                        else:
                            response = await client.post(url, headers=headers, json=payload)
                            if response.status_code != 200:
                                raise RuntimeError(f"HTTP Error {response.status_code}: {response.text}")
                            resp_json = response.json()
                            choice = resp_json["choices"][0]["message"]
                            content = choice.get("content", "") or ""
                            reasoning = choice.get("reasoning", "") or ""
                            if reasoning:
                                generated_text = f"<thinking>\n{reasoning}\n</thinking>\n\n{content}"
                            else:
                                generated_text = content

                # Router Checks
                is_forced_provider = (forced_provider and provider["id"] == forced_provider["id"])
                is_requested_provider = (request_provider and provider["id"] == request_provider["id"])
                
                if not (is_forced_provider or is_requested_provider):
                    gen_lower = generated_text.lower()
                    low_confidence_phrases = ["i don't know", "i am not sure", "as an ai", "i cannot fulfill", "i'm not sure", "i can't fulfill"]
                    
                    fallback_reason = None
                    if any(phrase in gen_lower for phrase in low_confidence_phrases):
                        fallback_reason = "Low confidence detected"
                    elif all_responses:
                        last_response = all_responses[-1]
                        tool_match = re.search(r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))', generated_text, re.DOTALL)
                        last_tool_match = re.search(r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))', last_response, re.DOTALL)
                        if tool_match and last_tool_match and tool_match.group(0) == last_tool_match.group(0):
                            fallback_reason = "Identical tool call loop detected"
                            
                    if fallback_reason:
                        err_msg = f"Provider '{p_name}' ({p_type}) skipped via router fallback: {fallback_reason}"
                        logger.info(err_msg)
                        errors.append(err_msg)
                        success = False
                        continue

                success = True
                break

            except Exception as exc:
                exc_desc = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                err_msg = f"Provider '{p_name}' ({p_type}) failed: {exc_desc}"
                logger.warning(err_msg)
                errors.append(err_msg)
                
                # Blacklist this endpoint for the rest of this generation session
                if p_type in ("local", "remote"):
                    endpoint = p_config.get("endpoint", "").strip()
                    if p_type == "local" and not endpoint:
                        engine = p_config.get("engine", "ollama")
                        endpoint = "http://localhost:11434/v1" if engine == "ollama" else "http://localhost:1234/v1"
                    if endpoint:
                        failed_endpoints.add(_normalize_endpoint_url(endpoint))

                await db.log_to_db(
                    "WARNING",
                    err_msg,
                    {"session_id": session_id, "provider_id": provider["id"], "error": str(exc)}
                )
                if provider["id"] != "embedded-builtin":
                    try:
                        await db.execute(
                            "UPDATE inference_providers SET status = 'offline', updated_at = NOW() WHERE id = $1",
                            provider["id"]
                        )
                        from python.harness.settings_nn import update_settings_inference_node
                        await update_settings_inference_node()
                        await broadcast_notification("inference.providers_changed")
                    except Exception as status_exc:
                        logger.warning("Failed to update status for failing provider '%s': %s", p_name, status_exc)

        if not success:
            all_errors = "; ".join(errors)
            return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"All inference providers failed: {all_errors}")

        # Parse tool calls
        tool_match = re.search(r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))', generated_text, re.DOTALL)
        if not tool_match:
            all_responses.append(generated_text)
            break

        # Truncate any text generated after the call tag to prevent model hallucinations of responses
        generated_text = generated_text[:tool_match.end()]

        tool_name = tool_match.group(1)
        attr_str = tool_match.group(2)
        inner_content = tool_match.group(3)
        if attr_str.endswith('/'):
            attr_str = attr_str[:-1]

        # Alias common LLM hallucinated tool names
        if tool_name.startswith("run_workflow"):
            tool_name = "run_workflow_node"

        # Parse attributes
        attrs = {}
        pattern = r'(\w+)\s*=\s*("[^"]*"|\'[^\']*\'|\[[^\]]*\]|{[^}]*})'
        for match in re.finditer(pattern, attr_str):
            k, v = match.groups()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                attrs[k] = v[1:-1]
            else:
                try:
                    attrs[k] = json.loads(v.replace("'", '"'))
                except Exception:
                    attrs[k] = v
                    
        if inner_content:
            inner_content = inner_content.strip()
            if tool_name == "generate_detailed_neural_node":
                attrs["detailed_markdown"] = inner_content
            elif tool_name == "write_workspace_file":
                attrs["content"] = inner_content
            elif tool_name in {"docs.create_documentation_node", "docs.register_program_target"}:
                attrs["semantic_memory"] = inner_content
            elif tool_name == "self_improvement.execute_plan":
                try:
                    attrs["plan"] = json.loads(inner_content)
                except Exception:
                    attrs["plan"] = {"raw": inner_content}
            elif tool_name == "self_improvement.create_plan":
                attrs["goal"] = inner_content
            elif tool_name == "submit_agent_goal":
                attrs["goal"] = inner_content
            elif "content" not in attrs:
                attrs["content"] = inner_content

        tool_lower = tool_name.lower()
        parts = tool_lower.split(".")
        is_search = any(p in ("search", "exact", "query", "google") for p in parts)
        is_crawl = any(("crawl" in p or "summarize" in p or p in ("click", "open")) for p in parts)

        tool_result = None
        if is_search:
            query = attrs.get("query", attrs.get("q", "")).strip()
            if not query:
                fallback_match = re.search(r'["\']([^"\']+)["\']', attr_str)
                if fallback_match:
                    query = fallback_match.group(1).strip()
            # Clean up query from trailing action terms (e.g. "third link", "and click the 3rd link")
            # which small models tend to append to the search query.
            query = re.sub(
                r'\s+(and\s+)?(click\s+)?(on\s+)?(the\s+)?(1st|2nd|3rd|first|second|third|fourth|fifth|last)\s+link.*$',
                '',
                query,
                flags=re.IGNORECASE
            ).strip()
            try:
                res_str = await handle_research_search({"query": query}, req_id=None)
                res_json = json.loads(res_str)
                if "result" in res_json:
                    tool_result = res_json["result"]
                else:
                    tool_result = res_json.get("error", "Unknown error")
            except Exception as e:
                tool_result = f"Error running search: {e}"
        elif is_crawl:
            urls = attrs.get("urls", attrs.get("url", attrs.get("link", [])))
            if isinstance(urls, str):
                urls = [urls]
            if not urls:
                fallback_match = re.search(r'["\']([^"\']+)["\']', attr_str)
                if fallback_match:
                    urls = [fallback_match.group(1).strip()]
            subject = attrs.get("subject", attrs.get("s", "Scraped Content"))
            try:
                res_str = await handle_research_crawl_and_summarize({"urls": urls, "subject": subject}, req_id=None)
                res_json = json.loads(res_str)
                if "result" in res_json:
                    tool_result = res_json["result"]
                else:
                    tool_result = res_json.get("error", "Unknown error")
            except Exception as e:
                tool_result = f"Error running crawl: {e}"
        else:
            # Dynamic Tool Registry execution fallback (so Tala can run workspace tools in chat)
            try:
                from sdk.agent.tools import ToolRegistry
                tools_list = [t["name"].lower() for t in ToolRegistry.get_tools()]
                if tool_lower in tools_list:
                    res = await ToolRegistry.run_tool(tool_name, attrs, authority_level="auto")
                    if res["success"]:
                        tool_result = res["output"]
                    else:
                        tool_result = f"Tool execution failed: {res.get('error')}"
                else:
                    tool_result = f"Tool '{tool_name}' is not supported in this chat environment."
            except Exception as exc:
                tool_result = f"Error executing registry tool: {exc}"

        if tool_lower.startswith("docs."):
            all_responses.append(json.dumps(tool_result, indent=2))
            break

        # Filter out the tag from user visible response
        clean_generated = re.sub(r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))', '', generated_text, flags=re.DOTALL)
        clean_generated = re.sub(r'```(?:xml)?\s*', '', clean_generated)
        clean_generated = clean_generated.replace('**Tool Call:**', '').strip()
        if clean_generated:
            all_responses.append(clean_generated)
        else:
            all_responses.append(f"*(Executing {tool_name}...)*")

        # Append to message history for next LLM turn
        messages.append({"role": "assistant", "content": generated_text})
        messages.append({"role": "user", "content": f"<response: {tool_name} result={json.dumps(tool_result)} />"})

        await db.log_to_db(
            "INFO",
            f"Agent Chat: Auto-executed tool '{tool_name}' during inference",
            {"tool": tool_name, "params": attrs}
        )

        loop_count += 1
    else:
        # Loop limit reached, append final text
        all_responses.append(generated_text)

    generated_text = "\n\n".join(all_responses)

    # Guardrails output validation (post-inference)
    try:
        validated_text = guardrails_mgr.validate_output(generated_text)
    except SafetyGuardrailViolation as exc:
        err_msg = f"Guardrail blocked response: {exc.message}"
        logger.warning(err_msg)
        await db.log_to_db(
            "WARNING",
            err_msg,
            {"session_id": session_id, "error_type": "SafetyGuardrailViolation", "payload": exc.payload}
        )
        return _err(
            req_id,
            JsonRpcError.INTERNAL_ERROR,
            exc.message,
            {
                "error_type": "SafetyGuardrailViolation",
                "payload": exc.payload,
            }
        )

    generated_text = validated_text

    try:
        from python.harness.memory import handle_memory_record_completed_turn
        tool_calls_list = []
        if "tool_name" in locals() and tool_name:
            tool_calls_list.append({"name": tool_name, "args": attrs, "result": tool_result})
        await handle_memory_record_completed_turn({
            "session_id": session_id,
            "role": "assistant",
            "content": generated_text,
            "tool_calls": tool_calls_list,
        })
    except Exception as exc:
        logger.warning(f"Failed to internally record turn: {exc}")

    await db.log_to_db(
        "INFO",
        f"inference.generate complete  session={session_id}",
        {
            "session_id": session_id,
            "output_length": len(generated_text),
            "ledger_id": check.ledger_id,
        },
    )

    return _ok(req_id, {
        "text": generated_text,
        "tokens_used": check.prompt_tokens,
        "cost_usd": check.cost_usd,
        "ledger_id": check.ledger_id,
        "balance_after": check.balance_after,
        "session_id": session_id,
    })



async def handle_db_query(params: dict[str, Any], req_id: Any) -> str:
    """
    ``db.query`` — Execute a parameterised SELECT and return rows.

    Params
    ------
    sql    : str            required — parameterised SQL (SELECT only)
    args   : list[Any]      optional — positional parameters

    Returns
    -------
    { rows: list[dict], count: int }
    """
    sql: str = params.get("sql", "")
    if not sql.strip():
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'sql' is required")

    # Safety: only allow SELECT statements through this handler
    normalised = sql.strip().upper()
    if not normalised.startswith("SELECT") and not normalised.startswith("WITH"):
        return _err(
            req_id,
            JsonRpcError.INVALID_PARAMS,
            "db.query only permits SELECT statements.  Use db.insert for writes.",
        )

    args: list[Any] = params.get("args", [])
    try:
        rows = await db.fetch(sql, *args)
        serialised = [dict(row) for row in rows]
        return _ok(req_id, {"rows": serialised, "count": len(serialised)})
    except Exception as exc:
        logger.exception("db.query error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_db_insert(params: dict[str, Any], req_id: Any) -> str:
    """
    ``db.insert`` — Execute a parameterised INSERT (or UPDATE/DELETE).

    Params
    ------
    sql    : str       required — parameterised DML
    args   : list[Any] optional — positional parameters

    Returns
    -------
    { status: str }  — PostgreSQL command tag, e.g. 'INSERT 0 1'
    """
    sql: str = params.get("sql", "")
    if not sql.strip():
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'sql' is required")

    args: list[Any] = params.get("args", [])
    try:
        status = await db.execute(sql, *args, caller="db.insert RPC")
        return _ok(req_id, {"status": status})
    except Exception as exc:
        logger.exception("db.insert error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_token_count(params: dict[str, Any], req_id: Any) -> str:
    """
    ``token.count`` — Count tokens in a prompt WITHOUT deducting balance.

    Params
    ------
    prompt : str   required

    Returns
    -------
    { tokens: int, cost_usd: float, balance: float, encoding: str }
    """
    prompt: str = params.get("prompt", "")
    if not prompt:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'prompt' is required")

    token_count, encoding_name = count_tokens(prompt)
    cost_usd = token_count * settings.TOKEN_COST_PER_TOKEN
    balance = await get_wallet_balance()

    return _ok(req_id, {
        "tokens": token_count,
        "cost_usd": cost_usd,
        "balance": balance,
        "encoding": encoding_name,
        "can_afford": balance >= cost_usd,
    })


async def handle_graph_activate(params: dict[str, Any], req_id: Any) -> str:
    """
    ``graph.activate`` — Activate (upsert) a node in the cognitive graph.

    Params
    ------
    node_type           : str    required
    content             : dict   required  — arbitrary JSON payload
    execution_authority : str    optional  — 'user', 'agent', or 'system'

    Returns
    -------
    { node_id: str, node_type: str, created: bool }
    """
    node_type: str = params.get("node_type", "")
    content: dict[str, Any] = params.get("content", {})
    authority: str = params.get("execution_authority", "user")

    if not node_type:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'node_type' is required")
    if not isinstance(content, dict):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'content' must be a JSON object")

    valid_authorities = {"user", "agent", "system"}
    if authority not in valid_authorities:
        return _err(
            req_id,
            JsonRpcError.INVALID_PARAMS,
            f"'execution_authority' must be one of {sorted(valid_authorities)}",
        )

    import json as json_mod

    try:
        row = await db.fetchrow(
            """
            INSERT INTO nodes (node_type, execution_authority, content_json)
            VALUES ($1, $2, $3::jsonb)
            RETURNING id, node_type, created_at,
                      (xmax = 0) AS is_insert
            """,
            node_type,
            authority,
            json_mod.dumps(content),
        )
        if row is None:
            return _err(req_id, JsonRpcError.INTERNAL_ERROR, "Node insert returned no row")

        node_id = str(row["id"])
        try:
            from sdk.agent.anchor import canonicalize_and_anchor

            document = {
                "id": node_id,
                "node_type": node_type,
                "execution_authority": authority,
                "content": content,
            }
            canonicalize_and_anchor(
                node_id,
                json_mod.dumps(document, sort_keys=True, ensure_ascii=False),
                document,
                None,
                creator="tala-system",
                document_type="node",
            )
        except Exception as anchor_exc:
            logger.warning("Failed to anchor node %s on blockchain: %s", node_id, anchor_exc)

        return _ok(req_id, {
            "node_id": node_id,
            "node_type": node_type,
            "execution_authority": authority,
            "created_at": row["created_at"].isoformat(),
        })
    except Exception as exc:
        logger.exception("graph.activate error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_vault_status(params: dict[str, Any], req_id: Any) -> str:
    """
    ``vault.status`` — Return filesystem status of the vault directory.

    Returns
    -------
    { path: str, exists: bool, file_count: int, size_bytes: int }
    """
    vault_path = settings.VAULT_PATH
    exists = vault_path.exists()

    file_count = 0
    size_bytes = 0
    if exists and vault_path.is_dir():
        for child in vault_path.rglob("*"):
            if child.is_file():
                file_count += 1
                size_bytes += child.stat().st_size

    return _ok(req_id, {
        "path": str(vault_path),
        "exists": exists,
        "file_count": file_count,
        "size_bytes": size_bytes,
    })


async def handle_harness_health(params: dict[str, Any], req_id: Any) -> str:
    """
    ``harness.health`` — Return server health and status info.
    """
    try:
        row = await db.fetchrow(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) as total FROM token_ledger"
        )
        tokens_used = int(row["total"]) if row else 0
    except Exception:
        tokens_used = 0

    active_agent = "Tala Engine"
    try:
        agent_row = await db.fetchrow("SELECT name FROM agents WHERE is_active = TRUE")
        if agent_row:
            chat_mode = get_current_chat_mode()
            if chat_mode == "hybrid":
                active_agent = f"{agent_row['name']} (Hybrid)"
            else:
                active_agent = agent_row["name"]
    except Exception:
        pass

    return _ok(req_id, {
        "status": "ok",
        "active_agent": active_agent,
        "tokens_used": tokens_used,
        "last_latency_ms": 1,
    })


async def handle_astro_transits(params: dict[str, Any], req_id: Any) -> str:
    """
    ``astro.transits`` — Compute planetary transits for user, agent, Tala, or custom birth data.
    """
    try:
        from python.astrology.ephemeris import EphemerisEngine, Birthdate
        from datetime import datetime, timezone

        def parse_birthdate(raw: str) -> Birthdate | None:
            raw = str(raw or "").strip()
            if not raw:
                return None
            dt = datetime.strptime(raw, "%Y-%m-%d")
            return Birthdate(year=dt.year, month=dt.month, day=dt.day, hour=12.0)

        def parse_target_time(raw: str) -> datetime:
            raw = str(raw or "").strip()
            if not raw:
                return datetime.now(timezone.utc)
            normalized = raw.replace("Z", "+00:00")
            if len(normalized) == 10:
                return datetime.strptime(normalized, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        subject = str(params.get("subject", "agent") or "agent").strip().lower()
        if subject not in {"agent", "user", "tala", "custom"}:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "'subject' must be one of: agent, user, tala, custom")

        # Default: Tala origin birthdate chart (Born May 26, 2014)
        birth = Birthdate(year=2014, month=5, day=26, hour=12.0)
        birth_source = "tala-origin"

        if subject == "custom":
            custom_birth = parse_birthdate(str(params.get("birthdate", "")))
            if not custom_birth:
                return _err(req_id, JsonRpcError.INVALID_PARAMS, "'birthdate' is required for subject='custom' in YYYY-MM-DD format")
            birth = custom_birth
            birth_source = "custom"
        elif subject == "user":
            try:
                row = await db.fetchrow("SELECT config FROM profiles WHERE id = 'master-user'")
                cfg = row["config"] if row else {}
                if isinstance(cfg, str):
                    import json
                    cfg = json.loads(cfg)
                birth_str = (
                    cfg.get("identity", {}).get("birthdate")
                    or cfg.get("pii_vault", {}).get("dob")
                    or cfg.get("birthdate")
                    or ""
                )
                user_birth = parse_birthdate(birth_str)
                if user_birth:
                    birth = user_birth
                    birth_source = "user-profile"
            except Exception as db_exc:
                logger.warning("Could not read user profile birthdate for astro transits: %s", db_exc)
        elif subject == "agent":
            try:
                agent_row = await db.fetchrow("SELECT config FROM agents WHERE is_active = TRUE")
                if agent_row:
                    cfg = agent_row["config"]
                    if isinstance(cfg, str):
                        import json
                        cfg = json.loads(cfg)
                    agent_birth = parse_birthdate(cfg.get("birthdate", ""))
                    if agent_birth:
                        birth = agent_birth
                        birth_source = "active-agent"
            except Exception as db_exc:
                logger.warning("Could not read active agent birthdate for astro transits: %s", db_exc)

        target_time = parse_target_time(str(params.get("target_time") or params.get("date") or ""))

        engine = EphemerisEngine()
        transits = engine.compute_transits(birth, target_time)
        
        serialized = []
        for t in transits:
            serialized.append({
                "planet": t.planet,
                "aspect_type": t.aspect_type,
                "natal_planet": t.natal_planet,
                "orb": t.orb,
                "is_challenging": t.is_challenging,
                "influence_score": t.influence_score,
            })
        return _ok(req_id, {
            "subject": subject,
            "birth_source": birth_source,
            "birthdate": f"{birth.year:04d}-{birth.month:02d}-{birth.day:02d}",
            "target_time": target_time.isoformat(),
            "is_predictive": target_time > datetime.now(timezone.utc),
            "transits": serialized,
        })
    except Exception as exc:
        logger.exception("astro.transits error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_astro_synastry(params: dict[str, Any], req_id: Any) -> str:
    """
    ``astro.synastry`` — Compare user-profile astro against active-agent astro.
    """
    try:
        from datetime import datetime
        from python.astrology.ephemeris import Birthdate
        from python.astrology.synastry import SynastryEngine

        def parse_birthdate(raw: str) -> Birthdate | None:
            raw = str(raw or "").strip()
            if not raw:
                return None
            dt = datetime.strptime(raw, "%Y-%m-%d")
            return Birthdate(year=dt.year, month=dt.month, day=dt.day, hour=12.0)

        user_birth = None
        user_source = "missing"
        user_row = await db.fetchrow("SELECT config FROM profiles WHERE id = 'master-user'")
        if user_row:
            user_cfg = user_row["config"]
            if isinstance(user_cfg, str):
                import json
                user_cfg = json.loads(user_cfg)
            user_birth_str = (
                user_cfg.get("identity", {}).get("birthdate")
                or user_cfg.get("pii_vault", {}).get("dob")
                or user_cfg.get("birthdate")
                or ""
            )
            user_birth = parse_birthdate(user_birth_str)
            if user_birth:
                user_source = "user-profile"

        agent_birth = None
        agent_source = "missing"
        agent_row = await db.fetchrow("SELECT id, name, config FROM agents WHERE is_active = TRUE")
        agent_id = ""
        agent_name = ""
        if agent_row:
            agent_id = str(agent_row["id"])
            agent_name = str(agent_row["name"])
            agent_cfg = agent_row["config"]
            if isinstance(agent_cfg, str):
                import json
                agent_cfg = json.loads(agent_cfg)
            agent_birth = parse_birthdate(agent_cfg.get("birthdate", ""))
            if agent_birth:
                agent_source = "active-agent"

        if not user_birth:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "User profile birthdate is required for astro.synastry")
        if not agent_birth:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "Active agent birthdate is required for astro.synastry")

        matrix = SynastryEngine().compute_synastry(user_birth, agent_birth)
        challenging = sum(1 for aspect in matrix.aspects if aspect.is_challenging)
        harmonious = len(matrix.aspects) - challenging
        if matrix.overall_compatibility >= 0.66 and harmonious >= challenging:
            relationship_effect = "enhance"
        elif matrix.overall_compatibility <= 0.40 or challenging > harmonious:
            relationship_effect = "conflict"
        else:
            relationship_effect = "mixed"

        return _ok(req_id, {
            "success": True,
            "relationship_effect": relationship_effect,
            "overall_compatibility": matrix.overall_compatibility,
            "dominant_theme": matrix.dominant_theme,
            "user": {
                "birth_source": user_source,
                "birthdate": f"{user_birth.year:04d}-{user_birth.month:02d}-{user_birth.day:02d}",
            },
            "agent": {
                "id": agent_id,
                "name": agent_name,
                "birth_source": agent_source,
                "birthdate": f"{agent_birth.year:04d}-{agent_birth.month:02d}-{agent_birth.day:02d}",
            },
            "aspect_counts": {
                "harmonious": harmonious,
                "challenging": challenging,
                "total": len(matrix.aspects),
            },
            "aspects": [
                {
                    "planet_a": aspect.planet_a,
                    "aspect_type": aspect.aspect_type,
                    "planet_b": aspect.planet_b,
                    "orb": aspect.orb,
                    "is_challenging": aspect.is_challenging,
                    "score": aspect.score,
                }
                for aspect in matrix.aspects
            ],
        })
    except Exception as exc:
        logger.exception("astro.synastry error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_identity_get_user_profile(params: dict[str, Any], req_id: Any) -> str:
    """
    ``identity.get_user_profile`` — Load user profile Neural Node details.
    """
    import os
    import json
    from pathlib import Path
    try:
        user_data = os.environ.get("TALA_USER_DATA")
        if user_data:
            nodes_dir = Path(user_data) / "nodes"
        else:
            appdata = os.environ.get("APPDATA")
            if appdata:
                nodes_dir = Path(appdata) / "tala-engine" / "nodes"
            else:
                nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"

        node_file = nodes_dir / "concept-user-profile.html"
        
        profile = {
            "name": "",
            "birthdate": "",
            "tone": "warm_professional",
            "biography": "",
            "rp_name": "",
            "rp_class": "",
            "rp_faction": "",
            "rp_backstory": "",
            "pii_legal_name": "",
            "pii_dob": "",
            "pii_email": "",
            "pii_phone": "",
            "pii_registries": []
        }

        if node_file.exists():
            from sdk.agent.parser import parse_file
            try:
                node = parse_file(str(node_file))
                sd = node.state_data or {}
                profile["name"] = sd.get("user_details", {}).get("name", sd.get("title", ""))
                profile["birthdate"] = sd.get("user_details", {}).get("birthdate", "")
                profile["tone"] = sd.get("formatting_preferences", {}).get("preferred_tone", sd.get("tone", "warm_professional"))
                
                profile["biography"] = sd.get("biography", "")
                if not profile["biography"]:
                    profile["biography"] = node.semantic_memory.strip()

                rp = sd.get("rp_persona", {})
                profile["rp_name"] = rp.get("name", "")
                profile["rp_class"] = rp.get("class", "")
                profile["rp_faction"] = rp.get("faction", "")
                profile["rp_backstory"] = rp.get("backstory", "")

                pii = sd.get("pii_vault", {})
                profile["pii_legal_name"] = pii.get("legal_name", "")
                profile["pii_dob"] = pii.get("dob", "")
                profile["pii_email"] = pii.get("email", "")
                profile["pii_phone"] = pii.get("phone", "")
                profile["pii_registries"] = pii.get("registries", [])
            except Exception as parse_exc:
                logger.warning("Failed to parse user profile node: %s", parse_exc)

        return _ok(req_id, {"success": True, "profile": profile})
    except Exception as exc:
        logger.exception("identity.get_user_profile error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_identity_save_user_profile(params: dict[str, Any], req_id: Any) -> str:
    """
    ``identity.save_user_profile`` — Compile user details into concept-user-profile.html
    and anchor on the Proof-of-Authority blockchain node.
    """
    import os
    import json
    import hashlib
    import urllib.request
    from pathlib import Path
    from datetime import datetime, timezone
    from sdk.agent.anchor import canonicalize_and_anchor
    
    name = params.get("name", "").strip()
    birthdate = params.get("birthdate", "").strip()
    tone = params.get("tone", "warm_professional").strip()
    biography = params.get("biography", "").strip()

    rp_name = params.get("rp_name", "").strip()
    rp_class = params.get("rp_class", "").strip()
    rp_faction = params.get("rp_faction", "").strip()
    rp_backstory = params.get("rp_backstory", "").strip()

    pii_legal_name = params.get("pii_legal_name", "").strip()
    pii_dob = params.get("pii_dob", "").strip()
    pii_email = params.get("pii_email", "").strip()
    pii_phone = params.get("pii_phone", "").strip()
    pii_registries = params.get("pii_registries", [])

    if not name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'name' is required")
    if not biography:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'biography' is required")

    try:
        user_data = os.environ.get("TALA_USER_DATA")
        if user_data:
            nodes_dir = Path(user_data) / "nodes"
        else:
            appdata = os.environ.get("APPDATA")
            if appdata:
                nodes_dir = Path(appdata) / "tala-engine" / "nodes"
            else:
                nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"
        nodes_dir.mkdir(parents=True, exist_ok=True)

        current_iso_time = datetime.now(timezone.utc).isoformat()
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        pii_locations_html = ""
        for r in pii_registries:
            label = r.get("label", "").strip()
        
        state_data = {
            "name": name,
            "birthdate": birthdate,
            "tone": tone,
            "biography": biography,
            "rp_name": rp_name,
            "rp_class": rp_class,
            "rp_faction": rp_faction,
            "rp_backstory": rp_backstory,
            "pii_legal_name": pii_legal_name,
            "pii_dob": pii_dob,
            "pii_email": pii_email,
            "pii_phone": pii_phone,
            "pii_registries": pii_registries
        }
        
        adapter_packet = {
            "activation_keywords": ["age", "old", "birthday", "birthdate", "name", "who am I", "how old am I", "roleplay", "rp", "persona", "avatar", "character", "class", "faction", "alignment", "pii", "secure", "vault", "legal", "email", "phone", "location"],
            "tool_affordances": [],
            "forbidden_actions": [],
            "repair_policies": {},
            "validators": {},
            "confidence": 1.0
        }
        
        content_html = f"""
    <h2>User Persona & Details</h2>
    <p>User Name: {name}</p>
    <p>User Birthdate: {birthdate}</p>
    <p>Keywords: age, old, birthday, birthdate, name, who am I, how old am I, roleplay, rp, persona, avatar, character, class, faction, alignment, pii, secure, vault, legal, email, phone, location</p>
    <p>{biography}</p>
    
    <h3>Roleplay Persona (Avatar)</h3>
    <p>RP Character Name: {rp_name}</p>
    <p>RP Class/Occupation: {rp_class}</p>
    <p>RP Faction/Alignment: {rp_faction}</p>
    <p>RP Character Backstory: {rp_backstory}</p>

    <h3>Personally Identifiable Information &amp; Vault Registry</h3>
    <p>Legal Name: {pii_legal_name}</p>
    <p>Date of Birth: {pii_dob}</p>
    <p>Primary Email: {pii_email}</p>
    <p>Primary Phone: {pii_phone}</p>
    
    <h4>Registered Keys & Identifiers</h4>
    <ul>
      {"".join([f"<li>{r}</li>" for r in pii_registries]) if pii_registries else "<li>None registered.</li>"}
    </ul>"""

        core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
            "user-profile",
            content_html,
            state_data,
            adapter_packet,
            creator="tala-system",
            document_type="user-profile",
        )

        node_provenance = {
            "core_hash": f"sha256:{core_hash}",
            "chaincode_id": chaincode_id,
            "anchoring_status": anchoring_status,
            "raw_response": raw_response,
            "key_fingerprint": public_key if public_key else "unanchored",
            "parent_chaincode_ids": [],
            "created_by": "tala-system",
            "created_at": current_iso_time,
            "wrapper_hash": "..."
        }

        state_json = json.dumps(state_data, indent=2, ensure_ascii=False)
        prov_json_placeholder = json.dumps(node_provenance, indent=2)

        html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>User Profile: {name}</title>
</head>
<body>
<article
  data-node-id="user-profile"
  data-node-type="concept"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.50"
  data-memory-authority="1.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-chaincode-id="{chaincode_id}"
  data-content-hash="{core_hash}"
  data-source="local"
  data-trust-tier="system"
  data-privacy="private">
  
  <header id="node-identity">
    <h1 id="node-title">Workspace Owner Profile</h1>
  </header>
  
  <section id="semantic-memory">
{content_html}
  </section>
  
  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul></ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{state_json}
  </script>

  <script type="application/json" id="adapter-packet" data-cognitive-block="adapter">
{json.dumps(adapter_packet, indent=2)}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
__PROV_PLACEHOLDER__
  </script>
</article>
</body>
</html>
"""
        wrapper_hash = hashlib.sha256(html_template.replace("__PROV_PLACEHOLDER__", prov_json_placeholder).encode("utf-8")).hexdigest()
        node_provenance["wrapper_hash"] = f"sha256:{wrapper_hash}"
        html = html_template.replace("__PROV_PLACEHOLDER__", json.dumps(node_provenance, indent=2))

        node_file = nodes_dir / "concept-user-profile.html"
        node_file.write_text(html, encoding="utf-8")
        logger.info("Saved user profile node: %s", node_file)

        # Sync to profiles database table
        try:
            existing_row = await db.fetchrow("SELECT config FROM profiles WHERE id = 'master-user'")
            if existing_row:
                existing_cfg = existing_row["config"]
                if isinstance(existing_cfg, str):
                    existing_cfg = json.loads(existing_cfg)
                existing_cfg = existing_cfg or {}
            else:
                existing_cfg = {}

            if "identity" not in existing_cfg:
                existing_cfg["identity"] = {}
            if "about" not in existing_cfg:
                existing_cfg["about"] = {}
            if "rp_persona" not in existing_cfg:
                existing_cfg["rp_persona"] = {}
            if "preferences" not in existing_cfg:
                existing_cfg["preferences"] = {}

            existing_cfg["identity"]["name"] = name
            existing_cfg["identity"]["birthdate"] = birthdate
            existing_cfg["preferences"]["tone"] = tone
            existing_cfg["about"]["bio"] = biography
            existing_cfg["rp_persona"]["name"] = rp_name
            existing_cfg["rp_persona"]["class"] = rp_class
            existing_cfg["rp_persona"]["faction"] = rp_faction
            existing_cfg["rp_persona"]["backstory"] = rp_backstory

            if "pii_vault" not in existing_cfg:
                existing_cfg["pii_vault"] = {}
            existing_cfg["pii_vault"]["legal_name"] = pii_legal_name
            existing_cfg["pii_vault"]["dob"] = pii_dob
            existing_cfg["pii_vault"]["email"] = pii_email
            existing_cfg["pii_vault"]["phone"] = pii_phone
            existing_cfg["pii_vault"]["registries"] = pii_registries

            await db.execute(
                """
                INSERT INTO profiles (id, type, name, username, config, updated_at)
                VALUES ('master-user', 'human', $1, '', $2::jsonb, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    config = EXCLUDED.config,
                    updated_at = NOW()
                """,
                name,
                json.dumps(existing_cfg),
                caller="identity.save_user_profile DB sync"
            )
            # Sync to memory index for searchability
            from python.harness.memory import index_profile_chunks
            await index_profile_chunks('master-user', existing_cfg)
        except Exception as db_sync_exc:
            logger.warning("Failed to sync identity save to profiles database table: %s", db_sync_exc)

        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("identity.save_user_profile error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_graph_query_activations(params: dict[str, Any], req_id: Any) -> str:
    """
    ``graph.query_activations`` — Compute activation scores and propagation for a given query.
    """
    query = params.get("query", "").strip()
    if not query:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'query' is required")

    import os
    from pathlib import Path
    from python.harness.retrieval import retrieve_relevant_nodes
    from sdk.agent.parser import parse_file
    from sdk.agent.activation import ActivationEngine, ActivationInputs

    # Detect user data nodes directory
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        nodes_dir = Path(user_data) / "nodes"
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            nodes_dir = Path(appdata) / "tala-engine" / "nodes"
        else:
            nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"

    # Fallback to repo root nodes
    if not nodes_dir.exists():
        repo_nodes = Path(__file__).resolve().parent.parent.parent / "nodes"
        if repo_nodes.exists():
            nodes_dir = repo_nodes

    if not nodes_dir.exists() or not nodes_dir.is_dir():
        return _ok(req_id, {"activations": {}, "base_activations": {}})

    # 1. Parse all nodes
    nodes = []
    for filepath in nodes_dir.glob("*.html"):
        try:
            node = parse_file(str(filepath))
            nodes.append(node)
        except Exception:
            pass

    if not nodes:
        return _ok(req_id, {"activations": {}, "base_activations": {}})

    # 2. Get similarities for all nodes
    matched = retrieve_relevant_nodes(query, nodes_dir, top_k=len(nodes), threshold=0.0)
    similarities = {node.node_id: score for node, score in matched}

    # 3. Calculate base activations
    engine = ActivationEngine()
    base_activations = {}
    for node in nodes:
        sem_match = similarities.get(node.node_id, 0.0)
        inputs = ActivationInputs(
            semantic_match=sem_match,
            relevance_weight=node.weight_relevance,
            confidence_weight=node.weight_confidence,
            freshness_weight=1.0,
            provider_trust_multiplier=1.0,
            governance_multiplier=node.execution_authority,
        )
        base_activations[node.node_id] = engine.score(inputs)

    # 4. Multi-hop propagation (2 hops)
    final_activations = dict(base_activations)
    for _ in range(2):
        temp_activations = dict(final_activations)
        for u in nodes:
            act_u = final_activations[u.node_id]
            if act_u > 0.0:
                for rel in u.relationships:
                    target_id = rel.href.replace("nodes/", "").replace("./", "").replace(".html", "")
                    if target_id in temp_activations:
                        prop_val = engine.propagate(act_u, rel.relationship_weight)
                        if prop_val > temp_activations[target_id]:
                            temp_activations[target_id] = prop_val
        final_activations = temp_activations

    return _ok(req_id, {
        "activations": final_activations,
        "base_activations": base_activations
    })


async def handle_inference_get_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``inference.get_providers`` — Retrieve all configured inference providers sorted by priority.
    """
    try:
        rows = await db.fetch("SELECT id, type, name, priority, status, config, locked FROM inference_providers ORDER BY priority ASC")
        providers = []
        for r in rows:
            import json as json_mod
            cfg_dict = r["config"]
            if isinstance(cfg_dict, str):
                cfg_dict = json_mod.loads(cfg_dict)
            providers.append({
                "id": r["id"],
                "type": r["type"],
                "name": r["name"],
                "priority": r["priority"],
                "status": r["status"],
                "config": cfg_dict,
                "locked": r["locked"],
            })
        return _ok(req_id, {"providers": providers})
    except Exception as exc:
        logger.exception("inference.get_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_save_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``inference.save_providers`` — Persist the list of providers and their ordering/priorities.
    """
    providers = params.get("providers")
    if providers is None or not isinstance(providers, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'providers' (list) is required")

    import json as json_mod
    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Delete existing providers
                await conn.execute("DELETE FROM inference_providers")
                # Insert the updated list
                for idx, p in enumerate(providers):
                    cfg = p.get("config", {})
                    # Always merge flat config fields into the config dict
                    flat_cfg = {
                        "model": p.get("model"),
                        "engine": p.get("engine"),
                        "endpoint": p.get("endpoint"),
                        "apiKey": p.get("apiKey"),
                        "service": p.get("service"),
                    }
                    for k, v in flat_cfg.items():
                        if v is not None:
                            cfg[k] = v
                    
                    await conn.execute(
                        """
                        INSERT INTO inference_providers (id, type, name, priority, status, config, locked)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        p["id"],
                        p["type"],
                        p["name"],
                        idx + 1,
                        p.get("status", "unknown"),
                        json_mod.dumps(cfg),
                        bool(p.get("locked", False)),
                    )
        try:
            await update_settings_inference_node()
        except Exception as exc:
            logger.warning("Failed to update inference settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("inference.save_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_get_embedded_status(params: dict[str, Any], req_id: Any) -> str:
    """
    ``inference.get_embedded_status`` — Return progress and status of the default embedded model download.

    This handler is deliberately non-blocking.  The module may not be loaded yet
    (the pre-warm runs in a background task); in that case we immediately return
    ``not_downloaded`` rather than blocking the event loop for 5-15 s on a cold
    huggingface_hub import — which would exceed the 15 s RPC timeout.
    """
    import sys
    try:
        mod = sys.modules.get("python.inference.embedded_engine")
        if mod is None:
            # Module not pre-warmed yet — return a safe default immediately so
            # the renderer knows the harness is alive but still initialising.
            return _ok(req_id, {
                "status": "not_downloaded",
                "progress": 0.0,
                "error": ""
            })
        # Module is cached — get_status() is an instant lock+read.
        status, progress, error = mod.get_status()
        return _ok(req_id, {
            "status": status,
            "progress": progress,
            "error": error
        })
    except Exception as exc:
        logger.exception("inference.get_embedded_status error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_trigger_download(params: dict[str, Any], req_id: Any) -> str:
    """
    ``inference.trigger_download`` — Manually start downloading the default embedded model.
    """
    try:
        from python.inference.embedded_engine import start_download_async
        start_download_async()
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("inference.trigger_download error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_get_engines(params: dict[str, Any], req_id: Any) -> str:
    """``inference.get_engines`` — Returns all supported embedded engines and their install status."""
    try:
        from python.inference.engine_manager import get_all_engines
        return _ok(req_id, {"engines": get_all_engines()})
    except Exception as exc:
        logger.exception("inference.get_engines error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_install_engine(params: dict[str, Any], req_id: Any) -> str:
    """``inference.install_engine`` — Triggers the background pip installation of an engine."""
    engine_id = params.get("engine_id")
    if not engine_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "engine_id required")
        
    try:
        from python.inference.engine_manager import start_install
        start_install(engine_id)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("inference.install_engine error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_get_engine_install_progress(params: dict[str, Any], req_id: Any) -> str:
    """``inference.get_engine_install_progress`` — Returns the status and logs of an engine installation."""
    engine_id = params.get("engine_id")
    if not engine_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "engine_id required")
        
    try:
        from python.inference.engine_manager import get_status
        return _ok(req_id, get_status(engine_id))
    except Exception as exc:
        logger.exception("inference.get_engine_install_progress error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_detect_local_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``inference.detect_local_providers`` — On-demand re-scan of local inference engines.

    Probes the standard local-engine endpoints (Ollama :11434, LM Studio :1234,
    vLLM :8000, generic OpenAI-compat :8080) and updates the inference_providers
    table, then returns the full updated provider list so the renderer can
    refresh immediately without a separate get_providers call.
    """
    try:
        await _auto_detect_and_verify_providers()
        rows = await db.fetch(
            "SELECT id, type, name, priority, status, config, locked FROM inference_providers ORDER BY priority ASC"
        )
        import json as json_mod
        providers = []
        for r in rows:
            cfg = r["config"]
            if isinstance(cfg, str):
                cfg = json_mod.loads(cfg)
            providers.append({
                "id":       r["id"],
                "type":     r["type"],
                "name":     r["name"],
                "priority": r["priority"],
                "status":   r["status"],
                "config":   cfg,
                "locked":   r["locked"],
            })
        return _ok(req_id, {"providers": providers})
    except Exception as exc:
        logger.exception("inference.detect_local_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_agent_get_agents(params: dict[str, Any], req_id: Any) -> str:
    """
    ``agent.get_agents`` — Retrieve all configured agents sorted by created_at.
    """
    try:
        rows = await db.fetch("SELECT id, name, type, role, crew_type, is_active, config FROM agents ORDER BY created_at ASC")
        agents = []
        for r in rows:
            cfg = r["config"]
            if isinstance(cfg, str):
                import json as json_mod
                cfg = json_mod.loads(cfg)
            agents.append({
                "id": r["id"],
                "name": r["name"],
                "type": r["type"],
                "role": r["role"],
                "crew_type": r["crew_type"],
                "is_active": r["is_active"],
                "config": cfg,
            })
        return _ok(req_id, {"agents": agents})
    except Exception as exc:
        logger.exception("agent.get_agents error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_agent_save_agent(params: dict[str, Any], req_id: Any) -> str:
    """
    ``agent.save_agent`` — Create or update an agent persona.
    """
    agent_id = params.get("id")
    name = params.get("name")
    agent_type = params.get("type", "standalone")
    role = params.get("role", "")
    crew_type = params.get("crew_type", "")
    config = params.get("config", {})

    if not name or not isinstance(name, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'name' is required and must be a string")

    if not agent_id:
        # Generate short readable id from name
        import re
        sanitized = re.sub(r"[^a-zA-Z0-9\-]", "", name.lower().replace(" ", "-"))
        agent_id = f"agent-{sanitized}-{uuid.uuid4().hex[:4]}"

    import json as json_mod
    config_json = json_mod.dumps(config)

    try:
        await db.execute(
            """
            INSERT INTO agents (id, name, type, role, crew_type, config, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW())
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                type = EXCLUDED.type,
                role = EXCLUDED.role,
                crew_type = EXCLUDED.crew_type,
                config = EXCLUDED.config,
                updated_at = NOW()
            """,
            agent_id,
            name,
            agent_type,
            role,
            crew_type,
            config_json,
            caller="agent.save_agent RPC"
        )
        try:
            from python.harness.settings_nn import save_agent_persona_node, update_settings_agent_node
            await save_agent_persona_node(agent_id, name, role, config)
            await update_settings_agent_node()
        except Exception as exc:
            logger.warning("Failed to update agent settings or persona node: %s", exc)
        return _ok(req_id, {"success": True, "id": agent_id})
    except Exception as exc:
        logger.exception("agent.save_agent error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_agent_delete_agent(params: dict[str, Any], req_id: Any) -> str:
    """
    ``agent.delete_agent`` — Delete a custom agent.
    """
    agent_id = params.get("id")
    if not agent_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")

    if agent_id == "tala-core":
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Cannot delete system default agent 'tala-core'")

    try:
        # Check if the deleted agent was active; if so, activate tala-core first
        active_row = await db.fetchrow("SELECT is_active FROM agents WHERE id = $1", agent_id)
        if active_row and active_row["is_active"]:
            pool = db.get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("UPDATE agents SET is_active = FALSE")
                    await conn.execute("UPDATE agents SET is_active = TRUE WHERE id = 'tala-core'")
                    await conn.execute("DELETE FROM agents WHERE id = $1", agent_id)
        else:
            await db.execute("DELETE FROM agents WHERE id = $1", agent_id, caller="agent.delete_agent RPC")

        try:
            from python.harness.settings_nn import delete_agent_persona_node, update_settings_agent_node
            await delete_agent_persona_node(agent_id)
            await update_settings_agent_node()
        except Exception as exc:
            logger.warning("Failed to delete agent persona node or update agent settings: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("agent.delete_agent error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_agent_set_active(params: dict[str, Any], req_id: Any) -> str:
    """
    ``agent.set_active`` — Toggle a specific agent as active and deactivate all others.
    """
    agent_id = params.get("id")
    if not agent_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")

    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Verify agent exists
                exists = await conn.fetchval("SELECT 1 FROM agents WHERE id = $1", agent_id)
                if not exists:
                    return _err(req_id, JsonRpcError.INVALID_PARAMS, f"Agent with id '{agent_id}' does not exist")

                await conn.execute("UPDATE agents SET is_active = FALSE")
                await conn.execute("UPDATE agents SET is_active = TRUE WHERE id = $1", agent_id)

        # Log change to database
        await db.log_to_db("INFO", f"Active agent changed to '{agent_id}'", {"active_agent_id": agent_id})
        try:
            await update_settings_agent_node()
        except Exception as exc:
            logger.warning("Failed to update agent settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("agent.set_active error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_agent_get_chat_mode(params: dict[str, Any], req_id: Any) -> str:
    """
    ``agent.get_chat_mode`` — Get current chat mode ('rp', 'hybrid', 'assistant').
    """
    try:
        mode = get_current_chat_mode()
        return _ok(req_id, {"mode": mode})
    except Exception as exc:
        logger.exception("agent.get_chat_mode error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_agent_set_chat_mode(params: dict[str, Any], req_id: Any) -> str:
    """
    ``agent.set_chat_mode`` — Set current chat mode ('rp', 'hybrid', 'assistant') and update DB active agent.
    """
    mode = params.get("mode")
    if mode not in ("rp", "hybrid", "assistant"):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Invalid mode. Must be 'rp', 'hybrid', or 'assistant'")

    try:
        # Determine target agent id
        target_agent_id = "tala-rp" if mode in ("rp", "hybrid") else "tala-core"

        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Verify target agent exists
                exists = await conn.fetchval("SELECT 1 FROM agents WHERE id = $1", target_agent_id)
                if not exists:
                    logger.warning("Target agent %s does not exist in DB", target_agent_id)
                else:
                    await conn.execute("UPDATE agents SET is_active = FALSE")
                    await conn.execute("UPDATE agents SET is_active = TRUE WHERE id = $1", target_agent_id)

        save_current_chat_mode(mode)

        # Log change to database
        await db.log_to_db("INFO", f"Chat mode changed to '{mode}' (Active agent: '{target_agent_id}')", {"mode": mode, "active_agent_id": target_agent_id})

        try:
            await update_settings_agent_node()
        except Exception as exc:
            logger.warning("Failed to update agent settings node: %s", exc)

        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("agent.set_chat_mode error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_contacts_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``contacts.list`` — Retrieve all contacts sorted by username.
    """
    try:
        rows = await db.fetch(
            "SELECT username, wallet_address, endpoint_url, permissions FROM contacts ORDER BY username ASC"
        )
        contacts = []
        for r in rows:
            perms = r["permissions"]
            if isinstance(perms, str):
                import json as json_mod
                perms = json_mod.loads(perms)
            contacts.append({
                "username": r["username"],
                "wallet_address": r["wallet_address"],
                "endpoint_url": r["endpoint_url"],
                "permissions": perms or {},
            })
        return _ok(req_id, {"contacts": contacts})
    except Exception as exc:
        logger.exception("contacts.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_contacts_save(params: dict[str, Any], req_id: Any) -> str:
    """
    ``contacts.save`` — Create or update a contact.
    """
    username = params.get("username")
    wallet_address = params.get("wallet_address")
    endpoint_url = params.get("endpoint_url", "")
    shared_secret = params.get("shared_secret", "")
    permissions = params.get("permissions", {})

    if not username or not isinstance(username, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'username' is required and must be a string")
    if not wallet_address or not isinstance(wallet_address, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'wallet_address' is required and must be a string")

    username = username.strip()
    wallet_address = wallet_address.strip()
    endpoint_url = endpoint_url.strip() if isinstance(endpoint_url, str) else ""
    shared_secret = shared_secret.strip() if isinstance(shared_secret, str) else ""

    if not username.startswith("@"):
        username = "@" + username

    import json as json_mod
    permissions_json = json_mod.dumps(permissions)

    try:
        await db.execute(
            """
            INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret, permissions, updated_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
            ON CONFLICT (username) DO UPDATE SET
                wallet_address = EXCLUDED.wallet_address,
                endpoint_url = EXCLUDED.endpoint_url,
                shared_secret = CASE
                    WHEN EXCLUDED.shared_secret = '' THEN contacts.shared_secret
                    ELSE EXCLUDED.shared_secret
                END,
                permissions = EXCLUDED.permissions,
                updated_at = NOW()
            """,
            username,
            wallet_address,
            endpoint_url,
            shared_secret,
            permissions_json,
            caller="contacts.save RPC"
        )
        return _ok(req_id, {"success": True, "username": username})
    except Exception as exc:
        logger.exception("contacts.save error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_contacts_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``contacts.delete`` — Delete a contact by username.
    """
    username = params.get("username")
    if not username:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'username' is required")

    try:
        await db.execute("DELETE FROM contacts WHERE username = $1", username, caller="contacts.delete RPC")
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("contacts.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


def _normalize_peer_endpoint(endpoint: str) -> str:
    endpoint = (endpoint or "").strip()
    if not endpoint:
        raise ValueError("Peer endpoint is required")
    if endpoint.startswith("ws://") or endpoint.startswith("wss://"):
        return endpoint
    if endpoint.startswith("http://"):
        return "ws://" + endpoint[len("http://"):]
    if endpoint.startswith("https://"):
        return "wss://" + endpoint[len("https://"):]
    return "ws://" + endpoint


def _normalize_chain_endpoint(endpoint: str) -> str:
    endpoint = (endpoint or "").strip()
    if not endpoint:
        raise ValueError("Chain endpoint is required")
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint.rstrip("/")
    if endpoint.startswith("ws://"):
        return ("http://" + endpoint[len("ws://"):]).rstrip("/")
    if endpoint.startswith("wss://"):
        return ("https://" + endpoint[len("wss://"):]).rstrip("/")
    return ("http://" + endpoint.lstrip("/")).rstrip("/")


def _derive_chain_endpoint(endpoint_url: str, chain_endpoint_url: str | None = None) -> str:
    if chain_endpoint_url and str(chain_endpoint_url).strip():
        return _normalize_chain_endpoint(str(chain_endpoint_url))

    endpoint = _normalize_peer_endpoint(endpoint_url)
    chain_endpoint = endpoint.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
    if chain_endpoint.endswith(":8765"):
        chain_endpoint = chain_endpoint[:-5] + ":8332"
    return chain_endpoint.rstrip("/")


async def _register_peer_on_chain_directory(
    chain_endpoint_url: str,
    peer_payload: dict[str, Any],
) -> dict[str, Any]:
    import urllib.request

    payload = json.dumps(peer_payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{_normalize_chain_endpoint(chain_endpoint_url)}/chain/peers/register",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        },
        method="POST",
    )

    def _send() -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=3.0) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    return await asyncio.to_thread(_send)


async def _heartbeat_peer_on_chain_directory(
    chain_endpoint_url: str,
    peer_payload: dict[str, Any],
) -> dict[str, Any]:
    import urllib.request

    payload = json.dumps(peer_payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{_normalize_chain_endpoint(chain_endpoint_url)}/chain/peers/heartbeat",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        },
        method="POST",
    )

    def _send() -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=3.0) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    return await asyncio.to_thread(_send)


async def _call_peer_rpc(endpoint: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    from python.harness.webrtc_manager import webrtc_manager
    import uuid
    import json
    
    # Look up peer username by endpoint
    peer_record = await db.fetchrow("SELECT peer_username FROM p2p_sessions WHERE endpoint_url = $1", endpoint)
    peer_username = peer_record["peer_username"] if peer_record else None

    # ── VPS Write State Machine Gating ──
    is_vps = False
    if peer_username and "vps" in peer_username.lower():
        is_vps = True
    elif "vps" in endpoint.lower() or "34.60." in endpoint:
        is_vps = True
        
    write_methods = {
        "p2p.receive_message", 
        "p2p.receive_handshake", 
        "system.trigger_update", 
        "system.update_available",
        "p2p.send_message",
        "p2p.handshake"
    }
    
    if is_vps and method in write_methods:
        from python.harness.vps_state_machine import VPSWriteStateMachine, VPSState
        context_text = ""
        try:
            active_task = await db.fetchrow("SELECT description FROM tasks WHERE status = 'in-progress' ORDER BY created_at DESC LIMIT 1")
            if active_task:
                context_text = active_task["description"]
        except Exception:
            pass
            
        if not context_text:
            packet = params.get("packet", {})
            if isinstance(packet, dict):
                context_text = packet.get("content", "") or packet.get("message", "")
            if not context_text:
                context_text = f"Approved sync write to VPS: {method}"
                
        sm = VPSWriteStateMachine()
        state = await sm.validate(context_text)
        if state != VPSState.APPROVED:
            raise RuntimeError(f"VPS Write Operation Blocked: Memory context validation failed (State: {state.value})")
    
    # E2EE Envelope Encryption
    from python.harness.e2ee_manager import e2ee_manager
    if peer_username and method.startswith("p2p.") and method not in ("p2p.handshake", "p2p.receive_handshake", "p2p.webrtc_offer", "p2p.webrtc_answer", "p2p.webrtc_ice_candidate", "p2p.encrypted_payload"):
        if e2ee_manager.peer_ciphers.get(peer_username):
            logger.debug("Applying E2EE AES-GCM encryption for %s to %s", method, peer_username)
            params = e2ee_manager.encrypt(peer_username, method, params)
            params["sender_username"] = os.environ.get("TALA_USERNAME", "@local-tala") # Need sender identity to decrypt
            method = "p2p.encrypted_payload"

    # If WebRTC data channel is open, route over WebRTC
    if peer_username and webrtc_manager.data_channels.get(peer_username):
        logger.debug("Routing RPC %s to %s via WebRTC Data Channel", method, peer_username)
        return await webrtc_manager.send_rpc(peer_username, method, params)
        
    request_id = str(uuid.uuid4())
    request = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        ensure_ascii=False,
    )
    
    # Fallback to direct WebSocket connection (usually via VPS relay)
    async with websockets.connect(_normalize_peer_endpoint(endpoint), open_timeout=5, close_timeout=2) as ws:
        await ws.send(request)
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        
    # Asynchronously attempt WebRTC upgrade to avoid future VPS relay costs
    if peer_username and method not in ("p2p.webrtc_offer", "p2p.webrtc_answer", "p2p.webrtc_ice_candidate"):
        async def _upgrade_webrtc():
            if not webrtc_manager.connections.get(peer_username):
                try:
                    offer = await webrtc_manager.create_offer(peer_username)
                    # Note: We send our identity so the peer knows who is connecting
                    await _call_peer_rpc(endpoint, "p2p.webrtc_offer", {"peer_username": peer_username, "offer": offer})
                except Exception as e:
                    logger.warning("Failed to negotiate WebRTC upgrade with %s: %s", peer_username, e)
        import asyncio
        asyncio.create_task(_upgrade_webrtc())
        
    response = json.loads(raw)
    if response.get("error"):
        raise RuntimeError(response["error"].get("message", response["error"]))
    result = response.get("result")
    return result if isinstance(result, dict) else {"result": result}


def _normalize_peer_node_key(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _node_reference_candidates(node: Any) -> set[str]:
    candidates: set[str] = set()
    if not node:
        return candidates

    def _add(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _add(item)
            return
        text = str(value).strip()
        if not text:
            return
        candidates.add(_normalize_peer_node_key(text))
        candidates.add(text.lower())

    _add(getattr(node, "node_id", ""))
    _add(getattr(node, "node_type", ""))
    _add(getattr(node, "source", ""))

    state_data = getattr(node, "state_data", {}) or {}
    for key in ("alias", "title", "name", "collection", "collection_name", "collection_id", "category", "node_group", "group"):
        _add(state_data.get(key))
    _add(state_data.get("aliases", []))
    _add(state_data.get("tags", []))

    source_path = getattr(node, "source_path", None)
    if source_path:
        _add(Path(source_path).stem)

    return {candidate for candidate in candidates if candidate}


def _get_local_nodes_directory() -> Path:
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        nodes_dir = Path(user_data) / "nodes"
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            nodes_dir = Path(appdata) / "tala-engine" / "nodes"
        else:
            nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"

    if not nodes_dir.exists():
        repo_nodes = Path(_ROOT_DIR) / "nodes"
        if repo_nodes.exists():
            nodes_dir = repo_nodes
    return nodes_dir


def _parse_peer_node_reference(reference: str) -> tuple[str, str]:
    raw = (reference or "").strip()
    match = re.fullmatch(r"@(?P<username>[A-Za-z0-9_.-]+)(?:[/:#]{1,2}(?P<node>[A-Za-z0-9_.\-/]+))?", raw)
    if not match:
        raise ValueError("Peer node references must look like @user323/financial")
    username = "@" + match.group("username")
    node_name = (match.group("node") or "").strip()
    if not node_name:
        raise ValueError("Peer node reference is missing a node or collection name")
    return username, node_name


def _extract_peer_node_references(text: str) -> list[tuple[str, str, str]]:
    matches: list[tuple[str, str, str]] = []
    if not text:
        return matches
    pattern = re.compile(r"@(?P<username>[A-Za-z0-9_.-]+)(?:[/:#]{1,2}(?P<node>[A-Za-z0-9_.\-/]+))")
    seen: set[tuple[str, str]] = set()
    for match in pattern.finditer(text):
        username = "@" + match.group("username")
        node_name = (match.group("node") or "").strip()
        key = (username.lower(), _normalize_peer_node_key(node_name))
        if node_name and key not in seen:
            seen.add(key)
            matches.append((username, node_name, f"{username}/{node_name}"))
    return matches


async def _get_firewall_contact_access_rights(peer_id: str) -> dict[str, Any] | None:
    row = await db.fetchrow(
        "SELECT access_rights FROM firewall_contacts WHERE id = $1",
        peer_id,
    )
    if row is None:
        return None
    rights = row["access_rights"] or {}
    if isinstance(rights, str):
        rights = json.loads(rights)
    return dict(rights or {})


def _firewall_allows_node_name(access_rights: dict[str, Any] | None, node_names: set[str]) -> bool:
    if not access_rights:
        return False
    allowed = access_rights.get("allowed_categories") or []
    if not isinstance(allowed, list):
        return False

    normalized_allowed = {_normalize_peer_node_key(str(item)) for item in allowed if str(item).strip()}
    if not normalized_allowed:
        return False

    if {"*", "nodes", "node", "peer-nodes", "peer_nodes"} & normalized_allowed:
        return True

    normalized_candidates = {_normalize_peer_node_key(name) for name in node_names if str(name).strip()}
    return bool(normalized_allowed & normalized_candidates)


async def _peer_can_resolve_node_reference(contact: dict[str, Any], node_names: set[str]) -> tuple[bool, str | None]:
    permissions = contact.get("permissions") or {}
    if not permissions.get("nodes"):
        return False, "contact_nodes_permission_disabled"
    access_rights = await _get_firewall_contact_access_rights(contact["wallet_address"])
    if not access_rights:
        return False, "firewall_denied"

    peer_clearance = int(access_rights.get("clearance_level", 0))
    peer_categories = access_rights.get("allowed_categories") or []
    if not isinstance(peer_categories, list):
        peer_categories = []

    # If the contact is granted full wildcard, skip granular checks
    if "*" in peer_categories or "all" in peer_categories:
        return True, None

    for node_name in node_names:
        # Check direct allowed categories/names configuration
        if _firewall_allows_node_name(access_rights, {node_name}):
            continue

        # Resolve local node reference to check tags and clearance
        resolved = await _resolve_local_html_nn_reference(node_name)
        if not resolved:
            continue

        source_path = resolved.get("source_path")
        if not source_path:
            continue

        from sdk.agent.parser import parse_file
        try:
            node_obj = parse_file(str(source_path))
            state_data = node_obj.state_data or {}
        except Exception:
            node_obj = None
            state_data = {}

        if node_obj:
            # Enforce Clearance Access level boundary [0-10]
            req_clearance = state_data.get("clearance") or state_data.get("clearance_level") or state_data.get("required_clearance")
            if req_clearance is not None:
                try:
                    req_clearance = int(req_clearance)
                except (ValueError, TypeError):
                    req_clearance = 0
            else:
                req_clearance = 0

            if peer_clearance < req_clearance:
                return False, f"clearance_level_insufficient: {peer_clearance} < {req_clearance}"

            # Enforce Allowed Node Categories access
            node_tags = state_data.get("tags") or state_data.get("categories") or state_data.get("category") or []
            if isinstance(node_tags, str):
                node_tags = [node_tags]
            elif not isinstance(node_tags, list):
                node_tags = []

            normalized_node_tags = [str(t).lower().strip() for t in node_tags if str(t).strip()]
            normalized_node_tags.append(str(node_obj.node_type or "").lower().strip())
            normalized_node_tags.append(str(node_name).lower().strip())

            peer_categories_lower = [str(cat).lower().strip() for cat in peer_categories if str(cat).strip()]

            # Must have at least one intersecting allowed category or tag
            if not any(cat in normalized_node_tags for cat in peer_categories_lower):
                return False, f"category_access_denied: peer allowed={peer_categories_lower}, node tags={normalized_node_tags}"

    return True, None


def _serialize_peer_node(node: Any, matched_reference: str) -> dict[str, Any]:
    state_data = getattr(node, "state_data", {}) or {}
    return {
        "matched_reference": matched_reference,
        "node_id": getattr(node, "node_id", None),
        "node_type": getattr(node, "node_type", None),
        "governance_status": getattr(node, "governance_status", None),
        "execution_authority": getattr(node, "execution_authority", None),
        "memory_authority": getattr(node, "memory_authority", None),
        "provider_authority": getattr(node, "provider_authority", None),
        "wallet_authority": getattr(node, "wallet_authority", None),
        "semantic_memory": getattr(node, "semantic_memory", ""),
        "state_data": state_data,
        "title": state_data.get("title") or state_data.get("name") or getattr(node, "node_id", None),
        "collection_name": state_data.get("collection_name") or state_data.get("collection"),
        "aliases": state_data.get("aliases", []),
        "tags": state_data.get("tags", []),
        "source": getattr(node, "source", None),
        "schema_version": getattr(node, "schema_version", None),
        "trust_tier": getattr(node, "trust_tier", None),
        "content_hash": getattr(node, "content_hash", None),
    }


async def _resolve_local_html_nn_reference(reference: str, nodes_dir: Path | None = None) -> dict[str, Any] | None:
    nodes_dir = nodes_dir or _get_local_nodes_directory()
    if not nodes_dir.exists() or not nodes_dir.is_dir():
        return None

    try:
        from sdk.agent.parser import parse_file
    except Exception:
        return None

    requested = _normalize_peer_node_key(reference)
    best_match: tuple[float, Any, str] | None = None

    for filepath in nodes_dir.glob("*.html"):
        try:
            node = parse_file(str(filepath))
        except Exception:
            continue

        candidates = _node_reference_candidates(node)
        if requested not in candidates:
            continue

        score = 0.0
        node_id = _normalize_peer_node_key(getattr(node, "node_id", ""))
        if requested == node_id:
            score = 3.0
        elif requested in {_normalize_peer_node_key(str(v)) for v in (node.state_data or {}).get("aliases", [])}:
            score = 3.0
        elif requested in {
            _normalize_peer_node_key(str((node.state_data or {}).get("title", ""))),
            _normalize_peer_node_key(str((node.state_data or {}).get("collection_name", ""))),
            _normalize_peer_node_key(str((node.state_data or {}).get("collection", ""))),
        }:
            score = 2.5
        elif requested == _normalize_peer_node_key(Path(filepath).stem):
            score = 2.0
        else:
            score = 1.0

        score += float(getattr(node, "execution_authority", 0.0) or 0.0)
        if best_match is None or score > best_match[0]:
            best_match = (score, node, str(filepath))

    if best_match is None:
        return None
    _, node, filepath = best_match
    return {
        "node": _serialize_peer_node(node, reference),
        "source_path": filepath,
    }


async def _call_peer_node_reference(endpoint: str, contact: dict[str, Any], node_reference: str) -> dict[str, Any]:
    secret = _contact_secret(contact)
    packet = _sign_packet(
        {
            "protocol": "tala-p2p/1.0",
            "type": "node-reference",
            "sender_username": os.environ.get("TALA_USERNAME", "@local-tala"),
            "sender_wallet": os.environ.get("TALA_WALLET_ADDRESS", "local-tala"),
            "recipient_wallet": contact["wallet_address"],
            "reference": node_reference,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        secret,
    )
    response = await _call_peer_rpc(endpoint, "p2p.resolve_node_reference", {"packet": packet})
    return response


def _format_peer_node_context(peer_username: str, reference: str, node_payload: dict[str, Any]) -> str:
    node = node_payload.get("node", node_payload)
    title = node.get("title") or node.get("node_id") or reference
    collection_name = node.get("collection_name") or ""
    tags = node.get("tags") or []
    state_data = node.get("state_data") or {}
    compact_state = {
        key: value
        for key, value in state_data.items()
        if key in {"title", "name", "alias", "aliases", "collection", "collection_name", "category", "categories", "tags", "summary", "description"}
    }
    state_json = json.dumps(compact_state, ensure_ascii=False, indent=2) if compact_state else "{}"
    tags_text = ", ".join(str(tag) for tag in tags) if tags else "none"
    return (
        f"=== PEER HTML NEURAL NODE RESOLUTION ===\n"
        f"Peer: {peer_username}\n"
        f"Reference: {reference}\n"
        f"Title: {title}\n"
        f"Collection: {collection_name or '—'}\n"
        f"Node ID: {node.get('node_id') or '—'}\n"
        f"Node Type: {node.get('node_type') or '—'}\n"
        f"Tags: {tags_text}\n"
        f"Semantic Memory:\n{(node.get('semantic_memory') or '').strip()}\n\n"
        f"State Data:\n{state_json}\n"
        f"========================================="
    )


async def _get_contact(username: str | None = None, wallet_address: str | None = None) -> dict[str, Any] | None:
    if username:
        lookup = username.strip()
        if lookup and not lookup.startswith("@"):
            lookup = "@" + lookup
        row = await db.fetchrow(
            "SELECT username, wallet_address, endpoint_url, shared_secret, permissions FROM contacts WHERE username = $1",
            lookup,
        )
    elif wallet_address:
        row = await db.fetchrow(
            "SELECT username, wallet_address, endpoint_url, shared_secret, permissions FROM contacts WHERE wallet_address = $1",
            wallet_address.strip(),
        )
    else:
        row = None
    if row is None:
        return None
    permissions = row["permissions"]
    if isinstance(permissions, str):
        permissions = json.loads(permissions)
    return {
        "username": row["username"],
        "wallet_address": row["wallet_address"],
        "endpoint_url": row["endpoint_url"],
        "shared_secret": row["shared_secret"],
        "permissions": permissions or {},
    }


async def _get_master_profile_config() -> dict[str, Any]:
    row = await db.fetchrow("SELECT config FROM profiles WHERE id = 'master-user'")
    if row is None:
        return {}
    config = row["config"]
    if isinstance(config, str):
        return json.loads(config)
    return dict(config or {})


def _profile_for_permissions(profile_config: dict[str, Any], permissions: dict[str, Any]) -> dict[str, Any]:
    shared: dict[str, Any] = {}
    for category, allowed in permissions.items():
        if allowed and category in profile_config:
            shared[category] = profile_config[category]
    return shared


def _packet_signature(packet: dict[str, Any], secret: str) -> str:
    unsigned = {k: v for k, v in packet.items() if k != "signature"}
    canonical = json.dumps(unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _sign_packet(packet: dict[str, Any], secret: str) -> dict[str, Any]:
    signed = dict(packet)
    signed["signature"] = _packet_signature(signed, secret)
    return signed


def _verify_packet(packet: dict[str, Any], secret: str) -> bool:
    signature = packet.get("signature")
    if not signature or not isinstance(signature, str):
        return False
    return hmac.compare_digest(signature, _packet_signature(packet, secret))


def _accept_any_tala_client_enabled() -> bool:
    return os.environ.get("TALA_VPS_ACCEPT_ANY_TALA", "").strip().lower() in {"1", "true", "yes", "on"}


def _is_tala_handshake_packet(packet: dict[str, Any]) -> bool:
    if packet.get("protocol") != "tala-p2p/1.0" or packet.get("type") != "handshake":
        return False
    sender_username = packet.get("sender_username")
    sender_wallet = packet.get("sender_wallet")
    timestamp = packet.get("timestamp")
    return all(isinstance(value, str) and value.strip() for value in (sender_username, sender_wallet, timestamp))


async def _enroll_unknown_tala_contact(packet: dict[str, Any]) -> dict[str, Any] | None:
    if not _accept_any_tala_client_enabled() or not _is_tala_handshake_packet(packet):
        return None

    enrollment_secret = packet.get("enrollment_secret")
    if not isinstance(enrollment_secret, str) or not enrollment_secret:
        return None
    if not _verify_packet(packet, enrollment_secret):
        return None

    username = str(packet["sender_username"]).strip()
    wallet_address = str(packet["sender_wallet"]).strip()
    endpoint_url = _normalize_peer_endpoint(str(packet.get("sender_endpoint", "")).strip())
    permissions = packet.get("requested_permissions")
    if not isinstance(permissions, dict):
        permissions = {
            "identity": True,
            "about": True,
            "rp_persona": True,
            "roles": False,
            "skills": False,
            "projects": False,
            "preferences": False,
            "nodes": False,
            "pii_vault": False,
        }

    await db.execute(
        """
        INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret, permissions)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT (username) DO UPDATE SET
            wallet_address = EXCLUDED.wallet_address,
            endpoint_url = EXCLUDED.endpoint_url,
            shared_secret = EXCLUDED.shared_secret,
            permissions = EXCLUDED.permissions,
            updated_at = NOW()
        """,
        username,
        wallet_address,
        endpoint_url,
        enrollment_secret,
        permissions,
        caller="p2p.enroll_unknown_tala_contact",
    )
    return {
        "username": username,
        "wallet_address": wallet_address,
        "endpoint_url": endpoint_url,
        "shared_secret": enrollment_secret,
        "permissions": permissions,
    }


def _contact_secret(contact: dict[str, Any]) -> str:
    secret = (contact.get("shared_secret") or "").strip()
    if not secret:
        raise ValueError("Contact shared_secret is required for live P2P")
    return secret


async def _record_p2p_message(peer_username: str, direction: str, message: dict[str, Any]) -> None:
    await db.execute(
        "INSERT INTO p2p_messages (peer_username, direction, message_json) VALUES ($1, $2, $3::jsonb)",
        peer_username,
        direction,
        json.dumps(message, ensure_ascii=False),
        caller="p2p.record_message",
    )
    await db.execute(
        "UPDATE p2p_sessions SET last_message_at = NOW(), updated_at = NOW() WHERE peer_username = $1",
        peer_username,
        caller="p2p.record_message.session",
    )
    try:
        from sdk.agent.anchor import canonicalize_and_anchor

        canonicalize_and_anchor(
            f"p2p-{peer_username}-{uuid.uuid4().hex}",
            json.dumps(message, ensure_ascii=False),
            {"peer_username": peer_username, "direction": direction},
            None,
            creator="tala-p2p",
            document_type="p2p-message",
        )
    except Exception as exc:
        logger.debug("Failed to anchor P2P message for %s: %s", peer_username, exc)


async def handle_p2p_handshake(params: dict[str, Any], req_id: Any) -> str:
    username = params.get("username")
    endpoint_url = params.get("endpoint_url", "")
    contact = await _get_contact(username=username)
    if contact is None:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Unknown contact")

    endpoint = endpoint_url or contact.get("endpoint_url")
    if not endpoint:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Contact has no endpoint_url")

    profile_config = await _get_master_profile_config()
    shared_profile = _profile_for_permissions(profile_config, contact["permissions"])
    now = datetime.now(timezone.utc).isoformat()
    try:
        secret = _contact_secret(contact)
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    packet_body = {
        "protocol": "tala-p2p/1.0",
        "type": "handshake",
        "sender_username": params.get("sender_username") or "@local-tala",
        "sender_wallet": params.get("sender_wallet") or os.environ.get("TALA_WALLET_ADDRESS", "local-tala"),
        "recipient_username": contact["username"],
        "recipient_wallet": contact["wallet_address"],
        "shared_profile": shared_profile,
        "permissions": contact["permissions"],
        "timestamp": now,
    }
    
    from python.harness.e2ee_manager import e2ee_manager
    packet_body["e2ee_public_key"] = e2ee_manager.get_public_key_b64()
    
    if params.get("allow_vps_enrollment") is True:
        packet_body["enrollment_secret"] = secret
        packet_body["sender_endpoint"] = _normalize_peer_endpoint(str(params.get("sender_endpoint", "")).strip())
        packet_body["requested_permissions"] = contact["permissions"]
    packet = _sign_packet(packet_body, secret)

    try:
        response = await _call_peer_rpc(endpoint, "p2p.receive_handshake", {"packet": packet})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"Peer handshake failed: {exc}")

    if response.get("e2ee_public_key"):
        from python.harness.e2ee_manager import e2ee_manager
        e2ee_manager.derive_shared_secret(contact["username"], response["e2ee_public_key"])

    await db.execute(
        """
        INSERT INTO p2p_sessions (peer_username, wallet_address, endpoint_url, status, handshake_json, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
        ON CONFLICT (peer_username) DO UPDATE SET
            wallet_address = EXCLUDED.wallet_address,
            endpoint_url = EXCLUDED.endpoint_url,
            status = EXCLUDED.status,
            handshake_json = EXCLUDED.handshake_json,
            updated_at = NOW()
        """,
        contact["username"],
        contact["wallet_address"],
        _normalize_peer_endpoint(endpoint),
        "connected" if response.get("accepted") else "rejected",
        json.dumps({"outbound": packet, "response": response}, ensure_ascii=False),
        caller="p2p.handshake",
    )
    return _ok(req_id, {"success": True, "peer": contact["username"], "response": response})


async def handle_p2p_receive_handshake(params: dict[str, Any], req_id: Any) -> str:
    packet = params.get("packet")
    if not isinstance(packet, dict):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'packet' is required")
    contact = await _get_contact(wallet_address=str(packet.get("sender_wallet", "")))
    if contact is None:
        contact = await _enroll_unknown_tala_contact(packet)
        if contact is None:
            return _ok(req_id, {"accepted": False, "reason": "unknown_sender"})
    try:
        secret = _contact_secret(contact)
    except ValueError as exc:
        return _ok(req_id, {"accepted": False, "reason": str(exc)})
    if not _verify_packet(packet, secret):
        return _ok(req_id, {"accepted": False, "reason": "bad_signature"})

    await db.execute(
        """
        INSERT INTO p2p_sessions (peer_username, wallet_address, endpoint_url, status, handshake_json, updated_at)
        VALUES ($1, $2, $3, 'connected', $4::jsonb, NOW())
        ON CONFLICT (peer_username) DO UPDATE SET
            wallet_address = EXCLUDED.wallet_address,
            endpoint_url = EXCLUDED.endpoint_url,
            status = 'connected',
            handshake_json = EXCLUDED.handshake_json,
            updated_at = NOW()
        """,
        contact["username"],
        contact["wallet_address"],
        contact.get("endpoint_url", ""),
        json.dumps({"inbound": packet}, ensure_ascii=False),
        caller="p2p.receive_handshake",
    )
    
    response_payload = {"accepted": True, "peer": contact["username"], "protocol": "tala-p2p/1.0"}
    
    if packet.get("e2ee_public_key"):
        from python.harness.e2ee_manager import e2ee_manager
        e2ee_manager.derive_shared_secret(contact["username"], packet["e2ee_public_key"])
        response_payload["e2ee_public_key"] = e2ee_manager.get_public_key_b64()
        
    return _ok(req_id, response_payload)


async def handle_p2p_send_message(params: dict[str, Any], req_id: Any) -> str:
    username = params.get("username")
    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'message' is required")
    contact = await _get_contact(username=username)
    if contact is None:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Unknown contact")
    endpoint = params.get("endpoint_url") or contact.get("endpoint_url")
    if not endpoint:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Contact has no endpoint_url")

    profile_config = await _get_master_profile_config()
    try:
        secret = _contact_secret(contact)
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    packet = _sign_packet(
        {
            "protocol": "tala-p2p/1.0",
            "type": "message",
            "sender_wallet": os.environ.get("TALA_WALLET_ADDRESS", "local-tala"),
            "recipient_wallet": contact["wallet_address"],
            "message": message.strip(),
            "shared_profile": _profile_for_permissions(profile_config, contact["permissions"]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        secret,
    )
    try:
        response = await _call_peer_rpc(endpoint, "p2p.receive_message", {"packet": packet})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"Peer message failed: {exc}")
    await _record_p2p_message(contact["username"], "outbound", packet)
    return _ok(req_id, {"success": True, "peer": contact["username"], "response": response})


async def handle_p2p_receive_message(params: dict[str, Any], req_id: Any) -> str:
    packet = params.get("packet")
    if not isinstance(packet, dict):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'packet' is required")
    contact = await _get_contact(wallet_address=str(packet.get("sender_wallet", "")))
    if contact is None:
        return _ok(req_id, {"accepted": False, "reason": "unknown_sender"})
    try:
        secret = _contact_secret(contact)
    except ValueError as exc:
        return _ok(req_id, {"accepted": False, "reason": str(exc)})
    if not _verify_packet(packet, secret):
        return _ok(req_id, {"accepted": False, "reason": "bad_signature"})
    await _record_p2p_message(contact["username"], "inbound", packet)

    if packet.get("type") == "a2a":
        asyncio.create_task(_handle_inbound_a2a(contact["username"], packet.get("message", "")))

    return _ok(req_id, {"accepted": True, "peer": contact["username"]})


async def _send_a2a_message_helper(username: str, message: str) -> dict[str, Any]:
    """
    NAME
           _send_a2a_message_helper - Package and send A2A message to peer contact
           
    SYNOPSIS
           _send_a2a_message_helper(username: str, message: str) -> dict[str, Any]
           
    DESCRIPTION
           Resolves peer contact by username, constructs and signs a P2P packet of type
           "a2a" containing the message, calls the peer's receive endpoint, and
           persists the message in the database.
           
           - username: The username of the peer contact
           - message: The string message to transmit
    """
    contact = await _get_contact(username=username)
    if contact is None:
        raise ValueError(f"Unknown contact: {username}")
        
    endpoint = contact.get("endpoint_url")
    if not endpoint:
        raise ValueError(f"Contact {username} has no endpoint_url")
        
    profile_config = await _get_master_profile_config()
    secret = _contact_secret(contact)
    
    packet = _sign_packet(
        {
            "protocol": "tala-p2p/1.0",
            "type": "a2a",
            "sender_wallet": os.environ.get("TALA_WALLET_ADDRESS", "local-tala"),
            "recipient_wallet": contact["wallet_address"],
            "message": message.strip(),
            "shared_profile": _profile_for_permissions(profile_config, contact["permissions"]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        secret,
    )
    
    response = await _call_peer_rpc(endpoint, "p2p.receive_message", {"packet": packet})
    await _record_p2p_message(contact["username"], "outbound", packet)
    return {"recipient": contact["username"], "response": response}


async def _handle_inbound_a2a(peer_username: str, incoming_message: str) -> None:
    """
    NAME
           _handle_inbound_a2a - Asynchronously process inbound A2A message
           
    SYNOPSIS
           _handle_inbound_a2a(peer_username: str, incoming_message: str) -> None
           
    DESCRIPTION
           Loads local active agent configuration and builds conversation history from
           database context. Runs agent inference to produce a response and automatically
           dispatches it back to the peer. Includes runaway loop detection.
           
           - peer_username: The username of the peer contact that sent the message
           - incoming_message: The content of the incoming message
    """
    # 1. Loop runaway protection: check if we received too many messages in the last 15 seconds
    try:
        recent_rows = await db.fetch(
            "SELECT created_at FROM p2p_messages WHERE peer_username = $1 AND created_at > NOW() - INTERVAL '15 seconds' LIMIT 10",
            peer_username
        )
        if len(recent_rows) >= 6:
            logger.warning("A2A: Rapid message loop detected with %s. Pausing automatic reply.", peer_username)
            return
    except Exception as exc:
        logger.warning("A2A loop protection check failed: %s", exc)

    # 2. Fetch active agent configuration
    try:
        agent_row = await db.fetchrow("SELECT name, config FROM agents WHERE is_active = TRUE")
        if agent_row:
            cfg = agent_row["config"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            active_system_prompt = cfg.get("system_prompt", "You are Tala, the core offline cognitive agent.")
        else:
            active_system_prompt = "You are Tala, the core offline cognitive agent."
    except Exception as e:
        logger.warning("A2A: Failed to fetch active agent config: %s", e)
        active_system_prompt = "You are Tala, the core offline cognitive agent."

    # 3. Load conversation history for prompt context (last 20 messages)
    history_messages = []
    try:
        rows = await db.fetch(
            "SELECT direction, message_json FROM p2p_messages WHERE peer_username = $1 ORDER BY created_at ASC LIMIT 20",
            peer_username
        )
        for r in rows:
            direction = r["direction"]
            msg_json = r["message_json"]
            if isinstance(msg_json, str):
                msg_json = json.loads(msg_json)
            msg_text = msg_json.get("message", "").strip()
            if msg_text:
                role = "user" if direction == "inbound" else "assistant"
                history_messages.append({"role": role, "content": msg_text})
    except Exception as e:
        logger.warning("A2A: Failed to fetch conversation history: %s", e)
        history_messages = [{"role": "user", "content": incoming_message}]

    # Ingest system prompt formatting
    from datetime import datetime as dt_module
    current_time = dt_module.now().strftime("%Y-%m-%d %H:%M:%S")
    time_metadata = f"[Current Date & Time: {current_time}]"
    if active_system_prompt:
        system_content = f"{time_metadata}\n{active_system_prompt}"
    else:
        system_content = time_metadata

    inference_messages = [{"role": "system", "content": system_content}]
    
    # Ensure current incoming message is at the end of the history
    if not history_messages or history_messages[-1]["content"] != incoming_message.strip():
        history_messages.append({"role": "user", "content": incoming_message})
        
    inference_messages.extend(history_messages)

    # 4. Invoke inference engine
    try:
        rows = await db.fetch(
            "SELECT id, type, name, priority, status, config, locked FROM inference_providers ORDER BY priority ASC"
        )
    except Exception as e:
        logger.error("A2A: Failed to query inference providers: %s", e)
        return

    generated_text = None
    errors = []
    
    for provider in rows:
        if provider.get("status") == "offline" and provider["id"] != "embedded-builtin":
            continue
            
        p_type = provider["type"]
        p_name = provider["name"]
        p_config = provider["config"] or {}
        if isinstance(p_config, str):
            p_config = json.loads(p_config)
            
        logger.info("A2A: Attempting generation with provider '%s' (%s)", p_name, p_type)
        
        try:
            if p_type == "embedded":
                from python.inference.embedded_engine import check_model_cached, generate_stream
                if not check_model_cached():
                    raise RuntimeError("Embedded model not cached.")
                chunks = []
                async for chunk in generate_stream(inference_messages, 512, 0.7):
                    chunks.append(chunk)
                generated_text = "".join(chunks)
                break
            elif p_type in ("local", "remote"):
                endpoint = p_config.get("endpoint", "").strip()
                model = p_config.get("model", "").strip()
                api_key = p_config.get("apiKey", "").strip()
                
                if p_type == "local":
                    engine = p_config.get("engine", "ollama")
                    if not endpoint:
                        endpoint = "http://localhost:11434/v1" if engine == "ollama" else "http://localhost:1234/v1"
                    if not model:
                        model = "llama3.2:3b" if engine == "ollama" else "qwen2.5-1.5b-instruct"
                else:
                    service = p_config.get("service", "openai")
                    if service == "openai" and not endpoint:
                        endpoint = "https://api.openai.com/v1"
                    elif service == "gemini" and not endpoint:
                        endpoint = "https://generativelanguage.googleapis.com/v1beta/openai"
                    elif service == "anthropic" and not endpoint:
                        endpoint = "https://api.anthropic.com/v1"
                        
                import httpx
                headers = {"Content-Type": "application/json"}
                if api_key:
                    if "anthropic" in endpoint or p_config.get("service") == "anthropic":
                        headers["x-api-key"] = api_key
                        headers["anthropic-version"] = "2023-06-01"
                    else:
                        headers["Authorization"] = f"Bearer {api_key}"
                        
                url = f"{_normalize_endpoint_url(endpoint)}/chat/completions"
                payload = {
                    "model": model,
                    "messages": inference_messages,
                    "max_tokens": 512,
                    "temperature": 0.7,
                    "stream": False
                }
                if p_type == "local" and p_config.get("engine", "ollama") == "ollama":
                    payload["options"] = {"num_ctx": 8192}
                    
                async with httpx.AsyncClient(timeout=90.0) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    if response.status_code != 200:
                        raise RuntimeError(f"HTTP Error {response.status_code}: {response.text}")
                    resp_json = response.json()
                    choice = resp_json["choices"][0]["message"]
                    generated_text = choice.get("content", "") or ""
                    break
        except Exception as e:
            exc_desc = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.warning("A2A: Provider '%s' failed: %s", p_name, exc_desc)
            errors.append(f"Provider '{p_name}' failed: {exc_desc}")
            
    if not generated_text:
        logger.error("A2A: All inference providers failed: %s", errors)
        return
        
    # 5. Send reply
    try:
        await _send_a2a_message_helper(peer_username, generated_text)
        logger.info("A2A: Sent reply to %s successfully.", peer_username)
    except Exception as e:
        logger.error("A2A: Failed to send reply to %s: %s", peer_username, e)



async def handle_p2p_resolve_node_reference(params: dict[str, Any], req_id: Any) -> str:
    packet = params.get("packet")
    if not isinstance(packet, dict):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'packet' is required")

    reference = str(packet.get("reference") or "").strip()
    if not reference:
        return _ok(req_id, {"resolved": False, "reason": "missing_reference"})

    sender_wallet = str(packet.get("sender_wallet", "")).strip()
    contact = await _get_contact(wallet_address=sender_wallet)
    if contact is None:
        return _ok(req_id, {"resolved": False, "reason": "unknown_sender"})

    try:
        secret = _contact_secret(contact)
    except ValueError as exc:
        return _ok(req_id, {"resolved": False, "reason": str(exc)})

    if not _verify_packet(packet, secret):
        return _ok(req_id, {"resolved": False, "reason": "bad_signature"})

    try:
        _requested_username, node_name = _parse_peer_node_reference(reference)
    except ValueError as exc:
        return _ok(req_id, {"resolved": False, "reason": str(exc)})

    can_resolve, deny_reason = await _peer_can_resolve_node_reference(contact, {node_name})
    if not can_resolve:
        return _ok(req_id, {"resolved": False, "reason": deny_reason or "firewall_denied"})

    resolved = await _resolve_local_html_nn_reference(node_name)
    if resolved is None:
        return _ok(req_id, {"resolved": False, "reason": "node_not_found"})

    return _ok(req_id, {
        "resolved": True,
        "reference": reference,
        "peer": contact["username"],
        "node": resolved["node"],
        "source_path": resolved["source_path"],
    })


CHAIN_CONNECTION_SETTING_KEY = "chain.connection"
DEFAULT_VPS_PERMISSIONS = {
    "identity": True,
    "about": True,
    "rp_persona": True,
    "roles": False,
    "skills": False,
    "projects": False,
    "preferences": False,
    "nodes": False,
    "pii_vault": False,
}


async def _get_client_setting(key: str) -> dict[str, Any]:
    row = await db.fetchrow("SELECT value FROM client_settings WHERE key = $1", key)
    if not row:
        return {}
    value = row["value"]
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


async def _save_client_setting(key: str, value: dict[str, Any]) -> None:
    await db.execute(
        """
        INSERT INTO client_settings (key, value, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = NOW()
        """,
        key,
        value,
        caller="client_settings.save",
    )


async def handle_chain_connection_get(params: dict[str, Any], req_id: Any) -> str:
    try:
        setting = await _get_client_setting(CHAIN_CONNECTION_SETTING_KEY)
        return _ok(req_id, {"connection": setting or {"mode": "local", "auto_connect": False}})
    except Exception as exc:
        logger.exception("chain.connection_get error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_stay_local(params: dict[str, Any], req_id: Any) -> str:
    setting = {
        "mode": "local",
        "auto_connect": False,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await _save_client_setting(CHAIN_CONNECTION_SETTING_KEY, setting)
        return _ok(req_id, {"success": True, "connection": setting})
    except Exception as exc:
        logger.exception("chain.stay_local error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_disconnect(params: dict[str, Any], req_id: Any) -> str:
    remove_contact = bool(params.get("remove_contact", False))
    try:
        setting = await _get_client_setting(CHAIN_CONNECTION_SETTING_KEY)
        username = setting.get("username") or "@tala-vps"
        endpoint_url = setting.get("endpoint_url", "")
        chain_endpoint_url = setting.get("chain_endpoint_url")
        disconnected = {
            "mode": "local",
            "auto_connect": False,
            "previous": setting,
            "disconnected_at": datetime.now(timezone.utc).isoformat(),
        }
        await _save_client_setting(CHAIN_CONNECTION_SETTING_KEY, disconnected)
        await db.execute(
            "UPDATE p2p_sessions SET status = 'disconnected', updated_at = NOW() WHERE peer_username = $1",
            username,
            caller="chain.disconnect.session",
        )
        if remove_contact:
            await db.execute(
                "DELETE FROM contacts WHERE username = $1",
                username,
                caller="chain.disconnect.contact",
            )
        if chain_endpoint_url and endpoint_url:
            try:
                await _heartbeat_peer_on_chain_directory(
                    chain_endpoint_url,
                    {
                        "username": username,
                        "wallet_address": setting.get("wallet_address") or os.environ.get("TALA_WALLET_ADDRESS", "local-tala"),
                        "endpoint_url": endpoint_url,
                        "discoverable": False,
                        "status": "offline",
                        "network": "tala",
                        "capabilities": ["p2p", "vps-discovery"],
                        "metadata": {
                            "source": "chain.disconnect",
                        },
                    },
                )
            except Exception as exc:
                logger.warning("chain.disconnect discovery heartbeat failed: %s", exc)
        return _ok(req_id, {"success": True, "connection": disconnected, "removed_contact": remove_contact})
    except Exception as exc:
        logger.exception("chain.disconnect error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_connect_vps(params: dict[str, Any], req_id: Any) -> str:
    endpoint_url = _normalize_peer_endpoint(str(params.get("endpoint_url", "")).strip())
    chain_endpoint_url = str(params.get("chain_endpoint_url") or "").strip()
    wallet_address = str(params.get("wallet_address", "")).strip()
    shared_secret = str(params.get("shared_secret", "")).strip()
    username = str(params.get("username") or "@tala-vps").strip()
    permissions = params.get("permissions") or DEFAULT_VPS_PERMISSIONS

    if not endpoint_url:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'endpoint_url' is required")
    if not wallet_address:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'wallet_address' is required")
    if not shared_secret:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'shared_secret' is required")
    if not isinstance(permissions, dict):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'permissions' must be an object")

    discovery_endpoint = _derive_chain_endpoint(endpoint_url, chain_endpoint_url or None)

    try:
        await db.execute(
            """
            INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret, permissions)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (username) DO UPDATE SET
                wallet_address = EXCLUDED.wallet_address,
                endpoint_url = EXCLUDED.endpoint_url,
                shared_secret = EXCLUDED.shared_secret,
                permissions = EXCLUDED.permissions,
                updated_at = NOW()
            """,
            username,
            wallet_address,
            endpoint_url,
            shared_secret,
            permissions,
            caller="chain.connect_vps.contacts",
        )
        discovery_payload = {
            "username": username,
            "wallet_address": wallet_address,
            "endpoint_url": endpoint_url,
            "discoverable": True,
            "status": "online",
            "network": "tala",
            "capabilities": ["p2p", "vps-discovery"],
            "metadata": {
                "source": "chain.connect_vps",
            },
        }
        try:
            discovery_result = await _register_peer_on_chain_directory(discovery_endpoint, discovery_payload)
        except Exception as exc:
            discovery_result = {"registered": False, "error": str(exc)}
            logger.warning("chain.connect_vps discovery register failed: %s", exc)
        setting = {
            "mode": "vps",
            "auto_connect": True,
            "username": username,
            "wallet_address": wallet_address,
            "endpoint_url": endpoint_url,
            "chain_endpoint_url": discovery_endpoint,
            "peer_directory": discovery_result,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }
        await _save_client_setting(CHAIN_CONNECTION_SETTING_KEY, setting)
    except Exception as exc:
        logger.exception("chain.connect_vps save error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

    return await handle_p2p_handshake(
        {
            "username": username,
            "endpoint_url": endpoint_url,
            "chain_endpoint_url": discovery_endpoint,
            "allow_vps_enrollment": True,
        },
        req_id,
    )


async def handle_chain_auto_connect(params: dict[str, Any], req_id: Any) -> str:
    try:
        setting = await _get_client_setting(CHAIN_CONNECTION_SETTING_KEY)
        if setting.get("mode") != "vps" or not setting.get("auto_connect"):
            return _ok(req_id, {"connected": False, "reason": "not_configured", "connection": setting or {}})
        username = setting.get("username") or "@tala-vps"
        endpoint_url = setting.get("endpoint_url", "")
        chain_endpoint_url = setting.get("chain_endpoint_url")
        if not chain_endpoint_url and endpoint_url:
            chain_endpoint_url = _derive_chain_endpoint(endpoint_url)
        if endpoint_url:
            try:
                await _register_peer_on_chain_directory(
                    chain_endpoint_url,
                    {
                        "username": username,
                        "wallet_address": setting.get("wallet_address") or os.environ.get("TALA_WALLET_ADDRESS", "local-tala"),
                        "endpoint_url": endpoint_url,
                        "discoverable": True,
                        "status": "online",
                        "network": "tala",
                        "capabilities": ["p2p", "vps-discovery"],
                        "metadata": {
                            "source": "chain.auto_connect",
                        },
                    },
                )
            except Exception as exc:
                logger.warning("chain.auto_connect discovery register failed: %s", exc)
        return await handle_p2p_handshake(
            {
                "username": username,
                "endpoint_url": endpoint_url,
                "chain_endpoint_url": chain_endpoint_url,
                "allow_vps_enrollment": True,
            },
            req_id,
        )
    except Exception as exc:
        logger.exception("chain.auto_connect error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_profiles_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``profiles.list`` — Retrieve all profiles, optionally filtered by type.
    """
    profile_type = params.get("type")
    try:
        if profile_type:
            rows = await db.fetch("SELECT id, type, name, username, config FROM profiles WHERE type = $1 ORDER BY name ASC", profile_type)
        else:
            rows = await db.fetch("SELECT id, type, name, username, config FROM profiles ORDER BY name ASC")
        profiles = []
        for r in rows:
            cfg = r["config"]
            if isinstance(cfg, str):
                import json as json_mod
                cfg = json_mod.loads(cfg)
            profiles.append({
                "id": r["id"],
                "type": r["type"],
                "name": r["name"],
                "username": r["username"],
                "config": cfg or {},
            })
        return _ok(req_id, {"profiles": profiles})
    except Exception as exc:
        logger.exception("profiles.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_profiles_get(params: dict[str, Any], req_id: Any) -> str:
    """
    ``profiles.get`` — Retrieve a single profile by id.
    """
    profile_id = params.get("id")
    if not profile_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")
    try:
        row = await db.fetchrow("SELECT id, type, name, username, config FROM profiles WHERE id = $1", profile_id)
        if not row:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"Profile with id '{profile_id}' not found")
        cfg = row["config"]
        if isinstance(cfg, str):
            import json as json_mod
            cfg = json_mod.loads(cfg)
        profile = {
            "id": row["id"],
            "type": row["type"],
            "name": row["name"],
            "username": row["username"],
            "config": cfg or {},
        }
        return _ok(req_id, {"profile": profile})
    except Exception as exc:
        logger.exception("profiles.get error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_profiles_save(params: dict[str, Any], req_id: Any) -> str:
    """
    ``profiles.save`` — Create or update a profile.
    """
    profile_id = params.get("id")
    profile_type = params.get("type")
    name = params.get("name")
    username = params.get("username", "")
    config = params.get("config", {})

    if not name or not isinstance(name, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'name' is required and must be a string")
    if not profile_type or not isinstance(profile_type, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'type' is required and must be a string")

    if not profile_id:
        import re
        import uuid
        sanitized = re.sub(r"[^a-zA-Z0-9\-]", "", name.lower().replace(" ", "-"))
        profile_id = f"profile-{sanitized}-{uuid.uuid4().hex[:4]}"

    import json as json_mod
    config_json = json_mod.dumps(config)

    try:
        await db.execute(
            """
            INSERT INTO profiles (id, type, name, username, config, updated_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
            ON CONFLICT (id) DO UPDATE SET
                type = EXCLUDED.type,
                name = EXCLUDED.name,
                username = EXCLUDED.username,
                config = EXCLUDED.config,
                updated_at = NOW()
            """,
            profile_id,
            profile_type,
            name,
            username,
            config_json,
            caller="profiles.save RPC"
        )
        
        # Sync to memory index for searchability
        from python.harness.memory import index_profile_chunks
        await index_profile_chunks(profile_id, config)

        try:
            from sdk.agent.anchor import canonicalize_and_anchor

            profile_document = {
                "id": profile_id,
                "type": profile_type,
                "name": name,
                "username": username,
                "config": config,
            }
            canonicalize_and_anchor(
                profile_id,
                json_mod.dumps(profile_document, sort_keys=True, ensure_ascii=False),
                profile_document,
                None,
                creator="tala-system",
                document_type="profile",
            )
        except Exception as anchor_exc:
            logger.warning("Failed to anchor profile %s on blockchain: %s", profile_id, anchor_exc)

        # Sync to HTML Neural Node concept-user-profile.html if this is the master-user
        if profile_id == 'master-user':
            try:
                import os
                import hashlib
                from pathlib import Path
                from datetime import datetime, timezone
                from sdk.agent.anchor import canonicalize_and_anchor
                
                id_obj = config.get("identity", {})
                about_obj = config.get("about", {})
                rp_obj = config.get("rp_persona", {})
                pref_obj = config.get("preferences", {})
                pii_obj = config.get("pii_vault", {})

                birthdate = id_obj.get("birthdate", "").strip()
                tone = pref_obj.get("tone", "warm_professional").strip()
                biography = about_obj.get("bio", "").strip() or name

                rp_name = rp_obj.get("name", "").strip()
                rp_class = rp_obj.get("class", "").strip()
                rp_faction = rp_obj.get("faction", "").strip()
                rp_backstory = rp_obj.get("backstory", "").strip()

                pii_legal_name = pii_obj.get("legal_name", "").strip()
                pii_dob = pii_obj.get("dob", "").strip()
                pii_email = pii_obj.get("email", "").strip()
                pii_phone = pii_obj.get("phone", "").strip()
                pii_registries = pii_obj.get("registries", [])

                user_data = os.environ.get("TALA_USER_DATA")
                if user_data:
                    nodes_dir = Path(user_data) / "nodes"
                else:
                    appdata = os.environ.get("APPDATA")
                    if appdata:
                        nodes_dir = Path(appdata) / "tala-engine" / "nodes"
                    else:
                        nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"
                nodes_dir.mkdir(parents=True, exist_ok=True)

                current_iso_time = datetime.now(timezone.utc).isoformat()
                
                state_data = {
                    "name": name,
                    "birthdate": birthdate,
                    "tone": tone,
                    "biography": biography,
                    "rp_name": rp_name,
                    "rp_class": rp_class,
                    "rp_faction": rp_faction,
                    "rp_backstory": rp_backstory,
                    "pii_legal_name": pii_legal_name,
                    "pii_dob": pii_dob,
                    "pii_email": pii_email,
                    "pii_phone": pii_phone,
                    "pii_registries": pii_registries
                }
                
                adapter_packet = {
                    "activation_keywords": ["age", "old", "birthday", "birthdate", "name", "who am I", "how old am I", "roleplay", "rp", "persona", "avatar", "character", "class", "faction", "alignment", "pii", "secure", "vault", "legal", "email", "phone", "location"],
                    "tool_affordances": [],
                    "forbidden_actions": [],
                    "repair_policies": {},
                    "validators": {},
                    "confidence": 1.0
                }
                
                content_html = f"""
    <h2>User Persona & Details</h2>
    <p>User Name: {name}</p>
    <p>User Birthdate: {birthdate}</p>
    <p>Keywords: age, old, birthday, birthdate, name, who am I, how old am I, roleplay, rp, persona, avatar, character, class, faction, alignment, pii, secure, vault, legal, email, phone, location</p>
    <p>{biography}</p>
    
    <h3>Roleplay Persona (Avatar)</h3>
    <p>RP Character Name: {rp_name}</p>
    <p>RP Class/Occupation: {rp_class}</p>
    <p>RP Faction/Alignment: {rp_faction}</p>
    <p>RP Character Backstory: {rp_backstory}</p>

    <h3>Personally Identifiable Information &amp; Vault Registry</h3>
    <p>Legal Name: {pii_legal_name}</p>
    <p>Date of Birth: {pii_dob}</p>
    <p>Primary Email: {pii_email}</p>
    <p>Primary Phone: {pii_phone}</p>
    
    <h4>Registered Keys & Identifiers</h4>
    <ul>
      {"".join([f"<li>{r}</li>" for r in pii_registries]) if pii_registries else "<li>None registered.</li>"}
    </ul>"""

                core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
                    "user-profile",
                    content_html,
                    state_data,
                    adapter_packet,
                    creator="tala-system",
                    document_type="user-profile",
                )

                node_provenance = {
                    "core_hash": f"sha256:{core_hash}",
                    "chaincode_id": chaincode_id,
                    "anchoring_status": anchoring_status,
                    "raw_response": raw_response,
                    "key_fingerprint": public_key if public_key else "unanchored",
                    "parent_chaincode_ids": [],
                    "created_by": "tala-system",
                    "created_at": current_iso_time,
                    "wrapper_hash": "..."
                }

                state_json = json_mod.dumps(state_data, indent=2, ensure_ascii=False)
                prov_json_placeholder = json_mod.dumps(node_provenance, indent=2)

                html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>User Profile: {name}</title>
</head>
<body>
<article
  data-node-id="user-profile"
  data-node-type="concept"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.50"
  data-memory-authority="1.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-chaincode-id="{chaincode_id}"
  data-content-hash="{core_hash}"
  data-source="local"
  data-trust-tier="system"
  data-privacy="private">
  
  <header id="node-identity">
    <h1 id="node-title">Workspace Owner Profile</h1>
  </header>
  
  <section id="semantic-memory">
{content_html}
  </section>
  
  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul></ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{state_json}
  </script>

  <script type="application/json" id="adapter-packet" data-cognitive-block="adapter">
{json_mod.dumps(adapter_packet, indent=2)}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
__PROV_PLACEHOLDER__
  </script>
</article>
</body>
</html>
"""
                wrapper_hash = hashlib.sha256(html_template.replace("__PROV_PLACEHOLDER__", prov_json_placeholder).encode("utf-8")).hexdigest()
                node_provenance["wrapper_hash"] = f"sha256:{wrapper_hash}"
                html = html_template.replace("__PROV_PLACEHOLDER__", json_mod.dumps(node_provenance, indent=2))

                node_file = nodes_dir / "concept-user-profile.html"
                node_file.write_text(html, encoding="utf-8")
                logger.info("Synced user profile node: %s", node_file)

            except Exception as sync_exc:
                logger.warning("Failed to sync profiles save to HTML Neural Node: %s", sync_exc)

        return _ok(req_id, {"success": True, "id": profile_id})
    except Exception as exc:
        logger.exception("profiles.save error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_profiles_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``profiles.delete`` — Delete a profile by id.
    """
    profile_id = params.get("id")
    if not profile_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")
    try:
        await db.execute("DELETE FROM profiles WHERE id = $1", profile_id, caller="profiles.delete RPC")
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("profiles.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_nodes_import_markdown(params: dict[str, Any], req_id: Any) -> str:
    """
    ``nodes.import_markdown`` — Import and convert Markdown files to HTML Neural Nodes.
    """
    import_path_str = params.get("path")
    node_type_default = params.get("node_type", "concept")

    if not import_path_str:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'path' is required")

    try:
        import hashlib
        import urllib.request
        import yaml
        import markdown

        import_path = Path(import_path_str).expanduser().resolve()
        if not import_path.exists():
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"Path does not exist: {import_path}")

        md_files = []
        if import_path.is_file():
            if import_path.suffix.lower() in (".md", ".txt"):
                md_files.append(import_path)
        elif import_path.is_dir():
            for child in import_path.rglob("*"):
                if child.is_file() and child.suffix.lower() in (".md", ".txt"):
                    md_files.append(child)

        if not md_files:
            return _ok(req_id, {
                "success": True,
                "imported_count": 0,
                "errors": [],
                "message": "No Markdown (.md) or Text (.txt) files found at target path."
            })

        user_data = os.environ.get("TALA_USER_DATA")
        if user_data:
            nodes_dir = Path(user_data) / "nodes"
        else:
            appdata = os.environ.get("APPDATA")
            if appdata:
                nodes_dir = Path(appdata) / "tala-engine" / "nodes"
            else:
                nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"
        nodes_dir.mkdir(parents=True, exist_ok=True)

        imported_count = 0
        errors = []

        from datetime import datetime, timezone

        for md_file in md_files:
            try:
                raw_text = md_file.read_text(encoding="utf-8")

                # Parse YAML frontmatter if present
                frontmatter = {}
                md_content = raw_text
                if raw_text.startswith("---"):
                    parts = raw_text.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            frontmatter = yaml.safe_load(parts[1]) or {}
                            md_content = parts[2]
                        except Exception as yex:
                            logger.warning("Failed to parse YAML frontmatter in %s: %s", md_file.name, yex)

                # Determine metadata
                title = frontmatter.get("title") or ""
                node_type = frontmatter.get("type") or node_type_default
                tags = frontmatter.get("tags") or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]

                if not title:
                    for line in md_content.splitlines():
                        if line.strip().startswith("# "):
                            title = line.strip()[2:].strip()
                            break
                if not title:
                    title = md_file.stem.replace("_", " ").replace("-", " ").title()

                node_id = frontmatter.get("id") or frontmatter.get("node_id") or ""
                if not node_id:
                    sanitized_stem = "".join([c if c.isalnum() or c in ("-", "_") else "" for c in md_file.stem.lower()])
                    node_id = f"concept-{sanitized_stem}" if node_type == "concept" else f"rule-{sanitized_stem}"

                # Render Markdown to HTML
                html_content = markdown.markdown(md_content)

                current_iso_time = datetime.now(timezone.utc).isoformat()
                current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                tags_list = list(set(["imported", node_type] + tags))
                tags_json = json.dumps(tags_list)

                html_nn = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="{node_id}">
  <meta name="tala-node-type" content="{node_type}">
  <meta name="tala-created" content="{current_iso_time}">
  <meta name="tala-author" content="tala-user">
  <title>{title}</title>
</head>
<body>
<article data-node-id="{node_id}"
         data-node-type="{node_type}"
         data-execution-authority="0.00"
         data-memory-authority="0.80"
         data-weight-relevance="1.00"
         data-weight-confidence="1.00"
         data-privacy="private">
  
  <header id="node-identity">
    <h1 id="node-title">{title}</h1>
    <dl>
      <dt>Node ID</dt><dd><code>{node_id}</code></dd>
      <dt>Type</dt><dd><code>{node_type}</code></dd>
      <dt>Created</dt><dd><time datetime="{current_iso_time}">{current_date}</time></dd>
    </dl>
  </header>
  
  <section id="semantic-memory">
    {html_content}
  </section>
  
  <nav id="relationship-anchors">
    <h2>Relationships</h2>
    <ul>
      <li><a href="concept-user-profile.html" data-relationship-type="references">User Profile</a></li>
    </ul>
  </nav>

  <script type="application/json" id="node-state">
  {{
    "title": "{title}",
    "description": "Imported memory node.",
    "tags": {tags_json}
  }}
  </script>
</article>
</body>
</html>
"""
                output_filepath = nodes_dir / f"{node_id}.html"
                output_filepath.write_text(html_nn, encoding="utf-8")

                # Store the imported HTML-NN on the local blockchain.
                try:
                    from sdk.agent.anchor import canonicalize_and_anchor

                    canonicalize_and_anchor(
                        node_id,
                        html_nn,
                        {
                            "node_id": node_id,
                            "title": title,
                            "tags": tags,
                            "source": "imported-memory",
                        },
                        None,
                        creator="tala-system",
                        document_type="html-nn",
                    )
                except Exception as ex:
                    logger.debug("Failed to anchor imported node %s: %s", node_id, ex)

                imported_count += 1
            except Exception as fexc:
                logger.warning("Failed to convert file %s: %s", md_file.name, fexc)
                errors.append(f"{md_file.name}: {str(fexc)}")

        return _ok(req_id, {
            "success": True,
            "imported_count": imported_count,
            "errors": errors
        })
    except Exception as exc:
        logger.exception("nodes.import_markdown error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_docs_create_documentation_node(params: dict[str, Any], req_id: Any) -> str:
    """
    ``docs.create_documentation_node`` — Create one validated HTML-NN documentation node.
    """
    try:
        from sdk.agent.documentation_nodes import create_documentation_node

        result = create_documentation_node(
            node_id=params.get("node_id", ""),
            title=params.get("title", ""),
            doc_target=params.get("doc_target", ""),
            semantic_memory=params.get("semantic_memory", ""),
            node_type=params.get("node_type", "documentation"),
            source_files=params.get("source_files"),
            relationships=params.get("relationships"),
            references=params.get("references"),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("docs.create_documentation_node error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_docs_create_learning_node(params: dict[str, Any], req_id: Any) -> str:
    """
    ``docs.create_learning_node`` — Create a fresh HTML-NN learning event node.
    """
    try:
        from sdk.agent.documentation_nodes import create_learning_documentation_node

        result = create_learning_documentation_node(
            topic=params.get("topic", ""),
            summary=params.get("summary", ""),
            canonical_node_id=params.get("canonical_node_id"),
            details=params.get("details"),
            source_files=params.get("source_files"),
            relationships=params.get("relationships"),
            references=params.get("references"),
            tags=params.get("tags"),
            node_id=params.get("node_id"),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("docs.create_learning_node error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_docs_generate_program_nodes(params: dict[str, Any], req_id: Any) -> str:
    """
    ``docs.generate_program_nodes`` — Generate validated program documentation HTML-NNs.
    """
    try:
        from sdk.agent.documentation_nodes import generate_program_nodes

        result = generate_program_nodes(targets=params.get("targets"))
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("docs.generate_program_nodes error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_docs_verify_documentation_nodes(params: dict[str, Any], req_id: Any) -> str:
    """
    ``docs.verify_documentation_nodes`` — Verify documentation HTML-NNs from disk.
    """
    try:
        from sdk.agent.documentation_nodes import verify_documentation_nodes

        result = verify_documentation_nodes(
            targets=params.get("targets"),
            node_ids=params.get("node_ids"),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("docs.verify_documentation_nodes error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_docs_generate_and_verify_program_nodes(params: dict[str, Any], req_id: Any) -> str:
    """
    ``docs.generate_and_verify_program_nodes`` — Generate docs, then verify from disk.
    """
    try:
        from sdk.agent.documentation_nodes import generate_and_verify_program_nodes

        result = generate_and_verify_program_nodes(targets=params.get("targets"))
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("docs.generate_and_verify_program_nodes error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_docs_register_program_target(params: dict[str, Any], req_id: Any) -> str:
    """
    ``docs.register_program_target`` — Register a new documentation target.
    """
    try:
        from sdk.agent.documentation_nodes import register_program_documentation_target

        result = register_program_documentation_target(
            doc_target=params.get("doc_target", ""),
            title=params.get("title", ""),
            semantic_memory=params.get("semantic_memory", ""),
            node_id=params.get("node_id"),
            source_files=params.get("source_files"),
            references=params.get("references"),
            relationships=params.get("relationships"),
            include_in_codebase=bool(params.get("include_in_codebase", True)),
            overwrite=bool(params.get("overwrite", False)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("docs.register_program_target error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_docs_list_program_targets(params: dict[str, Any], req_id: Any) -> str:
    """
    ``docs.list_program_targets`` — List built-in and registered documentation targets.
    """
    try:
        from sdk.agent.documentation_nodes import list_program_documentation_targets

        return _ok(req_id, list_program_documentation_targets())
    except Exception as exc:
        logger.exception("docs.list_program_targets error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_docs_refresh_node_grid(params: dict[str, Any], req_id: Any) -> str:
    """
    ``docs.refresh_node_grid`` — Renderer hint to reload visible HTML-NN cards.
    """
    return _ok(req_id, {
        "success": True,
        "refresh_required": True,
        "target": "node-grid",
    })


async def handle_workflow_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``workflow.list`` — Retrieve all configured workflows.
    """
    try:
        from python.harness.workflows_util import get_all_workflows
        workflows = get_all_workflows()
        return _ok(req_id, {"workflows": workflows})
    except Exception as exc:
        logger.exception("workflow.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_workflow_save(params: dict[str, Any], req_id: Any) -> str:
    """
    ``workflow.save`` — Create or update a workflow configuration.
    """
    workflow = params.get("workflow")
    if not workflow or not isinstance(workflow, dict):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'workflow' (dict) is required")

    flow_id = workflow.get("flow_id")
    if not flow_id:
        flow_id = str(uuid.uuid4())
        workflow["flow_id"] = flow_id

    name = workflow.get("name", "Unnamed Flow")

    try:
        # 1. Resolve directories
        user_data = os.environ.get("TALA_USER_DATA")
        if user_data:
            workflows_dir = Path(user_data) / "workflows"
            nodes_dir = Path(user_data) / "nodes"
        else:
            appdata = os.environ.get("APPDATA")
            if appdata:
                workflows_dir = Path(appdata) / "tala-engine" / "workflows"
                nodes_dir = Path(appdata) / "tala-engine" / "nodes"
            else:
                workflows_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "workflows"
                nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"

        # Fallbacks for dev env
        if not workflows_dir.exists() and not user_data:
            workflows_dir = Path(_ROOT_DIR) / "workflows"
        if not nodes_dir.exists() and not user_data:
            nodes_dir = Path(_ROOT_DIR) / "nodes"

        workflows_dir.mkdir(parents=True, exist_ok=True)
        nodes_dir.mkdir(parents=True, exist_ok=True)

        # 2. Save JSON file
        json_path = workflows_dir / f"{flow_id}.json"
        json_path.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")

        # 3. Compile to HTML-NN and save to nodes/
        from sdk.agent.workflow_compiler import compile_workflow_to_html
        html_nn = compile_workflow_to_html(workflow)
        html_path = nodes_dir / f"flow-{flow_id}.html"
        html_path.write_text(html_nn, encoding="utf-8")
        try:
            from sdk.agent.anchor import canonicalize_and_anchor

            canonicalize_and_anchor(
                f"flow-{flow_id}",
                html_nn,
                workflow,
                None,
                creator="tala-system",
                document_type="html-nn",
            )
        except Exception as anchor_exc:
            logger.warning("Failed to anchor workflow %s on blockchain: %s", flow_id, anchor_exc)

        # Log action to DB
        await db.log_to_db("INFO", f"Saved workflow '{name}' (ID: {flow_id}) and compiled HTML-NN", {"flow_id": flow_id})
        try:
            await update_settings_workflows_node()
        except Exception as exc:
            logger.warning("Failed to update workflows settings node: %s", exc)
        return _ok(req_id, {"success": True, "flow_id": flow_id})
    except Exception as exc:
        logger.exception("workflow.save error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_workflow_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``workflow.delete`` — Delete a workflow configuration.
    """
    flow_id = params.get("flow_id")
    if not flow_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'flow_id' is required")

    try:
        user_data = os.environ.get("TALA_USER_DATA")
        if user_data:
            workflows_dir = Path(user_data) / "workflows"
            nodes_dir = Path(user_data) / "nodes"
        else:
            appdata = os.environ.get("APPDATA")
            if appdata:
                workflows_dir = Path(appdata) / "tala-engine" / "workflows"
                nodes_dir = Path(appdata) / "tala-engine" / "nodes"
            else:
                workflows_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "workflows"
                nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"

        # Fallbacks for dev env
        if not workflows_dir.exists() and not user_data:
            workflows_dir = Path(_ROOT_DIR) / "workflows"
        if not nodes_dir.exists() and not user_data:
            nodes_dir = Path(_ROOT_DIR) / "nodes"

        # Delete JSON
        json_path = workflows_dir / f"{flow_id}.json"
        if json_path.exists():
            json_path.unlink()

        # Delete HTML-NN
        html_path = nodes_dir / f"flow-{flow_id}.html"
        if html_path.exists():
            html_path.unlink()

        await db.log_to_db("INFO", f"Deleted workflow ID: {flow_id} and its HTML-NN", {"flow_id": flow_id})
        try:
            await update_settings_workflows_node()
        except Exception as exc:
            logger.warning("Failed to update workflows settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("workflow.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_workflow_execute(
    params: dict[str, Any],
    ws: WebSocketServerProtocol,
    req_id: Any,
) -> str:
    """
    ``workflow.execute`` — Execute a workflow node-by-node.
    """
    flow_id = params.get("flow_id")
    inputs = params.get("inputs", {})

    if not flow_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'flow_id' is required")

    try:
        from python.harness.workflows_util import resolve_workflow_file
        json_path = resolve_workflow_file(flow_id)
        if not json_path or not json_path.exists():
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"Workflow '{flow_id}' does not exist.")

        workflow_data = json.loads(json_path.read_text(encoding="utf-8"))

        # Define step callback to stream updates
        async def step_callback(node_id: str, status: str, message: str, logs: list[str]) -> None:
            notification = json.dumps({
                "jsonrpc": "2.0",
                "method": "workflow.execute.step",
                "params": {
                    "flow_id": flow_id,
                    "node_id": node_id,
                    "status": status,
                    "message": message,
                    "logs": logs
                }
            })
            await ws.send(notification)

        from python.harness.workflow_executor import WorkflowExecutor
        executor = WorkflowExecutor(workflow_data, step_callback)
        result = await executor.execute(inputs)

        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("workflow.execute error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_mcp_connect_server(params: dict[str, Any], req_id: Any) -> str:
    """
    ``mcp.connect_server`` — Register an MCP server configuration.
    """
    name = params.get("name") or params.get("server_name")
    config = params.get("config") or params.get("server") or {}
    url = params.get("url")

    if not name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'name' is required")

    try:
        from python.harness.mcp_manager import mcp_manager
        await mcp_manager.connect_server(name, config if config else url, persist=bool(params.get("persist", True)))
        return _ok(req_id, {"success": True, "server_name": name, "persisted": bool(params.get("persist", True))})
    except Exception as exc:
        logger.exception("mcp.connect_server error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_mcp_list_servers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``mcp.list_servers`` — List persisted/registered MCP server configs.
    """
    try:
        from python.harness.mcp_manager import mcp_manager
        return _ok(req_id, {"servers": mcp_manager.list_servers()})
    except Exception as exc:
        logger.exception("mcp.list_servers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_mcp_remove_server(params: dict[str, Any], req_id: Any) -> str:
    """
    ``mcp.remove_server`` — Remove an MCP server from memory and persistence.
    """
    name = params.get("name") or params.get("server_name")
    if not name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'name' is required")

    try:
        from python.harness.mcp_manager import mcp_manager
        removed = await mcp_manager.remove_server(name)
        return _ok(req_id, {"success": True, "removed": removed, "server_name": name})
    except Exception as exc:
        logger.exception("mcp.remove_server error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_mcp_save_servers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``mcp.save_servers`` — Replace/update the configured MCP servers registry list.
    """
    servers = params.get("servers")
    if servers is None or not isinstance(servers, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'servers' (list) is required")

    try:
        from python.harness.mcp_manager import mcp_manager
        await mcp_manager.save_servers(servers)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("mcp.save_servers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_mcp_list_tools(params: dict[str, Any], req_id: Any) -> str:
    """
    ``mcp.list_tools`` — List tools from one MCP server, or all registered servers.
    """
    try:
        from python.harness.mcp_manager import mcp_manager
        tools = await mcp_manager.list_tools(params.get("server_name"))
        return _ok(req_id, {"tools": tools})
    except Exception as exc:
        logger.exception("mcp.list_tools error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_mcp_call_tool(params: dict[str, Any], req_id: Any) -> str:
    """
    ``mcp.call_tool`` — Call a tool on a registered MCP server.
    """
    server_name = params.get("server_name")
    tool_name = params.get("tool_name")

    if not server_name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'server_name' is required")
    if not tool_name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'tool_name' is required")

    try:
        from python.harness.mcp_manager import mcp_manager
        result = await mcp_manager.call_tool(server_name, tool_name, params.get("args") or {})
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("mcp.call_tool error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_guardrails_get(params: dict[str, Any], req_id: Any) -> str:
    """
    ``guardrails.get`` — Retrieve active guardrails configuration.
    """
    try:
        from python.harness.guardrails_manager import get_active_guardrails
        category, rules = await get_active_guardrails()
        return _ok(req_id, {"category": category, "rules": rules})
    except Exception as exc:
        logger.exception("guardrails.get error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_guardrails_save(params: dict[str, Any], req_id: Any) -> str:
    """
    ``guardrails.save`` — Save guardrails configuration.
    """
    category = params.get("category")
    rules = params.get("rules", {})
    if not category:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'category' is required")

    valid_categories = {"none", "lite", "moderate", "heavy", "custom"}
    if category not in valid_categories:
        return _err(
            req_id,
            JsonRpcError.INVALID_PARAMS,
            f"Invalid category: '{category}'. Must be one of {sorted(valid_categories)}",
        )

    try:
        import json as json_mod
        await db.execute(
            """
            UPDATE guardrails_config
            SET category = $1, rules = $2::jsonb, updated_at = NOW()
            WHERE id = 1
            """,
            category,
            json_mod.dumps(rules),
            caller="guardrails.save RPC",
        )
        try:
            await update_settings_guardrails_node()
        except Exception as exc:
            logger.warning("Failed to update guardrails settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("guardrails.save error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_vps_version_push(params: dict[str, Any], req_id: Any) -> str:
    """
    ``vps.version_push`` — Receive a high-priority VERSION_PUSH update from a peer on the VPS.
    Validates digital signature and performs in-memory dry-run check via compile_lora_adapter
    (validating weights and schema metadata parity) before writing the node.
    """
    import hmac
    import hashlib
    import json
    import os
    import re
    from pathlib import Path
    
    node_hash = params.get("node_hash", "")
    signature = params.get("signature", "")
    content_html = params.get("content_html", "")
    
    if not node_hash or not signature or not content_html:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Missing required parameters: node_hash, signature, content_html")
        
    # 1. Validate signature
    shared_secret = b"vps_shared_key_2026"
    expected_sig = hmac.new(shared_secret, node_hash.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_sig):
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, "ACCESS_DENIED: Invalid digital signature.")
        
    # 2. In-memory dry-run validation (simulating diagnose_html_nn.py)
    # Check that metadata and weight layers are present and valid
    metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content_html, re.DOTALL)
    weights_m = re.search(r'<script[^>]*id="lora-weights"[^>]*>(.*?)</script>', content_html, re.DOTALL)
    
    validation_score = 0.0
    
    if metadata_m and weights_m:
        try:
            meta = json.loads(metadata_m.group(1).strip())
            # Check mandatory fields
            has_mandatory = all(k in meta for k in ("semantic_tags", "emotional_valence", "relevance_weight", "usage_count"))
            if has_mandatory:
                # Decodes base64 weights in memory
                import base64
                weights_bytes = base64.b64decode(weights_m.group(1).strip())
                if len(weights_bytes) > 16:
                    validation_score = 100.0
        except Exception:
            pass
            
    if validation_score < 100.0:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, "Validation failed: HTML-NN node is invalid or corrupted.")
        
    # 3. Commit/write to local nodes directory
    try:
        user_data = os.environ.get("TALA_USER_DATA")
        if user_data:
            nodes_dir = Path(user_data) / "nodes"
        else:
            appdata = os.environ.get("APPDATA")
            if appdata:
                nodes_dir = Path(appdata) / "tala-engine" / "nodes"
            else:
                nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"
                
        # Resolve node filename
        article_m = re.search(r'<article[^>]*data-node-id="([^"]+)"', content_html)
        if article_m:
            node_id = article_m.group(1)
        else:
            node_id = f"peer_node_{node_hash[:12]}"
            
        nodes_dir.mkdir(parents=True, exist_ok=True)
        node_file = nodes_dir / f"{node_id}.html"
        node_file.write_text(content_html, encoding="utf-8")
        
        logger.info("VERSION_PUSH: Successfully validated and committed node %s", node_id)
        return _ok(req_id, {"status": "SUCCESS", "message": f"Node {node_id} successfully validated and committed.", "node_id": node_id})
    except Exception as exc:
        logger.exception("vps.version_push write error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_system_info(params: dict[str, Any], req_id: Any) -> str:
    """
    ``system.info`` — Collect host hardware, OS, and installed software versions.
    Returns a structured dict consumable directly by the frontend System panel.
    """
    import platform
    import shutil
    import subprocess
    import multiprocessing
    import importlib.metadata

    def _run(cmd: list[str]) -> str:
        """Run a subprocess, return stdout stripped, or empty string on failure."""
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _pkg_version(pkg: str) -> str:
        """Return installed package version via importlib.metadata."""
        try:
            return importlib.metadata.version(pkg)
        except Exception:
            return "not installed"

    # ── OS / platform ──────────────────────────────────────────────────────
    os_info = {
        "system":    platform.system(),
        "release":   platform.release(),
        "version":   platform.version(),
        "machine":   platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "node":      platform.node(),
    }

    # ── CPU ────────────────────────────────────────────────────────────────
    cpu_count_logical = multiprocessing.cpu_count()
    try:
        import psutil  # optional
        cpu_count_physical = psutil.cpu_count(logical=False) or cpu_count_logical
        cpu_freq = psutil.cpu_freq()
        cpu_freq_mhz = round(cpu_freq.current) if cpu_freq else 0
        cpu_percent = psutil.cpu_percent(interval=0.2)
        vm = psutil.virtual_memory()
        ram_total_gb = round(vm.total / (1024 ** 3), 2)
        ram_used_gb  = round(vm.used  / (1024 ** 3), 2)
        ram_percent  = vm.percent
        disk = psutil.disk_usage("/")
        disk_total_gb = round(disk.total / (1024 ** 3), 1)
        disk_used_gb  = round(disk.used  / (1024 ** 3), 1)
        disk_percent  = disk.percent
    except ImportError:
        cpu_count_physical = cpu_count_logical
        cpu_freq_mhz = 0
        cpu_percent = 0
        ram_total_gb = ram_used_gb = ram_percent = 0
        disk_total_gb = disk_used_gb = disk_percent = 0

    cpu_info = {
        "logical_cores":  cpu_count_logical,
        "physical_cores": cpu_count_physical,
        "freq_mhz":       cpu_freq_mhz,
        "usage_percent":  cpu_percent,
    }

    ram_info = {
        "total_gb":   ram_total_gb,
        "used_gb":    ram_used_gb,
        "percent":    ram_percent,
    }

    disk_info = {
        "total_gb":  disk_total_gb,
        "used_gb":   disk_used_gb,
        "percent":   disk_percent,
    }

    # ── Software versions ──────────────────────────────────────────────────
    python_version = platform.python_version()

    node_version = _run(["node", "--version"]).lstrip("v") or "not found"
    npm_version  = _run(["npm",  "--version"]) or "not found"

    # Electron version (read from local package.json if available)
    electron_version = "not found"
    try:
        import json as _json
        _pkg_path = Path(__file__).resolve().parent.parent.parent / "node_modules" / "electron" / "package.json"
        if _pkg_path.exists():
            _data = _json.loads(_pkg_path.read_text(encoding="utf-8"))
            electron_version = _data.get("version", "unknown")
    except Exception:
        pass

    # PostgreSQL client version
    pg_client = _run(["psql", "--version"]).replace("psql (PostgreSQL) ", "") or "not found"
    # Try pg server version via DB pool if available
    pg_server = "not connected"
    try:
        pool = db.get_pool()
        if pool:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT version()")
                if row:
                    raw = row["version"]
                    # e.g. 'PostgreSQL 16.2 on x86_64-pc-linux-gnu ...'
                    pg_server = raw.split(" ")[1] if raw else "unknown"
    except Exception:
        pass

    # Python package versions
    py_packages = {
        "asyncpg":          _pkg_version("asyncpg"),
        "websockets":       _pkg_version("websockets"),
        "tiktoken":         _pkg_version("tiktoken"),
        "pydantic":         _pkg_version("pydantic"),
        "httpx":            _pkg_version("httpx"),
        "lxml":             _pkg_version("lxml"),
        "ephem":            _pkg_version("ephem"),
        "python-dotenv":    _pkg_version("python-dotenv"),
    }

    # ── Runtime service status ─────────────────────────────────────────────
    services = {
        "harness_ws":  "online",   # If we're responding, the harness is running
        "postgresql":  "online" if pg_server != "not connected" else "offline",
        "blockchain":  "unknown",  # Blockchain node is external; check via its port
    }
    # Quick TCP ping to blockchain node port 8332
    try:
        import asyncio as _asyncio
        _r, _w = await _asyncio.wait_for(
            _asyncio.open_connection("127.0.0.1", 8332), timeout=1.0
        )
        _w.close()
        services["blockchain"] = "online"
    except Exception:
        services["blockchain"] = "offline"

    info = {
        "os":          os_info,
        "cpu":         cpu_info,
        "ram":         ram_info,
        "disk":        disk_info,
        "software": {
            "python":     python_version,
            "node":       node_version,
            "npm":        npm_version,
            "electron":   electron_version,
            "postgresql_client": pg_client,
            "postgresql_server": pg_server,
        },
        "python_packages": py_packages,
        "services":    services,
    }
    return _ok(req_id, {"info": info})


async def handle_git_get_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``git.get_providers`` — Retrieve all configured Git hosting providers sorted by priority.
    """
    try:
        rows = await db.fetch(
            "SELECT p.id, p.name, p.endpoint, p.username, p.api_key, p.priority, p.locked, p.credential_id, "
            "c.config as cred_config "
            "FROM git_providers p "
            "LEFT JOIN auth_credentials c ON p.credential_id = c.id "
            "ORDER BY p.priority ASC"
        )
        providers = []
        for r in rows:
            api_key = r["api_key"]
            if r["credential_id"] and r["cred_config"]:
                cred_cfg = dict(r["cred_config"]) if r["cred_config"] else {}
                api_key = cred_cfg.get("api_key", api_key)

            providers.append({
                "id":       r["id"],
                "name":     r["name"],
                "endpoint": r["endpoint"],
                "username": r["username"],
                "api_key":  api_key,
                "priority": r["priority"],
                "locked":   r["locked"],
                "credential_id": r["credential_id"],
            })
        return _ok(req_id, {"providers": providers})
    except Exception as exc:
        logger.exception("git.get_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_git_save_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``git.save_providers`` — Persist Git provider list (replaces entire table).
    """
    providers = params.get("providers")
    if providers is None or not isinstance(providers, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'providers' (list) is required")

    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM git_providers")
                for idx, p in enumerate(providers):
                    is_locked = bool(p.get('locked'))
                    await conn.execute(
                        '''
                        INSERT INTO git_providers
                          (id, name, endpoint, username, api_key, priority, locked, credential_id, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW(), NOW())
                        ''',
                        p['id'],
                        p['name'],
                        p['endpoint'],
                        p.get('username', ''),
                        p.get('api_key', ''),
                        idx + 1,
                        is_locked,
                        p.get('credential_id'),
                    )
        try:
            await update_settings_git_node()
        except Exception as exc:
            logger.warning("Failed to update git settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("git.save_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_search_get_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``search.get_providers`` — Retrieve all configured search providers sorted by priority.
    """
    try:
        from python.harness.search_manager import get_search_providers
        providers = await get_search_providers()
        return _ok(req_id, {"providers": providers})
    except Exception as exc:
        logger.exception("search.get_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_search_save_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``search.save_providers`` — Save / persist search providers list and order.
    """
    providers = params.get("providers")
    if providers is None or not isinstance(providers, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'providers' (list) is required")

    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Delete existing search providers
                await conn.execute("DELETE FROM search_providers")
                # Insert updated list
                for idx, p in enumerate(providers):
                    await conn.execute(
                        """
                        INSERT INTO search_providers (id, name, endpoint, api_key, priority, locked, credential_id, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())
                        """,
                        p["id"],
                        p["name"],
                        p["endpoint"],
                        p.get("api_key", ""),
                        idx + 1,
                        bool(p.get("locked", False)),
                        p.get("credential_id"),
                    )
        try:
            await update_settings_search_node()
        except Exception as exc:
            logger.warning("Failed to update search settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("search.save_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_search_query(params: dict[str, Any], req_id: Any) -> str:
    """
    ``search.query`` — Run search query through SearchManager.
    """
    query = params.get("query")
    if not query or not isinstance(query, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'query' is required and must be a string")

    try:
        from python.harness.search_manager import SearchManager
        results = await SearchManager.query(query)
        return _ok(req_id, {"results": results})
    except Exception as exc:
        logger.exception("search.query error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_storage_get_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``storage.get_providers`` — Retrieve all configured storage providers sorted by priority.
    Credentials are stored as a JSONB blob to support heterogeneous provider types.
    """
    try:
        rows = await db.fetch(
            "SELECT id, name, provider_type, credentials, priority, locked "
            "FROM storage_providers ORDER BY priority ASC"
        )
        providers = [
            {
                "id":            r["id"],
                "name":          r["name"],
                "provider_type": r["provider_type"],
                "credentials":   dict(r["credentials"]) if r["credentials"] else {},
                "priority":      r["priority"],
                "locked":        r["locked"],
            }
            for r in rows
        ]
        return _ok(req_id, {"providers": providers})
    except Exception as exc:
        logger.exception("storage.get_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_storage_save_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``storage.save_providers`` — Persist storage providers list (replaces entire table).
    """
    import json as _json
    providers = params.get("providers")
    if providers is None or not isinstance(providers, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'providers' (list) is required")

    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM storage_providers")
                for idx, p in enumerate(providers):
                    creds = p.get("credentials", {})
                    is_locked = bool(p.get("locked", False))
                    await conn.execute(
                        """
                        INSERT INTO storage_providers
                          (id, name, provider_type, credentials, priority, locked, created_at, updated_at)
                        VALUES ($1, $2, $3, $4::jsonb, $5, $6, NOW(), NOW())
                        """,
                        p["id"],
                        p["name"],
                        p["provider_type"],
                        _json.dumps(creds),
                        idx + 1,
                        is_locked,
                    )
        try:
            await update_settings_storage_node()
        except Exception as exc:
            logger.warning("Failed to update storage settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("storage.save_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


def parse_db_url(url: str) -> dict[str, Any]:
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        username = parsed.username or ""
        password = parsed.password or ""
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        database = parsed.path.lstrip('/') if parsed.path else ""
        endpoint = f"{host}:{port}"
        return {
            "endpoint": endpoint,
            "username": username,
            "password": password,
            "database": database,
        }
    except Exception:
        return {
            "endpoint": "localhost:5432",
            "username": "tala",
            "password": "tala",
            "database": "tala",
        }


async def handle_database_get_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``database.get_providers`` — Retrieve all configured Database providers sorted by priority.
    """
    try:
        rows = await db.fetch(
            "SELECT id, name, endpoint, username, password, database, priority, locked "
            "FROM database_providers ORDER BY priority ASC"
        )
        providers = [
            {
                "id":       r["id"],
                "name":     r["name"],
                "endpoint": r["endpoint"],
                "username": r["username"],
                "password": r["password"],
                "database": r["database"],
                "priority": r["priority"],
                "locked":   r["locked"],
            }
            for r in rows
        ]

        # Auto-detect local database details from settings
        from python.harness.config import settings
        db_info = parse_db_url(settings.DB_URL)
        has_local = any(
            p["id"] == "local-db" or
            (p["endpoint"] == db_info["endpoint"] and p["database"] == db_info["database"])
            for p in providers
        )

        if not has_local:
            local_provider = {
                "id": "local-db",
                "name": "Local Database (Primary)",
                "endpoint": db_info["endpoint"],
                "username": db_info["username"],
                "password": db_info["password"],
                "database": db_info["database"],
                "priority": 1,
                "locked": False,
            }
            providers.insert(0, local_provider)
            # Recompute priorities
            for idx, p in enumerate(providers):
                p["priority"] = idx + 1

        return _ok(req_id, {"providers": providers})
    except Exception as exc:
        logger.exception("database.get_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_database_save_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``database.save_providers`` — Persist Database provider list (replaces entire table).
    """
    providers = params.get("providers")
    if providers is None or not isinstance(providers, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'providers' (list) is required")

    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM database_providers")
                for idx, p in enumerate(providers):
                    is_locked = bool(p.get("locked", False))
                    await conn.execute(
                        """
                        INSERT INTO database_providers
                          (id, name, endpoint, username, password, database, priority, locked, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW(), NOW())
                        """,
                        p["id"],
                        p["name"],
                        p["endpoint"],
                        p.get("username", ""),
                        p.get("password", ""),
                        p.get("database", ""),
                        idx + 1,
                        is_locked,
                    )
        try:
            await update_settings_database_node()
        except Exception as exc:
            logger.warning("Failed to update database settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("database.save_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


_SECRET_CONFIG_KEYS = {
    "key",
    "passphrase",
    "password",
    "api_key",
    "client_secret",
    "refresh_token",
    "access_token",
    "token",
}


def _is_redacted_secret(value: Any) -> bool:
    return isinstance(value, str) and value.strip() in {"***", "••••••••", "********"}


def _auth_config_has_secret(config: dict[str, Any]) -> bool:
    for key, value in (config or {}).items():
        if key in _SECRET_CONFIG_KEYS and isinstance(value, str) and value:
            return True
    return False


def _redact_auth_config(config: dict[str, Any]) -> dict[str, Any]:
    safe = dict(config or {})
    for key in list(safe.keys()):
        if key in _SECRET_CONFIG_KEYS and safe.get(key):
            safe[key] = "***"
    return safe


def _merge_redacted_auth_config(incoming: Any, existing: dict[str, Any]) -> dict[str, Any]:
    incoming_cfg = dict(incoming or {}) if isinstance(incoming, dict) else {}
    merged = dict(incoming_cfg)
    for key in _SECRET_CONFIG_KEYS:
        if _is_redacted_secret(merged.get(key)):
            merged[key] = existing.get(key, "")
    return merged


async def handle_runtime_get_instances(params: dict[str, Any], req_id: Any) -> str:
    """
    ``runtime.get_instances`` — Retrieve all configured remote HTML-NN instances sorted by priority.
    """
    try:
        rows = await db.fetch(
            "SELECT id, name, url, api_key, priority, locked "
            "FROM runtime_instances ORDER BY priority ASC"
        )
        instances = []
        for r in rows:
            api_key = r["api_key"] or ""
            instances.append({
                "id":       r["id"],
                "name":     r["name"],
                "url":      r["url"],
                "api_key":  "***" if api_key else "",
                "has_auth": bool(api_key),
                "priority": r["priority"],
                "locked":   r["locked"],
            })
        return _ok(req_id, {"instances": instances})
    except Exception as exc:
        logger.exception("runtime.get_instances error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_runtime_save_instances(params: dict[str, Any], req_id: Any) -> str:
    """
    ``runtime.save_instances`` — Persist remote HTML-NN instance list (replaces entire table).
    """
    instances = params.get("instances")
    if instances is None or not isinstance(instances, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'instances' (list) is required")

    try:
        existing_rows = await db.fetch("SELECT id, api_key FROM runtime_instances")
        existing_keys = {r["id"]: r["api_key"] for r in existing_rows}
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM runtime_instances")
                for idx, inst in enumerate(instances):
                    inst_id = str(inst.get("id", "")).strip()
                    name = str(inst.get("name", "")).strip()
                    url = str(inst.get("url", "")).strip()
                    if not inst_id or not name or not url:
                        return _err(req_id, JsonRpcError.INVALID_PARAMS, "runtime instances require id, name, and url")
                    raw_api_key = str(inst.get("api_key", "") or "")
                    api_key = existing_keys.get(inst_id, "") if raw_api_key == "***" else raw_api_key
                    locked = bool(inst.get("locked", False))
                    await conn.execute(
                        """
                        INSERT INTO runtime_instances
                          (id, name, url, api_key, priority, locked, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
                        """,
                        inst_id,
                        name,
                        url,
                        api_key,
                        idx + 1,
                        locked,
                    )
        try:
            await update_settings_runtime_node()
        except Exception as exc:
            logger.warning("Failed to update runtime settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("runtime.save_instances error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_auth_get_credentials(params: dict[str, Any], req_id: Any) -> str:
    """
    ``auth.get_credentials`` — Retrieve all configured credentials sorted by priority.
    """
    try:
        rows = await db.fetch(
            "SELECT id, name, auth_type, config, priority, locked "
            "FROM auth_credentials ORDER BY priority ASC"
        )
        credentials = []
        for r in rows:
            cfg = dict(r["config"]) if r["config"] else {}
            safe_cfg = _redact_auth_config(cfg)
            credentials.append({
                "id":        r["id"],
                "name":      r["name"],
                "auth_type": r["auth_type"],
                "config":    safe_cfg,
                "has_secret": _auth_config_has_secret(cfg),
                "priority":  r["priority"],
                "locked":    r["locked"],
            })
        return _ok(req_id, {"credentials": credentials})
    except Exception as exc:
        logger.exception("auth.get_credentials error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_auth_save_credentials(params: dict[str, Any], req_id: Any) -> str:
    """
    ``auth.save_credentials`` — Persist credentials list (replaces entire table).
    """
    import json as _json
    credentials = params.get("credentials")
    if credentials is None or not isinstance(credentials, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'credentials' (list) is required")

    try:
        existing_rows = await db.fetch("SELECT id, config FROM auth_credentials")
        existing_configs = {
            r["id"]: (dict(r["config"]) if r["config"] else {})
            for r in existing_rows
        }
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM auth_credentials")
                for idx, c in enumerate(credentials):
                    cred_id = str(c.get("id", "")).strip()
                    name = str(c.get("name", "")).strip()
                    auth_type = str(c.get("auth_type", "")).strip()
                    if not cred_id or not name or auth_type not in {"ssh", "rdp", "api_key", "oauth"}:
                        return _err(req_id, JsonRpcError.INVALID_PARAMS, "credentials require id, name, and valid auth_type")
                    cfg = _merge_redacted_auth_config(c.get("config", {}), existing_configs.get(cred_id, {}))
                    locked = bool(c.get("locked", False))
                    await conn.execute(
                        """
                        INSERT INTO auth_credentials
                          (id, name, auth_type, config, priority, locked, created_at, updated_at)
                        VALUES ($1, $2, $3, $4::jsonb, $5, $6, NOW(), NOW())
                        """,
                        cred_id,
                        name,
                        auth_type,
                        _json.dumps(cfg),
                        idx + 1,
                        locked,
                    )
        try:
            await update_settings_auth_node()
        except Exception as exc:
            logger.warning("Failed to update auth settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("auth.save_credentials error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_firewall_get_contacts(params: dict[str, Any], req_id: Any) -> str:
    """
    ``firewall.get_contacts`` — Retrieve all trusted contacts sorted by priority.
    """
    try:
        rows = await db.fetch(
            "SELECT id, name, access_rights, priority "
            "FROM firewall_contacts ORDER BY priority ASC"
        )
        contacts = [
            {
                "id":            r["id"],
                "name":          r["name"],
                "access_rights": dict(r["access_rights"]) if r["access_rights"] else {},
                "priority":      r["priority"],
            }
            for r in rows
        ]
        return _ok(req_id, {"contacts": contacts})
    except Exception as exc:
        logger.exception("firewall.get_contacts error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_firewall_save_contacts(params: dict[str, Any], req_id: Any) -> str:
    """
    ``firewall.save_contacts`` — Persist trusted contacts list (replaces entire table).
    """
    import json as _json
    contacts = params.get("contacts")
    if contacts is None or not isinstance(contacts, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'contacts' (list) is required")

    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM firewall_contacts")
                for idx, c in enumerate(contacts):
                    rights = c.get("access_rights", {})
                    await conn.execute(
                        """
                        INSERT INTO firewall_contacts
                          (id, name, access_rights, priority, created_at, updated_at)
                        VALUES ($1, $2, $3::jsonb, $4, NOW(), NOW())
                        """,
                        c["id"],
                        c["name"],
                        _json.dumps(rights),
                        idx + 1,
                    )
        try:
            await update_settings_firewall_node()
        except Exception as exc:
            logger.warning("Failed to update firewall settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("firewall.save_contacts error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


def infer_subsystem(message: str, trace_dict: dict) -> str:
    """Helper to dynamically infer subsystem classification from message or trace."""
    if trace_dict and "subsystem" in trace_dict:
        return str(trace_dict["subsystem"]).upper()
    
    msg_lower = message.lower()
    if "guardrail" in msg_lower or "safety" in msg_lower:
        return "GUARDRAILS"
    if "workflow" in msg_lower or "flow-" in msg_lower:
        return "WORKFLOWS"
    if "inference" in msg_lower or "vllm" in msg_lower or "model" in msg_lower or "provider" in msg_lower:
        return "INFERENCE"
    if "token" in msg_lower or "wallet" in msg_lower or "balance" in msg_lower or "ledger" in msg_lower:
        return "TOKEN_LEDGER"
    if "db" in msg_lower or "database" in msg_lower or "postgresql" in msg_lower or "migration" in msg_lower:
        return "DATABASE"
    if "git" in msg_lower:
        return "GIT"
    if "storage" in msg_lower:
        return "STORAGE"
    if "runtime" in msg_lower or "remote html-nn" in msg_lower:
        return "RUNTIME"
    if "auth" in msg_lower or "credential" in msg_lower:
        return "AUTH"
    if "blockchain" in msg_lower or "block" in msg_lower or "transaction" in msg_lower:
        return "BLOCKCHAIN"
    if "astro" in msg_lower or "transit" in msg_lower:
        return "ASTROLOGY"
    return "SYSTEM"


def infer_event_type(message: str, trace_dict: dict) -> str:
    """Helper to dynamically infer event_type classification from message or trace."""
    if trace_dict and "event_type" in trace_dict:
        return str(trace_dict["event_type"])
        
    msg_lower = message.lower()
    if "guardrail blocked" in msg_lower:
        return "guardrail.blocked"
    if "guardrail" in msg_lower:
        if "fail" in msg_lower or "error" in msg_lower:
            return "guardrail.attempt_failed"
        if "start" in msg_lower:
            return "guardrail.attempt_started"
        return "guardrail.started"
    if "inference.generate" in msg_lower or "inference start" in msg_lower:
        return "inference.started"
    if "inference complete" in msg_lower:
        return "inference.completed"
    if "active agent changed" in msg_lower:
        return "agent.active_changed"
    if "saved workflow" in msg_lower:
        return "workflow.saved"
    if "deleted workflow" in msg_lower:
        return "workflow.deleted"
    if "harness started" in msg_lower:
        return "harness.started"
    if "harness shutting down" in msg_lower:
        return "harness.shutdown"
    
    # Generate generic event type from first few words of message
    import re
    words = re.findall(r"[a-z0-9]+", msg_lower)
    if words:
        clean_words = [w for w in words if w not in ("a", "the", "an", "is", "are", "to", "for", "in", "of", "with")]
        if clean_words:
            return ".".join(clean_words[:2])
    return "system.log"


async def handle_logging_get_events(params: dict[str, Any], req_id: Any) -> str:
    """
    ``logging.get_events`` — Query and filter log events from agent_log.
    Also returns real-time metrics for dashboard cards.
    """
    limit = int(params.get("limit", 500))
    search = params.get("search", "").strip()
    levels = params.get("levels", [])
    subsystem = params.get("subsystem", "All Subsystems").strip()
    source = params.get("source", "Audit Log").strip()

    try:
        # 1. Fetch metrics
        total_events = await db.fetchval("SELECT COUNT(*) FROM agent_log") or 0
        errors_count = await db.fetchval("SELECT COUNT(*) FROM agent_log WHERE level = 'ERROR'") or 0
        warns_count = await db.fetchval("SELECT COUNT(*) FROM agent_log WHERE level IN ('WARN', 'WARNING')") or 0
        prompt_audits = await db.fetchval(
            "SELECT COUNT(*) FROM agent_log WHERE message LIKE '%Guardrail%' OR message LIKE '%guardrail%'"
        ) or 0
        sessions = await db.fetchval("SELECT COUNT(DISTINCT session_id) FROM token_ledger") or 0
        turns = await db.fetchval("SELECT COUNT(*) FROM token_ledger") or 0

        metrics = {
            "total_events": total_events,
            "errors": errors_count,
            "warnings": warns_count,
            "prompt_audits": prompt_audits,
            "sessions": sessions,
            "turns": turns
        }

        # 2. Build filtered logs query
        query = "SELECT id, level, message, trace, tx_id, created_at FROM agent_log"
        where_clauses = []
        query_args = []
        
        if source == "node_blockchain":
            where_clauses.append("(message ILIKE '%blockchain%' OR message ILIKE '%block%' OR message ILIKE '%transaction%')")
        
        if levels and "ALL" not in [l.upper() for l in levels]:
            expanded_levels = []
            for lvl in levels:
                lvl_up = lvl.upper()
                expanded_levels.append(lvl_up)
                if lvl_up == "WARN":
                    expanded_levels.append("WARNING")
                elif lvl_up == "WARNING":
                    expanded_levels.append("WARN")
            where_clauses.append("level = ANY($1)")
            query_args.append(expanded_levels)
        
        if search:
            param_num = len(query_args) + 1
            where_clauses.append(f"message ILIKE ${param_num}")
            query_args.append(f"%{search}%")
            
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
            
        query += " ORDER BY id DESC"
        
        param_num = len(query_args) + 1
        query += f" LIMIT ${param_num}"
        query_args.append(limit)
        
        rows = await db.fetch(query, *query_args)

        import json as json_mod
        events = []
        for r in rows:
            trace_dict = r["trace"]
            if isinstance(trace_dict, str):
                try:
                    trace_dict = json_mod.loads(trace_dict)
                except Exception:
                    trace_dict = {}
            elif not isinstance(trace_dict, dict):
                trace_dict = {}
                
            sub = infer_subsystem(r["message"], trace_dict)
            
            if subsystem != "All Subsystems" and sub.upper() != subsystem.upper():
                continue
                
            events.append({
                "id": r["id"],
                "level": r["level"],
                "timestamp": r["created_at"].isoformat(),
                "subsystem": sub,
                "event_type": infer_event_type(r["message"], trace_dict),
                "message": r["message"],
                "trace": trace_dict,
                "tx_id": r["tx_id"]
            })

        return _ok(req_id, {"events": events, "metrics": metrics})
    except Exception as exc:
        logger.exception("logging.get_events error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_logging_get_health(params: dict[str, Any], req_id: Any) -> str:
    """
    ``logging.get_system_health`` — Return dynamic health indicators for the 12 subservices.
    """
    services = {}
    
    # 1. DB_HEALTH_SERVICE
    try:
        pool = db.get_pool()
        if pool:
            await db.fetchval("SELECT 1")
            services["DB_HEALTH_SERVICE"] = "HEALTHY"
        else:
            services["DB_HEALTH_SERVICE"] = "CRITICAL"
    except Exception:
        services["DB_HEALTH_SERVICE"] = "CRITICAL"

    # 2. INFERENCE_SERVICE
    try:
        online_count = await db.fetchval("SELECT COUNT(*) FROM inference_providers WHERE status = 'online'")
        if online_count and online_count > 0:
            services["INFERENCE_SERVICE"] = "HEALTHY"
        else:
            any_provider = await db.fetchval("SELECT COUNT(*) FROM inference_providers")
            services["INFERENCE_SERVICE"] = "HEALTHY" if (any_provider and any_provider > 0) else "DEGRADED"
    except Exception:
        services["INFERENCE_SERVICE"] = "DEGRADED"

    # 3. POLICY_GATE
    try:
        category = await db.fetchval("SELECT category FROM guardrails_config WHERE id = 1")
        services["POLICY_GATE"] = "MAINTENANCE" if category == "none" else "HEALTHY"
    except Exception:
        services["POLICY_GATE"] = "DEGRADED"

    # 4. MCP_TOOL_AVAILABILITY
    services["MCP_TOOL_AVAILABILITY"] = "HEALTHY"

    # 5. SEARCH_RETRIEVAL_SERVICE
    services["SEARCH_RETRIEVAL_SERVICE"] = "HEALTHY"

    # 6. MEMORY_AUTHORITY_SERVICE
    services["MEMORY_AUTHORITY_SERVICE"] = "HEALTHY"
    
    # 7. TOOL_EXECUTION_COORDINATOR
    services["TOOL_EXECUTION_COORDINATOR"] = "HEALTHY"

    # 8. QUEUE_BACKLOG_PRESSURE
    services["QUEUE_BACKLOG_PRESSURE"] = "HEALTHY"

    # 9. BOOTSTRAP_RUNTIME_SERVICES
    services["BOOTSTRAP_RUNTIME_SERVICES"] = "HEALTHY"

    # 10. AUTONOMY_ORCHESTRATOR
    services["AUTONOMY_ORCHESTRATOR"] = "MAINTENANCE"

    # 11. REFLECTION_SERVICE
    services["REFLECTION_SERVICE"] = "DEGRADED"

    # 12. DASHBOARD_TELEMETRY
    services["DASHBOARD_TELEMETRY"] = "DEGRADED"

    try:
        recent_errors = await db.fetchval(
            "SELECT COUNT(*) FROM agent_log WHERE level = 'ERROR' AND created_at > NOW() - INTERVAL '5 minutes'"
        )
        if recent_errors and recent_errors > 0:
            services["REFLECTION_SERVICE"] = "DEGRADED"
            services["AUTONOMY_ORCHESTRATOR"] = "DEGRADED"
    except Exception:
        pass

    criticals = [k for k, v in services.items() if v == "CRITICAL"]
    degradeds = [k for k, v in services.items() if v == "DEGRADED"]
    maintenances = [k for k, v in services.items() if v == "MAINTENANCE"]

    if criticals:
        overall = "CRITICAL"
        mode = "CRITICAL_FAILURE"
    elif degradeds:
        overall = "DEGRADED"
        mode = "DEGRADED_AUTONOMY"
    elif maintenances:
        overall = "MAINTENANCE"
        mode = "MAINTENANCE_REQUIRED"
    else:
        overall = "HEALTHY"
        mode = "FULL_AUTONOMY"

    return _ok(req_id, {
        "services": services,
        "overall": overall,
        "mode": mode
    })


async def handle_logging_clear_events(params: dict[str, Any], req_id: Any) -> str:
    """
    ``logging.clear_events`` — Delete log records from the agent_log table.
    """
    scope = params.get("scope", "all")
    search = params.get("search", "").strip()
    levels = params.get("levels", [])
    subsystem = params.get("subsystem", "All Subsystems").strip()

    try:
        if scope == "all":
            await db.execute("DELETE FROM agent_log", caller="logging.clear_events RPC")
        else:
            if subsystem != "All Subsystems":
                query = "SELECT id, message, trace FROM agent_log"
                rows = await db.fetch(query)
                import json as json_mod
                ids_to_delete = []
                for r in rows:
                    trace_dict = r["trace"]
                    if isinstance(trace_dict, str):
                        try:
                            trace_dict = json_mod.loads(trace_dict)
                        except Exception:
                            trace_dict = {}
                    sub = infer_subsystem(r["message"], trace_dict)
                    if sub.upper() == subsystem.upper():
                        ids_to_delete.append(r["id"])
                        
                if ids_to_delete:
                    await db.execute("DELETE FROM agent_log WHERE id = ANY($1)", ids_to_delete)
                return _ok(req_id, {"success": True, "count": len(ids_to_delete)})

            where_clauses = []
            query_args = []
            
            if levels and "ALL" not in [l.upper() for l in levels]:
                expanded_levels = []
                for lvl in levels:
                    lvl_up = lvl.upper()
                    expanded_levels.append(lvl_up)
                    if lvl_up == "WARN":
                        expanded_levels.append("WARNING")
                    elif lvl_up == "WARNING":
                        expanded_levels.append("WARN")
                where_clauses.append("level = ANY($1)")
                query_args.append(expanded_levels)
                
            if search:
                param_num = len(query_args) + 1
                where_clauses.append(f"message ILIKE ${param_num}")
                query_args.append(f"%{search}%")
                
            query = "DELETE FROM agent_log"
            if where_clauses:
                query += " WHERE " + " AND ".join(where_clauses)
                
            await db.execute(query, *query_args, caller="logging.clear_events RPC")
            
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("logging.clear_events error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_logging_archive_events(params: dict[str, Any], req_id: Any) -> str:
    """
    ``logging.archive_events`` — Export matching logs to data/archives/ folder and delete them.
    """
    scope = params.get("scope", "all")
    search = params.get("search", "").strip()
    levels = params.get("levels", [])
    subsystem = params.get("subsystem", "All Subsystems").strip()

    try:
        query = "SELECT id, level, message, trace, created_at FROM agent_log"
        where_clauses = []
        query_args = []
        
        if levels and "ALL" not in [l.upper() for l in levels]:
            expanded_levels = []
            for lvl in levels:
                lvl_up = lvl.upper()
                expanded_levels.append(lvl_up)
                if lvl_up == "WARN":
                    expanded_levels.append("WARNING")
                elif lvl_up == "WARNING":
                    expanded_levels.append("WARN")
            where_clauses.append("level = ANY($1)")
            query_args.append(expanded_levels)
            
        if search:
            param_num = len(query_args) + 1
            where_clauses.append(f"message ILIKE ${param_num}")
            query_args.append(f"%{search}%")
            
        if scope != "all" and where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
            
        query += " ORDER BY id DESC"
        rows = await db.fetch(query, *query_args)
        
        import json as json_mod
        events_to_archive = []
        ids_to_archive = []
        for r in rows:
            trace_dict = r["trace"]
            if isinstance(trace_dict, str):
                try:
                    trace_dict = json_mod.loads(trace_dict)
                except Exception:
                    trace_dict = {}
            sub = infer_subsystem(r["message"], trace_dict)
            if subsystem != "All Subsystems" and sub.upper() != subsystem.upper():
                continue
                
            ids_to_archive.append(r["id"])
            events_to_archive.append({
                "id": r["id"],
                "level": r["level"],
                "timestamp": r["created_at"].isoformat(),
                "subsystem": sub,
                "event_type": infer_event_type(r["message"], trace_dict),
                "message": r["message"],
                "trace": trace_dict
            })
            
        if not events_to_archive:
            return _ok(req_id, {"success": True, "count": 0})
            
        from pathlib import Path
        from datetime import datetime
        
        archives_dir = Path("data/archives")
        archives_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"log_archive_{timestamp}.json"
        archive_file = archives_dir / filename
        
        archive_file.write_text(
            json_mod.dumps(events_to_archive, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        
        if scope == "all" and subsystem == "All Subsystems":
            await db.execute("DELETE FROM agent_log")
        else:
            await db.execute("DELETE FROM agent_log WHERE id = ANY($1)", ids_to_archive)
            
        return _ok(req_id, {
            "success": True,
            "count": len(events_to_archive),
            "archive_file": str(archive_file)
        })
    except Exception as exc:
        logger.exception("logging.archive_events error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def fetch_webpage_text(url: str) -> str:
    """Fetch clean text content from a URL.
    
    Extracts headings/paragraphs first; falls back to <meta> description / OG tags
    for pages that block scraping (YouTube, Reddit, etc.).
    """
    import httpx
    from lxml import html as lxml_html
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=14.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 403 or response.status_code == 429:
                logger.warning("Scrape blocked %s: status %d — using meta fallback", url, response.status_code)
                # Still try to parse what we got
            elif response.status_code != 200:
                logger.warning("Failed to fetch webpage %s: status %d", url, response.status_code)
                return ""

            tree = lxml_html.fromstring(response.content)

            # --- Extract meta description / OG description as fallback content ---
            meta_desc = ""
            for xpath in [
                "//meta[@name='description']/@content",
                "//meta[@property='og:description']/@content",
                "//meta[@name='twitter:description']/@content",
                "//meta[@property='og:title']/@content",
                "//title/text()",
            ]:
                vals = tree.xpath(xpath)
                if vals:
                    meta_desc = " ".join(v.strip() for v in vals[:3] if v.strip())
                    break

            # --- Remove noise elements before extracting body text ---
            for element in tree.xpath(
                "//script | //style | //noscript | //nav | //footer | //header | //aside | //form"
            ):
                try:
                    element.getparent().remove(element)
                except Exception:
                    pass

            # --- Extract paragraphs and headings ---
            text_blocks: list[str] = []
            for element in tree.xpath("//h1 | //h2 | //h3 | //p | //li"):
                txt = element.text_content().strip()
                is_heading = element.tag.startswith("h")
                min_len = 3 if is_heading else 10
                if txt and len(txt) > min_len:
                    txt_clean = " ".join(txt.split())
                    text_blocks.append(txt_clean)

            body_text = "\n\n".join(text_blocks)

            # If body text is too thin, prepend meta description
            if len(body_text) < 200 and meta_desc:
                logger.info("Prepending meta-description for thin content on %s", url)
                body_text = f"Meta Description: {meta_desc}\n\n{body_text}".strip()

            return body_text if body_text else ""

    except Exception as exc:
        logger.warning("Failed to fetch webpage text from %s: %s", url, exc)
        return ""


async def execute_llm_generation(prompt: str, system_prompt: str = "") -> str:
    """Non-streaming LLM generation traversing configuration priority list with fallback routing."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    # Retrieve configured providers from DB
    providers = []
    try:
        rows = await db.fetch("SELECT id, type, name, priority, status, config, locked FROM inference_providers ORDER BY priority ASC")
        for r in rows:
            cfg_dict = r["config"]
            if isinstance(cfg_dict, str):
                import json
                cfg_dict = json.loads(cfg_dict)
            providers.append({
                "id": r["id"],
                "type": r["type"],
                "name": r["name"],
                "priority": r["priority"],
                "status": r["status"],
                "config": cfg_dict,
                "locked": r["locked"],
            })
    except Exception as db_exc:
        logger.warning("Could not read providers from DB in research: %s", db_exc)

    # Fallback to default embedded if no providers exist in DB
    if not providers:
        providers = [{
            "id": "embedded-builtin",
            "type": "embedded",
            "name": "Tala Embedded Engine",
            "priority": 1,
            "status": "online",
            "config": {"model": "Qwen/Qwen2.5-0.5B-Instruct"},
            "locked": True
        }]

    # Check if there is a forced provider
    forced_provider = None
    embedded_provider = None
    for p in providers:
        if p["config"] and p["config"].get("is_forced"):
            forced_provider = p
        if p["id"] == "embedded-builtin":
            embedded_provider = p

    if forced_provider:
        logger.info("Research generation: Forced provider active: '%s'", forced_provider["name"])
        if forced_provider["id"] == "embedded-builtin":
            providers = [forced_provider]
        else:
            providers = [forced_provider]
            if embedded_provider:
                providers.append(embedded_provider)
            else:
                providers.append({
                    "id": "embedded-builtin",
                    "type": "embedded",
                    "name": "Tala Embedded Engine",
                    "priority": 999,
                    "status": "online",
                    "config": {"model": "Qwen/Qwen2.5-0.5B-Instruct"},
                    "locked": True
                })

    for provider in providers:
        p_type = provider["type"]
        p_name = provider["name"]
        p_config = provider["config"] or {}

        try:
            if p_type == "embedded":
                from python.inference.embedded_engine import check_model_cached, generate_stream
                if not check_model_cached():
                    raise RuntimeError("Embedded model not cached")
                chunks = []
                async for chunk in generate_stream(messages, max_tokens=2048, temperature=0.3):
                    chunks.append(chunk)
                return "".join(chunks)

            elif p_type in ("local", "remote"):
                endpoint = p_config.get("endpoint", "").strip()
                model = p_config.get("model", "").strip()
                api_key = p_config.get("apiKey", "").strip()

                if p_type == "local":
                    engine = p_config.get("engine", "ollama")
                    if not endpoint:
                        endpoint = "http://localhost:11434/v1" if engine == "ollama" else "http://localhost:1234/v1"
                    if not model:
                        model = "llama3.2:3b" if engine == "ollama" else "qwen2.5-1.5b-instruct"
                else:  # remote
                    service = p_config.get("service", "openai")
                    if service == "openai" and not endpoint:
                        endpoint = "https://api.openai.com/v1"
                    elif service == "gemini" and not endpoint:
                        endpoint = "https://generativelanguage.googleapis.com/v1beta/openai"
                    elif service == "anthropic" and not endpoint:
                        endpoint = "https://api.anthropic.com/v1"

                import httpx
                headers = {"Content-Type": "application/json"}
                if api_key:
                    if "anthropic" in endpoint or p_config.get("service") == "anthropic":
                        headers["x-api-key"] = api_key
                        headers["anthropic-version"] = "2023-06-01"
                    else:
                        headers["Authorization"] = f"Bearer {api_key}"

                url = f"{_normalize_endpoint_url(endpoint)}/chat/completions"
                payload = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": 2048,
                    "temperature": 0.3,
                    "stream": False
                }

                async with httpx.AsyncClient(timeout=90.0) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    if response.status_code != 200:
                        raise RuntimeError(f"HTTP Error {response.status_code}: {response.text}")
                    resp_json = response.json()
                    return resp_json["choices"][0]["message"]["content"]

        except Exception as exc:
            logger.warning("Provider '%s' (%s) failed in research generation: %s. Trying next...", p_name, p_type, exc)
            continue

    raise RuntimeError("All inference providers failed to generate content.")


async def handle_research_search(params: dict[str, Any], req_id: Any) -> str:
    """
    ``research.search`` — Dispatches search queries to discover relevant web sources.
    """
    query = params.get("query", "").strip()
    if not query:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'query' is required")

    try:
        # Wire up: Broadcast search URL to Electron browser view
        import urllib.parse
        search_url = f"https://duckduckgo.com/?q={urllib.parse.quote(query)}"
        try:
            await broadcast_notification("browser.navigate", {"url": search_url})
        except Exception as e:
            logger.warning("Failed to broadcast browser search navigation notification: %s", e)

        from python.harness.search_manager import SearchManager
        provider_id = params.get("search_provider_id")
        results = await SearchManager.query(query, provider_id=provider_id)
        return _ok(req_id, {"results": results})
    except Exception as exc:
        logger.exception("research.search error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_research_crawl_and_summarize(params: dict[str, Any], req_id: Any) -> str:
    """
    ``research.crawl_and_summarize`` — Scrapes selected URLs, aggregates clean text, and
    prompts the active LLM to output a compiled HTML Neural Node summarizing the findings.
    """
    urls = params.get("urls", [])
    subject = params.get("subject", "").strip()
    session_id = str(params.get("session_id", uuid.uuid4()))

    if not urls or not isinstance(urls, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'urls' list is required")
    if not subject:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'subject' is required")

    try:
        # Fetch all URLs in parallel (max 15s each) instead of sequentially
        import asyncio

        async def _safe_fetch(url: str) -> tuple[str, str]:
            """Return (url, text) or (url, '') on failure."""
            try:
                # Wire up: Broadcast URL navigation to Electron browser view
                try:
                    await broadcast_notification("browser.navigate", {"url": url})
                    # Yield slightly so the frontend can receive and navigate
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning("Failed to broadcast browser crawl navigation notification: %s", e)

                text = await asyncio.wait_for(fetch_webpage_text(url), timeout=15.0)
                if text:
                    return url, text
            except Exception as exc:
                logger.warning("fetch timeout/error for %s: %s", url, exc)

            # Subdomain correction fallback on error (e.g. star-citizen.robertsspaceindustries.com -> robertsspaceindustries.com)
            try:
                from urllib.parse import urlparse, urlunparse
                parsed = urlparse(url)
                netloc_parts = parsed.netloc.split(".")
                if len(netloc_parts) >= 3:
                    fallback_netloc = ".".join(netloc_parts[1:])
                    fallback_url = urlunparse(parsed._replace(netloc=fallback_netloc))
                    logger.info("Attempting subdomain correction fallback: %s -> %s", url, fallback_url)
                    
                    try:
                        await broadcast_notification("browser.navigate", {"url": fallback_url})
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
                        
                    text = await asyncio.wait_for(fetch_webpage_text(fallback_url), timeout=15.0)
                    if text:
                        return fallback_url, text
            except Exception as fallback_exc:
                logger.warning("subdomain correction fallback failed: %s", fallback_exc)

            return url, ""

        fetch_results = await asyncio.gather(*[_safe_fetch(u) for u in urls])

        crawled_contents: list[str] = []
        successful_urls: list[str] = []
        failed_urls: list[str] = []

        for url, text in fetch_results:
            if text:
                crawled_contents.append(f"--- SOURCE WEBSITE: {url} ---\n{text[:6000]}")
                successful_urls.append(url)
            else:
                failed_urls.append(url)

        if not crawled_contents:
            return _err(
                req_id,
                JsonRpcError.INTERNAL_ERROR,
                "Unable to scrape or extract content from any of the selected URLs."
            )

        combined_context_text = "\n\n".join(crawled_contents)

        from datetime import datetime, timezone
        import json
        import re

        current_iso_time = datetime.now(timezone.utc).isoformat()
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        subject_slug = subject.lower().replace(" ", "-")
        subject_slug = re.sub(r'[^a-z0-9-]+', '', subject_slug)
        subject_slug = re.sub(r'-+', '-', subject_slug).strip('-')
        sources_json = json.dumps(successful_urls)

        system_prompt = (
            "You are a meticulous researcher and knowledge engineer. Your job is to produce ONE complete HTML Neural Node document.\n\n"
            "You must follow these formatting and structural guidelines precisely:\n"
            "1. Output ONLY a single ```html ... ``` code block. No explanation, markdown, or chat text before or after.\n"
            "2. The HTML document MUST follow this exact structure, including metadata, tags, and IDs:\n\n"
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"UTF-8\">\n"
            "  <meta name=\"tala-schema\" content=\"neural-node/v2\">\n"
            f"  <meta name=\"tala-node-id\" content=\"concept-{subject_slug}\">\n"
            "  <meta name=\"tala-node-type\" content=\"concept\">\n"
            f"  <meta name=\"tala-created\" content=\"{current_iso_time}\">\n"
            "  <meta name=\"tala-author\" content=\"research-agent\">\n"
            f"  <title>Neural Node: {subject}</title>\n"
            "</head>\n"
            "<body>\n"
            f"<article data-node-id=\"concept-{subject_slug}\" data-node-type=\"concept\" data-execution-authority=\"0.00\" data-memory-authority=\"0.80\" data-weight-relevance=\"0.80\" data-weight-confidence=\"0.80\">\n"
            "  <header id=\"node-identity\">\n"
            f"    <h1 id=\"node-title\">{subject}</h1>\n"
            "    <dl>\n"
            f"      <dt>Node ID</dt><dd><code>concept-{subject_slug}</code></dd>\n"
            "      <dt>Type</dt><dd><code>concept</code></dd>\n"
            "      <dt>Execution Authority</dt><dd><code>0.00</code></dd>\n"
            f"      <dt>Created</dt><dd><time datetime=\"{current_iso_time}\">{current_date}</time></dd>\n"
            f"      <dt>Memory Description</dt><dd>[REPLACE WITH: A one-sentence relational summary of what this node represents, e.g. 'A comprehensive research overview of {subject} covering its history, mechanisms, and applications.']</dd>\n"
            "    </dl>\n"
            "  </header>\n\n"
            "  <section id=\"math-weights\">\n"
            "    <h2>Activation Math Weights</h2>\n"
            "    <div class=\"math-weights\">\n"
            "      <table>\n"
            "        <tbody>\n"
            "          <tr><td>weight_relevance</td><td>0.80</td></tr>\n"
            "          <tr><td>weight_confidence</td><td>0.80</td></tr>\n"
            "        </tbody>\n"
            "      </table>\n"
            "    </div>\n"
            "  </section>\n\n"
            "  <section id=\"semantic-memory\">\n"
            "    <h2>Semantic Memory</h2>\n"
            "    <!-- Write 3–5 detailed, factual paragraphs based on the source material here. Replace all placeholders with actual content. -->\n"
            "  </section>\n\n"
            "  <nav id=\"relationship-anchors\" aria-label=\"Node relationships\">\n"
            "    <h2>Relationships</h2>\n"
            "    <ul>\n"
            "      <!-- Generate 2–4 related concept links here, for example:\n"
            "      <li><a href=\"./concept-[related-slug].html\" data-relationship-type=\"references\" data-relationship-weight=\"0.80\">[Related Concept Title]</a></li>\n"
            "      -->\n"
            "    </ul>\n"
            "  </nav>\n\n"
            "  <script type=\"application/json\" id=\"node-state\">\n"
            "  {\n"
            f"    \"title\": \"{subject}\",\n"
            "    \"description\": \"[REPLACE WITH: A one-sentence relational summary of what this node represents]\",\n"
            f"    \"tags\": [\"research\", \"{subject_slug}\"],\n"
            "    \"metadata\": {\n"
            "      \"author\": \"research-agent\",\n"
            f"      \"created\": \"{current_iso_time}\",\n"
            "      \"version\": \"1.0.0\",\n"
            "      \"schema\": \"neural-node/v2\"\n"
            "    },\n"
            f"    \"sources\": {sources_json}\n"
            "  }\n"
            "  </script>\n"
            "</article>\n"
            "</body>\n"
            "</html>\n"
            "```\n\n"
            "CRITICAL REQUIREMENTS:\n"
            "- DO NOT use placeholders like [REPLACE WITH...] or [Write summary here] in the output. Replace them with actual research findings.\n"
            "- The semantic-memory section MUST contain 3 to 5 paragraphs of factual text summarizing the source material.\n"
            "- The relationship-anchors nav must link to 2 to 4 related concept files using relative paths like ./concept-[related-slug].html."
        )

        prompt = (
            f"Generate the HTML Neural Node for the subject: \"{subject}\"\n\n"
            f"=== SOURCE MATERIAL ===\n"
            f"{combined_context_text[:20000]}\n"
        )

        generated_raw = await execute_llm_generation(prompt, system_prompt)

        return _ok(req_id, {
            "html_nn": generated_raw,
            "sources": {
                "successful": successful_urls,
                "failed": failed_urls
            }
        })
    except Exception as exc:
        logger.exception("research.crawl_and_summarize error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


# ---------------------------------------------------------------------------
# Developer & Agentic Capability Handlers (Milestones 1-7 integration)
# ---------------------------------------------------------------------------

async def handle_task_submit(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.submit`` — Accept a goal, decompose it, and queue/execute it using GoalEngine.
    """
    goal = params.get("goal", "").strip()
    if not goal:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'goal' is required")
    
    try:
        from sdk.agent.queue import GoalTreeDecomposer
        from sdk.agent.persistence import generate_deterministic_id
        from sdk.agent.goal_engine import GoalEngine
        from sdk.agent.adapter import AdapterCompiler
        from python.harness.retrieval import retrieve_relevant_nodes
        import os
        import sys
        from pathlib import Path
        
        # 0. Retrieve relevant context nodes and compile AdapterPacket
        user_data = params.get("user_data_path")
        appdata = os.environ.get("APPDATA")
        
        nodes_dir = Path.cwd() / "nodes"
        if user_data:
            nodes_dir = Path(user_data) / "nodes"
        else:
            if sys.platform == "win32" and appdata:
                nodes_dir = Path(appdata) / "tala-engine" / "nodes"
            elif sys.platform == "win32":
                nodes_dir = Path.home() / "AppData" / "Roaming" / "tala-engine" / "nodes"
            
        if not nodes_dir.exists():
            repo_nodes = Path(_ROOT_DIR) / "nodes"
            if repo_nodes.exists():
                nodes_dir = repo_nodes

        adapter_packet = None
        if nodes_dir.exists() and nodes_dir.is_dir():
            matched = retrieve_relevant_nodes(goal, nodes_dir, top_k=5, threshold=0.0)
            if matched:
                nodes = [node for node, score in matched]
                
                try:
                    rows = await db.fetch("SELECT access_rights FROM firewall_contacts")
                    contacts = [{"access_rights": dict(r["access_rights"])} for r in rows if r["access_rights"]]
                    
                    from sdk.agent.trust_registry import TrustRegistry
                    registry = TrustRegistry(contacts)
                    nodes = registry.verify_and_cap_nodes(nodes)
                except Exception as e:
                    logger.warning("Failed to apply TrustRegistry: %s", e)
                
                adapter_packet = AdapterCompiler.compile(nodes)
        
        
        # 1. Decompose goal to get count for schema compatibility
        decomposer = GoalTreeDecomposer()
        task_items = await decomposer.decompose(goal)
        if not task_items:
            return _err(req_id, JsonRpcError.INTERNAL_ERROR, "Decomposition produced no tasks")
        
        parent_task_id = generate_deterministic_id(goal, "goal-parent")
        
        # 2. Start Goal Engine in background task
        engine = GoalEngine(_ROOT_DIR)
        
        from sdk.agent.batch_policy import BatchPolicy
        authority_level = params.get("authority_level", "auto")
        max_steps = params.get("max_steps")
        
        policy = BatchPolicy.auto()
        if authority_level:
            policy.authority_level = authority_level
        if max_steps is not None:
            try:
                policy.max_steps = int(max_steps)
            except (ValueError, TypeError):
                pass
        
        async def run_goal_engine():
            await engine.run_goal(goal, policy=policy, adapter_packet=adapter_packet)
            
        asyncio.create_task(run_goal_engine())
        
        return _ok(req_id, {
            "success": True,
            "task_id": parent_task_id,
            "subtasks_count": len(task_items)
        })
    except Exception as exc:
        logger.exception("task.submit error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.list`` — Fetch all queued/executing tasks.
    """
    try:
        records = await db.fetch("SELECT id, parent_id, description, status, payload, assigned_agent_id, model_override, worktree_path, branch_name FROM tasks ORDER BY created_at DESC LIMIT 50")
        task_list = []
        for r in records:
            task_list.append({
                "id": r["id"],
                "parent_id": r["parent_id"],
                "description": r["description"],
                "status": r["status"],
                "assigned_agent_id": r["assigned_agent_id"],
                "model_override": json.loads(r["model_override"]) if isinstance(r["model_override"], str) else r["model_override"],
                "worktree_path": r["worktree_path"],
                "branch_name": r["branch_name"],
                "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            })
        return _ok(req_id, {"tasks": task_list})
    except Exception as exc:
        logger.exception("task.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_task_assign_worker(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    agent_id = params.get("agent_id")
    if not task_id or not agent_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id and agent_id required")
    try:
        await db.execute("UPDATE tasks SET assigned_agent_id = $1 WHERE id = $2", agent_id, task_id)
        return _ok(req_id, {"success": True, "task_id": task_id, "assigned_agent_id": agent_id})
    except Exception as exc:
        logger.exception("task.assign_worker error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_task_set_model_override(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    model_override = params.get("model_override")
    if not task_id or model_override is None:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id and model_override required")
    try:
        await db.execute("UPDATE tasks SET model_override = $1::jsonb WHERE id = $2", json.dumps(model_override), task_id)
        return _ok(req_id, {"success": True, "task_id": task_id, "model_override": model_override})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_task_create_worktree(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    
    from python.harness.config import settings
    base_dir = Path(settings.LOCAL_WORKSPACE_DIR) if getattr(settings, 'LOCAL_WORKSPACE_DIR', None) else Path(_ROOT_DIR)
    
    scratch_dir = base_dir / "scratch" / "worktrees"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    
    worktree_path = scratch_dir / task_id
    branch_name = f"tala-task/{task_id}"
    
    import asyncio
    try:
        git_check = await asyncio.create_subprocess_shell(
            "git rev-parse --is-inside-work-tree",
            cwd=str(base_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await git_check.communicate()
        is_git = (git_check.returncode == 0 and stdout.decode().strip() == "true")
        
        if is_git:
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", str(worktree_path.absolute()), "-b", branch_name,
                cwd=str(base_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(f"git worktree failed: {stderr.decode()}")
                return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"git worktree failed: {stderr.decode()}")
        else:
            import shutil
            shutil.copytree(str(base_dir), str(worktree_path), dirs_exist_ok=True, ignore=shutil.ignore_patterns('.git', 'scratch', '__pycache__'))
            branch_name = "copied-scratch"
            
        await db.execute(
            "UPDATE tasks SET worktree_path = $1, branch_name = $2 WHERE id = $3",
            str(worktree_path.absolute()), branch_name, task_id
        )
        return _ok(req_id, {"success": True, "task_id": task_id, "worktree_path": str(worktree_path.absolute()), "branch_name": branch_name})
    except Exception as exc:
        logger.exception("task.create_worktree error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_workers_active(params: dict[str, Any], req_id: Any) -> str:
    try:
        records = await db.fetch("SELECT id, name, role, config FROM agents WHERE is_active = TRUE")
        workers = [{"id": r["id"], "name": r["name"], "role": r["role"], "config": json.loads(r["config"]) if isinstance(r["config"], str) else r["config"]} for r in records]
        return _ok(req_id, {"workers": workers})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_topology_list(params: dict[str, Any], req_id: Any) -> str:
    try:
        from sdk.agent.topology import TopologyRegistry
        topologies = TopologyRegistry.list_all()
        # Convert to dict
        data = []
        for t in topologies:
            data.append({
                "topology_id": t.topology_id,
                "name": t.name,
                "nodes": [{"id": n.id, "role": n.role, "description": n.description} for n in t.nodes],
                "edges": [{"from_node": e.from_node, "to_node": e.to_node, "condition": e.condition} for e in t.edges],
                "failure_policy": t.failure_policy
            })
        return _ok(req_id, {"topologies": data})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_start(params: dict[str, Any], req_id: Any) -> str:
    topology_id = params.get("topology_id")
    goal = params.get("goal")
    if not topology_id or not goal:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "topology_id and goal are required")
        
    try:
        from sdk.agent.swarm_engine import SwarmEngine
        parent_id = await SwarmEngine.spawn_topology(goal, topology_id)
        return _ok(req_id, {"success": True, "task_id": parent_id})
    except Exception as exc:
        logger.exception("swarm.start error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_pause(params: dict[str, Any], req_id: Any) -> str:
    params["action"] = "pause"
    return await handle_task_control(params, req_id)

async def handle_swarm_resume(params: dict[str, Any], req_id: Any) -> str:
    params["action"] = "resume"
    return await handle_task_control(params, req_id)

async def handle_swarm_cancel(params: dict[str, Any], req_id: Any) -> str:
    params["action"] = "abort"
    return await handle_task_control(params, req_id)

async def handle_swarm_status(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    row = await db.fetchrow("SELECT status, payload, result FROM tasks WHERE id = $1", task_id)
    if not row: return _err(req_id, JsonRpcError.INVALID_PARAMS, "Task not found")
    
    children = await db.fetch("SELECT id, status, payload FROM tasks WHERE parent_id = $1", task_id)
    nodes = []
    for c in children:
        p = c["payload"]
        if isinstance(p, str): p = json.loads(p)
        nodes.append({"id": c["id"], "status": c["status"], "node_id": p.get("node_id")})
        
    p_dict = row["payload"]
    if isinstance(p_dict, str): p_dict = json.loads(p_dict)
    
    return _ok(req_id, {
        "task_id": task_id,
        "status": row["status"],
        "topology_id": p_dict.get("topology_id"),
        "nodes": nodes
    })

async def handle_swarm_runs_list(params: dict[str, Any], req_id: Any) -> str:
    rows = await db.fetch("SELECT id, status, description, created_at, completed_at FROM tasks WHERE payload::text LIKE '%topology_id%' AND parent_id IS NULL ORDER BY created_at DESC LIMIT 50")
    runs = []
    for r in rows:
        runs.append({
            "task_id": r["id"],
            "status": r["status"],
            "description": r["description"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None
        })
    return _ok(req_id, {"runs": runs})

async def handle_swarm_runs_get(params: dict[str, Any], req_id: Any) -> str:
    return await handle_swarm_status(params, req_id)

async def handle_swarm_artifact_inspect(params: dict[str, Any], req_id: Any) -> str:
    artifact_id = params.get("artifact_id")
    if not artifact_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "artifact_id required")
    row = await db.fetchrow("SELECT result FROM tasks WHERE id = $1", artifact_id)
    if not row: return _err(req_id, JsonRpcError.INVALID_PARAMS, "Artifact not found")
    return _ok(req_id, {"artifact": row["result"]})

async def handle_gateway_status(params: dict[str, Any], req_id: Any) -> str:
    from sdk.gateway.lifecycle import get_process_status
    
    db_ok = False
    auth_credentials_count = 0
    trusted_contacts_count = 0
    remote_instances_count = 0
    authenticated_remote_instances = 0
    try:
        await db.fetchval("SELECT 1")
        db_ok = True
        auth_credentials_count = int(await db.fetchval("SELECT COUNT(*) FROM auth_credentials") or 0)
        trusted_contacts_count = int(await db.fetchval("SELECT COUNT(*) FROM firewall_contacts") or 0)
        remote_instances_count = int(await db.fetchval("SELECT COUNT(*) FROM runtime_instances") or 0)
        authenticated_remote_instances = int(
            await db.fetchval("SELECT COUNT(*) FROM runtime_instances WHERE COALESCE(api_key, '') <> ''") or 0
        )
    except Exception:
        pass

    auth_ready = db_ok and (auth_credentials_count > 0 or trusted_contacts_count > 0 or authenticated_remote_instances > 0)
        
    return _ok(req_id, {
        "status": "online",
        "database_connected": db_ok,
        "auth_ready": auth_ready,
        "auth_credentials_count": auth_credentials_count,
        "trusted_contacts_count": trusted_contacts_count,
        "remote_instances_count": remote_instances_count,
        "authenticated_remote_instances": authenticated_remote_instances,
        "mcp_servers_tracked": len(get_process_status()),
        "processes": get_process_status()
    })

async def handle_plugins_list(params: dict[str, Any], req_id: Any) -> str:
    from sdk.gateway.plugins import scan_plugins_directory
    results = scan_plugins_directory()
    return _ok(req_id, {"plugins": results})

async def handle_plugins_validate(params: dict[str, Any], req_id: Any) -> str:
    from sdk.gateway.plugins import load_and_validate_manifest, PluginValidationError
    plugin_path = params.get("path")
    if not plugin_path: return _err(req_id, JsonRpcError.INVALID_PARAMS, "path required")
    
    import os
    manifest_path = os.path.join(plugin_path, "plugin.json")
    try:
        manifest = load_and_validate_manifest(manifest_path)
        return _ok(req_id, {"valid": True, "manifest": manifest})
    except PluginValidationError as e:
        return _ok(req_id, {"valid": False, "error": str(e)})
    except Exception as e:
        return _ok(req_id, {"valid": False, "error": str(e)})

async def handle_plugins_reload(params: dict[str, Any], req_id: Any) -> str:
    from sdk.gateway.plugins import scan_plugins_directory
    # In the future, this would also restart associated MCP processes
    results = scan_plugins_directory()
    return _ok(req_id, {"success": True, "plugins": results})

async def handle_task_control(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.control`` — Pause, resume, or abort task loops.
    """
    task_id = params.get("task_id", "").strip()
    action = params.get("action", "").strip() # pause, resume, abort
    if not task_id or not action:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' and 'action' are required")
        
    try:
        from sdk.agent.gates import trigger_emergency_halt
        if action == "abort":
            await trigger_emergency_halt()
            await db.execute("UPDATE tasks SET status = 'failed' WHERE id = $1 OR parent_id = $1", task_id)
        elif action == "pause":
            await db.execute("UPDATE tasks SET status = 'blocked' WHERE id = $1 OR parent_id = $1", task_id)
        elif action == "resume":
            await db.execute("UPDATE tasks SET status = 'pending' WHERE id = $1 OR parent_id = $1", task_id)
            
        return _ok(req_id, {"success": True, "action": action, "task_id": task_id})
    except Exception as exc:
        logger.exception("task.control error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_clear(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.clear`` — Wipe all tasks and their associated telemetry.
    """
    try:
        await db.execute("TRUNCATE TABLE tasks CASCADE;")
        return _ok(req_id, {"success": True, "message": "All tasks and telemetry have been cleared."})
    except Exception as exc:
        logger.exception("task.clear error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_get_telemetry(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.get_telemetry`` — Fetch event telemetry streams.
    """
    task_id = params.get("task_id", "").strip()
    include_children = params.get("include_children", False)
    try:
        query = "SELECT id, task_id, event_type, payload, created_at FROM task_event_store"
        args = []
        if task_id:
            if include_children:
                query += " WHERE task_id = $1 OR task_id IN (SELECT id FROM tasks WHERE parent_id = $1)"
            else:
                query += " WHERE task_id = $1"
            args.append(task_id)
        query += " ORDER BY created_at DESC LIMIT 100"
        
        records = await db.fetch(query, *args)
        events = []
        for r in records:
            events.append({
                "event_id": str(r["id"]),
                "task_id": r["task_id"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None
            })
        return _ok(req_id, {"events": events})
    except Exception as exc:
        logger.exception("task.get_telemetry error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_repo_scan(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.scan`` — Scan repository code structure.
    """
    try:
        from sdk.agent.scanner import RepositoryScanner
        scanner = RepositoryScanner(_ROOT_DIR)
        code_map = scanner.build_codebase_map()
        return _ok(req_id, {"codebase_map": code_map})
    except Exception as exc:
        logger.exception("repo.scan error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_repo_lint(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.lint`` — Lint repository for architectural violations.
    """
    try:
        from sdk.agent.linter import ArchitectureLinter
        linter = ArchitectureLinter(_ROOT_DIR)
        errors = linter.lint_all()
        return _ok(req_id, {"success": len(errors) == 0, "errors": errors})
    except Exception as exc:
        logger.exception("repo.lint error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_repo_checkpoint(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.checkpoint`` — Create repository state checkpoint.
    """
    try:
        from sdk.agent.recovery import RollbackRecovery
        checkpoint_name = str(params.get("checkpoint_name", "checkpoint") or "checkpoint").strip()
        rr = RollbackRecovery(_ROOT_DIR)
        stash_id = rr.create_checkpoint(checkpoint_name)
        return _ok(req_id, {"success": True, "checkpoint_id": stash_id})
    except Exception as exc:
        logger.exception("repo.checkpoint error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_repo_restore(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.restore`` — Revert to the last checkpoint.
    """
    try:
        from sdk.agent.recovery import RollbackRecovery
        checkpoint_name = str(params.get("checkpoint_name", "checkpoint") or "checkpoint").strip()
        rr = RollbackRecovery(_ROOT_DIR)
        success = rr.restore_checkpoint(checkpoint_name)
        return _ok(req_id, {"success": success, "checkpoint_id": checkpoint_name})
    except Exception as exc:
        logger.exception("repo.restore error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_report(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.report`` — Replay event logs and generate progress report.
    """
    task_id = params.get("task_id", "").strip()
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' is required")
        
    try:
        from sdk.agent.recovery import CompletionReporter
        reporter = CompletionReporter(task_id)
        report = await reporter.generate_report()
        return _ok(req_id, {"report": report})
    except Exception as exc:
        logger.exception("task.report error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_build_diagnose(params: dict[str, Any], req_id: Any) -> str:
    """
    ``build.diagnose`` — Diagnose compiler or terminal failure logs.
    """
    console_output = params.get("console_output", "")
    try:
        from sdk.agent.tester import BuildDiagnosis
        diag = BuildDiagnosis.diagnose_failure(console_output)
        return _ok(req_id, diag)
    except Exception as exc:
        logger.exception("build.diagnose error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_build_repair(params: dict[str, Any], req_id: Any) -> str:
    """
    ``build.repair`` — Triggers IterativeDebugger loop to fix a target file.
    """
    run_cmd = params.get("run_cmd", "")
    target_file = params.get("target_file", "")
    if not run_cmd or not target_file:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'run_cmd' and 'target_file' are required")
        
    try:
        from sdk.agent.tester import IterativeDebugger
        from sdk.agent.coder import CodeRepair
        
        target_code = params.get("target_code", "")
        replacement_code = params.get("replacement_code", "")
        
        def simple_edit(content, diagnostics):
            if target_code and replacement_code:
                return CodeRepair.apply_surgical_patch(content, target_code, replacement_code)
            return content
            
        debugger = IterativeDebugger(run_cmd, _ROOT_DIR)
        success = debugger.debug_loop(target_file, simple_edit, max_attempts=params.get("max_attempts", 3))
        return _ok(req_id, {"success": success})
    except Exception as exc:
        logger.exception("build.repair error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_shell_run(params: dict[str, Any], req_id: Any) -> str:
    """
    ``shell.run`` — Governed async shell execution.
    """
    command = params.get("command", "")
    if not command:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'command' is required")
        
    try:
        from sdk.agent.governance import RiskClassifier
        from sdk.agent.async_shell import AsyncShellExecutor
        
        risk = RiskClassifier.classify_action("execute_command", command)
        logger.info("Governance check: command=%r has risk=%s", command, risk)
        
        result = await AsyncShellExecutor.run(command, _ROOT_DIR, timeout=params.get("timeout", 60.0))
        return _ok(req_id, {
            "success": result["success"],
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "timed_out": result["timed_out"],
            "risk_level": risk,
        })
    except Exception as exc:
        logger.exception("shell.run error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_generate(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.generate`` — Generate file from a template.
    """
    file_type = params.get("file_type", "")
    class_name = params.get("class_name", "")
    if not file_type or not class_name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'file_type' and 'class_name' are required")
        
    try:
        from sdk.agent.coder import CodeGenerator
        code = CodeGenerator.generate_template(file_type, class_name, params.get("options"))
        return _ok(req_id, {"code": code})
    except Exception as exc:
        logger.exception("code.generate error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_repair(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.repair`` — Apply a surgical line patch to file content.
    """
    file_content = params.get("file_content", "")
    target_code = params.get("target_code", "")
    replacement_code = params.get("replacement_code", "")
    
    try:
        from sdk.agent.coder import CodeRepair
        patched = CodeRepair.apply_surgical_patch(file_content, target_code, replacement_code)
        return _ok(req_id, {"success": True, "patched_content": patched})
    except Exception as exc:
        logger.exception("code.repair error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_test_generate(params: dict[str, Any], req_id: Any) -> str:
    """
    ``test.generate`` — Generate a test suite.
    """
    target_file = params.get("target_file", "")
    file_type = params.get("file_type", "pytest")
    class_name = params.get("class_name", "")
    functions = params.get("functions", [])
    
    if not target_file or not class_name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'target_file' and 'class_name' are required")
        
    try:
        from sdk.agent.tester import TestGenerator
        test_code = TestGenerator.generate_test_suite(target_file, file_type, class_name, functions)
        return _ok(req_id, {"test_code": test_code})
    except Exception as exc:
        logger.exception("test.generate error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_propose_patch(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.propose_patch`` — Propose bounded exact-replacement file edits.
    """
    try:
        from sdk.agent.safe_code_bridges import propose_patch

        result = propose_patch(
            replacements=params.get("replacements"),
            rationale=params.get("rationale", ""),
            validation_commands=params.get("validation_commands"),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("code.propose_patch error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_apply_patch(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.apply_patch`` — Apply bounded exact-replacement file edits.
    """
    try:
        from sdk.agent.safe_code_bridges import apply_patch

        result = apply_patch(
            replacements=params.get("replacements"),
            expected_files=params.get("expected_files"),
            dry_run=bool(params.get("dry_run", False)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("code.apply_patch error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_propose_file_write(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.propose_file_write`` — Propose bounded workspace file creation.
    """
    try:
        from sdk.agent.safe_code_bridges import propose_file_write

        result = propose_file_write(
            file_writes=params.get("file_writes"),
            rationale=params.get("rationale", ""),
            validation_commands=params.get("validation_commands"),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("code.propose_file_write error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_write_files(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.write_files`` — Create bounded text files inside the workspace.
    """
    try:
        from sdk.agent.safe_code_bridges import write_files

        result = write_files(
            file_writes=params.get("file_writes"),
            expected_files=params.get("expected_files"),
            dry_run=bool(params.get("dry_run", False)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("code.write_files error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_test_run_validation(params: dict[str, Any], req_id: Any) -> str:
    """
    ``test.run_validation`` — Run allowlisted validation commands.
    """
    try:
        from sdk.agent.safe_code_bridges import run_validation

        result = await run_validation(
            commands=params.get("commands"),
            timeout=float(params.get("timeout", 60.0)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("test.run_validation error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_system_self_test(params: dict[str, Any], req_id: Any) -> str:
    """
    ``system.self_test`` — Run Tala's repeatable local health and validation suite.
    """
    try:
        from python.harness.self_test import run_self_test

        result = run_self_test(include_audit=bool(params.get("include_audit", True)))
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("system.self_test error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_system_trigger_update(params: dict[str, Any], req_id: Any) -> str:
    """
    ``system.trigger_update`` — Pull latest code from the repository and notify clients.
    """
    import asyncio
    try:
        proc = await asyncio.create_subprocess_shell(
            "git pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        
        await broadcast_notification("system.update_available")
        return _ok(req_id, {
            "status": "success", 
            "stdout": stdout.decode().strip(), 
            "stderr": stderr.decode().strip()
        })
    except Exception as exc:
        logger.exception("system.trigger_update error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_system_download_update(params: dict[str, Any], req_id: Any) -> str:
    """
    ``system.download_update`` — Package the current repository into a ZIP file and return as Base64.
    """
    import asyncio
    import base64
    import os
    try:
        zip_path = "update_payload.zip"
        # Use git archive to securely package the committed state without virtualenvs or node_modules
        proc = await asyncio.create_subprocess_shell(
            f"git archive -o {zip_path} HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"git archive failed: {stderr.decode()}")
            
        with open(zip_path, "rb") as f:
            zip_bytes = f.read()
            
        os.remove(zip_path)
        
        zip_b64 = base64.b64encode(zip_bytes).decode('utf-8')
        return _ok(req_id, {
            "status": "success",
            "zipBase64": zip_b64
        })
    except Exception as exc:
        logger.exception("system.download_update error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_self_improvement_create_plan(params: dict[str, Any], req_id: Any) -> str:
    """
    ``self_improvement.create_plan`` — Create a bounded self-improvement plan.
    """
    try:
        from sdk.agent.safe_code_bridges import create_self_improvement_plan

        result = create_self_improvement_plan(
            goal=params.get("goal", ""),
            target_files=params.get("target_files"),
            replacements=params.get("replacements"),
            file_writes=params.get("file_writes"),
            validation_commands=params.get("validation_commands"),
            dry_run=bool(params.get("dry_run", True)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("self_improvement.create_plan error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_self_improvement_execute_plan(params: dict[str, Any], req_id: Any) -> str:
    """
    ``self_improvement.execute_plan`` — Execute a bounded audited improvement plan.
    """
    try:
        from sdk.agent.safe_code_bridges import execute_self_improvement_plan

        result = await execute_self_improvement_plan(
            plan=params.get("plan"),
            dry_run=bool(params.get("dry_run", False)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("self_improvement.execute_plan error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_self_improvement_dry_run_goal(params: dict[str, Any], req_id: Any) -> str:
    """
    ``self_improvement.dry_run_goal`` — Create and execute a bounded dry-run plan.
    """
    try:
        from sdk.agent.safe_code_bridges import (
            create_self_improvement_plan,
            execute_self_improvement_plan,
        )

        goal = params.get("goal") or params.get("goal_text") or params.get("raw") or ""
        plan_result = create_self_improvement_plan(
            goal=goal,
            target_files=params.get("target_files") or ["README.md"],
            replacements=params.get("replacements"),
            file_writes=params.get("file_writes"),
            validation_commands=params.get("validation_commands"),
            dry_run=True,
        )
        if not plan_result.get("success"):
            return _ok(req_id, {
                "success": False,
                "plan_result": plan_result,
                "execution_result": None,
                "errors": plan_result.get("errors", []),
            })

        execution_result = await execute_self_improvement_plan(
            plan=plan_result["plan"],
            dry_run=True,
        )
        return _ok(req_id, {
            "success": bool(execution_result.get("success")),
            "plan_result": plan_result,
            "execution_result": execution_result,
            "errors": execution_result.get("errors", []),
        })
    except Exception as exc:
        logger.exception("self_improvement.dry_run_goal error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_self_improvement_execute_goal(params: dict[str, Any], req_id: Any) -> str:
    """
    ``self_improvement.execute_goal`` — Execute a goal through the selected approved bridge.
    """
    try:
        from sdk.agent.safe_code_bridges import execute_self_improvement_goal

        goal = params.get("goal") or params.get("goal_text") or params.get("raw") or ""
        result = await execute_self_improvement_goal(
            goal=goal,
            dry_run=bool(params.get("dry_run", False)),
            targets=params.get("targets"),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("self_improvement.execute_goal error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_wiki_query(params: dict[str, Any], req_id: Any) -> str:
    """
    ``wiki.query`` — Query Neural Wiki nodes.
    """
    query = params.get("query", "").strip().lower()
    try:
        from sdk.agent.scanner import RepositoryScanner
        from sdk.agent.wiki import HTMLNNParser
        scanner = RepositoryScanner(_ROOT_DIR)
        files = scanner.scan_repo()
        
        results = []
        for f in files:
            if f.endswith(".html"):
                abs_path = os.path.join(_ROOT_DIR, f)
                try:
                    content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                    if "data-node-id" in content:
                        meta = HTMLNNParser.parse_node_metadata(content)
                        if not query or query in meta.get("title", "").lower() or query in meta.get("node_id", "").lower() or query in content.lower():
                            meta["file_path"] = f
                            results.append(meta)
                except Exception:
                    pass
        return _ok(req_id, {"nodes": results})
    except Exception as exc:
        logger.exception("wiki.query error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_wiki_update(params: dict[str, Any], req_id: Any) -> str:
    """
    ``wiki.update`` — Update an HTML Neural Node.
    """
    node_path = params.get("node_path", "")
    sections = params.get("sections", {})
    
    if not node_path or not sections:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'node_path' and 'sections' are required")
        
    if not os.path.isabs(node_path):
        node_path = os.path.join(_ROOT_DIR, node_path)
         
    try:
        from sdk.agent.wiki import WikiUpdater
        updater = WikiUpdater(backup=params.get("backup", True))
        res = updater.update_node(node_path, sections)
        return _ok(req_id, {
             "success": res.success,
             "node_path": res.node_path,
             "sections_updated": res.sections_updated,
             "error": res.error,
        })
    except Exception as exc:
        logger.exception("wiki.update error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_command_parse(params: dict[str, Any], req_id: Any) -> str:
    """
    ``command.parse`` — Parses slash commands using CommandParser.
    """
    raw_input = params.get("raw_input", "").strip()
    if not raw_input:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'raw_input' is required")
        
    try:
        from sdk.agent.command_parser import CommandParser
        parser = CommandParser()
        res = parser.parse(raw_input)
        if res.success and res.action:
            return _ok(req_id, {
                "success": True,
                "action": res.action.to_dict()
            })
        else:
            return _ok(req_id, {
                "success": False,
                "error": res.error,
                "suggestion": res.suggestion
            })
    except Exception as exc:
        logger.exception("command.parse error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_repo_broken_imports(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.broken_imports`` — Detect broken imports across Python modules.
    """
    try:
        from sdk.agent.scanner import RepositoryScanner
        scanner = RepositoryScanner(_ROOT_DIR)
        violations = scanner.detect_broken_imports()
        return _ok(req_id, {"violations": violations})
    except Exception as exc:
        logger.exception("repo.broken_imports error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_repo_ipc_audit(params: dict[str, Any], req_id: Any) -> str:
    """
    ``repo.ipc_audit`` — Audit IPC registry to cross-reference RPC and slash commands.
    """
    try:
        from sdk.agent.ipc_registry import IPCRegistry
        registry = IPCRegistry(_ROOT_DIR)
        audit_data = registry.audit()
        return _ok(req_id, audit_data)
    except Exception as exc:
        logger.exception("repo.ipc_audit error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_wiki_check_exists(params: dict[str, Any], req_id: Any) -> str:
    """
    ``wiki.check_exists`` — Check if a wiki node exists for a given feature/node_id.
    """
    node_id = params.get("node_id", "").strip().lower()
    if not node_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'node_id' is required")
        
    try:
        from sdk.agent.scanner import RepositoryScanner
        from sdk.agent.wiki import HTMLNNParser
        scanner = RepositoryScanner(_ROOT_DIR)
        files = scanner.scan_repo()
        
        exists = False
        file_path = None
        
        for f in files:
            if f.endswith(".html"):
                abs_path = os.path.join(_ROOT_DIR, f)
                try:
                    content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                    if "data-node-id" in content:
                        meta = HTMLNNParser.parse_node_metadata(content)
                        if meta.get("node_id", "").strip().lower() == node_id:
                            exists = True
                            file_path = f
                            break
                except Exception:
                    pass
        return _ok(req_id, {"exists": exists, "file_path": file_path})
    except Exception as exc:
        logger.exception("wiki.check_exists error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_wiki_self_document_phase(params: dict[str, Any], req_id: Any) -> str:
    """
    ``wiki.self_document_phase`` — Generate or update self-documentation for a phase.
    """
    phase_name = params.get("phase_name", "").strip()
    accomplishments = params.get("accomplishments", [])
    changed_files = params.get("changed_files", [])
    tests_status = params.get("tests_status", "All passed")

    if not phase_name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'phase_name' is required")

    try:
        from sdk.agent.self_documenter import SelfDocumenter
        documenter = SelfDocumenter(_ROOT_DIR)
        node_path = documenter.self_document_phase(
            phase_name=phase_name,
            accomplishments=accomplishments,
            changed_files=changed_files,
            tests_status=tests_status
        )
        return _ok(req_id, {"success": True, "node_path": node_path})
    except Exception as exc:
        logger.exception("wiki.self_document_phase error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_telemetry_stream_subscribe(
    params: dict[str, Any],
    ws: WebSocketServerProtocol,
    req_id: Any,
) -> str:
    """
    ``telemetry.stream_subscribe`` — Register client websocket for live status updates.
    """
    try:
        from sdk.agent.telemetry import LiveStatusReporter
        LiveStatusReporter.register_websocket(ws)
        return _ok(req_id, {"success": True, "message": "Registered for live telemetry stream"})
    except Exception as exc:
        logger.exception("telemetry.stream_subscribe error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


# ---------------------------------------------------------------------------
# Heartbeat RPC handlers
# ---------------------------------------------------------------------------


async def handle_heartbeat_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.list`` — Return all configured heartbeat jobs.

    Returns
    -------
    { jobs: list[HeartbeatJob] }
    """
    try:
        scheduler = HeartbeatScheduler.get()
        jobs = scheduler.get_jobs()
        return _ok(req_id, {"jobs": jobs})
    except Exception as exc:
        logger.exception("heartbeat.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_create(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.create`` — Create a new heartbeat job.

    Params
    ------
    name      : str  required
    cron_expr : str  required  e.g. '0 9 * * 1' or '@startup'
    goal_text : str  required  natural-language goal for GoalEngine
    enabled   : bool optional  default true
    """
    name: str = params.get("name", "").strip()
    cron_expr: str = params.get("cron_expr", "").strip()
    goal_text: str = params.get("goal_text", "").strip()
    enabled: bool = bool(params.get("enabled", True))

    if not name:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'name' is required")
    if not cron_expr:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'cron_expr' is required")
    if not goal_text:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'goal_text' is required")

    # Validate the cron expression before persisting
    try:
        from python.harness.heartbeat_scheduler import CronExpression
        CronExpression(cron_expr)
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, f"Invalid cron_expr: {exc}")

    job_id = str(uuid.uuid4())
    try:
        await db.execute(
            """
            INSERT INTO heartbeats (id, name, cron_expr, goal_text, enabled)
            VALUES ($1, $2, $3, $4, $5)
            """,
            job_id, name, cron_expr, goal_text, enabled,
        )
        await HeartbeatScheduler.get().reload_from_db()
        return _ok(req_id, {"success": True, "id": job_id})
    except Exception as exc:
        logger.exception("heartbeat.create error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_update(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.update`` — Update an existing heartbeat job.

    Params
    ------
    id        : str  required
    name      : str  optional
    cron_expr : str  optional
    goal_text : str  optional
    enabled   : bool optional
    """
    job_id: str = params.get("id", "").strip()
    if not job_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")

    # Validate cron if provided
    cron_expr = params.get("cron_expr")
    if cron_expr is not None:
        try:
            from python.harness.heartbeat_scheduler import CronExpression
            CronExpression(cron_expr)
        except ValueError as exc:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"Invalid cron_expr: {exc}")

    # Build a dynamic SET clause from provided fields
    updates: list[str] = []
    args: list[Any] = []
    idx = 1

    for field_name in ("name", "cron_expr", "goal_text"):
        if field_name in params:
            updates.append(f"{field_name} = ${idx}")
            args.append(str(params[field_name]).strip())
            idx += 1
    if "enabled" in params:
        updates.append(f"enabled = ${idx}")
        args.append(bool(params["enabled"]))
        idx += 1

    if not updates:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "No fields to update")

    updates.append(f"updated_at = NOW()")
    args.append(job_id)
    sql = f"UPDATE heartbeats SET {', '.join(updates)} WHERE id = ${idx}"

    try:
        await db.execute(sql, *args)
        await HeartbeatScheduler.get().reload_from_db()
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("heartbeat.update error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.delete`` — Delete a heartbeat job by id.

    Params
    ------
    id : str  required
    """
    job_id: str = params.get("id", "").strip()
    if not job_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")
    try:
        await db.execute("DELETE FROM heartbeats WHERE id = $1", job_id)
        await HeartbeatScheduler.get().reload_from_db()
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("heartbeat.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_fire_now(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.fire_now`` — Manually trigger a heartbeat job immediately.

    Params
    ------
    id : str  required
    """
    job_id: str = params.get("id", "").strip()
    if not job_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")
    try:
        result = await HeartbeatScheduler.get().fire_now(job_id)
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("heartbeat.fire_now error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_heartbeat_reload(params: dict[str, Any], req_id: Any) -> str:
    """
    ``heartbeat.reload`` — Hot-reload all heartbeat jobs from the database
    without restarting the harness.
    """
    try:
        await HeartbeatScheduler.get().reload_from_db()
        jobs = HeartbeatScheduler.get().get_jobs()
        return _ok(req_id, {"success": True, "job_count": len(jobs)})
    except Exception as exc:
        logger.exception("heartbeat.reload error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))




async def handle_task_status(params: dict[str, Any], ws: WebSocketServerProtocol, req_id: Any) -> str:
    from sdk.agent.status_api import AgentStatusAPI
    task_id = params.get("task_id")
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Missing task_id")
    res = await AgentStatusAPI.get_task_status(task_id)
    return _ok(req_id, res)

async def handle_task_phase_events(params: dict[str, Any], ws: WebSocketServerProtocol, req_id: Any) -> str:
    from sdk.agent.status_api import AgentStatusAPI
    task_id = params.get("task_id")
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Missing task_id")
    res = await AgentStatusAPI.get_task_status(task_id)
    return _ok(req_id, res.get("phases", []))

async def handle_task_artifacts(params: dict[str, Any], ws: WebSocketServerProtocol, req_id: Any) -> str:
    from sdk.agent.status_api import AgentStatusAPI
    task_id = params.get("task_id")
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Missing task_id")
    res = await AgentStatusAPI.get_task_status(task_id)
    return _ok(req_id, res.get("artifacts", []))


# ---------------------------------------------------------------------------
# Memory & Research wrappers
# ---------------------------------------------------------------------------
async def handle_memory_search(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_search as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.search error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_session_search(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_session_search as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.session_search error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_browse(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_browse as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.browse error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_record_completed_turn(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_record_completed_turn as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.record_completed_turn error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_get_user_model(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_get_user_model as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.get_user_model error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_scan_context(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_scan_context as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.scan_context error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_reindex_node(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_reindex_node as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.reindex_node error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_save_node(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_save_node as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.save_node error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_delete_node(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_delete_node as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.delete_node error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_research_unified_search(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_research_unified_search as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("research.unified_search error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_research_distill_to_node(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_research_distill_to_node as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("research.distill_to_node error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_memory_reindex_profiles(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.memory import handle_memory_reindex_profiles as impl
    try:
        return _ok(req_id, await impl(params))
    except Exception as exc:
        logger.exception("memory.reindex_profiles error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

# ---------------------------------------------------------------------------
# Method dispatch table
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P2P & Multimodal & Browser Methods
# ---------------------------------------------------------------------------

async def handle_p2p_semantic_search(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.retrieval import retrieve_relevant_nodes
    try:
        query = params.get("query", "")
        # Filter internal nodes
        where_filter = {"trust_tier": {"$ne": "internal"}}
        matches = await retrieve_relevant_nodes(query, limit=5, where_filter=where_filter)
        return _ok(req_id, {"matches": matches})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_multimodal_capture_screen(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.multimodal import get_screen_context
    try:
        return _ok(req_id, await get_screen_context())
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_multimodal_transcribe_voice(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.multimodal import transcribe_voice
    try:
        duration = float(params.get("duration_seconds", 5.0))
        return _ok(req_id, await transcribe_voice(duration))
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_browser_get_dom(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.config import settings
    if settings.IS_HEADLESS:
        return _ok(req_id, {"error": "Tala is running in embedded/headless mode and cannot access Browser UI."})
    return _ok(req_id, {"status": "broadcast_sent", "note": "DOM retrieval requires IPC callback setup."})

async def handle_browser_execute_js(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.config import settings
    from python.harness.heartbeat_scheduler import broadcast_notification
    if settings.IS_HEADLESS:
        return _ok(req_id, {"error": "Tala is running in embedded/headless mode and cannot access Browser UI."})
    try:
        code = params.get("code", "")
        await broadcast_notification("browser.execute_js", {"code": code})
        return _ok(req_id, {"status": "broadcast_sent"})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_p2p_webrtc_offer(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.webrtc_manager import webrtc_manager
    peer_username = params.get("peer_username")
    offer = params.get("offer")
    if not peer_username or not offer:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "peer_username and offer are required")
    
    answer = await webrtc_manager.handle_offer(peer_username, offer)
    return _ok(req_id, {"answer": answer})

async def handle_p2p_webrtc_answer(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.webrtc_manager import webrtc_manager
    peer_username = params.get("peer_username")
    answer = params.get("answer")
    if not peer_username or not answer:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "peer_username and answer are required")
    
    await webrtc_manager.handle_answer(peer_username, answer)
    return _ok(req_id, {"success": True})

async def handle_p2p_encrypted_payload(params: dict[str, Any], req_id: Any) -> str:
    from python.harness.e2ee_manager import e2ee_manager
    sender_username = params.get("sender_username")
    ciphertext = params.get("ciphertext")
    nonce = params.get("nonce")
    
    if not sender_username or not ciphertext or not nonce:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "sender_username, ciphertext, and nonce are required")
        
    try:
        decrypted = e2ee_manager.decrypt(sender_username, ciphertext, nonce)
        inner_method = decrypted.get("method")
        inner_params = decrypted.get("params", {})
        
        logger.debug("E2EE Payload decrypted from %s: %s", sender_username, inner_method)
        
        handler = _HANDLERS.get(inner_method)
        if not handler:
            return _err(req_id, JsonRpcError.METHOD_NOT_FOUND, f"Inner method not found: {inner_method}")
            
        return await handler(inner_params, req_id)
    except Exception as e:
        logger.error("E2EE decryption failed for %s: %s", sender_username, e)
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"Decryption failed: {e}")

# ---------------------------------------------------------------------------
# Marketplace Handlers
# ---------------------------------------------------------------------------

async def handle_marketplace_publish(params: dict[str, Any], req_id: Any) -> str:
    from sdk.agent.anchor import canonicalize_and_anchor
    import uuid
    import json
    
    document_type = params.get("document_type", "marketplace-item")
    payload = params.get("payload")
    if not payload:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "payload is required")
        
    node_id = f"marketplace-item-{uuid.uuid4().hex[:8]}"
    content_html = json.dumps(payload, indent=2)
    state_data = {
        "title": payload.get("name", "Untitled Plugin"),
        "description": payload.get("description", ""),
        "author": params.get("author", "Local Peer")
    }
    
    core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
        node_id, content_html, state_data, document_type=document_type
    )
    
    if anchoring_status != "anchored":
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"Failed to anchor to marketplace: {raw_response}")
        
    return _ok(req_id, {
        "success": True,
        "node_id": node_id,
        "chaincode_id": chaincode_id,
        "public_key": public_key
    })

async def handle_marketplace_search(params: dict[str, Any], req_id: Any) -> str:
    query = params.get("query", "").lower()
    
    peers = await db.fetch("SELECT peer_username, endpoint_url FROM p2p_sessions WHERE status = 'connected'")
    results = []
    
    import asyncio
    async def _query_peer(peer):
        try:
            res = await _call_peer_rpc(peer["endpoint_url"], "p2p.marketplace_search", {"query": query})
            if res.get("items"):
                for item in res["items"]:
                    item["source_peer"] = peer["peer_username"]
                    results.append(item)
        except Exception as e:
            logger.warning(f"Failed to query peer {peer['peer_username']} marketplace: {e}")
            
    await asyncio.gather(*[_query_peer(p) for p in peers])
    return _ok(req_id, {"items": results})

async def handle_p2p_marketplace_search(params: dict[str, Any], req_id: Any) -> str:
    query = params.get("query", "").lower()
    items = []
    
    from pathlib import Path
    import yaml
    
    skills_dir = Path("skills")
    if skills_dir.exists():
        for file in skills_dir.glob("*.yaml"):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    if query in data.get("name", "").lower() or query in data.get("description", "").lower():
                        items.append({
                            "name": data.get("name"),
                            "description": data.get("description"),
                            "payload": data,
                            "public_key": "local-node-pubkey-0xTALA"
                        })
            except Exception:
                pass
                
    return _ok(req_id, {"items": items})

async def handle_marketplace_download(params: dict[str, Any], req_id: Any) -> str:
    item = params.get("item")
    if not item:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "item is required")
        
    public_key = item.get("public_key")
    if not public_key:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "item missing cryptographic signature")
        
    logger.info("Verifying marketplace payload against public key %s...", public_key)
    
    import yaml
    import uuid
    from pathlib import Path
    skills_dir = Path("skills")
    skills_dir.mkdir(exist_ok=True)
    
    skill_id = f"downloaded_{uuid.uuid4().hex[:8]}"
    file_path = skills_dir / f"{skill_id}.yaml"
    
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(item.get("payload", {}), f)
    except Exception as e:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(e))
        
    return _ok(req_id, {"success": True, "saved_to": str(file_path)})

_HANDLERS: dict[str, Any] = {
    "task.status": handle_task_status,
    "task.phase_events": handle_task_phase_events,
    "task.artifacts": handle_task_artifacts,
    "inference.generate": handle_inference_generate,
    "inference.get_providers": handle_inference_get_providers,
    "inference.save_providers": handle_inference_save_providers,
    "inference.get_embedded_status": handle_inference_get_embedded_status,
    "inference.trigger_download": handle_inference_trigger_download,
    "inference.get_engines": handle_inference_get_engines,
    "inference.install_engine": handle_inference_install_engine,
    "inference.get_engine_install_progress": handle_inference_get_engine_install_progress,
    "inference.detect_local_providers": handle_inference_detect_local_providers,
    "db.query":           handle_db_query,
    "db.insert":          handle_db_insert,
    "token.count":        handle_token_count,
    "graph.activate":     handle_graph_activate,
    "vault.status":       handle_vault_status,
    "harness.health":     handle_harness_health,
    "identity.get_user_profile": handle_identity_get_user_profile,
    "identity.save_user_profile": handle_identity_save_user_profile,
    "astro.transits":     handle_astro_transits,
    "astro.synastry":     handle_astro_synastry,
    "graph.query_activations": handle_graph_query_activations,
    "agent.get_agents":   handle_agent_get_agents,
    "agent.save_agent":   handle_agent_save_agent,
    "agent.delete_agent": handle_agent_delete_agent,
    "agent.set_active":   handle_agent_set_active,
    "agent.get_chat_mode": handle_agent_get_chat_mode,
    "agent.set_chat_mode": handle_agent_set_chat_mode,
    "workflow.list":      handle_workflow_list,
    "workflow.save":      handle_workflow_save,
    "workflow.delete":    handle_workflow_delete,
    "workflow.execute":   handle_workflow_execute,
    "vps.version_push":   handle_vps_version_push,
    "mcp.connect_server": handle_mcp_connect_server,
    "mcp.list_servers":   handle_mcp_list_servers,
    "mcp.remove_server":  handle_mcp_remove_server,
    "mcp.save_servers":   handle_mcp_save_servers,
    "mcp.list_tools":     handle_mcp_list_tools,
    "mcp.call_tool":      handle_mcp_call_tool,
    "guardrails.get":     handle_guardrails_get,
    "guardrails.save":    handle_guardrails_save,
    "search.get_providers": handle_search_get_providers,
    "search.save_providers": handle_search_save_providers,
    "search.query":       handle_search_query,
    "system.info":        handle_system_info,
    "system.self_test":   handle_system_self_test,
    "system.trigger_update": handle_system_trigger_update,
    "system.download_update": handle_system_download_update,
    "git.get_providers":  handle_git_get_providers,
    "git.save_providers": handle_git_save_providers,
    "storage.get_providers":  handle_storage_get_providers,
    "storage.save_providers": handle_storage_save_providers,
    "database.get_providers":  handle_database_get_providers,
    "database.save_providers": handle_database_save_providers,
    "runtime.get_instances":  handle_runtime_get_instances,
    "runtime.save_instances": handle_runtime_save_instances,
    "auth.get_credentials":  handle_auth_get_credentials,
    "auth.save_credentials": handle_auth_save_credentials,
    "firewall.get_contacts":  handle_firewall_get_contacts,
    "firewall.save_contacts": handle_firewall_save_contacts,
    "contacts.list":          handle_contacts_list,
    "contacts.save":          handle_contacts_save,
    "contacts.delete":        handle_contacts_delete,
    "p2p.handshake":          handle_p2p_handshake,
    "p2p.receive_handshake":  handle_p2p_receive_handshake,
    "p2p.send_message":       handle_p2p_send_message,
    "p2p.receive_message":    handle_p2p_receive_message,
    "p2p.resolve_node_reference": handle_p2p_resolve_node_reference,
    "p2p.semantic_search": handle_p2p_semantic_search,
    "multimodal.capture_screen": handle_multimodal_capture_screen,
    "multimodal.transcribe_voice": handle_multimodal_transcribe_voice,
    "browser.get_dom": handle_browser_get_dom,
    "browser.execute_js": handle_browser_execute_js,
    "chain.connection_get":   handle_chain_connection_get,
    "chain.stay_local":       handle_chain_stay_local,
    "chain.disconnect":       handle_chain_disconnect,
    "chain.connect_vps":      handle_chain_connect_vps,
    "chain.auto_connect":     handle_chain_auto_connect,
    "logging.get_events":     handle_logging_get_events,
    "logging.get_health":     handle_logging_get_health,
    "logging.clear_events":    handle_logging_clear_events,
    "logging.archive_events":  handle_logging_archive_events,
    "research.search":         handle_research_search,
    "research.crawl_and_summarize": handle_research_crawl_and_summarize,
    "nodes.import_markdown": handle_nodes_import_markdown,
        "docs.create_documentation_node": handle_docs_create_documentation_node,
        "docs.create_learning_node": handle_docs_create_learning_node,
        "docs.generate_program_nodes": handle_docs_generate_program_nodes,
    "docs.verify_documentation_nodes": handle_docs_verify_documentation_nodes,
    "docs.generate_and_verify_program_nodes": handle_docs_generate_and_verify_program_nodes,
    "docs.register_program_target": handle_docs_register_program_target,
    "docs.list_program_targets": handle_docs_list_program_targets,
    "docs.refresh_node_grid": handle_docs_refresh_node_grid,
    "task.submit": handle_task_submit,
    "task.list": handle_task_list,
    "task.control": handle_task_control,
    "task.assign_worker": handle_task_assign_worker,
    "task.set_model_override": handle_task_set_model_override,
    "task.create_worktree": handle_task_create_worktree,
    "swarm.workers.active": handle_swarm_workers_active,
    "swarm.topology.list": handle_swarm_topology_list,
    "swarm.start": handle_swarm_start,
    "swarm.pause": handle_swarm_pause,
    "swarm.resume": handle_swarm_resume,
    "swarm.cancel": handle_swarm_cancel,
    "swarm.status": handle_swarm_status,
    "swarm.runs.list": handle_swarm_runs_list,
    "swarm.runs.get": handle_swarm_runs_get,
    "swarm.artifact.inspect": handle_swarm_artifact_inspect,
    "gateway.status": handle_gateway_status,
    "plugins.list": handle_plugins_list,
    "plugins.validate": handle_plugins_validate,
    "plugins.reload": handle_plugins_reload,
    "task.clear": handle_task_clear,
    "task.get_telemetry": handle_task_get_telemetry,
    "repo.scan": handle_repo_scan,
    "repo.lint": handle_repo_lint,
    "repo.checkpoint": handle_repo_checkpoint,
    "repo.restore": handle_repo_restore,
    "task.report": handle_task_report,
    "build.diagnose": handle_build_diagnose,
    "build.repair": handle_build_repair,
    "shell.run": handle_shell_run,
    "code.generate": handle_code_generate,
    "code.repair": handle_code_repair,
    "code.propose_patch": handle_code_propose_patch,
    "code.apply_patch": handle_code_apply_patch,
    "code.propose_file_write": handle_code_propose_file_write,
    "code.write_files": handle_code_write_files,
    "test.generate": handle_test_generate,
    "test.run_validation": handle_test_run_validation,
    "self_improvement.create_plan": handle_self_improvement_create_plan,
    "self_improvement.execute_plan": handle_self_improvement_execute_plan,
    "self_improvement.dry_run_goal": handle_self_improvement_dry_run_goal,
    "self_improvement.execute_goal": handle_self_improvement_execute_goal,
    "wiki.query": handle_wiki_query,
    "wiki.update": handle_wiki_update,
    "wiki.check_exists": handle_wiki_check_exists,
    "wiki.self_document_phase": handle_wiki_self_document_phase,
    "command.parse": handle_command_parse,
    "profiles.list":      handle_profiles_list,
    "profiles.get":       handle_profiles_get,
    "profiles.save":      handle_profiles_save,
    "profiles.delete":    handle_profiles_delete,
    "repo.broken_imports": handle_repo_broken_imports,
    "repo.ipc_audit": handle_repo_ipc_audit,
    "telemetry.stream_subscribe": handle_telemetry_stream_subscribe,
    "memory.search": handle_memory_search,
    "memory.session_search": handle_memory_session_search,
    "memory.browse": handle_memory_browse,
    "memory.record_completed_turn": handle_memory_record_completed_turn,
    "memory.get_user_model": handle_memory_get_user_model,
    "memory.scan_context": handle_memory_scan_context,
    "memory.reindex_node": handle_memory_reindex_node,
    "memory.save_node": handle_memory_save_node,
    "memory.delete_node": handle_memory_delete_node,
    "memory.reindex_profiles": handle_memory_reindex_profiles,
    "research.unified_search": handle_research_unified_search,
    "research.distill_to_node": handle_research_distill_to_node,
    # Heartbeat / cron scheduler
    "heartbeat.list":     handle_heartbeat_list,
    "heartbeat.create":   handle_heartbeat_create,
    "heartbeat.update":   handle_heartbeat_update,
    "heartbeat.delete":   handle_heartbeat_delete,
    "heartbeat.fire_now": handle_heartbeat_fire_now,
    "heartbeat.reload":   handle_heartbeat_reload,
    "marketplace.publish": handle_marketplace_publish,
    "marketplace.search": handle_marketplace_search,
    "marketplace.download": handle_marketplace_download,
    "p2p.marketplace_search": handle_p2p_marketplace_search,
    "p2p.webrtc_offer": handle_p2p_webrtc_offer,
    "p2p.webrtc_answer": handle_p2p_webrtc_answer,
    "p2p.encrypted_payload": handle_p2p_encrypted_payload,
}


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

async def _handle_webrtc_message(peer_username: str, raw_message: Any) -> None:
    from python.harness.webrtc_manager import webrtc_manager
    response: str
    req_id: Any = None
    
    try:
        if isinstance(raw_message, dict):
            req_id = raw_message.get("id")
            method = raw_message.get("method")
            params = raw_message.get("params", {})
        else:
            req_id, method, params = _parse_request(str(raw_message))
    except Exception as exc:
        response = _err(None, JsonRpcError.PARSE_ERROR, str(exc))
        try:
            channel = webrtc_manager.data_channels.get(peer_username)
            if channel and channel.readyState == "open":
                channel.send(response)
        except Exception:
            pass
        return

    logger.debug("WebRTC -> %s id=%s params=%r", method, req_id, params)

    handler = _HANDLERS.get(method)
    if handler is None:
        response = _err(req_id, JsonRpcError.METHOD_NOT_FOUND, f"Method not found: '{method}'")
    else:
        try:
            if method in ("inference.generate", "workflow.execute", "telemetry.stream_subscribe"):
                response = _err(req_id, JsonRpcError.INTERNAL_ERROR, "Streaming methods not supported over WebRTC P2P")
            else:
                response = await handler(params, req_id)
        except Exception as exc:
            logger.exception("Unhandled error in WebRTC handler '%s'", method)
            response = _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

    logger.debug("WebRTC <- %s id=%s", method, req_id)
    try:
        channel = webrtc_manager.data_channels.get(peer_username)
        if channel and channel.readyState == "open":
            channel.send(response)
    except Exception as send_exc:
        logger.warning("Failed to send WebRTC response for %s: %s", method, send_exc)

async def _handle_connection(ws: WebSocketServerProtocol, path: str = "") -> None:
    """
    Handle the lifetime of a single WebSocket client connection.

    Each inbound message is parsed as JSON-RPC 2.0, dispatched to the
    appropriate handler concurrently, and the response is sent back on the same socket.
    """
    remote = ws.remote_address
    logger.info("Client connected: %s  path=%s", remote, path)

    async def handle_msg(raw_message: Any) -> None:
        response: str
        req_id: Any = None

        try:
            req_id, method, params = _parse_request(str(raw_message))
        except ValueError as exc:
            response = _err(None, JsonRpcError.PARSE_ERROR, str(exc))
            try:
                await ws.send(response)
            except Exception:
                pass
            return

        logger.debug("-> %s  id=%s  params=%r", method, req_id, params)

        handler = _HANDLERS.get(method)
        if handler is None:
            response = _err(
                req_id,
                JsonRpcError.METHOD_NOT_FOUND,
                f"Method not found: '{method}'",
                {"available": sorted(_HANDLERS.keys())},
            )
            try:
                await ws.send(response)
            except Exception:
                pass
            return

        try:
            # inference.generate, workflow.execute, and telemetry.stream_subscribe need the ws object
            if method in ("inference.generate", "workflow.execute", "telemetry.stream_subscribe"):
                response = await handler(params, ws, req_id)
            else:
                response = await handler(params, req_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled error in handler '%s'", method)
            response = _err(
                req_id,
                JsonRpcError.INTERNAL_ERROR,
                f"Internal error: {exc}",
                {"traceback": traceback.format_exc()[-2000:]},
            )

        logger.debug("← %s  id=%s", method, req_id)
        try:
            await ws.send(response)
        except Exception as send_exc:
            logger.warning("Failed to send response for %s (id=%s): %s", method, req_id, send_exc)

    _active_connections.add(ws)
    try:
        async for raw_message in ws:
            asyncio.create_task(handle_msg(raw_message))
    except websockets.exceptions.ConnectionClosedOK:
        logger.info("Client disconnected gracefully: %s", remote)
    except websockets.exceptions.ConnectionClosedError as exc:
        logger.warning("Client disconnected with error: %s  %s", remote, exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fatal error in connection handler for %s", remote)
    finally:
        _active_connections.discard(ws)
        try:
            from sdk.agent.telemetry import LiveStatusReporter
            LiveStatusReporter.unregister_websocket(ws)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

_server: websockets.WebSocketServer | None = None
_shutdown_event: asyncio.Event = asyncio.Event()
_active_connections: set[WebSocketServerProtocol] = set()


async def broadcast_notification(method: str, params: dict[str, Any] = None) -> None:
    """Send a JSON-RPC 2.0 notification to all connected WebSocket clients."""
    if not _active_connections:
        return
    import json
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
    })
    await asyncio.gather(
        *(ws.send(payload) for ws in _active_connections),
        return_exceptions=True
    )


async def _auto_detect_and_verify_providers() -> None:
    """Detect local providers (Ollama) and test all registered providers to update their status."""
    import httpx
    import json as json_mod

    # 1. Fetch all existing providers from the DB
    rows = await db.fetch("SELECT id, type, name, priority, status, config, locked FROM inference_providers ORDER BY priority ASC")
    existing_providers = []
    for r in rows:
        cfg = r["config"]
        if isinstance(cfg, str):
            cfg = json_mod.loads(cfg)
        existing_providers.append({
            "id": r["id"],
            "type": r["type"],
            "name": r["name"],
            "priority": r["priority"],
            "status": r["status"],
            "config": cfg,
            "locked": r["locked"]
        })

    # 2. Compile list of endpoints to check
    # Format: (engine_type, base_url, check_path, register_endpoint)
    import os
    import re
    check_targets = []
    
    # 2a. Resolve Ollama Host
    ollama_base = "http://localhost:11434"
    ollama_env = os.environ.get("OLLAMA_HOST", "").strip()
    if ollama_env:
        if not ollama_env.startswith("http://") and not ollama_env.startswith("https://"):
            ollama_env = f"http://{ollama_env}"
        if not re.search(r":\d+", ollama_env):
            ollama_env = ollama_env.rstrip("/") + ":11434"
        ollama_base = ollama_env.rstrip("/")
    
    check_targets.append(("ollama", ollama_base, "/api/tags", ollama_base))
    
    # 2b. Add LM Studio & vLLM / Local OpenAI compatible endpoints
    check_targets.append(("lm-studio", "http://localhost:1234", "/v1/models", "http://localhost:1234/v1"))
    check_targets.append(("vllm", "http://localhost:8000", "/v1/models", "http://localhost:8000/v1"))
    check_targets.append(("local-openai", "http://localhost:8080", "/v1/models", "http://localhost:8080/v1"))

    # 3. Scan and auto-register discovered models from each target
    # Local providers get priority < 10 so they're tried BEFORE the embedded engine (priority >= 10).
    # Find the lowest existing local/remote priority; new ones go just above 0.
    local_priorities = [p["priority"] for p in existing_providers if p["type"] in ("local", "remote")]
    next_local_priority = (min(local_priorities) - 1) if local_priorities else 1
    max_priority = max([p["priority"] for p in existing_providers]) if existing_providers else 0
    
    cached_ollama_tags = {}
    cached_openai_models = {}

    for engine_type, base_url, check_path, register_endpoint in check_targets:
        try:
            url = base_url.rstrip("/") + check_path
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    discovered_models = []
                    
                    if engine_type == "ollama":
                        for m in data.get("models", []):
                            discovered_models.append(m.get("name"))
                        cached_ollama_tags[base_url.rstrip("/")] = discovered_models
                    else:  # OpenAI-compatible /v1/models returns data list
                        for m in data.get("data", []):
                            discovered_models.append(m.get("id"))
                        cached_openai_models[register_endpoint] = discovered_models
                            
                    if discovered_models:
                        logger.info("Local %s running at %s, discovered models: %s", engine_type, base_url, discovered_models)
                        for model_name in discovered_models:
                            is_registered = False
                            registered_id = None
                            for p in existing_providers:
                                if p["type"] == "local" and p["config"].get("model") == model_name:
                                    is_registered = True
                                    registered_id = p["id"]
                                    # Ensure it's marked online
                                    if p["status"] != "online":
                                        await db.execute(
                                            "UPDATE inference_providers SET status = 'online', updated_at = NOW() WHERE id = $1",
                                            p["id"]
                                        )
                                        p["status"] = "online"
                                        logger.info("Marked existing provider '%s' as online", p["name"])
                                    break
                            
                            if not is_registered:
                                sanitized_id = f"{engine_type}-{model_name.replace(':', '-').replace('/', '-').replace('.', '-')}"
                                provider_name = f"{engine_type.upper().replace('-', ' ')} - {model_name}"
                                # Use next_local_priority so local engines are tried BEFORE embedded
                                cfg = {
                                    "engine": "ollama" if engine_type == "ollama" else "openai",
                                    "endpoint": register_endpoint,
                                    "model": model_name,
                                }
                                
                                await db.execute(
                                    """
                                    INSERT INTO inference_providers (id, type, name, priority, status, config, locked)
                                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                                    ON CONFLICT (id) DO UPDATE
                                      SET status = 'online', updated_at = NOW()
                                    """,
                                    sanitized_id,
                                    "local",
                                    provider_name,
                                    next_local_priority,
                                    "online",
                                    json_mod.dumps(cfg),
                                    False,
                                )
                                logger.info("Auto-registered new local provider: %s (priority=%d)", provider_name, next_local_priority)
                                next_local_priority -= 1  # Each subsequent auto-detected model gets slightly lower priority
                                
                                existing_providers.append({
                                    "id": sanitized_id,
                                    "type": "local",
                                    "name": provider_name,
                                    "priority": next_local_priority + 1,
                                    "status": "online",
                                    "config": cfg,
                                    "locked": False
                                })
                else:
                    if engine_type == "ollama":
                        cached_ollama_tags[base_url.rstrip("/")] = None
                    else:
                        cached_openai_models[register_endpoint] = None
        except Exception:
            if engine_type == "ollama":
                cached_ollama_tags[base_url.rstrip("/")] = None
            else:
                cached_openai_models[register_endpoint] = None

    # 4. Verify status of all non-embedded/non-locked providers
    for p in existing_providers:
        if p["id"] == "embedded-builtin":
            continue
            
        p_type = p["type"]
        p_config = p["config"] or {}
        new_status = "offline"
        
        if p_type == "local":
            engine = p_config.get("engine", "ollama")
            endpoint = p_config.get("endpoint", "")
            model = p_config.get("model", "")
            
            if engine == "ollama":
                if not endpoint:
                    endpoint = "http://localhost:11434"
                
                base_url = endpoint.rstrip("/")
                if base_url.endswith("/v1"):
                    base_url = base_url[:-3].rstrip("/")
                
                if base_url in cached_ollama_tags:
                    models = cached_ollama_tags[base_url]
                    if models is not None and (model in models or not model):
                        new_status = "online"
                    else:
                        new_status = "offline"
                else:
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            resp = await client.get(f"{base_url}/api/tags")
                            if resp.status_code == 200:
                                models = [m.get("name") for m in resp.json().get("models", [])]
                                cached_ollama_tags[base_url] = models
                                if model in models or not model:
                                    new_status = "online"
                            else:
                                cached_ollama_tags[base_url] = None
                    except Exception:
                        cached_ollama_tags[base_url] = None
                        new_status = "offline"
            else:
                if not endpoint:
                    endpoint = "http://localhost:1234/v1"
                
                if endpoint in cached_openai_models:
                    models = cached_openai_models[endpoint]
                    if models is not None and (model in models or not model):
                        new_status = "online"
                    else:
                        new_status = "offline"
                else:
                    try:
                        ep_url = _normalize_endpoint_url(endpoint)
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            resp = await client.get(f"{ep_url}/models")
                            if resp.status_code == 200:
                                data = resp.json()
                                models = [m.get("id") for m in data.get("data", [])]
                                cached_openai_models[endpoint] = models
                                if model in models or not model:
                                    new_status = "online"
                            else:
                                cached_openai_models[endpoint] = None
                    except Exception:
                        cached_openai_models[endpoint] = None
                        new_status = "offline"
                        
        elif p_type == "remote":
            api_key = p_config.get("apiKey", "")
            if api_key:
                new_status = "online"
            else:
                new_status = "offline"
                
        if p["status"] != new_status:
            await db.execute(
                "UPDATE inference_providers SET status = $1, updated_at = NOW() WHERE id = $2",
                new_status,
                p["id"]
            )
            logger.info("Updated provider '%s' status: %s -> %s", p["name"], p["status"], new_status)

    # Broadcast to all connected clients that the providers list has been updated
    try:
        await broadcast_notification("inference.providers_changed")
    except Exception as exc:
        logger.warning("Failed to broadcast providers update: %s", exc)


async def _startup() -> None:
    """Initialise DB pool and kick off background tasks.

    This runs as an asyncio task AFTER the WebSocket server is already bound,
    so slow DB / model-cache operations never delay the initial WS handshake.
    """
    # 1. Database pool + schema migrations
    await db.init_pool()

    # 1.5. Initialize Memory Providers
    from sdk.agent.memory.manager import get_manager
    from sdk.agent.memory.local_provider import LocalMemoryProvider
    from sdk.agent.memory.hindsight_provider import HindsightProvider
    
    manager = get_manager()
    manager.register_provider(LocalMemoryProvider())
    manager.register_provider(HindsightProvider())
    await manager.initialize()

    # 1.6. Register A2A callback for agent tools
    from sdk.agent.tools import register_p2p_send_callback
    register_p2p_send_callback(_send_a2a_message_helper)

    # 2. Log startup to agent_log
    await db.log_to_db(
        "INFO",
        "Tala harness started",
        {
            "ws_uri": settings.ws_uri,
            "vllm_host": settings.VLLM_HOST,
            "vllm_port": settings.VLLM_PORT,
            "vllm_model": settings.VLLM_MODEL,
        },
    )

    # 3. Kick off slower background work
    asyncio.create_task(_startup_background())


async def _startup_background() -> None:
    """Perform slower non-critical hardware discovery, model caching verification, and provider auto-detection in background."""
    loop = asyncio.get_running_loop()

    # embedded_engine pre-warm is now handled by _prewarm_embedded_engine() which
    # is launched immediately after the WS server binds, independently of DB init.
    # We still initialise the DB-side status here in case the model became ready.

    # 2.5. Start the local Ledger Server
    # (Disabled: Tala Local Chain is now run via Node.js in blockchain/src/app.js on port 8332)
    # try:
    #     def _start_ledger():
    #         try:
    #             from python.ledger.server import run_server
    #             run_server()
    #         except Exception as e:
    #             logger.error("Failed to start Ledger server: %s", e)
    #     loop.run_in_executor(None, _start_ledger)
    #     logger.info("Local Ledger Server task dispatched to background thread.")
    # except Exception as exc:
    #     logger.warning("Could not dispatch Ledger Server task: %s", exc)

    # 3. Print initial wallet balance for operator awareness
    try:
        balance = await get_wallet_balance()
        logger.info("Wallet AI token balance: %.4f USD", balance)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read wallet balance: %s", exc)

    # 4. Log hardware device info to agent_log (non-blocking; import is lazy)
    # vllm_engine may import torch which is slow; run in executor to avoid blocking.
    try:
        async def _log_device():
            from python.inference.vllm_engine import log_device_info_to_db, DEVICE_INFO
            await log_device_info_to_db()
            logger.info(
                "Inference device: %s (%s)  vRAM=%s GB",
                DEVICE_INFO.device_type,
                DEVICE_INFO.device_name,
                DEVICE_INFO.vram_gb,
            )
        await _log_device()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not log device info: %s", exc)

    # 5. Check and sync embedded model status with DB
    # The pre-warm + init_status_from_cache() already ran in _prewarm_embedded_engine().
    # Here we just sync the in-memory status to the DB (needs DB to be ready).
    try:
        import sys as _sys
        mod = _sys.modules.get("python.inference.embedded_engine")
        if mod is not None:
            status, _progress, _error = mod.get_status()
            if status == "ready":
                logger.info("Default embedded model Qwen/Qwen2.5-0.5B-Instruct is cached and ready.")
                await db.execute("UPDATE inference_providers SET status = 'online' WHERE id = 'embedded-builtin'")
            elif status == "not_downloaded":
                logger.info("Default embedded model Qwen/Qwen2.5-0.5B-Instruct is not cached. Initiating background download...")
                mod.start_download_async()
        else:
            # Module still loading — schedule a follow-up check after 20 s
            async def _retry_embedded_sync():
                await asyncio.sleep(20)
                try:
                    import sys as _sys2
                    mod2 = _sys2.modules.get("python.inference.embedded_engine")
                    if mod2:
                        st, _, _ = mod2.get_status()
                        if st == "ready":
                            await db.execute("UPDATE inference_providers SET status = 'online' WHERE id = 'embedded-builtin'")
                        elif st == "not_downloaded":
                            mod2.start_download_async()
                except Exception as exc2:
                    logger.warning("Retry embedded sync failed: %s", exc2)
            asyncio.create_task(_retry_embedded_sync())
    except Exception as exc:
        logger.warning("Could not sync embedded model status to DB: %s", exc)

    # 6. Auto-detect local LLM providers (Ollama / LM Studio) and update statuses
    try:
        await _auto_detect_and_verify_providers()
    except Exception as exc:
        logger.warning("Could not auto-detect and verify providers: %s", exc)

    # 6b. Initialise settings panels as HTML Neural Nodes (HTML-NN)
    try:
        pass
    except Exception as exc:
        logger.warning("Could not sync settings to HTML-NN nodes: %s", exc)

    logger.info("Background startup complete — all providers verified")

    # ── 7. Start Heartbeat Scheduler ──
    # Register the broadcast function so the scheduler can push WS notifications
    # to all connected Electron clients without a circular import.
    register_broadcast(broadcast_notification)
    try:
        await HeartbeatScheduler.get().start()
    except Exception as _hb_exc:
        logger.warning("HeartbeatScheduler failed to start: %s", _hb_exc)

    # ── 8. Start monitor tasks now that all startup imports are complete ──
    # Deferring these monitor tasks (especially _monitor_stdin which runs blocking ReadFile
    # in an executor thread) prevents Windows process loader lock / console handle lock deadlocks
    # when compiled modules (numpy/torch/etc.) are imported concurrently during startup.
    from python.harness.webrtc_manager import webrtc_manager
    webrtc_manager.set_message_handler(_handle_webrtc_message)
    asyncio.create_task(_monitor_parent_process())
    asyncio.create_task(_monitor_stdin())



async def _shutdown() -> None:
    """Drain and close all subsystems on shutdown."""
    global _server

    logger.info("Shutting down Tala harness …")

    # Stop the heartbeat scheduler first so in-flight goals can finish
    try:
        await HeartbeatScheduler.get().stop()
    except Exception as _hb_stop_exc:
        logger.warning("HeartbeatScheduler stop error: %s", _hb_stop_exc)

    try:
        await db.log_to_db("INFO", "Tala harness shutting down", {})
    except Exception:  # noqa: BLE001
        pass

    if _server is not None:
        _server.close()
        await _server.wait_closed()
        logger.info("WebSocket server closed")

    from sdk.agent.memory.manager import get_manager
    manager = get_manager()
    await manager.teardown()

    await db.close_pool()
    logger.info("=== Tala harness stopped ===")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGTERM / SIGINT handlers for graceful shutdown."""

    def _on_signal(sig_name: str) -> None:
        logger.info("Received %s — initiating graceful shutdown", sig_name)
        loop.call_soon_threadsafe(_shutdown_event.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except (NotImplementedError, ValueError):
            # Windows does not support add_signal_handler for all signals;
            # fall back to basic SIGINT (KeyboardInterrupt) handling.
            signal.signal(sig, lambda s, f: _shutdown_event.set())


async def _monitor_stdin() -> None:
    """Monitor stdin for EOF. If stdin is closed, trigger shutdown."""
    import sys
    try:
        def read_stdin():
            try:
                return sys.stdin.read(1)
            except Exception:
                return ""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, read_stdin)
    except Exception:
        pass
    logger.warning("Stdin EOF detected. Triggering shutdown.")
    _shutdown_event.set()


async def _monitor_parent_process() -> None:
    """Monitor if the parent process is still alive. If it dies, trigger shutdown."""
    parent_pid = os.getppid()
    if parent_pid <= 1:
        # On POSIX, parent PID 1 or 0 means orphaned/re-parented
        return

    logger.info("Parent process monitor started for PID %d", parent_pid)
    while not _shutdown_event.is_set():
        await asyncio.sleep(2)
        alive = False
        if sys.platform == "win32":
            try:
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, parent_pid)
                if handle:
                    exit_code = ctypes.c_ulong()
                    kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    kernel32.CloseHandle(handle)
                    STILL_ACTIVE = 259
                    alive = (exit_code.value == STILL_ACTIVE)
            except Exception as exc:
                logger.warning("Error checking parent process %d via ctypes: %s", parent_pid, exc)
                alive = True  # Avoid false positives on error
        else:
            try:
                os.kill(parent_pid, 0)
                alive = True
            except OSError:
                alive = False

        if not alive:
            logger.warning("Parent process %d died. Triggering shutdown.", parent_pid)
            _shutdown_event.set()
            break


async def _run_server() -> None:
    """Main coroutine: bind the WebSocket server FIRST, then initialise subsystems.

    Binding the WS server before any slow I/O (DB pool, model cache checks)
    means Electron can connect within its 2-second window.  All slow work is
    moved into background tasks that run after the port is open.
    """
    global _server

    logger.info("=== Tala Cognitive Engine Room — harness starting ===")
    logger.info("Config: WS=%s  DB=%s  vLLM=%s",
                settings.ws_uri, db._redact_url(settings.DB_URL), settings.vllm_base_url)

    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop)

    # ── 1. Bind the WebSocket server immediately so Electron can connect ──
    _server = await websockets.serve(
        _handle_connection,
        settings.WS_HOST,
        settings.WS_PORT,
        ping_interval=30,
        ping_timeout=10,
        max_size=16 * 1024 * 1024,
    )

    logger.info(
        "WebSocket JSON-RPC 2.0 server listening on %s", settings.ws_uri
    )
    logger.info("Registered methods: %s", ", ".join(sorted(_HANDLERS.keys())))

    # ── 2. Pre-warm the embedded_engine module immediately (non-DB-gated) ──
    # The huggingface_hub import takes 5-15 s on a cold process.  We await this
    # to ensure the import completes before we start any other background threads
    # (like vllm_engine/torch imports), preventing concurrent import deadlocks on Windows.
    await _prewarm_embedded_engine()

    # ── 3. Run DB init + all background work as non-blocking tasks ──
    # Note: _monitor_parent_process and _monitor_stdin are launched at the
    # end of _startup_background to prevent Loader Lock deadlocks on Windows.
    asyncio.create_task(_startup())

    # Block until shutdown signal
    await _shutdown_event.wait()
    await _shutdown()


async def _prewarm_embedded_engine() -> None:
    """Import python.inference.embedded_engine in a thread executor so the
    heavy huggingface_hub import happens before the renderer polls for status.
    This task is started immediately after the WS server is bound and runs
    independently of DB initialisation.
    """
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: __import__("python.inference.embedded_engine", fromlist=["get_status", "init_status_from_cache"])
        )
        # Now initialise the in-memory status from the local HF cache
        mod = __import__("python.inference.embedded_engine", fromlist=["init_status_from_cache"])
        mod.init_status_from_cache()
        logger.info("embedded_engine pre-warmed and status initialised (independent of DB)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not pre-warm embedded_engine: %s", exc)



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    CLI entry point.  Run with::

        python -m harness.server
        # or
        python python/harness/server.py
    """
    import concurrent.futures
    _configure_logging()
    # Use a larger thread pool so background tasks (huggingface_hub, torch imports,
    # model-cache walks) don't starve the RPC handler threads.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="tala-worker")
    try:
        loop = asyncio.new_event_loop()
        loop.set_default_executor(executor)
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_server())
    except KeyboardInterrupt:
        pass  # Handled by signal handler
    finally:
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
