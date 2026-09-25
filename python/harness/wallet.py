"""
wallet.py - Auto-generated documentation base.
"""

import os
import logging
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from mnemonic import Mnemonic

logger = logging.getLogger(__name__)


def _restrict_permissions(path: Path, mode: int = 0o600) -> None:
    """Best-effort chmod. Self-heals a key file created before this hardening
    existed. No-op on platforms without POSIX permission bits."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _write_secret_bytes(path: Path, data: bytes) -> None:
    """Write key material owner-only. ``open()`` creates files at the
    umask-derived default (commonly 644 -- world-readable), which for a
    wallet private key means any other local user can read it off disk."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    _restrict_permissions(path)


def _load_or_create_wallet(vault_path: Path) -> tuple[ed25519.Ed25519PrivateKey, str]:
    """
    Loads the private key from the vault or generates a new one.
    Returns the private key object and the public key hex string (wallet address).
    """
    key_path = vault_path / "wallet_private_key.pem"
    seed_path = vault_path / "wallet_seed_phrase.txt"

    if key_path.exists():
        with open(key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(
                f.read(),
                password=None,
            )
            logger.debug("Loaded existing wallet private key from vault.")
        _restrict_permissions(key_path)
        if seed_path.exists():
            _restrict_permissions(seed_path)
    else:
        mnemo = Mnemonic("english")
        seed_phrase = mnemo.generate(strength=128) # 12 words
        seed_bytes = mnemo.to_seed(seed_phrase, passphrase="")

        # Ed25519 requires exactly 32 bytes, take the first 32 bytes of BIP39 seed
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed_bytes[:32])

        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        vault_path.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(vault_path, 0o700)
        _write_secret_bytes(key_path, pem)

        _write_secret_bytes(
            seed_path,
            b"KEEP THIS SECRET AND SAFE. DO NOT SHARE.\n\n" + seed_phrase.encode("utf-8") + b"\n",
        )

        logger.info("Generated new wallet private key and saved to vault with seed phrase.")
        
    public_key = private_key.public_key()
    
    # We use the raw bytes hex encoded as the wallet address
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    wallet_address = public_bytes.hex()
    
    return private_key, wallet_address

# Module level singleton initialized by config.py
_private_key = None
_wallet_address = None

def init_wallet(vault_path: Path):
    global _private_key, _wallet_address
    _private_key, _wallet_address = _load_or_create_wallet(vault_path)
    os.environ["TALA_WALLET_ADDRESS"] = _wallet_address
    return _wallet_address

def get_private_key() -> ed25519.Ed25519PrivateKey:
    if not _private_key:
        raise RuntimeError("Wallet not initialized. Call init_wallet() first.")
    return _private_key

def get_wallet_address() -> str:
    if not _wallet_address:
        raise RuntimeError("Wallet not initialized. Call init_wallet() first.")
    return _wallet_address
