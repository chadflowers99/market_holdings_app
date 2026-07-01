# Holdings

A multi-user Streamlit app for lot-level stock portfolio tracking with real-time Supabase sync.

## Features

- **Lot-Level Tracking**: Track every buy separately for precise cost-basis calculation
- **Real-time Sync**: Automatic cloud synchronization with Supabase
- **Multi-User**: Team collaboration with row-level security (RLS)
- **Authentication**: Email/password and Google OAuth login
- **Trade History**: Permanent ledger of all buy/sell transactions
- **Portfolio Analytics**: Realized/unrealized P&L tracking

## Quick Start

1. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

2. Set up environment:
   - Create `.streamlit/secrets.toml` with Supabase credentials
   - Get your credentials from [Supabase Dashboard](https://supabase.com)

3. Run:

   ```powershell
   streamlit run app.py
   ```

## Data Storage

All data syncs to Supabase PostgreSQL:

- `portfolio`: Active holdings (lot-level)
- `permanent_ledger`: Complete trade history

## Deployment

Deployed to Streamlit Cloud: [pb-stocktrade.streamlit.app](https://pb-stocktrade.streamlit.app)
pip install -r requirements.txt
```

## Run

Run Streamlit app:

```powershell
streamlit run app.py
```

Run Tkinter app:

```powershell
python enter_trade.py
```

## Notes

- Avoid running Streamlit and Tkinter at the exact same time to prevent simultaneous writes.
- If legacy aggregated CSV format is detected, the app auto-migrates to lot-level format.
