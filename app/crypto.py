"""Шифрование секретов (токенов) в БД с использованием Fernet (AES-128-CBC + HMAC)."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_fernet = Fernet(settings.app_secret.encode())


def encrypt(plain: str | None) -> str | None:
    """Зашифровать строку. None → None (пустое значение не шифруем)."""
    if plain is None or plain == "":
        return None
    return _fernet.encrypt(plain.encode()).decode()


def decrypt(token: str | None) -> str | None:
    """Расшифровать. None → None. Плохой токен → None."""
    if token is None or token == "":
        return None
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return None
