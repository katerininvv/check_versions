"""Хэширование паролей (Argon2id) и простой троттлинг попыток входа."""
import time
import threading

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

from . import config

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


# --- Простой in-memory троттлинг по IP --------------------------------------
_attempts: dict[str, list[float]] = {}
_lock = threading.Lock()


def is_locked(ip: str) -> bool:
    now = time.time()
    with _lock:
        hits = [t for t in _attempts.get(ip, []) if now - t < config.LOGIN_LOCKOUT_SECONDS]
        _attempts[ip] = hits
        return len(hits) >= config.LOGIN_MAX_ATTEMPTS


def register_failure(ip: str) -> None:
    with _lock:
        _attempts.setdefault(ip, []).append(time.time())


def reset_attempts(ip: str) -> None:
    with _lock:
        _attempts.pop(ip, None)
