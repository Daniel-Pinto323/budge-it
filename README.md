# budge it

A private, self-hosted personal budget and net-worth app. It pulls accounts and
transactions from your banks through **Plaid** and/or **MX**, lets you set monthly
category budgets, and tracks net worth over time. Bank passwords never touch this
app: you sign in inside the provider's own flow and the server only holds a
read-only token, encrypted at rest.

- `server.py` — entry point and admin commands
- `budgeit/` — server: config, SQLite storage, auth, sync, provider adapters, HTTP API
- `static/` — the browser app (plain HTML/CSS/JS, no build step)
- `deploy/` — systemd unit and Caddy config for self-hosting
- `tests/` — integration tests against fake Plaid and MX servers

## Run it locally

Requires Python 3.10+ and the `cryptography` package.

```bash
pip install -r requirements.txt        # or: apt install python3-cryptography
cp .env.example .env
python3 server.py generate-key         # paste the output into BUDGEIT_ENCRYPTION_KEY in .env
python3 server.py create-user you@example.com
python3 server.py
```

Open <http://localhost:8080> and sign in. **Settings → Load sample data** gives you
synthetic accounts to explore before connecting anything real.

Run the tests with `python3 -m unittest discover -s tests`.

## Connecting banks

All provider settings live in `.env`; see `.env.example` for every option.

**Plaid.** Put your `PLAID_CLIENT_ID` and sandbox `PLAID_SECRET` in `.env` and
connect Plaid's test bank (user `user_good`, password `pass_good`). For real
accounts, apply for production access in the Plaid Dashboard, complete the OAuth
registration that banks such as Chase and Capital One require, then set
`PLAID_ENV=production` with the production secret. To link those banks from a
phone, also set `PLAID_REDIRECT_URI` to your public URL plus `/` and add the same
value under Allowed redirect URIs in the Plaid Dashboard.

**MX.** Put your `MX_CLIENT_ID` and `MX_API_KEY` in `.env`. The default base URL is
MX's integration (test) environment. Production access needs an agreement with MX
and your server's IP on their allowlist. The MX adapter has only been run against
a fake server so far; expect to adjust it on first contact with the real API.

**Freshness.** Data arrives when the provider has it, not in real time. The server
re-syncs every `SYNC_INTERVAL_MINUTES` (default 6 hours), on demand from the
Sync buttons, and immediately on provider webhooks when the server is reachable at
an `https://` `PUBLIC_URL`.

## Self-hosting (for example in a Proxmox container)

The app is one Python process with a SQLite file. It needs an always-on machine,
a persistent disk, and something in front of it that provides HTTPS.

**Status (2026-11-11):** this app has been deployed in a Proxmox LXC (Debian 13,
unprivileged, nesting on, start at boot, container firewall on) under
`/opt/budgeit` as a systemd service, and is served over HTTPS to the maintainer's
own devices through **Tailscale** (the "Tailscale only" option in step 5). It uses
sample data only; no bank is connected yet and backups are not set up.

#### Tailscale setup used (unprivileged Proxmox LXC)

1. On the Proxmox host, give the container the tunnel device: pass through
   `/dev/net/tun` (Proxmox's device passthrough, or `lxc.cgroup2.devices.allow:
   c 10:200 rwm` plus `lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file`
   in `/etc/pve/lxc/<id>.conf`), then fully stop and start the container. The host
   must have `/dev/net/tun` (run `modprobe tun` if not).
2. Inside the container, install Tailscale and join the tailnet:
   `tailscale up --hostname=budgeit`.
3. In the Tailscale admin console's DNS page, enable **MagicDNS** and
   **HTTPS Certificates**. (Issued certificates list the machine name in public
   certificate-transparency logs.)
4. Run `tailscale serve --bg 8080`. Tailscale now answers HTTPS on port 443 and
   forwards to the app.
5. In `.env`, set `HOST=127.0.0.1`, keep `PORT=8080`, set
   `PUBLIC_URL=https://budgeit.<tailnet>.ts.net` and `TRUST_PROXY=1`, then
   `systemctl restart budgeit`. The plain-HTTP LAN address stops working, and the
   container's firewall rule for port 8080 is no longer needed.
6. Install Tailscale on every device that should reach the app (laptop, iPhone)
   and sign in to the same account. A device must have Tailscale connected to
   open the app, at home or away.

Because the app is private to the tailnet, provider webhooks cannot reach it;
data refreshes on the periodic sync and the Sync buttons. For Plaid OAuth banks,
confirm at registration that a `*.ts.net` redirect URI is accepted, or use a
Cloudflare Tunnel instead.

1. **Create a container.** A dedicated unprivileged Debian LXC with 1 core, 512 MB
   RAM and 4 GB disk is plenty. Keep it separate from other services.
2. **Install.**
   ```bash
   apt install python3 python3-cryptography git
   useradd --system --home /opt/budgeit --shell /usr/sbin/nologin budgeit
   git clone <your repo> /opt/budgeit
   cp /opt/budgeit/.env.example /opt/budgeit/.env
   chown root:budgeit /opt/budgeit/.env && chmod 640 /opt/budgeit/.env
   ```
3. **Configure `/opt/budgeit/.env`.** Set `BUDGEIT_ENCRYPTION_KEY`
   (`python3 server.py generate-key`), `DATA_DIR=/var/lib/budgeit`,
   `PUBLIC_URL=https://your-hostname`, `TRUST_PROXY=1`, and your provider keys.
   Leave `HOST=127.0.0.1`.
4. **Start the service.**
   ```bash
   cp /opt/budgeit/deploy/budgeit.service /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now budgeit
   cd /opt/budgeit && runuser -u budgeit -- python3 server.py create-user you@example.com
   ```
   Add more people later with the same `create-user` command; each login only
   sees its own data.
5. **Put HTTPS in front.** Pick one:
   - **Cloudflare Tunnel** (no open ports, hides your home IP, works behind
     CGNAT): run `cloudflared` in the container pointing at
     `http://127.0.0.1:8080`. Optionally add Cloudflare Access in front of
     everything except `/api/webhooks/*`.
   - **Port forwarding + Caddy**: forward ports 80 and 443 from your router to the
     container and use `deploy/Caddyfile`. Needs a public IP and DNS (dynamic DNS
     if your IP changes).
   - **Tailscale only** (not public): reach the app from your own devices over
     your tailnet. No webhooks, so data refreshes on the periodic sync.
6. **Tell the providers your URL.** Plaid: add the redirect URI (webhooks are
   registered automatically). MX: set the webhook URL to
   `https://your-hostname/api/webhooks/mx` with the Basic credentials from `.env`.

### Backups

Everything is in `DATA_DIR/budgeit.sqlite3` plus the encryption key in `.env`.
Back up both, and keep at least one copy off the server:

```bash
sqlite3 /var/lib/budgeit/budgeit.sqlite3 ".backup '/root/budgeit-$(date +%F).sqlite3'"
```

A Proxmox container backup captures both at once; encrypt it if it leaves the
house, since it holds the key and the data together. Without the key, the Plaid
tokens in a restored database are unreadable and each bank must be relinked.

### Updating

```bash
cd /opt/budgeit && git pull && systemctl restart budgeit
```

## Security notes

- Logins use scrypt-hashed passwords (12+ characters), rate-limited sign-in,
  HttpOnly session cookies and CSRF tokens. There is no two-factor login yet, so
  if the app is on the public internet use a long unique password, and consider
  an access proxy in front.
- Plaid access tokens are encrypted with `BUDGEIT_ENCRYPTION_KEY`. Plaid webhooks
  are signature-verified; MX webhooks require HTTP Basic credentials.
- Only the Plaid access tokens are encrypted at rest. Transactions, balances and
  account names are stored as plain SQLite rows, protected by file permissions
  (`0600`), the container boundary and private network access. Treat the database
  file and any backup of it as sensitive, and encrypt copies that leave the server.
- Connections are read-only. The app cannot move money.

### Planned hardening (not implemented)

Possible future improvements to data protection, roughly in priority order:
encrypted backups, full-disk (LUKS) or encrypted-volume storage for the data
directory, an encrypted SQLite database (SQLCipher), field-level encryption of
sensitive columns, better key management and rotation (the encryption key is
currently a plain value in `.env`), and two-factor sign-in. See `agent.md` for the
trade-offs.
- **Settings → Delete all my data** revokes every connection at the provider and
  wipes your records.
