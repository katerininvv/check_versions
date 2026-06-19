"""Шифрование секретов в состоянии покоя (AES-256-GCM).

Ключ шифрования детерминированно выводится из мастер-ключа SECRET_KEY
(SHA-256 -> 32 байта). Мастер-ключ в БД не хранится.
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

_PREFIX = "enc:v1:"


def _key() -> bytes:
    return hashlib.sha256(config.effective_secret_key().encode("utf-8")).digest()


def encrypt(plaintext: str | None) -> str:
    """Шифрует строку. Пустые/None возвращаются как пустая строка."""
    if not plaintext:
        return ""
    aes = AESGCM(_key())
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    blob = base64.b64encode(nonce + ct).decode("ascii")
    return _PREFIX + blob


def decrypt(token: str | None) -> str:
    """Расшифровывает строку, созданную encrypt(). Безопасна к пустым значениям."""
    if not token:
        return ""
    if not token.startswith(_PREFIX):
        # Значение не зашифровано (например, ранее сохранённое в открытом виде) —
        # возвращаем как есть, чтобы не терять данные.
        return token
    raw = base64.b64decode(token[len(_PREFIX):])
    nonce, ct = raw[:12], raw[12:]
    aes = AESGCM(_key())
    return aes.decrypt(nonce, ct, None).decode("utf-8")


def mask(value: str | None) -> str:
    """Маскирует секрет для отображения в UI (показывает только хвост)."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * 6 + value[-4:]
