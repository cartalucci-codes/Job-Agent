"""Single-user access control: one secret access token, kept on disk."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from .config import DATA_DIR, ensure_data_dir

COOKIE = "jobagent_session"


def read_or_create(name: str, maker) -> str:
    """Read a secret file under DATA_DIR, creating it (mode 0600) on first use."""
    ensure_data_dir()
    path = DATA_DIR / name
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(maker())
        except FileExistsError:
            pass
    return path.read_text().strip()


def access_token() -> str:
    return read_or_create(".access_token", lambda: secrets.token_urlsafe(24))


def _secret() -> str:
    return read_or_create(".session_secret", lambda: secrets.token_hex(32))


def session_value() -> str:
    return hmac.new(_secret().encode(), b"jobagent-session", hashlib.sha256).hexdigest()


def is_authed(cookie: str | None) -> bool:
    return bool(cookie) and hmac.compare_digest(cookie, session_value())


def check_token(candidate: str) -> bool:
    return hmac.compare_digest(candidate.strip().encode(), access_token().encode())
