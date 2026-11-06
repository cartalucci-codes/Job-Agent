"""Encrypt secrets (such as the Gmail refresh token) before they touch the database."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .security import read_or_create


def _fernet() -> Fernet:
    key = read_or_create(".key", lambda: Fernet.generate_key().decode())
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None
