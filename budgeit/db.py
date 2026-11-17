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

SCHEMA_VERSION = 4

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
    anchor_cents INTEGER NOT NULL DEFAULT 0,  -- CSV accounts: a known balance ...
    anchor_date TEXT,                         -- ... as of this day (NULL = count every transaction)
    payment_match TEXT,                       -- CSV cards: text identifying payments made from other accounts
    kind_override TEXT,                       -- user-chosen account type for provider accounts (sync rewrites kind)
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
CREATE TABLE keyword_rules (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,               -- lower-cased; matches anywhere in the merchant text
    category TEXT NOT NULL,
    UNIQUE (user_id, keyword)
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
    elif version in (1, 2, 3):
        if version == 1:
            _migrate_1_to_2(conn)
        if version <= 2:
            _migrate_2_to_3(conn)
        _migrate_3_to_4(conn)
    elif version != SCHEMA_VERSION:
        raise SystemExit(f"Database schema version {version} is not supported by this build.")
    path.chmod(0o600)


def seed_keyword_rules(conn: sqlite3.Connection, user_id: int) -> None:
    """Give a user the default keyword rules, creating the Zelle category if they lack one."""
    from . import keywords
    if not conn.execute("SELECT 1 FROM categories WHERE user_id = ? AND name = 'Zelle'", (user_id,)).fetchone():
        position = conn.execute("SELECT COUNT(*) FROM categories WHERE user_id = ?", (user_id,)).fetchone()[0]
        conn.execute("INSERT INTO categories (id, user_id, name, grp, color, budget_cents, position) VALUES (?,?,?,?,?,0,?)",
                     (new_id(), user_id, "Zelle", "Everyday", "#7a6fb0", position))
    names = {row[0] for row in conn.execute("SELECT name FROM categories WHERE user_id = ?", (user_id,))}
    for category, words in keywords.DEFAULTS.items():
        if category in names or category in ("Transfer", "Income"):
            for word in words:
                conn.execute("INSERT OR IGNORE INTO keyword_rules (id, user_id, keyword, category) VALUES (?,?,?,?)",
                             (new_id(), user_id, word, category))


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE keyword_rules (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            keyword TEXT NOT NULL,
            category TEXT NOT NULL,
            UNIQUE (user_id, keyword))""")
        for row in conn.execute("SELECT id FROM users").fetchall():
            seed_keyword_rules(conn, row[0])
        conn.execute("PRAGMA user_version = 2")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    from . import ledger
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN anchor_cents INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE accounts ADD COLUMN anchor_date TEXT")
        conn.execute("ALTER TABLE accounts ADD COLUMN payment_match TEXT")
        for row in conn.execute("SELECT a.id, a.kind, a.name, a.balance_cents FROM accounts a "
                                "JOIN connections c ON c.id = a.connection_id WHERE c.provider = 'csv'").fetchall():
            if row["kind"] == "depository":
                # Their balance came from the file's Balance column, as of the newest imported day.
                conn.execute("UPDATE accounts SET anchor_cents = ?, anchor_date = "
                             "(SELECT MAX(date) FROM transactions WHERE account_id = accounts.id) WHERE id = ?",
                             (row["balance_cents"], row["id"]))
            elif row["kind"] == "credit" and len(row["name"].split()[0]) >= 3:
                conn.execute("UPDATE accounts SET payment_match = ? WHERE id = ?", (row["name"].split()[0].lower(), row["id"]))
        for user in conn.execute("SELECT id FROM users").fetchall():
            ledger.recompute(conn, user["id"])
        conn.execute("PRAGMA user_version = 3")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    from . import keywords
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN kind_override TEXT")
        for user in conn.execute("SELECT id FROM users").fetchall():
            names = {row[0] for row in conn.execute("SELECT name FROM categories WHERE user_id = ?", (user["id"],))}
            for category, words in keywords.ADDED_IN_SCHEMA_4.items():
                if category in names or category in ("Transfer", "Income"):
                    for word in words:
                        conn.execute("INSERT OR IGNORE INTO keyword_rules (id, user_id, keyword, category) VALUES (?,?,?,?)",
                                     (new_id(), user["id"], word, category))
        conn.execute("PRAGMA user_version = 4")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


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
