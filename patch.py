# patch9c.py
from __future__ import annotations
import ast
import shutil
import sys
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"_patch9c_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
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
print("PATCH 9c - FIX iv_hv_ratio None PROPAGATION")
print(f"Base: {BASE}")
print("=" * 68)

re_path = BASE / "regime_engine.py"

print("\n[DIAGNOSE] Finding iv_hv usage in calculate_regime and score line...")
show_lines(re_path, "iv_hv   = self.classifier._calculate_iv_hv_ratio", 4)
show_lines(re_path, "iv_hv_ratio=iv_hv", 4)
show_lines(re_path, "iv_hv_ratio=0.0", 4)
show_lines(re_path, "if iv_hv is None", 4)
show_lines(re_path, "scores.append.*iv_hv", 4)

print("\n[APPLYING FIXES]")
src = read(re_path)

src = rb(src,
'        iv_hv   = self.classifier._calculate_iv_hv_ratio(cur_iv_pct)',
'        iv_hv   = self.classifier._calculate_iv_hv_ratio(cur_iv_pct)\n        if iv_hv is None:\n            iv_hv = 0.0',
"P9c-1: guard iv_hv None in calculate_regime before passing to RegimeSnapshot")

src = rb(src,
'        iv_hv_s = self._t("iv_hv_sell_threshold", "iv_hv_sell")\n        if iv_hv is None or iv_hv <= 0:\n            scores.append(0)\n        else:\n            scores.append(-1 if iv_hv > iv_hv_s else -0.5 if iv_hv > self.config.iv_hv_neutral else 0 if iv_hv > self.config.iv_hv_buy else 1)',
'        iv_hv_s = self._t("iv_hv_sell_threshold", "iv_hv_sell")\n        _iv_hv_safe = iv_hv if (iv_hv is not None and iv_hv > 0) else None\n        if _iv_hv_safe is None:\n            scores.append(0)\n        else:\n            scores.append(-1 if _iv_hv_safe > iv_hv_s else -0.5 if _iv_hv_safe > self.config.iv_hv_neutral else 0 if _iv_hv_safe > self.config.iv_hv_buy else 1)',
"P9c-2: use _iv_hv_safe in score to handle None cleanly")

write(re_path, src)
ok = check_syntax(re_path)
if ok:
    print("  regime_engine.py syntax OK")

print("\n" + "=" * 68)
print("PATCH 9c SUMMARY")
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
    print("\n[SUCCESS] Patch 9c complete.")
    sys.exit(0)