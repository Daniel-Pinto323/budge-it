"""Balances for accounts that have no live provider feed (CSV imports), derived from their own transactions.

balance = anchor + effect of every transaction dated after the anchor date

The anchor is a balance the user (or a file's Balance column) vouches for as of a date. Spending lowers a
balance and refunds raise it, for cards and checking alike (balances are net-worth contributions, so card
debt is negative). A card's payments appear in its own export (for example Chase's "Payment" rows), so
each account is computed from its own file only; nothing is inferred from other accounts.
"""

from __future__ import annotations

from . import db


def recompute(conn, user_id: int) -> None:
    """Refresh the stored balance (and today's snapshot) of every CSV-backed account for a user."""
    accounts = conn.execute(
        "SELECT a.id, a.anchor_cents, a.anchor_date FROM accounts a "
        "JOIN connections c ON c.id = a.connection_id WHERE a.user_id = ? AND c.provider = 'csv'", (user_id,)).fetchall()
    today, stamp = db.today(), db.now()
    for a in accounts:
        total = a["anchor_cents"]
        for row in conn.execute("SELECT amount_cents FROM transactions WHERE account_id = ? AND date > ?",
                                (a["id"], a["anchor_date"] or "0000-00-00")):
            total -= row["amount_cents"]
        conn.execute("UPDATE accounts SET balance_cents = ?, balance_at = ? WHERE id = ?", (total, stamp, a["id"]))
        points = [(today, total)]
        if a["anchor_date"] and a["anchor_date"] < today:
            points.append((a["anchor_date"], a["anchor_cents"]))
        for day, cents in points:
            conn.execute("INSERT INTO balance_snapshots (account_id, day, balance_cents) VALUES (?,?,?) "
                         "ON CONFLICT (account_id, day) DO UPDATE SET balance_cents = excluded.balance_cents",
                         (a["id"], day, cents))
