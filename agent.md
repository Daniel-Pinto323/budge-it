# Personal Budget and Net Worth App — Project Handoff

## Purpose

Build a personal finance dashboard that brings transactions and balances from multiple institutions into one place, makes monthly spending easy to understand, and tracks net worth over time. The budgeting experience should take inspiration from the clarity of Piere’s Budgeting tab: categorized spending grouped into readable sections, explicit monthly allotments, and an at-a-glance indication of how much room remains or how far a category is over budget. Create an original interface; do not copy Piere’s branding or assets.

The first connected institutions should be Chase and Capital One. Investment accounts are a planned extension, with a useful first version focused on read-only balances and holdings. The web app should be comfortable on iPhone and installable to the Home Screen; a native iOS app can follow if the web experience proves useful.

## Product principles

- Show a trustworthy, understandable picture of spending and net worth.
- Make category budgets visible without requiring the user to build reports.
- Keep transaction categorization editable; automated categories are suggestions, not unchangeable truth.
- Make data freshness, pending transactions, and account connection problems visible.
- Use a provider such as Plaid for institution connections. Never ask for or store bank passwords.
- Start read-only. Do not initiate payments, transfers, trading, or investment advice.
- Keep a provider adapter boundary so another aggregator or manual CSV import can be added later.

## Scope and delivery sequence

### Phase 0 — Validate the integration path

1. Create a Plaid developer account and inspect the current product access, Sandbox, and pricing for Transactions, Auth/Link, Investments, and the institutions in scope.
2. Verify the exact Chase and Capital One entries and required products in the current Plaid Dashboard / `/institutions/get` results. Institution coverage and permissions can vary by product, account type, and production access; do not assume a generic coverage check guarantees each desired account will link.
3. Confirm which institution accounts are in scope (checking, savings, credit cards; brokerage/retirement later) and which countries/currencies matter.
4. Decide whether this is a single-user private tool or a multi-user product. Start single-user unless there is a concrete reason to support multiple households; the data model should still associate records with a user.
5. Build only with synthetic data until production access and deployment security are deliberately configured.

### Phase 1 — UI prototype with synthetic data

Build the dashboard and monthly budget views before connecting real accounts. Seed realistic sample accounts and transactions, then refine the mobile layout and terminology. The first home view should answer: “How much did I spend this month?”, “Which budgets have room?”, “What is over budget?”, “What is my current net worth?”, and “When did data last update?”

Core screens:

- **Overview:** month-to-date spending, income, net worth, recent transactions, budget status, and connection/sync freshness.
- **Budget:** month selector; category groups; spent / budget / remaining; progress bars; overspending state; category drill-down.
- **Transactions:** searchable/filterable list; date, merchant, account, amount, pending/posted state, category; edit category and mark transfer/reimbursement.
- **Accounts:** institution and account list, balances, included/excluded toggles, connection status, last successful update.
- **Net worth:** assets minus liabilities over time, with account-level breakdown and a clear “as of” time.
- **Settings:** budgets, category rules, account inclusion, data export, disconnect/delete data.

### Phase 2 — Web MVP and account linking

Recommended initial implementation, unless the repository or user constraints suggest otherwise:

- **Frontend:** Next.js + TypeScript, responsive UI, accessible components, PWA manifest and service worker only where useful.
- **Backend:** Next.js server routes or a small separate TypeScript API; keep all aggregator secrets and token operations server-side.
- **Database:** PostgreSQL with migrations; Prisma or Drizzle are both viable. Use managed Postgres for deployment.
- **Financial data:** Plaid Link + Transactions for checking, savings, and credit-card transactions; Accounts/Balance for account balances. Add Investments later.
- **Authentication:** secure session-based auth; use a reputable auth library/provider, even for a private app, rather than rolling password storage.
- **Hosting:** a deployment platform that supports HTTPS, server-side secrets, background jobs, and a public webhook endpoint.

Keep the provider-specific API behind an interface such as `FinancialDataProvider` (create link token, exchange public token, sync transactions, fetch accounts/balances, refresh/revoke connection). Domain logic and UI should consume normalized app records rather than raw provider responses.

Account connection flow:

1. Authenticated browser asks the server for a short-lived Link token.
2. Browser launches Plaid Link and user authenticates directly with the institution/provider flow.
3. Browser sends the temporary public token to the server after success.
4. Server exchanges it for the long-lived access token and encrypts the token before persistence.
5. Server stores the Item/institution metadata, fetches accounts, and starts the initial transaction sync.
6. Server accepts signed/verified provider webhooks, queues sync work, and updates sync status. Make handlers idempotent.
7. UI displays accounts and transactions when ready, with pending/history/loading/error states.

Transaction sync implementation notes:

- Prefer Plaid `/transactions/sync` for incremental added/modified/removed transactions; persist each Item’s cursor and handle pagination correctly.
- Upsert by provider transaction ID; process removed transactions and pending-to-posted replacements without double-counting.
- Make webhook-triggered work retryable and idempotent. Keep a periodic reconciliation job as a fallback.
- Transaction data is not necessarily real-time. Show the last successful sync and do not promise instant updates. On-demand refresh may require a paid/add-on entitlement.
- Seed realistic transactions in Plaid Sandbox and exercise transaction churn before connecting a real account.

### Phase 3 — Budgeting behavior and categorization

- Provide a sensible starter category set (e.g. Housing, Utilities, Groceries, Dining, Transportation, Health, Shopping, Subscriptions, Travel, Income, Transfers, Other), while allowing custom categories and groups.
- Create monthly category budgets with amount, period, and rollover setting (default no rollover until user chooses otherwise).
- Category remaining = budget amount minus included posted spending in the selected period. Define how pending items are shown and whether they affect “remaining”; make that choice consistent and visible.
- Income, transfers, credit-card payments, refunds, and reimbursements must not inflate ordinary spending. Add transaction flags and transfer pairing/manual correction.
- Use provider-supplied categories as an initial hint, then apply deterministic user-defined merchant/category rules. Persist the source/confidence and allow correction.
- When a user changes a category, offer to apply that choice to future matching transactions. Do not silently rewrite history without a clear rule.
- Budget UI should make status legible with amount and text, not color alone: `$240 left`, `$35 over`, etc.
- Consider a monthly “needs attention” section for uncategorized items and categories near/over limit.

### Phase 4 — Net worth and investments

- Compute net worth from included asset balances minus liabilities at a point in time; avoid summing investment holdings and an investment account’s reported cash/balance twice.
- Store balance snapshots with observation timestamps to render history. Mark stale balances and allow manual assets/liabilities as an explicit later feature.
- Add Plaid Investments only after core budgets are reliable: display account totals and holdings, with an “as of” timestamp. Investments data can update periodically and holding changes may arrive after market hours; avoid presenting it as live quotes.
- Keep investment transactions distinguishable from household expenses so buys/sells and transfers do not pollute monthly spending budgets.
- Treat values as estimates from linked data, not tax/accounting statements or financial advice.

### Phase 5 — iPhone app path

Start with a responsive web app and test it in Safari on an iPhone. Add PWA metadata/icons and ensure authentication, Link flow, charts, and navigation work well on a narrow screen. User can add it to the Home Screen. If later requirements demand native notifications, widgets, deeper OS integration, or App Store distribution, package the existing web frontend with Capacitor or build a native SwiftUI client against the same backend API. Confirm the financial provider’s supported Link flow and platform requirements before committing to native packaging.

## Initial data model

Adapt names to the chosen ORM, but retain these concepts:

- `User`: identity and preferences (base currency, timezone).
- `Connection`: user, provider, provider Item ID, institution, encrypted provider credential, status, consent/error metadata, last successful sync.
- `Account`: connection, provider account ID, display name, type/subtype, mask, currency, current/available balance, balance observation time, include-in-net-worth flag.
- `Transaction`: account, provider transaction ID, amount (document provider sign convention and normalize), currency, authorized/posted dates, merchant/name, pending flag, category, category source, transfer/refund flags, removed status.
- `Category` and `CategoryRule`: user-owned category/group and deterministic merchant/name matching rules.
- `Budget`: user, category, month/period, amount, rollover configuration.
- `BalanceSnapshot`: account, balance, timestamp, source.
- `SyncCursor` or fields on `Connection`: cursor, last attempt/success, error code, retry state.
- `WebhookEvent` / job record: event ID or dedupe key, received/processed status, timestamps; avoid retaining unnecessary raw financial payloads.

Use provider IDs for deduplication. Scope every query to the authenticated user. Store money as integer minor units or exact decimal values, never binary floating point.

## Security and privacy baseline

- Never store bank login credentials. Use the institution/provider authorization flow.
- Keep Plaid client secret and access tokens on the server only; encrypt stored access tokens with a managed key/KMS or equivalent envelope encryption. Never commit secrets or put them in frontend environment variables.
- Require HTTPS, secure/HttpOnly/SameSite cookies, CSRF protection where applicable, input validation, authorization checks, and rate limits on sensitive endpoints.
- Verify webhook authenticity before processing; use a queue/retry strategy, deduplicate events, and log metadata without exposing tokens or transaction details.
- Minimize stored fields and retention. Provide disconnect/revoke and account/data deletion paths, plus a user export.
- Avoid sending bank data to analytics, error reporting, or LLM services. Redact logs and crash reports.
- Define backups, access controls, dependency updates, and an incident response plan before production use. Do not claim a compliance certification unless actually obtained.

## Working definition of MVP done

- User can sign in and link supported Chase and Capital One accounts through provider-hosted authorization.
- Accounts and transaction history appear in one dashboard, with safe incremental refresh and duplicate/removal handling.
- User can edit categories, define monthly budgets, and see remaining/over-budget amounts.
- Transfers, refunds, and pending items have deliberate behavior and do not produce misleading spend totals.
- Account balances and liabilities can be included/excluded, with timestamps; net worth is calculated without double-counting.
- Sync health and disconnected/reauthentication states are understandable.
- Core flows work on desktop and iPhone Safari; data export and disconnect/delete are available.
- No production secrets are in source control; server-side token handling and webhook validation have been reviewed.

## Suggested first implementation tasks

1. Inspect the repo and any `AGENTS.md` instructions before creating project files; this handoff was written in a workspace with no app project detected.
2. Decide repo location/name and create a Next.js TypeScript app with linting, formatting, and database migrations.
3. Build an original mobile-first Budget screen with seeded mock data and the budget calculations above.
4. Add database models and seed data; implement transactions, categories, budgets, accounts, and snapshots.
5. Add authenticated server-side Plaid Sandbox Link flow, token exchange, encrypted credential storage, account import, and `/transactions/sync`.
6. Implement verified webhooks, retryable sync jobs, sync status UI, account disconnect, and data deletion/export.
7. Verify Chase and Capital One product coverage/access in the current provider dashboard and run Sandbox-to-development integration before production access.
8. Add balances/net worth, then optionally Investments holdings and transactions.
9. Deploy a private HTTPS staging instance, validate iPhone Safari/PWA behavior, then make a deliberate production-readiness review before using live financial data.

## Open decisions for the next session

- Is the intended deployment private for one user, or should multiple users/households be supported?
- Which account types are needed in the first release: checking/savings, credit cards, loans?
- Should budget limits be user-entered only, or should there be an optional suggested starter plan?
- Should pending card authorizations count against remaining budget?
- Which data retention/deployment region and monthly operating-cost ceiling are acceptable?
- Should the first version support importing CSV statements as a fallback?

## Reference links checked 2026-11-10

- Plaid Transactions overview and sync flow: https://plaid.com/docs/transactions/
- Plaid Transactions API and webhooks: https://plaid.com/docs/api/products/transactions/
- Plaid institution coverage explorer: https://plaid.com/docs/institutions/
- Plaid Investments overview: https://plaid.com/docs/investments/
- Plaid OAuth institution support (includes Chase and Capital One; verify current product-specific coverage/access in Dashboard): https://support.plaid.com/hc/en-us/articles/15687521555607-What-OAuth-institutions-does-Plaid-support
- Plaid Link token flow: https://plaid.com/docs/link/
- Apple Safari web apps on iOS: https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/SafariWebContent/ConfiguringWebApplications/ConfiguringWebApplications.html

## Notes for the next agent

This file records a plan, not implemented code or verified production approval. Begin by checking the repository state and local instructions. Keep live financial data out of development fixtures, verify current aggregator product access and institution/account support before relying on it, and tell the user what was actually implemented versus planned.
