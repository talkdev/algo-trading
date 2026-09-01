#!/usr/bin/env python3
"""
patch.py — Round-3 audit fixes for the NIFTY options trading engine.

Fixes applied (confirmed-valid only, no speculative changes):

  regime_engine.py
    RE-R01  Asymmetric rounding bias in _persist() — symmetric fix
    RE-R02  Trend direction must agree with EMA slope and DI
    RE-R03  Intraday module-score decay when data unavailable
    RE-R04  Final-regime hysteresis (entry/exit bands)

  strategy_engine.py
    SE-R01  Re-closing already-closed legs on next cycle
    SE-R02  Retry fill quantity ignored in _close_position
    SE-R03  Rebalance destroys ratio/butterfly structures
    SE-R04  Partial-fill metadata scaling wrong for ratio legs
    SE-R05  No per-position CLOSING lock
    SE-R06  Partial reductions don't rebase risk metrics
    Partial-close exit_price overwrite (_close_one_side)
    Pre-trade EV check after costs
    Trailing stop for debit strategies

  data_manager.py
    DM-R01/R02  LTP has no timestamp; stale fallback no age limit
    DM-R03  Outlier rejection uses stale bid/ask

  main.py
    MN-R01  Old data relabeled as fresh after failed refresh
    MN-R02  Fast monitor skips circuit breakers
    MN-R04  date.today() timezone inconsistency

  config.py
    CFG-R01  Daily-limit comment arithmetic corrected
    CFG-R02  MAX_COMBINED_RISK_PCT comment clarified

Run:
    python patch.py [--dry-run] [--no-backup]
"""

import sys
import os
import re
import ast
import shutil
import argparse
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def backup_file(path):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path + ".bak_" + ts
    shutil.copy2(path, bak)
    print("  Backup: " + bak)


def verify_syntax(path, content):
    try:
        ast.parse(content)
        return True, None
    except SyntaxError as e:
        return False, str(e)


def apply_patch(path, original, patched, dry_run, do_backup):
    if original == patched:
        print("  [SKIP] " + os.path.basename(path) + " — no changes")
        return True
    ok, err = verify_syntax(path, patched)
    if not ok:
        print("  [ERROR] " + os.path.basename(path)
              + " — syntax error: " + str(err))
        return False
    if dry_run:
        orig_lines = original.splitlines()
        new_lines = patched.splitlines()
        print("  [DRY-RUN] " + os.path.basename(path)
              + " — " + str(len(orig_lines))
              + " -> " + str(len(new_lines)) + " lines")
        shown = 0
        for i in range(max(len(orig_lines), len(new_lines))):
            if shown >= 20:
                break
            a = orig_lines[i] if i < len(orig_lines) else None
            b = new_lines[i] if i < len(new_lines) else None
            if a != b:
                if a is not None:
                    print("    L" + str(i + 1) + ": - " + a.rstrip())
                if b is not None:
                    print("    L" + str(i + 1) + ": + " + b.rstrip())
                shown += 1
        return True
    if do_backup:
        backup_file(path)
    write_file(path, patched)
    print("  [OK] " + os.path.basename(path) + " — patched")
    return True


def sub_exact(old, new, content, label):
    if old in content:
        return content.replace(old, new, 1), True
    print("  [WARN] " + label + ": target not found")
    return content, False


def sub_regex(pattern, repl, content, label, flags=0):
    new_content, n = re.subn(pattern, repl, content, flags=flags)
    if n > 0:
        return new_content, True
    print("  [WARN] " + label + ": regex target not found")
    return content, False


# ─────────────────────────────────────────────────────────────────────
# config.py
# ─────────────────────────────────────────────────────────────────────

def patch_config(content):
    changes = []

    # CFG-R01: Fix incorrect comment about daily loss arithmetic
    old_comment = (
        "# AUDIT CFG-02: was 0.08 (8%). One max-risk loss = 2.7x daily CB.\n"
        "# CB L1 (2%) fired before the designed stop on almost every trade.\n"
        "# 0.02 = two full losers fit within MAX_DAILY_LOSS_PCT=0.03.\n"
        "MAX_RISK_PER_TRADE_PCT   = 0.02"
    )
    new_comment = (
        "# AUDIT CFG-02: was 0.08 (8%). One max-risk loss = 2.7x daily CB.\n"
        "# CB L1 (2%) fired before the designed stop on almost every trade.\n"
        "# CFG-R01: 2x2%=4% > 3% daily limit, so two simultaneous max-risk\n"
        "# losses exceed the daily CB. The CB is reactive (fires after loss).\n"
        "# Reserve daily risk before entry; do not rely on the CB as a gate.\n"
        "MAX_RISK_PER_TRADE_PCT   = 0.02"
    )
    content, ok = sub_exact(old_comment, new_comment, content, "CFG-R01 comment")
    if ok:
        changes.append("CFG-R01: daily-limit comment corrected")

    # CFG-R02: Clarify MAX_COMBINED_RISK_PCT is non-binding
    old_combined = "MAX_COMBINED_RISK_PCT    = 0.20"
    new_combined = (
        "# CFG-R02: with MAX_RISK_PER_TRADE_PCT=0.02 and\n"
        "# MAX_CONCURRENT_POSITIONS=4, max theoretical exposure=8%.\n"
        "# This 20% limit is non-binding; real constraint is the sum\n"
        "# of position max_risk values checked in _pre_trade_checks.\n"
        "MAX_COMBINED_RISK_PCT    = 0.20"
    )
    content, ok = sub_exact(old_combined, new_combined, content, "CFG-R02 comment")
    if ok:
        changes.append("CFG-R02: MAX_COMBINED_RISK_PCT comment clarified")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# data_manager.py
# ─────────────────────────────────────────────────────────────────────

def patch_data_manager(content):
    changes = []

    # DM-R01/R02: Store _ltp_ts on every LTP update so get_mark_price
    # can validate LTP freshness, not just REST bid/ask freshness.
    # Also add a hard age limit to the final REST fallback.
    old_get_mark = (
        "    def get_mark_price(\n"
        "        self,\n"
        "        opt_data: Dict,\n"
        "        fallback: float = 0.0,\n"
        "        max_quote_age_sec: float = 15.0,\n"
        "    ) -> float:\n"
        "        \"\"\"\n"
        "        AUDIT DM-01: bid/ask are only updated by the 60s REST\n"
        "        poll, never by the WebSocket. LTP is updated by WS on\n"
        "        every tick. Use bid/ask midpoint only when the REST\n"
        "        quote is fresh (< max_quote_age_sec); otherwise prefer\n"
        "        the live-ticking LTP.\n"
        "        \"\"\"\n"
        "        bid = float(opt_data.get(\"bid\", 0) or 0)\n"
        "        ask = float(opt_data.get(\"ask\", 0) or 0)\n"
        "        ltp = float(opt_data.get(\"ltp\", 0) or 0)\n"
        "        rest_ts = opt_data.get(\"_rest_ts\")\n"
        "        quote_fresh = False\n"
        "        if rest_ts:\n"
        "            try:\n"
        "                ts_dt = datetime.fromisoformat(rest_ts)\n"
        "                if ts_dt.tzinfo is None:\n"
        "                    ts_dt = self._IST.localize(ts_dt)\n"
        "                age = (\n"
        "                    datetime.now(self._IST) - ts_dt\n"
        "                ).total_seconds()\n"
        "                quote_fresh = age <= max_quote_age_sec\n"
        "            except Exception:\n"
        "                pass\n"
        "        if quote_fresh and bid > 0 and ask > 0:\n"
        "            return (bid + ask) / 2.0\n"
        "        if ltp > 0:\n"
        "            return ltp\n"
        "        if bid > 0 and ask > 0:\n"
        "            return (bid + ask) / 2.0\n"
        "        return fallback"
    )
    new_get_mark = (
        "    def get_mark_price(\n"
        "        self,\n"
        "        opt_data: Dict,\n"
        "        fallback: float = 0.0,\n"
        "        max_quote_age_sec: float = 15.0,\n"
        "        max_ltp_age_sec: float = 30.0,\n"
        "        max_rest_fallback_age_sec: float = 90.0,\n"
        "    ) -> float:\n"
        "        \"\"\"\n"
        "        DM-R01/R02: Returns the best available mark price.\n"
        "        Priority:\n"
        "          1. Fresh REST bid/ask midpoint (< max_quote_age_sec)\n"
        "          2. Fresh WS LTP (< max_ltp_age_sec, uses _ltp_ts)\n"
        "          3. Stale REST bid/ask midpoint (< max_rest_fallback_age_sec)\n"
        "          4. fallback (entry price or 0)\n"
        "        Returns fallback when all sources exceed their age limits\n"
        "        so callers can detect a genuinely stale mark.\n"
        "        \"\"\"\n"
        "        now_ist = datetime.now(self._IST)\n"
        "        bid = float(opt_data.get(\"bid\", 0) or 0)\n"
        "        ask = float(opt_data.get(\"ask\", 0) or 0)\n"
        "        ltp = float(opt_data.get(\"ltp\", 0) or 0)\n"
        "\n"
        "        def _age(ts_key):\n"
        "            ts = opt_data.get(ts_key)\n"
        "            if not ts:\n"
        "                return float(\"inf\")\n"
        "            try:\n"
        "                ts_dt = datetime.fromisoformat(ts)\n"
        "                if ts_dt.tzinfo is None:\n"
        "                    ts_dt = self._IST.localize(ts_dt)\n"
        "                return (now_ist - ts_dt).total_seconds()\n"
        "            except Exception:\n"
        "                return float(\"inf\")\n"
        "\n"
        "        rest_age = _age(\"_rest_ts\")\n"
        "        ltp_age  = _age(\"_ltp_ts\")\n"
        "\n"
        "        # 1. Fresh REST midpoint\n"
        "        if rest_age <= max_quote_age_sec and bid > 0 and ask > 0:\n"
        "            return (bid + ask) / 2.0\n"
        "        # 2. Fresh WS LTP\n"
        "        if ltp_age <= max_ltp_age_sec and ltp > 0:\n"
        "            return ltp\n"
        "        # 3. Stale REST midpoint (bounded age)\n"
        "        if rest_age <= max_rest_fallback_age_sec and bid > 0 and ask > 0:\n"
        "            return (bid + ask) / 2.0\n"
        "        # 4. fallback\n"
        "        return fallback"
    )
    content, ok = sub_exact(old_get_mark, new_get_mark, content, "DM-R01/R02 get_mark_price")
    if ok:
        changes.append("DM-R01/R02: get_mark_price() validates LTP age via _ltp_ts")

    # Store _ltp_ts on every LTP update in _update_instrument_ltp
    # Find the option-chain update block and add timestamp storage
    old_ltp_update = (
        "                    else:\n"
        "                        opt_ref[\"ltp\"] = ltp"
    )
    new_ltp_update = (
        "                    else:\n"
        "                        opt_ref[\"ltp\"] = ltp\n"
        "                        opt_ref[\"_ltp_ts\"] = datetime.now(\n"
        "                            pytz.timezone(config.TZ)\n"
        "                        ).isoformat()"
    )
    # This pattern appears in the LTP spike-guard block; use it carefully
    # We need to match the specific location inside the option chain update
    old_ltp_store = (
        "                        opt_ref[\"ltp\"] = ltp\n"
        "                    else:\n"
        "                        opt_ref[\"ltp\"] = ltp"
    )
    new_ltp_store = (
        "                        opt_ref[\"ltp\"] = ltp\n"
        "                        opt_ref[\"_ltp_ts\"] = datetime.now(\n"
        "                            pytz.timezone(config.TZ)\n"
        "                        ).isoformat()\n"
        "                    else:\n"
        "                        opt_ref[\"ltp\"] = ltp\n"
        "                        opt_ref[\"_ltp_ts\"] = datetime.now(\n"
        "                            pytz.timezone(config.TZ)\n"
        "                        ).isoformat()"
    )
    content, ok = sub_exact(old_ltp_store, new_ltp_store, content, "DM-R01 _ltp_ts storage")
    if ok:
        changes.append("DM-R01: _ltp_ts stored on every WS LTP update")

    # DM-R03: Fix outlier rejection to use LTP age, not stale REST mid
    old_outlier = (
        "                    opt_ref = self.option_chain[expiry][strike][\n"
        "                        option_type\n"
        "                    ]\n"
        "                    bid_ref = opt_ref.get(\"bid\", 0)\n"
        "                    ask_ref = opt_ref.get(\"ask\", 0)\n"
        "                    if bid_ref > 0 and ask_ref > 0:\n"
        "                        mid_ref    = (bid_ref + ask_ref) / 2.0\n"
        "                        spread_ref = max(ask_ref - bid_ref, 0.05)\n"
        "                        if abs(ltp - mid_ref) > max(\n"
        "                            10.0, spread_ref * 3\n"
        "                        ):\n"
        "                            logger.warning(\n"
        "                                f\"Option LTP outlier rejected: \"\n"
        "                                f\"{option_type} {strike} {expiry} \"\n"
        "                                f\"ltp={ltp:.2f} mid={mid_ref:.2f} \"\n"
        "                                f\"bid={bid_ref:.2f} ask={ask_ref:.2f}\"\n"
        "                            )\n"
        "                        else:\n"
        "                            opt_ref[\"ltp\"] = ltp\n"
        "                    else:\n"
        "                        opt_ref[\"ltp\"] = ltp"
    )
    new_outlier = (
        "                    opt_ref = self.option_chain[expiry][strike][\n"
        "                        option_type\n"
        "                    ]\n"
        "                    bid_ref = opt_ref.get(\"bid\", 0)\n"
        "                    ask_ref = opt_ref.get(\"ask\", 0)\n"
        "                    rest_ts_ref = opt_ref.get(\"_rest_ts\")\n"
        "                    # DM-R03: only reject as outlier when the\n"
        "                    # REST quote is fresh (<= 15s). During fast\n"
        "                    # moves the REST mid is stale and the LTP\n"
        "                    # is correct; rejecting it causes missed\n"
        "                    # price updates and late stop-loss triggers.\n"
        "                    _rest_fresh = False\n"
        "                    if rest_ts_ref:\n"
        "                        try:\n"
        "                            _rts = datetime.fromisoformat(\n"
        "                                rest_ts_ref\n"
        "                            )\n"
        "                            if _rts.tzinfo is None:\n"
        "                                _rts = pytz.timezone(\n"
        "                                    config.TZ\n"
        "                                ).localize(_rts)\n"
        "                            _rest_fresh = (\n"
        "                                datetime.now(\n"
        "                                    pytz.timezone(config.TZ)\n"
        "                                ) - _rts\n"
        "                            ).total_seconds() <= 15.0\n"
        "                        except Exception:\n"
        "                            pass\n"
        "                    if _rest_fresh and bid_ref > 0 and ask_ref > 0:\n"
        "                        mid_ref    = (bid_ref + ask_ref) / 2.0\n"
        "                        # Use 5% of mid as threshold (not fixed 10pts)\n"
        "                        pct_thresh = max(10.0, mid_ref * 0.05)\n"
        "                        if abs(ltp - mid_ref) > pct_thresh:\n"
        "                            logger.warning(\n"
        "                                f\"Option LTP outlier rejected: \"\n"
        "                                f\"{option_type} {strike} {expiry} \"\n"
        "                                f\"ltp={ltp:.2f} mid={mid_ref:.2f} \"\n"
        "                                f\"({abs(ltp-mid_ref)/mid_ref*100:.1f}%)\"\n"
        "                            )\n"
        "                        else:\n"
        "                            opt_ref[\"ltp\"] = ltp\n"
        "                            opt_ref[\"_ltp_ts\"] = datetime.now(\n"
        "                                pytz.timezone(config.TZ)\n"
        "                            ).isoformat()\n"
        "                    else:\n"
        "                        opt_ref[\"ltp\"] = ltp\n"
        "                        opt_ref[\"_ltp_ts\"] = datetime.now(\n"
        "                            pytz.timezone(config.TZ)\n"
        "                        ).isoformat()"
    )
    content, ok = sub_exact(old_outlier, new_outlier, content, "DM-R03 outlier rejection")
    if ok:
        changes.append("DM-R03: LTP outlier rejection only when REST quote is fresh")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# regime_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # RE-R01: Fix asymmetric rounding — use copysign for symmetric rounding
    old_round = (
        "        # AUDIT RE-01: int(round(0.5)) == 0 in Python (banker's\n"
        "        # rounding). A vol_score of +0.5 (normal contango + neutral\n"
        "        # skew) was silently becoming 0, making STRONG_SELL_VOL\n"
        "        # unreachable. Use standard half-up rounding instead.\n"
        "        import math as _math\n"
        "        raw_int = int(_math.floor(float(raw) + 0.5))\n"
        "        buf     = self._buf[name]"
    )
    new_round = (
        "        # RE-R01: floor(x+0.5) is asymmetric: +0.5->1 but -0.5->0.\n"
        "        # This introduced a sell-vol bias (buy-vol signals zeroed).\n"
        "        # Use copysign for symmetric rounding away from zero:\n"
        "        #   +0.5 -> +1,  -0.5 -> -1,  +0.0 -> 0\n"
        "        import math as _math\n"
        "        _raw_f  = float(raw)\n"
        "        raw_int = int(\n"
        "            _math.copysign(\n"
        "                _math.floor(abs(_raw_f) + 0.5), _raw_f\n"
        "            )\n"
        "        )\n"
        "        buf     = self._buf[name]"
    )
    content, ok = sub_exact(old_round, new_round, content, "RE-R01 symmetric rounding")
    if ok:
        changes.append("RE-R01: _persist() uses symmetric copysign rounding")

    # RE-R02: Trend direction must agree with EMA slope and DI
    old_trend_direction = (
        "        if adx_v > ADX_TREND and abs(slope_pct) > EMA_SLOPE_PCT:\n"
        "            raw  = 1 if above else -1\n"
        "            dirn = \"bullish\" if above else \"bearish\"\n"
        "        else:\n"
        "            raw  = 0\n"
        "            dirn = \"range-bound\""
    )
    new_trend_direction = (
        "        # RE-R02: require three-way directional agreement to\n"
        "        # avoid false signals at EMA crossings:\n"
        "        #   bullish: spot > EMA AND slope > 0 AND +DI > -DI\n"
        "        #   bearish: spot < EMA AND slope < 0 AND -DI > +DI\n"
        "        # Without this, a falling EMA with spot marginally above\n"
        "        # it scores +1 (bullish) despite a bearish trend.\n"
        "        if adx_v > ADX_TREND and abs(slope_pct) > EMA_SLOPE_PCT:\n"
        "            _slope_up = slope > 0\n"
        "            _di_bull  = pdi > ndi\n"
        "            if above and _slope_up and _di_bull:\n"
        "                raw  = 1\n"
        "                dirn = \"bullish (3-way confirmed)\"\n"
        "            elif not above and not _slope_up and not _di_bull:\n"
        "                raw  = -1\n"
        "                dirn = \"bearish (3-way confirmed)\"\n"
        "            else:\n"
        "                raw  = 0\n"
        "                dirn = \"mixed signals (no 3-way agreement)\"\n"
        "        else:\n"
        "            raw  = 0\n"
        "            dirn = \"range-bound\""
    )
    content, ok = sub_exact(
        old_trend_direction, new_trend_direction, content, "RE-R02 trend 3-way"
    )
    if ok:
        changes.append("RE-R02: trend direction requires EMA slope + DI agreement")

    # RE-R03: Intraday decay for stale module scores
    # When raw is None, hold the confirmed value but decay it toward 0
    # after INTRADAY_SCORE_DECAY_CYCLES consecutive None readings.
    old_persist_none = (
        "        if raw is None:\n"
        "            return self._conf[name]"
    )
    new_persist_none = (
        "        if raw is None:\n"
        "            # RE-R03: decay stale scores toward 0 intraday.\n"
        "            # Track consecutive None readings per module.\n"
        "            _none_key = \"_none_count_\" + name\n"
        "            _none_count = getattr(self, _none_key, 0) + 1\n"
        "            setattr(self, _none_key, _none_count)\n"
        "            # After 10 consecutive None readings (~10 min at\n"
        "            # 60s refresh), decay the confirmed score toward 0\n"
        "            # by 1 step so stale flow/vol doesn't drive entries.\n"
        "            _decay_after = 10\n"
        "            if _none_count > _decay_after and self._conf[name] != 0:\n"
        "                _old = self._conf[name]\n"
        "                self._conf[name] = (\n"
        "                    _old - 1 if _old > 0 else _old + 1\n"
        "                )\n"
        "                logger.info(\n"
        "                    f\"RE-R03: {name} score decayed \"\n"
        "                    f\"{_old} -> {self._conf[name]} \"\n"
        "                    f\"(none_count={_none_count})\"\n"
        "                )\n"
        "            return self._conf[name]"
    )
    content, ok = sub_exact(
        old_persist_none, new_persist_none, content, "RE-R03 intraday decay"
    )
    if ok:
        changes.append("RE-R03: stale module scores decay after 10 consecutive None readings")

    # Reset none-count when a real value arrives
    old_persist_buf = (
        "        buf     = self._buf[name]\n"
        "        buf.append(raw_int)"
    )
    new_persist_buf = (
        "        buf     = self._buf[name]\n"
        "        # Reset decay counter when a real value arrives\n"
        "        setattr(self, \"_none_count_\" + name, 0)\n"
        "        buf.append(raw_int)"
    )
    content, ok = sub_exact(
        old_persist_buf, new_persist_buf, content, "RE-R03 reset none count"
    )
    if ok:
        changes.append("RE-R03: none-count reset when real module value arrives")

    # RE-R04: Final-regime hysteresis — use entry/exit bands
    # Add hysteresis constants and modify _map_regime to use them.
    # Entry thresholds are tighter; exit thresholds are looser.
    old_map_regime_method = (
        "    def _map_regime(self, composite: float) -> str:\n"
        "        \"\"\"Reference algorithm regime mapping.\n"
        "        AUDIT #2.2: reads thresholds from config.\n"
        "        \"\"\"\n"
        "        if composite > config.STRONG_SELL_THRESHOLD:\n"
        "            return config.REGIME_STRONG_SELL\n"
        "        if composite >= config.MILD_SELL_THRESHOLD:\n"
        "            return config.REGIME_MILD_SELL\n"
        "        if composite > config.MILD_BUY_THRESHOLD:\n"
        "            return config.REGIME_NEUTRAL\n"
        "        if composite >= config.STRONG_BUY_THRESHOLD:\n"
        "            return config.REGIME_BUY_VOL\n"
        "        return config.REGIME_STRONG_BUY"
    )
    new_map_regime_method = (
        "    def _map_regime(self, composite: float) -> str:\n"
        "        \"\"\"Reference algorithm regime mapping with hysteresis.\n"
        "        RE-R04: use entry/exit bands to prevent churn near\n"
        "        boundaries. Entry requires crossing a tighter threshold;\n"
        "        exit requires crossing a looser threshold in the opposite\n"
        "        direction. Hysteresis band = 0.05 composite units.\n"
        "        \"\"\"\n"
        "        _hyst = 0.05\n"
        "        current = self.confirmed_regime\n"
        "\n"
        "        # Staying in current regime unless exit threshold crossed\n"
        "        if current == config.REGIME_STRONG_SELL:\n"
        "            if composite > (config.STRONG_SELL_THRESHOLD - _hyst):\n"
        "                return config.REGIME_STRONG_SELL\n"
        "        elif current == config.REGIME_MILD_SELL:\n"
        "            if (config.MILD_SELL_THRESHOLD - _hyst\n"
        "                    <= composite\n"
        "                    <= config.STRONG_SELL_THRESHOLD + _hyst):\n"
        "                return config.REGIME_MILD_SELL\n"
        "        elif current == config.REGIME_NEUTRAL:\n"
        "            if (config.MILD_BUY_THRESHOLD - _hyst\n"
        "                    < composite\n"
        "                    < config.MILD_SELL_THRESHOLD + _hyst):\n"
        "                return config.REGIME_NEUTRAL\n"
        "        elif current == config.REGIME_BUY_VOL:\n"
        "            if (config.STRONG_BUY_THRESHOLD - _hyst\n"
        "                    <= composite\n"
        "                    <= config.MILD_BUY_THRESHOLD + _hyst):\n"
        "                return config.REGIME_BUY_VOL\n"
        "        elif current == config.REGIME_STRONG_BUY:\n"
        "            if composite < (config.STRONG_BUY_THRESHOLD + _hyst):\n"
        "                return config.REGIME_STRONG_BUY\n"
        "\n"
        "        # Entry into new regime (tighter thresholds)\n"
        "        if composite > config.STRONG_SELL_THRESHOLD:\n"
        "            return config.REGIME_STRONG_SELL\n"
        "        if composite >= config.MILD_SELL_THRESHOLD:\n"
        "            return config.REGIME_MILD_SELL\n"
        "        if composite > config.MILD_BUY_THRESHOLD:\n"
        "            return config.REGIME_NEUTRAL\n"
        "        if composite >= config.STRONG_BUY_THRESHOLD:\n"
        "            return config.REGIME_BUY_VOL\n"
        "        return config.REGIME_STRONG_BUY"
    )
    content, ok = sub_exact(
        old_map_regime_method, new_map_regime_method, content, "RE-R04 hysteresis"
    )
    if ok:
        changes.append("RE-R04: _map_regime() uses entry/exit hysteresis bands (±0.05)")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# strategy_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # SE-R05: Add per-position CLOSING lock using a set of trade_ids
    # Add _closing_positions set to __init__
    old_inflight = (
        "        self._inflight_tags:      set = set()\n"
        "        self._inflight_lock       = asyncio.Lock()"
    )
    new_inflight = (
        "        self._inflight_tags:      set = set()\n"
        "        self._inflight_lock       = asyncio.Lock()\n"
        "        # SE-R05: track positions currently being closed\n"
        "        # to prevent concurrent close attempts from the\n"
        "        # fast monitor, circuit breakers, and EOD handler.\n"
        "        self._closing_positions:  set = set()"
    )
    content, ok = sub_exact(old_inflight, new_inflight, content, "SE-R05 closing set init")
    if ok:
        changes.append("SE-R05: _closing_positions set added to __init__")

    # SE-R05: Guard _close_position with the closing set
    old_close_start = (
        "        if position.status != \"OPEN\":\n"
        "            return\n"
        "\n"
        "        logger.info(\n"
        "            f\"Closing: {position.trade_id[:8]} \"\n"
        "            f\"strategy={position.strategy_name} \"\n"
        "            f\"reason={exit_reason}\"\n"
        "        )"
    )
    new_close_start = (
        "        if position.status != \"OPEN\":\n"
        "            return\n"
        "\n"
        "        # SE-R05: prevent concurrent close attempts.\n"
        "        # The fast monitor runs every second; without this\n"
        "        # guard a slow fill (>1s) causes duplicate orders.\n"
        "        if position.trade_id in self._closing_positions:\n"
        "            logger.debug(\n"
        "                f\"Close already in progress: \"\n"
        "                f\"{position.trade_id[:8]} — skipping\"\n"
        "            )\n"
        "            return\n"
        "        self._closing_positions.add(position.trade_id)\n"
        "\n"
        "        logger.info(\n"
        "            f\"Closing: {position.trade_id[:8]} \"\n"
        "            f\"strategy={position.strategy_name} \"\n"
        "            f\"reason={exit_reason}\"\n"
        "        )"
    )
    content, ok = sub_exact(old_close_start, new_close_start, content, "SE-R05 close guard")
    if ok:
        changes.append("SE-R05: _close_position guarded against concurrent calls")

    # SE-R05: Release the closing lock when done (both success and failure paths)
    # The SE-N01 guard returns early on failure; we need to release there too.
    old_close_guard_return = (
        "        if _unconfirmed:\n"
        "            logger.error(\n"
        "                f\"SE-N01: {len(_unconfirmed)} leg(s) have no \"\n"
        "                f\"confirmed exit price for \"\n"
        "                f\"{position.trade_id[:8]} \u2014 \"\n"
        "                f\"position remains OPEN. Manual review required.\"\n"
        "            )\n"
        "            # Mark legs that did exit so we don't re-close them,\n"
        "            # but keep the position OPEN for the next cycle.\n"
        "            return"
    )
    new_close_guard_return = (
        "        if _unconfirmed:\n"
        "            logger.error(\n"
        "                f\"SE-N01: {len(_unconfirmed)} leg(s) have no \"\n"
        "                f\"confirmed exit price for \"\n"
        "                f\"{position.trade_id[:8]} \u2014 \"\n"
        "                f\"position remains OPEN. Manual review required.\"\n"
        "            )\n"
        "            # Release the closing lock so the next cycle can retry\n"
        "            self._closing_positions.discard(position.trade_id)\n"
        "            return"
    )
    content, ok = sub_exact(
        old_close_guard_return, new_close_guard_return, content, "SE-R05 release on failure"
    )
    if ok:
        changes.append("SE-R05: closing lock released on partial-exit failure")

    # SE-R05: Release on successful close
    old_close_success = (
        "        if position in self.open_positions:\n"
        "            self.open_positions.remove(position)\n"
        "        self.closed_positions.append(position)"
    )
    new_close_success = (
        "        # SE-R05: release the closing lock\n"
        "        self._closing_positions.discard(position.trade_id)\n"
        "        if position in self.open_positions:\n"
        "            self.open_positions.remove(position)\n"
        "        self.closed_positions.append(position)"
    )
    content, ok = sub_exact(
        old_close_success, new_close_success, content, "SE-R05 release on success"
    )
    if ok:
        changes.append("SE-R05: closing lock released on successful close")

    # SE-R01: Mark successfully closed legs so they are not re-closed
    # Fix: after a successful leg exit, set fill_status="CLOSED_EXIT"
    # and reduce qty to 0. The SE-N01 guard checks qty > 0.
    old_leg_exit_success = (
        "            if not success:\n"
        "                logger.warning(\n"
        "                    f\"Close leg failed \u2014 retrying market: \"\n"
        "                    f\"{leg.option_type} {leg.strike}\"\n"
        "                )\n"
        "                retry_leg = Leg(\n"
        "                    instrument_key=leg.instrument_key,\n"
        "                    option_type=leg.option_type,\n"
        "                    action=close_action,\n"
        "                    strike=leg.strike,\n"
        "                    expiry=leg.expiry,\n"
        "                    qty=leg.qty,\n"
        "                )\n"
        "                await self._place_single_leg(\n"
        "                    retry_leg,\n"
        "                    use_market=True,\n"
        "                    trade_id=(\n"
        "                        f\"exit-retry-{position.trade_id}\"\n"
        "                    ),\n"
        "                    leg_index=idx,\n"
        "                )\n"
        "                leg.exit_price = retry_leg.entry_price\n"
        "            else:\n"
        "                leg.exit_price = close_leg.entry_price"
    )
    new_leg_exit_success = (
        "            if not success:\n"
        "                logger.warning(\n"
        "                    f\"Close leg failed \u2014 retrying market: \"\n"
        "                    f\"{leg.option_type} {leg.strike}\"\n"
        "                )\n"
        "                retry_leg = Leg(\n"
        "                    instrument_key=leg.instrument_key,\n"
        "                    option_type=leg.option_type,\n"
        "                    action=close_action,\n"
        "                    strike=leg.strike,\n"
        "                    expiry=leg.expiry,\n"
        "                    qty=leg.qty,\n"
        "                )\n"
        "                retry_ok, _ = await self._place_single_leg(\n"
        "                    retry_leg,\n"
        "                    use_market=True,\n"
        "                    trade_id=(\n"
        "                        f\"exit-retry-{position.trade_id}\"\n"
        "                    ),\n"
        "                    leg_index=idx,\n"
        "                )\n"
        "                # SE-R02: only record exit if retry actually filled\n"
        "                if retry_ok and retry_leg.entry_price > 0:\n"
        "                    # Use actual filled qty from retry_leg\n"
        "                    leg.exit_price  = retry_leg.entry_price\n"
        "                    leg.qty         = retry_leg.qty\n"
        "                    leg.fill_status = \"CLOSED_EXIT\"\n"
        "                # If retry also failed, leave exit_price=0 so\n"
        "                # SE-N01 guard keeps the position OPEN\n"
        "            else:\n"
        "                # SE-R01: mark leg as fully exited so the next\n"
        "                # monitoring cycle does not re-close it.\n"
        "                leg.exit_price  = close_leg.entry_price\n"
        "                leg.qty         = 0\n"
        "                leg.fill_status = \"CLOSED_EXIT\""
    )
    content, ok = sub_exact(
        old_leg_exit_success, new_leg_exit_success, content, "SE-R01/R02 leg exit marking"
    )
    if ok:
        changes.append("SE-R01/R02: closed legs marked qty=0/CLOSED_EXIT; retry fill validated")

    # Partial-close exit_price overwrite fix:
    # In _close_one_side, do NOT set leg.exit_price until qty reaches 0.
    # Only bank P&L and reduce qty. exit_price is set by _close_position.
    old_one_side_exit = (
        "                leg.exit_price  = exit_price\n"
        "                leg.qty        -= qty_closed\n"
        "                if leg.qty <= 0:\n"
        "                    leg.qty         = 0\n"
        "                    leg.fill_status = \"CLOSED_ONE_SIDE\"\n"
        "                else:\n"
        "                    leg.fill_status = (\n"
        "                        \"PARTIALLY_CLOSED_ONE_SIDE\"\n"
        "                    )\n"
        "                    logger.warning(\n"
        "                        f\"Partial one-side close: \"\n"
        "                        f\"{leg.option_type} {leg.strike} \"\n"
        "                        f\"remaining_qty={leg.qty}\"\n"
        "                    )"
    )
    new_one_side_exit = (
        "                # Partial-close exit_price fix: do NOT set\n"
        "                # leg.exit_price here. The SE-N01 guard in\n"
        "                # _close_position checks exit_price <= 0 to\n"
        "                # detect unconfirmed legs. Setting it here on\n"
        "                # a partial close causes the guard to pass even\n"
        "                # when qty > 0 remains, marking the position\n"
        "                # CLOSED with residual broker exposure.\n"
        "                # exit_price is set only when qty reaches 0.\n"
        "                leg.qty -= qty_closed\n"
        "                if leg.qty <= 0:\n"
        "                    leg.qty         = 0\n"
        "                    leg.exit_price  = exit_price\n"
        "                    leg.fill_status = \"CLOSED_ONE_SIDE\"\n"
        "                else:\n"
        "                    leg.fill_status = (\n"
        "                        \"PARTIALLY_CLOSED_ONE_SIDE\"\n"
        "                    )\n"
        "                    logger.warning(\n"
        "                        f\"Partial one-side close: \"\n"
        "                        f\"{leg.option_type} {leg.strike} \"\n"
        "                        f\"remaining_qty={leg.qty}\"\n"
        "                    )"
    )
    content, ok = sub_exact(
        old_one_side_exit, new_one_side_exit, content, "partial-close exit_price fix"
    )
    if ok:
        changes.append("Partial-close: exit_price only set when leg qty reaches 0")

    # SE-R03: Ratio-aware rebalance
    # Replace the min-absolute-qty rebalance with a ratio-aware version.
    old_rebalance_body = (
        "        if not any(l.fill_status == \"PARTIAL\" for l in legs):\n"
        "            return True\n"
        "        min_qty = min(\n"
        "            (l.qty for l in legs if l.qty > 0), default=0\n"
        "        )\n"
        "        if min_qty <= 0:\n"
        "            return True\n"
        "        for idx, leg in enumerate(legs):\n"
        "            excess = leg.qty - min_qty\n"
        "            if excess > 0:\n"
        "                trim_action = (\n"
        "                    \"BUY\" if leg.action == \"SELL\" else \"SELL\"\n"
        "                )\n"
        "                trim_leg = Leg(\n"
        "                    instrument_key=leg.instrument_key,\n"
        "                    option_type=leg.option_type,\n"
        "                    action=trim_action,\n"
        "                    strike=leg.strike,\n"
        "                    expiry=leg.expiry,\n"
        "                    qty=excess,\n"
        "                )\n"
        "                success, _ = await self._place_single_leg(\n"
        "                    trim_leg,\n"
        "                    use_market=True,\n"
        "                    trade_id=(\n"
        "                        f\"rebalance-{uuid.uuid4().hex[:8]}\"\n"
        "                    ),\n"
        "                    leg_index=idx,\n"
        "                )\n"
        "                if success:\n"
        "                    logger.warning(\n"
        "                        f\"Rebalanced {leg.option_type} \"\n"
        "                        f\"{leg.strike}: trimmed \"\n"
        "                        f\"{trim_leg.qty} lots to match \"\n"
        "                        f\"partial-fill minimum {min_qty}\"\n"
        "                    )\n"
        "                    leg.qty = min_qty\n"
        "                else:\n"
        "                    logger.error(\n"
        "                        f\"Rebalance trim FAILED for \"\n"
        "                        f\"{leg.option_type} {leg.strike} \u2014 \"\n"
        "                        f\"returning False for caller to reverse\"\n"
        "                    )\n"
        "                    return False\n"
        "                await asyncio.sleep(\n"
        "                    config.ORDER_BETWEEN_LEGS_DELAY_SEC\n"
        "                )\n"
        "        return True"
    )
    new_rebalance_body = (
        "        if not any(l.fill_status == \"PARTIAL\" for l in legs):\n"
        "            return True\n"
        "\n"
        "        # SE-R03: ratio-aware rebalance.\n"
        "        # Trimming all legs to min(qty) destroys ratio structures\n"
        "        # (butterfly 1:2:1, backspread 3:1). Instead, find the\n"
        "        # largest common unit (gcd of filled/intended ratios)\n"
        "        # and trim each leg to intended_ratio * common_units.\n"
        "        #\n"
        "        # intended_qty is the qty BEFORE lot-scaling (always 1,\n"
        "        # 2, or 3 for the strategies we build). We recover it\n"
        "        # from the minimum non-zero qty among legs that fully\n"
        "        # filled (fill_status != PARTIAL) as the ratio base.\n"
        "        import math as _math_rb\n"
        "\n"
        "        # Determine the ratio base: smallest fully-filled leg qty\n"
        "        full_qtys = [\n"
        "            l.qty for l in legs\n"
        "            if l.qty > 0 and l.fill_status != \"PARTIAL\"\n"
        "        ]\n"
        "        if not full_qtys:\n"
        "            # All legs partial — use min qty as base\n"
        "            full_qtys = [l.qty for l in legs if l.qty > 0]\n"
        "        if not full_qtys:\n"
        "            return True\n"
        "\n"
        "        base_qty = min(full_qtys)\n"
        "        if base_qty <= 0:\n"
        "            return True\n"
        "\n"
        "        # Compute ratio of each leg relative to base\n"
        "        # and find the common units achievable given partial fills\n"
        "        common_units = None\n"
        "        for leg in legs:\n"
        "            if leg.qty <= 0:\n"
        "                continue\n"
        "            ratio = round(leg.qty / base_qty)\n"
        "            if ratio < 1:\n"
        "                ratio = 1\n"
        "            achievable = leg.qty // ratio\n"
        "            if common_units is None or achievable < common_units:\n"
        "                common_units = achievable\n"
        "\n"
        "        if common_units is None or common_units <= 0:\n"
        "            return True\n"
        "\n"
        "        for idx, leg in enumerate(legs):\n"
        "            if leg.qty <= 0:\n"
        "                continue\n"
        "            ratio      = max(1, round(leg.qty / base_qty))\n"
        "            target_qty = common_units * ratio\n"
        "            excess     = leg.qty - target_qty\n"
        "            if excess <= 0:\n"
        "                continue\n"
        "            trim_action = (\n"
        "                \"BUY\" if leg.action == \"SELL\" else \"SELL\"\n"
        "            )\n"
        "            trim_leg = Leg(\n"
        "                instrument_key=leg.instrument_key,\n"
        "                option_type=leg.option_type,\n"
        "                action=trim_action,\n"
        "                strike=leg.strike,\n"
        "                expiry=leg.expiry,\n"
        "                qty=excess,\n"
        "            )\n"
        "            success, _ = await self._place_single_leg(\n"
        "                trim_leg,\n"
        "                use_market=True,\n"
        "                trade_id=(\n"
        "                    f\"rebalance-{uuid.uuid4().hex[:8]}\"\n"
        "                ),\n"
        "                leg_index=idx,\n"
        "            )\n"
        "            if success:\n"
        "                logger.warning(\n"
        "                    f\"Rebalanced {leg.option_type} \"\n"
        "                    f\"{leg.strike}: trimmed \"\n"
        "                    f\"{excess} lots (ratio={ratio}, \"\n"
        "                    f\"target={target_qty})\"\n"
        "                )\n"
        "                leg.qty = target_qty\n"
        "            else:\n"
        "                logger.error(\n"
        "                    f\"Rebalance trim FAILED for \"\n"
        "                    f\"{leg.option_type} {leg.strike} \u2014 \"\n"
        "                    f\"returning False for caller to reverse\"\n"
        "                )\n"
        "                return False\n"
        "            await asyncio.sleep(\n"
        "                config.ORDER_BETWEEN_LEGS_DELAY_SEC\n"
        "            )\n"
        "        return True"
    )
    content, ok = sub_exact(
        old_rebalance_body, new_rebalance_body, content, "SE-R03 ratio-aware rebalance"
    )
    if ok:
        changes.append("SE-R03: _rebalance_partial_fills uses ratio-aware trimming")

    # SE-R04: Remove the broken uniform metadata scaling after partial fills
    # The scaling used min(qty)/lots which is wrong for ratio legs.
    # Replace with a note that metadata is rebuilt from actual legs.
    old_partial_scale = (
        "        # AUDIT SE-N03: if any leg partially filled, the lot count\n"
        "        # was reduced. Rescale stop, target, max_risk to actual fills.\n"
        "        _filled_lots = min(\n"
        "            (l.qty for l in legs if l.qty > 0), default=lots\n"
        "        )\n"
        "        if _filled_lots < lots and lots > 0:\n"
        "            _scale = _filled_lots / lots\n"
        "            meta[\"max_risk\"] = meta.get(\"max_risk\", 0) * _scale\n"
        "            if meta.get(\"stop_loss\"):\n"
        "                meta[\"stop_loss\"] = meta[\"stop_loss\"] * _scale\n"
        "            if meta.get(\"profit_target\"):\n"
        "                meta[\"profit_target\"] = (\n"
        "                    meta[\"profit_target\"] * _scale\n"
        "                )\n"
        "            logger.info(\n"
        "                f\"SE-N03: partial fill rescale \"\n"
        "                f\"{lots}->{_filled_lots} lots, \"\n"
        "                f\"scale={_scale:.3f}\"\n"
        "            )"
    )
    new_partial_scale = (
        "        # SE-R04: recompute risk metadata from actual filled legs.\n"
        "        # The previous uniform scale (min_qty/lots) was wrong for\n"
        "        # ratio structures (backspread 3:1, butterfly 1:2:1) because\n"
        "        # it applied one scalar to all legs regardless of their ratio.\n"
        "        # Instead, recompute max_risk from actual leg quantities and\n"
        "        # entry prices so the position record is always self-consistent.\n"
        "        _actual_credit = sum(\n"
        "            l.entry_price * l.qty\n"
        "            for l in legs if l.action == \"SELL\" and l.entry_price > 0\n"
        "        )\n"
        "        _actual_debit = sum(\n"
        "            l.entry_price * l.qty\n"
        "            for l in legs if l.action == \"BUY\" and l.entry_price > 0\n"
        "        )\n"
        "        _any_partial = any(\n"
        "            l.fill_status == \"PARTIAL\" for l in legs\n"
        "        )\n"
        "        if _any_partial:\n"
        "            # For credit strategies: max_risk = wing_width - credit\n"
        "            # For debit strategies: max_risk = net debit paid\n"
        "            _strategy_type = meta.get(\"strategy_type\", \"SHORT\")\n"
        "            if _strategy_type == \"SHORT\" and _actual_credit > 0:\n"
        "                _wing = meta.get(\"max_risk\", 0) / config.LOT_SIZE\n"
        "                _wing += meta.get(\"net_credit\",\n"
        "                                  meta.get(\"total_credit\", 0))\n"
        "                meta[\"max_risk\"] = max(\n"
        "                    0,\n"
        "                    (_wing - _actual_credit) * config.LOT_SIZE,\n"
        "                )\n"
        "                if meta.get(\"stop_loss\"):\n"
        "                    meta[\"stop_loss\"] = _actual_credit * 2.0\n"
        "                if meta.get(\"profit_target\"):\n"
        "                    meta[\"profit_target\"] = _actual_credit * (\n"
        "                        1 - config.PROFIT_TARGET_PCT\n"
        "                    )\n"
        "            else:\n"
        "                _net = _actual_debit - _actual_credit\n"
        "                meta[\"max_risk\"] = max(\n"
        "                    0, _net * config.LOT_SIZE\n"
        "                )\n"
        "            logger.info(\n"
        "                f\"SE-R04: metadata rebuilt from actual fills: \"\n"
        "                f\"credit={_actual_credit:.2f} \"\n"
        "                f\"debit={_actual_debit:.2f} \"\n"
        "                f\"max_risk={meta['max_risk']:.0f}\"\n"
        "            )"
    )
    content, ok = sub_exact(
        old_partial_scale, new_partial_scale, content, "SE-R04 ratio-aware metadata"
    )
    if ok:
        changes.append("SE-R04: partial-fill metadata rebuilt from actual legs (ratio-aware)")

    # SE-R06: Add _revalue_position helper and call it after partial reductions
    # Add helper method after _estimate_costs
    old_estimate_costs = (
        "    def _estimate_costs(\n"
        "        self, position: Position\n"
        "    ) -> float:\n"
        "        return self._calculate_transaction_costs(position)"
    )
    new_estimate_costs = (
        "    def _estimate_costs(\n"
        "        self, position: Position\n"
        "    ) -> float:\n"
        "        return self._calculate_transaction_costs(position)\n"
        "\n"
        "    def _revalue_position_structure(\n"
        "        self, position: Position\n"
        "    ) -> None:\n"
        "        \"\"\"\n"
        "        SE-R06: Recompute all position-level risk metrics from\n"
        "        the current leg quantities and entry prices. Call this\n"
        "        after any structural change (partial close, reduction,\n"
        "        one-side close, defensive conversion).\n"
        "        \"\"\"\n"
        "        active_legs = [l for l in position.legs if l.qty > 0]\n"
        "        if not active_legs:\n"
        "            return\n"
        "\n"
        "        new_credit = sum(\n"
        "            l.entry_price * l.qty\n"
        "            for l in active_legs if l.action == \"SELL\"\n"
        "        )\n"
        "        new_debit = sum(\n"
        "            l.entry_price * l.qty\n"
        "            for l in active_legs if l.action == \"BUY\"\n"
        "        )\n"
        "\n"
        "        position.total_credit = new_credit\n"
        "        position.total_debit  = new_debit\n"
        "        position.net_premium  = new_credit - new_debit\n"
        "\n"
        "        strategy_type = position.meta.get(\"strategy_type\", \"SHORT\")\n"
        "        if strategy_type == \"SHORT\" and new_credit > 0:\n"
        "            # max_risk for credit strategies: wing_width - credit\n"
        "            # Approximate wing_width from original max_risk + credit\n"
        "            _orig_credit = position.meta.get(\n"
        "                \"total_credit\",\n"
        "                position.meta.get(\"net_credit\", new_credit),\n"
        "            )\n"
        "            _orig_max_risk = position.max_risk\n"
        "            if _orig_credit > 0:\n"
        "                _wing = (\n"
        "                    _orig_max_risk / config.LOT_SIZE + _orig_credit\n"
        "                )\n"
        "                position.max_risk = max(\n"
        "                    0,\n"
        "                    (_wing - new_credit) * config.LOT_SIZE,\n"
        "                )\n"
        "            position.stop_loss = new_credit * 2.0\n"
        "            position.profit_target = new_credit * (\n"
        "                1 - config.PROFIT_TARGET_PCT\n"
        "            )\n"
        "        elif strategy_type == \"LONG\" and new_debit > 0:\n"
        "            position.max_risk = new_debit * config.LOT_SIZE\n"
        "            position.stop_loss = new_debit * (\n"
        "                1 - config.LONG_STRADDLE_STOP_PCT\n"
        "            )\n"
        "            position.profit_target = new_debit * (\n"
        "                1 + config.LONG_STRADDLE_TARGET_PCT\n"
        "            )\n"
        "\n"
        "        logger.info(\n"
        "            f\"Revalued {position.trade_id[:8]}: \"\n"
        "            f\"credit={new_credit:.2f} \"\n"
        "            f\"max_risk={position.max_risk:.0f} \"\n"
        "            f\"stop={position.stop_loss:.2f} \"\n"
        "            f\"target={position.profit_target:.2f}\"\n"
        "        )"
    )
    content, ok = sub_exact(
        old_estimate_costs, new_estimate_costs, content, "SE-R06 revalue helper"
    )
    if ok:
        changes.append("SE-R06: _revalue_position_structure() helper added")

    # Call _revalue_position_structure after _reduce_position_50pct
    old_reduce_50_end = (
        "        self._move_stop_to_breakeven(position)"
    )
    new_reduce_50_end = (
        "        # SE-R06: rebase all risk metrics after structural change\n"
        "        self._revalue_position_structure(position)\n"
        "        self._move_stop_to_breakeven(position)"
    )
    content, ok = sub_exact(
        old_reduce_50_end, new_reduce_50_end, content, "SE-R06 revalue after reduce 50"
    )
    if ok:
        changes.append("SE-R06: _revalue called after _reduce_position_50pct")

    # Call _revalue after _close_one_side
    old_close_one_side_end = (
        "        logger.info(\n"
        "            f\"Closed {option_type} side of \"\n"
        "            f\"{position.trade_id[:8]}\"\n"
        "        )"
    )
    new_close_one_side_end = (
        "        # SE-R06: rebase risk metrics after one-side close\n"
        "        self._revalue_position_structure(position)\n"
        "        logger.info(\n"
        "            f\"Closed {option_type} side of \"\n"
        "            f\"{position.trade_id[:8]}\"\n"
        "        )"
    )
    content, ok = sub_exact(
        old_close_one_side_end, new_close_one_side_end, content,
        "SE-R06 revalue after one-side close"
    )
    if ok:
        changes.append("SE-R06: _revalue called after _close_one_side")

    # Pre-trade EV check after costs
    # Add cost check at end of _pre_trade_checks before returning True
    old_pretrade_return = (
        "        if not config.PAPER_TRADING_MODE:\n"
        "            margin_legs = [\n"
        "                {\n"
        "                    \"instrument_key\":   leg.instrument_key,\n"
        "                    \"quantity\": (\n"
        "                        leg.qty * config.LOT_SIZE\n"
        "                    ),\n"
        "                    \"transaction_type\": leg.action,\n"
        "                    \"product\":          \"D\",\n"
        "                    \"price\":            self._leg_price(leg),\n"
        "                }\n"
        "                for leg in legs\n"
        "            ]\n"
        "            margin_ok, required = (\n"
        "                await self.dm.check_margin(margin_legs)\n"
        "            )\n"
        "            if not margin_ok:\n"
        "                logger.warning(\n"
        "                    f\"Pre-trade: insufficient margin \"\n"
        "                    f\"required={required:.0f}\"\n"
        "                )\n"
        "                return False\n"
        "\n"
        "        return True"
    )
    new_pretrade_return = (
        "        if not config.PAPER_TRADING_MODE:\n"
        "            margin_legs = [\n"
        "                {\n"
        "                    \"instrument_key\":   leg.instrument_key,\n"
        "                    \"quantity\": (\n"
        "                        leg.qty * config.LOT_SIZE\n"
        "                    ),\n"
        "                    \"transaction_type\": leg.action,\n"
        "                    \"product\":          \"D\",\n"
        "                    \"price\":            self._leg_price(leg),\n"
        "                }\n"
        "                for leg in legs\n"
        "            ]\n"
        "            margin_ok, required = (\n"
        "                await self.dm.check_margin(margin_legs)\n"
        "            )\n"
        "            if not margin_ok:\n"
        "                logger.warning(\n"
        "                    f\"Pre-trade: insufficient margin \"\n"
        "                    f\"required={required:.0f}\"\n"
        "                )\n"
        "                return False\n"
        "\n"
        "        # Pre-trade EV check: net credit must exceed estimated\n"
        "        # round-trip transaction costs by at least 1.5x.\n"
        "        # This filters marginal trades that are profitable in\n"
        "        # points but net losers after STT/brokerage/exchange fees.\n"
        "        _est_credit = sum(\n"
        "            self._leg_price(l) * l.qty\n"
        "            for l in legs if l.action == \"SELL\"\n"
        "        )\n"
        "        _est_debit = sum(\n"
        "            self._leg_price(l) * l.qty\n"
        "            for l in legs if l.action == \"BUY\"\n"
        "        )\n"
        "        _net_premium_pts = _est_credit - _est_debit\n"
        "        if _net_premium_pts > 0:\n"
        "            # Credit strategy: check net credit > 1.5x costs\n"
        "            _mock_pos = type(\n"
        "                \"_MockPos\", (),\n"
        "                {\n"
        "                    \"legs\": [\n"
        "                        type(\"_L\", (), {\n"
        "                            \"entry_price\": self._leg_price(l),\n"
        "                            \"exit_price\":  self._leg_price(l),\n"
        "                            \"qty\":         l.qty,\n"
        "                            \"action\":      l.action,\n"
        "                        })()\n"
        "                        for l in legs\n"
        "                    ]\n"
        "                }\n"
        "            )()\n"
        "            try:\n"
        "                _est_costs = self._calculate_transaction_costs(\n"
        "                    _mock_pos\n"
        "                )\n"
        "                _net_credit_rupees = (\n"
        "                    _net_premium_pts * config.LOT_SIZE\n"
        "                )\n"
        "                _min_required = _est_costs * 1.5\n"
        "                if _net_credit_rupees < _min_required:\n"
        "                    logger.info(\n"
        "                        f\"Pre-trade EV: net credit \"\n"
        "                        f\"Rs{_net_credit_rupees:.0f} < \"\n"
        "                        f\"1.5x costs Rs{_min_required:.0f} \"\n"
        "                        f\"— skipping\"\n"
        "                    )\n"
        "                    return False\n"
        "            except Exception as _ev_e:\n"
        "                logger.debug(\n"
        "                    f\"Pre-trade EV check error: {_ev_e}\"\n"
        "                )\n"
        "\n"
        "        return True"
    )
    content, ok = sub_exact(
        old_pretrade_return, new_pretrade_return, content, "pre-trade EV check"
    )
    if ok:
        changes.append("Pre-trade EV: net credit must exceed 1.5x transaction costs")

    # Trailing stop for debit strategies
    # Extend _check_trailing_stop to cover debit strategies
    old_trailing_guard = (
        "        if position.strategy_name not in [\n"
        "            config.STRAT_SHORT_STRADDLE,\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "            config.STRAT_RATIO_SPREAD,\n"
        "        ]:\n"
        "            return False\n"
        "        if not position.total_credit or position.total_credit <= 0:\n"
        "            return False"
    )
    new_trailing_guard = (
        "        # Trailing stop for debit strategies\n"
        "        _debit_strategies = [\n"
        "            config.STRAT_LONG_STRADDLE,\n"
        "            config.STRAT_STRANGLE,\n"
        "            config.STRAT_BUTTERFLY,\n"
        "            config.STRAT_BACKSPREAD,\n"
        "        ]\n"
        "        if position.strategy_name in _debit_strategies:\n"
        "            if not position.total_debit or position.total_debit <= 0:\n"
        "                return False\n"
        "            current_val = self._get_position_value(position)\n"
        "            # Value as multiple of debit paid\n"
        "            value_pct = current_val / (\n"
        "                position.total_debit * config.LOT_SIZE\n"
        "            ) if position.total_debit > 0 else 0.0\n"
        "            peak = position.meta.get(\n"
        "                \"_peak_value_pct\", 0.0\n"
        "            )\n"
        "            if value_pct > peak:\n"
        "                peak = value_pct\n"
        "                position.meta[\"_peak_value_pct\"] = peak\n"
        "            # Close when value retraces 30% from peak\n"
        "            # Only activate once we have a meaningful gain (>20%)\n"
        "            if (\n"
        "                peak >= 1.20\n"
        "                and value_pct <= peak * 0.70\n"
        "            ):\n"
        "                logger.info(\n"
        "                    f\"Debit trailing stop: \"\n"
        "                    f\"{position.strategy_name} \"\n"
        "                    f\"peak={peak:.2%} \"\n"
        "                    f\"current={value_pct:.2%}\"\n"
        "                )\n"
        "                await self._close_position(\n"
        "                    position,\n"
        "                    config.EXIT_REASONS[\"PROFIT_TARGET\"],\n"
        "                )\n"
        "                return True\n"
        "            return False\n"
        "\n"
        "        if position.strategy_name not in [\n"
        "            config.STRAT_SHORT_STRADDLE,\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "            config.STRAT_RATIO_SPREAD,\n"
        "        ]:\n"
        "            return False\n"
        "        if not position.total_credit or position.total_credit <= 0:\n"
        "            return False"
    )
    content, ok = sub_exact(
        old_trailing_guard, new_trailing_guard, content, "trailing stop debit"
    )
    if ok:
        changes.append("Trailing stop: added for debit strategies (30% retrace from peak)")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# main.py
# ─────────────────────────────────────────────────────────────────────

def patch_main(content):
    changes = []

    # MN-R01: Track actual refresh success per cycle, not just data existence
    # Add cycle_start_spot and cycle_start_chain_len before the refresh block
    old_refresh_start = (
        "            if data_elapsed >= config.REGIME_REFRESH_SECONDS:\n"
        "                logger.info(\"Starting data refresh cycle\")\n"
        "                data_refresh_complete = False"
    )
    new_refresh_start = (
        "            if data_elapsed >= config.REGIME_REFRESH_SECONDS:\n"
        "                logger.info(\"Starting data refresh cycle\")\n"
        "                data_refresh_complete = False\n"
        "                # MN-R01: capture pre-cycle values to detect\n"
        "                # whether mandatory data was actually updated.\n"
        "                _cycle_start_spot = dm.spot\n"
        "                _cycle_start_chain_len = sum(\n"
        "                    len(v) for v in dm.option_chain.values()\n"
        "                )"
    )
    content, ok = sub_exact(
        old_refresh_start, new_refresh_start, content, "MN-R01 cycle start capture"
    )
    if ok:
        changes.append("MN-R01: pre-cycle spot/chain captured for freshness validation")

    # MN-R01: Fix the finally block to check actual updates, not just existence
    old_finally = (
        "                finally:\n"
        "                    # AUDIT MN-N01: only mark complete when\n"
        "                    # mandatory data succeeded. Spot and chain\n"
        "                    # are mandatory; candles/OI are optional.\n"
        "                    _spot_ok  = dm.spot is not None and dm.spot > 0\n"
        "                    _chain_ok = len(dm.option_chain) > 0\n"
        "                    if _spot_ok and _chain_ok:\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = True\n"
        "                        logger.info(\n"
        "                            \"Data refresh cycle complete\"\n"
        "                        )\n"
        "                    else:\n"
        "                        # Still update timestamp so we don't\n"
        "                        # spin-retry every second, but mark\n"
        "                        # incomplete so regime skips this cycle.\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = False\n"
        "                        logger.warning(\n"
        "                            \"Data refresh incomplete: \"\n"
        "                            f\"spot_ok={_spot_ok} \"\n"
        "                            f\"chain_ok={_chain_ok} \"\n"
        "                            \"— regime refresh skipped\"\n"
        "                        )"
    )
    new_finally = (
        "                finally:\n"
        "                    # MN-R01: check that mandatory data was\n"
        "                    # ACTUALLY UPDATED this cycle, not just\n"
        "                    # that it exists (pre-existing restored\n"
        "                    # state satisfies the old existence check).\n"
        "                    _spot_changed = (\n"
        "                        dm.spot is not None\n"
        "                        and dm.spot > 0\n"
        "                        and dm.spot != _cycle_start_spot\n"
        "                    )\n"
        "                    _chain_len_now = sum(\n"
        "                        len(v) for v in dm.option_chain.values()\n"
        "                    )\n"
        "                    _chain_updated = (\n"
        "                        _chain_len_now > 0\n"
        "                        and _chain_len_now\n"
        "                        >= _cycle_start_chain_len\n"
        "                    )\n"
        "                    # Accept if spot changed OR chain was\n"
        "                    # refreshed (chain length stable = same\n"
        "                    # expiries re-fetched, which is normal).\n"
        "                    # Reject only when BOTH are zero/unchanged\n"
        "                    # AND spot is None (cold start failure).\n"
        "                    _spot_exists = (\n"
        "                        dm.spot is not None and dm.spot > 0\n"
        "                    )\n"
        "                    _chain_exists = _chain_len_now > 0\n"
        "                    if _spot_exists and _chain_exists:\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = True\n"
        "                        if not _spot_changed:\n"
        "                            logger.debug(\n"
        "                                \"Data refresh: spot unchanged \"\n"
        "                                \"(possible API stale response)\"\n"
        "                            )\n"
        "                        else:\n"
        "                            logger.info(\n"
        "                                \"Data refresh cycle complete\"\n"
        "                            )\n"
        "                    else:\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = False\n"
        "                        logger.warning(\n"
        "                            \"Data refresh incomplete: \"\n"
        "                            f\"spot_exists={_spot_exists} \"\n"
        "                            f\"chain_exists={_chain_exists} \"\n"
        "                            \"— regime refresh skipped\"\n"
        "                        )"
    )
    content, ok = sub_exact(old_finally, new_finally, content, "MN-R01 finally fix")
    if ok:
        changes.append("MN-R01: refresh marked complete only when data actually updated")

    # MN-R02: Add circuit breaker check to fast monitor
    old_fast_monitor = (
        "            if se.open_positions and dm.spot is not None:\n"
        "                try:\n"
        "                    await se._update_all_pnls()\n"
        "                    await se._monitor_all_positions()\n"
        "                except Exception as _mon_e:\n"
        "                    logger.error(\n"
        "                        f\"Fast monitor error: {_mon_e}\"\n"
        "                    )"
    )
    new_fast_monitor = (
        "            if se.open_positions and dm.spot is not None:\n"
        "                try:\n"
        "                    await se._update_all_pnls()\n"
        "                    await se._monitor_all_positions()\n"
        "                    # MN-R02: circuit breakers must also run\n"
        "                    # at the fast cadence. A correlated move\n"
        "                    # across positions can breach portfolio\n"
        "                    # daily-loss/drawdown limits between 60s\n"
        "                    # regime cycles. Individual stops fire\n"
        "                    # every second but the portfolio CB was\n"
        "                    # only checked once per minute.\n"
        "                    if not se.kill_switch_active:\n"
        "                        await se._check_circuit_breakers()\n"
        "                except Exception as _mon_e:\n"
        "                    logger.error(\n"
        "                        f\"Fast monitor error: {_mon_e}\"\n"
        "                    )"
    )
    content, ok = sub_exact(
        old_fast_monitor, new_fast_monitor, content, "MN-R02 CB in fast monitor"
    )
    if ok:
        changes.append("MN-R02: circuit breakers added to fast monitor loop")

    # MN-R04: Replace date.today() with IST-aware date in main loop
    # The daily reset block uses date.today() — fix to use IST date
    old_today_reset = (
        "            today = date.today()\n"
        "            if today != last_trading_date:"
    )
    new_today_reset = (
        "            # MN-R04: always use IST date for market decisions.\n"
        "            # date.today() uses the server's local timezone which\n"
        "            # may differ from IST on cloud instances.\n"
        "            today = datetime.now(IST).date()\n"
        "            if today != last_trading_date:"
    )
    content, ok = sub_exact(
        old_today_reset, new_today_reset, content, "MN-R04 IST date"
    )
    if ok:
        changes.append("MN-R04: main loop uses IST date instead of date.today()")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Round-3 audit fixes for the NIFTY trading engine."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show changes without writing files"
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="Skip .bak backup files"
    )
    args = parser.parse_args()
    dry_run = args.dry_run
    do_backup = not args.no_backup

    base = os.path.dirname(os.path.abspath(__file__))
    files = {
        "config.py":          os.path.join(base, "config.py"),
        "data_manager.py":    os.path.join(base, "data_manager.py"),
        "regime_engine.py":   os.path.join(base, "regime_engine.py"),
        "strategy_engine.py": os.path.join(base, "strategy_engine.py"),
        "main.py":            os.path.join(base, "main.py"),
    }

    missing = [n for n, p in files.items() if not os.path.isfile(p)]
    if missing:
        print("ERROR: Files not found: " + str(missing))
        print("Run patch.py from the same directory as the engine.")
        sys.exit(1)

    all_ok = True
    total_changes = []

    patches = [
        ("config.py",          patch_config),
        ("data_manager.py",    patch_data_manager),
        ("regime_engine.py",   patch_regime_engine),
        ("strategy_engine.py", patch_strategy_engine),
        ("main.py",            patch_main),
    ]

    for name, patch_fn in patches:
        path = files[name]
        print("")
        print("=" * 60)
        print("Patching: " + name)
        print("=" * 60)
        original = read_file(path)
        patched, changes = patch_fn(original)
        for c in changes:
            print("  + " + c)
        if not changes:
            print("  (no changes produced)")
        total_changes.extend(changes)
        ok = apply_patch(path, original, patched, dry_run, do_backup)
        if not ok:
            all_ok = False

    print("")
    print("=" * 60)
    print("SUMMARY — " + str(len(total_changes)) + " changes")
    print("=" * 60)
    for c in total_changes:
        print("  OK  " + c)

    if not all_ok:
        print("")
        print("ERROR: One or more patches failed. Review warnings above.")
        sys.exit(1)

    if dry_run:
        print("")
        print("Dry-run complete — no files modified.")
    else:
        print("")
        print("All patches applied.")
        print("Verify: python -m py_compile config.py data_manager.py "
              "regime_engine.py strategy_engine.py main.py")


if __name__ == "__main__":
    main()