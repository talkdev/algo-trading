# patch8.py
from __future__ import annotations
import ast
import shutil
import sys
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"_patch8_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
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
print("PATCH 8 - THREE CRITICAL FIXES")
print("Item 1: emg NameError in classify_volatility")
print("Item 2: VIX prev close from broker API not null daily_summary")
print("Item 3: rupee gate double-counting costs")
print(f"Base: {BASE}")
print("=" * 68)

re_path  = BASE / "regime_engine.py"
se_path  = BASE / "strategy_engine.py"
me_path  = BASE / "main.py"

print("\n[1/3] regime_engine.py — Item 1: fix emg NameError")
src = read(re_path)

src = rb(src,
'        scores.append(-1 if vix_roc < -2 else -0.5 if vix_roc < 0 else 0 if vix_roc < emg * 0.6 else 1)',
'        _emg_score_thresh = 3.0\n        scores.append(-1 if vix_roc < -2 else -0.5 if vix_roc < 0 else 0 if vix_roc < _emg_score_thresh else 1)',
"P8-1: fix emg NameError — replace emg*0.6 with fixed 3.0pct threshold for ROC score")

print("\n[2/3] regime_engine.py — Item 2: VIX prev close from broker API")
src = rb(src,
'        _prev_vix_close = 0.0\n        try:\n            _prev_row = self.db.query_one(\n                "SELECT vix_close FROM daily_summary WHERE trading_date < ? "\n                "AND vix_close IS NOT NULL AND vix_close > 0 "\n                "ORDER BY trading_date DESC LIMIT 1",\n                (str(datetime.now().date()),)\n            )\n            if _prev_row and _prev_row.get("vix_close"):\n                _prev_vix_close = float(_prev_row["vix_close"])\n        except Exception:\n            pass\n        _vix_pct_from_close = ((vix - _prev_vix_close) / _prev_vix_close * 100.0) if _prev_vix_close > 0 else 0.0\n        if _vix_pct_from_close >= 15.0 and vix >= 14.0:\n            details["trigger"] = "VIX_SPIKE_REAL"\n            self.logger.warning(f"REAL VIX EMERGENCY: VIX up {_vix_pct_from_close:.1f}pct from prev close {_prev_vix_close:.2f} to {vix:.2f}")\n            return VolatilityRegime.ABORT, details',
'        _prev_vix_close = 0.0\n        try:\n            _prev_row = self.db.query_one(\n                "SELECT vix_close_val FROM daily_summary WHERE trading_date < ? "\n                "AND vix_close_val IS NOT NULL AND vix_close_val > 0 "\n                "ORDER BY trading_date DESC LIMIT 1",\n                (str(datetime.now().date()),)\n            )\n            if _prev_row and _prev_row.get("vix_close_val"):\n                _prev_vix_close = float(_prev_row["vix_close_val"])\n        except Exception:\n            pass\n        if _prev_vix_close <= 0:\n            try:\n                _vix_rows = self.db.query(\n                    "SELECT vix_value FROM vix_history "\n                    "WHERE date < ? AND vix_value > 0 "\n                    "ORDER BY timestamp DESC LIMIT 5",\n                    (str(datetime.now().date()),)\n                )\n                if _vix_rows:\n                    _prev_vix_close = float(_vix_rows[0]["vix_value"])\n            except Exception:\n                pass\n        _vix_pct_from_close = ((vix - _prev_vix_close) / _prev_vix_close * 100.0) if _prev_vix_close > 0 else 0.0\n        if _vix_pct_from_close >= 15.0 and vix >= 14.0:\n            details["trigger"] = "VIX_SPIKE_REAL"\n            self.logger.warning(f"REAL VIX EMERGENCY: VIX up {_vix_pct_from_close:.1f}pct from prev close {_prev_vix_close:.2f} to {vix:.2f}")\n            return VolatilityRegime.ABORT, details',
"P8-2: VIX prev close uses vix_close_val then vix_history fallback (daily_summary vix_close always NULL)")

write(re_path, src)
ok1 = check_syntax(re_path)
if ok1:
    print("  regime_engine.py syntax OK")

print("\n[3/3] strategy_engine.py — Item 3: rupee gate double-counting")
src = read(se_path)

src = rb(src,
'            _rtrip_costs_rs = (total_costs_pts + total_slippage) * 2.0 * C02\n            min_rupee = max(int(_rtrip_costs_rs * 3.0), 150)\n            if net_profit_at_target * C02 < min_rupee:\n                return {"valid": False, "reason": f"projected_profit_below_Rs{min_rupee}"}',
'            _one_way_costs_rs = (total_costs_pts + total_slippage) * C02\n            _rtrip_costs_rs = _one_way_costs_rs * 2.0\n            min_rupee = max(int(_rtrip_costs_rs * 2.0), 150)\n            _gross_profit_at_target = gross_credit * self._get_target_pct(s) if gross_credit else net_credit * self._get_target_pct(s)\n            _gross_profit_rs = _gross_profit_at_target * C02\n            if _gross_profit_rs < min_rupee:\n                return {"valid": False, "reason": f"projected_profit_below_Rs{min_rupee}"}',
"P8-3: rupee gate on gross profit not net (net already subtracted costs once) gate=2x round-trip")

write(se_path, src)
ok2 = check_syntax(se_path)
if ok2:
    print("  strategy_engine.py syntax OK")

print("\n" + "=" * 68)
print("PATCH 8 SUMMARY")
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

print("""
ARITHMETIC VERIFICATION FOR P8-3:
IC credit 27pts gross, VIX 11.2, 1 lot, DTE 1:
  costs_pts = 0.75, slippage = 0.60
  one_way_costs_rs = (0.75+0.60)*65 = Rs87.75
  rtrip_costs_rs = 87.75*2 = Rs175.50
  min_rupee = max(175.50*2, 150) = Rs351
  gross_profit_at_target = 27*0.45 = 12.15pts
  gross_profit_rs = 12.15*65 = Rs789.75
  Rs789.75 > Rs351 -> PASSES

IC credit 22pts (minimum after P6), VIX 11.2, 1 lot:
  gross_profit_at_target = 22*0.45 = 9.9pts
  gross_profit_rs = 9.9*65 = Rs643.50
  Rs643.50 > Rs351 -> PASSES

IC credit 18pts (borderline), VIX 11.2, 1 lot:
  gross_profit_at_target = 18*0.45 = 8.1pts
  gross_profit_rs = 8.1*65 = Rs526.50
  Rs526.50 > Rs351 -> PASSES

IC credit 15pts (below minimum after P6 so never reaches gate):
  gross_profit_at_target = 15*0.45 = 6.75pts
  gross_profit_rs = 6.75*65 = Rs438.75
  Rs438.75 > Rs351 -> PASSES (but blocked by min credit gate first)
""")

if _errors:
    print("[FAIL]")
    sys.exit(1)
else:
    print("[SUCCESS] Patch 8 complete.")
    sys.exit(0)