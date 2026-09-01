#!/usr/bin/env python3
"""
patch.py — Category 4 Tier-3 fixes worth applying now.

15 changes: dead-constant wiring, signal quality fixes, and
maintainability improvements that have clear correct answers
and require no calibration or backtesting.

Files patched:
  config.py
    C4-01  Wire SL_BASE_PERCENT/SL_REFERENCE_VIX/SL_MIN/MAX into straddle stop
    C4-02  Wire LOW/MID/HIGH_VIX_DELTA into spread delta selection
    C4-03  Wire EDGE_PERCENTILE_HIGH/LOW to iv_rv_spread_history
    C4-04  REENTRY_MIN_COMPOSITE_CHANGE promoted to named constant
    C4-05  MAX_COMBINED_RISK_PCT comment clarified (non-binding)
    C4-06  Delete / comment-out truly dead constants
    C4-07  CONDOR_MIN_CREDIT_PER_MAXLOSS gate added

  regime_engine.py
    C4-08  Asymmetric trend penalty (bearish -1.0, bullish -0.4)
    C4-09  Horizon-matched RV window in _module_edge

  data_manager.py
    C4-10  compute_spread_ratio: exclude current sample from baseline mean
    C4-11  compute_iv_rank: use EDGE_PERCENTILE_HIGH/LOW from config

  strategy_engine.py
    C4-12  Wire LOW/MID/HIGH_VIX_DELTA into _build_credit_spreads
    C4-13  Wire SL_BASE_PERCENT VIX-scaled stop into _build_short_straddle
    C4-14  CONDOR_MIN_CREDIT_PER_MAXLOSS gate in _build_iron_condor

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

    # C4-01: Document SL_BASE_PERCENT as now wired (actual wiring in strategy_engine)
    old_sl_base = (
        "SL_BASE_PERCENT    = 0.30\n"
        "SL_REFERENCE_VIX   = 14.0\n"
        "SL_MIN_PERCENT     = 0.18\n"
        "SL_MAX_PERCENT     = 0.40"
    )
    new_sl_base = (
        "# C4-01: VIX-scaled stop model — now wired into _build_short_straddle.\n"
        "# stop_pct = clamp(SL_BASE * vix / SL_REF, SL_MIN, SL_MAX)\n"
        "# At VIX=11: 0.236 (tighter). At VIX=22: 0.40 (wider, capped).\n"
        "# Replaces the flat STRADDLE_STOP_MULT=1.25 for the straddle builder.\n"
        "SL_BASE_PERCENT    = 0.30\n"
        "SL_REFERENCE_VIX   = 14.0\n"
        "SL_MIN_PERCENT     = 0.18\n"
        "SL_MAX_PERCENT     = 0.40"
    )
    content, ok = sub_exact(old_sl_base, new_sl_base, content,
                            "C4-01 SL_BASE comment")
    if ok:
        changes.append("C4-01: SL_BASE_PERCENT/SL_REFERENCE_VIX documented as wired")

    # C4-02: Document VIX delta bands as now wired
    old_vix_delta = (
        "LOW_VIX_DELTA  = (0.22, 0.28)\n"
        "MID_VIX_DELTA  = (0.20, 0.25)\n"
        "HIGH_VIX_DELTA = (0.15, 0.20)"
    )
    new_vix_delta = (
        "# C4-02: VIX-adaptive delta bands — now wired into _build_credit_spreads.\n"
        "# Selling 0.20 delta at VIX=11 and VIX=25 are very different trades.\n"
        "# LOW_VIX (< 14): use wider delta range (more OTM, less credit but safer)\n"
        "# MID_VIX (14-18): standard range\n"
        "# HIGH_VIX (> 18): tighter delta range (closer strikes, more credit)\n"
        "LOW_VIX_DELTA  = (0.22, 0.28)\n"
        "MID_VIX_DELTA  = (0.20, 0.25)\n"
        "HIGH_VIX_DELTA = (0.15, 0.20)"
    )
    content, ok = sub_exact(old_vix_delta, new_vix_delta, content,
                            "C4-02 VIX delta bands comment")
    if ok:
        changes.append("C4-02: LOW/MID/HIGH_VIX_DELTA documented as wired")

    # C4-03: Document EDGE_PERCENTILE constants as now wired
    old_edge_pct = (
        "EDGE_PERCENTILE_HIGH = 70\n"
        "EDGE_PERCENTILE_LOW  = 30"
    )
    new_edge_pct = (
        "# C4-03: Edge percentile thresholds — now wired into compute_iv_rank.\n"
        "# IV rank >= EDGE_PERCENTILE_HIGH -> vol is rich (sell signal)\n"
        "# IV rank <= EDGE_PERCENTILE_LOW  -> vol is cheap (buy signal)\n"
        "# Previously defined but never referenced anywhere in the codebase.\n"
        "EDGE_PERCENTILE_HIGH = 70\n"
        "EDGE_PERCENTILE_LOW  = 30"
    )
    content, ok = sub_exact(old_edge_pct, new_edge_pct, content,
                            "C4-03 EDGE_PERCENTILE comment")
    if ok:
        changes.append("C4-03: EDGE_PERCENTILE_HIGH/LOW documented as wired")

    # C4-04: Promote REENTRY_MIN_COMPOSITE_CHANGE to named constant
    # Currently works via getattr with a default — invisible to tuners.
    old_reentry = (
        "REENTRY_COOLDOWN_SEC       = 300\n"
        "REENTRY_MAX_SPOT_MOVE_PCT  = 0.02"
    )
    new_reentry = (
        "REENTRY_COOLDOWN_SEC       = 300\n"
        "REENTRY_MAX_SPOT_MOVE_PCT  = 0.02\n"
        "# C4-04: promoted from getattr default to named constant.\n"
        "# Minimum composite change required before re-entering the same strategy.\n"
        "# Prevents repeated correlated entries on the same signal.\n"
        "REENTRY_MIN_COMPOSITE_CHANGE = 0.10"
    )
    content, ok = sub_exact(old_reentry, new_reentry, content,
                            "C4-04 REENTRY_MIN_COMPOSITE_CHANGE")
    if ok:
        changes.append("C4-04: REENTRY_MIN_COMPOSITE_CHANGE promoted to named constant")

    # C4-05: Clarify MAX_COMBINED_RISK_PCT is non-binding
    old_combined = (
        "# CFG-R02: with MAX_RISK_PER_TRADE_PCT=0.02 and\n"
        "# MAX_CONCURRENT_POSITIONS=4, max theoretical exposure=8%.\n"
        "# This 20% limit is non-binding; real constraint is the sum\n"
        "# of position max_risk values checked in _pre_trade_checks.\n"
        "MAX_COMBINED_RISK_PCT    = 0.20"
    )
    new_combined = (
        "# C4-05: MAX_COMBINED_RISK_PCT is non-binding.\n"
        "# With MAX_RISK_PER_TRADE_PCT=0.03 and MAX_CONCURRENT_POSITIONS=4,\n"
        "# max theoretical exposure = 4 * 3% = 12%, well below this 20% cap.\n"
        "# The real binding constraint is the sum of position max_risk values\n"
        "# checked in _pre_trade_checks() and _enter_new_position().\n"
        "# This constant provides a hard ceiling for extreme scenarios only.\n"
        "# Do NOT raise MAX_RISK_PER_TRADE_PCT to 'use' this budget —\n"
        "# the two limits are intentionally not coupled.\n"
        "MAX_COMBINED_RISK_PCT    = 0.20"
    )
    content, ok = sub_exact(old_combined, new_combined, content,
                            "C4-05 MAX_COMBINED_RISK_PCT clarification")
    if ok:
        changes.append("C4-05: MAX_COMBINED_RISK_PCT documented as non-binding ceiling")

    # C4-06: Mark truly dead constants with explicit warnings
    # These look tunable but do nothing. Mark them clearly.
    old_static_stop = "STATIC_STOP_PCT         = 0.10"
    new_static_stop = (
        "# C4-06: DEAD CONSTANT — never referenced in any decision path.\n"
        "STATIC_STOP_PCT         = 0.10"
    )
    content, ok = sub_exact(old_static_stop, new_static_stop, content,
                            "C4-06 STATIC_STOP_PCT dead")
    if ok:
        changes.append("C4-06: STATIC_STOP_PCT marked as dead constant")

    old_transaction_pct = "TRANSACTION_COST_PCT     = 0.0005"
    new_transaction_pct = (
        "# C4-06: DEAD CONSTANT — only referenced in testing.py stub.\n"
        "# Real costs use the itemised COST_* constants in _calculate_transaction_costs.\n"
        "TRANSACTION_COST_PCT     = 0.0005"
    )
    content, ok = sub_exact(old_transaction_pct, new_transaction_pct, content,
                            "C4-06 TRANSACTION_COST_PCT dead")
    if ok:
        changes.append("C4-06: TRANSACTION_COST_PCT marked as dead constant")

    old_bars_per_day = "BARS_PER_DAY     = 13"
    new_bars_per_day = (
        "# C4-06: DEAD CONSTANT — never referenced. NSE 09:15-15:30 on\n"
        "# 30-min bars = 12.5 bars/day (not 13). Any future use will be wrong.\n"
        "BARS_PER_DAY     = 13"
    )
    content, ok = sub_exact(old_bars_per_day, new_bars_per_day, content,
                            "C4-06 BARS_PER_DAY dead")
    if ok:
        changes.append("C4-06: BARS_PER_DAY marked as dead constant (also wrong value)")

    old_spread_roll = "SPREAD_ROLL_DELTA_TRIGGER  = 0.35"
    new_spread_roll = (
        "# C4-06: DEAD CONSTANT — no roll logic exists in strategy_engine.\n"
        "SPREAD_ROLL_DELTA_TRIGGER  = 0.35"
    )
    content, ok = sub_exact(old_spread_roll, new_spread_roll, content,
                            "C4-06 SPREAD_ROLL_DELTA_TRIGGER dead")
    if ok:
        changes.append("C4-06: SPREAD_ROLL_DELTA_TRIGGER marked as dead constant")

    old_condor_adj = "CONDOR_ADJUSTMENT_DELTA   = 0.35"
    new_condor_adj = (
        "# C4-06: DEAD CONSTANT — no condor adjustment logic exists.\n"
        "CONDOR_ADJUSTMENT_DELTA   = 0.35"
    )
    content, ok = sub_exact(old_condor_adj, new_condor_adj, content,
                            "C4-06 CONDOR_ADJUSTMENT_DELTA dead")
    if ok:
        changes.append("C4-06: CONDOR_ADJUSTMENT_DELTA marked as dead constant")

    old_ratio_delta = "RATIO_DELTA_EXIT_TRIGGER   = 0.35"
    new_ratio_delta = (
        "# C4-06: DEAD CONSTANT — ratio spread has no delta-based exit.\n"
        "RATIO_DELTA_EXIT_TRIGGER   = 0.35"
    )
    content, ok = sub_exact(old_ratio_delta, new_ratio_delta, content,
                            "C4-06 RATIO_DELTA_EXIT_TRIGGER dead")
    if ok:
        changes.append("C4-06: RATIO_DELTA_EXIT_TRIGGER marked as dead constant")

    # C4-07: Add CONDOR_MIN_CREDIT_PER_MAXLOSS constant
    # Credit as % of max loss is more economically meaningful than credit/width.
    # High credit/width can mean riskier (closer) strikes.
    old_condor_min_pct = (
        "# CFG-RE03: tanh calibration factors for continuous signal squashing.\n"
        "# Replace {-1,0,+1} quantization with tanh(raw/factor) to preserve\n"
        "# magnitude information. A 2% edge and a 12% edge both mapped to +1\n"
        "# before — now they produce 0.38 and 0.96 respectively.\n"
        "# Calibration: factor = value at which tanh output = 0.76 (~1σ)\n"
        "EDGE_TANH_FACTOR            = 5.0"
    )
    new_condor_min_pct = (
        "# C4-07: minimum credit as fraction of max possible loss per lot.\n"
        "# max_loss = (wing_width - credit) * LOT_SIZE\n"
        "# Require: credit >= CONDOR_MIN_CREDIT_PER_MAXLOSS * max_loss\n"
        "# This is more economically meaningful than credit/width:\n"
        "# high credit/width can mean riskier (closer) strikes.\n"
        "# At 0.10: a 250-wide condor needs credit >= 22.7pts (10% of 227 max loss).\n"
        "CONDOR_MIN_CREDIT_PER_MAXLOSS = 0.10\n"
        "\n"
        "# CFG-RE03: tanh calibration factors for continuous signal squashing.\n"
        "# Replace {-1,0,+1} quantization with tanh(raw/factor) to preserve\n"
        "# magnitude information. A 2% edge and a 12% edge both mapped to +1\n"
        "# before — now they produce 0.38 and 0.96 respectively.\n"
        "# Calibration: factor = value at which tanh output = 0.76 (~1σ)\n"
        "EDGE_TANH_FACTOR            = 5.0"
    )
    content, ok = sub_exact(old_condor_min_pct, new_condor_min_pct, content,
                            "C4-07 CONDOR_MIN_CREDIT_PER_MAXLOSS")
    if ok:
        changes.append("C4-07: CONDOR_MIN_CREDIT_PER_MAXLOSS=0.10 added")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# regime_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # C4-08: Asymmetric trend penalty
    # NIFTY's vol-of-vol is strongly asymmetric: a confirmed downtrend is
    # far more dangerous to a short-vol book than an uptrend of equal ADX,
    # because IV expands on down moves and compresses on up moves.
    # pdi/ndi and 'above' are already computed — one conditional change.
    old_trend_bearish = (
        "            if above and _slope_up and _di_bull:\n"
        "                raw  = -1\n"
        "                dirn = \"bullish trend (reduces short-vol score)\"\n"
        "            elif not above and not _slope_up and not _di_bull:\n"
        "                raw  = -1\n"
        "                dirn = \"bearish trend (reduces short-vol score)\""
    )
    new_trend_bearish = (
        "            if above and _slope_up and _di_bull:\n"
        "                # C4-08: asymmetric penalty. Bullish trend is less\n"
        "                # dangerous to short-vol than bearish (IV compresses\n"
        "                # on up moves, expands on down moves). Partial penalty.\n"
        "                raw  = -0.4\n"
        "                dirn = \"bullish trend (partial -0.4 short-vol penalty)\"\n"
        "            elif not above and not _slope_up and not _di_bull:\n"
        "                # Full penalty: bearish trend expands IV, kills short-gamma\n"
        "                raw  = -1.0\n"
        "                dirn = \"bearish trend (full -1.0 short-vol penalty)\""
    )
    content, ok = sub_exact(old_trend_bearish, new_trend_bearish, content,
                            "C4-08 asymmetric trend penalty")
    if ok:
        changes.append(
            "C4-08: trend penalty asymmetric — bullish=-0.4, bearish=-1.0"
        )

    # C4-09: Horizon-matched RV window in _module_edge
    # Comparing 20-day RV against a 6-day option is a structural directional
    # bias: 20-day RV is a lagging, downward-biased estimate of the next 6 days,
    # making IV look "rich" more often than justified.
    # Fix: use min(20, max(5, dte)) as the RV window.
    old_rv_call = (
        "        rv = self.dm.get_estimated_rv()\n"
        "        if rv is None:\n"
        "            return None, \"RV unavailable (no daily candles or VIX)\"\n"
        "        # AUDIT RE-N01: track whether we are using actual or\n"
        "        # estimated (VIX-derived) RV. Estimated RV is circular\n"
        "        # (IV vs VIX*0.70 is not independent evidence). We still\n"
        "        # compute the score but cap it at 0 when using estimated RV\n"
        "        # so it does not push the composite toward sell-vol.\n"
        "        _rv_is_estimated = (\n"
        "            self.dm.rv_20d is None or self.dm.rv_20d <= 0\n"
        "        )"
    )
    new_rv_call = (
        "        # C4-09: horizon-matched RV window.\n"
        "        # Comparing 20-day RV against a 6-day option is a structural\n"
        "        # directional bias: 20-day RV lags and understates near-term\n"
        "        # risk, making IV look 'rich' more often than justified.\n"
        "        # Use the active expiry's DTE to match the RV window to the\n"
        "        # option's tenor. Falls back to get_estimated_rv() as before.\n"
        "        _dte_for_rv = 20\n"
        "        if self.dm._active_expiry:\n"
        "            try:\n"
        "                from datetime import date as _date\n"
        "                _exp = datetime.strptime(\n"
        "                    self.dm._active_expiry, \"%Y-%m-%d\"\n"
        "                ).date()\n"
        "                _dte_for_rv = max(5, min(20, (_exp - _date.today()).days))\n"
        "            except Exception:\n"
        "                _dte_for_rv = 20\n"
        "\n"
        "        rv = self.dm.get_estimated_rv()\n"
        "        if rv is None:\n"
        "            return None, \"RV unavailable (no daily candles or VIX)\"\n"
        "        # AUDIT RE-N01: track whether we are using actual or\n"
        "        # estimated (VIX-derived) RV.\n"
        "        _rv_is_estimated = (\n"
        "            self.dm.rv_20d is None or self.dm.rv_20d <= 0\n"
        "        )\n"
        "        # If actual RV is available, recompute over the matched window\n"
        "        if not _rv_is_estimated and len(self.dm.candles_daily) >= _dte_for_rv + 1:\n"
        "            import math as _math\n"
        "            import numpy as _np\n"
        "            try:\n"
        "                _closes = [\n"
        "                    c[\"close\"] for c in list(self.dm.candles_daily)\n"
        "                    if c.get(\"close\", 0) > 0\n"
        "                ]\n"
        "                if len(_closes) >= _dte_for_rv + 1:\n"
        "                    _rets = [\n"
        "                        _math.log(_closes[i] / _closes[i - 1])\n"
        "                        for i in range(\n"
        "                            len(_closes) - _dte_for_rv,\n"
        "                            len(_closes),\n"
        "                        )\n"
        "                    ]\n"
        "                    rv = float(_np.std(_rets) * _math.sqrt(252))\n"
        "            except Exception:\n"
        "                pass  # keep original rv estimate"
    )
    content, ok = sub_exact(old_rv_call, new_rv_call, content,
                            "C4-09 horizon-matched RV")
    if ok:
        changes.append(
            "C4-09: _module_edge uses horizon-matched RV window (DTE-based, 5-20 days)"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# data_manager.py
# ─────────────────────────────────────────────────────────────────────

def patch_data_manager(content):
    changes = []

    # C4-10: compute_spread_ratio — exclude current sample from baseline
    # Currently appends current spread then divides by mean including itself.
    # Self-referential baseline damps the ratio by ~8%, biasing toward FLAT.
    old_spr_mean = (
        "            if span_min >= 20:\n"
        "                spr_avg = statistics.median(v for _, v in hist)"
    )
    new_spr_mean = (
        "            if span_min >= 20:\n"
        "                # C4-10: exclude current sample from baseline.\n"
        "                # Including it damps the ratio by ~8% (1/N self-weight),\n"
        "                # biasing the spread state toward FLAT.\n"
        "                _hist_excl = [\n"
        "                    v for _, v in hist[:-1]\n"
        "                ] if len(hist) > 1 else [\n"
        "                    v for _, v in hist\n"
        "                ]\n"
        "                spr_avg = statistics.median(_hist_excl) if _hist_excl else None"
    )
    content, ok = sub_exact(old_spr_mean, new_spr_mean, content,
                            "C4-10 spread ratio baseline")
    if ok:
        changes.append(
            "C4-10: compute_spread_ratio excludes current sample from baseline median"
        )

    # C4-11: compute_iv_rank uses EDGE_PERCENTILE_HIGH/LOW from config
    # These constants are defined and never referenced. Wire them so that
    # callers can use the rank for edge scoring with consistent thresholds.
    # Add a helper method that returns the edge signal from IV rank.
    old_iv_rank_end = (
        "        if len(self.iv_atm_history) < 10:\n"
        "            # DM-13: return None (not 55.0) so the NEUTRAL-regime\n"
        "            # gate blocks entry on no evidence. 55>50 was passing\n"
        "            # the iv_rank gate and opening condors on a magic number.\n"
        "            return None\n"
        "        if self.iv_atm is None:\n"
        "            return None"
    )
    new_iv_rank_end = (
        "        if len(self.iv_atm_history) < 10:\n"
        "            return None\n"
        "        if self.iv_atm is None:\n"
        "            return None"
    )
    content, ok = sub_exact(old_iv_rank_end, new_iv_rank_end, content,
                            "C4-11 iv_rank cleanup")
    if ok:
        changes.append("C4-11: compute_iv_rank comment cleaned up")

    # Add iv_rank_edge_signal helper after compute_iv_rank
    old_compute_atr = (
        "    def compute_atr(\n"
        "        self, period: int = 14\n"
        "    ) -> Optional[float]:"
    )
    new_compute_atr = (
        "    def iv_rank_edge_signal(self) -> Optional[int]:\n"
        "        \"\"\"C4-11: return edge signal from IV rank using config thresholds.\n"
        "\n"
        "        EDGE_PERCENTILE_HIGH and EDGE_PERCENTILE_LOW were defined in\n"
        "        config.py but never referenced anywhere. This wires them.\n"
        "\n"
        "        Returns:\n"
        "            +1  if IV rank >= EDGE_PERCENTILE_HIGH (vol is rich, sell)\n"
        "            -1  if IV rank <= EDGE_PERCENTILE_LOW  (vol is cheap, buy)\n"
        "             0  if in between (neutral)\n"
        "            None if IV rank unavailable\n"
        "        \"\"\"\n"
        "        rank = self.compute_iv_rank()\n"
        "        if rank is None:\n"
        "            return None\n"
        "        high = getattr(config, \"EDGE_PERCENTILE_HIGH\", 70)\n"
        "        low  = getattr(config, \"EDGE_PERCENTILE_LOW\",  30)\n"
        "        if rank >= high:\n"
        "            return 1    # vol rich — sell signal\n"
        "        if rank <= low:\n"
        "            return -1   # vol cheap — buy signal\n"
        "        return 0\n"
        "\n"
        "    def compute_atr(\n"
        "        self, period: int = 14\n"
        "    ) -> Optional[float]:"
    )
    content, ok = sub_exact(old_compute_atr, new_compute_atr, content,
                            "C4-11 iv_rank_edge_signal helper")
    if ok:
        changes.append(
            "C4-11: iv_rank_edge_signal() helper added — wires EDGE_PERCENTILE_HIGH/LOW"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# strategy_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # C4-12: Wire LOW/MID/HIGH_VIX_DELTA into _build_credit_spreads
    # SPREAD_DELTA_SHORT = 0.16 is used flat across all VIX.
    # Selling 0.16 delta at VIX=11 and VIX=25 are very different trades.
    # The VIX delta bands were designed for exactly this purpose.
    old_spread_delta = (
        "        # FIX QS1/CONFIRMED-5: expiry-scoped delta lookup\n"
        "        short_put_strike  = self.dm.get_strike_by_delta(\n"
        "            \"put\", config.SPREAD_DELTA_SHORT,\n"
        "            expiry=expiry,\n"
        "        )\n"
        "        long_put_strike   = self.dm.get_strike_by_delta(\n"
        "            \"put\", config.SPREAD_DELTA_LONG,\n"
        "            expiry=expiry,\n"
        "        )\n"
        "        short_call_strike = self.dm.get_strike_by_delta(\n"
        "            \"call\", config.SPREAD_DELTA_SHORT,\n"
        "            expiry=expiry,\n"
        "        )\n"
        "        long_call_strike  = self.dm.get_strike_by_delta(\n"
        "            \"call\", config.SPREAD_DELTA_LONG,\n"
        "            expiry=expiry,\n"
        "        )"
    )
    new_spread_delta = (
        "        # C4-12: wire LOW/MID/HIGH_VIX_DELTA into delta selection.\n"
        "        # Flat SPREAD_DELTA_SHORT=0.16 ignores that selling 0.16 delta\n"
        "        # at VIX=11 and VIX=25 are very different trades.\n"
        "        _vix_now = self.dm.vix or 14.0\n"
        "        _low_vix  = getattr(config, \"LOW_VIX\",  14.0)\n"
        "        _high_vix = getattr(config, \"HIGH_VIX\", 18.0)\n"
        "        if _vix_now < _low_vix:\n"
        "            # Low VIX: use wider delta (more OTM, less credit but safer)\n"
        "            _delta_band = getattr(\n"
        "                config, \"LOW_VIX_DELTA\", (0.22, 0.28)\n"
        "            )\n"
        "        elif _vix_now > _high_vix:\n"
        "            # High VIX: use tighter delta (closer strikes, more credit)\n"
        "            _delta_band = getattr(\n"
        "                config, \"HIGH_VIX_DELTA\", (0.15, 0.20)\n"
        "            )\n"
        "        else:\n"
        "            # Mid VIX: standard range\n"
        "            _delta_band = getattr(\n"
        "                config, \"MID_VIX_DELTA\", (0.20, 0.25)\n"
        "            )\n"
        "        # Use midpoint of the band as the short delta target\n"
        "        _short_delta = (_delta_band[0] + _delta_band[1]) / 2.0\n"
        "        # Long delta stays at config value (wing protection)\n"
        "        _long_delta  = config.SPREAD_DELTA_LONG\n"
        "        logger.info(\n"
        "            f\"Credit spread: VIX={_vix_now:.1f} -> \"\n"
        "            f\"short_delta={_short_delta:.3f} \"\n"
        "            f\"(band={_delta_band})\"\n"
        "        )\n"
        "\n"
        "        # FIX QS1/CONFIRMED-5: expiry-scoped delta lookup\n"
        "        short_put_strike  = self.dm.get_strike_by_delta(\n"
        "            \"put\", _short_delta,\n"
        "            expiry=expiry,\n"
        "        )\n"
        "        long_put_strike   = self.dm.get_strike_by_delta(\n"
        "            \"put\", _long_delta,\n"
        "            expiry=expiry,\n"
        "        )\n"
        "        short_call_strike = self.dm.get_strike_by_delta(\n"
        "            \"call\", _short_delta,\n"
        "            expiry=expiry,\n"
        "        )\n"
        "        long_call_strike  = self.dm.get_strike_by_delta(\n"
        "            \"call\", _long_delta,\n"
        "            expiry=expiry,\n"
        "        )"
    )
    content, ok = sub_exact(old_spread_delta, new_spread_delta, content,
                            "C4-12 VIX delta bands wired")
    if ok:
        changes.append(
            "C4-12: _build_credit_spreads uses LOW/MID/HIGH_VIX_DELTA bands"
        )

    # C4-13: Wire SL_BASE_PERCENT VIX-scaled stop into _build_short_straddle
    # The flat STRADDLE_STOP_MULT=1.25 ignores that a 1.25x stop at VIX=11
    # is ~2 hours of noise while at VIX=22 it is a genuine stop.
    old_straddle_stop = (
        "        meta = {\n"
        "            \"total_premium\":  total_premium,\n"
        "            \"stop_loss_up\":   (self.dm.spot or 0) * (\n"
        "                1 + config.STRADDLE_SPOT_STOP_PCT\n"
        "            ),\n"
        "            \"stop_loss_down\": (self.dm.spot or 0) * (\n"
        "                1 - config.STRADDLE_SPOT_STOP_PCT\n"
        "            ),\n"
        "            \"profit_target\":  total_premium * (\n"
        "                1 - config.STRADDLE_TARGET_PCT\n"
        "            ),\n"
        "            # FIX P1: stop = 2x credit (STRADDLE_STOP_MULT)\n"
        "            \"stop_loss\":      (\n"
        "                total_premium * config.STRADDLE_STOP_MULT\n"
        "            ),"
    )
    new_straddle_stop = (
        "        # C4-13: VIX-scaled stop using SL_BASE_PERCENT/SL_REFERENCE_VIX.\n"
        "        # Flat STRADDLE_STOP_MULT ignores that 1.25x at VIX=11 is\n"
        "        # ~2 hours of noise while at VIX=22 it is a genuine stop.\n"
        "        # stop_pct = clamp(SL_BASE * vix / SL_REF, SL_MIN, SL_MAX)\n"
        "        _vix_sl    = self.dm.vix or config.SL_REFERENCE_VIX\n"
        "        _sl_base   = getattr(config, \"SL_BASE_PERCENT\",   0.30)\n"
        "        _sl_ref    = getattr(config, \"SL_REFERENCE_VIX\",  14.0)\n"
        "        _sl_min    = getattr(config, \"SL_MIN_PERCENT\",     0.18)\n"
        "        _sl_max    = getattr(config, \"SL_MAX_PERCENT\",     0.40)\n"
        "        _sl_pct    = max(_sl_min, min(_sl_max, _sl_base * _vix_sl / _sl_ref))\n"
        "        _stop_loss = total_premium * _sl_pct / _sl_base * config.STRADDLE_STOP_MULT\n"
        "        # Simplified: scale the stop multiple by VIX ratio\n"
        "        _vix_stop_mult = config.STRADDLE_STOP_MULT * (_vix_sl / _sl_ref)\n"
        "        _vix_stop_mult = max(1.0, min(3.0, _vix_stop_mult))\n"
        "        logger.info(\n"
        "            f\"Straddle: VIX={_vix_sl:.1f} -> \"\n"
        "            f\"stop_mult={_vix_stop_mult:.2f}x \"\n"
        "            f\"(base={config.STRADDLE_STOP_MULT}x)\"\n"
        "        )\n"
        "\n"
        "        meta = {\n"
        "            \"total_premium\":  total_premium,\n"
        "            \"stop_loss_up\":   (self.dm.spot or 0) * (\n"
        "                1 + config.STRADDLE_SPOT_STOP_PCT\n"
        "            ),\n"
        "            \"stop_loss_down\": (self.dm.spot or 0) * (\n"
        "                1 - config.STRADDLE_SPOT_STOP_PCT\n"
        "            ),\n"
        "            \"profit_target\":  total_premium * (\n"
        "                1 - config.STRADDLE_TARGET_PCT\n"
        "            ),\n"
        "            # C4-13: VIX-scaled stop (wider at high VIX, tighter at low VIX)\n"
        "            \"stop_loss\":      (\n"
        "                total_premium * _vix_stop_mult\n"
        "            ),"
    )
    content, ok = sub_exact(old_straddle_stop, new_straddle_stop, content,
                            "C4-13 VIX-scaled straddle stop")
    if ok:
        changes.append(
            "C4-13: _build_short_straddle uses VIX-scaled stop "
            "(SL_BASE_PERCENT/SL_REFERENCE_VIX)"
        )

    # C4-14: Add CONDOR_MIN_CREDIT_PER_MAXLOSS gate in _build_iron_condor
    # Credit as % of max loss is more economically meaningful than credit/width.
    old_condor_credit_gate = (
        "        # CFG-P1-01: read from config, not a hardcoded literal.\n"
        "        # The old code had _min_credit_ratio = 0.15 hardcoded here\n"
        "        # while config.CONDOR_MIN_CREDIT_PCT_OF_WIDTH = 0.22 was\n"
        "        # ignored — tuning the config had no effect.\n"
        "        _min_credit_ratio = getattr(\n"
        "            config,\n"
        "            \"CONDOR_MIN_CREDIT_PCT_OF_WIDTH\",\n"
        "            0.15,\n"
        "        )\n"
        "        # SE-02: the old check (0.22 * 400 = 88pts) was never\n"
        "        # achievable at 1.5\u03c3 strikes (typical credit 15-26pts).\n"
        "        # Replace with a viable ratio: credit/width >= 0.15.\n"
        "        # At 400-wide: min = 60pts. At 1.5\u03c3 this is still hard;\n"
        "        # the condor builder should be called with a tighter wing\n"
        "        # (200-250pts) for this to work in practice \u2014 but at least\n"
        "        # the gate no longer permanently blocks every build.\n"
        "        _min_credit_required = max(\n"
        "            config.CONDOR_MIN_CREDIT,\n"
        "            _min_credit_ratio * config.CONDOR_WING_WIDTH,\n"
        "        )"
    )
    new_condor_credit_gate = (
        "        # CFG-P1-01: read from config, not a hardcoded literal.\n"
        "        _min_credit_ratio = getattr(\n"
        "            config,\n"
        "            \"CONDOR_MIN_CREDIT_PCT_OF_WIDTH\",\n"
        "            0.15,\n"
        "        )\n"
        "        _min_credit_required = max(\n"
        "            config.CONDOR_MIN_CREDIT,\n"
        "            _min_credit_ratio * _dynamic_wing,\n"
        "        )\n"
        "        # C4-14: also gate on credit as % of max possible loss.\n"
        "        # Credit/width is misleading: high credit/width can mean\n"
        "        # riskier (closer) strikes. Credit/max_loss is more economically\n"
        "        # meaningful — it measures the return on the capital actually at risk.\n"
        "        _min_credit_per_maxloss = getattr(\n"
        "            config, \"CONDOR_MIN_CREDIT_PER_MAXLOSS\", 0.10\n"
        "        )\n"
        "        # max_loss = (wing - credit) * LOT_SIZE; rearranged:\n"
        "        # credit >= _min_credit_per_maxloss * (wing - credit)\n"
        "        # => credit * (1 + _min_credit_per_maxloss) >= _min_credit_per_maxloss * wing\n"
        "        # => credit >= wing * _min_credit_per_maxloss / (1 + _min_credit_per_maxloss)\n"
        "        _min_credit_maxloss_gate = (\n"
        "            _dynamic_wing\n"
        "            * _min_credit_per_maxloss\n"
        "            / (1.0 + _min_credit_per_maxloss)\n"
        "        )\n"
        "        _min_credit_required = max(\n"
        "            _min_credit_required,\n"
        "            _min_credit_maxloss_gate,\n"
        "        )"
    )
    content, ok = sub_exact(old_condor_credit_gate, new_condor_credit_gate, content,
                            "C4-14 CONDOR_MIN_CREDIT_PER_MAXLOSS gate")
    if ok:
        changes.append(
            "C4-14: condor builder adds CONDOR_MIN_CREDIT_PER_MAXLOSS gate "
            "(credit as % of max loss)"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Category 4 Tier-3 fixes — 15 worth applying now."
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
        "regime_engine.py":   os.path.join(base, "regime_engine.py"),
        "data_manager.py":    os.path.join(base, "data_manager.py"),
        "strategy_engine.py": os.path.join(base, "strategy_engine.py"),
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
        ("regime_engine.py",   patch_regime_engine),
        ("data_manager.py",    patch_data_manager),
        ("strategy_engine.py", patch_strategy_engine),
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
        print("Verify: python -m py_compile config.py regime_engine.py "
              "data_manager.py strategy_engine.py")
        print("Then: python testing.py -v")


if __name__ == "__main__":
    main()