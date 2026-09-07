# patch10.py
from __future__ import annotations
import ast
import shutil
import sys
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"_patch10_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
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
print("PATCH 10 - CRITICAL EOD FIX + SIGNAL QUALITY")
print(f"Base: {BASE}")
print("=" * 68)

core_path = BASE / "nifty_algo_core.py"
me_path   = BASE / "main.py"
re_path   = BASE / "regime_engine.py"
mde_path  = BASE / "market_data_engine.py"
se_path   = BASE / "strategy_engine.py"
ee_path   = BASE / "execution_engine.py"

print("\n[1/6] nifty_algo_core.py — P10-1a: add opening_iv_pct migration")
src = read(core_path)

src = rb(src,
'    "ALTER TABLE daily_summary ADD COLUMN dominant_regime TEXT",',
'    "ALTER TABLE daily_summary ADD COLUMN dominant_regime TEXT",\n    "ALTER TABLE daily_summary ADD COLUMN opening_iv_pct REAL",',
"P10-1a: add opening_iv_pct to MIGRATION_SQL")

write(core_path, src)
check_syntax(core_path)

print("\n[2/6] main.py — P10-1b: wrap generate_daily_summary + add opening_iv_pct to schema")
src = read(me_path)

src = rb(src,
'        self._run_calibration_cycle(force=True)\n        self._last_calibration_time = time_module.monotonic()\n        self.generate_daily_summary()\n        self._eod_done = True',
'        self._run_calibration_cycle(force=True)\n        self._last_calibration_time = time_module.monotonic()\n        try:\n            self.generate_daily_summary()\n        except Exception as _eod_e:\n            self.logger.error(f"EOD summary error (non-fatal): {_eod_e}", exc_info=True)\n        self._eod_done = True',
"P10-1b: wrap generate_daily_summary in try/except so column errors dont abort EOD tasks")

write(me_path, src)
check_syntax(me_path)

print("\n[3/6] regime_engine.py — P10-2: IVR distinct dates guard")
src = read(re_path)

src = rb(src,
'    def _calculate_ivr(self, current_iv_pct: float) -> float:\n        try:\n            df = self.db.get_vix_history(days=365)\n            if df.empty or len(df) < 20:\n                return 50.0\n            vals = df["vix_value"].dropna().values\n            vals = vals[(vals > 8.0) & (vals < 90.0)]\n            if len(vals) < 20:\n                return 50.0\n            _adj_iv = current_iv_pct / 1.15\n            lo = np.percentile(vals, 5)\n            hi = np.percentile(vals, 95)\n            if hi <= lo:\n                return 50.0\n            return float(np.clip((_adj_iv - lo) / (hi - lo) * 100.0, 0, 100))\n        except Exception:\n            return 50.0',
'    def _calculate_ivr(self, current_iv_pct: float) -> float:\n        try:\n            df = self.db.get_vix_history(days=365)\n            if df.empty:\n                return 50.0\n            n_dates = df["date"].nunique() if "date" in df.columns else 0\n            if n_dates < 30:\n                return 50.0\n            vals = df["vix_value"].dropna().values\n            vals = vals[(vals > 8.0) & (vals < 90.0)]\n            if len(vals) < 50:\n                return 50.0\n            _adj_iv = current_iv_pct / 1.15\n            lo = np.percentile(vals, 5)\n            hi = np.percentile(vals, 95)\n            if hi <= lo:\n                return 50.0\n            return float(np.clip((_adj_iv - lo) / (hi - lo) * 100.0, 0, 100))\n        except Exception:\n            return 50.0',
"P10-2: IVR requires 30 distinct trading dates not just row count — prevents single-session pinning to 100")

src = rb(src,
'            avg = same.mean()\n            if dte == 0 and avg > 0:\n                from datetime import datetime as _dt\n                _now = now_ist()\n                _remaining_min = max(0, (_dt.combine(_now.date(), time(15, 30)) - _now).total_seconds() / 60.0)\n                _remaining_frac = _remaining_min / 375.0\n                avg = avg * _math.sqrt(max(_remaining_frac, 0.05))\n            return straddle / avg if avg > 0 else 1.0\n        except Exception:\n            return 1.0',
'            avg = same.mean()\n            if dte is not None and dte <= 1 and avg > 0:\n                from datetime import datetime as _dt\n                import math as _math\n                _now = now_ist()\n                _remaining_min = max(0, (_dt.combine(_now.date(), time(15, 30)) - _now).total_seconds() / 60.0)\n                _remaining_frac = _remaining_min / 375.0\n                avg = avg * _math.sqrt(max(_remaining_frac, 0.10))\n            return straddle / avg if avg > 0 else 1.0\n        except Exception:\n            return 1.0',
"P10-3: straddle ratio time-scaling for DTE<=1 not just DTE=0 (Monday DTE1 decays through day)")

write(re_path, src)
check_syntax(re_path)

print("\n[4/6] market_data_engine.py — P10-4: CHOPPY window refined")
src = read(mde_path)

src = rb(src,
'        in_choppy_window = now <= dtime(10, 15)\n        if in_choppy_window:\n            recent_cutoff = (now_ist() - timedelta(minutes=20)).strftime("%H:%M:%S")\n            recent = post[post["time"] >= recent_cutoff]\n            check_df = recent if not recent.empty else post\n            if (check_df["high"] > orb_high).any() and not (check_df["close"] > orb_high).any():\n                return "CHOPPY"\n            if (check_df["low"] < orb_low).any() and not (check_df["close"] < orb_low).any():\n                return "CHOPPY"',
'        in_choppy_window = now <= dtime(9, 45)\n        if in_choppy_window:\n            recent_cutoff = (now_ist() - timedelta(minutes=10)).strftime("%H:%M:%S")\n            recent = post[post["time"] >= recent_cutoff]\n            check_df = recent if not recent.empty else post\n            wick_high = int(((check_df["high"] > orb_high) & (check_df["close"] <= orb_high)).sum())\n            wick_low  = int(((check_df["low"] < orb_low)  & (check_df["close"] >= orb_low)).sum())\n            if wick_high >= 3:\n                return "CHOPPY"\n            if wick_low >= 3:\n                return "CHOPPY"',
"P10-4: CHOPPY window 10:15->09:45 requires 3 wick-throughs not 1 (opens 09:45-10:15 entry window)")

write(mde_path, src)
check_syntax(mde_path)

print("\n[5/6] strategy_engine.py — P10-5a: IC/IB straddle-unit price stop")
src = read(se_path)

src = rb(src,
'        if strategy_type == "SELL" and net_credit and net_credit > 0:\n            credit_stop_mult = 1.8 if actual_dte == 0 else (1.6 if actual_dte == 1 else 1.5)\n            credit_stop = net_credit * credit_stop_mult\n            static_stop = PRICE_STOPS.get(strategy_name, 80)\n            if actual_dte == 0:\n                static_stop = int(static_stop * 0.60)\n            elif actual_dte == 1:\n                static_stop = int(static_stop * 0.75)\n            elif actual_dte <= 3:\n                static_stop = int(static_stop * 0.90)\n            price_stop_pts = int(min(static_stop, max(credit_stop * 2.0, 30)))\n        else:\n            price_stop_pts = PRICE_STOPS.get(strategy_name, 80)\n            if actual_dte == 0:\n                price_stop_pts = 35',
'        _opening_straddle_ref = s.get("atm_straddle_price") or 0\n        if strategy_type == "SELL" and net_credit and net_credit > 0:\n            _is_ic_ib = strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY")\n            if _is_ic_ib and _opening_straddle_ref > 20:\n                price_stop_pts = max(int(_opening_straddle_ref * 0.30), 40)\n            else:\n                credit_stop_mult = 1.8 if actual_dte == 0 else (1.6 if actual_dte == 1 else 1.5)\n                credit_stop = net_credit * credit_stop_mult\n                static_stop = PRICE_STOPS.get(strategy_name, 80)\n                if actual_dte == 0:\n                    static_stop = int(static_stop * 0.60)\n                elif actual_dte == 1:\n                    static_stop = int(static_stop * 0.75)\n                elif actual_dte <= 3:\n                    static_stop = int(static_stop * 0.90)\n                price_stop_pts = int(min(static_stop, max(credit_stop * 2.0, 30)))\n        else:\n            price_stop_pts = PRICE_STOPS.get(strategy_name, 80)\n            if actual_dte == 0:\n                price_stop_pts = 35',
"P10-5a: IC/IB price_stop = 0.30*opening_straddle scales with vol and DTE not fixed pts")

write(se_path, src)
check_syntax(se_path)

print("\n[6/6] execution_engine.py — P10-5b: IC/IB spot stop vs short strike not entry")
src = read(ee_path)

src = rb(src,
'        price_stop_pts = position.get("price_stop_pts") or 0\n        spot = signals.get("spot")\n        if price_stop_pts > 0 and spot is not None and position.get("entry_spot"):\n            spot_move = spot - position["entry_spot"]\n            if strategy_name == "BULL_PUT_SPREAD":\n                triggered = spot_move <= -price_stop_pts\n            elif strategy_name == "BEAR_CALL_SPREAD":\n                triggered = spot_move >= price_stop_pts\n            else:\n                triggered = abs(spot_move) >= price_stop_pts\n            if triggered:\n                return "CLOSE_STOP", {"reason_detail": f"price_stop_{abs(spot_move):.0f}pts"}',
'        price_stop_pts = position.get("price_stop_pts") or 0\n        spot = signals.get("spot")\n        if price_stop_pts > 0 and spot is not None and position.get("entry_spot"):\n            spot_move = spot - position["entry_spot"]\n            if strategy_name == "BULL_PUT_SPREAD":\n                triggered = spot_move <= -price_stop_pts\n            elif strategy_name == "BEAR_CALL_SPREAD":\n                triggered = spot_move >= price_stop_pts\n            elif strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY"):\n                _pos_legs = self._get_position_legs(position["position_id"])\n                _short_puts  = [l["strike"] for l in _pos_legs if l["action"] == "SELL" and l["option_type"] == "put"  and l["leg_status"] == "OPEN"]\n                _short_calls = [l["strike"] for l in _pos_legs if l["action"] == "SELL" and l["option_type"] == "call" and l["leg_status"] == "OPEN"]\n                _near_put  = min(_short_puts,  default=0)\n                _near_call = max(_short_calls, default=999999)\n                _put_breach  = _near_put > 0 and spot <= (_near_put + price_stop_pts)\n                _call_breach = _near_call < 999999 and spot >= (_near_call - price_stop_pts)\n                triggered = _put_breach or _call_breach\n            else:\n                triggered = abs(spot_move) >= price_stop_pts\n            if triggered:\n                return "CLOSE_STOP", {"reason_detail": f"price_stop_{abs(spot_move):.0f}pts"}',
"P10-5b: IC/IB price stop triggers when spot within price_stop_pts of short strike not from entry")

write(ee_path, src)
check_syntax(ee_path)

print("\n" + "=" * 68)
print("PATCH 10 SUMMARY")
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
    print("\n[SUCCESS] Patch 10 complete.")
    sys.exit(0)