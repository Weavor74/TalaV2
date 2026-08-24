"""
credential_crypto.py — encrypt credential material at rest.

Answers UI-080. ``auth_credentials.config`` is plain JSONB holding the most
sensitive values in the app: full SSH private key blocks and their passphrases,
RDP passwords, API keys, and OAuth client secrets and refresh tokens. The AUTH
panel meanwhile promises *"Zero-Trust Database Encryption"*, which was true only
of the words "persisted locally inside PostgreSQL" — nothing was encrypted.

Uses what the project already relies on: AES-256-GCM from ``cryptography`` (the
same primitive as ``e2ee_manager``) with a 32-byte key file under
``vault/.keys/``, matching the existing ``blockchain_document_key.bin``
convention.

Design notes:

* **Envelope, not opaque blob.** An encrypted value is
  ``{"__enc": "aesgcm-v1", "n": <nonce>, "ct": <ciphertext>}``. Plaintext rows
  are left readable, so this can be adopted without a flag day and a partially
  migrated table still works.
* **Never destroy on failure.** If the key is missing or a value will not
  decrypt, the caller gets the row back untouched and a logged error — a
  credential you cannot read is recoverable, one that was silently dropped is
  not.
* **Selective.** Only fields that actually hold secrets are encrypted, so a
  human or a query can still tell an SSH credential from an API key without a
  key.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

ENVELOPE_MARKER = "__enc"
ENVELOPE_VERSION = "aesgcm-v1"

#: Fields inside a credential config that hold secret material.
SECRET_FIELDS = frozenset({
    "api_key", "apiKey", "token", "password", "passphrase",
    "private_key", "privateKey", "client_secret", "clientSecret",
    "refresh_token", "refreshToken", "secret",
})

_KEY_FILE = "credential_key.bin"


def _key_path() -> Path:
    from sdk.agent.documentation_nodes import resolve_user_data_dir
    return resolve_user_data_dir() / "vault" / ".keys" / _KEY_FILE


def load_or_create_key() -> bytes | None:
    """
    Return the 32-byte credential key, creating it on first use.

    Returns None rather than raising if the key cannot be established: callers
    fall back to storing plaintext, which is what happens today anyway. Silently
    refusing to save a credential would be worse than saving it unencrypted.
    """
    path = _key_path()
    try:
        if path.exists():
            key = path.read_bytes()
            if len(key) == 32:
                return key
            log.error("credential_crypto: %s is %d bytes, expected 32; refusing to use it.",
                      path, len(key))
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        key = AESGCM.generate_key(bit_length=256)
        path.write_bytes(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best effort; Windows ACLs differ
        log.info("credential_crypto: created a new credential key at %s", path)
        return key
    except OSError as exc:
        log.error("credential_crypto: could not load or create the key: %s", exc)
        return None


def is_encrypted(value: Any) -> bool:
    return isinstance(value, dict) and value.get(ENVELOPE_MARKER) == ENVELOPE_VERSION


def _encrypt_value(key: bytes, plaintext: str) -> dict[str, str]:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return {
        ENVELOPE_MARKER: ENVELOPE_VERSION,
        "n": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
    }


def _decrypt_value(key: bytes, envelope: dict[str, Any]) -> str | None:
    try:
        nonce = base64.b64decode(envelope["n"])
        ct = base64.b64decode(envelope["ct"])
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as exc:
        log.error("credential_crypto: a value failed to decrypt (%s). "
                  "Leaving it as-is rather than dropping it.", type(exc).__name__)
        return None


def encrypt_config(config: Any) -> Any:
    """Encrypt the secret fields of a credential config. Idempotent."""
    if not isinstance(config, dict):
        return config
    key = load_or_create_key()
    if key is None:
        return config
    out = dict(config)
    for field in list(out):
        if field in SECRET_FIELDS and isinstance(out[field], str) and out[field]:
            out[field] = _encrypt_value(key, out[field])
    return out


def decrypt_config(config: Any) -> Any:
    """Decrypt any encrypted fields. Plaintext values pass straight through."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            return config
    if not isinstance(config, dict):
        return config
    if not any(is_encrypted(v) for v in config.values()):
        return config
    key = load_or_create_key()
    if key is None:
        log.error("credential_crypto: encrypted values present but no key available.")
        return config
    out = dict(config)
    for field, value in list(out.items()):
        if is_encrypted(value):
            plain = _decrypt_value(key, value)
            if plain is not None:
                out[field] = plain
    return out
