"""Plaid adapter: Link tokens, token exchange, accounts, /transactions/sync, webhooks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from ..config import Config
from .base import ProviderError, http_json, to_cents

REAUTH_CODES = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT", "INVALID_ACCESS_TOKEN",
                "ITEM_NOT_FOUND", "ACCESS_NOT_GRANTED", "USER_PERMISSION_REVOKED"}
MUTATION_CODE = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"

# Plaid personal_finance_category -> starter app category. Detailed values win over primary.
_DETAILED_HINTS = {
    "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "RENT_AND_UTILITIES_RENT": "Housing",
    "LOAN_PAYMENTS_MORTGAGE_PAYMENT": "Housing",
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT": "Transfer",
    "GENERAL_SERVICES_INSURANCE": "Utilities",
    "ENTERTAINMENT_TV_AND_MOVIES": "Subscriptions",
    "ENTERTAINMENT_MUSIC_AND_AUDIO": "Subscriptions",
    "GOVERNMENT_AND_NON_PROFIT_DONATIONS": "Gifts",
    "GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES": "Gifts",
}
_PRIMARY_HINTS = {
    "INCOME": "Income",
    "TRANSFER_IN": "Transfer",
    "TRANSFER_OUT": "Transfer",
    "FOOD_AND_DRINK": "Dining",
    "GENERAL_MERCHANDISE": "Shopping",
    "HOME_IMPROVEMENT": "Shopping",
    "TRANSPORTATION": "Transportation",
    "TRAVEL": "Travel",
    "RENT_AND_UTILITIES": "Utilities",
    "ENTERTAINMENT": "Fun money",
}
_KINDS = {"depository", "credit", "loan", "investment"}


def _b64url(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class PlaidProvider:
    name = "plaid"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._keys: dict[str, dict[str, Any]] = {}
        self._keys_lock = threading.Lock()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.plaid_configured:
            raise ProviderError("Plaid is not configured on this server.", 503)
        body = {"client_id": self.config.plaid_client_id, "secret": self.config.plaid_secret, **payload}
        return http_json(f"{self.config.plaid_base_url}{path}", payload=body, provider="Plaid")

    # --- Link -----------------------------------------------------------------

    def link_token(self, user_id: int, access_token: str | None = None) -> dict[str, Any]:
        """New-connection token, or an update-mode token when access_token is given."""
        payload: dict[str, Any] = {
            "client_name": "budge it",
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": f"user-{user_id}"},
        }
        if access_token:
            payload["access_token"] = access_token
        else:
            payload["products"] = ["transactions"]
            payload["transactions"] = {"days_requested": 730}
            if self.config.secure:
                payload["webhook"] = f"{self.config.public_url}/api/webhooks/plaid"
        if self.config.plaid_redirect_uri:
            payload["redirect_uri"] = self.config.plaid_redirect_uri
        result = self._post("/link/token/create", payload)
        return {"link_token": result["link_token"], "expiration": result.get("expiration")}

    def exchange(self, public_token: str) -> tuple[str, str]:
        result = self._post("/item/public_token/exchange", {"public_token": public_token})
        return result["access_token"], result["item_id"]

    def item(self, access_token: str) -> dict[str, Any]:
        item = self._post("/item/get", {"access_token": access_token}).get("item", {})
        return {"institution_id": item.get("institution_id"), "institution": item.get("institution_name")}

    def remove(self, access_token: str) -> None:
        self._post("/item/remove", {"access_token": access_token})

    # --- Data -----------------------------------------------------------------

    def accounts(self, access_token: str) -> list[dict[str, Any]]:
        result = self._post("/accounts/get", {"access_token": access_token})
        return [self._account(account) for account in result.get("accounts", [])]

    @staticmethod
    def _account(account: dict[str, Any]) -> dict[str, Any]:
        balances = account.get("balances") or {}
        kind = account.get("type") if account.get("type") in _KINDS else "other"
        balance = to_cents(balances.get("current"))
        if kind in {"credit", "loan"}:
            balance = -balance  # Plaid reports amount owed as a positive number
        available = balances.get("available")
        return {
            "provider_account_id": account["account_id"],
            "name": account.get("name") or account.get("official_name") or "Linked account",
            "kind": kind,
            "subtype": account.get("subtype"),
            "mask": account.get("mask"),
            "currency": balances.get("iso_currency_code") or "USD",
            "balance_cents": balance,
            "available_cents": to_cents(available) if available is not None else None,
        }

    def sync(self, access_token: str, cursor: str | None) -> dict[str, Any]:
        """Fetch every change since `cursor`. Restarts the page loop if data mutates mid-way."""
        for _attempt in range(4):
            added: list[dict[str, Any]] = []
            modified: list[dict[str, Any]] = []
            removed: list[str] = []
            page_cursor = cursor
            try:
                while True:
                    payload: dict[str, Any] = {"access_token": access_token, "count": 500}
                    if page_cursor:
                        payload["cursor"] = page_cursor
                    page = self._post("/transactions/sync", payload)
                    added.extend(self._transaction(item) for item in page.get("added", []))
                    modified.extend(self._transaction(item) for item in page.get("modified", []))
                    removed.extend(item["transaction_id"] for item in page.get("removed", []))
                    page_cursor = page.get("next_cursor") or page_cursor
                    if not page.get("has_more"):
                        return {"added": added, "modified": modified, "removed": removed, "cursor": page_cursor}
            except ProviderError as error:
                if error.code != MUTATION_CODE:
                    raise
                time.sleep(1)
        raise ProviderError("Plaid transaction data kept changing during sync. It will be retried.", 502)

    @staticmethod
    def _transaction(item: dict[str, Any]) -> dict[str, Any]:
        pfc = item.get("personal_finance_category") or {}
        primary, detailed = pfc.get("primary"), pfc.get("detailed")
        return {
            "provider_txn_id": item["transaction_id"],
            "provider_account_id": item["account_id"],
            "date": item.get("authorized_date") or item.get("date"),
            "merchant": item.get("merchant_name") or item.get("name") or "Unknown merchant",
            "amount_cents": to_cents(item.get("amount")),  # Plaid: positive = money out
            "currency": item.get("iso_currency_code") or item.get("unofficial_currency_code") or "USD",
            "pending": bool(item.get("pending")),
            "pending_ref": item.get("pending_transaction_id"),
            "hint": _DETAILED_HINTS.get(detailed) or _PRIMARY_HINTS.get(primary),
            "provider_category": detailed or primary,
        }

    # --- Webhooks -------------------------------------------------------------

    def _verification_key(self, key_id: str) -> dict[str, Any]:
        with self._keys_lock:
            key = self._keys.get(key_id)
        if key is None:
            key = self._post("/webhook_verification_key/get", {"key_id": key_id})["key"]
            with self._keys_lock:
                if len(self._keys) > 20:
                    self._keys.clear()
                self._keys[key_id] = key
        return key

    def verify_webhook(self, body: bytes, signed_jwt: str) -> str:
        """Validate the Plaid-Verification JWT against the raw body. Returns a dedupe key."""
        try:
            header_b64, payload_b64, signature_b64 = signed_jwt.split(".")
            header = json.loads(_b64url(header_b64))
            if header.get("alg") != "ES256" or not header.get("kid"):
                raise ValueError("unexpected algorithm")
            key = self._verification_key(str(header["kid"]))
            if key.get("expired_at") is not None:
                raise ValueError("verification key expired")
            public_key = ec.EllipticCurvePublicNumbers(
                int.from_bytes(_b64url(key["x"]), "big"), int.from_bytes(_b64url(key["y"]), "big"), ec.SECP256R1()
            ).public_key()
            signature = _b64url(signature_b64)
            if len(signature) != 64:
                raise ValueError("bad signature length")
            der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
            public_key.verify(der, f"{header_b64}.{payload_b64}".encode(), ec.ECDSA(hashes.SHA256()))
            claims = json.loads(_b64url(payload_b64))
            if abs(time.time() - float(claims["iat"])) > 300:
                raise ValueError("webhook is too old")
            body_hash = hashlib.sha256(body).hexdigest()
            if not hmac.compare_digest(body_hash, str(claims["request_body_sha256"])):
                raise ValueError("body hash mismatch")
        except ProviderError:
            raise
        except (ValueError, KeyError, TypeError, InvalidSignature) as error:
            raise ProviderError("Webhook verification failed.", 401) from error
        return hashlib.sha256(signed_jwt.encode()).hexdigest()
