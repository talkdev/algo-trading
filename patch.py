# patch4.py — only 2 real fixes needed now
from __future__ import annotations
import ast
import shutil
import sys
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"_patch4_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
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

def show_lines(fp, pattern, ctx=4):
    lines = Path(fp).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if pattern in line:
            s = max(0, i - ctx)
            e = min(len(lines), i + ctx + 1)
            print(f"  Found at line {i+1}:")
            for j in range(s, e):
                m = ">>>" if j == i else "   "
                print(f"  {m} L{j+1}: {repr(lines[j])}")

print("=" * 68)
print("NIFTY OPTIONS ALGO ENGINE PATCH 4")
print("Only real issues confirmed in patched code")
print(f"Base dir: {BASE}")
print("=" * 68)

re_path = BASE / "regime_engine.py"

print("\n[DIAGNOSE] Finding exact patterns...")
print("\n--- pd.read_sql_query in CalibrationEngine.run ---")
show_lines(re_path, "pd.read_sql_query", 5)
print("\n--- snap_df = pd.read_sql_query ---")
show_lines(re_path, "snap_df = pd.read_sql_query", 5)
print("\n--- _pd2 usage ---")
show_lines(re_path, "_pd2", 3)
print("\n--- vix bootstrap from daily_summary ---")
show_lines(re_path, "bootstrap from daily_summary", 3)
show_lines(re_path, "_vix_bootstrapped", 3)
print("\n--- VIX p25 floor in CalibrationEngine.run ---")
show_lines(re_path, "p25 = max", 3)
show_lines(re_path, "p25 = float", 3)

print("\n" + "=" * 68)
print("APPLYING PATCH 4")
print("=" * 68)

print("\n[RE-FIX-1] Fix pd not defined in CalibrationEngine.run")
src = read(re_path)

src = rb(src,
'            snap_df = pd.read_sql_query(\n                "SELECT skew, oi_change_pct, resistance_oi, support_oi, "\n                "total_ce_oi, total_pe_oi FROM market_snapshots "\n                "WHERE skew != 0 AND date >= ? ORDER BY timestamp",\n                self.db.get_connection(),\n                params=((date.today() - timedelta(days=365)).isoformat(),),\n            )',
'            import pandas as _pd_cal\n            snap_df = _pd_cal.read_sql_query(\n                "SELECT skew, oi_change_pct, resistance_oi, support_oi, "\n                "total_ce_oi, total_pe_oi FROM market_snapshots "\n                "WHERE skew != 0 AND date >= ? ORDER BY timestamp",\n                self.db.get_connection(),\n                params=((date.today() - timedelta(days=365)).isoformat(),),\n            )',
"RE-FIX-1: fix pd not defined in CalibrationEngine.run snap_df query")

print("\n[RE-FIX-2] Fix VIX bootstrap in CalibrationEngine.run to use daily_summary vix columns")
src = rb(src,
'        if len(vix_df) >= 50:\n            v   = vix_df["vix_value"].dropna().values\n            p25 = float(np.percentile(v, 25))\n            p50 = float(np.percentile(v, 50))\n            p75 = float(np.percentile(v, 75))\n            p90 = float(np.percentile(v, 90))\n            _n_vix = len(v)\n            if _n_vix < 500:\n                p90 = max(p90, 24.0)\n            if _n_vix < 200:\n                p75 = max(p75, 18.0)\n            if _n_vix < 100:\n                p50 = max(p50, 14.0)\n            self.logger.info(\n                f"  VIX: p25={p25:.1f} p50={p50:.1f} p75={p75:.1f} p90={p90:.1f} (n={_n_vix})"\n            )',
'        if len(vix_df) >= 50:\n            v   = vix_df["vix_value"].dropna().values\n            v   = v[(v > 8.0) & (v < 90.0)]\n            p25 = float(np.percentile(v, 25))\n            p50 = float(np.percentile(v, 50))\n            p75 = float(np.percentile(v, 75))\n            p90 = float(np.percentile(v, 90))\n            _n_vix = len(v)\n            if _n_vix < 500:\n                p90 = max(p90, 24.0)\n            if _n_vix < 200:\n                p75 = max(p75, 18.0)\n            if _n_vix < 100:\n                p50 = max(p50, 14.0)\n            p25 = max(p25, 10.5)\n            p50 = max(p50, 13.0)\n            p75 = max(p75, 17.0)\n            self.logger.info(\n                f"  VIX: p25={p25:.1f} p50={p50:.1f} p75={p75:.1f} p90={p90:.1f} (n={_n_vix})"\n            )',
"RE-FIX-2: add floor to calibration VIX percentiles (p25>=10.5, p50>=13.0, p75>=17.0)")

src = rb(src,
'        else:\n            self.logger.info(f"  VIX rows={len(vix_df)} < 50. Attempting bootstrap from daily_summary.")\n            _vix_bootstrapped = False\n            try:\n                _daily = self.db.get_daily_summary(days=730)\n                _vcols = [c for c in ["vix_open", "vix_close", "vix_high", "vix_low"] if c in _daily.columns]\n                if not _daily.empty and _vcols:\n                    _vvals = _pd.concat([_daily[c].dropna() for c in _vcols]).values\n                    _vvals = _vvals[(_vvals > 8.0) & (_vvals < 90.0)]\n                    if len(_vvals) >= 20:\n                        p25 = float(max(np.percentile(_vvals, 25), 11.0))\n                        p50 = float(max(np.percentile(_vvals, 50), 14.0))\n                        p75 = float(max(np.percentile(_vvals, 75), 18.0))\n                        p90 = float(max(np.percentile(_vvals, 90), 24.0))\n                        self.logger.info(f"  VIX bootstrap from daily_summary: p25={p25:.1f} p50={p50:.1f} p75={p75:.1f} p90={p90:.1f} (n={len(_vvals)})")\n                        _vix_bootstrapped = True\n            except Exception as _ve:\n                self.logger.debug(f"  VIX bootstrap error: {_ve}")',
'        else:\n            self.logger.info(f"  VIX rows={len(vix_df)} < 50. Attempting bootstrap from daily_summary.")\n            _vix_bootstrapped = False\n            try:\n                import pandas as _pd_boot\n                _daily = self.db.get_daily_summary(days=730)\n                _vcols = [c for c in ["vix_open", "vix_close", "vix_high", "vix_low", "vix_close_val"] if c in _daily.columns]\n                if not _daily.empty and _vcols:\n                    _vvals = _pd_boot.concat([_daily[c].dropna() for c in _vcols]).values\n                    _vvals = _vvals[(_vvals > 8.0) & (_vvals < 90.0)]\n                    if len(_vvals) >= 10:\n                        p25 = float(max(np.percentile(_vvals, 25), 10.5))\n                        p50 = float(max(np.percentile(_vvals, 50), 13.0))\n                        p75 = float(max(np.percentile(_vvals, 75), 17.0))\n                        p90 = float(max(np.percentile(_vvals, 90), 24.0))\n                        self.logger.info(f"  VIX bootstrap from daily_summary: p25={p25:.1f} p50={p50:.1f} p75={p75:.1f} p90={p90:.1f} (n={len(_vvals)})")\n                        _vix_bootstrapped = True\n            except Exception as _ve:\n                self.logger.debug(f"  VIX bootstrap error: {_ve}")',
"RE-FIX-3: fix pd not defined in VIX bootstrap, add vix_close_val column, lower min to 10 rows")

write(re_path, src)
ok = check_syntax(re_path)

print("\n" + "=" * 68)
print("PATCH 4 SUMMARY")
print("=" * 68)
print(f"Applied: {len(_applied)}")
for a in _applied:
    print(f"  [OK] {a}")
print(f"Skipped: {len(_skipped)}")
for s in _skipped:
    print(f"  [SKIP] {s}")
print(f"Errors:  {len(_errors)}")
for e in _errors:
    print(f"  [ERR] {e}")
print(f"\nBackups: {BACKUP_DIR}")
print("\nNOTE: Issues 1,4,9,10,11,12,13 need fresh trading day run to verify.")
print("      Issues 2,5,6,7 are self-fixing on next run.")
print("      Issues 3,8 fixed by this patch.")

if _errors:
    sys.exit(1)
else:
    print("\n[SUCCESS] Patch 4 complete.")
    sys.exit(0)