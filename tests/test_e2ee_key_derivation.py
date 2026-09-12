"""
The E2EE key derivation must bind the handshake it came from (P3 review item).

Before this, HKDF ran with a null salt and a static info string, so the
symmetric key depended on the raw ECDH output alone. Both peers' identity
public keys are now folded into the salt: the two sides see the same key pair
in opposite roles, so the derivation sorts them to land on identical inputs.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from python.harness.e2ee_manager import E2EEManager


def _paired_managers():
    alice, bob = E2EEManager(), E2EEManager()
    alice.derive_shared_secret("bob", bob.get_public_key_b64())
    bob.derive_shared_secret("alice", alice.get_public_key_b64())
    return alice, bob


def test_both_sides_derive_the_same_key():
    """Sorting the pubkeys is what makes the derivation symmetric — a message
    encrypted by either side must decrypt on the other."""
    alice, bob = _paired_managers()

    envelope = alice.encrypt("bob", "p2p.receive_message", {"text": "hi"})
    plain = bob.decrypt("alice", envelope["ciphertext"], envelope["nonce"])
    assert plain == {"method": "p2p.receive_message", "params": {"text": "hi"}}

    # And in the other direction, since the roles are reversed.
    envelope = bob.encrypt("alice", "p2p.receive_message", {"text": "yo"})
    plain = alice.decrypt("bob", envelope["ciphertext"], envelope["nonce"])
    assert plain["params"] == {"text": "yo"}


def test_a_third_party_key_does_not_decrypt():
    alice, bob = _paired_managers()
    mallory = E2EEManager()
    mallory.derive_shared_secret("alice", alice.get_public_key_b64())

    envelope = alice.encrypt("bob", "p2p.receive_message", {"text": "secret"})
    try:
        mallory.decrypt("alice", envelope["ciphertext"], envelope["nonce"])
    except ValueError:
        pass
    else:
        raise AssertionError("a non-participant decrypted the payload")
