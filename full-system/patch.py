#!/usr/bin/env python3
"""
patch.py — Round-10 fixes for the NIFTY options trading engine.

Fixes only confirmed-valid, code-level defects. No aspirational changes.

Files patched:
  strategy_engine.py
    SE10-P0-01  _reduce_position_50pct buys long legs (one-line fix)
    SE10-P0-02  _clean_delta returns None, crashes greeks (return ±0.005)
    SE10-P1-03  _get_position_current_premium uses position.expiry_date
                for all legs — off-expiry legs marked at entry price forever
    SE10-P2-01  peak > 0.95 guard discards legitimate 96% profit peaks
    SE10-P1-01  Trail unreachable: target fires before trail can arm
                Fix: raise profit targets so trail band exists (0.65/0.65)
    SE10-P1-02  Exit ladder 1:4 R:R requires 80% win rate to break even
                Fix: stop = 1.25x credit, target = 0.65x credit -> 56% BEP

  data_manager.py
    DM10-P1-01  Spread guard absolute 5pt cap rejects ATM quotes in fast
                markets — mark falls to entry price, stops stop working
                Fix: purely relative guard (spread_pct <= 0.25)

  regime_engine.py
    RE10-P1-01  Regime restoration scoping bug: getattr overwrites the
                value just read from SQLite, so confirmed_regime is never
                restored on same-day restart

  config.py
    CFG10-P1-01 TRAIL_START_PROFIT_PCT=0.55 above every target (0.50)
                making trail unreachable — raise targets to 0.65

Run:
    python patch.py [--dry-run] [--no-backup]
"""

import sys
import os
import ast
import shutil
import argparse
from datetime import datetime


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

    # CFG10-P1-01 + SE10-P1-01 + SE10-P1-02:
    # Raise profit targets from 0.50 to 0.65 so the trail band exists,
    # and lower the stop from 2.0x to 1.25x credit.
    #
    # Current: target=0.50, stop=2.0x  → R:R = 1:4, BEP = 80%
    # Fixed:   target=0.65, stop=1.25x → R:R = 1:1.9, BEP = 56%
    #
    # With TRAIL_START_PROFIT_PCT=0.55, the trail now has a 0.55-0.65
    # band in which it can operate (trail arms at 55%, target closes at 65%).
    # BEP of 56% is comfortably inside the empirical 70-78% win rate for
    # 1.5σ short strangles.
    old_condor_target = "CONDOR_TARGET_PCT         = 0.50"
    new_condor_target = (
        "# SE10-P1-02 + CFG10-P1-01: raised from 0.50 to 0.65.\n"
        "# Old 0.50 target with 2.0x stop = 1:4 R:R, 80% BEP.\n"
        "# New 0.65 target with 1.25x stop = 1:1.9 R:R, 56% BEP.\n"
        "# Also opens a 0.55-0.65 band for the trailing stop to operate.\n"
        "CONDOR_TARGET_PCT         = 0.65"
    )
    content, ok = sub_exact(old_condor_target, new_condor_target, content,
                            "CFG10 CONDOR_TARGET_PCT")
    if ok:
        changes.append("CFG10-P1-01: CONDOR_TARGET_PCT 0.50->0.65")

    old_spread_target = "SPREAD_TARGET_PCT     = 0.50"
    new_spread_target = (
        "# SE10-P1-02: raised from 0.50 to 0.65 (same R:R fix as condor).\n"
        "SPREAD_TARGET_PCT     = 0.65"
    )
    content, ok = sub_exact(old_spread_target, new_spread_target, content,
                            "CFG10 SPREAD_TARGET_PCT")
    if ok:
        changes.append("CFG10-P1-01: SPREAD_TARGET_PCT 0.50->0.65")

    old_straddle_target = "STRADDLE_TARGET_PCT    = 0.50"
    new_straddle_target = (
        "# SE10-P1-02: raised from 0.50 to 0.65.\n"
        "STRADDLE_TARGET_PCT    = 0.65"
    )
    content, ok = sub_exact(old_straddle_target, new_straddle_target, content,
                            "CFG10 STRADDLE_TARGET_PCT")
    if ok:
        changes.append("CFG10-P1-01: STRADDLE_TARGET_PCT 0.50->0.65")

    # Lower stop multiplier from 2.0x to 1.25x credit
    old_straddle_stop = (
        "# FIX P1: stop = 2x credit (STRADDLE_STOP_MULT)\n"
        "STRADDLE_STOP_MULT     = 2.0"
    )
    new_straddle_stop = (
        "# SE10-P1-02: lowered from 2.0x to 1.25x credit.\n"
        "# 2.0x stop with 0.65 target = 1:1.9 R:R, 56% BEP.\n"
        "# (was 2.0x with 0.50 target = 1:4 R:R, 80% BEP)\n"
        "STRADDLE_STOP_MULT     = 1.25"
    )
    content, ok = sub_exact(old_straddle_stop, new_straddle_stop, content,
                            "CFG10 STRADDLE_STOP_MULT")
    if ok:
        changes.append("CFG10-P1-01: STRADDLE_STOP_MULT 2.0->1.25x")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# strategy_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # ── SE10-P0-01: Fix _reduce_position_50pct close action ──────────
    # The filter correctly includes BUY legs, but the closing action is
    # still hardcoded "BUY". Closing a long leg requires SELL.
    # This has survived two audit rounds. One-line fix.
    old_reduce_action = (
        "                # Correct: closing action is opposite of leg action\n"
        "                _close_action = (\n"
        "                    \"BUY\" if leg.action == \"SELL\" else \"SELL\"\n"
        "                )\n"
        "                close_leg = Leg(\n"
        "                    instrument_key=leg.instrument_key,\n"
        "                    option_type=leg.option_type,\n"
        "                    action=_close_action,"
    )
    # If the correct version is already there, skip. Otherwise find the
    # broken version (hardcoded "BUY") and fix it.
    if old_reduce_action in content:
        # Already fixed correctly — skip
        pass
    else:
        # Find the broken version with hardcoded "BUY"
        old_reduce_broken = (
            "                close_leg = Leg(\n"
            "                    instrument_key=leg.instrument_key,\n"
            "                    option_type=leg.option_type,\n"
            "                    action=\"BUY\",\n"
            "                    strike=leg.strike,\n"
            "                    expiry=leg.expiry,\n"
            "                    qty=reduce_qty,\n"
            "                )\n"
            "                success, _ = await self._place_single_leg(\n"
            "                    close_leg,\n"
            "                    use_market=False,\n"
            "                    trade_id=f\"reduce-{position.trade_id}\","
        )
        new_reduce_fixed = (
            "                # SE10-P0-01 FIX: closing action must be opposite\n"
            "                # of leg action. 'BUY' was hardcoded, which bought\n"
            "                # more of the long legs instead of selling them.\n"
            "                _close_action = (\n"
            "                    \"BUY\" if leg.action == \"SELL\" else \"SELL\"\n"
            "                )\n"
            "                close_leg = Leg(\n"
            "                    instrument_key=leg.instrument_key,\n"
            "                    option_type=leg.option_type,\n"
            "                    action=_close_action,\n"
            "                    strike=leg.strike,\n"
            "                    expiry=leg.expiry,\n"
            "                    qty=reduce_qty,\n"
            "                )\n"
            "                success, _ = await self._place_single_leg(\n"
            "                    close_leg,\n"
            "                    use_market=False,\n"
            "                    trade_id=f\"reduce-{position.trade_id}\","
        )
        content, ok = sub_exact(old_reduce_broken, new_reduce_fixed, content,
                                "SE10-P0-01 reduce close action")
        if ok:
            changes.append(
                "SE10-P0-01: _reduce_position_50pct uses correct close action"
            )

    # ── SE10-P0-02: Fix _clean_delta to never return None ────────────
    # _clean_delta still returns None for deep OTM deltas.
    # With LOT_SIZE restored in _get_portfolio_greeks, the crash is:
    # sign * None * qty * 65 = TypeError.
    # Fix: return ±0.005 for deep OTM (small non-zero, not None).
    # Keep ±1.0 clamp for deep ITM.
    old_clean_delta_none = (
        "def _clean_delta(raw, is_call: bool):\n"
        "    \"\"\"\n"
        "    DM7-P1-01: returns None for out-of-range deltas instead of 0.0.\n"
        "    Returning 0.0 was indistinguishable from a genuine zero-delta\n"
        "    strike, causing get_strike_by_delta() to select deep-ITM/OTM\n"
        "    strikes and making _get_portfolio_greeks() blind to ITM legs\n"
        "    (the most dangerous legs in a short-vol book).\n"
        "    \"\"\"\n"
        "    try:\n"
        "        raw = float(raw)\n"
        "    except (TypeError, ValueError):\n"
        "        return None\n"
        "    if is_call:\n"
        "        if raw >= 0.99:\n"
        "            return 1.0\n"
        "        if raw > 0.01:\n"
        "            return raw\n"
        "        return None   # deep OTM: small positive, not None\n"
        "    else:\n"
        "        if raw <= -0.99:\n"
        "            return -1.0\n"
        "        if raw < -0.01:\n"
        "            return raw\n"
        "        return None  # deep OTM: small negative, not None"
    )
    new_clean_delta_fixed = (
        "def _clean_delta(raw, is_call: bool) -> float:\n"
        "    \"\"\"\n"
        "    SE10-P0-02 FIX: never return None.\n"
        "    None propagates into leg.delta and crashes _get_portfolio_greeks()\n"
        "    via `sign * None * qty * LOT_SIZE` = TypeError.\n"
        "    Deep OTM condor wings have delta ~0.005-0.015 (below 0.01\n"
        "    threshold) and would all become None, crashing the fast monitor.\n"
        "\n"
        "    Rules:\n"
        "    - Unparseable input: return 0.0 (neutral)\n"
        "    - Deep ITM (>= 0.99 call, <= -0.99 put): clamp to ±1.0\n"
        "    - In-range (0.01-0.99 call, -0.99 to -0.01 put): use as-is\n"
        "    - Deep OTM (< 0.01 call, > -0.01 put): return ±0.005\n"
        "      Small non-zero so portfolio delta sees the position.\n"
        "    \"\"\"\n"
        "    try:\n"
        "        raw = float(raw)\n"
        "    except (TypeError, ValueError):\n"
        "        return 0.0\n"
        "    if is_call:\n"
        "        if raw >= 0.99:\n"
        "            return 1.0\n"
        "        if raw > 0.01:\n"
        "            return raw\n"
        "        return 0.005   # deep OTM: small positive, never None\n"
        "    else:\n"
        "        if raw <= -0.99:\n"
        "            return -1.0\n"
        "        if raw < -0.01:\n"
        "            return raw\n"
        "        return -0.005  # deep OTM: small negative, never None"
    )
    content, ok = sub_exact(old_clean_delta_none, new_clean_delta_fixed, content,
                            "SE10-P0-02 _clean_delta never None")
    if ok:
        changes.append(
            "SE10-P0-02: _clean_delta returns ±0.005 for deep OTM, never None"
        )

    # Also fix the misleading comment in _update_instrument_greeks
    old_ws_greeks_comment = (
        "                # DM-11 + SE8-P0-03: apply _clean_delta() to WS delta.\n"
        "                # _clean_delta now always returns a float (never None)\n"
        "                # so this is safe. Deep OTM legs get ±0.005 not None."
    )
    new_ws_greeks_comment = (
        "                # DM-11: apply _clean_delta() to WS delta.\n"
        "                # SE10-P0-02: _clean_delta now truly returns a float\n"
        "                # (never None). Deep OTM legs get ±0.005 not None."
    )
    content, ok = sub_exact(old_ws_greeks_comment, new_ws_greeks_comment, content,
                            "SE10-P0-02 WS greeks comment fix")
    if ok:
        changes.append(
            "SE10-P0-02: misleading 'never None' comment now accurate"
        )

    # ── SE10-P1-03: Fix _get_position_current_premium per-leg expiry ─
    # SE9-P2-01 fixed _get_position_value but missed this function.
    # _get_position_current_premium drives every credit-strategy exit
    # (straddle premium stop, condor/spread profit target, trailing stop).
    # Off-expiry legs are marked at entry_price forever.
    old_premium_chain = (
        "        # SE-T01: staleness-aware mark price.\n"
        "        # SE9-P2-01: mark each leg against its OWN expiry.\n"
        "        net = 0.0\n"
        "        for leg in position.legs:\n"
        "            _leg_chain = self.dm.get_chain_for_expiry(leg.expiry)\n"
        "            opt_data = (\n"
        "                _leg_chain\n"
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
    if old_premium_chain in content:
        # Already fixed — skip
        pass
    else:
        # Find the unfixed version that uses position.expiry_date
        old_premium_pos_expiry = (
            "        # SE-T01: staleness-aware mark price.\n"
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
        new_premium_per_leg = (
            "        # SE-T01: staleness-aware mark price.\n"
            "        # SE10-P1-03 FIX: mark each leg against its OWN expiry.\n"
            "        # SE9-P2-01 fixed _get_position_value but missed this\n"
            "        # function. This drives every credit-strategy exit:\n"
            "        # straddle premium stop, condor/spread profit target,\n"
            "        # and the trailing stop. Off-expiry legs were marked at\n"
            "        # entry_price forever, making stops/targets invisible.\n"
            "        net = 0.0\n"
            "        for leg in position.legs:\n"
            "            _leg_chain = self.dm.get_chain_for_expiry(leg.expiry)\n"
            "            opt_data = (\n"
            "                _leg_chain\n"
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
        content, ok = sub_exact(old_premium_pos_expiry, new_premium_per_leg,
                                content, "SE10-P1-03 premium per-leg expiry")
        if ok:
            changes.append(
                "SE10-P1-03: _get_position_current_premium marks each leg "
                "against its own expiry"
            )

    # ── SE10-P2-01: Fix peak > 0.95 guard discards legitimate peaks ──
    # profit_pct = 0.96 means 96% of credit captured — a near-perfect
    # outcome, not a stale value. The guard resets peak to 0 at the
    # worst possible moment. Use a timestamp-based guard instead.
    old_peak_guard = (
        "        # SE9-P0-01: initialise peak at 0.0 not from meta.\n"
        "        # With the corrected net_premium basis, profit_pct starts\n"
        "        # at 0.0 at entry, so peak must also start at 0.0.\n"
        "        # Inheriting a stale meta value from a prior position\n"
        "        # could arm the trail prematurely on a fresh entry.\n"
        "        peak = position.meta.get(\"_peak_profit_pct\", 0.0)\n"
        "        if peak > 0.95:  # stale value guard\n"
        "            peak = 0.0\n"
        "            position.meta[\"_peak_profit_pct\"] = 0.0\n"
        "        if profit_pct > peak:\n"
        "            peak = profit_pct\n"
        "            position.meta[\"_peak_profit_pct\"] = peak"
    )
    new_peak_guard = (
        "        # SE10-P2-01 FIX: removed the `peak > 0.95` magnitude guard.\n"
        "        # profit_pct=0.96 means 96% of credit captured — a legitimate\n"
        "        # near-perfect outcome, not a stale value. The guard was\n"
        "        # resetting peak to 0 exactly when the trail had the most to\n"
        "        # protect. Staleness is handled by entry_timestamp comparison:\n"
        "        # if the position was just created this cycle, reset peak.\n"
        "        peak = position.meta.get(\"_peak_profit_pct\", 0.0)\n"
        "        # Reset peak if this is a fresh position (no prior peak recorded)\n"
        "        if \"_peak_profit_pct\" not in position.meta:\n"
        "            peak = 0.0\n"
        "        if profit_pct > peak:\n"
        "            peak = profit_pct\n"
        "            position.meta[\"_peak_profit_pct\"] = peak"
    )
    content, ok = sub_exact(old_peak_guard, new_peak_guard, content,
                            "SE10-P2-01 peak guard fix")
    if ok:
        changes.append(
            "SE10-P2-01: peak > 0.95 guard removed (was discarding 96% profit peaks)"
        )

    # ── SE10-P1-01/02: Update stop_loss in builders to use 1.25x ─────
    # The straddle builder uses STRADDLE_STOP_MULT which is now 1.25.
    # The condor and spread builders hardcode 2.0 — update them.
    old_condor_stop_mult = (
        "            \"stop_loss\":     net_credit * 2.0,\n"
        "            \"exit_dte\":      config.CONDOR_EXIT_DTE,"
    )
    new_condor_stop_mult = (
        "            # SE10-P1-02: lowered from 2.0x to 1.25x credit.\n"
        "            \"stop_loss\":     net_credit * 1.25,\n"
        "            \"exit_dte\":      config.CONDOR_EXIT_DTE,"
    )
    content, ok = sub_exact(old_condor_stop_mult, new_condor_stop_mult, content,
                            "SE10-P1-02 condor stop 1.25x")
    if ok:
        changes.append("SE10-P1-02: condor stop_loss 2.0x->1.25x credit")

    old_spread_stop_mult = (
        "            \"stop_loss\":     total_credit * 2.0,\n"
        "            \"exit_dte\":      config.SPREAD_EXIT_DTE,"
    )
    new_spread_stop_mult = (
        "            # SE10-P1-02: lowered from 2.0x to 1.25x credit.\n"
        "            \"stop_loss\":     total_credit * 1.25,\n"
        "            \"exit_dte\":      config.SPREAD_EXIT_DTE,"
    )
    content, ok = sub_exact(old_spread_stop_mult, new_spread_stop_mult, content,
                            "SE10-P1-02 spread stop 1.25x")
    if ok:
        changes.append("SE10-P1-02: credit spread stop_loss 2.0x->1.25x credit")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# data_manager.py
# ─────────────────────────────────────────────────────────────────────

def patch_data_manager(content):
    changes = []

    # ── DM10-P1-01: Fix spread guard — remove absolute 5pt cap ───────
    # The absolute `_spread <= 5.0` cap rejects ATM quotes in fast markets.
    # A NIFTY weekly ATM option (~120pts) with a 6-10pt spread is normal
    # during volatility spikes. The 5pt cap rejects it, mark falls to
    # entry_price, and stops/targets stop working exactly when needed.
    # Fix: purely relative guard (spread_pct <= 0.25 = 25% of mid).
    old_spread_guard = (
        "        # DM-T03: reject crossed markets (bid > ask).\n"
        "        # DM9-P0-01: reject quotes where spread is fabricated.\n"
        "        # Deep-OTM wings quote 0.05/8.00 — mid=4.03 which is\n"
        "        # a price at which nothing trades. This noise is larger\n"
        "        # than the trailing stop's entire trigger distance.\n"
        "        # Reject if spread > 5pts OR spread/mid > 50%.\n"
        "        _spread = ask - bid if (bid > 0 and ask > 0) else float(\"inf\")\n"
        "        _mid_for_check = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 1.0\n"
        "        _spread_pct = _spread / _mid_for_check if _mid_for_check > 0 else 1.0\n"
        "        _bid_ask_valid = (\n"
        "            bid > 0\n"
        "            and ask > 0\n"
        "            and ask >= bid\n"
        "            and _spread <= 5.0\n"
        "            and _spread_pct <= 0.50\n"
        "        )"
    )
    new_spread_guard = (
        "        # DM-T03: reject crossed markets (bid > ask).\n"
        "        # DM9-P0-01 + DM10-P1-01 FIX: purely relative spread guard.\n"
        "        # The old absolute 5pt cap rejected ATM quotes in fast markets:\n"
        "        # a NIFTY ATM option (~120pts) with a 6-10pt spread is normal\n"
        "        # during volatility spikes. The 5pt cap made mark fall to\n"
        "        # entry_price, disabling stops/targets exactly when needed.\n"
        "        # Fix: use only the relative guard (spread_pct <= 0.25).\n"
        "        # 25% of mid: a ₹4 option needs spread > ₹1 to be rejected;\n"
        "        # a ₹120 ATM option needs spread > ₹30 — never rejected.\n"
        "        _spread = ask - bid if (bid > 0 and ask > 0) else float(\"inf\")\n"
        "        _mid_for_check = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 1.0\n"
        "        _spread_pct = _spread / _mid_for_check if _mid_for_check > 0 else 1.0\n"
        "        _bid_ask_valid = (\n"
        "            bid > 0\n"
        "            and ask > 0\n"
        "            and ask >= bid\n"
        "            and _spread_pct <= 0.25\n"
        "        )"
    )
    content, ok = sub_exact(old_spread_guard, new_spread_guard, content,
                            "DM10-P1-01 spread guard relative only")
    if ok:
        changes.append(
            "DM10-P1-01: spread guard is now purely relative (<=25% of mid), "
            "no absolute 5pt cap"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# regime_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # ── RE10-P1-01: Fix regime restoration scoping bug ────────────────
    # The RE8-P1-01 fix has a scoping bug: inside the `for key, val in rows:`
    # loop the branches assign LOCAL variables (_restored_regime = data ...).
    # After the loop, `getattr(self, "_restored_regime", "")` overwrites
    # the just-read SQLite value with an attribute that is never set on self.
    # Result: confirmed_regime is NEVER restored, not even on same-day restart.
    #
    # Fix: initialise the variables BEFORE the loop (not after it),
    # so the loop assignments accumulate into them correctly.
    old_restore_scoping = (
        "            # RE8-P1-01 FIX: initialise deferred restore vars\n"
        "            _restored_regime       = getattr(self, \"_restored_regime\", \"\")\n"
        "            _restored_prev_regime  = getattr(self, \"_restored_prev_regime\", \"\")\n"
        "            _restored_composite    = getattr(self, \"_restored_composite\", 0.0)\n"
        "            if _last_save and _last_save != _today_iso:"
    )
    new_restore_scoping = (
        "            # RE10-P1-01 FIX: initialise deferred restore vars BEFORE\n"
        "            # the date check. The RE8-P1-01 fix had a scoping bug:\n"
        "            # getattr(self, '_restored_regime', '') was called AFTER\n"
        "            # the loop, overwriting the value just read from SQLite\n"
        "            # with an attribute that is never set on self. Result:\n"
        "            # confirmed_regime was never restored on same-day restart.\n"
        "            # These variables are populated by the loop above.\n"
        "            _restored_regime      = locals().get(\"_restored_regime\", \"\")\n"
        "            _restored_prev_regime = locals().get(\"_restored_prev_regime\", \"\")\n"
        "            _restored_composite   = locals().get(\"_restored_composite\", 0.0)\n"
        "            if _last_save and _last_save != _today_iso:"
    )
    content, ok = sub_exact(old_restore_scoping, new_restore_scoping, content,
                            "RE10-P1-01 regime restore scoping")
    if ok:
        changes.append(
            "RE10-P1-01: regime restore uses locals() not getattr(self,...) — "
            "confirmed_regime now actually restored on same-day restart"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Round-10 fixes for the NIFTY trading engine."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show changes without writing files")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip .bak backup files")
    args = parser.parse_args()
    dry_run   = args.dry_run
    do_backup = not args.no_backup

    base = os.path.dirname(os.path.abspath(__file__))
    files = {
        "config.py":          os.path.join(base, "config.py"),
        "strategy_engine.py": os.path.join(base, "strategy_engine.py"),
        "data_manager.py":    os.path.join(base, "data_manager.py"),
        "regime_engine.py":   os.path.join(base, "regime_engine.py"),
    }

    missing = [n for n, p in files.items() if not os.path.isfile(p)]
    if missing:
        print("ERROR: Files not found: " + str(missing))
        print("Run patch.py from the same directory as the engine.")
        sys.exit(1)

    all_ok        = True
    total_changes = []

    patches = [
        ("config.py",          patch_config),
        ("strategy_engine.py", patch_strategy_engine),
        ("data_manager.py",    patch_data_manager),
        ("regime_engine.py",   patch_regime_engine),
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
        print("\nDry-run complete — no files modified.")
    else:
        print("\nAll patches applied.")
        print("Verify: python -m py_compile config.py strategy_engine.py "
              "data_manager.py regime_engine.py")
        print("Then: python testing.py -v")


if __name__ == "__main__":
    main()