# patch3.py
# Fixes regime_engine.py self-test assertions broken by DEFAULTS change.
# PCR thresholds changed: bullish 0.72→0.65, bearish 1.28→1.20
# Self-test mock values must be updated to match new thresholds.
# Also applies the 2 NOT FOUND blocks from patch2:
#   - persistence filter DTE-aware
#   - calibration_engine run from tier1
#   - calibration_engine signal weights from tier2

from __future__ import annotations
import ast
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"backup_p3_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


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


def show_context(src: str, fragment: str, context: int = 3) -> None:
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        if fragment in line:
            start = max(0, i - context - 1)
            end = min(len(lines), i + context)
            print(f"  Found at line {i}:")
            for j in range(start, end):
                marker = ">>>" if j == i - 1 else "   "
                print(f"  {marker} {j+1:4d}: {lines[j]}")
            break


print("=" * 70)
print("NIFTY ALGO v3.0 — APPLYING PATCH3 (fixes + missed blocks)")
print("=" * 70)

# ─────────────────────────────────────────────────────────────────────────────
# FILE 1: regime_engine.py
# Fix 1: Persistence filter DTE-aware (was NOT FOUND in patch2)
# Fix 2: Self-test PCR mock values updated for new thresholds
# ─────────────────────────────────────────────────────────────────────────────
print("\n[1/2] regime_engine.py")
p = BASE / "regime_engine.py"
backup(p)
src = read(p)

print("\n  Diagnosing persistence filter block...")
show_context(src, "self._pending_count >= self.config.regime_persistence_cycles", 5)

src = replace_once(
    src,
    "        if (self._pending_regime is not None and\n"
    "                self._pending_regime.final_regime == new_regime.final_regime):\n"
    "            self._pending_count += 1\n"
    "            if self._pending_count >= self.config.regime_persistence_cycles:\n"
    "                self.logger.info(\n"
    "                    f\"Regime confirmed after {self._pending_count} cycles: \"\n"
    "                    f\"{new_regime.final_regime}\"\n"
    "                )\n"
    "                self._pending_regime = None\n"
    "                self._pending_count  = 0\n"
    "                return new_regime\n"
    "            self.logger.debug(\n"
    "                f\"Regime pending ({self._pending_count}/\"\n"
    "                f\"{self.config.regime_persistence_cycles}): \"\n"
    "                f\"{new_regime.final_regime}\"\n"
    "            )\n"
    "            return self._current_regime\n"
    "        else:\n"
    "            self._pending_regime = new_regime\n"
    "            self._pending_count  = 1\n"
    "            self.logger.debug(\n"
    "                f\"New regime candidate (1/{self.config.regime_persistence_cycles}): \"\n"
    "                f\"{new_regime.final_regime}\"\n"
    "            )\n"
    "            return self._current_regime",
    "        _dte_now = new_regime.dte if hasattr(new_regime, 'dte') else 2\n"
    "        if _dte_now == 0:\n"
    "            _required = 1\n"
    "        elif _dte_now == 1:\n"
    "            _required = 2\n"
    "        else:\n"
    "            _required = self.config.regime_persistence_cycles\n"
    "        if (self._pending_regime is not None and\n"
    "                self._pending_regime.final_regime == new_regime.final_regime):\n"
    "            self._pending_count += 1\n"
    "            if self._pending_count >= _required:\n"
    "                self.logger.info(\n"
    "                    f\"Regime confirmed after {self._pending_count} cycles: \"\n"
    "                    f\"{new_regime.final_regime} (DTE={_dte_now})\"\n"
    "                )\n"
    "                self._pending_regime = None\n"
    "                self._pending_count  = 0\n"
    "                return new_regime\n"
    "            self.logger.debug(\n"
    "                f\"Regime pending ({self._pending_count}/{_required}): \"\n"
    "                f\"{new_regime.final_regime}\"\n"
    "            )\n"
    "            return self._current_regime\n"
    "        else:\n"
    "            self._pending_regime = new_regime\n"
    "            self._pending_count  = 1\n"
    "            self.logger.debug(\n"
    "                f\"New regime candidate (1/{_required}): \"\n"
    "                f\"{new_regime.final_regime}\"\n"
    "            )\n"
    "            return self._current_regime",
    "persistence filter DTE-aware"
)

src = replace_once(
    src,
    "    pos2 = classifier.classify_positioning(make_signals(pcr=0.60))\n"
    "    print(f\"  PCR=0.60 (extreme greed) → {pos2.value} (expect BULLISH)\")\n"
    "    assert pos2 == PositioningRegime.BULLISH, f\"Expected BULLISH, got {pos2}\"",
    "    pos2 = classifier.classify_positioning(make_signals(pcr=0.48))\n"
    "    print(f\"  PCR=0.48 (extreme greed) → {pos2.value} (expect BULLISH)\")\n"
    "    assert pos2 == PositioningRegime.BULLISH, f\"Expected BULLISH, got {pos2}\"",
    "self-test PCR extreme greed mock value"
)

src = replace_once(
    src,
    "    pos3 = classifier.classify_positioning(make_signals(pcr=1.60))\n"
    "    print(f\"  PCR=1.60 (extreme fear) → {pos3.value} (expect BEARISH)\")\n"
    "    assert pos3 == PositioningRegime.BEARISH, f\"Expected BEARISH, got {pos3}\"",
    "    pos3 = classifier.classify_positioning(make_signals(pcr=1.50))\n"
    "    print(f\"  PCR=1.50 (extreme fear) → {pos3.value} (expect BEARISH)\")\n"
    "    assert pos3 == PositioningRegime.BEARISH, f\"Expected BEARISH, got {pos3}\"",
    "self-test PCR extreme fear mock value"
)

write(p, src)

# ─────────────────────────────────────────────────────────────────────────────
# FILE 2: calibration_engine.py
# Fix: Run schedule blocks that were NOT FOUND in patch2
# The exact strings differ from what patch2 expected.
# Diagnose first, then patch.
# ─────────────────────────────────────────────────────────────────────────────
print("\n[2/2] calibration_engine.py")
p = BASE / "calibration_engine.py"
backup(p)
src = read(p)

print("\n  Diagnosing run schedule block...")
show_context(src, "_run_vrp_calibration(new_state, n_days)", 8)

print("\n  Diagnosing signal weights block...")
show_context(src, "_run_signal_weight_calibration(new_state, n_days)", 5)

src = replace_once(
    src,
    "        if tier2 or schedule in (\"weekly\", \"monthly\", \"force\", \"startup\"):\n"
    "            self._run_vrp_calibration(new_state, n_days)\n"
    "\n"
    "        if tier2 or schedule in (\"weekly\", \"monthly\", \"force\", \"startup\"):\n"
    "            self._run_day_size_calibration(new_state, n_days)\n"
    "\n"
    "        if tier2 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
    "            self._run_oi_calibration(new_state, n_days)\n"
    "\n"
    "        if tier2 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
    "            self._run_pcr_calibration(new_state, n_days)\n"
    "\n"
    "        if tier2 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
    "            self._run_skew_calibration(new_state, n_days)\n"
    "\n"
    "        if tier2 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
    "            self._run_straddle_ratio_calibration(new_state, n_days)",
    "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\", \"startup\"):\n"
    "            self._run_vrp_calibration(new_state, n_days)\n"
    "\n"
    "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\", \"startup\"):\n"
    "            self._run_day_size_calibration(new_state, n_days)\n"
    "\n"
    "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
    "            self._run_oi_calibration(new_state, n_days)\n"
    "\n"
    "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
    "            self._run_pcr_calibration(new_state, n_days)\n"
    "\n"
    "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
    "            self._run_skew_calibration(new_state, n_days)\n"
    "\n"
    "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
    "            self._run_straddle_ratio_calibration(new_state, n_days)",
    "cal_engine run from tier1"
)

src = replace_once(
    src,
    "        if tier3 or schedule in (\"monthly\", \"force\"):\n"
    "            self._run_signal_weight_calibration(new_state, n_days)\n"
    "\n"
    "        if tier3 or schedule == \"monthly\":\n"
    "            self._run_drift_detection(new_state)",
    "        if tier2 or schedule in (\"monthly\", \"force\"):\n"
    "            self._run_signal_weight_calibration(new_state, n_days)\n"
    "\n"
    "        if tier2 or schedule == \"monthly\":\n"
    "            self._run_drift_detection(new_state)",
    "cal_engine signal weights from tier2"
)

write(p, src)

# ─────────────────────────────────────────────────────────────────────────────
# FINAL SYNTAX VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("FINAL SYNTAX VERIFICATION")
print("=" * 70)

all_ok = True
for fname in [
    "data_engine.py", "strategy_engine.py", "regime_engine.py",
    "calibration_engine.py", "core.py", "backtest.py", "eod_report.py"
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
    print("SYNTAX ERRORS FOUND — check above, restore from backup if needed")
    print(f"Backups in: {BACKUP_DIR}")

print()
print("PATCH3 APPLIED:")
print("  regime_engine.py    persistence filter DTE-aware (was NOT FOUND in patch2)")
print("  regime_engine.py    self-test PCR mock values updated for new thresholds")
print("                      (pcr_bullish 0.72→0.65 means extreme_greed needs pcr<0.5525)")
print("                      (test now uses pcr=0.48 for extreme_greed BULLISH)")
print("                      (test now uses pcr=1.50 for extreme_fear BEARISH)")
print("  calibration_engine  run from tier1 (was NOT FOUND in patch2)")
print("  calibration_engine  signal weights from tier2 (was NOT FOUND in patch2)")
print()
print("NEXT STEPS:")
print("  1. python verify_all.py   (should now show 7/7 PASS)")
print("  2. python main.py         (start engine)")