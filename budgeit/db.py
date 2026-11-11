"""SQLite storage. One connection per thread; every write runs in a transaction.

Money is stored as integer minor units (cents). Transaction amounts follow one
convention everywhere: positive = money out (spending), negative = money in.
Account balances are stored as their net-worth contribution, so debts are negative.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    mx_user_guid TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE connections (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,              -- plaid | mx | demo
    provider_item_id TEXT NOT NULL,      -- Plaid item_id / MX member guid
    institution TEXT NOT NULL,
    institution_id TEXT,
    secret_enc TEXT,                     -- encrypted Plaid access token
    status TEXT NOT NULL DEFAULT 'healthy',  -- healthy | needs_reauth | error
    error TEXT,
    cursor TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (provider, provider_item_id)
);
CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    provider_account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- depository | credit | loan | investment | other
    subtype TEXT,
    mask TEXT,
    currency TEXT NOT NULL DEFAULT 'USD',
    balance_cents INTEGER NOT NULL DEFAULT 0,
    available_cents INTEGER,
    balance_at TEXT,
    included INTEGER NOT NULL DEFAULT 1,
    UNIQUE (connection_id, provider_account_id)
);
CREATE TABLE transactions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    provider_txn_id TEXT,
    date TEXT NOT NULL,                  -- YYYY-MM-DD
    merchant TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    pending INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'Uncategorized',
    category_source TEXT NOT NULL DEFAULT 'provider',  -- provider | rule | user | manual
    provider_category TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    manual INTEGER NOT NULL DEFAULT 0,
    account_label TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, provider_txn_id)
);
CREATE INDEX transactions_user_date ON transactions (user_id, date DESC);
CREATE TABLE categories (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    grp TEXT NOT NULL,
    color TEXT NOT NULL,
    budget_cents INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, name)
);
CREATE TABLE category_rules (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    match TEXT NOT NULL,                 -- lower-cased merchant name, exact match
    category TEXT NOT NULL,
    UNIQUE (user_id, match)
);
CREATE TABLE balance_snapshots (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    balance_cents INTEGER NOT NULL,
    PRIMARY KEY (account_id, day)
);
CREATE TABLE webhook_events (
    dedupe_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""

_local = threading.local()
_path: Path | None = None


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def new_id() -> str:
    return uuid4().hex


def init(path: Path) -> None:
    global _path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _path = path
    _local.__dict__.pop("conn", None)
    conn = connect()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        raise SystemExit(f"Database schema version {version} is not supported by this build.")
    path.chmod(0o600)


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        if _path is None:
            raise RuntimeError("Database is not initialised.")
        conn = sqlite3.connect(_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        _local.conn = conn
    return conn


def close() -> None:
    conn = _local.__dict__.pop("conn", None)
    if conn is not None:
        conn.close()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
