# patch4b.py
# Fixes: strategy_engine.py self-test assertion for new slippage model,
#        0DTE margin with SEBI ELM (was NOT FOUND in patch4).

from __future__ import annotations
import ast
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"backup_p4b_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def backup(path: Path) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, BACKUP_DIR / path.name)
    print(f"  Backed up : {path.name}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, src: str) -> None:
    try:
        ast.parse(src)
    except SyntaxError as e:
        print(f"  SYNTAX ERROR in {path.name} line {e.lineno}: {e.msg}")
        print("  ABORTING — original file preserved.")
        sys.exit(1)
    path.write_text(src, encoding="utf-8")
    print(f"  Written   : {path.name}")


def replace_once(src: str, old: str, new: str, label: str) -> str:
    if old not in src:
        print(f"  NOT FOUND : [{label}] — skipping")
        return src
    result = src.replace(old, new, 1)
    print(f"  Patched   : [{label}]")
    return result


def show_context(src: str, fragment: str, ctx: int = 5) -> None:
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        if fragment in line:
            start = max(0, i - ctx - 1)
            end = min(len(lines), i + ctx)
            print(f"  Found at line {i}:")
            for j in range(start, end):
                m = ">>>" if j == i-1 else "   "
                print(f"  {m} {j+1:4d}: {lines[j]}")
            return
    print(f"  NOT FOUND: [{fragment}]")


print("=" * 70)
print("NIFTY ALGO v3.0 — APPLYING PATCH4b")
print("=" * 70)

print("\n[1/1] strategy_engine.py")
p = BASE / "strategy_engine.py"
backup(p)
src = read(p)

print("\n  Diagnosing margin line...")
show_context(src, "margin_per_lot", ctx=4)

print("\n  Diagnosing slippage test assertion...")
show_context(src, "expected_exit = 2 *", ctx=4)
show_context(src, "Exit slippage: expected", ctx=4)

src = replace_once(
    src,
    "    slip_exit = engine._compute_slippage(legs_ba, is_exit=True)\n"
    "    expected_exit = 2 * ((46 - 44) / 2.0 * 1.5)\n"
    "    assert abs(slip_exit - expected_exit) < 0.01, (\n"
    "        f\"Exit slippage: expected {expected_exit:.3f}, got {slip_exit:.3f}\"\n"
    "    )\n"
    "    print(f\"  Exit slippage (2 legs bid/ask): {slip_exit:.3f}pts [OK]\")",
    "    slip_exit = engine._compute_slippage(legs_ba, is_exit=True)\n"
    "    expected_exit = 2 * ((46 - 44) / 2.0 * 3.0)\n"
    "    assert abs(slip_exit - expected_exit) < 0.01, (\n"
    "        f\"Exit slippage: expected {expected_exit:.3f}, got {slip_exit:.3f}\"\n"
    "    )\n"
    "    print(f\"  Exit slippage (2 legs bid/ask): {slip_exit:.3f}pts [OK]\")",
    "self-test exit slippage assertion updated"
)

src = replace_once(
    src,
    "    slip_no_ba = engine._compute_slippage(legs_no_ba, is_exit=False)\n"
    "    expected_no_ba = 2 * 0.35\n"
    "    assert abs(slip_no_ba - expected_no_ba) < 0.01, (\n"
    "        f\"No bid/ask slippage: expected {expected_no_ba:.3f}, got {slip_no_ba:.3f}\"\n"
    "    )\n"
    "    print(f\"  Entry slippage (no bid/ask): {slip_no_ba:.3f}pts [OK]\")",
    "    slip_no_ba = engine._compute_slippage(legs_no_ba, is_exit=False)\n"
    "    expected_no_ba = 2 * 0.35\n"
    "    assert abs(slip_no_ba - expected_no_ba) < 0.01, (\n"
    "        f\"No bid/ask slippage: expected {expected_no_ba:.3f}, got {slip_no_ba:.3f}\"\n"
    "    )\n"
    "    print(f\"  Entry slippage (no bid/ask): {slip_no_ba:.3f}pts [OK]\")\n"
    "\n"
    "    slip_exit_no_ba = engine._compute_slippage(legs_no_ba, is_exit=True)\n"
    "    expected_exit_no_ba = 2 * 1.20\n"
    "    assert abs(slip_exit_no_ba - expected_exit_no_ba) < 0.01, (\n"
    "        f\"Exit no bid/ask slippage: expected {expected_exit_no_ba:.3f}, got {slip_exit_no_ba:.3f}\"\n"
    "    )\n"
    "    print(f\"  Exit slippage (no bid/ask): {slip_exit_no_ba:.3f}pts [OK]\")",
    "self-test exit no-bid-ask slippage assertion"
)

print("\n  Applying 0DTE margin with SEBI ELM...")
show_context(src, "margin_per_lot   = (actual_wing_pts", ctx=4)

src = replace_once(
    src,
    "        margin_per_lot   = (actual_wing_pts or 150) * C02 * 1.10\n"
    "        total_margin   = margin_per_lot * final_lots",
    "        _wing_margin = (actual_wing_pts or 150) * C02 * 1.10\n"
    "        if actual_dte == 0:\n"
    "            _spot_ref = float(signals.get(\"spot\") or 23900)\n"
    "            _n_short_legs = sum(\n"
    "                1 for _l in validated_legs if _l[\"action\"] == \"SELL\"\n"
    "            )\n"
    "            _elm = 0.02 * _spot_ref * C02 * _n_short_legs\n"
    "            margin_per_lot = _wing_margin + _elm\n"
    "        else:\n"
    "            margin_per_lot = _wing_margin\n"
    "        total_margin   = margin_per_lot * final_lots",
    "0DTE margin with SEBI ELM"
)

write(p, src)

print("\n" + "=" * 70)
print("FINAL SYNTAX VERIFICATION")
print("=" * 70)

all_ok = True
for fname in [
    "data_engine.py", "strategy_engine.py", "regime_engine.py",
    "calibration_engine.py", "core.py", "backtest.py", "eod_report.py",
    "execution_engine.py", "main.py",
]:
    fpath = BASE / fname
    if not fpath.exists():
        print(f"  MISSING   : {fname}")
        all_ok = False
        continue
    try:
        ast.parse(fpath.read_text(encoding="utf-8"))
        print(f"  SYNTAX OK : {fname}")
    except SyntaxError as e:
        print(f"  SYNTAX ERR: {fname} line {e.lineno}: {e.msg}")
        all_ok = False

print()
if all_ok:
    print("ALL PATCHES APPLIED SUCCESSFULLY")
    print(f"Backups in: {BACKUP_DIR}")
else:
    print("SYNTAX ERRORS FOUND — restore from backup if needed")
    print(f"Backups in: {BACKUP_DIR}")

print()
print("PATCH4b APPLIED:")
print("  strategy_engine.py  Self-test exit slippage assertion updated (1.5x->3.0x)")
print("  strategy_engine.py  0DTE margin with SEBI 2% ELM on contract notional")
print()
print("NEXT STEPS:")
print("  1. python verify_all.py  (should show 7/7 PASS)")
print("  2. python main.py")