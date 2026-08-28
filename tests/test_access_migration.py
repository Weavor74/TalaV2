"""
The existing access systems now compose with the grant model.

UI-101 listed several findings as "partial views of the same missing thing":
per-section profile privacy (UI-058), per-skill sharing scope (UI-040) and
firewall clearance on peer node reads (UI-082). Each had its own rule and none
could be superseded by an invitation.

Steven's ordering is what is implemented:

    "If you give a contact premissions it superseeds other settings, unless a
    lockdown is initiated, then no access anywhere"

Composed rather than replaced. Replacing each subsystem's rule with grants
would have meant every existing setting silently becoming "deny" the moment
this shipped; composition means **nothing changes for a user who never opens
the access panel**, which the first test here pins.
"""
from __future__ import annotations

import pytest

from python.harness import access
from python.harness.access import AGENT, OWNER, PEER, Principal


@pytest.fixture
def no_lockdown(monkeypatch):
    from python.harness import environment as env

    async def state():
        return {"lockdown": False, "environment": "local"}
    monkeypatch.setattr(env, "get_state", state)


@pytest.fixture
def grants(monkeypatch):
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


# -- the composition rule itself ----------------------------------------------

@pytest.mark.asyncio
async def test_with_no_grant_the_subsystem_decides(no_lockdown, grants):
    """Nothing changes for a user who never opens the access panel."""
    allowed = await access.resolve(
        Principal(PEER, "alice"), "profile:skills", "read",
        fallback_allowed=True, fallback_reason="visibility is public")
    assert allowed.allowed
    assert "visibility" in allowed.reason

    denied = await access.resolve(
        Principal(PEER, "alice"), "profile:connections", "read",
        fallback_allowed=False, fallback_reason="visibility is connections-only")
    assert not denied.allowed


@pytest.mark.asyncio
async def test_a_grant_supersedes_the_subsystem(no_lockdown, grants):
    _add(grants, PEER, "alice", "profile:connections")

    decision = await access.resolve(
        Principal(PEER, "alice"), "profile:connections", "read",
        fallback_allowed=False, fallback_reason="visibility is connections-only")

    assert decision.allowed
    assert "invitation" in decision.reason


@pytest.mark.asyncio
async def test_lockdown_overrides_the_grant_and_the_subsystem(monkeypatch, grants):
    from python.harness import environment as env

    async def locked():
        return {"lockdown": True, "environment": "local"}
    monkeypatch.setattr(env, "get_state", locked)

    _add(grants, PEER, "alice", "profile:skills")

    decision = await access.resolve(
        Principal(PEER, "alice"), "profile:skills", "read",
        fallback_allowed=True, fallback_reason="visibility is public")

    assert not decision.allowed
    assert "lockdown" in decision.reason


@pytest.mark.asyncio
async def test_an_unreadable_grant_table_falls_through_rather_than_denying(
        no_lockdown, monkeypatch):
    """A grant can only widen access -- it turns a subsystem's deny into an
    allow and never the reverse -- so failing to read the table can only make
    the outcome more restrictive. Hard-denying would take away a user's own
    "public" section because of a database hiccup: a worse failure, not a
    safer one."""
    from python.harness import db

    async def boom(*a, **k):
        raise RuntimeError("table gone")
    monkeypatch.setattr(db, "fetch", boom)

    decision = await access.resolve(
        Principal(PEER, "alice"), "profile:skills", "read",
        fallback_allowed=True, fallback_reason="visibility is public")
    assert decision.allowed


@pytest.mark.asyncio
async def test_an_unknown_lockdown_state_still_denies(monkeypatch, grants):
    """Lockdown exists to remove access, so not knowing must deny -- the
    opposite direction from a failed grant lookup, deliberately."""
    from python.harness import environment as env

    async def broken():
        raise RuntimeError("no database")
    monkeypatch.setattr(env, "get_state", broken)

    decision = await access.resolve(
        Principal(PEER, "alice"), "profile:skills", "read",
        fallback_allowed=True, fallback_reason="visibility is public")
    assert not decision.allowed


# -- UI-058: profile sections -------------------------------------------------

@pytest.fixture
def profile(monkeypatch):
    from python.harness import user_profile as up

    async def load():
        return {
            "skills": [{"name": "AI"}],
            "connections": [{"name": "Someone"}],
            "privacy": {"skills": "public", "connections": "connections"},
        }
    monkeypatch.setattr(up, "load_config", load)
    return up


@pytest.mark.asyncio
async def test_a_public_section_still_reads_without_any_grant(
        no_lockdown, grants, profile):
    result = await profile.lookup("skills")
    assert result["allowed"] is True


@pytest.mark.asyncio
async def test_a_withheld_section_can_be_opened_by_invitation(
        no_lockdown, grants, profile):
    """The point of the model: the owner invites without having to change a
    global visibility setting for everyone."""
    assert not (await profile.lookup("connections"))["allowed"]

    _add(grants, AGENT, "researcher", "profile:connections")
    opened = await profile.lookup("connections", actor="researcher")

    assert opened["allowed"] is True
    assert opened["data"][0]["name"] == "Someone"


@pytest.mark.asyncio
async def test_a_grant_to_one_agent_does_not_open_it_to_another(
        no_lockdown, grants, profile):
    """An agent is a principal assigned like a peer, not a category."""
    _add(grants, AGENT, "researcher", "profile:connections")

    assert (await profile.lookup("connections", actor="researcher"))["allowed"]
    assert not (await profile.lookup("connections", actor="other"))["allowed"]


@pytest.mark.asyncio
async def test_pii_is_still_refused_even_with_a_grant(no_lockdown, grants, profile):
    """The vault refusal is not a visibility setting and is not superseded."""
    _add(grants, AGENT, "default", "profile:pii_vault")
    result = await profile.lookup("pii_vault")

    assert result["allowed"] is False
    assert "personally identifying" in result["reason"]


# -- the call sites are wired -------------------------------------------------

def test_the_skill_scope_is_composed():
    import inspect
    from python.harness import server

    src = inspect.getsource(server.handle_p2p_marketplace_search)
    assert "resolve(" in src
    assert "skill:" in src
    # the subsystem rule is kept, not deleted
    assert "_scope_permits" in src


def test_the_peer_node_check_is_composed():
    import inspect
    from python.harness import server

    src = inspect.getsource(server._peer_can_resolve_node_reference)
    assert "access_check" in src
    assert "node:" in src
    # and the clearance machinery survives for peers who were not invited
    assert "clearance_level" in src


def test_lockdown_short_circuits_the_peer_path():
    import inspect
    from python.harness import server

    src = inspect.getsource(server._peer_can_resolve_node_reference)
    assert "lockdown_engaged" in src
    assert src.index("lockdown") < src.index("contact_nodes_permission_disabled")


def test_the_vault_accepts_an_access_grant():
    import inspect
    from python.harness import vault_nodes

    src = inspect.getsource(vault_nodes.assert_may_read)
    assert "access_check" in src
    assert "vault:" in src
    # the document-local grant still works, so vaults written before the model
    # existed are not broken
    assert "get_grants(doc_id)" in src
