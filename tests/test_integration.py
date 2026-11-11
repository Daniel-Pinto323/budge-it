"""End-to-end tests: the real server over HTTP, with Plaid and MX replaced by a local mock.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from budgeit import db, security, store  # noqa: E402
from budgeit.config import load_config  # noqa: E402
from budgeit.sync import SyncService  # noqa: E402
from budgeit.web import make_server  # noqa: E402

PASSWORD = "correct horse battery"
WEBHOOK_KEY = ec.generate_private_key(ec.SECP256R1())


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def plaid_txn(txn_id, amount, name, *, pending=False, replaces=None, primary="FOOD_AND_DRINK", detailed=None, account="acc-check"):
    return {"transaction_id": txn_id, "account_id": account, "amount": amount, "date": "2026-11-05", "authorized_date": None,
            "name": name.upper(), "merchant_name": name, "pending": pending, "pending_transaction_id": replaces,
            "iso_currency_code": "USD", "personal_finance_category": {"primary": primary, "detailed": detailed or primary + "_OTHER"}}


class Mock:
    """Scripted provider responses plus a log of what the app asked for."""
    plaid_stage = 1
    removed_items: list[str] = []
    mx_deleted: list[str] = []
    mx_headers: dict[str, str] = {}
    mutation_errors = 0

    @classmethod
    def plaid(cls, path: str, body: dict) -> tuple[int, dict]:
        assert body.get("client_id") == "client" and body.get("secret") == "secret"
        if path == "/link/token/create":
            cls.last_link_request = body
            return 200, {"link_token": "link-sandbox-123", "expiration": "2026-11-10T22:00:00Z"}
        if path == "/item/public_token/exchange":
            if body["public_token"] == "public-dup":
                return 200, {"access_token": "access-dup", "item_id": "item-dup"}
            return 200, {"access_token": "access-1", "item_id": "item-1"}
        if path == "/item/get":
            return 200, {"item": {"institution_id": "ins_1", "institution_name": "First Platypus Bank"}}
        if path == "/accounts/get":
            if cls.plaid_stage == 99:
                return 400, {"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "the login details of this item have changed"}
            return 200, {"accounts": [
                {"account_id": "acc-check", "name": "Plaid Checking", "mask": "0000", "type": "depository", "subtype": "checking",
                 "balances": {"current": 110.10 if cls.plaid_stage == 1 else 95.5, "available": 100, "iso_currency_code": "USD"}},
                {"account_id": "acc-card", "name": "Plaid Credit Card", "mask": "3333", "type": "credit", "subtype": "credit card",
                 "balances": {"current": 410.25, "available": None, "iso_currency_code": "USD"}},
            ]}
        if path == "/transactions/sync":
            cursor = body.get("cursor")
            if cursor is None:
                return 200, {"added": [plaid_txn("t1", 12.34, "Coffee Hut", pending=True),
                                       plaid_txn("t2", 80.01, "Grocer", detailed="FOOD_AND_DRINK_GROCERIES")],
                             "modified": [], "removed": [], "next_cursor": "c1", "has_more": True}
            if cursor == "c1":
                if cls.mutation_errors:
                    cls.mutation_errors -= 1
                    return 400, {"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "error_message": "mutation"}
                return 200, {"added": [plaid_txn("t3", -2500, "Employer", primary="INCOME"),
                                       plaid_txn("t4", 300, "Card payment", primary="LOAN_PAYMENTS",
                                                 detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", account="acc-card")],
                             "modified": [], "removed": [], "next_cursor": "c2", "has_more": False}
            if cursor == "c2" and cls.plaid_stage >= 2:
                return 200, {"added": [plaid_txn("t1-posted", 13.00, "Coffee Hut", replaces="t1")],
                             "modified": [plaid_txn("t2", 82.50, "Grocer", detailed="FOOD_AND_DRINK_GROCERIES")],
                             "removed": [{"transaction_id": "t1", "account_id": "acc-check"}],
                             "next_cursor": "c3", "has_more": False}
            return 200, {"added": [], "modified": [], "removed": [], "next_cursor": cursor, "has_more": False}
        if path == "/item/remove":
            cls.removed_items.append(body["access_token"])
            return 200, {"request_id": "r"}
        if path == "/webhook_verification_key/get":
            numbers = WEBHOOK_KEY.public_key().public_numbers()
            return 200, {"key": {"alg": "ES256", "crv": "P-256", "kty": "EC", "kid": body["key_id"], "use": "sig",
                                 "x": b64url(numbers.x.to_bytes(32, "big")), "y": b64url(numbers.y.to_bytes(32, "big")),
                                 "created_at": 1, "expired_at": None}}
        return 404, {"error_message": "unknown path " + path}

    @classmethod
    def mx(cls, method: str, path: str, query: str, body: dict) -> tuple[int, dict]:
        one_page = {"current_page": 1, "per_page": 100, "total_entries": 1, "total_pages": 1}
        if method == "POST" and path == "/users":
            return 200, {"user": {"guid": "USR-1", "id": body["user"]["id"]}}
        if method == "POST" and path == "/users/USR-1/widget_urls":
            cls.last_widget_request = body
            return 200, {"widget_url": {"type": "connect_widget", "url": "https://int-widgets.moneydesktop.com/md/connect/abc", "user_id": "x"}}
        if method == "GET" and path == "/users/USR-1/members":
            return 200, {"members": [{"guid": "MBR-1", "name": "MX Bank", "institution_code": "mxbank"}], "pagination": one_page}
        if path == "/users/USR-1/members/MBR-1/status":
            return 200, {"member_status": {"status": "CONNECTED"}}
        if method == "POST" and path == "/users/USR-1/members/MBR-1/transactions":
            return 202, {"member_status": {"status": "CONNECTED"}}
        if method == "GET" and path == "/users/USR-1/members/MBR-1/accounts":
            return 200, {"accounts": [
                {"guid": "ACT-1", "name": "MX Savings", "type": "SAVINGS", "account_number_display": "****7788",
                 "current_balance": 2000.5, "balances": {"balance": 2000.5, "available_balance": 1990}},
                {"guid": "ACT-2", "name": "MX Visa", "type": "CREDIT_CARD", "account_number_display": "1234", "current_balance": 150},
            ], "pagination": one_page}
        if method == "GET" and path == "/users/USR-1/accounts/ACT-1/transactions":
            assert "from_date=" in query and "to_date=" in query
            return 200, {"transactions": [
                {"guid": "TRN-1", "amount": 1500, "type": "CREDIT", "status": "POSTED", "date": "2026-11-03",
                 "description": "Paycheck", "category": {"name": "Paycheck", "top_level_category": "Income"}},
            ], "pagination": one_page}
        if method == "GET" and path == "/users/USR-1/accounts/ACT-2/transactions":
            return 200, {"transactions": [
                {"guid": "TRN-2", "amount": 42.42, "type": "DEBIT", "status": "PENDING", "date": "2026-11-08",
                 "description": "GAS STATION 123", "merchant": {"name": "Gas Station"},
                 "category": {"name": "Gas", "top_level_category": "Auto & Transport"}},
                # legacy field layout must parse too
                {"guid": "TRN-3", "amount": 9.99, "type": "DEBIT", "status": "POSTED", "date": "2026-11-07",
                 "description": "=HYPERLINK(evil)", "category": "Groceries", "top_level_category": "Food & Dining"},
            ], "pagination": one_page}
        if method == "DELETE" and path == "/users/USR-1/members/MBR-1":
            cls.mx_deleted.append("MBR-1")
            return 204, {}
        return 404, {"error": {"message": "unknown path " + path}}


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _handle(self):
        parts = urlsplit(self.path)
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size)) if size else {}
        if parts.path.startswith("/plaid"):
            status, payload = Mock.plaid(parts.path[len("/plaid"):], body)
        else:
            Mock.mx_headers = dict(self.headers)
            status, payload = Mock.mx(self.command, parts.path[len("/mx"):], parts.query, body)
        raw = json.dumps(payload).encode() if status != 204 else b""
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_DELETE = _handle


class Client:
    def __init__(self, base: str) -> None:
        self.base, self.cookie, self.csrf = base, "", ""

    def request(self, method: str, path: str, body=None, headers=None, raw: bytes | None = None):
        send = {"Content-Type": "application/json", **(headers or {})}
        if self.cookie:
            send["Cookie"] = self.cookie
        if self.csrf and "X-CSRF-Token" not in send:
            send["X-CSRF-Token"] = self.csrf
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, headers=send, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, payload, response_headers = response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            status, payload, response_headers = error.code, error.read(), error.headers
        if response_headers.get("Set-Cookie"):
            self.cookie = response_headers["Set-Cookie"].split(";")[0]
        content_type = response_headers.get("Content-Type", "")
        return status, (json.loads(payload) if "json" in content_type else payload), response_headers

    def login(self, email="me@example.com", password=PASSWORD):
        status, payload, _ = self.request("POST", "/api/login", {"email": email, "password": password})
        if status == 200:
            self.csrf = payload["csrf"]
        return status, payload


class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.mock = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        threading.Thread(target=cls.mock.serve_forever, daemon=True).start()
        mock_url = f"http://127.0.0.1:{cls.mock.server_address[1]}"
        os.environ.update({
            "HOST": "127.0.0.1", "PORT": "0", "PUBLIC_URL": "http://localhost:1", "DATA_DIR": cls.tmp.name,
            "BUDGEIT_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "PLAID_CLIENT_ID": "client", "PLAID_SECRET": "secret", "PLAID_ENV": "sandbox", "PLAID_BASE_URL": mock_url + "/plaid",
            "MX_CLIENT_ID": "mxid", "MX_API_KEY": "mxkey", "MX_BASE_URL": mock_url + "/mx",
            "MX_WEBHOOK_USERNAME": "hook", "MX_WEBHOOK_PASSWORD": "hook-secret",
        })
        cls.config = load_config()
        security.init_encryption(cls.config.encryption_key)
        db.init(cls.config.db_path)
        store.create_user("me@example.com", PASSWORD, "Me")
        store.create_user("other@example.com", PASSWORD, "Other")
        cls.sync = SyncService(cls.config)
        cls.sync.start()
        cls.server = make_server(cls.config, cls.sync)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.client = Client(cls.base)
        assert cls.client.login()[0] == 200

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.mock.shutdown()
        db.close()
        cls.tmp.cleanup()

    def state(self, client=None):
        status, payload, _ = (client or self.client).request("GET", "/api/state")
        self.assertEqual(status, 200)
        return payload["state"]

    # Tests run in name order; the numbered ones build on each other.

    def test_01_auth_is_required_and_static_is_confined(self):
        anonymous = Client(self.base)
        self.assertEqual(anonymous.request("GET", "/api/state")[0], 401)
        self.assertEqual(anonymous.request("POST", "/api/sync", {})[0], 401)
        self.assertEqual(anonymous.request("GET", "/api/session")[1], {"authenticated": False})
        self.assertEqual(anonymous.login(password="wrong password!!")[0], 401)
        status, body, headers = anonymous.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"budge it", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/../server.py", "/..%2f.env", "/%2e%2e/server.py", "/../data/budgeit.sqlite3", "/server.py", "/.env"):
            self.assertEqual(anonymous.request("GET", path)[0], 404, path)

    def test_02_csrf_and_origin_checks(self):
        self.assertEqual(self.client.request("POST", "/api/demo", {}, headers={"X-CSRF-Token": "nope"})[0], 403)
        self.assertEqual(self.client.request("POST", "/api/demo", {}, headers={"Origin": "https://evil.example"})[0], 403)

    def test_03_login_rate_limit(self):
        attacker = Client(self.base)
        codes = [attacker.login(email="victim@example.com", password="x" * 12)[0] for _ in range(10)]
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(codes[-1], 429)

    def test_10_plaid_link_and_initial_sync(self):
        Mock.mutation_errors = 1  # first pagination attempt must be restarted from the start
        status, payload, _ = self.client.request("POST", "/api/plaid/link-token", {})
        self.assertEqual((status, payload["link"]["link_token"]), (200, "link-sandbox-123"))
        self.assertEqual(Mock.last_link_request["products"], ["transactions"])
        self.assertNotIn("webhook", Mock.last_link_request)  # PUBLIC_URL is not https in tests
        status, payload, _ = self.client.request("POST", "/api/plaid/exchange", {"public_token": "public-1"})
        self.assertEqual(status, 200, payload)
        state = payload["state"]
        self.assertEqual([(c["provider"], c["institution"], c["status"]) for c in state["connections"]],
                         [("plaid", "First Platypus Bank", "healthy")])
        balances = {a["name"]: a["balance_cents"] for a in state["accounts"]}
        self.assertEqual(balances, {"Plaid Checking": 11010, "Plaid Credit Card": -41025})
        txns = {t["merchant"]: t for t in state["transactions"]}
        self.assertEqual(len(state["transactions"]), 4)
        self.assertEqual((txns["Coffee Hut"]["amount"], txns["Coffee Hut"]["pending"], txns["Coffee Hut"]["category"]), (1234, True, "Dining"))
        self.assertEqual(txns["Grocer"]["category"], "Groceries")
        self.assertEqual((txns["Employer"]["amount"], txns["Employer"]["category"]), (-250000, "Income"))
        self.assertEqual(txns["Card payment"]["category"], "Transfer")
        self.assertEqual(txns["Grocer"]["account"], "Plaid Checking ••0000")
        self.assertEqual(state["net_worth_history"][-1]["total"], 11010 - 41025)
        # The access token is encrypted at rest and never returned to the browser.
        raw = Path(self.config.db_path).read_bytes()
        self.assertNotIn(b"access-1", raw)
        self.assertNotIn("access-1", json.dumps(payload))
        secret = db.connect().execute("SELECT secret_enc, cursor FROM connections").fetchone()
        self.assertEqual((security.decrypt(secret["secret_enc"]), secret["cursor"]), ("access-1", "c2"))

    def test_11_duplicate_institution_is_refused_and_cleaned_up(self):
        status, payload, _ = self.client.request("POST", "/api/plaid/exchange", {"public_token": "public-dup"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(Mock.removed_items, ["access-dup"])
        self.assertEqual(len(self.state()["connections"]), 1)

    def test_12_user_edits_survive_pending_to_posted_and_resync_is_idempotent(self):
        state = self.state()
        coffee = next(t for t in state["transactions"] if t["merchant"] == "Coffee Hut")
        self.assertEqual(self.client.request("PATCH", f"/api/transactions/{coffee['id']}", {"category": "Fun money", "hidden": True})[0], 200)
        self.assertEqual(self.client.request("PATCH", f"/api/transactions/{coffee['id']}", {"category": "Nope"})[0], 400)
        connection_id = state["connections"][0]["id"]
        self.assertEqual(self.client.request("POST", f"/api/connections/{connection_id}/sync", {})[0], 200)
        self.assertEqual(len(self.state()["transactions"]), 4)  # nothing new upstream: no duplicates

        Mock.plaid_stage = 2
        body = json.dumps({"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item-1"}, indent=2).encode()
        self.assertEqual(self.post_plaid_webhook(body, tamper=True), 401)
        self.assertEqual(self.post_plaid_webhook(body, age=900), 401)
        self.assertEqual(self.post_plaid_webhook(body), 200)
        for _ in range(100):
            state = self.state()
            if any(t["merchant"] == "Grocer" and t["amount"] == 8250 for t in state["transactions"]):
                break
            time.sleep(0.1)
        txns = {t["merchant"]: t for t in state["transactions"]}
        self.assertEqual(len(state["transactions"]), 4)
        self.assertEqual(txns["Grocer"]["amount"], 8250)
        coffee = txns["Coffee Hut"]
        self.assertEqual((coffee["amount"], coffee["pending"], coffee["category"], coffee["hidden"]), (1300, False, "Fun money", True))
        self.assertEqual({a["name"]: a["balance_cents"] for a in state["accounts"]}["Plaid Checking"], 9550)

    def post_plaid_webhook(self, body: bytes, tamper: bool = False, age: int = 0) -> int:
        header = b64url(json.dumps({"alg": "ES256", "kid": "key-1", "typ": "JWT"}).encode())
        claims = b64url(json.dumps({"iat": int(time.time()) - age, "request_body_sha256": hashlib.sha256(body).hexdigest()}).encode())
        r, s = decode_dss_signature(WEBHOOK_KEY.sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
        token = f"{header}.{claims}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        sent = body.replace(b"item-1", b"item-2") if tamper else body
        return Client(self.base).request("POST", "/api/webhooks/plaid", raw=sent, headers={"Plaid-Verification": token})[0]

    def test_13_unsigned_webhook_is_rejected(self):
        self.assertEqual(Client(self.base).request("POST", "/api/webhooks/plaid", {"item_id": "item-1"})[0], 401)
        self.assertEqual(Client(self.base).request("POST", "/api/webhooks/mx", {"member_guid": "MBR-1"})[0], 401)

    def test_14_rules_apply_to_future_imports_only(self):
        self.assertEqual(self.client.request("POST", "/api/rules", {"merchant": "Gas Station", "category": "Travel"})[0], 200)

    def test_20_mx_widget_discover_and_import(self):
        status, payload, _ = self.client.request("POST", "/api/mx/widget-url", {})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["widget"]["url"].startswith("https://int-widgets.moneydesktop.com/"))
        self.assertEqual(Mock.last_widget_request["widget_url"]["data_request"], {"products": ["transactions"]})
        self.assertEqual(Mock.mx_headers.get("Accept-Version"), "v20260929")
        self.assertEqual(Mock.mx_headers.get("Authorization"), "Basic " + base64.b64encode(b"mxid:mxkey").decode())
        status, payload, _ = self.client.request("POST", "/api/mx/discover", {})
        self.assertEqual((status, payload.get("members")), (200, 1), payload)
        state = payload["state"]
        mx = [c for c in state["connections"] if c["provider"] == "mx"]
        self.assertEqual([(c["institution"], c["status"]) for c in mx], [("MX Bank", "healthy")])
        accounts = {a["name"]: a for a in state["accounts"]}
        self.assertEqual((accounts["MX Savings"]["balance_cents"], accounts["MX Savings"]["mask"]), (200050, "7788"))
        self.assertEqual((accounts["MX Visa"]["balance_cents"], accounts["MX Visa"]["kind"]), (-15000, "credit"))
        txns = {t["merchant"]: t for t in state["transactions"] if t["provider"] == "mx"}
        self.assertEqual((txns["Paycheck"]["amount"], txns["Paycheck"]["category"]), (-150000, "Income"))
        self.assertEqual((txns["Gas Station"]["amount"], txns["Gas Station"]["pending"], txns["Gas Station"]["category"]),
                         (4242, True, "Travel"))  # rule from test_14 beats the provider hint
        self.assertEqual(txns["=HYPERLINK(evil)"]["category"], "Groceries")
        # Discovering again must not duplicate connections, accounts, or transactions.
        again = self.client.request("POST", "/api/mx/discover", {})[1]["state"]
        self.assertEqual(len(again["transactions"]), len(state["transactions"]))
        self.assertEqual(len(again["accounts"]), len(state["accounts"]))

    def test_21_mx_webhook_with_basic_auth(self):
        auth = "Basic " + base64.b64encode(b"hook:hook-secret").decode()
        event = {"type": "CONNECTION_STATUS", "action": "CHANGED", "connection_status": "DENIED", "member_guid": "MBR-1", "user_guid": "USR-1"}
        self.assertEqual(Client(self.base).request("POST", "/api/webhooks/mx", event, headers={"Authorization": auth})[0], 200)
        mx = next(c for c in self.state()["connections"] if c["provider"] == "mx")
        self.assertEqual(mx["status"], "needs_reauth")

    def test_30_budget_data_and_export(self):
        self.assertEqual(self.client.request("POST", "/api/categories", {"name": "Pets", "group": "Everyday", "budget": 5000})[0], 200)
        self.assertEqual(self.client.request("POST", "/api/categories", {"name": "pets", "group": "Everyday", "budget": 1})[0], 409)
        self.assertEqual(self.client.request("POST", "/api/categories", {"name": "X", "group": "Y", "budget": 1.5})[0], 400)
        pets = next(c for c in self.state()["categories"] if c["name"] == "Pets")
        self.assertEqual(self.client.request("POST", "/api/transactions",
                                             {"merchant": "Vet", "date": "2026-11-09", "amount": 4500, "category": "Pets", "kind": "expense"})[0], 200)
        self.assertEqual(self.client.request("PATCH", f"/api/categories/{pets['id']}", {"name": "Pet care"})[0], 200)
        vet = next(t for t in self.state()["transactions"] if t["merchant"] == "Vet")
        self.assertEqual((vet["category"], vet["manual"], vet["amount"]), ("Pet care", True, 4500))
        self.assertEqual(self.client.request("DELETE", f"/api/categories/{pets['id']}", {})[0], 200)
        self.assertEqual(next(t for t in self.state()["transactions"] if t["merchant"] == "Vet")["category"], "Uncategorized")
        status, body, headers = self.client.request("GET", "/api/export.csv")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("'=HYPERLINK(evil)", body.decode("utf-8-sig"))

    def test_31_account_toggle_changes_net_worth(self):
        state = self.state()
        before = state["net_worth_history"][-1]["total"]
        card = next(a for a in state["accounts"] if a["name"] == "Plaid Credit Card")
        after = self.client.request("PATCH", f"/api/accounts/{card['id']}", {"included": False})[1]["state"]
        self.assertEqual(after["net_worth_history"][-1]["total"], before + 41025)

    def test_32_users_are_isolated(self):
        other = Client(self.base)
        self.assertEqual(other.login(email="other@example.com")[0], 200)
        mine = self.state()
        theirs = self.state(other)
        self.assertEqual((theirs["transactions"], theirs["accounts"], theirs["connections"]), ([], [], []))
        self.assertEqual(other.request("PATCH", f"/api/transactions/{mine['transactions'][0]['id']}", {"hidden": True})[0], 404)
        self.assertEqual(other.request("DELETE", f"/api/connections/{mine['connections'][0]['id']}", {})[0], 404)
        self.assertEqual(other.request("POST", f"/api/connections/{mine['connections'][0]['id']}/sync", {})[0], 404)
        self.assertEqual(other.request("POST", "/api/demo", {})[0], 200)
        demo_state = self.state(other)
        self.assertTrue(demo_state["has_demo"])
        self.assertGreater(len(demo_state["transactions"]), 10)
        self.assertEqual(other.request("DELETE", "/api/demo", {})[1]["state"]["transactions"], [])

    def test_40_reauth_state_then_disconnect(self):
        Mock.plaid_stage = 99
        plaid = next(c for c in self.state()["connections"] if c["provider"] == "plaid")
        state = self.client.request("POST", f"/api/connections/{plaid['id']}/sync", {})[1]["state"]
        self.assertEqual(next(c for c in state["connections"] if c["id"] == plaid["id"])["status"], "needs_reauth")
        status, payload, _ = self.client.request("POST", "/api/plaid/link-token", {"connection_id": plaid["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(Mock.last_link_request.get("access_token"), "access-1")  # update mode
        self.assertNotIn("products", Mock.last_link_request)
        Mock.plaid_stage = 2
        state = self.client.request("DELETE", f"/api/connections/{plaid['id']}", {})[1]["state"]
        self.assertIn("access-1", Mock.removed_items)
        self.assertFalse([c for c in state["connections"] if c["provider"] == "plaid"])
        self.assertFalse([t for t in state["transactions"] if t["provider"] == "plaid"])

    def test_41_delete_all_data_requires_password(self):
        self.assertEqual(self.client.request("POST", "/api/data/delete", {"password": "wrong"})[0], 403)
        status, payload, _ = self.client.request("POST", "/api/data/delete", {"password": PASSWORD})
        self.assertEqual(status, 200, payload)
        self.assertEqual(Mock.mx_deleted, ["MBR-1"])
        state = payload["state"]
        self.assertEqual((state["transactions"], state["accounts"], state["connections"], state["rules"]), ([], [], [], []))
        self.assertEqual(len(state["categories"]), len(store.DEFAULT_CATEGORIES))

    def test_50_logout(self):
        client = Client(self.base)
        client.login(email="other@example.com")
        self.assertEqual(client.request("POST", "/api/logout", {})[0], 200)
        client.cookie = client.cookie or "budgeit=stale"
        self.assertEqual(client.request("GET", "/api/state")[0], 401)


if __name__ == "__main__":
    unittest.main()
