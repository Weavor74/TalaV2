"""
crypto.py - Auto-generated documentation base.
"""

import os
import base64
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

class CryptoManager:
    def __init__(self, key_path: str | None = None):
        if not key_path:
            # Default to ~/.tala/identity/ledger_key.pem
            key_path = Path.home() / ".tala" / "identity" / "ledger_key.pem"
        else:
            key_path = Path(key_path)

        self.key_path = key_path
        self._private_key = self._load_or_generate_key()
        
    def _load_or_generate_key(self) -> ed25519.Ed25519PrivateKey:
        if self.key_path.exists():
            with open(self.key_path, "rb") as key_file:
                return serialization.load_pem_private_key(
                    key_file.read(),
                    password=None,
                )
        else:
            # Generate new key
            private_key = ed25519.Ed25519PrivateKey.generate()
            
            # Save it
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.key_path, "wb") as key_file:
                key_file.write(private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                ))
            return private_key

    def sign(self, payload: str) -> str:
        """Sign a UTF-8 string payload and return the signature as a hex string."""
        signature = self._private_key.sign(payload.encode("utf-8"))
        return signature.hex()
        
    def get_public_key_hex(self) -> str:
        """Return the public key as a hex string."""
        pub_bytes = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        return pub_bytes.hex()

    @staticmethod
    def verify_signature(public_key_hex: str, payload: str, signature_hex: str) -> bool:
        """Verify a signature using a public key hex string."""
        try:
            pub_bytes = bytes.fromhex(public_key_hex)
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
            signature_bytes = bytes.fromhex(signature_hex)
            public_key.verify(signature_bytes, payload.encode("utf-8"))
            return True
        except Exception:
            return False
