# fix_target_pct.py
import ast, shutil
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP = BASE / f"backup_tgt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
BACKUP.mkdir(parents=True, exist_ok=True)

fpath = BASE / "strategy_engine.py"
shutil.copy2(fpath, BACKUP / "strategy_engine.py")

src = fpath.read_text(encoding="utf-8")

old = '''    def _get_target_pct(self, dte: Optional[int], signals: dict) -> float:
        """
        NIFTY 2026 0DTE: target 30-35% quickly, not 50% slowly.
        Gamma grows super-linearly after 13:30.
        Breakeven WR: 30% target needs ~58% WR vs 50% needing ~72% WR.
        DTE 1 (Monday): 38-42% - less gamma urgency.
        """
        vix = float(signals.get("vix") or 11.0)
        if dte == 0:
            return 0.35 if vix < 12.0 else (0.32 if vix < 14.0 else 0.30)
        if dte == 1:
            return 0.42 if vix < 12.0 else (0.38 if vix < 14.0 else 0.35)
        return 0.30'''

new = '''    def _get_target_pct(self, dte: Optional[int], signals: dict) -> float:
        """
        NIFTY 2026 target percentages by DTE.
        DTE 0 (Tuesday 0DTE): 45-50% — fastest gamma, exit before 13:30 gamma explosion.
        DTE 1 (Monday 1DTE): 38-42% — good theta, moderate urgency.
        DTE 2+ (Wed-Fri):    30-35% — least theta per hour, patient exit.
        Higher DTE = lower target because less gamma urgency per unit of time.
        DTE0 > DTE1 > DTE2 is the correct ordering for NIFTY intraday.
        """
        vix = float(signals.get("vix") or 11.0)
        if dte == 0:
            return 0.50 if vix < 12.0 else (0.47 if vix < 14.0 else 0.45)
        if dte == 1:
            return 0.42 if vix < 12.0 else (0.38 if vix < 14.0 else 0.35)
        return 0.32'''

if old in src:
    shutil.copy2(fpath, BACKUP / "strategy_engine.py")
    new_src = src.replace(old, new, 1)
    try:
        ast.parse(new_src)
        fpath.write_text(new_src, encoding="utf-8")
        print("OK   Fixed _get_target_pct: DTE0=45-50%, DTE1=38-42%, DTE2+=30-35%")
        print("     DTE0 > DTE1 > DTE2 ordering now correct for NIFTY 2026")
    except SyntaxError as e:
        print(f"SYNTAX ERROR: {e}")
else:
    print("Pattern not found - showing current _get_target_pct:")
    for i, line in enumerate(src.splitlines()):
        if "_get_target_pct" in line or "return 0.35" in line or "return 0.42" in line:
            print(f"  L{i+1}: {repr(line)}")

old_test = '''    assert tgt0_low >= tgt0_high, "Lower VIX should give higher target"
    assert tgt0_low > tgt1, "DTE0 should have higher target than DTE1"
    assert tgt1 > tgt2, "DTE1 should have higher target than DTE2"
    assert 0.30 <= tgt0_low <= 0.55, f"Target out of range: {tgt0_low}"
    print(
        f"  DTE0 VIX=11: {tgt0_low:.2f}  DTE0 VIX=16: {tgt0_high:.2f}  "
        f"DTE1: {tgt1:.2f}  DTE2: {tgt2:.2f} [OK]"
    )'''

new_test = '''    assert tgt0_low >= tgt0_high, f"Lower VIX should give higher target: {tgt0_low} vs {tgt0_high}"
    assert tgt0_low > tgt1, f"DTE0 {tgt0_low:.2f} should be > DTE1 {tgt1:.2f}"
    assert tgt1 > tgt2, f"DTE1 {tgt1:.2f} should be > DTE2 {tgt2:.2f}"
    assert 0.30 <= tgt0_low <= 0.55, f"Target out of range: {tgt0_low}"
    print(
        f"  DTE0 VIX=11: {tgt0_low:.2f}  DTE0 VIX=16: {tgt0_high:.2f}  "
        f"DTE1: {tgt1:.2f}  DTE2: {tgt2:.2f} [OK]"
    )'''

src2 = fpath.read_text(encoding="utf-8")
if old_test in src2:
    new_src2 = src2.replace(old_test, new_test, 1)
    try:
        ast.parse(new_src2)
        fpath.write_text(new_src2, encoding="utf-8")
        print("OK   Updated test assertions with better error messages")
    except SyntaxError as e:
        print(f"SYNTAX ERROR in test fix: {e}")
else:
    print("Test assertions already updated or pattern not found")

print("\nRun: python verify_all.py")