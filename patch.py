# patch9.py
from __future__ import annotations
import ast
import shutil
import sys
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"_patch9_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
_errors = []
_applied = []
_skipped = []

def backup(fp):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fp, BACKUP_DIR / Path(fp).name)

def read(fp):
    return Path(fp).read_text(encoding="utf-8")

def write(fp, content):
    backup(Path(fp))
    Path(fp).write_text(content, encoding="utf-8")

def check_syntax(fp):
    src = Path(fp).read_text(encoding="utf-8")
    try:
        ast.parse(src)
        return True
    except SyntaxError as e:
        msg = f"SYNTAX ERROR in {Path(fp).name} line {e.lineno}: {e.msg}"
        _errors.append(msg)
        print(f"  [FAIL] {msg}")
        shutil.copy2(BACKUP_DIR / Path(fp).name, fp)
        return False

def rb(src, old, new, label):
    if old not in src:
        _skipped.append(label)
        print(f"  [SKIP] Not found: {label}")
        return src
    result = src.replace(old, new, 1)
    _applied.append(label)
    print(f"  [OK] {label}")
    return result

print("=" * 68)
print("PATCH 9 - VOL REGIME INPUTS AND STRUCTURAL FIXES")
print(f"Base: {BASE}")
print("=" * 68)

re_path  = BASE / "regime_engine.py"
ee_path  = BASE / "execution_engine.py"
mde_path = BASE / "market_data_engine.py"
me_path  = BASE / "main.py"
se_path  = BASE / "strategy_engine.py"

print("\n[1/5] regime_engine.py")
src = read(re_path)

src = rb(src,
'    def _calculate_ivr(self, current_iv_pct: float) -> float:\n        try:\n            df = self.db.get_vix_history(days=365)\n            if df.empty or len(df) < 20:\n                return 50.0\n            vals = df["vix_value"].dropna().values\n            lo = np.percentile(vals, 5)\n            hi = np.percentile(vals, 95)\n            if hi <= lo:\n                return 50.0\n            return float(np.clip((current_iv_pct - lo) / (hi - lo) * 100.0, 0, 100))\n        except Exception:\n            return 50.0',
'    def _calculate_ivr(self, current_iv_pct: float) -> float:\n        try:\n            df = self.db.get_vix_history(days=365)\n            if df.empty or len(df) < 20:\n                return 50.0\n            vals = df["vix_value"].dropna().values\n            vals = vals[(vals > 8.0) & (vals < 90.0)]\n            if len(vals) < 20:\n                return 50.0\n            _adj_iv = current_iv_pct / 1.15\n            lo = np.percentile(vals, 5)\n            hi = np.percentile(vals, 95)\n            if hi <= lo:\n                return 50.0\n            return float(np.clip((_adj_iv - lo) / (hi - lo) * 100.0, 0, 100))\n        except Exception:\n            return 50.0',
"P9-2a: IVR ranks ATM_IV/1.15 against VIX history (DTE1 ATM IV structurally above VIX by ~15pct)")

src = rb(src,
'        iv_hv_s = self._t("iv_hv_sell_threshold", "iv_hv_sell")\n        scores.append(-1 if iv_hv > iv_hv_s else -0.5 if iv_hv > self.config.iv_hv_neutral else 0 if iv_hv > self.config.iv_hv_buy else 1)',
'        iv_hv_s = self._t("iv_hv_sell_threshold", "iv_hv_sell")\n        if iv_hv is None or iv_hv <= 0:\n            scores.append(0)\n        else:\n            scores.append(-1 if iv_hv > iv_hv_s else -0.5 if iv_hv > self.config.iv_hv_neutral else 0 if iv_hv > self.config.iv_hv_buy else 1)',
"P9-2b: IV/HV missing data (1.0 default) scores 0 neutral not -0.5 sell")

src = rb(src,
'    def _calculate_iv_hv_ratio(self, current_iv_pct: float) -> float:\n        try:\n            df = self.db.get_spot_history(days=self.config.hv_lookback_days + 10)\n            if df.empty or "close" not in df.columns:\n                return 1.0\n            daily_closes = df.groupby("date")["close"].last().sort_index().values[-self.config.hv_lookback_days:]\n            if len(daily_closes) < 5:\n                return 1.0\n            valid = daily_closes[daily_closes > 0]\n            if len(valid) < 5:\n                return 1.0\n            hv = float(np.std(np.diff(np.log(valid))) * np.sqrt(252) * 100)\n            return current_iv_pct / hv if hv > 0 else 1.0\n        except Exception:\n            return 1.0',
'    def _calculate_iv_hv_ratio(self, current_iv_pct: float) -> float:\n        try:\n            df = self.db.get_spot_history(days=self.config.hv_lookback_days + 10)\n            if df.empty or "close" not in df.columns:\n                return None\n            daily_closes = df.groupby("date")["close"].last().sort_index().values[-self.config.hv_lookback_days:]\n            if len(daily_closes) < 5:\n                return None\n            valid = daily_closes[daily_closes > 0]\n            if len(valid) < 5:\n                return None\n            hv = float(np.std(np.diff(np.log(valid))) * np.sqrt(252) * 100)\n            return current_iv_pct / hv if hv > 0 else None\n        except Exception:\n            return None',
"P9-2b2: _calculate_iv_hv_ratio returns None when data unavailable (not 1.0 which scores -0.5)")

src = rb(src,
'            same = df[df["weekday"] == weekday]["realized_move"].dropna()\n            if len(same) < 5:\n                if "day_range_points" in df.columns:\n                    same_r = df[df["weekday"] == weekday]["day_range_points"].dropna()\n                    if len(same_r) >= 5:\n                        avg_r = same_r.mean()\n                        avg = avg_r / 1.6 if avg_r > 0 else 0',
'            same_r = df[df["weekday"] == weekday]["day_range_points"].dropna() if "day_range_points" in df.columns else None\n            if same_r is not None and len(same_r) >= 5:\n                avg_r = same_r.mean()\n                avg = avg_r * 0.70 if avg_r > 0 else 0\n                if dte == 0 and avg > 0:\n                    from datetime import datetime as _dt\n                    _now = now_ist()\n                    _remaining_min = max(0, (_dt.combine(_now.date(), time(15, 30)) - _now).total_seconds() / 60.0)\n                    _remaining_frac = _remaining_min / 375.0\n                    import math as _math\n                    avg = avg * _math.sqrt(max(_remaining_frac, 0.05))\n                return straddle / avg if avg > 0 else 1.0\n            same = df[df["weekday"] == weekday]["realized_move"].dropna()\n            if len(same) < 5:\n                if "day_range_points" in df.columns:\n                    same_r2 = df[df["weekday"] == weekday]["day_range_points"].dropna()\n                    if len(same_r2) >= 5:\n                        avg_r = same_r2.mean()\n                        avg = avg_r / 1.6 if avg_r > 0 else 0',
"P9-2c: straddle ratio uses day_range_points*0.70 as primary benchmark (straddle prices range not displacement)")

write(re_path, src)
ok1 = check_syntax(re_path)
if ok1:
    print("  regime_engine.py syntax OK")

print("\n[2/5] execution_engine.py")
src = read(ee_path)

src = rb(src,
'        if strategy_type == "SELL" and position.get("entry_credit") and position["entry_credit"] > 0:\n            is_dir = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")\n            if is_dir:\n                gc = position.get("gross_credit") or position["entry_credit"]\n                credit_stop_limit = gc * 2.2\n                if current_premium >= credit_stop_limit:\n                    return "CLOSE_STOP", {"current_premium": current_premium}\n            else:\n                credit_stop_limit = position["entry_credit"] * 1.6\n                actual_stop = min(\n                    effective_stop if effective_stop is not None else credit_stop_limit,\n                    credit_stop_limit\n                )\n                if current_premium >= actual_stop:\n                    return "CLOSE_STOP", {"current_premium": current_premium}',
'        if strategy_type == "SELL" and position.get("entry_credit") and position["entry_credit"] > 0:\n            is_dir = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")\n            if is_dir:\n                gc = position.get("gross_credit") or position["entry_credit"]\n                credit_stop_limit = gc * 2.2\n                if current_premium >= credit_stop_limit:\n                    return "CLOSE_STOP", {"current_premium": current_premium}\n            elif strategy_name not in ("IRON_CONDOR", "IRON_BUTTERFLY"):\n                credit_stop_limit = position["entry_credit"] * 1.6\n                actual_stop = min(\n                    effective_stop if effective_stop is not None else credit_stop_limit,\n                    credit_stop_limit\n                )\n                if current_premium >= actual_stop:\n                    return "CLOSE_STOP", {"current_premium": current_premium}',
"P9-A: IC/IB skip credit-multiple stop — delta/spot/time exits are primary authority for multi-leg structures")

src = rb(src,
'        elif reason in ("CLOSE_ADX", "CLOSE_VWAP", "CLOSE_DELTA"):\n            state["last_stop_time"]   = now_ist().isoformat()\n            state["last_stop_reason"] = reason',
'        elif reason in ("CLOSE_ADX", "CLOSE_VWAP", "CLOSE_DELTA"):\n            pass',
"P9-G: CLOSE_ADX/VWAP/DELTA do not set last_stop_time — only real CLOSE_STOP triggers cooldown")

write(ee_path, src)
ok2 = check_syntax(ee_path)
if ok2:
    print("  execution_engine.py syntax OK")

print("\n[3/5] market_data_engine.py")
src = read(mde_path)

src = rb(src,
'        if current_time >= dtime(10, 15) and not self.state.get("or_computed"):',
'        if current_time >= dtime(9, 30) and not self.state.get("or_computed"):',
"P9-F: ORB gate 10:15->09:30 opens 09:40-10:30 entry window (best NIFTY sell window)")

src = rb(src,
'        if current_time >= dtime(10, 15) and not self.state.get("session_initialized"):',
'        if current_time >= dtime(9, 30) and not self.state.get("session_initialized"):',
"P9-F2: session_initialized gate 10:15->09:30 consistent with ORB gate")

write(mde_path, src)
ok3 = check_syntax(mde_path)
if ok3:
    print("  market_data_engine.py syntax OK")

print("\n[4/5] strategy_engine.py")
src = read(se_path)

src = rb(src,
'                state.get("hard_exit_time", "15:25"), "%H:%M"\n            ).time()\n        except Exception:\n            hard_exit = self.config.hard_exit_time\n        mins_to_exit = self._time_diff_minutes(current_time, hard_exit)\n        if mins_to_exit < 90:',
'                state.get("hard_exit_time", "15:15"), "%H:%M"\n            ).time()\n        except Exception:\n            hard_exit = self.config.hard_exit_time\n        mins_to_exit = self._time_diff_minutes(current_time, hard_exit)\n        if mins_to_exit < 90:',
"P9-F3: hard exit default 15:25->15:15 in strategy gate (single source of truth)")

src = rb(src,
'                    state.get("hard_exit_time", "15:25"), "%H:%M"\n                ).time()\n            except Exception:\n                hard_exit = self.config.hard_exit_time\n            mins = self._time_diff_minutes(current_time, hard_exit)',
'                    state.get("hard_exit_time", "15:15"), "%H:%M"\n                ).time()\n            except Exception:\n                hard_exit = self.config.hard_exit_time\n            mins = self._time_diff_minutes(current_time, hard_exit)',
"P9-F3b: IRON_CONDOR hard exit default 15:25->15:15")

write(se_path, src)
ok4 = check_syntax(se_path)
if ok4:
    print("  strategy_engine.py syntax OK")

print("\n[5/5] main.py")
src = read(me_path)

src = rb(src,
'                _cal_interval = max(int(getattr(self.config, "calibration_interval_sec", 3600)), 3600)\n                if (now_mono - self._last_calibration_time) >= _cal_interval:\n                    self._last_calibration_time = now_mono\n                    self._run_calibration_cycle()',
'                pass',
"P9-C: remove intraday hourly calibration — calibrate only at startup and EOD from daily data")

src = rb(src,
'            "opening_straddle": self.market_engine.state.get("_straddle_open_for_summary", 0),',
'            "opening_straddle": self.market_engine.state.get("_straddle_open_for_summary", 0),\n            "opening_iv_pct": round((self.market_engine.state.get("opening_iv") or 0) * 100.0, 3),',
"P9-I: persist opening_iv_pct to daily_summary for IVR calibration")

write(me_path, src)
ok5 = check_syntax(me_path)
if ok5:
    print("  main.py syntax OK")

print("\n" + "=" * 68)
print("PATCH 9 SUMMARY")
print("=" * 68)
print(f"Applied : {len(_applied)}")
for a in _applied:
    print(f"  [OK] {a}")
print(f"Skipped : {len(_skipped)}")
for s in _skipped:
    print(f"  [SKIP] {s}")
print(f"Errors  : {len(_errors)}")
for e in _errors:
    print(f"  [ERR] {e}")
print(f"Backups : {BACKUP_DIR}")

if _errors:
    print("\n[FAIL]")
    sys.exit(1)
else:
    print("\n[SUCCESS] Patch 9 complete.")
    sys.exit(0)