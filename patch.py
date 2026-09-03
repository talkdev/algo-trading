import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def verify_all():
    errors = []

    src = read("strategy_engine.py")
    if "bullish_trend_sell_puts_aligned" not in src:
        errors.append("strategy_engine: trend-side selection fix not present")
    if "bearish_trend_sell_calls_aligned" not in src:
        errors.append("strategy_engine: trend-side selection fix not present (bearish)")
    if "bullish_trend_sell_calls_safe_side" in src:
        errors.append("strategy_engine: OLD reversed trend logic still present")
    if "fair_vrp_mild_trend" not in src:
        errors.append("strategy_engine: FAIR VRP mild trend block not present in any form")
    if "neutral+fair_vrp+range_quarter_size" not in src:
        errors.append("strategy_engine: FAIR VRP neutral condor branch not present")

    src = read("nifty_algo_core.py")
    if "for key, val in raw.items():" not in src:
        errors.append("nifty_algo_core: event loader fix not present")
    if "stamp_duty_buy_options=_get_float(env, \"STAMP_DUTY_BUY_OPTIONS\", 0.00003)" not in src:
        errors.append("nifty_algo_core: stamp duty config default not 0.00003")

    src = read("execution_engine.py")
    if "_actual_dte_gate = self.market_engine.state.get(\"actual_dte\")" not in src:
        errors.append("execution_engine: 0DTE gate by actual_dte not present")
    if "0dte_past_13:30_entry_cutoff" not in src:
        errors.append("execution_engine: 0DTE entry cutoff not present")
    if "_vwap_exits_active" not in src:
        errors.append("execution_engine: VWAP exit CAS disable not present")
    if "mid = (bid + ask) / 2.0" not in src:
        errors.append("execution_engine: mid-price limit order not present")
    if "time_decay_target" not in src:
        errors.append("execution_engine: time-decay trailing target not present")
    if "all_short_legs_below_2pt_cheap_buyback" not in src:
        errors.append("execution_engine: cheap leg early buyback not present")
    if "_credit_stop_limit = position[\"entry_credit\"] * 2.5" not in src:
        errors.append("execution_engine: credit-based stop limit not present")

    src = read("market_data_engine.py")
    if "_or_width_pct" not in src:
        errors.append("market_data_engine: OR width percentage normalization not present")
    if "if vix < 14.0: return \"LOW\"" not in src:
        errors.append("market_data_engine: VIX LOW threshold 14 not present")
    if "rolling_intraday" not in src:
        errors.append("market_data_engine: Parkinson rolling not present")
    if "intraday_rv > atm_iv * 1.10" not in src:
        errors.append("market_data_engine: intraday RV veto threshold not present")
    if "equal_pv / total_bars" not in src:
        errors.append("market_data_engine: price-anchored VWAP fallback not present")
    if "dte_wing_map" not in src:
        errors.append("market_data_engine: DTE wing map not present")
    if "is_0dte_window" not in src:
        errors.append("market_data_engine: expiry 0DTE window logic not present")
    if "gap_fade_opportunity" not in src:
        errors.append("market_data_engine: gap fade opportunity not present")

    if errors:
        print("\nVERIFICATION FAILED:")
        for e in errors:
            print("  ERROR: " + e)
        sys.exit(1)
    else:
        print("\nAll patches from patch1 through patch9 verified successfully.")
        print("Engine is fully patched.")


if __name__ == "__main__":
    print("Running full verification of all applied patches...")
    verify_all()