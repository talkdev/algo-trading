import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def find_delta_block():
    src = read("execution_engine.py")
    idx = src.find("live_delta")
    if idx >= 0:
        print(f"Found live_delta at index {idx}:")
        print(repr(src[max(0, idx-300):idx+300]))
    else:
        print("live_delta not found")


def patch_delta_fallback_conservative():
    filename = "execution_engine.py"
    src = read(filename)

    if "delta_conservative_fallback" in src:
        print("execution_engine.py delta conservative fallback already present — skipping")
        return

    idx = src.find("live_delta")
    if idx < 0:
        print("ERROR: live_delta not found in execution_engine.py")
        sys.exit(1)

    line_start = src.rfind("\n", 0, idx) + 1
    line_end = src.find("\n", idx) + 1
    next_line_start = line_end
    next_line_end = src.find("\n", next_line_start) + 1
    next_next_line_end = src.find("\n", next_line_end) + 1

    current_block = src[line_start:next_next_line_end]
    print(f"Current live_delta block: {repr(current_block)}")

    indent = len(src[line_start:idx]) - len(src[line_start:idx].lstrip())
    ind = " " * indent

    old_block = src[line_start:next_next_line_end]

    if "opt.get(\"delta\"" in old_block or "live_delta" in old_block:
        opt_line_start = src.rfind("\n", 0, line_start - 1) + 1
        opt_line_end = line_start
        opt_line = src[opt_line_start:opt_line_end]
        print(f"opt line: {repr(opt_line)}")

        sign_idx = src.find("sign = -1 if leg[\"action\"] == \"SELL\"", idx)
        if sign_idx < 0:
            print("ERROR: sign line not found after live_delta")
            sys.exit(1)

        sign_line_end = src.find("\n", sign_idx) + 1
        total_delta_idx = src.find("total_delta +=", sign_line_end)
        total_delta_line_end = src.find("\n", total_delta_idx) + 1

        old_segment = src[line_start:total_delta_line_end]
        print(f"Full segment to replace: {repr(old_segment)}")

        new_segment = (
            f"{ind}if opt:\n"
            f"{ind}    live_delta = opt.get(\"delta\", leg[\"entry_delta\"])\n"
            f"{ind}else:\n"
            f"{ind}    delta_conservative_fallback = True\n"
            f"{ind}    _ed = leg[\"entry_delta\"] or 0\n"
            f"{ind}    live_delta = _ed * 1.5 if abs(_ed) < 0.5 else _ed\n"
            f"{ind}sign = -1 if leg[\"action\"] == \"SELL\" else 1\n"
            f"{ind}total_delta += sign * live_delta * leg[\"qty\"]\n"
        )

        assert old_segment in src, f"segment not found: {repr(old_segment)}"
        src = src.replace(old_segment, new_segment, 1)
        write(filename, src)
        print("patched execution_engine.py portfolio delta uses conservative 1.5x fallback")
    else:
        print("ERROR: unexpected block structure")
        sys.exit(1)


def verify_patches():
    errors = []

    src = read("execution_engine.py")
    if "delta_conservative_fallback" not in src:
        errors.append("execution_engine: delta conservative fallback missing")
    if "entry_count_only_on_loss" not in src and "entry_count decremented" not in src:
        if "entry_count = max(0, current_count - 1)" not in src:
            errors.append("execution_engine: entry count on loss fix missing")
    if "close_one_side_stop_update" not in src:
        errors.append("execution_engine: close_one_side stop update missing")
    if "net_pnl_includes_entry_costs" not in src:
        errors.append("execution_engine: net_pnl entry costs fix missing")
    if "total_open_lots_for_delta" not in src:
        errors.append("execution_engine: portfolio delta total lots missing")
    if "lock_stop = max(true_breakeven_premium, current_premium * 1.05)" not in src:
        errors.append("execution_engine: profit lock not current_premium * 1.05")
    if "adx_value > 35 and strategy_type == \"SELL\"" not in src:
        errors.append("execution_engine: ADX exit threshold not 35")
    if "_credit_stop_limit = position[\"entry_credit\"] * 1.5" not in src:
        errors.append("execution_engine: credit stop not 1.5x")
    if "mid = (bid + ask) / 2.0" not in src:
        errors.append("execution_engine: mid-price limit order missing")
    if "vega_dte_scaled_fallback" not in src:
        errors.append("execution_engine: vega DTE-scaled fallback missing")

    src = read("strategy_engine.py")
    if "bull_put_or_midpoint_check" not in src:
        errors.append("strategy_engine: bull put OR midpoint check missing")
    if "condor_short_strike_gap_check" not in src:
        errors.append("strategy_engine: Iron Condor short strike gap check missing")
    if "fair_vrp_directional_no_reduction" not in src:
        errors.append("strategy_engine: FAIR VRP directional no reduction missing")
    if "trending_neutral_adx_safe_side" not in src:
        errors.append("strategy_engine: TRENDING+NEUTRAL safe side missing")
    if '"MONDAY": 0.32' not in src:
        errors.append("strategy_engine: TARGET_PCT not 0.32")
    if "contractual_max_loss" not in src:
        errors.append("strategy_engine: contractual max loss missing")
    if '"BULL_PUT_SPREAD": 15' not in src:
        errors.append("strategy_engine: MIN_CREDITS not updated")

    src = read("market_data_engine.py")
    if "skew_tolerance_fallback" not in src:
        errors.append("market_data_engine: skew tolerance fallback missing")
    if "wing_dte_vix_combined" not in src:
        errors.append("market_data_engine: wing width DTE+VIX combined missing")
    if "atm_iv_zero_oi_fallback" not in src:
        errors.append("market_data_engine: ATM IV zero OI fallback missing")
    if "skew_thresholds_2026" not in src:
        errors.append("market_data_engine: skew thresholds not 2026-calibrated")
    if "ratio_check_removed" not in src:
        errors.append("market_data_engine: Parkinson ratio check not removed")
    if "\"TUESDAY\": 0.60" not in src:
        errors.append("market_data_engine: Tuesday size multiplier not 0.60")
    if "pcr < 0.2 or pcr > 6.0" not in src:
        errors.append("market_data_engine: PCR range not [0.2, 6.0]")
    if "pcr_baseline_set_at_10am" in src:
        errors.append("market_data_engine: pcr_baseline_set_at_10am still present (DB column error)")
    if "vrp_blend_weight" not in src:
        errors.append("market_data_engine: VRP intraday blend missing")
    if "if vix < 14.0: return \"LOW\"" not in src:
        errors.append("market_data_engine: VIX thresholds missing")

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
        print("Engine is fully patched through patch19.")


if __name__ == "__main__":
    print("Applying patch19b (delta fallback fix)...")
    find_delta_block()
    patch_delta_fallback_conservative()
    verify_patches()
    print("\nDone.")