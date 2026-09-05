import os
import sys
import ast

TARGET = "regime_engine.py"

if not os.path.exists(TARGET):
    print(f"ERROR: {TARGET} not found.")
    sys.exit(1)

with open(TARGET, "r", encoding="utf-8") as fh:
    src = fh.read()

original_src = src
patches_applied = []
patches_failed = []


def apply(label, old, new):
    global src
    old = old.lstrip("\n")
    new = new.lstrip("\n")
    if old in src:
        src = src.replace(old, new, 1)
        patches_applied.append(label)
    else:
        patches_failed.append(label)


apply(
    "PATCH-1: Fix variable ordering in classify_positioning - move _cal and derived vars to top",
    (
        "    def classify_positioning(self, snap: MarketSnapshot) -> PositioningRegime:\n"
        "        strikes_half = max(Config.TOTAL_STRIKES // 2, 1)\n"
        "        avg_ce = max(snap.total_ce_oi / strikes_half, 1) if snap.total_ce_oi > 0 else 1.0\n"
        "        avg_pe = max(snap.total_pe_oi / strikes_half, 1) if snap.total_pe_oi > 0 else 1.0\n"
        "        r_str  = snap.resistance_oi / avg_ce if snap.total_ce_oi > 0 else 0.0\n"
        "        s_str  = snap.support_oi    / avg_pe if snap.total_pe_oi > 0 else 0.0\n"
        "        rng_pct = (abs(snap.resistance_strike - snap.support_strike) / snap.nifty_spot * 100\n"
        "                   if snap.resistance_strike and snap.support_strike else 0.0)\n"
        "        pcr = snap.pcr\n"
        "        oi_change = snap.oi_change_pct\n"
        "        skew      = snap.skew\n"
        "        strong = self._t(\"oi_wall_strong_threshold\", \"OI_WALL_STRONG\")\n"
        "        mod    = (_cal.oi_wall_moderate_cal if (_cal and _cal.is_calibrated) else Config.OI_WALL_MODERATE)\n"
        "        wall_range        = r_str >= mod and s_str >= mod and rng_pct < 4.0\n"
        "        wall_strong_range = r_str >= strong and s_str >= strong and 0 < rng_pct < 3.0\n"
        "        oi_building  = oi_change > _oi_build\n"
        "        oi_unwinding = oi_change < _oi_unwind\n"
        "        pcr_extreme_bull = pcr < Config.PCR_EXTREME_BULL\n"
        "        pcr_extreme_bear = pcr > Config.PCR_EXTREME_BEAR\n"
        "        pcr_bullish      = pcr < Config.PCR_BULLISH_THRESHOLD\n"
        "        pcr_bearish      = pcr > Config.PCR_BEARISH_THRESHOLD\n"
        "        _cal = self.cal\n"
        "        _skew_bear = _cal.skew_bearish_threshold if (_cal and _cal.is_calibrated) else Config.SKEW_BEARISH_THRESHOLD\n"
        "        _skew_bull = _cal.skew_bullish_threshold if (_cal and _cal.is_calibrated) else Config.SKEW_BULLISH_THRESHOLD\n"
        "        _oi_build  = _cal.oi_buildup_threshold   if (_cal and _cal.is_calibrated) else Config.OI_BUILDUP_THRESHOLD\n"
        "        _oi_unwind = _cal.oi_unwind_threshold    if (_cal and _cal.is_calibrated) else Config.OI_UNWIND_THRESHOLD\n"
        "        skew_bearish     = skew > _skew_bear\n"
        "        skew_bullish     = skew < _skew_bull"
    ),
    (
        "    def classify_positioning(self, snap: MarketSnapshot) -> PositioningRegime:\n"
        "        _cal = self.cal\n"
        "        _skew_bear = _cal.skew_bearish_threshold if (_cal and _cal.is_calibrated) else Config.SKEW_BEARISH_THRESHOLD\n"
        "        _skew_bull = _cal.skew_bullish_threshold if (_cal and _cal.is_calibrated) else Config.SKEW_BULLISH_THRESHOLD\n"
        "        _oi_build  = _cal.oi_buildup_threshold   if (_cal and _cal.is_calibrated) else Config.OI_BUILDUP_THRESHOLD\n"
        "        _oi_unwind = _cal.oi_unwind_threshold    if (_cal and _cal.is_calibrated) else Config.OI_UNWIND_THRESHOLD\n"
        "        strikes_half = max(Config.TOTAL_STRIKES // 2, 1)\n"
        "        avg_ce = max(snap.total_ce_oi / strikes_half, 1) if snap.total_ce_oi > 0 else 1.0\n"
        "        avg_pe = max(snap.total_pe_oi / strikes_half, 1) if snap.total_pe_oi > 0 else 1.0\n"
        "        r_str  = snap.resistance_oi / avg_ce if snap.total_ce_oi > 0 else 0.0\n"
        "        s_str  = snap.support_oi    / avg_pe if snap.total_pe_oi > 0 else 0.0\n"
        "        rng_pct = (abs(snap.resistance_strike - snap.support_strike) / snap.nifty_spot * 100\n"
        "                   if snap.resistance_strike and snap.support_strike else 0.0)\n"
        "        pcr = snap.pcr\n"
        "        oi_change = snap.oi_change_pct\n"
        "        skew      = snap.skew\n"
        "        strong = self._t(\"oi_wall_strong_threshold\", \"OI_WALL_STRONG\")\n"
        "        mod    = (_cal.oi_wall_moderate_cal if (_cal and _cal.is_calibrated) else Config.OI_WALL_MODERATE)\n"
        "        wall_range        = r_str >= mod and s_str >= mod and rng_pct < 4.0\n"
        "        wall_strong_range = r_str >= strong and s_str >= strong and 0 < rng_pct < 3.0\n"
        "        oi_building  = oi_change > _oi_build\n"
        "        oi_unwinding = oi_change < _oi_unwind\n"
        "        pcr_extreme_bull = pcr < Config.PCR_EXTREME_BULL\n"
        "        pcr_extreme_bear = pcr > Config.PCR_EXTREME_BEAR\n"
        "        pcr_bullish      = pcr < Config.PCR_BULLISH_THRESHOLD\n"
        "        pcr_bearish      = pcr > Config.PCR_BEARISH_THRESHOLD\n"
        "        skew_bearish     = skew > _skew_bear\n"
        "        skew_bullish     = skew < _skew_bull"
    )
)

if src == original_src:
    print("WARNING: No changes made. Marker failed.")
    for p in patches_failed:
        print(f"  FAILED: {p}")
    sys.exit(1)

try:
    ast.parse(src)
except SyntaxError as e:
    print(f"SYNTAX ERROR after patching: {e}")
    print("File NOT written. Original preserved.")
    sys.exit(1)

with open(TARGET, "w", encoding="utf-8") as fh:
    fh.write(src)

print(f"\npatch.py complete. File written: {TARGET}")
print(f"\nApplied ({len(patches_applied)}):")
for p in patches_applied:
    print(f"  OK: {p}")
if patches_failed:
    print(f"\nFailed ({len(patches_failed)}):")
    for p in patches_failed:
        print(f"  SKIP: {p}")
print("\nSyntax verified OK before write.")
print("Verification: python -m py_compile regime_engine.py && echo SYNTAX OK")