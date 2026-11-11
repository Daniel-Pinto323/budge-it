"""Password hashing, sessions, provider-token encryption, and login rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken

from . import db

SESSION_DAYS = 14
MIN_PASSWORD_LENGTH = 12
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}
_fernet: Fernet | None = None


def init_encryption(key: str) -> None:
    global _fernet
    try:
        _fernet = Fernet(key.encode())
    except (ValueError, TypeError) as error:
        raise SystemExit(
            "BUDGEIT_ENCRYPTION_KEY is missing or invalid. Generate one with "
            "`python3 server.py generate-key` and keep it outside source control."
        ) from error


def encrypt(value: str) -> str:
    assert _fernet is not None
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    assert _fernet is not None
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken as error:
        raise RuntimeError("Stored credential cannot be decrypted with the current encryption key.") from error


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=2**27, dklen=32, **_SCRYPT)
    encode = lambda raw: base64.b64encode(raw).decode()
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${encode(salt)}${encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
            maxmem=2**27, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the email is unknown, so timing does not reveal which emails exist.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_login(email: str, password: str):
    row = db.connect().execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    valid = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    return row if row and valid else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf, created_at, expires_at) VALUES (?,?,?,?,?)",
            (_token_hash(token), user_id, csrf, db.now(), expires.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
    return token, csrf


def find_session(token: str):
    if not token:
        return None
    return db.connect().execute(
        "SELECT s.csrf, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (_token_hash(token), db.now()),
    ).fetchone()


def delete_session(token: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def purge_expired_sessions() -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (db.now(),))


class RateLimiter:
    """Fixed-window failure counter kept in memory (single-process server)."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        cutoff = time.monotonic() - self.window
        with self._lock:
            hits = [stamp for stamp in self._hits.get(key, []) if stamp > cutoff]
            if hits:
                self._hits[key] = hits
            else:
                self._hits.pop(key, None)
            return len(hits) >= self.limit

    def record(self, key: str) -> None:
        with self._lock:
            if len(self._hits) > 10_000:
                self._hits.clear()
            self._hits.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
