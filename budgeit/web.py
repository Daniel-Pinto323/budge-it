"""HTTP layer: static files, session auth, JSON API, and provider webhooks.

Designed to sit behind a TLS-terminating reverse proxy (see deploy/Caddyfile). Only files
inside static/ are ever served; everything under /api/ except login, health, and
the signed webhooks requires a session cookie plus a CSRF header.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

from . import db, demo, security, store
from .config import STATIC_DIR, Config
from .providers.base import ProviderError
from .store import AppError
from .sync import SyncService

log = logging.getLogger("budgeit.web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json", ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
}
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://cdn.plaid.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    "connect-src 'self' https://*.plaid.com",
    "frame-src https://cdn.plaid.com https://*.moneydesktop.com https://*.mx.com",
    "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'", "object-src 'none'",
])
ID = r"([A-Za-z0-9-]{1,64})"


class App:
    def __init__(self, config: Config, sync: SyncService) -> None:
        self.config = config
        self.sync = sync
        self.cookie_name = "__Host-budgeit" if config.secure else "budgeit"
        self.login_by_ip = security.RateLimiter(limit=10, window_seconds=900)
        self.login_by_email = security.RateLimiter(limit=8, window_seconds=900)
        self.sync_limiter = security.RateLimiter(limit=12, window_seconds=600)


def make_server(config: Config, sync: SyncService) -> ThreadingHTTPServer:
    app = App(config, sync)

    class Handler(RequestHandler):
        pass

    Handler.app = app
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    server.daemon_threads = True
    return server


class RequestHandler(BaseHTTPRequestHandler):
    app: App
    server_version = "budgeit"
    sys_version = ""
    timeout = 60  # drop idle or stalled sockets

    # --- Plumbing ---------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # request lines are logged in _dispatch without query strings

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            db.close()

    def client_ip(self) -> str:
        if self.app.config.trust_proxy:
            forwarded = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            if forwarded:
                return forwarded
        return self.client_address[0]

    def respond(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None,
                cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # MX widget events are only delivered when the embedding origin is sent as referrer.
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        if self.app.config.secure:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload: Any, status: int = 200, extra: dict[str, str] | None = None) -> None:
        self.respond(status, json.dumps(payload).encode(), "application/json; charset=utf-8", extra)

    def read_body(self, limit: int = 64_000) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise AppError("Invalid Content-Length.") from error
        if size < 0 or size > limit:
            raise AppError("Request is too large.", 413)
        return self.rfile.read(size) if size else b""

    def read_json(self) -> dict[str, Any]:
        body = self.read_body()
        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AppError("Request body must be JSON.") from error
        if not isinstance(payload, dict):
            raise AppError("Request body must be a JSON object.")
        return payload

    def session_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:  # malformed cookie header
            return ""
        morsel = cookie.get(self.app.cookie_name)
        return morsel.value if morsel else ""

    def session_cookie(self, token: str, max_age: int) -> str:
        secure = "; Secure" if self.app.config.secure else ""
        return f"{self.app.cookie_name}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-browser client; state changes still need the CSRF token
        allowed = {self.app.config.origin}
        if not self.app.config.secure:
            allowed.add(f"http://{self.headers.get('Host', '')}")
        return origin in allowed

    # --- Dispatch ---------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch()

    do_HEAD = do_POST = do_PATCH = do_DELETE = do_GET

    def _dispatch(self) -> None:
        path = urlsplit(self.path).path
        method = "GET" if self.command == "HEAD" else self.command
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    raise AppError("Method not allowed.", 405)
                self.serve_static(path)
                return
            self.route_api(method, path)
        except AppError as error:
            self.send_json({"error": str(error)}, error.status)
        except ProviderError as error:
            self.send_json({"error": str(error)}, error.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            log.exception("Unhandled error on %s %s", method, path)
            self.send_json({"error": "Unexpected server error."}, 500)
        finally:
            if path.startswith("/api/") and path != "/api/health":
                log.info("%s %s", method, path)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        content_type = CONTENT_TYPES.get(target.suffix)
        if not content_type or not target.is_relative_to(STATIC_DIR) or not target.is_file():
            self.respond(404, b"Not found", "text/plain; charset=utf-8")
            return
        self.respond(200, target.read_bytes(), content_type, cache="no-cache")

    def route_api(self, method: str, path: str) -> None:
        app = self.app
        if method == "GET" and path == "/api/health":
            self.send_json({"ok": True})
            return
        if method == "POST" and path == "/api/webhooks/plaid":
            app.sync.handle_plaid_webhook(self.read_body(512_000), self.headers.get("Plaid-Verification", ""))
            self.send_json({"ok": True})
            return
        if method == "POST" and path == "/api/webhooks/mx":
            self.mx_webhook()
            return
        if method != "GET" and not self.same_origin():
            raise AppError("Cross-origin request refused.", 403)
        if method == "POST" and path == "/api/login":
            self.login()
            return

        token = self.session_token()
        user = security.find_session(token)
        if method == "GET" and path == "/api/session":
            self.send_json(self.session_payload(user))
            return
        if not user:
            raise AppError("Sign in required.", 401)
        if method != "GET" and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), user["csrf"]):
            raise AppError("Missing or invalid CSRF token.", 403)
        user_id = user["id"]

        if method == "GET" and path == "/api/state":
            self.send_state(user_id)
            return
        if method == "GET" and path == "/api/export.csv":
            self.respond(200, store.export_csv(user_id).encode("utf-8-sig"), "text/csv; charset=utf-8",
                         {"Content-Disposition": 'attachment; filename="budge-it-transactions.csv"'})
            return
        if method == "POST" and path == "/api/logout":
            security.delete_session(token)
            self.send_json({"ok": True}, extra={"Set-Cookie": self.session_cookie("", 0)})
            return

        data = self.read_json() if method != "GET" else {}
        for route_method, pattern, handler in self.routes():
            match = re.fullmatch(pattern, path)
            if match and route_method == method:
                extra = handler(user_id, data, *match.groups()) or {}
                self.send_state(user_id, extra)
                return
        raise AppError("Not found.", 404)

    def routes(self) -> list[tuple[str, str, Callable[..., dict[str, Any] | None]]]:
        sync = self.app.sync
        return [
            ("POST", "/api/categories", lambda u, d: store.add_category(u, d)),
            ("PATCH", f"/api/categories/{ID}", lambda u, d, i: store.update_category(u, i, d)),
            ("DELETE", f"/api/categories/{ID}", lambda u, d, i: store.delete_category(u, i)),
            ("POST", "/api/transactions", lambda u, d: store.add_manual_transaction(u, d)),
            ("PATCH", f"/api/transactions/{ID}", lambda u, d, i: store.update_transaction(u, i, d)),
            ("DELETE", f"/api/transactions/{ID}", lambda u, d, i: store.delete_manual_transaction(u, i)),
            ("PATCH", f"/api/accounts/{ID}", lambda u, d, i: store.set_account_included(u, i, bool(d.get("included")))),
            ("POST", "/api/rules", lambda u, d: store.add_rule(u, d)),
            ("DELETE", f"/api/rules/{ID}", lambda u, d, i: store.delete_rule(u, i)),
            ("POST", "/api/plaid/link-token", lambda u, d: {"link": sync.plaid_link_token(u, d.get("connection_id"))}),
            ("POST", "/api/plaid/exchange", self.plaid_exchange),
            ("POST", "/api/mx/widget-url", lambda u, d: {"widget": sync.mx_widget_url(u, d.get("connection_id"))}),
            ("POST", "/api/mx/discover", lambda u, d: {"members": self.limited_sync(u, lambda: sync.mx_discover(u))}),
            ("POST", "/api/sync", lambda u, d: self.limited_sync(u, lambda: sync.sync_user(u, refresh=True))),
            ("POST", f"/api/connections/{ID}/sync", self.sync_one),
            ("DELETE", f"/api/connections/{ID}", lambda u, d, i: sync.disconnect(u, i)),
            ("POST", "/api/demo", lambda u, d: demo.load(u)),
            ("DELETE", "/api/demo", lambda u, d: demo.remove(u)),
            ("POST", "/api/data/delete", self.delete_data),
        ]

    # --- Handlers ---------------------------------------------------------------

    def session_payload(self, user) -> dict[str, Any]:
        config = self.app.config
        if not user:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "user": {"name": user["name"], "email": user["email"]},
            "csrf": user["csrf"],
            "providers": {"plaid": config.plaid_configured, "mx": config.mx_configured, "plaid_env": config.plaid_env,
                          "mx_env": "production" if config.mx_base_url == "https://api.mx.com" else "integration"},
        }

    def send_state(self, user_id: int, extra: dict[str, Any] | None = None) -> None:
        self.send_json({**(extra or {}), "state": {**store.state(user_id), "has_demo": demo.has_demo(user_id)}})

    def login(self) -> None:
        app = self.app
        data = self.read_json()
        email = str(data.get("email") or "").strip().lower()[:254]
        password = str(data.get("password") or "")[:1024]
        ip = self.client_ip()
        if app.login_by_ip.blocked(ip) or app.login_by_email.blocked(email):
            raise AppError("Too many sign-in attempts. Try again in a few minutes.", 429)
        user = security.check_login(email, password)
        if not user:
            app.login_by_ip.record(ip)
            app.login_by_email.record(email)
            log.warning("Failed sign-in attempt from %s", ip)
            raise AppError("Email or password is incorrect.", 401)
        app.login_by_email.reset(email)
        token, _csrf = security.create_session(user["id"])
        self.send_json(self.session_payload(security.find_session(token)),
                       extra={"Set-Cookie": self.session_cookie(token, security.SESSION_DAYS * 86400)})

    def limited_sync(self, user_id: int, action: Callable[[], Any]) -> Any:
        key = f"user-{user_id}"
        if self.app.sync_limiter.blocked(key):
            raise AppError("That was a lot of refreshes. Give it a few minutes.", 429)
        self.app.sync_limiter.record(key)
        return action()

    def sync_one(self, user_id: int, data: dict[str, Any], connection_id: str) -> None:
        connection = SyncService._connection(user_id, connection_id)
        self.limited_sync(user_id, lambda: self.app.sync.sync_connection(connection["id"], refresh=True))

    def plaid_exchange(self, user_id: int, data: dict[str, Any]) -> None:
        public_token = data.get("public_token")
        if not isinstance(public_token, str) or not public_token:
            raise AppError("public_token is required.")
        self.app.sync.plaid_connect(user_id, public_token)

    def delete_data(self, user_id: int, data: dict[str, Any]) -> None:
        user = db.connect().execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if not security.verify_password(str(data.get("password") or ""), user["password_hash"]):
            raise AppError("Password is incorrect.", 403)
        self.app.sync.disconnect_all(user_id)
        store.wipe_user_data(user_id)

    def mx_webhook(self) -> None:
        config = self.app.config
        if not (config.mx_webhook_username and config.mx_webhook_password):
            raise AppError("Not found.", 404)
        expected = base64.b64encode(f"{config.mx_webhook_username}:{config.mx_webhook_password}".encode()).decode()
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Basic {expected}".encode()):
            raise AppError("Unauthorized.", 401)
        body = self.read_body(512_000)
        try:
            event = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AppError("Invalid webhook body.") from error
        if isinstance(event, dict):
            self.app.sync.handle_mx_webhook(event)
        self.send_json({"ok": True})
