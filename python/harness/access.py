"""
access.py — one grant model, so the three access systems can compose.

Three independent mechanisms existed, each with its own storage, vocabulary and
enforcement point, and none able to reference another (UI-062): guardrails on
prompt text, nine booleans on a contact, and firewall clearance levels. Nothing
expressed the model Steven actually specified (UI-101, UI-118).

His model, in his words:

* *"permissions are by invite only ... I have to invite her to access. This
  does not mean I have to invite her to be a contact. Just to access a file"* —
  identity and access are separate, and access is per resource.
* *"If you give a contact premissions it superseeds other settings, unless a
  lockdown is initiated, then no access anywhere"* — a grant wins, and lockdown
  wins over the grant.
* *"An agent, should be assigned just like a peer"* (Q-63) — the agent is a
  principal with assigned privileges, not a special case. There is no
  "locally initiated" exemption; the agent is always the actor.
* *"I could configure a user to have access to one of my agents. But that agent
  can only access a certain search API, on a certain port, at a single
  allocated time"* — a grant carries constraints, not just a yes.

So: a grant is ``(principal, resource, permission)`` with optional constraints,
recorded by invitation, and **default deny**. The owner is implicit and
unrestricted and has no rows; anyone else has exactly what they were given.

This module decides. It does not enforce -- enforcement belongs at each call
site, because a decision function that also performs the action is one nobody
can test in isolation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any

log = logging.getLogger(__name__)

OWNER = "owner"
AGENT = "agent"
PEER = "peer"

PRINCIPAL_KINDS = (OWNER, AGENT, PEER)


@dataclass(frozen=True)
class Principal:
    """Who is acting. The owner is implicit and unrestricted."""

    kind: str
    id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in PRINCIPAL_KINDS:
            raise ValueError(
                f"Unknown principal kind {self.kind!r}. "
                f"Expected one of {', '.join(PRINCIPAL_KINDS)}."
            )

    @property
    def is_owner(self) -> bool:
        return self.kind == OWNER

    def __str__(self) -> str:
        return self.kind if self.is_owner else f"{self.kind}:{self.id}"


@dataclass
class Decision:
    """
    Whether an action is permitted, and why.

    The reason is not decoration. A refusal a user cannot act on is the same
    problem as a silent failure, and every access denial in this codebase is
    something someone will have to debug.
    """

    allowed: bool
    reason: str
    principal: str = ""
    resource: str = ""
    permission: str = ""
    grant_id: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_window(text: str) -> tuple[time, time] | None:
    """``"09:00-17:00"`` into a pair of times, or None if unparseable."""
    try:
        start_text, _, end_text = str(text).partition("-")
        start = time.fromisoformat(start_text.strip())
        end = time.fromisoformat(end_text.strip())
        return start, end
    except (ValueError, AttributeError):
        return None


def _check_constraints(constraints: dict[str, Any],
                      context: dict[str, Any] | None = None) -> tuple[bool, str]:
    """
    Whether a grant's constraints are satisfied right now.

    Steven's example is the specification: access "to a certain search API, on
    a certain port, at a single allocated time". Each constraint is optional;
    an absent constraint does not restrict, but an *unrecognised* one denies --
    a constraint nobody evaluates is worse than no constraint, because it reads
    as a restriction that is not there.
    """
    context = context or {}
    # An unrecognised constraint denies. A constraint nobody evaluates is worse
    # than no constraint, because it reads as a restriction that is not there.
    well_formed, problem = _validate_constraints(constraints)
    if not well_formed:
        return False, f"the grant is not usable: {problem}"

    now = _now()

    expires = constraints.get("expires_at")
    if expires:
        try:
            when = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if now > when:
                return False, f"the grant expired at {when.isoformat()}"
        except ValueError:
            return False, f"the grant has an unreadable expiry ({expires!r})"

    starts = constraints.get("not_before")
    if starts:
        try:
            when = datetime.fromisoformat(str(starts).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if now < when:
                return False, f"the grant does not start until {when.isoformat()}"
        except ValueError:
            return False, f"the grant has an unreadable start time ({starts!r})"

    hours = constraints.get("hours")
    if hours:
        window = _parse_window(hours)
        if window is None:
            return False, f"the grant has an unreadable time window ({hours!r})"
        start, end = window
        current = now.time()
        inside = (start <= current <= end) if start <= end else (
            current >= start or current <= end  # a window that crosses midnight
        )
        if not inside:
            return False, f"outside the permitted window {hours} (now {current:%H:%M} UTC)"

    ports = constraints.get("ports")
    if ports:
        port = context.get("port")
        if port is None:
            return False, "this grant is limited to specific ports and no port was given"
        if int(port) not in [int(p) for p in ports]:
            return False, f"port {port} is not among the permitted ports {ports}"

    hosts = constraints.get("hosts")
    if hosts:
        host = str(context.get("host") or "").lower()
        if not host:
            return False, "this grant is limited to specific hosts and no host was given"
        if host not in [str(h).lower() for h in hosts]:
            return False, f"host {host!r} is not among the permitted hosts"

    return True, ""


def _validate_constraints(constraints: dict[str, Any]) -> tuple[bool, str]:
    """
    Whether a set of constraints is *well formed*, without evaluating it.

    Separate from :func:`check_constraints` because the two questions are
    different and conflating them was a real bug: grant() validated by running
    the evaluator against a dummy context, so a legitimate ``ports: [443]``
    constraint refused its own grant on the grounds that port 0 was not in the
    list. Structure is checked when a grant is written; satisfaction is checked
    when it is used.
    """
    known = {"expires_at", "not_before", "hours", "ports", "hosts", "note"}
    unknown = set(constraints) - known
    if unknown:
        return False, (
            f"unrecognised constraint(s): {', '.join(sorted(unknown))}. "
            f"Supported: {', '.join(sorted(known))}."
        )

    for ts_field in ("expires_at", "not_before"):
        value = constraints.get(ts_field)
        if value:
            try:
                datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return False, f"{ts_field} is not a readable timestamp ({value!r})"

    hours = constraints.get("hours")
    if hours and _parse_window(hours) is None:
        return False, f"hours is not a readable window ({hours!r}); expected HH:MM-HH:MM"

    for list_field in ("ports", "hosts"):
        value = constraints.get(list_field)
        if value is not None and not isinstance(value, (list, tuple)):
            return False, f"{list_field} must be a list"
        if list_field == "ports" and value:
            try:
                [int(p) for p in value]
            except (TypeError, ValueError):
                return False, "ports must all be numbers"

    return True, ""


async def check(
    principal: Principal,
    resource: str,
    permission: str = "read",
    context: dict[str, Any] | None = None,
) -> Decision:
    """
    Decide whether *principal* may do *permission* on *resource*.

    Order matters and is deliberate:

    1. **Lockdown denies everyone but the owner.** Steven: a grant "superseeds
       other settings, unless a lockdown is initiated, then no access anywhere".
    2. **The owner is unrestricted.** Their machine, their data.
    3. **Everyone else is denied unless a grant says otherwise.** Default deny
       is the whole point of "by invite only".
    """
    resource = str(resource or "").strip()
    permission = str(permission or "read").strip().lower()

    if not resource:
        return Decision(False, "no resource was named", str(principal), resource, permission)

    # 1. lockdown
    try:
        from python.harness import environment as env
        state = await env.get_state()
        if state.get("lockdown") and not principal.is_owner:
            return Decision(
                False,
                "lockdown is engaged: no principal other than the owner has "
                "access to anything until it is released",
                str(principal), resource, permission,
            )
    except Exception as exc:
        # Not knowing whether lockdown is on is not permission to proceed.
        if not principal.is_owner:
            return Decision(
                False,
                f"could not determine lockdown state ({type(exc).__name__}), "
                f"so access is refused",
                str(principal), resource, permission,
            )

    # 2. the owner
    if principal.is_owner:
        return Decision(True, "the owner is unrestricted", str(principal),
                        resource, permission)

    if not principal.id:
        return Decision(False, f"a {principal.kind} principal with no id cannot be "
                               f"matched to a grant", str(principal), resource, permission)

    # 3. an explicit grant, or nothing
    from python.harness import db
    try:
        rows = await db.fetch(
            """
            SELECT id, permission, constraints FROM access_grants
            WHERE principal_kind = $1 AND principal_id = $2
              AND resource = $3
              AND (permission = $4 OR permission = 'all')
            """,
            principal.kind, principal.id, resource, permission,
        )
    except Exception as exc:
        return Decision(
            False,
            f"could not read the grant table ({type(exc).__name__}), so access "
            f"is refused",
            str(principal), resource, permission,
        )

    if not rows:
        return Decision(
            False,
            f"{principal} has no grant for {permission!r} on {resource!r}. "
            f"Access is by invitation: nothing is permitted by default.",
            str(principal), resource, permission,
        )

    problems = []
    for row in rows:
        constraints = row["constraints"]
        if isinstance(constraints, str):
            try:
                constraints = json.loads(constraints)
            except (ValueError, json.JSONDecodeError):
                constraints = {}
        constraints = constraints if isinstance(constraints, dict) else {}

        ok, why = _check_constraints(constraints, context)
        if ok:
            return Decision(True, "granted by invitation", str(principal),
                            resource, permission, row["id"], constraints)
        problems.append(why)

    return Decision(
        False,
        f"{principal} has a grant for {resource!r} but it does not apply: "
        + "; ".join(problems),
        str(principal), resource, permission,
    )


async def grant(
    principal: Principal,
    resource: str,
    permission: str = "read",
    constraints: dict[str, Any] | None = None,
    granted_by: str = "owner",
    note: str = "",
) -> dict[str, Any]:
    """Record a grant. Only the owner ever calls this."""
    from python.harness import db
    import uuid

    if principal.is_owner:
        raise ValueError("The owner is unrestricted; granting to them is meaningless.")
    if not principal.id:
        raise ValueError(f"A {principal.kind} grant needs a principal id.")
    resource = str(resource or "").strip()
    if not resource:
        raise ValueError("A grant needs a resource.")

    constraints = constraints or {}
    ok, why = _validate_constraints(constraints)
    if not ok:
        raise ValueError(f"Those constraints are not usable: {why}")

    grant_id = f"grant-{uuid.uuid4().hex[:12]}"
    await db.execute(
        """
        INSERT INTO access_grants
            (id, principal_kind, principal_id, resource, permission,
             constraints, granted_by, note)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
        ON CONFLICT (principal_kind, principal_id, resource, permission)
        DO UPDATE SET constraints = EXCLUDED.constraints,
                      granted_by = EXCLUDED.granted_by,
                      note = EXCLUDED.note,
                      granted_at = NOW()
        """,
        grant_id, principal.kind, principal.id, resource, permission,
        json.dumps(constraints), granted_by, note,
        caller="access.grant",
    )
    log.info("access: granted %s %s on %s%s", principal, permission, resource,
             f" with {sorted(constraints)}" if constraints else "")
    return {"id": grant_id, "principal": str(principal), "resource": resource,
            "permission": permission, "constraints": constraints}


async def revoke(principal: Principal, resource: str,
                 permission: str = "") -> dict[str, Any]:
    """Withdraw a grant. Omitting *permission* withdraws all of them."""
    from python.harness import db

    if permission:
        await db.execute(
            "DELETE FROM access_grants WHERE principal_kind = $1 AND "
            "principal_id = $2 AND resource = $3 AND permission = $4",
            principal.kind, principal.id, resource, permission,
            caller="access.revoke",
        )
    else:
        await db.execute(
            "DELETE FROM access_grants WHERE principal_kind = $1 AND "
            "principal_id = $2 AND resource = $3",
            principal.kind, principal.id, resource,
            caller="access.revoke",
        )
    log.info("access: revoked %s on %s from %s", permission or "all", resource, principal)
    return {"revoked": True, "principal": str(principal), "resource": resource}


async def list_grants(principal: Principal | None = None) -> list[dict[str, Any]]:
    """Every grant, or every grant held by one principal."""
    from python.harness import db

    if principal and principal.id:
        rows = await db.fetch(
            "SELECT * FROM access_grants WHERE principal_kind = $1 AND "
            "principal_id = $2 ORDER BY granted_at DESC",
            principal.kind, principal.id,
        )
    else:
        rows = await db.fetch("SELECT * FROM access_grants ORDER BY granted_at DESC")

    out = []
    for row in rows:
        constraints = row["constraints"]
        if isinstance(constraints, str):
            try:
                constraints = json.loads(constraints)
            except (ValueError, json.JSONDecodeError):
                constraints = {}
        out.append({
            "id": row["id"],
            "principal": f"{row['principal_kind']}:{row['principal_id']}",
            "principal_kind": row["principal_kind"],
            "principal_id": row["principal_id"],
            "resource": row["resource"],
            "permission": row["permission"],
            "constraints": constraints or {},
            "granted_by": row["granted_by"],
            "granted_at": str(row["granted_at"]),
            "note": row["note"],
        })
    return out


async def resolve(
    principal: Principal,
    resource: str,
    permission: str,
    fallback_allowed: bool,
    fallback_reason: str,
    context: dict[str, Any] | None = None,
) -> Decision:
    """
    Compose an explicit grant with a subsystem's own rule.

    This is how the three existing access systems join the model without being
    torn out. Steven's ordering, exactly:

        "If you give a contact premissions it superseeds other settings, unless
        a lockdown is initiated, then no access anywhere"

    So: lockdown denies; the owner is unrestricted; an explicit grant wins over
    whatever the subsystem would have said; and with no grant, the subsystem's
    own rule stands. A per-section privacy setting, a skill's shared scope and a
    firewall clearance level all keep working exactly as they did for anyone who
    was never granted anything.

    The alternative -- replacing each subsystem's rule with grants -- would have
    meant every existing setting silently becoming "deny" the moment this
    shipped. Composition means nothing changes for a user who never opens the
    new panel.
    """
    decision = await check(principal, resource, permission, context)

    # Only lockdown is final. A failed *grant* lookup is not: in this model a
    # grant can only widen access -- it supersedes a subsystem's deny with an
    # allow, and never the reverse -- so being unable to read the table can
    # only make the outcome more restrictive, never less. Treating it as a hard
    # deny would take a user's own "public" profile section away because of a
    # database hiccup, which is a worse failure and not a safer one.
    #
    # Lockdown is the opposite: it exists to remove access, so not knowing
    # whether it is engaged must deny.
    if not decision.allowed and (
        "lockdown" in decision.reason
        or "could not determine" in decision.reason
    ):
        return decision

    if decision.allowed:
        return decision

    # No grant. The subsystem decides, and says so in its own words.
    return Decision(
        fallback_allowed,
        fallback_reason,
        str(principal), resource, permission,
    )
