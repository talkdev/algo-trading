import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def patch_sizing_contractual_max_loss():
    filename = "strategy_engine.py"
    src = read(filename)

    if "contractual_max_loss" in src:
        print("strategy_engine.py contractual max loss sizing already patched — skipping")
        return

    old = (
        "        if strategy_type == \"SELL\":\n"
        "            stop_premium = net_credit * stop_multiplier\n"
        "            # RISK FIX: 1.25x buffer on the theoretical stop-triggered loss so\n"
        "            # sizing doesn't understate risk if the exit fill is worse than\n"
        "            # the theoretical stop price (gap/slippage on a fast move).\n"
        "            max_loss_per_lot = (stop_premium - net_credit) * C02 * 1.25\n"
        "        else:\n"
        "            max_loss_per_lot = net_debit * 0.50 * C02 * 1.25"
    )
    new = (
        "        if strategy_type == \"SELL\":\n"
        "            stop_premium = net_credit * stop_multiplier\n"
        "            stop_based_loss = (stop_premium - net_credit) * C02 * 1.25\n"
        "            if actual_wing_pts is not None and actual_wing_pts > 0 and net_credit > 0:\n"
        "                contractual_max_loss = (actual_wing_pts - net_credit) * C02\n"
        "                max_loss_per_lot = min(stop_based_loss, contractual_max_loss) if contractual_max_loss > 0 else stop_based_loss\n"
        "            else:\n"
        "                max_loss_per_lot = stop_based_loss\n"
        "        else:\n"
        "            max_loss_per_lot = net_debit * 0.50 * C02 * 1.25"
    )
    assert old in src, "patch_sizing_contractual_max_loss not found even with comments"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched strategy_engine.py sizing uses min(stop-based, contractual max loss)")


def patch_min_credits_viable_2026():
    filename = "strategy_engine.py"
    src = read(filename)

    if '"BULL_PUT_SPREAD": 15' in src:
        print("strategy_engine.py MIN_CREDITS already at viable 2026 levels — skipping")
        return

    old = (
        "MIN_CREDITS = {\n"
        "    \"IRON_BUTTERFLY\": 18, \"IRON_CONDOR\": 12,\n"
        "    \"BULL_PUT_SPREAD\": 10, \"BEAR_CALL_SPREAD\": 9, \"POST_EVENT_STRADDLE\": 22,\n"
        "}"
    )
    new = (
        "MIN_CREDITS = {\n"
        "    \"IRON_BUTTERFLY\": 20, \"IRON_CONDOR\": 18,\n"
        "    \"BULL_PUT_SPREAD\": 15, \"BEAR_CALL_SPREAD\": 14, \"POST_EVENT_STRADDLE\": 25,\n"
        "}"
    )
    assert old in src, "patch_min_credits_viable_2026 not found"
    src = src.replace(old, new, 1)

    old = (
        "MIN_CREDITS_TUESDAY = {\n"
        "    \"IRON_BUTTERFLY\": 22, \"IRON_CONDOR\": 14, \"BULL_PUT_SPREAD\": 11, \"BEAR_CALL_SPREAD\": 10,\n"
        "}"
    )
    new = (
        "MIN_CREDITS_TUESDAY = {\n"
        "    \"IRON_BUTTERFLY\": 25, \"IRON_CONDOR\": 18, \"BULL_PUT_SPREAD\": 16, \"BEAR_CALL_SPREAD\": 15,\n"
        "}"
    )
    assert old in src, "patch_min_credits_tuesday_viable_2026 not found"
    src = src.replace(old, new, 1)

    write(filename, src)
    print("patched strategy_engine.py MIN_CREDITS raised to viable 2026 levels")


def patch_or_breakout_direction_signal():
    filename = "market_data_engine.py"
    src = read(filename)

    if "or_breakout_score" in src:
        print("market_data_engine.py OR breakout signal already patched — skipping")
        return

    old = (
        "        if vwap_dist_pct is None:\n"
        "            vwap_signal, vwap_score = \"UNKNOWN\", 0\n"
        "        elif vwap_dist_pct > 0.50: vwap_signal, vwap_score = \"BULLISH_EXTENDED\", 1\n"
        "        elif vwap_dist_pct > 0.15: vwap_signal, vwap_score = \"BULLISH\", 1\n"
        "        elif vwap_dist_pct > -0.15: vwap_signal, vwap_score = \"NEUTRAL\", 0\n"
        "        elif vwap_dist_pct > -0.50: vwap_signal, vwap_score = \"BEARISH\", -1\n"
        "        else: vwap_signal, vwap_score = \"BEARISH_EXTENDED\", -1"
    )
    new = (
        "        if vwap_dist_pct is None:\n"
        "            vwap_signal, vwap_score = \"UNKNOWN\", 0\n"
        "        elif vwap_dist_pct > 0.50: vwap_signal, vwap_score = \"BULLISH_EXTENDED\", 1\n"
        "        elif vwap_dist_pct > 0.15: vwap_signal, vwap_score = \"BULLISH\", 1\n"
        "        elif vwap_dist_pct > -0.15: vwap_signal, vwap_score = \"NEUTRAL\", 0\n"
        "        elif vwap_dist_pct > -0.50: vwap_signal, vwap_score = \"BEARISH\", -1\n"
        "        else: vwap_signal, vwap_score = \"BEARISH_EXTENDED\", -1\n"
        "\n"
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
    assert old in src, "patch_or_breakout_direction_signal vwap block not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched market_data_engine.py OR breakout as direction signal when VWAP unavailable")


def patch_credit_stop_tighter():
    filename = "execution_engine.py"
    src = read(filename)

    if "_credit_stop_limit = position[\"entry_credit\"] * 1.5" in src:
        print("execution_engine.py credit stop already 1.5x — skipping")
        return

    old = "            _credit_stop_limit = position[\"entry_credit\"] * 2.5"
    new = "            _credit_stop_limit = position[\"entry_credit\"] * 1.5"
    assert old in src, "patch_credit_stop_tighter not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched execution_engine.py credit stop tightened from 2.5x to 1.5x")


def patch_time_decay_target_tighter():
    filename = "execution_engine.py"
    src = read(filename)

    if "_time_target_pct = 0.25" in src:
        print("execution_engine.py time decay target already has 0.25 — skipping")
        return

    old = (
        "            if _now_time_td >= dtime(13, 30):\n"
        "                _time_target_pct = 0.30\n"
        "            elif _now_time_td >= dtime(13, 0):\n"
        "                _time_target_pct = 0.40\n"
        "            else:\n"
        "                _time_target_pct = None"
    )
    new = (
        "            if _now_time_td >= dtime(13, 30):\n"
        "                _time_target_pct = 0.25\n"
        "            elif _now_time_td >= dtime(13, 0):\n"
        "                _time_target_pct = 0.30\n"
        "            elif _now_time_td >= dtime(12, 0):\n"
        "                _time_target_pct = 0.32\n"
        "            else:\n"
        "                _time_target_pct = None"
    )
    assert old in src, "patch_time_decay_target_tighter not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("patched execution_engine.py time-decay targets tightened for afternoon")


def verify_patches():
    errors = []

    src = read("strategy_engine.py")
    if '"MONDAY": 0.32' not in src:
        errors.append("strategy_engine: TARGET_PCT not 0.32")
    if '"TUESDAY": 0.30' not in src:
        errors.append("strategy_engine: TARGET_PCT TUESDAY not 0.30")
    if "bullish_strong_trend_put_credit_safe_side" not in src:
        errors.append("strategy_engine: STRONG_TREND bull put spread not added")
    if "bearish_strong_trend_call_credit_safe_side" not in src:
        errors.append("strategy_engine: STRONG_TREND bear call spread not added")
    if "contractual_max_loss" not in src:
        errors.append("strategy_engine: contractual max loss sizing not added")
    if '"BULL_PUT_SPREAD": 15' not in src:
        errors.append("strategy_engine: MIN_CREDITS BULL_PUT not 15")
    if '"IRON_CONDOR": 18' not in src:
        errors.append("strategy_engine: MIN_CREDITS IRON_CONDOR not 18")
    if "bullish_trend_sell_puts_aligned" not in src:
        errors.append("strategy_engine: trend-side selection fix missing")
    if "fair_vrp_mild_trend" not in src:
        errors.append("strategy_engine: FAIR VRP mild trend block missing")

    src = read("execution_engine.py")
    if "_credit_stop_limit = position[\"entry_credit\"] * 1.5" not in src:
        errors.append("execution_engine: credit stop not 1.5x")
    if "_time_target_pct = 0.25" not in src:
        errors.append("execution_engine: afternoon time target not 0.25")
    if "_vwap_exits_active" not in src:
        errors.append("execution_engine: VWAP exit CAS disable missing")
    if "mid = (bid + ask) / 2.0" not in src:
        errors.append("execution_engine: mid-price limit order missing")

    src = read("market_data_engine.py")
    if "or_breakout_score" not in src:
        errors.append("market_data_engine: OR breakout direction signal not added")
    if "if vix < 14.0: return \"LOW\"" not in src:
        errors.append("market_data_engine: VIX regime thresholds missing")
    if "rolling_intraday" not in src:
        errors.append("market_data_engine: Parkinson rolling missing")
    if "equal_pv / total_bars" not in src:
        errors.append("market_data_engine: price-anchored VWAP fallback missing")

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
    print("Applying patch10 (final)...")
    patch_sizing_contractual_max_loss()
    patch_min_credits_viable_2026()
    patch_or_breakout_direction_signal()
    patch_credit_stop_tighter()
    patch_time_decay_target_tighter()
    verify_patches()
    print("\nDone. All patch10 changes applied.")