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
