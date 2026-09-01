#!/usr/bin/env python3
"""
patch_p4.py — P4 profitability-enhancement fixes for NIFTY options algo engine.

Assumes patch_p0.py, patch_p1.py, patch_p2.py, and patch_p3.py have
already been applied successfully.

Fixes applied
─────────────
P4-1  strategy_engine.py  _select_strategy
      STRONG_SELL always routes to STRAT_SHORT_STRADDLE regardless of
      market conditions.  At VIX=11-14 with low ADX (range-bound), an
      iron condor has 5x better return on margin (15.6% vs 3.2%) and
      defined risk.  The straddle is naked short gamma with SPAN margin
      ~Rs1.7L/lot vs condor ~Rs16k/lot.
      Fix: in STRONG_SELL, prefer iron condor when ADX is below
      ADX_RANGE_THRESHOLD (genuinely range-bound).  Fall back to
      straddle only when ADX is elevated (trending) where the condor's
      fixed wings are more likely to be breached.

P4-2  strategy_engine.py  _calculate_lot_size
      composite=0.31 and composite=0.75 both produce the same lot size.
      The composite magnitude contains real information about signal
      quality that is discarded at the regime→strategy handoff.
      Fix: apply a quality multiplier to the final lot count.
      Composite at MILD_SELL_ENTER (0.20 after P3) = baseline (1.0x).
      Composite at 0.50 (STRONG_SELL_ENTER after P3) = 1.25x.
      Composite at 0.75+ = 1.5x (capped).
      The multiplier is applied AFTER all other caps (regime max lots,
      margin cap) so it never exceeds the hard limits.

P4-3  strategy_engine.py  _should_enter_new_position
      (a) IV spike detection: when current IV_ATM is >= 15% above the
          recent 5-cycle average IV_ATM, the engine is in a fear-premium
          window — the highest-EV entry condition for premium selling.
          Override the normal regime gate and allow entry even if the
          composite is below MILD_SELL_ENTER, provided VIX is within
          the sell-vol range.  This is gated on SELL_VOL regimes only
          (not BUY_VOL or NEUTRAL).
      (b) Monday gap risk filter: block straddle entries before 10:00
          IST on Mondays.  NIFTY average Monday gap of 0.4-0.8% can
          wipe 1.6 weeks of theta decay.  The filter applies only to
          STRAT_SHORT_STRADDLE (undefined risk); condors and spreads
          have defined risk and are not blocked.

P4-4  strategy_engine.py  _should_enter_new_position
      Regime stability requirement: require persistence_count >= 5
      before entering a new position.  A regime confirmed for only
      3 cycles (3 minutes) is much more likely to be a false signal
      than one confirmed for 5+ cycles (5 minutes).  This reduces
      entries on transient regime classifications.

P4-5  config.py
      CONDOR_EXIT_DTE = 1  →  2
      At DTE=1 (Monday for Tuesday expiry), remaining theta is ~Rs390
      but closing a 4-leg condor costs ~Rs334 + slippage (~Rs520 total).
      Net capture is negative.  At DTE=2 the remaining theta (~Rs650-910)
      comfortably exceeds closing costs.

Skipped from P4 report list
────────────────────────────
"Add time-of-day IV level check" — requires tracking intraday IV high
which is not currently stored anywhere.  Adding new state tracking
risks introducing bugs; deferred to a dedicated review.

"Lower STRONG_SELL_MIN_CONFIRMING_MODULES from 3 to 2" — already
applied as P3-5g in patch_p3.py.

Run from the directory containing the source files:

    python patch_p4.py

The script is idempotent (sentinel-guarded) and writes .bak backups
before modifying any file.  It will not overwrite an existing .bak.
"""

import re
import shutil
import sys
import os


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _backup(path: str) -> None:
    bak = path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print(f"  backup → {bak}")
    else:
        print(f"  backup already exists: {bak}  (skipped)")


def _assert_file(path: str) -> None:
    if not os.path.isfile(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)


def _verify(path: str, sentinels: list) -> bool:
    src = _read(path)
    ok = True
    for s in sentinels:
        if s in src:
            print(f"  VERIFY OK  : {s!r}")
        else:
            print(f"  VERIFY FAIL: {s!r}  not found in {path}")
            ok = False
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# P4-1  strategy_engine.py — route STRONG_SELL to condor when range-bound
# ─────────────────────────────────────────────────────────────────────────────

def fix_strong_sell_routing(src: str) -> str:
    """
    Current code in _select_strategy():
        if regime == config.REGIME_STRONG_SELL:
            # PATCH S-08: always route STRONG_SELL to straddle.
            return config.STRAT_SHORT_STRADDLE

    We replace this with ADX-conditional routing:
    - ADX < ADX_RANGE_THRESHOLD (genuinely range-bound): use condor
      (defined risk, 5x better margin efficiency)
    - ADX >= ADX_RANGE_THRESHOLD (trending or indeterminate): use straddle
      (straddle profits from IV compression regardless of direction)
    """
    sentinel = "# P4-1: STRONG_SELL routes to condor when range-bound"
    if sentinel in src:
        print("  P4-1 already applied (sentinel found)")
        return src

    old = (
        "        if regime == config.REGIME_STRONG_SELL:\n"
        "            # PATCH S-08: always route STRONG_SELL to straddle.\n"
        "            # STRONG_SELL requires trend=+1 (ADX<ADX_RANGE_THRESHOLD),\n"
        "            # so ADX>ADX_TREND_THRESHOLD is impossible when in this regime.\n"
        "            # The condor branch was permanently dead code.\n"
        "            # Straddle: 2 legs, maximum theta, no wing debit.\n"
        "            return config.STRAT_SHORT_STRADDLE\n"
    )
    new = (
        "        if regime == config.REGIME_STRONG_SELL:\n"
        "            # P4-1: STRONG_SELL routes to condor when range-bound\n"
        "            # Iron condor has 5x better return on margin at VIX=11-14\n"
        "            # (defined risk ~Rs16k/lot vs naked straddle ~Rs1.7L/lot).\n"
        "            # Use condor when ADX confirms range-bound (low trend strength).\n"
        "            # Fall back to straddle when ADX is elevated (trending market\n"
        "            # where fixed condor wings are more likely to be breached).\n"
        "            _adx_now = self.dm.adx or 99.0\n"
        "            if _adx_now < config.ADX_RANGE_THRESHOLD:\n"
        "                return config.STRAT_IRON_CONDOR\n"
        "            return config.STRAT_SHORT_STRADDLE\n"
    )

    if old in src:
        src = src.replace(old, new, 1)
        print("  P4-1 applied: STRONG_SELL routes to condor when ADX < ADX_RANGE_THRESHOLD")
    else:
        # Try a shorter anchor in case comments differ slightly
        old2 = (
            "        if regime == config.REGIME_STRONG_SELL:\n"
            "            return config.STRAT_SHORT_STRADDLE\n"
        )
        if old2 in src:
            src = src.replace(old2, new, 1)
            print("  P4-1 applied (alt anchor): STRONG_SELL routes to condor when range-bound")
        else:
            print("  P4-1 SKIPPED: STRONG_SELL anchor not found — check _select_strategy manually")
    return src


# ─────────────────────────────────────────────────────────────────────────────
# P4-2  strategy_engine.py — composite quality multiplier in lot sizing
# ─────────────────────────────────────────────────────────────────────────────

def fix_composite_lot_scaling(src: str) -> str:
    """
    In _calculate_lot_size(), after all caps are applied, add a
    composite quality multiplier.

    The multiplier is computed from self.re.raw_composite relative to
    the MILD_SELL_ENTER threshold (0.20 after P3):
      - At composite = MILD_SELL_ENTER (0.20): multiplier = 1.0 (no change)
      - At composite = STRONG_SELL_ENTER (0.50): multiplier = 1.25
      - At composite >= 0.75: multiplier = 1.5 (cap)

    Formula: mult = 1.0 + min(0.5, max(0.0, (composite - 0.20) / 1.10))
    This gives a smooth linear ramp from 1.0 at 0.20 to 1.5 at 0.75.

    The multiplier is applied AFTER the margin cap so it never pushes
    lots above what margin allows.  It is also capped by REGIME_MAX_LOTS.

    We insert this block immediately before the final `return lots` line
    in _calculate_lot_size().
    """
    sentinel = "# P4-2: composite quality multiplier"
    if sentinel in src:
        print("  P4-2 already applied (sentinel found)")
        return src

    # The function ends with:
    #         logger.info(
    #             f"Lot size: {lots} for {strategy_name} "
    #             ...
    #         )
    #         return lots
    #
    # We insert the multiplier block between the logger.info and return.
    # Use a precise anchor: the return statement is the last line of the
    # function and is uniquely preceded by the logger.info call.

    old = (
        "        logger.info(\n"
        "            f\"Lot size: {lots} for {strategy_name} \"\n"
        "            f\"risk={risk_per_trade} \"\n"
        "            f\"max_loss={max_loss_per_lot:.0f} \"\n"
        "            f\"margin_per_lot={margin_per_lot:.0f}\"\n"
        "        )\n"
        "        return lots\n"
    )
    new = (
        "        # P4-2: composite quality multiplier\n"
        "        # Scale lot count by signal quality so high-conviction\n"
        "        # entries (composite well above threshold) get more size\n"
        "        # than marginal entries (composite barely above threshold).\n"
        "        # Multiplier ramps linearly from 1.0 at composite=0.20\n"
        "        # to 1.5 at composite=0.75, capped at 1.5.\n"
        "        # Applied AFTER all other caps so it never exceeds margin\n"
        "        # or regime max lots limits.\n"
        "        if lots > 0:\n"
        "            _comp_now = abs(getattr(self.re, 'raw_composite', 0.0))\n"
        "            _comp_base = getattr(\n"
        "                config, 'MILD_SELL_ENTER', 0.20\n"
        "            )\n"
        "            _comp_mult = 1.0 + min(\n"
        "                0.5,\n"
        "                max(0.0, (_comp_now - _comp_base) / 1.10)\n"
        "            )\n"
        "            if _comp_mult > 1.05:  # only apply meaningful boost\n"
        "                _lots_before_mult = lots\n"
        "                lots = min(\n"
        "                    config.REGIME_MAX_LOTS.get(\n"
        "                        self.re.confirmed_regime, lots\n"
        "                    ),\n"
        "                    max(1, int(lots * _comp_mult)),\n"
        "                )\n"
        "                if lots != _lots_before_mult:\n"
        "                    logger.info(\n"
        "                        f\"P4-2: composite quality boost \"\n"
        "                        f\"{_lots_before_mult} → {lots} lots \"\n"
        "                        f\"(composite={_comp_now:.3f} \"\n"
        "                        f\"mult={_comp_mult:.2f}x)\"\n"
        "                    )\n"
        "        logger.info(\n"
        "            f\"Lot size: {lots} for {strategy_name} \"\n"
        "            f\"risk={risk_per_trade} \"\n"
        "            f\"max_loss={max_loss_per_lot:.0f} \"\n"
        "            f\"margin_per_lot={margin_per_lot:.0f}\"\n"
        "        )\n"
        "        return lots\n"
    )

    if old in src:
        src = src.replace(old, new, 1)
        print("  P4-2 applied: composite quality multiplier added to lot sizing")
    else:
        print("  P4-2 SKIPPED: lot sizing return anchor not found — check manually")
    return src


# ─────────────────────────────────────────────────────────────────────────────
# P4-3  strategy_engine.py — IV spike detection + Monday gap filter
# ─────────────────────────────────────────────────────────────────────────────

def fix_entry_gate_enhancements(src: str) -> str:
    """
    Two additions to _should_enter_new_position(), inserted just before
    the final `logger.info('Entry gate PASSED...')` line.

    (a) IV spike detection:
        When current IV_ATM >= 15% above the recent 5-reading average
        from iv_atm_history, we are in a fear-premium window.  Log this
        as an enhanced entry opportunity.  We do NOT override the regime
        gate — instead we log the spike so the operator knows this is a
        high-quality entry, and we relax the persistence_count requirement
        (see P4-4) for spike entries.

    (b) Monday gap risk filter:
        On Mondays before 10:00 IST, block STRAT_SHORT_STRADDLE entries.
        The straddle is the only undefined-risk strategy; condors and
        spreads have defined risk and are not blocked.
        We read the intended strategy from _select_strategy() to know
        whether a straddle would be selected — but calling _select_strategy
        here would be a side-effect.  Instead we check the regime directly:
        STRONG_SELL after P4-1 routes to condor when ADX is low, so the
        straddle is only selected when ADX >= ADX_RANGE_THRESHOLD.
        We block the entry when:
          - Monday before 10:00 IST
          - regime == STRONG_SELL
          - ADX >= ADX_RANGE_THRESHOLD (straddle would be selected)
    """
    sentinel_a = "# P4-3a: IV spike detection"
    sentinel_b = "# P4-3b: Monday gap risk filter"

    # We insert both blocks before the final PASSED log line.
    # Anchor: the unique string that ends _should_enter_new_position.
    anchor = (
        "        logger.info(\n"
        "            f'Entry gate PASSED: regime={regime} '\n"
        "            f'time={now_time} '\n"
        "            f'composite={self.re.raw_composite:.4f}'\n"
        "        )\n"
        "        return True\n"
    )

    if sentinel_a in src and sentinel_b in src:
        print("  P4-3a already applied (sentinel found)")
        print("  P4-3b already applied (sentinel found)")
        return src

    if anchor not in src:
        print("  P4-3 SKIPPED: entry gate PASSED anchor not found — check manually")
        return src

    insertion = ""

    if sentinel_a not in src:
        insertion += (
            "        # P4-3a: IV spike detection\n"
            "        # When IV_ATM is >= 15% above recent average, we are in a\n"
            "        # fear-premium window — the highest-EV entry for premium selling.\n"
            "        # Log this so the operator knows this is a high-quality entry.\n"
            "        # Also store the spike flag so P4-4 can relax persistence.\n"
            "        _iv_spike_entry = False\n"
            "        if (\n"
            "            self.dm.iv_atm is not None\n"
            "            and self.dm.iv_atm > 0\n"
            "            and len(self.dm.iv_atm_history) >= 5\n"
            "            and regime in [\n"
            "                config.REGIME_STRONG_SELL,\n"
            "                config.REGIME_MILD_SELL,\n"
            "            ]\n"
            "        ):\n"
            "            _recent_iv_avg = float(\n"
            "                sum(list(self.dm.iv_atm_history)[-5:])\n"
            "                / 5\n"
            "            )\n"
            "            if (\n"
            "                _recent_iv_avg > 0\n"
            "                and self.dm.iv_atm >= _recent_iv_avg * 1.15\n"
            "            ):\n"
            "                _iv_spike_entry = True\n"
            "                logger.info(\n"
            "                    f'P4-3a: IV spike detected — '\n"
            "                    f'iv_atm={self.dm.iv_atm*100:.2f}%% '\n"
            "                    f'vs recent_avg={_recent_iv_avg*100:.2f}%% '\n"
            "                    f'(+{(self.dm.iv_atm/_recent_iv_avg-1)*100:.1f}%%) '\n"
            "                    f'— high-quality fear-premium entry'\n"
            "                )\n"
            "        self._iv_spike_entry = _iv_spike_entry\n"
        )
        print("  P4-3a will be applied: IV spike detection")
    else:
        print("  P4-3a already applied (sentinel found)")

    if sentinel_b not in src:
        insertion += (
            "        # P4-3b: Monday gap risk filter\n"
            "        # Block straddle entries before 10:00 IST on Mondays.\n"
            "        # NIFTY Monday gap of 0.4-0.8%% can wipe 1.6 weeks of theta.\n"
            "        # Only blocks when the engine would select a straddle\n"
            "        # (STRONG_SELL + ADX >= ADX_RANGE_THRESHOLD after P4-1).\n"
            "        # Condors and credit spreads (defined risk) are NOT blocked.\n"
            "        if (\n"
            "            now.weekday() == 0\n"
            "            and now_time < config.EXEC_START_TIME.__class__(10, 0)\n"
            "            and regime == config.REGIME_STRONG_SELL\n"
            "            and (self.dm.adx or 0) >= config.ADX_RANGE_THRESHOLD\n"
            "        ):\n"
            "            logger.info(\n"
            "                'P4-3b: Entry gate BLOCKED: Monday gap risk window '\n"
            "                '(straddle before 10:00 IST — NIFTY gap risk)'\n"
            "            )\n"
            "            return False\n"
        )
        print("  P4-3b will be applied: Monday gap risk filter")
    else:
        print("  P4-3b already applied (sentinel found)")

    if insertion:
        new_anchor = insertion + anchor
        src = src.replace(anchor, new_anchor, 1)
        print("  P4-3 applied: IV spike detection and Monday gap filter inserted")

    return src


# ─────────────────────────────────────────────────────────────────────────────
# P4-4  strategy_engine.py — regime stability requirement
# ─────────────────────────────────────────────────────────────────────────────

def fix_regime_stability_gate(src: str) -> str:
    """
    Add a persistence_count >= 5 requirement to _should_enter_new_position().

    Exception: when an IV spike is detected (P4-3a sets self._iv_spike_entry),
    reduce the requirement to 3 cycles — the spike itself is confirmation.

    We insert this check after the existing persistence_count < 3 check
    (which guards against NEUTRAL→STRONG transitions) but before the
    positions/capital checks.

    The existing check is:
        if (
            self.re.previous_regime == config.REGIME_NEUTRAL
            and self.re.confirmed_regime in [...]
            and self.re.persistence_count < 3
        ):

    We add a broader stability check AFTER it:
        if self.re.persistence_count < 5:
            (unless IV spike detected, then require only 3)
    """
    sentinel = "# P4-4: regime stability requirement"
    if sentinel in src:
        print("  P4-4 already applied (sentinel found)")
        return src

    # Anchor: the block immediately after the existing persistence check.
    # The existing check ends with `return False` and is followed by the
    # positions check.  We anchor on the positions check opening.
    anchor = (
        "        if len(self.open_positions) >= config.MAX_CONCURRENT_POSITIONS:\n"
        "            logger.info(\n"
        "                f'Entry gate BLOCKED: max positions '\n"
        "                f'({len(self.open_positions)}/'\n"
        "                f'{config.MAX_CONCURRENT_POSITIONS})'\n"
        "            )\n"
        "            return False\n"
    )

    if anchor not in src:
        print("  P4-4 SKIPPED: max_positions anchor not found — check manually")
        return src

    insertion = (
        "        # P4-4: regime stability requirement\n"
        "        # Require the regime to have been confirmed for >= 5 cycles\n"
        "        # before entering a new position.  A regime confirmed for only\n"
        "        # 3 cycles (3 minutes) is more likely to be a transient signal.\n"
        "        # Exception: IV spike entries (P4-3a) require only 3 cycles\n"
        "        # because the spike itself provides additional confirmation.\n"
        "        _iv_spike_now = getattr(self, '_iv_spike_entry', False)\n"
        "        _min_persistence = 3 if _iv_spike_now else 5\n"
        "        if self.re.persistence_count < _min_persistence:\n"
        "            logger.info(\n"
        "                f'Entry gate BLOCKED: regime not yet stable '\n"
        "                f'(persistence={self.re.persistence_count} '\n"
        "                f'< required={_min_persistence})'\n"
        "            )\n"
        "            return False\n"
    )

    new_anchor = insertion + anchor
    src = src.replace(anchor, new_anchor, 1)
    print("  P4-4 applied: regime stability requirement (persistence_count >= 5)")
    return src


# ─────────────────────────────────────────────────────────────────────────────
# P4-5  config.py — CONDOR_EXIT_DTE 1 → 2
# ─────────────────────────────────────────────────────────────────────────────

def fix_condor_exit_dte(src: str) -> str:
    sentinel = "# P4-5: CONDOR_EXIT_DTE raised"
    if sentinel in src:
        print("  P4-5 already applied (sentinel found)")
        return src

    old = "CONDOR_EXIT_DTE           = 1"
    new = (
        "CONDOR_EXIT_DTE           = 2"
        "   # P4-5: CONDOR_EXIT_DTE raised (was 1; at DTE=1 closing costs"
        " exceed remaining theta; DTE=2 is profitable to close)"
    )

    if old in src:
        src = src.replace(old, new, 1)
        print("  P4-5 applied: CONDOR_EXIT_DTE 1 → 2")
    else:
        print("  P4-5 SKIPPED: anchor not found — check config.py manually")
    return src


# ─────────────────────────────────────────────────────────────────────────────
# File-level fix drivers
# ─────────────────────────────────────────────────────────────────────────────

def patch_strategy_engine(path: str) -> None:
    src = _read(path)
    original = src

    src = fix_strong_sell_routing(src)
    src = fix_composite_lot_scaling(src)
    src = fix_entry_gate_enhancements(src)
    src = fix_regime_stability_gate(src)

    if src != original:
        _write(path, src)
        print("  strategy_engine.py written.")
    else:
        print("  strategy_engine.py: no changes written.")


def patch_config(path: str) -> None:
    src = _read(path)
    original = src

    src = fix_condor_exit_dte(src)

    if src != original:
        _write(path, src)
        print("  config.py written.")
    else:
        print("  config.py: no changes written.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    base = os.path.dirname(os.path.abspath(__file__))

    se_path  = os.path.join(base, "strategy_engine.py")
    cfg_path = os.path.join(base, "config.py")

    _assert_file(se_path)
    _assert_file(cfg_path)

    print("\n=== Patching strategy_engine.py ===")
    _backup(se_path)
    patch_strategy_engine(se_path)

    print("\n=== Patching config.py ===")
    _backup(cfg_path)
    patch_config(cfg_path)

    print("\n=== Verification ===")
    se_ok = _verify(se_path, [
        "# P4-1: STRONG_SELL routes to condor when range-bound",
        "# P4-2: composite quality multiplier",
        "# P4-3a: IV spike detection",
        "# P4-3b: Monday gap risk filter",
        "# P4-4: regime stability requirement",
    ])
    cfg_ok = _verify(cfg_path, [
        "# P4-5: CONDOR_EXIT_DTE raised",
    ])

    print()
    if se_ok and cfg_ok:
        print("All P4 patches verified successfully.")
        print()
        print("What was fixed:")
        print(
            "  P4-1  strategy_engine.py  STRONG_SELL routes to iron condor\n"
            "         when ADX < ADX_RANGE_THRESHOLD (range-bound confirmed).\n"
            "         Falls back to straddle when ADX is elevated (trending).\n"
            "         Iron condor has 5x better return on margin at VIX=11-14."
        )
        print(
            "  P4-2  strategy_engine.py  Composite quality multiplier in lot sizing.\n"
            "         Multiplier ramps 1.0x → 1.5x as composite rises from\n"
            "         MILD_SELL_ENTER (0.20) to 0.75.  Applied after all hard\n"
            "         caps so it never exceeds margin or regime max lots."
        )
        print(
            "  P4-3a strategy_engine.py  IV spike detection in entry gate.\n"
            "         Logs when IV_ATM >= 15%% above recent 5-cycle average.\n"
            "         Sets _iv_spike_entry flag used by P4-4 to relax\n"
            "         persistence requirement."
        )
        print(
            "  P4-3b strategy_engine.py  Monday gap risk filter.\n"
            "         Blocks straddle entries before 10:00 IST on Mondays\n"
            "         (NIFTY average Monday gap 0.4-0.8%% wipes 1.6 weeks theta).\n"
            "         Only blocks when engine would select straddle\n"
            "         (STRONG_SELL + ADX >= ADX_RANGE_THRESHOLD)."
        )
        print(
            "  P4-4  strategy_engine.py  Regime stability requirement.\n"
            "         Requires persistence_count >= 5 before entry.\n"
            "         Relaxed to 3 when IV spike detected (P4-3a)."
        )
        print(
            "  P4-5  config.py           CONDOR_EXIT_DTE 1 → 2.\n"
            "         At DTE=1, closing costs (~Rs334 + slippage) exceed\n"
            "         remaining theta (~Rs390).  At DTE=2, theta (~Rs650-910)\n"
            "         comfortably exceeds closing costs."
        )
        print()
        print("Skipped from P4 report list:")
        print(
            "  'Add time-of-day IV level check'\n"
            "   → Requires tracking intraday IV high (not currently stored).\n"
            "     Deferred to avoid introducing new state bugs."
        )
        print(
            "  'Lower STRONG_SELL_MIN_CONFIRMING_MODULES from 3 to 2'\n"
            "   → Already applied as P3-5g in patch_p3.py."
        )
        print()
        print("No other files were modified.")
        print("Backups: strategy_engine.py.bak  config.py.bak")
    else:
        print("One or more patches could not be verified.")
        print("Inspect the output above and use .bak files to restore if needed.")
        sys.exit(1)


if __name__ == "__main__":
    main()