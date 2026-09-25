"""
One grant model, so the three access systems can compose.

Three independent mechanisms existed -- guardrails on prompt text, nine
booleans on a contact, firewall clearance levels -- each with its own storage
and enforcement point, none able to reference another (UI-062). None expressed
the model Steven specified (UI-101, UI-118):

* *"permissions are by invite only ... I have to invite her to access. This
  does not mean I have to invite her to be a contact. Just to access a file"*
* *"If you give a contact premissions it superseeds other settings, unless a
  lockdown is initiated, then no access anywhere"*
* *"An agent, should be assigned just like a peer"* -- there is no
  "locally initiated" exemption; the agent is always the actor.
* *"that agent can only access a certain search API, on a certain port, at a
  single allocated time"* -- a grant carries constraints, not just a yes.

`AuthorizationGate` now has a ToolRegistry caller (UI-103 / H1); historically it had none, which is why the
`priority = 1` bug it carried was latent rather than active: nothing was being
wrongly authorised because nothing asked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from python.harness import access
from python.harness.access import AGENT, OWNER, PEER, Decision, Principal


def _future(hours=1):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture
def no_lockdown(monkeypatch):
    from python.harness import environment as env

    async def state():
        return {"lockdown": False, "environment": "local"}
    monkeypatch.setattr(env, "get_state", state)


@pytest.fixture
def grants(monkeypatch):
    """An in-memory grant table, so the model is tested rather than Postgres."""
    from python.harness import db
    rows: list[dict] = []

    async def fetch(sql, *args):
        if "access_grants" not in sql:
            return []
        kind, pid, resource, permission = args[0], args[1], args[2], args[3]
        return [
            r for r in rows
            if r["principal_kind"] == kind and r["principal_id"] == pid
            and r["resource"] == resource
            and r["permission"] in (permission, "all")
        ]

    monkeypatch.setattr(db, "fetch", fetch)
    return rows


def _add(rows, kind, pid, resource, permission="read", constraints=None):
    rows.append({
        "id": f"g{len(rows)}", "principal_kind": kind, "principal_id": pid,
        "resource": resource, "permission": permission,
        "constraints": constraints or {},
    })


# -- principals ---------------------------------------------------------------

def test_an_unknown_principal_kind_is_rejected():
    with pytest.raises(ValueError, match="Unknown principal kind"):
        Principal("robot", "x")


def test_a_decision_is_truthy_or_falsy():
    assert bool(Decision(True, "")) is True
    assert bool(Decision(False, "")) is False


# -- default deny -------------------------------------------------------------

@pytest.mark.asyncio
async def test_nothing_is_permitted_without_a_grant(no_lockdown, grants):
    decision = await access.check(Principal(PEER, "alice"), "notes.md", "read")

    assert not decision.allowed
    assert "by invitation" in decision.reason


@pytest.mark.asyncio
async def test_being_a_contact_grants_nothing(no_lockdown, grants):
    """Identity and access are separate: "This does not mean I have to invite
    her to be a contact. Just to access a file"."""
    _add(grants, PEER, "alice", "notes.md")

    assert await access.check(Principal(PEER, "alice"), "notes.md", "read")
    assert not await access.check(Principal(PEER, "alice"), "other.md", "read")
    assert not await access.check(Principal(PEER, "bob"), "notes.md", "read")


@pytest.mark.asyncio
async def test_a_grant_is_per_permission(no_lockdown, grants):
    _add(grants, PEER, "alice", "notes.md", "read")

    assert await access.check(Principal(PEER, "alice"), "notes.md", "read")
    assert not await access.check(Principal(PEER, "alice"), "notes.md", "write")


@pytest.mark.asyncio
async def test_an_all_grant_covers_every_permission(no_lockdown, grants):
    _add(grants, PEER, "alice", "notes.md", "all")

    assert await access.check(Principal(PEER, "alice"), "notes.md", "write")


@pytest.mark.asyncio
async def test_a_principal_with_no_id_cannot_match_a_grant(no_lockdown, grants):
    decision = await access.check(Principal(PEER, ""), "notes.md", "read")
    assert not decision.allowed


# -- the owner and the agent --------------------------------------------------

@pytest.mark.asyncio
async def test_the_owner_needs_no_grant(no_lockdown, grants):
    assert await access.check(Principal(OWNER), "anything", "write")


@pytest.mark.asyncio
async def test_the_agent_is_a_principal_like_any_peer(no_lockdown, grants):
    """Q-63: "An agent, should be assigned just like a peer." There is no
    locally-initiated exemption."""
    agent = Principal(AGENT, "researcher")
    assert not await access.check(agent, "search-api", "read")

    _add(grants, AGENT, "researcher", "search-api")
    assert await access.check(agent, "search-api", "read")


# -- constraints: Steven's own example ----------------------------------------

@pytest.mark.asyncio
async def test_a_grant_can_be_limited_to_a_port(no_lockdown, grants):
    _add(grants, AGENT, "r", "search-api", constraints={"ports": [443]})
    agent = Principal(AGENT, "r")

    assert await access.check(agent, "search-api", "read", {"port": 443})
    assert not await access.check(agent, "search-api", "read", {"port": 8080})


@pytest.mark.asyncio
async def test_a_port_limited_grant_refuses_when_no_port_is_given(no_lockdown, grants):
    """Assuming the port would defeat the constraint."""
    _add(grants, AGENT, "r", "search-api", constraints={"ports": [443]})

    decision = await access.check(Principal(AGENT, "r"), "search-api", "read")
    assert not decision.allowed
    assert "no port was given" in decision.reason


@pytest.mark.asyncio
async def test_an_expired_grant_is_refused(no_lockdown, grants):
    _add(grants, PEER, "alice", "notes.md", constraints={"expires_at": _past()})

    decision = await access.check(Principal(PEER, "alice"), "notes.md", "read")
    assert not decision.allowed
    assert "expired" in decision.reason


@pytest.mark.asyncio
async def test_a_grant_that_has_not_started_is_refused(no_lockdown, grants):
    _add(grants, PEER, "alice", "notes.md", constraints={"not_before": _future()})

    assert not await access.check(Principal(PEER, "alice"), "notes.md", "read")


@pytest.mark.asyncio
async def test_a_host_limited_grant(no_lockdown, grants):
    _add(grants, AGENT, "r", "api", constraints={"hosts": ["api.example.com"]})
    agent = Principal(AGENT, "r")

    assert await access.check(agent, "api", "read", {"host": "API.example.com"})
    assert not await access.check(agent, "api", "read", {"host": "evil.example.com"})


def test_an_unrecognised_constraint_denies():
    """A constraint nobody evaluates is worse than no constraint: it reads as a
    restriction that is not there."""
    ok, why = access._check_constraints({"moon_phase": "full"})
    assert not ok
    assert "moon_phase" in why


def test_structure_and_satisfaction_are_separate_questions():
    """Conflating them was a real bug: grant() validated by running the
    evaluator against a dummy context, so a legitimate ports constraint refused
    its own grant because port 0 was not in the list."""
    ok, _ = access._validate_constraints({"ports": [443]})
    assert ok

    satisfied, _ = access._check_constraints({"ports": [443]}, {"port": 8080})
    assert not satisfied


@pytest.mark.parametrize("bad", [
    {"expires_at": "not-a-date"},
    {"hours": "all day"},
    {"ports": "443"},
    {"unknown_key": 1},
])
def test_malformed_constraints_are_rejected_at_grant_time(bad):
    ok, why = access._validate_constraints(bad)
    assert not ok
    assert why


def test_a_window_may_cross_midnight():
    window = access._parse_window("22:00-02:00")
    assert window is not None


# -- lockdown -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_lockdown_overrides_every_grant(monkeypatch, grants):
    """"If you give a contact premissions it superseeds other settings, unless
    a lockdown is initiated, then no access anywhere"."""
    from python.harness import environment as env

    async def locked():
        return {"lockdown": True, "environment": "local"}
    monkeypatch.setattr(env, "get_state", locked)

    _add(grants, PEER, "alice", "notes.md")
    decision = await access.check(Principal(PEER, "alice"), "notes.md", "read")

    assert not decision.allowed
    assert "lockdown" in decision.reason


@pytest.mark.asyncio
async def test_lockdown_does_not_lock_out_the_owner(monkeypatch, grants):
    from python.harness import environment as env

    async def locked():
        return {"lockdown": True, "environment": "local"}
    monkeypatch.setattr(env, "get_state", locked)

    assert await access.check(Principal(OWNER), "notes.md", "write")


@pytest.mark.asyncio
async def test_an_unknown_lockdown_state_denies(monkeypatch, grants):
    """Not knowing whether lockdown is on is not permission to proceed."""
    from python.harness import environment as env

    async def broken():
        raise RuntimeError("no database")
    monkeypatch.setattr(env, "get_state", broken)

    _add(grants, PEER, "alice", "notes.md")
    decision = await access.check(Principal(PEER, "alice"), "notes.md", "read")

    assert not decision.allowed
    assert "lockdown" in decision.reason


@pytest.mark.asyncio
async def test_an_unreadable_grant_table_denies(no_lockdown, monkeypatch):
    from python.harness import db

    async def boom(*a, **k):
        raise RuntimeError("table gone")
    monkeypatch.setattr(db, "fetch", boom)

    decision = await access.check(Principal(PEER, "alice"), "notes.md", "read")
    assert not decision.allowed


# -- the gate -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_gate_now_asks(no_lockdown, grants):
    """It had no callers, so it authorised nothing and refused nothing."""
    from sdk.agent.gates import AuthorizationGate

    gate = AuthorizationGate()
    allowed = await gate.request_authorization(
        "read", "search-api", "high", principal=Principal(AGENT, "nobody"))

    assert allowed is False
    assert gate.last_reason


@pytest.mark.asyncio
async def test_the_gate_allows_a_granted_principal(no_lockdown, grants):
    from sdk.agent.gates import AuthorizationGate

    _add(grants, AGENT, "r", "search-api", "read")
    gate = AuthorizationGate()

    assert await gate.request_authorization(
        "read", "search-api", "high", principal=Principal(AGENT, "r"))


def test_the_priority_one_authorization_bug_is_gone():
    """Any credential that happened to sort first authorised any action."""
    import inspect
    from sdk.agent.gates import AuthorizationGate

    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(AuthorizationGate.request_authorization)))
    # Read the code, not the prose: the docstring names both in order to record
    # that they were removed. This is the third time this session that a
    # substring check has been tripped by an explanation of what it checks for.
    body = tree.body[0].body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)):
        body = body[1:]
    code = " ".join(ast.dump(node) for node in body)

    assert "priority = 1" not in code
    assert "auth_credentials" not in code


def test_the_emergency_halt_still_wins():
    import inspect
    from sdk.agent.gates import AuthorizationGate

    src = inspect.getsource(AuthorizationGate.request_authorization)
    assert "RevocationHalt.is_halted()" in src
    assert src.index("is_halted") < src.index("_approved_actions")


def test_the_rpcs_exist():
    from python.harness.server import _HANDLERS
    for method in ("access.grant", "access.revoke", "access.list", "access.check"):
        assert method in _HANDLERS
