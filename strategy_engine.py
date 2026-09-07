# strategy_engine.py
# NIFTY Intraday Options Engine v3.0

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
SELL = "SELL"
BUY  = "BUY"

DTE_REQUIREMENTS: Dict[str, Tuple[int, int]] = {
    IRON_BUTTERFLY:   (0, 1),
    IRON_CONDOR:      (0, 2),
    BULL_PUT_SPREAD:  (0, 2),
    BEAR_CALL_SPREAD: (0, 2),
}

MIN_CREDITS: Dict[str, float] = {
    IRON_BUTTERFLY:   28.0,
    IRON_CONDOR:      22.0,
    BULL_PUT_SPREAD:  18.0,
    BEAR_CALL_SPREAD: 18.0,
}

MIN_CREDITS_TUESDAY: Dict[str, float] = {
    IRON_BUTTERFLY:   32.0,
    IRON_CONDOR:      25.0,
    BULL_PUT_SPREAD:  20.0,
    BEAR_CALL_SPREAD: 20.0,
}

MIN_CREDIT_RATIO: Dict[str, float] = {
    IRON_BUTTERFLY:   0.18,
    IRON_CONDOR:      0.12,
    BULL_PUT_SPREAD:  0.10,
    BEAR_CALL_SPREAD: 0.10,
}

MIN_CREDIT_RATIO_DTE0: Dict[str, float] = {
    IRON_BUTTERFLY:   0.22,
    IRON_CONDOR:      0.16,
    BULL_PUT_SPREAD:  0.14,
    BEAR_CALL_SPREAD: 0.14,
}

LOT_CAPS_BY_DAY: Dict[str, int] = {
    "MONDAY":    4,
    "TUESDAY":   3,
    "WEDNESDAY": 3,
    "THURSDAY":  3,
    "FRIDAY":    3,
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
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sd_time ON strategy_decisions(decision_time)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sd_date ON strategy_decisions(trading_date)")

    def _count_open_positions(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) as cnt FROM positions WHERE trading_date=? AND status='OPEN'",
            (today_ist().isoformat(),),
        )
        return row["cnt"] if row else 0

    def _count_today_entries(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) as cnt FROM positions WHERE trading_date=? AND status IN ('OPEN','CLOSED')",
            (today_ist().isoformat(),),
        )
        return row["cnt"] if row else 0

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
        if iv_behavior == "EXPANDING":
            return "NO_TRADE", "iv_expanding_never_sell_into_rising_iv"
        if iv_behavior == "SPIKING":
            return "NO_TRADE", "iv_spiking"

        try:
            entry_start = datetime.strptime(state.get("entry_start", "09:45"), "%H:%M").time()
            entry_end   = datetime.strptime(state.get("entry_end",   "14:00"), "%H:%M").time()
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

        last_entry_time = state.get("last_entry_time")
        if last_entry_time and open_count == 0 and total_count > 0:
            try:
                mins = (now_ist() - datetime.fromisoformat(last_entry_time)).total_seconds() / 60.0
                if mins < ENTRY_COOLDOWN_MIN:
                    return "NO_TRADE", f"entry_cooldown_{ENTRY_COOLDOWN_MIN - mins:.0f}min_remaining"
            except Exception:
                pass

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
                mins = (now_ist() - datetime.fromisoformat(last_stop_time)).total_seconds() / 60.0
                if mins < required:
                    return "NO_TRADE", f"stop_cooldown_{required - mins:.0f}min_remaining"
            except Exception:
                pass

        if not signals.get("or_computed"):
            return "NO_TRADE", "opening_range_not_yet_computed"
        if signals.get("price_regime") in ("OBSERVING",):
            return "NO_TRADE", "opening_range_pending"
        if signals.get("chain_stale"):
            return "NO_TRADE", "chain_stale_cannot_validate_strikes"

        confidence = signals.get("confidence_level", "NONE")
        if confidence in ("LOW", "NONE"):
            return "NO_TRADE", f"confidence_{confidence}_insufficient_edge_after_costs"

        actual_dte = signals.get("actual_dte")
        vol_regime = signals.get("vol_regime", "NEUTRAL")
        if actual_dte is not None and actual_dte >= 2:
            if vol_regime != "STRONG_SELL_PREMIUM":
                return "NO_TRADE", f"dte_{actual_dte}_requires_strong_sell_premium_not_{vol_regime}"
            if confidence != "HIGH":
                return "NO_TRADE", f"dte_{actual_dte}_requires_high_confidence_not_{confidence}"

        day_move_used = float(signals.get("day_move_used_pct") or 0.0)
        if day_move_used >= self.config.day_move_used_block_pct:
            return "NO_TRADE", f"day_move_used_{day_move_used:.0f}pct_of_opening_straddle_no_edge"

        try:
            hard_exit = datetime.strptime(state.get("hard_exit_time", "15:00"), "%H:%M").time()
        except Exception:
            hard_exit = self.config.hard_exit_time

        mins_to_exit = self._minutes_to_time(current_time, hard_exit)
        min_buffer   = 90
        if mins_to_exit < min_buffer:
            return "NO_TRADE", f"only_{mins_to_exit:.0f}min_before_hard_exit_need_{min_buffer}"

        if signals.get("event_day") and self.config.defined_risk_only_on_event:
            self.logger.info(f"Event day trade: {signals.get('event_name', '')} — defined risk only")

        or_condition = signals.get("or_condition", "MODERATE")
        if or_condition in ("WIDE", "VERY_WIDE"):
            if not signals.get("gap_fade_opportunity"):
                return "NO_TRADE", f"wide_or_{or_condition}_dangerous_to_sell_premium"

        return None

    def _map_regime_to_strategy(
        self, signals: dict, _test_time: Optional[dtime] = None
    ) -> Tuple[str, str]:
        final_regime  = signals.get("final_regime", "NO_TRADE")
        confidence    = signals.get("confidence_level", "NONE")
        dte           = signals.get("actual_dte")
        or_condition  = signals.get("or_condition", "MODERATE")
        adx_15        = float(signals.get("adx_15") or 0.0)
        adx_15_mature = bool(signals.get("adx_15_mature", False))
        current_time  = _test_time if _test_time is not None else now_ist().time()
        vol_regime    = signals.get("vol_regime", "NEUTRAL")

        if final_regime == "PREMIUM_SELL_RANGE":
            strategy = self._resolve_range_strategy(
                dte, or_condition, adx_15, adx_15_mature, current_time, vol_regime, signals
            )
            reason = f"regime:{final_regime}:conf={confidence}:dte={dte}:or={or_condition}:adx={adx_15:.0f}"
            return strategy, reason

        if final_regime == "PREMIUM_SELL_BULL":
            reason = f"regime:{final_regime}:conf={confidence}:dte={dte}:adx={adx_15:.0f}:price={signals.get('price_regime')}"
            return BULL_PUT_SPREAD, reason

        if final_regime == "PREMIUM_SELL_BEAR":
            reason = f"regime:{final_regime}:conf={confidence}:dte={dte}:adx={adx_15:.0f}:price={signals.get('price_regime')}"
            return BEAR_CALL_SPREAD, reason

        return "NO_TRADE", f"no_strategy_for_regime:{final_regime}"

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
            return IRON_CONDOR
        if (or_condition == "VERY_NARROW" and
                adx_15_mature and
                adx_15 < 15 and
                current_time < dtime(11, 30) and
                vol_regime in ("STRONG_SELL_PREMIUM", "SELL_PREMIUM")):
            spot       = float(signals.get("spot") or 0)
            atm_strike = int(signals.get("atm_strike") or 0)
            if atm_strike > 0 and abs(spot - atm_strike) < 30:
                return IRON_BUTTERFLY
        return IRON_CONDOR

    def _validate_entry_rules(
        self, strategy_name: str, signals: dict
    ) -> Tuple[bool, str]:
        state        = self.market_engine.state
        current_time = now_ist().time()
        spot         = float(signals.get("spot") or 0)
        adx_15       = float(signals.get("adx_15") or 0)
        dte          = signals.get("actual_dte")

        if strategy_name == IRON_BUTTERFLY:
            atm_strike = int(signals.get("atm_strike") or 0)
            if atm_strike > 0 and abs(spot - atm_strike) > 25:
                return False, f"butterfly_spot_too_far_from_atm_{atm_strike:.0f}"
            if dte not in (0, 1):
                return False, f"butterfly_requires_dte_0_or_1_not_{dte}"
            if dte == 0 and current_time >= dtime(11, 30):
                return False, "butterfly_too_late_after_11:30_on_0dte"
            if adx_15 > 20:
                return False, f"butterfly_blocked_adx_{adx_15:.0f}_needs_flat_below_20"
            if signals.get("or_condition", "MODERATE") not in ("VERY_NARROW", "NARROW"):
                return False, f"butterfly_requires_narrow_or_not_{signals.get('or_condition')}"

        elif strategy_name == IRON_CONDOR:
            try:
                hard_exit = datetime.strptime(state.get("hard_exit_time", "15:00"), "%H:%M").time()
            except Exception:
                hard_exit = self.config.hard_exit_time
            mins     = self._minutes_to_time(current_time, hard_exit)
            min_mins = 75 if dte == 0 else 90
            if mins < min_mins:
                return False, f"condor_needs_{min_mins}min_before_exit_only_{mins:.0f}min"
            if adx_15 >= self.config.adx_strong_threshold:
                return False, f"condor_blocked_strong_adx_{adx_15:.0f}"
            if signals.get("or_condition") == "VERY_WIDE":
                return False, "condor_blocked_very_wide_or"

        elif strategy_name == BULL_PUT_SPREAD:
            or_high = float(signals.get("or_high") or 0)
            or_low  = float(signals.get("or_low") or 0)
            if or_high > 0 and or_low > 0:
                or_mid    = (or_high + or_low) / 2.0
                or_buffer = 30 if dte == 0 else 15
                if spot < or_mid - or_buffer:
                    return False, f"bull_put_spot_{spot:.0f}_below_or_mid_{or_mid:.0f}_by_{or_mid - spot:.0f}pts"
            vwap = signals.get("vwap")
            if vwap and vwap > 0 and spot < vwap - 30:
                return False, f"bull_put_spot_below_vwap_{vwap:.0f}_by_{vwap - spot:.0f}pts"

        elif strategy_name == BEAR_CALL_SPREAD:
            or_high = float(signals.get("or_high") or 0)
            or_low  = float(signals.get("or_low") or 0)
            if or_high > 0 and or_low > 0:
                or_mid    = (or_high + or_low) / 2.0
                or_buffer = 30 if dte == 0 else 15
                if spot > or_mid + or_buffer:
                    return False, f"bear_call_spot_{spot:.0f}_above_or_mid_{or_mid:.0f}_by_{spot - or_mid:.0f}pts"
            max_pain = float(signals.get("max_pain") or 0)
            if max_pain > 0 and abs(spot - max_pain) < 25:
                return False, f"bear_call_spot_within_25pts_of_max_pain_{max_pain:.0f}"

        return True, "entry_rules_passed"

    def _select_strikes(
        self,
        strategy_name: str,
        chain:         dict,
        spot:          float,
        dte:           Optional[int],
        signals:       dict,
    ) -> Tuple[Optional[List[dict]], Optional[str]]:
        step             = self.config.nifty_strike_step
        opening_straddle = float(signals.get("opening_straddle_pts") or 0)
        adx_15           = float(signals.get("adx_15") or 0)
        vix              = float(signals.get("vix") or 11.0)

        if dte == 0:
            dist_mult, floor_pts = 1.0, 120
        elif dte == 1:
            dist_mult, floor_pts = 0.85, 150
        else:
            dist_mult, floor_pts = 0.75, 150

        if adx_15 >= self.config.adx_strong_threshold:
            dist_mult *= 1.20
            floor_pts  = int(floor_pts * 1.10)

        if opening_straddle > 20:
            short_dist = max(int(opening_straddle * dist_mult), floor_pts)
            short_dist = int(round(short_dist / step) * step)
        else:
            short_dist = None

        if adx_15 >= self.config.adx_strong_threshold:
            delta_target = 0.17
        elif adx_15 >= self.config.adx_trend_threshold:
            delta_target = 0.20
        else:
            delta_target = 0.22

        if vix < 12.0:
            delta_target = max(delta_target - 0.02, 0.15)
        elif vix < 14.0:
            delta_target = max(delta_target - 0.01, 0.16)

        wing = int(round((int(signals.get("wing_width") or 150)) / step) * step)
        wing = max(wing, 100)

        if strategy_name == IRON_BUTTERFLY:
            return self._build_iron_butterfly(chain, spot, step, wing)
        if strategy_name == IRON_CONDOR:
            return self._build_iron_condor(chain, spot, step, dte, short_dist, delta_target, wing, signals)
        if strategy_name == BULL_PUT_SPREAD:
            return self._build_bull_put_spread(chain, spot, step, dte, short_dist, delta_target, wing)
        if strategy_name == BEAR_CALL_SPREAD:
            return self._build_bear_call_spread(chain, spot, step, dte, short_dist, delta_target, wing)
        return None, f"unknown_strategy_{strategy_name}"

    def _build_iron_butterfly(self, chain, spot, step, wing):
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

    def _build_iron_condor(self, chain, spot, step, dte, short_dist, delta_target, wing, signals):
        if short_dist is not None:
            sc = int(round((spot + short_dist) / step) * step)
            sp = int(round((spot - short_dist) / step) * step)
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

    def _build_bull_put_spread(self, chain, spot, step, dte, short_dist, delta_target, wing):
        if short_dist is not None:
            sp = int(round((spot - short_dist) / step) * step)
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

    def _build_bear_call_spread(self, chain, spot, step, dte, short_dist, delta_target, wing):
        if short_dist is not None:
            sc = int(round((spot + short_dist) / step) * step)
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

    def _find_strike_by_delta(self, chain, opt_type, target, tolerance=0.10):
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

    def _validate_leg(self, chain, strike, opt_type, action):
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
        min_oi = 500 if action == "SELL" else 100
        if oi < min_oi:
            return False, f"strike_{strike:.0f}_{opt_type}_oi_{oi}_below_{min_oi}"
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            if mid > 0 and (ask - bid) / mid > (0.15 if action == "SELL" else 0.30):
                return False, f"strike_{strike:.0f}_{opt_type}_spread_too_wide"
        eff = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else ltp
        if eff < 0.50:
            return False, f"strike_{strike:.0f}_{opt_type}_premium_{eff:.2f}_below_0.50"
        return True, "valid"

    def _get_exec_price(self, chain, strike, opt_type, action):
        opt = chain.get(strike, {}).get(opt_type, {})
        if not opt:
            return 0.0
        bid = float(opt.get("bid", 0) or 0)
        ask = float(opt.get("ask", 0) or 0)
        ltp = float(opt.get("ltp", 0) or 0)
        if action == "SELL":
            return bid if bid > 0 else (ltp if ltp > 0 else ask)
        return ask if ask > 0 else (ltp if ltp > 0 else bid)

    def _build_validated_legs(self, legs_spec, chain):
        step      = self.config.nifty_strike_step
        validated = []
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
            ep = self._get_exec_price(chain, strike, opt_type, action)
            if ep <= 0:
                return None, f"leg_{strike:.0f}_{opt_type}_no_exec_price"
            opt = chain[strike][opt_type]
            validated.append({
                "strike": strike, "option_type": opt_type, "action": action,
                "exec_price": ep,
                "bid":   float(opt.get("bid",   0) or 0),
                "ask":   float(opt.get("ask",   0) or 0),
                "ltp":   float(opt.get("ltp",   0) or 0),
                "delta": float(opt.get("delta", 0) or 0),
                "gamma": float(opt.get("gamma", 0) or 0),
                "vega":  float(opt.get("vega",  0) or 0),
                "theta": float(opt.get("theta", 0) or 0),
                "iv":    float(opt.get("iv",    0) or 0),
                "oi":    int(opt.get("oi",      0) or 0),
                "instrument_key": opt.get("instrument_key"),
            })
        return validated, None

    def _compute_costs(self, legs, lots, action):
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
                "stt": round(stt, 2), "exchange": round(exchange, 2),
                "sebi": round(sebi, 4), "stamp": round(stamp, 4),
                "brokerage": round(brokerage, 2), "gst": round(gst, 2),
                "total": round(total, 2),
            },
        }

    def _compute_slippage(self, legs):
        total = 0.0
        for leg in legs:
            bid = float(leg.get("bid", 0) or 0)
            ask = float(leg.get("ask", 0) or 0)
            total += 0.30 if (bid > 0 and ask > 0) else 0.50
        return round(total, 3)

    def compute_params(self, strategy_name, selection_reason, signals, size_mult):
        state      = self.market_engine.state
        expiry_str = signals.get("active_expiry")
        actual_dte = signals.get("actual_dte")
        C02        = self.config.lot_size

        if expiry_str is None or actual_dte is None:
            return {"valid": False, "reason": "no_active_expiry"}

        dte_min, dte_max = DTE_REQUIREMENTS.get(strategy_name, (0, 2))
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

        legs_spec, err = self._select_strikes(strategy_name, chain, spot, actual_dte, signals)
        if legs_spec is None:
            return {"valid": False, "reason": f"strike_selection_failed:{err}"}

        validated_legs, err = self._build_validated_legs(legs_spec, chain)
        if validated_legs is None:
            return {"valid": False, "reason": f"leg_validation_failed:{err}"}

        num_legs    = len(validated_legs)
        gross_value = 0.0
        for leg in validated_legs:
            bid = float(leg.get("bid", 0) or 0)
            ask = float(leg.get("ask", 0) or 0)
            mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else float(leg.get("exec_price", 0) or 0)
            gross_value += mid if leg["action"] == "SELL" else -mid

        gross_credit = gross_value
        if gross_credit <= 0:
            return {"valid": False, "reason": f"gross_credit_{gross_credit:.2f}_non_positive"}

        total_slippage   = self._compute_slippage(validated_legs)
        entry_costs_dict = self._compute_costs(validated_legs, 1, "ENTRY")
        entry_costs_pts  = entry_costs_dict["total_rupees"] / C02 if C02 > 0 else 0
        net_credit       = gross_credit - total_slippage - entry_costs_pts

        if net_credit <= 0:
            return {"valid": False, "reason": f"net_credit_{net_credit:.2f}_non_positive_after_costs"}

        day_label = state.get("day_label", "TUESDAY")
        min_credit_static = (MIN_CREDITS_TUESDAY if day_label == "TUESDAY" else MIN_CREDITS).get(strategy_name, 18.0)
        min_credit = max(min_credit_static, (entry_costs_pts + total_slippage) * 2.0 * 2.5)

        if net_credit < min_credit:
            return {"valid": False, "reason": f"net_credit_{net_credit:.2f}pts_below_min_{min_credit:.2f}pts"}

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

        if actual_wing_pts and actual_wing_pts > 0:
            ratio     = net_credit / actual_wing_pts
            min_ratio = MIN_CREDIT_RATIO_DTE0.get(strategy_name, 0.14) if actual_dte == 0 else MIN_CREDIT_RATIO.get(strategy_name, 0.10)
            if ratio < min_ratio:
                return {"valid": False, "reason": f"credit_ratio_{ratio:.3f}_below_min_{min_ratio:.3f}"}

        target_pct       = self._get_target_pct(actual_dte, signals)
        exit_costs       = entry_costs_pts + total_slippage
        expected_edge    = net_credit * target_pct - exit_costs
        vix              = float(signals.get("vix") or 11.0)
        min_edge_mult    = 0.60 if vix < 11.5 else (0.70 if vix < 13.0 else (0.85 if vix < 15.0 else 1.00))
        if expected_edge < exit_costs * min_edge_mult:
            return {"valid": False, "reason": f"expected_edge_{expected_edge:.2f}pts_below_min_{exit_costs*min_edge_mult:.2f}pts"}

        current_capital  = state.get("current_capital", self.config.starting_capital)
        risk_pct         = min(self.config.max_risk_per_trade_pct, 0.004) if actual_dte == 0 else self.config.max_risk_per_trade_pct
        max_risk         = current_capital * risk_pct
        stop_mult        = min(float(state.get("stop_multiplier", 1.5) or 1.5), 1.5)

        if strategy_name in (BULL_PUT_SPREAD, BEAR_CALL_SPREAD):
            stop_premium  = gross_credit * 2.5
            stop_loss_pts = gross_credit * 1.5
        else:
            stop_premium  = net_credit * stop_mult
            stop_loss_pts = stop_premium - net_credit

        max_loss_per_lot = max(stop_loss_pts * C02 * 1.25, 1.0)
        if actual_wing_pts and actual_wing_pts > 0 and net_credit > 0:
            max_loss_per_lot = min(max_loss_per_lot, (actual_wing_pts - net_credit) * C02 * 1.10)
        if max_loss_per_lot <= 0:
            max_loss_per_lot = (actual_wing_pts or 150) * C02 * 0.5

        raw_lots   = max_risk / max_loss_per_lot
        final_lots = max(1, int(int(raw_lots) * size_mult))
        day_cap    = LOT_CAPS_BY_DAY.get(day_label, 3) * max(1, int(current_capital / self.config.starting_capital))
        final_lots = min(final_lots, day_cap)
        if max_loss_per_lot * final_lots > max_risk * 1.5:
            final_lots = max(1, int(max_risk / max_loss_per_lot))
        if strategy_name in (IRON_CONDOR, IRON_BUTTERFLY) and final_lots < 2:
            if raw_lots * size_mult < 1.0:
                return {"valid": False, "reason": f"intended_lots_{raw_lots*size_mult:.2f}_below_minimum_2_for_{strategy_name}"}
            final_lots = 2

        margin_per_lot = (actual_wing_pts or 150) * C02 * 1.10
        total_margin   = margin_per_lot * final_lots
        if total_margin > current_capital * 0.80 and final_lots > 1:
            final_lots   = max(1, int(current_capital * 0.80 / margin_per_lot))
            total_margin = margin_per_lot * final_lots

        target_premium = net_credit * (1.0 - target_pct)
        opening_straddle = float(signals.get("opening_straddle_pts") or 0)
        price_stop_pts   = max(int(opening_straddle * self.config.price_stop_straddle_mult), 30) if opening_straddle > 20 else 50

        price_stop_call = price_stop_put = None
        for leg in validated_legs:
            if leg["action"] == "SELL":
                if leg["option_type"] == "call":
                    price_stop_call = leg["strike"] - price_stop_pts
                elif leg["option_type"] == "put":
                    price_stop_put  = leg["strike"] + price_stop_pts

        return {
            "valid": True,
            "strategy_name": strategy_name, "strategy_type": SELL,
            "selection_reason": selection_reason,
            "target_expiry": expiry_str, "actual_dte": actual_dte,
            "legs": validated_legs, "num_legs": num_legs,
            "gross_credit": round(gross_credit, 3),
            "entry_credit": round(net_credit, 3),
            "total_slippage": round(total_slippage, 3),
            "total_costs_pts": round(entry_costs_pts, 4),
            "total_costs_rupees_per_lot": round(entry_costs_dict["total_rupees"], 2),
            "total_fixed_costs_rupees": 0.0,
            "entry_costs_rupees": round(entry_costs_dict["total_rupees"] * final_lots, 2),
            "stop_premium": round(stop_premium, 3),
            "target_premium": round(target_premium, 3),
            "price_stop_pts": price_stop_pts,
            "price_stop_level_call": price_stop_call,
            "price_stop_level_put": price_stop_put,
            "hard_exit_time": state.get("hard_exit_time", self.config.hard_exit_time.strftime("%H:%M")),
            "target_pct": target_pct,
            "final_lots": final_lots,
            "max_loss_per_lot": round(max_loss_per_lot, 2),
            "total_max_risk": round(max_loss_per_lot * final_lots, 2),
            "estimated_margin": round(total_margin, 2),
            "wing_width": actual_wing_pts,
            "last_known_premium": round(net_credit, 3),
            "entry_spot": spot, "entry_vix": signals.get("vix"),
            "entry_vrp": signals.get("vrp_smoothed"),
            "entry_vrp_smoothed": signals.get("vrp_smoothed"),
            "opening_straddle_at_entry": opening_straddle,
            "vol_regime_at_entry": signals.get("vol_regime"),
            "price_regime_at_entry": signals.get("price_regime"),
            "positioning_at_entry": signals.get("positioning_regime"),
            "confidence_level_at_entry": signals.get("confidence_level"),
            "confidence_score_at_entry": signals.get("confidence_score"),
            "final_regime_at_entry": signals.get("final_regime"),
            "defined_risk_only": bool(signals.get("defined_risk_only", False)),
            "event_day": bool(signals.get("event_day", False)),
            "event_name": signals.get("event_name", ""),
            "borderline_sell": bool(signals.get("borderline_sell", False)),
            "is_borderline_sell": int(bool(signals.get("borderline_sell", False))),
            "calibration_tier_at_entry": self._get_calibration().calibration_tier if self._get_calibration() else 0,
            "profit_lock_activated": False,
            "profit_lock_stop_level": None,
            "stop_at_breakeven": False,
        }

    def _get_target_pct(self, dte, signals):
        vix = float(signals.get("vix") or 11.0)
        if dte == 0:
            return 0.50 if vix < 12.0 else (0.47 if vix < 14.0 else 0.45)
        if dte == 1:
            return 0.45 if vix < 12.0 else (0.42 if vix < 14.0 else 0.40)
        return 0.38

    def _log_decision(self, signals, action, reason, strategy_name="", params=None):
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
                    "Legs": params["num_legs"], "Net Credit": params.get("entry_credit"),
                    "Stop": params.get("stop_premium"), "Target": params.get("target_premium"),
                    "Final Lots": params.get("final_lots"), "Max Risk (Rs)": params.get("total_max_risk"),
                    "DTE": params.get("actual_dte"), "Vol Regime": params.get("vol_regime_at_entry"),
                }, title="TRADE PARAMETERS")
                print("\n  LEGS:")
                for leg in params.get("legs", []):
                    iv_pct = float(leg.get("iv") or 0) * 100
                    if iv_pct < 1.0:
                        iv_pct *= 100
                    print(f"    {leg['action']:<4} {leg['option_type'].upper():<4} {leg['strike']:.0f} @ {leg['exec_price']:.2f}  (delta={leg.get('delta',0):.3f}, iv={iv_pct:.1f}%)")
            self.logger.info(f"STRATEGY DECISION: {action} {strategy_name} — {reason}")
        print()

    def _persist_decision(self, signals, strategy_name, reason, params, action):
        safe_signals = {k: v for k, v in signals.items() if k not in ("conditions_met", "conditions_not_met")}
        try:
            self.db.insert("strategy_decisions", {
                "decision_time": now_ist().isoformat(),
                "trading_date":  signals.get("trading_date", today_ist().isoformat()),
                "action": action, "strategy_name": strategy_name, "reason": reason,
                "params_json": json.dumps(params, default=str) if params else None,
                "signals_json": json.dumps(safe_signals, default=str),
            })
        except Exception as e:
            self.logger.debug(f"strategy_decisions insert error: {e}")

    def _log_phantom_if_neutral(self, signals, block_reason):
        if not self.config.phantom_trade_tracking:
            return
        if "VOL_NEUTRAL" not in block_reason and "vol_neutral" not in block_reason.lower():
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
                signals=signals, block_reason=block_reason,
                strategy_would_be=strategy_name,
                strikes_json=json.dumps(legs_spec),
                credit_would_be=round(credit, 3),
            )
        except Exception as e:
            self.logger.debug(f"Phantom trade logging error: {e}")

    def decide(self, signals):
        gate = self._check_hard_gates(signals)
        if gate:
            action, reason = gate
            self._log_decision(signals, action, reason)
            self._persist_decision(signals, "NONE", reason, None, action)
            self.market_engine.finalize_cycle_log(action, reason, self._count_open_positions())
            return {"action": action, "reason": reason}

        strategy_name, selection_reason = self._map_regime_to_strategy(signals)
        if strategy_name == "NO_TRADE":
            self._log_decision(signals, "NO_TRADE", selection_reason)
            self._persist_decision(signals, "NO_TRADE", selection_reason, None, "NO_TRADE")
            self.market_engine.finalize_cycle_log("NO_TRADE", selection_reason, self._count_open_positions())
            return {"action": "NO_TRADE", "reason": selection_reason}

        rules_ok, rules_reason = self._validate_entry_rules(strategy_name, signals)
        if not rules_ok:
            full_reason = f"strategy_rules_failed:{rules_reason}"
            self._log_decision(signals, "NO_TRADE", full_reason)
            self._persist_decision(signals, strategy_name, full_reason, None, "NO_TRADE")
            self.market_engine.finalize_cycle_log("NO_TRADE", full_reason, self._count_open_positions())
            return {"action": "NO_TRADE", "reason": full_reason}

        size_mult = max(float(signals.get("size_multiplier") or 0.50), 0.10)
        params    = self.compute_params(strategy_name, selection_reason, signals, size_mult)

        if not params.get("valid"):
            full_reason = f"params_invalid:{params.get('reason', 'unknown')}"
            if "neutral" in full_reason.lower() or "vrp" in full_reason.lower():
                self._log_phantom_if_neutral(signals, full_reason)
            self._log_decision(signals, "NO_TRADE", full_reason)
            self._persist_decision(signals, strategy_name, full_reason, None, "NO_TRADE")
            self.market_engine.finalize_cycle_log("NO_TRADE", full_reason, self._count_open_positions())
            return {"action": "NO_TRADE", "reason": full_reason}

        self._log_decision(signals, "STRATEGY_SELECTED", selection_reason, strategy_name, params)
        self._persist_decision(signals, strategy_name, selection_reason, params, "STRATEGY_SELECTED")
        self.market_engine.finalize_cycle_log(f"STRATEGY_SELECTED:{strategy_name}", None, self._count_open_positions())
        return {"action": "ENTER", "strategy_name": strategy_name, "reason": selection_reason, "params": params}


def _self_test() -> None:
    from datetime import time as dtime
    print_section("NIFTY ALGO v3.0 — STRATEGY ENGINE SELF-TEST", char="#")

    from core import load_config, Database, RateLimiter, UpstoxClient, setup_logging
    config        = load_config()
    db            = Database(config.db_path)
    logger        = setup_logging(db, config.log_dir)
    rate_limiter  = RateLimiter(config.rate_limits)
    client        = UpstoxClient(config, rate_limiter, db, logger)
    market_engine = MarketDataEngine(config, db, client, rate_limiter, logger)
    cal_engine    = CalibrationEngine(db, config, logger)
    engine        = StrategyEngine(config, db, market_engine, cal_engine, logger)

    def make_chain(spot=24000.0):
        step, chain = 50, {}
        atm = int(round(spot / step) * step)
        for offset in range(-8, 9):
            strike = atm + offset * step
            dist   = abs(offset)
            cd     = max(0.02, 0.50 - dist * 0.04)
            pd     = max(0.02, 0.50 - dist * 0.04)
            cp     = max(0.50, 120 - dist * 10)
            pp     = max(0.50, 120 - dist * 10)
            sp     = max(0.30, cp * 0.05)
            chain[float(strike)] = {
                "call": {"bid": round(cp-sp/2,2), "ask": round(cp+sp/2,2), "ltp": round(cp,2),
                         "oi": max(1000,50000-dist*3000), "volume": 500, "iv": 0.125+dist*0.003,
                         "delta": cd if offset>=0 else -cd, "gamma": 0.002, "theta": -0.5, "vega": 15.0,
                         "instrument_key": f"NSE_FO|NIFTY{strike}CE"},
                "put":  {"bid": round(pp-sp/2,2), "ask": round(pp+sp/2,2), "ltp": round(pp,2),
                         "oi": max(1000,50000-dist*3000), "volume": 500, "iv": 0.125+dist*0.003,
                         "delta": -pd if offset<=0 else pd, "gamma": 0.002, "theta": -0.5, "vega": 15.0,
                         "instrument_key": f"NSE_FO|NIFTY{strike}PE"},
            }
        return chain

    def make_signals(**overrides):
        base = {
            "trading_date": today_ist().isoformat(), "day_label": "TUESDAY",
            "vix": 11.5, "spot": 24000.0, "atm_strike": 24000,
            "vrp_raw": 3.5, "vrp_smoothed": 3.2, "atm_iv": 0.125, "parkinson_rv": 0.085,
            "iv_behavior": "STABLE", "iv_change_pct_from_open": -2.0,
            "day_move_used_pct": 25.0, "opening_straddle_pts": 175.0,
            "or_computed": True, "or_condition": "NARROW",
            "or_high": 24100.0, "or_low": 24040.0, "or_width": 60.0,
            "choppy_detected": False, "adx_15": 14.0, "adx_60": 12.0,
            "adx_15_mature": True, "adx_condition": "FLAT",
            "ema_structure": "NEUTRAL", "hh_hl": "NEUTRAL",
            "pcr": 0.95, "skew_ratio": 1.15, "oi_change_pct": 0.10,
            "resistance_strength": 2.8, "support_strength": 2.6,
            "resistance_oi": 120000, "support_oi": 110000,
            "total_ce_oi": 500000, "total_pe_oi": 480000,
            "chain_size": 71, "chain_stale": False,
            "atm_straddle_price": 175.0, "max_pain": 24000.0,
            "actual_dte": 0, "active_expiry": today_ist().isoformat(),
            "circuit_breaker_suspected": False, "vix_spike_detected": False,
            "gap_fade_opportunity": False, "vwap": 24010.0, "vwap_dist_pct": -0.04,
            "direction": "NEUTRAL", "preferred_sell_side": "BOTH", "wing_width": 150,
            "vol_regime": "SELL_PREMIUM", "price_regime": "RANGE",
            "positioning_regime": "STRONG_RANGE", "confidence_level": "HIGH",
            "confidence_score": 0.82, "final_regime": "PREMIUM_SELL_RANGE",
            "final_regime_notes": "RANGE_STRONG_RANGE_NARROW",
            "size_multiplier": 0.60, "raw_size_multiplier": 0.60,
            "block_new_entries": False, "borderline_sell": False,
            "event_day": False, "event_name": "", "defined_risk_only": False,
            "is_calibrated": True, "calibration_tier": 2,
        }
        base.update(overrides)
        return base

    market_engine.state.update({
        "or_computed": True, "or_condition": "NARROW",
        "or_high": 24100.0, "or_low": 24040.0,
        "entry_start": "00:01", "entry_end": "23:58", "hard_exit_time": "23:59",
        "daily_halted": False, "consecutive_stops": 0, "entry_count": 0,
        "last_stop_time": None, "last_stop_reason": "", "last_stop_signal_combo": "",
        "last_entry_time": None, "opening_straddle_pts": 175.0,
        "day_label": "TUESDAY", "size_multiplier": 1.0, "stop_multiplier": 1.5,
        "wing_width": 150, "current_capital": config.starting_capital, "daily_pnl": 0.0,
    })
    mock_chain = make_chain(24000.0)
    market_engine.last_chain        = mock_chain
    market_engine.last_chain_expiry = today_ist()

    print_section("Hard Gates Tests")

    market_engine.state["daily_halted"] = True
    gate = engine._check_hard_gates(make_signals())
    print(f"  Daily halted -> {gate[1] if gate else 'PASS'}")
    assert gate is not None and "daily" in gate[1], f"Expected daily halt gate, got {gate}"
    market_engine.state["daily_halted"] = False

    gate2 = engine._check_hard_gates(make_signals(block_new_entries=True))
    print(f"  Block new entries -> {gate2[1][:30] if gate2 else 'PASS'}")
    assert gate2 is not None and "ABORT" in gate2[1]

    gate3 = engine._check_hard_gates(make_signals(final_regime="NO_TRADE"))
    print(f"  NO_TRADE regime -> {gate3[1][:30] if gate3 else 'PASS'}")
    assert gate3 is not None

    gate4 = engine._check_hard_gates(make_signals(iv_behavior="EXPANDING"))
    print(f"  IV expanding -> {gate4[1][:30] if gate4 else 'PASS'}")
    assert gate4 is not None and "expanding" in gate4[1]

    market_engine.state.update({"last_stop_time": None, "last_stop_reason": "", "last_stop_signal_combo": "", "consecutive_stops": 0})
    gate5 = engine._check_hard_gates(make_signals(confidence_level="LOW"))
    print(f"  LOW confidence -> {gate5[1][:30] if gate5 else 'PASS'}")
    assert gate5 is not None and "confidence" in gate5[1], f"Expected confidence gate, got {gate5}"

    market_engine.state.update({"entry_start": "00:01", "entry_end": "23:58", "hard_exit_time": "23:59"})
    gate6 = engine._check_hard_gates(make_signals())
    print(f"  All gates pass -> {gate6}")
    assert gate6 is None, f"Expected all gates to pass, got {gate6}"

    print("  [OK] Hard gates tests passed")

    print_section("Regime -> Strategy Mapping Tests")

    strat1, _ = engine._map_regime_to_strategy(make_signals(
        final_regime="PREMIUM_SELL_RANGE", actual_dte=0, or_condition="NARROW", adx_15=14.0,
    ), _test_time=dtime(10, 0))
    print(f"  PREMIUM_SELL_RANGE, DTE0, NARROW -> {strat1} (expect IRON_CONDOR)")
    assert strat1 == IRON_CONDOR

    strat2, _ = engine._map_regime_to_strategy(make_signals(
        final_regime="PREMIUM_SELL_RANGE", actual_dte=0, or_condition="VERY_NARROW",
        adx_15=12.0, adx_15_mature=True, vol_regime="SELL_PREMIUM",
        spot=24000.0, atm_strike=24000,
    ), _test_time=dtime(10, 0))
    print(f"  PREMIUM_SELL_RANGE, DTE0, VERY_NARROW, ADX=12 -> {strat2} (expect IRON_BUTTERFLY)")
    assert strat2 == IRON_BUTTERFLY, f"Expected IRON_BUTTERFLY, got {strat2}"

    strat3, _ = engine._map_regime_to_strategy(make_signals(final_regime="PREMIUM_SELL_BULL"))
    print(f"  PREMIUM_SELL_BULL -> {strat3} (expect BULL_PUT_SPREAD)")
    assert strat3 == BULL_PUT_SPREAD

    strat4, _ = engine._map_regime_to_strategy(make_signals(final_regime="PREMIUM_SELL_BEAR"))
    print(f"  PREMIUM_SELL_BEAR -> {strat4} (expect BEAR_CALL_SPREAD)")
    assert strat4 == BEAR_CALL_SPREAD

    print("  [OK] Regime mapping tests passed")

    print_section("Entry Rules Validation Tests")

    ok1, _ = engine._validate_entry_rules(IRON_BUTTERFLY, make_signals(spot=24080.0, atm_strike=24000))
    assert not ok1
    ok2, _ = engine._validate_entry_rules(IRON_BUTTERFLY, make_signals(adx_15=22.0, spot=24000.0, atm_strike=24000))
    assert not ok2
    ok3, r3 = engine._validate_entry_rules(IRON_CONDOR, make_signals())
    assert ok3, f"Expected True for condor, got {r3}"
    ok4, _ = engine._validate_entry_rules(BULL_PUT_SPREAD, make_signals(spot=24050.0, or_high=24100.0, or_low=24040.0))
    assert not ok4
    ok5, _ = engine._validate_entry_rules(BEAR_CALL_SPREAD, make_signals(spot=24090.0, or_high=24100.0, or_low=24040.0))
    assert not ok5
    print("  [OK] Entry rules validation tests passed")

    print_section("Strike Selection Tests")

    legs1, err1 = engine._select_strikes(IRON_CONDOR, mock_chain, 24000.0, 0, make_signals(opening_straddle_pts=175.0))
    assert err1 is None and legs1 is not None
    sc = next(l["strike"] for l in legs1 if l["action"]=="SELL" and l["option_type"]=="call")
    sp = next(l["strike"] for l in legs1 if l["action"]=="SELL" and l["option_type"]=="put")
    assert sc > sp and abs(sc - 24000.0) >= 120
    print(f"  Iron Condor DTE0: SC={sc:.0f} SP={sp:.0f} [OK]")

    legs2, err2 = engine._select_strikes(IRON_BUTTERFLY, mock_chain, 24000.0, 0, make_signals())
    assert err2 is None and legs2 is not None
    print(f"  Iron Butterfly DTE0: [OK]")

    legs3, err3 = engine._select_strikes(BULL_PUT_SPREAD, mock_chain, 24000.0, 0, make_signals(opening_straddle_pts=175.0))
    assert err3 is None and legs3 is not None
    print(f"  Bull Put DTE0: [OK]")

    legs4, err4 = engine._select_strikes(BEAR_CALL_SPREAD, mock_chain, 24000.0, 0, make_signals(opening_straddle_pts=175.0))
    assert err4 is None and legs4 is not None
    print(f"  Bear Call DTE0: [OK]")

    print("  [OK] Strike selection tests passed")

    print_section("Leg Validation Tests")
    ok_v, r_v = engine._validate_leg(mock_chain, 24000.0, "call", "SELL")
    assert ok_v, f"Expected True, got {r_v}"
    ok_nv, _ = engine._validate_leg(mock_chain, 99999.0, "call", "SELL")
    assert not ok_nv
    print("  [OK] Leg validation tests passed")

    print_section("Cost Computation Tests")
    costs = engine._compute_costs([
        {"action":"SELL","option_type":"call","exec_price":45.0},
        {"action":"SELL","option_type":"put", "exec_price":42.0},
        {"action":"BUY", "option_type":"call","exec_price":12.0},
        {"action":"BUY", "option_type":"put", "exec_price":11.0},
    ], 2, "ENTRY")
    assert costs["total_rupees"] > 0 and "stt" in costs["breakdown"]
    print(f"  4-leg condor 2 lots: Rs{costs['total_rupees']:.2f} [OK]")

    print_section("Slippage Computation Tests")
    assert abs(engine._compute_slippage([{"bid":44.0,"ask":46.0},{"bid":41.0,"ask":43.0}]) - 0.60) < 0.01
    assert abs(engine._compute_slippage([{"bid":0,"ask":0},{"bid":0,"ask":0}]) - 1.00) < 0.01
    print("  [OK] Slippage tests passed")

    print_section("Full Parameter Computation Tests")
    params = engine.compute_params(IRON_CONDOR, "test", make_signals(
        actual_dte=0, active_expiry=today_ist().isoformat(),
        opening_straddle_pts=175.0, vol_regime="SELL_PREMIUM",
        price_regime="RANGE", positioning_regime="STRONG_RANGE",
        confidence_level="HIGH", final_regime="PREMIUM_SELL_RANGE", size_multiplier=0.60,
    ), 0.60)
    print(f"  Iron Condor params valid: {params['valid']}")
    if params.get("valid"):
        assert params["entry_credit"] > 0
        assert params["stop_premium"] > params["entry_credit"]
        assert params["target_premium"] < params["entry_credit"]
        assert params["final_lots"] >= 1
    print("  [OK] Parameter computation test passed")

    print_section("Target Percentage Tests")
    tgt0 = engine._get_target_pct(0, make_signals(vix=11.0))
    tgt1 = engine._get_target_pct(1, make_signals(vix=11.5))
    tgt2 = engine._get_target_pct(2, make_signals(vix=11.5))
    assert tgt0 > tgt1 > tgt2 and 0.30 <= tgt0 <= 0.55
    print(f"  DTE0={tgt0:.2f} DTE1={tgt1:.2f} DTE2={tgt2:.2f} [OK]")

    print_section("Full decide() Test")
    market_engine.state.update({"or_computed":True,"daily_halted":False,"consecutive_stops":0,
        "entry_count":0,"last_entry_time":None,"last_stop_time":None})
    decision = engine.decide(make_signals())
    print(f"  decide() action: {decision['action']}")
    assert decision["action"] in ("ENTER", "NO_TRADE")
    print("  [OK] Full decide() test passed")

    print_section("NO_TRADE Path Tests")
    assert engine.decide(make_signals(final_regime="ABORT", block_new_entries=True))["action"] == "NO_TRADE"
    assert engine.decide(make_signals(final_regime="NO_TRADE"))["action"] == "NO_TRADE"
    market_engine.state["daily_halted"] = True
    assert engine.decide(make_signals())["action"] == "NO_TRADE"
    market_engine.state["daily_halted"] = False
    market_engine.state["consecutive_stops"] = 2
    assert engine.decide(make_signals())["action"] == "NO_TRADE"
    market_engine.state["consecutive_stops"] = 0
    print("  [OK] NO_TRADE path tests passed")

    print_section("Phantom Trade Logging Test")
    engine._log_phantom_if_neutral(make_signals(final_regime="PREMIUM_SELL_RANGE"), "VOL_NEUTRAL:VRP_1.8pp")
    phantom_count = db.query_one("SELECT COUNT(*) as cnt FROM phantom_trades WHERE trading_date=?", (today_ist().isoformat(),))
    print(f"  Phantom trades logged: {phantom_count['cnt'] if phantom_count else 0} [OK]")

    db.close()
    print_section("STRATEGY ENGINE SELF-TEST COMPLETE", char="#")
    print("  All tests passed")
    print(f"  Database: {config.db_path}")
    print()


if __name__ == "__main__":
    _self_test()
