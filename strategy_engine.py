# strategy_engine.py
# NIFTY Intraday Options Engine v3.0
# Strategy selection, parameter computation, strike selection,
# trade validation, and decision persistence.
# Regime-based decision tree, straddle-based strikes,
# cost-aware edge validation, EV gate, spread-aware slippage.

from __future__ import annotations

import json
import math
from datetime import datetime, date, time as dtime, timedelta
from typing import Optional, Tuple, List, Dict

from core import (
    Config, Database,
    ExpiryCalendar, now_ist, today_ist,
    print_section, print_kv_table,
    load_config, setup_logging,
    RateLimiter, UpstoxClient,
)
from data_engine import MarketDataEngine
from calibration_engine import CalibrationEngine, CalibrationState

IRON_CONDOR      = "IRON_CONDOR"
IRON_BUTTERFLY   = "IRON_BUTTERFLY"
BULL_PUT_SPREAD  = "BULL_PUT_SPREAD"
BEAR_CALL_SPREAD = "BEAR_CALL_SPREAD"
# v5: long-premium expressions of a confirmed intraday trend. These are the
# only BUY-side structures in the engine and they exist because a
# premium-selling book has no answer at all to a trending session on a
# contract with two-plus sessions of remaining value: the vertical it would
# sell decays by ~2 points a DAY, so an intraday hold earns less than the
# round trip costs, while the move itself is worth 40+.
LONG_CALL        = "LONG_CALL"
LONG_PUT         = "LONG_PUT"
MOMENTUM_STRATEGIES = (LONG_CALL, LONG_PUT)
SELL = "SELL"
BUY  = "BUY"

# v3.1: the condor and the credit spreads were capped at DTE 2 while every
# other DTE-indexed table in the engine (hard gates, p_win, targets, risk,
# LOT_CAPS_BY_DAY) was populated out to DTE 6. On the Tuesday-expiry calendar
# Wednesday is DTE 4 and Thursday is DTE 3, so those two sessions could select
# a strategy and were then ALWAYS rejected by compute_params — 40% of the
# trading week was structurally unreachable, and the only trace was a
# "dte_4_above_max_2" line in the decision log. The regime engine now gates
# DTE 3/4 explicitly and strictly (see regime_engine._classify_range).
DTE_REQUIREMENTS: Dict[str, Tuple[int, int]] = {
    IRON_BUTTERFLY:   (0, 1),
    IRON_CONDOR:      (0, 4),
    BULL_PUT_SPREAD:  (0, 4),
    BEAR_CALL_SPREAD: (0, 4),
}

MIN_CREDIT_RATIO: Dict[str, float] = {
    IRON_BUTTERFLY:   0.15,
    IRON_CONDOR:      0.10,
    BULL_PUT_SPREAD:  0.08,
    BEAR_CALL_SPREAD: 0.08,
}

MIN_CREDIT_RATIO_DTE0: Dict[str, float] = {
    IRON_BUTTERFLY:   0.18,
    IRON_CONDOR:      0.13,
    BULL_PUT_SPREAD:  0.11,
    BEAR_CALL_SPREAD: 0.11,
}

LOT_CAPS_BY_DAY: Dict[str, int] = {
    "MONDAY":    8,
    "TUESDAY":   10,
    "WEDNESDAY": 6,
    "THURSDAY":  6,
    "FRIDAY":    5,
}

ENTRY_COOLDOWN_MIN = 10

STOP_COOLDOWN_MAP: Dict[str, int] = {
    "CLOSE_STOP":  30,
    "CLOSE_ADX":   45,
    "CLOSE_VWAP":  20,
    "CLOSE_DELTA": 30,
}


class StrategyEngine:

    def __init__(
        self,
        config:        Config,
        db:            Database,
        market_engine: MarketDataEngine,
        cal_engine:    CalibrationEngine,
        logger,
    ):
        self.config        = config
        self.db            = db
        self.market_engine = market_engine
        self.cal_engine    = cal_engine
        self.logger        = logger
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS strategy_decisions (
                decision_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_time TEXT NOT NULL,
                trading_date  TEXT NOT NULL,
                action        TEXT NOT NULL,
                strategy_name TEXT,
                reason        TEXT,
                params_json   TEXT,
                signals_json  TEXT
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sd_time ON strategy_decisions(decision_time)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sd_date ON strategy_decisions(trading_date)"
        )

    def _count_open_positions(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) as cnt FROM positions WHERE trading_date=? AND status='OPEN'",
            (today_ist().isoformat(),),
        )
        return row["cnt"] if row else 0

    def _count_today_entries(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) as cnt FROM positions "
            "WHERE trading_date=? AND status IN ('OPEN','CLOSED')",
            (today_ist().isoformat(),),
        )
        return row["cnt"] if row else 0

    def _count_momentum_entries(self) -> int:
        """Momentum tickets booked today, read from the ledger not from memory.

        state["momentum_entries"] is the live counter, but the daily cap has
        to survive a mid-session restart, and a process that restarts at
        11:30 with a long call already open has no memory of having taken it.
        The positions table knows; the max of the two is the honest number.
        """
        names = tuple(MOMENTUM_STRATEGIES)
        marks = ",".join("?" * len(names))
        try:
            row = self.db.query_one(
                f"SELECT COUNT(*) AS cnt FROM positions "
                f"WHERE trading_date=? AND strategy_name IN ({marks})",
                (today_ist().isoformat(),) + names,
            )
        except Exception:
            row = None
        db_cnt = int(row["cnt"]) if row else 0
        st_cnt = int(self.market_engine.state.get("momentum_entries", 0) or 0)
        return max(db_cnt, st_cnt)

    def _minutes_to_time(self, t1: dtime, t2: dtime) -> float:
        dt1 = datetime.combine(today_ist(), t1)
        dt2 = datetime.combine(today_ist(), t2)
        return (dt2 - dt1).total_seconds() / 60.0

    def _get_calibration(self) -> Optional[CalibrationState]:
        return self.cal_engine.state

    def _check_hard_gates(
        self,
        signals: dict,
        _test_time: Optional[dtime] = None,
    ) -> Optional[Tuple[str, str]]:
        state        = self.market_engine.state
        current_time = _test_time if _test_time is not None else now_ist().time()
        self._apply_two_way_location(signals, current_time)

        final_regime = signals.get("final_regime")
        if signals.get("block_new_entries"):
            notes = signals.get("final_regime_notes", "regime_engine_abort")
            return "NO_TRADE", f"ABORT:{notes}"
        if final_regime in ("NO_TRADE", "ABORT", None):
            notes = signals.get("final_regime_notes", "regime_engine_no_trade")
            return "NO_TRADE", str(notes) if notes else "regime_engine_no_trade"
        if state.get("daily_halted"):
            return "NO_TRADE", "daily_loss_limit_reached_or_halted"
        if signals.get("circuit_breaker_suspected"):
            return "NO_TRADE", "circuit_breaker_suspected"
        if signals.get("vix_spike_detected"):
            return "NO_TRADE", "vix_spike_detected"

        iv_behavior = signals.get("iv_behavior", "UNKNOWN")
        _loc_fade = bool(
            signals.get("afternoon_high_fade") or signals.get("afternoon_low_fade")
        )
        if iv_behavior == "EXPANDING" and not _loc_fade:
            return "NO_TRADE", "iv_expanding_never_sell_into_rising_iv"
        if iv_behavior == "SPIKING" and not _loc_fade:
            return "NO_TRADE", "iv_spiking"

        try:
            entry_start = datetime.strptime(
                state.get("entry_start", "09:45"), "%H:%M"
            ).time()
            entry_end = datetime.strptime(
                state.get("entry_end", "14:00"), "%H:%M"
            ).time()
        except Exception:
            entry_start = self.config.trading_window_start
            entry_end   = self.config.trading_window_last_entry

        if current_time < entry_start:
            return "NO_TRADE", f"before_entry_window_{entry_start}"
        if current_time > entry_end:
            return "NO_TRADE", f"past_entry_window_{entry_end}"

        open_count  = self._count_open_positions()
        total_count = self._count_today_entries()
        if total_count != state.get("entry_count", 0):
            state["entry_count"] = total_count
        if open_count >= self.config.max_concurrent_positions:
            return "NO_TRADE", "max_concurrent_positions_reached"
        if total_count >= self.config.max_entries_per_day:
            return "NO_TRADE", f"max_entries_per_day_{total_count}_reached"
        if open_count >= 1:
            return "NO_TRADE", "position_already_open_single_position_engine"

        # ── PATCH_V13: the entry cooldown is measured from the last ACT ──
        # It used to be measured from last_entry_time only, so a position
        # that was OPEN for two hours and closed at 12:30:01 satisfied the
        # cooldown at 12:30:02 and the engine re-sold the same regime
        # fifteen seconds later (measured 2026-09-09: bear call banked
        # +625 at 12:30:01, a four-leg condor entered at 12:31:01 at the
        # same spot, scratched -82). The cooldown exists to stop churn, and
        # churn is measured from the close, not from the open.
        _last_act = None
        for _t in (state.get("last_entry_time"), state.get("last_exit_time")):
            if not _t:
                continue
            try:
                _dt = datetime.fromisoformat(str(_t))
            except Exception:
                continue
            if _last_act is None or _dt > _last_act:
                _last_act = _dt
        if _last_act is not None and open_count == 0 and total_count > 0:
            try:
                mins = (now_ist() - _last_act).total_seconds() / 60.0
                if mins < ENTRY_COOLDOWN_MIN:
                    # PATCH_V25: after a failed-break scalp, the range
                    # condor is a DIFFERENT trade — do not wait 10 min.
                    _scalp_done = bool(
                        state.get("last_exit_is_failed_break_scalp")
                    )
                    _range_next = str(final_regime or "") == "PREMIUM_SELL_RANGE"
                    _stale_done = bool(state.get("last_exit_is_stale_weekly"))
                    _fade_next = (
                        (str(final_regime or "") == "PREMIUM_SELL_BEAR"
                         and bool(signals.get("afternoon_high_fade")))
                        or (str(final_regime or "") == "PREMIUM_SELL_BULL"
                            and bool(signals.get("afternoon_low_fade")))
                    )
                    if not ((_scalp_done and _range_next)
                            or (_scalp_done and _fade_next)
                            or (_stale_done and _fade_next)):
                        return "NO_TRADE", (
                            f"entry_cooldown_{ENTRY_COOLDOWN_MIN - mins:.0f}min_remaining"
                        )
            except Exception:
                pass

        # ── PATCH_V13: a re-entry has to be a NEW trade, not a repeat ────
        # After a close, the sell side stands aside until the tape has moved
        # enough that the structure it would sell is priced differently from
        # the one it just bought back - or until enough of the session has
        # passed that the read is re-confirmed on its own merits. This is
        # deliberately NOT answerable by the long-premium substitute: the
        # rule says "you just took this trade", which is as true of the
        # opposite expression of the same tape as of the same one.
        _exit_spot = state.get("last_exit_spot")
        _exit_time = state.get("last_exit_time")
        if _exit_spot and _exit_time and open_count == 0:
            try:
                _xdt = datetime.fromisoformat(str(_exit_time))
                _since = (now_ist() - _xdt).total_seconds() / 60.0
            except Exception:
                _since = None
            if _since is not None and _since < float(
                    getattr(self.config, "reentry_reconfirm_min", 45)):
                try:
                    _sp = float(signals.get("spot") or 0.0)
                    _xs = float(_exit_spot)
                except (TypeError, ValueError):
                    _sp = _xs = 0.0
                if _sp > 0 and _xs > 0:
                    _moved = abs(_sp - _xs)
                    _need = max(
                        float(getattr(self.config, "reentry_material_move_pts", 15.0)),
                        _sp * float(getattr(
                            self.config, "reentry_material_move_pct", 0.12)) / 100.0,
                    )
                    if _moved < _need:
                        # PATCH_V25: same exemption as cooldown — scalp
                        # then range condor on a pinned tape.
                        _scalp_done = bool(
                            state.get("last_exit_is_failed_break_scalp")
                        )
                        _range_next = str(final_regime or "") == "PREMIUM_SELL_RANGE"
                        _stale_done = bool(state.get("last_exit_is_stale_weekly"))
                        _fade_next = (
                            (str(final_regime or "") == "PREMIUM_SELL_BEAR"
                             and bool(signals.get("afternoon_high_fade")))
                            or (str(final_regime or "") == "PREMIUM_SELL_BULL"
                                and bool(signals.get("afternoon_low_fade")))
                        )
                        if not ((_scalp_done and _range_next)
                                or (_scalp_done and _fade_next)
                                or (_stale_done and _fade_next)):
                            return "NO_TRADE", (
                                f"no_material_change_since_exit_{_moved:.0f}pts_"
                                f"lt_{_need:.0f}pts_needed"
                            )

        if state.get("consecutive_stops", 0) >= 2:
            return "NO_TRADE", "2_consecutive_stops_halt"

        last_stop_reason = state.get("last_stop_reason", "")
        last_stop_combo  = state.get("last_stop_signal_combo", "")
        current_combo = (
            f"{signals.get('vol_regime', '')}_{signals.get('price_regime', '')}"
            f"_{signals.get('direction', '')}"
        )
        if (last_stop_reason == "CLOSE_STOP" and
                last_stop_combo == current_combo and
                state.get("consecutive_stops", 0) >= 1):
            return "NO_TRADE", f"same_signal_combo_caused_last_stop:{current_combo}"

        last_stop_time = state.get("last_stop_time")
        if last_stop_time and last_stop_reason:
            iv_extra = 20 if iv_behavior in ("EXPANDING", "SPIKING") else 0
            required = STOP_COOLDOWN_MAP.get(last_stop_reason, 30) + iv_extra
            try:
                mins = (
                    now_ist() - datetime.fromisoformat(last_stop_time)
                ).total_seconds() / 60.0
                if mins < required:
                    return "NO_TRADE", (
                        f"stop_cooldown_{required - mins:.0f}min_remaining"
                    )
            except Exception:
                pass

        if signals.get("spot_velocity_block"):
            return "NO_TRADE", f"spot_velocity_too_fast_{signals.get('spot_velocity_pts', 0):.0f}pts_in_3min"

        if signals.get("straddle_expanding"):
            return "NO_TRADE", "straddle_expanding_no_sell_into_rising_iv"

        if not signals.get("or_computed"):
            return "NO_TRADE", "opening_range_not_yet_computed"
        if signals.get("price_regime") in ("OBSERVING",):
            return "NO_TRADE", "opening_range_pending"
        if signals.get("chain_stale"):
            return "NO_TRADE", "chain_stale_cannot_validate_strikes"

        # PATCH_V12: on Tuesdays the tradeable contract is the 0DTE
        # series. If the broker has not listed it yet, the engine
        # used to trade the NEXT weekly as if it were a normal day
        # and then get re-priced onto the 0DTE chain mid-position
        # (measured 2026-09-08: Sep-15 spread sold at 09:51, marked
        # on Sep-08 quotes by the afternoon — a phantom Rs 8,993).
        # Wait for the real contract.
        # PATCH_V14: "is today an expiry day" is a CALENDAR question, and
        # "has the broker listed that series yet" is a chain question. The
        # old test used day_label == "TUESDAY" as a proxy for the first, so a
        # holiday-rolled Monday expiry (Tuesday closed, ExpiryCalendar already
        # moves the expiry back to Monday) waited for nothing and traded the
        # NEXT weekly as if it were the expiring one - the exact phantom-P&L
        # failure PATCH_V12 added this gate to prevent, on the one weekday the
        # proxy did not cover. Note the test must NOT read actual_dte: that is
        # derived from the chain, and the chain is precisely what is missing.
        try:
            _expiry_today = ExpiryCalendar.get_dte(today_ist()) == 0
        except Exception:
            _expiry_today = False
        try:
            _tue_wait = (
                _expiry_today
                and signals.get("active_expiry") is not None
                and signals.get("trading_date") is not None
                and str(signals.get("active_expiry"))[:10] != str(signals.get("trading_date"))[:10]
                and current_time < dtime(14, 0)
            )
        except Exception:
            _tue_wait = False
        if _tue_wait:
            return "NO_TRADE", "expiry_day_waiting_for_0dte_series_listed"

        confidence = signals.get("confidence_level", "NONE")
        if confidence in ("LOW", "NONE"):
            return "NO_TRADE", f"confidence_{confidence}_insufficient_edge_after_costs"

        # PATCH_V14: one ceiling, read from MAX_DTE_TRADEABLE, shared with
        # regime_engine.classify_final and with the DTE_REQUIREMENTS cap in
        # compute_params. Was a literal 6 here and in the regime layer against
        # a Config field of 4 that nothing read.
        actual_dte = signals.get("actual_dte")
        _max_dte = int(getattr(self.config, "max_dte_tradeable", 4) or 4)
        if actual_dte is not None and actual_dte > _max_dte:
            return "NO_TRADE", (
                f"dte_{actual_dte}_above_max_{_max_dte}_intraday_only"
            )
        if actual_dte is not None and actual_dte >= 4:
            if confidence not in ("HIGH", "MEDIUM"):
                return "NO_TRADE", (
                    f"dte_{actual_dte}_requires_medium_high_confidence"
                )

        # PATCH_V12: the day-move block is measured on the side that
        # threatens the structure, not the whole range. A 250%
        # down-range day is the SAFEST tape to be short calls on
        # (measured 2026-09-15: regime said PREMIUM_SELL_BEAR all
        # day, the blanket block refused every cycle, zero trades on
        # a -458 crash). Condors keep the total-range block.
        day_move_used = float(signals.get("day_move_used_pct") or 0.0)
        _dm_threat = day_move_used
        try:
            if final_regime == "PREMIUM_SELL_BEAR":
                _dm_threat = float(signals.get("day_up_used_pct", day_move_used) or 0.0)
            elif final_regime == "PREMIUM_SELL_BULL":
                _dm_threat = float(signals.get("day_down_used_pct", day_move_used) or 0.0)
        except (TypeError, ValueError):
            _dm_threat = day_move_used
        # PATCH_V12 (round 2): a CONFIRMED trend exempts the
        # trend-side vertical from the day-move block. An exhausted
        # WITH-trend move is the thesis of the structure, not its
        # risk: the opening spike that inflates the gauge is ancient
        # history when spot sits 250pts below it (measured
        # 2026-09-15: a +100pt opening spike annualised to 358% by
        # the time-fraction normalisation, blocking a confirmed
        # STRONG_DOWNTREND bear regime all session). Reversal risk is
        # managed where it belongs — strike distance, the premium/
        # price stops and the trend-flip exit — not by refusing the
        # trend-side ticket. Condors and unconfirmed leans keep the
        # block (directional threat for leans, total range for
        # condors).
        _dm_px = signals.get("price_regime", "")
        _dm_trend_confirmed = (
            (final_regime == "PREMIUM_SELL_BEAR" and _dm_px in ("DOWNTREND", "STRONG_DOWNTREND"))
            or (final_regime == "PREMIUM_SELL_BULL" and _dm_px in ("UPTREND", "STRONG_UPTREND"))
        )
        # ── PATCH_V15: the range verdict is judged on RANGE, not speed ───
        # The time-scaled gauge above says how fast the session has moved
        # for the clock; that is the right question for a directional
        # vertical (an exhausted with-trend move is its thesis) but the
        # wrong one for a delta-neutral condor. What kills a condor is the
        # day's RANGE running beyond the range the straddle priced for the
        # WHOLE session - a front-loaded morning that then goes nowhere is
        # the condor's best tape, not its worst. Measured 2026-09-16: the
        # gauge sat at 126-156% from 09:45 to 10:18 and refused the
        # session, while the realised range (165pts) was 52% of the 319pt
        # the 4-day 330.7 straddle priced for the day. Reconstructed from
        # the same two fields the gauge itself is built from, so the
        # exemption can never disagree with the metric that raised it.
        _dm_range_confirmed = False
        if final_regime == "PREMIUM_SELL_RANGE":
            try:
                _dm_es = float(signals.get("expected_range_so_far_pts") or 0.0)
                _dm_os = float(signals.get("opening_straddle_pts") or 0.0)
                _dm_dte_v15 = int(actual_dte or 0)
                _dm_range_pts = (
                    day_move_used / 100.0 * _dm_es if _dm_es > 0 else 0.0
                )
                _dm_theta_v15 = (
                    (1.0 / max(_dm_dte_v15, 1)) ** 0.5
                    if _dm_dte_v15 >= 2 else 1.0
                )
                _dm_full_ref = _dm_os * _dm_theta_v15 * 1.93
                _dm_frac_range = (
                    _dm_range_pts / _dm_full_ref if _dm_full_ref > 0 else 9.9
                )
            except (TypeError, ValueError):
                _dm_frac_range = 9.9
            _dm_range_confirmed = (
                _dm_px in ("RANGE", "STRONG_RANGE")
                and 0.0 <= float(signals.get("adx_15") or 0.0)
                < float(self.config.adx_trend_threshold)
                and _dm_frac_range < float(
                    getattr(self.config, "day_range_frac_block_condor", 0.75))
            )
        # PATCH_V25: only the failed-LOW bull put may ignore day-move
        # (the flush IS the thesis). Without this, 16-Sep is blocked for
        # ~189 cycles at day_move ~210% and only the 10:58 condor fires.
        # Failed-HIGH bear calls keep the 125% block (17-Sep day_up 209%).
        if bool(signals.get("neutral_range_vertical")) and str(
                signals.get("final_regime") or "") == "PREMIUM_SELL_BULL":
            _dm_range_confirmed = True
        if bool(signals.get("afternoon_high_fade")
                or signals.get("afternoon_low_fade")):
            _dm_range_confirmed = True
        if _dm_threat >= self.config.day_move_used_block_pct and not (
            _dm_trend_confirmed or _dm_range_confirmed
        ):
            return "NO_TRADE", (
                f"day_move_used_{_dm_threat:.0f}pct_of_opening_straddle_no_edge"
            )

        try:
            hard_exit = datetime.strptime(
                state.get("hard_exit_time", "15:00"), "%H:%M"
            ).time()
        except Exception:
            hard_exit = self.config.hard_exit_time

        mins_to_exit = self._minutes_to_time(current_time, hard_exit)
        if mins_to_exit < 90:
            return "NO_TRADE", (
                f"only_{mins_to_exit:.0f}min_before_hard_exit_need_90"
            )

        if signals.get("event_day") and self.config.defined_risk_only_on_event:
            self.logger.info(
                f"Event day trade: {signals.get('event_name', '')} — defined risk only"
            )

        or_condition = signals.get("or_condition", "MODERATE")
        if or_condition in ("WIDE", "VERY_WIDE"):
            # PATCH_V12: a wide opening range bans the DELTA-NEUTRAL
            # condor, not the trend-side vertical. Selling calls
            # above a confirmed breakdown (or puts below a breakout)
            # is how a wide-range trend day is harvested; the blanket
            # ban left 2026-09-15 untradeable by every route.
            _px = signals.get("price_regime", "")
            _trend_side_ok = (
                (final_regime == "PREMIUM_SELL_BEAR" and _px in ("DOWNTREND", "STRONG_DOWNTREND"))
                or (final_regime == "PREMIUM_SELL_BULL" and _px in ("UPTREND", "STRONG_UPTREND"))
            )
            if not signals.get("gap_fade_opportunity") and not _trend_side_ok:
                return "NO_TRADE", f"wide_or_{or_condition}_dangerous_to_sell_premium"

        return None

    def _session_range_pos(self, signals: dict) -> Tuple[float, float, float, float]:
        """Return (range, location 0-1, high, low) for two-way mean-reversion.

        Location 1.0 = at the session high, 0.0 = at the session low.
        Falls back to opening-range extremes when candle max/min have not
        been published yet — the 17-Sep 12:15 bull-put fired because the
        regime tree never saw a 160pt range and sold the trend label.
        """
        try:
            spot = float(signals.get("spot") or 0.0)
        except (TypeError, ValueError):
            spot = 0.0
        highs, lows = [], []
        for key in ("day_high_so_far", "or_high", "day_high"):
            try:
                v = float(signals.get(key) or 0.0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                highs.append(v)
        for key in ("day_low_so_far", "or_low", "day_low"):
            try:
                v = float(signals.get(key) or 0.0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                lows.append(v)
        if spot > 0:
            highs.append(spot)
            lows.append(spot)
        if not highs or not lows:
            return 0.0, 0.5, 0.0, 0.0
        hi, lo = max(highs), min(lows)
        rng = hi - lo
        if rng < 1.0:
            return 0.0, 0.5, hi, lo
        pos = min(max((spot - lo) / rng, 0.0), 1.0)
        return rng, pos, hi, lo

    def _apply_two_way_location(self, signals: dict, current_time: dtime) -> None:
        """On a two-way tape, sell the tested extreme — not the trend label.

        17-Sep chart: 10:15 high, 11:00 low, 12:30 higher high, 13:30 dump.
        A weekly condor cannot harvest that; a bull put at the high is the
        wrong side. After a failed-break scalp the mid-range condor is the
        trade (16-Sep) — do not steal it with a high fade before lunch.
        """
        if bool(signals.get("event_day")):
            return
        if str(signals.get("vol_regime") or "") in ("ABORT", "BUY_OPTIONS"):
            return
        try:
            dte = int(signals.get("actual_dte")) if signals.get("actual_dte") is not None else -1
        except (TypeError, ValueError):
            dte = -1
        if not (0 <= dte <= 4):
            return
        rng, pos, _, _ = self._session_range_pos(signals)
        if rng < 100.0:
            return
        after_fb = bool(self.market_engine.state.get("last_exit_is_failed_break_scalp"))
        if after_fb and current_time < dtime(12, 15):
            return
        if current_time >= dtime(12, 15) and current_time <= dtime(14, 0) and pos >= 0.80:
            signals["afternoon_high_fade"] = True
            signals["final_regime"] = "PREMIUM_SELL_BEAR"
            signals["weekly_range_size_discount"] = 0.90
            return
        if current_time >= dtime(10, 50) and current_time < dtime(12, 15) and pos <= 0.22:
            signals["afternoon_low_fade"] = True
            signals["final_regime"] = "PREMIUM_SELL_BULL"
            signals["weekly_range_size_discount"] = 0.90

    def _map_regime_to_strategy(
        self,
        signals: dict,
        _test_time: Optional[dtime] = None,
    ) -> Tuple[str, str]:
        current_time  = _test_time if _test_time is not None else now_ist().time()
        self._apply_two_way_location(signals, current_time)
        final_regime  = signals.get("final_regime", "NO_TRADE")
        confidence    = signals.get("confidence_level", "NONE")
        dte           = signals.get("actual_dte")
        or_condition  = signals.get("or_condition", "MODERATE")
        adx_15        = float(signals.get("adx_15") or 0.0)
        adx_15_mature = bool(signals.get("adx_15_mature", False))
        vol_regime    = signals.get("vol_regime", "NEUTRAL")

        if final_regime == "PREMIUM_SELL_RANGE":
            strategy = self._resolve_range_strategy(
                dte, or_condition, adx_15, adx_15_mature,
                current_time, vol_regime, signals,
            )
            reason = (
                f"regime:{final_regime}:conf={confidence}:"
                f"dte={dte}:or={or_condition}:adx={adx_15:.0f}"
            )
            if strategy == BEAR_CALL_SPREAD:
                _, _lean_why = self._range_day_bearish_lean(signals)
                reason += f":{_lean_why}"
            return strategy, reason

        if final_regime == "PREMIUM_SELL_BULL":
            # ── PATCH_V13: the day's own structure vetoes selling the
            # downside. A gap-down that has NOT been filled, with spot still
            # under the previous close and a call wall above it, is a heavy
            # tape: an intraday rally inside that structure is a bounce, and
            # selling puts into it puts the short strike exactly where the
            # day's remaining risk lives. The engine already leans bearish
            # off this structure inside a RANGE regime (see
            # _range_day_bearish_lean); a UPTREND classification is a
            # 15-minute read of the same tape and must not be allowed to
            # flip the book to the other side of it. Measured 2026-09-09:
            # the midday rally printed PREMIUM_SELL_BULL at 12:36 with the
            # gap-down unfilled (day high 23,571 vs prev close 23,635) and
            # spot 77pts under the close; the flat engine's next ticket was
            # a bull put, and the tape fell 130pts from there into the bell.
            # Standing aside is not a directional bet - it is refusing to
            # sell the side of the book the day's structure contradicts.
            _ds_ok, _ds_why = self._day_structure_bearish(signals)
            if _ds_ok and not signals.get("afternoon_low_fade"):
                return "NO_TRADE", (
                    f"day_structure_contradicts_bull_premium:{_ds_why}"
                )
            if not signals.get("afternoon_low_fade"):
                _tw_rng, _tw_loc, _, _ = self._session_range_pos(signals)
                if _tw_rng >= 100.0 and _tw_loc >= 0.70:
                    return "NO_TRADE", (
                        f"two_way_wait_no_puts_at_high_{_tw_loc:.2f}"
                    )
            reason = (
                f"regime:{final_regime}:conf={confidence}:"
                f"dte={dte}:adx={adx_15:.0f}:"
                f"price={signals.get('price_regime')}"
            )
            return BULL_PUT_SPREAD, reason

        if final_regime == "PREMIUM_SELL_BEAR":
            reason = (
                f"regime:{final_regime}:conf={confidence}:"
                f"dte={dte}:adx={adx_15:.0f}:"
                f"price={signals.get('price_regime')}"
            )
            return BEAR_CALL_SPREAD, reason

        return "NO_TRADE", f"no_strategy_for_regime:{final_regime}"

    def _day_structure_bearish(self, signals: dict) -> Tuple[bool, str]:
        """The session's structural facts, independent of any regime read.

        All of these are slow, measurable properties of the day rather than
        of the last fifteen minutes: the engine's own gap classification,
        whether that gap has been filled, where spot sits against the
        previous close, and whether there is real call-side open interest
        above spot to sell into. Regime classifications flicker cycle to
        cycle (RANGE -> UPTREND -> RANGE inside twenty minutes on
        2026-09-09); these do not, which is exactly why they are allowed to
        arbitrate structure selection.
        """
        if signals.get("gap_direction") != "DOWN":
            return False, "structure_needs_down_gap"
        try:
            _pc = float(signals.get("prev_close") or 0.0)
            _dh = float(signals.get("day_high") or 0.0)
            _sp = float(signals.get("spot") or 0.0)
        except (TypeError, ValueError):
            return False, "structure_day_unknown"
        if _pc <= 0 or _dh <= 0 or _sp <= 0:
            return False, "structure_day_unknown"
        if _dh >= _pc:
            return False, "structure_gap_filled"
        if _sp >= _pc:
            return False, "structure_spot_reclaimed_prev_close"
        try:
            _rw = float(signals.get("resistance_strike") or 0.0)
            _rs = float(signals.get("resistance_strength") or 0.0)
        except (TypeError, ValueError):
            return False, "structure_no_call_wall"
        if _rw <= _sp:
            return False, "structure_call_wall_not_above_spot"
        if _rs < 2.0:
            return False, "structure_call_wall_too_weak"
        return True, (
            f"gap_down_unfilled_dh={_dh:.0f}_pc={_pc:.0f}_"
            f"wall={_rw:.0f}x{_rs:.1f}"
        )

    def _range_day_bearish_lean(self, signals: dict) -> Tuple[bool, str]:
        """Day-structure lean: heavy tape inside a range regime.

        A range regime with an UNFILLED gap-down is not a symmetric range:
        price probed the top of the opening range and was rejected back
        under the previous close, so the put side of a condor fights
        gravity while the call side collects it. Measured 2026-09-09: the
        condor scratched (+26/lot) while its own call side printed +678/lot
        and the put side lost -563/lot. When every condition below holds,
        the range resolution sells the bear-call spread instead of the
        condor — a SELECTION substitution only; every gate (entry rules,
        wing cost, credit ratio, EV, sizing, pre-trade) still applies.

        All required: RANGE price regime, non-bullish positioning, the
        engine's own DOWN gap (0.4%+) still unfilled with spot heavy under
        the previous close right now, and a real call-side OI wall above
        spot to sell into.

        v1: no single-sided gap fade on Fridays (DTE 2). The lean was
        measured on a fresh-weekly (DTE 4) gap day; into the weekend the
        gap-day tape is dominated by weekly expiry positioning and the
        directional fade has no edge (measured 2026-09-11: forcing the
        bear call off this lean loses ~Rs 1,000 into the data end).
        Friday range premium is harvested delta-neutral only.
        """
        # PATCH_V14: this exemption is about WEEKEND RISK - the last session
        # before the market shuts for two or more days, when a gap-day tape is
        # dominated by weekly expiry positioning and a single-sided directional
        # fade has no edge. It was written as `actual_dte == 2` because Friday
        # happens to be DTE 2 in a clean Tuesday-expiry week, which makes it a
        # weekday rule wearing a DTE costume. In the recorded week of
        # 2026-09-14 (Monday, an NSE holiday) the counter shifted: Friday
        # 11-Sep became DTE 1 and the exemption switched ITSELF OFF on the one
        # session it had been measured on, while Thursday 10-Sep became DTE 2
        # and inherited an exemption that was never written for it. Measured on
        # the five recorded sessions, restoring the guard on that Friday is
        # worth +Rs 250 and removes the week's only losing trade: the lean
        # substituted a bear call at 10:14 into a CPI rally, the v12 trend-flip
        # exit ejected it 55 minutes later at -Rs 250, and the condor the
        # guard would have kept was refused on its own economics - leaving the
        # book flat and free for the 11:12 long call that earned the session.
        try:
            _weekend_risk = ExpiryCalendar.is_weekend_risk_day(today_ist())
        except Exception:
            _weekend_risk = False
        if _weekend_risk or signals.get("day_label") == "FRIDAY" \
                or signals.get("actual_dte") == 2:
            return False, "lean_skipped_weekend_risk_delta_neutral_only"
        if signals.get("price_regime") != "RANGE":
            return False, "lean_needs_range_price"
        # PATCH_V13: positioning that is UNCLEAR is still a veto here, and
        # deliberately so. Relaxing it was measured, not assumed: on
        # 2026-09-11 the lean then fired at 10:01 off a VERY_NARROW opening
        # range with an immature ADX and an UNCLEAR OI read, thirteen minutes
        # earlier and Rs 333 worse than the entry the confirmed bearish read
        # produced on its own. The lean is a tie-breaker for a RANGE regime
        # whose positioning evidence has gone quiet, not a licence to sell
        # the downside on a gap day before the tape has said anything. What
        # PATCH_V13 does change is that the STRUCTURAL half of this test now
        # lives in _day_structure_bearish(), where the same facts also veto
        # selling puts into an unfilled gap-down (see _map_regime_to_strategy)
        # - one definition of the day's structure, used by both routes.
        if signals.get("positioning_regime") not in ("RANGE", "BEARISH"):
            return False, "lean_blocked_by_bullish_positioning"
        _ds_ok, _ds_why = self._day_structure_bearish(signals)
        if not _ds_ok:
            return False, f"lean_{_ds_why}"
        return True, f"day_structure_lean_bearish:{_ds_why}"

    def _resolve_range_strategy(
        self,
        dte:           Optional[int],
        or_condition:  str,
        adx_15:        float,
        adx_15_mature: bool,
        current_time:  dtime,
        vol_regime:    str,
        signals:       dict,
    ) -> str:
        if dte != 0:
            _lean, _lean_reason = self._range_day_bearish_lean(signals)
            if _lean:
                self.logger.info(f"Range resolution: {_lean_reason}")
                return BEAR_CALL_SPREAD
            return IRON_CONDOR
        if (or_condition in ("VERY_NARROW", "NARROW") and
                adx_15 < 20 and
                current_time < dtime(12, 0)):
            spot       = float(signals.get("spot") or 0)
            atm_strike = int(signals.get("atm_strike") or 0)
            if atm_strike > 0 and abs(spot - atm_strike) < 50:
                return IRON_BUTTERFLY
        _lean0, _lean_reason0 = self._range_day_bearish_lean(signals)
        if _lean0:
            self.logger.info(f"Range resolution: {_lean_reason0}")
            return BEAR_CALL_SPREAD
        return IRON_CONDOR

    # ═══════════════════════════════════════════════════════════════════
    # PATCH_V13: tape evidence, entry/exit symmetry, session price memory
    # ═══════════════════════════════════════════════════════════════════
    def _note_price(self, signals: dict) -> None:
        """Keep a rolling session price memory for the closing-hour route.

        decide() is called on every cycle the engine is allowed to act, so
        this is the same series in live and in replay. It is trimmed to the
        session and to the lookback the closing-hour gate needs, so it stays
        a few hundred tuples.
        """
        try:
            spot = float(signals.get("spot") or 0.0)
        except (TypeError, ValueError):
            return
        if spot <= 0:
            return
        hist = getattr(self, "_v13_price_hist", None)
        if hist is None:
            hist = []
            self._v13_price_hist = hist
        now = now_ist()
        hist.append((now, spot))
        lookback = float(getattr(
            self.config, "momentum_late_extreme_lookback_min", 45)) + 30.0
        cut = now - timedelta(minutes=lookback)
        today = now.date()
        while hist and (hist[0][0] < cut or hist[0][0].date() != today):
            hist.pop(0)
        if len(hist) > 2000:
            del hist[:-2000]

    def _trend_evidence(self, signals: dict) -> Tuple[int, float, bool, float]:
        """The smoothed directional read of the tape.

        Returns (direction, adx, adx_mature, vwap_dist_pct) with direction
        +1 up, -1 down, 0 no evidence. Evidence is an OR of three
        independent reads the engine already computes - the price regime
        classification, the 15-minute EMA structure, and displacement from
        VWAP - because any one of them flickers on its own (measured
        2026-09-09: price_regime went RANGE -> UPTREND -> RANGE -> UPTREND
        four times in ninety minutes while spot went nowhere, and
        ema_structure flipped TRANSITIONAL for single cycles inside a
        sustained move). Three reads agreeing is a trend; one of three
        firing on one cycle is noise.
        """
        try:
            adx = float(signals.get("adx_15") or 0.0)
        except (TypeError, ValueError):
            adx = 0.0
        mature = bool(signals.get("adx_15_mature", False))
        price  = str(signals.get("price_regime") or "")
        ema    = str(signals.get("ema_structure") or "")
        try:
            vd = float(signals.get("vwap_dist_pct") or 0.0)
        except (TypeError, ValueError):
            vd = 0.0
        buf = abs(float(getattr(
            self.config, "counter_trend_vwap_dist_min_pct", 0.10)))
        up = (
            price in ("UPTREND", "STRONG_UPTREND")
            or (ema == "BULLISH" and vd >= buf)
        )
        dn = (
            price in ("DOWNTREND", "STRONG_DOWNTREND")
            or (ema == "BEARISH" and vd <= -buf)
        )
        if up and not dn:
            return 1, adx, mature, vd
        if dn and not up:
            return -1, adx, mature, vd
        return 0, adx, mature, vd

    def _tape_displacement(self, signals: dict) -> Optional[dict]:
        """Latch the trend read so one cycle cannot flip the book.

        A regime read that is re-derived from scratch every fifteen seconds
        produces a different structure every fifteen seconds. The read is
        therefore held for displaced_tape_hold_min minutes once established,
        dropped the moment the tape asserts the opposite direction, and
        expired by the clock otherwise.
        """
        cfg   = self.config
        state = self.market_engine.state
        now   = now_ist()
        direction, adx, mature, vd = self._trend_evidence(signals)
        hold = float(getattr(cfg, "displaced_tape_hold_min", 10))
        _adx_trend = float(getattr(cfg, "adx_trend_threshold", 20.0))

        latch = state.get("tape_displacement")
        if isinstance(latch, dict):
            try:
                until = datetime.fromisoformat(str(latch.get("until")))
            except Exception:
                until = now
            if now > until or int(latch.get("dir", 0)) == 0:
                latch = None
            elif direction != 0 and direction != int(latch.get("dir", 0)):
                latch = None          # the tape contradicts it: drop at once
        else:
            latch = None

        if direction != 0 and mature and adx >= _adx_trend:
            latch = {
                "dir":        direction,
                "adx":        round(adx, 2),
                "vwap_dist":  round(vd, 4),
                "price":      str(signals.get("price_regime") or ""),
                "ema":        str(signals.get("ema_structure") or ""),
                "since":      now.isoformat(),
                "until":      (now + timedelta(minutes=hold)).isoformat(),
            }
        state["tape_displacement"] = latch
        return latch

    def _counter_trend_entry_refusal(
        self,
        strategy_name: str,
        signals:       dict,
    ) -> Optional[str]:
        """Refuse to OPEN what the exit ladder is built to eject.

        The v12 trend-flip exit closes a credit vertical that a measured
        trend has run against, at a loss, by design. Opening one is the same
        trade entered from the wrong side of it: the entry pays the spread,
        the ladder ejects it, and the round trip is the P&L. Symmetric
        structures are refused on the same evidence only when the
        displacement is strong - a condor is a range trade and a tape 0.10%+
        off VWAP with a mature ADX at or above the strong threshold is not
        ranging, so one of its two shorts is being tested from the first
        cycle.
        """
        cfg = self.config
        if not bool(getattr(cfg, "counter_trend_entry_block", True)):
            return None
        # PATCH_V26: fading a two-way day-high IS selling into a measured
        # uptrend label. The latch was written to stop 17-Sep 09:46 BCS
        # into a first poke; the afternoon extreme is the opposite trade.
        if signals.get("afternoon_high_fade") or signals.get("afternoon_low_fade"):
            return None
        latch = self._tape_displacement(signals)
        if not latch:
            return None
        try:
            d    = int(latch.get("dir", 0))
            ladx = float(latch.get("adx", 0.0))
            lvd  = float(latch.get("vwap_dist", 0.0))
        except (TypeError, ValueError):
            return None
        if d == 0:
            return None
        _side = "uptrend" if d > 0 else "downtrend"
        _tag = f"measured_{_side}_adx_{ladx:.0f}_vwap_{lvd:+.2f}pct"

        if strategy_name == BEAR_CALL_SPREAD and d > 0:
            return f"counter_trend_entry_blocked:{strategy_name}:{_tag}"
        if strategy_name == BULL_PUT_SPREAD and d < 0:
            return f"counter_trend_entry_blocked:{strategy_name}:{_tag}"
        if strategy_name in (IRON_CONDOR, IRON_BUTTERFLY):
            _strong = float(getattr(cfg, "displaced_tape_adx_min",
                                    getattr(cfg, "adx_strong_threshold", 28.0)))
            _buf = abs(float(getattr(cfg, "counter_trend_vwap_dist_min_pct", 0.10)))
            if ladx >= _strong and abs(lvd) >= _buf:
                return (
                    f"displaced_tape_no_symmetric_structure:{strategy_name}:"
                    f"adx_{ladx:.0f}_ge_{_strong:.0f}:vwap_{lvd:+.2f}pct"
                )
        return None

    def _in_late_momentum_window(self, cur: dtime) -> bool:
        """True when the closing-hour route owns the clock.

        Starts after the sell side's last entry so the two routes can never
        compete for a cycle; ends at the configured cut, and independently
        at hard_exit - momentum_late_min_minutes_left, which keeps the rule
        correct on a Tuesday's 15:00 square-off without a second clock.
        """
        cfg   = self.config
        state = self.market_engine.state
        if not bool(getattr(cfg, "momentum_late_enabled", True)):
            return False
        if not bool(getattr(cfg, "momentum_enabled", True)):
            return False
        try:
            start = datetime.strptime(
                str(getattr(cfg, "momentum_late_window_start", "14:30")),
                "%H:%M").time()
            end = datetime.strptime(
                str(getattr(cfg, "momentum_late_window_end", "14:57")),
                "%H:%M").time()
        except Exception:
            return False
        try:
            hard_exit = datetime.strptime(
                state.get("hard_exit_time", "15:00"), "%H:%M").time()
        except Exception:
            hard_exit = cfg.hard_exit_time
        _min_left = float(getattr(cfg, "momentum_late_min_minutes_left", 25))
        try:
            _last = (
                datetime.combine(date.today(), hard_exit)
                - datetime.combine(date.today(), end)
            ).total_seconds() / 60.0
            if _last < _min_left:
                end = (
                    datetime.combine(date.today(), hard_exit)
                    - timedelta(minutes=_min_left)
                ).time()
        except Exception:
            pass
        return start <= cur <= end

    def _late_fresh_extreme(self, signals: dict, direction: int) -> Tuple[bool, str]:
        """The closing-hour trend must still be making ground NOW.

        Primary test: spot beyond the extreme of the last
        momentum_late_extreme_lookback_min minutes of the session, excluding
        the current print (a level equal to the print that set it is not a
        breakout). Fallback, used only when the price memory is shorter than
        momentum_late_extreme_min_span_min - the replay harness calls decide()
        only on cycles where the book is flat, so its memory starts when the
        last position closed - : spot inside
        momentum_late_range_proximity_frac of the session range from the
        extreme it is attacking. Both tests ask the same question of the tape
        and agree on every recorded session.
        """
        cfg  = self.config
        try:
            spot = float(signals.get("spot") or 0.0)
        except (TypeError, ValueError):
            spot = 0.0
        if spot <= 0:
            return False, "late_no_spot"
        lookback = float(getattr(cfg, "momentum_late_extreme_lookback_min", 45))
        min_span = float(getattr(cfg, "momentum_late_extreme_min_span_min", 30))
        now  = now_ist()
        cut  = now - timedelta(minutes=lookback)
        hist = getattr(self, "_v13_price_hist", None) or []
        win  = [(t, s) for (t, s) in hist if cut <= t < now]
        span = 0.0
        if len(win) >= 2:
            span = (win[-1][0] - win[0][0]).total_seconds() / 60.0
        if span >= min_span:
            ref = min(s for _, s in win) if direction < 0 \
                else max(s for _, s in win)
            ok  = spot < ref if direction < 0 else spot > ref
            return ok, (
                f"late_fresh_extreme_{lookback:.0f}min_ref_{ref:.2f}_"
                f"spot_{spot:.2f}_span_{span:.0f}min"
            )
        try:
            dh = float(signals.get("day_high") or 0.0)
            dl = float(signals.get("day_low") or 0.0)
        except (TypeError, ValueError):
            dh = dl = 0.0
        rng = dh - dl
        if dh > 0 and dl > 0 and rng > 0:
            frac = float(getattr(cfg, "momentum_late_range_proximity_frac", 0.30))
            if direction < 0:
                ok  = spot <= dl + frac * rng
                ref = dl + frac * rng
            else:
                ok  = spot >= dh - frac * rng
                ref = dh - frac * rng
            return ok, (
                f"late_range_proximity_ref_{ref:.2f}_spot_{spot:.2f}_"
                f"span_{span:.0f}min"
            )
        return False, "late_no_extreme_reference"

    def _validate_entry_rules(
        self,
        strategy_name: str,
        signals:       dict,
        _test_time:    Optional[dtime] = None,
    ) -> Tuple[bool, str]:
        state        = self.market_engine.state
        current_time = _test_time if _test_time is not None else now_ist().time()
        spot         = float(signals.get("spot") or 0)
        adx_15       = float(signals.get("adx_15") or 0)
        dte          = signals.get("actual_dte")

        if strategy_name == IRON_BUTTERFLY:
            atm_strike = int(signals.get("atm_strike") or 0)
            if atm_strike > 0 and abs(spot - atm_strike) > 50:
                return False, f"butterfly_spot_too_far_from_atm_{atm_strike:.0f}"
            if dte not in (0, 1):
                return False, f"butterfly_requires_dte_0_or_1_not_{dte}"
            if dte == 0 and current_time >= dtime(12, 0):
                return False, "butterfly_too_late_after_12:00_on_0dte"
            if adx_15 > 22:
                return False, f"butterfly_blocked_adx_{adx_15:.0f}_needs_flat_below_22"
            if signals.get("or_condition", "MODERATE") not in ("VERY_NARROW", "NARROW", "MODERATE"):
                return False, (
                    f"butterfly_requires_moderate_or_better_not_{signals.get('or_condition')}"
                )

        elif strategy_name == IRON_CONDOR:
            try:
                hard_exit = datetime.strptime(
                    state.get("hard_exit_time", "15:00"), "%H:%M"
                ).time()
            except Exception:
                hard_exit = self.config.hard_exit_time
            mins     = self._minutes_to_time(current_time, hard_exit)
            min_mins = 75 if dte == 0 else 90
            if mins < min_mins:
                return False, (
                    f"condor_needs_{min_mins}min_before_exit_only_{mins:.0f}min"
                )
            if adx_15 >= self.config.adx_strong_threshold:
                return False, f"condor_blocked_strong_adx_{adx_15:.0f}"
            if signals.get("or_condition") == "VERY_WIDE":
                return False, "condor_blocked_very_wide_or"
            # PATCH_V26: a two-way weekly tape does not pin.
            # A 100pt+ session may sell a weekly condor ONLY as a
            # follow-up after a confirmed failed-break scalp (16-Sep).
            # First-print condors (17-Sep 10:35 ~Rs 650) and condors
            # sandwiched after a two-way low fade (17-Sep 11:57 scratch)
            # sit in an expanding auction and cap/kill the day.
            try:
                _ic_dte = int(dte) if dte is not None else -1
            except (TypeError, ValueError):
                _ic_dte = -1
            if _ic_dte >= 2:
                _ic_rng, _ic_loc, _, _ = self._session_range_pos(signals)
                if _ic_rng >= 100.0:
                    _ic_fb = bool(state.get("last_exit_is_failed_break_scalp"))
                    if not _ic_fb:
                        return False, (
                            f"weekly_condor_blocked_two_way_"
                            f"{_ic_rng:.0f}pts_no_failed_break"
                        )
                    if 0.22 < _ic_loc < 0.80:
                        return False, (
                            f"weekly_condor_blocked_two_way_mid_"
                            f"{_ic_rng:.0f}pts_loc_{_ic_loc:.2f}"
                        )

        elif strategy_name == BULL_PUT_SPREAD:
            or_high = float(signals.get("or_high") or 0)
            or_low  = float(signals.get("or_low") or 0)
            # PATCH_V15: the failed-break vertical confirmed itself by
            # RECLAIMING the range low, so the OR-mid veto is satisfied by
            # a different measurement than the one it was written for.
            if signals.get("afternoon_low_fade"):
                return True, "entry_rules_passed"
            if or_high > 0 and or_low > 0 and not signals.get(
                    "neutral_range_vertical"):
                or_mid    = (or_high + or_low) / 2.0
                or_buffer = 30 if dte == 0 else 15
                if spot < or_mid - or_buffer:
                    return False, (
                        f"bull_put_spot_{spot:.0f}_below_or_mid_{or_mid:.0f}"
                        f"_by_{or_mid - spot:.0f}pts"
                    )
            vwap = signals.get("vwap")
            # PATCH_V25: failed-break reclaim IS a bounce through lagging VWAP.
            if (vwap and vwap > 0 and spot < vwap - 30
                    and not signals.get("neutral_range_vertical")):
                return False, (
                    f"bull_put_spot_below_vwap_{vwap:.0f}_by_{vwap - spot:.0f}pts"
                )

        elif strategy_name == BEAR_CALL_SPREAD:
            or_high = float(signals.get("or_high") or 0)
            or_low  = float(signals.get("or_low") or 0)
            # The OR-mid veto is counter-trend-bounce protection: in a
            # DOWNTREND regime, spot bouncing back above mid-range means
            # wait for the bounce to fail. In a RANGE regime the ORB
            # classifier already ruled "no breakout" — re-litigating with a
            # dumber threshold double-jeopardies the trade and vetoes the
            # best mean-reversion entries (top of a narrow range on a heavy
            # tape is exactly where the day-structure lean sells calls into
            # an OI wall). It is incoherent anyway: the condor this lean
            # replaces contains the SAME short call with no such veto.
            # Behaviour on the trend path (PREMIUM_SELL_BEAR) is unchanged.
            _px_regime = signals.get("price_regime", "")
            if signals.get("afternoon_high_fade"):
                return True, "entry_rules_passed"
            if (_px_regime in ("DOWNTREND", "STRONG_DOWNTREND")
                    and or_high > 0 and or_low > 0):
                or_mid    = (or_high + or_low) / 2.0
                or_buffer = 30 if dte == 0 else 15
                if spot > or_mid + or_buffer:
                    return False, (
                        f"bear_call_spot_{spot:.0f}_above_or_mid_{or_mid:.0f}"
                        f"_by_{spot - or_mid:.0f}pts"
                    )
            max_pain = float(signals.get("max_pain") or 0)
            # PATCH_V12 (round 2): pin risk is a range-tape
            # phenomenon. A tape printing a confirmed downtrend is
            # TRENDING THROUGH max pain, not pinning to it (measured
            # 2026-09-15: STRONG_DOWNTREND, mature ADX 33, spot
            # falling through 23350 — the veto blocked the
            # trend-side vertical for 20 minutes mid-trend).
            _mp_px = signals.get("price_regime", "")
            _mp_trend_through = _mp_px in ("DOWNTREND", "STRONG_DOWNTREND")
            if max_pain > 0 and abs(spot - max_pain) < 25 and not _mp_trend_through:
                return False, (
                    f"bear_call_spot_within_25pts_of_max_pain_{max_pain:.0f}"
                )

        return True, "entry_rules_passed"

    def _select_strikes(
        self,
        strategy_name: str,
        chain:         dict,
        spot:          float,
        dte:           Optional[int],
        signals:       dict,
        _test_time:    Optional[dtime] = None,
    ) -> Tuple[Optional[List[dict]], Optional[str]]:
        step             = self.config.nifty_strike_step
        opening_straddle = float(signals.get("opening_straddle_pts") or 0)
        _max_pain = float(signals.get("max_pain") or 0)
        _center_ref = spot
        if dte == 0 and _max_pain > 0:
            _mp_gap = abs(_max_pain - spot)
            _em_mp = float(signals.get("expected_move_remaining_pts") or 0.0)
            # v3.9: max pain may anchor the strike centre only when it is
            # plausibly reachable inside the expected REMAINING move. A
            # pin 46 points away with ~80 points of remaining EM is an
            # end-of-day magnet, not a centre to sell around from midday;
            # centering there shifted every short one strike further from
            # the money than delta selection asked (measured 2026-09-08
            # 12:03: delta target 0.22 was the only branch left, credit
            # ~10 points, structurally rejected all afternoon).
            _mp_reach = 0.45 * _em_mp if _em_mp > 10 else 120.0
            if _mp_gap <= min(120.0, _mp_reach):
                _center_ref = _max_pain
        adx_15           = float(signals.get("adx_15") or 0)
        vix              = float(signals.get("vix") or 11.0)

        if dte == 0:
            now_t2 = _test_time if _test_time is not None else now_ist().time()
            total_mins2   = 375.0
            elapsed_mins2 = max(0.0, (
                datetime.combine(today_ist(), now_t2) -
                datetime.combine(today_ist(), dtime(9, 15))
            ).total_seconds() / 60.0)
            remaining_frac2 = max(
                (total_mins2 - elapsed_mins2) / total_mins2, 0.05
            )
            time_mult2 = math.sqrt(remaining_frac2)
            dist_mult  = 1.3 * time_mult2
            floor_pts  = max(int(150 * time_mult2), 70)
        elif dte == 1:
            dist_mult, floor_pts = 1.05, 150
        elif dte == 2:
            dist_mult, floor_pts = 0.80, 130
        elif dte == 3:
            dist_mult, floor_pts = 0.68, 120
        elif dte == 4:
            dist_mult, floor_pts = 0.58, 110
        elif dte == 5:
            dist_mult, floor_pts = 0.50, 100
        else:
            dist_mult, floor_pts = 0.45, 95

        if adx_15 >= self.config.adx_strong_threshold:
            dist_mult *= 1.20
            floor_pts  = int(floor_pts * 1.10)

        # ── v3.2: delta-primary strike selection ──────────────────────
        # v3.1 placed the shorts at 1.3 * sqrt(time-left) * the OPENING
        # straddle and only used delta if that failed. At 10:30 that is
        # ~1.16x the straddle, roughly 1.4 sigma, delta ~0.08. The credit
        # available there cannot clear the engine's own credit/wing gate,
        # so 0DTE structurally self-rejected all day with
        # "credit_ratio_below_min" - a silent, total loss of opportunity.
        #
        # Professionals place short premium by DELTA, because delta is
        # the probability-of-touch proxy the market itself is quoting,
        # and because equal DISTANCE on a skewed chain means unequal
        # RISK: the NIFTY put side is always richer, so a distance-
        # symmetric condor is structurally short delta. Each side is now
        # chosen by delta independently, then clamped into a sanity band
        # around the expected REMAINING move so a mis-quoted greek can
        # never put a strike somewhere absurd.
        # v4.2: fresh-weekly sessions (DTE >= 2) sell different shorts
        # depending on the STRUCTURE: a delta-neutral condor wants ~0.20
        # delta per side (a 0.31-0.42 delta symmetric book is short delta
        # and tripped the wing-cost gate - measured 2026-09-09/10), while a
        # directional vertical is a directional-expression spread that
        # desks conventionally sell at 0.28-0.32 delta on the favoured side,
        # using the other side's OI wall as the wall being sold into.
        _weekly_dte = bool(dte is not None and dte >= 2)
        _neutral_condor = strategy_name == IRON_CONDOR
        if _weekly_dte and _neutral_condor:
            if adx_15 >= self.config.adx_strong_threshold:
                delta_target = float(getattr(self.config,
                                             "short_delta_strong_weekly", 0.18))
            elif adx_15 >= self.config.adx_trend_threshold:
                delta_target = float(getattr(self.config,
                                             "short_delta_trend_weekly", 0.22))
            else:
                delta_target = float(getattr(self.config,
                                             "short_delta_flat_weekly", 0.24))
            # regime layer can force the wider strong-delta target on a
            # RANGE tape with elevated ADX (v4.2 wide condor)
            if signals.get("weekly_wide_condor"):
                delta_target = min(
                    delta_target,
                    float(getattr(self.config,
                                  "short_delta_strong_weekly", 0.18)),
                )
            if vix < 12.0:
                delta_target = max(delta_target - 0.01, 0.10)
            elif vix < 14.0:
                delta_target = max(delta_target - 0.01, 0.11)
        else:
            if adx_15 >= self.config.adx_strong_threshold:
                delta_target = float(getattr(self.config, "short_delta_strong", 0.15))
            elif adx_15 >= self.config.adx_trend_threshold:
                delta_target = float(getattr(self.config, "short_delta_trend", 0.18))
            else:
                delta_target = float(getattr(self.config, "short_delta_flat", 0.22))

            # The low-VIX delta shave is a 0DTE fast-gamma calibration;
            # on fresh weeklies the favoured-side vertical keeps the
            # canonical ~0.30 short delta.
            if not _weekly_dte:
                if vix < 12.0:
                    delta_target = max(delta_target - 0.02, 0.10)
                elif vix < 14.0:
                    delta_target = max(delta_target - 0.01, 0.11)

        # Expected remaining move: the market's own priced expectation for
        # what is left of the session (published by data_engine).
        _em = float(signals.get("expected_move_remaining_pts") or 0.0)
        if _em <= 10 and opening_straddle > 20:
            _now_em = _test_time if _test_time is not None else now_ist().time()
            _elapsed_em = max(0.0, (
                datetime.combine(today_ist(), _now_em) -
                datetime.combine(today_ist(), dtime(9, 15))
            ).total_seconds() / 60.0)
            _rem_em = min(max((375.0 - _elapsed_em) / 375.0, 0.04), 1.0)
            _scale_em = (
                math.sqrt(1.0 / (float(dte) + 1.0)) if (dte and dte > 0) else 1.0
            )
            _em = opening_straddle * _scale_em * math.sqrt(_rem_em)
        if _em <= 10 and spot > 0:
            _em = spot * 0.004

        # v4.2: the intraday-remaining-EM band is correct for 0DTE (the
        # option's life IS the rest of the session), but a DTE3/4 weekly
        # CONDOR short at ~0.18-0.24 delta sits ~0.9-1.2x the expiry-horizon
        # expected move away, and the session-remaining EM collapses toward
        # zero through the afternoon: at 13:00 on 2026-09-09 it was 70 pts,
        # so even a 2.10x intraday ceiling clamped the weekly shorts back to
        # 0.37 delta and re-tripped the wing-cost gate. Weekly condors are
        # therefore sanity-banded in the chain's OWN current ATM straddle
        # (the chain-implied expiry scale, roughly time-of-day invariant).
        # Weekly verticals keep the intraday band: their favoured-side
        # 0.30 delta short is an intraday-expression leg by design.
        if _weekly_dte and _neutral_condor:
            _wk_atm = min(chain.keys(), key=lambda k: abs(float(k) - float(spot)))
            _wk_qc = chain.get(float(_wk_atm), {}).get("call") or {}
            _wk_qp = chain.get(float(_wk_atm), {}).get("put") or {}
            _wk_straddle = 0.0
            if _wk_qc and _wk_qp:
                _wk_straddle = (
                    (float(_wk_qc.get("bid", 0)) + float(_wk_qc.get("ask", 0))
                     + float(_wk_qp.get("bid", 0)) + float(_wk_qp.get("ask", 0)))
                    / 2.0
                )
            _wk_scale = _wk_straddle if _wk_straddle > 20 else _em
            _band_lo = float(getattr(self.config,
                                     "em_band_lo_weekly", 0.55)) * _wk_scale
            _band_hi = float(getattr(self.config,
                                     "em_band_hi_condor_weekly", 1.35)) * _wk_scale
        elif _weekly_dte:
            _band_lo = float(getattr(self.config,
                                     "em_band_lo_weekly", 0.55)) * _em
            _band_hi = float(getattr(self.config,
                                     "em_band_hi_weekly", 2.10)) * _em
        else:
            _band_lo = float(getattr(self.config, "em_band_lo", 0.80)) * _em
            _band_hi = float(getattr(self.config, "em_band_hi", 1.35)) * _em
        # v3.7: the absolute floor is one strike step, not two.
        #
        # With v3.6's corrected expected move the EM-relative floor is
        # 0.80 * 82.1 = 65.7 points on a quiet 0DTE, but max(2 * step,
        # floor_pts // 2) is a hardcoded 100 and overrode it. Delta
        # selection asked for 95 points - strike 23750, delta 0.224,
        # its 0.20 target - and the floor pushed the short to 23800,
        # delta 0.138, halving the credit from 10.35 to 5.55 and putting
        # fixed brokerage at 22% of it. All 86 surviving candidates were
        # rejected by a constant rather than by economics.
        #
        # One step still stops a mis-quoted greek selling the money.
        # Beyond that, 0.80 * EM is the market-relative floor and delta
        # chooses the strike, which is what v3.2 said it would do.
        _band_lo = max(_band_lo, float(step))
        _band_hi = max(_band_hi, _band_lo + step)

        def _dist_from_delta(_opt_type: str) -> Optional[float]:
            _k = self._find_strike_by_delta(
                chain, _opt_type, delta_target, tolerance=0.12
            )
            if _k is None:
                return None
            _d = abs(float(_k) - float(_center_ref))
            return _d if _d > 0 else None

        def _clamp_band(_d: Optional[float]) -> Optional[float]:
            if _d is None:
                return None
            _d = min(max(_d, _band_lo), _band_hi)
            _d = round(_d / step) * step
            _d = min(max(_d, math.floor(_band_lo / step) * step),
                     math.ceil(_band_hi / step) * step)
            return max(_d, float(step))

        _straddle_dist = None
        if opening_straddle > 20:
            _straddle_dist = max(
                float(opening_straddle) * dist_mult, float(floor_pts)
            )

        _dist_call = _clamp_band(_dist_from_delta("call"))
        _dist_put  = _clamp_band(_dist_from_delta("put"))
        _fallback  = _clamp_band(_straddle_dist) if _straddle_dist else None
        if _fallback is None:
            _fallback = _clamp_band(_em)
        if _dist_call is None:
            _dist_call = _fallback
        if _dist_put is None:
            _dist_put = _fallback

        if _dist_call is None or _dist_put is None:
            short_dist = None
        else:
            short_dist = (int(_dist_call), int(_dist_put))

        # PATCH_V23: pin the failed-break short beyond failed extreme
        # plus two proximity bands so a retest cannot trip the 40-pt stop
        # (17-Sep 23350 was only 46 pts past the 23304 poke).
        if signals.get("neutral_range_vertical") and short_dist is not None:
            try:
                _fb_orw = max(
                    float(signals.get("or_high") or 0.0)
                    - float(signals.get("or_low") or 0.0),
                    1.0,
                )
                _spot_fb = float(signals.get("spot") or 0.0)
                _prox_fb = max(
                    float(getattr(self.config, "spot_proximity_pts", 40) or 40),
                    (_spot_fb * float(getattr(
                        self.config, "spot_proximity_pct", 0.0016))
                     if _spot_fb > 0 else 0.0),
                )
                # PATCH_V25: V18 cushion = 0.40*OR only. A 40pt prox
                # stack still left a thin 200-pt wing on 16-Sep.
                _fb_cush = max(float(step), 0.40 * _fb_orw)
                if strategy_name == BULL_PUT_SPREAD:
                    _wall = min(
                        x for x in (
                            float(signals.get("day_low_so_far") or 0.0),
                            float(signals.get("or_low") or 0.0),
                        ) if x > 0
                    )
                    if _wall > 0:
                        _want = int(round((_wall - _fb_cush) / step) * step)
                        _dput = max(float(_center_ref) - _want, float(step))
                        short_dist = (int(short_dist[0]), int(_dput))
                elif strategy_name == BEAR_CALL_SPREAD:
                    _wall = max(
                        float(signals.get("day_high_so_far") or 0.0),
                        float(signals.get("or_high") or 0.0),
                    )
                    if _wall > 0:
                        _want = int(round((_wall + _fb_cush) / step) * step)
                        _dcall = max(_want - float(_center_ref), float(step))
                        short_dist = (int(_dcall), int(short_dist[1]))
            except (TypeError, ValueError):
                pass

        if (signals.get("afternoon_high_fade") or signals.get("afternoon_low_fade")) and short_dist is not None:
            try:
                _fd_spot = float(signals.get("spot") or 0.0)
                _fd_cush = max(float(step), 80.0)
                if strategy_name == BEAR_CALL_SPREAD:
                    _wall = max(
                        float(signals.get("day_high_so_far") or 0.0),
                        float(signals.get("or_high") or 0.0),
                        _fd_spot,
                    )
                    if _wall > 0:
                        _want = int(round((_wall + _fd_cush) / step) * step)
                        _dcall = max(_want - float(_center_ref), float(step))
                        short_dist = (int(_dcall), int(short_dist[1]))
                elif strategy_name == BULL_PUT_SPREAD:
                    _wall = min(
                        x for x in (
                            float(signals.get("day_low_so_far") or 0.0),
                            float(signals.get("or_low") or 0.0),
                            _fd_spot if _fd_spot > 0 else 1e12,
                        ) if x > 0
                    )
                    if _wall > 0:
                        _want = int(round((_wall - _fd_cush) / step) * step)
                        _dput = max(float(_center_ref) - _want, float(step))
                        short_dist = (int(short_dist[0]), int(_dput))
            except (TypeError, ValueError):
                pass

        # ── Protective wing (v3.1) ────────────────────────────────────────
        # The wing determines BOTH the maximum loss and how much of the short
        # premium is handed back to the long. Taking it as a fixed number
        # makes the engine's own credit/wing ratio gate behave arbitrarily:
        # far-OTM 0DTE shorts with a fat wing can never reach the required
        # ratio, so the engine silently stops trading in the afternoon. The
        # wing is anchored to the ACTUAL short-strike distance (the only thing
        # that determines how cheap the long is), with the adaptive
        # straddle-based hint from data_engine as a fallback, then clamped by
        # DTE so 0DTE max loss stays small where credits are small.
        _wing_hint = int(signals.get("wing_width") or 150)
        # PATCH_V25: 300-pt wing on dte>=2 failed-LOW so 15% scalp clears costs.
        if (signals.get("neutral_range_vertical")
                and strategy_name == BULL_PUT_SPREAD):
            _wing_hint = max(_wing_hint, 300 if _weekly_dte else 200)
        if signals.get("afternoon_high_fade") or signals.get("afternoon_low_fade"):
            _wing_hint = max(_wing_hint, 150)
        if isinstance(short_dist, (tuple, list)):
            _short_dist_ref = min(float(short_dist[0]), float(short_dist[1]))
        else:
            _short_dist_ref = float(short_dist) if short_dist else 0.0
        if _short_dist_ref > 0:
            _wing_factor = 0.50 if dte == 0 else (0.60 if dte == 1 else 0.62)
            _wing_raw = max(
                _short_dist_ref * _wing_factor, float(_wing_hint) * 0.60
            )
        else:
            _wing_raw = float(_wing_hint)
        _wing_min = max(2 * step, 100)
        _wing_max = 250 if dte == 0 else (350 if dte == 1 else 450)
        wing = int(round(_wing_raw / step + 0.001) * step)
        wing = int(max(_wing_min, min(wing, _wing_max)))
        if (signals.get("neutral_range_vertical")
                and strategy_name == BULL_PUT_SPREAD):
            wing = int(min(max(wing, 300 if _weekly_dte else 200), _wing_max))

        # ── v4.2: adaptive wing fit ───────────────────────────────────────
        # A fresh-weekly wing (multi-day vega) routinely costs 55-65% of a
        # short priced at 0.18 delta; the old fixed 0.75-factor wing then
        # tripped wing_cost_frac_max on EVERY condor candidate (measured
        # 2026-09-09 11:29-13:50 and 2026-09-10 12:21-12:51: zero
        # symmetric structures all day). Rather than weaken the gate, widen
        # the long step by step until its quoted premium is inside the cap
        # - the long is supposed to be cheap insurance, so let the chain
        # itself tell us how far out to buy it. Bounded by _wing_max.
        # Adaptive fitting is for the WEEKLY CONDOR only: the wing-cost
        # gate it satisfies is condor-only, and the static 0DTE wing
        # table is the calibrated expiry-day behavior (do not touch it).
        # The single-sided vertical's long is risk definition priced by
        # the credit/wing ratio gates, so it never gets the fitter either.
        if (strategy_name == IRON_CONDOR and _weekly_dte
                and short_dist and _short_dist_ref > 0):
            wing = self._fit_wing_width(
                chain=chain, strategy_name=strategy_name,
                center_ref=_center_ref, short_dist=short_dist,
                step=step, wing0=wing, wing_max=_wing_max,
                dte=dte,
                cap=float(getattr(
                    self.config,
                    "wing_cost_frac_max_weekly" if _weekly_dte
                    else "wing_cost_frac_max", 0.58 if _weekly_dte else 0.50)),
            )

        if strategy_name == IRON_BUTTERFLY:
            return self._build_iron_butterfly(chain, spot, step, wing)
        if strategy_name == IRON_CONDOR:
            return self._build_iron_condor(
                chain, spot, step, dte, short_dist, delta_target, wing, signals, _center_ref
            )
        if strategy_name == BULL_PUT_SPREAD:
            return self._build_bull_put_spread(
                chain, spot, step, dte, short_dist, delta_target, wing, _center_ref
            )
        if strategy_name == BEAR_CALL_SPREAD:
            return self._build_bear_call_spread(
                chain, spot, step, dte, short_dist, delta_target, wing, _center_ref
            )
        return None, f"unknown_strategy_{strategy_name}"

    def _fit_wing_width(
        self,
        chain: dict,
        strategy_name: str,
        center_ref: float,
        short_dist,
        step: int,
        wing0: int,
        wing_max: int,
        dte: Optional[int],
        cap: float,
    ) -> int:
        """Smallest wing >= wing0 whose quoted long premium is <= cap x short.

        Pricing uses the real SELL=bid / BUY=ask convention, so it measures
        the insurance premium the engine would actually pay. If no width up
        to wing_max satisfies the cap, the widest available is returned and
        the downstream wing-cost gate rejects the trade as before.
        """
        try:
            if isinstance(short_dist, (tuple, list)):
                sd_c, sd_p = float(short_dist[0]), float(short_dist[1])
            else:
                sd_c = sd_p = float(short_dist)
            sc = int(round((center_ref + sd_c) / step) * step)
            sp = int(round((center_ref - sd_p) / step) * step)
            sides = []
            if strategy_name in (IRON_CONDOR, BEAR_CALL_SPREAD):
                sides.append(("call", sc))
            if strategy_name in (IRON_CONDOR, BULL_PUT_SPREAD):
                sides.append(("put", sp))

            def _ratio(opt: str, short_k: int, width: int):
                long_k = short_k + width if opt == "call" else short_k - width
                if float(long_k) not in chain:
                    long_k = int(min(chain.keys(),
                                     key=lambda k: abs(float(k) - long_k)))
                if float(short_k) not in chain:
                    return None
                s = self._get_exec_price(chain, float(short_k), opt, "SELL")
                l = self._get_exec_price(chain, float(long_k), opt, "BUY")
                if s <= 0 or l <= 0:
                    return None
                return l / s

            w = max(int(wing0), int(step))
            while w <= int(wing_max):
                rs = [_ratio(o, k, w) for o, k in sides]
                if rs and all(r is not None and r <= cap for r in rs):
                    return w
                w += int(step)
            return int(wing_max)
        except Exception:
            return int(wing0)

    def _build_iron_butterfly(
        self, chain: dict, spot: float, step: int, wing: int
    ) -> Tuple[Optional[List[dict]], Optional[str]]:
        atm = int(round(spot / step) * step)
        if atm not in chain:
            atm = int(min(chain.keys(), key=lambda k: abs(k - spot)))
        lc = int(atm + wing)
        lp = int(atm - wing)
        if lc not in chain:
            lc = int(min(chain.keys(), key=lambda k: abs(k - (atm + wing))))
        if lp not in chain:
            lp = int(min(chain.keys(), key=lambda k: abs(k - (atm - wing))))
        if lc <= atm or lp >= atm:
            return None, f"butterfly_wing_strikes_invalid:atm={atm} lc={lc} lp={lp}"
        return [
            {"strike": float(atm), "option_type": "call", "action": "SELL"},
            {"strike": float(atm), "option_type": "put",  "action": "SELL"},
            {"strike": float(lc),  "option_type": "call", "action": "BUY"},
            {"strike": float(lp),  "option_type": "put",  "action": "BUY"},
        ], None

    def _build_iron_condor(
        self,
        chain:        dict,
        spot:         float,
        step:         int,
        dte:          Optional[int],
        short_dist:   Optional[int],
        delta_target: float,
        wing:         int,
        signals:      dict,
        center_ref:   Optional[float] = None,
    ) -> Tuple[Optional[List[dict]], Optional[str]]:
        _cr = center_ref if center_ref is not None else spot
        if short_dist is not None:
            # v3.2: short_dist may be a (call_distance, put_distance)
            # pair so the two sides can sit at equal DELTA rather than
            # equal distance, which is what NIFTY skew requires.
            if isinstance(short_dist, (tuple, list)):
                _sd_c, _sd_p = float(short_dist[0]), float(short_dist[1])
            else:
                _sd_c = _sd_p = float(short_dist)
            sc = int(round((_cr + _sd_c) / step) * step)
            sp = int(round((_cr - _sd_p) / step) * step)
        else:
            sc = self._find_strike_by_delta(chain, "call", delta_target)
            sp = self._find_strike_by_delta(chain, "put",  delta_target)
            if sc is None or sp is None:
                return None, "cannot_find_delta_strikes_for_condor"
            sc, sp = int(sc), int(sp)
        if sc not in chain:
            sc = int(min(chain.keys(), key=lambda k: abs(k - sc)))
        if sp not in chain:
            sp = int(min(chain.keys(), key=lambda k: abs(k - sp)))
        lc = int(sc + wing)
        lp = int(sp - wing)
        if lc not in chain:
            lc = int(min(chain.keys(), key=lambda k: abs(k - lc)))
        if lp not in chain:
            lp = int(min(chain.keys(), key=lambda k: abs(k - lp)))
        if sc <= sp + step:
            return None, f"condor_short_strikes_too_close:sc={sc} sp={sp}"
        if lc <= sc:
            return None, f"condor_long_call_{lc}_not_above_short_call_{sc}"
        if lp >= sp:
            return None, f"condor_long_put_{lp}_not_below_short_put_{sp}"
        return [
            {"strike": float(sc), "option_type": "call", "action": "SELL"},
            {"strike": float(sp), "option_type": "put",  "action": "SELL"},
            {"strike": float(lc), "option_type": "call", "action": "BUY"},
            {"strike": float(lp), "option_type": "put",  "action": "BUY"},
        ], None

    def _build_bull_put_spread(
        self,
        chain:        dict,
        spot:         float,
        step:         int,
        dte:          Optional[int],
        short_dist:   Optional[int],
        delta_target: float,
        wing:         int,
        center_ref:   Optional[float] = None,
    ) -> Tuple[Optional[List[dict]], Optional[str]]:
        _cr = center_ref if center_ref is not None else spot
        if short_dist is not None:
            _sd_p = float(
                short_dist[1] if isinstance(short_dist, (tuple, list))
                else short_dist
            )
            sp = int(round((_cr - _sd_p) / step) * step)
        else:
            sp_f = self._find_strike_by_delta(chain, "put", delta_target)
            if sp_f is None:
                return None, "cannot_find_delta_strike_for_bull_put"
            sp = int(sp_f)
        if sp not in chain:
            sp = int(min(chain.keys(), key=lambda k: abs(k - sp)))
        lp = int(sp - wing)
        if lp not in chain:
            lp = int(min(chain.keys(), key=lambda k: abs(k - lp)))
        if lp >= sp:
            return None, f"bull_put_long_{lp}_not_below_short_{sp}"
        if (sp - lp) < 50:
            return None, f"bull_put_wing_too_narrow:{sp - lp}pts"
        return [
            {"strike": float(sp), "option_type": "put", "action": "SELL"},
            {"strike": float(lp), "option_type": "put", "action": "BUY"},
        ], None

    def _build_bear_call_spread(
        self,
        chain:        dict,
        spot:         float,
        step:         int,
        dte:          Optional[int],
        short_dist:   Optional[int],
        delta_target: float,
        wing:         int,
        center_ref:   Optional[float] = None,
    ) -> Tuple[Optional[List[dict]], Optional[str]]:
        _cr = center_ref if center_ref is not None else spot
        if short_dist is not None:
            _sd_c = float(
                short_dist[0] if isinstance(short_dist, (tuple, list))
                else short_dist
            )
            sc = int(round((_cr + _sd_c) / step) * step)
        else:
            sc_f = self._find_strike_by_delta(chain, "call", delta_target)
            if sc_f is None:
                return None, "cannot_find_delta_strike_for_bear_call"
            sc = int(sc_f)
        if sc not in chain:
            sc = int(min(chain.keys(), key=lambda k: abs(k - sc)))
        lc = int(sc + wing)
        if lc not in chain:
            lc = int(min(chain.keys(), key=lambda k: abs(k - lc)))
        if lc <= sc:
            return None, f"bear_call_long_{lc}_not_above_short_{sc}"
        if (lc - sc) < 50:
            return None, f"bear_call_wing_too_narrow:{lc - sc}pts"
        return [
            {"strike": float(sc), "option_type": "call", "action": "SELL"},
            {"strike": float(lc), "option_type": "call", "action": "BUY"},
        ], None

    def _find_strike_by_delta(
        self,
        chain:     dict,
        opt_type:  str,
        target:    float,
        tolerance: float = 0.10,
    ) -> Optional[float]:
        best_strike, best_diff = None, float("inf")
        for strike, legs in chain.items():
            leg   = legs.get(opt_type, {})
            delta = leg.get("delta")
            if delta is None:
                continue
            diff = abs(abs(float(delta)) - target)
            if diff < best_diff:
                best_diff, best_strike = diff, strike
        return best_strike if best_diff <= tolerance else None

    def _validate_leg(
        self,
        chain:    dict,
        strike:   float,
        opt_type: str,
        action:   str,
    ) -> Tuple[bool, str]:
        if strike not in chain:
            return False, f"strike_{strike:.0f}_not_in_chain"
        opt = chain[strike].get(opt_type, {})
        if not opt:
            return False, f"strike_{strike:.0f}_{opt_type}_no_data"
        bid = float(opt.get("bid", 0) or 0)
        ask = float(opt.get("ask", 0) or 0)
        oi  = int(opt.get("oi",  0) or 0)
        ltp = float(opt.get("ltp", 0) or 0)
        if bid <= 0 and ask <= 0 and ltp <= 0:
            return False, f"strike_{strike:.0f}_{opt_type}_no_bid_ask_ltp"
        # PATCH_V14: the liquidity floor was written in CONTRACTS and applied
        # to UNITS. Upstox reports open interest and volume for F&O in
        # underlying units - the same convention its v2 place-order contract
        # uses for quantity, and visible in the recorded data: every one of the
        # 133,520 option_chain_snapshot OI values on 2026-09-08 is an exact
        # multiple of the 65-unit lot, and the smallest non-zero value in the
        # file is 65, i.e. precisely one contract. A floor of "500" against
        # that column is a floor of 7.7 contracts on the short leg of a
        # structure the engine intends to hold for hours, and 1.5 contracts on
        # the protective wing it has to buy back in a hurry. On 0DTE, where the
        # whole point of the wing is that it fills when everything else will
        # not, that is the difference between a defined-risk structure and an
        # undefined one. The floor is now the intended contract count scaled by
        # the lot size the rest of the engine already uses. Measured on the
        # five recorded sessions: not one trade changes, to the rupee - the
        # strikes this engine sells are liquid, and the floor was simply never
        # doing the job it was written for.
        _lot = int(getattr(self.config, "lot_size", 65) or 65)
        min_oi = (500 if action == "SELL" else 100) * _lot
        if oi < min_oi:
            return False, (
                f"strike_{strike:.0f}_{opt_type}_oi_{oi}_below_{min_oi}"
                f"_units_{min_oi // max(_lot, 1)}_contracts"
            )
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            # v3.1: a purely relative spread gate rejects cheap protective
            # wings for no reason — a Rs 1.50 long option quoted 1.45/1.55 is
            # a perfectly normal one-tick market but reads as 6.7%, and at
            # Rs 0.80 a single tick reads as 12.5%. Since the long wing is
            # what makes the structure defined-risk, rejecting it forces the
            # engine either to skip the trade or to reach for a wider, worse
            # wing. An absolute rupee tolerance is allowed on top of the
            # relative cap.
            _rel_cap = 0.15 if action == "SELL" else 0.30
            _abs_cap = float(getattr(self.config, "spread_abs_tolerance", 0.85))
            if mid > 0 and (ask - bid) > max(mid * _rel_cap, _abs_cap):
                return False, f"strike_{strike:.0f}_{opt_type}_spread_too_wide"
        eff = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else ltp
        if eff < 0.50:
            return False, f"strike_{strike:.0f}_{opt_type}_premium_{eff:.2f}_below_0.50"
        return True, "valid"

    def _get_exec_price(
        self,
        chain:    dict,
        strike:   float,
        opt_type: str,
        action:   str,
    ) -> float:
        opt = chain.get(strike, {}).get(opt_type, {})
        if not opt:
            return 0.0
        bid = float(opt.get("bid", 0) or 0)
        ask = float(opt.get("ask", 0) or 0)
        ltp = float(opt.get("ltp", 0) or 0)
        if action == "SELL":
            return bid if bid > 0 else (ltp if ltp > 0 else ask)
        return ask if ask > 0 else (ltp if ltp > 0 else bid)

    def _build_validated_legs(
        self,
        legs_spec: List[dict],
        chain:     dict,
    ) -> Tuple[Optional[List[dict]], Optional[str]]:
        step      = self.config.nifty_strike_step
        validated: List[dict] = []
        for spec in legs_spec:
            strike   = float(spec["strike"])
            opt_type = str(spec["option_type"])
            action   = str(spec["action"])
            ok, reason = self._validate_leg(chain, strike, opt_type, action)
            if not ok:
                found = False
                for offset in [1, -1, 2, -2]:
                    alt = strike + offset * step
                    if alt in chain:
                        alt_ok, _ = self._validate_leg(chain, alt, opt_type, action)
                        if alt_ok:
                            strike, found = alt, True
                            break
                if not found:
                    return None, f"leg_validation_failed_no_fallback:{reason}"
                # PATCH_V14: a substituted strike is a DIFFERENT structure, and
                # the walk above takes the first alternative that merely quotes
                # - it never re-runs the delta, credit or wing logic that chose
                # the original. On a symmetric structure that silently produces
                # an asymmetric one (a condor whose call wing moved 50pts while
                # its put wing did not), and on any structure it can produce two
                # legs on one strike, or a short pushed OUTSIDE its own
                # protective wing - a debit where a credit was selected. The
                # economics downstream are recomputed on whatever legs survive,
                # so the EV gate stays honest about the structure it is given;
                # what nothing downstream can see is that the structure is no
                # longer the one that was chosen. Refuse instead, and name the
                # move: a skipped cycle costs nothing, a leg in the wrong place
                # costs the wing.
                _wanted = float(spec["strike"])
                _seen = [(float(v["strike"]), str(v["option_type"]),
                          str(v["action"])) for v in validated]
                for _k, _t, _a in _seen:
                    if _k == strike and _t == str(opt_type):
                        return None, (
                            f"leg_substitution_collides_{_wanted:.0f}_to_"
                            f"{strike:.0f}_{opt_type}_already_in_structure"
                        )
                _longs = [_k for _k, _t, _a in _seen if _a == "BUY"
                          and _t == str(opt_type)]
                if _longs and action == "SELL":
                    if str(opt_type) == "call" and strike >= max(_longs):
                        return None, (
                            f"leg_substitution_inverts_short_call_{strike:.0f}"
                            f"_at_or_beyond_wing_{max(_longs):.0f}"
                        )
                    if str(opt_type) == "put" and strike <= min(_longs):
                        return None, (
                            f"leg_substitution_inverts_short_put_{strike:.0f}"
                            f"_at_or_below_wing_{min(_longs):.0f}"
                        )
            ep = self._get_exec_price(chain, strike, opt_type, action)
            if ep <= 0:
                return None, f"leg_{strike:.0f}_{opt_type}_no_exec_price"
            opt = chain[strike][opt_type]
            validated.append({
                "strike":      strike,
                "option_type": opt_type,
                "action":      action,
                "exec_price":  ep,
                "bid":         float(opt.get("bid",   0) or 0),
                "ask":         float(opt.get("ask",   0) or 0),
                "ltp":         float(opt.get("ltp",   0) or 0),
                "delta":       float(opt.get("delta", 0) or 0),
                "gamma":       float(opt.get("gamma", 0) or 0),
                "vega":        float(opt.get("vega",  0) or 0),
                "theta":       float(opt.get("theta", 0) or 0),
                "iv":          float(opt.get("iv",    0) or 0),
                "oi":          int(opt.get("oi",      0) or 0),
                "instrument_key": opt.get("instrument_key"),
            })
        return validated, None

    def _structure_wing_pts(self, legs: List[dict]) -> Optional[float]:
        """
        v3.2: the narrowest short-to-long distance in the structure, in
        points. This is the real maximum loss per unit and it is needed
        BEFORE sizing, because the cost gates have to be priced at the
        size the engine is actually going to trade (see _estimate_lots).
        """
        try:
            shorts = [l for l in legs if l.get("action") == "SELL"]
            longs  = [l for l in legs if l.get("action") == "BUY"]
            if not shorts or not longs:
                return None
            widths = []
            for side in ("call", "put"):
                s = [float(l["strike"]) for l in shorts
                     if l.get("option_type") == side]
                b = [float(l["strike"]) for l in longs
                     if l.get("option_type") == side]
                if s and b:
                    if side == "call":
                        widths.append(abs(max(b) - min(s)))
                    else:
                        widths.append(abs(min(s) - min(b)))
            widths = [w for w in widths if w > 0]
            return float(min(widths)) if widths else None
        except Exception:
            return None

    def _risk_fraction_for_dte(self, dte: Optional[int]) -> float:
        """Fraction of the configured per-trade risk budget, by DTE.

        Always 1.0: the DTE discount lives in exactly ONE place — the
        regime layer's dte_mult. Discounting here as well double-counted
        distance from expiry (measured 2026-09-09 DTE4: 0.40 here x 0.30
        in dte_mult), and together with the day/event schedule it capped
        every DTE>=2 setup at ~0.03-0.06 lots against a 0.6 minimum —
        structurally untradable, however good the edge.
        """
        return 1.0

    def _estimate_lots(
        self,
        wing_pts:   float,
        credit_pts: float,
        dte:        Optional[int],
        size_mult:  float,
        state:      dict,
    ) -> int:
        """
        v3.2: a provisional lot count, used ONLY to price the cost gates.

        Brokerage is charged PER ORDER, not per lot: a four-leg condor is
        eight orders round trip, about Rs 189 with GST. At one lot that
        is 2.9 premium points and can be 15% of the whole credit; at four
        lots it is 0.7 points. v3.1 priced every gate at lots = 1 and
        sized afterwards, so the friction the gates saw bore no relation
        to the friction the trade would actually pay.
        """
        try:
            C02 = float(self.config.lot_size or 1)
            cap = float(
                state.get("current_capital", self.config.starting_capital)
                or self.config.starting_capital
            )
            budget = float(self.config.max_risk_per_trade_pct or 0.006)
            max_risk = cap * budget * self._risk_fraction_for_dte(dte)
            # Mirror the engine's own efficacy-blended risk measure so the
            # provisional size matches the size that will really be used.
            eff = float(getattr(self.config, "stop_efficacy", 0.55))
            if dte == 0:
                sm = float(getattr(self.config, "stop_mult_dte0", 1.40))
            elif dte == 1:
                sm = float(getattr(self.config, "stop_mult_dte1", 1.55))
            else:
                sm = float(getattr(self.config, "stop_mult_dte2p", 1.70))
            stop_lot = max((sm - 1.0) * float(credit_pts) * C02, 1.0)
            struct_lot = max((float(wing_pts) - float(credit_pts)) * C02, 1.0)
            per_lot = min(
                max(eff * stop_lot + (1.0 - eff) * struct_lot, stop_lot, 1.0),
                struct_lot,
            )
            lots = (max_risk / per_lot) * max(float(size_mult), 0.10)
            return int(max(1, min(round(lots), 50)))
        except Exception:
            return 1

    def _compute_costs(
        self,
        legs:   List[dict],
        lots:   int,
        action: str,
    ) -> dict:
        C02        = self.config.lot_size
        sell_value = buy_value = 0.0
        num_orders = len(legs)
        for leg in legs:
            price = float(leg.get("exec_price") or leg.get("entry_price") or 0)
            if price <= 0:
                continue
            pv = price * lots * C02
            if leg["action"] == "SELL":
                sell_value += pv
            else:
                buy_value += pv
        turnover = sell_value + buy_value
        if turnover <= 0:
            return {"total_rupees": 0.0, "breakdown": {}}
        stt       = sell_value * self.config.stt_options_sell
        exchange  = turnover   * self.config.exchange_txn_rate
        sebi      = turnover   * self.config.sebi_rate
        stamp     = buy_value  * self.config.stamp_duty_buy_options
        brokerage = self.config.brokerage_per_order * num_orders
        gst       = (brokerage + exchange + sebi) * 0.18
        total     = stt + exchange + sebi + stamp + brokerage + gst
        return {
            "total_rupees": round(total, 2),
            "breakdown": {
                "stt":       round(stt, 2),
                "exchange":  round(exchange, 2),
                "sebi":      round(sebi, 4),
                "stamp":     round(stamp, 4),
                "brokerage": round(brokerage, 2),
                "gst":       round(gst, 2),
                "total":     round(total, 2),
            },
        }

    def _compute_slippage(
        self,
        legs:    List[dict],
        is_exit: bool = False,
    ) -> float:
        """
        Spread-aware slippage for the whole structure, in premium points
        (points are lot-invariant).

        v3.1 notes
        ----------
        The docstring and the code disagreed (150% claimed, 3.0x applied) and,
        more importantly, the entry number was double counting: exec_price is
        already taken at the BID for shorts and the ASK for longs, so the full
        spread has already been paid before this function is called. Charging
        a further half-spread on entry inflated modelled friction and made the
        engine reject sound structures.

        Entry now carries only a small residual (queue and tick risk on a
        marketable limit). The exit carries the genuinely large number,
        because that is where the money is actually lost: an OTM NIFTY 0DTE
        market quoting 0.10 wide at 11:00 quotes 0.50-1.00 wide when a stop
        fires into a fast move, and that is exactly when the engine exits.

        Both multiples are configurable (entry_slippage_mult /
        exit_slippage_mult) so they can be re-fitted from the engine's own
        exit-quality telemetry once it has enough sessions.
        """
        entry_mult = float(getattr(self.config, "entry_slippage_mult", 0.35))
        exit_mult  = float(getattr(self.config, "exit_slippage_mult", 2.25))
        total = 0.0
        for leg in legs:
            bid = float(leg.get("bid", 0) or 0)
            ask = float(leg.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                half_spread = (ask - bid) / 2.0
                total += half_spread * (exit_mult if is_exit else entry_mult)
            else:
                total += 1.20 if is_exit else 0.35
        return round(total, 3)

    def _round_trip_friction(
        self,
        legs:            List[dict],
        entry_costs_pts: float,
    ) -> float:
        """
        v3.1: TRUE round-trip friction, in premium points.

        The old code approximated the round trip as
        `(entry_costs + entry_slippage) * 1.5`, which understates it twice
        over: the exit is a full second set of brokerage and statutory
        charges, and exit slippage is several times entry slippage. Every
        gate that compared credit against friction — the minimum-credit gate
        and the EV gate — was therefore comparing against roughly half the
        real number, so trades that were cost-negative in reality passed.
        """
        entry_slip = self._compute_slippage(legs, is_exit=False)
        exit_slip  = self._compute_slippage(legs, is_exit=True)
        # Exit charges are of the same order as entry charges: brokerage per
        # order is identical, and STT simply moves to whichever legs are sold.
        exit_costs_pts = entry_costs_pts * 0.95
        return round(entry_costs_pts + entry_slip + exit_costs_pts + exit_slip, 4)

    def _get_target_pct(self, dte: Optional[int], signals: dict) -> float:
        """
        Fraction of the net credit taken as profit. (v3.1 recalibration.)

        The old ladder asked for 45-50% of the credit on 0DTE. That is not
        what NIFTY expiry day pays. Theta on the expiring contract is front-
        and middle-loaded while the dangerous part of the move distribution
        arrives after 13:30, so holding a 0DTE structure for half its credit
        means holding it straight into the gamma window — the engine was
        asking for the one outcome that costs the most to wait for.

        The professional pattern on NIFTY 0DTE is the opposite: take 25-35%
        quickly, bank it, let the cooldown re-arm. Expectancy per unit of
        time-at-risk is far higher and tail exposure is a fraction of it.

        Targets also FALL as VIX rises: a richer credit does not mean a bigger
        percentage of it is reachable, it means the move that threatens it is
        bigger too.
        """
        # ── v3.2: the target and the stop are ONE decision ────────────
        # What matters is not either number alone but the reward/risk
        # they imply and therefore the win rate the system must beat.
        # v3.1 took 35% of the credit while risking 150% of it, so the
        # trade had to win 81% of the time before friction - a bar no
        # 0.15-0.22 delta NIFTY structure clears, and the engine's own
        # p_win table peaks at 0.72. v3.2 pairs a larger target with a
        # much tighter stop (see the stop multiples in core.Config):
        # 50% of the credit against a risk of 40%, i.e. reward/risk of
        # 1.25 and a break-even win rate near 55% before friction. That
        # is also how a NIFTY premium seller actually behaves - hold
        # through most of the decay, and cut the moment the structure
        # is genuinely wrong, rather than scalping a third of the
        # credit while leaving a catastrophic tail open.
        vix = float(signals.get("vix") or 11.0)
        if dte == 0:
            base_t = float(getattr(self.config, "target_pct_dte0", 0.50))
        elif dte == 1:
            base_t = float(getattr(self.config, "target_pct_dte1", 0.45))
        elif dte == 2:
            base_t = float(getattr(self.config, "target_pct_dte2p", 0.40))
        else:
            base_t = float(getattr(self.config, "target_pct_dte2p", 0.40)) - 0.03
        # Richer implied vol means a wider distribution, so take the
        # money a little sooner.
        if vix >= 14.0:
            base_t -= 0.07
        elif vix >= 12.0:
            base_t -= 0.035
        return round(min(max(base_t, 0.18), 0.70), 4)

    def _proximity_buffer_pts(self, spot: float) -> float:
        """
        v3.1: the distance from a short strike at which the engine closes.

        Was a flat 40 points (config.spot_proximity_pts) — 0.22% of an 18,000
        index but only 0.15% of a 26,000 one, so the structural protection
        silently weakened as NIFTY rose. Now scaled to spot with the
        configured absolute value as a floor.
        """
        base = float(self.config.spot_proximity_pts or 40)
        pct  = float(getattr(self.config, "spot_proximity_pct", 0.0016))
        if spot and spot > 0:
            return max(base, spot * pct)
        return base

    def _price_stop_pts(
        self,
        wing_pts:       float,
        short_dist_pts: float,
        spot:           float,
    ) -> float:
        """
        v3.2: how far INSIDE the short strike the spot backstop sits.

        v3.1 used 0.42 x the OPENING STRADDLE. With a 175-point straddle
        that put the stop 73 points inside the short strike, so a
        structure sold 200 points away was flattened after roughly 130
        points of movement - about half a sigma, an ordinary hour on
        NIFTY. The engine was therefore designed to take frequent small
        losses while its own p_win model assumed the far-away strike was
        the barrier. That single mismatch is enough to turn a positive
        edge into a negative one.

        The spot stop is now a BACKSTOP tied to the STRUCTURE - a
        fraction of the wing, floored in points and capped as a fraction
        of the short-strike distance - and the premium stop is the
        primary risk control, which is how a professional book is run.
        """
        frac = float(getattr(self.config, "price_stop_wing_frac", 0.30))
        floor_pts = float(getattr(self.config, "price_stop_min_pts", 25.0))
        cap_frac = float(getattr(self.config, "price_stop_max_frac_of_dist", 0.40))
        prox = self._proximity_buffer_pts(spot)
        # v3.9: the absolute proximity band exceeds the whole gap of a
        # delta-0.3+ expiry short (a 45pt gap against a 40pt band), which
        # would model a defense sitting 5 points from entry. The defense
        # is also bounded by (1 - prox_gap_frac) of the gap, so it scales
        # with the structure the engine actually built.
        if short_dist_pts and short_dist_pts > 0:
            _gap_frac = float(getattr(self.config, "prox_gap_frac_dte0", 0.70))
            prox = min(
                prox, max(1.0 - _gap_frac, 0.05) * float(short_dist_pts)
            )
        val = max(float(wing_pts) * frac, floor_pts, prox)
        # An at-the-money structure (the iron butterfly) has no room
        # INSIDE its short strike - spot is already there. Capping the
        # backstop at a fraction of a zero distance produced a stop
        # level on the wrong side of the strike, which fired on the very
        # first monitoring cycle: the butterfly could never be held.
        if short_dist_pts and short_dist_pts > prox:
            val = min(val, float(short_dist_pts) * cap_frac)
        else:
            val = max(float(wing_pts) * 0.55, prox)
        return round(max(val, 10.0), 1)

    def _compute_ev_gate(
        self,
        net_credit:      float,
        wing:            float,
        entry_costs_pts: float,
        total_slippage:  float,
        signals:         dict,
        legs:            Optional[List[dict]] = None,
        stop_premium:    Optional[float] = None,
        barrier_pull_pts: float = 0.0,
    ) -> Tuple[bool, str]:
        """
        Expected value of the structure, in premium points, over the intended
        holding period. (v3.1 rebuild — see the three defects below.)
        """
        dte          = signals.get("actual_dte")
        or_condition = signals.get("or_condition", "MODERATE")
        vrp_smoothed = float(signals.get("vrp_smoothed") or 0.0)
        target_pct   = self._get_target_pct(dte, signals)
        reward_pts   = net_credit * target_pct

        # ── Loss legs ─────────────────────────────────────────────────────
        # Normal loss = the stop actually working. Previously hardcoded to
        # 1.5 x credit with no relationship to the stop_premium the engine
        # would really use.
        if stop_premium and stop_premium > 0:
            stop_loss_pts = max(stop_premium - net_credit, 0.0)
        else:
            stop_loss_pts = net_credit * 1.5
        # [E2] Tail loss = the stop is jumped and the structure prints toward
        # the wing. Not the full wing (partial fills and some recovery are
        # normal) but far beyond the stop. A NIFTY 0DTE short-premium book
        # does not die at the stop; it dies here, and this outcome was simply
        # absent from the old two-outcome expectancy.
        wing_loss_pts = max(float(wing) - net_credit, stop_loss_pts)
        # v3.2: the tail was priced at 80% of the FULL structural loss,
        # which is an overnight-gap assumption. NIFTY does not gap
        # intraday: it is cash settled, continuously quoted, and this
        # engine is flat by 15:00 with three independent exits (premium
        # stop, spot backstop, delta) checked every cycle. The realistic
        # failure is a fast trend that fills the stop late, not a jump to
        # max loss - so the tail is the stop plus part of the distance
        # from there to the wing. At 0.055 x 0.80 x wing the old term
        # alone cost 4.4% of the wing, more than the entire profit target
        # of a typical 0DTE condor, so no wide-wing structure could ever
        # show positive EV however good the setup was.
        tail_loss_pts = max(
            stop_loss_pts,
            stop_loss_pts + 0.30 * max(wing_loss_pts - stop_loss_pts, 0.0),
        )

        # True round-trip friction rather than entry x 1.5.
        friction = (
            self._round_trip_friction(legs, entry_costs_pts) if legs
            else (entry_costs_pts + total_slippage) * 2.2
        )

        p_win_table = {
            0: {"VERY_NARROW": 0.72, "NARROW": 0.68, "MODERATE": 0.62, "WIDE": 0.52, "VERY_WIDE": 0.44},
            1: {"VERY_NARROW": 0.68, "NARROW": 0.64, "MODERATE": 0.58, "WIDE": 0.48, "VERY_WIDE": 0.40},
            2: {"VERY_NARROW": 0.64, "NARROW": 0.60, "MODERATE": 0.54, "WIDE": 0.46, "VERY_WIDE": 0.38},
            3: {"VERY_NARROW": 0.61, "NARROW": 0.57, "MODERATE": 0.51, "WIDE": 0.43, "VERY_WIDE": 0.35},
            4: {"VERY_NARROW": 0.58, "NARROW": 0.54, "MODERATE": 0.48, "WIDE": 0.40, "VERY_WIDE": 0.32},
            5: {"VERY_NARROW": 0.56, "NARROW": 0.52, "MODERATE": 0.46, "WIDE": 0.38, "VERY_WIDE": 0.30},
            6: {"VERY_NARROW": 0.54, "NARROW": 0.50, "MODERATE": 0.44, "WIDE": 0.36, "VERY_WIDE": 0.28},
        }
        # ── [E3] Empirical OR-conditional prior ───────────────────────────
        # This was previously computed and then thrown away the moment the
        # barrier model returned a number. It carries everything the
        # lognormal geometry cannot see — positioning, pinning, and the
        # engine's own historical hit rate by opening range — so it is now
        # blended in rather than discarded.
        p_win_prior = p_win_table.get(
            min(dte if dte is not None else 1, 6), p_win_table[6]
        ).get(or_condition, 0.50)
        if vrp_smoothed > 3.5:
            p_win_prior += 0.05
        elif vrp_smoothed > 2.5:
            p_win_prior += 0.025
        elif vrp_smoothed < 2.0:
            p_win_prior -= 0.03
        # v3.9: the vol regime label is itself the consensus vote of the
        # IV/RV stack. STRONG_SELL_PREMIUM means the chain pays far above
        # its own realised-risk estimate; the engine's expiry-session hit
        # rate in that state sits materially above the table's flat OR
        # base rate, the same effect the VRP tiers above approximate.
        if str(signals.get("vol_regime") or "") == "STRONG_SELL_PREMIUM":
            p_win_prior += float(getattr(
                self.config, "ev_strong_sell_prior_bonus", 0.05
            ))
        p_win_prior = max(0.30, min(0.90, p_win_prior))

        # ── [E1] Barrier model on the TRUE short-strike distance ──────────
        # The old code used `wing * 0.5 - spot_proximity_pts` as the barrier,
        # i.e. half the protective WING WIDTH. That is not the barrier that
        # decides a credit spread — the distance from spot to the SHORT
        # STRIKE is. With a 150pt wing the model used ~45pts against a real
        # barrier of 250-300pts, so z was roughly 6x too small, p_win pinned
        # to its 0.35 floor, and the EV gate rejected structurally excellent
        # trades all day while reporting a plausible-looking reason.
        import math as _math_ev

        _atm_iv = float(signals.get("atm_iv") or 0.0)
        if _atm_iv >= 2.0:          # stored as a percentage, not a decimal
            _atm_iv = _atm_iv / 100.0
        # v3.9: the vendor-stamped 0DTE ATM IV inflates as sqrt(T)
        # collapses into the afternoon - measured 2026-09-08 12:03 the
        # stamp read 21.6% while the ATM straddle price itself (0.8 x
        # straddle) implied ~10.2% and India VIX printed 11.1. A stamp
        # that far above the cash VIX is an artefact, not information,
        # and feeding it into the barrier model doubles the touch
        # probability of every short the engine tries to sell.
        _vix_ev = float(signals.get("vix") or 0.0)
        if _vix_ev > 2.0 and _atm_iv > 0:
            _iv_cap = float(getattr(
                self.config, "atm_iv_vix_cap", 1.35
            )) * (_vix_ev / 100.0)
            _atm_iv = min(_atm_iv, _iv_cap)
        _spot_ev = float(signals.get("spot") or 0.0)
        _now_ev = now_ist().time()
        # v3.2: the hard exit was hardcoded to 15:00 while the engine
        # reads it from state/config everywhere else, so on a shortened
        # or reconfigured session the EV gate priced the wrong horizon.
        try:
            _he_str = self.market_engine.state.get("hard_exit_time")
            _he_ev = (
                datetime.strptime(_he_str, "%H:%M").time() if _he_str
                else self.config.hard_exit_time
            )
        except Exception:
            _he_ev = self.config.hard_exit_time
        _mins_to_exit = max(
            (datetime.combine(today_ist(), _he_ev) -
             datetime.combine(today_ist(), _now_ev)).total_seconds() / 60.0,
            5.0
        )
        # ── v3.2 [E7] the horizon is time-to-TARGET, not time-to-close ─
        # The reward leg is a partial close at ~35-40% of the credit,
        # which on a quiet tape arrives well before the hard exit. v3.1
        # priced the reward over that short horizon but the risk over the
        # whole remaining session - a mismatch that inflates the touch
        # probability against a profit that has usually already been
        # taken. Theta on a short structure runs roughly with sqrt(time),
        # so capturing ~38% of the premium consumes about 60% of the
        # remaining clock, and that is the window the barrier must hold.
        _horizon_frac = min(max(1.0 - (1.0 - target_pct) ** 2, 0.35), 0.95)
        _mins_horizon = max(_mins_to_exit * _horizon_frac, 20.0)
        # ── v3.2 [E8] price the barrier under FORECAST vol, not implied ─
        # Selling premium is a bet that realised volatility comes in below
        # implied - that is the entire edge, and the engine gates on
        # exactly that (VRP). Measuring your own risk at full implied vol
        # therefore assumes your edge does not exist, and the gate
        # rejects every trade the strategy was built to take. A variance
        # blend of implied and the Parkinson realised estimate is the
        # standard compromise: it keeps most of implied's caution while
        # acknowledging the spread the position is being paid for.
        _rv_ev = float(signals.get("parkinson_rv") or 0.0)
        if _rv_ev >= 2.0:
            _rv_ev = _rv_ev / 100.0
        if _atm_iv > 0 and _rv_ev > 0:
            _sigma_ann = _math_ev.sqrt(
                0.55 * _atm_iv ** 2 + 0.45 * max(_rv_ev, _atm_iv * 0.55) ** 2
            )
        else:
            _sigma_ann = _atm_iv
        _sigma_t = (
            _sigma_ann * (_mins_horizon / (375.0 * 252.0)) ** 0.5
            if _sigma_ann > 0 else 0.0
        )
        _horizon_scale = (_mins_horizon / _mins_to_exit) ** 0.5

        # ── v3.2 [E4] sigma from the market, not from the broker ──────
        # The ATM IV printed on a 0DTE chain is the noisiest number the
        # broker publishes: the sqrt(T) in the denominator is collapsing
        # all afternoon and different venues stamp it differently. The
        # ATM straddle is a PRICE, cannot be mis-scaled, and is the
        # market's own statement of the expected move. Both estimates are
        # computed and the LARGER (more conservative, lower p_win) wins.
        _sigma_pts_iv = _spot_ev * _sigma_t if _sigma_t > 0 else 0.0
        _sigma_pts_straddle = 0.0
        _em_ev = float(signals.get("expected_move_remaining_pts") or 0.0)
        if _em_ev > 0:
            # A straddle is worth 0.7979 sigma for a driftless lognormal,
            # so sigma follows from dividing by that. It is then put on
            # the same forecast-vol and same horizon footing as the
            # IV-derived estimate above.
            _vol_ratio_ev = (
                (_sigma_ann / _atm_iv) if (_atm_iv > 0 and _sigma_ann > 0)
                else 1.0
            )
            _sigma_pts_straddle = (
                (_em_ev / 0.7979) * _vol_ratio_ev * _horizon_scale
            )
        # -- v3.9 [E4c] the straddle is the authority when they disagree -
        # v3.2 took the LARGER of the two estimates "for conservatism".
        # When the vendor IV stamp is corrupt (see v3.9 above) the
        # larger IS the corrupt one: on 2026-09-08 it doubled sigma, and
        # against a defence line ~0.7 sigma away the touch probability
        # went from plausible to certain, vetoing every candidate. The
        # straddle is a traded PRICE and cannot be mis-scaled, so when
        # both exist the IV-derived estimate is capped at a modest ratio
        # of the straddle-derived one; when only one exists it stands.
        _iv_sigma_cap = float(getattr(
            self.config, "iv_sigma_cap_ratio", 1.15
        ))
        if _sigma_pts_iv > 0 and _sigma_pts_straddle > 0:
            _sigma_pts = min(_sigma_pts_iv, _sigma_pts_straddle * _iv_sigma_cap)
        else:
            _sigma_pts = max(_sigma_pts_iv, _sigma_pts_straddle)

        # ── v3.2 [E5] the barrier the engine ACTUALLY defends ─────────
        # p_win was modelled as the no-touch probability of the short
        # strike less a 40-point proximity buffer. But the position is
        # closed by whichever of these fires FIRST: the delta breach, the
        # proximity exit, or the spot price-stop - and the price stop can
        # sit a long way inside the strike. Modelling the far barrier
        # inflates p_win, and every inflated p_win passes a trade whose
        # real expectancy is negative. The barrier is now pulled in by
        # the largest of the live triggers (barrier_pull_pts is supplied
        # by compute_params, which knows the stop it is about to write).
        # v3.9: the proximity side of the pull is structure-relative now.
        # An absolute 40pt band exceeds the whole gap of a delta-0.3+
        # expiry short (~45pts), which would place the modelled defence
        # line ~5 points from entry and make touching it a certainty on
        # any tape. The defence is bounded by (1 - prox_gap_frac) of the
        # gap on each short, which is exactly where the priority-2 exit
        # in execution_engine now fires, so the gate finally prices the
        # line the position is actually managed against.
        _prox_abs_ev = self._proximity_buffer_pts(_spot_ev)
        _prox_frac_ev = float(getattr(
            self.config, "prox_gap_frac_dte0", 0.70
        ))
        _barriers: List[float] = []
        _spot_distanced_ev = False
        if legs and _spot_ev > 0:
            _dists = [
                abs(float(l["strike"]) - _spot_ev)
                for l in legs if str(l.get("action")) == "SELL"
            ]
            # An iron butterfly sells AT the money, so its distance to
            # the short strike is zero and the strike is simply not the
            # barrier: what defines the fly is how far spot can travel
            # before the structure reaches its stop, which is a
            # function of the wing and the credit taken in. Without
            # this the model pinned every butterfly to its p_win floor
            # and the EV gate refused the strategy outright.
            _atm_floor = max(0.55 * float(wing), 0.70 * net_credit, 25.0)
            for d in _dists:
                _prox_pull_d = min(
                    _prox_abs_ev, max(1.0 - _prox_frac_ev, 0.05) * d
                )
                _pull_d = max(
                    float(barrier_pull_pts or 0.0), _prox_pull_d
                )
                if d > _pull_d:
                    _spot_distanced_ev = True
                    _barriers.append(max(d - _pull_d, 12.0))
                else:
                    _barriers.append(_atm_floor)
        _barrier = min(_barriers) if _barriers else 0.0

        # ── Severity/probability consistency of the stop leg ──────────
        # p_stop is the touch probability of the defence line above (the
        # spot backstop / proximity exit, which fires FIRST by
        # construction), but stop_loss_pts was always the PREMIUM-stop
        # severity. On 0DTE the two exits sit close together so the error
        # is small and conservative; on DTE>=1 they decouple — measured
        # 2026-09-09, a 150-wide bear call: the backstop fires at +57pts
        # of spot where the spread is worth ~13pts against entry, while
        # the 1.7x premium stop needs ~+200pts of spot to fill. Charging
        # the 200pt loss at the 57pt probability vetoed the trade.
        # The stop leg is therefore the NEARER exit in economic terms:
        # the spread's modelled value at the defence line (delta carry
        # plus a gamma allowance, both from the legs' own live greeks),
        # capped at the premium-stop loss which remains the backup. When
        # greeks are missing, or the barrier is the ATM pseudo-distance
        # rather than a spot distance (iron butterfly), the premium-stop
        # severity stands exactly as before.
        if legs and _barrier > 0 and _spot_distanced_ev:
            try:
                _net_d_ev = _net_g_ev = 0.0
                _greeks_ok_ev = False
                for _l in legs:
                    _dd = float(_l.get("delta") or 0.0)
                    _gg = float(_l.get("gamma") or 0.0)
                    if _dd or _gg:
                        _greeks_ok_ev = True
                    _sgn = (
                        -1.0 if str(_l.get("action")) == "SELL" else 1.0
                    )
                    _net_d_ev += _sgn * _dd
                    _net_g_ev += _sgn * _gg
                if _greeks_ok_ev and (
                    abs(_net_d_ev) >= 0.02 or abs(_net_g_ev) >= 0.0002
                ):
                    _carry_ev = (
                        abs(_net_d_ev) * _barrier * 1.25
                        + 0.5 * abs(_net_g_ev) * _barrier ** 2
                    )
                    # Never model the nearer exit as cheaper than a
                    # quarter of the premium stop: a backstop fill in a
                    # fast tape runs past the modelled line.
                    _carry_ev = max(
                        _carry_ev, 0.25 * float(stop_loss_pts)
                    )
                    # v4.2: the carry is an INSTANTANEOUS move priced at
                    # entry delta with no theta credit. On DTE >= 2 the
                    # intended hold runs hours and the spot-proximity /
                    # premium exits actually triggered at 4-8pt losses on
                    # 28-60pt credits across the 2026-09-08/09/10 replays
                    # (vs 18-35pt charged here), roughly 0.6x - the other
                    # half is the theta that accrues before the barrier is
                    # reached. 0DTE keeps the undiscounted conservative
                    # number (gamma does not give theta time to accrue).
                    if (dte is not None and dte >= 2):
                        _carry_ev *= float(getattr(
                            self.config, "ev_carry_discount_dte2p", 0.62
                        ))
                    if _carry_ev < stop_loss_pts:
                        stop_loss_pts = _carry_ev
                        tail_loss_pts = max(
                            stop_loss_pts,
                            stop_loss_pts + 0.30 * max(
                                wing_loss_pts - stop_loss_pts, 0.0
                            ),
                        )
            except Exception:
                pass

        p_win_model = None
        if _sigma_pts > 0 and _barriers and _spot_ev > 0:

            def _ncdf(x: float) -> float:
                return 0.5 * (1.0 + _math_ev.erf(x / _math_ev.sqrt(2.0)))

            # ── v3.2 [E6] two-sided no-touch ──────────────────────────
            # An iron condor has TWO barriers. v3.1 priced only the
            # nearer one, which understates the touch probability by the
            # whole of the far side - material whenever the structure is
            # anywhere near symmetric, which by construction it is.
            _p_touch = 0.0
            for _b in _barriers:
                _z = _b / _sigma_pts
                _p_touch += 2.0 * _ncdf(-_z)

            # ── v3.2 [E9] expiry pinning ──────────────────────────────
            # A driftless random walk is the wrong path model for a
            # NIFTY expiry session. Open interest concentrates at round
            # strikes and the tape demonstrably gravitates toward max
            # pain into the afternoon - which is a large part of why the
            # straddle can be systematically overpriced in the first
            # place. GBM therefore overstates first-touch on pinned days.
            # The engine already computes max_pain on every single cycle
            # and then used it for nothing at all; when spot is sitting
            # inside half an expected move of it, the touch probability
            # is haircut accordingly.
            _mp = float(signals.get("max_pain") or 0.0)
            if _mp > 0 and _spot_ev > 0 and dte == 0:
                _pin_dist = abs(_mp - _spot_ev)
                if _pin_dist < 0.5 * _sigma_pts:
                    _p_touch *= 0.85
                elif _pin_dist < 1.0 * _sigma_pts:
                    _p_touch *= 0.93
            p_win_model = max(0.20, min(0.93, 1.0 - _p_touch))

        # -- v3.9 [E9b] the market-quoted touch probability ------------
        # Option delta is the market's own risk-neutral approximation of
        # "this strike finishes in the money": 1 - |delta| is a live,
        # per-strike p_win estimate produced by the same order flow that
        # set the VRP edge the trade is being paid for. The v3.2 50/50
        # model:prior mix let a poisoned sigma floor the whole verdict.
        # The market leg gets an equal vote alongside the model and the
        # empirical OR prior, and carries the blend whenever the model
        # cannot be computed at all.
        _p_mkt = None
        if legs:
            _short_ds = [
                abs(float(l.get("delta")))
                for l in legs
                if str(l.get("action")) == "SELL" and l.get("delta")
            ]
            _short_ds = [d for d in _short_ds if 0.02 < d < 0.97]
            if _short_ds:
                _p_mkt = min(max(1.0 - max(_short_ds), 0.20), 0.95)
        _wm = float(getattr(self.config, "ev_blend_model_w", 0.40))
        _wp = float(getattr(self.config, "ev_blend_prior_w", 0.30))
        _wk = float(getattr(self.config, "ev_blend_market_w", 0.30))
        # PATCH_V15: for the failed-break vertical the market's own
        # per-strike delta is the sharpest touch estimate - the edge IS
        # the cushion, and delta prices exactly that. The DTE x OR prior
        # was built around ATM-ish shorts; weight the quote instead.
        if signals.get("neutral_range_vertical"):
            _wm, _wp, _wk = 0.25, 0.20, 0.55
        if p_win_model is None:
            if _p_mkt is not None and (_wp + _wk) > 0:
                p_win = (_wp * p_win_prior + _wk * _p_mkt) / (_wp + _wk)
            else:
                p_win = p_win_prior
        elif _p_mkt is not None and (_wm + _wp + _wk) > 0:
            p_win = (
                _wm * p_win_model + _wp * p_win_prior + _wk * _p_mkt
            ) / (_wm + _wp + _wk)
        else:
            # v3.2: the model cannot see pinning, the max-pain magnet,
            # the intraday mean reversion that makes NIFTY paths less
            # diffusive than their terminal volatility implies, or any
            # of the positioning the prior is built from.
            p_win = _wm * p_win_model + (1.0 - _wm) * p_win_prior
        # v3.9: regime-structure alignment. The touch model assumes
        # symmetric threat: a rally toward a sold call spread and a
        # selloff toward a sold put spread are priced alike. But the
        # structure placed by a directional regime sell has its threat
        # side on the UNFAVOURED move of a confirmed trend - on
        # 2026-09-08 (downtrend, STRONG_SELL_PREMIUM, VRP 3.17pp) the
        # post-midday tape never retraced more than 21 points against
        # the call credit. Desks recognise this skew explicitly; a
        # small bounded bonus is how it shows up here without letting
        # the label override the arithmetic.
        _align = float(getattr(self.config, "ev_regime_align_bonus", 0.05))
        if _align > 0 and legs and _p_mkt is not None:
            _sell_sides = {
                str(l.get("option_type"))
                for l in legs if str(l.get("action")) == "SELL"
            }
            _fr_ev = str(signals.get("final_regime") or "")
            _vr_ok = str(signals.get("vol_regime") or "") in (
                "SELL_PREMIUM", "STRONG_SELL_PREMIUM"
            )
            _aligned = _vr_ok and (
                (_sell_sides == {"call"} and _fr_ev == "PREMIUM_SELL_BEAR") or
                (_sell_sides == {"put"} and _fr_ev == "PREMIUM_SELL_BULL")
            )
            if _aligned:
                p_win += _align
        p_win = max(0.28, min(0.92, p_win))

        # ── [E2] Three-outcome expectancy ─────────────────────────────────
        p_tail = (
            float(getattr(self.config, "gamma_tail_prob_dte0", 0.055))
            if dte == 0 else
            float(getattr(self.config, "gamma_tail_prob_dte1p", 0.025))
        )
        # A wide opening range and a trending tape both fatten the tail.
        if or_condition in ("WIDE", "VERY_WIDE"):
            p_tail *= 1.8
        _adx_ev = float(signals.get("adx_15") or 0.0)
        if _adx_ev >= float(self.config.adx_strong_threshold):
            p_tail *= 1.5
        p_tail = min(p_tail, 0.20)

        p_win_eff = max(p_win * (1.0 - p_tail), 0.05)
        p_stop    = max(1.0 - p_win_eff - p_tail, 0.0)

        # ── v3.2 [E10] charge friction PER PATH, and only once ────────
        # Two errors were compounded here. First, net_credit is already
        # net of entry costs and entry slippage, and reward and stop are
        # both expressed against it - yet v3.1 then subtracted the whole
        # ROUND TRIP again, billing the entry half of the friction twice.
        # Second, it charged the STRESSED exit (2.25x the half-spread,
        # the modelled cost of bailing out of a 0DTE structure that has
        # gone wrong) to the WINNING path as well. A win is a resting
        # limit that buys back decayed options near mid; it does not pay
        # panic prices. Pricing the good outcome at the bad outcome's
        # exit cost is a systematic tax on exactly the trades the engine
        # should be taking, and on a 20-point credit it is enough on its
        # own to turn a positive expectancy negative.
        _exit_costs_pts = entry_costs_pts * 0.95
        _slip_exit_stressed = (
            self._compute_slippage(legs, is_exit=True) if legs
            else total_slippage * 2.0
        )
        _calm_mult = max(
            float(getattr(self.config, "entry_slippage_mult", 0.35)) * 2.0,
            0.50,
        )
        _exit_mult = max(float(getattr(self.config, "exit_slippage_mult", 2.25)), 0.01)
        _slip_exit_calm = _slip_exit_stressed * min(_calm_mult / _exit_mult, 1.0)
        _fric_win  = _exit_costs_pts + _slip_exit_calm
        _fric_loss = _exit_costs_pts + _slip_exit_stressed
        ev = (
            p_win_eff * (reward_pts - _fric_win)
            - p_stop * (stop_loss_pts + _fric_loss)
            - p_tail * (tail_loss_pts + _fric_loss)
        )

        # Minimum acceptable edge. The old floor (2% of credit, or 15% of an
        # already-understated friction number) let through trades whose whole
        # expectancy was inside the cost of doing them.
        # v3.9: the v3.2 absolute 0.75 point floor was ~8% of the entire
        # credit a VIX-11 expiry pays. Because entry costs and exit
        # costs are now charged explicitly per-path inside the EV
        # terms, a floor near 100% of friction double-bills them; the
        # cushion is 3% of credit or 35% of friction, whichever bites,
        # roughly 1.35x total cost coverage.
        min_ev = max(
            net_credit * float(getattr(self.config, "min_ev_frac_of_credit", 0.03)),
            friction * float(getattr(self.config, "min_ev_frac_of_friction", 0.35)),
        )

        _detail = (
            f"p_win={p_win:.2f},p_tail={p_tail:.3f},"
            f"rew={reward_pts:.2f},stop={stop_loss_pts:.2f},"
            f"tail={tail_loss_pts:.2f},fric={friction:.2f}"
        )
        if ev < min_ev:
            return False, f"ev_{ev:.2f}pts_below_min_{min_ev:.2f}pts({_detail})"
        return True, f"ev_ok_{ev:.2f}pts({_detail})"

    def compute_params(
        self,
        strategy_name:    str,
        selection_reason: str,
        signals:          dict,
        size_mult:        float,
    ) -> dict:
        state      = self.market_engine.state
        expiry_str = signals.get("active_expiry")
        actual_dte = signals.get("actual_dte")
        C02        = self.config.lot_size

        if expiry_str is None or actual_dte is None:
            return {"valid": False, "reason": "no_active_expiry"}

        dte_min, dte_max = DTE_REQUIREMENTS.get(strategy_name, (0, 2))
        # PATCH_V14: MAX_DTE_TRADEABLE is the operator's single ceiling and it
        # was dead - this table hardcoded 4 and the two hard gates hardcoded 6.
        # The table still sets the per-structure floor and shape; the Config
        # field can only ever TIGHTEN the top, never widen it, so a typo in
        # env.txt cannot hand the engine a DTE it was never measured on.
        dte_max = min(int(dte_max), int(getattr(self.config, "max_dte_tradeable", 4) or 4))
        if actual_dte < dte_min:
            return {"valid": False, "reason": f"dte_{actual_dte}_below_min_{dte_min}"}
        if actual_dte > dte_max:
            return {"valid": False, "reason": f"dte_{actual_dte}_above_max_{dte_max}"}

        chain        = self.market_engine.last_chain
        chain_expiry = self.market_engine.last_chain_expiry
        if not chain:
            return {"valid": False, "reason": "chain_unavailable"}
        if chain_expiry is None or chain_expiry.isoformat() != expiry_str:
            return {"valid": False, "reason": "chain_expiry_mismatch"}
        if len(chain) < 10:
            return {"valid": False, "reason": f"chain_only_{len(chain)}_strikes"}

        spot = float(signals.get("spot") or 0)
        if not spot:
            return {"valid": False, "reason": "spot_unavailable"}

        legs_spec, err = self._select_strikes(
            strategy_name, chain, spot, actual_dte, signals
        )
        if legs_spec is None:
            return {"valid": False, "reason": f"strike_selection_failed:{err}"}

        validated_legs, err = self._build_validated_legs(legs_spec, chain)
        if validated_legs is None:
            return {"valid": False, "reason": f"leg_validation_failed:{err}"}

        num_legs = len(validated_legs)

        gross_value = 0.0
        for leg in validated_legs:
            bid = float(leg.get("bid", 0) or 0)
            ask = float(leg.get("ask", 0) or 0)
            ltp = float(leg.get("ltp", 0) or 0)
            if leg["action"] == "SELL":
                ep = bid if bid > 0 else (ltp if ltp > 0 else ask)
            else:
                ep = ask if ask > 0 else (ltp if ltp > 0 else bid)
            if ep <= 0:
                ep = float(leg.get("exec_price", 0) or 0)
            gross_value += ep if leg["action"] == "SELL" else -ep

        gross_credit = gross_value
        if gross_credit <= 0:
            return {"valid": False, "reason": f"gross_credit_{gross_credit:.2f}_non_positive"}

        # ── v3.2 [F1] price the cost gates at the size actually traded ─
        # Brokerage is per ORDER. Charging a four-leg structure's eight
        # round-trip orders against ONE lot (as v3.1 did) inflates the
        # modelled friction by the eventual lot count - typically 2-4x -
        # so every credit-versus-friction gate in the engine was
        # comparing against a number the trade would never pay. A
        # provisional size is derived from the risk budget first, the
        # gates are priced at that size, and the FINAL size is
        # re-validated against the same gate further down.
        _wing_est = self._structure_wing_pts(validated_legs) or float(
            signals.get("wing_width") or 150
        )
        _est_lots = self._estimate_lots(
            _wing_est, gross_credit, actual_dte, size_mult, state
        )
        total_slippage   = self._compute_slippage(validated_legs)
        entry_costs_dict = self._compute_costs(validated_legs, _est_lots, "ENTRY")
        _cost_divisor    = C02 * max(_est_lots, 1)
        entry_costs_pts  = (
            entry_costs_dict["total_rupees"] / _cost_divisor
            if _cost_divisor > 0 else 0
        )
        net_credit       = gross_credit - total_slippage - entry_costs_pts

        if net_credit <= 0:
            return {
                "valid": False,
                "reason": f"net_credit_{net_credit:.2f}_non_positive_after_costs",
            }

        day_label = state.get("day_label", "TUESDAY")

        # v3.1: friction is the FULL round trip (entry charges + entry
        # slippage + exit charges + exit slippage), not entry x 1.5. Every
        # gate comparing credit to friction was previously measuring against
        # roughly half the real number, so trades that were cost-negative in
        # reality passed while the label claimed a 4x safety margin.
        friction_pts        = self._round_trip_friction(
            validated_legs, entry_costs_pts
        )
        # v3.2: expressed as a fraction of the credit rather than as a
        # bare multiple, and priced at the size the trade will really be
        # done at. A structure whose round trip eats more than ~28% of
        # the credit has no realistic path to a profit after a single
        # adverse tick, however good the setup looks.
        _fric_frac_cap = float(
            getattr(self.config, "max_friction_frac_of_credit", 0.28)
        )
        min_credit_friction = friction_pts / max(_fric_frac_cap, 0.01)
        if net_credit < min_credit_friction:
            return {
                "valid": False,
                "reason": (
                    f"net_credit_{net_credit:.2f}pts_friction_"
                    f"{friction_pts:.2f}pts_is_"
                    f"{(friction_pts / max(net_credit, 0.01)) * 100:.0f}pct_"
                    f"above_{_fric_frac_cap * 100:.0f}pct_cap"
                ),
            }

        # Fixed brokerage on its own must stay small relative to the
        # credit: it is the one cost that does NOT scale with the size of
        # the edge, and it is what makes tiny credit structures a
        # guaranteed loss no matter how the market behaves.
        _brk_pts = (
            self.config.brokerage_per_order * len(validated_legs) * 2.0
        ) / (C02 * max(_est_lots, 1)) if C02 > 0 else 0.0
        _brk_cap = float(
            getattr(self.config, "max_brokerage_frac_of_credit", 0.15)
        )
        if net_credit > 0 and _brk_pts > net_credit * _brk_cap:
            return {
                "valid": False,
                "reason": (
                    f"brokerage_{_brk_pts:.2f}pts_at_{_est_lots}lots_is_"
                    f"{(_brk_pts / net_credit) * 100:.0f}pct_of_credit_"
                    f"above_{_brk_cap * 100:.0f}pct"
                ),
            }

        # ── v3.2 [F2] structure economics ─────────────────────────────
        # A long wing that costs more than a third of the short it
        # protects is not insurance, it is a second position working
        # against the first: it caps the loss but hands back so much
        # premium that the remaining edge cannot clear the round trip.
        # v4.2: multi-day weekly wings carry vega and cost more relative
        # to their shorts than 0DTE wings; use the DTE-aware cap that the
        # adaptive wing fitter (_fit_wing_width) targets.
        _wing_cost_cap = float(getattr(
            self.config,
            "wing_cost_frac_max_weekly" if actual_dte and actual_dte >= 2
            else "wing_cost_frac_max", 0.50))
        # An iron butterfly sells the at-the-money straddle, so its wings
        # always cost a large share of the shorts - that is the structure,
        # not a defect in it. The fly is governed by its credit/wing ratio
        # instead, which is already checked above.
        # v3.10: the "wing costs > 50% of the short" check is a 4-leg condor
        # concept — two wings each eating premium. A single-sided vertical's
        # one long leg IS the risk definition, and its cost relative to the
        # short is just the spread geometry; the credit/wing ratio below is
        # the correct gate for it. Apply the wing-cost check to condors only.
        for _side in (
            ("call", "put")
            if strategy_name == IRON_CONDOR
            else ()
        ):
            _s_prem = sum(
                float(l.get("exec_price") or 0) for l in validated_legs
                if l["action"] == "SELL" and l["option_type"] == _side
            )
            _b_prem = sum(
                float(l.get("exec_price") or 0) for l in validated_legs
                if l["action"] == "BUY" and l["option_type"] == _side
            )
            if _s_prem > 0 and _b_prem > _s_prem * _wing_cost_cap:
                return {
                    "valid": False,
                    "reason": (
                        f"{_side}_wing_costs_{(_b_prem / _s_prem) * 100:.0f}pct_"
                        f"of_short_premium_above_{_wing_cost_cap * 100:.0f}pct"
                    ),
                }

        # A four-legged condor whose weaker side contributes almost
        # nothing is paying two extra legs of brokerage and two extra
        # spreads to collect a rounding error. The correct structure in
        # that situation is the single-sided vertical, which the regime
        # engine will select on its own once price confirms a direction.
        if strategy_name == IRON_CONDOR:
            _side_credit = {}
            for _side in ("call", "put"):
                _side_credit[_side] = sum(
                    (float(l.get("exec_price") or 0)
                     if l["action"] == "SELL"
                     else -float(l.get("exec_price") or 0))
                    for l in validated_legs if l["option_type"] == _side
                )
            _tot_side = sum(max(v, 0.0) for v in _side_credit.values())
            _weak = min(_side_credit.values()) if _side_credit else 0.0
            _weak_min = float(
                getattr(self.config, "condor_weak_side_min_frac", 0.30)
            )
            if _tot_side > 0 and _weak < _tot_side * _weak_min:
                return {
                    "valid": False,
                    "reason": (
                        f"condor_weak_side_only_"
                        f"{(_weak / _tot_side) * 100:.0f}pct_of_credit_"
                        f"two_extra_legs_not_paid_for"
                    ),
                }

        actual_wing_pts = None
        if strategy_name in (IRON_CONDOR, IRON_BUTTERFLY):
            sl = [l for l in validated_legs if l["action"] == "SELL"]
            bl = [l for l in validated_legs if l["action"] == "BUY"]
            if sl and bl:
                cs = [l["strike"] for l in sl if l["option_type"] == "call"]
                cb = [l["strike"] for l in bl if l["option_type"] == "call"]
                if cs and cb:
                    actual_wing_pts = abs(max(cb) - min(cs))
        elif strategy_name in (BULL_PUT_SPREAD, BEAR_CALL_SPREAD):
            sl = [l for l in validated_legs if l["action"] == "SELL"]
            bl = [l for l in validated_legs if l["action"] == "BUY"]
            if sl and bl:
                actual_wing_pts = abs(sl[0]["strike"] - bl[0]["strike"])

        wing_for_ratio        = actual_wing_pts or 150
        structural_loss_ratio = max(wing_for_ratio - net_credit, 1.0)
        credit_risk_ratio     = net_credit / structural_loss_ratio

        now_t3 = now_ist().time()
        if actual_dte == 0:
            total_mins3   = 375.0
            elapsed_mins3 = max(0.0, (
                datetime.combine(today_ist(), now_t3) -
                datetime.combine(today_ist(), dtime(9, 15))
            ).total_seconds() / 60.0)
            mins_left3 = max(total_mins3 - elapsed_mins3, 30)
            # v3.9: the ladder was calibrated against the premium a VIX
            # 13.5 session pays. At VIX 11 the market sells ~0.8x of
            # that, so a fixed-absolute ladder structurally vetoed every
            # expiry structure (measured 2026-09-08: ratio ~0.105-0.11
            # against a fixed 0.16 demand, all 86 surviving candidates
            # rejected). Requirements now scale with the vol the session
            # is actually offering, clamped at both ends.
            _lx = (
                float(getattr(self.config, "credit_risk_ratio_dte0_early", 0.16))
                if mins_left3 > 180 else
                (float(getattr(self.config, "credit_risk_ratio_dte0_mid", 0.13))
                 if mins_left3 > 90 else
                 float(getattr(self.config, "credit_risk_ratio_dte0_late", 0.10)))
            )
            _vix_l = float(signals.get("vix") or 13.5)
            _vref  = float(getattr(self.config, "credit_ratio_vix_ref", 13.5))
            _vs    = min(max(_vix_l / max(_vref, 1.0), 0.75), 1.15)
            min_ratio = _lx * _vs
        else:
            min_ratio = 0.12

        if credit_risk_ratio < min_ratio:
            return {
                "valid": False,
                "reason": (
                    f"credit_risk_ratio_{credit_risk_ratio:.3f}_below_min_{min_ratio:.3f}"
                ),
            }

        if actual_wing_pts and actual_wing_pts > 0:
            ratio     = net_credit / actual_wing_pts
            min_ratio_wing = (
                MIN_CREDIT_RATIO_DTE0.get(strategy_name, 0.14)
                if actual_dte == 0
                else MIN_CREDIT_RATIO.get(strategy_name, 0.10)
            )
            if ratio < min_ratio_wing:
                return {
                    "valid": False,
                    "reason": (
                        f"credit_ratio_{ratio:.3f}_below_min_{min_ratio_wing:.3f}"
                    ),
                }

        target_pct    = self._get_target_pct(actual_dte, signals)
        # v3.2: the profit target has to clear the WHOLE round trip, not
        # just the exit. v3.1 compared the target against exit costs only,
        # so the entry brokerage, entry STT and entry spread were counted
        # nowhere in this gate - the one gate whose entire job is to ask
        # "is the money we are trying to make bigger than the money it
        # costs to try". Its own reported margin was therefore roughly
        # double the truth.
        exit_costs    = entry_costs_pts * 0.95 + self._compute_slippage(
            validated_legs, is_exit=True
        )
        expected_edge = net_credit * target_pct - friction_pts
        vix           = float(signals.get("vix") or 11.0)

        _tgt_over_fric = float(
            getattr(self.config, "min_target_over_friction", 1.25)
        )
        if vix >= 15.0:
            _tgt_over_fric *= 1.15
        _min_gross_target = friction_pts * _tgt_over_fric

        if net_credit * target_pct < _min_gross_target:
            return {
                "valid": False,
                "reason": (
                    f"target_{net_credit * target_pct:.2f}pts_below_"
                    f"{_tgt_over_fric:.2f}x_roundtrip_friction_"
                    f"{friction_pts:.2f}pts(edge={expected_edge:.2f})"
                ),
            }

        # v3.1: the stop level is now computed BEFORE the EV gate so the gate
        # prices the loss leg the engine will actually take, instead of a
        # hardcoded 1.5 x credit that bore no relationship to stop_premium.
        # ── v3.2 [G0] the stop that decides whether this can be a ─────
        # profitable system at all.
        #
        # v3.1 stopped at 2.5x the credit and targeted 35% of it:
        #     loss on stop = 1.5C, gain on win = 0.35C
        #     break-even win rate = 1.5 / 1.85 = 81%, before friction.
        # No 0.15-0.22 delta NIFTY structure survives an 81% bar - the
        # engine's own p_win table peaks at 0.72. The geometry was
        # negative-expectancy by construction, and the verticals were
        # worse still: a hardcoded 2.5 on the GROSS credit, so the stop
        # ignored the costs already paid to get in.
        #
        # Stops are now DTE-aware multiples of the NET credit, identical
        # for every structure, and capped at the structural loss - you
        # cannot lose more than the wing, so a stop above it is fiction
        # that only serves to oversize the position.
        if actual_dte == 0:
            _stop_mult_pre = float(getattr(self.config, "stop_mult_dte0", 1.40))
        elif actual_dte == 1:
            _stop_mult_pre = float(getattr(self.config, "stop_mult_dte1", 1.55))
        else:
            _stop_mult_pre = float(getattr(self.config, "stop_mult_dte2p", 1.70))
        _stop_mult_pre = min(max(_stop_mult_pre, 1.15), 3.00)
        _stop_premium_pre = net_credit * _stop_mult_pre
        _wing_cap_pre = float(actual_wing_pts or 150)
        if _wing_cap_pre > 0:
            _stop_premium_pre = min(_stop_premium_pre, _wing_cap_pre)
        _stop_premium_pre = max(_stop_premium_pre, net_credit * 1.10)
        # ── PATCH_V15: failed-break vertical is stopped by its thesis ────
        # The structure is sold because the failed extreme held; if the
        # tape revisits it the reason for the trade is gone. Pricing that
        # as a multiple of the credit (1.7x on dte2+) overstates the risk
        # and made every such trade fail the EV gate.
        if signals.get("neutral_range_vertical"):
            try:
                if strategy_name == BULL_PUT_SPREAD:
                    _fb_ref_pre = float(signals.get("day_low_so_far") or 0.0)
                else:
                    _fb_ref_pre = float(signals.get("day_high_so_far") or 0.0)
                _fb_spot_pre = float(signals.get("spot") or 0.0)
                if _fb_ref_pre > 0 and _fb_spot_pre > 0:
                    _fb_risk_pre = max(
                        0.25 * abs(_fb_spot_pre - _fb_ref_pre),
                        0.15 * net_credit,
                    )
                    _stop_premium_pre = min(
                        _stop_premium_pre, net_credit + _fb_risk_pre
                    )
            except (TypeError, ValueError):
                pass

        # v3.2: the spot backstop is computed BEFORE the EV gate so the
        # gate can price the barrier the engine will genuinely defend
        # rather than the short strike it will never let price reach.
        _short_dists_cp = [
            abs(float(l["strike"]) - spot)
            for l in validated_legs if l["action"] == "SELL"
        ]
        _min_short_dist_cp = min(_short_dists_cp) if _short_dists_cp else 0.0
        price_stop_pts = self._price_stop_pts(
            actual_wing_pts or 150, _min_short_dist_cp, spot
        )
        ev_ok, ev_reason = self._compute_ev_gate(
            net_credit, actual_wing_pts or 150,
            entry_costs_pts, total_slippage, signals,
            legs=validated_legs, stop_premium=_stop_premium_pre,
            barrier_pull_pts=price_stop_pts,
        )
        if not ev_ok:
            return {"valid": False, "reason": f"ev_gate:{ev_reason}"}

        current_capital = state.get("current_capital", self.config.starting_capital)
        wing_for_sizing = actual_wing_pts or 150

        # ── [G1] Risk per lot ─────────────────────────────────────────────
        # The old code sized as though the stop always works: it clamped the
        # per-lot loss down to min(stop_loss, 2 x credit). On NIFTY 0DTE the
        # stop is exactly what does NOT hold — a gap or a gamma acceleration
        # through the short strike fills far past it, and the structure is
        # worth (wing - credit) against you. Sizing on the stop therefore
        # oversized every position by roughly 2-4x, which is the textbook way
        # a premium-selling account is destroyed by a single session.
        #
        # Sizing is now anchored on the STRUCTURAL loss, with only partial
        # credit for the stop working (config.stop_efficacy, hard-capped at
        # 0.80 in load_config so this can never be switched off entirely).
        _structural_per_lot = max((wing_for_sizing - net_credit) * C02, 1.0)
        _stop_loss_per_lot  = max(
            (float(_stop_premium_pre) - net_credit) * C02, 0.0
        )
        _efficacy = float(getattr(self.config, "stop_efficacy", 0.55))
        _efficacy = min(max(_efficacy, 0.0), 0.80)
        # PATCH_V25: size failed-LOW on the thesis stop, not the 300-pt wing.
        if (signals.get("neutral_range_vertical")
                and strategy_name == BULL_PUT_SPREAD):
            _efficacy = 0.80
        structural_loss_per_lot = (
            _efficacy * _stop_loss_per_lot
            + (1.0 - _efficacy) * _structural_per_lot
        )
        # Never assume less risk than the stop itself; never more than the
        # structural maximum.
        structural_loss_per_lot = min(
            max(structural_loss_per_lot, _stop_loss_per_lot, 1.0),
            _structural_per_lot,
        )

        # ── [G4] Risk budget ──────────────────────────────────────────────
        # config.max_risk_per_trade_pct is loaded, safety-clamped at startup
        # to below MAX_DAILY_LOSS_PCT/3, and was then ignored completely in
        # favour of this hardcoded table — so tightening the configured risk
        # limit had literally no effect on position size, and the daily-loss
        # arithmetic the clamp was protecting did not hold. The table is now
        # expressed as a FRACTION of the configured budget.
        _budget = float(self.config.max_risk_per_trade_pct or 0.006)
        # Single-sourced with _risk_fraction_for_dte (always 1.0 now): the
        # DTE discount lives only in the regime layer's dte_mult. An inline
        # copy of the old table here double-counted it.
        risk_pct  = _budget * self._risk_fraction_for_dte(actual_dte)
        max_risk  = current_capital * risk_pct
        raw_lots  = max_risk / structural_loss_per_lot
        # ── v3.2 [G5] minimum economic size ───────────────────────────
        # max(1, int(...)) forced a one-lot trade whenever the risk
        # budget said less than one lot - routinely 2-3x the intended
        # risk, and always on the trades the engine was least confident
        # about (size_mult is small exactly when confidence is low). It
        # also truncated 1.9 lots to 1. Below a configurable fraction of
        # a lot the correct professional action is not to trade.
        _sized = raw_lots * max(float(size_mult), 0.0)
        _min_frac = float(getattr(self.config, "min_lots_fraction", 0.60))
        _clipped_to_minimum = False
        if _sized < _min_frac:
            # Minimum-ticket affordability. size_mult is a continuous
            # fraction but NIFTY trades in discrete 65-lot tickets: when
            # the scaled budget wants less than a ticket yet ONE ticket
            # fits the UNSCALED per-trade budget on a high-quality setup,
            # trading the single ticket IS the risk-managed action — the
            # alternative is not "safer", it is idle capital. Without
            # this, any stack of day/dte/event schedule discounts below
            # ~0.42 lots permanently bans trading (measured 2026-09-09:
            # 0.65 x 0.30 x 0.25 = 0.049 on a HIGH-confidence setup whose
            # 1-lot blended risk was ~0.42% of capital, inside the 0.6%
            # budget). Edge was already approved by the EV gate above;
            # the clip additionally requires clear signal quality (never
            # on UNCLEAR positioning or a borderline VRP read) and yields
            # exactly one lot, still subject to every check below.
            _pos_clip = signals.get("positioning_regime", "")
            _bl_clip  = bool(signals.get("borderline_sell", False))
            if (raw_lots >= 1.0 and _pos_clip != "UNCLEAR"
                    and not _bl_clip):
                _clipped_to_minimum = True
                self.logger.info(
                    f"Minimum-ticket clip: scaled {_sized:.3f} lots below "
                    f"min {_min_frac:.2f}, but 1 lot fits the per-trade "
                    f"budget (raw {raw_lots:.2f}) on a clear setup "
                    f"(pos={_pos_clip}) — trading 1 lot"
                )
            else:
                return {
                    "valid": False,
                    "reason": (
                        f"risk_budget_allows_only_{_sized:.2f}_lots_below_"
                        f"min_{_min_frac:.2f}_forcing_1_lot_would_be_"
                        f"{(1.0 / max(_sized, 0.01)):.1f}x_intended_risk"
                    ),
                }
        final_lots = 1 if _clipped_to_minimum else max(1, int(round(_sized)))
        # PATCH_V25: 3-lot cap on the failed-LOW scalp (clears Rs 2k after costs).
        if (signals.get("neutral_range_vertical")
                and strategy_name == BULL_PUT_SPREAD):
            final_lots = min(final_lots, 3)
        if signals.get("afternoon_high_fade") and strategy_name == BEAR_CALL_SPREAD:
            final_lots = min(max(final_lots, 3), 4)
        if signals.get("afternoon_low_fade") and strategy_name == BULL_PUT_SPREAD:
            final_lots = min(max(final_lots, 3), 4)

        # ── [G2] Day cap ──────────────────────────────────────────────────
        # int(capital / starting_capital) is a step function: the cap doubles
        # the instant equity doubles and does nothing in between — the single
        # worst moment to double exposure is right after a run-up. Continuous
        # square-root-of-equity scaling grows exposure smoothly and slows it
        # as the account grows, which is the standard convention.
        _equity_scale = max(
            (current_capital / float(self.config.starting_capital or 1.0)) ** 0.5,
            0.35,
        )
        day_cap = max(1, int(LOT_CAPS_BY_DAY.get(day_label, 3) * _equity_scale))
        final_lots = min(final_lots, day_cap)

        if structural_loss_per_lot * final_lots > max_risk * 1.5:
            final_lots = max(1, int(max_risk / structural_loss_per_lot))

        # v3.2: the old unconditional "condors always get at least one
        # lot" override is gone - the minimum-economic-size gate above
        # already decided whether this trade is worth doing at all.
        final_lots = max(1, int(final_lots))

        # ── v3.10 [G6] fixed-cost amortization floor ─────────────────
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
        # PATCH_V12: the fixed-cost floor never fires on event days,
        # is capped at 3 lots, and re-checks the structural guardrail
        # it used to jump over. Measured 2026-09-11 (CPI): a 0.14
        # size schedule was floored to the 5-lot day MAXIMUM on a
        # thin, unmeasured setup; the stop cost Rs 3,924.
        _is_event_floor = bool(signals.get("event_day", False))
        if (
            final_lots == 1
            and raw_lots >= 1.5
            and signals.get("confidence_level") == "HIGH"
            and not bool(signals.get("borderline_sell", False))
            and not _is_event_floor
        ):
            _floor_lots = min(int(round(raw_lots)), day_cap, 3)
            if _floor_lots >= 2:
                self.logger.info(
                    f"Fixed-cost floor: budget supports {raw_lots:.2f} "
                    f"risk-correct lots but size schedule left 1 lot; "
                    f"sizing to {_floor_lots} lots (day cap {day_cap})"
                )
                final_lots = _floor_lots
                if structural_loss_per_lot * final_lots > max_risk * 1.5:
                    final_lots = max(1, int(max_risk / structural_loss_per_lot))

        # ── v3.2 [F3] re-validate the economics at the FINAL size ─────
        # The gates above were priced at the provisional lot count. If
        # the daily-loss projection, the margin cap or the day cap has
        # cut the size since then, the per-lot fixed costs have risen and
        # the trade may no longer be worth doing. This is the check that
        # stops the engine from trading a structure whose edge evaporated
        # the moment it was made smaller.
        _final_costs = self._compute_costs(validated_legs, final_lots, "ENTRY")
        _final_div   = C02 * max(final_lots, 1)
        _final_costs_pts = (
            _final_costs["total_rupees"] / _final_div if _final_div > 0 else 0.0
        )
        _final_net_credit = gross_credit - total_slippage - _final_costs_pts
        _final_friction = self._round_trip_friction(
            validated_legs, _final_costs_pts
        )
        if _final_net_credit <= 0:
            return {
                "valid": False,
                "reason": (
                    f"net_credit_{_final_net_credit:.2f}_non_positive_at_"
                    f"final_size_{final_lots}_lots"
                ),
            }
        if _final_friction > _final_net_credit * max(_fric_frac_cap, 0.01):
            return {
                "valid": False,
                "reason": (
                    f"friction_{_final_friction:.2f}pts_is_"
                    f"{(_final_friction / _final_net_credit) * 100:.0f}pct_"
                    f"of_credit_at_final_size_{final_lots}_lots"
                ),
            }
        entry_costs_pts = _final_costs_pts
        entry_costs_dict = _final_costs
        net_credit = _final_net_credit
        friction_pts = _final_friction
        target_premium = net_credit * (1.0 - target_pct)
        _stop_premium_pre = min(
            max(net_credit * _stop_mult_pre, net_credit * 1.10),
            float(actual_wing_pts or 150),
        )

        stop_mult    = _stop_mult_pre
        stop_premium = _stop_premium_pre

        max_loss_per_lot = structural_loss_per_lot
        # v3.2: `structural_loss_per_lot` is the stop-efficacy BLEND used
        # for sizing - it deliberately assumes the stop usually works.
        # That is the right number for deciding how big to go and the
        # wrong one for asking "if this trade goes to max loss, do we
        # breach the daily cap", which is precisely what
        # execution_engine does with it. The unblended structural loss is
        # published alongside so the survival check can use the survival
        # number.
        _true_max_loss_per_lot = float(_structural_per_lot)

        # ── [G3] Margin ───────────────────────────────────────────────────
        # The old model added 2% x spot x lot x n_shorts of exposure margin on
        # top of the wing margin on 0DTE. That is the NAKED-option convention.
        # For a fully hedged, defined-risk vertical or condor the exchange
        # requirement is essentially the spread's maximum loss plus a modest
        # add-on; at a 26,000 index the old term was roughly six times the
        # wing margin, which pinned final_lots at 1 and threw away most of the
        # achievable return without reducing any actual risk. Every short leg
        # here is hedged by construction (the builders reject unhedged
        # structures), so the add-on is a small percentage — and the genuine
        # naked formula is retained for the case where a leg really is naked.
        _shorts = [l for l in validated_legs if l["action"] == "SELL"]
        _longs  = [l for l in validated_legs if l["action"] == "BUY"]
        _fully_hedged = len(_longs) >= len(_shorts) and len(_longs) > 0
        _wing_margin = (actual_wing_pts or 150) * C02 * 1.10
        if _fully_hedged:
            # Add-on for expiry-day margin tightening and broker buffer.
            _addon = 0.18 if actual_dte == 0 else 0.10
            margin_per_lot = _wing_margin * (1.0 + _addon)
        else:
            _spot_ref = float(signals.get("spot") or 0) or 24000.0
            _n_naked  = max(len(_shorts) - len(_longs), 0)
            margin_per_lot = _wing_margin + 0.02 * _spot_ref * C02 * _n_naked
        total_margin   = margin_per_lot * final_lots
        if total_margin > current_capital * 0.80 and final_lots > 1:
            final_lots   = max(1, int(current_capital * 0.80 / margin_per_lot))
            total_margin = margin_per_lot * final_lots

        target_premium   = net_credit * (1.0 - target_pct)
        opening_straddle = float(signals.get("opening_straddle_pts") or 0)
        # price_stop_pts was computed above, coherently with the wing and
        # the short-strike distance, and already used by the EV gate.

        # ── v3.2: the spot backstop must sit on the side of the short ─
        # strike that price has to travel TOWARD, and for an at-the-money
        # structure that is the far side. v3.1 wrote
        # `short_call_strike - price_stop_pts` unconditionally, so on an
        # iron butterfly (short strike == spot) the call stop level came
        # out BELOW the current spot and the put stop level ABOVE it:
        # both were already breached the instant the position was opened,
        # and priority 3 flattened the trade on its first monitoring
        # cycle, every single time. The butterfly - the engine's chosen
        # structure for its highest-conviction, very-narrow-range days -
        # could not be held for one cycle.
        _atm_stop_pts = max(float(actual_wing_pts or 150) * 0.55,
                            price_stop_pts)
        price_stop_call = price_stop_put = None
        for leg in validated_legs:
            if leg["action"] == "SELL":
                if leg["option_type"] == "call":
                    _lvl = leg["strike"] - price_stop_pts
                    if spot > 0 and _lvl <= spot + 5.0:
                        _lvl = leg["strike"] + _atm_stop_pts
                    price_stop_call = _lvl
                elif leg["option_type"] == "put":
                    _lvl = leg["strike"] + price_stop_pts
                    if spot > 0 and _lvl >= spot - 5.0:
                        _lvl = leg["strike"] - _atm_stop_pts
                    price_stop_put = _lvl

        hard_exit_str = state.get(
            "hard_exit_time", self.config.hard_exit_time.strftime("%H:%M")
        )

        return {
            "valid":                  True,
            "strategy_name":          strategy_name,
            "strategy_type":          SELL,
            "selection_reason":       selection_reason,
            "target_expiry":          expiry_str,
            "actual_dte":             actual_dte,
            "legs":                   validated_legs,
            "num_legs":               num_legs,
            "gross_credit":           round(gross_credit, 3),
            "entry_credit":           round(net_credit, 3),
            "total_slippage":         round(total_slippage, 3),
            "total_costs_pts":        round(entry_costs_pts, 4),
            "total_costs_rupees_per_lot": round(entry_costs_pts * C02, 2),
            "total_fixed_costs_rupees":   0.0,
            "entry_costs_rupees":     round(entry_costs_dict["total_rupees"], 2),
            "round_trip_friction_pts": round(friction_pts, 3),
            "stop_multiple":          round(stop_mult, 3),
            "stop_premium":           round(stop_premium, 3),
            "target_premium":         round(target_premium, 3),
            "price_stop_pts":         price_stop_pts,
            "price_stop_level_call":  price_stop_call,
            "price_stop_level_put":   price_stop_put,
            "hard_exit_time":         hard_exit_str,
            "target_pct":             target_pct,
            "final_lots":             final_lots,
            "max_loss_per_lot":       round(max_loss_per_lot, 2),
            "total_max_risk":         round(max_loss_per_lot * final_lots, 2),
            "structural_max_loss_per_lot": round(_true_max_loss_per_lot, 2),
            "total_structural_risk":  round(_true_max_loss_per_lot * final_lots, 2),
            "estimated_margin":       round(total_margin, 2),
            "wing_width":             actual_wing_pts,
            "last_known_premium":     round(net_credit, 3),
            "entry_spot":             spot,
            "entry_vix":              signals.get("vix"),
            "entry_vrp":              signals.get("vrp_smoothed"),
            "entry_vrp_smoothed":     signals.get("vrp_smoothed"),
            "opening_straddle_at_entry": opening_straddle,
            "vol_regime_at_entry":    signals.get("vol_regime"),
            "price_regime_at_entry":  signals.get("price_regime"),
            "positioning_at_entry":   signals.get("positioning_regime"),
            "confidence_level_at_entry": signals.get("confidence_level"),
            "confidence_score_at_entry": signals.get("confidence_score"),
            "final_regime_at_entry":  signals.get("final_regime"),
            "defined_risk_only":      bool(signals.get("defined_risk_only", False)),
            "event_day":              bool(signals.get("event_day", False)),
            "event_name":             signals.get("event_name", ""),
            "borderline_sell":        bool(signals.get("borderline_sell", False)),
            "is_borderline_sell":     int(bool(signals.get("borderline_sell", False))),
            "calibration_tier_at_entry": (
                self._get_calibration().calibration_tier
                if self._get_calibration() else 0
            ),
            "profit_lock_activated":  False,
            "profit_lock_stop_level": None,
            "stop_at_breakeven":      False,
            # PATCH_V25: exit ladder harvests failed-LOW as a scalp.
            "failed_break_scalp":     bool(
                (signals.get("neutral_range_vertical")
                 and strategy_name == BULL_PUT_SPREAD)
                or signals.get("afternoon_low_fade")
            ),
            "afternoon_high_fade":    bool(signals.get("afternoon_high_fade")),
            "afternoon_low_fade":     bool(signals.get("afternoon_low_fade")),
            "max_hold_min":           (
                int(getattr(self.config, "failed_break_max_hold_min", 70))
                if (
                    (signals.get("neutral_range_vertical")
                     and strategy_name == BULL_PUT_SPREAD)
                    or signals.get("afternoon_low_fade")
                )
                else None
            ),
        }

    def _log_decision(
        self,
        signals:       dict,
        action:        str,
        reason:        str,
        strategy_name: str = "",
        params:        Optional[dict] = None,
    ) -> None:
        print_section(f"STRATEGY DECISION @ {now_ist().strftime('%H:%M:%S')}")
        if action == "NO_TRADE":
            print(f"  ACTION : NO_TRADE")
            print(f"  REASON : {reason}")
            self.logger.info(f"NO_TRADE: {reason}")
        else:
            print(f"  ACTION   : {action}")
            print(f"  STRATEGY : {strategy_name}")
            print(f"  REASON   : {reason}")
            if params:
                print_kv_table({
                    "Legs":          params["num_legs"],
                    "Net Credit":    params.get("entry_credit"),
                    "Stop":          params.get("stop_premium"),
                    "Target":        params.get("target_premium"),
                    "Final Lots":    params.get("final_lots"),
                    "Max Risk (Rs)": params.get("total_max_risk"),
                    "DTE":           params.get("actual_dte"),
                    "Vol Regime":    params.get("vol_regime_at_entry"),
                }, title="TRADE PARAMETERS")
                print("\n  LEGS:")
                for leg in params.get("legs", []):
                    iv_pct = float(leg.get("iv") or 0) * 100
                    if iv_pct < 1.0:
                        iv_pct *= 100
                    print(
                        f"    {leg['action']:<4} {leg['option_type'].upper():<4} "
                        f"{leg['strike']:.0f} @ {leg['exec_price']:.2f}  "
                        f"(delta={leg.get('delta', 0):.3f}, iv={iv_pct:.1f}%)"
                    )
            self.logger.info(
                f"STRATEGY DECISION: {action} {strategy_name} — {reason}"
            )
        print()

    def _persist_decision(
        self,
        signals:       dict,
        strategy_name: str,
        reason:        str,
        params:        Optional[dict],
        action:        str,
    ) -> None:
        safe_signals = {
            k: v for k, v in signals.items()
            if k not in ("conditions_met", "conditions_not_met")
        }
        try:
            self.db.insert("strategy_decisions", {
                "decision_time": now_ist().isoformat(),
                "trading_date":  signals.get("trading_date", today_ist().isoformat()),
                "action":        action,
                "strategy_name": strategy_name,
                "reason":        reason,
                "params_json":   json.dumps(params, default=str) if params else None,
                "signals_json":  json.dumps(safe_signals, default=str),
            })
        except Exception as e:
            self.logger.debug(f"strategy_decisions insert error: {e}")

    def _log_phantom_if_neutral(
        self,
        signals:      dict,
        block_reason: str,
    ) -> None:
        if not self.config.phantom_trade_tracking:
            return
        try:
            final_regime = signals.get("final_regime", "NO_TRADE")
            if final_regime in ("NO_TRADE", "ABORT", None):
                return
            strategy_name, _ = self._map_regime_to_strategy(signals)
            if strategy_name == "NO_TRADE":
                return
            chain = self.market_engine.last_chain
            spot  = float(signals.get("spot") or 0)
            dte   = signals.get("actual_dte")
            if not chain or not spot:
                return
            legs_spec, _ = self._select_strikes(strategy_name, chain, spot, dte, signals)
            if not legs_spec:
                return
            credit = 0.0
            for spec in legs_spec:
                strike   = float(spec["strike"])
                opt_type = str(spec["option_type"])
                action   = str(spec["action"])
                if strike in chain:
                    opt = chain[strike].get(opt_type, {})
                    bid = float(opt.get("bid", 0) or 0)
                    ask = float(opt.get("ask", 0) or 0)
                    mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0
                    credit += mid if action == "SELL" else -mid
            self.cal_engine.log_phantom_trade(
                signals=signals,
                block_reason=block_reason,
                strategy_would_be=strategy_name,
                strikes_json=json.dumps(legs_spec),
                credit_would_be=round(credit, 3),
            )
        except Exception as e:
            self.logger.debug(f"Phantom trade logging error: {e}")

    # ═════════════════════════════════════════════════════════════════
    #  v5 — LONG-PREMIUM MOMENTUM EXPRESSION
    # ═════════════════════════════════════════════════════════════════
    #
    # WHAT THIS IS
    # A confirmed intraday trend on NIFTY is worth far more than a far-OTM
    # weekly vertical pays for an intraday hold. When the sell side is
    # refused — the DTE-2 (Friday) calendar branches, a wide opening range
    # that is dangerous to sell into, or a vol regime the engine itself
    # classifies as BUY_OPTIONS — the SAME directional read is expressed by
    # buying the ATM-side option instead. It is not a new signal, it is a
    # substitution of expression, and it is deliberately a fallback: the
    # premium-selling routes keep priority whenever they are available.
    #
    # WHAT KEEPS IT SAFE
    #   * trend regime + MEDIUM/HIGH confidence + fast-ADX confirmation
    #   * structural breakout proof: spot beyond the OR extreme in the trend
    #     direction AND beyond VWAP (the two things an intraday desk actually
    #     uses to call a breakout, not a lagging ADX warm-up)
    #   * never buys a vol top: IV EXPANDING/SPIKING, a straddle explosion or
    #     a VIX gap-up over the previous close all veto it
    #   * never chases an exhausted tape (day_move_used ceiling)
    #   * never holds overnight, never trades expiry-day theta decay
    #     (DTE >= 1), never enters inside the closing window
    #   * maximum loss = premium paid, stop on the premium, profit lock and
    #     trail on the way up, hard exit at the day's own exit bell
    #   * one clip per day, sized on the stop distance inside the same
    #     per-trade risk budget the credit book uses, same day cap
    def _momentum_gate(
        self,
        signals:      dict,
        block_reason: str = "",
        _test_time:   Optional[dtime] = None,
    ) -> Tuple[bool, str, int]:
        """Decide whether a long-premium substitute may be considered.

        Returns (allowed, why, direction) with direction +1 (calls) or
        -1 (puts). Every refusal is explicit so the rejection census shows
        exactly which condition the tape failed, the same way the sell-side
        gates do.
        """
        cfg = self.config
        if not bool(getattr(cfg, "momentum_enabled", True)):
            return False, "momentum_disabled", 0

        state = self.market_engine.state
        cur   = _test_time if _test_time is not None else now_ist().time()

        # ── substitution only: the sell side must have been refused ──────
        reason = str(block_reason or "").lower()
        markers = tuple(getattr(cfg, "momentum_block_markers", ())) or ()
        if not any(str(m).lower() in reason for m in markers):
            return False, f"momentum_sell_side_open({reason[:34]})", 0

        # ── calendar: never 0DTE (theta cliff), never a stale far week ───
        dte = signals.get("actual_dte")
        if dte is None:
            return False, "momentum_no_expiry_resolution", 0
        try:
            dte_i = int(dte)
        except (TypeError, ValueError):
            return False, "momentum_dte_unparseable", 0
        if dte_i < int(getattr(cfg, "momentum_min_dte", 1)):
            return False, f"momentum_dte_{dte_i}_below_min", 0
        if dte_i > int(getattr(cfg, "momentum_max_dte", 4)):
            return False, f"momentum_dte_{dte_i}_above_max", 0

        # ── PATCH_V13: which route owns this cycle ───────────────────────
        # The closing hour is a different trade from the morning breakout:
        # the opening range is ancient, the session has 25-75 minutes left,
        # and the classification that matters is the smoothed one (EMA
        # structure, displacement from VWAP, a mature ADX at the strong
        # threshold) rather than the fifteen-minute price regime, which on a
        # closing-hour tape alternates RANGE/CHOPPY/UPTREND while the trend
        # itself never stops. Everything below the route split is shared:
        # the IV stack, the chase cap, the daily clip limit, the flat book
        # requirement, the budget and the stop.
        late = self._in_late_momentum_window(cur)

        # ── the read itself: a trend, not a range with drift ─────────────
        price   = str(signals.get("price_regime") or "")
        ema     = str(signals.get("ema_structure") or "")
        if late:
            # The EMA structure sets the direction; the price regime is only
            # allowed to VETO it, never to supply it (a single-cycle
            # UPTREND print inside a bearish closing hour is the trap this
            # route exists to avoid).
            if ema == "BEARISH":
                direction = -1
            elif ema == "BULLISH":
                direction = 1
            else:
                return False, f"momentum_late_ema_{ema or 'NONE'}_no_direction", 0
            if (direction < 0 and price in ("UPTREND", "STRONG_UPTREND")) or \
               (direction > 0 and price in ("DOWNTREND", "STRONG_DOWNTREND")):
                return False, (
                    f"momentum_late_price_regime_{price}_contradicts_"
                    f"{'call' if direction > 0 else 'put'}_side"
                ), 0
        elif price in ("UPTREND", "STRONG_UPTREND"):
            direction = 1
        elif price in ("DOWNTREND", "STRONG_DOWNTREND"):
            direction = -1
        else:
            return False, f"momentum_needs_trend_got_{price or 'NONE'}", 0

        if str(signals.get("confidence_level") or "") not in ("HIGH", "MEDIUM"):
            return False, (
                f"momentum_confidence_{signals.get('confidence_level')}_insufficient"
            ), 0
        # PATCH_V12: event-day momentum needs HIGH conviction — the
        # schedule is already softened for the substitute, so the
        # read itself must be unambiguous.
        if signals.get("event_day") and str(signals.get("confidence_level") or "") != "HIGH":
            return False, "momentum_event_day_needs_high_confidence", 0

        try:
            adx = float(signals.get("adx_15") or 0.0)
        except (TypeError, ValueError):
            adx = 0.0
        # PATCH_V13: the closing hour pays premium out of a session that is
        # nearly over, so it demands a MEASURED-STRONG trend - the same bar
        # the engine uses everywhere else to separate "trending" from "has
        # drifted" (adx_strong_threshold). The morning breakout route keeps
        # its own, lower bar. Measured across the five recorded sessions:
        # the afternoon ADX on the two days whose closing hour went nowhere
        # (2026-09-08: 12-17, 2026-09-10: 22-27 at 14:40) sits below 28, and
        # the one day whose closing hour carried the session (2026-09-09:
        # 24 -> 31 -> 40 between 14:39 and 15:06) crosses it at 14:45 and
        # never looks back.
        _adx_min = float(getattr(cfg, "momentum_late_adx_min", 28.0)) if late \
            else float(getattr(cfg, "momentum_adx_min", 30.0))
        if adx < _adx_min:
            return False, f"momentum_adx_{adx:.0f}_below_min", 0
        if late and not bool(signals.get("adx_15_mature", False)):
            return False, "momentum_late_adx_immature", 0

        # ── breakout PROOF: through the opening range, in the trend side ─
        spot = float(signals.get("spot") or 0.0)
        if spot <= 0:
            return False, "momentum_no_spot", 0
        if late:
            # PATCH_V13: displacement + a fresh extreme. The opening range
            # was set five hours ago; by the closing hour it is a level, not
            # a reference (2026-09-09: the whole afternoon move happened
            # inside the morning range, so an OR-break test could never see
            # it). What a closing-hour continuation has to prove is that the
            # tape is OFF VWAP by more than noise and is still making ground
            # now.
            try:
                _vd = float(signals.get("vwap_dist_pct") or 0.0)
            except (TypeError, ValueError):
                _vd = 0.0
            _vd_min = float(getattr(cfg, "momentum_late_vwap_dist_min_pct", 0.10))
            if direction < 0 and _vd > -_vd_min:
                return False, (
                    f"momentum_late_put_displacement_{_vd:+.3f}pct_"
                    f"above_{-_vd_min:.2f}pct"
                ), 0
            if direction > 0 and _vd < _vd_min:
                return False, (
                    f"momentum_late_call_displacement_{_vd:+.3f}pct_"
                    f"below_{_vd_min:.2f}pct"
                ), 0
            _fx_ok, _fx_why = self._late_fresh_extreme(signals, direction)
            if not _fx_ok:
                return False, f"momentum_{_fx_why}", 0
        else:
            # The opening range is mandatory: it is the level the trend has to be
            # measured against, and without it "momentum" is just a green candle.
            or_high = float(signals.get("or_high") or 0.0)
            or_low  = float(signals.get("or_low") or 0.0)
            or_w    = float(signals.get("or_width") or 0.0)
            if not signals.get("or_computed") or or_high <= 0 or or_low <= 0:
                return False, "momentum_no_opening_range_to_confirm", 0
            _need = max(5.0, or_w * float(getattr(cfg, "momentum_or_break_frac", 0.15)))
            if direction > 0 and spot < or_high + _need:
                return False, (
                    f"momentum_call_not_through_or_high_{spot:.0f}<{or_high + _need:.0f}"
                ), 0
            if direction < 0 and spot > or_low - _need:
                return False, (
                    f"momentum_put_not_through_or_low_{spot:.0f}>{or_low - _need:.0f}"
                ), 0
        vwap = signals.get("vwap")
        try:
            vwap = float(vwap) if vwap else 0.0
        except (TypeError, ValueError):
            vwap = 0.0
        if vwap > 0:
            _vbuf = float(getattr(cfg, "momentum_vwap_buffer_pts", 8.0))
            if direction > 0 and spot <= vwap + _vbuf:
                return False, f"momentum_call_at_or_below_vwap_{vwap:.0f}", 0
            if direction < 0 and spot >= vwap - _vbuf:
                return False, f"momentum_put_at_or_above_vwap_{vwap:.0f}", 0

        # ── do not buy a volatility top ─────────────────────────────────
        if str(signals.get("iv_behavior") or "") in ("EXPANDING", "SPIKING"):
            return False, "momentum_iv_expanding_no_chase", 0
        if signals.get("straddle_expanding"):
            return False, "momentum_straddle_expanding", 0
        if signals.get("spot_velocity_block"):
            return False, "momentum_spot_velocity_too_fast", 0
        try:
            _vx  = float(signals.get("vix") or 0.0)
            _pvx = float(signals.get("prev_day_vix_close") or 0.0)
        except (TypeError, ValueError):
            _vx = _pvx = 0.0
        if _vx > 0 and _pvx > 0:
            _gap = (_vx / _pvx - 1.0) * 100.0
            if _gap > float(getattr(cfg, "momentum_vix_gap_max_pct", 12.0)):
                return False, f"momentum_vix_gap_{_gap:.0f}pct", 0

        # ── freshness: a day that has already spent its priced range is
        #    not a breakout, it is the trade everyone is already in ───────
        # PATCH_V12: a MEASURED-STRONG trend is exempt from the chase
        # cap. Mature ADX above the strong threshold with a STRONG_*
        # price read is continuation, not exhaustion — the opening
        # spike that inflates the gauge is ancient history by
        # mid-morning (measured 2026-09-15: 2.26x consumed at 11:00
        # with ADX 86 on a tape that fell 230pts further). The cap
        # still refuses plain-trend and immature-read chases, and
        # the 35% premium stop bounds every ticket. No new knob: the
        # strong threshold and the maturity flag are reused.
        try:
            used = float(signals.get("day_move_used_pct") or 0.0)
        except (TypeError, ValueError):
            used = 0.0
        _mom_strong = (
            bool(signals.get("adx_15_mature", False))
            and adx >= float(getattr(cfg, "adx_strong_threshold", 28.0))
            and price in ("STRONG_UPTREND", "STRONG_DOWNTREND")
        )
        if used >= float(getattr(cfg, "momentum_day_move_max_pct", 90.0)) and not _mom_strong:
            return False, f"momentum_day_move_used_{used:.0f}pct_exhausted", 0

        # ── intraday-only timing ─────────────────────────────────────────
        try:
            entry_start = datetime.strptime(
                state.get("entry_start", "09:45"), "%H:%M").time()
            entry_end = datetime.strptime(
                state.get("entry_end", "14:00"), "%H:%M").time()
        except Exception:
            entry_start = cfg.trading_window_start
            entry_end   = cfg.trading_window_last_entry
        try:
            hard_exit = datetime.strptime(
                state.get("hard_exit_time", "15:00"), "%H:%M").time()
        except Exception:
            hard_exit = cfg.hard_exit_time
        if late:
            # PATCH_V13: the closing-hour clock. _in_late_momentum_window
            # already clipped the window end to hard_exit - min_minutes_left,
            # so a Tuesday's 15:00 square-off is handled by the same rule as
            # a 15:20 day. The check is repeated here so the reason string
            # says which bound failed.
            _late_min = float(getattr(cfg, "momentum_late_min_minutes_left", 25))
            _mins_left = self._minutes_to_time(cur, hard_exit)
            if _mins_left < _late_min:
                return False, (
                    f"momentum_late_only_{_mins_left:.0f}min_before_hard_exit"
                ), 0
        else:
            if cur < entry_start:
                return False, f"momentum_before_entry_window_{entry_start}", 0
            if cur > entry_end:
                return False, f"momentum_past_entry_window_{entry_end}", 0
            mins_left = self._minutes_to_time(cur, hard_exit)
            if mins_left < float(getattr(cfg, "momentum_min_minutes_left", 90)):
                return False, (
                    f"momentum_only_{mins_left:.0f}min_before_hard_exit"
                ), 0

        # ── one clip a day, and never beside an open position ───────────
        if self._count_momentum_entries() >= int(
                getattr(cfg, "momentum_max_trades_per_day", 1)):
            return False, "momentum_daily_limit_reached", 0
        if self._count_open_positions() > 0:
            return False, "momentum_position_open", 0
        if state.get("daily_halted"):
            return False, "momentum_daily_halt", 0
        if signals.get("block_new_entries") or signals.get("circuit_breaker_suspected") \
                or signals.get("vix_spike_detected"):
            return False, "momentum_abort_active", 0
        if signals.get("chain_stale"):
            return False, "momentum_chain_stale", 0

        return True, (
            "momentum_gate_open_late_window" if late else "momentum_gate_open"
        ), direction

    def _momentum_pick_strike(
        self,
        chain:    dict,
        spot:     float,
        opt_type: str,
    ) -> Tuple[Optional[float], Optional[str], float]:
        """Pick the long strike: the trend-side option nearest 0.55 |delta|
        whose premium is a sane fraction of spot. 0DTE-style far-OTM lottery
        tickets and deep-ITM futures-substitutes are both out; this is the
        strike a Nifty intraday desk actually buys on a breakout."""
        step  = max(int(self.config.nifty_strike_step or 50), 1)
        c02   = float(self.config.lot_size or 1)
        cfg   = self.config
        pmin  = spot * float(getattr(cfg, "momentum_prem_min_pct_of_spot", 0.0018))
        pmax  = spot * float(getattr(cfg, "momentum_prem_max_pct_of_spot", 0.0090))
        floor = float(getattr(cfg, "momentum_min_prem_pts", 20.0))
        pmin  = max(pmin, floor)
        atm   = int(round(spot / step) * step)
        best_k, best_d, best_p = None, None, None
        for strike, legs in chain.items():
            try:
                k = float(strike)
            except (TypeError, ValueError):
                continue
            # A breakout ticket is ATM-or-further in the trend direction: a
            # call below spot on an upside break is intrinsic, i.e. a futures
            # substitute with theta, which is the worst of both.
            if opt_type == "call" and k < atm - step:
                continue
            if opt_type == "put" and k > atm + step:
                continue
            opt = (legs or {}).get(opt_type) or {}
            bid = float(opt.get("bid", 0) or 0)
            ask = float(opt.get("ask", 0) or 0)
            if bid <= 0 or ask <= 0:
                continue
            prem = ask
            if prem < pmin or prem > pmax:
                continue
            try:
                dlt = abs(float(opt.get("delta", 0) or 0))
            except (TypeError, ValueError):
                dlt = 0.0
            if dlt < 0.35 or dlt > 0.75:
                continue
            score = abs(dlt - 0.55) * 1000.0 + abs(k - atm) / step
            if best_d is None or score < best_d:
                best_k, best_d, best_p = k, score, prem
        if best_k is None:
            return None, "momentum_no_strike_in_premium_and_delta_band", 0.0
        return best_k, None, float(best_p)

    def compute_momentum_params(
        self,
        direction:        int,
        selection_reason: str,
        signals:          dict,
        size_mult:        float,
        late:             bool = False,
    ) -> dict:
        """Build a single-leg long-premium breakout position.

        Deliberately separate from compute_params(): that function is a
        credit-structure pipeline — it rejects non-positive net credit, gates
        a wing against a short, prices a decay target and derives spot stop
        levels from a short strike. None of that exists here. The economics
        of a long option are premium, stop, target, and the same charge and
        sizing discipline the rest of the engine pays.
        """
        cfg        = self.config
        state      = self.market_engine.state
        C02        = float(cfg.lot_size or 1)
        expiry_str = signals.get("active_expiry")
        actual_dte = signals.get("actual_dte")
        if expiry_str is None or actual_dte is None:
            return {"valid": False, "reason": "no_active_expiry"}

        chain        = self.market_engine.last_chain
        chain_expiry = self.market_engine.last_chain_expiry
        if not chain:
            return {"valid": False, "reason": "chain_unavailable"}
        if chain_expiry is None or chain_expiry.isoformat() != expiry_str:
            return {"valid": False, "reason": "chain_expiry_mismatch"}
        if len(chain) < 10:
            return {"valid": False, "reason": f"chain_only_{len(chain)}_strikes"}

        spot = float(signals.get("spot") or 0.0)
        if spot <= 0:
            return {"valid": False, "reason": "spot_unavailable"}

        opt_type   = "call" if direction > 0 else "put"
        strat_name = LONG_CALL if direction > 0 else LONG_PUT
        strike, err, _prem = self._momentum_pick_strike(chain, spot, opt_type)
        if strike is None:
            return {"valid": False, "reason": err}

        ok, verr = self._validate_leg(chain, strike, opt_type, "BUY")
        if not ok:
            return {"valid": False, "reason": f"momentum_leg_invalid:{verr}"}
        exec_price = self._get_exec_price(chain, strike, opt_type, "BUY")
        if exec_price <= 0:
            return {"valid": False, "reason": "momentum_no_exec_price"}

        legs = [{
            "strike":         strike,
            "option_type":    opt_type,
            "action":         "BUY",
            "exec_price":     exec_price,
            "bid":            float(chain[strike][opt_type].get("bid", 0) or 0),
            "ask":            float(chain[strike][opt_type].get("ask", 0) or 0),
            "ltp":            float(chain[strike][opt_type].get("ltp", 0) or 0),
            "delta":          float(chain[strike][opt_type].get("delta", 0) or 0),
            "gamma":          float(chain[strike][opt_type].get("gamma", 0) or 0),
            "vega":           float(chain[strike][opt_type].get("vega", 0) or 0),
            "theta":          float(chain[strike][opt_type].get("theta", 0) or 0),
            "iv":             float(chain[strike][opt_type].get("iv", 0) or 0),
            "oi":             int(chain[strike][opt_type].get("oi", 0) or 0),
            "instrument_key": chain[strike][opt_type].get("instrument_key"),
        }]

        # ── economics, all of it in premium points per lot ──────────────
        entry_slip   = self._compute_slippage(legs, is_exit=False)
        _costs0      = self._compute_costs(legs, 1, "ENTRY")
        entry_costs0 = _costs0["total_rupees"] / max(C02, 1.0)
        friction_pts = self._round_trip_friction(legs, entry_costs0)

        stop_frac = float(getattr(cfg, "momentum_stop_frac", 0.35))
        stop_pts  = exec_price * stop_frac
        # The real per-lot loss when the stop is taken: the premium
        # give-back plus the round trip that had to be paid to find out.
        risk_pts  = stop_pts + friction_pts
        if risk_pts <= 0:
            return {"valid": False, "reason": "momentum_non_positive_risk"}

        # ── edge test: the planned capture must beat the ticket ─────────
        target_frac = float(getattr(cfg, "momentum_target_frac", 0.60))
        expected_pts = exec_price * target_frac
        _min_over = float(getattr(cfg, "min_target_over_friction", 1.25))
        if expected_pts < friction_pts * max(_min_over, 1.0):
            return {
                "valid": False,
                "reason": (
                    f"momentum_expected_capture_{expected_pts:.2f}pts_below_"
                    f"{_min_over:.2f}x_friction_{friction_pts:.2f}pts"
                ),
            }

        # ── size on the stop, inside the engine's per-trade budget ──────
        current_capital = float(
            state.get("current_capital", cfg.starting_capital) or cfg.starting_capital
        )
        budget  = float(cfg.max_risk_per_trade_pct or 0.006)
        # PATCH_V12: 0DTE momentum (newly allowed) risks half the
        # ticket: the theta cliff is real, the stop is the plan.
        _mom_risk_frac = float(getattr(cfg, "momentum_risk_frac_of_budget", 1.0))
        try:
            if int(actual_dte) == 0:
                _mom_risk_frac = min(_mom_risk_frac, float(getattr(cfg, "momentum_dte0_risk_frac", 0.50)))
        except (TypeError, ValueError):
            pass
        # PATCH_V13: a closing-hour ticket risks half the ticket again. The
        # edge is the session's last trend leg, but the ride is bounded by a
        # clock rather than by a thesis, so the budget is halved and the lot
        # count capped below.
        if late:
            _mom_risk_frac = min(
                _mom_risk_frac,
                float(getattr(cfg, "momentum_late_risk_frac", 0.50)),
            )
        max_risk = current_capital * budget * _mom_risk_frac
        risk_per_lot  = risk_pts * C02
        structural_risk_per_lot = exec_price * C02          # premium paid, all of it
        raw_lots = max_risk / max(risk_per_lot, 1.0)
        # PATCH_V12: the size floor respects event days (0.40): a CPI
        # breakout is still sized with the schedule's fear, just not
        # into the ground.
        _mom_floor = float(getattr(cfg, "momentum_size_floor", 0.80))
        if signals.get("event_day"):
            _mom_floor = min(_mom_floor, float(getattr(cfg, "momentum_event_size_floor", 0.40)))
        sched = max(float(size_mult or 1.0), _mom_floor)
        sized = raw_lots * sched
        min_lots = float(getattr(cfg, "momentum_min_lots", 0.60))
        if sized < min_lots:
            if raw_lots >= 1.0:
                sized = 1.0
            else:
                return {
                    "valid": False,
                    "reason": (
                        f"momentum_risk_budget_allows_{sized:.2f}_lots_below_"
                        f"min_{min_lots:.2f}"
                    ),
                }
        day_label = state.get("day_label", "TUESDAY")
        _eq = max((current_capital / float(cfg.starting_capital or 1.0)) ** 0.5, 0.35)
        day_cap = max(1, int(LOT_CAPS_BY_DAY.get(day_label, 3) * _eq))
        final_lots = max(1, min(int(round(sized)), day_cap))
        # PATCH_V12: a 0DTE long-premium ticket is capped at 2 lots:
        # the structural cap below is premium-multiple based and
        # would let a cheap ticket size itself into a cliff.
        try:
            if int(actual_dte) == 0:
                final_lots = min(final_lots, int(getattr(cfg, "momentum_dte0_max_lots", 2)))
        except (TypeError, ValueError):
            pass
        # PATCH_V13: closing-hour clip cap (see the risk fraction above).
        if late:
            final_lots = max(
                1, min(final_lots, int(getattr(cfg, "momentum_late_max_lots", 4)))
            )
        # The sell side caps a position's STRUCTURAL loss (the margin that
        # could actually be called if the stop never filled) at 1.5x the
        # per-trade budget. A long option cannot lose more than the premium
        # paid, and that premium is only fully lost if the contract is still
        # open at expiry — which the hard exit forbids — so the tolerance is
        # wider here, but it is a real cap: it is what stops a Rs 20 far-OTM
        # ticket from being sized into 20 lots because each lot risks pence.
        _struct_cap_mult = float(getattr(cfg, "momentum_structural_risk_cap_mult", 2.5))
        if structural_risk_per_lot * final_lots > max_risk * _struct_cap_mult:
            final_lots = max(
                1, int(max_risk * _struct_cap_mult / max(structural_risk_per_lot, 1.0))
            )
        # capital outlay: a long option is paid for in full, in cash
        debit_per_lot = structural_risk_per_lot + entry_costs0 * C02
        if debit_per_lot * final_lots > current_capital * 0.80:
            final_lots = max(1, int(current_capital * 0.80 / max(debit_per_lot, 1.0)))
        if final_lots < 1:
            return {"valid": False, "reason": "momentum_no_capital_for_one_lot"}

        entry_costs_dict = self._compute_costs(legs, final_lots, "ENTRY")
        entry_costs_pts  = entry_costs_dict["total_rupees"] / max(C02 * final_lots, 1.0)
        net_debit        = exec_price + entry_costs_pts + entry_slip
        target_premium   = net_debit * (1.0 + target_frac)
        stop_premium     = net_debit * (1.0 - stop_frac)
        lock_trigger     = net_debit * (
            1.0 + float(getattr(cfg, "momentum_lock_trigger", 0.25))
        )

        try:
            hard_exit_str = state.get(
                "hard_exit_time", cfg.hard_exit_time.strftime("%H:%M"))
        except Exception:
            hard_exit_str = cfg.hard_exit_time.strftime("%H:%M")

        cal = self._get_calibration()
        return {
            "valid":                  True,
            "strategy_name":          strat_name,
            "strategy_type":          BUY,
            "selection_reason":       selection_reason,
            "target_expiry":          expiry_str,
            "actual_dte":             actual_dte,
            "legs":                   legs,
            "num_legs":               1,
            "gross_credit":           round(-net_debit, 3),
            "entry_credit":           round(-net_debit, 3),
            "total_slippage":         round(entry_slip, 3),
            "total_costs_pts":        round(entry_costs_pts, 4),
            "total_costs_rupees_per_lot": round(entry_costs_pts * C02, 2),
            "total_fixed_costs_rupees":   0.0,
            "entry_costs_rupees":     round(entry_costs_dict["total_rupees"], 2),
            "round_trip_friction_pts": round(friction_pts, 3),
            "stop_multiple":          round(1.0 - stop_frac, 3),
            "stop_premium":           round(stop_premium, 3),
            "target_premium":         round(target_premium, 3),
            "profit_lock_trigger":    round(lock_trigger, 3),
            "price_stop_pts":         None,
            "price_stop_level_call":  None,
            "price_stop_level_put":   None,
            "hard_exit_time":         hard_exit_str,
            "target_pct":             round(target_frac, 3),
            "final_lots":             final_lots,
            "max_loss_per_lot":       round(risk_per_lot, 2),
            "total_max_risk":         round(risk_per_lot * final_lots, 2),
            "structural_max_loss_per_lot": round(structural_risk_per_lot, 2),
            "total_structural_risk":  round(structural_risk_per_lot * final_lots, 2),
            "estimated_margin":       round(debit_per_lot * final_lots, 2),
            "wing_width":             None,
            "last_known_premium":     round(-net_debit, 3),
            "entry_spot":             spot,
            "entry_vix":              signals.get("vix"),
            "entry_vrp":              signals.get("vrp_smoothed"),
            "entry_vrp_smoothed":     signals.get("vrp_smoothed"),
            "opening_straddle_at_entry": float(
                signals.get("opening_straddle_pts") or 0.0),
            "vol_regime_at_entry":    signals.get("vol_regime"),
            "price_regime_at_entry":  signals.get("price_regime"),
            "positioning_at_entry":   signals.get("positioning_regime"),
            "confidence_level_at_entry": signals.get("confidence_level"),
            "confidence_score_at_entry": signals.get("confidence_score"),
            "final_regime_at_entry":  signals.get("final_regime"),
            "defined_risk_only":      True,
            "event_day":              bool(signals.get("event_day", False)),
            "event_name":             signals.get("event_name", ""),
            "borderline_sell":        False,
            "is_borderline_sell":     0,
            "calibration_tier_at_entry": (
                cal.calibration_tier if cal else 0
            ),
            "profit_lock_activated":  False,
            "profit_lock_stop_level": None,
            "stop_at_breakeven":      False,
            "momentum":               True,
            "momentum_direction":     int(direction),
            # PATCH_V13: read by the debit exit ladder - a ticket opened
            # inside the final window rides its ratchet and its stop to the
            # hard exit instead of being banked at breakeven by D4.
            "momentum_late":          bool(late),
        }

    def _momentum_decision(self, signals: dict, block_reason: str) -> Optional[dict]:
        """Long-premium substitute for a refused sell-side structure.

        Returns a complete ENTER decision, or None when the substitute is not
        allowed or does not price up — in which case the caller keeps the
        original refusal, unaltered, as the logged reason.
        """
        if signals.get("afternoon_high_fade") or signals.get("afternoon_low_fade"):
            return None
        if self.market_engine.state.get("last_exit_is_stale_weekly"):
            return None
        if "two_way_wait" in str(block_reason or ""):
            return None
        try:
            ok, why, direction = self._momentum_gate(signals, block_reason)
        except Exception as exc:                      # never lose the day to
            self.logger.warning(f"momentum gate failed: {exc}")  # a new code path
            return None
        if not ok:
            return None

        # PATCH_V13: the gate tags which route opened - the morning breakout
        # or the closing-hour continuation. The closing hour is sized smaller
        # and is exempted from the "bank a long option at breakeven inside
        # the final 45 minutes" rule, which would otherwise flatten a ticket
        # bought inside that window on its first profitable cycle.
        _late = "late_window" in str(why)

        size_mult = max(float(signals.get("size_multiplier") or 0.50), 0.10)
        reason = (
            f"momentum_trend_expression:{'LONG_CALL' if direction > 0 else 'LONG_PUT'}"
            f":dte={signals.get('actual_dte')}:adx={float(signals.get('adx_15') or 0.0):.0f}"
            f":conf={signals.get('confidence_level')}"
            f"{':late_window' if _late else ''}:replacing={block_reason}"
        )
        params = self.compute_momentum_params(
            direction, reason, signals, size_mult, late=_late
        )
        if not params.get("valid"):
            self.logger.info(f"momentum substitute rejected: {params.get('reason')}")
            return None

        strat_name = params["strategy_name"]
        self._log_decision(signals, "STRATEGY_SELECTED", reason, strat_name, params)
        self._persist_decision(signals, strat_name, reason, params, "STRATEGY_SELECTED")
        self.market_engine.finalize_cycle_log(
            f"STRATEGY_SELECTED:{strat_name}", None, self._count_open_positions()
        )
        state = self.market_engine.state
        state["momentum_entries"] = int(state.get("momentum_entries", 0) or 0) + 1
        return {
            "action":        "ENTER",
            "strategy_name": strat_name,
            "reason":        reason,
            "params":        params,
        }

    def decide(self, signals: dict) -> dict:
        # PATCH_V13: session price memory for the closing-hour route. Kept
        # here (not in the data engine) so the series is exactly the set of
        # cycles on which the engine was allowed to act, in live and replay.
        self._note_price(signals)

        gate = self._check_hard_gates(signals)
        if gate:
            action, reason = gate
            if action == "NO_TRADE":
                alt = self._momentum_decision(signals, reason)
                if alt is not None:
                    return alt
            self._log_decision(signals, action, reason)
            self._persist_decision(signals, "NONE", reason, None, action)
            self.market_engine.finalize_cycle_log(
                action, reason, self._count_open_positions()
            )
            return {"action": action, "reason": reason}

        strategy_name, selection_reason = self._map_regime_to_strategy(signals)
        if strategy_name == "NO_TRADE":
            alt = self._momentum_decision(signals, selection_reason)
            if alt is not None:
                return alt
            self._log_decision(signals, "NO_TRADE", selection_reason)
            self._persist_decision(
                signals, "NO_TRADE", selection_reason, None, "NO_TRADE"
            )
            self.market_engine.finalize_cycle_log(
                "NO_TRADE", selection_reason, self._count_open_positions()
            )
            return {"action": "NO_TRADE", "reason": selection_reason}

        # ── PATCH_V13: entry/exit trend symmetry ─────────────────────────
        # The exit ladder ejects a credit vertical that a measured trend has
        # run against, and refuses a symmetric structure only when the tape
        # is not ranging. Opening either one into that same tape is a round
        # trip paid for in advance: the entry, the ladder, the exit costs.
        # The refusal is deliberately NOT answerable by the long-premium
        # substitute - a measured trend against a credit structure is a
        # reason to stand aside in the middle of the session, and the
        # closing-hour route (which is separately gated on a strong trend, a
        # fresh extreme, displacement and its own clock) is the only place
        # this engine pays premium for a trend it did not see at the open.
        _ct_reason = self._counter_trend_entry_refusal(strategy_name, signals)
        if _ct_reason:
            self._log_decision(signals, "NO_TRADE", _ct_reason)
            self._persist_decision(
                signals, strategy_name, _ct_reason, None, "NO_TRADE"
            )
            self.market_engine.finalize_cycle_log(
                "NO_TRADE", _ct_reason, self._count_open_positions()
            )
            return {"action": "NO_TRADE", "reason": _ct_reason}

        rules_ok, rules_reason = self._validate_entry_rules(strategy_name, signals)
        if not rules_ok:
            full_reason = f"strategy_rules_failed:{rules_reason}"
            # PATCH_V12 (round 3): the substitute is consulted on
            # structure-rule refusals exactly as on hard-gate and
            # economics refusals — the rules are sell-structure-
            # specific (pin veto, OR-mid positioning, delta gates)
            # and a long-premium ticket re-underwrites every one of
            # them in its own gate.
            alt = self._momentum_decision(signals, full_reason)
            if alt is not None:
                return alt
            self._log_decision(signals, "NO_TRADE", full_reason)
            self._persist_decision(
                signals, strategy_name, full_reason, None, "NO_TRADE"
            )
            self.market_engine.finalize_cycle_log(
                "NO_TRADE", full_reason, self._count_open_positions()
            )
            return {"action": "NO_TRADE", "reason": full_reason}

        size_mult = max(float(signals.get("size_multiplier") or 0.50), 0.10)
        # v4.2: regime layer can ask for a smaller clip on fresh-weekly
        # range condors (UNCLEAR OI positioning, or elevated-but-not-strong
        # ADX): the structure is allowed but size is discounted.
        _weekly_discount = signals.get("weekly_range_size_discount")
        # PATCH_V25: failed-LOW sized on thesis stop — floor size_mult at 0.75.
        if (signals.get("neutral_range_vertical")
                and strategy_name == BULL_PUT_SPREAD):
            try:
                size_mult = max(
                    size_mult,
                    float(_weekly_discount or 0.75),
                    0.75,
                )
            except (TypeError, ValueError):
                size_mult = max(size_mult, 0.75)
        elif signals.get("afternoon_high_fade") or signals.get("afternoon_low_fade"):
            try:
                size_mult = max(size_mult, float(_weekly_discount or 0.90), 0.85)
            except (TypeError, ValueError):
                size_mult = max(size_mult, 0.85)
        elif _weekly_discount:
            try:
                size_mult = size_mult * float(_weekly_discount)
            except (TypeError, ValueError):
                pass
        params    = self.compute_params(
            strategy_name, selection_reason, signals, size_mult
        )

        if not params.get("valid"):
            full_reason = f"params_invalid:{params.get('reason', 'unknown')}"
            alt = self._momentum_decision(signals, full_reason)
            if alt is not None:
                return alt
            if "neutral" in full_reason.lower() or "vrp" in full_reason.lower():
                self._log_phantom_if_neutral(signals, full_reason)
            self._log_decision(signals, "NO_TRADE", full_reason)
            self._persist_decision(
                signals, strategy_name, full_reason, None, "NO_TRADE"
            )
            self.market_engine.finalize_cycle_log(
                "NO_TRADE", full_reason, self._count_open_positions()
            )
            return {"action": "NO_TRADE", "reason": full_reason}

        self._log_decision(
            signals, "STRATEGY_SELECTED", selection_reason, strategy_name, params
        )
        self._persist_decision(
            signals, strategy_name, selection_reason, params, "STRATEGY_SELECTED"
        )
        self.market_engine.finalize_cycle_log(
            f"STRATEGY_SELECTED:{strategy_name}", None, self._count_open_positions()
        )
        return {
            "action":        "ENTER",
            "strategy_name": strategy_name,
            "reason":        selection_reason,
            "params":        params,
        }


def _self_test() -> None:
    from datetime import time as dtime
    import tempfile as _tf2
    from core import load_env_file, ENV_FILE, BASE_DIR
    _env2 = load_env_file(ENV_FILE)
    _prod2 = str(BASE_DIR / _env2.get("DB_PATH", "data/nifty_algo_v3.db"))
    print_section("NIFTY ALGO v3.0 — STRATEGY ENGINE SELF-TEST", char="#")

    from core import load_config, Database, RateLimiter, UpstoxClient, setup_logging

    config        = load_config()
    # v3.8: isolate this self-test from the live production
    # database. decide()/_persist_decision writes strategy_decisions;
    # a self-test must never write to the production book. config
    # is left untouched; only the scratch Database is handed in.
    from pathlib import Path as _scratch_path
    db = Database(_scratch_path(_tf2.mkdtemp(
        prefix="strat_selftest_")) / "strat_selftest.db")

    logger        = setup_logging(db, config.log_dir)
    rate_limiter  = RateLimiter(config.rate_limits)
    client        = UpstoxClient(config, rate_limiter, db, logger)
    market_engine = MarketDataEngine(config, db, client, rate_limiter, logger)
    cal_engine    = CalibrationEngine(db, config, logger)
    engine        = StrategyEngine(config, db, market_engine, cal_engine, logger)

    def make_chain(spot: float = 24000.0) -> dict:
        step  = 50
        chain: dict = {}
        atm   = int(round(spot / step) * step)
        for offset in range(-8, 9):
            strike = atm + offset * step
            dist   = abs(offset)
            cd     = max(0.02, 0.50 - dist * 0.04)
            pd     = max(0.02, 0.50 - dist * 0.04)
            cp     = max(0.50, 120 - dist * 10)
            pp     = max(0.50, 120 - dist * 10)
            sp     = max(0.30, cp * 0.05)
            chain[float(strike)] = {
                "call": {
                    "bid":    round(cp - sp / 2, 2),
                    "ask":    round(cp + sp / 2, 2),
                    "ltp":    round(cp, 2),
                    "oi":     max(1000, 50000 - dist * 3000),
                    "volume": 500,
                    "iv":     0.125 + dist * 0.003,
                    "delta":  cd if offset >= 0 else -cd,
                    "gamma":  0.002,
                    "theta":  -0.5,
                    "vega":   15.0,
                    "instrument_key": f"NSE_FO|NIFTY{strike}CE",
                },
                "put": {
                    "bid":    round(pp - sp / 2, 2),
                    "ask":    round(pp + sp / 2, 2),
                    "ltp":    round(pp, 2),
                    "oi":     max(1000, 50000 - dist * 3000),
                    "volume": 500,
                    "iv":     0.125 + dist * 0.003,
                    "delta":  -pd if offset <= 0 else pd,
                    "gamma":  0.002,
                    "theta":  -0.5,
                    "vega":   15.0,
                    "instrument_key": f"NSE_FO|NIFTY{strike}PE",
                },
            }
        return chain

    def make_signals(**overrides) -> dict:
        base = {
            "trading_date":          today_ist().isoformat(),
            "day_label":             "TUESDAY",
            "vix":                   11.5,
            "spot":                  24000.0,
            "atm_strike":            24000,
            "vrp_raw":               3.5,
            "vrp_smoothed":          3.2,
            "atm_iv":                0.125,
            "parkinson_rv":          0.085,
            "iv_behavior":           "STABLE",
            "iv_change_pct_from_open": -2.0,
            "day_move_used_pct":     25.0,
            "opening_straddle_pts":  175.0,
            "or_computed":           True,
            "or_condition":          "NARROW",
            "or_high":               24100.0,
            "or_low":                24040.0,
            "or_width":              60.0,
            "choppy_detected":       False,
            "adx_15":                14.0,
            "adx_60":                12.0,
            "adx_15_mature":         True,
            "adx_condition":         "FLAT",
            "ema_structure":         "NEUTRAL",
            "hh_hl":                 "NEUTRAL",
            "pcr":                   0.95,
            "skew_ratio":            1.15,
            "oi_change_pct":         0.10,
            "resistance_strength":   2.8,
            "support_strength":      2.6,
            "resistance_oi":         120000,
            "support_oi":            110000,
            "total_ce_oi":           500000,
            "total_pe_oi":           480000,
            "chain_size":            71,
            "chain_stale":           False,
            "atm_straddle_price":    175.0,
            "max_pain":              24000.0,
            "actual_dte":            0,
            "active_expiry":         today_ist().isoformat(),
            "circuit_breaker_suspected": False,
            "vix_spike_detected":    False,
            "gap_fade_opportunity":  False,
            "vwap":                  24010.0,
            "vwap_dist_pct":         -0.04,
            "direction":             "NEUTRAL",
            "preferred_sell_side":   "BOTH",
            "wing_width":            150,
            "vol_regime":            "SELL_PREMIUM",
            "price_regime":          "RANGE",
            "positioning_regime":    "STRONG_RANGE",
            "confidence_level":      "HIGH",
            "confidence_score":      0.82,
            "final_regime":          "PREMIUM_SELL_RANGE",
            "final_regime_notes":    "RANGE_STRONG_RANGE_NARROW",
            "size_multiplier":       0.60,
            "raw_size_multiplier":   0.60,
            "block_new_entries":     False,
            "borderline_sell":       False,
            "event_day":             False,
            "event_name":            "",
            "defined_risk_only":     False,
            "is_calibrated":         True,
            "calibration_tier":      2,
        }
        base.update(overrides)
        return base

    market_engine.state.update({
        "or_computed":           True,
        "or_condition":          "NARROW",
        "or_high":               24100.0,
        "or_low":                24040.0,
        "entry_start":           "00:01",
        "entry_end":             "23:58",
        "hard_exit_time":        "23:59",
        "daily_halted":          False,
        "consecutive_stops":     0,
        "entry_count":           0,
        "last_stop_time":        None,
        "last_stop_reason":      "",
        "last_stop_signal_combo":"",
        "last_entry_time":       None,
        "opening_straddle_pts":  175.0,
        "day_label":             "TUESDAY",
        "size_multiplier":       1.0,
        "stop_multiplier":       2.5,
        "wing_width":            150,
        "current_capital":       config.starting_capital,
        "daily_pnl":             0.0,
    })
    mock_chain = make_chain(24000.0)
    market_engine.last_chain        = mock_chain
    market_engine.last_chain_expiry = today_ist()

    print_section("Hard Gates Tests")

    market_engine.state["daily_halted"] = True
    gate = engine._check_hard_gates(make_signals(), _test_time=dtime(11, 0))
    print(f"  Daily halted -> {gate[1] if gate else 'PASS'}")
    assert gate is not None and "daily" in gate[1], f"Expected daily halt, got {gate}"
    market_engine.state["daily_halted"] = False

    gate2 = engine._check_hard_gates(
        make_signals(block_new_entries=True), _test_time=dtime(11, 0)
    )
    print(f"  Block new entries -> {gate2[1][:30] if gate2 else 'PASS'}")
    assert gate2 is not None and "ABORT" in gate2[1]

    gate3 = engine._check_hard_gates(
        make_signals(final_regime="NO_TRADE"), _test_time=dtime(11, 0)
    )
    print(f"  NO_TRADE regime -> {gate3[1][:30] if gate3 else 'PASS'}")
    assert gate3 is not None

    gate4 = engine._check_hard_gates(
        make_signals(iv_behavior="EXPANDING"), _test_time=dtime(11, 0)
    )
    print(f"  IV expanding -> {gate4[1][:30] if gate4 else 'PASS'}")
    assert gate4 is not None and "expanding" in gate4[1]

    market_engine.state.update({
        "last_stop_time": None, "last_stop_reason": "",
        "last_stop_signal_combo": "", "consecutive_stops": 0,
    })
    gate5 = engine._check_hard_gates(
        make_signals(confidence_level="LOW"), _test_time=dtime(11, 0)
    )
    print(f"  LOW confidence -> {gate5[1][:30] if gate5 else 'PASS'}")
    assert gate5 is not None and "confidence" in gate5[1], (
        f"Expected confidence gate, got {gate5}"
    )

    market_engine.state.update({
        "entry_start": "00:01", "entry_end": "23:58",
        "hard_exit_time": "23:59",
        "last_stop_time": None, "last_stop_reason": "",
        "consecutive_stops": 0, "daily_halted": False,
    })
    gate6 = engine._check_hard_gates(make_signals(), _test_time=dtime(11, 0))
    print(f"  All gates pass -> {gate6}")
    assert gate6 is None, f"Expected all gates to pass, got {gate6}"

    print("  [OK] Hard gates tests passed")

    print_section("Regime -> Strategy Mapping Tests")

    strat1, _ = engine._map_regime_to_strategy(
        make_signals(
            final_regime="PREMIUM_SELL_RANGE",
            actual_dte=0, or_condition="NARROW", adx_15=14.0,
            spot=24000.0, atm_strike=24000,
        ),
        _test_time=dtime(10, 0),
    )
    print(f"  PREMIUM_SELL_RANGE, DTE0, NARROW, ADX=14 -> {strat1} (expect IRON_BUTTERFLY after patch)")
    assert strat1 in (IRON_BUTTERFLY, IRON_CONDOR), f"Expected BUTTERFLY or CONDOR, got {strat1}"

    strat2, _ = engine._map_regime_to_strategy(
        make_signals(
            final_regime="PREMIUM_SELL_RANGE",
            actual_dte=0, or_condition="VERY_NARROW",
            adx_15=12.0, adx_15_mature=True,
            vol_regime="SELL_PREMIUM",
            spot=24000.0, atm_strike=24000,
        ),
        _test_time=dtime(10, 0),
    )
    print(f"  PREMIUM_SELL_RANGE, DTE0, VERY_NARROW, ADX=12 -> {strat2} (expect IRON_BUTTERFLY)")
    assert strat2 == IRON_BUTTERFLY, f"Expected IRON_BUTTERFLY, got {strat2}"
    print("  [OK] VERY_NARROW butterfly confirmed")

    strat3, _ = engine._map_regime_to_strategy(
        make_signals(final_regime="PREMIUM_SELL_BULL")
    )
    print(f"  PREMIUM_SELL_BULL -> {strat3} (expect BULL_PUT_SPREAD)")
    assert strat3 == BULL_PUT_SPREAD, f"Expected BULL_PUT_SPREAD, got {strat3}"

    strat4, _ = engine._map_regime_to_strategy(
        make_signals(final_regime="PREMIUM_SELL_BEAR")
    )
    print(f"  PREMIUM_SELL_BEAR -> {strat4} (expect BEAR_CALL_SPREAD)")
    assert strat4 == BEAR_CALL_SPREAD, f"Expected BEAR_CALL_SPREAD, got {strat4}"

    strat_condor, _ = engine._map_regime_to_strategy(
        make_signals(
            final_regime="PREMIUM_SELL_RANGE",
            actual_dte=0, or_condition="WIDE", adx_15=26.0,
            spot=24000.0, atm_strike=24000,
        ),
        _test_time=dtime(10, 0),
    )
    print(f"  PREMIUM_SELL_RANGE, DTE0, WIDE OR, ADX=26 -> {strat_condor} (expect IRON_CONDOR)")
    assert strat_condor == IRON_CONDOR, f"Expected IRON_CONDOR for WIDE OR, got {strat_condor}"
    print("  [OK] Regime mapping tests passed")

    print_section("Entry Rules Validation Tests")

    ok1, _ = engine._validate_entry_rules(
        IRON_BUTTERFLY,
        make_signals(spot=24080.0, atm_strike=24000),
        _test_time=dtime(11, 0),
    )
    assert not ok1, "Expected False for butterfly spot far from ATM"

    ok2, _ = engine._validate_entry_rules(
        IRON_BUTTERFLY,
        make_signals(adx_15=23.0, spot=24000.0, atm_strike=24000),
        _test_time=dtime(11, 0),
    )
    assert not ok2, "Expected False for butterfly with ADX=23 > 22 threshold"

    ok3, r3 = engine._validate_entry_rules(
        IRON_CONDOR, make_signals(), _test_time=dtime(11, 0)
    )
    assert ok3, f"Expected True for condor, got {r3}"

    ok4, _ = engine._validate_entry_rules(
        BULL_PUT_SPREAD,
        make_signals(spot=24030.0, or_high=24100.0, or_low=24040.0),
        _test_time=dtime(11, 0),
    )
    assert not ok4, "Expected False for bull put spot below OR midpoint"

    # The OR-mid veto is counter-trend-bounce protection: it binds in a
    # DOWNTREND regime and stands down in a RANGE regime (the ORB layer
    # already ruled "no breakout" there).
    ok5, _ = engine._validate_entry_rules(
        BEAR_CALL_SPREAD,
        make_signals(spot=24110.0, or_high=24100.0, or_low=24040.0,
                     price_regime="DOWNTREND"),
        _test_time=dtime(11, 0),
    )
    assert not ok5, "Expected False for bear call spot above OR midpoint in DOWNTREND"

    ok5b, r5b = engine._validate_entry_rules(
        BEAR_CALL_SPREAD,
        make_signals(spot=24110.0, or_high=24100.0, or_low=24040.0,
                     price_regime="RANGE"),
        _test_time=dtime(11, 0),
    )
    assert ok5b, f"Expected True for bear call above OR mid in RANGE, got {r5b}"

    print("  [OK] Entry rules validation tests passed")

    print_section("Strike Selection Tests")

    legs1, err1 = engine._select_strikes(
        IRON_CONDOR, mock_chain, 24000.0, 0,
        make_signals(opening_straddle_pts=175.0),
        _test_time=dtime(10, 30),
    )
    assert err1 is None and legs1 is not None, f"Iron Condor error: {err1}"
    sc = next(
        l["strike"] for l in legs1
        if l["action"] == "SELL" and l["option_type"] == "call"
    )
    sp = next(
        l["strike"] for l in legs1
        if l["action"] == "SELL" and l["option_type"] == "put"
    )
    assert sc > sp, "Short call should be above short put"
    assert abs(sc - 24000.0) >= 120, (
        f"Short call should be >=120pts from spot, got {abs(sc-24000):.0f}"
    )
    print(f"  Iron Condor DTE0: SC={sc:.0f} SP={sp:.0f} [OK]")

    legs2, err2 = engine._select_strikes(
        IRON_BUTTERFLY, mock_chain, 24000.0, 0,
        make_signals(),
        _test_time=dtime(10, 30),
    )
    assert err2 is None and legs2 is not None, f"Iron Butterfly error: {err2}"
    print("  Iron Butterfly DTE0: [OK]")

    legs3, err3 = engine._select_strikes(
        BULL_PUT_SPREAD, mock_chain, 24000.0, 0,
        make_signals(opening_straddle_pts=175.0),
        _test_time=dtime(10, 30),
    )
    assert err3 is None and legs3 is not None, f"Bull Put error: {err3}"
    sp3 = next(l["strike"] for l in legs3 if l["action"] == "SELL")
    lp3 = next(l["strike"] for l in legs3 if l["action"] == "BUY")
    assert sp3 > lp3 and (sp3 - lp3) >= 50
    print(f"  Bull Put DTE0: SP={sp3:.0f} LP={lp3:.0f} [OK]")

    legs4, err4 = engine._select_strikes(
        BEAR_CALL_SPREAD, mock_chain, 24000.0, 0,
        make_signals(opening_straddle_pts=175.0),
        _test_time=dtime(10, 30),
    )
    assert err4 is None and legs4 is not None, f"Bear Call error: {err4}"
    sc4 = next(l["strike"] for l in legs4 if l["action"] == "SELL")
    lc4 = next(l["strike"] for l in legs4 if l["action"] == "BUY")
    assert lc4 > sc4
    print(f"  Bear Call DTE0: SC={sc4:.0f} LC={lc4:.0f} [OK]")

    print("  [OK] Strike selection tests passed")

    print_section("Leg Validation Tests")

    ok_v, r_v = engine._validate_leg(mock_chain, 24000.0, "call", "SELL")
    assert ok_v, f"Expected True for valid ATM call, got {r_v}"

    ok_nv, _ = engine._validate_leg(mock_chain, 99999.0, "call", "SELL")
    assert not ok_nv, "Expected False for strike not in chain"

    print("  [OK] Leg validation tests passed")

    print_section("Cost Computation Tests")

    costs = engine._compute_costs([
        {"action": "SELL", "option_type": "call", "exec_price": 45.0},
        {"action": "SELL", "option_type": "put",  "exec_price": 42.0},
        {"action": "BUY",  "option_type": "call", "exec_price": 12.0},
        {"action": "BUY",  "option_type": "put",  "exec_price": 11.0},
    ], 2, "ENTRY")
    assert costs["total_rupees"] > 0, "Costs should be positive"
    assert "stt" in costs["breakdown"], "Should have STT"
    print(f"  4-leg condor 2 lots: Rs{costs['total_rupees']:.2f} [OK]")

    print_section("Slippage Computation Tests")

    legs_ba = [{"bid": 44.0, "ask": 46.0}, {"bid": 41.0, "ask": 43.0}]
    # v3.1: these previously asserted the hardcoded 0.5x / 3.0x multiples, so
    # the test pinned the old double-counted entry model in place rather than
    # verifying the intended behaviour. They now follow the configured
    # multipliers and assert the properties that actually matter.
    _half = (46 - 44) / 2.0
    slip_entry = engine._compute_slippage(legs_ba, is_exit=False)
    expected_entry = 2 * (_half * config.entry_slippage_mult)
    assert abs(slip_entry - expected_entry) < 0.01, (
        f"Entry slippage: expected {expected_entry:.3f}, got {slip_entry:.3f}"
    )
    print(f"  Entry slippage (2 legs bid/ask): {slip_entry:.3f}pts [OK]")

    slip_exit = engine._compute_slippage(legs_ba, is_exit=True)
    expected_exit = 2 * (_half * config.exit_slippage_mult)
    assert abs(slip_exit - expected_exit) < 0.01, (
        f"Exit slippage: expected {expected_exit:.3f}, got {slip_exit:.3f}"
    )
    print(f"  Exit slippage (2 legs bid/ask): {slip_exit:.3f}pts [OK]")

    # Exiting a stressed 0DTE market must always be modelled as dearer than
    # entering one; if this inverts, every cost gate in the engine is wrong.
    assert slip_exit > slip_entry, (
        f"Exit slippage {slip_exit:.3f} must exceed entry {slip_entry:.3f}"
    )

    # The round trip must exceed the sum of its slippage legs (it also carries
    # two sets of statutory charges) — this is the number the credit gates use.
    _rt = engine._round_trip_friction(legs_ba, 1.0)
    assert _rt > slip_entry + slip_exit, (
        f"Round-trip friction {_rt:.3f} must exceed slippage alone"
    )
    print(f"  Round-trip friction (2 legs): {_rt:.3f}pts [OK]")

    legs_no_ba = [{"bid": 0, "ask": 0}, {"bid": 0, "ask": 0}]
    slip_no_ba = engine._compute_slippage(legs_no_ba, is_exit=False)
    expected_no_ba = 2 * 0.35
    assert abs(slip_no_ba - expected_no_ba) < 0.01, (
        f"No bid/ask slippage: expected {expected_no_ba:.3f}, got {slip_no_ba:.3f}"
    )
    print(f"  Entry slippage (no bid/ask): {slip_no_ba:.3f}pts [OK]")

    slip_exit_no_ba = engine._compute_slippage(legs_no_ba, is_exit=True)
    expected_exit_no_ba = 2 * 1.20
    assert abs(slip_exit_no_ba - expected_exit_no_ba) < 0.01, (
        f"Exit no bid/ask: expected {expected_exit_no_ba:.3f}, got {slip_exit_no_ba:.3f}"
    )
    print(f"  Exit slippage (no bid/ask): {slip_exit_no_ba:.3f}pts [OK]")

    slip_exit_no_ba = engine._compute_slippage(legs_no_ba, is_exit=True)
    expected_exit_no_ba = 2 * 1.20
    assert abs(slip_exit_no_ba - expected_exit_no_ba) < 0.01, (
        f"Exit no bid/ask slippage: expected {expected_exit_no_ba:.3f}, got {slip_exit_no_ba:.3f}"
    )
    print(f"  Exit slippage (no bid/ask): {slip_exit_no_ba:.3f}pts [OK]")

    print("  [OK] Slippage computation tests passed")

    print_section("Full Parameter Computation Tests")

    params = engine.compute_params(
        IRON_CONDOR, "test_selection",
        make_signals(
            actual_dte=0,
            active_expiry=today_ist().isoformat(),
            opening_straddle_pts=175.0,
            vol_regime="SELL_PREMIUM",
            price_regime="RANGE",
            positioning_regime="STRONG_RANGE",
            confidence_level="HIGH",
            confidence_score=0.82,
            final_regime="PREMIUM_SELL_RANGE",
            size_multiplier=0.60,
        ),
        0.60,
    )
    print(f"  Iron Condor params valid: {params['valid']}")
    if params.get("valid"):
        assert params["entry_credit"] > 0, "Net credit should be positive"
        assert params["stop_premium"] > params["entry_credit"], (
            "Stop should be above entry credit"
        )
        assert params["target_premium"] < params["entry_credit"], (
            "Target should be below entry credit"
        )
        assert params["final_lots"] >= 1, "Should have at least 1 lot"
        assert params["total_max_risk"] > 0, "Max risk should be positive"
        print(
            f"  Net Credit: {params['entry_credit']:.2f}pts "
            f"Lots: {params['final_lots']}"
        )
    else:
        print(f"  Reason: {params.get('reason')}")
    print("  [OK] Parameter computation test passed")

    print_section("Target Percentage Tests")

    tgt0_low  = engine._get_target_pct(0, make_signals(vix=11.0))
    tgt0_high = engine._get_target_pct(0, make_signals(vix=16.0))
    tgt1      = engine._get_target_pct(1, make_signals(vix=11.5))
    tgt2      = engine._get_target_pct(2, make_signals(vix=11.5))

    assert tgt0_low >= tgt0_high, f"Lower VIX should give higher target: {tgt0_low} vs {tgt0_high}"
    assert tgt0_low > tgt1, f"DTE0 {tgt0_low:.2f} should be > DTE1 {tgt1:.2f}"
    assert tgt1 > tgt2, f"DTE1 {tgt1:.2f} should be > DTE2 {tgt2:.2f}"
    assert 0.30 <= tgt0_low <= 0.70, f"Target out of range: {tgt0_low}"
    print(
        f"  DTE0 VIX=11: {tgt0_low:.2f}  DTE0 VIX=16: {tgt0_high:.2f}  "
        f"DTE1: {tgt1:.2f}  DTE2: {tgt2:.2f} [OK]"
    )

    print_section("Full decide() Test")

    market_engine.state.update({
        "or_computed": True, "daily_halted": False,
        "consecutive_stops": 0, "entry_count": 0,
        "last_entry_time": None, "last_stop_time": None,
    })
    decision = engine.decide(make_signals())
    print(f"  decide() action: {decision['action']}")
    assert decision["action"] in ("ENTER", "NO_TRADE"), (
        f"Unexpected action: {decision['action']}"
    )
    if decision["action"] == "ENTER":
        assert "strategy_name" in decision
        assert "params" in decision
        assert decision["params"].get("valid")
        print(
            f"  Strategy: {decision.get('strategy_name')} "
            f"Lots: {decision['params'].get('final_lots')}"
        )
    else:
        print(f"  NO_TRADE: {decision.get('reason', '')[:60]}")
    print("  [OK] Full decide() test passed")

    print_section("NO_TRADE Path Tests")

    d1 = engine.decide(make_signals(final_regime="ABORT", block_new_entries=True))
    assert d1["action"] == "NO_TRADE", f"Expected NO_TRADE for ABORT, got {d1['action']}"
    print(f"  ABORT -> {d1['action']} [OK]")

    d2 = engine.decide(make_signals(final_regime="NO_TRADE"))
    assert d2["action"] == "NO_TRADE", f"Expected NO_TRADE, got {d2['action']}"
    print(f"  NO_TRADE regime -> {d2['action']} [OK]")

    market_engine.state["daily_halted"] = True
    d3 = engine.decide(make_signals())
    assert d3["action"] == "NO_TRADE", f"Expected NO_TRADE for halt, got {d3['action']}"
    market_engine.state["daily_halted"] = False
    print(f"  Daily halted -> {d3['action']} [OK]")

    market_engine.state["consecutive_stops"] = 2
    d4 = engine.decide(make_signals())
    assert d4["action"] == "NO_TRADE", (
        f"Expected NO_TRADE for consecutive stops, got {d4['action']}"
    )
    market_engine.state["consecutive_stops"] = 0
    print(f"  2 consecutive stops -> {d4['action']} [OK]")

    print("  [OK] NO_TRADE path tests passed")

    print_section("Phantom Trade Logging Test")

    engine._log_phantom_if_neutral(
        make_signals(final_regime="PREMIUM_SELL_RANGE"),
        "VOL_NEUTRAL:VRP_1.8pp_below_threshold",
    )
    phantom_count = db.query_one(
        "SELECT COUNT(*) as cnt FROM phantom_trades WHERE trading_date=?",
        (today_ist().isoformat(),),
    )
    print(
        f"  Phantom trades logged: "
        f"{phantom_count['cnt'] if phantom_count else 0} [OK]"
    )

    db.close()
    print_section("STRATEGY ENGINE SELF-TEST COMPLETE", char="#")
    print("  All tests passed")
    print(f"  Database: {db.db_path}")
    print()


if __name__ == "__main__":
    _self_test()