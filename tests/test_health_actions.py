"""Operator next-actions for logging.get_health (MAINTENANCE → what to click)."""
from __future__ import annotations

from python.harness.health_actions import action_for, build_actions, build_health_payload


def test_healthy_service_has_no_action():
    assert action_for("MEMORY_AUTHORITY_SERVICE", "HEALTHY", "ok") is None


def test_maintenance_mcp_points_at_plugins_settings():
    action = action_for("MCP_TOOL_AVAILABILITY", "MAINTENANCE", "no MCP servers configured")
    assert action is not None
    assert action["kind"] == "settings_panel"
    assert action["target"] == "plugins"
    assert "Plugins" in action["label"]


def test_emergency_halt_points_at_release_halt():
    action = action_for("EMERGENCY_HALT", "CRITICAL", "halt engaged")
    assert action["kind"] == "mission_control"
    assert action["target"] == "release-halt"


def test_actions_ordered_critical_before_maintenance():
    services = {
        "MEMORY_AUTHORITY_SERVICE": "HEALTHY",
        "MCP_TOOL_AVAILABILITY": "MAINTENANCE",
        "EMERGENCY_HALT": "CRITICAL",
        "POLICY_GATE": "MAINTENANCE",
    }
    details = {k: "x" for k in services}
    actions = build_actions(services, details)
    assert [a["service"] for a in actions] == [
        "EMERGENCY_HALT",
        "MCP_TOOL_AVAILABILITY",
        "POLICY_GATE",
    ]


def test_payload_includes_actions_when_maintenance_required():
    services = {
        "MEMORY_AUTHORITY_SERVICE": "HEALTHY",
        "MCP_TOOL_AVAILABILITY": "MAINTENANCE",
        "POLICY_GATE": "MAINTENANCE",
    }
    details = {
        "MEMORY_AUTHORITY_SERVICE": "index ok",
        "MCP_TOOL_AVAILABILITY": "no MCP servers configured",
        "POLICY_GATE": "guardrail category is 'none'",
    }
    payload = build_health_payload(
        services, details, "MAINTENANCE", "MAINTENANCE_REQUIRED", []
    )
    assert payload["overall"] == "MAINTENANCE"
    assert payload["mode"] == "MAINTENANCE_REQUIRED"
    assert len(payload["actions"]) == 2
    assert all(a["service"] != "MEMORY_AUTHORITY_SERVICE" for a in payload["actions"])
