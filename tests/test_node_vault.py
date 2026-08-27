"""
Vault-tagged nodes carry their sensitive values encrypted.

Steven's design: *"Maybe the nodes that containe PII can be tagged vault and
anything in vault is encrypted"*.

Chosen over the alternative — stripping PII out of the profile node and keeping
it only in Postgres. That worked, but it made the database the real store and
the node a lossy summary, against the standing requirement that everything the
harness creates and consumes is an HTML-NN. Tagging keeps the node as the
record and seals the sensitive half of it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from python.harness import node_vault
from python.harness.credential_crypto import is_encrypted
from python.harness.node_vault import (
    VAULT_FIELDS,
    VAULT_MARKER,
    is_sealed,
    seal_for,
    seal_state,
    summarise_vault,
    unseal_state,
)

PII = ("pii_legal_name", "pii_dob", "pii_email", "pii_phone", "pii_registries")

STATE = {
    "name": "Weavor",
    "biography": "A biography that is not secret.",
    "pii_legal_name": "A Real Name",
    "pii_dob": "1974-09-19",
    "pii_email": "a@b.c",
    "pii_phone": "5551234",
    "pii_registries": [{"label": "Passport", "location": "/x"}],
}


def test_sealing_encrypts_only_the_vault_fields():
    sealed = seal_state(STATE, PII)

    assert is_sealed(sealed)
    for field in ("pii_legal_name", "pii_dob", "pii_email", "pii_phone"):
        assert is_encrypted(sealed[field]), f"{field} was left in clear"
    # The rest of the node stays readable, or the node stops being useful.
    assert sealed["name"] == "Weavor"
    assert sealed["biography"] == "A biography that is not secret."


def test_no_secret_survives_in_the_serialised_node():
    """This is the property that matters on a USB stick."""
    blob = json.dumps(seal_state(STATE, PII))
    for secret in ("A Real Name", "1974-09-19", "a@b.c", "5551234", "Passport"):
        assert secret not in blob, f"{secret!r} is readable in the node file"


def test_unsealing_returns_the_original():
    sealed = seal_state(STATE, PII)
    assert unseal_state(sealed) == STATE


def test_sealing_is_idempotent():
    """A profile is re-saved on every edit; a second pass must not double-wrap
    into a value that decrypts to an envelope."""
    once = seal_state(STATE, PII)
    twice = seal_state(once, PII)
    assert twice == once
    assert unseal_state(twice) == STATE


def test_an_unsealed_state_passes_through():
    assert unseal_state(dict(STATE)) == STATE


def test_an_empty_vault_is_still_marked():
    """So a reader can tell an empty vault from a node written before sealing
    existed."""
    sealed = seal_state({"name": "x", "pii_dob": ""}, PII)
    assert is_sealed(sealed)
    assert unseal_state(sealed)["pii_dob"] == ""


def test_node_types_opt_in():
    """Absent from the registry means nothing is sealed -- a new node type must
    not acquire sealing it was never designed for."""
    assert seal_for("some-other-node", dict(STATE)) == STATE
    assert is_sealed(seal_for("user-profile", dict(STATE)))


def test_the_registry_covers_the_profile_fields():
    assert set(VAULT_FIELDS["user-profile"]) == set(PII)


def test_a_missing_key_leaves_plaintext_and_says_so(monkeypatch, caplog):
    """Storing plaintext is bad; refusing to save the user's profile is worse.
    What is not acceptable is doing either one quietly."""
    import python.harness.credential_crypto as cc
    monkeypatch.setattr(cc, "load_or_create_key", lambda: None)

    with caplog.at_level("ERROR"):
        out = seal_state(STATE, PII)

    assert out == STATE
    assert not is_sealed(out)
    assert any("no encryption key" in r.message for r in caplog.records)


# -- the summary that retrieval is allowed to index --------------------------

def test_the_summary_gives_nothing_away():
    text = summarise_vault(STATE, PII)
    for secret in ("A Real Name", "1974-09-19", "a@b.c", "5551234"):
        assert secret not in text


def test_the_summary_says_the_vault_exists():
    """Tala must be able to offer to look something up rather than claiming she
    knows nothing about the user."""
    text = summarise_vault(STATE, PII)
    assert "vault" in text.lower()
    assert "5" in text  # five populated items

    empty = summarise_vault({"name": "x"}, PII)
    assert "empty" in empty.lower()


# -- the wiring in server.py -------------------------------------------------

SERVER = Path("python/harness/server.py")


def test_both_profile_writers_seal():
    """There are two node-writing paths and an earlier fix reached only one."""
    src = SERVER.read_text(encoding="utf-8")
    assert src.count('seal_for("user-profile", state_data)') == 2


def test_no_writer_interpolates_pii_into_indexed_prose():
    src = SERVER.read_text(encoding="utf-8")
    for field in ("{pii_legal_name}", "{pii_dob}", "{pii_email}", "{pii_phone}"):
        assert f"Legal Name: {field}" not in src
        assert f"<p>Date of Birth: {field}</p>" not in src
    assert "summarise_vault(" in src


def test_parse_html_does_not_silently_unseal():
    """If reading any node returned plaintext, sealing would be decoration:
    retrieval and anything shipping a node to a peer would carry the values
    straight back out."""
    parser = Path("sdk/agent/parser.py").read_text(encoding="utf-8")
    assert "unseal_state" not in parser
    assert "node_vault" not in parser


def test_the_entitled_reader_unseals():
    src = SERVER.read_text(encoding="utf-8")
    assert "sd = unseal_state(node.state_data or {})" in src
