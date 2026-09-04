from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

print("=" * 60)
print("PATCH V5 FINAL CHECK AND ADX FIX")
print("=" * 60)

mde_path = BASE_DIR / "market_data_engine.py"
c = mde_path.read_text(encoding="utf-8")

print("\n--- ADX block current state ---")
adx_idx = c.find("reliability = \"LOW\" if n_bars < 35")
if adx_idx >= 0:
    print(repr(c[adx_idx:adx_idx+400]))
else:
    print("  ADX block not found")

changed = False

old_adx_or = (
    "            reliability = \"LOW\" if n_bars < 35 else (\"MEDIUM\" if n_bars < 50 else \"HIGH\")\n"
    "            adx_value, pdi, ndi, adx_dir = self._compute_adx(candles_today, period=14)\n"
    "            if reliability == \"LOW\" or n_bars < 35:\n"
    "                adx_condition = \"EARLY_SESSION\"\n"
    "                adx_dir = \"UNKNOWN\"\n"
    "                adx_value = 0.0\n"
    "            else:\n"
    "                adx_condition, _ = self._classify_adx(adx_value)"
)
new_adx_clean = (
    "            reliability = \"LOW\" if n_bars < 35 else (\"MEDIUM\" if n_bars < 50 else \"HIGH\")\n"
    "            adx_value, pdi, ndi, adx_dir = self._compute_adx(candles_today, period=14)\n"
    "            if n_bars < 35:\n"
    "                adx_condition = \"EARLY_SESSION\"\n"
    "                adx_dir = \"UNKNOWN\"\n"
    "                adx_value = 0.0\n"
    "            else:\n"
    "                adx_condition, _ = self._classify_adx(adx_value)"
)

if old_adx_or in c:
    c = c.replace(old_adx_or, new_adx_clean, 1)
    print("  PATCHED: ADX condition simplified to if n_bars < 35")
    changed = True
elif new_adx_clean in c:
    print("  ALREADY CORRECT: ADX uses if n_bars < 35")
else:
    print("  NOT FOUND: ADX block in either form")

if changed:
    mde_path.write_text(c, encoding="utf-8")
    print("  SAVED: market_data_engine.py")

print("\n--- Final verification of ALL patches ---")
checks = [
    (BASE_DIR / "market_data_engine.py", [
        ("prefer_dte_min=1", "prefer_dte_min: int = 1"),
        ("Parkinson no volume filter", "b[\"high\"] >= b[\"low\"]]"),
        ("VWAP fallback False", "return equal_pv / total_bars, False"),
        ("PCR OTM", "_pcr_band = spot * 0.03"),
        ("ADX 35-bar gate", "if n_bars < 35:\n                adx_condition = \"EARLY_SESSION\""),
        ("OR coverage gate", "_or_bars_available"),
        ("DOW tables inverted", "\"MONDAY\": 1.00"),
        ("VWAP signal flag", "_vwap_valid_flag"),
        ("discover_active_expiry call", "discover_active_expiry(prefer_dte_min=1)"),
    ]),
    (BASE_DIR / "strategy_engine.py", [
        ("LOT_CAPS inverted", "\"MONDAY\": 3"),
        ("structural cap", "_structural_max"),
        ("theta budget gate", "_theta_capture_pts"),
    ]),
    (BASE_DIR / "execution_engine.py", [
        ("exit leg SELL first", "key=lambda l: 0 if l[\"action\"] == \"SELL\" else 1"),
        ("price stop directional", "_sname_ps = position.get(\"strategy_name\""),
        ("profit lock 70pct", "entry_credit * 0.70"),
    ]),
    (BASE_DIR / "main.py", [
        ("monitoring 60s", "_has_open = len(self.execution_engine._get_open_positions()) > 0"),
    ]),
    (BASE_DIR / "backtest.py", [
        ("double-cost fix", "net_credit = gross_credit - slip"),
        ("gross_pnl gross_credit", "gross_pnl_pts = gross_credit - exit_prem"),
        ("ADX condor exit 25", "and adx > 25:"),
    ]),
]

all_ok = True
for fpath, items in checks:
    fc = fpath.read_text(encoding="utf-8")
    for label, check_str in items:
        status = "OK" if check_str in fc else "MISSING"
        if status == "MISSING":
            all_ok = False
        print(f"  {status}: [{fpath.name}] {label}")

print()
if all_ok:
    print("ALL 19 PATCHES CONFIRMED APPLIED — ENGINE READY")
else:
    print("SOME PATCHES STILL MISSING")