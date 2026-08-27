"""
node_vault.py — vault-tagged nodes carry their sensitive values encrypted.

Steven's design, 2026-08-23:

    "Maybe the nodes that containe PII can be tagged vault and anything in
    vault is encrypted"

Better than the alternative it replaced, which was to strip PII out of the
profile node and keep it only in Postgres. That worked, but it cut against the
standing requirement that everything the harness creates and consumes is an
HTML-NN: it made the database the real store and the node a lossy summary.

Tagging inverts it. The node stays the record, and the sensitive part of it is
sealed. Three properties follow:

* **Encrypted at rest.** A node file on disk -- or on the USB stick, which is
  the case that matters, since a portable install travels through places its
  owner does not control -- carries ciphertext for its vault fields.
* **Not silently unsealed.** ``parse_html`` deliberately does *not* decrypt.
  If reading a node returned plaintext automatically, the encryption would be
  decoration: every existing reader, including retrieval and anything that
  ships a node to a peer, would carry the values straight back out. Unsealing
  is an explicit call.
* **Visible without being readable.** The marker and a non-sensitive summary
  stay in clear, so retrieval can still answer "does this user have an identity
  vault" and Tala can know a thing exists without being able to read it.

The key is the same one credentials use (``vault/.keys/credential_key.bin``),
so a portable install has one key to carry rather than several.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: Marker written into a node's state. Named for what it means to a reader.
VAULT_MARKER = "vault_sealed"

#: Which state fields are vault material, per node type. A node type absent
#: from here seals nothing -- opt in, never opt out, so a new node type cannot
#: acquire sealing it was never designed for.
VAULT_FIELDS: dict[str, tuple[str, ...]] = {
    "user-profile": (
        "pii_legal_name", "pii_dob", "pii_email", "pii_phone", "pii_registries",
    ),
}


def is_sealed(state: Any) -> bool:
    return isinstance(state, dict) and bool(state.get(VAULT_MARKER))


def seal_state(state: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """
    Encrypt the named fields of a node's state and mark it sealed.

    Idempotent: sealing an already-sealed state returns it unchanged rather
    than double-wrapping, which matters because a profile is re-saved on every
    edit and a second pass would produce a value that decrypts to an envelope.
    """
    from python.harness.credential_crypto import encrypt_section, load_or_create_key

    if not isinstance(state, dict) or not fields:
        return state
    if is_sealed(state):
        return state

    if load_or_create_key() is None:
        # Storing plaintext is bad; refusing to save the user's profile is
        # worse. Say so loudly rather than failing quietly either way.
        log.error(
            "node_vault: no encryption key available -- sealing %d field(s) was "
            "skipped and they are being written in clear.", len(fields),
        )
        return state

    sealed = dict(state)
    payload = {f: sealed[f] for f in fields if f in sealed and sealed[f]}
    if not payload:
        # Nothing sensitive present. Still mark it, so a reader can tell an
        # empty vault from a node that predates sealing entirely.
        sealed[VAULT_MARKER] = True
        return sealed

    encrypted = encrypt_section(payload)
    sealed.update(encrypted)
    sealed[VAULT_MARKER] = True
    return sealed


def unseal_state(state: Any) -> dict[str, Any]:
    """
    Decrypt a sealed state. Unsealed states pass through untouched.

    Callers must be ones that are entitled to the values -- the settings panel
    the user is looking at, or the astrology engine reading a date of birth.
    Not retrieval, and not anything that sends a node to a peer.
    """
    from python.harness.credential_crypto import decrypt_section

    if not isinstance(state, dict):
        return state if isinstance(state, dict) else {}
    if not is_sealed(state):
        return dict(state)

    out = decrypt_section(state)
    out.pop(VAULT_MARKER, None)
    return out


def seal_for(node_type: str, state: dict[str, Any]) -> dict[str, Any]:
    """Seal a state using the field list registered for *node_type*."""
    return seal_state(state, VAULT_FIELDS.get(node_type, ()))


def summarise_vault(state: dict[str, Any], fields: tuple[str, ...]) -> str:
    """
    A sentence about the vault that gives nothing away.

    Retrieval indexes a node's prose, so the prose must not contain the values.
    It should still say enough that Tala knows the vault exists and can offer
    to look something up rather than claiming she knows nothing about the user.
    """
    present = sum(
        1 for f in fields if isinstance(state.get(f), (str, list, dict)) and state.get(f)
    )
    if not present:
        return (
            "This user has an empty identity vault. Nothing is recorded in it."
        )
    return (
        f"This user has a populated identity vault holding {present} sealed "
        f"item(s). The values are encrypted at rest and are deliberately not "
        f"reproduced in this node, which is indexed for retrieval. No agent can "
        f"read them: user_profile_lookup refuses the pii_vault section at every "
        f"visibility setting."
    )
