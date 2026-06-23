from datetime import datetime
import json
from pathlib import Path

import streamlit as st
from supabase import create_client, Client
from supabase.client import ClientOptions

# Load Supabase credentials from secrets
SUPABASE_URL = st.secrets.get("SUPABASE_URL")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY")

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in .streamlit/secrets.toml")
    st.stop()

if "your-project" in SUPABASE_URL or "your-project-ref" in SUPABASE_URL:
    st.error(
        "SUPABASE_URL in .streamlit/secrets.toml is still a placeholder. "
        "Use your real project URL from Supabase Settings > API."
    )
    st.stop()

if "your_anon_key_here" in SUPABASE_ANON_KEY:
    st.error(
        "SUPABASE_ANON_KEY in .streamlit/secrets.toml is still a placeholder. "
        "Use your real anon/publishable key from Supabase Settings > API."
    )
    st.stop()


BASE_DIR = Path(__file__).resolve().parent
AUTH_STORAGE_FILE = BASE_DIR / ".streamlit" / "supabase_auth_storage.json"


class FileAuthStorage:
    def __init__(self, storage_file):
        self.storage_file = storage_file

    def _read(self):
        if not self.storage_file.exists():
            return {}
        try:
            return json.loads(self.storage_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data):
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        self.storage_file.write_text(json.dumps(data), encoding="utf-8")

    def get_item(self, key):
        return self._read().get(key)

    def set_item(self, key, value):
        data = self._read()
        data[key] = value
        self._write(data)

    def remove_item(self, key):
        data = self._read()
        data.pop(key, None)
        self._write(data)

# Initialize Supabase client.
# Use a file-backed auth storage adapter so the PKCE code verifier survives a full-page OAuth redirect.
@st.cache_resource
def get_supabase_client() -> Client:
    return create_client(
        SUPABASE_URL,
        SUPABASE_ANON_KEY,
        options=ClientOptions(
            flow_type="pkce",
            storage=FileAuthStorage(AUTH_STORAGE_FILE),
        ),
    )


supabase: Client = get_supabase_client()


def auth_ui():
    """Displays login/signup UI and manages authentication state."""
    if "user" not in st.session_state:
        st.session_state.user = None

    if st.session_state.user:
        return st.session_state.user

    # Restore session from file-backed storage after page refresh
    if not st.session_state.user:
        try:
            existing = supabase.auth.get_session()
            if existing and existing.user:
                st.session_state.user = existing.user
                st.session_state.access_token = existing.access_token
                return st.session_state.user
        except Exception:
            pass

    oauth_error = st.query_params.get("error")
    oauth_error_description = st.query_params.get("error_description")
    if oauth_error:
        st.error(
            f"GitHub login failed: {oauth_error}"
            + (f" ({oauth_error_description})" if oauth_error_description else "")
        )

    # Handle OAuth callback from Supabase (PKCE flow).
    auth_code = st.query_params.get("code")
    if auth_code:
        try:
            response = supabase.auth.exchange_code_for_session({"auth_code": auth_code})
            if response and response.user:
                st.session_state.user = response.user
                st.session_state.access_token = response.session.access_token if response.session else None
                st.query_params.clear()
                st.rerun()
            else:
                st.error("GitHub login failed: no user returned from Supabase callback.")
        except Exception as e:
            st.error(f"GitHub login callback failed: {str(e)}")

    st.markdown("### Authentication")
    auth_tab1, auth_tab2, auth_tab3 = st.tabs(["Login", "Sign Up", "GitHub"])

    with auth_tab1:
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_password")

        if st.button("Log In", key="login_button"):
            try:
                response = supabase.auth.sign_in_with_password(
                    {
                        "email": email,
                        "password": password,
                    }
                )
                st.session_state.user = response.user
                st.session_state.access_token = response.session.access_token
                st.rerun()
            except Exception as e:
                st.error(f"Login failed: {str(e)}")

    with auth_tab2:
        email = st.text_input("Email", key="signup_email")
        password = st.text_input("Password", type="password", key="signup_password")

        if st.button("Sign Up", key="signup_button"):
            try:
                supabase.auth.sign_up(
                    {
                        "email": email,
                        "password": password,
                    }
                )
                st.success("Sign up successful! Please check your email to confirm.")
            except Exception as e:
                st.error(f"Sign up failed: {str(e)}")

    with auth_tab3:
        current_url = getattr(st.context, "url", None)
        if current_url:
            redirect_to = current_url.split("?", 1)[0].rstrip("/") + "/"
        else:
            app_base_url = st.secrets.get("APP_BASE_URL", "http://localhost:8501")
            redirect_to = f"{app_base_url.rstrip('/')}/"
        oauth_response = supabase.auth.sign_in_with_oauth(
            {
                "provider": "github",
                "options": {
                    "redirect_to": redirect_to,
                },
            }
        )
        authorize_url = oauth_response.url

        st.write("Use your GitHub account to sign in.")
        st.caption(f"GitHub will redirect back to: {redirect_to}")
        st.link_button("Continue with GitHub", authorize_url, use_container_width=True)
        st.caption(
            "If this does not work, enable GitHub in Supabase Auth Providers and add your app URL"
            " to Supabase Redirect URLs."
        )

    return None


def load_portfolio():
    """Reads active lots from Supabase portfolio table for current user."""
    if not st.session_state.get("user"):
        return []

    try:
        user_id = st.session_state.user.id
        response = supabase.table("portfolio").select("*").eq("user_id", user_id).execute()
        return response.data if response.data else []
    except Exception as e:
        st.error(f"Failed to load portfolio: {str(e)}")
        return []


def save_portfolio_row(lot):
    """Inserts or updates a single lot in Supabase portfolio table."""
    if not st.session_state.get("user"):
        return False

    try:
        user_id = st.session_state.user.id
        lot_with_user = {**lot, "user_id": user_id}

        # Try to update if it exists, otherwise insert
        supabase.table("portfolio").upsert(lot_with_user).execute()
        return True
    except Exception as e:
        st.error(f"Failed to save lot: {str(e)}")
        return False


def delete_lot_from_db(symbol, last_updated):
    """Deletes a lot from Supabase portfolio table."""
    if not st.session_state.get("user"):
        return False

    try:
        user_id = st.session_state.user.id
        supabase.table("portfolio").delete().eq("user_id", user_id).eq("symbol", symbol).eq(
            "last_updated", last_updated
        ).execute()
        return True
    except Exception as e:
        st.error(f"Failed to delete lot: {str(e)}")
        return False


def log_to_db(symbol, action, qty, price, avg_buy, pl_value):
    """Appends a permanent trade record to Supabase permanent_ledger."""
    if not st.session_state.get("user"):
        return False

    try:
        user_id = st.session_state.user.id
        now = datetime.now().isoformat()

        record = {
            "user_id": user_id,
            "timestamp": now,
            "symbol": symbol,
            "action": action,
            "quantity": qty,
            "price": price,
            "avg_buy_price": avg_buy,
            "realized_pl": pl_value,
        }

        supabase.table("permanent_ledger").insert(record).execute()
        return True
    except Exception as e:
        st.error(f"Failed to log trade: {str(e)}")
        return False


def handle_trade(action, symbol, quantity, price):
    """Processes BUY or SELL trades with lowest-buy-first FIFO matching."""
    symbol = symbol.strip().upper()
    now = datetime.now().isoformat()
    portfolio = load_portfolio()

    if action == "buy":
        new_lot = {
            "symbol": symbol,
            "quantity": quantity,
            "avg_price": price,
            "last_updated": now,
        }
        save_portfolio_row(new_lot)
        log_to_db(symbol, "BUY", quantity, price, price, 0.0)
        return True, f"Bought {quantity} shares of {symbol} @ ${price:.2f}."

    symbol_lots = [lot for lot in portfolio if lot["symbol"] == symbol and lot["quantity"] > 0]
    total_qty = sum(lot["quantity"] for lot in symbol_lots)

    if total_qty <= 0:
        return False, f"No holdings found for {symbol}."

    if quantity > total_qty:
        return False, f"Cannot sell {quantity} shares. You only own {total_qty} shares of {symbol}."

    remaining_to_sell = quantity
    total_realized_pl = 0.0

    for lot in sorted(symbol_lots, key=lambda x: (x["avg_price"], x["last_updated"])):
        if remaining_to_sell <= 0:
            break

        consumed = min(lot["quantity"], remaining_to_sell)
        lot_realized_pl = (price - lot["avg_price"]) * consumed
        total_realized_pl += lot_realized_pl

        log_to_db(symbol, "SELL", consumed, price, lot["avg_price"], lot_realized_pl)

        lot["quantity"] -= consumed
        lot["last_updated"] = now

        if lot["quantity"] <= 0:
            delete_lot_from_db(symbol, lot["last_updated"])
        else:
            save_portfolio_row(lot)

        remaining_to_sell -= consumed

    pl_status = "Profit" if total_realized_pl >= 0 else "Loss"
    return True, f"Sold {quantity} shares of {symbol}. Realized {pl_status}: ${abs(total_realized_pl):.2f}."


def load_running_realized_pl():
    """Returns running realized P/L by symbol and overall total from Supabase."""
    if not st.session_state.get("user"):
        return {}, 0.0

    try:
        user_id = st.session_state.user.id
        
        # Query permanent_ledger and aggregate by symbol
        response = supabase.table("permanent_ledger").select("symbol, realized_pl").eq(
            "user_id", user_id
        ).execute()

        pl_by_symbol = {}
        for row in response.data:
            symbol = row["symbol"]
            pl = float(row.get("realized_pl", 0.0))
            pl_by_symbol[symbol] = pl_by_symbol.get(symbol, 0.0) + pl

        total_pl = sum(pl_by_symbol.values())
        return pl_by_symbol, total_pl
    except Exception as e:
        st.error(f"Failed to load P/L: {str(e)}")
        return {}, 0.0


# Main App
st.set_page_config(page_title="Stock Tracker", layout="centered")
st.title("Stock Trade Journal")

# Authentication
user = auth_ui()
if not user:
    st.stop()

st.caption("Cloud-synced lot-level trading with RLS security.")

st.markdown(f"**Logged in as:** {user.email}")
if st.button("Log Out"):
    st.session_state.user = None
    st.session_state.access_token = None
    st.rerun()

# Trade Form
with st.form("trade_form", clear_on_submit=True):
    col1, col2, col3 = st.columns(3)
    with col1:
        sym_input = st.text_input("Stock Symbol").upper()
    with col2:
        qty_input = st.number_input("Quantity", min_value=1, step=1)
    with col3:
        price_input = st.number_input("Price per Share", min_value=0.01, step=0.01, format="%.2f")

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        submit_buy = st.form_submit_button("Log Buy", use_container_width=True)
    with btn_col2:
        submit_sell = st.form_submit_button("Log Sell", use_container_width=True)

if (submit_buy or submit_sell) and not sym_input:
    st.warning("Please enter a stock symbol.")
elif submit_buy:
    ok, msg = handle_trade("buy", sym_input, int(qty_input), float(price_input))
    if ok:
        st.success(msg)
    else:
        st.error(msg)
elif submit_sell:
    ok, msg = handle_trade("sell", sym_input, int(qty_input), float(price_input))
    if ok:
        st.success(msg)
    else:
        st.error(msg)

# Holdings Display
st.markdown("### Current Cloud Holdings")
current_portfolio = load_portfolio()
running_pl_by_symbol, running_total_pl = load_running_realized_pl()

st.metric("Running Realized P/L", f"${running_total_pl:,.2f}")

if current_portfolio:
    if st.button("Show Current Holdings", use_container_width=True):
        display_rows = []
        for lot in sorted(current_portfolio, key=lambda x: (x["symbol"], x["last_updated"])):
            display_rows.append(
                {
                    "SYMBOL": lot["symbol"],
                    "QTY": lot["quantity"],
                    "AVG PRICE": round(lot["avg_price"], 2),
                }
            )
        st.dataframe(display_rows, use_container_width=True, hide_index=True)
else:
    st.info("No active lots (Your portfolio is empty).")
