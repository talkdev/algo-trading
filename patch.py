# patch11b.py
from __future__ import annotations
import ast
import shutil
import sys
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"_patch11b_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
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
            print(f"  L{i+1}:")
            for j in range(s, e):
                m = ">>>" if j == i else "   "
                print(f"  {m} L{j+1}: {repr(lines[j])}")

print("=" * 68)
print("PATCH 11b - REMAINING TWO SKIPPED FIXES")
print(f"Base: {BASE}")
print("=" * 68)

re_path = BASE / "regime_engine.py"

print("\n[DIAGNOSE] Exact patterns for skipped items...")
print("\n--- P11-1a: calculate_regime call site (L1600) ---")
show_lines(re_path, "signals.get(\"atm_straddle_price\", 0) or 0,", 5)
show_lines(re_path, "ts.weekday() if hasattr(ts, \"weekday\") else 0", 5)

print("\n--- P11-1c: primary branch dte==0 at L1092 ---")
show_lines(re_path, "if dte == 0 and avg > 0:", 5)

print("\n" + "=" * 68)
print("APPLYING PATCH 11b")
print("=" * 68)

src = read(re_path)

src = rb(src,
'        s_ratio = self.classifier._calculate_straddle_ratio(\n            signals.get("atm_straddle_price", 0) or 0,\n            ts.weekday() if hasattr(ts, "weekday") else 0\n        )',
'        _dte_for_ratio = signals.get("actual_dte")\n        s_ratio = self.classifier._calculate_straddle_ratio(\n            signals.get("atm_straddle_price", 0) or 0,\n            ts.weekday() if hasattr(ts, "weekday") else 0,\n            dte=_dte_for_ratio,\n        )',
"P11b-1a: pass actual_dte to _calculate_straddle_ratio in calculate_regime")

src = rb(src,
'                avg = avg_r * 0.70 if avg_r > 0 else 0\n                if dte == 0 and avg > 0:',
'                avg = avg_r * 0.70 if avg_r > 0 else 0\n                if dte is not None and dte <= 1 and avg > 0:',
"P11b-1c: primary day_range_points branch scales for dte<=1 not just dte==0")

write(re_path, src)
ok = check_syntax(re_path)
if ok:
    print("  regime_engine.py syntax OK")

print("\n" + "=" * 68)
print("PATCH 11b SUMMARY")
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
    print("\n[SUCCESS] Patch 11b complete.")
    sys.exit(0)