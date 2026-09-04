from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def verify_syntax(filename):
    import ast
    path = BASE_DIR / filename
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        ast.parse(source)
        print(f"  [OK] {filename} syntax valid")
        return True
    except SyntaxError as e:
        print(f"  [ERROR] {filename} line {e.lineno}: {e.msg}")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for idx in range(max(0, e.lineno - 4), min(len(lines), e.lineno + 4)):
            marker = ">>>" if idx + 1 == e.lineno else "   "
            print(f"    {marker} {idx+1}: {lines[idx].rstrip()}")
        return False


def fix_stop_premium_computation():
    path = BASE_DIR / "strategy_engine.py"
    content = read_file(path)
    changed = []

    old1 = (
        "        if strategy_type == \"SELL\":\n"
        "            _is_directional_sizing = strategy_name in (\"BULL_PUT_SPREAD\", \"BEAR_CALL_SPREAD\")\n"
        "            if _is_directional_sizing:\n"
        "                _gross_credit_for_stop = gross_credit if gross_credit and gross_credit > 0 else net_credit\n"
        "                stop_premium = _gross_credit_for_stop * 2.5\n"
        "                stop_based_loss = (_gross_credit_for_stop * 1.5) * C02 * 1.25\n"
        "            else:\n"
        "                stop_premium = net_credit * stop_multiplier\n"
        "                stop_based_loss = (stop_premium - net_credit) * C02 * 1.25"
    )
    if old1 in content:
        print("  [SKIP] stop_premium computation already patched correctly")
    else:
        old1b = (
            "        if strategy_type == \"SELL\":\n"
            "            stop_premium = net_credit * stop_multiplier\n"
            "            stop_based_loss = (stop_premium - net_credit) * C02 * 1.25\n"
            "            if actual_wing_pts is not None and actual_wing_pts > 0 and net_credit > 0:\n"
            "                contractual_max_loss = (actual_wing_pts - net_credit) * C02\n"
            "                max_loss_per_lot = min(stop_based_loss, contractual_max_loss) if contractual_max_loss > 0 else stop_based_loss\n"
            "            else:\n"
            "                max_loss_per_lot = stop_based_loss"
        )
        new1b = (
            "        if strategy_type == \"SELL\":\n"
            "            _is_directional_sizing = strategy_name in (\"BULL_PUT_SPREAD\", \"BEAR_CALL_SPREAD\")\n"
            "            if _is_directional_sizing:\n"
            "                _gross_credit_for_stop = gross_credit if gross_credit and gross_credit > 0 else net_credit\n"
            "                stop_premium = _gross_credit_for_stop * 2.5\n"
            "                stop_based_loss = (_gross_credit_for_stop * 1.5) * C02 * 1.25\n"
            "            else:\n"
            "                stop_premium = net_credit * stop_multiplier\n"
            "                stop_based_loss = (stop_premium - net_credit) * C02 * 1.25\n"
            "            if actual_wing_pts is not None and actual_wing_pts > 0 and net_credit > 0:\n"
            "                contractual_max_loss = (actual_wing_pts - net_credit) * C02\n"
            "                max_loss_per_lot = min(stop_based_loss, contractual_max_loss) if contractual_max_loss > 0 else stop_based_loss\n"
            "            else:\n"
            "                max_loss_per_lot = stop_based_loss"
        )
        if old1b in content:
            content = content.replace(old1b, new1b)
            changed.append("stop_premium: directional spreads use 2.5x gross_credit, neutral use net_credit × stop_multiplier")
        else:
            print("  [SKIP] stop_premium base block not found in any known form")

    write_file(path, content)
    for c in changed:
        print(f"  [OK] {c}")
    print("Checked: strategy_engine.py (stop_premium)")


def fix_target_pct():
    path = BASE_DIR / "strategy_engine.py"
    content = read_file(path)
    changed = []

    old1 = (
        "TARGET_PCT_BY_DAY = {\"MONDAY\": 0.32, \"TUESDAY\": 0.30, \"WEDNESDAY\": 0.35,\n"
        "                      \"THURSDAY\": 0.35, \"FRIDAY\": 0.32}"
    )
    new1 = (
        "TARGET_PCT_BY_DAY = {\"MONDAY\": 0.50, \"TUESDAY\": 0.45, \"WEDNESDAY\": 0.50,\n"
        "                      \"THURSDAY\": 0.50, \"FRIDAY\": 0.50}"
    )
    if old1 in content:
        content = content.replace(old1, new1)
        changed.append("TARGET_PCT_BY_DAY: raised to 50% for all days (was 32-35%)")
    else:
        old1b = (
            "TARGET_PCT_BY_DAY = {\"MONDAY\": 0.50, \"TUESDAY\": 0.45, \"WEDNESDAY\": 0.50,\n"
            "                      \"THURSDAY\": 0.50, \"FRIDAY\": 0.45}"
        )
        new1b = (
            "TARGET_PCT_BY_DAY = {\"MONDAY\": 0.50, \"TUESDAY\": 0.45, \"WEDNESDAY\": 0.50,\n"
            "                      \"THURSDAY\": 0.50, \"FRIDAY\": 0.50}"
        )
        if old1b in content:
            content = content.replace(old1b, new1b)
            changed.append("TARGET_PCT_BY_DAY: Friday raised from 0.45 to 0.50")
        else:
            old1c = (
                "TARGET_PCT_BY_DAY = {\"MONDAY\": 0.50, \"TUESDAY\": 0.45, \"WEDNESDAY\": 0.50,\n"
                "                      \"THURSDAY\": 0.50, \"FRIDAY\": 0.50}"
            )
            if old1c in content:
                print("  [SKIP] TARGET_PCT_BY_DAY already at target values")
            else:
                print("  [SKIP] TARGET_PCT_BY_DAY not found in any known form")

    write_file(path, content)
    for c in changed:
        print(f"  [OK] {c}")
    print("Checked: strategy_engine.py (target_pct)")


def fix_tightening_schedule():
    path = BASE_DIR / "strategy_engine.py"
    content = read_file(path)
    changed = []

    old1 = (
        "    def _build_tightening_schedule(self, day_label: Optional[str]) -> list:\n"
        "        if day_label == \"TUESDAY\":\n"
        "            return [(\"11:00\", 0.80), (\"12:00\", 0.65), (\"12:30\", 0.50), (\"13:00\", 0.35)]\n"
        "        return [(\"13:00\", 0.85), (\"14:00\", 0.70), (\"14:30\", 0.55)]"
    )
    if old1 in content:
        print("  [SKIP] tightening_schedule already at target values")
    else:
        old1b = (
            "    def _build_tightening_schedule(self, day_label: Optional[str]) -> list:\n"
            "        if day_label == \"TUESDAY\":\n"
            "            return [(\"11:00\", 0.80), (\"12:00\", 0.65), (\"13:00\", 0.50)]\n"
            "        return [(\"13:00\", 0.80), (\"14:00\", 0.65)]\n"
            "        tightening_profit_gated = True"
        )
        new1b = (
            "    def _build_tightening_schedule(self, day_label: Optional[str]) -> list:\n"
            "        if day_label == \"TUESDAY\":\n"
            "            return [(\"11:00\", 0.80), (\"12:00\", 0.65), (\"12:30\", 0.50), (\"13:00\", 0.35)]\n"
            "        return [(\"13:00\", 0.85), (\"14:00\", 0.70), (\"14:30\", 0.55)]"
        )
        if old1b in content:
            content = content.replace(old1b, new1b)
            changed.append("Tightening schedule: Tuesday more aggressive for 0DTE, non-Tuesday improved")
        else:
            old1c = (
                "    def _build_tightening_schedule(self, day_label: Optional[str]) -> list:\n"
                "        if day_label == \"TUESDAY\":\n"
                "            return [(\"11:00\", 0.80), (\"12:00\", 0.65), (\"13:00\", 0.50)]\n"
                "        return [(\"13:00\", 0.80), (\"14:00\", 0.65)]"
            )
            new1c = (
                "    def _build_tightening_schedule(self, day_label: Optional[str]) -> list:\n"
                "        if day_label == \"TUESDAY\":\n"
                "            return [(\"11:00\", 0.80), (\"12:00\", 0.65), (\"12:30\", 0.50), (\"13:00\", 0.35)]\n"
                "        return [(\"13:00\", 0.85), (\"14:00\", 0.70), (\"14:30\", 0.55)]"
            )
            if old1c in content:
                content = content.replace(old1c, new1c)
                changed.append("Tightening schedule updated")
            else:
                print("  [SKIP] _build_tightening_schedule not found in any known form")

    write_file(path, content)
    for c in changed:
        print(f"  [OK] {c}")
    print("Checked: strategy_engine.py (tightening_schedule)")


def fix_hard_exit_time():
    path = BASE_DIR / "market_data_engine.py"
    content = read_file(path)
    changed = []

    old1 = (
        "            \"hard_exit_time\": self.config.hard_exit_time.strftime(\"%H:%M\"),"
    )
    new1 = (
        "            \"hard_exit_time\": max(self.config.hard_exit_time, dtime(15, 25)).strftime(\"%H:%M\"),"
    )
    if old1 in content:
        content = content.replace(old1, new1)
        changed.append("hard_exit_time in session_state defaults to at least 15:25")
    else:
        print("  [SKIP] hard_exit_time in session_state defaults not found")

    write_file(path, content)
    for c in changed:
        print(f"  [OK] {c}")
    print("Checked: market_data_engine.py (hard_exit_time)")


def fix_env_txt_trading_window():
    path = BASE_DIR / "env.txt"
    if not path.exists():
        print("  [SKIP] env.txt not found")
        return
    content = read_file(path)
    changed = []

    replacements = [
        ("TRADING_WINDOW_START=10:00", "TRADING_WINDOW_START=10:30"),
        ("TRADING_WINDOW_LAST_ENTRY=14:00", "TRADING_WINDOW_LAST_ENTRY=13:00"),
        ("TRADING_WINDOW_LAST_ENTRY=15:00", "TRADING_WINDOW_LAST_ENTRY=13:00"),
        ("HARD_EXIT_TIME=15:00", "HARD_EXIT_TIME=15:25"),
    ]

    for old, new in replacements:
        if old in content:
            content = content.replace(old, new)
            changed.append(f"env.txt: {old} -> {new}")

    write_file(path, content)
    for c in changed:
        print(f"  [OK] {c}")
    print("Checked: env.txt")


def main():
    print("Fixing parameter values for next fresh position...")
    print()
    print("Note: The current open position uses old parameters stored at entry time.")
    print("These fixes apply to the NEXT position entered after a fresh session.")
    print()

    print("--- strategy_engine.py: stop_premium ---")
    fix_stop_premium_computation()
    print()

    print("--- strategy_engine.py: target_pct ---")
    fix_target_pct()
    print()

    print("--- strategy_engine.py: tightening_schedule ---")
    fix_tightening_schedule()
    print()

    print("--- market_data_engine.py: hard_exit_time ---")
    fix_hard_exit_time()
    print()

    print("--- env.txt: trading window ---")
    fix_env_txt_trading_window()
    print()

    print("--- Syntax verification ---")
    verify_syntax("strategy_engine.py")
    verify_syntax("market_data_engine.py")
    print()
    print("All fixes complete.")
    print()
    print("For the current open position:")
    print("  stop_premium=28.75 (old 1.3x net_credit)")
    print("  This position will use its stored parameters until it closes")
    print("  The next position will use: stop=2.5x gross_credit=57.5pts")
    print()
    print("Run: python main.py")


if __name__ == "__main__":
    main()