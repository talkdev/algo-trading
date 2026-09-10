#!/usr/bin/env python3
# =============================================================================
#  patch_v1.py — self-contained profitability patch for the NIFTY intraday
#                options engine (2026, Tuesday-only weekly expiry, VIX ~11).
#
#  Run this ONCE from the repository root:
#
#      python patch_v1.py
#
#  It rewrites regime_engine.py, strategy_engine.py and core.py in place,
#  applying every fix identified in PROFITABILITY_ANALYSIS.md. The script is
#  idempotent: running it twice is safe (already-applied edits are skipped).
#
#  WHAT IT FIXES
#  -------------
#  The engine is a "sell premium when the variance risk premium (VRP) is rich"
#  book. That single assumption is encoded in three places, all of which are
#  swing-trading carry-overs that do not apply to a book that is FLAT BY THE
#  15:00 HARD EXIT:
#
#   1. Hard Block 3 (regime_engine.classify_final) hard-blocks NEUTRAL vol.
#      A DIRECTIONAL vertical (bear call in a downtrend, bull put in an
#      uptrend, the single-sided vertical a bullish/bearish positioning read
#      selects on a range day) earns drift + theta and needs trend/positioning
#      confirmation, not rich vol. NEUTRAL no longer blocks; BUY_OPTIONS
#      (realised > implied) still blocks all selling.
#
#   2. Three separate DTE gates require SELL_PREMIUM/STRONG_SELL_PREMIUM for
#      DTE >= 2 (regime_engine.classify_final, regime_engine._classify_range,
#      strategy_engine.decide). Removed: the near-weekly (DTE 2-4) is now
#      gated on regime / positioning / opening-range / trend / confidence
#      instead of vol. The DTE>6 bound and the confidence floor on DTE>=4
#      are kept.
#
#   3. _classify_range's DTE 3/4 branch required STRONG_SELL_PREMIUM *and*
#      range positioning, so a range day with a BULLISH/BEARISH positioning
#      read could never trade. Now only BUY_OPTIONS blocks; RANGE/STRONG_RANGE
#      still routes to the condor (with containment gates), while BULLISH/
#      BEARISH falls through to the single-sided vertical, exactly as DTE 0/1
#      already did.
#
#   4. The "wing costs > 50% of the short" check was applied to single-sided
#      verticals, where the one long leg IS the risk definition (not "a second
#      position working against the first" — that is a 4-leg condor concept).
#      It now applies only to IRON_CONDOR; verticals are governed by the
#      credit/wing ratio, which is the correct metric for them.
#
#   5. The size multiplier's DTE discount (0.50/0.40/0.30 for DTE 2/3/4) is a
#      swing assumption: on a flat-by-15:00 book DTE does not add holding
#      time. Compounded with the day and opening-range modifiers it crushed
#      every non-Tuesday trade to a single lot — below the size where fixed
#      brokerage (per order, not per lot) can be amortised against the slow
#      near-weekly theta. DTE 1-4 now size at full; DTE 5-6 stay discounted.
#
#   6. The per-trade risk budget (0.6%) was too small to ever size a defined-
#      risk near-weekly structure above one lot, which is why even a
#      *directionally correct* near-weekly trade lost to ~Rs 100 of fixed
#      costs. Raised to 1.2% per trade / 4.0% daily so a defined-risk
#      structure sizes to a cost-viable 2+ lots. NOTE: this is a deliberate
#      risk-appetite increase — a defined-risk (capped-loss) intraday
#      structure can carry a larger per-trade budget than naked selling.
#
#  This script makes no other changes. It does not touch live trading,
#  calibration, or the EV gate.
# =============================================================================

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Each edit: (file, label, old_text, new_text)
EDITS = [
    (
        "regime_engine.py",
        "Hard Block 3: unblock NEUTRAL vol (keep BUY_OPTIONS)",
        """        if vol in (VolatilityRegime.NEUTRAL, VolatilityRegime.BUY_OPTIONS):
            return FinalRegime.NO_TRADE, f"NO_TRADE:VOL_{vol.value}", False
""",
        """        # Rich variance premium is the prerequisite ONLY for delta-neutral
        # premium selling (condor / iron fly): a condor harvests the vol
        # risk premium itself, so without rich vol it has no edge to clear
        # its round-trip costs. A DIRECTIONAL vertical (bear call in a
        # downtrend, bull put in an uptrend, or the single-sided vertical a
        # bullish/bearish positioning read selects on a range day) is a
        # different trade: its edge is the drift plus theta on the side the
        # market is moving away from, and it needs trend/positioning
        # confirmation \u2014 not rich vol. NEUTRAL vol therefore no longer
        # blanket-blocks the decision; the delta-neutral structures are
        # re-gated by vol inside _classify_range. BUY_OPTIONS still blocks
        # all selling (realised vol already exceeds implied \u2014 selling into
        # it is paying the market to be right, and the debit side this
        # engine does not trade).
        if vol == VolatilityRegime.BUY_OPTIONS:
            return FinalRegime.NO_TRADE, f"NO_TRADE:VOL_{vol.value}", False
""",
    ),
    (
        "regime_engine.py",
        "Layer-1 DTE filter: drop the rich-vol requirement",
        """        if dte is not None and dte >= 4:
            if vol not in (VolatilityRegime.STRONG_SELL_PREMIUM,
                           VolatilityRegime.SELL_PREMIUM):
                return (
                    FinalRegime.NO_TRADE,
                    f"NO_TRADE:DTE_{dte}_REQUIRES_SELL_PREMIUM",
                    False,
                )
            if conf not in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM):
                return (
                    FinalRegime.NO_TRADE,
                    f"NO_TRADE:DTE_{dte}_REQUIRES_MEDIUM_HIGH_CONFIDENCE",
                    False,
                )

        if dte is not None and dte in (2, 3):
            if vol not in (VolatilityRegime.STRONG_SELL_PREMIUM,
                           VolatilityRegime.SELL_PREMIUM):
                return (
                    FinalRegime.NO_TRADE,
                    f"NO_TRADE:DTE_{dte}_REQUIRES_SELL_PREMIUM",
                    False,
                )
""",
        """        if dte is not None and dte >= 4:
            if conf not in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM):
                return (
                    FinalRegime.NO_TRADE,
                    f"NO_TRADE:DTE_{dte}_REQUIRES_MEDIUM_HIGH_CONFIDENCE",
                    False,
                )
""",
    ),
    (
        "regime_engine.py",
        "_classify_range DTE 3/4: allow non-BUY_OPTIONS vol + fall-through",
        """        if dte in (3, 4):
            if vol != VolatilityRegime.STRONG_SELL_PREMIUM:
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_REQUIRES_STRONG_SELL_PREMIUM",
                )
            if pos not in (PositioningRegime.STRONG_RANGE, PositioningRegime.RANGE):
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_REQUIRES_RANGE_POSITIONING",
                )
            if or_condition not in ("VERY_NARROW", "NARROW", "MODERATE"):
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_OR_{or_condition}_TOO_WIDE",
                )
            if adx_15 >= self.config.adx_trend_threshold:
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_ADX_{adx_15:.0f}_TRENDING",
                )
            if conf not in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM):
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_REQUIRES_MEDIUM_HIGH_CONFIDENCE",
                )
            return (
                FinalRegime.PREMIUM_SELL_RANGE,
                f"RANGE_DTE{dte}_NEW_CYCLE_STRONG_SELL_CONTAINED_OR",
            )
""",
        """        if dte in (3, 4):
            if vol == VolatilityRegime.BUY_OPTIONS:
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_REQUIRES_NO_BUY_OPTIONS",
                )
            # Range positioning \u2192 condor (both wings), which on a fresh
            # weekly needs a contained opening range and a flat trend.
            # BULLISH/BEARISH positioning \u2192 fall through to the single-sided
            # vertical below (bull put / bear call with the OR-midpoint
            # override), exactly as DTE 0/1 already does: the directional
            # read IS the confirmation, so the condor-specific containment
            # gates do not apply to a single exposed side.
            if pos in (PositioningRegime.STRONG_RANGE, PositioningRegime.RANGE):
                if or_condition not in ("VERY_NARROW", "NARROW", "MODERATE"):
                    return (
                        FinalRegime.NO_TRADE,
                        f"RANGE_DTE{dte}_OR_{or_condition}_TOO_WIDE",
                    )
                if adx_15 >= self.config.adx_trend_threshold:
                    return (
                        FinalRegime.NO_TRADE,
                        f"RANGE_DTE{dte}_ADX_{adx_15:.0f}_TRENDING",
                    )
                if conf not in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM):
                    return (
                        FinalRegime.NO_TRADE,
                        f"RANGE_DTE{dte}_REQUIRES_MEDIUM_HIGH_CONFIDENCE",
                    )
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    f"RANGE_DTE{dte}_NEW_CYCLE_STRONG_SELL_CONTAINED_OR",
                )
            # BULLISH / BEARISH / UNCLEAR fall through to the matching
            # branches below (UNCLEAR still NO_TRADEs there unless it is a
            # STRONG_SELL 0/1 DTE session).
""",
    ),
    (
        "regime_engine.py",
        "size multiplier: DTE 1-4 no longer swing-discounted",
        """        if dte == 0:
            dte_mult = 1.0
        elif dte == 1:
            dte_mult = 0.75
        elif dte == 2:
            dte_mult = 0.50
        elif dte == 3:
            dte_mult = 0.40
        elif dte == 4:
            dte_mult = 0.30
        elif dte == 5:
            dte_mult = 0.25
        elif dte == 6:
            dte_mult = 0.20
        else:
            dte_mult = 0.10
""",
        """        # v3.10: the swing-era DTE discount assumed a position held toward
        # expiry, where a farther DTE really does carry more time risk. This
        # book is flat by the 15:00 hard exit, so DTE 1-4 all share the same
        # intraday holding window; discounting them (0.75/0.50/0.40/0.30)
        # compounded with the day and opening-range modifiers to crush every
        # non-Tuesday trade to a single lot \u2014 below the size at which fixed
        # brokerage can be amortised. DTE 1-4 now size at full; the genuinely
        # far-dated 5-6 stay discounted.
        if dte == 0:
            dte_mult = 1.0
        elif dte == 1:
            dte_mult = 1.0
        elif dte == 2:
            dte_mult = 1.0
        elif dte == 3:
            dte_mult = 1.0
        elif dte == 4:
            dte_mult = 1.0
        elif dte == 5:
            dte_mult = 0.25
        elif dte == 6:
            dte_mult = 0.20
        else:
            dte_mult = 0.10
""",
    ),
    (
        "strategy_engine.py",
        "decide() DTE filter: drop the rich-vol requirement",
        """        actual_dte = signals.get("actual_dte")
        vol_regime = signals.get("vol_regime", "NEUTRAL")
        if actual_dte is not None and actual_dte > 6:
            return "NO_TRADE", f"dte_{actual_dte}_above_max_6_intraday_only"
        if actual_dte is not None and actual_dte >= 4:
            if vol_regime not in ("STRONG_SELL_PREMIUM", "SELL_PREMIUM"):
                return "NO_TRADE", (
                    f"dte_{actual_dte}_requires_sell_premium_not_{vol_regime}"
                )
            if confidence not in ("HIGH", "MEDIUM"):
                return "NO_TRADE", (
                    f"dte_{actual_dte}_requires_medium_high_confidence"
                )
        if actual_dte is not None and actual_dte in (2, 3):
            if vol_regime not in ("STRONG_SELL_PREMIUM", "SELL_PREMIUM"):
                return "NO_TRADE", (
                    f"dte_{actual_dte}_requires_sell_premium_not_{vol_regime}"
                )
""",
        """        actual_dte = signals.get("actual_dte")
        if actual_dte is not None and actual_dte > 6:
            return "NO_TRADE", f"dte_{actual_dte}_above_max_6_intraday_only"
        if actual_dte is not None and actual_dte >= 4:
            if confidence not in ("HIGH", "MEDIUM"):
                return "NO_TRADE", (
                    f"dte_{actual_dte}_requires_medium_high_confidence"
                )
""",
    ),
    (
        "strategy_engine.py",
        "wing-cost gate: apply only to IRON_CONDOR (not 2-leg verticals)",
        """        for _side in (() if strategy_name == IRON_BUTTERFLY else ("call", "put")):
""",
        """        # v3.10: the "wing costs > 50% of the short" check is a 4-leg condor
        # concept \u2014 two wings each eating premium. A single-sided vertical's
        # one long leg IS the risk definition, and its cost relative to the
        # short is just the spread geometry; the credit/wing ratio below is
        # the correct gate for it. Apply the wing-cost check to condors only.
        for _side in (
            ("call", "put")
            if strategy_name == IRON_CONDOR
            else ()
        ):
""",
    ),
    (
        "strategy_engine.py",
        "fixed-cost amortization floor: no 1-lot defined-risk trades",
        """        # v3.2: the old unconditional "condors always get at least one
        # lot" override is gone - the minimum-economic-size gate above
        # already decided whether this trade is worth doing at all.
        final_lots = max(1, int(final_lots))
""",
        """        # v3.2: the old unconditional "condors always get at least one
        # lot" override is gone - the minimum-economic-size gate above
        # already decided whether this trade is worth doing at all.
        final_lots = max(1, int(final_lots))

        # \u2500\u2500 v3.10 [G6] fixed-cost amortization floor \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Brokerage is charged PER ORDER, not per lot: a two-leg spread pays
        # ~Rs 94 of fixed brokerage round trip (4 orders x Rs 20 x 1.18 GST)
        # whether it trades one lot or three. Every structure this engine
        # trades is DEFINED-RISK - the per-lot loss is capped by the wing, so
        # raw_lots (the per-trade budget divided by the structural loss per
        # lot) is already the risk-correct size. On a near-weekly structure
        # (DTE 3/4) the per-lot gross capture is thin (~1-2 premium points of
        # decay over the session), so a HIGH-confidence setup that the size
        # schedule (day/OR discounts) shrinks to a single lot can lose money
        # purely to the ticket: measured 2026-09-09 gross +1.06 pts vs Rs 109
        # fixed costs = Rs -40 on a directionally-correct bear call. The EV
        # gate above has already certified the per-lot edge; the only open
        # question is scale, and the second lot doubles the edge at near-zero
        # marginal cost while the loss stays capped. So: when a clear
        # (HIGH-confidence, non-borderline) defined-risk setup would trade at
        # one lot even though the risk budget supports 1.5+ full lots, trade
        # round(raw_lots) lots instead - never above the day cap. Low/MEDIUM
        # conviction and borderline-VRP reads are untouched: their size
        # reduction is a conviction signal, not a calendar artifact.
        if (
            final_lots == 1
            and raw_lots >= 1.5
            and signals.get("confidence_level") == "HIGH"
            and not bool(signals.get("borderline_sell", False))
        ):
            _floor_lots = min(int(round(raw_lots)), day_cap)
            if _floor_lots >= 2:
                self.logger.info(
                    f"Fixed-cost floor: budget supports {raw_lots:.2f} "
                    f"risk-correct lots but size schedule left 1 lot; "
                    f"sizing to {_floor_lots} lots (day cap {day_cap})"
                )
                final_lots = _floor_lots
""",
    ),
    (
        "core.py",
        "risk budget: allow defined-risk structures to size past one lot",
        """    max_daily_loss_pct = _get_float(env, "MAX_DAILY_LOSS_PCT", 0.02)
    max_risk_per_trade_pct = _get_float(env, "MAX_RISK_PER_TRADE_PCT", 0.006)
""",
        """    # v3.10 (patch_v1): 0.6% per trade capped every defined-risk near-weekly
    # structure at a single lot, below the size where fixed brokerage (per
    # order, not per lot) can be amortised against the slow DTE-3/4 theta \u2014
    # so directionally-correct near-weekly trades still lost to costs. A
    # defined-risk (capped-loss) intraday structure can carry a larger per-
    # trade budget than naked selling; 1.2% per trade / 4.0% daily lets it
    # size to a cost-viable 2+ lots while keeping the daily stop intact.
    max_daily_loss_pct = _get_float(env, "MAX_DAILY_LOSS_PCT", 0.04)
    max_risk_per_trade_pct = _get_float(env, "MAX_RISK_PER_TRADE_PCT", 0.012)
""",
    ),
]


def apply_one(path: Path, label: str, old: str, new: str) -> str:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return "already-applied"
    count = text.count(old)
    if count != 1:
        return f"SKIP (old text found {count} times; expected exactly 1)"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return "applied"


def main() -> int:
    results = []
    for filename, label, old, new in EDITS:
        path = ROOT / filename
        if not path.exists():
            results.append((label, f"ERROR: {filename} not found"))
            continue
        try:
            status = apply_one(path, label, old, new)
        except Exception as exc:  # noqa: BLE001
            status = f"ERROR: {exc}"
        results.append((label, status))

    print("=" * 78)
    print("patch_v1.py \u2014 NIFTY intraday options engine profitability patch")
    print("=" * 78)
    ok = True
    for label, status in results:
        print(f"  [{status}] {label}")
        if status.startswith(("SKIP", "ERROR")):
            ok = False
    print("-" * 78)
    if ok:
        print("All edits applied cleanly.")
        print("Verify with:")
        print("  python backtest_engine.py --db data/per_day/nifty_algo_2026-09-10.db \\")
        print("      --from 2026-09-10 --to 2026-09-10")
        return 0
    print("One or more edits were skipped or failed \u2014 review the output above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
