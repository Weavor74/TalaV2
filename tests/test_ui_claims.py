"""
The interface says what is true.

Twelve findings of one shape: a panel asserting behaviour the code does not
deliver. A wrong claim in a settings panel is worse than a missing one, because
somebody acts on it — UI-053 is the clearest case, asking a user to enter paths
to their personal records on the promise that the agent will use them.

These tests pin the claims to the code that has to back them. They are
deliberately about *specific sentences*: a claim is a fact about the system, and
a fact that nothing checks drifts back into fiction the next time the code moves
underneath it.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def markup() -> str:
    return (ROOT / "renderer" / "index.html").read_text(encoding="utf-8")


def renderer() -> str:
    return (ROOT / "renderer" / "renderer.js").read_text(encoding="utf-8")


# -- UI-053: the vault is not consulted, and no longer says it is -------------

def test_the_pii_panel_does_not_promise_the_agent_reads_it():
    """The values are sealed and ``user_profile_lookup`` refuses this section at
    every visibility setting, so the promise was not merely unimplemented — the
    system is now built to break it."""
    text = markup()

    assert "The AI agent consults these mappings" not in text
    assert "no agent can read them" in text


def test_the_lookup_really_does_refuse_that_section():
    """The claim is only worth making because this is true."""
    from python.harness import user_profile

    source = inspect.getsource(user_profile)
    assert "pii_vault" in source, "the section the panel names is not handled"


def test_the_registry_field_is_sealed():
    from python.harness import node_vault

    sealed = {field for fields in node_vault.VAULT_FIELDS.values()
              for field in fields}
    assert "pii_registries" in sealed


# -- UI-081: credentials are ordered for what actually consumes them ----------

def test_the_shell_is_not_claimed_to_try_credentials_in_order():
    """`shell.py` references credentials only to strip them out of the
    subprocess environment — the opposite of attempting them."""
    text = markup()

    assert "Authenticated shell tasks attempt credentials" not in text
    assert "strip credentials out of the subprocess environment" in text


def test_the_shell_still_only_strips_them():
    from sdk.agent import shell

    source = inspect.getsource(shell)
    assert "credential" in source.lower(), "the claim describes code that moved"


# -- UI-089: PostgreSQL is not a vector database -----------------------------

def test_postgres_is_not_labelled_a_vector_database():
    text = markup()

    assert "PostgreSQL Vector DB" not in text
    assert "full-text" in text


def test_there_is_no_pgvector_to_justify_the_old_label():
    from python.harness import db

    statements = "\n".join(db._DDL_STATEMENTS).lower()
    assert "extension vector" not in statements
    assert "tsvector" in statements, "the replacement label names what is there"


# -- UI-093: the memory capability describes the memory that exists ----------

def test_mem0_is_not_claimed_as_live_memory():
    """It is installed now, which it was not when this was filed, but nothing
    constructs the adapter in any live path and the adapter blocks writes by
    design. Installed is not the same as used."""
    text = markup()

    assert "Dual Memory: mem0" not in text
    assert "ChromaDB retrieval" in text


def test_nothing_constructs_the_mem0_adapter():
    """If this ever becomes false, the capability claim can come back."""
    offenders = []
    for path in list((ROOT / "python").rglob("*.py")) + list((ROOT / "sdk").rglob("*.py")):
        if path.name == "mem0_adapter.py":
            continue
        if "Mem0ReadOnlyAdapter(" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


# -- UI-090: a hand-maintained sketch says so ---------------------------------

def test_the_diagram_does_not_claim_to_be_a_data_flow_map():
    """Hardcoded markup nothing derives from the code, with undirected edges
    and no labels — it cannot fall out of date loudly, only quietly."""
    text = markup()

    assert "Component Graph & Data Flow Map" not in text
    assert "Hand-maintained, not derived from the code" in text


# -- UI-092: the About version can be reached --------------------------------

def test_the_about_version_is_not_hardcoded():
    text = markup()

    assert "v0.0.0" not in text
    assert 'id="about-version"' in text


def test_the_updater_reaches_it():
    assert "#about-version" in renderer()


# -- UI-066: the panel checks the dependency that matters --------------------

def test_the_package_panel_checks_pyswisseph_not_ephem():
    """`ephem` is neither declared nor imported anywhere; the astrology engine
    imports `swisseph`, from `pyswisseph`. The panel raised a red alarm for the
    wrong library while ignoring the one whose absence silently biases emotion
    labels."""
    from python.harness import system_info

    source = inspect.getsource(system_info.collect_system_info)
    assert '"pyswisseph"' in source
    assert '_pkg_version("ephem")' not in source


def test_ephem_really_is_unused():
    """Checked as imports rather than as text: "import ephem" is a substring of
    "import ephemeris_source", and a substring test would report the astrology
    module as the thing it is meant to distinguish from."""
    offenders = []
    for path in list((ROOT / "python").rglob("*.py")) + list((ROOT / "sdk").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            if "ephem" in names:
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


# -- UI-087: the audit tile counts records, not word matches -----------------

def test_the_audit_tile_is_not_a_substring_grep():
    """It counted `agent_log` rows whose free text happened to contain
    "guardrail". No guardrail ever wrote there, so it read 0 regardless of how
    much validation ran."""
    from python.harness import server

    source = inspect.getsource(server)
    assert "message LIKE '%Guardrail%'" not in source
    assert "'guardrail-violation'" in source
    # Read through both shapes: rows written before log_to_db stopped
    # double-encoding hold a JSON *string* in a jsonb column, where ->> returns
    # NULL. Counting only the recent half would be a quieter version of the
    # same bug.
    assert "jsonb_typeof(trace) = 'string'" in source


def test_guardrail_violations_reach_the_audit_log():
    """The tile can only be right if something writes what it counts."""
    from python.harness import guardrails_manager

    tree = ast.parse(inspect.getsource(guardrails_manager._log_violation_to_ledger))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "log_to_db" in called


def test_the_audit_trace_is_stored_as_an_object():
    """The connection codec already json.dumps every JSONB value, so encoding
    at the call site stored a JSON *string*: jsonb_typeof(trace) came back
    'string' and every trace->>'key' silently returned NULL. The tile would
    have read 0 for exactly the reason it was being rewritten to stop reading
    0."""
    from python.harness import db

    source = inspect.getsource(db.log_to_db)
    assert "json.dumps(trace" not in source
    assert "trace_payload" in source


def test_the_marker_is_structured_not_prose():
    """Counting a substring of a message is how the tile came to be wrong; a
    message somebody rewords must not change a number."""
    from python.harness import guardrails_manager

    source = inspect.getsource(guardrails_manager._log_violation_to_ledger)
    assert '"event_type": "guardrail-violation"' in source


# -- UI-017: the badge reports the browser ------------------------------------

def test_the_ready_badge_is_wired():
    """Styled markup with no JavaScript anywhere in renderer/ — it read READY
    while a page was loading and while a page had failed. A status indicator
    that cannot indicate is worse than none, because it is believed."""
    text = renderer()

    assert "#browser-status" in text
    for event in ("did-start-loading", "did-stop-loading", "did-fail-load"):
        assert event in text, f"{event} is not handled"


def test_an_aborted_navigation_is_not_shown_as_a_failure():
    """ERR_ABORTED fires on ordinary navigation away from a page."""
    assert "errorCode === -3" in renderer()


# -- UI-111 / UI-113: optional capabilities described as such ----------------

def test_storage_is_described_as_optional_and_general_purpose():
    text = markup()

    assert "Optional, general-purpose storage" in text
    assert "nothing here is required" in text


def test_the_background_sync_claim_is_withdrawn():
    """`sync_manager` records session events for memory.py. Nothing walks
    storage providers on a timer."""
    text = markup()

    assert "propagates changes to active storage providers in the background" not in text
    assert "On request, not in the background" in text


def test_nothing_actually_propagates_to_storage_in_the_background():
    from python.harness import sync_manager

    source = inspect.getsource(sync_manager)
    assert "storage_provider" not in source, "the withdrawn claim became true"


def test_the_git_panel_says_whose_version_control_it_is():
    """Steven: 'The GIT panel should stay. But Tala does not use GIT for
    herself.' Her updates come from the website."""
    text = markup()

    assert "Tala does not use git for herself" in text
    assert "Your version control, for your own code and content" in text
