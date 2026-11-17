# budge it

A private, self-hosted budget and net-worth tracker. One small Python process, one
SQLite file, and a plain HTML/CSS/JS front end with no build step. It runs on a
home server, keeps your data on that machine, and works on a phone as a Home Screen
web app.

You can fill it three ways: **import CSV statements**, **add accounts by hand**, or
**link banks through Plaid** (read-only; your bank password never reaches this app).

## What it does

- **Budgets.** Monthly limits per category, with status that turns amber ("Almost
  there") and red ("Budget hit", "Budget passed!"). Click a category to preview its
  latest transactions for the month, or click an Overview tile to jump to it.
- **Activity.** Search and filter every transaction, edit them, add notes (a bank memo
  carries over), and hide any from your budget with the eye. Transfers such as card
  payments are hidden from budgets by default.
- **Auto-categorization.** Merchants are filed by an editable keyword list (Settings),
  your own "Always" rules, and the bank's category, in that order. Categories you set
  by hand are never overwritten.
- **Accounts by type.** Credit cards, bank accounts, investments and loans, each with a
  total. The type is guessed from the CSV format and can be changed.
- **Net worth.** Balances from linked accounts, CSV imports and manual accounts
  (401(k), home, loans) roll up into one history.
- **Your data stays yours.** Export everything as CSV, or delete it all from Settings.

## Status

- Built and tested end to end: budgeting, activity, CSV import (Chase and Capital One),
  manual accounts, net worth, Plaid linking and sync, multi-user logins, and
  self-hosting with automatic updates.
- Plaid was verified in Sandbox. **Production access is still pending**, so live data
  currently comes from CSV imports and manual accounts.
- The experimental MX integration was removed; Plaid is the only provider.

## Quick start

Requires Python 3.10+ and the `cryptography` package.

```bash
pip install -r requirements.txt          # or: apt install python3-cryptography
cp .env.example .env
python3 server.py generate-key           # paste the output into BUDGEIT_ENCRYPTION_KEY in .env
python3 server.py create-user you@example.com
python3 server.py
```

Open <http://localhost:8080> and sign in. **Settings → Load sample data** adds
synthetic accounts so you can look around first. Run the tests with
`python3 -m unittest discover -s tests`.

## Getting data in

**CSV import** (Accounts → ↑ Import CSV). The format is recognised from the file's
columns, and re-importing the same account never duplicates transactions.

| File | Identifying columns | Treated as |
|---|---|---|
| Chase card activity | `Transaction Date, Post Date, Description, Category, Type, Amount, Memo` | Credit card |
| Capital One credit card | `Transaction Date, Posted Date, Card No., Description, Category, Debit, Credit` | Credit card |
| Capital One checking / savings | `Account Number, Transaction Description, Transaction Date, Transaction Type, Transaction Amount, Balance` | Bank account |

**Balances.** A CSV account's balance is a known balance on a date plus the
transactions dated after it. Capital One bank files include a `Balance` column, so
that is automatic. Card files do not, so press **Set current balance** once per card;
until then it shows an estimate from the imported rows alone. Each account is
computed from its own file, so import each card's own statement to see its payments.
Only CSV accounts work this way; Plaid accounts keep the balances Plaid reports.

**Manual accounts** (Net worth, or Accounts → + Manual account) cover anything with no
feed. Update the amount whenever it changes; each value is kept in the history.

**Plaid.** Put `PLAID_CLIENT_ID` and a sandbox `PLAID_SECRET` in `.env` and link
Plaid's test bank (`user_good` / `pass_good`). For real banks, apply for production
access, complete the OAuth registration banks like Chase and Capital One require, then
set `PLAID_ENV=production`. To link from a phone, also set `PLAID_REDIRECT_URI` to your
public URL plus `/` and add it under Allowed redirect URIs in the Plaid Dashboard.
Data arrives on a timer (`SYNC_INTERVAL_MINUTES`, default 6 hours), on the Sync
buttons, and on webhooks when the server is reachable at an `https://` `PUBLIC_URL`.

## Self-hosting

### How the maintainer hosts it

This repository is deployed on a home server as follows:

- **Where.** A dedicated unprivileged Debian 13 LXC container on Proxmox (nesting on,
  start at boot, container firewall on), running only this app.
- **How it runs.** The code lives in `/opt/budgeit` and runs as the `budgeit` systemd
  service under a locked-down system user (`deploy/budgeit.service`). The database is
  in `/var/lib/budgeit`; `.env` is root-owned and readable only by that user. The only
  dependency is the Debian package `python3-cryptography`.
- **Access.** Private, through **Tailscale**. The app listens on `127.0.0.1:8080` and
  `tailscale serve` publishes it over HTTPS to the maintainer's own devices. Nothing is
  exposed to the internet, which also means Plaid webhooks cannot reach it, so data
  refreshes on the timer. Getting Tailscale working inside an unprivileged LXC took
  three steps: pass `/dev/net/tun` through to the container in Proxmox, run
  `tailscale up`, then enable MagicDNS and HTTPS certificates in the Tailscale admin
  console and run `tailscale serve --bg 8080`. `.env` then has `HOST=127.0.0.1`,
  `PUBLIC_URL=https://<machine>.<tailnet>.ts.net` and `TRUST_PROXY=1`.
- **Updates.** Changes are made and tested on a laptop and pushed to GitHub. The
  server pulls them itself every 10 minutes (see Automatic updates below), using a
  read-only GitHub deploy key.
- **Data.** Plaid Sandbox ran cleanly. Until Production access arrives the server holds
  CSV imports and manual accounts.
- **Not done yet.** Scheduled off-server backups. Every update takes a local database
  snapshot first, but a copy that survives losing the container is still to be set up.

### Hosting your own fork

You need an always-on machine (a small VM, a Raspberry Pi, or a container is plenty:
about 1 core, 512 MB RAM and a few GB of disk) and a way to reach it over HTTPS.

1. **Fork and clone.** Fork this repo and clone it to the server, for example into
   `/opt/budgeit`. For a private fork, add a read-only deploy key to the repo.
2. **Install.**
   ```bash
   apt install python3 python3-cryptography git
   useradd --system --home /opt/budgeit --shell /usr/sbin/nologin budgeit
   cp /opt/budgeit/.env.example /opt/budgeit/.env
   chown root:budgeit /opt/budgeit/.env && chmod 640 /opt/budgeit/.env
   ```
3. **Configure `.env`.** Set `BUDGEIT_ENCRYPTION_KEY` (from `python3 server.py
   generate-key`; back it up separately from the database), `DATA_DIR=/var/lib/budgeit`,
   `PUBLIC_URL`, `TRUST_PROXY=1`, and your Plaid keys if you have them. Leave
   `HOST=127.0.0.1`.
4. **Run it as a service.**
   ```bash
   cp /opt/budgeit/deploy/budgeit.service /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now budgeit
   cd /opt/budgeit && runuser -u budgeit -- python3 server.py create-user you@example.com
   ```
   The unit assumes `/opt/budgeit` and `/var/lib/budgeit`; edit it if you use other
   paths. Each login only sees its own data, so you can add family with `create-user`.
5. **Put HTTPS in front** (the app itself speaks plain HTTP on loopback). Choose one:

   | Option | Good for | Notes |
   |---|---|---|
   | **Tailscale** (`tailscale serve`) | Private use from your own devices | No open ports; no webhooks |
   | **Cloudflare Tunnel** | Reaching it from anywhere without open ports | Works behind CGNAT; consider Cloudflare Access, leaving `/api/webhooks/*` open |
   | **Caddy + port forwarding** | A public hostname you control | Uses `deploy/Caddyfile`; needs a public IP and DNS |

6. **Tell Plaid your URL.** Add your redirect URI in the Plaid Dashboard. Webhooks are
   registered automatically when `PUBLIC_URL` is `https://`.

### Automatic updates

A systemd timer can keep the server on the latest commit of a branch. Every 10 minutes
`deploy/budgeit-update` does this:

1. Fetch the branch (default `main`); stop quietly if nothing is new.
2. Fast-forward the checkout, never merging or rewriting history.
3. Run the test suite. **If anything fails, roll back and leave the running app alone.**
4. Copy the database to `/var/backups/budgeit/` (newest 10 kept), because new code may
   upgrade the schema on start-up and that cannot be undone.
5. Restart `budgeit`.

Install it once as root on the server:

```bash
cd /opt/budgeit
install -m 755 deploy/budgeit-update /usr/local/bin/budgeit-update
cp deploy/budgeit-update.service deploy/budgeit-update.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now budgeit-update.timer
```

Root runs the fetch, so your deploy key (or a public HTTPS remote) must work for root;
if git complains about "dubious ownership", run `git config --global --add safe.directory
/opt/budgeit`. Re-run the `install` line after pulling a change to the script itself.

```bash
systemctl list-timers budgeit-update.timer     # next and last run
systemctl start budgeit-update.service         # check now
journalctl -u budgeit-update -n 50             # what happened
systemctl disable --now budgeit-update.timer   # turn it off
```

To avoid every push going live, set `BRANCH=release` (via `systemctl edit
budgeit-update.service`) and push to that branch only when a change is ready. Other
variables: `APP_DIR`, `DATA_DIR`, `BACKUP_DIR`, `KEEP_BACKUPS`. A failing update retries
every 10 minutes until you push a fix. The script cannot install dependencies; if
`requirements.txt` gains one it logs a note and you install the package yourself. To
roll back a release that passed the tests, `git reset --hard <good commit>` on the
server and push a revert to the followed branch.

### Backups

Everything lives in `DATA_DIR/budgeit.sqlite3` plus the encryption key in `.env`. Back
up both and keep a copy off the machine:

```bash
sqlite3 /var/lib/budgeit/budgeit.sqlite3 ".backup '/root/budgeit-$(date +%F).sqlite3'"
```

A hypervisor snapshot of the whole container also captures both; encrypt it if it
leaves your home, since the key and the data sit together. Without the key, saved Plaid
tokens cannot be read and each bank has to be relinked. To restore, stop the service and
copy a backup over `budgeit.sqlite3`.

## Security

- Passwords are scrypt-hashed (12+ characters); sign-in is rate-limited; sessions use
  HttpOnly cookies with CSRF tokens. There is no two-factor login yet, so on the public
  internet use a long unique password and consider an access proxy.
- Plaid access tokens are encrypted with `BUDGEIT_ENCRYPTION_KEY`, and Plaid webhooks
  are signature-verified. Connections are read-only; the app cannot move money.
- Only the Plaid tokens are encrypted at rest. Transactions, balances and account names
  are plain SQLite rows protected by file permissions (`0600`), the container boundary
  and private network access. Treat the database and its backups as sensitive.
- **Settings → Delete all my data** revokes every Plaid connection and wipes your records.
- Not implemented yet: encrypted backups, an encrypted data volume or SQLCipher,
  encryption key rotation, and two-factor sign-in. `agent.md` discusses the trade-offs.

## Project layout

- `server.py`: entry point and admin commands (`generate-key`, `create-user`)
- `budgeit/`: server code (config, SQLite storage and migrations, auth, sync, CSV import,
  balances, Plaid adapter, HTTP API)
- `static/`: the browser app
- `deploy/`: systemd units, Caddy config, and the auto-update script and timer
- `tests/`: end-to-end tests against a fake Plaid server
- `agent.md`: the project plan and detailed implementation status
