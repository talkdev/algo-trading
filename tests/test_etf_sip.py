"""Offline regression tests for the live Upstox SIP refactor.

Run with: python -m unittest discover -s tests -v
Every broker interaction uses a fake client; these tests never place real orders.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("etf_sip_module", ROOT / "etf-momentum-screener.py")
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


class FakeUpstox:
    def __init__(self, holdings=None, positions=None, orders=None, cash=100000.0):
        self._holdings = holdings or []
        self._positions = positions or []
        self._orders = orders or []
        self._cash = float(cash)
        self.placed = []
        self.details = {}
        self.default_details = {"status": "open", "filled_quantity": 0, "average_price": 0.0}
        self.submit_error = None

    def holdings(self):
        return list(self._holdings)

    def positions(self):
        return list(self._positions)

    def orders_book(self):
        return list(self._orders)

    def funds_available(self):
        return self._cash

    def place_order(self, symbol, instrument_key, side, qty, price, order_type, is_amo=False):
        oid = f"O{len(self.placed) + 1}"
        self.placed.append({"oid": oid, "symbol": symbol, "key": instrument_key, "side": side,
                            "qty": int(qty), "price": float(price), "type": order_type, "amo": bool(is_amo)})
        if self.submit_error:
            raise self.submit_error
        return {"data": {"order_id": oid}}

    def order_details(self, oid):
        d = dict(self.details.get(oid, self.default_details))
        if d.get("status") in ("complete", "filled") and "filled_quantity" not in d:
            d["filled_quantity"] = next((p["qty"] for p in self.placed if p["oid"] == oid), 0)
        if "average_price" not in d:
            d["average_price"] = next((p["price"] for p in self.placed if p["oid"] == oid), 0.0)
        return {"data": d}


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class SipTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = m.Db(pathlib.Path(self.tmp.name) / "test.db")
        for sym in ("A", "B", "C", "OLD", "NEW"):
            self.db.execute(
                "INSERT OR REPLACE INTO etf_universe(symbol,name,category,index_family,status,instrument_key) "
                "VALUES(?,?,?,?,?,?)", (sym, sym, "EQUITY", sym, "ACTIVE", f"NSE_EQ|{sym}"))
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(m, "INVEST_PER_DAY", 5000.0))
        self.stack.enter_context(mock.patch.object(m, "SIP_MODE", True))
        self.stack.enter_context(mock.patch.object(m, "DAILY_EXIT_GUARD", True))

    def tearDown(self):
        self.stack.close()
        self.db.close()
        self.tmp.cleanup()

    def broker(self, client):
        b = m.Broker(self.db, m.TaxEngine(self.db), client)
        self.assertTrue(b.sync_live_book())
        return b

    def seed_price(self, sym, close=100.0, date=None):
        self.db.execute(
            "INSERT OR REPLACE INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (sym, date or m.today_str(), close, close, close, close, close, 1e6, 1e8))

    def cycle(self, broker):
        c = m.TradingCycle.__new__(m.TradingCycle)
        c.db = self.db
        c.broker = broker
        c.tax = m.TaxEngine(self.db)
        c.risk = m.RiskEngine(self.db)
        c.filters = types.SimpleNamespace(spread_p90=lambda *a: 8.0, liquidity_p25=lambda *a: 1e9)
        c.data = types.SimpleNamespace(series=lambda *a, **k: [])
        c.builder = types.SimpleNamespace()
        return c


class TestPortfolioBasis(SipTestCase):
    def test_portfolio_is_broker_market_value_excluding_cash_and_foreign_assets(self):
        client = FakeUpstox(
            holdings=[{"trading_symbol": "A", "quantity": 10, "t1_quantity": 2,
                       "average_price": 100.0, "last_price": 110.0},
                      {"trading_symbol": "FOREIGN", "quantity": 999, "average_price": 1.0, "last_price": 999.0}],
            cash=50000.0)
        b = self.broker(client)
        self.assertAlmostEqual(b.holdings_value(), 1100.0)
        self.assertAlmostEqual(b.equity(), 1100.0)
        self.assertAlmostEqual(b.cash(), 50000.0)
        self.assertAlmostEqual(b.account_equity(), 51100.0)
        self.assertEqual(set(b.holdings()), {"A"})

    def test_fresh_live_book_has_no_fixed_starting_capital_seed(self):
        client = FakeUpstox(cash=123456.0)
        b = self.broker(client)
        self.assertEqual(b.equity(), 0.0)
        self.assertAlmostEqual(b.account_equity(), 123456.0)
        risk = m.RiskEngine(self.db)
        risk.update(b.account_equity(), "first verified live read")
        self.assertAlmostEqual(risk.peak, 123456.0)
        self.assertFalse(hasattr(m, "STARTING_CAPITAL"))
        self.assertFalse(hasattr(m, "PAPER_TRADE"))
        self.assertTrue(m.DAILY_EXIT_GUARD)

    def test_legacy_peak_without_mode_tag_is_rebased_not_treated_as_crash(self):
        self.db.meta_set("equity_peak", "1000000")
        risk = m.RiskEngine(self.db)
        with quiet():
            ctx = risk.update(300000.0, "legacy migration")
        self.assertAlmostEqual(risk.peak, 300000.0)
        self.assertEqual(ctx["state"], "NORMAL")
        self.assertFalse(ctx["all_cash"])


class TestStrictDailyAllowance(SipTestCase):
    def test_reservations_survive_reruns_and_partial_fills_release_only_unfilled_budget(self):
        client = FakeUpstox()
        b = self.broker(client)
        r1 = b.execute("A", "BUY", 20, 100.0, reason="first run")
        self.assertEqual(r1["status"], "PENDING")
        self.assertAlmostEqual(b.sip_committed(), 2004.0)

        b2 = self.broker(client)  # a new process/run reads the same journal
        self.assertAlmostEqual(b2.sip_remaining(), 2996.0)
        too_big = b2.execute("B", "BUY", 30, 100.0, reason="over cap")
        self.assertEqual(too_big["status"], "REJECTED")
        r2 = b2.execute("B", "BUY", 29, 100.0, reason="fits remaining")
        self.assertEqual(r2["status"], "PENDING")
        self.assertAlmostEqual(b2.sip_committed(), 4909.8)

        client.details[r1["broker_order_id"]] = {"status": "partially filled", "filled_quantity": 10, "average_price": 99.0}
        m.resolve_pending_live_orders(self.db, b2)
        row = self.db.one("SELECT * FROM orders WHERE broker_order_id=?", (r1["broker_order_id"],))
        self.assertEqual(row["filled_qty"], 10)
        self.assertAlmostEqual(row["reserved_value"], 990.0 + 10 * 100.2)
        cash_after_partial = b2.cash()
        m.resolve_pending_live_orders(self.db, b2)
        self.assertAlmostEqual(b2.cash(), cash_after_partial)  # cumulative fill applied once

        client.details[r2["broker_order_id"]] = {"status": "cancelled", "filled_quantity": 0, "average_price": 0.0}
        m.resolve_pending_live_orders(self.db, b2)
        cancelled = self.db.one("SELECT reserved_value,status FROM orders WHERE broker_order_id=?", (r2["broker_order_id"],))
        self.assertEqual(cancelled["status"], "REJECTED")
        self.assertAlmostEqual(cancelled["reserved_value"], 0.0)
        self.assertAlmostEqual(b2.sip_committed(), 1992.0)

    def test_ambiguous_submission_keeps_reservation_and_never_fabricates_a_fill(self):
        client = FakeUpstox()
        client.submit_error = TimeoutError("response lost after acceptance")
        b = self.broker(client)
        with quiet():
            r = b.execute("A", "BUY", 20, 100.0, reason="ambiguous")
        self.assertEqual(r["status"], "UNCONFIRMED")
        self.assertAlmostEqual(b.sip_committed(), 2004.0)
        self.assertAlmostEqual(b.cash(), 100000.0)
        self.assertIsNone(self.db.one("SELECT qty FROM positions WHERE symbol='A'"))
        self.assertEqual(b.execute("A", "BUY", 20, 100.0)["status"], "REJECTED")  # no blind retry

    def test_zero_allowance_blocks_buys_but_not_exit_sells(self):
        client = FakeUpstox(holdings=[{"trading_symbol": "A", "quantity": 30,
                                       "average_price": 100.0, "last_price": 100.0}])
        b = self.broker(client)
        c = self.cycle(b)
        c.data = types.SimpleNamespace(series=lambda *a, **k: [200.0] * 260 + [100.0] * 40)
        client.default_details = {"status": "complete", "average_price": 100.0, "filled_quantity": 30}
        self.seed_price("A", 100.0)
        with mock.patch.object(m, "INVEST_PER_DAY", 0.0), quiet():
            self.assertEqual(c.deploy_day(), 0)
            self.assertEqual(b.execute("A", "BUY", 20, 100.0)["status"], "REJECTED")
            self.assertEqual(c.daily_exit_guard(), 1)
        self.assertEqual([p["side"] for p in client.placed], ["SELL"])
        self.assertEqual(client.placed[0]["qty"], 30)

    def test_monthly_rebalance_sells_orphans_without_reinvesting_sale_proceeds(self):
        client = FakeUpstox(holdings=[{"trading_symbol": "OLD", "quantity": 30,
                                       "average_price": 100.0, "last_price": 100.0}])
        b = self.broker(client)
        c = self.cycle(b)
        self.seed_price("OLD", 100.0)
        self.seed_price("NEW", 100.0)
        st = {"symbol": "NEW", "name": "NEW", "close": 100.0, "momentum": 0.2,
              "vol": 0.1, "liq_p25": 1e9, "spread_bps": 8.0}
        selection = {"chosen": [st], "exits": [], "decisions": [(st, "SELECTED", "test")]}
        sized = {"weights": {"NEW": 1.0}, "risk_scale": 1.0, "port_vol": 0.1,
                 "capped": {}, "delta": 0.0, "cond": 1.0, "vol_reachable": True}
        c.builder.select = lambda *a, **k: selection
        c.builder.size = lambda *a, **k: sized
        c.collect_stats = lambda as_of: [st]
        with mock.patch.object(m, "INVEST_PER_DAY", 0.0), quiet():
            c.run(force_exec=True, as_of_override=m.today_str(), quiet=True)
        self.assertEqual([p["side"] for p in client.placed], ["SELL"])
        self.assertEqual(client.placed[0]["symbol"], "OLD")
        self.assertAlmostEqual(b.sip_committed(), 0.0)

    def test_run_daily_still_runs_exits_and_rebalance_at_zero_cap(self):
        client = FakeUpstox(holdings=[{"trading_symbol": "A", "quantity": 30,
                                       "average_price": 100.0, "last_price": 100.0}])
        b = self.broker(client)
        c = self.cycle(b)
        self.seed_price("A", 100.0, date=m.last_completed_session())
        calls = []
        c.apply_derisk = lambda ctx, prices: calls.append("derisk")
        c.consume_pending_sells = lambda prices: calls.append("pending_sells") or 0
        c.run = lambda **kw: calls.append("rebalance") or 0.0
        c.daily_exit_guard = lambda: calls.append("exit") or 0
        c.deploy_day = lambda: calls.append("deploy") or 0
        with mock.patch.object(m, "INVEST_PER_DAY", 0.0), quiet():
            hv = c.run_daily(force_exec=True, final_status=False)
        self.assertAlmostEqual(hv, 3000.0)
        self.assertIn("rebalance", calls)
        self.assertIn("exit", calls)
        self.assertEqual(calls[-1], "deploy")
        self.assertEqual(client.placed, [])


class TestLiveOrderSafety(SipTestCase):
    def test_account_and_order_http_contracts_use_one_v2_prefix_and_version_header(self):
        # Exercise the real urllib client, but replace its transport. These
        # checks must never contact Upstox or submit a real order.
        client = m.UpstoxClient({"UPSTOX_ACCESS_TOKEN": "offline-test"})
        base = "https://api.upstox.com/v2"
        cases = [
            ("holdings", (), "/portfolio/long-term-holdings", "GET", []),
            ("positions", (), "/portfolio/short-term-positions", "GET", []),
            ("funds_available", (), "/user/get-funds-and-margin?segment=SEC", "GET",
             {"equity": {"available_margin": 12345.0}}),
            ("orders_book", (), "/order/retrieve-all", "GET", []),
            ("order_details", ("O1",), "/order/details?order_id=O1", "GET",
             {"order_id": "O1", "status": "complete"}),
            ("place_order", ("A", "NSE_EQ|A", "BUY", 20, 100.2, "LIMIT"),
             "/order/place", "POST", {"order_id": "O1"}),
        ]
        for name, args, path, method, data in cases:
            with self.subTest(endpoint=name):
                payload = json.dumps({"status": "success", "data": data}).encode("utf-8")
                with mock.patch.object(m.urllib.request, "urlopen",
                                       return_value=io.BytesIO(payload)) as op:
                    getattr(client, name)(*args)
                self.assertEqual(op.call_count, 1)
                request = op.call_args.args[0]
                self.assertEqual(request.full_url, base + path)
                self.assertEqual(request.get_method(), method)
                headers = {key.lower(): value for key, value in request.headers.items()}
                self.assertEqual(headers.get("api-version"), "2.0")
                self.assertEqual(headers["authorization"], "Bearer offline-test")
                self.assertEqual(headers["accept"], "application/json")
                if name == "place_order":
                    body = json.loads(request.data)
                    self.assertEqual(body["instrument_token"], "NSE_EQ|A")
                    self.assertEqual(body["transaction_type"], "BUY")
                    self.assertEqual(body["quantity"], 20)
                    self.assertEqual(body["product"], "D")
                    self.assertEqual(body["order_type"], "LIMIT")
                    self.assertEqual(body["price"], 100.2)
                    self.assertFalse(body["is_amo"])

    def test_order_submission_is_not_retried_on_network_failure(self):
        client = m.UpstoxClient({"UPSTOX_ACCESS_TOKEN": "offline-test"})
        with mock.patch.object(m.urllib.request, "urlopen", side_effect=TimeoutError("lost response")) as op:
            with self.assertRaises(m.UpstoxError):
                client.place_order("A", "NSE_EQ|A", "BUY", 20, 100.2, "LIMIT")
        self.assertEqual(op.call_count, 1)
        request = op.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.upstox.com/v2/order/place")
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers.get("api-version"), "2.0")

    def test_after_market_orders_are_flagged_amo(self):
        client = FakeUpstox()
        b = self.broker(client)
        with mock.patch.object(m, "market_open_now", return_value=False), quiet():
            b.execute("A", "BUY", 20, 100.0)
        self.assertTrue(client.placed[0]["amo"])
        client2 = FakeUpstox()
        b2 = m.Broker(self.db, m.TaxEngine(self.db), client2)
        # same symbol already has an open order from b; use a fresh symbol
        self.assertTrue(b2.sync_live_book())
        with mock.patch.object(m, "market_open_now", return_value=True), quiet():
            b2.execute("B", "BUY", 20, 100.0)
        self.assertFalse(client2.placed[0]["amo"])


if __name__ == "__main__":
    unittest.main()
