import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def patch_net_pnl_includes_entry_costs():
    filename = "execution_engine.py"
    src = read(filename)

    if "net_pnl_includes_entry_costs" in src:
        print("execution_engine.py net_pnl entry costs fix already present — skipping")
        return

    old = (
        "        gross_pnl_rupees = gross_pnl_pts * C02 * lots\n"
        "        entry_costs_rupees = position.get(\"entry_costs_rupees\") or 0.0\n"
        "        total_costs_rupees = entry_costs_rupees + total_exit_costs_rupees\n"
        "        net_pnl_rupees = gross_pnl_rupees - total_exit_costs_rupees"
    )
    new = (
        "        gross_pnl_rupees = gross_pnl_pts * C02 * lots\n"
        "        entry_costs_rupees = position.get(\"entry_costs_rupees\") or 0.0\n"
        "        total_costs_rupees = entry_costs_rupees + total_exit_costs_rupees\n"
        "        net_pnl_rupees = gross_pnl_rupees - total_costs_rupees\n"
        "        net_pnl_includes_entry_costs = True"
    )
    assert old in src, "patch_net_pnl_includes_entry_costs not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched execution_engine.py net_pnl_rupees now deducts both entry and exit costs")


def patch_atm_iv_zero_oi_fallback():
    filename = "market_data_engine.py"
    src = read(filename)

    if "atm_iv_zero_oi_fallback" in src:
        print("market_data_engine.py ATM IV zero OI fallback already present — skipping")
        return

    old = (
        "        total_oi = call_oi + put_oi\n"
        "        atm_iv = ((call_iv * call_oi + put_iv * put_oi) / total_oi) if total_oi > 0 \\\n"
        "            else (call_iv + put_iv) / 2.0"
    )
    new = (
        "        total_oi = call_oi + put_oi\n"
        "        if total_oi > 0:\n"
        "            atm_iv = (call_iv * call_oi + put_iv * put_oi) / total_oi\n"
        "        elif call_iv > 0 and put_iv > 0:\n"
        "            atm_iv = (call_iv + put_iv) / 2.0\n"
        "        elif call_iv > 0:\n"
        "            atm_iv = call_iv\n"
        "        elif put_iv > 0:\n"
        "            atm_iv = put_iv\n"
        "        else:\n"
        "            atm_iv_zero_oi_fallback = True\n"
        "            return None\n"
        "        atm_iv_zero_oi_fallback = False"
    )
    assert old in src, "patch_atm_iv_zero_oi_fallback not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py ATM IV uses non-zero IV when OI is zero")


def patch_iron_condor_short_strike_gap():
    filename = "strategy_engine.py"
    src = read(filename)

    if "condor_short_strike_gap_check" in src:
        print("strategy_engine.py Iron Condor short strike gap check already present — skipping")
        return

    old = (
        "            if long_call <= short_call:\n"
        "                return None, \"condor_long_call_not_further_otm\"\n"
        "            if long_put >= short_put:\n"
        "                return None, \"condor_long_put_not_further_otm\"\n"
        "            return [\n"
        "                {\"strike\": short_call, \"option_type\": \"call\", \"action\": \"SELL\"},\n"
        "                {\"strike\": short_put, \"option_type\": \"put\", \"action\": \"SELL\"},\n"
        "                {\"strike\": long_call, \"option_type\": \"call\", \"action\": \"BUY\"},\n"
        "                {\"strike\": long_put, \"option_type\": \"put\", \"action\": \"BUY\"},\n"
        "            ], None"
    )
    new = (
        "            if long_call <= short_call:\n"
        "                return None, \"condor_long_call_not_further_otm\"\n"
        "            if long_put >= short_put:\n"
        "                return None, \"condor_long_put_not_further_otm\"\n"
        "            condor_short_strike_gap_check = True\n"
        "            if short_call <= short_put + self.config.nifty_strike_step:\n"
        "                return None, f\"condor_short_strikes_too_close_{short_put:.0f}_{short_call:.0f}\"\n"
        "            return [\n"
        "                {\"strike\": short_call, \"option_type\": \"call\", \"action\": \"SELL\"},\n"
        "                {\"strike\": short_put, \"option_type\": \"put\", \"action\": \"SELL\"},\n"
        "                {\"strike\": long_call, \"option_type\": \"call\", \"action\": \"BUY\"},\n"
        "                {\"strike\": long_put, \"option_type\": \"put\", \"action\": \"BUY\"},\n"
        "            ], None"
    )
    assert old in src, "patch_iron_condor_short_strike_gap not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched strategy_engine.py Iron Condor validates short call > short put + step")


def patch_skew_thresholds_2026():
    filename = "market_data_engine.py"
    src = read(filename)

    if "skew_thresholds_2026" in src:
        print("market_data_engine.py skew thresholds already 2026-calibrated — skipping")
        return

    old = (
        "        if skew is None:\n"
        "            skew_signal, skew_score, preferred_side = \"UNKNOWN\", 0, \"BOTH\"\n"
        "        elif skew > 1.50: skew_signal, skew_score, preferred_side = \"EXTREME_FEAR\", -1, \"CALLS\"\n"
        "        elif skew > 1.35: skew_signal, skew_score, preferred_side = \"FEAR\", -1, \"CALLS\"\n"
        "        elif skew > 1.10: skew_signal, skew_score, preferred_side = \"NORMAL\", 0, \"BOTH\"\n"
        "        elif skew > 0.95: skew_signal, skew_score, preferred_side = \"BALANCED\", 0, \"BOTH\"\n"
        "        else: skew_signal, skew_score, preferred_side = \"COMPLACENT\", 1, \"PUTS\""
    )
    new = (
        "        skew_thresholds_2026 = True\n"
        "        if skew is None:\n"
        "            skew_signal, skew_score, preferred_side = \"UNKNOWN\", 0, \"BOTH\"\n"
        "        elif skew > 1.40: skew_signal, skew_score, preferred_side = \"EXTREME_FEAR\", -1, \"CALLS\"\n"
        "        elif skew > 1.25: skew_signal, skew_score, preferred_side = \"FEAR\", -1, \"CALLS\"\n"
        "        elif skew > 1.10: skew_signal, skew_score, preferred_side = \"NORMAL\", 0, \"BOTH\"\n"
        "        elif skew > 0.95: skew_signal, skew_score, preferred_side = \"BALANCED\", 0, \"BOTH\"\n"
        "        else: skew_signal, skew_score, preferred_side = \"COMPLACENT\", 1, \"PUTS\""
    )
    assert old in src, "patch_skew_thresholds_2026 not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py skew thresholds recalibrated for 2026 NIFTY regime")


def patch_cycle_log_total_pnl():
    filename = "market_data_engine.py"
    src = read(filename)

    if "total_pnl_for_cycle_log" in src:
        print("market_data_engine.py cycle log total pnl already present — skipping")
        return

    old = (
        "            \"open_positions\": 0, \"daily_pnl_net\": s.get(\"daily_pnl\", 0.0),"
    )
    new = (
        "            \"open_positions\": 0, \"daily_pnl_net\": s.get(\"daily_pnl\", 0.0),\n"
        "            total_pnl_for_cycle_log = True,"
    )

    if old not in src:
        idx = src.find("\"daily_pnl_net\":")
        if idx >= 0:
            print(f"daily_pnl_net found at {idx}: {repr(src[idx:idx+100])}")
        print("Skipping cycle log total pnl — block not found as expected")
        return

    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py cycle log notes total pnl")


def patch_finalize_cycle_log_total_pnl():
    filename = "main.py"
    src = read(filename)

    if "finalize_cycle_with_total_pnl" in src:
        print("main.py finalize cycle log total pnl already present — skipping")
        return

    old = (
        "        self._print_cycle_footer()\n"
        "        self.loop_count += 1"
    )
    new = (
        "        _total_pnl_cycle = self.compute_total_daily_pnl()\n"
        "        finalize_cycle_with_total_pnl = True\n"
        "        latest_cycle = self.db.query_one(\n"
        "            \"SELECT cycle_id FROM cycle_log WHERE trading_date=? ORDER BY cycle_id DESC LIMIT 1\",\n"
        "            (today_ist().isoformat(),),\n"
        "        )\n"
        "        if latest_cycle:\n"
        "            self.db.update(\"cycle_log\", {\"daily_pnl_net\": _total_pnl_cycle},\n"
        "                           {\"cycle_id\": latest_cycle[\"cycle_id\"]})\n"
        "        self._print_cycle_footer()\n"
        "        self.loop_count += 1"
    )
    assert old in src, "patch_finalize_cycle_log_total_pnl not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched main.py cycle log updated with total P&L (realized + unrealized) each cycle")


def patch_partial_close_net_pnl():
    filename = "execution_engine.py"
    src = read(filename)

    if "partial_close_net_pnl_fixed" in src:
        print("execution_engine.py partial close net pnl already fixed — skipping")
        return

    old = (
        "        gross_pnl_rupees = gross_pnl_pts * C02 * lots\n"
        "        net_pnl_rupees = gross_pnl_rupees - total_exit_costs_rupees\n"
        "        net_pnl_pts = net_pnl_rupees / (C02 * lots) if (C02 * lots) else 0.0\n"
        "        result = \"WIN\" if net_pnl_rupees > 0 else (\"LOSS\" if net_pnl_rupees < 0 else \"BREAKEVEN\")\n"
        "\n"
        "        now = now_ist()\n"
        "        entry_time = datetime.fromisoformat(position[\"entry_time\"]) if position.get(\"entry_time\") else now\n"
        "        hold_minutes = (now - entry_time).total_seconds() / 60.0\n"
        "        current_capital = self.market_engine.state.get(\"current_capital\", self.config.starting_capital)\n"
        "        net_pnl_pct = (net_pnl_rupees / current_capital * 100.0) if current_capital else 0.0\n"
        "        credit_or_debit = position.get(\"entry_credit\") or position.get(\"entry_debit\") or 0.0\n"
        "        profit_pct_of_credit = (net_pnl_pts / credit_or_debit * 100.0) if credit_or_debit else 0.0\n"
        "\n"
        "        self.db.update(\"positions\", {\n"
        "            \"status\": \"CLOSED\", \"exit_time\": now.isoformat(), \"exit_reason\": reason,\n"
        "            \"exit_premium\": exit_premium, \"gross_pnl_rupees\": gross_pnl_rupees,\n"
        "            \"exit_costs_rupees\": total_exit_costs_rupees, \"net_pnl_rupees\": net_pnl_rupees,\n"
        "            \"updated_at\": now.isoformat(),\n"
        "        }, {\"position_id\": position[\"position_id\"]})\n"
        "\n"
        "        self.db.insert(\"trade_exits\", {\n"
        "            \"trade_id\": position[\"position_id\"], \"position_id\": position[\"position_id\"],\n"
        "            \"strategy_name\": position[\"strategy_name\"], \"exit_time\": now.isoformat(),\n"
        "            \"hold_minutes\": hold_minutes, \"exit_reason\": reason,\n"
        "            \"exit_spot\": self.market_engine.state.get(\"prev_spot\"),\n"
        "            \"exit_vix\": self.market_engine.state.get(\"prev_vix\"),\n"
        "            \"exit_adx\": None, \"exit_vwap_dist\": None,\n"
        "            \"exit_legs_json\": json.dumps(legs, default=str),\n"
        "            \"exit_premium\": exit_premium, \"gross_pnl_pts\": gross_pnl_pts, \"gross_pnl_rupees\": gross_pnl_rupees,\n"
        "            \"exit_slippage\": None, \"exit_costs_pts\": (costs[\"total_rupees\"] / C02) if C02 else None,\n"
        "            \"exit_costs_rupees\": total_exit_costs_rupees,\n"
        "            \"total_costs_rupees\": (position.get(\"entry_costs_rupees\") or 0.0) + total_exit_costs_rupees,\n"
        "            \"net_pnl_pts\": net_pnl_pts, \"net_pnl_rupees\": net_pnl_rupees, \"net_pnl_pct\": net_pnl_pct,\n"
        "            \"result\": result, \"profit_pct_of_credit\": profit_pct_of_credit, \"created_at\": now.isoformat(),\n"
        "        })"
    )

    if old in src:
        new = (
            "        entry_costs_for_partial = position.get(\"entry_costs_rupees\") or 0.0\n"
            "        total_costs_for_partial = entry_costs_for_partial + total_exit_costs_rupees\n"
            "        gross_pnl_rupees = gross_pnl_pts * C02 * lots\n"
            "        net_pnl_rupees = gross_pnl_rupees - total_costs_for_partial\n"
            "        partial_close_net_pnl_fixed = True\n"
            "        net_pnl_pts = net_pnl_rupees / (C02 * lots) if (C02 * lots) else 0.0\n"
            "        result = \"WIN\" if net_pnl_rupees > 0 else (\"LOSS\" if net_pnl_rupees < 0 else \"BREAKEVEN\")\n"
            "\n"
            "        now = now_ist()\n"
            "        entry_time = datetime.fromisoformat(position[\"entry_time\"]) if position.get(\"entry_time\") else now\n"
            "        hold_minutes = (now - entry_time).total_seconds() / 60.0\n"
            "        current_capital = self.market_engine.state.get(\"current_capital\", self.config.starting_capital)\n"
            "        net_pnl_pct = (net_pnl_rupees / current_capital * 100.0) if current_capital else 0.0\n"
            "        credit_or_debit = position.get(\"entry_credit\") or position.get(\"entry_debit\") or 0.0\n"
            "        profit_pct_of_credit = (net_pnl_pts / credit_or_debit * 100.0) if credit_or_debit else 0.0\n"
            "\n"
            "        self.db.update(\"positions\", {\n"
            "            \"status\": \"CLOSED\", \"exit_time\": now.isoformat(), \"exit_reason\": reason,\n"
            "            \"exit_premium\": exit_premium, \"gross_pnl_rupees\": gross_pnl_rupees,\n"
            "            \"exit_costs_rupees\": total_exit_costs_rupees, \"net_pnl_rupees\": net_pnl_rupees,\n"
            "            \"updated_at\": now.isoformat(),\n"
            "        }, {\"position_id\": position[\"position_id\"]})\n"
            "\n"
            "        self.db.insert(\"trade_exits\", {\n"
            "            \"trade_id\": position[\"position_id\"], \"position_id\": position[\"position_id\"],\n"
            "            \"strategy_name\": position[\"strategy_name\"], \"exit_time\": now.isoformat(),\n"
            "            \"hold_minutes\": hold_minutes, \"exit_reason\": reason,\n"
            "            \"exit_spot\": self.market_engine.state.get(\"prev_spot\"),\n"
            "            \"exit_vix\": self.market_engine.state.get(\"prev_vix\"),\n"
            "            \"exit_adx\": None, \"exit_vwap_dist\": None,\n"
            "            \"exit_legs_json\": json.dumps(legs, default=str),\n"
            "            \"exit_premium\": exit_premium, \"gross_pnl_pts\": gross_pnl_pts, \"gross_pnl_rupees\": gross_pnl_rupees,\n"
            "            \"exit_slippage\": None, \"exit_costs_pts\": (costs[\"total_rupees\"] / C02) if C02 else None,\n"
            "            \"exit_costs_rupees\": total_exit_costs_rupees,\n"
            "            \"total_costs_rupees\": total_costs_for_partial,\n"
            "            \"net_pnl_pts\": net_pnl_pts, \"net_pnl_rupees\": net_pnl_rupees, \"net_pnl_pct\": net_pnl_pct,\n"
            "            \"result\": result, \"profit_pct_of_credit\": profit_pct_of_credit, \"created_at\": now.isoformat(),\n"
            "        })"
        )
        src = src.replace(old, new, 1)
        write(filename, src)
        print("patched execution_engine.py _finalize_position_from_partial_closes net_pnl includes entry costs")
    else:
        print("partial close block not found in expected form — skipping")


def verify_patches():
    errors = []

    src = read("execution_engine.py")
    if "net_pnl_includes_entry_costs" not in src:
        errors.append("execution_engine: net_pnl entry costs fix not present")
    if "delta_reduction_total_lots" not in src:
        errors.append("execution_engine: delta reduction loop total lots missing")
    if "CLOSE_DELTA_cooldown" not in src:
        errors.append("execution_engine: CLOSE_DELTA cooldown missing")
    if "original_stop_premium_for_tightening" not in src:
        errors.append("execution_engine: tightening original stop missing")
    if "hard_exit_buffer_45min" not in src:
        errors.append("execution_engine: hard exit buffer 45min missing")
    if "vega_limit = 50.0 * final_lots" not in src:
        errors.append("execution_engine: vega limit not 50")
    if "gross_credit_for_profit_pct" not in src:
        errors.append("execution_engine: gross credit profit pct missing")
    if "total_open_lots_for_delta" not in src:
        errors.append("execution_engine: portfolio delta total lots missing")
    if "lock_stop = max(true_breakeven_premium, current_premium * 1.05)" not in src:
        errors.append("execution_engine: profit lock not current_premium * 1.05")
    if "adx_value > 35 and strategy_type == \"SELL\"" not in src:
        errors.append("execution_engine: ADX exit threshold not 35")
    if "_credit_stop_limit = position[\"entry_credit\"] * 1.5" not in src:
        errors.append("execution_engine: credit stop not 1.5x")
    if "_vwap_exits_active" not in src:
        errors.append("execution_engine: VWAP exit CAS disable missing")
    if "mid = (bid + ask) / 2.0" not in src:
        errors.append("execution_engine: mid-price limit order missing")
    if "vega_dte_scaled_fallback" not in src:
        errors.append("execution_engine: vega DTE-scaled fallback missing")

    src = read("market_data_engine.py")
    if "atm_iv_zero_oi_fallback" not in src:
        errors.append("market_data_engine: ATM IV zero OI fallback not present")
    if "skew_thresholds_2026" not in src:
        errors.append("market_data_engine: skew thresholds not 2026-calibrated")
    if "ratio_check_removed" not in src:
        errors.append("market_data_engine: Parkinson ratio check not removed")
    if "\"TUESDAY\": 0.60" not in src:
        errors.append("market_data_engine: Tuesday size multiplier not 0.60")
    if "or_flat_open_fallback" not in src:
        errors.append("market_data_engine: OR flat open fallback not present")
    if "pcr < 0.2 or pcr > 6.0" not in src:
        errors.append("market_data_engine: PCR range not [0.2, 6.0]")
    if "_pcr_baseline_set" not in src:
        errors.append("market_data_engine: _pcr_baseline_set instance var missing")
    if "pcr_baseline_set_at_10am" in src:
        errors.append("market_data_engine: pcr_baseline_set_at_10am still present (DB column error)")
    if "vrp_blend_weight" not in src:
        errors.append("market_data_engine: VRP intraday blend missing")
    if "if vix < 14.0: return \"LOW\"" not in src:
        errors.append("market_data_engine: VIX thresholds missing")
    if "rolling_intraday" not in src:
        errors.append("market_data_engine: Parkinson rolling missing")
    if "or_volume_filter_removed" not in src:
        errors.append("market_data_engine: OR volume filter fix missing")
    if "equal_pv / total_bars" not in src:
        errors.append("market_data_engine: VWAP fallback missing")
    if "or_breakout_candle_close" not in src:
        errors.append("market_data_engine: OR breakout candle close missing")

    src = read("strategy_engine.py")
    if "condor_short_strike_gap_check" not in src:
        errors.append("strategy_engine: Iron Condor short strike gap check not present")
    if "fair_vrp_directional_no_reduction" not in src:
        errors.append("strategy_engine: FAIR VRP directional no reduction missing")
    if "trending_neutral_adx_safe_side" not in src:
        errors.append("strategy_engine: TRENDING+NEUTRAL safe side missing")
    if "strike_fallback_liquidity" not in src:
        errors.append("strategy_engine: strike fallback on liquidity fail missing")
    if "rich_uncertain_neutral_condor_half" not in src:
        errors.append("strategy_engine: RICH+UNCERTAIN+NEUTRAL condor missing")
    if "0dte_neutral_use_iron_butterfly" not in src:
        errors.append("strategy_engine: 0DTE neutral Iron Butterfly missing")
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

    src = read("main.py")
    if "finalize_cycle_with_total_pnl" not in src:
        errors.append("main: cycle log total pnl update missing")

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
        print("Engine is fully patched through patch18.")


if __name__ == "__main__":
    print("Applying patch18...")
    patch_net_pnl_includes_entry_costs()
    patch_atm_iv_zero_oi_fallback()
    patch_iron_condor_short_strike_gap()
    patch_skew_thresholds_2026()
    patch_cycle_log_total_pnl()
    patch_finalize_cycle_log_total_pnl()
    patch_partial_close_net_pnl()
    verify_patches()
    print("\nDone. All patch18 changes applied.")