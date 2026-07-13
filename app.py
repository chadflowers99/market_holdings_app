from datetime import datetime
import csv
import json
import io
import logging
import tempfile
import time
import uuid
from pathlib import Path

import streamlit as st

# MUST BE FIRST STREAMLIT COMMAND
st.set_page_config(page_title="Portfolio brand", layout="wide")

from supabase import create_client, Client
from supabase.client import ClientOptions


def _secret_get(key, default=None):
    """Safe st.secrets accessor that tolerates missing cloud secrets config."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


# Load Supabase credentials from secrets.
# Supports both flat keys and optional nested [supabase] block.
supabase_block = _secret_get("supabase", {})
if not isinstance(supabase_block, dict):
    supabase_block = {}

SUPABASE_URL = (
    _secret_get("SUPABASE_URL")
    or supabase_block.get("SUPABASE_URL")
    or supabase_block.get("url")
)
SUPABASE_ANON_KEY = (
    _secret_get("SUPABASE_ANON_KEY")
    or _secret_get("SUPABASE_KEY")
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
TRADE_TRACE_FILE = Path(tempfile.gettempdir()) / "stock_trade_trade_trace.log"

logger = logging.getLogger("holdings_app")


def _trace_marker(trace_id: str, step: str, phase: str, details: str = ""):
    """Persist crash-isolation breadcrumbs to temp file and app logs."""
    timestamp = datetime.now().isoformat(timespec="milliseconds")
    line = f"{timestamp} | trace={trace_id} | {step} | {phase}"
    if details:
        line = f"{line} | {details}"

    try:
        TRADE_TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRADE_TRACE_FILE, "a", encoding="utf-8") as trace_f:
            trace_f.write(line + "\n")
            trace_f.flush()
    except OSError:
        pass

    logger.warning(line)


def _new_trace_id(symbol: str) -> str:
    return f"{symbol[:8]}-{uuid.uuid4().hex[:8]}"


def _read_recent_trace_lines(max_lines: int = 120):
    """Read recent durable trace lines for post-crash diagnostics."""
    try:
        with open(TRADE_TRACE_FILE, "r", encoding="utf-8") as trace_f:
            lines = trace_f.readlines()
        return [line.rstrip("\n") for line in lines[-max_lines:]]
    except FileNotFoundError:
        return []
    except OSError as e:
        return [f"Unable to read trace file: {str(e)}"]


def _clear_trace_file():
    """Reset trace file to isolate the next reproduction run."""
    try:
        TRADE_TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRADE_TRACE_FILE, "w", encoding="utf-8") as trace_f:
            trace_f.write("")
        return True
    except OSError:
        return False


def _resolve_oauth_redirect_url() -> str:
    """Build OAuth callback URL for current host, with secrets override support."""
    configured = (
        _secret_get("OAUTH_REDIRECT_URL")
        or _secret_get("APP_REDIRECT_URL")
        or (supabase_block.get("OAUTH_REDIRECT_URL") if isinstance(supabase_block, dict) else None)
    )
    if isinstance(configured, str) and configured.strip():
        return configured.strip()

    try:
        headers = getattr(st.context, "headers", {})
        host = (headers.get("x-forwarded-host") or headers.get("host") or "").strip()
        proto = (headers.get("x-forwarded-proto") or "https").split(",")[0].strip()
        if host:
            if host.startswith("localhost") or host.startswith("127.0.0.1"):
                proto = "http"
            return f"{proto}://{host}"
    except Exception:
        pass

    return "https://pb-stocktrade.streamlit.app"


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
    """File-backed auth storage with session_state fallback for Streamlit Cloud."""
    def __init__(self, storage_file):
        self.storage_file = storage_file

    def _read(self):
        # Try session_state first (fastest, survives reruns)
        if "_supabase_auth_store" in st.session_state:
            return st.session_state._supabase_auth_store
        # Fall back to file
        if not self.storage_file.exists():
            return {}
        try:
            data = json.loads(self.storage_file.read_text(encoding="utf-8"))
            st.session_state._supabase_auth_store = data
            return data
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data):
        # Always write to session_state (fastest)
        st.session_state._supabase_auth_store = data
        # Also write to file if possible
        try:
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)
            self.storage_file.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass  # File write failed, but session_state has it

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
                # Ensure the session is set on the client for RLS policies
                supabase.auth.set_session(existing.access_token, existing.refresh_token)
                return st.session_state.user
        except Exception:
            pass

    oauth_error = st.query_params.get("error")
    oauth_error_description = st.query_params.get("error_description")
    if oauth_error:
        st.error(
            f"Login attempt failed: {oauth_error}"
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
                # Set the session on the Supabase client so RLS works
                if response.session:
                    supabase.auth.set_session(response.session.access_token, response.session.refresh_token)
                st.session_state.pop("oauth_url", None)
                st.query_params.clear()
                st.rerun()
            else:
                st.error("Login attempt failed: no user returned from Supabase callback.")
                st.session_state.pop("oauth_url", None)
                st.query_params.clear()
        except Exception as e:
            st.error(f"Login attempt failed: {str(e)}")
            st.session_state.pop("oauth_url", None)
            st.query_params.clear()

    st.markdown(
        """
        <div style="text-align: center;">
            <h1>Portfolio brand.</h1>
            <div style="font-size: 18px; margin-bottom: 30px;">
                    MARKET HOLDINGS
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_l, col_m, col_r = st.columns([1, 2, 1])
    with col_m:
        st.markdown("### Authentication")
        auth_tab1, auth_tab2, auth_tab3 = st.tabs(["Login", "Sign Up", "Google"])

        with auth_tab1:
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")

            if st.button("Log In", key="login_button", use_container_width=True):
                try:
                    response = supabase.auth.sign_in_with_password(
                        {
                            "email": email,
                            "password": password,
                        }
                    )
                    st.session_state.user = response.user
                    st.session_state.access_token = response.session.access_token
                    # Set the session on the Supabase client so RLS works
                    supabase.auth.set_session(response.session.access_token, response.session.refresh_token)
                    st.rerun()
                except Exception as e:
                    st.error(f"Login attempt failed: {str(e)}")

        with auth_tab2:
            email = st.text_input("Email", key="signup_email")
            password = st.text_input("Password", type="password", key="signup_password")

            if st.button("Sign Up", key="signup_button", use_container_width=True):
                try:
                    response = supabase.auth.sign_up(
                        {
                            "email": email,
                            "password": password,
                        }
                    )
                    st.session_state.user = response.user
                    if response.session:
                        st.session_state.access_token = response.session.access_token
                        # Set the session on the Supabase client so RLS works
                        supabase.auth.set_session(response.session.access_token, response.session.refresh_token)
                    st.success("Account created! Log in with your credentials.")
                except Exception as e:
                    st.error(f"Sign up failed: {str(e)}")

        with auth_tab3:
            # Skip button rendering during callback (callback handler processes it above)
            if not (st.query_params.get("code") or st.query_params.get("error")):
                st.info("Click the button below to sign in with Google")
                redirect_to = _resolve_oauth_redirect_url()
                
                # Cache the OAuth URL to avoid regenerating the PKCE verifier on each render
                should_refresh_oauth = (
                    "oauth_url" not in st.session_state
                    or st.session_state.get("oauth_redirect_to") != redirect_to
                )
                if should_refresh_oauth:
                    try:
                        response = supabase.auth.sign_in_with_oauth(
                            {
                                "provider": "google",
                                "options": {"redirect_to": redirect_to}
                            }
                        )
                        st.session_state.oauth_url = response.url if (response and hasattr(response, 'url')) else None
                        st.session_state.oauth_redirect_to = redirect_to
                    except Exception as e:
                        st.error(f"Google sign in error: {str(e)}")
                
                oauth_url = st.session_state.get("oauth_url")
                if oauth_url:
                    st.link_button("Sign In with Google", oauth_url, use_container_width=True)
                else:
                    st.error("Could not generate Google sign-in URL")

    return None


def load_portfolio(trace_id: str = "system"):
    """Reads active lots from Supabase portfolio table for current user."""
    if not st.session_state.get("user"):
        return []

    try:
        user_id = st.session_state.user.id
        _trace_marker(trace_id, "portfolio.select", "before", f"user_id={user_id}")
        response = supabase.table("portfolio").select("*").eq("user_id", user_id).gt("quantity", 0).execute()
        count = len(response.data) if response and response.data else 0
        _trace_marker(trace_id, "portfolio.select", "after", f"rows={count}")
        return response.data if response.data else []
    except Exception as e:
        _trace_marker(trace_id, "portfolio.select", "error", str(e))
        st.error(f"Failed to load portfolio: {str(e)}")
        return []


def save_portfolio_row(lot, trace_id: str = "system"):
    """Inserts a new lot or updates existing lot in Supabase portfolio table."""
    if not st.session_state.get("user"):
        return False

    try:
        user_id = st.session_state.user.id
        lot_with_user = {**lot, "user_id": user_id}
        
        # If lot has an id, update it; otherwise insert new
        if "id" in lot_with_user and lot_with_user["id"]:
            # Update existing lot
            _trace_marker(
                trace_id,
                "portfolio.update",
                "before",
                f"id={lot_with_user['id']} symbol={lot_with_user.get('symbol')} qty={lot_with_user.get('quantity')}",
            )
            supabase.table("portfolio").update(lot_with_user).eq("id", lot_with_user["id"]).execute()
            _trace_marker(trace_id, "portfolio.update", "after", f"id={lot_with_user['id']}")
        else:
            # Insert new lot (id will be auto-generated)
            _trace_marker(
                trace_id,
                "portfolio.insert",
                "before",
                f"symbol={lot_with_user.get('symbol')} qty={lot_with_user.get('quantity')}",
            )
            supabase.table("portfolio").insert(lot_with_user).execute()
            _trace_marker(trace_id, "portfolio.insert", "after", f"symbol={lot_with_user.get('symbol')}")
        return True
    except Exception as e:
        _trace_marker(trace_id, "portfolio.write", "error", str(e))
        st.error(f"Failed to save lot: {str(e)}")
        return False


def delete_lot_from_db(symbol, last_updated, trace_id: str = "system"):
    """Sets lot quantity to 0 in Supabase portfolio table."""
    if not st.session_state.get("user"):
        return False

    try:
        user_id = st.session_state.user.id
        _trace_marker(trace_id, "portfolio.close_lot", "before", f"symbol={symbol} last_updated={last_updated}")
        supabase.table("portfolio").update({"quantity": 0}).eq("user_id", user_id).eq("symbol", symbol).eq(
            "last_updated", last_updated
        ).execute()
        _trace_marker(trace_id, "portfolio.close_lot", "after", f"symbol={symbol}")
        return True
    except Exception as e:
        _trace_marker(trace_id, "portfolio.close_lot", "error", str(e))
        st.error(f"Failed to close lot: {str(e)}")
        return False


def log_to_db(symbol, action, qty, price, avg_buy, pl_value, trace_id: str = "system"):
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

        _trace_marker(
            trace_id,
            "ledger.insert",
            "before",
            f"action={action} symbol={symbol} qty={qty} price={price}",
        )
        supabase.table("permanent_ledger").insert(record).execute()
        _trace_marker(trace_id, "ledger.insert", "after", f"action={action} symbol={symbol}")
        return True
    except Exception as e:
        _trace_marker(trace_id, "ledger.insert", "error", str(e))
        st.error(f"Failed to log trade: {str(e)}")
        return False


def handle_trade(action, symbol, quantity, price):
    """Processes BUY or SELL trades with lowest-buy-first FIFO matching."""
    symbol = symbol.strip().upper()
    trace_id = _new_trace_id(symbol)
    now = datetime.now().isoformat()
    _trace_marker(trace_id, "trade.handle", "start", f"action={action} symbol={symbol} qty={quantity} price={price}")
    portfolio = load_portfolio(trace_id=trace_id)

    if action == "buy":
        new_lot = {
            "symbol": symbol,
            "quantity": quantity,
            "avg_price": price,
            "last_updated": now,
        }
        save_portfolio_row(new_lot, trace_id=trace_id)
        log_to_db(symbol, "BUY", quantity, price, price, 0.0, trace_id=trace_id)
        _trace_marker(trace_id, "trade.handle", "done", "buy_complete")
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

        log_to_db(symbol, "SELL", consumed, price, lot["avg_price"], lot_realized_pl, trace_id=trace_id)

        # Save original timestamp before updating
        original_last_updated = lot["last_updated"]
        lot["quantity"] -= consumed
        lot["last_updated"] = now

        if lot["quantity"] <= 0:
            delete_lot_from_db(symbol, original_last_updated, trace_id=trace_id)
        else:
            save_portfolio_row(lot, trace_id=trace_id)

        remaining_to_sell -= consumed

    pl_status = "Profit" if total_realized_pl >= 0 else "Loss"
    _trace_marker(trace_id, "trade.handle", "done", f"sell_complete realized={total_realized_pl:.2f}")
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
        return f"{trade_date} | {action} | {symbol} | {quantity} | ${price:.2f}"
    return f"{action} | {symbol} | {quantity} | ${price:.2f}"


def _parse_positive_int(value, field_name):
    try:
        parsed = int(str(value).strip())
        if parsed < 1:
            raise ValueError
        return parsed, None
    except Exception:
        return None, f"{field_name} must be a whole number greater than 0."


def _parse_positive_float(value, field_name):
    try:
        parsed = float(str(value).strip())
        if parsed < 0.01:
            raise ValueError
        return parsed, None
    except Exception:
        return None, f"{field_name} must be a number greater than 0.00."


# Main App
st.markdown(
    """
    <style>
    input[type=number]::-webkit-outer-spin-button,
    input[type=number]::-webkit-inner-spin-button {
        -webkit-appearance: none;
        margin: 0;
    }
    input[type=number] {
        -moz-appearance: textfield;
        appearance: textfield;
    }
    div[data-baseweb="input"] [data-testid="stNumberInputStepUp"],
    div[data-baseweb="input"] [data-testid="stNumberInputStepDown"],
    div[data-baseweb="input"] button[aria-label="Increment value"],
    div[data-baseweb="input"] button[aria-label="Decrement value"],
    div[data-baseweb="input"] button[title="Increment"],
    div[data-baseweb="input"] button[title="Decrement"] {
        display: none !important;
        visibility: hidden !important;
        width: 0 !important;
        min-width: 0 !important;
        padding: 0 !important;
        margin: 0 !important;
        border: 0 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Authentication
user = auth_ui()
if not user:
    st.stop()

user_id = user.id

# Add logout button in sidebar
with st.sidebar:
    if st.button("Logout"):
        supabase.auth.sign_out()
        st.session_state.user = None
        st.session_state.access_token = None
        st.session_state.pop("oauth_url", None)
        st.query_params.clear()
        st.rerun()
    st.caption(f"Logged in as: {user.email}")

st.markdown(
    """
    <div>
        <h1 style='margin: 0; line-height: 1.05;'>Portfolio brand.</h1>
        <div style='font-size: 0.72rem;'>
            MARKET HOLDINGS
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.expander("Crash Debug Trace", expanded=False):
    st.caption("Shows durable before/after markers for Supabase calls in the trade path.")
    st.caption(f"Trace file: {TRADE_TRACE_FILE}")

    trace_lines = _read_recent_trace_lines(max_lines=200)
    if trace_lines:
        trace_text = "\n".join(trace_lines)
        st.code(trace_text, language="text")
    else:
        trace_text = ""
        st.caption("No trace markers found yet.")

    debug_col1, debug_col2 = st.columns(2)
    with debug_col1:
        if st.button("Refresh Trace", key="refresh_trace_button", use_container_width=True):
            st.rerun()
    with debug_col2:
        if st.button("Clear Trace", key="clear_trace_button", use_container_width=True):
            if _clear_trace_file():
                st.success("Trace file cleared.")
                st.rerun()
            else:
                st.error("Unable to clear trace file.")

    st.download_button(
        "Download Trace Log",
        data=trace_text,
        file_name="stock_trade_trade_trace.log",
        mime="text/plain",
        use_container_width=True,
        disabled=not bool(trace_text),
    )

# Trade Form
with st.form("trade_form", clear_on_submit=True):
    col1, col2, col3 = st.columns(3)
    with col1:
        sym_input = st.text_input("Stock Symbol").upper()
    with col2:
        qty_input = st.text_input("Quantity")
    with col3:
        price_input = st.text_input("Price per Share")

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        submit_buy = st.form_submit_button("Log Buy", use_container_width=True)
    with btn_col2:
        submit_sell = st.form_submit_button("Log Sell", use_container_width=True)

if submit_buy or submit_sell:
    if not sym_input:
        st.warning("Please enter a stock symbol.")
    else:
        parsed_qty, qty_error = _parse_positive_int(qty_input, "Quantity")
        parsed_price, price_error = _parse_positive_float(price_input, "Price per Share")
        if qty_error:
            st.error(qty_error)
        elif price_error:
            st.error(price_error)
        elif submit_buy:
            ok, msg = handle_trade("buy", sym_input, parsed_qty, parsed_price)
            if ok:
                st.success(msg)
            else:
                st.error(msg)
        elif submit_sell:
            ok, msg = handle_trade("sell", sym_input, parsed_qty, parsed_price)
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
                        edit_qty = st.text_input("Quantity", value=str(int(selected_trade.get("quantity", 1) or 1)))
                        edit_price = st.text_input("Price", value=f"{float(selected_trade.get('price', 0.0) or 0.0):.2f}")
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
                            parsed_edit_qty, edit_qty_error = _parse_positive_int(edit_qty, "Quantity")
                            parsed_edit_price, edit_price_error = _parse_positive_float(edit_price, "Price")
                            if edit_qty_error:
                                raise ValueError(edit_qty_error)
                            if edit_price_error:
                                raise ValueError(edit_price_error)
                            updated_record = {
                                "action": edit_action,
                                "symbol": edit_symbol,
                                "quantity": parsed_edit_qty,
                                "price": parsed_edit_price,
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
