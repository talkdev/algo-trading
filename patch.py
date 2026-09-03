import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(filename):
    return (BASE / filename).read_text(encoding="utf-8")


def write(filename, content):
    (BASE / filename).write_text(content, encoding="utf-8")


def patch_nifty_algo_core():
    filename = "nifty_algo_core.py"
    src = read(filename)

    old = "MAX_RISK_PER_TRADE_PCT=0.005"
    new = "MAX_RISK_PER_TRADE_PCT=0.010"
    assert old in src, "patch_nifty_algo_core MAX_RISK not found"
    src = src.replace(old, new, 1)

    write(filename, src)
    print("patched " + filename)


def patch_market_data_engine():
    filename = "market_data_engine.py"
    src = read(filename)

    old = (
        "    def compute_vwap(self, candles_today: list) -> tuple[Optional[float], bool]:\n"
        "        if len(candles_today) < 3:\n"
        "            return None, False\n"
        "        cum_pv, cum_vol = 0.0, 0.0\n"
        "        for b in candles_today:\n"
        "            typical = (b[\"high\"] + b[\"low\"] + b[\"close\"]) / 3.0\n"
        "            cum_pv += typical * b[\"volume\"]\n"
        "            cum_vol += b[\"volume\"]\n"
        "        if cum_vol <= 0:\n"
        "            return None, False\n"
        "        return cum_pv / cum_vol, True"
    )
    new = (
        "    def compute_vwap(self, candles_today: list) -> tuple[Optional[float], bool]:\n"
        "        if len(candles_today) < 3:\n"
        "            return None, False\n"
        "        cum_pv, cum_vol = 0.0, 0.0\n"
        "        for b in candles_today:\n"
        "            typical = (b[\"high\"] + b[\"low\"] + b[\"close\"]) / 3.0\n"
        "            cum_pv += typical * b[\"volume\"]\n"
        "            cum_vol += b[\"volume\"]\n"
        "        if cum_vol > 0:\n"
        "            return cum_pv / cum_vol, True\n"
        "        total_bars = len(candles_today)\n"
        "        if total_bars < 3:\n"
        "            return None, False\n"
        "        equal_pv = sum((b[\"high\"] + b[\"low\"] + b[\"close\"]) / 3.0 for b in candles_today)\n"
        "        return equal_pv / total_bars, True"
    )
    assert old in src, "patch_market_data_engine vwap not found"
    src = src.replace(old, new, 1)

    old = (
        "    def compute_parkinson_rv(self, vix: Optional[float]) -> tuple[Optional[float], str]:\n"
        "        today_str = today_ist().isoformat()\n"
        "        if (self.state.get(\"parkinson_rv_computed_date\") == today_str\n"
        "                and self.state.get(\"parkinson_rv_pct\") is not None):\n"
        "            return self.state[\"parkinson_rv_pct\"], \"cached\""
    )
    new = (
        "    def compute_intraday_parkinson_rv(self, candles_today: list) -> Optional[float]:\n"
        "        valid = [b for b in candles_today if b[\"high\"] > b[\"low\"] and b[\"high\"] > 0]\n"
        "        if len(valid) < 6:\n"
        "            return None\n"
        "        log_hl_sq = [math.log(b[\"high\"] / b[\"low\"]) ** 2 for b in valid]\n"
        "        park_const = 1.0 / (4.0 * math.log(2.0))\n"
        "        variance_per_bar = park_const * (sum(log_hl_sq) / len(log_hl_sq))\n"
        "        annual_variance = variance_per_bar * (75.0 * 252.0)\n"
        "        rv = math.sqrt(annual_variance)\n"
        "        if rv < 0.02 or rv > 0.80:\n"
        "            return None\n"
        "        return rv\n"
        "\n"
        "    def compute_parkinson_rv(self, vix: Optional[float]) -> tuple[Optional[float], str]:\n"
        "        today_str = today_ist().isoformat()\n"
        "        if (self.state.get(\"parkinson_rv_computed_date\") == today_str\n"
        "                and self.state.get(\"parkinson_rv_pct\") is not None):\n"
        "            return self.state[\"parkinson_rv_pct\"], \"cached\""
    )
    assert old in src, "patch_market_data_engine parkinson not found"
    src = src.replace(old, new, 1)

    old = (
        "        parkinson_rv, rv_source = self.compute_parkinson_rv(vix)\n"
        "        vrp = (atm_iv * 100.0 - parkinson_rv * 100.0) if (atm_iv is not None and parkinson_rv is not None) else None"
    )
    new = (
        "        parkinson_rv, rv_source = self.compute_parkinson_rv(vix)\n"
        "        intraday_rv = self.compute_intraday_parkinson_rv(candles_today)\n"
        "        effective_rv = parkinson_rv\n"
        "        if intraday_rv is not None and parkinson_rv is not None:\n"
        "            effective_rv = max(parkinson_rv, intraday_rv)\n"
        "        elif intraday_rv is not None:\n"
        "            effective_rv = intraday_rv\n"
        "        vrp = (atm_iv * 100.0 - effective_rv * 100.0) if (atm_iv is not None and effective_rv is not None) else None\n"
        "        intraday_rv_selling_veto = (intraday_rv is not None and atm_iv is not None and intraday_rv > atm_iv)"
    )
    assert old in src, "patch_market_data_engine vrp_calc not found"
    src = src.replace(old, new, 1)

    old = (
        "        signals = {\n"
        "            \"trading_date\": trading_date, \"spot\": spot, \"vix\": vix,"
    )
    new = (
        "        signals = {\n"
        "            \"trading_date\": trading_date, \"spot\": spot, \"vix\": vix,\n"
        "            \"intraday_rv_selling_veto\": intraday_rv_selling_veto,"
    )
    assert old in src, "patch_market_data_engine signals_dict not found"
    src = src.replace(old, new, 1)

    old = (
        "        if vrp > 6.0: vol_cond, vol_score, sell_ok = \"VERY_RICH\", 2, True\n"
        "        elif vrp > 2.0: vol_cond, vol_score, sell_ok = \"RICH\", 1, True\n"
        "        elif vrp > 0.5: vol_cond, vol_score, sell_ok, sell_reduction = \"FAIR\", 0, True, 0.5\n"
        "        elif vrp > -0.5: vol_cond, vol_score = \"THIN\", -1\n"
        "        elif vrp > -2.0: vol_cond, vol_score, buy_ok = \"CHEAP\", -2, True\n"
        "        else: vol_cond, vol_score, buy_ok = \"INVERTED\", -3, True"
    )
    new = (
        "        if vrp > 5.0: vol_cond, vol_score, sell_ok = \"VERY_RICH\", 2, True\n"
        "        elif vrp > 3.0: vol_cond, vol_score, sell_ok = \"RICH\", 1, True\n"
        "        elif vrp > 1.5: vol_cond, vol_score, sell_ok, sell_reduction = \"FAIR\", 0, True, 0.5\n"
        "        elif vrp > 0.0: vol_cond, vol_score = \"THIN\", -1\n"
        "        elif vrp > -2.0: vol_cond, vol_score, buy_ok = \"CHEAP\", -2, True\n"
        "        else: vol_cond, vol_score, buy_ok = \"INVERTED\", -3, True"
    )
    assert old in src, "patch_market_data_engine vrp_bands not found"
    src = src.replace(old, new, 1)

    write(filename, src)
    print("patched " + filename)


def patch_strategy_engine():
    filename = "strategy_engine.py"
    src = read(filename)

    old = (
        "        if s[\"vix_spike_detected\"]:\n"
        "            return \"NO_TRADE\", \"vix_spike_detected_no_new_sells\""
    )
    new = (
        "        if s[\"vix_spike_detected\"]:\n"
        "            return \"NO_TRADE\", \"vix_spike_detected_no_new_sells\"\n"
        "        if s.get(\"intraday_rv_selling_veto\"):\n"
        "            return \"NO_TRADE\", \"intraday_rv_exceeds_atm_iv_no_premium_selling\""
    )
    assert old in src, "patch_strategy_engine intraday_rv_veto not found"
    src = src.replace(old, new, 1)

    old = (
        "        elif vol == \"FAIR\" and sell_ok:\n"
        "            if trend in (\"RANGE_BOUND\", \"MILD_RANGE\", \"RANGE_ASSUMED\"):\n"
        "                if dirn in (\"BULLISH\", \"MILD_BULLISH\"):\n"
        "                    if vwap_sig not in (\"BEARISH\", \"BEARISH_EXTENDED\"):\n"
        "                        return \"BULL_PUT_SPREAD\", \"bullish+fair_vrp+range_half_size\"\n"
        "                    return \"NO_TRADE\", \"fair_vrp_vwap_contradicts_direction\"\n"
        "                elif dirn in (\"BEARISH\", \"MILD_BEARISH\"):\n"
        "                    if vwap_sig not in (\"BULLISH\", \"BULLISH_EXTENDED\"):\n"
        "                        return \"BEAR_CALL_SPREAD\", \"bearish+fair_vrp+range_half_size\"\n"
        "                    return \"NO_TRADE\", \"fair_vrp_vwap_contradicts_direction\"\n"
        "                return \"NO_TRADE\", \"fair_vrp_neutral_direction_insufficient_edge\"\n"
        "            return \"NO_TRADE\", \"fair_vrp_trending_no_trade\""
    )
    new = (
        "        elif vol == \"FAIR\" and sell_ok:\n"
        "            if trend in (\"RANGE_BOUND\", \"MILD_RANGE\", \"RANGE_ASSUMED\"):\n"
        "                if dirn in (\"BULLISH\", \"MILD_BULLISH\"):\n"
        "                    if vwap_sig not in (\"BEARISH\", \"BEARISH_EXTENDED\"):\n"
        "                        return \"BULL_PUT_SPREAD\", \"bullish+fair_vrp+range_half_size\"\n"
        "                    return \"NO_TRADE\", \"fair_vrp_vwap_contradicts_direction\"\n"
        "                elif dirn in (\"BEARISH\", \"MILD_BEARISH\"):\n"
        "                    if vwap_sig not in (\"BULLISH\", \"BULLISH_EXTENDED\"):\n"
        "                        return \"BEAR_CALL_SPREAD\", \"bearish+fair_vrp+range_half_size\"\n"
        "                    return \"NO_TRADE\", \"fair_vrp_vwap_contradicts_direction\"\n"
        "                if dirn == \"NEUTRAL\" and straddle_allowed:\n"
        "                    return \"IRON_CONDOR\", \"neutral+fair_vrp+range_quarter_size\"\n"
        "                return \"NO_TRADE\", \"fair_vrp_neutral_direction_insufficient_edge\"\n"
        "            return \"NO_TRADE\", \"fair_vrp_trending_no_trade\""
    )
    assert old in src, "patch_strategy_engine fair_neutral not found"
    src = src.replace(old, new, 1)

    old = (
        "TARGET_PCT_BY_DAY = {\"MONDAY\": 0.25, \"TUESDAY\": 0.20, \"WEDNESDAY\": 0.25,\n"
        "                      \"THURSDAY\": 0.25, \"FRIDAY\": 0.25}"
    )
    new = (
        "TARGET_PCT_BY_DAY = {\"MONDAY\": 0.50, \"TUESDAY\": 0.50, \"WEDNESDAY\": 0.55,\n"
        "                      \"THURSDAY\": 0.55, \"FRIDAY\": 0.50}"
    )
    assert old in src, "patch_strategy_engine target_pct not found"
    src = src.replace(old, new, 1)

    write(filename, src)
    print("patched " + filename)


def patch_market_data_engine_stop_multiplier():
    filename = "market_data_engine.py"
    src = read(filename)

    old = (
        "        dow_stop = {\"MONDAY\": 2.2, \"TUESDAY\": 1.4, \"WEDNESDAY\": 1.8, \"THURSDAY\": 1.9, \"FRIDAY\": 1.6}"
    )
    new = (
        "        dow_stop = {\"MONDAY\": 1.5, \"TUESDAY\": 1.2, \"WEDNESDAY\": 1.4, \"THURSDAY\": 1.5, \"FRIDAY\": 1.3}"
    )
    assert old in src, "patch_market_data_engine stop_multiplier not found"
    src = src.replace(old, new, 1)

    write(filename, src)
    print("patched market_data_engine.py stop_multiplier")


def patch_event_json_loader():
    filename = "nifty_algo_core.py"
    src = read(filename)

    old = (
        "def load_high_impact_events(path: Path = DEFAULT_EVENTS_FILE) -> dict:\n"
        "    \"\"\"\n"
        "    Loads the high-impact-events calendar from an external JSON file\n"
        "    (date_str -> event_name). Intentionally NOT hardcoded with any dates —\n"
        "    populate this file yourself with verified event dates.\n"
        "    Expected format: {\"2026-02-01\": \"Union Budget\", \"2026-02-06\": \"RBI MPC\", ...}\n"
        "    \"\"\"\n"
        "    if not path.exists():\n"
        "        path.write_text(\"{}\\n\", encoding=\"utf-8\")\n"
        "        print(f\"[SETUP] {path} did not exist — created empty calendar. \"\n"
        "              f\"Populate it with verified event dates (FOMC, RBI MPC, Budget, \"\n"
        "              f\"CPI/WPI, expiry dates, etc.) for event-day handling to work.\")\n"
        "        return {}\n"
        "    try:\n"
        "        return json.loads(path.read_text(encoding=\"utf-8\"))\n"
        "    except json.JSONDecodeError as e:\n"
        "        print(f\"[WARNING] {path} is not valid JSON ({e}); treating as empty calendar.\")\n"
        "        return {}"
    )
    new = (
        "def load_high_impact_events(path: Path = DEFAULT_EVENTS_FILE) -> dict:\n"
        "    if not path.exists():\n"
        "        path.write_text(\"{}\\n\", encoding=\"utf-8\")\n"
        "        print(f\"[SETUP] {path} did not exist — created empty calendar. \"\n"
        "              f\"Populate it with verified event dates (FOMC, RBI MPC, Budget, \"\n"
        "              f\"CPI/WPI, expiry dates, etc.) for event-day handling to work.\")\n"
        "        return {}\n"
        "    try:\n"
        "        raw = json.loads(path.read_text(encoding=\"utf-8\"))\n"
        "    except json.JSONDecodeError as e:\n"
        "        print(f\"[WARNING] {path} is not valid JSON ({e}); treating as empty calendar.\")\n"
        "        return {}\n"
        "    if not raw:\n"
        "        return {}\n"
        "    first_val = next(iter(raw.values()), None)\n"
        "    if isinstance(first_val, dict) and \"dates\" in first_val:\n"
        "        flat = {}\n"
        "        for category, payload in raw.items():\n"
        "            if isinstance(payload, dict):\n"
        "                dates_list = payload.get(\"dates\", [])\n"
        "                desc = payload.get(\"description\", category)\n"
        "                for d in dates_list:\n"
        "                    if isinstance(d, str) and len(d) == 10:\n"
        "                        flat[d] = desc\n"
        "        return flat\n"
        "    if isinstance(first_val, list):\n"
        "        flat = {}\n"
        "        for category, dates_list in raw.items():\n"
        "            if isinstance(dates_list, list):\n"
        "                for d in dates_list:\n"
        "                    if isinstance(d, str) and len(d) == 10:\n"
        "                        flat[d] = category\n"
        "        return flat\n"
        "    return raw"
    )
    assert old in src, "patch_event_json_loader not found"
    src = src.replace(old, new, 1)

    write(filename, src)
    print("patched " + filename + " event loader")


def patch_min_credits():
    filename = "strategy_engine.py"
    src = read(filename)

    old = (
        "MIN_CREDITS = {\n"
        "    \"IRON_BUTTERFLY\": 25, \"IRON_CONDOR\": 15,\n"
        "    \"BULL_PUT_SPREAD\": 12, \"BEAR_CALL_SPREAD\": 10, \"POST_EVENT_STRADDLE\": 30,\n"
        "}"
    )
    new = (
        "MIN_CREDITS = {\n"
        "    \"IRON_BUTTERFLY\": 15, \"IRON_CONDOR\": 8,\n"
        "    \"BULL_PUT_SPREAD\": 7, \"BEAR_CALL_SPREAD\": 6, \"POST_EVENT_STRADDLE\": 20,\n"
        "}"
    )
    assert old in src, "patch_min_credits not found"
    src = src.replace(old, new, 1)

    old = (
        "MIN_CREDITS_TUESDAY = {\n"
        "    \"IRON_BUTTERFLY\": 30, \"IRON_CONDOR\": 20, \"BULL_PUT_SPREAD\": 15, \"BEAR_CALL_SPREAD\": 12,\n"
        "}"
    )
    new = (
        "MIN_CREDITS_TUESDAY = {\n"
        "    \"IRON_BUTTERFLY\": 18, \"IRON_CONDOR\": 10, \"BULL_PUT_SPREAD\": 8, \"BEAR_CALL_SPREAD\": 7,\n"
        "}"
    )
    assert old in src, "patch_min_credits_tuesday not found"
    src = src.replace(old, new, 1)

    write(filename, src)
    print("patched strategy_engine.py min_credits")


def patch_fair_neutral_size():
    filename = "strategy_engine.py"
    src = read(filename)

    old = (
        "        if \"half_size\" in reason or \"uncertain\" in reason or \"fair_vrp\" in reason:\n"
        "            size_mult *= 0.50\n"
        "            self.logger.info(f\"Half-size applied due to: {reason}\")"
    )
    new = (
        "        if \"quarter_size\" in reason:\n"
        "            size_mult *= 0.25\n"
        "            self.logger.info(f\"Quarter-size applied due to: {reason}\")\n"
        "        elif \"half_size\" in reason or \"uncertain\" in reason or \"fair_vrp\" in reason:\n"
        "            size_mult *= 0.50\n"
        "            self.logger.info(f\"Half-size applied due to: {reason}\")"
    )
    assert old in src, "patch_fair_neutral_size not found"
    src = src.replace(old, new, 1)

    write(filename, src)
    print("patched strategy_engine.py fair_neutral_size")


def verify_patches():
    errors = []

    src = read("nifty_algo_core.py")
    if "MAX_RISK_PER_TRADE_PCT=0.010" not in src:
        errors.append("nifty_algo_core: MAX_RISK_PER_TRADE_PCT not updated to 0.010")
    if "first_val = next(iter(raw.values()), None)" not in src:
        errors.append("nifty_algo_core: event JSON loader not patched")

    src = read("market_data_engine.py")
    if "equal_pv / total_bars" not in src:
        errors.append("market_data_engine: price-anchored VWAP fallback not added")
    if "compute_intraday_parkinson_rv" not in src:
        errors.append("market_data_engine: intraday parkinson rv not added")
    if "intraday_rv_selling_veto" not in src:
        errors.append("market_data_engine: intraday_rv_selling_veto not added to signals")
    if "vrp > 5.0" not in src:
        errors.append("market_data_engine: VRP bands not updated")
    if "dow_stop = {\"MONDAY\": 1.5" not in src:
        errors.append("market_data_engine: stop multipliers not reduced")

    src = read("strategy_engine.py")
    if "intraday_rv_exceeds_atm_iv_no_premium_selling" not in src:
        errors.append("strategy_engine: intraday_rv_veto gate not added")
    if "neutral+fair_vrp+range_quarter_size" not in src:
        errors.append("strategy_engine: FAIR+NEUTRAL condor branch not added")
    if '"MONDAY": 0.50' not in src:
        errors.append("strategy_engine: TARGET_PCT not updated to 50-55%")
    if '"IRON_BUTTERFLY\": 15' not in src:
        errors.append("strategy_engine: MIN_CREDITS not updated")
    if "quarter_size" not in src:
        errors.append("strategy_engine: quarter_size multiplier not added")

    if errors:
        print("\nVERIFICATION FAILED:")
        for e in errors:
            print("  ERROR: " + e)
        sys.exit(1)
    else:
        print("\nAll patches verified successfully.")


if __name__ == "__main__":
    print("Applying patch4...")
    patch_nifty_algo_core()
    patch_market_data_engine()
    patch_strategy_engine()
    patch_market_data_engine_stop_multiplier()
    patch_event_json_loader()
    patch_min_credits()
    patch_fair_neutral_size()
    verify_patches()
    print("\nDone. All patch4 changes applied.")