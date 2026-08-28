"""
A peer is a peer.

Two mechanisms reached another install's HTML-NN nodes, and only one of them
worked.

**Contacts + p2p** is built end to end: a username, a wallet address, an
endpoint and a shared secret; HMAC-signed packets; a permission check on both
sides (``_peer_can_resolve_node_reference`` requires the contact's ``nodes``
permission *and* a firewall record with clearance); and
``p2p.resolve_node_reference`` called from a live flow.

**Remote HTML-NN Instances** stored a Common Name, a URL and a bearer token,
with no identity and no permission model in either direction — and nothing ever
dereferenced a row. It was listed, saved, labelled and *counted*, and that was
all (UI-075). Meanwhile the panel claimed remote instances were "queried first
during node retrieval" and that the system "automatically falls back to
secondary remote servers".

Steven's call: delete it. Contacts already works whether the install on the
other end is yours or a friend's, so two paths to the same place — one of them
weaker and unused — is one too many (UI-076).

These tests exist because a removal is only finished when nothing references
the thing. Half-removed subsystems are how this codebase accumulated the dead
ones in the first place.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def source(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# -- nothing references it ----------------------------------------------------

def test_no_python_reads_the_table():
    offenders = []
    for path in list((ROOT / "python").rglob("*.py")) + list((ROOT / "sdk").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if "runtime_instances" not in line:
                continue
            stripped = line.strip()
            # the guarded DROP and its explanation are the removal itself
            # The guarded DROP that performs the removal, and the comment
            # explaining it, are not references to a live table.
            if stripped.startswith("#") or stripped.startswith("--"):
                continue
            if any(marker in line for marker in
                   ("DROP TABLE", "information_schema", "table_name =",
                    "NOT EXISTS (SELECT 1 FROM runtime_instances)")):
                continue
            offenders.append(f"{path.relative_to(ROOT)}: {stripped[:70]}")
    assert not offenders, offenders


def test_the_rpcs_are_gone():
    from python.harness.server import _HANDLERS

    assert "runtime.get_instances" not in _HANDLERS
    assert "runtime.save_instances" not in _HANDLERS


def test_the_panel_is_gone():
    markup = source("renderer/index.html")

    assert 'id="settings-panel-runtime"' not in markup
    assert 'data-panel="runtime"' not in markup
    assert 'id="modal-add-runtime-instance"' not in markup


def test_the_renderer_object_is_gone():
    renderer = source("renderer/renderer.js")

    assert "RuntimeInstanceManager" not in renderer
    assert "rt-inst-" not in renderer


def test_every_settings_tab_still_has_its_panel():
    """Removing a panel and leaving its tab produces a button that opens
    nothing — the shape of UI-117 and UI-033."""
    markup = source("renderer/index.html")

    tabs = set(re.findall(r'class="settings-sub-tab[^"]*"\s+data-panel="([^"]+)"', markup))
    panels = set(re.findall(r'class="settings-panel"\s+id="settings-panel-([^"]+)"', markup))

    assert tabs == panels, {"tab without a panel": sorted(tabs - panels),
                            "panel without a tab": sorted(panels - tabs)}


# -- the schema, and what the removal must not break --------------------------

def test_the_table_is_dropped_only_when_empty():
    """Deleting somebody's configuration to tidy up a schema is not a trade
    this project makes."""
    from python.harness import db

    statements = "\n".join(db._DDL_STATEMENTS)
    drop = [s for s in db._DDL_STATEMENTS if "DROP TABLE runtime_instances" in s]

    assert len(drop) == 1
    assert "NOT EXISTS (SELECT 1 FROM runtime_instances)" in drop[0]
    assert "CREATE TABLE IF NOT EXISTS runtime_instances" not in statements


def test_nothing_alters_the_dropped_table():
    """The later ALTERs run sequentially in one connection, so an ALTER on a
    table that no longer exists would raise and silently skip every migration
    after it."""
    from python.harness import db
    import inspect

    source_text = inspect.getsource(db)
    assert "ALTER TABLE runtime_instances" not in source_text


@pytest.mark.asyncio
async def test_the_migrations_after_it_still_applied():
    """The failure mode this guards: a fresh install losing auth_credentials.locked,
    agent_log.tx_id and the credential_id columns because one earlier ALTER
    raised."""
    from python.harness import db

    await db.init_pool()
    try:
        for table, column in (("auth_credentials", "locked"),
                              ("agent_log", "tx_id"),
                              ("git_providers", "credential_id"),
                              ("search_providers", "credential_id")):
            present = await db.fetchval(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = $2", table, column)
            assert present, f"{table}.{column} is missing"
    finally:
        await db.close_pool()


# -- what replaced it is real -------------------------------------------------

def test_the_peer_path_is_permission_gated():
    """The reason one mechanism was kept and the other deleted."""
    import inspect

    from python.harness import server

    gate = inspect.getsource(server._peer_can_resolve_node_reference)
    assert "permission" in gate.lower() or "firewall" in gate.lower()


def test_the_peer_path_is_called_from_a_live_flow():
    """Not another built-but-never-invoked subsystem."""
    server_source = source("python/harness/server.py")

    callers = server_source.count("_peer_can_resolve_node_reference")
    assert callers >= 3, "definition plus at least two call sites expected"
    assert 'p2p.resolve_node_reference' in server_source


def test_readiness_no_longer_counts_rows_nothing_reads():
    """`auth_ready` was satisfied by a row in a table nothing dereferenced."""
    import inspect

    from python.harness import server

    source_text = inspect.getsource(server)
    assert "authenticated_remote_instances" not in source_text
