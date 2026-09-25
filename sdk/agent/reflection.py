"""
sdk/agent/reflection.py -- the loop that reads her own state and acts on it.

The About panel claimed a "Reflection System (Self-improvement loop)" and
the REFLECTION_SERVICE probe answered it in the same breath: *"the absence of a
reflection subsystem to act on them is UI-086 and is not something this probe
can invent."* She could already read her own health and logs (UI-086 shipped
``read_own_health`` / ``read_own_logs`` / ``diagnose_self``) and repair her own
documentation when asked. Nothing ran any of it unprompted. This is the thing
that does.

What makes it autonomous and survivable at the same time
--------------------------------------------------------

The failure this repository keeps repeating is not "the loop did nothing", it
is "the loop did something confidently wrong and reported success": a rollback
that deleted 427 tracked files, goals whose write target was regex-scraped
from their own prose so ``run X.py`` overwrote ``X.py``, 14 of 17 auto-minted
learning nodes that were junk. An unattended loop with a free hand repeats
that history at its own cadence, at night, while nobody is reading.

So the loop acts on exactly what it can repair *correctly*, and records the
rest as evidence:

* A finding is only acted on when a **repair is registered for it** -- a named,
  bounded, already-approved operation with a known blast radius. There is no
  path from "something is wrong" to "improvise a fix".
* Repairs are **dry-run first** by default, and a dry run that reports failure
  cancels the real one.
* One action per cycle. A cycle that finds five problems fixes the first and
  leaves the other four for the next pass, so a bad repair cannot cascade.
* A repair that runs twice without clearing its finding is **abandoned**, not
  retried forever. Loops that cannot tell "fixed" from "kept trying" are how a
  scheduler quietly burns a machine all night.
* The emergency halt is checked at entry, and again before acting. A halt that
  only stops things at the door is the bug that made the last one useless.

Everything it does is recorded in ``status()``, which ``probe()`` at the
foot of this module reports on -- so the loop's own health is measured rather than asserted,
which is the whole point of the probe that used to apologise for its absence.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from sdk.agent.tools import register_tool

log = logging.getLogger(__name__)

#: How often the sweep is allowed to run. The scheduler ticks every minute;
#: this is what stops every tick from being a reflection.
DEFAULT_INTERVAL_MINUTES = 60

#: Actions one cycle may take. Deliberately one.
MAX_ACTIONS_PER_CYCLE = 1

#: How many consecutive cycles may attempt the same repair before it is
#: abandoned. Two: one to try, one to confirm trying again changed nothing.
MAX_REPAIR_ATTEMPTS = 2


_state: dict[str, Any] = {
    "last_run_at": None,
    "last_status": "never run",
    "cycles": 0,
    "findings": [],
    "actions": [],
    "abandoned": {},
}

#: finding key -> consecutive attempts that did not clear it.
_attempts: dict[str, int] = {}


def status() -> dict[str, Any]:
    """What the loop last saw and did. Read by ``probe()`` below."""
    return {
        **_state,
        "findings": list(_state["findings"]),
        "actions": list(_state["actions"]),
        "abandoned": dict(_state["abandoned"]),
    }


def reset_state() -> None:
    """Test seam. Not called in normal operation."""
    _state.update({
        "last_run_at": None, "last_status": "never run", "cycles": 0,
        "findings": [], "actions": [], "abandoned": {},
    })
    _attempts.clear()


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

async def _observe(minutes: int) -> list[dict[str, Any]]:
    """
    Findings, each carrying the evidence for itself.

    Two sources, because they answer different questions: the health probes say
    which subsystem is wrong, and documentation verification says whether what
    she believes about herself still matches the code. A reflection loop that
    only watched errors would never notice its own description going stale.
    """
    findings: list[dict[str, Any]] = []

    try:
        from sdk.agent.self_diagnosis import diagnose_self
        seen = await diagnose_self(minutes=minutes)
        for service in seen.get("needs_attention", []) or []:
            name = service if isinstance(service, str) else service.get("service", "?")
            detail = "" if isinstance(service, str) else service.get("detail", "")
            findings.append({
                "key": f"health:{name}",
                "kind": "health",
                "summary": f"{name} needs attention",
                "evidence": detail or seen.get("overall", ""),
            })
    except Exception as exc:  # noqa: BLE001
        log.error("reflection: health observation failed: %s", exc)
        findings.append({
            "key": "health:unobservable",
            "kind": "health",
            "summary": "could not read own health",
            "evidence": f"{type(exc).__name__}: {exc}",
        })

    try:
        from sdk.agent.documentation_nodes import verify_documentation_nodes
        verified = verify_documentation_nodes()
        if not verified.get("success"):
            errors = verified.get("errors", []) or []
            findings.append({
                "key": "docs:invalid_nodes",
                "kind": "documentation",
                "summary": f"{len(errors)} documentation node(s) do not verify",
                "evidence": "; ".join(str(e) for e in errors[:3]),
            })
    except Exception as exc:  # noqa: BLE001
        log.error("reflection: documentation observation failed: %s", exc)

    return findings


# ---------------------------------------------------------------------------
# Repairs -- the only things this loop may do on its own
# ---------------------------------------------------------------------------

async def _repair_documentation(finding: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    """
    Re-derive documentation from source and apply the corrections.

    Safe to run unattended because it is bounded to ``nodes/*.html``, derives
    its content from the AST rather than inventing prose, and touches no code.
    Its comparison heuristic was the loose part -- it once produced 473
    corrections of which 4 were real -- and that is fixed; if it regresses, the
    blast radius is still documentation, and git still has the previous copy.
    """
    if dry_run:
        # There is no dry-run mode on the heal bridge, so the dry run is the
        # verification half on its own: it reports what is wrong and writes
        # nothing. A dry run that cannot fail is not a gate, so this reports
        # failure when verification itself cannot run.
        from sdk.agent.documentation_nodes import verify_documentation_nodes
        verified = verify_documentation_nodes()
        return {
            "success": True,
            "would_act": not verified.get("success", False),
            "detail": f"{len(verified.get('errors', []) or [])} node(s) failing verification",
        }

    from sdk.agent.tools import verify_and_heal_documentation
    result = await verify_and_heal_documentation()
    return {
        "success": bool(result.get("success", True)),
        "detail": str(result.get("summary") or result)[:300],
    }


#: finding key -> (human name, repair). A finding with no entry here is
#: recorded and left alone; that absence is the safety property.
REPAIRS: dict[str, tuple[str, Callable[..., Awaitable[dict[str, Any]]]]] = {
    "docs:invalid_nodes": ("verify_and_heal_documentation", _repair_documentation),
}


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------

def _halted() -> bool:
    try:
        from sdk.agent.gates import RevocationHalt
        return bool(RevocationHalt.is_halted())
    except Exception:  # noqa: BLE001
        return False  # a halt that cannot be read must not wedge the loop shut


async def _reflect_once(
    *,
    minutes: int = 60,
    dry_run_first: bool = True,
    max_actions: int = MAX_ACTIONS_PER_CYCLE,
) -> dict[str, Any]:
    """
    One reflection: observe, act on what has a registered repair, record.

    ``dry_run_first`` is the gate. Turning it off is a deliberate choice to let
    repairs run unrehearsed, not a default anybody drifts into.
    """
    if _halted():
        _state["last_status"] = "skipped: emergency halt"
        return status()

    findings = await _observe(minutes)
    _state["cycles"] += 1
    _state["last_run_at"] = datetime.now(UTC).isoformat()
    _state["findings"] = findings
    actions: list[dict[str, Any]] = []

    # Findings that cleared since last cycle stop counting against their repair.
    live = {f["key"] for f in findings}
    for key in [k for k in _attempts if k not in live]:
        _attempts.pop(key, None)
        _state["abandoned"].pop(key, None)

    for finding in findings:
        if len(actions) >= max_actions:
            break
        key = finding["key"]
        registered = REPAIRS.get(key)
        if not registered:
            continue  # observed, recorded, not improvised on
        if _attempts.get(key, 0) >= MAX_REPAIR_ATTEMPTS:
            _state["abandoned"][key] = "repair ran and the finding persisted"
            continue
        if _halted():
            _state["last_status"] = "stopped mid-cycle: emergency halt"
            break

        name, repair = registered
        attempt: dict[str, Any] = {"finding": key, "repair": name}
        try:
            if dry_run_first:
                rehearsal = await repair(finding, dry_run=True)
                attempt["dry_run"] = rehearsal
                if not rehearsal.get("success"):
                    attempt["outcome"] = "cancelled: dry run failed"
                    actions.append(attempt)
                    continue
                if not rehearsal.get("would_act", True):
                    attempt["outcome"] = "skipped: dry run found nothing to do"
                    actions.append(attempt)
                    continue

            result = await repair(finding, dry_run=False)
            attempt["result"] = result
            attempt["outcome"] = "applied" if result.get("success") else "failed"
            _attempts[key] = _attempts.get(key, 0) + 1
        except Exception as exc:  # noqa: BLE001
            log.error("reflection: repair %s raised: %s", name, exc)
            attempt["outcome"] = f"raised {type(exc).__name__}: {exc}"
            _attempts[key] = _attempts.get(key, 0) + 1
        actions.append(attempt)

    _state["actions"] = actions
    if not _state["last_status"].startswith("stopped"):
        _state["last_status"] = (
            f"{len(findings)} finding(s), {len(actions)} action(s)" if findings
            else "nothing to report"
        )
    log.info("reflection: %s", _state["last_status"])
    return status()


_last_cycle_monotonic: float | None = None


async def reflect_if_due(interval_minutes: int = DEFAULT_INTERVAL_MINUTES) -> dict[str, Any] | None:
    """
    Run a cycle if one is due, otherwise nothing.

    The scheduler ticks every minute and this is what keeps that from meaning a
    reflection every minute. Monotonic, so a clock correction cannot make the
    next cycle due in an hour's time or immediately forever.

    Returns None when not due, so a caller can tell "did not run" from "ran and
    found nothing" -- a distinction the old probe could not make about itself.
    """
    global _last_cycle_monotonic
    import time as _time

    now = _time.monotonic()
    if _last_cycle_monotonic is not None:
        if now - _last_cycle_monotonic < interval_minutes * 60:
            return None
    _last_cycle_monotonic = now
    return await _reflect_once()


def _health_probe(recent_errors: int | None = None) -> tuple[str, str]:
    """
    This subsystem's own verdict, for the health panel.

    Lives here rather than in server.py's probe so the loop reports itself:
    the probe stays a thin caller, and what "healthy reflection" means is
    defined next to the thing being measured.

    ``recent_errors`` is the caller's error count for the window before a
    first cycle has run. None means it could not be read.
    """
    seen = status()
    if seen.get("cycles"):
        abandoned = seen.get("abandoned") or {}
        if abandoned:
            return "DEGRADED", (
                f"{len(abandoned)} finding(s) a repair could not clear: "
                + ", ".join(sorted(abandoned))
            )
        return "HEALTHY", f"last cycle {seen.get('last_run_at')}: {seen.get('last_status')}"

    if recent_errors is None:
        return "UNKNOWN", "reflection has not run, and the error log is unreadable"
    if recent_errors > 10:
        return "CRITICAL", f"{recent_errors} error(s) logged, reflection has not run yet"
    if recent_errors:
        return "DEGRADED", f"{recent_errors} error(s) logged, reflection has not run yet"
    # Deliberately not HEALTHY. Nothing has reflected; the errors merely happen
    # to be zero in one window, which is not the same as the loop working.
    return "MAINTENANCE", "reflection has not run a cycle yet"


async def probe() -> tuple[str, str]:
    """
    The REFLECTION_SERVICE health probe, whole.

    Kept here rather than in server.py so the subsystem answers for itself:
    the panel asks this module how reflection is doing instead of server.py
    reimplementing what "doing" means. server.py registers it by name.
    """
    from python.harness import db
    try:
        errors = await db.fetchval(
            "SELECT COUNT(*) FROM agent_log WHERE level = 'ERROR' "
            "AND created_at > NOW() - INTERVAL '5 minutes'"
        ) or 0
    except Exception:  # noqa: BLE001
        errors = None
    return _health_probe(recent_errors=errors)


# ---------------------------------------------------------------------------
# On demand
# ---------------------------------------------------------------------------
# Registered at the bottom, after everything it calls exists, and imported by
# tools.py the way every other tool module is. Two reasons beyond reachability:
# the scheduled cycle is hourly, so without this the only way to see what
# reflection would say is to wait; and a loop that can only be observed through
# its own side effects is the thing this subsystem was built to stop being.

@register_tool(
    name="reflect_now",
    description=(
        "Run a self-reflection cycle now: read own health and verify own "
        "documentation, apply any repair that is registered for what was "
        "found, and report findings that have no automatic repair. Returns "
        "what was seen and what was done."
    ),
)
async def reflect_now(minutes: int = 60, dry_run_first: bool = True) -> dict[str, Any]:
    """
    NAME
           reflect_now - run a reflection cycle immediately

    SYNOPSIS
           reflect_now(minutes: int = 60, dry_run_first: bool = True) -> dict

    DESCRIPTION
           Observes health and documentation, repairs what has a registered
           repair, records the rest. Same cycle the scheduler runs hourly,
           with the same guardrails; this only changes when it happens.

           Does not reset the scheduled interval, so an on-demand cycle cannot
           be used to starve the automatic one.
    """
    return await _reflect_once(minutes=minutes, dry_run_first=dry_run_first)
