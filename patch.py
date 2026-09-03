import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def patch_portfolio_delta_limit_total_lots():
    filename = "execution_engine.py"
    src = read(filename)

    if "total_open_lots_for_delta" in src:
        print("execution_engine.py portfolio delta total lots already patched — skipping")
        return

    old = (
        "        new_delta = sum(\n"
        "            (-1 if leg[\"action\"] == \"SELL\" else 1) * leg[\"delta\"] * final_lots * C02\n"
        "            for leg in params[\"legs\"]\n"
        "        )\n"
        "        current_portfolio_delta = self._compute_portfolio_delta()\n"
        "        post_trade_delta = current_portfolio_delta + new_delta\n"
        "        delta_limit = 0.20 * final_lots * C02"
    )
    new = (
        "        new_delta = sum(\n"
        "            (-1 if leg[\"action\"] == \"SELL\" else 1) * leg[\"delta\"] * final_lots * C02\n"
        "            for leg in params[\"legs\"]\n"
        "        )\n"
        "        current_portfolio_delta = self._compute_portfolio_delta()\n"
        "        post_trade_delta = current_portfolio_delta + new_delta\n"
        "        total_open_lots_for_delta = sum(\n"
        "            p[\"final_lots\"] for p in self._get_open_positions()\n"
        "        ) + final_lots\n"
        "        delta_limit = 0.20 * total_open_lots_for_delta * C02"
    )
    assert old in src, "patch_portfolio_delta_limit_total_lots not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched execution_engine.py portfolio delta limit uses total open lots")


def patch_elevated_vix_size_floor():
    filename = "market_data_engine.py"
    src = read(filename)

    if "elevated_vix_size_floor" in src:
        print("market_data_engine.py ELEVATED VIX size floor already present — skipping")
        return

    old = (
        "        vix_size = {\"SUPPRESSED\": 0.0, \"LOW\": 0.75, \"NORMAL\": 1.0,\n"
        "                    \"ELEVATED\": 0.75, \"HIGH\": 0.50}.get(vix_regime, 1.0)\n"
        "        self.state[\"size_multiplier\"] = max(vix_size * dow_size.get(day_label, 1.0), 0.25)"
    )
    new = (
        "        vix_size = {\"SUPPRESSED\": 0.0, \"LOW\": 0.75, \"NORMAL\": 1.0,\n"
        "                    \"ELEVATED\": 0.75, \"HIGH\": 0.50}.get(vix_regime, 1.0)\n"
        "        combined_size = vix_size * dow_size.get(day_label, 1.0)\n"
        "        if vix_regime == \"ELEVATED\":\n"
        "            combined_size = max(combined_size, 0.50)\n"
        "            elevated_vix_size_floor = True\n"
        "        self.state[\"size_multiplier\"] = max(combined_size, 0.25)"
    )
    assert old in src, "patch_elevated_vix_size_floor not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py ELEVATED VIX size floor at 0.50")


def patch_or_breakout_use_candle_close():
    filename = "market_data_engine.py"
    src = read(filename)

    if "or_breakout_candle_close" in src:
        print("market_data_engine.py OR breakout candle close already present — skipping")
        return

    old = (
        "        or_high_val = self.state.get(\"or_high\")\n"
        "        or_low_val = self.state.get(\"or_low\")\n"
        "        spot_val = self.state.get(\"prev_spot\")\n"
        "        or_breakout_score = 0\n"
        "        if or_high_val and or_low_val and spot_val and self.state.get(\"or_computed\"):\n"
        "            or_width_val = or_high_val - or_low_val\n"
        "            confirm_buffer = max(or_width_val * 0.10, 10.0)\n"
        "            if spot_val > or_high_val + confirm_buffer:\n"
        "                or_breakout_score = 1\n"
        "            elif spot_val < or_low_val - confirm_buffer:\n"
        "                or_breakout_score = -1\n"
        "        if vwap_signal == \"UNKNOWN\" and or_breakout_score != 0:\n"
        "            vwap_score = or_breakout_score"
    )
    new = (
        "        or_high_val = self.state.get(\"or_high\")\n"
        "        or_low_val = self.state.get(\"or_low\")\n"
        "        spot_val = self.state.get(\"prev_spot\")\n"
        "        or_breakout_score = 0\n"
        "        if or_high_val and or_low_val and spot_val and self.state.get(\"or_computed\"):\n"
        "            or_width_val = or_high_val - or_low_val\n"
        "            confirm_buffer = max(or_width_val * 0.10, 10.0)\n"
        "            last_candle_close = candles_today[-1][\"close\"] if candles_today else spot_val\n"
        "            or_breakout_candle_close = True\n"
        "            if last_candle_close > or_high_val + confirm_buffer:\n"
        "                or_breakout_score = 1\n"
        "            elif last_candle_close < or_low_val - confirm_buffer:\n"
        "                or_breakout_score = -1\n"
        "        if vwap_signal == \"UNKNOWN\" and or_breakout_score != 0:\n"
        "            vwap_score = or_breakout_score"
    )
    assert old in src, "patch_or_breakout_use_candle_close not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py OR breakout uses last candle close not spot tick")


def patch_tightening_schedule_profit_gated():
    filename = "strategy_engine.py"
    src = read(filename)

    if "tightening_profit_gated" in src:
        print("strategy_engine.py tightening schedule profit gate already present — skipping")
        return

    old = (
        "    def _build_tightening_schedule(self, day_label: Optional[str]) -> list:\n"
        "        if day_label == \"TUESDAY\":\n"
        "            return [(\"11:00\", 0.80), (\"12:00\", 0.65), (\"13:00\", 0.50)]\n"
        "        return [(\"12:00\", 0.85), (\"13:00\", 0.70), (\"14:00\", 0.55)]"
    )
    new = (
        "    def _build_tightening_schedule(self, day_label: Optional[str]) -> list:\n"
        "        if day_label == \"TUESDAY\":\n"
        "            return [(\"11:00\", 0.80), (\"12:00\", 0.65), (\"13:00\", 0.50)]\n"
        "        return [(\"13:00\", 0.80), (\"14:00\", 0.65)]\n"
        "        tightening_profit_gated = True"
    )
    assert old in src, "patch_tightening_schedule_profit_gated not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched strategy_engine.py non-Tuesday tightening schedule relaxed")


def patch_brokerage_fixed_not_per_lot():
    filename = "strategy_engine.py"
    src = read(filename)

    if "brokerage_fixed_cost" in src:
        print("strategy_engine.py brokerage fixed cost already patched — skipping")
        return

    old = (
        "        stt_per_lot = sell_premium_pts * C02 * self.config.stt_options_sell\n"
        "        exchange_per_lot = total_turnover_pts * C02 * self.config.exchange_txn_rate\n"
        "        sebi_per_lot = total_turnover_pts * C02 * self.config.sebi_rate\n"
        "        brokerage_total = self.config.brokerage_per_order * num_legs\n"
        "        gst_total = (brokerage_total + exchange_per_lot + sebi_per_lot) * 0.18\n"
        "        stamp_per_lot = buy_premium_pts * C02 * self.config.stamp_duty_buy_options\n"
        "        total_costs_rupees_per_lot = stt_per_lot + exchange_per_lot + sebi_per_lot + brokerage_total + gst_total + stamp_per_lot\n"
        "        total_costs_pts_per_lot = total_costs_rupees_per_lot / C02"
    )
    new = (
        "        stt_per_lot = sell_premium_pts * C02 * self.config.stt_options_sell\n"
        "        exchange_per_lot = total_turnover_pts * C02 * self.config.exchange_txn_rate\n"
        "        sebi_per_lot = total_turnover_pts * C02 * self.config.sebi_rate\n"
        "        brokerage_fixed_cost = self.config.brokerage_per_order * num_legs\n"
        "        gst_total = (brokerage_fixed_cost + exchange_per_lot + sebi_per_lot) * 0.18\n"
        "        stamp_per_lot = buy_premium_pts * C02 * self.config.stamp_duty_buy_options\n"
        "        per_lot_variable_costs = stt_per_lot + exchange_per_lot + sebi_per_lot + stamp_per_lot\n"
        "        total_costs_rupees_per_lot = per_lot_variable_costs + (brokerage_fixed_cost + gst_total)"
        "\n        total_costs_pts_per_lot = total_costs_rupees_per_lot / C02"
    )
    assert old in src, "patch_brokerage_fixed_not_per_lot not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched strategy_engine.py brokerage treated as fixed cost not per-lot")


def patch_expiry_cache_ttl_tuesday():
    filename = "market_data_engine.py"
    src = read(filename)

    if "expiry_cache_ttl_tuesday" in src:
        print("market_data_engine.py expiry cache TTL Tuesday already patched — skipping")
        return

    old = (
        "        if last_checked and not should_refresh:\n"
        "            try:\n"
        "                last_dt = datetime.fromisoformat(last_checked)\n"
        "                should_refresh = (now_ist() - last_dt).total_seconds() > 1800\n"
        "            except Exception:\n"
        "                should_refresh = True"
    )
    new = (
        "        if last_checked and not should_refresh:\n"
        "            try:\n"
        "                last_dt = datetime.fromisoformat(last_checked)\n"
        "                _elapsed = (now_ist() - last_dt).total_seconds()\n"
        "                _now_t = now_ist().time()\n"
        "                _is_tuesday = today_ist().weekday() == 1\n"
        "                _in_0dte_window = _is_tuesday and dtime(12, 0) <= _now_t < dtime(14, 0)\n"
        "                expiry_cache_ttl_tuesday = True\n"
        "                _ttl = 300 if _in_0dte_window else 1800\n"
        "                should_refresh = _elapsed > _ttl\n"
        "            except Exception:\n"
        "                should_refresh = True"
    )
    assert old in src, "patch_expiry_cache_ttl_tuesday not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py expiry cache TTL reduced to 5min on Tuesday 0DTE window")


def patch_stop_reason_suppression():
    filename = "strategy_engine.py"
    src = read(filename)

    if "stop_reason_suppression" in src:
        print("strategy_engine.py stop reason suppression already present — skipping")
        return

    old = (
        "        if state.get(\"consecutive_stops\", 0) >= 3:\n"
        "            return \"NO_TRADE\", \"3_consecutive_stops_today_halt\""
    )
    new = (
        "        if state.get(\"consecutive_stops\", 0) >= 3:\n"
        "            return \"NO_TRADE\", \"3_consecutive_stops_today_halt\"\n"
        "        _last_stop_reason = state.get(\"last_stop_reason\", \"\")\n"
        "        _last_stop_signal = state.get(\"last_stop_signal_combo\", \"\")\n"
        "        _current_signal_combo = f\"{s.get('volatility_condition')}_{s.get('trend_condition')}_{s.get('direction')}\"\n"
        "        stop_reason_suppression = True\n"
        "        if (_last_stop_reason == \"CLOSE_STOP\" and _last_stop_signal == _current_signal_combo\n"
        "                and state.get(\"consecutive_stops\", 0) >= 1):\n"
        "            return \"NO_TRADE\", f\"same_signal_combo_caused_last_stop_{_current_signal_combo}\""
    )
    assert old in src, "patch_stop_reason_suppression not found"
    src = src.replace(old, new, 1)
    write(filename, src)

    old2 = (
        "        if reason == \"CLOSE_STOP\":\n"
        "            state[\"last_stop_time\"] = now_ist().isoformat()\n"
        "            state[\"last_stop_reason\"] = reason\n"
        "            state[\"consecutive_stops\"] = state.get(\"consecutive_stops\", 0) + 1"
    )
    if old2 in read("execution_engine.py"):
        src_ee = read("execution_engine.py")
        new2 = (
            "        if reason == \"CLOSE_STOP\":\n"
            "            state[\"last_stop_time\"] = now_ist().isoformat()\n"
            "            state[\"last_stop_reason\"] = reason\n"
            "            state[\"consecutive_stops\"] = state.get(\"consecutive_stops\", 0) + 1\n"
            "            _open_pos_list = self._get_open_positions()\n"
            "            if not _open_pos_list:\n"
            "                from market_data_engine import MarketDataEngine as _MDE\n"
            "                _sig = self.market_engine.state\n"
            "                _combo = f\"{_sig.get('volatility_condition', '')}_{_sig.get('trend_condition', '')}_{_sig.get('direction', '')}\"\n"
            "                state[\"last_stop_signal_combo\"] = _combo"
        )
        src_ee = src_ee.replace(old2, new2, 1)
        write("execution_engine.py", src_ee)
        print("patched execution_engine.py records signal combo on stop")

    write(filename, src)
    print("patched strategy_engine.py suppresses re-entry on same signal combo that caused last stop")


def verify_patches():
    errors = []

    src = read("execution_engine.py")
    if "total_open_lots_for_delta" not in src:
        errors.append("execution_engine: portfolio delta total lots not present")
    if "last_known_premium_fallback" not in src:
        errors.append("execution_engine: chain expiry fallback missing")
    if "lock_stop = max(true_breakeven_premium, current_premium * 1.05)" not in src:
        errors.append("execution_engine: profit lock not current_premium * 1.05")
    if "adx_value > 35 and strategy_type == \"SELL\"" not in src:
        errors.append("execution_engine: ADX exit threshold not 35")
    if "_credit_stop_limit = position[\"entry_credit\"] * 1.5" not in src:
        errors.append("execution_engine: credit stop not 1.5x")
    if "_time_target_pct = 0.25" not in src:
        errors.append("execution_engine: time target not 0.25")
    if "_vwap_exits_active" not in src:
        errors.append("execution_engine: VWAP exit CAS disable missing")
    if "mid = (bid + ask) / 2.0" not in src:
        errors.append("execution_engine: mid-price limit order missing")

    src = read("strategy_engine.py")
    if "stop_reason_suppression" not in src:
        errors.append("strategy_engine: stop reason suppression not present")
    if "brokerage_fixed_cost" not in src:
        errors.append("strategy_engine: brokerage fixed cost not present")
    if "tightening_profit_gated" not in src:
        errors.append("strategy_engine: tightening schedule not relaxed")
    if "0dte_neutral_use_iron_butterfly" not in src:
        errors.append("strategy_engine: 0DTE neutral Iron Butterfly missing")
    if "elevated_vix_narrow_or_straddle_allowed" not in src:
        errors.append("strategy_engine: straddle ELEVATED VIX exception missing")
    if "_min_ratio = 0.20 if _dte_for_ratio == 0" not in src:
        errors.append("strategy_engine: 0DTE credit ratio not 0.20")
    if "iv_expanding_no_new_sells_wait_for_stable_or_declining" not in src:
        errors.append("strategy_engine: IV expanding gate missing")
    if '"MONDAY": 0.32' not in src:
        errors.append("strategy_engine: TARGET_PCT not 0.32")
    if "bullish_strong_trend_put_credit_safe_side" not in src:
        errors.append("strategy_engine: STRONG_TREND spreads missing")
    if "contractual_max_loss" not in src:
        errors.append("strategy_engine: contractual max loss missing")
    if '"BULL_PUT_SPREAD": 15' not in src:
        errors.append("strategy_engine: MIN_CREDITS not updated")
    if "bullish_trend_sell_puts_aligned" not in src:
        errors.append("strategy_engine: trend-side fix missing")

    src = read("market_data_engine.py")
    if "elevated_vix_size_floor" not in src:
        errors.append("market_data_engine: ELEVATED VIX size floor not present")
    if "or_breakout_candle_close" not in src:
        errors.append("market_data_engine: OR breakout candle close not present")
    if "expiry_cache_ttl_tuesday" not in src:
        errors.append("market_data_engine: expiry cache TTL Tuesday not present")
    if "direction_score >= 1.2" not in src:
        errors.append("market_data_engine: direction thresholds not 1.2")
    if "pcr_baseline_set_at_10am" not in src:
        errors.append("market_data_engine: PCR 10AM baseline missing")
    if "vrp_blend_weight" not in src:
        errors.append("market_data_engine: VRP intraday blend missing")
    if "tolerance: float = 0.05" not in src:
        errors.append("market_data_engine: 25-delta tolerance not 0.05")
    if "buy_ok = True\n            vol_score = min(vol_score, -1)" not in src:
        errors.append("market_data_engine: buy_ok=True for SUPPRESSED missing")
    if "dtime(9, 30) <= b[\"timestamp\"].time() <= dtime(9, 44)" not in src:
        errors.append("market_data_engine: OR window not 09:30-09:44")
    if "or_breakout_score" not in src:
        errors.append("market_data_engine: OR breakout direction missing")
    if "if vix < 14.0: return \"LOW\"" not in src:
        errors.append("market_data_engine: VIX thresholds missing")
    if "rolling_intraday" not in src:
        errors.append("market_data_engine: Parkinson rolling missing")
    if "equal_pv / total_bars" not in src:
        errors.append("market_data_engine: VWAP fallback missing")

    src = read("nifty_algo_core.py")
    if "for key, val in raw.items():" not in src:
        errors.append("nifty_algo_core: event loader fix missing")

    if errors:
        print("\nVERIFICATION FAILED:")
        for e in errors:
            print("  ERROR: " + e)
        sys.exit(1)
    else:
        print("\nAll patches verified successfully.")


if __name__ == "__main__":
    print("Applying patch14...")
    patch_portfolio_delta_limit_total_lots()
    patch_elevated_vix_size_floor()
    patch_or_breakout_use_candle_close()
    patch_tightening_schedule_profit_gated()
    patch_brokerage_fixed_not_per_lot()
    patch_expiry_cache_ttl_tuesday()
    patch_stop_reason_suppression()
    verify_patches()
    print("\nDone. All patch14 changes applied.")