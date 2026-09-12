"""
ui_control.py — let Tala operate her own interface.

Closes UI-007. Four tools, backed by the ``ui-view-*`` Neural Node map:

    ui_describe   read what a screen is and what it offers
    ui_navigate   switch the workspace to a view
    ui_click      press a documented control
    ui_reload     apply an edit to renderer/ without restarting

Deliberately routed over the existing ``python:notification`` channel rather
than a Chrome DevTools port (UI-119). CDP has no authentication, so exposing it
would let any local process drive the app and would bypass the per-agent
privilege model entirely. Every call here goes through the harness and stays
gateable.

Two habits this module keeps, because the audit found the opposite everywhere:

* **No silent success.** ``broadcast_notification`` returns how many clients it
  actually reached. Zero means nothing is listening, and these tools say so
  rather than returning ok.
* **No unbounded surface.** ``ui_click`` will only press an element the map
  documents, and refuses controls the map records as dead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sdk.agent.documentation_nodes import resolve_active_nodes_dir
from sdk.agent.parser import parse_file
from sdk.agent.tools import register_tool

log = logging.getLogger(__name__)

_INDEX_NODE = "ui-map-index"


# ── map access ──────────────────────────────────────────────────────────────

def _nodes_dir() -> Path:
    return resolve_active_nodes_dir()


def _load(node_id: str) -> dict[str, Any] | None:
    path = _nodes_dir() / f"{node_id}.html"
    if not path.exists():
        return None
    try:
        return parse_file(str(path)).state_data or {}
    except Exception as exc:  # pragma: no cover - corrupt node
        log.warning("ui_control: could not parse %s: %s", node_id, exc)
        return None


def _view_map() -> dict[str, str]:
    """``{'view-vault': 'ui-view-vault', ...}`` from the index node."""
    state = _load(_INDEX_NODE) or {}
    return dict(state.get("views") or {})


def _resolve_view(view: str) -> tuple[str | None, str | None]:
    """Accept 'vault', 'view-vault' or 'ui-view-vault'. Returns (view_id, node_id)."""
    views = _view_map()
    if not views:
        return None, None
    candidates = {view, f"view-{view}", view.replace("ui-view-", "view-")}
    for vid, nid in views.items():
        if vid in candidates or nid == view:
            return vid, nid
    return None, None


def _known_views() -> list[str]:
    return sorted(v.replace("view-", "", 1) for v in _view_map())


async def _broadcast(method: str, params: dict[str, Any]) -> int:
    from python.harness.server import broadcast_notification
    return await broadcast_notification(method, params)


def _no_listener(action: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": "no_ui_connected",
        "detail": (
            f"Could not {action}: the notification reached 0 clients. The Tala "
            "window is not open, or its WebSocket is not connected."
        ),
    }


# ── tools ───────────────────────────────────────────────────────────────────

@register_tool(
    name="ui_describe",
    description=(
        "Describe a screen of Tala's own interface: how it is reached, what controls it "
        "offers with their element ids, and any known defects. Call with no argument to "
        "list every screen. Reads the ui-view-* Neural Node map."
    ),
)
async def ui_describe(view: str = "") -> dict[str, Any]:
    """
    NAME
           ui_describe - read what a screen is and what it offers

    SYNOPSIS
           ui_describe(view: str = "") -> dict[str, Any]
    """
    if not view:
        index = _load(_INDEX_NODE)
        if index is None:
            return {"success": False, "error": "map_missing",
                    "detail": "ui-map-index node not found. Run scripts/generate_ui_nodes.py."}
        return {
            "success": True,
            "views": _known_views(),
            "sidebar_rail": index.get("sidebar_rail", []),
            "settings_panels": index.get("settings_panels", []),
        }

    view_id, node_id = _resolve_view(view)
    if not node_id:
        return {"success": False, "error": "unknown_view",
                "detail": f"No such view {view!r}.", "known_views": _known_views()}

    state = _load(node_id) or {}
    return {
        "success": True,
        "view": view_id,
        "node_id": node_id,
        "title": state.get("title"),
        "purpose": state.get("description"),
        "reached_by": state.get("reached_by"),
        "operator_label": state.get("operator_label"),
        "controls": state.get("controls", []),
        "known_defects": state.get("known_defects", []),
    }


@register_tool(
    name="ui_navigate",
    description=(
        "Switch Tala's workspace to one of her own views, e.g. 'vault', 'settings', "
        "'research'. Use ui_describe first to see the available views. Fails clearly "
        "if the window is not open."
    ),
)
async def ui_navigate(view: str) -> dict[str, Any]:
    """
    NAME
           ui_navigate - bring a view to the front of the workspace

    SYNOPSIS
           ui_navigate(view: str) -> dict[str, Any]
    """
    view_id, node_id = _resolve_view(view)
    if not node_id:
        return {"success": False, "error": "unknown_view",
                "detail": f"No such view {view!r}.", "known_views": _known_views()}

    state = _load(node_id) or {}
    title = (state.get("title") or view_id).replace("UI View: ", "")
    short = view_id.replace("view-", "", 1)

    reached = await _broadcast("ui.navigate", {"view": short, "title": title})
    if reached == 0:
        return _no_listener(f"navigate to {short!r}")

    result: dict[str, Any] = {"success": True, "view": view_id, "title": title,
                              "clients_notified": reached}
    if state.get("known_defects"):
        result["known_defects"] = state["known_defects"]
    return result


def _clickable() -> dict[str, dict[str, str]]:
    """Every control the map documents, excluding ones it records as dead."""
    out: dict[str, dict[str, str]] = {}
    for view_id, node_id in _view_map().items():
        for ctrl in (_load(node_id) or {}).get("controls", []):
            cid = str(ctrl.get("id", "")).strip()
            if not cid or ctrl.get("verdict") == "dead":
                continue
            out[cid] = {"view": view_id, "does": ctrl.get("does", ""),
                        "verdict": ctrl.get("verdict", "")}
    return out


@register_tool(
    name="ui_click",
    description=(
        "Press a control in Tala's own interface by its element id, e.g. "
        "'btn-refresh-nodes'. Only controls documented in the ui-view-* map can be "
        "pressed, and controls known to be dead are refused. Use ui_describe to see "
        "what a screen offers."
    ),
)
async def ui_click(element_id: str) -> dict[str, Any]:
    """
    NAME
           ui_click - press a documented control

    SYNOPSIS
           ui_click(element_id: str) -> dict[str, Any]
    """
    element_id = (element_id or "").strip()
    allowed = _clickable()

    if element_id not in allowed:
        dead = {
            str(c.get("id", "")).strip()
            for nid in _view_map().values()
            for c in (_load(nid) or {}).get("controls", [])
            if c.get("verdict") == "dead"
        }
        if element_id in dead:
            return {"success": False, "error": "control_is_dead",
                    "detail": (f"{element_id!r} is documented as having no effect. "
                               "Pressing it would do nothing.")}
        return {"success": False, "error": "unknown_control",
                "detail": f"{element_id!r} is not in the UI map.",
                "hint": "Call ui_describe(view) to see the controls a screen offers."}

    info = allowed[element_id]
    reached = await _broadcast("ui.click", {"element_id": element_id})
    if reached == 0:
        return _no_listener(f"click {element_id!r}")

    return {"success": True, "element_id": element_id, "view": info["view"],
            "does": info["does"], "clients_notified": reached}


_LIVE_TASK_STATUSES = ("in_progress", "running")


@register_tool(
    name="ui_reload",
    description=(
        "Reload Tala's window so an edit to renderer/ takes effect without restarting "
        "the app. Refuses while a task is mid-run unless force=True, because a reload "
        "discards in-flight UI state. Use after editing renderer files."
    ),
)
async def ui_reload(force: bool = False) -> dict[str, Any]:
    """
    NAME
           ui_reload - apply a UI change without restarting the app

    SYNOPSIS
           ui_reload(force: bool = False) -> dict[str, Any]
    """
    if not force:
        try:
            from python.harness import db
            rows = await db.fetch(
                "SELECT id, description, status FROM tasks WHERE status = ANY($1::text[])",
                list(_LIVE_TASK_STATUSES),
            )
            if rows:
                return {
                    "success": False,
                    "error": "tasks_in_flight",
                    "detail": (
                        f"{len(rows)} task(s) are still running. Reloading discards "
                        "in-flight UI state. Wait, or call again with force=True."
                    ),
                    "tasks": [
                        {"id": r["id"], "status": r["status"],
                         "description": (r["description"] or "")[:80]}
                        for r in rows[:5]
                    ],
                }
        except Exception as exc:
            # A database that cannot be reached is not a reason to block a
            # reload, but the caller should know the check did not happen.
            log.warning("ui_reload: could not check for live tasks: %s", exc)
            reached = await _broadcast("ui.reload", {"forced": bool(force)})
            if reached == 0:
                return _no_listener("reload the window")
            return {"success": True, "clients_notified": reached,
                    "warning": f"could not verify live tasks before reloading: {exc}"}

    reached = await _broadcast("ui.reload", {"forced": bool(force)})
    if reached == 0:
        return _no_listener("reload the window")
    return {"success": True, "forced": bool(force), "clients_notified": reached}
