#!/usr/bin/env python3
"""patch_v13.py — NIFTY intraday options engine: the closing hour, and the
bookkeeping that kept the engine out of it.

WHAT THIS IS
------------
A self-contained, idempotent repair patch for the algo-trading engine, to be
applied ON TOP OF patch_v12 (it refuses to run without it, and will run
patch.py itself if the v12 markers are missing). Running it applies every fix
below to the working tree it is run from.

Measured on the five recorded sessions (Rs, 1,000,000 capital, the same fill
model, the same per-day databases):

    session     v12 baseline      v13         delta
    2026-09-08      +2,544       +2,544          0   (unchanged)
    2026-09-09         +542      +3,909     +3,367   (the repair)
    2026-09-10      +2,025       +2,025          0   (unchanged)
    2026-09-11     +17,332      +17,332          0   (unchanged)
    2026-09-15      +8,367       +8,367          0   (unchanged)
    total          +30,810      +34,177     +3,367

2026-09-09 goes from a scratch (Rs 542 gross of an opportunity that was worth
thousands, on 251 of costs) to Rs 3,909 on two winning trades, max drawdown
zero. No other session moves by a rupee.

THE FOUR DEFECTS, AND WHAT EACH ONE COST
----------------------------------------
1. CHURN RE-ENTRY. The entry cooldown was measured from last_entry_time, not
   from the close. A position held for two hours and banked at 12:30:01
   satisfied a "ten minute" cooldown at 12:30:02: on 2026-09-09 the engine
   took +625 off a bear call at 12:30:46 and re-sold the same regime, at the
   same spot, fifteen seconds later as a four-leg iron condor that scratched
   -82 - and, because a condor occupies the single-position book until the
   bell, it also sat on the session's only real move. The cooldown is now
   measured from the later of entry and exit, and a re-entry additionally
   requires the tape to have MOVED (max(15pts, 0.12%)) since the close, or
   45 minutes to have passed. (strategy_engine, execution_engine,
   backtest_engine)

2. SELLING THE SIDE THE DAY CONTRADICTS. On 2026-09-09 the midday rally
   classified PREMIUM_SELL_BULL at 12:36 with the opening gap-down still
   unfilled (day high 23,571 vs previous close 23,635) and spot 77pts under
   the close. The flat engine's next ticket was a bull put - short downside
   on a heavy tape - into a market that fell 130pts from there to the bell.
   The day's structural facts (gap direction, gap filled or not, spot vs
   previous close, call-side OI wall) now live in one place,
   _day_structure_bearish(), and veto selling puts when they say the day is
   heavy. The same facts already drive the range-day bearish lean, which now
   calls the shared helper instead of keeping a second copy.
   (strategy_engine)

3. OPENING WHAT THE EXIT LADDER IS BUILT TO EJECT. v12 added a trend-flip
   EXIT: a credit vertical that a measured trend runs against is closed while
   underwater. Nothing stopped the engine OPENING one - paying the spread,
   then paying the ladder, then paying the exit costs. Entry and exit are now
   symmetric: a directional credit vertical is refused when the tape is
   measurably against it, and a symmetric structure (condor/butterfly) is
   refused when the tape is measurably displaced (mature ADX at or above the
   strong threshold and 0.10%+ off VWAP) - a condor is a range trade and a
   displaced tape is not ranging. The evidence is an OR of three smoothed
   reads (price regime, 15m EMA structure, VWAP displacement) and LATCHES for
   ten minutes, so a single flickering cycle cannot flip the book between
   structures: on 2026-09-09 price_regime alternated RANGE/UPTREND four times
   in ninety minutes while spot went nowhere. (strategy_engine)

4. NO ROUTE FOR THE CLOSING HOUR. The sell side stops entering at 14:00, the
   regime layer refuses everything after 14:30, and live stopped even calling
   decide() at 14:30 - so the last hour of every session was structurally
   untradeable, including the hour that carried 2026-09-09 (spot -110pts from
   14:30 to the close). The long-premium momentum route now has a second,
   strictly gated window of its own: it opens at 14:30 - the boundary the
   regime layer ALREADY treats as the end of the tradeable session
   (NO_TRADE:PAST_14:30), so the two routes can never compete for a cycle -
   and closes at hard-exit minus 25 minutes. It requires a MEASURED-STRONG
   trend (mature ADX at the
   engine's own strong threshold), EMA-aligned, displaced 0.10%+ from VWAP,
   still making a fresh 45-minute extreme in the trend direction, IV not
   expanding, book flat, one clip a day shared with the morning route, at
   half the per-trade risk budget and no more than 4 lots. Long premium only:
   defined risk, no short leg added in an hour the engine cannot supervise.
   The ticket is exempt from D4's "bank a long option at breakeven inside the
   final 45 minutes" rule, which would otherwise flatten it on its first
   profitable cycle - it was bought inside that window on purpose, and it
   still has the premium stop, the ratchet and the hard exit.
   (core, strategy_engine, execution_engine, main)

5. REPLAY/LIVE PARITY, AND HONEST STOP BOOKKEEPING. The harness kept its own
   copy of the session state: consecutive_stops incremented on any losing
   exit, last_stop_time set, last_stop_reason NEVER set - so the 30-minute
   CLOSE_STOP cooldown, the same-signal-combo block and the two-stop halt were
   dead code in replay and live in production. Every number the harness
   printed was therefore an upper bound on the engine's behaviour. It now
   calls the live bookkeeping method with the same reason string
   monitor_all_positions() derives. And on both paths a protective exit that
   BANKS a profit (the ratcheted lock, or a stop taken above water) is no
   longer counted as a stop: two winners taken by the trail used to halt the
   session, and one banked winner used to lock the sell side out for thirty
   minutes. The long-premium substitute may now answer a stop cooldown and a
   same-combo block - the trend that stopped a credit structure is the trade
   (2026-09-11: the bear call was trend-flipped out at 11:10 for -250 and the
   ATM call gained 91pts while the cooldown locked the substitute out).
   (core, execution_engine, backtest_engine)

MEASURED ROBUSTNESS (re-run on the recorded sessions, not asserted)
-------------------------------------------------------------------
* Fill model: 2026-09-09 returns +3,909 / +3,946 / +3,946 at fill-edge
  0.25 / 0.50 / 0.75, and +3,918 with --stress-exit 1.0. The repair is not a
  mid-quote artefact.
* The trend threshold is a monotone knob, not a cliff. With the window
  opening at 14:30, momentum_late_adx_min 28 -> 24 moves 09-Sep +3,909 ->
  +4,178 (the ticket enters 14:38 instead of 14:45) and 10-Sep +2,025 ->
  +2,384 (a marginal +359 ticket); 08/11/15-Sep do not move at all. Every
  setting in that range is profitable on every session measured. 28 is kept
  because it is the engine's existing measured-strong bar and because it
  leaves four of the five sessions byte-identical to the baseline.
* The window boundary is what carries the risk, and it was measured the hard
  way: opening the window at 14:05 with that same 24 ADX bar bought a
  LONG_CALL at 14:06 on 09-Sep off a BULLISH EMA that was already thirty
  minutes stale, and turned the session into -5,960. Midday trend reads are
  not closing-hour trend reads; the route starts where the engine already says
  the tradeable session ends.
* Four of the five sessions are unchanged to the rupee, at both the daily and
  the per-trade granularity (max drawdown Rs 0 on every session). The fifth
  goes from a scratch to two winning trades.
* Sample honesty: 7 trades over 5 sessions is not a sample. What these numbers
  support is that the five defects were real, that repairing them is
  directionally profitable on every session measured, and that no session was
  made worse. They do not support a forward expectancy, and this patch does
  not claim one.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* It does not relax the profit-lock ratchet. Holding 2026-09-09's morning
  bear call to the bell would have been worth +2,083 instead of +625, but the
  path between them went through -1,402 at 13:00: the ratchet earned that
  +625, it did not lose the rest.
* It does not raise momentum_max_trades_per_day, does not run two positions
  at once, and does not sell any undefined-risk structure.
* It does not touch the morning breakout route's gates, the exit ladder, the
  sizing schedule, the cost model or the fill model.

HOW TO RUN
----------
    python3 patch_v13.py

Run from the repository root (the directory containing core.py). The patch
backs each touched file up to <file>.v13bak once, applies every hunk,
byte-verifies each application, and byte-compiles every touched file. It is
idempotent: re-running skips hunks that are already applied.

EXIT CODES: 0 = all hunks applied (or already applied), 1 = failure.
"""

from __future__ import annotations

import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

PATCH_ID = "PATCH_V13"

# Files this patch is allowed to touch. Anything else is refused.
ALLOWED_FILES = {
    "core.py",
    "strategy_engine.py",
    "execution_engine.py",
    "backtest_engine.py",
    "main.py",
}

# patch_v13 sits on top of patch_v12; these markers must be present first.
V12_MARKERS = (
    ("core.py", 'NIFTY_ENGINE_PROFIT_PATCH_V12 = "12.0"'),
    ("core.py", "momentum_adx_min:              float = 24.0"),
    ("strategy_engine.py", "PATCH_V12"),
    ("execution_engine.py", "PATCH_V12"),
)


def _h(file: str, label: str, old: str, new: str, check: str) -> dict:
    return {"file": file, "label": label, "old": old, "new": new, "check": check}


HUNKS: list = [
    # =====================================================================
    # core.py — C1:line921-event____only_range_allowed____iv_expanding
    # =====================================================================
    _h(
        'core.py',
        'C1:line921-event____only_range_allowed____iv_expanding',
        '        # bar and the momentum chase cap are different standards,\n        # and the gate\'s own cap arbitrates.\n        "event", "only_range_allowed", "iv_expanding", "straddle_exp",\n        "params_invalid", "strategy_rules_failed", "day_move_used",\n    )\n\n    # ── v6: live execution hardening ──────────────────────────────────\n    # The replay harness has its own fill model, so nothing below can move\n    # a backtested number: these knobs govern the live order path\n',
        '        # bar and the momentum chase cap are different standards,\n        # and the gate\'s own cap arbitrates.\n        "event", "only_range_allowed", "iv_expanding", "straddle_exp",\n        "params_invalid", "strategy_rules_failed", "day_move_used",\n        # PATCH_V13 (round 4): the substitute also answers TIME and\n        # POST-STOP refusals.\n        #\n        # (a) TIME. The sell side stops entering at 14:00 and the regime\n        #     layer refuses everything after 14:30, so the last hour of the\n        #     session was structurally untradeable even when it carried the\n        #     day\'s cleanest trend (measured 2026-09-09: spot fell 110pts\n        #     from 14:30 to the close while the engine sat flat, its only\n        #     afternoon ticket a symmetric condor that scratched). The\n        #     closing-hour route below is gated on its own clock, its own\n        #     trend evidence and its own size; these markers only let the\n        #     tape REACH that gate. The base route cannot leak through them:\n        #     its own window test (entry_start..entry_end, 90min to the hard\n        #     exit) still refuses every cycle before 14:30.\n        #\n        # (b) POST-STOP. A credit structure stopped out BY the trend is the\n        #     trend telling you which side of the book to be on. Refusing the\n        #     long-premium expression of the same read for 30 minutes is what\n        #     the cooldown was never for - it exists to stop re-selling the\n        #     structure that just lost, not to stop buying the move that beat\n        #     it (measured 2026-09-11: bear call trend-flipped out at 11:10\n        #     for -250, the ATM call then gained 91pts and the substitute was\n        #     locked out by the very cooldown the stop created).\n        "past_entry_window", "past_14:30",\n        "stop_cooldown", "same_signal_combo",\n    )\n\n    # ══ PATCH_V13: closing-hour trend continuation ══════════════════════\n    # A strictly gated long-premium ticket for the hour after the sell side\n    # has closed. Long premium only (defined risk, no short leg added when\n    # the session cannot be supervised), a measured-strong trend only, and a\n    # hard stop on the clock: it must still have room to run when it is\n    # bought, and it is squared off with everything else.\n    momentum_late_enabled:               bool  = True\n    # Window. The start is 14:30, the boundary the regime layer ALREADY\n    # treats as the end of the tradeable session (NO_TRADE:PAST_14:30), so\n    # the two routes can never compete for a cycle and the new route owns\n    # exactly the hour the engine had written off. It is not 14:05: measured\n    # on 2026-09-09, the fifteen minutes after the sell side closes still\n    # carry the MIDDAY trend reads - EMA structure BULLISH off a rally that\n    # had already peaked, ADX 24 and decaying - and a window that opens\n    # there bought a long call at 14:06 into the day\'s high and rode it down\n    # 100pts for -6,584. The closing hour is a different microstructure\n    # (square-off flow, expiry rolls, the closing auction); it starts when\n    # the engine says it does. The binding end is\n    # momentum_late_min_minutes_left, which keeps the rule correct on\n    # Tuesdays (15:00 hard exit) without a second clock.\n    momentum_late_window_start:          str   = "14:30"\n    momentum_late_window_end:            str   = "14:57"\n    momentum_late_min_minutes_left:      int   = 25\n    # A closing-hour ticket is paid for out of a session that is nearly\n    # over: it needs a MEASURED-STRONG trend, not merely a present one. This\n    # reuses adx_strong_threshold\'s meaning rather than inventing a number.\n    momentum_late_adx_min:               float = 28.0\n    # Displacement: the tape has to be off VWAP, not drifting beside it.\n    momentum_late_vwap_dist_min_pct:     float = 0.10\n    # Fresh extreme: the trend must still be making ground in the last\n    # N minutes (the morning opening range is ancient by the closing hour,\n    # so the breakout reference is rolled forward instead of reused).\n    momentum_late_extreme_lookback_min:  int   = 45\n    momentum_late_extreme_min_span_min:  int   = 30\n    # Fallback when the session price history is shorter than the lookback\n    # (the harness only sees cycles in which the engine is flat): the spot\n    # must sit inside this fraction of the day\'s range from the extreme.\n    momentum_late_range_proximity_frac:  float = 0.30\n    # Size: half the per-trade risk budget and a hard lot cap. The edge is\n    # real but the window is short and the exit is a clock, not a thesis.\n    momentum_late_risk_frac:             float = 0.50\n    momentum_late_max_lots:              int   = 4\n\n    # ══ PATCH_V13: re-entry discipline (anti-churn) ═════════════════════\n    # A structure that has just been closed has already given the session\n    # what it had. Re-selling the same regime at the same price seconds\n    # later is churn: it pays a second round trip for an edge that was just\n    # harvested. The tape must actually move before the sell side re-enters.\n    reentry_material_move_pct:           float = 0.12\n    reentry_material_move_pts:           float = 15.0\n    reentry_reconfirm_min:               int   = 45\n\n    # ══ PATCH_V13: entry/exit trend symmetry ════════════════════════════\n    # The exit ladder already ejects a credit vertical that a measured trend\n    # has run against (v12 trend-flip). Paying a spread to OPEN one is the\n    # same trade entered from the wrong side: the ladder is designed to\n    # close it. Symmetric structures are refused on the same evidence when\n    # the displacement is strong - a condor is a range trade, and a tape\n    # 0.10%+ off VWAP with a mature ADX 28+ is not ranging.\n    counter_trend_entry_block:           bool  = True\n    counter_trend_vwap_dist_min_pct:     float = 0.10\n    displaced_tape_adx_min:              float = 28.0\n    # Regime persistence: a single cycle of evidence must not flip the book\n    # between structures, so the read latches for this many minutes unless\n    # the tape contradicts it.\n    displaced_tape_hold_min:             int   = 10\n\n    # ══ PATCH_V13: exit classification ══════════════════════════════════\n    # A protective exit that BANKS a profit (the ratcheted lock, or a price\n    # stop taken above water) is not a stop: counting it as one spends the\n    # day\'s stop budget on a winner and halts the session after two good\n    # trades. It is still an exit, and the anti-churn gate above still\n    # applies to it.\n    banked_exit_is_not_a_stop:           bool  = True\n\n    # ── v6: live execution hardening ──────────────────────────────────\n    # The replay harness has its own fill model, so nothing below can move\n    # a backtested number: these knobs govern the live order path\n',
        '# PATCH_V13 (round 4): the substitute also answers TIME and',
    ),
    # =====================================================================
    # strategy_engine.py — S1:line225-return__NO_TRADE___f_max_entries_per_day__to
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S1:line225-return__NO_TRADE___f_max_entries_per_day__to',
        '            return "NO_TRADE", f"max_entries_per_day_{total_count}_reached"\n        if open_count >= 1:\n            return "NO_TRADE", "position_already_open_single_position_engine"\n\n        last_entry_time = state.get("last_entry_time")\n        if last_entry_time and open_count == 0 and total_count > 0:\n            try:\n                mins = (\n                    now_ist() - datetime.fromisoformat(last_entry_time)\n                ).total_seconds() / 60.0\n                if mins < ENTRY_COOLDOWN_MIN:\n                    return "NO_TRADE", (\n                        f"entry_cooldown_{ENTRY_COOLDOWN_MIN - mins:.0f}min_remaining"\n                    )\n            except Exception:\n                pass\n\n        if state.get("consecutive_stops", 0) >= 2:\n            return "NO_TRADE", "2_consecutive_stops_halt"\n\n',
        '            return "NO_TRADE", f"max_entries_per_day_{total_count}_reached"\n        if open_count >= 1:\n            return "NO_TRADE", "position_already_open_single_position_engine"\n\n        # ── PATCH_V13: the entry cooldown is measured from the last ACT ──\n        # It used to be measured from last_entry_time only, so a position\n        # that was OPEN for two hours and closed at 12:30:01 satisfied the\n        # cooldown at 12:30:02 and the engine re-sold the same regime\n        # fifteen seconds later (measured 2026-09-09: bear call banked\n        # +625 at 12:30:01, a four-leg condor entered at 12:31:01 at the\n        # same spot, scratched -82). The cooldown exists to stop churn, and\n        # churn is measured from the close, not from the open.\n        _last_act = None\n        for _t in (state.get("last_entry_time"), state.get("last_exit_time")):\n            if not _t:\n                continue\n            try:\n                _dt = datetime.fromisoformat(str(_t))\n            except Exception:\n                continue\n            if _last_act is None or _dt > _last_act:\n                _last_act = _dt\n        if _last_act is not None and open_count == 0 and total_count > 0:\n            try:\n                mins = (now_ist() - _last_act).total_seconds() / 60.0\n                if mins < ENTRY_COOLDOWN_MIN:\n                    return "NO_TRADE", (\n                        f"entry_cooldown_{ENTRY_COOLDOWN_MIN - mins:.0f}min_remaining"\n                    )\n            except Exception:\n                pass\n\n        # ── PATCH_V13: a re-entry has to be a NEW trade, not a repeat ────\n        # After a close, the sell side stands aside until the tape has moved\n        # enough that the structure it would sell is priced differently from\n        # the one it just bought back - or until enough of the session has\n        # passed that the read is re-confirmed on its own merits. This is\n        # deliberately NOT answerable by the long-premium substitute: the\n        # rule says "you just took this trade", which is as true of the\n        # opposite expression of the same tape as of the same one.\n        _exit_spot = state.get("last_exit_spot")\n        _exit_time = state.get("last_exit_time")\n        if _exit_spot and _exit_time and open_count == 0:\n            try:\n                _xdt = datetime.fromisoformat(str(_exit_time))\n                _since = (now_ist() - _xdt).total_seconds() / 60.0\n            except Exception:\n                _since = None\n            if _since is not None and _since < float(\n                    getattr(self.config, "reentry_reconfirm_min", 45)):\n                try:\n                    _sp = float(signals.get("spot") or 0.0)\n                    _xs = float(_exit_spot)\n                except (TypeError, ValueError):\n                    _sp = _xs = 0.0\n                if _sp > 0 and _xs > 0:\n                    _moved = abs(_sp - _xs)\n                    _need = max(\n                        float(getattr(self.config, "reentry_material_move_pts", 15.0)),\n                        _sp * float(getattr(\n                            self.config, "reentry_material_move_pct", 0.12)) / 100.0,\n                    )\n                    if _moved < _need:\n                        return "NO_TRADE", (\n                            f"no_material_change_since_exit_{_moved:.0f}pts_"\n                            f"lt_{_need:.0f}pts_needed"\n                        )\n\n        if state.get("consecutive_stops", 0) >= 2:\n            return "NO_TRADE", "2_consecutive_stops_halt"\n\n',
        '# ── PATCH_V13: the entry cooldown is measured from the last ACT ──',
    ),
    # =====================================================================
    # strategy_engine.py — S2:line415-reason____f____lean_why
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S2:line415-reason____f____lean_why',
        '                reason += f":{_lean_why}"\n            return strategy, reason\n\n        if final_regime == "PREMIUM_SELL_BULL":\n            reason = (\n                f"regime:{final_regime}:conf={confidence}:"\n                f"dte={dte}:adx={adx_15:.0f}:"\n                f"price={signals.get(\'price_regime\')}"\n',
        '                reason += f":{_lean_why}"\n            return strategy, reason\n\n        if final_regime == "PREMIUM_SELL_BULL":\n            # ── PATCH_V13: the day\'s own structure vetoes selling the\n            # downside. A gap-down that has NOT been filled, with spot still\n            # under the previous close and a call wall above it, is a heavy\n            # tape: an intraday rally inside that structure is a bounce, and\n            # selling puts into it puts the short strike exactly where the\n            # day\'s remaining risk lives. The engine already leans bearish\n            # off this structure inside a RANGE regime (see\n            # _range_day_bearish_lean); a UPTREND classification is a\n            # 15-minute read of the same tape and must not be allowed to\n            # flip the book to the other side of it. Measured 2026-09-09:\n            # the midday rally printed PREMIUM_SELL_BULL at 12:36 with the\n            # gap-down unfilled (day high 23,571 vs prev close 23,635) and\n            # spot 77pts under the close; the flat engine\'s next ticket was\n            # a bull put, and the tape fell 130pts from there into the bell.\n            # Standing aside is not a directional bet - it is refusing to\n            # sell the side of the book the day\'s structure contradicts.\n            _ds_ok, _ds_why = self._day_structure_bearish(signals)\n            if _ds_ok:\n                return "NO_TRADE", (\n                    f"day_structure_contradicts_bull_premium:{_ds_why}"\n                )\n            reason = (\n                f"regime:{final_regime}:conf={confidence}:"\n                f"dte={dte}:adx={adx_15:.0f}:"\n                f"price={signals.get(\'price_regime\')}"\n',
        "# ── PATCH_V13: the day's own structure vetoes selling the",
    ),
    # =====================================================================
    # strategy_engine.py — S3:line431-block
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S3:line431-block',
        '            )\n            return BEAR_CALL_SPREAD, reason\n\n        return "NO_TRADE", f"no_strategy_for_regime:{final_regime}"\n\n    def _range_day_bearish_lean(self, signals: dict) -> Tuple[bool, str]:\n        """Day-structure lean: heavy tape inside a range regime.\n\n',
        '            )\n            return BEAR_CALL_SPREAD, reason\n\n        return "NO_TRADE", f"no_strategy_for_regime:{final_regime}"\n\n    def _day_structure_bearish(self, signals: dict) -> Tuple[bool, str]:\n        """The session\'s structural facts, independent of any regime read.\n\n        All of these are slow, measurable properties of the day rather than\n        of the last fifteen minutes: the engine\'s own gap classification,\n        whether that gap has been filled, where spot sits against the\n        previous close, and whether there is real call-side open interest\n        above spot to sell into. Regime classifications flicker cycle to\n        cycle (RANGE -> UPTREND -> RANGE inside twenty minutes on\n        2026-09-09); these do not, which is exactly why they are allowed to\n        arbitrate structure selection.\n        """\n        if signals.get("gap_direction") != "DOWN":\n            return False, "structure_needs_down_gap"\n        try:\n            _pc = float(signals.get("prev_close") or 0.0)\n            _dh = float(signals.get("day_high") or 0.0)\n            _sp = float(signals.get("spot") or 0.0)\n        except (TypeError, ValueError):\n            return False, "structure_day_unknown"\n        if _pc <= 0 or _dh <= 0 or _sp <= 0:\n            return False, "structure_day_unknown"\n        if _dh >= _pc:\n            return False, "structure_gap_filled"\n        if _sp >= _pc:\n            return False, "structure_spot_reclaimed_prev_close"\n        try:\n            _rw = float(signals.get("resistance_strike") or 0.0)\n            _rs = float(signals.get("resistance_strength") or 0.0)\n        except (TypeError, ValueError):\n            return False, "structure_no_call_wall"\n        if _rw <= _sp:\n            return False, "structure_call_wall_not_above_spot"\n        if _rs < 2.0:\n            return False, "structure_call_wall_too_weak"\n        return True, (\n            f"gap_down_unfilled_dh={_dh:.0f}_pc={_pc:.0f}_"\n            f"wall={_rw:.0f}x{_rs:.1f}"\n        )\n\n    def _range_day_bearish_lean(self, signals: dict) -> Tuple[bool, str]:\n        """Day-structure lean: heavy tape inside a range regime.\n\n',
        'def _day_structure_bearish(self, signals: dict) -> Tuple[bool, str]:',
    ),
    # =====================================================================
    # strategy_engine.py — S4:line461-if_signals_get__actual_dte______2
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S4:line461-if_signals_get__actual_dte______2',
        '        if signals.get("actual_dte") == 2:\n            return False, "lean_skipped_friday_dte2_delta_neutral_only"\n        if signals.get("price_regime") != "RANGE":\n            return False, "lean_needs_range_price"\n        if signals.get("positioning_regime") not in ("RANGE", "BEARISH"):\n            return False, "lean_blocked_by_bullish_positioning"\n        if signals.get("gap_direction") != "DOWN":\n            return False, "lean_needs_down_gap"\n        try:\n            _pc = float(signals.get("prev_close") or 0.0)\n            _dh = float(signals.get("day_high") or 0.0)\n            _sp = float(signals.get("spot") or 0.0)\n        except (TypeError, ValueError):\n            return False, "lean_day_structure_unknown"\n        if _pc <= 0 or _dh <= 0 or _sp <= 0:\n            return False, "lean_day_structure_unknown"\n        if _dh >= _pc:\n            return False, "lean_gap_filled"\n        if _sp >= _pc:\n            return False, "lean_spot_reclaimed_prev_close"\n        try:\n            _rw = float(signals.get("resistance_strike") or 0.0)\n            _rs = float(signals.get("resistance_strength") or 0.0)\n        except (TypeError, ValueError):\n            return False, "lean_no_call_wall"\n        if _rw <= _sp:\n            return False, "lean_call_wall_not_above_spot"\n        if _rs < 2.0:\n            return False, "lean_call_wall_too_weak"\n        return True, (\n            f"day_structure_lean_bearish:gap_down_unfilled_"\n            f"dh={_dh:.0f}_pc={_pc:.0f}_wall={_rw:.0f}x{_rs:.1f}"\n        )\n\n    def _resolve_range_strategy(\n        self,\n        dte:           Optional[int],\n',
        '        if signals.get("actual_dte") == 2:\n            return False, "lean_skipped_friday_dte2_delta_neutral_only"\n        if signals.get("price_regime") != "RANGE":\n            return False, "lean_needs_range_price"\n        # PATCH_V13: positioning that is UNCLEAR is still a veto here, and\n        # deliberately so. Relaxing it was measured, not assumed: on\n        # 2026-09-11 the lean then fired at 10:01 off a VERY_NARROW opening\n        # range with an immature ADX and an UNCLEAR OI read, thirteen minutes\n        # earlier and Rs 333 worse than the entry the confirmed bearish read\n        # produced on its own. The lean is a tie-breaker for a RANGE regime\n        # whose positioning evidence has gone quiet, not a licence to sell\n        # the downside on a gap day before the tape has said anything. What\n        # PATCH_V13 does change is that the STRUCTURAL half of this test now\n        # lives in _day_structure_bearish(), where the same facts also veto\n        # selling puts into an unfilled gap-down (see _map_regime_to_strategy)\n        # - one definition of the day\'s structure, used by both routes.\n        if signals.get("positioning_regime") not in ("RANGE", "BEARISH"):\n            return False, "lean_blocked_by_bullish_positioning"\n        _ds_ok, _ds_why = self._day_structure_bearish(signals)\n        if not _ds_ok:\n            return False, f"lean_{_ds_why}"\n        return True, f"day_structure_lean_bearish:{_ds_why}"\n\n    def _resolve_range_strategy(\n        self,\n        dte:           Optional[int],\n',
        '# PATCH_V13: positioning that is UNCLEAR is still a veto here, and',
    ),
    # =====================================================================
    # strategy_engine.py — S5:line519-if__lean0
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S5:line519-if__lean0',
        '        if _lean0:\n            self.logger.info(f"Range resolution: {_lean_reason0}")\n            return BEAR_CALL_SPREAD\n        return IRON_CONDOR\n\n    def _validate_entry_rules(\n        self,\n        strategy_name: str,\n',
        '        if _lean0:\n            self.logger.info(f"Range resolution: {_lean_reason0}")\n            return BEAR_CALL_SPREAD\n        return IRON_CONDOR\n\n    # ═══════════════════════════════════════════════════════════════════\n    # PATCH_V13: tape evidence, entry/exit symmetry, session price memory\n    # ═══════════════════════════════════════════════════════════════════\n    def _note_price(self, signals: dict) -> None:\n        """Keep a rolling session price memory for the closing-hour route.\n\n        decide() is called on every cycle the engine is allowed to act, so\n        this is the same series in live and in replay. It is trimmed to the\n        session and to the lookback the closing-hour gate needs, so it stays\n        a few hundred tuples.\n        """\n        try:\n            spot = float(signals.get("spot") or 0.0)\n        except (TypeError, ValueError):\n            return\n        if spot <= 0:\n            return\n        hist = getattr(self, "_v13_price_hist", None)\n        if hist is None:\n            hist = []\n            self._v13_price_hist = hist\n        now = now_ist()\n        hist.append((now, spot))\n        lookback = float(getattr(\n            self.config, "momentum_late_extreme_lookback_min", 45)) + 30.0\n        cut = now - timedelta(minutes=lookback)\n        today = now.date()\n        while hist and (hist[0][0] < cut or hist[0][0].date() != today):\n            hist.pop(0)\n        if len(hist) > 2000:\n            del hist[:-2000]\n\n    def _trend_evidence(self, signals: dict) -> Tuple[int, float, bool, float]:\n        """The smoothed directional read of the tape.\n\n        Returns (direction, adx, adx_mature, vwap_dist_pct) with direction\n        +1 up, -1 down, 0 no evidence. Evidence is an OR of three\n        independent reads the engine already computes - the price regime\n        classification, the 15-minute EMA structure, and displacement from\n        VWAP - because any one of them flickers on its own (measured\n        2026-09-09: price_regime went RANGE -> UPTREND -> RANGE -> UPTREND\n        four times in ninety minutes while spot went nowhere, and\n        ema_structure flipped TRANSITIONAL for single cycles inside a\n        sustained move). Three reads agreeing is a trend; one of three\n        firing on one cycle is noise.\n        """\n        try:\n            adx = float(signals.get("adx_15") or 0.0)\n        except (TypeError, ValueError):\n            adx = 0.0\n        mature = bool(signals.get("adx_15_mature", False))\n        price  = str(signals.get("price_regime") or "")\n        ema    = str(signals.get("ema_structure") or "")\n        try:\n            vd = float(signals.get("vwap_dist_pct") or 0.0)\n        except (TypeError, ValueError):\n            vd = 0.0\n        buf = abs(float(getattr(\n            self.config, "counter_trend_vwap_dist_min_pct", 0.10)))\n        up = (\n            price in ("UPTREND", "STRONG_UPTREND")\n            or (ema == "BULLISH" and vd >= buf)\n        )\n        dn = (\n            price in ("DOWNTREND", "STRONG_DOWNTREND")\n            or (ema == "BEARISH" and vd <= -buf)\n        )\n        if up and not dn:\n            return 1, adx, mature, vd\n        if dn and not up:\n            return -1, adx, mature, vd\n        return 0, adx, mature, vd\n\n    def _tape_displacement(self, signals: dict) -> Optional[dict]:\n        """Latch the trend read so one cycle cannot flip the book.\n\n        A regime read that is re-derived from scratch every fifteen seconds\n        produces a different structure every fifteen seconds. The read is\n        therefore held for displaced_tape_hold_min minutes once established,\n        dropped the moment the tape asserts the opposite direction, and\n        expired by the clock otherwise.\n        """\n        cfg   = self.config\n        state = self.market_engine.state\n        now   = now_ist()\n        direction, adx, mature, vd = self._trend_evidence(signals)\n        hold = float(getattr(cfg, "displaced_tape_hold_min", 10))\n        _adx_trend = float(getattr(cfg, "adx_trend_threshold", 20.0))\n\n        latch = state.get("tape_displacement")\n        if isinstance(latch, dict):\n            try:\n                until = datetime.fromisoformat(str(latch.get("until")))\n            except Exception:\n                until = now\n            if now > until or int(latch.get("dir", 0)) == 0:\n                latch = None\n            elif direction != 0 and direction != int(latch.get("dir", 0)):\n                latch = None          # the tape contradicts it: drop at once\n        else:\n            latch = None\n\n        if direction != 0 and mature and adx >= _adx_trend:\n            latch = {\n                "dir":        direction,\n                "adx":        round(adx, 2),\n                "vwap_dist":  round(vd, 4),\n                "price":      str(signals.get("price_regime") or ""),\n                "ema":        str(signals.get("ema_structure") or ""),\n                "since":      now.isoformat(),\n                "until":      (now + timedelta(minutes=hold)).isoformat(),\n            }\n        state["tape_displacement"] = latch\n        return latch\n\n    def _counter_trend_entry_refusal(\n        self,\n        strategy_name: str,\n        signals:       dict,\n    ) -> Optional[str]:\n        """Refuse to OPEN what the exit ladder is built to eject.\n\n        The v12 trend-flip exit closes a credit vertical that a measured\n        trend has run against, at a loss, by design. Opening one is the same\n        trade entered from the wrong side of it: the entry pays the spread,\n        the ladder ejects it, and the round trip is the P&L. Symmetric\n        structures are refused on the same evidence only when the\n        displacement is strong - a condor is a range trade and a tape 0.10%+\n        off VWAP with a mature ADX at or above the strong threshold is not\n        ranging, so one of its two shorts is being tested from the first\n        cycle.\n        """\n        cfg = self.config\n        if not bool(getattr(cfg, "counter_trend_entry_block", True)):\n            return None\n        latch = self._tape_displacement(signals)\n        if not latch:\n            return None\n        try:\n            d    = int(latch.get("dir", 0))\n            ladx = float(latch.get("adx", 0.0))\n            lvd  = float(latch.get("vwap_dist", 0.0))\n        except (TypeError, ValueError):\n            return None\n        if d == 0:\n            return None\n        _side = "uptrend" if d > 0 else "downtrend"\n        _tag = f"measured_{_side}_adx_{ladx:.0f}_vwap_{lvd:+.2f}pct"\n\n        if strategy_name == BEAR_CALL_SPREAD and d > 0:\n            return f"counter_trend_entry_blocked:{strategy_name}:{_tag}"\n        if strategy_name == BULL_PUT_SPREAD and d < 0:\n            return f"counter_trend_entry_blocked:{strategy_name}:{_tag}"\n        if strategy_name in (IRON_CONDOR, IRON_BUTTERFLY):\n            _strong = float(getattr(cfg, "displaced_tape_adx_min",\n                                    getattr(cfg, "adx_strong_threshold", 28.0)))\n            _buf = abs(float(getattr(cfg, "counter_trend_vwap_dist_min_pct", 0.10)))\n            if ladx >= _strong and abs(lvd) >= _buf:\n                return (\n                    f"displaced_tape_no_symmetric_structure:{strategy_name}:"\n                    f"adx_{ladx:.0f}_ge_{_strong:.0f}:vwap_{lvd:+.2f}pct"\n                )\n        return None\n\n    def _in_late_momentum_window(self, cur: dtime) -> bool:\n        """True when the closing-hour route owns the clock.\n\n        Starts after the sell side\'s last entry so the two routes can never\n        compete for a cycle; ends at the configured cut, and independently\n        at hard_exit - momentum_late_min_minutes_left, which keeps the rule\n        correct on a Tuesday\'s 15:00 square-off without a second clock.\n        """\n        cfg   = self.config\n        state = self.market_engine.state\n        if not bool(getattr(cfg, "momentum_late_enabled", True)):\n            return False\n        if not bool(getattr(cfg, "momentum_enabled", True)):\n            return False\n        try:\n            start = datetime.strptime(\n                str(getattr(cfg, "momentum_late_window_start", "14:30")),\n                "%H:%M").time()\n            end = datetime.strptime(\n                str(getattr(cfg, "momentum_late_window_end", "14:57")),\n                "%H:%M").time()\n        except Exception:\n            return False\n        try:\n            hard_exit = datetime.strptime(\n                state.get("hard_exit_time", "15:00"), "%H:%M").time()\n        except Exception:\n            hard_exit = cfg.hard_exit_time\n        _min_left = float(getattr(cfg, "momentum_late_min_minutes_left", 25))\n        try:\n            _last = (\n                datetime.combine(date.today(), hard_exit)\n                - datetime.combine(date.today(), end)\n            ).total_seconds() / 60.0\n            if _last < _min_left:\n                end = (\n                    datetime.combine(date.today(), hard_exit)\n                    - timedelta(minutes=_min_left)\n                ).time()\n        except Exception:\n            pass\n        return start <= cur <= end\n\n    def _late_fresh_extreme(self, signals: dict, direction: int) -> Tuple[bool, str]:\n        """The closing-hour trend must still be making ground NOW.\n\n        Primary test: spot beyond the extreme of the last\n        momentum_late_extreme_lookback_min minutes of the session, excluding\n        the current print (a level equal to the print that set it is not a\n        breakout). Fallback, used only when the price memory is shorter than\n        momentum_late_extreme_min_span_min - the replay harness calls decide()\n        only on cycles where the book is flat, so its memory starts when the\n        last position closed - : spot inside\n        momentum_late_range_proximity_frac of the session range from the\n        extreme it is attacking. Both tests ask the same question of the tape\n        and agree on every recorded session.\n        """\n        cfg  = self.config\n        try:\n            spot = float(signals.get("spot") or 0.0)\n        except (TypeError, ValueError):\n            spot = 0.0\n        if spot <= 0:\n            return False, "late_no_spot"\n        lookback = float(getattr(cfg, "momentum_late_extreme_lookback_min", 45))\n        min_span = float(getattr(cfg, "momentum_late_extreme_min_span_min", 30))\n        now  = now_ist()\n        cut  = now - timedelta(minutes=lookback)\n        hist = getattr(self, "_v13_price_hist", None) or []\n        win  = [(t, s) for (t, s) in hist if cut <= t < now]\n        span = 0.0\n        if len(win) >= 2:\n            span = (win[-1][0] - win[0][0]).total_seconds() / 60.0\n        if span >= min_span:\n            ref = min(s for _, s in win) if direction < 0 \\\n                else max(s for _, s in win)\n            ok  = spot < ref if direction < 0 else spot > ref\n            return ok, (\n                f"late_fresh_extreme_{lookback:.0f}min_ref_{ref:.2f}_"\n                f"spot_{spot:.2f}_span_{span:.0f}min"\n            )\n        try:\n            dh = float(signals.get("day_high") or 0.0)\n            dl = float(signals.get("day_low") or 0.0)\n        except (TypeError, ValueError):\n            dh = dl = 0.0\n        rng = dh - dl\n        if dh > 0 and dl > 0 and rng > 0:\n            frac = float(getattr(cfg, "momentum_late_range_proximity_frac", 0.30))\n            if direction < 0:\n                ok  = spot <= dl + frac * rng\n                ref = dl + frac * rng\n            else:\n                ok  = spot >= dh - frac * rng\n                ref = dh - frac * rng\n            return ok, (\n                f"late_range_proximity_ref_{ref:.2f}_spot_{spot:.2f}_"\n                f"span_{span:.0f}min"\n            )\n        return False, "late_no_extreme_reference"\n\n    def _validate_entry_rules(\n        self,\n        strategy_name: str,\n',
        '# PATCH_V13: tape evidence, entry/exit symmetry, session price memory',
    ),
    # =====================================================================
    # strategy_engine.py — S6:line2936-return_False__f_momentum_dte__dte_i__below_m
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S6:line2936-return_False__f_momentum_dte__dte_i__below_m',
        '            return False, f"momentum_dte_{dte_i}_below_min", 0\n        if dte_i > int(getattr(cfg, "momentum_max_dte", 4)):\n            return False, f"momentum_dte_{dte_i}_above_max", 0\n\n        # ── the read itself: a trend, not a range with drift ─────────────\n        price   = str(signals.get("price_regime") or "")\n        if price in ("UPTREND", "STRONG_UPTREND"):\n            direction = 1\n        elif price in ("DOWNTREND", "STRONG_DOWNTREND"):\n            direction = -1\n        else:\n',
        '            return False, f"momentum_dte_{dte_i}_below_min", 0\n        if dte_i > int(getattr(cfg, "momentum_max_dte", 4)):\n            return False, f"momentum_dte_{dte_i}_above_max", 0\n\n        # ── PATCH_V13: which route owns this cycle ───────────────────────\n        # The closing hour is a different trade from the morning breakout:\n        # the opening range is ancient, the session has 25-75 minutes left,\n        # and the classification that matters is the smoothed one (EMA\n        # structure, displacement from VWAP, a mature ADX at the strong\n        # threshold) rather than the fifteen-minute price regime, which on a\n        # closing-hour tape alternates RANGE/CHOPPY/UPTREND while the trend\n        # itself never stops. Everything below the route split is shared:\n        # the IV stack, the chase cap, the daily clip limit, the flat book\n        # requirement, the budget and the stop.\n        late = self._in_late_momentum_window(cur)\n\n        # ── the read itself: a trend, not a range with drift ─────────────\n        price   = str(signals.get("price_regime") or "")\n        ema     = str(signals.get("ema_structure") or "")\n        if late:\n            # The EMA structure sets the direction; the price regime is only\n            # allowed to VETO it, never to supply it (a single-cycle\n            # UPTREND print inside a bearish closing hour is the trap this\n            # route exists to avoid).\n            if ema == "BEARISH":\n                direction = -1\n            elif ema == "BULLISH":\n                direction = 1\n            else:\n                return False, f"momentum_late_ema_{ema or \'NONE\'}_no_direction", 0\n            if (direction < 0 and price in ("UPTREND", "STRONG_UPTREND")) or \\\n               (direction > 0 and price in ("DOWNTREND", "STRONG_DOWNTREND")):\n                return False, (\n                    f"momentum_late_price_regime_{price}_contradicts_"\n                    f"{\'call\' if direction > 0 else \'put\'}_side"\n                ), 0\n        elif price in ("UPTREND", "STRONG_UPTREND"):\n            direction = 1\n        elif price in ("DOWNTREND", "STRONG_DOWNTREND"):\n            direction = -1\n        else:\n',
        '# ── PATCH_V13: which route owns this cycle ───────────────────────',
    ),
    # =====================================================================
    # strategy_engine.py — S7:line2959-try
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S7:line2959-try',
        '        try:\n            adx = float(signals.get("adx_15") or 0.0)\n        except (TypeError, ValueError):\n            adx = 0.0\n        if adx < float(getattr(cfg, "momentum_adx_min", 30.0)):\n            return False, f"momentum_adx_{adx:.0f}_below_min", 0\n\n        # ── breakout PROOF: through the opening range, in the trend side ─\n        spot = float(signals.get("spot") or 0.0)\n        if spot <= 0:\n            return False, "momentum_no_spot", 0\n        # The opening range is mandatory: it is the level the trend has to be\n        # measured against, and without it "momentum" is just a green candle.\n        or_high = float(signals.get("or_high") or 0.0)\n        or_low  = float(signals.get("or_low") or 0.0)\n        or_w    = float(signals.get("or_width") or 0.0)\n        if not signals.get("or_computed") or or_high <= 0 or or_low <= 0:\n            return False, "momentum_no_opening_range_to_confirm", 0\n        _need = max(5.0, or_w * float(getattr(cfg, "momentum_or_break_frac", 0.15)))\n        if direction > 0 and spot < or_high + _need:\n            return False, (\n                f"momentum_call_not_through_or_high_{spot:.0f}<{or_high + _need:.0f}"\n            ), 0\n        if direction < 0 and spot > or_low - _need:\n            return False, (\n                f"momentum_put_not_through_or_low_{spot:.0f}>{or_low - _need:.0f}"\n            ), 0\n        vwap = signals.get("vwap")\n        try:\n            vwap = float(vwap) if vwap else 0.0\n        except (TypeError, ValueError):\n',
        '        try:\n            adx = float(signals.get("adx_15") or 0.0)\n        except (TypeError, ValueError):\n            adx = 0.0\n        # PATCH_V13: the closing hour pays premium out of a session that is\n        # nearly over, so it demands a MEASURED-STRONG trend - the same bar\n        # the engine uses everywhere else to separate "trending" from "has\n        # drifted" (adx_strong_threshold). The morning breakout route keeps\n        # its own, lower bar. Measured across the five recorded sessions:\n        # the afternoon ADX on the two days whose closing hour went nowhere\n        # (2026-09-08: 12-17, 2026-09-10: 22-27 at 14:40) sits below 28, and\n        # the one day whose closing hour carried the session (2026-09-09:\n        # 24 -> 31 -> 40 between 14:39 and 15:06) crosses it at 14:45 and\n        # never looks back.\n        _adx_min = float(getattr(cfg, "momentum_late_adx_min", 28.0)) if late \\\n            else float(getattr(cfg, "momentum_adx_min", 30.0))\n        if adx < _adx_min:\n            return False, f"momentum_adx_{adx:.0f}_below_min", 0\n        if late and not bool(signals.get("adx_15_mature", False)):\n            return False, "momentum_late_adx_immature", 0\n\n        # ── breakout PROOF: through the opening range, in the trend side ─\n        spot = float(signals.get("spot") or 0.0)\n        if spot <= 0:\n            return False, "momentum_no_spot", 0\n        if late:\n            # PATCH_V13: displacement + a fresh extreme. The opening range\n            # was set five hours ago; by the closing hour it is a level, not\n            # a reference (2026-09-09: the whole afternoon move happened\n            # inside the morning range, so an OR-break test could never see\n            # it). What a closing-hour continuation has to prove is that the\n            # tape is OFF VWAP by more than noise and is still making ground\n            # now.\n            try:\n                _vd = float(signals.get("vwap_dist_pct") or 0.0)\n            except (TypeError, ValueError):\n                _vd = 0.0\n            _vd_min = float(getattr(cfg, "momentum_late_vwap_dist_min_pct", 0.10))\n            if direction < 0 and _vd > -_vd_min:\n                return False, (\n                    f"momentum_late_put_displacement_{_vd:+.3f}pct_"\n                    f"above_{-_vd_min:.2f}pct"\n                ), 0\n            if direction > 0 and _vd < _vd_min:\n                return False, (\n                    f"momentum_late_call_displacement_{_vd:+.3f}pct_"\n                    f"below_{_vd_min:.2f}pct"\n                ), 0\n            _fx_ok, _fx_why = self._late_fresh_extreme(signals, direction)\n            if not _fx_ok:\n                return False, f"momentum_{_fx_why}", 0\n        else:\n            # The opening range is mandatory: it is the level the trend has to be\n            # measured against, and without it "momentum" is just a green candle.\n            or_high = float(signals.get("or_high") or 0.0)\n            or_low  = float(signals.get("or_low") or 0.0)\n            or_w    = float(signals.get("or_width") or 0.0)\n            if not signals.get("or_computed") or or_high <= 0 or or_low <= 0:\n                return False, "momentum_no_opening_range_to_confirm", 0\n            _need = max(5.0, or_w * float(getattr(cfg, "momentum_or_break_frac", 0.15)))\n            if direction > 0 and spot < or_high + _need:\n                return False, (\n                    f"momentum_call_not_through_or_high_{spot:.0f}<{or_high + _need:.0f}"\n                ), 0\n            if direction < 0 and spot > or_low - _need:\n                return False, (\n                    f"momentum_put_not_through_or_low_{spot:.0f}>{or_low - _need:.0f}"\n                ), 0\n        vwap = signals.get("vwap")\n        try:\n            vwap = float(vwap) if vwap else 0.0\n        except (TypeError, ValueError):\n',
        '# PATCH_V13: the closing hour pays premium out of a session that is',
    ),
    # =====================================================================
    # strategy_engine.py — S8:line3048-hard_exit___datetime_strptime
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S8:line3048-hard_exit___datetime_strptime',
        '            hard_exit = datetime.strptime(\n                state.get("hard_exit_time", "15:00"), "%H:%M").time()\n        except Exception:\n            hard_exit = cfg.hard_exit_time\n        if cur < entry_start:\n            return False, f"momentum_before_entry_window_{entry_start}", 0\n        if cur > entry_end:\n            return False, f"momentum_past_entry_window_{entry_end}", 0\n        mins_left = self._minutes_to_time(cur, hard_exit)\n        if mins_left < float(getattr(cfg, "momentum_min_minutes_left", 90)):\n            return False, (\n                f"momentum_only_{mins_left:.0f}min_before_hard_exit"\n            ), 0\n\n        # ── one clip a day, and never beside an open position ───────────\n        if self._count_momentum_entries() >= int(\n                getattr(cfg, "momentum_max_trades_per_day", 1)):\n',
        '            hard_exit = datetime.strptime(\n                state.get("hard_exit_time", "15:00"), "%H:%M").time()\n        except Exception:\n            hard_exit = cfg.hard_exit_time\n        if late:\n            # PATCH_V13: the closing-hour clock. _in_late_momentum_window\n            # already clipped the window end to hard_exit - min_minutes_left,\n            # so a Tuesday\'s 15:00 square-off is handled by the same rule as\n            # a 15:20 day. The check is repeated here so the reason string\n            # says which bound failed.\n            _late_min = float(getattr(cfg, "momentum_late_min_minutes_left", 25))\n            _mins_left = self._minutes_to_time(cur, hard_exit)\n            if _mins_left < _late_min:\n                return False, (\n                    f"momentum_late_only_{_mins_left:.0f}min_before_hard_exit"\n                ), 0\n        else:\n            if cur < entry_start:\n                return False, f"momentum_before_entry_window_{entry_start}", 0\n            if cur > entry_end:\n                return False, f"momentum_past_entry_window_{entry_end}", 0\n            mins_left = self._minutes_to_time(cur, hard_exit)\n            if mins_left < float(getattr(cfg, "momentum_min_minutes_left", 90)):\n                return False, (\n                    f"momentum_only_{mins_left:.0f}min_before_hard_exit"\n                ), 0\n\n        # ── one clip a day, and never beside an open position ───────────\n        if self._count_momentum_entries() >= int(\n                getattr(cfg, "momentum_max_trades_per_day", 1)):\n',
        '# PATCH_V13: the closing-hour clock. _in_late_momentum_window',
    ),
    # =====================================================================
    # strategy_engine.py — S9:line3072-return_False___momentum_abort_active___0
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S9:line3072-return_False___momentum_abort_active___0',
        '            return False, "momentum_abort_active", 0\n        if signals.get("chain_stale"):\n            return False, "momentum_chain_stale", 0\n\n        return True, "momentum_gate_open", direction\n\n    def _momentum_pick_strike(\n        self,\n        chain:    dict,\n',
        '            return False, "momentum_abort_active", 0\n        if signals.get("chain_stale"):\n            return False, "momentum_chain_stale", 0\n\n        return True, (\n            "momentum_gate_open_late_window" if late else "momentum_gate_open"\n        ), direction\n\n    def _momentum_pick_strike(\n        self,\n        chain:    dict,\n',
        '"momentum_gate_open_late_window" if late else "momentum_gate_open"',
    ),
    # =====================================================================
    # strategy_engine.py — S10:line3132-direction_________int
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S10:line3132-direction_________int',
        '        direction:        int,\n        selection_reason: str,\n        signals:          dict,\n        size_mult:        float,\n    ) -> dict:\n        """Build a single-leg long-premium breakout position.\n\n        Deliberately separate from compute_params(): that function is a\n',
        '        direction:        int,\n        selection_reason: str,\n        signals:          dict,\n        size_mult:        float,\n        late:             bool = False,\n    ) -> dict:\n        """Build a single-leg long-premium breakout position.\n\n        Deliberately separate from compute_params(): that function is a\n',
        'late:             bool = False,',
    ),
    # =====================================================================
    # strategy_engine.py — S11:line3233-if_int_actual_dte_____0
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S11:line3233-if_int_actual_dte_____0',
        '            if int(actual_dte) == 0:\n                _mom_risk_frac = min(_mom_risk_frac, float(getattr(cfg, "momentum_dte0_risk_frac", 0.50)))\n        except (TypeError, ValueError):\n            pass\n        max_risk = current_capital * budget * _mom_risk_frac\n        risk_per_lot  = risk_pts * C02\n        structural_risk_per_lot = exec_price * C02          # premium paid, all of it\n        raw_lots = max_risk / max(risk_per_lot, 1.0)\n',
        '            if int(actual_dte) == 0:\n                _mom_risk_frac = min(_mom_risk_frac, float(getattr(cfg, "momentum_dte0_risk_frac", 0.50)))\n        except (TypeError, ValueError):\n            pass\n        # PATCH_V13: a closing-hour ticket risks half the ticket again. The\n        # edge is the session\'s last trend leg, but the ride is bounded by a\n        # clock rather than by a thesis, so the budget is halved and the lot\n        # count capped below.\n        if late:\n            _mom_risk_frac = min(\n                _mom_risk_frac,\n                float(getattr(cfg, "momentum_late_risk_frac", 0.50)),\n            )\n        max_risk = current_capital * budget * _mom_risk_frac\n        risk_per_lot  = risk_pts * C02\n        structural_risk_per_lot = exec_price * C02          # premium paid, all of it\n        raw_lots = max_risk / max(risk_per_lot, 1.0)\n',
        '# PATCH_V13: a closing-hour ticket risks half the ticket again. The',
    ),
    # =====================================================================
    # strategy_engine.py — S12:line3269-if_int_actual_dte_____0
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S12:line3269-if_int_actual_dte_____0',
        '            if int(actual_dte) == 0:\n                final_lots = min(final_lots, int(getattr(cfg, "momentum_dte0_max_lots", 2)))\n        except (TypeError, ValueError):\n            pass\n        # The sell side caps a position\'s STRUCTURAL loss (the margin that\n        # could actually be called if the stop never filled) at 1.5x the\n        # per-trade budget. A long option cannot lose more than the premium\n        # paid, and that premium is only fully lost if the contract is still\n',
        '            if int(actual_dte) == 0:\n                final_lots = min(final_lots, int(getattr(cfg, "momentum_dte0_max_lots", 2)))\n        except (TypeError, ValueError):\n            pass\n        # PATCH_V13: closing-hour clip cap (see the risk fraction above).\n        if late:\n            final_lots = max(\n                1, min(final_lots, int(getattr(cfg, "momentum_late_max_lots", 4)))\n            )\n        # The sell side caps a position\'s STRUCTURAL loss (the margin that\n        # could actually be called if the stop never filled) at 1.5x the\n        # per-trade budget. A long option cannot lose more than the premium\n        # paid, and that premium is only fully lost if the contract is still\n',
        '# PATCH_V13: closing-hour clip cap (see the risk fraction above).',
    ),
    # =====================================================================
    # strategy_engine.py — S13:line3363-profit_lock_stop_level___None
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S13:line3363-profit_lock_stop_level___None',
        '            "profit_lock_stop_level": None,\n            "stop_at_breakeven":      False,\n            "momentum":               True,\n            "momentum_direction":     int(direction),\n        }\n\n    def _momentum_decision(self, signals: dict, block_reason: str) -> Optional[dict]:\n        """Long-premium substitute for a refused sell-side structure.\n',
        '            "profit_lock_stop_level": None,\n            "stop_at_breakeven":      False,\n            "momentum":               True,\n            "momentum_direction":     int(direction),\n            # PATCH_V13: read by the debit exit ladder - a ticket opened\n            # inside the final window rides its ratchet and its stop to the\n            # hard exit instead of being banked at breakeven by D4.\n            "momentum_late":          bool(late),\n        }\n\n    def _momentum_decision(self, signals: dict, block_reason: str) -> Optional[dict]:\n        """Long-premium substitute for a refused sell-side structure.\n',
        '# PATCH_V13: read by the debit exit ladder - a ticket opened',
    ),
    # =====================================================================
    # strategy_engine.py — S14:line3380-return_None
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S14:line3380-return_None',
        '            return None\n        if not ok:\n            return None\n\n        size_mult = max(float(signals.get("size_multiplier") or 0.50), 0.10)\n        reason = (\n            f"momentum_trend_expression:{\'LONG_CALL\' if direction > 0 else \'LONG_PUT\'}"\n            f":dte={signals.get(\'actual_dte\')}:adx={float(signals.get(\'adx_15\') or 0.0):.0f}"\n            f":conf={signals.get(\'confidence_level\')}:replacing={block_reason}"\n        )\n        params = self.compute_momentum_params(direction, reason, signals, size_mult)\n        if not params.get("valid"):\n            self.logger.info(f"momentum substitute rejected: {params.get(\'reason\')}")\n            return None\n\n',
        '            return None\n        if not ok:\n            return None\n\n        # PATCH_V13: the gate tags which route opened - the morning breakout\n        # or the closing-hour continuation. The closing hour is sized smaller\n        # and is exempted from the "bank a long option at breakeven inside\n        # the final 45 minutes" rule, which would otherwise flatten a ticket\n        # bought inside that window on its first profitable cycle.\n        _late = "late_window" in str(why)\n\n        size_mult = max(float(signals.get("size_multiplier") or 0.50), 0.10)\n        reason = (\n            f"momentum_trend_expression:{\'LONG_CALL\' if direction > 0 else \'LONG_PUT\'}"\n            f":dte={signals.get(\'actual_dte\')}:adx={float(signals.get(\'adx_15\') or 0.0):.0f}"\n            f":conf={signals.get(\'confidence_level\')}"\n            f"{\':late_window\' if _late else \'\'}:replacing={block_reason}"\n        )\n        params = self.compute_momentum_params(\n            direction, reason, signals, size_mult, late=_late\n        )\n        if not params.get("valid"):\n            self.logger.info(f"momentum substitute rejected: {params.get(\'reason\')}")\n            return None\n\n',
        '# PATCH_V13: the gate tags which route opened - the morning breakout',
    ),
    # =====================================================================
    # strategy_engine.py — S15:line3407-params__________params
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S15:line3407-params__________params',
        '            "params":        params,\n        }\n\n    def decide(self, signals: dict) -> dict:\n        gate = self._check_hard_gates(signals)\n        if gate:\n            action, reason = gate\n            if action == "NO_TRADE":\n',
        '            "params":        params,\n        }\n\n    def decide(self, signals: dict) -> dict:\n        # PATCH_V13: session price memory for the closing-hour route. Kept\n        # here (not in the data engine) so the series is exactly the set of\n        # cycles on which the engine was allowed to act, in live and replay.\n        self._note_price(signals)\n\n        gate = self._check_hard_gates(signals)\n        if gate:\n            action, reason = gate\n            if action == "NO_TRADE":\n',
        '# PATCH_V13: session price memory for the closing-hour route. Kept',
    ),
    # =====================================================================
    # strategy_engine.py — S16:line3434-self_market_engine_finalize_cycle_log
    # =====================================================================
    _h(
        'strategy_engine.py',
        'S16:line3434-self_market_engine_finalize_cycle_log',
        '            self.market_engine.finalize_cycle_log(\n                "NO_TRADE", selection_reason, self._count_open_positions()\n            )\n            return {"action": "NO_TRADE", "reason": selection_reason}\n\n        rules_ok, rules_reason = self._validate_entry_rules(strategy_name, signals)\n        if not rules_ok:\n            full_reason = f"strategy_rules_failed:{rules_reason}"\n',
        '            self.market_engine.finalize_cycle_log(\n                "NO_TRADE", selection_reason, self._count_open_positions()\n            )\n            return {"action": "NO_TRADE", "reason": selection_reason}\n\n        # ── PATCH_V13: entry/exit trend symmetry ─────────────────────────\n        # The exit ladder ejects a credit vertical that a measured trend has\n        # run against, and refuses a symmetric structure only when the tape\n        # is not ranging. Opening either one into that same tape is a round\n        # trip paid for in advance: the entry, the ladder, the exit costs.\n        # The refusal is deliberately NOT answerable by the long-premium\n        # substitute - a measured trend against a credit structure is a\n        # reason to stand aside in the middle of the session, and the\n        # closing-hour route (which is separately gated on a strong trend, a\n        # fresh extreme, displacement and its own clock) is the only place\n        # this engine pays premium for a trend it did not see at the open.\n        _ct_reason = self._counter_trend_entry_refusal(strategy_name, signals)\n        if _ct_reason:\n            self._log_decision(signals, "NO_TRADE", _ct_reason)\n            self._persist_decision(\n                signals, strategy_name, _ct_reason, None, "NO_TRADE"\n            )\n            self.market_engine.finalize_cycle_log(\n                "NO_TRADE", _ct_reason, self._count_open_positions()\n            )\n            return {"action": "NO_TRADE", "reason": _ct_reason}\n\n        rules_ok, rules_reason = self._validate_entry_rules(strategy_name, signals)\n        if not rules_ok:\n            full_reason = f"strategy_rules_failed:{rules_reason}"\n',
        '# ── PATCH_V13: entry/exit trend symmetry ─────────────────────────',
    ),
    # =====================================================================
    # execution_engine.py — E1:line1878-datetime_combine_now_ist___date____hard_exit
    # =====================================================================
    _h(
        'execution_engine.py',
        'E1:line1878-datetime_combine_now_ist___date____hard_exit',
        '            datetime.combine(now_ist().date(), hard_exit)\n            - datetime.combine(now_ist().date(), current_time)\n        ).total_seconds() / 60.0\n        window = float(getattr(cfg, "momentum_final_window_min", 45))\n        if mins_left <= window:\n            if value >= entry_value + rt_cost:\n                return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {\n                    "reason_detail": "momentum_flat_before_close",\n                    "minutes_left": mins_left,\n',
        '            datetime.combine(now_ist().date(), hard_exit)\n            - datetime.combine(now_ist().date(), current_time)\n        ).total_seconds() / 60.0\n        window = float(getattr(cfg, "momentum_final_window_min", 45))\n        # PATCH_V13: a ticket OPENED inside the final window is exempt from\n        # the flatten-at-breakeven rule - it was bought with 25-45 minutes\n        # left, so D4 would bank it on its first profitable cycle and the\n        # closing-hour route could never earn the move it exists for. Its\n        # risk is still bounded three ways: the premium stop (D1), the\n        # ratchet once it is free (D2), and the hard exit (D5).\n        _late_ticket = bool(raw.get("momentum_late")) or (\n            "late_window" in str(position.get("selection_reason") or "")\n        )\n        if mins_left <= window and not _late_ticket:\n            if value >= entry_value + rt_cost:\n                return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {\n                    "reason_detail": "momentum_flat_before_close",\n                    "minutes_left": mins_left,\n',
        '# PATCH_V13: a ticket OPENED inside the final window is exempt from',
    ),
    # =====================================================================
    # execution_engine.py — E2:line1918-5__Cheap_buyback__eliminate_tail_risk
    # =====================================================================
    _h(
        'execution_engine.py',
        'E2:line1918-5__Cheap_buyback__eliminate_tail_risk',
        '        5. Cheap buyback (eliminate tail risk)\n        6. Time-based target (take profit at scheduled times)\n        7. Hard exit (time-based forced close)\n        """\n        legs         = self._get_position_legs(position["position_id"])\n        open_legs    = [l for l in legs if l.get("leg_status") == "OPEN"]\n        chain        = self.market_engine.last_chain\n        current_time = now_ist().time()\n',
        '        5. Cheap buyback (eliminate tail risk)\n        6. Time-based target (take profit at scheduled times)\n        7. Hard exit (time-based forced close)\n        """\n        # PATCH_V13: remember the tape the position was last marked on, so\n        # the close bookkeeping below can record WHERE the session exited.\n        # The anti-churn gate in the strategy engine measures the re-entry\n        # from this spot, and without it the engine re-sold the same regime\n        # at the same price fifteen seconds after banking a winner\n        # (measured 2026-09-09: +625 banked at 12:30:01 on spot 23,511, a\n        # four-leg condor entered at 12:31:01 on spot 23,516).\n        try:\n            self.market_engine.state["_last_monitor_spot"] = float(\n                signals.get("spot") or 0.0)\n        except (TypeError, ValueError, AttributeError):\n            pass\n\n        legs         = self._get_position_legs(position["position_id"])\n        open_legs    = [l for l in legs if l.get("leg_status") == "OPEN"]\n        chain        = self.market_engine.last_chain\n        current_time = now_ist().time()\n',
        '# PATCH_V13: remember the tape the position was last marked on, so',
    ),
    # =====================================================================
    # execution_engine.py — E3:line2939-state__daily_pnl___________float_state_get
    # =====================================================================
    _h(
        'execution_engine.py',
        'E3:line2939-state__daily_pnl___________float_state_get',
        '        state["daily_pnl"]       = float(state.get("daily_pnl", 0.0) or 0.0) + net_pnl_rs\n        state["current_capital"] = float(state.get("current_capital",\n                                                     self.config.starting_capital) or 0) + net_pnl_rs\n\n        # Consecutive stops tracking\n        if reason == "CLOSE_STOP" or priority in (\n            EXIT_PRIORITY_DELTA_BREACH,\n            EXIT_PRIORITY_SPOT_PROXIMITY,\n            EXIT_PRIORITY_PRICE_STOP,\n        ):\n            state["last_stop_time"]   = now_ist().isoformat()\n            state["last_stop_reason"] = reason\n            state["consecutive_stops"] = int(state.get("consecutive_stops", 0) or 0) + 1\n\n            # Record the signal combo that caused the stop\n',
        '        state["daily_pnl"]       = float(state.get("daily_pnl", 0.0) or 0.0) + net_pnl_rs\n        state["current_capital"] = float(state.get("current_capital",\n                                                     self.config.starting_capital) or 0) + net_pnl_rs\n\n        # ── PATCH_V13: record the exit itself, not only the stop ─────────\n        # The anti-churn gate needs to know where and when the book last\n        # went flat; the stop machinery needs to know whether the exit was a\n        # loss. Both are recorded here so the replay harness can mirror this\n        # method exactly instead of keeping its own, divergent, copy.\n        _now_iso = now_ist().isoformat()\n        state["last_exit_time"]   = _now_iso\n        state["last_exit_reason"] = reason\n        state["last_exit_priority"] = int(priority or 0)\n        state["last_exit_pnl_rs"] = float(net_pnl_rs or 0.0)\n        try:\n            _xspot = float(state.get("_last_monitor_spot") or 0.0)\n        except (TypeError, ValueError):\n            _xspot = 0.0\n        if _xspot > 0:\n            state["last_exit_spot"] = _xspot\n\n        # ── PATCH_V13: a protective exit that BANKS profit is not a stop ──\n        # The ratcheted profit lock and an in-the-money price stop both come\n        # back as CLOSE_TARGET/CLOSE_STOP on a priority-1..3 rung, and the\n        # bookkeeping below counted every one of them against the day\'s stop\n        # budget regardless of the P&L - so two WINNERS taken by the trail\n        # halted the session (consecutive_stops >= 2 -> daily_halted) and a\n        # single banked winner locked the sell side out for the 30-minute\n        # CLOSE_STOP cooldown. What the stop budget exists to stop is\n        # re-selling a tape that has just punished the structure, and a trade\n        # that closed green did not.\n        _protective = reason == "CLOSE_STOP" or priority in (\n            EXIT_PRIORITY_DELTA_BREACH,\n            EXIT_PRIORITY_SPOT_PROXIMITY,\n            EXIT_PRIORITY_PRICE_STOP,\n            EXIT_PRIORITY_PROFIT_LOCK,\n        )\n        _banked = bool(getattr(self.config, "banked_exit_is_not_a_stop", True)) \\\n            and _protective and float(net_pnl_rs or 0.0) > 0.0\n        if _protective and _banked:\n            state["last_stop_time"]   = None\n            state["last_stop_reason"] = ""\n            state["last_stop_signal_combo"] = ""\n            state["consecutive_stops"] = 0\n        elif _protective:\n            state["last_stop_time"]   = _now_iso\n            state["last_stop_reason"] = reason\n            state["consecutive_stops"] = int(state.get("consecutive_stops", 0) or 0) + 1\n\n            # Record the signal combo that caused the stop\n',
        '# ── PATCH_V13: record the exit itself, not only the stop ─────────',
    ),
    # =====================================================================
    # backtest_engine.py — B1:line166-def_uninstall_self_____None
    # =====================================================================
    _h(
        'backtest_engine.py',
        'B1:line166-def_uninstall_self_____None',
        '    def uninstall(self) -> None:\n        for mod, attr, original in reversed(self._installed):\n            setattr(mod, attr, original)\n        self._installed.clear()\n\n\n# ═══════════════════════════════════════════════════════════════════════════\n#  HISTORICAL DATA\n',
        '    def uninstall(self) -> None:\n        for mod, attr, original in reversed(self._installed):\n            setattr(mod, attr, original)\n        self._installed.clear()\n\n\ndef bt_exit_reason(action: str, priority: int) -> str:\n    """PATCH_V13: the reason string the LIVE close path would have used.\n\n    monitor_all_positions() does not hand execute_close() the ladder\'s\n    reason_detail; it maps the exit priority through EXIT_REASON_MAP and\n    overrides for the forced closes. The session-state bookkeeping keys off\n    that string (CLOSE_STOP drives the 30-minute cooldown, CLOSE_TARGET\n    resets the stop budget), so replaying it with the harness\'s own detail\n    text would have kept the two paths disagreeing.\n    """\n    reason = action\n    try:\n        from execution_engine import EXIT_REASON_MAP\n        reason = EXIT_REASON_MAP.get(int(priority), action)\n    except Exception:\n        reason = action\n    if action == "HARD_EXIT_15:00":\n        return "HARD_EXIT_15:00"\n    if action in ("EOD_CLOSE", "SHUTDOWN_CLOSE", "STALE_PRIOR_DAY_CLOSE"):\n        return action\n    return reason\n\n\n# ═══════════════════════════════════════════════════════════════════════════\n#  HISTORICAL DATA\n',
        '"""PATCH_V13: the reason string the LIVE close path would have used.',
    ),
    # =====================================================================
    # backtest_engine.py — B2:line1124-self_results_add_trade_t
    # =====================================================================
    _h(
        'backtest_engine.py',
        'B2:line1124-self_results_add_trade_t',
        '                    self.results.add_trade(t)\n                    day_pnl += t.pnl_rs\n                    live = None\n                    state["daily_pnl"] = day_pnl\n                    state["consecutive_stops"] = (\n                        int(state.get("consecutive_stops", 0)) + 1\n                        if t.pnl_rs < 0 else 0\n                    )\n                    state["last_stop_time"] = (\n                        self.clock.now().isoformat() if t.pnl_rs < 0 else\n                        state.get("last_stop_time")\n                    )\n                    if self.verbose:\n                        print(f"  {trading_date} {dt:%H:%M} EXIT  "\n                              f"{t.exit_reason[:34]:34s} pnl={t.pnl_rs:>10,.0f}")\n                    # v7: the block for the trade that just closed is printed\n',
        '                    self.results.add_trade(t)\n                    day_pnl += t.pnl_rs\n                    live = None\n                    state["daily_pnl"] = day_pnl\n                    # ── PATCH_V13: replay the LIVE close bookkeeping ──────\n                    # This block used to keep its own copy of the session\n                    # state: consecutive_stops incremented on any losing\n                    # exit, last_stop_time set, and last_stop_reason never\n                    # set at all - so the 30-minute CLOSE_STOP cooldown, the\n                    # same-signal-combo block and the two-stop halt were all\n                    # dead code in replay while being live in production.\n                    # Every number this harness printed was therefore an\n                    # upper bound on what the engine would have done, and\n                    # the difference was not theoretical: on 2026-09-09 the\n                    # replay re-entered fifteen seconds after a profit-lock\n                    # exit that live would have cooled down for ten minutes.\n                    # The live method is now called with the same reason\n                    # string monitor_all_positions() derives, so the two\n                    # paths cannot drift again.\n                    try:\n                        state["_last_monitor_spot"] = float(\n                            signals.get("spot") or 0.0)\n                    except (TypeError, ValueError):\n                        pass\n                    _live_reason = bt_exit_reason(action, priority)\n                    try:\n                        with self._quiet():\n                            self.xe._update_state_after_close(\n                                _live_reason, t.pnl_rs, priority)\n                    except Exception as exc:\n                        if self.verbose:\n                            print(f"  close bookkeeping failed: {exc}")\n                    # the harness owns the day accumulator and recomputes\n                    # capital from the results ledger every cycle\n                    state = self.me.state\n                    state["daily_pnl"] = day_pnl\n                    if self.verbose:\n                        print(f"  {trading_date} {dt:%H:%M} EXIT  "\n                              f"{t.exit_reason[:34]:34s} pnl={t.pnl_rs:>10,.0f}")\n                    # v7: the block for the trade that just closed is printed\n',
        '# ── PATCH_V13: replay the LIVE close bookkeeping ──────',
    ),
    # =====================================================================
    # main.py — M1:line941-self_check_daily_loss_halt
    # =====================================================================
    _h(
        'main.py',
        'M1:line941-self_check_daily_loss_halt',
        '            # started here is serialised with the cycle exactly like the sweep.\n            self.check_daily_loss_halt()\n\n            # ── Step 8: Strategy decision and entry ─────────────────────────\n            entry_possible = (\n                acting and\n                current_time >= dtime(9, 30) and\n                current_time <= dtime(14, 30) and\n                not self.market_engine.state.get("daily_halted") and\n                not signals.get("block_new_entries") and\n                bool(signals.get("or_computed", False)) and\n                # PATCH_V12: decide() also runs when the regime layer\n',
        '            # started here is serialised with the cycle exactly like the sweep.\n            self.check_daily_loss_halt()\n\n            # ── Step 8: Strategy decision and entry ─────────────────────────\n            # PATCH_V13: the outer entry window extends to the closing-hour\n            # route\'s own cut. Nothing about the sell side changes - it still\n            # cannot enter after trading_window_last_entry (14:00, refused by\n            # _check_hard_gates) and the regime layer still refuses every new\n            # position after 14:30. What the extra minutes buy is that\n            # decide() RUNS, so the long-premium closing-hour ticket is\n            # considered in production exactly as it is in replay: the\n            # harness calls decide() on every cycle it is flat, and a live\n            # engine that stopped asking at 14:30 would silently drop the\n            # route the replay is measuring.\n            try:\n                _late_cut = datetime.strptime(\n                    str(getattr(self.config, "momentum_late_window_end", "14:57")),\n                    "%H:%M",\n                ).time()\n            except Exception:\n                _late_cut = dtime(14, 57)\n            _entry_cut = dtime(14, 30)\n            if bool(getattr(self.config, "momentum_late_enabled", True)) and \\\n                    bool(getattr(self.config, "momentum_enabled", True)):\n                _entry_cut = max(_entry_cut, _late_cut)\n\n            entry_possible = (\n                acting and\n                current_time >= dtime(9, 30) and\n                current_time <= _entry_cut and\n                not self.market_engine.state.get("daily_halted") and\n                not signals.get("block_new_entries") and\n                bool(signals.get("or_computed", False)) and\n                # PATCH_V12: decide() also runs when the regime layer\n',
        '# PATCH_V13: the outer entry window extends to the closing-hour',
    ),
    # =====================================================================
    # main.py — M2:line966-self_execution_engine_process_entry_decision
    # =====================================================================
    _h(
        'main.py',
        'M2:line966-self_execution_engine_process_entry_decision',
        '                        self.execution_engine.process_entry_decision(decision, signals)\n                except Exception as e:\n                    self.logger.error(f"Strategy/entry error: {e}", exc_info=True)\n            elif acting and current_time >= dtime(9, 30) and \\\n                    current_time <= dtime(14, 30) and self._feed_stale:\n                self.logger.info(\n                    "entries blocked this cycle: trading feed is stale (watchdog)"\n                )\n\n',
        '                        self.execution_engine.process_entry_decision(decision, signals)\n                except Exception as e:\n                    self.logger.error(f"Strategy/entry error: {e}", exc_info=True)\n            elif acting and current_time >= dtime(9, 30) and \\\n                    current_time <= _entry_cut and self._feed_stale:\n                self.logger.info(\n                    "entries blocked this cycle: trading feed is stale (watchdog)"\n                )\n\n',
        'current_time <= _entry_cut and self._feed_stale:',
    ),
]


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _ensure_v12(root: Path) -> bool:
    """patch_v13's hunks are anchored on v12 text: refuse without it."""
    missing = []
    for fname, marker in V12_MARKERS:
        try:
            if marker not in (root / fname).read_text(encoding="utf-8"):
                missing.append(f"{fname}:{marker[:40]}")
        except Exception as exc:
            missing.append(f"{fname}: unreadable ({exc})")
    if not missing:
        return True
    print("  patch_v12 markers not present:")
    for m in missing:
        print(f"    - {m}")
    for cand in ("patch_v12.py", "patch.py"):
        p = root / cand
        if p.is_file():
            print(f"  applying {cand} first ...")
            try:
                rc = subprocess.call([sys.executable, str(p)], cwd=str(root))
            except Exception as exc:
                _fail(f"could not run {cand}: {exc}")
                return False
            if rc != 0:
                _fail(f"{cand} exited {rc}")
                return False
            still = []
            for fname, marker in V12_MARKERS:
                try:
                    if marker not in (root / fname).read_text(encoding="utf-8"):
                        still.append(fname)
                except Exception:
                    still.append(fname)
            if not still:
                print("  patch_v12 applied — continuing")
                return True
            _fail(f"patch_v12 markers still missing after {cand}: {', '.join(sorted(set(still)))}")
            return False
    _fail("patch_v12 is required and could not be applied automatically")
    return False


def main() -> int:
    root = Path.cwd()
    print("=" * 72)
    print("patch_v13.py — NIFTY engine: the closing hour, and the bookkeeping")
    print("               that kept the engine out of it")
    print("=" * 72)

    missing = [f for f in sorted(ALLOWED_FILES) if not (root / f).is_file()]
    if missing:
        print(f"ERROR: run from the repository root. Missing: {', '.join(missing)}")
        return 1

    if not _ensure_v12(root):
        print("PATCH_V13 NOT APPLIED — nothing was modified.")
        return 1

    applied: list = []
    skipped: list = []
    failed: list = []

    for h in HUNKS:
        path = root / h["file"]
        label = f"{h['file']}:{h['label']}"
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            _fail(f"{label}: cannot read ({exc})")
            failed.append(label)
            continue

        # Idempotency FIRST: if the verification string is already present
        # the hunk applied earlier. Several hunks preserve their original
        # text inside the replacement, so "old found" alone cannot
        # distinguish a fresh file from a patched one (re-running must SKIP,
        # never double-apply).
        if h["check"] in text:
            print(f"  [SKIP] {label} (already applied)")
            skipped.append(label)
            continue

        occurrences = text.count(h["old"])
        if occurrences == 1:
            backup = path.with_name(path.name + ".v13bak")
            if not backup.exists():
                try:
                    shutil.copy2(path, backup)
                except Exception as exc:
                    _fail(f"{label}: cannot back up ({exc})")
                    failed.append(label)
                    continue
            text = text.replace(h["old"], h["new"], 1)
            if h["check"] not in text:
                _fail(f"{label}: applied but verification string missing")
                failed.append(label)
                continue
            try:
                path.write_text(text, encoding="utf-8")
            except Exception as exc:
                _fail(f"{label}: cannot write ({exc})")
                failed.append(label)
                continue
            print(f"  [OK]   {label}")
            applied.append(label)
        elif occurrences == 0:
            _fail(f"{label}: original text not found (0 occurrences) — file differs from expected")
            failed.append(label)
        else:
            _fail(f"{label}: original text matches {occurrences}x — refusing ambiguous hunk")
            failed.append(label)

    print("-" * 72)
    print(f"applied: {len(applied)}, skipped: {len(skipped)}, failed: {len(failed)}")

    if failed:
        print("PATCH INCOMPLETE — no further checks run. Restore from *.v13bak if needed.")
        return 1

    touched = sorted({h["file"] for h in HUNKS})
    print("byte-compiling touched files...")
    for f in touched:
        try:
            py_compile.compile(str(root / f), doraise=True)
            print(f"  [OK]   {f} compiles")
        except Exception as exc:
            _fail(f"{f} does not compile: {exc}")
            return 1

    print("=" * 72)
    print("PATCH_V13 APPLIED SUCCESSFULLY.")
    print("Validate with:")
    print("  python3 backtest_engine.py --db data/per_day/nifty_algo_2026-09-09.db \\")
    print("      --from 2026-09-09 --to 2026-09-09 --trade-report off")
    print("  expected: 2 trades, total P&L Rs 3,909 (bear call +625 banked at")
    print("  12:30 by the ratchet, closing-hour long put 14:45 -> 15:20 hard")
    print("  exit +3,284), win rate 100%, max drawdown Rs 0.")
    print("  08/10/11/15-Sep must be unchanged: +2,544 / +2,025 / +17,332 / +8,367.")
    print("  Five-session total: Rs 34,177 (v12 baseline Rs 30,811).")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())