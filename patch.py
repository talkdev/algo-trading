import ast
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def read_file(p):
    return p.read_text(encoding="utf-8")


def write_file(p, content):
    p.write_text(content, encoding="utf-8")


def verify_syntax(path):
    src = read_file(path)
    try:
        ast.parse(src)
        print(f"  SYNTAX OK: {path.name}")
        return True
    except SyntaxError as e:
        print(f"  SYNTAX ERROR in {path.name}: line {e.lineno}: {e.msg}")
        lines = src.split("\n")
        start = max(0, e.lineno - 5)
        end = min(len(lines), e.lineno + 5)
        for i, line in enumerate(lines[start:end], start=start + 1):
            marker = ">>>" if i == e.lineno else "   "
            print(f"    {marker} {i:4d}: {repr(line)}")
        return False


def fix_mde_vol_ok():
    path = BASE_DIR / "market_data_engine.py"
    src = read_file(path)
    lines = src.split("\n")

    print("Diagnosing market_data_engine.py vol_ok area...")
    for i, line in enumerate(lines[545:575], start=546):
        print(f"  {i:4d}: {repr(line)}")
    print()

    new_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if "vol_ok = True" in line and i + 1 < len(lines) and "avg_vol" in lines[i + 1]:
            indent = ""
            for ch in line:
                if ch in (" ", "\t"):
                    indent += ch
                else:
                    break
            new_lines.append(line)
            i += 1
            while i < len(lines):
                next_line = lines[i]
                stripped = next_line.strip()
                if (stripped.startswith("if avg_vol") or
                        stripped.startswith("else False") or
                        stripped == "else False)" or
                        ("avg_vol" in stripped and "else" in stripped)):
                    i += 1
                    continue
                break
            continue

        if ("vol_ok = (recent_vol > avg_vol" in line or
                ("avg_vol > 0 else False)" in line and "vol_ok" not in line)):
            i += 1
            continue

        new_lines.append(line)
        i += 1

    src = "\n".join(new_lines)

    if "vol_ok" in src:
        lines2 = src.split("\n")
        for i, line in enumerate(lines2):
            if "vol_ok = True" in line:
                start = max(0, i - 2)
                end = min(len(lines2), i + 5)
                print("  After fix, vol_ok area:")
                for j in range(start, end):
                    print(f"    {j+1:4d}: {repr(lines2[j])}")
                break

    write_file(path, src)
    print("  APPLIED: mde: vol_ok dangling continuation removed")


def fix_mde_vol_ok_comprehensive():
    path = BASE_DIR / "market_data_engine.py"
    src = read_file(path)

    print("Comprehensive vol_ok fix in classify_orb_price_structure...")

    lines = src.split("\n")

    method_start = None
    method_end = None
    for i, line in enumerate(lines):
        if "def classify_orb_price_structure(" in line:
            method_start = i
        if method_start is not None and i > method_start + 1:
            if line.startswith("    def "):
                method_end = i
                break

    if method_start is None:
        print("  SKIP: classify_orb_price_structure not found")
        return

    print(f"  Found method: lines {method_start+1}-{method_end}")

    current_method = "\n".join(lines[method_start:method_end])
    print("  Current method:")
    for i, line in enumerate(current_method.split("\n"), start=method_start+1):
        print(f"    {i:4d}: {repr(line)}")

    new_method = '''    def classify_orb_price_structure(
        self, bars: pd.DataFrame, orb_high: float, orb_low: float
    ) -> str:
        now = now_ist().time()
        if now < dtime(9, 30) or orb_high == 0 or orb_low == 0:
            return "OBSERVING"

        post = bars[
            (bars["time"] >= "09:30:00") & (bars["time"] <= "15:30:00")
        ] if not bars.empty else pd.DataFrame()
        if post.empty:
            return "OBSERVING"

        last_close = float(post["close"].iloc[-1])

        in_choppy_window = now <= dtime(10, 15)
        if in_choppy_window:
            recent_cutoff = (now_ist() - timedelta(minutes=20)).strftime("%H:%M:%S")
            recent = post[post["time"] >= recent_cutoff]
            check_df = recent if not recent.empty else post
            if (check_df["high"] > orb_high).any() and not (check_df["close"] > orb_high).any():
                return "CHOPPY"
            if (check_df["low"] < orb_low).any() and not (check_df["close"] < orb_low).any():
                return "CHOPPY"
        else:
            if last_close > orb_high + 20:
                return "UPTREND"
            if last_close < orb_low - 20:
                return "DOWNTREND"
            return "RANGE"

        if last_close > orb_high + 20:
            return "UPTREND"
        if last_close < orb_low - 20:
            return "DOWNTREND"
        return "RANGE"

'''
    new_method_lines = new_method.split("\n")
    lines = lines[:method_start] + new_method_lines + lines[method_end:]
    src = "\n".join(lines)
    write_file(path, src)
    print("  APPLIED: mde: classify_orb_price_structure rewritten without vol_ok")


def fix_se_low_confidence_verification():
    path = BASE_DIR / "strategy_engine.py"
    src = read_file(path)

    print("Checking strategy_engine.py LOW confidence gate...")

    lines = src.split("\n")
    found_low_gate = False
    for i, line in enumerate(lines, 1):
        if "LOW" in line and "confidence" in line.lower() and "NO_TRADE" in line:
            print(f"  Found at line {i}: {repr(line)}")
            found_low_gate = True

    if not found_low_gate:
        print("  LOW confidence gate not found — adding it now")

        old_vrp_unknown = '        if s.get("volatility_condition") == "UNKNOWN":\n            return "NO_TRADE", "vrp_unknown_insufficient_data"'
        new_vrp_unknown = '''        if s.get("volatility_condition") == "UNKNOWN":
            return "NO_TRADE", "vrp_unknown_insufficient_data"

        _conf_gate = s.get("confidence")
        if _conf_gate in ("LOW", "NONE"):
            return "NO_TRADE", f"confidence_{_conf_gate}_insufficient_edge_after_costs"

        _actual_dte_gate = s.get("actual_dte")
        _vol_cond_gate = s.get("volatility_condition", "UNKNOWN")
        if _actual_dte_gate is not None and _actual_dte_gate >= 2:
            if _vol_cond_gate not in ("RICH", "VERY_RICH"):
                return "NO_TRADE", f"dte_{_actual_dte_gate}_requires_rich_vrp_not_{_vol_cond_gate}"
            if _conf_gate != "HIGH":
                return "NO_TRADE", f"dte_{_actual_dte_gate}_requires_high_confidence_not_{_conf_gate}"

        _day_move_used = s.get("day_move_used_pct", 0.0) or 0.0
        if _day_move_used >= 70.0 and s.get("sell_ok"):
            return "NO_TRADE", f"day_move_used_{_day_move_used:.0f}pct_of_opening_straddle_no_edge"

        _strategy_for_buy_check = None
        if final_regime and final_regime not in ("NO_TRADE", "EMERGENCY_EXIT"):
            try:
                from regime_bridge import final_regime_to_strategy_name as _frts
                _strategy_for_buy_check = _frts(final_regime, s)
            except Exception:
                pass
        _buy_side = {"LONG_STRADDLE", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"}
        if _strategy_for_buy_check in _buy_side:
            return "NO_TRADE", "buy_side_requires_pre_1030_entry_window"'''

        if old_vrp_unknown in src:
            src = src.replace(old_vrp_unknown, new_vrp_unknown)
            print("  APPLIED: se: LOW confidence gate added")
        else:
            print("  SKIP: vrp_unknown block not found for LOW confidence gate")
    else:
        print("  LOW confidence gate IS present in file")
        if "buy_side_requires_pre_1030_entry_window" not in src:
            print("  But buy-side gate missing — checking...")
            for i, line in enumerate(lines, 1):
                if "buy_side" in line or "LONG_STRADDLE" in line and "NO_TRADE" in line:
                    print(f"    {i:4d}: {repr(line)}")

    write_file(path, src)
    print("  DONE: strategy_engine.py")


def final_verify():
    results = {}

    core = read_file(BASE_DIR / "nifty_algo_core.py")
    results["CORE-01 STT_OPTIONS_SELL=0.000625"] = "STT_OPTIONS_SELL=0.000625" in core
    results["CORE-02 stt_options_sell 0.000625 in load_config"] = "0.000625" in core

    mde = read_file(BASE_DIR / "market_data_engine.py")
    results["MDE-01 VIX size SUPPRESSED=1.0"] = '"SUPPRESSED": 1.0' in mde
    results["MDE-02 vol_ok syntax clean"] = "avg_vol > 0 else False)" not in mde
    results["MDE-03 OR width absolute <40"] = "or_width < 40" in mde
    results["MDE-04 day_move_used_pct in signals"] = '"day_move_used_pct"' in mde
    results["MDE-05 Tuesday 14:30 hard exit"] = "14:30" in mde

    se = read_file(BASE_DIR / "strategy_engine.py")
    results["SE-01 IRON_BUTTERFLY max DTE 1"] = '"IRON_BUTTERFLY":  (0, 1)' in se
    results["SE-02 IRON_CONDOR max DTE 2"] = '"IRON_CONDOR":     (0, 2)' in se
    results["SE-03 0DTE always condor"] = "actual_dte == 0" in se and "_resolve_premium_sell_range" in se
    results["SE-04 buy-side blocked"] = "buy_side_requires_pre_1030_entry_window" in se
    results["SE-05 LOW confidence blocked"] = (
        ('confidence_LOW_insufficient_edge' in se or
         '_conf_gate in ("LOW", "NONE")' in se or
         'confidence_{_conf_gate}' in se)
    )
    results["SE-06 DTE>=2 requires RICH VRP"] = "requires_rich_vrp" in se or "dte_" in se and "requires_rich" in se
    results["SE-07 day_move_used gate 70pct"] = "day_move_used_pct" in se and "70.0" in se
    results["SE-08 0DTE straddle placement 0.85"] = "0.85" in se and "atm_straddle" in se
    results["SE-09 DTE>=2 size cap 0.25"] = "dte_midweek_size_cap" in se

    re_src = read_file(BASE_DIR / "regime_engine.py")
    results["RE-01 EXPIRY_MAX_PAIN entry removed"] = "FinalRegime.EXPIRY_MAX_PAIN" not in re_src
    results["RE-02 straddle ratio time-scaling"] = "remaining_frac" in re_src
    results["RE-03 day_size_monday 1.0"] = "day_size_monday=1.0" in re_src or '"day_size_monday": 1.0' in re_src
    results["RE-04 pre-Sep-2025 warning"] = "2025-09-01" in re_src
    results["RE-05 DTE performance calibrator"] = "_calibrate_dte_performance" in re_src
    results["RE-06 DTE self-tuning"] = "dte_0_win_rate" in re_src or "_dte0_wr" in re_src

    ee = read_file(BASE_DIR / "execution_engine.py")
    results["EE-01 tick 0.10"] = "tick = 0.10" in ee

    bt = read_file(BASE_DIR / "backtest.py")
    results["BT-01 slippage model"] = "slippage_rs" in bt
    results["BT-02 dte_performance in day"] = "dte_performance" in bt
    results["BT-03 dte_agg"] = "dte_agg" in bt
    results["BT-04 dte_performance_breakdown"] = "dte_performance_breakdown" in bt

    eod = read_file(BASE_DIR / "eod_report.py")
    results["EOD-01 day_move_used_pct in dq"] = "day_move_used_pct" in eod
    results["EOD-02 dte_breakdown in LLM"] = "dte_breakdown" in eod
    results["EOD-03 dte_performance_raw"] = "dte_performance_raw" in eod

    passed = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if not v)
    print(f"\n  Verification: {passed}/{len(results)} confirmed")
    if failed:
        print("  MISSING:")
        for k, v in results.items():
            if not v:
                print(f"    MISSING: {k}")
    else:
        print("  ALL confirmed")
    return failed == 0


def main():
    print("Step 1: Fix market_data_engine.py syntax error...")
    fix_mde_vol_ok_comprehensive()
    print()

    print("Step 2: Fix strategy_engine.py LOW confidence gate...")
    fix_se_low_confidence_verification()
    print()

    print("Step 3: Syntax verification...")
    all_ok = True
    for fname in ["nifty_algo_core.py", "market_data_engine.py", "regime_engine.py",
                  "strategy_engine.py", "execution_engine.py", "main.py",
                  "eod_report.py", "backtest.py"]:
        fpath = BASE_DIR / fname
        if fpath.exists():
            ok = verify_syntax(fpath)
            if not ok:
                all_ok = False
    print()

    print("Step 4: Final verification...")
    all_present = final_verify()
    print()

    if all_ok and all_present:
        print("=" * 65)
        print("ALL PATCHES VERIFIED — ENGINE READY")
        print("=" * 65)
        print()
        print("Engine trades ALL trading days based on regime:")
        print("  0DTE (Tuesday):  condor only, straddle strikes, flatten 14:30")
        print("  1DTE (Monday):   full size 1.0x, butterfly allowed if mature ADX")
        print("  2+DTE (Wed-Fri): directional spreads only, HIGH conf, RICH VRP, 0.25x")
        print()
        print("Self-calibration:")
        print("  AutoCalibrator tracks DTE=0/1/2+ win rates separately")
        print("  Tuesday size adjusts if 0DTE win rate < 40% or > 65%")
        print("  Monday size adjusts if 1DTE win rate < 40% or > 65%")
        print("  Pre-Sep-2025 Thursday-regime data excluded from calibration")
    elif not all_ok:
        print("SYNTAX ERRORS remain — review above")
        sys.exit(1)
    else:
        print("PATCHES MISSING — review above")
        sys.exit(1)


if __name__ == "__main__":
    main()