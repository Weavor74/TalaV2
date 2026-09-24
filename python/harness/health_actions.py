"""
Concrete next actions for logging.get_health rows.

When overall is MAINTENANCE_REQUIRED while MEMORY_AUTHORITY is HEALTHY, the
operator still needs to know what to click. Each non-HEALTHY service gets a
structured action (label + navigation/rpc hint) derived from the probe detail.

The single entry point is :func:`build_health_payload` (called by the
``logging.get_health`` handler in server.py; renderer/health_ops.js renders
the resulting ``actions[]``). The per-service helpers are private — the
reachability audit rightly flagged them when they were public with no caller
outside this module.
"""
from __future__ import annotations

from typing import Any


# service → (button label, kind, target)
# kind: settings_panel | view | rpc | reconnect | mission_control
_ACTION_BY_SERVICE: dict[str, dict[str, str]] = {
    "MCP_TOOL_AVAILABILITY": {
        "label": "Open Settings → Plugins (add/enable MCP)",
        "kind": "settings_panel",
        "target": "plugins",
    },
    "POLICY_GATE": {
        "label": "Open Settings → Guardrails (set a category)",
        "kind": "settings_panel",
        "target": "guardrails",
    },
    "SEARCH_RETRIEVAL_SERVICE": {
        "label": "Open Settings → Search (add a provider)",
        "kind": "settings_panel",
        "target": "search",
    },
    "DASHBOARD_TELEMETRY": {
        "label": "Open Mission Control → Scheduled Goals",
        "kind": "view",
        "target": "mission-control",
    },
    "BOOTSTRAP_RUNTIME_SERVICES": {
        "label": "Reconnect harness WebSocket",
        "kind": "reconnect",
        "target": "ws",
    },
    "EMERGENCY_HALT": {
        "label": "Mission Control → RELEASE HALT (owner unlock)",
        "kind": "mission_control",
        "target": "release-halt",
    },
    "INFERENCE_SERVICE": {
        "label": "Open Settings → Inference (fix/add provider)",
        "kind": "settings_panel",
        "target": "inference",
    },
    "DB_HEALTH_SERVICE": {
        "label": "Open Settings → Database (check Postgres)",
        "kind": "settings_panel",
        "target": "database",
    },
    "MEMORY_AUTHORITY_SERVICE": {
        "label": "Rebuild memory index from disk",
        "kind": "rpc",
        "target": "memory.rebuild_from_disk",
    },
    "ARCHITECTURAL_INVARIANTS": {
        "label": "Open Settings → Architecture (review invariants)",
        "kind": "settings_panel",
        "target": "architecture",
    },
    "TOOL_EXECUTION_COORDINATOR": {
        "label": "Open Settings → Plugins / review tool registry",
        "kind": "settings_panel",
        "target": "plugins",
    },
    "QUEUE_BACKLOG_PRESSURE": {
        "label": "Open Mission Control (drain or clear queue)",
        "kind": "view",
        "target": "mission-control",
    },
    "AUTONOMY_ORCHESTRATOR": {
        "label": "Open Mission Control (inspect failed tasks)",
        "kind": "view",
        "target": "mission-control",
    },
    "REFLECTION_SERVICE": {
        "label": "Open Settings → Agent (reflection)",
        "kind": "settings_panel",
        "target": "agent",
    },
}


def _action_for(service: str, status: str, detail: str = "") -> dict[str, str] | None:
    """Return a next-action dict for a non-healthy service, or None if healthy."""
    if status == "HEALTHY":
        return None
    base = _ACTION_BY_SERVICE.get(service)
    if not base:
        return {
            "service": service,
            "status": status,
            "detail": detail or "",
            "label": f"Inspect {service} (see detail)",
            "kind": "inspect",
            "target": service,
        }
    return {
        "service": service,
        "status": status,
        "detail": detail or "",
        **base,
    }


def _build_actions(services: dict[str, str], details: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Ordered list of next actions for every non-HEALTHY measured service."""
    details = details or {}
    actions: list[dict[str, str]] = []
    # Criticals first, then degraded, maintenance, unknown.
    order = {"CRITICAL": 0, "DEGRADED": 1, "MAINTENANCE": 2, "UNKNOWN": 3}
    ranked = sorted(
        ((s, st) for s, st in services.items() if st != "HEALTHY"),
        key=lambda pair: (order.get(pair[1], 9), pair[0]),
    )
    for service, status in ranked:
        action = _action_for(service, status, details.get(service, ""))
        if action:
            actions.append(action)
    return actions


def build_health_payload(
    services: dict[str, str],
    details: dict[str, str],
    overall: str,
    mode: str,
    unknowns: list[str],
) -> dict[str, Any]:
    """Payload for logging.get_health including operator next actions."""
    return {
        "services": services,
        "details": details,
        "overall": overall,
        "mode": mode,
        "measured": len(services) - len(unknowns),
        "unmeasured": unknowns,
        "actions": _build_actions(services, details),
    }
