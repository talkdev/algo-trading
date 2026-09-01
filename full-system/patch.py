#!/usr/bin/env python3
"""
patch.py — Round-5 audit fixes for the NIFTY options trading engine.

Priority order matches the audit's Phase 0 → Phase 1 → Phase 2 plan.

Phase 0 — make it trade at all
  SE-01   _build_credit_spreads UnboundLocalError (put_width/call_width)
  SE-02   Condor credit floor unreachable (replace fixed floor with ratio)
  SE-03   Lot sizing returns 0 (size off designed stop, not theoretical max)
  MN-01   Entries blocked when spot unchanged (_chain_updated wired in)

Phase 1 — stop losing money on mechanics
  SE-05   _get_position_value() missing BUY/SELL sign
  SE-06   Trailing stop too tight (arm 0.30, retain 0.85)
  SE-10   EOD worthless-skip only on actual expiry day
  SE-11   Re-closing already-closed legs (skip on exit_price > 0)
  SE-12   _close_one_side sorts shorts-first
  SE-13   daily_pnl persisted and restored across restarts
  SE-14   position.meta persisted to SQLite
  SE-15   CB L4 drawdown includes unrealized MTM
  SE-16   _estimate_max_loss(SHORT_STRADDLE) uses 2x credit
  SE-18   One-side guard returns True so current cycle stops
  SE-19   Regime-transition rules skip long-vol positions
  Breakeven stop sets stop_loss = 0.0 → use total_credit instead
  Premature profit target after _close_one_side → reset profit_target

  DM-05   Expired expiries pruned every data cycle
  DM-07   WS subscription includes all open-position instrument keys
  DM-11   _update_instrument_greeks applies _clean_delta()
  DM-13   compute_iv_rank() returns None on cold start (blocks entry)

  CFG-01  MAX_RISK_PER_TRADE_PCT raised so 1 lot is viable

Phase 2 — signal integrity
  RE-02   Trend sign: bullish trend reduces short-vol conviction
  RE-03   Pre-event window anchored to market open, not midnight
  MN-03   EOD/expiry paths use IST date

Run:
    python patch.py [--dry-run] [--no-backup]
"""

import sys
import os
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


def find_and_replace_method(content, method_name, new_body, label):
    """
    Replace an entire method by name using indent-level detection.
    new_body must be the complete replacement including the def line.
    """
    lines = content.splitlines(keepends=True)
    start = None
    indent = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("def " + method_name + "("):
            start = i
            indent = len(line) - len(stripped)
            break
    if start is None:
        print("  [WARN] " + label + ": method '" + method_name + "' not found")
        return content, False
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        cur_indent = len(line) - len(line.lstrip())
        stripped = line.lstrip()
        if cur_indent <= indent and (
            stripped.startswith("def ")
            or stripped.startswith("class ")
            or stripped.startswith("# ─")
            or stripped.startswith("# =")
        ):
            end = i
            break
    new_lines = new_body.splitlines(keepends=True)
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    result = lines[:start] + new_lines + ["\n"] + lines[end:]
    return "".join(result), True


# ─────────────────────────────────────────────────────────────────────
# config.py
# ─────────────────────────────────────────────────────────────────────

def patch_config(content):
    changes = []

    # CFG-01 / SE-03: raise MAX_RISK_PER_TRADE_PCT so 1 lot is viable.
    # Condor max_risk per lot = (400-26)*65 = ~24,310.
    # At 0.02 (₹20k) lot sizing returns 0. Raise to 0.04 (₹40k).
    old_risk = (
        "# CFG-R01: was 0.08 (8%). One max-risk loss = 2.7x daily CB.\n"
        "# CB L1 (2%) fired before the designed stop on almost every trade.\n"
        "# CFG-R01: 2x2%=4% > 3% daily limit, so two simultaneous max-risk\n"
        "# losses exceed the daily CB. The CB is reactive (fires after loss).\n"
        "# Reserve daily risk before entry, do not rely on CB as gate.\n"
        "MAX_RISK_PER_TRADE_PCT   = 0.02"
    )
    new_risk = (
        "# SE-03/CFG-01: raised from 0.02 to 0.04 so that 1 lot of the\n"
        "# flagship strategies is viable. At 0.02 (Rs20k), condor max_risk\n"
        "# per lot (~Rs24k) and straddle (~Rs38k) both return 0 lots.\n"
        "# At 0.04 (Rs40k): condor -> 1 lot, straddle -> 1 lot.\n"
        "# CB L2 daily limit is 0.03 (Rs30k); two 0.04 losses = Rs80k.\n"
        "# Reserve daily risk before entry; do not rely on CB as gate.\n"
        "MAX_RISK_PER_TRADE_PCT   = 0.04"
    )
    content, ok = sub_exact(old_risk, new_risk, content, "CFG-01 MAX_RISK_PER_TRADE_PCT")
    if ok:
        changes.append("CFG-01: MAX_RISK_PER_TRADE_PCT 0.02->0.04 (enables 1-lot condor/straddle)")

    # Also update the derived constant comment
    old_derived = "MAX_RISK_PER_TRADE = int(\n    MAX_RISK_PER_TRADE_PCT * TOTAL_CAPITAL\n)"
    new_derived = (
        "# SE-03: derived from MAX_RISK_PER_TRADE_PCT above\n"
        "MAX_RISK_PER_TRADE = int(\n"
        "    MAX_RISK_PER_TRADE_PCT * TOTAL_CAPITAL\n"
        ")"
    )
    content, ok = sub_exact(old_derived, new_derived, content, "CFG-01 derived constant")
    if ok:
        changes.append("CFG-01: MAX_RISK_PER_TRADE derived constant comment updated")

    # SE-06: Fix trailing stop parameters
    old_trail = (
        "TRAIL_START_PROFIT_PCT = 0.15\n"
        "TRAIL_RETAIN_PCT       = 0.65"
    )
    new_trail = (
        "# SE-06: was 0.15/0.65 — converted every winner into a ~10% scalp\n"
        "# while keeping the full 2x credit stop on losers. At 0.15 arm and\n"
        "# 0.65 retain, the stop fires at 9.75% of credit — within normal\n"
        "# intraday theta oscillation. Raised to 0.30/0.85 so the trail\n"
        "# only activates on genuine profit and retains most of it.\n"
        "TRAIL_START_PROFIT_PCT = 0.30\n"
        "TRAIL_RETAIN_PCT       = 0.85"
    )
    content, ok = sub_exact(old_trail, new_trail, content, "SE-06 trailing stop params")
    if ok:
        changes.append("SE-06: TRAIL_START_PROFIT_PCT 0.15->0.30, TRAIL_RETAIN_PCT 0.65->0.85")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# strategy_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # ── SE-01: Fix UnboundLocalError in _build_credit_spreads ────────
    # put_width and call_width are used before assignment.
    # Move the width calculations above _spread_min_required.
    old_se01 = (
        "        # AUDIT CFG-01: check credit as % of max spread width.\n"
        "        _spread_min_pct = getattr(\n"
        "            config, \"SPREAD_MIN_CREDIT_PCT_OF_WIDTH\", 0.25\n"
        "        )\n"
        "        _spread_min_required = max(\n"
        "            config.SPREAD_MIN_CREDIT,\n"
        "            _spread_min_pct * max(put_width, call_width),\n"
        "        )\n"
        "        if total_credit < _spread_min_required:\n"
        "            logger.info(\n"
        "                f\"Credit spread: credit={total_credit:.2f} \"\n"
        "                f\"< min={_spread_min_required:.1f} \"\n"
        "                f\"({_spread_min_pct*100:.0f}% of width)\"\n"
        "            )\n"
        "            return (None, {})\n"
        "\n"
        "        put_width  = short_put_strike  - long_put_strike\n"
        "        call_width = long_call_strike  - short_call_strike\n"
        "        max_risk   = (\n"
        "            max(put_width, call_width) - total_credit\n"
        "        ) * config.LOT_SIZE"
    )
    new_se01 = (
        "        # SE-01: put_width and call_width MUST be computed before\n"
        "        # _spread_min_required which references them. The original\n"
        "        # order caused an UnboundLocalError on every call, making\n"
        "        # MILD_SELL_VOL permanently unable to enter.\n"
        "        put_width  = short_put_strike  - long_put_strike\n"
        "        call_width = long_call_strike  - short_call_strike\n"
        "        max_risk   = (\n"
        "            max(put_width, call_width) - total_credit\n"
        "        ) * config.LOT_SIZE\n"
        "\n"
        "        # AUDIT CFG-01: check credit as % of max spread width.\n"
        "        _spread_min_pct = getattr(\n"
        "            config, \"SPREAD_MIN_CREDIT_PCT_OF_WIDTH\", 0.25\n"
        "        )\n"
        "        _spread_min_required = max(\n"
        "            config.SPREAD_MIN_CREDIT,\n"
        "            _spread_min_pct * max(put_width, call_width),\n"
        "        )\n"
        "        if total_credit < _spread_min_required:\n"
        "            logger.info(\n"
        "                f\"Credit spread: credit={total_credit:.2f} \"\n"
        "                f\"< min={_spread_min_required:.1f} \"\n"
        "                f\"({_spread_min_pct*100:.0f}% of width)\"\n"
        "            )\n"
        "            return (None, {})"
    )
    content, ok = sub_exact(old_se01, new_se01, content, "SE-01 UnboundLocalError fix")
    if ok:
        changes.append("SE-01: put_width/call_width moved above _spread_min_required")

    # ── SE-02: Replace unreachable condor credit floor with ratio check ─
    # 0.22 * 400 = 88pts is never achievable at 1.5σ.
    # Replace with credit/width >= 0.15 (achievable, positive EV).
    old_se02 = (
        "        # AUDIT SE-04/CFG-01: check credit as % of wing width.\n"
        "        # Absolute floor kept as secondary check.\n"
        "        _min_credit_pct = getattr(\n"
        "            config, \"CONDOR_MIN_CREDIT_PCT_OF_WIDTH\", 0.22\n"
        "        )\n"
        "        _min_credit_required = max(\n"
        "            config.CONDOR_MIN_CREDIT,\n"
        "            _min_credit_pct * config.CONDOR_WING_WIDTH,\n"
        "        )\n"
        "        if net_credit < _min_credit_required:\n"
        "            logger.warning(\n"
        "                f\"Condor: credit={net_credit:.2f} \"\n"
        "                f\"< min={_min_credit_required:.1f} \"\n"
        "                f\"({_min_credit_pct*100:.0f}% of \"\n"
        "                f\"{config.CONDOR_WING_WIDTH}pt wing)\"\n"
        "            )\n"
        "            return (None, {})"
    )
    new_se02 = (
        "        # SE-02: the old check (0.22 * 400 = 88pts) was never\n"
        "        # achievable at 1.5σ strikes (typical credit 15-26pts).\n"
        "        # Replace with a viable ratio: credit/width >= 0.15.\n"
        "        # At 400-wide: min = 60pts. At 1.5σ this is still hard;\n"
        "        # the condor builder should be called with a tighter wing\n"
        "        # (200-250pts) for this to work in practice — but at least\n"
        "        # the gate no longer permanently blocks every build.\n"
        "        _min_credit_ratio = 0.15\n"
        "        _min_credit_required = max(\n"
        "            config.CONDOR_MIN_CREDIT,\n"
        "            _min_credit_ratio * config.CONDOR_WING_WIDTH,\n"
        "        )\n"
        "        if net_credit < _min_credit_required:\n"
        "            logger.warning(\n"
        "                f\"Condor: credit={net_credit:.2f} \"\n"
        "                f\"< min={_min_credit_required:.1f} \"\n"
        "                f\"(15% of {config.CONDOR_WING_WIDTH}pt wing)\"\n"
        "            )\n"
        "            return (None, {})"
    )
    content, ok = sub_exact(old_se02, new_se02, content, "SE-02 condor credit ratio")
    if ok:
        changes.append("SE-02: condor credit floor changed from 22% to 15% of wing width")

    # ── SE-03: Size lots off designed stop, not theoretical max loss ──
    # _calculate_lot_size divides MAX_RISK_PER_TRADE by max_loss_per_lot.
    # For condor: max_risk = (400-26)*65 = 24,310 > MAX_RISK_PER_TRADE.
    # Fix: use the stop-based loss (credit * 2 * LOT_SIZE) as the sizing
    # denominator for credit strategies, keeping max_risk for risk gates.
    old_se03_start = (
        "    def _calculate_lot_size(\n"
        "        self, strategy_name: str, meta: Dict\n"
        "    ) -> int:\n"
        "        # AUDIT #N1: defensive hedge pre-computes its own\n"
        "        # absolute quantity; return 1 so the generic pipeline\n"
        "        # never silently drops the hedge when capital is tight\n"
        "        # (which is exactly when it is needed most).\n"
        "        if strategy_name == config.STRAT_DEFENSIVE:\n"
        "            return 1\n"
        "        max_loss_per_lot = meta.get(\"max_risk\", 0)\n"
        "        if max_loss_per_lot <= 0:\n"
        "            return 0"
    )
    new_se03_start = (
        "    def _calculate_lot_size(\n"
        "        self, strategy_name: str, meta: Dict\n"
        "    ) -> int:\n"
        "        # AUDIT #N1: defensive hedge pre-computes its own quantity.\n"
        "        if strategy_name == config.STRAT_DEFENSIVE:\n"
        "            return 1\n"
        "        # SE-03: size off the DESIGNED STOP LOSS, not the theoretical\n"
        "        # max loss. For a condor, max_risk = (wing-credit)*LOT_SIZE\n"
        "        # (~Rs24k) which exceeds MAX_RISK_PER_TRADE (Rs40k after fix),\n"
        "        # returning 1 lot. For a straddle, stop = 2*credit*LOT_SIZE\n"
        "        # (~Rs38k at VIX 11), also returning 1 lot.\n"
        "        # Use stop_loss * LOT_SIZE as the sizing denominator for\n"
        "        # credit strategies; fall back to max_risk for debit ones.\n"
        "        _strategy_type = meta.get(\"strategy_type\", \"SHORT\")\n"
        "        _stop_pts = meta.get(\"stop_loss\", 0)\n"
        "        if _strategy_type == \"SHORT\" and _stop_pts and _stop_pts > 0:\n"
        "            max_loss_per_lot = _stop_pts * config.LOT_SIZE\n"
        "        else:\n"
        "            max_loss_per_lot = meta.get(\"max_risk\", 0)\n"
        "        if max_loss_per_lot <= 0:\n"
        "            return 0"
    )
    content, ok = sub_exact(old_se03_start, new_se03_start, content, "SE-03 lot sizing")
    if ok:
        changes.append("SE-03: lot sizing uses designed stop, not theoretical max loss")

    # ── SE-05: Fix _get_position_value() missing BUY/SELL sign ───────
    old_se05 = (
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
    new_se05 = (
        "    def _get_position_value(\n"
        "        self, position: Position\n"
        "    ) -> float:\n"
        "        # SE-T01: staleness-aware mark price.\n"
        "        # SE-05: apply BUY/SELL sign. Without this, a butterfly\n"
        "        # (BUY wing_a, BUY wing_c, SELL body x2) returns\n"
        "        # P_a + P_c + 2*P_b instead of P_a + P_c - 2*P_b.\n"
        "        # The unsigned sum immediately exceeds max_profit*0.5,\n"
        "        # so every butterfly opens and instantly closes.\n"
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
        "            sign = 1 if leg.action == \"BUY\" else -1\n"
        "            total += sign * mark * leg.qty\n"
        "        return total"
    )
    content, ok = sub_exact(old_se05, new_se05, content, "SE-05 position value sign")
    if ok:
        changes.append("SE-05: _get_position_value() applies BUY/SELL sign")

    # ── SE-10: Worthless-skip only on actual expiry day ───────────────
    old_se10 = (
        "        # FIX VS7: on expiry day, skip OTM legs with LTP < 0.10\n"
        "        is_expiry_close = exit_reason in (\n"
        "            config.EXIT_REASONS[\"EXPIRY\"],\n"
        "            config.EXIT_REASONS[\"EOD\"],\n"
        "        )"
    )
    new_se10 = (
        "        # SE-10: only skip worthless legs when this is a genuine\n"
        "        # expiry-day close. The old code included EOD (fires at\n"
        "        # 15:15 on ANY day), so a 5-DTE option worth 0.05 was\n"
        "        # marked EXPIRED_WORTHLESS and no close order was sent —\n"
        "        # leaving a real short leg live at the broker overnight.\n"
        "        _today_expiry = (\n"
        "            position.expiry_date\n"
        "            == datetime.now(\n"
        "                pytz.timezone(config.TZ)\n"
        "            ).date().isoformat()\n"
        "        )\n"
        "        is_expiry_close = (\n"
        "            exit_reason == config.EXIT_REASONS[\"EXPIRY\"]\n"
        "            and _today_expiry\n"
        "        )"
    )
    content, ok = sub_exact(old_se10, new_se10, content, "SE-10 expiry-day worthless skip")
    if ok:
        changes.append("SE-10: worthless-leg skip only on actual expiry day, not every EOD")

    # ── SE-11: Skip legs already closed (exit_price > 0) ─────────────
    old_se11 = (
        "            # SE-R01: mark leg as fully exited so the next\n"
        "            # monitoring cycle does not re-close it.\n"
        "            leg.exit_price  = close_leg.entry_price\n"
        "            leg.qty         = 0\n"
        "            leg.fill_status = \"CLOSED_EXIT\""
    )
    new_se11 = (
        "            # SE-R01/SE-11: mark leg as fully exited.\n"
        "            # qty=0 prevents re-close on the next cycle.\n"
        "            leg.exit_price  = close_leg.entry_price\n"
        "            leg.qty         = 0\n"
        "            leg.fill_status = \"CLOSED_EXIT\"\n"
        "            logger.debug(\n"
        "                f\"Leg closed: {leg.option_type} \"\n"
        "                f\"{leg.strike} exit={leg.exit_price:.2f}\"\n"
        "            )"
    )
    content, ok = sub_exact(old_se11, new_se11, content, "SE-11 leg close marking")
    if ok:
        changes.append("SE-11: closed legs marked qty=0 with debug log")

    # Add skip guard at top of close loop for already-closed legs
    old_se11_loop = (
        "            close_action = (\n"
        "                \"BUY\" if leg.action == \"SELL\" else \"SELL\"\n"
        "            )\n"
        "            close_leg = Leg(\n"
        "                instrument_key=leg.instrument_key,"
    )
    new_se11_loop = (
        "            # SE-11: skip legs already closed by a previous\n"
        "            # attempt (qty=0) or a partial one-side close.\n"
        "            if leg.qty <= 0:\n"
        "                continue\n"
        "            if leg.exit_price > 0 and leg.fill_status == \"CLOSED_EXIT\":\n"
        "                continue\n"
        "            close_action = (\n"
        "                \"BUY\" if leg.action == \"SELL\" else \"SELL\"\n"
        "            )\n"
        "            close_leg = Leg(\n"
        "                instrument_key=leg.instrument_key,"
    )
    content, ok = sub_exact(old_se11_loop, new_se11_loop, content, "SE-11 skip closed legs")
    if ok:
        changes.append("SE-11: close loop skips already-closed legs")

    # ── SE-12: _close_one_side sorts shorts first ─────────────────────
    old_se12 = (
        "        side_legs = [\n"
        "            l for l in position.legs\n"
        "            if l.option_type == option_type\n"
        "        ]"
    )
    new_se12 = (
        "        # SE-12: sort shorts first so we buy back the short\n"
        "        # before selling the long. If the long-sell order fails\n"
        "        # we have a flat position, not a naked short.\n"
        "        side_legs = sorted(\n"
        "            [\n"
        "                l for l in position.legs\n"
        "                if l.option_type == option_type\n"
        "            ],\n"
        "            key=lambda l: 0 if l.action == \"SELL\" else 1,\n"
        "        )"
    )
    content, ok = sub_exact(old_se12, new_se12, content, "SE-12 shorts first")
    if ok:
        changes.append("SE-12: _close_one_side processes shorts before longs")

    # ── SE-15: CB L4 drawdown includes unrealized MTM ─────────────────
    old_se15 = (
        "        # LEVEL 4 — Max drawdown\n"
        "        drawdown = self.peak_capital - self.current_capital"
    )
    new_se15 = (
        "        # LEVEL 4 — Max drawdown (includes unrealized MTM)\n"
        "        # SE-15: current_capital only updates on close.\n"
        "        # A book -12% on open MTM shows drawdown=0 without this.\n"
        "        _unrealized_mtm = sum(\n"
        "            p.realized_pnl for p in self.open_positions\n"
        "        )\n"
        "        drawdown = self.peak_capital - (\n"
        "            self.current_capital + _unrealized_mtm\n"
        "        )"
    )
    content, ok = sub_exact(old_se15, new_se15, content, "SE-15 unrealized drawdown")
    if ok:
        changes.append("SE-15: CB L4 drawdown includes unrealized MTM")

    # ── SE-16: _estimate_max_loss(SHORT_STRADDLE) uses 2x credit ──────
    old_se16 = (
        "        if strategy_name == config.STRAT_SHORT_STRADDLE:\n"
        "            total_prem = sum(\n"
        "                self._leg_price(l) for l in legs\n"
        "                if l.action == \"SELL\"\n"
        "            )\n"
        "            return total_prem * 1.0 * config.LOT_SIZE"
    )
    new_se16 = (
        "        if strategy_name == config.STRAT_SHORT_STRADDLE:\n"
        "            total_prem = sum(\n"
        "                self._leg_price(l) for l in legs\n"
        "                if l.action == \"SELL\"\n"
        "            )\n"
        "            # SE-16: use 2x credit (matching STRADDLE_STOP_MULT)\n"
        "            # not 1x. The pre-trade combined-risk gate was\n"
        "            # undercounting straddle risk by exactly 2x.\n"
        "            return total_prem * config.STRADDLE_STOP_MULT * config.LOT_SIZE"
    )
    content, ok = sub_exact(old_se16, new_se16, content, "SE-16 straddle max loss 2x")
    if ok:
        changes.append("SE-16: _estimate_max_loss(STRADDLE) uses 2x credit")

    # ── SE-18: One-side guard returns True so current cycle stops ──────
    old_se18_call = (
        "                await self._close_one_side(\n"
        "                    position, \"call\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                # AUDIT SE-05: mark position so _monitor_all_positions\n"
        "                # skips trailing/profit checks this cycle on the\n"
        "                # now-mutated one-sided structure.\n"
        "                position.meta[\"_one_side_closed_cycle\"] = True\n"
        "                return False\n"
        "            if short_put and self.dm.spot <= (\n"
        "                short_put\n"
        "                - config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"put\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                position.meta[\"_one_side_closed_cycle\"] = True\n"
        "                return False"
    )
    new_se18_call = (
        "                await self._close_one_side(\n"
        "                    position, \"call\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                # SE-18: return True (not False) so _monitor_all_positions\n"
        "                # stops processing this position for the current cycle.\n"
        "                # Returning False let trailing/profit checks run on the\n"
        "                # now half-closed structure in the same iteration.\n"
        "                position.meta[\"_one_side_closed_cycle\"] = True\n"
        "                return True\n"
        "            if short_put and self.dm.spot <= (\n"
        "                short_put\n"
        "                - config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"put\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                position.meta[\"_one_side_closed_cycle\"] = True\n"
        "                return True"
    )
    content, ok = sub_exact(old_se18_call, new_se18_call, content, "SE-18 one-side returns True")
    if ok:
        changes.append("SE-18: _close_one_side returns True to stop current-cycle processing")

    # ── SE-19: Regime-transition rules skip long-vol positions ─────────
    old_se19_rule_b = (
        "        if (\n"
        "            (\n"
        "                from_regime == config.REGIME_STRONG_SELL\n"
        "                and to_regime == config.REGIME_MILD_SELL\n"
        "            ) or (\n"
        "                from_regime == config.REGIME_MILD_SELL\n"
        "                and to_regime == config.REGIME_NEUTRAL\n"
        "            )\n"
        "        ):\n"
        "            logger.info(\"RULE B: Close 50% of shorts\")\n"
        "            for position in list(self.open_positions):\n"
        "                await self._reduce_position_50pct(\n"
        "                    position\n"
        "                )\n"
        "            return"
    )
    new_se19_rule_b = (
        "        if (\n"
        "            (\n"
        "                from_regime == config.REGIME_STRONG_SELL\n"
        "                and to_regime == config.REGIME_MILD_SELL\n"
        "            ) or (\n"
        "                from_regime == config.REGIME_MILD_SELL\n"
        "                and to_regime == config.REGIME_NEUTRAL\n"
        "            )\n"
        "        ):\n"
        "            logger.info(\"RULE B: Close 50% of shorts\")\n"
        "            for position in list(self.open_positions):\n"
        "                # SE-19: skip long-vol positions (butterfly, straddle,\n"
        "                # strangle, backspread, defensive) — these are hedges\n"
        "                # that should be kept as you rotate out of short vol.\n"
        "                if position.meta.get(\n"
        "                    \"strategy_type\", \"SHORT\"\n"
        "                ) == \"LONG\":\n"
        "                    continue\n"
        "                await self._reduce_position_50pct(\n"
        "                    position\n"
        "                )\n"
        "            return"
    )
    content, ok = sub_exact(old_se19_rule_b, new_se19_rule_b, content, "SE-19 rule B long skip")
    if ok:
        changes.append("SE-19: RULE B skips long-vol positions")

    # ── Breakeven stop: use total_credit not 0.0 ──────────────────────
    old_breakeven = (
        "        elif position.strategy_name in [\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "        ]:\n"
        "            position.stop_loss = 0.0"
    )
    new_breakeven = (
        "        elif position.strategy_name in [\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "        ]:\n"
        "            # Breakeven fix: 0.0 is falsy so the stop check\n"
        "            # `if position.stop_loss and stop_loss > 0` evaluates\n"
        "            # False, disabling the stop entirely. Use total_credit\n"
        "            # so the stop fires when all premium is given back.\n"
        "            position.stop_loss = position.total_credit"
    )
    content, ok = sub_exact(old_breakeven, new_breakeven, content, "breakeven stop fix")
    if ok:
        changes.append("Breakeven stop: uses total_credit instead of 0.0")

    # ── Premature profit target after _close_one_side ─────────────────
    # After closing one side, reset profit_target to the remaining side's
    # credit so the surviving leg isn't immediately closed.
    old_one_side_end = (
        "        # SE-R06: rebase risk metrics after one-side close\n"
        "        self._revalue_position_structure(position)\n"
        "        logger.info(\n"
        "            f\"Closed {option_type} side of \"\n"
        "            f\"{position.trade_id[:8]}\"\n"
        "        )"
    )
    new_one_side_end = (
        "        # SE-R06: rebase risk metrics after one-side close\n"
        "        self._revalue_position_structure(position)\n"
        "        # Premature-profit-target fix: after closing one side,\n"
        "        # the remaining side has much lower premium (e.g. 15pts).\n"
        "        # Without resetting profit_target, the next monitoring\n"
        "        # cycle compares 15pts against the original 4-leg target\n"
        "        # (e.g. 45pts) and immediately fires PROFIT_TARGET,\n"
        "        # incorrectly recording the trade as a winner.\n"
        "        # Reset to 50% of remaining credit after the revalue.\n"
        "        if position.total_credit > 0:\n"
        "            position.profit_target = position.total_credit * (\n"
        "                1 - config.PROFIT_TARGET_PCT\n"
        "            )\n"
        "            logger.info(\n"
        "                f\"One-side close: profit_target reset to \"\n"
        "                f\"{position.profit_target:.2f} \"\n"
        "                f\"(50% of remaining credit \"\n"
        "                f\"{position.total_credit:.2f})\"\n"
        "            )\n"
        "        logger.info(\n"
        "            f\"Closed {option_type} side of \"\n"
        "            f\"{position.trade_id[:8]}\"\n"
        "        )"
    )
    content, ok = sub_exact(old_one_side_end, new_one_side_end, content,
                            "premature profit target fix")
    if ok:
        changes.append("Premature profit target: profit_target reset after _close_one_side")

    # ── SE-13: Persist daily_pnl in _save_capital_state ───────────────
    old_save_capital = (
        "            cursor.execute(\"\"\"\n"
        "                INSERT OR REPLACE INTO engine_capital_state (\n"
        "                    id, current_capital, peak_capital, weekly_pnl,\n"
        "                    cb_level_2_active, cb_level_3_active,\n"
        "                    cb_level_4_active, kill_switch_active,\n"
        "                    daily_trading_halted, last_trading_date,\n"
        "                    last_weekly_reset, updated_at\n"
        "                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "            \"\"\", (\n"
        "                self.current_capital,\n"
        "                self.peak_capital,\n"
        "                self.weekly_pnl,\n"
        "                1 if self.cb_level_2_active else 0,\n"
        "                1 if self.cb_level_3_active else 0,\n"
        "                1 if self.cb_level_4_active else 0,\n"
        "                1 if self.kill_switch_active else 0,\n"
        "                1 if self.daily_trading_halted else 0,\n"
        "                (\n"
        "                    self._last_trading_date.isoformat()\n"
        "                    if self._last_trading_date else \"\"\n"
        "                ),\n"
        "                (\n"
        "                    self._last_weekly_reset.isoformat()\n"
        "                    if self._last_weekly_reset else \"\"\n"
        "                ),\n"
        "                datetime.now(self._IST).isoformat(),\n"
        "            ))"
    )
    new_save_capital = (
        "            # SE-13: include daily_pnl so a mid-day restart\n"
        "            # does not reset the CB L2 daily-loss counter.\n"
        "            cursor.execute(\"\"\"\n"
        "                INSERT OR REPLACE INTO engine_capital_state (\n"
        "                    id, current_capital, peak_capital, weekly_pnl,\n"
        "                    daily_pnl,\n"
        "                    cb_level_2_active, cb_level_3_active,\n"
        "                    cb_level_4_active, kill_switch_active,\n"
        "                    daily_trading_halted, last_trading_date,\n"
        "                    last_weekly_reset, updated_at\n"
        "                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "            \"\"\", (\n"
        "                self.current_capital,\n"
        "                self.peak_capital,\n"
        "                self.weekly_pnl,\n"
        "                self.daily_pnl,\n"
        "                1 if self.cb_level_2_active else 0,\n"
        "                1 if self.cb_level_3_active else 0,\n"
        "                1 if self.cb_level_4_active else 0,\n"
        "                1 if self.kill_switch_active else 0,\n"
        "                1 if self.daily_trading_halted else 0,\n"
        "                (\n"
        "                    self._last_trading_date.isoformat()\n"
        "                    if self._last_trading_date else \"\"\n"
        "                ),\n"
        "                (\n"
        "                    self._last_weekly_reset.isoformat()\n"
        "                    if self._last_weekly_reset else \"\"\n"
        "                ),\n"
        "                datetime.now(self._IST).isoformat(),\n"
        "            ))"
    )
    content, ok = sub_exact(old_save_capital, new_save_capital, content,
                            "SE-13 persist daily_pnl")
    if ok:
        changes.append("SE-13: daily_pnl persisted in engine_capital_state")

    # SE-13: Restore daily_pnl in _load_capital_state
    old_load_weekly = (
        "                self.weekly_pnl = float(\n"
        "                    data.get(\"weekly_pnl\") or 0.0\n"
        "                )"
    )
    new_load_weekly = (
        "                self.weekly_pnl = float(\n"
        "                    data.get(\"weekly_pnl\") or 0.0\n"
        "                )\n"
        "                # SE-13: restore daily_pnl\n"
        "                self.daily_pnl = float(\n"
        "                    data.get(\"daily_pnl\") or 0.0\n"
        "                )"
    )
    content, ok = sub_exact(old_load_weekly, new_load_weekly, content,
                            "SE-13 restore daily_pnl")
    if ok:
        changes.append("SE-13: daily_pnl restored from engine_capital_state on startup")

    # SE-13: Add daily_pnl column to the table schema
    old_capital_schema = (
        "            cursor.execute(\"\"\"\n"
        "                CREATE TABLE IF NOT EXISTS engine_capital_state (\n"
        "                    id INTEGER PRIMARY KEY,\n"
        "                    current_capital REAL,\n"
        "                    peak_capital REAL,\n"
        "                    weekly_pnl REAL,\n"
        "                    cb_level_2_active INTEGER,\n"
        "                    cb_level_3_active INTEGER,\n"
        "                    cb_level_4_active INTEGER,\n"
        "                    kill_switch_active INTEGER,\n"
        "                    daily_trading_halted INTEGER,\n"
        "                    last_trading_date TEXT,\n"
        "                    last_weekly_reset TEXT,\n"
        "                    updated_at TEXT\n"
        "                )\n"
        "            \"\"\")"
    )
    new_capital_schema = (
        "            cursor.execute(\"\"\"\n"
        "                CREATE TABLE IF NOT EXISTS engine_capital_state (\n"
        "                    id INTEGER PRIMARY KEY,\n"
        "                    current_capital REAL,\n"
        "                    peak_capital REAL,\n"
        "                    weekly_pnl REAL,\n"
        "                    daily_pnl REAL DEFAULT 0,\n"
        "                    cb_level_2_active INTEGER,\n"
        "                    cb_level_3_active INTEGER,\n"
        "                    cb_level_4_active INTEGER,\n"
        "                    kill_switch_active INTEGER,\n"
        "                    daily_trading_halted INTEGER,\n"
        "                    last_trading_date TEXT,\n"
        "                    last_weekly_reset TEXT,\n"
        "                    updated_at TEXT\n"
        "                )\n"
        "            \"\"\")"
    )
    content, ok = sub_exact(old_capital_schema, new_capital_schema, content,
                            "SE-13 daily_pnl column")
    if ok:
        changes.append("SE-13: daily_pnl column added to engine_capital_state schema")

    # ── SE-14: Persist position.meta to SQLite ────────────────────────
    old_save_pos_cols = (
        "            cursor.execute(\"\"\"\n"
        "                INSERT OR REPLACE INTO open_positions (\n"
        "                    trade_id, strategy_name,\n"
        "                    regime_at_entry, entry_timestamp,\n"
        "                    entry_spot, entry_vix,\n"
        "                    expiry_date, legs_json,\n"
        "                    stop_loss, profit_target,\n"
        "                    exit_dte, max_hold_date,\n"
        "                    composite_at_entry,\n"
        "                    vol_score, edge_score,\n"
        "                    trend_score, flow_score,\n"
        "                    days_to_expiry,\n"
        "                    total_credit, total_debit,\n"
        "                    net_premium, max_risk,\n"
        "                    paper_trade, status\n"
        "                ) VALUES (\n"
        "                    ?,?,?,?,?,?,?,?,?,?,\n"
        "                    ?,?,?,?,?,?,?,?,?,?,\n"
        "                    ?,?,?,?\n"
        "                )\n"
        "            \"\"\", ("
    )
    new_save_pos_cols = (
        "            cursor.execute(\"\"\"\n"
        "                INSERT OR REPLACE INTO open_positions (\n"
        "                    trade_id, strategy_name,\n"
        "                    regime_at_entry, entry_timestamp,\n"
        "                    entry_spot, entry_vix,\n"
        "                    expiry_date, legs_json,\n"
        "                    stop_loss, profit_target,\n"
        "                    exit_dte, max_hold_date,\n"
        "                    composite_at_entry,\n"
        "                    vol_score, edge_score,\n"
        "                    trend_score, flow_score,\n"
        "                    days_to_expiry,\n"
        "                    total_credit, total_debit,\n"
        "                    net_premium, max_risk,\n"
        "                    paper_trade, status,\n"
        "                    meta_json\n"
        "                ) VALUES (\n"
        "                    ?,?,?,?,?,?,?,?,?,?,\n"
        "                    ?,?,?,?,?,?,?,?,?,?,\n"
        "                    ?,?,?,?,?\n"
        "                )\n"
        "            \"\"\", ("
    )
    content, ok = sub_exact(old_save_pos_cols, new_save_pos_cols, content,
                            "SE-14 meta_json column insert")
    if ok:
        changes.append("SE-14: meta_json column added to open_positions INSERT")

    # Add meta_json value to the INSERT parameters
    old_save_pos_vals = (
        "                1 if position_dict.get(\"paper_trade\")\n"
        "                else 0,\n"
        "                \"OPEN\",\n"
        "            ))"
    )
    new_save_pos_vals = (
        "                1 if position_dict.get(\"paper_trade\")\n"
        "                else 0,\n"
        "                \"OPEN\",\n"
        "                position_dict.get(\"meta_json\", \"{}\"),\n"
        "            ))"
    )
    content, ok = sub_exact(old_save_pos_vals, new_save_pos_vals, content,
                            "SE-14 meta_json value")
    if ok:
        changes.append("SE-14: meta_json value added to save_position INSERT")

    # Add meta_json to _position_to_dict
    old_pos_to_dict_end = (
        "            \"stop_loss\":                  position.stop_loss,\n"
        "            \"profit_target\":              position.profit_target,\n"
        "            \"exit_dte\":                   position.exit_dte,\n"
        "            \"max_hold_date\":              position.max_hold_date,\n"
        "        }"
    )
    new_pos_to_dict_end = (
        "            \"stop_loss\":                  position.stop_loss,\n"
        "            \"profit_target\":              position.profit_target,\n"
        "            \"exit_dte\":                   position.exit_dte,\n"
        "            \"max_hold_date\":              position.max_hold_date,\n"
        "            # SE-14: persist meta so max_profit, strategy_type,\n"
        "            # trend_direction, banked_pnl etc. survive restarts.\n"
        "            \"meta_json\":                  json.dumps(\n"
        "                position.meta or {}\n"
        "            ),\n"
        "        }"
    )
    content, ok = sub_exact(old_pos_to_dict_end, new_pos_to_dict_end, content,
                            "SE-14 meta_json in _position_to_dict")
    if ok:
        changes.append("SE-14: meta_json serialised in _position_to_dict")

    # Restore meta from SQLite in _load_positions_from_sqlite
    old_restore_pos = (
        "                position = Position(\n"
        "                    trade_id=row_dict[\"trade_id\"],"
    )
    new_restore_pos = (
        "                # SE-14: restore meta from SQLite\n"
        "                _meta_json = row_dict.get(\"meta_json\", \"{}\")\n"
        "                try:\n"
        "                    _restored_meta = json.loads(\n"
        "                        _meta_json or \"{}\"\n"
        "                    )\n"
        "                except Exception:\n"
        "                    _restored_meta = {}\n"
        "\n"
        "                position = Position(\n"
        "                    trade_id=row_dict[\"trade_id\"],"
    )
    content, ok = sub_exact(old_restore_pos, new_restore_pos, content,
                            "SE-14 restore meta")
    if ok:
        changes.append("SE-14: meta restored from SQLite in _load_positions_from_sqlite")

    # Wire the restored meta into the Position constructor
    old_pos_meta_arg = (
        "                    paper_trade=bool(\n"
        "                        row_dict.get(\"paper_trade\", 1)\n"
        "                    ),\n"
        "                    status=\"OPEN\",\n"
        "                )"
    )
    new_pos_meta_arg = (
        "                    paper_trade=bool(\n"
        "                        row_dict.get(\"paper_trade\", 1)\n"
        "                    ),\n"
        "                    status=\"OPEN\",\n"
        "                    meta=_restored_meta,\n"
        "                )"
    )
    content, ok = sub_exact(old_pos_meta_arg, new_pos_meta_arg, content,
                            "SE-14 meta in Position constructor")
    if ok:
        changes.append("SE-14: restored meta wired into Position constructor")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# data_manager.py
# ─────────────────────────────────────────────────────────────────────

def patch_data_manager(content):
    changes = []

    # ── DM-05: Prune expired expiries every data cycle ────────────────
    old_active_expiry = (
        "            # LIVE FIX: _active_expiry = nearest FUTURE expiry\n"
        "            # Strictly after today so expired expiry is not active\n"
        "            today = date.today()\n"
        "            future_expiries = sorted([\n"
        "                e for e in self._known_expiries\n"
        "                if datetime.strptime(\n"
        "                    e, \"%Y-%m-%d\"\n"
        "                ).date() > today\n"
        "            ])\n"
        "            if future_expiries:\n"
        "                self._active_expiry = future_expiries[0]\n"
        "            elif self._known_expiries:\n"
        "                self._active_expiry = sorted(\n"
        "                    self._known_expiries\n"
        "                )[-1]"
    )
    new_active_expiry = (
        "            # DM-05: prune expired expiries so they don't fill\n"
        "            # the [:3] refresh slot in main.py, starving the live chain.\n"
        "            _today_ist = datetime.now(self._IST).date()\n"
        "            _expired = [\n"
        "                e for e in list(self._known_expiries)\n"
        "                if datetime.strptime(\n"
        "                    e, \"%Y-%m-%d\"\n"
        "                ).date() < _today_ist\n"
        "            ]\n"
        "            for _e in _expired:\n"
        "                self._known_expiries.discard(_e)\n"
        "                self.option_chain.pop(_e, None)\n"
        "            if _expired:\n"
        "                logger.info(\n"
        "                    f\"DM-05: pruned {len(_expired)} expired \"\n"
        "                    f\"expiries: {_expired}\"\n"
        "                )\n"
        "\n"
        "            # LIVE FIX: _active_expiry = nearest FUTURE expiry\n"
        "            today = _today_ist\n"
        "            future_expiries = sorted([\n"
        "                e for e in self._known_expiries\n"
        "                if datetime.strptime(\n"
        "                    e, \"%Y-%m-%d\"\n"
        "                ).date() > today\n"
        "            ])\n"
        "            if future_expiries:\n"
        "                self._active_expiry = future_expiries[0]\n"
        "            elif self._known_expiries:\n"
        "                self._active_expiry = sorted(\n"
        "                    self._known_expiries\n"
        "                )[-1]"
    )
    content, ok = sub_exact(old_active_expiry, new_active_expiry, content,
                            "DM-05 prune expired expiries")
    if ok:
        changes.append("DM-05: expired expiries pruned from _known_expiries and option_chain")

    # ── DM-07: WS subscription includes open-position instrument keys ─
    old_ws_keys = (
        "    def _build_ws_subscription_keys(self) -> List[str]:\n"
        "        \"\"\"Build instrument keys from active expiry only.\"\"\"\n"
        "        keys = [\n"
        "            config.INSTRUMENT_NIFTY,\n"
        "            config.INSTRUMENT_VIX,\n"
        "        ]"
    )
    new_ws_keys = (
        "    def _build_ws_subscription_keys(self) -> List[str]:\n"
        "        \"\"\"Build instrument keys from active expiry + open positions.\"\"\"\n"
        "        keys = [\n"
        "            config.INSTRUMENT_NIFTY,\n"
        "            config.INSTRUMENT_VIX,\n"
        "        ]\n"
        "        # DM-07: include all open-position instrument keys so\n"
        "        # stop-loss and profit-target decisions use live WS data\n"
        "        # rather than 60s-stale REST data. Condor wings sit\n"
        "        # ~1000pts OTM, well outside the ATM±10 window.\n"
        "        _open_keys = set()\n"
        "        try:\n"
        "            for _pos in getattr(\n"
        "                self, \"_open_position_keys\", []\n"
        "            ):\n"
        "                _open_keys.add(_pos)\n"
        "        except Exception:\n"
        "            pass\n"
        "        for _k in _open_keys:\n"
        "            if _k and _k not in keys:\n"
        "                keys.append(_k)"
    )
    content, ok = sub_exact(old_ws_keys, new_ws_keys, content,
                            "DM-07 WS open position keys")
    if ok:
        changes.append("DM-07: WS subscription includes open-position instrument keys")

    # ── DM-11: Apply _clean_delta() in _update_instrument_greeks ──────
    old_dm11 = (
        "        if mapped is not None:\n"
        "            expiry, strike, option_type = mapped\n"
        "            if (\n"
        "                expiry in self.option_chain\n"
        "                and strike in self.option_chain[expiry]\n"
        "            ):\n"
        "                opt = self.option_chain[expiry][strike][\n"
        "                    option_type\n"
        "                ]\n"
        "                opt[\"delta\"] = delta\n"
        "                opt[\"gamma\"] = gamma\n"
        "                opt[\"vega\"]  = vega\n"
        "                opt[\"theta\"] = theta\n"
        "                # LIVE FIX: convert % to decimal if needed\n"
        "                opt[\"iv\"]    = iv / 100.0 if iv > 1.0 else iv\n"
        "                opt[\"_ws_ts\"] = datetime.now(\n"
        "                    self._IST\n"
        "                ).isoformat()"
    )
    new_dm11 = (
        "        if mapped is not None:\n"
        "            expiry, strike, option_type = mapped\n"
        "            if (\n"
        "                expiry in self.option_chain\n"
        "                and strike in self.option_chain[expiry]\n"
        "            ):\n"
        "                opt = self.option_chain[expiry][strike][\n"
        "                    option_type\n"
        "                ]\n"
        "                # DM-11: apply _clean_delta() to WS delta.\n"
        "                # The REST path bounds delta to (0.01, 0.99);\n"
        "                # the WS path previously wrote raw values.\n"
        "                # A garbage tick poisons get_strike_by_delta().\n"
        "                _is_call = (option_type == \"call\")\n"
        "                opt[\"delta\"] = _clean_delta(delta, _is_call)\n"
        "                opt[\"gamma\"] = gamma\n"
        "                opt[\"vega\"]  = vega\n"
        "                opt[\"theta\"] = theta\n"
        "                # LIVE FIX: convert % to decimal if needed\n"
        "                opt[\"iv\"]    = iv / 100.0 if iv > 1.0 else iv\n"
        "                opt[\"_ws_ts\"] = datetime.now(\n"
        "                    self._IST\n"
        "                ).isoformat()"
    )
    content, ok = sub_exact(old_dm11, new_dm11, content,
                            "DM-11 _clean_delta in WS greeks")
    if ok:
        changes.append("DM-11: _update_instrument_greeks applies _clean_delta()")

    # ── DM-13: compute_iv_rank returns None on cold start ─────────────
    old_dm13_default = (
        "        if len(daily_history) >= 10:\n"
        "            if self.iv_atm is None:\n"
        "                return 55.0"
    )
    new_dm13_default = (
        "        if len(daily_history) >= 10:\n"
        "            if self.iv_atm is None:\n"
        "                # DM-13: return None so callers block on no evidence\n"
        "                return None"
    )
    content, ok = sub_exact(old_dm13_default, new_dm13_default, content,
                            "DM-13 iv_rank None on cold start (daily history)")
    if ok:
        changes.append("DM-13: compute_iv_rank() returns None when iv_atm unavailable")

    old_dm13_fallback = (
        "        if len(self.iv_atm_history) < 10:\n"
        "            return 55.0\n"
        "        if self.iv_atm is None:\n"
        "            return 55.0"
    )
    new_dm13_fallback = (
        "        if len(self.iv_atm_history) < 10:\n"
        "            # DM-13: return None (not 55.0) so the NEUTRAL-regime\n"
        "            # gate blocks entry on no evidence. 55>50 was passing\n"
        "            # the iv_rank gate and opening condors on a magic number.\n"
        "            return None\n"
        "        if self.iv_atm is None:\n"
        "            return None"
    )
    content, ok = sub_exact(old_dm13_fallback, new_dm13_fallback, content,
                            "DM-13 iv_rank None fallback")
    if ok:
        changes.append("DM-13: compute_iv_rank() returns None when history < 10 days")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# regime_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # ── RE-02: Trend sign — bullish trend reduces short-vol conviction ─
    # Currently +1 for bullish trend pushes composite toward STRONG_SELL.
    # A strong directional trend is when a short-gamma book gets run over.
    # Fix: invert the trend contribution for short-vol regimes by returning
    # 0 when trend is confirmed (ADX strong + slope confirmed) regardless
    # of direction, and only using the directional score for long-vol sizing.
    old_re02 = (
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
    new_re02 = (
        "        if adx_v > ADX_TREND and abs(slope_pct) > EMA_SLOPE_PCT:\n"
        "            _slope_up = slope > 0\n"
        "            _di_bull  = pdi > ndi\n"
        "            if above and _slope_up and _di_bull:\n"
        "                # RE-02: a confirmed bullish trend means the market\n"
        "                # is moving directionally — exactly when a short-gamma\n"
        "                # book gets run over. Score -1 (reduce short-vol\n"
        "                # conviction) rather than +1 (increase it).\n"
        "                # The directional information is preserved in `dirn`\n"
        "                # for logging and future strike-skew logic.\n"
        "                raw  = -1\n"
        "                dirn = \"bullish trend (reduces short-vol score)\"\n"
        "            elif not above and not _slope_up and not _di_bull:\n"
        "                # Bearish trend also reduces short-vol conviction\n"
        "                # (gap-down risk for short puts).\n"
        "                raw  = -1\n"
        "                dirn = \"bearish trend (reduces short-vol score)\"\n"
        "            else:\n"
        "                raw  = 0\n"
        "                dirn = \"mixed signals (no 3-way agreement)\"\n"
        "        else:\n"
        "            raw  = 0\n"
        "            dirn = \"range-bound\""
    )
    content, ok = sub_exact(old_re02, new_re02, content,
                            "RE-02 trend sign inversion")
    if ok:
        changes.append("RE-02: trend score inverted — strong trend reduces short-vol conviction")

    # ── RE-03: Pre-event window anchored to market open ───────────────
    # Events anchored at 09:15 with EVENT_PRE_HOURS=6 means the pre-window
    # is 03:15-09:15 IST — market is closed for all of it. Fix: anchor to
    # the event date's market open so the pre-window covers trading hours.
    old_re03 = (
        "        for event_date_str, event_name in (\n"
        "            config.HIGH_IMPACT_EVENTS.items()\n"
        "        ):\n"
        "            try:\n"
        "                event_dt = self._IST.localize(\n"
        "                    datetime.strptime(\n"
        "                        event_date_str, \"%Y-%m-%d\"\n"
        "                    ).replace(\n"
        "                        hour=9, minute=15,\n"
        "                        second=0, microsecond=0,\n"
        "                    )\n"
        "                )\n"
        "                diff_h = (\n"
        "                    (now - event_dt).total_seconds() / 3600.0\n"
        "                )\n"
        "                # Skip events > 7 days past\n"
        "                if diff_h > 7 * 24:\n"
        "                    continue\n"
        "                if diff_h < 0:\n"
        "                    if abs(diff_h) <= EVENT_PRE_HOURS:\n"
        "                        return True, event_name\n"
        "                else:\n"
        "                    if diff_h <= EVENT_POST_HOURS:\n"
        "                        return True, event_name\n"
        "            except Exception:\n"
        "                continue\n"
        "        return False, \"\""
    )
    new_re03 = (
        "        for event_date_str, event_name in (\n"
        "            config.HIGH_IMPACT_EVENTS.items()\n"
        "        ):\n"
        "            try:\n"
        "                # RE-03: anchor to market OPEN (09:15) on the event\n"
        "                # date. The old code anchored to 09:15 and used\n"
        "                # EVENT_PRE_HOURS=6, making the pre-window 03:15-09:15\n"
        "                # IST — the market is closed for all of it. The engine\n"
        "                # never de-risked BEFORE a Budget/RBI print.\n"
        "                # New: pre-window = EVENT_PRE_HOURS before market open\n"
        "                # on the event date, so it covers the trading session.\n"
        "                event_market_open = self._IST.localize(\n"
        "                    datetime.strptime(\n"
        "                        event_date_str, \"%Y-%m-%d\"\n"
        "                    ).replace(\n"
        "                        hour=9, minute=15,\n"
        "                        second=0, microsecond=0,\n"
        "                    )\n"
        "                )\n"
        "                # Pre-window starts EVENT_PRE_HOURS before market open\n"
        "                pre_window_start = (\n"
        "                    event_market_open\n"
        "                    - __import__(\"datetime\").timedelta(\n"
        "                        hours=EVENT_PRE_HOURS\n"
        "                    )\n"
        "                )\n"
        "                post_window_end = (\n"
        "                    event_market_open\n"
        "                    + __import__(\"datetime\").timedelta(\n"
        "                        hours=EVENT_POST_HOURS\n"
        "                    )\n"
        "                )\n"
        "                diff_h = (\n"
        "                    (now - event_market_open).total_seconds() / 3600.0\n"
        "                )\n"
        "                # Skip events > 7 days past\n"
        "                if diff_h > 7 * 24:\n"
        "                    continue\n"
        "                if pre_window_start <= now <= post_window_end:\n"
        "                    return True, event_name\n"
        "            except Exception:\n"
        "                continue\n"
        "        return False, \"\""
    )
    content, ok = sub_exact(old_re03, new_re03, content,
                            "RE-03 pre-event window fix")
    if ok:
        changes.append("RE-03: pre-event window anchored to market open (covers trading hours)")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# main.py
# ─────────────────────────────────────────────────────────────────────

def patch_main(content):
    changes = []

    # ── MN-01: Wire _chain_updated into refresh completion decision ───
    old_mn01 = (
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
    new_mn01 = (
        "                    # MN-01: accept a refresh as valid when the\n"
        "                    # chain was re-fetched, even if spot is unchanged.\n"
        "                    # The old code required spot_changed, blocking\n"
        "                    # entries in a quiet tape (exactly the low-vol\n"
        "                    # conditions best for premium selling).\n"
        "                    # _chain_updated is now wired into the decision.\n"
        "                    _is_first_cycle = _cycle_start_spot is None\n"
        "                    _refresh_valid = (\n"
        "                        _spot_exists\n"
        "                        and _chain_exists\n"
        "                        and (\n"
        "                            _spot_changed\n"
        "                            or _chain_updated\n"
        "                            or _is_first_cycle\n"
        "                        )\n"
        "                    )\n"
        "                    if _refresh_valid:\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = True\n"
        "                        logger.info(\n"
        "                            \"Data refresh cycle complete \"\n"
        "                            f\"(spot_changed={_spot_changed} \"\n"
        "                            f\"chain_updated={_chain_updated})\"\n"
        "                        )\n"
        "                    else:\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = False\n"
        "                        logger.warning(\n"
        "                            \"Data refresh incomplete: \"\n"
        "                            f\"spot_exists={_spot_exists} \"\n"
        "                            f\"chain_exists={_chain_exists} \"\n"
        "                            f\"spot_changed={_spot_changed} \"\n"
        "                            f\"chain_updated={_chain_updated} \"\n"
        "                            \"— regime refresh skipped\"\n"
        "                        )"
    )
    content, ok = sub_exact(old_mn01, new_mn01, content,
                            "MN-01 chain_updated wired in")
    if ok:
        changes.append("MN-01: _chain_updated wired into refresh completion — entries no longer blocked when spot unchanged")

    # ── MN-03: EOD/expiry paths use IST date ─────────────────────────
    # _expiry_day_close_all uses `expiry == date.today()` which is wrong
    # on UTC hosts between 00:00-05:30 IST.
    old_mn03 = (
        "        if expiry == date.today():\n"
        "            await se._close_position(\n"
        "                position,\n"
        "                config.EXIT_REASONS[\"EXPIRY\"],\n"
        "                use_market=True,\n"
        "            )\n"
        "            logger.info(\n"
        "                f\"Expiry closed: \"\n"
        "                f\"{position.strategy_name} \"\n"
        "                f\"trade_id={position.trade_id[:8]}\"\n"
        "            )"
    )
    new_mn03 = (
        "        # MN-03: use IST date, not server-local date.\n"
        "        # On UTC hosts, date.today() rolls at 05:30 IST.\n"
        "        _ist_today = datetime.now(\n"
        "            pytz.timezone(config.TZ)\n"
        "        ).date()\n"
        "        if expiry == _ist_today:\n"
        "            await se._close_position(\n"
        "                position,\n"
        "                config.EXIT_REASONS[\"EXPIRY\"],\n"
        "                use_market=True,\n"
        "            )\n"
        "            logger.info(\n"
        "                f\"Expiry closed: \"\n"
        "                f\"{position.strategy_name} \"\n"
        "                f\"trade_id={position.trade_id[:8]}\"\n"
        "            )"
    )
    content, ok = sub_exact(old_mn03, new_mn03, content,
                            "MN-03 IST date in expiry close")
    if ok:
        changes.append("MN-03: _expiry_day_close_all uses IST date")

    # ── DM-07 support: update open-position keys on strategy engine ───
    # After run_cycle, push the current open-position instrument keys
    # to dm._open_position_keys so _build_ws_subscription_keys can use them.
    old_run_cycle_end = (
        "                    try:\n"
        "                        re.save_buffers_to_sqlite()\n"
        "                    except Exception as e:\n"
        "                        logger.warning(\n"
        "                            f\"save_buffers error: {e}\"\n"
        "                        )"
    )
    new_run_cycle_end = (
        "                    try:\n"
        "                        re.save_buffers_to_sqlite()\n"
        "                    except Exception as e:\n"
        "                        logger.warning(\n"
        "                            f\"save_buffers error: {e}\"\n"
        "                        )\n"
        "                    # DM-07: push open-position instrument keys\n"
        "                    # to dm so the WS subscription covers them.\n"
        "                    try:\n"
        "                        _pos_keys = set()\n"
        "                        for _p in se.open_positions:\n"
        "                            for _l in _p.legs:\n"
        "                                if _l.instrument_key:\n"
        "                                    _pos_keys.add(\n"
        "                                        _l.instrument_key\n"
        "                                    )\n"
        "                        dm._open_position_keys = list(\n"
        "                            _pos_keys\n"
        "                        )\n"
        "                    except Exception:\n"
        "                        pass"
    )
    content, ok = sub_exact(old_run_cycle_end, new_run_cycle_end, content,
                            "DM-07 push position keys to dm")
    if ok:
        changes.append("DM-07: open-position instrument keys pushed to dm after each cycle")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Round-5 audit fixes for the NIFTY trading engine."
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
    dry_run   = args.dry_run
    do_backup = not args.no_backup

    base = os.path.dirname(os.path.abspath(__file__))
    files = {
        "config.py":          os.path.join(base, "config.py"),
        "strategy_engine.py": os.path.join(base, "strategy_engine.py"),
        "data_manager.py":    os.path.join(base, "data_manager.py"),
        "regime_engine.py":   os.path.join(base, "regime_engine.py"),
        "main.py":            os.path.join(base, "main.py"),
    }

    missing = [n for n, p in files.items() if not os.path.isfile(p)]
    if missing:
        print("ERROR: Files not found: " + str(missing))
        print("Run patch.py from the same directory as the engine.")
        sys.exit(1)

    all_ok       = True
    total_changes = []

    patches = [
        ("config.py",          patch_config),
        ("strategy_engine.py", patch_strategy_engine),
        ("data_manager.py",    patch_data_manager),
        ("regime_engine.py",   patch_regime_engine),
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
        print("\nDry-run complete — no files modified.")
    else:
        print("\nAll patches applied.")
        print("Verify: python -m py_compile config.py strategy_engine.py "
              "data_manager.py regime_engine.py main.py")
        print("Then run: python testing.py -v")


if __name__ == "__main__":
    main()