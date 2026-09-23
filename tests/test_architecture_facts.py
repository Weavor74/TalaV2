"""
The ratchet that keeps Tala's self-description honest.

Tala reads her own documentation as context when reasoning about herself, so
stale architecture documents are not untidiness: they make her diagnose bugs
that were fixed, misjudge what she can execute, and plan repairs against a
shape the system no longer has.

The self-healing that was supposed to prevent this does not run. Documentation
regenerates when somebody asks for it in chat, when an RPC is called, or when
an autonomous goal completes -- there is no git hook, no scheduled job, and
nothing that watches code or documents change. Verified at the time of
writing: `.git/hooks` holds only samples and the heartbeat jobs table does not
exist in this database.

A daemon nobody notices stopping would be a poor answer to that anyway. This
file is the answer instead: the contract's load-bearing facts are DERIVED from
the code, and these tests fail when the published node disagrees with what the
code now says. Drift stops being silent, which is the only property that
actually matters.

The failure that motivates all of it: the contract hardcoded
``executable_node_types: ["workflow", "flow"]``. Function, decision and service
nodes became executable and the contract went on saying otherwise -- so the one
document a model is told to read *first* answered wrong about what it could
run, confidently and for weeks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from python.harness import architecture_facts as facts

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_NODE = ROOT / "nodes" / "system-architecture-contract.html"

REGENERATE = "Regenerate it: python scripts/generate_architecture_contract.py"


def _published_contract() -> dict:
    """The contract as it currently stands on disk, or a skip."""
    if not CONTRACT_NODE.exists():
        pytest.skip(f"no contract node published yet. {REGENERATE}")
    html = CONTRACT_NODE.read_text(encoding="utf-8", errors="replace")
    blocks = re.findall(
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    for block in blocks:
        try:
            data = json.loads(block)
        except Exception:
            continue
        if isinstance(data, dict) and "architecture_contract" in data:
            return data["architecture_contract"]
        if isinstance(data, dict) and "state_data" in data:
            inner = data["state_data"]
            if isinstance(inner, dict) and "architecture_contract" in inner:
                return inner["architecture_contract"]
    pytest.skip(f"the contract node carries no readable contract. {REGENERATE}")


# ---------------------------------------------------------------------------
# The facts must be derived, not restated
# ---------------------------------------------------------------------------


def test_execution_gates_come_from_the_code():
    from sdk.agent import node_execution as ne

    gates = facts.execution_gates()
    assert gates["available"] is True, gates.get("reason")
    assert set(gates["executable_node_types"]) == set(ne.EXECUTABLE_TYPES)
    assert set(gates["code_bearing_node_types"]) == set(ne.CODE_BEARING_TYPES)
    assert gates["min_execution_authority"] == ne.MIN_EXECUTION_AUTHORITY


def test_the_published_contract_still_matches_the_code():
    """
    The drift test, and the reason this file exists.

    A contract that has fallen behind the code is worse than none: it is read
    first, and believed.
    """
    from sdk.agent import node_execution as ne

    published = _published_contract()
    capabilities = published.get("capabilities", {})
    declared = capabilities.get("executable_node_types")

    assert declared is not None, f"the contract does not say what can run. {REGENERATE}"
    assert set(declared) == set(ne.EXECUTABLE_TYPES), (
        f"the contract says {sorted(declared)} can run; the code says "
        f"{sorted(ne.EXECUTABLE_TYPES)}. {REGENERATE}"
    )
    assert capabilities.get("min_execution_authority") == ne.MIN_EXECUTION_AUTHORITY, (
        f"the contract's execution-authority floor is stale. {REGENERATE}"
    )


def test_the_published_contract_carries_the_operational_sections():
    published = _published_contract()
    for section in ("data_ownership", "security_model", "execution_paths",
                    "capability_maturity", "verification"):
        assert section in published, (
            f"the published contract predates the {section!r} section. {REGENERATE}"
        )


# ---------------------------------------------------------------------------
# Every claim names evidence, and the evidence is still there
# ---------------------------------------------------------------------------


def test_every_maturity_claim_has_its_evidence():
    """
    A claim whose evidence has been deleted or renamed must not keep being
    made. This is what stops the maturity table becoming the historical gap
    list it replaced.
    """
    unsupported = [
        (c["capability"], c["evidence_missing"])
        for c in facts.capability_maturity()
        if c.get("evidence_missing")
    ]
    assert not unsupported, (
        "maturity claims whose evidence no longer exists: "
        + "; ".join(f"{cap} -> {missing}" for cap, missing in unsupported)
    )


def test_claims_above_implemented_actually_cite_something():
    """
    'tested' and 'live-verified' are claims about evidence. Making one without
    naming any is how everything drifted to sounding finished.
    """
    for claim in facts.capability_maturity():
        if claim["maturity"] in ("tested", "live-verified"):
            assert claim.get("evidence_present"), (
                f"{claim['capability']!r} claims {claim['maturity']} and cites nothing"
            )


def test_maturity_levels_are_from_the_defined_set():
    for claim in facts.capability_maturity():
        assert claim["maturity"] in facts.MATURITY_LEVELS, claim


def test_every_execution_path_still_has_code_behind_it():
    """
    A route described here that no longer has an entry point is a route a
    model will plan around and never reach.
    """
    missing = [(p["path"], p["entry"]) for p in facts.execution_paths()
               if not p["entry_exists"]]
    assert not missing, f"execution paths whose entry point is gone: {missing}"


def test_the_symbol_check_can_actually_fail():
    """
    A verifier that always passes verifies nothing. This pins the check
    itself, which was silently broken once: a stray control character in the
    pattern made every symbol look absent.
    """
    assert facts._exists("python/harness/server.py")
    assert facts._exists("python/harness/server.py::handle_relay_deliver")
    assert not facts._exists("python/harness/server.py::not_a_real_function")
    assert not facts._exists("python/harness/no_such_module.py")


# ---------------------------------------------------------------------------
# Security description must track the code, not the intention
# ---------------------------------------------------------------------------


def test_the_security_description_reflects_the_real_preload():
    """
    Whether an unlisted IPC channel is refused or forwarded to the harness is
    the difference between a permission boundary and a transport choice.
    preload.js must fail closed (reject unknown channels); the architecture
    contract must describe the same behaviour as the live guard.
    """
    preload = (ROOT / "electron" / "preload.js").read_text(encoding="utf-8", errors="replace")
    guard = re.search(
        r"if\s*\(!ALLOWED_CHANNELS\.has\(channel\)\)\s*\{(.*?)\n\s{2}\}",
        preload, re.DOTALL,
    )
    assert guard, "the preload allowlist guard has moved; the contract's reading of it is stale"

    body = guard.group(1)
    forwards = (
        "ipcRenderer.invoke('python:rpc'" in body
        or 'ipcRenderer.invoke("python:rpc"' in body
    )
    described = facts.security_model()["ipc_surface"]["unlisted_channels_forwarded_to_rpc"]
    assert described == forwards, (
        "the contract describes the IPC surface as "
        f"{'forwarding' if described else 'refusing'} unlisted channels; the code "
        f"{'forwards' if forwards else 'refuses'} them"
    )


def test_electron_isolation_flags_are_read_not_asserted():
    flags = facts.security_model()["renderer_isolation"]["electron_flags"]
    assert flags, "no Electron flags were read; the contract would be asserting them"
    assert set(flags) == {"contextIsolation", "sandbox", "nodeIntegration", "webSecurity"}
    for name, value in flags.items():
        assert value is not None, f"{name} could not be read from electron/main.js"


def test_host_execution_is_not_described_as_sandboxed():
    """
    The renderer being sandboxed says nothing about the harness. Conflating
    them is the specific error this section exists to prevent.
    """
    host = facts.security_model()["host_command_execution"]
    text = (host["reality"] + " " + host["consequence"]).lower()
    assert "does not block" in text or "logged" in text
    assert "not" in text and "sandbox" in text


# ---------------------------------------------------------------------------
# Data ownership
# ---------------------------------------------------------------------------


def test_authoritative_stores_are_never_described_as_rebuildable():
    """
    The whole point of the section. Something authoritative that reads as
    rebuildable is a backup somebody will skip.
    """
    for item in facts.data_ownership()["authoritative"]:
        assert item["rebuildable_from"] is None, item
        assert "REQUIRED" in item["backup"], item
        assert item.get("if_lost"), f"{item['records']}: no consequence stated"


def test_derived_stores_say_what_they_can_be_rebuilt_from():
    for item in facts.data_ownership()["derived"]:
        assert item["rebuildable_from"], item


def test_the_conversation_contradiction_is_stated_rather_than_hidden():
    """
    Rooms live only in Postgres, against the rule that everything the harness
    creates is an HTML-NN. Documentation that quietly omitted this would leave
    conversations outside the node backup with nothing to say so.
    """
    contradiction = facts.data_ownership()["known_contradiction"]
    assert "conversation" in contradiction["what"].lower()
    assert contradiction["status"] == "known and unresolved"


# ---------------------------------------------------------------------------
# Verification metadata
# ---------------------------------------------------------------------------


def test_verification_records_what_was_reviewed_and_when():
    meta = facts.verification_metadata()
    assert re.fullmatch(r"[0-9a-f]{40}", meta["reviewed_commit"]), (
        "no reviewed commit recorded; the contract cannot be reconciled against anything"
    )
    assert meta["generated_at"]
    assert meta["known_limitations"], "a document with no stated limits is claiming there are none"
    assert isinstance(meta["working_tree_clean"], bool)
