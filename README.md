# Market Holdings

A multi-user Streamlit app for lot-level stock portfolio tracking with Supabase sync.

## Features

- Lot-level holdings and sell tracking
- Permanent trade ledger
- Email/password and Google OAuth login
- RLS-backed per-user data isolation

## Quick Start

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

## Data Storage

All data syncs to Supabase PostgreSQL:

- `portfolio`: Active holdings
- `permanent_ledger`: Full trade history

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
4. Supabase Auth redirect configuration:
   - `https://pb-marketholdings.streamlit.app`
   - `https://pb-flexbudget.streamlit.app`
   - Optional local dev: `http://localhost:8501`
