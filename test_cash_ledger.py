import ast
from copy import deepcopy
from datetime import date
from ipaddress import ip_address
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from postgrest.exceptions import APIError
from streamlit.testing.v1 import AppTest
import streamlit as st


APP_FILE = Path(__file__).with_name("app.py")


class LoginRedirectTests(unittest.TestCase):
    def test_callback_addresses(self):
        tree = ast.parse(APP_FILE.read_text(encoding="utf-8"))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_resolve_oauth_redirect_url"
        )
        headers = {}
        secret_get = Mock(return_value=None)
        namespace = {
            "st": SimpleNamespace(context=SimpleNamespace(headers=headers)),
            "_secret_get": secret_get,
            "supabase_block": {},
            "ip_address": ip_address,
            "urlsplit": urlsplit,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(APP_FILE), "exec"), namespace)
        resolve = namespace["_resolve_oauth_redirect_url"]
        for host, forwarded, expected in (
            ("192.168.0.19:8501", "", "https://192.168.0.19:8501"),
            ("localhost:8501", "", "http://localhost:8501"),
            ("127.0.0.1:8501", "", "http://127.0.0.1:8501"),
            ("pb-marketholdings.streamlit.app", "", "https://pb-marketholdings.streamlit.app"),
            ("192.168.0.19:8501", "https", "https://192.168.0.19:8501"),
        ):
            with self.subTest(host=host, forwarded=forwarded):
                headers.clear()
                headers.update({"host": host, "x-forwarded-proto": forwarded})
                self.assertEqual(resolve(), expected)
        secret_get.return_value = "https://configured.example"
        self.assertEqual(resolve(), "https://configured.example")


class CashLedgerTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.trades = []
        self.read_error = None
        self.client = Mock()
        self.trade_updates = []
        self.portfolio_writes = []
        self.portfolio_rows = []
        self.pl_baselines = {}
        self.pl_reset_error = None
        self.pl_read_error = None
        self.client.table.side_effect = self.table
        self.client.rpc.side_effect = self.rpc

    def table(self, name):
        query = Mock()
        for method in ("select", "eq", "order", "range", "gt", "update", "delete", "insert"):
            getattr(query, method).return_value = query
        query.select.return_value = query

        def execute():
            if name == "realized_pl_baselines":
                if self.pl_read_error:
                    raise self.pl_read_error
                user_id = next(call.args[1] for call in query.eq.call_args_list if call.args[0] == "user_id")
                return SimpleNamespace(data=(
                    [{"baseline": self.pl_baselines[user_id]}] if user_id in self.pl_baselines else []
                ))
            if name == "portfolio" and any(
                getattr(query, method).called for method in ("insert", "update", "delete")
            ):
                self.portfolio_writes.append(query)
            if name == "portfolio":
                return SimpleNamespace(data=self.portfolio_rows)
            if name == "permanent_ledger":
                if query.update.called:
                    updates = query.update.call_args.args[0]
                    record_id = next(call.args[1] for call in query.eq.call_args_list if call.args[0] == "id")
                    self.trade_updates.append((record_id, updates))
                    next(row for row in self.trades if row["id"] == record_id).update(updates)
                rows = self.trades
                user_id = next(
                    (call.args[1] for call in query.eq.call_args_list if call.args[0] == "user_id"),
                    None,
                )
                if user_id is not None:
                    rows = [row for row in rows if row.get("user_id", user_id) == user_id]
                if query.range.called:
                    start, end = query.range.call_args.args
                    rows = rows[start:end + 1]
                return SimpleNamespace(data=rows)
            if name != "cash_transactions":
                return SimpleNamespace(data=[])
            if self.read_error:
                raise self.read_error
            user_id = next(call.args[1] for call in query.eq.call_args_list if call.args[0] == "user_id")
            rows = [row for row in reversed(self.rows) if row["user_id"] == user_id]
            start, end = query.range.call_args.args
            return SimpleNamespace(data=rows[start:end + 1])

        query.execute.side_effect = execute
        return query

    def rpc(self, name, params):
        self.assertIn(name, (
            "record_cash_transaction", "clear_cash_transactions",
            "change_cash_transaction", "reset_realized_pl_total",
        ))
        result = Mock()

        def execute():
            if name == "reset_realized_pl_total":
                if self.pl_reset_error:
                    raise self.pl_reset_error
                self.pl_baselines["alice"] = sum(
                    row.get("realized_pl", 0) or 0 for row in self.trades
                    if row.get("user_id") == "alice"
                )
                return SimpleNamespace(data=None)
            if name == "clear_cash_transactions":
                self.rows[:] = [row for row in self.rows if row["user_id"] != "alice"]
                return SimpleNamespace(data=None)
            if name == "change_cash_transaction":
                row = next(
                    (row for row in self.rows if row.get("id") == params["p_id"] and row["user_id"] == "alice"),
                    None,
                )
                if row is None:
                    raise APIError({"message": "Transaction not found.", "code": "P0001"})
                updated = None if params["p_delete"] else {
                    **row,
                    "transaction_type": params["p_transaction_type"],
                    "amount_cents": params["p_amount_cents"],
                    "timestamp": params["p_timestamp"],
                    "note": params["p_note"],
                }
                proposed = [item for item in self.rows if item is not row]
                if updated:
                    proposed.append(updated)
                balance = sum(
                    item["amount_cents"] * (1 if item["transaction_type"] == "deposit" else -1)
                    for item in proposed if item["user_id"] == "alice"
                )
                if balance < 0:
                    raise APIError({"message": "This change would make the available cash balance negative.", "code": "P0001"})
                self.rows[:] = proposed
                return SimpleNamespace(data=None)
            amount = params["p_amount_cents"]
            balance = sum(
                row["amount_cents"] * (1 if row["transaction_type"] == "deposit" else -1)
                for row in self.rows if row["user_id"] == "alice"
            )
            if params["p_transaction_type"] == "withdrawal" and amount > balance:
                raise APIError({"message": "Withdrawal exceeds the available cash balance.", "code": "P0001"})
            self.rows.append({
                "id": len(self.rows) + 1,
                "user_id": "alice",
                "transaction_type": params["p_transaction_type"],
                "amount_cents": amount,
                "timestamp": "2026-10-20T12:00:00+00:00",
                "note": params["p_note"],
            })
            return SimpleNamespace(data=None)

        result.execute.side_effect = execute
        return result

    def app(self, user_id="alice", guest=False):
        app = AppTest.from_file(str(APP_FILE), default_timeout=15)
        app.secrets["SUPABASE_URL"] = "https://example.supabase.co"
        app.secrets["SUPABASE_ANON_KEY"] = "test-anon-key"
        app.session_state["_supabase_client"] = self.client
        app.session_state["user"] = None if guest else SimpleNamespace(id=user_id)
        app.session_state["guest_mode"] = guest
        app.run()
        self.assertFalse(app.exception)
        return app

    def submit(self, app, amount, label):
        next(field for field in app.text_input if field.label == "Amount ($)").set_value(amount)
        next(button for button in app.button if button.label == label).click()
        app.run()
        self.assertFalse(app.exception)

    def balance(self, app):
        return next(metric.value for metric in app.metric if metric.label == "Available cash")

    def dialog_app(self, trade):
        tree = ast.parse(APP_FILE.read_text(encoding="utf-8"))
        names = {
            "_edit_trade_dialog", "_ledger_display_label", "_parse_positive_int",
            "_parse_positive_float", "_records_after_ledger_change",
            "_validate_ledger_records", "_ledger_sort_key", "_update_ledger_record",
            "_delete_ledger_record",
        }
        functions = [
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        for function in functions:
            function.decorator_list = []
        source = (
            "import streamlit as st\nfrom datetime import datetime\n"
            "supabase = st.session_state.client\n"
            "rebuild_portfolio_from_ledger = st.session_state.rebuild\n"
            + ast.unparse(ast.Module(body=functions, type_ignores=[]))
            + "\n_edit_trade_dialog(st.session_state.selected_trade)\n"
        )
        app = AppTest.from_string(source)
        app.session_state["client"] = self.client
        app.session_state["rebuild"] = Mock()
        app.session_state["user"] = SimpleNamespace(id="alice")
        app.session_state["selected_trade"] = trade
        app.run()
        self.assertFalse(app.exception)
        return app

    def test_signed_in_cash_survives_new_session_and_is_user_filtered(self):
        app = self.app()
        self.submit(app, "100.25", "Deposit")
        self.submit(app, "25.10", "Withdraw")
        self.assertEqual(self.balance(app), "$75.15")
        self.assertEqual(self.balance(self.app()), "$75.15")
        self.assertEqual(self.balance(self.app(user_id="bob")), "$0.00")

    def test_overdraw_and_invalid_amount_do_not_change_balance(self):
        app = self.app()
        self.submit(app, "10.00", "Deposit")
        self.submit(app, "10.01", "Withdraw")
        self.assertTrue(any("exceeds" in error.value for error in app.error))
        self.assertEqual(self.balance(app), "$10.00")
        self.submit(app, "1.001", "Deposit")
        self.assertEqual(len(self.rows), 1)
        self.assertTrue(any("whole cents" in error.value for error in app.error))

    def test_guest_cash_does_not_write_to_supabase(self):
        app = self.app(guest=True)
        self.submit(app, "12.34", "Deposit")
        self.assertEqual(self.balance(app), "$12.34")
        self.client.rpc.assert_not_called()
        self.assertEqual(self.rows, [])

    def test_load_failure_does_not_display_zero_balance_or_entry_form(self):
        self.read_error = APIError({"message": "Cash table unavailable", "code": "PGRST205"})
        app = self.app()
        self.assertTrue(any("Unable to load cash" in error.value for error in app.error))
        self.assertFalse(any(metric.label == "Available cash" for metric in app.metric))
        self.assertFalse(any(field.label == "Amount ($)" for field in app.text_input))

    def test_all_pages_are_included_in_balance(self):
        self.rows = [
            {
                "user_id": "alice",
                "transaction_type": "deposit",
                "amount_cents": 1,
                "timestamp": "2026-10-20T12:00:00+00:00",
                "note": "",
            }
            for _ in range(1001)
        ]
        self.assertEqual(self.balance(self.app()), "$10.01")

    def test_row_edit_opens_selected_trade_without_picker(self):
        self.trades = [
            {
                "id": "first", "timestamp": "2026-10-20T12:00:00",
                "action": "BUY", "symbol": "AAPL", "quantity": 2, "price": 150,
            },
            {
                "id": "second", "timestamp": "2026-10-20T13:00:00",
                "action": "BUY", "symbol": "MSFT", "quantity": 3, "price": 200,
            },
        ]
        app = self.app()
        self.assertFalse(any(widget.label == "Choose trade" for widget in app.selectbox))
        edit_buttons = [button for button in app.button if button.label == "Edit selected trade"]
        self.assertEqual(len(edit_buttons), 1)
        self.assertTrue(edit_buttons[0].disabled)
        original_dataframe = st.dataframe

        def select_second_trade(*args, **kwargs):
            result = original_dataframe(*args, **kwargs)
            if kwargs.get("selection_mode") == "single-row":
                return {"selection": {"rows": [1]}}
            return result

        with patch("streamlit.dataframe", side_effect=select_second_trade):
            app.run()
            next(button for button in app.button if button.label == "Edit selected trade").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any(field.label == "Symbol" and field.value == "MSFT" for field in app.text_input))
        self.assertTrue(any(button.label == "Save Changes" for button in app.button))
        self.assertTrue(any(button.label == "Delete Trade" for button in app.button))
        self.assertTrue(any(button.label == "Delete All" for button in app.button))
        self.assertEqual(next(field.value for field in app.date_input if field.label == "Trade date"), date(2026, 10, 20))

    def test_edit_trade_date_preserves_time_and_timezone(self):
        self.trades = [{
            "id": "buy", "timestamp": "2026-10-20T13:14:15.123456+02:00",
            "action": "BUY", "symbol": "MSFT", "quantity": 3, "price": 200,
        }]
        app = self.dialog_app(self.trades[0])
        next(field for field in app.date_input if field.label == "Trade date").set_value(date(2026, 10, 19))
        next(button for button in app.button if button.label == "Save Changes").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(
            self.trades[0]["timestamp"], "2026-10-19T13:14:15.123456+02:00",
            [error.value for error in app.error],
        )
        self.assertEqual(self.trade_updates[0][0], "buy")

    def test_date_change_cannot_move_buy_after_sale(self):
        self.trades = [
            {"id": "buy", "timestamp": "2026-10-19T12:00:00", "action": "BUY",
             "symbol": "MSFT", "quantity": 3, "price": 200},
            {"id": "sell", "timestamp": "2026-10-20T12:00:00", "action": "SELL",
             "symbol": "MSFT", "quantity": 1, "price": 210},
        ]
        app = self.dialog_app(self.trades[0])
        next(field for field in app.date_input if field.label == "Trade date").set_value(date(2026, 10, 21))
        next(button for button in app.button if button.label == "Save Changes").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(
            any("shares available from earlier buys" in error.value for error in app.error),
            [error.value for error in app.error],
        )
        self.assertEqual(self.trade_updates, [])
        self.assertEqual(self.trades[0]["timestamp"], "2026-10-19T12:00:00")

    def test_holdings_are_inside_separate_privacy_toggle(self):
        app = self.app(guest=True)
        app.session_state["guest_portfolio"] = [{
            "symbol": "AAPL",
            "quantity": 2,
            "avg_price": 150.25,
            "last_updated": "2026-10-20T12:00:00",
        }]
        app.run()
        self.assertFalse(app.exception)
        holdings = next(expander for expander in app.expander if expander.label == "Current Holdings")
        self.assertFalse(holdings.proto.expanded)
        self.assertEqual(holdings.metric[0].label, "Running Realized P/L")
        table = holdings.dataframe[0].value
        self.assertEqual(table.iloc[0]["SYMBOL"], "AAPL")
        self.assertEqual(table.iloc[0]["QTY"], 2)
        self.assertEqual(table.iloc[0]["PRICE"], 150.25)
        self.assertTrue(any(expander.label == "Cash Ledger" for expander in app.expander))
        self.assertTrue(any(expander.label == "Trade History" for expander in app.expander))

    def test_guest_sale_tracks_lots_and_realized_profit(self):
        app = self.app(guest=True)
        app.session_state["guest_portfolio"] = [
            {"id": "low", "symbol": "MSFT", "quantity": 2, "avg_price": 100,
             "last_updated": "2026-10-19T12:00:00"},
            {"id": "high", "symbol": "MSFT", "quantity": 3, "avg_price": 200,
             "last_updated": "2026-10-20T12:00:00"},
        ]
        next(field for field in app.text_input if field.label == "Stock Symbol").set_value("MSFT")
        next(field for field in app.text_input if field.label == "Quantity").set_value("3")
        next(field for field in app.text_input if field.label == "Price per Share").set_value("250")
        next(button for button in app.button if button.label == "Log Sell").click().run()
        self.assertFalse(app.exception)
        lots = [lot for lot in app.session_state["guest_portfolio"] if lot["quantity"] > 0]
        self.assertEqual([(lot["quantity"], lot["avg_price"]) for lot in lots], [(2, 200)])
        ledger = app.session_state["guest_ledger"]
        self.assertEqual([row["quantity"] for row in ledger], [2, 1])
        self.assertEqual([row["realized_pl"] for row in ledger], [300, 50])
        self.assertTrue(any("Realized Profit: $350.00" in message.value for message in app.success))
        self.assertEqual(next(metric.value for metric in app.metric if metric.label == "Running Realized P/L"), "$350.00")

    def test_rebuild_preserves_historical_pl_and_other_symbol_lots(self):
        self.trades = [
            {"id": "a", "timestamp": "2026-10-18", "action": "BUY",
             "symbol": "AAPL", "quantity": 4, "price": 50},
            {"id": "m", "timestamp": "2026-10-19", "action": "BUY",
             "symbol": "MSFT", "quantity": 3, "price": 100},
            {"id": "s", "timestamp": "2026-10-20", "action": "SELL",
             "symbol": "MSFT", "quantity": 1, "price": 150, "realized_pl": 50},
        ]
        tree = ast.parse(APP_FILE.read_text(encoding="utf-8"))
        names = {
            "rebuild_portfolio_from_ledger", "_validate_ledger_records",
            "_ledger_sort_key", "_update_ledger_record", "_load_pl_ledger",
        }
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        save_lot = Mock()
        namespace = {"supabase": self.client, "save_portfolio_row": save_lot}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(APP_FILE), "exec"), namespace)
        namespace["rebuild_portfolio_from_ledger"]("alice")
        lots = [call.args[0] for call in save_lot.call_args_list]
        self.assertEqual([(lot["symbol"], lot["quantity"]) for lot in lots], [("AAPL", 4), ("MSFT", 2)])
        self.assertEqual(self.trades[2]["realized_pl"], 50)
        self.assertEqual(self.trades[2]["avg_buy_price"], 100)
        self.assertTrue(all("realized_pl" in updates for _, updates in self.trade_updates))

    def test_pl_display_has_no_manual_recalculation_controls(self):
        self.trades = [
            {"id": "buy", "timestamp": "2026-10-19", "action": "BUY",
             "symbol": "MSFT", "quantity": 3, "price": 100, "realized_pl": 0},
            {"id": "sell", "timestamp": "2026-10-20", "action": "SELL",
             "symbol": "MSFT", "quantity": 2, "price": 90, "realized_pl": 0},
        ]
        app = self.app()
        self.assertFalse(any("Recalculate" in button.label for button in app.button))
        self.assertFalse(any("recalculate" in field.label for field in app.checkbox))
        self.assertEqual(next(metric.value for metric in app.metric if metric.label == "Running Realized P/L"), "$0.00")
        self.assertEqual(self.trade_updates, [])
        self.client.rpc.assert_not_called()
        self.assertEqual(self.portfolio_writes, [])

    def test_holdings_sort_by_symbol_then_descending_price(self):
        self.portfolio_rows = [
            {"symbol": symbol, "quantity": quantity, "avg_price": price, "last_updated": timestamp}
            for symbol, quantity, price, timestamp in (
                ("MSFT", 1, 90, "2026-10-20"),
                ("AAPL", 2, 100, "2026-10-20"),
                ("MSFT", 3, 200, "2026-10-19"),
                ("AAPL", 4, 150, "2026-10-20"),
                ("AAPL", 5, 150, "2026-10-19"),
            )
        ]
        app = self.app()
        holdings = next(expander for expander in app.expander if expander.label == "Current Holdings")
        rows = holdings.dataframe[0].value.to_dict("records")
        self.assertEqual(
            [(row["SYMBOL"], row["PRICE"], row["QTY"]) for row in rows],
            [("AAPL", 150, 5), ("AAPL", 150, 4), ("AAPL", 100, 2),
             ("MSFT", 200, 3), ("MSFT", 90, 1)],
        )
        self.assertEqual(self.portfolio_writes, [])
        self.assertEqual(self.trade_updates, [])

    def test_pl_loads_all_ledger_pages(self):
        self.trades = [
            {"id": str(index), "timestamp": "2026-10-20", "action": "BUY",
             "symbol": "MSFT", "quantity": 1, "price": 100, "realized_pl": 1}
            for index in range(1001)
        ]
        app = self.app()
        self.assertEqual(next(metric.value for metric in app.metric if metric.label == "Running Realized P/L"), "$1,001.00")

    def pl_total(self, app):
        return next(metric.value for metric in app.metric if metric.label == "Running Realized P/L")

    def reset_pl(self, app):
        next(field for field in app.checkbox if field.label == "Confirm reset P/L total").check()
        next(button for button in app.button if button.label == "Reset P/L total").click().run()
        self.assertFalse(app.exception)

    def test_pl_reset_is_confirmed_persistent_and_does_not_change_records(self):
        self.trades = [
            {"id": "sale", "user_id": "alice", "realized_pl": 125.25},
            {"id": "other-sale", "user_id": "bob", "realized_pl": 40},
        ]
        self.rows = [self.cash_row()]
        original_trades, original_cash = deepcopy(self.trades), deepcopy(self.rows)
        self.pl_baselines["bob"] = 30
        app = self.app()
        next(button for button in app.button if button.label == "Reset P/L total").click().run()
        self.client.rpc.assert_not_called()
        self.assertEqual(self.pl_total(app), "$125.25")
        self.reset_pl(app)
        self.assertEqual(self.pl_total(app), "$0.00")
        self.assertEqual(self.trades, original_trades)
        self.assertEqual(self.rows, original_cash)
        self.assertEqual(self.trade_updates, [])
        self.assertEqual(self.portfolio_writes, [])
        self.assertEqual(self.pl_baselines["bob"], 30)
        self.assertEqual(self.pl_total(self.app()), "$0.00")
        self.trades.append({"id": "loss", "user_id": "alice", "realized_pl": -20.50})
        app.run()
        self.assertEqual(self.pl_total(app), "$-20.50")
        self.reset_pl(app)
        self.assertEqual(self.pl_total(app), "$0.00")
        self.assertEqual(self.pl_total(self.app(user_id="bob")), "$10.00")

    def test_guest_pl_reset_leaves_trades_and_makes_no_database_writes(self):
        app = self.app(guest=True)
        trades = [{"realized_pl": -12.25}]
        app.session_state["guest_ledger"] = deepcopy(trades)
        app.run()
        self.reset_pl(app)
        self.assertEqual(self.pl_total(app), "$0.00")
        self.assertEqual(app.session_state["guest_ledger"], trades)
        app.session_state["guest_ledger"] = trades + [{"realized_pl": 5.50}]
        app.run()
        self.assertEqual(self.pl_total(app), "$5.50")
        self.client.rpc.assert_not_called()
        self.assertEqual(self.portfolio_writes, [])

    def test_pl_reset_failure_does_not_report_success(self):
        self.trades = [{"id": "sale", "user_id": "alice", "realized_pl": 50}]
        self.pl_reset_error = APIError({"message": "Reset unavailable", "code": "P0001"})
        app = self.app()
        self.reset_pl(app)
        self.assertTrue(any("P/L reset could not be confirmed" in error.value for error in app.error))
        self.assertFalse(any("P/L total reset." in notice.value for notice in app.success))
        self.assertEqual(self.pl_total(app), "$50.00")
        self.assertEqual(self.pl_baselines, {})

    def test_pl_baseline_load_failure_is_explicit(self):
        self.pl_read_error = APIError({"message": "Missing migration", "code": "PGRST205"})
        app = self.app()
        self.assertTrue(any("Unable to load realized P/L" in error.value for error in app.error))
        self.assertFalse(any(metric.label == "Running Realized P/L" for metric in app.metric))
        self.assertFalse(any(button.label == "Reset P/L total" for button in app.button))

    def test_transaction_history_is_displayed_inside_cash_toggle(self):
        self.rows = [{
            "user_id": "alice",
            "transaction_type": "deposit",
            "amount_cents": 1234,
            "timestamp": "2026-10-20T12:00:00+00:00",
            "note": "private note",
        }]
        app = self.app()
        self.assertEqual(self.balance(app), "$12.34")
        self.assertFalse(app.toggle(key="show_cash_history").value)
        cash_toggle = next(expander for expander in app.expander if expander.label == "Cash Ledger")
        self.assertEqual(len(cash_toggle.dataframe), 0)
        app.toggle(key="show_cash_history").set_value(True).run()
        cash_toggle = next(expander for expander in app.expander if expander.label == "Cash Ledger")
        self.assertTrue(any("Cash Transaction History" in heading.value for heading in cash_toggle.markdown))
        history = cash_toggle.dataframe[0].value
        self.assertEqual(list(history.columns), ["DATE", "TYPE", "AMOUNT", "NOTE"])
        self.assertEqual(history.iloc[0]["DATE"], "2026-10-20")
        self.assertEqual(history.iloc[0]["AMOUNT"], "$12.34")
        self.assertEqual(history.iloc[0]["NOTE"], "private note")

    def test_clear_all_requires_confirmation_and_only_clears_current_user_cash(self):
        self.rows = [
            {"user_id": user, "transaction_type": "deposit", "amount_cents": 1000,
             "timestamp": "2026-10-20T12:00:00", "note": ""}
            for user in ("alice", "bob")
        ]
        app = self.app()
        app.toggle(key="show_cash_history").set_value(True).run()
        next(button for button in app.button if button.label == "Clear all cash transactions").click().run()
        self.client.rpc.assert_not_called()
        self.assertEqual(len(self.rows), 2)
        next(field for field in app.checkbox if field.label == "Confirm clear all cash transactions").check()
        next(button for button in app.button if button.label == "Clear all cash transactions").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.balance(app), "$0.00")
        self.assertEqual([row["user_id"] for row in self.rows], ["bob"])
        self.client.rpc.assert_called_once_with("clear_cash_transactions", {})

    def test_guest_clear_resets_balance_without_database_write(self):
        app = self.app(guest=True)
        self.submit(app, "25", "Deposit")
        app.toggle(key="show_cash_history").set_value(True).run()
        next(field for field in app.checkbox if field.label == "Confirm clear all cash transactions").check()
        next(button for button in app.button if button.label == "Clear all cash transactions").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.balance(app), "$0.00")
        self.client.rpc.assert_not_called()

    def cash_dialog_app(self, transaction):
        tree = ast.parse(APP_FILE.read_text(encoding="utf-8"))
        functions = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in {"_edit_cash_dialog", "_cash_amount_to_cents"}
        ]
        for function in functions:
            function.decorator_list = []
        source = (
            "import streamlit as st\nfrom datetime import datetime\n"
            "from decimal import Decimal, InvalidOperation\n"
            "supabase = st.session_state.client\n"
            + ast.unparse(ast.Module(body=functions, type_ignores=[]))
            + "\n_edit_cash_dialog(st.session_state.transaction)\n"
        )
        app = AppTest.from_string(source)
        app.session_state["client"] = self.client
        app.session_state["transaction"] = transaction.copy()
        app.run()
        self.assertFalse(app.exception)
        return app

    def cash_row(self, row_id=1, kind="deposit", amount=10000, user="alice"):
        return {
            "id": row_id, "user_id": user, "transaction_type": kind,
            "amount_cents": amount, "timestamp": "2026-10-20T12:34:56.123456+02:00", "note": "original",
        }

    def test_cash_selection_opens_correct_dialog(self):
        self.rows = [self.cash_row(), self.cash_row(2, amount=2000)]
        app = self.app()
        app.toggle(key="show_cash_history").set_value(True).run()
        button = next(button for button in app.button if button.label == "Edit selected transaction")
        self.assertTrue(button.disabled)
        original_dataframe = st.dataframe

        def select_cash_row(*args, **kwargs):
            result = original_dataframe(*args, **kwargs)
            if str(kwargs.get("key", "")).startswith("cash_selection_"):
                return {"selection": {"rows": [0]}}
            return result

        with patch("streamlit.dataframe", side_effect=select_cash_row):
            app.run()
            next(button for button in app.button if button.label == "Edit selected transaction").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any(field.label == "Amount ($)" and field.value == "20.00" for field in app.text_input))
        self.assertTrue(any(button.label == "Delete Transaction" for button in app.button))
        self.assertTrue(any(button.label == "Clear all cash transactions" for button in app.button))

    def test_cash_edit_all_fields_preserves_time(self):
        self.rows = [self.cash_row(), self.cash_row(2, amount=2000)]
        app = self.cash_dialog_app(self.rows[1])
        next(field for field in app.date_input if field.label == "Transaction date").set_value(date(2026, 10, 18))
        next(field for field in app.selectbox if field.label == "Transaction type").select("Withdrawal")
        next(field for field in app.text_input if field.label == "Amount ($)").set_value("15.25")
        next(field for field in app.text_input if field.label == "Note").set_value(" corrected ")
        next(button for button in app.button if button.label == "Save Changes").click().run()
        self.assertFalse(app.exception)
        updated = next(row for row in self.rows if row["id"] == 2)
        self.assertEqual(updated["timestamp"], "2026-10-18T12:34:56.123456+02:00")
        self.assertEqual(updated["transaction_type"], "withdrawal")
        self.assertEqual(updated["amount_cents"], 1525)
        self.assertEqual(updated["note"], "corrected")

    def test_cash_edit_or_delete_cannot_make_balance_negative(self):
        for deleting in (False, True):
            with self.subTest(deleting=deleting):
                self.rows = [self.cash_row(), self.cash_row(2, "withdrawal", 8000)]
                before = [row.copy() for row in self.rows]
                app = self.cash_dialog_app(self.rows[0])
                if not deleting:
                    next(field for field in app.text_input if field.label == "Amount ($)").set_value("10")
                label = "Delete Transaction" if deleting else "Save Changes"
                next(button for button in app.button if button.label == label).click().run()
                self.assertFalse(app.exception)
                self.assertTrue(any("negative" in error.value for error in app.error))
                self.assertEqual(self.rows, before)

    def test_cash_delete_removes_only_selected_transaction(self):
        self.rows = [self.cash_row(), self.cash_row(2, "withdrawal", 2000), self.cash_row(3, user="bob")]
        app = self.cash_dialog_app(self.rows[1])
        next(button for button in app.button if button.label == "Delete Transaction").click().run()
        self.assertFalse(app.exception)
        self.assertEqual([row["id"] for row in self.rows], [1, 3])
        self.client.rpc.assert_called_with("change_cash_transaction", {"p_id": 2, "p_delete": True})

    def test_invalid_cash_edit_does_not_write(self):
        self.rows = [self.cash_row()]
        app = self.cash_dialog_app(self.rows[0])
        next(field for field in app.text_input if field.label == "Amount ($)").set_value("1.001")
        next(button for button in app.button if button.label == "Save Changes").click().run()
        self.assertTrue(any("whole cents" in error.value for error in app.error))
        self.client.rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
