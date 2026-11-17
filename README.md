# budge it

A private, self-hosted personal budget and net-worth app. It pulls accounts and
transactions from your banks through **Plaid**, lets you set monthly
category budgets, and tracks net worth over time. Bank passwords never touch this
app: you sign in inside the provider's own flow and the server only holds a
read-only token, encrypted at rest.

- `server.py` — entry point and admin commands
- `budgeit/` — server: config, SQLite storage, auth, sync, provider adapters, HTTP API
- `static/` — the browser app (plain HTML/CSS/JS, no build step)
- `deploy/` — systemd unit, Caddy config, and the auto-update script and timer for self-hosting
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

**Other providers.** Plaid is the only supported provider. The code includes an
experimental MX adapter (`MX_CLIENT_ID`, `MX_API_KEY` in `.env`), but it has only
been run against a fake server, MX production access requires an agreement with
MX, and it is out of scope for this project. Treat it as a possible future
backup, not a working feature.

**Freshness.** Data arrives when the provider has it, not in real time. The server
re-syncs every `SYNC_INTERVAL_MINUTES` (default 6 hours), on demand from the
Sync buttons, and immediately on provider webhooks when the server is reachable at
an `https://` `PUBLIC_URL`.

## Manual data while a provider is unavailable

**Account types.** The Accounts tab is grouped into **Credit cards**, **Bank
accounts**, **Investments** and **Loans & other**, each with its own total. When you
import a CSV the type is assumed from the file's columns and shown in the dialog
("Detected: …") so you can change it before importing; afterwards every account has a
**Type** dropdown (CSV, manual, and Plaid/MX accounts alike, so a provider's
guess can be corrected and the sync will not undo it). The type decides the section,
and credit cards are the ones that count payments from other accounts.

| File | Columns that identify it | Assumed type |
|---|---|---|
| Chase card activity | `Transaction Date, Post Date, Description, Category, Type, Amount, Memo` | Credit card |
| Capital One credit card | `Transaction Date, Posted Date, Card No., Description, Category, Debit, Credit` | Credit card |
| Capital One checking / savings | `Account Number, Transaction Description, Transaction Date, Transaction Type, Transaction Amount, Balance` | Bank account (savings if the name says so) |

No investment export format is recognised yet; choose **Investment** by hand
(or add the account under Net worth → Manual accounts).

**Import CSVs** (Accounts → ↑ Import CSV). The three formats above are recognised by
their column headers. Re-importing the same
account skips rows already imported. Categories are guessed from the merchant text
using the keyword list in **Settings → Auto-categorization keywords** (editable);
categories you set yourself are never overwritten. Imported CSV rows can be edited
(merchant, date, amount, category) from the Activity page.

**Balances follow the transactions.** Each CSV account's balance is
`anchor + transactions dated after the anchor date`, and it feeds net worth like
any other account:

- A **Capital One bank** file carries a `Balance` column, so its newest row anchors the
  account automatically.
- A **Chase** file has no balance. Until you press **Set balance** (enter what the
  bank shows, whether you owe it, and the date), the card shows an estimate from
  imported transactions only. After that, only transactions dated after your date
  move it. Spending lowers a balance; refunds and payments raise it.
- **Card payments from other accounts.** A Transfer-category transaction in another
  account (for example `Withdrawal from CHASE CREDIT CRD AUTOPAY` in checking)
  reduces a card's debt when its text contains the card's "payments containing…"
  phrase (default: the first word of the account name, such as `chase`; editable
  under the card). If the card's own export lists the same payment (same amount, within
  5 days) it is counted once. A payment that matches several cards and is listed by
  none is left out rather than guessed, so give each card a more specific phrase.
- Only **CSV-imported** accounts work this way. Plaid and MX accounts keep the
  balances their provider reports.
- Manual transactions are not tied to an account yet, so they do not move any
  balance.

**Manual accounts** (Net worth → Manual accounts) track things no provider links,
such as a 401(k), home or loan. Enter the amount whenever it changes; each value is
kept in the net worth history.

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
   registered automatically).

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

Manually:

```bash
cd /opt/budgeit && git pull --ff-only && systemctl restart budgeit
```

#### Automatic updates (home server pulls from GitHub)

The workflow is: edit and test on the laptop, push to `main`, and the server picks
the change up on its own within about ten minutes. A systemd timer runs
`deploy/budgeit-update` every 10 minutes (and 2 minutes after boot). Each run:

1. `git fetch`es `main`. If there is nothing new it exits silently.
2. Fast-forwards the checkout (`git merge --ff-only`; it never rewrites history
   or merges, so a diverged server checkout is an error, not a surprise).
3. Runs the test suite. **If any test fails, the checkout is rolled back to the
   previous commit and the running app is left alone.**
4. Copies the database to `/var/backups/budgeit/pre-update-<time>-<old commit>.sqlite3`
   (mode `0600`, newest 10 kept) because new code may upgrade the schema on
   startup and that cannot be undone. This is a same-machine safety net, not an
   off-server backup; see Backups above.
5. Restarts the `budgeit` service.

`.env` and the database are untracked, so they survive updates.

**Install (once, as root on the server):**

```bash
cd /opt/budgeit
install -m 755 deploy/budgeit-update /usr/local/bin/budgeit-update
cp deploy/budgeit-update.service deploy/budgeit-update.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now budgeit-update.timer
```

The checkout needs `git fetch` to work as root, so the GitHub deploy key (or a
public HTTPS remote) must be usable by root. If git reports "dubious ownership",
run `git config --global --add safe.directory /opt/budgeit`. The script itself
only changes when you reinstall it; after pulling a change to
`deploy/budgeit-update`, rerun the `install` line.

**Operating it:**

```bash
systemctl list-timers budgeit-update.timer     # next and last run
systemctl start budgeit-update.service         # check for updates right now
journalctl -u budgeit-update -n 50             # what happened (updates, test failures)
systemctl disable --now budgeit-update.timer   # turn auto-updates off
```

**Settings** (environment variables, set with `systemctl edit budgeit-update.service`
and an `[Service]` / `Environment=` line): `BRANCH` (default `main`; set it to a
`release` branch and push there only when a change is ready, if you do not want
every push to go live), `APP_DIR`, `DATA_DIR`, `BACKUP_DIR`, `KEEP_BACKUPS`.

**Limits to know about:** a failed update keeps retrying every 10 minutes and
logs the test output each time until you push a fix. Dependencies are the Debian
packages from step 2 of the install; if a change to `requirements.txt` adds one,
the script logs a note and you must `apt install` it yourself, or the restarted
app will fail to start. To roll back a bad release that passed the tests, `git
reset --hard <good commit>` on the server and (so it does not move forward again)
push a revert to the branch the server follows. To restore data, stop the service
and copy a backup over `/var/lib/budgeit/budgeit.sqlite3`.

## Security notes

- Logins use scrypt-hashed passwords (12+ characters), rate-limited sign-in,
  HttpOnly session cookies and CSRF tokens. There is no two-factor login yet, so
  if the app is on the public internet use a long unique password, and consider
  an access proxy in front.
- Plaid access tokens are encrypted with `BUDGEIT_ENCRYPTION_KEY`. Plaid webhooks
  are signature-verified.
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
