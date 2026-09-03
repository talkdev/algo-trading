"""
════════════════════════════════════════════════════════════════════════════
NIFTY INTRADAY OPTIONS ALGO TRADING ENGINE — 2026 PRODUCTION BUILD
FILE 3 of 5 : STRATEGY ENGINE
════════════════════════════════════════════════════════════════════════════

Save as: strategy_engine.py  (same directory as nifty_algo_core.py and
                               market_data_engine.py)

REQUIRES the small patch to market_data_engine.py described above (caches
self.last_chain / self.last_chain_expiry after each fetch).

Implements:
    Module 7 — Strategy Selection
        Step 7.1: hard no-trade gates (daily halt, circuit breaker, VIX spike,
                  time windows, cooldowns, data quality, event/VIX-suppressed
                  blocks, entry-timing WAIT block)
        Step 7.3: VRP × Trend × Direction selection matrix
        Step 7.4: post-selection downgrades (straddle->condor, condor->spread
                  on direction shift, half-size application)
        Step 7.5: strategy-specific entry rules
    Module 8 — Strategy Parameter Computation
        Strike selection (delta targeting + liquidity/spread/OI validation)
        Gross/net credit-debit, slippage, full transaction cost stack
        Credit-floor + credit/width-ratio + exit-cost-positive checks
        Position sizing (risk % of current capital, with a tail-risk buffer),
        margin estimation, stop/target/price-stop computation

Every decision (ENTER or NO_TRADE, with full reasoning) is:
    - printed to console
    - written to the audit log (via the shared logger from File 1)
    - persisted to a new `strategy_decisions` table
    - used to enrich the current cycle_log row via finalize_cycle_log()

FIXES APPLIED VS. THE ORIGINAL SPEC (identified during earlier review):
  - Direction-shift downgrade (Step 7.4) now also triggers on MILD_BULLISH/
    MILD_BEARISH, not only the strong BULLISH/BEARISH labels.
  - The 10% credit/width ratio discipline (previously condor/butterfly only)
    is now also applied to 2-leg Bull Put / Bear Call spreads.
  - Position sizing multiplies the theoretical stop-triggered loss by 1.25x
    before sizing, to avoid understating risk if an exit doesn't fill
    exactly at the theoretical stop price (a gap/slippage buffer).
"""

from __future__ import annotations

import json
from datetime import datetime, date, time as dtime
from typing import Optional

from nifty_algo_core import (
    Config, Database, RateLimiter, UpstoxClient,
    now_ist, today_ist, print_section, print_kv_table,
    load_config, setup_logging,
)
from market_data_engine import MarketDataEngine, ensure_column

# ─────────────────────────────────────────────────────────────────────────
# STRATEGY CONSTANTS
# ─────────────────────────────────────────────────────────────────────────
DTE_REQUIREMENTS = {
    "IRON_BUTTERFLY": (0, 7), "IRON_CONDOR": (1, 8),
    "BULL_PUT_SPREAD": (0, 8), "BEAR_CALL_SPREAD": (0, 8),
    "BULL_CALL_SPREAD": (1, 5), "BEAR_PUT_SPREAD": (1, 5),
    "LONG_STRADDLE": (1, 5), "POST_EVENT_STRADDLE": (0, 3),
}

MIN_CREDITS = {
    "IRON_BUTTERFLY": 20, "IRON_CONDOR": 18,
    "BULL_PUT_SPREAD": 15, "BEAR_CALL_SPREAD": 14, "POST_EVENT_STRADDLE": 25,
}
MIN_CREDITS_TUESDAY = {
    "IRON_BUTTERFLY": 25, "IRON_CONDOR": 18, "BULL_PUT_SPREAD": 16, "BEAR_CALL_SPREAD": 15,
}

PRICE_STOPS = {
    "IRON_BUTTERFLY": 50, "IRON_CONDOR": 80, "BULL_PUT_SPREAD": 80,
    "BEAR_CALL_SPREAD": 80, "POST_EVENT_STRADDLE": 120,
}

TARGET_PCT_BY_DAY = {"MONDAY": 0.32, "TUESDAY": 0.30, "WEDNESDAY": 0.35,
                      "THURSDAY": 0.35, "FRIDAY": 0.32}

MIN_CREDIT_MULTIPLIER_BY_REGIME = {"SUPPRESSED": 1.0, "LOW": 1.1, "NORMAL": 1.0,
                                    "ELEVATED": 1.15, "HIGH": 1.3}

LOT_CAPS_BY_DAY = {"TUESDAY": 2, "MONDAY": 1, "FRIDAY": 2}

STRATEGY_STATE_EXTRA_COLUMNS = [
    ("pre_event_spot", "REAL"),
    ("pre_event_iv", "REAL"),
    ("event_announcement_time", "TEXT"),
]


class StrategyEngine:
    """
    Consumes the `signals` dict produced by MarketDataEngine.run_cycle() and
    decides whether to trade, what to trade, and with exactly what parameters.
    Holds no state of its own beyond what's mirrored in the DB via
    market_engine.state and the positions table.
    """

    def __init__(self, config: Config, db: Database, market_engine: MarketDataEngine, logger):
        self.config = config
        self.db = db
        self.market_engine = market_engine
        self.logger = logger

        for col, coltype in STRATEGY_STATE_EXTRA_COLUMNS:
            ensure_column(self.db, "session_state", col, coltype)
        self._ensure_strategy_tables()

    def _ensure_strategy_tables(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS strategy_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_time TEXT, trading_date TEXT, action TEXT, strategy_name TEXT,
                reason TEXT, params_json TEXT, signals_json TEXT
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategy_decisions_time ON strategy_decisions(decision_time)"
        )

    # ─────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────
    def _time_diff_minutes(self, t1: dtime, t2: dtime) -> float:
        dt1 = datetime.combine(today_ist(), t1)
        dt2 = datetime.combine(today_ist(), t2)
        return (dt2 - dt1).total_seconds() / 60.0

    def _count_open_positions(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) as cnt FROM positions WHERE trading_date=? AND status='OPEN'",
            (today_ist().isoformat(),),
        )
        return row["cnt"] if row else 0

    def _straddle_allowed(self, s: dict) -> bool:
        if s["vix_regime"] == "HIGH":
            return False
        if s["vix_regime"] == "SUPPRESSED":
            return False
        if s["vix_regime"] == "ELEVATED":
            if s.get("or_condition") in ("VERY_NARROW", "NARROW") and (s.get("vrp") or 0) > 3.0:
                elevated_vix_narrow_or_straddle_allowed = True
                return True
            return False
        if s["day_mode"] == "PRE_EVENT":
            return False
        return True

    def _get_target_pct(self, s: dict) -> float:
        return TARGET_PCT_BY_DAY.get(self.market_engine.state.get("day_label"), 0.40)

    def _get_min_credit_multiplier(self, s: dict) -> float:
        return MIN_CREDIT_MULTIPLIER_BY_REGIME.get(s["vix_regime"], 1.0)

    def _compute_entry_timing(self, s: dict) -> tuple[str, list]:
        notes = []
        timing = "GOOD"
        current_time = now_ist().time()
        state = self.market_engine.state
        dte = state.get("actual_dte")

        if dtime(9, 15) <= current_time < dtime(9, 30):
            return "WAIT", ["first_15min_spread_too_wide_avoid_entry"]

        if dte == 0 and dtime(12, 0) <= current_time < dtime(12, 30):
            return "WAIT", ["0dte_avoid_12pm_eu_open_transition"]

        if dte == 0 and current_time >= dtime(14, 0):
            return "WAIT", ["0dte_no_new_positions_after_14:00"]

        if current_time >= dtime(15, 0):
            return "WAIT", ["last_30min_gamma_spread_blowout"]

        trend_cond = s.get("trend_condition", "")
        vwap_signal_active = s["vwap_signal"] not in ("UNKNOWN", None) and current_time < dtime(14, 30)
        if vwap_signal_active and s["vwap_signal"] in ("BULLISH_EXTENDED", "BEARISH_EXTENDED"):
            if trend_cond not in ("TRENDING", "STRONG_TREND"):
                timing = "WAIT"
                dist = s.get("vwap_dist_pct")
                notes.append(f"Spot extended from VWAP ({dist:.2f}%) — wait for mean reversion"
                             if dist is not None else "Spot extended from VWAP — wait for mean reversion")

        spot_vs_or = s.get("spot_vs_or", "") or ""
        or_width = s.get("or_width") or 50
        if spot_vs_or.startswith("ABOVE_OR") or spot_vs_or.startswith("BELOW_OR"):
            try:
                breakout_pts = float(spot_vs_or.split("_")[-1].replace("pts", ""))
            except ValueError:
                breakout_pts = 0.0
            if or_width and breakout_pts > or_width * 0.75:
                if timing == "GOOD":
                    timing = "CAUTION"
                notes.append(f"Spot has broken OR by {breakout_pts:.0f}pts — trend day risk")

        if current_time < dtime(10, 30):
            if timing == "GOOD":
                timing = "CAUTION"
            notes.append("Early in trading window — opening volatility may not have settled")
        if current_time > dtime(13, 0):
            if timing == "GOOD":
                timing = "CAUTION"
            notes.append("Late in trading window — reduced time for IV compression before hard exit")
        return timing, notes

    def _update_event_detection(self, s: dict) -> None:
        state = self.market_engine.state
        if s["day_mode"] != "EVENT" or state.get("event_announced"):
            return
        if state.get("pre_event_iv") is None:
            if s["atm_iv"]:
                state["pre_event_iv"] = s["atm_iv"]
                state["pre_event_spot"] = s["spot"]
            return
        pre_iv = state["pre_event_iv"]
        if s["atm_iv"] and pre_iv and pre_iv > 0:
            drop_pct = (pre_iv - s["atm_iv"]) / pre_iv * 100.0
            if drop_pct >= 10.0:
                state["event_announced"] = True
                state["event_announcement_time"] = now_ist().isoformat()
                self.logger.info(f"EVENT ANNOUNCEMENT DETECTED: IV dropped {drop_pct:.1f}% "
                                  f"from pre-event {pre_iv*100:.2f}% to {s['atm_iv']*100:.2f}%")

    # ─────────────────────────────────────────────────────────────────
    # STEP 7.1: HARD NO-TRADE GATES
    # ─────────────────────────────────────────────────────────────────
    def _check_hard_gates(self, s: dict) -> Optional[tuple[str, str]]:
        state = self.market_engine.state
        current_time = now_ist().time()

        if state.get("daily_halted"):
            return "NO_TRADE", "daily_loss_limit_reached_or_halted"
        if s["circuit_breaker_suspected"]:
            return "NO_TRADE", "circuit_breaker_suspected_halt_trading"
        if s["vix_spike_detected"]:
            return "NO_TRADE", "vix_spike_detected_no_new_sells"
        if s.get("intraday_rv_selling_veto"):
            return "NO_TRADE", "intraday_rv_exceeds_atm_iv_no_premium_selling"
        if s.get("iv_behavior") == "EXPANDING" and s.get("sell_ok"):
            return "NO_TRADE", "iv_expanding_no_new_sells_wait_for_stable_or_declining"
        if s.get("iv_behavior") == "SPIKING":
            return "NO_TRADE", "iv_spiking_no_new_sells"

        try:
            entry_start = datetime.strptime(state["entry_start"], "%H:%M").time()
            entry_end = datetime.strptime(state["entry_end"], "%H:%M").time()
        except Exception:
            entry_start, entry_end = self.config.trading_window_start, self.config.trading_window_last_entry

        if current_time < entry_start:
            return "NO_TRADE", f"before_entry_window_start_{entry_start}"
        if current_time > entry_end:
            return "NO_TRADE", f"past_entry_window_end_{entry_end}"

        if state.get("entry_count", 0) >= self.config.max_entries_per_day:
            return "NO_TRADE", "max_entries_per_day_reached"

        if self._count_open_positions() >= self.config.max_concurrent_positions:
            return "NO_TRADE", "max_concurrent_positions_reached"

        if state.get("consecutive_stops", 0) >= 3:
            return "NO_TRADE", "3_consecutive_stops_today_halt"
        _last_stop_reason = state.get("last_stop_reason", "")
        _last_stop_signal = state.get("last_stop_signal_combo", "")
        _current_signal_combo = f"{s.get('volatility_condition')}_{s.get('trend_condition')}_{s.get('direction')}"
        stop_reason_suppression = True
        if (_last_stop_reason == "CLOSE_STOP" and _last_stop_signal == _current_signal_combo
                and state.get("consecutive_stops", 0) >= 1):
            return "NO_TRADE", f"same_signal_combo_caused_last_stop_{_current_signal_combo}"

        last_stop_time = state.get("last_stop_time")
        if last_stop_time:
            last_stop_reason = state.get("last_stop_reason", "")
            cooldown_map = {"CLOSE_ADX": 45, "CLOSE_VWAP": 20, "CLOSE_STOP": 30}
            iv_expanding_cooldown = 20 if s.get("iv_behavior") in ("EXPANDING", "SPIKING") else 0
            required_cooldown = cooldown_map.get(last_stop_reason, 30) + iv_expanding_cooldown
            try:
                minutes_since = (now_ist() - datetime.fromisoformat(last_stop_time)).total_seconds() / 60.0
            except Exception:
                minutes_since = required_cooldown
            if minutes_since < required_cooldown:
                return "NO_TRADE", f"stop_cooldown_{required_cooldown - minutes_since:.0f}min_remaining"

        if s["volatility_condition"] == "UNKNOWN":
            return "NO_TRADE", "vrp_unknown_insufficient_data"
        if not state.get("or_computed"):
            return "NO_TRADE", "opening_range_not_yet_computed"
        if s["trend_condition"] == "OR_PENDING":
            return "NO_TRADE", "opening_range_pending"
        if s["day_mode"] == "EVENT" and not state.get("event_announced"):
            return "NO_TRADE", "event_day_awaiting_announcement"
        if s["vix_regime"] == "SUPPRESSED":
            return "NO_TRADE", "vix_suppressed_no_edge"
        gap_fade = self.market_engine.state.get("gap_fade_opportunity", False)
        if s["or_condition"] == "VERY_WIDE" and s["volatility_condition"] in ("RICH", "VERY_RICH"):
            if not gap_fade:
                return "NO_TRADE", "very_wide_or_dangerous_to_sell_premium"
            self.logger.info("VERY_WIDE OR but gap_fade_opportunity active — allowing condor")

        entry_timing, timing_notes = self._compute_entry_timing(s)
        if entry_timing == "WAIT":
            return "NO_TRADE", f"entry_timing_wait: {'; '.join(timing_notes)}"

        hard_exit_str = state.get("hard_exit_time", self.config.hard_exit_time.strftime("%H:%M"))
        try:
            hard_exit = datetime.strptime(hard_exit_str, "%H:%M").time()
        except Exception:
            hard_exit = self.config.hard_exit_time
        minutes_to_exit = self._time_diff_minutes(current_time, hard_exit)
        if minutes_to_exit < 60:
            return "NO_TRADE", f"only_{minutes_to_exit:.0f}min_before_hard_exit_insufficient"

        return None

    # ─────────────────────────────────────────────────────────────────
    # STEP 7.3: STRATEGY SELECTION MATRIX
    # ─────────────────────────────────────────────────────────────────
    def _select_strategy(self, s: dict) -> tuple[str, str]:
        vol, trend, dirn = s["volatility_condition"], s["trend_condition"], s["direction"]
        sell_ok, buy_ok = s["sell_ok"], s["buy_ok"]
        vwap_sig, adx_dir = s["vwap_signal"], s["adx_direction"]
        straddle_allowed = self._straddle_allowed(s)

        if vol in ("VERY_RICH", "RICH") and sell_ok:
            if trend in ("RANGE_BOUND", "MILD_RANGE", "RANGE_ASSUMED"):
                if dirn == "NEUTRAL":
                    if (vol in ("VERY_RICH", "RICH") and s["or_condition"] in ("VERY_NARROW", "NARROW")
                            and straddle_allowed):
                        return "IRON_BUTTERFLY", f"neutral+{vol}_vrp+{s['or_condition']}_or+straddle_allowed"
                    return "IRON_CONDOR", f"neutral+{vol}_vrp+{s['or_condition']}_or"
                elif dirn in ("BULLISH", "MILD_BULLISH"):
                    if vwap_sig == "BULLISH_EXTENDED":
                        return "NO_TRADE", "bullish_extended_above_vwap_wait_for_pullback"
                    if vwap_sig in ("BEARISH", "BEARISH_EXTENDED"):
                        return "NO_TRADE", "spot_below_vwap_cannot_sell_put_spread_direction_wrong"
                    return "BULL_PUT_SPREAD", f"bullish+{vol}_vrp+{trend}"
                elif dirn in ("BEARISH", "MILD_BEARISH"):
                    if vwap_sig == "BEARISH_EXTENDED":
                        return "NO_TRADE", "bearish_extended_below_vwap_wait_for_recovery"
                    if vwap_sig in ("BULLISH", "BULLISH_EXTENDED"):
                        return "NO_TRADE", "spot_above_vwap_cannot_sell_call_spread_direction_wrong"
                    return "BEAR_CALL_SPREAD", f"bearish+{vol}_vrp+{trend}"

            elif trend == "UNCERTAIN":
                if dirn in ("BULLISH", "MILD_BULLISH"):
                    if vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                        return "BULL_PUT_SPREAD", f"bullish+{vol}_vrp+uncertain_trend_half_size"
                    return "NO_TRADE", "uncertain_trend_vwap_contradicts_direction"
                elif dirn in ("BEARISH", "MILD_BEARISH"):
                    if vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                        return "BEAR_CALL_SPREAD", f"bearish+{vol}_vrp+uncertain_trend_half_size"
                    return "NO_TRADE", "uncertain_trend_vwap_contradicts_direction"
                return "NO_TRADE", "uncertain_trend_neutral_direction_no_edge"

            elif trend in ("MILD_TREND", "TRENDING"):
                if adx_dir == "BULLISH" and dirn in ("BULLISH", "MILD_BULLISH") \
                        and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                    return "BULL_PUT_SPREAD", f"bullish_trend_sell_puts_aligned+{vol}_vrp"
                if adx_dir == "BEARISH" and dirn in ("BEARISH", "MILD_BEARISH") \
                        and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                    return "BEAR_CALL_SPREAD", f"bearish_trend_sell_calls_aligned+{vol}_vrp"
                return "NO_TRADE", f"trending_no_aligned_side_adx_dir={adx_dir}_direction={dirn}"

            elif trend == "STRONG_TREND":
                if adx_dir == "BULLISH" and dirn in ("BULLISH", "MILD_BULLISH") \
                        and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                    return "BULL_PUT_SPREAD", f"bullish_strong_trend_put_credit_safe_side+{vol}_vrp"
                if adx_dir == "BEARISH" and dirn in ("BEARISH", "MILD_BEARISH") \
                        and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                    return "BEAR_CALL_SPREAD", f"bearish_strong_trend_call_credit_safe_side+{vol}_vrp"
                return "NO_TRADE", "strong_trend_no_aligned_direction_for_credit_spread"

        elif vol == "FAIR" and sell_ok:
            if trend in ("RANGE_BOUND", "MILD_RANGE", "RANGE_ASSUMED"):
                if dirn in ("BULLISH", "MILD_BULLISH"):
                    if vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                        return "BULL_PUT_SPREAD", "bullish+fair_vrp+range_half_size"
                    return "NO_TRADE", "fair_vrp_vwap_contradicts_direction"
                elif dirn in ("BEARISH", "MILD_BEARISH"):
                    if vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                        return "BEAR_CALL_SPREAD", "bearish+fair_vrp+range_half_size"
                    return "NO_TRADE", "fair_vrp_vwap_contradicts_direction"
                if dirn == "NEUTRAL" and straddle_allowed:
                    return "IRON_CONDOR", "neutral+fair_vrp+range_quarter_size"
                return "NO_TRADE", "fair_vrp_neutral_direction_insufficient_edge"
            elif trend == "MILD_TREND":
                if adx_dir == "BULLISH" and dirn in ("BULLISH", "MILD_BULLISH") \
                        and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                    return "BULL_PUT_SPREAD", "bullish+fair_vrp+mild_trend_aligned_half_size"
                if adx_dir == "BEARISH" and dirn in ("BEARISH", "MILD_BEARISH") \
                        and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                    return "BEAR_CALL_SPREAD", "bearish+fair_vrp+mild_trend_aligned_half_size"
                return "NO_TRADE", "fair_vrp_mild_trend_no_aligned_side"
            return "NO_TRADE", "fair_vrp_trending_no_trade"

        elif vol in ("THIN", "CHEAP", "INVERTED") and buy_ok:
            if trend in ("TRENDING", "STRONG_TREND"):
                if dirn in ("BULLISH", "MILD_BULLISH"):
                    return "BULL_CALL_SPREAD", f"bullish+{vol.lower()}_iv+trending"
                elif dirn in ("BEARISH", "MILD_BEARISH"):
                    return "BEAR_PUT_SPREAD", f"bearish+{vol.lower()}_iv+trending"
                elif dirn == "NEUTRAL":
                    if vol in ("CHEAP", "INVERTED"):
                        return "LONG_STRADDLE", "neutral+cheap_iv+strong_trend_breakout"
                    return "NO_TRADE", "thin_iv_no_direction_no_trade"
            elif trend in ("RANGE_BOUND", "MILD_RANGE", "RANGE_ASSUMED"):
                return "NO_TRADE", "cheap_iv_range_bound_theta_decay_kills_long"
            else:
                return "NO_TRADE", "cheap_iv_uncertain_trend_no_trade"

        if s["day_mode"] == "EVENT" and self.market_engine.state.get("event_announced"):
            ok, reason = self._check_post_event_conditions(s)
            return ("POST_EVENT_STRADDLE", reason) if ok else ("NO_TRADE", reason)

        return "NO_TRADE", f"no_conditions_met: vol={vol} trend={trend} dir={dirn} sell_ok={sell_ok} buy_ok={buy_ok}"

    def _check_post_event_conditions(self, s: dict) -> tuple[bool, str]:
        if s["atm_iv"] is None or s["atm_iv"] <= 0:
            return False, "post_event_atm_iv_unavailable"
        if s["atm_iv"] * 100 <= 14.0:
            return False, "post_event_iv_already_compressed_below_14pct"
        if now_ist().time() >= dtime(12, 30):
            return False, "post_event_too_late_after_12:30"

        state = self.market_engine.state
        pre_event_spot = state.get("pre_event_spot")
        if pre_event_spot and pre_event_spot > 0 and s["spot"]:
            move_pct = abs(s["spot"] - pre_event_spot) / pre_event_spot * 100
            if move_pct > 1.0:
                return False, f"post_event_nifty_moved_{move_pct:.1f}pct_surprise_iv_wont_compress"

        announcement_time = state.get("event_announcement_time")
        if announcement_time:
            try:
                mins_since = (now_ist() - datetime.fromisoformat(announcement_time)).total_seconds() / 60.0
                if mins_since < 5:
                    return False, f"post_event_wait_{5-mins_since:.0f}min_for_panic_to_settle"
            except Exception:
                pass

        return True, f"post_event_iv_crush_opportunity_iv={s['atm_iv']*100:.1f}pct_still_elevated"

    # ─────────────────────────────────────────────────────────────────
    # STEP 7.4: POST-SELECTION VALIDATION / DOWNGRADES
    # ─────────────────────────────────────────────────────────────────
    def _post_selection_validation(self, strategy_name: str, reason: str, s: dict) -> tuple[str, str, float]:
        state = self.market_engine.state
        size_mult = state.get("size_multiplier", 1.0)
        straddle_allowed = self._straddle_allowed(s)

        if strategy_name in ("IRON_BUTTERFLY", "POST_EVENT_STRADDLE") and not straddle_allowed:
            actual_dte_downgrade = self.market_engine.state.get("actual_dte")
            dirn_downgrade = s.get("direction", "NEUTRAL")
            side_downgrade = s.get("preferred_sell_side", "BOTH")
            if actual_dte_downgrade == 0:
                if dirn_downgrade in ("BULLISH", "MILD_BULLISH") and side_downgrade == "PUTS":
                    strategy_name = "BULL_PUT_SPREAD"
                elif dirn_downgrade in ("BEARISH", "MILD_BEARISH") and side_downgrade == "CALLS":
                    strategy_name = "BEAR_CALL_SPREAD"
                elif dirn_downgrade == "NEUTRAL":
                    strategy_name = "IRON_BUTTERFLY"
                    reason += "_0dte_neutral_use_iron_butterfly"
                elif side_downgrade == "PUTS":
                    strategy_name = "BULL_PUT_SPREAD"
                else:
                    strategy_name = "BEAR_CALL_SPREAD"
                reason += "_downgraded_0dte_straddle_not_allowed"
                self.logger.info(f"Strategy downgraded -> {strategy_name} (0DTE straddle not allowed)")
            else:
                strategy_name = "IRON_CONDOR"
                reason += "_downgraded_straddle_not_allowed"
                self.logger.info("Strategy downgraded -> IRON_CONDOR (straddle not allowed)")

        if strategy_name in ("IRON_BUTTERFLY", "IRON_CONDOR"):
            dirn, side = s["direction"], s["preferred_sell_side"]
            # FIX vs original spec: also catch MILD_BULLISH/MILD_BEARISH, not
            # only the strong labels.
            if dirn in ("BULLISH", "BEARISH", "MILD_BULLISH", "MILD_BEARISH"):
                if side == "PUTS":
                    strategy_name = "BULL_PUT_SPREAD"
                    reason += "_downgraded_direction_shifted"
                elif side == "CALLS":
                    strategy_name = "BEAR_CALL_SPREAD"
                    reason += "_downgraded_direction_shifted"

        if "quarter_size" in reason:
            size_mult *= 0.25
            self.logger.info(f"Quarter-size applied due to: {reason}")
        elif "half_size" in reason or "uncertain" in reason or "fair_vrp" in reason:
            size_mult *= 0.50
            self.logger.info(f"Half-size applied due to: {reason}")

        sell_size_reduction = s.get("sell_size_reduction", 1.0)
        if sell_size_reduction < 1.0:
            size_mult *= sell_size_reduction

        return strategy_name, reason, max(size_mult, 0.25)

    # ─────────────────────────────────────────────────────────────────
    # STEP 7.5: STRATEGY-SPECIFIC ENTRY RULES
    # ─────────────────────────────────────────────────────────────────
    def _validate_strategy_entry_rules(self, strategy_name: str, s: dict) -> tuple[bool, str]:
        state = self.market_engine.state
        day_label = state.get("day_label")
        current_time = now_ist().time()
        spot = s["spot"]

        if strategy_name == "IRON_BUTTERFLY":
            step = self.config.nifty_strike_step
            atm_strike = round(spot / step) * step if spot else None
            if atm_strike is not None and abs(spot - atm_strike) > 20:
                return False, f"spot_{spot:.0f}_too_far_from_atm_{atm_strike:.0f}"
            if day_label not in ("TUESDAY", "MONDAY") and s["or_condition"] not in ("VERY_NARROW", "NARROW"):
                return False, "iron_butterfly_requires_tuesday_monday_or_narrow_or"
            if day_label == "MONDAY" and s["or_condition"] not in ("VERY_NARROW", "NARROW"):
                return False, "iron_butterfly_monday_requires_narrow_or"

        elif strategy_name == "IRON_CONDOR":
            hard_exit_str = state.get("hard_exit_time")
            try:
                hard_exit = datetime.strptime(hard_exit_str, "%H:%M").time()
            except Exception:
                hard_exit = self.config.hard_exit_time
            mins_to_exit = self._time_diff_minutes(current_time, hard_exit)
            actual_dte_condor = state.get("actual_dte")
            if actual_dte_condor == 0:
                if mins_to_exit < 90:
                    return False, f"iron_condor_0dte_needs_90min_before_exit_only_{mins_to_exit:.0f}min"
            else:
                if mins_to_exit < 120:
                    return False, f"iron_condor_needs_2hr_before_exit_only_{mins_to_exit:.0f}min"
            if s["adx_condition"] in ("STRONG", "VERY_STRONG"):
                return False, "iron_condor_blocked_adx_trending"

        elif strategy_name == "BULL_PUT_SPREAD":
            if s["vwap"] and spot and spot < s["vwap"]:
                return False, f"bull_put_spread_requires_spot_above_vwap_spot={spot:.0f}_vwap={s['vwap']:.0f}"
            if (s["spot_vs_or"] or "").startswith("BELOW_OR"):
                try:
                    breakdown = float(s["spot_vs_or"].split("_")[-1].replace("pts", ""))
                except ValueError:
                    breakdown = 0.0
                if breakdown > 30:
                    return False, f"bull_put_spread_spot_below_or_by_{breakdown:.0f}pts"

        elif strategy_name == "BEAR_CALL_SPREAD":
            if s["vwap"] and spot and spot > s["vwap"]:
                return False, f"bear_call_spread_requires_spot_below_vwap_spot={spot:.0f}_vwap={s['vwap']:.0f}"
            if (s["spot_vs_or"] or "").startswith("ABOVE_OR"):
                try:
                    breakout = float(s["spot_vs_or"].split("_")[-1].replace("pts", ""))
                except ValueError:
                    breakout = 0.0
                if breakout > 30:
                    return False, f"bear_call_spread_spot_above_or_by_{breakout:.0f}pts"

        elif strategy_name == "BULL_CALL_SPREAD":
            if s["volatility_condition"] in ("FAIR", "RICH", "VERY_RICH"):
                return False, "bull_call_spread_requires_cheap_iv_not_rich"
            if s["adx_direction"] != "BULLISH":
                return False, "bull_call_spread_requires_bullish_adx_direction"

        elif strategy_name == "BEAR_PUT_SPREAD":
            if s["volatility_condition"] in ("FAIR", "RICH", "VERY_RICH"):
                return False, "bear_put_spread_requires_cheap_iv_not_rich"
            if s["adx_direction"] != "BEARISH":
                return False, "bear_put_spread_requires_bearish_adx_direction"

        elif strategy_name == "LONG_STRADDLE":
            if s["volatility_condition"] not in ("CHEAP", "INVERTED"):
                return False, "long_straddle_requires_cheap_or_inverted_iv"
            if s["trend_condition"] not in ("TRENDING", "STRONG_TREND"):
                return False, "long_straddle_requires_trending_market"
            if current_time > dtime(12, 30):
                return False, "long_straddle_too_late_after_12:30"

        elif strategy_name == "POST_EVENT_STRADDLE":
            if s["atm_iv"] and s["atm_iv"] * 100 < 14.0:
                return False, "post_event_iv_already_compressed"

        return True, "entry_rules_passed"

    # ─────────────────────────────────────────────────────────────────
    # MODULE 8: STRIKE SELECTION HELPERS
    # ─────────────────────────────────────────────────────────────────
    def _find_strike_by_delta(self, chain: dict, opt_type: str, target_delta: float,
                                tolerance: float = 0.08) -> tuple[Optional[float], Optional[float]]:
        best_strike, best_delta, best_diff = None, None, float("inf")
        for strike, data in chain.items():
            opt = data.get(opt_type, {})
            delta = opt.get("delta")
            if delta is None:
                continue
            diff = abs(abs(delta) - target_delta)
            if diff < best_diff:
                best_diff, best_strike, best_delta = diff, strike, delta
        if best_strike is None or best_diff > tolerance:
            return None, None
        return best_strike, best_delta

    def _validate_strike(self, chain: dict, strike: float, opt_type: str, action: str = "SELL") -> tuple[bool, str]:
        if strike not in chain:
            return False, f"strike_{strike}_not_in_chain"
        opt = chain[strike].get(opt_type, {})
        bid, ask, oi, ltp = opt.get("bid", 0), opt.get("ask", 0), opt.get("oi", 0), opt.get("ltp", 0)
        if bid <= 0 and ask <= 0:
            return False, f"strike_{strike}_{opt_type}_no_bid_ask"
        min_oi = 500 if action == "SELL" else 100
        max_spread_pct = 0.15 if action == "SELL" else 0.30
        if oi < min_oi:
            return False, f"strike_{strike}_{opt_type}_oi_{oi}_below_{min_oi}"
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            if mid > 0 and (ask - bid) / mid > max_spread_pct:
                return False, f"strike_{strike}_{opt_type}_spread_too_wide"
        effective_price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else ltp
        if effective_price < 0.50:
            return False, f"strike_{strike}_{opt_type}_premium_below_minimum"
        return True, "valid"

    def _get_executable_price(self, chain: dict, strike: float, opt_type: str, action: str) -> float:
        opt = chain[strike].get(opt_type, {})
        bid, ask = opt.get("bid", 0), opt.get("ask", 0)
        if bid > 0 and ask > 0:
            return bid if action == "SELL" else ask
        if bid > 0:
            return bid
        if ask > 0:
            return ask
        ltp = opt.get("ltp", 0)
        if ltp > 0:
            self.logger.warning(f"Using LTP for {strike} {opt_type} — no bid/ask available")
            return ltp
        return 0.0

    def _dte_adjusted_short_delta(self, base_delta: float, actual_dte) -> float:
        if actual_dte is None:
            return base_delta
        if actual_dte <= 0:
            return max(0.12, base_delta - 0.10)
        if actual_dte == 1:
            return max(0.15, base_delta - 0.07)
        if actual_dte == 2:
            return max(0.18, base_delta - 0.05)
        if actual_dte == 3:
            return max(0.20, base_delta - 0.03)
        return base_delta

    def _build_legs_spec(self, strategy_name: str, chain: dict, spot: float,
                           wing: float, actual_dte: Optional[int] = None) -> tuple[Optional[list], Optional[str]]:
        step = self.config.nifty_strike_step
        if not chain:
            return None, "empty_chain"

        if strategy_name == "IRON_BUTTERFLY":
            atm = round(spot / step) * step
            if atm not in chain:
                atm = min(chain.keys(), key=lambda k: abs(k - spot))
            butterfly_wing = max(wing, 50)
            long_call, long_put = atm + butterfly_wing, atm - butterfly_wing
            if long_call not in chain:
                long_call = min(chain.keys(), key=lambda k: abs(k - (atm + butterfly_wing)))
            if long_put not in chain:
                long_put = min(chain.keys(), key=lambda k: abs(k - (atm - butterfly_wing)))
            return [
                {"strike": atm, "option_type": "call", "action": "SELL"},
                {"strike": atm, "option_type": "put", "action": "SELL"},
                {"strike": long_call, "option_type": "call", "action": "BUY"},
                {"strike": long_put, "option_type": "put", "action": "BUY"},
            ], None

        if strategy_name == "IRON_CONDOR":
            target_delta = self._dte_adjusted_short_delta(0.25, actual_dte)
            short_call, _ = self._find_strike_by_delta(chain, "call", target_delta)
            short_put, _ = self._find_strike_by_delta(chain, "put", target_delta)
            if short_call is None or short_put is None:
                return None, "cannot_find_0.25_delta_strikes_for_condor"
            long_call, long_put = short_call + wing, short_put - wing
            if long_call not in chain:
                long_call = min(chain.keys(), key=lambda k: abs(k - (short_call + wing)))
            if long_put not in chain:
                long_put = min(chain.keys(), key=lambda k: abs(k - (short_put - wing)))
            if long_call <= short_call:
                return None, "condor_long_call_not_further_otm"
            if long_put >= short_put:
                return None, "condor_long_put_not_further_otm"
            return [
                {"strike": short_call, "option_type": "call", "action": "SELL"},
                {"strike": short_put, "option_type": "put", "action": "SELL"},
                {"strike": long_call, "option_type": "call", "action": "BUY"},
                {"strike": long_put, "option_type": "put", "action": "BUY"},
            ], None

        if strategy_name == "BULL_PUT_SPREAD":
            target_delta = self._dte_adjusted_short_delta(0.25, actual_dte)
            short_put, _ = self._find_strike_by_delta(chain, "put", target_delta)
            if short_put is None:
                return None, "cannot_find_0.25_delta_put"
            long_put = short_put - wing
            if long_put not in chain:
                long_put = min(chain.keys(), key=lambda k: abs(k - (short_put - wing)))
            if long_put >= short_put:
                return None, "bull_put_spread_long_not_below_short"
            if (short_put - long_put) < 50:
                return None, "bull_put_spread_wing_too_narrow"
            return [
                {"strike": short_put, "option_type": "put", "action": "SELL"},
                {"strike": long_put, "option_type": "put", "action": "BUY"},
            ], None

        if strategy_name == "BEAR_CALL_SPREAD":
            target_delta = self._dte_adjusted_short_delta(0.25, actual_dte)
            short_call, _ = self._find_strike_by_delta(chain, "call", target_delta)
            if short_call is None:
                return None, "cannot_find_0.25_delta_call"
            long_call = short_call + wing
            if long_call not in chain:
                long_call = min(chain.keys(), key=lambda k: abs(k - (short_call + wing)))
            if long_call <= short_call:
                return None, "bear_call_spread_long_not_above_short"
            if (long_call - short_call) < 50:
                return None, "bear_call_spread_wing_too_narrow"
            return [
                {"strike": short_call, "option_type": "call", "action": "SELL"},
                {"strike": long_call, "option_type": "call", "action": "BUY"},
            ], None

        if strategy_name == "BULL_CALL_SPREAD":
            long_call, _ = self._find_strike_by_delta(chain, "call", 0.40)
            short_call, _ = self._find_strike_by_delta(chain, "call", 0.20)
            if long_call is None or short_call is None:
                return None, "cannot_find_strikes_for_bull_call_spread"
            if long_call >= short_call:
                return None, "bull_call_spread_long_not_below_short"
            return [
                {"strike": long_call, "option_type": "call", "action": "BUY"},
                {"strike": short_call, "option_type": "call", "action": "SELL"},
            ], None

        if strategy_name == "BEAR_PUT_SPREAD":
            long_put, _ = self._find_strike_by_delta(chain, "put", 0.40)
            short_put, _ = self._find_strike_by_delta(chain, "put", 0.20)
            if long_put is None or short_put is None:
                return None, "cannot_find_strikes_for_bear_put_spread"
            if long_put <= short_put:
                return None, "bear_put_spread_long_not_above_short"
            return [
                {"strike": long_put, "option_type": "put", "action": "BUY"},
                {"strike": short_put, "option_type": "put", "action": "SELL"},
            ], None

        if strategy_name == "LONG_STRADDLE":
            atm = round(spot / step) * step
            if atm not in chain:
                atm = min(chain.keys(), key=lambda k: abs(k - spot))
            return [
                {"strike": atm, "option_type": "call", "action": "BUY"},
                {"strike": atm, "option_type": "put", "action": "BUY"},
            ], None

        if strategy_name == "POST_EVENT_STRADDLE":
            atm = round(spot / step) * step
            if atm not in chain:
                atm = min(chain.keys(), key=lambda k: abs(k - spot))
            event_wing = max(wing, 150)
            long_call = atm + event_wing
            long_put = atm - event_wing
            if long_call not in chain:
                long_call = min(chain.keys(), key=lambda k: abs(k - (atm + event_wing)))
            if long_put not in chain:
                long_put = min(chain.keys(), key=lambda k: abs(k - (atm - event_wing)))
            return [
                {"strike": atm, "option_type": "call", "action": "SELL"},
                {"strike": atm, "option_type": "put", "action": "SELL"},
                {"strike": long_call, "option_type": "call", "action": "BUY"},
                {"strike": long_put, "option_type": "put", "action": "BUY"},
            ], None

        return None, f"unknown_strategy_{strategy_name}"

    def _build_tightening_schedule(self, day_label: Optional[str]) -> list:
        if day_label == "TUESDAY":
            return [("11:00", 0.80), ("12:00", 0.65), ("13:00", 0.50)]
        return [("13:00", 0.80), ("14:00", 0.65)]
        tightening_profit_gated = True

    # ─────────────────────────────────────────────────────────────────
    # MODULE 8: FULL PARAMETER COMPUTATION
    # ─────────────────────────────────────────────────────────────────
    def compute_strategy_params(self, strategy_name: str, selection_reason: str,
                                  s: dict, size_mult: float) -> dict:
        state = self.market_engine.state

        expiry_str, actual_dte = s.get("active_expiry"), s.get("actual_dte")
        if expiry_str is None or actual_dte is None:
            return {"valid": False, "reason": "no_active_expiry_available"}

        dte_min, dte_max = DTE_REQUIREMENTS.get(strategy_name, (0, 10))
        if actual_dte < dte_min:
            return {"valid": False, "reason": f"dte={actual_dte}_below_minimum_{dte_min}_for_{strategy_name}"}
        if actual_dte > dte_max:
            return {"valid": False, "reason": f"dte={actual_dte}_above_maximum_{dte_max}_for_{strategy_name}"}

        chain = self.market_engine.last_chain
        chain_expiry = self.market_engine.last_chain_expiry
        if not chain or chain_expiry is None or chain_expiry.isoformat() != expiry_str:
            return {"valid": False, "reason": "chain_not_available_or_expiry_mismatch"}
        if len(chain) < 10:
            return {"valid": False, "reason": f"chain_has_only_{len(chain)}_strikes_insufficient"}

        spot = s["spot"]
        wing = state.get("wing_width", 150)

        legs_spec, err = self._build_legs_spec(strategy_name, chain, spot, wing, actual_dte)
        if legs_spec is None:
            return {"valid": False, "reason": err}

        strategy_type = "BUY" if strategy_name in ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "LONG_STRADDLE") else "SELL"

        validated_legs = []
        for leg_spec in legs_spec:
            strike, opt_type, action = leg_spec["strike"], leg_spec["option_type"], leg_spec["action"]
            ok, reason = self._validate_strike(chain, strike, opt_type, action)
            if not ok:
                return {"valid": False, "reason": f"leg_validation_failed: {reason}"}
            exec_price = self._get_executable_price(chain, strike, opt_type, action)
            if exec_price <= 0:
                return {"valid": False, "reason": f"leg_{strike}_{opt_type}_no_executable_price"}
            opt = chain[strike][opt_type]
            validated_legs.append({
                "strike": strike, "option_type": opt_type, "action": action, "exec_price": exec_price,
                "bid": opt.get("bid", 0), "ask": opt.get("ask", 0), "ltp": opt.get("ltp", 0),
                "delta": opt.get("delta", 0), "gamma": opt.get("gamma", 0), "vega": opt.get("vega", 0),
                "theta": opt.get("theta", 0), "iv": opt.get("iv", 0), "oi": opt.get("oi", 0),
            })

        gross_value = sum(l["exec_price"] if l["action"] == "SELL" else -l["exec_price"] for l in validated_legs)
        num_legs = len(validated_legs)
        gross_credit, gross_debit = None, None
        if strategy_type == "SELL":
            gross_credit = gross_value
            if gross_credit <= 0:
                return {"valid": False, "reason": f"gross_credit_{gross_credit:.2f}_non_positive"}
        else:
            gross_debit = abs(gross_value)
            if gross_debit <= 0:
                return {"valid": False, "reason": "gross_debit_zero_not_viable"}

        total_slippage = 0.0
        for leg in validated_legs:
            bid, ask = leg["bid"], leg["ask"]
            total_slippage += min((ask - bid) / 2.0, 2.0) if (bid > 0 and ask > 0) else 1.5

        sell_premium_pts = sum(l["exec_price"] for l in validated_legs if l["action"] == "SELL")
        buy_premium_pts = sum(l["exec_price"] for l in validated_legs if l["action"] == "BUY")
        total_turnover_pts = sell_premium_pts + buy_premium_pts

        C02 = self.config.lot_size
        stt_per_lot = sell_premium_pts * C02 * self.config.stt_options_sell
        exchange_per_lot = total_turnover_pts * C02 * self.config.exchange_txn_rate
        sebi_per_lot = total_turnover_pts * C02 * self.config.sebi_rate
        brokerage_fixed_cost = self.config.brokerage_per_order * num_legs
        gst_total = (brokerage_fixed_cost + exchange_per_lot + sebi_per_lot) * 0.18
        stamp_per_lot = buy_premium_pts * C02 * self.config.stamp_duty_buy_options
        per_lot_variable_costs = stt_per_lot + exchange_per_lot + sebi_per_lot + stamp_per_lot
        total_costs_rupees_per_lot = per_lot_variable_costs + (brokerage_fixed_cost + gst_total)
        total_costs_pts_per_lot = total_costs_rupees_per_lot / C02

        net_credit, net_debit = None, None
        if strategy_type == "SELL":
            net_credit = gross_credit - total_slippage - total_costs_pts_per_lot
            if net_credit <= 0:
                return {"valid": False, "reason": f"net_credit_{net_credit:.2f}pts_non_positive_after_costs"}
        else:
            net_debit = gross_debit + total_slippage + total_costs_pts_per_lot

        actual_wing_pts = None  # initialized here so it's always defined below regardless of strategy
        stop_premium = None

        if strategy_type == "SELL":
            day_label = state.get("day_label")
            min_credits = dict(MIN_CREDITS)
            if day_label == "TUESDAY":
                min_credits.update(MIN_CREDITS_TUESDAY)
            static_floor = min_credits.get(strategy_name, 12) * self._get_min_credit_multiplier(s)
            cost_floor = total_costs_pts_per_lot * 3.0
            min_credit = max(static_floor, cost_floor)

            if net_credit < min_credit:
                return {"valid": False, "reason": f"net_credit_{net_credit:.2f}pts_below_minimum_{min_credit:.2f}pts"}

            _dte_for_ratio = s.get("actual_dte", 5)
            _min_ratio = 0.20 if _dte_for_ratio == 0 else (0.12 if _dte_for_ratio <= 1 else 0.10)
            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY", "POST_EVENT_STRADDLE"):
                actual_wing_pts = abs(validated_legs[2]["strike"] - validated_legs[0]["strike"])
                if actual_wing_pts > 0 and (net_credit / actual_wing_pts) < _min_ratio:
                    return {"valid": False, "reason": f"credit_ratio_below_{_min_ratio}_insufficient_edge"}
            elif strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
                actual_wing_pts = abs(validated_legs[0]["strike"] - validated_legs[1]["strike"])
                # FIX vs original spec: apply the same 10% ratio discipline to
                # 2-leg spreads that was previously only applied to condors.
                if actual_wing_pts > 0 and (net_credit / actual_wing_pts) < 0.10:
                    return {"valid": False, "reason": f"credit_ratio_below_0.10_insufficient_edge"}

            target_pct = self._get_target_pct(s)
            target_premium_at_target = net_credit * (1.0 - target_pct)
            exit_costs_pts = total_costs_pts_per_lot + total_slippage
            net_profit_at_target = net_credit - target_premium_at_target - exit_costs_pts
            if net_profit_at_target <= 0:
                return {"valid": False, "reason": f"net_profit_at_target_{net_profit_at_target:.2f}pts_non_positive"}
            _min_rupee_profit = 500.0
            _projected_rupee_profit = net_profit_at_target * C02
            if _projected_rupee_profit < _min_rupee_profit:
                return {"valid": False, "reason": f"projected_profit_Rs{_projected_rupee_profit:.0f}_below_minimum_Rs{_min_rupee_profit:.0f}"}

        current_capital = state.get("current_capital", self.config.starting_capital)
        risk_pct = self.config.max_risk_per_trade_pct
        if s.get("actual_dte") == 0:
            risk_pct = min(risk_pct, 0.003)
        max_risk_per_trade = current_capital * risk_pct
        stop_multiplier = state.get("stop_multiplier", 2.0)
        _size_mult_check = size_mult

        if strategy_type == "SELL":
            stop_premium = net_credit * stop_multiplier
            stop_based_loss = (stop_premium - net_credit) * C02 * 1.25
            if actual_wing_pts is not None and actual_wing_pts > 0 and net_credit > 0:
                contractual_max_loss = (actual_wing_pts - net_credit) * C02
                max_loss_per_lot = min(stop_based_loss, contractual_max_loss) if contractual_max_loss > 0 else stop_based_loss
            else:
                max_loss_per_lot = stop_based_loss
        else:
            max_loss_per_lot = net_debit * 0.50 * C02 * 1.25

        if max_loss_per_lot <= 0:
            max_loss_per_lot = wing * C02 * 0.5
            self.logger.warning("max_loss_per_lot was <= 0, using fallback")

        raw_base_lots = max_risk_per_trade / max_loss_per_lot
        base_lots = max(1, int(raw_base_lots))
        intended_lots = raw_base_lots * size_mult
        if intended_lots < 0.5:
            return {"valid": False, "reason": f"intended_lots_{intended_lots:.2f}_below_minimum_viable_0.5_size_throttled_to_no_trade"}
        if base_lots == 1 and _size_mult_check < 0.4:
            return {"valid": False, "reason": f"base_lots_1_size_mult_{_size_mult_check:.2f}_below_0.4_insufficient_for_required_reduction"}
        final_lots = max(1, int(base_lots * size_mult))
        capital_scale = max(1, int(current_capital / self.config.starting_capital))
        day_cap = LOT_CAPS_BY_DAY.get(state.get("day_label"), 3) * capital_scale
        final_lots = min(final_lots, day_cap)

        if max_loss_per_lot * final_lots > max_risk_per_trade * 1.5:
            final_lots = max(1, int(max_risk_per_trade / max_loss_per_lot))

        if strategy_type == "SELL":
            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "POST_EVENT_STRADDLE"):
                estimated_margin_per_lot = (actual_wing_pts or wing) * C02 * 1.15
            else:
                estimated_margin_per_lot = spot * C02 * 0.11
        else:
            estimated_margin_per_lot = net_debit * C02

        total_estimated_margin = estimated_margin_per_lot * final_lots
        margin_available = current_capital * 0.80
        if total_estimated_margin > margin_available and final_lots > 1:
            lots_by_margin = max(1, int(margin_available / estimated_margin_per_lot))
            final_lots = min(final_lots, lots_by_margin)
            total_estimated_margin = estimated_margin_per_lot * final_lots

        if strategy_type == "SELL" and net_credit and net_credit > 0:
            _credit_stop_multiplier = 2.5 if actual_dte == 0 else 3.0
            _credit_based_stop = net_credit * _credit_stop_multiplier
            _static_stop = PRICE_STOPS.get(strategy_name, 80)
            if actual_dte == 0: _static_stop = int(_static_stop * 0.60)
            elif actual_dte == 1: _static_stop = int(_static_stop * 0.75)
            elif actual_dte <= 3: _static_stop = int(_static_stop * 0.90)
            price_stop_pts = int(min(_static_stop, max(_credit_based_stop * 2.0, 30)))
        else:
            price_stop_pts = PRICE_STOPS.get(strategy_name, 80)
            if actual_dte == 0: price_stop_pts = 35
            elif actual_dte == 1: price_stop_pts = int(price_stop_pts * 0.75)
            elif actual_dte <= 3: price_stop_pts = int(price_stop_pts * 0.90)
            price_stop_pts_0dte_tightened = True

        hard_exit_str = state.get("hard_exit_time", self.config.hard_exit_time.strftime("%H:%M"))
        target_pct_final = self._get_target_pct(s)

        return {
            "valid": True,
            "strategy_name": strategy_name, "strategy_type": strategy_type,
            "selection_reason": selection_reason,
            "target_expiry": expiry_str, "actual_dte": actual_dte,
            "legs": validated_legs, "num_legs": num_legs,
            "gross_credit": gross_credit, "gross_debit": gross_debit,
            "total_slippage": total_slippage, "total_costs_pts": total_costs_pts_per_lot,
            "total_costs_rupees_per_lot": total_costs_rupees_per_lot,
            "entry_credit": net_credit, "entry_debit": net_debit,
            "stop_premium": stop_premium if strategy_type == "SELL" else None,
            "target_premium": (net_credit * (1.0 - target_pct_final)) if strategy_type == "SELL" else None,
            "stop_value": (net_debit * 0.50) if strategy_type == "BUY" else None,
            "target_value": (net_debit * 1.50) if strategy_type == "BUY" else None,
            "price_stop_pts": price_stop_pts,
            "tightening_schedule": self._build_tightening_schedule(state.get("day_label")),
            "final_lots": final_lots, "max_loss_per_lot": max_loss_per_lot,
            "total_max_risk": max_loss_per_lot * final_lots,
            "estimated_margin": total_estimated_margin,
            "hard_exit_time": hard_exit_str, "target_pct": target_pct_final,
            "entry_spot": spot, "entry_vix": s["vix"], "entry_vrp": s["vrp"],
            "entry_time": now_ist().isoformat(),
            "wing_width": actual_wing_pts if strategy_type == "SELL" else None,
            "stop_at_breakeven": False, "stop_moved_to_25pct": False,
            "last_known_premium": net_credit if strategy_type == "SELL" else net_debit,
        }

    # ─────────────────────────────────────────────────────────────────
    # CONSOLE + PERSISTENCE
    # ─────────────────────────────────────────────────────────────────
    def _log_decision(self, s: dict, action: str, reason: str, params: Optional[dict] = None) -> None:
        print_section(f"STRATEGY DECISION @ {now_ist().strftime('%H:%M:%S')}")
        if action == "NO_TRADE":
            print(f"  ACTION: NO_TRADE")
            print(f"  REASON: {reason}")
            self.logger.info(f"NO_TRADE: {reason}")
        else:
            print(f"  ACTION: {action}")
            print(f"  STRATEGY: {params['strategy_name'] if params else ''}")
            print(f"  REASON: {reason}")
            if params:
                print_kv_table({
                    "Legs": len(params["legs"]),
                    "Net Credit/Debit (pts)": params.get("entry_credit") or params.get("entry_debit"),
                    "Stop (pts)": params.get("stop_premium") or params.get("stop_value"),
                    "Target (pts)": params.get("target_premium") or params.get("target_value"),
                    "Price Stop (pts)": params["price_stop_pts"],
                    "Final Lots": params["final_lots"],
                    "Max Risk (Rs)": params["total_max_risk"],
                    "Estimated Margin (Rs)": params["estimated_margin"],
                    "DTE": params["actual_dte"],
                }, title="TRADE PARAMETERS")
                print("\n  LEGS:")
                for leg in params["legs"]:
                    print(f"    {leg['action']:<4} {leg['option_type'].upper():<4} {leg['strike']:.0f} "
                          f"@ {leg['exec_price']:.2f}  (delta={leg['delta']:.3f}, iv={leg['iv']*100:.1f}%)")
            self.logger.info(f"STRATEGY DECISION: {action} "
                              f"{params['strategy_name'] if params else ''} — {reason}")
        print()

    def _persist_strategy_decision(self, s: dict, strategy_name: str, reason: str,
                                     params: Optional[dict], action: str) -> None:
        self.db.insert("strategy_decisions", {
            "decision_time": now_ist().isoformat(), "trading_date": s["trading_date"],
            "action": action, "strategy_name": strategy_name, "reason": reason,
            "params_json": json.dumps(params, default=str) if params else None,
            "signals_json": json.dumps(
                {k: v for k, v in s.items() if k not in ("atm_greeks", "conditions_met", "conditions_not_met")},
                default=str,
            ),
        })

    # ─────────────────────────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ─────────────────────────────────────────────────────────────────
    def decide(self, signals: dict) -> dict:
        self._update_event_detection(signals)

        gate_result = self._check_hard_gates(signals)
        if gate_result:
            action, reason = gate_result
            self._log_decision(signals, action, reason)
            self._persist_strategy_decision(signals, "NONE", reason, None, action)
            self.market_engine.finalize_cycle_log(action, reason, self._count_open_positions())
            return {"action": action, "reason": reason}

        strategy_name, reason = self._select_strategy(signals)

        if strategy_name == "NO_TRADE":
            self._log_decision(signals, "NO_TRADE", reason)
            self._persist_strategy_decision(signals, "NO_TRADE", reason, None, "NO_TRADE")
            self.market_engine.finalize_cycle_log("NO_TRADE", reason, self._count_open_positions())
            return {"action": "NO_TRADE", "reason": reason}

        strategy_name, reason, size_mult = self._post_selection_validation(strategy_name, reason, signals)

        rules_ok, rules_reason = self._validate_strategy_entry_rules(strategy_name, signals)
        if not rules_ok:
            full_reason = f"strategy_rules_failed: {rules_reason}"
            self._log_decision(signals, "NO_TRADE", full_reason)
            self._persist_strategy_decision(signals, strategy_name, full_reason, None, "NO_TRADE")
            self.market_engine.finalize_cycle_log("NO_TRADE", full_reason, self._count_open_positions())
            return {"action": "NO_TRADE", "reason": full_reason}

        params = self.compute_strategy_params(strategy_name, reason, signals, size_mult)

        if not params.get("valid"):
            full_reason = f"params_invalid: {params.get('reason')}"
            self._log_decision(signals, "NO_TRADE", full_reason)
            self._persist_strategy_decision(signals, strategy_name, full_reason, None, "NO_TRADE")
            self.market_engine.finalize_cycle_log("NO_TRADE", full_reason, self._count_open_positions())
            return {"action": "NO_TRADE", "reason": full_reason}

        self._log_decision(signals, "STRATEGY_SELECTED", reason, params)
        self._persist_strategy_decision(signals, strategy_name, reason, params, "STRATEGY_SELECTED")
        self.market_engine.finalize_cycle_log(f"STRATEGY_SELECTED:{strategy_name}", None,
                                                self._count_open_positions())

        return {"action": "ENTER", "strategy_name": strategy_name, "reason": reason, "params": params}


# ─────────────────────────────────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────────────────────────────────
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
        logger.warning("No valid Upstox token — engines constructed but cannot run a live decision test.")
        db.close()
        return

    signals = market_engine.run_cycle()
    decision = strategy_engine.decide(signals)
    print_section(f"DECISION RESULT: {decision['action']}")
    print(decision.get("reason", ""))
    db.close()


if __name__ == "__main__":
    _self_test()