from __future__ import annotations

import json
import logging
import time as time_module
from datetime import datetime, date, timedelta, time
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from enum import Enum

import pandas as pd
import numpy as np

from nifty_algo_core import (
    Config, Database, RateLimiter, UpstoxClient,
    ExpiryCalendar, now_ist, today_ist,
    load_config, setup_logging,
    get_nse_holidays, get_high_impact_events,
    INSTRUMENT_KEY_NIFTY_SPOT, INSTRUMENT_KEY_INDIA_VIX,
)
from market_data_engine import MarketDataEngine, TechnicalEngine


class VolatilityRegime(str, Enum):
    STRONG_SELL_PREMIUM = "STRONG_SELL_PREMIUM"
    SELL_PREMIUM        = "SELL_PREMIUM"
    NEUTRAL             = "NEUTRAL"
    BUY_OPTIONS         = "BUY_OPTIONS"
    HIGH_VOL_CAUTION    = "HIGH_VOL_CAUTION"
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
    BUY_STRADDLE         = "BUY_STRADDLE"
    BUY_DIRECTIONAL_BULL = "BUY_DIRECTIONAL_BULL"
    BUY_DIRECTIONAL_BEAR = "BUY_DIRECTIONAL_BEAR"
    EXPIRY_MAX_PAIN      = "EXPIRY_MAX_PAIN"
    NO_TRADE             = "NO_TRADE"
    EMERGENCY_EXIT       = "EMERGENCY_EXIT"


class ConfidenceLevel(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"
    NONE   = "NONE"


@dataclass
class RegimeSnapshot:
    timestamp:           datetime
    day_type:            str
    dte:                 int
    event_day:           bool
    event_name:          str
    defined_risk_only:   bool
    volatility_regime:   str
    price_regime:        str
    price_regime_15:     str
    price_regime_60:     str
    mtf_aligned:         bool
    positioning_regime:  str
    final_regime:        str
    confidence:          str
    size_multiplier:     float
    raw_size_multiplier: float
    vix_level:           float
    vix_roc:             float
    ivr:                 float
    iv_hv_ratio:         float
    straddle_ratio:      float
    adx_15:              float
    adx_60:              float
    ema_structure:       str
    oi_wall_strength:    float
    oi_change_pct:       float
    skew:                float
    max_pain_distance:   float
    pcr:                 float
    notes:               str
    is_calibrated:       bool
    calibration_tier:    int


@dataclass
class CalibrationState:
    last_calibrated:          Optional[datetime]
    vix_p25:                  float
    vix_p50:                  float
    vix_p75:                  float
    vix_p90:                  float
    vix_roc_emergency:        float
    ivr_sell_threshold:       float
    ivr_buy_threshold:        float
    iv_hv_sell_threshold:     float
    straddle_ratio_sell:      float
    oi_wall_strong_threshold: float
    tuesday_avg_range:        float
    monday_avg_range:         float
    thursday_avg_range:       float
    friday_avg_range:         float
    wednesday_avg_range:      float
    n_tuesday_expiries:       int
    n_trading_days:           int
    skew_bearish_threshold:   float
    skew_bullish_threshold:   float
    oi_buildup_threshold:     float
    oi_unwind_threshold:      float
    oi_wall_strong_cal:       float
    oi_wall_moderate_cal:     float
    calibration_tier:         int
    is_calibrated:            bool
    vrp_sell_threshold:       float
    vrp_fair_threshold:       float
    day_size_monday:          float
    day_size_tuesday:         float
    day_size_wednesday:       float
    day_size_thursday:        float
    day_size_friday:          float


class AutoCalibrator:

    NIFTY_VIX_LOW_DEFAULT = 12.5
    NIFTY_VIX_NORMAL_DEFAULT = 16.0
    NIFTY_VIX_HIGH_DEFAULT = 22.0
    NIFTY_VIX_EXTREME_DEFAULT = 28.0
    NIFTY_SKEW_BEAR_DEFAULT = 3.0
    NIFTY_SKEW_BULL_DEFAULT = -1.0
    NIFTY_OI_BUILD_DEFAULT = 0.08
    NIFTY_OI_UNWIND_DEFAULT = -0.08
    NIFTY_OI_WALL_STRONG_DEFAULT = 2.8
    NIFTY_OI_WALL_MOD_DEFAULT = 1.7
    NIFTY_STRADDLE_RATIO_SELL_DEFAULT = 1.18
    NIFTY_VRP_SELL_DEFAULT = 3.0
    NIFTY_VRP_FAIR_DEFAULT = 1.5

    def __init__(self, db, config, logger):
        self.db = db
        self.config = config
        self.logger = logger

    def run(self):
        self.logger.info("AutoCalibrator: starting self-calibration from all available historical data")
        result = {}
        try:
            result.update(self._calibrate_vix_thresholds())
        except Exception as e:
            self.logger.debug(f"AutoCalibrator VIX calibration error: {e}")
        try:
            result.update(self._calibrate_vrp_thresholds())
        except Exception as e:
            self.logger.debug(f"AutoCalibrator VRP calibration error: {e}")
        try:
            result.update(self._calibrate_skew_thresholds())
        except Exception as e:
            self.logger.debug(f"AutoCalibrator skew calibration error: {e}")
        try:
            result.update(self._calibrate_oi_thresholds())
        except Exception as e:
            self.logger.debug(f"AutoCalibrator OI calibration error: {e}")
        try:
            result.update(self._calibrate_day_size_multipliers())
        except Exception as e:
            self.logger.debug(f"AutoCalibrator day size calibration error: {e}")
        try:
            result.update(self._calibrate_straddle_ratio())
        except Exception as e:
            self.logger.debug(f"AutoCalibrator straddle ratio calibration error: {e}")
        self.logger.info("AutoCalibrator: completed. Keys calibrated: %s" % list(result.keys()))
        for _h in self.logger.handlers:
            if hasattr(_h, 'stream') and hasattr(_h.stream, 'flush'):
                _h.stream.flush()
        return result

    def _calibrate_vix_thresholds(self):
        vix_df = self.db.get_vix_history(days=730)
        if vix_df.empty or len(vix_df) < 20:
            self.logger.info("AutoCalibrator VIX: insufficient data, using NIFTY 2026 defaults")
            return {
                "vix_p25": getattr(self.config, "vix_low", self.NIFTY_VIX_LOW_DEFAULT),
                "vix_p50": getattr(self.config, "vix_normal", self.NIFTY_VIX_NORMAL_DEFAULT),
                "vix_p75": getattr(self.config, "vix_high", self.NIFTY_VIX_HIGH_DEFAULT),
                "vix_p90": getattr(self.config, "vix_extreme_high", self.NIFTY_VIX_EXTREME_DEFAULT),
            }
        v = vix_df["vix_value"].dropna().values
        p25 = float(np.percentile(v, 25))
        p50 = float(np.percentile(v, 50))
        p75 = float(np.percentile(v, 75))
        p90 = float(np.percentile(v, 90))
        p25 = max(p25, 11.0)
        p50 = max(p50, 14.0)
        p75 = max(p75, 18.0)
        p90 = max(p90, 24.0)
        self.logger.info(f"AutoCalibrator VIX: p25={p25:.1f} p50={p50:.1f} p75={p75:.1f} p90={p90:.1f} (n={len(v)})")
        return {"vix_p25": p25, "vix_p50": p50, "vix_p75": p75, "vix_p90": p90}

    def _calibrate_vrp_thresholds(self):
        try:
            rows = self.db.query(
                "SELECT vrp, volatility_condition FROM market_snapshots "
                "WHERE vrp IS NOT NULL AND vrp != 0 ORDER BY timestamp"
            )
            if len(rows) < 30:
                return {"vrp_sell_threshold": self.NIFTY_VRP_SELL_DEFAULT, "vrp_fair_threshold": self.NIFTY_VRP_FAIR_DEFAULT}
            vrps = [r["vrp"] for r in rows if r["vrp"] is not None]
            positive_vrps = [v for v in vrps if v > 0]
            if len(positive_vrps) < 10:
                return {"vrp_sell_threshold": self.NIFTY_VRP_SELL_DEFAULT, "vrp_fair_threshold": self.NIFTY_VRP_FAIR_DEFAULT}
            sell_thresh = float(np.percentile(positive_vrps, 40))
            fair_thresh = float(np.percentile(positive_vrps, 20))
            sell_thresh = max(sell_thresh, 2.0)
            fair_thresh = max(fair_thresh, 1.0)
            self.logger.info(f"AutoCalibrator VRP: sell={sell_thresh:.2f} fair={fair_thresh:.2f} (n={len(positive_vrps)})")
            return {"vrp_sell_threshold": sell_thresh, "vrp_fair_threshold": fair_thresh}
        except Exception:
            return {"vrp_sell_threshold": self.NIFTY_VRP_SELL_DEFAULT, "vrp_fair_threshold": self.NIFTY_VRP_FAIR_DEFAULT}

    def _calibrate_skew_thresholds(self):
        try:
            snap_df = self.db.get_market_snapshots(days=730)
            if snap_df.empty or "skew" not in snap_df.columns or len(snap_df) < 50:
                return {
                    "skew_bearish_threshold": self.NIFTY_SKEW_BEAR_DEFAULT,
                    "skew_bullish_threshold": self.NIFTY_SKEW_BULL_DEFAULT,
                }
            skew_vals = snap_df["skew"].dropna()
            skew_vals = skew_vals[(skew_vals > -1.5) & (skew_vals < 8.0)]
            if len(skew_vals) < 30:
                return {
                    "skew_bearish_threshold": self.NIFTY_SKEW_BEAR_DEFAULT,
                    "skew_bullish_threshold": self.NIFTY_SKEW_BULL_DEFAULT,
                }
            bear = float(np.percentile(skew_vals, 75))
            bull = float(np.percentile(skew_vals, 25))
            bear = max(bear, 2.0)
            bull = min(bull, 0.5)
            self.logger.info(f"AutoCalibrator Skew: bearish={bear:.2f} bullish={bull:.2f} (n={len(skew_vals)})")
            return {"skew_bearish_threshold": bear, "skew_bullish_threshold": bull}
        except Exception:
            return {
                "skew_bearish_threshold": self.NIFTY_SKEW_BEAR_DEFAULT,
                "skew_bullish_threshold": self.NIFTY_SKEW_BULL_DEFAULT,
            }

    def _calibrate_oi_thresholds(self):
        try:
            snap_df = self.db.get_market_snapshots(days=730)
            if snap_df.empty or "oi_change_pct" not in snap_df.columns or len(snap_df) < 50:
                return {
                    "oi_buildup_threshold": self.NIFTY_OI_BUILD_DEFAULT,
                    "oi_unwind_threshold": self.NIFTY_OI_UNWIND_DEFAULT,
                    "oi_wall_strong_cal": self.NIFTY_OI_WALL_STRONG_DEFAULT,
                    "oi_wall_moderate_cal": self.NIFTY_OI_WALL_MOD_DEFAULT,
                }
            oi_chg = snap_df["oi_change_pct"].dropna().values
            pos_chg = oi_chg[oi_chg > 0]
            neg_chg = oi_chg[oi_chg < 0]
            build = float(np.percentile(pos_chg, 60)) if len(pos_chg) >= 20 else self.NIFTY_OI_BUILD_DEFAULT
            unwind = float(np.percentile(neg_chg, 40)) if len(neg_chg) >= 20 else self.NIFTY_OI_UNWIND_DEFAULT
            build = max(build, 0.04)
            unwind = min(unwind, -0.04)
            self.logger.info(f"AutoCalibrator OI: build={build:.4f} unwind={unwind:.4f}")
            return {
                "oi_buildup_threshold": build,
                "oi_unwind_threshold": unwind,
                "oi_wall_strong_cal": self.NIFTY_OI_WALL_STRONG_DEFAULT,
                "oi_wall_moderate_cal": self.NIFTY_OI_WALL_MOD_DEFAULT,
            }
        except Exception:
            return {
                "oi_buildup_threshold": self.NIFTY_OI_BUILD_DEFAULT,
                "oi_unwind_threshold": self.NIFTY_OI_UNWIND_DEFAULT,
                "oi_wall_strong_cal": self.NIFTY_OI_WALL_STRONG_DEFAULT,
                "oi_wall_moderate_cal": self.NIFTY_OI_WALL_MOD_DEFAULT,
            }

    def _calibrate_day_size_multipliers(self):
        try:
            df = self.db.get_daily_summary(days=730)
            if df.empty or "net_pnl_rupees" not in df.columns or len(df) < 20:
                return {}
            result = {}
            day_map = {1: "monday", 2: "tuesday", 3: "wednesday", 4: "thursday", 5: "friday"}
            base_sizes = {1: 0.75, 2: 0.60, 3: 0.75, 4: 0.75, 5: 0.50}
            for wd, name in day_map.items():
                sub = df[df["weekday"] == wd]
                if len(sub) < 5:
                    result[f"day_size_{name}"] = base_sizes[wd]
                    continue
                wins = (sub["net_pnl_rupees"] > 0).sum()
                total = len(sub)
                win_rate = wins / total
                avg_pnl = sub["net_pnl_rupees"].mean()
                if win_rate >= 0.60 and avg_pnl > 0:
                    size = min(base_sizes[wd] * 1.10, 1.00)
                elif win_rate < 0.40 or avg_pnl < 0:
                    size = max(base_sizes[wd] * 0.80, 0.25)
                else:
                    size = base_sizes[wd]
                result[f"day_size_{name}"] = round(size, 2)
                self.logger.info(f"AutoCalibrator DaySize: {name} win_rate={win_rate:.1%} size={size:.2f} (n={total})")
            return result
        except Exception:
            return {}

    def _calibrate_straddle_ratio(self):
        try:
            df = self.db.get_daily_summary(days=730)
            if df.empty or "straddle_ratio" not in df.columns:
                return {"straddle_ratio_sell": self.NIFTY_STRADDLE_RATIO_SELL_DEFAULT}
            ratios = df["straddle_ratio"].dropna()
            ratios = ratios[(ratios > 0.5) & (ratios < 3.0)]
            if len(ratios) < 10:
                return {"straddle_ratio_sell": self.NIFTY_STRADDLE_RATIO_SELL_DEFAULT}
            sr_sell = float(np.percentile(ratios, 65))
            sr_sell = max(sr_sell, 1.05)
            self.logger.info(f"AutoCalibrator StraddleRatio: sell={sr_sell:.3f} (n={len(ratios)})")
            return {"straddle_ratio_sell": sr_sell}
        except Exception:
            return {"straddle_ratio_sell": self.NIFTY_STRADDLE_RATIO_SELL_DEFAULT}


class CalibrationEngine:

    def __init__(self, db: Database, config: Config, logger):
        self.db = db
        self.config = config
        self.logger = logger
        self._state: Optional[CalibrationState] = None
        self._load()

    def _load(self) -> None:
        d = self.db.get_latest_calibration()
        if d:
            self._state = CalibrationState(
                last_calibrated=datetime.fromisoformat(d["calibrated_at"]),
                vix_p25=d["vix_p25"], vix_p50=d["vix_p50"],
                vix_p75=d["vix_p75"], vix_p90=d["vix_p90"],
                vix_roc_emergency=d["vix_roc_emergency"],
                ivr_sell_threshold=d["ivr_sell_threshold"],
                ivr_buy_threshold=d["ivr_buy_threshold"],
                iv_hv_sell_threshold=d["iv_hv_sell_threshold"],
                straddle_ratio_sell=d["straddle_ratio_sell"],
                oi_wall_strong_threshold=d["oi_wall_strong"],
                tuesday_avg_range=d["tuesday_avg_range"],
                monday_avg_range=d["monday_avg_range"],
                thursday_avg_range=d["thursday_avg_range"],
                friday_avg_range=d["friday_avg_range"],
                wednesday_avg_range=d["wednesday_avg_range"],
                n_tuesday_expiries=d["n_tuesday_expiries"],
                n_trading_days=d["n_trading_days"],
                skew_bearish_threshold=d.get("skew_bearish_threshold", self.config.skew_bearish_threshold),
                skew_bullish_threshold=d.get("skew_bullish_threshold", self.config.skew_bullish_threshold),
                oi_buildup_threshold=d.get("oi_buildup_threshold", self.config.oi_buildup_threshold),
                oi_unwind_threshold=d.get("oi_unwind_threshold", self.config.oi_unwind_threshold),
                oi_wall_strong_cal=d.get("oi_wall_strong_cal", self.config.oi_wall_strong),
                oi_wall_moderate_cal=d.get("oi_wall_moderate_cal", self.config.oi_wall_moderate),
                calibration_tier=d.get("calibration_tier", 0),
                is_calibrated=bool(d["is_valid"]),
                vrp_sell_threshold=float(d.get("vrp_sell_threshold") or 3.0),
                vrp_fair_threshold=float(d.get("vrp_fair_threshold") or 1.5),
                day_size_monday=float(d.get("day_size_monday") or 0.75),
                day_size_tuesday=float(d.get("day_size_tuesday") or 0.60),
                day_size_wednesday=float(d.get("day_size_wednesday") or 0.75),
                day_size_thursday=float(d.get("day_size_thursday") or 0.75),
                day_size_friday=float(d.get("day_size_friday") or 0.50),
            )
            self.logger.info(
                f"Calibration loaded: {d['n_trading_days']} days, "
                f"tier={d.get('calibration_tier', 0)}, valid={bool(d['is_valid'])}"
            )
        else:
            self.logger.info("No prior calibration. Using Config estimates.")

    @property
    def state(self) -> Optional[CalibrationState]:
        return self._state

    def run(self) -> Optional[CalibrationState]:
        self.logger.info("Calibration run starting...")
        n_exp  = self.db.count_tuesday_expiries()
        n_days = self.db.count_trading_days()
        self.logger.info(f"  Data: {n_exp} Tuesday expiries, {n_days} trading days")

        vix_df = self.db.get_vix_history(days=365)
        if len(vix_df) >= 50:
            v   = vix_df["vix_value"].dropna().values
            p25 = float(np.percentile(v, 25))
            p50 = float(np.percentile(v, 50))
            p75 = float(np.percentile(v, 75))
            p90 = float(np.percentile(v, 90))
            self.logger.info(
                f"  VIX: p25={p25:.1f} p50={p50:.1f} p75={p75:.1f} p90={p90:.1f}"
            )
        else:
            self.logger.warning(f"  VIX rows={len(vix_df)} < 50. Using estimates.")
            p25 = getattr(self.config, "vix_low", 13.0)
            p50 = getattr(self.config, "vix_normal", 16.0)
            p75 = getattr(self.config, "vix_high", 22.0)
            p90 = getattr(self.config, "vix_extreme_high", 28.0)

        vix_roc_emg = self.config.vix_roc_emergency_pct
        if len(vix_df) >= 200:
            vals = vix_df.sort_values(["date", "time"])["vix_value"].values
            rocs = []
            w = 6
            for i in range(w, len(vals)):
                if vals[i - w] > 0:
                    rocs.append((vals[i] - vals[i - w]) / vals[i - w] * 100)
            if rocs:
                vix_roc_emg = float(np.percentile(rocs, 95))
                self.logger.info(f"  VIX ROC emergency (p95): {vix_roc_emg:.2f}%")

        daily = self.db.get_daily_summary(days=365)
        ranges = {wd: 150.0 for wd in range(5)}
        if not daily.empty and "day_range_points" in daily.columns:
            for wd in range(5):
                sub = daily[daily["weekday"] == wd]["day_range_points"].dropna()
                if len(sub) >= 3:
                    ranges[wd] = float(sub.mean())
                    self.logger.info(
                        f"  Day {wd} avg range: {ranges[wd]:.1f} pts (n={len(sub)})"
                    )

        sr_sell = self.config.straddle_ratio_sell
        if not daily.empty and "straddle_ratio" in daily.columns:
            ratios = daily["straddle_ratio"].dropna()
            if len(ratios) >= 10:
                sr_sell = float(np.percentile(ratios, 65))
                self.logger.info(f"  Straddle ratio sell (p65): {sr_sell:.3f}")

        skew_bear = self.config.skew_bearish_threshold
        skew_bull = self.config.skew_bullish_threshold
        oi_build  = self.config.oi_buildup_threshold
        oi_unwind = self.config.oi_unwind_threshold
        oi_strong = self.config.oi_wall_strong
        oi_mod    = self.config.oi_wall_moderate

        try:
            snap_df = pd.read_sql_query(
                "SELECT skew, oi_change_pct, resistance_oi, support_oi, "
                "total_ce_oi, total_pe_oi FROM market_snapshots "
                "WHERE skew != 0 AND date >= ? ORDER BY timestamp",
                self.db.get_connection(),
                params=((date.today() - timedelta(days=365)).isoformat(),),
            )
            if len(snap_df) >= 100:
                skew_vals = snap_df["skew"].dropna().values
                skew_bear = float(np.percentile(skew_vals, 75))
                skew_bull = float(np.percentile(skew_vals, 25))
                self.logger.info(
                    f"  Skew: bearish p75={skew_bear:.2f} bullish p25={skew_bull:.2f}"
                )
                oi_chg = snap_df["oi_change_pct"].dropna().values
                pos_chg = oi_chg[oi_chg > 0]
                neg_chg = oi_chg[oi_chg < 0]
                if len(pos_chg) >= 20:
                    oi_build = float(np.percentile(pos_chg, 60))
                    self.logger.info(f"  OI buildup (p60 pos): {oi_build:.4f}")
                if len(neg_chg) >= 20:
                    oi_unwind = float(np.percentile(neg_chg, 40))
                    self.logger.info(f"  OI unwind (p40 neg): {oi_unwind:.4f}")
                if snap_df["total_ce_oi"].sum() > 0:
                    _sh = max(71 // 2, 1)
                    snap_df["r_str"] = snap_df.apply(
                        lambda r: r["resistance_oi"] / max(r["total_ce_oi"] / _sh, 1)
                        if r["total_ce_oi"] > 0 else 0.0, axis=1
                    )
                    r_vals = snap_df["r_str"].dropna().values
                    r_vals = r_vals[r_vals > 0]
                    if len(r_vals) >= 50:
                        oi_strong = float(np.percentile(r_vals, 80))
                        oi_mod    = float(np.percentile(r_vals, 55))
                        self.logger.info(
                            f"  OI wall: strong p80={oi_strong:.2f} mod p55={oi_mod:.2f}"
                        )
        except Exception as e:
            self.logger.debug(f"  Extended calibration skipped: {e}")

        tier1 = len(vix_df) >= 50 and n_days >= 5
        tier2 = n_days >= self.config.min_trading_days_for_calibration and len(vix_df) >= 50
        tier3 = n_days >= 60 and len(vix_df) >= 100
        cal_tier = 3 if tier3 else (2 if tier2 else (1 if tier1 else 0))
        valid = tier2

        if not valid:
            self.logger.warning(
                f"  Calibration tier={cal_tier}. "
                f"Need {self.config.min_trading_days_for_calibration} days "
                f"(have {n_days}) and 50 VIX rows (have {len(vix_df)})."
            )
        else:
            self.logger.info(f"  Calibration tier={cal_tier} VALID.")

        cal = CalibrationState(
            last_calibrated=datetime.now(),
            vix_p25=p25, vix_p50=p50, vix_p75=p75, vix_p90=p90,
            vix_roc_emergency=vix_roc_emg,
            ivr_sell_threshold=self.config.ivr_sell,
            ivr_buy_threshold=self.config.ivr_buy,
            iv_hv_sell_threshold=self.config.iv_hv_sell,
            straddle_ratio_sell=sr_sell,
            oi_wall_strong_threshold=oi_strong,
            tuesday_avg_range=ranges[1], monday_avg_range=ranges[0],
            thursday_avg_range=ranges[3], friday_avg_range=ranges[4],
            wednesday_avg_range=ranges[2],
            n_tuesday_expiries=n_exp, n_trading_days=n_days,
            skew_bearish_threshold=skew_bear,
            skew_bullish_threshold=skew_bull,
            oi_buildup_threshold=oi_build,
            oi_unwind_threshold=oi_unwind,
            oi_wall_strong_cal=oi_strong,
            oi_wall_moderate_cal=oi_mod,
            calibration_tier=cal_tier,
            is_calibrated=valid,
            vrp_sell_threshold=3.0,
            vrp_fair_threshold=1.5,
            day_size_monday=0.75,
            day_size_tuesday=0.60,
            day_size_wednesday=0.75,
            day_size_thursday=0.75,
            day_size_friday=0.50,
        )

        try:
            self.db.insert("calibration_state", {
                "calibrated_at": datetime.now().isoformat(),
                "n_tuesday_expiries": n_exp, "n_trading_days": n_days,
                "vix_p25": p25, "vix_p50": p50, "vix_p75": p75, "vix_p90": p90,
                "vix_roc_emergency": vix_roc_emg,
                "ivr_sell_threshold": self.config.ivr_sell,
                "ivr_buy_threshold": self.config.ivr_buy,
                "iv_hv_sell_threshold": self.config.iv_hv_sell,
                "straddle_ratio_sell": sr_sell,
                "oi_wall_strong": oi_strong,
                "tuesday_avg_range": ranges[1], "monday_avg_range": ranges[0],
                "thursday_avg_range": ranges[3], "friday_avg_range": ranges[4],
                "wednesday_avg_range": ranges[2],
                "skew_bearish_threshold": skew_bear,
                "skew_bullish_threshold": skew_bull,
                "oi_buildup_threshold": oi_build,
                "oi_unwind_threshold": oi_unwind,
                "oi_wall_strong_cal": oi_strong,
                "oi_wall_moderate_cal": oi_mod,
                "calibration_tier": cal_tier,
                "is_valid": int(valid),
                "notes": f"tier={cal_tier} days={n_days}",
                "vrp_sell_threshold": 3.0,
                "vrp_fair_threshold": 1.5,
                "day_size_monday": 0.75,
                "day_size_tuesday": 0.60,
                "day_size_wednesday": 0.75,
                "day_size_thursday": 0.75,
                "day_size_friday": 0.50,
            })
        except Exception as e:
            self.logger.warning(f"Could not save calibration: {e}")

        auto_cal = AutoCalibrator(self.db, self.config, self.logger)
        auto_results = auto_cal.run()
        if auto_results.get("vix_p25") and not valid:
            cal.vix_p25 = auto_results.get("vix_p25", cal.vix_p25)
            cal.vix_p50 = auto_results.get("vix_p50", cal.vix_p50)
            cal.vix_p75 = auto_results.get("vix_p75", cal.vix_p75)
            cal.vix_p90 = auto_results.get("vix_p90", cal.vix_p90)
        if auto_results.get("skew_bearish_threshold"):
            cal.skew_bearish_threshold = auto_results["skew_bearish_threshold"]
        if auto_results.get("skew_bullish_threshold"):
            cal.skew_bullish_threshold = auto_results["skew_bullish_threshold"]
        if auto_results.get("oi_buildup_threshold"):
            cal.oi_buildup_threshold = auto_results["oi_buildup_threshold"]
        if auto_results.get("oi_unwind_threshold"):
            cal.oi_unwind_threshold = auto_results["oi_unwind_threshold"]
        if auto_results.get("straddle_ratio_sell"):
            cal.straddle_ratio_sell = auto_results["straddle_ratio_sell"]
        if auto_results.get("vrp_sell_threshold"):
            cal.vrp_sell_threshold = auto_results["vrp_sell_threshold"]
        if auto_results.get("vrp_fair_threshold"):
            cal.vrp_fair_threshold = auto_results["vrp_fair_threshold"]
        if auto_results.get("day_size_monday"):
            cal.day_size_monday = auto_results["day_size_monday"]
        if auto_results.get("day_size_tuesday"):
            cal.day_size_tuesday = auto_results["day_size_tuesday"]
        if auto_results.get("day_size_wednesday"):
            cal.day_size_wednesday = auto_results["day_size_wednesday"]
        if auto_results.get("day_size_thursday"):
            cal.day_size_thursday = auto_results["day_size_thursday"]
        if auto_results.get("day_size_friday"):
            cal.day_size_friday = auto_results["day_size_friday"]
        self._state = cal
        self.logger.info(f"Calibration complete. Valid={valid} Tier={cal_tier}.")
        return cal


class RegimeClassifier:

    def __init__(
        self, db: Database, config: Config,
        cal: Optional[CalibrationState] = None,
        engine_ref=None, logger=None
    ):
        self.db = db
        self.config = config
        self.cal = cal
        self._engine_ref = engine_ref
        self.logger = logger or logging.getLogger("RegimeEngine")

    def _t(self, cal_attr: str, cfg_attr: str) -> float:
        if self.cal and self.cal.is_calibrated:
            v = getattr(self.cal, cal_attr, None)
            if v is not None:
                return float(v)
        return float(getattr(self.config, cfg_attr, 0.0))

    def classify_volatility(self, signals: dict) -> Tuple[VolatilityRegime, dict]:
        vix     = signals.get("vix") or 15.0
        vix_roc = self._calculate_vix_roc()
        cur_iv  = signals.get("atm_iv") or (vix / 100.0)
        if cur_iv < 1.0:
            cur_iv_pct = cur_iv * 100.0
        else:
            cur_iv_pct = cur_iv
        cur_iv_decimal = cur_iv_pct / 100.0

        ivr     = self._calculate_ivr(cur_iv_pct)
        iv_hv   = self._calculate_iv_hv_ratio(cur_iv_pct)
        s_ratio = self._calculate_straddle_ratio(
            signals.get("atm_straddle_price", 0),
            signals.get("timestamp", datetime.now()).weekday()
            if hasattr(signals.get("timestamp", None), "weekday") else 0
        )

        details = {
            "vix": vix, "vix_roc": vix_roc, "ivr": ivr,
            "iv_hv": iv_hv, "straddle_ratio": s_ratio,
        }

        emg = self._t("vix_roc_emergency", "vix_roc_emergency_pct")
        if vix_roc >= emg:
            details["trigger"] = "VIX_SPIKE"
            return VolatilityRegime.ABORT, details

        vix_ceil = self._t("vix_p90", "vix_extreme_high")
        if vix >= vix_ceil:
            details["trigger"] = "VIX_EXTREME"
            return VolatilityRegime.ABORT, details

        vix_fail = getattr(self._engine_ref, "_vix_fail_count", 0)
        if vix_fail >= self.config.vix_fail_limit:
            details["trigger"] = "VIX_DATA_UNAVAILABLE"
            return VolatilityRegime.ABORT, details

        scores = []
        vix_lo = self._t("vix_p25", "vix_low")
        vix_md = self._t("vix_p50", "vix_normal")
        vix_hi = self._t("vix_p75", "vix_high")
        scores.append(-1 if vix < vix_lo else -0.5 if vix < vix_md else 0 if vix < vix_hi else 1)
        scores.append(-1 if vix_roc < -2 else -0.5 if vix_roc < 0 else 0 if vix_roc < emg * 0.6 else 1)

        ivr_s = self._t("ivr_sell_threshold", "ivr_sell")
        ivr_b = self._t("ivr_buy_threshold",  "ivr_buy")
        scores.append(-1 if ivr > ivr_s else -0.5 if ivr > self.config.ivr_neutral_low else 0 if ivr > ivr_b else 1)

        iv_hv_s = self._t("iv_hv_sell_threshold", "iv_hv_sell")
        scores.append(-1 if iv_hv > iv_hv_s else -0.5 if iv_hv > self.config.iv_hv_neutral else 0 if iv_hv > self.config.iv_hv_buy else 1)

        sr_s = self._t("straddle_ratio_sell", "straddle_ratio_sell")
        scores.append(-1 if s_ratio > sr_s else -0.5 if s_ratio > self.config.straddle_ratio_neutral_h else 0 if s_ratio > self.config.straddle_ratio_neutral_l else 1)

        avg = float(np.mean(scores))
        details["avg_score"] = avg

        regime = (
            VolatilityRegime.STRONG_SELL_PREMIUM if avg <= -0.75 else
            VolatilityRegime.SELL_PREMIUM        if avg <= -0.25 else
            VolatilityRegime.NEUTRAL             if avg <=  0.25 else
            VolatilityRegime.BUY_OPTIONS         if avg <=  0.75 else
            VolatilityRegime.HIGH_VOL_CAUTION
        )
        return regime, details

    def _calculate_vix_roc(self) -> float:
        try:
            cutoff = (datetime.now() - timedelta(minutes=self.config.vix_roc_lookback_min)).isoformat()
            rows = self.db.query(
                "SELECT vix_value FROM vix_history WHERE timestamp >= ? ORDER BY timestamp",
                (cutoff,),
            )
            vals = [r["vix_value"] for r in rows]
            if len(vals) < 2 or vals[0] <= 0:
                return 0.0
            return (vals[-1] - vals[0]) / vals[0] * 100.0
        except Exception:
            return 0.0

    def _calculate_ivr(self, current_iv_pct: float) -> float:
        try:
            df = self.db.get_vix_history(days=365)
            if df.empty or len(df) < 20:
                return 50.0
            vals = df["vix_value"].dropna().values
            lo = np.percentile(vals, 5)
            hi = np.percentile(vals, 95)
            if hi <= lo:
                return 50.0
            return float(np.clip((current_iv_pct - lo) / (hi - lo) * 100.0, 0, 100))
        except Exception:
            return 50.0

    def _calculate_iv_hv_ratio(self, current_iv_pct: float) -> float:
        try:
            df = self.db.get_spot_history(days=self.config.hv_lookback_days + 10)
            if df.empty or "close" not in df.columns:
                return 1.0
            daily_closes = df.groupby("date")["close"].last().sort_index().values[-self.config.hv_lookback_days:]
            if len(daily_closes) < 5:
                return 1.0
            valid = daily_closes[daily_closes > 0]
            if len(valid) < 5:
                return 1.0
            hv = float(np.std(np.diff(np.log(valid))) * np.sqrt(252) * 100)
            return current_iv_pct / hv if hv > 0 else 1.0
        except Exception:
            return 1.0

    def _calculate_straddle_ratio(self, straddle: float, weekday: int) -> float:
        try:
            df = self.db.get_daily_summary(days=120)
            if df.empty or "realized_move" not in df.columns:
                return 1.0
            same = df[df["weekday"] == weekday]["realized_move"].dropna()
            if len(same) < 5:
                if "day_range_points" in df.columns:
                    same_r = df[df["weekday"] == weekday]["day_range_points"].dropna()
                    if len(same_r) >= 5:
                        avg_r = same_r.mean()
                        avg = avg_r / 1.6 if avg_r > 0 else 0
                        return straddle / avg if avg > 0 else 1.0
                return 1.0
            avg = same.mean()
            return straddle / avg if avg > 0 else 1.0
        except Exception:
            return 1.0

    def classify_positioning(self, signals: dict) -> PositioningRegime:
        _cal = self.cal
        _skew_bear = _cal.skew_bearish_threshold if (_cal and _cal.is_calibrated) else self.config.skew_bearish_threshold
        _skew_bull = _cal.skew_bullish_threshold if (_cal and _cal.is_calibrated) else self.config.skew_bullish_threshold
        _oi_build  = _cal.oi_buildup_threshold   if (_cal and _cal.is_calibrated) else self.config.oi_buildup_threshold
        _oi_unwind = _cal.oi_unwind_threshold    if (_cal and _cal.is_calibrated) else self.config.oi_unwind_threshold

        chain_size = signals.get("chain_size") or 71
        total_strikes_half = max(chain_size // 2, 1)
        total_ce_oi = signals.get("total_ce_oi", 0) or 0
        total_pe_oi = signals.get("total_pe_oi", 0) or 0
        resistance_oi = signals.get("resistance_oi", 0) or 0
        support_oi    = signals.get("support_oi", 0) or 0
        resistance_strike = signals.get("resistance_strike", 0) or 0
        support_strike    = signals.get("support_strike", 0) or 0
        spot = signals.get("spot") or 24000.0

        avg_ce = max(total_ce_oi / total_strikes_half, 1) if total_ce_oi > 0 else 1.0
        avg_pe = max(total_pe_oi / total_strikes_half, 1) if total_pe_oi > 0 else 1.0
        r_str  = resistance_oi / avg_ce if total_ce_oi > 0 else 0.0
        s_str  = support_oi    / avg_pe if total_pe_oi > 0 else 0.0

        rng_pct = (
            abs(resistance_strike - support_strike) / spot * 100
            if resistance_strike and support_strike else 0.0
        )

        pcr       = signals.get("pcr", 1.0) or 1.0
        oi_change = signals.get("oi_change_pct", 0.0) or 0.0
        skew      = signals.get("skew", 0.0) or 0.0

        strong = self._t("oi_wall_strong_threshold", "oi_wall_strong")
        mod    = _cal.oi_wall_moderate_cal if (_cal and _cal.is_calibrated) else self.config.oi_wall_moderate

        wall_range        = r_str >= mod and s_str >= mod and rng_pct < 4.0
        wall_strong_range = r_str >= strong and s_str >= strong and 0 < rng_pct < 3.0
        oi_building  = oi_change > _oi_build
        oi_unwinding = oi_change < _oi_unwind

        pcr_extreme_bull = pcr < self.config.pcr_extreme_bull
        pcr_extreme_bear = pcr > self.config.pcr_extreme_bear
        pcr_bullish      = pcr < self.config.pcr_bullish_threshold
        pcr_bearish      = pcr > self.config.pcr_bearish_threshold
        skew_bearish     = skew > _skew_bear
        skew_bullish     = skew < _skew_bull

        event_day = getattr(self._engine_ref, "_event_day", False) if self._engine_ref else False
        if event_day and skew_bearish:
            return PositioningRegime.BEARISH
        if event_day and skew_bullish:
            return PositioningRegime.BULLISH

        if wall_strong_range and oi_building and not pcr_extreme_bull and not pcr_extreme_bear:
            return PositioningRegime.STRONG_RANGE
        if wall_range and not oi_unwinding and not pcr_extreme_bull and not pcr_extreme_bear:
            return PositioningRegime.RANGE
        if pcr_extreme_bull or (s_str > r_str * 1.5 and pcr_bullish and not skew_bearish):
            return PositioningRegime.BULLISH
        if pcr_extreme_bear or (r_str > s_str * 1.5 and pcr_bearish) or skew_bearish:
            return PositioningRegime.BEARISH
        if skew_bullish and pcr_bullish:
            return PositioningRegime.BULLISH
        if wall_range:
            return PositioningRegime.RANGE
        if s_str > r_str * 1.4:
            return PositioningRegime.BULLISH
        if r_str > s_str * 1.4:
            return PositioningRegime.BEARISH
        return PositioningRegime.UNCLEAR

    def classify_final(
        self,
        signals: dict,
        vol: VolatilityRegime,
        price: PriceRegime,
        pos: PositioningRegime,
        day_type: str,
        dte: int,
        event_day: bool,
        event_name: str,
    ) -> Tuple[FinalRegime, ConfidenceLevel, float, float, bool, str]:
        now   = now_ist().time()
        notes = []
        if event_day:
            notes.append(f"EVENT:{event_name}")

        if vol == VolatilityRegime.ABORT:
            return FinalRegime.EMERGENCY_EXIT, ConfidenceLevel.NONE, 0.0, 0.0, False, "ABORT: VIX emergency"
        if price == PriceRegime.OBSERVING:
            return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, False, "NO_TRADE: Price regime OBSERVING — OR not established"
        if price == PriceRegime.CHOPPY:
            return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, False, "NO_TRADE: Choppy"
        if now >= time(14, 45):
            return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, False, "NO_TRADE: Past 14:45"
        if now < time(9, 45):
            return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, False, "NO_TRADE: Before 09:45"

        if event_day and getattr(self.config, "defined_risk_only_on_event", True):
            notes.append(f"EVENT_RESTRICTION:{event_name}")

        spot = signals.get("spot") or 0
        max_pain = signals.get("max_pain", 0) or 0
        vix = signals.get("vix") or 15.0

        if (day_type == "EXPIRY_DAY" and now >= time(13, 0) and
                max_pain > 0 and vix < self.config.vix_high):
            dist = abs(spot - max_pain)
            if dist > 100:
                notes.append(f"MaxPain={max_pain} dist={dist:.0f}")
                labels = {0: "MONDAY", 1: "TUESDAY", 2: "WEDNESDAY", 3: "THURSDAY", 4: "FRIDAY"}
                day_label = labels.get(now_ist().weekday(), "MONDAY")
                _cal_st2 = self.cal
                _dsm2 = {
                    0: getattr(_cal_st2, "day_size_monday",    0.75) if _cal_st2 else 0.75,
                    1: getattr(_cal_st2, "day_size_tuesday",   0.60) if _cal_st2 else 0.60,
                    2: getattr(_cal_st2, "day_size_wednesday", 0.75) if _cal_st2 else 0.75,
                    3: getattr(_cal_st2, "day_size_thursday",  0.75) if _cal_st2 else 0.75,
                    4: getattr(_cal_st2, "day_size_friday",    0.50) if _cal_st2 else 0.50,
                }
                day_m = _dsm2.get(now_ist().weekday(), 0.5)
                raw_size = day_m * 0.5
                final_size = raw_size * self.config.event_size_multiplier if event_day else raw_size
                return FinalRegime.EXPIRY_MAX_PAIN, ConfidenceLevel.MEDIUM, final_size, raw_size, event_day, " | ".join(notes)

        votes = []
        agreements = 0

        if vol in (VolatilityRegime.STRONG_SELL_PREMIUM, VolatilityRegime.SELL_PREMIUM):
            votes.append("SELL"); agreements += 1
        elif vol == VolatilityRegime.BUY_OPTIONS:
            votes.append("BUY"); agreements += 1
        else:
            votes.append("NEUTRAL")

        if price == PriceRegime.RANGE:
            votes.append("RANGE"); agreements += 1
        elif price in (PriceRegime.UPTREND, PriceRegime.STRONG_UPTREND):
            votes.append("BULL"); agreements += 1
        elif price in (PriceRegime.DOWNTREND, PriceRegime.STRONG_DOWNTREND):
            votes.append("BEAR"); agreements += 1
        else:
            votes.append("UNCLEAR")

        if pos in (PositioningRegime.STRONG_RANGE, PositioningRegime.RANGE):
            votes.append("RANGE"); agreements += 1
        elif pos == PositioningRegime.BULLISH:
            votes.append("BULL"); agreements += 1
        elif pos == PositioningRegime.BEARISH:
            votes.append("BEAR"); agreements += 1
        else:
            votes.append("UNCLEAR")

        notes.append(f"votes={votes}")

        conf = (
            ConfidenceLevel.HIGH   if agreements == 3 else
            ConfidenceLevel.MEDIUM if agreements == 2 else
            ConfidenceLevel.LOW    if agreements == 1 else
            ConfidenceLevel.NONE
        )
        if conf == ConfidenceLevel.NONE:
            return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, False, "NO_TRADE: No agreement"

        _cal_state = self.cal
        _day_size_map_from_cal = {
            0: getattr(_cal_state, "day_size_monday",    0.75) if _cal_state else 0.75,
            1: getattr(_cal_state, "day_size_tuesday",   0.60) if _cal_state else 0.60,
            2: getattr(_cal_state, "day_size_wednesday", 0.75) if _cal_state else 0.75,
            3: getattr(_cal_state, "day_size_thursday",  0.75) if _cal_state else 0.75,
            4: getattr(_cal_state, "day_size_friday",    0.50) if _cal_state else 0.50,
        }
        _vix_level = signals.get("vix") or 15.0
        _vix_size_cap = 1.0
        if _vix_level >= getattr(self.config, "vix_extreme_high", 28.0):
            _vix_size_cap = 0.25
        elif _vix_level >= getattr(self.config, "vix_high", 22.0):
            _vix_size_cap = 0.50
        elif _vix_level >= getattr(self.config, "vix_normal", 16.0):
            _vix_size_cap = 0.75
        day_m  = _day_size_map_from_cal.get(now_ist().weekday(), 0.5)
        conf_m = {ConfidenceLevel.HIGH: 1.0, ConfidenceLevel.MEDIUM: 0.5, ConfidenceLevel.LOW: 0.25}.get(conf, 0)
        raw_size = day_m * conf_m
        raw_size = min(raw_size, _vix_size_cap)
        if now_ist().weekday() == 4:
            raw_size = min(raw_size, 0.50)
        final_size = raw_size * self.config.event_size_multiplier if event_day else raw_size
        defined_risk = event_day and self.config.defined_risk_only_on_event

        sell   = votes.count("SELL")
        buy    = votes.count("BUY")
        range_ = votes.count("RANGE")
        bull   = votes.count("BULL")
        bear   = votes.count("BEAR")

        if sell >= 1 and range_ >= 1:
            _candidate = FinalRegime.PREMIUM_SELL_RANGE
        elif sell >= 1 and bull >= 1:
            _candidate = FinalRegime.PREMIUM_SELL_BULL
        elif sell >= 1 and bear >= 1:
            _candidate = FinalRegime.PREMIUM_SELL_BEAR
        elif buy >= 1 and range_ >= 1:
            _candidate = FinalRegime.BUY_STRADDLE
        elif buy >= 1 and bull >= 1:
            _candidate = FinalRegime.BUY_DIRECTIONAL_BULL
        elif buy >= 1 and bear >= 1:
            _candidate = FinalRegime.BUY_DIRECTIONAL_BEAR
        elif range_ >= 2:
            _candidate = FinalRegime.PREMIUM_SELL_RANGE
        else:
            return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, False, "NO_TRADE: No regime map"

        if event_day and getattr(self.config, "defined_risk_only_on_event", True):
            _naked = {FinalRegime.PREMIUM_SELL_BULL, FinalRegime.PREMIUM_SELL_BEAR}
            if _candidate in _naked:
                notes.append("EVENT:NAKED_PREMIUM_BLOCKED")
                return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, True, " | ".join(notes)
            if _candidate == FinalRegime.PREMIUM_SELL_RANGE and conf != ConfidenceLevel.HIGH:
                notes.append("EVENT:PREMIUM_SELL_RANGE_NEEDS_HIGH_CONF")
                return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, True, " | ".join(notes)
            if _candidate == FinalRegime.BUY_STRADDLE:
                notes.append("EVENT:STRADDLE_BLOCKED_ON_EVENT_DAY")
                return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, True, " | ".join(notes)

        return _candidate, conf, final_size, raw_size, defined_risk, " | ".join(notes)


class RegimeEngine:

    def __init__(
        self,
        config: Config,
        db: Database,
        market_engine: MarketDataEngine,
        logger,
    ):
        self.config = config
        self.db = db
        self.market_engine = market_engine
        self.logger = logger

        self._regime: Optional[RegimeSnapshot] = None
        self._pending_regime: Optional[RegimeSnapshot] = None
        self._pending_regime_count: int = 0
        self._vix_fail_count: int = 0
        self._event_day: bool = False
        self._event_name: str = ""
        self._last_reset_date: Optional[date] = None
        self._straddle_history: list = []

        self.calibrator = CalibrationEngine(db, config, logger)
        self.classifier = RegimeClassifier(
            db, config, self.calibrator.state,
            engine_ref=self, logger=logger
        )

        self.logger.info("RegimeEngine initialized (integrated mode).")

    def _daily_reset_if_needed(self) -> None:
        today = today_ist()
        if self._last_reset_date == today:
            return
        self._pending_regime       = None
        self._pending_regime_count = 0
        self._vix_fail_count       = 0
        self._straddle_history     = []
        self._event_name = ExpiryCalendar.is_event_day(today)
        self._event_day  = bool(self._event_name)
        if self._event_day:
            self.logger.warning(
                f"EVENT DAY: {self._event_name} | "
                f"Size reduced {int((1-self.config.event_size_multiplier)*100)}% | "
                f"Defined risk only"
            )
        self._last_reset_date = today
        self.logger.info(f"RegimeEngine daily reset complete for {today}")

    def _check_straddle_explosion(self, straddle: float) -> bool:
        if straddle <= 0:
            return False
        opening_straddle = self.market_engine.state.get("_straddle_open_for_regime", 0)
        if opening_straddle > 0:
            chg = (straddle - opening_straddle) / opening_straddle * 100
            if chg >= self.config.straddle_explosion_pct:
                self.logger.warning(f"STRADDLE EXPLOSION: +{chg:.1f}%")
                return True
        self._straddle_history.append((datetime.now(), straddle))
        cutoff = datetime.now().timestamp() - self.config.straddle_roc_window_min * 60
        self._straddle_history = [
            (t, v) for t, v in self._straddle_history if t.timestamp() >= cutoff
        ]
        if len(self._straddle_history) >= 2:
            oldest = self._straddle_history[0][1]
            if oldest > 0:
                roc = (straddle - oldest) / oldest * 100
                if roc >= self.config.straddle_roc_alert_pct:
                    self.logger.warning(
                        f"STRADDLE ROC ALERT: +{roc:.1f}% in "
                        f"{self.config.straddle_roc_window_min}min"
                    )
                    return True
        return False

    def _record_opening_straddle(self, straddle: float) -> None:
        state = self.market_engine.state
        if (straddle > 0 and
                state.get("_straddle_open_for_regime", 0) == 0 and
                now_ist().time() >= time(9, 30)):
            state["_straddle_open_for_regime"] = straddle
            self.logger.info(f"Regime opening straddle recorded: {straddle:.2f}")

    def _apply_persistence_filter(self, new_regime: RegimeSnapshot) -> RegimeSnapshot:
        immediate = {FinalRegime.EMERGENCY_EXIT.value, FinalRegime.NO_TRADE.value}
        if new_regime.final_regime in immediate:
            self._pending_regime       = None
            self._pending_regime_count = 0
            return new_regime
        if self._regime is None:
            return new_regime
        if self._regime.timestamp.date() != today_ist():
            self._pending_regime       = None
            self._pending_regime_count = 0
            return new_regime
        if new_regime.final_regime == self._regime.final_regime:
            self._pending_regime       = None
            self._pending_regime_count = 0
            return new_regime
        if (self._pending_regime is not None and
                self._pending_regime.final_regime == new_regime.final_regime):
            self._pending_regime_count += 1
            if self._pending_regime_count >= self.config.regime_confirm_cycles:
                self.logger.info(
                    f"Regime confirmed after {self._pending_regime_count} cycles: "
                    f"{new_regime.final_regime}"
                )
                self._pending_regime       = None
                self._pending_regime_count = 0
                return new_regime
            self.logger.debug(
                f"Regime pending ({self._pending_regime_count}/"
                f"{self.config.regime_confirm_cycles}): {new_regime.final_regime}"
            )
            return self._regime
        self._pending_regime       = new_regime
        self._pending_regime_count = 1
        self.logger.debug(
            f"New regime candidate (1/{self.config.regime_confirm_cycles}): "
            f"{new_regime.final_regime}"
        )
        return self._regime

    def _persist_regime_decision(self, r: RegimeSnapshot) -> None:
        try:
            ts = r.timestamp
            self.db.insert("regime_decisions", {
                "timestamp": ts.isoformat(),
                "date": ts.date().isoformat(),
                "time": ts.strftime("%H:%M:%S"),
                "weekday": ts.weekday(),
                "dte": r.dte,
                "day_type": r.day_type,
                "event_day": int(r.event_day),
                "event_name": r.event_name,
                "defined_risk_only": int(r.defined_risk_only),
                "volatility_regime": r.volatility_regime,
                "price_regime": r.price_regime,
                "price_regime_15": r.price_regime_15,
                "price_regime_60": r.price_regime_60,
                "mtf_aligned": int(r.mtf_aligned),
                "positioning_regime": r.positioning_regime,
                "final_regime": r.final_regime,
                "confidence": r.confidence,
                "size_multiplier": r.size_multiplier,
                "raw_size_multiplier": r.raw_size_multiplier,
                "vix_level": r.vix_level,
                "vix_roc": r.vix_roc,
                "ivr": r.ivr,
                "iv_hv_ratio": r.iv_hv_ratio,
                "straddle_ratio": r.straddle_ratio,
                "adx_15": r.adx_15,
                "adx_60": r.adx_60,
                "ema_structure": r.ema_structure,
                "oi_wall_strength": r.oi_wall_strength,
                "oi_change_pct": r.oi_change_pct,
                "skew": r.skew,
                "max_pain_distance": r.max_pain_distance,
                "pcr": r.pcr,
                "notes": r.notes,
                "is_calibrated": int(r.is_calibrated),
                "calibration_tier": r.calibration_tier,
            })
        except Exception as e:
            self.logger.debug(f"Could not persist regime decision: {e}")

    def calculate_regime(self, signals: dict) -> RegimeSnapshot:
        self._daily_reset_if_needed()
        self.classifier.cal         = self.calibrator.state
        self.classifier._engine_ref = self

        ts       = signals.get("timestamp") or datetime.now()
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except Exception:
                ts = datetime.now()

        today_d  = ts.date() if hasattr(ts, "date") else date.today()
        dte      = ExpiryCalendar.get_dte(today_d)
        day_type = ExpiryCalendar.get_day_type(today_d)

        straddle = signals.get("atm_straddle_price", 0) or 0
        self._record_opening_straddle(straddle)

        if self._check_straddle_explosion(straddle):
            snap = RegimeSnapshot(
                timestamp=ts, day_type=day_type, dte=dte,
                event_day=self._event_day, event_name=self._event_name,
                defined_risk_only=self._event_day,
                volatility_regime=VolatilityRegime.ABORT.value,
                price_regime=PriceRegime.OBSERVING.value,
                price_regime_15=PriceRegime.OBSERVING.value,
                price_regime_60=PriceRegime.OBSERVING.value,
                mtf_aligned=False,
                positioning_regime=PositioningRegime.UNCLEAR.value,
                final_regime=FinalRegime.EMERGENCY_EXIT.value,
                confidence=ConfidenceLevel.NONE.value,
                size_multiplier=0.0, raw_size_multiplier=0.0,
                vix_level=signals.get("vix") or 0.0, vix_roc=0.0,
                ivr=0.0, iv_hv_ratio=0.0, straddle_ratio=0.0,
                adx_15=0.0, adx_60=0.0, ema_structure="N/A",
                oi_wall_strength=0.0,
                oi_change_pct=signals.get("oi_change_pct") or 0.0,
                skew=signals.get("skew") or 0.0,
                max_pain_distance=0.0,
                pcr=signals.get("pcr") or 1.0,
                notes="EMERGENCY: Straddle explosion",
                is_calibrated=bool(self.calibrator.state and self.calibrator.state.is_calibrated),
                calibration_tier=self.calibrator.state.calibration_tier if self.calibrator.state else 0,
            )
            return snap

        vol, vol_d = self.classifier.classify_volatility(signals)

        price_regime_15 = signals.get("price_regime_15") or "OBSERVING"
        price_regime_60 = signals.get("price_regime_60") or "OBSERVING"
        mtf_aligned     = signals.get("mtf_aligned", False)
        adx_15          = signals.get("adx_15") or 0.0
        adx_60          = signals.get("adx_60") or 0.0
        ema_structure   = signals.get("ema_structure") or "INSUFFICIENT_DATA"

        try:
            price_enum = PriceRegime(price_regime_15)
        except ValueError:
            price_enum = PriceRegime.OBSERVING

        pos = self.classifier.classify_positioning(signals)

        final, conf, final_size, raw_size, defined_risk, notes = self.classifier.classify_final(
            signals, vol, price_enum, pos, day_type, dte,
            self._event_day, self._event_name
        )

        cur_iv_raw = signals.get("atm_iv") or (signals.get("vix") or 15.0) / 100.0
        cur_iv_pct = cur_iv_raw * 100.0 if cur_iv_raw < 2.0 else cur_iv_raw
        ivr     = self.classifier._calculate_ivr(cur_iv_pct)
        iv_hv   = self.classifier._calculate_iv_hv_ratio(cur_iv_pct)
        s_ratio = self.classifier._calculate_straddle_ratio(
            signals.get("atm_straddle_price", 0) or 0,
            ts.weekday() if hasattr(ts, "weekday") else 0
        )

        _sh   = max((signals.get("chain_size") or 71) // 2, 1)
        _ace  = max((signals.get("total_ce_oi") or 0) / _sh, 1) if (signals.get("total_ce_oi") or 0) > 0 else 1.0
        _ape  = max((signals.get("total_pe_oi") or 0) / _sh, 1) if (signals.get("total_pe_oi") or 0) > 0 else 1.0
        r_str = (signals.get("resistance_oi") or 0) / _ace if (signals.get("total_ce_oi") or 0) > 0 else 0.0
        s_str = (signals.get("support_oi") or 0)    / _ape if (signals.get("total_pe_oi") or 0) > 0 else 0.0
        mp_dist = abs((signals.get("spot") or 0) - (signals.get("max_pain") or 0)) if signals.get("max_pain") else 0.0

        snap = RegimeSnapshot(
            timestamp=ts, day_type=day_type, dte=dte,
            event_day=self._event_day, event_name=self._event_name,
            defined_risk_only=defined_risk,
            volatility_regime=vol.value,
            price_regime=price_regime_15,
            price_regime_15=price_regime_15,
            price_regime_60=price_regime_60,
            mtf_aligned=mtf_aligned,
            positioning_regime=pos.value,
            final_regime=final.value,
            confidence=conf.value,
            size_multiplier=final_size,
            raw_size_multiplier=raw_size,
            vix_level=signals.get("vix") or 0.0,
            vix_roc=vol_d.get("vix_roc", 0.0),
            ivr=ivr, iv_hv_ratio=iv_hv, straddle_ratio=s_ratio,
            adx_15=adx_15, adx_60=adx_60, ema_structure=ema_structure,
            oi_wall_strength=max(r_str, s_str),
            oi_change_pct=signals.get("oi_change_pct") or 0.0,
            skew=signals.get("skew") or 0.0,
            max_pain_distance=mp_dist,
            pcr=signals.get("pcr") or 1.0,
            notes=notes,
            is_calibrated=bool(self.calibrator.state and self.calibrator.state.is_calibrated),
            calibration_tier=self.calibrator.state.calibration_tier if self.calibrator.state else 0,
        )
        return snap

    def process_signals(self, signals: dict) -> RegimeSnapshot:
        self._daily_reset_if_needed()
        raw_regime = self.calculate_regime(signals)
        confirmed  = self._apply_persistence_filter(raw_regime)

        if self._regime is None or confirmed.final_regime != self._regime.final_regime:
            self._log_regime_change(self._regime, confirmed)
            filter_held = (
                self._regime is not None and
                confirmed.final_regime == self._regime.final_regime and
                raw_regime.final_regime != confirmed.final_regime
            )
            if not filter_held:
                self._persist_regime_decision(confirmed)

        self._regime = confirmed
        return confirmed

    def _log_regime_change(
        self, old: Optional[RegimeSnapshot], new: RegimeSnapshot
    ) -> None:
        if old is None or old.final_regime != new.final_regime:
            self.logger.info("-" * 55)
            self.logger.info(f"  REGIME -> {new.final_regime}")
            if new.event_day:
                self.logger.warning(
                    f"  EVENT: {new.event_name} | SIZE x{self.config.event_size_multiplier} | "
                    f"DEFINED RISK ONLY: {new.defined_risk_only}"
                )
            self.logger.info(f"  Day={new.day_type} DTE={new.dte}")
            self.logger.info(f"  Vol={new.volatility_regime}")
            self.logger.info(
                f"  Price15={new.price_regime_15} Price60={new.price_regime_60} "
                f"MTF={new.mtf_aligned}"
            )
            self.logger.info(
                f"  ADX15={new.adx_15:.1f} ADX60={new.adx_60:.1f} "
                f"EMA={new.ema_structure}"
            )
            self.logger.info(
                f"  Pos={new.positioning_regime} "
                f"OIChg={new.oi_change_pct:.2%} Skew={new.skew:.2f}"
            )
            self.logger.info(
                f"  Conf={new.confidence} RawSize={new.raw_size_multiplier:.2f} "
                f"FinalSize={new.size_multiplier:.2f}"
            )
            self.logger.info(
                f"  VIX={new.vix_level:.2f} ROC={new.vix_roc:.2f}% "
                f"IVR={new.ivr:.1f} IV/HV={new.iv_hv_ratio:.2f}"
            )
            self.logger.info(
                f"  OIWall={new.oi_wall_strength:.2f} "
                f"MaxPainDist={new.max_pain_distance:.0f}pts PCR={new.pcr:.3f}"
            )
            self.logger.info(
                f"  Calibrated={new.is_calibrated} Tier={new.calibration_tier}"
            )
            self.logger.info(f"  Notes: {new.notes}")
            self.logger.info("-" * 55)

    def get_current_regime(self) -> Optional[RegimeSnapshot]:
        return self._regime

    def run_calibration(self, force: bool = False) -> Optional[CalibrationState]:
        if not force and not self._is_market_open():
            return None
        try:
            cal = self.calibrator.run()
            if cal:
                self.classifier.cal         = cal
                self.classifier._engine_ref = self
            return cal
        except Exception as e:
            self.logger.error(f"Calibration error: {e}", exc_info=True)
            return None

    def _is_market_open(self) -> bool:
        if ExpiryCalendar.is_holiday(today_ist()):
            return False
        now = now_ist().time()
        return time(9, 15) <= now <= time(15, 30)


def _self_test() -> None:
    from nifty_algo_core import print_section, print_kv_table
    print_section("NIFTY ALGO — REGIME ENGINE SELF-TEST")
    config = load_config()
    db = Database(config.db_path)
    logger = setup_logging(db, config.log_dir)
    rate_limiter = RateLimiter(config.rate_limits)
    client = UpstoxClient(config, rate_limiter, db, logger)
    market_engine = MarketDataEngine(config, db, client, rate_limiter, logger)
    regime_engine = RegimeEngine(config, db, market_engine, logger)

    regime_engine.run_calibration(force=True)

    if not config.upstox_access_token or not client.validate_token():
        logger.warning("No valid Upstox token — cannot run live cycle test.")
        db.close()
        return

    signals = market_engine.run_cycle()
    regime_snapshot = regime_engine.process_signals(signals)

    print_section("REGIME ENGINE OUTPUT")
    print_kv_table({
        "Final Regime": regime_snapshot.final_regime,
        "Confidence": regime_snapshot.confidence,
        "Raw Size": regime_snapshot.raw_size_multiplier,
        "Final Size": regime_snapshot.size_multiplier,
        "Vol Regime": regime_snapshot.volatility_regime,
        "Price Regime 15": regime_snapshot.price_regime_15,
        "Price Regime 60": regime_snapshot.price_regime_60,
        "MTF Aligned": regime_snapshot.mtf_aligned,
        "Positioning": regime_snapshot.positioning_regime,
        "ADX-15": regime_snapshot.adx_15,
        "ADX-60": regime_snapshot.adx_60,
        "EMA Structure": regime_snapshot.ema_structure,
        "OI Change": f"{regime_snapshot.oi_change_pct:.2%}",
        "Skew": regime_snapshot.skew,
        "Event Day": regime_snapshot.event_day,
        "Defined Risk Only": regime_snapshot.defined_risk_only,
        "Calibrated": regime_snapshot.is_calibrated,
        "Calibration Tier": regime_snapshot.calibration_tier,
        "Notes": regime_snapshot.notes,
    })
    db.close()


if __name__ == "__main__":
    _self_test()