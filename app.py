from datetime import datetime
import csv
import json
import io
import tempfile
from pathlib import Path

import streamlit as st
from supabase import create_client, Client
from supabase.client import ClientOptions

# Load Supabase credentials from secrets.
# Supports both flat keys and optional nested [supabase] block.
supabase_block = st.secrets.get("supabase", {})

SUPABASE_URL = (
    st.secrets.get("SUPABASE_URL")
    or supabase_block.get("SUPABASE_URL")
    or supabase_block.get("url")
)
SUPABASE_ANON_KEY = (
    st.secrets.get("SUPABASE_ANON_KEY")
    or st.secrets.get("SUPABASE_KEY")
    or supabase_block.get("SUPABASE_ANON_KEY")
    or supabase_block.get("SUPABASE_KEY")
    or supabase_block.get("anon_key")
)

if isinstance(SUPABASE_URL, str):
    SUPABASE_URL = SUPABASE_URL.strip()
if isinstance(SUPABASE_ANON_KEY, str):
    SUPABASE_ANON_KEY = SUPABASE_ANON_KEY.strip()

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.error(
        "Missing SUPABASE_URL and/or Supabase key in secrets. "
        "Expected SUPABASE_ANON_KEY (preferred) or SUPABASE_KEY (legacy)."
    )
    st.stop()

if "sb_secret_" in SUPABASE_ANON_KEY.lower() or "service_role" in SUPABASE_ANON_KEY.lower():
    st.error(
        "SUPABASE_ANON_KEY appears to be a service-role/secret key. "
        "Use the anon/publishable key from Supabase Settings > API."
    )
    st.stop()

if "your-project" in SUPABASE_URL or "your-project-ref" in SUPABASE_URL:
    st.error(
        "SUPABASE_URL in .streamlit/secrets.toml is still a placeholder. "
        "Use your real project URL from Supabase Settings > API."
    )
    st.stop()

if "your_anon_key_here" in SUPABASE_ANON_KEY.lower():
    st.error(
        "SUPABASE_ANON_KEY in .streamlit/secrets.toml is still a placeholder. "
        "Use your real anon/publishable key from Supabase Settings > API."
    )
    st.stop()


BASE_DIR = Path(__file__).resolve().parent
AUTH_STORAGE_FILE = BASE_DIR / ".streamlit" / "supabase_auth_storage.json"
AUTH_STORAGE_FILE_FALLBACK = Path(tempfile.gettempdir()) / "stock_trade_supabase_auth_storage.json"


class MemoryAuthStorage:
    """In-memory auth storage fallback when filesystem is not writable."""
    def __init__(self):
        self._store = {}

    def get_item(self, key):
        return self._store.get(key)

    def set_item(self, key, value):
        self._store[key] = value

    def remove_item(self, key):
        self._store.pop(key, None)


class FileAuthStorage:
    """File-backed auth storage — required for PKCE verifier across redirects."""
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


def _is_writable(path: Path) -> bool:
    """Checks whether a path is writable by creating/removing a tiny probe file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _build_auth_storage():
    """Use file-backed storage when possible to preserve PKCE verifier on callback."""
    if _is_writable(AUTH_STORAGE_FILE):
        return FileAuthStorage(AUTH_STORAGE_FILE)
    if _is_writable(AUTH_STORAGE_FILE_FALLBACK):
        return FileAuthStorage(AUTH_STORAGE_FILE_FALLBACK)
    return MemoryAuthStorage()


# Initialize Supabase client with appropriate storage backend.
@st.cache_resource
def get_supabase_client() -> Client:
    storage = _build_auth_storage()
    return create_client(
        SUPABASE_URL,
        SUPABASE_ANON_KEY,
        options=ClientOptions(
            flow_type="pkce",
            storage=storage,
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
        st.query_params.clear()

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
                st.query_params.clear()
        except Exception as e:
            st.error(f"GitHub login callback failed: {str(e)}")
            st.query_params.clear()

    st.markdown("### Authentication")
    auth_tab1, auth_tab2, auth_tab3 = st.tabs(["Login", "Sign Up", "Google"])

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
        # Skip if in callback flow to prevent regenerating verifier
        if not st.query_params.get("code") and not st.query_params.get("error"):
            # Cache OAuth URL in session_state to avoid regenerating verifier on every rerun
            if "google_oauth_url" not in st.session_state:
                current_url = getattr(st.context, "url", None)
                oauth_payload = {"provider": "google"}
                redirect_to = None
                if current_url:
                    redirect_to = current_url.split("?", 1)[0].rstrip("/") + "/"
                    oauth_payload["options"] = {"redirect_to": redirect_to}

                oauth_response = supabase.auth.sign_in_with_oauth(oauth_payload)
                st.session_state.google_oauth_url = oauth_response.url
                st.session_state.google_redirect_to = redirect_to
            else:
                redirect_to = st.session_state.get("google_redirect_to")

            authorize_url = st.session_state.google_oauth_url
            st.write("Use your Google account to sign in.")
            if redirect_to:
                st.caption(f"Google will redirect back to: {redirect_to}")
            else:
                st.caption("Google will redirect using Supabase Auth Site/Redirect URL settings.")
            st.link_button("Continue with Google", authorize_url, use_container_width=True)
            st.caption(
                "If this does not work, enable Google in Supabase Auth Providers and add your app URL"
                " to Supabase Redirect URLs."
            )
        else:
            st.info("Processing Google login callback...")

    return None


def load_portfolio():
    """Reads active lots from Supabase portfolio table for current user."""
    if not st.session_state.get("user"):
        return []

    try:
        user_id = st.session_state.user.id
        response = supabase.table("portfolio").select("*").eq("user_id", user_id).gt("quantity", 0).execute()
        return response.data if response.data else []
    except Exception as e:
        st.error(f"Failed to load portfolio: {str(e)}")
        return []


def save_portfolio_row(lot):
    """Inserts a new lot or updates existing lot in Supabase portfolio table."""
    if not st.session_state.get("user"):
        return False

    try:
        user_id = st.session_state.user.id
        lot_with_user = {**lot, "user_id": user_id}
        
        # If lot has an id, update it; otherwise insert new
        if "id" in lot_with_user and lot_with_user["id"]:
            # Update existing lot
            supabase.table("portfolio").update(lot_with_user).eq("id", lot_with_user["id"]).execute()
        else:
            # Insert new lot (id will be auto-generated)
            supabase.table("portfolio").insert(lot_with_user).execute()
        return True
    except Exception as e:
        st.error(f"Failed to save lot: {str(e)}")
        return False


def delete_lot_from_db(symbol, last_updated):
    """Sets lot quantity to 0 in Supabase portfolio table."""
    if not st.session_state.get("user"):
        return False

    try:
        user_id = st.session_state.user.id
        supabase.table("portfolio").update({"quantity": 0}).eq("user_id", user_id).eq("symbol", symbol).eq(
            "last_updated", last_updated
        ).execute()
        return True
    except Exception as e:
        st.error(f"Failed to close lot: {str(e)}")
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

        # Save original timestamp before updating
        original_last_updated = lot["last_updated"]
        lot["quantity"] -= consumed
        lot["last_updated"] = now

        if lot["quantity"] <= 0:
            delete_lot_from_db(symbol, original_last_updated)
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


def _ledger_sort_key(record):
    """Sort key that keeps ledger replay in timestamp order with stable ties."""
    return (str(record.get("timestamp", "")), str(record.get("id", "")))


def _update_ledger_record(record, updates):
    """Update one ledger row using id when available, otherwise a strict row match."""
    query = supabase.table("permanent_ledger").update(updates)
    record_id = record.get("id")
    if record_id is not None:
        query = query.eq("id", record_id)
    else:
        query = query.eq("user_id", st.session_state.user.id)
        for field in ("timestamp", "action", "symbol", "quantity", "price"):
            query = query.eq(field, record.get(field))
    query.execute()


def _delete_ledger_record(record):
    """Delete one ledger row using id when available, otherwise a strict row match."""
    query = supabase.table("permanent_ledger").delete()
    record_id = record.get("id")
    if record_id is not None:
        query = query.eq("id", record_id)
    else:
        query = query.eq("user_id", st.session_state.user.id)
        for field in ("timestamp", "action", "symbol", "quantity", "price"):
            query = query.eq(field, record.get(field))
    query.execute()


def rebuild_portfolio_from_ledger(user_id):
    """Replays the entire ledger to rebuild current holdings and realized P/L consistency."""
    ledger_response = supabase.table("permanent_ledger").select("*").eq("user_id", user_id).execute()
    ledger_rows = list(ledger_response.data or [])
    ledger_rows.sort(key=_ledger_sort_key)

    open_lots = []

    for record in ledger_rows:
        action = str(record.get("action", "")).upper()
        symbol = str(record.get("symbol", "")).strip().upper()
        quantity = int(record.get("quantity", 0) or 0)
        price = float(record.get("price", 0) or 0)
        timestamp_value = record.get("timestamp", "")

        if not symbol or quantity <= 0:
            continue

        if action == "BUY":
            open_lots.append(
                {
                    "symbol": symbol,
                    "quantity": quantity,
                    "avg_price": price,
                    "last_updated": timestamp_value,
                }
            )
            continue

        if action != "SELL":
            continue

        remaining = quantity
        consumed_total = 0
        realized_total = 0.0
        cost_basis_total = 0.0

        for lot in sorted(open_lots, key=lambda x: (x["avg_price"], x["last_updated"])):
            if remaining <= 0:
                break

            consumed = min(lot["quantity"], remaining)
            realized_total += (price - lot["avg_price"]) * consumed
            cost_basis_total += lot["avg_price"] * consumed
            lot["quantity"] -= consumed
            remaining -= consumed
            consumed_total += consumed

        open_lots = [lot for lot in open_lots if lot["quantity"] > 0]

        avg_buy_price = cost_basis_total / consumed_total if consumed_total else price
        _update_ledger_record(
            record,
            {
                "avg_buy_price": avg_buy_price,
                "realized_pl": realized_total,
            },
        )

    supabase.table("portfolio").delete().eq("user_id", user_id).execute()
    for lot in open_lots:
        save_portfolio_row(lot)


def _ledger_display_label(record):
    """Human-friendly label for selecting a trade row to edit."""
    action = str(record.get("action", "")).upper()
    symbol = str(record.get("symbol", ""))
    quantity = int(record.get("quantity", 0) or 0)
    price = float(record.get("price", 0) or 0)
    trade_date = str(record.get("timestamp", ""))[:10]
    if trade_date:
        return f"{action} {symbol} {quantity} @ ${price:.2f} | {trade_date}"
    return f"{action} {symbol} {quantity} @ ${price:.2f}"


# Main App
st.set_page_config(page_title="portfolio brand", layout="centered")
st.markdown(
    """
    <div style='padding: 0.25rem 0 0.55rem 0; border-bottom: 2px solid rgba(46, 204, 113, 0.25);'>
        <h1 style='margin: 0; line-height: 1.05; letter-spacing: 0.015em; font-weight: 400;'><span style='color: #2ecc71; font-family: "Courier New", monospace; font-size: 2rem; font-weight: 800;'>Portfolio</span><span style='color: #666666; font-size: 0.72rem; letter-spacing: 0.14em; text-transform: uppercase; margin-left: 0.35em;'>brand</span></h1>
        <div style='font-size: 0.72rem; letter-spacing: 0.14em; text-transform: uppercase; color: #666666;'>
            OPEN LOT HOLDINGS
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Authentication
user = auth_ui()
if not user:
    st.stop()

st.caption("Cloud-synced lot-level trading with RLS security.")

st.markdown(f"**Logged in as:** {user.email}")
if st.button("Log Out"):
    try:
        supabase.auth.sign_out()
    except Exception as e:
        st.warning(f"Sign out warning: {str(e)}")
    st.session_state.user = None
    st.session_state.access_token = None
    st.query_params.clear()
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
st.markdown("### Current Holdings")
current_portfolio = load_portfolio()
running_pl_by_symbol, running_total_pl = load_running_realized_pl()

st.metric("Running Realized P/L", f"${running_total_pl:,.2f}")

if current_portfolio:
    display_rows = []
    for lot in sorted(current_portfolio, key=lambda x: (x["symbol"], x["last_updated"])):
        display_rows.append(
            {
                "SYMBOL": lot["symbol"],
                "QTY": lot["quantity"],
                "PRICE": round(lot["avg_price"], 2),
            }
        )
    st.dataframe(display_rows, use_container_width=True, hide_index=True)
else:
    st.info("No active lots (Your portfolio is empty).")

st.markdown("### Trade History")
with st.expander("Trade History", expanded=False):
    try:
        user_id = st.session_state.user.id
        ledger_response = supabase.table("permanent_ledger").select("*").eq("user_id", user_id).order("timestamp", desc=True).execute()
        if ledger_response.data:
            history_rows = []
            available_dates = []
            for record in ledger_response.data:
                timestamp_value = record.get("timestamp", "")
                parsed_date = None
                timestamp_text = str(timestamp_value)
                # Use the literal YYYY-MM-DD prefix from the stored timestamp for stable filtering.
                if len(timestamp_text) >= 10:
                    try:
                        parsed_date = datetime.strptime(timestamp_text[:10], "%Y-%m-%d").date()
                    except ValueError:
                        parsed_date = None
                if parsed_date:
                    available_dates.append(parsed_date)

                row = {
                    "_record": dict(record),
                    "TRADE DATE": parsed_date.isoformat() if parsed_date else "",
                    "ACTION": record.get("action", "").upper(),
                    "SYMBOL": record.get("symbol", ""),
                    "QTY": record.get("quantity", 0),
                    "PRICE": round(float(record.get("price", 0)), 2),
                    "_parsed_date": parsed_date,
                }
                history_rows.append(row)

            filter_col1, filter_col2, filter_col3 = st.columns(3)
            with filter_col1:
                action_filter = st.selectbox("Action", ["All", "BUY", "SELL"], key="trade_history_action_filter")
            with filter_col2:
                symbol_filter = st.text_input("Symbol", key="trade_history_symbol_filter").strip().upper()
            with filter_col3:
                date_filter_mode = st.selectbox(
                    "Date filter",
                    ["All dates", "Date range"],
                    key="trade_history_date_filter_mode",
                )
                start_date_filter = None
                end_date_filter = None

                if date_filter_mode == "Date range":
                    if available_dates:
                        min_date = min(available_dates)
                        max_date = max(available_dates)
                        date_col1, date_col2 = st.columns(2)
                        with date_col1:
                            start_date_filter = st.date_input(
                                "From date",
                                value=min_date,
                                min_value=min_date,
                                max_value=max_date,
                                key="trade_history_start_date_filter_v3",
                            )
                        with date_col2:
                            end_date_filter = st.date_input(
                                "End date",
                                value=max_date,
                                min_value=min_date,
                                max_value=max_date,
                                key="trade_history_end_date_filter_v3",
                            )
                        if start_date_filter and end_date_filter and start_date_filter > end_date_filter:
                            start_date_filter, end_date_filter = end_date_filter, start_date_filter
                    else:
                        st.caption("No valid trade dates found for range filtering.")

            filtered_rows = []
            filtered_records = []
            for row in history_rows:
                if action_filter != "All" and row["ACTION"] != action_filter:
                    continue
                if symbol_filter and row["SYMBOL"].upper() != symbol_filter:
                    continue
                if (start_date_filter or end_date_filter) and row["_parsed_date"] is None:
                    continue
                if start_date_filter and row["_parsed_date"] and row["_parsed_date"] < start_date_filter:
                    continue
                if end_date_filter and row["_parsed_date"] and row["_parsed_date"] > end_date_filter:
                    continue
                filtered_records.append(row)
                filtered_rows.append({
                    "TRADE DATE": row["TRADE DATE"],
                    "ACTION": row["ACTION"],
                    "SYMBOL": row["SYMBOL"],
                    "QTY": row["QTY"],
                    "PRICE": row["PRICE"],
                })

            if filtered_rows:
                st.caption(f"Showing {len(filtered_rows)} of {len(history_rows)} trades")
                st.dataframe(filtered_rows, use_container_width=True, hide_index=True)
                csv_buffer = io.StringIO()
                csv_writer = csv.DictWriter(csv_buffer, fieldnames=["TRADE DATE", "ACTION", "SYMBOL", "QTY", "PRICE"])
                csv_writer.writeheader()
                csv_writer.writerows(filtered_rows)
                st.download_button(
                    "Download CSV",
                    data=csv_buffer.getvalue(),
                    file_name="trade_history.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

                with st.expander("Edit Trade", expanded=False):
                    trade_choice_map = {}
                    trade_choices = []
                    for idx, item in enumerate(filtered_records):
                        record = item["_record"]
                        record_id = record.get("id")
                        if record_id:
                            choice_key = f"id:{record_id}"
                        else:
                            choice_key = (
                                f"fallback:{idx}:{record.get('timestamp', '')}:{record.get('action', '')}:"
                                f"{record.get('symbol', '')}:{record.get('quantity', '')}:{record.get('price', '')}"
                            )
                        trade_choices.append(choice_key)
                        trade_choice_map[choice_key] = record

                    selected_trade_key = st.selectbox(
                        "Choose trade",
                        trade_choices,
                        format_func=lambda choice: _ledger_display_label(trade_choice_map[choice]),
                        key="trade_history_edit_choice",
                    )
                    selected_trade = trade_choice_map[selected_trade_key]

                    with st.form("edit_trade_form"):
                        current_action = str(selected_trade.get("action", "")).upper()
                        action_index = 0 if current_action == "BUY" else 1
                        edit_action = st.selectbox("Action", ["BUY", "SELL"], index=action_index)
                        edit_symbol = st.text_input("Symbol", value=str(selected_trade.get("symbol", ""))).strip().upper()
                        edit_qty = st.number_input(
                            "Quantity",
                            min_value=1,
                            step=1,
                            value=int(selected_trade.get("quantity", 1) or 1),
                        )
                        edit_price = st.number_input(
                            "Price",
                            min_value=0.01,
                            step=0.01,
                            format="%.2f",
                            value=float(selected_trade.get("price", 0.0) or 0.0),
                        )
                        confirm_delete_all = st.checkbox(
                            "Confirm delete all trades",
                            value=False,
                            help="This permanently removes your entire trade history and resets holdings.",
                        )
                        save_trade_col, delete_trade_col, delete_all_col = st.columns(3)
                        with save_trade_col:
                            save_trade_edit = st.form_submit_button("Save Changes", use_container_width=True)
                        with delete_trade_col:
                            delete_trade_edit = st.form_submit_button("Delete Trade", use_container_width=True)
                        with delete_all_col:
                            delete_all_trades = st.form_submit_button("Delete All", use_container_width=True)

                    if save_trade_edit:
                        try:
                            updated_record = {
                                "action": edit_action,
                                "symbol": edit_symbol,
                                "quantity": int(edit_qty),
                                "price": float(edit_price),
                            }
                            _update_ledger_record(selected_trade, updated_record)
                            rebuild_portfolio_from_ledger(user_id)
                            st.success("Trade updated and portfolio recalculated.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to update trade: {str(e)}")
                    elif delete_trade_edit:
                        try:
                            _delete_ledger_record(selected_trade)
                            rebuild_portfolio_from_ledger(user_id)
                            st.success("Trade deleted and portfolio recalculated.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to delete trade: {str(e)}")
                    elif delete_all_trades:
                        if not confirm_delete_all:
                            st.warning("Enable 'Confirm delete all trades' before deleting everything.")
                        else:
                            try:
                                supabase.table("permanent_ledger").delete().eq("user_id", user_id).execute()
                                rebuild_portfolio_from_ledger(user_id)
                                st.success("All trades deleted and portfolio reset.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed to delete all trades: {str(e)}")
            else:
                st.caption("No trade history matches the current filters.")
        else:
            st.caption("No trade history yet.")
    except Exception as e:
        st.error(f"Failed to load trade history: {str(e)}")
