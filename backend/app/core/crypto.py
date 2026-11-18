"""Application-level encryption for secrets the platform must read back (webhook signing secrets).

AES-256-GCM with a random 96-bit nonce; the stored value is ``v1:<nonce hex>:<ciphertext hex>``. The key
comes from ``DATA_ENCRYPTION_KEY``. Without it (development only, reported as a development adapter and
refused in production) a key is derived with HKDF-SHA256 from the database secret, so it is stable per
deployment and never written anywhere.
"""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import Settings

PREFIX = "v1"


@lru_cache(maxsize=4)
def _key(configured: str | None, fallback_material: str) -> bytes:
    if configured:
        return bytes.fromhex(configured)
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"claimos-dev-data-key", info=b"claimos:v1")
    return hkdf.derive(fallback_material.encode())


def key_for(settings: Settings) -> bytes:
    configured = settings.data_encryption_key.get_secret_value() if settings.data_encryption_key else None
    return _key(configured, settings.database_url.get_secret_value())


def encrypt(settings: Settings, plaintext: str, *, context: str) -> str:
    """``context`` (e.g. the row id) is bound as associated data: a value cannot be moved to another row."""
    nonce = os.urandom(12)
    ciphertext = AESGCM(key_for(settings)).encrypt(nonce, plaintext.encode(), context.encode())
    return f"{PREFIX}:{nonce.hex()}:{ciphertext.hex()}"


def decrypt(settings: Settings, stored: str, *, context: str) -> str:
    version, nonce, ciphertext = stored.split(":", 2)
    if version != PREFIX:
        raise ValueError("unknown ciphertext version")
    aead = AESGCM(key_for(settings))
    return aead.decrypt(bytes.fromhex(nonce), bytes.fromhex(ciphertext), context.encode()).decode()
