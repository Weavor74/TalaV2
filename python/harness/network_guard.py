"""
network_guard.py — guardrails that govern the harness, not just the text.

Steven's scope for the guardrails panel:

    "This isnt just about what the agent can talk about. This is about the
    entire harness. Gaurdrails should be able to address firewall rules, and
    port access. It should allow for permissions from a contact and what
    information of yours they can access. As an example: I could configure a
    user to have access to one of my agents. But that agent can only access a
    certain search API, on a certain port, at a single allocated time."

Guardrails were prompt-in / response-out only (UI-061), and nothing anywhere
enforced a port or a time window (UI-063). The second half of his example --
the port and the allocated time -- is already expressible as grant constraints
in :mod:`python.harness.access`. What was missing is the place that asks before
a connection is made.

**Default on + default-deny (Highs H7).** Fresh installs enable the network
guard with ``default: deny``. Loopback remains reachable so local inference
and the harness socket stay bootable on a CPU-only box. Settings → Guardrails
(or ``guardrails.set_network``) can disable the guard or switch to
default-allow for first-run / lab use. An unreadable rules row still leaves
the guard *off* so a broken DB cannot silently cut the machine off the
network — that is a bootability exception, not a security default.

**Migration:** installs that previously relied on implicit opt-in (enabled
false / default allow) keep whatever they saved under ``guardrails_config``.
Only the code defaults applied when no ``network`` key is present changed.
Disable explicitly if you need the old fail-open posture.

Three things can deny, in order:

1. **The USB environment preset**, which is meant to be isolated -- and was
   the one preset that read as a security control while changing nothing
   (UI-044).
2. **A deny rule**, which beats an allow rule, because a blocklist entry that
   an allowlist can override is not a blocklist.
3. **Default deny**, if the user has chosen it, unless something allows.

And two things can permit under default-deny: an allow rule, or an explicit
grant to the acting principal for that host -- which is where the port and
time-window constraints come from.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_RULES: dict[str, Any] = {
    "enabled": True,
    "default": "deny",
    "allow": [],
    "deny": [],
}

#: Hosts that are always reachable when the rules are on but do not name them.
#: A firewall that cuts the machine off from its own local model turns a
#: security setting into a broken app, and the traffic never leaves the box.
LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


@dataclass
class NetDecision:
    allowed: bool
    reason: str
    host: str = ""
    port: int | None = None
    matched: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed


def split_target(target: str, default_port: int | None = None) -> tuple[str, int | None]:
    """A URL or ``host:port`` into a host and a port."""
    text = str(target or "").strip()
    if not text:
        return "", None
    if "://" in text:
        parsed = urlparse(text)
        port = parsed.port
        if port is None:
            port = {"http": 80, "https": 443, "ftp": 21, "ssh": 22}.get(
                parsed.scheme.lower(), default_port)
        return (parsed.hostname or "").lower(), port
    host, _, port_text = text.rpartition(":")
    if host and port_text.isdigit():
        return host.lower(), int(port_text)
    return text.lower(), default_port


def _rule_matches(rule: dict[str, Any], host: str, port: int | None) -> bool:
    pattern = str(rule.get("host") or "*")
    if not fnmatch.fnmatch(host, pattern.lower()):
        return False
    ports = rule.get("ports")
    if ports:
        if port is None:
            # A rule naming ports cannot be satisfied by a connection whose
            # port is unknown; assuming it would defeat the rule.
            return False
        return int(port) in [int(p) for p in ports]
    return True


async def load_rules() -> dict[str, Any]:
    """Network rules from the guardrails row, defaulted and never raising."""
    from python.harness import db

    rules = dict(DEFAULT_RULES)
    try:
        row = await db.fetchrow("SELECT rules FROM guardrails_config WHERE id = 1")
        stored = row["rules"] if row else None
        if isinstance(stored, str):
            import json
            stored = json.loads(stored)
        network = (stored or {}).get("network") if isinstance(stored, dict) else None
        if isinstance(network, dict):
            rules.update(network)
    except Exception as exc:
        # Bootability exception: an unreadable rule row must not silently cut
        # the machine off the network. Disable the guard for this check only;
        # this is not the security default (DEFAULT_RULES stay on + deny).
        log.warning("network_guard: could not read the rules (%s); leaving the "
                    "network guard off.", exc)
        rules["enabled"] = False
    return rules


async def check(
    target: str,
    principal: Any = None,
    purpose: str = "",
    default_port: int | None = None,
) -> NetDecision:
    """
    Whether this outbound connection is permitted.

    *target* may be a URL or ``host:port``. *principal* is the acting
    :class:`~python.harness.access.Principal`; a grant to it for ``net:<host>``
    permits the connection under default-deny, carrying any port and hours
    constraints with it.
    """
    host, port = split_target(target, default_port)
    if not host:
        return NetDecision(True, "no host to check", host, port)

    # 1. the USB preset is meant to be isolated
    try:
        from python.harness import environment as env
        state = await env.get_state()
        if state.get("environment") == env.USB and host not in LOOPBACK:
            return NetDecision(
                False,
                f"the USB preset is selected, which permits no outbound network "
                f"access. {host} was not contacted.",
                host, port, matched="environment:usb",
            )
    except Exception as exc:
        log.debug("network_guard: environment unavailable (%s)", exc)

    rules = await load_rules()
    if not rules.get("enabled"):
        return NetDecision(True, "network rules are not enabled", host, port)

    # 2. deny beats allow
    for rule in rules.get("deny") or []:
        if _rule_matches(rule, host, port):
            return NetDecision(
                False,
                f"{host}{':' + str(port) if port else ''} matches a deny rule "
                f"({rule.get('host', '*')})",
                host, port, matched=f"deny:{rule.get('host', '*')}",
            )

    if host in LOOPBACK:
        return NetDecision(True, "loopback is always reachable", host, port)

    for rule in rules.get("allow") or []:
        if _rule_matches(rule, host, port):
            return NetDecision(
                True, f"matches allow rule {rule.get('host', '*')}",
                host, port, matched=f"allow:{rule.get('host', '*')}",
            )

    if str(rules.get("default", "allow")).lower() != "deny":
        return NetDecision(True, "no rule matched and the default is allow", host, port)

    # 3. default deny -- unless this principal was granted this host
    if principal is not None:
        try:
            from python.harness.access import check as access_check
            decision = await access_check(
                principal, f"net:{host}", "connect", {"host": host, "port": port},
            )
            if decision.allowed:
                return NetDecision(
                    True,
                    f"{principal} holds a grant for {host}",
                    host, port, matched=f"grant:{decision.grant_id}",
                    constraints=decision.constraints,
                )
            grant_note = f" {decision.reason}"
        except Exception as exc:
            grant_note = f" (grant lookup failed: {type(exc).__name__})"
    else:
        grant_note = " No principal was given, so no grant could apply."

    return NetDecision(
        False,
        f"{host}{':' + str(port) if port else ''} is not permitted: the default "
        f"is deny and nothing allows it.{grant_note}",
        host, port, matched="default:deny",
    )


class NetworkBlocked(PermissionError):
    """Raised when an outbound connection is refused."""


async def assert_may_reach(target: str, principal: Any = None,
                           purpose: str = "") -> NetDecision:
    """
    :func:`check`, but raising, for call sites that cannot carry a decision.

    Logs every refusal to the audit log: a connection that did not happen is
    exactly the kind of thing someone will later need to explain.
    """
    decision = await check(target, principal, purpose)
    if not decision.allowed:
        try:
            from python.harness import db
            await db.log_to_db(
                "WARNING",
                f"Network guard refused {decision.host}"
                f"{':' + str(decision.port) if decision.port else ''}"
                f"{' for ' + purpose if purpose else ''}: {decision.reason}",
                {"host": decision.host, "port": decision.port,
                 "matched": decision.matched, "purpose": purpose},
            )
        except Exception:
            pass
        raise NetworkBlocked(decision.reason)
    return decision
