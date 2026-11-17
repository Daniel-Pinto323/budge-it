"""Balances for accounts that have no live provider feed (CSV imports), derived from their transactions.

balance = anchor + effect of every transaction dated after the anchor date

The anchor is a balance the user (or a file's Balance column) vouches for as of a date. Spending lowers a
balance and refunds raise it, for cards and checking alike (balances are net-worth contributions, so card
debt is negative). Credit cards also count payments made from other accounts: a Transfer-category
transaction elsewhere whose text contains the card's "payment match" phrase lowers that card's debt. If the
card's own export lists the same payment (same amount within a few days), it is counted once. A payment that
matches several cards, none of which lists it, is left out rather than guessed.
"""

from __future__ import annotations

from datetime import date

from . import db

PAYMENT_WINDOW_DAYS = 5


def _same_payment(a: str, b: str) -> bool:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= PAYMENT_WINDOW_DAYS
    except ValueError:
        return False


def recompute(conn, user_id: int) -> None:
    """Refresh the stored balance (and today's snapshot) of every CSV-backed account for a user."""
    accounts = conn.execute(
        "SELECT a.id, a.kind, a.anchor_cents, a.anchor_date, a.payment_match FROM accounts a "
        "JOIN connections c ON c.id = a.connection_id WHERE a.user_id = ? AND c.provider = 'csv' ORDER BY a.rowid",
        (user_id,)).fetchall()
    if not accounts:
        return
    transfers = conn.execute(
        "SELECT account_id, date, amount_cents, merchant FROM transactions WHERE user_id = ? AND category = 'Transfer'",
        (user_id,)).fetchall()
    since = {a["id"]: a["anchor_date"] or "0000-00-00" for a in accounts}
    totals = {a["id"]: a["anchor_cents"] for a in accounts}
    for a in accounts:
        for row in conn.execute("SELECT amount_cents FROM transactions WHERE account_id = ? AND date > ?", (a["id"], since[a["id"]])):
            totals[a["id"]] -= row["amount_cents"]

    cards = [a for a in accounts if a["kind"] == "credit" and (a["payment_match"] or "").strip()]
    own = {c["id"]: [(t["date"], -t["amount_cents"]) for t in transfers if t["account_id"] == c["id"] and t["amount_cents"] < 0] for c in cards}
    used: dict[str, set[int]] = {c["id"]: set() for c in cards}
    for t in sorted((t for t in transfers if t["amount_cents"] > 0), key=lambda t: t["date"]):
        text = t["merchant"].lower()
        candidates = [c for c in cards if c["id"] != t["account_id"] and c["payment_match"].lower() in text and t["date"] > since[c["id"]]]
        twin = None
        for c in candidates:  # a card whose own export lists this payment already counted it
            index = next((i for i, (day, cents) in enumerate(own[c["id"]])
                          if i not in used[c["id"]] and cents == t["amount_cents"] and _same_payment(day, t["date"])), None)
            if index is not None:
                used[c["id"]].add(index)
                twin = c
                break
        if twin is None and len(candidates) == 1:
            totals[candidates[0]["id"]] += t["amount_cents"]
        # Otherwise it is ambiguous (several cards match and none lists it), so it is left out rather than guessed.

    today, stamp = db.today(), db.now()
    for a in accounts:
        conn.execute("UPDATE accounts SET balance_cents = ?, balance_at = ? WHERE id = ?", (totals[a["id"]], stamp, a["id"]))
        points = [(today, totals[a["id"]])]
        if a["anchor_date"] and a["anchor_date"] < today:
            points.append((a["anchor_date"], a["anchor_cents"]))
        for day, cents in points:
            conn.execute("INSERT INTO balance_snapshots (account_id, day, balance_cents) VALUES (?,?,?) "
                         "ON CONFLICT (account_id, day) DO UPDATE SET balance_cents = excluded.balance_cents",
                         (a["id"], day, cents))
