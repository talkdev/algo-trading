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
        return {"status": "success", "data": {"order_ids": [oid]}}

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
        # Order-path tests default to a regular session inside the place-order
        # API window; closed-market/window tests override these explicitly.
        self.stack.enter_context(mock.patch.object(m, "market_open_now", return_value=True))
        self.stack.enter_context(mock.patch.object(m, "amo_window_open_now", return_value=True))

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
        risk.update(b.holdings_value(), "first verified live read")
        self.assertAlmostEqual(risk.peak, 0.0)  # cash is not the ETF sleeve
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

    def test_small_daily_cap_uses_buy_floor_and_whole_share_limit_rounding(self):
        client = FakeUpstox(cash=100000.0)
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0), \
             mock.patch.object(m, "MIN_BUY_VALUE", 500.0), \
             mock.patch.object(m, "MIN_TRADE_VALUE", 2000.0), quiet():
            b = self.broker(client)
            ok = b.execute("A", "BUY", 9, 100.0, reason="small SIP")
            self.assertEqual(ok["status"], "PENDING")
            self.assertEqual(len(client.placed), 1)
            self.assertAlmostEqual(b.sip_committed(), 901.8)
            tiny = b.execute("B", "BUY", 4, 100.0, reason="below buy floor")
            self.assertEqual(tiny["status"], "REJECTED")
            self.assertIn("MIN_BUY_VALUE", tiny["reason"])
            self.assertEqual(len(client.placed), 1)
            self.assertEqual(m.day_slices({"A": 1000.0}, 1000.0), {"A": 1000.0})
            self.assertEqual(m.day_slices({"A": 400.0}, 1000.0), {})

    def test_deploy_day_can_place_a_small_sip_order_under_the_daily_cap(self):
        client = FakeUpstox(cash=100000.0)
        b = self.broker(client)
        c = self.cycle(b)
        self.seed_price("A", 100.0)
        self.db.meta_set(m.SIP_BASKET_KEY, "A")
        self.db.meta_set(m.SIP_BASKET_DATE_KEY, m.today_str())
        self.db.meta_set(m.SIP_TARGETS_KEY, m.json.dumps({"A": 1.0}))
        c._live_quotes = lambda symbols: ({s: 100.0 for s in symbols}, {}, {s: True for s in symbols})
        c.filters.check_execution = lambda *a, **k: (True, "", {})
        c.filters.inav_premium_block = lambda *a, **k: (False, None, None)
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0), \
             mock.patch.object(m, "MIN_BUY_VALUE", 500.0), \
             mock.patch.object(m, "market_open_now", return_value=True), quiet():
            submitted = c.deploy_day()
        self.assertEqual(submitted, 1)
        self.assertEqual(len(client.placed), 1)
        self.assertEqual(client.placed[0]["side"], "BUY")
        self.assertEqual(client.placed[0]["qty"], 9)
        self.assertFalse(client.placed[0]["amo"])
        self.assertAlmostEqual(b.sip_committed(), 901.8)

    def test_sell_trim_dust_floor_is_not_lowered_by_small_buy_floor(self):
        client = FakeUpstox(holdings=[{"trading_symbol": "A", "quantity": 30,
                                       "average_price": 100.0, "last_price": 100.0}])
        b = self.broker(client)
        c = self.cycle(b)
        self.seed_price("A", 100.0)
        st = {"symbol": "A", "name": "A", "close": 100.0, "momentum": 0.2,
              "vol": 0.1, "liq_p25": 1e9, "spread_bps": 8.0}
        selection = {"chosen": [st], "exits": [], "decisions": [(st, "HOLD", "test")]}
        sized = {"weights": {"A": 0.9}, "risk_scale": 1.0, "port_vol": 0.1,
                 "capped": {}, "delta": 0.0, "cond": 1.0, "vol_reachable": True}
        c.builder.select = lambda *a, **k: selection
        c.builder.size = lambda *a, **k: sized
        c.collect_stats = lambda as_of: [st]
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0), \
             mock.patch.object(m, "MIN_BUY_VALUE", 500.0), \
             mock.patch.object(m, "MIN_TRADE_VALUE", 2000.0), \
             mock.patch.object(m, "NO_TRADE_BAND", 0.0), quiet():
            c.run(force_exec=True, as_of_override=m.today_str(), quiet=True)
        self.assertEqual(client.placed, [])  # Rs 300 trim stays below the Rs 2,000 sell floor

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


class TestAuditFixes(SipTestCase):
    def test_locked_contract_constants_are_enforced(self):
        self.assertEqual(m.LOCKED_CONTRACT_ID, "SIP-LOCK-v2")
        self.assertTrue(m.SIP_MODE)
        self.assertTrue(m.DAILY_EXIT_GUARD)
        self.assertEqual(m.config_errors(), [])
        with mock.patch.object(m, "DAILY_EXIT_GUARD", False):
            self.assertTrue(any("DAILY_EXIT_GUARD" in e for e in m.config_errors()))
        with mock.patch.object(m, "SIP_MODE", False):
            self.assertTrue(any("SIP_MODE" in e for e in m.config_errors()))

    def test_small_cap_blocks_buys_only_and_is_not_a_fatal_config_error(self):
        # Locked rule L7: a buy-side misconfiguration must never kill exits.
        with mock.patch.object(m, "INVEST_PER_DAY", 100.0):
            self.assertEqual(m.config_errors(), [])
            self.assertIn("minimum buy ticket", m.buy_config_blocked())
        with mock.patch.object(m, "INVEST_PER_DAY", 0.0):
            self.assertEqual(m.buy_config_blocked(), "")
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0):
            self.assertEqual(m.buy_config_blocked(), "")

    def test_bad_buy_cap_still_runs_the_daily_exit_guard(self):
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
        with mock.patch.object(m, "INVEST_PER_DAY", 100.0), quiet():
            c.run_daily(force_exec=True, final_status=False)
        self.assertIn("exit", calls)
        self.assertEqual(calls[-1], "deploy")

    def test_zero_order_sip_run_prints_a_reason_for_every_basket_etf(self):
        client = FakeUpstox(cash=100000.0)
        b = self.broker(client)
        c = self.cycle(b)
        self.seed_price("A", 100.0)
        self.db.meta_set(m.SIP_BASKET_KEY, "A")
        self.db.meta_set(m.SIP_BASKET_DATE_KEY, m.today_str())
        self.db.meta_set(m.SIP_TARGETS_KEY, json.dumps({"A": 1.0}))
        # Declining series: the trend guard parks the only basket name.
        c.data = types.SimpleNamespace(
            series=lambda sym, *a, **k: [{"adj_close": 200.0 - i * 0.3} for i in range(300)])
        c._live_quotes = lambda symbols: ({}, {}, {})
        out = io.StringIO()
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0), \
             mock.patch.object(m, "market_open_now", return_value=True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(c.deploy_day(), 0)
        text = out.getvalue()
        self.assertIn("Reason per basket ETF", text)
        self.assertIn("A: trend guard", text)
        self.assertEqual(client.placed, [])

    def test_replay_of_the_2026_11_08_state_submits_one_v3_amo_buy(self):
        # The exact reported Windows state: Rs 7,542 of ETF holdings already
        # above target, Rs 2.07 L cash, Rs 1,000 cap, three-name basket.
        for sym, px in (("PHARMABEES", 29.22), ("MOCAPITAL", 57.84), ("SML100CASE", 12.0)):
            self.db.execute(
                "INSERT OR REPLACE INTO etf_universe(symbol,name,category,index_family,"
                "status,instrument_key) VALUES(?,?,?,?,?,?)",
                (sym, sym, "EQUITY", sym, "ACTIVE", f"NSE_EQ|{sym}"))
            self.seed_price(sym, px)
        client = FakeUpstox(cash=207000.0, holdings=[
            {"trading_symbol": "PHARMABEES", "quantity": 173, "average_price": 27.22, "last_price": 29.22},
            {"trading_symbol": "MOCAPITAL", "quantity": 43, "average_price": 51.74, "last_price": 57.84}])
        client.default_details = {"status": "open", "filled_quantity": 0, "average_price": 0.0}
        b = self.broker(client)
        self.assertAlmostEqual(b.holdings_value(), 7542.18, places=2)
        c = self.cycle(b)
        c.data = types.SimpleNamespace(
            series=lambda sym, *a, **k: [{"adj_close": 100.0 + i * 0.1} for i in range(300)])
        self.db.meta_set(m.SIP_BASKET_KEY, "PHARMABEES,MOCAPITAL,SML100CASE")
        self.db.meta_set(m.SIP_BASKET_DATE_KEY, m.today_str())
        self.db.meta_set(m.SIP_TARGETS_KEY, json.dumps(
            {"PHARMABEES": 0.1735, "MOCAPITAL": 0.1735, "SML100CASE": 0.1735}))
        c._live_quotes = lambda symbols: ({}, {}, {})
        c.filters.check_execution = lambda *a, **k: (True, "", {})
        c.filters.inav_premium_block = lambda *a, **k: (False, None, None)
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0), \
             mock.patch.object(m, "market_open_now", return_value=False), \
             mock.patch.object(m, "amo_window_open_now", return_value=True), quiet():
            submitted = c.deploy_day()
        self.assertEqual(submitted, 1)
        self.assertEqual(len(client.placed), 1)
        order = client.placed[0]
        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["type"], "LIMIT")
        self.assertTrue(order["amo"])
        self.assertGreaterEqual(b.sip_committed(), m.MIN_BUY_VALUE)
        self.assertLessEqual(b.sip_committed(), 1000.0)

    def test_upstox_charge_schedule_small_ticket_and_dp_sell(self):
        buy = m.calc_charges("BUY", 1000.0)
        self.assertAlmostEqual(buy["brokerage"], 0.25)
        self.assertAlmostEqual(buy["dp"], 0.0)
        sell = m.calc_charges("SELL", 1000.0)
        self.assertAlmostEqual(sell["dp"], 23.60)
        self.assertAlmostEqual(sell["brokerage"], 0.25)
        capped = m.calc_charges("BUY", 1_000_000.0)
        self.assertAlmostEqual(capped["brokerage"], 20.0)

    def test_sip_contribution_buys_the_daily_cap_even_when_holdings_are_overweight(self):
        client = FakeUpstox(cash=100000.0, holdings=[
            {"trading_symbol": "A", "quantity": 1000, "average_price": 100.0, "last_price": 100.0}])
        b = self.broker(client)
        c = self.cycle(b)
        self.seed_price("A", 100.0)
        self.seed_price("B", 100.0)
        self.db.meta_set(m.SIP_BASKET_KEY, "A,B")
        self.db.meta_set(m.SIP_BASKET_DATE_KEY, m.today_str())
        self.db.meta_set(m.SIP_TARGETS_KEY, json.dumps({"A": 0.5, "B": 0.5}))
        c._live_quotes = lambda symbols: ({s: 100.0 for s in symbols}, {}, {s: True for s in symbols})
        c.filters.check_execution = lambda *a, **k: (True, "", {})
        c.filters.inav_premium_block = lambda *a, **k: (False, None, None)
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0), mock.patch.object(m, "market_open_now", return_value=True), quiet():
            submitted = c.deploy_day()
        self.assertEqual(submitted, 1)
        self.assertEqual(len(client.placed), 1)
        self.assertGreaterEqual(b.sip_committed(), m.MIN_BUY_VALUE)
        self.assertLessEqual(b.sip_committed(), 1000.0)

    def test_hard_block_drops_symbol_from_basket_and_targets(self):
        client = FakeUpstox(cash=100000.0)
        b = self.broker(client)
        c = self.cycle(b)
        self.seed_price("B", 100.0)
        self.db.meta_set(m.SIP_BASKET_KEY, "A,B")
        self.db.meta_set(m.SIP_BASKET_DATE_KEY, m.today_str())
        self.db.meta_set(m.SIP_TARGETS_KEY, json.dumps({"A": 0.5, "B": 0.5}))
        self.db.meta_set(m.DEPLOY_SKIP_KEY, json.dumps({"A": m.DEPLOY_HARD_BLOCK_DAYS}))
        c.data = types.SimpleNamespace(series=lambda sym, *a, **k: [{"adj_close": 100.0 + i * 0.1} for i in range(300)])
        c._live_quotes = lambda symbols: ({s: 100.0 for s in symbols}, {}, {s: True for s in symbols})
        c.filters.check_execution = lambda *a, **k: (True, "", {})
        c.filters.inav_premium_block = lambda *a, **k: (False, None, None)
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0), mock.patch.object(m, "market_open_now", return_value=True), quiet():
            c.deploy_day()
        self.assertEqual(self.db.meta_get(m.SIP_BASKET_KEY, ""), "B")
        self.assertNotIn("A", json.loads(self.db.meta_get(m.SIP_TARGETS_KEY, "{}")))

    def test_exit_guard_counts_accepted_pending_sells_and_dma_exit_needs_confirmation(self):
        client = FakeUpstox(holdings=[{"trading_symbol": "A", "quantity": 30,
                                       "average_price": 100.0, "last_price": 100.0}])
        client.default_details = {"status": "open", "filled_quantity": 0, "average_price": 0.0}
        b = self.broker(client)
        c = self.cycle(b)
        c.data = types.SimpleNamespace(series=lambda *a, **k: [200.0] * 260 + [100.0] * 40)
        self.seed_price("A", 100.0)
        with quiet():
            self.assertEqual(c.daily_exit_guard(), 1)
        self.assertEqual(client.placed[0]["side"], "SELL")
        self.assertEqual(client.placed[0]["type"], "LIMIT")
        self.assertLess(client.placed[0]["price"], 100.0)

        rise = [100.0 + i * 0.1 for i in range(300)]
        dma = sum(rise[-200:]) / 200.0
        one_below = rise[:-1] + [dma - 1.0]
        two_below = rise[:-2] + [dma - 1.0, dma - 1.0]
        with mock.patch.object(m, "EXIT_DMA_CONFIRM_CLOSES", 2):
            self.assertFalse(m.exit_rules_fire(one_below))
            self.assertTrue(m.exit_rules_fire(two_below))

    def test_old_account_equity_peak_rebases_to_etf_holdings_not_a_false_crash(self):
        self.db.meta_set("equity_peak", "214000")
        self.db.meta_set("equity_peak_mode", "LIVE_SIP")
        risk = m.RiskEngine(self.db)
        with quiet():
            ctx = risk.update(7542.0, "switch to sleeve NAV")
        self.assertEqual(ctx["state"], "NORMAL")
        self.assertFalse(ctx["all_cash"])
        self.assertAlmostEqual(risk.peak, 7542.0)

    def test_risk_drawdown_is_sleeve_only_and_flow_adjusted(self):
        risk = m.RiskEngine(self.db)
        with quiet():
            risk.update(1000.0, "opening holdings")
            ctx = risk.update(2000.0, "SIP buy", flow=1000.0)
            self.assertAlmostEqual(ctx["drawdown"], 0.0)
            ctx = risk.update(1800.0, "price decline")
            self.assertAlmostEqual(ctx["drawdown"], 0.10)
            ctx = risk.update(1800.0, "cash withdrawal elsewhere")
            self.assertAlmostEqual(ctx["drawdown"], 0.10)
            self.assertFalse(ctx["all_cash"])


class TestLiveOrderSafety(SipTestCase):
    def test_account_and_order_read_http_contracts_use_one_v2_prefix_and_version_header(self):
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

    def test_place_order_v3_http_contract_regular_and_amo(self):
        client = m.UpstoxClient({"UPSTOX_ACCESS_TOKEN": "offline-test"})
        for is_amo in (False, True):
            with self.subTest(is_amo=is_amo):
                payload = json.dumps({"status": "success", "data": {"order_ids": ["V3O1"]}}).encode("utf-8")
                with mock.patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(payload)) as op:
                    resp = client.place_order("A", "NSE_EQ|A", "BUY", 20, 100.2, "LIMIT", is_amo=is_amo)
                self.assertEqual(resp["data"]["order_ids"], ["V3O1"])
                self.assertEqual(op.call_count, 1)
                request = op.call_args.args[0]
                self.assertEqual(request.full_url, "https://api.upstox.com/v3/order/place")
                self.assertEqual(request.get_method(), "POST")
                headers = {key.lower(): value for key, value in request.headers.items()}
                self.assertNotIn("api-version", headers)
                self.assertEqual(headers["authorization"], "Bearer offline-test")
                body = json.loads(request.data)
                self.assertEqual(body["instrument_token"], "NSE_EQ|A")
                self.assertEqual(body["transaction_type"], "BUY")
                self.assertEqual(body["quantity"], 20)
                self.assertEqual(body["product"], "D")
                self.assertEqual(body["validity"], "DAY")
                self.assertEqual(body["order_type"], "LIMIT")
                self.assertEqual(body["price"], 100.2)
                self.assertEqual(body["is_amo"], is_amo)
                self.assertFalse(body["slice"])

    def test_order_submission_is_not_retried_on_network_failure(self):
        client = m.UpstoxClient({"UPSTOX_ACCESS_TOKEN": "offline-test"})
        with mock.patch.object(m.urllib.request, "urlopen", side_effect=TimeoutError("lost response")) as op:
            with self.assertRaises(m.UpstoxError):
                client.place_order("A", "NSE_EQ|A", "BUY", 20, 100.2, "LIMIT")
        self.assertEqual(op.call_count, 1)
        request = op.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.upstox.com/v3/order/place")
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertNotIn("api-version", headers)

    def test_closed_market_sends_v3_amo_and_open_market_sends_regular_order(self):
        client = FakeUpstox()
        b = self.broker(client)
        with mock.patch.object(m, "market_open_now", return_value=False), \
             mock.patch.object(m, "amo_window_open_now", return_value=True), quiet():
            res = b.execute("A", "BUY", 20, 100.0)
        self.assertEqual(res["status"], "PENDING")
        self.assertTrue(client.placed[0]["amo"])
        self.assertTrue(res["is_amo"])
        with mock.patch.object(m, "market_open_now", return_value=True), quiet():
            res = b.execute("B", "BUY", 20, 100.0)
        self.assertEqual(res["status"], "PENDING")
        self.assertFalse(client.placed[1]["amo"])

    def test_no_amo_during_market_hours_and_no_order_outside_api_window(self):
        client = FakeUpstox()
        b = self.broker(client)
        with mock.patch.object(m, "market_open_now", return_value=True), quiet():
            res = b.execute("A", "BUY", 20, 100.0)
        self.assertEqual(res["status"], "PENDING")
        self.assertFalse(client.placed[0]["amo"])  # never AMO while market open
        with mock.patch.object(m, "market_open_now", return_value=False), \
             mock.patch.object(m, "amo_window_open_now", return_value=False), quiet():
            res = b.execute("B", "BUY", 20, 100.0)
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("place-order API window closed", res["reason"])
        self.assertEqual(len(client.placed), 1)
        self.assertAlmostEqual(b.sip_committed(), 2004.0)

    def test_explicit_broker_rejection_journals_zero_filled_value_and_releases_budget(self):
        client = FakeUpstox()
        client.submit_error = m.UpstoxError('Upstox HTTP 400: {"errors":[{"errorCode":"UDAPI1162"}]}')
        b = self.broker(client)
        with mock.patch.object(m, "market_open_now", return_value=True), quiet():
            res = b.execute("A", "BUY", 20, 100.0, reason="explicit reject")
        self.assertEqual(res["status"], "FAILED")
        row = self.db.one("SELECT status,value,filled_qty,reserved_value FROM orders ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["status"], "FAILED")
        self.assertAlmostEqual(row["value"], 0.0)
        self.assertAlmostEqual(row["filled_qty"], 0.0)
        self.assertAlmostEqual(row["reserved_value"], 0.0)
        self.assertAlmostEqual(b.sip_committed(), 0.0)

    def test_deploy_defers_when_place_order_api_window_closed(self):
        client = FakeUpstox(cash=100000.0)
        b = self.broker(client)
        c = self.cycle(b)
        self.seed_price("A", 100.0)
        self.db.meta_set(m.SIP_BASKET_KEY, "A")
        self.db.meta_set(m.SIP_BASKET_DATE_KEY, m.today_str())
        self.db.meta_set(m.SIP_TARGETS_KEY, json.dumps({"A": 1.0}))
        c._live_quotes = lambda symbols: ({}, {}, {})
        out = io.StringIO()
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0), \
             mock.patch.object(m, "market_open_now", return_value=False), \
             mock.patch.object(m, "amo_window_open_now", return_value=False), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(c.deploy_day(), 0)
        self.assertEqual(client.placed, [])
        self.assertIn("place-order API window closed", out.getvalue())
        self.assertTrue(self.db.meta_get(m.SIP_DEFERRED_KEY, ""))
        self.assertAlmostEqual(b.sip_committed(), 0.0)

    def test_deploy_submits_amo_when_market_closed_inside_api_window(self):
        client = FakeUpstox(cash=100000.0)
        b = self.broker(client)
        c = self.cycle(b)
        self.seed_price("A", 100.0)
        self.db.meta_set(m.SIP_BASKET_KEY, "A")
        self.db.meta_set(m.SIP_BASKET_DATE_KEY, m.today_str())
        self.db.meta_set(m.SIP_TARGETS_KEY, json.dumps({"A": 1.0}))
        c._live_quotes = lambda symbols: ({}, {}, {})
        c.filters.check_execution = lambda *a, **k: (True, "", {})
        c.filters.inav_premium_block = lambda *a, **k: (False, None, None)
        with mock.patch.object(m, "INVEST_PER_DAY", 1000.0), \
             mock.patch.object(m, "market_open_now", return_value=False), \
             mock.patch.object(m, "amo_window_open_now", return_value=True), quiet():
            submitted = c.deploy_day()
        self.assertEqual(submitted, 1)
        self.assertEqual(len(client.placed), 1)
        self.assertTrue(client.placed[0]["amo"])
        self.assertEqual(client.placed[0]["type"], "LIMIT")
        self.assertAlmostEqual(b.sip_committed(), 901.8)

    def test_exit_guard_submits_amo_sell_when_market_closed_inside_api_window(self):
        client = FakeUpstox(holdings=[{"trading_symbol": "A", "quantity": 30,
                                       "average_price": 100.0, "last_price": 100.0}])
        b = self.broker(client)
        c = self.cycle(b)
        c.data = types.SimpleNamespace(series=lambda *a, **k: [200.0] * 260 + [100.0] * 40)
        self.seed_price("A", 100.0)
        with mock.patch.object(m, "market_open_now", return_value=False), \
             mock.patch.object(m, "amo_window_open_now", return_value=True), quiet():
            self.assertEqual(c.daily_exit_guard(), 1)
        self.assertEqual(len(client.placed), 1)
        self.assertEqual(client.placed[0]["side"], "SELL")
        self.assertTrue(client.placed[0]["amo"])
        self.assertIsNone(self.db.one("SELECT * FROM pending_trades WHERE symbol='A' AND status='PENDING'"))

    def test_exit_guard_queues_pending_sell_when_api_window_closed(self):
        client = FakeUpstox(holdings=[{"trading_symbol": "A", "quantity": 30,
                                       "average_price": 100.0, "last_price": 100.0}])
        b = self.broker(client)
        c = self.cycle(b)
        c.data = types.SimpleNamespace(series=lambda *a, **k: [200.0] * 260 + [100.0] * 40)
        self.seed_price("A", 100.0)
        with mock.patch.object(m, "market_open_now", return_value=False), \
             mock.patch.object(m, "amo_window_open_now", return_value=False), quiet():
            self.assertEqual(c.daily_exit_guard(), 0)
        self.assertEqual(client.placed, [])
        row = self.db.one("SELECT symbol,side,target_qty,status FROM pending_trades WHERE symbol='A'")
        self.assertIsNotNone(row)
        self.assertEqual(row["side"], "SELL")
        self.assertAlmostEqual(row["target_qty"], 30.0)

    def test_migration_zeroes_old_failed_zero_fill_value(self):
        self.db.execute(
            "INSERT INTO orders(ts,exec_date,symbol,side,qty,price,value,charges,mode,status,filled_qty,reserved_value) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.now_iso(), m.today_str(), "A", "BUY", 34, 28.74, 977.16, 1.0, "LIVE", "FAILED", 0, 0))
        path = self.db.path
        self.db.close()
        db2 = m.Db(path)
        row = db2.one("SELECT value,charges FROM orders WHERE symbol='A' AND status='FAILED'")
        self.assertAlmostEqual(row["value"], 0.0)
        self.assertAlmostEqual(row["charges"], 0.0)
        db2.close()
        self.db = m.Db(path)  # tearDown closes a live handle

    def test_next_market_open_skips_weekend_and_holidays(self):
        # 2026-11-08 is a Sunday and in the repo holiday file; next session is Mon 09:15.
        now = m.dt.datetime(2026, 11, 8, 12, 0)
        nxt = m.next_market_open(now)
        self.assertEqual(nxt, m.dt.datetime(2026, 11, 9, 9, 15))
        friday_after = m.dt.datetime(2026, 11, 6, 16, 0)
        self.assertEqual(m.next_market_open(friday_after), m.dt.datetime(2026, 11, 9, 9, 15))
        monday_pre = m.dt.datetime(2026, 11, 9, 8, 0)
        self.assertEqual(m.next_market_open(monday_pre), m.dt.datetime(2026, 11, 9, 9, 15))


if __name__ == "__main__":
    unittest.main()
