"""User-scoped application data: categories, transactions, accounts, rules, net worth."""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Any

from . import db, security

SPECIAL_CATEGORIES = ("Uncategorized", "Income", "Transfer")
COLORS = ["#3d9770", "#5f86ad", "#d3735f", "#c59b48", "#8c77a3", "#608f99", "#4c94a5", "#d29272", "#438260", "#b17d89"]
DEFAULT_CATEGORIES = [
    ("Essentials", "Housing", "#758d7b", 145000),
    ("Essentials", "Groceries", "#3d9770", 60000),
    ("Essentials", "Utilities", "#5f86ad", 26000),
    ("Everyday", "Dining", "#d3735f", 25000),
    ("Everyday", "Transportation", "#c59b48", 18000),
    ("Everyday", "Shopping", "#8c77a3", 26000),
    ("Everyday", "Subscriptions", "#608f99", 8000),
    ("Goals & fun", "Travel", "#4c94a5", 30000),
    ("Goals & fun", "Fun money", "#d29272", 22000),
    ("Goals & fun", "Gifts", "#b17d89", 20000),
]
MAX_TRANSACTIONS = 5000
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AppError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _text(value: Any, field: str, limit: int, required: bool = True) -> str:
    text = str(value if value is not None else "").strip()
    if required and not text:
        raise AppError(f"{field} is required.")
    if len(text) > limit:
        raise AppError(f"{field} must be {limit} characters or fewer.")
    return text


def _cents(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 10**11:
        raise AppError(f"{field} must be a whole number of cents.")
    return value


# --- Users ---------------------------------------------------------------------

def create_user(email: str, password: str, name: str = "") -> int:
    email = email.strip().lower()
    if not _EMAIL.match(email):
        raise AppError("Enter a valid email address.")
    if len(password) < security.MIN_PASSWORD_LENGTH:
        raise AppError(f"Password must be at least {security.MIN_PASSWORD_LENGTH} characters.")
    name = name.strip() or email.split("@")[0].capitalize()
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise AppError("A user with that email already exists.", 409)
        user_id = conn.execute(
            "INSERT INTO users (email, name, password_hash, created_at) VALUES (?,?,?,?)",
            (email, name, security.hash_password(password), db.now()),
        ).lastrowid
        _seed_categories(conn, user_id)
    return user_id


def set_password(email: str, password: str) -> None:
    if len(password) < security.MIN_PASSWORD_LENGTH:
        raise AppError(f"Password must be at least {security.MIN_PASSWORD_LENGTH} characters.")
    with db.tx() as conn:
        user = conn.execute("SELECT id FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
        if not user:
            raise AppError("No user with that email.", 404)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (security.hash_password(password), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))


def user_count() -> int:
    return db.connect().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _seed_categories(conn, user_id: int) -> None:
    for position, (group, name, color, budget) in enumerate(DEFAULT_CATEGORIES):
        conn.execute(
            "INSERT INTO categories (id, user_id, name, grp, color, budget_cents, position) VALUES (?,?,?,?,?,?,?)",
            (db.new_id(), user_id, name, group, color, budget, position),
        )


# --- Read model ----------------------------------------------------------------

def category_names(conn, user_id: int) -> set[str]:
    rows = conn.execute("SELECT name FROM categories WHERE user_id = ?", (user_id,)).fetchall()
    return {row["name"] for row in rows} | set(SPECIAL_CATEGORIES)


def _account_label(row) -> str:
    return f"{row['account_name']} ••{row['mask']}" if row["mask"] else row["account_name"]


def state(user_id: int) -> dict[str, Any]:
    conn = db.connect()
    categories = conn.execute(
        "SELECT id, name, grp, color, budget_cents FROM categories WHERE user_id = ? ORDER BY position, name", (user_id,)
    ).fetchall()
    connections = conn.execute(
        "SELECT id, provider, institution, status, error, last_attempt_at, last_success_at "
        "FROM connections WHERE user_id = ? ORDER BY created_at", (user_id,)
    ).fetchall()
    accounts = conn.execute(
        "SELECT id, connection_id, name, kind, subtype, mask, currency, balance_cents, available_cents, balance_at, included "
        "FROM accounts WHERE user_id = ? ORDER BY kind, name", (user_id,)
    ).fetchall()
    transactions = conn.execute(
        "SELECT t.*, a.name AS account_name, a.mask AS mask, c.provider AS provider FROM transactions t "
        "LEFT JOIN accounts a ON a.id = t.account_id LEFT JOIN connections c ON c.id = a.connection_id "
        "WHERE t.user_id = ? ORDER BY t.date DESC, t.created_at DESC LIMIT ?", (user_id, MAX_TRANSACTIONS)
    ).fetchall()
    rules = conn.execute("SELECT id, match, category FROM category_rules WHERE user_id = ? ORDER BY match", (user_id,)).fetchall()
    return {
        "categories": [{"id": c["id"], "name": c["name"], "group": c["grp"], "color": c["color"], "budget": c["budget_cents"]}
                       for c in categories],
        "connections": [dict(row) for row in connections],
        "accounts": [{**dict(a), "included": bool(a["included"])} for a in accounts],
        "transactions": [{
            "id": t["id"], "date": t["date"], "merchant": t["merchant"], "amount": t["amount_cents"],
            "pending": bool(t["pending"]), "category": t["category"], "hidden": bool(t["hidden"]),
            "manual": bool(t["manual"]), "provider": "manual" if t["manual"] else t["provider"],
            "account": (t["account_label"] or "Manual entry") if t["manual"] or not t["account_name"] else _account_label(t),
        } for t in transactions],
        "rules": [dict(row) for row in rules],
        "net_worth_history": net_worth_history(conn, user_id),
        "special_categories": list(SPECIAL_CATEGORIES),
    }


def net_worth_history(conn, user_id: int) -> list[dict[str, Any]]:
    """Daily net worth from balance snapshots of included accounts, carrying balances forward."""
    rows = conn.execute(
        "SELECT s.account_id, s.day, s.balance_cents FROM balance_snapshots s JOIN accounts a ON a.id = s.account_id "
        "WHERE a.user_id = ? AND a.included = 1 ORDER BY s.day", (user_id,)
    ).fetchall()
    latest: dict[str, int] = {}
    history: list[dict[str, Any]] = []
    for row in rows:
        latest[row["account_id"]] = row["balance_cents"]
        total = sum(latest.values())
        if history and history[-1]["day"] == row["day"]:
            history[-1]["total"] = total
        else:
            history.append({"day": row["day"], "total": total})
    return history[-400:]


# --- Categories ----------------------------------------------------------------

def _category_fields(conn, user_id: int, data: dict[str, Any], exclude_id: str | None = None) -> tuple[str, str, int]:
    name = _text(data.get("name"), "Category name", 30)
    group = _text(data.get("group"), "Group", 30)
    budget = _cents(data.get("budget"), "Monthly limit")
    if name.lower() in {special.lower() for special in SPECIAL_CATEGORIES}:
        raise AppError(f"“{name}” is a built-in category.")
    clash = conn.execute(
        "SELECT id FROM categories WHERE user_id = ? AND lower(name) = lower(?)", (user_id, name)
    ).fetchone()
    if clash and clash["id"] != exclude_id:
        raise AppError("A category with that name already exists.", 409)
    return name, group, budget


def add_category(user_id: int, data: dict[str, Any]) -> None:
    with db.tx() as conn:
        name, group, budget = _category_fields(conn, user_id, data)
        count = conn.execute("SELECT COUNT(*) FROM categories WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count >= 200:
            raise AppError("Category limit reached.")
        conn.execute(
            "INSERT INTO categories (id, user_id, name, grp, color, budget_cents, position) VALUES (?,?,?,?,?,?,?)",
            (db.new_id(), user_id, name, group, COLORS[count % len(COLORS)], budget, count),
        )


def update_category(user_id: int, category_id: str, data: dict[str, Any]) -> None:
    with db.tx() as conn:
        current = conn.execute("SELECT * FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id)).fetchone()
        if not current:
            raise AppError("Category not found.", 404)
        merged = {"name": current["name"], "group": current["grp"], "budget": current["budget_cents"], **data}
        name, group, budget = _category_fields(conn, user_id, merged, exclude_id=category_id)
        conn.execute("UPDATE categories SET name = ?, grp = ?, budget_cents = ? WHERE id = ?", (name, group, budget, category_id))
        if name != current["name"]:
            conn.execute("UPDATE transactions SET category = ? WHERE user_id = ? AND category = ?", (name, user_id, current["name"]))
            conn.execute("UPDATE category_rules SET category = ? WHERE user_id = ? AND category = ?", (name, user_id, current["name"]))


def delete_category(user_id: int, category_id: str) -> None:
    with db.tx() as conn:
        current = conn.execute("SELECT name FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id)).fetchone()
        if not current:
            raise AppError("Category not found.", 404)
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        conn.execute("UPDATE transactions SET category = 'Uncategorized', category_source = 'user' "
                     "WHERE user_id = ? AND category = ?", (user_id, current["name"]))
        conn.execute("DELETE FROM category_rules WHERE user_id = ? AND category = ?", (user_id, current["name"]))


# --- Transactions --------------------------------------------------------------

def add_manual_transaction(user_id: int, data: dict[str, Any]) -> None:
    merchant = _text(data.get("merchant"), "Merchant", 80)
    amount = _cents(data.get("amount"), "Amount", minimum=1)
    try:
        day = date.fromisoformat(str(data.get("date"))).isoformat()
    except ValueError as error:
        raise AppError("Date must be YYYY-MM-DD.") from error
    with db.tx() as conn:
        category = str(data.get("category") or "Uncategorized")
        if category not in category_names(conn, user_id):
            raise AppError("Unknown category.")
        stamp = db.now()
        conn.execute(
            "INSERT INTO transactions (id, user_id, date, merchant, amount_cents, category, category_source, hidden, manual, "
            "account_label, created_at, updated_at) VALUES (?,?,?,?,?,?,'manual',?,1,?,?,?)",
            (db.new_id(), user_id, day, merchant, -amount if data.get("kind") == "income" else amount, category,
             int(bool(data.get("hidden"))), _text(data.get("account"), "Account", 60, required=False) or "Manual entry",
             stamp, stamp),
        )


def update_transaction(user_id: int, transaction_id: str, data: dict[str, Any]) -> None:
    with db.tx() as conn:
        current = conn.execute("SELECT id FROM transactions WHERE id = ? AND user_id = ?", (transaction_id, user_id)).fetchone()
        if not current:
            raise AppError("Transaction not found.", 404)
        if "category" in data:
            if data["category"] not in category_names(conn, user_id):
                raise AppError("Unknown category.")
            conn.execute("UPDATE transactions SET category = ?, category_source = 'user', updated_at = ? WHERE id = ?",
                         (data["category"], db.now(), transaction_id))
        if "hidden" in data:
            conn.execute("UPDATE transactions SET hidden = ?, updated_at = ? WHERE id = ?",
                         (int(bool(data["hidden"])), db.now(), transaction_id))


def delete_manual_transaction(user_id: int, transaction_id: str) -> None:
    with db.tx() as conn:
        deleted = conn.execute("DELETE FROM transactions WHERE id = ? AND user_id = ? AND manual = 1",
                               (transaction_id, user_id)).rowcount
    if not deleted:
        raise AppError("Only manual transactions can be deleted.", 404)


def set_account_included(user_id: int, account_id: str, included: bool) -> None:
    with db.tx() as conn:
        updated = conn.execute("UPDATE accounts SET included = ? WHERE id = ? AND user_id = ?",
                               (int(bool(included)), account_id, user_id)).rowcount
    if not updated:
        raise AppError("Account not found.", 404)


# --- Rules ---------------------------------------------------------------------

def add_rule(user_id: int, data: dict[str, Any]) -> None:
    match = _text(data.get("merchant"), "Merchant", 80).lower()
    with db.tx() as conn:
        if data.get("category") not in category_names(conn, user_id):
            raise AppError("Unknown category.")
        conn.execute(
            "INSERT INTO category_rules (id, user_id, match, category) VALUES (?,?,?,?) "
            "ON CONFLICT (user_id, match) DO UPDATE SET category = excluded.category",
            (db.new_id(), user_id, match, data["category"]),
        )


def delete_rule(user_id: int, rule_id: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM category_rules WHERE id = ? AND user_id = ?", (rule_id, user_id))


# --- Export and deletion -------------------------------------------------------

def _csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection from bank-supplied text."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def export_csv(user_id: int) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Merchant", "Category", "Account", "Status", "Budget status", "Amount (positive = spending)"])
    for item in state(user_id)["transactions"]:
        writer.writerow([
            item["date"], _csv_safe(item["merchant"]), _csv_safe(item["category"]), _csv_safe(item["account"]),
            "Pending" if item["pending"] else "Posted", "Hidden" if item["hidden"] else "Included",
            f"{item['amount'] / 100:.2f}",
        ])
    return output.getvalue()


def wipe_user_data(user_id: int) -> None:
    """Delete everything except the login itself, then restore starter categories."""
    with db.tx() as conn:
        for table in ("transactions", "connections", "categories", "category_rules"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        _seed_categories(conn, user_id)
