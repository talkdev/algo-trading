from __future__ import annotations

import json
from datetime import datetime, date, time as dtime
from typing import Optional, Tuple

from nifty_algo_core import (
    Config, Database, RateLimiter, UpstoxClient,
    ExpiryCalendar, now_ist, today_ist,
    print_section, print_kv_table,
    load_config, setup_logging,
)
from market_data_engine import MarketDataEngine

FINAL_REGIME_TO_STRATEGY = {
    "PREMIUM_SELL_RANGE":   None,
    "PREMIUM_SELL_BULL":    "BULL_PUT_SPREAD",
    "PREMIUM_SELL_BEAR":    "BEAR_CALL_SPREAD",
    "BUY_STRADDLE":         "LONG_STRADDLE",
    "BUY_DIRECTIONAL_BULL": "BULL_CALL_SPREAD",
    "BUY_DIRECTIONAL_BEAR": "BEAR_PUT_SPREAD",
    "EXPIRY_MAX_PAIN":      None,
    "NO_TRADE":             None,
    "EMERGENCY_EXIT":       None,
}

DTE_REQUIREMENTS = {
    "IRON_BUTTERFLY":  (0, 1),
    "IRON_CONDOR":     (0, 2),
    "BULL_PUT_SPREAD": (0, 8),
    "BEAR_CALL_SPREAD":(0, 8),
    "BULL_CALL_SPREAD":(1, 5),
    "BEAR_PUT_SPREAD": (1, 5),
    "LONG_STRADDLE":   (1, 5),
    "POST_EVENT_STRADDLE": (0, 3),
}

MIN_CREDITS = {
    "IRON_BUTTERFLY":  15,
    "IRON_CONDOR":     12,
    "BULL_PUT_SPREAD": 10,
    "BEAR_CALL_SPREAD":10,
    "POST_EVENT_STRADDLE": 20,
}
MIN_CREDITS_TUESDAY = {
    "IRON_BUTTERFLY":  18,
    "IRON_CONDOR":     14,
    "BULL_PUT_SPREAD": 12,
    "BEAR_CALL_SPREAD":12,
}

PRICE_STOPS = {
    "IRON_BUTTERFLY":  50,
    "IRON_CONDOR":     80,
    "BULL_PUT_SPREAD": 80,
    "BEAR_CALL_SPREAD":80,
    "POST_EVENT_STRADDLE": 120,
}

TARGET_PCT_BY_DAY = {
    "MONDAY": 0.50, "TUESDAY": 0.45, "WEDNESDAY": 0.50,
    "THURSDAY": 0.50, "FRIDAY": 0.50,
}

MIN_CREDIT_MULT_BY_REGIME = {
    "SUPPRESSED": 1.0, "LOW": 1.1, "NORMAL": 1.0,
    "ELEVATED": 1.15, "HIGH": 1.3,
}

LOT_CAPS_BY_DAY = {
    "MONDAY": 3, "TUESDAY": 2, "WEDNESDAY": 2,
    "THURSDAY": 2, "FRIDAY": 1,
}


class StrategyEngine:

    def __init__(self, config: Config, db: Database,
                 market_engine: MarketDataEngine, logger):
        self.config = config
        self.db = db
        self.market_engine = market_engine
        self.logger = logger
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS strategy_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_time TEXT, trading_date TEXT,
                action TEXT, strategy_name TEXT, reason TEXT,
                params_json TEXT, signals_json TEXT
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategy_decisions_time "
            "ON strategy_decisions(decision_time)"
        )

    def _time_diff_minutes(self, t1: dtime, t2: dtime) -> float:
        dt1 = datetime.combine(today_ist(), t1)
        dt2 = datetime.combine(today_ist(), t2)
        return (dt2 - dt1).total_seconds() / 60.0

    def _count_open_positions(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) as cnt FROM positions "
            "WHERE trading_date=? AND status='OPEN'",
            (today_ist().isoformat(),),
        )
        return row["cnt"] if row else 0

    def _get_target_pct(self, s: dict) -> float:
        day_label = self.market_engine.state.get("day_label")
        base = TARGET_PCT_BY_DAY.get(day_label, 0.50)
        dte = s.get("actual_dte")
        if dte is None:
            return base
        if dte == 0:
            return min(base, 0.35)
        if dte == 1:
            return min(base, 0.40)
        if dte == 2:
            return min(base, 0.45)
        if dte == 3:
            return min(base, 0.48)
        return base

    def _get_min_credit_mult(self, s: dict) -> float:
        return MIN_CREDIT_MULT_BY_REGIME.get(s.get("vix_regime", "NORMAL"), 1.0)

    def _straddle_allowed(self, s: dict) -> bool:
        vix_regime = s.get("vix_regime", "NORMAL")
        if vix_regime in ("SUPPRESSED", "HIGH"):
            return False
        if vix_regime == "ELEVATED":
            if (s.get("or_condition") in ("VERY_NARROW", "NARROW") and
                    (s.get("vrp") or 0) > 3.0):
                return True
            return False
        if s.get("day_mode") == "PRE_EVENT":
            return False
        return True

    def _resolve_premium_sell_range(self, s: dict) -> str:
        actual_dte = s.get("actual_dte")
        if actual_dte == 0:
            return "IRON_CONDOR"
        or_condition = s.get("or_condition", "MODERATE")
        adx_15 = s.get("adx_15") or 0
        adx_15_mature = s.get("adx_15_mature", False)
        if (or_condition in ("VERY_NARROW", "NARROW") and
                adx_15_mature and adx_15 < 20 and
                actual_dte == 1 and
                self._straddle_allowed(s)):
            return "IRON_BUTTERFLY"
        return "IRON_CONDOR"


    def _resolve_expiry_max_pain(self, s: dict) -> str:
        direction = s.get("direction", "NEUTRAL")
        preferred = s.get("preferred_sell_side", "BOTH")
        if direction in ("BULLISH", "MILD_BULLISH") or preferred == "PUTS":
            return "BULL_PUT_SPREAD"
        if direction in ("BEARISH", "MILD_BEARISH") or preferred == "CALLS":
            return "BEAR_CALL_SPREAD"
        return "BULL_PUT_SPREAD"

    def _map_final_regime_to_strategy(self, final_regime: str, s: dict) -> str:
        if final_regime == "PREMIUM_SELL_RANGE":
            return self._resolve_premium_sell_range(s)
        if final_regime == "EXPIRY_MAX_PAIN":
            return self._resolve_expiry_max_pain(s)
        return FINAL_REGIME_TO_STRATEGY.get(final_regime, "NO_TRADE") or "NO_TRADE"

    def _select_strategy_from_signals(self, s: dict) -> Tuple[str, str]:
        vol = s.get("volatility_condition", "UNKNOWN")
        trend = s.get("trend_condition", "OR_PENDING")
        dirn = s.get("direction", "NEUTRAL")
        sell_ok = s.get("sell_ok", False)
        buy_ok = s.get("buy_ok", False)
        vwap_sig = s.get("vwap_signal", "UNKNOWN")
        adx_15 = s.get("adx_15") or 0
        vrp = s.get("vrp") or 0
        vix_regime = s.get("vix_regime", "NORMAL")

        if vol == "UNKNOWN" or trend in ("OR_PENDING", "UNKNOWN", "OBSERVING"):
            return "NO_TRADE", "insufficient_data"
        if vix_regime == "SUPPRESSED" and vrp < 3.0:
            return "NO_TRADE", "vix_suppressed_low_vrp"
        if s.get("iv_behavior") in ("SPIKING", "EXPANDING"):
            return "NO_TRADE", "iv_expanding_or_spiking"

        straddle_ok = self._straddle_allowed(s)

        if vol in ("VERY_RICH", "RICH") and sell_ok:
            if trend in ("RANGE", "CHOPPY", "RANGE_BOUND", "MILD_RANGE",
                         "RANGE_ASSUMED", "UNCERTAIN"):
                if dirn == "NEUTRAL":
                    adx_15_mature = s.get("adx_15_mature", True)
                    if adx_15_mature and adx_15 > 25:
                        return "NO_TRADE", f"condor_blocked_adx_{adx_15:.0f}"
                    if (vol in ("VERY_RICH", "RICH") and
                            s.get("or_condition") in ("VERY_NARROW", "NARROW") and
                            straddle_ok):
                        return "IRON_BUTTERFLY", f"neutral+{vol}+narrow_or"
                    return "IRON_CONDOR", f"neutral+{vol}+{trend}"
                elif dirn in ("BULLISH", "MILD_BULLISH"):
                    if vwap_sig in ("BEARISH", "BEARISH_EXTENDED"):
                        return "NO_TRADE", "vwap_contradicts_bullish_direction"
                    return "BULL_PUT_SPREAD", f"bullish+{vol}+{trend}"
                elif dirn in ("BEARISH", "MILD_BEARISH"):
                    if vwap_sig in ("BULLISH", "BULLISH_EXTENDED"):
                        return "NO_TRADE", "vwap_contradicts_bearish_direction"
                    return "BEAR_CALL_SPREAD", f"bearish+{vol}+{trend}"

            elif trend in ("UPTREND", "STRONG_UPTREND", "MILD_TREND",
                           "TRENDING", "STRONG_TREND"):
                if dirn in ("BULLISH", "MILD_BULLISH") and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                    return "BULL_PUT_SPREAD", f"bullish_trend+{vol}"
                elif dirn in ("BEARISH", "MILD_BEARISH") and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                    return "BEAR_CALL_SPREAD", f"bearish_trend+{vol}"
                elif dirn == "NEUTRAL":
                    if adx_15 > 25:
                        return "NO_TRADE", f"condor_blocked_adx_{adx_15:.0f}_trending"
                    return "IRON_CONDOR", f"neutral_trend+{vol}"

            elif trend in ("DOWNTREND", "STRONG_DOWNTREND"):
                if dirn in ("BEARISH", "MILD_BEARISH") and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                    return "BEAR_CALL_SPREAD", f"bearish_downtrend+{vol}"
                elif dirn in ("BULLISH", "MILD_BULLISH") and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                    return "BULL_PUT_SPREAD", f"bullish_downtrend+{vol}"

        elif vol == "FAIR" and sell_ok:
            if trend in ("RANGE", "CHOPPY", "RANGE_BOUND", "MILD_RANGE",
                         "RANGE_ASSUMED", "UNCERTAIN"):
                if dirn in ("BULLISH", "MILD_BULLISH") and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                    return "BULL_PUT_SPREAD", "bullish+fair+range_half_size"
                elif dirn in ("BEARISH", "MILD_BEARISH") and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                    return "BEAR_CALL_SPREAD", "bearish+fair+range_half_size"
                elif dirn == "NEUTRAL" and straddle_ok:
                    return "IRON_CONDOR", "neutral+fair+range_quarter_size"
            elif trend in ("UPTREND", "STRONG_UPTREND", "MILD_TREND", "TRENDING"):
                if dirn in ("BULLISH", "MILD_BULLISH") and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                    return "BULL_PUT_SPREAD", "bullish+fair+trend_half_size"
                elif dirn in ("BEARISH", "MILD_BEARISH") and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                    return "BEAR_CALL_SPREAD", "bearish+fair+trend_half_size"

        elif vol in ("CHEAP", "INVERTED") and buy_ok:
            if trend in ("UPTREND", "STRONG_UPTREND", "TRENDING", "STRONG_TREND"):
                if dirn in ("BULLISH", "MILD_BULLISH"):
                    return "BULL_CALL_SPREAD", f"bullish+{vol}+trending"
                elif dirn in ("BEARISH", "MILD_BEARISH"):
                    return "BEAR_PUT_SPREAD", f"bearish+{vol}+trending"
                elif dirn == "NEUTRAL" and vol in ("CHEAP", "INVERTED"):
                    return "LONG_STRADDLE", f"neutral+{vol}+strong_trend_breakout"
            elif trend in ("DOWNTREND", "STRONG_DOWNTREND"):
                if dirn in ("BEARISH", "MILD_BEARISH"):
                    return "BEAR_PUT_SPREAD", f"bearish+{vol}+downtrend"
                elif dirn in ("BULLISH", "MILD_BULLISH"):
                    return "BULL_CALL_SPREAD", f"bullish+{vol}+downtrend"

        if s.get("day_mode") == "EVENT" and self.market_engine.state.get("event_announced"):
            ok, reason = self._check_post_event_conditions(s)
            if ok:
                return "POST_EVENT_STRADDLE", reason
            return "NO_TRADE", reason

        return "NO_TRADE", f"no_match:vol={vol}+trend={trend}+dir={dirn}"

    def _check_post_event_conditions(self, s: dict) -> Tuple[bool, str]:
        atm_iv = s.get("atm_iv")
        if atm_iv is None or atm_iv <= 0:
            return False, "post_event_atm_iv_unavailable"
        if atm_iv * 100 <= 14.0:
            return False, "post_event_iv_already_compressed"
        if now_ist().time() >= dtime(12, 30):
            return False, "post_event_too_late_after_12:30"
        state = self.market_engine.state
        pre_spot = state.get("pre_event_spot")
        spot = s.get("spot")
        if pre_spot and pre_spot > 0 and spot:
            move_pct = abs(spot - pre_spot) / pre_spot * 100
            if move_pct > 1.0:
                return False, f"post_event_nifty_moved_{move_pct:.1f}pct"
        ann_time = state.get("event_announcement_time")
        if ann_time:
            try:
                mins = (now_ist() - datetime.fromisoformat(ann_time)).total_seconds() / 60.0
                if mins < 5:
                    return False, f"post_event_wait_{5-mins:.0f}min"
            except Exception:
                pass
        return True, f"post_event_iv_crush_iv={atm_iv*100:.1f}pct"

    def _check_hard_gates(self, s: dict) -> Optional[Tuple[str, str]]:
        final_regime = s.get("final_regime")
        if final_regime in ("NO_TRADE", "EMERGENCY_EXIT"):
            notes = s.get("notes", "regime_engine_no_trade")
            return "NO_TRADE", str(notes) if notes else "regime_engine_no_trade"

        state = self.market_engine.state
        current_time = now_ist().time()

        if state.get("daily_halted"):
            return "NO_TRADE", "daily_loss_limit_reached_or_halted"
        if s.get("circuit_breaker_suspected"):
            return "NO_TRADE", "circuit_breaker_suspected"
        if s.get("vix_spike_detected"):
            return "NO_TRADE", "vix_spike_detected"
        if s.get("iv_behavior") == "EXPANDING" and s.get("sell_ok"):
            return "NO_TRADE", "iv_expanding_never_sell_into_rising_iv"
        if s.get("iv_behavior") == "SPIKING":
            return "NO_TRADE", "iv_spiking"

        try:
            entry_start = datetime.strptime(state["entry_start"], "%H:%M").time()
            entry_end = datetime.strptime(state["entry_end"], "%H:%M").time()
        except Exception:
            entry_start = self.config.trading_window_start
            entry_end = self.config.trading_window_last_entry

        if current_time < entry_start:
            return "NO_TRADE", f"before_entry_window_{entry_start}"
        if current_time > entry_end:
            return "NO_TRADE", f"past_entry_window_{entry_end}"

        today_str = today_ist().isoformat()
        open_count = self._count_open_positions()
        actual = self.db.query_one(
            "SELECT COUNT(*) as cnt FROM positions "
            "WHERE trading_date=? AND status IN ('OPEN','CLOSED')",
            (today_str,),
        )
        db_count = actual["cnt"] if actual else 0
        if db_count != state.get("entry_count", 0):
            state["entry_count"] = db_count

        if open_count >= self.config.max_concurrent_positions:
            return "NO_TRADE", "max_concurrent_positions_reached"
        if db_count >= self.config.max_entries_per_day:
            return "NO_TRADE", f"max_entries_per_day_{db_count}_reached"
        if open_count >= 1:
            return "NO_TRADE", "position_already_open_single_position_engine"

        last_entry_time = state.get("last_entry_time")
        if last_entry_time and open_count == 0 and db_count > 0:
            try:
                mins = (now_ist() - datetime.fromisoformat(last_entry_time)).total_seconds() / 60.0
                if mins < 15:
                    return "NO_TRADE", f"entry_cooldown_{15-mins:.0f}min_remaining"
            except Exception:
                pass

        if state.get("consecutive_stops", 0) >= 3:
            return "NO_TRADE", "3_consecutive_stops_halt"

        last_stop_reason = state.get("last_stop_reason", "")
        last_stop_signal = state.get("last_stop_signal_combo", "")
        current_combo = (f"{s.get('volatility_condition')}_"
                         f"{s.get('trend_condition')}_"
                         f"{s.get('direction')}")
        if (last_stop_reason == "CLOSE_STOP" and
                last_stop_signal == current_combo and
                state.get("consecutive_stops", 0) >= 1):
            return "NO_TRADE", f"same_signal_combo_caused_last_stop_{current_combo}"

        last_stop_time = state.get("last_stop_time")
        if last_stop_time:
            cooldown_map = {"CLOSE_ADX": 45, "CLOSE_VWAP": 20, "CLOSE_STOP": 30}
            iv_extra = 20 if s.get("iv_behavior") in ("EXPANDING", "SPIKING") else 0
            required = cooldown_map.get(last_stop_reason, 30) + iv_extra
            try:
                mins = (now_ist() - datetime.fromisoformat(last_stop_time)).total_seconds() / 60.0
                if mins < required:
                    return "NO_TRADE", f"stop_cooldown_{required-mins:.0f}min_remaining"
            except Exception:
                pass

        if s.get("volatility_condition") == "UNKNOWN":
            return "NO_TRADE", "vrp_unknown_insufficient_data"

        _conf_gate = s.get("confidence")
        if _conf_gate in ("LOW", "NONE"):
            return "NO_TRADE", f"confidence_{_conf_gate}_insufficient_edge_after_costs"

        _actual_dte_gate = s.get("actual_dte")
        _vol_cond_gate = s.get("volatility_condition", "UNKNOWN")
        if _actual_dte_gate is not None and _actual_dte_gate >= 2:
            if _vol_cond_gate not in ("RICH", "VERY_RICH"):
                return "NO_TRADE", f"dte_{_actual_dte_gate}_requires_rich_vrp_not_{_vol_cond_gate}"
            if _conf_gate != "HIGH":
                return "NO_TRADE", f"dte_{_actual_dte_gate}_requires_high_confidence_not_{_conf_gate}"

        _day_move_used = s.get("day_move_used_pct", 0.0) or 0.0
        if _day_move_used >= 70.0 and s.get("sell_ok"):
            return "NO_TRADE", f"day_move_used_{_day_move_used:.0f}pct_of_opening_straddle_no_edge"

        _strategy_for_buy_check = None
        if final_regime and final_regime not in ("NO_TRADE", "EMERGENCY_EXIT"):
            try:
                from regime_bridge import final_regime_to_strategy_name as _frts
                _strategy_for_buy_check = _frts(final_regime, s)
            except Exception:
                pass
        _buy_side = {"LONG_STRADDLE", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"}
        if _strategy_for_buy_check in _buy_side:
            return "NO_TRADE", "buy_side_requires_pre_1030_entry_window"

        _strategy_for_buy_check = None
        if final_regime and final_regime not in ("NO_TRADE", "EMERGENCY_EXIT"):
            from regime_bridge import final_regime_to_strategy_name
            try:
                _strategy_for_buy_check = final_regime_to_strategy_name(final_regime, s)
            except Exception:
                pass
        _buy_side_strategies = {"LONG_STRADDLE", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"}
        if _strategy_for_buy_check in _buy_side_strategies:
            return "NO_TRADE", "buy_side_requires_pre_1030_entry_window"

        _confidence_check = s.get("confidence")
        if _confidence_check in ("LOW", "NONE"):
            return "NO_TRADE", f"confidence_{_confidence_check}_insufficient_edge_after_costs"

        _actual_dte_gate = s.get("actual_dte")
        _vol_cond_gate = s.get("volatility_condition", "UNKNOWN")
        if _actual_dte_gate is not None and _actual_dte_gate >= 2:
            if _vol_cond_gate not in ("RICH", "VERY_RICH"):
                return "NO_TRADE", f"dte_{_actual_dte_gate}_requires_rich_vrp_not_{_vol_cond_gate}"
            if _confidence_check != "HIGH":
                return "NO_TRADE", f"dte_{_actual_dte_gate}_requires_high_confidence_not_{_confidence_check}"

        _day_move_used = s.get("day_move_used_pct", 0.0) or 0.0
        if _day_move_used >= 70.0 and s.get("sell_ok"):
            return "NO_TRADE", f"day_move_used_{_day_move_used:.0f}pct_of_opening_straddle_no_edge"
        if not state.get("or_computed"):
            return "NO_TRADE", "opening_range_not_yet_computed"
        if s.get("trend_condition") in ("OR_PENDING", "OBSERVING"):
            return "NO_TRADE", "opening_range_pending"
        if s.get("day_mode") == "EVENT" and not state.get("event_announced"):
            return "NO_TRADE", "event_day_awaiting_announcement"
        if s.get("vix_regime") == "SUPPRESSED" and (s.get("vrp") or 0) < 3.0:
            return "NO_TRADE", "vix_suppressed_vrp_below_3.0"
        if s.get("or_condition") in ("WIDE", "VERY_WIDE") and s.get("sell_ok"):
            if not self.market_engine.state.get("gap_fade_opportunity"):
                return "NO_TRADE", "wide_or_dangerous_to_sell_premium"

        try:
            hard_exit = datetime.strptime(
                state.get("hard_exit_time", "15:25"), "%H:%M"
            ).time()
        except Exception:
            hard_exit = self.config.hard_exit_time
        mins_to_exit = self._time_diff_minutes(current_time, hard_exit)
        if mins_to_exit < 90:
            return "NO_TRADE", f"only_{mins_to_exit:.0f}min_before_hard_exit"

        return None

    def _post_selection_validation(
        self, strategy_name: str, reason: str, s: dict
    ) -> Tuple[str, str, float]:
        state = self.market_engine.state
        final_regime = s.get("final_regime")
        confidence = s.get("confidence")

        if final_regime and final_regime not in ("NO_TRADE", "EMERGENCY_EXIT"):
            size_mult = s.get("size_multiplier") or state.get("size_multiplier", 1.0)
        else:
            size_mult = state.get("size_multiplier", 1.0)

        if not (final_regime and final_regime not in ("NO_TRADE", "EMERGENCY_EXIT", None)):
            if confidence == "MEDIUM":
                size_mult *= 0.50
            elif confidence == "LOW":
                size_mult *= 0.25

        _dte_for_cap = s.get("actual_dte")
        if _dte_for_cap is not None and _dte_for_cap >= 2:
            dte_midweek_size_cap = 0.25
            if size_mult > dte_midweek_size_cap:
                size_mult = dte_midweek_size_cap

        if not self._straddle_allowed(s) and strategy_name in ("IRON_BUTTERFLY", "POST_EVENT_STRADDLE"):
            dte = s.get("actual_dte")
            dirn = s.get("direction", "NEUTRAL")
            side = s.get("preferred_sell_side", "BOTH")
            if dte == 0:
                if dirn in ("BULLISH", "MILD_BULLISH") or side == "PUTS":
                    strategy_name = "BULL_PUT_SPREAD"
                elif dirn in ("BEARISH", "MILD_BEARISH") or side == "CALLS":
                    strategy_name = "BEAR_CALL_SPREAD"
                else:
                    strategy_name = "IRON_BUTTERFLY"
            else:
                strategy_name = "IRON_CONDOR"
            reason += "_downgraded_straddle_not_allowed"

        if strategy_name in ("IRON_BUTTERFLY", "IRON_CONDOR"):
            dirn = s.get("direction", "NEUTRAL")
            side = s.get("preferred_sell_side", "BOTH")
            if dirn in ("BULLISH", "MILD_BULLISH", "BEARISH", "MILD_BEARISH"):
                if side == "PUTS":
                    strategy_name = "BULL_PUT_SPREAD"
                    reason += "_downgraded_direction_shifted_to_bull_put"
                elif side == "CALLS":
                    strategy_name = "BEAR_CALL_SPREAD"
                    reason += "_downgraded_direction_shifted_to_bear_call"

        if "quarter_size" in reason:
            size_mult *= 0.25
        elif "half_size" in reason or "uncertain" in reason or "fair" in reason:
            size_mult *= 0.50

        if s.get("high_quality_sell_day") and size_mult >= 0.50:
            size_mult = min(size_mult * 1.25, 1.25)

        sell_size_reduction = s.get("sell_size_reduction", 1.0)
        is_directional = strategy_name in (
            "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD",
            "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"
        )
        if sell_size_reduction < 1.0 and not is_directional:
            size_mult *= sell_size_reduction

        return strategy_name, reason, max(size_mult, 0.10)


    def _validate_strategy_entry_rules(
        self, strategy_name: str, s: dict
    ) -> Tuple[bool, str]:
        state = self.market_engine.state
        current_time = now_ist().time()
        spot = s.get("spot")
        adx_15 = s.get("adx_15") or 0

        if strategy_name == "IRON_BUTTERFLY":
            step = self.config.nifty_strike_step
            atm = round(spot / step) * step if spot else None
            if atm is not None and abs(spot - atm) > 20:
                return False, f"spot_too_far_from_atm_{atm:.0f}"
            day_label = state.get("day_label")
            if day_label not in ("TUESDAY", "MONDAY") and s.get("or_condition") not in ("VERY_NARROW", "NARROW"):
                return False, "butterfly_requires_tuesday_monday_or_narrow_or"
            if adx_15 > 20:
                return False, f"butterfly_blocked_adx_{adx_15:.0f}_needs_flat"

        elif strategy_name == "IRON_CONDOR":
            try:
                hard_exit = datetime.strptime(
                    state.get("hard_exit_time", "15:25"), "%H:%M"
                ).time()
            except Exception:
                hard_exit = self.config.hard_exit_time
            mins = self._time_diff_minutes(current_time, hard_exit)
            dte = state.get("actual_dte")
            min_mins = 75 if dte == 0 else 90
            if mins < min_mins:
                return False, f"condor_needs_{min_mins}min_before_exit_only_{mins:.0f}min"
            if s.get("adx_condition") in ("STRONG", "VERY_STRONG"):
                return False, "condor_blocked_adx_trending"

        elif strategy_name == "BULL_PUT_SPREAD":
            or_high = state.get("or_high")
            or_low = state.get("or_low")
            or_mid = (or_high + or_low) / 2.0 if (or_high and or_low) else None
            vwap = s.get("vwap")
            if or_mid and spot and spot < or_mid:
                return False, f"bull_put_spot_below_or_midpoint_{spot:.0f}_vs_{or_mid:.0f}"
            if not or_mid and vwap and spot and spot < vwap:
                return False, f"bull_put_spot_below_vwap_{spot:.0f}_vs_{vwap:.0f}"
            spot_vs_or = s.get("spot_vs_or", "") or ""
            if spot_vs_or.startswith("BELOW_OR"):
                try:
                    pts = float(spot_vs_or.split("_")[-1].replace("pts", ""))
                    if pts > 30:
                        return False, f"bull_put_spot_below_or_by_{pts:.0f}pts"
                except ValueError:
                    pass

        elif strategy_name == "BEAR_CALL_SPREAD":
            vwap = s.get("vwap")
            if adx_15 <= 25 and vwap and spot and spot > vwap:
                return False, f"bear_call_spot_above_vwap_{spot:.0f}_vs_{vwap:.0f}"
            max_pain = s.get("max_pain", 0)
            if max_pain > 0 and spot and abs(spot - max_pain) < 30:
                return False, f"bear_call_spot_within_30pts_of_max_pain_{max_pain:.0f}"

        elif strategy_name == "BULL_CALL_SPREAD":
            if s.get("volatility_condition") in ("FAIR", "RICH", "VERY_RICH"):
                return False, "bull_call_requires_cheap_iv"
            if s.get("adx_direction") != "BULLISH":
                return False, "bull_call_requires_bullish_adx_direction"

        elif strategy_name == "BEAR_PUT_SPREAD":
            if s.get("volatility_condition") in ("FAIR", "RICH", "VERY_RICH"):
                return False, "bear_put_requires_cheap_iv"
            if s.get("adx_direction") != "BEARISH":
                return False, "bear_put_requires_bearish_adx_direction"

        elif strategy_name == "LONG_STRADDLE":
            if s.get("volatility_condition") not in ("CHEAP", "INVERTED"):
                return False, "straddle_requires_cheap_or_inverted_iv"
            if s.get("trend_condition") not in ("UPTREND", "STRONG_UPTREND",
                                                  "DOWNTREND", "STRONG_DOWNTREND",
                                                  "TRENDING", "STRONG_TREND"):
                return False, "straddle_requires_trending_market"
            if current_time > dtime(12, 30):
                return False, "straddle_too_late_after_12:30"

        elif strategy_name == "POST_EVENT_STRADDLE":
            atm_iv = s.get("atm_iv")
            if atm_iv and atm_iv * 100 < 14.0:
                return False, "post_event_iv_already_compressed"

        return True, "entry_rules_passed"

    def _find_strike_by_delta(
        self, chain: dict, opt_type: str, target: float, tol: float = 0.08
    ) -> Optional[float]:
        best, best_diff = None, float("inf")
        for strike, legs in chain.items():
            leg = legs.get(opt_type, {})
            delta = leg.get("delta")
            if delta is None:
                continue
            diff = abs(abs(delta) - target)
            if diff < best_diff:
                best_diff, best = diff, strike
        return best if best_diff <= tol else None

    def _dte_adjusted_delta(self, base: float, dte: Optional[int]) -> float:
        if dte is None:
            return base
        if dte <= 0:
            return max(0.10, base - 0.12)
        if dte == 1:
            return max(0.12, base - 0.10)
        if dte == 2:
            return max(0.15, base - 0.07)
        if dte == 3:
            return max(0.18, base - 0.05)
        return base

    def _validate_strike(
        self, chain: dict, strike: float, opt_type: str, action: str = "SELL"
    ) -> Tuple[bool, str]:
        if strike not in chain:
            return False, f"strike_{strike}_not_in_chain"
        opt = chain[strike].get(opt_type, {})
        bid = opt.get("bid", 0) or 0
        ask = opt.get("ask", 0) or 0
        oi = opt.get("oi", 0) or 0
        ltp = opt.get("ltp", 0) or 0
        if bid <= 0 and ask <= 0:
            return False, f"strike_{strike}_{opt_type}_no_bid_ask"
        min_oi = 500 if action == "SELL" else 100
        if oi < min_oi:
            return False, f"strike_{strike}_{opt_type}_oi_{oi}_below_{min_oi}"
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            max_spread = 0.15 if action == "SELL" else 0.30
            if mid > 0 and (ask - bid) / mid > max_spread:
                return False, f"strike_{strike}_{opt_type}_spread_too_wide"
        eff = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else ltp
        if eff < 0.50:
            return False, f"strike_{strike}_{opt_type}_premium_below_minimum"
        return True, "valid"

    def _get_exec_price(
        self, chain: dict, strike: float, opt_type: str, action: str
    ) -> float:
        opt = chain.get(strike, {}).get(opt_type, {})
        bid = opt.get("bid", 0) or 0
        ask = opt.get("ask", 0) or 0
        ltp = opt.get("ltp", 0) or 0
        if bid > 0 and ask > 0:
            return bid if action == "SELL" else ask
        if bid > 0:
            return bid
        if ask > 0:
            return ask
        return ltp

    def _build_legs_spec(
        self, strategy: str, chain: dict, spot: float, wing: float, dte: Optional[int], s: Optional[dict] = None
    ) -> Tuple[Optional[list], Optional[str]]:
        step = self.config.nifty_strike_step
        td = self._dte_adjusted_delta(0.25, dte)

        if strategy == "IRON_BUTTERFLY":
            atm = round(spot / step) * step
            if atm not in chain:
                atm = min(chain.keys(), key=lambda k: abs(k - spot))
            bw = max(wing, 50)
            lc = atm + bw
            lp = atm - bw
            if lc not in chain:
                lc = min(chain.keys(), key=lambda k: abs(k - (atm + bw)))
            if lp not in chain:
                lp = min(chain.keys(), key=lambda k: abs(k - (atm - bw)))
            return [
                {"strike": atm, "option_type": "call", "action": "SELL"},
                {"strike": atm, "option_type": "put",  "action": "SELL"},
                {"strike": lc,  "option_type": "call", "action": "BUY"},
                {"strike": lp,  "option_type": "put",  "action": "BUY"},
            ], None

        if strategy == "IRON_CONDOR":
            if dte == 0:
                _atm_straddle_ref = s.get("atm_straddle_price", 0) if s else 0
                if _atm_straddle_ref and _atm_straddle_ref > 20:
                    _short_dist = round(_atm_straddle_ref * 0.85 / step) * step
                    _short_dist = max(_short_dist, step)
                    _wing_dist = round(_atm_straddle_ref * 0.55 / step) * step
                    _wing_dist = max(_wing_dist, step)
                    sc = round((spot + _short_dist) / step) * step
                    sp = round((spot - _short_dist) / step) * step
                    lc = sc + _wing_dist
                    lp = sp - _wing_dist
                else:
                    sc = self._find_strike_by_delta(chain, "call", td)
                    sp = self._find_strike_by_delta(chain, "put",  td)
                    if sc is None or sp is None:
                        return None, "cannot_find_strikes_for_0dte_condor"
                    lc = sc + wing
                    lp = sp - wing
            else:
                sc = self._find_strike_by_delta(chain, "call", td)
                sp = self._find_strike_by_delta(chain, "put",  td)
                if sc is None or sp is None:
                    return None, "cannot_find_0.25_delta_strikes_for_condor"
                lc = sc + wing
                lp = sp - wing
            if lc not in chain:
                lc = min(chain.keys(), key=lambda k: abs(k - lc))
            if lp not in chain:
                lp = min(chain.keys(), key=lambda k: abs(k - lp))
            if sc not in chain:
                sc = min(chain.keys(), key=lambda k: abs(k - sc))
            if sp not in chain:
                sp = min(chain.keys(), key=lambda k: abs(k - sp))
            if lc <= sc or lp >= sp:
                return None, "condor_long_strikes_not_further_otm"
            if sc <= sp + step:
                return None, f"condor_short_strikes_too_close_{sp:.0f}_{sc:.0f}"
            return [
                {"strike": sc, "option_type": "call", "action": "SELL"},
                {"strike": sp, "option_type": "put",  "action": "SELL"},
                {"strike": lc, "option_type": "call", "action": "BUY"},
                {"strike": lp, "option_type": "put",  "action": "BUY"},
            ], None

        if strategy == "BULL_PUT_SPREAD":
            sp = self._find_strike_by_delta(chain, "put", td)
            if sp is None:
                return None, "cannot_find_0.25_delta_put"
            lp = sp - wing
            if lp not in chain:
                lp = min(chain.keys(), key=lambda k: abs(k - (sp - wing)))
            if lp >= sp or (sp - lp) < 50:
                return None, "bull_put_spread_wing_invalid"
            return [
                {"strike": sp, "option_type": "put", "action": "SELL"},
                {"strike": lp, "option_type": "put", "action": "BUY"},
            ], None

        if strategy == "BEAR_CALL_SPREAD":
            sc = self._find_strike_by_delta(chain, "call", td)
            if sc is None:
                return None, "cannot_find_0.25_delta_call"
            lc = sc + wing
            if lc not in chain:
                lc = min(chain.keys(), key=lambda k: abs(k - (sc + wing)))
            if lc <= sc or (lc - sc) < 50:
                return None, "bear_call_spread_wing_invalid"
            return [
                {"strike": sc, "option_type": "call", "action": "SELL"},
                {"strike": lc, "option_type": "call", "action": "BUY"},
            ], None

        if strategy == "BULL_CALL_SPREAD":
            lc = self._find_strike_by_delta(chain, "call", 0.40)
            sc = self._find_strike_by_delta(chain, "call", 0.20)
            if lc is None or sc is None or lc >= sc:
                return None, "cannot_find_strikes_for_bull_call_spread"
            return [
                {"strike": lc, "option_type": "call", "action": "BUY"},
                {"strike": sc, "option_type": "call", "action": "SELL"},
            ], None

        if strategy == "BEAR_PUT_SPREAD":
            lp = self._find_strike_by_delta(chain, "put", 0.40)
            sp = self._find_strike_by_delta(chain, "put", 0.20)
            if lp is None or sp is None or lp <= sp:
                return None, "cannot_find_strikes_for_bear_put_spread"
            return [
                {"strike": lp, "option_type": "put", "action": "BUY"},
                {"strike": sp, "option_type": "put", "action": "SELL"},
            ], None

        if strategy == "LONG_STRADDLE":
            atm = round(spot / step) * step
            if atm not in chain:
                atm = min(chain.keys(), key=lambda k: abs(k - spot))
            return [
                {"strike": atm, "option_type": "call", "action": "BUY"},
                {"strike": atm, "option_type": "put",  "action": "BUY"},
            ], None

        if strategy == "POST_EVENT_STRADDLE":
            atm = round(spot / step) * step
            if atm not in chain:
                atm = min(chain.keys(), key=lambda k: abs(k - spot))
            ew = max(wing, 150)
            lc = atm + ew
            lp = atm - ew
            if lc not in chain:
                lc = min(chain.keys(), key=lambda k: abs(k - (atm + ew)))
            if lp not in chain:
                lp = min(chain.keys(), key=lambda k: abs(k - (atm - ew)))
            return [
                {"strike": atm, "option_type": "call", "action": "SELL"},
                {"strike": atm, "option_type": "put",  "action": "SELL"},
                {"strike": lc,  "option_type": "call", "action": "BUY"},
                {"strike": lp,  "option_type": "put",  "action": "BUY"},
            ], None

        return None, f"unknown_strategy_{strategy}"

    def _build_tightening_schedule(self) -> list:
        day_label = self.market_engine.state.get("day_label")
        if day_label == "TUESDAY":
            return [("11:00", 0.80), ("12:00", 0.65), ("12:30", 0.50), ("13:00", 0.35)]
        return [("13:00", 0.85), ("14:00", 0.70), ("14:30", 0.55)]

    def compute_strategy_params(
        self, strategy_name: str, selection_reason: str,
        s: dict, size_mult: float
    ) -> dict:
        state = self.market_engine.state
        expiry_str = s.get("active_expiry")
        actual_dte = s.get("actual_dte")
        if expiry_str is None or actual_dte is None:
            return {"valid": False, "reason": "no_active_expiry"}

        dte_min, dte_max = DTE_REQUIREMENTS.get(strategy_name, (0, 10))
        if actual_dte < dte_min:
            return {"valid": False, "reason": f"dte={actual_dte}_below_min_{dte_min}"}
        if actual_dte > dte_max:
            return {"valid": False, "reason": f"dte={actual_dte}_above_max_{dte_max}"}

        chain = self.market_engine.last_chain
        chain_expiry = self.market_engine.last_chain_expiry
        if not chain or chain_expiry is None or chain_expiry.isoformat() != expiry_str:
            return {"valid": False, "reason": "chain_unavailable_or_expiry_mismatch"}
        if len(chain) < 10:
            return {"valid": False, "reason": f"chain_only_{len(chain)}_strikes"}

        spot = s.get("spot")
        if not spot:
            return {"valid": False, "reason": "spot_unavailable"}

        wing = state.get("wing_width", 150)
        legs_spec, err = self._build_legs_spec(strategy_name, chain, spot, wing, actual_dte, s)
        if legs_spec is None:
            return {"valid": False, "reason": err}

        strategy_type = "BUY" if strategy_name in (
            "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "LONG_STRADDLE"
        ) else "SELL"

        validated_legs = []
        for leg_spec in legs_spec:
            strike = leg_spec["strike"]
            opt_type = leg_spec["option_type"]
            action = leg_spec["action"]
            ok, reason = self._validate_strike(chain, strike, opt_type, action)
            if not ok:
                adjacent = sorted(
                    [k for k in chain.keys() if abs(k - strike) <= self.config.nifty_strike_step * 2 and k != strike],
                    key=lambda k: abs(k - strike)
                )
                found = False
                for alt in adjacent:
                    alt_ok, _ = self._validate_strike(chain, alt, opt_type, action)
                    if alt_ok:
                        leg_spec["strike"] = alt
                        strike = alt
                        found = True
                        break
                if not found:
                    return {"valid": False, "reason": f"leg_validation_failed_no_fallback:{reason}"}

            exec_p = self._get_exec_price(chain, strike, opt_type, action)
            if exec_p <= 0:
                return {"valid": False, "reason": f"leg_{strike}_{opt_type}_no_exec_price"}

            opt = chain[strike][opt_type]
            validated_legs.append({
                "strike": strike, "option_type": opt_type, "action": action,
                "exec_price": exec_p,
                "bid": opt.get("bid", 0), "ask": opt.get("ask", 0),
                "ltp": opt.get("ltp", 0), "delta": opt.get("delta", 0),
                "gamma": opt.get("gamma", 0), "vega": opt.get("vega", 0),
                "theta": opt.get("theta", 0), "iv": opt.get("iv", 0),
                "oi": opt.get("oi", 0),
            })

        gross_value = sum(
            l["exec_price"] if l["action"] == "SELL" else -l["exec_price"]
            for l in validated_legs
        )
        num_legs = len(validated_legs)

        gross_credit = gross_debit = None
        if strategy_type == "SELL":
            gross_credit = gross_value
            if gross_credit <= 0:
                return {"valid": False, "reason": f"gross_credit_{gross_credit:.2f}_non_positive"}
        else:
            gross_debit = abs(gross_value)
            if gross_debit <= 0:
                return {"valid": False, "reason": "gross_debit_zero"}

        total_slippage = 0.0
        for leg in validated_legs:
            bid = leg["bid"]
            ask = leg["ask"]
            leg_delta = abs(leg.get("delta", 0.25) or 0.25)
            slip_cap = 1.5 if leg_delta > 0.35 else (2.0 if leg_delta > 0.20 else 3.0)
            total_slippage += min((ask - bid) / 2.0, slip_cap) if (bid > 0 and ask > 0) else slip_cap

        C02 = self.config.lot_size
        sell_pts = sum(l["exec_price"] for l in validated_legs if l["action"] == "SELL")
        buy_pts  = sum(l["exec_price"] for l in validated_legs if l["action"] == "BUY")
        turnover = sell_pts + buy_pts

        stt_per_lot      = sell_pts * C02 * self.config.stt_options_sell
        exchange_per_lot = turnover * C02 * self.config.exchange_txn_rate
        sebi_per_lot     = turnover * C02 * self.config.sebi_rate
        stamp_per_lot    = buy_pts  * C02 * self.config.stamp_duty_buy_options
        brokerage_fixed  = self.config.brokerage_per_order * num_legs
        gst              = (brokerage_fixed + exchange_per_lot + sebi_per_lot) * 0.18
        per_lot_var      = stt_per_lot + exchange_per_lot + sebi_per_lot + stamp_per_lot
        total_costs_pts  = (per_lot_var + brokerage_fixed + gst) / C02

        net_credit = net_debit = None
        if strategy_type == "SELL":
            net_credit = gross_credit - total_slippage - total_costs_pts
            if net_credit <= 0:
                return {"valid": False, "reason": f"net_credit_{net_credit:.2f}_non_positive"}
        else:
            net_debit = gross_debit + total_slippage + total_costs_pts

        actual_wing_pts = None
        stop_premium = None

        if strategy_type == "SELL":
            day_label = state.get("day_label")
            min_creds = dict(MIN_CREDITS)
            if day_label == "TUESDAY":
                min_creds.update(MIN_CREDITS_TUESDAY)
            static_floor = min_creds.get(strategy_name, 12) * self._get_min_credit_mult(s)
            cost_floor   = total_costs_pts * 3.0
            min_credit   = max(static_floor, cost_floor)

            if net_credit < min_credit:
                return {"valid": False, "reason": f"net_credit_{net_credit:.2f}_below_min_{min_credit:.2f}"}

            min_ratio = 0.20 if actual_dte == 0 else (0.12 if actual_dte <= 1 else 0.10)
            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY", "POST_EVENT_STRADDLE"):
                actual_wing_pts = abs(validated_legs[2]["strike"] - validated_legs[0]["strike"])
                if actual_wing_pts > 0 and (net_credit / actual_wing_pts) < min_ratio:
                    return {"valid": False, "reason": f"credit_ratio_below_{min_ratio}"}
            elif strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
                actual_wing_pts = abs(validated_legs[0]["strike"] - validated_legs[1]["strike"])
                if actual_wing_pts > 0 and (net_credit / actual_wing_pts) < 0.10:
                    return {"valid": False, "reason": "credit_ratio_below_0.10"}

            target_pct = self._get_target_pct(s)
            target_at_target = net_credit * (1.0 - target_pct)
            exit_costs = total_costs_pts + total_slippage
            net_profit_at_target = net_credit - target_at_target - exit_costs
            if net_profit_at_target <= 0:
                return {"valid": False, "reason": f"net_profit_at_target_{net_profit_at_target:.2f}_non_positive"}

            vix_regime = s.get("vix_regime", "NORMAL")
            min_rupee = {"SUPPRESSED": 350, "LOW": 400, "NORMAL": 500,
                         "ELEVATED": 600, "HIGH": 700}.get(vix_regime, 300)
            if net_profit_at_target * C02 < min_rupee:
                return {"valid": False, "reason": f"projected_profit_below_Rs{min_rupee}"}

        current_capital = state.get("current_capital", self.config.starting_capital)
        risk_pct = self.config.max_risk_per_trade_pct
        if actual_dte == 0:
            risk_pct = min(risk_pct, 0.003)
        max_risk = current_capital * risk_pct
        stop_mult = state.get("stop_multiplier", 2.0)

        if strategy_type == "SELL":
            is_dir = strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
            if is_dir:
                gc = gross_credit if gross_credit and gross_credit > 0 else net_credit
                stop_premium = gc * 2.5
                stop_loss = (gc * 1.5) * C02 * 1.25
                if actual_wing_pts and actual_wing_pts > 0:
                    structural = (actual_wing_pts - (net_credit or 0)) * C02
                    stop_loss = min(stop_loss, structural * 1.10)
            else:
                stop_premium = net_credit * stop_mult
                stop_loss = (stop_premium - net_credit) * C02 * 1.25

            if actual_wing_pts and actual_wing_pts > 0 and net_credit > 0:
                contractual = (actual_wing_pts - net_credit) * C02
                max_loss_per_lot = min(stop_loss, contractual) if contractual > 0 else stop_loss
            else:
                max_loss_per_lot = stop_loss
        else:
            max_loss_per_lot = net_debit * 0.50 * C02 * 1.25

        if max_loss_per_lot <= 0:
            max_loss_per_lot = wing * C02 * 0.5

        raw_lots = max_risk / max_loss_per_lot
        base_lots = max(1, int(raw_lots))
        intended = raw_lots * size_mult
        if intended < 0.5:
            return {"valid": False, "reason": f"intended_lots_{intended:.2f}_below_minimum"}

        final_lots = max(1, int(base_lots * size_mult))
        capital_scale = max(1, int(current_capital / self.config.starting_capital))
        day_cap = LOT_CAPS_BY_DAY.get(state.get("day_label"), 3) * capital_scale
        final_lots = min(final_lots, day_cap)
        if max_loss_per_lot * final_lots > max_risk * 1.5:
            final_lots = max(1, int(max_risk / max_loss_per_lot))

        if strategy_type == "SELL":
            margin_per_lot = (actual_wing_pts or wing) * C02 * 1.15
        else:
            margin_per_lot = (net_debit or 0) * C02
        total_margin = margin_per_lot * final_lots
        margin_avail = current_capital * 0.80
        if total_margin > margin_avail and final_lots > 1:
            final_lots = min(final_lots, max(1, int(margin_avail / margin_per_lot)))
            total_margin = margin_per_lot * final_lots

        if strategy_type == "SELL" and net_credit and net_credit > 0:
            credit_stop_mult = 2.5 if actual_dte == 0 else 3.0
            credit_stop = net_credit * credit_stop_mult
            static_stop = PRICE_STOPS.get(strategy_name, 80)
            if actual_dte == 0:
                static_stop = int(static_stop * 0.60)
            elif actual_dte == 1:
                static_stop = int(static_stop * 0.75)
            elif actual_dte <= 3:
                static_stop = int(static_stop * 0.90)
            price_stop_pts = int(min(static_stop, max(credit_stop * 2.0, 30)))
        else:
            price_stop_pts = PRICE_STOPS.get(strategy_name, 80)
            if actual_dte == 0:
                price_stop_pts = 35

        hard_exit_str = state.get("hard_exit_time", self.config.hard_exit_time.strftime("%H:%M"))
        target_pct_final = self._get_target_pct(s)

        net_theta = sum(
            (abs(l.get("theta", 0) or 0) if l["action"] == "SELL"
             else -(abs(l.get("theta", 0) or 0)))
            for l in validated_legs
        )
        try:
            hard_exit_t = datetime.strptime(hard_exit_str, "%H:%M").time()
        except Exception:
            hard_exit_t = dtime(15, 25)
        now_t = now_ist().time()
        entry_dt = datetime.combine(today_ist(), now_t)
        exit_dt  = datetime.combine(today_ist(), hard_exit_t)
        hold_hrs = max(0.5, (exit_dt - entry_dt).total_seconds() / 3600.0)
        theta_capture = net_theta * (hold_hrs / 24.0)
        round_trip_cost = total_costs_pts * 2.0
        if strategy_type == "SELL" and round_trip_cost > 0 and theta_capture < (2.0 * round_trip_cost):
            return {
                "valid": False,
                "reason": f"theta_capture_{theta_capture:.3f}pts_below_2x_round_trip_{round_trip_cost:.3f}pts"
            }

        return {
            "valid": True,
            "strategy_name": strategy_name,
            "strategy_type": strategy_type,
            "selection_reason": selection_reason,
            "target_expiry": expiry_str,
            "actual_dte": actual_dte,
            "legs": validated_legs,
            "num_legs": num_legs,
            "gross_credit": gross_credit,
            "gross_debit": gross_debit,
            "total_slippage": total_slippage,
            "total_costs_pts": total_costs_pts,
            "total_costs_rupees_per_lot": per_lot_var,
            "total_fixed_costs_rupees": brokerage_fixed + gst,
            "entry_credit": net_credit,
            "entry_debit": net_debit,
            "stop_premium": stop_premium if strategy_type == "SELL" else None,
            "target_premium": (net_credit * (1.0 - target_pct_final)) if strategy_type == "SELL" else None,
            "stop_value":  (net_debit * 0.50) if strategy_type == "BUY" else None,
            "target_value":(net_debit * 1.50) if strategy_type == "BUY" else None,
            "price_stop_pts": price_stop_pts,
            "tightening_schedule": self._build_tightening_schedule(),
            "final_lots": final_lots,
            "max_loss_per_lot": max_loss_per_lot,
            "total_max_risk": max_loss_per_lot * final_lots,
            "estimated_margin": total_margin,
            "hard_exit_time": hard_exit_str,
            "target_pct": target_pct_final,
            "entry_spot": spot,
            "entry_vix": s.get("vix"),
            "entry_vrp": s.get("vrp"),
            "entry_time": now_ist().isoformat(),
            "wing_width": actual_wing_pts if strategy_type == "SELL" else None,
            "stop_at_breakeven": False,
            "stop_moved_to_25pct": False,
            "last_known_premium": net_credit if strategy_type == "SELL" else net_debit,
            "defined_risk_only": s.get("defined_risk_only", False),
            "event_day": s.get("event_day", False),
            "event_name": s.get("event_name", ""),
            "final_regime_at_entry": s.get("final_regime"),
            "confidence_at_entry": s.get("confidence"),
        }

    def _log_decision(
        self, s: dict, action: str, reason: str,
        strategy_name: str = "", params: Optional[dict] = None
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
                    "Legs": len(params["legs"]),
                    "Net Credit/Debit (pts)": params.get("entry_credit") or params.get("entry_debit"),
                    "Stop (pts)": params.get("stop_premium") or params.get("stop_value"),
                    "Target (pts)": params.get("target_premium") or params.get("target_value"),
                    "Price Stop (pts)": params["price_stop_pts"],
                    "Final Lots": params["final_lots"],
                    "Max Risk (Rs)": params["total_max_risk"],
                    "Margin (Rs)": params["estimated_margin"],
                    "DTE": params["actual_dte"],
                    "Defined Risk Only": params.get("defined_risk_only"),
                    "Final Regime": params.get("final_regime_at_entry"),
                    "Confidence": params.get("confidence_at_entry"),
                }, title="TRADE PARAMETERS")
                print("\n  LEGS:")
                for leg in params["legs"]:
                    print(
                        f"    {leg['action']:<4} {leg['option_type'].upper():<4} "
                        f"{leg['strike']:.0f} @ {leg['exec_price']:.2f}  "
                        f"(delta={leg['delta']:.3f}, iv={leg['iv']*100:.1f}%)"
                    )
            self.logger.info(
                f"STRATEGY DECISION: {action} {strategy_name} — {reason}"
            )
        print()

    def _persist_decision(
        self, s: dict, strategy_name: str, reason: str,
        params: Optional[dict], action: str
    ) -> None:
        safe_signals = {
            k: v for k, v in s.items()
            if k not in ("atm_greeks", "conditions_met", "conditions_not_met")
        }
        self.db.insert("strategy_decisions", {
            "decision_time": now_ist().isoformat(),
            "trading_date": s.get("trading_date", today_ist().isoformat()),
            "action": action,
            "strategy_name": strategy_name,
            "reason": reason,
            "params_json": json.dumps(params, default=str) if params else None,
            "signals_json": json.dumps(safe_signals, default=str),
        })

    def decide(self, signals: dict) -> dict:
        final_regime = signals.get("final_regime")
        confidence   = signals.get("confidence")

        gate = self._check_hard_gates(signals)
        if gate:
            action, reason = gate
            self._log_decision(signals, action, reason)
            self._persist_decision(signals, "NONE", reason, None, action)
            self.market_engine.finalize_cycle_log(
                action, reason, self._count_open_positions()
            )
            return {"action": action, "reason": reason}

        if final_regime and final_regime not in ("NO_TRADE", "EMERGENCY_EXIT", None):
            strategy_name = self._map_final_regime_to_strategy(final_regime, signals)
            reason = (f"regime_engine:{final_regime}:conf={confidence}:"
                      f"{signals.get('notes', '')}")
        else:
            strategy_name, reason = self._select_strategy_from_signals(signals)

        if strategy_name == "NO_TRADE":
            self._log_decision(signals, "NO_TRADE", reason)
            self._persist_decision(signals, "NO_TRADE", reason, None, "NO_TRADE")
            self.market_engine.finalize_cycle_log(
                "NO_TRADE", reason, self._count_open_positions()
            )
            return {"action": "NO_TRADE", "reason": reason}

        strategy_name, reason, size_mult = self._post_selection_validation(
            strategy_name, reason, signals
        )

        rules_ok, rules_reason = self._validate_strategy_entry_rules(strategy_name, signals)
        if not rules_ok:
            full_reason = f"strategy_rules_failed:{rules_reason}"
            self._log_decision(signals, "NO_TRADE", full_reason)
            self._persist_decision(signals, strategy_name, full_reason, None, "NO_TRADE")
            self.market_engine.finalize_cycle_log(
                "NO_TRADE", full_reason, self._count_open_positions()
            )
            return {"action": "NO_TRADE", "reason": full_reason}

        params = self.compute_strategy_params(strategy_name, reason, signals, size_mult)

        if not params.get("valid"):
            full_reason = f"params_invalid:{params.get('reason')}"
            self._log_decision(signals, "NO_TRADE", full_reason)
            self._persist_decision(signals, strategy_name, full_reason, None, "NO_TRADE")
            self.market_engine.finalize_cycle_log(
                "NO_TRADE", full_reason, self._count_open_positions()
            )
            return {"action": "NO_TRADE", "reason": full_reason}

        self._log_decision(signals, "STRATEGY_SELECTED", reason, strategy_name, params)
        self._persist_decision(signals, strategy_name, reason, params, "STRATEGY_SELECTED")
        self.market_engine.finalize_cycle_log(
            f"STRATEGY_SELECTED:{strategy_name}", None, self._count_open_positions()
        )

        return {
            "action": "ENTER",
            "strategy_name": strategy_name,
            "reason": reason,
            "params": params,
        }


def _self_test() -> None:
    print_section("NIFTY ALGO — STRATEGY ENGINE SELF-TEST")
    config = load_config()
    db = Database(config.db_path)
    logger = setup_logging(db, config.log_dir)
    rate_limiter = RateLimiter(config.rate_limits)
    client = UpstoxClient(config, rate_limiter, db, logger)
    market_engine = MarketDataEngine(config, db, client, rate_limiter, logger)
    strategy_engine = StrategyEngine(config, db, market_engine, logger)

    if not config.upstox_access_token or not client.validate_token():
        logger.warning("No valid Upstox token — cannot run live decision test.")
        db.close()
        return

    signals = market_engine.run_cycle()
    decision = strategy_engine.decide(signals)
    print_section(f"DECISION: {decision['action']}")
    print(f"  Reason: {decision.get('reason', '')}")
    db.close()


if __name__ == "__main__":
    _self_test()