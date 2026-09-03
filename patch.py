import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def patch_25delta_tolerance():
    filename = "market_data_engine.py"
    src = read(filename)

    if "tolerance=0.05" in src:
        print("market_data_engine.py 25-delta tolerance already 0.05 — skipping")
        return

    old = (
        "    def _find_by_delta(self, chain: dict, opt_type: str, target: float, tolerance: float) -> Optional[float]:"
    )
    new = (
        "    def _find_by_delta(self, chain: dict, opt_type: str, target: float, tolerance: float = 0.05) -> Optional[float]:"
    )
    assert old in src, "patch_25delta_tolerance signature not found"
    src = src.replace(old, new, 1)

    for old_call, new_call in [
        (
            "put_iv = self._find_by_delta(chain, \"put\", 0.25, tolerance=0.08)",
            "put_iv = self._find_by_delta(chain, \"put\", 0.25, tolerance=0.05)"
        ),
        (
            "call_iv = self._find_by_delta(chain, \"call\", 0.25, tolerance=0.08)",
            "call_iv = self._find_by_delta(chain, \"call\", 0.25, tolerance=0.05)"
        ),
    ]:
        if old_call in src:
            src = src.replace(old_call, new_call, 1)

    write(filename, src)
    print("patched market_data_engine.py 25-delta tolerance set to 0.05 default")


def patch_buy_ok_suppressed_vix():
    filename = "market_data_engine.py"
    src = read(filename)

    if "buy_ok = True\n            vol_score = min(vol_score, -1)" in src:
        print("market_data_engine.py buy_ok=True for SUPPRESSED already present — skipping")
        return

    old = (
        "        if vix_regime == \"SUPPRESSED\":\n"
        "            sell_ok = False\n"
        "            vol_score = min(vol_score, -1)"
    )
    new = (
        "        if vix_regime == \"SUPPRESSED\":\n"
        "            sell_ok = False\n"
        "            buy_ok = True\n"
        "            vol_score = min(vol_score, -1)"
    )
    assert old in src, "patch_buy_ok_suppressed_vix not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py buy_ok=True when VIX SUPPRESSED")


def verify_patches():
    errors = []

    src = read("execution_engine.py")
    if "lock_stop = max(true_breakeven_premium, current_premium * 1.05)" not in src:
        errors.append("execution_engine: profit lock not using current_premium * 1.05")
    if "adx_value > 35 and strategy_type == \"SELL\"" not in src:
        errors.append("execution_engine: ADX exit threshold not 35")
    if "last_known_premium_fallback" not in src:
        errors.append("execution_engine: chain expiry fallback not present")
    if "_credit_stop_limit = position[\"entry_credit\"] * 1.5" not in src:
        errors.append("execution_engine: credit stop not 1.5x")
    if "_time_target_pct = 0.25" not in src:
        errors.append("execution_engine: time target not 0.25")
    if "_vwap_exits_active" not in src:
        errors.append("execution_engine: VWAP exit CAS disable missing")
    if "mid = (bid + ask) / 2.0" not in src:
        errors.append("execution_engine: mid-price limit order missing")
    if "all_short_legs_below_2pt_cheap_buyback" not in src:
        errors.append("execution_engine: cheap leg buyback missing")

    src = read("strategy_engine.py")
    if "0dte_neutral_use_iron_butterfly" not in src:
        errors.append("strategy_engine: 0DTE neutral Iron Butterfly not present")
    if "elevated_vix_narrow_or_straddle_allowed" not in src:
        errors.append("strategy_engine: straddle ELEVATED VIX exception not present")
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
    if "iv_expanding_cooldown" not in src:
        errors.append("strategy_engine: IV expanding cooldown missing")

    src = read("market_data_engine.py")
    if "tolerance: float = 0.05" not in src:
        errors.append("market_data_engine: 25-delta tolerance not 0.05")
    if "buy_ok = True\n            vol_score = min(vol_score, -1)" not in src:
        errors.append("market_data_engine: buy_ok=True for SUPPRESSED not present")
    if "direction_score >= 1.2" not in src:
        errors.append("market_data_engine: direction thresholds not 1.2")
    if "pcr_baseline_set_at_10am" not in src:
        errors.append("market_data_engine: PCR 10AM baseline not present")
    if "vrp_blend_weight" not in src:
        errors.append("market_data_engine: VRP intraday blend not present")
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
    if "or_position_trend_override" not in src:
        errors.append("market_data_engine: OR breakout thresholds missing")

    src = read("nifty_algo_core.py")
    if "for key, val in raw.items():" not in src:
        errors.append("nifty_algo_core: event loader fix missing")

    if errors:
        print("\nVERIFICATION FAILED:")
        for e in errors:
            print("  ERROR: " + e)
        sys.exit(1)
    else:
        print("\nAll patches from patch1 through patch13 verified successfully.")
        print("Engine is fully patched.")


if __name__ == "__main__":
    print("Applying patch13c...")
    patch_25delta_tolerance()
    patch_buy_ok_suppressed_vix()
    verify_patches()
    print("\nDone.")