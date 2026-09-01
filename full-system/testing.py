#!/usr/bin/env python3
"""
patch.py — Applies all audit-identified fixes (Round 1 + Round 2).

Fixes applied
─────────────
strategy_engine.py
  #1.1  CRITICAL : Straddle max_risk = 2x credit (STRADDLE_STOP_MULT)
  #1.3  MEDIUM   : Condor/CreditSpread premium stop enforced
  #N1   CRITICAL : Defensive hedge bypasses generic lot-scaling
  #N2   MEDIUM   : Event strangle upper DTE bound added

main.py
  #3.1  MEDIUM-H : _ensure_term_structure_expiry DTE window 28-42 -> 30-45
  regression fix : Remove the erroneous Wednesday weekly-reset block
                   (only the Monday reset in run_cycle() is correct)

regime_engine.py
  #2.2  HIGH     : Weights / thresholds / ADX / skew / edge constants
                   all read from config (single source of truth)

config.py
  #2.1  MEDIUM   : Three docstring contradictions corrected
  #2.3  LOW      : ADX_PERIOD corrected from 26 to 14
  #2.4  LOW      : CB_LEVEL_3_PCT comment rewritten
  #N2   MEDIUM   : EVENT_STRANGLE_DTE_TARGET / EVENT_STRANGLE_DTE_MAX added

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
        max_idx = max(len(orig_lines), len(new_lines))
        for i in range(max_idx):
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
#
#  #1.1  Straddle max_risk = 2x credit
#  #1.3  Condor/CreditSpread premium stop
#  #N1   Defensive hedge: bypass generic lot-scaling
#  #N2   Event strangle: upper DTE bound
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # ── #1.1 : straddle max_risk ──────────────────────────────────
    old_1_1 = (
        "        # FIX NS4: max_risk = 1x credit \u00d7 LOT_SIZE\n"
        "        max_risk = total_premium * 1.0 * config.LOT_SIZE"
    )
    new_1_1 = (
        "        # AUDIT #1.1: max_risk sized to the actual stop level\n"
        "        # (STRADDLE_STOP_MULT=2.0 x credit) so lot sizing and\n"
        "        # portfolio risk gates are not understated by 2x.\n"
        "        max_risk = (\n"
        "            total_premium\n"
        "            * config.STRADDLE_STOP_MULT\n"
        "            * config.LOT_SIZE\n"
        "        )"
    )
    if old_1_1 in content:
        content = content.replace(old_1_1, new_1_1)
        changes.append("#1.1 straddle max_risk = 2x credit")
    else:
        print("  [WARN] #1.1: straddle max_risk target not found")

    # ── #1.3 : condor / credit-spread premium stop ────────────────
    old_1_3 = (
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
    new_1_3 = (
        "        elif strategy in [\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "        ]:\n"
        "            if self.dm.spot is None:\n"
        "                return False\n"
        "            # AUDIT #1.3: premium-based stop (2x credit).\n"
        "            # Catches IV-expansion losses that do not breach a\n"
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
    if old_1_3 in content:
        content = content.replace(old_1_3, new_1_3)
        changes.append("#1.3 condor/credit-spread premium stop added")
    else:
        print("  [WARN] #1.3: condor stop target not found")

    # ── #N1 : defensive hedge — bypass generic lot-scaling ────────
    #
    # Strategy: set meta["already_sized"] = True in the builder,
    # then skip the leg.qty * lots multiplication in
    # _enter_new_position() for that flag, and force lots=1 in
    # _calculate_lot_size() for STRAT_DEFENSIVE so the guard
    # "if lots < 1: return" never silently drops the hedge.
    #
    # Step N1-a: add already_sized flag to _build_defensive_hedge meta
    old_n1_meta = (
        "        meta = {\n"
        "            \"total_debit\":    (\n"
        "                atm_put_data[\"ltp\"] * hedge_qty\n"
        "            ),\n"
        "            \"max_risk\":       (\n"
        "                atm_put_data[\"ltp\"]\n"
        "                * hedge_qty\n"
        "                * config.LOT_SIZE\n"
        "            ),\n"
        "            \"stop_loss\":      (\n"
        "                atm_put_data[\"ltp\"]\n"
        "                * hedge_qty\n"
        "                * (1 - config.EVENT_STRANGLE_STOP_PCT)\n"
        "            ),\n"
        "            \"profit_target\":  None,\n"
        "            \"exit_dte\":       None,\n"
        "            \"max_hold_date\":  max_hold_date,\n"
        "            \"strategy_type\":  \"LONG\",\n"
        "            \"reduction_legs\": reduction_legs,\n"
        "            \"hedge_qty\":      hedge_qty,\n"
        "        }"
    )
    new_n1_meta = (
        "        meta = {\n"
        "            \"total_debit\":    (\n"
        "                atm_put_data[\"ltp\"] * hedge_qty\n"
        "            ),\n"
        "            \"max_risk\":       (\n"
        "                atm_put_data[\"ltp\"]\n"
        "                * hedge_qty\n"
        "                * config.LOT_SIZE\n"
        "            ),\n"
        "            \"stop_loss\":      (\n"
        "                atm_put_data[\"ltp\"]\n"
        "                * hedge_qty\n"
        "                * (1 - config.EVENT_STRANGLE_STOP_PCT)\n"
        "            ),\n"
        "            \"profit_target\":  None,\n"
        "            \"exit_dte\":       None,\n"
        "            \"max_hold_date\":  max_hold_date,\n"
        "            \"strategy_type\":  \"LONG\",\n"
        "            \"reduction_legs\": reduction_legs,\n"
        "            \"hedge_qty\":      hedge_qty,\n"
        "            # AUDIT #N1: hedge_qty is already the correct\n"
        "            # absolute quantity — skip the generic lot-scaling\n"
        "            # multiplication in _enter_new_position().\n"
        "            \"already_sized\":  True,\n"
        "        }"
    )
    if old_n1_meta in content:
        content = content.replace(old_n1_meta, new_n1_meta)
        changes.append("#N1-a defensive hedge meta already_sized=True")
    else:
        print("  [WARN] #N1-a: defensive hedge meta target not found")

    # Step N1-b: honour already_sized in _enter_new_position
    # Target the lot-scaling loop:
    #   for leg in legs:
    #       leg.qty = leg.qty * lots
    old_n1_scale = (
        "        for leg in legs:\n"
        "            leg.qty = leg.qty * lots"
    )
    new_n1_scale = (
        "        # AUDIT #N1: skip lot-scaling for strategies that\n"
        "        # pre-compute an absolute quantity (e.g. defensive hedge).\n"
        "        _already_sized = meta.get(\"already_sized\", False)\n"
        "        if not _already_sized:\n"
        "            for leg in legs:\n"
        "                leg.qty = leg.qty * lots\n"
        "        else:\n"
        "            # Force lots=1 so downstream position-record\n"
        "            # fields (max_risk scaling etc.) stay consistent.\n"
        "            lots = 1"
    )
    if old_n1_scale in content:
        content = content.replace(old_n1_scale, new_n1_scale)
        changes.append("#N1-b _enter_new_position honours already_sized")
    else:
        print("  [WARN] #N1-b: lot-scaling loop target not found")

    # Step N1-c: _calculate_lot_size returns 1 for STRAT_DEFENSIVE
    # so the "if lots < 1: return" guard never silently drops the hedge.
    # Insert the early-return at the top of _calculate_lot_size.
    old_n1_lotsize = (
        "    def _calculate_lot_size(\n"
        "        self, strategy_name: str, meta: Dict\n"
        "    ) -> int:\n"
        "        max_loss_per_lot = meta.get(\"max_risk\", 0)\n"
        "        if max_loss_per_lot <= 0:\n"
        "            return 0"
    )
    new_n1_lotsize = (
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
    if old_n1_lotsize in content:
        content = content.replace(old_n1_lotsize, new_n1_lotsize)
        changes.append("#N1-c _calculate_lot_size returns 1 for STRAT_DEFENSIVE")
    else:
        print("  [WARN] #N1-c: _calculate_lot_size target not found")

    # ── #N2 : event strangle upper DTE bound ──────────────────────
    # The builder calls get_expiry_by_dte(7, tolerance=3) but never
    # re-validates the returned DTE against an upper bound.
    # We add the guard immediately after the existing expiry fetch.
    old_n2 = (
        "    async def _build_long_strangle(\n"
        "        self,\n"
        "    ) -> Tuple[Optional[List[Leg]], Dict]:\n"
        "        \"\"\"Build long strangle for event volatility.\"\"\"\n"
        "        expiry = self.dm.get_expiry_by_dte(7, tolerance=3)\n"
        "        if expiry is None:\n"
        "            return (None, {})"
    )
    new_n2 = (
        "    async def _build_long_strangle(\n"
        "        self,\n"
        "    ) -> Tuple[Optional[List[Leg]], Dict]:\n"
        "        \"\"\"Build long strangle for event volatility.\"\"\"\n"
        "        expiry = self.dm.get_expiry_by_dte(\n"
        "            config.EVENT_STRANGLE_DTE_TARGET,\n"
        "            tolerance=config.EVENT_STRANGLE_DTE_TARGET - 2,\n"
        "        )\n"
        "        if expiry is None:\n"
        "            return (None, {})\n"
        "        # AUDIT #N2: enforce upper DTE bound so a far-dated\n"
        "        # expiry is never silently used when the intended\n"
        "        # short-dated window is unavailable.\n"
        "        _n2_dte = (\n"
        "            datetime.strptime(expiry, \"%Y-%m-%d\").date()\n"
        "            - date.today()\n"
        "        ).days\n"
        "        if _n2_dte > config.EVENT_STRANGLE_DTE_MAX:\n"
        "            logger.info(\n"
        "                f\"Strangle: expiry {expiry} DTE={_n2_dte} \"\n"
        "                f\"> max={config.EVENT_STRANGLE_DTE_MAX} \"\n"
        "                f\"— skip\"\n"
        "            )\n"
        "            return (None, {})"
    )
    if old_n2 in content:
        content = content.replace(old_n2, new_n2)
        changes.append("#N2 event strangle upper DTE bound added")
    else:
        print("  [WARN] #N2: strangle builder target not found")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Patch 2 — main.py
#
#  #3.1       : _ensure_term_structure_expiry DTE window 28-42 -> 30-45
#  regression : Remove the erroneous Wednesday weekly-reset block
#               introduced by the previous patch round.
#               The Monday reset in strategy_engine.run_cycle() is
#               correct and self-contained; a second reset in main.py
#               would erase Mon-Tue P&L and clear cb_level_3_active
#               mid-week on every Wednesday unconditionally.
# ─────────────────────────────────────────────────────────────────────

def patch_main(content):
    changes = []

    # ── #3.1 : align DTE window ───────────────────────────────────
    old_dte = (
        "    target_dte_low: int = 28,\n"
        "    target_dte_high: int = 42,"
    )
    new_dte = (
        "    target_dte_low: int = 30,\n"
        "    target_dte_high: int = 45,"
    )
    if old_dte in content:
        content = content.replace(old_dte, new_dte)
        changes.append("#3.1 _ensure_term_structure_expiry DTE 28-42 -> 30-45")
    else:
        print("  [WARN] #3.1: DTE window target not found")

    # ── regression fix : remove Wednesday weekly-reset block ──────
    # This block was added by the previous patch but is incorrect:
    # it never updates se._last_weekly_reset so it fires every
    # Wednesday unconditionally, double-resetting weekly_pnl and
    # cb_level_3_active.  The Monday reset in run_cycle() is enough.
    wednesday_block = (
        "                # AUDIT #1.2: weekly reset \u2014 wire up the\n"
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
    if wednesday_block in content:
        content = content.replace(wednesday_block, "")
        changes.append(
            "regression: removed erroneous Wednesday weekly-reset block from main.py"
        )
    else:
        # The block may not be present if the previous patch was
        # never applied — that is fine; nothing to remove.
        pass

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Patch 3 — regime_engine.py
#
#  #2.2 : wire all hardcoded constants to config
#         WEIGHTS, map_regime(), _map_regime(), composite aggregation,
#         ADX_TREND, SKEW_Z_STEEP, SKEW_Z_FLAT, EDGE_RICH, EDGE_CHEAP
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # ── WEIGHTS dict ──────────────────────────────────────────────
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
        changes.append("#2.2 WEIGHTS reads from config")
    else:
        print("  [WARN] #2.2: WEIGHTS target not found")

    # ── module-level map_regime() ─────────────────────────────────
    old_map = (
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
    new_map = (
        "def map_regime(x: float) -> str:\n"
        "    \"\"\"Reference algorithm regime mapping.\n"
        "    AUDIT #2.2: thresholds read from config.\n"
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
    if old_map in content:
        content = content.replace(old_map, new_map)
        changes.append("#2.2 module-level map_regime() reads from config")
    else:
        print("  [WARN] #2.2: map_regime() target not found")

    # ── _map_regime() method ──────────────────────────────────────
    old_method = (
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
    new_method = (
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
    if old_method in content:
        content = content.replace(old_method, new_method)
        changes.append("#2.2 _map_regime() method reads from config")
    else:
        print("  [WARN] #2.2: _map_regime() method target not found")

    # ── composite aggregation uses live weights ───────────────────
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
        changes.append("#2.2 composite aggregation uses live config weights")
    else:
        print("  [WARN] #2.2: composite aggregation target not found")

    # ── ADX_TREND ─────────────────────────────────────────────────
    pattern_adx = (
        r"ADX_TREND\s*=\s*20\.0\s*"
        r"# PATCH: was 25\.0[^\n]*"
    )
    repl_adx = (
        "ADX_TREND = config.ADX_TREND_THRESHOLD"
        "  # AUDIT #2.2/#2.3: reads from config"
    )
    new_content, n = re.subn(pattern_adx, repl_adx, content)
    if n > 0:
        content = new_content
        changes.append("#2.2/#2.3 ADX_TREND reads from config")
    else:
        print("  [WARN] #2.2/#2.3: ADX_TREND target not found")

    # ── SKEW_Z_STEEP ──────────────────────────────────────────────
    pattern_ss = r"SKEW_Z_STEEP\s*=\s*1\.5\s*# z > 1\.5[^\n]*"
    repl_ss = (
        "SKEW_Z_STEEP = config.SKEW_ZSCORE_FEAR"
        "        # AUDIT #2.2: reads from config"
    )
    new_content, n = re.subn(pattern_ss, repl_ss, content)
    if n > 0:
        content = new_content
        changes.append("#2.2 SKEW_Z_STEEP reads from config")
    else:
        print("  [WARN] #2.2: SKEW_Z_STEEP target not found")

    # ── SKEW_Z_FLAT ───────────────────────────────────────────────
    pattern_sf = r"SKEW_Z_FLAT\s*=\s*-1\.0\s*# z < -1\.0[^\n]*"
    repl_sf = (
        "SKEW_Z_FLAT = config.SKEW_ZSCORE_COMPLACENT"
        "  # AUDIT #2.2: reads from config"
    )
    new_content, n = re.subn(pattern_sf, repl_sf, content)
    if n > 0:
        content = new_content
        changes.append("#2.2 SKEW_Z_FLAT reads from config")
    else:
        print("  [WARN] #2.2: SKEW_Z_FLAT target not found")

    # ── EDGE_RICH ─────────────────────────────────────────────────
    pattern_er = r"EDGE_RICH\s*=\s*5\.0\s*# IV - RV > 5[^\n]*"
    repl_er = (
        "EDGE_RICH = config.EDGE_RICH"
        "        # AUDIT #2.2: reads from config"
    )
    new_content, n = re.subn(pattern_er, repl_er, content)
    if n > 0:
        content = new_content
        changes.append("#2.2 EDGE_RICH reads from config")
    else:
        print("  [WARN] #2.2: EDGE_RICH target not found")

    # ── EDGE_CHEAP ────────────────────────────────────────────────
    pattern_ec = r"EDGE_CHEAP\s*=\s*0\.0\s*# IV - RV < 0[^\n]*"
    repl_ec = (
        "EDGE_CHEAP = config.EDGE_CHEAP"
        "       # AUDIT #2.2: reads from config"
    )
    new_content, n = re.subn(pattern_ec, repl_ec, content)
    if n > 0:
        content = new_content
        changes.append("#2.2 EDGE_CHEAP reads from config")
    else:
        print("  [WARN] #2.2: EDGE_CHEAP target not found")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Patch 4 — config.py
#
#  #2.1 : three docstring contradictions corrected
#  #2.3 : ADX_PERIOD corrected from 26 to 14
#  #2.4 : CB_LEVEL_3_PCT comment rewritten
#  #N2  : EVENT_STRANGLE_DTE_TARGET / EVENT_STRANGLE_DTE_MAX added
# ─────────────────────────────────────────────────────────────────────

def patch_config(content):
    changes = []

    # ── #2.1a : PERSISTENCE_READINGS docstring ────────────────────
    old_p = "  LIVE: PERSISTENCE_READINGS=2 (was 3) \u2014 faster confirmation"
    new_p = "  LIVE: PERSISTENCE_READINGS=3 (confirmed value in file)"
    if old_p in content:
        content = content.replace(old_p, new_p)
        changes.append("#2.1a PERSISTENCE_READINGS docstring corrected")
    else:
        # Try ASCII dash variant
        old_p2 = "  LIVE: PERSISTENCE_READINGS=2 (was 3) - faster confirmation"
        if old_p2 in content:
            content = content.replace(old_p2, new_p)
            changes.append("#2.1a PERSISTENCE_READINGS docstring corrected")
        else:
            print("  [WARN] #2.1a: PERSISTENCE_READINGS doc target not found")

    # ── #2.1b : STRONG_SELL_THRESHOLD docstring ───────────────────
    old_s = "  LIVE: STRONG_SELL_THRESHOLD=0.30 (recalibrated VIX=11)"
    new_s = "  LIVE: STRONG_SELL_THRESHOLD=0.45 (confirmed value in file)"
    if old_s in content:
        content = content.replace(old_s, new_s)
        changes.append("#2.1b STRONG_SELL_THRESHOLD docstring corrected")
    else:
        print("  [WARN] #2.1b: STRONG_SELL_THRESHOLD doc target not found")

    # ── #2.1c : weight redistribution docstring ───────────────────
    old_w = "  LIVE: Weight redistribution (EDGE=0.40, TREND=0.30)"
    new_w = (
        "  LIVE: Weights unchanged from reference "
        "(VOL=0.30 EDGE=0.30 TREND=0.25 FLOW=0.15)"
    )
    if old_w in content:
        content = content.replace(old_w, new_w)
        changes.append("#2.1c weight redistribution docstring corrected")
    else:
        print("  [WARN] #2.1c: weight doc target not found")

    # ── #2.3 : ADX_PERIOD 26 -> 14 ───────────────────────────────
    # Use word-boundary regex to avoid partial matches.
    pattern_ap = r"\bADX_PERIOD\s*=\s*26\b"
    repl_ap = (
        "ADX_PERIOD          = 14"
        "  # AUDIT #2.3: corrected from 26 (stale/unused)"
        " to 14 (matches regime_engine.adx14 default)"
    )
    new_content, n = re.subn(pattern_ap, repl_ap, content)
    if n > 0:
        content = new_content
        changes.append("#2.3 ADX_PERIOD corrected from 26 to 14")
    else:
        print("  [WARN] #2.3: ADX_PERIOD=26 target not found")

    # Also update the ADX_TREND_THRESHOLD comment to note it is
    # now read by regime_engine.py (after fix #2.2).
    pattern_at = (
        r"ADX_TREND_THRESHOLD\s*=\s*20\s*"
        r"# PATCH: was 25[^\n]*"
    )
    repl_at = (
        "ADX_TREND_THRESHOLD = 20"
        "  # AUDIT #2.2: now read by regime_engine.py via ADX_TREND"
    )
    new_content, n = re.subn(pattern_at, repl_at, content)
    if n > 0:
        content = new_content
        changes.append("#2.2 ADX_TREND_THRESHOLD comment updated")
    else:
        print("  [WARN] #2.2: ADX_TREND_THRESHOLD comment target not found")

    # ── #2.4 : CB_LEVEL_3_PCT comment ────────────────────────────
    old_cb = (
        "CB_LEVEL_3_PCT = 0.08   "
        "# PATCH: was 0.10 (identical to CB_LEVEL_4_PCT, "
        "causing overlapping triggers)"
    )
    new_cb = (
        "# AUDIT #2.4: CB_LEVEL_3_PCT=0.08 is the current correct\n"
        "# value. Previously 0.10 (same as CB_LEVEL_4_PCT) caused L3\n"
        "# and L4 to trigger simultaneously. Now 0.08 < 0.10 so L3\n"
        "# (50% reduction) fires before L4 (full stop).\n"
        "CB_LEVEL_3_PCT = 0.08"
    )
    if old_cb in content:
        content = content.replace(old_cb, new_cb)
        changes.append("#2.4 CB_LEVEL_3_PCT comment clarified")
    else:
        print("  [WARN] #2.4: CB_LEVEL_3_PCT target not found")

    # ── #N2 : add EVENT_STRANGLE_DTE_TARGET / DTE_MAX ─────────────
    # Insert after the existing EVENT_STRANGLE_MAX_SPREAD_PTS line.
    old_event_spread = (
        "EVENT_STRANGLE_MAX_SPREAD_PTS  = 3"
    )
    new_event_spread = (
        "EVENT_STRANGLE_MAX_SPREAD_PTS  = 3\n"
        "# AUDIT #N2: named DTE constants for event strangle so the\n"
        "# builder can enforce an upper bound (previously a bare\n"
        "# literal 7 with no upper-bound check).\n"
        "EVENT_STRANGLE_DTE_TARGET      = 7   # target days to expiry\n"
        "EVENT_STRANGLE_DTE_MAX         = 14  # reject if DTE > this"
    )
    if old_event_spread in content and "EVENT_STRANGLE_DTE_TARGET" not in content:
        content = content.replace(old_event_spread, new_event_spread)
        changes.append(
            "#N2 EVENT_STRANGLE_DTE_TARGET / EVENT_STRANGLE_DTE_MAX added"
        )
    elif "EVENT_STRANGLE_DTE_TARGET" in content:
        changes.append("#N2 EVENT_STRANGLE_DTE_TARGET already present — skipped")
    else:
        print("  [WARN] #N2: EVENT_STRANGLE_MAX_SPREAD_PTS anchor not found")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Apply audit fixes (Round 1 + Round 2) to the trading engine."
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

    missing = [n for n, p in files.items() if not os.path.isfile(p)]
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
            print("  (no changes produced)")

        ok = apply_patch(path, original, patched, dry_run, do_backup)
        if not ok:
            all_ok = False

    print("")
    print("=" * 60)
    print("PATCH SUMMARY")
    print("=" * 60)
    print("Total changes: " + str(len(total_changes)))
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
        print(
            "Verify: python -m py_compile "
            "strategy_engine.py main.py regime_engine.py config.py"
        )


if __name__ == "__main__":
    main()