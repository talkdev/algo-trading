# ============ FILE: regime_engine.py ============
"""
Computes regime scores and maps to trading regime.
Handles vol/edge/trend/flow scores, persistence filtering,
macro override, composite aggregation, and regime mapping.
"""

import logging
import sqlite3
import json
import numpy as np
from collections import deque
from datetime import datetime
from typing import Optional
from typing import Optional, Dict, List
import pytz
import config
from data_manager import DataManager

logger = logging.getLogger(__name__)


class RegimeEngine:
    """
    Detects and confirms market regime using four composite scores.
    Applies persistence filter to avoid regime whipsawing.
    """

    def __init__(self, data_manager: DataManager, db_path: str) -> None:
        """Initialize RegimeEngine with data manager and database path."""
        self.dm = data_manager
        self.db_path = db_path
        self._IST = pytz.timezone(config.TZ)

        # Score buffers (circular, size=PERSISTENCE_READINGS)
        self.vol_buffer: deque = deque(maxlen=config.PERSISTENCE_READINGS)
        self.edge_buffer: deque = deque(maxlen=config.PERSISTENCE_READINGS)
        self.trend_buffer: deque = deque(maxlen=config.PERSISTENCE_READINGS)
        self.flow_buffer: deque = deque(maxlen=config.PERSISTENCE_READINGS)

        # Confirmed scores
        self.confirmed_vol: float = 0.0
        self.confirmed_edge: float = 0.0
        self.confirmed_trend: float = 0.0
        self.confirmed_flow: float = 0.0

        # Regime state
        self.raw_composite: float = 0.0
        self.confirmed_regime: str = config.REGIME_NEUTRAL
        self.previous_regime: str = config.REGIME_NEUTRAL
        self.regime_changed: bool = False
        self.persistence_count: int = 0
        self.last_refresh_time: Optional[datetime] = None

        # Score history for logging (288 = one full trading day of 5-min bars)
        self.score_history: deque = deque(maxlen=288)

    async def refresh(self) -> str:
        """
        Main method called every 5 minutes.
        Orchestrates all score computations and regime detection.
        Returns confirmed regime string.
        """
        logger.info("Regime refresh started")

        # Step 0: Pre-processing validation
        if self.dm.spot is None:
            logger.warning("Regime refresh skipped: spot is None")
            return self.confirmed_regime
        if self.dm.vix is None:
            logger.warning("Regime refresh skipped: vix is None")
            return self.confirmed_regime
        if not self.dm.option_chain:
            logger.warning("Regime refresh skipped: option chain empty")
            return self.confirmed_regime

        # Step 1: Compute raw scores
        raw_vol = self._compute_vol_score()
        raw_edge = self._compute_edge_score()
        raw_trend = self._compute_trend_score()
        raw_flow = self._compute_flow_score()

        # Step 2: Apply persistence filter
        conf_vol = self._apply_persistence(
            raw_vol, self.vol_buffer, self.confirmed_vol
        )
        conf_edge = self._apply_persistence(
            raw_edge, self.edge_buffer, self.confirmed_edge
        )
        conf_trend = self._apply_persistence(
            raw_trend, self.trend_buffer, self.confirmed_trend
        )
        conf_flow = self._apply_persistence(
            raw_flow, self.flow_buffer, self.confirmed_flow
        )

        self.confirmed_vol = conf_vol
        self.confirmed_edge = conf_edge
        self.confirmed_trend = conf_trend
        self.confirmed_flow = conf_flow

        # Step 3: Check macro override
        macro_active = self._check_macro_override()

        if macro_active:
            new_regime = config.REGIME_EVENT
            logger.info(
                f"Macro override active — forcing {config.REGIME_EVENT}"
            )
        else:
            # Step 4: Compute composite score
            composite = (
                config.WEIGHT_VOL * conf_vol +
                config.WEIGHT_EDGE * conf_edge +
                config.WEIGHT_TREND * conf_trend +
                config.WEIGHT_FLOW * conf_flow
            )
            # Clamp to [-1.0, +1.0]
            self.raw_composite = float(max(-1.0, min(1.0, composite)))

            # Step 5: Map to regime
            new_regime = self._map_to_regime(self.raw_composite)

        # Step 6: Detect regime change
        self.regime_changed = (new_regime != self.confirmed_regime)

        if self.regime_changed:
            self.previous_regime = self.confirmed_regime
            self.confirmed_regime = new_regime
            self.persistence_count = 1
            logger.info(
                f"REGIME CHANGE: {self.previous_regime} -> {self.confirmed_regime} | "
                f"composite={self.raw_composite:.4f} | "
                f"vol={conf_vol:.2f} edge={conf_edge:.2f} "
                f"trend={conf_trend:.2f} flow={conf_flow:.2f}"
            )
        else:
            self.persistence_count += 1

        # Step 7: Save to SQLite
        self._save_regime_to_sqlite({
            "timestamp": datetime.now(self._IST).isoformat(),
            "vol_score": conf_vol,
            "edge_score": conf_edge,
            "trend_score": conf_trend,
            "flow_score": conf_flow,
            "composite_score": self.raw_composite,
            "raw_regime": new_regime,
            "confirmed_regime": self.confirmed_regime,
            "persistence_count": self.persistence_count,
            "macro_override": 1 if macro_active else 0
        })

        # Step 8: Log console output
        self._log_console_output()

        self.last_refresh_time = datetime.now(self._IST)

        # Record in score history
        self.score_history.append({
            "timestamp": self.last_refresh_time.isoformat(),
            "vol": conf_vol,
            "edge": conf_edge,
            "trend": conf_trend,
            "flow": conf_flow,
            "composite": self.raw_composite,
            "regime": self.confirmed_regime
        })

        logger.info(
            f"Regime refresh complete: {self.confirmed_regime} "
            f"(composite={self.raw_composite:.4f}, "
            f"persist={self.persistence_count})"
        )
        return self.confirmed_regime

    def _compute_vol_score(self) -> float:
        """Compute volatility score from term spread and skew z-score."""
        z = 0.0

        # TERM SPREAD COMPONENT
        if self.dm.forward_iv is None or self.dm.vix is None:
            term_score = 0
        else:
            term_spread = self.dm.forward_iv - self.dm.vix
            if term_spread > config.TERM_SPREAD_CONTANGO:
                term_score = +1
            elif term_spread < config.TERM_SPREAD_BACKWARDATION:
                term_score = -1
            else:
                term_score = 0

        # SKEW Z-SCORE COMPONENT
        if len(self.dm.skew_history) < 10:
            skew_score = 0
        else:
            skew_arr = np.array(list(self.dm.skew_history))
            skew_mean = float(np.mean(skew_arr))
            skew_std = float(np.std(skew_arr))

            if skew_std < 1e-10:
                skew_score = 0
            else:
                z = (self.dm.skew - skew_mean) / skew_std if self.dm.skew is not None else 0.0
                if z > config.SKEW_ZSCORE_FEAR:
                    skew_score = -1
                elif z < config.SKEW_ZSCORE_COMPLACENT:
                    skew_score = +1
                else:
                    skew_score = 0

        vol_score = (0.5 * term_score) + (0.5 * skew_score)
        vol_score = float(max(-1.0, min(1.0, vol_score)))

        logger.info(
            f"Vol_Score={vol_score:.2f} term={term_score} "
            f"skew_z={z:.2f} skew_score={skew_score}"
        )
        return vol_score

    def _compute_edge_score(self) -> float:
        """Compute edge score from IV vs RV percentile comparison."""
        if self.dm.rv_20d is None:
            logger.info("Edge_Score=0.0 (rv_20d is None)")
            return 0.0
        if self.dm.iv_atm is None:
            logger.info("Edge_Score=0.0 (iv_atm is None)")
            return 0.0
        if len(self.dm.iv_rv_spread_history) < 10:
            logger.info(
                f"Edge_Score=0.0 (insufficient history: "
                f"{len(self.dm.iv_rv_spread_history)}/10)"
            )
            return 0.0

        current_spread = self.dm.iv_atm - self.dm.rv_20d
        spread_array = np.array(list(self.dm.iv_rv_spread_history))
        pct_70 = float(np.percentile(spread_array, config.EDGE_PERCENTILE_HIGH))
        pct_30 = float(np.percentile(spread_array, config.EDGE_PERCENTILE_LOW))

        if current_spread > pct_70:
            edge_score = +1.0
        elif current_spread < pct_30:
            edge_score = -1.0
        else:
            edge_score = 0.0

        logger.info(
            f"Edge_Score={edge_score:.2f} IV={self.dm.iv_atm:.4f} "
            f"RV={self.dm.rv_20d:.4f} spread={current_spread:.4f} "
            f"p70={pct_70:.4f} p30={pct_30:.4f}"
        )
        return float(edge_score)

    def _compute_trend_score(self) -> float:
        """Compute trend score from ADX and EMA slope."""
        if self.dm.adx is None:
            logger.info("Trend_Score=0.0 (adx is None)")
            return 0.0
        if self.dm.ema_50 is None:
            logger.info("Trend_Score=0.0 (ema_50 is None)")
            return 0.0
        if self.dm.ema_slope is None:
            logger.info("Trend_Score=0.0 (ema_slope is None)")
            return 0.0
        if self.dm.spot is None:
            logger.info("Trend_Score=0.0 (spot is None)")
            return 0.0

        adx = self.dm.adx
        slope = self.dm.ema_slope
        spot = self.dm.spot
        ema = self.dm.ema_50

        if (adx > config.ADX_TREND_THRESHOLD and
                abs(slope) > config.EMA_SLOPE_THRESHOLD):
            if spot > ema:
                trend_score = +1.0
            else:
                trend_score = -1.0
        elif adx < config.ADX_RANGE_THRESHOLD:
            trend_score = 0.0
        else:
            # Transition zone — use last confirmed
            trend_score = self.confirmed_trend

        logger.info(
            f"Trend_Score={trend_score:.2f} ADX={adx:.2f} "
            f"slope={slope:.6f} spot={spot:.2f} ema={ema:.2f}"
        )
        return float(trend_score)

    def _compute_flow_score(self) -> float:
        """Compute flow score from net OI flow and spread ratio."""
        if self.dm.net_flow is None:
            logger.info("Flow_Score=0.0 (net_flow is None)")
            return 0.0
        if self.dm.spread_ratio is None:
            logger.info("Flow_Score=0.0 (spread_ratio is None)")
            return 0.0

        net_flow = self.dm.net_flow
        spread_ratio = self.dm.spread_ratio

        if net_flow > 0 and spread_ratio < 1.0:
            flow_score = +1.0
        elif net_flow < 0 and spread_ratio > 1.0:
            flow_score = -1.0
        else:
            flow_score = 0.0

        logger.info(
            f"Flow_Score={flow_score:.2f} net_flow={net_flow:.4f} "
            f"spread_ratio={spread_ratio:.3f}"
        )
        return float(flow_score)

    def _apply_persistence(
        self,
        new_score: float,
        buffer: deque,
        last_confirmed: float
    ) -> float:
        """
        Apply persistence filter: confirm only if all 3 readings identical.
        Mixed buffer reverts to last confirmed value.
        """
        buffer.append(new_score)

        if len(buffer) < config.PERSISTENCE_READINGS:
            logger.info(
                f"Persistence buffer not full: {len(buffer)}/{config.PERSISTENCE_READINGS} "
                f"— using last_confirmed={last_confirmed:.2f}"
            )
            return last_confirmed

        # Check if all readings are identical
        unique_values = set(buffer)
        if len(unique_values) == 1:
            confirmed = buffer[-1]
            logger.info(
                f"Persistence confirmed: all {config.PERSISTENCE_READINGS} "
                f"readings = {confirmed:.2f}"
            )
            return confirmed
        else:
            logger.info(
                f"Persistence mixed: buffer={list(buffer)} "
                f"— reverting to last_confirmed={last_confirmed:.2f}"
            )
            return last_confirmed

    def _check_macro_override(self) -> bool:
        """Check if current time is within event window of high-impact event."""
        now = datetime.now(self._IST)

        for event_date_str, event_name in config.HIGH_IMPACT_EVENTS.items():
            try:
                event_dt = datetime.strptime(
                    event_date_str, "%Y-%m-%d"
                ).replace(
                    hour=9, minute=15, second=0, microsecond=0,
                    tzinfo=self._IST
                )

                hours_diff = (now - event_dt).total_seconds() / 3600.0

                if now < event_dt:
                    # Before event
                    hours_until = abs(hours_diff)
                    if hours_until <= config.EVENT_WINDOW_BEFORE_HOURS:
                        logger.info(
                            f"Macro override: {event_name} in "
                            f"{hours_until:.1f} hours"
                        )
                        return True
                else:
                    # After event
                    hours_after = hours_diff
                    if hours_after <= config.EVENT_WINDOW_AFTER_HOURS:
                        logger.info(
                            f"Macro override: {event_name} "
                            f"{hours_after:.1f} hours ago"
                        )
                        return True

            except (ValueError, Exception) as e:
                logger.warning(f"Macro override check error for {event_date_str}: {e}")
                continue

        return False

    def _map_to_regime(self, composite: float) -> str:
        """Map composite score to regime label."""
        if composite > config.STRONG_SELL_THRESHOLD:
            return config.REGIME_STRONG_SELL
        elif composite >= config.MILD_SELL_THRESHOLD:
            return config.REGIME_MILD_SELL
        elif composite > config.MILD_BUY_THRESHOLD:
            return config.REGIME_NEUTRAL
        elif composite >= config.STRONG_BUY_THRESHOLD:
            return config.REGIME_BUY_VOL
        else:
            return config.REGIME_STRONG_BUY

    def _save_regime_to_sqlite(self, data: Dict) -> None:
        """Save regime history record to SQLite. Never raises."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO regime_history (
                    timestamp, vol_score, edge_score,
                    trend_score, flow_score,
                    composite_score, raw_regime,
                    confirmed_regime, persistence_count,
                    macro_override
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("timestamp"),
                data.get("vol_score"),
                data.get("edge_score"),
                data.get("trend_score"),
                data.get("flow_score"),
                data.get("composite_score"),
                data.get("raw_regime"),
                data.get("confirmed_regime"),
                data.get("persistence_count"),
                data.get("macro_override", 0)
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"_save_regime_to_sqlite error: {e}")

    def _log_console_output(self) -> None:
        """Log formatted regime summary to console."""
        COLORS = {
            config.REGIME_STRONG_SELL: "\033[92m",
            config.REGIME_MILD_SELL:   "\033[32m",
            config.REGIME_NEUTRAL:     "\033[93m",
            config.REGIME_BUY_VOL:     "\033[91m",
            config.REGIME_STRONG_BUY:  "\033[31m",
            config.REGIME_EVENT:       "\033[95m",
        }
        RESET = "\033[0m"
        color = COLORS.get(self.confirmed_regime, "")
        now_str = datetime.now(self._IST).strftime("%Y-%m-%d %H:%M:%S")

        logger.info(
            f"\n┌─────────────────────────────────────────┐\n"
            f"│ {now_str} │\n"
            f"│ Spot={self.dm.spot or 'N/A':<10} VIX={self.dm.vix or 'N/A':<6} "
            f"Composite={self.raw_composite:+.4f} │\n"
            f"│ Vol={self.confirmed_vol:+.2f} Edge={self.confirmed_edge:+.2f} "
            f"Trend={self.confirmed_trend:+.2f} Flow={self.confirmed_flow:+.2f} │\n"
            f"│ Regime: {color}{self.confirmed_regime}{RESET} "
            f"(persist={self.persistence_count}) │\n"
            f"│ Action: {self.get_regime_action_description()} │\n"
            f"└─────────────────────────────────────────┘"
        )

    def get_regime_action_description(self) -> str:
        """Return human-readable action description for current regime."""
        descriptions = {
            config.REGIME_STRONG_SELL: "SELL PREMIUM: Straddle/Condor",
            config.REGIME_MILD_SELL:   "SELL DEFINED: Credit Spreads",
            config.REGIME_NEUTRAL:     "HOLD: Manage existing only",
            config.REGIME_BUY_VOL:     "DEFENSIVE: Hedge/Reduce",
            config.REGIME_STRONG_BUY:  "BUY VOL: Long Straddle/Backspread",
            config.REGIME_EVENT:       "EVENT: Long Strangle"
        }
        return descriptions.get(self.confirmed_regime, "UNKNOWN")

    def load_buffers_from_sqlite(self) -> None:
        """Restore score buffers and confirmed scores from SQLite."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM score_buffers")
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                logger.info("No score buffers in SQLite — starting fresh")
                return

            cols = ["id", "score_name", "buffer_json", "confirmed_score", "updated_at"]
            for row in rows:
                row_dict = dict(zip(cols, row))
                score_name = row_dict.get("score_name", "")
                buffer_json = row_dict.get("buffer_json", "[]")
                confirmed = float(row_dict.get("confirmed_score", 0.0))

                try:
                    buffer_list = json.loads(buffer_json)
                except Exception:
                    buffer_list = []

                if score_name == "vol":
                    self.vol_buffer = deque(buffer_list, maxlen=config.PERSISTENCE_READINGS)
                    self.confirmed_vol = confirmed
                elif score_name == "edge":
                    self.edge_buffer = deque(buffer_list, maxlen=config.PERSISTENCE_READINGS)
                    self.confirmed_edge = confirmed
                elif score_name == "trend":
                    self.trend_buffer = deque(buffer_list, maxlen=config.PERSISTENCE_READINGS)
                    self.confirmed_trend = confirmed
                elif score_name == "flow":
                    self.flow_buffer = deque(buffer_list, maxlen=config.PERSISTENCE_READINGS)
                    self.confirmed_flow = confirmed

            logger.info("Score buffers restored from SQLite")

        except sqlite3.OperationalError:
            logger.info("No score_buffers table — fresh start")
        except Exception as e:
            logger.warning(f"load_buffers_from_sqlite error: {e}")

    def save_buffers_to_sqlite(self) -> None:
        """Persist score buffers and confirmed scores to SQLite."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            now_str = datetime.now(self._IST).isoformat()

            buffers = {
                "vol":   (self.vol_buffer,   self.confirmed_vol),
                "edge":  (self.edge_buffer,  self.confirmed_edge),
                "trend": (self.trend_buffer, self.confirmed_trend),
                "flow":  (self.flow_buffer,  self.confirmed_flow),
            }

            for score_name, (buf, confirmed) in buffers.items():
                cursor.execute("""
                    INSERT OR REPLACE INTO score_buffers
                    (score_name, buffer_json, confirmed_score, updated_at)
                    VALUES (?, ?, ?, ?)
                """, (
                    score_name,
                    json.dumps(list(buf)),
                    confirmed,
                    now_str
                ))

            conn.commit()
            conn.close()
            logger.info("Score buffers saved to SQLite")

        except sqlite3.Error as e:
            logger.warning(f"save_buffers_to_sqlite error: {e}")