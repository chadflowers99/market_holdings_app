# -*- coding: utf-8 -*-
"""
Created on Wed Jan 14 13:20:58 2026

@author: busin
"""

# stock_trader_gui.py

from datetime import datetime
from pathlib import Path
import sqlite3
import tkinter as tk
from tkinter import messagebox

# --- Define local directories, CSV, and DB paths ---
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PORTFOLIO_CSV = OUTPUT_DIR / "portfolio.csv"
DB_FILE = OUTPUT_DIR / "trades_archive.db"


def init_storage():
    """Ensures output folder, lot CSV, and database exist."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Initialize CSV
    if not PORTFOLIO_CSV.exists():
        with open(PORTFOLIO_CSV, "w", encoding="utf-8") as f:
            f.write("Symbol,Quantity,Buy_Price,Buy_Timestamp,Last_Updated\n")
            
    # Initialize SQLite DB
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS permanent_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            action TEXT,
            quantity INTEGER,
            price REAL,
            avg_buy_price REAL,
            realized_pl REAL
        )
    """)
    conn.commit()
    conn.close()


def rebuild_active_lots_from_db():
    """Reconstructs active lots from permanent_ledger using lowest-buy-first matching."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        SELECT id, timestamp, symbol, action, quantity, price
        FROM permanent_ledger
        ORDER BY id ASC
        """
    )
    rows = c.fetchall()
    conn.close()

    lots = []
    for row_id, ts, symbol, action, qty, price in rows:
        action_upper = (action or "").upper()

        if action_upper == "BUY":
            lots.append(
                {
                    "symbol": symbol,
                    "quantity": int(qty),
                    "buy_price": float(price),
                    "buy_timestamp": ts,
                    "last_updated": ts,
                }
            )
            continue

        if action_upper == "SELL":
            remaining = int(qty)
            for lot in sorted(
                [l for l in lots if l["symbol"] == symbol and l["quantity"] > 0],
                key=lambda x: (x["buy_price"], x["buy_timestamp"]),
            ):
                if remaining <= 0:
                    break
                consumed = min(lot["quantity"], remaining)
                lot["quantity"] -= consumed
                lot["last_updated"] = ts
                remaining -= consumed

    return [lot for lot in lots if lot["quantity"] > 0]


def load_portfolio():
    """Reads current active lot rows from CSV into a list of lots."""
    lots = []
    if not PORTFOLIO_CSV.exists():
        return lots

    with open(PORTFOLIO_CSV, "r", encoding="utf-8") as f:
        lines = f.readlines()
        if len(lines) <= 1:
            return lots

        header = lines[0].strip()

        # Backward-compatible read for old aggregated format.
        if header == "Symbol,Quantity,Avg_Price,Last_Updated":
            rebuilt_lots = rebuild_active_lots_from_db()
            if rebuilt_lots:
                save_portfolio(rebuilt_lots)
                return rebuilt_lots

            # Fallback only if DB has no history to rebuild from.
            for line in lines[1:]:
                line = line.strip()
                if not line:
                    continue
                symbol, qty, avg_price, last_updated = line.split(",")
                lots.append(
                    {
                        "symbol": symbol,
                        "quantity": int(qty),
                        "buy_price": float(avg_price),
                        "buy_timestamp": last_updated,
                        "last_updated": last_updated,
                    }
                )
            return lots

        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) == 6:
                _, symbol, qty, buy_price, buy_timestamp, last_updated = parts
            elif len(parts) == 5:
                symbol, qty, buy_price, buy_timestamp, last_updated = parts
            else:
                continue

            lots.append(
                {
                    "symbol": symbol,
                    "quantity": int(qty),
                    "buy_price": float(buy_price),
                    "buy_timestamp": buy_timestamp,
                    "last_updated": last_updated,
                }
            )
    return lots


def save_portfolio(portfolio):
    """Writes active lot rows back to CSV (one row per buy lot)."""
    with open(PORTFOLIO_CSV, "w", encoding="utf-8") as f:
        f.write("Symbol,Quantity,Buy_Price,Buy_Timestamp,Last_Updated\n")
        for lot in sorted(portfolio, key=lambda x: (x["symbol"], x["buy_timestamp"])):
            f.write(
                f"{lot['symbol']},{lot['quantity']},{lot['buy_price']:.2f},{lot['buy_timestamp']},{lot['last_updated']}\n"
            )


def log_to_db(symbol, action, qty, price, avg_buy, pl_value):
    """Saves a permanent record of the trade and P/L into the SQLite database."""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Defensive guard: ensure archive table exists even if DB was recreated mid-session.
    c.execute("""
        CREATE TABLE IF NOT EXISTS permanent_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            action TEXT,
            quantity INTEGER,
            price REAL,
            avg_buy_price REAL,
            realized_pl REAL
        )
    """)
    c.execute("""
        INSERT INTO permanent_ledger (timestamp, symbol, action, quantity, price, avg_buy_price, realized_pl)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (now, symbol, action, qty, price, avg_buy, pl_value))
    conn.commit()
    conn.close()


def log_trade(action):
    symbol = symbol_entry.get().strip().upper()
    qty_str = quantity_entry.get().strip()
    price_str = price_entry.get().strip()

    if not symbol or not qty_str or not price_str:
        messagebox.showwarning("Missing Fields", "Please fill in all fields.")
        return

    try:
        quantity = int(qty_str.replace(",", "").strip())
        price = float(price_str.replace("$", "").replace(",", "").strip())
    except ValueError:
        messagebox.showerror(
            "Input Error", f"Invalid input -> Qty: '{qty_str}', Price: '{price_str}'"
        )
        return

    now = datetime.now().isoformat()
    portfolio = load_portfolio()

    if action == "buy":
        portfolio.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "buy_price": price,
                "buy_timestamp": now,
                "last_updated": now,
            }
        )

        # 1. Update Active CSV
        save_portfolio(portfolio)
        # 2. Archive to permanent Database (P/L is 0 for buys)
        log_to_db(symbol, "BUY", quantity, price, price, 0.0)
        
        messagebox.showinfo(
            "Trade Logged", f"Bought {quantity} shares of {symbol} @ ${price:.2f}\nSaved to active CSV & permanent DB archive."
        )

    elif action == "sell":
        symbol_lots = [lot for lot in portfolio if lot["symbol"] == symbol and lot["quantity"] > 0]
        total_qty = sum(lot["quantity"] for lot in symbol_lots)

        if total_qty <= 0:
            messagebox.showwarning("Sell Error", f"No holdings found for {symbol}")
            return

        if quantity > total_qty:
            messagebox.showerror(
                "Sell Error",
                f"Cannot sell {quantity} shares. You only own {total_qty} shares of {symbol}.",
            )
            return

        remaining_to_sell = quantity
        total_realized_pl = 0.0

        # Lowest-buy-first depletion (tie-break: earliest timestamp).
        for lot in sorted(symbol_lots, key=lambda x: (x["buy_price"], x["buy_timestamp"])):
            if remaining_to_sell <= 0:
                break

            consumed = min(lot["quantity"], remaining_to_sell)
            lot_realized_pl = (price - lot["buy_price"]) * consumed
            total_realized_pl += lot_realized_pl

            log_to_db(symbol, "SELL", consumed, price, lot["buy_price"], lot_realized_pl)

            lot["quantity"] -= consumed
            lot["last_updated"] = now
            remaining_to_sell -= consumed

        # Keep only active lots with non-zero quantity.
        portfolio = [lot for lot in portfolio if lot["quantity"] > 0]

        pl_status = "Profit" if total_realized_pl >= 0 else "Loss"
        pl_msg = f"Realized {pl_status}: ${abs(total_realized_pl):.2f}\n(Recorded permanently in DB)"

        remaining_symbol_qty = sum(
            lot["quantity"] for lot in portfolio if lot["symbol"] == symbol
        )

        if remaining_symbol_qty <= 0:
            messagebox.showinfo(
                "Trade Logged & Removed",
                f"Sold remaining {quantity} shares of {symbol}.\n\n{pl_msg}\n\nAll active lots removed from CSV.",
            )
        else:
            messagebox.showinfo(
                "Trade Logged", 
                f"Sold {quantity} shares of {symbol} @ ${price:.2f}.\n\n{pl_msg}"
            )

        # 2. Update Active CSV
        save_portfolio(portfolio)

    # Clear UI elements
    symbol_entry.delete(0, tk.END)
    quantity_entry.delete(0, tk.END)
    price_entry.delete(0, tk.END)


def view_portfolio_csv():
    portfolio = load_portfolio()
    summary_window = tk.Toplevel(root)
    summary_window.title("Current CSV Active Lots")

    text = tk.Text(summary_window, width=96, height=14)
    text.pack(padx=10, pady=10)

    if not portfolio:
        text.insert(tk.END, "CSV file is empty (No active lots).")
        return

    text.insert(
        tk.END,
        f"{'SYMBOL':<8} | {'QTY':<6} | {'BUY PRICE':<10} | {'BUY TIMESTAMP':<19} | {'LAST UPDATED':<19}\n",
    )
    text.insert(tk.END, "-" * 78 + "\n")

    for lot in sorted(portfolio, key=lambda x: (x["symbol"], x["buy_timestamp"])):
        line = (
            f"{lot['symbol']:<8} | {lot['quantity']:<6} | "
            f"${lot['buy_price']:<9.2f} | {lot['buy_timestamp'][:19]:<19} | {lot['last_updated'][:19]}\n"
        )
        text.insert(tk.END, line)


root = None
symbol_entry = None
quantity_entry = None
price_entry = None


def build_gui():
    global root, symbol_entry, quantity_entry, price_entry

    root = tk.Tk()
    root.title("Stock Tracker (CSV + DB Archive)")
    root.geometry("500x220")

    tk.Label(root, text="Stock Symbol").grid(row=0, column=0, padx=10, pady=5, sticky="e")
    symbol_entry = tk.Entry(root, width=25)
    symbol_entry.grid(row=0, column=1, padx=10, pady=5)

    tk.Label(root, text="Quantity").grid(row=1, column=0, padx=10, pady=5, sticky="e")
    quantity_entry = tk.Entry(root, width=25)
    quantity_entry.grid(row=1, column=1, padx=10, pady=5)

    tk.Label(root, text="Price per Share").grid(row=2, column=0, padx=10, pady=5, sticky="e")
    price_entry = tk.Entry(root, width=25)
    price_entry.grid(row=2, column=1, padx=10, pady=5)

    button_frame = tk.Frame(root)
    button_frame.grid(row=3, column=0, columnspan=2, pady=10)
    tk.Button(button_frame, text="Buy", width=10, command=lambda: log_trade("buy")).grid(row=0, column=0, padx=10)
    tk.Button(button_frame, text="Sell", width=10, command=lambda: log_trade("sell")).grid(row=0, column=1, padx=10)

    tk.Button(root, text="View CSV Active Holdings", command=view_portfolio_csv).grid(row=4, column=0, columnspan=2, pady=5)


if __name__ == "__main__":
    init_storage()
    build_gui()
    root.mainloop()