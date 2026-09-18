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
* **Log a bad ciphertext once.** The same sealed value is decrypted on every
  chat turn and every settings load. Re-logging InvalidTag each time trains
  everyone to ignore it; the first failure still fires at ERROR with a clear
  remediation line, later attempts for that envelope stay silent.
* **Selective.** Only fields that actually hold secrets are encrypted, so a
  human or a query can still tell an SSH credential from an API key without a
  key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

#: Ciphertexts that already failed to decrypt under the current process.
#: Fingerprinted by nonce+ciphertext so a bad row logs once and then stays
#: quiet across the chat/settings paths that re-decrypt the same envelope on
#: every request. A real key mismatch is still reported — just not fifty times.
_failed_decrypt_fps: set[str] = set()
_failed_decrypt_lock = threading.Lock()

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


def clear_decrypt_failure_cache() -> None:
    """Forget which envelopes have already been reported (tests / key restore)."""
    with _failed_decrypt_lock:
        _failed_decrypt_fps.clear()


def _envelope_fingerprint(envelope: dict[str, Any]) -> str:
    """Stable id for a ciphertext without logging the ciphertext itself."""
    n = str(envelope.get("n", ""))
    ct = str(envelope.get("ct", ""))
    return hashlib.sha256(f"{n}\0{ct}".encode("utf-8")).hexdigest()[:32]


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
        # Postgres or a sealed HTML-NN (concept-user-profile, vault docs).
        # Pointing TALA_USER_DATA somewhere new, or deleting the key file,
        # mints a fresh key and every stored secret stops decrypting while the
        # ciphertext store is untouched.
        #
        # Chat and settings re-walk the same sealed values constantly. Log the
        # first failure for each ciphertext at ERROR; stay silent after that
        # so a real problem is still visible without drowning the console.
        is_tag = type(exc).__name__ == "InvalidTag"
        detail = (
            "the key does not match the one that encrypted it (the key lives in "
            "vault/.keys/credential_key.bin under the user-data root; ciphertext "
            "lives in Postgres or a sealed HTML-NN — restore the original key, "
            "or re-enter the secret and save so it is sealed with the current key)"
            if is_tag
            else str(exc)
        )
        fp = _envelope_fingerprint(envelope)
        with _failed_decrypt_lock:
            first = fp not in _failed_decrypt_fps
            if first:
                _failed_decrypt_fps.add(fp)
        if first:
            log.error(
                "credential_crypto: %s failed to decrypt (%s): %s. "
                "Leaving it as-is rather than dropping it. "
                "Further attempts for this ciphertext will not be re-logged.",
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


# -- redaction for transport ------------------------------------------------
#
# Encrypting at rest bought nothing while ``inference.get_providers`` decrypted
# every config and shipped the plaintext apiKey to any client that could open a
# socket (SEC-W0-02). Redaction lives here, next to the encryption, so the two
# cannot drift about what counts as a secret.
#
# PII_FIELDS is deliberately *not* redacted: a settings panel that blanks the
# user's own date of birth every time they open it is a bug, not a control.

#: Sent in place of a stored secret, and accepted back from a client to mean
#: "keep the one you already have". A client that never sees a key cannot leak
#: it, but it also cannot send it back -- so the round-trip needs a word for
#: "unchanged" or the next save silently wipes every credential.
REDACTED_SENTINEL = "__tala_redacted__"


def redact_config(config: Any) -> Any:
    """
    Replace credential values with :data:`REDACTED_SENTINEL` for transport.

    Returns a copy; the input is untouched. A field that is absent or empty
    stays that way, so a client can still tell "no key configured" from "a key
    is set and withheld" without being told the key.
    """
    if not isinstance(config, dict):
        return config
    out = dict(config)
    for field, value in list(out.items()):
        if field in NEVER_ENCRYPT:
            continue
        if field in CREDENTIAL_SECRET_FIELDS and isinstance(value, str) and value:
            out[field] = REDACTED_SENTINEL
        elif field in CREDENTIAL_SECRET_FIELDS and is_encrypted(value):
            # Never decrypted on this path -- still a secret, still withheld.
            out[field] = REDACTED_SENTINEL
    return out


def config_has_secret(config: Any) -> bool:
    """True if the config holds any credential value at all."""
    if not isinstance(config, dict):
        return False
    for field, value in config.items():
        if field in NEVER_ENCRYPT or field not in CREDENTIAL_SECRET_FIELDS:
            continue
        if is_encrypted(value):
            return True
        if isinstance(value, str) and value and value != REDACTED_SENTINEL:
            return True
    return False


def merge_preserved_secrets(incoming: Any, existing: Any) -> Any:
    """
    Fold a client's config over the stored one, keeping withheld secrets.

    The rules, in order:

    * sentinel  -> keep the stored value (the client was never shown it)
    * absent    -> keep the stored value (the client did not mention the field)
    * ``""``    -> keep the stored value; a blank password box means "unchanged"
    * ``None``  -> the user explicitly cleared it; clear it
    * anything else -> a new secret the user typed

    Blank meaning "unchanged" also fixes a live credential-loss bug that
    predates redaction: the renderer prefills the edit form from a flat
    ``apiKey`` field that only exists after a previous edit, so opening any
    freshly-loaded provider and pressing save posted ``apiKey: ""`` and wiped
    the stored key. Clearing is rare and destructive; it gets the explicit
    spelling, not the one you land on by accident.

    ``existing`` is expected to be the *decrypted* stored config, because the
    caller re-encrypts the merged result on the way back down.
    """
    if not isinstance(incoming, dict):
        return incoming
    out = dict(incoming)
    if not isinstance(existing, dict):
        existing = {}
    for field in CREDENTIAL_SECRET_FIELDS:
        if field in NEVER_ENCRYPT or field not in existing:
            continue
        if field not in out:
            out[field] = existing[field]
            continue
        value = out[field]
        if value is None:
            out.pop(field)          # explicit clear
        elif value == REDACTED_SENTINEL or (isinstance(value, str) and not value.strip()):
            out[field] = existing[field]
    return out


def resolve_secret(incoming: Any, existing: Any) -> str:
    """
    Scalar counterpart of :func:`merge_preserved_secrets`.

    ``git_providers.api_key`` and ``search_providers.api_key`` are bare TEXT
    columns with no config dict to walk, but they are saved by the same
    replace-the-whole-table pattern and so carry the same hazard.

    Same rules: sentinel, absent and blank keep the stored value; an explicit
    ``None`` clears it.
    """
    existing_str = existing if isinstance(existing, str) else ""
    if incoming is None:
        return ""
    if not isinstance(incoming, str):
        return existing_str
    if incoming == REDACTED_SENTINEL or not incoming.strip():
        return existing_str
    return incoming


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
