#!/usr/bin/env python3
# patch_v12.py
# NIFTY Intraday Options Engine — v12
# THE DIRECTIONAL TREND ROUTE: make the engine able to express a confirmed
# intraday trend with long premium, on every DTE, in the exact tape where the
# sell side is refused for a sell-side reason.
#
# ─────────────────────────────────────────────────────────────────────────────
# THE INCIDENT THIS PATCH ANSWERS
# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-15 was a Tuesday — the weekly expiry, DTE 0. The engine took ZERO
# trades. 484 cycles, 484 rejections, and the tape was the single most
# tradeable session of the sample:
#
#   09:15  prev close 23343.7, gap UP +178 pts, first minute prints
#          23576.15 / 23592.85 / 23487.30 / 23492.10 — a 105-point opening
#          drive straight back through the gap. Failed gap-up.
#   09:45  opening range H 23592.85 L 23457.05 W 135.8 (VERY_WIDE).
#   09:49  price structure = DOWNTREND, spot 23426 vs OR low 23457 and a
#          VWAP of 23466 — a decisive, confirmed opening-range breakdown.
#   11:10  spot 23327.6 — 265 points below the opening high, ADX(5m) ~90.
#   IV     23.3% -> 28.1% (EXPANDING), India VIX 12.28 -> ~12.9 (+5%).
#
# The engine watched all of it. Every refusal was, in order:
#
#   09:45-09:48  RANGE_WIDE_OR_VERY_WIDE_NO_TRADE      (regime layer)
#   09:49-09:52  before_entry_window_10:30:00          (Tuesday 0DTE window)
#   09:53-10:45  iv_expanding_never_sell_into_rising_iv  188 cycles
#   10:46-11:16  day_move_used_NNNpct_of_opening_straddle_no_edge  110 cycles
#   (plus NO_TRADE:STRADDLE_EXPLOSION, 37 cycles, 09:55-10:04)
#
# Four separate gates, one shared defect: EVERY ONE OF THEM IS A STATEMENT
# ABOUT SELLING PREMIUM.
#
#   * "never sell into rising IV" — true, and irrelevant to a buyer. On this
#     tape the ATM 23450 put went 73.70 (09:50) -> 128.90 (11:00). The vega
#     loss on a 0.59-delta put was a rounding error against 90 points of
#     delta.
#   * "the day has spent 225% of its opening straddle, no edge left" — true
#     for a variance seller (there is no unpriced range left to sell) and
#     backwards for a momentum buyer (the day is running 2.2x its priced
#     move, which is exactly what a trend ticket is paid on).
#   * "the straddle exploded, stand aside" — the straddle exploded because
#     the market repriced a 265-point trend. Standing aside is the correct
#     response to owning that risk and the wrong response to being long it.
#   * "RANGE_WIDE_OR_VERY_WIDE_NO_TRADE" — a condor veto. A wide OR is a
#     condor's problem and a breakout's prerequisite.
#
# and the one route the engine has that COULD buy the tape refused it four
# times over, independently, none of which is visible anywhere except by
# reading the code:
#
#   1. Config.momentum_block_markers = ("dte2", "no_exception", "immature",
#      "buy_options", "wide_or", "dangerous_to_sell") does not contain the
#      iv-expansion family, the day-move family, the straddle family or the
#      Tuesday entry-window family — so for the *entire* session the v5 route
#      reported "momentum_sell_side_open(...)" and never reached its own gates.
#   2. Config.momentum_min_dte = 1. The session was DTE 0.
#   3. _momentum_gate() re-blocks EXPANDING IV ("momentum_iv_expanding_no_chase").
#   4. Config.momentum_day_move_max_pct = 90 — STRICTER than the sell-side
#      gate it exists to answer (125), so on this tape the substitute was
#      refused by the number that caused the original refusal.
#
# ─────────────────────────────────────────────────────────────────────────────
# WHAT THIS PATCH DOES — AND WHAT IT DELIBERATELY DOES NOT DO
# ─────────────────────────────────────────────────────────────────────────────
# It ADDS a route. It does not weaken one gate that is already profitable.
# The sell-side pipeline (compute_params, every credit structure, every
# existing entry rule, both entry windows, the regime engine's verdict tree,
# the exit ladder for sold structures) is byte-for-byte untouched, so days
# that already trade — 2026-09-08, 09, 10, 11 — keep the tickets they had.
#
#   1. strategy_engine.StrategyEngine._debit_substitute()  (new)
#      One substitution entry point for the three NO_TRADE exits in decide().
#      It tries the v5 momentum route first, UNCHANGED, so existing behaviour
#      is preserved exactly; only if that refuses does the new route run.
#
#   2. StrategyEngine._trend_route_gate() / _trend_route_decision() /
#      compute_trend_params() / trend_route_size_plan()  (new)
#      A long-premium trend expression that may answer ONLY the refusal
#      families that assert something about the SELL side (an explicit,
#      auditable, config-driven whitelist), and that then validates the tape
#      from scratch:
#        * a price regime that is genuinely directional (DOWNTREND /
#          STRONG_DOWNTREND / UPTREND / STRONG_UPTREND) — the trend label the
#          regime layer already computes, which on 2026-09-15 was correct
#          from 09:48:50 onward;
#        * a confirmed OPENING-RANGE BREAK in that direction;
#        * VWAP on the trend side;
#        * the trend must be YOUNG, measured in opening ranges travelled from
#          the level (a scale-free, level-free extension band). This is the
#          one rule that stops the route buying the top of a spent move — and
#          the reason the route would have been OUT at 10:30 even had the
#          entry window been open, when the same put was worth 113.55 and
#          had 10 points left in it;
#        * its own time window (independent of the sell-side one, so the
#          Tuesday 0DTE 10:30 sell-side start is preserved rather than
#          re-tuned);
#        * refusal of every account-safety and market-data gate;
#        * a sizing policy that floors the regime's (naked-gamma) schedule,
#          then steps it DOWN with the extension, on 0DTE, and on an
#          expanding straddle.
#
#   3. execution_engine._monitor_debit_position()
#      A DTE-0 unconditional time-flat (D3b) for trend-route tickets. The
#      existing ladder only flattens a long inside the final window IF it is
#      in profit; a 0DTE long that is not is carried into the 15:00 bell and
#      to zero. Trend tickets are flat by 14:30 on expiry day.
#
#   4. Config: a trend_route_* block with env overrides (core.py).
#
#   5. backtest_engine.STAGE_ORDER / Results._bucket: diagnostics only. The
#      harness itself reported "120 rejection(s) from gates this tool cannot
#      place in the chain" on 2026-09-15 — day_move (110) and
#      RANGE_WIDE_OR (10) — which is why the funnel could not show this
#      incident. Those gates are now classifiable, and
#      strategy_rules_failed: reasons group instead of producing one bucket
#      per strike. No simulated P&L is touched by this part.
#
#   6. forensic trail: the route's verdict for every cycle it evaluates is
#      written into strategy_decisions.signals_json as "trend_route_verdict",
#      so "why did the substitute not fire" is a query, not an archaeology
#      exercise.
#
# ─────────────────────────────────────────────────────────────────────────────
# EVIDENCE — WHAT WAS MEASURED, AND HOW TO REPRODUCE IT
# ─────────────────────────────────────────────────────────────────────────────
# Reproduce with the venv this repo needs (`pip install requests pandas numpy`;
# the system interpreter is PEP-668 managed):
#
#   python patch_v12.py                     # apply + verify + 09-15 self-check
#   python backtest_engine.py --db data/per_day/nifty_algo_<date>.db \
#          --from <date> --to <date> --trade-report off --csv <out>.csv
#
# THE FOUR PROFITABLE SESSIONS ARE UNTOUCHED. Trade-for-trade identical
# blotters before and after the patch, and identical with the route ON:
#
#   2026-09-08  BEAR_CALL_SPREAD  12:03:30 -> 13:45:33   6L   +Rs 2,544.06
#   2026-09-09  IRON_CONDOR       09:45:31 -> 15:20:11   3L   +Rs 1,121.06
#   2026-09-10  BEAR_CALL_SPREAD  09:54:53 -> 15:00:56   5L   +Rs 3,048.53
#   2026-09-11  IRON_CONDOR       13:26:35 -> 14:03:38   5L   -Rs 5,820.80
#   4-day total before = after = +Rs 892.85
#
# and that is not luck, it is the structure of the change. On those four days
# the route was evaluated 2,416 times (its refusal is logged, and persisted in
# strategy_decisions.signals_json) and substituted zero times:
#
#   09-08  621 evals   all "sell_side_not_refused" - past_14:30, past entry
#                      window, dte_5_above_max, RANGE_UNCLEAR, CHOPPY
#   09-09  164 evals   all "sell_side_not_refused"
#   09-10  279 evals   51 reached the trend test: needs_trend_got_RANGE
#   09-11  1352 evals  311 reached the trend test: needs_trend_got_RANGE
#
# The reason is not a tuned threshold. The four profitable sessions are RANGE
# days and their money is made selling premium inside a range; the route needs
# a directional price regime AND a refused sell side at the SAME instant, and
# on a range day those two never coincide. Re-run with deliberately
# promiscuous settings (route starts 09:45, or_break_frac 0.02, no ADX floor,
# no VWAP buffer, extension cap 3x OR, IV cap 120%, day-move cap 2000%,
# 3 tickets/day, size floor 1.0) and the route STILL takes zero trades on all
# four days, with every blotter byte-identical.
#
# And it cannot silently eat the sell side's lunch: it takes at most one
# ticket a day, it never opens while any position is open, it stops for the
# day after two consecutive stops, and the premium at risk on one ticket
# (max_loss_per_lot ~Rs 2,068, three lots here ~Rs 6,200 on Rs 1,000,000) is
# an order of magnitude below the 8% daily halt, so a failed trend ticket
# cannot trip the halt that the sell side's own trade depends on.
#
# THE INCIDENT SESSION IS FIXED:
#
#   2026-09-15  LONG_PUT          09:50:05 -> 10:13:08   3L   +Rs 3,056.11
#
# 09:50:05 is the FIRST cycle at which the route's window is open, i.e. it is
# not a lucky timestamp - the route enters as soon as its confirmation exists.
# Entry 23450 PE at 73.65 with spot 23416.7; the tape ran to a mid of 107.73
# by 09:58:20 and the existing ratchet took it out at 89.75. It is the first
# and only ticket of that session, and it is on the correct side of the move
# the engine spent two hours watching.
#
# ROBUSTNESS. 27 single-knob perturbations of the new route, all on 09-15:
#   * route disabled (control) ............ 0 trades, exactly the old system
#   * or_break_frac 0.05 / 0.10 / 0.15 .... identical trade, identical entry
#   * iv cap 20 / 30 / 50 ................. identical
#   * vwap buffer 0 / 8 / 25 .............. identical
#   * adx floor 0 / 20 / 30 ............... identical
#   * lock trigger 0.15 / 0.25 ............ identical
#   * extension cap 0.35 / 0.45 / 0.60 / 0.90 / 1.50 ... same trade; only the
#     position size moves, and monotonically (2 / 3 / 3 / 4 / 4 lots)
#   * size floor 0.50 / 0.70 / 1.00 ....... monotonically 2 / 3 / 5 lots
#   * start 10:30 or 11:00 ................ 0 trades (it refuses the late
#     chase - the put had ~10 points left by then) and a loss-nothing default
#   * start 09:45 ......................... enters at 09:47:49 instead
# Only two knobs move the result materially, and both are interpretable
# rather than fitted: how much confirmation is demanded before the break is
# treated as real (09:45 vs 09:50) and how many tickets the day may take.
# Both are env-overridable (TREND_ROUTE_START, TREND_ROUTE_MAX_TRADES_PER_DAY)
# and both defaults are the conservative choice.
#
# KNOWN LIMITATION, STATED PLAINLY: there are five sessions of data in this
# repository and 09-15 is the only one that is both directional and partial.
# This is an in-sample change on those five sessions and there is no
# walk-forward set to validate against. The design deliberately leans on
# ratios of the opening range rather than point counts, on the regime layer's
# own regime labels rather than new thresholds, and on one ticket a day, so
# that it degrades to "no trade" rather than to "wrong trade" on a tape whose
# regime it has never seen.
#
# USAGE
#   python patch_v12.py            # apply, then verify
#   python patch_v12.py --check    # verify only (no writes)
#   python patch_v12.py --dry-run  # show what would change
#   python patch_v12.py --force    # apply even if the baseline md5 differs
#
# The patch is IDEMPOTENT: a second run reports the edits as already applied
# and writes nothing.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import hashlib
from datetime import date, time as dtime
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
PYTHON = sys.executable

PATCH_TAG = "v12"

# md5 of the post-v11 files this patch was authored against. Informational:
# a mismatch is reported, and only blocks the run with --strict-baseline.
BASELINE_MD5 = {
    "core.py":             "696900b544b4ffdf9b90a13d7a40911c",
    "strategy_engine.py":  "0d1e6a929d9a8e9510495d4e6616add4",
    "execution_engine.py": "e9bb4a84d677b59af792746a63632642",
    "backtest_engine.py":  "9d37be5733c29b66d5c3054a20407da4",
}

MARK = "# ── v12 (patch_v12)"
# One string that exists in a file ONLY once this patch has been applied.
# Used to tell "already patched" apart from "somebody else edited this".
MARKERS = {
    "core.py":             "trend_route_enabled",
    "strategy_engine.py":  "_debit_substitute",
    "execution_engine.py": "trend_route_dte0_time_flat",
    "backtest_engine.py":  "strategy_rules_failed",
}


# ═════════════════════════════════════════════════════════════════════════════
# EDIT 1 — core.py: Config, the trend_route_* block
# ═════════════════════════════════════════════════════════════════════════════
CORE_CONFIG_ANCHOR = '''    momentum_block_markers:        tuple = (
        "dte2", "no_exception", "immature", "buy_options",
        "wide_or", "dangerous_to_sell",
    )
'''

CORE_CONFIG_NEW = CORE_CONFIG_ANCHOR + '''
    # ── v12 (patch_v12): the directional trend route ────────────────────
    # v5 taught the engine to buy a confirmed trend. It then fenced that
    # route out of the tape it was written for: it answers only six refusal
    # markers, so it never sees an IV-expansion or day-move refusal (the two
    # that account for 298 of 484 cycles on 2026-09-15), it cannot trade DTE
    # 0 (momentum_min_dte = 1 — expiry day IS the trend day on the Tuesday
    # weekly), it re-blocks EXPANDING IV in its own gate, and its
    # day_move cap (90) is stricter than the sell-side gate it exists to
    # answer (125), so it is refused by the very number that refused the
    # sell side. The result was a two-hour, 265-point opening-range
    # breakdown traded by nobody.
    #
    # The route below is the buy-side answer to a SELL-SIDE refusal, and
    # nothing else. It is a different trade from the sell side, not a
    # looser version of it: the risk is the premium paid (capped), the
    # exposure is delta not vega, the stop is on the option's own value and
    # the ticket is flattened before the bell on expiry day.
    trend_route_enabled:                     bool  = True
    # Its own entry window. Deliberately NOT tied to entry_start: the
    # Tuesday 0DTE sell-side window (10:30) is calibrated for naked gamma
    # and is preserved untouched. An opening-range breakout is a 09:45-11:00
    # event by construction — the OR is not complete until 09:45 and the
    # level is usually spent within the hour — so the buy-side window opens
    # at 09:50 and closes with the session's own last-entry rule.
    trend_route_start:                       dtime = dtime(9, 50)
    trend_route_last_entry:                  dtime = dtime(14, 0)
    trend_route_min_minutes_left:            int   = 75
    # A breakout ticket is ATM-or-further in the trend direction; a 0DTE
    # long is never carried into the close.
    trend_route_min_dte:                     int   = 0
    trend_route_max_dte:                     int   = 4
    trend_route_dte0_flat_min:               int   = 30
    # Trend confirmation. The direction itself comes from the regime
    # layer's price classifier (which on the measured session printed
    # DOWNTREND from 09:48:50, fourteen minutes before ADX existed at all).
    # ADX on a 5-minute series needs 2*period+1 bars and is therefore 0.0
    # for the first ~50 minutes of every session, so requiring it at the
    # entry moment is how a route guarantees it only ever arrives after the
    # move: it is used as a CONFIRMING reading when present, never as a
    # precondition. The opening-range structure carries the confirmation
    # when it is absent.
    trend_route_adx_min:                     float = 20.0
    # The break must be a break, and it must be young. Both bounds are
    # expressed in opening ranges travelled from the level the trend broke
    # out of, which is scale-free (it means the same thing at 18,000 and at
    # 26,000), instrument-neutral and cannot be fitted to one session's
    # point count. On 2026-09-15 (OR width 135.8) the band was 20.4 to 81.5
    # points below OR low: the 09:50 print of 23416.7 sat at 40.4 (inside),
    # the 10:30 print of 23358 sat at 99.1 (outside — the move was spent).
    trend_route_or_break_frac:               float = 0.15
    trend_route_max_extension_frac:          float = 0.60
    trend_route_vwap_buffer_pts:             float = 8.0
    # Do-not-chase: refuse to buy an option that has already quadrupled its
    # premium since the open, and refuse a genuine vol spike outright.
    trend_route_max_iv_change_pct:           float = 30.0
    # The refusal families this route MAY answer. Every entry is a refusal
    # that assets something about the SELL side; nothing that asserts
    # something about the ACCOUNT or the DATA is present, because those are
    # re-checked by the route's own gate but must never be substituted away.
    trend_route_block_markers:               tuple = (
        # sell-side risk vetoes
        "iv_expanding_never_sell_into_rising_iv",
        "straddle_expanding",
        "straddle_explosion",
        "day_move_used_",
        "dangerous_to_sell_premium",
        "range_wide_or",
        # sell-side structure / calendar refusals a momentum ticket can answer
        "vol_buy_options",
        "before_entry_window_",
        "no_exception",
        "condor_requires_sell_premium",
        "requires_no_buy_options",
        "or_very_wide_too_wide",
        "immature",
    )
    # Sizing. The regime's size_multiplier is a NAKED-GAMMA schedule (on the
    # measured session it read 0.106 on a DTE-0 taper); a defined-risk long
    # with a 35% premium stop is not that trade, so the schedule is floored
    # and then stepped down by the three things that genuinely widen a long
    # option's loss distribution.
    trend_route_size_floor:                  float = 0.70
    trend_route_min_size_frac:               float = 0.35
    trend_route_dte0_size_mult:              float = 0.50
    trend_route_straddle_expanding_size_mult: float = 0.70
    trend_route_max_trades_per_day:          int   = 1
    # A day that has already spent several times its opening straddle has
    # no unpriced range left to sell - but a trend ticket is paid ON the
    # realised move, so this cap is deliberately far looser than the 125%
    # sell-side gate. It exists only to refuse a tape that has gone
    # parabolic (a 4x straddle day is not a breakout, it is an event).
    trend_route_day_move_max_pct:            float = 400.0
    # Risk ladder on the premium paid, per lot.
    trend_route_stop_frac:                   float = 0.35
    trend_route_target_frac:                 float = 0.60
    trend_route_lock_trigger:                float = 0.25
'''

CORE_CONFIG_ENV_ANCHOR = '''        momentum_block_markers=(
            tuple(
                s.strip().lower()
                for s in env.get("MOMENTUM_BLOCK_MARKERS", "").split(",")
                if s.strip()
            ) or Config.momentum_block_markers
        ),
'''

CORE_CONFIG_ENV_NEW = CORE_CONFIG_ENV_ANCHOR + '''        # ── v12 directional trend route ───────────────────────────────────
        trend_route_enabled=_get_bool(env, "TREND_ROUTE_ENABLED", True),
        trend_route_start=_get_time(env, "TREND_ROUTE_START", dtime(9, 50)),
        trend_route_last_entry=_get_time(
            env, "TREND_ROUTE_LAST_ENTRY", dtime(14, 0)
        ),
        trend_route_min_minutes_left=min(
            max(_get_int(env, "TREND_ROUTE_MIN_MINUTES_LEFT", 75), 20), 240
        ),
        trend_route_min_dte=min(
            max(_get_int(env, "TREND_ROUTE_MIN_DTE", 0), 0), 6
        ),
        trend_route_max_dte=min(
            max(_get_int(env, "TREND_ROUTE_MAX_DTE", 4), 0), 6
        ),
        trend_route_dte0_flat_min=min(
            max(_get_int(env, "TREND_ROUTE_DTE0_FLAT_MIN", 30), 5), 120
        ),
        trend_route_adx_min=min(
            max(_get_float(env, "TREND_ROUTE_ADX_MIN", 20.0), 0.0), 60.0
        ),
        trend_route_or_break_frac=min(
            max(_get_float(env, "TREND_ROUTE_OR_BREAK_FRAC", 0.15), 0.0), 1.00
        ),
        trend_route_max_extension_frac=min(
            max(_get_float(env, "TREND_ROUTE_MAX_EXTENSION_FRAC", 0.60), 0.16), 3.00
        ),
        trend_route_vwap_buffer_pts=min(
            max(_get_float(env, "TREND_ROUTE_VWAP_BUFFER_PTS", 8.0), 0.0), 80.0
        ),
        trend_route_max_iv_change_pct=min(
            max(_get_float(env, "TREND_ROUTE_MAX_IV_CHANGE_PCT", 30.0), 3.0), 120.0
        ),
        trend_route_block_markers=(
            tuple(
                s.strip().lower()
                for s in env.get("TREND_ROUTE_BLOCK_MARKERS", "").split(",")
                if s.strip()
            ) or Config.trend_route_block_markers
        ),
        trend_route_size_floor=min(
            max(_get_float(env, "TREND_ROUTE_SIZE_FLOOR", 0.70), 0.10), 1.00
        ),
        trend_route_min_size_frac=min(
            max(_get_float(env, "TREND_ROUTE_MIN_SIZE_FRAC", 0.35), 0.05), 1.00
        ),
        trend_route_dte0_size_mult=min(
            max(_get_float(env, "TREND_ROUTE_DTE0_SIZE_MULT", 0.50), 0.10), 1.00
        ),
        trend_route_straddle_expanding_size_mult=min(
            max(_get_float(env, "TREND_ROUTE_STRADDLE_EXPANDING_SIZE_MULT", 0.70),
                0.10), 1.00
        ),
        trend_route_max_trades_per_day=max(
            _get_int(env, "TREND_ROUTE_MAX_TRADES_PER_DAY", 1), 1
        ),
        trend_route_day_move_max_pct=min(
            max(_get_float(env, "TREND_ROUTE_DAY_MOVE_MAX_PCT", 400.0), 125.0),
            2000.0
        ),
        trend_route_stop_frac=min(
            max(_get_float(env, "TREND_ROUTE_STOP_FRAC", 0.35), 0.10), 0.70
        ),
        trend_route_target_frac=min(
            max(_get_float(env, "TREND_ROUTE_TARGET_FRAC", 0.60), 0.10), 3.00
        ),
        trend_route_lock_trigger=min(
            max(_get_float(env, "TREND_ROUTE_LOCK_TRIGGER", 0.25), 0.05), 1.00
        ),
'''


# ═════════════════════════════════════════════════════════════════════════════
# EDIT 2 — strategy_engine.py: compute_momentum_params() override hooks
# ═════════════════════════════════════════════════════════════════════════════
SE_MOM_SIG_ANCHOR = '''    def compute_momentum_params(
        self,
        direction:        int,
        selection_reason: str,
        signals:          dict,
        size_mult:        float,
    ) -> dict:
'''

SE_MOM_SIG_NEW = '''    def compute_momentum_params(
        self,
        direction:        int,
        selection_reason: str,
        signals:          dict,
        size_mult:        float,
        stop_frac:        Optional[float] = None,
        target_frac:      Optional[float] = None,
        lock_trigger:     Optional[float] = None,
        size_floor:       Optional[float] = None,
    ) -> dict:
'''

SE_MOM_STOP_ANCHOR = '''        stop_frac = float(getattr(cfg, "momentum_stop_frac", 0.35))
'''

SE_MOM_STOP_NEW = '''        # v12: the risk ladder is overridable so the directional trend route
        # can run its own (config-driven, defaulting to these) numbers
        # without duplicating a 200-line credit-free pricing pipeline. The
        # v5 defaults are unchanged when nothing is passed.
        stop_frac = float(
            stop_frac if stop_frac is not None
            else getattr(cfg, "momentum_stop_frac", 0.35)
        )
'''

SE_MOM_TARGET_ANCHOR = '''        target_frac = float(getattr(cfg, "momentum_target_frac", 0.60))
'''

SE_MOM_TARGET_NEW = '''        target_frac = float(
            target_frac if target_frac is not None
            else getattr(cfg, "momentum_target_frac", 0.60)
        )
'''

SE_MOM_SCHED_ANCHOR = '''        sched = max(float(size_mult or 1.0),
                    float(getattr(cfg, "momentum_size_floor", 0.80)))
'''

SE_MOM_SCHED_NEW = '''        # v12: size_floor is overridable. The trend route computes its own
        # schedule factor (regime schedule floored, then stepped down for
        # extension / DTE 0 / an expanding straddle) and must be able to
        # apply it verbatim; the v5 route keeps the 0.80 floor it was
        # measured with.
        _size_floor = float(
            size_floor if size_floor is not None
            else getattr(cfg, "momentum_size_floor", 0.80)
        )
        sched = max(float(size_mult or 1.0), _size_floor)
'''

SE_MOM_LOCK_ANCHOR = '''        lock_trigger     = net_debit * (
            1.0 + float(getattr(cfg, "momentum_lock_trigger", 0.25))
        )
'''

SE_MOM_LOCK_NEW = '''        _lock_frac = float(
            lock_trigger if lock_trigger is not None
            else getattr(cfg, "momentum_lock_trigger", 0.25)
        )
        lock_trigger     = net_debit * (1.0 + _lock_frac)
'''

SE_MOM_MARK_ANCHOR = '''            "momentum":               True,
            "momentum_direction":     int(direction),
        }
'''

SE_MOM_MARK_NEW = '''            "momentum":               True,
            "momentum_direction":     int(direction),
            "trend_route":            bool(trend_route),
            "stop_frac":              round(stop_frac, 4),
            "target_frac":            round(target_frac, 4),
        }
'''

# trend_route is threaded in as a real parameter, not a module global
SE_MOM_SIG_NEW2 = '''    def compute_momentum_params(
        self,
        direction:        int,
        selection_reason: str,
        signals:          dict,
        size_mult:        float,
        stop_frac:        Optional[float] = None,
        target_frac:      Optional[float] = None,
        lock_trigger:     Optional[float] = None,
        size_floor:       Optional[float] = None,
        trend_route:      bool = False,
    ) -> dict:
'''


# ═════════════════════════════════════════════════════════════════════════════
# EDIT 3 — strategy_engine.py: the trend route itself
# ═════════════════════════════════════════════════════════════════════════════
SE_ROUTE_ANCHOR = '''    def _momentum_decision(self, signals: dict, block_reason: str) -> Optional[dict]:
'''

SE_ROUTE_NEW = '''    # ─────────────────────────────────────────────────────────────────────
    # v12 — DIRECTIONAL TREND ROUTE (the buy-side answer to a sell-side veto)
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _tr_num(value, default: float = 0.0) -> float:
        """Float coercion that never raises inside a decision path."""
        try:
            if value is None:
                return default
            out = float(value)
            if out != out:          # NaN
                return default
            return out
        except (TypeError, ValueError):
            return default

    def _trend_route_gate(
        self,
        signals:      dict,
        block_reason: str,
        _test_time:   Optional[dtime] = None,
    ) -> Tuple[bool, str, int]:
        """May the DIRECTIONAL TREND ROUTE be considered on this cycle?

        Returns (allowed, why, direction), direction +1 = calls, -1 = puts.
        Every refusal is a distinct, greppable string so the reason a
        substitution did not happen is measurable rather than inferred.

        The route answers a refusal ONLY when the refusal is a statement
        about the sell side (see Config.trend_route_block_markers). It then
        validates the tape itself; it does not inherit the sell side's
        conclusion. Account-safety refusals (daily halt, ABORT, circuit
        breaker, VIX emergency, an open position, a cooldown, the stop
        counter) and market-data refusals (no opening range, stale chain)
        are never substitutable and are re-checked here as well, so a
        caller that reaches this function by some future path still cannot
        trade through them.
        """
        cfg = self.config
        if not bool(getattr(cfg, "trend_route_enabled", True)):
            return False, "trend_route_disabled", 0

        state = self.market_engine.state
        cur   = _test_time if _test_time is not None else now_ist().time()

        # ── 1. substitution only, and only for a sell-side family ─────────
        reason  = str(block_reason or "").lower()
        markers = tuple(getattr(cfg, "trend_route_block_markers", ())) or ()
        if not any(str(m).lower() in reason for m in markers):
            return False, (
                f"trend_route_sell_side_not_refused({reason[:40]})"
            ), 0

        # ── 2. never over a safety interlock ──────────────────────────────
        if signals.get("block_new_entries"):
            return False, "trend_route_regime_abort", 0
        if state.get("daily_halted"):
            return False, "trend_route_daily_halt", 0
        if signals.get("circuit_breaker_suspected"):
            return False, "trend_route_circuit_breaker", 0
        if signals.get("vix_spike_detected"):
            return False, "trend_route_vix_spike", 0
        if signals.get("chain_stale"):
            return False, "trend_route_chain_stale", 0
        if state.get("consecutive_stops", 0) >= 2:
            return False, "trend_route_two_consecutive_stops", 0
        if self._count_open_positions() > 0:
            return False, "trend_route_position_open", 0
        if signals.get("spot_velocity_block"):
            return False, (
                f"trend_route_spot_velocity_"
                f"{self._tr_num(signals.get('spot_velocity_pts')):.0f}pts_3min"
            ), 0

        # ── 3. calendar and clip limit ────────────────────────────────────
        dte = signals.get("actual_dte")
        if dte is None:
            return False, "trend_route_no_expiry_resolution", 0
        try:
            dte_i = int(dte)
        except (TypeError, ValueError):
            return False, "trend_route_dte_unparseable", 0
        if dte_i < int(getattr(cfg, "trend_route_min_dte", 0)):
            return False, f"trend_route_dte_{dte_i}_below_min", 0
        if dte_i > int(getattr(cfg, "trend_route_max_dte", 4)):
            return False, f"trend_route_dte_{dte_i}_above_max", 0

        used = self._count_momentum_entries()
        if used >= int(getattr(cfg, "trend_route_max_trades_per_day", 1)):
            return False, f"trend_route_daily_limit_{used}_reached", 0

        # ── 4. the direction is the regime layer's own trend label ────────
        price = str(signals.get("price_regime") or "")
        if price in ("UPTREND", "STRONG_UPTREND"):
            direction = 1
        elif price in ("DOWNTREND", "STRONG_DOWNTREND"):
            direction = -1
        else:
            return False, f"trend_route_needs_trend_got_{price or 'NONE'}", 0

        conf = str(signals.get("confidence_level") or "")
        if conf not in ("HIGH", "MEDIUM"):
            return False, f"trend_route_confidence_{conf or 'NONE'}_insufficient", 0

        # ── 5. the opening-range break, with a scale-free band ────────────
        spot = self._tr_num(signals.get("spot"))
        if spot <= 0:
            return False, "trend_route_no_spot", 0
        if not signals.get("or_computed"):
            return False, "trend_route_no_opening_range", 0
        or_high = self._tr_num(signals.get("or_high"))
        or_low  = self._tr_num(signals.get("or_low"))
        or_w    = self._tr_num(signals.get("or_width"))
        if or_high <= 0 or or_low <= 0 or or_w <= 0:
            return False, "trend_route_opening_range_incomplete", 0

        _brk_frac = float(getattr(cfg, "trend_route_or_break_frac", 0.15))
        _max_frac = float(getattr(cfg, "trend_route_max_extension_frac", 0.60))
        if _max_frac <= _brk_frac:
            return False, "trend_route_extension_band_misconfigured", 0
        need = max(5.0, or_w * _brk_frac)
        ext_cap = or_w * _max_frac

        if direction < 0:
            brk = or_low - spot
            if brk < need:
                return False, (
                    f"trend_route_put_not_through_or_low_"
                    f"{spot:.0f}>{or_low - need:.0f}"
                ), 0
        else:
            brk = spot - or_high
            if brk < need:
                return False, (
                    f"trend_route_call_not_through_or_high_"
                    f"{spot:.0f}<{or_high + need:.0f}"
                ), 0
        if brk > ext_cap:
            return False, (
                f"trend_route_break_{brk:.0f}pts_is_{brk / or_w:.2f}x_or_"
                f"beyond_{_max_frac:.2f}x_no_chase"
            ), 0

        # ── 6. VWAP on the trend side ─────────────────────────────────────
        vwap = self._tr_num(signals.get("vwap"))
        if vwap > 0:
            _vbuf = float(getattr(cfg, "trend_route_vwap_buffer_pts", 8.0))
            if direction < 0 and spot > vwap - _vbuf:
                return False, f"trend_route_put_not_under_vwap_{vwap:.0f}", 0
            if direction > 0 and spot < vwap + _vbuf:
                return False, f"trend_route_call_not_over_vwap_{vwap:.0f}", 0

        # ── 7. confirmation: the opening-range structure AND the trend read
        want = "UPTREND" if direction > 0 else "DOWNTREND"
        orb  = str(signals.get("orb_price_regime") or "")
        adx  = self._tr_num(signals.get("adx_15"))
        adx_min = float(getattr(cfg, "trend_route_adx_min", 20.0))
        adx_mature = bool(signals.get("adx_15_mature"))
        if orb != want:
            return False, f"trend_route_orb_{orb or 'NONE'}_disagrees", 0
        if not (adx >= adx_min or (adx <= 0.0 and not adx_mature)):
            return False, (
                f"trend_route_adx_{adx:.0f}_immature_reading_below_"
                f"{adx_min:.0f}"
            ), 0

        # ── 8. do not buy the top of an IV spike ─────────────────────────
        ivb = str(signals.get("iv_behavior") or "UNKNOWN")
        if ivb == "SPIKING":
            return False, "trend_route_iv_spiking", 0
        iv_chg = self._tr_num(signals.get("iv_change_pct_from_open"))
        iv_cap = float(getattr(cfg, "trend_route_max_iv_change_pct", 30.0))
        if iv_chg > iv_cap:
            return False, (
                f"trend_route_iv_change_{iv_chg:.0f}pct_above_{iv_cap:.0f}"
            ), 0

        # ── 9. a day already past its statistical range is not fresh ─────
        dmu = self._tr_num(signals.get("day_move_used_pct"))
        dmu_cap = float(getattr(cfg, "trend_route_day_move_max_pct", 400.0))
        if dmu >= dmu_cap:
            return False, (
                f"trend_route_day_move_{dmu:.0f}pct_exhausted"
            ), 0

        # ── 10. its own entry window, inside the session's rules ─────────
        try:
            tr_start = datetime.strptime(
                getattr(cfg, "trend_route_start",
                        dtime(9, 50)).strftime("%H:%M"), "%H:%M"
            ).time()
        except Exception:
            tr_start = dtime(9, 50)
        try:
            tr_end = datetime.strptime(
                getattr(cfg, "trend_route_last_entry",
                        dtime(14, 0)).strftime("%H:%M"), "%H:%M"
            ).time()
        except Exception:
            tr_end = dtime(14, 0)
        # The session's own last-entry rule still applies on top: a Tuesday
        # 0DTE session must not take a NEW directional ticket after its
        # 13:00 cut-off, and no session may take one after trading_window
        # last entry. This route may start EARLIER than entry_start; it may
        # never end later.
        try:
            sess_end = datetime.strptime(
                state.get("entry_end", "14:00"), "%H:%M"
            ).time()
        except Exception:
            sess_end = cfg.trading_window_last_entry
        eff_start = max(tr_start, dtime(9, 15))
        eff_end   = min(tr_end, sess_end)
        if cur < eff_start:
            return False, f"trend_route_before_{eff_start}", 0
        if cur > eff_end:
            return False, f"trend_route_past_{eff_end}", 0

        try:
            hard_exit = datetime.strptime(
                state.get("hard_exit_time", "15:00"), "%H:%M"
            ).time()
        except Exception:
            hard_exit = cfg.hard_exit_time
        mins_left = self._minutes_to_time(cur, hard_exit)
        _min_left = float(getattr(cfg, "trend_route_min_minutes_left", 75))
        if mins_left < _min_left:
            return False, (
                f"trend_route_only_{mins_left:.0f}min_before_hard_exit"
            ), 0

        return True, "trend_route_gate_open", direction

    def trend_route_size_plan(self, signals: dict, direction: int) -> dict:
        """Size the trend ticket, and say why in numbers.

        Four independent multipliers. None of them is fitted to a session;
        each answers a question about the distribution of a long option:

          regime schedule : the regime layer's size_multiplier, floored —
                            it is a naked-gamma schedule, this is a
                            defined-risk ticket.
          extension       : how far the break has already travelled, as a
                            fraction of the young-to-extended band. Linear
                            from 1.0 at the band's start to
                            trend_route_min_size_frac at its edge.
          DTE 0           : a 0DTE long is a more binary instrument (pin
                            risk, an accelerating theta, no overnight
                            optionality) and gets a permanent haircut.
          straddle expand : the market is repricing realised vol in real
                            time. Right side, wrong entry — take less.
        """
        cfg   = self.config
        state = self.market_engine.state
        out = {
            "size_mult": 1.0, "multipliers": {}, "refuse": False, "reason": ""
        }

        raw_sched = self._tr_num(signals.get("size_multiplier"), 1.0)
        if raw_sched <= 0:
            raw_sched = float(getattr(cfg, "trend_route_size_floor", 0.70))
        floor = float(getattr(cfg, "trend_route_size_floor", 0.70))
        base  = max(raw_sched, floor)

        # extension multiplier
        spot = self._tr_num(signals.get("spot"))
        or_high = self._tr_num(signals.get("or_high"))
        or_low  = self._tr_num(signals.get("or_low"))
        or_w    = self._tr_num(signals.get("or_width"))
        brk_frac = float(getattr(cfg, "trend_route_or_break_frac", 0.15))
        max_frac = float(getattr(cfg, "trend_route_max_extension_frac", 0.60))
        min_size = float(getattr(cfg, "trend_route_min_size_frac", 0.35))
        if or_w > 0 and spot > 0:
            brk = (or_low - spot) if direction < 0 else (spot - or_high)
            ext = max(brk / or_w, 0.0)
        else:
            ext = 0.0
        span = max(max_frac - brk_frac, 1e-6)
        ext_frac = min(max((ext - brk_frac) / span, 0.0), 1.0)
        ext_mult = 1.0 - ext_frac * (1.0 - min_size)

        # DTE multiplier
        dte_i = int(self._tr_num(signals.get("actual_dte"), 1.0))
        dte_mult = (
            float(getattr(cfg, "trend_route_dte0_size_mult", 0.50))
            if dte_i == 0 else 1.0
        )

        # straddle-expansion multiplier
        straddle_mult = (
            float(getattr(cfg, "trend_route_straddle_expanding_size_mult", 0.70))
            if signals.get("straddle_expanding") else 1.0
        )

        size = base * ext_mult * dte_mult * straddle_mult
        out["size_mult"] = round(size, 6)
        out["multipliers"] = {
            "regime_schedule_raw": round(raw_sched, 4),
            "regime_schedule_floored": round(base, 4),
            "extension": round(ext_mult, 4),
            "extension_frac_of_band": round(ext_frac, 4),
            "extension_x_or": round(ext, 4),
            "dte0": round(dte_mult, 4),
            "straddle_expanding": round(straddle_mult, 4),
            "final": round(size, 4),
        }
        _cap = float(getattr(cfg, "trend_route_size_floor", 0.70))
        if size > _cap:
            out["size_mult"] = _cap
            out["multipliers"]["final"] = round(_cap, 4)
            out["multipliers"]["capped_at"] = _cap
        return out

    def compute_trend_params(
        self,
        direction:        int,
        selection_reason: str,
        signals:          dict,
        size_mult:        float,
    ) -> dict:
        """Trend-route parameters, on the momentum pipeline's own economics.

        The position geometry is identical to the v5 long-premium ticket (one
        leg, ATM-or-further in the trend direction, |delta| nearest 0.55, band
        0.35-0.75, premium between 0.18% and 0.90% of spot), because that
        geometry was chosen for exactly this trade and is not the defect. The
        only differences are the risk ladder (configurable), the size floor
        (applied verbatim rather than floored at 0.80) and the provenance
        stamps the exit ladder reads.
        """
        cfg = self.config
        params = self.compute_momentum_params(
            direction, selection_reason, signals, size_mult,
            stop_frac=float(getattr(cfg, "trend_route_stop_frac", 0.35)),
            target_frac=float(getattr(cfg, "trend_route_target_frac", 0.60)),
            lock_trigger=float(getattr(cfg, "trend_route_lock_trigger", 0.25)),
            size_floor=float(size_mult),
            trend_route=True,
        )
        if params.get("valid"):
            params["trend_route"] = True
            params["selection_reason"] = selection_reason
        return params

    def _trend_route_decision(
        self, signals: dict, block_reason: str
    ) -> Optional[dict]:
        """Long-premium trend substitute for a refused sell-side structure.

        Returns a complete ENTER decision or None. When it returns None the
        caller keeps the ORIGINAL refusal unaltered as the logged reason —
        this route is never allowed to rewrite why the sell side was refused.
        Its own verdict is still recorded (signals["trend_route_verdict"],
        persisted into strategy_decisions.signals_json) so the census and a
        forensic query both stay honest.
        """
        try:
            ok, why, direction = self._trend_route_gate(signals, block_reason)
        except Exception as exc:
            self.logger.warning(f"trend route gate failed: {exc}")
            signals["trend_route_verdict"] = f"trend_route_error:{exc}"
            return None
        signals["trend_route_verdict"] = why
        if not ok:
            self.logger.info(f"TREND ROUTE refused: {why}")
            return None

        plan = self.trend_route_size_plan(signals, direction)
        signals["trend_route_size_plan"] = plan.get("multipliers")
        if plan.get("refuse"):
            signals["trend_route_verdict"] = plan.get("reason") or "size_refused"
            self.logger.info(f"TREND ROUTE sizing refused: {plan.get('reason')}")
            return None

        _mv = plan.get("multipliers") or {}
        reason = (
            f"trend_route:{'LONG_CALL' if direction > 0 else 'LONG_PUT'}"
            f":dte={signals.get('actual_dte')}"
            f":adx={self._tr_num(signals.get('adx_15')):.0f}"
            f":conf={signals.get('confidence_level')}"
            f":ext={_mv.get('extension_x_or', 0.0)}x_or"
            f":size={_mv.get('final', 1.0)}"
            f":replacing={block_reason}"
        )
        params = self.compute_trend_params(
            direction, reason, signals, plan["size_mult"]
        )
        if not params.get("valid"):
            signals["trend_route_verdict"] = (
                f"trend_route_params_invalid:{params.get('reason')}"
            )
            self.logger.info(
                f"trend route ticket rejected: {params.get('reason')}"
            )
            return None

        strat_name = params["strategy_name"]
        self._log_decision(signals, "STRATEGY_SELECTED", reason, strat_name, params)
        self._persist_decision(signals, strat_name, reason, params,
                               "STRATEGY_SELECTED")
        self.market_engine.finalize_cycle_log(
            f"STRATEGY_SELECTED:{strat_name}", None, self._count_open_positions()
        )
        state = self.market_engine.state
        state["momentum_entries"] = int(state.get("momentum_entries", 0) or 0) + 1
        state["trend_route_entries"] = int(
            state.get("trend_route_entries", 0) or 0
        ) + 1
        return {
            "action":        "ENTER",
            "strategy_name": strat_name,
            "reason":        reason,
            "params":        params,
        }

    def _debit_substitute(
        self, signals: dict, block_reason: str
    ) -> Optional[dict]:
        """Long-premium substitutes for a refused sell-side ticket.

        Order matters and is deliberate: the v5 momentum route is asked
        FIRST, exactly as before v12, so every day that already traded keeps
        its behaviour. The trend route only ever sees what the momentum
        route refused.
        """
        alt = self._momentum_decision(signals, block_reason)
        if alt is not None:
            return alt
        try:
            return self._trend_route_decision(signals, block_reason)
        except Exception as exc:                    # never lose the day to
            self.logger.warning(f"trend route failed: {exc}")   # a new path
            return None

''' + SE_ROUTE_ANCHOR


# ═════════════════════════════════════════════════════════════════════════════
# EDIT 4 — strategy_engine.py: decide() uses the substitution entry point
# ═════════════════════════════════════════════════════════════════════════════
SE_DECIDE_A_ANCHOR = '''            if action == "NO_TRADE":
                alt = self._momentum_decision(signals, reason)
                if alt is not None:
                    return alt
'''
SE_DECIDE_A_NEW = '''            if action == "NO_TRADE":
                alt = self._debit_substitute(signals, reason)
                if alt is not None:
                    return alt
'''

SE_DECIDE_B_ANCHOR = '''        if strategy_name == "NO_TRADE":
            alt = self._momentum_decision(signals, selection_reason)
            if alt is not None:
                return alt
'''
SE_DECIDE_B_NEW = '''        if strategy_name == "NO_TRADE":
            alt = self._debit_substitute(signals, selection_reason)
            if alt is not None:
                return alt
'''

SE_DECIDE_C_ANCHOR = '''            full_reason = f"params_invalid:{params.get('reason', 'unknown')}"
            alt = self._momentum_decision(signals, full_reason)
'''
SE_DECIDE_C_NEW = '''            full_reason = f"params_invalid:{params.get('reason', 'unknown')}"
            alt = self._debit_substitute(signals, full_reason)
'''


# ═════════════════════════════════════════════════════════════════════════════
# EDIT 5 — execution_engine.py: DTE-0 unconditional flat for trend tickets
# ═════════════════════════════════════════════════════════════════════════════
XE_DEBIT_ANCHOR = '''        window = float(getattr(cfg, "momentum_final_window_min", 45))
        if mins_left <= window:
'''

XE_DEBIT_NEW = '''        window = float(getattr(cfg, "momentum_final_window_min", 45))
        # ── D3b [v12]: a 0DTE long is flat before the bell, in profit or not
        # D4 below flattens a long inside the final window only IF it is
        # worth more than it cost. That is the right rule for a two-session
        # contract and the wrong one for a ticket that expires today: an
        # out-of-the-money 0DTE option that is not in profit at 14:30 has a
        # theta curve, not a chance, and holding it to the 15:00 bell books
        # the whole premium. Expiry-day longs are therefore flattened on the
        # clock, unconditionally, at trend_route_dte0_flat_min before the
        # hard exit. Only the v12 trend route can hold a DTE-0 long (the v5
        # route is gated at DTE >= 1 and the credit ladder never holds a
        # debit position at all), so nothing that traded before this patch
        # changes behaviour here.
        _raw_dte = raw.get("actual_dte")
        if (_raw_dte is not None and int(_raw_dte) == 0
                and raw.get("trend_route")):
            _flat_min = float(getattr(cfg, "trend_route_dte0_flat_min", 30))
            if mins_left <= _flat_min:
                self.logger.info(
                    f"0DTE TREND FLAT: {position['strategy_name']} "
                    f"{mins_left:.0f}min to hard exit, value={value:.2f} "
                    f"(entry {entry_value:.2f})"
                )
                return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                    "reason_detail": "trend_route_dte0_time_flat",
                    "minutes_left":  mins_left,
                    "value":         value,
                }
        if mins_left <= window:
'''


# ═════════════════════════════════════════════════════════════════════════════
# EDIT 6 — backtest_engine.py: diagnostics (gate placement + bucketing)
# ═════════════════════════════════════════════════════════════════════════════
BT_STAGE_A_ANCHOR = '''        "RANGE_UNCLEAR", "STRADDLE_EXPLOSION", "or_not_established",
'''
BT_STAGE_A_NEW = '''        "RANGE_UNCLEAR", "STRADDLE_EXPLOSION", "or_not_established",
        # v12: the regime layer's own wide-opening-range veto. It was
        # unclassified, so 10 of 2026-09-15's 484 refusals were dropped from
        # the funnel entirely - and they were the first 10 of the session.
        "wide_or",
'''

BT_STAGE_B_ANCHOR = '''    ("safety interlocks", (
        "vix", "circuit_breaker", "expanding", "spiking", "daily_loss_halt",
        "daily", "abort")),
'''
BT_STAGE_B_NEW = '''    ("safety interlocks", (
        "vix", "circuit_breaker", "expanding", "spiking", "daily_loss_halt",
        "daily", "abort",
        # v12: these two sit inside _check_hard_gates' safety block, between
        # the IV veto and the entry window. Unclassified, "day_move" alone
        # hid 110 of 484 refusals on 2026-09-15 - a quarter of the session.
        "day_move", "trend_route", "momentum")),
'''

BT_STAGE_C_ANCHOR = '''        "credit_risk", "wing_cost", "condor_weak_side", "friction",
'''
BT_STAGE_C_NEW = '''        "credit_risk", "wing_cost", "condor_weak_side", "friction",
        # v12: _validate_entry_rules() refusals arrive prefixed
        # "strategy_rules_failed:". Without this key every one of them
        # became its own bucket (the strike and the distance are in the
        # string), so a gate that fires all session read as dozens of
        # singletons.
        "strategy_rules_failed",
'''

BT_BUCKET_ANCHOR = '''            "vix", "dte", "spread", "liquidity", "regime",
'''
BT_BUCKET_NEW = '''            "vix", "dte", "spread", "liquidity", "regime",
            "strategy_rules_failed", "wide_or", "trend_route", "momentum",
'''


EDITS = [
    # (file, label, anchor, replacement)
    ("core.py", "Config: trend_route block", CORE_CONFIG_ANCHOR, CORE_CONFIG_NEW),
    ("core.py", "load_config: trend_route env overrides",
     CORE_CONFIG_ENV_ANCHOR, CORE_CONFIG_ENV_NEW),
    ("strategy_engine.py", "compute_momentum_params: override hooks",
     SE_MOM_SIG_ANCHOR, SE_MOM_SIG_NEW2),
    ("strategy_engine.py", "compute_momentum_params: stop_frac override",
     SE_MOM_STOP_ANCHOR, SE_MOM_STOP_NEW),
    ("strategy_engine.py", "compute_momentum_params: target_frac override",
     SE_MOM_TARGET_ANCHOR, SE_MOM_TARGET_NEW),
    ("strategy_engine.py", "compute_momentum_params: size_floor override",
     SE_MOM_SCHED_ANCHOR, SE_MOM_SCHED_NEW),
    ("strategy_engine.py", "compute_momentum_params: lock_trigger override",
     SE_MOM_LOCK_ANCHOR, SE_MOM_LOCK_NEW),
    ("strategy_engine.py", "compute_momentum_params: provenance stamps",
     SE_MOM_MARK_ANCHOR, SE_MOM_MARK_NEW),
    ("strategy_engine.py", "the directional trend route",
     SE_ROUTE_ANCHOR, SE_ROUTE_NEW),
    ("strategy_engine.py", "decide(): hard-gate substitution",
     SE_DECIDE_A_ANCHOR, SE_DECIDE_A_NEW),
    ("strategy_engine.py", "decide(): regime-verdict substitution",
     SE_DECIDE_B_ANCHOR, SE_DECIDE_B_NEW),
    ("strategy_engine.py", "decide(): params-invalid substitution",
     SE_DECIDE_C_ANCHOR, SE_DECIDE_C_NEW),
    ("execution_engine.py", "debit ladder: 0DTE unconditional flat",
     XE_DEBIT_ANCHOR, XE_DEBIT_NEW),
    ("backtest_engine.py", "STAGE_ORDER: regime wide_or",
     BT_STAGE_A_ANCHOR, BT_STAGE_A_NEW),
    ("backtest_engine.py", "STAGE_ORDER: safety day_move",
     BT_STAGE_B_ANCHOR, BT_STAGE_B_NEW),
    ("backtest_engine.py", "STAGE_ORDER: entry rules",
     BT_STAGE_C_ANCHOR, BT_STAGE_C_NEW),
    ("backtest_engine.py", "_bucket: new gate families",
     BT_BUCKET_ANCHOR, BT_BUCKET_NEW),
]


# ═════════════════════════════════════════════════════════════════════════════
# APPLY / VERIFY
# ═════════════════════════════════════════════════════════════════════════════
def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# IDEMPOTENCY PROBES
# ─────────────────────────────────────────────────────────────────────────────
# One line per edit that exists in the patched file EXACTLY ONCE and does not
# exist anywhere in the pre-patch file. Both properties were machine-checked
# against the pristine post-v11 bytes when this patch was authored
# (`before.count(probe) == 0 and after.count(probe) == 1` for every row), and
# plan_edits() re-checks the "exactly once" half on every run.
#
# It has to be an explicit table. The obvious rules do not work:
#   * the anchor is useless for an INSERTION edit - the route block is spliced
#     in ahead of `_momentum_decision`, so that signature is still present
#     afterwards and anchor-count alone would splice the block in twice;
#   * "the first changed line" does not work either - the first changed line
#     of the target_frac override is `target_frac = float(`, which occurs
#     verbatim in compute_params() too, and a probe that matches an untouched
#     file silently SKIPS the edit;
#   * "the longest changed line" picks
#     `self._log_decision(signals, "STRATEGY_SELECTED", reason, strat_name,
#     params)`, which is copied from _momentum_decision and appears twice.
# Hence: chosen by machine, verified by machine, recorded here.
PROBES = {
    "core.py": {
        "Config: trend_route block":
            "# markers, so it never sees an IV-expansion or day-move refusal (the two",
        "load_config: trend_route env overrides":
            'max(_get_float(env, "TREND_ROUTE_MAX_EXTENSION_FRAC", 0.60), 0.16), 3.00',
    },
    "strategy_engine.py": {
        "compute_momentum_params: override hooks":
            "stop_frac:        Optional[float] = None,",
        "compute_momentum_params: stop_frac override":
            "# v12: the risk ladder is overridable so the directional trend route",
        "compute_momentum_params: target_frac override":
            'else getattr(cfg, "momentum_target_frac", 0.60)',
        "compute_momentum_params: size_floor override":
            "# v12: size_floor is overridable. The trend route computes its own",
        "compute_momentum_params: lock_trigger override":
            "lock_trigger     = net_debit * (1.0 + _lock_frac)",
        "compute_momentum_params: provenance stamps":
            '"target_frac":            round(target_frac, 4),',
        "the directional trend route":
            "# v12 \u2014 DIRECTIONAL TREND ROUTE (the buy-side answer to a sell-side veto)",
        "decide(): hard-gate substitution":
            "alt = self._debit_substitute(signals, reason)",
        "decide(): regime-verdict substitution":
            "alt = self._debit_substitute(signals, selection_reason)",
        "decide(): params-invalid substitution":
            "alt = self._debit_substitute(signals, full_reason)",
    },
    "execution_engine.py": {
        "debit ladder: 0DTE unconditional flat":
            "# \u2500\u2500 D3b [v12]: a 0DTE long is flat before the bell, in profit or not",
    },
    "backtest_engine.py": {
        "STAGE_ORDER: regime wide_or":
            "# unclassified, so 10 of 2026-09-15's 484 refusals were dropped from",
        "STAGE_ORDER: safety day_move":
            "# v12: these two sit inside _check_hard_gates' safety block, between",
        "STAGE_ORDER: entry rules":
            "# became its own bucket (the strike and the distance are in the",
        "_bucket: new gate families":
            '"strategy_rules_failed", "wide_or", "trend_route", "momentum",',
    },
}


def applied_probe(fname: str, label: str) -> str:
    """The machine-verified "this edit is already here" marker. See PROBES."""
    return (PROBES.get(fname) or {}).get(label, "")


def plan_edits(dry_run: bool) -> int:
    """Work out every edit before writing anything. Fail closed."""
    texts: dict = {}
    pending: dict = {}
    probes: dict = {}
    problems = []

    for fname in sorted({e[0] for e in EDITS}):
        p = BASE / fname
        if not p.exists():
            problems.append(f"{fname}: file not found")
            continue
        txt = read(p)
        texts[fname] = txt
        if MARK in txt:
            pass  # idempotency is decided per-edit below
        pending[fname] = []

    for fname, label, anchor, new in EDITS:
        if fname not in texts:
            continue
        txt = texts[fname]
        # Is the file byte-identical to the pre-patch baseline? Then every
        # edit in it is outstanding and no probe may be consulted - a probe
        # false-positive on a pristine file would silently drop an edit,
        # which is the one failure mode this whole function exists to make
        # impossible.
        if md5(BASE / fname) != BASELINE_MD5.get(fname):
            # Already applied? Ask before touching the anchor: an
            # insertion-style edit leaves its anchor in place, so
            # anchor-count alone would splice the same block in twice.
            probe = applied_probe(fname, label)
            if probe and probe in txt:
                print(f"  [skip ] {fname:22s} {label}  (already applied)")
                continue
        n = txt.count(anchor)
        if n == 0:
            problems.append(
                f"{fname}: anchor not found for '{label}'. The file has "
                f"probably been changed by hand or by another patch. "
                f"Refusing to guess."
            )
            continue
        if n > 1:
            problems.append(
                f"{fname}: anchor for '{label}' matches {n} times "
                f"(expected exactly 1). Refusing to guess."
            )
            continue
        texts[fname] = txt.replace(anchor, new, 1)
        pending[fname].append(label)
        probes.setdefault(fname, []).append((label, applied_probe(fname, label)))
        print(f"  [edit ] {fname:22s} {label}")

    if problems:
        print()
        print("  ABORTED — nothing written:")
        for p in problems:
            print(f"    * {p}")
        return 2

    # Every probe an edit will rely on next time must be present in the
    # result, or a re-run would splice a block in a second time.
    for fname, items in probes.items():
        for label, probe in items:
            if probe and texts[fname].count(probe) != 1:
                problems.append(
                    f"{fname}: the idempotency probe for '{label}' appears "
                    f"{texts[fname].count(probe)} times in the patched file "
                    f"(expected 1). Refusing to write a tree that a second "
                    f"run would double-apply."
                )
    if problems:
        print()
        print("  ABORTED — nothing written:")
        for pr in problems:
            print(f"    * {pr}")
        return 2

    changed = {f: t for f, t in texts.items()
               if pending.get(f) and t != read(BASE / f)}
    if not changed:
        print()
        print("  Nothing to do: every edit is already present.")
        return 0

    if dry_run:
        print()
        for f in sorted(changed):
            print(f"  would rewrite {f} ({len(pending[f])} edit(s))")
        return 0

    # Write atomically: compile-check every file in a scratch copy FIRST, so
    # a syntax error can never be left on disk.
    tmpdir = Path(tempfile.mkdtemp(prefix="patch_v12_"))
    try:
        for f, txt in changed.items():
            (tmpdir / f).write_text(txt, encoding="utf-8")
        for f in changed:
            try:
                py_compile.compile(str(tmpdir / f), doraise=True)
            except py_compile.PyCompileError as exc:
                print(f"\n  ABORTED — {f} would not compile:\n{exc}")
                return 3
        # Backups, then the real write.
        for f, txt in changed.items():
            (BASE / f"{f}.v11.bak").write_text(
                read(BASE / f), encoding="utf-8"
            )
            (BASE / f).write_text(txt, encoding="utf-8")
            print(f"  [write] {f}  ({len(pending[f])} edit(s); "
                  f"backup {f}.v11.bak)")
    finally:
        # py_compile drops a __pycache__ beside the scratch copies, so this
        # cannot be a glob-and-unlink.
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────
def verify(deep: bool = True) -> int:
    print()
    print("─" * 78)
    print("VERIFY")
    print("─" * 78)
    failures = []

    # 1. every touched file compiles and imports
    for f in sorted({e[0] for e in EDITS}):
        try:
            py_compile.compile(str(BASE / f), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"{f} does not compile: {exc}")
            continue
        print(f"  [ok] compile    {f}")

    sys.path.insert(0, str(BASE))
    for mod in ("core", "strategy_engine", "execution_engine", "backtest_engine",
                "data_engine", "regime_engine", "calibration_engine",
                "eod_report", "main"):
        try:
            __import__(mod)
            print(f"  [ok] import     {mod}")
        except Exception as exc:
            failures.append(f"import {mod} failed: {exc!r}")

    if failures:
        print()
        for f in failures:
            print(f"  FAIL {f}")
        return 1

    # 2. the new surface actually exists
    import core
    import strategy_engine as se
    import execution_engine as xe
    import backtest_engine as bt

    cfg = core.load_config()
    need_cfg = [
        "trend_route_enabled", "trend_route_start", "trend_route_last_entry",
        "trend_route_min_minutes_left", "trend_route_min_dte",
        "trend_route_max_dte", "trend_route_dte0_flat_min",
        "trend_route_adx_min", "trend_route_or_break_frac",
        "trend_route_max_extension_frac", "trend_route_vwap_buffer_pts",
        "trend_route_max_iv_change_pct", "trend_route_block_markers",
        "trend_route_size_floor", "trend_route_min_size_frac",
        "trend_route_dte0_size_mult",
        "trend_route_straddle_expanding_size_mult",
        "trend_route_max_trades_per_day", "trend_route_stop_frac",
        "trend_route_target_frac", "trend_route_lock_trigger",
        "trend_route_day_move_max_pct",
    ]
    missing = [f for f in need_cfg if not hasattr(cfg, f)]
    if missing:
        failures.append(f"Config missing: {missing}")
    else:
        print(f"  [ok] config     {len(need_cfg)} trend_route_* fields")

    for attr in ("_trend_route_gate", "_trend_route_decision",
                 "_debit_substitute", "compute_trend_params",
                 "trend_route_size_plan", "_tr_num"):
        if not hasattr(se.StrategyEngine, attr):
            failures.append(f"StrategyEngine.{attr} missing")
    print("  [ok] strategy   trend route methods present")

    try:
        _unit_debit_ladder()
        print("  [ok] unit       debit exit ladder (0DTE trend flat)")
    except AssertionError as exc:
        failures.append(f"debit-ladder unit test: {exc}")
    except Exception as exc:
        failures.append(f"debit-ladder unit test crashed: {exc!r}")

    _stages = {k for _n, keys in bt.STAGE_ORDER for k in keys}
    for k in ("day_move", "wide_or", "strategy_rules_failed"):
        if k not in _stages:
            failures.append(f"STAGE_ORDER missing key {k}")
    print("  [ok] backtest   gate placement")

    # 3. unit-level behaviour of the new gate, on synthetic signals
    try:
        _unit_trend_route()
        print("  [ok] unit       trend-route gate accept/refuse matrix")
    except AssertionError as exc:
        failures.append(f"trend-route unit test: {exc}")
    except Exception as exc:
        failures.append(f"trend-route unit test crashed: {exc!r}")

    # 4. the modules' own self-tests
    if deep:
        for mod in ("data_engine", "regime_engine", "execution_engine",
                    "strategy_engine", "calibration_engine"):
            rc = subprocess.run(
                [PYTHON, str(BASE / f"{mod}.py")],
                cwd=str(BASE), capture_output=True, text=True,
            ).returncode
            if rc == 0:
                print(f"  [ok] self-test  {mod}.py")
            else:
                failures.append(f"{mod}.py self-test returned {rc}")
        rc = subprocess.run(
            [PYTHON, str(BASE / "backtest_engine.py"), "--test"],
            cwd=str(BASE), capture_output=True, text=True,
        ).returncode
        if rc == 0:
            print("  [ok] self-test  backtest_engine.py --test")
        else:
            failures.append("backtest_engine.py --test failed")

    print()
    if failures:
        print("VERIFICATION FAILED")
        for f in failures:
            print(f"  * {f}")
        return 1
    print("VERIFICATION PASSED")
    return 0


def _unit_debit_ladder() -> None:
    """Drive the exit ladder for a long position by hand.

    The 0DTE time-flat (D3b) is the one risk control v12 adds to a position,
    and asserting that the string "trend_route_dte0_time_flat" occurs in
    execution_engine.py proves nothing about whether it fires - which is not
    hypothetical, because when it was first written it did NOT fire:
    `int(raw.get("actual_dte") or -1)` evaluates to -1 when DTE is 0, so the
    guard was dead on exactly the day it was written for. This test is what
    caught it.

    The ladder is a pure function of (position, legs, chain, clock, marks)
    plus self.config / self.logger / self.db, so it is called here through a
    duck-typed `self` and asserted on behaviour, including the ordering the
    rungs must keep against the rungs that already existed.
    """
    import logging
    import json

    import core
    import execution_engine as xe

    cfg = core.load_config()
    log = logging.getLogger("patch_v12_ladder")
    log.handlers = [logging.NullHandler()]
    log.propagate = False

    class _FakeDB:
        def __init__(self):
            self.updates = []

        def update(self, table, values, where):
            self.updates.append((table, values, where))

    class _FakeSelf:
        def __init__(self):
            self.config = cfg
            self.logger = log
            self.db = _FakeDB()

        def _round_trip_cost_pts(self, legs, chain):
            return 1.0

    mon = xe.ExecutionEngine._monitor_debit_position
    legs = [{"leg_status": "OPEN", "action": "BUY", "strike": 23450.0,
             "option_type": "put", "entry_delta": -0.55}]
    chain = {23450.0: {"put": {"bid": 60.0, "ask": 60.2, "ltp": 60.1,
                               "delta": -0.55}}}
    TREND = json.dumps({"actual_dte": 0, "trend_route": True})
    V5 = json.dumps({"actual_dte": 0, "trend_route": False})

    def pos(**over):
        out = {
            "position_id": "UT1",
            "strategy_name": "LONG_PUT",
            "strategy_type": "BUY",
            "entry_credit": -70.0,          # the premium paid
            "stop_premium": 45.0,           # 70 minus the 35% stop
            "target_premium": 112.0,        # 70 plus the 60% target
            "actual_dte": 0,
            "hard_exit_time": "15:00",
            "profit_lock_activated": 0,
            "profit_lock_stop_level": None,
            "raw_params_json": TREND,
        }
        out.update(over)
        return out

    # ── the 0DTE flat: expiry-day tickets are flat on the clock ─────────
    a, prio, ctx = mon(_FakeSelf(), pos(), legs, chain, dtime(14, 35),
                       23450.0, -58.0, -58.0)
    assert a == "CLOSE_TARGET", (a, prio, ctx)
    assert ctx.get("reason_detail") == "trend_route_dte0_time_flat", ctx

    # ...but a v5 momentum ticket keeps the ladder it was measured on
    a, prio, ctx = mon(_FakeSelf(), pos(raw_params_json=V5), legs, chain,
                       dtime(14, 35), 23450.0, -58.0, -58.0)
    assert a == "HOLD", (a, prio, ctx)

    # ...the trigger is expiry day, not merely a late hour
    a, prio, ctx = mon(_FakeSelf(),
                       pos(actual_dte=3,
                           raw_params_json=json.dumps(
                               {"actual_dte": 3, "trend_route": True})),
                       legs, chain, dtime(14, 35), 23450.0, -58.0, -58.0)
    assert a == "HOLD", (a, prio, ctx)

    # ...and it never fires with time left on the clock
    for hhmm in (dtime(10, 0), dtime(13, 0), dtime(14, 25)):
        a, prio, ctx = mon(_FakeSelf(), pos(), legs, chain, hhmm,
                           23450.0, -58.0, -58.0)
        assert a == "HOLD", (hhmm, a, prio, ctx)

    # ── the pre-existing rungs keep their positions in front of it ──────
    # a loss limit outranks a clock
    a, prio, ctx = mon(_FakeSelf(), pos(), legs, chain, dtime(14, 35),
                       23450.0, -44.0, -44.0)
    assert a == "CLOSE_STOP" and prio == xe.EXIT_PRIORITY_PRICE_STOP, (a, prio, ctx)

    # ── the ratchet raises the stop and persists it ─────────────────────
    s = _FakeSelf()
    a, prio, ctx = mon(s, pos(), legs, chain, dtime(11, 0),
                       23450.0, -120.0, -120.0)
    assert a == "TIGHTEN_STOP" and prio == xe.EXIT_PRIORITY_PROFIT_LOCK, (
        a, prio, ctx,
    )
    assert s.db.updates, "the ratchet persisted nothing"
    _row = s.db.updates[-1][1]
    assert _row["profit_lock_activated"] == 1, _row
    assert _row["stop_premium"] > 70.0, _row          # above the entry value
    assert _row["profit_lock_stop_level"] == ctx["new_level"], (_row, ctx)
    # and mid 120 gives entry + half of the 50-point gain back
    assert abs(float(ctx["new_level"]) - 95.0) < 1e-6, ctx

    # the stored level is what closes the position on a later cycle
    a, prio, ctx = mon(
        _FakeSelf(),
        pos(stop_premium=float(_row["stop_premium"]),
            profit_lock_activated=1,
            profit_lock_stop_level=_row["profit_lock_stop_level"]),
        legs, chain, dtime(11, 30), 23450.0,
        -float(_row["stop_premium"]), -float(_row["stop_premium"]),
    )
    assert a == "CLOSE_STOP", (a, prio, ctx)

    # ── and the marks are asymmetric in the right direction ─────────────
    # the ratchet reads the MID (so one wide print cannot lock a fake gain)
    # while the stop reads the liquidation value (so it is exitable).
    # mid 85 is under the 87.5 lock trigger, so nothing arms -- even though
    # the liquidation mark of 100 is well over it. A ratchet that read the
    # liquidation value would have armed the lock on a print it could not
    # have sold into.
    a, prio, ctx = mon(_FakeSelf(), pos(), legs, chain, dtime(11, 0),
                       23450.0, -85.0, -100.0)
    assert a == "HOLD", (a, prio, ctx)
    # ...and one point higher on the mid, it arms
    a, prio, ctx = mon(_FakeSelf(), pos(), legs, chain, dtime(11, 0),
                       23450.0, -88.0, -100.0)
    assert a == "TIGHTEN_STOP", (a, prio, ctx)


def _unit_trend_route() -> None:
    """Drive the trend route by hand, on a synthetic chain.

    _trend_route_gate and trend_route_size_plan are pure over the signals
    dict, so they run without a broker, a socket or a clock. The 09:50 print
    of 2026-09-15 is the positive case; every property of it is then broken
    in turn and the gate must refuse each time. A substitute route that keeps
    saying yes when its premise is removed is not a route, it is a licence.

    compute_trend_params() does need a live chain, so a small deterministic
    one is built here (21 strikes, a linear delta ramp, a strictly monotone
    premium ramp, tradable bid/ask and real open interest). That exercises
    _momentum_pick_strike, _validate_leg, _get_exec_price, the friction
    model and the whole sizing ladder, not just the new plumbing.
    """
    import logging
    import shutil
    import tempfile

    import core
    from calibration_engine import CalibrationEngine
    from strategy_engine import StrategyEngine

    cfg = core.load_config()
    tmp = Path(tempfile.mkdtemp(prefix="patch_v12_ut_"))
    db  = core.Database(tmp / "ut.db")
    log = logging.getLogger("patch_v12_ut")
    log.handlers = [logging.NullHandler()]
    log.propagate = False

    exp = date(2026, 9, 15)          # the Tuesday weekly

    def build_chain(spot: float) -> dict:
        chain = {}
        for k in range(int(spot) - 800, int(spot) + 500, 50):
            m = (spot - k) / 300.0                 # >0 => puts are ITM
            for ot, sgn in (("put", 1.0), ("call", -1.0)):
                d = sgn * max(0.05, min(0.95, 0.5 + sgn * 0.25 * m))
                prem = max(2.0, 95.0 - sgn * 55.0 * m)
                chain.setdefault(k, {})[ot] = {
                    "bid": prem - 0.35, "ask": prem + 0.35, "ltp": prem,
                    "delta": d, "gamma": 0.0004, "vega": 6.0, "theta": -12.0,
                    "iv": 0.28, "oi": 5000,
                    "instrument_key": f"NIFTY{k}{ot}",
                }
        return chain

    class _FakeME:
        def __init__(self, state, chain):
            self.state = state
            self.last_chain = chain
            self.last_chain_expiry = exp

        def finalize_cycle_log(self, *a, **k):
            return None

    state = {
        "daily_halted":      False,
        "consecutive_stops": 0,
        "entry_start":       "10:30",   # the Tuesday 0DTE sell-side window
        "entry_end":         "13:00",
        "hard_exit_time":    "15:00",
        "momentum_entries":  0,
        "trend_route_entries": 0,
    }
    me = _FakeME(state, build_chain(23416.7))
    se = StrategyEngine(cfg, db, me, CalibrationEngine(db, cfg, log), log)

    def sig(**over):
        base = {
            "spot": 23416.7,                # the 09:50 print of 2026-09-15
            "active_expiry": exp.isoformat(),
            "or_computed": True,
            "or_high": 23592.85,
            "or_low": 23457.05,
            "or_width": 135.8,
            "vwap": 23462.0,
            "price_regime": "DOWNTREND",
            "orb_price_regime": "DOWNTREND",
            "adx_15": 0.0,
            "adx_15_mature": False,
            "actual_dte": 0,
            "confidence_level": "MEDIUM",
            "confidence_score": 0.62,
            "size_multiplier": 0.106,
            "iv_behavior": "EXPANDING",
            "iv_change_pct_from_open": 8.0,
            "day_move_used_pct": 100.0,
            "straddle_expanding": False,
            "block_new_entries": False,
            "spot_velocity_block": False,
            "chain_stale": False,
        }
        base.update(over)
        return base

    REFUSAL = "iv_expanding_never_sell_into_rising_iv"
    T950 = dtime(9, 50)

    def gate(s, reason=REFUSAL, when=T950):
        return se._trend_route_gate(s, reason, when)

    # ── positive: the 2026-09-15 09:50 tape, sell side refused for IV ────
    ok, why, direction = gate(sig())
    assert ok, f"positive case refused: {why}"
    assert direction == -1, f"expected puts, got direction={direction}"

    # ── substitution is limited to sell-side refusals ────────────────────
    for account_reason in (
        "daily_loss_halt_reached", "NO_TRADE:ABORT", "circuit_breaker",
        "vix_emergency", "params_invalid:credit_risk_too_high",
        "open_position_limit", "cooldown_after_stop",
    ):
        ok, why, _ = gate(sig(), account_reason)
        assert not ok, (
            f"account/data refusal '{account_reason}' was substituted away"
        )
        assert "sell_side_not_refused" in why, why

    # A straddle explosion IS a sell-side veto and the route may answer it -
    # but the regime snapshot that produces it carries confidence NONE, and
    # the route's own confidence rule refuses. The gate lets it through, the
    # tape refuses it: the right division of labour.
    ok, why, _ = gate(sig(), "NO_TRADE:STRADDLE_EXPLOSION")
    assert ok, f"straddle explosion should be an answerable family: {why}"
    ok, why, _ = gate(sig(confidence_level="NONE", size_multiplier=0.0),
                      "NO_TRADE:STRADDLE_EXPLOSION")
    assert not ok and "confidence" in why, why

    # ── and to a tape that is actually trending the right way ────────────
    for broken, label in (
        ({"price_regime": "RANGE"}, "range tape"),
        ({"price_regime": "CHOPPY"}, "choppy tape"),
        ({"orb_price_regime": "UPTREND"}, "OR structure disagreement"),
        ({"confidence_level": "LOW"}, "LOW confidence"),
        ({"confidence_level": "NONE"}, "NONE confidence"),
        ({"vwap": 23400.0}, "spot above a falling VWAP"),
        ({"or_computed": False}, "no opening range"),
        ({"or_width": 0.0}, "zero-width opening range"),
        ({"spot": 23470.0}, "back inside the opening range"),
        ({"spot": 23212.0}, "0.80x OR below the low - the move is spent"),
        ({"adx_15_mature": True, "adx_15": 12.0}, "mature ADX of 12"),
        ({"iv_behavior": "SPIKING"}, "IV spiking"),
        ({"iv_change_pct_from_open": 45.0}, "IV up 45% since the open"),
        ({"block_new_entries": True}, "regime hard block"),
        ({"spot_velocity_block": True}, "spot velocity circuit"),
        ({"chain_stale": True}, "stale chain"),
        ({"actual_dte": 5}, "DTE 5"),
        ({"actual_dte": None}, "unresolved expiry"),
        ({"day_move_used_pct": 480.0}, "day beyond 4x its straddle"),
    ):
        ok, why, _ = gate(sig(**broken))
        assert not ok, f"gate accepted a {label} tape"

    ok, why, _ = gate(sig())
    assert ok, f"positive case refused after the negative loop: {why}"

    # ── its own clock, never wider than the session's ────────────────────
    ok, why, _ = gate(sig(), REFUSAL, dtime(9, 40))
    assert not ok and why.startswith("trend_route_before_"), why
    ok, why, _ = gate(sig(), REFUSAL, dtime(12, 59))
    assert ok, f"12:59 should still be inside the window: {why}"
    ok, why, _ = gate(sig(), REFUSAL, dtime(13, 1))
    assert not ok and why.startswith("trend_route_past_"), why
    state["entry_end"] = "11:30"        # the session's rule must still bind
    ok, why, _ = gate(sig(), REFUSAL, dtime(12, 59))
    assert not ok, "the trend route ignored the session entry_end"
    state["entry_end"] = "13:00"
    # not enough session left
    ok, why, _ = gate(sig(), REFUSAL, dtime(13, 0))
    assert ok, why
    state["entry_end"] = "14:00"
    state["hard_exit_time"] = "14:30"
    ok, why, _ = gate(sig(), REFUSAL, dtime(13, 10))   # 80 min left
    assert ok, f"80 minutes before the bell is enough for a trend ticket: {why}"
    ok, why, _ = gate(sig(), REFUSAL, dtime(13, 20))   # 70 min left
    assert not ok and "min_before_hard_exit" in why, why
    state["hard_exit_time"] = "15:00"
    state["entry_end"] = "13:00"

    # ── the trend must be young, in opening-range units ─────────────────
    ok, _, _ = gate(sig(spot=23445.0))          # 12 pts  = 0.09x OR
    assert not ok, "0.09x OR break is not a break"
    ok, _, _ = gate(sig(spot=23436.0))          # 21 pts  = 0.155x OR
    assert ok, "0.155x OR break should qualify"
    ok, _, _ = gate(sig(spot=23376.5))          # 80.6 pts = 0.59x OR
    assert ok, "0.59x OR is inside the band and must qualify"
    ok, _, _ = gate(sig(spot=23375.0))          # 82.1 pts = 0.60x OR
    assert not ok, "0.60x OR is the edge of the band - refuse on touch"
    ok, _, _ = gate(sig(spot=23370.0))          # 87 pts  = 0.64x OR
    assert not ok, "0.64x OR is extension, not entry"

    # ── sizing: down, never up, and for a stated reason ─────────────────
    plan = se.trend_route_size_plan(sig(), -1)
    assert 0 < plan["size_mult"] <= cfg.trend_route_size_floor, plan
    assert plan["multipliers"]["regime_schedule_raw"] < 0.2, plan
    fresh = se.trend_route_size_plan(sig(spot=23436.0), -1)["size_mult"]
    spent = se.trend_route_size_plan(sig(spot=23376.5), -1)["size_mult"]
    assert fresh > spent > 0, (fresh, spent)
    dte1 = se.trend_route_size_plan(sig(actual_dte=1), -1)["size_mult"]
    assert dte1 > plan["size_mult"], (dte1, plan["size_mult"])
    stab = se.trend_route_size_plan(sig(straddle_expanding=True), -1)
    assert stab["size_mult"] < plan["size_mult"], stab

    # ── the override plumbing must be WIRED, not merely consistent ──────
    # momentum_stop_frac (0.35) and momentum_target_frac (0.60) ship with the
    # SAME defaults as trend_route_stop_frac / trend_route_target_frac, so
    # asserting "the ticket carries 0.35 and 0.60" passes whether the override
    # is wired or dead. Config is frozen, so build a second config whose trend
    # knobs hold values that exist nowhere else, and assert they arrive.
    import dataclasses

    cfg2 = dataclasses.replace(
        cfg,
        trend_route_stop_frac=0.20,
        trend_route_target_frac=1.25,
        trend_route_lock_trigger=0.40,
    )
    se2 = StrategyEngine(cfg2, db, me, CalibrationEngine(db, cfg2, log), log)

    _probe = se2.compute_trend_params(-1, "probe", sig(), plan["size_mult"])
    assert _probe.get("valid"), _probe.get("reason")
    assert abs(float(_probe["stop_frac"]) - 0.20) < 1e-9, (
        "trend_route_stop_frac did not reach the ticket", _probe["stop_frac"],
    )
    assert abs(float(_probe["target_frac"]) - 1.25) < 1e-9, (
        "trend_route_target_frac did not reach the ticket", _probe["target_frac"],
    )
    assert abs(float(_probe["profit_lock_trigger"])
               - abs(float(_probe["entry_credit"])) * 1.40) < 1e-2, (
        "trend_route_lock_trigger did not reach the ticket",
        _probe["profit_lock_trigger"],
    )
    # and the v5 momentum route must NOT inherit them
    _v5probe = se2.compute_momentum_params(-1, "probe_v5", sig(actual_dte=1), 1.0)
    assert _v5probe.get("valid"), _v5probe.get("reason")
    assert abs(float(_v5probe["stop_frac"]) - cfg2.momentum_stop_frac) < 1e-9, (
        "the trend override leaked into the v5 momentum route",
        _v5probe["stop_frac"],
    )
    assert abs(float(_v5probe["target_frac"])
               - cfg2.momentum_target_frac) < 1e-9, (
        "the trend override leaked into the v5 momentum route",
        _v5probe["target_frac"],
    )

    # ── the ticket the exit ladder reads ────────────────────────────────
    p = se.compute_trend_params(-1, "unit", sig(), plan["size_mult"])
    assert p.get("valid"), p.get("reason")
    assert p.get("trend_route") is True
    assert p.get("strategy_type") == "BUY", p.get("strategy_type")
    assert p.get("strategy_name") == "LONG_PUT", p.get("strategy_name")
    assert float(p.get("entry_credit")) < 0, p.get("entry_credit")
    assert abs(float(p["stop_frac"]) - cfg.trend_route_stop_frac) < 1e-9, (
        "stop_frac not applied", p["stop_frac"], cfg.trend_route_stop_frac
    )
    assert abs(float(p["target_frac"]) - cfg.trend_route_target_frac) < 1e-9, (
        "target_frac not applied", p["target_frac"], cfg.trend_route_target_frac
    )
    assert abs(float(p["profit_lock_trigger"])
               - abs(float(p["entry_credit"]))
               * (1.0 + cfg.trend_route_lock_trigger)) < 1e-2, (
        "lock trigger not applied", p["profit_lock_trigger"],
        p["entry_credit"], cfg.trend_route_lock_trigger,
    )
    assert int(p["final_lots"]) >= 1, p["final_lots"]

    # ── and the v5 pipeline still defaults to the v5 numbers ────────────
    s2 = sig(actual_dte=1)
    p_v5 = se.compute_momentum_params(-1, "unit_v5", s2, 1.0)
    assert p_v5.get("valid"), p_v5.get("reason")
    assert p_v5.get("trend_route") is False
    assert abs(float(p_v5["stop_frac"]) - cfg.momentum_stop_frac) < 1e-9, (
        "v5 stop_frac changed", p_v5["stop_frac"], cfg.momentum_stop_frac
    )
    assert abs(float(p_v5["target_frac"]) - cfg.momentum_target_frac) < 1e-9, (
        "v5 target_frac changed", p_v5["target_frac"], cfg.momentum_target_frac
    )
    assert abs(float(p_v5["profit_lock_trigger"])
               - abs(float(p_v5["entry_credit"]))
               * (1.0 + cfg.momentum_lock_trigger)) < 1e-2, (
        "v5 lock trigger changed", p_v5["profit_lock_trigger"]
    )
    # the hooks are additive: v5 sized at 1.0 must still buy more than the
    # trend route sized at 0.28, which is the point of the size floor
    assert int(p_v5["final_lots"]) > int(p["final_lots"]), (
        p_v5["final_lots"], p["final_lots"]
    )

    try:
        db._conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)



def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Apply the v12 directional trend route."
    )
    ap.add_argument("--check", action="store_true",
                    help="verify only; write nothing")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the edits, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="apply even if the baseline md5 differs")
    ap.add_argument("--strict-baseline", action="store_true",
                    help="refuse to apply if the baseline md5 differs")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the post-apply verification")
    ap.add_argument("--quick", action="store_true",
                    help="verify without the per-module self-tests")
    args = ap.parse_args(argv)

    print("=" * 78)
    print(f"NIFTY INTRADAY OPTIONS ENGINE — PATCH {PATCH_TAG} "
          f"(directional trend route)")
    print("=" * 78)
    print(f"repo: {BASE}")

    # ── baseline ─────────────────────────────────────────────────────────
    drifted = []
    print()
    print("Baseline check (post-v11):")
    for f, want in sorted(BASELINE_MD5.items()):
        p = BASE / f
        if not p.exists():
            drifted.append((f, want, "MISSING"))
            print(f"  [MISSING] {f}")
            continue
        got = md5(p)
        if got == want:
            print(f"  [ok]      {f}  {got}")
        elif MARKERS.get(f, MARK) in p.read_text(encoding="utf-8"):
            print(f"  [patched] {f}  {got} (patch {PATCH_TAG} already present)")
        else:
            drifted.append((f, want, got))
            print(f"  [drift]   {f}  {got} (patch authored against {want})")
    if drifted:
        print()
        print("  NOTE: the files above are not the exact post-v11 bytes this")
        print("  patch was authored against. The patch is anchor-based and")
        print("  refuses to guess, so it will still fail closed on any anchor")
        print("  it cannot match uniquely.")
        if args.strict_baseline and not args.check:
            print()
            print("  --strict-baseline: refusing to apply.")
            return 4

    if args.check:
        return verify(deep=not args.quick)

    # ── apply ────────────────────────────────────────────────────────────
    print()
    print("Edits:")
    rc = plan_edits(dry_run=args.dry_run)
    if rc == 2:
        return 2
    if rc == 3:
        return 3
    if args.dry_run:
        return 0
    if rc == 0:
        print("  (already applied)")

    if args.no_verify:
        print()
        print("Applied. Verification skipped (--no-verify).")
        return 0

    vrc = verify(deep=not args.quick)

    # ── the smoke test that matters ──────────────────────────────────────
    print()
    print("─" * 78)
    print("REPLAY SELF-CHECK (2026-09-15, the incident session)")
    print("─" * 78)
    db = BASE / "data" / "per_day" / "nifty_algo_2026-09-15.db"
    if not db.exists():
        print(f"  skipped: {db} not present")
    elif vrc != 0:
        print("  skipped: verification failed")
    else:
        import csv as _csv
        out = Path(tempfile.mkdtemp(prefix="patch_v12_bt_"))
        csv_path = out / "2026-09-15.csv"
        try:
            proc = subprocess.run(
                [PYTHON, str(BASE / "backtest_engine.py"),
                 "--db", str(db), "--from", "2026-09-15",
                 "--to", "2026-09-15", "--trade-report", "off",
                 "--csv", str(csv_path)],
                cwd=str(BASE), capture_output=True, text=True,
            )
            tail = (proc.stdout or "").strip().splitlines()
            for line in tail[-18:]:
                print(f"  {line}")
            if proc.returncode != 0:
                print(f"  replay returned {proc.returncode}")
                print((proc.stderr or "")[-2000:])
            elif csv_path.exists():
                rows = list(_csv.DictReader(csv_path.open()))
                print()
                if rows:
                    print(f"  {len(rows)} trade(s) taken on 2026-09-15:")
                    for r in rows:
                        print("   ", {k: r[k] for k in list(r)[:8]})
                else:
                    print("  STILL ZERO TRADES on 2026-09-15 — the route did "
                          "not fire. Query strategy_decisions for "
                          "signals_json -> trend_route_verdict to see why.")
        finally:
            shutil.rmtree(out, ignore_errors=True)

    print()
    print("=" * 78)
    if vrc != 0:
        print(f"PATCH {PATCH_TAG}: APPLIED, VERIFICATION FAILED — restore the")
        print("*.v11.bak backups before trading.")
        return 1
    print(f"PATCH {PATCH_TAG}: APPLIED AND VERIFIED")
    print("Backups: core.py.v11.bak, strategy_engine.py.v11.bak,")
    print("         execution_engine.py.v11.bak, backtest_engine.py.v11.bak")
    print("Rollback: python patch_v12.py --rollback   (or copy the .bak back)")
    print("=" * 78)
    return 0


def rollback() -> int:
    n = 0
    for f in sorted({e[0] for e in EDITS}):
        bak = BASE / f"{f}.v11.bak"
        if bak.exists():
            (BASE / f).write_text(bak.read_text(encoding="utf-8"),
                                  encoding="utf-8")
            print(f"  restored {f}")
            n += 1
    if not n:
        print("  no .v11.bak backups found")
        return 1
    print(f"  {n} file(s) rolled back")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--rollback":
        sys.exit(rollback())
    sys.exit(main(argv))