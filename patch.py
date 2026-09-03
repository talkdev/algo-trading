import ast
import shutil
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent

PATCHES = []

def add(filename, find, replace, label):
    PATCHES.append({"file": filename, "find": find, "replace": replace, "label": label})

add(
    "nifty_algo_core.py",
    '    if not log_dir.is_absolute():\n        log_dir = BASE_DIR / log_dir\n\n    return Config(',
    '    if not log_dir.is_absolute():\n        log_dir = BASE_DIR / log_dir\n\n    paper_trade_mode = _get_bool(env, "PAPER_TRADE_MODE", True)\n    live_rates_verified = _get_bool(env, "LIVE_RATES_VERIFIED", False)\n    if not paper_trade_mode and not live_rates_verified:\n        print("[SAFETY] PAPER_TRADE_MODE=false but LIVE_RATES_VERIFIED is not true in env.txt. Forcing paper trade mode until NIFTY lot size, STT rate, and weekly expiry weekday are manually verified against the current NSE contract specification.")\n        paper_trade_mode = True\n\n    return Config(',
    "core: add LIVE_RATES_VERIFIED safety gate before allowing live trading",
)

add(
    "nifty_algo_core.py",
    '        paper_trade_mode=_get_bool(env, "PAPER_TRADE_MODE", True),',
    "        paper_trade_mode=paper_trade_mode,",
    "core: use gated paper_trade_mode variable in Config construction",
)

add(
    "market_data_engine.py",
    "session_bars = self._fetch_last_n_complete_sessions_5min(n=3)",
    "session_bars = self._fetch_last_n_complete_sessions_5min(n=5)",
    "market_data: extend Parkinson RV lookback from 3 to 5 sessions",
)

add(
    "market_data_engine.py",
    "def _fetch_last_n_complete_sessions_5min(self, n: int = 3) -> dict:",
    "def _fetch_last_n_complete_sessions_5min(self, n: int = 5) -> dict:",
    "market_data: match default lookback parameter to 5 sessions",
)

add(
    "market_data_engine.py",
    "        if len(valid_bars) < 20:\n            return self._parkinson_fallback(vix), \"vix_proxy_insufficient_bars\"",
    "        if len(valid_bars) < 30:\n            return self._parkinson_fallback(vix), \"vix_proxy_insufficient_bars\"",
    "market_data: raise minimum valid bar count for Parkinson RV to 30",
)

add(
    "market_data_engine.py",
    '        if not GIFT_NIFTY_KEY:\n            self.state["gap_size"], self.state["gap_direction"] = "SMALL", "FLAT"\n            return',
    '        if not GIFT_NIFTY_KEY:\n            self.state["gap_size"], self.state["gap_direction"] = "SMALL", "FLAT"\n            if not self.config.paper_trade_mode:\n                self.state["daily_halted"] = True\n                self.logger.critical("GIFT_NIFTY_INSTRUMENT_KEY is not configured and PAPER_TRADE_MODE is false. Gap risk cannot be assessed before live trading. Halting trading for today.")\n            return',
    "market_data: make GIFT Nifty gap detection a hard requirement in live mode",
)

add(
    "market_data_engine.py",
    "        atm_iv = self.compute_atm_iv(chain, spot)\n        pcr = self.compute_pcr(chain)\n        put_iv, call_iv = self.compute_25d_ivs(chain)\n        skew = self.compute_skew_ratio(put_iv, call_iv)",
    "        atm_iv = self.compute_atm_iv(chain, spot)\n        pcr = self.compute_pcr(chain)\n        put_iv, call_iv = self.compute_25d_ivs(chain)\n        skew = self.compute_skew_ratio(put_iv, call_iv)\n\n        if atm_iv is not None and dte is not None and spot is not None:\n            expected_move = spot * atm_iv * ((max(dte, 1) / 365.0) ** 0.5)\n            computed_wing = int(round((expected_move * 0.60) / self.config.nifty_strike_step) * self.config.nifty_strike_step)\n            self.state[\"wing_width\"] = max(100, min(computed_wing, 400))",
    "market_data: replace flat VIX-bucket wing width with expected-move-scaled wing",
)

add(
    "market_data_engine.py",
    'dow_stop = {"MONDAY": 2.5, "TUESDAY": 1.5, "WEDNESDAY": 2.0, "THURSDAY": 2.0, "FRIDAY": 1.8}',
    'dow_stop = {"MONDAY": 2.2, "TUESDAY": 1.4, "WEDNESDAY": 1.8, "THURSDAY": 1.9, "FRIDAY": 1.6}',
    "market_data: tighten day-of-week stop multipliers for elevated 2026 NIFTY vol",
)

add(
    "strategy_engine.py",
    'MIN_CREDIT_MULTIPLIER_BY_REGIME = {"SUPPRESSED": 1.0, "LOW": 1.2, "NORMAL": 1.0,\n                                    "ELEVATED": 0.9, "HIGH": 0.8}',
    'MIN_CREDIT_MULTIPLIER_BY_REGIME = {"SUPPRESSED": 1.0, "LOW": 1.1, "NORMAL": 1.0,\n                                    "ELEVATED": 1.15, "HIGH": 1.3}',
    "strategy: demand more relative credit in high-vol regimes, not less",
)

add(
    "strategy_engine.py",
    'MIN_CREDITS = {\n    "IRON_BUTTERFLY": 25, "IRON_CONDOR": 15,\n    "BULL_PUT_SPREAD": 12, "BEAR_CALL_SPREAD": 10, "POST_EVENT_STRADDLE": 0,\n}',
    'MIN_CREDITS = {\n    "IRON_BUTTERFLY": 25, "IRON_CONDOR": 15,\n    "BULL_PUT_SPREAD": 12, "BEAR_CALL_SPREAD": 10, "POST_EVENT_STRADDLE": 30,\n}',
    "strategy: raise POST_EVENT_STRADDLE minimum credit now that it is defined-risk",
)

add(
    "strategy_engine.py",
    '            min_credit = min_credits.get(strategy_name, 12) * self._get_min_credit_multiplier(s)\n\n            if net_credit < min_credit:\n                return {"valid": False, "reason": f"net_credit_{net_credit:.2f}pts_below_minimum_{min_credit:.2f}pts"}',
    '            static_floor = min_credits.get(strategy_name, 12) * self._get_min_credit_multiplier(s)\n            cost_floor = total_costs_pts_per_lot * 3.0\n            min_credit = max(static_floor, cost_floor)\n\n            if net_credit < min_credit:\n                return {"valid": False, "reason": f"net_credit_{net_credit:.2f}pts_below_minimum_{min_credit:.2f}pts"}',
    "strategy: derive credit floor from live cost stack, not only a static points figure",
)

add(
    "strategy_engine.py",
    '            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY"):\n                actual_wing_pts = (abs(validated_legs[2]["strike"] - validated_legs[0]["strike"])\n                                    if strategy_name == "IRON_CONDOR" else 100)\n                if actual_wing_pts > 0 and (net_credit / actual_wing_pts) < 0.10:\n                    return {"valid": False, "reason": f"credit_ratio_below_0.10_insufficient_edge"}',
    '            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY", "POST_EVENT_STRADDLE"):\n                actual_wing_pts = abs(validated_legs[2]["strike"] - validated_legs[0]["strike"])\n                if actual_wing_pts > 0 and (net_credit / actual_wing_pts) < 0.10:\n                    return {"valid": False, "reason": f"credit_ratio_below_0.10_insufficient_edge"}',
    "strategy: apply credit/width ratio check to POST_EVENT_STRADDLE and use its real wing",
)

add(
    "strategy_engine.py",
    '        base_lots = max(1, int(max_risk_per_trade / max_loss_per_lot))\n        final_lots = max(1, int(base_lots * size_mult))\n        final_lots = min(final_lots, LOT_CAPS_BY_DAY.get(state.get("day_label"), 3))',
    '        raw_base_lots = max_risk_per_trade / max_loss_per_lot\n        base_lots = max(1, int(raw_base_lots))\n        intended_lots = raw_base_lots * size_mult\n        if intended_lots < 0.5:\n            return {"valid": False, "reason": f"intended_lots_{intended_lots:.2f}_below_minimum_viable_0.5_size_throttled_to_no_trade"}\n        final_lots = max(1, int(base_lots * size_mult))\n        capital_scale = max(1, int(current_capital / self.config.starting_capital))\n        day_cap = LOT_CAPS_BY_DAY.get(state.get("day_label"), 3) * capital_scale\n        final_lots = min(final_lots, day_cap)',
    "strategy: reject sub-half-lot sizing as NO_TRADE and scale lot caps with capital",
)

add(
    "strategy_engine.py",
    '        if strategy_type == "SELL":\n            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):\n                estimated_margin_per_lot = (actual_wing_pts or wing) * C02 * 1.15\n            else:\n                estimated_margin_per_lot = spot * C02 * 0.11',
    '        if strategy_type == "SELL":\n            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "POST_EVENT_STRADDLE"):\n                estimated_margin_per_lot = (actual_wing_pts or wing) * C02 * 1.15\n            else:\n                estimated_margin_per_lot = spot * C02 * 0.11',
    "strategy: use defined-risk margin formula for POST_EVENT_STRADDLE",
)

add(
    "strategy_engine.py",
    '        if strategy_name in ("LONG_STRADDLE", "POST_EVENT_STRADDLE"):\n            atm = round(spot / step) * step\n            if atm not in chain:\n                atm = min(chain.keys(), key=lambda k: abs(k - spot))\n            action = "BUY" if strategy_name == "LONG_STRADDLE" else "SELL"\n            return [\n                {"strike": atm, "option_type": "call", "action": action},\n                {"strike": atm, "option_type": "put", "action": action},\n            ], None',
    '        if strategy_name == "LONG_STRADDLE":\n            atm = round(spot / step) * step\n            if atm not in chain:\n                atm = min(chain.keys(), key=lambda k: abs(k - spot))\n            return [\n                {"strike": atm, "option_type": "call", "action": "BUY"},\n                {"strike": atm, "option_type": "put", "action": "BUY"},\n            ], None\n\n        if strategy_name == "POST_EVENT_STRADDLE":\n            atm = round(spot / step) * step\n            if atm not in chain:\n                atm = min(chain.keys(), key=lambda k: abs(k - spot))\n            event_wing = max(wing, 150)\n            long_call = atm + event_wing\n            long_put = atm - event_wing\n            if long_call not in chain:\n                long_call = min(chain.keys(), key=lambda k: abs(k - (atm + event_wing)))\n            if long_put not in chain:\n                long_put = min(chain.keys(), key=lambda k: abs(k - (atm - event_wing)))\n            return [\n                {"strike": atm, "option_type": "call", "action": "SELL"},\n                {"strike": atm, "option_type": "put", "action": "SELL"},\n                {"strike": long_call, "option_type": "call", "action": "BUY"},\n                {"strike": long_put, "option_type": "put", "action": "BUY"},\n            ], None',
    "strategy: replace naked POST_EVENT_STRADDLE with a defined-risk winged structure",
)

add(
    "strategy_engine.py",
    '        if strategy_name == "IRON_BUTTERFLY":\n            atm = round(spot / step) * step\n            if atm not in chain:\n                atm = min(chain.keys(), key=lambda k: abs(k - spot))\n            long_call, long_put = atm + 100, atm - 100\n            if long_call not in chain:\n                long_call = min(chain.keys(), key=lambda k: abs(k - (atm + 100)))\n            if long_put not in chain:\n                long_put = min(chain.keys(), key=lambda k: abs(k - (atm - 100)))',
    '        if strategy_name == "IRON_BUTTERFLY":\n            atm = round(spot / step) * step\n            if atm not in chain:\n                atm = min(chain.keys(), key=lambda k: abs(k - spot))\n            butterfly_wing = max(wing, 100)\n            long_call, long_put = atm + butterfly_wing, atm - butterfly_wing\n            if long_call not in chain:\n                long_call = min(chain.keys(), key=lambda k: abs(k - (atm + butterfly_wing)))\n            if long_put not in chain:\n                long_put = min(chain.keys(), key=lambda k: abs(k - (atm - butterfly_wing)))',
    "strategy: size Iron Butterfly wings from expected move instead of a fixed 100pts",
)

add(
    "strategy_engine.py",
    '        ltp = opt.get("ltp", 0)\n        if ltp > 0:\n            self.logger.warning(f"Using LTP for {strike} {opt_type} — no bid/ask available")\n            return ltp\n        return 0.0\n\n    def _build_legs_spec(self, strategy_name: str, chain: dict, spot: float,\n                           wing: float) -> tuple[Optional[list], Optional[str]]:',
    '        ltp = opt.get("ltp", 0)\n        if ltp > 0:\n            self.logger.warning(f"Using LTP for {strike} {opt_type} — no bid/ask available")\n            return ltp\n        return 0.0\n\n    def _dte_adjusted_short_delta(self, base_delta: float, actual_dte) -> float:\n        if actual_dte is None:\n            return base_delta\n        if actual_dte <= 0:\n            return max(0.12, base_delta - 0.10)\n        if actual_dte == 1:\n            return max(0.15, base_delta - 0.07)\n        if actual_dte == 2:\n            return max(0.18, base_delta - 0.05)\n        if actual_dte == 3:\n            return max(0.20, base_delta - 0.03)\n        return base_delta\n\n    def _build_legs_spec(self, strategy_name: str, chain: dict, spot: float,\n                           wing: float, actual_dte: Optional[int] = None) -> tuple[Optional[list], Optional[str]]:',
    "strategy: add DTE-adjusted delta helper and extend _build_legs_spec signature",
)

add(
    "strategy_engine.py",
    '        if strategy_name == "IRON_CONDOR":\n            short_call, _ = self._find_strike_by_delta(chain, "call", 0.25)\n            short_put, _ = self._find_strike_by_delta(chain, "put", 0.25)',
    '        if strategy_name == "IRON_CONDOR":\n            target_delta = self._dte_adjusted_short_delta(0.25, actual_dte)\n            short_call, _ = self._find_strike_by_delta(chain, "call", target_delta)\n            short_put, _ = self._find_strike_by_delta(chain, "put", target_delta)',
    "strategy: DTE-adjust Iron Condor short-strike delta targeting",
)

add(
    "strategy_engine.py",
    '        if strategy_name == "BULL_PUT_SPREAD":\n            short_put, _ = self._find_strike_by_delta(chain, "put", 0.25)',
    '        if strategy_name == "BULL_PUT_SPREAD":\n            target_delta = self._dte_adjusted_short_delta(0.25, actual_dte)\n            short_put, _ = self._find_strike_by_delta(chain, "put", target_delta)',
    "strategy: DTE-adjust Bull Put Spread short-strike delta targeting",
)

add(
    "strategy_engine.py",
    '        if strategy_name == "BEAR_CALL_SPREAD":\n            short_call, _ = self._find_strike_by_delta(chain, "call", 0.25)',
    '        if strategy_name == "BEAR_CALL_SPREAD":\n            target_delta = self._dte_adjusted_short_delta(0.25, actual_dte)\n            short_call, _ = self._find_strike_by_delta(chain, "call", target_delta)',
    "strategy: DTE-adjust Bear Call Spread short-strike delta targeting",
)

add(
    "strategy_engine.py",
    '        legs_spec, err = self._build_legs_spec(strategy_name, chain, spot, wing)',
    '        legs_spec, err = self._build_legs_spec(strategy_name, chain, spot, wing, actual_dte)',
    "strategy: pass actual_dte into leg-spec builder",
)

add(
    "strategy_engine.py",
    '    def _validate_strike(self, chain: dict, strike: float, opt_type: str) -> tuple[bool, str]:\n        if strike not in chain:\n            return False, f"strike_{strike}_not_in_chain"\n        opt = chain[strike].get(opt_type, {})\n        bid, ask, oi, ltp = opt.get("bid", 0), opt.get("ask", 0), opt.get("oi", 0), opt.get("ltp", 0)\n        if bid <= 0 and ask <= 0:\n            return False, f"strike_{strike}_{opt_type}_no_bid_ask"\n        if oi < 100:\n            return False, f"strike_{strike}_{opt_type}_oi_{oi}_below_100"\n        if bid > 0 and ask > 0:\n            mid = (bid + ask) / 2.0\n            if mid > 0 and (ask - bid) / mid > 0.30:\n                return False, f"strike_{strike}_{opt_type}_spread_too_wide"',
    '    def _validate_strike(self, chain: dict, strike: float, opt_type: str, action: str = "SELL") -> tuple[bool, str]:\n        if strike not in chain:\n            return False, f"strike_{strike}_not_in_chain"\n        opt = chain[strike].get(opt_type, {})\n        bid, ask, oi, ltp = opt.get("bid", 0), opt.get("ask", 0), opt.get("oi", 0), opt.get("ltp", 0)\n        if bid <= 0 and ask <= 0:\n            return False, f"strike_{strike}_{opt_type}_no_bid_ask"\n        min_oi = 500 if action == "SELL" else 100\n        max_spread_pct = 0.15 if action == "SELL" else 0.30\n        if oi < min_oi:\n            return False, f"strike_{strike}_{opt_type}_oi_{oi}_below_{min_oi}"\n        if bid > 0 and ask > 0:\n            mid = (bid + ask) / 2.0\n            if mid > 0 and (ask - bid) / mid > max_spread_pct:\n                return False, f"strike_{strike}_{opt_type}_spread_too_wide"',
    "strategy: tighten liquidity gates specifically for short (SELL) legs",
)

add(
    "strategy_engine.py",
    '            ok, reason = self._validate_strike(chain, strike, opt_type)',
    '            ok, reason = self._validate_strike(chain, strike, opt_type, action)',
    "strategy: pass leg action into tightened liquidity validation",
)

add(
    "execution_engine.py",
    '                total_delta += sign * live_delta * leg["qty"]\n        return total_delta\n\n    def _compute_transaction_costs(self, legs: list, lots: int, action: str) -> dict:',
    '                total_delta += sign * live_delta * leg["qty"]\n        return total_delta\n\n    def _compute_portfolio_vega_gamma(self) -> tuple[float, float]:\n        chain = self.market_engine.last_chain\n        total_vega = 0.0\n        total_gamma = 0.0\n        for pos in self._get_open_positions():\n            for leg in self._get_position_legs(pos["position_id"]):\n                if leg["leg_status"] != "OPEN":\n                    continue\n                opt = chain.get(leg["strike"], {}).get(leg["option_type"], {}) if chain else {}\n                live_vega = opt.get("vega", leg["entry_vega"]) or 0\n                live_gamma = opt.get("gamma", leg["entry_gamma"]) or 0\n                sign = -1 if leg["action"] == "SELL" else 1\n                total_vega += sign * live_vega * leg["qty"]\n                total_gamma += sign * live_gamma * leg["qty"]\n        return total_vega, total_gamma\n\n    def _compute_transaction_costs(self, legs: list, lots: int, action: str) -> dict:',
    "execution: add portfolio-level vega/gamma aggregation",
)

add(
    "execution_engine.py",
    '            if not found:\n                return "NO_GO", {"reason": "portfolio_delta_exceeds_limit_cannot_reduce_further"}\n\n        # Liquidity re-check at execution time\n        chain = self.market_engine.last_chain',
    '            if not found:\n                return "NO_GO", {"reason": "portfolio_delta_exceeds_limit_cannot_reduce_further"}\n\n        new_vega = sum((-1 if leg["action"] == "SELL" else 1) * (leg.get("vega") or 0) * final_lots * C02 for leg in params["legs"])\n        new_gamma = sum((-1 if leg["action"] == "SELL" else 1) * (leg.get("gamma") or 0) * final_lots * C02 for leg in params["legs"])\n        current_vega, current_gamma = self._compute_portfolio_vega_gamma()\n        post_trade_vega = current_vega + new_vega\n        post_trade_gamma = current_gamma + new_gamma\n        vega_limit = 2000.0 * final_lots\n        gamma_limit = 50.0 * final_lots\n        if abs(post_trade_vega) > vega_limit:\n            return "NO_GO", {"reason": f"portfolio_vega_{post_trade_vega:.1f}_exceeds_limit_{vega_limit:.1f}"}\n        if abs(post_trade_gamma) > gamma_limit:\n            return "NO_GO", {"reason": f"portfolio_gamma_{post_trade_gamma:.4f}_exceeds_limit_{gamma_limit:.4f}"}\n\n        chain = self.market_engine.last_chain',
    "execution: enforce portfolio-level vega/gamma limits in pre-trade validation",
)

add(
    "execution_engine.py",
    '        try:\n            for leg in params["legs"]:\n                fill = self.executor.execute_leg_entry(leg, lots, chain)\n                filled_legs.append({**leg, "fill": fill})',
    '        try:\n            entry_order = sorted(params["legs"], key=lambda l: 0 if l["action"] == "BUY" else 1)\n            for leg in entry_order:\n                fill = self.executor.execute_leg_entry(leg, lots, chain)\n                filled_legs.append({**leg, "fill": fill})',
    "execution: fill protective BUY legs before SELL legs on entry for margin benefit",
)

add(
    "execution_engine.py",
    '        legs = self._get_position_legs(position["position_id"])\n        open_legs = [l for l in legs if l["leg_status"] == "OPEN"]\n        lots = position["final_lots"]\n        chain = self.market_engine.last_chain\n\n        exit_legs_info = []',
    '        legs = self._get_position_legs(position["position_id"])\n        open_legs = [l for l in legs if l["leg_status"] == "OPEN"]\n        open_legs = sorted(open_legs, key=lambda l: 0 if l["action"] == "SELL" else 1)\n        lots = position["final_lots"]\n        chain = self.market_engine.last_chain\n\n        exit_legs_info = []',
    "execution: buy back short legs before releasing long hedges on full exit",
)

add(
    "execution_engine.py",
    '    def close_one_side(self, position: dict, side: str, reason: str) -> None:\n        legs = self._get_position_legs(position["position_id"])\n        side_legs = [l for l in legs if l["option_type"] == side and l["leg_status"] == "OPEN"]\n        lots = position["final_lots"]\n        chain = self.market_engine.last_chain',
    '    def close_one_side(self, position: dict, side: str, reason: str) -> None:\n        legs = self._get_position_legs(position["position_id"])\n        side_legs = [l for l in legs if l["option_type"] == side and l["leg_status"] == "OPEN"]\n        side_legs = sorted(side_legs, key=lambda l: 0 if l["action"] == "SELL" else 1)\n        lots = position["final_lots"]\n        chain = self.market_engine.last_chain',
    "execution: apply same SELL-before-BUY ordering to partial (one-side) exits",
)

add(
    "execution_engine.py",
    '        self.logger.warning(f"Using fallback price for order {order_id} (could not confirm fill price)")\n        return fallback\n\n    def execute_leg_entry(self, leg: dict, lots: int, chain: dict) -> dict:',
    '        self.logger.warning(f"Using fallback price for order {order_id} (could not confirm fill price)")\n        return fallback\n\n    def _aggressive_limit_price(self, chain: dict, strike: float, opt_type: str, transaction_type: str, fallback: float) -> float:\n        opt = chain.get(strike, {}).get(opt_type, {}) if chain else {}\n        bid, ask = opt.get("bid", 0) or 0, opt.get("ask", 0) or 0\n        if transaction_type == "BUY":\n            base = ask if ask > 0 else fallback\n            price = base * 1.05\n        else:\n            base = bid if bid > 0 else fallback\n            price = base * 0.95\n        tick = 0.05\n        return round(round(price / tick) * tick, 2)\n\n    def execute_leg_entry(self, leg: dict, lots: int, chain: dict) -> dict:',
    "execution: add aggressive marketable-limit price calculator for live orders",
)

add(
    "execution_engine.py",
    '        transaction_type = "SELL" if leg["action"] == "SELL" else "BUY"\n        result = self.client.place_order(\n            instrument_token=instrument_key, quantity=qty, transaction_type=transaction_type,\n            order_type="MARKET", product="I",\n        )\n        order_id = result.get("order_id", "")\n        self.logger.info(f"[LIVE] ENTRY ORDER PLACED: {transaction_type} {leg[\'option_type\'].upper()} "\n                          f"{leg[\'strike\']:.0f} x{lots} order_id={order_id}")\n        fill_price = self._get_fill_price(order_id, fallback=leg["exec_price"])',
    '        transaction_type = "SELL" if leg["action"] == "SELL" else "BUY"\n        limit_price = self._aggressive_limit_price(chain, leg["strike"], leg["option_type"], transaction_type, leg["exec_price"])\n        result = self.client.place_order(\n            instrument_token=instrument_key, quantity=qty, transaction_type=transaction_type,\n            order_type="LIMIT", price=limit_price, product="I",\n        )\n        order_id = result.get("order_id", "")\n        self.logger.info(f"[LIVE] ENTRY ORDER PLACED: {transaction_type} {leg[\'option_type\'].upper()} "\n                          f"{leg[\'strike\']:.0f} x{lots} order_id={order_id}")\n        fill_price = self._get_fill_price(order_id, fallback=leg["exec_price"])',
    "execution: route live entry orders as aggressive LIMIT instead of MARKET",
)

add(
    "execution_engine.py",
    '        transaction_type = "BUY" if leg["action"] == "SELL" else "SELL"\n        result = self.client.place_order(\n            instrument_token=instrument_key, quantity=qty, transaction_type=transaction_type,\n            order_type="MARKET", product="I",\n        )\n        order_id = result.get("order_id", "")\n        self.logger.info(f"[LIVE] EXIT ORDER PLACED: {transaction_type} {leg[\'option_type\'].upper()} "\n                          f"{leg[\'strike\']:.0f} x{lots} order_id={order_id}")\n        fill_price = self._get_fill_price(order_id, fallback=leg.get("entry_price", 0) or 0)',
    '        transaction_type = "BUY" if leg["action"] == "SELL" else "SELL"\n        limit_price = self._aggressive_limit_price(chain, leg["strike"], leg["option_type"], transaction_type, leg.get("entry_price", 0) or 0)\n        result = self.client.place_order(\n            instrument_token=instrument_key, quantity=qty, transaction_type=transaction_type,\n            order_type="LIMIT", price=limit_price, product="I",\n        )\n        order_id = result.get("order_id", "")\n        self.logger.info(f"[LIVE] EXIT ORDER PLACED: {transaction_type} {leg[\'option_type\'].upper()} "\n                          f"{leg[\'strike\']:.0f} x{lots} order_id={order_id}")\n        fill_price = self._get_fill_price(order_id, fallback=leg.get("entry_price", 0) or 0)',
    "execution: route live exit orders as aggressive LIMIT instead of MARKET",
)


def apply_patches_to_file(filepath, file_patches, report):
    if not filepath.exists():
        report["missing"].append(str(filepath))
        return
    original = filepath.read_text(encoding="utf-8")
    content = original
    applied = []
    skipped = []
    for p in file_patches:
        count = content.count(p["find"])
        if count == 1:
            content = content.replace(p["find"], p["replace"], 1)
            applied.append(p["label"])
        elif count == 0:
            skipped.append((p["label"], "anchor not found"))
        else:
            skipped.append((p["label"], f"anchor found {count} times, expected 1"))
    if not applied:
        report["unchanged"].append(str(filepath))
        for label, why in skipped:
            report["skipped"].append((str(filepath), label, why))
        return
    try:
        ast.parse(content)
    except SyntaxError as e:
        report["syntax_errors"].append((str(filepath), str(e)))
        for label, why in skipped:
            report["skipped"].append((str(filepath), label, why))
        return
    backup_path = filepath.with_suffix(filepath.suffix + ".bak_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(filepath, backup_path)
    filepath.write_text(content, encoding="utf-8")
    report["patched"].append((str(filepath), str(backup_path), applied))
    for label, why in skipped:
        report["skipped"].append((str(filepath), label, why))


def main():
    report = {"patched": [], "skipped": [], "unchanged": [], "missing": [], "syntax_errors": []}
    files = {}
    for p in PATCHES:
        files.setdefault(p["file"], []).append(p)
    for filename, file_patches in files.items():
        apply_patches_to_file(BASE_DIR / filename, file_patches, report)

    print("=" * 78)
    print("NIFTY ALGO ENGINE PATCH REPORT")
    print("=" * 78)

    if report["patched"]:
        print("\nSUCCESSFULLY PATCHED:")
        for filepath, backup, applied in report["patched"]:
            print(f"  {filepath}  (backup: {backup})")
            for label in applied:
                print(f"    - {label}")

    if report["skipped"]:
        print("\nSKIPPED (anchor mismatch, file left untouched for that fix):")
        for filepath, label, why in report["skipped"]:
            print(f"  {filepath} :: {label} :: {why}")

    if report["syntax_errors"]:
        print("\nABORTED DUE TO SYNTAX ERROR (no changes written to this file):")
        for filepath, err in report["syntax_errors"]:
            print(f"  {filepath} :: {err}")

    if report["missing"]:
        print("\nFILE NOT FOUND (check patch.py is in the same directory as the engine files):")
        for filepath in report["missing"]:
            print(f"  {filepath}")

    if report["unchanged"]:
        print("\nNO PATCHES APPLIED TO:")
        for filepath in report["unchanged"]:
            print(f"  {filepath}")

    print("\n" + "=" * 78)
    print("Run each file's own self-test (python market_data_engine.py, etc.) before live use.")
    print("Set LIVE_RATES_VERIFIED=true in env.txt only after manually confirming current")
    print("NIFTY lot size, STT rate, and weekly expiry weekday against the live NSE contract")
    print("specification and your broker's contract note.")
    print("=" * 78)


if __name__ == "__main__":
    main()