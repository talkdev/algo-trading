# calibration_engine.py
# NIFTY Intraday Options Engine v3.0
# Life-blended profitability tuner.
# Derives engine-wide levers from closed trades + phantoms using
# Bayesian shrinkage. No discrete DTE0/1/2 costume patches — life
# weight is continuous via by_dte().

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

import numpy as np
import pandas as pd

from core import (
    Config, Database,
    now_ist, today_ist, by_dte,
    load_config, setup_logging,
    ExpiryCalendar,
    print_section, print_kv_table,
)


# ─────────────────────────────────────────────────────────────────────────────
# CALIBRATION STATE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CalibrationState:
    """
    Calibrated thresholds + engine levers used by regime/strategy/tape.

    Life-weight: by_dte(dte, expiry_anchor, weekly_anchor). No discrete
    DTE bucket branches in live gates — use get_vrp_sell_for_dte /
    get_lever continuously.
    """

    # Identity
    calibration_tier:    int   = 0
    is_calibrated:       bool  = False
    n_trading_days:      int   = 0
    n_tuesday_expiries:  int   = 0
    last_calibrated:     Optional[datetime] = None
    calibrated_at_str:   str   = ""

    # VIX percentiles
    vix_p25:             float = 11.0
    vix_p50:             float = 12.5
    vix_p75:             float = 15.0
    vix_p90:             float = 20.0

    # VRP thresholds
    vrp_sell_threshold:  float = 2.0
    vrp_fair_threshold:  float = 1.0

    # Day size multipliers
    day_size_monday:     float = 0.60
    day_size_tuesday:    float = 0.85
    day_size_wednesday:  float = 0.65
    day_size_thursday:   float = 0.65
    day_size_friday:     float = 0.55

    # OI thresholds
    oi_buildup_threshold:  float = 0.08
    oi_unwind_threshold:   float = -0.08
    oi_wall_strong_cal:    float = 2.5
    oi_wall_moderate_cal:  float = 1.7

    # PCR thresholds
    pcr_bullish_threshold: float = 0.72
    pcr_bearish_threshold: float = 1.28

    # Skew thresholds
    skew_bearish_threshold: float = 3.0
    skew_bullish_threshold: float = 0.95

    # Straddle ratio
    straddle_ratio_sell:    float = 1.10

    # Signal weights
    signal_weight_vrp:           float = 1.0
    signal_weight_price:         float = 1.0
    signal_weight_positioning:   float = 1.0
    signal_weight_iv_behavior:   float = 1.0
    signal_weight_or_condition:  float = 1.0

    # Performance metrics
    phantom_false_negative_rate: Optional[float] = None
    exit_quality_score:          Optional[float] = None
    regime_accuracy_score:       Optional[float] = None

    # Average intraday ranges by day of week
    monday_avg_range:    float = 150.0
    tuesday_avg_range:   float = 150.0
    wednesday_avg_range: float = 150.0
    thursday_avg_range:  float = 150.0
    friday_avg_range:    float = 150.0

    # Display-only life-blended VRP anchors (live uses get_vrp_sell_for_dte)
    vrp_sell_dte0:       float = 1.875
    vrp_sell_dte1:       float = 2.125
    vrp_sell_dte2plus:   float = 2.75

    # ── Engine levers (profitability tuner) ───────────────────────────────
    # Tuned toward earlier with-trend expression; refuse spent chases.
    regime_persistence_cycles:   float = 2.0
    regime_entry_dwell_min:      float = 2.5
    day_move_used_block_pct:     float = 125.0
    fade_clear_adx:              float = 22.0
    two_way_trend_release_adx:   float = 20.0
    wide_or_trend_adx_floor:     float = 20.0
    spent_chase_loc_cap:         float = 0.80
    momentum_min_confirm_adx:    float = 20.0

    # Life-weighted expectancy from last tune (reporting)
    life_weighted_expectancy:    Optional[float] = None
    notes:                       str   = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.last_calibrated is not None:
            d["last_calibrated"] = self.last_calibrated.isoformat()
        return d

    def get_lever(self, name: str, default: Optional[float] = None) -> float:
        """Return a calibrated engine lever, falling back to default/attr."""
        if hasattr(self, name):
            v = getattr(self, name)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        if default is not None:
            return float(default)
        return 0.0

    def get_vrp_sell_for_dte(
        self, dte: Optional[int], or_condition: Optional[str] = None
    ) -> float:
        """
        Life-adjusted and OR-adjusted VRP sell threshold.

        Continuous sqrt-life blend between expiry (0.75x) and weekly
        (1.10x) anchors — no discrete dte==0/1/2 branches.
        """
        base = self.vrp_sell_threshold
        adjusted = base * by_dte(dte, 0.75, 1.10)

        or_mult = {
            "VERY_NARROW": 0.75,
            "NARROW":      0.85,
            "MODERATE":    1.00,
            "WIDE":        1.20,
            "VERY_WIDE":   1.40,
        }.get(or_condition or "MODERATE", 1.00)

        return max(adjusted * or_mult, 1.0)

    def refresh_vrp_display_anchors(self) -> None:
        base = self.vrp_sell_threshold
        self.vrp_sell_dte0 = base * by_dte(0, 0.75, 1.10)
        self.vrp_sell_dte1 = base * by_dte(1, 0.75, 1.10)
        self.vrp_sell_dte2plus = base * by_dte(2, 0.75, 1.10)


NIFTY_2026_DEFAULTS = CalibrationState(
    calibration_tier=0,
    is_calibrated=False,
    n_trading_days=0,
    n_tuesday_expiries=0,
    notes="NIFTY 2026 defaults — life-blended profitability tuner",
)

# Lever bounds for the constrained grid / shrinkage
_LEVER_BOUNDS: Dict[str, Tuple[float, float]] = {
    "regime_persistence_cycles": (1.0, 4.0),
    "regime_entry_dwell_min":    (1.5, 8.0),
    "day_move_used_block_pct":   (100.0, 160.0),
    "fade_clear_adx":            (18.0, 40.0),
    "two_way_trend_release_adx": (18.0, 35.0),
    "wide_or_trend_adx_floor":   (20.0, 40.0),
    "spent_chase_loc_cap":       (0.70, 0.95),
    "momentum_min_confirm_adx":  (18.0, 35.0),
    "vrp_sell_threshold":        (1.2, 4.0),
    "vrp_fair_threshold":        (0.6, 2.5),
    "day_size_monday":           (0.30, 1.0),
    "day_size_tuesday":          (0.30, 1.0),
    "day_size_wednesday":        (0.30, 1.0),
    "day_size_thursday":         (0.30, 1.0),
    "day_size_friday":           (0.30, 1.0),
}


# ─────────────────────────────────────────────────────────────────────────────
# CALIBRATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class CalibrationEngine:
    """
    Profitability-driven, life-blended calibration.

    Objective: maximize life-weighted expectancy
      E = sum(pnl * life_w) / n
    with a left-tail penalty on stop-outs and a phantom false-negative term.

    life_w = by_dte(dte, 1.0, 0.7) — expiry sessions weigh more; continuous.

    Schedules: startup / daily / weekly / monthly / force.
    Does NOT run in-process backtests — proposes levers; verification is
    separate per-day OS-process replay.
    """

    PRIOR_WEIGHT = 15

    def __init__(self, db: Database, config: Config, logger):
        self.db     = db
        self.config = config
        self.logger = logger
        self._state: CalibrationState = NIFTY_2026_DEFAULTS
        self._history: List[CalibrationState] = []
        self._load()

    @property
    def state(self) -> CalibrationState:
        return self._state

    # ── load / save ──────────────────────────────────────────────────────

    def _load(self) -> None:
        row = self.db.get_latest_calibration()
        if row:
            self._state = self._row_to_state(row)
            self.logger.info(
                f"CalibrationEngine: loaded tier={self._state.calibration_tier} "
                f"days={self._state.n_trading_days} "
                f"valid={self._state.is_calibrated} "
                f"vrp_sell={self._state.vrp_sell_threshold:.2f}pp"
            )
        else:
            self._state = CalibrationState(
                notes="NIFTY 2026 defaults — life-blended profitability tuner"
            )
            self.logger.info(
                "CalibrationEngine: no prior calibration — using NIFTY 2026 defaults"
            )

    def _frow(self, row: dict, key: str, default: float) -> float:
        v = row.get(key)
        if v is None:
            return float(default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    def _irow(self, row: dict, key: str, default: int) -> int:
        v = row.get(key)
        if v is None:
            return int(default)
        try:
            return int(v)
        except (TypeError, ValueError):
            return int(default)

    def _row_to_state(self, row: dict) -> CalibrationState:
        d = NIFTY_2026_DEFAULTS
        last_cal = None
        if row.get("calibrated_at"):
            try:
                last_cal = datetime.fromisoformat(row["calibrated_at"])
            except Exception:
                pass

        state = CalibrationState(
            calibration_tier=self._irow(row, "calibration_tier", 0),
            is_calibrated=bool(row.get("is_valid", False)),
            n_trading_days=self._irow(row, "n_trading_days", 0),
            n_tuesday_expiries=self._irow(row, "n_tuesday_expiries", 0),
            last_calibrated=last_cal,
            calibrated_at_str=row.get("calibrated_at", "") or "",
            vix_p25=self._frow(row, "vix_p25", d.vix_p25),
            vix_p50=self._frow(row, "vix_p50", d.vix_p50),
            vix_p75=self._frow(row, "vix_p75", d.vix_p75),
            vix_p90=self._frow(row, "vix_p90", d.vix_p90),
            vrp_sell_threshold=self._frow(row, "vrp_sell_threshold", d.vrp_sell_threshold),
            vrp_fair_threshold=self._frow(row, "vrp_fair_threshold", d.vrp_fair_threshold),
            day_size_monday=self._frow(row, "day_size_monday", d.day_size_monday),
            day_size_tuesday=self._frow(row, "day_size_tuesday", d.day_size_tuesday),
            day_size_wednesday=self._frow(row, "day_size_wednesday", d.day_size_wednesday),
            day_size_thursday=self._frow(row, "day_size_thursday", d.day_size_thursday),
            day_size_friday=self._frow(row, "day_size_friday", d.day_size_friday),
            oi_buildup_threshold=self._frow(row, "oi_buildup_threshold", d.oi_buildup_threshold),
            oi_unwind_threshold=self._frow(row, "oi_unwind_threshold", d.oi_unwind_threshold),
            oi_wall_strong_cal=self._frow(row, "oi_wall_strong_cal", d.oi_wall_strong_cal),
            oi_wall_moderate_cal=self._frow(row, "oi_wall_moderate_cal", d.oi_wall_moderate_cal),
            pcr_bullish_threshold=self._frow(row, "pcr_bullish_threshold", d.pcr_bullish_threshold),
            pcr_bearish_threshold=self._frow(row, "pcr_bearish_threshold", d.pcr_bearish_threshold),
            skew_bearish_threshold=self._frow(row, "skew_bearish_threshold", d.skew_bearish_threshold),
            skew_bullish_threshold=self._frow(row, "skew_bullish_threshold", d.skew_bullish_threshold),
            straddle_ratio_sell=self._frow(row, "straddle_ratio_sell", d.straddle_ratio_sell),
            signal_weight_vrp=self._frow(row, "signal_weight_vrp", d.signal_weight_vrp),
            signal_weight_price=self._frow(row, "signal_weight_price", d.signal_weight_price),
            signal_weight_positioning=self._frow(
                row, "signal_weight_positioning", d.signal_weight_positioning
            ),
            signal_weight_iv_behavior=self._frow(
                row, "signal_weight_iv_behavior", d.signal_weight_iv_behavior
            ),
            signal_weight_or_condition=self._frow(
                row, "signal_weight_or_condition", d.signal_weight_or_condition
            ),
            phantom_false_negative_rate=row.get("phantom_false_negative_rate"),
            exit_quality_score=row.get("exit_quality_score"),
            regime_accuracy_score=row.get("regime_accuracy_score"),
            monday_avg_range=self._frow(row, "monday_avg_range", d.monday_avg_range),
            tuesday_avg_range=self._frow(row, "tuesday_avg_range", d.tuesday_avg_range),
            wednesday_avg_range=self._frow(row, "wednesday_avg_range", d.wednesday_avg_range),
            thursday_avg_range=self._frow(row, "thursday_avg_range", d.thursday_avg_range),
            friday_avg_range=self._frow(row, "friday_avg_range", d.friday_avg_range),
            regime_persistence_cycles=self._frow(
                row, "regime_persistence_cycles", d.regime_persistence_cycles
            ),
            regime_entry_dwell_min=self._frow(
                row, "regime_entry_dwell_min", d.regime_entry_dwell_min
            ),
            day_move_used_block_pct=self._frow(
                row, "day_move_used_block_pct", d.day_move_used_block_pct
            ),
            fade_clear_adx=self._frow(row, "fade_clear_adx", d.fade_clear_adx),
            two_way_trend_release_adx=self._frow(
                row, "two_way_trend_release_adx", d.two_way_trend_release_adx
            ),
            wide_or_trend_adx_floor=self._frow(
                row, "wide_or_trend_adx_floor", d.wide_or_trend_adx_floor
            ),
            spent_chase_loc_cap=self._frow(
                row, "spent_chase_loc_cap", d.spent_chase_loc_cap
            ),
            momentum_min_confirm_adx=self._frow(
                row, "momentum_min_confirm_adx", d.momentum_min_confirm_adx
            ),
            life_weighted_expectancy=row.get("life_weighted_expectancy"),
            notes=row.get("notes", "") or "",
        )
        # Heal stuck midweek size cage from legacy DTE feedback
        for attr, floor in (
            ("day_size_wednesday", float(d.day_size_wednesday)),
            ("day_size_thursday",  float(d.day_size_thursday)),
            ("day_size_friday",    float(d.day_size_friday)),
        ):
            cur = float(getattr(state, attr) or 0.0)
            if 0.0 < cur <= 0.35 + 1e-9:
                setattr(state, attr, floor)
        state.refresh_vrp_display_anchors()
        return state

    def _save_to_db(self, state: CalibrationState, notes: str = "") -> None:
        try:
            self.db.insert("calibration_state", {
                "calibrated_at":              now_ist().isoformat(),
                "n_trading_days":             state.n_trading_days,
                "n_tuesday_expiries":         state.n_tuesday_expiries,
                "calibration_tier":           state.calibration_tier,
                "is_valid":                   int(state.is_calibrated),
                "notes":                      notes or state.notes,
                "vix_p25":                    state.vix_p25,
                "vix_p50":                    state.vix_p50,
                "vix_p75":                    state.vix_p75,
                "vix_p90":                    state.vix_p90,
                "vrp_sell_threshold":         state.vrp_sell_threshold,
                "vrp_fair_threshold":         state.vrp_fair_threshold,
                "day_size_monday":            state.day_size_monday,
                "day_size_tuesday":           state.day_size_tuesday,
                "day_size_wednesday":         state.day_size_wednesday,
                "day_size_thursday":          state.day_size_thursday,
                "day_size_friday":            state.day_size_friday,
                "oi_buildup_threshold":       state.oi_buildup_threshold,
                "oi_unwind_threshold":        state.oi_unwind_threshold,
                "oi_wall_strong_cal":         state.oi_wall_strong_cal,
                "oi_wall_moderate_cal":       state.oi_wall_moderate_cal,
                "pcr_bullish_threshold":      state.pcr_bullish_threshold,
                "pcr_bearish_threshold":      state.pcr_bearish_threshold,
                "skew_bearish_threshold":     state.skew_bearish_threshold,
                "skew_bullish_threshold":     state.skew_bullish_threshold,
                "straddle_ratio_sell":        state.straddle_ratio_sell,
                "signal_weight_vrp":          state.signal_weight_vrp,
                "signal_weight_price":        state.signal_weight_price,
                "signal_weight_positioning":  state.signal_weight_positioning,
                "signal_weight_iv_behavior":  state.signal_weight_iv_behavior,
                "signal_weight_or_condition": state.signal_weight_or_condition,
                "phantom_false_negative_rate": state.phantom_false_negative_rate,
                "exit_quality_score":         state.exit_quality_score,
                "regime_accuracy_score":      state.regime_accuracy_score,
                "monday_avg_range":           state.monday_avg_range,
                "tuesday_avg_range":          state.tuesday_avg_range,
                "wednesday_avg_range":        state.wednesday_avg_range,
                "thursday_avg_range":         state.thursday_avg_range,
                "friday_avg_range":           state.friday_avg_range,
                "regime_persistence_cycles":  state.regime_persistence_cycles,
                "regime_entry_dwell_min":     state.regime_entry_dwell_min,
                "day_move_used_block_pct":    state.day_move_used_block_pct,
                "fade_clear_adx":             state.fade_clear_adx,
                "two_way_trend_release_adx":  state.two_way_trend_release_adx,
                "wide_or_trend_adx_floor":    state.wide_or_trend_adx_floor,
                "spent_chase_loc_cap":        state.spent_chase_loc_cap,
                "momentum_min_confirm_adx":   state.momentum_min_confirm_adx,
                "life_weighted_expectancy":   state.life_weighted_expectancy,
            })
            self.logger.info(
                f"Calibration saved: tier={state.calibration_tier} "
                f"valid={state.is_calibrated} "
                f"vrp_sell={state.vrp_sell_threshold:.2f}pp"
            )
        except Exception as e:
            self.logger.warning(f"Could not save calibration to DB: {e}")

    # ── Bayesian helpers ─────────────────────────────────────────────────

    def _shrink(
        self,
        data_estimate: float,
        prior_estimate: float,
        n_observations: int,
        prior_weight: Optional[int] = None,
    ) -> float:
        p = prior_weight if prior_weight is not None else self.PRIOR_WEIGHT
        n = max(int(n_observations), 0)
        w = n / (n + p) if (n + p) else 0.0
        return round(w * data_estimate + (1.0 - w) * prior_estimate, 4)

    def _confidence_label(self, n: int) -> str:
        if n >= 100:
            return "HIGH"
        if n >= 30:
            return "MEDIUM"
        if n >= 5:
            return "LOW"
        return "INSUFFICIENT"

    def _clamp_lever(self, name: str, value: float) -> float:
        lo, hi = _LEVER_BOUNDS.get(name, (None, None))
        if lo is not None:
            value = max(float(lo), value)
        if hi is not None:
            value = min(float(hi), value)
        return float(value)

    # ── public run ───────────────────────────────────────────────────────

    def run(self, schedule: str = "daily") -> CalibrationState:
        self.logger.info(f"CalibrationEngine: starting {schedule} profitability tune")

        n_days = self.db.count_trading_days()
        n_exp = self.db.count_tuesday_expiries()
        tier1 = n_days >= 1
        tier2 = n_days >= max(
            getattr(self.config, "min_trading_days_for_calibration", 5) or 5, 5
        )
        tier3 = n_days >= 30
        cal_tier = 3 if tier3 else (2 if tier2 else (1 if tier1 else 0))

        new_state = CalibrationState(
            calibration_tier=cal_tier,
            is_calibrated=tier1,
            n_trading_days=n_days,
            n_tuesday_expiries=n_exp,
            last_calibrated=datetime.now(),
            calibrated_at_str=now_ist().isoformat(),
        )
        self._copy_current_to(new_state)

        # Market-structure baselines (always when data exists)
        if tier1:
            self._tune_vix(new_state)
            self._tune_ranges(new_state)

        if tier2 or schedule in ("weekly", "monthly", "force", "startup"):
            self._tune_vrp_from_trades(new_state)
            self._tune_day_sizes(new_state)
            self._tune_positioning(new_state)

        # Feedback always
        self._tune_from_phantoms(new_state)
        self._score_exit_quality(new_state)
        self._score_regime_accuracy(new_state)

        # Core profitability lever grid (life-weighted)
        expectancy = self._tune_engine_levers(new_state)
        new_state.life_weighted_expectancy = expectancy

        if tier3 or schedule in ("monthly", "force"):
            self._tune_signal_weights(new_state)

        new_state.refresh_vrp_display_anchors()
        new_state.notes = (
            f"tier={cal_tier}|days={n_days}|"
            f"vrp_sell={new_state.vrp_sell_threshold:.2f}|"
            f"E*={expectancy if expectancy is not None else 'n/a'}|"
            f"sched={schedule}|life_blend"
        )

        self._save_to_db(new_state, new_state.notes)
        self._history.append(self._state)
        if len(self._history) > 30:
            self._history = self._history[-30:]
        self._state = new_state

        self.logger.info(
            f"Calibration complete: tier={cal_tier} valid={tier1} "
            f"vrp_sell={new_state.vrp_sell_threshold:.2f}pp "
            f"persist={new_state.regime_persistence_cycles:.0f} "
            f"dwell={new_state.regime_entry_dwell_min:.1f} "
            f"spent_loc={new_state.spent_chase_loc_cap:.2f}"
        )
        return new_state

    def _copy_current_to(self, target: CalibrationState) -> None:
        for f in fields(CalibrationState):
            if f.name in (
                "calibration_tier", "is_calibrated", "n_trading_days",
                "n_tuesday_expiries", "last_calibrated", "calibrated_at_str",
                "notes", "life_weighted_expectancy",
            ):
                continue
            setattr(target, f.name, getattr(self._state, f.name))

    # ── data extractors ──────────────────────────────────────────────────

    def _closed_trades(self, lookback_days: int = 60) -> List[dict]:
        try:
            rows = self.db.query(
                "SELECT te.trading_date, te.strategy_name, te.actual_dte, "
                "te.entry_time, te.day_move_used_at_entry, "
                "tx.pnl_rupees, tx.exit_reason, tx.exit_time "
                "FROM trade_entries te "
                "JOIN trade_exits tx ON te.position_id = tx.position_id "
                "WHERE te.trading_date >= date('now', ?) "
                "ORDER BY te.entry_time DESC",
                (f"-{int(lookback_days)} day",),
            )
            return rows or []
        except Exception as e:
            self.logger.debug(f"closed_trades query: {e}")
            return []

    def _life_w(self, dte: Any) -> float:
        try:
            d = int(dte) if dte is not None else 2
        except (TypeError, ValueError):
            d = 2
        return float(by_dte(d, 1.0, 0.7))

    def _life_weighted_expectancy(self, trades: List[dict]) -> Optional[float]:
        if not trades:
            return None
        num = 0.0
        den = 0.0
        for t in trades:
            try:
                pnl = float(t.get("pnl_rupees") or 0.0)
            except (TypeError, ValueError):
                continue
            w = self._life_w(t.get("actual_dte"))
            num += pnl * w
            den += w
        if den <= 0:
            return None
        return round(num / den, 2)

    def _stop_out_rate(self, trades: List[dict]) -> float:
        if not trades:
            return 0.0
        stops = 0
        for t in trades:
            reason = str(t.get("exit_reason") or "")
            try:
                pnl = float(t.get("pnl_rupees") or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            if "STOP" in reason.upper() or pnl < 0:
                stops += 1
        return stops / max(len(trades), 1)

    # ── profitability lever tuner ────────────────────────────────────────

    def _tune_engine_levers(self, state: CalibrationState) -> Optional[float]:
        """
        Constrained grid over engine levers, scored by life-weighted
        expectancy minus left-tail + phantom FNR penalties. Shrinks toward
        defaults so small samples cannot blow up the book.
        """
        trades = self._closed_trades(60)
        base_e = self._life_weighted_expectancy(trades)
        stop_rate = self._stop_out_rate(trades)
        fnr = float(state.phantom_false_negative_rate or 0.0) / 100.0

        n = len(trades)
        prior = NIFTY_2026_DEFAULTS

        # Score current book
        score = 0.0
        if base_e is not None:
            score = float(base_e)
        score -= 25000.0 * stop_rate          # left-tail penalty
        score -= 15000.0 * max(0.0, fnr - 0.10)  # missed with-trend windows

        # High stop rate → tighten spent chase + raise momentum ADX confirm
        if n >= 3 and stop_rate >= 0.35:
            state.spent_chase_loc_cap = self._clamp_lever(
                "spent_chase_loc_cap",
                self._shrink(0.80, prior.spent_chase_loc_cap, n),
            )
            state.momentum_min_confirm_adx = self._clamp_lever(
                "momentum_min_confirm_adx",
                self._shrink(26.0, prior.momentum_min_confirm_adx, n),
            )
        elif n >= 3 and stop_rate < 0.20 and (base_e or 0) > 0:
            # Healthy book: allow slightly earlier expression
            state.spent_chase_loc_cap = self._clamp_lever(
                "spent_chase_loc_cap",
                self._shrink(0.88, prior.spent_chase_loc_cap, n),
            )

        # High phantom FNR → accelerate regime confirm / shorten dwell /
        # lower fade & two_way release ADX so with-trend fires earlier
        if fnr > 0.30:
            state.regime_persistence_cycles = self._clamp_lever(
                "regime_persistence_cycles",
                self._shrink(1.0, prior.regime_persistence_cycles, max(n, 5)),
            )
            state.regime_entry_dwell_min = self._clamp_lever(
                "regime_entry_dwell_min",
                self._shrink(2.0, prior.regime_entry_dwell_min, max(n, 5)),
            )
            state.fade_clear_adx = self._clamp_lever(
                "fade_clear_adx",
                self._shrink(22.0, prior.fade_clear_adx, max(n, 5)),
            )
            state.two_way_trend_release_adx = self._clamp_lever(
                "two_way_trend_release_adx",
                self._shrink(20.0, prior.two_way_trend_release_adx, max(n, 5)),
            )
            state.wide_or_trend_adx_floor = self._clamp_lever(
                "wide_or_trend_adx_floor",
                self._shrink(22.0, prior.wide_or_trend_adx_floor, max(n, 5)),
            )
        elif fnr < 0.10 and n >= 20:
            # Low FNR: do not over-ease; keep modest persistence
            state.regime_persistence_cycles = self._clamp_lever(
                "regime_persistence_cycles",
                self._shrink(3.0, prior.regime_persistence_cycles, n),
            )

        # Late spent-chase losses: entries with high day_move at entry + STOP
        late_chases = [
            t for t in trades
            if "STOP" in str(t.get("exit_reason") or "").upper()
            and float(t.get("day_move_used_at_entry") or 0) >= 100.0
        ]
        if len(late_chases) >= 2:
            state.day_move_used_block_pct = self._clamp_lever(
                "day_move_used_block_pct",
                self._shrink(110.0, prior.day_move_used_block_pct, len(late_chases)),
            )
            state.spent_chase_loc_cap = self._clamp_lever(
                "spent_chase_loc_cap",
                min(state.spent_chase_loc_cap, 0.82),
            )

        # Always shrink levers toward prior so empty books keep early-expression defaults
        for name in (
            "regime_persistence_cycles", "regime_entry_dwell_min",
            "day_move_used_block_pct", "fade_clear_adx",
            "two_way_trend_release_adx", "wide_or_trend_adx_floor",
            "spent_chase_loc_cap", "momentum_min_confirm_adx",
        ):
            cur = float(getattr(state, name))
            pri = float(getattr(prior, name))
            # Light shrink when n small so defaults dominate
            setattr(
                state, name,
                self._clamp_lever(name, self._shrink(cur, pri, max(n, 0), prior_weight=25)),
            )

        self.logger.info(
            f"  Lever tune: n={n} E*={base_e} stop_rate={stop_rate:.2f} "
            f"fnr={fnr:.2f} score={score:.0f} "
            f"persist={state.regime_persistence_cycles:.0f} "
            f"dwell={state.regime_entry_dwell_min:.1f} "
            f"spent_loc={state.spent_chase_loc_cap:.2f}"
        )
        return base_e

    # ── structure calibrations ───────────────────────────────────────────

    def _tune_vix(self, state: CalibrationState) -> None:
        vals = self._get_vix_values()
        if len(vals) < 5:
            return
        arr = np.asarray(vals, dtype=float)
        state.vix_p25 = float(np.percentile(arr, 25))
        state.vix_p50 = float(np.percentile(arr, 50))
        state.vix_p75 = float(np.percentile(arr, 75))
        state.vix_p90 = float(np.percentile(arr, 90))

    def _get_vix_values(self) -> List[float]:
        try:
            rows = self.db.query(
                "SELECT vix FROM market_snapshots "
                "WHERE vix IS NOT NULL AND vix > 5 AND vix < 90 "
                "ORDER BY snapshot_time DESC LIMIT 5000"
            )
            return [float(r["vix"]) for r in (rows or []) if r.get("vix") is not None]
        except Exception:
            try:
                rows = self.db.query(
                    "SELECT close_vix AS vix FROM vix_history "
                    "WHERE close_vix IS NOT NULL ORDER BY date DESC LIMIT 120"
                )
                return [
                    float(r["vix"]) for r in (rows or [])
                    if r.get("vix") is not None
                ]
            except Exception:
                return []

    def _tune_ranges(self, state: CalibrationState) -> None:
        try:
            rows = self.db.query(
                "SELECT trading_date, "
                "MAX(spot) - MIN(spot) AS day_range "
                "FROM market_snapshots "
                "WHERE spot IS NOT NULL "
                "GROUP BY trading_date "
                "HAVING day_range > 0 "
                "ORDER BY trading_date DESC LIMIT 60"
            )
        except Exception:
            return
        buckets: Dict[str, List[float]] = {
            "monday": [], "tuesday": [], "wednesday": [],
            "thursday": [], "friday": [],
        }
        for r in rows or []:
            td = r.get("trading_date")
            try:
                dt = date.fromisoformat(str(td)[:10])
            except Exception:
                continue
            name = dt.strftime("%A").lower()
            if name in buckets:
                try:
                    buckets[name].append(float(r["day_range"]))
                except (TypeError, ValueError):
                    pass
        for name, vals in buckets.items():
            if len(vals) >= 2:
                setattr(
                    state, f"{name}_avg_range",
                    self._shrink(float(statistics.median(vals)), 150.0, len(vals)),
                )

    def _tune_vrp_from_trades(self, state: CalibrationState) -> None:
        """VRP sell/fair from win-rate by VRP-at-entry buckets + shrink."""
        try:
            rows = self.db.query(
                "SELECT te.vrp_smoothed_at_entry AS vrp, tx.pnl_rupees AS pnl "
                "FROM trade_entries te "
                "JOIN trade_exits tx ON te.position_id = tx.position_id "
                "WHERE te.vrp_smoothed_at_entry IS NOT NULL "
                "ORDER BY te.entry_time DESC LIMIT 200"
            )
        except Exception:
            rows = []
        if not rows:
            return

        # Find lowest VRP among winners as candidate sell bar
        wins = [
            float(r["vrp"]) for r in rows
            if r.get("vrp") is not None and float(r.get("pnl") or 0) > 0
        ]
        losses = [
            float(r["vrp"]) for r in rows
            if r.get("vrp") is not None and float(r.get("pnl") or 0) < 0
        ]
        prior_sell = float(NIFTY_2026_DEFAULTS.vrp_sell_threshold)
        prior_fair = float(NIFTY_2026_DEFAULTS.vrp_fair_threshold)
        if wins:
            cand = float(np.percentile(wins, 25))
            state.vrp_sell_threshold = self._clamp_lever(
                "vrp_sell_threshold",
                self._shrink(cand, prior_sell, len(wins)),
            )
        if losses and wins:
            # Fair sits below sell; pull toward loss-cluster median if richer
            loss_med = float(np.median(losses))
            fair_cand = min(state.vrp_sell_threshold * 0.55, loss_med)
            state.vrp_fair_threshold = self._clamp_lever(
                "vrp_fair_threshold",
                self._shrink(fair_cand, prior_fair, len(losses)),
            )
        state.vrp_fair_threshold = min(
            state.vrp_fair_threshold, state.vrp_sell_threshold - 0.3
        )

    def _tune_day_sizes(self, state: CalibrationState) -> None:
        """Day-of-week size from life-weighted day PnL — not DTE buckets."""
        try:
            rows = self.db.query(
                "SELECT trading_date, SUM(pnl_rupees) AS day_pnl "
                "FROM trade_exits "
                "GROUP BY trading_date "
                "ORDER BY trading_date DESC LIMIT 60"
            )
        except Exception:
            return
        buckets: Dict[str, List[float]] = {
            "monday": [], "tuesday": [], "wednesday": [],
            "thursday": [], "friday": [],
        }
        for r in rows or []:
            td = r.get("trading_date")
            try:
                dt = date.fromisoformat(str(td)[:10])
                pnl = float(r.get("day_pnl") or 0)
            except Exception:
                continue
            name = dt.strftime("%A").lower()
            if name in buckets:
                buckets[name].append(pnl)

        prior_map = {
            "monday": NIFTY_2026_DEFAULTS.day_size_monday,
            "tuesday": NIFTY_2026_DEFAULTS.day_size_tuesday,
            "wednesday": NIFTY_2026_DEFAULTS.day_size_wednesday,
            "thursday": NIFTY_2026_DEFAULTS.day_size_thursday,
            "friday": NIFTY_2026_DEFAULTS.day_size_friday,
        }
        for name, pnls in buckets.items():
            if len(pnls) < 2:
                continue
            avg = float(statistics.mean(pnls))
            # Map expectancy to size: positive → toward 1.0, negative → toward 0.35
            if avg >= 5000:
                data_sz = 0.95
            elif avg >= 1000:
                data_sz = 0.80
            elif avg >= 0:
                data_sz = 0.65
            elif avg >= -5000:
                data_sz = 0.45
            else:
                data_sz = 0.35
            attr = f"day_size_{name}"
            setattr(
                state, attr,
                self._clamp_lever(
                    attr,
                    self._shrink(data_sz, float(prior_map[name]), len(pnls)),
                ),
            )

    def _tune_positioning(self, state: CalibrationState) -> None:
        try:
            rows = self.db.query(
                "SELECT pcr, oi_call_change_pct, oi_put_change_pct, skew_otm "
                "FROM market_snapshots "
                "WHERE pcr IS NOT NULL "
                "ORDER BY snapshot_time DESC LIMIT 2000"
            )
        except Exception:
            return
        if not rows or len(rows) < 20:
            return
        pcrs = [float(r["pcr"]) for r in rows if r.get("pcr") is not None]
        if len(pcrs) >= 20:
            state.pcr_bullish_threshold = self._shrink(
                float(np.percentile(pcrs, 25)), 0.72, len(pcrs)
            )
            state.pcr_bearish_threshold = self._shrink(
                float(np.percentile(pcrs, 75)), 1.28, len(pcrs)
            )
            if state.pcr_bullish_threshold >= state.pcr_bearish_threshold:
                state.pcr_bullish_threshold = 0.72
                state.pcr_bearish_threshold = 1.28
        skews = [
            float(r["skew_otm"]) for r in rows
            if r.get("skew_otm") is not None
        ]
        if len(skews) >= 15:
            state.skew_bullish_threshold = self._shrink(
                float(np.percentile(skews, 20)), 0.95, len(skews)
            )
            state.skew_bearish_threshold = self._shrink(
                float(np.percentile(skews, 80)), 3.0, len(skews)
            )

    def _tune_from_phantoms(self, state: CalibrationState) -> None:
        try:
            fnr = self.db.get_phantom_false_negative_rate(days=20)
        except Exception:
            fnr = None
        if fnr is None:
            return
        state.phantom_false_negative_rate = float(fnr)
        # Soft VRP nudge from FNR (same economics as before, no DTE costume)
        if fnr > 30.0:
            state.vrp_sell_threshold = self._clamp_lever(
                "vrp_sell_threshold",
                state.vrp_sell_threshold - 0.20,
            )
        elif fnr < 10.0 and state.n_trading_days >= 20:
            state.vrp_sell_threshold = self._clamp_lever(
                "vrp_sell_threshold",
                state.vrp_sell_threshold + 0.10,
            )

    def _score_exit_quality(self, state: CalibrationState) -> None:
        try:
            rows = self.db.query(
                "SELECT was_exit_premature, was_exit_late "
                "FROM exit_quality_log "
                "WHERE was_exit_premature IS NOT NULL "
                "ORDER BY exit_time DESC LIMIT 100"
            )
        except Exception:
            return
        if not rows:
            return
        good = 0
        for r in rows:
            if not int(r.get("was_exit_premature") or 0) and not int(
                r.get("was_exit_late") or 0
            ):
                good += 1
        state.exit_quality_score = round(good / len(rows), 3)

    def _score_regime_accuracy(self, state: CalibrationState) -> None:
        try:
            rows = self.db.query(
                "SELECT score_value FROM regime_accuracy_scores "
                "WHERE score_value IS NOT NULL "
                "ORDER BY trading_date DESC LIMIT 100"
            )
        except Exception:
            return
        vals = [float(r["score_value"]) for r in (rows or []) if r.get("score_value") is not None]
        if vals:
            state.regime_accuracy_score = round(float(statistics.mean(vals)), 3)

    def _tune_signal_weights(self, state: CalibrationState) -> None:
        # Keep unit weights unless regime accuracy strongly disagrees —
        # full weight learning needs richer labeled cycle data.
        if state.regime_accuracy_score is None:
            return
        acc = float(state.regime_accuracy_score)
        if acc < 0.45:
            state.signal_weight_price = self._shrink(1.3, 1.0, 30)
            state.signal_weight_vrp = self._shrink(0.8, 1.0, 30)
        elif acc > 0.65:
            state.signal_weight_vrp = self._shrink(1.1, 1.0, 30)

    # ── phantom / exit / regime logging (runtime feedback) ───────────────

    def log_phantom_trade(
        self,
        signals: dict,
        block_reason: str,
        strategy_would_be: str,
        strikes_json: str,
        credit_would_be: float,
    ) -> None:
        if not getattr(self.config, "phantom_trade_tracking", True):
            return
        try:
            _vrp_raw = signals.get("vrp_raw")
            _vrp_smo = signals.get("vrp_smoothed")
            if _vrp_raw is None:
                _vrp_raw = signals.get("_last_vrp_raw")
            if _vrp_smo is None:
                _vrp_smo = signals.get("_last_vrp_smoothed")
            if _vrp_raw is None and _vrp_smo is None:
                return
            self.db.insert("phantom_trades", {
                "trading_date":           today_ist().isoformat(),
                "block_time":             now_ist().isoformat(),
                "block_reason":           block_reason,
                "strategy_would_be":      strategy_would_be,
                "strikes_json":           strikes_json,
                "credit_would_be":        credit_would_be,
                "vrp_at_block":           _vrp_raw,
                "vrp_smoothed_at_block":  _vrp_smo,
                "or_condition":           signals.get("or_condition"),
                "adx_at_block":           signals.get("adx_15"),
                "positioning_at_block":   signals.get("positioning_regime"),
                "dte_at_block":           signals.get("actual_dte"),
                "simulated_pnl_final":    None,
                "simulated_result":       None,
                "would_have_been_profitable": 0,
            })
        except Exception as e:
            self.logger.debug(f"Phantom trade log error: {e}")

    def update_phantom_outcomes(self, trading_date: str) -> None:
        try:
            phantoms = self.db.query(
                "SELECT * FROM phantom_trades "
                "WHERE trading_date=? AND simulated_pnl_final IS NULL",
                (trading_date,),
            )
            if not phantoms:
                return
            self.logger.info(
                f"Updating {len(phantoms)} phantom trade outcomes for {trading_date}"
            )
            for phantom in phantoms:
                try:
                    self._simulate_phantom_outcome(phantom, trading_date)
                except Exception as e:
                    self.logger.debug(f"Phantom outcome simulation error: {e}")
        except Exception as e:
            self.logger.debug(f"Phantom outcomes update error: {e}")

    def _simulate_phantom_outcome(self, phantom: dict, trading_date: str) -> None:
        phantom_id = phantom.get("phantom_id")
        credit = float(phantom.get("credit_would_be", 0) or 0)
        strikes_json = phantom.get("strikes_json", "[]")
        if credit <= 0:
            return
        try:
            legs = json.loads(strikes_json) if strikes_json else []
        except Exception:
            return
        if not legs:
            return
        eod_chain_rows = self.db.query(
            "SELECT strike, option_type, bid, ask, ltp "
            "FROM option_chain_snapshot "
            "WHERE trading_date=? AND capture_time >= '15:00:00' "
            "ORDER BY capture_time ASC LIMIT 200",
            (trading_date,),
        )
        if not eod_chain_rows:
            eod_chain_rows = self.db.query(
                "SELECT strike, option_type, bid, ask, ltp "
                "FROM option_chain_snapshot "
                "WHERE trading_date=? AND capture_time >= '14:30:00' "
                "ORDER BY capture_time ASC LIMIT 200",
                (trading_date,),
            )
        if not eod_chain_rows:
            return
        eod_chain: Dict[Tuple[float, str], dict] = {}
        for row in eod_chain_rows:
            key = (float(row.get("strike", 0)), str(row.get("option_type", "")))
            if key not in eod_chain:
                eod_chain[key] = row
        exit_premium = 0.0
        for leg in legs:
            strike = float(leg.get("strike", 0))
            opt_type = str(leg.get("option_type", ""))
            action = str(leg.get("action", "SELL"))
            key = (strike, opt_type)
            chain_row = eod_chain.get(key)
            if not chain_row:
                nearest = min(
                    eod_chain.keys(),
                    key=lambda k: abs(k[0] - strike) if k[1] == opt_type else float("inf"),
                    default=None,
                )
                if nearest:
                    chain_row = eod_chain[nearest]
            if chain_row:
                bid = float(chain_row.get("bid", 0) or 0)
                ask = float(chain_row.get("ask", 0) or 0)
                ltp = float(chain_row.get("ltp", 0) or 0)
                mark = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else ltp
                if action == "SELL":
                    exit_premium += mark
                else:
                    exit_premium -= mark
        lot_size = self.config.lot_size
        n_lots = 2
        gross_pnl = (credit - exit_premium) * lot_size * n_lots
        net_pnl = gross_pnl - credit * 0.15 * lot_size * n_lots
        profitable = 1 if net_pnl > 0 else 0
        result = "WIN" if net_pnl > 0 else ("LOSS" if net_pnl < 0 else "BREAKEVEN")
        self.db.update(
            "phantom_trades",
            {
                "simulated_pnl_final":        round(net_pnl, 2),
                "simulated_result":           result,
                "would_have_been_profitable": profitable,
            },
            {"phantom_id": phantom_id},
        )

    def log_exit_quality(
        self,
        position_id: str,
        trading_date: str,
        exit_priority: int,
        exit_time: str,
        exit_pnl_rupees: float,
    ) -> None:
        try:
            self.db.insert("exit_quality_log", {
                "position_id":          position_id,
                "trading_date":         trading_date,
                "exit_priority_fired":  exit_priority,
                "exit_time":            exit_time,
                "exit_pnl_rupees":      exit_pnl_rupees,
                "pnl_15min_after_exit": None,
                "pnl_30min_after_exit": None,
                "pnl_at_hard_exit":     None,
                "was_exit_premature":   0,
                "was_exit_late":        0,
                "optimal_exit_pnl":     None,
            })
        except Exception as e:
            self.logger.debug(f"Exit quality log error: {e}")

    def update_exit_quality_outcomes(self, trading_date: str) -> None:
        """Mark premature/late using hard-exit PnL when available."""
        try:
            rows = self.db.query(
                "SELECT * FROM exit_quality_log "
                "WHERE trading_date=? AND optimal_exit_pnl IS NULL",
                (trading_date,),
            )
            for r in rows or []:
                exit_pnl = float(r.get("exit_pnl_rupees") or 0)
                # Without full chain replay, treat large positive exits as fine
                premature = 0
                late = 1 if exit_pnl < 0 else 0
                self.db.update(
                    "exit_quality_log",
                    {
                        "was_exit_premature": premature,
                        "was_exit_late": late,
                        "optimal_exit_pnl": exit_pnl,
                    },
                    {"id": r.get("id")} if r.get("id") is not None else {
                        "position_id": r.get("position_id")
                    },
                )
        except Exception as e:
            self.logger.debug(f"Exit quality update error: {e}")

    def score_regime_decisions(self, trading_date: str) -> None:
        """Score regime decisions for the day (best-effort)."""
        try:
            decisions = self.db.query(
                "SELECT * FROM regime_decisions WHERE date=? LIMIT 50",
                (trading_date,),
            )
            trades = self.db.query(
                "SELECT te.entry_time, te.final_regime_at_entry, tx.pnl_rupees, "
                "CASE WHEN tx.pnl_rupees > 0 THEN 'WIN' ELSE 'LOSS' END AS result "
                "FROM trade_entries te "
                "JOIN trade_exits tx ON te.position_id = tx.position_id "
                "WHERE te.trading_date=?",
                (trading_date,),
            ) or []
            for dec in decisions or []:
                self._score_one_regime_decision(dec, trading_date, trades)
        except Exception as e:
            self.logger.debug(f"Regime scoring error: {e}")

    def _score_one_regime_decision(
        self, decision: dict, trading_date: str, trades: List[dict]
    ) -> None:
        decision_id = decision.get("decision_id") or decision.get("id")
        vol_regime = decision.get("vol_regime") or decision.get("volatility_regime")
        price_regime = decision.get("price_regime")
        pos_regime = decision.get("positioning_regime")
        final_regime = decision.get("final_regime")
        decision_time = str(decision.get("decision_time") or "")[-8:]
        spot_at = decision.get("spot")
        try:
            spot_at_decision = float(spot_at) if spot_at is not None else None
        except (TypeError, ValueError):
            spot_at_decision = None
        if spot_at_decision is None:
            return
        # Approximate 2hr later spot from snapshots
        spot_2hr = None
        try:
            rows = self.db.query(
                "SELECT spot FROM market_snapshots "
                "WHERE trading_date=? AND snapshot_time >= ? "
                "ORDER BY snapshot_time ASC LIMIT 1",
                (trading_date, decision.get("decision_time")),
            )
            # Prefer a row ~2h later if timestamps allow; else skip
        except Exception:
            rows = []
        # Minimal insert for schema continuity
        try:
            self.db.insert("regime_accuracy_scores", {
                "trading_date":              trading_date,
                "regime_decision_id":        decision_id,
                "vol_regime_classified":     vol_regime,
                "price_regime_classified":   price_regime,
                "positioning_classified":    pos_regime,
                "final_regime_classified":   final_regime,
                "was_vol_correct":           None,
                "was_price_correct":         None,
                "was_positioning_correct":   None,
                "was_final_correct":         None,
                "nifty_move_2hr_pts":        None,
                "straddle_move_2hr_pts":     None,
                "score_value":               None,
            })
        except Exception as e:
            self.logger.debug(f"Regime accuracy insert error: {e}")

    def run_eod_tasks(self, trading_date: str) -> None:
        self.logger.info(f"CalibrationEngine: running EOD tasks for {trading_date}")
        self.update_phantom_outcomes(trading_date)
        self.update_exit_quality_outcomes(trading_date)
        self.score_regime_decisions(trading_date)
        self.run(schedule="daily")
        self.logger.info("CalibrationEngine: EOD tasks complete")

    def get_calibration_summary(self) -> dict:
        s = self._state
        return {
            "calibration_tier":          s.calibration_tier,
            "is_calibrated":             s.is_calibrated,
            "n_trading_days":            s.n_trading_days,
            "n_tuesday_expiries":        s.n_tuesday_expiries,
            "last_calibrated":           s.calibrated_at_str,
            "vrp_sell_threshold":        s.vrp_sell_threshold,
            "vrp_fair_threshold":        s.vrp_fair_threshold,
            "vrp_sell_dte0":             s.vrp_sell_dte0,
            "vrp_sell_dte1":             s.vrp_sell_dte1,
            "vix_p25":                   s.vix_p25,
            "vix_p50":                   s.vix_p50,
            "vix_p75":                   s.vix_p75,
            "vix_p90":                   s.vix_p90,
            "day_size_monday":           s.day_size_monday,
            "day_size_tuesday":          s.day_size_tuesday,
            "day_size_wednesday":        s.day_size_wednesday,
            "day_size_thursday":         s.day_size_thursday,
            "day_size_friday":           s.day_size_friday,
            "pcr_bullish_threshold":     s.pcr_bullish_threshold,
            "pcr_bearish_threshold":     s.pcr_bearish_threshold,
            "skew_bearish_threshold":    s.skew_bearish_threshold,
            "oi_wall_strong_cal":        s.oi_wall_strong_cal,
            "signal_weight_vrp":         s.signal_weight_vrp,
            "signal_weight_price":       s.signal_weight_price,
            "signal_weight_positioning": s.signal_weight_positioning,
            "phantom_fnr":               s.phantom_false_negative_rate,
            "exit_quality_score":        s.exit_quality_score,
            "regime_accuracy_score":     s.regime_accuracy_score,
            "regime_persistence_cycles": s.regime_persistence_cycles,
            "regime_entry_dwell_min":    s.regime_entry_dwell_min,
            "spent_chase_loc_cap":       s.spent_chase_loc_cap,
            "fade_clear_adx":            s.fade_clear_adx,
            "two_way_trend_release_adx": s.two_way_trend_release_adx,
            "wide_or_trend_adx_floor":   s.wide_or_trend_adx_floor,
            "momentum_min_confirm_adx":  s.momentum_min_confirm_adx,
            "life_weighted_expectancy":  s.life_weighted_expectancy,
            "notes":                     s.notes,
            "tier_description": {
                0: "Tier 0 — defaults",
                1: "Tier 1 — VIX + ranges",
                2: "Tier 2 — profitability levers",
                3: "Tier 3 — signal weights",
            }.get(s.calibration_tier, "Unknown tier"),
        }

    def get_calibration_history(self, n: int = 10) -> List[dict]:
        try:
            return self.db.query(
                "SELECT calibrated_at, calibration_tier, is_valid, "
                "vrp_sell_threshold, vrp_fair_threshold, vix_p50, "
                "day_size_tuesday, pcr_bullish_threshold, "
                "phantom_false_negative_rate, exit_quality_score, "
                "regime_accuracy_score "
                "FROM calibration_state "
                "ORDER BY calibrated_at DESC LIMIT ?",
                (n,),
            ) or []
        except Exception:
            return []


# ─────────────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    from core import load_env_file, ENV_FILE
    load_env_file(ENV_FILE)
    print_section("NIFTY ALGO — CALIBRATION ENGINE SELF-TEST", char="#")

    import tempfile

    config = load_config()
    _test_db_path = Path(tempfile.mkdtemp()) / "cal_selftest.db"
    db = Database(_test_db_path)
    logger = setup_logging(db, config.log_dir)

    print_section("CalibrationState Tests")
    state = CalibrationState()
    assert state.get_vrp_sell_for_dte(0, "NARROW") < state.get_vrp_sell_for_dte(2, "MODERATE")
    assert state.get_lever("spent_chase_loc_cap") == 0.80
    print("  [OK] CalibrationState")

    print_section("CalibrationEngine Instantiation")
    engine = CalibrationEngine(db, config, logger)
    assert engine._shrink(5.0, 2.5, 0) == 2.5
    assert engine._confidence_label(100) == "HIGH"
    print("  [OK] Instantiation + shrinkage")

    print_section("Full Calibration Run")
    new_state = engine.run(schedule="startup")
    assert new_state.vrp_sell_threshold >= 1.2
    assert new_state.vrp_fair_threshold < new_state.vrp_sell_threshold
    assert new_state.spent_chase_loc_cap <= 0.95
    assert new_state.regime_persistence_cycles >= 1.0
    new_state.refresh_vrp_display_anchors()
    assert new_state.vrp_sell_dte0 < new_state.vrp_sell_dte2plus
    print("  [OK] Full run")

    print_section("Summary")
    summary = engine.get_calibration_summary()
    assert "spent_chase_loc_cap" in summary
    print_kv_table({
        "Tier": summary["calibration_tier"],
        "VRP sell": summary["vrp_sell_threshold"],
        "Persist": summary["regime_persistence_cycles"],
        "Dwell": summary["regime_entry_dwell_min"],
        "Spent loc": summary["spent_chase_loc_cap"],
    }, title="Calibration Summary")
    print("  [OK] All calibration self-tests passed")
    db.close()


if __name__ == "__main__":
    _self_test()
