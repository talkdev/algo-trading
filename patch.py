import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def find_vega_gamma_block():
    src = read("execution_engine.py")
    idx = src.find("live_vega")
    if idx >= 0:
        print("Found live_vega at index", idx)
        print(repr(src[max(0, idx-200):idx+300]))
    else:
        print("live_vega not found in execution_engine.py")


def patch_vega_stale_fallback_dte_scaled():
    filename = "execution_engine.py"
    src = read(filename)

    if "vega_dte_scaled_fallback" in src:
        print("execution_engine.py vega DTE-scaled fallback already present — skipping")
        return

    idx = src.find("live_vega")
    if idx < 0:
        print("ERROR: live_vega not found in execution_engine.py")
        sys.exit(1)

    line_start = src.rfind("\n", 0, idx) + 1
    line_end = src.find("\n", idx) + 1
    next_line_end = src.find("\n", line_end) + 1

    current_vega_line = src[line_start:line_end]
    current_gamma_line = src[line_end:next_line_end]

    print(f"Current vega line: {repr(current_vega_line)}")
    print(f"Current gamma line: {repr(current_gamma_line)}")

    indent = len(current_vega_line) - len(current_vega_line.lstrip())
    ind = " " * indent

    old_block = current_vega_line + current_gamma_line
    new_block = (
        f"{ind}_entry_vega = leg[\"entry_vega\"] or 0\n"
        f"{ind}_entry_gamma = leg[\"entry_gamma\"] or 0\n"
        f"{ind}if opt:\n"
        f"{ind}    live_vega = opt.get(\"vega\") or _entry_vega\n"
        f"{ind}    live_gamma = opt.get(\"gamma\") or _entry_gamma\n"
        f"{ind}else:\n"
        f"{ind}    vega_dte_scaled_fallback = True\n"
        f"{ind}    live_vega = _entry_vega * 0.5\n"
        f"{ind}    live_gamma = _entry_gamma * 2.0\n"
    )

    assert old_block in src, f"vega/gamma block not found as expected: {repr(old_block)}"
    src = src.replace(old_block, new_block, 1)
    write(filename, src)
    print("patched execution_engine.py vega fallback scaled down, gamma scaled up when chain unavailable")


def verify_patches():
    errors = []

    src = read("market_data_engine.py")
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

    src = read("strategy_engine.py")
    if "fair_vrp_directional_no_reduction" not in src:
        errors.append("strategy_engine: FAIR VRP directional no reduction not present")
    if "trending_neutral_adx_safe_side" not in src:
        errors.append("strategy_engine: TRENDING+NEUTRAL safe side not present")
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

    src = read("execution_engine.py")
    if "vega_dte_scaled_fallback" not in src:
        errors.append("execution_engine: vega DTE-scaled fallback not present")
    if "delta_reduction_total_lots" not in src:
        errors.append("execution_engine: delta reduction loop total lots not present")
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
        print("Engine is fully patched through patch17.")


if __name__ == "__main__":
    print("Applying patch17b...")
    find_vega_gamma_block()
    patch_vega_stale_fallback_dte_scaled()
    verify_patches()
    print("\nDone.")