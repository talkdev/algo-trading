from __future__ import annotations

import json
import time as time_module
import uuid
from datetime import datetime, date, time as dtime
from typing import Optional, List

from nifty_algo_core import (
    Config, Database, RateLimiter, UpstoxClient,
    ExpiryCalendar, now_ist, today_ist,
    print_section, print_kv_table,
    load_config, setup_logging,
)
from market_data_engine import MarketDataEngine
from strategy_engine import StrategyEngine


class PaperOrderExecutor:

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
        self.logger.info(
            f"[PAPER] ENTRY: {leg['action']} {leg['option_type'].upper()} "
            f"{leg['strike']:.0f} x{lots} @ {exec_price:.2f} (order={order_id})"
        )
        return {"order_id": order_id, "fill_price": exec_price, "status": "FILLED"}

    def execute_leg_exit(self, leg: dict, chain: dict, lots: int) -> dict:
        order_id = self._next_order_id()
        opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
        bid = opt.get("bid", 0) or 0
        ask = opt.get("ask", 0) or 0
        ltp = opt.get("ltp", 0) or 0
        entry_price = leg.get("entry_price", 0) or 0
        if leg["action"] == "SELL":
            fill_price = ask if ask > 0 else (ltp if ltp > 0 else entry_price)
        else:
            fill_price = bid if bid > 0 else (ltp if ltp > 0 else entry_price)
        self.logger.info(
            f"[PAPER] EXIT: close {leg['action']} {leg['option_type'].upper()} "
            f"{leg['strike']:.0f} x{lots} @ {fill_price:.2f} (order={order_id})"
        )
        return {"order_id": order_id, "fill_price": fill_price, "status": "FILLED"}


class LiveOrderExecutor:

    def __init__(self, config: Config, client: UpstoxClient, logger):
        if config.paper_trade_mode:
            raise RuntimeError(
                "LiveOrderExecutor instantiated while PAPER_TRADE_MODE=True. Refusing."
            )
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
        self.logger.warning(f"Using fallback price for order {order_id}")
        return fallback

    def _aggressive_limit_price(
        self, chain: dict, strike: float, opt_type: str,
        transaction_type: str, fallback: float
    ) -> float:
        opt = chain.get(strike, {}).get(opt_type, {}) if chain else {}
        bid = opt.get("bid", 0) or 0
        ask = opt.get("ask", 0) or 0
        tick = 0.10
        if transaction_type == "BUY":
            price = ask + (2 * tick) if ask > 0 else (bid + (4 * tick) if bid > 0 else fallback + (2 * tick))
        else:
            price = max(bid - (2 * tick), tick) if bid > 0 else max(ask - (4 * tick), tick) if ask > 0 else max(fallback - (2 * tick), tick)
        return round(round(price / tick) * tick, 2)

    def execute_leg_entry(self, leg: dict, lots: int, chain: dict) -> dict:
        instrument_key = self._resolve_instrument_key(leg, chain)
        if not instrument_key:
            raise RuntimeError(
                f"No instrument_key for leg {leg['strike']} {leg['option_type']}"
            )
        qty = lots * self.config.lot_size
        transaction_type = "SELL" if leg["action"] == "SELL" else "BUY"
        limit_price = self._aggressive_limit_price(
            chain, leg["strike"], leg["option_type"],
            transaction_type, leg["exec_price"]
        )
        result = self.client.place_order(
            instrument_token=instrument_key, quantity=qty,
            transaction_type=transaction_type, order_type="LIMIT",
            price=limit_price, product="I",
        )
        order_id = result.get("order_id", "")
        self.logger.info(
            f"[LIVE] ENTRY ORDER: {transaction_type} {leg['option_type'].upper()} "
            f"{leg['strike']:.0f} x{lots} order_id={order_id}"
        )
        fill_price = self._get_fill_price(order_id, fallback=leg["exec_price"])
        return {"order_id": order_id, "fill_price": fill_price, "status": "FILLED"}

    def execute_leg_exit(self, leg: dict, chain: dict, lots: int) -> dict:
        instrument_key = self._resolve_instrument_key(leg, chain)
        if not instrument_key:
            raise RuntimeError(
                f"No instrument_key for leg {leg['strike']} {leg['option_type']}"
            )
        qty = lots * self.config.lot_size
        transaction_type = "BUY" if leg["action"] == "SELL" else "SELL"
        fallback = leg.get("entry_price", 0) or 0
        limit_price = self._aggressive_limit_price(
            chain, leg["strike"], leg["option_type"],
            transaction_type, fallback
        )
        result = self.client.place_order(
            instrument_token=instrument_key, quantity=qty,
            transaction_type=transaction_type, order_type="LIMIT",
            price=limit_price, product="I",
        )
        order_id = result.get("order_id", "")
        self.logger.info(
            f"[LIVE] EXIT ORDER: {transaction_type} {leg['option_type'].upper()} "
            f"{leg['strike']:.0f} x{lots} order_id={order_id}"
        )
        fill_price = self._get_fill_price(order_id, fallback=fallback)
        return {"order_id": order_id, "fill_price": fill_price, "status": "FILLED"}


class ExecutionEngine:

    def __init__(
        self, config: Config, db: Database,
        market_engine: MarketDataEngine,
        client: UpstoxClient, logger
    ):
        self.config = config
        self.db = db
        self.market_engine = market_engine
        self.logger = logger

        self._ensure_extra_columns()

        if config.paper_trade_mode:
            self.executor = PaperOrderExecutor(config, logger)
            logger.info("ExecutionEngine: PAPER TRADE mode.")
        else:
            self.executor = LiveOrderExecutor(config, client, logger)
            logger.warning("ExecutionEngine: LIVE TRADING mode — REAL ORDERS WILL BE PLACED.")

    def _ensure_extra_columns(self) -> None:
        extra = [
            ("positions", "stop_tightened_for_delta", "INTEGER DEFAULT 0"),
            ("positions", "defined_risk_only",        "INTEGER DEFAULT 0"),
            ("positions", "final_regime_at_entry",    "TEXT"),
            ("positions", "confidence_at_entry",      "TEXT"),
            ("positions", "event_day",                "INTEGER DEFAULT 0"),
            ("positions", "event_name",               "TEXT DEFAULT ''"),
        ]
        for table, col, coltype in extra:
            self.db.ensure_column(table, col, coltype)

    def _get_open_positions(self) -> list:
        return self.db.query(
            "SELECT * FROM positions WHERE trading_date=? AND status='OPEN'",
            (today_ist().isoformat(),),
        )

    def _get_position_legs(self, position_id: str) -> list:
        return self.db.query(
            "SELECT * FROM position_legs WHERE position_id=?", (position_id,)
        )

    def _compute_transaction_costs(
        self, legs: list, lots: int, action: str
    ) -> dict:
        C02 = self.config.lot_size
        sell_pts = buy_pts = 0.0
        num_orders = len(legs)
        for leg in legs:
            price = (leg.get("entry_price", 0) or 0) if action == "ENTRY" else (leg.get("exit_price", 0) or leg.get("entry_price", 0) or 0)
            if price <= 0:
                price = leg.get("entry_price", 0) or 0
            qty = lots * C02
            if price <= 0:
                continue
            premium_value = price * qty
            if leg["action"] == "SELL":
                sell_pts += premium_value
            else:
                buy_pts += premium_value

        turnover = sell_pts + buy_pts
        if turnover <= 0:
            return {"total_rupees": 0.0, "breakdown": {}}

        stt      = sell_pts * self.config.stt_options_sell
        exchange = turnover * self.config.exchange_txn_rate
        sebi     = turnover * self.config.sebi_rate
        stamp    = buy_pts  * self.config.stamp_duty_buy_options
        brokerage = self.config.brokerage_per_order * num_orders
        gst      = (brokerage + exchange + sebi) * 0.18
        total    = stt + exchange + sebi + stamp + brokerage + gst

        return {
            "total_rupees": round(total, 2),
            "breakdown": {
                "stt": round(stt, 2), "exchange": round(exchange, 2),
                "sebi": round(sebi, 4), "stamp": round(stamp, 4),
                "brokerage": round(brokerage, 2), "gst": round(gst, 2),
                "total": round(total, 2),
            },
        }

    def _compute_portfolio_delta(self) -> float:
        chain = self.market_engine.last_chain
        total_delta = 0.0
        C02 = self.config.lot_size
        for pos in self._get_open_positions():
            for leg in self._get_position_legs(pos["position_id"]):
                if leg["leg_status"] != "OPEN":
                    continue
                opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
                live_delta = opt.get("delta", leg["entry_delta"]) if opt else (leg["entry_delta"] or 0)
                lots = leg["qty"] // C02 if C02 > 0 else 1
                sign = -1 if leg["action"] == "SELL" else 1
                total_delta += sign * (live_delta or 0) * lots
        return total_delta

    def _compute_portfolio_vega_gamma(self) -> tuple:
        chain = self.market_engine.last_chain
        total_vega = total_gamma = 0.0
        C02 = self.config.lot_size
        for pos in self._get_open_positions():
            for leg in self._get_position_legs(pos["position_id"]):
                if leg["leg_status"] != "OPEN":
                    continue
                opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
                vega  = opt.get("vega",  leg["entry_vega"]  or 0) if opt else (leg["entry_vega"]  or 0)
                gamma = opt.get("gamma", leg["entry_gamma"] or 0) if opt else (leg["entry_gamma"] or 0)
                lots = leg["qty"] // C02 if C02 > 0 else 1
                sign = -1 if leg["action"] == "SELL" else 1
                total_vega  += sign * (vega  or 0) * lots
                total_gamma += sign * (gamma or 0) * lots
        return total_vega, total_gamma

    def _get_mark_price(self, leg: dict, chain: dict) -> float:
        if not chain:
            return leg.get("entry_price", 0) or 0
        opt = chain.get(leg["strike"], {}).get(leg["option_type"], {})
        if not opt:
            return leg.get("entry_price", 0) or 0
        bid = opt.get("bid", 0) or 0
        ask = opt.get("ask", 0) or 0
        ltp = opt.get("ltp", 0) or 0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        if ltp > 0:
            return ltp
        if bid > 0:
            return bid
        if ask > 0:
            return ask
        return leg.get("entry_price", 0) or 0

    def _compute_current_premium(self, legs: list, chain: dict) -> float:
        premium = 0.0
        for leg in legs:
            if leg["leg_status"] != "OPEN":
                continue
            mark = self._get_mark_price(leg, chain)
            premium += mark if leg["action"] == "SELL" else -mark
        return premium

    def validate_pre_trade(self, params: dict, signals: dict) -> tuple:
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
            self.logger.info(f"Soft daily loss limit: reducing size 50%")

        final_lots = params["final_lots"]
        trade_max_loss = params["total_max_risk"]
        projected_pct = (daily_loss + trade_max_loss) / current_cap
        max_projected = self.config.max_daily_loss_pct * 1.25

        if projected_pct > max_projected:
            max_additional = (current_cap * max_projected) - daily_loss
            if max_additional <= 0:
                return "NO_GO", {"reason": "projected_daily_loss_would_exceed_limit"}
            max_lots_by_daily = max(1, int(max_additional / params["max_loss_per_lot"]))
            if max_lots_by_daily < final_lots:
                self.logger.info(f"Lots reduced {final_lots} -> {max_lots_by_daily} for daily loss limit")
                final_lots = max_lots_by_daily

        final_lots = max(1, int(final_lots * size_adj))

        if params.get("defined_risk_only"):
            undefined_risk = {"LONG_STRADDLE"}
            strategy_name = params.get("strategy_name", "")
            if strategy_name in undefined_risk:
                return "NO_GO", {"reason": f"event_day_defined_risk_only_{strategy_name}_not_allowed"}

        C02 = self.config.lot_size
        new_delta = sum(
            (-1 if leg["action"] == "SELL" else 1) * (leg.get("delta", 0) or 0) * final_lots
            for leg in params["legs"]
        )
        current_portfolio_delta = self._compute_portfolio_delta()
        post_trade_delta = current_portfolio_delta + new_delta
        total_open_lots = sum(p["final_lots"] for p in self._get_open_positions()) + final_lots
        delta_limit = 0.20 * total_open_lots * self.config.lot_size

        if abs(post_trade_delta) > delta_limit:
            reduced = final_lots - 1
            found = False
            while reduced >= 1:
                new_d = sum(
                    (-1 if leg["action"] == "SELL" else 1) * (leg.get("delta", 0) or 0) * reduced
                    for leg in params["legs"]
                )
                total_lots_r = sum(p["final_lots"] for p in self._get_open_positions()) + reduced
                if abs(current_portfolio_delta + new_d) <= (0.20 * total_lots_r):
                    final_lots = reduced
                    found = True
                    break
                reduced -= 1
            if not found:
                return "NO_GO", {"reason": "portfolio_delta_exceeds_limit_cannot_reduce"}

        new_vega  = sum((-1 if l["action"] == "SELL" else 1) * (l.get("vega",  0) or 0) * final_lots for l in params["legs"])
        new_gamma = sum((-1 if l["action"] == "SELL" else 1) * (l.get("gamma", 0) or 0) * final_lots for l in params["legs"])
        cur_vega, cur_gamma = self._compute_portfolio_vega_gamma()
        post_vega  = cur_vega  + new_vega
        post_gamma = cur_gamma + new_gamma
        total_lots_port = sum(p["final_lots"] for p in self._get_open_positions()) + final_lots
        vega_limit  = 120.0 * total_lots_port
        gamma_limit = 0.30  * total_lots_port
        if abs(post_vega) > vega_limit:
            return "NO_GO", {"reason": f"portfolio_vega_{post_vega:.1f}_exceeds_{vega_limit:.1f}"}
        if abs(post_gamma) > gamma_limit:
            return "NO_GO", {"reason": f"portfolio_gamma_{post_gamma:.4f}_exceeds_{gamma_limit:.4f}"}

        chain = self.market_engine.last_chain
        if not chain:
            return "NO_GO", {"reason": "chain_unavailable_at_execution"}

        for leg in params["legs"]:
            strike, opt_type = leg["strike"], leg["option_type"]
            opt = chain.get(strike, {}).get(opt_type, {})
            if not opt:
                return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_not_in_chain"}
            bid = opt.get("bid", 0) or 0
            ask = opt.get("ask", 0) or 0
            oi  = opt.get("oi",  0) or 0
            if bid <= 0 and ask <= 0:
                return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_no_bid_ask"}
            if oi < 500:
                return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_oi_{oi}_below_500"}
            if bid > 0 and ask > 0 and (ask - bid) / ask > 0.08:
                return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_spread_too_wide"}
            current_price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
            original_price = leg["exec_price"]
            if original_price > 0 and current_price > 0:
                drift = abs(current_price - original_price) / original_price
                if drift > 0.25:
                    return "NO_GO", {"reason": f"leg_{strike}_{opt_type}_price_drifted_{drift*100:.0f}pct"}

        if params.get("strategy_type") == "SELL" and params.get("entry_credit", 0) > 0:
            current_gross = 0.0
            for leg in params["legs"]:
                opt = chain.get(leg["strike"], {}).get(leg["option_type"], {})
                bid = opt.get("bid", 0) or 0
                ask = opt.get("ask", 0) or 0
                ltp = opt.get("ltp", 0) or 0
                if leg["action"] == "SELL":
                    current_gross += bid if bid > 0 else ltp
                else:
                    current_gross -= ask if ask > 0 else ltp
            original_credit = params["entry_credit"]
            if original_credit > 0:
                decay = (original_credit - current_gross) / original_credit
                if decay > 0.20:
                    return "NO_GO", {"reason": f"credit_decayed_{decay*100:.0f}pct_since_computed"}

        current_time = now_ist().time()
        state = self.market_engine.state
        try:
            entry_start = datetime.strptime(state["entry_start"], "%H:%M").time()
            entry_end   = datetime.strptime(state["entry_end"],   "%H:%M").time()
        except Exception:
            entry_start = self.config.trading_window_start
            entry_end   = self.config.trading_window_last_entry

        if current_time > entry_end:
            return "NO_GO", {"reason": f"past_entry_window_{entry_end}"}
        if current_time < entry_start:
            return "NO_GO", {"reason": f"before_entry_window_{entry_start}"}

        try:
            hard_exit = datetime.strptime(
                state.get("hard_exit_time", "15:25"), "%H:%M"
            ).time()
        except Exception:
            hard_exit = self.config.hard_exit_time

        dte = state.get("actual_dte")
        min_buffer = 90 if dte == 0 else 60
        dt1 = datetime.combine(today_ist(), current_time)
        dt2 = datetime.combine(today_ist(), hard_exit)
        mins_to_exit = (dt2 - dt1).total_seconds() / 60.0
        if mins_to_exit < min_buffer:
            return "NO_GO", {"reason": f"only_{mins_to_exit:.0f}min_before_hard_exit_need_{min_buffer}"}

        if dte == 0 and current_time > dtime(13, 30):
            return "NO_GO", {"reason": "0dte_past_13:30_entry_cutoff"}

        params["final_lots"]    = final_lots
        params["total_max_risk"] = params["max_loss_per_lot"] * final_lots
        return "GO", params

    def _emergency_unwind(self, filled_legs: list, lots: int, chain: dict) -> None:
        for leg in filled_legs:
            try:
                self.executor.execute_leg_exit(leg, chain, lots)
                self.logger.warning(
                    f"Emergency unwind: closed {leg['action']} "
                    f"{leg['option_type']} {leg['strike']:.0f}"
                )
            except Exception as e:
                self.logger.critical(
                    f"EMERGENCY UNWIND FAILED for {leg['strike']} "
                    f"{leg['option_type']}: {e}. "
                    f"MANUAL INTERVENTION REQUIRED."
                )

    def execute_entry(self, params: dict, signals: dict) -> Optional[str]:
        position_id = str(uuid.uuid4())
        lots  = params["final_lots"]
        chain = self.market_engine.last_chain
        filled_legs = []

        try:
            buy_legs  = [l for l in params["legs"] if l["action"] == "BUY"]
            sell_legs = [l for l in params["legs"] if l["action"] == "SELL"]
            for leg in buy_legs + sell_legs:
                fill = self.executor.execute_leg_entry(leg, lots, chain)
                filled_legs.append({**leg, "fill": fill})
        except Exception as e:
            self.logger.error(f"Entry execution failed: {e}")
            if filled_legs:
                self.logger.critical(
                    f"PARTIAL FILL on entry — {len(filled_legs)}/{len(params['legs'])} "
                    f"legs filled. Attempting emergency unwind."
                )
                self._emergency_unwind(filled_legs, lots, chain)
            return None

        now = now_ist()
        actual_fill_legs = [{**fl, "entry_price": fl["fill"]["fill_price"]} for fl in filled_legs]
        entry_costs = self._compute_transaction_costs(actual_fill_legs, lots, "ENTRY")
        actual_entry_costs_rs = entry_costs["total_rupees"]

        self.db.insert("positions", {
            "position_id": position_id,
            "trading_date": today_ist().isoformat(),
            "strategy_name": params["strategy_name"],
            "strategy_type": params["strategy_type"],
            "selection_reason": params["selection_reason"],
            "target_expiry": params["target_expiry"],
            "actual_dte": params["actual_dte"],
            "entry_time": now.isoformat(),
            "entry_spot": params["entry_spot"],
            "entry_vix": params["entry_vix"],
            "entry_vrp": params["entry_vrp"],
            "entry_credit": params.get("entry_credit"),
            "entry_debit": params.get("entry_debit"),
            "gross_credit": params.get("gross_credit"),
            "total_slippage": params["total_slippage"],
            "entry_costs_rupees": actual_entry_costs_rs,
            "stop_premium": params.get("stop_premium"),
            "target_premium": params.get("target_premium"),
            "stop_value": params.get("stop_value"),
            "target_value": params.get("target_value"),
            "price_stop_pts": params["price_stop_pts"],
            "hard_exit_time": params["hard_exit_time"],
            "final_lots": lots,
            "max_loss_per_lot": params["max_loss_per_lot"],
            "total_max_risk": params["total_max_risk"],
            "estimated_margin": params["estimated_margin"],
            "status": "OPEN",
            "last_known_premium": params["last_known_premium"],
            "stop_at_breakeven": 0,
            "stop_moved_to_25pct": 0,
            "stop_tightened_for_delta": 0,
            "defined_risk_only": int(params.get("defined_risk_only", False)),
            "event_day": int(params.get("event_day", False)),
            "event_name": params.get("event_name", ""),
            "final_regime_at_entry": params.get("final_regime_at_entry"),
            "confidence_at_entry": params.get("confidence_at_entry"),
            "paper_trade": 1 if self.config.paper_trade_mode else 0,
            "raw_params_json": json.dumps(params, default=str),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        })

        for leg in filled_legs:
            self.db.insert("position_legs", {
                "position_id": position_id,
                "strike": leg["strike"],
                "option_type": leg["option_type"],
                "action": leg["action"],
                "qty": lots * self.config.lot_size,
                "entry_price": leg["fill"]["fill_price"],
                "exit_price": None,
                "entry_bid": leg.get("bid", 0),
                "entry_ask": leg.get("ask", 0),
                "entry_delta": leg.get("delta", 0),
                "entry_gamma": leg.get("gamma", 0),
                "entry_vega": leg.get("vega", 0),
                "entry_theta": leg.get("theta", 0),
                "entry_iv": leg.get("iv", 0),
                "entry_oi": leg.get("oi", 0),
                "exit_delta": None,
                "broker_order_id_entry": leg["fill"]["order_id"],
                "broker_order_id_exit": None,
                "quoted_mid_at_entry": (leg.get("bid", 0) + leg.get("ask", 0)) / 2.0 if (leg.get("bid", 0) > 0 and leg.get("ask", 0) > 0) else leg.get("exec_price", 0),
                "quoted_mid_at_exit": None,
                "leg_status": "OPEN",
            })

        self._persist_trade_entry(position_id, params, signals)

        state = self.market_engine.state
        state["entry_count"]       = state.get("entry_count", 0) + 1
        state["consecutive_stops"] = 0
        state["last_entry_time"]   = now_ist().isoformat()
        self.market_engine._save_session_state()

        print_section(f"POSITION OPENED: {params['strategy_name']}")
        print_kv_table({
            "Position ID": position_id,
            "Lots": lots,
            "Entry Credit/Debit (pts)": params.get("entry_credit") or params.get("entry_debit"),
            "Stop (pts)": params.get("stop_premium") or params.get("stop_value"),
            "Target (pts)": params.get("target_premium") or params.get("target_value"),
            "Max Risk (Rs)": params["total_max_risk"],
            "Defined Risk Only": params.get("defined_risk_only"),
            "Event Day": params.get("event_day"),
            "Final Regime": params.get("final_regime_at_entry"),
            "Confidence": params.get("confidence_at_entry"),
            "Paper Trade": self.config.paper_trade_mode,
        })
        self.logger.info(
            f"ENTRY EXECUTED: {params['strategy_name']} position_id={position_id} "
            f"lots={lots} paper={self.config.paper_trade_mode}"
        )
        return position_id

    def _persist_trade_entry(self, position_id: str, params: dict, s: dict) -> None:
        self.db.insert("trade_entries", {
            "trade_id": position_id,
            "position_id": position_id,
            "strategy_name": params["strategy_name"],
            "entry_time": now_ist().isoformat(),
            "trading_date": s.get("trading_date", today_ist().isoformat()),
            "day_label": self.market_engine.state.get("day_label"),
            "entry_spot": params["entry_spot"],
            "entry_vix": params["entry_vix"],
            "entry_vrp": params["entry_vrp"],
            "entry_atm_iv": s.get("atm_iv"),
            "entry_parkinson_rv": s.get("parkinson_rv"),
            "entry_adx": s.get("adx_15"),
            "entry_vwap": s.get("vwap"),
            "entry_vwap_dist": s.get("vwap_dist_pct"),
            "entry_pcr": s.get("pcr"),
            "entry_pcr_change": s.get("pcr_change"),
            "entry_skew_ratio": s.get("skew_ratio"),
            "or_width": s.get("or_width"),
            "or_condition": s.get("or_condition"),
            "volatility_condition": s.get("volatility_condition"),
            "iv_behavior": s.get("iv_behavior"),
            "trend_condition": s.get("trend_condition"),
            "adx_condition": s.get("adx_condition"),
            "direction": s.get("direction"),
            "vwap_signal": s.get("vwap_signal"),
            "pcr_signal": s.get("pcr_signal"),
            "skew_signal": s.get("skew_signal"),
            "preferred_sell_side": s.get("preferred_sell_side"),
            "target_expiry": params["target_expiry"],
            "actual_dte": params["actual_dte"],
            "legs_json": json.dumps(params["legs"], default=str),
            "entry_credit": params.get("entry_credit"),
            "entry_debit": params.get("entry_debit"),
            "gross_credit": params.get("gross_credit"),
            "total_slippage": params["total_slippage"],
            "entry_costs_pts": params["total_costs_pts"],
            "entry_costs_rupees": params.get("total_costs_rupees_per_lot", 0) * params["final_lots"] + params.get("total_fixed_costs_rupees", 0),
            "stop_premium": params.get("stop_premium"),
            "target_premium": params.get("target_premium"),
            "price_stop_pts": params["price_stop_pts"],
            "hard_exit_time": params["hard_exit_time"],
            "final_lots": params["final_lots"],
            "max_loss_per_lot": params["max_loss_per_lot"],
            "total_max_risk": params["total_max_risk"],
            "capital_at_entry": self.market_engine.state.get("current_capital"),
            "daily_pnl_at_entry": self.market_engine.state.get("daily_pnl"),
            "paper_trade": 1 if self.config.paper_trade_mode else 0,
            "selection_reason": params["selection_reason"],
            "final_regime_at_entry": params.get("final_regime_at_entry"),
            "confidence_at_entry": params.get("confidence_at_entry"),
            "created_at": now_ist().isoformat(),
        })

    def process_entry_decision(self, decision: dict, signals: dict) -> Optional[str]:
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

    def monitor_position(self, position: dict, signals: dict) -> tuple:
        legs  = self._get_position_legs(position["position_id"])
        chain_expiry = self.market_engine.last_chain_expiry
        chain_matches = (chain_expiry and
                         chain_expiry.isoformat() == position["target_expiry"])
        chain = self.market_engine.last_chain if chain_matches else {}

        current_premium = self._compute_current_premium(legs, chain)
        self.db.update(
            "positions",
            {"last_known_premium": current_premium, "updated_at": now_ist().isoformat()},
            {"position_id": position["position_id"]},
        )

        current_time  = now_ist().time()
        strategy_type = position["strategy_type"]
        strategy_name = position["strategy_name"]
        raw_params    = json.loads(position.get("raw_params_json") or "{}")
        tightening    = raw_params.get("tightening_schedule", [])
        orig_stop     = raw_params.get("stop_premium") or position.get("stop_premium")
        effective_stop = position.get("stop_premium")

        if effective_stop is not None and orig_stop:
            best_factor = 1.0
            for tighten_time_str, factor in tightening:
                try:
                    tighten_time = datetime.strptime(tighten_time_str, "%H:%M").time()
                except Exception:
                    continue
                if current_time >= tighten_time:
                    best_factor = min(best_factor, factor)
            effective_stop = orig_stop * best_factor
            if strategy_type == "SELL" and position.get("entry_time"):
                try:
                    entry_dt = datetime.fromisoformat(position["entry_time"])
                    hold_min = (now_ist() - entry_dt).total_seconds() / 60.0
                    if hold_min > 120:
                        time_factor = max(0.50, 1.0 - (hold_min - 120) / 180.0)
                        effective_stop = min(effective_stop, (position.get("stop_premium") or orig_stop) * time_factor)
                except Exception:
                    pass

        if strategy_type == "SELL" and position.get("entry_credit") and position["entry_credit"] > 0:
            is_dir = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
            if is_dir:
                gc = position.get("gross_credit") or position["entry_credit"]
                credit_stop_limit = gc * 2.5
                if current_premium >= credit_stop_limit:
                    return "CLOSE_STOP", {"current_premium": current_premium}
            else:
                credit_stop_limit = position["entry_credit"] * 1.8
                actual_stop = min(
                    effective_stop if effective_stop is not None else credit_stop_limit,
                    credit_stop_limit
                )
                if current_premium >= actual_stop:
                    return "CLOSE_STOP", {"current_premium": current_premium}
        elif strategy_type == "SELL" and effective_stop is not None and current_premium >= effective_stop:
            return "CLOSE_STOP", {"current_premium": current_premium}
        if strategy_type == "BUY" and position.get("stop_value") is not None and current_premium <= position["stop_value"]:
            return "CLOSE_STOP", {"current_premium": current_premium}

        price_stop_pts = position.get("price_stop_pts") or 0
        spot = signals.get("spot")
        if price_stop_pts > 0 and spot is not None and position.get("entry_spot"):
            spot_move = spot - position["entry_spot"]
            if strategy_name == "BULL_PUT_SPREAD":
                triggered = spot_move <= -price_stop_pts
            elif strategy_name == "BEAR_CALL_SPREAD":
                triggered = spot_move >= price_stop_pts
            else:
                triggered = abs(spot_move) >= price_stop_pts
            if triggered:
                return "CLOSE_STOP", {"reason_detail": f"price_stop_{abs(spot_move):.0f}pts"}

        if strategy_type == "SELL" and position.get("entry_credit"):
            entry_credit = position["entry_credit"]
            is_dir_td = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
            if is_dir_td:
                if current_time >= dtime(15, 0):
                    time_target_pct = 0.30
                elif current_time >= dtime(14, 30):
                    time_target_pct = 0.38
                elif current_time >= dtime(14, 0):
                    time_target_pct = 0.45
                else:
                    time_target_pct = None
            else:
                if current_time >= dtime(14, 30):
                    time_target_pct = 0.35
                elif current_time >= dtime(14, 0):
                    time_target_pct = 0.40
                elif current_time >= dtime(13, 0):
                    time_target_pct = 0.45
                else:
                    time_target_pct = None

            if time_target_pct is not None:
                time_target = entry_credit * (1.0 - time_target_pct)
                eff_target = min(
                    position["target_premium"] if position.get("target_premium") is not None else time_target,
                    time_target
                )
                if current_premium <= eff_target:
                    return "CLOSE_TARGET", {"current_premium": current_premium, "time_decay_target": True}
            elif position.get("target_premium") is not None and current_premium <= position["target_premium"]:
                return "CLOSE_TARGET", {"current_premium": current_premium}
        elif strategy_type == "SELL" and position.get("target_premium") is not None and current_premium <= position["target_premium"]:
            return "CLOSE_TARGET", {"current_premium": current_premium}
        if strategy_type == "BUY" and position.get("target_value") is not None and current_premium >= position["target_value"]:
            return "CLOSE_TARGET", {"current_premium": current_premium}

        if strategy_type == "SELL" and position.get("entry_credit"):
            entry_credit = position["entry_credit"]
            gc_for_pct = position.get("gross_credit") or entry_credit
            profit_pct = (gc_for_pct - current_premium) / gc_for_pct if gc_for_pct > 0 else 0
            C02 = self.config.lot_size
            lots = position.get("final_lots", 1)
            entry_costs_pts = (position.get("entry_costs_rupees") or 0.0) / max(C02 * lots, 1)
            true_breakeven = entry_credit - entry_costs_pts
            is_dir_lock = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
            lock_thresh = 0.25 if is_dir_lock else 0.20
            move_thresh = 0.55 if is_dir_lock else 0.50

            if profit_pct >= lock_thresh and not position.get("stop_at_breakeven"):
                lock_stop = max(true_breakeven, entry_credit * 0.80)
                self.db.update("positions",
                               {"stop_premium": lock_stop, "stop_at_breakeven": 1},
                               {"position_id": position["position_id"]})
                self.logger.info(f"PROFIT LOCK: {strategy_name} -> breakeven lock (profit={profit_pct*100:.0f}%)")
                return "TIGHTEN_STOP", {}
            if profit_pct >= move_thresh and not position.get("stop_moved_to_25pct"):
                self.db.update("positions",
                               {"stop_premium": entry_credit * 0.85, "stop_moved_to_25pct": 1},
                               {"position_id": position["position_id"]})
                self.logger.info(f"PROFIT LOCK: {strategy_name} -> 25% lock (profit={profit_pct*100:.0f}%)")
                return "TIGHTEN_STOP", {}

        vwap_dist = signals.get("vwap_dist_pct")
        if vwap_dist is not None and current_time < dtime(14, 30):
            if strategy_name == "BULL_PUT_SPREAD" and vwap_dist < -0.30:
                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}
            if strategy_name == "BEAR_CALL_SPREAD" and vwap_dist > 0.30:
                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}
            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY"):
                if vwap_dist > 0.25:
                    return "CLOSE_CALL_SIDE", {"vwap_dist": vwap_dist}
                if vwap_dist < -0.25:
                    return "CLOSE_PUT_SIDE", {"vwap_dist": vwap_dist}

        if strategy_type == "SELL" and current_time >= dtime(14, 30):
            cheap_thresh = 5.00 if strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD") else 3.00
            all_cheap = True
            for leg in legs:
                if leg["leg_status"] != "OPEN" or leg["action"] != "SELL":
                    continue
                opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
                bid = opt.get("bid", 0) or 0
                ask = opt.get("ask", 0) or 0
                mark = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else bid
                if mark > cheap_thresh:
                    all_cheap = False
                    break
            if all_cheap:
                return "CLOSE_TARGET", {"reason_detail": "all_short_legs_cheap_buyback"}

        adx_value = signals.get("adx_15") or signals.get("adx")
        if (adx_value is not None and adx_value > 28 and
                strategy_type == "SELL" and
                strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY")):
            return "CLOSE_ADX", {"adx": adx_value}

        portfolio_delta = self._compute_portfolio_delta()
        delta_limit = 0.20 * position["final_lots"] * self.config.lot_size
        if abs(portfolio_delta) > delta_limit:
            return "CLOSE_DELTA", {"portfolio_delta": portfolio_delta}

        try:
            hard_exit = datetime.strptime(
                position.get("hard_exit_time", "15:25"), "%H:%M"
            ).time()
        except Exception:
            hard_exit = self.config.hard_exit_time
        if current_time >= hard_exit:
            return "CLOSE_TIME", {}

        if strategy_type == "SELL":
            is_dir_delta = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
            delta_close  = 0.42 if is_dir_delta else 0.40
            delta_tight  = 0.35 if is_dir_delta else 0.32
            for leg in legs:
                if leg["action"] == "SELL" and leg["leg_status"] == "OPEN":
                    opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}
                    cur_delta = abs(opt.get("delta", leg["entry_delta"]) or 0)
                    if cur_delta > delta_close:
                        return "CLOSE_STOP", {"reason_detail": f"short_leg_delta_breach_{cur_delta:.3f}"}
                    if cur_delta > delta_tight and not position.get("stop_tightened_for_delta"):
                        self.db.update(
                            "positions",
                            {"stop_premium": (position.get("stop_premium") or 0) * 0.80,
                             "stop_tightened_for_delta": 1},
                            {"position_id": position["position_id"]},
                        )
                        self.logger.info(
                            f"Delta approaching limit ({cur_delta:.3f}) — "
                            f"tightening stop for {strategy_name}"
                        )
                        return "TIGHTEN_STOP", {}

        return "HOLD", {"current_premium": current_premium}

    def monitor_all_positions(self, signals: dict) -> None:
        final_regime = signals.get("final_regime")
        if final_regime == "EMERGENCY_EXIT":
            open_positions = self._get_open_positions()
            if open_positions:
                self.logger.warning(
                    f"REGIME ENGINE EMERGENCY_EXIT — force-closing "
                    f"{len(open_positions)} position(s)"
                )
                self.close_all_positions("EMERGENCY_EXIT")
            return

        if signals.get("vix_spike_detected"):
            open_positions = self._get_open_positions()
            if open_positions:
                legs_by_pos = {}
                for pos in open_positions:
                    pos_legs = self._get_position_legs(pos["position_id"])
                    net_vega = sum(
                        (-1 if l["action"] == "SELL" else 1) * (l.get("entry_vega") or 0)
                        for l in pos_legs if l["leg_status"] == "OPEN"
                    )
                    legs_by_pos[pos["position_id"]] = net_vega
                most_short_id = min(legs_by_pos, key=lambda k: legs_by_pos[k])
                for pos in open_positions:
                    if pos["position_id"] == most_short_id:
                        self.logger.warning(
                            f"VIX SPIKE: force-closing most short-vega position "
                            f"{pos['strategy_name']} {pos['position_id'][:16]}"
                        )
                        self.execute_close(pos, "CLOSE_VIX_SPIKE", {})
                        break

        for position in self._get_open_positions():
            action, context = self.monitor_position(position, signals)
            if action == "HOLD":
                continue
            if action == "TIGHTEN_STOP":
                self.logger.info(
                    f"Stop tightened for {position['strategy_name']} "
                    f"({position['position_id'][:16]})"
                )
                continue
            if action == "CLOSE_CALL_SIDE":
                self.close_one_side(position, "call", action)
            elif action == "CLOSE_PUT_SIDE":
                self.close_one_side(position, "put", action)
            elif action in ("CLOSE_STOP", "CLOSE_TARGET", "CLOSE_VWAP",
                             "CLOSE_ADX", "CLOSE_DELTA", "CLOSE_TIME",
                             "CLOSE_VIX_SPIKE", "EMERGENCY_EXIT"):
                self.execute_close(position, action, context)

    def execute_close(
        self, position: dict, reason: str, context: Optional[dict] = None
    ) -> None:
        legs      = self._get_position_legs(position["position_id"])
        open_legs = sorted(
            [l for l in legs if l["leg_status"] == "OPEN"],
            key=lambda l: 0 if l["action"] == "SELL" else 1
        )
        lots  = position["final_lots"]
        chain = self.market_engine.last_chain
        exit_legs_info = []
        exit_premium   = 0.0

        try:
            for leg in open_legs:
                fill = self.executor.execute_leg_exit(leg, chain, lots)
                exit_price = fill["fill_price"]
                self.db.update(
                    "position_legs",
                    {"exit_price": exit_price, "leg_status": "CLOSED",
                     "broker_order_id_exit": fill["order_id"]},
                    {"leg_id": leg["leg_id"]},
                )
                exit_legs_info.append({**leg, "exit_price": exit_price})
                exit_premium += exit_price if leg["action"] == "SELL" else -exit_price
        except Exception as e:
            self.logger.critical(
                f"EXIT EXECUTION FAILED for {position['position_id']}: {e}. "
                f"MANUAL INTERVENTION MAY BE REQUIRED."
            )
            return

        costs = self._compute_transaction_costs(exit_legs_info, lots, "EXIT")
        total_exit_costs_rs = costs["total_rupees"]
        C02 = self.config.lot_size

        if position["strategy_type"] == "SELL":
            gross_pnl_pts = (position["entry_credit"] or 0.0) - exit_premium
        else:
            gross_pnl_pts = exit_premium - (position["entry_debit"] or 0.0)

        gross_pnl_rs      = gross_pnl_pts * C02 * lots
        entry_costs_rs    = position.get("entry_costs_rupees") or 0.0
        total_costs_rs    = entry_costs_rs + total_exit_costs_rs
        net_pnl_rs        = gross_pnl_rs - total_costs_rs
        net_pnl_pts       = net_pnl_rs / (C02 * lots) if (C02 * lots) else 0.0
        current_capital   = self.market_engine.state.get("current_capital", self.config.starting_capital)
        net_pnl_pct       = (net_pnl_rs / current_capital * 100.0) if current_capital else 0.0
        result            = "WIN" if net_pnl_rs > 0 else ("LOSS" if net_pnl_rs < 0 else "BREAKEVEN")
        credit_or_debit   = position.get("entry_credit") or position.get("entry_debit") or 0.0
        profit_pct_credit = (net_pnl_pts / credit_or_debit * 100.0) if credit_or_debit else 0.0

        now = now_ist()
        entry_time = datetime.fromisoformat(position["entry_time"]) if position.get("entry_time") else now
        hold_minutes = (now - entry_time).total_seconds() / 60.0

        self.db.update("positions", {
            "status": "CLOSED",
            "exit_time": now.isoformat(),
            "exit_reason": reason,
            "exit_premium": exit_premium,
            "gross_pnl_rupees": gross_pnl_rs,
            "exit_costs_rupees": total_exit_costs_rs,
            "net_pnl_rupees": net_pnl_rs,
            "updated_at": now.isoformat(),
        }, {"position_id": position["position_id"]})

        self.db.insert("trade_exits", {
            "trade_id": position["position_id"],
            "position_id": position["position_id"],
            "strategy_name": position["strategy_name"],
            "exit_time": now.isoformat(),
            "hold_minutes": hold_minutes,
            "exit_reason": reason,
            "exit_spot": self.market_engine.state.get("prev_spot"),
            "exit_vix": self.market_engine.state.get("prev_vix"),
            "exit_adx": (context or {}).get("adx"),
            "exit_vwap_dist": (context or {}).get("vwap_dist"),
            "exit_legs_json": json.dumps(exit_legs_info, default=str),
            "exit_premium": exit_premium,
            "gross_pnl_pts": gross_pnl_pts,
            "gross_pnl_rupees": gross_pnl_rs,
            "exit_slippage": None,
            "exit_costs_pts": (costs["total_rupees"] / C02) if C02 else None,
            "exit_costs_rupees": total_exit_costs_rs,
            "total_costs_rupees": total_costs_rs,
            "net_pnl_pts": net_pnl_pts,
            "net_pnl_rupees": net_pnl_rs,
            "net_pnl_pct": net_pnl_pct,
            "result": result,
            "profit_pct_of_credit": profit_pct_credit,
            "created_at": now.isoformat(),
        })

        self._update_state_after_close(reason, net_pnl_rs)

        print_section(f"POSITION CLOSED: {position['strategy_name']} — {reason}")
        print_kv_table({
            "Position ID": position["position_id"],
            "Hold Time (min)": f"{hold_minutes:.1f}",
            "Exit Reason": reason,
            "Gross P&L (Rs)": gross_pnl_rs,
            "Total Costs (Rs)": total_costs_rs,
            "Net P&L (Rs)": net_pnl_rs,
            "Net P&L (%)": f"{net_pnl_pct:.3f}%",
            "Result": result,
        })
        self.logger.info(
            f"POSITION CLOSED: {position['strategy_name']} reason={reason} "
            f"net_pnl=Rs{net_pnl_rs:.2f} result={result}"
        )

    def close_one_side(self, position: dict, side: str, reason: str) -> None:
        legs = self._get_position_legs(position["position_id"])
        side_legs = sorted(
            [l for l in legs if l["option_type"] == side and l["leg_status"] == "OPEN"],
            key=lambda l: 0 if l["action"] == "SELL" else 1
        )
        lots  = position["final_lots"]
        chain = self.market_engine.last_chain

        for leg in side_legs:
            try:
                fill = self.executor.execute_leg_exit(leg, chain, lots)
                self.db.update(
                    "position_legs",
                    {"exit_price": fill["fill_price"], "leg_status": "CLOSED",
                     "broker_order_id_exit": fill["order_id"]},
                    {"leg_id": leg["leg_id"]},
                )
            except Exception as e:
                self.logger.error(
                    f"Failed to close {side} side of {position['position_id']}: {e}"
                )
                return

        self.logger.info(
            f"Closed {side} side of {position['strategy_name']} "
            f"({position['position_id'][:16]}): {reason}"
        )

        remaining = [l for l in self._get_position_legs(position["position_id"])
                     if l["leg_status"] == "OPEN"]
        if remaining:
            new_credit = (
                sum(l["entry_price"] for l in remaining if l["action"] == "SELL") -
                sum(l["entry_price"] for l in remaining if l["action"] == "BUY")
            )
            stop_mult = self.market_engine.state.get("stop_multiplier", 1.5)
            new_stop = new_credit * stop_mult if new_credit > 0 else position.get("stop_premium")
            self.db.update(
                "positions",
                {"entry_credit": new_credit, "stop_premium": new_stop,
                 "updated_at": now_ist().isoformat()},
                {"position_id": position["position_id"]},
            )
        else:
            self._finalize_partial_closes(position, reason)

    def _finalize_partial_closes(self, position: dict, reason: str) -> None:
        legs = self._get_position_legs(position["position_id"])
        lots = position["final_lots"]
        C02  = self.config.lot_size

        exit_premium = sum(
            (l["exit_price"] if l["action"] == "SELL" else -l["exit_price"])
            for l in legs if l.get("exit_price") is not None
        )

        if position["strategy_type"] == "SELL":
            gross_pnl_pts = (position["entry_credit"] or 0.0) - exit_premium
        else:
            gross_pnl_pts = exit_premium - (position["entry_debit"] or 0.0)

        costs = self._compute_transaction_costs(legs, lots, "EXIT")
        entry_costs_rs = position.get("entry_costs_rupees") or 0.0
        total_costs_rs = entry_costs_rs + costs["total_rupees"]
        gross_pnl_rs   = gross_pnl_pts * C02 * lots
        net_pnl_rs     = gross_pnl_rs - total_costs_rs
        net_pnl_pts    = net_pnl_rs / (C02 * lots) if (C02 * lots) else 0.0
        result         = "WIN" if net_pnl_rs > 0 else ("LOSS" if net_pnl_rs < 0 else "BREAKEVEN")
        current_capital = self.market_engine.state.get("current_capital", self.config.starting_capital)
        net_pnl_pct    = (net_pnl_rs / current_capital * 100.0) if current_capital else 0.0
        credit_or_debit = position.get("entry_credit") or position.get("entry_debit") or 0.0
        profit_pct     = (net_pnl_pts / credit_or_debit * 100.0) if credit_or_debit else 0.0

        now = now_ist()
        entry_time = datetime.fromisoformat(position["entry_time"]) if position.get("entry_time") else now
        hold_minutes = (now - entry_time).total_seconds() / 60.0

        self.db.update("positions", {
            "status": "CLOSED", "exit_time": now.isoformat(),
            "exit_reason": reason, "exit_premium": exit_premium,
            "gross_pnl_rupees": gross_pnl_rs,
            "exit_costs_rupees": costs["total_rupees"],
            "net_pnl_rupees": net_pnl_rs,
            "updated_at": now.isoformat(),
        }, {"position_id": position["position_id"]})

        self.db.insert("trade_exits", {
            "trade_id": position["position_id"],
            "position_id": position["position_id"],
            "strategy_name": position["strategy_name"],
            "exit_time": now.isoformat(),
            "hold_minutes": hold_minutes,
            "exit_reason": reason,
            "exit_spot": self.market_engine.state.get("prev_spot"),
            "exit_vix": self.market_engine.state.get("prev_vix"),
            "exit_adx": None, "exit_vwap_dist": None,
            "exit_legs_json": json.dumps(legs, default=str),
            "exit_premium": exit_premium,
            "gross_pnl_pts": gross_pnl_pts, "gross_pnl_rupees": gross_pnl_rs,
            "exit_slippage": None,
            "exit_costs_pts": (costs["total_rupees"] / C02) if C02 else None,
            "exit_costs_rupees": costs["total_rupees"],
            "total_costs_rupees": total_costs_rs,
            "net_pnl_pts": net_pnl_pts, "net_pnl_rupees": net_pnl_rs,
            "net_pnl_pct": net_pnl_pct, "result": result,
            "profit_pct_of_credit": profit_pct,
            "created_at": now.isoformat(),
        })

        self._update_state_after_close(reason, net_pnl_rs)
        self.logger.info(
            f"POSITION FULLY CLOSED (partial): {position['strategy_name']} "
            f"net_pnl=Rs{net_pnl_rs:.2f} result={result}"
        )

    def _update_state_after_close(self, reason: str, net_pnl_rs: float) -> None:
        state = self.market_engine.state
        state["daily_pnl"]       = state.get("daily_pnl", 0.0) + net_pnl_rs
        state["current_capital"] = state.get("current_capital", self.config.starting_capital) + net_pnl_rs

        if reason in ("CLOSE_STOP", "CLOSE_VIX_SPIKE", "EMERGENCY_EXIT"):
            if reason == "CLOSE_STOP":
                state["last_stop_time"]   = now_ist().isoformat()
                state["last_stop_reason"] = reason
                state["consecutive_stops"] = state.get("consecutive_stops", 0) + 1
                open_pos = self._get_open_positions()
                if not open_pos:
                    sig = self.market_engine.state
                    combo = (f"{sig.get('volatility_condition', '')}_"
                             f"{sig.get('trend_condition', '')}_"
                             f"{sig.get('direction', '')}")
                    state["last_stop_signal_combo"] = combo
            if state.get("consecutive_stops", 0) >= 2:
                state["daily_halted"] = True
                self.logger.warning("2 consecutive stops — halting trading for the day")
        elif reason in ("CLOSE_ADX", "CLOSE_VWAP", "CLOSE_DELTA"):
            state["last_stop_time"]   = now_ist().isoformat()
            state["last_stop_reason"] = reason
        elif reason in ("CLOSE_TARGET", "CLOSE_TIME", "HARD_EXIT_15:00",
                         "EOD_CLOSE", "SHUTDOWN_CLOSE", "STALE_PRIOR_DAY_CLOSE",
                         "SELF_TEST_CLEANUP"):
            state["consecutive_stops"] = 0

        if state.get("current_capital"):
            daily_loss_pct = max(0.0, -state["daily_pnl"]) / state["current_capital"]
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                state["daily_halted"] = True
                self.logger.warning(
                    f"DAILY LOSS LIMIT: {daily_loss_pct*100:.2f}% — halting trading"
                )

        self.db.update(
            "session_state",
            {"daily_pnl": state["daily_pnl"], "current_capital": state["current_capital"]},
            {"trading_date": today_ist().isoformat()},
        )
        self.market_engine._save_session_state()

    def close_all_positions(self, reason: str) -> None:
        for position in self._get_open_positions():
            self.execute_close(position, reason)


def _self_test() -> None:
    print_section("NIFTY ALGO — EXECUTION ENGINE SELF-TEST")
    config = load_config()
    db = Database(config.db_path)
    logger = setup_logging(db, config.log_dir)
    rate_limiter = RateLimiter(config.rate_limits)
    client = UpstoxClient(config, rate_limiter, db, logger)
    market_engine = MarketDataEngine(config, db, client, rate_limiter, logger)
    strategy_engine = StrategyEngine(config, db, market_engine, logger)
    execution_engine = ExecutionEngine(config, db, market_engine, client, logger)

    if not config.upstox_access_token or not client.validate_token():
        logger.warning("No valid Upstox token — cannot run live self-test.")
        db.close()
        return

    signals = market_engine.run_cycle()
    decision = strategy_engine.decide(signals)
    print_section(f"STRATEGY DECISION: {decision['action']}")

    if decision["action"] == "ENTER":
        position_id = execution_engine.process_entry_decision(decision, signals)
        if position_id:
            logger.info(f"Self-test opened paper position {position_id}. Monitoring once.")
            execution_engine.monitor_all_positions(signals)
            execution_engine.close_all_positions("SELF_TEST_CLEANUP")
    else:
        print(f"  Reason: {decision.get('reason', '')}")

    db.close()


if __name__ == "__main__":
    _self_test()