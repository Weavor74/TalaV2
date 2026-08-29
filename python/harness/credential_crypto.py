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

#: Secrets that authenticate: keys, tokens, passwords.
#:
#: Both snake_case and camelCase spellings appear, because the renderer writes
#: JSON straight from form fields and the harness writes Python dicts. Missing
#: one spelling silently leaves that value in clear, so both are listed rather
#: than normalised at one end -- normalising would mean trusting every writer
#: to have done it.
CREDENTIAL_SECRET_FIELDS = frozenset({
    # generic
    "key", "api_key", "apiKey", "token", "password", "passphrase", "secret",
    "private_key", "privateKey", "client_secret", "clientSecret",
    "refresh_token", "refreshToken", "access_token", "accessToken",
    # object storage (UI-070) -- the highest-value credential in the settings
    # surface and, before this, the one with the weakest handling
    "secret_access_key", "secretAccessKey", "aws_secret_access_key",
    "account_key", "accountKey", "sas_token", "sasToken",
    "connection_string", "connectionString",
    # database (UI-074)
    "db_password", "dbPassword",
})

#: Personal identity (UI-055). Encrypted at rest like a credential, but kept a
#: separate set: redacting these in a settings panel would blank fields the
#: user is trying to read back, and "email" as a username is not a secret.
PII_FIELDS = frozenset({
    "dob", "date_of_birth", "legal_name", "legalName", "ssn",
    "passport", "drivers_license", "national_id",
})

#: Everything encrypted at rest.
SECRET_FIELDS = CREDENTIAL_SECRET_FIELDS | PII_FIELDS

#: Fields that identify rather than authenticate. An access key id or a bucket
#: name has to stay queryable and is not worth a key to read.
NEVER_ENCRYPT = frozenset({
    "access_key_id", "accessKeyId", "bucket", "region", "endpoint",
    "username", "user", "host", "port", "database", "auth_type", "type",
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


def _decrypt_value(key: bytes, envelope: dict[str, Any], field: str = "") -> str | None:
    try:
        nonce = base64.b64decode(envelope["n"])
        ct = base64.b64decode(envelope["ct"])
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as exc:
        # Name the field and say what InvalidTag actually means.
        #
        # This used to read "a value failed to decrypt (InvalidTag)" with no
        # identifier, which is undiagnosable: it fired twice on every goal run
        # for a whole session and nobody could tell whether it was one
        # credential or fifty, or which store it came from. An error nobody can
        # act on is an error everybody learns to scroll past.
        #
        # InvalidTag specifically means the ciphertext did not authenticate --
        # the data is intact but the key is not the one that encrypted it. That
        # is almost always a key/data split: the key lives in the user-data root
        # (vault/.keys/credential_key.bin) while the ciphertext lives in
        # Postgres, so pointing TALA_USER_DATA somewhere new mints a fresh key
        # and every stored secret stops decrypting while the database is
        # untouched.
        detail = (
            "the key does not match the one that encrypted it (the key lives in "
            "the user-data root, the value lives in the database, so they can be "
            "separated)"
            if type(exc).__name__ == "InvalidTag"
            else str(exc)
        )
        log.error(
            "credential_crypto: %s failed to decrypt (%s): %s. "
            "Leaving it as-is rather than dropping it.",
            f"field {field!r}" if field else "a value",
            type(exc).__name__,
            detail,
        )
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
        if field in NEVER_ENCRYPT:
            continue
        if field in SECRET_FIELDS and isinstance(out[field], str) and out[field]:
            out[field] = _encrypt_value(key, out[field])
    return out


def _unwrap(value: Any, limit: int = 5) -> Any:
    """
    Decode a JSON value that may have been encoded more than once.

    Some columns are double-encoded -- a jsonb codec plus json.dumps at the
    call site -- so a single loads() yields a string again. A single loads()
    here returned that string, and the caller then got an AttributeError or,
    worse, treated it as an empty config.
    """
    seen = 0
    while isinstance(value, (str, bytes)) and seen < limit:
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
        seen += 1
    return value


def decrypt_config(config: Any) -> Any:
    """Decrypt any encrypted fields. Plaintext values pass straight through."""
    if isinstance(config, (str, bytes)):
        config = _unwrap(config)
        if isinstance(config, str):
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
            plain = _decrypt_value(key, value, field)
            if plain is not None:
                out[field] = plain
    return out


# -- single values (plain TEXT columns) --------------------------------------
#
# database_providers.password is a bare TEXT column, not JSONB, so there is no
# config dict to walk (UI-074). These wrap one string in the same envelope,
# serialised to JSON so it still fits a TEXT column.

def encrypt_value(plaintext: str) -> str:
    """Encrypt a single string for storage in a TEXT column. Idempotent."""
    if not isinstance(plaintext, str) or not plaintext:
        return plaintext
    if is_encrypted_text(plaintext):
        return plaintext
    key = load_or_create_key()
    if key is None:
        return plaintext
    return json.dumps(_encrypt_value(key, plaintext))


def is_encrypted_text(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("{"):
        return False
    try:
        return is_encrypted(json.loads(value))
    except (json.JSONDecodeError, ValueError):
        return False


def decrypt_value(stored: str) -> str:
    """Decrypt a TEXT column value. Plaintext passes straight through."""
    if not is_encrypted_text(stored):
        return stored
    key = load_or_create_key()
    if key is None:
        log.error("credential_crypto: encrypted value present but no key available.")
        return stored
    plain = _decrypt_value(key, json.loads(stored))
    # Returning the ciphertext rather than "" on failure: an unreadable
    # password is recoverable, a silently blanked one is not.
    return stored if plain is None else plain


# -- whole sections ----------------------------------------------------------
#
# profiles.config["pii_vault"] holds a legal name, date of birth, contact
# details and identity-document registries (UI-055). Every value in it is
# sensitive by definition, so matching field names would be the wrong tool --
# a field added to the vault later must not stay in clear because nobody
# remembered to add its name to a list.

def encrypt_section(section: Any) -> Any:
    """Encrypt every string value in a section. Idempotent; nesting preserved."""
    if not isinstance(section, dict):
        return section
    key = load_or_create_key()
    if key is None:
        return section
    out: dict[str, Any] = {}
    for field, value in section.items():
        if is_encrypted(value):
            # Already wrapped. Without this the recursion below treats the
            # envelope as an ordinary dict and encrypts its own nonce and
            # ciphertext, which decrypts back to an envelope rather than a
            # value -- a save-after-load cycle would have done exactly that.
            out[field] = value
        elif isinstance(value, str) and value:
            out[field] = _encrypt_value(key, value)
        elif isinstance(value, dict):
            out[field] = encrypt_section(value)
        elif isinstance(value, list):
            out[field] = [
                encrypt_section(v) if isinstance(v, dict)
                else (_encrypt_value(key, v) if isinstance(v, str) and v else v)
                for v in value
            ]
        else:
            out[field] = value
    return out


def decrypt_section(section: Any) -> Any:
    """Reverse :func:`encrypt_section`. Plaintext passes straight through."""
    if isinstance(section, (str, bytes)):
        section = _unwrap(section)
        if isinstance(section, str):
            return section
    if not isinstance(section, dict):
        return section
    key = load_or_create_key()
    out: dict[str, Any] = {}
    for field, value in section.items():
        if is_encrypted(value):
            if key is None:
                out[field] = value
                continue
            plain = _decrypt_value(key, value, field)
            out[field] = value if plain is None else plain
        elif isinstance(value, dict):
            out[field] = decrypt_section(value)
        elif isinstance(value, list):
            out[field] = [
                decrypt_section(v) if isinstance(v, dict) else v for v in value
            ]
        else:
            out[field] = value
    return out
