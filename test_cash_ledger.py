import ast
from datetime import date
from ipaddress import ip_address
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit

from postgrest.exceptions import APIError
from streamlit.testing.v1 import AppTest


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
        self.client.table.side_effect = self.table
        self.client.rpc.side_effect = self.rpc

    def table(self, name):
        query = Mock()
        for method in ("select", "eq", "order", "range", "gt", "update", "delete", "insert"):
            getattr(query, method).return_value = query
        query.select.return_value = query

        def execute():
            if name == "permanent_ledger":
                if query.update.called:
                    updates = query.update.call_args.args[0]
                    record_id = next(call.args[1] for call in query.eq.call_args_list if call.args[0] == "id")
                    self.trade_updates.append((record_id, updates))
                    next(row for row in self.trades if row["id"] == record_id).update(updates)
                return SimpleNamespace(data=self.trades)
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
        self.assertIn(name, ("record_cash_transaction", "clear_cash_transactions"))
        result = Mock()

        def execute():
            if name == "clear_cash_transactions":
                self.rows[:] = [row for row in self.rows if row["user_id"] != "alice"]
                return SimpleNamespace(data=None)
            amount = params["p_amount_cents"]
            balance = sum(
                row["amount_cents"] * (1 if row["transaction_type"] == "deposit" else -1)
                for row in self.rows if row["user_id"] == "alice"
            )
            if params["p_transaction_type"] == "withdrawal" and amount > balance:
                raise APIError({"message": "Withdrawal exceeds the available cash balance.", "code": "P0001"})
            self.rows.append({
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
        self.assertEqual(len([button for button in app.button if button.label == "Edit"]), 2)
        app.button(key="edit_trade_second").click().run()
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
        self.assertEqual(list(history.columns), ["DATE / TIME", "TYPE", "AMOUNT", "NOTE"])
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


if __name__ == "__main__":
    unittest.main()
