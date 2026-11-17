#!/usr/bin/env python3
"""budge it server entry point.

    python3 server.py                     run the web app
    python3 server.py generate-key        print a new BUDGEIT_ENCRYPTION_KEY
    python3 server.py create-user EMAIL   create a login (prompts for a password)
    python3 server.py set-password EMAIL  reset a password and sign out its sessions
"""

from __future__ import annotations

import getpass
import ipaddress
import logging
import os
import sys

from budgeit import db, security, store
from budgeit.config import Config, load_config, load_env_file
from budgeit.sync import SyncService
from budgeit.web import make_server


def _prompt_password() -> str:
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords did not match.")
    return password


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _bootstrap_admin() -> None:
    """Create the first user from the environment (for container deployments)."""
    email, password = os.environ.get("BUDGEIT_ADMIN_EMAIL", "").strip(), os.environ.get("BUDGEIT_ADMIN_PASSWORD", "")
    if store.user_count() == 0 and email and password:
        store.create_user(email, password)
        logging.getLogger("budgeit").info("Created initial user from BUDGEIT_ADMIN_EMAIL.")


def serve(config: Config) -> None:
    log = logging.getLogger("budgeit")
    _bootstrap_admin()
    if store.user_count() == 0:
        log.warning("No users exist yet. Run: python3 server.py create-user you@example.com")
    if not _is_loopback(config.host) and not config.secure:
        log.warning("Listening on %s without PUBLIC_URL set to an https:// address. Session cookies will not be "
                    "marked Secure and Plaid webhooks are disabled. Put this server behind HTTPS before real use.",
                    config.host)
    sync = SyncService(config)
    sync.start()
    server = make_server(config, sync)
    log.info("budge it listening on http://%s:%s (public URL %s)", config.host, config.port, config.public_url)
    log.info("Providers: Plaid %s (%s), MX %s", "on" if config.plaid_configured else "off", config.plaid_env,
             "on" if config.mx_configured else "off")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str]) -> None:
    command = argv[0] if argv else "serve"
    if command == "generate-key":
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
        return
    if command not in {"serve", "create-user", "set-password"} or (command != "serve" and len(argv) != 2):
        raise SystemExit(__doc__)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_file()
    config = load_config()
    security.init_encryption(config.encryption_key)
    db.init(config.db_path)
    try:
        if command == "create-user":
            store.create_user(argv[1], _prompt_password())
            print(f"Created user {argv[1].strip().lower()}.")
        elif command == "set-password":
            store.set_password(argv[1], _prompt_password())
            print("Password updated; existing sessions were signed out.")
        else:
            serve(config)
    except store.AppError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv[1:])
