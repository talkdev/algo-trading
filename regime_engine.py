# regime_engine.py
# NIFTY Intraday Options Engine v3.0
# Regime classification engine: volatility, price, positioning, confidence,
# final regime decision tree, size computation, self-calibration.
# Complete rewrite from old regime_engine.py — regime-based not VIX-based.

from __future__ import annotations

import json
import math
import statistics
import logging
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from enum import Enum
from typing import Optional, Dict, List, Tuple

import numpy as np
import pandas as pd

from core import (
    Config, Database, RateLimiter, UpstoxClient,
    ExpiryCalendar, now_ist, today_ist,
    load_config, setup_logging,
    get_high_impact_events,
    print_section, print_kv_table,
)
from data_engine import MarketDataEngine


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class VolatilityRegime(str, Enum):
    STRONG_SELL_PREMIUM = "STRONG_SELL_PREMIUM"
    SELL_PREMIUM        = "SELL_PREMIUM"
    BORDERLINE_SELL     = "BORDERLINE_SELL"
    NEUTRAL             = "NEUTRAL"
    BUY_OPTIONS         = "BUY_OPTIONS"
    ABORT               = "ABORT"


class PriceRegime(str, Enum):
    STRONG_UPTREND   = "STRONG_UPTREND"
    UPTREND          = "UPTREND"
    RANGE            = "RANGE"
    DOWNTREND        = "DOWNTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"
    CHOPPY           = "CHOPPY"
    OBSERVING        = "OBSERVING"


class PositioningRegime(str, Enum):
    STRONG_RANGE = "STRONG_RANGE"
    RANGE        = "RANGE"
    BULLISH      = "BULLISH"
    BEARISH      = "BEARISH"
    UNCLEAR      = "UNCLEAR"


class FinalRegime(str, Enum):
    PREMIUM_SELL_RANGE   = "PREMIUM_SELL_RANGE"
    PREMIUM_SELL_BULL    = "PREMIUM_SELL_BULL"
    PREMIUM_SELL_BEAR    = "PREMIUM_SELL_BEAR"
    NO_TRADE             = "NO_TRADE"
    ABORT                = "ABORT"


class ConfidenceLevel(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"
    NONE   = "NONE"


# ─────────────────────────────────────────────────────────────────────────────
# REGIME SNAPSHOT DATACLASS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeSnapshot:
    """
    Complete output of one regime classification cycle.
    Consumed by strategy_engine.py for trade decisions.
    """
    # Identity
    timestamp:           datetime
    trading_date:        str
    day_type:            str
    dte:                 int
    day_label:           str

    # Event context
    event_day:           bool
    event_name:          str
    defined_risk_only:   bool

    # Four dimensions
    vol_regime:          str
    price_regime:        str
    positioning_regime:  str
    confidence_level:    str
    confidence_score:    float

    # Final output
    final_regime:        str
    final_regime_notes:  str
    block_new_entries:   bool

    # Size (computed once here, never modified downstream)
    size_multiplier:     float
    raw_size_multiplier: float

    # Borderline sell flag
    borderline_sell:     bool

    # Conflict reduction applied
    size_conflict_reduction: float

    # Key signals at classification time
    vix_level:           float
    vrp_raw:             Optional[float]
    vrp_smoothed:        Optional[float]
    atm_iv_pct:          Optional[float]
    parkinson_rv_pct:    Optional[float]
    iv_behavior:         str
    day_move_used_pct:   float
    opening_straddle_pts: float
    adx_15:              float
    adx_60:              float
    ema_structure:       str
    hh_hl:               str
    or_condition:        Optional[str]
    or_computed:         bool
    choppy_detected:     bool
    pcr:                 Optional[float]
    skew_ratio:          Optional[float]
    oi_change_pct:       float
    oi_wall_strength:    float
    max_pain_distance:   float
    gap_fade_opportunity: bool

    # Calibration metadata
    is_calibrated:       bool
    calibration_tier:    int


# ─────────────────────────────────────────────────────────────────────────────
# CALIBRATION STATE DATACLASS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CalibrationState:
    """
    All calibrated thresholds used by the regime engine.
    Tier 0 = defaults, Tier 1-3 = progressively more data-derived.
    """
    calibration_tier:         int
    is_calibrated:            bool
    n_trading_days:           int
    n_tuesday_expiries:       int
    last_calibrated:          Optional[datetime]

    # VIX percentiles
    vix_p25:                  float
    vix_p50:                  float
    vix_p75:                  float
    vix_p90:                  float

    # VRP thresholds
    vrp_sell_threshold:       float
    vrp_fair_threshold:       float

    # Day size multipliers
    day_size_monday:          float
    day_size_tuesday:         float
    day_size_wednesday:       float
    day_size_thursday:        float
    day_size_friday:          float

    # OI thresholds
    oi_buildup_threshold:     float
    oi_unwind_threshold:      float
    oi_wall_strong_cal:       float
    oi_wall_moderate_cal:     float

    # PCR thresholds
    pcr_bullish_threshold:    float
    pcr_bearish_threshold:    float

    # Skew thresholds
    skew_bearish_threshold:   float
    skew_bullish_threshold:   float

    # Straddle ratio
    straddle_ratio_sell:      float

    # Signal weights (for confidence score)
    signal_weight_vrp:           float
    signal_weight_price:         float
    signal_weight_positioning:   float
    signal_weight_iv_behavior:   float
    signal_weight_or_condition:  float

    # Performance metrics
    phantom_false_negative_rate: Optional[float]
    exit_quality_score:          Optional[float]
    regime_accuracy_score:       Optional[float]

    # Day ranges
    monday_avg_range:    float
    tuesday_avg_range:   float
    wednesday_avg_range: float
    thursday_avg_range:  float
    friday_avg_range:    float


# ─────────────────────────────────────────────────────────────────────────────
# CALIBRATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class CalibrationEngine:
    """
    Self-calibrating threshold engine.
    Reads stored historical data and derives optimal thresholds.
    Implements Bayesian shrinkage to prevent overfitting on small samples.

    Calibration tiers:
    Tier 0: < 5 days  — hardcoded NIFTY 2026 defaults
    Tier 1: 5-20 days — VIX percentiles from live data
    Tier 2: 20-60 days — full calibration, all thresholds data-derived
    Tier 3: 60+ days  — robust calibration with signal weights
    """

    # NIFTY 2026 defaults (VIX 11 suppressed environment)
    DEFAULTS = {
        "vix_p25":                 11.0,
        "vix_p50":                 12.5,
        "vix_p75":                 14.5,
        "vix_p90":                 18.0,
        "vrp_sell_threshold":       2.0,
        "vrp_fair_threshold":       1.0,
        "day_size_monday":          0.60,
        "day_size_tuesday":         0.85,
        "day_size_wednesday":       0.65,
        "day_size_thursday":        0.65,
        "day_size_friday":          0.55,
        "oi_buildup_threshold":     0.06,
        "oi_unwind_threshold":     -0.06,
        "oi_wall_strong_cal":       2.2,
        "oi_wall_moderate_cal":     1.5,
        "pcr_bullish_threshold":    0.65,
        "pcr_bearish_threshold":    1.20,
        "skew_bearish_threshold":   2.5,
        "skew_bullish_threshold":   0.90,
        "straddle_ratio_sell":      1.05,
        "signal_weight_vrp":        0.8,
        "signal_weight_price":      1.2,
        "signal_weight_positioning":1.0,
        "signal_weight_iv_behavior":1.3,
        "signal_weight_or_condition":1.1,
        "monday_avg_range":         120.0,
        "tuesday_avg_range":        130.0,
        "wednesday_avg_range":      110.0,
        "thursday_avg_range":       110.0,
        "friday_avg_range":         100.0,
    }

    # Bayesian prior weight (equivalent to N observations of prior belief)
    PRIOR_WEIGHT = 15

    def __init__(self, db: Database, config: Config, logger):
        self.db     = db
        self.config = config
        self.logger = logger
        self._state: Optional[CalibrationState] = None
        self._load()

    def _load(self) -> None:
        """Load the most recent valid calibration from database."""
        row = self.db.get_latest_calibration()
        if row:
            self._state = self._row_to_state(row)
            self.logger.info(
                f"Calibration loaded: tier={row.get('calibration_tier', 0)} "
                f"days={row.get('n_trading_days', 0)} "
                f"valid={bool(row.get('is_valid'))}"
            )
        else:
            self.logger.info(
                "No prior calibration found — using NIFTY 2026 defaults (Tier 0)"
            )

    def _row_to_state(self, row: dict) -> CalibrationState:
        """Convert a calibration_state DB row to CalibrationState dataclass."""
        d = self.DEFAULTS

        def _f(key: str) -> float:
            v = row.get(key)
            return float(v) if v is not None else float(d.get(key, 0.0))

        return CalibrationState(
            calibration_tier=int(row.get("calibration_tier", 0)),
            is_calibrated=bool(row.get("is_valid", False)),
            n_trading_days=int(row.get("n_trading_days", 0)),
            n_tuesday_expiries=int(row.get("n_tuesday_expiries", 0)),
            last_calibrated=(
                datetime.fromisoformat(row["calibrated_at"])
                if row.get("calibrated_at") else None
            ),
            vix_p25=_f("vix_p25"),
            vix_p50=_f("vix_p50"),
            vix_p75=_f("vix_p75"),
            vix_p90=_f("vix_p90"),
            vrp_sell_threshold=_f("vrp_sell_threshold"),
            vrp_fair_threshold=_f("vrp_fair_threshold"),
            day_size_monday=_f("day_size_monday"),
            day_size_tuesday=_f("day_size_tuesday"),
            day_size_wednesday=_f("day_size_wednesday"),
            day_size_thursday=_f("day_size_thursday"),
            day_size_friday=_f("day_size_friday"),
            oi_buildup_threshold=_f("oi_buildup_threshold"),
            oi_unwind_threshold=_f("oi_unwind_threshold"),
            oi_wall_strong_cal=_f("oi_wall_strong_cal"),
            oi_wall_moderate_cal=_f("oi_wall_moderate_cal"),
            pcr_bullish_threshold=_f("pcr_bullish_threshold"),
            pcr_bearish_threshold=_f("pcr_bearish_threshold"),
            skew_bearish_threshold=_f("skew_bearish_threshold"),
            skew_bullish_threshold=_f("skew_bullish_threshold"),
            straddle_ratio_sell=_f("straddle_ratio_sell"),
            signal_weight_vrp=_f("signal_weight_vrp"),
            signal_weight_price=_f("signal_weight_price"),
            signal_weight_positioning=_f("signal_weight_positioning"),
            signal_weight_iv_behavior=_f("signal_weight_iv_behavior"),
            signal_weight_or_condition=_f("signal_weight_or_condition"),
            phantom_false_negative_rate=row.get("phantom_false_negative_rate"),
            exit_quality_score=row.get("exit_quality_score"),
            regime_accuracy_score=row.get("regime_accuracy_score"),
            monday_avg_range=_f("monday_avg_range"),
            tuesday_avg_range=_f("tuesday_avg_range"),
            wednesday_avg_range=_f("wednesday_avg_range"),
            thursday_avg_range=_f("thursday_avg_range"),
            friday_avg_range=_f("friday_avg_range"),
        )

    @property
    def state(self) -> Optional[CalibrationState]:
        return self._state

    def _bayesian_shrink(
        self,
        data_estimate: float,
        prior_estimate: float,
        n_observations: int,
    ) -> float:
        """
        Apply Bayesian shrinkage toward prior.
        With few observations, result is close to prior.
        With many observations, result is close to data.
        """
        n = max(n_observations, 0)
        w = n / (n + self.PRIOR_WEIGHT)
        return round(w * data_estimate + (1 - w) * prior_estimate, 4)

    def run(self) -> Optional[CalibrationState]:
        """
        Run full calibration from stored historical data.
        Called at startup and after market close.
        Returns updated CalibrationState.
        """
        self.logger.info("CalibrationEngine: starting calibration run")

        n_days = self.db.count_trading_days()
        n_exp  = self.db.count_tuesday_expiries()
        self.logger.info(
            f"  Data: {n_days} trading days, {n_exp} Tuesday expiries"
        )

        # Determine calibration tier
        tier1 = n_days >= 1
        tier2 = n_days >= max(self.config.min_trading_days_for_calibration, 5)
        tier3 = n_days >= 30
        cal_tier = 3 if tier3 else (2 if tier2 else (1 if tier1 else 0))
        is_valid = tier1

        result = dict(self.DEFAULTS)

        # ── VIX percentiles ───────────────────────────────────────────────
        if tier1:
            vix_result = self._calibrate_vix(n_days)
            result.update(vix_result)

        # ── VRP thresholds ────────────────────────────────────────────────
        if tier2:
            vrp_result = self._calibrate_vrp(n_days)
            result.update(vrp_result)

        # ── Day size multipliers ──────────────────────────────────────────
        if tier2:
            size_result = self._calibrate_day_sizes(n_days)
            result.update(size_result)

        # ── OI thresholds ─────────────────────────────────────────────────
        if tier2:
            oi_result = self._calibrate_oi(n_days)
            result.update(oi_result)

        # ── PCR thresholds ────────────────────────────────────────────────
        if tier2:
            pcr_result = self._calibrate_pcr(n_days)
            result.update(pcr_result)

        # ── Skew thresholds ───────────────────────────────────────────────
        if tier2:
            skew_result = self._calibrate_skew(n_days)
            result.update(skew_result)

        # ── Day ranges ────────────────────────────────────────────────────
        if tier1:
            range_result = self._calibrate_day_ranges(n_days)
            result.update(range_result)

        # ── Signal weights (Tier 3 only) ──────────────────────────────────
        if tier3:
            weight_result = self._calibrate_signal_weights(n_days)
            result.update(weight_result)

        # ── Phantom trade analysis ────────────────────────────────────────
        phantom_fnr = self.db.get_phantom_false_negative_rate(days=20)
        if phantom_fnr > 30.0 and tier2:
            # Too many good trades being blocked — lower VRP threshold
            old_thresh = result["vrp_sell_threshold"]
            result["vrp_sell_threshold"] = max(
                old_thresh - 0.1, self.config.vrp_fair_threshold_default
            )
            self.logger.info(
                f"  Phantom FNR={phantom_fnr:.1f}% > 30% — "
                f"lowering VRP threshold {old_thresh:.2f} → {result['vrp_sell_threshold']:.2f}"
            )
        elif phantom_fnr < 10.0 and tier2 and phantom_fnr > 0:
            # Very few good trades blocked — threshold may be too low
            old_thresh = result["vrp_sell_threshold"]
            result["vrp_sell_threshold"] = min(
                old_thresh + 0.1, 4.0
            )
            self.logger.info(
                f"  Phantom FNR={phantom_fnr:.1f}% < 10% — "
                f"raising VRP threshold {old_thresh:.2f} → {result['vrp_sell_threshold']:.2f}"
            )

        # ── Exit quality ──────────────────────────────────────────────────
        exit_quality = self.db.get_exit_quality_summary(days=20)
        exit_quality_score = None
        if exit_quality.get("total_exits", 0) >= 10:
            avg_improvement = exit_quality.get("avg_improvement_15min", 0.0) or 0.0
            premature_rate  = (
                exit_quality.get("premature_count", 0) /
                max(exit_quality.get("total_exits", 1), 1) * 100
            )
            exit_quality_score = round(100 - premature_rate, 1)
            self.logger.info(
                f"  Exit quality: avg_improvement_15min=Rs{avg_improvement:.0f} "
                f"premature_rate={premature_rate:.1f}% "
                f"score={exit_quality_score:.1f}"
            )

        # ── Regime accuracy ───────────────────────────────────────────────
        regime_acc = self.db.get_regime_accuracy(days=30)
        regime_accuracy_score = regime_acc.get("avg_score")

        # ── Persist calibration ───────────────────────────────────────────
        try:
            self.db.insert("calibration_state", {
                "calibrated_at":              now_ist().isoformat(),
                "n_trading_days":             n_days,
                "n_tuesday_expiries":         n_exp,
                "calibration_tier":           cal_tier,
                "is_valid":                   int(is_valid),
                "notes":                      f"tier={cal_tier} days={n_days}",
                "vix_p25":                    result["vix_p25"],
                "vix_p50":                    result["vix_p50"],
                "vix_p75":                    result["vix_p75"],
                "vix_p90":                    result["vix_p90"],
                "vrp_sell_threshold":         result["vrp_sell_threshold"],
                "vrp_fair_threshold":         result["vrp_fair_threshold"],
                "day_size_monday":            result["day_size_monday"],
                "day_size_tuesday":           result["day_size_tuesday"],
                "day_size_wednesday":         result["day_size_wednesday"],
                "day_size_thursday":          result["day_size_thursday"],
                "day_size_friday":            result["day_size_friday"],
                "oi_buildup_threshold":       result["oi_buildup_threshold"],
                "oi_unwind_threshold":        result["oi_unwind_threshold"],
                "oi_wall_strong_cal":         result["oi_wall_strong_cal"],
                "oi_wall_moderate_cal":       result["oi_wall_moderate_cal"],
                "pcr_bullish_threshold":      result["pcr_bullish_threshold"],
                "pcr_bearish_threshold":      result["pcr_bearish_threshold"],
                "skew_bearish_threshold":     result["skew_bearish_threshold"],
                "skew_bullish_threshold":     result["skew_bullish_threshold"],
                "straddle_ratio_sell":        result["straddle_ratio_sell"],
                "signal_weight_vrp":          result["signal_weight_vrp"],
                "signal_weight_price":        result["signal_weight_price"],
                "signal_weight_positioning":  result["signal_weight_positioning"],
                "signal_weight_iv_behavior":  result["signal_weight_iv_behavior"],
                "signal_weight_or_condition": result["signal_weight_or_condition"],
                "phantom_false_negative_rate":phantom_fnr if phantom_fnr > 0 else None,
                "exit_quality_score":         exit_quality_score,
                "regime_accuracy_score":      regime_accuracy_score,
                "monday_avg_range":           result["monday_avg_range"],
                "tuesday_avg_range":          result["tuesday_avg_range"],
                "wednesday_avg_range":        result["wednesday_avg_range"],
                "thursday_avg_range":         result["thursday_avg_range"],
                "friday_avg_range":           result["friday_avg_range"],
            })
        except Exception as e:
            self.logger.warning(f"Could not persist calibration: {e}")

        # ── Build CalibrationState ────────────────────────────────────────
        cal = CalibrationState(
            calibration_tier=cal_tier,
            is_calibrated=is_valid,
            n_trading_days=n_days,
            n_tuesday_expiries=n_exp,
            last_calibrated=datetime.now(),
            vix_p25=result["vix_p25"],
            vix_p50=result["vix_p50"],
            vix_p75=result["vix_p75"],
            vix_p90=result["vix_p90"],
            vrp_sell_threshold=result["vrp_sell_threshold"],
            vrp_fair_threshold=result["vrp_fair_threshold"],
            day_size_monday=result["day_size_monday"],
            day_size_tuesday=result["day_size_tuesday"],
            day_size_wednesday=result["day_size_wednesday"],
            day_size_thursday=result["day_size_thursday"],
            day_size_friday=result["day_size_friday"],
            oi_buildup_threshold=result["oi_buildup_threshold"],
            oi_unwind_threshold=result["oi_unwind_threshold"],
            oi_wall_strong_cal=result["oi_wall_strong_cal"],
            oi_wall_moderate_cal=result["oi_wall_moderate_cal"],
            pcr_bullish_threshold=result["pcr_bullish_threshold"],
            pcr_bearish_threshold=result["pcr_bearish_threshold"],
            skew_bearish_threshold=result["skew_bearish_threshold"],
            skew_bullish_threshold=result["skew_bullish_threshold"],
            straddle_ratio_sell=result["straddle_ratio_sell"],
            signal_weight_vrp=result["signal_weight_vrp"],
            signal_weight_price=result["signal_weight_price"],
            signal_weight_positioning=result["signal_weight_positioning"],
            signal_weight_iv_behavior=result["signal_weight_iv_behavior"],
            signal_weight_or_condition=result["signal_weight_or_condition"],
            phantom_false_negative_rate=phantom_fnr if phantom_fnr > 0 else None,
            exit_quality_score=exit_quality_score,
            regime_accuracy_score=regime_accuracy_score,
            monday_avg_range=result["monday_avg_range"],
            tuesday_avg_range=result["tuesday_avg_range"],
            wednesday_avg_range=result["wednesday_avg_range"],
            thursday_avg_range=result["thursday_avg_range"],
            friday_avg_range=result["friday_avg_range"],
        )

        self._state = cal
        self.logger.info(
            f"Calibration complete: tier={cal_tier} valid={is_valid} "
            f"vrp_sell={result['vrp_sell_threshold']:.2f}pp"
        )
        return cal

    def _calibrate_vix(self, n_days: int) -> dict:
        """Calibrate VIX percentile thresholds from stored vix_history."""
        result = {}
        try:
            vix_df = self.db.get_vix_history(days=365)
            if not hasattr(vix_df, 'empty') or vix_df.empty or len(vix_df) < 20:
                # Bootstrap from daily_summary
                daily_df = self.db.get_daily_summary(days=730)
                if not hasattr(daily_df, 'empty') or daily_df.empty:
                    return result
                vcols = [c for c in ["vix_open", "vix_close", "vix_high", "vix_low"]
                         if c in daily_df.columns]
                if not vcols:
                    return result
                vix_vals = pd.concat(
                    [daily_df[c].dropna() for c in vcols]
                ).values
                vix_vals = vix_vals[(vix_vals > 8.0) & (vix_vals < 90.0)]
                if len(vix_vals) < 10:
                    return result
                v = vix_vals
            else:
                v = vix_df["vix_value"].dropna().values
                v = v[(v > 8.0) & (v < 90.0)]

            n = len(v)
            p25 = float(np.percentile(v, 25))
            p50 = float(np.percentile(v, 50))
            p75 = float(np.percentile(v, 75))
            p90 = float(np.percentile(v, 90))

            # Apply Bayesian shrinkage toward NIFTY 2026 defaults
            d = self.DEFAULTS
            result["vix_p25"] = self._bayesian_shrink(p25, d["vix_p25"], n)
            result["vix_p50"] = self._bayesian_shrink(p50, d["vix_p50"], n)
            result["vix_p75"] = self._bayesian_shrink(p75, d["vix_p75"], n)
            result["vix_p90"] = self._bayesian_shrink(p90, d["vix_p90"], n)

            # Enforce minimum floors
            result["vix_p25"] = max(result["vix_p25"], 10.0)
            result["vix_p50"] = max(result["vix_p50"], 12.0)
            result["vix_p75"] = max(result["vix_p75"], 15.0)
            result["vix_p90"] = max(result["vix_p90"], 20.0)

            self.logger.info(
                f"  VIX calibrated (n={n}): "
                f"p25={result['vix_p25']:.1f} p50={result['vix_p50']:.1f} "
                f"p75={result['vix_p75']:.1f} p90={result['vix_p90']:.1f}"
            )
        except Exception as e:
            self.logger.debug(f"VIX calibration error: {e}")
        return result

    def _calibrate_vrp(self, n_days: int) -> dict:
        """
        Calibrate VRP sell threshold from win rates by VRP bucket.
        Finds minimum VRP where win_rate > 55% AND avg_pnl > round_trip_costs.
        """
        result = {}
        try:
            rows = self.db.get_vrp_win_rates(days=60)
            if not rows or len(rows) < 3:
                return result

            d = self.DEFAULTS
            sell_thresh = d["vrp_sell_threshold"]
            fair_thresh = d["vrp_fair_threshold"]

            for row in sorted(rows, key=lambda r: r.get("vrp_bucket", 0)):
                bucket   = float(row.get("vrp_bucket", 0) or 0)
                n        = int(row.get("n_trades", 0) or 0)
                wins     = int(row.get("n_wins", 0) or 0)
                avg_pnl  = float(row.get("avg_pnl", 0) or 0)
                avg_cost = float(row.get("avg_costs", 0) or 0)

                if n < 5:
                    continue

                win_rate = wins / n
                # Bayesian shrinkage on win rate
                shrunk_wr = self._bayesian_shrink(win_rate, 0.55, n)

                if shrunk_wr >= 0.55 and avg_pnl > avg_cost:
                    sell_thresh = min(sell_thresh, bucket)
                    break

            for row in sorted(rows, key=lambda r: r.get("vrp_bucket", 0)):
                bucket   = float(row.get("vrp_bucket", 0) or 0)
                n        = int(row.get("n_trades", 0) or 0)
                avg_pnl  = float(row.get("avg_pnl", 0) or 0)

                if n < 5:
                    continue
                shrunk_pnl = self._bayesian_shrink(avg_pnl, 0, n)
                if shrunk_pnl > -500:
                    fair_thresh = min(fair_thresh, bucket)
                    break

            sell_thresh = max(sell_thresh, 1.5)
            fair_thresh = max(fair_thresh, 0.8)
            fair_thresh = min(fair_thresh, sell_thresh * 0.8)

            result["vrp_sell_threshold"] = round(sell_thresh, 2)
            result["vrp_fair_threshold"] = round(fair_thresh, 2)
            self.logger.info(
                f"  VRP calibrated: sell={sell_thresh:.2f}pp fair={fair_thresh:.2f}pp "
                f"(n_buckets={len(rows)})"
            )
        except Exception as e:
            self.logger.debug(f"VRP calibration error: {e}")
        return result

    def _calibrate_day_sizes(self, n_days: int) -> dict:
        """
        Calibrate day size multipliers from win rate by day of week.
        Increases size on high-win-rate days, decreases on low-win-rate days.
        """
        result = {}
        d = self.DEFAULTS
        day_map = {
            0: ("monday",    d["day_size_monday"]),
            1: ("tuesday",   d["day_size_tuesday"]),
            2: ("wednesday", d["day_size_wednesday"]),
            3: ("thursday",  d["day_size_thursday"]),
            4: ("friday",    d["day_size_friday"]),
        }
        try:
            daily_df = self.db.get_daily_summary(days=365)
            if not hasattr(daily_df, 'empty') or daily_df.empty:
                return result
            if "net_pnl_rupees" not in daily_df.columns:
                return result

            for wd, (name, base) in day_map.items():
                sub = daily_df[daily_df["weekday"] == wd].copy()
                if len(sub) < 5:
                    result[f"day_size_{name}"] = base
                    continue

                traded = (
                    sub[sub["trades_executed"] > 0].copy()
                    if "trades_executed" in sub.columns
                    else sub.copy()
                )
                if len(traded) < 3:
                    result[f"day_size_{name}"] = base
                    continue

                wins     = (traded["net_pnl_rupees"] > 0).sum()
                total    = len(traded)
                win_rate = wins / total
                avg_pnl  = float(traded["net_pnl_rupees"].mean())

                # Bayesian shrinkage
                shrunk_wr = self._bayesian_shrink(win_rate, 0.55, total)

                if shrunk_wr >= 0.60 and avg_pnl > 0:
                    size = min(base * 1.10, 1.00)
                elif shrunk_wr < 0.40 or avg_pnl < 0:
                    size = max(base * 0.80, 0.25)
                else:
                    size = base

                result[f"day_size_{name}"] = round(size, 2)
                self.logger.info(
                    f"  DaySize {name}: wr={win_rate:.1%} "
                    f"shrunk={shrunk_wr:.1%} size={size:.2f} (n={total})"
                )
        except Exception as e:
            self.logger.debug(f"Day size calibration error: {e}")
        return result

    def _calibrate_oi(self, n_days: int) -> dict:
        """Calibrate OI buildup/unwind thresholds from market_snapshots."""
        result = {}
        try:
            snap_df = self.db.get_market_snapshots(days=365)
            if not hasattr(snap_df, 'empty') or snap_df.empty:
                return result
            if "oi_change_pct" not in snap_df.columns:
                return result

            oi_chg = snap_df["oi_change_pct"].dropna().values
            pos_chg = oi_chg[oi_chg > 0]
            neg_chg = oi_chg[oi_chg < 0]

            d = self.DEFAULTS
            if len(pos_chg) >= 20:
                build = float(np.percentile(pos_chg, 60))
                result["oi_buildup_threshold"] = self._bayesian_shrink(
                    build, d["oi_buildup_threshold"], len(pos_chg)
                )
                result["oi_buildup_threshold"] = max(
                    result["oi_buildup_threshold"], 0.04
                )

            if len(neg_chg) >= 20:
                unwind = float(np.percentile(neg_chg, 40))
                result["oi_unwind_threshold"] = self._bayesian_shrink(
                    unwind, d["oi_unwind_threshold"], len(neg_chg)
                )
                result["oi_unwind_threshold"] = min(
                    result["oi_unwind_threshold"], -0.04
                )

            self.logger.info(
                f"  OI calibrated: build={result.get('oi_buildup_threshold', d['oi_buildup_threshold']):.4f} "
                f"unwind={result.get('oi_unwind_threshold', d['oi_unwind_threshold']):.4f}"
            )
        except Exception as e:
            self.logger.debug(f"OI calibration error: {e}")
        return result

    def _calibrate_pcr(self, n_days: int) -> dict:
        """
        Calibrate PCR thresholds by finding levels that predicted
        directional bias in historical data.
        """
        result = {}
        try:
            snap_df = self.db.get_market_snapshots(days=365)
            if not hasattr(snap_df, 'empty') or snap_df.empty:
                return result
            if "pcr" not in snap_df.columns:
                return result

            pcr_vals = snap_df["pcr"].dropna().values
            pcr_vals = pcr_vals[(pcr_vals > 0.3) & (pcr_vals < 4.0)]
            if len(pcr_vals) < 50:
                return result

            d = self.DEFAULTS
            bull_thresh = float(np.percentile(pcr_vals, 25))
            bear_thresh = float(np.percentile(pcr_vals, 75))

            result["pcr_bullish_threshold"] = self._bayesian_shrink(
                bull_thresh, d["pcr_bullish_threshold"], len(pcr_vals)
            )
            result["pcr_bearish_threshold"] = self._bayesian_shrink(
                bear_thresh, d["pcr_bearish_threshold"], len(pcr_vals)
            )

            # Enforce valid range
            result["pcr_bullish_threshold"] = max(
                min(result["pcr_bullish_threshold"], 0.85), 0.55
            )
            result["pcr_bearish_threshold"] = max(
                min(result["pcr_bearish_threshold"], 1.60), 1.10
            )

            self.logger.info(
                f"  PCR calibrated: bull={result['pcr_bullish_threshold']:.3f} "
                f"bear={result['pcr_bearish_threshold']:.3f} (n={len(pcr_vals)})"
            )
        except Exception as e:
            self.logger.debug(f"PCR calibration error: {e}")
        return result

    def _calibrate_skew(self, n_days: int) -> dict:
        """Calibrate skew thresholds from market_snapshots skew_ratio column."""
        result = {}
        try:
            snap_df = self.db.get_market_snapshots(days=365)
            if not hasattr(snap_df, 'empty') or snap_df.empty:
                return result
            if "skew_ratio" not in snap_df.columns:
                return result

            skew_vals = snap_df["skew_ratio"].dropna().values
            skew_vals = skew_vals[(skew_vals > 0.5) & (skew_vals < 3.0)]
            if len(skew_vals) < 30:
                return result

            d = self.DEFAULTS
            bear = float(np.percentile(skew_vals, 75))
            bull = float(np.percentile(skew_vals, 25))

            result["skew_bearish_threshold"] = self._bayesian_shrink(
                bear, d["skew_bearish_threshold"], len(skew_vals)
            )
            result["skew_bullish_threshold"] = self._bayesian_shrink(
                bull, d["skew_bullish_threshold"], len(skew_vals)
            )

            result["skew_bearish_threshold"] = max(result["skew_bearish_threshold"], 1.5)
            result["skew_bullish_threshold"] = min(result["skew_bullish_threshold"], 1.1)

            self.logger.info(
                f"  Skew calibrated: bear={result['skew_bearish_threshold']:.3f} "
                f"bull={result['skew_bullish_threshold']:.3f} (n={len(skew_vals)})"
            )
        except Exception as e:
            self.logger.debug(f"Skew calibration error: {e}")
        return result

    def _calibrate_day_ranges(self, n_days: int) -> dict:
        """Calibrate average intraday range by day of week."""
        result = {}
        day_map = {
            0: "monday", 1: "tuesday", 2: "wednesday",
            3: "thursday", 4: "friday",
        }
        try:
            daily_df = self.db.get_daily_summary(days=365)
            if not hasattr(daily_df, 'empty') or daily_df.empty:
                return result
            if "day_range_points" not in daily_df.columns:
                return result

            for wd, name in day_map.items():
                sub = daily_df[daily_df["weekday"] == wd]["day_range_points"].dropna()
                if len(sub) >= 3:
                    avg_range = float(sub.mean())
                    result[f"{name}_avg_range"] = self._bayesian_shrink(
                        avg_range, 150.0, len(sub)
                    )
                    self.logger.info(
                        f"  Range {name}: avg={avg_range:.0f}pts (n={len(sub)})"
                    )
        except Exception as e:
            self.logger.debug(f"Day range calibration error: {e}")
        return result

    def _calibrate_signal_weights(self, n_days: int) -> dict:
        """
        Calibrate signal weights from historical predictive accuracy.
        Tier 3 only (60+ days).
        """
        result = {}
        try:
            accuracy = self.db.get_signal_predictive_accuracy(days=60)
            if not accuracy or accuracy.get("total_trades", 0) < 20:
                return result

            d = self.DEFAULTS
            # Lift > 1.0 means signal is predictive, use as weight
            result["signal_weight_vrp"] = self._bayesian_shrink(
                accuracy.get("vol_lift", 1.0), d["signal_weight_vrp"],
                accuracy.get("total_trades", 0)
            )
            result["signal_weight_price"] = self._bayesian_shrink(
                accuracy.get("price_lift", 1.0), d["signal_weight_price"],
                accuracy.get("total_trades", 0)
            )
            result["signal_weight_positioning"] = self._bayesian_shrink(
                accuracy.get("pos_lift", 1.0), d["signal_weight_positioning"],
                accuracy.get("total_trades", 0)
            )
            result["signal_weight_iv_behavior"] = self._bayesian_shrink(
                accuracy.get("iv_lift", 1.0), d["signal_weight_iv_behavior"],
                accuracy.get("total_trades", 0)
            )
            result["signal_weight_or_condition"] = self._bayesian_shrink(
                accuracy.get("or_lift", 1.0), d["signal_weight_or_condition"],
                accuracy.get("total_trades", 0)
            )

            # Clamp weights to reasonable range
            for key in [
                "signal_weight_vrp", "signal_weight_price",
                "signal_weight_positioning", "signal_weight_iv_behavior",
                "signal_weight_or_condition",
            ]:
                result[key] = max(0.1, min(result[key], 3.0))

            self.logger.info(
                f"  Signal weights calibrated: "
                f"vrp={result['signal_weight_vrp']:.2f} "
                f"price={result['signal_weight_price']:.2f} "
                f"pos={result['signal_weight_positioning']:.2f} "
                f"iv={result['signal_weight_iv_behavior']:.2f} "
                f"or={result['signal_weight_or_condition']:.2f}"
            )
        except Exception as e:
            self.logger.debug(f"Signal weight calibration error: {e}")
        return result


# ─────────────────────────────────────────────────────────────────────────────
# REGIME CLASSIFIER
# ─────────────────────────────────────────────────────────────────────────────

# NIFTY_ENGINE_PROFIT_PATCH_V34: bounds for the VRP data-error guard. The expiry series gets a
# looser ratio because a low realised-to-implied ratio is its normal state,
# and an absolute floor carries the burden of catching a genuinely dead feed.
VRP_DATA_ERROR_FRAC = 0.7
VRP_DATA_ERROR_FRAC_DTE0 = 0.92
VRP_RV_DEAD_PCT = 0.5


class RegimeClassifier:
    """
    Classifies the four regime dimensions from market signals.
    Uses calibrated thresholds from CalibrationState.
    All methods are pure functions of signals + calibration — no side effects.
    """

    def __init__(
        self,
        config: Config,
        cal: Optional[CalibrationState],
        logger,
    ):
        self.config = config
        self.cal    = cal
        self.logger = logger

    def _t(self, cal_attr: str, cfg_attr: str, default: float = 0.0) -> float:
        """
        Get threshold: calibration value if available, else config value, else default.
        """
        if self.cal and self.cal.is_calibrated:
            v = getattr(self.cal, cal_attr, None)
            if v is not None:
                return float(v)
        v2 = getattr(self.config, cfg_attr, None)
        if v2 is not None:
            return float(v2)
        return default

    # ─────────────────────────────────────────────────────────────────────
    # VOLATILITY REGIME
    # ─────────────────────────────────────────────────────────────────────

    def classify_volatility(
        self,
        signals: dict,
        vix_fail_count: int,
        prev_day_vix_close: Optional[float],
    ) -> Tuple[VolatilityRegime, dict]:
        """
        Classify volatility regime using the new gate-based approach.

        Gate order (hard blocks checked first):
        1. ABORT: real VIX spike, extreme VIX, VIX data failure
        2. VRP data error: VRP > max(8pp, 0.70 x ATM IV) → NEUTRAL
        3. IV behavior: EXPANDING/SPIKING → NEUTRAL (hard block)
        4. Day move used: > 55% of straddle → NEUTRAL
        5. VRP classification: proportional to ATM IV, DTE-adjusted, OR-adjusted
        6. Borderline sell check

        Returns (VolatilityRegime, details_dict)
        """
        vix         = float(signals.get("vix") or 15.0)
        vrp_smoothed = signals.get("vrp_smoothed")
        vrp_raw      = signals.get("vrp_raw")
        atm_iv       = signals.get("atm_iv")
        iv_behavior  = signals.get("iv_behavior", "UNKNOWN")
        day_move_used = float(signals.get("day_move_used_pct") or 0.0)
        dte          = signals.get("actual_dte")
        or_condition = signals.get("or_condition")
        chain_stale  = bool(signals.get("chain_stale", False))

        details: dict = {
            "vix": vix,
            "vrp_smoothed": vrp_smoothed,
            "vrp_raw": vrp_raw,
            "iv_behavior": iv_behavior,
            "day_move_used_pct": day_move_used,
            "dte": dte,
            "or_condition": or_condition,
        }

        # ── Gate 1: ABORT checks ──────────────────────────────────────────

        # Real VIX spike: VIX up ≥15% from previous day close AND VIX ≥ 14
        if prev_day_vix_close and prev_day_vix_close > 0 and vix >= 14.0:
            spike_pct = (vix - prev_day_vix_close) / prev_day_vix_close * 100.0
            if spike_pct >= self.config.abort_vix_spike_pct:
                details["trigger"] = f"REAL_VIX_SPIKE_{spike_pct:.1f}pct"
                self.logger.warning(
                    f"ABORT: Real VIX spike {spike_pct:.1f}% "
                    f"({prev_day_vix_close:.1f} → {vix:.1f})"
                )
                return VolatilityRegime.ABORT, details

        # Extreme absolute VIX
        if vix >= self.config.abort_vix_absolute:
            details["trigger"] = f"VIX_EXTREME_{vix:.1f}"
            self.logger.warning(f"ABORT: VIX={vix:.1f} >= {self.config.abort_vix_absolute}")
            return VolatilityRegime.ABORT, details

        # VIX data failure
        if vix_fail_count >= self.config.vix_fail_limit:
            details["trigger"] = f"VIX_DATA_FAILURE_{vix_fail_count}_cycles"
            self.logger.warning(
                f"ABORT: VIX data unavailable for {vix_fail_count} consecutive cycles"
            )
            return VolatilityRegime.ABORT, details

        # ── Gate 2: VRP data error (v3.1) ─────────────────────────────────
        # The same flat 8pp rule existed independently HERE and in
        # data_engine._compute_vrp, and this one is the binding constraint:
        # it returns NEUTRAL, which is a hard no-trade. Correcting only the
        # data_engine copy would have achieved nothing.
        #
        # A genuine variance risk premium above 8pp is routine on a quiet
        # NIFTY 0DTE morning and is precisely the condition a premium seller
        # exists to harvest, so the flat rule stood the engine down on its
        # best days. A real Parkinson failure does not present as an absolute
        # number of points — it presents as realised vol collapsing to a small
        # fraction of implied — so the bound is now relative to ATM IV, with
        # the old 8pp kept as a floor to protect genuinely low-IV regimes.
        _atm_iv_pct = None
        if atm_iv is not None:
            try:
                _atm_iv_pct = float(atm_iv)
                if _atm_iv_pct <= 2.0:      # stored as a decimal, not a pct
                    _atm_iv_pct *= 100.0
            except (TypeError, ValueError):
                _atm_iv_pct = None
        # ── v3.4 ──────────────────────────────────────────────────────────
        # Measured over the 2026-09-08 0DTE session: this guard was the
        # terminal gate on 544 of 768 market-hours cycles, 70.8% of the day.
        # Broker ATM IV on the expiry series ran 27-29% against a Parkinson
        # RV near 6% and a VIX of 11.2, so vrp_raw sat around 22pp and
        # cleared the 0.70 x IV bound of 20.2pp. Every one of those cycles
        # was discarded as corrupt input.
        #
        # None of it was corrupt. The 28.8% median ATM IV is the broker's
        # own figure in the stored chain and the ~6% realised vol is right
        # for a session with a 90-point total range. VIX prices roughly 30
        # calendar days; a contract with hours left prices pin risk and
        # gamma, and two to three times VIX is its ordinary state on expiry
        # day. Realised vol at a fifth of implied is not a broken feed — it
        # is the variance risk premium this engine exists to sell.
        #
        # A real Parkinson failure does not look like a low ratio. It looks
        # like realised vol collapsing to nothing because the bar feed is
        # empty, flat or degenerate. So the test is split: an absolute floor
        # catches the true failure, and the ratio bound is relaxed on the
        # expiry series where a low ratio is the expected reading.
        _rv_pct = None
        _rv_raw = signals.get("parkinson_rv")
        if _rv_raw is not None:
            try:
                _rv_pct = float(_rv_raw)
                if _rv_pct <= 2.0:          # stored as a decimal, not a pct
                    _rv_pct *= 100.0
            except (TypeError, ValueError):
                _rv_pct = None

        try:
            _dte_vrp = int(dte) if dte is not None else None
        except (TypeError, ValueError):
            _dte_vrp = None

        # 0.92 on the expiry series admits realised vol down to 8% of
        # implied before the reading is called impossible; 0.70 elsewhere,
        # unchanged from v3.1.
        _vrp_frac = VRP_DATA_ERROR_FRAC_DTE0 if _dte_vrp == 0 else VRP_DATA_ERROR_FRAC
        _vrp_limit = max(8.0, _vrp_frac * _atm_iv_pct) if _atm_iv_pct else 8.0

        _rv_dead = _rv_pct is not None and _rv_pct <= VRP_RV_DEAD_PCT
        _vrp_over = vrp_raw is not None and vrp_raw > _vrp_limit

        if _rv_dead or _vrp_over:
            details["trigger"] = "VRP_DATA_ERROR_NEUTRAL"
            details["vrp_limit"] = _vrp_limit
            details["vrp_reason"] = "rv_dead" if _rv_dead else "ratio"
            _why = (
                f"realised vol {_rv_pct:.2f}% at or below the "
                f"{VRP_RV_DEAD_PCT:.2f}% floor — bar feed looks empty"
                if _rv_dead else
                f"VRP={vrp_raw:.2f}pp > limit {_vrp_limit:.2f}pp"
            )
            self.logger.warning(
                f"{_why} (ATM IV "
                f"{_atm_iv_pct if _atm_iv_pct else float('nan'):.2f}%, "
                f"dte={_dte_vrp}) — likely Parkinson RV error. "
                f"Treating as NEUTRAL."
            )
            return VolatilityRegime.NEUTRAL, details

        # ── Gate 3: IV behavior hard block ────────────────────────────────
        if iv_behavior in ("EXPANDING", "SPIKING"):
            details["trigger"] = f"IV_{iv_behavior}_HARD_BLOCK"
            return VolatilityRegime.NEUTRAL, details

        # ── Gate 4: Day move used ─────────────────────────────────────────
        if day_move_used >= self.config.day_move_used_block_pct:
            details["trigger"] = f"DAY_MOVE_USED_{day_move_used:.0f}PCT"
            return VolatilityRegime.NEUTRAL, details

        # ── Gate 5: Chain stale ───────────────────────────────────────────
        if chain_stale:
            details["trigger"] = "CHAIN_STALE"
            return VolatilityRegime.NEUTRAL, details

        # ── Gate 6: VRP unavailable ───────────────────────────────────────
        if vrp_smoothed is None:
            details["trigger"] = "VRP_UNAVAILABLE"
            return VolatilityRegime.NEUTRAL, details

        # ── Gate 7: VRP classification ────────────────────────────────────

        # Base thresholds from calibration
        vrp_sell = self._t("vrp_sell_threshold", "vrp_sell_threshold_default", 2.5)
        vrp_fair = self._t("vrp_fair_threshold", "vrp_fair_threshold_default", 1.5)

        # DTE adjustment: lower threshold for 0DTE (theta compensates)
        if dte == 0:
            vrp_sell = vrp_sell * 1.00
        elif dte == 1:
            vrp_sell = vrp_sell * 1.00
        elif dte == 2:
            vrp_sell = vrp_sell * 1.05
        elif dte == 3:
            vrp_sell = vrp_sell * 1.10
        elif dte == 4:
            vrp_sell = vrp_sell * 1.20
        elif dte == 5:
            vrp_sell = vrp_sell * 1.30
        elif dte is not None and dte >= 6:
            vrp_sell = vrp_sell * 1.40

        # OR condition adjustment
        if or_condition == "WIDE":
            vrp_sell = vrp_sell * 1.20
        elif or_condition == "VERY_WIDE":
            vrp_sell = vrp_sell * 1.50

        vrp_sell = max(vrp_sell, 2.0)
        vrp_very_rich = vrp_sell * 1.30  # STRONG_SELL threshold

        details["vrp_sell_adjusted"] = round(vrp_sell, 3)
        details["vrp_fair"] = round(vrp_fair, 3)

        if vrp_smoothed > vrp_very_rich:
            details["trigger"] = f"VRP_{vrp_smoothed:.2f}pp_STRONG_SELL"
            return VolatilityRegime.STRONG_SELL_PREMIUM, details

        if vrp_smoothed > vrp_sell:
            details["trigger"] = f"VRP_{vrp_smoothed:.2f}pp_SELL"
            return VolatilityRegime.SELL_PREMIUM, details

        if vrp_smoothed > vrp_fair:
            # Check borderline sell eligibility
            if self._is_borderline_sell_eligible(signals, vrp_smoothed, vrp_sell):
                details["trigger"] = f"VRP_{vrp_smoothed:.2f}pp_BORDERLINE"
                return VolatilityRegime.BORDERLINE_SELL, details
            details["trigger"] = f"VRP_{vrp_smoothed:.2f}pp_NEUTRAL"
            return VolatilityRegime.NEUTRAL, details

        if vrp_smoothed > 0:
            details["trigger"] = f"VRP_{vrp_smoothed:.2f}pp_THIN"
            return VolatilityRegime.NEUTRAL, details

        # VRP <= 0: realized vol exceeds implied vol
        details["trigger"] = f"VRP_{vrp_smoothed:.2f}pp_BUY_OPTIONS_DISABLED"
        return VolatilityRegime.BUY_OPTIONS, details

    def _is_borderline_sell_eligible(
        self,
        signals: dict,
        vrp: float,
        threshold: float,
    ) -> bool:
        """
        Check if a borderline VRP trade is eligible.
        ALL conditions must be true for BORDERLINE_SELL to proceed.

        Conditions:
        - VRP within 25% of threshold (not too far below)
        - DTE = 0 (Tuesday only — theta compensates)
        - OR condition NARROW or VERY_NARROW
        - IV behavior STABLE or DECLINING or CRUSHING
        - ADX < 20 (clearly flat market)
        - OR computed (opening range established)
        """
        if vrp < threshold * 0.75:
            return False  # Too far below threshold

        dte          = signals.get("actual_dte")
        or_condition = signals.get("or_condition")
        iv_behavior  = signals.get("iv_behavior", "UNKNOWN")
        adx_15       = float(signals.get("adx_15") or 0.0)
        or_computed  = bool(signals.get("or_computed", False))

        if dte != 0:
            return False  # Only on Tuesday 0DTE

        if or_condition not in ("VERY_NARROW", "NARROW"):
            return False  # Need narrow OR for borderline

        if iv_behavior not in ("STABLE", "DECLINING", "CRUSHING"):
            return False  # IV must be stable or falling

        if adx_15 >= 20:
            return False  # Market must be flat

        if not or_computed:
            return False  # Need OR established

        return True

    # ─────────────────────────────────────────────────────────────────────
    # PRICE REGIME
    # ─────────────────────────────────────────────────────────────────────

    def classify_price(self, signals: dict) -> PriceRegime:
        """
        Classify price regime using hard rules (no voting).

        Priority order:
        1. OBSERVING: OR not established
        2. CHOPPY: OR established but fake breakouts detected
        3. ADX immature -> opening-range structure, confirmed by VWAP
           (v3.2: this block used to live inside this docstring, so it
           was prose and never ran; it is now real code below)
        4. STRONG_DOWNTREND: ADX > 35, bearish EMA, spot > 100pts below OR
        5. STRONG_UPTREND: ADX > 35, bullish EMA, spot > 100pts above OR
        6. DOWNTREND: ADX >= 25, bearish EMA, spot below OR low
        7. UPTREND: ADX >= 25, bullish EMA, spot above OR high
        8. RANGE: default when ADX < 25 and spot inside OR

        Key rule: price regime is determined by ADX + EMA + spot vs OR.
        HH/HL is used as confirmation only, not as primary signal.
        """
        or_computed   = bool(signals.get("or_computed", False))
        choppy        = bool(signals.get("choppy_detected", False))
        adx_15        = float(signals.get("adx_15") or 0.0)
        adx_15_mature = bool(signals.get("adx_15_mature", False))
        ema_structure = signals.get("ema_structure", "INSUFFICIENT_DATA")
        hh_hl         = signals.get("hh_hl", "INSUFFICIENT_DATA")
        or_high       = float(signals.get("or_high") or 0.0)
        or_low        = float(signals.get("or_low") or 0.0)
        spot          = float(signals.get("spot") or 0.0)
        orb_structure = signals.get("orb_price_regime", "OBSERVING")

        adx_trend  = self.config.adx_trend_threshold
        adx_strong = self.config.adx_strong_threshold

        # ── Step 1: OBSERVING ─────────────────────────────────────────────
        if not or_computed:
            return PriceRegime.OBSERVING

        # ── Step 2: CHOPPY ────────────────────────────────────────────────
        if choppy:
            return PriceRegime.CHOPPY

        # ── Step 3: ADX maturity ──────────────────────────────────────────
        # v3.2: when the trend reading is not yet trustworthy the engine
        # falls back to the opening-range structure - but an ORB label on
        # its own is the single most over-traded signal on NIFTY: price
        # pokes 20 points through the range and reverts constantly. The
        # confirmation a professional applies is VWAP: a breakout that is
        # not supported by the volume-weighted average price is noise,
        # and calling it a trend makes the engine sell the WRONG side.
        # This is the logic that was stranded inside the docstring.
        if not adx_15_mature or adx_15 <= 0:
            _vwap_s = signals.get("vwap_signal", "UNKNOWN")
            if orb_structure == "UPTREND":
                if adx_15 >= adx_trend:
                    return PriceRegime.UPTREND
                if _vwap_s in ("BULLISH", "BULLISH_EXTENDED",
                               "NEUTRAL", "UNKNOWN"):
                    return PriceRegime.UPTREND
                return PriceRegime.RANGE
            if orb_structure == "DOWNTREND":
                if adx_15 >= adx_trend:
                    return PriceRegime.DOWNTREND
                if _vwap_s in ("BEARISH", "BEARISH_EXTENDED",
                               "NEUTRAL", "UNKNOWN"):
                    return PriceRegime.DOWNTREND
                return PriceRegime.RANGE
            if orb_structure == "CHOPPY":
                return PriceRegime.CHOPPY
            return PriceRegime.RANGE

        or_mid = (or_high + or_low) / 2.0 if (or_high > 0 and or_low > 0) else spot

        # ── Step 4: STRONG_DOWNTREND ──────────────────────────────────────
        if (adx_15 > adx_strong and
                ema_structure == "BEARISH" and
                or_low > 0 and spot < or_low - 100):
            return PriceRegime.STRONG_DOWNTREND

        # ── Step 5: STRONG_UPTREND ────────────────────────────────────────
        if (adx_15 > adx_strong and
                ema_structure == "BULLISH" and
                or_high > 0 and spot > or_high + 100):
            return PriceRegime.STRONG_UPTREND

        # ── Step 6: DOWNTREND ─────────────────────────────────────────────
        if (adx_15 >= adx_trend and
                ema_structure in ("BEARISH", "TRANSITIONAL") and
                or_low > 0 and spot < or_low - 20):
            # Confirm with HH/HL if available
            if hh_hl in ("DOWNTREND", "NEUTRAL", "INSUFFICIENT_DATA"):
                return PriceRegime.DOWNTREND
            # HH/HL says UPTREND but ADX+EMA say DOWNTREND — use ADX+EMA
            if adx_15 >= adx_strong:
                return PriceRegime.DOWNTREND
            # Conflicting signals at moderate ADX — stay RANGE
            return PriceRegime.RANGE

        # ── Step 7: UPTREND ───────────────────────────────────────────────
        if (adx_15 >= adx_trend and
                ema_structure in ("BULLISH", "TRANSITIONAL") and
                or_high > 0 and spot > or_high + 20):
            if hh_hl in ("UPTREND", "NEUTRAL", "INSUFFICIENT_DATA"):
                return PriceRegime.UPTREND
            if adx_15 >= adx_strong:
                return PriceRegime.UPTREND
            return PriceRegime.RANGE

        vwap_signal_pr = signals.get("vwap_signal", "UNKNOWN")
        vwap_dist_pr   = float(signals.get("vwap_dist_pct") or 0.0)
        if adx_15 >= (adx_trend - 3) and vwap_signal_pr in ("BULLISH", "BULLISH_EXTENDED") and vwap_dist_pr > 0.20:
            return PriceRegime.UPTREND
        if adx_15 >= (adx_trend - 3) and vwap_signal_pr in ("BEARISH", "BEARISH_EXTENDED") and vwap_dist_pr < -0.20:
            return PriceRegime.DOWNTREND
        return PriceRegime.RANGE

    # ─────────────────────────────────────────────────────────────────────
    # POSITIONING REGIME
    # ─────────────────────────────────────────────────────────────────────

    def classify_positioning(self, signals: dict) -> PositioningRegime:
        """
        Classify positioning regime from OI walls, PCR, skew, OI change.

        Positioning is a MODIFIER not a voter.
        It determines WHERE to place strikes, not WHETHER to trade.

        Priority:
        1. STRONG_RANGE: both walls strong, PCR neutral, OI building
        2. RANGE: walls present, PCR mild
        3. BULLISH: PCR extreme greed OR support wall dominates OR complacent skew
        4. BEARISH: PCR extreme fear OR resistance wall dominates OR fear skew
        5. UNCLEAR: mixed signals
        """
        pcr            = float(signals.get("pcr") or 1.0)
        resistance_oi  = int(signals.get("resistance_oi") or 0)
        support_oi     = int(signals.get("support_oi") or 0)
        total_ce_oi    = int(signals.get("total_ce_oi") or 0)
        total_pe_oi    = int(signals.get("total_pe_oi") or 0)
        skew_ratio     = signals.get("skew_ratio")
        oi_change      = float(signals.get("oi_change_pct") or 0.0)
        chain_size     = int(signals.get("chain_size") or 71)
        r_str          = float(signals.get("resistance_strength") or 0.0)
        s_str          = float(signals.get("support_strength") or 0.0)

        # Calibrated thresholds
        pcr_bull   = self._t("pcr_bullish_threshold", "pcr_bullish_threshold", 0.72)
        pcr_bear   = self._t("pcr_bearish_threshold", "pcr_bearish_threshold", 1.28)
        oi_build   = self._t("oi_buildup_threshold",  "oi_buildup_threshold",  0.08)
        oi_unwind  = self._t("oi_unwind_threshold",   "oi_unwind_threshold",  -0.08)
        oi_strong  = self._t("oi_wall_strong_cal",    "oi_wall_strong",        2.5)
        oi_mod     = self._t("oi_wall_moderate_cal",  "oi_wall_moderate",      1.7)
        skew_bear  = self._t("skew_bearish_threshold","skew_bearish_threshold",3.0)
        skew_bull  = self._t("skew_bullish_threshold","skew_bullish_threshold",0.95)

        # Derived conditions
        wall_strong_range = (
            r_str >= oi_strong and
            s_str >= oi_strong and
            0.8 <= pcr <= 1.3 and
            oi_change > oi_build
        )
        wall_range = (
            r_str >= oi_mod and
            s_str >= oi_mod and
            0.7 <= pcr <= 1.4
        )
        oi_building  = oi_change > oi_build
        oi_unwinding = oi_change < oi_unwind

        # v3.2: the extremes were purely relative to the calibrated
        # thresholds, so with the shipped defaults "extreme fear" meant
        # PCR > 1.28 * 1.20 = 1.536. On the NIFTY weekly chain a PCR of
        # 1.50 IS an extreme - the contrarian read that positioning is
        # supposed to provide simply never fired, and the engine fell
        # through to UNCLEAR, which halves its size and often blocks the
        # trade outright. Absolute bounds are applied alongside the
        # relative ones so calibration can tighten but not un-fire them.
        _pcr_abs_bull = 1.45
        _pcr_abs_bear = 0.58
        pcr_extreme_bull = pcr > min(pcr_bear * 1.20, _pcr_abs_bull)
        pcr_extreme_bear = pcr < max(pcr_bull * 0.80, _pcr_abs_bear)
        pcr_bullish      = pcr > pcr_bear
        pcr_bearish      = pcr < pcr_bull

        skew_bearish = skew_ratio is not None and skew_ratio > skew_bear
        skew_bullish = skew_ratio is not None and skew_ratio < skew_bull

        # ── STRONG_RANGE ──────────────────────────────────────────────────
        if skew_bearish and not wall_strong_range:
            return PositioningRegime.BEARISH

        if skew_bullish and pcr_extreme_bull and not wall_strong_range:
            return PositioningRegime.BULLISH

        if (wall_strong_range and
                not pcr_extreme_bull and
                not pcr_extreme_bear):
            return PositioningRegime.STRONG_RANGE

        # ── RANGE ─────────────────────────────────────────────────────────
        if (wall_range and
                not oi_unwinding and
                not pcr_extreme_bull and
                not pcr_extreme_bear):
            return PositioningRegime.RANGE

        # ── BULLISH ───────────────────────────────────────────────────────
        if (pcr_extreme_bull or
                (s_str > r_str * 1.5 and pcr_bullish and not skew_bearish) or
                (skew_bullish and pcr_bullish)):
            return PositioningRegime.BULLISH

        # ── BEARISH ───────────────────────────────────────────────────────
        if (pcr_extreme_bear or
                (r_str > s_str * 1.5 and pcr_bearish) or
                skew_bearish):
            return PositioningRegime.BEARISH

        # ── Mild directional ──────────────────────────────────────────────
        if s_str > r_str * 1.4 and not pcr_bearish:
            return PositioningRegime.BULLISH
        if r_str > s_str * 1.4 and not pcr_bullish:
            return PositioningRegime.BEARISH

        # ── RANGE fallback if walls present ───────────────────────────────
        if wall_range:
            return PositioningRegime.RANGE

        _chain_sz = int(signals.get("chain_size") or 0)
        if _chain_sz < 30:
            return PositioningRegime.RANGE
        return PositioningRegime.UNCLEAR

    # ─────────────────────────────────────────────────────────────────────
    # CONFIDENCE SCORE
    # ─────────────────────────────────────────────────────────────────────

    def compute_confidence(
        self,
        vol: VolatilityRegime,
        price: PriceRegime,
        pos: PositioningRegime,
        signals: dict,
    ) -> Tuple[ConfidenceLevel, float]:
        """
        Compute weighted confidence score from all regime dimensions.

        Each signal is scored 0.0-1.0:
        - 1.0 = strongly supports trading
        - 0.5 = neutral
        - 0.0 = against trading

        Weights come from calibration (how predictive each signal has been historically).

        Returns (ConfidenceLevel, weighted_score_0_to_1)
        """
        if vol in (VolatilityRegime.ABORT, VolatilityRegime.BUY_OPTIONS):
            return ConfidenceLevel.NONE, 0.0

        # Signal weights from calibration
        w_vrp  = self._t("signal_weight_vrp",          "vrp_sell_threshold_default", 1.0)
        w_price = self._t("signal_weight_price",        "adx_trend_threshold",        1.0)
        w_pos  = self._t("signal_weight_positioning",   "oi_wall_strong",             1.0)
        w_iv   = self._t("signal_weight_iv_behavior",   "vrp_fair_threshold_default", 1.0)
        w_or   = self._t("signal_weight_or_condition",  "vrp_sell_threshold_default", 1.0)

        # Normalise weights so they don't all need to be 1.0
        # Use actual calibration values if available
        if self.cal:
            w_vrp   = float(self.cal.signal_weight_vrp)
            w_price = float(self.cal.signal_weight_price)
            w_pos   = float(self.cal.signal_weight_positioning)
            w_iv    = float(self.cal.signal_weight_iv_behavior)
            w_or    = float(self.cal.signal_weight_or_condition)

        # Score each signal
        vrp_score = {
            VolatilityRegime.STRONG_SELL_PREMIUM: 1.0,
            VolatilityRegime.SELL_PREMIUM:        0.85,
            VolatilityRegime.BORDERLINE_SELL:     0.60,
            VolatilityRegime.NEUTRAL:             0.0,
            VolatilityRegime.BUY_OPTIONS:         0.0,
            VolatilityRegime.ABORT:               0.0,
        }.get(vol, 0.0)

        price_score = {
            PriceRegime.RANGE:            1.0,
            PriceRegime.UPTREND:          0.85,
            PriceRegime.DOWNTREND:        0.85,
            PriceRegime.STRONG_UPTREND:   0.70,
            PriceRegime.STRONG_DOWNTREND: 0.70,
            PriceRegime.CHOPPY:           0.0,
            PriceRegime.OBSERVING:        0.0,
        }.get(price, 0.0)

        pos_score = {
            PositioningRegime.STRONG_RANGE: 1.0,
            PositioningRegime.RANGE:        0.85,
            PositioningRegime.BULLISH:      0.75,
            PositioningRegime.BEARISH:      0.75,
            PositioningRegime.UNCLEAR:      0.30,
        }.get(pos, 0.30)

        iv_behavior = signals.get("iv_behavior", "UNKNOWN")
        iv_score = {
            "CRUSHING":  1.0,
            "DECLINING": 0.90,
            "STABLE":    0.80,
            "UNKNOWN":   0.50,
            "EXPANDING": 0.0,
            "SPIKING":   0.0,
        }.get(iv_behavior, 0.50)

        or_condition = signals.get("or_condition")
        or_score = {
            "VERY_NARROW": 1.0,
            "NARROW":      0.85,
            "MODERATE":    0.65,
            "WIDE":        0.35,
            "VERY_WIDE":   0.10,
            None:          0.30,
        }.get(or_condition, 0.30)

        # Weighted average
        total_weight = w_vrp + w_price + w_pos + w_iv + w_or
        if total_weight <= 0:
            return ConfidenceLevel.NONE, 0.0

        weighted_score = (
            vrp_score  * w_vrp  +
            price_score * w_price +
            pos_score  * w_pos  +
            iv_score   * w_iv   +
            or_score   * w_or
        ) / total_weight

        weighted_score = round(max(0.0, min(1.0, weighted_score)), 3)

        if weighted_score >= 0.75:
            return ConfidenceLevel.HIGH, weighted_score
        if weighted_score >= 0.50:
            return ConfidenceLevel.MEDIUM, weighted_score
        if weighted_score >= 0.30:
            return ConfidenceLevel.LOW, weighted_score
        return ConfidenceLevel.NONE, weighted_score

    # ─────────────────────────────────────────────────────────────────────
    # FINAL REGIME DECISION TREE
    # ─────────────────────────────────────────────────────────────────────

    def classify_final(
        self,
        vol:      VolatilityRegime,
        price:    PriceRegime,
        pos:      PositioningRegime,
        conf:     ConfidenceLevel,
        signals:  dict,
        event_day: bool,
        event_name: str,
        _test_time: dtime = None,
    ) -> Tuple[FinalRegime, str, bool]:
        """
        Apply the complete decision tree to produce the final regime.

        Returns (FinalRegime, notes_string, block_new_entries)

        Decision tree (hard rules, no overrides):
        1. Hard blocks: ABORT, OBSERVING, CHOPPY, NEUTRAL/BUY_OPTIONS, NONE confidence
        2. Time gates: before 09:45, after 14:30
        3. DTE filter: DTE 3+ = NO_TRADE, DTE 2 needs STRONG_SELL
        4. Price regime → structure (RANGE/UPTREND/DOWNTREND/STRONG_*)
        5. Positioning → strike side modifier
        6. Special contexts: event day, gap day, Tuesday afternoon pin
        """
        current_time = _test_time if _test_time is not None else now_ist().time()
        dte          = signals.get("actual_dte")
        spot         = float(signals.get("spot") or 0.0)
        notes_parts: List[str] = []

        if event_day:
            notes_parts.append(f"EVENT:{event_name}")

        # ── Hard Block 1: ABORT ───────────────────────────────────────────
        if vol == VolatilityRegime.ABORT:
            return (
                FinalRegime.ABORT,
                "ABORT:VIX_EMERGENCY — new entries blocked, positions managed by own rules",
                True,  # block_new_entries = True
            )

        # ── Hard Block 2: Price regime blocks ─────────────────────────────
        if price == PriceRegime.OBSERVING:
            return FinalRegime.NO_TRADE, "NO_TRADE:OR_NOT_ESTABLISHED", False

        if price == PriceRegime.CHOPPY:
            return FinalRegime.NO_TRADE, "NO_TRADE:CHOPPY_MARKET", False

        # ── Hard Block 3: Volatility blocks ───────────────────────────────
        if vol in (VolatilityRegime.NEUTRAL, VolatilityRegime.BUY_OPTIONS):
            return FinalRegime.NO_TRADE, f"NO_TRADE:VOL_{vol.value}", False

        # ── Hard Block 4: Confidence block ────────────────────────────────
        if conf == ConfidenceLevel.NONE:
            return FinalRegime.NO_TRADE, "NO_TRADE:CONFIDENCE_NONE", False

        # ── Time Gate 1: Before 09:45 ─────────────────────────────────────
        if current_time < time(9, 45):
            return FinalRegime.NO_TRADE, "NO_TRADE:BEFORE_09:45", False

        # ── Time Gate 2: After 14:30 ──────────────────────────────────────
        if current_time > time(14, 30):
            return FinalRegime.NO_TRADE, "NO_TRADE:PAST_14:30", False

        # ── DTE Filter ────────────────────────────────────────────────────
        if dte is not None and dte > 6:
            return FinalRegime.NO_TRADE, f"NO_TRADE:DTE_{dte}_ABOVE_MAX_6", False

        if dte is not None and dte >= 4:
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

        # ── Event Day Rules ───────────────────────────────────────────────
        if event_day and self.config.defined_risk_only_on_event:
            notes_parts.append("EVENT:DEFINED_RISK_ONLY")
            # Only allow RANGE regime on event day, and only with HIGH confidence
            if price != PriceRegime.RANGE:
                return (
                    FinalRegime.NO_TRADE,
                    " | ".join(notes_parts + ["EVENT:ONLY_RANGE_ALLOWED"]),
                    False,
                )
            if conf != ConfidenceLevel.HIGH:
                return (
                    FinalRegime.NO_TRADE,
                    " | ".join(notes_parts + ["EVENT:REQUIRES_HIGH_CONFIDENCE"]),
                    False,
                )

        # ── Price Regime → Structure ──────────────────────────────────────
        if price == PriceRegime.RANGE:
            final, note = self._classify_range(vol, pos, conf, signals)
            notes_parts.append(note)
            return final, " | ".join(notes_parts), False

        if price in (PriceRegime.DOWNTREND, PriceRegime.STRONG_DOWNTREND):
            final, note = self._classify_downtrend(vol, pos, conf, signals, price)
            notes_parts.append(note)
            return final, " | ".join(notes_parts), False

        if price in (PriceRegime.UPTREND, PriceRegime.STRONG_UPTREND):
            final, note = self._classify_uptrend(vol, pos, conf, signals, price)
            notes_parts.append(note)
            return final, " | ".join(notes_parts), False

        return FinalRegime.NO_TRADE, "NO_TRADE:NO_REGIME_MATCH", False

    def _classify_range(
        self,
        vol:     VolatilityRegime,
        pos:     PositioningRegime,
        conf:    ConfidenceLevel,
        signals: dict,
    ) -> Tuple[FinalRegime, str]:
        """
        Classify final regime when price is RANGE.
        RANGE → sell both sides (condor/fly) based on positioning.
        """
        dte          = signals.get("actual_dte")
        or_condition = signals.get("or_condition", "MODERATE")
        adx_15       = float(signals.get("adx_15") or 0.0)
        spot         = float(signals.get("spot") or 0.0)
        max_pain     = float(signals.get("max_pain") or 0.0)
        current_time = now_ist().time()
        r_str        = float(signals.get("resistance_strength") or 0.0)

        # ── DTE 2 exception ───────────────────────────────────────────────
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    pos == PositioningRegime.STRONG_RANGE):
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    f"RANGE_DTE2_EXCEPTION_STRONG_SELL_STRONG_RANGE",
                )
            # v3.1: a second, narrower DTE 2 route. Friday is DTE 2 on the
            # Tuesday-expiry calendar and the old single condition (STRONG
            # sell AND STRONG range together) is rare enough that Friday was
            # effectively closed too.
            if (vol == VolatilityRegime.SELL_PREMIUM and
                    pos == PositioningRegime.RANGE and
                    or_condition in ("VERY_NARROW", "NARROW") and
                    adx_15 < self.config.adx_trend_threshold):
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    "RANGE_DTE2_SELL_PREMIUM_NARROW_OR_FLAT_ADX",
                )
            return FinalRegime.NO_TRADE, "RANGE_DTE2_NO_EXCEPTION"

        # ── DTE 3 / DTE 4 new-cycle branch (v3.1) ─────────────────────────
        # Wednesday is DTE 4 and Thursday is DTE 3 on the Tuesday-expiry
        # calendar. Previously neither could ever produce a tradeable regime
        # (there was no branch here, and strategy_engine capped the condor at
        # DTE 2 anyway), so 40% of the trading week was structurally dead.
        # These are legitimate premium-selling sessions — a fresh weekly
        # contract carries the most vega and the widest credit — but they hold
        # overnight gap risk into the next session, so the bar is deliberately
        # higher than for DTE 0/1: rich VRP, genuine range positioning, a
        # contained opening range and a flat trend reading are ALL required.
        if dte in (3, 4):
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

        # ── Wide OR blocks condor ─────────────────────────────────────────
        if or_condition in ("WIDE", "VERY_WIDE") and pos not in (
            PositioningRegime.STRONG_RANGE,
        ):
            return FinalRegime.NO_TRADE, f"RANGE_WIDE_OR_{or_condition}_NO_TRADE"

        # ── UNCLEAR positioning ───────────────────────────────────────────
        if pos == PositioningRegime.UNCLEAR:
            if vol == VolatilityRegime.STRONG_SELL_PREMIUM and dte in (0, 1):
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    "RANGE_UNCLEAR_POS_STRONG_SELL_HALF_SIZE",
                )
            return FinalRegime.NO_TRADE, "RANGE_UNCLEAR_POSITIONING_NO_TRADE"

        # ── STRONG_RANGE or RANGE positioning → condor/fly ───────────────
        if pos in (PositioningRegime.STRONG_RANGE, PositioningRegime.RANGE):
            if (dte == 0 and
                    current_time >= time(13, 0) and
                    max_pain > 0 and
                    abs(spot - max_pain) <= 80 and
                    r_str >= 1.5):
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    "RANGE_TUESDAY_AFTERNOON_PIN",
                )
            return (
                FinalRegime.PREMIUM_SELL_RANGE,
                f"RANGE_{pos.value}_{or_condition}",
            )

        # ── BULLISH positioning → bull put (unless spot below OR midpoint) ─
        if pos == PositioningRegime.BULLISH:
            or_high = float(signals.get("or_high") or spot)
            or_low  = float(signals.get("or_low")  or spot)
            or_mid  = (or_high + or_low) / 2.0
            if spot >= or_mid:
                return (
                    FinalRegime.PREMIUM_SELL_BULL,
                    "RANGE_BULLISH_SPOT_ABOVE_OR_MID",
                )
            else:
                # Spot below OR midpoint overrides bullish positioning
                return (
                    FinalRegime.PREMIUM_SELL_BEAR,
                    "RANGE_BULLISH_SPOT_BELOW_OR_MID_OVERRIDE",
                )

        # ── BEARISH positioning → bear call (unless spot above OR midpoint) ─
        if pos == PositioningRegime.BEARISH:
            or_high = float(signals.get("or_high") or spot)
            or_low  = float(signals.get("or_low")  or spot)
            or_mid  = (or_high + or_low) / 2.0
            if spot <= or_mid:
                return (
                    FinalRegime.PREMIUM_SELL_BEAR,
                    "RANGE_BEARISH_SPOT_BELOW_OR_MID",
                )
            else:
                return (
                    FinalRegime.PREMIUM_SELL_BULL,
                    "RANGE_BEARISH_SPOT_ABOVE_OR_MID_OVERRIDE",
                )

        return FinalRegime.NO_TRADE, "RANGE_NO_MATCH"

    def _classify_downtrend(
        self,
        vol:     VolatilityRegime,
        pos:     PositioningRegime,
        conf:    ConfidenceLevel,
        signals: dict,
        price:   PriceRegime,
    ) -> Tuple[FinalRegime, str]:
        """
        Classify final regime when price is DOWNTREND or STRONG_DOWNTREND.
        Hard rule: SELL CALLS ONLY — never sell puts in downtrend.
        """
        dte    = signals.get("actual_dte")
        adx_15 = float(signals.get("adx_15") or 0.0)

        # DTE 2 exception
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    adx_15 > 30):
                return (
                    FinalRegime.PREMIUM_SELL_BEAR,
                    f"DOWNTREND_DTE2_EXCEPTION_ADX_{adx_15:.0f}",
                )
            return FinalRegime.NO_TRADE, "DOWNTREND_DTE2_NO_EXCEPTION"

        # Positioning conflict: BULLISH positioning in downtrend
        if pos == PositioningRegime.BULLISH:
            if conf != ConfidenceLevel.HIGH:
                return (
                    FinalRegime.NO_TRADE,
                    "DOWNTREND_BULLISH_CONFLICT_NEEDS_HIGH_CONFIDENCE",
                )
            # Price overrides positioning — still bear call but reduced size
            return (
                FinalRegime.PREMIUM_SELL_BEAR,
                f"DOWNTREND_BULLISH_CONFLICT_PRICE_OVERRIDES_ADX_{adx_15:.0f}",
            )

        # Standard downtrend → bear call spread
        trend_label = "STRONG_DOWNTREND" if price == PriceRegime.STRONG_DOWNTREND else "DOWNTREND"
        return (
            FinalRegime.PREMIUM_SELL_BEAR,
            f"{trend_label}_ADX_{adx_15:.0f}_{pos.value}",
        )

    def _classify_uptrend(
        self,
        vol:     VolatilityRegime,
        pos:     PositioningRegime,
        conf:    ConfidenceLevel,
        signals: dict,
        price:   PriceRegime,
    ) -> Tuple[FinalRegime, str]:
        """
        Classify final regime when price is UPTREND or STRONG_UPTREND.
        Hard rule: SELL PUTS ONLY — never sell calls in uptrend.
        """
        dte    = signals.get("actual_dte")
        adx_15 = float(signals.get("adx_15") or 0.0)

        # DTE 2 exception
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    adx_15 > 30):
                return (
                    FinalRegime.PREMIUM_SELL_BULL,
                    f"UPTREND_DTE2_EXCEPTION_ADX_{adx_15:.0f}",
                )
            return FinalRegime.NO_TRADE, "UPTREND_DTE2_NO_EXCEPTION"

        # Positioning conflict: BEARISH positioning in uptrend
        if pos == PositioningRegime.BEARISH:
            if conf != ConfidenceLevel.HIGH:
                return (
                    FinalRegime.NO_TRADE,
                    "UPTREND_BEARISH_CONFLICT_NEEDS_HIGH_CONFIDENCE",
                )
            return (
                FinalRegime.PREMIUM_SELL_BULL,
                f"UPTREND_BEARISH_CONFLICT_PRICE_OVERRIDES_ADX_{adx_15:.0f}",
            )

        # Standard uptrend → bull put spread
        trend_label = "STRONG_UPTREND" if price == PriceRegime.STRONG_UPTREND else "UPTREND"
        return (
            FinalRegime.PREMIUM_SELL_BULL,
            f"{trend_label}_ADX_{adx_15:.0f}_{pos.value}",
        )

    # ─────────────────────────────────────────────────────────────────────
    # SIZE COMPUTATION
    # ─────────────────────────────────────────────────────────────────────

    def compute_final_size(
        self,
        vol:       VolatilityRegime,
        price:     PriceRegime,
        pos:       PositioningRegime,
        conf:      ConfidenceLevel,
        signals:   dict,
        event_day: bool,
        borderline_sell: bool,
    ) -> Tuple[float, float, float]:
        """
        Compute final size multiplier using the single formula:
        final_size = day_size × vix_mult × confidence_mult × dte_mult × event_mult

        Then apply modifiers:
        - Positioning conflict: × 0.75
        - UNCLEAR positioning: × 0.50
        - Borderline sell: × 0.50
        - OR condition: moderate × 0.75, wide × 0.50

        Returns (final_size, raw_size, conflict_reduction)
        raw_size = before modifiers
        conflict_reduction = the modifier applied (1.0 = no reduction)
        """
        vix       = float(signals.get("vix") or 11.0)
        dte       = signals.get("actual_dte")
        day_label = signals.get("day_label") or "TUESDAY"
        or_condition = signals.get("or_condition") or "MODERATE"

        # ── day_size from calibration ─────────────────────────────────────
        # v3.9: the fallback was event_size_multiplier (a budget/event-day
        # reducer). On any cycle where the calibrator had no valid state -
        # live start-of-day, tier-0, and every backtest replay - that
        # silently sized a normal Tuesday at 25%, quartering the book. The
        # fallback is now the per-weekday normal size in Config, which
        # mirrors the calibration defaults.
        day_size_map = {
            "MONDAY":    self._t("day_size_monday",    "day_size_monday",    0.60),
            "TUESDAY":   self._t("day_size_tuesday",   "day_size_tuesday",   0.85),
            "WEDNESDAY": self._t("day_size_wednesday", "day_size_wednesday", 0.65),
            "THURSDAY":  self._t("day_size_thursday",  "day_size_thursday",  0.65),
            "FRIDAY":    self._t("day_size_friday",    "day_size_friday",    0.55),
        }
        if self.cal:
            day_size_map = {
                "MONDAY":    float(self.cal.day_size_monday),
                "TUESDAY":   float(self.cal.day_size_tuesday),
                "WEDNESDAY": float(self.cal.day_size_wednesday),
                "THURSDAY":  float(self.cal.day_size_thursday),
                "FRIDAY":    float(self.cal.day_size_friday),
            }
        base_size = day_size_map.get(day_label, 0.55)

        # ── vix_mult ──────────────────────────────────────────────────────
        # VIX adjusts size only — NEVER blocks trade
        # SUPPRESSED/LOW = 2026 normal = full size
        vix_suppressed = self.config.vix_suppressed   # 12.5
        vix_low        = self.config.vix_low           # 16.0
        vix_normal     = self.config.vix_normal        # 22.0
        vix_elevated   = self.config.vix_elevated      # 28.0

        if vix < vix_suppressed:
            vix_mult = 1.0   # SUPPRESSED = 2026 normal, full size
        elif vix < vix_low:
            vix_mult = 1.0   # LOW
        elif vix < vix_normal:
            vix_mult = 0.75  # NORMAL
        elif vix < vix_elevated:
            vix_mult = 0.50  # ELEVATED
        else:
            vix_mult = 0.25  # HIGH

        # ── confidence_mult ───────────────────────────────────────────────
        conf_mult = {
            ConfidenceLevel.HIGH:   1.0,
            ConfidenceLevel.MEDIUM: 0.50,
            ConfidenceLevel.LOW:    0.25,
            ConfidenceLevel.NONE:   0.0,
        }.get(conf, 0.0)

        # ── dte_mult ──────────────────────────────────────────────────────
        if dte == 0:
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

        # ── event_mult ────────────────────────────────────────────────────
        event_mult = self.config.event_size_multiplier if event_day else 1.0

        # ── Raw size (before modifiers) ───────────────────────────────────
        raw_size = base_size * vix_mult * conf_mult * dte_mult * event_mult
        raw_size = round(max(raw_size, 0.0), 3)

        # ── Modifiers (v3.1: compounding, not min()) ──────────────────────
        # These are independent sources of risk. Taking min() meant that once
        # any one of them fired the rest were free — an unclear positioning
        # read, a very wide opening range and a borderline VRP all at once
        # produced exactly the same size as any one of them alone. A book run
        # that way is systematically largest when conditions are worst.
        #
        # Each factor keeps its ORIGINAL value and they are now multiplied.
        # That ordering matters: with one condition active the result is
        # byte-identical to the old min() behaviour, so every documented
        # invariant still holds (UNCLEAR still reduces to 0.50, a VERY_WIDE
        # opening range still reduces to 0.25). With several active the
        # result is strictly more conservative, which is the entire point.
        # An earlier draft softened each factor to "compensate" for
        # compounding — that made the single-condition case LARGER than
        # before, i.e. the opposite of the intent, and it broke the engine's
        # own UNCLEAR <= 0.50 contract.
        conflict_reduction = 1.0

        # Positioning conflict: price and positioning disagree
        price_is_down = price in (PriceRegime.DOWNTREND, PriceRegime.STRONG_DOWNTREND)
        price_is_up   = price in (PriceRegime.UPTREND,   PriceRegime.STRONG_UPTREND)

        if price_is_down and pos == PositioningRegime.BULLISH:
            conflict_reduction *= 0.75
        elif price_is_up and pos == PositioningRegime.BEARISH:
            conflict_reduction *= 0.75

        # UNCLEAR positioning
        if pos == PositioningRegime.UNCLEAR:
            conflict_reduction *= 0.50

        # Borderline sell
        if borderline_sell:
            conflict_reduction *= 0.50

        # OR condition modifier
        if or_condition == "MODERATE":
            conflict_reduction *= 0.75
        elif or_condition == "WIDE":
            conflict_reduction *= 0.50
        elif or_condition == "VERY_WIDE":
            conflict_reduction *= 0.25

        # Strong trend → reduce size (more risk)
        if price in (PriceRegime.STRONG_UPTREND, PriceRegime.STRONG_DOWNTREND):
            conflict_reduction *= 0.75

        # Floor so a pile-up of modifiers still leaves a real (if small)
        # position rather than a meaningless one.
        conflict_reduction = max(round(conflict_reduction, 4), 0.15)

        final_size = round(raw_size * conflict_reduction, 3)
        final_size = max(final_size, 0.0)

        return final_size, raw_size, conflict_reduction


# ─────────────────────────────────────────────────────────────────────────────
# REGIME ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class RegimeEngine:
    """
    Main regime engine.
    Orchestrates calibration, classification, persistence, and regime change logging.

    Usage:
    1. Instantiate once at startup
    2. Call process_signals(signals) every cycle
    3. Call run_calibration() at startup and after market close
    4. Read get_current_regime() for the latest snapshot
    """

    def __init__(
        self,
        config:         Config,
        db:             Database,
        market_engine:  MarketDataEngine,
        logger,
    ):
        self.config        = config
        self.db            = db
        self.market_engine = market_engine
        self.logger        = logger

        # Sub-components
        self.calibrator  = CalibrationEngine(db, config, logger)
        self.classifier  = RegimeClassifier(config, self.calibrator.state, logger)

        # State
        self._current_regime: Optional[RegimeSnapshot] = None
        self._pending_regime:  Optional[RegimeSnapshot] = None
        self._pending_count:   int = 0
        self._last_reset_date: Optional[date] = None
        self._event_day:       bool = False
        self._event_name:      str  = ""
        self._straddle_history: List[Tuple[datetime, float]] = []

        self.logger.info("RegimeEngine v3.0 initialized (regime-based, not VIX-based)")

    # ─────────────────────────────────────────────────────────────────────
    # DAILY RESET
    # ─────────────────────────────────────────────────────────────────────

    def _daily_reset_if_needed(self) -> None:
        """Reset intraday state at the start of each new trading day."""
        today = today_ist()
        if self._last_reset_date == today:
            return

        self._pending_regime    = None
        self._pending_count     = 0
        self._straddle_history  = []

        event_str        = ExpiryCalendar.is_event_day(today)
        self._event_day  = bool(event_str)
        self._event_name = event_str

        if self._event_day:
            self.logger.warning(
                f"EVENT DAY: {self._event_name} | "
                f"Size reduced {int((1 - self.config.event_size_multiplier) * 100)}% | "
                f"Defined risk only"
            )

        self._last_reset_date = today
        self.logger.info(f"RegimeEngine daily reset for {today}")

    # ─────────────────────────────────────────────────────────────────────
    # STRADDLE EXPLOSION CHECK
    # ─────────────────────────────────────────────────────────────────────

    def _check_straddle_explosion(self, straddle: float) -> bool:
        """
        Check if the ATM straddle has exploded (rapid expansion).
        Two checks:
        1. vs opening straddle: > straddle_explosion_pct% increase
        2. vs N minutes ago: > straddle_roc_alert_pct% increase in window

        Returns True if straddle has exploded (triggers NO_TRADE).
        Does NOT trigger ABORT — straddle explosion is not a genuine emergency.
        """
        if straddle <= 0:
            return False

        opening_straddle = self.market_engine.state.get("_straddle_open_for_regime", 0)
        if opening_straddle > 0:
            chg = (straddle - opening_straddle) / opening_straddle * 100
            if chg >= self.config.straddle_explosion_pct:
                self.logger.warning(
                    f"Straddle explosion: +{chg:.1f}% from open "
                    f"({opening_straddle:.0f} → {straddle:.0f})"
                )
                return True

        # Rate of change check
        now_dt  = now_ist()
        cutoff  = now_dt.timestamp() - self.config.straddle_roc_window_min * 60
        self._straddle_history.append((now_dt, straddle))
        self._straddle_history = [
            (t, v) for t, v in self._straddle_history
            if t.timestamp() >= cutoff
        ]

        if len(self._straddle_history) >= 2:
            oldest = self._straddle_history[0][1]
            if oldest > 0:
                roc = (straddle - oldest) / oldest * 100
                if roc >= self.config.straddle_roc_alert_pct:
                    self.logger.warning(
                        f"Straddle ROC alert: +{roc:.1f}% in "
                        f"{self.config.straddle_roc_window_min}min"
                    )
                    return True

        return False

    # ─────────────────────────────────────────────────────────────────────
    # REGIME PERSISTENCE FILTER
    # ─────────────────────────────────────────────────────────────────────

    def _apply_persistence_filter(
        self, new_regime: RegimeSnapshot
    ) -> RegimeSnapshot:
        """
        Apply regime persistence filter to prevent whipsawing.
        A regime change requires regime_persistence_cycles consecutive
        cycles with the same new regime before it is confirmed.

        Exceptions (immediate change):
        - ABORT: always immediate
        - NO_TRADE: always immediate
        - First regime of the day: immediate
        - Same regime as current: immediate (no change needed)
        """
        # Always immediate
        if new_regime.final_regime in (
            FinalRegime.ABORT.value, FinalRegime.NO_TRADE.value
        ):
            self._pending_regime = None
            self._pending_count  = 0
            return new_regime

        # First regime of the day
        if self._current_regime is None:
            return new_regime

        # Different day
        if self._current_regime.timestamp.date() != today_ist():
            self._pending_regime = None
            self._pending_count  = 0
            return new_regime

        # Same regime — no change needed
        if new_regime.final_regime == self._current_regime.final_regime:
            self._pending_regime = None
            self._pending_count  = 0
            return new_regime

        # New regime candidate
        if (self._pending_regime is not None and
                self._pending_regime.final_regime == new_regime.final_regime):
            self._pending_count += 1
            if self._pending_count >= self.config.regime_persistence_cycles:
                self.logger.info(
                    f"Regime confirmed after {self._pending_count} cycles: "
                    f"{new_regime.final_regime}"
                )
                self._pending_regime = None
                self._pending_count  = 0
                return new_regime
            self.logger.debug(
                f"Regime pending ({self._pending_count}/"
                f"{self.config.regime_persistence_cycles}): "
                f"{new_regime.final_regime}"
            )
            return self._current_regime
        else:
            # New candidate
            self._pending_regime = new_regime
            self._pending_count  = 1
            self.logger.debug(
                f"New regime candidate (1/{self.config.regime_persistence_cycles}): "
                f"{new_regime.final_regime}"
            )
            return self._current_regime

    # ─────────────────────────────────────────────────────────────────────
    # MAIN CLASSIFICATION
    # ─────────────────────────────────────────────────────────────────────

    def calculate_regime(self, signals: dict) -> RegimeSnapshot:
        """
        Run the complete regime classification for one cycle.
        This is the pure classification — no persistence filter applied.
        Returns a RegimeSnapshot with all dimensions classified.
        """
        self._daily_reset_if_needed()

        # Update classifier with latest calibration
        self.classifier.cal = self.calibrator.state

        ts           = now_ist()
        trading_date = today_ist().isoformat()
        today_d      = today_ist()
        dte          = ExpiryCalendar.get_dte(today_d)
        day_type     = ExpiryCalendar.get_day_type(today_d)
        day_label    = ExpiryCalendar.get_day_label(today_d)

        # Straddle explosion check (before vol classification)
        straddle = float(signals.get("atm_straddle_price") or 0.0)
        if self._check_straddle_explosion(straddle):
            # Straddle explosion → NO_TRADE (not ABORT — positions still managed)
            return self._build_snapshot(
                ts, trading_date, day_type, dte, day_label,
                VolatilityRegime.NEUTRAL,
                PriceRegime.RANGE,
                PositioningRegime.UNCLEAR,
                ConfidenceLevel.NONE,
                0.0,
                0.0, 0.0, 1.0,
                FinalRegime.NO_TRADE,
                "NO_TRADE:STRADDLE_EXPLOSION",
                False, False, signals,
            )

        # ── Classify four dimensions ──────────────────────────────────────
        prev_day_vix_close = self.market_engine.state.get("prev_day_vix_close")
        vix_fail_count     = getattr(self.market_engine, "_vix_fail_count", 0)

        vol, vol_details = self.classifier.classify_volatility(
            signals, vix_fail_count, prev_day_vix_close
        )
        price   = self.classifier.classify_price(signals)
        pos     = self.classifier.classify_positioning(signals)
        conf, conf_score = self.classifier.compute_confidence(
            vol, price, pos, signals
        )

        # ── Borderline sell flag ──────────────────────────────────────────
        borderline_sell = (vol == VolatilityRegime.BORDERLINE_SELL)
        # Treat BORDERLINE_SELL as SELL_PREMIUM for downstream logic
        effective_vol = (
            VolatilityRegime.SELL_PREMIUM
            if borderline_sell
            else vol
        )

        # ── Final regime decision tree ────────────────────────────────────
        final, notes, block_new_entries = self.classifier.classify_final(
            effective_vol, price, pos, conf, signals,
            self._event_day, self._event_name,
        )

        # ── Size computation ──────────────────────────────────────────────
        final_size, raw_size, conflict_reduction = self.classifier.compute_final_size(
            effective_vol, price, pos, conf, signals,
            self._event_day, borderline_sell,
        )

        return self._build_snapshot(
            ts, trading_date, day_type, dte, day_label,
            vol, price, pos, conf, conf_score,
            final_size, raw_size, conflict_reduction,
            final, notes, block_new_entries, borderline_sell, signals,
        )

    def _build_snapshot(
        self,
        ts:                 datetime,
        trading_date:       str,
        day_type:           str,
        dte:                int,
        day_label:          str,
        vol:                VolatilityRegime,
        price:              PriceRegime,
        pos:                PositioningRegime,
        conf:               ConfidenceLevel,
        conf_score:         float,
        final_size:         float,
        raw_size:           float,
        conflict_reduction: float,
        final:              FinalRegime,
        notes:              str,
        block_new_entries:  bool,
        borderline_sell:    bool,
        signals:            dict,
    ) -> RegimeSnapshot:
        """Build a RegimeSnapshot from classified dimensions and signals."""
        cal = self.calibrator.state

        # OI wall strength (max of resistance and support)
        r_str = float(signals.get("resistance_strength") or 0.0)
        s_str = float(signals.get("support_strength")    or 0.0)
        oi_wall_strength = max(r_str, s_str)

        # Max pain distance
        spot     = float(signals.get("spot") or 0.0)
        max_pain = float(signals.get("max_pain") or 0.0)
        mp_dist  = abs(spot - max_pain) if (spot > 0 and max_pain > 0) else 0.0

        return RegimeSnapshot(
            timestamp=ts,
            trading_date=trading_date,
            day_type=day_type,
            dte=dte,
            day_label=day_label,
            event_day=self._event_day,
            event_name=self._event_name,
            defined_risk_only=(
                self._event_day and self.config.defined_risk_only_on_event
            ),
            vol_regime=vol.value,
            price_regime=price.value,
            positioning_regime=pos.value,
            confidence_level=conf.value,
            confidence_score=conf_score,
            final_regime=final.value,
            final_regime_notes=notes,
            block_new_entries=block_new_entries,
            size_multiplier=final_size,
            raw_size_multiplier=raw_size,
            borderline_sell=borderline_sell,
            size_conflict_reduction=conflict_reduction,
            vix_level=float(signals.get("vix") or 0.0),
            vrp_raw=signals.get("vrp_raw"),
            vrp_smoothed=signals.get("vrp_smoothed"),
            atm_iv_pct=(
                float(signals["atm_iv"]) * 100
                if signals.get("atm_iv") else None
            ),
            parkinson_rv_pct=(
                float(signals["parkinson_rv"]) * 100
                if signals.get("parkinson_rv") else None
            ),
            iv_behavior=signals.get("iv_behavior", "UNKNOWN"),
            day_move_used_pct=float(signals.get("day_move_used_pct") or 0.0),
            opening_straddle_pts=float(signals.get("opening_straddle_pts") or 0.0),
            adx_15=float(signals.get("adx_15") or 0.0),
            adx_60=float(signals.get("adx_60") or 0.0),
            ema_structure=signals.get("ema_structure", "INSUFFICIENT_DATA"),
            hh_hl=signals.get("hh_hl", "INSUFFICIENT_DATA"),
            or_condition=signals.get("or_condition"),
            or_computed=bool(signals.get("or_computed", False)),
            choppy_detected=bool(signals.get("choppy_detected", False)),
            pcr=signals.get("pcr"),
            skew_ratio=signals.get("skew_ratio"),
            oi_change_pct=float(signals.get("oi_change_pct") or 0.0),
            oi_wall_strength=oi_wall_strength,
            max_pain_distance=mp_dist,
            gap_fade_opportunity=bool(signals.get("gap_fade_opportunity", False)),
            is_calibrated=bool(cal and cal.is_calibrated),
            calibration_tier=int(cal.calibration_tier) if cal else 0,
        )

    # ─────────────────────────────────────────────────────────────────────
    # PROCESS SIGNALS (main entry point)
    # ─────────────────────────────────────────────────────────────────────

    def process_signals(self, signals: dict) -> RegimeSnapshot:
        """
        Main entry point called every cycle from main.py.
        1. Calculate raw regime
        2. Apply persistence filter
        3. Log regime changes
        4. Persist to database
        5. Update signals dict with regime outputs
        6. Return confirmed RegimeSnapshot
        """
        self._daily_reset_if_needed()

        raw_regime = self.calculate_regime(signals)
        confirmed  = self._apply_persistence_filter(raw_regime)

        # Log regime changes
        if (self._current_regime is None or
                confirmed.final_regime != self._current_regime.final_regime):
            self._log_regime_change(self._current_regime, confirmed)

        # Persist to database (only when regime changes or every 10 cycles)
        should_persist = (
            self._current_regime is None or
            confirmed.final_regime != self._current_regime.final_regime or
            raw_regime.final_regime != confirmed.final_regime
        )
        if should_persist:
            self._persist_regime_decision(confirmed)

        self._current_regime = confirmed

        # Update signals dict with regime outputs (for strategy_engine.py)
        signals["vol_regime"]          = confirmed.vol_regime
        signals["price_regime"]        = confirmed.price_regime
        signals["positioning_regime"]  = confirmed.positioning_regime
        signals["confidence_level"]    = confirmed.confidence_level
        signals["confidence_score"]    = confirmed.confidence_score
        signals["final_regime"]        = confirmed.final_regime
        signals["final_regime_notes"]  = confirmed.final_regime_notes
        signals["size_multiplier"]     = confirmed.size_multiplier
        signals["raw_size_multiplier"] = confirmed.raw_size_multiplier
        signals["block_new_entries"]   = confirmed.block_new_entries
        signals["borderline_sell"]     = confirmed.borderline_sell
        signals["event_day"]           = confirmed.event_day
        signals["event_name"]          = confirmed.event_name
        signals["defined_risk_only"]   = confirmed.defined_risk_only
        signals["is_calibrated"]       = confirmed.is_calibrated
        signals["calibration_tier"]    = confirmed.calibration_tier

        return confirmed

    # ─────────────────────────────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────────────────────────────

    def _persist_regime_decision(self, r: RegimeSnapshot) -> None:
        """Persist regime decision to regime_decisions table."""
        try:
            ts = r.timestamp
            self.db.insert("regime_decisions", {
                "timestamp":             ts.isoformat(),
                "date":                  ts.date().isoformat(),
                "time":                  ts.strftime("%H:%M:%S"),
                "weekday":               ts.weekday(),
                "dte":                   r.dte,
                "day_type":              r.day_type,
                "event_day":             int(r.event_day),
                "event_name":            r.event_name,
                "defined_risk_only":     int(r.defined_risk_only),
                "vol_regime":            r.vol_regime,
                "price_regime":          r.price_regime,
                "positioning_regime":    r.positioning_regime,
                "final_regime":          r.final_regime,
                "confidence_level":      r.confidence_level,
                "confidence_score":      r.confidence_score,
                "size_multiplier":       r.size_multiplier,
                "block_new_entries":     int(r.block_new_entries),
                "vix_level":             r.vix_level,
                "vrp_raw":               r.vrp_raw,
                "vrp_smoothed":          r.vrp_smoothed,
                "atm_iv_pct":            r.atm_iv_pct,
                "parkinson_rv_pct":      r.parkinson_rv_pct,
                "adx_15":                r.adx_15,
                "adx_60":                r.adx_60,
                "ema_structure":         r.ema_structure,
                "pcr":                   r.pcr,
                "skew_ratio":            r.skew_ratio,
                "oi_change_pct":         r.oi_change_pct,
                "oi_wall_strength":      r.oi_wall_strength,
                "max_pain_distance":     r.max_pain_distance,
                "day_move_used_pct":     r.day_move_used_pct,
                "opening_straddle_pts":  r.opening_straddle_pts,
                "gap_fade_opportunity":  int(r.gap_fade_opportunity),
                "borderline_sell":       int(r.borderline_sell),
                "notes":                 r.final_regime_notes,
                "is_calibrated":         int(r.is_calibrated),
                "calibration_tier":      r.calibration_tier,
            })
        except Exception as e:
            self.logger.debug(f"Could not persist regime decision: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # LOGGING
    # ─────────────────────────────────────────────────────────────────────

    def _log_regime_change(
        self,
        old: Optional[RegimeSnapshot],
        new: RegimeSnapshot,
    ) -> None:
        """Log a regime change with full context."""
        if old is not None and old.final_regime == new.final_regime:
            return

        old_regime = old.final_regime if old else "NONE"

        def _fmt(v, fmt=".2f"):
            return f"{v:{fmt}}" if v is not None else "N/A"

        self.logger.info("─" * 60)
        self.logger.info(
            f"  REGIME: {old_regime} → {new.final_regime}"
        )
        if new.event_day:
            self.logger.warning(
                f"  EVENT: {new.event_name} | "
                f"SIZE ×{self.config.event_size_multiplier} | "
                f"DEFINED RISK ONLY: {new.defined_risk_only}"
            )
        self.logger.info(
            f"  Day={new.day_type} DTE={new.dte} Label={new.day_label}"
        )
        self.logger.info(
            f"  Vol={new.vol_regime} | "
            f"Price={new.price_regime} | "
            f"Pos={new.positioning_regime}"
        )
        self.logger.info(
            f"  Conf={new.confidence_level} ({new.confidence_score:.2f}) | "
            f"RawSize={_fmt(new.raw_size_multiplier)} | "
            f"FinalSize={_fmt(new.size_multiplier)} | "
            f"Conflict={_fmt(new.size_conflict_reduction)}"
        )
        self.logger.info(
            f"  VIX={_fmt(new.vix_level)} | "
            f"VRP_raw={_fmt(new.vrp_raw)} | "
            f"VRP_smooth={_fmt(new.vrp_smoothed)} | "
            f"IV_beh={new.iv_behavior}"
        )
        self.logger.info(
            f"  ADX15={_fmt(new.adx_15)} | "
            f"ADX60={_fmt(new.adx_60)} | "
            f"EMA={new.ema_structure} | "
            f"HH/HL={new.hh_hl}"
        )
        self.logger.info(
            f"  OR={new.or_condition} | "
            f"Choppy={new.choppy_detected} | "
            f"DayMove={_fmt(new.day_move_used_pct)}% | "
            f"Straddle={_fmt(new.opening_straddle_pts, '.0f')}pts"
        )
        self.logger.info(
            f"  PCR={_fmt(new.pcr, '.3f')} | "
            f"Skew={_fmt(new.skew_ratio, '.3f')} | "
            f"OI_chg={_fmt(new.oi_change_pct, '.2%')} | "
            f"OI_wall={_fmt(new.oi_wall_strength)}"
        )
        self.logger.info(
            f"  Calibrated={new.is_calibrated} Tier={new.calibration_tier} | "
            f"Borderline={new.borderline_sell}"
        )
        self.logger.info(f"  Notes: {new.final_regime_notes}")
        self.logger.info("─" * 60)

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC ACCESSORS
    # ─────────────────────────────────────────────────────────────────────

    def get_current_regime(self) -> Optional[RegimeSnapshot]:
        """Return the current confirmed regime snapshot."""
        return self._current_regime

    def run_calibration(self, force: bool = False) -> Optional[CalibrationState]:
        """
        Run calibration.
        force=True: run even outside market hours (e.g. at startup or EOD).
        force=False: only run during market hours.
        """
        if not force and not self._is_market_open():
            return None
        try:
            cal = self.calibrator.run()
            if cal:
                self.classifier.cal = cal
            return cal
        except Exception as e:
            self.logger.error(f"Calibration error: {e}", exc_info=True)
            return None

    def _is_market_open(self) -> bool:
        """Return True if NSE market is currently open."""
        if ExpiryCalendar.is_holiday(today_ist()):
            return False
        now = now_ist().time()
        return time(9, 15) <= now <= time(15, 30)


# ─────────────────────────────────────────────────────────────────────────────
# REGIME BRIDGE FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def merge_regime_into_signals(
    signals: dict,
    regime_snapshot: Optional[RegimeSnapshot],
) -> dict:
    """
    Merge RegimeSnapshot outputs into the signals dict.
    Called from main.py after process_signals().
    Returns enriched signals dict.
    """
    if regime_snapshot is None:
        return signals

    enriched = dict(signals)
    enriched["vol_regime"]          = regime_snapshot.vol_regime
    enriched["price_regime"]        = regime_snapshot.price_regime
    enriched["positioning_regime"]  = regime_snapshot.positioning_regime
    enriched["confidence_level"]    = regime_snapshot.confidence_level
    enriched["confidence_score"]    = regime_snapshot.confidence_score
    enriched["final_regime"]        = regime_snapshot.final_regime
    enriched["final_regime_notes"]  = regime_snapshot.final_regime_notes
    enriched["size_multiplier"]     = regime_snapshot.size_multiplier
    enriched["raw_size_multiplier"] = regime_snapshot.raw_size_multiplier
    enriched["block_new_entries"]   = regime_snapshot.block_new_entries
    enriched["borderline_sell"]     = regime_snapshot.borderline_sell
    enriched["event_day"]           = regime_snapshot.event_day
    enriched["event_name"]          = regime_snapshot.event_name
    enriched["defined_risk_only"]   = regime_snapshot.defined_risk_only
    enriched["is_calibrated"]       = regime_snapshot.is_calibrated
    enriched["calibration_tier"]    = regime_snapshot.calibration_tier
    return enriched


def is_regime_tradeable(signals: dict) -> bool:
    """Return True if the current regime allows new entries."""
    final_regime = signals.get("final_regime")
    if not final_regime:
        return False
    if final_regime in (FinalRegime.NO_TRADE.value, FinalRegime.ABORT.value):
        return False
    if signals.get("block_new_entries"):
        return False
    if signals.get("confidence_level") in (ConfidenceLevel.NONE.value, None):
        return False
    if float(signals.get("size_multiplier") or 0) <= 0:
        return False
    return True


def get_strategy_from_regime(signals: dict) -> str:
    """
    Map final_regime to strategy name.
    PREMIUM_SELL_RANGE → IRON_CONDOR or IRON_BUTTERFLY (decided by strategy_engine)
    PREMIUM_SELL_BULL  → BULL_PUT_SPREAD
    PREMIUM_SELL_BEAR  → BEAR_CALL_SPREAD
    """
    final_regime = signals.get("final_regime")
    mapping = {
        FinalRegime.PREMIUM_SELL_RANGE.value: None,  # strategy_engine decides condor vs fly
        FinalRegime.PREMIUM_SELL_BULL.value:  "BULL_PUT_SPREAD",
        FinalRegime.PREMIUM_SELL_BEAR.value:  "BEAR_CALL_SPREAD",
        FinalRegime.NO_TRADE.value:           "NO_TRADE",
        FinalRegime.ABORT.value:              "NO_TRADE",
    }
    return mapping.get(final_regime, "NO_TRADE") or "NO_TRADE"


# ─────────────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    from datetime import time as dtime
    import tempfile as _tf4
    from core import load_env_file, ENV_FILE, BASE_DIR
    _env4 = load_env_file(ENV_FILE)
    _prod4 = str(BASE_DIR / _env4.get("DB_PATH", "data/nifty_algo_v3.db"))
    """
    Standalone self-test for regime_engine.py.
    Tests: calibration loading, all four classifiers, final decision tree,
           size computation, persistence filter, regime change logging.
    Run: python regime_engine.py
    """
    print_section("NIFTY ALGO v3.0 — REGIME ENGINE SELF-TEST", char="#")

    from core import load_config, Database, RateLimiter, UpstoxClient, setup_logging

    config       = load_config()
    # v3.8: isolate this self-test from the live production
    # database. CalibrationEngine.run() persists calibration_state;
    # a self-test must never write to the production book. config
    # is left untouched; only the scratch Database is handed in.
    from pathlib import Path as _scratch_path
    db = Database(_scratch_path(_tf4.mkdtemp(
        prefix="regime_selftest_")) / "regime_selftest.db")

    logger       = setup_logging(db, config.log_dir)
    rate_limiter = RateLimiter(config.rate_limits)
    client       = UpstoxClient(config, rate_limiter, db, logger)
    market_engine = MarketDataEngine(config, db, client, rate_limiter, logger)

    # ── CalibrationEngine tests ───────────────────────────────────────────
    print_section("CalibrationEngine Tests")
    cal_engine = CalibrationEngine(db, config, logger)
    cal = cal_engine.run()

    if cal:
        print_kv_table({
            "Calibration Tier":    cal.calibration_tier,
            "Is Valid":            cal.is_calibrated,
            "N Trading Days":      cal.n_trading_days,
            "VRP Sell Threshold":  cal.vrp_sell_threshold,
            "VRP Fair Threshold":  cal.vrp_fair_threshold,
            "Day Size Tuesday":    cal.day_size_tuesday,
            "Day Size Monday":     cal.day_size_monday,
            "PCR Bullish":         cal.pcr_bullish_threshold,
            "PCR Bearish":         cal.pcr_bearish_threshold,
            "OI Wall Strong":      cal.oi_wall_strong_cal,
        }, title="Calibration State")
    else:
        print("  No calibration data — using defaults")
    print("  [OK] CalibrationEngine test passed")

    # ── RegimeClassifier unit tests ───────────────────────────────────────
    print_section("RegimeClassifier Unit Tests")
    classifier = RegimeClassifier(config, cal, logger)

    # Build a comprehensive mock signals dict
    def make_signals(**overrides) -> dict:
        base = {
            "trading_date":          today_ist().isoformat(),
            "day_label":             "TUESDAY",
            "vix":                   11.5,
            "prev_day_vix_close":    11.0,
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
            "orb_price_regime":      "RANGE",
            "adx_15":                14.0,
            "adx_60":                12.0,
            "adx_15_mature":         True,
            "adx_60_mature":         True,
            "adx_condition":         "FLAT",
            "ema_structure":         "NEUTRAL",
            "hh_hl":                 "NEUTRAL",
            "spot":                  24070.0,
            "pcr":                   0.95,
            "skew_ratio":            1.15,
            "oi_change_pct":         0.10,
            "resistance_strength":   2.8,
            "support_strength":      2.6,
            "total_ce_oi":           500000,
            "total_pe_oi":           480000,
            "resistance_oi":         120000,
            "support_oi":            110000,
            "chain_size":            71,
            "chain_stale":           False,
            "atm_straddle_price":    175.0,
            "max_pain":              24050.0,
            "actual_dte":            0,
            "circuit_breaker_suspected": False,
            "vix_spike_detected":    False,
            "gap_fade_opportunity":  False,
        }
        base.update(overrides)
        return base

    # ── Test 1: Volatility regime classification ──────────────────────────
    print("\n--- Volatility Regime Tests ---")

    # SELL_PREMIUM
    s1 = make_signals(vrp_smoothed=3.2, iv_behavior="STABLE")
    vol1, d1 = classifier.classify_volatility(s1, 0, 11.0)
    print(f"  VRP=3.2pp, STABLE → {vol1.value} (expect SELL_PREMIUM or STRONG)")
    assert vol1 in (VolatilityRegime.SELL_PREMIUM, VolatilityRegime.STRONG_SELL_PREMIUM), \
        f"Expected SELL_PREMIUM/STRONG, got {vol1}"

    # NEUTRAL (IV expanding)
    s2 = make_signals(vrp_smoothed=3.2, iv_behavior="EXPANDING")
    vol2, d2 = classifier.classify_volatility(s2, 0, 11.0)
    print(f"  VRP=3.2pp, EXPANDING → {vol2.value} (expect NEUTRAL)")
    assert vol2 == VolatilityRegime.NEUTRAL, f"Expected NEUTRAL, got {vol2}"

    # NEUTRAL (day move used)
    # v3.2: day_move_used_pct is no longer "percentage of the whole-day
    # straddle consumed" - it is the realised range as a percentage of
    # the range the market PRICED for the elapsed part of the session,
    # so 100 means "running exactly as priced" and the block threshold
    # moved from 60 to 125. The fixture is derived from the configured
    # threshold so it tests the behaviour rather than pinning a number.
    _dm_block = float(classifier.config.day_move_used_block_pct)
    s3 = make_signals(vrp_smoothed=3.2, day_move_used_pct=_dm_block + 15.0)
    vol3, d3 = classifier.classify_volatility(s3, 0, 11.0)
    print(
        f"  VRP=3.2pp, day_move={_dm_block + 15.0:.0f}% "
        f"(block={_dm_block:.0f}%) → {vol3.value} (expect NEUTRAL)"
    )
    assert vol3 == VolatilityRegime.NEUTRAL, f"Expected NEUTRAL, got {vol3}"

    # ABORT (real VIX spike)
    s4 = make_signals(vix=16.0)
    vol4, d4 = classifier.classify_volatility(s4, 0, 11.0)  # 11→16 = 45% spike
    print(f"  VIX=16, prev=11 (45% spike) → {vol4.value} (expect ABORT)")
    assert vol4 == VolatilityRegime.ABORT, f"Expected ABORT, got {vol4}"

    # ABORT (extreme VIX)
    s5 = make_signals(vix=25.0)
    vol5, d5 = classifier.classify_volatility(s5, 0, 11.0)
    print(f"  VIX=25 (extreme) → {vol5.value} (expect ABORT)")
    assert vol5 == VolatilityRegime.ABORT, f"Expected ABORT, got {vol5}"

    # v3.4 replaces the old s6 fixture. It asserted that 9.5pp of VRP at
    # 12.5% ATM IV on the expiry series is bad data; under the corrected
    # contract that is an ordinary 0DTE reading and must pass. The guard is
    # now pinned by the three cases that actually define it.

    # (a) a dead bar feed is still a data error, whatever the ratio says
    s6 = make_signals(vrp_raw=9.5, vrp_smoothed=9.5, parkinson_rv=0.0)
    vol6, d6 = classifier.classify_volatility(s6, 0, 11.0)
    print(f"  RV=0% (dead feed)    -> {vol6.value} (expect NEUTRAL - bad data)")
    assert vol6 == VolatilityRegime.NEUTRAL, f"Expected NEUTRAL for dead feed, got {vol6}"
    assert d6.get("trigger") == "VRP_DATA_ERROR_NEUTRAL", (
        f"A dead bar feed must trip the data-error guard, got {d6}"
    )
    assert d6.get("vrp_reason") == "rv_dead", f"Expected the absolute floor to fire, got {d6}"

    # (b) the measured 2026-09-08 condition: 28% ATM IV against 6% realised
    #     on the expiry series is a real variance premium, not a broken feed
    s6c = make_signals(vrp_raw=22.0, vrp_smoothed=22.0, atm_iv=0.28,
                       parkinson_rv=0.06, actual_dte=0)
    vol6c, d6c = classifier.classify_volatility(s6c, 0, 11.0)
    print(f"  VRP=22pp @ IV 28% 0DTE -> {vol6c.value} (expect NOT bad data)")
    assert d6c.get("trigger") != "VRP_DATA_ERROR_NEUTRAL", (
        f"A genuine 0DTE variance premium must not be called a data error, got {d6c}"
    )

    # (c) away from the expiry series the v3.1 ratio still applies
    s6d = make_signals(vrp_raw=11.0, vrp_smoothed=11.0, atm_iv=0.125,
                       parkinson_rv=0.015, actual_dte=2)
    vol6d, d6d = classifier.classify_volatility(s6d, 0, 11.0)
    print(f"  VRP=11pp @ IV 12.5% dte2 -> {vol6d.value} (expect NEUTRAL - bad data)")
    assert d6d.get("trigger") == "VRP_DATA_ERROR_NEUTRAL", (
        f"An impossible ratio off the expiry series is still a data error, got {d6d}"
    )

    # v3.1: the same 9.5pp VRP against a 22% ATM IV is a genuine, rich
    # variance risk premium (bound = 15.4pp), not a data error. Under the old
    # flat 8pp rule this was hard-blocked as NEUTRAL — the engine stood down
    # on exactly the days it existed to trade. It must NOT be blocked now.
    s6b = make_signals(vrp_raw=9.5, vrp_smoothed=9.5, atm_iv=0.22)
    vol6b, d6b = classifier.classify_volatility(s6b, 0, 11.0)
    print(f"  VRP=9.5pp @ IV 22%   → {vol6b.value} (expect NOT blocked as bad data)")
    assert d6b.get("trigger") != "VRP_DATA_ERROR_NEUTRAL", (
        f"Rich VRP at high IV must not be treated as a data error, got {d6b}"
    )

    # NEUTRAL (VRP too low)
    s7 = make_signals(vrp_smoothed=1.0, or_condition="MODERATE")
    vol7, d7 = classifier.classify_volatility(s7, 0, 11.0)
    print(f"  VRP=1.0pp, MODERATE OR → {vol7.value} (expect NEUTRAL)")
    assert vol7 == VolatilityRegime.NEUTRAL, f"Expected NEUTRAL, got {vol7}"

    print("  [OK] Volatility regime tests passed")

    # ── Test 2: Price regime classification ───────────────────────────────
    print("\n--- Price Regime Tests ---")

    # OBSERVING (OR not computed)
    p1 = classifier.classify_price(make_signals(or_computed=False))
    print(f"  OR not computed → {p1.value} (expect OBSERVING)")
    assert p1 == PriceRegime.OBSERVING, f"Expected OBSERVING, got {p1}"

    # CHOPPY
    p2 = classifier.classify_price(make_signals(choppy_detected=True))
    print(f"  Choppy detected → {p2.value} (expect CHOPPY)")
    assert p2 == PriceRegime.CHOPPY, f"Expected CHOPPY, got {p2}"

    # RANGE (flat ADX)
    p3 = classifier.classify_price(make_signals(adx_15=14.0, ema_structure="NEUTRAL"))
    print(f"  ADX=14, NEUTRAL EMA → {p3.value} (expect RANGE)")
    assert p3 == PriceRegime.RANGE, f"Expected RANGE, got {p3}"

    # UPTREND
    p4 = classifier.classify_price(make_signals(
        adx_15=28.0, ema_structure="BULLISH",
        spot=24200.0, or_high=24100.0, or_low=24040.0,
    ))
    print(f"  ADX=28, BULLISH EMA, spot above OR → {p4.value} (expect UPTREND)")
    assert p4 == PriceRegime.UPTREND, f"Expected UPTREND, got {p4}"

    # DOWNTREND
    p5 = classifier.classify_price(make_signals(
        adx_15=28.0, ema_structure="BEARISH",
        spot=23900.0, or_high=24100.0, or_low=24040.0,
    ))
    print(f"  ADX=28, BEARISH EMA, spot below OR → {p5.value} (expect DOWNTREND)")
    assert p5 == PriceRegime.DOWNTREND, f"Expected DOWNTREND, got {p5}"

    # STRONG_UPTREND
    p6 = classifier.classify_price(make_signals(
        adx_15=38.0, ema_structure="BULLISH",
        spot=24300.0, or_high=24100.0, or_low=24040.0,
    ))
    print(f"  ADX=38, BULLISH EMA, spot 200pts above OR → {p6.value} (expect STRONG_UPTREND)")
    assert p6 == PriceRegime.STRONG_UPTREND, f"Expected STRONG_UPTREND, got {p6}"

    print("  [OK] Price regime tests passed")

    # ── Test 3: Positioning regime classification ─────────────────────────
    print("\n--- Positioning Regime Tests ---")

    # STRONG_RANGE
    pos1 = classifier.classify_positioning(make_signals(
        resistance_strength=3.0, support_strength=2.8,
        pcr=0.95, oi_change_pct=0.12,
    ))
    print(f"  Strong walls, neutral PCR, OI building → {pos1.value} (expect STRONG_RANGE)")
    assert pos1 == PositioningRegime.STRONG_RANGE, f"Expected STRONG_RANGE, got {pos1}"

    # BULLISH
    pos2 = classifier.classify_positioning(make_signals(pcr=0.48))
    print(f"  PCR=0.48 (extreme greed/low PCR) → {pos2.value} (contrarian: expect BEARISH)")
    assert pos2 == PositioningRegime.BEARISH, f"Expected BEARISH (contrarian low-PCR), got {pos2}"

    # BEARISH
    pos3 = classifier.classify_positioning(make_signals(pcr=1.50))
    print(f"  PCR=1.50 (extreme fear/high PCR) → {pos3.value} (contrarian: expect BULLISH)")
    assert pos3 == PositioningRegime.BULLISH, f"Expected BULLISH (contrarian high-PCR), got {pos3}"

    # BEARISH (fear skew)
    pos4 = classifier.classify_positioning(make_signals(
        skew_ratio=3.5,
        resistance_strength=1.2,
        support_strength=1.0,
        oi_change_pct=0.02,
        pcr=1.05,
        total_ce_oi=200000,
        total_pe_oi=210000,
        resistance_oi=25000,
        support_oi=22000,
    ))
    print(f"  Skew=3.5 (fear skew), moderate walls → {pos4.value} (expect BEARISH)")
    assert pos4 == PositioningRegime.BEARISH, f"Expected BEARISH, got {pos4}"

    print("  [OK] Positioning regime tests passed")

    # ── Test 4: Confidence score ──────────────────────────────────────────
    print("\n--- Confidence Score Tests ---")

    # HIGH confidence: all signals aligned
    conf1, score1 = classifier.compute_confidence(
        VolatilityRegime.STRONG_SELL_PREMIUM,
        PriceRegime.RANGE,
        PositioningRegime.STRONG_RANGE,
        make_signals(iv_behavior="DECLINING", or_condition="NARROW"),
    )
    print(f"  All aligned → {conf1.value} ({score1:.3f}) (expect HIGH)")
    assert conf1 == ConfidenceLevel.HIGH, f"Expected HIGH, got {conf1}"

    # NONE confidence: ABORT vol
    conf2, score2 = classifier.compute_confidence(
        VolatilityRegime.ABORT,
        PriceRegime.RANGE,
        PositioningRegime.RANGE,
        make_signals(),
    )
    print(f"  ABORT vol → {conf2.value} ({score2:.3f}) (expect NONE)")
    assert conf2 == ConfidenceLevel.NONE, f"Expected NONE, got {conf2}"

    print("  [OK] Confidence score tests passed")

    # ── Test 5: Final regime decision tree ────────────────────────────────
    print("\n--- Final Regime Decision Tree Tests ---")

    # RANGE + STRONG_RANGE → PREMIUM_SELL_RANGE
    final1, notes1, block1 = classifier.classify_final(
            VolatilityRegime.SELL_PREMIUM,
            PriceRegime.RANGE,
            PositioningRegime.STRONG_RANGE,
            ConfidenceLevel.HIGH,
            make_signals(),
            False, "",
            _test_time=dtime(11, 0),
    )
    print(f"  RANGE+STRONG_RANGE → {final1.value} (expect PREMIUM_SELL_RANGE)")
    assert final1 == FinalRegime.PREMIUM_SELL_RANGE, f"Got {final1}"

    # UPTREND + BULLISH → PREMIUM_SELL_BULL
    final2, notes2, block2 = classifier.classify_final(
            VolatilityRegime.SELL_PREMIUM,
            PriceRegime.UPTREND,
            PositioningRegime.BULLISH,
            ConfidenceLevel.HIGH,
            make_signals(adx_15=28.0, spot=24200.0, or_high=24100.0, or_low=24040.0),
            False, "",
            _test_time=dtime(11, 0),
    )
    print(f"  UPTREND+BULLISH → {final2.value} (expect PREMIUM_SELL_BULL)")
    assert final2 == FinalRegime.PREMIUM_SELL_BULL, f"Got {final2}"

    # DOWNTREND → PREMIUM_SELL_BEAR (never sell puts in downtrend)
    final3, notes3, block3 = classifier.classify_final(
            VolatilityRegime.SELL_PREMIUM,
            PriceRegime.DOWNTREND,
            PositioningRegime.BEARISH,
            ConfidenceLevel.HIGH,
            make_signals(adx_15=28.0, spot=23900.0, or_high=24100.0, or_low=24040.0),
            False, "",
            _test_time=dtime(11, 0),
    )
    print(f"  DOWNTREND → {final3.value} (expect PREMIUM_SELL_BEAR)")
    assert final3 == FinalRegime.PREMIUM_SELL_BEAR, f"Got {final3}"

    # ABORT → block_new_entries=True
    final4, notes4, block4 = classifier.classify_final(
            VolatilityRegime.ABORT,
            PriceRegime.RANGE,
            PositioningRegime.RANGE,
            ConfidenceLevel.NONE,
            make_signals(),
            False, "",
            _test_time=dtime(11, 0),
    )
    print(f"  ABORT → {final4.value} block={block4} (expect ABORT, True)")
    assert final4 == FinalRegime.ABORT, f"Got {final4}"
    assert block4 == True, f"Expected block_new_entries=True"

    # NEUTRAL vol → NO_TRADE
    final5, notes5, block5 = classifier.classify_final(
            VolatilityRegime.NEUTRAL,
            PriceRegime.RANGE,
            PositioningRegime.RANGE,
            ConfidenceLevel.MEDIUM,
            make_signals(),
            False, "",
            _test_time=dtime(11, 0),
    )
    print(f"  NEUTRAL vol → {final5.value} (expect NO_TRADE)")
    assert final5 == FinalRegime.NO_TRADE, f"Got {final5}"

    # OBSERVING price → NO_TRADE
    final6, notes6, block6 = classifier.classify_final(
            VolatilityRegime.SELL_PREMIUM,
            PriceRegime.OBSERVING,
            PositioningRegime.RANGE,
            ConfidenceLevel.HIGH,
            make_signals(or_computed=False),
            False, "",
            _test_time=dtime(11, 0),
    )
    print(f"  OBSERVING price → {final6.value} (expect NO_TRADE)")
    assert final6 == FinalRegime.NO_TRADE, f"Got {final6}"

    # Downtrend + BULLISH positioning conflict + LOW confidence → NO_TRADE
    final7, notes7, block7 = classifier.classify_final(
            VolatilityRegime.SELL_PREMIUM,
            PriceRegime.DOWNTREND,
            PositioningRegime.BULLISH,
            ConfidenceLevel.MEDIUM,
            make_signals(adx_15=28.0),
            False, "",
            _test_time=dtime(11, 0),
    )
    print(f"  DOWNTREND+BULLISH conflict+MEDIUM conf → {final7.value} (expect NO_TRADE)")
    assert final7 == FinalRegime.NO_TRADE, f"Got {final7}"

    print("  [OK] Final regime decision tree tests passed")

    # ── Test 6: Size computation ──────────────────────────────────────────
    print("\n--- Size Computation Tests ---")

    # Normal Tuesday 0DTE, HIGH confidence, VIX 11
    size1, raw1, cr1 = classifier.compute_final_size(
        VolatilityRegime.SELL_PREMIUM,
        PriceRegime.RANGE,
        PositioningRegime.STRONG_RANGE,
        ConfidenceLevel.HIGH,
        make_signals(vix=11.5, actual_dte=0, or_condition="NARROW"),
        False, False,
    )
    print(f"  Tuesday 0DTE, HIGH, VIX=11.5, NARROW OR: size={size1:.3f} raw={raw1:.3f}")
    assert size1 > 0, "Size should be positive"
    assert size1 <= 1.0, "Size should be <= 1.0"

    # Event day → size reduced by event_mult
    size2, raw2, cr2 = classifier.compute_final_size(
        VolatilityRegime.SELL_PREMIUM,
        PriceRegime.RANGE,
        PositioningRegime.RANGE,
        ConfidenceLevel.HIGH,
        make_signals(vix=11.5, actual_dte=0),
        True, False,  # event_day=True
    )
    print(f"  Event day: size={size2:.3f} (expect ~{size1*config.event_size_multiplier:.3f})")
    assert size2 < size1, "Event day size should be smaller"

    # MEDIUM confidence → half size
    size3, raw3, cr3 = classifier.compute_final_size(
        VolatilityRegime.SELL_PREMIUM,
        PriceRegime.RANGE,
        PositioningRegime.RANGE,
        ConfidenceLevel.MEDIUM,
        make_signals(vix=11.5, actual_dte=0),
        False, False,
    )
    print(f"  MEDIUM confidence: size={size3:.3f}")
    assert size3 < size1, "MEDIUM confidence should give smaller size"

    # UNCLEAR positioning → 0.5× reduction
    size4, raw4, cr4 = classifier.compute_final_size(
        VolatilityRegime.SELL_PREMIUM,
        PriceRegime.RANGE,
        PositioningRegime.UNCLEAR,
        ConfidenceLevel.HIGH,
        make_signals(vix=11.5, actual_dte=0),
        False, False,
    )
    print(f"  UNCLEAR positioning: size={size4:.3f} conflict_reduction={cr4:.2f}")
    assert cr4 <= 0.50, f"UNCLEAR should have conflict_reduction <= 0.50, got {cr4}"

    # VIX 20 → 0.75× vix_mult
    size5, raw5, cr5 = classifier.compute_final_size(
        VolatilityRegime.SELL_PREMIUM,
        PriceRegime.RANGE,
        PositioningRegime.RANGE,
        ConfidenceLevel.HIGH,
        make_signals(vix=20.0, actual_dte=0),
        False, False,
    )
    print(f"  VIX=20 (NORMAL): size={size5:.3f}")
    assert size5 < size1, "Higher VIX should give smaller size"

    print("  [OK] Size computation tests passed")

    # ── Test 7: Full RegimeEngine integration ─────────────────────────────
    print_section("RegimeEngine Integration Test")
    engine = RegimeEngine(config, db, market_engine, logger)
    engine.run_calibration(force=True)

    # Test with mock signals (no live API needed)
    mock_signals = make_signals()
    snapshot = engine.calculate_regime(mock_signals)

    print_kv_table({
        "Vol Regime":        snapshot.vol_regime,
        "Price Regime":      snapshot.price_regime,
        "Positioning":       snapshot.positioning_regime,
        "Confidence":        f"{snapshot.confidence_level} ({snapshot.confidence_score:.3f})",
        "Final Regime":      snapshot.final_regime,
        "Size Multiplier":   snapshot.size_multiplier,
        "Raw Size":          snapshot.raw_size_multiplier,
        "Block New Entries": snapshot.block_new_entries,
        "Borderline Sell":   snapshot.borderline_sell,
        "Calibrated":        snapshot.is_calibrated,
        "Cal Tier":          snapshot.calibration_tier,
        "Notes":             snapshot.final_regime_notes,
    }, title="Mock Signals Regime Output")

    # Test persistence filter
    snap1 = engine.calculate_regime(mock_signals)
    confirmed1 = engine._apply_persistence_filter(snap1)
    print(f"\n  Persistence filter test:")
    print(f"  First regime: {snap1.final_regime} → confirmed: {confirmed1.final_regime}")

    # Test process_signals
    result_signals = dict(mock_signals)
    confirmed_snap = engine.process_signals(result_signals)
    print(f"  process_signals: final_regime={result_signals.get('final_regime')}")
    assert "final_regime" in result_signals, "process_signals should update signals dict"
    assert "size_multiplier" in result_signals, "process_signals should set size_multiplier"

    print("  [OK] RegimeEngine integration test passed")

    # ── Test 8: Regime bridge functions ───────────────────────────────────
    print_section("Regime Bridge Tests")

    # is_regime_tradeable
    tradeable_signals = {
        "final_regime":    FinalRegime.PREMIUM_SELL_RANGE.value,
        "confidence_level": ConfidenceLevel.HIGH.value,
        "size_multiplier": 0.60,
        "block_new_entries": False,
    }
    assert is_regime_tradeable(tradeable_signals), "Should be tradeable"

    no_trade_signals = {
        "final_regime":    FinalRegime.NO_TRADE.value,
        "confidence_level": ConfidenceLevel.NONE.value,
        "size_multiplier": 0.0,
        "block_new_entries": False,
    }
    assert not is_regime_tradeable(no_trade_signals), "Should not be tradeable"

    abort_signals = {
        "final_regime":    FinalRegime.ABORT.value,
        "confidence_level": ConfidenceLevel.NONE.value,
        "size_multiplier": 0.0,
        "block_new_entries": True,
    }
    assert not is_regime_tradeable(abort_signals), "ABORT should not be tradeable"

    # get_strategy_from_regime
    assert get_strategy_from_regime({"final_regime": "PREMIUM_SELL_BULL"}) == "BULL_PUT_SPREAD"
    assert get_strategy_from_regime({"final_regime": "PREMIUM_SELL_BEAR"}) == "BEAR_CALL_SPREAD"
    assert get_strategy_from_regime({"final_regime": "NO_TRADE"}) == "NO_TRADE"
    assert get_strategy_from_regime({"final_regime": "PREMIUM_SELL_RANGE"}) == "NO_TRADE"  # strategy_engine decides

    print("  [OK] Regime bridge tests passed")

    # ── Live API test ─────────────────────────────────────────────────────
    if config.upstox_access_token and client.validate_token():
        print_section("LIVE API TEST")
        signals = market_engine.run_cycle()
        live_snap = engine.process_signals(signals)
        print_kv_table({
            "Vol Regime":      live_snap.vol_regime,
            "Price Regime":    live_snap.price_regime,
            "Positioning":     live_snap.positioning_regime,
            "Confidence":      f"{live_snap.confidence_level} ({live_snap.confidence_score:.3f})",
            "Final Regime":    live_snap.final_regime,
            "Size":            live_snap.size_multiplier,
            "VRP Smoothed":    live_snap.vrp_smoothed,
            "IV Behavior":     live_snap.iv_behavior,
            "Day Move Used":   f"{live_snap.day_move_used_pct:.1f}%",
            "Notes":           live_snap.final_regime_notes,
        }, title="Live Regime Output")
    else:
        print_section("LIVE API TEST: SKIPPED (no token)")
        print("  Set UPSTOX_ACCESS_TOKEN in env.txt to test live regime classification.")

    db.close()
    print_section("REGIME ENGINE SELF-TEST COMPLETE", char="#")
    print(f"  All unit tests passed")
    print(f"  Database: {db.db_path}")
    print()


if __name__ == "__main__":
    _self_test()
