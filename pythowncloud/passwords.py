"""
Password hashing and verification using stdlib hashlib.scrypt.
Stored format: scrypt:n=16384,r=8,p=1:<hex_salt>:<hex_hash>
"""

import hashlib
import os


_PARAMS = {"n": 16384, "r": 8, "p": 1}
_PREFIX = "scrypt:n=16384,r=8,p=1"


def hash_password(plain: str) -> str:
    """Hash a plaintext password and return a self-contained string."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(plain.encode(), salt=salt, **_PARAMS)
    return f"{_PREFIX}:{salt.hex()}:{dk.hex()}"


def verify_password(plain: str, stored_hash: str) -> bool:
    """Return True if plain matches the stored scrypt hash string."""
    if not stored_hash.startswith("scrypt:"):
        return False
    try:
        _, params, salt_hex, hash_hex = stored_hash.split(":", 3)
        kv = dict(p.split("=") for p in params.split(","))
        n, r, p = int(kv["n"]), int(kv["r"]), int(kv["p"])
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(plain.encode(), salt=salt, n=n, r=r, p=p)
        return dk == expected
    except Exception:
        return False
