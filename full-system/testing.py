#!/usr/bin/env python3
"""
patch.py — Round-4 audit fixes for the NIFTY options trading engine.

Fixes applied (confirmed-valid only):

  strategy_engine.py
    Ratio spread max_risk undercalculation (critical)
    Backspread premium stop ignored
    P&L fallback uses entry_price on exit failure
    SE-T01  _get_position_value / _get_position_current_premium use stale mid
    SE-T04  Order tags use date.today() not IST date
    Partial close exit_price overwrite (carried forward, incomplete fix)
    SE-T06  Greeks not age-validated before limits check
    ATR contraction too restrictive
    MN-T04  CB Level 5 fires every second (idempotency guard)

  data_manager.py
    DM-T01  Rejected LTP given fresh _ltp_ts
    DM-T02  REST chain LTP has no _ltp_ts
    DM-T03  get_mark_price does not reject crossed markets

  regime_engine.py
    RE-T01  Hysteresis creates hidden thresholds (explicit enter/exit constants)
    RE-T02  Integer rounding destroys score granularity (keep floats)
    RE-T03/T04  Stale-score decay uses wall-clock time + persisted last_valid_at

  main.py
    MN-T01  Kill switch suspends position monitoring
    MN-T02/T03  EOD marked complete without confirming positions closed
    MN-T05  Data refresh variables don't prove a refresh happened

  config.py
    RE-T01  Add STRONG_SELL_ENTER/EXIT and MILD_SELL_ENTER/EXIT constants

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


# ─────────────────────────────────────────────────────────────────────
# config.py
# ─────────────────────────────────────────────────────────────────────

def patch_config(content):
    changes = []

    # RE-T01: Add explicit hysteresis enter/exit threshold constants
    # so operators can tune them directly without hidden inline math.
    old_thresholds = (
        "STRONG_SELL_THRESHOLD =  0.45   # reference: x > 0.45\n"
        "MILD_SELL_THRESHOLD   =  0.15   # reference: x >= 0.15\n"
        "MILD_BUY_THRESHOLD    = -0.15   # reference: x > -0.15 = NEUTRAL\n"
        "STRONG_BUY_THRESHOLD  = -0.45   # reference: x >= -0.45 = BUY_VOL"
    )
    new_thresholds = (
        "STRONG_SELL_THRESHOLD =  0.45   # reference: x > 0.45\n"
        "MILD_SELL_THRESHOLD   =  0.15   # reference: x >= 0.15\n"
        "MILD_BUY_THRESHOLD    = -0.15   # reference: x > -0.15 = NEUTRAL\n"
        "STRONG_BUY_THRESHOLD  = -0.45   # reference: x >= -0.45 = BUY_VOL\n"
        "\n"
        "# RE-T01: explicit hysteresis enter/exit thresholds.\n"
        "# Enter a regime when composite crosses the ENTER threshold;\n"
        "# exit only when it crosses the EXIT threshold in the opposite\n"
        "# direction. This prevents churn near boundaries without\n"
        "# creating hidden thresholds that contradict the base values.\n"
        "# Band = 0.05 composite units (tune here, not inline).\n"
        "STRONG_SELL_ENTER =  0.45   # enter STRONG_SELL above this\n"
        "STRONG_SELL_EXIT  =  0.40   # exit  STRONG_SELL below this\n"
        "MILD_SELL_ENTER   =  0.15   # enter MILD_SELL above this\n"
        "MILD_SELL_EXIT    =  0.10   # exit  MILD_SELL below this\n"
        "MILD_BUY_ENTER    = -0.15   # enter NEUTRAL above this (from BUY_VOL)\n"
        "MILD_BUY_EXIT     = -0.20   # exit  NEUTRAL below this\n"
        "STRONG_BUY_ENTER  = -0.45   # enter BUY_VOL above this (from STRONG_BUY)\n"
        "STRONG_BUY_EXIT   = -0.50   # exit  STRONG_BUY above this"
    )
    content, ok = sub_exact(
        old_thresholds, new_thresholds, content, "RE-T01 hysteresis constants"
    )
    if ok:
        changes.append("RE-T01: explicit ENTER/EXIT hysteresis constants added")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# data_manager.py
# ─────────────────────────────────────────────────────────────────────

def patch_data_manager(content):
    changes = []

    # DM-T01: Only update _ltp_ts when the LTP is actually accepted.
    # The previous patch updated _ltp_ts outside the acceptance branch,
    # so a rejected outlier tick was still given a fresh timestamp,
    # making get_mark_price() trust the old (unchanged) LTP.
    old_outlier_block = (
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
    new_outlier_block = (
        "                    if _rest_fresh and bid_ref > 0 and ask_ref > 0:\n"
        "                        mid_ref    = (bid_ref + ask_ref) / 2.0\n"
        "                        pct_thresh = max(10.0, mid_ref * 0.05)\n"
        "                        if abs(ltp - mid_ref) > pct_thresh:\n"
        "                            # DM-T01: do NOT update _ltp_ts on\n"
        "                            # rejection. Updating it here made\n"
        "                            # get_mark_price() trust the old\n"
        "                            # (unchanged) LTP as if it were fresh.\n"
        "                            logger.warning(\n"
        "                                f\"Option LTP outlier rejected: \"\n"
        "                                f\"{option_type} {strike} {expiry} \"\n"
        "                                f\"ltp={ltp:.2f} mid={mid_ref:.2f} \"\n"
        "                                f\"({abs(ltp-mid_ref)/mid_ref*100:.1f}%)\"\n"
        "                            )\n"
        "                        else:\n"
        "                            # Accept: update both value and timestamp\n"
        "                            opt_ref[\"ltp\"] = ltp\n"
        "                            opt_ref[\"_ltp_ts\"] = datetime.now(\n"
        "                                pytz.timezone(config.TZ)\n"
        "                            ).isoformat()\n"
        "                    else:\n"
        "                        # REST quote stale or no bid/ask: accept LTP\n"
        "                        # without outlier check (can't validate)\n"
        "                        opt_ref[\"ltp\"] = ltp\n"
        "                        opt_ref[\"_ltp_ts\"] = datetime.now(\n"
        "                            pytz.timezone(config.TZ)\n"
        "                        ).isoformat()"
    )
    content, ok = sub_exact(
        old_outlier_block, new_outlier_block, content, "DM-T01 _ltp_ts on accept only"
    )
    if ok:
        changes.append("DM-T01: _ltp_ts only updated when LTP is accepted, not rejected")

    # DM-T02: Set _ltp_ts when REST chain populates ltp so that
    # get_mark_price() can use a fresh REST LTP for illiquid strikes.
    # We patch the ltp assignment inside fetch_option_chain's parsed dict.
    old_rest_ltp = (
        "                        \"ltp\":    _sf(call_md.get(\"ltp\", 0)),"
    )
    new_rest_ltp = (
        "                        \"ltp\":    _sf(call_md.get(\"ltp\", 0)),\n"
        "                        # DM-T02: REST LTP gets _ltp_ts so\n"
        "                        # get_mark_price() treats it as fresh.\n"
        "                        \"_ltp_ts\": datetime.now(\n"
        "                            self._IST\n"
        "                        ).isoformat(),"
    )
    content, ok = sub_exact(
        old_rest_ltp, new_rest_ltp, content, "DM-T02 REST call ltp_ts"
    )
    if ok:
        changes.append("DM-T02: REST chain call ltp gets _ltp_ts")

    old_rest_put_ltp = (
        "                        \"ltp\":    _sf(put_md.get(\"ltp\", 0)),"
    )
    new_rest_put_ltp = (
        "                        \"ltp\":    _sf(put_md.get(\"ltp\", 0)),\n"
        "                        # DM-T02: REST LTP gets _ltp_ts\n"
        "                        \"_ltp_ts\": datetime.now(\n"
        "                            self._IST\n"
        "                        ).isoformat(),"
    )
    content, ok = sub_exact(
        old_rest_put_ltp, new_rest_put_ltp, content, "DM-T02 REST put ltp_ts"
    )
    if ok:
        changes.append("DM-T02: REST chain put ltp gets _ltp_ts")

    # DM-T03: Reject crossed markets in get_mark_price()
    old_mark_mid = (
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
    new_mark_mid = (
        "        # DM-T03: reject crossed markets (bid > ask).\n"
        "        _bid_ask_valid = bid > 0 and ask > 0 and ask >= bid\n"
        "\n"
        "        # 1. Fresh REST midpoint\n"
        "        if rest_age <= max_quote_age_sec and _bid_ask_valid:\n"
        "            return (bid + ask) / 2.0\n"
        "        # 2. Fresh WS LTP\n"
        "        if ltp_age <= max_ltp_age_sec and ltp > 0:\n"
        "            return ltp\n"
        "        # 3. Stale REST midpoint (bounded age, still valid)\n"
        "        if rest_age <= max_rest_fallback_age_sec and _bid_ask_valid:\n"
        "            return (bid + ask) / 2.0\n"
        "        # 4. fallback\n"
        "        return fallback"
    )
    content, ok = sub_exact(
        old_mark_mid, new_mark_mid, content, "DM-T03 crossed market rejection"
    )
    if ok:
        changes.append("DM-T03: get_mark_price() rejects crossed markets (bid > ask)")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# regime_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # RE-T01: Replace inline hysteresis math with config constants
    old_hysteresis = (
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
    new_hysteresis = (
        "    def _map_regime(self, composite: float) -> str:\n"
        "        \"\"\"Regime mapping with explicit config-driven hysteresis.\n"
        "        RE-T01: thresholds are read from config.STRONG_SELL_ENTER\n"
        "        etc. so operators can tune them directly. The previous\n"
        "        inline `threshold ± 0.05` created hidden effective\n"
        "        thresholds that contradicted the documented base values.\n"
        "        \"\"\"\n"
        "        current = self.confirmed_regime\n"
        "\n"
        "        # Persistence: stay in current regime until the EXIT\n"
        "        # threshold is crossed in the opposite direction.\n"
        "        if current == config.REGIME_STRONG_SELL:\n"
        "            if composite > getattr(\n"
        "                config, \"STRONG_SELL_EXIT\",\n"
        "                config.STRONG_SELL_THRESHOLD - 0.05\n"
        "            ):\n"
        "                return config.REGIME_STRONG_SELL\n"
        "        elif current == config.REGIME_MILD_SELL:\n"
        "            _ms_exit = getattr(\n"
        "                config, \"MILD_SELL_EXIT\",\n"
        "                config.MILD_SELL_THRESHOLD - 0.05\n"
        "            )\n"
        "            _ss_exit = getattr(\n"
        "                config, \"STRONG_SELL_EXIT\",\n"
        "                config.STRONG_SELL_THRESHOLD - 0.05\n"
        "            )\n"
        "            if _ms_exit <= composite <= (\n"
        "                getattr(\n"
        "                    config, \"STRONG_SELL_ENTER\",\n"
        "                    config.STRONG_SELL_THRESHOLD\n"
        "                )\n"
        "            ):\n"
        "                return config.REGIME_MILD_SELL\n"
        "        elif current == config.REGIME_NEUTRAL:\n"
        "            _nb_exit = getattr(\n"
        "                config, \"MILD_BUY_EXIT\",\n"
        "                config.MILD_BUY_THRESHOLD - 0.05\n"
        "            )\n"
        "            _ns_enter = getattr(\n"
        "                config, \"MILD_SELL_ENTER\",\n"
        "                config.MILD_SELL_THRESHOLD\n"
        "            )\n"
        "            if _nb_exit < composite < _ns_enter:\n"
        "                return config.REGIME_NEUTRAL\n"
        "        elif current == config.REGIME_BUY_VOL:\n"
        "            _bv_exit = getattr(\n"
        "                config, \"STRONG_BUY_EXIT\",\n"
        "                config.STRONG_BUY_THRESHOLD - 0.05\n"
        "            )\n"
        "            _bv_top = getattr(\n"
        "                config, \"MILD_BUY_ENTER\",\n"
        "                config.MILD_BUY_THRESHOLD\n"
        "            )\n"
        "            if _bv_exit <= composite <= _bv_top:\n"
        "                return config.REGIME_BUY_VOL\n"
        "        elif current == config.REGIME_STRONG_BUY:\n"
        "            if composite < getattr(\n"
        "                config, \"STRONG_BUY_EXIT\",\n"
        "                config.STRONG_BUY_THRESHOLD - 0.05\n"
        "            ):\n"
        "                return config.REGIME_STRONG_BUY\n"
        "\n"
        "        # Entry: use ENTER thresholds (fall back to base if not set)\n"
        "        _ss_enter = getattr(\n"
        "            config, \"STRONG_SELL_ENTER\",\n"
        "            config.STRONG_SELL_THRESHOLD\n"
        "        )\n"
        "        _ms_enter = getattr(\n"
        "            config, \"MILD_SELL_ENTER\",\n"
        "            config.MILD_SELL_THRESHOLD\n"
        "        )\n"
        "        _mb_enter = getattr(\n"
        "            config, \"MILD_BUY_ENTER\",\n"
        "            config.MILD_BUY_THRESHOLD\n"
        "        )\n"
        "        _sb_enter = getattr(\n"
        "            config, \"STRONG_BUY_ENTER\",\n"
        "            config.STRONG_BUY_THRESHOLD\n"
        "        )\n"
        "        if composite > _ss_enter:\n"
        "            return config.REGIME_STRONG_SELL\n"
        "        if composite >= _ms_enter:\n"
        "            return config.REGIME_MILD_SELL\n"
        "        if composite > _mb_enter:\n"
        "            return config.REGIME_NEUTRAL\n"
        "        if composite >= _sb_enter:\n"
        "            return config.REGIME_BUY_VOL\n"
        "        return config.REGIME_STRONG_BUY"
    )
    content, ok = sub_exact(
        old_hysteresis, new_hysteresis, content, "RE-T01 config-driven hysteresis"
    )
    if ok:
        changes.append("RE-T01: _map_regime() uses config ENTER/EXIT constants")

    # RE-T02: Keep module scores as floats in persistence buffers.
    # The current code rounds to int, which maps ±0.5 to ±1 (max conviction)
    # even when only one of two sub-signals fired. Keep float values and
    # confirm on sign-stability (same sign for 3 consecutive readings).
    old_persist_round = (
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
        "        buf     = self._buf[name]\n"
        "        # Reset decay counter when a real value arrives\n"
        "        setattr(self, \"_none_count_\" + name, 0)\n"
        "        buf.append(raw_int)"
    )
    new_persist_round = (
        "        # RE-T02: keep scores as floats. Rounding ±0.5 to ±1\n"
        "        # (even symmetrically) overstates conviction when only\n"
        "        # one of two sub-signals fired. Confirm on sign-stability\n"
        "        # across 3 consecutive readings instead of exact-integer\n"
        "        # equality, which is meaningless for floats anyway.\n"
        "        _raw_f = float(raw)\n"
        "        buf    = self._buf[name]\n"
        "        # Reset decay counter when a real value arrives\n"
        "        setattr(self, \"_none_count_\" + name, 0)\n"
        "        buf.append(_raw_f)"
    )
    content, ok = sub_exact(
        old_persist_round, new_persist_round, content, "RE-T02 float persistence"
    )
    if ok:
        changes.append("RE-T02: module scores kept as floats in persistence buffer")

    # RE-T02: Update the confirmation logic to use sign-stability on floats
    old_confirm_logic = (
        "        if len(buf) == 3 and buf[0] == buf[1] == buf[2]:\n"
        "            self._conf[name] = raw_int\n"
        "            logger.info(\n"
        "                f\"Persistence confirmed: {name}={raw_int}\"\n"
        "            )\n"
        "        else:\n"
        "            logger.info(\n"
        "                f\"Persistence unconfirmed: {name} \"\n"
        "                f\"buf={buf} \"\n"
        "                f\"holding={self._conf[name]}\"\n"
        "            )\n"
        "        return self._conf[name]"
    )
    new_confirm_logic = (
        "        # RE-T02: confirm when the last 3 readings have the\n"
        "        # same sign (or are all zero). Use the mean of the\n"
        "        # buffer as the confirmed value to preserve granularity.\n"
        "        import math as _math_p\n"
        "        if len(buf) >= 3:\n"
        "            _last3 = buf[-3:]\n"
        "            _signs = [_math_p.copysign(1, v) if v != 0 else 0\n"
        "                      for v in _last3]\n"
        "            if len(set(_signs)) == 1:  # all same sign\n"
        "                _confirmed = sum(_last3) / len(_last3)\n"
        "                self._conf[name] = _confirmed\n"
        "                logger.info(\n"
        "                    f\"Persistence confirmed: \"\n"
        "                    f\"{name}={_confirmed:.3f} \"\n"
        "                    f\"(sign-stable over 3 readings)\"\n"
        "                )\n"
        "            else:\n"
        "                logger.info(\n"
        "                    f\"Persistence unconfirmed: {name} \"\n"
        "                    f\"buf={[round(v,3) for v in buf[-3:]]} \"\n"
        "                    f\"holding={self._conf[name]:.3f}\"\n"
        "                )\n"
        "        return self._conf[name]"
    )
    content, ok = sub_exact(
        old_confirm_logic, new_confirm_logic, content, "RE-T02 sign-stability confirm"
    )
    if ok:
        changes.append("RE-T02: persistence confirms on sign-stability, not integer equality")

    # RE-T03/T04: Replace cycle-count decay with wall-clock decay
    # and persist last_valid_at per module in SQLite.
    old_decay_none = (
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
    new_decay_none = (
        "        if raw is None:\n"
        "            # RE-T03/T04: wall-clock decay using last_valid_at.\n"
        "            # Cycle-count decay was imprecise (API delays could\n"
        "            # make 10 cycles take 20+ min). Use actual elapsed\n"
        "            # exchange time. last_valid_at is persisted in SQLite\n"
        "            # (see _save_state/_load_state) so restarts don't\n"
        "            # reset the grace period.\n"
        "            _lva_key = \"_last_valid_at_\" + name\n"
        "            _last_valid = getattr(self, _lva_key, None)\n"
        "            if _last_valid is not None:\n"
        "                try:\n"
        "                    _elapsed = (\n"
        "                        datetime.now(self._IST)\n"
        "                        - _last_valid\n"
        "                    ).total_seconds()\n"
        "                    # Decay after 10 minutes of no data\n"
        "                    if _elapsed > 600 and self._conf[name] != 0:\n"
        "                        _old = self._conf[name]\n"
        "                        # Decay by 10% of current value per\n"
        "                        # additional 5-minute interval\n"
        "                        _intervals = int(\n"
        "                            (_elapsed - 600) / 300\n"
        "                        ) + 1\n"
        "                        _decay = _old * (0.90 ** _intervals)\n"
        "                        if abs(_decay) < 0.05:\n"
        "                            _decay = 0.0\n"
        "                        self._conf[name] = _decay\n"
        "                        logger.info(\n"
        "                            f\"RE-T03: {name} score decayed \"\n"
        "                            f\"{_old:.3f} -> {_decay:.3f} \"\n"
        "                            f\"(elapsed={_elapsed:.0f}s)\"\n"
        "                        )\n"
        "                except Exception:\n"
        "                    pass\n"
        "            return self._conf[name]"
    )
    content, ok = sub_exact(
        old_decay_none, new_decay_none, content, "RE-T03/T04 wall-clock decay"
    )
    if ok:
        changes.append("RE-T03/T04: stale-score decay uses wall-clock time")

    # RE-T03/T04: Set last_valid_at when a real value arrives
    old_reset_none_count = (
        "        # Reset decay counter when a real value arrives\n"
        "        setattr(self, \"_none_count_\" + name, 0)"
    )
    new_reset_none_count = (
        "        # RE-T03/T04: record when this module last had real data\n"
        "        setattr(\n"
        "            self,\n"
        "            \"_last_valid_at_\" + name,\n"
        "            datetime.now(self._IST),\n"
        "        )"
    )
    content, ok = sub_exact(
        old_reset_none_count, new_reset_none_count, content, "RE-T04 last_valid_at"
    )
    if ok:
        changes.append("RE-T04: last_valid_at set when real module value arrives")

    # RE-T04: Persist last_valid_at in _save_state
    old_save_items = (
        "                (\"last_save_date\",  _today_save),\n"
        "            ]:"
    )
    new_save_items = (
        "                (\"last_save_date\",  _today_save),\n"
        "                (\"last_valid_at\", {\n"
        "                    m: getattr(\n"
        "                        self,\n"
        "                        \"_last_valid_at_\" + m,\n"
        "                        None,\n"
        "                    ).isoformat()\n"
        "                    if getattr(\n"
        "                        self,\n"
        "                        \"_last_valid_at_\" + m,\n"
        "                        None,\n"
        "                    ) is not None else None\n"
        "                    for m in MODULES\n"
        "                }),\n"
        "            ]:"
    )
    content, ok = sub_exact(
        old_save_items, new_save_items, content, "RE-T04 persist last_valid_at"
    )
    if ok:
        changes.append("RE-T04: last_valid_at persisted in SQLite via _save_state")

    # RE-T04: Restore last_valid_at in _load_state
    old_load_confirmed = (
        "                    elif key == \"confirmed\":\n"
        "                        self._conf = {\n"
        "                            m: int(data.get(m, 0))\n"
        "                            for m in MODULES\n"
        "                        }"
    )
    new_load_confirmed = (
        "                    elif key == \"confirmed\":\n"
        "                        self._conf = {\n"
        "                            m: float(data.get(m, 0))\n"
        "                            for m in MODULES\n"
        "                        }\n"
        "                    elif key == \"last_valid_at\":\n"
        "                        for m in MODULES:\n"
        "                            ts_str = data.get(m)\n"
        "                            if ts_str:\n"
        "                                try:\n"
        "                                    _ts = datetime.fromisoformat(\n"
        "                                        ts_str\n"
        "                                    )\n"
        "                                    if _ts.tzinfo is None:\n"
        "                                        _ts = self._IST.localize(_ts)\n"
        "                                    setattr(\n"
        "                                        self,\n"
        "                                        \"_last_valid_at_\" + m,\n"
        "                                        _ts,\n"
        "                                    )\n"
        "                                except Exception:\n"
        "                                    pass"
    )
    content, ok = sub_exact(
        old_load_confirmed, new_load_confirmed, content, "RE-T04 restore last_valid_at"
    )
    if ok:
        changes.append("RE-T04: last_valid_at restored from SQLite on startup")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# strategy_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # Ratio spread max_risk undercalculation
    old_ratio_risk = (
        "        meta = {\n"
        "            \"total_credit\":  total_credit,\n"
        "            \"max_risk\":      total_credit * 2 * config.LOT_SIZE,\n"
        "            \"profit_target\": total_credit * (\n"
        "                1 - config.RATIO_TARGET_PCT\n"
        "            ),\n"
        "            \"stop_loss\":     total_credit * 2.0,\n"
        "            \"exit_dte\":      config.RATIO_EXIT_DTE,\n"
        "            \"max_hold_date\": None,\n"
        "            \"strategy_type\": \"SHORT\",\n"
        "        }"
    )
    new_ratio_risk = (
        "        # Ratio spread max_risk fix: the maximum loss of a\n"
        "        # 1x2 ratio spread occurs when the underlying pins the\n"
        "        # long strike at expiry. Max loss = wing_width - credit.\n"
        "        # The old formula (credit * 2) understated this by ~2x\n"
        "        # for typical credit levels, causing over-leveraging.\n"
        "        _ratio_max_risk = max(\n"
        "            (config.RATIO_ATM_OFFSET_PTS - total_credit)\n"
        "            * config.LOT_SIZE,\n"
        "            total_credit * config.LOT_SIZE,  # floor: at least credit\n"
        "        )\n"
        "        meta = {\n"
        "            \"total_credit\":  total_credit,\n"
        "            \"max_risk\":      _ratio_max_risk,\n"
        "            \"profit_target\": total_credit * (\n"
        "                1 - config.RATIO_TARGET_PCT\n"
        "            ),\n"
        "            \"stop_loss\":     total_credit * 2.0,\n"
        "            \"exit_dte\":      config.RATIO_EXIT_DTE,\n"
        "            \"max_hold_date\": None,\n"
        "            \"strategy_type\": \"SHORT\",\n"
        "        }"
    )
    content, ok = sub_exact(
        old_ratio_risk, new_ratio_risk, content, "ratio spread max_risk fix"
    )
    if ok:
        changes.append("Ratio spread: max_risk = (wing_width - credit) * LOT_SIZE")

    # Backspread: add premium stop-loss check
    old_backspread_stop = (
        "        elif strategy == config.STRAT_BACKSPREAD:\n"
        "            if self.dm.spot is None:\n"
        "                return False\n"
        "            trend = position.trend_direction\n"
        "            if trend >= 0:\n"
        "                stop_level = position.entry_spot * (\n"
        "                    1 - config.BACKSPREAD_STOP_MOVE_PCT\n"
        "                )\n"
        "                if self.dm.spot < stop_level:\n"
        "                    await self._close_position(\n"
        "                        position,\n"
        "                        config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                    )\n"
        "                    return True\n"
        "            else:\n"
        "                stop_level = position.entry_spot * (\n"
        "                    1 + config.BACKSPREAD_STOP_MOVE_PCT\n"
        "                )\n"
        "                if self.dm.spot > stop_level:\n"
        "                    await self._close_position(\n"
        "                        position,\n"
        "                        config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                    )\n"
        "                    return True"
    )
    new_backspread_stop = (
        "        elif strategy == config.STRAT_BACKSPREAD:\n"
        "            if self.dm.spot is None:\n"
        "                return False\n"
        "            # Backspread premium stop: check debit threshold\n"
        "            # FIRST. During an IV crush, spot may barely move\n"
        "            # while the position bleeds premium. The spot-based\n"
        "            # stop alone misses this scenario entirely.\n"
        "            if position.stop_loss and position.stop_loss > 0:\n"
        "                current_val = self._get_position_value(position)\n"
        "                if current_val <= position.stop_loss:\n"
        "                    logger.info(\n"
        "                        f\"Backspread premium stop: \"\n"
        "                        f\"val={current_val:.2f} \"\n"
        "                        f\"stop={position.stop_loss:.2f}\"\n"
        "                    )\n"
        "                    await self._close_position(\n"
        "                        position,\n"
        "                        config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                    )\n"
        "                    return True\n"
        "            trend = position.trend_direction\n"
        "            if trend >= 0:\n"
        "                stop_level = position.entry_spot * (\n"
        "                    1 - config.BACKSPREAD_STOP_MOVE_PCT\n"
        "                )\n"
        "                if self.dm.spot < stop_level:\n"
        "                    await self._close_position(\n"
        "                        position,\n"
        "                        config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                    )\n"
        "                    return True\n"
        "            else:\n"
        "                stop_level = position.entry_spot * (\n"
        "                    1 + config.BACKSPREAD_STOP_MOVE_PCT\n"
        "                )\n"
        "                if self.dm.spot > stop_level:\n"
        "                    await self._close_position(\n"
        "                        position,\n"
        "                        config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                    )\n"
        "                    return True"
    )
    content, ok = sub_exact(
        old_backspread_stop, new_backspread_stop, content, "backspread premium stop"
    )
    if ok:
        changes.append("Backspread: premium stop-loss added (IV crush protection)")

    # P&L fallback: use get_mark_price() instead of entry_price
    old_pnl_fallback = (
        "            if exit_price == 0 and not is_expired_worthless:\n"
        "                # PATCH: prefer bid/ask midpoint over raw ltp when\n"
        "                # falling back (leg was never actually closed with\n"
        "                # a real fill price).\n"
        "                fallback_opt = (\n"
        "                    expiry_chain\n"
        "                    .get(leg.strike, {})\n"
        "                    .get(leg.option_type, {})\n"
        "                )\n"
        "                fb_bid = fallback_opt.get(\"bid\", 0)\n"
        "                fb_ask = fallback_opt.get(\"ask\", 0)\n"
        "                if fb_bid > 0 and fb_ask > 0:\n"
        "                    exit_price = (fb_bid + fb_ask) / 2.0\n"
        "                else:\n"
        "                    exit_price = fallback_opt.get(\"ltp\", 0)\n"
        "            if exit_price == 0 and not is_expired_worthless:\n"
        "                exit_price = leg.entry_price"
    )
    new_pnl_fallback = (
        "            if exit_price == 0 and not is_expired_worthless:\n"
        "                # Use staleness-aware mark price as fallback.\n"
        "                # The old fallback (entry_price) recorded zero P&L\n"
        "                # for a failed exit on a deep-ITM leg, preventing\n"
        "                # CB L2/L3 from firing when they were needed most.\n"
        "                fallback_opt = (\n"
        "                    expiry_chain\n"
        "                    .get(leg.strike, {})\n"
        "                    .get(leg.option_type, {})\n"
        "                )\n"
        "                _mark = self.dm.get_mark_price(\n"
        "                    fallback_opt,\n"
        "                    fallback=0.0,\n"
        "                )\n"
        "                if _mark > 0:\n"
        "                    exit_price = _mark\n"
        "            if exit_price == 0 and not is_expired_worthless:\n"
        "                # Last resort: use entry_price only if mark is\n"
        "                # also unavailable (e.g. position not in chain).\n"
        "                exit_price = leg.entry_price"
    )
    content, ok = sub_exact(
        old_pnl_fallback, new_pnl_fallback, content, "P&L fallback get_mark_price"
    )
    if ok:
        changes.append("P&L fallback: uses get_mark_price() instead of entry_price")

    # SE-T01: Route _get_position_value through get_mark_price()
    old_get_value = (
        "    def _get_position_value(\n"
        "        self, position: Position\n"
        "    ) -> float:\n"
        "        total        = 0.0\n"
        "        expiry_chain = self.dm.get_chain_for_expiry(\n"
        "            position.expiry_date\n"
        "        )\n"
        "        for leg in position.legs:\n"
        "            opt_data = (\n"
        "                expiry_chain\n"
        "                .get(leg.strike, {})\n"
        "                .get(leg.option_type, {})\n"
        "            )\n"
        "            # PATCH: prefer bid/ask midpoint over raw ltp —\n"
        "            # avoids a stale single-print ltp driving stop/target\n"
        "            # decisions for long straddle/strangle/backspread/etc.\n"
        "            bid = opt_data.get(\"bid\", 0)\n"
        "            ask = opt_data.get(\"ask\", 0)\n"
        "            ltp = opt_data.get(\"ltp\", 0)\n"
        "            if bid > 0 and ask > 0:\n"
        "                mark = (bid + ask) / 2.0\n"
        "            elif ltp > 0:\n"
        "                mark = ltp\n"
        "            else:\n"
        "                mark = leg.entry_price\n"
        "            total += mark * leg.qty\n"
        "        return total"
    )
    new_get_value = (
        "    def _get_position_value(\n"
        "        self, position: Position\n"
        "    ) -> float:\n"
        "        # SE-T01: use staleness-aware get_mark_price() so\n"
        "        # profit-target and stop-loss decisions use the same\n"
        "        # freshness logic as the fast P&L monitor.\n"
        "        total        = 0.0\n"
        "        expiry_chain = self.dm.get_chain_for_expiry(\n"
        "            position.expiry_date\n"
        "        )\n"
        "        for leg in position.legs:\n"
        "            opt_data = (\n"
        "                expiry_chain\n"
        "                .get(leg.strike, {})\n"
        "                .get(leg.option_type, {})\n"
        "            )\n"
        "            mark = self.dm.get_mark_price(\n"
        "                opt_data, fallback=leg.entry_price\n"
        "            )\n"
        "            total += mark * leg.qty\n"
        "        return total"
    )
    content, ok = sub_exact(
        old_get_value, new_get_value, content, "SE-T01 _get_position_value"
    )
    if ok:
        changes.append("SE-T01: _get_position_value uses get_mark_price()")

    # SE-T01: Route _get_position_current_premium through get_mark_price()
    old_get_premium = (
        "    def _get_position_current_premium(\n"
        "        self, position: Position\n"
        "    ) -> float:\n"
        "        net          = 0.0\n"
        "        expiry_chain = self.dm.get_chain_for_expiry(\n"
        "            position.expiry_date\n"
        "        )\n"
        "        for leg in position.legs:\n"
        "            opt_data = (\n"
        "                expiry_chain\n"
        "                .get(leg.strike, {})\n"
        "                .get(leg.option_type, {})\n"
        "            )\n"
        "            # PATCH: prefer bid/ask midpoint over raw ltp —\n"
        "            # profit-target/stop-loss decisions should not be\n"
        "            # driven by a stale single-print ltp.\n"
        "            bid = opt_data.get(\"bid\", 0)\n"
        "            ask = opt_data.get(\"ask\", 0)\n"
        "            ltp = opt_data.get(\"ltp\", 0)\n"
        "            if bid > 0 and ask > 0:\n"
        "                mark = (bid + ask) / 2.0\n"
        "            elif ltp > 0:\n"
        "                mark = ltp\n"
        "            else:\n"
        "                mark = leg.entry_price\n"
        "            if leg.action == \"SELL\":\n"
        "                net += mark * leg.qty\n"
        "            else:\n"
        "                net -= mark * leg.qty\n"
        "        return net"
    )
    new_get_premium = (
        "    def _get_position_current_premium(\n"
        "        self, position: Position\n"
        "    ) -> float:\n"
        "        # SE-T01: use staleness-aware get_mark_price() so\n"
        "        # credit-strategy stop/target decisions use the same\n"
        "        # freshness logic as the fast P&L monitor.\n"
        "        net          = 0.0\n"
        "        expiry_chain = self.dm.get_chain_for_expiry(\n"
        "            position.expiry_date\n"
        "        )\n"
        "        for leg in position.legs:\n"
        "            opt_data = (\n"
        "                expiry_chain\n"
        "                .get(leg.strike, {})\n"
        "                .get(leg.option_type, {})\n"
        "            )\n"
        "            mark = self.dm.get_mark_price(\n"
        "                opt_data, fallback=leg.entry_price\n"
        "            )\n"
        "            if leg.action == \"SELL\":\n"
        "                net += mark * leg.qty\n"
        "            else:\n"
        "                net -= mark * leg.qty\n"
        "        return net"
    )
    content, ok = sub_exact(
        old_get_premium, new_get_premium, content, "SE-T01 _get_position_current_premium"
    )
    if ok:
        changes.append("SE-T01: _get_position_current_premium uses get_mark_price()")

    # SE-T04: Use IST date in _generate_order_tag
    old_order_tag = (
        "        raw = (\n"
        "            f\"{trade_id[:12]}-\"\n"
        "            f\"{instrument_key[-8:]}-\"\n"
        "            f\"{action}-\"\n"
        "            f\"{leg_index}-\"\n"
        "            f\"{date.today().isoformat()}\"\n"
        "        )"
    )
    new_order_tag = (
        "        # SE-T04: use IST date, not server-local date.\n"
        "        # On UTC servers, date.today() rolls at 05:30 IST,\n"
        "        # causing tag mismatches and wrong session cleanup.\n"
        "        _ist_date = datetime.now(\n"
        "            self._IST\n"
        "        ).date().isoformat()\n"
        "        raw = (\n"
        "            f\"{trade_id[:12]}-\"\n"
        "            f\"{instrument_key[-8:]}-\"\n"
        "            f\"{action}-\"\n"
        "            f\"{leg_index}-\"\n"
        "            f\"{_ist_date}\"\n"
        "        )"
    )
    content, ok = sub_exact(
        old_order_tag, new_order_tag, content, "SE-T04 IST date in order tag"
    )
    if ok:
        changes.append("SE-T04: order tags use IST date not server-local date")

    # SE-T04: Use IST date in startup_cancel_stale_orders session_date query
    old_stale_query = (
        "                AND   session_date < ?\n"
        "            \"\"\", (date.today().isoformat(),))"
    )
    new_stale_query = (
        "                AND   session_date < ?\n"
        "            \"\"\", (datetime.now(self._IST).date().isoformat(),))"
    )
    content, ok = sub_exact(
        old_stale_query, new_stale_query, content, "SE-T04 IST date stale query"
    )
    if ok:
        changes.append("SE-T04: startup stale-order query uses IST date")

    # SE-T06: Add Greeks staleness warning in _check_greeks_limits
    old_greeks_limits = (
        "    async def _check_greeks_limits(self) -> None:\n"
        "        \"\"\"Check Greeks. Skip when no positions open.\"\"\"\n"
        "        if not self.open_positions:\n"
        "            return\n"
        "        regime = self.re.confirmed_regime\n"
        "        limits = config.GREEKS_LIMITS.get(regime, {})\n"
        "        greeks = self._get_portfolio_greeks()"
    )
    new_greeks_limits = (
        "    async def _check_greeks_limits(self) -> None:\n"
        "        \"\"\"Check Greeks. Skip when no positions open.\"\"\"\n"
        "        if not self.open_positions:\n"
        "            return\n"
        "        # SE-T06: warn when leg Greeks are stale.\n"
        "        # Greeks are set at entry and updated by WS. Near expiry,\n"
        "        # gamma changes rapidly; a 30-min-old reading can be off\n"
        "        # by 2-3x. We cannot recompute without a live model, but\n"
        "        # we can warn so the operator knows the limits check may\n"
        "        # be operating on stale data.\n"
        "        _now_ist = datetime.now(self._IST)\n"
        "        for _pos in self.open_positions:\n"
        "            for _leg in _pos.legs:\n"
        "                _ws_ts = None\n"
        "                _exp_chain = self.dm.get_chain_for_expiry(\n"
        "                    _pos.expiry_date\n"
        "                )\n"
        "                _opt = (\n"
        "                    _exp_chain\n"
        "                    .get(_leg.strike, {})\n"
        "                    .get(_leg.option_type, {})\n"
        "                )\n"
        "                _ws_ts_str = _opt.get(\"_ws_ts\")\n"
        "                if _ws_ts_str:\n"
        "                    try:\n"
        "                        _ws_ts = datetime.fromisoformat(_ws_ts_str)\n"
        "                        if _ws_ts.tzinfo is None:\n"
        "                            _ws_ts = self._IST.localize(_ws_ts)\n"
        "                        _age = (\n"
        "                            _now_ist - _ws_ts\n"
        "                        ).total_seconds()\n"
        "                        if _age > 1800:  # 30 minutes\n"
        "                            logger.warning(\n"
        "                                f\"SE-T06: Greeks stale \"\n"
        "                                f\"{_leg.option_type} \"\n"
        "                                f\"{_leg.strike} \"\n"
        "                                f\"age={_age:.0f}s — \"\n"
        "                                f\"limits check may be inaccurate\"\n"
        "                            )\n"
        "                    except Exception:\n"
        "                        pass\n"
        "        regime = self.re.confirmed_regime\n"
        "        limits = config.GREEKS_LIMITS.get(regime, {})\n"
        "        greeks = self._get_portfolio_greeks()"
    )
    content, ok = sub_exact(
        old_greeks_limits, new_greeks_limits, content, "SE-T06 Greeks staleness warning"
    )
    if ok:
        changes.append("SE-T06: Greeks staleness warning added to _check_greeks_limits")

    # ATR contraction: replace strict monotonic with trend comparison
    old_atr_logic = (
        "            if len(atrs) < lookback:\n"
        "                result = False\n"
        "            else:\n"
        "                result = all(\n"
        "                    atrs[i] < atrs[i - 1]\n"
        "                    for i in range(1, len(atrs))\n"
        "                )"
    )
    new_atr_logic = (
        "            if len(atrs) < lookback:\n"
        "                result = False\n"
        "            else:\n"
        "                # ATR contraction fix: strict monotonic decrease\n"
        "                # (all(atrs[i] < atrs[i-1])) is statistically rare\n"
        "                # even in genuine contraction — micro-fluctuations\n"
        "                # break the chain. Compare current ATR against the\n"
        "                # oldest in the window: if the trend is down, ATR\n"
        "                # is contracting regardless of intrabar noise.\n"
        "                result = atrs[-1] < atrs[0]"
    )
    content, ok = sub_exact(
        old_atr_logic, new_atr_logic, content, "ATR contraction fix"
    )
    if ok:
        changes.append("ATR contraction: uses atrs[-1] < atrs[0] instead of strict monotonic")

    # MN-T04: Add idempotency guard to CB Level 5
    old_cb5 = (
        "        # LEVEL 5 — Absolute VIX threshold\n"
        "        # FIX P8: use absolute VIX level not % change\n"
        "        if (\n"
        "            self.dm.vix is not None\n"
        "            and self.dm.vix >= config.CB_LEVEL_5_VIX_ABSOLUTE\n"
        "        ):\n"
        "            logger.critical(\n"
        "                f\"CB L5: VIX={self.dm.vix:.1f} >= \"\n"
        "                f\"{config.CB_LEVEL_5_VIX_ABSOLUTE}\"\n"
        "            )\n"
        "            self._log_circuit_breaker(\n"
        "                5,\n"
        "                f\"vix_absolute={self.dm.vix:.1f}\",\n"
        "                config.CB_LEVEL_5_ACTION,\n"
        "            )\n"
        "            self.re.previous_regime  = (\n"
        "                self.re.confirmed_regime\n"
        "            )\n"
        "            self.re.confirmed_regime = (\n"
        "                config.REGIME_STRONG_BUY\n"
        "            )\n"
        "            self.re.regime_changed   = True"
    )
    new_cb5 = (
        "        # LEVEL 5 — Absolute VIX threshold\n"
        "        # MN-T04: guard with cb_level_5_active so the fast\n"
        "        # monitor (running every second) does not emit a new\n"
        "        # regime-change event on every iteration while VIX\n"
        "        # stays elevated. Without this guard, _handle_regime_\n"
        "        # transition fires every second, closing positions\n"
        "        # repeatedly and generating excessive API activity.\n"
        "        if (\n"
        "            self.dm.vix is not None\n"
        "            and self.dm.vix >= config.CB_LEVEL_5_VIX_ABSOLUTE\n"
        "        ):\n"
        "            if not getattr(self, \"cb_level_5_active\", False):\n"
        "                logger.critical(\n"
        "                    f\"CB L5: VIX={self.dm.vix:.1f} >= \"\n"
        "                    f\"{config.CB_LEVEL_5_VIX_ABSOLUTE}\"\n"
        "                )\n"
        "                self._log_circuit_breaker(\n"
        "                    5,\n"
        "                    f\"vix_absolute={self.dm.vix:.1f}\",\n"
        "                    config.CB_LEVEL_5_ACTION,\n"
        "                )\n"
        "                self.re.previous_regime  = (\n"
        "                    self.re.confirmed_regime\n"
        "                )\n"
        "                self.re.confirmed_regime = (\n"
        "                    config.REGIME_STRONG_BUY\n"
        "                )\n"
        "                self.re.regime_changed   = True\n"
        "                self.cb_level_5_active   = True\n"
        "                self._save_capital_state()\n"
        "        else:\n"
        "            # Reset when VIX drops back below threshold\n"
        "            if getattr(self, \"cb_level_5_active\", False):\n"
        "                logger.info(\n"
        "                    f\"CB L5: VIX={self.dm.vix:.1f} below \"\n"
        "                    f\"threshold — resetting\"\n"
        "                )\n"
        "                self.cb_level_5_active = False"
    )
    content, ok = sub_exact(
        old_cb5, new_cb5, content, "MN-T04 CB L5 idempotency"
    )
    if ok:
        changes.append("MN-T04: CB Level 5 is now idempotent (fires once, resets on recovery)")

    # Add cb_level_5_active to __init__
    old_cb_init = (
        "        self.cb_level_1_count:  int  = 0\n"
        "        self.cb_level_2_active: bool = False\n"
        "        self.cb_level_3_active: bool = False\n"
        "        self.cb_level_4_active: bool = False"
    )
    new_cb_init = (
        "        self.cb_level_1_count:  int  = 0\n"
        "        self.cb_level_2_active: bool = False\n"
        "        self.cb_level_3_active: bool = False\n"
        "        self.cb_level_4_active: bool = False\n"
        "        # MN-T04: CB L5 idempotency flag\n"
        "        self.cb_level_5_active: bool = False"
    )
    content, ok = sub_exact(
        old_cb_init, new_cb_init, content, "MN-T04 cb_level_5_active init"
    )
    if ok:
        changes.append("MN-T04: cb_level_5_active added to __init__")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# main.py
# ─────────────────────────────────────────────────────────────────────

def patch_main(content):
    changes = []

    # MN-T01: Kill switch must not skip position monitoring.
    # The current code sleeps 60s and continues, leaving any
    # residual open positions (from failed SE-N01 exits) unmonitored.
    old_kill_switch = (
        "            if se.kill_switch_active:\n"
        "                logger.critical(\n"
        "                    \"Strategy kill switch ACTIVE — \"\n"
        "                    \"no new trades, monitoring continues\"\n"
        "                )\n"
        "                await se.cancel_all_open_orders(\n"
        "                    context=\"KILL_SWITCH_SWEEP\"\n"
        "                )\n"
        "                # Do NOT break — engine keeps running\n"
        "                await asyncio.sleep(60)\n"
        "                continue"
    )
    new_kill_switch = (
        "            if se.kill_switch_active:\n"
        "                logger.critical(\n"
        "                    \"Strategy kill switch ACTIVE — \"\n"
        "                    \"no new trades, monitoring continues\"\n"
        "                )\n"
        "                await se.cancel_all_open_orders(\n"
        "                    context=\"KILL_SWITCH_SWEEP\"\n"
        "                )\n"
        "                # MN-T01: do NOT skip position monitoring.\n"
        "                # Failed SE-N01 exits leave positions OPEN;\n"
        "                # they need stop-loss checks and retry attempts\n"
        "                # even while the kill switch is active.\n"
        "                # Run the fast monitor then sleep briefly.\n"
        "                if se.open_positions and dm.spot is not None:\n"
        "                    try:\n"
        "                        await se._update_all_pnls()\n"
        "                        await se._monitor_all_positions()\n"
        "                    except Exception as _ks_e:\n"
        "                        logger.error(\n"
        "                            f\"Kill-switch monitor error: {_ks_e}\"\n"
        "                        )\n"
        "                await asyncio.sleep(5)\n"
        "                continue"
    )
    content, ok = sub_exact(
        old_kill_switch, new_kill_switch, content, "MN-T01 kill switch monitoring"
    )
    if ok:
        changes.append("MN-T01: kill switch no longer skips position monitoring")

    # MN-T02: EOD marked complete only when open_positions is empty
    old_eod_done = (
        "                    await _end_of_day(se, dm)\n"
        "                    eod_done_today = True\n"
        "                    logger.info(\n"
        "                        \"EOD complete — engine monitoring, \"\n"
        "                        \"waiting for next trading day\"\n"
        "                    )"
    )
    new_eod_done = (
        "                    await _end_of_day(se, dm)\n"
        "                    # MN-T02: only mark EOD complete when all\n"
        "                    # positions are confirmed closed. Failed\n"
        "                    # SE-N01 exits keep positions OPEN; marking\n"
        "                    # eod_done_today=True would reduce them to\n"
        "                    # once-per-minute monitoring.\n"
        "                    if not se.open_positions:\n"
        "                        eod_done_today = True\n"
        "                        logger.info(\n"
        "                            \"EOD complete — all positions closed, \"\n"
        "                            \"waiting for next trading day\"\n"
        "                        )\n"
        "                    else:\n"
        "                        logger.warning(\n"
        "                            f\"EOD: {len(se.open_positions)} \"\n"
        "                            f\"position(s) still open after \"\n"
        "                            f\"_end_of_day — will retry\"\n"
        "                        )"
    )
    content, ok = sub_exact(
        old_eod_done, new_eod_done, content, "MN-T02 EOD completion guard"
    )
    if ok:
        changes.append("MN-T02: EOD only marked complete when open_positions is empty")

    # MN-T03: Expiry-day EOD same fix
    old_expiry_eod = (
        "                    await _expiry_day_close_all(se)\n"
        "                    await _end_of_day(se, dm)\n"
        "                    eod_done_today = True   # PATCH\n"
        "                    logger.info(\n"
        "                        \"Expiry day EOD complete — \"\n"
        "                        \"engine monitoring\"\n"
        "                    )\n"
        "                    await asyncio.sleep(300)\n"
        "                    continue"
    )
    new_expiry_eod = (
        "                    await _expiry_day_close_all(se)\n"
        "                    await _end_of_day(se, dm)\n"
        "                    # MN-T03: same guard as normal EOD.\n"
        "                    # Do not enter the 300s sleep while\n"
        "                    # positions remain open.\n"
        "                    if not se.open_positions:\n"
        "                        eod_done_today = True\n"
        "                        logger.info(\n"
        "                            \"Expiry day EOD complete — \"\n"
        "                            \"engine monitoring\"\n"
        "                        )\n"
        "                        await asyncio.sleep(300)\n"
        "                        continue\n"
        "                    else:\n"
        "                        logger.warning(\n"
        "                            f\"Expiry EOD: \"\n"
        "                            f\"{len(se.open_positions)} \"\n"
        "                            f\"position(s) still open — \"\n"
        "                            f\"will retry close\"\n"
        "                        )"
    )
    content, ok = sub_exact(
        old_expiry_eod, new_expiry_eod, content, "MN-T03 expiry EOD guard"
    )
    if ok:
        changes.append("MN-T03: expiry-day EOD only marked complete when positions closed")

    # MN-T05: Use _spot_changed in the completion decision
    old_refresh_complete = (
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
    new_refresh_complete = (
        "                    # MN-T05: require that spot was actually\n"
        "                    # updated this cycle OR that this is the\n"
        "                    # first cycle after startup (no prev value).\n"
        "                    # Pre-existing restored state satisfies\n"
        "                    # _spot_exists but not _spot_changed.\n"
        "                    _is_first_cycle = _cycle_start_spot is None\n"
        "                    _refresh_valid = (\n"
        "                        _spot_exists\n"
        "                        and _chain_exists\n"
        "                        and (_spot_changed or _is_first_cycle)\n"
        "                    )\n"
        "                    if _refresh_valid:\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = True\n"
        "                        logger.info(\n"
        "                            \"Data refresh cycle complete\"\n"
        "                        )\n"
        "                    elif _spot_exists and _chain_exists:\n"
        "                        # Data exists but spot didn't change —\n"
        "                        # could be a genuine flat market or a\n"
        "                        # stale API response. Update timestamp\n"
        "                        # to avoid spin-retry but mark incomplete\n"
        "                        # so regime uses previous confirmed data.\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = False\n"
        "                        logger.debug(\n"
        "                            \"Data refresh: spot unchanged \"\n"
        "                            \"— marking incomplete\"\n"
        "                        )\n"
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
    content, ok = sub_exact(
        old_refresh_complete, new_refresh_complete, content, "MN-T05 refresh validation"
    )
    if ok:
        changes.append("MN-T05: data refresh only marked complete when spot actually updated")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Round-4 audit fixes for the NIFTY trading engine."
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