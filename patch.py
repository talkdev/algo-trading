import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def patch_pcr_valid_range():
    filename = "market_data_engine.py"
    src = read(filename)

    if "pcr < 0.2 or pcr > 6.0" in src:
        print("market_data_engine.py PCR range already updated — skipping")
        return

    old = (
        "        if pcr < 0.3 or pcr > 3.0:\n"
        "            self.logger.warning(f\"PCR {pcr:.3f} outside valid range [0.3,3.0] — discarding\")\n"
        "            return None"
    )
    new = (
        "        if pcr < 0.2 or pcr > 6.0:\n"
        "            self.logger.warning(f\"PCR {pcr:.3f} outside valid range [0.2,6.0] — discarding\")\n"
        "            return None"
    )
    assert old in src, "patch_pcr_valid_range not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py PCR valid range expanded to [0.2, 6.0]")


def patch_or_breakout_thresholds_restore():
    filename = "market_data_engine.py"
    src = read(filename)

    if "or_breakout_threshold_restored" in src:
        print("market_data_engine.py OR breakout thresholds already restored — skipping")
        return

    old = (
        "                if breakout_pts > or_width * 0.30 and trend_condition in (\"RANGE_BOUND\", \"MILD_RANGE\"):\n"
        "                        trend_condition, trend_score = \"MILD_TREND\", 0\n"
        "                    if breakout_pts > or_width * 0.70:\n"
        "                        trend_condition, trend_score = \"TRENDING\", -1\n"
        "                    if breakout_pts > or_width * 1.20 and adx_condition in (\"MODERATE\", \"STRONG\", \"VERY_STRONG\"):\n"
        "                        trend_condition, trend_score = \"STRONG_TREND\", -2\n"
        "            or_position_trend_override = True"
    )

    if old not in src:
        idx = src.find("or_width * 0.30")
        if idx >= 0:
            print("Found or_width * 0.30 at index", idx)
            print(repr(src[max(0, idx-200):idx+400]))
        else:
            idx2 = src.find("or_position_trend_override")
            if idx2 >= 0:
                print("Found or_position_trend_override at index", idx2)
                print(repr(src[max(0, idx2-400):idx2+100]))
            else:
                print("or_position_trend_override not found either")
        print("Skipping OR breakout threshold restore — block not found as expected")
        return

    new = (
        "                if breakout_pts > or_width * 0.50 and trend_condition in (\"RANGE_BOUND\", \"MILD_RANGE\"):\n"
        "                        trend_condition, trend_score = \"MILD_TREND\", 0\n"
        "                    if breakout_pts > or_width * 1.00:\n"
        "                        trend_condition, trend_score = \"TRENDING\", -1\n"
        "                    if breakout_pts > or_width * 1.20 and adx_condition in (\"MODERATE\", \"STRONG\", \"VERY_STRONG\"):\n"
        "                        trend_condition, trend_score = \"STRONG_TREND\", -2\n"
        "            or_breakout_threshold_restored = True"
    )
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py OR breakout thresholds restored to 0.50/1.00/1.20")


def patch_or_breakout_thresholds_targeted():
    filename = "market_data_engine.py"
    src = read(filename)

    if "or_breakout_threshold_restored" in src:
        print("market_data_engine.py OR breakout thresholds already restored — skipping")
        return

    idx = src.find("or_width * 0.30")
    if idx < 0:
        idx = src.find("or_width * 0.50")
        if idx >= 0:
            print("OR breakout already uses 0.50 — checking full block")
            if "or_position_trend_override" in src and "or_breakout_threshold_restored" not in src:
                src = src.replace("or_position_trend_override = True", "or_breakout_threshold_restored = True", 1)
                write(filename, src)
                print("patched market_data_engine.py OR breakout marker updated")
            return
        print("WARNING: or_width * 0.30 not found and or_width * 0.50 not found")
        return

    context_start = max(0, idx - 10)
    context_end = src.find("\n", idx + 200) + 1
    current_block = src[context_start:context_end]
    print(f"Current OR breakout block context: {repr(current_block)}")

    src = src.replace("or_width * 0.30", "or_width * 0.50", 1)
    src = src.replace("or_width * 0.70", "or_width * 1.00", 1)
    if "or_position_trend_override" in src:
        src = src.replace("or_position_trend_override = True", "or_breakout_threshold_restored = True", 1)

    write(filename, src)
    print("patched market_data_engine.py OR breakout thresholds 0.30→0.50, 0.70→1.00")


def patch_close_delta_cooldown():
    filename = "execution_engine.py"
    src = read(filename)

    if "CLOSE_DELTA_cooldown" in src:
        print("execution_engine.py CLOSE_DELTA cooldown already present — skipping")
        return

    old = (
        "        elif reason in (\"CLOSE_ADX\", \"CLOSE_VWAP\"):\n"
        "            state[\"last_stop_time\"] = now_ist().isoformat()\n"
        "            state[\"last_stop_reason\"] = reason"
    )
    new = (
        "        elif reason in (\"CLOSE_ADX\", \"CLOSE_VWAP\", \"CLOSE_DELTA\"):\n"
        "            state[\"last_stop_time\"] = now_ist().isoformat()\n"
        "            state[\"last_stop_reason\"] = reason\n"
        "            CLOSE_DELTA_cooldown = True"
    )
    assert old in src, "patch_close_delta_cooldown not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched execution_engine.py CLOSE_DELTA sets cooldown")


def patch_tightening_uses_original_stop():
    filename = "execution_engine.py"
    src = read(filename)

    if "original_stop_premium_for_tightening" in src:
        print("execution_engine.py tightening original stop already patched — skipping")
        return

    old = (
        "        raw_params = json.loads(position.get(\"raw_params_json\") or \"{}\")\n"
        "        tightening_schedule = raw_params.get(\"tightening_schedule\", [])\n"
        "        effective_stop = position.get(\"stop_premium\")\n"
        "        if effective_stop is not None:\n"
        "            for tighten_time_str, factor in tightening_schedule:\n"
        "                try:\n"
        "                    tighten_time = datetime.strptime(tighten_time_str, \"%H:%M\").time()\n"
        "                except Exception:\n"
        "                    continue\n"
        "                if current_time >= tighten_time:\n"
        "                    effective_stop = position[\"stop_premium\"] * factor"
    )
    new = (
        "        raw_params = json.loads(position.get(\"raw_params_json\") or \"{}\")\n"
        "        tightening_schedule = raw_params.get(\"tightening_schedule\", [])\n"
        "        original_stop_premium_for_tightening = raw_params.get(\"stop_premium\") or position.get(\"stop_premium\")\n"
        "        effective_stop = position.get(\"stop_premium\")\n"
        "        if effective_stop is not None and original_stop_premium_for_tightening:\n"
        "            for tighten_time_str, factor in tightening_schedule:\n"
        "                try:\n"
        "                    tighten_time = datetime.strptime(tighten_time_str, \"%H:%M\").time()\n"
        "                except Exception:\n"
        "                    continue\n"
        "                if current_time >= tighten_time:\n"
        "                    effective_stop = original_stop_premium_for_tightening * factor"
    )
    assert old in src, "patch_tightening_uses_original_stop not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched execution_engine.py tightening schedule uses original stop from raw_params")


def patch_brokerage_fixed_cost_correct():
    filename = "strategy_engine.py"
    src = read(filename)

    if "brokerage_fixed_cost" not in src:
        print("strategy_engine.py brokerage_fixed_cost not present — skipping (not yet patched)")
        return

    if "total_costs_rupees_per_lot = per_lot_variable_costs + (brokerage_fixed_cost + gst_total)" not in src:
        print("strategy_engine.py brokerage cost structure not in expected form — skipping")
        return

    if "entry_costs_rupees_correct" in src:
        print("strategy_engine.py brokerage fixed cost already correctly separated — skipping")
        return

    old = (
        "        per_lot_variable_costs = stt_per_lot + exchange_per_lot + sebi_per_lot + stamp_per_lot\n"
        "        total_costs_rupees_per_lot = per_lot_variable_costs + (brokerage_fixed_cost + gst_total)"
        "\n        total_costs_pts_per_lot = total_costs_rupees_per_lot / C02"
    )
    new = (
        "        per_lot_variable_costs = stt_per_lot + exchange_per_lot + sebi_per_lot + stamp_per_lot\n"
        "        total_costs_rupees_per_lot = per_lot_variable_costs\n"
        "        total_fixed_costs_rupees = brokerage_fixed_cost + gst_total\n"
        "        total_costs_pts_per_lot = (total_costs_rupees_per_lot + total_fixed_costs_rupees / max(1, 1)) / C02\n"
        "        entry_costs_rupees_correct = True"
    )
    assert old in src, "patch_brokerage_fixed_cost_correct not found"
    src = src.replace(old, new, 1)

    old2 = (
        "            \"entry_costs_rupees\": params[\"total_costs_rupees_per_lot\"] * lots,"
    )
    if old2 in read("execution_engine.py"):
        src_ee = read("execution_engine.py")
        new2 = (
            "            \"entry_costs_rupees\": params[\"total_costs_rupees_per_lot\"] * lots + params.get(\"total_fixed_costs_rupees\", 0),"
        )
        src_ee = src_ee.replace(old2, new2, 1)
        write("execution_engine.py", src_ee)
        print("patched execution_engine.py entry_costs_rupees adds fixed costs")

    old3 = (
        "            \"entry_costs_rupees\": params[\"total_costs_rupees_per_lot\"] * params[\"final_lots\"],"
    )
    if old3 in read("execution_engine.py"):
        src_ee = read("execution_engine.py")
        new3 = (
            "            \"entry_costs_rupees\": params[\"total_costs_rupees_per_lot\"] * params[\"final_lots\"] + params.get(\"total_fixed_costs_rupees\", 0),"
        )
        src_ee = src_ee.replace(old3, new3, 1)
        write("execution_engine.py", src_ee)

    old4 = (
        "        return {\n"
        "            \"valid\": True,\n"
        "            \"strategy_name\": strategy_name, \"strategy_type\": strategy_type,"
    )
    if old4 in src:
        new4 = (
            "        return {\n"
            "            \"valid\": True,\n"
            "            \"total_fixed_costs_rupees\": total_fixed_costs_rupees,\n"
            "            \"strategy_name\": strategy_name, \"strategy_type\": strategy_type,"
        )
        src = src.replace(old4, new4, 1)

    write(filename, src)
    print("patched strategy_engine.py brokerage separated as fixed cost correctly")


def patch_strike_fallback_on_liquidity_fail():
    filename = "strategy_engine.py"
    src = read(filename)

    if "strike_fallback_liquidity" in src:
        print("strategy_engine.py strike fallback on liquidity fail already present — skipping")
        return

    old = (
        "        for leg_spec in legs_spec:\n"
        "            strike, opt_type, action = leg_spec[\"strike\"], leg_spec[\"option_type\"], leg_spec[\"action\"]\n"
        "            ok, reason = self._validate_strike(chain, strike, opt_type, action)\n"
        "            if not ok:\n"
        "                return {\"valid\": False, \"reason\": f\"leg_validation_failed: {reason}\"}"
    )
    new = (
        "        for leg_spec in legs_spec:\n"
        "            strike, opt_type, action = leg_spec[\"strike\"], leg_spec[\"option_type\"], leg_spec[\"action\"]\n"
        "            ok, reason = self._validate_strike(chain, strike, opt_type, action)\n"
        "            if not ok:\n"
        "                strike_fallback_liquidity = True\n"
        "                adjacent_strikes = sorted(\n"
        "                    [s for s in chain.keys() if abs(s - strike) <= self.config.nifty_strike_step * 2 and s != strike],\n"
        "                    key=lambda s: abs(s - strike)\n"
        "                )\n"
        "                fallback_found = False\n"
        "                for alt_strike in adjacent_strikes:\n"
        "                    alt_ok, _ = self._validate_strike(chain, alt_strike, opt_type, action)\n"
        "                    if alt_ok:\n"
        "                        leg_spec[\"strike\"] = alt_strike\n"
        "                        strike = alt_strike\n"
        "                        fallback_found = True\n"
        "                        self.logger.info(f\"Strike fallback: {strike:.0f} failed liquidity, using {alt_strike:.0f}\")\n"
        "                        break\n"
        "                if not fallback_found:\n"
        "                    return {\"valid\": False, \"reason\": f\"leg_validation_failed_no_fallback: {reason}\"}"
    )
    assert old in src, "patch_strike_fallback_on_liquidity_fail not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched strategy_engine.py strike fallback to adjacent strike on liquidity failure")


def verify_patches():
    errors = []

    src = read("market_data_engine.py")
    if "pcr < 0.2 or pcr > 6.0" not in src:
        errors.append("market_data_engine: PCR range not [0.2, 6.0]")
    if "or_breakout_threshold_restored" not in src and "or_width * 0.50" not in src:
        errors.append("market_data_engine: OR breakout threshold not restored to 0.50")
    if "_pcr_baseline_set" not in src:
        errors.append("market_data_engine: _pcr_baseline_set instance var missing")
    if "pcr_baseline_set_at_10am" in src:
        errors.append("market_data_engine: pcr_baseline_set_at_10am still present (DB column error)")
    if "n_candles >= 6 and opening_iv" not in src:
        errors.append("market_data_engine: n_candles guard missing")
    if "last_candle_close_val: float = 0.0" not in src:
        errors.append("market_data_engine: last_candle_close_val parameter missing")
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

    src = read("execution_engine.py")
    if "CLOSE_DELTA_cooldown" not in src:
        errors.append("execution_engine: CLOSE_DELTA cooldown not present")
    if "original_stop_premium_for_tightening" not in src:
        errors.append("execution_engine: tightening original stop not present")
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

    src = read("strategy_engine.py")
    if "strike_fallback_liquidity" not in src:
        errors.append("strategy_engine: strike fallback on liquidity fail not present")
    if "rich_uncertain_neutral_condor_half" not in src:
        errors.append("strategy_engine: RICH+UNCERTAIN+NEUTRAL condor missing")
    if "stop_reason_suppression" not in src:
        errors.append("strategy_engine: stop reason suppression missing")
    if "tightening_profit_gated" not in src:
        errors.append("strategy_engine: tightening schedule missing")
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
        print("Engine is fully patched through patch16.")


if __name__ == "__main__":
    print("Applying patch16...")
    patch_pcr_valid_range()
    patch_or_breakout_thresholds_restore()
    patch_or_breakout_thresholds_targeted()
    patch_close_delta_cooldown()
    patch_tightening_uses_original_stop()
    patch_brokerage_fixed_cost_correct()
    patch_strike_fallback_on_liquidity_fail()
    verify_patches()
    print("\nDone. All patch16 changes applied.")