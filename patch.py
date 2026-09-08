# patch5.py
from __future__ import annotations
import ast
import sys
import shutil
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"patch5_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

def backup(path: Path) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, BACKUP_DIR / path.name)

def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")

def rep(content: str, old: str, new: str, label: str) -> str:
    if old not in content:
        print(f"  MISS [{label}]")
        return content
    result = content.replace(old, new, 1)
    print(f"  OK   [{label}]")
    return result

print("=" * 60)
print("NIFTY ALGO v3.0 PATCH5 — strategy_engine _center_ref fix")
print("=" * 60)

print("\n[strategy_engine.py]")
p = BASE / "strategy_engine.py"
backup(p)
c = read(p)

# The problem: _center_ref is defined in _select_strikes but
# _build_iron_condor, _build_bull_put_spread, _build_bear_call_spread
# are called with it as if it were their local variable.
# Fix: pass center_ref as explicit parameter to each build method.

# Step 1: Update _select_strikes dispatch calls to pass center_ref
c = rep(c,
    '        if strategy_name == IRON_BUTTERFLY:\n            return self._build_iron_butterfly(chain, spot, step, wing)\n        if strategy_name == IRON_CONDOR:\n            return self._build_iron_condor(\n                chain, spot, step, dte, short_dist, delta_target, wing, signals\n            )\n        if strategy_name == BULL_PUT_SPREAD:\n            return self._build_bull_put_spread(\n                chain, spot, step, dte, short_dist, delta_target, wing\n            )\n        if strategy_name == BEAR_CALL_SPREAD:\n            return self._build_bear_call_spread(\n                chain, spot, step, dte, short_dist, delta_target, wing\n            )',
    '        if strategy_name == IRON_BUTTERFLY:\n            return self._build_iron_butterfly(chain, spot, step, wing)\n        if strategy_name == IRON_CONDOR:\n            return self._build_iron_condor(\n                chain, spot, step, dte, short_dist, delta_target, wing, signals, _center_ref\n            )\n        if strategy_name == BULL_PUT_SPREAD:\n            return self._build_bull_put_spread(\n                chain, spot, step, dte, short_dist, delta_target, wing, _center_ref\n            )\n        if strategy_name == BEAR_CALL_SPREAD:\n            return self._build_bear_call_spread(\n                chain, spot, step, dte, short_dist, delta_target, wing, _center_ref\n            )',
    "center-ref-dispatch")

# Step 2: Update _build_iron_condor signature and body
c = rep(c,
    '    def _build_iron_condor(\n        self,\n        chain:        dict,\n        spot:         float,\n        step:         int,\n        dte:          Optional[int],\n        short_dist:   Optional[int],\n        delta_target: float,\n        wing:         int,\n        signals:      dict,\n    ) -> Tuple[Optional[List[dict]], Optional[str]]:\n        if short_dist is not None:\n            sc = int(round((_center_ref + short_dist) / step) * step)\n            sp = int(round((_center_ref - short_dist) / step) * step)',
    '    def _build_iron_condor(\n        self,\n        chain:        dict,\n        spot:         float,\n        step:         int,\n        dte:          Optional[int],\n        short_dist:   Optional[int],\n        delta_target: float,\n        wing:         int,\n        signals:      dict,\n        center_ref:   Optional[float] = None,\n    ) -> Tuple[Optional[List[dict]], Optional[str]]:\n        _cr = center_ref if center_ref is not None else spot\n        if short_dist is not None:\n            sc = int(round((_cr + short_dist) / step) * step)\n            sp = int(round((_cr - short_dist) / step) * step)',
    "center-ref-condor-sig")

# Step 3: Update _build_bull_put_spread signature and body
c = rep(c,
    '    def _build_bull_put_spread(\n        self,\n        chain:        dict,\n        spot:         float,\n        step:         int,\n        dte:          Optional[int],\n        short_dist:   Optional[int],\n        delta_target: float,\n        wing:         int,\n    ) -> Tuple[Optional[List[dict]], Optional[str]]:\n        if short_dist is not None:\n            sp = int(round((_center_ref - short_dist) / step) * step)',
    '    def _build_bull_put_spread(\n        self,\n        chain:        dict,\n        spot:         float,\n        step:         int,\n        dte:          Optional[int],\n        short_dist:   Optional[int],\n        delta_target: float,\n        wing:         int,\n        center_ref:   Optional[float] = None,\n    ) -> Tuple[Optional[List[dict]], Optional[str]]:\n        _cr = center_ref if center_ref is not None else spot\n        if short_dist is not None:\n            sp = int(round((_cr - short_dist) / step) * step)',
    "center-ref-bullput-sig")

# Step 4: Update _build_bear_call_spread signature and body
c = rep(c,
    '    def _build_bear_call_spread(\n        self,\n        chain:        dict,\n        spot:         float,\n        step:         int,\n        dte:          Optional[int],\n        short_dist:   Optional[int],\n        delta_target: float,\n        wing:         int,\n    ) -> Tuple[Optional[List[dict]], Optional[str]]:\n        if short_dist is not None:\n            sc = int(round((_center_ref + short_dist) / step) * step)',
    '    def _build_bear_call_spread(\n        self,\n        chain:        dict,\n        spot:         float,\n        step:         int,\n        dte:          Optional[int],\n        short_dist:   Optional[int],\n        delta_target: float,\n        wing:         int,\n        center_ref:   Optional[float] = None,\n    ) -> Tuple[Optional[List[dict]], Optional[str]]:\n        _cr = center_ref if center_ref is not None else spot\n        if short_dist is not None:\n            sc = int(round((_cr + short_dist) / step) * step)',
    "center-ref-bearcall-sig")

write(p, c)

# ─────────────────────────────────────────────────────────────
# SYNTAX VERIFICATION
# ─────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("SYNTAX VERIFICATION")
print("=" * 60)

files = [
    "core.py", "data_engine.py", "regime_engine.py",
    "calibration_engine.py", "strategy_engine.py",
    "execution_engine.py", "main.py",
    "backtest.py", "eod_report.py",
]

all_ok = True
for fname in files:
    fpath = BASE / fname
    if not fpath.exists():
        print(f"  MISSING: {fname}")
        all_ok = False
        continue
    try:
        source = fpath.read_text(encoding="utf-8")
        ast.parse(source)
        print(f"  OK  {fname}")
    except SyntaxError as e:
        print(f"  SYNTAX ERROR {fname}: line {e.lineno}: {e.msg}")
        all_ok = False

print()
if all_ok:
    print("ALL FILES PASS SYNTAX CHECK")
    print(f"Backups in: {BACKUP_DIR}")
    print()
    print("Run verify_all.py to confirm all self-tests pass.")
else:
    print("SYNTAX ERRORS REMAIN")
    print(f"Backups in: {BACKUP_DIR}")
    sys.exit(1)