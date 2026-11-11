"""Shared pieces for financial-data provider adapters.

Adapters translate a provider's API into the normalized records below, so the
rest of the app never touches raw provider payloads:

account:      provider_account_id, name, kind, subtype, mask, currency,
              balance_cents (net-worth contribution; debts negative), available_cents
transaction:  provider_txn_id, provider_account_id, date (YYYY-MM-DD), merchant,
              amount_cents (positive = money out), currency, pending,
              pending_ref (id of the pending transaction this one replaces),
              hint (suggested app category or None), provider_category
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 502, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def to_cents(value: Any) -> int:
    if value is None:
        return 0
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def http_json(
    url: str,
    *,
    method: str = "POST",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    provider: str = "Provider",
) -> dict[str, Any]:
    """Call a provider API. Money values are parsed as Decimal, never float."""
    request_headers = {"Accept": "application/json", "User-Agent": "budgeit-server"}
    request_headers.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body, parse_float=Decimal) if body.strip() else {}
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        code = None
        message = error.reason
        try:
            details = json.loads(body)
            if isinstance(details, dict):
                nested = details.get("error") if isinstance(details.get("error"), dict) else {}
                code = details.get("error_code") or nested.get("error_code") or nested.get("type")
                message = details.get("error_message") or nested.get("message") or details.get("message") or message
        except json.JSONDecodeError:
            pass
        raise ProviderError(f"{provider} request failed ({error.code}): {message}", 502, code and str(code)) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ProviderError(f"Could not reach {provider}.", 502) from error
    except json.JSONDecodeError as error:
        raise ProviderError(f"{provider} returned an unreadable response.", 502) from error
