"""Manual import of CSV exports while a live provider is unavailable.

Recognised by their column headers (see _rows): Chase card activity, Capital One credit-card downloads
(Debit/Credit columns) and Capital One bank accounts (checking/savings, with a running Balance). The
account type (credit card / bank / investment) is assumed from the schema and can be overridden.

Each imported card becomes a connection with provider = 'csv'. Rows get a stable ID derived from
their contents, so re-importing an overlapping file never double-counts, and categories the user
already changed are left alone.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import db, keywords, ledger
from .store import AppError, category_names, hidden_after_recategorize, keyword_pairs

MAX_ROWS = 5000
# Chase category -> this app's default categories. Unmapped ones stay Uncategorized.
CATEGORY_HINTS = {
    "food & drink": "Dining", "shopping": "Shopping", "groceries": "Groceries", "bills & utilities": "Utilities",
    "gas": "Transportation", "automotive": "Transportation", "travel": "Travel", "entertainment": "Fun money",
    "gifts & donations": "Gifts", "personal": "Fun money",
    # Capital One card categories
    "merchandise": "Shopping", "dining": "Dining", "gas/automotive": "Transportation", "airfare": "Travel",
    "lodging": "Travel", "other travel": "Travel", "phone/cable": "Utilities",
}
IMPORT_KINDS = ("credit", "depository", "investment")
_REQUIRED = ("transaction date", "description", "amount")


def _date(value: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    raise AppError(f"Unrecognised date “{value}”. Expected MM/DD/YYYY.")


def _cents(value: str) -> int:
    text = value.strip().replace("$", "").replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    try:
        number = Decimal(text.strip("()"))
    except InvalidOperation as error:
        raise AppError(f"Unrecognised amount “{value}”.") from error
    cents = int(number * 100)
    return -cents if negative else cents


def _rows(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse either supported export into (kind, rows), using the app's sign convention (positive = money out)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = {(name or "").strip().lower(): name for name in (reader.fieldnames or [])}
    chase = all(column in fields for column in ("transaction date", "description", "amount"))
    capital_one = all(column in fields for column in ("transaction description", "transaction date", "transaction type", "transaction amount"))
    card = all(column in fields for column in ("transaction date", "card no.", "description", "debit", "credit"))
    if not (chase or capital_one or card):
        raise AppError("Unrecognised CSV. Supported: Chase card activity (Transaction Date, Description, Category, Type, "
                       "Amount), Capital One credit cards (Transaction Date, Card No., Description, Category, Debit, Credit) "
                       "and Capital One bank accounts (Transaction Description, Transaction Date, Transaction Type, "
                       "Transaction Amount, Balance).")
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for line in reader:
        get = lambda key: (line.get(fields.get(key, "")) or "").strip()  # noqa: E731
        if not get("transaction date") and not (get("transaction amount") or get("amount") or get("debit") or get("credit")):
            continue
        if len(rows) >= MAX_ROWS:
            raise AppError(f"Files are limited to {MAX_ROWS} transactions.")
        day = _date(get("transaction date"))
        if card:
            description, post, memo, category = get("description"), get("posted date"), get("card no."), get("category") or None
            note = None
            # Debit is a purchase (money out); Credit is a payment or refund (money in).
            amount = (_cents(get("debit")) if get("debit") else 0) - (_cents(get("credit")) if get("credit") else 0)
            transfer, balance = (category or "").lower().startswith("payment"), None
            last4 = re.sub(r"\D", "", memo)[-4:] or None
        elif chase:
            description, post, memo = get("description"), get("post date"), get("memo")
            note = memo or None
            # Chase shows card spending as negative; the app stores spending as positive.
            amount, category = -_cents(get("amount")), get("category") or None
            transfer = get("type").lower() == "payment"
            balance = None
            last4 = None
        else:
            description, post, memo, category = get("transaction description"), "", get("account number"), None
            note = None
            # Capital One amounts are unsigned; the Debit/Credit type gives the direction.
            magnitude = abs(_cents(get("transaction amount")))
            amount = magnitude if get("transaction type").lower() == "debit" else -magnitude
            transfer = False
            balance = _cents(get("balance")) if get("balance") else None
            last4 = re.sub(r"\D", "", memo)[-4:] or None
        description = description[:120] or "Unknown"
        digest = "|".join((day, post, description, str(amount), memo))
        seen[digest] = seen.get(digest, 0) + 1
        rows.append({
            "provider_txn_id": hashlib.sha256(f"{digest}#{seen[digest]}".encode()).hexdigest()[:32],
            "date": day, "merchant": description, "amount_cents": amount, "provider_category": category,
            "transfer": transfer, "balance_cents": balance, "last4": last4, "notes": (note or "")[:500] or None,
        })
    if not rows:
        raise AppError("No transactions found in that file.")
    return ("depository" if capital_one else "credit"), rows


def parse(text: str) -> list[dict[str, Any]]:
    return _rows(text)[1]


def _subtype(kind: str, name: str) -> str:
    if kind == "credit":
        return "credit card"
    if kind == "investment":
        return "brokerage"
    return "savings" if "saving" in name.lower() else "checking"


def import_csv(user_id: int, data: dict[str, Any]) -> dict[str, int]:
    text = data.get("csv")
    if not isinstance(text, str) or not text.strip():
        raise AppError("Choose a CSV file to import.")
    name = re.sub(r"\s+", " ", str(data.get("account") or "")).strip()[:60]
    if not name:
        raise AppError("Account name is required.")
    detected, rows = _rows(text)
    kind = data.get("kind") if data.get("kind") in IMPORT_KINDS else detected  # applies to new accounts; edit it later in Accounts
    mask = re.sub(r"\D", "", str(data.get("mask") or ""))[:4] or next((row["last4"] for row in rows if row["last4"]), None)
    stamp = db.now()
    added = 0
    with db.tx() as conn:
        names = category_names(conn, user_id)
        rules = {row["match"]: row["category"] for row in conn.execute(
            "SELECT match, category FROM category_rules WHERE user_id = ?", (user_id,))}
        item_id = f"csv-{user_id}-{name.lower()}"
        connection = conn.execute("SELECT id FROM connections WHERE provider = 'csv' AND provider_item_id = ?",
                                  (item_id,)).fetchone()
        if connection:
            connection_id = connection["id"]
            conn.execute("UPDATE connections SET last_attempt_at = ?, last_success_at = ? WHERE id = ?",
                         (stamp, stamp, connection_id))
        else:
            connection_id = db.new_id()
            conn.execute(
                "INSERT INTO connections (id, user_id, provider, provider_item_id, institution, last_attempt_at, "
                "last_success_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (connection_id, user_id, "csv", item_id, name, stamp, stamp, stamp))
        account = conn.execute("SELECT id FROM accounts WHERE connection_id = ?", (connection_id,)).fetchone()
        if account:
            account_id = account["id"]
        else:
            account_id = db.new_id()
            # Balance is derived from the account's own transactions (see ledger.py).
            conn.execute(
                "INSERT INTO accounts (id, user_id, connection_id, provider_account_id, name, kind, subtype, mask, "
                "balance_cents, included) VALUES (?,?,?,?,?,?,?,?,0,1)",
                (account_id, user_id, connection_id, "csv", name, kind, _subtype(kind, name), mask))
        keyword_rules = keyword_pairs(conn, user_id)
        recategorized = 0
        for row in rows:
            rule = rules.get(row["merchant"].lower())
            keyword = keywords.match(keyword_rules, row["merchant"])
            hint = CATEGORY_HINTS.get((row["provider_category"] or "").lower())
            if rule in names:
                category, source = rule, "rule"
            elif row["transfer"] and "Transfer" in names:
                category, source = "Transfer", "provider"
            elif keyword in names:
                category, source = keyword, "provider"
            else:
                category, source = (hint if hint in names else "Uncategorized"), "provider"
            existing = conn.execute("SELECT id, category, category_source, notes, hidden FROM transactions WHERE account_id = ? AND provider_txn_id = ?",
                                    (account_id, row["provider_txn_id"])).fetchone()
            if existing:
                if row["notes"] and not existing["notes"]:  # carry a bank memo, never replacing a note already there
                    conn.execute("UPDATE transactions SET notes = ? WHERE id = ?", (row["notes"], existing["id"]))
                # Re-importing refreshes guesses, but never touches a category the user or a rule chose.
                if existing["category_source"] == "provider" and existing["category"] != category:
                    conn.execute("UPDATE transactions SET category = ?, hidden = ?, updated_at = ? WHERE id = ?",
                                 (category, hidden_after_recategorize(existing["category"], category, existing["hidden"]), stamp, existing["id"]))
                    recategorized += 1
                continue
            added += 1
            conn.execute(
                "INSERT INTO transactions (id, user_id, account_id, provider_txn_id, date, merchant, amount_cents, currency, "
                "pending, category, category_source, provider_category, hidden, notes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,'USD',0,?,?,?,?,?,?,?)",
                (db.new_id(), user_id, account_id, row["provider_txn_id"], row["date"], row["merchant"], row["amount_cents"],
                 category, source, row["provider_category"], int(category == "Transfer"), row["notes"], stamp, stamp))
        # Exports that carry a running balance re-anchor the account (newest day wins; an older file never rewinds it).
        dated = [row for row in rows if row["balance_cents"] is not None]
        if dated:
            newest = max(dated, key=lambda row: row["date"])  # stable: first of the newest day = the file's newest row
            anchor = conn.execute("SELECT anchor_date FROM accounts WHERE id = ?", (account_id,)).fetchone()["anchor_date"]
            if anchor is None or newest["date"] >= anchor:
                conn.execute("UPDATE accounts SET anchor_cents = ?, anchor_date = ? WHERE id = ?",
                             (newest["balance_cents"], newest["date"], account_id))
        ledger.recompute(conn, user_id)
    return {"added": added, "skipped": len(rows) - added, "recategorized": recategorized}
