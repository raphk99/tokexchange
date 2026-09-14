"""Optional end-to-end sealing of bundles so the coordinator only stores ciphertext.

A shared secret configured on both laptops derives an AES-256-GCM key
(PBKDF2-HMAC-SHA256, per-message salt). The coordinator never has the secret.
AES-GCM comes from the optional ``cryptography`` package; without it only the
``NullSealer`` is available and bundles travel in clear over TLS.

Wire format: ``b"TXE1" + salt(16) + nonce(12) + ciphertext``.
"""
from __future__ import annotations

import hashlib
import os

MAGIC = b"TXE1"
_PBKDF2_ROUNDS = 200_000


class SealError(RuntimeError):
    pass


class Sealer:
    name = "none"

    def seal(self, data: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def open(self, data: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError


class NullSealer(Sealer):
    name = "none"

    def seal(self, data: bytes) -> bytes:
        return data

    def open(self, data: bytes) -> bytes:
        if data.startswith(MAGIC):
            raise SealError("payload is encrypted but no shared secret is configured")
        return data


class AesGcmSealer(Sealer):
    name = "aes-256-gcm"

    def __init__(self, secret: str):
        if not secret:
            raise SealError("empty secret")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise SealError("end-to-end encryption needs the 'cryptography' package: pip install 'tokexchange[crypto]'") from exc
        self._secret = secret.encode("utf-8")

    def _key(self, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", self._secret, salt, _PBKDF2_ROUNDS, dklen=32)

    def seal(self, data: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt, nonce = os.urandom(16), os.urandom(12)
        ct = AESGCM(self._key(salt)).encrypt(nonce, data, MAGIC)
        return MAGIC + salt + nonce + ct

    def open(self, data: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
        if not data.startswith(MAGIC):
            raise SealError("payload is not encrypted but a shared secret is configured; refusing to accept plaintext")
        salt, nonce, ct = data[4:20], data[20:32], data[32:]
        try:
            return AESGCM(self._key(salt)).decrypt(nonce, ct, MAGIC)
        except InvalidTag as exc:
            raise SealError("decryption failed: wrong secret or corrupted payload") from exc


def sealer_for(secret: str | None) -> Sealer:
    return AesGcmSealer(secret) if secret else NullSealer()
