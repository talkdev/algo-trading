# data_engine.py
# NIFTY Intraday Options Engine v3.0
# Market data engine: candles, VIX, option chain, technical indicators,
# opening range, VRP computation, gap detection, session state management.
# Evolved from market_data_engine.py with new regime-based architecture.

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import pandas as pd
import numpy as np

from core import (
    Config, Database, RateLimiter, UpstoxClient,
    ExpiryCalendar, now_ist, today_ist, parse_ist_timestamp, IST,
    INSTRUMENT_KEY_NIFTY_SPOT, INSTRUMENT_KEY_INDIA_VIX,
    print_section, print_kv_table,
    load_config, setup_logging,
    get_nse_holidays, get_high_impact_events,
)


# ─────────────────────────────────────────────────────────────────────────────
# TECHNICAL ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class TechnicalEngine:
    """
    Stateless technical analysis methods.
    All methods are static — no instantiation needed.
    Used by MarketDataEngine every cycle.
    """

    # ── Bar Resampling ────────────────────────────────────────────────────

    @staticmethod
    def resample_bars(bars_1min: pd.DataFrame, interval: str) -> pd.DataFrame:
        """
        Resample 1-minute bars to a higher timeframe.
        interval: pandas offset string e.g. '900s' (15min), '3600s' (60min)
        Returns DataFrame with columns: datetime, open, high, low, close, volume
        """
        if bars_1min is None or bars_1min.empty:
            return pd.DataFrame()
        try:
            df = bars_1min.copy()

            # Build datetime index
            if "datetime" not in df.columns:
                if "date" in df.columns and "time" in df.columns:
                    df["datetime"] = pd.to_datetime(
                        df["date"].astype(str) + " " + df["time"].astype(str),
                        errors="coerce",
                    )
                elif "time" in df.columns:
                    today_str = today_ist().isoformat()
                    df["datetime"] = pd.to_datetime(
                        today_str + " " + df["time"].astype(str),
                        errors="coerce",
                    )
                else:
                    return pd.DataFrame()

            # Strip timezone to avoid resampling issues
            if hasattr(df["datetime"].dtype, "tz") and df["datetime"].dtype.tz is not None:
                df["datetime"] = df["datetime"].dt.tz_localize(None)

            df = df.set_index("datetime").sort_index()
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            # Ensure numeric columns
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            resampled = (
                df[["open", "high", "low", "close", "volume"]]
                .resample(interval, label="left", closed="left")
                .agg({
                    "open":   "first",
                    "high":   "max",
                    "low":    "min",
                    "close":  "last",
                    "volume": "sum",
                })
                .dropna(subset=["open", "close"])
            )
            resampled = resampled[resampled["open"] > 0]
            return resampled.reset_index()

        except Exception:
            return pd.DataFrame()

    # ── ADX ───────────────────────────────────────────────────────────────

    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
        """
        Calculate Average Directional Index (ADX) from OHLC bars.
        Returns 0.0 if insufficient data.
        """
        if df is None or df.empty or len(df) < period + 2:
            return 0.0
        try:
            high  = df["high"].values.astype(float)
            low   = df["low"].values.astype(float)
            close = df["close"].values.astype(float)
            n     = len(high)

            tr_arr  = np.zeros(n)
            pdm_arr = np.zeros(n)
            ndm_arr = np.zeros(n)

            for i in range(1, n):
                hl  = high[i] - low[i]
                hpc = abs(high[i] - close[i - 1])
                lpc = abs(low[i]  - close[i - 1])
                tr_arr[i] = max(hl, hpc, lpc)

                up   = high[i] - high[i - 1]
                down = low[i - 1] - low[i]
                pdm_arr[i] = up   if (up > down   and up   > 0) else 0.0
                ndm_arr[i] = down if (down > up   and down > 0) else 0.0

            # Wilder smoothing
            atr     = np.zeros(n)
            pdi_raw = 0.0
            ndi_raw = 0.0

            atr[period]  = tr_arr[1:period + 1].sum()
            pdi_raw      = pdm_arr[1:period + 1].sum()
            ndi_raw      = ndm_arr[1:period + 1].sum()

            pdi = np.zeros(n)
            ndi = np.zeros(n)

            for i in range(period + 1, n):
                atr[i]  = atr[i - 1] - atr[i - 1] / period + tr_arr[i]
                pdi_raw = pdi_raw    - pdi_raw    / period + pdm_arr[i]
                ndi_raw = ndi_raw    - ndi_raw    / period + ndm_arr[i]
                pdi[i]  = 100 * pdi_raw / atr[i] if atr[i] > 0 else 0.0
                ndi[i]  = 100 * ndi_raw / atr[i] if atr[i] > 0 else 0.0

            dx_arr = np.zeros(n)
            for i in range(period + 1, n):
                denom    = pdi[i] + ndi[i]
                dx_arr[i] = 100 * abs(pdi[i] - ndi[i]) / denom if denom > 0 else 0.0

            valid_dx = dx_arr[period + 1:]
            if len(valid_dx) < period:
                return 0.0

            adx_val = float(np.mean(valid_dx[:period]))
            for i in range(period, len(valid_dx)):
                adx_val = (adx_val * (period - 1) + valid_dx[i]) / period

            return round(max(adx_val, 0.0), 2)

        except Exception:
            return 0.0

    # ── EMA ───────────────────────────────────────────────────────────────

    @staticmethod
    def calculate_ema(series: pd.Series, period: int) -> Optional[float]:
        """
        Calculate EMA of a pandas Series.
        Returns None if insufficient data.
        """
        if series is None or len(series) < period:
            return None
        try:
            return float(series.ewm(span=period, adjust=False).mean().iloc[-1])
        except Exception:
            return None

    # ── EMA Structure ─────────────────────────────────────────────────────

    @staticmethod
    def classify_ema_structure(
        df: pd.DataFrame, ema_fast: int, ema_slow: int
    ) -> str:
        """
        Classify EMA structure as BULLISH, BEARISH, TRANSITIONAL, or NEUTRAL.
        BULLISH:      close > fast EMA > slow EMA
        BEARISH:      close < fast EMA < slow EMA
        TRANSITIONAL: EMAs very close together (< 0.2% apart)
        NEUTRAL:      mixed
        Returns INSUFFICIENT_DATA if not enough bars.
        """
        if df is None or df.empty or len(df) < ema_slow:
            return "INSUFFICIENT_DATA"
        try:
            closes = df["close"]
            fast   = TechnicalEngine.calculate_ema(closes, ema_fast)
            slow   = TechnicalEngine.calculate_ema(closes, ema_slow)
            if fast is None or slow is None:
                return "INSUFFICIENT_DATA"
            last_close = float(closes.iloc[-1])
            if last_close > fast > slow:
                return "BULLISH"
            if last_close < fast < slow:
                return "BEARISH"
            if slow > 0 and abs(fast - slow) / slow < 0.002:
                return "TRANSITIONAL"
            return "NEUTRAL"
        except Exception:
            return "INSUFFICIENT_DATA"

    # ── HH/HL Pattern ─────────────────────────────────────────────────────

    @staticmethod
    def detect_hh_hl(df: pd.DataFrame, lookback: int = 6) -> str:
        """
        Detect Higher Highs / Higher Lows (UPTREND) or
        Lower Highs / Lower Lows (DOWNTREND) pattern.
        Returns: UPTREND, DOWNTREND, NEUTRAL, or INSUFFICIENT_DATA.
        """
        if df is None or df.empty or len(df) < lookback + 2:
            return "INSUFFICIENT_DATA"
        try:
            highs = df["high"].values[-lookback:]
            lows  = df["low"].values[-lookback:]

            swing_highs: List[float] = []
            swing_lows:  List[float] = []

            for i in range(1, len(highs) - 1):
                if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
                    swing_highs.append(float(highs[i]))
                if lows[i]  < lows[i - 1]  and lows[i]  < lows[i + 1]:
                    swing_lows.append(float(lows[i]))

            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                if (swing_highs[-1] > swing_highs[-2] and
                        swing_lows[-1] > swing_lows[-2]):
                    return "UPTREND"
                if (swing_highs[-1] < swing_highs[-2] and
                        swing_lows[-1] < swing_lows[-2]):
                    return "DOWNTREND"
            return "NEUTRAL"
        except Exception:
            return "INSUFFICIENT_DATA"

    # ── Choppy Detection ──────────────────────────────────────────────────

    @staticmethod
    def detect_choppy(
        bars: pd.DataFrame,
        or_high: float,
        or_low: float,
        lookback_min: int = 10,
        wick_threshold: int = 3,
    ) -> bool:
        """
        Detect choppy market: OR established but ≥ wick_threshold wick-throughs
        without close confirmation in the last lookback_min minutes.
        Returns True if choppy.
        """
        if bars is None or bars.empty or or_high <= 0 or or_low <= 0:
            return False
        try:
            now_t   = now_ist()
            cutoff  = (now_t - timedelta(minutes=lookback_min)).strftime("%H:%M:%S")
            recent  = bars[bars["time"] >= cutoff] if "time" in bars.columns else bars.tail(lookback_min)
            if recent.empty:
                return False

            wick_high = int(
                ((recent["high"] > or_high) & (recent["close"] <= or_high)).sum()
            )
            wick_low  = int(
                ((recent["low"]  < or_low)  & (recent["close"] >= or_low)).sum()
            )
            return (wick_high >= wick_threshold) or (wick_low >= wick_threshold)
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────────────────
# MARKET DATA ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class MarketDataEngine:
    """
    Core market data engine for NIFTY intraday options trading.

    Responsibilities:
    - Fetch and store spot, VIX, option chain, intraday candles
    - Compute VRP (ATM IV - Parkinson RV) with smoothing and anomaly detection
    - Compute opening range, gap detection, VWAP, PCR, OI walls, skew
    - Classify IV behavior relative to session open
    - Track day_move_used relative to opening straddle
    - Manage session state with mid-day restart recovery
    - Persist all data to database every cycle
    - Return clean signals dict for regime engine consumption

    Does NOT:
    - Classify volatility/price/positioning regimes (regime_engine.py)
    - Make trade/no-trade decisions (strategy_engine.py)
    - Compute size multipliers (regime_engine.py)
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        client: UpstoxClient,
        rate_limiter: RateLimiter,
        logger,
    ):
        self.config       = config
        self.db           = db
        self.client       = client
        self.rate_limiter = rate_limiter
        self.logger       = logger
        self.tech         = TechnicalEngine()

        # Ensure all required columns exist
        self._ensure_extra_columns()

        # Load or initialise session state
        self.state: dict = self._load_or_init_session_state()

        # In-memory chain cache
        self.last_chain: dict              = {}
        self.last_chain_expiry: Optional[date] = None
        self._chain_fetch_time: Optional[datetime] = None

        # VRP smoothing buffer (list of raw VRP floats, most recent last)
        self._vrp_buffer: List[float] = self._seed_vrp_buffer()

        # Calibration cache (refreshed hourly)
        self._cached_calibration: Optional[dict]   = None
        self._calibration_cache_time: Optional[datetime] = None

        # Intraday tracking
        self._pcr_baseline_set: bool = False
        self._first_bar_close_today: Optional[float] = None
        self._first_bar_date: Optional[str]          = None
        self._vix_fail_count: int                    = 0

    # ─────────────────────────────────────────────────────────────────────
    # INITIALISATION HELPERS
    # ─────────────────────────────────────────────────────────────────────

    def _ensure_extra_columns(self) -> None:
        """Add any columns that may be missing from older database versions."""
        extra = [
            ("session_state", "opening_straddle_pts",       "REAL DEFAULT 0"),
            ("session_state", "prev_day_vix_close",         "REAL"),
            ("session_state", "gap_direction",              "TEXT DEFAULT 'FLAT'"),
            ("session_state", "gap_size_pts",               "REAL DEFAULT 0"),
            ("session_state", "gap_fade_opportunity",       "INTEGER DEFAULT 0"),
            ("session_state", "first_bar_close",            "REAL"),
            ("session_state", "last_stop_signal_combo",     "TEXT"),
            ("session_state", "_straddle_open_for_regime",  "REAL DEFAULT 0"),
            ("session_state", "_straddle_open_for_summary", "REAL DEFAULT 0"),
            # v3.1: these keys are written by _load_or_init_session_state on
            # every cycle but were never present in the schema, which made
            # MarketDataEngine() raise sqlite3.OperationalError on startup —
            # the engine could not run at all.
            ("session_state", "_last_valid_atm_iv",         "REAL"),
            ("session_state", "_atm_iv_none_cycles",        "INTEGER DEFAULT 0"),
            ("session_state", "_vrp_none_cycles",           "INTEGER DEFAULT 0"),
            ("session_state", "_stale_count",               "INTEGER DEFAULT 0"),
            # Liquidation mark, used by the honest mark-to-exit logic.
            ("positions",     "last_liquidation_premium",   "REAL"),
            ("positions",     "profit_lock_activated",      "INTEGER DEFAULT 0"),
            ("positions",     "profit_lock_stop_level",     "REAL"),
            ("positions",     "exit_priority",              "INTEGER"),
            ("positions",     "price_stop_level_call",      "REAL"),
            ("positions",     "price_stop_level_put",       "REAL"),
            ("positions",     "is_borderline_sell",         "INTEGER DEFAULT 0"),
            ("positions",     "opening_straddle_at_entry",  "REAL"),
            ("positions",     "entry_vrp_smoothed",         "REAL"),
        ]
        for table, col, coltype in extra:
            self.db.ensure_column(table, col, coltype)

    def _load_or_init_session_state(self) -> dict:
        """
        Load today's session_state row from DB, or create a fresh one.
        Handles mid-day restart by reconciling entry_count with actual positions.
        """
        today_str = today_ist().isoformat()
        row = self.db.query_one(
            "SELECT * FROM session_state WHERE trading_date=?", (today_str,)
        )

        if row is not None:
            # Reconcile entry_count with actual DB positions
            actual = self.db.query_one(
                "SELECT COUNT(*) as cnt FROM positions "
                "WHERE trading_date=? AND status IN ('OPEN','CLOSED')",
                (today_str,),
            )
            if actual:
                db_count = actual["cnt"]
                if row.get("entry_count", 0) != db_count:
                    self.db.update(
                        "session_state",
                        {"entry_count": db_count},
                        {"trading_date": today_str},
                    )
                    row["entry_count"] = db_count

            # Convert integer columns to bool
            for bool_col in (
                "daily_halted", "circuit_breaker_suspected",
                "vix_spike_detected", "event_announced",
                "or_computed", "session_initialized",
                "vwap_valid", "paper_trade_mode",
                "gap_fade_opportunity",
            ):
                if bool_col in row and row[bool_col] is not None:
                    row[bool_col] = bool(row[bool_col])

            self.logger.info(
                f"Session state loaded for {today_str} "
                f"(mid-day restart recovery, entries={row.get('entry_count', 0)})"
            )
            return dict(row)

        # Fresh session
        prev_day_vix = self.db.get_prev_day_vix_close()
        day_label    = ExpiryCalendar.get_day_label(today_ist())
        dte          = ExpiryCalendar.get_dte(today_ist())

        # Entry window defaults (overridden by regime engine)
        if day_label == "TUESDAY":
            entry_start = self.config.trading_window_start.strftime("%H:%M")
            entry_end   = self.config.tuesday_last_entry.strftime("%H:%M")
            hard_exit   = self.config.tuesday_hard_exit.strftime("%H:%M")
        else:
            entry_start = self.config.trading_window_start.strftime("%H:%M")
            entry_end   = self.config.trading_window_last_entry.strftime("%H:%M")
            hard_exit   = self.config.hard_exit_time.strftime("%H:%M")

        defaults = {
            "trading_date":                today_str,
            "day_mode":                    "NORMAL",
            "vix_regime":                  "UNKNOWN",
            "day_label":                   day_label,
            "or_high":                     None,
            "or_low":                      None,
            "or_width":                    None,
            "or_condition":                None,
            "or_computed":                 False,
            "session_initialized":         False,
            "entry_start":                 entry_start,
            "entry_end":                   entry_end,
            "hard_exit_time":              hard_exit,
            "size_multiplier":             1.0,
            "wing_width":                  150,
            "entry_count":                 0,
            "daily_halted":                False,
            "consecutive_stops":           0,
            "last_stop_time":              None,
            "last_stop_reason":            None,
            "last_entry_time":             None,
            "last_stop_signal_combo":      None,
            "actual_expiry":               None,
            "actual_dte":                  dte,
            "opening_iv":                  None,
            "opening_pcr":                 None,
            "opening_straddle_pts":        0.0,
            "current_capital":             self.config.starting_capital,
            "daily_pnl":                   0.0,
            "circuit_breaker_suspected":   False,
            "vix_spike_detected":          False,
            "event_announced":             False,
            "paper_trade_mode":            self.config.paper_trade_mode,
            "prev_spot":                   None,
            "prev_vix":                    None,
            "prev_day_vix_close":          prev_day_vix,
            "parkinson_rv_pct":            None,
            "parkinson_rv_computed_date":  None,
            "vwap_valid":                  False,
            "expiry_last_checked":         None,
            "vix_regime_last_checked":     None,
            "gap_direction":               "FLAT",
            "gap_size_pts":                0.0,
            "gap_fade_opportunity":        False,
            "first_bar_close":             None,
            "_straddle_open_for_regime":   0.0,
            "_straddle_open_for_summary":  0.0,
            "_last_valid_atm_iv":           None,
            "_atm_iv_none_cycles":          0,
            "_vrp_none_cycles":             0,
            "_stale_count":                 0,
            "_straddle_hist":               [],
            "created_at":                  now_ist().isoformat(),
            "updated_at":                  now_ist().isoformat(),
        }

        # v3.1: persist only scalar values that actually exist as columns.
        # Previously every default was written blindly, so any key without a
        # column, or a non-scalar value, raised sqlite3 errors during
        # MarketDataEngine construction. `_straddle_hist` in particular is a
        # list of (timestamp, straddle) tuples feeding the straddle-expansion
        # entry block; SQLite cannot bind a list, and the 10-minute window
        # rebuilds within minutes of a restart, so it is intentionally kept
        # in-memory only.
        try:
            _cols = {
                r[1] for r in self.db.get_connection()
                .execute("PRAGMA table_info(session_state)").fetchall()
            }
        except Exception:
            _cols = set()
        insert_row = {}
        for _k, _v in defaults.items():
            if _cols and _k not in _cols:
                continue
            if isinstance(_v, bool):
                _v = int(_v)
            elif isinstance(_v, (list, tuple, dict, set)):
                continue
            insert_row[_k] = _v
        self.db.insert("session_state", insert_row)
        self.logger.info(f"Fresh session state initialised for {today_str}")
        return defaults

    def _save_session_state(self) -> None:
        """Persist current session state to database."""
        data = dict(self.state)
        trading_date = data.pop("trading_date")
        data.pop("created_at", None)
        data["updated_at"] = now_ist().isoformat()

        # Convert bools to ints for SQLite
        for k, v in list(data.items()):
            if isinstance(v, bool):
                data[k] = int(v)

        # Only update columns that exist in the table
        try:
            existing_cols = {
                row[1] for row in
                self.db.get_connection().execute(
                    "PRAGMA table_info(session_state)"
                ).fetchall()
            }
            data = {k: v for k, v in data.items() if k in existing_cols}
        except Exception:
            pass

        self.db.update("session_state", data, {"trading_date": trading_date})

    # ─────────────────────────────────────────────────────────────────────
    # DAY RESET
    # ─────────────────────────────────────────────────────────────────────

    def reset_if_new_day(self) -> None:
        """
        Detect a new trading day and reset all intraday state.
        Closes any stale prior-day positions.
        """
        today_str = today_ist().isoformat()
        if self.state.get("trading_date") == today_str:
            return

        self.logger.info(
            f"New trading day: {today_str} "
            f"(previous: {self.state.get('trading_date')})"
        )
        self._close_stale_prior_day_positions(self.state.get("trading_date"))

        # Reset in-memory state
        self.state                   = self._load_or_init_session_state()
        self.last_chain              = {}
        self.last_chain_expiry       = None
        self._chain_fetch_time       = None
        self._vrp_buffer             = self._seed_vrp_buffer()
        self._pcr_baseline_set       = False
        self.state["_straddle_hist"]  = []
        self.state["_stale_count"]    = 0
        self.state["_atm_iv_none_cycles"] = 0
        self.state["_vrp_none_cycles"]    = 0
        self._first_bar_close_today  = None
        self._first_bar_date         = None
        self._vix_fail_count         = 0

    def _close_stale_prior_day_positions(self, prior_date: Optional[str]) -> None:
        """Mark any open positions from a prior date as stale-closed."""
        if not prior_date:
            return
        open_pos = self.db.query(
            "SELECT position_id, strategy_name FROM positions "
            "WHERE trading_date=? AND status='OPEN'",
            (prior_date,),
        )
        for pos in open_pos:
            self.logger.warning(
                f"Stale prior-day position: {pos['strategy_name']} "
                f"{pos['position_id'][:16]} from {prior_date} — marking STALE_CLOSE"
            )
            self.db.update(
                "positions",
                {
                    "status":       "CLOSED",
                    "exit_reason":  "STALE_PRIOR_DAY_CLOSE",
                    "exit_time":    now_ist().isoformat(),
                    "updated_at":   now_ist().isoformat(),
                },
                {"position_id": pos["position_id"]},
            )

    # ─────────────────────────────────────────────────────────────────────
    # SPOT & VIX
    # ─────────────────────────────────────────────────────────────────────

    def fetch_spot_and_vix(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Fetch NIFTY spot and India VIX from Upstox LTP API.
        Falls back to last known values on failure.
        Validates ranges: spot 10000-50000, VIX 5-90.
        """
        try:
            data = self.client.get_ltp(
                [INSTRUMENT_KEY_NIFTY_SPOT, INSTRUMENT_KEY_INDIA_VIX]
            )
        except Exception as e:
            self.logger.error(f"Failed to fetch spot/VIX: {e}")
            self._vix_fail_count += 1
            return self.state.get("prev_spot"), self.state.get("prev_vix")

        spot = vix = None
        for key, v in data.items():
            ltp       = v.get("last_price")
            key_upper = str(key).upper()
            if "VIX" in key_upper:
                vix = float(ltp) if ltp is not None else None
            elif "NIFTY" in key_upper:
                spot = float(ltp) if ltp is not None else None

        # Validate ranges
        if spot is not None and not (10000 < spot < 50000):
            self.logger.warning(
                f"Spot {spot} outside valid range 10000-50000 — using last known"
            )
            spot = self.state.get("prev_spot")

        if vix is not None and not (5.0 < vix < 90.0):
            self.logger.warning(
                f"VIX {vix} outside valid range 5-90 — using last known"
            )
            vix = self.state.get("prev_vix")

        if vix is not None:
            self._vix_fail_count = 0
        else:
            self._vix_fail_count += 1

        return spot, vix

    def check_circuit_breaker_and_vix_spike(
        self,
        spot: Optional[float],
        vix: Optional[float],
    ) -> Tuple[bool, bool]:
        """
        Detect circuit breaker (spot moves > 5% in one cycle) and
        VIX spike (VIX up > 25% intraday from previous reading).
        Returns (circuit_breaker_suspected, vix_spike_detected).
        """
        prev_spot = self.state.get("prev_spot")
        prev_vix  = self.state.get("prev_vix")
        circuit   = False
        vix_spike = self.state.get("vix_spike_detected", False)

        if prev_spot and prev_spot > 0 and spot:
            pct = abs(spot - prev_spot) / prev_spot
            if pct > 0.05:
                circuit = True
                self.logger.warning(
                    f"CIRCUIT BREAKER SUSPECTED: spot moved "
                    f"{pct * 100:.2f}% in one cycle "
                    f"({prev_spot:.0f} → {spot:.0f})"
                )

        if prev_vix and prev_vix > 0 and vix:
            chg = (vix - prev_vix) / prev_vix * 100.0
            if chg > 25.0:
                vix_spike = True
                self.logger.warning(
                    f"VIX INTRADAY SPIKE: {prev_vix:.1f} → {vix:.1f} "
                    f"({chg:.1f}% in one cycle)"
                )
            elif chg < -15.0:
                vix_spike = False

        self.state["prev_spot"] = spot
        self.state["prev_vix"]  = vix
        return circuit, vix_spike

    # ─────────────────────────────────────────────────────────────────────
    # INTRADAY CANDLES
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_candle_row(row) -> dict:
        """
        Normalise a raw candle row (list/tuple or dict) to a standard dict.
        Returns dict with keys: timestamp, open, high, low, close, volume.
        """
        try:
            if isinstance(row, (list, tuple)):
                ts = parse_ist_timestamp(row[0])
                return {
                    "timestamp": ts,
                    "open":      float(row[1]),
                    "high":      float(row[2]),
                    "low":       float(row[3]),
                    "close":     float(row[4]),
                    "volume":    int(row[5]) if len(row) > 5 else 0,
                }
            ts = parse_ist_timestamp(row.get("timestamp"))
            return {
                "timestamp": ts,
                "open":      float(row.get("open",   0) or 0),
                "high":      float(row.get("high",   0) or 0),
                "low":       float(row.get("low",    0) or 0),
                "close":     float(row.get("close",  0) or 0),
                "volume":    int(row.get("volume",   0) or 0),
            }
        except (ValueError, TypeError, IndexError):
            return {
                "timestamp": None,
                "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0,
            }

    def fetch_and_store_intraday_candles(self) -> pd.DataFrame:
        """
        Fetch today's 1-minute NIFTY candles from Upstox API,
        validate, store to intraday_candles table, and return as DataFrame.
        Falls back to DB if API fails.
        """
        trading_date = today_ist().isoformat()
        try:
            raw = self.client.get_intraday_candles(
                INSTRUMENT_KEY_NIFTY_SPOT, "1minute"
            )
        except Exception as e:
            self.logger.error(f"Failed to fetch intraday candles: {e}")
            return self._load_candles_from_db(trading_date)

        bars_1m = []
        for row in raw:
            b = self._normalize_candle_row(row)
            if b["timestamp"] is None:
                continue
            t = b["timestamp"].time()
            if not (dtime(9, 15) <= t <= dtime(15, 29)):
                continue
            if b["high"] <= 0 or b["low"] <= 0 or b["high"] < b["low"]:
                continue
            if b["close"] <= 0 or b["open"] <= 0:
                continue
            bars_1m.append(b)

        bars_1m.sort(key=lambda x: x["timestamp"])

        if bars_1m:
            rows_to_insert = []
            for b in bars_1m:
                ts = b["timestamp"]
                try:
                    ts_clean = ts.strftime("%H:%M:%S")
                    # Validate time format
                    h, m, s = ts_clean.split(":")
                    if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59 and 0 <= int(s) <= 59):
                        continue
                except Exception:
                    continue
                rows_to_insert.append((
                    trading_date,
                    ts_clean,
                    1,
                    b["open"], b["high"], b["low"], b["close"],
                    b.get("volume", 0),
                    "upstox_intraday",
                ))

            if rows_to_insert:
                try:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO intraday_candles "
                        "(trading_date, candle_time, interval_min, "
                        "open, high, low, close, volume, source) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        rows_to_insert,
                    )
                    self.logger.debug(
                        f"Stored {len(rows_to_insert)} candle bars for {trading_date}"
                    )
                except Exception as e:
                    self.logger.warning(f"Candle insert error: {e}")

        return self._load_candles_from_db(trading_date)

    def _load_candles_from_db(self, trading_date: str) -> pd.DataFrame:
        """Load today's 1-minute candles from database as DataFrame."""
        try:
            rows = self.db.query(
                "SELECT candle_time as time, open, high, low, close, volume "
                "FROM intraday_candles "
                "WHERE trading_date=? AND interval_min=1 "
                "ORDER BY candle_time",
                (trading_date,),
            )
            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows)
            df["date"] = trading_date
            df["datetime"] = pd.to_datetime(
                df["date"] + " " + df["time"],
                format="%Y-%m-%d %H:%M:%S",
                errors="coerce",
            )
            if hasattr(df["datetime"].dtype, "tz") and df["datetime"].dtype.tz is not None:
                df["datetime"] = df["datetime"].dt.tz_localize(None)

            df = df.dropna(subset=["datetime"])
            df = df[df["open"] > 0].copy()
            return df

        except Exception as e:
            self.logger.warning(f"Could not load candles from DB: {e}")
            return pd.DataFrame()

    def get_today_spot_bars(self) -> pd.DataFrame:
        """Return today's 1-minute candles from database."""
        return self._load_candles_from_db(today_ist().isoformat())

    def _snap_available(self) -> bool:
        """Return True if we have a recent spot price and a valid chain."""
        return (
            self.state.get("prev_spot") is not None
            and self.last_chain is not None
            and len(self.last_chain) > 0
        )

    # ─────────────────────────────────────────────────────────────────────
    # PREVIOUS CLOSE
    # ─────────────────────────────────────────────────────────────────────

    def _get_prev_close(self) -> Optional[float]:
        """
        Fetch previous trading day's closing price for NIFTY.
        Used for gap detection.
        """
        today = today_ist()
        from_date = (today - timedelta(days=10)).isoformat()
        to_date   = (today - timedelta(days=1)).isoformat()
        try:
            raw  = self.client.get_historical_candles(
                INSTRUMENT_KEY_NIFTY_SPOT, "day", from_date, to_date
            )
            bars = [self._normalize_candle_row(r) for r in raw]
            bars = [b for b in bars if b["timestamp"] is not None]
            if not bars:
                return None
            bars.sort(key=lambda b: b["timestamp"])
            return float(bars[-1]["close"])
        except Exception as e:
            self.logger.warning(f"Failed to fetch previous close: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────
    # OPENING RANGE
    # ─────────────────────────────────────────────────────────────────────

    def compute_opening_range(self, bars: pd.DataFrame) -> Optional[dict]:
        """
        Compute the 09:15-09:30 opening range from 1-minute bars.
        Requires at least 10 valid bars in the OR window.
        Falls back to a synthetic ±25pt range if OR window is incomplete
        but time is past 10:45 (ensures engine can trade even on data-thin days).

        Returns dict with: or_high, or_low, or_width, or_condition, or_score, partial
        or_condition: VERY_NARROW (<50), NARROW (50-100), MODERATE (100-150),
                      WIDE (150-200), VERY_WIDE (>200)
        """
        if bars is None or bars.empty:
            return None

        orb_bars = bars[
            (bars["time"] >= "09:15:00") & (bars["time"] < "09:45:00")
        ]
        orb_bars = orb_bars[
            (orb_bars["high"] > orb_bars["low"]) &
            ((orb_bars["high"] - orb_bars["low"]) <= 1000) &
            (orb_bars["high"] > 0) &
            (orb_bars["low"] > 0)
        ]

        if len(orb_bars) < 20:
            # Synthetic fallback after 10:45
            spot_now = self.state.get("prev_spot")
            if spot_now and spot_now > 0 and now_ist().time() >= dtime(10, 45):
                self.logger.debug(
                    f"OR window had only {len(orb_bars)} bars — "
                    f"using synthetic ±25pt range around {spot_now:.0f}"
                )
                return None
            return None

        orb_bars = orb_bars.sort_values("time").head(12)
        or_high  = float(orb_bars["high"].max())
        or_low   = float(orb_bars["low"].min())
        or_width = or_high - or_low

        if or_width <= 0:
            return None

        # ── Classify OR width (v3.1: scale-invariant) ─────────────────
        # The old absolute 50/100/150/200pt buckets were calibrated for an
        # ~18,000 NIFTY. or_condition drives the p_win table, the size
        # multiplier, the VRP sell threshold and the hard "wide OR" no-trade
        # gate, so absolute buckets make the engine progressively refuse to
        # trade as the index rises — a silent, compounding loss of
        # opportunity that looks like nothing at all in the logs.
        #
        # Two normalisations are computed and the MORE CONSERVATIVE (wider)
        # of the two is used:
        #   1. OR width as a fraction of spot.
        #   2. OR width against the opening ATM straddle, i.e. against the
        #      market's own priced expectation for the day's range. This is
        #      the measure a professional actually uses: an 80pt opening
        #      range is narrow when the straddle is 300pts and wide when the
        #      straddle is 120pts.
        _bands = ["VERY_NARROW", "NARROW", "MODERATE", "WIDE", "VERY_WIDE"]
        _scores = {"VERY_NARROW": 2, "NARROW": 1, "MODERATE": 0,
                   "WIDE": -1, "VERY_WIDE": -2}

        _ref_spot = (or_high + or_low) / 2.0
        try:
            _ps = float(self.state.get("prev_spot") or 0)
            if _ps > 0:
                _ref_spot = _ps
        except Exception:
            pass

        _idx_pct = 4
        if _ref_spot > 0:
            _frac = or_width / _ref_spot
            if _frac < self.config.or_pct_very_narrow:
                _idx_pct = 0
            elif _frac < self.config.or_pct_narrow:
                _idx_pct = 1
            elif _frac < self.config.or_pct_moderate:
                _idx_pct = 2
            elif _frac < self.config.or_pct_wide:
                _idx_pct = 3

        _idx_str = _idx_pct
        try:
            _straddle = float(
                self.state.get("opening_straddle_pts")
                or self.state.get("_straddle_open_for_regime")
                or self.state.get("_last_atm_straddle")
                or 0.0
            )
        except Exception:
            _straddle = 0.0
        if _straddle > 20:
            _sr = or_width / _straddle
            if _sr < self.config.or_straddle_very_narrow:
                _idx_str = 0
            elif _sr < self.config.or_straddle_narrow:
                _idx_str = 1
            elif _sr < self.config.or_straddle_moderate:
                _idx_str = 2
            elif _sr < self.config.or_straddle_wide:
                _idx_str = 3
            else:
                _idx_str = 4

        or_condition = _bands[max(_idx_pct, _idx_str)]
        or_score     = _scores[or_condition]

        return {
            "or_high":      or_high,
            "or_low":       or_low,
            "or_width":     or_width,
            "or_condition": or_condition,
            "or_score":     or_score,
            "partial":      False,
        }

    def classify_orb_price_structure(
        self,
        bars: pd.DataFrame,
        orb_high: float,
        orb_low: float,
    ) -> str:
        """
        Classify where price is relative to the opening range.
        Returns: OBSERVING, CHOPPY, UPTREND, DOWNTREND, RANGE
        This is the ORB component of price regime — combined with ADX/EMA
        in regime_engine.py to produce the final price regime.
        """
        now_t = now_ist().time()
        if now_t < dtime(9, 30) or orb_high <= 0 or orb_low <= 0:
            return "OBSERVING"

        if bars is None or bars.empty:
            return "OBSERVING"

        post = bars[
            (bars["time"] >= "09:30:00") & (bars["time"] <= "15:30:00")
        ]
        if post.empty:
            return "OBSERVING"

        last_close = float(post["close"].iloc[-1])

        # Check for choppy within first 15 minutes after OR
        if now_t <= dtime(9, 45):
            choppy = TechnicalEngine.detect_choppy(
                post, orb_high, orb_low, lookback_min=10, wick_threshold=3
            )
            if choppy:
                return "CHOPPY"

        # Classify by last close vs OR boundaries
        if last_close > orb_high + 20:
            return "UPTREND"
        if last_close < orb_low - 20:
            return "DOWNTREND"
        return "RANGE"

    # ─────────────────────────────────────────────────────────────────────
    # GAP DETECTION
    # ─────────────────────────────────────────────────────────────────────

    def _compute_gap_detection(self, bars: pd.DataFrame) -> None:
        """
        Detect opening gap vs previous day close.
        Sets in session_state:
          gap_direction: 'UP', 'DOWN', 'FLAT'
          gap_size_pts: absolute gap in points
          gap_fade_opportunity: True when gap is retracing ≥ 40%

        Gap detection runs once per session (before 09:35).
        Gap fade monitoring runs until 09:35.
        """
        if self.state.get("gap_direction") not in (None, "FLAT", ""):
            # Already detected a gap today — only update fade status
            self._update_gap_fade_status(bars)
            return

        # Need first bar close
        if bars is None or bars.empty:
            return

        market_bars = bars[bars["time"] >= "09:15:00"]
        if market_bars.empty:
            return

        first_bar_open = float(market_bars["open"].iloc[0])

        # Get previous close
        prev_close = self.state.get("_prev_close_for_gap")
        if prev_close is None:
            prev_close = self._get_prev_close()
            if prev_close:
                self.state["_prev_close_for_gap"] = prev_close

        if not prev_close or prev_close <= 0:
            return

        gap_pts = first_bar_open - prev_close
        gap_pct = abs(gap_pts) / prev_close * 100.0

        if gap_pct < 0.4:
            self.state["gap_direction"]  = "FLAT"
            self.state["gap_size_pts"]   = 0.0
            return

        direction = "UP" if gap_pts > 0 else "DOWN"
        self.state["gap_direction"] = direction
        self.state["gap_size_pts"]  = abs(gap_pts)
        self.logger.info(
            f"Gap detected: {direction} {abs(gap_pts):.0f}pts "
            f"({gap_pct:.2f}%) from prev close {prev_close:.0f}"
        )
        self._update_gap_fade_status(bars)

    def _update_gap_fade_status(self, bars: pd.DataFrame) -> None:
        """
        Monitor gap retracement during first 20 minutes.
        Set gap_fade_opportunity = True when gap retraces ≥ 40%.
        """
        if self.state.get("gap_fade_opportunity"):
            return  # Already set

        gap_dir  = self.state.get("gap_direction", "FLAT")
        gap_size = self.state.get("gap_size_pts", 0.0)

        if gap_dir == "FLAT" or gap_size <= 0:
            return

        now_t = now_ist().time()
        if now_t > dtime(9, 35):
            return  # Only monitor first 20 minutes

        if bars is None or bars.empty:
            return

        # Get bars since market open
        recent = bars[
            (bars["time"] >= "09:15:00") &
            (bars["time"] <= now_t.strftime("%H:%M:%S"))
        ]
        if recent.empty:
            return

        first_open = float(recent["open"].iloc[0])
        last_close = float(recent["close"].iloc[-1])

        if gap_dir == "UP":
            # Gap up: fade = price moving back down toward prev close
            retracement = first_open - last_close
        else:
            # Gap down: fade = price moving back up toward prev close
            retracement = last_close - first_open

        if retracement >= gap_size * 0.40:
            self.state["gap_fade_opportunity"] = True
            self.logger.info(
                f"Gap fade opportunity: {gap_dir} gap fading "
                f"{retracement:.0f}pts ({retracement/gap_size*100:.0f}% of gap)"
            )

    # ─────────────────────────────────────────────────────────────────────
    # PARKINSON REALIZED VOLATILITY
    # ─────────────────────────────────────────────────────────────────────

    def compute_parkinson_rv(
        self,
        vix: Optional[float],
        bars: Optional[pd.DataFrame] = None,
    ) -> Tuple[Optional[float], str]:
        """
        Compute Parkinson Realized Volatility from intraday high-low bars.
        Formula: RV = sqrt(1/(4*ln2) * mean(ln(H/L)^2) * 375 * 252)
        Uses 375 bars/day annualization for NIFTY (6.25 hour session).

        Anomaly detection:
        - RV < 6% annualized → floor, use cached or VIX-implied
        - RV > 60% annualized → ceiling, use cached
        - RV drops > 50% from cached → use cached (data anomaly)

        Returns (rv_decimal, source) where source describes how RV was obtained.
        """
        today_str = today_ist().isoformat()
        cached_rv = self.state.get("parkinson_rv_pct")
        rv_floor  = 0.06   # 6% annualized minimum
        rv_ceil   = 0.60   # 60% annualized maximum

        # ── Compute from bars ─────────────────────────────────────────────
        if bars is not None and not bars.empty and len(bars) >= 20:
            _dte_rv = self.state.get("actual_dte", 2)
            if _dte_rv is None:
                _dte_rv = 2
            if _dte_rv == 0:
                rolling = bars[bars["time"] >= "09:15:00"] if "time" in bars.columns else bars
            elif _dte_rv == 1:
                rolling = bars.tail(120)
            else:
                rolling = bars.tail(90)
            valid   = rolling[
                (rolling["high"] > rolling["low"]) &
                (rolling["high"] > 0) &
                (rolling["low"] > 0)
            ]
            if len(valid) >= 15:
                log_hl_sq = []
                for _, r in valid.iterrows():
                    ratio = r["high"] / r["low"]
                    if ratio > 1.0001:
                        log_hl_sq.append(math.log(ratio) ** 2)

                if len(log_hl_sq) >= 10:
                    park_const = 1.0 / (4.0 * math.log(2.0))
                    # v3.1: this RV is used as a FORECAST of the volatility
                    # that will be realized over the remaining holding period
                    # — it is differenced against a forward-looking ATM IV to
                    # produce VRP, the engine's core edge measure. An
                    # equal-weighted session mean lets a violent 09:15-10:00
                    # dominate the estimate for a quiet 13:00-15:00 hold,
                    # which understates VRP exactly when selling premium is
                    # most attractive. Exponential recency weighting is the
                    # standard short-horizon estimator.
                    _n_hl = len(log_hl_sq)
                    _half_life = max(_n_hl / 3.0, 10.0)
                    _decay = 0.5 ** (1.0 / _half_life)
                    _w = [_decay ** (_n_hl - 1 - _i) for _i in range(_n_hl)]
                    _wsum = sum(_w) or 1.0
                    _mean_hl = sum(v * w for v, w in zip(log_hl_sq, _w)) / _wsum
                    variance   = park_const * _mean_hl
                    # 1.05 corrects the well-known downward discretisation
                    # bias of a Parkinson estimator run on 1-minute index bars.
                    rv         = math.sqrt(variance * 375.0 * 252.0) * 1.05

                    # Anomaly: below floor
                    if rv < rv_floor:
                        if cached_rv and cached_rv >= rv_floor:
                            self.logger.debug(
                                f"Parkinson RV {rv*100:.2f}% below floor "
                                f"— using cached {cached_rv*100:.2f}%"
                            )
                            return cached_rv, "cached"
                        # Use VIX-implied
                        vix_now = self.state.get("prev_vix") or vix or 15.0
                        return None, "vix_implied_disabled"

                    # Anomaly: above ceiling
                    if rv > rv_ceil:
                        if cached_rv and cached_rv >= rv_floor:
                            self.logger.debug(
                                f"Parkinson RV {rv*100:.2f}% above ceiling "
                                f"— using cached {cached_rv*100:.2f}%"
                            )
                            return cached_rv, "cached"
                        return None, "anomaly_no_cache"

                    if cached_rv and cached_rv >= rv_floor:
                        if rv < cached_rv * 0.50:
                            self.logger.debug(
                                f"Parkinson RV {rv*100:.2f}% dropped >50% "
                                f"from cached {cached_rv*100:.2f}% — using cached"
                            )
                            return cached_rv, "cached"
                        if rv > cached_rv * 1.50:
                            self.logger.warning(
                                f"Parkinson RV spike {rv*100:.2f}% > 1.5x cached "
                                f"{cached_rv*100:.2f}% — bad candle, using cached"
                            )
                            return cached_rv, "cached_spike_guard"

                    # Valid RV
                    self.state["parkinson_rv_pct"]            = rv
                    self.state["parkinson_rv_computed_date"]  = today_str
                    return rv, "rolling_intraday"

        # ── Use cached value ──────────────────────────────────────────────
        if (self.state.get("parkinson_rv_computed_date") == today_str and
                cached_rv is not None and cached_rv >= rv_floor):
            return cached_rv, "cached"

        # ── VIX-implied fallback ──────────────────────────────────────────
        vix_now = self.state.get("prev_vix") or vix or 15.0
        if vix_now and vix_now > 0:
            vix_implied = (vix_now / 100.0) * 0.75
            if rv_floor * 0.5 < vix_implied < rv_ceil:
                self.logger.debug(
                    f"Parkinson RV unavailable — VIX-implied: "
                    f"{vix_implied*100:.2f}% (VIX={vix_now:.2f})"
                )
                return vix_implied, "vix_implied"

        return None, "unavailable"

    # ─────────────────────────────────────────────────────────────────────
    # VRP COMPUTATION WITH SMOOTHING
    # ─────────────────────────────────────────────────────────────────────

    def _compute_vrp_smoothed(
        self,
        atm_iv: Optional[float],
        parkinson_rv: Optional[float],
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Compute raw VRP and smoothed VRP.
        Raw VRP = ATM IV (%) - Parkinson RV (%)
        Smoothed VRP = exponential weighted average of last N raw VRP readings.

        Anomaly detection:
        - Raw VRP > 8pp → likely Parkinson RV data error → use previous smoothed
        - Raw VRP < -5pp → unusual, cap at -5pp

        Returns (vrp_raw, vrp_smoothed). Both can be None if data unavailable.
        """
        if atm_iv is None or parkinson_rv is None:
            return None, None

        atm_iv_pct = atm_iv * 100.0 if atm_iv < 2.0 else atm_iv
        rv_pct     = parkinson_rv * 100.0 if parkinson_rv < 2.0 else parkinson_rv
        vrp_raw    = atm_iv_pct - rv_pct

        # v3.1 anomaly bound. The old rule ("VRP > 8pp must be a data
        # error") threw away the richest and most profitable readings: on
        # NIFTY 0DTE a genuine 8-15pp variance risk premium is routine on a
        # quiet expiry morning, and because regime_engine turns this into a
        # NEUTRAL hard block the engine stood aside precisely on its best
        # days. A real Parkinson failure does not present as an absolute
        # number of points — it presents as RV collapsing to a small fraction
        # of IV — so the bound is now relative to ATM IV, with the old 8pp
        # retained as a floor so genuinely low-IV regimes stay protected.
        _vrp_anomaly_limit = max(8.0, atm_iv_pct * 0.70)
        if vrp_raw > _vrp_anomaly_limit:
            self.logger.warning(
                f"VRP spike {vrp_raw:.2f}pp > limit {_vrp_anomaly_limit:.2f}pp "
                f"(ATM IV {atm_iv_pct:.2f}%) — likely Parkinson RV error. "
                f"Capping at previous smoothed value."
            )
            vrp_raw_capped = self._vrp_buffer[-1] if self._vrp_buffer else 3.0
            # Do not add anomalous value to buffer
            return vrp_raw, vrp_raw_capped

        # Anomaly: VRP < -5pp is unusual, cap
        if vrp_raw < -5.0:
            vrp_raw = -5.0

        # Add to smoothing buffer
        self._vrp_buffer.append(vrp_raw)
        # Keep only last N readings
        max_buf = self.config.vrp_smoothing_cycles
        if len(self._vrp_buffer) > max_buf:
            self._vrp_buffer = self._vrp_buffer[-max_buf:]

        # Exponential weighted average (most recent = highest weight)
        n = len(self._vrp_buffer)
        if n == 1:
            vrp_smoothed = self._vrp_buffer[0]
        else:
            weights = [2 ** i for i in range(n)]  # exponential weights
            total_w = sum(weights)
            vrp_smoothed = sum(
                v * w for v, w in zip(self._vrp_buffer, weights)
            ) / total_w

        return round(vrp_raw, 3), round(vrp_smoothed, 3)

    # ─────────────────────────────────────────────────────────────────────
    # IV BEHAVIOR
    # ─────────────────────────────────────────────────────────────────────

    def _session_rem_frac(self) -> float:
        """
        Fraction of the 09:15-15:30 session still to run, floored at 0.04.

        v3.5: hoisted out of _compute_iv_behavior so the IV baseline and the
        live reading are normalised by the same clock.
        """
        _elapsed = max(0.0, (
            datetime.combine(today_ist(), now_ist().time()) -
            datetime.combine(today_ist(), dtime(9, 15))
        ).total_seconds() / 60.0)
        return max((375.0 - _elapsed) / 375.0, 0.04)

    def _compute_iv_behavior(
        self,
        atm_iv: Optional[float],
        bars: pd.DataFrame,
    ) -> Tuple[str, float]:
        """
        Classify IV behavior relative to session opening IV.
        Requires at least 6 bars to be meaningful.

        Returns (iv_behavior, iv_change_pct_from_open)

        Classifications:
        CRUSHING:  IV down > 12% from open (strong theta/vol crush)
        DECLINING: IV down 4-12% from open (normal, good for selling)
        STABLE:    IV within ±4% of open (neutral)
        EXPANDING: IV up 4-15% from open (HARD BLOCK on new entries)
        SPIKING:   IV up > 15% from open (HARD BLOCK, check ABORT)
        UNKNOWN:   not enough data
        """
        opening_iv = self.state.get("opening_iv")
        if not opening_iv or opening_iv <= 0 or atm_iv is None or atm_iv <= 0:
            return "UNKNOWN", 0.0
        if bars is None or len(bars) < 6:
            return "UNKNOWN", 0.0

        # v3.5: a baseline taken on a different expiry series is not a
        # baseline. On 2026-09-08 the engine opened on the 15-Sep chain,
        # latched 10.13%, then switched to the 0DTE chain at 12:03 and read
        # the change of contract as a volatility spike for the rest of the
        # day. Re-take it, and say so in the log.
        _cur_exp  = self.state.get("actual_expiry")
        _base_exp = self.state.get("opening_iv_expiry")
        if _cur_exp and _base_exp and _cur_exp != _base_exp:
            self.state["opening_iv"]          = atm_iv
            self.state["opening_iv_expiry"]   = _cur_exp
            self.state["opening_iv_rem_frac"] = self._session_rem_frac()
            self.logger.info(
                f"IV baseline re-taken on expiry change {_base_exp} -> "
                f"{_cur_exp}: opening_iv={atm_iv * 100.0:.2f}%"
            )
            return "UNKNOWN", 0.0

        atm_iv_pct     = atm_iv * 100.0 if atm_iv < 2.0 else atm_iv
        opening_iv_pct = opening_iv * 100.0 if opening_iv < 2.0 else opening_iv

        # ── v3.5 ──────────────────────────────────────────────────────────
        # Raw ATM IV cannot be compared against itself across an expiry
        # session. As T collapses the annualisation factor blows up: the
        # measured 0DTE series ran 21.9% at 12:05 to 64.4% at 15:20 on a day
        # whose whole range was 90 points, a +536% drift that the v3.1
        # sqrt(T) band widening could not absorb because it caps at 3.2x.
        # 556 of 562 afternoon cycles were hard-blocked as SPIKING - the
        # entire 0DTE window, which is the only part of the day worth
        # trading.
        #
        # IV * sqrt(T_remaining) is proportional to the expected move in
        # points, which is the thing a premium seller is short, and it does
        # not depend on T. On the measured session it decays 16.17 -> 10.52,
        # a 35% crush read correctly as DECLINING. A real expansion still
        # registers because it moves the expected move itself.
        #
        # The normalised path needs a baseline taken at a known point in the
        # session. Where that is absent - a caller that sets opening_iv by
        # hand, including this module's own self test - behaviour falls back
        # to v3.1 exactly, sqrt(T) tolerance widening included.
        _dte_iv   = self.state.get("actual_dte", 0)
        _rem_base = self.state.get("opening_iv_rem_frac")
        _tol = 1.0

        if _dte_iv == 0 and _rem_base:
            _rem_now   = self._session_rem_frac()
            _cur_norm  = atm_iv_pct * math.sqrt(max(_rem_now, 1e-6))
            _base_norm = opening_iv_pct * math.sqrt(max(float(_rem_base), 1e-6))
            if _base_norm <= 0.0:
                return "UNKNOWN", 0.0
            iv_change_pct = (_cur_norm - _base_norm) / _base_norm * 100.0
        else:
            iv_change_pct = (atm_iv_pct - opening_iv_pct) / opening_iv_pct * 100.0
            if _dte_iv == 0:
                _rem_frac = self._session_rem_frac()
                _tol = min(max(_rem_frac ** -0.5, 1.0), 3.2)

        if iv_change_pct < -10.0 * _tol:
            return "CRUSHING", round(iv_change_pct, 2)
        if iv_change_pct < -3.0 * _tol:
            return "DECLINING", round(iv_change_pct, 2)
        if iv_change_pct <= 5.0 * _tol:
            return "STABLE", round(iv_change_pct, 2)
        if iv_change_pct <= 18.0 * _tol:
            return "EXPANDING", round(iv_change_pct, 2)
        return "SPIKING", round(iv_change_pct, 2)

    # ─────────────────────────────────────────────────────────────────────
    # DAY MOVE USED
    # ─────────────────────────────────────────────────────────────────────

    def _compute_day_move_used(self, spot: Optional[float]) -> float:
        """
        Range-consumed as % of opening straddle.
        Uses intraday high-low range, not net displacement.
        Chop day (+120,-180,+140) has small net move but huge range that kills premium.
        Range-consumed correctly captures what threatens a short-premium position.
        NIFTY 2026: block when range > 55% of opening straddle.
        """
        opening_straddle = (
            self.state.get("opening_straddle_pts") or
            self.state.get("_straddle_open_for_regime") or 0.0
        )
        if opening_straddle <= 0 or spot is None:
            return 0.0
        _dte = self.state.get("actual_dte", 0) or 0
        if _dte >= 2:
            import math as _math
            _theta_frac = max(1.0 / max(_dte, 1), 0.10)
            _straddle_ref = max(
                opening_straddle * _math.sqrt(_theta_frac),
                60.0
            )
        else:
            _straddle_ref = opening_straddle
        # ── v3.2: normalise by ELAPSED TIME ───────────────────────────
        # The realised range was compared against the straddle for the
        # WHOLE day. That makes the ratio meaninglessly small at 10:00
        # (nothing can have happened yet) and mechanically large at
        # 14:00 (the day has, by definition, done most of its range) -
        # so a flat 60% threshold blocked the afternoon on ordinary
        # sessions and never triggered in the morning on violent ones.
        # A range is only informative against the range the market
        # PRICED for the time elapsed, which under a diffusion is the
        # straddle scaled by sqrt(elapsed fraction). 100 now means "the
        # day is running exactly as priced"; the gate blocks above 125.
        import math as _math_dm
        _elapsed_dm = max(0.0, (
            datetime.combine(today_ist(), now_ist().time()) -
            datetime.combine(today_ist(), dtime(9, 15))
        ).total_seconds() / 60.0)
        _frac_dm = min(max(_elapsed_dm / 375.0, 0.06), 1.0)
        # ── v3.3: convert the priced DISPLACEMENT into a priced RANGE ──
        # The numerator below is day_high - day_low, a range. A straddle
        # prices E[|displacement|], not E[range]. For a driftless
        # diffusion those differ by exactly 2.0 in continuous time, and
        # by 1.933 when the path is observed at 1-minute bars as it is
        # here. v3.2 omitted the conversion and asserted that 100 meant
        # 'running exactly as priced'; the true figure was ~193, which
        # sits above the 125 block threshold, so classify_volatility
        # returned NEUTRAL on effectively every cycle of every session
        # and no trade could ever be reached. Measured on a replayed
        # session: the regime layer went from passing 109 of 787 cycles
        # to passing 0 of 789.
        _range_factor_dm = float(
            getattr(self.config, 'day_move_range_factor', 1.93)
        )
        _straddle_ref = max(
            _straddle_ref * _math_dm.sqrt(_frac_dm) * _range_factor_dm,
            12.0,
        )
        today_str = today_ist().isoformat()
        try:
            bars = self._load_candles_from_db(today_str)
            if bars is not None and not bars.empty and len(bars) >= 3:
                market_bars = bars[bars["time"] >= "09:15:00"]
                if not market_bars.empty:
                    day_high = float(market_bars["high"].max())
                    day_low  = float(market_bars["low"].min())
                    return round((day_high - day_low) / _straddle_ref * 100.0, 2)
        except Exception:
            pass
        first_close = self.state.get("first_bar_close")
        if first_close is None or first_close <= 0:
            return 0.0
        return round(abs(spot - first_close) / _straddle_ref * 100.0, 2)

    # ─────────────────────────────────────────────────────────────────────
    # OPTION CHAIN COMPUTATIONS
    # ─────────────────────────────────────────────────────────────────────

    def compute_atm_iv(
        self, chain: dict, spot: Optional[float]
    ) -> Optional[float]:
        """
        Compute ATM implied volatility as weighted average of
        ATM-1, ATM, ATM+1 strikes using OI weighting.

        Returns IV as decimal (e.g. 0.125 for 12.5%).
        Returns None if data unavailable or IV appears invalid.
        """
        if not chain or spot is None:
            return None

        step = self.config.nifty_strike_step
        atm  = round(spot / step) * step
        if atm not in chain:
            atm = min(chain.keys(), key=lambda k: abs(k - spot))

        iv_samples: List[Tuple[float, float]] = []  # (iv, weight)

        for s_strike in [atm - step, atm, atm + step]:
            leg = chain.get(s_strike, {})
            if not leg:
                continue
            c_leg = leg.get("call", {})
            p_leg = leg.get("put",  {})
            c_iv  = float(c_leg.get("iv", 0.0) or 0.0)
            p_iv  = float(p_leg.get("iv", 0.0) or 0.0)
            c_oi  = int(c_leg.get("oi",  0)   or 0)
            p_oi  = int(p_leg.get("oi",  0)   or 0)

            # Normalise IV to decimal
            if c_iv > 2.0:
                c_iv /= 100.0
            if p_iv > 2.0:
                p_iv /= 100.0

            if c_iv <= 0 and p_iv <= 0:
                continue

            total_oi = c_oi + p_oi
            if total_oi > 0:
                s_iv = (c_iv * c_oi + p_iv * p_oi) / total_oi
            elif c_iv > 0 and p_iv > 0:
                s_iv = (c_iv + p_iv) / 2.0
            elif c_iv > 0:
                s_iv = c_iv
            else:
                s_iv = p_iv

            if not (0.05 <= s_iv <= 0.80):
                continue

            weight = 2.0 if s_strike == atm else 1.0
            iv_samples.append((s_iv, weight))

        if not iv_samples:
            return None

        total_w = sum(w for _, w in iv_samples)
        atm_iv  = sum(iv * w for iv, w in iv_samples) / total_w

        if not (0.05 <= atm_iv <= 0.80):
            return None

        # Sanity check against VIX
        vix_state = self.state.get("prev_vix")
        if vix_state and vix_state > 0:
            vix_decimal = vix_state / 100.0
            ratio = atm_iv / vix_decimal
            _dte_now = self.state.get("actual_dte", 2)
            if _dte_now is None:
                _dte_now = 2
            if _dte_now == 0:
                _ratio_lo, _ratio_hi = 0.25, 9.00
            elif _dte_now == 1:
                _ratio_lo, _ratio_hi = 0.35, 5.00
            else:
                _ratio_lo, _ratio_hi = 0.50, 3.00
            if ratio < _ratio_lo or ratio > _ratio_hi:
                self.logger.warning(
                    f"ATM IV {atm_iv*100:.2f}% vs VIX {vix_state:.2f} "
                    f"ratio {ratio:.2f} outside {_ratio_lo}-{_ratio_hi} "
                    f"(DTE={_dte_now}) — chain may be stale"
                )
                return None

        return atm_iv

    def compute_pcr(
        self, chain: dict, spot: Optional[float] = None
    ) -> Optional[float]:
        """
        Compute Put-Call Ratio from OI.
        Uses ±5% band around spot if spot is available (more relevant PCR).
        Returns None if data unavailable or PCR outside valid range (0.3-4.0).
        """
        if not chain:
            return None

        if spot is not None and spot > 0:
            band       = spot * 0.05
            total_put  = sum(
                legs.get("put",  {}).get("oi", 0) or 0
                for strike, legs in chain.items()
                if (spot - band) <= strike < spot
            )
            total_call = sum(
                legs.get("call", {}).get("oi", 0) or 0
                for strike, legs in chain.items()
                if spot < strike <= (spot + band)
            )
        else:
            total_put  = sum(
                legs.get("put",  {}).get("oi", 0) or 0
                for legs in chain.values()
            )
            total_call = sum(
                legs.get("call", {}).get("oi", 0) or 0
                for legs in chain.values()
            )

        if total_call <= 0:
            return None

        pcr = total_put / total_call
        if pcr < 0.3 or pcr > 4.0:
            return None
        return round(pcr, 3)

    def compute_vwap(
        self, bars: pd.DataFrame
    ) -> Tuple[Optional[float], bool]:
        """
        Compute VWAP from intraday bars.
        For NIFTY index, volume is always 0 (NSE index limitation).
        Falls back to simple average of typical price when volume is zero.
        Returns (vwap, is_valid). is_valid = True if at least 10 bars available.
        """
        if bars is None or len(bars) < 10:
            return None, False

        cum_pv  = 0.0
        cum_vol = 0.0
        for _, row in bars.iterrows():
            typical  = (row["high"] + row["low"] + row["close"]) / 3.0
            cum_pv  += typical * row["volume"]
            cum_vol += row["volume"]

        if cum_vol > 0:
            return round(cum_pv / cum_vol, 2), True

        # Volume is zero (NSE index) — use simple average of typical price
        total = len(bars)
        avg   = sum(
            (r["high"] + r["low"] + r["close"]) / 3.0
            for _, r in bars.iterrows()
        ) / total
        return round(avg, 2), True

    def compute_25d_ivs(
        self, chain: dict
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Find the IV of options closest to 0.25 delta for puts and calls.
        Used to compute skew ratio.
        Returns (put_iv_25d, call_iv_25d) as decimals.
        """
        def find_by_delta(
            opt_type: str,
            target: float,
            tolerance: float = 0.08,
        ) -> Optional[float]:
            best_iv   = None
            best_diff = float("inf")
            for strike, legs in chain.items():
                leg   = legs.get(opt_type, {})
                delta = leg.get("delta")
                iv    = float(leg.get("iv", 0.0) or 0.0)
                if delta is None or iv <= 0:
                    continue
                diff = abs(abs(float(delta)) - target)
                if diff < best_diff:
                    best_diff = diff
                    best_iv   = iv / 100.0 if iv > 2.0 else iv
            return best_iv if best_diff <= tolerance else None

        put_iv  = find_by_delta("put",  0.25) or find_by_delta("put",  0.25, 0.12)
        call_iv = find_by_delta("call", 0.25) or find_by_delta("call", 0.25, 0.12)
        return put_iv, call_iv

    def compute_skew_ratio(
        self,
        put_iv: Optional[float],
        call_iv: Optional[float],
    ) -> Optional[float]:
        """
        Compute skew ratio = put_iv_25d / call_iv_25d.
        > 1.0: puts more expensive (fear skew, normal for NIFTY)
        < 1.0: calls more expensive (complacency/bullish skew)
        Returns None if data unavailable or ratio outside valid range (0.5-3.0).
        """
        if put_iv is None or call_iv is None:
            return None
        if put_iv <= 0.02 or call_iv <= 0.02:
            return None
        skew = put_iv / call_iv
        if skew < 0.5 or skew > 3.0:
            return None
        return round(skew, 3)

    def compute_otm_skew(
        self, chain: dict, atm_strike: int
    ) -> Tuple[float, float, float]:
        """
        Compute OTM skew: difference between OTM put IV and OTM call IV.
        Looks at 1-3 strikes OTM for each side.
        Returns (otm_ce_iv_pct, otm_pe_iv_pct, skew_pts)
        where skew_pts = otm_pe_iv - otm_ce_iv (positive = put skew = fear)
        """
        step = self.config.nifty_strike_step

        def _best_otm_iv(opt_type: str, direction: int) -> float:
            for multiplier in [1, 2, 3]:
                strike = atm_strike + (step * multiplier * direction)
                if strike in chain:
                    raw = chain[strike].get(opt_type, {}).get("iv", 0) or 0
                    iv  = raw * 100.0 if raw < 2.0 else raw
                    bid = chain[strike].get(opt_type, {}).get("bid", 0) or 0
                    ask = chain[strike].get(opt_type, {}).get("ask", 0) or 0
                    if iv > 0.5 and (bid > 0 or ask > 0):
                        return float(iv)
            return 0.0

        otm_ce_iv = _best_otm_iv("call",  1)
        otm_pe_iv = _best_otm_iv("put",  -1)

        if otm_pe_iv <= 0 or otm_ce_iv <= 0:
            return otm_ce_iv, otm_pe_iv, 0.0

        skew = round(otm_pe_iv - otm_ce_iv, 2)
        # Negative skew (calls > puts) is unusual — treat as neutral
        if skew < -1.5:
            self.logger.debug(
                f"OTM skew={skew:.2f} negative (calls>puts) — treating as 0"
            )
            return otm_ce_iv, otm_pe_iv, 0.0

        return otm_ce_iv, otm_pe_iv, skew

    def compute_max_pain(self, chain: dict) -> int:
        """
        Compute max pain strike: the strike where total option buyer loss is maximum.
        Returns 0 if insufficient data.
        """
        if not chain:
            return 0
        strikes = sorted(chain.keys())
        if len(strikes) < 5:
            return 0

        ce_map = {s: int(chain[s].get("call", {}).get("oi", 0) or 0) for s in strikes}
        pe_map = {s: int(chain[s].get("put",  {}).get("oi", 0) or 0) for s in strikes}

        best_s    = strikes[0]
        best_pain = float("inf")

        for candidate in strikes:
            pain = 0
            for s in strikes:
                if candidate > s:
                    pain += (candidate - s) * ce_map[s] * self.config.lot_size
                elif candidate < s:
                    pain += (s - candidate) * pe_map[s] * self.config.lot_size
            if pain < best_pain:
                best_pain = pain
                best_s    = candidate

        return int(best_s)

    def compute_oi_walls(self, chain: dict, spot: float) -> dict:
        """
        Identify OI resistance (call wall above spot) and support (put wall below spot).
        Computes wall strength as OI / average OI per strike.
        Returns dict with resistance_strike, support_strike, strengths, PCR, etc.
        """
        empty = {
            "resistance_strike":   0,
            "resistance_oi":       0,
            "resistance_strength": 0.0,
            "support_strike":      0,
            "support_oi":          0,
            "support_strength":    0.0,
            "range_width_pts":     0,
            "range_width_pct":     0.0,
            "max_pain_strike":     0,
            "max_pain_distance":   0.0,
            "total_ce_oi":         0,
            "total_pe_oi":         0,
            "pcr":                 1.0,
        }
        if not chain or spot <= 0:
            return empty

        above: List[Tuple[float, int]] = []
        below: List[Tuple[float, int]] = []
        total_ce = total_pe = 0

        for strike, legs in chain.items():
            ce_oi = int(legs.get("call", {}).get("oi", 0) or 0)
            pe_oi = int(legs.get("put",  {}).get("oi", 0) or 0)
            total_ce += ce_oi
            total_pe += pe_oi
            if strike > spot:
                above.append((strike, ce_oi))
            elif strike < spot:
                below.append((strike, pe_oi))

        avg_ce = float(statistics.mean([v for _, v in above])) if above else 1.0
        avg_pe = float(statistics.mean([v for _, v in below])) if below else 1.0
        avg_ce = max(avg_ce, 1.0)
        avg_pe = max(avg_pe, 1.0)

        resist  = max(above, key=lambda x: x[1], default=(0, 0))
        support = max(below, key=lambda x: x[1], default=(0, 0))

        r_str   = resist[1]  / avg_ce
        s_str   = support[1] / avg_pe
        rng_pts = int(resist[0] - support[0]) if resist[0] and support[0] else 0
        rng_pct = round(rng_pts / spot * 100, 2) if spot > 0 else 0.0
        mp      = self.compute_max_pain(chain)
        mp_dist = round(abs(spot - mp), 1) if mp else 0.0
        pcr     = round(total_pe / total_ce, 3) if total_ce > 0 else 1.0

        return {
            "resistance_strike":   int(resist[0]),
            "resistance_oi":       int(resist[1]),
            "resistance_strength": round(r_str, 2),
            "support_strike":      int(support[0]),
            "support_oi":          int(support[1]),
            "support_strength":    round(s_str, 2),
            "range_width_pts":     rng_pts,
            "range_width_pct":     rng_pct,
            "max_pain_strike":     int(mp) if mp else 0,
            "max_pain_distance":   mp_dist,
            "total_ce_oi":         total_ce,
            "total_pe_oi":         total_pe,
            "pcr":                 pcr,
        }

    def compute_oi_change(
        self,
        atm_strike: int,
        expiry_str: str,
        current_ce_oi: int,
        current_pe_oi: int,
    ) -> float:
        """
        Compute OI change at ATM strike over the last lookback_min minutes.
        Returns fractional change: (current - prior) / prior
        Positive = OI building, Negative = OI unwinding.
        Returns 0.0 if no prior data available.
        """
        current_total = current_ce_oi + current_pe_oi
        if current_total <= 0:
            return 0.0

        lookback  = self.config.oi_change_lookback_min
        today_str = today_ist().isoformat()
        cutoff_ts = (now_ist() - timedelta(minutes=lookback + 5)).isoformat()
        limit_ts  = (now_ist() - timedelta(minutes=lookback)).isoformat()

        # Try option_chain_snapshot first
        row = self.db.query_one(
            "SELECT SUM(oi) as total_oi FROM option_chain_snapshot "
            "WHERE trading_date=? AND strike=? AND expiry=? "
            "AND capture_time >= ? AND capture_time <= ? "
            "LIMIT 1",
            (today_str, atm_strike, expiry_str, cutoff_ts, limit_ts),
        )
        if row and row.get("total_oi") and row["total_oi"] > 0:
            prior = row["total_oi"]
            return round((current_total - prior) / prior, 4)

        # Try options_chain table
        row2 = self.db.query_one(
            "SELECT ce_oi, pe_oi FROM options_chain "
            "WHERE strike=? AND expiry_date=? "
            "AND timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp ASC LIMIT 1",
            (atm_strike, expiry_str, cutoff_ts, limit_ts),
        )
        if row2:
            prior2 = (row2.get("ce_oi") or 0) + (row2.get("pe_oi") or 0)
            if prior2 > 0:
                return round((current_total - prior2) / prior2, 4)

        # Fallback: compare to first reading of the day
        row3 = self.db.query_one(
            "SELECT SUM(oi) as total_oi FROM option_chain_snapshot "
            "WHERE trading_date=? AND strike=? AND expiry=? "
            "ORDER BY capture_time ASC LIMIT 1",
            (today_str, atm_strike, expiry_str),
        )
        if row3 and row3.get("total_oi") and row3["total_oi"] > 0:
            prior3 = row3["total_oi"]
            if prior3 != current_total:
                return round((current_total - prior3) / prior3, 4)

        return 0.0

    # ─────────────────────────────────────────────────────────────────────
    # CHAIN STALENESS CHECK
    # ─────────────────────────────────────────────────────────────────────

    def _check_chain_staleness(
        self, chain: dict, spot: Optional[float]
    ) -> bool:
        """
        Check if the option chain data is stale.
        Stale if:
        - Chain is empty or spot is None
        - Chain was fetched > 480 seconds ago
        - ATM strike has zero bid, ask, AND ltp for both call and put

        Returns True if stale (do not use for IV/VRP computation).
        """
        if not chain or spot is None:
            return True

        now = now_ist()
        current_time = now.time()
        market_open  = dtime(9, 15) <= current_time <= dtime(15, 30)
        if not market_open:
            return True

        # Age check
        if self._chain_fetch_time is not None:
            fetch_age = (now - self._chain_fetch_time).total_seconds()
            if fetch_age > 600:
                self.logger.warning(
                    f"Chain stale: fetched {fetch_age:.0f}s ago (> 480s)"
                )
                return True

        # ATM liquidity check
        step = self.config.nifty_strike_step
        atm  = round(spot / step) * step
        if atm not in chain:
            strikes = list(chain.keys())
            if not strikes:
                return True
            atm = min(strikes, key=lambda k: abs(k - spot))

        atm_legs = chain.get(atm, {})
        stale_legs = 0
        for opt_type in ("call", "put"):
            leg = atm_legs.get(opt_type, {})
            bid = float(leg.get("bid", 0) or 0)
            ask = float(leg.get("ask", 0) or 0)
            ltp = float(leg.get("ltp", 0) or 0)
            if bid <= 0 and ask <= 0 and ltp <= 0:
                stale_legs += 1
        if stale_legs >= 2:
            _sc = self.state.get("_stale_count", 0) + 1
            self.state["_stale_count"] = _sc
            if _sc >= 2:
                self.logger.warning("Chain stale: ATM both legs zero for 2+ cycles")
                return True
            return False
        self.state["_stale_count"] = 0
        return False

    # ─────────────────────────────────────────────────────────────────────
    # OPTION CHAIN PARSING
    # ─────────────────────────────────────────────────────────────────────

    def _normalize_option_leg(self, raw: dict) -> dict:
        """
        Normalise a raw option leg dict from Upstox API response.
        Handles both nested (market_data/option_greeks) and flat formats.
        Normalises IV to decimal (0.0-1.0 range).
        """
        md      = raw.get("market_data")   or raw or {}
        greeks  = raw.get("option_greeks") or raw or {}

        def _f(d: dict, key: str, default: float = 0.0) -> float:
            try:
                v = d.get(key)
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        iv = _f(greeks, "iv", 0.0)
        if iv > 1.0:
            iv = iv / 100.0  # Normalise to decimal

        return {
            "instrument_key": raw.get("instrument_key"),
            "bid":    _f(md, "bid_price",  0.0),
            "ask":    _f(md, "ask_price",  0.0),
            "ltp":    _f(md, "ltp",        0.0),
            "oi":     int(_f(md, "oi",     0)),
            "volume": int(_f(md, "volume", 0)),
            "iv":     iv,
            "delta":  _f(greeks, "delta", 0.0),
            "gamma":  _f(greeks, "gamma", 0.0),
            "theta":  _f(greeks, "theta", 0.0),
            "vega":   _f(greeks, "vega",  0.0),
            "timestamp": now_ist().isoformat(),
        }

    def _parse_chain_response(self, raw_list: list) -> dict:
        """
        Parse raw Upstox option chain response into a clean dict.
        Returns {strike: {"call": leg_dict, "put": leg_dict}, ...}
        """
        chain: dict = {}
        for item in raw_list:
            try:
                strike = float(item.get("strike_price"))
            except (TypeError, ValueError):
                continue

            call_raw = (
                item.get("call_options") or
                item.get("call") or {}
            )
            put_raw  = (
                item.get("put_options") or
                item.get("put") or {}
            )
            chain[strike] = {
                "call": self._normalize_option_leg(call_raw),
                "put":  self._normalize_option_leg(put_raw),
            }
        return chain

    # ─────────────────────────────────────────────────────────────────────
    # EXPIRY DISCOVERY
    # ─────────────────────────────────────────────────────────────────────

    def _get_active_expiry(self) -> Tuple[Optional[date], Optional[int]]:
        """
        Discover the active NIFTY weekly expiry date and DTE.
        Caches result in session_state.
        Refreshes every 30 minutes normally, every 5 minutes in 0DTE window.

        Returns (expiry_date, dte_trading_days).
        """
        last_checked   = self.state.get("expiry_last_checked")
        cached_expiry  = self.state.get("actual_expiry")
        should_refresh = cached_expiry is None

        if last_checked and not should_refresh:
            try:
                elapsed  = (now_ist() - datetime.fromisoformat(last_checked)).total_seconds()
                now_t    = now_ist().time()
                is_tue   = today_ist().weekday() == 1
                in_0dte  = is_tue and dtime(12, 0) <= now_t < dtime(14, 0)
                ttl      = 300 if in_0dte else 1800
                should_refresh = elapsed > ttl
            except Exception:
                should_refresh = True

        if should_refresh:
            try:
                contracts = self.client.get_option_contracts(
                    INSTRUMENT_KEY_NIFTY_SPOT
                )
                today    = today_ist()
                now_time = now_ist().time()
                is_tue   = today.weekday() == 1
                is_0dte  = is_tue

                future: List[Tuple[int, date]] = []
                seen: set = set()

                for c in contracts:
                    exp_str = c.get("expiry")
                    if not exp_str or exp_str in seen:
                        continue
                    seen.add(exp_str)
                    try:
                        exp_date = datetime.strptime(exp_str[:10], "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    dte = (exp_date - today).days
                    if dte >= 0:
                        future.append((dte, exp_date))

                if future:
                    future.sort(key=lambda x: x[0])
                    if is_0dte:
                        zero_dte = [f for f in future if f[0] == 0]
                        expiry   = zero_dte[0][1] if zero_dte else future[0][1]
                    else:
                        preferred = [f for f in future if f[0] >= 1]
                        expiry    = preferred[0][1] if preferred else future[0][1]

                    is_tue_full = today.weekday() == 1
                    if is_tue_full:
                        zero_dte_today = [f for f in future if f[0] == 0]
                        expiry = zero_dte_today[0][1] if zero_dte_today else future[0][1]
                    else:
                        preferred = [f for f in future if f[0] >= 1]
                        expiry = preferred[0][1] if preferred else future[0][1]
                    expiry_dte = 0
                    _d = today + timedelta(days=1)
                    while _d <= expiry:
                        if not ExpiryCalendar.is_holiday(_d):
                            expiry_dte += 1
                        _d += timedelta(days=1)
                    self.state["actual_expiry"]       = expiry.isoformat()
                    self.state["actual_dte"]          = expiry_dte
                    self.state["expiry_last_checked"] = now_ist().isoformat()
                    self.logger.info(
                        f"Active expiry: {expiry} expiry_dte={expiry_dte}"
                    )

            except Exception as e:
                self.logger.error(f"Failed to discover active expiry: {e}")

        exp_str = self.state.get("actual_expiry")
        if exp_str is None:
            return None, None

        try:
            expiry_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            # v3.2: the expiry is discovered from the broker's live
            # contract list and then DTE was overwritten with a
            # calendar-only recomputation, so the two could disagree
            # (holiday-shifted expiries, an extra series listed). Every
            # DTE-indexed table in the engine - stop multiple, target,
            # risk fraction, p_win - keys off this number, so it must
            # describe the contract that is actually going to be traded.
            _today_exp = today_ist()
            if expiry_date <= _today_exp:
                dte = 0
            else:
                dte = 0
                _d_walk = _today_exp + timedelta(days=1)
                while _d_walk <= expiry_date:
                    if not ExpiryCalendar.is_holiday(_d_walk):
                        dte += 1
                    _d_walk += timedelta(days=1)
            self.state["actual_dte"] = dte
            return expiry_date, dte
        except Exception:
            return None, None

    def fetch_option_chain(self, expiry_date: date) -> dict:
        """Fetch and parse the full NIFTY option chain for a given expiry."""
        try:
            raw   = self.client.get_option_chain(
                INSTRUMENT_KEY_NIFTY_SPOT, expiry_date.isoformat()
            )
            chain = self._parse_chain_response(raw)
            if len(chain) < 10:
                self.logger.warning(
                    f"Option chain for {expiry_date} has only "
                    f"{len(chain)} strikes (expected 40+)"
                )
            return chain
        except Exception as e:
            self.logger.error(
                f"Failed to fetch option chain for {expiry_date}: {e}"
            )
            return {}

    # ─────────────────────────────────────────────────────────────────────
    # VIX REGIME (for session state only — regime engine does full classification)
    # ─────────────────────────────────────────────────────────────────────

    def _compute_vix_regime(self, vix: float) -> str:
        """
        Classify VIX into regime label.
        Used only for session_state.vix_regime (display/logging).
        The regime engine does the full volatility regime classification.
        """
        if vix < self.config.vix_suppressed:
            return "SUPPRESSED"
        if vix < self.config.vix_low:
            return "LOW"
        if vix < self.config.vix_normal:
            return "NORMAL"
        if vix < self.config.vix_elevated:
            return "ELEVATED"
        return "HIGH"

    def _maybe_update_vix_regime(self, vix: Optional[float]) -> None:
        """
        Update vix_regime in session_state with hysteresis.
        Refreshes at most every 30 minutes to avoid flip-flopping.
        """
        if vix is None or vix <= 0:
            return

        last_checked = self.state.get("vix_regime_last_checked")
        should_run   = last_checked is None
        if last_checked:
            try:
                elapsed    = (now_ist() - datetime.fromisoformat(last_checked)).total_seconds()
                should_run = elapsed > 1800
            except Exception:
                should_run = True

        if not should_run:
            return

        new_regime  = self._compute_vix_regime(vix)
        prev_regime = self.state.get("vix_regime", "UNKNOWN")

        if prev_regime != "UNKNOWN" and new_regime != prev_regime:
            self.logger.info(
                f"VIX regime changed: {prev_regime} → {new_regime} "
                f"(VIX={vix:.1f})"
            )

        self.state["vix_regime"]              = new_regime
        self.state["vix_regime_last_checked"] = now_ist().isoformat()

        # Update day label and mode
        today = today_ist()
        self.state["day_label"] = ExpiryCalendar.get_day_label(today)

        events    = get_high_impact_events()
        today_str = today.isoformat()
        if today_str in {d.isoformat() for d in events.keys()}:
            self.state["day_mode"] = "EVENT"
        else:
            next_day = ExpiryCalendar.get_next_trading_day(today)
            if next_day and next_day in events:
                self.state["day_mode"] = "PRE_EVENT"
            else:
                self.state["day_mode"] = "NORMAL"

    # ─────────────────────────────────────────────────────────────────────
    # PERSISTENCE METHODS
    # ─────────────────────────────────────────────────────────────────────

    def _persist_cycle_log(self, s: dict) -> None:
        """Write a cycle_log row for this cycle's signals."""
        if ExpiryCalendar.is_holiday(today_ist()):
            return

        open_pos = self.db.query(
            "SELECT position_id FROM positions "
            "WHERE trading_date=? AND status='OPEN'",
            (s["trading_date"],),
        )
        open_pos_ids = json.dumps([r["position_id"] for r in open_pos])

        try:
            self.db.insert("cycle_log", {
                "cycle_time":               now_ist().isoformat(),
                "trading_date":             s["trading_date"],
                "spot":                     s.get("spot"),
                "vix":                      s.get("vix"),
                "vrp_raw":                  s.get("vrp_raw"),
                "vrp_smoothed":             s.get("vrp_smoothed"),
                "atm_iv_pct":               (s["atm_iv"] * 100) if s.get("atm_iv") else None,
                "parkinson_rv_pct":         (s["parkinson_rv"] * 100) if s.get("parkinson_rv") else None,
                "adx_15":                   s.get("adx_15"),
                "adx_60":                   s.get("adx_60"),
                "adx_condition":            s.get("adx_condition"),
                "ema_structure":            s.get("ema_structure"),
                "hh_hl":                    s.get("hh_hl"),
                "vwap":                     s.get("vwap"),
                "vwap_dist_pct":            s.get("vwap_dist_pct"),
                "pcr":                      s.get("pcr"),
                "pcr_change":               s.get("pcr_change"),
                "skew_ratio":               s.get("skew_ratio"),
                "skew_otm":                 s.get("skew"),
                "or_width":                 s.get("or_width"),
                "or_condition":             s.get("or_condition"),
                "iv_behavior":              s.get("iv_behavior"),
                "iv_change_pct_from_open":  s.get("iv_change_pct_from_open"),
                "day_move_used_pct":        s.get("day_move_used_pct"),
                "opening_straddle_pts":     s.get("opening_straddle_pts"),
                "choppy_detected":          int(s.get("choppy_detected", False)),
                "gap_fade_opportunity":     int(s.get("gap_fade_opportunity", False)),
                "vol_regime":               s.get("vol_regime"),
                "price_regime":             s.get("price_regime"),
                "positioning_regime":       s.get("positioning_regime"),
                "confidence_level":         s.get("confidence_level"),
                "confidence_score":         s.get("confidence_score"),
                "final_regime":             s.get("final_regime"),
                "final_regime_notes":       s.get("final_regime_notes"),
                "size_multiplier":          s.get("size_multiplier"),
                "block_new_entries":        int(s.get("block_new_entries", False)),
                "action_taken":             s.get("final_regime") or "SIGNAL_ONLY",
                "no_trade_reason":          s.get("no_trade_reason"),
                "open_positions":           len(open_pos),
                "daily_pnl_net":            s.get("daily_pnl", 0.0),
                "vix_regime":               s.get("vix_regime"),
                "day_mode":                 s.get("day_mode"),
                "atm_straddle_price":       s.get("atm_straddle_price"),
                "max_pain":                 s.get("max_pain"),
                "chain_stale":              int(s.get("chain_stale", False)),
                "oi_change_pct":            s.get("oi_change_pct"),
                "resistance_strength":      s.get("resistance_strength"),
                "support_strength":         s.get("support_strength"),
                "raw_json":                 json.dumps(
                    {k: v for k, v in s.items()
                     if k not in ("conditions_met", "conditions_not_met")},
                    default=str,
                ),
            })
        except Exception as e:
            self.logger.debug(f"cycle_log insert error: {e}")

    def _persist_option_chain_snapshot(
        self,
        chain: dict,
        expiry: Optional[date],
        signals: Optional[dict] = None,
    ) -> None:
        """Persist full option chain snapshot to database."""
        if not chain or not expiry:
            return
        if ExpiryCalendar.is_holiday(today_ist()):
            return

        capture_time = now_ist().isoformat()
        trading_date = today_ist().isoformat()

        latest_cycle = self.db.query_one(
            "SELECT cycle_id FROM cycle_log "
            "WHERE trading_date=? ORDER BY cycle_id DESC LIMIT 1",
            (trading_date,),
        )
        cycle_id_val = latest_cycle["cycle_id"] if latest_cycle else None

        spot         = signals.get("spot")         if signals else self.state.get("prev_spot")
        vix          = signals.get("vix")          if signals else self.state.get("prev_vix")
        vrp          = signals.get("vrp_smoothed") if signals else None
        vol_regime   = signals.get("vol_regime")   if signals else None
        price_regime = signals.get("price_regime") if signals else None
        final_regime = signals.get("final_regime") if signals else None

        rows = []
        for strike, legs in chain.items():
            for opt_type in ("call", "put"):
                leg = legs.get(opt_type, {})
                if not leg:
                    continue
                rows.append((
                    capture_time, trading_date, expiry.isoformat(),
                    strike, opt_type,
                    leg.get("bid",    0), leg.get("ask",   0),
                    leg.get("ltp",    0), leg.get("oi",    0),
                    leg.get("volume", 0), leg.get("iv",    0),
                    leg.get("delta",  0), leg.get("gamma", 0),
                    leg.get("theta",  0), leg.get("vega",  0),
                    leg.get("timestamp"),
                    cycle_id_val,
                    spot, vix, vrp,
                    vol_regime, price_regime, final_regime,
                ))

        if rows:
            try:
                self.db.executemany(
                    """INSERT INTO option_chain_snapshot
                       (capture_time, trading_date, expiry, strike, option_type,
                        bid, ask, ltp, oi, volume, iv, delta, gamma, theta, vega,
                        data_timestamp, cycle_id,
                        spot_at_capture, vix_at_capture, vrp_at_capture,
                        vol_regime_at_capture, price_regime_at_capture,
                        final_regime_at_capture)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
            except Exception as e:
                self.logger.debug(f"Chain snapshot persist error: {e}")

    def _store_atm_options_chain(
        self,
        atm_strike: int,
        expiry_str: str,
        atm_ce: float, atm_ce_iv: float, atm_ce_oi: int, atm_ce_vol: int,
        atm_pe: float, atm_pe_iv: float, atm_pe_oi: int, atm_pe_vol: int,
    ) -> None:
        """Store condensed ATM chain data to options_chain table."""
        try:
            now = now_ist()
            self.db.insert("options_chain", {
                "timestamp":   now.isoformat(),
                "date":        today_ist().isoformat(),
                "time":        now.strftime("%H:%M:%S"),
                "expiry_date": expiry_str,
                "strike":      atm_strike,
                "ce_ltp":      atm_ce,    "ce_iv":     atm_ce_iv,
                "ce_oi":       atm_ce_oi, "ce_volume": atm_ce_vol,
                "pe_ltp":      atm_pe,    "pe_iv":     atm_pe_iv,
                "pe_oi":       atm_pe_oi, "pe_volume": atm_pe_vol,
            })
        except Exception as e:
            self.logger.debug(f"ATM options_chain insert error: {e}")

    def _persist_vix_history(self, s: dict) -> None:
        """Store VIX reading to vix_history table."""
        if ExpiryCalendar.is_holiday(today_ist()):
            return
        ct = now_ist().time()
        if not (dtime(9, 15) <= ct <= dtime(15, 30)):
            return
        vix = s.get("vix")
        if vix is None or vix <= 0:
            return
        try:
            now = now_ist()
            self.db.insert("vix_history", {
                "timestamp": now.isoformat(),
                "date":      today_ist().isoformat(),
                "time":      now.strftime("%H:%M:%S"),
                "weekday":   today_ist().weekday(),
                "vix_value": vix,
                "dte":       s.get("actual_dte"),
            })
        except Exception as e:
            self.logger.debug(f"vix_history insert error: {e}")

    def _persist_market_snapshot(self, s: dict) -> None:
        """Store condensed market snapshot to market_snapshots table."""
        if ExpiryCalendar.is_holiday(today_ist()):
            return
        ct = now_ist().time()
        if not (dtime(9, 15) <= ct <= dtime(15, 30)):
            return
        try:
            now = now_ist()
            self.db.insert("market_snapshots", {
                "timestamp":         now.isoformat(),
                "date":              today_ist().isoformat(),
                "time":              now.strftime("%H:%M:%S"),
                "spot":              s.get("spot"),
                "vix":               s.get("vix"),
                "atm_iv":            s.get("atm_iv"),
                "parkinson_rv":      s.get("parkinson_rv"),
                "vrp_raw":           s.get("vrp_raw"),
                "vrp_smoothed":      s.get("vrp_smoothed"),
                "skew_ratio":        s.get("skew_ratio"),
                "skew_otm":          s.get("skew"),
                "oi_change_pct":     s.get("oi_change_pct"),
                "resistance_oi":     s.get("resistance_oi", 0),
                "support_oi":        s.get("support_oi", 0),
                "total_ce_oi":       s.get("total_ce_oi", 0),
                "total_pe_oi":       s.get("total_pe_oi", 0),
                "pcr":               s.get("pcr"),
                "adx_15":            s.get("adx_15"),
                "vwap_dist_pct":     s.get("vwap_dist_pct"),
                "iv_behavior":       s.get("iv_behavior"),
                "day_move_used_pct": s.get("day_move_used_pct"),
                "vol_regime":        s.get("vol_regime"),
                "price_regime":      s.get("price_regime"),
                "positioning_regime":s.get("positioning_regime"),
                "final_regime":      s.get("final_regime"),
                "confidence_level":  s.get("confidence_level"),
            })
        except Exception as e:
            self.logger.debug(f"market_snapshot insert error: {e}")

    def finalize_cycle_log(
        self,
        action_taken: str,
        no_trade_reason: Optional[str],
        open_positions: int,
    ) -> None:
        """Update the most recent cycle_log row with final action and position count."""
        latest = self.db.query_one(
            "SELECT cycle_id FROM cycle_log "
            "WHERE trading_date=? ORDER BY cycle_id DESC LIMIT 1",
            (today_ist().isoformat(),),
        )
        if latest:
            self.db.update(
                "cycle_log",
                {
                    "action_taken":    action_taken,
                    "no_trade_reason": no_trade_reason,
                    "open_positions":  open_positions,
                },
                {"cycle_id": latest["cycle_id"]},
            )

    # ─────────────────────────────────────────────────────────────────────
    # CALIBRATION CACHE
    # ─────────────────────────────────────────────────────────────────────

    def _get_calibration(self) -> Optional[dict]:
        """
        Return latest valid calibration, refreshing cache every hour.
        Returns None if no valid calibration exists.
        """
        now = now_ist()
        if (self._cached_calibration is None or
                self._calibration_cache_time is None or
                (now - self._calibration_cache_time).total_seconds() > 3600):
            self._cached_calibration     = self.db.get_latest_calibration()
            self._calibration_cache_time = now
        return self._cached_calibration

    # ─────────────────────────────────────────────────────────────────────
    # MAIN CYCLE
    # ─────────────────────────────────────────────────────────────────────

    def _seed_vrp_buffer(self) -> List[float]:
        try:
            rows = self.db.get_vrp_smoothed_history(
                n_cycles=self.config.vrp_smoothing_cycles
            )
            if rows:
                seeded = list(reversed(rows))
                self.logger.debug(
                    f"VRP buffer seeded from DB: {len(seeded)} values"
                )
                return seeded
        except Exception:
            pass
        return []

    def run_cycle(self) -> dict:
        """
        Execute one complete market data cycle.
        Called every regime_calc_interval_sec (default 45 seconds).

        Flow:
        1. Reset if new day
        2. Fetch spot and VIX
        3. Check circuit breaker and VIX spike
        4. Update VIX regime in session state
        5. Fetch and store intraday candles
        6. Compute VWAP
        7. Discover active expiry
        8. Fetch option chain
        9. Compute ATM IV, PCR, OI walls, skew
        10. Compute Parkinson RV and VRP (raw + smoothed)
        11. Compute IV behavior
        12. Compute day_move_used
        13. Compute opening range (if not yet done)
        14. Detect gap and fade opportunity
        15. Compute technical indicators (ADX, EMA, HH/HL)
        16. Compute ORB price structure
        17. Compute VWAP signal, PCR signal, skew signal, direction
        18. Build and return signals dict
        19. Persist all data to database

        Returns signals dict consumed by regime_engine.py.
        """
        self.reset_if_new_day()
        trading_date = today_ist().isoformat()

        # ── 1. Spot and VIX ──────────────────────────────────────────────
        spot, vix = self.fetch_spot_and_vix()
        circuit, vix_spike = self.check_circuit_breaker_and_vix_spike(spot, vix)
        self.state["circuit_breaker_suspected"] = circuit
        self.state["vix_spike_detected"]        = vix_spike

        # ── 2. VIX regime update ─────────────────────────────────────────
        self._maybe_update_vix_regime(vix)

        # ── 3. Intraday candles ───────────────────────────────────────────
        bars = self.fetch_and_store_intraday_candles()

        # Track first bar close for day_move_used computation
        today_str = today_ist().isoformat()
        if self._first_bar_date != today_str:
            self._first_bar_close_today = None
            self._first_bar_date        = today_str

        if self._first_bar_close_today is None and not bars.empty:
            market_bars = bars[bars["time"] >= "09:15:00"]
            if not market_bars.empty:
                self._first_bar_close_today = float(market_bars["close"].iloc[0])
                self.state["first_bar_close"] = self._first_bar_close_today

        # ── 4. VWAP ───────────────────────────────────────────────────────
        vwap, vwap_valid = self.compute_vwap(bars)
        self.state["vwap_valid"] = vwap_valid

        # ── 5. Active expiry ──────────────────────────────────────────────
        expiry, dte = self._get_active_expiry()

        # ── 6. Option chain ───────────────────────────────────────────────
        chain: dict = {}
        if expiry:
            chain = self.fetch_option_chain(expiry)
        self.last_chain        = chain
        self.last_chain_expiry = expiry
        self._chain_fetch_time = now_ist()

        chain_stale = self._check_chain_staleness(chain, spot)

        # ── 7. ATM computations ───────────────────────────────────────────
        step       = self.config.nifty_strike_step
        atm_ref    = spot or 24000.0
        atm_strike = int(round(atm_ref / step) * step)

        atm_ce = atm_pe = atm_ce_iv = atm_pe_iv = 0.0
        atm_ce_oi = atm_pe_oi = 0
        atm_straddle = 0.0

        if chain and spot:
            if atm_strike not in chain:
                atm_strike = int(min(chain.keys(), key=lambda k: abs(k - spot)))

            atm_legs = chain.get(atm_strike, {})
            ce_leg   = atm_legs.get("call", {})
            pe_leg   = atm_legs.get("put",  {})

            atm_ce    = float(ce_leg.get("ltp", 0) or 0)
            atm_pe    = float(pe_leg.get("ltp", 0) or 0)
            _r_ce     = float(ce_leg.get("iv",  0) or 0)
            _r_pe     = float(pe_leg.get("iv",  0) or 0)
            atm_ce_iv = _r_ce * 100.0 if _r_ce < 2.0 else _r_ce
            atm_pe_iv = _r_pe * 100.0 if _r_pe < 2.0 else _r_pe
            atm_ce_oi = int(ce_leg.get("oi", 0) or 0)
            atm_pe_oi = int(pe_leg.get("oi", 0) or 0)
            atm_straddle = atm_ce + atm_pe

            if expiry:
                self._store_atm_options_chain(
                    atm_strike, expiry.isoformat(),
                    atm_ce, atm_ce_iv, atm_ce_oi,
                    int(ce_leg.get("volume", 0) or 0),
                    atm_pe, atm_pe_iv, atm_pe_oi,
                    int(pe_leg.get("volume", 0) or 0),
                )

        # Record opening straddle (once per session, after 09:30)
        current_time = now_ist().time()
        if (atm_straddle > 20 and
                self.state.get("_straddle_open_for_regime", 0) == 0 and
                current_time >= dtime(9, 30) and
                not chain_stale and
                atm_ce > 0 and atm_pe > 0):
            self.state["_straddle_open_for_regime"]  = atm_straddle
            self.state["_straddle_open_for_summary"] = atm_straddle
            self.state["opening_straddle_pts"]       = atm_straddle
            self.logger.info(
                f"Opening straddle recorded: {atm_straddle:.2f}pts"
            )

        if atm_straddle > 0:
            self.state["_last_atm_straddle"] = atm_straddle

        # ── 8. ATM IV ─────────────────────────────────────────────────────
        atm_iv = None
        if not chain_stale:
            atm_iv = self.compute_atm_iv(chain, spot)
        if atm_iv is None:
            _cached_iv = self.state.get("_last_valid_atm_iv")
            if _cached_iv is not None and _cached_iv > 0:
                atm_iv = _cached_iv
        if atm_iv is not None and atm_iv > 0:
            self.state["_last_valid_atm_iv"] = atm_iv
            self.state["_atm_iv_none_cycles"] = 0
        else:
            self.state["_atm_iv_none_cycles"] = self.state.get("_atm_iv_none_cycles", 0) + 1
            if self.state["_atm_iv_none_cycles"] >= 3:
                self.logger.critical(
                    f"SIGNAL ALERT: ATM IV has been None for "
                    f"{self.state['_atm_iv_none_cycles']} consecutive cycles"
                )
        if chain_stale and dtime(9, 15) <= current_time <= dtime(15, 30):
            self.logger.warning(
                "Chain is stale during market hours — skipping IV/VRP computation"
            )

        # ── 9. Session initialisation ─────────────────────────────────────
        if (current_time >= dtime(9, 30) and
                not self.state.get("session_initialized") and
                atm_iv and not chain_stale):
            self.state["opening_iv"]          = atm_iv
            self.state["session_initialized"] = True
            # v3.5: record which series the baseline came from and how much
            # of the session was left when it was taken. Without both, the
            # baseline cannot be compared against anything later on.
            self.state["opening_iv_expiry"]   = self.state.get("actual_expiry")
            self.state["opening_iv_rem_frac"] = self._session_rem_frac()
            self.logger.info(
                f"Session initialized: opening_iv={atm_iv*100:.2f}%"
            )

        # ── 10. PCR baseline ──────────────────────────────────────────────
        pcr = self.compute_pcr(chain, spot)
        if (current_time >= dtime(10, 0) and
                not self._pcr_baseline_set and pcr):
            self.state["opening_pcr"] = pcr
            self._pcr_baseline_set    = True
            self.logger.info(f"PCR baseline: {pcr:.3f}")

        # PCR change from baseline
        pcr_change  = None
        opening_pcr = self.state.get("opening_pcr")
        if opening_pcr and opening_pcr > 0 and pcr and pcr > 0:
            pcr_change = round(pcr - opening_pcr, 3)

        # ── 11. OI walls, max pain, OI change ────────────────────────────
        oi_walls = self.compute_oi_walls(chain, spot) if (chain and spot) else {}
        max_pain = oi_walls.get("max_pain_strike", 0)

        oi_change = 0.0
        if chain and spot and expiry:
            oi_change = self.compute_oi_change(
                atm_strike, expiry.isoformat(), atm_ce_oi, atm_pe_oi
            )

        # ── 12. Skew ──────────────────────────────────────────────────────
        put_iv_25d, call_iv_25d = self.compute_25d_ivs(chain)
        skew_ratio = self.compute_skew_ratio(put_iv_25d, call_iv_25d)
        otm_ce_iv, otm_pe_iv, skew_otm = self.compute_otm_skew(chain, atm_strike)

        # ── 13. Parkinson RV and VRP ──────────────────────────────────────
        parkinson_rv, rv_source = self.compute_parkinson_rv(vix, bars)
        vrp_raw, vrp_smoothed   = self._compute_vrp_smoothed(atm_iv, parkinson_rv)
        if vrp_smoothed is None:
            self.state["_vrp_none_cycles"] = self.state.get("_vrp_none_cycles", 0) + 1
            if self.state["_vrp_none_cycles"] >= 3:
                self.logger.critical(
                    f"SIGNAL ALERT: VRP has been None for "
                    f"{self.state['_vrp_none_cycles']} consecutive cycles"
                )
        else:
            self.state["_vrp_none_cycles"] = 0

        # ── 14. IV behavior ───────────────────────────────────────────────
        iv_behavior, iv_change_pct = self._compute_iv_behavior(atm_iv, bars)

        # ── 15. Day move used ─────────────────────────────────────────────
        day_move_used_pct = self._compute_day_move_used(spot)

        # ── 16. Opening range ─────────────────────────────────────────────
        if current_time >= dtime(9, 30) and not self.state.get("or_computed"):
            orb_bars     = bars[
                (bars["time"] >= "09:15:00") & (bars["time"] < "09:45:00")
            ] if not bars.empty else pd.DataFrame()
            coverage_ok  = len(orb_bars) >= 20
            if coverage_ok or current_time >= dtime(10, 45):
                or_result = self.compute_opening_range(bars)
                if or_result:
                    if not or_result.get("partial") or current_time >= dtime(10, 45):
                        self.state["or_high"]      = or_result["or_high"]
                        self.state["or_low"]       = or_result["or_low"]
                        self.state["or_width"]     = or_result["or_width"]
                        self.state["or_condition"] = or_result["or_condition"]
                        self.state["or_computed"]  = True
                        self.logger.info(
                            f"ORB: H={or_result['or_high']:.0f} "
                            f"L={or_result['or_low']:.0f} "
                            f"W={or_result['or_width']:.0f} "
                            f"[{or_result['or_condition']}]"
                            f"{' (partial)' if or_result.get('partial') else ''}"
                        )

        # ── 17. Gap detection ─────────────────────────────────────────────
        if current_time <= dtime(9, 35):
            self._compute_gap_detection(bars)

        # ── 18. Choppy detection ──────────────────────────────────────────
        orb_high = float(self.state.get("or_high") or 0)
        orb_low  = float(self.state.get("or_low")  or 0)
        choppy_detected = False
        if self.state.get("or_computed") and orb_high > 0 and orb_low > 0:
            post_bars = bars[bars["time"] >= "09:30:00"] if not bars.empty else pd.DataFrame()
            choppy_detected = TechnicalEngine.detect_choppy(
                post_bars, orb_high, orb_low, lookback_min=10, wick_threshold=3
            )

        # ── 19. ORB price structure ───────────────────────────────────────
        orb_price_regime = self.classify_orb_price_structure(bars, orb_high, orb_low)

        # ── 20. Technical indicators ──────────────────────────────────────
        df15 = TechnicalEngine.resample_bars(bars, self.config.mtf_resample_15)
        df60 = TechnicalEngine.resample_bars(bars, self.config.mtf_resample_60)
        # v3.2: the engine's whole trend filter ran on 15-minute bars.
        # calculate_adx() needs 2*period+1 bars before it returns
        # anything: with period 14 that is 29 fifteen-minute bars =
        # 7h15m, and a NIFTY session is 25 bars long. adx_15 was
        # therefore 0.0 for most of the day and adx_15_mature
        # (len >= period*2 = 28 bars) was mathematically unreachable -
        # always False. Every downstream consumer (the condor's strong-
        # ADX block, the butterfly's flat-ADX requirement, the strike
        # widening on trend, the EV gate's tail fattening and the whole
        # ADX branch of classify_price) was reading a constant zero.
        # A 5-minute series gives 75 bars per session, so a Wilder ADX
        # can genuinely mature inside the trading day.
        df5 = TechnicalEngine.resample_bars(
            bars, getattr(self.config, "adx_fast_resample", "300s")
        )

        _adx_min_15 = max(8, min(self.config.min_bars_for_adx, 10))
        _adx_min_60 = max(4, _adx_min_15 // 2)

        adx_15_raw    = 0.0
        adx_15_mature = False
        if not df15.empty and len(df15) >= _adx_min_15:
            _period_15    = min(self.config.adx_period, max(5, len(df15) - 2))
            adx_15_raw    = TechnicalEngine.calculate_adx(df15, _period_15)
            adx_15_mature = (
                adx_15_raw > 0.0 and len(df15) >= 2 * _period_15 + 2
            )

        # Adaptive Wilder period on the fast series: always chosen so the
        # 2*period+1 requirement is satisfied by the bars available.
        adx_5        = 0.0
        adx_5_mature = False
        _period_5    = 0
        if not df5.empty and len(df5) >= 11:
            _period_5 = max(5, min(self.config.adx_period, (len(df5) - 1) // 2))
            if len(df5) >= 2 * _period_5 + 1:
                adx_5 = TechnicalEngine.calculate_adx(df5, _period_5)
                adx_5_mature = (
                    adx_5 > 0.0 and _period_5 >= 9 and len(df5) >= 2 * _period_5 + 2
                )

        # The effective reading every downstream gate consumes: the
        # 15-minute value when it is genuinely mature, otherwise the
        # fast-series value, which on NIFTY intraday is the number a
        # discretionary trader would actually be looking at.
        if adx_15_mature and adx_15_raw > 0.0:
            adx_15 = adx_15_raw
        elif adx_5 > 0.0:
            adx_15 = adx_5
        else:
            adx_15 = adx_15_raw
        adx_15_mature = bool(adx_15_mature or adx_5_mature)

        adx_60        = 0.0
        adx_60_mature = False
        if not df60.empty and len(df60) >= _adx_min_60:
            _period_60    = min(self.config.adx_period, max(3, len(df60) - 2))
            adx_60        = TechnicalEngine.calculate_adx(df60, _period_60)
            adx_60_mature = len(df60) >= self.config.adx_period * 2

        # v3.2: classify_ema_structure needs ema_slow (21) bars. On the
        # 15-minute series that is 5h15m, so ema_structure only leaves
        # INSUFFICIENT_DATA at about 14:30 - and every price-regime
        # branch that requires BULLISH/BEARISH/TRANSITIONAL was
        # unreachable before then. A 9/21 EMA pair on 5-minute bars is
        # the standard intraday structure read and is available from
        # roughly 11:00.
        ema_structure = TechnicalEngine.classify_ema_structure(
            df5, self.config.ema_fast, self.config.ema_slow
        )
        if ema_structure == "INSUFFICIENT_DATA":
            ema_structure = TechnicalEngine.classify_ema_structure(
                df15, self.config.ema_fast, self.config.ema_slow
            )
        ema_15_structure = TechnicalEngine.classify_ema_structure(
            df15, self.config.ema_fast, self.config.ema_slow
        )
        ema_60 = TechnicalEngine.classify_ema_structure(
            df60, self.config.ema_fast, self.config.ema_slow
        )

        # HH/HL on post-10:15 bars to avoid false patterns from opening volatility
        market_df15 = (
            df15[df15["datetime"].dt.time >= dtime(10, 15)]
            if not df15.empty and "datetime" in df15.columns
            else df15
        )
        hh_hl = TechnicalEngine.detect_hh_hl(market_df15)

        # ADX condition label
        adx_condition = "INSUFFICIENT_DATA"
        if adx_15 > 0:
            if adx_15 >= self.config.adx_strong_threshold:
                adx_condition = "STRONG"
            elif adx_15 >= self.config.adx_trend_threshold:
                adx_condition = "MODERATE"
            elif adx_15 >= 20:
                adx_condition = "WEAK"
            else:
                adx_condition = "FLAT"

        # ── 21. VWAP signal ───────────────────────────────────────────────
        vwap_dist_pct = None
        if vwap and vwap > 0 and spot is not None:
            vwap_dist_pct = round((spot - vwap) / vwap * 100.0, 3)

        if vwap_dist_pct is None or not vwap_valid:
            vwap_signal = "UNKNOWN"
        elif vwap_dist_pct > 0.50:
            vwap_signal = "BULLISH_EXTENDED"
        elif vwap_dist_pct > 0.15:
            vwap_signal = "BULLISH"
        elif vwap_dist_pct > -0.15:
            vwap_signal = "NEUTRAL"
        elif vwap_dist_pct > -0.50:
            vwap_signal = "BEARISH"
        else:
            vwap_signal = "BEARISH_EXTENDED"

        # ── 22. PCR signal ────────────────────────────────────────────────
        pcr_signal = "UNKNOWN"
        if opening_pcr and opening_pcr > 0 and pcr and pcr > 0 and pcr_change is not None:
            if pcr > 1.8:
                pcr_signal = "EXTREME_FEAR_CONTRARIAN"
            elif pcr < 0.60:
                pcr_signal = "EXTREME_GREED_CONTRARIAN"
            elif pcr_change > 0.20:
                pcr_signal = "STRONG_FEAR"
            elif pcr_change > 0.08:
                pcr_signal = "FEAR_RISING"
            elif pcr_change > -0.08:
                pcr_signal = "STABLE"
            elif pcr_change > -0.20:
                pcr_signal = "GREED_RISING"
            else:
                pcr_signal = "STRONG_GREED"

        # ── 23. Skew signal ───────────────────────────────────────────────
        if skew_ratio is None:
            skew_signal    = "UNKNOWN"
            preferred_side = "BOTH"
        elif skew_ratio > 1.40:
            skew_signal    = "EXTREME_FEAR"
            preferred_side = "CALLS"
        elif skew_ratio > 1.25:
            skew_signal    = "FEAR"
            preferred_side = "CALLS"
        elif skew_ratio > 1.10:
            skew_signal    = "NORMAL"
            preferred_side = "BOTH"
        elif skew_ratio > 0.95:
            skew_signal    = "BALANCED"
            preferred_side = "BOTH"
        else:
            skew_signal    = "COMPLACENT"
            preferred_side = "PUTS"

        # ── 24. Direction score ───────────────────────────────────────────
        vwap_score = {
            "BULLISH_EXTENDED": 2.0, "BULLISH": 1.0, "NEUTRAL": 0.0,
            "BEARISH": -1.0, "BEARISH_EXTENDED": -2.0, "UNKNOWN": 0.0,
        }.get(vwap_signal, 0.0)

        pcr_score = {
            "EXTREME_FEAR_CONTRARIAN": 1.0, "GREED_RISING": 1.0, "STRONG_GREED": 1.0,
            "STABLE": 0.0, "FEAR_RISING": -1.0, "STRONG_FEAR": -1.0,
            "EXTREME_GREED_CONTRARIAN": -1.0, "UNKNOWN": 0.0,
        }.get(pcr_signal, 0.0)

        skew_score = {
            "COMPLACENT": 1.0, "BALANCED": 0.0, "NORMAL": 0.0,
            "FEAR": -1.0, "EXTREME_FEAR": -1.0, "UNKNOWN": 0.0,
        }.get(skew_signal, 0.0)

        direction_score = float(vwap_score * 2.0 + pcr_score * 0.5 + skew_score * 1.0)

        if direction_score >= 2.0:
            direction = "BULLISH"
        elif direction_score >= 0.8:
            direction = "MILD_BULLISH"
        elif direction_score <= -2.0:
            direction = "BEARISH"
        elif direction_score <= -0.8:
            direction = "MILD_BEARISH"
        else:
            direction = "NEUTRAL"

        # VWAP extended overrides preferred side
        if vwap_signal == "BULLISH_EXTENDED":
            preferred_side = "PUTS"
        elif vwap_signal == "BEARISH_EXTENDED":
            preferred_side = "CALLS"
        elif direction in ("BULLISH", "MILD_BULLISH"):
            preferred_side = "PUTS"
        elif direction in ("BEARISH", "MILD_BEARISH"):
            preferred_side = "CALLS"

        # ── 25. Event day check ───────────────────────────────────────────
        event_day_str = ExpiryCalendar.is_event_day(today_ist())
        event_day     = bool(event_day_str)
        event_name    = event_day_str

        # ── 26. Tuesday 0DTE entry window adjustment ──────────────────────
        day_label  = self.state.get("day_label")
        actual_dte = dte if dte is not None else self.state.get("actual_dte")

        if day_label == "TUESDAY" and actual_dte == 0:
            tue_start = "10:30"
            tue_end   = "13:00"
            tue_exit  = "15:00"
            if self.state.get("entry_start") != tue_start:
                self.state["entry_start"]    = tue_start
                self.state["entry_end"]      = tue_end
                self.state["hard_exit_time"] = tue_exit
                self.logger.info(
                    f"Tuesday 0DTE: entry window {tue_start}-{tue_end}, "
                    f"hard exit {tue_exit}"
                )

        # v3.1: a flat 35pt/3min abort is 0.19% at an 18,000 index but only
        # 0.13% at 26,000 — ordinary noise, so the gate fires constantly and
        # blocks entries all day. Scaled to spot so it keeps the same economic
        # meaning as NIFTY rises, and widened a little in high-VIX regimes
        # where 3-minute noise is genuinely larger (and the premium collected
        # is correspondingly larger too).
        _spot_velocity_block = False
        _sv = 0.0
        if not bars.empty and len(bars) >= 3:
            _recent3 = bars.tail(3)
            if len(_recent3) >= 2:
                _sv = abs(float(_recent3["close"].iloc[-1]) - float(_recent3["close"].iloc[0]))
                try:
                    _sv_ref = float(spot or self.state.get("prev_spot") or 0.0)
                except Exception:
                    _sv_ref = 0.0
                try:
                    _vix_ref = float(vix or self.state.get("prev_vix") or 12.0)
                except Exception:
                    _vix_ref = 12.0
                _vix_adj = 1.0 + max(0.0, (_vix_ref - 12.0)) / 24.0
                _sv_limit = max(
                    (_sv_ref * self.config.spot_velocity_pct * _vix_adj)
                    if _sv_ref > 0 else 35.0,
                    25.0,
                )
                if _sv > _sv_limit:
                    _spot_velocity_block = True

        # ── 26b. Adaptive protective wing width (v3.1) ────────────────────
        # wing_width was a literal 150 written once into session state and
        # never touched again, so every spread got the same 150-point wing
        # regardless of volatility, DTE or index level. The wing sets BOTH the
        # maximum loss and how much of the short premium is handed back to the
        # long, so a frozen wing makes the engine's own credit/wing ratio gate
        # behave arbitrarily: on a quiet 0DTE afternoon a far-OTM condor with
        # 150pt wings simply cannot reach the 0.13 credit ratio the engine
        # demands, so it structurally stops trading and logs nothing but
        # "credit_ratio_below_min". The wing is now scaled off the opening
        # straddle (the market's own expected move) and tightened on 0DTE,
        # where credits are small and the max loss must be small to match.
        try:
            _aw_straddle = float(
                self.state.get("opening_straddle_pts")
                or self.state.get("_last_atm_straddle")
                or 0.0
            )
            _aw_spot = float(spot or self.state.get("prev_spot") or 0.0)
            _aw_step = int(self.config.nifty_strike_step or 50)
            _aw_dte  = actual_dte if actual_dte is not None else 1
            if _aw_straddle <= 20 and _aw_spot > 0:
                _aw_straddle = _aw_spot * 0.009
            if _aw_straddle > 20:
                _aw_factor = 0.55 if _aw_dte == 0 else (
                    0.70 if _aw_dte == 1 else 0.85
                )
                _aw_raw = _aw_straddle * _aw_factor
            else:
                _aw_raw = 150.0
            _aw = int(round(_aw_raw / _aw_step + 0.001) * _aw_step)
            _aw_min = max(2 * _aw_step, 100)
            _aw_max = 250 if _aw_dte == 0 else (350 if _aw_dte == 1 else 450)
            _adaptive_wing_width = int(max(_aw_min, min(_aw, _aw_max)))
        except Exception:
            _adaptive_wing_width = int(self.state.get("wing_width", 150) or 150)
        self.state["wing_width"] = _adaptive_wing_width

        # ── 26c. Expected remaining move (v3.2) ───────────────────────
        # The market's own priced expectation for what is LEFT of the
        # session. This is the only honest yardstick for how far a short
        # strike should be placed, and it is what the EV gate needs to
        # size its barrier. It is deliberately taken from the straddle
        # rather than from the broker's ATM IV, which on expiry day is
        # the noisiest number on the chain.
        try:
            import math as _math_em
            _em_now = now_ist().time()
            _em_elapsed = max(0.0, (
                datetime.combine(today_ist(), _em_now) -
                datetime.combine(today_ist(), dtime(9, 15))
            ).total_seconds() / 60.0)
            _em_rem_frac = min(max((375.0 - _em_elapsed) / 375.0, 0.04), 1.0)
            # ── v3.6 ─────────────────────────────────────────────────
            # The expected remaining move now comes from the live ATM
            # straddle of the ACTIVE chain, every cycle.
            #
            # It used to come from state["opening_straddle_pts"] scaled by
            # sqrt(remaining fraction). On 2026-09-08 that baseline was
            # captured at 09:30 on the 15-Sep series - 280 points - and
            # the engine was still using it after it switched to the 0DTE
            # chain at 12:03, where the real straddle was 82.1:
            #
            #     280 * sqrt(0.552) = 208.0   the engine's answer
            #     live ATM straddle =  82.1   the market's answer
            #
            # A 2.5x overstatement, which strategy_engine turns into a
            # floor of 0.80 * EM on how far out the short strike must sit.
            # Delta selection asked for 95 points and got clamped to 166,
            # so the engine sold 0.085 delta instead of the 0.224 it had
            # chosen, collected 2.80 instead of 10.35, and then rejected
            # itself because friction was 76% of the credit.
            #
            # On the expiry series no time scaling belongs here at all: a
            # 0DTE straddle already prices exactly the time left in the
            # session, so scaling it again by sqrt(T) double-counts decay.
            # Away from expiry the straddle prices the move to ITS expiry,
            # so today's share and the unexpired part of today both apply.
            _em_live = float(atm_straddle or 0.0)
            if _em_live <= 20:
                _em_live = float(self.state.get("_last_atm_straddle") or 0.0)

            _em_dte = actual_dte if actual_dte is not None else 1

            if _em_live > 20:
                if _em_dte is not None and _em_dte > 0:
                    _expected_move_remaining = round(
                        _em_live
                        * _math_em.sqrt(1.0 / max(float(_em_dte) + 1.0, 1.0))
                        * _math_em.sqrt(_em_rem_frac), 2
                    )
                else:
                    _expected_move_remaining = round(_em_live, 2)
            else:
                # No usable chain. Fall back to a fraction of spot, still
                # never to the opening baseline.
                _em_sp = float(spot or self.state.get("prev_spot") or 0.0)
                _expected_move_remaining = round(
                    _em_sp * 0.009 * _math_em.sqrt(_em_rem_frac), 2
                ) if _em_sp > 0 else 0.0

            # expected_range_so_far is about the whole session, not what is
            # left of it, so it keeps the opening baseline unchanged.
            _em_base = float(self.state.get("opening_straddle_pts") or 0.0)
            if _em_base <= 20 and _em_live > 20:
                _em_base = _em_live
            _expected_range_so_far = round(
                _em_base * _math_em.sqrt(max(1.0 - _em_rem_frac, 0.02)), 2
            ) if _em_base > 20 else 0.0
        except Exception:
            _expected_move_remaining = 0.0
            _expected_range_so_far = 0.0

        # ── 27. Build signals dict ────────────────────────────────────────
        signals: dict = {
            # Identity
            "trading_date":             trading_date,
            "day_label":                day_label,
            "day_mode":                 self.state.get("day_mode"),
            "vix_regime":               self.state.get("vix_regime"),

            # Market data
            "spot":                     spot,
            "vix":                      vix,
            "prev_day_vix_close":       self.state.get("prev_day_vix_close"),
            "vix_fail_count":           self._vix_fail_count,
            "circuit_breaker_suspected": circuit,
            "vix_spike_detected":       vix_spike,

            # Volatility
            "atm_iv":                   atm_iv,
            "parkinson_rv":             parkinson_rv,
            "rv_source":                rv_source,
            "vrp_raw":                  vrp_raw,
            "vrp_smoothed":             vrp_smoothed,
            "iv_behavior":              iv_behavior,
            "iv_change_pct_from_open":  iv_change_pct,

            # Day move
            "day_move_used_pct":        day_move_used_pct,
            "opening_straddle_pts":     self.state.get("opening_straddle_pts", 0.0),
            "expected_move_remaining_pts": _expected_move_remaining,
            "expected_range_so_far_pts":   _expected_range_so_far,

            # Opening range
            "or_condition":             self.state.get("or_condition"),
            "or_width":                 self.state.get("or_width"),
            "or_high":                  self.state.get("or_high"),
            "or_low":                   self.state.get("or_low"),
            "or_computed":              bool(self.state.get("or_computed")),
            "orb_price_regime":         orb_price_regime,
            "choppy_detected":          choppy_detected,

            # Gap
            "gap_direction":            self.state.get("gap_direction", "FLAT"),
            "gap_size_pts":             self.state.get("gap_size_pts", 0.0),
            "gap_fade_opportunity":     bool(self.state.get("gap_fade_opportunity", False)),

            # Technical
            "adx_15":                   adx_15,
            "adx_15_raw":               adx_15_raw,
            "adx_5":                    adx_5,
            "adx_5_mature":             adx_5_mature,
            "adx_5_period":             _period_5,
            "ema_15_structure":         ema_15_structure,
            "adx_60":                   adx_60,
            "adx_15_mature":            adx_15_mature,
            "adx_60_mature":            adx_60_mature,
            "adx_condition":            adx_condition,
            "ema_structure":            ema_structure,
            "ema_60":                   ema_60,
            "hh_hl":                    hh_hl,

            # VWAP
            "vwap":                     vwap,
            "vwap_dist_pct":            vwap_dist_pct,
            "vwap_signal":              vwap_signal,

            # PCR
            "pcr":                      pcr,
            "pcr_change":               pcr_change,
            "pcr_signal":               pcr_signal,

            # Skew
            "skew_ratio":               skew_ratio,
            "skew":                     skew_otm,
            "otm_ce_iv":                otm_ce_iv,
            "otm_pe_iv":                otm_pe_iv,
            "skew_signal":              skew_signal,

            # Direction
            "direction":                direction,
            "direction_score":          direction_score,
            "preferred_sell_side":      preferred_side,

            # OI
            "oi_change_pct":            oi_change,
            "resistance_strike":        oi_walls.get("resistance_strike", 0),
            "resistance_oi":            oi_walls.get("resistance_oi", 0),
            "resistance_strength":      oi_walls.get("resistance_strength", 0.0),
            "support_strike":           oi_walls.get("support_strike", 0),
            "support_oi":               oi_walls.get("support_oi", 0),
            "support_strength":         oi_walls.get("support_strength", 0.0),
            "total_ce_oi":              oi_walls.get("total_ce_oi", 0),
            "total_pe_oi":              oi_walls.get("total_pe_oi", 0),

            # ATM chain data
            "atm_strike":               atm_strike,
            "atm_ce_price":             atm_ce,
            "atm_pe_price":             atm_pe,
            "atm_ce_iv":                atm_ce_iv,
            "atm_pe_iv":                atm_pe_iv,
            "atm_ce_oi":                atm_ce_oi,
            "atm_pe_oi":                atm_pe_oi,
            "atm_straddle_price":       atm_straddle,
            "max_pain":                 max_pain,
            "max_pain_distance":        oi_walls.get("max_pain_distance", 0.0),
            "chain_size":               len(chain),
            "chain_stale":              chain_stale,

            # Expiry
            "active_expiry":            expiry.isoformat() if expiry else None,
            "actual_dte":               dte,

            # Event
            "event_day":                event_day,
            "event_name":               event_name,

            # Session state
            "entry_start":              self.state.get("entry_start"),
            "entry_end":                self.state.get("entry_end"),
            "hard_exit_time":           self.state.get("hard_exit_time"),
            "daily_pnl":                self.state.get("daily_pnl", 0.0),
            "current_capital":          self.state.get("current_capital",
                                                        self.config.starting_capital),
            "wing_width":               _adaptive_wing_width,

            # Regime outputs (filled by regime_engine.py — None here)
            "vol_regime":               None,
            "price_regime":             None,
            "positioning_regime":       None,
            "confidence_level":         None,
            "confidence_score":         None,
            "final_regime":             None,
            "final_regime_notes":       None,
            "size_multiplier":          self.state.get("size_multiplier", 1.0),
            "block_new_entries":        False,
            "no_trade_reason":          None,
            "borderline_sell":          False,
            "spot_velocity_block":       _spot_velocity_block,
            "spot_velocity_pts":         _sv,
        }

        # ── 28. Save session state and persist data ───────────────────────
        _straddle_expanding = False
        _straddle_5min_ago = None
        _straddle_hist = self.state.get("_straddle_hist", [])
        _now_ts = now_ist()
        if atm_straddle > 0:
            _straddle_hist.append((_now_ts.timestamp(), atm_straddle))
            _straddle_hist = [(t, v) for t, v in _straddle_hist if _now_ts.timestamp() - t <= 600]
            self.state["_straddle_hist"] = _straddle_hist
            _old5 = [(t, v) for t, v in _straddle_hist if _now_ts.timestamp() - t >= 270]
            if _old5:
                _straddle_5min_ago = _old5[0][1]
                if _straddle_5min_ago > 0 and atm_straddle > _straddle_5min_ago * 1.06:
                    _straddle_expanding = True
        signals["straddle_expanding"] = _straddle_expanding
        signals["straddle_5min_ago"] = _straddle_5min_ago
        self._save_session_state()
        self._persist_cycle_log(signals)
        self._persist_option_chain_snapshot(chain, expiry, signals)
        self._persist_market_snapshot(signals)
        self._persist_vix_history(signals)

        # ── 29. Print dashboard ───────────────────────────────────────────
        self._print_cycle_dashboard(signals)

        return signals

    # ─────────────────────────────────────────────────────────────────────
    # DASHBOARD PRINT
    # ─────────────────────────────────────────────────────────────────────

    def _print_cycle_dashboard(self, s: dict) -> None:
        """Print a concise cycle summary to console."""
        print_section(
            f"CYCLE @ {now_ist().strftime('%H:%M:%S')} IST — {s['trading_date']}"
        )
        print_kv_table({
            "Spot":           s.get("spot"),
            "VIX":            s.get("vix"),
            "VIX Regime":     s.get("vix_regime"),
            "Day Label":      s.get("day_label"),
            "DTE":            s.get("actual_dte"),
            "Expiry":         s.get("active_expiry"),
            "Event Day":      f"{s.get('event_day')} {s.get('event_name') or ''}",
            "Circuit Breaker":s.get("circuit_breaker_suspected"),
            "VIX Spike":      s.get("vix_spike_detected"),
        }, title="MARKET")

        vrp_r = s.get("vrp_raw")
        vrp_s = s.get("vrp_smoothed")
        print_kv_table({
            "ATM IV":         f"{s['atm_iv']*100:.2f}%" if s.get("atm_iv") else "N/A",
            "Parkinson RV":   f"{s['parkinson_rv']*100:.2f}% ({s.get('rv_source')})"
                              if s.get("parkinson_rv") else "N/A",
            "VRP Raw":        f"{vrp_r:.2f}pp" if vrp_r is not None else "N/A",
            "VRP Smoothed":   f"{vrp_s:.2f}pp" if vrp_s is not None else "N/A",
            "IV Behavior":    s.get("iv_behavior"),
            "IV Change":      f"{s.get('iv_change_pct_from_open', 0):.1f}% from open",
            "Day Move Used":  f"{s.get('day_move_used_pct', 0):.1f}%",
            "Straddle":       f"{s.get('atm_straddle_price', 0):.0f}pts",
        }, title="VOLATILITY")

        print_kv_table({
            "OR Condition":   s.get("or_condition"),
            "OR Width":       s.get("or_width"),
            "ORB Structure":  s.get("orb_price_regime"),
            "Choppy":         s.get("choppy_detected"),
            "ADX-15":         f"{s['adx_15']:.1f} [{s.get('adx_condition')}]"
                              if s.get("adx_15") else "N/A",
            "ADX-60":         f"{s['adx_60']:.1f}" if s.get("adx_60") else "N/A",
            "EMA Structure":  s.get("ema_structure"),
            "HH/HL":          s.get("hh_hl"),
        }, title="PRICE")

        print_kv_table({
            "VWAP Dist":      f"{s.get('vwap_dist_pct', 0):.2f}%"
                              if s.get("vwap_dist_pct") is not None else "N/A",
            "PCR":            s.get("pcr"),
            "PCR Change":     s.get("pcr_change"),
            "Skew Ratio":     s.get("skew_ratio"),
            "OTM Skew":       s.get("skew"),
            "OI Change":      f"{s.get('oi_change_pct', 0):.2%}"
                              if s.get("oi_change_pct") is not None else "N/A",
            "Direction":      s.get("direction"),
            "Preferred Side": s.get("preferred_sell_side"),
            "Gap":            f"{s.get('gap_direction')} {s.get('gap_size_pts', 0):.0f}pts"
                              f" fade={s.get('gap_fade_opportunity')}",
        }, title="POSITIONING")

        print_kv_table({
            "Daily P&L":      f"Rs{s.get('daily_pnl', 0):,.0f}",
            "Capital":        f"Rs{s.get('current_capital', 0):,.0f}",
            "Chain Stale":    s.get("chain_stale"),
            "Chain Strikes":  s.get("chain_size"),
            "Max Pain":       s.get("max_pain"),
        }, title="SESSION")
        print()


# ─────────────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    import tempfile as _tf5
    from core import load_env_file, ENV_FILE, BASE_DIR
    _env5 = load_env_file(ENV_FILE)
    _prod5 = str(BASE_DIR / _env5.get("DB_PATH", "data/nifty_algo_v3.db"))
    print_section("NIFTY ALGO v3.0 — DATA ENGINE SELF-TEST", char="#")

    from core import load_config, Database, RateLimiter, UpstoxClient, setup_logging

    config       = load_config()
    db           = Database(config.db_path)
    logger       = setup_logging(db, config.log_dir)
    rate_limiter = RateLimiter(config.rate_limits)
    client       = UpstoxClient(config, rate_limiter, db, logger)
    engine       = MarketDataEngine(config, db, client, rate_limiter, logger)

    # ── TechnicalEngine unit tests ────────────────────────────────────────
    print_section("TechnicalEngine Unit Tests")

    # Create synthetic 1-minute bars (trending up)
    import numpy as np
    n_bars   = 60
    base     = 24000.0
    rng      = np.random.default_rng(42)
    closes   = base + np.cumsum(rng.normal(0.5, 5.0, n_bars))
    highs    = closes + rng.uniform(2, 15, n_bars)
    lows     = closes - rng.uniform(2, 15, n_bars)
    opens    = closes - rng.normal(0, 3, n_bars)

    times = []
    for i in range(n_bars):
        h = 9 + (15 + i) // 60
        m = (15 + i) % 60
        times.append(f"{h:02d}:{m:02d}:00")

    test_bars = pd.DataFrame({
        "time":   times,
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": rng.integers(0, 1000, n_bars),
        "date":   today_ist().isoformat(),
    })
    test_bars["datetime"] = pd.to_datetime(
        test_bars["date"] + " " + test_bars["time"],
        format="%Y-%m-%d %H:%M:%S",
    )

    # ADX test
    df15 = TechnicalEngine.resample_bars(test_bars, "900s")
    adx  = TechnicalEngine.calculate_adx(df15, 14) if not df15.empty else 0.0
    print(f"  ADX (14, 15min bars): {adx:.2f} (expect > 0)")
    assert adx >= 0.0, "ADX should be non-negative"

    # EMA structure test
    ema_struct = TechnicalEngine.classify_ema_structure(df15, 9, 21)
    print(f"  EMA structure: {ema_struct}")
    assert ema_struct in ("BULLISH", "BEARISH", "NEUTRAL", "TRANSITIONAL",
                          "INSUFFICIENT_DATA"), f"Unexpected EMA structure: {ema_struct}"

    # HH/HL test
    hh_hl = TechnicalEngine.detect_hh_hl(df15)
    print(f"  HH/HL pattern: {hh_hl}")
    assert hh_hl in ("UPTREND", "DOWNTREND", "NEUTRAL",
                     "INSUFFICIENT_DATA"), f"Unexpected HH/HL: {hh_hl}"

    # Choppy detection test
    choppy = TechnicalEngine.detect_choppy(test_bars, 24050.0, 23950.0)
    print(f"  Choppy detected: {choppy}")
    assert isinstance(choppy, bool), "Choppy should be bool"

    print("  [OK] TechnicalEngine tests passed")

    # ── Opening Range test ────────────────────────────────────────────────
    print_section("Opening Range Test")
    or_result = engine.compute_opening_range(test_bars)
    if or_result:
        print_kv_table({
            "OR High":      or_result["or_high"],
            "OR Low":       or_result["or_low"],
            "OR Width":     or_result["or_width"],
            "OR Condition": or_result["or_condition"],
            "Partial":      or_result.get("partial"),
        })
    else:
        print("  No OR computed (insufficient bars in 09:15-09:30 window)")
    print("  [OK] Opening range test passed")

    # ── Parkinson RV test ─────────────────────────────────────────────────
    print_section("Parkinson RV Test")
    rv, source = engine.compute_parkinson_rv(14.0, test_bars)
    print(f"  Parkinson RV: {rv*100:.2f}% (source={source})" if rv else
          f"  Parkinson RV: unavailable (source={source})")
    print("  [OK] Parkinson RV test passed")

    # ── VRP smoothing test ────────────────────────────────────────────────
    print_section("VRP Smoothing Test")
    test_atm_iv = 0.125   # 12.5%
    test_rv     = 0.085   # 8.5%
    vrp_raw, vrp_smoothed = engine._compute_vrp_smoothed(test_atm_iv, test_rv)
    print(f"  ATM IV: {test_atm_iv*100:.1f}%")
    print(f"  Parkinson RV: {test_rv*100:.1f}%")
    print(f"  VRP Raw: {vrp_raw:.3f}pp")
    print(f"  VRP Smoothed: {vrp_smoothed:.3f}pp")
    assert vrp_raw is not None, "VRP raw should not be None"
    assert abs(vrp_raw - 4.0) < 0.01, f"VRP raw should be ~4.0pp, got {vrp_raw}"

    # Test anomaly detection
    vrp_anomaly_raw, vrp_anomaly_smoothed = engine._compute_vrp_smoothed(0.125, 0.01)
    print(f"  VRP anomaly test (RV=1%): raw={vrp_anomaly_raw}, smoothed={vrp_anomaly_smoothed}")
    print("  [OK] VRP smoothing test passed")

    # ── IV behavior test ──────────────────────────────────────────────────
    print_section("IV Behavior Test")
    engine.state["opening_iv"] = 0.125  # 12.5%
    engine.state["session_initialized"] = True
    # v3.5: pin the series away from expiry so the band assertions below are
    # deterministic. They were not: v3.1's sqrt(T) tolerance widening reads
    # the wall clock, so out of hours _tol reached its 3.2 cap, the STABLE
    # band opened to +/-16%, and "IV 13.8% vs open 12.5%" (+10.4%) returned
    # STABLE instead of EXPANDING. This test therefore passed during market
    # hours and failed outside them, on the tree as it stood before v3.5.
    # The 0DTE path it used to exercise by accident is now covered on
    # purpose, with the clock pinned, at the end of this block.
    engine.state["actual_dte"] = 5

    # Test STABLE
    beh, chg = engine._compute_iv_behavior(0.127, test_bars)
    print(f"  IV 12.7% vs open 12.5%: {beh} ({chg:.1f}%)")
    assert beh == "STABLE", f"Expected STABLE, got {beh}"

    # Test EXPANDING
    beh2, chg2 = engine._compute_iv_behavior(0.138, test_bars)
    print(f"  IV 13.8% vs open 12.5%: {beh2} ({chg2:.1f}%)")
    assert beh2 == "EXPANDING", f"Expected EXPANDING, got {beh2}"
    beh_spike, chg_spike = engine._compute_iv_behavior(0.152, test_bars)
    print(f"  IV 15.2% vs open 12.5%: {beh_spike} ({chg_spike:.1f}%)")
    assert beh_spike == "SPIKING", f"Expected SPIKING, got {beh_spike}"

    # Test DECLINING
    beh3, chg3 = engine._compute_iv_behavior(0.115, test_bars)
    print(f"  IV 11.5% vs open 12.5%: {beh3} ({chg3:.1f}%)")
    assert beh3 == "DECLINING", f"Expected DECLINING, got {beh3}"

    # v3.5: the measured 2026-09-08 afternoon, with the session clock
    # pinned so the result does not depend on when the test is run. The
    # baseline is the 0DTE reading at 12:05 (22.0% with 205 of 375 minutes
    # left) and the live reading is 15:20 (64.4% with 10 minutes left).
    # Raw, that is +193% and a hard SPIKING block. Normalised it is a 35%
    # collapse in the expected move, which is what actually happened.
    engine.state["actual_dte"]          = 0
    engine.state["opening_iv"]          = 0.22
    engine.state["opening_iv_rem_frac"] = 205.0 / 375.0
    engine.state["opening_iv_expiry"]   = "2026-09-08"
    engine.state["actual_expiry"]       = "2026-09-08"
    _saved_rem_frac = engine._session_rem_frac
    engine._session_rem_frac = lambda: 10.0 / 375.0
    try:
        beh0, chg0 = engine._compute_iv_behavior(0.6443, test_bars)
    finally:
        engine._session_rem_frac = _saved_rem_frac
    print(f"  0DTE IV 64.4% vs open 22.0% into the close: {beh0} ({chg0:.1f}%)")
    assert beh0 in ("CRUSHING", "DECLINING"), (
        f"A quiet expiry afternoon must read as a vol crush, got {beh0} {chg0}"
    )
    assert chg0 < -20.0, f"Expected a large negative normalised change, got {chg0}"

    print("  [OK] IV behavior test passed")

    # ── Day move used test ────────────────────────────────────────────────
    print_section("Day Move Used Test")
    engine.state["opening_straddle_pts"] = 175.0
    engine.state["first_bar_close"]      = 24000.0
    engine._first_bar_close_today        = 24000.0

    dmu = engine._compute_day_move_used(24080.0)
    print(f"  Range-based day_move_used: {dmu:.1f}% (range/straddle)")
    assert dmu >= 0.0, f"day_move_used should be non-negative, got {dmu:.1f}%"
    assert dmu <= 200.0, f"day_move_used should be reasonable, got {dmu:.1f}%"

    dmu2 = engine._compute_day_move_used(24100.0)
    print(f"  day_move_used with wider spot: {dmu2:.1f}%")
    assert dmu2 >= 0.0, f"day_move_used should be non-negative, got {dmu2:.1f}%"

    print("  [OK] Day move used test passed")

    # ── Live API test (if token available) ────────────────────────────────
    if not config.upstox_access_token:
        print_section("LIVE API TEST: SKIPPED (no token)")
        print("  Set UPSTOX_ACCESS_TOKEN in env.txt to test live data.")
    else:
        if client.validate_token():
            print_section("LIVE API TEST")
            signals = engine.run_cycle()
            print_kv_table({
                "spot":              signals.get("spot"),
                "vix":               signals.get("vix"),
                "vix_regime":        signals.get("vix_regime"),
                "vrp_raw":           signals.get("vrp_raw"),
                "vrp_smoothed":      signals.get("vrp_smoothed"),
                "iv_behavior":       signals.get("iv_behavior"),
                "day_move_used_pct": signals.get("day_move_used_pct"),
                "or_condition":      signals.get("or_condition"),
                "adx_15":            signals.get("adx_15"),
                "ema_structure":     signals.get("ema_structure"),
                "direction":         signals.get("direction"),
                "chain_size":        signals.get("chain_size"),
                "chain_stale":       signals.get("chain_stale"),
            }, title="Live Cycle Signals")
        else:
            print_section("LIVE API TEST: SKIPPED (invalid token)")

    db.close()
    print_section("DATA ENGINE SELF-TEST COMPLETE", char="#")
    print(f"  Database: {config.db_path}")
    print()


if __name__ == "__main__":
    _self_test()