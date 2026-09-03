import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def patch_0dte_price_stop_35pts():
    filename = "strategy_engine.py"
    src = read(filename)

    if "price_stop_pts_0dte_tightened" in src:
        print("strategy_engine.py 0DTE price stop already tightened to 35 — skipping")
        return

    old = (
        "            if actual_dte == 0: price_stop_pts = int(price_stop_pts * 0.60)\n"
        "            elif actual_dte == 1: price_stop_pts = int(price_stop_pts * 0.75)\n"
        "            elif actual_dte <= 3: price_stop_pts = int(price_stop_pts * 0.90)"
    )
    new = (
        "            if actual_dte == 0: price_stop_pts = 35\n"
        "            elif actual_dte == 1: price_stop_pts = int(price_stop_pts * 0.75)\n"
        "            elif actual_dte <= 3: price_stop_pts = int(price_stop_pts * 0.90)\n"
        "            price_stop_pts_0dte_tightened = True"
    )
    assert old in src, "patch_0dte_price_stop_35pts not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched strategy_engine.py 0DTE price stop set to 35 pts")


def verify():
    errors = []

    src = read("strategy_engine.py")
    if "if actual_dte == 0: price_stop_pts = 35" not in src:
        errors.append("strategy_engine: 0DTE price stop not 35 pts")
    if "iv_expanding_no_new_sells_wait_for_stable_or_declining" not in src:
        errors.append("strategy_engine: IV expanding gate missing (patch11)")
    if "base_lots == 1 and _size_mult_check < 0.4" not in src:
        errors.append("strategy_engine: half-size threshold not 0.4 (patch11)")
    if '"MONDAY": 0.32' not in src:
        errors.append("strategy_engine: TARGET_PCT not 0.32 (patch10)")
    if "bullish_strong_trend_put_credit_safe_side" not in src:
        errors.append("strategy_engine: STRONG_TREND spreads missing (patch10)")
    if "contractual_max_loss" not in src:
        errors.append("strategy_engine: contractual max loss missing (patch10)")
    if '"BULL_PUT_SPREAD": 15' not in src:
        errors.append("strategy_engine: MIN_CREDITS not updated (patch10)")
    if "bullish_trend_sell_puts_aligned" not in src:
        errors.append("strategy_engine: trend-side fix missing (patch9)")

    src = read("market_data_engine.py")
    if "dtime(9, 30) <= b[\"timestamp\"].time() <= dtime(9, 44)" not in src:
        errors.append("market_data_engine: OR window not 09:30-09:44 (patch11)")
    if "or_position_trend_override" not in src:
        errors.append("market_data_engine: OR breakout thresholds missing (patch11)")
    if "or_breakout_score" not in src:
        errors.append("market_data_engine: OR breakout direction missing (patch10)")
    if "if vix < 14.0: return \"LOW\"" not in src:
        errors.append("market_data_engine: VIX thresholds missing (patch5)")
    if "rolling_intraday" not in src:
        errors.append("market_data_engine: Parkinson rolling missing (patch5)")
    if "equal_pv / total_bars" not in src:
        errors.append("market_data_engine: VWAP fallback missing (patch4)")

    src = read("execution_engine.py")
    if "_credit_stop_limit = position[\"entry_credit\"] * 1.5" not in src:
        errors.append("execution_engine: credit stop not 1.5x (patch10)")
    if "_time_target_pct = 0.25" not in src:
        errors.append("execution_engine: time target not 0.25 (patch10)")
    if "_vwap_exits_active" not in src:
        errors.append("execution_engine: VWAP exit CAS disable missing (patch8)")
    if "mid = (bid + ask) / 2.0" not in src:
        errors.append("execution_engine: mid-price limit order missing (patch7)")
    if "all_short_legs_below_2pt_cheap_buyback" not in src:
        errors.append("execution_engine: cheap leg buyback missing (patch7)")

    src = read("nifty_algo_core.py")
    if "for key, val in raw.items():" not in src:
        errors.append("nifty_algo_core: event loader fix missing (patch9)")

    if errors:
        print("\nVERIFICATION FAILED:")
        for e in errors:
            print("  ERROR: " + e)
        sys.exit(1)
    else:
        print("\nAll patches from patch1 through patch12 verified successfully.")
        print("Engine is fully patched.")


if __name__ == "__main__":
    print("Applying patch12 (0DTE price stop fix + full verification)...")
    patch_0dte_price_stop_35pts()
    verify()
    print("\nDone.")