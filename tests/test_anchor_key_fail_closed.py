"""
A missing document key must never be quietly replaced when a chain already
holds documents encrypted under the old one.

4,126 of the 7,966 encrypted documents on the live chain (51.8%) opened with no
key that exists anywhere on the machine. The writers were test and sandbox
processes: the key path follows TALA_USER_DATA, so a sandbox found no key and
minted a throwaway one, while the chain URL and admin secret did not follow it,
so the live node accepted the write. Nothing recorded which key was used and the
throwaway keys were deleted with their sandboxes.
"""
from __future__ import annotations

import json
import os
import socket

import pytest

from sdk.agent import anchor, ghost_persistence

_ENV = ("TALA_CHAIN_DOCUMENT_KEY", "TALA_CHAIN_DOCUMENT_KEY_FILE", "TALA_CHAIN_DATA_DIR",
        "TALA_USER_DATA", "TALA_ALLOW_NEW_CHAIN_KEY", "TALA_CHAIN_ADMIN_SECRET")


def _encrypted_tx():
    return {"id": "node-x", "type": "node-create", "document": {"encrypted": True, "ciphertext": "AAAA"}}


def _chain(directory, *, with_documents: bool):
    directory.mkdir(parents=True, exist_ok=True)
    txs = [_encrypted_tx()] if with_documents else [{"id": "plain", "type": "task-complete"}]
    (directory / "chain.json").write_text(json.dumps([{"index": 0, "transactions": []},
                                                      {"index": 1, "transactions": txs}]), encoding="utf-8")
    return directory


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_a_missing_key_is_not_replaced_when_the_chain_holds_documents(tmp_path, clean_env):
    chain = _chain(tmp_path / "chain", with_documents=True)
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(chain))
    clean_env.setenv("TALA_USER_DATA", str(tmp_path / "sandbox"))

    with pytest.raises(anchor.ChainKeyMissingError):
        anchor._load_or_create_document_key()
    assert not (tmp_path / "sandbox").exists(), "refusing must not leave a minted key behind"


def test_the_first_key_is_minted_when_the_chain_has_no_documents(tmp_path, clean_env):
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(_chain(tmp_path / "chain", with_documents=False)))
    clean_env.setenv("TALA_USER_DATA", str(tmp_path / "data"))

    key = anchor._load_or_create_document_key()

    assert len(key) == 32
    assert (tmp_path / "data" / "vault" / ".keys" / "blockchain_document_key.bin").read_bytes() == key


def test_the_first_key_is_minted_when_there_is_no_chain_at_all(tmp_path, clean_env):
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(tmp_path / "no-chain-here"))
    clean_env.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    assert len(anchor._load_or_create_document_key()) == 32


def test_an_existing_key_is_used_untouched_even_beside_documents(tmp_path, clean_env):
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(_chain(tmp_path / "chain", with_documents=True)))
    clean_env.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    keyfile = tmp_path / "data" / "vault" / ".keys" / "blockchain_document_key.bin"
    keyfile.parent.mkdir(parents=True)
    keyfile.write_bytes(b"k" * 32)

    assert anchor._load_or_create_document_key() == b"k" * 32


def test_deliberate_rotation_is_still_possible(tmp_path, clean_env):
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(_chain(tmp_path / "chain", with_documents=True)))
    clean_env.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    clean_env.setenv("TALA_ALLOW_NEW_CHAIN_KEY", "1")
    assert len(anchor._load_or_create_document_key()) == 32


def test_an_explicit_key_file_that_is_missing_is_refused_the_same_way(tmp_path, clean_env):
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(_chain(tmp_path / "chain", with_documents=True)))
    clean_env.setenv("TALA_CHAIN_DOCUMENT_KEY_FILE", str(tmp_path / "elsewhere" / "k.bin"))
    with pytest.raises(anchor.ChainKeyMissingError):
        anchor._load_or_create_document_key()


def test_encrypting_without_the_key_raises_instead_of_writing_under_a_new_one(tmp_path, clean_env):
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(_chain(tmp_path / "chain", with_documents=True)))
    clean_env.setenv("TALA_USER_DATA", str(tmp_path / "sandbox"))
    with pytest.raises(anchor.ChainKeyMissingError):
        anchor._encrypt_document({"a": 1}, "node-x:abc")


def test_anchoring_reports_failure_and_posts_nothing_when_the_key_is_refused(tmp_path, clean_env):
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(_chain(tmp_path / "chain", with_documents=True)))
    clean_env.setenv("TALA_USER_DATA", str(tmp_path / "sandbox"))
    sent = []
    clean_env.setattr(anchor.urllib.request, "urlopen", lambda *a, **k: sent.append(a) or (_ for _ in ()).throw(AssertionError("posted")))

    _, chaincode_id, status, _, _ = anchor.canonicalize_and_anchor("node-x", "<p>x</p>", {})

    assert (chaincode_id, status) == ("unanchored", "failed")
    assert sent == []


def test_the_chain_dir_follows_the_variable_the_node_reads(tmp_path, clean_env):
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(tmp_path / "pinned"))
    clean_env.setenv("TALA_USER_DATA", str(_chain(tmp_path / "ud" / "blockchain" / "data", with_documents=True).parent.parent))
    assert ghost_persistence.local_chain_data_dir() == (tmp_path / "pinned").resolve()


def test_without_the_variable_the_user_data_chain_wins_when_it_exists(tmp_path, clean_env):
    user_data = tmp_path / "ud"
    _chain(user_data / "blockchain" / "data", with_documents=False)
    clean_env.setenv("TALA_USER_DATA", str(user_data))
    assert ghost_persistence.local_chain_data_dir() == (user_data / "blockchain" / "data").resolve()


def test_a_pinned_chain_dir_never_falls_back_to_another_nodes_admin_secret(tmp_path, clean_env):
    """The secret is the node's own: reading the repo's while talking to a pinned node is the leak."""
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    clean_env.setenv("TALA_CHAIN_DATA_DIR", str(pinned))
    assert ghost_persistence._local_chain_admin_headers() == {}
    (pinned / "admin_secret.txt").write_text("pinned-secret\n")
    assert ghost_persistence._local_chain_admin_headers() == {"X-Tala-Chain-Secret": "pinned-secret"}


def test_the_suite_cannot_reach_the_live_chain_node():
    """conftest pins the endpoint: a dead port and a sandbox data dir, so no test can anchor anywhere real."""
    host, port = ghost_persistence.local_chain_host_port()
    assert port != 8332
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex((host, port)) != 0, "something is listening where the suite sends anchors"
    data_dir = ghost_persistence.local_chain_data_dir()
    assert os.path.realpath(data_dir) != os.path.realpath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "blockchain", "data"))
