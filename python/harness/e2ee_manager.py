"""
e2ee_manager.py - Auto-generated documentation base.
"""

import base64
import hashlib
import json
import logging
import os
from typing import Any, Dict

from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization

logger = logging.getLogger(__name__)

class E2EEManager:
    def __init__(self):
        # Generate the local node's long-term identity key for this session
        self.private_key = x25519.X25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        
        # Maps peer_username -> AESGCM cipher instance
        self.peer_ciphers: Dict[str, AESGCM] = {}

    def get_public_key_b64(self) -> str:
        """Returns the local public key encoded in Base64 for handshakes."""
        pub_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        return base64.b64encode(pub_bytes).decode('utf-8')

    def derive_shared_secret(self, peer_username: str, peer_pub_b64: str) -> None:
        """Computes the shared ECDH secret from the peer's public key and initializes AES-GCM."""
        try:
            peer_pub_bytes = base64.b64decode(peer_pub_b64)
            peer_public_key = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            
            # Perform ECDH key exchange
            shared_key = self.private_key.exchange(peer_public_key)

            # Bind both handshake public keys into the derivation. Identity
            # keys are regenerated every process start, so this doubles as a
            # per-session salt. Sorted, because the two sides see the same
            # pair in opposite roles and must land on identical HKDF inputs.
            # The v2 label means a pre-v2 peer derives a different key and
            # fails to decrypt loudly instead of interoperating silently.
            own_pub_bytes = self.public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            first, second = sorted((own_pub_bytes, peer_pub_bytes))
            derived_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=hashlib.sha256(b'tala-e2ee-p2p-v2' + first + second).digest(),
                info=b'tala-e2ee-p2p-v2'
            ).derive(shared_key)
            
            # Initialize the AES-GCM cipher for this peer
            self.peer_ciphers[peer_username] = AESGCM(derived_key)
            logger.info("E2EE shared secret established with %s", peer_username)
            
        except Exception as e:
            logger.error("Failed to derive E2EE shared secret for %s: %s", peer_username, e)

    def encrypt(self, peer_username: str, method: str, params: dict) -> Dict[str, Any]:
        """Encrypts a JSON-RPC payload using AES-GCM."""
        cipher = self.peer_ciphers.get(peer_username)
        if not cipher:
            raise ValueError(f"No E2EE shared secret established with {peer_username}")
            
        nonce = os.urandom(12)
        payload_bytes = json.dumps({"method": method, "params": params}).encode('utf-8')
        
        ciphertext = cipher.encrypt(nonce, payload_bytes, associated_data=None)
        
        return {
            "ciphertext": base64.b64encode(ciphertext).decode('utf-8'),
            "nonce": base64.b64encode(nonce).decode('utf-8')
        }

    def decrypt(self, peer_username: str, ciphertext_b64: str, nonce_b64: str) -> Dict[str, Any]:
        """Decrypts a JSON-RPC payload using AES-GCM."""
        cipher = self.peer_ciphers.get(peer_username)
        if not cipher:
            raise ValueError(f"No E2EE shared secret established with {peer_username}")
            
        try:
            nonce = base64.b64decode(nonce_b64)
            ciphertext = base64.b64decode(ciphertext_b64)
            
            plaintext_bytes = cipher.decrypt(nonce, ciphertext, associated_data=None)
            return json.loads(plaintext_bytes.decode('utf-8'))
        except Exception as e:
            raise ValueError(f"Failed to decrypt E2EE payload from {peer_username}: {e}")

e2ee_manager = E2EEManager()
