from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def patch_execution_engine_directional_stop():
    path = BASE_DIR / "execution_engine.py"
    content = read_file(path)
    changed = []

    old1 = (
        "        if strategy_type == \"SELL\" and position.get(\"entry_credit\") and position[\"entry_credit\"] > 0:\n"
        "            _is_directional = strategy_name in (\"BULL_PUT_SPREAD\", \"BEAR_CALL_SPREAD\")\n"
        "            _stop_multiplier = 2.0 if _is_directional else 1.8\n"
        "            _credit_stop_limit = position[\"entry_credit\"] * _stop_multiplier\n"
        "            _actual_stop = min(\n"
        "                effective_stop if effective_stop is not None else _credit_stop_limit,\n"
        "                _credit_stop_limit\n"
        "            )\n"
        "            if current_premium >= _actual_stop:\n"
        "                return \"CLOSE_STOP\", {\"current_premium\": current_premium, \"effective_stop\": _actual_stop}"
    )
    new1 = (
        "        if strategy_type == \"SELL\" and position.get(\"entry_credit\") and position[\"entry_credit\"] > 0:\n"
        "            _is_directional = strategy_name in (\"BULL_PUT_SPREAD\", \"BEAR_CALL_SPREAD\")\n"
        "            if _is_directional:\n"
        "                _gross_credit = position.get(\"gross_credit\") or position[\"entry_credit\"]\n"
        "                _credit_stop_limit = _gross_credit * 2.5\n"
        "                if current_premium >= _credit_stop_limit:\n"
        "                    return \"CLOSE_STOP\", {\"current_premium\": current_premium, \"effective_stop\": _credit_stop_limit}\n"
        "            else:\n"
        "                _credit_stop_limit = position[\"entry_credit\"] * 1.8\n"
        "                _actual_stop = min(\n"
        "                    effective_stop if effective_stop is not None else _credit_stop_limit,\n"
        "                    _credit_stop_limit\n"
        "                )\n"
        "                if current_premium >= _actual_stop:\n"
        "                    return \"CLOSE_STOP\", {\"current_premium\": current_premium, \"effective_stop\": _actual_stop}"
    )
    if old1 in content:
        content = content.replace(old1, new1)
        changed.append("Directional spreads: stop based on 2.5x GROSS credit (not net). Neutral strategies: 1.8x net credit.")
    else:
        print("  [SKIP] credit stop block not found in expected form")
        for variant in [
            "            _stop_multiplier = 2.0 if _is_directional else 1.8",
            "            _stop_multiplier = 2.5 if _is_directional else 2.0",
        ]:
            if variant in content:
                print(f"  Found variant: {repr(variant)}")

    old2 = (
        "        if strategy_type == \"SELL\":\n"
        "            _is_directional_delta = strategy_name in (\"BULL_PUT_SPREAD\", \"BEAR_CALL_SPREAD\")\n"
        "            _delta_close_limit = 0.50 if _is_directional_delta else 0.40\n"
        "            _delta_tighten_limit = 0.42 if _is_directional_delta else 0.32\n"
        "            for leg in legs:\n"
        "                if leg[\"action\"] == \"SELL\" and leg[\"leg_status\"] == \"OPEN\":\n"
        "                    opt = chain.get(leg[\"strike\"], {}).get(leg[\"option_type\"], {}) if chain else {}\n"
        "                    current_delta = abs(opt.get(\"delta\", leg[\"entry_delta\"]) or 0)\n"
        "                    if current_delta > _delta_close_limit:\n"
        "                        return \"CLOSE_STOP\", {\"reason_detail\": f\"short_leg_delta_breach_{current_delta:.3f}\"}\n"
        "                    if current_delta > _delta_tighten_limit and not position.get(\"stop_tightened_for_delta\"):\n"
        "                        self.db.update(\n"
        "                            \"positions\",\n"
        "                            {\"stop_premium\": position[\"stop_premium\"] * 0.80, \"stop_tightened_for_delta\": 1},\n"
        "                            {\"position_id\": position[\"position_id\"]},\n"
        "                        )\n"
        "                        self.logger.info(f\"Short leg delta approaching limit ({current_delta:.3f}) — \"\n"
        "                                          f\"tightening stop for {strategy_name}\")\n"
        "                        return \"TIGHTEN_STOP\", {}"
    )
    new2 = (
        "        if strategy_type == \"SELL\":\n"
        "            _is_directional_delta = strategy_name in (\"BULL_PUT_SPREAD\", \"BEAR_CALL_SPREAD\")\n"
        "            _delta_close_limit = 0.42 if _is_directional_delta else 0.40\n"
        "            _delta_tighten_limit = 0.35 if _is_directional_delta else 0.32\n"
        "            for leg in legs:\n"
        "                if leg[\"action\"] == \"SELL\" and leg[\"leg_status\"] == \"OPEN\":\n"
        "                    opt = chain.get(leg[\"strike\"], {}).get(leg[\"option_type\"], {}) if chain else {}\n"
        "                    current_delta = abs(opt.get(\"delta\", leg[\"entry_delta\"]) or 0)\n"
        "                    if current_delta > _delta_close_limit:\n"
        "                        return \"CLOSE_STOP\", {\"reason_detail\": f\"short_leg_delta_breach_{current_delta:.3f}\"}\n"
        "                    if current_delta > _delta_tighten_limit and not position.get(\"stop_tightened_for_delta\"):\n"
        "                        self.db.update(\n"
        "                            \"positions\",\n"
        "                            {\"stop_premium\": position[\"stop_premium\"] * 0.80, \"stop_tightened_for_delta\": 1},\n"
        "                            {\"position_id\": position[\"position_id\"]},\n"
        "                        )\n"
        "                        self.logger.info(f\"Short leg delta approaching limit ({current_delta:.3f}) — \"\n"
        "                                          f\"tightening stop for {strategy_name}\")\n"
        "                        return \"TIGHTEN_STOP\", {}"
    )
    if old2 in content:
        content = content.replace(old2, new2)
        changed.append("Delta breach: directional close at 0.42 (was 0.50), tighten at 0.35 (was 0.42). Delta is now primary stop for directional spreads.")
    else:
        print("  [SKIP] delta breach block not found in expected form")

    write_file(path, content)
    for c in changed:
        print(f"  [OK] {c}")
    print("Patched: execution_engine.py")


def patch_strategy_engine_directional_stop():
    path = BASE_DIR / "strategy_engine.py"
    content = read_file(path)
    changed = []

    old1 = (
        "        if strategy_type == \"SELL\":\n"
        "            stop_premium = net_credit * stop_multiplier\n"
        "            stop_based_loss = (stop_premium - net_credit) * C02 * 1.25\n"
        "            if actual_wing_pts is not None and actual_wing_pts > 0 and net_credit > 0:\n"
        "                contractual_max_loss = (actual_wing_pts - net_credit) * C02\n"
        "                max_loss_per_lot = min(stop_based_loss, contractual_max_loss) if contractual_max_loss > 0 else stop_based_loss\n"
        "            else:\n"
        "                max_loss_per_lot = stop_based_loss"
    )
    new1 = (
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
    if old1 in content:
        content = content.replace(old1, new1)
        changed.append("Directional spread stop_premium now computed as 2.5x gross_credit. Sizing uses 1.5x gross_credit as expected loss.")
    else:
        print("  [SKIP] stop_premium computation block not found")

    old2 = (
        "            \"stop_premium\": stop_premium if strategy_type == \"SELL\" else None,"
    )
    new2 = (
        "            \"stop_premium\": stop_premium if strategy_type == \"SELL\" else None,\n"
        "            \"stop_basis\": \"gross_credit_2.5x\" if strategy_name in (\"BULL_PUT_SPREAD\", \"BEAR_CALL_SPREAD\") else \"net_credit_multiplier\","
    )
    if old2 in content and "stop_basis" not in content:
        content = content.replace(old2, new2)
        changed.append("Added stop_basis field to params for audit trail")
    else:
        print("  [SKIP] stop_premium return line not found or stop_basis already present")

    write_file(path, content)
    for c in changed:
        print(f"  [OK] {c}")
    print("Patched: strategy_engine.py")


def patch_execution_engine_time_stop_directional():
    path = BASE_DIR / "execution_engine.py"
    content = read_file(path)
    changed = []

    old1 = (
        "        if strategy_type == \"SELL\" and position.get(\"entry_credit\"):\n"
        "            _entry_credit_td = position[\"entry_credit\"]\n"
        "            _now_time_td = now_ist().time()\n"
        "            if _now_time_td >= dtime(14, 30):\n"
        "                _time_target_pct = 0.35\n"
        "            elif _now_time_td >= dtime(14, 0):\n"
        "                _time_target_pct = 0.40\n"
        "            elif _now_time_td >= dtime(13, 0):\n"
        "                _time_target_pct = 0.45\n"
        "            else:\n"
        "                _time_target_pct = None"
    )
    new1 = (
        "        if strategy_type == \"SELL\" and position.get(\"entry_credit\"):\n"
        "            _entry_credit_td = position[\"entry_credit\"]\n"
        "            _now_time_td = now_ist().time()\n"
        "            _is_directional_time = strategy_name in (\"BULL_PUT_SPREAD\", \"BEAR_CALL_SPREAD\")\n"
        "            if _is_directional_time:\n"
        "                if _now_time_td >= dtime(15, 0):\n"
        "                    _time_target_pct = 0.30\n"
        "                elif _now_time_td >= dtime(14, 30):\n"
        "                    _time_target_pct = 0.38\n"
        "                elif _now_time_td >= dtime(14, 0):\n"
        "                    _time_target_pct = 0.45\n"
        "                else:\n"
        "                    _time_target_pct = None\n"
        "            else:\n"
        "                if _now_time_td >= dtime(14, 30):\n"
        "                    _time_target_pct = 0.35\n"
        "                elif _now_time_td >= dtime(14, 0):\n"
        "                    _time_target_pct = 0.40\n"
        "                elif _now_time_td >= dtime(13, 0):\n"
        "                    _time_target_pct = 0.45\n"
        "                else:\n"
        "                    _time_target_pct = None"
    )
    if old1 in content:
        content = content.replace(old1, new1)
        changed.append("Time-based target: directional spreads start later (14:00) to let theta work longer. Neutral strategies unchanged.")
    else:
        print("  [SKIP] time-based target block not found")

    write_file(path, content)
    for c in changed:
        print(f"  [OK] {c}")
    print("Patched: execution_engine.py (time stop)")


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
        print(f"  [ERROR] {filename} syntax error at line {e.lineno}: {e.msg}")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        start = max(0, e.lineno - 4)
        end = min(len(lines), e.lineno + 4)
        for idx in range(start, end):
            marker = ">>>" if idx + 1 == e.lineno else "   "
            print(f"    {marker} {idx+1}: {lines[idx].rstrip()}")
        return False


def main():
    print("Applying directional spread stop calibration patch...")
    print()
    print("Problem being fixed:")
    print("  BULL_PUT_SPREAD entered at 22.11pt net credit")
    print("  Stop at 1.3x = 28.75pts — only 6.64pts of room")
    print("  A 25pt NIFTY move triggers stop via normal noise")
    print("  Position stopped out before real risk materializes")
    print()
    print("Fix:")
    print("  Directional spreads: stop = 2.5x GROSS credit")
    print("  For 23pt gross credit: stop = 57.5pts")
    print("  Room = 57.5 - 22.11 = 35.4pts of premium movement")
    print("  Corresponds to ~130pt NIFTY adverse move")
    print("  Delta breach at 0.42 is primary exit signal")
    print("  Price stop 80pts remains as hard backstop")
    print()

    print("--- execution_engine.py (stop logic) ---")
    patch_execution_engine_directional_stop()
    print()

    print("--- strategy_engine.py (stop computation) ---")
    patch_strategy_engine_directional_stop()
    print()

    print("--- execution_engine.py (time-based target) ---")
    patch_execution_engine_time_stop_directional()
    print()

    print("--- Syntax verification ---")
    verify_syntax("execution_engine.py")
    verify_syntax("strategy_engine.py")
    print()

    print("Patch complete.")
    print()
    print("New stop behavior for BULL_PUT_SPREAD / BEAR_CALL_SPREAD:")
    print("  Primary exit: short leg delta > 0.42 (spot approaching short strike)")
    print("  Secondary exit: combined premium > 2.5x gross credit")
    print("  Tertiary exit: price stop 80pts NIFTY move")
    print("  Final exit: hard exit at 15:25")
    print()
    print("New stop behavior for IRON_CONDOR / IRON_BUTTERFLY:")
    print("  Unchanged: combined premium > 1.8x net credit")
    print("  Delta breach at 0.40")
    print()
    print("This patch takes effect on next engine restart.")
    print("Current open position uses old stop parameters stored in DB.")
    print("Run: python main.py")


if __name__ == "__main__":
    main()