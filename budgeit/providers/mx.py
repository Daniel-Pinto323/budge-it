"""MX Platform API adapter: users, Connect Widget URLs, members, accounts, transactions.

Written against Platform API v20260929 (the documented default as of 2026-11).
Setting MX_API_VERSION=v20111101 switches to the legacy headers, widget options,
and aggregation path. Response parsing accepts the field layouts of both versions.
This adapter has not been exercised against a live MX environment yet.
"""

from __future__ import annotations

import base64
import time
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from ..config import Config
from .base import ProviderError, http_json, to_cents

LEGACY_VERSION = "v20111101"
# Member statuses where the user has to act in the Connect Widget again.
REAUTH_STATUSES = {"CHALLENGED", "DENIED", "EXPIRED", "IMPAIRED", "IMPEDED", "LOCKED", "PREVENTED",
                   "REJECTED", "DISCONNECTED", "CLOSED", "DISABLED", "DISCONTINUED"}
MAX_PAGES = 60

_KINDS = {
    "CHECKING": "depository", "SAVINGS": "depository", "CASH": "depository", "PREPAID": "depository",
    "DIGITAL_WALLET": "depository",
    "CREDIT_CARD": "credit", "LINE_OF_CREDIT": "credit", "CHECKING_LINE_OF_CREDIT": "credit",
    "LOAN": "loan", "MORTGAGE": "loan",
    "INVESTMENT": "investment",
}
_CATEGORY_HINTS = {
    "groceries": "Groceries",
    "mortgage & rent": "Housing",
    "credit card payment": "Transfer",
}
_TOP_LEVEL_HINTS = {
    "auto & transport": "Transportation",
    "bills & utilities": "Utilities",
    "food & dining": "Dining",
    "shopping": "Shopping",
    "entertainment": "Fun money",
    "travel": "Travel",
    "home": "Housing",
    "gifts & donations": "Gifts",
    "income": "Income",
    "transfer": "Transfer",
}


def _named(value: Any, key: str = "name") -> str | None:
    """Fields that are plain strings in the legacy API and nested objects in v20260929."""
    if isinstance(value, dict):
        return value.get(key)
    return value if isinstance(value, str) else None


class MxProvider:
    name = "mx"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.legacy = config.mx_api_version == LEGACY_VERSION

    def _request(self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
                 query: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        if not self.config.mx_configured:
            raise ProviderError("MX is not configured on this server.", 503)
        token = base64.b64encode(f"{self.config.mx_client_id}:{self.config.mx_api_key}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        if self.legacy:
            headers["Accept"] = "application/vnd.mx.api.v1+json"
        else:
            headers["Accept-Version"] = self.config.mx_api_version
        url = f"{self.config.mx_base_url}{path}"
        if query:
            url += "?" + urlencode(query)
        return http_json(url, method=method, payload=payload, headers=headers, provider="MX")

    def _pages(self, path: str, key: str, query: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            result = self._request(path, query=[*(query or []), ("page", str(page)), ("records_per_page", "100")])
            batch = result.get(key) or []
            records.extend(batch)
            total_pages = (result.get("pagination") or {}).get("total_pages")
            if not batch or (total_pages is not None and page >= int(total_pages)):
                break
        return records

    # --- Users and the Connect Widget -------------------------------------------

    def ensure_user(self, external_id: str) -> str:
        """Return the MX user guid for our user, creating the MX user on first use."""
        try:
            user = self._request("/users", method="POST", payload={"user": {"id": external_id}}).get("user") or {}
        except ProviderError:
            matches = self._request("/users", query=[("id", external_id)]).get("users") or []
            user = next((match for match in matches if match.get("id") == external_id), {})
        if not user.get("guid"):
            raise ProviderError("MX did not return a user for this account.", 502)
        return user["guid"]

    def widget_url(self, user_guid: str, member_guid: str | None = None) -> str:
        options: dict[str, Any] = {"widget_type": "connect_widget", "color_scheme": "light"}
        if self.legacy:
            options.update({"mode": "aggregation", "include_transactions": True, "ui_message_version": 4,
                            "is_mobile_webview": False})
        else:
            options.update({"data_request": {"products": ["transactions"]},
                            "target_origin_referrer": self.config.origin})
        if member_guid:
            options.update({"current_member_guid": member_guid, "update_credentials": True})
        result = self._request(f"/users/{quote(user_guid)}/widget_urls", method="POST", payload={"widget_url": options})
        url = (result.get("widget_url") or {}).get("url")
        if not url:
            raise ProviderError("MX did not return a Connect Widget URL.", 502)
        return url

    # --- Members ------------------------------------------------------------------

    def members(self, user_guid: str) -> list[dict[str, Any]]:
        members = self._pages(f"/users/{quote(user_guid)}/members", "members")
        return [{
            "member_guid": member["guid"],
            "institution": member.get("name") or _named(member.get("institution")) or "MX connection",
            "institution_id": member.get("institution_code") or _named(member.get("institution"), "code"),
        } for member in members if member.get("guid")]

    def member_status(self, user_guid: str, member_guid: str) -> dict[str, Any]:
        result = self._request(f"/users/{quote(user_guid)}/members/{quote(member_guid)}/status")
        status = result.get("member_status") or result.get("member") or {}
        return {
            "status": str(status.get("status") or status.get("connection_status") or "").upper(),
            "aggregating": status.get("is_being_aggregated"),
        }

    def refresh(self, user_guid: str, member_guid: str, wait_seconds: int = 20) -> None:
        """Ask MX to aggregate, then wait briefly. MX throttles this; failures are not fatal."""
        base = f"/users/{quote(user_guid)}/members/{quote(member_guid)}"
        try:
            self._request(f"{base}/aggregate" if self.legacy else f"{base}/transactions", method="POST")
        except ProviderError:
            return
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            time.sleep(3)
            aggregating = self.member_status(user_guid, member_guid)["aggregating"]
            if aggregating is False or aggregating is None:
                # v20260929 has no in-progress flag; webhooks and the periodic sync pick up late data.
                return

    def remove(self, user_guid: str, member_guid: str) -> None:
        self._request(f"/users/{quote(user_guid)}/members/{quote(member_guid)}", method="DELETE")

    # --- Data ---------------------------------------------------------------------

    def accounts(self, user_guid: str, member_guid: str) -> list[dict[str, Any]]:
        records = self._pages(f"/users/{quote(user_guid)}/members/{quote(member_guid)}/accounts", "accounts",
                              [("includes[]", "balances")])
        return [self._account(record) for record in records if record.get("guid")]

    @staticmethod
    def _account(account: dict[str, Any]) -> dict[str, Any]:
        balances = account.get("balances") if isinstance(account.get("balances"), dict) else {}
        current = next((value for value in (balances.get("balance"), account.get("current_balance"),
                                            account.get("balance")) if value is not None), 0)
        available = balances.get("available_balance", account.get("available_balance"))
        kind = _KINDS.get(str(account.get("type") or "").upper(), "other")
        balance = to_cents(current)
        if kind in {"credit", "loan"}:
            balance = -abs(balance)
        number = str(account.get("account_number_display") or account.get("account_number") or "")
        digits = "".join(char for char in number if char.isalnum())
        return {
            "provider_account_id": account["guid"],
            "name": account.get("nickname") or account.get("name") or "Linked account",
            "kind": kind,
            "subtype": str(account.get("subtype") or account.get("type") or "").replace("_", " ").lower() or None,
            "mask": digits[-4:] or None,
            "currency": account.get("currency_code") or balances.get("currency_code") or "USD",
            "balance_cents": balance,
            "available_cents": to_cents(available) if available is not None else None,
        }

    def transactions(self, user_guid: str, account_guid: str, days: int) -> list[dict[str, Any]]:
        """Transactions for one account over a recent window (MX allows at most ~6 months per query)."""
        today = date.today()
        query = [("from_date", (today - timedelta(days=min(days, 170))).isoformat()),
                 ("to_date", (today + timedelta(days=5)).isoformat())]
        if not self.legacy:
            query += [("includes[]", "category"), ("includes[]", "merchant")]
        records = self._pages(f"/users/{quote(user_guid)}/accounts/{quote(account_guid)}/transactions",
                              "transactions", query)
        return [self._transaction(record, account_guid) for record in records if record.get("guid")]

    @staticmethod
    def _transaction(item: dict[str, Any], account_guid: str) -> dict[str, Any]:
        category = _named(item.get("category"))
        top_level = item.get("top_level_category") or _named(item.get("category"), "top_level_category")
        hint = _CATEGORY_HINTS.get((category or "").lower()) or _TOP_LEVEL_HINTS.get((top_level or "").lower())
        amount = abs(to_cents(item.get("amount")))
        if str(item.get("type") or "").upper() == "CREDIT":
            amount = -amount  # MX: direction comes from type; normalise to positive = money out
        return {
            "provider_txn_id": item["guid"],
            "provider_account_id": account_guid,
            "date": str(item.get("date") or item.get("transacted_at") or item.get("posted_at") or "")[:10],
            "merchant": _named(item.get("merchant")) or item.get("description") or "Unknown merchant",
            "amount_cents": amount,
            "currency": item.get("currency_code") or "USD",
            "pending": str(item.get("status") or "").upper() == "PENDING",
            "pending_ref": None,
            "hint": hint,
            "provider_category": category or top_level,
        }
