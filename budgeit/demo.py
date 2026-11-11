"""Optional synthetic sample data, clearly labelled as such in the UI.

Stored as connections with provider = 'demo', so removing it is one cascade delete
and it can never be mistaken for, or synced as, a real institution.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import db
from .store import AppError, category_names

_ACCOUNTS = [
    # institution, name, kind, subtype, mask, balance_cents
    ("Sample Bank", "Everyday Checking", "depository", "checking", "1842", 862000),
    ("Sample Bank", "Rewards Card", "credit", "credit card", "2091", -101300),
    ("Sample Credit Union", "Cash Back Card", "credit", "credit card", "5022", -35500),
    ("Sample Credit Union", "High-Yield Savings", "depository", "savings", "7914", 5330000),
]
_MONTHLY = [
    # day of month, merchant, category, account index, amount_cents (positive = spending)
    (1, "Maple Street Apartments", "Housing", 0, 145000),
    (2, "Payroll deposit", "Income", 0, -240000),
    (3, "Shell", "Transportation", 1, 3810),
    (4, "Target", "Shopping", 2, 5214),
    (5, "Delta Air Lines", "Travel", 1, 4900),
    (6, "Netflix", "Subscriptions", 2, 1549),
    (7, "Whole Foods Market", "Groceries", 0, 11264),
    (8, "The Corner Table", "Dining", 1, 4860),
    (9, "Uber", "Transportation", 1, 1643),
    (9, "City Power & Light", "Utilities", 0, 7200),
    (10, "Trader Joe's", "Groceries", 0, 6428),
    (12, "Card payment", "Transfer", 0, 60000),
    (14, "Blue Bottle Coffee", "Dining", 1, 1175),
    (16, "Payroll deposit", "Income", 0, -240000),
    (18, "Trader Joe's", "Groceries", 0, 8842),
    (20, "Spotify", "Subscriptions", 2, 1199),
    (22, "Thai Basil", "Dining", 1, 6230),
    (24, "Amazon", "Shopping", 2, 3899),
    (26, "Target refund", "Shopping", 2, -1500),
]


def has_demo(user_id: int) -> bool:
    return bool(db.connect().execute(
        "SELECT 1 FROM connections WHERE user_id = ? AND provider = 'demo'", (user_id,)).fetchone())


def load(user_id: int) -> None:
    if has_demo(user_id):
        raise AppError("Sample data is already loaded.", 409)
    today = date.today()
    stamp = db.now()
    with db.tx() as conn:
        names = category_names(conn, user_id)
        connections: dict[str, str] = {}
        account_ids: list[str] = []
        for institution, name, kind, subtype, mask, balance in _ACCOUNTS:
            if institution not in connections:
                connections[institution] = db.new_id()
                conn.execute(
                    "INSERT INTO connections (id, user_id, provider, provider_item_id, institution, last_attempt_at, "
                    "last_success_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (connections[institution], user_id, "demo", f"demo-{user_id}-{institution}", institution, stamp, stamp, stamp))
            account_id = db.new_id()
            account_ids.append(account_id)
            conn.execute(
                "INSERT INTO accounts (id, user_id, connection_id, provider_account_id, name, kind, subtype, mask, "
                "balance_cents, balance_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (account_id, user_id, connections[institution], f"demo-{mask}", name, kind, subtype, mask, balance, stamp))
            for months_back in range(6):
                # A gentle upward drift so the net-worth chart has a shape.
                day = today if months_back == 0 else (today.replace(day=1) - timedelta(days=30 * months_back - 14))
                drift = int(balance * 0.025 * months_back) if balance > 0 else int(balance * -0.04 * months_back)
                conn.execute("INSERT OR REPLACE INTO balance_snapshots (account_id, day, balance_cents) VALUES (?,?,?)",
                             (account_id, day.isoformat(), balance - drift))
        first = today.replace(day=1)
        for months_back in range(3):
            month_start = first
            for _ in range(months_back):
                month_start = (month_start - timedelta(days=1)).replace(day=1)
            for index, (day_of_month, merchant, category, account, amount) in enumerate(_MONTHLY):
                when = month_start + timedelta(days=day_of_month - 1)
                if when > today:
                    continue
                pending = int(when >= today - timedelta(days=1) and amount > 0)
                conn.execute(
                    "INSERT INTO transactions (id, user_id, account_id, provider_txn_id, date, merchant, amount_cents, pending, "
                    "category, category_source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,'provider',?,?)",
                    (db.new_id(), user_id, account_ids[account], f"demo-{months_back}-{index}", when.isoformat(), merchant,
                     amount, pending, category if category in names else "Uncategorized", stamp, stamp))


def remove(user_id: int) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM connections WHERE user_id = ? AND provider = 'demo'", (user_id,))
