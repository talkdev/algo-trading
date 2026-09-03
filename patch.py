import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def fix_pcr_baseline_column_error():
    filename = "market_data_engine.py"
    src = read(filename)

    old = (
        "        if current_time >= dtime(10, 0) and not self.state.get(\"pcr_baseline_set_at_10am\"):\n"
        "            if pcr:\n"
        "                self.state[\"opening_pcr\"] = pcr\n"
        "                self.state[\"pcr_baseline_set_at_10am\"] = True\n"
        "                self.logger.info(f\"PCR baseline set at 10:00: opening_pcr={pcr:.3f}\")"
    )
    new = (
        "        if current_time >= dtime(10, 0) and not getattr(self, \"_pcr_baseline_set\", False):\n"
        "            if pcr:\n"
        "                self.state[\"opening_pcr\"] = pcr\n"
        "                self._pcr_baseline_set = True\n"
        "                self.logger.info(f\"PCR baseline set at 10:00: opening_pcr={pcr:.3f}\")"
    )
    assert old in src, "fix_pcr_baseline_column_error: block not found"
    src = src.replace(old, new, 1)
    write(filename, src)
    print("fixed market_data_engine.py pcr_baseline uses instance var not session_state column")


def fix_or_volume_filter_also_check_stop_reason_suppression():
    filename = "market_data_engine.py"
    src = read(filename)

    if "pcr_baseline_set_at_10am" in src:
        print("WARNING: pcr_baseline_set_at_10am still present in market_data_engine.py")
        idx = src.find("pcr_baseline_set_at_10am")
        print(repr(src[max(0, idx-100):idx+200]))
    else:
        print("market_data_engine.py: pcr_baseline_set_at_10am fully removed — OK")


def verify():
    errors = []
    src = read("market_data_engine.py")

    if "pcr_baseline_set_at_10am" in src:
        errors.append("market_data_engine: pcr_baseline_set_at_10am still present (will cause DB column error)")
    if "_pcr_baseline_set" not in src:
        errors.append("market_data_engine: _pcr_baseline_set instance var not present")
    if "getattr(self, \"_pcr_baseline_set\", False)" not in src:
        errors.append("market_data_engine: _pcr_baseline_set check not present")
    if "n_candles >= 6 and opening_iv" not in src:
        errors.append("market_data_engine: n_candles guard missing")
    if "last_candle_close_val: float = 0.0" not in src:
        errors.append("market_data_engine: last_candle_close_val parameter missing")
    if "or_volume_filter_removed" not in src:
        errors.append("market_data_engine: OR volume filter fix missing")
    if "vrp_blend_weight" not in src:
        errors.append("market_data_engine: VRP intraday blend missing")
    if "if vix < 14.0: return \"LOW\"" not in src:
        errors.append("market_data_engine: VIX thresholds missing")

    if errors:
        print("\nVERIFICATION FAILED:")
        for e in errors:
            print("  ERROR: " + e)
        sys.exit(1)
    else:
        print("\nAll fixes verified successfully.")


if __name__ == "__main__":
    print("Fixing pcr_baseline_set_at_10am DB column error...")
    fix_pcr_baseline_column_error()
    fix_or_volume_filter_also_check_stop_reason_suppression()
    verify()
    print("\nDone.")