import os
import logging
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from mnemonic import Mnemonic

logger = logging.getLogger(__name__)

def load_or_create_wallet(vault_path: Path) -> tuple[ed25519.Ed25519PrivateKey, str]:
    """
    Loads the private key from the vault or generates a new one.
    Returns the private key object and the public key hex string (wallet address).
    """
    key_path = vault_path / "wallet_private_key.pem"
    
    if key_path.exists():
        with open(key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(
                f.read(),
                password=None,
            )
            logger.debug("Loaded existing wallet private key from vault.")
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
        with open(key_path, "wb") as f:
            f.write(pem)
            
        with open(vault_path / "wallet_seed_phrase.txt", "w") as f:
            f.write("KEEP THIS SECRET AND SAFE. DO NOT SHARE.\n\n")
            f.write(seed_phrase + "\n")
            
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
    _private_key, _wallet_address = load_or_create_wallet(vault_path)
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
