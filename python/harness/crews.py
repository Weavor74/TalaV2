"""
crews.py — the organisation: who reports to whom, and what that does.

Steven's definition, and the reason crews and swarms are separate things:

===============  ==========================================  ==========================================
                 Crew                                        Swarm
===============  ==========================================  ==========================================
topology         structured — linear pipelines or explicit    decentralised mesh — peer-to-peer routing,
                 manager/worker trees                         event-driven graphs
control flow     deterministic, defined upfront               emergent — handoffs as conditions evolve
roles            deep personas, fixed responsibilities        thin utility functions and tool interfaces
best for         predictable multi-stage pipelines            open-ended exploration, dynamic dispatch
===============  ==========================================  ==========================================

The dividing line is **control flow**, not headcount. That matters here because
everything in this module is deliberately deterministic: work goes where the
structure says it goes, and when it cannot be done it goes up the reporting
line. Nothing negotiates, and nothing decides at runtime who takes over. An
agent choosing its own successor would be a swarm, and this is not that.

Three things were wrong before this existed:

* **Hierarchy could not be written down** (UI-049). No ``parent_id``,
  ``manager_id``, ``reports_to`` or ``supervisor`` existed anywhere, so "one
  boss and many subordinates" was inexpressible. ``role`` is free text and
  carries no structure: agents labelled ``Boss`` and ``Worker`` were siblings.
* **A crew was a category, not a crew** (UI-050). ``agents.crew_type`` holds
  classifications like "Business"; two agents sharing one are not thereby
  colleagues. It stays as the classification it is, and ``crew_id`` is the
  membership.
* **Nothing consumed any of it** (UI-051). The fields round-tripped to the UI
  and changed a chip on a card. So this module exists to be *called*:
  :func:`assign` routes work into a crew and :func:`escalate` routes it back
  up, and both write ``tasks.assigned_agent_id``, which is what the executors
  actually read.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

TEAM, DEPARTMENT = "team", "department"
KINDS = (TEAM, DEPARTMENT)

#: How far any walk up or down the organisation may go.
#:
#: Not a guess about org design — a guard. A ``reports_to`` cycle turns every
#: upward walk into an infinite loop, and :func:`_would_cycle` refuses to create
#: one, but a row written by hand or by an older version is not covered by that.
#: The depth limit means a corrupted hierarchy degrades to a truncated answer
#: instead of a hung task.
MAX_DEPTH = 64


class CrewError(Exception):
    """A structural change that would not make sense."""


# ── crews ────────────────────────────────────────────────────────────────────

async def create(name: str, kind: str = TEAM, parent_crew_id: str = "",
                 purpose: str = "", description: str = "") -> dict[str, Any]:
    """Create a team or a department."""
    from python.harness import db

    name = str(name or "").strip()
    if not name:
        raise CrewError("a crew needs a name")

    kind = str(kind or TEAM).strip().lower()
    if kind not in KINDS:
        raise CrewError(f"kind must be one of {', '.join(KINDS)}")

    parent = str(parent_crew_id or "").strip() or None
    if parent:
        if not await _crew_exists(parent):
            raise CrewError("that parent crew does not exist")

    crew_id = f"crew-{uuid.uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO crews (id, name, kind, parent_crew_id, purpose, description) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        crew_id, name, kind, parent, str(purpose or ""), str(description or ""),
        caller="crews.create",
    )
    log.info("crew: created %s (%s) %s", name, kind, crew_id)
    return {"id": crew_id, "name": name, "kind": kind,
            "parent_crew_id": parent or ""}


async def update(crew_id: str, **fields: Any) -> dict[str, Any]:
    """Rename a crew, move it under another, or change what it is for."""
    from python.harness import db

    crew_id = str(crew_id or "").strip()
    if not await _crew_exists(crew_id):
        raise CrewError("that crew does not exist")

    if "parent_crew_id" in fields:
        parent = str(fields["parent_crew_id"] or "").strip() or None
        if parent:
            if parent == crew_id:
                raise CrewError("a crew cannot sit inside itself")
            if await _crew_would_cycle(crew_id, parent):
                # Departments containing each other is not a hierarchy, and
                # every walk over it would loop.
                raise CrewError("that would put a crew inside its own descendant")
        fields["parent_crew_id"] = parent

    if "kind" in fields:
        kind = str(fields["kind"] or "").strip().lower()
        if kind not in KINDS:
            raise CrewError(f"kind must be one of {', '.join(KINDS)}")
        fields["kind"] = kind

    allowed = {"name", "kind", "parent_crew_id", "purpose", "description"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if not changes:
        return {"updated": False}

    assignments = ", ".join(f"{key} = ${n + 2}" for n, key in enumerate(changes))
    await db.execute(
        f"UPDATE crews SET {assignments}, updated_at = NOW() WHERE id = $1",
        crew_id, *changes.values(), caller="crews.update",
    )
    return {"updated": True, "id": crew_id, **changes}


async def delete(crew_id: str) -> dict[str, Any]:
    """
    Disband a crew.

    Members are released rather than deleted, and sub-crews are lifted to this
    crew's own parent instead of being orphaned. Disbanding a department should
    not delete the people in it, and the schema's ``ON DELETE SET NULL`` alone
    would leave its teams pointing at nothing.
    """
    from python.harness import db

    crew_id = str(crew_id or "").strip()
    row = await db.fetchrow("SELECT parent_crew_id FROM crews WHERE id = $1",
                            crew_id)
    if not row:
        raise CrewError("that crew does not exist")

    released = await db.fetchval(
        "SELECT COUNT(*) FROM agents WHERE crew_id = $1", crew_id) or 0
    lifted = await db.fetchval(
        "SELECT COUNT(*) FROM crews WHERE parent_crew_id = $1", crew_id) or 0

    await db.execute("UPDATE crews SET parent_crew_id = $2 WHERE parent_crew_id = $1",
                     crew_id, row["parent_crew_id"], caller="crews.delete")
    await db.execute("DELETE FROM crews WHERE id = $1", crew_id,
                     caller="crews.delete")

    log.info("crew: disbanded %s — %d member(s) released, %d sub-crew(s) lifted",
             crew_id, released, lifted)
    return {"deleted": True, "released": released, "lifted": lifted}


async def listing() -> list[dict[str, Any]]:
    """Every crew, with its headcount."""
    from python.harness import db

    rows = await db.fetch(
        "SELECT c.id, c.name, c.kind, c.parent_crew_id, c.purpose, c.description, "
        "       COUNT(a.id) AS members "
        "FROM crews c LEFT JOIN agents a ON a.crew_id = c.id "
        "GROUP BY c.id ORDER BY c.kind DESC, c.name"
    )
    return [{"id": r["id"], "name": r["name"], "kind": r["kind"],
             "parent_crew_id": r["parent_crew_id"] or "",
             "purpose": r["purpose"], "description": r["description"],
             "members": r["members"]} for r in rows]


# ── membership and reporting ─────────────────────────────────────────────────

async def set_membership(agent_id: str, crew_id: str = "",
                         reports_to: str = "") -> dict[str, Any]:
    """
    Put an agent in a crew and under a manager. Either may be cleared.

    ``reports_to`` is checked against the existing tree before it is written.
    A cycle is not a strange configuration to be tolerated — it makes
    :func:`chain_of_command` and :func:`escalate` walk forever, so work would
    stop rather than misroute.
    """
    from python.harness import db

    agent_id = str(agent_id or "").strip()
    if not await _agent_exists(agent_id):
        raise CrewError("that agent does not exist")

    crew = str(crew_id or "").strip() or None
    if crew and not await _crew_exists(crew):
        raise CrewError("that crew does not exist")

    manager = str(reports_to or "").strip() or None
    if manager:
        if manager == agent_id:
            raise CrewError("an agent cannot report to itself")
        if not await _agent_exists(manager):
            raise CrewError("that manager does not exist")
        if await _would_cycle(agent_id, manager):
            raise CrewError(
                "that would make the reporting line a loop — the proposed "
                "manager already reports to this agent")

    await db.execute(
        "UPDATE agents SET crew_id = $2, reports_to = $3, updated_at = NOW() "
        "WHERE id = $1",
        agent_id, crew, manager, caller="crews.set_membership",
    )
    return {"agent_id": agent_id, "crew_id": crew or "",
            "reports_to": manager or ""}


async def members(crew_id: str) -> list[dict[str, Any]]:
    """Everyone in one crew."""
    from python.harness import db

    rows = await db.fetch(
        "SELECT id, name, role, crew_type, reports_to, is_active "
        "FROM agents WHERE crew_id = $1 ORDER BY name", str(crew_id or "").strip())
    return [dict(r) for r in rows]


async def chain_of_command(agent_id: str) -> list[dict[str, Any]]:
    """
    Everyone this agent reports to, nearest manager first.

    Bounded and cycle-aware. :func:`set_membership` refuses to create a loop,
    but a row written by hand or by an older version is not covered by that,
    and an unbounded walk over one never returns.
    """
    from python.harness import db

    chain: list[dict[str, Any]] = []
    seen = {str(agent_id or "").strip()}
    current = str(agent_id or "").strip()

    for _ in range(MAX_DEPTH):
        row = await db.fetchrow(
            "SELECT m.id, m.name, m.role, m.crew_id FROM agents a "
            "JOIN agents m ON m.id = a.reports_to WHERE a.id = $1", current)
        if not row:
            break
        if row["id"] in seen:
            log.warning("crew: reporting cycle at %s — stopping the walk", row["id"])
            break
        seen.add(row["id"])
        chain.append(dict(row))
        current = row["id"]
    else:
        log.warning("crew: reporting line deeper than %d from %s", MAX_DEPTH, agent_id)

    return chain


async def subordinates(agent_id: str, direct_only: bool = False) -> list[dict[str, Any]]:
    """Everyone who reports to this agent, directly or below."""
    from python.harness import db

    agent_id = str(agent_id or "").strip()
    rows = await db.fetch(
        "SELECT id, name, role, crew_id, reports_to FROM agents "
        "WHERE reports_to = $1 ORDER BY name", agent_id)
    found = [dict(r) for r in rows]
    if direct_only:
        return found

    out = list(found)
    seen = {agent_id} | {r["id"] for r in found}
    frontier = list(found)
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        rows = await db.fetch(
            "SELECT id, name, role, crew_id, reports_to FROM agents "
            "WHERE reports_to = ANY($1::text[]) ORDER BY name",
            [r["id"] for r in frontier])
        frontier = [dict(r) for r in rows if r["id"] not in seen]
        seen.update(r["id"] for r in frontier)
        out.extend(frontier)
    return out


async def chart() -> dict[str, Any]:
    """The whole organisation, for drawing."""
    from python.harness import db

    crews = await listing()
    rows = await db.fetch(
        "SELECT id, name, role, crew_type, crew_id, reports_to, is_active "
        "FROM agents ORDER BY name")
    agents = [{"id": r["id"], "name": r["name"], "role": r["role"],
               "crew_type": r["crew_type"], "crew_id": r["crew_id"] or "",
               "reports_to": r["reports_to"] or "", "is_active": r["is_active"]}
              for r in rows]

    managed = {a["reports_to"] for a in agents if a["reports_to"]}
    return {
        "crews": crews,
        "agents": agents,
        "unassigned": [a["id"] for a in agents if not a["crew_id"]],
        "managers": sorted(managed),
        "cycles": await find_cycles(),
    }


async def find_cycles() -> list[list[str]]:
    """
    Reporting loops, if any exist.

    :func:`set_membership` will not create one, so a loop here means a row was
    written another way. Reported rather than repaired: which link to break is
    the owner's decision, and guessing would silently restructure their
    organisation.
    """
    from python.harness import db

    rows = await db.fetch(
        "SELECT id, reports_to FROM agents WHERE reports_to IS NOT NULL")
    manager_of = {r["id"]: r["reports_to"] for r in rows}

    cycles: list[list[str]] = []
    checked: set[str] = set()
    for start in manager_of:
        if start in checked:
            continue
        path: list[str] = []
        seen: set[str] = set()
        current: str | None = start
        while current and current in manager_of and current not in seen:
            seen.add(current)
            path.append(current)
            current = manager_of[current]
        checked.update(seen)
        if current and current in seen:
            cycle = path[path.index(current):]
            if sorted(cycle) not in [sorted(c) for c in cycles]:
                cycles.append(cycle)
    return cycles


# ── what the hierarchy is *for* ──────────────────────────────────────────────

async def assign(task_id: str, crew_id: str, role: str = "") -> dict[str, Any]:
    """
    Route a task into a crew.

    This is the consumer the crew fields never had (UI-051). It writes
    ``tasks.assigned_agent_id``, which is the column the executors actually
    read — marking an agent as crew previously changed a chip on a card and
    nothing else.

    Selection is deterministic, because a crew is: a member whose ``role``
    matches is preferred, then any active member, in a stable order. No
    negotiation, no runtime handoff — that would be a swarm.
    """
    from python.harness import db

    task_id = str(task_id or "").strip()
    crew_id = str(crew_id or "").strip()
    if not await _crew_exists(crew_id):
        raise CrewError("that crew does not exist")

    role = str(role or "").strip()
    candidates = await db.fetch(
        "SELECT id, name, role, is_active FROM agents WHERE crew_id = $1 "
        "ORDER BY is_active DESC, name", crew_id)
    if not candidates:
        raise CrewError("that crew has no members to give the work to")

    chosen = None
    if role:
        chosen = next((c for c in candidates
                       if c["role"].strip().lower() == role.lower()), None)
    if chosen is None:
        chosen = next((c for c in candidates if c["is_active"]), None)
    if chosen is None:
        # Every member is inactive. Assigning anyway and saying so beats
        # refusing: the work is recorded against the right crew, and the
        # caller learns why nothing is moving.
        chosen = candidates[0]
        log.warning("crew: every member of %s is inactive; assigned to %s anyway",
                    crew_id, chosen["id"])

    status = await db.execute(
        "UPDATE tasks SET assigned_agent_id = $2 WHERE id = $1",
        task_id, chosen["id"], caller="crews.assign")
    if str(status).endswith(" 0"):
        raise CrewError("that task does not exist")

    log.info("crew: task %s assigned to %s (%s)", task_id, chosen["name"], crew_id)
    return {"task_id": task_id, "assigned_agent_id": chosen["id"],
            "assigned_to": chosen["name"], "crew_id": crew_id,
            "matched_role": bool(role and chosen["role"].strip().lower() == role.lower()),
            "active": chosen["is_active"]}


async def escalate(task_id: str, reason: str = "") -> dict[str, Any]:
    """
    Send a task up the reporting line.

    Steven: reporting is *"both ownership and upward work routing"*. This is
    the upward half — the reason a hierarchy is worth having rather than
    drawing.

    One step per call, to the current assignee's manager. Escalating straight
    to the top would skip the person most likely to be able to help, and
    looping to the top inside one call would hide where it actually got stuck.
    """
    from python.harness import db

    task_id = str(task_id or "").strip()
    row = await db.fetchrow(
        "SELECT assigned_agent_id FROM tasks WHERE id = $1", task_id)
    if not row:
        raise CrewError("that task does not exist")
    if not row["assigned_agent_id"]:
        raise CrewError("that task is not assigned to anybody to escalate from")

    current = row["assigned_agent_id"]
    manager = await db.fetchrow(
        "SELECT m.id, m.name, m.role FROM agents a "
        "JOIN agents m ON m.id = a.reports_to WHERE a.id = $1", current)
    if not manager:
        return {"escalated": False, "task_id": task_id, "from": current,
                "reason": "nobody to escalate to — that agent is at the top "
                          "of its reporting line"}

    await db.execute("UPDATE tasks SET assigned_agent_id = $2 WHERE id = $1",
                     task_id, manager["id"], caller="crews.escalate")
    await db.log_to_db(
        "WARNING",
        f"Task {task_id} escalated from {current} to {manager['name']}"
        + (f": {reason}" if reason else ""),
        {"task_id": task_id, "from": current, "to": manager["id"],
         "reason": reason},
    )
    log.info("crew: task %s escalated to %s", task_id, manager["name"])
    return {"escalated": True, "task_id": task_id, "from": current,
            "to": manager["id"], "to_name": manager["name"]}


async def owns(manager_id: str, agent_id: str) -> bool:
    """
    Whether one agent is anywhere above another.

    The ownership half of reporting. A manager can act on work belonging to
    anyone beneath them, at any depth — otherwise a department head could not
    touch anything two levels down, which is not what reporting means.
    """
    manager_id = str(manager_id or "").strip()
    return any(link["id"] == manager_id
               for link in await chain_of_command(agent_id))


# ── helpers ──────────────────────────────────────────────────────────────────

async def _crew_exists(crew_id: str) -> bool:
    from python.harness import db
    return bool(await db.fetchval("SELECT 1 FROM crews WHERE id = $1", crew_id))


async def _agent_exists(agent_id: str) -> bool:
    from python.harness import db
    return bool(await db.fetchval("SELECT 1 FROM agents WHERE id = $1", agent_id))


async def _would_cycle(agent_id: str, proposed_manager: str) -> bool:
    """Whether the proposed manager already reports to this agent."""
    return any(link["id"] == agent_id
               for link in await chain_of_command(proposed_manager))


async def _crew_would_cycle(crew_id: str, proposed_parent: str) -> bool:
    """Whether the proposed parent already sits inside this crew."""
    from python.harness import db

    current: str | None = proposed_parent
    for _ in range(MAX_DEPTH):
        if not current:
            return False
        if current == crew_id:
            return True
        row = await db.fetchrow(
            "SELECT parent_crew_id FROM crews WHERE id = $1", current)
        current = row["parent_crew_id"] if row else None
    return True
