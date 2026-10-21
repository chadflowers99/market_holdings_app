# Market Holdings

A multi-user Streamlit app for lot-level stock portfolio tracking with Supabase sync.

## Features

- Lot-level holdings and sell tracking
- Collapsed-by-default Current Holdings toggle for lot tracking and realized P/L
- Current Holdings groups symbols alphabetically and lists each symbol's lots by price, highest first
- Permanent trade ledger
- Select a Trade History row and use Edit selected trade to open its edit/delete dialog; confirmed Delete All remains separate
- Trade dates can be edited while preserving time/timezone and validating buy/sell chronology
- Separate per-user cash ledger for deposits and withdrawals
- Email/password and Google OAuth login
- RLS-backed per-user data isolation

## Quick Start

Realized P/L uses lowest-buy-first matching separately for each symbol.
**Current Holdings** displays running realized P/L. Editing/deleting trades
recalculates P/L and rebuilds holdings; there is no standalone recalculation
control. Signed-in local actions write to the configured Supabase database.
**Reset P/L total** requires confirmation and saves the current realized total
as an account-specific baseline. The displayed total starts at zero across
devices; future realized gains/losses accumulate from there. No trades, holdings,
or cash records are changed. Editing/deleting older trades can still change the
displayed total because the baseline stays fixed. Guest resets are session-only.
Apply `supabase/migrations/20261023_reset_realized_pl.sql` once in the Supabase
SQL Editor before using this version. The migration creates the baseline table
and reset function; it does not reset any totals or modify existing trades.

1. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

2. Create local secrets file at `.streamlit/secrets.toml` with:

   ```toml
   SUPABASE_URL = "https://<your-project>.supabase.co"
   SUPABASE_ANON_KEY = "<your-anon-key>"
   ```

3. Run locally:

   ```powershell
   streamlit run app.py
   ```

4. Sign in and expand **Cash Ledger** to record deposits and withdrawals. Cash
   transactions are saved to Supabase separately from stock holdings. The
   ledger shows your available cash balance and newest-first transaction
   history with dates only inside the privacy toggle. Full timestamps remain
   stored for ordering. Guest-mode cash entries remain temporary.
   The Supabase cash table and `record_cash_transaction` function must already
   be installed; see `supabase/migrations/20261020_create_cash_transactions.sql`.
   **Show transaction history** is off by default. Inside history, **Clear all
   cash transactions** requires confirmation and resets only your cash to zero.
   To enable signed-in clearing, run
   `supabase/migrations/20261021_clear_cash_transactions.sql` in the Supabase SQL
   Editor once. Running the migration sets up deletion permissions/function;
   it does not clear any cash until you confirm the action in the app.
   Select a cash-history row and click **Edit selected transaction** to edit
   its date, type, amount, or note, or choose **Delete Transaction** in the
   dialog. Clear all remains a separate confirmed action. Changes that would
   make the current cash balance negative are rejected. Date edits preserve
   time/timezone; history still displays only the date.
   Apply `supabase/migrations/20261022_edit_cash_transactions.sql` once in the
   Supabase SQL Editor to enable signed-in edits and individual deletions.
   Applying this migration does not modify existing cash records.

## Google Sign-In Setup

1. In Google Cloud Console, create OAuth 2.0 Web application credentials and
   add `https://<project-ref>.supabase.co/auth/v1/callback` as an authorized redirect URI.
2. In Supabase Dashboard, open Authentication > Providers > Google, enable it,
   and enter the Google client ID and client secret.
3. In Supabase Dashboard, open Authentication > URL Configuration and add the
   deployed app URL and `http://localhost:8501` to Redirect URLs.
4. Optionally set `OAUTH_REDIRECT_URL` in Streamlit secrets to the deployed
   app URL. Otherwise, the app derives the current host automatically.

## Data Storage

All data syncs to Supabase PostgreSQL:

- `portfolio`: Active holdings
- `permanent_ledger`: Full trade history
- `cash_transactions`: Per-user cash deposits and withdrawals protected by RLS
- `realized_pl_baselines`: Per-user P/L reset baseline protected by RLS

## Streamlit Deployment Runbook

1. App source settings:
   - Repo: `chadflowers99/market_holdings_app`
   - Branch: `main`
   - Main file: `app.py`
2. App URL:
   - `https://pb-marketholdings.streamlit.app`
3. Streamlit Cloud secrets:
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
