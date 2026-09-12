"""
tests/test_p0_security_fixes.py — the release-blocker fixes, 2026-09-12.

Each test pins one of the P0 findings from
docs/production-readiness-review-2026-09-09.md closed:

1. `p2p.encrypted_payload` dispatched its decrypted inner method against the
   full handler map — an unauthenticated remote shell for anyone a connection
   node had auto-enrolled. The envelope now dispatches only E2EE_INNER_METHODS,
   and only for an APPROVED contact.
2. Re-enrolment overwrote an existing contact's shared_secret and endpoint —
   knowing a username was enough to hijack the contact. An existing row now
   re-keys only for a caller proving the secret already on it.
3. Peer connections were gated per-handler; the connection-class layer now
   grants an unapproved peer only PEER_UNAPPROVED_METHODS.
4. Peer traffic dialled plaintext ws:// across the internet.
5. The web bridge served the vault, .env and chain keys to any caller inside
   the data root.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
import pytest_asyncio

from python.harness import db, server, vps_mode, webui

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pool():
    await db.init_pool()
    # The enrolment rate cap is global state; a full test run enrols more
    # than a real node ever would in one minute.
    server._ENROLMENT_HITS.clear()
    yield
    await db.close_pool()


def signed_handshake(username, wallet, secret, endpoint=""):
    packet = {
        "protocol": "tala-p2p/1.0", "type": "handshake",
        "sender_username": username, "sender_wallet": wallet,
        "sender_endpoint": endpoint, "timestamp": "2026-09-12T00:00:00+00:00",
        "enrollment_secret": secret,
    }
    unsigned = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    packet["signature"] = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    return packet


async def _insert_contact(username, wallet, secret, status):
    await db.execute(
        """
        INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret, permissions, status)
        VALUES ($1, $2, '', $3, '{}'::jsonb, $4)
        ON CONFLICT (username) DO UPDATE SET
            wallet_address = EXCLUDED.wallet_address,
            shared_secret = EXCLUDED.shared_secret,
            status = EXCLUDED.status
        """,
        username, wallet, secret, status,
    )


async def _cleanup(*usernames):
    for username in usernames:
        await db.execute("DELETE FROM contacts WHERE username = $1", username)


# ---------------------------------------------------------------------------
# 1. The encrypted envelope is a peer surface, not a dispatch table
# ---------------------------------------------------------------------------


def test_the_envelope_surface_contains_no_local_verbs():
    for verb in ("shell.run", "code.apply_patch", "vault.read", "file.write",
                 "system.apply_update", "p2p.encrypted_payload", "relay.send"):
        assert verb not in server.E2EE_INNER_METHODS, f"{verb} must not ride an envelope"
    # And what the outbound wrapper legitimately produces is present.
    assert "p2p.receive_message" in server.E2EE_INNER_METHODS


async def test_an_envelope_from_a_stranger_is_refused_before_decryption(pool):
    response = json.loads(await server.handle_p2p_encrypted_payload(
        {"sender_username": f"@nobody-{uuid.uuid4().hex[:8]}", "ciphertext": "x", "nonce": "y"},
        "req-1",
    ))
    assert response.get("error"), "a stranger's envelope must not be dispatched"


async def test_an_envelope_from_an_unapproved_contact_is_refused(pool):
    username = f"@enrolled-{uuid.uuid4().hex[:8]}"
    await _insert_contact(username, f"wallet-{uuid.uuid4().hex[:8]}", "s3cret", "enrolled")
    try:
        response = json.loads(await server.handle_p2p_encrypted_payload(
            {"sender_username": username, "ciphertext": "x", "nonce": "y"},
            "req-2",
        ))
        assert response.get("error")
        assert "refused" in response["error"]["message"] or "not" in response["error"]["message"]
    finally:
        await _cleanup(username)


async def test_an_approved_contact_still_cannot_smuggle_shell_run(pool, monkeypatch):
    username = f"@approved-{uuid.uuid4().hex[:8]}"
    await _insert_contact(username, f"wallet-{uuid.uuid4().hex[:8]}", "s3cret", "approved")

    from python.harness.e2ee_manager import e2ee_manager
    monkeypatch.setattr(
        e2ee_manager, "decrypt",
        lambda sender, ct, nonce: {"method": "shell.run", "params": {"command": "id"}},
    )
    try:
        response = json.loads(await server.handle_p2p_encrypted_payload(
            {"sender_username": username, "ciphertext": "x", "nonce": "y"},
            "req-3",
        ))
        assert response.get("error"), "shell.run inside an envelope must be refused"
        assert "cannot be called inside an encrypted payload" in response["error"]["message"]
    finally:
        await _cleanup(username)


async def test_an_approved_contact_reaches_the_allowlisted_surface(pool, monkeypatch):
    username = f"@approved-{uuid.uuid4().hex[:8]}"
    await _insert_contact(username, f"wallet-{uuid.uuid4().hex[:8]}", "s3cret", "approved")

    from python.harness.e2ee_manager import e2ee_manager
    monkeypatch.setattr(
        e2ee_manager, "decrypt",
        lambda sender, ct, nonce: {"method": "p2p.semantic_search", "params": {}},
    )

    async def fake_handler(params, req_id):
        return server._ok(req_id, {"dispatched": True})

    monkeypatch.setitem(server._HANDLERS, "p2p.semantic_search", fake_handler)
    try:
        response = json.loads(await server.handle_p2p_encrypted_payload(
            {"sender_username": username, "ciphertext": "x", "nonce": "y"},
            "req-4",
        ))
        assert response.get("result", {}).get("dispatched") is True
    finally:
        await _cleanup(username)


# ---------------------------------------------------------------------------
# 2. Re-enrolment is not a takeover
# ---------------------------------------------------------------------------


async def test_reenrolling_an_existing_username_cannot_mint_a_new_secret(pool):
    username = f"@victim-{uuid.uuid4().hex[:8]}"
    original_secret = "the-original-secret"
    await _insert_contact(username, f"wallet-{uuid.uuid4().hex[:8]}", original_secret, "approved")
    try:
        packet = signed_handshake(
            username, f"attacker-wallet-{uuid.uuid4().hex[:8]}", "attacker-minted-secret",
            endpoint="wss://attacker.example/ws",
        )
        result = await server._enroll_unknown_tala_contact(packet)
        assert result is None, "an unproven re-enrolment must be refused"

        row = await db.fetchrow("SELECT shared_secret, endpoint_url, status FROM contacts WHERE username = $1", username)
        assert row["shared_secret"] == original_secret
        assert row["endpoint_url"] == ""
        assert row["status"] == "approved"
    finally:
        await _cleanup(username)


async def test_a_peer_proving_its_own_secret_may_update_its_row(pool):
    username = f"@returning-{uuid.uuid4().hex[:8]}"
    secret = "the-original-secret"
    wallet = f"wallet-{uuid.uuid4().hex[:8]}"
    await _insert_contact(username, wallet, secret, "approved")
    try:
        packet = signed_handshake(username, wallet, secret, endpoint="wss://new.example/ws")
        result = await server._enroll_unknown_tala_contact(packet)
        assert result is not None, "possession of the row's secret is proof enough"

        row = await db.fetchrow("SELECT endpoint_url, status FROM contacts WHERE username = $1", username)
        assert row["endpoint_url"] == "wss://new.example/ws"
        assert row["status"] == "approved", "re-enrolment must not touch status"
    finally:
        await _cleanup(username)


# ---------------------------------------------------------------------------
# 3. The unapproved-peer surface
# ---------------------------------------------------------------------------


def test_an_unapproved_peer_gets_handshakes_and_relay_only():
    assert "p2p.encrypted_payload" not in vps_mode.PEER_UNAPPROVED_METHODS
    assert "p2p.receive_message" not in vps_mode.PEER_UNAPPROVED_METHODS
    assert "p2p.receive_handshake" in vps_mode.PEER_UNAPPROVED_METHODS
    assert "relay.send" in vps_mode.PEER_UNAPPROVED_METHODS
    assert vps_mode.PEER_UNAPPROVED_METHODS < vps_mode.PEER_CONNECTION_METHODS | {"p2p.handshake"} | vps_mode.RELAYABLE_METHODS


# ---------------------------------------------------------------------------
# 4. No plaintext peer traffic off this machine
# ---------------------------------------------------------------------------


def test_plaintext_ws_to_the_internet_is_refused(monkeypatch):
    monkeypatch.delenv("TALA_ALLOW_PLAINTEXT_PEER_WS", raising=False)
    with pytest.raises(ValueError):
        server._require_peer_transport_security("ws://talacore.app:8765")


def test_loopback_and_tls_are_fine(monkeypatch):
    monkeypatch.delenv("TALA_ALLOW_PLAINTEXT_PEER_WS", raising=False)
    assert server._require_peer_transport_security("ws://127.0.0.1:8765") == "ws://127.0.0.1:8765"
    assert server._require_peer_transport_security("ws://localhost:8765") == "ws://localhost:8765"
    assert server._require_peer_transport_security("wss://talacore.app/ws") == "wss://talacore.app/ws"


def test_a_lab_can_opt_back_in(monkeypatch):
    monkeypatch.setenv("TALA_ALLOW_PLAINTEXT_PEER_WS", "1")
    assert server._require_peer_transport_security("ws://192.168.1.5:8765") == "ws://192.168.1.5:8765"


def test_the_default_vps_endpoint_is_tls():
    assert server._default_vps_endpoint().startswith("wss://")


# ---------------------------------------------------------------------------
# 5. The web bridge does not serve secrets
# ---------------------------------------------------------------------------


def test_the_bridge_refuses_the_vault_env_and_chain_keys(tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "wallet_private_key.pem").write_text("pem", encoding="utf-8")
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=x", encoding="utf-8")
    (tmp_path / "blockchain" / "data").mkdir(parents=True)
    (tmp_path / "blockchain" / "data" / "poa_keys.json").write_text("{}", encoding="utf-8")

    for candidate in (
        "vault/wallet_private_key.pem",
        "vault/wallet_seed_phrase.txt",
        ".env",
        "blockchain/data/poa_keys.json",
        "blockchain/data/admin_secret.txt",
        "anything/nested/key.pem",
    ):
        with pytest.raises(PermissionError):
            webui._safe_path(tmp_path, candidate)


def test_ordinary_files_are_still_served(tmp_path):
    (tmp_path / "nodes").mkdir()
    (tmp_path / "nodes" / "doc.html").write_text("<html/>", encoding="utf-8")
    resolved = webui._safe_path(tmp_path, "nodes/doc.html")
    assert resolved == (tmp_path / "nodes" / "doc.html").resolve()
