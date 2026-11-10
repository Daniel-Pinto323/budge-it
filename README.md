# budge it

An original, mobile-friendly personal-budget MVP. It uses synthetic data only and runs entirely in the browser—no bank connection, server, account, or real financial data is involved.

## Run it

From this folder:

```bash
python3 -m http.server 8080
```

Open [http://localhost:8080](http://localhost:8080). Opening `index.html` directly also works for most features.

## Quick walkthrough

1. **Overview:** Check this month’s spending, remaining planned budget, net worth, current budget status, and recent activity.
2. **Budget:** Select the three-dot button beside a category to change its monthly limit. Use **+ Add category** to add a category with a group and monthly limit. **Manage categories** lets you rename, regroup, change the limit for, or delete any starter or custom category. Deleting a category preserves its transaction records but labels directly affected ones **Uncategorized**.
3. **Transactions:** Search or filter activity; use the category dropdown to correct a transaction. Click **+ Add transaction** to log a cash purchase, reimbursement, income, or another manual item.
4. **Budget status:** Every transaction stays in the ledger. Press **Included** to mark a duplicate, transfer-like edge case, or other item as **Hidden**. Hidden items remain visible and are included in CSV exports, but they do not count toward the category or month’s budget. Press **Hidden** to restore it.
5. **Accounts / Net worth:** Browse the synthetic accounts and toggle their inclusion. The connected-bank controls are placeholders only.

## Data and reset behavior

- Budget/category edits, manual transactions, category changes, and budget-hidden choices are stored in this browser’s local storage.
- **Reset demo changes** on the Budget screen restores the sample budgets and transactions.
- **Export CSV** downloads the currently stored transaction log, including each item’s budget status.
- The project deliberately contains no Plaid credentials, live institution connections, or financial-data backend.

## Project files

- `index.html` — screens, dialogs, and accessible page structure
- `styles.css` — desktop and mobile styling
- `app.js` — seeded data, budget calculations, and browser interactions
- `agent.md` — product direction, delivery status, and next implementation work
