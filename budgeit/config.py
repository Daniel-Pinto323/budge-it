"""Runtime configuration, read once from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


def load_env_file(path: Path | None = None) -> None:
    """Load simple KEY=VALUE lines. Real environment variables always win."""
    env_file = path or ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    public_url: str
    data_dir: Path
    encryption_key: str
    trust_proxy: bool
    sync_interval_minutes: int
    plaid_client_id: str
    plaid_secret: str
    plaid_env: str
    plaid_base_url: str
    plaid_redirect_uri: str
    mx_client_id: str
    mx_api_key: str
    mx_base_url: str
    mx_api_version: str
    mx_webhook_username: str
    mx_webhook_password: str

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def plaid_configured(self) -> bool:
        return bool(self.plaid_client_id and self.plaid_secret)

    @property
    def mx_configured(self) -> bool:
        return bool(self.mx_client_id and self.mx_api_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "budgeit.sqlite3"


def load_config() -> Config:
    port = int(_env("PORT", "8080"))
    plaid_env = _env("PLAID_ENV", "sandbox").lower()
    if plaid_env not in {"sandbox", "production"}:
        raise SystemExit("PLAID_ENV must be 'sandbox' or 'production'.")
    public_url = _env("PUBLIC_URL", f"http://localhost:{port}").rstrip("/")
    if urlsplit(public_url).scheme not in {"http", "https"}:
        raise SystemExit("PUBLIC_URL must start with http:// or https://")
    return Config(
        host=_env("HOST", "127.0.0.1"),
        port=port,
        public_url=public_url,
        data_dir=Path(_env("DATA_DIR", str(ROOT / "data"))).expanduser(),
        encryption_key=_env("BUDGEIT_ENCRYPTION_KEY"),
        trust_proxy=_env("TRUST_PROXY", "0").lower() in {"1", "true", "yes"},
        sync_interval_minutes=max(15, int(_env("SYNC_INTERVAL_MINUTES", "360"))),
        plaid_client_id=_env("PLAID_CLIENT_ID"),
        plaid_secret=_env("PLAID_SECRET"),
        plaid_env=plaid_env,
        plaid_base_url=_env("PLAID_BASE_URL", f"https://{plaid_env}.plaid.com").rstrip("/"),
        plaid_redirect_uri=_env("PLAID_REDIRECT_URI"),
        mx_client_id=_env("MX_CLIENT_ID") or _env("MX_USERNAME"),
        mx_api_key=_env("MX_API_KEY") or _env("MX_PASSWORD"),
        mx_base_url=_env("MX_BASE_URL", "https://int-api.mx.com").rstrip("/"),
        mx_api_version=_env("MX_API_VERSION", "v20260929"),
        mx_webhook_username=_env("MX_WEBHOOK_USERNAME"),
        mx_webhook_password=_env("MX_WEBHOOK_PASSWORD"),
    )
