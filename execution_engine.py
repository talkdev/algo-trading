"""
════════════════════════════════════════════════════════════════════════════
NIFTY INTRADAY OPTIONS ALGO TRADING ENGINE — 2026 PRODUCTION BUILD
FILE 4 of 5 : EXECUTION ENGINE
════════════════════════════════════════════════════════════════════════════

Save as: execution_engine.py  (same directory as the other files)

Implements:
    Module 9  — Pre-Trade Validation (final gate before order placement):
                capital/daily-loss checks, portfolio delta check, liquidity
                re-validation, price-drift check, time-window final check
    Module 10 — Position Monitoring (every cycle, per open position):
                staleness-aware mark price, time-based stop tightening,
                premium stop, price-based stop, profit target, profit-lock
                trailing stop, VWAP breach (full or partial close), ADX
                breach, portfolio delta breach, hard time exit, short-leg
                delta breach
    Module 12 — Transaction cost computation (entry AND exit, using ACTUAL
                fill prices, for accurate net P&L)

SAFETY DESIGN:
    - PaperOrderExecutor is used whenever config.paper_trade_mode is True
      (the default). LiveOrderExecutor refuses to even construct if
      paper_trade_mode is True (defense in depth, matching File 1's guard
      inside UpstoxClient.place_order()).
    - Partial-fill / leg-execution risk (identified during earlier review)
      is handled directly: if some legs of a multi-leg entry fill and
      others fail, the engine immediately attempts an emergency unwind of
      the filled legs rather than silently holding an accidentally-naked
      position.

Every entry, exit, and NO_GO decision is printed to console, logged (and
therefore mirrored to the audit_log table + daily audit file from File 1),
and persisted to positions / position_legs / trade_entries / trade_exits.
"""

from __future__ import annotations

import json
import time as time_module
import uuid
from datetime import datetime, time as dtime
from typing import Optional

from nifty_algo_core import (
    Config, Database, RateLimiter, UpstoxClient,
    now_ist, today_ist, parse_ist_timestamp,
    print_section, print_kv_table,
    load_config, setup_logging,
)
from market_data_engine import MarketDataEngine, ensure_column

POSITIONS_EXTRA_COLUMNS = [
    ("stop_tightened_for_delta", "INTEGER DEFAULT 0"),
]


# ─────────────────────────────────────────────────────────────────────────
# ORDER EXECUTORS
# ─────────────────────────────────────────────────────────────────────────
class PaperOrderExecutor:
    """
    Simulates fills using the same executable-price convention Module 8
    already used for economics (bid for sell, ask for buy). `chain` is
    accepted for interface parity with LiveOrderExecutor but unused here.
    """

    def __init__(self, config: Config, logger):
        self.config = config
        self.logger = logger
        self._counter = 0

    def _next_order_id(self) -> str:
        self._counter += 1
        return f"PAPER-{today_ist().isoformat()}-{self._counter:04d}"

    def execute_leg_entry(self, leg: dict, lots: int, chain: dict) -> dict:
        order_id = self._next_order_id()
        exec_price = leg["exec_price"]
        self.logger.info(f"[PAPER] ENTRY FILL: {leg['action']} {leg['option_type'].upper()} "
                          f"{leg['strike']:.0f} x{lots} @ {exec_price:.2f} (order_id={order_id})")
        return {"order_id": order_id, "fill_price": exec_price, "status": "FILLED"}

    def execute_leg_exit(self, leg: dict, chain: dict, lots: int) -> dict:
        opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
        bid, ask, ltp = opt.get("bid", 0), opt.get("ask", 0), opt.get("ltp", 0)
        # To CLOSE: reverse of original action. Original SELL -> buy back (pay ask).
        # Original BUY -> sell out (receive bid). Both conservative for paper P&L.
        if leg["action"] == "SELL":
            fill_price = ask if ask > 0 else (ltp if ltp > 0 else leg.get("entry_price", 0) or 0)
        else:
            fill_price = bid if bid > 0 else (ltp if ltp > 0 else leg.get("entry_price", 0) or 0)
        order_id = self._next_order_id()
        self.logger.info(f"[PAPER] EXIT FILL: close {leg['action']} {leg['option_type'].upper()} "
                          f"{leg['strike']:.0f} x{lots} @ {fill_price:.2f} (order_id={order_id})")
        return {"order_id": order_id, "fill_price": fill_price, "status": "FILLED"}


class LiveOrderExecutor:
    """
    Routes real orders through Upstox. Refuses to construct while
    paper_trade_mode is True — defense in depth on top of the guard already
    inside UpstoxClient.place_order()/cancel_order().

    VERIFY: order response field names (order_id, average_price) against
    the current Upstox API — used defensively with fallbacks below.
    """

    def __init__(self, config: Config, client: UpstoxClient, logger):
        if config.paper_trade_mode:
            raise RuntimeError("LiveOrderExecutor instantiated while PAPER_TRADE_MODE=True. Refusing.")
        self.config = config
        self.client = client
        self.logger = logger

    def _resolve_instrument_key(self, leg: dict, chain: dict) -> Optional[str]:
        if leg.get("instrument_key"):
            return leg["instrument_key"]
        opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
        return opt.get("instrument_key")

    def _get_fill_price(self, order_id: str, fallback: float, retries: int = 3) -> float:
        for _ in range(retries):
            try:
                details = self.client.get_order_details(order_id)
                price = details.get("average_price") or details.get("price")
                if price:
                    return float(price)
            except Exception as e:
                self.logger.warning(f"Could not fetch order details for {order_id}: {e}")
            time_module.sleep(1)
        self.logger.warning(f"Using fallback price for order {order_id} (could not confirm fill price)")
        return fallback

    def _aggressive_limit_price(self, chain: dict, strike: float, opt_type: str, transaction_type: str, fallback: float) -> float:
        opt = chain.get(strike, {}).get(opt_type, {}) if chain else {}
        bid, ask = opt.get("bid", 0) or 0, opt.get("ask", 0) or 0
        tick = 0.05
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
        else:
            mid = fallback
        if transaction_type == "BUY":
            price = mid + tick
        else:
            price = mid - tick
            price = max(price, tick)
        return round(round(price / tick) * tick, 2)

    def execute_leg_entry(self, leg: dict, lots: int, chain: dict) -> dict:
        instrument_key = self._resolve_instrument_key(leg, chain)
        if not instrument_key:
            raise RuntimeError(f"No instrument_key resolvable for leg {leg['strike']} {leg['option_type']} "
                                f"— cannot place live order")
        qty = lots * self.config.lot_size
        transaction_type = "SELL" if leg["action"] == "SELL" else "BUY"
        limit_price = self._aggressive_limit_price(chain, leg["strike"], leg["option_type"], transaction_type, leg["exec_price"])
        result = self.client.place_order(
            instrument_token=instrument_key, quantity=qty, transaction_type=transaction_type,
            order_type="LIMIT", price=limit_price, product="I",
        )
        order_id = result.get("order_id", "")
        self.logger.info(f"[LIVE] ENTRY ORDER PLACED: {transaction_type} {leg['option_type'].upper()} "
                          f"{leg['strike']:.0f} x{lots} order_id={order_id}")
        fill_price = self._get_fill_price(order_id, fallback=leg["exec_price"])
        return {"order_id": order_id, "fill_price": fill_price, "status": "FILLED"}

    def execute_leg_exit(self, leg: dict, chain: dict, lots: int) -> dict:
        instrument_key = self._resolve_instrument_key(leg, chain)
        if not instrument_key:
            raise RuntimeError(f"No instrument_key resolvable for leg {leg['strike']} {leg['option_type']} "
                                f"— cannot place live order")
        qty = lots * self.config.lot_size
        transaction_type = "BUY" if leg["action"] == "SELL" else "SELL"
        limit_price = self._aggressive_limit_price(chain, leg["strike"], leg["option_type"], transaction_type, leg.get("entry_price", 0) or 0)
        result = self.client.place_order(
            instrument_token=instrument_key, quantity=qty, transaction_type=transaction_type,
            order_type="LIMIT", price=limit_price, product="I",
        )
        order_id = result.get("order_id", "")
        self.logger.info(f"[LIVE] EXIT ORDER PLACED: {transaction_type} {leg['option_type'].upper()} "
                          f"{leg['strike']:.0f} x{lots} order_id={order_id}")
        fill_price = self._get_fill_price(order_id, fallback=leg.get("entry_price", 0) or 0)
        return {"order_id": order_id, "fill_price": fill_price, "status": "FILLED"}


# ─────────────────────────────────────────────────────────────────────────
# EXECUTION ENGINE
# ─────────────────────────────────────────────────────────────────────────
class ExecutionEngine:
    def __init__(self, config: Config, db: Database, market_engine: MarketDataEngine,
                 client: UpstoxClient, logger):
        self.config = config
        self.db = db
        self.market_engine = market_engine
        self.logger = logger

        for col, coltype in POSITIONS_EXTRA_COLUMNS:
            ensure_column(self.db, "positions", col, coltype)

        if config.paper_trade_mode:
            self.executor = PaperOrderExecutor(config, logger)
            logger.info("ExecutionEngine initialized in PAPER TRADE mode.")
        else:
            self.executor = LiveOrderExecutor(config, client, logger)
            logger.warning("ExecutionEngine initialized in LIVE TRADING mode — REAL ORDERS WILL BE PLACED.")

    # ─────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────
    def _time_diff_minutes(self, t1: dtime, t2: dtime) -> float:
        dt1 = datetime.combine(today_ist(), t1)
        dt2 = datetime.combine(today_ist(), t2)
        return (dt2 - dt1).total_seconds() / 60.0

    def _get_open_positions(self) -> list:
        return self.db.query(
            "SELECT * FROM positions WHERE trading_date=? AND status='OPEN'",
            (today_ist().isoformat(),),
        )

    def _get_position_legs(self, position_id: str) -> list:
        return self.db.query("SELECT * FROM position_legs WHERE position_id=?", (position_id,))

    def _compute_portfolio_delta(self) -> float:
        chain = self.market_engine.last_chain
        total_delta = 0.0
        C02 = self.config.lot_size
        for pos in self._get_open_positions():
            for leg in self._get_position_legs(pos["position_id"]):
                if leg["leg_status"] != "OPEN":
                    continue
                opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
                if opt:
                    live_delta = opt.get("delta", leg["entry_delta"])
                else:
                    _ed = leg["entry_delta"] or 0
                    live_delta = _ed * 1.5 if abs(_ed) < 0.5 else _ed
                lots = leg["qty"] // C02 if C02 > 0 else 1
                sign = -1 if leg["action"] == "SELL" else 1
                total_delta += sign * live_delta * lots
        return total_delta

    def _compute_portfolio_vega_gamma(self) -> tuple[float, float]:
        chain = self.market_engine.last_chain
        total_vega = 0.0
        total_gamma = 0.0
        C02 = self.config.lot_size
        for pos in self._get_open_positions():
            for leg in self._get_position_legs(pos["position_id"]):
                if leg["leg_status"] != "OPEN":
                    continue
                opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
                _entry_vega = leg["entry_vega"] or 0
                _entry_gamma = leg["entry_gamma"] or 0
                if opt:
                    live_vega = opt.get("vega") or _entry_vega
                    live_gamma = opt.get("gamma") or _entry_gamma
                else:
                    live_vega = _entry_vega * 0.5
                    live_gamma = _entry_gamma * 2.0
                lots = leg["qty"] // C02 if C02 > 0 else 1
                sign = -1 if leg["action"] == "SELL" else 1
                total_vega += sign * live_vega * lots
                total_gamma += sign * live_gamma * lots
        return total_vega, total_gamma

    def _compute_transaction_costs(self, legs: list, lots: int, action: str) -> dict:
        total_sell_premium, total_buy_premium = 0.0, 0.0
        num_orders = len(legs)
        C02 = self.config.lot_size

        for leg in legs:
            if action == "ENTRY":
                price = leg.get("entry_price", 0) or 0
            else:
                price = leg.get("exit_price", 0) or 0
            if price <= 0:
                price = leg.get("entry_price", 0) or 0
            qty = lots * C02
            if price <= 0:
                continue
            premium_value = price * qty
            if leg["action"] == "SELL":
                total_sell_premium += premium_value
            else:
                total_buy_premium += premium_value

        total_turnover = total_sell_premium + total_buy_premium
        if total_turnover <= 0:
            return {"total_rupees": 0.0, "breakdown": {}}

        stt = total_sell_premium * self.config.stt_options_sell
        exchange = total_turnover * self.config.exchange_txn_rate
        sebi = total_turnover * self.config.sebi_rate
        stamp = total_buy_premium * self.config.stamp_duty_buy_options
        brokerage = self.config.brokerage_per_order * num_orders
        gst = (brokerage + exchange + sebi) * 0.18
        total_costs = stt + exchange + sebi + stamp + brokerage + gst

        self.logger.debug(f"Transaction costs ({action}): STT={stt:.2f} Brokerage={brokerage:.2f} "
                           f"GST={gst:.2f} Total={total_costs:.2f}")
        return {"total_rupees": round(total_costs, 2), "breakdown": {
            "stt": round(stt, 2), "exchange": round(exchange, 2),
            "sebi": round(sebi, 4), "stamp": round(stamp, 4), "brokerage": round(brokerage, 2),
            "gst": round(gst, 2), "total": round(total_costs, 2),
        }}

    # ─────────────────────────────────────────────────────────────────
    # MODULE 9: PRE-TRADE VALIDATION
    # ─────────────────────────────────────────────────────────────────
    def validate_pre_trade(self, params: dict, signals: dict) -> tuple[str, dict]:
        state = self.market_engine.state
        current_cap = state.get("current_capital", self.config.starting_capital)
        if current_cap <= 0:
            return "NO_GO", {"reason": "current_capital_zero_or_negative"}

        daily_pnl = state.get("daily_pnl", 0.0)
        daily_loss = max(0.0, -daily_pnl)
        daily_loss_pct = daily_loss / current_cap

        if daily_loss_pct >= self.config.max_daily_loss_pct:
            return "NO_GO", {"reason": f"daily_loss_{daily_loss_pct*100:.2f}pct_exceeds_limit"}

        size_adj = 1.0
        if daily_loss_pct >= self.config.max_daily_loss_pct * 0.80:
            size_adj = 0.50
            self.logger.info(f"Soft limit: daily loss {daily_loss_pct*100:.1f}%, reducing size 50%")

        final_lots = params["final_lots"]
        trade_max_loss = params["total_max_risk"]
        projected_pct = (daily_loss + trade_max_loss) / current_cap
        max_projected_pct = self.config.max_daily_loss_pct * 1.25

        if projected_pct > max_projected_pct:
            max_additional_loss = (current_cap * max_projected_pct) - daily_loss
            if max_additional_loss <= 0:
                return "NO_GO", {"reason": "projected_daily_loss_would_exceed_limit"}
            max_lots_by_daily = max(1, int(max_additional_loss / params["max_loss_per_lot"]))
            if max_lots_by_daily < final_lots:
                self.logger.info(f"Lots reduced {final_lots} -> {max_lots_by_daily} for daily loss limit")
                final_lots = max_lots_by_daily

        final_lots = max(1, int(final_lots * size_adj))

        # Portfolio delta check
        C02 = self.config.lot_size
        new_delta = sum(
            (-1 if leg["action"] == "SELL" else 1) * leg["delta"] * final_lots
            for leg in params["legs"]
        )
        current_portfolio_delta = self._compute_portfolio_delta()
        post_trade_delta = current_portfolio_delta + new_delta
        total_open_lots_for_delta = sum(
            p["final_lots"] for p in self._get_open_positions()
        ) + final_lots
        delta_limit = 0.20 * total_open_lots_for_delta

        if abs(post_trade_delta) > delta_limit:
            reduced_lots, found = final_lots - 1, False
            while reduced_lots >= 1:
                new_delta_r = sum(
                    (-1 if leg["action"] == "SELL" else 1) * leg["delta"] * reduced_lots
                    for leg in params["legs"]
                )
                _total_lots_reduced = sum(
                    p["final_lots"] for p in self._get_open_positions()
                ) + reduced_lots
                if abs(current_portfolio_delta + new_delta_r) <= (0.20 * _total_lots_reduced):
                    final_lots, found = reduced_lots, True
                    break
                reduced_lots -= 1
            if not found:
                return "NO_GO", {"reason": "portfolio_delta_exceeds_limit_cannot_reduce_further"}

        new_vega = sum((-1 if leg["action"] == "SELL" else 1) * (leg.get("vega") or 0) * final_lots for leg in params["legs"])
        new_gamma = sum((-1 if leg["action"] == "SELL" else 1) * (leg.get("gamma") or 0) * final_lots for leg in params["legs"])
        current_vega, current_gamma = self._compute_portfolio_vega_gamma()
        post_trade_vega = current_vega + new_vega
        post_trade_gamma = current_gamma + new_gamma
        vega_limit = 200.0 * final_lots
        gamma_limit = 0.50 * final_lots
        if abs(post_trade_vega) > vega_limit:
            return "NO_GO", {"reason": f"portfolio_vega_{post_trade_vega:.1f}_exceeds_limit_{vega_limit:.1f}"}
        if abs(post_trade_gamma) > gamma_limit:
            return "NO_GO", {"reason": f"portfolio_gamma_{post_trade_gamma:.4f}_exceeds_limit_{gamma_limit:.4f}"}

        chain = self.market_engine.last_chain
        if not chain:
            return "NO_GO", {"reason": "chain_unavailable_at_execution"}

        for leg in params["legs"]:
            strike, opt_type = leg["strike"], leg["option_type"]
            opt = chain.get(strike, {}).get(opt_type, {})
            if not opt:
                return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_not_in_chain_at_execution"}
            bid, ask, oi = opt.get("bid", 0), opt.get("ask", 0), opt.get("oi", 0)
            if bid <= 0 and ask <= 0:
                return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_no_bid_ask_at_execution"}
            if oi < 50:
                return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_oi_below_50_at_execution"}
            if bid > 0 and ask > 0 and (ask - bid) / ask > 0.50:
                return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_spread_too_wide_at_execution"}

            current_price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
            original_price = leg["exec_price"]
            if original_price > 0 and current_price > 0:
                drift = abs(current_price - original_price) / original_price
                if drift > 0.25:
                    return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_price_drifted_{drift*100:.0f}pct"}

        # Recompute credit with current prices; reject if it decayed too much
        if params["strategy_type"] == "SELL":
            current_gross = 0.0
            for leg in params["legs"]:
                opt = chain.get(leg["strike"], {}).get(leg["option_type"], {})
                bid, ask = opt.get("bid", 0), opt.get("ask", 0)
                if leg["action"] == "SELL":
                    current_gross += bid if bid > 0 else opt.get("ltp", 0)
                else:
                    current_gross -= ask if ask > 0 else opt.get("ltp", 0)

            current_net_credit = current_gross - params["total_slippage"] - params["total_costs_pts"]
            original_net_credit = params["entry_credit"]
            if original_net_credit and original_net_credit > 0:
                decay = (original_net_credit - current_net_credit) / original_net_credit
                if decay > 0.25:
                    return "NO_GO", {"reason": f"credit_decayed_{decay*100:.0f}pct_since_computed"}
                if current_net_credit < original_net_credit:
                    params["entry_credit"] = current_net_credit
                    params["stop_premium"] = current_net_credit * state.get("stop_multiplier", 2.0)
                    params["target_premium"] = current_net_credit * (1 - params["target_pct"])

        # Time window final check
        current_time = now_ist().time()
        try:
            entry_start = datetime.strptime(state["entry_start"], "%H:%M").time()
            entry_end = datetime.strptime(state["entry_end"], "%H:%M").time()
        except Exception:
            entry_start, entry_end = self.config.trading_window_start, self.config.trading_window_last_entry

        if current_time > entry_end:
            return "NO_GO", {"reason": f"past_entry_window_{entry_end}"}
        if current_time < entry_start:
            return "NO_GO", {"reason": f"before_entry_window_{entry_start}"}

        hard_exit_str = state.get("hard_exit_time", self.config.hard_exit_time.strftime("%H:%M"))
        try:
            hard_exit = datetime.strptime(hard_exit_str, "%H:%M").time()
        except Exception:
            hard_exit = self.config.hard_exit_time
        _actual_dte_buffer = self.market_engine.state.get("actual_dte")
        _min_buffer = 75 if _actual_dte_buffer == 0 else 30
        if self._time_diff_minutes(current_time, hard_exit) < _min_buffer:
            return "NO_GO", {"reason": f"only_{self._time_diff_minutes(current_time, hard_exit):.0f}min_before_hard_exit_need_{_min_buffer}"}

        _actual_dte_gate = self.market_engine.state.get("actual_dte")
        if _actual_dte_gate == 0:
            if current_time > dtime(13, 30):
                return "NO_GO", {"reason": "0dte_past_13:30_entry_cutoff"}
            hard_exit_0dte_str = state.get("hard_exit_time", "15:00")
            try:
                hard_exit_0dte = datetime.strptime(hard_exit_0dte_str, "%H:%M").time()
            except Exception:
                hard_exit_0dte = dtime(15, 0)
            if self._time_diff_minutes(current_time, hard_exit_0dte) < 90:
                return "NO_GO", {"reason": "0dte_insufficient_time_before_hard_exit"}

        params["final_lots"] = final_lots
        params["total_max_risk"] = params["max_loss_per_lot"] * final_lots
        return "GO", params

    # ─────────────────────────────────────────────────────────────────
    # POSITION OPENING
    # ─────────────────────────────────────────────────────────────────
    def _emergency_unwind(self, filled_legs: list, lots: int, chain: dict) -> None:
        """
        Leg-execution-risk mitigation: if a multi-leg entry partially fills
        (some legs succeed, one fails), immediately close whatever filled so
        the book never carries an accidentally-naked, undefined-risk leg.
        """
        for leg in filled_legs:
            try:
                self.executor.execute_leg_exit(leg, chain, lots)
                self.logger.warning(f"Emergency unwind: closed {leg['action']} {leg['option_type']} "
                                     f"{leg['strike']:.0f}")
            except Exception as e:
                self.logger.critical(
                    f"EMERGENCY UNWIND FAILED for {leg['strike']} {leg['option_type']}: {e}. "
                    f"MANUAL INTERVENTION REQUIRED — verify actual broker positions immediately."
                )

    def execute_entry(self, params: dict, signals: dict) -> Optional[str]:
        position_id = str(uuid.uuid4())
        lots = params["final_lots"]
        chain = self.market_engine.last_chain
        filled_legs = []

        try:
            buy_legs = [l for l in params["legs"] if l["action"] == "BUY"]
            sell_legs = [l for l in params["legs"] if l["action"] == "SELL"]
            entry_order = buy_legs + sell_legs
            for leg in entry_order:
                fill = self.executor.execute_leg_entry(leg, lots, chain)
                filled_legs.append({**leg, "fill": fill})
        except Exception as e:
            self.logger.error(f"Entry execution failed: {e}")
            if filled_legs:
                self.logger.critical(f"PARTIAL FILL on entry — {len(filled_legs)}/{len(params['legs'])} "
                                      f"legs filled before failure. Attempting emergency unwind.")
                self._emergency_unwind(filled_legs, lots, chain)
            return None

        now = now_ist()
        actual_fill_legs_for_cost = [{**fl, "entry_price": fl["fill"]["fill_price"]} for fl in filled_legs]
        recomputed_entry_costs = self._compute_transaction_costs(actual_fill_legs_for_cost, lots, "ENTRY")
        actual_entry_costs_rupees = recomputed_entry_costs["total_rupees"]
        self.db.insert("positions", {
            "position_id": position_id, "trading_date": today_ist().isoformat(),
            "strategy_name": params["strategy_name"], "strategy_type": params["strategy_type"],
            "selection_reason": params["selection_reason"], "target_expiry": params["target_expiry"],
            "actual_dte": params["actual_dte"], "entry_time": now.isoformat(),
            "entry_spot": params["entry_spot"], "entry_vix": params["entry_vix"],
            "entry_vrp": params["entry_vrp"],
            "entry_credit": params.get("entry_credit"), "entry_debit": params.get("entry_debit"),
            "gross_credit": params.get("gross_credit"), "total_slippage": params["total_slippage"],
            "entry_costs_rupees": actual_entry_costs_rupees,
            "stop_premium": params.get("stop_premium"), "target_premium": params.get("target_premium"),
            "stop_value": params.get("stop_value"), "target_value": params.get("target_value"),
            "price_stop_pts": params["price_stop_pts"], "hard_exit_time": params["hard_exit_time"],
            "final_lots": lots, "max_loss_per_lot": params["max_loss_per_lot"],
            "total_max_risk": params["total_max_risk"], "estimated_margin": params["estimated_margin"],
            "status": "OPEN", "last_known_premium": params["last_known_premium"],
            "stop_at_breakeven": 0, "stop_moved_to_25pct": 0, "stop_tightened_for_delta": 0,
            "paper_trade": 1 if self.config.paper_trade_mode else 0,
            "raw_params_json": json.dumps(params, default=str),
            "created_at": now.isoformat(), "updated_at": now.isoformat(),
        })

        for leg in filled_legs:
            self.db.insert("position_legs", {
                "position_id": position_id, "strike": leg["strike"], "option_type": leg["option_type"],
                "action": leg["action"], "qty": lots * self.config.lot_size,
                "entry_price": leg["fill"]["fill_price"], "exit_price": None,
                "entry_bid": leg["bid"], "entry_ask": leg["ask"], "entry_delta": leg["delta"],
                "entry_gamma": leg["gamma"], "entry_vega": leg["vega"], "entry_theta": leg["theta"],
                "entry_iv": leg["iv"], "entry_oi": leg["oi"], "exit_delta": None,
                "broker_order_id_entry": leg["fill"]["order_id"], "broker_order_id_exit": None,
                "leg_status": "OPEN",
            })

        self._persist_trade_entry(position_id, params, signals)

        state = self.market_engine.state
        state["entry_count"] = state.get("entry_count", 0) + 1
        state["consecutive_stops"] = 0
        state["last_entry_time"] = now_ist().isoformat()
        self.market_engine._save_session_state()

        print_section(f"POSITION OPENED: {params['strategy_name']}")
        print_kv_table({
            "Position ID": position_id, "Lots": lots,
            "Entry Credit/Debit (pts)": params.get("entry_credit") or params.get("entry_debit"),
            "Stop (pts)": params.get("stop_premium") or params.get("stop_value"),
            "Target (pts)": params.get("target_premium") or params.get("target_value"),
            "Max Risk (Rs)": params["total_max_risk"], "Paper Trade": self.config.paper_trade_mode,
        })
        self.logger.info(f"ENTRY EXECUTED: {params['strategy_name']} position_id={position_id} "
                          f"lots={lots} paper_trade={self.config.paper_trade_mode}")
        return position_id

    def _persist_trade_entry(self, position_id: str, params: dict, s: dict) -> None:
        self.db.insert("trade_entries", {
            "trade_id": position_id, "position_id": position_id, "strategy_name": params["strategy_name"],
            "entry_time": now_ist().isoformat(), "trading_date": s["trading_date"],
            "day_label": self.market_engine.state.get("day_label"),
            "entry_spot": params["entry_spot"], "entry_vix": params["entry_vix"],
            "entry_vrp": params["entry_vrp"], "entry_atm_iv": s["atm_iv"],
            "entry_parkinson_rv": s["parkinson_rv"], "entry_adx": s["adx"],
            "entry_vwap": s["vwap"], "entry_vwap_dist": s["vwap_dist_pct"], "entry_pcr": s["pcr"],
            "entry_pcr_change": s.get("pcr_change"), "entry_skew_ratio": s["skew_ratio"],
            "or_width": s["or_width"], "or_condition": s["or_condition"],
            "volatility_condition": s["volatility_condition"], "iv_behavior": s["iv_behavior"],
            "trend_condition": s["trend_condition"], "adx_condition": s["adx_condition"],
            "direction": s["direction"], "vwap_signal": s["vwap_signal"], "pcr_signal": s["pcr_signal"],
            "skew_signal": s["skew_signal"], "preferred_sell_side": s["preferred_sell_side"],
            "target_expiry": params["target_expiry"], "actual_dte": params["actual_dte"],
            "legs_json": json.dumps(params["legs"], default=str),
            "entry_credit": params.get("entry_credit"), "entry_debit": params.get("entry_debit"),
            "gross_credit": params.get("gross_credit"), "total_slippage": params["total_slippage"],
            "entry_costs_pts": params["total_costs_pts"],
            "entry_costs_rupees": params["total_costs_rupees_per_lot"] * params["final_lots"] + params.get("total_fixed_costs_rupees", 0),
            "stop_premium": params.get("stop_premium"), "target_premium": params.get("target_premium"),
            "price_stop_pts": params["price_stop_pts"], "hard_exit_time": params["hard_exit_time"],
            "final_lots": params["final_lots"], "max_loss_per_lot": params["max_loss_per_lot"],
            "total_max_risk": params["total_max_risk"],
            "capital_at_entry": self.market_engine.state.get("current_capital"),
            "daily_pnl_at_entry": self.market_engine.state.get("daily_pnl"),
            "paper_trade": 1 if self.config.paper_trade_mode else 0,
            "selection_reason": params["selection_reason"], "created_at": now_ist().isoformat(),
        })

    def process_entry_decision(self, decision: dict, signals: dict) -> Optional[str]:
        """Called by File 5 with the output of StrategyEngine.decide() when
        action == 'ENTER'. Runs final pre-trade validation and executes if GO."""
        if decision.get("action") != "ENTER":
            return None

        go, result = self.validate_pre_trade(decision["params"], signals)

        if go != "GO":
            reason = result.get("reason", "unknown")
            print_section("PRE-TRADE VALIDATION: NO_GO")
            print(f"  Reason: {reason}")
            self.logger.warning(f"PRE_TRADE_NO_GO: {reason}")
            return None

        print_section("PRE-TRADE VALIDATION: GO")
        return self.execute_entry(result, signals)

    # ─────────────────────────────────────────────────────────────────
    # MODULE 10: POSITION MONITORING
    # ─────────────────────────────────────────────────────────────────
    def _get_mark_price_with_staleness(self, opt: dict, fallback: float) -> float:
        if not opt:
            return fallback
        bid, ask, ltp = opt.get("bid", 0), opt.get("ask", 0), opt.get("ltp", 0)
        ts_str = opt.get("timestamp")
        age_seconds = float("inf")
        if ts_str:
            ts = parse_ist_timestamp(ts_str)
            if ts:
                age_seconds = (now_ist() - ts).total_seconds()
        if bid > 0 and ask > 0 and age_seconds < 30:
            return (bid + ask) / 2.0
        if ltp > 0 and age_seconds < 60:
            return ltp
        if bid > 0 and ask > 0 and age_seconds < 120:
            return (bid + ask) / 2.0
        if ltp > 0 and age_seconds < 300:
            return ltp
        return fallback

    def _compute_current_premium(self, legs: list, chain: dict) -> float:
        current_premium = 0.0
        for leg in legs:
            if leg["leg_status"] != "OPEN":
                continue
            opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
            mark = self._get_mark_price_with_staleness(opt, leg["entry_price"] or 0)
            current_premium += mark if leg["action"] == "SELL" else -mark
        return current_premium

    def monitor_position(self, position: dict, signals: dict) -> tuple[str, dict]:
        legs = self._get_position_legs(position["position_id"])
        chain_expiry = self.market_engine.last_chain_expiry
        _chain_matches = chain_expiry and chain_expiry.isoformat() == position["target_expiry"]
        chain = self.market_engine.last_chain if _chain_matches else {}
        if not _chain_matches and position.get("last_known_premium") is not None:
            self.logger.debug(f"Chain expiry mismatch for {position['position_id']} — "
                              f"using last_known_premium as mark fallback")
        last_known_premium_fallback = not _chain_matches

        current_premium = self._compute_current_premium(legs, chain)
        self.db.update("positions", {"last_known_premium": current_premium, "updated_at": now_ist().isoformat()},
                        {"position_id": position["position_id"]})

        current_time = now_ist().time()
        strategy_type = position["strategy_type"]
        strategy_name = position["strategy_name"]

        raw_params = json.loads(position.get("raw_params_json") or "{}")
        tightening_schedule = raw_params.get("tightening_schedule", [])
        original_stop_premium_for_tightening = raw_params.get("stop_premium") or position.get("stop_premium")
        effective_stop = position.get("stop_premium")
        if effective_stop is not None and original_stop_premium_for_tightening:
            for tighten_time_str, factor in tightening_schedule:
                try:
                    tighten_time = datetime.strptime(tighten_time_str, "%H:%M").time()
                except Exception:
                    continue
                if current_time >= tighten_time:
                    effective_stop = original_stop_premium_for_tightening * factor
            if strategy_type == "SELL" and position.get("entry_time"):
                try:
                    entry_dt = datetime.fromisoformat(position["entry_time"])
                    hold_minutes = (now_ist() - entry_dt).total_seconds() / 60.0
                    if hold_minutes > 120:
                        time_factor = max(0.50, 1.0 - (hold_minutes - 120) / 180.0)
                        effective_stop = min(effective_stop, position["stop_premium"] * time_factor)
                except Exception:
                    pass

        if strategy_type == "SELL" and position.get("entry_credit") and position["entry_credit"] > 0:
            _is_directional = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
            _stop_multiplier = 2.5 if _is_directional else 2.0
            _credit_stop_limit = position["entry_credit"] * _stop_multiplier
            _actual_stop = min(
                effective_stop if effective_stop is not None else _credit_stop_limit,
                _credit_stop_limit
            )
            if current_premium >= _actual_stop:
                return "CLOSE_STOP", {"current_premium": current_premium, "effective_stop": _actual_stop}
        elif strategy_type == "SELL" and effective_stop is not None and current_premium >= effective_stop:
            return "CLOSE_STOP", {"current_premium": current_premium, "effective_stop": effective_stop}
        if strategy_type == "BUY" and position.get("stop_value") is not None \
                and current_premium <= position["stop_value"]:
            return "CLOSE_STOP", {"current_premium": current_premium}

        price_stop_pts = position.get("price_stop_pts") or 0
        spot = signals.get("spot")
        if price_stop_pts > 0 and spot is not None and position.get("entry_spot"):
            if abs(spot - position["entry_spot"]) >= price_stop_pts:
                return "CLOSE_STOP", {"reason_detail": f"price_stop_{abs(spot - position['entry_spot']):.0f}pts"}

        if strategy_type == "SELL" and position.get("entry_credit"):
            _entry_credit_td = position["entry_credit"]
            _now_time_td = now_ist().time()
            if _now_time_td >= dtime(13, 30):
                _time_target_pct = 0.25
            elif _now_time_td >= dtime(13, 0):
                _time_target_pct = 0.30
            elif _now_time_td >= dtime(12, 0):
                _time_target_pct = 0.32
            else:
                _time_target_pct = None
            if _time_target_pct is not None:
                _time_target_premium = _entry_credit_td * (1.0 - _time_target_pct)
                _effective_target = min(
                    position["target_premium"] if position.get("target_premium") is not None else _time_target_premium,
                    _time_target_premium
                )
                if current_premium <= _effective_target:
                    return "CLOSE_TARGET", {"current_premium": current_premium, "time_decay_target": True}
            elif position.get("target_premium") is not None and current_premium <= position["target_premium"]:
                return "CLOSE_TARGET", {"current_premium": current_premium}
        elif strategy_type == "SELL" and position.get("target_premium") is not None \
                and current_premium <= position["target_premium"]:
            return "CLOSE_TARGET", {"current_premium": current_premium}
        if strategy_type == "BUY" and position.get("target_value") is not None \
                and current_premium >= position["target_value"]:
            return "CLOSE_TARGET", {"current_premium": current_premium}

        _is_directional_for_lock = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
        if strategy_type == "SELL" and position.get("entry_credit"):
            entry_credit = position["entry_credit"]
            gross_credit_for_profit_pct = position.get("gross_credit") or entry_credit
            profit_pct = (gross_credit_for_profit_pct - current_premium) / gross_credit_for_profit_pct
            C02_lock = self.config.lot_size
            lots_lock = position.get("final_lots", 1)
            entry_costs_pts = (position.get("entry_costs_rupees") or 0.0) / max(C02_lock * lots_lock, 1)
            true_breakeven_premium = entry_credit - entry_costs_pts
            _lock_threshold = 0.25 if _is_directional_for_lock else 0.20
            _move_threshold = 0.55 if _is_directional_for_lock else 0.50
            if profit_pct >= _lock_threshold and not position.get("stop_at_breakeven"):
                lock_stop = max(true_breakeven_premium, current_premium * 1.05)
                self.db.update("positions", {"stop_premium": lock_stop, "stop_at_breakeven": 1},
                                {"position_id": position["position_id"]})
                self.logger.info(f"PROFIT LOCK: {strategy_name} -> breakeven lock (profit={profit_pct*100:.0f}%)")
                return "TIGHTEN_STOP", {}
            if profit_pct >= _move_threshold and not position.get("stop_moved_to_25pct"):
                self.db.update("positions", {"stop_premium": entry_credit * 0.75, "stop_moved_to_25pct": 1},
                                {"position_id": position["position_id"]})
                self.logger.info(f"PROFIT LOCK: {strategy_name} -> 25% profit lock (profit={profit_pct*100:.0f}%)")
                return "TIGHTEN_STOP", {}

        vwap_dist = signals.get("vwap_dist_pct")
        from datetime import time as _dtime
        _now_time = now_ist().time()
        _vwap_exits_active = _now_time < _dtime(14, 30)
        if vwap_dist is not None and _vwap_exits_active:
            if strategy_name == "BULL_PUT_SPREAD" and vwap_dist < -0.30:
                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}
            if strategy_name == "BEAR_CALL_SPREAD" and vwap_dist > 0.30:
                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}
            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY"):
                if vwap_dist > 0.25:
                    return "CLOSE_CALL_SIDE", {"vwap_dist": vwap_dist}
                if vwap_dist < -0.25:
                    return "CLOSE_PUT_SIDE", {"vwap_dist": vwap_dist}

        _now_time_cheap = now_ist().time()
        _is_directional_cheap = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
        _cheap_time_threshold = dtime(14, 45) if _is_directional_cheap else dtime(14, 30)
        if strategy_type == "SELL" and _now_time_cheap >= _cheap_time_threshold:
            _cheap_threshold = 5.00 if _is_directional_cheap else 3.00
            _all_cheap = True
            for _leg_cheap in legs:
                if _leg_cheap["leg_status"] != "OPEN" or _leg_cheap["action"] != "SELL":
                    continue
                _opt_cheap = chain.get(_leg_cheap["strike"], {}).get(_leg_cheap["option_type"], {}) if chain else {}
                _bid_cheap = _opt_cheap.get("bid", 0) or 0
                _ask_cheap = _opt_cheap.get("ask", 0) or 0
                _mark_cheap = (_bid_cheap + _ask_cheap) / 2.0 if (_bid_cheap > 0 and _ask_cheap > 0) else _bid_cheap
                if _mark_cheap > _cheap_threshold:
                    _all_cheap = False
                    break
            if _all_cheap:
                return "CLOSE_TARGET", {"reason_detail": "all_short_legs_below_threshold_cheap_buyback"}

        adx_value = signals.get("adx")
        if (adx_value is not None and adx_value > 30 and strategy_type == "SELL"
                and strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY")):
            return "CLOSE_ADX", {"adx": adx_value}

        portfolio_delta = self._compute_portfolio_delta()
        delta_limit = 0.20 * position["final_lots"] * self.config.lot_size
        if abs(portfolio_delta) > delta_limit:
            return "CLOSE_DELTA", {"portfolio_delta": portfolio_delta}

        hard_exit_str = position.get("hard_exit_time")
        try:
            hard_exit = datetime.strptime(hard_exit_str, "%H:%M").time()
        except Exception:
            hard_exit = self.config.hard_exit_time
        if current_time >= hard_exit:
            return "CLOSE_TIME", {}

        if strategy_type == "SELL":
            _is_directional_delta = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
            _delta_close_limit = 0.50 if _is_directional_delta else 0.40
            _delta_tighten_limit = 0.42 if _is_directional_delta else 0.32
            for leg in legs:
                if leg["action"] == "SELL" and leg["leg_status"] == "OPEN":
                    opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
                    current_delta = abs(opt.get("delta", leg["entry_delta"]) or 0)
                    if current_delta > _delta_close_limit:
                        return "CLOSE_STOP", {"reason_detail": f"short_leg_delta_breach_{current_delta:.3f}"}
                    if current_delta > _delta_tighten_limit and not position.get("stop_tightened_for_delta"):
                        self.db.update(
                            "positions",
                            {"stop_premium": position["stop_premium"] * 0.80, "stop_tightened_for_delta": 1},
                            {"position_id": position["position_id"]},
                        )
                        self.logger.info(f"Short leg delta approaching limit ({current_delta:.3f}) — "
                                          f"tightening stop for {strategy_name}")
                        return "TIGHTEN_STOP", {}

        return "HOLD", {"current_premium": current_premium}

    def monitor_all_positions(self, signals: dict) -> None:
        for position in self._get_open_positions():
            action, context = self.monitor_position(position, signals)
            if action == "HOLD":
                continue
            if action == "TIGHTEN_STOP":
                self.logger.info(f"Stop tightened for {position['strategy_name']} ({position['position_id']})")
                continue
            if action == "CLOSE_CALL_SIDE":
                self.close_one_side(position, "call", action)
            elif action == "CLOSE_PUT_SIDE":
                self.close_one_side(position, "put", action)
            elif action in ("CLOSE_STOP", "CLOSE_TARGET", "CLOSE_VWAP", "CLOSE_ADX", "CLOSE_DELTA", "CLOSE_TIME"):
                self.execute_close(position, action, context)

    # ─────────────────────────────────────────────────────────────────
    # POSITION CLOSING
    # ─────────────────────────────────────────────────────────────────
    def execute_close(self, position: dict, reason: str, context: Optional[dict] = None) -> None:
        legs = self._get_position_legs(position["position_id"])
        open_legs = [l for l in legs if l["leg_status"] == "OPEN"]
        open_legs = sorted(open_legs, key=lambda l: 0 if l["action"] == "BUY" else 1)
        lots = position["final_lots"]
        chain = self.market_engine.last_chain

        exit_legs_info = []
        exit_premium = 0.0

        try:
            for leg in open_legs:
                fill = self.executor.execute_leg_exit(leg, chain, lots)
                exit_price = fill["fill_price"]
                self.db.update("position_legs",
                                {"exit_price": exit_price, "leg_status": "CLOSED",
                                 "broker_order_id_exit": fill["order_id"]},
                                {"leg_id": leg["leg_id"]})
                exit_legs_info.append({**leg, "exit_price": exit_price})
                exit_premium += exit_price if leg["action"] == "SELL" else -exit_price
        except Exception as e:
            self.logger.critical(f"EXIT EXECUTION FAILED for position {position['position_id']}: {e}. "
                                  f"MANUAL INTERVENTION MAY BE REQUIRED — verify broker positions.")
            return

        costs = self._compute_transaction_costs(exit_legs_info, lots, "EXIT")
        total_exit_costs_rupees = costs["total_rupees"]

        if position["strategy_type"] == "SELL":
            gross_pnl_pts = (position["entry_credit"] or 0.0) - exit_premium
        else:
            gross_pnl_pts = exit_premium - (position["entry_debit"] or 0.0)

        C02 = self.config.lot_size
        gross_pnl_rupees = gross_pnl_pts * C02 * lots
        entry_costs_rupees = position.get("entry_costs_rupees") or 0.0
        total_costs_rupees = entry_costs_rupees + total_exit_costs_rupees
        net_pnl_rupees = gross_pnl_rupees - total_costs_rupees
        net_pnl_includes_entry_costs = True
        net_pnl_pts = net_pnl_rupees / (C02 * lots) if (C02 * lots) else 0.0

        current_capital = self.market_engine.state.get("current_capital", self.config.starting_capital)
        net_pnl_pct = (net_pnl_rupees / current_capital * 100.0) if current_capital else 0.0
        result = "WIN" if net_pnl_rupees > 0 else ("LOSS" if net_pnl_rupees < 0 else "BREAKEVEN")
        credit_or_debit = position.get("entry_credit") or position.get("entry_debit") or 0.0
        profit_pct_of_credit = (net_pnl_pts / credit_or_debit * 100.0) if credit_or_debit else 0.0

        now = now_ist()
        entry_time = datetime.fromisoformat(position["entry_time"]) if position.get("entry_time") else now
        hold_minutes = (now - entry_time).total_seconds() / 60.0

        self.db.update("positions", {
            "status": "CLOSED", "exit_time": now.isoformat(), "exit_reason": reason,
            "exit_premium": exit_premium, "gross_pnl_rupees": gross_pnl_rupees,
            "exit_costs_rupees": total_exit_costs_rupees, "net_pnl_rupees": net_pnl_rupees,
            "updated_at": now.isoformat(),
        }, {"position_id": position["position_id"]})

        self.db.insert("trade_exits", {
            "trade_id": position["position_id"], "position_id": position["position_id"],
            "strategy_name": position["strategy_name"], "exit_time": now.isoformat(),
            "hold_minutes": hold_minutes, "exit_reason": reason,
            "exit_spot": self.market_engine.state.get("prev_spot"),
            "exit_vix": self.market_engine.state.get("prev_vix"),
            "exit_adx": (context or {}).get("adx"), "exit_vwap_dist": (context or {}).get("vwap_dist"),
            "exit_legs_json": json.dumps(exit_legs_info, default=str),
            "exit_premium": exit_premium, "gross_pnl_pts": gross_pnl_pts, "gross_pnl_rupees": gross_pnl_rupees,
            "exit_slippage": None, "exit_costs_pts": (costs["total_rupees"] / C02) if C02 else None,
            "exit_costs_rupees": total_exit_costs_rupees, "total_costs_rupees": total_costs_rupees,
            "net_pnl_pts": net_pnl_pts, "net_pnl_rupees": net_pnl_rupees, "net_pnl_pct": net_pnl_pct,
            "result": result, "profit_pct_of_credit": profit_pct_of_credit, "created_at": now.isoformat(),
        })

        self._update_state_after_close(reason, net_pnl_rupees)

        print_section(f"POSITION CLOSED: {position['strategy_name']} — {reason}")
        print_kv_table({
            "Position ID": position["position_id"], "Hold Time (min)": f"{hold_minutes:.1f}",
            "Exit Reason": reason, "Gross P&L (Rs)": gross_pnl_rupees,
            "Total Costs (Rs)": total_costs_rupees, "Net P&L (Rs)": net_pnl_rupees,
            "Net P&L (%)": f"{net_pnl_pct:.3f}%", "Result": result,
        })
        self.logger.info(f"POSITION CLOSED: {position['strategy_name']} reason={reason} "
                          f"net_pnl=Rs{net_pnl_rupees:.2f} result={result}")

    def close_one_side(self, position: dict, side: str, reason: str) -> None:
        legs = self._get_position_legs(position["position_id"])
        side_legs = [l for l in legs if l["option_type"] == side and l["leg_status"] == "OPEN"]
        side_legs = sorted(side_legs, key=lambda l: 0 if l["action"] == "SELL" else 1)
        lots = position["final_lots"]
        chain = self.market_engine.last_chain

        for leg in side_legs:
            try:
                fill = self.executor.execute_leg_exit(leg, chain, lots)
                self.db.update("position_legs",
                                {"exit_price": fill["fill_price"], "leg_status": "CLOSED",
                                 "broker_order_id_exit": fill["order_id"]},
                                {"leg_id": leg["leg_id"]})
            except Exception as e:
                self.logger.error(f"Failed to close {side} side of {position['position_id']}: {e}")
                return

        self.logger.info(f"Closed {side} side of {position['strategy_name']} ({position['position_id']}): {reason}")

        remaining_open = [l for l in self._get_position_legs(position["position_id"]) if l["leg_status"] == "OPEN"]
        if remaining_open:
            new_credit = sum(l["entry_price"] for l in remaining_open if l["action"] == "SELL") \
                - sum(l["entry_price"] for l in remaining_open if l["action"] == "BUY")
            close_one_side_stop_update = True
            raw_p = json.loads(position.get("raw_params_json") or "{}")
            _stop_mult = self.market_engine.state.get("stop_multiplier", 1.5)
            new_stop = new_credit * _stop_mult if new_credit > 0 else position.get("stop_premium")
            self.db.update("positions", {
                "entry_credit": new_credit,
                "stop_premium": new_stop,
                "updated_at": now_ist().isoformat()
            }, {"position_id": position["position_id"]})
        else:
            self._finalize_position_from_partial_closes(position, reason)

    def _finalize_position_from_partial_closes(self, position: dict, reason: str) -> None:
        """Both sides of a condor/butterfly were closed via successive
        close_one_side() calls — finalize P&L using the already-recorded
        exit prices without re-executing any orders."""
        legs = self._get_position_legs(position["position_id"])
        lots = position["final_lots"]
        C02 = self.config.lot_size

        exit_premium = sum(
            (l["exit_price"] if l["action"] == "SELL" else -l["exit_price"])
            for l in legs if l["exit_price"] is not None
        )

        if position["strategy_type"] == "SELL":
            gross_pnl_pts = (position["entry_credit"] or 0.0) - exit_premium
        else:
            gross_pnl_pts = exit_premium - (position["entry_debit"] or 0.0)

        costs = self._compute_transaction_costs(legs, lots, "EXIT")
        total_exit_costs_rupees = costs["total_rupees"]
        entry_costs_for_partial = position.get("entry_costs_rupees") or 0.0
        total_costs_for_partial = entry_costs_for_partial + total_exit_costs_rupees
        gross_pnl_rupees = gross_pnl_pts * C02 * lots
        net_pnl_rupees = gross_pnl_rupees - total_costs_for_partial
        partial_close_net_pnl_fixed = True
        net_pnl_pts = net_pnl_rupees / (C02 * lots) if (C02 * lots) else 0.0
        result = "WIN" if net_pnl_rupees > 0 else ("LOSS" if net_pnl_rupees < 0 else "BREAKEVEN")

        now = now_ist()
        entry_time = datetime.fromisoformat(position["entry_time"]) if position.get("entry_time") else now
        hold_minutes = (now - entry_time).total_seconds() / 60.0
        current_capital = self.market_engine.state.get("current_capital", self.config.starting_capital)
        net_pnl_pct = (net_pnl_rupees / current_capital * 100.0) if current_capital else 0.0
        credit_or_debit = position.get("entry_credit") or position.get("entry_debit") or 0.0
        profit_pct_of_credit = (net_pnl_pts / credit_or_debit * 100.0) if credit_or_debit else 0.0

        self.db.update("positions", {
            "status": "CLOSED", "exit_time": now.isoformat(), "exit_reason": reason,
            "exit_premium": exit_premium, "gross_pnl_rupees": gross_pnl_rupees,
            "exit_costs_rupees": total_exit_costs_rupees, "net_pnl_rupees": net_pnl_rupees,
            "updated_at": now.isoformat(),
        }, {"position_id": position["position_id"]})

        self.db.insert("trade_exits", {
            "trade_id": position["position_id"], "position_id": position["position_id"],
            "strategy_name": position["strategy_name"], "exit_time": now.isoformat(),
            "hold_minutes": hold_minutes, "exit_reason": reason,
            "exit_spot": self.market_engine.state.get("prev_spot"),
            "exit_vix": self.market_engine.state.get("prev_vix"),
            "exit_adx": None, "exit_vwap_dist": None,
            "exit_legs_json": json.dumps(legs, default=str),
            "exit_premium": exit_premium, "gross_pnl_pts": gross_pnl_pts, "gross_pnl_rupees": gross_pnl_rupees,
            "exit_slippage": None, "exit_costs_pts": (costs["total_rupees"] / C02) if C02 else None,
            "exit_costs_rupees": total_exit_costs_rupees,
            "total_costs_rupees": total_costs_for_partial,
            "net_pnl_pts": net_pnl_pts, "net_pnl_rupees": net_pnl_rupees, "net_pnl_pct": net_pnl_pct,
            "result": result, "profit_pct_of_credit": profit_pct_of_credit, "created_at": now.isoformat(),
        })

        self._update_state_after_close(reason, net_pnl_rupees)
        self.logger.info(f"POSITION FULLY CLOSED (via partial closes): {position['strategy_name']} "
                          f"net_pnl=Rs{net_pnl_rupees:.2f} result={result}")

    def _update_state_after_close(self, reason: str, net_pnl_rupees: float) -> None:
        state = self.market_engine.state
        state["daily_pnl"] = state.get("daily_pnl", 0.0) + net_pnl_rupees
        state["current_capital"] = state.get("current_capital", self.config.starting_capital) + net_pnl_rupees

        if reason in ("CLOSE_STOP", "SHUTDOWN_CLOSE", "EOD_CLOSE", "HARD_EXIT_15:00"):
            if reason == "CLOSE_STOP":
                state["last_stop_time"] = now_ist().isoformat()
                state["last_stop_reason"] = reason
                state["consecutive_stops"] = state.get("consecutive_stops", 0) + 1
            _open_pos_list = self._get_open_positions()
            if not _open_pos_list:
                from market_data_engine import MarketDataEngine as _MDE
                _sig = self.market_engine.state
                _combo = f"{_sig.get('volatility_condition', '')}_{_sig.get('trend_condition', '')}_{_sig.get('direction', '')}"
                state["last_stop_signal_combo"] = _combo
            if state["consecutive_stops"] >= 2:
                state["daily_halted"] = True
                self.logger.warning("2 consecutive stops — halting trading for the day")
        elif reason in ("CLOSE_ADX", "CLOSE_VWAP", "CLOSE_DELTA"):
            state["last_stop_time"] = now_ist().isoformat()
            state["last_stop_reason"] = reason
        elif reason in ("SHUTDOWN_CLOSE", "EOD_CLOSE", "HARD_EXIT_15:00", "SELF_TEST_CLEANUP"):
            state["consecutive_stops"] = 0
            current_entry_count = state.get("entry_count", 1)
            state["entry_count"] = max(0, current_entry_count - 1)
        else:
            state["consecutive_stops"] = 0
            if reason == "CLOSE_TARGET":
                current_count = state.get("entry_count", 1)
                state["entry_count"] = max(0, current_count - 1)

        if state["current_capital"]:
            daily_loss_pct = max(0.0, -state["daily_pnl"]) / state["current_capital"]
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                state["daily_halted"] = True
                self.logger.warning(f"DAILY LOSS LIMIT: {daily_loss_pct*100:.2f}% — halting trading")

        self.db.update("session_state",
            {"daily_pnl": state["daily_pnl"], "current_capital": state["current_capital"]},
            {"trading_date": today_ist().isoformat()})
        self.market_engine._save_session_state()

    def close_all_positions(self, reason: str) -> None:
        """Used by File 5 for hard-exit-time sweeps, EOD cleanup, and
        graceful shutdown."""
        for position in self._get_open_positions():
            self.execute_close(position, reason)


# ─────────────────────────────────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────────────────────────────────
def _self_test() -> None:
    print_section("NIFTY ALGO — EXECUTION ENGINE SELF-TEST")
    config = load_config()
    db = Database(config.db_path)
    logger = setup_logging(db, config.log_dir)
    rate_limiter = RateLimiter(config.rate_limits)
    client = UpstoxClient(config, rate_limiter, db, logger)

    from strategy_engine import StrategyEngine  # local import to avoid a hard circular dependency at module load

    market_engine = MarketDataEngine(config, db, client, rate_limiter, logger)
    strategy_engine = StrategyEngine(config, db, market_engine, logger)
    execution_engine = ExecutionEngine(config, db, market_engine, client, logger)

    if not config.upstox_access_token or not client.validate_token():
        logger.warning("No valid Upstox token — cannot run a live self-test.")
        db.close()
        return

    signals = market_engine.run_cycle()
    decision = strategy_engine.decide(signals)
    print_section(f"STRATEGY DECISION: {decision['action']}")

    if decision["action"] == "ENTER":
        position_id = execution_engine.process_entry_decision(decision, signals)
        if position_id:
            logger.info(f"Self-test opened paper position {position_id}. "
                        f"Monitoring once, then closing for cleanup.")
            execution_engine.monitor_all_positions(signals)
            execution_engine.close_all_positions("SELF_TEST_CLEANUP")
    else:
        print(decision.get("reason"))

    db.close()


if __name__ == "__main__":
    _self_test()