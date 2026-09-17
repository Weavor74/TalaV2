"""
architecture_facts.py — the parts of the contract that must not be able to lie.

Tala reads its own documentation as context when reasoning about itself, so a
stale architecture document is not a tidiness problem: it makes the system
diagnose bugs that were fixed, misjudge what it can run, and plan repairs
against a shape it no longer has.

Prose cannot defend itself against that. So every claim here is **derived from
the code or the database**, or, where a claim is genuinely a judgement, it
**names the evidence** and this module checks that the evidence still exists.
A claim whose evidence has gone is reported as ``evidence_missing`` rather than
repeated confidently -- and ``tests/test_architecture_facts.py`` fails when
that happens, which is what makes this a ratchet instead of another document.

The distinction the maturity levels exist to preserve:

    implemented     the code path exists
    tested          a regression test covers it, named here
    live-verified   it has been observed working outside this machine
    experimental    present, incomplete, and not to be relied on
    unavailable     absent, or known not to work

"Implemented" was the level everything used to be described at, which is how a
feature that had never once executed could read as finished.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

#: Maturity levels, weakest last. Ordered so a reader can compare them.
MATURITY_LEVELS = (
    "live-verified",
    "tested",
    "implemented",
    "experimental",
    "unavailable",
)


def _exists(reference: str) -> bool:
    """
    Whether a named thing is still there.

    ``path`` checks the file; ``path::symbol`` also checks that the symbol is
    still defined in it. A renamed function is the ordinary way a described
    route stops being real while the file it lived in stays put, so checking
    the file alone would let exactly the drift this module exists to catch
    slip through.
    """
    path, _, symbol = reference.partition("::")
    target = ROOT / path
    if not target.exists():
        return False
    if not symbol:
        return True
    try:
        source = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    pattern = r"^\s*(async def|def|class)\s+" + re.escape(symbol) + r"\s*[\(:]"
    return re.search(pattern, source, re.MULTILINE) is not None


def _check_evidence(evidence: list[str]) -> tuple[list[str], list[str]]:
    """Split named evidence into what is still there and what is not."""
    present = [e for e in evidence if _exists(e)]
    missing = [e for e in evidence if e not in present]
    return present, missing


# ---------------------------------------------------------------------------
# Execution gates — derived, never restated
# ---------------------------------------------------------------------------


def execution_gates() -> dict[str, Any]:
    """
    What may run, and what it takes to run it.

    Read from `node_execution` rather than copied. The contract used to carry
    a hardcoded ``["workflow", "flow"]``, which stopped being true the day
    function, decision and service nodes became executable -- and a model
    asking the contract what it could run was told the wrong answer by the
    one document it is supposed to consult first.
    """
    try:
        from sdk.agent import node_execution as ne
    except Exception as exc:  # pragma: no cover - import failure is the finding
        return {"available": False, "reason": str(exc)}

    return {
        "available": True,
        "executable_node_types": sorted(ne.EXECUTABLE_TYPES),
        "code_bearing_node_types": sorted(ne.CODE_BEARING_TYPES),
        "state_node_type": ne.STATE_TYPE,
        "min_execution_authority": ne.MIN_EXECUTION_AUTHORITY,
        "admission_required_for_code": True,
        "note": (
            "A code-bearing node that arrives from elsewhere is inert until a "
            "person reviews and admits it, and admission is keyed to the "
            "content hash of the whole file -- editing a line revokes it. "
            "Arrival is not execution: see sdk/agent/node_admission.py."
        ),
    }


# ---------------------------------------------------------------------------
# Who owns which record
# ---------------------------------------------------------------------------


def data_ownership() -> dict[str, Any]:
    """
    Which store owns what, and what losing it costs.

    The question this answers is not "where is the data" but "what is
    authoritative and what is disposable", because backing up a derived index
    while missing the record it was derived from is the specific mistake this
    architecture invites.
    """
    return {
        "authoritative": [
            {
                "records": "Neural nodes: documentation, tools, skills, personas, "
                           "settings, workflows, learning",
                "store": "nodes/*.html under the portable data root",
                "rebuildable_from": None,
                "backup": "REQUIRED. This is the system of record.",
                "if_lost": "Unrecoverable. Every index below can be rebuilt from "
                           "these; none of them can rebuild these.",
            },
            {
                "records": "Live operational state: tasks, agents, profiles, "
                           "contacts, p2p_sessions, conversations, client_settings, "
                           "credentials",
                "store": "PostgreSQL",
                "rebuildable_from": None,
                "backup": "REQUIRED. Not derivable from nodes.",
                "if_lost": "Enrolments, approvals, conversation history and "
                           "provider configuration are gone. Nodes do not carry them.",
            },
            {
                "records": "Key material: wallet signing key, credential "
                           "encryption key, web token",
                "store": "vault/ (VAULT_PATH, default: the repository root)",
                "rebuildable_from": None,
                "backup": "REQUIRED, and separately from the database.",
                "if_lost": "Encrypted columns become unreadable and this install's "
                           "chain identity changes, so peers no longer recognise it.",
            },
            {
                "records": "The ledger: blocks and anchored transactions",
                "store": "blockchain/data/chain.json plus the chain tables",
                "rebuildable_from": None,
                "backup": "REQUIRED. Append-only and not regenerable.",
                "if_lost": "The audit trail is gone. Ghost (local, everything) and "
                           "Real (shared, external interactions only) are separate; "
                           "losing Ghost cannot be repaired from Real.",
            },
        ],
        "derived": [
            {
                "records": "memory_index (full-text over sessions, skills, "
                           "profiles, documentation)",
                "store": "PostgreSQL",
                "rebuildable_from": "nodes/ and the operational tables",
                "backup": "Optional. Cheaper to rebuild than to restore.",
            },
            {
                "records": "ChromaDB vector index over nodes",
                "store": "on disk beside the data root",
                "rebuildable_from": "nodes/",
                "backup": "Optional. Re-indexes lazily on retrieval.",
            },
            {
                "records": "embeddings (pgvector)",
                "store": "PostgreSQL",
                "rebuildable_from": "the indexed source rows",
                "backup": "Optional.",
                "note": "Absent without the pgvector extension. Its absence "
                        "denies writes to a connection node, which reads as an "
                        "unrelated failure.",
            },
        ],
        "known_contradiction": {
            "what": "Conversations live only in PostgreSQL.",
            "why_it_matters": (
                "The standing rule is that everything the harness creates or "
                "consumes is an HTML-NN. Conversations are an exception: rooms, "
                "participants and messages are database rows with no node "
                "behind them, so they are outside the node backup and invisible "
                "to node retrieval."
            ),
            "status": "known and unresolved",
        },
        "restore_order": [
            "vault/ first — without the keys, encrypted columns restore as noise",
            "nodes/ — the system of record",
            "PostgreSQL — operational state",
            "ledger files",
            "derived indexes last, or not at all: let them rebuild",
        ],
    }


# ---------------------------------------------------------------------------
# Security: three different things that get called one thing
# ---------------------------------------------------------------------------


def security_model() -> dict[str, Any]:
    """
    Three separate layers, described separately because conflating them is how
    a system gets credited with protection it does not have.

    Read from the source where it can be: the Electron flags and the preload
    surface are parsed rather than asserted, so this cannot claim an isolation
    setting that was since turned off.
    """
    main_js = ROOT / "electron" / "main.js"
    preload_js = ROOT / "electron" / "preload.js"

    flags: dict[str, bool | None] = {}
    if main_js.exists():
        src = main_js.read_text(encoding="utf-8", errors="replace")
        # The webPreferences block, not a comment describing one.
        for flag in ("contextIsolation", "sandbox", "nodeIntegration", "webSecurity"):
            match = re.search(rf"^\s*{flag}:\s*(true|false)", src, re.MULTILINE)
            flags[flag] = (match.group(1) == "true") if match else None

    allowlisted: list[str] = []
    unlisted_forwarded = None
    if preload_js.exists():
        src = preload_js.read_text(encoding="utf-8", errors="replace")
        block = re.search(r"ALLOWED_CHANNELS\s*=\s*new Set\(\[(.*?)\]\)", src, re.DOTALL)
        if block:
            allowlisted = re.findall(r"'([^']+)'", block.group(1))
        # Does an unlisted channel get refused, or forwarded to the RPC surface?
        guard = re.search(
            r"if\s*\(!ALLOWED_CHANNELS\.has\(channel\)\)\s*\{(.*?)\n\s{2}\}",
            src, re.DOTALL,
        )
        if guard:
            body = guard.group(1)
            unlisted_forwarded = (
                "ipcRenderer.invoke('python:rpc'" in body
                or 'ipcRenderer.invoke("python:rpc"' in body
            )

    return {
        "renderer_isolation": {
            "what_it_protects": "The browser process. Page script cannot reach "
                                "Node, the filesystem or other origins directly.",
            "electron_flags": flags,
            "verified_from": "electron/main.js",
        },
        "ipc_surface": {
            "allowlisted_channels": allowlisted,
            "unlisted_channels_forwarded_to_rpc": unlisted_forwarded,
            "what_this_means": (
                "ALLOWED_CHANNELS selects a TRANSPORT, not a permission. An "
                "unlisted name is forwarded to the Python harness as a JSON-RPC "
                "method, so the renderer's reachable surface is the harness's "
                "whole method table -- roughly 190 methods, shell.run among "
                "them -- and not this list."
                if unlisted_forwarded else
                "An unlisted channel is refused at preload. Dotted RPC method "
                "names must go through helpers.invoke, which remaps them to "
                "python:rpc without reopening a preload fallthrough."
            ),
            "docstring_is_wrong": bool(unlisted_forwarded),
            "note": (
                "preload.js documents this as '@throws if the channel is not "
                "whitelisted'. It does not throw. Anything rendered into the "
                "renderer that can execute script can therefore call the "
                "harness."
                if unlisted_forwarded else
                "renderer/helpers.js remaps dotted method names to python:rpc; "
                "bare window.tala.request/invoke of those names is rejected."
            ),
        },
        "peer_permission_checks": {
            "what_it_protects": "What another install may ask of this one.",
            "layers": [
                "WebSocket admission class: local (vault token), peer (HMAC over "
                "the contact's shared secret), bootstrap (no credential, and only "
                "on a connection node, for enrolment alone)",
                "Per-class method allowlists — an unrecognised class gets the "
                "empty set, so a missing label means trusted with nothing",
                "Contact status: only 'approved' permits direct interaction; "
                "'pending' and 'denied' are refused and told apart",
                "Per-contact permission grants, all defaulting to False; asking "
                "is recorded as a request and grants nothing",
                "Persona nodes are never resolvable by a peer, ahead of any grant",
            ],
            "scope_note": (
                "Room membership is a separate, narrower permission: it lets a "
                "participant speak in that conversation and grants nothing "
                "outside it."
            ),
        },
        "host_command_execution": {
            "what_it_protects": "Nothing. This is the part with no sandbox.",
            "reality": (
                "shell.run executes with the privileges of the user running the "
                "harness, in the repository root. Risk classification runs and "
                "is LOGGED; it does not block. The halt switch can refuse "
                "execution, and that is the only gate on this path."
            ),
            "consequence": (
                "The renderer being OS-sandboxed says nothing about the harness, "
                "which is not. Treat 'Tala is sandboxed' as false."
            ),
        },
    }


# ---------------------------------------------------------------------------
# How anything actually reaches a tool
# ---------------------------------------------------------------------------


def execution_paths() -> list[dict[str, Any]]:
    """
    The routes from an intention to a tool call, and what verifies the result.

    Each names its entry point, and the entry point is checked to still exist:
    a route described here that no longer has code behind it is reported as
    missing rather than left in place for a model to plan around.
    """
    paths = [
        {
            "path": "chat",
            "entry": "python/harness/server.py::handle_inference_generate",
            "reaches_tools_via": "the model emits a tool call; ToolRegistry.run_tool dispatches it",
            "verifies_results": "no automatic verification — the user reads the reply",
        },
        {
            "path": "goals (autonomous)",
            "entry": "sdk/agent/goal_engine.py",
            "reaches_tools_via": "decomposition into a DAG, then tool nodes executed by the graph engine",
            "verifies_results": "sdk/agent/goal_verification.py checks the stated acceptance criteria",
        },
        {
            "path": "workflows",
            "entry": "python/harness/workflow_executor.py",
            "reaches_tools_via": "steps declared in an HTML-NN; each step is a tool call, a branch, or another node",
            "verifies_results": "step status only; a failed step stops the chain",
        },
        {
            "path": "DAG / graph execution",
            "entry": "sdk/agent/graph_engine.py",
            "reaches_tools_via": "tool nodes in the graph",
            "verifies_results": "per-node status; execution gates apply to node steps",
        },
        {
            "path": "crews",
            "entry": "python/harness/crews.py",
            "reaches_tools_via": "each member runs as an agent with the shared registry",
            "verifies_results": "escalation on failure; no independent verification pass",
        },
        {
            "path": "swarms",
            "entry": "sdk/agent/swarm_engine.py",
            "reaches_tools_via": "workers claim nodes atomically and call tools directly",
            "verifies_results": "join barrier and in-flight counting decide completion",
        },
        {
            "path": "scheduled jobs",
            "entry": "python/harness/heartbeat_scheduler.py",
            "reaches_tools_via": "a job fires one of the routes above",
            "verifies_results": "whatever the fired route verifies",
        },
        {
            "path": "peer requests",
            "entry": "python/harness/server.py::handle_relay_deliver",
            "reaches_tools_via": "it does not — the p2p surface has no tool dispatch",
            "verifies_results": "n/a",
        },
    ]
    for entry in paths:
        entry["entry_exists"] = _exists(entry["entry"])
    return paths


# ---------------------------------------------------------------------------
# What is finished, and how anyone would know
# ---------------------------------------------------------------------------


def capability_maturity() -> list[dict[str, Any]]:
    """
    Where each capability actually stands, with the evidence named.

    Only claims that can point at something are made. A capability nobody has
    run outside this machine is not live-verified, however complete it looks,
    because "the code exists" and "it works" have repeatedly differed here.
    """
    claims: list[dict[str, Any]] = [
        {
            "capability": "Federation: enrolment, relay, rooms across installs",
            "maturity": "live-verified",
            "evidence": ["tests/test_federation_end_to_end.py", "tests/test_vps_relay.py"],
            "observed": "Three harnesses on separate databases, plus deployment to "
                        "the talacore.app connection node.",
            "limits": "Two installs; not tested at any scale, over real latency, "
                      "or across a restart of the node.",
        },
        {
            "capability": "Conversations: multi-party rooms, hybrid membership rule",
            "maturity": "tested",
            "evidence": ["tests/test_conversations.py", "tests/test_conversation_delivery.py"],
            "limits": "Rooms are database-only; see data_ownership.known_contradiction.",
        },
        {
            "capability": "Agents as room participants",
            "maturity": "tested",
            "evidence": ["tests/test_room_agents.py"],
            "limits": "Inference is stubbed in the tests. Agent replies in a real "
                      "room have not been observed end to end.",
        },
        {
            "capability": "Node execution gates and admission of code-bearing nodes",
            "maturity": "tested",
            "evidence": ["tests/test_node_execution.py", "tests/test_peer_admission.py"],
            "limits": "Model-written review of arriving code is not implemented; "
                      "admission is a human act.",
        },
        {
            "capability": "Peer approval and permission model",
            "maturity": "tested",
            "evidence": ["tests/test_peer_approval_gate.py", "tests/test_contact_approval.py",
                         "tests/test_request_notification.py"],
        },
        {
            "capability": "Workflows from HTML-NN",
            "maturity": "tested",
            "evidence": ["tests/test_workflow_load_gate.py", "tests/test_workflow_coordinator.py",
                         "tests/test_interactive_workflow_runs_real_steps.py"],
        },
        {
            "capability": "Swarm fan-out / fan-in",
            "maturity": "tested",
            "evidence": ["tests/test_swarm_orchestration_e2e.py"],
        },
        {
            "capability": "Crews",
            "maturity": "tested",
            "evidence": ["tests/test_crews.py", "tests/test_crew_run.py"],
        },
        {
            "capability": "Goal decomposition and verification",
            "maturity": "tested",
            "evidence": ["tests/test_goal_verification.py", "tests/test_goal_resume.py"],
            "limits": "Benchmark completion with a local model is partial, not full.",
        },
        {
            "capability": "Scheduled jobs",
            "maturity": "tested",
            "evidence": ["tests/test_heartbeat_resume_hook.py",
                         "tests/test_heartbeat_status_reconcile.py"],
        },
        {
            "capability": "OTA update and packaging",
            "maturity": "experimental",
            "evidence": ["tests/test_ota_update_push.py", "tests/test_updater_script.py"],
            "limits": "The 2026-09-07 production review found release engineering "
                      "and the updater unresolved. Tests cover the mechanism, not "
                      "a completed real upgrade.",
        },
        {
            "capability": "Unattended autonomy",
            "maturity": "experimental",
            "evidence": [],
            "limits": "The production review's conclusion: controlled private beta "
                      "is the defensible level. Supervised operation only.",
        },
        {
            "capability": "Model-reviewed admission of arriving code",
            "maturity": "unavailable",
            "evidence": [],
            "limits": "Specified — the model should analyse code before execution "
                      "is permitted — and not built. Admission today is manual.",
        },
    ]

    for claim in claims:
        present, missing = _check_evidence(claim.get("evidence") or [])
        claim["evidence_present"] = present
        if missing:
            claim["evidence_missing"] = missing
            claim["maturity_claim_unsupported"] = True
    return claims


# ---------------------------------------------------------------------------
# When this was true, and of what
# ---------------------------------------------------------------------------


def verification_metadata() -> dict[str, Any]:
    """
    What was reviewed, when, and at which commit.

    Undated documentation cannot be reconciled against anything: a reader
    cannot tell whether it predates the thing they are looking at.
    """
    def _git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15,
            ).stdout.strip()
        except Exception:
            return ""

    dirty = _git("status", "--porcelain")
    return {
        "reviewed_commit": _git("rev-parse", "HEAD"),
        "reviewed_commit_subject": _git("log", "-1", "--format=%s"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "working_tree_clean": not bool(dirty),
        "uncommitted_files": len(dirty.splitlines()) if dirty else 0,
        "how_to_reproduce": "python scripts/generate_architecture_contract.py",
        "regression_gate": "python -m pytest tests/ -q  (excluding tests marked llm)",
        "known_limitations": [
            "Generated on one machine: node and index counts are that machine's, "
            "not a property of the project.",
            "Maturity above 'implemented' rests on the named evidence and nothing "
            "more; a passing test is not a working feature.",
            "This contract describes structure. It does not assert that any "
            "given subsystem is currently healthy — call logging.get_system_health "
            "for that.",
        ],
    }


def all_facts() -> dict[str, Any]:
    """Everything in this module, for the contract generator."""
    return {
        "execution_gates": execution_gates(),
        "data_ownership": data_ownership(),
        "security_model": security_model(),
        "execution_paths": execution_paths(),
        "capability_maturity": capability_maturity(),
        "verification": verification_metadata(),
    }
