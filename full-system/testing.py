#!/usr/bin/env python3
"""
patch.py — Applies all audit-identified fixes across the trading engine.

Fixes applied:
  1. strategy_engine.py — #1.1 CRITICAL: Straddle max_risk uses 2x credit
  2. strategy_engine.py — #1.2 HIGH: Weekly reset already in run_cycle()
                          (no main.py change needed — existing Monday reset
                           in run_cycle() is correct and self-contained)
  3. strategy_engine.py — #1.3 MEDIUM: Condor/CreditSpread premium stop enforced
  4. regime_engine.py   — #2.2 HIGH: Import weights/thresholds from config
  5. config.py          — #2.1 MEDIUM: Docstring contradictions corrected
  6. config.py          — #2.3 LOW: ADX_PERIOD corrected to 14
  7. config.py          — #2.4 LOW: CB_LEVEL_3_PCT comment corrected
  8. main.py            — #3.1 MEDIUM-HIGH: DTE window aligned (28->30, 42->45)

Run:
    python patch.py [--dry-run] [--no-backup]

Options:
    --dry-run    Show diffs without writing files
    --no-backup  Skip creating .bak backup files
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
        print("  [SKIP] " + path + " — no changes needed")
        return True

    ok, err = verify_syntax(path, patched)
    if not ok:
        print("  [ERROR] " + path + " — syntax error after patch: " + str(err))
        return False

    if dry_run:
        orig_lines = original.splitlines()
        new_lines = patched.splitlines()
        print(
            "  [DRY-RUN] " + path + " — "
            + str(len(orig_lines)) + " -> " + str(len(new_lines)) + " lines"
        )
        shown = 0
        max_show = len(orig_lines) if len(orig_lines) > len(new_lines) else len(new_lines)
        for i in range(max_show):
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
    print("  [OK] " + path + " — patched successfully")
    return True


# ─────────────────────────────────────────────────────────────────────
# Patch 1 — strategy_engine.py
# Fix #1.1: Straddle max_risk = 2x credit (not 1x)
# Fix #1.3: Condor/CreditSpread premium-based stop enforced
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # ── Fix #1.1: max_risk in _build_short_straddle ───────────────
    # Old: max_risk = total_premium * 1.0 * config.LOT_SIZE
    # New: max_risk = total_premium * config.STRADDLE_STOP_MULT * config.LOT_SIZE
    old_1_1 = (
        "        # FIX NS4: max_risk = 1x credit \u00d7 LOT_SIZE\n"
        "        max_risk = total_premium * 1.0 * config.LOT_SIZE"
    )
    new_1_1 = (
        "        # AUDIT #1.1: max_risk sized to the actual stop level\n"
        "        # (2x credit via STRADDLE_STOP_MULT=2.0) so lot sizing\n"
        "        # and portfolio risk gates are not understated by 2x.\n"
        "        max_risk = (\n"
        "            total_premium\n"
        "            * config.STRADDLE_STOP_MULT\n"
        "            * config.LOT_SIZE\n"
        "        )"
    )
    if old_1_1 in content:
        content = content.replace(old_1_1, new_1_1)
        changes.append("Fix #1.1: straddle max_risk = 2x credit (STRADDLE_STOP_MULT)")
    else:
        print("  [WARN] Fix #1.1: straddle max_risk target not found — check manually")

    # ── Fix #1.3: Add premium stop to Iron Condor / Credit Spreads branch ──
    # We locate the elif block for IRON_CONDOR / CREDIT_SPREADS in
    # _check_stop_loss and prepend a premium-based stop check before
    # the existing tested-side logic.
    old_condor_stop = (
        "        elif strategy in [\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "        ]:\n"
        "            if self.dm.spot is None:\n"
        "                return False\n"
        "            short_call = self._get_short_strike(\n"
        "                position, \"call\"\n"
        "            )\n"
        "            short_put  = self._get_short_strike(\n"
        "                position, \"put\"\n"
        "            )\n"
        "            if short_call and self.dm.spot >= (\n"
        "                short_call\n"
        "                + config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"call\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                return False\n"
        "            if short_put and self.dm.spot <= (\n"
        "                short_put\n"
        "                - config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"put\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                return False"
    )
    new_condor_stop = (
        "        elif strategy in [\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "        ]:\n"
        "            if self.dm.spot is None:\n"
        "                return False\n"
        "            # AUDIT #1.3: premium-based stop (2x credit received).\n"
        "            # Catches IV-expansion losses that do not breach a\n"
        "            # strike by CONDOR_TESTED_SIDE_BUFFER but still exceed\n"
        "            # the designed loss threshold. Mirrors the same check\n"
        "            # already present for straddle and ratio-spread.\n"
        "            if position.stop_loss and position.stop_loss > 0:\n"
        "                current_premium = (\n"
        "                    self._get_position_current_premium(\n"
        "                        position\n"
        "                    )\n"
        "                )\n"
        "                if current_premium >= position.stop_loss:\n"
        "                    logger.info(\n"
        "                        f\"Condor/Spread premium stop: \"\n"
        "                        f\"current={current_premium:.2f} \"\n"
        "                        f\"stop={position.stop_loss:.2f}\"\n"
        "                    )\n"
        "                    await self._close_position(\n"
        "                        position,\n"
        "                        config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                    )\n"
        "                    return True\n"
        "            short_call = self._get_short_strike(\n"
        "                position, \"call\"\n"
        "            )\n"
        "            short_put  = self._get_short_strike(\n"
        "                position, \"put\"\n"
        "            )\n"
        "            if short_call and self.dm.spot >= (\n"
        "                short_call\n"
        "                + config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"call\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                return False\n"
        "            if short_put and self.dm.spot <= (\n"
        "                short_put\n"
        "                - config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"put\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                return False"
    )
    if old_condor_stop in content:
        content = content.replace(old_condor_stop, new_condor_stop)
        changes.append("Fix #1.3: condor/credit-spread premium stop added")
    else:
        print("  [WARN] Fix #1.3: condor stop target not found — check manually")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Patch 2 — main.py
# Fix #3.1 ONLY: Align _ensure_term_structure_expiry DTE window
#                from 28-42 to 30-45 so it matches the acceptance
#                window in data_manager._compute_forward_iv().
#
# NOTE: No weekly-reset block is added here.
# The existing Monday-based reset in strategy_engine.run_cycle()
# is correct, self-contained, and updates _last_weekly_reset
# properly. Adding a second reset in main.py would cause double
# resets (Monday + Wednesday), erasing Monday-Tuesday P&L from
# the weekly loss breaker's view and clearing cb_level_3_active
# mid-week even when legitimately tripped.
# ─────────────────────────────────────────────────────────────────────

def patch_main(content):
    changes = []

    # ── Fix #3.1: Align _ensure_term_structure_expiry defaults ───
    # Old: target_dte_low: int = 28, target_dte_high: int = 42
    # New: target_dte_low: int = 30, target_dte_high: int = 45
    # _compute_forward_iv() accepts 30 <= dte <= 45.
    # The old fetcher window (28-42) could fetch DTE 28, 29, 43,
    # or 44 which _compute_forward_iv() would then reject, leaving
    # the system on the VIX/100 fallback (term_score always 0).
    old_dte_window = (
        "    target_dte_low: int = 28,\n"
        "    target_dte_high: int = 42,"
    )
    new_dte_window = (
        "    target_dte_low: int = 30,\n"
        "    target_dte_high: int = 45,"
    )
    if old_dte_window in content:
        content = content.replace(old_dte_window, new_dte_window)
        changes.append(
            "Fix #3.1: _ensure_term_structure_expiry DTE window 28-42 -> 30-45"
        )
    else:
        print(
            "  [WARN] Fix #3.1: DTE window target not found — check manually"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Patch 3 — regime_engine.py
# Fix #2.2 HIGH: Replace hardcoded constants with config imports so
#                config.py is the genuine single source of truth.
#
# Constants wired to config:
#   WEIGHTS dict          -> config.WEIGHT_VOL/EDGE/TREND/FLOW
#   map_regime()          -> config.STRONG/MILD/BUY thresholds
#   _map_regime() method  -> same config thresholds
#   composite aggregation -> _build_weights() (live each cycle)
#   ADX_TREND             -> config.ADX_TREND_THRESHOLD
#   SKEW_Z_STEEP          -> config.SKEW_ZSCORE_FEAR
#   SKEW_Z_FLAT           -> config.SKEW_ZSCORE_COMPLACENT
#   EDGE_RICH             -> config.EDGE_RICH
#   EDGE_CHEAP            -> config.EDGE_CHEAP
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # ── Replace hardcoded WEIGHTS dict with config-backed builder ─
    old_weights = (
        'WEIGHTS          = {"vol": 0.30, "edge": 0.30, '
        '"trend": 0.25, "flow": 0.15}'
    )
    new_weights = (
        "# AUDIT #2.2: weights read from config so tuning\n"
        "# config.WEIGHT_* actually takes effect at runtime.\n"
        "def _build_weights():\n"
        "    return {\n"
        '        "vol":   config.WEIGHT_VOL,\n'
        '        "edge":  config.WEIGHT_EDGE,\n'
        '        "trend": config.WEIGHT_TREND,\n'
        '        "flow":  config.WEIGHT_FLOW,\n'
        "    }\n"
        "WEIGHTS = _build_weights()"
    )
    if old_weights in content:
        content = content.replace(old_weights, new_weights)
        changes.append("Fix #2.2: WEIGHTS dict reads from config")
    else:
        print("  [WARN] Fix #2.2: WEIGHTS target not found — check manually")

    # ── Replace module-level map_regime() hardcoded thresholds ────
    old_map_regime = (
        "def map_regime(x: float) -> str:\n"
        "    \"\"\"Reference algorithm regime mapping.\"\"\"\n"
        "    if x > 0.45:\n"
        "        return \"STRONG_SELL_VOL\"\n"
        "    if x >= 0.15:\n"
        "        return \"MILD_SELL_VOL\"\n"
        "    if x > -0.15:\n"
        "        return \"NEUTRAL\"\n"
        "    if x >= -0.45:\n"
        "        return \"BUY_VOL\"\n"
        "    return \"STRONG_BUY_VOL\""
    )
    new_map_regime = (
        "def map_regime(x: float) -> str:\n"
        "    \"\"\"Reference algorithm regime mapping.\n"
        "    AUDIT #2.2: thresholds read from config so\n"
        "    config.STRONG_SELL_THRESHOLD etc. take effect.\n"
        "    \"\"\"\n"
        "    if x > config.STRONG_SELL_THRESHOLD:\n"
        "        return \"STRONG_SELL_VOL\"\n"
        "    if x >= config.MILD_SELL_THRESHOLD:\n"
        "        return \"MILD_SELL_VOL\"\n"
        "    if x > config.MILD_BUY_THRESHOLD:\n"
        "        return \"NEUTRAL\"\n"
        "    if x >= config.STRONG_BUY_THRESHOLD:\n"
        "        return \"BUY_VOL\"\n"
        "    return \"STRONG_BUY_VOL\""
    )
    if old_map_regime in content:
        content = content.replace(old_map_regime, new_map_regime)
        changes.append("Fix #2.2: module-level map_regime() reads from config")
    else:
        print("  [WARN] Fix #2.2: map_regime() target not found — check manually")

    # ── Replace _map_regime() method hardcoded thresholds ─────────
    old_method_map = (
        "    def _map_regime(self, composite: float) -> str:\n"
        "        \"\"\"Reference algorithm regime mapping.\"\"\"\n"
        "        if composite > 0.45:\n"
        "            return config.REGIME_STRONG_SELL\n"
        "        if composite >= 0.15:\n"
        "            return config.REGIME_MILD_SELL\n"
        "        if composite > -0.15:\n"
        "            return config.REGIME_NEUTRAL\n"
        "        if composite >= -0.45:\n"
        "            return config.REGIME_BUY_VOL\n"
        "        return config.REGIME_STRONG_BUY"
    )
    new_method_map = (
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
    if old_method_map in content:
        content = content.replace(old_method_map, new_method_map)
        changes.append("Fix #2.2: _map_regime() method reads from config")
    else:
        print(
            "  [WARN] Fix #2.2: _map_regime() method target not found "
            "— check manually"
        )

    # ── Wire composite aggregation to rebuild weights from config ─
    old_composite = (
        "            composite = sum(\n"
        "                WEIGHTS[m] * self._conf[m]\n"
        "                for m in MODULES\n"
        "            )"
    )
    new_composite = (
        "            # AUDIT #2.2: rebuild weights from config each\n"
        "            # cycle so config.WEIGHT_* tuning is live.\n"
        "            _live_weights = _build_weights()\n"
        "            composite = sum(\n"
        "                _live_weights[m] * self._conf[m]\n"
        "                for m in MODULES\n"
        "            )"
    )
    if old_composite in content:
        content = content.replace(old_composite, new_composite)
        changes.append("Fix #2.2: composite aggregation uses live config weights")
    else:
        print(
            "  [WARN] Fix #2.2: composite aggregation target not found "
            "— check manually"
        )

    # ── Wire ADX_TREND to config.ADX_TREND_THRESHOLD ──────────────
    # The original line has a long inline comment; use regex to
    # match it robustly regardless of exact whitespace.
    pattern_adx = (
        r"ADX_TREND\s*=\s*20\.0\s*"
        r"# PATCH: was 25\.0[^\n]*"
    )
    replacement_adx = (
        "ADX_TREND = config.ADX_TREND_THRESHOLD"
        "  # AUDIT #2.2/#2.3: reads from config (= 20, calibrated for 30-min bars)"
    )
    new_content, n_adx = re.subn(pattern_adx, replacement_adx, content)
    if n_adx > 0:
        content = new_content
        changes.append("Fix #2.2/#2.3: ADX_TREND reads from config")
    else:
        print("  [WARN] Fix #2.2/#2.3: ADX_TREND target not found — check manually")

    # ── Wire SKEW_Z_STEEP to config.SKEW_ZSCORE_FEAR ─────────────
    pattern_skew_steep = (
        r"SKEW_Z_STEEP\s*=\s*1\.5\s*"
        r"# z > 1\.5[^\n]*"
    )
    replacement_skew_steep = (
        "SKEW_Z_STEEP = config.SKEW_ZSCORE_FEAR"
        "        # AUDIT #2.2: reads from config"
    )
    new_content, n_ss = re.subn(
        pattern_skew_steep, replacement_skew_steep, content
    )
    if n_ss > 0:
        content = new_content
        changes.append("Fix #2.2: SKEW_Z_STEEP reads from config")
    else:
        print("  [WARN] Fix #2.2: SKEW_Z_STEEP target not found — check manually")

    # ── Wire SKEW_Z_FLAT to config.SKEW_ZSCORE_COMPLACENT ────────
    pattern_skew_flat = (
        r"SKEW_Z_FLAT\s*=\s*-1\.0\s*"
        r"# z < -1\.0[^\n]*"
    )
    replacement_skew_flat = (
        "SKEW_Z_FLAT = config.SKEW_ZSCORE_COMPLACENT"
        "  # AUDIT #2.2: reads from config"
    )
    new_content, n_sf = re.subn(
        pattern_skew_flat, replacement_skew_flat, content
    )
    if n_sf > 0:
        content = new_content
        changes.append("Fix #2.2: SKEW_Z_FLAT reads from config")
    else:
        print("  [WARN] Fix #2.2: SKEW_Z_FLAT target not found — check manually")

    # ── Wire EDGE_RICH to config.EDGE_RICH ────────────────────────
    pattern_edge_rich = (
        r"EDGE_RICH\s*=\s*5\.0\s*"
        r"# IV - RV > 5[^\n]*"
    )
    replacement_edge_rich = (
        "EDGE_RICH = config.EDGE_RICH"
        "        # AUDIT #2.2: reads from config"
    )
    new_content, n_er = re.subn(
        pattern_edge_rich, replacement_edge_rich, content
    )
    if n_er > 0:
        content = new_content
        changes.append("Fix #2.2: EDGE_RICH reads from config")
    else:
        print("  [WARN] Fix #2.2: EDGE_RICH target not found — check manually")

    # ── Wire EDGE_CHEAP to config.EDGE_CHEAP ─────────────────────
    pattern_edge_cheap = (
        r"EDGE_CHEAP\s*=\s*0\.0\s*"
        r"# IV - RV < 0[^\n]*"
    )
    replacement_edge_cheap = (
        "EDGE_CHEAP = config.EDGE_CHEAP"
        "       # AUDIT #2.2: reads from config"
    )
    new_content, n_ec = re.subn(
        pattern_edge_cheap, replacement_edge_cheap, content
    )
    if n_ec > 0:
        content = new_content
        changes.append("Fix #2.2: EDGE_CHEAP reads from config")
    else:
        print("  [WARN] Fix #2.2: EDGE_CHEAP target not found — check manually")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Patch 4 — config.py
# Fix #2.1: Correct three docstring contradictions
# Fix #2.3: ADX_PERIOD corrected from 26 to 14
# Fix #2.4: CB_LEVEL_3_PCT comment rewritten
# ─────────────────────────────────────────────────────────────────────

def patch_config(content):
    changes = []

    # ── Fix #2.1a: PERSISTENCE_READINGS docstring ─────────────────
    old_persist_doc = (
        "  LIVE: PERSISTENCE_READINGS=2 (was 3) — faster confirmation"
    )
    new_persist_doc = (
        "  LIVE: PERSISTENCE_READINGS=3 (confirmed value in file)"
    )
    if old_persist_doc in content:
        content = content.replace(old_persist_doc, new_persist_doc)
        changes.append("Fix #2.1a: PERSISTENCE_READINGS docstring corrected")
    else:
        print(
            "  [WARN] Fix #2.1a: PERSISTENCE_READINGS doc target "
            "not found — check manually"
        )

    # ── Fix #2.1b: STRONG_SELL_THRESHOLD docstring ────────────────
    old_strong_sell_doc = (
        "  LIVE: STRONG_SELL_THRESHOLD=0.30 (recalibrated VIX=11)"
    )
    new_strong_sell_doc = (
        "  LIVE: STRONG_SELL_THRESHOLD=0.45 (confirmed value in file)"
    )
    if old_strong_sell_doc in content:
        content = content.replace(old_strong_sell_doc, new_strong_sell_doc)
        changes.append("Fix #2.1b: STRONG_SELL_THRESHOLD docstring corrected")
    else:
        print(
            "  [WARN] Fix #2.1b: STRONG_SELL_THRESHOLD doc target "
            "not found — check manually"
        )

    # ── Fix #2.1c: Weight redistribution docstring ────────────────
    old_weight_doc = (
        "  LIVE: Weight redistribution (EDGE=0.40, TREND=0.30)"
    )
    new_weight_doc = (
        "  LIVE: Weights unchanged from reference "
        "(VOL=0.30 EDGE=0.30 TREND=0.25 FLOW=0.15)"
    )
    if old_weight_doc in content:
        content = content.replace(old_weight_doc, new_weight_doc)
        changes.append("Fix #2.1c: weight redistribution docstring corrected")
    else:
        print(
            "  [WARN] Fix #2.1c: weight doc target not found — check manually"
        )

    # ── Fix #2.3: ADX_PERIOD corrected from 26 to 14 ─────────────
    # Use regex to handle the long inline comment robustly.
    pattern_adx_period = r"ADX_PERIOD\s*=\s*26\b"
    replacement_adx_period = (
        "ADX_PERIOD          = 14"
        "  # AUDIT #2.3: corrected from 26 (was unused/stale)"
        " to 14 (matches regime_engine.adx14 default)"
    )
    new_content, n_ap = re.subn(
        pattern_adx_period, replacement_adx_period, content
    )
    if n_ap > 0:
        content = new_content
        changes.append("Fix #2.3: ADX_PERIOD corrected from 26 to 14")
    else:
        print("  [WARN] Fix #2.3: ADX_PERIOD=26 target not found — check manually")

    # Also update the ADX_TREND_THRESHOLD comment to note it is
    # now read by regime_engine.py (after fix #2.2).
    old_adx_trend_comment = (
        "ADX_TREND_THRESHOLD = 20   "
        "# PATCH: was 25 (NOTE: regime_engine.py has its own "
        "hardcoded ADX_TREND constant, patched separately \u2014 "
        "this config value is NOT currently read by that module)"
    )
    new_adx_trend_comment = (
        "ADX_TREND_THRESHOLD = 20"
        "  # AUDIT #2.2: now read by regime_engine.py via ADX_TREND"
    )
    if old_adx_trend_comment in content:
        content = content.replace(
            old_adx_trend_comment, new_adx_trend_comment
        )
        changes.append("Fix #2.2: ADX_TREND_THRESHOLD comment updated")
    else:
        # Try without the unicode em-dash in case encoding differs
        old_adx_trend_comment_ascii = (
            "ADX_TREND_THRESHOLD = 20   "
            "# PATCH: was 25 (NOTE: regime_engine.py has its own "
            "hardcoded ADX_TREND constant, patched separately - "
            "this config value is NOT currently read by that module)"
        )
        if old_adx_trend_comment_ascii in content:
            content = content.replace(
                old_adx_trend_comment_ascii, new_adx_trend_comment
            )
            changes.append(
                "Fix #2.2: ADX_TREND_THRESHOLD comment updated (ascii dash)"
            )
        else:
            # Fallback: regex match on the key part
            pattern_adx_thresh = (
                r"ADX_TREND_THRESHOLD\s*=\s*20\s*"
                r"# PATCH: was 25[^\n]*"
            )
            new_content2, n_at = re.subn(
                pattern_adx_thresh, new_adx_trend_comment, content
            )
            if n_at > 0:
                content = new_content2
                changes.append(
                    "Fix #2.2: ADX_TREND_THRESHOLD comment updated (regex)"
                )
            else:
                print(
                    "  [WARN] Fix #2.2: ADX_TREND_THRESHOLD comment target "
                    "not found — check manually"
                )

    # ── Fix #2.4: CB_LEVEL_3_PCT comment rewritten ────────────────
    # The old comment describes the old bug on the already-fixed line,
    # making it confusing. Replace with a clear forward description.
    old_cb3 = (
        "CB_LEVEL_3_PCT = 0.08   "
        "# PATCH: was 0.10 (identical to CB_LEVEL_4_PCT, "
        "causing overlapping triggers)"
    )
    new_cb3 = (
        "# AUDIT #2.4: CB_LEVEL_3_PCT=0.08 is the current correct\n"
        "# value. It was previously 0.10 (same as CB_LEVEL_4_PCT),\n"
        "# which caused L3 and L4 to trigger simultaneously. Now\n"
        "# 0.08 < 0.10 so L3 (50% reduction) fires before L4\n"
        "# (full stop / manual review), as intended.\n"
        "CB_LEVEL_3_PCT = 0.08"
    )
    if old_cb3 in content:
        content = content.replace(old_cb3, new_cb3)
        changes.append("Fix #2.4: CB_LEVEL_3_PCT comment clarified")
    else:
        print(
            "  [WARN] Fix #2.4: CB_LEVEL_3_PCT target not found — check manually"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Apply audit fixes to the trading engine."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip .bak backup files",
    )
    args = parser.parse_args()

    dry_run = args.dry_run
    do_backup = not args.no_backup

    base = os.path.dirname(os.path.abspath(__file__))

    files = {
        "strategy_engine.py": os.path.join(base, "strategy_engine.py"),
        "main.py":            os.path.join(base, "main.py"),
        "regime_engine.py":   os.path.join(base, "regime_engine.py"),
        "config.py":          os.path.join(base, "config.py"),
    }

    # Verify all files exist before starting any patches
    missing = [
        name for name, path in files.items()
        if not os.path.isfile(path)
    ]
    if missing:
        print("ERROR: Files not found: " + str(missing))
        print("Run patch.py from the same directory as the engine.")
        sys.exit(1)

    all_ok = True
    total_changes = []

    patches = [
        ("strategy_engine.py", patch_strategy_engine),
        ("main.py",            patch_main),
        ("regime_engine.py",   patch_regime_engine),
        ("config.py",          patch_config),
    ]

    for name, patch_fn in patches:
        path = files[name]
        print("")
        print("=" * 60)
        print("Patching: " + name)
        print("=" * 60)

        original = read_file(path)
        patched, changes = patch_fn(original)

        if changes:
            for c in changes:
                print("  + " + c)
            total_changes.extend(changes)
        else:
            print("  (no changes produced by this patch function)")

        ok = apply_patch(path, original, patched, dry_run, do_backup)
        if not ok:
            all_ok = False

    print("")
    print("=" * 60)
    print("PATCH SUMMARY")
    print("=" * 60)
    print("Total changes applied: " + str(len(total_changes)))
    for c in total_changes:
        print("  OK  " + c)

    if not all_ok:
        print("")
        print("ERROR: One or more patches failed — review warnings above.")
        sys.exit(1)

    if dry_run:
        print("")
        print("Dry-run complete — no files were modified.")
    else:
        print("")
        print("All patches applied successfully.")
        print("Verify with: python -m py_compile strategy_engine.py "
              "main.py regime_engine.py config.py")


if __name__ == "__main__":
    main()