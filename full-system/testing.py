#!/usr/bin/env python3
"""
patch.py — Applies all audit-identified fixes across the trading engine.

Fixes applied:
  1. strategy_engine.py — #1.1 CRITICAL: Straddle max_risk uses 2x credit
  2. main.py            — #1.2 HIGH: Weekly reset wired up in main loop
  3. strategy_engine.py — #1.3 MEDIUM: Condor/CreditSpread premium stop enforced
  4. regime_engine.py   — #2.2/#3.1 HIGH: Import weights/thresholds from config
  5. config.py          — #2.1 MEDIUM: Docstring contradictions corrected
  6. config.py          — #2.3 LOW: ADX_PERIOD wired to regime_engine default (14)
  7. config.py          — #2.4 LOW: CB_LEVEL_3_PCT comment corrected
  8. main.py            — #3.1 MEDIUM-HIGH: DTE window aligned (28->30)

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
    bak = path + f".bak_{ts}"
    shutil.copy2(path, bak)
    print(f"  Backup: {bak}")


def verify_syntax(path, content):
    try:
        ast.parse(content)
        return True, None
    except SyntaxError as e:
        return False, str(e)


def apply_patch(path, original, patched, dry_run, do_backup):
    if original == patched:
        print(f"  [SKIP] {path} — no changes needed")
        return True

    ok, err = verify_syntax(path, patched)
    if not ok:
        print(f"  [ERROR] {path} — syntax error after patch: {err}")
        return False

    if dry_run:
        # Show a simple line-count diff summary
        orig_lines = original.splitlines()
        new_lines = patched.splitlines()
        print(f"  [DRY-RUN] {path} — "
              f"{len(orig_lines)} -> {len(new_lines)} lines")
        # Show changed lines (first 20)
        shown = 0
        for i, (a, b) in enumerate(
            zip(orig_lines, new_lines), 1
        ):
            if a != b and shown < 20:
                print(f"    L{i}: - {a.rstrip()}")
                print(f"    L{i}: + {b.rstrip()}")
                shown += 1
        if len(new_lines) > len(orig_lines):
            for j, line in enumerate(
                new_lines[len(orig_lines):], len(orig_lines) + 1
            ):
                if shown < 20:
                    print(f"    L{j}: + {line.rstrip()}")
                    shown += 1
        return True

    if do_backup:
        backup_file(path)

    write_file(path, patched)
    print(f"  [OK] {path} — patched successfully")
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
        "        # FIX NS4: max_risk = 1x credit × LOT_SIZE\n"
        "        max_risk = total_premium * 1.0 * config.LOT_SIZE"
    )
    new_1_1 = (
        "        # FIX NS4 (AUDIT #1.1): max_risk = stop level × LOT_SIZE\n"
        "        # Stop is 2x credit (STRADDLE_STOP_MULT=2.0), so size to that\n"
        "        # to prevent systematic over-leverage vs all capital guardrails.\n"
        "        max_risk = (\n"
        "            total_premium\n"
        "            * config.STRADDLE_STOP_MULT\n"
        "            * config.LOT_SIZE\n"
        "        )"
    )
    if old_1_1 in content:
        content = content.replace(old_1_1, new_1_1)
        changes.append("Fix #1.1: straddle max_risk = 2x credit")
    else:
        print("  [WARN] Fix #1.1: straddle max_risk target not found "
              "— check manually")

    # ── Fix #1.3: Add premium stop to Iron Condor branch ─────────
    # Target the condor branch in _check_stop_loss.
    # We find the block that checks CONDOR_TESTED_SIDE_BUFFER
    # and prepend a premium-based stop check.
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
        "            # AUDIT #1.3: premium-based stop (2x credit)\n"
        "            # Catches IV-expansion moves that don't breach a\n"
        "            # strike by CONDOR_TESTED_SIDE_BUFFER but still\n"
        "            # exceed the designed loss threshold.\n"
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
        changes.append(
            "Fix #1.3: condor/credit-spread premium stop added"
        )
    else:
        print("  [WARN] Fix #1.3: condor stop target not found "
              "— check manually")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Patch 2 — main.py
# Fix #1.2: Wire up weekly reset in main loop
# Fix #3.1: Align _ensure_term_structure_expiry DTE window to 30-45
# ─────────────────────────────────────────────────────────────────────

def patch_main(content):
    changes = []

    # ── Fix #1.2: Add weekly reset to daily-reset block ──────────
    # The existing daily reset block looks like:
    #   if today != last_trading_date:
    #       se.reset_daily_state()
    #       last_trading_date     = today
    #       eod_cancel_sweep_done = False
    #       eod_done_today        = False   # PATCH
    #       cached_greeks         = None
    #       data_refresh_complete = True
    #       logger.info(f"New trading day: {today}")
    # We add a weekly reset check inside this block.
    old_daily_reset = (
        "            if today != last_trading_date:\n"
        "                se.reset_daily_state()\n"
        "                last_trading_date     = today\n"
        "                eod_cancel_sweep_done = False\n"
        "                eod_done_today        = False   # PATCH\n"
        "                cached_greeks         = None\n"
        "                data_refresh_complete = True\n"
        "                logger.info(f\"New trading day: {today}\")"
    )
    new_daily_reset = (
        "            if today != last_trading_date:\n"
        "                se.reset_daily_state()\n"
        "                last_trading_date     = today\n"
        "                eod_cancel_sweep_done = False\n"
        "                eod_done_today        = False   # PATCH\n"
        "                cached_greeks         = None\n"
        "                data_refresh_complete = True\n"
        "                logger.info(f\"New trading day: {today}\")\n"
        "                # AUDIT #1.2: weekly reset — wire up the\n"
        "                # previously-defined-but-never-called\n"
        "                # reset_weekly_state() so CB_LEVEL_3 resets\n"
        "                # each week instead of latching forever.\n"
        "                # NSE weekly cycle: Tuesday expiry, so reset\n"
        "                # on Wednesday (weekday==2) each week.\n"
        "                if today.weekday() == 2:\n"
        "                    _last_reset = getattr(\n"
        "                        se, '_last_weekly_reset', None\n"
        "                    )\n"
        "                    if _last_reset != today:\n"
        "                        se.reset_weekly_state()\n"
        "                        logger.info(\n"
        "                            f\"Weekly state reset \"\n"
        "                            f\"(Wednesday: {today})\"\n"
        "                        )"
    )
    if old_daily_reset in content:
        content = content.replace(old_daily_reset, new_daily_reset)
        changes.append("Fix #1.2: weekly reset wired to Wednesday")
    else:
        print("  [WARN] Fix #1.2: daily-reset block not found "
              "— check manually")

    # ── Fix #3.1: Align _ensure_term_structure_expiry defaults ───
    # Old: target_dte_low: int = 28, target_dte_high: int = 42
    # New: target_dte_low: int = 30, target_dte_high: int = 45
    # This aligns the fetcher's window with _compute_forward_iv()'s
    # acceptance window (30 <= dte <= 45).
    old_dte_window = (
        "    target_dte_low: int = 28,\n"
        "    target_dte_high: int = 42,"
    )
    new_dte_window = (
        "    target_dte_low: int = 30,   "
        "# AUDIT #3.1: aligned with _compute_forward_iv 30-45\n"
        "    target_dte_high: int = 45,  "
        "# AUDIT #3.1: was 42, _compute_forward_iv accepts <=45"
    )
    if old_dte_window in content:
        content = content.replace(old_dte_window, new_dte_window)
        changes.append(
            "Fix #3.1: _ensure_term_structure_expiry DTE 28-42 -> 30-45"
        )
    else:
        print("  [WARN] Fix #3.1: DTE window target not found "
              "— check manually")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Patch 3 — regime_engine.py
# Fix #2.2: Import weights and thresholds from config
#           so config.py is the genuine single source of truth.
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # ── Replace hardcoded WEIGHTS dict ───────────────────────────
    # Old:
    #   WEIGHTS = {"vol": 0.30, "edge": 0.30, "trend": 0.25, "flow": 0.15}
    # New: read from config
    old_weights = (
        'WEIGHTS          = {"vol": 0.30, "edge": 0.30, '
        '"trend": 0.25, "flow": 0.15}'
    )
    new_weights = (
        "# AUDIT #2.2: weights now read from config so tuning\n"
        "# config.WEIGHT_* actually takes effect at runtime.\n"
        "def _build_weights():\n"
        "    return {\n"
        "        \"vol\":   config.WEIGHT_VOL,\n"
        "        \"edge\":  config.WEIGHT_EDGE,\n"
        "        \"trend\": config.WEIGHT_TREND,\n"
        "        \"flow\":  config.WEIGHT_FLOW,\n"
        "    }\n"
        "WEIGHTS = _build_weights()"
    )
    if old_weights in content:
        content = content.replace(old_weights, new_weights)
        changes.append("Fix #2.2: WEIGHTS reads from config")
    else:
        print("  [WARN] Fix #2.2: WEIGHTS target not found "
              "— check manually")

    # ── Replace hardcoded map_regime thresholds ───────────────────
    # Old standalone function (module level):
    #   def map_regime(x: float) -> str:
    #       if x > 0.45: return "STRONG_SELL_VOL"
    #       if x >= 0.15: return "MILD_SELL_VOL"
    #       if x > -0.15: return "NEUTRAL"
    #       if x >= -0.45: return "BUY_VOL"
    #       return "STRONG_BUY_VOL"
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
        "    AUDIT #2.2: thresholds now read from config so\n"
        "    config.STRONG_SELL_THRESHOLD etc. actually take effect.\n"
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
        changes.append(
            "Fix #2.2: map_regime() reads thresholds from config"
        )
    else:
        print("  [WARN] Fix #2.2: map_regime target not found "
              "— check manually")

    # ── Wire _map_regime() method to use config thresholds ────────
    # The RegimeEngine class also has its own _map_regime() method
    # with the same hardcoded values. Update it too.
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
        changes.append(
            "Fix #2.2: _map_regime() method reads from config"
        )
    else:
        print("  [WARN] Fix #2.2: _map_regime() method target "
              "not found — check manually")

    # ── Wire composite aggregation to use config WEIGHTS ─────────
    # Old (in _refresh_locked):
    #   composite = sum(
    #       WEIGHTS[m] * self._conf[m]
    #       for m in MODULES
    #   )
    # New: rebuild WEIGHTS from config each refresh so live
    # changes to config take effect without restart.
    old_composite = (
        "            composite = sum(\n"
        "                WEIGHTS[m] * self._conf[m]\n"
        "                for m in MODULES\n"
        "            )"
    )
    new_composite = (
        "            # AUDIT #2.2: rebuild weights from config\n"
        "            # each cycle so config.WEIGHT_* tuning is live.\n"
        "            _live_weights = _build_weights()\n"
        "            composite = sum(\n"
        "                _live_weights[m] * self._conf[m]\n"
        "                for m in MODULES\n"
        "            )"
    )
    if old_composite in content:
        content = content.replace(old_composite, new_composite)
        changes.append(
            "Fix #2.2: composite aggregation uses live config weights"
        )
    else:
        print("  [WARN] Fix #2.2: composite aggregation target "
              "not found — check manually")

    # ── Wire ADX threshold to config ─────────────────────────────
    # Old: ADX_TREND = 20.0  (hardcoded, config.ADX_TREND_THRESHOLD unused)
    # New: use config value
    old_adx_trend = (
        "ADX_TREND        = 20.0   "
        "# PATCH: was 25.0 — recalibrated for 30-min bars "
        "(live logs showed ADX rarely exceeding ~16-24 even in "
        "directional phases; 25 was likely tuned for daily bars)"
    )
    new_adx_trend = (
        "# AUDIT #2.2/#2.3: ADX_TREND now reads from config.\n"
        "# config.ADX_TREND_THRESHOLD = 20 (calibrated for 30-min bars).\n"
        "ADX_TREND = config.ADX_TREND_THRESHOLD"
    )
    if old_adx_trend in content:
        content = content.replace(old_adx_trend, new_adx_trend)
        changes.append(
            "Fix #2.2/#2.3: ADX_TREND reads from config"
        )
    else:
        # Try a more flexible match since the comment may wrap
        pattern = (
            r"ADX_TREND\s*=\s*20\.0\s*"
            r"# PATCH: was 25\.0[^\n]*"
        )
        replacement = (
            "ADX_TREND = config.ADX_TREND_THRESHOLD"
            "  # AUDIT #2.2/#2.3: reads from config"
        )
        new_content, n = re.subn(pattern, replacement, content)
        if n > 0:
            content = new_content
            changes.append(
                "Fix #2.2/#2.3: ADX_TREND reads from config (regex)"
            )
        else:
            print("  [WARN] Fix #2.2/#2.3: ADX_TREND target not found "
                  "— check manually")

    # ── Wire SKEW thresholds to config ────────────────────────────
    old_skew_z_steep = (
        "SKEW_Z_STEEP     =  1.5    "
        "# z > 1.5  -> fear    (Skew_Score = -1)"
    )
    new_skew_z_steep = (
        "SKEW_Z_STEEP = config.SKEW_ZSCORE_FEAR"
        "       # AUDIT #2.2: reads from config"
    )
    if old_skew_z_steep in content:
        content = content.replace(old_skew_z_steep, new_skew_z_steep)
        changes.append("Fix #2.2: SKEW_Z_STEEP reads from config")
    else:
        print("  [WARN] Fix #2.2: SKEW_Z_STEEP target not found "
              "— check manually")

    old_skew_z_flat = (
        "SKEW_Z_FLAT      = -1.0   "
        "# z < -1.0 -> complacent (Skew_Score = +1)"
    )
    new_skew_z_flat = (
        "SKEW_Z_FLAT = config.SKEW_ZSCORE_COMPLACENT"
        "  # AUDIT #2.2: reads from config"
    )
    if old_skew_z_flat in content:
        content = content.replace(old_skew_z_flat, new_skew_z_flat)
        changes.append("Fix #2.2: SKEW_Z_FLAT reads from config")
    else:
        print("  [WARN] Fix #2.2: SKEW_Z_FLAT target not found "
              "— check manually")

    # ── Wire EDGE thresholds to config ────────────────────────────
    old_edge_rich = (
        "EDGE_RICH        = 5.0    "
        "# IV - RV > 5  -> rich (Edge_Score = +1)"
    )
    new_edge_rich = (
        "EDGE_RICH = config.EDGE_RICH"
        "        # AUDIT #2.2: reads from config"
    )
    if old_edge_rich in content:
        content = content.replace(old_edge_rich, new_edge_rich)
        changes.append("Fix #2.2: EDGE_RICH reads from config")
    else:
        print("  [WARN] Fix #2.2: EDGE_RICH target not found "
              "— check manually")

    old_edge_cheap = (
        "EDGE_CHEAP       = 0.0    "
        "# IV - RV < 0  -> cheap (Edge_Score = -1)"
    )
    new_edge_cheap = (
        "EDGE_CHEAP = config.EDGE_CHEAP"
        "       # AUDIT #2.2: reads from config"
    )
    if old_edge_cheap in content:
        content = content.replace(old_edge_cheap, new_edge_cheap)
        changes.append("Fix #2.2: EDGE_CHEAP reads from config")
    else:
        print("  [WARN] Fix #2.2: EDGE_CHEAP target not found "
              "— check manually")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Patch 4 — config.py
# Fix #2.1: Correct docstring contradictions
# Fix #2.3: Wire ADX_PERIOD to the actual value used (14)
# Fix #2.4: Correct CB_LEVEL_3_PCT comment
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
        changes.append(
            "Fix #2.1a: PERSISTENCE_READINGS docstring corrected"
        )
    else:
        print("  [WARN] Fix #2.1a: PERSISTENCE_READINGS doc target "
              "not found — check manually")

    # ── Fix #2.1b: STRONG_SELL_THRESHOLD docstring ────────────────
    old_strong_sell_doc = (
        "  LIVE: STRONG_SELL_THRESHOLD=0.30 (recalibrated VIX=11)"
    )
    new_strong_sell_doc = (
        "  LIVE: STRONG_SELL_THRESHOLD=0.45 (confirmed value in file)"
    )
    if old_strong_sell_doc in content:
        content = content.replace(
            old_strong_sell_doc, new_strong_sell_doc
        )
        changes.append(
            "Fix #2.1b: STRONG_SELL_THRESHOLD docstring corrected"
        )
    else:
        print("  [WARN] Fix #2.1b: STRONG_SELL_THRESHOLD doc target "
              "not found — check manually")

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
        changes.append(
            "Fix #2.1c: weight redistribution docstring corrected"
        )
    else:
        print("  [WARN] Fix #2.1c: weight doc target not found "
              "— check manually")

    # ── Fix #2.3: ADX_PERIOD — add note that engine uses 14 ───────
    old_adx_period = (
        "ADX_PERIOD          = 26\n"
        "ADX_TREND_THRESHOLD = 20   "
        "# PATCH: was 25 (NOTE: regime_engine.py has its own "
        "hardcoded ADX_TREND constant, patched separately — "
        "this config value is NOT currently read by that module)"
    )
    new_adx_period = (
        "# AUDIT #2.3: ADX_PERIOD=14 matches the reference algorithm\n"
        "# (regime_engine.adx14 default n=14). The old value of 26\n"
        "# was never passed anywhere and has been corrected here.\n"
        "ADX_PERIOD          = 14\n"
        "ADX_TREND_THRESHOLD = 20   "
        "# AUDIT #2.2: now read by regime_engine.py via ADX_TREND"
    )
    if old_adx_period in content:
        content = content.replace(old_adx_period, new_adx_period)
        changes.append(
            "Fix #2.3: ADX_PERIOD corrected to 14"
        )
    else:
        # Try simpler match
        old_adx_simple = "ADX_PERIOD          = 26"
        new_adx_simple = (
            "ADX_PERIOD          = 14"
            "  # AUDIT #2.3: corrected from 26 (unused) to 14 (actual)"
        )
        if old_adx_simple in content:
            content = content.replace(
                old_adx_simple, new_adx_simple
            )
            changes.append(
                "Fix #2.3: ADX_PERIOD corrected to 14 (simple)"
            )
        else:
            print("  [WARN] Fix #2.3: ADX_PERIOD target not found "
                  "— check manually")

    # ── Fix #2.4: CB_LEVEL_3_PCT comment ──────────────────────────
    old_cb3_comment = (
        "CB_LEVEL_3_PCT = 0.08   "
        "# PATCH: was 0.10 (identical to CB_LEVEL_4_PCT, "
        "causing overlapping triggers)"
    )
    new_cb3_comment = (
        "# AUDIT #2.4: CB_LEVEL_3_PCT=0.08 is the CURRENT correct\n"
        "# value. It was previously 0.10 (same as CB_LEVEL_4_PCT,\n"
        "# which caused overlapping triggers). Now 0.08 < 0.10,\n"
        "# so L3 (50% reduction) fires before L4 (full stop).\n"
        "CB_LEVEL_3_PCT = 0.08"
    )
    if old_cb3_comment in content:
        content = content.replace(old_cb3_comment, new_cb3_comment)
        changes.append(
            "Fix #2.4: CB_LEVEL_3_PCT comment clarified"
        )
    else:
        print("  [WARN] Fix #2.4: CB_LEVEL_3_PCT target not found "
              "— check manually")

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

    dry_run   = args.dry_run
    do_backup = not args.no_backup

    base = os.path.dirname(os.path.abspath(__file__))

    files = {
        "strategy_engine.py": os.path.join(
            base, "strategy_engine.py"
        ),
        "main.py":            os.path.join(base, "main.py"),
        "regime_engine.py":   os.path.join(
            base, "regime_engine.py"
        ),
        "config.py":          os.path.join(base, "config.py"),
    }

    # Verify all files exist before starting
    missing = [
        name for name, path in files.items()
        if not os.path.isfile(path)
    ]
    if missing:
        print(f"ERROR: Files not found: {missing}")
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
        print(f"\n{'='*60}")
        print(f"Patching: {name}")
        print(f"{'='*60}")

        original = read_file(path)
        patched, changes = patch_fn(original)

        if changes:
            for c in changes:
                print(f"  + {c}")
            total_changes.extend(changes)
        else:
            print("  (no changes from this patch function)")

        ok = apply_patch(
            path, original, patched, dry_run, do_backup
        )
        if not ok:
            all_ok = False

    print(f"\n{'='*60}")
    print("PATCH SUMMARY")
    print(f"{'='*60}")
    print(f"Total changes applied: {len(total_changes)}")
    for c in total_changes:
        print(f"  ✓ {c}")

    if not all_ok:
        print("\nERROR: One or more patches failed — "
              "review warnings above.")
        sys.exit(1)

    if dry_run:
        print("\nDry-run complete — no files were modified.")
    else:
        print("\nAll patches applied successfully.")
        print("Run your test suite to verify correctness.")


if __name__ == "__main__":
    main()