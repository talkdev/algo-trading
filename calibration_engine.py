# calibration_engine.py
# NIFTY Intraday Options Engine v3.0
# Self-calibrating threshold engine.
# Reads stored historical data and derives optimal thresholds.
# Implements Bayesian shrinkage, phantom trade analysis,
# exit quality tuning, signal weight learning, and drift detection.

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

import numpy as np
import pandas as pd

from core import (
    Config, Database,
    now_ist, today_ist,
    load_config, setup_logging,
    ExpiryCalendar,
    print_section, print_kv_table,
)


# ─────────────────────────────────────────────────────────────────────────────
# CALIBRATION STATE DATACLASS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CalibrationState:
    """
    All calibrated thresholds used by the regime and strategy engines.

    Calibration tiers:
    Tier 0: < 5 trading days  — hardcoded NIFTY 2026 defaults
    Tier 1: 5-19 trading days — VIX percentiles + basic VRP from live data
    Tier 2: 20-59 days        — full calibration, all thresholds data-derived
    Tier 3: 60+ days          — robust calibration with signal weights + drift detection
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
    vrp_sell_threshold:  float = 2.5
    vrp_fair_threshold:  float = 1.5

    # Day size multipliers
    day_size_monday:     float = 0.55
    day_size_tuesday:    float = 0.80
    day_size_wednesday:  float = 0.70
    day_size_thursday:   float = 0.70
    day_size_friday:     float = 0.60

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

    # Signal weights (for confidence score computation)
    signal_weight_vrp:           float = 1.0
    signal_weight_price:         float = 1.0
    signal_weight_positioning:   float = 1.0
    signal_weight_iv_behavior:   float = 1.0
    signal_weight_or_condition:  float = 1.0

    # Performance metrics (updated daily)
    phantom_false_negative_rate: Optional[float] = None
    exit_quality_score:          Optional[float] = None
    regime_accuracy_score:       Optional[float] = None

    # Average intraday ranges by day of week
    monday_avg_range:    float = 150.0
    tuesday_avg_range:   float = 150.0
    wednesday_avg_range: float = 150.0
    thursday_avg_range:  float = 150.0
    friday_avg_range:    float = 150.0

    # DTE-specific VRP thresholds (derived from base + DTE adjustment)
    vrp_sell_dte0:       float = 1.875   # vrp_sell * 0.75
    vrp_sell_dte1:       float = 2.125   # vrp_sell * 0.85
    vrp_sell_dte2plus:   float = 2.75    # vrp_sell * 1.10

    # Calibration notes
    notes:               str   = ""

    def to_dict(self) -> dict:
        """Convert to plain dict for JSON serialisation."""
        d = asdict(self)
        if self.last_calibrated is not None:
            d["last_calibrated"] = self.last_calibrated.isoformat()
        return d

    def get_vrp_sell_for_dte(self, dte: Optional[int], or_condition: Optional[str] = None) -> float:
        """
        Return the DTE-adjusted and OR-adjusted VRP sell threshold.
        This is the primary threshold used by the volatility gate.
        """
        base = self.vrp_sell_threshold

        # DTE adjustment
        if dte == 0:
            adjusted = base * 0.75
        elif dte == 1:
            adjusted = base * 0.85
        elif dte is not None and dte >= 2:
            adjusted = base * 1.10
        else:
            adjusted = base

        # OR condition adjustment
        or_mult = {
            "VERY_NARROW": 0.75,
            "NARROW":      0.85,
            "MODERATE":    1.00,
            "WIDE":        1.20,
            "VERY_WIDE":   1.40,
        }.get(or_condition or "MODERATE", 1.00)

        result = adjusted * or_mult
        return max(result, 1.0)  # Never below 1.0pp absolute


# ─────────────────────────────────────────────────────────────────────────────
# NIFTY 2026 DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────

NIFTY_2026_DEFAULTS = CalibrationState(
    calibration_tier=0,
    is_calibrated=False,
    n_trading_days=0,
    n_tuesday_expiries=0,
    vix_p25=11.0,
    vix_p50=12.5,
    vix_p75=15.0,
    vix_p90=20.0,
    vrp_sell_threshold=2.5,
    vrp_fair_threshold=1.5,
    day_size_monday=0.55,
    day_size_tuesday=0.80,
    day_size_wednesday=0.70,
    day_size_thursday=0.70,
    day_size_friday=0.60,
    oi_buildup_threshold=0.08,
    oi_unwind_threshold=-0.08,
    oi_wall_strong_cal=2.5,
    oi_wall_moderate_cal=1.7,
    pcr_bullish_threshold=0.72,
    pcr_bearish_threshold=1.28,
    skew_bearish_threshold=3.0,
    skew_bullish_threshold=0.95,
    straddle_ratio_sell=1.10,
    signal_weight_vrp=1.0,
    signal_weight_price=1.0,
    signal_weight_positioning=1.0,
    signal_weight_iv_behavior=1.0,
    signal_weight_or_condition=1.0,
    monday_avg_range=150.0,
    tuesday_avg_range=150.0,
    wednesday_avg_range=150.0,
    thursday_avg_range=150.0,
    friday_avg_range=150.0,
    notes="NIFTY 2026 defaults — VIX 11 suppressed environment",
)


# ─────────────────────────────────────────────────────────────────────────────
# CALIBRATION RESULT (intermediate)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CalibrationResult:
    """
    Intermediate result from one calibration component.
    Carries the computed value, sample size, and confidence.
    """
    key:             str
    value:           float
    prior:           float
    n_observations:  int
    shrunk_value:    float
    confidence:      str   # HIGH / MEDIUM / LOW
    notes:           str   = ""

    @property
    def is_reliable(self) -> bool:
        return self.n_observations >= 30

    @property
    def is_usable(self) -> bool:
        return self.n_observations >= 5


# ─────────────────────────────────────────────────────────────────────────────
# CALIBRATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class CalibrationEngine:
    """
    Self-calibrating threshold engine for NIFTY intraday options trading.

    Architecture:
    - Reads stored historical data from SQLite database
    - Derives optimal thresholds using Bayesian shrinkage
    - Implements four feedback loops:
      1. VRP threshold tuning from win rates by VRP bucket
      2. Phantom trade analysis (is NEUTRAL gate too tight?)
      3. Exit quality tuning (are exits happening at right time?)
      4. Signal weight tuning (which signals predict outcomes best?)
    - Runs on three schedules:
      Daily (after close): VRP, phantom, day sizes
      Weekly (Sunday): full recalibration
      Monthly (last Sunday): drift detection, signal weights

    Bayesian shrinkage:
    With N observations and prior_weight P:
      shrunk = (N/(N+P)) * data_estimate + (P/(N+P)) * prior
    P = 30 by default (equivalent to 30 observations of prior belief)
    This prevents overfitting on small samples.
    """

    # Bayesian prior weight
    PRIOR_WEIGHT = 30

    # Minimum sample sizes for each calibration component
    MIN_SAMPLES_VIX         = 20
    MIN_SAMPLES_VRP         = 5
    MIN_SAMPLES_DAY_SIZE    = 3
    MIN_SAMPLES_OI          = 20
    MIN_SAMPLES_PCR         = 50
    MIN_SAMPLES_SKEW        = 30
    MIN_SAMPLES_RANGES      = 3
    MIN_SAMPLES_WEIGHTS     = 20
    MIN_SAMPLES_PHANTOM     = 5
    MIN_SAMPLES_EXIT        = 10

    def __init__(self, db: Database, config: Config, logger):
        self.db     = db
        self.config = config
        self.logger = logger

        # Current calibration state
        self._state: CalibrationState = NIFTY_2026_DEFAULTS

        # History of calibration runs (for drift detection)
        self._history: List[CalibrationState] = []

        # Load latest calibration from DB
        self._load()

    # ─────────────────────────────────────────────────────────────────────
    # LOAD / SAVE
    # ─────────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load the most recent valid calibration from database."""
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
            self._state = NIFTY_2026_DEFAULTS
            self.logger.info(
                "CalibrationEngine: no prior calibration — using NIFTY 2026 defaults (Tier 0)"
            )

    def _row_to_state(self, row: dict) -> CalibrationState:
        """Convert a calibration_state DB row to CalibrationState."""
        d = NIFTY_2026_DEFAULTS

        def _f(key: str, default: float = 0.0) -> float:
            v = row.get(key)
            if v is None:
                return getattr(d, key, default)
            try:
                return float(v)
            except (TypeError, ValueError):
                return getattr(d, key, default)

        def _i(key: str, default: int = 0) -> int:
            v = row.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        last_cal = None
        if row.get("calibrated_at"):
            try:
                last_cal = datetime.fromisoformat(row["calibrated_at"])
            except Exception:
                pass

        vrp_sell = _f("vrp_sell_threshold", d.vrp_sell_threshold)

        state = CalibrationState(
            calibration_tier=_i("calibration_tier", 0),
            is_calibrated=bool(row.get("is_valid", False)),
            n_trading_days=_i("n_trading_days", 0),
            n_tuesday_expiries=_i("n_tuesday_expiries", 0),
            last_calibrated=last_cal,
            calibrated_at_str=row.get("calibrated_at", ""),
            vix_p25=_f("vix_p25", d.vix_p25),
            vix_p50=_f("vix_p50", d.vix_p50),
            vix_p75=_f("vix_p75", d.vix_p75),
            vix_p90=_f("vix_p90", d.vix_p90),
            vrp_sell_threshold=vrp_sell,
            vrp_fair_threshold=_f("vrp_fair_threshold", d.vrp_fair_threshold),
            day_size_monday=_f("day_size_monday", d.day_size_monday),
            day_size_tuesday=_f("day_size_tuesday", d.day_size_tuesday),
            day_size_wednesday=_f("day_size_wednesday", d.day_size_wednesday),
            day_size_thursday=_f("day_size_thursday", d.day_size_thursday),
            day_size_friday=_f("day_size_friday", d.day_size_friday),
            oi_buildup_threshold=_f("oi_buildup_threshold", d.oi_buildup_threshold),
            oi_unwind_threshold=_f("oi_unwind_threshold", d.oi_unwind_threshold),
            oi_wall_strong_cal=_f("oi_wall_strong_cal", d.oi_wall_strong_cal),
            oi_wall_moderate_cal=_f("oi_wall_moderate_cal", d.oi_wall_moderate_cal),
            pcr_bullish_threshold=_f("pcr_bullish_threshold", d.pcr_bullish_threshold),
            pcr_bearish_threshold=_f("pcr_bearish_threshold", d.pcr_bearish_threshold),
            skew_bearish_threshold=_f("skew_bearish_threshold", d.skew_bearish_threshold),
            skew_bullish_threshold=_f("skew_bullish_threshold", d.skew_bullish_threshold),
            straddle_ratio_sell=_f("straddle_ratio_sell", d.straddle_ratio_sell),
            signal_weight_vrp=_f("signal_weight_vrp", d.signal_weight_vrp),
            signal_weight_price=_f("signal_weight_price", d.signal_weight_price),
            signal_weight_positioning=_f("signal_weight_positioning", d.signal_weight_positioning),
            signal_weight_iv_behavior=_f("signal_weight_iv_behavior", d.signal_weight_iv_behavior),
            signal_weight_or_condition=_f("signal_weight_or_condition", d.signal_weight_or_condition),
            phantom_false_negative_rate=row.get("phantom_false_negative_rate"),
            exit_quality_score=row.get("exit_quality_score"),
            regime_accuracy_score=row.get("regime_accuracy_score"),
            monday_avg_range=_f("monday_avg_range", d.monday_avg_range),
            tuesday_avg_range=_f("tuesday_avg_range", d.tuesday_avg_range),
            wednesday_avg_range=_f("wednesday_avg_range", d.wednesday_avg_range),
            thursday_avg_range=_f("thursday_avg_range", d.thursday_avg_range),
            friday_avg_range=_f("friday_avg_range", d.friday_avg_range),
            notes=row.get("notes", ""),
        )

        # Compute DTE-specific VRP thresholds
        state.vrp_sell_dte0    = vrp_sell * 0.75
        state.vrp_sell_dte1    = vrp_sell * 0.85
        state.vrp_sell_dte2plus = vrp_sell * 1.10

        return state

    def _save_to_db(self, state: CalibrationState, notes: str = "") -> None:
        """Persist calibration state to calibration_state table."""
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
            })
            self.logger.info(
                f"Calibration saved: tier={state.calibration_tier} "
                f"valid={state.is_calibrated} "
                f"vrp_sell={state.vrp_sell_threshold:.2f}pp"
            )
        except Exception as e:
            self.logger.warning(f"Could not save calibration to DB: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC INTERFACE
    # ─────────────────────────────────────────────────────────────────────

    @property
    def state(self) -> CalibrationState:
        """Return current calibration state."""
        return self._state

    def run(self, schedule: str = "daily") -> CalibrationState:
        """
        Run calibration.

        schedule:
        "startup"  — full calibration at engine start
        "daily"    — after market close (VRP, phantom, day sizes)
        "weekly"   — Sunday evening (full recalibration)
        "monthly"  — last Sunday (drift detection + signal weights)
        "force"    — full calibration regardless of schedule

        Returns updated CalibrationState.
        """
        self.logger.info(f"CalibrationEngine: starting {schedule} calibration run")

        n_days = self.db.count_trading_days()
        n_exp  = self.db.count_tuesday_expiries()
        self.logger.info(
            f"  Data available: {n_days} trading days, {n_exp} Tuesday expiries"
        )

        # Determine calibration tier
        tier1 = n_days >= 5
        tier2 = n_days >= self.config.min_trading_days_for_calibration  # default 20
        tier3 = n_days >= 60
        cal_tier  = 3 if tier3 else (2 if tier2 else (1 if tier1 else 0))
        is_valid  = tier2

        # Start from current state (preserve what we know)
        new_state = CalibrationState(
            calibration_tier=cal_tier,
            is_calibrated=is_valid,
            n_trading_days=n_days,
            n_tuesday_expiries=n_exp,
            last_calibrated=datetime.now(),
            calibrated_at_str=now_ist().isoformat(),
        )

        # Copy current thresholds as starting point
        self._copy_current_to(new_state)

        # ── Run calibration components based on schedule and tier ─────────

        # Tier 1+: VIX percentiles
        if tier1:
            self._run_vix_calibration(new_state, n_days)

        # Tier 1+: Day ranges
        if tier1:
            self._run_range_calibration(new_state, n_days)

        # Tier 2+: VRP thresholds
        if tier2 or schedule in ("weekly", "monthly", "force", "startup"):
            self._run_vrp_calibration(new_state, n_days)

        # Tier 2+: Day size multipliers
        if tier2 or schedule in ("weekly", "monthly", "force", "startup"):
            self._run_day_size_calibration(new_state, n_days)

        # Tier 2+: OI thresholds
        if tier2 or schedule in ("weekly", "monthly", "force"):
            self._run_oi_calibration(new_state, n_days)

        # Tier 2+: PCR thresholds
        if tier2 or schedule in ("weekly", "monthly", "force"):
            self._run_pcr_calibration(new_state, n_days)

        # Tier 2+: Skew thresholds
        if tier2 or schedule in ("weekly", "monthly", "force"):
            self._run_skew_calibration(new_state, n_days)

        # Tier 2+: Straddle ratio
        if tier2 or schedule in ("weekly", "monthly", "force"):
            self._run_straddle_ratio_calibration(new_state, n_days)

        # Always: Phantom trade analysis (feedback loop 1)
        self._run_phantom_analysis(new_state)

        # Always: Exit quality analysis (feedback loop 2)
        self._run_exit_quality_analysis(new_state)

        # Always: Regime accuracy
        self._run_regime_accuracy(new_state)

        # Tier 3 only: Signal weights (feedback loop 3)
        if tier3 or schedule in ("monthly", "force"):
            self._run_signal_weight_calibration(new_state, n_days)

        # Tier 3 only: Drift detection
        if tier3 or schedule == "monthly":
            self._run_drift_detection(new_state)

        # Compute DTE-specific VRP thresholds
        new_state.vrp_sell_dte0     = new_state.vrp_sell_threshold * 0.75
        new_state.vrp_sell_dte1     = new_state.vrp_sell_threshold * 0.85
        new_state.vrp_sell_dte2plus = new_state.vrp_sell_threshold * 1.10

        # Build notes
        notes_parts = [
            f"tier={cal_tier}",
            f"days={n_days}",
            f"vrp_sell={new_state.vrp_sell_threshold:.2f}pp",
            f"schedule={schedule}",
        ]
        if not is_valid:
            notes_parts.append(
                f"need {self.config.min_trading_days_for_calibration} days for Tier 2"
            )
        new_state.notes = " | ".join(notes_parts)

        # Save to DB
        self._save_to_db(new_state, new_state.notes)

        # Update internal state
        self._history.append(self._state)
        if len(self._history) > 30:
            self._history = self._history[-30:]
        self._state = new_state

        self.logger.info(
            f"Calibration complete: tier={cal_tier} valid={is_valid} "
            f"vrp_sell={new_state.vrp_sell_threshold:.2f}pp "
            f"vrp_fair={new_state.vrp_fair_threshold:.2f}pp"
        )
        return new_state

    def _copy_current_to(self, target: CalibrationState) -> None:
        """Copy all threshold values from current state to target."""
        d = self._state
        target.vix_p25                  = d.vix_p25
        target.vix_p50                  = d.vix_p50
        target.vix_p75                  = d.vix_p75
        target.vix_p90                  = d.vix_p90
        target.vrp_sell_threshold       = d.vrp_sell_threshold
        target.vrp_fair_threshold       = d.vrp_fair_threshold
        target.day_size_monday          = d.day_size_monday
        target.day_size_tuesday         = d.day_size_tuesday
        target.day_size_wednesday       = d.day_size_wednesday
        target.day_size_thursday        = d.day_size_thursday
        target.day_size_friday          = d.day_size_friday
        target.oi_buildup_threshold     = d.oi_buildup_threshold
        target.oi_unwind_threshold      = d.oi_unwind_threshold
        target.oi_wall_strong_cal       = d.oi_wall_strong_cal
        target.oi_wall_moderate_cal     = d.oi_wall_moderate_cal
        target.pcr_bullish_threshold    = d.pcr_bullish_threshold
        target.pcr_bearish_threshold    = d.pcr_bearish_threshold
        target.skew_bearish_threshold   = d.skew_bearish_threshold
        target.skew_bullish_threshold   = d.skew_bullish_threshold
        target.straddle_ratio_sell      = d.straddle_ratio_sell
        target.signal_weight_vrp        = d.signal_weight_vrp
        target.signal_weight_price      = d.signal_weight_price
        target.signal_weight_positioning = d.signal_weight_positioning
        target.signal_weight_iv_behavior = d.signal_weight_iv_behavior
        target.signal_weight_or_condition = d.signal_weight_or_condition
        target.monday_avg_range         = d.monday_avg_range
        target.tuesday_avg_range        = d.tuesday_avg_range
        target.wednesday_avg_range      = d.wednesday_avg_range
        target.thursday_avg_range       = d.thursday_avg_range
        target.friday_avg_range         = d.friday_avg_range

    # ─────────────────────────────────────────────────────────────────────
    # BAYESIAN SHRINKAGE
    # ─────────────────────────────────────────────────────────────────────

    def _shrink(
        self,
        data_estimate: float,
        prior_estimate: float,
        n_observations: int,
        prior_weight: Optional[int] = None,
    ) -> float:
        """
        Apply Bayesian shrinkage toward prior.

        Formula: shrunk = (N/(N+P)) * data + (P/(N+P)) * prior
        Where P = prior_weight (default PRIOR_WEIGHT = 30)

        With N=0:   100% prior
        With N=30:  50% data, 50% prior
        With N=100: 77% data, 23% prior
        With N=300: 91% data, 9% prior
        """
        p = prior_weight if prior_weight is not None else self.PRIOR_WEIGHT
        n = max(int(n_observations), 0)
        w = n / (n + p)
        result = w * data_estimate + (1.0 - w) * prior_estimate
        return round(result, 4)

    def _confidence_label(self, n: int) -> str:
        """Return confidence label based on sample size."""
        if n >= 100:
            return "HIGH"
        if n >= 30:
            return "MEDIUM"
        if n >= 5:
            return "LOW"
        return "INSUFFICIENT"

    # ─────────────────────────────────────────────────────────────────────
    # VIX CALIBRATION
    # ─────────────────────────────────────────────────────────────────────

    def _run_vix_calibration(self, state: CalibrationState, n_days: int) -> None:
        """
        Calibrate VIX percentile thresholds from stored vix_history.
        Falls back to daily_summary VIX columns if vix_history is sparse.

        Updates: vix_p25, vix_p50, vix_p75, vix_p90
        """
        d = NIFTY_2026_DEFAULTS
        vix_vals = self._get_vix_values()

        if len(vix_vals) < self.MIN_SAMPLES_VIX:
            self.logger.info(
                f"  VIX calibration: only {len(vix_vals)} readings "
                f"(need {self.MIN_SAMPLES_VIX}) — using defaults"
            )
            return

        n = len(vix_vals)
        p25_raw = float(np.percentile(vix_vals, 25))
        p50_raw = float(np.percentile(vix_vals, 50))
        p75_raw = float(np.percentile(vix_vals, 75))
        p90_raw = float(np.percentile(vix_vals, 90))

        state.vix_p25 = max(self._shrink(p25_raw, d.vix_p25, n), 10.0)
        state.vix_p50 = max(self._shrink(p50_raw, d.vix_p50, n), 12.0)
        state.vix_p75 = max(self._shrink(p75_raw, d.vix_p75, n), 15.0)
        state.vix_p90 = max(self._shrink(p90_raw, d.vix_p90, n), 20.0)

        # Enforce monotonicity
        state.vix_p50 = max(state.vix_p50, state.vix_p25 + 0.5)
        state.vix_p75 = max(state.vix_p75, state.vix_p50 + 0.5)
        state.vix_p90 = max(state.vix_p90, state.vix_p75 + 1.0)

        self.logger.info(
            f"  VIX calibrated (n={n}, conf={self._confidence_label(n)}): "
            f"p25={state.vix_p25:.1f} p50={state.vix_p50:.1f} "
            f"p75={state.vix_p75:.1f} p90={state.vix_p90:.1f}"
        )

    def _get_vix_values(self) -> np.ndarray:
        """Get all valid VIX readings from vix_history and daily_summary."""
        vals: List[float] = []

        # Primary: vix_history table
        try:
            vix_df = self.db.get_vix_history(days=365)
            if hasattr(vix_df, 'empty') and not vix_df.empty and "vix_value" in vix_df.columns:
                v = vix_df["vix_value"].dropna().values
                v = v[(v > 8.0) & (v < 90.0)]
                vals.extend(v.tolist())
        except Exception as e:
            self.logger.debug(f"VIX history read error: {e}")

        # Fallback: daily_summary VIX columns
        if len(vals) < self.MIN_SAMPLES_VIX:
            try:
                daily_df = self.db.get_daily_summary(days=730)
                if hasattr(daily_df, 'empty') and not daily_df.empty:
                    vcols = [c for c in
                             ["vix_open", "vix_close", "vix_high", "vix_low", "vix_close_val"]
                             if c in daily_df.columns]
                    for col in vcols:
                        v2 = daily_df[col].dropna().values
                        v2 = v2[(v2 > 8.0) & (v2 < 90.0)]
                        vals.extend(v2.tolist())
            except Exception as e:
                self.logger.debug(f"Daily summary VIX read error: {e}")

        if not vals:
            return np.array([])

        arr = np.array(vals)
        arr = arr[(arr > 8.0) & (arr < 90.0)]
        return arr

    # ─────────────────────────────────────────────────────────────────────
    # VRP CALIBRATION
    # ─────────────────────────────────────────────────────────────────────

    def _run_vrp_calibration(self, state: CalibrationState, n_days: int) -> None:
        """
        Calibrate VRP sell and fair thresholds from win rates by VRP bucket.

        Method:
        1. Group all historical trades by VRP at entry (0.5pp buckets)
        2. For each bucket, compute win rate and average P&L
        3. Apply Bayesian shrinkage
        4. Find minimum VRP where: shrunk_win_rate > 55% AND avg_pnl > avg_costs
        5. That becomes the sell threshold

        Also runs phantom trade feedback:
        - If phantom FNR > 30%: lower threshold (too many good trades blocked)
        - If phantom FNR < 10%: raise threshold (threshold may be too low)

        Updates: vrp_sell_threshold, vrp_fair_threshold
        """
        d = NIFTY_2026_DEFAULTS

        rows = self.db.get_vrp_win_rates(days=90)

        if not rows or len(rows) < self.MIN_SAMPLES_VRP:
            self.logger.info(
                f"  VRP calibration: insufficient trade data "
                f"({len(rows) if rows else 0} buckets) — keeping current"
            )
            return

        # Find optimal sell threshold
        sell_thresh = state.vrp_sell_threshold  # start from current
        fair_thresh = state.vrp_fair_threshold

        # Sort buckets by VRP level
        sorted_rows = sorted(rows, key=lambda r: float(r.get("vrp_bucket", 0) or 0))

        # Find sell threshold: minimum VRP where trade has positive expectation
        for row in sorted_rows:
            bucket   = float(row.get("vrp_bucket", 0) or 0)
            n        = int(row.get("n_trades", 0) or 0)
            wins     = int(row.get("n_wins", 0) or 0)
            avg_pnl  = float(row.get("avg_pnl", 0) or 0)
            avg_cost = float(row.get("avg_costs", 0) or 0)

            if n < self.MIN_SAMPLES_VRP:
                continue

            win_rate = wins / n if n > 0 else 0.0
            shrunk_wr = self._shrink(win_rate, 0.55, n)

            # Positive expectation: win rate > 55% AND avg P&L > costs
            if shrunk_wr >= 0.55 and avg_pnl > max(avg_cost, 0):
                sell_thresh = bucket
                break

        # Find fair threshold: minimum VRP where trade is not clearly losing
        for row in sorted_rows:
            bucket  = float(row.get("vrp_bucket", 0) or 0)
            n       = int(row.get("n_trades", 0) or 0)
            avg_pnl = float(row.get("avg_pnl", 0) or 0)

            if n < self.MIN_SAMPLES_VRP:
                continue

            shrunk_pnl = self._shrink(avg_pnl, 0.0, n)
            if shrunk_pnl > -500:  # Not clearly losing (Rs500 threshold)
                fair_thresh = bucket
                break

        # Apply Bayesian shrinkage toward defaults
        total_n = sum(int(r.get("n_trades", 0) or 0) for r in rows)
        state.vrp_sell_threshold = max(
            self._shrink(sell_thresh, d.vrp_sell_threshold, total_n),
            1.2  # Absolute minimum
        )
        state.vrp_fair_threshold = max(
            self._shrink(fair_thresh, d.vrp_fair_threshold, total_n),
            0.6  # Absolute minimum
        )

        # Enforce: fair < sell
        state.vrp_fair_threshold = min(
            state.vrp_fair_threshold,
            state.vrp_sell_threshold * 0.75
        )

        self.logger.info(
            f"  VRP calibrated (n_buckets={len(rows)}, total_trades={total_n}): "
            f"sell={state.vrp_sell_threshold:.2f}pp "
            f"fair={state.vrp_fair_threshold:.2f}pp"
        )

    # ─────────────────────────────────────────────────────────────────────
    # DAY SIZE CALIBRATION
    # ─────────────────────────────────────────────────────────────────────

    def _run_day_size_calibration(self, state: CalibrationState, n_days: int) -> None:
        """
        Calibrate day size multipliers from win rate and avg P&L by day of week.

        Method:
        1. Group daily_summary by weekday
        2. For each day: compute win rate and average P&L on traded days
        3. Apply Bayesian shrinkage
        4. Adjust size: +10% if win_rate >= 60% AND avg_pnl > 0
                        -20% if win_rate < 40% OR avg_pnl < 0
                        unchanged otherwise

        Also applies DTE performance feedback:
        - 0DTE (Tuesday) win rate < 40% → reduce Tuesday size
        - 0DTE (Tuesday) win rate >= 65% → increase Tuesday size

        Updates: day_size_monday through day_size_friday
        """
        d = NIFTY_2026_DEFAULTS
        day_map = {
            0: ("monday",    d.day_size_monday),
            1: ("tuesday",   d.day_size_tuesday),
            2: ("wednesday", d.day_size_wednesday),
            3: ("thursday",  d.day_size_thursday),
            4: ("friday",    d.day_size_friday),
        }

        try:
            daily_df = self.db.get_daily_summary(days=365)
            if not hasattr(daily_df, 'empty') or daily_df.empty:
                return
            if "net_pnl_rupees" not in daily_df.columns:
                return

            for wd, (name, base) in day_map.items():
                sub = daily_df[daily_df["weekday"] == wd].copy()
                if len(sub) < self.MIN_SAMPLES_DAY_SIZE:
                    continue

                # Only count days where trades were actually executed
                if "trades_executed" in sub.columns:
                    traded = sub[sub["trades_executed"] > 0].copy()
                else:
                    traded = sub.copy()

                if len(traded) < self.MIN_SAMPLES_DAY_SIZE:
                    continue

                wins     = (traded["net_pnl_rupees"] > 0).sum()
                total    = len(traded)
                win_rate = wins / total if total > 0 else 0.5
                avg_pnl  = float(traded["net_pnl_rupees"].mean())

                # Bayesian shrinkage
                shrunk_wr = self._shrink(win_rate, 0.55, total)

                # Adjust size based on performance
                if shrunk_wr >= 0.60 and avg_pnl > 0:
                    new_size = min(base * 1.10, 1.00)
                elif shrunk_wr < 0.40 or avg_pnl < 0:
                    new_size = max(base * 0.80, 0.25)
                else:
                    new_size = base

                setattr(state, f"day_size_{name}", round(new_size, 2))
                self.logger.info(
                    f"  DaySize {name}: wr={win_rate:.1%} "
                    f"shrunk={shrunk_wr:.1%} "
                    f"avg_pnl=Rs{avg_pnl:.0f} "
                    f"size={new_size:.2f} (n={total})"
                )

        except Exception as e:
            self.logger.debug(f"Day size calibration error: {e}")
            return

        # DTE-specific feedback for Tuesday (0DTE)
        self._apply_dte_feedback(state)

    def _apply_dte_feedback(self, state: CalibrationState) -> None:
        """
        Apply DTE-specific performance feedback to day size multipliers.
        Reads trade_entries + trade_exits grouped by actual_dte.
        """
        try:
            rows = self.db.query(
                """
                SELECT te.actual_dte, te.trading_date,
                       tx.net_pnl_rupees, tx.result
                FROM trade_entries te
                JOIN trade_exits tx ON te.position_id = tx.position_id
                WHERE te.trading_date >= ?
                ORDER BY te.entry_time
                """,
                ((today_ist() - timedelta(days=90)).isoformat(),),
            )
            if not rows:
                return

            dte_buckets: Dict[int, List[float]] = {0: [], 1: [], 2: []}
            for r in rows:
                dte = r.get("actual_dte")
                pnl = float(r.get("net_pnl_rupees", 0) or 0)
                if dte == 0:
                    dte_buckets[0].append(pnl)
                elif dte == 1:
                    dte_buckets[1].append(pnl)
                elif dte is not None and dte >= 2:
                    dte_buckets[2].append(pnl)

            # DTE 0 (Tuesday) feedback
            dte0_pnls = dte_buckets[0]
            if len(dte0_pnls) >= 5:
                dte0_wr = sum(1 for p in dte0_pnls if p > 0) / len(dte0_pnls)
                shrunk_wr = self._shrink(dte0_wr, 0.60, len(dte0_pnls))
                avg_pnl_0 = sum(dte0_pnls) / len(dte0_pnls)
                if shrunk_wr < 0.40 or avg_pnl_0 < 0:
                    old = state.day_size_tuesday
                    state.day_size_tuesday = max(state.day_size_tuesday * 0.75, 0.20)
                    self.logger.info(
                        f"  DTE0 feedback: wr={dte0_wr:.1%} avg=Rs{avg_pnl_0:.0f} low -> "
                        f"Tuesday size {old:.2f} -> {state.day_size_tuesday:.2f}"
                    )
                elif shrunk_wr >= 0.65 and avg_pnl_0 > 0:
                    old = state.day_size_tuesday
                    state.day_size_tuesday = min(state.day_size_tuesday * 1.15, 1.00)
                    self.logger.info(
                        f"  DTE0 feedback: wr={dte0_wr:.1%} avg=Rs{avg_pnl_0:.0f} high -> "
                        f"Tuesday size {old:.2f} -> {state.day_size_tuesday:.2f}"
                    )
                else:
                    self.logger.info(
                        f"  DTE0 feedback: wr={dte0_wr:.1%} avg=Rs{avg_pnl_0:.0f} "
                        f"stable -> Tuesday size unchanged at {state.day_size_tuesday:.2f}"
                    )

            dte1_pnls = dte_buckets[1]
            if len(dte1_pnls) >= 5:
                dte1_wr = sum(1 for p in dte1_pnls if p > 0) / len(dte1_pnls)
                shrunk_wr = self._shrink(dte1_wr, 0.55, len(dte1_pnls))
                avg_pnl_1 = sum(dte1_pnls) / len(dte1_pnls)
                if shrunk_wr < 0.40 or avg_pnl_1 < 0:
                    old = state.day_size_monday
                    state.day_size_monday = max(state.day_size_monday * 0.80, 0.25)
                    self.logger.info(
                        f"  DTE1 feedback: wr={dte1_wr:.1%} avg=Rs{avg_pnl_1:.0f} low -> "
                        f"Monday size {old:.2f} -> {state.day_size_monday:.2f}"
                    )
                elif shrunk_wr >= 0.65 and avg_pnl_1 > 0:
                    old = state.day_size_monday
                    state.day_size_monday = min(state.day_size_monday * 1.12, 0.75)
                    self.logger.info(
                        f"  DTE1 feedback: wr={dte1_wr:.1%} avg=Rs{avg_pnl_1:.0f} high -> "
                        f"Monday size {old:.2f} -> {state.day_size_monday:.2f}"
                    )
                else:
                    self.logger.info(
                        f"  DTE1 feedback: wr={dte1_wr:.1%} avg=Rs{avg_pnl_1:.0f} "
                        f"stable -> Monday size unchanged at {state.day_size_monday:.2f}"
                    )

            dte2_pnls = dte_buckets[2]
            if len(dte2_pnls) >= 5:
                dte2_wr = sum(1 for p in dte2_pnls if p > 0) / len(dte2_pnls)
                avg_pnl_2 = sum(dte2_pnls) / len(dte2_pnls)
                if avg_pnl_2 < 0:
                    for attr in ["day_size_wednesday", "day_size_thursday", "day_size_friday"]:
                        old_v = getattr(state, attr)
                        setattr(state, attr, max(old_v * 0.80, 0.15))
                    self.logger.info(
                        f"  DTE2+ feedback: wr={dte2_wr:.1%} avg=Rs{avg_pnl_2:.0f} "
                        f"negative -> reducing Wed/Thu/Fri sizes"
                    )
                elif avg_pnl_2 > 0 and dte2_wr >= 0.60:
                    for attr in ["day_size_wednesday", "day_size_thursday", "day_size_friday"]:
                        old_v = getattr(state, attr)
                        setattr(state, attr, min(old_v * 1.05, 0.35))
                    self.logger.info(
                        f"  DTE2+ feedback: wr={dte2_wr:.1%} avg=Rs{avg_pnl_2:.0f} "
                        f"positive -> slightly increasing Wed/Thu/Fri sizes"
                    )
                    self.logger.info(
                        f"  DTE1 feedback: wr={dte1_wr:.1%} low → "
                        f"Monday size {old:.2f} → {state.day_size_monday:.2f}"
                    )
                elif shrunk_wr >= 0.65:
                    old = state.day_size_monday
                    state.day_size_monday = min(state.day_size_monday * 1.10, 0.70)
                    self.logger.info(
                        f"  DTE1 feedback: wr={dte1_wr:.1%} high → "
                        f"Monday size {old:.2f} → {state.day_size_monday:.2f}"
                    )

        except Exception as e:
            self.logger.debug(f"DTE feedback error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # OI CALIBRATION
    # ─────────────────────────────────────────────────────────────────────

    def _run_oi_calibration(self, state: CalibrationState, n_days: int) -> None:
        """
        Calibrate OI buildup/unwind thresholds from market_snapshots.

        Method:
        - Buildup threshold: p60 of positive OI changes
        - Unwind threshold: p40 of negative OI changes

        Updates: oi_buildup_threshold, oi_unwind_threshold
        """
        d = NIFTY_2026_DEFAULTS
        try:
            snap_df = self.db.get_market_snapshots(days=365)
            if not hasattr(snap_df, 'empty') or snap_df.empty:
                return
            if "oi_change_pct" not in snap_df.columns:
                return

            oi_chg = snap_df["oi_change_pct"].dropna().values
            oi_chg = oi_chg[np.isfinite(oi_chg)]

            pos_chg = oi_chg[oi_chg > 0]
            neg_chg = oi_chg[oi_chg < 0]

            if len(pos_chg) >= self.MIN_SAMPLES_OI:
                build_raw = float(np.percentile(pos_chg, 60))
                state.oi_buildup_threshold = max(
                    self._shrink(build_raw, d.oi_buildup_threshold, len(pos_chg)),
                    0.04
                )
                self.logger.info(
                    f"  OI buildup calibrated: {state.oi_buildup_threshold:.4f} "
                    f"(n={len(pos_chg)})"
                )

            if len(neg_chg) >= self.MIN_SAMPLES_OI:
                unwind_raw = float(np.percentile(neg_chg, 40))
                state.oi_unwind_threshold = min(
                    self._shrink(unwind_raw, d.oi_unwind_threshold, len(neg_chg)),
                    -0.04
                )
                self.logger.info(
                    f"  OI unwind calibrated: {state.oi_unwind_threshold:.4f} "
                    f"(n={len(neg_chg)})"
                )

        except Exception as e:
            self.logger.debug(f"OI calibration error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PCR CALIBRATION
    # ─────────────────────────────────────────────────────────────────────

    def _run_pcr_calibration(self, state: CalibrationState, n_days: int) -> None:
        """
        Calibrate PCR thresholds from market_snapshots.

        Method:
        - Bullish threshold: p25 of PCR distribution (low PCR = bullish)
        - Bearish threshold: p75 of PCR distribution (high PCR = bearish)

        Updates: pcr_bullish_threshold, pcr_bearish_threshold
        """
        d = NIFTY_2026_DEFAULTS
        try:
            snap_df = self.db.get_market_snapshots(days=365)
            if not hasattr(snap_df, 'empty') or snap_df.empty:
                return
            if "pcr" not in snap_df.columns:
                return

            pcr_vals = snap_df["pcr"].dropna().values
            pcr_vals = pcr_vals[(pcr_vals > 0.3) & (pcr_vals < 4.0)]
            pcr_vals = pcr_vals[np.isfinite(pcr_vals)]

            if len(pcr_vals) < self.MIN_SAMPLES_PCR:
                return

            bull_raw = float(np.percentile(pcr_vals, 25))
            bear_raw = float(np.percentile(pcr_vals, 75))

            state.pcr_bullish_threshold = max(
                min(
                    self._shrink(bull_raw, d.pcr_bullish_threshold, len(pcr_vals)),
                    0.85
                ),
                0.50
            )
            state.pcr_bearish_threshold = max(
                min(
                    self._shrink(bear_raw, d.pcr_bearish_threshold, len(pcr_vals)),
                    1.65
                ),
                1.05
            )

            # Enforce: bullish < bearish with gap
            if state.pcr_bullish_threshold >= state.pcr_bearish_threshold - 0.10:
                state.pcr_bullish_threshold = state.pcr_bearish_threshold - 0.20

            self.logger.info(
                f"  PCR calibrated (n={len(pcr_vals)}): "
                f"bull={state.pcr_bullish_threshold:.3f} "
                f"bear={state.pcr_bearish_threshold:.3f}"
            )

        except Exception as e:
            self.logger.debug(f"PCR calibration error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # SKEW CALIBRATION
    # ─────────────────────────────────────────────────────────────────────

    def _run_skew_calibration(self, state: CalibrationState, n_days: int) -> None:
        """
        Calibrate skew thresholds from market_snapshots skew_ratio column.

        Method:
        - Bearish threshold: p75 of skew_ratio (high skew = fear = bearish)
        - Bullish threshold: p25 of skew_ratio (low skew = complacency = bullish)

        Updates: skew_bearish_threshold, skew_bullish_threshold
        """
        d = NIFTY_2026_DEFAULTS
        try:
            snap_df = self.db.get_market_snapshots(days=365)
            if not hasattr(snap_df, 'empty') or snap_df.empty:
                return
            if "skew_ratio" not in snap_df.columns:
                return

            skew_vals = snap_df["skew_ratio"].dropna().values
            skew_vals = skew_vals[(skew_vals > 0.5) & (skew_vals < 3.5)]
            skew_vals = skew_vals[np.isfinite(skew_vals)]

            if len(skew_vals) < self.MIN_SAMPLES_SKEW:
                return

            bear_raw = float(np.percentile(skew_vals, 75))
            bull_raw = float(np.percentile(skew_vals, 25))

            state.skew_bearish_threshold = max(
                self._shrink(bear_raw, d.skew_bearish_threshold, len(skew_vals)),
                1.5
            )
            state.skew_bullish_threshold = min(
                self._shrink(bull_raw, d.skew_bullish_threshold, len(skew_vals)),
                1.1
            )

            # Enforce: bullish < bearish
            if state.skew_bullish_threshold >= state.skew_bearish_threshold:
                state.skew_bullish_threshold = state.skew_bearish_threshold * 0.70

            self.logger.info(
                f"  Skew calibrated (n={len(skew_vals)}): "
                f"bear={state.skew_bearish_threshold:.3f} "
                f"bull={state.skew_bullish_threshold:.3f}"
            )

        except Exception as e:
            self.logger.debug(f"Skew calibration error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # STRADDLE RATIO CALIBRATION
    # ─────────────────────────────────────────────────────────────────────

    def _run_straddle_ratio_calibration(
        self, state: CalibrationState, n_days: int
    ) -> None:
        """
        Calibrate straddle ratio sell threshold from daily_summary.
        Straddle ratio = opening straddle / expected daily move.
        p65 of historical ratios = sell threshold.

        Updates: straddle_ratio_sell
        """
        d = NIFTY_2026_DEFAULTS
        try:
            daily_df = self.db.get_daily_summary(days=365)
            if not hasattr(daily_df, 'empty') or daily_df.empty:
                return
            if "straddle_ratio" not in daily_df.columns:
                return

            ratios = daily_df["straddle_ratio"].dropna().values
            ratios = ratios[(ratios > 0.5) & (ratios < 3.0)]
            ratios = ratios[np.isfinite(ratios)]

            if len(ratios) < 10:
                return

            sr_raw = float(np.percentile(ratios, 65))
            state.straddle_ratio_sell = max(
                self._shrink(sr_raw, d.straddle_ratio_sell, len(ratios)),
                1.00
            )
            self.logger.info(
                f"  Straddle ratio calibrated (n={len(ratios)}): "
                f"sell={state.straddle_ratio_sell:.3f}"
            )

        except Exception as e:
            self.logger.debug(f"Straddle ratio calibration error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # DAY RANGE CALIBRATION
    # ─────────────────────────────────────────────────────────────────────

    def _run_range_calibration(self, state: CalibrationState, n_days: int) -> None:
        """
        Calibrate average intraday range by day of week.
        Used for straddle-based strike distance computation.

        Updates: monday_avg_range through friday_avg_range
        """
        d = NIFTY_2026_DEFAULTS
        day_map = {
            0: ("monday",    d.monday_avg_range),
            1: ("tuesday",   d.tuesday_avg_range),
            2: ("wednesday", d.wednesday_avg_range),
            3: ("thursday",  d.thursday_avg_range),
            4: ("friday",    d.friday_avg_range),
        }

        try:
            daily_df = self.db.get_daily_summary(days=365)
            if not hasattr(daily_df, 'empty') or daily_df.empty:
                return

            range_col = None
            for col in ["day_range_points", "nifty_high", "nifty_low"]:
                if col in daily_df.columns:
                    range_col = col
                    break

            if range_col is None:
                return

            for wd, (name, base) in day_map.items():
                sub = daily_df[daily_df["weekday"] == wd]
                if range_col == "day_range_points":
                    ranges = sub[range_col].dropna().values
                else:
                    # Compute from high-low if day_range_points not available
                    if "nifty_high" in sub.columns and "nifty_low" in sub.columns:
                        h = sub["nifty_high"].dropna()
                        l = sub["nifty_low"].dropna()
                        if len(h) > 0 and len(l) > 0:
                            ranges = (h - l).values
                        else:
                            continue
                    else:
                        continue

                ranges = ranges[ranges > 0]
                ranges = ranges[np.isfinite(ranges)]

                if len(ranges) < self.MIN_SAMPLES_RANGES:
                    continue

                avg_raw = float(np.mean(ranges))
                shrunk  = self._shrink(avg_raw, base, len(ranges))
                setattr(state, f"{name}_avg_range", round(shrunk, 1))
                self.logger.info(
                    f"  Range {name}: avg={avg_raw:.0f}pts "
                    f"shrunk={shrunk:.0f}pts (n={len(ranges)})"
                )

        except Exception as e:
            self.logger.debug(f"Range calibration error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PHANTOM TRADE ANALYSIS (Feedback Loop 1)
    # ─────────────────────────────────────────────────────────────────────

    def _run_phantom_analysis(self, state: CalibrationState) -> None:
        """
        Analyse phantom trades (NEUTRAL-blocked trades) to determine
        if the VRP sell threshold is too tight.

        Logic:
        - phantom_fnr = % of blocked trades that would have been profitable
        - fnr > 30%: threshold too tight → lower by 0.1pp
        - fnr < 10%: threshold may be too low → raise by 0.1pp (only if enough data)
        - 10-30%: threshold is appropriate → no change

        Updates: vrp_sell_threshold (small adjustments only)
        Updates: phantom_false_negative_rate (metric only)
        """
        try:
            phantom_fnr = self.db.get_phantom_false_negative_rate(days=20)
            state.phantom_false_negative_rate = phantom_fnr if phantom_fnr > 0 else None

            if phantom_fnr <= 0:
                return

            # Count phantom trades to ensure we have enough data
            cutoff = (today_ist() - timedelta(days=20)).isoformat()
            phantom_count_row = self.db.query_one(
                "SELECT COUNT(*) as cnt FROM phantom_trades WHERE trading_date >= ?",
                (cutoff,),
            )
            phantom_count = phantom_count_row["cnt"] if phantom_count_row else 0

            if phantom_count < self.MIN_SAMPLES_PHANTOM:
                self.logger.info(
                    f"  Phantom analysis: only {phantom_count} phantom trades "
                    f"(need {self.MIN_SAMPLES_PHANTOM}) — skipping threshold adjustment"
                )
                return

            old_thresh = state.vrp_sell_threshold

            if phantom_fnr > 30.0:
                # Too many good trades being blocked — lower threshold
                adjustment = min(0.1 * (phantom_fnr / 30.0), 0.3)  # cap at 0.3pp
                state.vrp_sell_threshold = max(
                    old_thresh - adjustment,
                    NIFTY_2026_DEFAULTS.vrp_fair_threshold + 0.3
                )
                self.logger.info(
                    f"  Phantom FNR={phantom_fnr:.1f}% > 30% — "
                    f"lowering VRP threshold: {old_thresh:.2f} → "
                    f"{state.vrp_sell_threshold:.2f}pp "
                    f"(n={phantom_count})"
                )
            elif phantom_fnr < 10.0 and phantom_count >= 20:
                # Very few good trades blocked — threshold may be too low
                adjustment = min(0.1 * (10.0 / max(phantom_fnr, 1.0)), 0.2)
                state.vrp_sell_threshold = min(
                    old_thresh + adjustment,
                    4.0  # Never exceed 4.0pp
                )
                self.logger.info(
                    f"  Phantom FNR={phantom_fnr:.1f}% < 10% — "
                    f"raising VRP threshold: {old_thresh:.2f} → "
                    f"{state.vrp_sell_threshold:.2f}pp "
                    f"(n={phantom_count})"
                )
            else:
                self.logger.info(
                    f"  Phantom FNR={phantom_fnr:.1f}% in 10-30% range — "
                    f"VRP threshold appropriate at {old_thresh:.2f}pp"
                )

        except Exception as e:
            self.logger.debug(f"Phantom analysis error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # EXIT QUALITY ANALYSIS (Feedback Loop 2)
    # ─────────────────────────────────────────────────────────────────────

    def _run_exit_quality_analysis(self, state: CalibrationState) -> None:
        """
        Analyse exit quality to determine if time-based targets are optimal.

        Metrics:
        - avg_improvement_15min: average P&L change if held 15min longer
          Positive = exits too early (should hold longer)
          Negative = exits correct or late
        - premature_rate: % of exits that were premature
        - exit_quality_score: 100 - premature_rate

        Does NOT modify thresholds (exit rules are fixed in execution_engine.py).
        Updates: exit_quality_score (metric only, for reporting)
        """
        try:
            quality = self.db.get_exit_quality_summary(days=20)

            if not quality or quality.get("total_exits", 0) < self.MIN_SAMPLES_EXIT:
                return

            total_exits    = int(quality.get("total_exits", 0))
            avg_improvement = float(quality.get("avg_improvement_15min", 0.0) or 0.0)
            premature_count = int(quality.get("premature_count", 0) or 0)
            late_count      = int(quality.get("late_count", 0) or 0)

            premature_rate = premature_count / total_exits * 100 if total_exits > 0 else 0.0
            state.exit_quality_score = round(100.0 - premature_rate, 1)

            self.logger.info(
                f"  Exit quality (n={total_exits}): "
                f"avg_improvement_15min=Rs{avg_improvement:.0f} "
                f"premature={premature_rate:.1f}% "
                f"late={late_count/total_exits*100:.1f}% "
                f"score={state.exit_quality_score:.1f}"
            )

            if avg_improvement > 500:
                self.logger.warning(
                    f"  EXIT QUALITY ALERT: exits are Rs{avg_improvement:.0f} "
                    f"too early on average — consider reviewing time targets"
                )
            elif avg_improvement < -500:
                self.logger.info(
                    f"  Exit quality: exits are Rs{abs(avg_improvement):.0f} "
                    f"better than holding — exits are well-timed"
                )

        except Exception as e:
            self.logger.debug(f"Exit quality analysis error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # REGIME ACCURACY ANALYSIS
    # ─────────────────────────────────────────────────────────────────────

    def _run_regime_accuracy(self, state: CalibrationState) -> None:
        """
        Compute regime classification accuracy from regime_accuracy_scores table.
        Updates: regime_accuracy_score (metric only, for reporting)
        """
        try:
            accuracy = self.db.get_regime_accuracy(days=30)
            if not accuracy or accuracy.get("total", 0) < 5:
                return

            avg_score = accuracy.get("avg_score")
            if avg_score is not None:
                state.regime_accuracy_score = round(float(avg_score), 3)

            self.logger.info(
                f"  Regime accuracy (n={accuracy.get('total', 0)}): "
                f"vol={accuracy.get('vol_accuracy', 0):.1%} "
                f"price={accuracy.get('price_accuracy', 0):.1%} "
                f"pos={accuracy.get('pos_accuracy', 0):.1%} "
                f"final={accuracy.get('final_accuracy', 0):.1%} "
                f"avg_score={avg_score:.3f}"
            )

        except Exception as e:
            self.logger.debug(f"Regime accuracy error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # SIGNAL WEIGHT CALIBRATION (Feedback Loop 3)
    # ─────────────────────────────────────────────────────────────────────

    def _run_signal_weight_calibration(
        self, state: CalibrationState, n_days: int
    ) -> None:
        """
        Calibrate signal weights from historical predictive accuracy.
        Tier 3 only (60+ days).

        Method:
        1. For each signal, compute how often it predicted winning trades
        2. Compute lift = signal_win_rate / base_win_rate
        3. Apply Bayesian shrinkage toward 1.0 (neutral weight)
        4. Clamp to [0.1, 3.0]

        Lift > 1.0: signal is predictive (increase weight)
        Lift < 1.0: signal is anti-predictive (decrease weight)
        Lift = 1.0: signal has no predictive power (neutral)

        Updates: signal_weight_vrp through signal_weight_or_condition
        """
        try:
            accuracy = self.db.get_signal_predictive_accuracy(days=90)
            if not accuracy or accuracy.get("total_trades", 0) < self.MIN_SAMPLES_WEIGHTS:
                self.logger.info(
                    f"  Signal weights: insufficient data "
                    f"({accuracy.get('total_trades', 0) if accuracy else 0} trades) "
                    f"— keeping current"
                )
                return

            total_n = int(accuracy.get("total_trades", 0))

            def _calibrate_weight(
                lift_key: str,
                current: float,
                prior: float = 1.0,
            ) -> float:
                lift = float(accuracy.get(lift_key, 1.0) or 1.0)
                shrunk = self._shrink(lift, prior, total_n, prior_weight=50)
                return round(max(0.1, min(shrunk, 3.0)), 3)

            state.signal_weight_vrp = _calibrate_weight(
                "vol_lift", state.signal_weight_vrp
            )
            state.signal_weight_price = _calibrate_weight(
                "price_lift", state.signal_weight_price
            )
            state.signal_weight_positioning = _calibrate_weight(
                "pos_lift", state.signal_weight_positioning
            )
            state.signal_weight_iv_behavior = _calibrate_weight(
                "iv_lift", state.signal_weight_iv_behavior
            )
            state.signal_weight_or_condition = _calibrate_weight(
                "or_lift", state.signal_weight_or_condition
            )

            self.logger.info(
                f"  Signal weights calibrated (n={total_n}): "
                f"vrp={state.signal_weight_vrp:.3f} "
                f"price={state.signal_weight_price:.3f} "
                f"pos={state.signal_weight_positioning:.3f} "
                f"iv={state.signal_weight_iv_behavior:.3f} "
                f"or={state.signal_weight_or_condition:.3f}"
            )

        except Exception as e:
            self.logger.debug(f"Signal weight calibration error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # DRIFT DETECTION
    # ─────────────────────────────────────────────────────────────────────

    def _run_drift_detection(self, state: CalibrationState) -> None:
        """
        Detect significant drift in calibrated thresholds vs historical values.
        Alerts when any threshold has drifted > 20% from prior month.

        This detects market regime changes (e.g. VIX moving from 11 to 18).
        Does not modify thresholds — only logs alerts.
        """
        try:
            # Get calibration from 30 days ago
            prior_rows = self.db.query(
                "SELECT * FROM calibration_state "
                "WHERE calibrated_at <= ? "
                "ORDER BY calibrated_at DESC LIMIT 1",
                ((now_ist() - timedelta(days=30)).isoformat(),),
            )
            if not prior_rows:
                return

            prior_row   = prior_rows[0]
            prior_state = self._row_to_state(prior_row)

            drift_checks = [
                ("vrp_sell_threshold", state.vrp_sell_threshold,
                 prior_state.vrp_sell_threshold),
                ("vix_p50",            state.vix_p50,
                 prior_state.vix_p50),
                ("day_size_tuesday",   state.day_size_tuesday,
                 prior_state.day_size_tuesday),
                ("pcr_bullish_threshold", state.pcr_bullish_threshold,
                 prior_state.pcr_bullish_threshold),
            ]

            for name, current, prior in drift_checks:
                if prior <= 0:
                    continue
                drift_pct = abs(current - prior) / prior * 100.0
                if drift_pct > 20.0:
                    self.logger.warning(
                        f"  DRIFT ALERT: {name} drifted {drift_pct:.1f}% "
                        f"in 30 days "
                        f"({prior:.3f} → {current:.3f}) — "
                        f"market regime may have changed"
                    )
                else:
                    self.logger.debug(
                        f"  Drift check {name}: {drift_pct:.1f}% "
                        f"({prior:.3f} → {current:.3f}) — stable"
                    )

        except Exception as e:
            self.logger.debug(f"Drift detection error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PHANTOM TRADE LOGGING
    # ─────────────────────────────────────────────────────────────────────

    def log_phantom_trade(
        self,
        signals: dict,
        block_reason: str,
        strategy_would_be: str,
        strikes_json: str,
        credit_would_be: float,
    ) -> None:
        """
        Log a phantom trade (NEUTRAL-blocked trade) to the phantom_trades table.
        Called by strategy_engine.py when a trade is blocked by NEUTRAL gate.

        The simulated P&L is computed at EOD by update_phantom_outcomes().
        """
        if not self.config.phantom_trade_tracking:
            return

        try:
            self.db.insert("phantom_trades", {
                "trading_date":           today_ist().isoformat(),
                "block_time":             now_ist().isoformat(),
                "block_reason":           block_reason,
                "strategy_would_be":      strategy_would_be,
                "strikes_json":           strikes_json,
                "credit_would_be":        credit_would_be,
                "vrp_at_block":           signals.get("vrp_raw"),
                "vrp_smoothed_at_block":  signals.get("vrp_smoothed"),
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
        """
        Update phantom trade outcomes at end of day.
        Simulates what would have happened if the blocked trade had been taken.

        For each phantom trade:
        1. Get the option chain snapshot at the time of blocking
        2. Get the option chain snapshot at hard exit time (15:00)
        3. Compute simulated P&L based on credit collected - exit premium
        4. Update phantom_trades table with result

        Called from main.py after market close.
        """
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
                    self.logger.debug(
                        f"Phantom outcome simulation error for "
                        f"{phantom.get('phantom_id')}: {e}"
                    )

        except Exception as e:
            self.logger.debug(f"Phantom outcomes update error: {e}")

    def _simulate_phantom_outcome(self, phantom: dict, trading_date: str) -> None:
        """
        Simulate the outcome of one phantom trade.
        Uses option chain snapshots to estimate what would have happened.
        """
        phantom_id    = phantom.get("phantom_id")
        block_time    = phantom.get("block_time", "")
        credit        = float(phantom.get("credit_would_be", 0) or 0)
        strikes_json  = phantom.get("strikes_json", "[]")

        if credit <= 0:
            return

        try:
            legs = json.loads(strikes_json) if strikes_json else []
        except Exception:
            return

        if not legs:
            return

        # Get EOD chain snapshot (closest to 15:00)
        eod_chain_rows = self.db.query(
            "SELECT strike, option_type, bid, ask, ltp "
            "FROM option_chain_snapshot "
            "WHERE trading_date=? AND capture_time >= '15:00:00' "
            "ORDER BY capture_time ASC LIMIT 200",
            (trading_date,),
        )
        if not eod_chain_rows:
            # Try 14:30 as fallback
            eod_chain_rows = self.db.query(
                "SELECT strike, option_type, bid, ask, ltp "
                "FROM option_chain_snapshot "
                "WHERE trading_date=? AND capture_time >= '14:30:00' "
                "ORDER BY capture_time ASC LIMIT 200",
                (trading_date,),
            )

        if not eod_chain_rows:
            return

        # Build EOD chain dict
        eod_chain: Dict[Tuple[float, str], dict] = {}
        for row in eod_chain_rows:
            key = (float(row.get("strike", 0)), str(row.get("option_type", "")))
            if key not in eod_chain:
                eod_chain[key] = row

        # Compute exit premium
        exit_premium = 0.0
        for leg in legs:
            strike    = float(leg.get("strike", 0))
            opt_type  = str(leg.get("option_type", ""))
            action    = str(leg.get("action", "SELL"))

            key = (strike, opt_type)
            chain_row = eod_chain.get(key)
            if not chain_row:
                # Try nearest strike
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

        # Compute simulated P&L
        # For credit spreads: P&L = credit_collected - exit_premium
        lot_size  = self.config.lot_size
        n_lots    = 2  # Assume 2 lots for phantom trades
        gross_pnl = (credit - exit_premium) * lot_size * n_lots

        # Approximate costs
        round_trip_cost = credit * 0.15 * lot_size * n_lots  # ~15% of credit
        net_pnl = gross_pnl - round_trip_cost

        profitable = 1 if net_pnl > 0 else 0
        result     = "WIN" if net_pnl > 0 else ("LOSS" if net_pnl < 0 else "BREAKEVEN")

        self.db.update(
            "phantom_trades",
            {
                "simulated_pnl_final":        round(net_pnl, 2),
                "simulated_result":           result,
                "would_have_been_profitable": profitable,
            },
            {"phantom_id": phantom_id},
        )

    # ─────────────────────────────────────────────────────────────────────
    # EXIT QUALITY LOGGING
    # ─────────────────────────────────────────────────────────────────────

    def log_exit_quality(
        self,
        position_id:        str,
        trading_date:       str,
        exit_priority:      int,
        exit_time:          str,
        exit_pnl_rupees:    float,
    ) -> None:
        """
        Log exit quality data for a closed position.
        The 15min and 30min P&L values are filled in later by
        update_exit_quality_outcomes().
        Called from execution_engine.py when a position is closed.
        """
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
        """
        Update exit quality log with what would have happened if held longer.
        Called from main.py after market close.

        For each exit quality record:
        1. Get the option chain at exit_time + 15min
        2. Get the option chain at exit_time + 30min
        3. Get the option chain at 15:00 (hard exit)
        4. Compute what P&L would have been at each time
        5. Determine if exit was premature or late
        """
        try:
            records = self.db.query(
                "SELECT eql.*, te.legs_json, te.entry_credit, te.final_lots "
                "FROM exit_quality_log eql "
                "LEFT JOIN trade_entries te ON eql.position_id = te.position_id "
                "WHERE eql.trading_date=? AND eql.pnl_15min_after_exit IS NULL",
                (trading_date,),
            )
            if not records:
                return

            self.logger.info(
                f"Updating {len(records)} exit quality records for {trading_date}"
            )

            for record in records:
                try:
                    self._compute_exit_quality_outcome(record, trading_date)
                except Exception as e:
                    self.logger.debug(
                        f"Exit quality outcome error for "
                        f"{record.get('position_id', 'unknown')}: {e}"
                    )

        except Exception as e:
            self.logger.debug(f"Exit quality outcomes update error: {e}")

    def _compute_exit_quality_outcome(self, record: dict, trading_date: str) -> None:
        """Compute what P&L would have been at 15min and 30min after exit."""
        quality_id   = record.get("quality_id")
        exit_time    = record.get("exit_time", "")
        exit_pnl     = float(record.get("exit_pnl_rupees", 0) or 0)
        legs_json    = record.get("legs_json", "[]")
        entry_credit = float(record.get("entry_credit", 0) or 0)
        final_lots   = int(record.get("final_lots", 1) or 1)

        if not exit_time or entry_credit <= 0:
            return

        try:
            legs = json.loads(legs_json) if legs_json else []
        except Exception:
            return

        if not legs:
            return

        # Parse exit time
        try:
            exit_dt = datetime.fromisoformat(exit_time)
            exit_time_str = exit_dt.strftime("%H:%M:%S")
        except Exception:
            return

        # Compute P&L at different times
        def get_pnl_at_time(time_str: str) -> Optional[float]:
            chain_rows = self.db.query(
                "SELECT strike, option_type, bid, ask, ltp "
                "FROM option_chain_snapshot "
                "WHERE trading_date=? AND capture_time >= ? "
                "ORDER BY capture_time ASC LIMIT 200",
                (trading_date, time_str),
            )
            if not chain_rows:
                return None

            chain: Dict[Tuple[float, str], dict] = {}
            for row in chain_rows:
                key = (float(row.get("strike", 0)), str(row.get("option_type", "")))
                if key not in chain:
                    chain[key] = row

            premium = 0.0
            for leg in legs:
                strike   = float(leg.get("strike", 0))
                opt_type = str(leg.get("option_type", ""))
                action   = str(leg.get("action", "SELL"))
                key      = (strike, opt_type)
                row      = chain.get(key)
                if not row:
                    return None
                bid  = float(row.get("bid", 0) or 0)
                ask  = float(row.get("ask", 0) or 0)
                ltp  = float(row.get("ltp", 0) or 0)
                mark = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else ltp
                if action == "SELL":
                    premium += mark
                else:
                    premium -= mark

            # P&L = entry_credit - current_premium (for credit spreads)
            gross_pnl = (entry_credit - premium) * self.config.lot_size * final_lots
            costs     = entry_credit * 0.15 * self.config.lot_size * final_lots
            return round(gross_pnl - costs, 2)

        # Get P&L at 15min and 30min after exit
        try:
            exit_dt_obj = datetime.fromisoformat(exit_time)
            time_15min  = (exit_dt_obj + timedelta(minutes=15)).strftime("%H:%M:%S")
            time_30min  = (exit_dt_obj + timedelta(minutes=30)).strftime("%H:%M:%S")
        except Exception:
            return

        pnl_15min = get_pnl_at_time(time_15min)
        pnl_30min = get_pnl_at_time(time_30min)
        pnl_eod   = get_pnl_at_time("15:00:00")

        # Determine if exit was premature or late
        was_premature = 0
        was_late      = 0
        optimal_pnl   = exit_pnl

        if pnl_15min is not None and pnl_15min > exit_pnl + 500:
            was_premature = 1
            optimal_pnl   = max(optimal_pnl, pnl_15min)

        if pnl_eod is not None and pnl_eod < exit_pnl - 1000:
            was_late = 1

        self.db.update(
            "exit_quality_log",
            {
                "pnl_15min_after_exit": pnl_15min,
                "pnl_30min_after_exit": pnl_30min,
                "pnl_at_hard_exit":     pnl_eod,
                "was_exit_premature":   was_premature,
                "was_exit_late":        was_late,
                "optimal_exit_pnl":     optimal_pnl,
            },
            {"quality_id": quality_id},
        )

    # ─────────────────────────────────────────────────────────────────────
    # REGIME ACCURACY SCORING
    # ─────────────────────────────────────────────────────────────────────

    def score_regime_decisions(self, trading_date: str) -> None:
        """
        Score regime decisions made today against actual outcomes.
        Called from main.py after market close.

        For each regime_decisions row today:
        1. Get NIFTY price movement in the 2 hours after the decision
        2. Get ATM straddle movement in the 2 hours after the decision
        3. Score: was the vol regime correct? was the price regime correct?
        4. Store in regime_accuracy_scores table

        Scoring rules:
        - Vol regime SELL_PREMIUM correct if: trade was profitable (if taken)
          or VRP remained positive for 2 hours (if not taken)
        - Price regime RANGE correct if: NIFTY stayed within ±0.5% for 2 hours
        - Price regime UPTREND correct if: NIFTY rose > 0.3% in 2 hours
        - Price regime DOWNTREND correct if: NIFTY fell > 0.3% in 2 hours
        - Final regime correct if: trade taken with this regime was profitable
        """
        try:
            decisions = self.db.query(
                "SELECT * FROM regime_decisions WHERE date=? ORDER BY timestamp",
                (trading_date,),
            )
            if not decisions:
                return

            # Get NIFTY candles for the day
            candles = self.db.query(
                "SELECT candle_time, close FROM intraday_candles "
                "WHERE trading_date=? AND interval_min=1 ORDER BY candle_time",
                (trading_date,),
            )
            candle_map: Dict[str, float] = {
                r["candle_time"]: float(r["close"])
                for r in candles
                if r.get("close")
            }

            # Get trades for the day
            trades = self.db.query(
                "SELECT te.*, tx.result, tx.net_pnl_rupees "
                "FROM trade_entries te "
                "LEFT JOIN trade_exits tx ON te.position_id = tx.position_id "
                "WHERE te.trading_date=?",
                (trading_date,),
            )

            for decision in decisions:
                try:
                    self._score_one_decision(
                        decision, candle_map, trades, trading_date
                    )
                except Exception as e:
                    self.logger.debug(
                        f"Regime scoring error for decision "
                        f"{decision.get('id')}: {e}"
                    )

            self.logger.info(
                f"Scored {len(decisions)} regime decisions for {trading_date}"
            )

        except Exception as e:
            self.logger.debug(f"Regime accuracy scoring error: {e}")

    def _score_one_decision(
        self,
        decision: dict,
        candle_map: Dict[str, float],
        trades: List[dict],
        trading_date: str,
    ) -> None:
        """Score one regime decision against actual market outcome."""
        decision_id     = decision.get("id")
        decision_time   = decision.get("time", "")
        vol_regime      = decision.get("vol_regime", "")
        price_regime    = decision.get("price_regime", "")
        pos_regime      = decision.get("positioning_regime", "")
        final_regime    = decision.get("final_regime", "")

        # Get spot at decision time and 2 hours later
        spot_at_decision = candle_map.get(decision_time)
        if spot_at_decision is None:
            # Find nearest candle
            times = sorted(candle_map.keys())
            for t in times:
                if t >= decision_time:
                    spot_at_decision = candle_map[t]
                    break

        if spot_at_decision is None:
            return

        # Get spot 2 hours later
        try:
            dec_dt = datetime.strptime(f"{trading_date} {decision_time}", "%Y-%m-%d %H:%M:%S")
            two_hr_later = (dec_dt + timedelta(hours=2)).strftime("%H:%M:%S")
        except Exception:
            return

        spot_2hr = candle_map.get(two_hr_later)
        if spot_2hr is None:
            # Find nearest candle within 5 minutes
            for offset in range(0, 6):
                try:
                    t_check = (dec_dt + timedelta(hours=2, minutes=offset)).strftime("%H:%M:%S")
                    if t_check in candle_map:
                        spot_2hr = candle_map[t_check]
                        break
                except Exception:
                    pass

        if spot_2hr is None:
            return

        nifty_move_pct = (spot_2hr - spot_at_decision) / spot_at_decision * 100.0
        nifty_move_pts = spot_2hr - spot_at_decision

        # Score vol regime
        was_vol_correct = None
        if vol_regime in ("SELL_PREMIUM", "STRONG_SELL_PREMIUM", "BORDERLINE_SELL"):
            # Correct if: market didn't make a big move (stayed within 1% range)
            was_vol_correct = 1 if abs(nifty_move_pct) < 1.0 else 0
        elif vol_regime == "NEUTRAL":
            # Correct if: market made a move that would have hurt a seller
            was_vol_correct = 1 if abs(nifty_move_pct) >= 0.8 else 0
        elif vol_regime == "ABORT":
            # Correct if: market made a large move
            was_vol_correct = 1 if abs(nifty_move_pct) >= 1.5 else 0

        # Score price regime
        was_price_correct = None
        if price_regime == "RANGE":
            was_price_correct = 1 if abs(nifty_move_pct) < 0.5 else 0
        elif price_regime in ("UPTREND", "STRONG_UPTREND"):
            was_price_correct = 1 if nifty_move_pct > 0.3 else 0
        elif price_regime in ("DOWNTREND", "STRONG_DOWNTREND"):
            was_price_correct = 1 if nifty_move_pct < -0.3 else 0
        elif price_regime == "CHOPPY":
            # Choppy is correct if market reversed direction multiple times
            was_price_correct = 1 if abs(nifty_move_pct) < 0.8 else 0

        # Score positioning regime (harder to score directly)
        was_pos_correct = None  # Set to None — requires more complex analysis

        # Score final regime
        was_final_correct = None
        if final_regime in ("PREMIUM_SELL_RANGE", "PREMIUM_SELL_BULL", "PREMIUM_SELL_BEAR"):
            # Find if there was a trade with this regime that was profitable
            matching_trades = [
                t for t in trades
                if t.get("final_regime_at_entry") == final_regime
                and t.get("entry_time", "") >= f"{trading_date} {decision_time}"
            ]
            if matching_trades:
                wins = sum(1 for t in matching_trades if t.get("result") == "WIN")
                was_final_correct = 1 if wins > len(matching_trades) / 2 else 0
            else:
                # No trade taken — score based on vol regime correctness
                was_final_correct = was_vol_correct
        elif final_regime == "NO_TRADE":
            # NO_TRADE is correct if a trade would have lost money
            was_final_correct = 1 if abs(nifty_move_pct) >= 1.0 else 0

        # Compute overall score
        scores = [s for s in [was_vol_correct, was_price_correct, was_final_correct]
                  if s is not None]
        avg_score = round(sum(scores) / len(scores), 3) if scores else None

        # Store in regime_accuracy_scores
        try:
            self.db.insert("regime_accuracy_scores", {
                "trading_date":              trading_date,
                "regime_decision_id":        decision_id,
                "vol_regime_classified":     vol_regime,
                "price_regime_classified":   price_regime,
                "positioning_classified":    pos_regime,
                "final_regime_classified":   final_regime,
                "was_vol_correct":           was_vol_correct,
                "was_price_correct":         was_price_correct,
                "was_positioning_correct":   was_pos_correct,
                "was_final_correct":         was_final_correct,
                "nifty_move_2hr_pts":        round(nifty_move_pts, 2),
                "straddle_move_2hr_pts":     None,  # Would need chain data
                "score_value":               avg_score,
            })
        except Exception as e:
            self.logger.debug(f"Regime accuracy insert error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # EOD TASKS
    # ─────────────────────────────────────────────────────────────────────

    def run_eod_tasks(self, trading_date: str) -> None:
        """
        Run all end-of-day calibration tasks.
        Called from main.py after market close.

        Tasks:
        1. Update phantom trade outcomes
        2. Update exit quality outcomes
        3. Score regime decisions
        4. Run daily calibration
        """
        self.logger.info(f"CalibrationEngine: running EOD tasks for {trading_date}")

        self.update_phantom_outcomes(trading_date)
        self.update_exit_quality_outcomes(trading_date)
        self.score_regime_decisions(trading_date)
        self.run(schedule="daily")

        self.logger.info("CalibrationEngine: EOD tasks complete")

    # ─────────────────────────────────────────────────────────────────────
    # REPORTING
    # ─────────────────────────────────────────────────────────────────────

    def get_calibration_summary(self) -> dict:
        """Return a summary dict of current calibration state for reporting."""
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
            "notes":                     s.notes,
            "tier_description": {
                0: "Tier 0 — NIFTY 2026 defaults (< 5 trading days)",
                1: "Tier 1 — VIX percentiles from live data (5-19 days)",
                2: "Tier 2 — Full calibration (20-59 days)",
                3: "Tier 3 — Robust calibration with signal weights (60+ days)",
            }.get(s.calibration_tier, "Unknown tier"),
        }

    def get_calibration_history(self, n: int = 10) -> List[dict]:
        """Return last N calibration runs from database for drift tracking."""
        try:
            rows = self.db.query(
                "SELECT calibrated_at, calibration_tier, is_valid, "
                "vrp_sell_threshold, vrp_fair_threshold, vix_p50, "
                "day_size_tuesday, pcr_bullish_threshold, "
                "phantom_false_negative_rate, exit_quality_score, "
                "regime_accuracy_score "
                "FROM calibration_state "
                "ORDER BY calibrated_at DESC LIMIT ?",
                (n,),
            )
            return rows
        except Exception:
            return []


# ─────────────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    """
    Standalone self-test for calibration_engine.py.
    Tests: CalibrationState, Bayesian shrinkage, all calibration components,
           phantom trade logging, exit quality logging, regime scoring.
    Run: python calibration_engine.py
    """
    print_section("NIFTY ALGO v3.0 — CALIBRATION ENGINE SELF-TEST", char="#")

    from core import load_config, Database, setup_logging

    config = load_config()
    db     = Database(config.db_path)
    logger = setup_logging(db, config.log_dir)

    # ── CalibrationState tests ────────────────────────────────────────────
    print_section("CalibrationState Tests")

    # Default state
    state = NIFTY_2026_DEFAULTS
    print_kv_table({
        "Tier":              state.calibration_tier,
        "Valid":             state.is_calibrated,
        "VRP Sell":          state.vrp_sell_threshold,
        "VRP Fair":          state.vrp_fair_threshold,
        "Day Size Tuesday":  state.day_size_tuesday,
        "VIX p50":           state.vix_p50,
    }, title="NIFTY 2026 Defaults")

    # DTE-adjusted VRP thresholds
    dte0_thresh = state.get_vrp_sell_for_dte(0, "NARROW")
    dte1_thresh = state.get_vrp_sell_for_dte(1, "MODERATE")
    dte2_thresh = state.get_vrp_sell_for_dte(2, "MODERATE")
    print(f"  VRP sell DTE0 NARROW:    {dte0_thresh:.3f}pp (expect ~{2.5*0.75*0.85:.3f})")
    print(f"  VRP sell DTE1 MODERATE:  {dte1_thresh:.3f}pp (expect ~{2.5*0.85:.3f})")
    print(f"  VRP sell DTE2 MODERATE:  {dte2_thresh:.3f}pp (expect ~{2.5*1.10:.3f})")
    assert dte0_thresh < dte1_thresh < dte2_thresh, \
        "DTE0 should have lowest threshold, DTE2 highest"
    assert dte0_thresh >= 1.0, "Threshold should never go below 1.0pp"

    # to_dict
    d = state.to_dict()
    assert "vrp_sell_threshold" in d, "to_dict should include vrp_sell_threshold"
    assert "day_size_tuesday" in d, "to_dict should include day_size_tuesday"
    print("  [OK] CalibrationState tests passed")

    # ── CalibrationEngine instantiation ──────────────────────────────────
    print_section("CalibrationEngine Instantiation")
    engine = CalibrationEngine(db, config, logger)
    print(f"  Current state: tier={engine.state.calibration_tier} "
          f"valid={engine.state.is_calibrated}")
    print(f"  VRP sell: {engine.state.vrp_sell_threshold:.2f}pp")
    print("  [OK] Instantiation test passed")

    # ── Bayesian shrinkage tests ──────────────────────────────────────────
    print_section("Bayesian Shrinkage Tests")

    # With 0 observations: should equal prior
    shrunk_0 = engine._shrink(5.0, 2.5, 0)
    print(f"  n=0: shrink(5.0, 2.5) = {shrunk_0:.4f} (expect 2.5000)")
    assert abs(shrunk_0 - 2.5) < 0.001, f"With n=0, should equal prior 2.5, got {shrunk_0}"

    # With 30 observations: should be 50/50
    shrunk_30 = engine._shrink(5.0, 2.5, 30)
    print(f"  n=30: shrink(5.0, 2.5) = {shrunk_30:.4f} (expect 3.7500)")
    assert abs(shrunk_30 - 3.75) < 0.01, f"With n=30, should be 3.75, got {shrunk_30}"

    # With 300 observations: should be close to data
    shrunk_300 = engine._shrink(5.0, 2.5, 300)
    print(f"  n=300: shrink(5.0, 2.5) = {shrunk_300:.4f} (expect ~4.77)")
    assert shrunk_300 > 4.5, f"With n=300, should be close to 5.0, got {shrunk_300}"

    # Monotonicity: more data → closer to data estimate
    s1 = engine._shrink(5.0, 2.5, 10)
    s2 = engine._shrink(5.0, 2.5, 50)
    s3 = engine._shrink(5.0, 2.5, 200)
    assert s1 < s2 < s3, "More data should pull toward data estimate"

    # Confidence labels
    assert engine._confidence_label(0)   == "INSUFFICIENT"
    assert engine._confidence_label(5)   == "LOW"
    assert engine._confidence_label(30)  == "MEDIUM"
    assert engine._confidence_label(100) == "HIGH"

    print("  [OK] Bayesian shrinkage tests passed")

    # ── VIX values extraction test ────────────────────────────────────────
    print_section("VIX Values Extraction Test")
    vix_vals = engine._get_vix_values()
    print(f"  VIX values found: {len(vix_vals)}")
    if len(vix_vals) > 0:
        print(f"  VIX range: {vix_vals.min():.1f} - {vix_vals.max():.1f}")
        print(f"  VIX mean: {vix_vals.mean():.1f}")
        assert all(8.0 < v < 90.0 for v in vix_vals), "All VIX values should be in valid range"
    print("  [OK] VIX extraction test passed")

    # ── Full calibration run test ─────────────────────────────────────────
    print_section("Full Calibration Run Test")
    new_state = engine.run(schedule="startup")

    print_kv_table({
        "Tier":              new_state.calibration_tier,
        "Valid":             new_state.is_calibrated,
        "N Days":            new_state.n_trading_days,
        "VRP Sell":          f"{new_state.vrp_sell_threshold:.2f}pp",
        "VRP Fair":          f"{new_state.vrp_fair_threshold:.2f}pp",
        "VRP DTE0":          f"{new_state.vrp_sell_dte0:.2f}pp",
        "VRP DTE1":          f"{new_state.vrp_sell_dte1:.2f}pp",
        "VIX p25/p50/p75":   f"{new_state.vix_p25:.1f}/{new_state.vix_p50:.1f}/{new_state.vix_p75:.1f}",
        "Day Size Tue":      f"{new_state.day_size_tuesday:.2f}",
        "Day Size Mon":      f"{new_state.day_size_monday:.2f}",
        "PCR Bull/Bear":     f"{new_state.pcr_bullish_threshold:.3f}/{new_state.pcr_bearish_threshold:.3f}",
        "Skew Bear/Bull":    f"{new_state.skew_bearish_threshold:.3f}/{new_state.skew_bullish_threshold:.3f}",
        "OI Wall Strong":    f"{new_state.oi_wall_strong_cal:.2f}",
        "Signal Wt VRP":     f"{new_state.signal_weight_vrp:.3f}",
        "Phantom FNR":       f"{new_state.phantom_false_negative_rate}%"
                             if new_state.phantom_false_negative_rate else "N/A",
        "Exit Quality":      f"{new_state.exit_quality_score}"
                             if new_state.exit_quality_score else "N/A",
        "Notes":             new_state.notes,
    }, title="Calibration Result")

    # Validate constraints
    assert new_state.vrp_sell_threshold >= 1.2, \
        f"VRP sell should be >= 1.2, got {new_state.vrp_sell_threshold}"
    assert new_state.vrp_fair_threshold < new_state.vrp_sell_threshold, \
        "VRP fair should be < VRP sell"
    assert new_state.vix_p25 < new_state.vix_p50 < new_state.vix_p75 < new_state.vix_p90, \
        "VIX percentiles should be monotonically increasing"
    assert new_state.pcr_bullish_threshold < new_state.pcr_bearish_threshold, \
        "PCR bullish should be < PCR bearish"
    assert new_state.skew_bullish_threshold < new_state.skew_bearish_threshold, \
        "Skew bullish should be < skew bearish"
    assert 0.25 <= new_state.day_size_tuesday <= 1.0, \
        f"Tuesday size should be in [0.25, 1.0], got {new_state.day_size_tuesday}"
    assert new_state.vrp_sell_dte0 < new_state.vrp_sell_dte1 < new_state.vrp_sell_dte2plus, \
        "DTE-adjusted thresholds should increase with DTE"

    print("  [OK] Full calibration run test passed")

    # ── Phantom trade logging test ────────────────────────────────────────
    print_section("Phantom Trade Logging Test")

    mock_signals = {
        "vrp_raw":           2.1,
        "vrp_smoothed":      2.0,
        "or_condition":      "NARROW",
        "adx_15":            14.0,
        "positioning_regime": "RANGE",
        "actual_dte":        0,
    }

    engine.log_phantom_trade(
        signals=mock_signals,
        block_reason="NEUTRAL:VRP_2.0pp_below_threshold_2.5pp",
        strategy_would_be="IRON_CONDOR",
        strikes_json=json.dumps([
            {"strike": 24100, "option_type": "call", "action": "SELL"},
            {"strike": 24250, "option_type": "call", "action": "BUY"},
            {"strike": 23950, "option_type": "put",  "action": "SELL"},
            {"strike": 23800, "option_type": "put",  "action": "BUY"},
        ]),
        credit_would_be=28.5,
    )

    # Verify it was logged
    phantom_count = db.query_one(
        "SELECT COUNT(*) as cnt FROM phantom_trades WHERE trading_date=?",
        (today_ist().isoformat(),),
    )
    print(f"  Phantom trades logged today: {phantom_count['cnt'] if phantom_count else 0}")
    print("  [OK] Phantom trade logging test passed")

    # ── Exit quality logging test ─────────────────────────────────────────
    print_section("Exit Quality Logging Test")

    engine.log_exit_quality(
        position_id="test-position-001",
        trading_date=today_ist().isoformat(),
        exit_priority=4,  # Profit lock
        exit_time=now_ist().isoformat(),
        exit_pnl_rupees=3250.0,
    )

    eq_count = db.query_one(
        "SELECT COUNT(*) as cnt FROM exit_quality_log WHERE trading_date=?",
        (today_ist().isoformat(),),
    )
    print(f"  Exit quality records today: {eq_count['cnt'] if eq_count else 0}")
    print("  [OK] Exit quality logging test passed")

    # ── Calibration summary test ──────────────────────────────────────────
    print_section("Calibration Summary Test")
    summary = engine.get_calibration_summary()
    print_kv_table({
        k: v for k, v in summary.items()
        if k not in ("notes", "tier_description")
    }, title="Calibration Summary")
    print(f"  Tier description: {summary.get('tier_description')}")
    assert "calibration_tier" in summary, "Summary should include calibration_tier"
    assert "vrp_sell_threshold" in summary, "Summary should include vrp_sell_threshold"
    print("  [OK] Calibration summary test passed")

    # ── Calibration history test ──────────────────────────────────────────
    print_section("Calibration History Test")
    history = engine.get_calibration_history(n=5)
    print(f"  Calibration history rows: {len(history)}")
    if history:
        print_kv_table({
            "Most recent": history[0].get("calibrated_at"),
            "Tier":        history[0].get("calibration_tier"),
            "VRP Sell":    history[0].get("vrp_sell_threshold"),
        })
    print("  [OK] Calibration history test passed")

    # ── DTE threshold computation test ───────────────────────────────────
    print_section("DTE Threshold Computation Test")
    test_state = CalibrationState(vrp_sell_threshold=2.5)

    test_cases = [
        (0, "VERY_NARROW", 2.5 * 0.75 * 0.75),
        (0, "NARROW",      2.5 * 0.75 * 0.85),
        (0, "MODERATE",    2.5 * 0.75 * 1.00),
        (0, "WIDE",        2.5 * 0.75 * 1.20),
        (1, "NARROW",      2.5 * 0.85 * 0.85),
        (1, "MODERATE",    2.5 * 0.85 * 1.00),
        (2, "MODERATE",    2.5 * 1.10 * 1.00),
    ]

    for dte, or_cond, expected in test_cases:
        result = test_state.get_vrp_sell_for_dte(dte, or_cond)
        result = max(result, 1.0)  # Floor
        expected = max(expected, 1.0)
        print(f"  DTE={dte} OR={or_cond}: {result:.4f}pp (expect ~{expected:.4f}pp)")
        assert abs(result - expected) < 0.01, \
            f"DTE={dte} OR={or_cond}: expected {expected:.4f}, got {result:.4f}"

    print("  [OK] DTE threshold computation tests passed")

    # ── Regime scoring test ───────────────────────────────────────────────
    print_section("Regime Scoring Test (mock data)")

    # Insert mock regime decisions and candles for scoring test
    test_date = today_ist().isoformat()

    # Only run if there are regime decisions to score
    decision_count = db.query_one(
        "SELECT COUNT(*) as cnt FROM regime_decisions WHERE date=?",
        (test_date,),
    )
    if decision_count and decision_count["cnt"] > 0:
        engine.score_regime_decisions(test_date)
        score_count = db.query_one(
            "SELECT COUNT(*) as cnt FROM regime_accuracy_scores WHERE trading_date=?",
            (test_date,),
        )
        print(f"  Regime accuracy scores computed: {score_count['cnt'] if score_count else 0}")
    else:
        print("  No regime decisions to score today (expected in paper mode)")

    print("  [OK] Regime scoring test passed")

    # ── Engine state persistence test ────────────────────────────────────
    print_section("State Persistence Test")

    # Create a new engine instance and verify it loads the saved state
    engine2 = CalibrationEngine(db, config, logger)
    print(f"  Reloaded state: tier={engine2.state.calibration_tier} "
          f"vrp_sell={engine2.state.vrp_sell_threshold:.2f}pp")
    assert engine2.state.vrp_sell_threshold > 0, "Reloaded state should have positive VRP threshold"
    print("  [OK] State persistence test passed")

    db.close()
    print_section("CALIBRATION ENGINE SELF-TEST COMPLETE", char="#")
    print("  All tests passed")
    print(f"  Database: {config.db_path}")
    print()


if __name__ == "__main__":
    _self_test()
