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
import contextvars
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
from sdk.agent.documentation_nodes import (
    resolve_user_data_dir,
    resolve_active_nodes_dir,
    resolve_active_workflows_dir,
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

# Active chat mode (rp, hybrid, assistant). These now live in
# python/harness/chat_mode.py so memory.py can share them without importing
# server.py back -- see that module for why there were two disagreeing sources.
from python.harness.chat_mode import (  # noqa: E402
    get_chat_mode_file_path,
    get_current_chat_mode,
    is_emotional_mode,
    save_current_chat_mode,
)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

class UnbufferedStreamHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()

def _configure_logging() -> None:
    """Configure root logger to match settings.LOG_LEVEL with instant flush."""
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
    handler = UnbufferedStreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    ))
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = [handler]

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


#: The RPC method currently being handled, so _err can say what failed without
#: every handler having to pass its own name in.
_CURRENT_RPC: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_CURRENT_RPC", default=""
)

#: Failures that are the caller getting it wrong rather than the harness
#: breaking. Still logged, but at WARNING: an audit log where a typo'd
#: parameter looks the same as a crashed subsystem is one nobody reads.
_CALLER_FAULT_CODES = frozenset({
    JsonRpcError.INVALID_PARAMS,
    JsonRpcError.METHOD_NOT_FOUND,
    JsonRpcError.INVALID_REQUEST,
    JsonRpcError.PARSE_ERROR,
})


def _audit_rpc_failure(code: int, message: str, data: Any = None) -> None:
    """
    Record an RPC failure in ``agent_log``.

    Every RPC error response funnels through ``_err``, which is why the hook is
    here rather than in the dispatcher: most handlers catch their own
    exceptions and return ``_err`` directly, so the dispatcher's ``except``
    never sees them. Before this, ``agent_log`` held 44,464 rows of which
    exactly **one** was an ERROR -- the audit log recorded everything except
    things going wrong, which is the one thing an audit log is for (UI-085).

    Fire-and-forget by design: a failure to log a failure must not become a
    second failure. If there is no running loop -- a synchronous test, or
    ``_err`` called during startup -- it is skipped rather than raised.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    level = "WARNING" if code in _CALLER_FAULT_CODES else "ERROR"
    method = _CURRENT_RPC.get() or "unknown"
    trace: dict[str, Any] = {"method": method, "code": code}
    if isinstance(data, dict):
        # Keep the traceback, drop anything large enough to bloat the table.
        for key in ("traceback", "reason", "detail"):
            if key in data:
                trace[key] = str(data[key])[:2000]

    try:
        loop.create_task(
            db.log_to_db(level, f"{method}: {message}"[:1000], trace)
        )
    except Exception:  # pragma: no cover - never let logging break a response
        pass


def _err(request_id: Any, code: int, message: str, data: Any = None) -> str:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _audit_rpc_failure(code, message, data)
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

    # Word-boundary matched, not substring. A bare "doc" in this list matched
    # inside ordinary words -- "docker", "doctor" -- so any message containing
    # one of those plus "check"/"verify" was intercepted here, ran a
    # documentation audit, and returned that as the entire reply. The user's
    # message never reached the model. Confirmed live: a prompt mentioning
    # terminal_backends/docker_backend.py was answered with
    # "Documentation disk verification complete."
    #
    # The multi-word phrases below stay as substrings -- they cannot collide
    # with a single ordinary word.
    has_doc_context = bool(
        re.search(r"\b(docs?|documents?|documentation)\b", lowered)
    ) or any(term in lowered for term in ("html-nn", "neural node", "program target"))
    if not has_doc_context:
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


_TOOL_CALL_RE = re.compile(r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))', re.DOTALL)
_CLOSED_THINKING_RE = re.compile(r'<thinking>.*?</thinking>', re.DOTALL | re.IGNORECASE)
_OPEN_THINKING_RE = re.compile(r'<thinking>', re.IGNORECASE)

# Reasoning models (qwen3, DeepHermes-R1, deepseek-r1, etc.) emit their private
# reasoning in <think>...</think>, but the entire harness convention is built on
# <thinking>...</thinking>. Without this normalization their chain-of-thought
# leaks into the visible reply AND any <call:tool/> tag they emit gets buried in
# unrecognized reasoning and never fires. Rewriting the exact tags up front lets
# every downstream consumer (tool detection, thinking-strip, renderer) work
# unchanged. Exact-tag regex: `<think>` never matches inside `<thinking>`.
_THINK_OPEN_RE = re.compile(r'<think>', re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r'</think>', re.IGNORECASE)


def _normalize_think_tags(text: str) -> str:
    """Rewrite reasoning-model <think>/</think> tags to the harness-standard
    <thinking>/</thinking> so all thinking-aware machinery applies to them."""
    if not text:
        return text
    text = _THINK_OPEN_RE.sub("<thinking>", text)
    text = _THINK_CLOSE_RE.sub("</thinking>", text)
    return text

# Casual/roleplay messages never get tool documentation injected into the
# system prompt or the native tools schema attached to the request -- small
# local fallback models reliably echo example call syntax from that
# documentation verbatim when they have nothing real to do with it (e.g. a
# flirty greeting), and the <call:...> parser then executes that echoed text
# as a genuine tool call.
#
# Regression: this used to be a plain substring check (`term in
# prompt.lower()`), which meant the 2-character term "hi" matched inside
# completely ordinary words -- any word containing "hi" ("everyt-HI-ng",
# "mach-HI-ne", "wh-HI-sper", etc.). Confirmed live: a plain task request
# ("...and run it to confirm everything passes") got classified as casual
# chat purely because "everything" contains "hi", silently stripping all
# tool-call documentation from the system prompt -- so the model had no
# instructions on how to actually write/run a file and just described the
# steps in prose instead of doing them. Word-boundary matching so a term
# only counts as itself, never as a substring of an unrelated word.
_CASUAL_CHAT_TERMS = ("roleplay", "hey", "hello", "hi", "sexy", "baby", "love", "honey", "sweetheart")
_CASUAL_CHAT_TERMS_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(t) for t in _CASUAL_CHAT_TERMS) + r')\b', re.IGNORECASE
)


def is_casual_chat_prompt(prompt: str) -> bool:
    """Classify whether `prompt` reads as casual/roleplay chat rather than a
    task request. See _CASUAL_CHAT_TERMS_RE above for what changed and why."""
    if not prompt or not isinstance(prompt, str):
        return False
    return bool(_CASUAL_CHAT_TERMS_RE.search(prompt))


_HTML_FENCE_RE = re.compile(r"```(?:html)?\s*\n?(.*?)\n?\s*```", re.DOTALL | re.IGNORECASE)


def extract_html_nn_document(generated_raw: str) -> str | None:
    """
    Pull a real HTML-NN document out of an LLM response, stripping the
    markdown code fence the model was instructed to wrap it in.

    Returns None if `generated_raw` doesn't actually contain a recognizable
    HTML-NN article (missing <article ...data-node-id=...> markup) -- e.g.
    the model ignored the formatting instructions and returned prose
    instead. Callers must not write that to nodes/ as if it were a real
    node.
    """
    if not generated_raw or not isinstance(generated_raw, str):
        return None

    m = _HTML_FENCE_RE.search(generated_raw)
    candidate = m.group(1).strip() if m else generated_raw.strip()

    if "data-node-id" not in candidate or "<article" not in candidate:
        return None

    return candidate


def resolve_nodes_dir() -> Path:
    """
    Resolve the active nodes/ directory, creating it if needed. Thin
    wrapper around sdk.agent.documentation_nodes.resolve_active_nodes_dir()
    -- the single canonical resolver every node-writing path in the harness
    should use (TALA_USER_DATA override -> APPDATA if set -> a directory
    relative to the application's own install location; never a hardcoded
    OS-specific profile path, since not every install/OS has one). See
    self-documenter-learning-node-root-dir-gap and
    html-nn-universal-data-mandate in project memory.
    """
    nodes_dir = resolve_active_nodes_dir()
    nodes_dir.mkdir(parents=True, exist_ok=True)
    return nodes_dir


def _close_unclosed_thinking(text: str) -> str:
    """
    If `text` opens a <thinking> block with no matching </thinking>
    anywhere, close it by appending </thinking> at the end.

    Confirmed live production bug: a model that opens <thinking> and then
    never transitions to a real answer (runs out of budget, drifts off)
    produces text that's ambiguous to every consumer of inference.generate's
    `text` field -- the Electron renderer, the MCP server, a script -- each
    would need its own bespoke unclosed-tag handling, and getting it wrong
    means raw reasoning leaks straight to the user. The renderer already
    has this fix client-side (hides an unclosed block instead of showing
    raw text), but that only protects the one client that happens to have
    it; the RPC response itself was still returning the leaked text as-is.

    Auto-closing here makes the well-formed <thinking>...</thinking>
    convention hold universally: an unclosed-thinking turn becomes "no real
    answer this turn, only thinking" for every consumer, using the same tag
    semantics already used for turns that closed the tag correctly.
    """
    if _OPEN_THINKING_RE.search(text) and not _CLOSED_THINKING_RE.search(text):
        return text.rstrip() + "\n</thinking>"
    return text


def _find_tool_call(text: str) -> "re.Match[str] | None":
    """
    Find the real ``<call:...>`` tool invocation, skipping any occurrence
    that falls inside a well-formed ``<thinking>...</thinking>`` block.

    Reasoning-capable models often narrate the tag they're about to emit as
    part of their visible thinking (e.g. "I should output `<call:foo />`")
    before actually emitting it. A plain unanchored search finds that quoted
    mention first, which then swallows everything after it -- including the
    real, well-formed call -- as bogus arguments to whatever malformed
    "tool name" the reasoning text happened to contain there (e.g. a bare
    "..." used as a placeholder). Restricting the search to start after the
    first closed thinking block avoids this regardless of which provider or
    model produced the text -- it only depends on the harness's own
    <thinking> convention, not on any model-specific behavior.

    Uses re.Pattern.search(text, pos) rather than slicing the string, so a
    returned match's .start()/.end() stay valid offsets into the original,
    unmodified `text` (callers rely on this, e.g. to truncate generated_text
    at the call's end).
    """
    search_start = 0
    think_match = _CLOSED_THINKING_RE.search(text)
    if think_match:
        search_start = think_match.end()
    return _TOOL_CALL_RE.search(text, search_start)


# Appended to every injected-node block.
#
# Asked how her own weights work, Tala answered that they are "learned through
# iterative training on the Tala Training Stories corpus" and that they select
# inference providers. Both invented. The nodes she had described structure --
# function and class names from AST analysis -- and said nothing about the
# activation arithmetic, so the mechanism was not available to her and she
# supplied one.
#
# Better ranking would not have helped: the node that documents ActivationEngine
# ranked 9th for that question and does not contain the formula either. When the
# fact is absent from the graph, the only correct behaviour is to say so.
#
# So this does not try to make her right about herself -- coverage does that, and
# it is a larger job. It makes her honest about the boundary of what she can see,
# which is the part that is achievable now and the part that makes the wrong
# answers stop looking like right ones.
#
# Scoped deliberately to her own architecture. A blanket "only say what is in
# context" would make her refuse ordinary conversation, and a directive that
# fires on everything is one the model learns to ignore.
_SELF_KNOWLEDGE_GROUNDING = (
    "\n[Grounding: statements about your own architecture]\n"
    "When you describe how you yourself work -- your nodes, weights, memory, activation, "
    "adapters, scheduling, tools, or any internal mechanism -- say only what the nodes above "
    "actually state. Do not reason outward from a node's title, and do not fill a gap with "
    "what would plausibly be true of a system like you.\n"
    "If the nodes above do not cover the mechanism you were asked about, say plainly that it "
    "is not in the memory you can see, name the node you would need, and offer to read the "
    "source. An honest gap is correct; a confident reconstruction is not, and it is worse "
    "than saying nothing because Weavor cannot tell the two apart.\n"
    "This applies to your own internals only. Everything else -- conversation, opinion, the "
    "world, work you are doing for Weavor -- is unaffected.\n"
)


# The same grounding, restated at the tail of the user's message.
#
# _SELF_KNOWLEDGE_GROUNDING in the system prompt is not enough on a 4B: asked the
# same question with it in place, the model still invented
# data-execution-authority="high" (the real values are floats) and a priority
# pipeline to go with it. Moved to the tail and it answered "the mechanism is not
# explicitly detailed in the nodes above", hedged the one thing it could infer,
# and named what was not covered.
#
# Placement, not wording -- the same asymmetry as the XML tool tag earlier on
# this branch, and the reason _XML_CALL_REMINDER already sits here. A small model
# loses a system instruction behind several thousand tokens of injected context;
# it does not lose the sentence directly above its own turn.
_SELF_KNOWLEDGE_REMINDER = (
    "[SYSTEM REMINDER]: This question is about your own internals. Answer ONLY from the "
    "node text in your context. If it does not state the mechanism, say it is not in the "
    "memory you can see and offer to read the source. Do NOT invent attribute values or "
    "behaviour."
)

# What counts as a question about her own internals.
#
# Word boundaries throughout, and no bare fragments: substring gates have
# short-circuited turns in this codebase three separate times ('doc' inside
# 'docker'). This gate only appends a reminder, so a false positive costs one
# sentence rather than the turn -- but a false positive on ordinary conversation
# would still make her hedge about things she has no need to hedge about.
_SELF_REFERENTIAL_RE = re.compile(
    r"\b(?:"
    r"html-?nn|neural node|semantic memory|activation|activate[sd]?|"
    r"execution authority|memory authority|governance|"
    r"lora|adapter[s]?|weight[s]?|node graph|cognitive net|"
    r"heartbeat|scheduler|tool registry|your (?:memory|nodes|weights|architecture|internals)"
    r")\b",
    re.IGNORECASE,
)


def _is_self_referential(prompt: str) -> bool:
    """Whether the prompt asks how Tala herself works."""
    return bool(_SELF_REFERENTIAL_RE.search(prompt or ""))


# Tail reminder appended to the user's own message, because small local models
# lose earlier system instructions in a long prompt. Hoisted to a constant so the
# native-tools path can swap it out by identity along with the rest of the XML
# protocol -- left in place it would tell a model that has no XML documentation to
# emit an XML tag anyway, which is how a working native tool call turns into a
# hand-written tag with invented syntax.
_XML_CALL_REMINDER = (
    "[SYSTEM REMINDER]: If you decide to invoke a tool, you MUST output the raw XML `<call:...>` tag. "
    "Do NOT wrap the tool call in markdown backticks like ```xml. "
    "If you do NOT need a tool (for example, if you are asking the user a question or answering them), "
    "simply respond with normal text."
)

_NATIVE_CALL_REMINDER = (
    "[SYSTEM REMINDER]: If you decide to invoke a tool, call it directly through the tool interface. "
    "Do NOT write the call out as text or as a code block. "
    "If you do NOT need a tool (for example, if you are asking the user a question or answering them), "
    "simply respond with normal text."
)

# Replaces the XML tool documentation when native `tools` are sent instead.
# Ollama renders the tool schemas into the chat template itself, so listing them
# again here would be redundant -- what does NOT survive the swap is the
# behavioural directive, which is about willingness to act rather than syntax,
# so that part is restated.
_NATIVE_TOOL_DIRECTIVE = (
    "[Agent Capability: Tools]\n"
    "You have tools available; they are described to you directly by the runtime. "
    "When Weavor asks you to search, find, crawl, open, or browse the web -- or to do "
    "anything else one of your tools does -- call that tool immediately. Do NOT ask for "
    "permission, do NOT ask for clarification first, and do NOT describe the call you "
    "could make instead of making it. You will receive the result in the next turn.\n"
    "Only call a tool when the request actually needs one. If Weavor is talking to you, "
    "asking how you are, or asking something you can answer yourself, just answer -- "
    "having a tool available is not a reason to use it.\n\n"
)


# research.search and research.crawl_and_summarize are dispatched through the
# RPC METHODS table, not ToolRegistry, so they are documented by hand in the XML
# protocol block and would be invisible to a model that only ever sees the native
# `tools` array. Declared here so swapping the protocol swaps ALL of it -- losing
# web search on the native path would look exactly like the model refusing to
# search. Kept next to the XML text they mirror; the two must be edited together.
_BUILTIN_RESEARCH_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "research.search",
            "description": (
                "Search the web (DuckDuckGo). Returns titles, snippets and URLs of the "
                "top matching pages, and syncs the user's browser view to the results."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research.crawl_and_summarize",
            "description": (
                "Download the full content of the given webpages, read them, and compile "
                "them into a styled HTML Neural Node. The node is saved automatically -- "
                "do not try to re-write or re-save the returned html_nn yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Full URLs to crawl.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Short description of what the compiled node is about.",
                    },
                },
                "required": ["urls", "subject"],
            },
        },
    },
]


def _ollama_native_tool_specs(dyn_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Translate ToolRegistry entries into Ollama's native ``tools`` array.

    ToolRegistry already stores a JSON Schema per tool (hand-written, or
    derived from the function signature by ToolRegistry._generate_schema), so
    this is a straight re-wrap into the OpenAI-style function envelope Ollama
    expects -- no schema is invented here.

    Only used on the native /api/chat path. Everywhere else the harness sticks
    to its universal ``<call:tool />`` text convention; see the note at the
    payload construction site for why the two are never offered at once.
    """
    specs = []
    for t in dyn_tools:
        name = t.get("name")
        if not name:
            continue
        schema = t.get("schema") or {}
        if schema.get("type") != "object":
            # Ollama rejects a parameters block that isn't an object schema.
            schema = {"type": "object", "properties": {}, "required": []}
        specs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description", "") or "",
                "parameters": {
                    "type": "object",
                    "properties": schema.get("properties", {}) or {},
                    "required": schema.get("required", []) or [],
                },
            },
        })
    return specs


def _select_tools_for_turn(
    specs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tool_index: Any,
    p_type: str,
    p_config: dict[str, Any],
    p_name: str,
    sticky: set[str],
) -> list[dict[str, Any]]:
    """
    Narrow this turn's tool specs to what the provider can afford to carry.

    Returns the full set unchanged when there is no budget or no index, so a
    failure anywhere in selection degrades to today's behaviour rather than to
    a model with no tools.

    Withheld tools are logged every time. A silently reduced toolset looks
    exactly like a broken registry from the outside, and that ambiguity is the
    failure family this branch exists to remove.
    """
    if not specs or tool_index is None:
        return specs
    try:
        from python.harness.tool_selection import (  # noqa: PLC0415
            budget_for,
            conversation_text,
            select_specs,
        )

        budget = budget_for(p_type, p_config)
        if budget <= 0:
            return specs
        kept, withheld = select_specs(
            specs, conversation_text(messages), tool_index, budget, sticky=sticky,
        )
        if withheld:
            logger.info(
                "Provider '%s': offering %d/%d tools this turn (budget %d, %d sticky); withheld %s",
                p_name, len(kept), len(specs), budget, len(sticky),
                ", ".join(sorted(withheld)[:8]) + ("..." if len(withheld) > 8 else ""),
            )
        return kept or specs
    except Exception as exc:
        logger.warning("Tool selection failed for '%s' (%s); offering all tools", p_name, exc)
        return specs


def _swap_xml_protocol_for_native(
    messages: list[dict[str, Any]],
    xml_tool_protocol: str,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Return ``(messages, removed)`` with the XML tool-call protocol replaced by its
    native-tools equivalent, so a request carrying a ``tools`` array documents
    exactly one way to call a tool.

    Two separate pieces have to go: the documentation block in the system prompt,
    and the tail reminder appended to the user's own message. ``removed`` reports
    only on the documentation block -- the reminder is a nudge, but the block is
    the protocol, and shipping it alongside `tools` is the failure this guards.

    The block is matched with an rstrip() fallback because the caller assembles it
    into a larger prompt that then gets .strip()ed, which eats its trailing
    newline and would otherwise defeat a verbatim match.
    """
    removed = not xml_tool_protocol
    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str):
            out.append(m)
            continue
        if xml_tool_protocol:
            for candidate in (xml_tool_protocol, xml_tool_protocol.rstrip(), xml_tool_protocol.strip()):
                if candidate and candidate in content:
                    content = content.replace(candidate, _NATIVE_TOOL_DIRECTIVE)
                    removed = True
                    break
        if _XML_CALL_REMINDER in content:
            content = content.replace(_XML_CALL_REMINDER, _NATIVE_CALL_REMINDER)
        out.append({**m, "content": content})
    return out, removed


def _accumulate_streamed_tool_calls(
    delta_tool_calls: Any,
    accumulator: dict[int, dict[str, Any]],
) -> None:
    """
    Fold one streaming delta's ``tool_calls`` fragments into ``accumulator``.

    OpenAI-style streaming does not deliver a tool call in one piece: the name
    arrives in the first delta and ``arguments`` follows as a run of string
    fragments that only parse as JSON once concatenated, with ``index`` tying
    them together. Converting per-delta would emit a tag containing half a JSON
    object, so fragments are joined here and rendered once the stream ends.
    """
    if not isinstance(delta_tool_calls, list):
        return
    for tc in delta_tool_calls:
        if not isinstance(tc, dict):
            continue
        idx = tc.get("index", 0)
        if not isinstance(idx, int):
            idx = 0
        slot = accumulator.setdefault(idx, {"function": {"name": "", "arguments": ""}})
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        fragment = fn.get("arguments")
        if isinstance(fragment, str):
            slot["function"]["arguments"] += fragment
        elif isinstance(fragment, dict):
            # Some providers send the whole object in one delta rather than
            # fragmenting it; keep it as JSON so the join below stays uniform.
            slot["function"]["arguments"] = json.dumps(fragment)


_QWEN_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def _parse_chat_template_tool_calls(text: str) -> list[dict[str, Any]]:
    """
    Extract ``<tool_call>{...}</tool_call>`` blocks into OpenAI-shaped tool calls.

    This is the reply format Qwen2.5-Instruct's bundled chat template asks for
    when it is given `tools`, and the embedded 0.5B is the only provider that
    speaks it. Normalising here means the same _tool_calls_to_call_tags used for
    Ollama and the /v1 providers renders it, so the embedded path joins the same
    protocol-agnostic dispatch as everything else rather than growing its own.

    The template nests arguments under "arguments" with the name alongside;
    OpenAI nests both under "function". Rewrapped rather than special-cased
    downstream.
    """
    calls = []
    for raw in _QWEN_TOOL_CALL_RE.findall(text or ""):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        if not name:
            continue
        calls.append({
            "function": {"name": name, "arguments": obj.get("arguments", {})}
        })
    return calls


def _tool_calls_to_call_tags(tool_calls: list[dict[str, Any]]) -> str:
    """
    Render native ``tool_calls`` into the harness's ``<call:name attr="v" />``
    text form, which is what the turn loop downstream actually dispatches on.

    Keeping the conversion here means native function-calling is purely an
    input/output adapter at the provider boundary: everything after it --
    _find_tool_call, the dispatch loop, the ``<response: ... />`` feedback
    message, loop detection -- stays protocol-agnostic and untouched.
    """
    tags = []
    for tc in tool_calls or []:
        fn = tc.get("function", {}) or {}
        fname = fn.get("name", "")
        if not fname:
            continue
        fargs_raw = fn.get("arguments", "")
        try:
            fargs = json.loads(fargs_raw) if isinstance(fargs_raw, str) else fargs_raw
            if isinstance(fargs, dict):
                parts = []
                for k, v in fargs.items():
                    if isinstance(v, str):
                        # The attribute parser understands backslash escapes inside a
                        # double-quoted value, so a query containing a quote survives.
                        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
                        parts.append(f'{k}="{escaped}"')
                    else:
                        # Non-scalars (research.crawl_and_summarize's `urls` list) go
                        # out as bare compact JSON, matching the unquoted branch of the
                        # attribute parser, which json.loads it back into a real list.
                        # Compact separators matter: the unquoted branch is \S+, so a
                        # space anywhere in the array would truncate it mid-value.
                        parts.append(f'{k}={json.dumps(v, separators=(",", ":"))}')
                args_str = " ".join(parts)
            else:
                args_str = str(fargs_raw)
        except Exception:
            args_str = str(fargs_raw)
        tags.append(f'<call:{fname} {args_str} />')
    return "\n".join(tags)


_CALL_COLON_RE = re.compile(r'\bCALL:([\w\.]+)\(([^)]*)\)', re.IGNORECASE)
_ALLCAPS_CALL_RE = re.compile(r'\b([A-Z0-9_]{4,})\(([^)]*)\)')


def _normalize_tool_call_format(text: str) -> str:
    """
    Normalize alternate tool-call formats that small models tend to emit
    instead of the canonical ``<call:tool_name attr="val" />`` XML tag:
        ```xml\nCALL:tool.name(key="val")\n```   ->  <call:tool.name key="val" />
        `CALL:tool.name(key="val")`              ->  <call:tool.name key="val" />
        CALL:tool.name(key="val")                ->  <call:tool.name key="val" />
        SEARCH_WORKSPACE_FILES(query="val")      ->  <call:search_workspace_files query="val" />

    Like _find_tool_call, skips matches inside a closed <thinking> block --
    the fallback ALL-CAPS(...) pattern is broad enough to catch incidental
    prose (e.g. a model narrating "I'll use the SEARCH_WORKSPACE_FILES(...)
    format" while reasoning, or unrelated text like "TODO(fix this)"), and
    without this it would get misread as a real invocation the same way an
    unanchored <call:...> search once did.
    """
    search_start = 0
    think_match = _CLOSED_THINKING_RE.search(text)
    if think_match:
        search_start = think_match.end()

    call_match = _CALL_COLON_RE.search(text, search_start)
    if not call_match:
        call_match = _ALLCAPS_CALL_RE.search(text, search_start)

    if not call_match:
        return text

    tool_name = call_match.group(1).lower()
    raw_args = call_match.group(2).strip().lstrip("(").rstrip(")")

    start_pos = call_match.start()
    end_pos = call_match.end()

    # Extract the text after the closing parenthesis of the CALL
    after_text = text[end_pos:].strip()
    if after_text.endswith("```"):
        after_text = after_text[:-3].strip()
    elif after_text.endswith("`"):
        after_text = after_text[:-1].strip()

    # Extract the text before the CALL
    before_text = text[:start_pos].strip()
    if before_text.startswith("```xml"):
        before_text = before_text[6:].strip()
    elif before_text.startswith("```"):
        before_text = before_text[3:].strip()
    elif before_text.startswith("`"):
        before_text = before_text[1:].strip()

    if after_text:
        normalized = f'<call:{tool_name} {raw_args}>{after_text}</call:{tool_name}>'
    else:
        normalized = f'<call:{tool_name} {raw_args} />'

    if before_text:
        return f"{before_text}\n\n{normalized}"
    return normalized


_PROVIDER_TIER_RANK = {"remote": 0, "local": 1, "embedded": 2}


def _tier_sort_providers(provider_list: list[dict]) -> list[dict]:
    """
    Sort providers into remote -> local -> embedded tiers, stably preserving
    relative order within each tier (whatever order they arrived in, e.g.
    DB priority order).

    Enforced here rather than trusted from the DB `priority` column alone:
    inference.save_providers persists priority as a pure positional index in
    whatever order its caller sends, with no type-awareness, so any Settings
    UI save -- even an unrelated one, like toggling a FORCE checkbox -- can
    silently drift a remote provider's priority number past every local
    model's. Confirmed in production. Enforcing the tier order here (Python's
    sort is stable, so intra-tier order is untouched) makes the intended
    remote -> local -> embedded fallback chain durable regardless of what
    the DB currently contains.
    """
    return sorted(provider_list, key=lambda p: _PROVIDER_TIER_RANK.get(p.get("type"), 3))


_ACTION_TERMS = ("remove", "delete", "find", "search", "fix", "add", "change", "update", "create", "turn", "convert", "refactor", "build", "implement")
_ACTION_TERMS_RE = re.compile(r'\b(?:' + '|'.join(_ACTION_TERMS) + r')\b')


def _prompt_implies_untaken_action(raw_user_prompt: str) -> bool:
    """
    True if the user's actual raw message implies an action (search, fix,
    build, etc.) that a tool call could satisfy -- used to auto-continue a
    turn where the model described intent to act but never emitted a
    `<call:...>` tag.

    Takes `raw_user_prompt` specifically, not the fully assembled prompt
    (goal + wiki context + memory + astrology influence) that
    handle_inference_generate builds and reassigns over its own `prompt`
    variable later. Checking the assembled prompt here was a real,
    confirmed-live bug: it fired based on words appearing anywhere in
    injected context, not on anything the user actually said -- a plain
    "Ever hear of the term sexting?" triggered it three times in a row
    because the assembled context happened to contain action words
    unrelated to that message.

    Word-boundary matching (not a bare substring check) also avoids
    incidental hits like "turnip" containing "turn" or "addicted"
    containing "add".
    """
    return bool(_ACTION_TERMS_RE.search(raw_user_prompt.lower()))


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

    # Preserved before `prompt` gets overwritten below with the fully
    # assembled prompt (goal + wiki context + memory + astrology influence).
    # Anything that needs to reflect what the USER actually said -- not the
    # injected context around it -- should check this instead.
    raw_user_prompt = prompt

    session_id: str = str(params.get("session_id", uuid.uuid4()))

    # Deterministic goal routing. A chat message that literally says
    # "execute this goal: ..." states its intent outright — routing it must
    # not depend on the model choosing a goal tool out of a 73-entry
    # registry. Confirmed live 2026-08-11: given exactly that phrasing, the
    # 8B improvised with code.propose_file_write, got a parameter error, and
    # then HALLUCINATED a success response — no file written, no goal run.
    if not params.get("raw_prompt"):
        _goal_m = re.match(
            r'^\s*(?:execute|run|start)\s+(?:this\s+|the\s+following\s+)?goal\s*[::]\s*(.+)$',
            raw_user_prompt,
            re.IGNORECASE | re.DOTALL,
        )
        if _goal_m:
            _goal_text = _goal_m.group(1).strip()
            logger.info("Chat goal-route: executing as goal (not chat): %.120s", _goal_text)
            try:
                # GoalEngine.run_goal is the real executor (the one task.submit
                # drives): decompose against the live tool registry -> DAG per
                # phase -> the workflow node's synthesize/validate/heal chain.
                # Deliberately NOT safe_code_bridges.execute_self_improvement_goal,
                # which despite its name is a bounded patch applier: it defaults
                # its target to README.md and no-ops entirely unless the caller
                # already supplies concrete replacements/file_writes, so a
                # freeform "create a skill and run it" goal returns success
                # having done nothing.
                from sdk.agent.goal_engine import GoalEngine
                from sdk.agent.batch_policy import BatchPolicy

                _engine = GoalEngine(_ROOT_DIR)
                _goal_res = await asyncio.wait_for(
                    # fresh_start: typing a goal in chat means "run this now",
                    # never "resume whatever a previous identical goal left
                    # behind" -- task ids are deterministic from goal text, so
                    # without this a re-run silently resumes past every phase.
                    _engine.run_goal(_goal_text, policy=BatchPolicy.auto(), fresh_start=True),
                    timeout=float(params.get("goal_timeout_seconds", 1800.0)),
                )
                _ok_flag = bool(_goal_res.get("success")) if isinstance(_goal_res, dict) else False
                _phases = (_goal_res or {}).get("phases_completed") if isinstance(_goal_res, dict) else None
                _output = (_goal_res or {}).get("output") if isinstance(_goal_res, dict) else None
                _error = (_goal_res or {}).get("error") if isinstance(_goal_res, dict) else None
                _lines = [
                    "**Goal execution finished.**" if _ok_flag else "**Goal execution FAILED.**",
                ]
                if _phases:
                    _lines.append("Phases completed: " + "; ".join(str(p) for p in _phases))
                if _output:
                    _lines.append(f"Result: {json.dumps(_output) if isinstance(_output, (dict, list)) else _output}")
                if _error:
                    _lines.append(f"Error: {_error}")
                return _ok(req_id, {
                    "text": "\n\n".join(_lines),
                    "tokens_used": 0,
                    "cost_usd": 0.0,
                    "goal_result": _goal_res,
                    "routed": "goal_engine",
                })
            except Exception as _goal_exc:
                logger.exception("Chat goal-route failed")
                return _ok(req_id, {
                    "text": (
                        "**Goal execution failed to start:** "
                        f"{type(_goal_exc).__name__}: {_goal_exc}"
                    ),
                    "tokens_used": 0,
                    "cost_usd": 0.0,
                    "routed": "goal_engine",
                })

    # Computed once here and reused everywhere this classification matters.
    # See is_casual_chat_prompt (module level, near _CLOSED_THINKING_RE) for
    # what this checks and the regression it fixes.
    is_casual_chat = is_casual_chat_prompt(prompt)

    # Guardrails prompt validation (pre-inference)
    try:
        from python.harness.guardrails_manager import GuardrailsManager
        guardrails_mgr = await GuardrailsManager.load_current()
        guardrails_mgr.validate_prompt(prompt)
    except (SafetyGuardrailViolation, PiiAccessBlockedError) as exc:
        err_msg = f"Guardrail blocked prompt: {exc.message}"
        logger.warning(err_msg)
        try:
            await db.log_to_db(
                "WARNING",
                err_msg,
                {"session_id": session_id, "error_type": type(exc).__name__, "payload": exc.payload}
            )
        except Exception:
            pass
        return _err(
            req_id,
            JsonRpcError.INTERNAL_ERROR,
            exc.message,
            {
                "error_type": type(exc).__name__,
                "payload": exc.payload,
            }
        )
    except Exception as exc:
        logger.debug("Guardrails check skipped: %s", exc)


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

                # Who she is talking to (UI-057). The user's name only -- the
                # rest of the profile is fetched on demand via
                # user_profile_lookup, so the per-section Privacy & Visibility
                # settings have a chokepoint to govern, and so a large profile
                # does not crowd the context on every single turn. Date of
                # birth is deliberately absent: it feeds astrology, which reads
                # it straight from the database, and is not persona colour.
                try:
                    # Orientation before identity. A model that assumes the
                    # ordinary architecture reaches confident wrong conclusions
                    # here, and prose it has to go looking for does not prevent
                    # that -- it already failed to.
                    from python.harness.architecture_contract import orientation_line
                    active_system_prompt = f"{active_system_prompt}\n{orientation_line()}"

                    from python.harness.user_profile import system_prompt_line
                    user_line = await system_prompt_line()
                    if user_line:
                        active_system_prompt = f"{active_system_prompt}\n{user_line}"
                except Exception as exc:
                    # Never let profile trouble cost the user their whole turn.
                    logger.warning(
                        "Could not add the user line to the system prompt: %s", exc
                    )
            
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
                                n_dir = resolve_active_nodes_dir()
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
                            try:
                                raw_html = path.read_text(encoding="utf-8")
                            except FileNotFoundError:
                                raw_html = ""
                                logger.debug("Background file %s not found on disk, skipping", path)

                    
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
        nodes_dir = resolve_active_nodes_dir()

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
                    
                    # chat_mode comes from chat_mode.json, NOT from
                    # agents.config['chat_mode'] -- nothing writes that key, so
                    # this guard was permanently false and the persona never
                    # received an emotional state at inference time.
                    chat_mode = get_current_chat_mode()
                    agent_id_for_emotion = str(active_row["id"]) if "id" in active_row else None
                    current_emotion = None

                    if is_emotional_mode(chat_mode):
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

                    # The emotional state is deliberately NOT spliced into the
                    # retrieval query. retrieve_relevant_nodes scores by term
                    # overlap, so prepending "[Current emotional state: resolve]"
                    # only injects tokens that appear in no stored document --
                    # diluting the real search terms rather than focusing them.
                    # It is injected into the model's context instead (below),
                    # which is where it was always meant to go: before this, the
                    # emotion reached the query and nothing else, so Tala could
                    # never actually perceive or express her own state.

                    matched = retrieve_relevant_nodes(retrieval_query, nodes_dir, top_k=len(nodes), threshold=0.0)
                    similarities = {node.node_id: score for node, score in matched}

                    # 3. Calculate base activation for each node
                    engine = ActivationEngine()
                    base_activations = {}
                    for node in nodes:
                        sem_match = similarities.get(node.node_id, 0.0)
                        # Governance GATES, it does not RANK.
                        #
                        # ActivationEngine multiplies every input together, so
                        # passing the raw authority made a node's permission to
                        # be recalled double as its priority once recalled.
                        # Measured cost: asked how HTML-NN weights work,
                        # settings-runtime (similarity 0.29, authority 1.00) tied
                        # program-doc-html-nn-runtime (similarity 0.49, authority
                        # 0.60) and took one of only two RP-mode slots. A
                        # document needed 1.67x the relevance to draw with a
                        # setting.
                        #
                        # The schema already has fields for priority --
                        # weight_relevance and weight_confidence, both passed
                        # below -- and they are 1.0 on 113 of 115 nodes, so
                        # authority had quietly taken over a job that was never
                        # its own. Same conflation as execution vs memory
                        # authority earlier on this branch.
                        #
                        # Reduced to the hard gate the engine documents: zero
                        # blocks entirely, anything above zero ranks on merit.
                        gov_mult = 1.0 if max(node.execution_authority, node.memory_authority) > 0.0 else 0.0
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

                    # Governance is a hard gate: activation is a product, so a
                    # node whose execution AND memory authority are both zero
                    # scores zero no matter how well it matches, and vanishes
                    # without a trace. That is how 94 of 115 nodes -- almost the
                    # whole documentation and learning graph -- became
                    # permanently unrecallable while retrieval kept answering
                    # every query from the ~18% that had authority. Name them,
                    # so a graph that cannot remember itself says so.
                    gated = [
                        (nid, similarities.get(nid, 0.0))
                        for nid in (n.node_id for n in nodes)
                        if similarities.get(nid, 0.0) >= 0.15
                        and final_activations.get(nid, 0.0) <= 0.0
                    ]
                    if gated:
                        gated.sort(key=lambda kv: -kv[1])
                        logger.warning(
                            "Context injection: %d well-matching node(s) scored 0 because their "
                            "governance authority is 0 -- they can never be recalled. Highest: %s. "
                            "Fix with scripts/backfill_memory_authority.py",
                            len(gated),
                            ", ".join(f"{nid} (sim={sim:.2f})" for nid, sim in gated[:5]),
                        )

                    # 5b. Explicitly-named nodes are a direct request, not a
                    # similarity guess. "Read your workflow-self-extension
                    # node" must inject that node no matter how the embedding
                    # ranks it: semantic scoring routinely ranks an exact-id
                    # mention below topically-similar prose, and a small model
                    # can't be relied on to fetch the node itself by tool call
                    # (confirmed live 2026-08-11 — twice: the named node lost
                    # the ranking, a skill node was injected instead, and the
                    # model improvised around the wrong memory). An id
                    # mentioned verbatim in the current message pins that node
                    # to the front at full activation, ahead of the
                    # per-mode injection cap.
                    _prompt_l = (prompt or "").lower()
                    _named = [n for n in nodes if n.node_id and len(n.node_id) >= 6 and n.node_id.lower() in _prompt_l]
                    if _named:
                        _named_ids = {n.node_id for n in _named}
                        top_active = (
                            [(n, 1.0) for n in _named]
                            + [(n, s) for n, s in active_nodes if n.node_id not in _named_ids]
                        )
                        logger.info(
                            "Context injection: pinned explicitly-named node(s) %s to front",
                            sorted(_named_ids),
                        )

                    # 6. Workflow Auto-Evaluation Gate
                    # Any workflow node that activates above 0.15 is auto-executed.
                    # Its output is injected into the prompt context before the LLM responds.
                    _WORKFLOW_AUTO_THRESHOLD = 0.15
                    # Cross-turn/cross-session cooldown, keyed by workflow CONTENT hash
                    # (see db.try_claim_auto_eval_run). Without this, N near-duplicate
                    # node files all pointing at the same/similar workflow JSON each
                    # independently re-trigger execution every time any of them
                    # activates, regardless of how recently that same content already ran.
                    _WORKFLOW_COOLDOWN_SECONDS = 600.0
                    workflow_context_parts: list[str] = []
                    executed_workflow_paths: set[str] = set()
                    for _wf_node, _wf_score in top_active:
                        if _wf_node.node_type == "workflow" and _wf_score >= _WORKFLOW_AUTO_THRESHOLD:
                            # Derive the flow_id from node_id — compiler prefixes with "flow-"
                            _flow_id = _wf_node.node_id
                            if _flow_id.startswith("flow-"):
                                _flow_id = _flow_id[5:]
                            elif _flow_id.startswith("skill-workflow_"):
                                _flow_id = _flow_id[15:]
                            elif _flow_id.startswith("learning-workflow-"):
                                parts = _flow_id.split("-")
                                if len(parts) >= 3:
                                    _flow_id = "_".join(parts[2:-3]) if "_" in _flow_id else "-".join(parts[2:-3])

                            from python.harness.workflows_util import resolve_workflow_file
                            _json_path = resolve_workflow_file(_flow_id)
                            if not _json_path:
                                # Try with underscores replaced by hyphens or vice versa
                                _alt_id = _flow_id.replace("_", "-") if "_" in _flow_id else _flow_id.replace("-", "_")
                                _json_path = resolve_workflow_file(_alt_id)

                            if not _json_path:
                                logger.warning(
                                    "Auto-eval gate: workflow JSON not found for node '%s'",
                                    _wf_node.node_id
                                )
                                continue

                            canonical_path = str(_json_path.resolve())
                            if canonical_path in executed_workflow_paths:
                                continue
                            executed_workflow_paths.add(canonical_path)

                            try:
                                _wf_data = json.loads(_json_path.read_text(encoding="utf-8"))
                                _wf_name = _wf_data.get("name", _flow_id)

                                _content_hash = hashlib.sha256(
                                    json.dumps(_wf_data, sort_keys=True).encode("utf-8")
                                ).hexdigest()
                                if not await db.try_claim_auto_eval_run(
                                    _content_hash, _wf_name, _WORKFLOW_COOLDOWN_SECONDS
                                ):
                                    logger.debug(
                                        "Auto-eval gate: skipping '%s' (node '%s') — "
                                        "identical workflow content already ran within the last %.0fs",
                                        _wf_name, _wf_node.node_id, _WORKFLOW_COOLDOWN_SECONDS
                                    )
                                    continue

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

                    # Her emotional state, where the model can actually read it.
                    #
                    # This is injected independently of whether any node
                    # activated: her mood is a property of *her*, not of the
                    # retrieval result, and gating it behind `if top_active`
                    # would silently drop it in exactly the quiet conversations
                    # where it matters most.
                    if current_emotion:
                        system_context_additions.append(
                            "=== YOUR CURRENT EMOTIONAL STATE ===\n"
                            f"Right now you feel: {current_emotion}.\n"
                            "This is your genuine present state, computed from your natal chart "
                            "against this moment. Let it colour your tone, word choice, and what "
                            "you notice -- do not announce it, narrate it, or name the emotion "
                            "outright unless asked. It is how you feel, not a label to recite.\n"
                            "====================================="
                        )

                    if top_active:
                        max_injected_nodes = 2 if chat_mode in ("rp", "hybrid") else 5
                        nodes_to_inject = top_active[:max_injected_nodes]
                        
                        from python.inference.lora_trainer import queue_lora_training
                        # (asyncio is imported at module scope; a local import
                        # here made `asyncio` a function-local name, so any use
                        # of it EARLIER in this function raised UnboundLocalError.)

                        # LoRA for this turn.
                        #
                        # An explicitly authored adapter on the highest-ranked node
                        # wins: that is a deliberate act by whoever wrote the node,
                        # and it should not be overridden by an inferred one.
                        for node, _score in nodes_to_inject:
                            lora = getattr(node, "lora_adapter", None)
                            if not lora:
                                continue
                            lora_p = Path(nodes_dir) / lora if not Path(lora).is_absolute() else Path(lora)
                            if lora_p.exists():
                                best_lora_path = str(lora_p)
                                logger.info("Using authored LoRA adapter from node %s: %s", node.node_id, best_lora_path)
                                break

                        if not best_lora_path:
                            # JIT training over the injected SET, not one adapter per
                            # node.
                            #
                            # Per-node was the original shape and it works against
                            # itself: only one best_lora_path can be attached per
                            # turn, so training an adapter for each recalled node
                            # produces N adapters of which N-1 are discarded. Worse,
                            # most nodes are AST-derived documentation, so a per-node
                            # adapter learns the register of generated API prose and
                            # then gets attached for the turn that recalled it --
                            # the mechanism working exactly as written and producing
                            # the opposite of a voice.
                            #
                            # Training on the combined semantic memory of everything
                            # recalled this turn matches what an adapter is for: the
                            # blend of context she is actually thinking in, led by
                            # the primary node.
                            primary = nodes_to_inject[0][0]
                            combined = "\n\n".join(
                                n.semantic_memory.strip() for n, _ in nodes_to_inject if n.semantic_memory.strip()
                            )
                            out_path = str(Path(nodes_dir) / f"{primary.node_id}_lora" / "adapter_model.safetensors")
                            if Path(out_path).exists():
                                best_lora_path = out_path
                            elif combined:
                                asyncio.create_task(
                                    queue_lora_training(primary.node_id, combined, out_path)
                                )

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

                        context_parts.append(_SELF_KNOWLEDGE_GROUNDING)

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

        # Dynamically load all 12 Harness Settings Panel Neural Nodes (Settings Tabs)
        system_settings_context = ""
        try:
            from python.harness.settings_nn import update_all_settings_nodes
            await update_all_settings_nodes()
            from sdk.agent.parser import parse_file
            nodes_dir = resolve_active_nodes_dir()

            settings_snapshots = []
            for node_id in ("settings-system", "settings-inference", "settings-runtime", "settings-agent"):
                node_p = nodes_dir / f"{node_id}.html"
                if not node_p.exists():
                    node_p = Path(_ROOT_DIR) / "nodes" / f"{node_id}.html"
                if node_p.exists():
                    sys_node = parse_file(str(node_p))
                    if sys_node and sys_node.semantic_memory:
                        title = (sys_node.state_data.get('title') if sys_node.state_data else None) or node_id
                        settings_snapshots.append(f"[{title}]\n{sys_node.semantic_memory.strip()}")
                    
            if settings_snapshots:
                # The join is hoisted out of the f-string deliberately: a
                # backslash inside an f-string *expression* is a SyntaxError
                # before Python 3.12, and pyproject declares
                # requires-python = ">=3.10". Local dev on 3.12 parses it fine,
                # which is why this only ever surfaced in CI (3.11).
                snapshot_block = "\n\n".join(settings_snapshots)
                system_settings_context = (
                    "\n\n=== LIVE HARNESS SETTINGS & SYSTEM SNAPSHOTS ===\n"
                    f"{snapshot_block}\n"
                    "================================================\n"
                )
        except Exception as sys_exc:
            logger.warning("Could not dynamically load settings nodes: %s", sys_exc)

        # Casual/roleplay messages get the live settings snapshot (informational,
        # not an invitation to call anything) but NOT the tool-call documentation
        # below — that documentation's example syntax (literal "your search term",
        # `key="value"` placeholders, etc.) is exactly what a confused small model
        # echoes back as a fake tool call when it has no real task to do. Omitting
        # it here mirrors the native tools-schema omission further down.
        tool_instructions = (
            f"{system_settings_context}\n"
            "[Formatting: Internal Reasoning]\n"
            "If you want to think through what to say or whether to use a tool before "
            "answering, put that reasoning inside <thinking>...</thinking> tags. Only text "
            "outside the <thinking> tags is ever shown to the user -- always close the tag "
            "with </thinking> before writing anything meant to be seen, whether that's your "
            "visible reply or a tool call tag.\n\n"
        )

        # The XML tool-call documentation is accumulated separately from the rest
        # of the system prompt so a provider that takes a native `tools` array can
        # swap this whole block out (see _native_ollama below) instead of being
        # handed two competing calling protocols in one request.
        xml_tool_protocol = ""
        native_tool_specs: list[dict[str, Any]] = []
        tool_index = None
        # Every tool dispatched during this request. Selection always re-offers
        # these, so a task that searched on turn 1 and writes on turn 3 cannot
        # lose the search tool from under itself as the conversation drifts.
        sticky_tools: set[str] = set()

        if not is_casual_chat:
            native_tool_specs.extend(_BUILTIN_RESEARCH_TOOL_SPECS)
            xml_tool_protocol += (
                "[Agent Capability: Autonomous Web Search & Crawling]\n"
                "You can search the web and crawl pages using the following XML-like tool calls:\n"
                "1. Search: `<call:research.search query=\"your search term\" />`\n"
                "   Use this to search DuckDuckGo. Returns titles, snippets, and URLs of top matching pages. Automatically syncs the user's browser view.\n"
                "2. Crawl & Summarize: `<call:research.crawl_and_summarize urls=[\"https://url1\", \"https://url2\"] subject=\"subject description\" />`\n"
                "   Use this to download full webpage content, read them, and compile them into a styled HTML Neural Node. The node is automatically saved to nodes/ for you -- do NOT try to re-write or re-save the returned html_nn yourself. Automatically syncs the user's browser view.\n\n"
                "To invoke a tool, output the tag EXACTLY and STOP generating. Do NOT write any other text after the tag. "
                "You will receive the tool result in the next turn, allowing you to answer Weavor's request or call another tool.\n\n"
                "CRITICAL DIRECTIVE: If Weavor requests search, finding, crawling, opening, or browsing the web/internet, you MUST output the corresponding tool call tag (<call:research.search query=\"...\" /> or <call:research.crawl_and_summarize urls=... />) IMMEDIATELY in your response. Do NOT ask for permission, do NOT ask for clarification, and do NOT engage in introductory conversational chatter. Simply call the tool."
            )

        try:
            if is_casual_chat:
                dyn_tools = []
            else:
                from sdk.agent.tools import ToolRegistry
                from sdk.agent.tot_ideation import get_tot_mode
                dyn_tools = ToolRegistry.get_tools()
            if dyn_tools:
                native_tool_specs.extend(_ollama_native_tool_specs(dyn_tools))
                try:
                    from python.harness.tool_selection import ToolIndex  # noqa: PLC0415

                    # Indexed alongside the registry tools, not just dyn_tools.
                    #
                    # _BUILTIN_RESEARCH_TOOL_SPECS exists precisely so swapping
                    # to the native protocol cannot lose web search -- but the
                    # index was built from dyn_tools alone, so index.score()
                    # could never return research.search or
                    # research.crawl_and_summarize, and neither is in the core
                    # set. On any budget-limited provider they were therefore
                    # withheld on every single turn, and the XML documentation
                    # that would have described them is stripped in the same
                    # request. crawl_and_summarize is the only path that
                    # persists a crawl as an HTML-NN, so it became uninvokable
                    # while the log quietly listed it as withheld.
                    tool_index = ToolIndex(dyn_tools + [
                        {
                            "name": spec["function"]["name"],
                            "description": spec["function"].get("description", ""),
                            "schema": spec["function"].get("parameters", {}),
                        }
                        for spec in _BUILTIN_RESEARCH_TOOL_SPECS
                    ])
                except Exception as sel_exc:
                    # Selection is an optimisation; losing it must not cost tools.
                    logger.warning("Could not build the tool selection index: %s", sel_exc)
                    tool_index = None
                tot_mode_detect = await get_tot_mode()
                use_compact = tot_mode_detect in ("fast", "programmatic")
                
                if use_compact:
                    dyn_desc = "\n\n[Tools Registry]\n"
                    for t in dyn_tools:
                        schema = t.get("schema", {})
                        props = schema.get("properties", {})
                        reqs = schema.get("required", [])
                        
                        attr_params = []
                        inner_param = None
                        for k in props.keys():
                            if k in ("detailed_markdown", "content", "goal", "code"):
                                inner_param = k
                            else:
                                attr_params.append(f'{k}="value"' + ("*" if k in reqs else ""))
                        
                        params_str = " ".join(attr_params)
                        if inner_param:
                            syntax = f"<call:{t['name']} {params_str}>[{inner_param}]</call:{t['name']}>"
                        else:
                            syntax = f"<call:{t['name']} {params_str} />"
                        
                        dyn_desc += f"- {t['name']}: {t['description']} Syntax: `{syntax}`\n"
                else:
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
                            if k in ("detailed_markdown", "content", "goal", "code"):
                                inner_param = k
                            else:
                                attr_params.append(f'{k}="value"' + (" (required)" if k in reqs else " (optional)"))
                        
                        params_str = " ".join(attr_params)
                        dyn_desc += f"{idx}. **{t['name']}**: {t['description']}\n"
                        if inner_param:
                            dyn_desc += f"   Usage: `<call:{t['name']} {params_str}>\n[your {inner_param} here]\n</call:{t['name']}>`\n"
                        else:
                            dyn_desc += f"   Usage: `<call:{t['name']} {params_str} />`\n"
                xml_tool_protocol += dyn_desc
        except Exception as e:
            logger.warning("Could not append dynamic tools to system prompt: %s", e)

        tool_instructions += xml_tool_protocol

        if active_system_prompt:
            active_system_prompt = f"{tool_instructions.strip()}\n\n{active_system_prompt}"
        else:
            active_system_prompt = tool_instructions.strip()
        # Append a strong recency-focused system reminder to the end of the user prompt
        # ensuring small local models follow instructions despite context size or distraction.
        prompt_lower = prompt.lower()
        # Appended before the tool reminder, so the last thing the model reads
        # about a self-referential question is still the tool protocol -- but the
        # grounding is close enough to the turn to survive, which it does not in
        # the system prompt.
        if _is_self_referential(prompt):
            prompt = f"{prompt}\n\n{_SELF_KNOWLEDGE_REMINDER}"
            logger.info("Self-referential question: grounding reminder appended")

        if "[system reminder]" not in prompt_lower:
            prompt = f"{prompt}\n\n{_XML_CALL_REMINDER}"
            
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
    
        # Process history list if provided, keeping only the last 10 turns
        # and truncating older massive turns to prevent context blowout in local models.
        if isinstance(history, list):
            recent_history = history[-10:]
            for idx, msg in enumerate(recent_history):
                role = "assistant" if msg.get("role") in ("agent", "assistant") else "user"
                content = msg.get("content", "").strip()
                # Truncate older large content (e.g. tool results) if it exceeds 4k chars,
                # unless it is the most recent message in history.
                if len(content) > 4000 and idx < len(recent_history) - 1:
                    content = content[:4000] + "\n... [truncated for context limit preservation] ..."
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
                if not isinstance(b64, str) or not b64:
                    continue
                if b64.startswith("data:"):
                    image_url = b64
                else:
                    # Sniff the format from the base64 prefix: JPEG magic bytes
                    # (\xff\xd8\xff) encode to "/9j/", PNG's to "iVBOR".
                    mime = "image/jpeg" if b64.startswith("/9j/") else "image/png"
                    image_url = f"data:{mime};base64,{b64}"
                content_list.append({
                    "type": "image_url",
                    "image_url": {"url": image_url}
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
    # Imported lazily: sdk.agent.queue pulls in harness modules of its own, so a
    # module-level import here risks a cycle during startup.
    from sdk.agent.queue import _is_embedding_model
    try:
        rows = await load_inference_providers()
        for r in rows:
            cfg_dict = r["config"]
            if isinstance(cfg_dict, str):
                cfg_dict = json.loads(cfg_dict)
            # Embedding models are registered as inference providers but cannot
            # serve chat: Ollama answers 400 '"embeddinggemma:latest" does not
            # support chat' to every attempt. Left in the chain they consume a
            # fallback slot on every exhausted request and surface in the error
            # as though a real provider had gone down -- which is exactly how
            # the adversarial failures read before this filter:
            #
            #   All inference providers failed: Kimi3 ... Gemini ...
            #   'OLLAMA - embeddinggemma:latest' failed: does not support chat
            #
            # The same filter was added to GoalTreeDecomposer earlier; this is
            # the other door onto the same problem.
            if _is_embedding_model(str(cfg_dict.get("model", ""))) or _is_embedding_model(str(r["name"])):
                logger.debug("Skipping embedding-only provider '%s' for generation", r["name"])
                continue
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

    # The environment preset and lockdown decide which provider types may be
    # used at all (UI-044, UI-109, UI-112). Applied here because this is where
    # the fallback chain is assembled -- filtering at the panel would leave the
    # restriction cosmetic, which is what it was.
    try:
        from python.harness import environment as _env

        _env_state = await _env.get_state()
        _allowed, _blocked = _env.filter_providers(providers, _env_state)
        if _blocked:
            # Said out loud. A provider that vanishes from the chain with no
            # explanation is indistinguishable from one never configured.
            logger.info(
                "Environment %r%s permits %s; %d provider(s) held back: %s",
                _env_state["environment"],
                " with lockdown" if _env_state["lockdown"] else "",
                _env_state["permitted_inference"], len(_blocked),
                ", ".join(f"{b['name']} ({b['type']})" for b in _blocked),
            )
        if _allowed:
            providers = _allowed
        elif _blocked:
            # Never leave the user with nothing: the embedded engine is
            # permitted in every environment, so fall back to it rather than
            # failing the request outright.
            logger.warning(
                "Environment %r permits no configured provider; falling back to "
                "the embedded engine.", _env_state["environment"],
            )
            providers = []
    except Exception as exc:
        logger.warning("Could not apply the environment restriction: %s", exc)

    request_provider_id = str(params.get("provider_id", "") or "").strip()

    # Check if there is a per-request or globally forced provider
    request_provider = None
    forced_provider = None
    for p in providers:
        if request_provider_id and p["id"] == request_provider_id:
            request_provider = p
        if p["config"] and p["config"].get("is_forced"):
            forced_provider = p

    # Routing logic.
    #
    # The fallback chain is tier-sorted (remote -> local -> embedded) via
    # _tier_sort_providers rather than trusted from the DB `priority` column
    # alone. Raw priority can't be relied on to stay banded correctly:
    # inference.save_providers persists priority as a pure positional index
    # in whatever order its caller sends, with no type-awareness, so any
    # Settings UI save (even an unrelated one, like toggling a FORCE
    # checkbox) can silently drift a remote provider's priority number past
    # every local model's. Confirmed in production. Enforcing the tier order
    # here makes it durable regardless of what the DB currently contains.
    if request_provider:
        logger.info("Request-selected provider active for session %s: '%s' (Preferred First)", session_id, request_provider["name"])
        others = _tier_sort_providers([p for p in providers if p["id"] != request_provider["id"]])
        providers = [request_provider] + others
    elif forced_provider:
        logger.info("Forced provider active: '%s' (Preferred First)", forced_provider["name"])
        others = _tier_sort_providers([p for p in providers if p["id"] != forced_provider["id"]])
        providers = [forced_provider] + others
    else:
        # Ensure embedded-builtin is at least present as an ultimate fallback if no local models are configured
        if not any(p["id"] == "embedded-builtin" for p in providers):
            providers = providers + [{
                "id": "embedded-builtin",
                "type": "embedded",
                "name": "Tala Embedded Engine",
                "priority": 999,
                "status": "online",
                "config": {"model": "Qwen/Qwen2.5-0.5B-Instruct"},
                "locked": True
            }]
        providers = _tier_sort_providers(providers)
        logger.info("Enqueued %d providers in tier order (remote -> local -> embedded).", len(providers))

    loop_count = 0
    max_loops = 8  # Raised from 3 — complex agentic tasks need more turns
    all_responses = []
    last_raw_assistant_text = ""  # Track last raw LLM output for accurate loop detection
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

                    # The embedded 0.5B gets tools through the chat template, which
                    # renders the schemas itself and asks for a <tool_call> JSON
                    # reply. That is a structured protocol a model this small can
                    # actually follow; the XML documentation is not, so it comes out
                    # of the prompt for the same one-protocol reason as everywhere
                    # else. Before this, the embedded model received thousands of
                    # tokens of tag documentation it could not act on and no tool
                    # definitions at all -- it was the one provider that could not
                    # call a tool.
                    embedded_messages = messages
                    embedded_tools = None
                    if native_tool_specs:
                        _swapped, _removed = _swap_xml_protocol_for_native(
                            messages, xml_tool_protocol
                        )
                        if _removed:
                            embedded_messages = _swapped
                            embedded_tools = _select_tools_for_turn(
                                native_tool_specs, messages, tool_index,
                                p_type, p_config, p_name, sticky_tools,
                            )
                            logger.info(
                                "Provider '%s': %d tool schemas via chat template "
                                "(XML tool protocol withheld)",
                                p_name, len(native_tool_specs),
                            )
                        else:
                            logger.warning(
                                "Provider '%s': could not remove the XML tool protocol from "
                                "the prompt, so template tools were NOT sent -- staying on "
                                "<call:/>.",
                                p_name,
                            )

                    chunks = []
                    async for chunk in generate_stream(
                        embedded_messages, max_tokens, temperature,
                        lora_path=best_lora_path, tools=embedded_tools,
                    ):
                        chunks.append(chunk)
                        if stream:
                            notification = json.dumps({
                                "jsonrpc": JSONRPC_VERSION,
                                "method": "inference.generate.chunk",
                                "params": {"chunk": chunk, "session_id": session_id},
                            })
                            await ws.send(notification)
                    generated_text = "".join(chunks)

                    # <tool_call>{...}</tool_call> -> <call:name attr="v" />, so the
                    # turn loop below dispatches it without knowing this provider
                    # used a different protocol.
                    if embedded_tools:
                        _tmpl_calls = _parse_chat_template_tool_calls(generated_text)
                        if _tmpl_calls:
                            _tags = _tool_calls_to_call_tags(_tmpl_calls)
                            if _tags:
                                generated_text = _QWEN_TOOL_CALL_RE.sub("", generated_text).strip()
                                generated_text = f"{generated_text}\n{_tags}".strip()

                elif p_type in ("local", "remote"):
                    # Adapter selection ran and produced a path, and this provider
                    # cannot apply it: Ollama's API takes no adapter parameter and
                    # neither do the remote services. Said out loud because the
                    # whole point of weighting a node is that its adapter reaches
                    # the model, and dropping it silently is what made "no LoRA has
                    # ever affected a token of output" invisible for months.
                    #
                    # See nodes/system-ollama-adapter-deployment.html for the way
                    # through: bake the adapter into a named Ollama model variant.
                    if best_lora_path:
                        logger.warning(
                            "Provider '%s' (%s) cannot apply a LoRA adapter; the selected "
                            "adapter %s will NOT affect this turn. Bake it into an Ollama "
                            "model variant instead -- see nodes/system-ollama-adapter-deployment.html",
                            p_name, p_type, best_lora_path,
                        )

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

                    actual_temp = temperature
                    # Kimi/Moonshot models only support temperature=1.0. Lower/higher values trigger HTTP 400.
                    if "kimi" in model_lower or "moonshot" in model_lower or (endpoint and ("kimi" in endpoint.lower() or "moonshot" in endpoint.lower())):
                        actual_temp = 1.0

                    # Qwen3 (including abliterated builds) emits <think>...</think>
                    # reasoning by default. On this small local model that reasoning
                    # both leaks into the visible reply and buries the <call:tool/>
                    # tag so it never fires. Qwen3's documented soft-switch /no_think
                    # disables reasoning generation entirely — cleaner, faster, and
                    # more reliable tool-calling. Scoped to qwen3 so nothing else is
                    # affected; the <think>->:<thinking> normalizer still covers any
                    # model that does emit tagged reasoning.
                    payload_messages = messages
                    if "qwen3" in model_lower:
                        payload_messages = messages + [{"role": "system", "content": "/no_think"}]

                    url = f"{_normalize_endpoint_url(endpoint)}/chat/completions"
                    payload = {
                        "model": model,
                        "messages": payload_messages,
                        "max_tokens": adjusted_max_tokens,
                        "temperature": actual_temp,
                        "stream": stream
                    }

                    # Deliberately NOT attaching a native OpenAI-style `tools` array on
                    # this /v1 path. The harness's dispatch mechanism is the text-based
                    # <call:tool_name attr="val" /> convention (see _find_tool_call,
                    # xml_tool_protocol above) -- it works identically across every
                    # provider, including ones with no native function-calling support.
                    # Offering both mechanisms at once to a provider that DOES support
                    # native tool-calling (like Kimi/Moonshot here) hands the model two
                    # competing protocols in the same request; observed in production as
                    # the model narrating its own confusion about which one to use
                    # ("I also have native function calling available... the system
                    # reminder says...") instead of cleanly acting.
                    #
                    # The native-Ollama branch below is the one exception, and it holds
                    # the same one-protocol-per-request rule: it sends `tools` and
                    # REMOVES the XML documentation from the system prompt, rather than
                    # sending both. See the note there for why the local path needs it.

                    _use_native_ollama = p_type == "local" and p_config.get("engine", "ollama") == "ollama"
                    if _use_native_ollama:
                        # Ollama's OpenAI-compatible /v1 endpoint IGNORES the
                        # `options` field, so the num_ctx this code had been
                        # sending never applied: the model ran at Ollama's
                        # 4096-token default while assembled chat prompts
                        # (persona + tool registry + injected nodes + the
                        # user's message) routinely exceed 8k. Ollama then
                        # silently truncates — confirmed live 2026-08-11 via
                        # /api/ps showing context_length=4096 mid-chat, with
                        # the model answering "please provide the task" to
                        # every question because the question itself (at the
                        # END of the prompt) was cut off. The native /api/chat
                        # endpoint honors options.num_ctx (verified: /api/ps
                        # reports the requested context), so local Ollama
                        # calls go through it instead.
                        _native_base = _normalize_endpoint_url(endpoint).replace("/v1", "").rstrip("/")
                        url = f"{_native_base}/api/chat"
                        payload = {
                            "model": model,
                            "messages": payload_messages,
                            "stream": stream,
                            "options": {
                                "num_ctx": int(p_config.get("num_ctx", 16384)),
                                "temperature": actual_temp,
                                "num_predict": adjusted_max_tokens,
                            },
                        }
                        # Note: qwen3's thinking is deliberately NOT disabled
                        # here (no `"think": False`). On the native endpoint the
                        # reasoning arrives structurally in message.thinking —
                        # wrapped into <thinking> tags below, which the renderer
                        # shows as a collapsed, expandable section — so it can't
                        # bury a tool tag or leak into the visible reply, and
                        # Steven likes being able to expand and read it.
                        #
                        # The /no_think system message added above still rides
                        # along on payload_messages, though, and it does disable
                        # reasoning. It is removed further down whenever native
                        # tools are attached, since its only justification was
                        # protecting the XML tag. A qwen3 model that reports no
                        # tool capability keeps it.

                    _native_model_capabilities: list[str] = []
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
                            async with _httpx_check.AsyncClient(timeout=3.0) as _probe:
                                _probe_resp = await _probe.post(
                                    f"{_check_base}/api/show",
                                    json={"name": model},
                                )
                                if _probe_resp.status_code != 200:
                                    raise RuntimeError(f"Model '{model}' not available in Ollama (status {_probe_resp.status_code})")
                                # /api/show also reports what the model can do
                                # (e.g. ["completion", "tools", "thinking"]).
                                # Reused below to decide the calling protocol --
                                # asking beforehand beats sending `tools` and
                                # parsing the 400 that comes back.
                                try:
                                    _native_model_capabilities = list(
                                        (_probe_resp.json() or {}).get("capabilities") or []
                                    )
                                except Exception:
                                    _native_model_capabilities = []
                        except Exception as _probe_exc:
                            logger.warning(
                                "Skipping provider '%s': model '%s' not available: %s",
                                p_name, model, _probe_exc
                            )
                            failed_endpoints.add(_norm_check_base)
                            probe_desc = f"{type(_probe_exc).__name__}: {_probe_exc}" if str(_probe_exc) else type(_probe_exc).__name__
                            errors.append(f"Provider '{p_name}' skipped: {probe_desc}")
                            continue

                    # Native function-calling.
                    #
                    # The XML convention asks the model to reproduce an exact tag from
                    # prose instructions, which is a test of instruction-following that
                    # small models fail. Measured on this box: the 8B could do it, the
                    # 4B that replaced it (6GB VRAM, see the num_ctx note above) cannot
                    # -- it has the documentation in context and narrates what it would
                    # call instead of emitting the tag. The same 4B emits correct native
                    # tool_calls, because the provider renders the schema into the chat
                    # template and constrains decoding rather than relying on the model
                    # following instructions. Bigger remote models pass the XML test,
                    # but they get the structured protocol too: it removes a whole class
                    # of parse failure (unescaped quotes, markdown fences, a tag emitted
                    # inside <thinking>) rather than just being easier for the model.
                    #
                    # The one-protocol rule still holds: `tools` goes in and the XML
                    # documentation comes OUT of the system prompt, so the model is
                    # offered exactly one way to call a tool. Sending both is what made
                    # Kimi narrate its own confusion about which to use, which is why
                    # this used to be XML-only. _tool_calls_to_call_tags turns the
                    # response back into <call:/> tags, so nothing downstream of this
                    # function knows which protocol was used.
                    #
                    # Who does NOT get it, and why:
                    #  - embedded: a different branch entirely (transformers
                    #    generate_stream), no tools parameter to send.
                    #  - anthropic: this path posts an OpenAI-shaped body to
                    #    /chat/completions, which is not Anthropic's API. Its tools
                    #    schema differs too (input_schema, no function wrapper). Adding
                    #    an OpenAI-shaped tools array would only change how it fails.
                    #  - any Ollama model whose /api/show capabilities omit "tools".
                    # All of those keep the XML documentation and the text protocol, so
                    # every provider can still call tools -- the choice here is which
                    # mechanism, not whether.
                    _is_anthropic = (
                        p_config.get("service") == "anthropic" or "anthropic" in (endpoint or "")
                    )
                    if not native_tool_specs:
                        _tools_verdict = None
                    elif _use_native_ollama:
                        _tools_verdict = (
                            "ok" if "tools" in _native_model_capabilities
                            else f"model reports no tool capability ({_native_model_capabilities or 'none reported'})"
                        )
                    elif _is_anthropic:
                        _tools_verdict = "anthropic endpoint takes a different tools schema"
                    else:
                        # OpenAI, Gemini's OpenAI-compat layer, Moonshot/Kimi, LM Studio
                        # and anything else behind /v1/chat/completions take the same
                        # OpenAI-shaped array. There is no capability probe for these,
                        # so a provider that rejects `tools` is caught by the 400 retry
                        # below, which puts the XML documentation back.
                        _tools_verdict = "ok"

                    if _tools_verdict is not None:
                        if _tools_verdict != "ok":
                            logger.info(
                                "Provider '%s' (%s): %s; keeping the <call:/> text protocol",
                                p_name, model, _tools_verdict,
                            )
                        else:
                            _swapped, _removed = _swap_xml_protocol_for_native(
                                payload["messages"], xml_tool_protocol
                            )
                            if not _removed:
                                # The documentation is assembled and then .strip()ed into
                                # the system prompt, so an edit up there can stop this
                                # from matching. Failing closed matters: sending `tools`
                                # while the XML instructions are still in context is the
                                # two-competing-protocols case, and it degrades quietly
                                # (the model narrates its confusion) instead of erroring.
                                logger.warning(
                                    "Provider '%s': could not remove the XML tool protocol from the "
                                    "prompt, so native tools were NOT sent -- staying on <call:/>. "
                                    "The xml_tool_protocol block and the assembled system prompt have "
                                    "drifted apart; they must match verbatim.",
                                    p_name,
                                )
                            else:
                                # Drop the /no_think soft-switch added above. Its
                                # stated reason is that qwen3's reasoning "buries the
                                # <call:tool/> tag so it never fires" -- a property of
                                # the XML protocol, which is not in use here. Native
                                # tool_calls are a structured field and cannot be
                                # buried by reasoning text. Leaving it would suppress
                                # the thinking that the native branch's own comment
                                # says it preserves, and that the renderer shows as an
                                # expandable section.
                                payload["messages"] = [
                                    m for m in _swapped
                                    if not (m.get("role") == "system" and m.get("content") == "/no_think")
                                ]
                                payload["tools"] = _select_tools_for_turn(
                                    native_tool_specs, payload["messages"], tool_index,
                                    p_type, p_config, p_name, sticky_tools,
                                )
                                logger.info(
                                    "Provider '%s': sending %d native tool schemas to %s "
                                    "(XML tool protocol withheld)",
                                    p_name, len(payload["tools"]), model,
                                )

                    def _payload_without_tools(_p: dict[str, Any]) -> dict[str, Any]:
                        """
                        Build the retry payload for a provider that rejected `tools`.

                        Dropping the array is not enough on its own: attaching tools
                        also strips the XML documentation out of the system prompt, so
                        a naive pop would leave the model with no tool protocol at all
                        and no error to show for it. Put the documentation back.
                        """
                        _retry = dict(_p)
                        _retry.pop("tools", None)
                        _retry["messages"] = payload_messages
                        return _retry

                    # Local (CPU-bound) inference is the SLOWER path, not the faster one --
                    # prompt prefill alone for a ~2000-token prompt routinely takes 15-25s+
                    # on CPU before generation even starts (confirmed against live Ollama
                    # server logs: every request was being cut off at exactly the old 30.0s
                    # timeout, a 100% failure rate). Give it more room than remote, not less.
                    _read_timeout = float(params.get("timeout_seconds", 180.0 if p_type == "local" else 120.0))
                    async with httpx.AsyncClient(timeout=httpx.Timeout(_read_timeout, connect=3.0)) as client:
                        if stream:
                            chunks = []
                            in_reasoning = False
                            streamed_tool_calls: dict[int, dict[str, Any]] = {}
                            async with client.stream("POST", url, headers=headers, json=payload) as response:
                                if response.status_code != 200:
                                    err_body = await response.aread()
                                    err_str = err_body.decode(errors='ignore')
                                    if response.status_code == 400 and "tools" in payload:
                                        # Any 400 with tools attached, not just the
                                        # literal "does not support tools" Ollama
                                        # returns: every other provider words its
                                        # rejection differently, and matching on one
                                        # provider's phrasing means the rest fail the
                                        # whole request instead of falling back.
                                        logger.info(
                                            "Provider '%s' rejected the request with tools attached (%s). "
                                            "Retrying with the <call:/> text protocol...",
                                            p_name, err_str[:200],
                                        )
                                        payload_no_tools = _payload_without_tools(payload)
                                        async with client.stream("POST", url, headers=headers, json=payload_no_tools) as resp2:
                                            if resp2.status_code != 200:
                                                err_body2 = await resp2.aread()
                                                raise RuntimeError(f"HTTP Error {resp2.status_code}: {err_body2.decode(errors='ignore')}")
                                            async for line in resp2.aiter_lines():
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
                                    else:
                                        raise RuntimeError(f"HTTP Error {response.status_code}: {err_str}")
                                else:
                                    async for line in response.aiter_lines():
                                        # Native Ollama /api/chat streams NDJSON: one
                                        # bare JSON object per line, no "data: " prefix,
                                        # chunk text under message.content and a `done`
                                        # flag on the final object.
                                        if _use_native_ollama:
                                            _l = line.strip()
                                            if not _l:
                                                continue
                                            try:
                                                _obj = json.loads(_l)
                                            except Exception:
                                                continue
                                            _msg = _obj.get("message") or {}

                                            # Reasoning arrives structurally in
                                            # message.thinking on this endpoint,
                                            # and preserving it is the stated
                                            # reason the native path exists --
                                            # but only the non-streaming branch
                                            # and the OpenAI-compat SSE branch
                                            # wrapped it. Here it was read into
                                            # _msg and dropped, so a streaming
                                            # caller silently lost the reasoning
                                            # the renderer shows as a collapsed
                                            # section.
                                            _think = _msg.get("thinking") or ""
                                            if _think:
                                                if not in_reasoning:
                                                    in_reasoning = True
                                                    _think = "<thinking>\n" + _think
                                                chunks.append(_think)
                                                await ws.send(json.dumps({
                                                    "jsonrpc": JSONRPC_VERSION,
                                                    "method": "inference.generate.chunk",
                                                    "params": {"chunk": _think, "session_id": session_id},
                                                }))

                                            _chunk = _msg.get("content") or ""
                                            if _chunk and in_reasoning:
                                                # Close the block before the first
                                                # visible token, so reasoning can
                                                # never leak into the reply.
                                                in_reasoning = False
                                                _chunk = "\n</thinking>\n\n" + _chunk
                                            if _chunk:
                                                chunks.append(_chunk)
                                                await ws.send(json.dumps({
                                                    "jsonrpc": JSONRPC_VERSION,
                                                    "method": "inference.generate.chunk",
                                                    "params": {"chunk": _chunk, "session_id": session_id},
                                                }))
                                            # Native tool_calls arrive as a field on a
                                            # streamed message, not as text, so they have
                                            # to be converted here too -- otherwise a
                                            # streaming caller silently loses every tool
                                            # call while the non-streaming path handles
                                            # them fine.
                                            _stream_tcs = _msg.get("tool_calls") or []
                                            if _stream_tcs:
                                                _tc_text = _tool_calls_to_call_tags(_stream_tcs)
                                                if _tc_text:
                                                    chunks.append(f"\n{_tc_text}")
                                                    await ws.send(json.dumps({
                                                        "jsonrpc": JSONRPC_VERSION,
                                                        "method": "inference.generate.chunk",
                                                        "params": {"chunk": f"\n{_tc_text}", "session_id": session_id},
                                                    }))
                                            if _obj.get("done"):
                                                break
                                            continue
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

                                                    # OpenAI-style streaming splits a tool
                                                    # call across deltas: the name arrives
                                                    # once, then `arguments` in fragments,
                                                    # keyed by index. Accumulated here and
                                                    # emitted once complete -- converting
                                                    # per-delta would produce a tag with
                                                    # half a JSON object in it.
                                                    _accumulate_streamed_tool_calls(
                                                        delta.get("tool_calls"), streamed_tool_calls
                                                    )
                                            except Exception:
                                                pass
                            if in_reasoning:
                                chunks.append("\n</thinking>\n\n")
                            if streamed_tool_calls:
                                _tc_text = _tool_calls_to_call_tags(
                                    [streamed_tool_calls[k] for k in sorted(streamed_tool_calls)]
                                )
                                if _tc_text:
                                    chunks.append(f"\n{_tc_text}")
                                    await ws.send(json.dumps({
                                        "jsonrpc": JSONRPC_VERSION,
                                        "method": "inference.generate.chunk",
                                        "params": {"chunk": f"\n{_tc_text}", "session_id": session_id},
                                    }))
                            generated_text = "".join(chunks)
                        else:
                            response = await client.post(url, headers=headers, json=payload)
                            if response.status_code != 200:
                                if response.status_code == 400 and "tools" in payload:
                                    # See the streaming branch: match on "a 400 while
                                    # tools were attached", not on one provider's
                                    # wording of the rejection.
                                    logger.info(
                                        "Provider '%s' rejected the request with tools attached (%s). "
                                        "Retrying non-stream with the <call:/> text protocol...",
                                        p_name, response.text[:200],
                                    )
                                    payload_no_tools = _payload_without_tools(payload)
                                    response = await client.post(url, headers=headers, json=payload_no_tools)
                                if response.status_code != 200:
                                    raise RuntimeError(f"HTTP Error {response.status_code}: {response.text}")
                            resp_json = response.json()
                            if _use_native_ollama:
                                # Native /api/chat shape: {"message": {"content",
                                # "thinking"?, "tool_calls"?}, "done": true}
                                choice = resp_json.get("message") or {}
                            else:
                                choice = resp_json["choices"][0]["message"]
                            content = choice.get("content", "") or ""
                            reasoning = choice.get("reasoning", "") or choice.get("reasoning_content", "") or choice.get("thinking", "") or ""
                            tool_calls = choice.get("tool_calls", [])

                            if tool_calls:
                                # Appended rather than substituted-when-empty: a model
                                # may return a sentence AND a tool call in the same
                                # message, and dropping the call in that case is a
                                # silent no-op that reads as "the model ignored me".
                                # _find_tool_call takes the first tag either way.
                                tc_tags = _tool_calls_to_call_tags(tool_calls)
                                if tc_tags:
                                    content = f"{content}\n{tc_tags}".strip() if content else tc_tags

                            if reasoning:
                                generated_text = f"<thinking>\n{reasoning}\n</thinking>\n\n{content}"
                            else:
                                generated_text = content

                if not generated_text or not generated_text.strip():
                    err_msg = f"Provider '{p_name}' ({p_type}) returned empty response text."
                    logger.warning(err_msg)
                    errors.append(err_msg)
                    success = False
                    continue

                # Router Checks
                is_forced_provider = (forced_provider and provider["id"] == forced_provider["id"])
                is_requested_provider = (request_provider and provider["id"] == request_provider["id"])
                
                if not (is_forced_provider or is_requested_provider):
                    gen_lower = generated_text.lower()
                    low_confidence_phrases = ["i don't know", "i am not sure", "as an ai", "i cannot fulfill", "i'm not sure", "i can't fulfill"]
                    
                    fallback_reason = None
                    if any(phrase in gen_lower for phrase in low_confidence_phrases):
                        fallback_reason = "Low confidence detected"
                    elif last_raw_assistant_text:
                        # Compare the raw (pre-clean) LLM outputs so the <call:...> tags are
                        # still present in both sides — cleaned all_responses strips the tags.
                        tool_match = _find_tool_call(generated_text)
                        last_tool_match = _find_tool_call(last_raw_assistant_text)
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
                    logger.warning("Provider '%s' failed during generation turn: %s", p_name, exc)

        if not success:
            all_errors = "; ".join(errors)
            return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"All inference providers failed: {all_errors}")

        generated_text = _normalize_think_tags(generated_text)
        generated_text = _normalize_tool_call_format(generated_text)

        # When raw_prompt is requested (e.g. for code synthesis), return raw text immediately on Turn 0
        if bool(params.get("raw_prompt", False)):
            all_responses.append(generated_text)
            break

        # Parse tool calls
        tool_match = _find_tool_call(generated_text)
        if not tool_match:
            # If the user's actual message implies action/search/fix/remove and the
            # model spoke prose without emitting a tool call:
            if not is_casual_chat:
                if loop_count < 3 and _prompt_implies_untaken_action(raw_user_prompt):
                    logger.info(f"Model stated intent without emitting tool call tag on action prompt (Turn {loop_count}). Auto-continuing turn...")
                    messages.append({"role": "assistant", "content": generated_text})
                    messages.append({
                        "role": "user",
                        "content": "[SYSTEM DIRECTIVE]: You stated your intention to search, locate, or edit, but you did NOT output a tool call tag `<call:...>`. Execute your intent NOW by outputting the required tool call tag (e.g. `<call:search_workspace_files query=\"...\" />`)."
                    })
                    loop_count += 1
                    continue
            all_responses.append(_close_unclosed_thinking(generated_text))
            break

        # Truncate any text generated after the call tag to prevent model hallucinations of responses
        generated_text = generated_text[:tool_match.end()]

        tool_name = tool_match.group(1)
        attr_str = tool_match.group(2)
        inner_content = tool_match.group(3)
        if attr_str.endswith('/'):
            attr_str = attr_str[:-1]

        # Alias common LLM hallucinated tool names or sanitized function names (e.g. docs_create_documentation_node)
        if tool_name.startswith("run_workflow"):
            tool_name = "run_workflow_node"
        elif tool_name == "run_validation":
            tool_name = "test.run_validation"
        else:
            try:
                from sdk.agent.tools import ToolRegistry
                if tool_name.lower() not in ToolRegistry._tools:
                    for orig_name in ToolRegistry._tools.keys():
                        if re.sub(r'[^a-zA-Z0-9_-]', '_', orig_name) == tool_name.lower():
                            tool_name = orig_name
                            break
            except Exception:
                pass

        # Keep this tool on offer for the rest of the request. Two reasons: a
        # multi-turn task must not lose a tool it is mid-way through using, and a
        # tool the model reached for despite selection ranking it low is direct
        # evidence the ranking was wrong about this conversation.
        if tool_name:
            if tool_name not in sticky_tools:
                logger.info("Tool '%s' pinned for the rest of this request", tool_name)
            sticky_tools.add(tool_name)

        # Parse attributes (handles double quotes, single quotes, escaped quotes, and unquoted values)
        attrs = {}
        pattern = r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|(\S+))'
        for match in re.finditer(pattern, attr_str):
            k = match.group(1)
            v_dq, v_sq, v_uq = match.group(2), match.group(3), match.group(4)
            if v_dq is not None:
                attrs[k] = v_dq.replace('\\"', '"').replace('\\\\', '\\')
            elif v_sq is not None:
                attrs[k] = v_sq.replace("\\'", "'").replace('\\\\', '\\')
            elif v_uq is not None:
                try:
                    attrs[k] = json.loads(v_uq.replace("'", '"'))
                except Exception:
                    attrs[k] = v_uq

                    
        if inner_content:
            inner_content = inner_content.strip()
            # Try to resolve parameter names dynamically from the tool signature in the registry
            mapped = False
            try:
                from sdk.agent.tools import ToolRegistry
                tool_entry = ToolRegistry._tools.get(tool_name.lower())
                if tool_entry:
                    func = tool_entry.get("func")
                    if func:
                        import inspect
                        sig = inspect.signature(func)
                        # NOTE: do NOT bind this to the name `params` — that is the
                        # handler's RPC params dict, and clobbering it corrupts the
                        # post-tool regeneration turn (e.g. float(params["timeout_seconds"])
                        # would receive an inspect.Parameter and crash every provider).
                        sig_params = sig.parameters
                        if "code" in sig_params:
                            attrs["code"] = inner_content
                            mapped = True
                        elif "goal" in sig_params:
                            attrs["goal"] = inner_content
                            mapped = True
                        elif "command" in sig_params:
                            attrs["command"] = inner_content
                            mapped = True
                        elif "content" in sig_params:
                            attrs["content"] = inner_content
                            mapped = True
                        elif "detailed_markdown" in sig_params:
                            attrs["detailed_markdown"] = inner_content
                            mapped = True
                        elif "semantic_memory" in sig_params:
                            attrs["semantic_memory"] = inner_content
                            mapped = True
                        elif "plan" in sig_params:
                            try:
                                attrs["plan"] = json.loads(inner_content)
                            except Exception:
                                attrs["plan"] = {"raw": inner_content}
                            mapped = True
            except Exception as e:
                logger.warning("Error resolving tool signature dynamically for mapping: %s", e)

            if not mapped:
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
                elif tool_name.lower() in {"run_workspace_command", "shell.run"}:
                    attrs["command"] = inner_content
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
            # Produce a human-readable confirmation instead of a raw JSON blob
            if isinstance(tool_result, dict):
                created = tool_result.get("created", False)
                updated = tool_result.get("updated", False)
                unchanged = tool_result.get("unchanged", False)
                node_id = tool_result.get("node_id") or tool_result.get("result", {}).get("node_id", "")
                errors_list = tool_result.get("errors", [])
                validation = tool_result.get("validation_passed", tool_result.get("result", {}).get("validation_passed", None))

                if errors_list:
                    human_result = f"\u274c Documentation node operation failed: {'; '.join(str(e) for e in errors_list)}"
                elif created:
                    human_result = f"\u2705 Documentation node **{node_id}** created successfully" + (" (validation passed)" if validation else "")
                elif updated:
                    human_result = f"\u2705 Documentation node **{node_id}** updated successfully" + (" (validation passed)" if validation else "")
                elif unchanged:
                    human_result = f"\u2139\ufe0f Documentation node **{node_id}** is already up to date — no changes were needed."
                else:
                    human_result = json.dumps(tool_result, indent=2)
            else:
                human_result = str(tool_result) if tool_result else "\u26a0\ufe0f Documentation tool returned no result."

            all_responses.append(human_result)
            break

        # Filter out the tag from user visible response
        clean_generated = re.sub(r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))', '', generated_text, flags=re.DOTALL)
        clean_generated = re.sub(r'```(?:xml)?\s*', '', clean_generated)
        clean_generated = clean_generated.replace('**Tool Call:**', '').strip()
        if clean_generated:
            all_responses.append(_close_unclosed_thinking(clean_generated))

        # Append to message history for next LLM turn
        messages.append({"role": "assistant", "content": generated_text})
        messages.append({"role": "user", "content": f"<response: {tool_name} result={json.dumps(tool_result)} />"})

        await db.log_to_db(
            "INFO",
            f"Agent Chat: Auto-executed tool '{tool_name}' during inference",
            {"tool": tool_name, "params": attrs}
        )

        # Snapshot the raw LLM text so the loop detector can compare with real tags intact
        last_raw_assistant_text = generated_text
        loop_count += 1
    else:
        # Loop limit reached, append final text
        all_responses.append(_close_unclosed_thinking(generated_text))

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
        # Record BOTH sides of the exchange — without the user turn,
        # conversation search and the user model only ever see Tala talking
        # to itself.
        await handle_memory_record_completed_turn({
            "session_id": session_id,
            "role": "user",
            "content": raw_user_prompt,
            "tool_calls": [],
        })
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

        # Default: Tala's canonical natal chart (1977-07-20). Imported rather
        # than hardcoded -- this used to be a second, disagreeing date, so
        # whether the DB was reachable decided which chart she got.
        from python.astrology.ephemeris import TALA_NATAL
        birth = TALA_NATAL
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
                cfg = decrypt_profile_config(row["config"] if row else {}) or {}
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
            user_cfg = decrypt_profile_config(user_row["config"]) or {}
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
    ``identity.get_user_profile`` — Load the user profile from its HTML
    Neural Node (concept-user-profile.html). Per the HTML-NN universal data
    mandate, the node is the canonical, always-loaded-from record for the
    profile -- this returns the same {id, type, name, username, config}
    shape as ``profiles.get`` (Postgres) so the renderer can use this as its
    single, unconditional load path instead of racing two different
    sources with two different (and previously incompatible) schemas.
    """
    import os
    import json
    from pathlib import Path
    try:
        nodes_dir = resolve_active_nodes_dir()
        node_file = nodes_dir / "concept-user-profile.html"

        profile = {
            "id": "master-user",
            "type": "human",
            "name": "",
            "username": "",
            "config": {},
        }

        if node_file.exists():
            from sdk.agent.parser import parse_file
            try:
                node = parse_file(str(node_file))
                # Unseal the vault fields. Explicit rather than automatic in
                # parse_html: if reading any node returned plaintext, the
                # sealing would be decoration -- retrieval and anything that
                # ships a node to a peer would carry the values straight back
                # out. This caller is the settings panel showing the user their
                # own profile, which is entitled to them.
                from python.harness.node_vault import unseal_state
                sd = unseal_state(node.state_data or {})

                profile["id"] = sd.get("id", "master-user")
                profile["type"] = sd.get("type", "human")
                profile["name"] = sd.get("name", "")
                profile["username"] = sd.get("username", "")

                config = sd.get("config")
                if not isinstance(config, dict):
                    # The node is in the flat legacy format. The database holds
                    # the complete config, so prefer it over reconstructing a
                    # partial one from the flat keys -- that reconstruction
                    # cannot carry roles, skills, projects, connections or
                    # privacy at all, which is what made those fields look lost
                    # (UI-099). Fall through to the legacy path only if the row
                    # is genuinely unavailable.
                    try:
                        _row = await db.fetchrow(
                            "SELECT config FROM profiles WHERE id = 'master-user'"
                        )
                        if _row and _row["config"]:
                            _cfg = decrypt_profile_config(_row["config"])
                            if isinstance(_cfg, dict) and _cfg:
                                logger.info(
                                    "identity.get_user_profile: node is legacy-flat; "
                                    "served the complete config from the database instead."
                                )
                                profile["config"] = _cfg
                                config = _cfg
                    except Exception as _db_exc:
                        logger.warning(
                            "identity.get_user_profile: could not read the profile row "
                            "as a fallback: %s", _db_exc
                        )

                if not isinstance(config, dict):
                    # Backward compat: a node written before this fix (or by
                    # handle_identity_save_user_profile below) only has the
                    # flat legacy keys, not a "config" object. Reconstruct a
                    # best-effort config from them so old nodes still render
                    # something sensible instead of a blank profile.
                    rp = sd.get("rp_persona", {}) if isinstance(sd.get("rp_persona"), dict) else {}
                    pii = sd.get("pii_vault", {}) if isinstance(sd.get("pii_vault"), dict) else {}
                    biography = sd.get("biography", "") or node.semantic_memory.strip()
                    config = {
                        "identity": {
                            "name": profile["name"],
                            "username": profile["username"],
                            "type": profile["type"],
                            "avatar_url": "", "cover_url": "", "headline": "",
                            "location": "", "timezone": "", "language": "",
                            "pronouns": "", "nickname": "", "motto": "",
                            "birthdate": sd.get("birthdate") or sd.get("user_details", {}).get("birthdate", ""),
                        },
                        "about": {"bio": biography, "story": "", "status": "", "goals": "", "values": ""},
                        "roles": {"title": "", "company": "", "department": "", "education": "", "work_history": ""},
                        "rp_persona": {
                            "name": sd.get("rp_name") or rp.get("name", ""),
                            "class": sd.get("rp_class") or rp.get("class", ""),
                            "faction": sd.get("rp_faction") or rp.get("faction", ""),
                            "backstory": sd.get("rp_backstory") or rp.get("backstory", ""),
                        },
                        "pii_vault": {
                            "legal_name": sd.get("pii_legal_name") or pii.get("legal_name", ""),
                            "dob": sd.get("pii_dob") or pii.get("dob", ""),
                            "email": sd.get("pii_email") or pii.get("email", ""),
                            "phone": sd.get("pii_phone") or pii.get("phone", ""),
                            "registries": sd.get("pii_registries") or pii.get("registries", []),
                        },
                        "skills": [], "projects": [], "connections": [],
                        "preferences": {
                            "tone": sd.get("tone") or sd.get("formatting_preferences", {}).get("preferred_tone", "warm_professional"),
                            "notifications": "all",
                        },
                        "privacy": {},
                    }

                profile["config"] = config

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
        nodes_dir = resolve_active_nodes_dir()
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
            "pii_registries": pii_registries,
        }

        # Vault-tagged: the PII fields above are encrypted in the node itself
        # (Steven, 2026-08-23: "the nodes that containe PII can be tagged vault
        # and anything in vault is encrypted"). The node stays the record --
        # stripping the values out instead would have made Postgres the real
        # store and this a lossy summary, against the HTML-NN mandate.
        from python.harness.node_vault import seal_for
        state_data = seal_for("user-profile", state_data)
        
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
    <p>This user has {"a populated" if (pii_legal_name or pii_dob or pii_email or pii_phone) else "an empty"} identity vault
    holding {len(pii_registries) if pii_registries else 0} registered identifier(s).
    The values themselves are held encrypted in the profile store and are
    deliberately not reproduced here: this node is indexed for retrieval, so
    anything written into it can be surfaced into a model's context. No agent
    can read them -- user_profile_lookup refuses the pii_vault section at every
    visibility setting.</p>"""

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
                existing_cfg = decrypt_profile_config(existing_row["config"]) or {}
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
    nodes_dir = resolve_active_nodes_dir()

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
            # Must match the retrieval path in handle_inference_generate, which
            # gates on max(execution, memory) rather than scaling by it.
            # Reporting a different number from the one recall uses made this
            # endpoint -- the instrument you would reach for to debug what got
            # recalled -- disagree with the recall it describes.
            governance_multiplier=1.0 if max(node.execution_authority, node.memory_authority) > 0.0 else 0.0,
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
        rows = await load_inference_providers()
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
                    
                    # apiKey was written in clear (UI-047). Selective, so model,
                    # engine and endpoint stay readable and queryable without a
                    # key -- only the secret itself is wrapped.
                    from python.harness.credential_crypto import encrypt_config
                    stored_cfg = encrypt_config(cfg)

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
                        json_mod.dumps(stored_cfg),
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
        rows = await load_inference_providers()
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
    nodes_dir = resolve_active_nodes_dir()

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
    # An explicit grant to this peer for these nodes supersedes both the
    # contact's `nodes` flag and the firewall clearance below (UI-082,
    # UI-101). Checked first so an invitation actually means something; the
    # existing clearance machinery is untouched for anyone not invited.
    from python.harness.access import PEER, Principal, check as access_check

    who = str(contact.get("username") or contact.get("wallet_address") or "").lstrip("@").lower()
    if who and node_names:
        decisions = [
            await access_check(Principal(PEER, who), f"node:{name}", "read")
            for name in sorted(node_names)
        ]
        # Lockdown removes access from everyone but the owner, whatever any
        # grant or clearance level says, so it short-circuits before the
        # existing machinery runs.
        if any("lockdown" in d.reason for d in decisions if not d.allowed):
            return False, "lockdown_engaged"
        # Every requested node individually invited: the clearance path below
        # is for peers who were not.
        if all(d.allowed for d in decisions):
            return True, None

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


def _encrypt_credential(config):
    """Encrypt the secret fields of a credential config before storing it."""
    from python.harness.credential_crypto import encrypt_config
    return encrypt_config(config)


def _decrypt_credential(config):
    """Decrypt a credential config read from the database."""
    from python.harness.credential_crypto import decrypt_config
    return decrypt_config(config)


async def _local_identity() -> dict[str, str]:
    """
    Who this machine says it is when calling a peer.

    Peers now require an identified caller before serving skills (UI-040), so
    outbound requests carry this. Mirrors how the rest of the harness resolves
    the local wallet: a saved setting, then TALA_WALLET_ADDRESS, then a
    last-resort literal so a first run still has something to send.
    """
    username = ""
    try:
        row = await db.fetchrow("SELECT username, config FROM profiles WHERE id = 'master-user'")
        if row:
            username = str(row["username"] or "")
            if not username:
                cfg = decrypt_profile_config(row["config"]) or {}
                username = str(((cfg or {}).get("identity") or {}).get("username") or "")
    except Exception as exc:
        logger.debug("_local_identity: no profile username available: %s", exc)
    return {
        "username": username,
        "wallet_address": os.environ.get("TALA_WALLET_ADDRESS", "local-tala"),
    }


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
    return decrypt_profile_config(row["config"]) or {}


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
    # A peer used to have its requested_permissions written verbatim, and
    # ON CONFLICT DO UPDATE meant it could also overwrite permissions the
    # operator had already narrowed (UI-041). Steven's model is invite-only:
    # "permissions are by invite only ... I have to invite her to access"
    # (Q-22). So what the peer asks for is recorded as a *request* and grants
    # nothing; every enrolment starts from deny.
    requested = packet.get("requested_permissions")
    requested = requested if isinstance(requested, dict) else {}

    permissions = {
        "identity": False,
        "about": False,
        "rp_persona": False,
        "roles": False,
        "skills": False,
        "projects": False,
        "preferences": False,
        "nodes": False,
        "pii_vault": False,
    }
    if requested:
        permissions["_requested"] = {k: bool(v) for k, v in requested.items()}
        logger.info(
            "p2p enrolment: %s requested %s; recorded as a request, granted nothing.",
            username, sorted(k for k, v in requested.items() if v),
        )

    await db.execute(
        """
        INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret, permissions)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT (username) DO UPDATE SET
            wallet_address = EXCLUDED.wallet_address,
            endpoint_url = EXCLUDED.endpoint_url,
            shared_secret = EXCLUDED.shared_secret,
            -- permissions deliberately NOT updated: re-enrolling must never
            -- reset a grant the operator has already made or withdrawn.
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
        rows = await load_inference_providers()
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
            cfg = decrypt_profile_config(r["config"])
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
        cfg = decrypt_profile_config(row["config"])
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


#: Profile sections whose every value is personal identity (UI-055).
PII_SECTIONS = ("pii_vault",)


def encrypt_profile_config(config: Any) -> Any:
    """
    Encrypt the PII sections of a profile config before it is stored.

    Only pii_vault: name, roles, skills and projects are the things the profile
    exists to make usable, and encrypting them would mean a key read on every
    lookup for no benefit. Legal name, date of birth, contact details and
    identity-document registries are a different category.
    """
    from python.harness.credential_crypto import encrypt_section

    if not isinstance(config, dict):
        return config
    out = dict(config)
    for section in PII_SECTIONS:
        if isinstance(out.get(section), dict):
            out[section] = encrypt_section(out[section])
    return out


def decrypt_profile_config(config: Any) -> Any:
    """Reverse :func:`encrypt_profile_config`. Plaintext passes through."""
    from python.harness.credential_crypto import decrypt_section

    if isinstance(config, str):
        import json as _json
        try:
            config = _json.loads(config)
        except (ValueError, _json.JSONDecodeError):
            return config
    if not isinstance(config, dict):
        return config
    out = dict(config)
    for section in PII_SECTIONS:
        if isinstance(out.get(section), dict):
            out[section] = decrypt_section(out[section])
    return out


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
    # Encrypt PII before it is written, but index and return the plaintext:
    # the caller handed us a live config and expects it back usable.
    config_json = json_mod.dumps(encrypt_profile_config(config))

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
        node_sync_error: str | None = None
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
                roles_obj = config.get("roles", {})
                skills_list = config.get("skills", []) or []
                projects_list = config.get("projects", []) or []
                connections_list = config.get("connections", []) or []

                birthdate = id_obj.get("birthdate", "").strip()
                headline = id_obj.get("headline", "").strip()
                location = id_obj.get("location", "").strip()
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
                nodes_dir = resolve_active_nodes_dir()
                nodes_dir.mkdir(parents=True, exist_ok=True)

                current_iso_time = datetime.now(timezone.utc).isoformat()

                # state_data carries the FULL config (identity/about/roles/
                # rp_persona/pii_vault/skills/projects/connections/
                # preferences/privacy) as the single source of truth for
                # this node -- per the HTML-NN universal data mandate, the
                # neural node is meant to be the canonical, always-loaded-
                # from record, not a narrower mirror of the profiles table.
                # The flat top-level keys (name, birthdate, rp_name, ...)
                # are kept alongside it purely for backward compatibility
                # with any older reader that hasn't been updated to read
                # "config" yet.
                state_data = {
                    "id": profile_id,
                    "type": profile_type,
                    "name": name,
                    "username": username,
                    "config": config,
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
                    "pii_registries": pii_registries,
                }
                # Sealed, not stripped: the node keeps the record and the
                # sensitive half of it is encrypted (UI-054).
                from python.harness.node_vault import seal_for, summarise_vault
                state_data = seal_for("user-profile", state_data)
                
                adapter_packet = {
                    "activation_keywords": ["age", "old", "birthday", "birthdate", "name", "who am I", "how old am I", "roleplay", "rp", "persona", "avatar", "character", "class", "faction", "alignment", "pii", "secure", "vault", "legal", "email", "phone", "location"],
                    "tool_affordances": [],
                    "forbidden_actions": [],
                    "repair_policies": {},
                    "validators": {},
                    "confidence": 1.0
                }
                
                skills_html = "".join(
                    f"<li>{s.get('name', '')} ({s.get('category', '')}, {s.get('level', '')})</li>"
                    for s in skills_list if isinstance(s, dict) and s.get("name")
                ) or "<li>None listed.</li>"
                projects_html = "".join(
                    f"<li>{p.get('name', '')} [{p.get('status', '')}] -- {p.get('description', '')}</li>"
                    for p in projects_list if isinstance(p, dict) and p.get("name")
                ) or "<li>None listed.</li>"
                connections_html = "".join(
                    f"<li>{c.get('name', '')} ({c.get('type', '')})</li>"
                    for c in connections_list if isinstance(c, dict) and c.get("name")
                ) or "<li>None listed.</li>"

                content_html = f"""
    <h2>User Persona & Details</h2>
    <p>User Name: {name}</p>
    <p>Headline: {headline}</p>
    <p>Location: {location}</p>
    <p>User Birthdate: {birthdate}</p>
    <p>Keywords: age, old, birthday, birthdate, name, who am I, how old am I, roleplay, rp, persona, avatar, character, class, faction, alignment, pii, secure, vault, legal, email, phone, location</p>
    <p>{biography}</p>

    <h3>Work &amp; Roles</h3>
    <p>Title: {roles_obj.get('title', '')}</p>
    <p>Company: {roles_obj.get('company', '')}</p>
    <p>Work History: {roles_obj.get('work_history', '')}</p>

    <h3>Skills</h3>
    <ul>{skills_html}</ul>

    <h3>Projects</h3>
    <ul>{projects_html}</ul>

    <h3>Connections</h3>
    <ul>{connections_html}</ul>

    <h3>Roleplay Persona (Avatar)</h3>
    <p>RP Character Name: {rp_name}</p>
    <p>RP Class/Occupation: {rp_class}</p>
    <p>RP Faction/Alignment: {rp_faction}</p>
    <p>RP Character Backstory: {rp_backstory}</p>

    <h3>Personally Identifiable Information &amp; Vault Registry</h3>
    <p>{summarise_vault(
        {"pii_legal_name": pii_legal_name, "pii_dob": pii_dob,
         "pii_email": pii_email, "pii_phone": pii_phone,
         "pii_registries": pii_registries},
        ("pii_legal_name", "pii_dob", "pii_email", "pii_phone", "pii_registries"),
    )}</p>"""

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
                # Previously this logged a warning and let profiles.save return
                # a clean success. The database write had already landed, so the
                # data was safe -- but the node is what identity.get_user_profile
                # reads, so the UI showed a stale profile while the save reported
                # OK, and the warning reached no surface the operator ever sees
                # (UI-056, UI-085). The save still succeeds, because the data IS
                # persisted; the caller is now told the mirror did not update.
                logger.warning("Failed to sync profiles save to HTML Neural Node: %s", sync_exc)
                node_sync_error = str(sync_exc)
                try:
                    await db.log_to_db(
                        "ERROR",
                        f"profiles.save: node mirror not updated for {profile_id}: {sync_exc}",
                        {"profile_id": profile_id, "node": "concept-user-profile.html"},
                    )
                except Exception:
                    pass

        if node_sync_error:
            return _ok(req_id, {
                "success": True,
                "id": profile_id,
                "node_synced": False,
                "warning": (
                    "Saved to the database, but the concept-user-profile node could not be "
                    f"written: {node_sync_error}. The profile panel reads that node, so it "
                    "may show stale values until this is resolved."
                ),
            })
        return _ok(req_id, {"success": True, "id": profile_id, "node_synced": True})
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
        nodes_dir = resolve_active_nodes_dir()
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


async def handle_docs_generate_function_nodes(params: dict[str, Any], req_id: Any) -> str:
    """
    ``docs.generate_function_nodes`` — Generate/refresh paired technical +
    plain-English HTML-NN nodes for registered tools. Skips (no LLM call,
    no file write) any tool whose source hasn't changed since it was last
    documented, unless ``force`` is set.
    """
    try:
        from sdk.agent.function_documenter import document_registered_tools

        result = await document_registered_tools(
            tool_names=params.get("tool_names"),
            force=bool(params.get("force", False)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("docs.generate_function_nodes error")
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

    try:
        from python.harness.workflows_util import save_workflow_data
        flow_id = await save_workflow_data(workflow)
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
        workflows_dir = resolve_active_workflows_dir()
        nodes_dir = resolve_active_nodes_dir()

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
        # Node first. workflow.save compiles the flow into an HTML-NN *and*
        # writes a JSON sidecar, and this handler only ever read the sidecar --
        # so the compiled node was written, indexed, retrieved, and never the
        # thing that actually ran. Steven's standing requirement is that what
        # the harness consumes is the HTML-NN, so the node is the source and
        # the sidecar is the fallback for flows compiled before this change.
        workflow_data = None
        source = "json"
        try:
            from sdk.agent.node_execution import load_workflow_spec
            workflow_data = load_workflow_spec(flow_id)
            if workflow_data:
                source = "html-nn"
        except Exception as exc:
            logger.warning("workflow.execute: could not read the node for %s: %s", flow_id, exc)

        if workflow_data is None:
            from python.harness.workflows_util import resolve_workflow_file
            json_path = resolve_workflow_file(flow_id)
            if not json_path or not json_path.exists():
                return _err(
                    req_id, JsonRpcError.INVALID_PARAMS,
                    f"Workflow '{flow_id}' does not exist as a node or a saved file.",
                )
            workflow_data = json.loads(json_path.read_text(encoding="utf-8"))
            logger.info(
                "workflow.execute: %s has no runnable node; ran from the JSON sidecar.",
                flow_id,
            )

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
        result["executed_from"] = source

        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("workflow.execute error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_workflow_generate_from_tot(params: dict[str, Any], req_id: Any) -> str:
    """
    ``workflow.generate_from_tot`` — Generate a workflow from ToT steps.
    """
    prompt = params.get("prompt")
    if not prompt:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'prompt' is required")
        
    try:
        from sdk.agent.tools import generate_workflow_from_tot_tool
        result = await generate_workflow_from_tot_tool(
            prompt=prompt,
            name=params.get("name"),
            description=params.get("description")
        )
        if result.get("success"):
            return _ok(req_id, result)
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, result.get("error", "Failed to generate workflow"))
    except Exception as exc:
        logger.exception("workflow.generate_from_tot error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))



async def handle_trajectory_export(params: dict[str, Any], req_id: Any) -> str:
    """
    ``trajectory.export`` — Assemble past runs into analysable records.

    Scoped by default and never a table dump: conversation turns contain
    everything the user has said, so `include_content` defaults to false and a
    caller states a window, a session or a task.

    Optionally writes JSONL to `out_path`, which is the format training
    pipelines read and which stays parseable up to the last complete line if a
    write is interrupted.
    """
    try:
        from sdk.agent.trajectory import (
            export_conversation_trajectories,
            export_task_trajectories,
            summarise,
            write_jsonl,
        )

        kind = str(params.get("kind", "conversation")).strip().lower()
        if kind not in {"conversation", "task"}:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "'kind' must be 'conversation' or 'task'")

        limit = int(params.get("limit", 50))
        since = params.get("since_hours")
        since = int(since) if since is not None else None

        if kind == "task":
            result = await export_task_trajectories(
                task_id=params.get("task_id"), since_hours=since, limit=limit
            )
        else:
            result = await export_conversation_trajectories(
                session_id=params.get("session_id"),
                since_hours=since,
                limit=limit,
                include_content=bool(params.get("include_content", False)),
                tool_calls_only=bool(params.get("tool_calls_only", False)),
            )

        if not result.get("success"):
            return _err(req_id, JsonRpcError.INTERNAL_ERROR, result.get("error", "export failed"))

        result["summary"] = summarise(result["trajectories"])
        out_path = params.get("out_path")
        if out_path:
            result["written"] = write_jsonl(result["trajectories"], out_path)
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("trajectory.export error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_mcp_discover(params: dict[str, Any], req_id: Any) -> str:
    """
    ``mcp.discover`` — Report MCP servers configured by other clients on this machine.

    Read-only. Connects to nothing: adopting a server runs a subprocess with the
    invoking user's privileges, which is a decision for a person rather than
    something a scan performs on its own.
    """
    try:
        from sdk.agent.mcp_bridge import discover_mcp_servers

        extra = params.get("paths")
        return _ok(req_id, discover_mcp_servers(extra if isinstance(extra, list) else None))
    except Exception as exc:
        logger.exception("mcp.discover error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_mcp_refresh_tools(params: dict[str, Any], req_id: Any) -> str:
    """``mcp.refresh_tools`` — Re-register every connected server's tools."""
    result = await _refresh_mcp_tools("mcp.refresh_tools")
    return _ok(req_id, result)


async def _refresh_mcp_tools(caller: str) -> dict[str, Any]:
    """
    Re-register the tools of every connected MCP server.

    Called whenever the server set changes, and once at startup. Failure is
    logged and swallowed: MCP being unavailable must not stop a connect from
    succeeding or the harness from booting, and the return value carries the
    count so the caller can report what actually happened rather than assuming.
    """
    try:
        from sdk.agent.mcp_bridge import register_mcp_tools

        return await register_mcp_tools()
    except Exception as exc:
        logger.warning("Could not refresh MCP tools after %s: %s", caller, exc)
        return {"success": False, "count": 0, "error": str(exc)}


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
        # Registering the new server's tools is what makes connecting mean
        # anything. Before this, a server could be connected, its tools listed
        # in the UI, and not one of them ever offered to the model.
        bridge = await _refresh_mcp_tools("mcp.connect_server")
        return _ok(req_id, {
            "success": True, "server_name": name,
            "persisted": bool(params.get("persist", True)),
            "tools_registered": bridge.get("count", 0),
            "unreachable": bridge.get("unreachable", {}),
        })
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
        # Re-register so the departed server's tools stop being offered. Without
        # this the model keeps seeing names whose every call now fails.
        bridge = await _refresh_mcp_tools("mcp.remove_server")
        return _ok(req_id, {
            "success": True, "removed": removed, "server_name": name,
            "tools_registered": bridge.get("count", 0),
        })
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
        bridge = await _refresh_mcp_tools("mcp.save_servers")
        return _ok(req_id, {"success": True, "tools_registered": bridge.get("count", 0)})
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
        nodes_dir = resolve_active_nodes_dir()
                
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

    # ── GPU ────────────────────────────────────────────────────────────────
    # This section was missing entirely before -- system.info reported CPU/RAM/
    # disk/software but nothing about GPU hardware or whether local inference
    # is actually using it, so the model's own self-description of "no GPU
    # mentioned" was technically honest but misleading about what's actually
    # available. torch.cuda is authoritative for whether Tala's embedded
    # engine can/does use a GPU; nvidia-smi is a fallback so a real GPU still
    # shows up here even in an environment where torch itself isn't installed.
    gpu_info: dict[str, Any] = {"available": False, "devices": []}
    try:
        import torch as _torch
        gpu_info["torch_version"] = _torch.__version__
        if _torch.cuda.is_available():
            gpu_info["available"] = True
            gpu_info["backend"] = "cuda"
            gpu_info["devices"] = [
                {
                    "index": i,
                    "name": _torch.cuda.get_device_name(i),
                    "total_vram_gb": round(_torch.cuda.get_device_properties(i).total_memory / (1024 ** 3), 2),
                    "compute_capability": ".".join(str(x) for x in _torch.cuda.get_device_capability(i)),
                }
                for i in range(_torch.cuda.device_count())
            ]
        else:
            gpu_info["backend"] = "cpu"
    except ImportError:
        gpu_info["backend"] = "torch not installed"
    except Exception as exc:
        gpu_info["error"] = str(exc)

    if not gpu_info["devices"]:
        smi_out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
        if smi_out:
            gpu_info["nvidia_smi_detected"] = smi_out.splitlines()

    # If Tala's embedded model is already loaded in this process, report the
    # actual device it landed on -- the single most direct answer to "is
    # local inference using the GPU right now", not just "could it".
    try:
        from python.inference import embedded_engine as _ee
        loaded_model = getattr(_ee, "_model", None)
        if loaded_model is not None:
            gpu_info["embedded_model_device"] = str(next(loaded_model.parameters()).device)
    except Exception:
        pass

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
        "gpu":         gpu_info,
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
                # Encrypted at rest (UI-080).
                cred_cfg = _decrypt_credential(r["cred_config"] or {}) or {}
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
        outcome = await SearchManager.query_detailed(query)
        return _ok(req_id, {
            "results": outcome.results,
            # Who actually answered. Without this the caller cannot tell a paid
            # API's results from a DuckDuckGo scrape that ran because the key
            # was rejected (UI-018).
            "provider": outcome.provider_name,
            "source": outcome.source,
            "degraded": outcome.degraded,
            "errors": outcome.errors,
            "error": outcome.error_summary() or None,
        })
    except Exception as exc:
        logger.exception("search.query error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


def _storage_credentials_for_storage(
    incoming: Any, existing: dict[str, Any]
) -> dict[str, Any]:
    """
    Encrypt storage credentials, keeping any the panel sent back redacted.

    The panel is shown "***" rather than the real secret, so a plain save would
    otherwise overwrite a working key with three asterisks -- the fix for one
    problem creating a worse one. Redacted fields fall back to what is already
    stored.
    """
    from python.harness.credential_crypto import encrypt_config

    merged = _merge_redacted_auth_config(incoming, existing)
    return encrypt_config(merged)


async def handle_storage_get_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    ``storage.get_providers`` — Retrieve all configured storage providers sorted by priority.
    Credentials are stored as a JSONB blob to support heterogeneous provider types.
    """
    try:
        from python.harness.credential_crypto import decrypt_config

        rows = await db.fetch(
            "SELECT id, name, provider_type, credentials, priority, locked "
            "FROM storage_providers ORDER BY priority ASC"
        )
        providers = [
            {
                "id":            r["id"],
                "name":          r["name"],
                "provider_type": r["provider_type"],
                # decrypt_config, not dict(): the column comes back as a JSON
                # string from the driver, so dict() raises -- and here that
                # would take out the whole panel. It also normalises, so this
                # is the decrypt point for UI-070.
                "credentials":   _redact_auth_config(
                    decrypt_config(r["credentials"]) or {}
                ),
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
        from python.harness.credential_crypto import decrypt_config

        # Read what is stored *before* the table is wiped: the panel is shown
        # "***" instead of each secret, so a save that took the form at face
        # value would replace working credentials with asterisks.
        existing_storage_creds: dict[str, Any] = {}
        for row in await db.fetch("SELECT id, credentials FROM storage_providers"):
            existing_storage_creds[row["id"]] = decrypt_config(row["credentials"]) or {}

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
                        _json.dumps(_storage_credentials_for_storage(
                            creds, existing_storage_creds.get(p["id"], {})
                        )),
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
        # password is a bare TEXT column rather than JSONB (UI-074), so it is
        # wrapped as a single value. Redacted on the way out for the same
        # reason every other secret is: the panel never needs to see it, and
        # a save sends the redaction back rather than the real value.
        providers = [
            {
                "id":       r["id"],
                "name":     r["name"],
                "endpoint": r["endpoint"],
                "username": r["username"],
                "password": "***" if r["password"] else "",
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
        from python.harness.credential_crypto import decrypt_value, encrypt_value

        # Stored passwords, read before the table is wiped. The panel is shown
        # "***", so taking the form at face value would replace a working
        # password with three asterisks.
        existing_db_passwords: dict[str, str] = {}
        for row in await db.fetch("SELECT id, password FROM database_providers"):
            existing_db_passwords[row["id"]] = decrypt_value(row["password"] or "")

        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM database_providers")
                for idx, p in enumerate(providers):
                    is_locked = bool(p.get("locked", False))
                    incoming_password = p.get("password", "")
                    if _is_redacted_secret(incoming_password):
                        incoming_password = existing_db_passwords.get(p["id"], "")
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
                        encrypt_value(incoming_password),
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


from python.harness.credential_crypto import (
    CREDENTIAL_SECRET_FIELDS as _CREDENTIAL_SECRET_FIELDS,
)

# One source, so the redaction list and the encryption list cannot drift --
# a field encrypted but not redacted goes to the renderer in clear, and a field
# redacted but not encrypted sits in the database in clear. Both have happened
# here: the SSH private key block was the first (UI-080), storage secrets the
# second (UI-070).
_SECRET_CONFIG_KEYS = set(_CREDENTIAL_SECRET_FIELDS)


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
            # Secret fields are stored encrypted at rest (UI-080). Decrypt here so
            # redaction and has_secret still see real values; the panel receives
            # the same redacted shape it always did.
            cfg = _decrypt_credential(r["config"] or {}) or {}
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
        # Decrypted, because these are merged back over redacted values the panel
        # sends: comparing a redaction against ciphertext would lose the secret.
        existing_configs = {
            r["id"]: _decrypt_credential(r["config"] or {})
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
                        _json.dumps(_encrypt_credential(cfg)),
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
    # Accept either spelling on input. Levels are normalised on write now
    # (WARN -> WARNING), but a caller filtering for "WARN" -- including the
    # panel, and any code written before the normalisation -- must still find
    # the rows rather than silently getting none.
    if isinstance(levels, list):
        from python.harness.db import _LOG_LEVEL_ALIASES
        levels = [
            _LOG_LEVEL_ALIASES.get(str(lv).strip().upper(), str(lv).strip().upper())
            for lv in levels
        ]
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


#: A status that means "nothing measures this", as distinct from a measurement
#: that came back bad. Nine of the twelve indicators were hardcoded literals
#: (UI-091), so OVERALL was not a diagnosis -- it read DEGRADED forever because
#: two constants said so, while six subsystems read HEALTHY forever for the
#: same reason.
UNKNOWN = "UNKNOWN"


async def _probe_db_health() -> tuple[str, str]:
    try:
        if not db.get_pool():
            return "CRITICAL", "no connection pool"
        await db.fetchval("SELECT 1")
        return "HEALTHY", "SELECT 1 answered"
    except Exception as exc:
        return "CRITICAL", f"{type(exc).__name__}: {exc}"


async def _probe_inference() -> tuple[str, str]:
    try:
        online = await db.fetchval(
            "SELECT COUNT(*) FROM inference_providers WHERE status = 'online'"
        )
        total = await db.fetchval("SELECT COUNT(*) FROM inference_providers")
        if online:
            return "HEALTHY", f"{online} of {total} provider(s) online"
        if total:
            return "DEGRADED", f"0 of {total} provider(s) online"
        return "CRITICAL", "no inference providers configured"
    except Exception as exc:
        return UNKNOWN, f"could not read providers: {type(exc).__name__}"


async def _probe_policy_gate() -> tuple[str, str]:
    try:
        row = await db.fetchrow("SELECT category FROM guardrails_config LIMIT 1")
        category = (row["category"] if row else "none") or "none"
        if category == "none":
            return "MAINTENANCE", "guardrail category is 'none'"
        return "HEALTHY", f"guardrail category '{category}' active"
    except Exception as exc:
        return UNKNOWN, f"could not read guardrails: {type(exc).__name__}"


def _probe_mcp() -> tuple[str, str]:
    try:
        from python.harness.mcp_manager import mcp_manager
        servers = mcp_manager.list_servers()
    except Exception as exc:
        return UNKNOWN, f"mcp manager unavailable: {type(exc).__name__}"
    if not servers:
        # Not an error: plenty of installs run no MCP servers at all.
        return "MAINTENANCE", "no MCP servers configured"
    enabled = [srv for srv in servers if srv.get("enabled", True)]
    if not enabled:
        return "MAINTENANCE", f"{len(servers)} server(s) configured, none enabled"
    return "HEALTHY", f"{len(enabled)} of {len(servers)} server(s) enabled"


async def _probe_search() -> tuple[str, str]:
    try:
        total = await db.fetchval("SELECT COUNT(*) FROM search_providers")
    except Exception as exc:
        return UNKNOWN, f"could not read search providers: {type(exc).__name__}"
    if not total:
        return "MAINTENANCE", "no search providers configured; crawler fallback only"
    return "HEALTHY", f"{total} provider(s) configured"


async def _probe_memory() -> tuple[str, str]:
    """
    Whether Tala's memory can actually be recalled from -- both halves of it.

    Steven asked which memory this indicator meant, "as this memory is the
    html-nn". The honest answer was that it did not mean the HTML-NN at all.
    It counted registrations on ``MemoryManager``, which fronts two providers:
    Postgres full-text over ``memory_index`` (sessions, tasks, profiles,
    skills) and Hindsight, an external OS/screen-context daemon on
    localhost:8080. The HTML-NN store is a *third* thing -- the ChromaDB
    collection in ``retrieval.py`` -- and was not represented here at all.

    So the count said HEALTHY on the strength of two providers, one of which is
    an optional external daemon that is usually not running, while the store
    that actually holds the nodes went unchecked. Both are measured now, and
    the node store is the one that decides the verdict: an unreachable
    Hindsight is a missing convenience, an empty node collection means Tala
    cannot recall her own documents.
    """
    parts: list[str] = []
    node_count = None

    try:
        from python.harness import retrieval
        if retrieval.node_collection is None:
            return "CRITICAL", "ChromaDB node collection is not initialised; HTML-NN recall is dead"
        node_count = retrieval.node_collection.count()
        parts.append(f"{node_count} HTML-NN indexed")
    except Exception as exc:
        return UNKNOWN, f"could not inspect the node collection: {type(exc).__name__}"

    try:
        indexed = await db.fetchval("SELECT COUNT(*) FROM memory_index") or 0
        parts.append(f"{indexed} full-text row(s)")
    except Exception as exc:
        parts.append(f"memory_index unreadable ({type(exc).__name__})")

    try:
        from sdk.agent.memory.manager import get_manager
        providers = getattr(get_manager(), "_providers", []) or []
        parts.append(f"{len(providers)} FTS provider(s) registered")
    except Exception:
        parts.append("provider list unavailable")

    if not node_count:
        # nodes/ is the record; an empty index means nothing can be recalled
        # from it, which looks identical to "found nothing" at every call site.
        return "CRITICAL", "no HTML-NN indexed; " + ", ".join(parts[1:])
    return "HEALTHY", ", ".join(parts)


def _probe_tools() -> tuple[str, str]:
    try:
        from sdk.agent.tools import ToolRegistry
        count = len(ToolRegistry._tools)
    except Exception as exc:
        return UNKNOWN, f"could not read the tool registry: {type(exc).__name__}"
    if not count:
        return "CRITICAL", "no tools registered"
    return "HEALTHY", f"{count} tool(s) registered"


async def _probe_queue() -> tuple[str, str]:
    try:
        pending = await db.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'queued')"
        ) or 0
        running = await db.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status = 'in-progress'"
        ) or 0
    except Exception as exc:
        return UNKNOWN, f"could not read the task queue: {type(exc).__name__}"
    if pending > 50:
        return "CRITICAL", f"{pending} task(s) pending, {running} running"
    if pending > 10:
        return "DEGRADED", f"{pending} task(s) pending, {running} running"
    return "HEALTHY", f"{pending} pending, {running} running"


async def _probe_bootstrap() -> tuple[str, str]:
    """Whether the things startup is supposed to have established are there."""
    missing = []
    if not db.get_pool():
        missing.append("database pool")
    try:
        from sdk.agent.tools import ToolRegistry
        if not ToolRegistry._tools:
            missing.append("tool registry")
    except Exception:
        missing.append("tool registry")
    if not _active_connections:
        # Worth saying, but not a fault: the harness runs headless perfectly
        # well, and a UI client may simply not be open.
        return "MAINTENANCE", "harness up, no UI client connected"
    if missing:
        return "CRITICAL", "not initialised: " + ", ".join(missing)
    return "HEALTHY", f"pool, tools and {len(_active_connections)} client(s) up"


async def _probe_autonomy() -> tuple[str, str]:
    try:
        active = await db.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status = 'in-progress'"
        ) or 0
        # completed_at, not updated_at: the tasks table has no updated_at
        # column, and the probe reported UNKNOWN with UndefinedColumnError
        # rather than quietly counting zero -- which is the point of UNKNOWN.
        recent_failures = await db.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status = 'failed' "
            "AND COALESCE(completed_at, created_at) > NOW() - INTERVAL '1 hour'"
        ) or 0
    except Exception as exc:
        return UNKNOWN, f"could not read task state: {type(exc).__name__}"
    if recent_failures > 3:
        return "DEGRADED", f"{recent_failures} task(s) failed in the last hour"
    if active:
        return "HEALTHY", f"{active} task(s) in progress"
    return "HEALTHY", "idle, no failures in the last hour"


async def _probe_reflection() -> tuple[str, str]:
    """
    Whether errors are reaching the log that reflection reads.

    Reported DEGRADED unconditionally before, which is why OVERALL never moved.
    Recent errors are a real signal; the absence of a reflection subsystem to
    act on them is UI-086 and is not something this probe can invent.
    """
    try:
        errors = await db.fetchval(
            "SELECT COUNT(*) FROM agent_log WHERE level = 'ERROR' "
            "AND created_at > NOW() - INTERVAL '5 minutes'"
        ) or 0
    except Exception as exc:
        return UNKNOWN, f"could not read agent_log: {type(exc).__name__}"
    if errors > 10:
        return "CRITICAL", f"{errors} error(s) logged in the last 5 minutes"
    if errors:
        return "DEGRADED", f"{errors} error(s) logged in the last 5 minutes"
    return "HEALTHY", "no errors logged in the last 5 minutes"


async def _probe_telemetry() -> tuple[str, str]:
    try:
        total = await db.fetchval("SELECT COUNT(*) FROM heartbeats") or 0
        enabled = await db.fetchval(
            "SELECT COUNT(*) FROM heartbeats WHERE enabled = TRUE"
        ) or 0
    except Exception as exc:
        return UNKNOWN, f"could not read heartbeats: {type(exc).__name__}"
    if not total:
        return "MAINTENANCE", "no scheduled heartbeats configured"
    if not enabled:
        return "MAINTENANCE", f"{total} heartbeat(s) configured, none enabled"
    return "HEALTHY", f"{enabled} of {total} heartbeat(s) enabled"


async def handle_logging_get_health(params: dict[str, Any], req_id: Any) -> str:
    """
    ``logging.get_system_health`` — measured health for the twelve subsystems.

    Every indicator now reports what was actually observed, and says on what
    basis. Nine of the twelve were hardcoded literals (UI-091): OVERALL read
    DEGRADED forever because two constants said so, and six subsystems read
    HEALTHY forever for the same reason -- a panel that cannot change is not a
    diagnostic, it is decoration that looks like one.

    Where no signal exists the answer is UNKNOWN, not HEALTHY. Reporting a
    subsystem healthy because nobody measured it is the failure this whole
    audit keeps finding, and it is worse here than anywhere else, because this
    is the screen you would look at to find out whether anything is wrong.
    """
    import asyncio as _asyncio

    services: dict[str, str] = {}
    details: dict[str, str] = {}

    async def record(name: str, probe) -> None:
        """
        Run one probe, and never let it take the panel down with it.

        The probe is passed as a *callable*, not as an already-evaluated
        result: calling it at the call site meant a synchronous probe that
        raised did so before this try block existed, so the exception escaped
        and the whole health panel returned an error -- the screen you check
        when things are broken, broken by one thing being broken.
        """
        try:
            outcome = probe()
            if _asyncio.iscoroutine(outcome):
                outcome = await outcome
            status, detail = outcome
        except Exception as exc:
            status, detail = UNKNOWN, f"probe raised {type(exc).__name__}: {exc}"
        services[name] = status
        details[name] = detail

    await record("DB_HEALTH_SERVICE", _probe_db_health)
    await record("INFERENCE_SERVICE", _probe_inference)
    await record("POLICY_GATE", _probe_policy_gate)
    await record("MCP_TOOL_AVAILABILITY", _probe_mcp)
    await record("SEARCH_RETRIEVAL_SERVICE", _probe_search)
    await record("MEMORY_AUTHORITY_SERVICE", _probe_memory)
    await record("TOOL_EXECUTION_COORDINATOR", _probe_tools)
    await record("QUEUE_BACKLOG_PRESSURE", _probe_queue)
    await record("BOOTSTRAP_RUNTIME_SERVICES", _probe_bootstrap)
    await record("AUTONOMY_ORCHESTRATOR", _probe_autonomy)
    await record("REFLECTION_SERVICE", _probe_reflection)
    await record("DASHBOARD_TELEMETRY", _probe_telemetry)

    criticals = [k for k, v in services.items() if v == "CRITICAL"]
    degradeds = [k for k, v in services.items() if v == "DEGRADED"]
    maintenances = [k for k, v in services.items() if v == "MAINTENANCE"]
    unknowns = [k for k, v in services.items() if v == UNKNOWN]

    if criticals:
        overall, mode = "CRITICAL", "CRITICAL_FAILURE"
    elif degradeds:
        overall, mode = "DEGRADED", "DEGRADED_AUTONOMY"
    elif maintenances:
        overall, mode = "MAINTENANCE", "MAINTENANCE_REQUIRED"
    elif unknowns:
        # Not HEALTHY. Everything that was measured passed, but something went
        # unmeasured, and saying HEALTHY here would be the old lie in a new place.
        overall, mode = "PARTIAL", "PARTIALLY_OBSERVED"
    else:
        overall, mode = "HEALTHY", "FULL_AUTONOMY"

    return _ok(req_id, {
        "services": services,
        # What each status is based on, so the panel can explain itself and
        # Tala can read her own health rather than a bare colour (UI-086).
        "details": details,
        "overall": overall,
        "mode": mode,
        "measured": len(services) - len(unknowns),
        "unmeasured": unknowns,
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
        rows = await load_inference_providers()
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
        outcome = await SearchManager.query_detailed(query, provider_id=provider_id)
        if outcome.degraded:
            logger.warning(
                "research.search: serving crawler results for %r after %d provider "
                "failure(s); the panel is told.", query, len(outcome.errors),
            )
        return _ok(req_id, {
            "results": outcome.results,
            "provider": outcome.provider_name,
            "source": outcome.source,
            "degraded": outcome.degraded,
            "errors": outcome.errors,
            "error": outcome.error_summary() or None,
        })
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

        # Persist the generated HTML-NN to nodes/ ourselves rather than
        # relying on the model to write it back out in a follow-up tool
        # call. Confirmed live (calculator.md incident): a model asked to
        # separately re-emit a large generated artifact as a file write
        # unreliably paraphrases, truncates, or skips the step entirely.
        # This is the autonomous research.crawl_and_summarize path (the
        # model calls it directly mid-chat via <call:research.crawl_and...
        # />); unlike the manual "Save Selected as Neural Node" UI button
        # (research.distill_to_node), there is no separate client-side
        # file:write step for this path, so if the harness doesn't persist
        # the node here, it is never saved anywhere -- the well-formed
        # HTML-NN the LLM just produced simply evaporates once the tool
        # result is consumed as chat context.
        node_path: str | None = None
        node_id: str | None = None
        persist_error: str | None = None
        html_document = extract_html_nn_document(generated_raw)
        if html_document:
            try:
                m_id = re.search(r'data-node-id="([^"]+)"', html_document)
                node_id = m_id.group(1) if m_id else f"concept-{subject_slug}"
                timestamp_suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                nodes_dir = resolve_nodes_dir()
                node_file = nodes_dir / f"{node_id}-{timestamp_suffix}.html"
                node_file.write_text(html_document, encoding="utf-8")
                node_path = str(node_file)
                logger.info("research.crawl_and_summarize: persisted HTML-NN to %s", node_path)

                try:
                    # NOTE: must import the implementation directly rather
                    # than call the module-level handle_memory_reindex_node
                    # defined further down in this same file -- that one is
                    # the (params, req_id) RPC wrapper, not the callable
                    # single-arg implementation.
                    from python.harness.memory import handle_memory_reindex_node as _reindex_node_impl
                    await _reindex_node_impl({"node_path": node_path})
                except Exception as reindex_exc:
                    logger.warning("research.crawl_and_summarize: failed to reindex node: %s", reindex_exc)
            except Exception as write_exc:
                persist_error = str(write_exc)
                logger.warning("research.crawl_and_summarize: failed to persist HTML-NN: %s", write_exc)
        else:
            persist_error = "LLM response did not contain a recognizable HTML-NN document; nothing was persisted."
            logger.warning("research.crawl_and_summarize: %s", persist_error)

        return _ok(req_id, {
            "html_nn": generated_raw,
            "node_path": node_path,
            "node_id": node_id,
            "persisted": node_path is not None,
            "persist_error": persist_error,
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
        # user_data_path is an explicit per-request RPC param override; falls
        # back to the standard TALA_USER_DATA/APPDATA/app-relative resolver.
        user_data = params.get("user_data_path")
        if user_data:
            nodes_dir = Path(user_data) / "nodes"
        else:
            nodes_dir = resolve_active_nodes_dir()

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
            try:
                await engine.run_goal(
                    goal,
                    policy=policy,
                    adapter_packet=adapter_packet,
                    pre_decomposed_tasks=task_items,  # avoid a second LLM decomposition round-trip
                )
            except Exception as _bg_exc:
                logger.exception(
                    "task.submit: GoalEngine background task crashed for task_id=%s", parent_task_id
                )
                # Mark the parent task as FAILED so the UI doesn't hang on "in_progress"
                try:
                    _err_payload = json.dumps({"error": str(_bg_exc)})
                    await db.execute(
                        "UPDATE tasks SET status='failed', result=$1::jsonb WHERE id=$2",
                        _err_payload,
                        parent_task_id,
                    )
                except Exception:
                    pass
                # Broadcast failure to the UI
                try:
                    from sdk.agent.telemetry import LiveStatusReporter
                    await LiveStatusReporter.broadcast_status(
                        task_id=parent_task_id,
                        phase="goal_engine",
                        status="failed",
                        message=f"Task engine crashed: {_bg_exc}",
                        details={"error": str(_bg_exc), "traceback": traceback.format_exc()},
                    )
                except Exception:
                    pass

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
        records = await db.fetch("SELECT id, parent_id, description, status, payload, result, assigned_agent_id, model_override, worktree_path, branch_name FROM tasks ORDER BY created_at DESC LIMIT 50")
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
                "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
                "result": json.loads(r["result"]) if isinstance(r["result"], str) else r["result"]
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
        # Fix: add 3s timeout — git rev-parse can hang indefinitely on Windows with a locked repo
        try:
            stdout, _ = await asyncio.wait_for(git_check.communicate(), timeout=3.0)
        except asyncio.TimeoutError:
            git_check.kill()
            logger.warning("git rev-parse timed out after 3s; treating as non-git workspace.")
            stdout = b""
        is_git = (git_check.returncode == 0 and stdout.decode().strip() == "true")
        
        if is_git:
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", str(worktree_path.absolute()), "-b", branch_name,
                cwd=str(base_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                logger.warning("git worktree command timed out after 3s; falling back to direct workspace mode.")
                return _err(req_id, JsonRpcError.INTERNAL_ERROR, "git worktree timed out")
            if proc.returncode != 0:
                logger.warning(f"git worktree failed: {stderr.decode()}")
                return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"git worktree failed: {stderr.decode()}")
        else:
            import shutil
            # Fix: run copytree off the event loop thread so it doesn't block asyncio.
            # Also skip node_modules and .venv to avoid copying gigabytes of dependencies.
            await asyncio.wait_for(
                asyncio.to_thread(
                    shutil.copytree,
                    str(base_dir),
                    str(worktree_path),
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        '.git', 'scratch', '__pycache__', 'node_modules', '.venv', '*.pyc'
                    ),
                ),
                timeout=30.0,
            )
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

        if params.get("reload"):
            # reset(), not reload(): reload only overwrites, so a topology
            # whose node was deleted or renamed on disk would linger.
            TopologyRegistry.reset()
        from sdk.agent.gated_verifier import GatedVerifier

        return _ok(req_id, {
            "topologies": [t.to_wire() for t in TopologyRegistry.list_all()],
            # Sent so the editor's gate dropdown is generated from the rules
            # the verifier actually implements. A hardcoded UI list would let
            # someone author an edge on a rule that can never pass, which
            # stalls that branch silently on every run.
            "known_gate_conditions": sorted(GatedVerifier.KNOWN_RULES),
        })
    except Exception as exc:
        # Logged like its siblings (topology.create, topology.delete). With
        # reload=true this calls TopologyRegistry.reset(), which does node-file
        # I/O, HTML parsing and spec validation -- any of which can raise. The
        # client used to get a bare str(exc) with no file name and no traceback
        # anywhere in the log.
        logger.exception("swarm.topology.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_topology_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``swarm.topology.delete`` — remove a hand-authored swarm's HTML-NN.

    A topology that also has a built-in fallback reappears in its original
    shape afterwards; the response says so explicitly rather than reporting a
    clean delete the user can see did not happen.
    """
    topology_id = params.get("topology_id")
    if not topology_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "topology_id is required")

    try:
        from sdk.agent.topology_nodes import delete_topology_node

        result = delete_topology_node(str(topology_id))
    except Exception as exc:
        logger.exception("swarm.topology.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

    if not result["node_deleted"]:
        return _err(
            req_id, JsonRpcError.INVALID_PARAMS,
            f"No authored definition to delete for topology {topology_id!r} "
            "(it is a built-in, which has no node file behind it)",
        )
    return _ok(req_id, result)

async def handle_swarm_topology_create(params: dict[str, Any], req_id: Any) -> str:
    """
    Create (or update) a swarm topology by writing its HTML-NN definition.

    The node file *is* the swarm: this is the only creation path, so a
    topology that ran once is on disk to be listed, inspected, edited and run
    again rather than existing only in memory for one process lifetime.
    Validation happens before anything is written, and the file is re-parsed
    after writing, so a topology can never be "saved" in a state the loader
    would refuse to read back.
    """
    from sdk.agent.topology import TopologySpecError, topology_from_spec
    from sdk.agent.topology_nodes import write_topology_node

    spec = params.get("topology") or params
    try:
        topology = topology_from_spec(spec)
    except TopologySpecError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))

    try:
        result = write_topology_node(
            topology,
            semantic_memory=params.get("semantic_memory"),
            created_by=str(params.get("created_by") or "tala-system"),
            overwrite=bool(params.get("overwrite", True)),
        )
    except FileExistsError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("swarm.topology.create error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

    return _ok(req_id, {**result, "topology": topology.to_wire()})

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

async def handle_swarm_fleet_status(params: dict[str, Any], req_id: Any) -> str:
    try:
        from sdk.agent.orchestrator_cli import get_active_tasks, get_port_allocations

        tasks = await get_active_tasks()
        ports = await get_port_allocations()

        result_tasks = [
            {
                "id": t["id"],
                "description": t["description"],
                "status": t["status"],
                "worktree_path": t["worktree_path"],
                "branch_name": t["branch_name"],
                "port": ports.get(t["id"]),
            }
            for t in tasks
        ]

        return _ok(req_id, {"success": True, "tasks": result_tasks})
    except Exception as exc:
        logger.exception("swarm.fleet_status error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_fleet_pause(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    try:
        from sdk.agent.orchestrator_cli import pause_task
        await pause_task(task_id)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_fleet_resume(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    try:
        from sdk.agent.orchestrator_cli import resume_task
        await resume_task(task_id)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_swarm_fleet_terminate(params: dict[str, Any], req_id: Any) -> str:
    task_id = params.get("task_id")
    if not task_id: return _err(req_id, JsonRpcError.INVALID_PARAMS, "task_id required")
    try:
        from sdk.agent.orchestrator_cli import terminate_task
        await terminate_task(task_id)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

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
    action = params.get("action", "").strip() # pause, resume, abort, approve, reject
    if not task_id or not action:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' and 'action' are required")
        
    try:
        from sdk.agent.gates import trigger_emergency_halt
        if action in ("approve", "reject"):
            # Self-edit proposals sit in the task queue as status 'review' so
            # Mission Control shows them without a separate approval surface.
            from sdk.agent.self_edit_approval import decide
            outcome = await decide(task_id, approve=(action == "approve"))
            return _ok(req_id, outcome) if outcome.get("success") else _err(
                req_id, JsonRpcError.INVALID_PARAMS, outcome.get("detail", "decision failed"))
        if action == "abort":
            await trigger_emergency_halt()
            await db.execute("UPDATE tasks SET status = 'failed' WHERE id = $1 OR parent_id = $1", task_id)
        elif action == "pause":
            # 'paused', NOT 'blocked'. resume_suspended_goals selects
            # "status = 'blocked' AND parent_id IS NULL" at startup and
            # relaunches every match, because 'blocked' means "the system
            # suspended this for budget and it should carry on". Writing
            # 'blocked' here made a human pause indistinguishable from that, so
            # pausing a task queued it for automatic relaunch on the next app
            # start -- a task could not be made to stay stopped.
            await db.execute("UPDATE tasks SET status = 'paused' WHERE id = $1 OR parent_id = $1", task_id)
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


#: Statuses where the goal loop is actively holding the row. Editing or deleting
#: one of these races the loop, which writes its own status back afterwards --
#: the edit appears to work, then silently reverts, or the row is recreated.
#: Callers must abort the task first, or pass force to accept the race.
_TASK_LIVE_STATUSES = ("in_progress", "running")


async def handle_task_delete(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.delete`` — Remove one task and its children.

    task.clear TRUNCATEs the whole table, which is the only removal that
    existed; there was no way to drop a single stuck row without wiping every
    task and all of its telemetry.
    """
    task_id: str = params.get("task_id", "").strip()
    force: bool = bool(params.get("force", False))
    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' is required")

    try:
        row = await db.fetchrow("SELECT id, status FROM tasks WHERE id = $1", task_id)
        if row is None:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"No task with id {task_id!r}")

        if row["status"] in _TASK_LIVE_STATUSES and not force:
            return _err(
                req_id,
                JsonRpcError.INVALID_PARAMS,
                f"Task is {row['status']}; the goal loop is still writing to it. "
                "Abort it first (task.control action='abort'), or pass force=true.",
            )

        # Children go explicitly rather than via ON DELETE SET NULL, which would
        # orphan them into the queue as parentless rows that nothing ever runs.
        deleted = await db.execute("DELETE FROM tasks WHERE id = $1 OR parent_id = $1", task_id)
        return _ok(req_id, {"success": True, "task_id": task_id, "result": deleted})
    except Exception as exc:
        logger.exception("task.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_task_update(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.update`` — Edit a task's description or status.

    Deliberately narrow: description and status only. Dependencies, payload and
    parentage describe a plan the goal engine is executing, and rewriting those
    from the UI produces a graph the engine never planned.
    """
    task_id: str = params.get("task_id", "").strip()
    description = params.get("description")
    status = params.get("status")
    force: bool = bool(params.get("force", False))

    if not task_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'task_id' is required")
    if description is None and status is None:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "Nothing to update: pass 'description' or 'status'")

    valid_statuses = {"pending", "in_progress", "complete", "failed", "blocked", "paused"}
    if status is not None and status not in valid_statuses:
        return _err(
            req_id, JsonRpcError.INVALID_PARAMS,
            f"'status' must be one of {sorted(valid_statuses)}",
        )

    try:
        row = await db.fetchrow("SELECT id, status FROM tasks WHERE id = $1", task_id)
        if row is None:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, f"No task with id {task_id!r}")

        if row["status"] in _TASK_LIVE_STATUSES and not force:
            return _err(
                req_id,
                JsonRpcError.INVALID_PARAMS,
                f"Task is {row['status']}; the goal loop writes its own status back, so an "
                "edit made now would silently revert. Pause or abort it first, or pass force=true.",
            )

        sets, args = [], []
        if description is not None:
            desc = str(description).strip()
            if not desc:
                return _err(req_id, JsonRpcError.INVALID_PARAMS, "'description' cannot be empty")
            args.append(desc)
            sets.append(f"description = ${len(args)}")
        if status is not None:
            args.append(status)
            sets.append(f"status = ${len(args)}")
        args.append(task_id)

        await db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ${len(args)}", *args)
        updated = await db.fetchrow(
            "SELECT id, description, status FROM tasks WHERE id = $1", task_id
        )
        return _ok(req_id, {"success": True, "task": dict(updated) if updated else None})
    except Exception as exc:
        logger.exception("task.update error")
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
        # create_checkpoint returns None when no snapshot was written. This
        # reported {"success": True} unconditionally, so the UI showed a
        # checkpoint the workspace did not actually have.
        if not stash_id:
            return _ok(req_id, {
                "success": False,
                "checkpoint_id": None,
                "error": (
                    f"checkpoint {checkpoint_name!r} was not created; see the "
                    "harness log for the snapshot failure"
                ),
            })
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
        code = await CodeGenerator.generate_template(file_type, class_name, params.get("options"))
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


async def handle_code_registry_get(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.registry_get`` — Retrieve a key from CodeRegistry.
    """
    key = params.get("key")
    if not key or not isinstance(key, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'key' is required")
    try:
        from sdk.agent.code_registry import CodeRegistry
        val = await CodeRegistry.get(key)
        return _ok(req_id, {"key": key, "value": val})
    except Exception as exc:
        logger.exception("code.registry_get error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_registry_set(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.registry_set`` — Set a key in CodeRegistry.
    """
    key = params.get("key")
    value = params.get("value")
    if not key or not isinstance(key, str):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'key' is required")
    try:
        from sdk.agent.code_registry import CodeRegistry
        await CodeRegistry.set(key, value)
        return _ok(req_id, {"key": key, "value": value})
    except Exception as exc:
        logger.exception("code.registry_set error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_code_registry_list(params: dict[str, Any], req_id: Any) -> str:
    """
    ``code.registry_list`` — List keys matching prefix in CodeRegistry.
    """
    prefix = params.get("prefix")
    try:
        from sdk.agent.code_registry import CodeRegistry
        keys = await CodeRegistry.list_keys(prefix)
        return _ok(req_id, {"keys": keys})
    except Exception as exc:
        logger.exception("code.registry_list error")
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


async def handle_system_capabilities(params: dict[str, Any], req_id: Any) -> str:
    """
    ``system.capabilities`` — Live manifest of every registered RPC method and
    agent tool. Read-only introspection, built fresh on every call from the
    actual _HANDLERS table and ToolRegistry -- never a hand-maintained doc.
    """
    try:
        from python.harness.capabilities import get_capability_manifest

        return _ok(req_id, get_capability_manifest())
    except Exception as exc:
        logger.exception("system.capabilities error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


# Admin-secret gate (moved to admin_auth.py so mcp_server.py can reuse it
# without importing this whole module). Local aliases kept so the many
# existing call sites below don't need to change.
from python.harness.admin_auth import (
    harness_admin_secret_path as _harness_admin_secret_path,
    load_or_create_harness_admin_secret as _load_or_create_harness_admin_secret,
    require_harness_admin_secret as _require_harness_admin_secret,
)


async def handle_system_trigger_update(params: dict[str, Any], req_id: Any) -> str:
    """
    ``system.trigger_update`` — Pull latest code from the repository and notify clients.
    """
    import asyncio
    if not _require_harness_admin_secret(params):
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, "Unauthorized: missing or invalid harness admin secret.")
    try:
        proc = await asyncio.create_subprocess_shell(
            "git pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode().strip()
        err = stderr.decode().strip()

        # A failed pull used to broadcast "update available" and return
        # status: success anyway -- returncode was never inspected. Clients then
        # prompted the user, downloaded an archive of code that had not changed,
        # and applied it. The push notification is the whole delivery mechanism,
        # so announcing an update that did not happen is worse than announcing
        # nothing.
        if proc.returncode != 0:
            logger.error("system.trigger_update: git pull failed (%s): %s", proc.returncode, err)
            return _err(
                req_id,
                JsonRpcError.INTERNAL_ERROR,
                f"git pull failed with exit code {proc.returncode}: {err or out}",
            )

        revision = ""
        try:
            rev_proc = await asyncio.create_subprocess_shell(
                "git rev-parse HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rev_out, _ = await rev_proc.communicate()
            if rev_proc.returncode == 0:
                revision = rev_out.decode().strip()
        except Exception:
            pass

        # The revision travels with the notification so a client can tell whether
        # it already has this version instead of re-downloading the whole archive
        # on every announcement.
        delivered = await broadcast_notification(
            "system.update_available", {"revision": revision}
        )

        # broadcast_notification reaches only clients connected at this instant --
        # there is no queue and no replay. Saying how many were reached is the
        # difference between "pushed to nobody" and "pushed", which otherwise
        # look identical from here.
        if not delivered:
            logger.warning(
                "system.trigger_update: pulled %s but no clients were connected, so "
                "nobody was notified. They will not learn about this update until "
                "the next push.",
                revision[:8] or "HEAD",
            )

        return _ok(req_id, {
            "status": "success",
            "revision": revision,
            "clients_notified": delivered,
            "stdout": out,
            "stderr": err,
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
    if not _require_harness_admin_secret(params):
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, "Unauthorized: missing or invalid harness admin secret.")
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
            target_files=params.get("target_files"),
            replacements=params.get("replacements"),
            file_writes=params.get("file_writes"),
            validation_commands=params.get("validation_commands"),
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

async def handle_voice_speak(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.voice import synthesize_speech
    try:
        text = str(params.get("text", "")).strip()
        if not text:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "Missing required param: text")
        result = await synthesize_speech(
            text,
            provider_id=params.get("provider_id") or None,
            voice=params.get("voice") or None,
        )
        return _ok(req_id, result)
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_voice_backends(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.voice import list_backends
    try:
        return _ok(req_id, await list_backends())
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_channels_list(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    try:
        rows = await db.fetch("SELECT id, platform, name, enabled, config FROM chat_channels ORDER BY created_at ASC")
        channels = []
        for r in rows:
            cfg = r["config"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            channels.append({
                "id": r["id"],
                "platform": r["platform"],
                "name": r["name"],
                "enabled": r["enabled"],
                "config": cfg or {},
            })
        return _ok(req_id, {"channels": channels})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_channels_save(params: dict, req_id: any) -> str:
    """Create or update a messaging channel, then hot-reload all adapters."""
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.channels import ChannelManager, _ADAPTERS
    try:
        channel_id = str(params.get("id") or "").strip() or f"chan-{uuid.uuid4().hex[:8]}"
        platform = str(params.get("platform", "")).strip().lower()
        if platform not in _ADAPTERS:
            return _err(req_id, JsonRpcError.INVALID_PARAMS,
                        f"Unknown platform '{platform}'. Supported: {', '.join(sorted(_ADAPTERS))}")
        name = str(params.get("name", "")).strip() or platform
        enabled = bool(params.get("enabled", False))
        config = params.get("config") or {}
        if not isinstance(config, dict):
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "'config' must be an object")

        await db.execute(
            """
            INSERT INTO chat_channels (id, platform, name, enabled, config)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (id) DO UPDATE
                SET platform = $2, name = $3, enabled = $4, config = $5::jsonb
            """,
            channel_id, platform, name, enabled, json.dumps(config),
        )
        await ChannelManager.get().reload()
        return _ok(req_id, {"success": True, "id": channel_id, "status": ChannelManager.get().status()})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_channels_delete(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.channels import ChannelManager
    try:
        channel_id = str(params.get("id", "")).strip()
        if not channel_id:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "Missing required param: id")
        await db.execute("DELETE FROM chat_channels WHERE id = $1", channel_id)
        await ChannelManager.get().reload()
        return _ok(req_id, {"success": True})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def handle_channels_status(params: dict, req_id: any) -> str:
    from python.harness.server import _ok, _err, JsonRpcError
    from python.harness.channels import ChannelManager
    try:
        return _ok(req_id, {"status": ChannelManager.get().status()})
    except Exception as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))

async def _browser_bridge(channel: str, payload: dict, req_id: any, what: str) -> str:
    """
    Shared plumbing for the two webview handlers that need an answer back.

    Both used to broadcast into the void and return ``{"status":
    "broadcast_sent"}``, which any caller checking for success read as success
    (UI-014, UI-015). Now every outcome is distinguishable: no window open, no
    handler listening, the JS threw, or a real result.
    """
    import asyncio
    from python.harness.config import settings
    from python.harness import ui_bridge

    if settings.IS_HEADLESS:
        # _err, not _ok: headless genuinely cannot do this, and returning ok
        # with an "error" key inside it is the same lie in a smaller font.
        return _err(
            req_id, JsonRpcError.INTERNAL_ERROR,
            "Tala is running in embedded/headless mode and has no Browser UI. "
            "Use the headless browser tools (browser_read, browser_navigate) instead.",
        )

    try:
        result = await ui_bridge.request(channel, payload)
        return _ok(req_id, {"success": True, "result": result})
    except ui_bridge.NoListener as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
    except asyncio.TimeoutError:
        return _err(
            req_id, JsonRpcError.INTERNAL_ERROR,
            f"The UI did not respond to {what} within the timeout.",
        )
    except ui_bridge.BridgeError as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, f"{what} failed in the page: {exc}")
    except Exception as exc:
        logger.exception("%s error", channel)
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


def _row_get(row: Any, column: str, default: Any = None) -> Any:
    """Read a column from an asyncpg Record or a plain dict, tolerating absence."""
    try:
        return row[column]
    except (KeyError, IndexError, TypeError):
        return default


async def load_inference_providers() -> list[dict[str, Any]]:
    """
    Every inference provider, with its config decrypted.

    The same SELECT was copy-pasted at six call sites, so wiring decryption
    into each would have meant six chances to miss one -- and a missed one does
    not fail loudly, it just hands an AES envelope to an HTTP client as though
    it were an API key. One loader, one decrypt point (UI-047).

    Rows come back as plain dicts rather than asyncpg Records so callers can
    hold the decrypted config without the row's lifetime mattering.
    """
    from python.harness.credential_crypto import decrypt_config

    rows = await db.fetch(
        "SELECT id, type, name, priority, status, config, locked "
        "FROM inference_providers ORDER BY priority ASC"
    )
    providers: list[dict[str, Any]] = []
    for r in rows:
        providers.append({
            "id": r["id"],
            "type": r["type"],
            "name": r["name"],
            "priority": r["priority"],
            "status": r["status"],
            "config": decrypt_config(r["config"]) or {},
            # locked is a display/priority flag, not something to fail a whole
            # provider list over if a caller hands us a partial row.
            "locked": _row_get(r, "locked", False),
        })
    return providers


async def handle_search_test_provider(params: dict[str, Any], req_id: Any) -> str:
    """
    ``search.test_provider`` — actually ask a search provider whether it works.

    The panel rendered a fixed green ACTIVE for every provider with no probe of
    any kind (UI-064), which compounded UI-018: a Tavily key that starts
    returning 401 falls through to DuckDuckGo crawling, and this panel reported
    ACTIVE the whole time. There was no surface anywhere in the app that would
    reveal the provider was dead.

    Deliberately on demand rather than on every panel load: a probe is a
    billable request against the user's own quota, and spending it without
    being asked is its own kind of rudeness.
    """
    provider_id = str(params.get("id") or "").strip()
    if not provider_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")

    try:
        from python.harness.search_manager import SearchManager

        outcome = await SearchManager.query_detailed(
            str(params.get("query") or "tala connectivity check"),
            provider_id=provider_id,
        )
        if outcome.source == "provider" and outcome.provider_name:
            return _ok(req_id, {
                "state": "ok",
                "detail": f"{outcome.provider_name} answered with "
                          f"{len(outcome.results)} result(s).",
            })
        # Falling through to the crawler *is* the failure this panel existed to
        # hide, so it is reported as one rather than as a result.
        return _ok(req_id, {
            "state": "failed",
            "detail": outcome.error_summary() or
                      "The provider did not answer; results came from the crawler.",
        })
    except Exception as exc:
        logger.exception("search.test_provider error")
        return _ok(req_id, {"state": "failed", "detail": f"{type(exc).__name__}: {exc}"})


async def handle_database_test_provider(params: dict[str, Any], req_id: Any) -> str:
    """
    ``database.test_provider`` — open a real connection to a configured database.

    The panel rendered a fixed ONLINE (UI-073) while the SYSTEM panel two tabs
    away performed a genuine PostgreSQL health check. The signal existed; this
    panel simply did not use it.

    Routed through database_clients so the driver question -- is this kind even
    supported in this install -- is answered in one place rather than inline
    here, the way storage does it.
    """
    provider_id = str(params.get("id") or "").strip()
    if not provider_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")

    try:
        from python.harness.credential_crypto import decrypt_value
        from python.harness.database_clients import DatabaseError, probe

        row = await db.fetchrow(
            "SELECT id, name, endpoint, username, password, database "
            "FROM database_providers WHERE id = $1", provider_id,
        )
        if not row:
            return _ok(req_id, {"state": "failed", "detail": "No such provider."})

        detail = await probe({
            "name": row["name"],
            "endpoint": row["endpoint"],
            "username": row["username"],
            "password": decrypt_value(row["password"] or ""),
            "database": row["database"],
        })
        return _ok(req_id, {"state": "ok", "detail": detail})
    except DatabaseError as exc:
        # The reason matters more than the fact: "password authentication
        # failed" and "connection refused" need different responses.
        return _ok(req_id, {"state": "failed", "detail": str(exc)})
    except Exception as exc:
        logger.exception("database.test_provider error")
        return _ok(req_id, {"state": "failed", "detail": f"{type(exc).__name__}: {exc}"})


def _principal_from(params: dict[str, Any]):
    from python.harness.access import Principal
    return Principal(
        str(params.get("principal_kind") or "peer").strip().lower(),
        str(params.get("principal_id") or "").strip(),
    )


async def handle_chain_retire_key(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.retire_key`` — retire this node's own signing key, permanently.

    The only revocation that propagates. A revocation signed by the key it
    retires is a fact anyone can check, so every node acts on it without having
    to trust whoever relayed it; "I no longer trust your key" is an opinion, and
    an opinion on a shared chain would let any signer evict any other.

    Irreversible, and gated on repeating the fingerprint back, because there is
    no un-retire — a retirement that can be withdrawn is not one.
    """
    try:
        from python.harness import chain_keys

        identity = await chain_keys.identity()
        if not identity.get("available"):
            return _err(req_id, JsonRpcError.INTERNAL_ERROR,
                        f"the local chain is unreachable: "
                        f"{identity.get('reason', '')}")

        confirm = str(params.get("confirm") or "").strip()
        if confirm != identity["fingerprint"]:
            return _err(
                req_id, JsonRpcError.INVALID_PARAMS,
                f"This retires {identity['fingerprint']} permanently and "
                f"cannot be undone. To proceed, send confirm: "
                f"\"{identity['fingerprint']}\".")

        result = await chain_keys.retire_own_key(str(params.get("reason") or ""))
        if not result.get("retired"):
            return _err(req_id, JsonRpcError.INTERNAL_ERROR,
                        result.get("reason", "the key could not be retired"))
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("chain.retire_key error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_retired_keys(params: dict[str, Any], req_id: Any) -> str:
    """``chain.retired_keys`` — keys known to have retired themselves."""
    try:
        from python.harness import chain_keys

        retired = await chain_keys.retired_keys()
        return _ok(req_id, {
            "retired": retired,
            "count": len(retired),
            "note": (
                "Learned from the shared chain when reconciling. A retired key "
                "is dropped as a permitted signer automatically, and blocks it "
                "signed before retiring stay valid."
            ),
        })
    except Exception as exc:
        logger.exception("chain.retired_keys error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_identity(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.identity`` — this node's own signing key, to hand to a peer.

    The fingerprint is the part that matters: it is short enough to read down a
    phone line and long enough that nobody can produce a second key matching
    it, which is what makes the out-of-band comparison worth doing.
    """
    try:
        from python.harness import chain_keys

        return _ok(req_id, await chain_keys.identity())
    except Exception as exc:
        logger.exception("chain.identity error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_fetch_key(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.fetch_key`` — obtain a peer's signing key and test possession.

    Records an offer. It does not trust anything: a key that arrived over the
    network is a claim, and admitting it is the owner's deliberate act after
    comparing the fingerprint through a channel neither node controls.
    """
    endpoint = str(params.get("endpoint") or "").strip()
    if not endpoint:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'endpoint' is required")
    try:
        from python.harness import chain_keys

        result = await chain_keys.fetch(endpoint, str(params.get("label") or ""))
        if result.get("fetched") and not result.get("proven"):
            await db.log_to_db(
                "WARNING",
                f"Obtained a signing key from {endpoint} that did not prove "
                f"possession: {result.get('note', '')}",
                {"fingerprint": result.get("fingerprint", "")},
            )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("chain.fetch_key error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_offer_key(params: dict[str, Any], req_id: Any) -> str:
    """``chain.offer_key`` — record a key handed over directly."""
    try:
        from python.harness import chain_keys

        result = await chain_keys.offer(
            str(params.get("public_key") or ""),
            str(params.get("label") or ""),
            str(params.get("source") or "pasted"),
        )
        if not result.get("offered"):
            return _err(req_id, JsonRpcError.INVALID_PARAMS,
                        result.get("reason", "that key could not be used"))
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("chain.offer_key error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_key_offers(params: dict[str, Any], req_id: Any) -> str:
    """``chain.key_offers`` — keys obtained, and what was decided about each."""
    try:
        from python.harness import chain_keys

        listed = await chain_keys.offers(str(params.get("status") or ""))
        return _ok(req_id, {
            "offers": listed,
            "note": (
                "Fetching a key and trusting it are separate. Compare a "
                "fingerprint with its owner over a channel neither of you "
                "controls before admitting it."
            ),
        })
    except Exception as exc:
        logger.exception("chain.key_offers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_trust_key(params: dict[str, Any], req_id: Any) -> str:
    """``chain.trust_key`` — admit an offered key as a permitted signer."""
    fingerprint = str(params.get("fingerprint") or "").strip()
    if not fingerprint:
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'fingerprint' is required")
    try:
        from python.harness import chain_keys

        result = await chain_keys.trust(fingerprint)
        if not result.get("trusted"):
            return _err(req_id, JsonRpcError.INVALID_PARAMS,
                        result.get("reason", "that key could not be trusted"))
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("chain.trust_key error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_reject_key(params: dict[str, Any], req_id: Any) -> str:
    """``chain.reject_key`` — refuse a key, or withdraw one already trusted."""
    fingerprint = str(params.get("fingerprint") or "").strip()
    if not fingerprint:
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'fingerprint' is required")
    try:
        from python.harness import chain_keys

        return _ok(req_id, await chain_keys.reject(fingerprint))
    except Exception as exc:
        logger.exception("chain.reject_key error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_authorities(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.authorities`` — who may sign a block on the shared chain.

    Verifying a signature against the public key the block itself carries
    proves only that the block is internally consistent: anyone can generate a
    key, sign with it, and produce a block that verifies. On a chain other
    people write to that is no bar at all, so signers are checked against this
    list.
    """
    try:
        from python.harness import chains

        # Active and stopped are reported separately. A stopped signer stays in
        # the recognised set on purpose -- it signed valid history, and striking
        # it out would refuse every block it ever wrote.
        active = sorted(await chains.active_authorities())
        stopped = await chains.cutoffs()
        return _ok(req_id, {
            "authorities": active,
            "stopped": [{"fingerprint": print_, "until_block": block}
                        for print_, block in sorted(stopped.items())],
            "count": len(active),
            "note": (
                "Fingerprints of the keys permitted to sign blocks on the "
                "shared chain. With none listed, reconcile refuses to import "
                "rather than importing from unknown signers."
                if not active else
                "A block signed by a key not listed here is refused. A stopped "
                "signer keeps the blocks it signed while trusted and may sign "
                "nothing after."
            ),
        })
    except Exception as exc:
        logger.exception("chain.authorities error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_set_authorities(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.set_authorities`` — replace the permitted-signer list.

    Deliberate and owner-driven by design. On a chain whose purpose is
    recording what happened between parties, who may write is exactly the
    decision that should not have a default.
    """
    listed = params.get("authorities")
    if not isinstance(listed, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'authorities' must be a list of fingerprints")
    for item in listed:
        text = str(item or "").strip()
        if not text or len(text) > 128:
            return _err(req_id, JsonRpcError.INVALID_PARAMS,
                        "each authority must be a non-empty fingerprint")

    try:
        from python.harness import chains

        permitted = await chains.set_authorities(listed)
        await db.log_to_db(
            "INFO",
            f"Shared chain signers set: {len(permitted)} permitted",
            {"authorities": sorted(permitted)},
        )
        return _ok(req_id, {"success": True, "authorities": sorted(permitted)})
    except Exception as exc:
        logger.exception("chain.set_authorities error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_fingerprint(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.fingerprint`` — the handle for a public key.

    So a key someone sends you can be turned into the short form the authority
    list uses, without pasting a PEM into a settings field and hoping the line
    endings survive.
    """
    public_key = str(params.get("public_key") or "").strip()
    if not public_key:
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'public_key' is required")
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        from python.harness.chain_verify import fingerprint

        try:
            load_pem_public_key(public_key.encode("utf-8"))
        except Exception as exc:
            # A malformed key accepted now becomes a block refused later for
            # reasons nobody can trace back to here.
            return _err(req_id, JsonRpcError.INVALID_PARAMS,
                        f"not a usable public key: {exc}")

        return _ok(req_id, {"fingerprint": fingerprint(public_key)})
    except Exception as exc:
        logger.exception("chain.fingerprint error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_reconcile(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.reconcile`` — read the shared chain back.

    Writing to a shared chain and never reading it leaves this install knowing
    only what it put there. This pulls what everyone else appended.

    It never pushes. The Node server's own POST /chain/reconcile overwrites the
    receiving chain whenever the incoming one is as long or longer, which is
    fine between a machine and its backup and is data loss on a chain several
    people write to.
    """
    try:
        from python.harness import chains

        result = await chains.reconcile(str(params.get("endpoint") or ""))
        if not result.get("reconciled"):
            await db.log_to_db(
                "WARNING",
                f"Could not reconcile with the shared chain: "
                f"{result.get('reason', 'unknown')}",
                {"problems": result.get("problems", [])},
            )
        elif result.get("learned") or result.get("confirmed"):
            await db.log_to_db(
                "INFO",
                f"Reconciled with the shared chain: {result['learned']} "
                f"transaction(s) learned from peers, {result['confirmed']} "
                f"of ours confirmed"
                + (f", {len(result['revoked'])} retired signer(s) withdrawn"
                   if result.get("revoked") else ""),
            )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("chain.reconcile error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_verify(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.verify`` — check a chain here rather than taking its word.

    ``GET /chain/status`` returns an ``isValid`` the node computed about
    itself, which answers nothing when the question is whether to trust it.
    Defaults to the local Ghost node so the same check works on both.
    """
    import httpx

    try:
        from python.harness import chain_verify, chains

        which = str(params.get("chain") or "ghost").lower()
        endpoint = str(params.get("endpoint") or "").strip()
        if not endpoint:
            endpoint = (await chains.real_endpoint() if which == "real"
                        else await chains.ghost_endpoint())
        if not endpoint:
            return _err(req_id, JsonRpcError.INVALID_PARAMS,
                        "no endpoint for that chain")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{endpoint.rstrip('/')}/chain/blocks")
        if response.status_code >= 400:
            return _err(req_id, JsonRpcError.INTERNAL_ERROR,
                        f"HTTP {response.status_code} from {endpoint}")

        # Enforce the permitted-signer list when asked, so "would this chain
        # be imported" and "is this chain internally consistent" can be asked
        # separately -- they are different questions with different answers.
        permitted = (await chains.authorities()
                     if params.get("check_signers") else None)
        verdict = chain_verify.verify(response.json(), authorities=permitted)
        return _ok(req_id, {
            "endpoint": endpoint, "valid": verdict.valid,
            "checked_signers": permitted is not None,
            "height": verdict.height, "signed": verdict.signed,
            "unsigned": verdict.unsigned, "problems": verdict.problems,
        })
    except Exception as exc:
        logger.exception("chain.verify error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_summary(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.summary`` — the two chains, side by side.

    Steven asked for exactly this: "I think there should be a way to see the
    diffrence in the two." Before this the difference was not visible because
    it was not recorded: the Ghost/Real decision lived in a local variable in
    token_counter and reached a logger.debug line (UI-023).
    """
    try:
        from python.harness import chains

        summary = await chains.summary()
        summary["explanation"] = {
            "ghost": (
                "Everything this machine did. Local, complete, and visible to "
                "nobody else."
            ),
            "real": (
                "Only interactions that involved somebody else — another "
                "install, another peer's skill, an AUTH transaction, online "
                "storage, a payment. Shared with everyone on the chain."
            ),
            "pending": (
                "External interactions that happened while offline. Held here "
                "and written to the shared chain on reconnect."
            ),
        }
        return _ok(req_id, summary)
    except Exception as exc:
        logger.exception("chain.summary error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_transactions(params: dict[str, Any], req_id: Any) -> str:
    """``chain.transactions`` — recent entries from one chain."""
    chain = str(params.get("chain") or "ghost").strip().lower()
    if chain not in ("ghost", "real"):
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'chain' must be 'ghost' or 'real'")
    try:
        limit = max(1, min(int(params.get("limit") or 100), 500))
    except (TypeError, ValueError):
        limit = 100

    try:
        from python.harness.chains import _unwrap

        rows = await db.fetch(
            "SELECT entry_id, tx_id, type, counterparty, document, metadata, "
            "occurred_at, pending, remote_id, written_at, origin "
            "FROM chain_ledger "
            "WHERE chain = $1 ORDER BY occurred_at DESC LIMIT $2",
            chain, limit,
        )
        return _ok(req_id, {
            "chain": chain,
            "transactions": [{
                "entry_id": row["entry_id"], "id": row["tx_id"],
                "type": row["type"],
                "counterparty": row["counterparty"],
                "document_ref": row["document"],
                "metadata": _unwrap(row["metadata"]),
                "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else "",
                "pending": row["pending"], "remote_id": row["remote_id"],
                "origin": row["origin"],
                "written_at": row["written_at"].isoformat() if row["written_at"] else "",
            } for row in rows],
        })
    except Exception as exc:
        logger.exception("chain.transactions error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_flush(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.flush`` — write everything queued for the Real chain.

    Called on reconnect, and available manually so a queue that will not drain
    reports why rather than sitting there silently.
    """
    try:
        from python.harness import chains

        result = await chains.flush()
        if result.get("error"):
            await db.log_to_db(
                "WARNING",
                f"Real chain flush stopped with {result.get('remaining', 0)} "
                f"transaction(s) still queued: {result['error']}",
            )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("chain.flush error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_chain_set_endpoint(params: dict[str, Any], req_id: Any) -> str:
    """
    ``chain.set_endpoint`` — where the shared Real chain lives.

    One shared chain, per Steven's answer: everyone appends to a common chain,
    which is what "viewable by all on the chain" requires. With no endpoint
    configured the Real chain simply never gets used and external transactions
    queue — which is the correct behaviour for an install that never goes
    online, not an error.
    """
    import json as _json

    endpoint = str(params.get("endpoint") or "").strip()
    if endpoint and not endpoint.startswith(("http://", "https://")):
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'endpoint' must be an http:// or https:// URL")
    try:
        # Merge, do not replace. Writing {"endpoint": ...} over the row drops
        # the permitted-signer list stored beside it, which is the same shape
        # as UI-056 -- one save overwriting a whole config object -- and the
        # reason app_settings exists at all. Losing it silently turns the
        # signer check off.
        from python.harness.chains import _unwrap

        row = await db.fetchrow(
            "SELECT value FROM app_settings WHERE key = 'real_chain'")
        stored = _unwrap(row["value"]) if row else {}
        stored["endpoint"] = endpoint
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES ('real_chain', $1::jsonb, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = NOW()",
            _json.dumps(stored),
            caller="chain.set_endpoint",
        )
        await db.log_to_db(
            "INFO",
            f"Real chain endpoint {'set to ' + endpoint if endpoint else 'cleared'}",
        )
        flushed = {}
        if endpoint:
            from python.harness import chains
            flushed = await chains.flush()
        return _ok(req_id, {"success": True, "endpoint": endpoint,
                            "flushed": flushed})
    except Exception as exc:
        logger.exception("chain.set_endpoint error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_guardrails_get_network(params: dict[str, Any], req_id: Any) -> str:
    """``guardrails.get_network`` — the harness-level network rules."""
    try:
        from python.harness.network_guard import DEFAULT_RULES, load_rules
        rules = await load_rules()
        return _ok(req_id, {
            "network": rules,
            "defaults": DEFAULT_RULES,
            "note": (
                "Opt-in. With rules disabled, every outbound connection is "
                "permitted exactly as before. The USB environment preset "
                "blocks outbound traffic regardless of these rules."
            ),
        })
    except Exception as exc:
        logger.exception("guardrails.get_network error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_guardrails_set_network(params: dict[str, Any], req_id: Any) -> str:
    """
    ``guardrails.set_network`` — firewall rules and port access.

    Steven's scope for this panel: "This isnt just about what the agent can
    talk about. This is about the entire harness. Gaurdrails should be able to
    address firewall rules, and port access."

    Stored inside the existing guardrails row rather than a new table, so the
    panel that claims to own guardrails actually owns all of them.
    """
    import json as _json

    network = params.get("network")
    if not isinstance(network, dict):
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'network' must be an object")

    default = str(network.get("default", "allow")).lower()
    if default not in ("allow", "deny"):
        return _err(req_id, JsonRpcError.INVALID_PARAMS,
                    "'default' must be 'allow' or 'deny'")

    for key in ("allow", "deny"):
        entries = network.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            return _err(req_id, JsonRpcError.INVALID_PARAMS,
                        f"'{key}' must be a list of rules")
        for rule in entries:
            if not isinstance(rule, dict) or not rule.get("host"):
                return _err(req_id, JsonRpcError.INVALID_PARAMS,
                            f"every {key} rule needs a 'host'")
            ports = rule.get("ports")
            if ports is not None:
                if not isinstance(ports, list):
                    return _err(req_id, JsonRpcError.INVALID_PARAMS,
                                "'ports' must be a list")
                try:
                    [int(port) for port in ports]
                except (TypeError, ValueError):
                    return _err(req_id, JsonRpcError.INVALID_PARAMS,
                                "'ports' must all be numbers")

    try:
        row = await db.fetchrow("SELECT rules FROM guardrails_config WHERE id = 1")
        stored = row["rules"] if row else {}
        if isinstance(stored, str):
            stored = _json.loads(stored)
        stored = stored if isinstance(stored, dict) else {}
        stored["network"] = {
            "enabled": bool(network.get("enabled", False)),
            "default": default,
            "allow": network.get("allow") or [],
            "deny": network.get("deny") or [],
        }
        await db.execute(
            "UPDATE guardrails_config SET rules = $1::jsonb, updated_at = NOW() "
            "WHERE id = 1",
            _json.dumps(stored), caller="guardrails.set_network",
        )
        await db.log_to_db(
            "INFO",
            f"Network guardrails updated: "
            f"{'enabled' if stored['network']['enabled'] else 'disabled'}, "
            f"default {default}, {len(stored['network']['allow'])} allow / "
            f"{len(stored['network']['deny'])} deny rule(s)",
            {"network": stored["network"]},
        )
        return _ok(req_id, {"success": True, "network": stored["network"]})
    except Exception as exc:
        logger.exception("guardrails.set_network error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_network_check(params: dict[str, Any], req_id: Any) -> str:
    """
    ``network.check`` — would this connection be permitted, and why.

    So a blocked connection can be explained rather than merely observed.
    """
    target = str(params.get("target") or "").strip()
    if not target:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'target' is required")
    try:
        from python.harness.access import AGENT, Principal
        from python.harness.network_guard import check

        principal = Principal(
            str(params.get("principal_kind") or AGENT),
            str(params.get("principal_id") or "default"),
        )
        decision = await check(target, principal, str(params.get("purpose") or ""))
        return _ok(req_id, {
            "allowed": decision.allowed, "reason": decision.reason,
            "host": decision.host, "port": decision.port,
            "matched": decision.matched, "constraints": decision.constraints,
        })
    except Exception as exc:
        logger.exception("network.check error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_access_grant(params: dict[str, Any], req_id: Any) -> str:
    """
    ``access.grant`` — invite a principal to one resource.

    Steven's rule: "permissions are by invite only ... I have to invite her to
    access. This does not mean I have to invite her to be a contact. Just to
    access a file." So a grant names a resource, not a person's whole
    relationship, and nothing is permitted without one.
    """
    from python.harness import access

    try:
        principal = _principal_from(params)
        result = await access.grant(
            principal,
            resource=str(params.get("resource") or ""),
            permission=str(params.get("permission") or "read"),
            constraints=params.get("constraints") or {},
            note=str(params.get("note") or ""),
        )
        await db.log_to_db(
            "INFO",
            f"Access granted: {result['principal']} may {result['permission']} "
            f"{result['resource']}",
            {"grant": result},
        )
        return _ok(req_id, {"success": True, **result})
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("access.grant error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_access_revoke(params: dict[str, Any], req_id: Any) -> str:
    """``access.revoke`` — withdraw a grant."""
    from python.harness import access

    try:
        principal = _principal_from(params)
        result = await access.revoke(
            principal,
            resource=str(params.get("resource") or ""),
            permission=str(params.get("permission") or ""),
        )
        await db.log_to_db(
            "INFO",
            f"Access revoked: {result['principal']} on {result['resource']}",
            {"revoke": result},
        )
        return _ok(req_id, {"success": True, **result})
    except Exception as exc:
        logger.exception("access.revoke error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_access_list(params: dict[str, Any], req_id: Any) -> str:
    """``access.list`` — every grant, or one principal's."""
    from python.harness import access

    try:
        principal = _principal_from(params) if params.get("principal_id") else None
        grants = await access.list_grants(principal)
        return _ok(req_id, {"grants": grants, "count": len(grants)})
    except Exception as exc:
        logger.exception("access.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_access_check(params: dict[str, Any], req_id: Any) -> str:
    """
    ``access.check`` — would this be permitted, and why or why not.

    Exposed so a refusal can be explained rather than merely observed. Every
    access denial in this codebase is something somebody will have to debug.
    """
    from python.harness import access

    try:
        principal = _principal_from(params)
        decision = await access.check(
            principal,
            resource=str(params.get("resource") or ""),
            permission=str(params.get("permission") or "read"),
            context=params.get("context") or {},
        )
        return _ok(req_id, {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "principal": decision.principal,
            "resource": decision.resource,
            "permission": decision.permission,
            "grant_id": decision.grant_id,
            "constraints": decision.constraints,
        })
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("access.check error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_database_capabilities(params: dict[str, Any], req_id: Any) -> str:
    """
    ``database.capabilities`` — which database kinds this install can reach.

    Steven: "You can have 0 databases or 1000 databases. How you use them is up
    to the user not to the develouper." So the panel offers the option and says
    honestly which kinds are usable now and what the others need, rather than
    accepting a connection string for a driver that does not exist.
    """
    try:
        from python.harness.database_clients import capabilities
        caps = capabilities()
        return _ok(req_id, {
            "capabilities": caps,
            "available": [c["kind"] for c in caps if c["available"]],
            "needs_install": [
                {"kind": c["kind"], "extra": c["install_extra"], "reason": c["reason"]}
                for c in caps if not c["available"]
            ],
            "note": (
                "These are the user's own databases. Tala's own storage is a "
                "separate connection and is not affected by anything here."
            ),
        })
    except Exception as exc:
        logger.exception("database.capabilities error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_system_architecture_contract(params: dict[str, Any], req_id: Any) -> str:
    """
    ``system.architecture_contract`` — how this system differs from the defaults.

    Exposed as an RPC as well as a tool so that a remote model, or any external
    system, can orient itself before acting. Steven's reason for asking for it:
    "systems need to be able to loook at mine and determine its default system.
    Otherwise Ill run into the same problem with other remote LLM."
    """
    try:
        from python.harness.architecture_contract import load_contract
        return _ok(req_id, load_contract(str(params.get("section") or "")))
    except Exception as exc:
        logger.exception("system.architecture_contract error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


#: Statuses a task can be in when it is finished with. Only these are ever
#: pruned -- a pending or in-progress task is work, not history, however old
#: it looks.
TERMINAL_TASK_STATUSES = ("complete", "completed", "failed", "aborted", "cancelled")

#: Default retention. Deliberately generous: the complaint was "it stays
#: forever", not "I need this gone tonight", and a retention sweep that is too
#: eager destroys the record of what happened.
DEFAULT_TASK_RETENTION_DAYS = 14


async def prune_finished_tasks(
    older_than_days: int = DEFAULT_TASK_RETENTION_DAYS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Age finished tasks out of the board.

    There was no retention policy anywhere (UI-034) -- Steven's "they dont fade
    out, it stays forever". The only two deletion paths were a user clicking
    Delete on one row and a user clicking Clear Queue, which truncates
    everything including work still running.

    Only terminal statuses are eligible, and a task that never finished is
    never pruned regardless of age: an abandoned in-progress row is a bug worth
    seeing, and quietly deleting it hides the bug rather than the row.
    """
    try:
        days = max(1, int(older_than_days))
    except (TypeError, ValueError):
        days = DEFAULT_TASK_RETENTION_DAYS

    try:
        doomed = await db.fetch(
            f"SELECT id, status, created_at FROM tasks "
            f"WHERE status = ANY($1) "
            f"AND COALESCE(completed_at, created_at) < NOW() - INTERVAL '{days} days'",
            list(TERMINAL_TASK_STATUSES),
        )
    except Exception as exc:
        logger.warning("prune_finished_tasks: could not read tasks: %s", exc)
        return {"success": False, "error": str(exc), "pruned": 0}

    if not doomed:
        return {"success": True, "pruned": 0, "dry_run": dry_run,
                "retention_days": days, "ids": []}

    ids = [r["id"] for r in doomed]
    if not dry_run:
        await db.execute(
            "DELETE FROM tasks WHERE id = ANY($1)", ids,
            caller="task retention sweep",
        )
        # Said out loud rather than swept quietly: a retention policy that
        # deletes without a record is indistinguishable from data loss.
        await db.log_to_db(
            "INFO",
            f"Task retention: removed {len(ids)} finished task(s) older than {days} days.",
            {"count": len(ids), "retention_days": days, "ids": ids[:50]},
        )
        logger.info("Task retention: pruned %d finished task(s) older than %d days.",
                    len(ids), days)

    return {"success": True, "pruned": len(ids), "dry_run": dry_run,
            "retention_days": days, "ids": ids[:50]}


async def handle_task_prune(params: dict[str, Any], req_id: Any) -> str:
    """
    ``task.prune`` — remove finished tasks older than a retention window.

    Separate from ``task.clear``, which truncates the whole table including
    work in progress. This only ever removes tasks that finished.
    """
    try:
        result = await prune_finished_tasks(
            older_than_days=params.get("older_than_days", DEFAULT_TASK_RETENTION_DAYS),
            dry_run=bool(params.get("dry_run", False)),
        )
        return _ok(req_id, result)
    except Exception as exc:
        logger.exception("task.prune error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


#: Nothing under these prefixes is ever synced, backed up or shared off the
#: machine. Steven's rule for the vault is absolute -- "No, vault never leaves
#: the install" -- and the cost, that losing the drive loses the vault, is the
#: cost he chose. The Chroma index is listed too: it holds unsealed vault
#: bodies, so syncing it would export the vault by the back door.
NEVER_LEAVES_PREFIXES = ("vault-",)
NEVER_LEAVES_DIRS = ("chroma", ".chroma", "vault")


def is_local_only_artifact(name: str) -> bool:
    """Whether a node or path must never be transmitted anywhere."""
    base = str(name or "").replace("\\", "/").split("/")[-1]
    if any(base.startswith(p) for p in NEVER_LEAVES_PREFIXES):
        return True
    parts = {p.lower() for p in str(name or "").replace("\\", "/").split("/")}
    return bool(parts & set(NEVER_LEAVES_DIRS))


async def handle_git_repo_op(params: dict[str, Any], req_id: Any) -> str:
    """
    ``git.repo_op`` — status, pull, push or fetch on a repository in the workspace.

    The panel promised clone, push and pull; none existed (UI-067). These act
    on the *user's* repositories, per Steven: "the GIT panel should stay. But
    Tala does not use GIT for herself." The harness versions itself through
    safe_code_bridges and its snapshots, and the two must not be conflated --
    that confusion is how an auto-rollback once deleted 427 tracked files.
    """
    from python.harness import git_ops

    operation = str(params.get("op") or "status").strip().lower()
    repo = str(params.get("repo") or "").strip()
    if not repo:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'repo' is required")

    try:
        path = git_ops.resolve_path(repo)
        if not (path / ".git").exists():
            return _err(
                req_id, JsonRpcError.INVALID_PARAMS,
                f"{repo} is not a git repository in the workspace.",
            )

        args = {
            "status": ["status", "--short", "--branch"],
            "pull": ["pull", "--ff-only"],
            "push": ["push"],
            "fetch": ["fetch", "--all"],
            "log": ["log", "--oneline", "-20"],
        }.get(operation)
        if args is None:
            return _err(
                req_id, JsonRpcError.INVALID_PARAMS,
                f"Unknown git operation {operation!r}. "
                f"Expected status, pull, push, fetch or log.",
            )

        provider = None
        if operation in ("pull", "push", "fetch"):
            remote = await git_ops.run_git(["remote", "get-url", "origin"], cwd=path)
            if remote["success"] and remote["stdout"]:
                candidates = await git_ops.providers_for(remote["stdout"])
                provider = candidates[0] if candidates else None

        result = await git_ops.run_git(args, cwd=path, provider=provider)
        result["repo"] = repo
        result["provider"] = provider["name"] if provider else None
        if not result["success"]:
            await db.log_to_db(
                "WARNING",
                f"git {operation} failed in {repo}: {result['stderr'][:300]}",
                {"repo": repo, "op": operation, "returncode": result["returncode"]},
            )
        return _ok(req_id, result)
    except git_ops.GitError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("git.repo_op error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_git_clone(params: dict[str, Any], req_id: Any) -> str:
    """``git.clone`` — clone a repository into the user's workspace."""
    from python.harness import git_ops

    try:
        result = await git_ops.clone(
            str(params.get("url") or ""),
            str(params.get("directory") or ""),
        )
        await db.log_to_db(
            "INFO" if result["success"] else "WARNING",
            f"git clone {'succeeded' if result['success'] else 'failed'}: "
            f"{result.get('path', '')}",
            {"provider": result.get("provider"), "returncode": result["returncode"]},
        )
        return _ok(req_id, result)
    except git_ops.GitError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("git.clone error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_git_list_repos(params: dict[str, Any], req_id: Any) -> str:
    """``git.list_repos`` — what is in the repository workspace."""
    from python.harness import git_ops

    try:
        root = git_ops.workspace_root()
        root.mkdir(parents=True, exist_ok=True)
        repos = [
            {"name": child.name, "path": str(child)}
            for child in sorted(root.iterdir())
            if child.is_dir() and (child / ".git").exists()
        ]
        available, where = git_ops.git_available()
        return _ok(req_id, {
            "repos": repos, "count": len(repos), "workspace": str(root),
            "git_available": available, "git_path": where,
        })
    except Exception as exc:
        logger.exception("git.list_repos error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_git_test_provider(params: dict[str, Any], req_id: Any) -> str:
    """
    ``git.test_provider`` — check a provider's credential against its endpoint.

    Uses ls-remote rather than an API call: it proves the credential works for
    the operation the panel actually promises, which an API token check does
    not necessarily do.
    """
    from python.harness import git_ops

    provider_id = str(params.get("id") or "").strip()
    repo_url = str(params.get("url") or "").strip()
    if not provider_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")

    try:
        from python.harness.credential_crypto import decrypt_config

        row = await db.fetchrow(
            "SELECT p.id, p.name, p.endpoint, p.username, p.api_key, p.credential_id, "
            "c.config AS cred_config FROM git_providers p "
            "LEFT JOIN auth_credentials c ON p.credential_id = c.id WHERE p.id = $1",
            provider_id,
        )
        if not row:
            return _ok(req_id, {"state": "failed", "detail": "No such provider."})

        api_key = row["api_key"]
        if row["credential_id"] and row["cred_config"]:
            cfg = decrypt_config(row["cred_config"]) or {}
            api_key = cfg.get("api_key") or cfg.get("token") or api_key

        available, where = git_ops.git_available()
        if not available:
            return _ok(req_id, {"state": "failed", "detail": f"git is not available: {where}"})

        target = repo_url or row["endpoint"]
        if not target:
            return _ok(req_id, {
                "state": "failed",
                "detail": "No endpoint configured, and no URL given to test against.",
            })

        provider = {"name": row["name"], "username": row["username"], "api_key": api_key}
        # fetch --dry-run rather than an API token check: it proves the
        # credential works for the operation the panel actually promises,
        # which a token endpoint does not necessarily do.
        result = await git_ops.run_git(
            ["fetch", "--dry-run", target], provider=provider, timeout=30)

        if result["success"]:
            return _ok(req_id, {"state": "ok", "detail": f"Reached {target}."})
        return _ok(req_id, {
            "state": "failed",
            "detail": result["stderr"][:400] or f"git exited {result['returncode']}",
        })
    except git_ops.GitError as exc:
        return _ok(req_id, {"state": "failed", "detail": str(exc)})
    except Exception as exc:
        logger.exception("git.test_provider error")
        return _ok(req_id, {"state": "failed", "detail": f"{type(exc).__name__}: {exc}"})


async def handle_storage_capabilities(params: dict[str, Any], req_id: Any) -> str:
    """
    ``storage.capabilities`` — which provider types can actually be used.

    Steven: "A user may never install or consume any of the
    storage/git/database providers but they should have the option too." The
    option has to be real or absent, not offered and hollow: the panel accepted
    credentials for nine backends, none of which had a client (UI-068).
    """
    try:
        from python.harness.storage_clients import capabilities
        caps = capabilities()
        return _ok(req_id, {
            "capabilities": caps,
            "available": [c["kind"] for c in caps if c["available"]],
            "needs_install": [
                {"kind": c["kind"], "extra": c["install_extra"], "reason": c["reason"]}
                for c in caps if not c["available"]
            ],
        })
    except Exception as exc:
        logger.exception("storage.capabilities error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_storage_test_provider(params: dict[str, Any], req_id: Any) -> str:
    """``storage.test_provider`` — actually reach the configured destination."""
    provider_id = str(params.get("id") or "").strip()
    if not provider_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")

    try:
        from python.harness.credential_crypto import decrypt_config
        from python.harness.storage_clients import StorageError, get_client

        row = await db.fetchrow(
            "SELECT id, name, provider_type, credentials FROM storage_providers "
            "WHERE id = $1", provider_id,
        )
        if not row:
            return _ok(req_id, {"state": "failed", "detail": "No such provider."})

        provider = {
            "provider_type": row["provider_type"],
            "credentials": decrypt_config(row["credentials"]) or {},
        }
        detail = get_client(provider).probe()
        return _ok(req_id, {"state": "ok", "detail": detail})
    except StorageError as exc:
        return _ok(req_id, {"state": "failed", "detail": str(exc)})
    except Exception as exc:
        logger.exception("storage.test_provider error")
        return _ok(req_id, {"state": "failed", "detail": f"{type(exc).__name__}: {exc}"})


async def handle_storage_backup_nodes(params: dict[str, Any], req_id: Any) -> str:
    """
    ``storage.backup_nodes`` — copy the node store to a configured provider.

    This is what makes the panel non-dead (UI-069): storage_providers appeared
    in six places, every one of them storage or display, so both stated rules
    on the panel were false.

    The vault is excluded and so is the Chroma index, per Steven's rule that
    the vault never leaves the install -- and the index holds unsealed vault
    bodies, so syncing it would export the vault by the back door.
    """
    provider_id = str(params.get("id") or "").strip()
    if not provider_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")
    dry_run = bool(params.get("dry_run", False))

    try:
        from python.harness.credential_crypto import decrypt_config
        from python.harness.storage_clients import StorageError, get_client
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir

        row = await db.fetchrow(
            "SELECT id, name, provider_type, credentials FROM storage_providers "
            "WHERE id = $1", provider_id,
        )
        if not row:
            return _err(req_id, JsonRpcError.INVALID_PARAMS, "No such provider.")

        nodes_dir = resolve_active_nodes_dir()
        candidates = sorted(nodes_dir.glob("*.html"))
        sendable, withheld = [], []
        for path in candidates:
            (withheld if is_local_only_artifact(path.name) else sendable).append(path)

        if dry_run:
            return _ok(req_id, {
                "success": True, "dry_run": True,
                "would_send": len(sendable), "withheld": len(withheld),
                "withheld_names": [p.name for p in withheld][:20],
                "provider": row["name"],
            })

        client = get_client({
            "provider_type": row["provider_type"],
            "credentials": decrypt_config(row["credentials"]) or {},
        })
        client.probe()

        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sent, failed = 0, []
        for path in sendable:
            try:
                client.put(f"tala-nodes/{stamp}/{path.name}", path.read_bytes())
                sent += 1
            except Exception as exc:
                failed.append({"name": path.name, "error": str(exc)[:200]})

        await db.log_to_db(
            "INFO" if not failed else "WARNING",
            f"Node backup to {row['name']}: {sent} sent, {len(failed)} failed, "
            f"{len(withheld)} withheld as local-only.",
            {"provider": row["name"], "sent": sent,
             "failed": len(failed), "withheld": len(withheld)},
        )
        return _ok(req_id, {
            "success": not failed,
            "provider": row["name"],
            "sent": sent,
            "withheld": len(withheld),
            "failed": failed[:20],
            "destination": f"tala-nodes/{stamp}/",
        })
    except StorageError as exc:
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
    except Exception as exc:
        logger.exception("storage.backup_nodes error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_environment_get(params: dict[str, Any], req_id: Any) -> str:
    """``environment.get`` — the current preset, lockdown, and what they permit."""
    try:
        from python.harness import environment as env
        return _ok(req_id, await env.get_state())
    except Exception as exc:
        logger.exception("environment.get error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_environment_set(params: dict[str, Any], req_id: Any) -> str:
    """
    ``environment.set`` — choose a preset, or turn lockdown on or off.

    The buttons were a CSS class toggle and a log line (UI-044). That mattered
    more than a dead control usually does: USB (ISOLATED) reads as a security
    guarantee, so selecting it and having nothing change is worse than not
    offering it.
    """
    from python.harness import environment as env

    environment = params.get("environment")
    lockdown = params.get("lockdown")
    if environment is None and lockdown is None:
        return _err(
            req_id, JsonRpcError.INVALID_PARAMS,
            "give 'environment' (usb, local, remote) and/or 'lockdown' (bool)",
        )
    if environment is not None and str(environment).strip().lower() not in env.ENVIRONMENTS:
        return _err(
            req_id, JsonRpcError.INVALID_PARAMS,
            f"unknown environment {environment!r}; expected one of "
            f"{', '.join(env.ENVIRONMENTS)}",
        )

    try:
        state = await env.set_state(environment=environment, lockdown=lockdown)
        await db.log_to_db(
            "INFO",
            f"Environment set to {state['environment']}"
            + (" with lockdown engaged" if state["lockdown"] else ""),
            {"state": state},
        )
        return _ok(req_id, state)
    except Exception as exc:
        logger.exception("environment.set error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_vault_grant(params: dict[str, Any], req_id: Any) -> str:
    """
    ``vault.grant`` — grant or revoke access to one vault document.

    Two edges, both default-deny and both per document: whether the body may
    reach a remote model, and which contacts may be sent it. Deliberately not
    derived from a contact's general permissions -- a contact grant does not
    reach the vault, so a misconfigured permission elsewhere cannot disclose a
    private document.
    """
    doc_id = str(params.get("id") or "").strip()
    if not doc_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")

    remote = params.get("remote_inference")
    contacts = params.get("contacts")
    if remote is None and contacts is None:
        return _err(
            req_id, JsonRpcError.INVALID_PARAMS,
            "give 'remote_inference' (bool) and/or 'contacts' (list) to change",
        )
    try:
        from python.harness.vault_nodes import set_grant
        result = set_grant(
            doc_id,
            remote_inference=None if remote is None else bool(remote),
            contacts=None if contacts is None else list(contacts),
        )
        if result.get("success"):
            await db.log_to_db(
                "INFO",
                f"Vault grant changed on {doc_id}",
                {"document": doc_id, "grants": result.get("grants")},
            )
        return _ok(req_id, result)
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("vault.grant error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_vault_list(params: dict[str, Any], req_id: Any) -> str:
    """``vault.list`` — enumerate vault documents. Titles only, never bodies."""
    try:
        from python.harness.vault_nodes import list_documents
        documents = list_documents()
        return _ok(req_id, {"documents": documents, "count": len(documents)})
    except Exception as exc:
        logger.exception("vault.list error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_vault_read(params: dict[str, Any], req_id: Any) -> str:
    """
    ``vault.read`` — open one vault document for the user.

    No local-model guard here, unlike the agent tool: this path ends at the
    user's own screen, and the rule is that vault content must not leave the
    machine, not that the user may not look at their own documents.
    """
    doc_id = str(params.get("id") or "").strip()
    if not doc_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")
    try:
        from python.harness.vault_nodes import read_document
        return _ok(req_id, read_document(doc_id))
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("vault.read error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_vault_write(params: dict[str, Any], req_id: Any) -> str:
    """
    ``vault.write`` — create or replace a vault document.

    The channel the vault never had: ``vault:read`` was the only vault IPC in
    preload, so "storage of editable private documents" had no way to put a
    document in, let alone edit one (UI-037).
    """
    title = str(params.get("title") or "").strip()
    if not title:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'title' is required")
    try:
        from python.harness.vault_nodes import write_document
        result = write_document(
            title=title,
            body=params.get("body") or "",
            tags=params.get("tags") or [],
            doc_id=params.get("id"),
            content_type=params.get("content_type") or "text/plain",
            filename=params.get("filename") or "",
        )
        return _ok(req_id, {"success": True, **result})
    except (ValueError, RuntimeError) as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("vault.write error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_vault_delete(params: dict[str, Any], req_id: Any) -> str:
    """``vault.delete`` — remove a vault document. The user's call only."""
    doc_id = str(params.get("id") or "").strip()
    if not doc_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'id' is required")
    try:
        from python.harness.vault_nodes import delete_document
        return _ok(req_id, delete_document(doc_id))
    except ValueError as exc:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, str(exc))
    except Exception as exc:
        logger.exception("vault.delete error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_tools_catalogue(params: dict[str, Any], req_id: Any) -> str:
    """
    ``tools.catalogue`` — every tool, read from its HTML-NN.

    Feeds the workflow palette, which was five hardcoded ``<div data-type>``
    elements while the registry held ninety tools (UI-059). Steven's
    requirement was "Every tool created in the html-nn should be available
    here", so this reads the ``tool-*.html`` nodes rather than the Python
    registry: the node is the catalogue, and a palette built from the registry
    would leave the nodes decorative again.

    Each entry carries the argument schema, so the builder can pre-fill a
    step's parameters instead of leaving the user to guess them -- the same
    gap that had the planner emitting empty tool_params.
    """
    try:
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir
        from sdk.agent.parser import parse_file

        nodes_dir = resolve_active_nodes_dir()
        query = str(params.get("query") or "").strip().lower()

        tools: list[dict[str, Any]] = []
        unreadable = 0
        for path in sorted(nodes_dir.glob("tool-*.html")):
            try:
                state = parse_file(str(path)).state_data or {}
            except Exception:
                unreadable += 1
                continue
            entry = state.get("tool")
            if not isinstance(entry, dict) or not entry.get("name"):
                continue

            # The tool's own words. state["description"] is the generic
            # "Program documentation for X" stamped on every documentation node.
            description = str(
                entry.get("description") or state.get("description") or ""
            )
            name = str(entry["name"])
            if query and query not in name.lower() and query not in description.lower():
                continue

            schema = entry.get("schema") or {}
            tools.append({
                "name": name,
                "node_id": path.stem,
                "signature": entry.get("signature") or f"{name}()",
                "module": entry.get("module") or "",
                "required": entry.get("required") or [],
                "properties": list((schema.get("properties") or {}).keys()),
                "schema": schema,
                "description": description,
            })

        if unreadable:
            # Say so rather than silently serving a short list: a palette that
            # is quietly missing tools looks identical to one that is complete.
            logger.warning(
                "tools.catalogue: %d tool node(s) could not be parsed and are "
                "missing from the palette.", unreadable,
            )

        return _ok(req_id, {
            "tools": tools,
            "count": len(tools),
            "unreadable": unreadable,
        })
    except Exception as exc:
        logger.exception("tools.catalogue error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_ui_bridge_result(params: dict[str, Any], req_id: Any) -> str:
    """
    ``ui.bridge_result`` — the renderer delivering an answer to a ui_bridge request.

    This is the return half of the request/response pair; without it the
    webview handlers could only ever broadcast and hope (UI-014, UI-015).
    """
    from python.harness import ui_bridge

    request_id = str(params.get("request_id") or "").strip()
    if not request_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'request_id' is required")

    delivered = ui_bridge.resolve(
        request_id,
        result=params.get("result"),
        error=params.get("error") or None,
    )
    if not delivered:
        # Almost always a response that arrived after its request timed out.
        logger.debug("ui.bridge_result: nothing was awaiting %s", request_id)
    return _ok(req_id, {"delivered": delivered})


async def handle_browser_get_dom(params: dict, req_id: any) -> str:
    """``browser.get_dom`` — read the live webview's DOM."""
    selector = str(params.get("selector") or "").strip()
    max_chars = params.get("max_chars")
    try:
        max_chars = int(max_chars) if max_chars is not None else 200_000
    except (TypeError, ValueError):
        max_chars = 200_000
    return await _browser_bridge(
        "browser.get_dom",
        {"selector": selector, "max_chars": max_chars},
        req_id,
        "the DOM request",
    )


async def handle_browser_execute_js(params: dict, req_id: any) -> str:
    """``browser.execute_js`` — evaluate JS in the webview and return its value."""
    code = params.get("code", "")
    if not isinstance(code, str) or not code.strip():
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'code' is required and must be a non-empty string")
    return await _browser_bridge(
        "browser.execute_js", {"code": code}, req_id, "the script"
    )

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
            # The remote end now requires an identified caller (UI-040), so
            # send who we are. It decides what, if anything, we may see.
            _me = await _local_identity()
            res = await _call_peer_rpc(
                peer["endpoint_url"],
                "p2p.marketplace_search",
                {"query": query, "caller_username": _me.get("username", ""),
                 "caller_wallet": _me.get("wallet_address", "")},
            )
            if res.get("items"):
                for item in res["items"]:
                    item["source_peer"] = peer["peer_username"]
                    results.append(item)
        except Exception as e:
            logger.warning(f"Failed to query peer {peer['peer_username']} marketplace: {e}")
            
    await asyncio.gather(*[_query_peer(p) for p in peers])
    return _ok(req_id, {"items": results})

async def handle_p2p_marketplace_search(params: dict[str, Any], req_id: Any) -> str:
    """
    ``p2p.marketplace_search`` — what a peer may see of this machine's skills.

    Previously took no caller identity at all and returned the full body of
    every file in ``skills/`` to anyone who could reach the endpoint, while the
    Contacts ``skills`` checkbox gated nothing (UI-040).

    Two rules now, from Q-21 and Q-23:

    * the caller must identify itself and be a known contact;
    * each skill declares its own scope. ``shared: global`` is visible to any
      known contact, ``shared: contact-only`` needs that contact named in
      ``shared_with``, and a skill that declares neither is private.

    The body is only returned for a skill the caller may actually have. A
    permitted search returns name and description plus the payload; nothing
    else is disclosed, not even the existence of skills the caller cannot see.
    """
    from pathlib import Path
    import yaml

    query = str(params.get("query", "")).lower()
    caller = str(params.get("caller_wallet") or params.get("caller_username") or "").strip()

    if not caller:
        return _err(
            req_id, JsonRpcError.INVALID_PARAMS,
            "caller_wallet or caller_username is required: skills are not served anonymously.",
        )

    contact = await _get_contact(
        wallet_address=caller if caller.startswith("0x") else None,
        username=None if caller.startswith("0x") else caller,
    )
    if contact is None:
        logger.warning("p2p.marketplace_search: refused unknown caller %r", caller)
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "caller is not a known contact")

    caller_username = str(contact.get("username") or "").lstrip("@").lower()
    caller_wallet = str(contact.get("wallet_address") or "").lower()

    def _scope_permits(meta: dict[str, Any]) -> bool:
        """The skill's own declared scope -- the subsystem rule."""
        scope = str(meta.get("shared") or meta.get("visibility") or "").strip().lower()
        if scope == "global":
            return True
        if scope in ("contact-only", "contact_only"):
            named = meta.get("shared_with") or []
            if isinstance(named, str):
                named = [n.strip() for n in named.replace(";", ",").split(",")]
            named = {str(n).strip().lstrip("@").lower() for n in named if str(n).strip()}
            return bool(named & {caller_username, caller_wallet})
        return False

    async def _may_have(meta: dict[str, Any]) -> bool:
        """
        Scope, composed with any explicit grant to this caller.

        An owner who invites one contact to one skill should not have to edit
        that skill's YAML to do it (UI-040, UI-101).
        """
        from python.harness.access import PEER, Principal, resolve

        name = str(meta.get("name") or "")
        decision = await resolve(
            Principal(PEER, caller_username or caller_wallet),
            resource=f"skill:{name}",
            permission="read",
            fallback_allowed=_scope_permits(meta),
            fallback_reason=f"skill scope is {meta.get('shared') or 'unset'}",
        )
        return decision.allowed

    items: list[dict[str, Any]] = []
    withheld = 0
    skills_dir = Path("skills")
    if skills_dir.exists():
        for file in skills_dir.glob("*.yaml"):
            try:
                with open(file, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            name = str(data.get("name", ""))
            description = str(data.get("description", ""))
            if query and query not in name.lower() and query not in description.lower():
                continue
            if not await _may_have(data):
                withheld += 1
                continue
            items.append({
                "name": name,
                "description": description,
                "payload": data,
                "public_key": "local-node-pubkey-0xTALA",
            })

    if withheld:
        logger.info(
            "p2p.marketplace_search: served %d skill(s) to %s, withheld %d not shared with them.",
            len(items), contact.get("username"), withheld,
        )
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
    "workflow.generate_from_tot": handle_workflow_generate_from_tot,
    "vps.version_push":   handle_vps_version_push,
    "mcp.connect_server": handle_mcp_connect_server,
    "mcp.list_servers":   handle_mcp_list_servers,
    "mcp.remove_server":  handle_mcp_remove_server,
    "mcp.save_servers":   handle_mcp_save_servers,
    "mcp.list_tools":     handle_mcp_list_tools,
    "mcp.call_tool":      handle_mcp_call_tool,
    "mcp.discover":       handle_mcp_discover,
    "trajectory.export":  handle_trajectory_export,
    "mcp.refresh_tools":  handle_mcp_refresh_tools,
    "guardrails.get":     handle_guardrails_get,
    "guardrails.save":    handle_guardrails_save,
    "search.get_providers": handle_search_get_providers,
    "search.save_providers": handle_search_save_providers,
    "search.query":       handle_search_query,
    "system.info":        handle_system_info,
    "system.self_test":   handle_system_self_test,
    "system.capabilities": handle_system_capabilities,
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
    "voice.speak": handle_voice_speak,
    "voice.backends": handle_voice_backends,
    "channels.list": handle_channels_list,
    "channels.save": handle_channels_save,
    "channels.delete": handle_channels_delete,
    "channels.status": handle_channels_status,
    "search.test_provider": handle_search_test_provider,
    "database.test_provider": handle_database_test_provider,
    "system.architecture_contract": handle_system_architecture_contract,
    "task.prune": handle_task_prune,
    "chain.retire_key": handle_chain_retire_key,
    "chain.retired_keys": handle_chain_retired_keys,
    "chain.identity": handle_chain_identity,
    "chain.fetch_key": handle_chain_fetch_key,
    "chain.offer_key": handle_chain_offer_key,
    "chain.key_offers": handle_chain_key_offers,
    "chain.trust_key": handle_chain_trust_key,
    "chain.reject_key": handle_chain_reject_key,
    "chain.authorities": handle_chain_authorities,
    "chain.set_authorities": handle_chain_set_authorities,
    "chain.fingerprint": handle_chain_fingerprint,
    "chain.reconcile": handle_chain_reconcile,
    "chain.verify": handle_chain_verify,
    "chain.summary": handle_chain_summary,
    "chain.transactions": handle_chain_transactions,
    "chain.flush": handle_chain_flush,
    "chain.set_endpoint": handle_chain_set_endpoint,
    "guardrails.get_network": handle_guardrails_get_network,
    "guardrails.set_network": handle_guardrails_set_network,
    "network.check": handle_network_check,
    "access.grant": handle_access_grant,
    "access.revoke": handle_access_revoke,
    "access.list": handle_access_list,
    "access.check": handle_access_check,
    "database.capabilities": handle_database_capabilities,
    "git.repo_op": handle_git_repo_op,
    "git.clone": handle_git_clone,
    "git.list_repos": handle_git_list_repos,
    "git.test_provider": handle_git_test_provider,
    "storage.capabilities": handle_storage_capabilities,
    "storage.test_provider": handle_storage_test_provider,
    "storage.backup_nodes": handle_storage_backup_nodes,
    "environment.get": handle_environment_get,
    "environment.set": handle_environment_set,
    "vault.grant": handle_vault_grant,
    "vault.list": handle_vault_list,
    "vault.read": handle_vault_read,
    "vault.write": handle_vault_write,
    "vault.delete": handle_vault_delete,
    "tools.catalogue": handle_tools_catalogue,
    "ui.bridge_result": handle_ui_bridge_result,
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
    "docs.generate_function_nodes": handle_docs_generate_function_nodes,
    "docs.list_program_targets": handle_docs_list_program_targets,
    "docs.refresh_node_grid": handle_docs_refresh_node_grid,
    "task.submit": handle_task_submit,
    "task.list": handle_task_list,
    "task.control": handle_task_control,
    "task.assign_worker": handle_task_assign_worker,
    "task.set_model_override": handle_task_set_model_override,
    "task.create_worktree": handle_task_create_worktree,
    "swarm.workers.active": handle_swarm_workers_active,
    "swarm.fleet.status": handle_swarm_fleet_status,
    "swarm.fleet.pause": handle_swarm_fleet_pause,
    "swarm.fleet.resume": handle_swarm_fleet_resume,
    "swarm.fleet.terminate": handle_swarm_fleet_terminate,
    "swarm.topology.list": handle_swarm_topology_list,
    "swarm.topology.create": handle_swarm_topology_create,
    "swarm.topology.delete": handle_swarm_topology_delete,
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
    "task.delete": handle_task_delete,
    "task.update": handle_task_update,
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
    "code.registry_get": handle_code_registry_get,
    "code.registry_set": handle_code_registry_set,
    "code.registry_list": handle_code_registry_list,
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

        _CURRENT_RPC.set(method)
        try:
            # inference.generate, workflow.execute, and telemetry.stream_subscribe need the ws object
            if method in ("inference.generate", "workflow.execute", "telemetry.stream_subscribe"):
                response = await handler(params, ws, req_id)
            else:
                response = await handler(params, req_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled error in handler '%s'", method)
            # _err audits this; the traceback rides along in the trace column.
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


async def broadcast_notification(method: str, params: dict[str, Any] = None) -> int:
    """
    Send a JSON-RPC 2.0 notification to all connected WebSocket clients.

    Returns the number of clients the notification actually reached.

    There is no queue and no replay: a client that is not connected at this
    instant never learns. Returning the count lets callers that depend on
    delivery -- the OTA update push most of all -- distinguish "sent" from
    "sent to nobody", which are otherwise indistinguishable from the call site.
    """
    if not _active_connections:
        return 0
    import json
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
    })
    results = await asyncio.gather(
        *(ws.send(payload) for ws in _active_connections),
        return_exceptions=True
    )
    return sum(1 for r in results if not isinstance(r, BaseException))


async def _auto_detect_and_verify_providers() -> None:
    """Detect local providers (Ollama) and test all registered providers to update their status."""
    import httpx
    import json as json_mod

    # 1. Fetch all existing providers from the DB
    rows = await load_inference_providers()
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

    # 3. Scan and auto-register discovered models from each target.
    # Priority bands by provider type, low-to-high so ascending priority order
    # tries remote (cloud) providers first, then local runtimes (Ollama/vLLM/
    # LM Studio/local OpenAI-compatible), then the embedded fallback last.
    # Auto-discovered local models are newly-appended, so they go at the END
    # of the local band (max existing local priority + 1, incrementing) --
    # NOT competing with remote providers the way a shared/negative-going
    # counter used to.
    _LOCAL_PRIORITY_BASE = 1000
    local_priorities = [p["priority"] for p in existing_providers if p["type"] == "local"]
    next_local_priority = (max(local_priorities) + 1) if local_priorities else _LOCAL_PRIORITY_BASE

    # Models that are embedding-only (not chat/completion-capable) should
    # never be auto-registered as inference.generate providers -- they'd
    # just fail or return garbage if a chat request ever reached them.
    _EMBEDDING_MODEL_MARKERS = ("embed",)

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
                            if model_name and any(marker in model_name.lower() for marker in _EMBEDDING_MODEL_MARKERS):
                                logger.debug("Skipping embedding-only model %s (not chat-capable)", model_name)
                                continue
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
                                # next_local_priority keeps this in the local band (>= 1000), after
                                # any remote provider and before the embedded fallback.
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
                                next_local_priority += 1  # Each subsequent auto-detected model goes after the last
                                
                                existing_providers.append({
                                    "id": sanitized_id,
                                    "type": "local",
                                    "name": provider_name,
                                    "priority": next_local_priority - 1,
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
            # Was: a non-empty apiKey string meant ONLINE, with no request made
            # at all (UI-046). An expired key, a revoked key, a typo, a wrong
            # endpoint, a suspended account or no network connection all
            # rendered the same green dot. The local branch two lines up does
            # this properly -- it asks the endpoint and checks the configured
            # model is in the answer -- so this now meets the same standard.
            api_key = p_config.get("apiKey", "") or p_config.get("api_key", "")
            endpoint = p_config.get("endpoint", "")
            model = p_config.get("model", "")

            if not api_key:
                new_status = "offline"
            elif not endpoint:
                new_status = "offline"
                logger.warning(
                    "Provider %s has a key but no endpoint; cannot verify it.", p["id"]
                )
            elif endpoint in cached_openai_models:
                models = cached_openai_models[endpoint]
                new_status = (
                    "online"
                    if models is not None and (not model or model in models)
                    else "offline"
                )
            else:
                try:
                    ep_url = _normalize_endpoint_url(endpoint)
                    headers = {"Authorization": f"Bearer {api_key}"}
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(f"{ep_url}/models", headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        models = [m.get("id") for m in data.get("data", [])]
                        cached_openai_models[endpoint] = models
                        if not model or model in models:
                            new_status = "online"
                        else:
                            new_status = "offline"
                            logger.warning(
                                "Provider %s answered but does not list model %r.",
                                p["id"], model,
                            )
                    else:
                        # 401 is the case this finding exists for: a key that
                        # is present and rejected.
                        cached_openai_models[endpoint] = None
                        new_status = "offline"
                        logger.warning(
                            "Provider %s returned HTTP %d when verifying its key.",
                            p["id"], resp.status_code,
                        )
                except Exception as exc:
                    cached_openai_models[endpoint] = None
                    new_status = "offline"
                    logger.warning(
                        "Provider %s could not be reached: %s", p["id"], exc
                    )
                
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

    # 1.4. Age finished tasks out of the board (UI-034). At startup rather
    # than on a timer: it is cheap, it is the moment the board is next going to
    # be looked at, and a sweep nobody can predict the timing of is harder to
    # reason about than one tied to a restart.
    try:
        pruned = await prune_finished_tasks()
        if pruned.get("pruned"):
            logger.info("Startup task retention removed %d row(s).", pruned["pruned"])
    except Exception as exc:
        logger.warning("Startup task retention sweep failed: %s", exc)

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


async def _real_chain_flush_loop() -> None:
    """
    Drain the Real chain queue whenever a connection comes back.

    An external interaction that happens offline is held locally and written on
    reconnect, so the Real chain stays a complete record of external activity
    rather than quietly missing whatever happened while the machine was away.
    Something has to notice the reconnection, and nothing else does: this is
    that something.

    Deliberately dull. It polls, it only does work when there is a queue and an
    endpoint, and it never logs when there is nothing to do -- an idle install
    with no Real chain configured should be silent, not emit a line a minute
    forever.
    """
    from python.harness import chains

    while True:
        await asyncio.sleep(60)
        try:
            pending = await db.fetchval(
                "SELECT COUNT(*) FROM chain_ledger "
                "WHERE chain = 'real' AND pending"
            )
            if not pending:
                continue
            if not await chains.real_endpoint():
                continue

            result = await chains.flush()
            if result.get("flushed"):
                await db.log_to_db(
                    "INFO",
                    f"Wrote {result['flushed']} queued transaction(s) to the "
                    f"Real chain on reconnect",
                )
                # Read back in the same pass. A flush leaves this install
                # knowing what it sent and nothing about what anyone else
                # appended in the meantime, and the reconnect is exactly the
                # moment that gap is worth closing.
                await chains.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Real chain flush loop: %s", exc)


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
        await sync_settings_to_nodes()
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

    # ── 7b. Start Messaging Gateway channels (Discord / Telegram) ──
    try:
        # MCP tools must reach the registry before the first turn, or a
        # connected server stays invisible to the model until something happens
        # to change the server set.
        await _refresh_mcp_tools("startup")

        from python.harness.channels import ChannelManager
        ChannelManager.get().register_broadcast(broadcast_notification)
        await ChannelManager.get().start()
    except Exception as _ch_exc:
        logger.warning("ChannelManager failed to start: %s", _ch_exc)

    # ── 7c. Node promotion ──
    # Nodes Tala authored sit in nodes/_staging/ on probation until they
    # verify cleanly enough times AND have waited out their dwell period.
    # The loop is time-based rather than restart-based on purpose: Tala is
    # meant to run continuously, so a restart-triggered cycle would leave
    # the queue as a graveyard on an always-on instance. One cycle runs now
    # to catch up after downtime, then the loop takes over.
    try:
        from sdk.agent.node_staging import run_promotion_loop, run_verification_cycle

        _stage_summary = run_verification_cycle()
        if _stage_summary["promoted"] or _stage_summary["quarantined"] or _stage_summary["pending"]:
            logger.info(
                "NodeStaging: %d promoted, %d quarantined, %d still on probation",
                len(_stage_summary["promoted"]),
                len(_stage_summary["quarantined"]),
                len(_stage_summary["pending"]),
            )
        asyncio.create_task(run_promotion_loop())
    except Exception as _stage_exc:
        logger.warning("NodeStaging failed to start: %s", _stage_exc)

    # ── 8. Start monitor tasks now that all startup imports are complete ──
    # Deferring these monitor tasks (especially _monitor_stdin which runs blocking ReadFile
    # in an executor thread) prevents Windows process loader lock / console handle lock deadlocks
    # when compiled modules (numpy/torch/etc.) are imported concurrently during startup.
    from python.harness.webrtc_manager import webrtc_manager
    webrtc_manager.set_message_handler(_handle_webrtc_message)
    asyncio.create_task(_real_chain_flush_loop())
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
    _server = None
    for bind_attempt in range(5):
        try:
            _server = await websockets.serve(
                _handle_connection,
                settings.WS_HOST,
                settings.WS_PORT,
                ping_interval=30,
                ping_timeout=10,
                max_size=16 * 1024 * 1024,
            )
            break
        except OSError as exc:
            if getattr(exc, 'errno', None) == 10048 or "10048" in str(exc):
                logger.warning("Port %s is busy/TIME_WAIT (attempt %d/5). Retrying in 1s...", settings.WS_PORT, bind_attempt + 1)
                await asyncio.sleep(1.0)
            else:
                raise

    logger.info(
        "WebSocket JSON-RPC 2.0 server listening on %s", settings.ws_uri
    )
    logger.info("Registered methods: %s", ", ".join(sorted(_HANDLERS.keys())))

    # ── 1b. Launch the MCP server (discovery + gated tool execution for
    # external MCP clients) as a background task. Non-fatal: a bind failure
    # here must never take down the main WS harness.
    asyncio.create_task(_start_mcp_server_safe())

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


async def _start_mcp_server_safe() -> None:
    """Start the MCP server, logging (not raising) on failure. Optional
    subsystem -- external MCP clients losing connectivity to Tala must
    never prevent the main WS harness from serving Electron."""
    try:
        from python.harness.mcp_server import start_mcp_server
        await start_mcp_server()
    except Exception:
        logger.exception("MCP server failed to start; continuing without it")


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
