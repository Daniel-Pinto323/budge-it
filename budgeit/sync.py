"""Connection lifecycle and transaction import for Plaid and MX.

Imports are idempotent: accounts and transactions are upserted by provider ID, so a
repeated webhook, a retried job, or an overlapping periodic run cannot double-count.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import date, timedelta
from typing import Any

from . import db, security
from .config import Config
from .providers.base import ProviderError
from .providers.mx import REAUTH_STATUSES, MxProvider
from .providers.plaid import REAUTH_CODES, PlaidProvider
from .store import AppError, category_names

log = logging.getLogger("budgeit.sync")


class SyncService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.plaid = PlaidProvider(config)
        self.mx = MxProvider(config)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()

    # --- Helpers ----------------------------------------------------------------

    def _lock(self, connection_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(connection_id, threading.Lock())

    @staticmethod
    def _connection(user_id: int, connection_id: Any):
        row = db.connect().execute(
            "SELECT * FROM connections WHERE id = ? AND user_id = ?", (str(connection_id), user_id)
        ).fetchone()
        if not row:
            raise AppError("Connection not found.", 404)
        return row

    def _mx_user_guid(self, user_id: int) -> str:
        conn = db.connect()
        guid = conn.execute("SELECT mx_user_guid FROM users WHERE id = ?", (user_id,)).fetchone()["mx_user_guid"]
        if guid:
            return guid
        # A stable, non-identifying external id: MX never receives the user's email.
        guid = self.mx.ensure_user(f"budgeit-user-{user_id}")
        with db.tx() as tx:
            tx.execute("UPDATE users SET mx_user_guid = ? WHERE id = ?", (guid, user_id))
        return guid

    # --- Plaid linking ----------------------------------------------------------

    def plaid_link_token(self, user_id: int, connection_id: str | None = None) -> dict[str, Any]:
        access_token = None
        if connection_id:
            connection = self._connection(user_id, connection_id)
            if connection["provider"] != "plaid":
                raise AppError("That is not a Plaid connection.")
            access_token = security.decrypt(connection["secret_enc"])
        return self.plaid.link_token(user_id, access_token)

    def plaid_connect(self, user_id: int, public_token: str) -> str:
        access_token, item_id = self.plaid.exchange(public_token)
        try:
            item = self.plaid.item(access_token)
            accounts = self.plaid.accounts(access_token)
            if self._is_duplicate(user_id, "plaid", item.get("institution_id"), accounts):
                raise AppError("That institution is already connected. Use Reconnect or Sync on the existing connection.", 409)
            connection_id = db.new_id()
            with db.tx() as conn:
                conn.execute(
                    "INSERT INTO connections (id, user_id, provider, provider_item_id, institution, institution_id, "
                    "secret_enc, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (connection_id, user_id, "plaid", item_id, item.get("institution") or "Plaid connection",
                     item.get("institution_id"), security.encrypt(access_token), db.now()),
                )
        except Exception:
            # Never leave a billable Item at Plaid that we hold no record of.
            try:
                self.plaid.remove(access_token)
            except ProviderError:
                log.warning("Could not remove orphaned Plaid item after a failed link.")
            raise
        self.sync_connection(connection_id)
        return connection_id

    @staticmethod
    def _is_duplicate(user_id: int, provider: str, institution_id: str | None, accounts: list[dict[str, Any]]) -> bool:
        if not institution_id:
            return False
        existing = db.connect().execute(
            "SELECT a.name, a.mask FROM accounts a JOIN connections c ON c.id = a.connection_id "
            "WHERE c.user_id = ? AND c.provider = ? AND c.institution_id = ?", (user_id, provider, institution_id)
        ).fetchall()
        known = {(row["name"], row["mask"]) for row in existing}
        return any((account["name"], account["mask"]) in known for account in accounts)

    # --- MX linking -------------------------------------------------------------

    def mx_widget_url(self, user_id: int, connection_id: str | None = None) -> dict[str, Any]:
        member_guid = None
        if connection_id:
            connection = self._connection(user_id, connection_id)
            if connection["provider"] != "mx":
                raise AppError("That is not an MX connection.")
            member_guid = connection["provider_item_id"]
        return {"url": self.mx.widget_url(self._mx_user_guid(user_id), member_guid)}

    def mx_discover(self, user_id: int) -> int:
        """Record every MX member the user has connected, then import each one. Returns the member count."""
        members = self.mx.members(self._mx_user_guid(user_id))
        for member in members:
            with db.tx() as conn:
                conn.execute(
                    "INSERT INTO connections (id, user_id, provider, provider_item_id, institution, institution_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT (provider, provider_item_id) DO NOTHING",
                    (db.new_id(), user_id, "mx", member["member_guid"], member["institution"], member["institution_id"], db.now()),
                )
        rows = db.connect().execute("SELECT id FROM connections WHERE user_id = ? AND provider = 'mx'", (user_id,)).fetchall()
        for row in rows:
            self.sync_connection(row["id"])
        return len(members)

    # --- Sync -------------------------------------------------------------------

    def sync_user(self, user_id: int, refresh: bool = False) -> None:
        rows = db.connect().execute(
            "SELECT id FROM connections WHERE user_id = ? AND provider != 'demo'", (user_id,)
        ).fetchall()
        for row in rows:
            self.sync_connection(row["id"], refresh=refresh)

    def sync_all(self) -> None:
        for row in db.connect().execute("SELECT id FROM connections WHERE provider != 'demo'").fetchall():
            self.sync_connection(row["id"])

    def sync_connection(self, connection_id: str, refresh: bool = False) -> None:
        """Import the latest data for one connection. Failures are recorded on the connection, not raised."""
        with self._lock(connection_id):
            connection = db.connect().execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
            if not connection or connection["provider"] == "demo":
                return
            with db.tx() as conn:
                conn.execute("UPDATE connections SET last_attempt_at = ? WHERE id = ?", (db.now(), connection_id))
            try:
                if connection["provider"] == "plaid":
                    self._sync_plaid(connection)
                else:
                    self._sync_mx(connection, refresh)
            except ProviderError as error:
                status = "needs_reauth" if error.code in REAUTH_CODES else "error"
                self._mark(connection_id, status, str(error))
                log.warning("Sync failed for connection %s (%s): %s", connection_id, connection["provider"], error.code or "error")
            except Exception:
                self._mark(connection_id, "error", "Unexpected error during sync.")
                log.exception("Unexpected sync failure for connection %s", connection_id)

    @staticmethod
    def _mark(connection_id: str, status: str, error: str | None) -> None:
        with db.tx() as conn:
            conn.execute("UPDATE connections SET status = ?, error = ? WHERE id = ?", (status, error, connection_id))

    def _sync_plaid(self, connection) -> None:
        access_token = security.decrypt(connection["secret_enc"])
        accounts = self.plaid.accounts(access_token)
        changes = self.plaid.sync(access_token, connection["cursor"])
        self._apply(connection, accounts, changes["added"] + changes["modified"], removed=changes["removed"],
                    cursor=changes["cursor"])

    def _sync_mx(self, connection, refresh: bool) -> None:
        user_guid = self._mx_user_guid(connection["user_id"])
        member_guid = connection["provider_item_id"]
        if refresh:
            self.mx.refresh(user_guid, member_guid)
        status = self.mx.member_status(user_guid, member_guid)["status"]
        days = 45 if connection["last_success_at"] else 170
        accounts = self.mx.accounts(user_guid, member_guid)
        transactions: list[dict[str, Any]] = []
        for account in accounts:
            transactions.extend(self.mx.transactions(user_guid, account["provider_account_id"], days))
        window_start = (date.today() - timedelta(days=days)).isoformat()
        self._apply(connection, accounts, transactions, pending_window_start=window_start)
        if status in REAUTH_STATUSES:
            self._mark(connection["id"], "needs_reauth", f"MX reports this connection as {status.lower()}. Reconnect to resume updates.")

    def _apply(self, connection, accounts: list[dict[str, Any]], transactions: list[dict[str, Any]], *,
               removed: list[str] | None = None, cursor: str | None = None,
               pending_window_start: str | None = None) -> None:
        user_id, connection_id, stamp, day = connection["user_id"], connection["id"], db.now(), db.today()
        with db.tx() as conn:
            for account in accounts:
                conn.execute(
                    "INSERT INTO accounts (id, user_id, connection_id, provider_account_id, name, kind, subtype, mask, currency, "
                    "balance_cents, available_cents, balance_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (connection_id, provider_account_id) DO UPDATE SET name = excluded.name, kind = excluded.kind, "
                    "subtype = excluded.subtype, mask = excluded.mask, currency = excluded.currency, "
                    "balance_cents = excluded.balance_cents, available_cents = excluded.available_cents, balance_at = excluded.balance_at",
                    (db.new_id(), user_id, connection_id, account["provider_account_id"], account["name"], account["kind"],
                     account["subtype"], account["mask"], account["currency"], account["balance_cents"],
                     account["available_cents"], stamp),
                )
            account_ids = {row["provider_account_id"]: row["id"] for row in conn.execute(
                "SELECT id, provider_account_id FROM accounts WHERE connection_id = ?", (connection_id,))}
            for account in accounts:
                conn.execute(
                    "INSERT INTO balance_snapshots (account_id, day, balance_cents) VALUES (?,?,?) "
                    "ON CONFLICT (account_id, day) DO UPDATE SET balance_cents = excluded.balance_cents",
                    (account_ids[account["provider_account_id"]], day, account["balance_cents"]),
                )
            rules = {row["match"]: row["category"] for row in conn.execute(
                "SELECT match, category FROM category_rules WHERE user_id = ?", (user_id,))}
            names = category_names(conn, user_id)
            placeholders = ",".join("?" * len(account_ids)) or "NULL"
            scope = list(account_ids.values())
            seen: set[str] = set()
            for item in transactions:
                account_id = account_ids.get(item["provider_account_id"])
                if not account_id or not item["date"]:
                    continue
                seen.add(item["provider_txn_id"])
                self._upsert_transaction(conn, user_id, account_id, item, rules, names, placeholders, scope, stamp)
            for provider_txn_id in removed or []:
                conn.execute(f"DELETE FROM transactions WHERE provider_txn_id = ? AND account_id IN ({placeholders})",
                             (provider_txn_id, *scope))
            if pending_window_start:
                # Providers without a removal feed: drop pending rows that no longer exist upstream.
                stale = conn.execute(
                    f"SELECT id, provider_txn_id FROM transactions WHERE pending = 1 AND date >= ? AND account_id IN ({placeholders})",
                    (pending_window_start, *scope)).fetchall()
                for row in stale:
                    if row["provider_txn_id"] not in seen:
                        conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
            conn.execute(
                "UPDATE connections SET cursor = COALESCE(?, cursor), status = 'healthy', error = NULL, last_success_at = ? WHERE id = ?",
                (cursor, stamp, connection_id))

    @staticmethod
    def _upsert_transaction(conn, user_id, account_id, item, rules, names, placeholders, scope, stamp) -> None:
        existing = conn.execute("SELECT * FROM transactions WHERE account_id = ? AND provider_txn_id = ?",
                                (account_id, item["provider_txn_id"])).fetchone()
        hidden = existing["hidden"] if existing else 0
        if existing and existing["category_source"] in ("user", "rule"):
            category, source = existing["category"], existing["category_source"]
        else:
            rule = rules.get(item["merchant"].lower())
            hint = item["hint"] if item["hint"] in names else None
            category, source = (rule, "rule") if rule in names else (hint or "Uncategorized", "provider")
            if not existing and item["pending_ref"]:
                # A posted transaction replacing its pending version keeps the user's edits.
                previous = conn.execute(
                    f"SELECT category, category_source, hidden FROM transactions WHERE provider_txn_id = ? AND account_id IN ({placeholders})",
                    (item["pending_ref"], *scope)).fetchone()
                if previous:
                    hidden = previous["hidden"]
                    if previous["category_source"] == "user" and previous["category"] in names:
                        category, source = previous["category"], "user"
        if existing:
            conn.execute(
                "UPDATE transactions SET date = ?, merchant = ?, amount_cents = ?, currency = ?, pending = ?, category = ?, "
                "category_source = ?, provider_category = ?, updated_at = ? WHERE id = ?",
                (item["date"], item["merchant"][:120], item["amount_cents"], item["currency"], int(item["pending"]), category,
                 source, item["provider_category"], stamp, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO transactions (id, user_id, account_id, provider_txn_id, date, merchant, amount_cents, currency, pending, "
                "category, category_source, provider_category, hidden, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (db.new_id(), user_id, account_id, item["provider_txn_id"], item["date"], item["merchant"][:120],
                 item["amount_cents"], item["currency"], int(item["pending"]), category, source, item["provider_category"],
                 hidden, stamp, stamp))

    # --- Disconnect -------------------------------------------------------------

    def disconnect(self, user_id: int, connection_id: str) -> None:
        connection = self._connection(user_id, connection_id)
        with self._lock(connection["id"]):
            try:
                if connection["provider"] == "plaid":
                    self.plaid.remove(security.decrypt(connection["secret_enc"]))
                elif connection["provider"] == "mx":
                    self.mx.remove(self._mx_user_guid(user_id), connection["provider_item_id"])
            except ProviderError as error:
                if error.code not in REAUTH_CODES and connection["status"] == "healthy":
                    raise AppError(f"The provider did not confirm the disconnect, so nothing was deleted. {error}", 502) from error
                log.warning("Provider revoke failed for an already-broken connection %s; deleting local data.", connection["id"])
            with db.tx() as conn:
                conn.execute("DELETE FROM connections WHERE id = ?", (connection["id"],))

    def disconnect_all(self, user_id: int) -> None:
        for row in db.connect().execute("SELECT id FROM connections WHERE user_id = ?", (user_id,)).fetchall():
            self.disconnect(user_id, row["id"])

    # --- Webhooks ---------------------------------------------------------------

    @staticmethod
    def _first_delivery(provider: str, dedupe_key: str) -> bool:
        with db.tx() as conn:
            return conn.execute(
                "INSERT INTO webhook_events (dedupe_key, provider, received_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                (dedupe_key, provider, db.now())).rowcount == 1

    def handle_plaid_webhook(self, body: bytes, signed_jwt: str) -> None:
        dedupe_key = self.plaid.verify_webhook(body, signed_jwt)
        try:
            event = json.loads(body)
        except json.JSONDecodeError as error:
            raise AppError("Invalid webhook body.") from error
        if not isinstance(event, dict) or not self._first_delivery("plaid", dedupe_key):
            return
        connection = db.connect().execute(
            "SELECT id FROM connections WHERE provider = 'plaid' AND provider_item_id = ?", (str(event.get("item_id")),)
        ).fetchone()
        if not connection:
            return
        kind, code = event.get("webhook_type"), event.get("webhook_code")
        log.info("Plaid webhook %s/%s for connection %s", kind, code, connection["id"])
        if kind == "TRANSACTIONS":
            self.enqueue(connection["id"])
        elif kind == "ITEM":
            error_code = (event.get("error") or {}).get("error_code")
            if code in ("PENDING_EXPIRATION", "PENDING_DISCONNECT", "USER_PERMISSION_REVOKED") or (
                    code == "ERROR" and error_code in REAUTH_CODES):
                self._mark(connection["id"], "needs_reauth", "This institution needs you to sign in again.")
            elif code in ("LOGIN_REPAIRED", "NEW_ACCOUNTS_AVAILABLE"):
                self.enqueue(connection["id"])

    def handle_mx_webhook(self, event: dict[str, Any]) -> None:
        member_guid = str(event.get("member_guid") or "")
        kind = str(event.get("type") or "").upper()
        key = "|".join(str(event.get(field) or "") for field in
                       ("type", "action", "member_guid", "completed_at", "connection_status", "job_guid"))
        if not member_guid or not self._first_delivery("mx", key):
            return
        connection = db.connect().execute(
            "SELECT id FROM connections WHERE provider = 'mx' AND provider_item_id = ?", (member_guid,)).fetchone()
        if not connection:
            return
        log.info("MX webhook %s for connection %s", kind, connection["id"])
        if kind == "CONNECTION_STATUS" and str(event.get("connection_status") or "").upper() in REAUTH_STATUSES:
            self._mark(connection["id"], "needs_reauth", "This institution needs you to sign in again.")
        else:
            self.enqueue(connection["id"])

    # --- Background worker ------------------------------------------------------

    def enqueue(self, connection_id: str) -> None:
        with self._locks_guard:
            if connection_id in self._queued:
                return
            self._queued.add(connection_id)
        self._queue.put(connection_id)

    def start(self) -> None:
        threading.Thread(target=self._run, name="sync-worker", daemon=True).start()

    def _run(self) -> None:
        interval = self.config.sync_interval_minutes * 60
        next_full = time.monotonic() + 30
        while True:
            try:
                connection_id = self._queue.get(timeout=20)
                with self._locks_guard:
                    self._queued.discard(connection_id)
                self.sync_connection(connection_id)
            except queue.Empty:
                pass
            except Exception:
                log.exception("Sync worker error")
            if time.monotonic() >= next_full:
                next_full = time.monotonic() + interval
                try:
                    self.sync_all()
                    security.purge_expired_sessions()
                    with db.tx() as conn:
                        conn.execute("DELETE FROM webhook_events WHERE received_at < ?",
                                     ((date.today() - timedelta(days=14)).isoformat(),))
                except Exception:
                    log.exception("Periodic sync failed")
