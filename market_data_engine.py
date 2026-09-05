from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import pandas as pd
import numpy as np

from nifty_algo_core import (
    Config, Database, RateLimiter, UpstoxClient,
    ExpiryCalendar, now_ist, today_ist, parse_ist_timestamp, IST,
    INSTRUMENT_KEY_NIFTY_SPOT, INSTRUMENT_KEY_INDIA_VIX,
    print_section, print_kv_table,
    load_config, setup_logging,
    get_nse_holidays, get_high_impact_events,
)


class TechnicalEngine:

    @staticmethod
    def resample_bars(bars_1min: pd.DataFrame, interval: str) -> pd.DataFrame:
        if bars_1min.empty:
            return pd.DataFrame()
        try:
            df = bars_1min.copy()
            if "datetime" not in df.columns:
                df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"])
            df = df.set_index("datetime").sort_index()
            resampled = df[["open", "high", "low", "close", "volume"]].resample(
                interval, label="left", closed="left"
            ).agg({
                "open": "first", "high": "max",
                "low": "min", "close": "last", "volume": "sum",
            }).dropna()
            resampled = resampled[resampled["open"] > 0]
            return resampled.reset_index()
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
        if df.empty or len(df) < period + 2:
            return 0.0
        try:
            high = df["high"].values.astype(float)
            low = df["low"].values.astype(float)
            close = df["close"].values.astype(float)
            n = len(high)
            tr_arr = np.zeros(n)
            pdm_arr = np.zeros(n)
            ndm_arr = np.zeros(n)
            for i in range(1, n):
                hl = high[i] - low[i]
                hpc = abs(high[i] - close[i - 1])
                lpc = abs(low[i] - close[i - 1])
                tr_arr[i] = max(hl, hpc, lpc)
                up = high[i] - high[i - 1]
                down = low[i - 1] - low[i]
                pdm_arr[i] = up if (up > down and up > 0) else 0.0
                ndm_arr[i] = down if (down > up and down > 0) else 0.0
            atr = np.zeros(n)
            pdi = np.zeros(n)
            ndi = np.zeros(n)
            atr[period] = tr_arr[1:period + 1].sum()
            pdi_raw = pdm_arr[1:period + 1].sum()
            ndi_raw = ndm_arr[1:period + 1].sum()
            for i in range(period + 1, n):
                atr[i] = atr[i - 1] - atr[i - 1] / period + tr_arr[i]
                pdi_raw = pdi_raw - pdi_raw / period + pdm_arr[i]
                ndi_raw = ndi_raw - ndi_raw / period + ndm_arr[i]
                pdi[i] = 100 * pdi_raw / atr[i] if atr[i] > 0 else 0.0
                ndi[i] = 100 * ndi_raw / atr[i] if atr[i] > 0 else 0.0
            dx_arr = np.zeros(n)
            for i in range(period + 1, n):
                denom = pdi[i] + ndi[i]
                dx_arr[i] = 100 * abs(pdi[i] - ndi[i]) / denom if denom > 0 else 0.0
            valid_dx = dx_arr[period + 1:]
            if len(valid_dx) < period:
                return 0.0
            adx_val = float(np.mean(valid_dx[:period]))
            for i in range(period, len(valid_dx)):
                adx_val = (adx_val * (period - 1) + valid_dx[i]) / period
            return round(adx_val, 2)
        except Exception:
            return 0.0

    @staticmethod
    def calculate_ema(series: pd.Series, period: int) -> Optional[float]:
        if len(series) < period:
            return None
        try:
            return float(series.ewm(span=period, adjust=False).mean().iloc[-1])
        except Exception:
            return None

    @staticmethod
    def classify_ema_structure(df: pd.DataFrame, ema_fast: int, ema_slow: int) -> str:
        if df.empty or len(df) < ema_slow:
            return "INSUFFICIENT_DATA"
        try:
            closes = df["close"]
            fast = TechnicalEngine.calculate_ema(closes, ema_fast)
            slow = TechnicalEngine.calculate_ema(closes, ema_slow)
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

    @staticmethod
    def detect_hh_hl(df: pd.DataFrame, lookback: int = 6) -> str:
        if df.empty or len(df) < lookback + 2:
            return "INSUFFICIENT_DATA"
        try:
            highs = df["high"].values[-lookback:]
            lows = df["low"].values[-lookback:]
            swing_highs = []
            swing_lows = []
            for i in range(1, len(highs) - 1):
                if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
                    swing_highs.append(highs[i])
                if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
                    swing_lows.append(lows[i])
            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                if swing_highs[-1] > swing_highs[-2] and swing_lows[-1] > swing_lows[-2]:
                    return "UPTREND"
                if swing_highs[-1] < swing_highs[-2] and swing_lows[-1] < swing_lows[-2]:
                    return "DOWNTREND"
            return "NEUTRAL"
        except Exception:
            return "INSUFFICIENT_DATA"

    @staticmethod
    def classify_price_from_adx_ema(
        adx_15: float, adx_60: float,
        ema_structure: str, hh_hl: str,
        orb_regime: str,
        adx_trend_threshold: float,
        adx_strong_threshold: float,
    ) -> Tuple[str, bool]:
        if adx_15 <= 0:
            return orb_regime, False

        mtf_aligned = False
        if adx_60 > 0:
            both_trend = (adx_15 >= adx_trend_threshold and adx_60 >= adx_trend_threshold)
            both_range = (adx_15 < adx_trend_threshold and adx_60 < adx_trend_threshold)
            direction_agree = (
                (ema_structure == "BULLISH" and hh_hl in ("UPTREND", "INSUFFICIENT_DATA", "NEUTRAL")) or
                (ema_structure == "BEARISH" and hh_hl in ("DOWNTREND", "INSUFFICIENT_DATA", "NEUTRAL")) or
                (ema_structure in ("NEUTRAL", "TRANSITIONAL", "INSUFFICIENT_DATA"))
            )
            mtf_aligned = (both_trend and direction_agree) or both_range

        if adx_15 >= adx_strong_threshold:
            if ema_structure == "BULLISH" or hh_hl == "UPTREND":
                return "STRONG_UPTREND", mtf_aligned
            if ema_structure == "BEARISH" or hh_hl == "DOWNTREND":
                return "STRONG_DOWNTREND", mtf_aligned
            if orb_regime in ("UPTREND", "STRONG_UPTREND"):
                return "STRONG_UPTREND", mtf_aligned
            if orb_regime in ("DOWNTREND", "STRONG_DOWNTREND"):
                return "STRONG_DOWNTREND", mtf_aligned
            return "RANGE", mtf_aligned

        if adx_15 >= adx_trend_threshold:
            if ema_structure == "BULLISH":
                return "UPTREND", mtf_aligned
            if ema_structure == "BEARISH":
                return "DOWNTREND", mtf_aligned
            if orb_regime in ("UPTREND", "STRONG_UPTREND"):
                return "UPTREND", mtf_aligned
            if orb_regime in ("DOWNTREND", "STRONG_DOWNTREND"):
                return "DOWNTREND", mtf_aligned
            return "RANGE", mtf_aligned

        if orb_regime == "CHOPPY":
            return "CHOPPY", mtf_aligned
        return "RANGE", mtf_aligned


class MarketDataEngine:

    def __init__(self, config: Config, db: Database, client: UpstoxClient,
                 rate_limiter: RateLimiter, logger):
        self.config = config
        self.db = db
        self.client = client
        self.rate_limiter = rate_limiter
        self.logger = logger
        self.tech = TechnicalEngine()

        self._ensure_tables()
        self.state = self._load_or_init_session_state()
        self.last_chain: dict = {}
        self.last_chain_expiry: Optional[date] = None
        self._pcr_baseline_set: bool = False
        self._chain_fetch_time: Optional[datetime] = None
        self._cached_calibration: Optional[dict] = None
        self._calibration_cache_time: Optional[datetime] = None

    def _ensure_tables(self) -> None:
        extra_cols = [
            ("session_state", "or_computed", "INTEGER DEFAULT 0"),
            ("session_state", "session_initialized", "INTEGER DEFAULT 0"),
            ("session_state", "vix_regime_last_checked", "TEXT"),
            ("session_state", "prev_spot", "REAL"),
            ("session_state", "prev_vix", "REAL"),
            ("session_state", "parkinson_rv_pct", "REAL"),
            ("session_state", "parkinson_rv_computed_date", "TEXT"),
            ("session_state", "vwap_valid", "INTEGER DEFAULT 0"),
            ("session_state", "expiry_last_checked", "TEXT"),
            ("session_state", "pre_event_spot", "REAL"),
            ("session_state", "pre_event_iv", "REAL"),
            ("session_state", "event_announcement_time", "TEXT"),
            ("session_state", "last_stop_signal_combo", "TEXT"),
            ("session_state", "gap_fade_opportunity", "INTEGER DEFAULT 0"),
        ]
        for table, col, coltype in extra_cols:
            self.db.ensure_column(table, col, coltype)

    def _load_or_init_session_state(self) -> dict:
        today_str = today_ist().isoformat()
        row = self.db.query_one(
            "SELECT * FROM session_state WHERE trading_date=?", (today_str,)
        )
        if row is not None:
            actual = self.db.query_one(
                "SELECT COUNT(*) as cnt FROM positions WHERE trading_date=? "
                "AND status IN ('OPEN','CLOSED')", (today_str,)
            )
            if actual:
                db_count = actual["cnt"]
                if row.get("entry_count", 0) != db_count:
                    self.db.update("session_state", {"entry_count": db_count},
                                   {"trading_date": today_str})
                    row["entry_count"] = db_count
            for boolcol in ("daily_halted", "circuit_breaker_suspected",
                             "vix_spike_detected", "event_announced",
                             "or_computed", "session_initialized",
                             "vwap_valid", "paper_trade_mode",
                             "gap_fade_opportunity"):
                if boolcol in row and row[boolcol] is not None:
                    row[boolcol] = bool(row[boolcol])
            self.logger.info(f"Loaded session_state for {today_str} (mid-day restart recovery)")
            return dict(row)

        defaults = {
            "trading_date": today_str,
            "day_mode": "NORMAL", "vix_regime": "UNKNOWN",
            "gap_size": "SMALL", "gap_direction": "FLAT", "day_label": None,
            "or_high": None, "or_low": None, "or_width": None, "or_condition": None,
            "entry_start": max(self.config.trading_window_start, dtime(9, 45)).strftime("%H:%M"),
            "entry_end": min(self.config.trading_window_last_entry, dtime(14, 0)).strftime("%H:%M"),
            "hard_exit_time": self.config.hard_exit_time.strftime("%H:%M"),
            "stop_multiplier": 2.0, "size_multiplier": 1.0, "wing_width": 150,
            "entry_count": 0, "reentry_count": 0,
            "daily_halted": False, "consecutive_stops": 0,
            "last_stop_time": None, "last_stop_reason": None, "last_entry_time": None,
            "actual_expiry": None, "actual_dte": None,
            "opening_iv": None, "opening_pcr": None,
            "current_capital": self.config.starting_capital, "daily_pnl": 0.0,
            "circuit_breaker_suspected": False, "vix_spike_detected": False,
            "event_announced": False,
            "paper_trade_mode": self.config.paper_trade_mode,
            "or_computed": False, "session_initialized": False,
            "vix_regime_last_checked": None,
            "prev_spot": None, "prev_vix": None,
            "parkinson_rv_pct": None, "parkinson_rv_computed_date": None,
            "vwap_valid": False, "expiry_last_checked": None,
            "pre_event_spot": None, "pre_event_iv": None,
            "event_announcement_time": None,
            "last_stop_signal_combo": None, "gap_fade_opportunity": False,
            "created_at": now_ist().isoformat(), "updated_at": now_ist().isoformat(),
        }
        insert_row = {k: (int(v) if isinstance(v, bool) else v) for k, v in defaults.items()}
        self.db.insert("session_state", insert_row)
        self.logger.info(f"Initialized fresh session_state for {today_str}")
        return defaults

    def _save_session_state(self) -> None:
        data = dict(self.state)
        trading_date = data.pop("trading_date")
        data.pop("created_at", None)
        data["updated_at"] = now_ist().isoformat()
        for k, v in list(data.items()):
            if isinstance(v, bool):
                data[k] = int(v)
        self.db.update("session_state", data, {"trading_date": trading_date})

    def reset_if_new_day(self) -> None:
        today_str = today_ist().isoformat()
        if self.state.get("trading_date") != today_str:
            self.logger.info(
                f"New trading day: {today_str} (previous: {self.state.get('trading_date')})"
            )
            self._close_stale_prior_day_positions(self.state.get("trading_date"))
            self.state = self._load_or_init_session_state()
            self.last_chain = {}
            self.last_chain_expiry = None
            self._pcr_baseline_set = False

    def _close_stale_prior_day_positions(self, prior_date: Optional[str]) -> None:
        if not prior_date:
            return
        open_pos = self.db.query(
            "SELECT position_id, strategy_name FROM positions "
            "WHERE trading_date=? AND status='OPEN'", (prior_date,)
        )
        for pos in open_pos:
            self.logger.warning(
                f"Stale prior-day position detected: {pos['strategy_name']} "
                f"{pos['position_id'][:16]} from {prior_date} — marking as STALE_CLOSE"
            )
            self.db.update(
                "positions",
                {"status": "CLOSED", "exit_reason": "STALE_PRIOR_DAY_CLOSE",
                 "exit_time": now_ist().isoformat(), "updated_at": now_ist().isoformat()},
                {"position_id": pos["position_id"]}
            )

    def fetch_spot_and_vix(self) -> Tuple[Optional[float], Optional[float]]:
        try:
            data = self.client.get_ltp([INSTRUMENT_KEY_NIFTY_SPOT, INSTRUMENT_KEY_INDIA_VIX])
        except Exception as e:
            self.logger.error(f"Failed to fetch spot/VIX: {e}")
            return self.state.get("prev_spot"), self.state.get("prev_vix")

        spot, vix = None, None
        for key, v in data.items():
            ltp = v.get("last_price")
            key_upper = str(key).upper()
            if "VIX" in key_upper:
                vix = float(ltp) if ltp is not None else None
            elif "NIFTY" in key_upper:
                spot = float(ltp) if ltp is not None else None

        if spot is not None and not (10000 < spot < 50000):
            self.logger.warning(f"Spot {spot} outside valid range — using last known")
            spot = self.state.get("prev_spot")
        if vix is not None and not (5.0 < vix < 90.0):
            self.logger.warning(f"VIX {vix} outside valid range — using last known")
            vix = self.state.get("prev_vix")

        return spot, vix

    def check_circuit_breaker_and_vix_spike(
        self, spot: Optional[float], vix: Optional[float]
    ) -> Tuple[bool, bool]:
        prev_spot = self.state.get("prev_spot")
        prev_vix = self.state.get("prev_vix")
        circuit = False
        vix_spike = self.state.get("vix_spike_detected", False)

        if prev_spot and prev_spot > 0 and spot:
            pct = abs(spot - prev_spot) / prev_spot
            if pct > 0.05:
                circuit = True
                self.logger.warning(
                    f"CIRCUIT BREAKER SUSPECTED: spot moved {pct*100:.2f}% in one cycle"
                )

        if prev_vix and prev_vix > 0 and vix:
            chg = (vix - prev_vix) / prev_vix * 100.0
            if chg > 25.0:
                vix_spike = True
                self.logger.warning(f"VIX SPIKE: {prev_vix:.1f} -> {vix:.1f} ({chg:.1f}%)")
            elif chg < -15.0:
                vix_spike = False

        self.state["prev_spot"] = spot
        self.state["prev_vix"] = vix
        return circuit, vix_spike

    @staticmethod
    def _normalize_candle_row(row) -> dict:
        try:
            if isinstance(row, (list, tuple)):
                ts = parse_ist_timestamp(row[0])
                return {
                    "timestamp": ts, "open": float(row[1]), "high": float(row[2]),
                    "low": float(row[3]), "close": float(row[4]),
                    "volume": int(row[5]) if len(row) > 5 else 0,
                }
            ts = parse_ist_timestamp(row.get("timestamp"))
            return {
                "timestamp": ts,
                "open": float(row.get("open", 0) or 0),
                "high": float(row.get("high", 0) or 0),
                "low": float(row.get("low", 0) or 0),
                "close": float(row.get("close", 0) or 0),
                "volume": int(row.get("volume", 0) or 0),
            }
        except (ValueError, TypeError, IndexError):
            return {"timestamp": None, "open": 0.0, "high": 0.0,
                    "low": 0.0, "close": 0.0, "volume": 0}

    def fetch_and_store_intraday_candles(self) -> pd.DataFrame:
        trading_date = today_ist().isoformat()
        try:
            raw = self.client.get_intraday_candles(INSTRUMENT_KEY_NIFTY_SPOT, "1minute")
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
            bars_1m.append(b)
        bars_1m.sort(key=lambda x: x["timestamp"])

        if bars_1m:
            rows = []
            for b in bars_1m:
                rows.append((
                    trading_date,
                    b["timestamp"].isoformat(),
                    1,
                    b["open"], b["high"], b["low"], b["close"],
                    b.get("volume", 0),
                    "upstox_intraday",
                ))
            try:
                self.db.executemany(
                    "INSERT OR IGNORE INTO intraday_candles "
                    "(trading_date, candle_time, interval_min, open, high, low, close, volume, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    rows,
                )
            except Exception as e:
                self.logger.warning(f"Could not persist intraday candles: {e}")

        return self._load_candles_from_db(trading_date)

    def _load_candles_from_db(self, trading_date: str) -> pd.DataFrame:
        try:
            rows = self.db.query(
                "SELECT candle_time as time, open, high, low, close, volume "
                "FROM intraday_candles WHERE trading_date=? AND interval_min=1 "
                "ORDER BY candle_time",
                (trading_date,),
            )
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df["date"] = trading_date
            df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"])
            return df
        except Exception as e:
            self.logger.warning(f"Could not load candles from DB: {e}")
            return pd.DataFrame()

    def get_today_spot_bars(self) -> pd.DataFrame:
        return self._load_candles_from_db(today_ist().isoformat())

    def _get_prev_close(self) -> Optional[float]:
        today = today_ist()
        from_date = (today - timedelta(days=10)).isoformat()
        to_date = (today - timedelta(days=1)).isoformat()
        try:
            raw = self.client.get_historical_candles(
                INSTRUMENT_KEY_NIFTY_SPOT, "day", from_date, to_date
            )
            bars = [self._normalize_candle_row(r) for r in raw]
            bars = [b for b in bars if b["timestamp"] is not None]
            if not bars:
                return None
            bars.sort(key=lambda b: b["timestamp"])
            return bars[-1]["close"]
        except Exception as e:
            self.logger.warning(f"Failed to fetch previous close: {e}")
            return None

    def compute_opening_range(self, bars: pd.DataFrame) -> Optional[dict]:
        if bars.empty:
            return None
        orb_bars = bars[
            (bars["time"] >= "09:15:00") & (bars["time"] < "09:30:00")
        ]
        orb_bars = orb_bars[
            (orb_bars["high"] > orb_bars["low"]) &
            ((orb_bars["high"] - orb_bars["low"]) <= 1000)
        ]
        if len(orb_bars) < 3:
            spot_now = self.state.get("prev_spot")
            if spot_now and spot_now > 0 and now_ist().time() >= dtime(10, 45):
                return {
                    "or_high": spot_now + 25, "or_low": spot_now - 25,
                    "or_width": 50, "or_condition": "NARROW", "or_score": 1, "partial": True,
                }
            return None

        orb_bars = orb_bars.sort_values("time").head(12)
        or_high = float(orb_bars["high"].max())
        or_low = float(orb_bars["low"].min())
        or_width = or_high - or_low
        if or_width <= 0:
            return None

        or_mid = (or_high + or_low) / 2.0
        or_width_pct = (or_width / or_mid) * 100.0 if or_mid > 0 else 0.0

        if or_width < 40:
            or_condition, or_score = "VERY_NARROW", 2
        elif or_width < 75:
            or_condition, or_score = "NARROW", 1
        elif or_width < 120:
            or_condition, or_score = "MODERATE", 0
        elif or_width < 180:
            or_condition, or_score = "WIDE", -1
        else:
            or_condition, or_score = "VERY_WIDE", -2

        return {
            "or_high": or_high, "or_low": or_low, "or_width": or_width,
            "or_condition": or_condition, "or_score": or_score, "partial": False,
        }

    def classify_orb_price_structure(
        self, bars: pd.DataFrame, orb_high: float, orb_low: float
    ) -> str:
        now = now_ist().time()
        if now < dtime(9, 30) or orb_high == 0 or orb_low == 0:
            return "OBSERVING"

        post = bars[
            (bars["time"] >= "09:30:00") & (bars["time"] <= "15:30:00")
        ] if not bars.empty else pd.DataFrame()
        if post.empty:
            return "OBSERVING"

        last_close = float(post["close"].iloc[-1])

        in_choppy_window = now <= dtime(10, 15)
        if in_choppy_window:
            recent_cutoff = (now_ist() - timedelta(minutes=20)).strftime("%H:%M:%S")
            recent = post[post["time"] >= recent_cutoff]
            check_df = recent if not recent.empty else post
            if (check_df["high"] > orb_high).any() and not (check_df["close"] > orb_high).any():
                return "CHOPPY"
            if (check_df["low"] < orb_low).any() and not (check_df["close"] < orb_low).any():
                return "CHOPPY"
        else:
            if last_close > orb_high + 20:
                return "UPTREND"
            if last_close < orb_low - 20:
                return "DOWNTREND"
            return "RANGE"

        if last_close > orb_high + 20:
            return "UPTREND"
        if last_close < orb_low - 20:
            return "DOWNTREND"
        return "RANGE"


    def compute_parkinson_rv(
        self, vix: Optional[float], bars: Optional[pd.DataFrame] = None
    ) -> Tuple[Optional[float], str]:
        today_str = today_ist().isoformat()

        if bars is not None and not bars.empty and len(bars) >= 12:
            rolling = bars.tail(30)
            valid = rolling[
                (rolling["high"] > rolling["low"]) & (rolling["high"] > 0)
            ]
            if len(valid) >= 6:
                log_hl_sq = [
                    math.log(r["high"] / r["low"]) ** 2
                    for _, r in valid.iterrows()
                    if r["low"] > 0
                ]
                if log_hl_sq:
                    park_const = 1.0 / (4.0 * math.log(2.0))
                    variance = park_const * (sum(log_hl_sq) / len(log_hl_sq))
                    rv = math.sqrt(variance * 375.0 * 252.0)
                    if 0.02 < rv < 0.80:
                        self.state["parkinson_rv_pct"] = rv
                        self.state["parkinson_rv_computed_date"] = today_str
                        return rv, "rolling_intraday"

        if (self.state.get("parkinson_rv_computed_date") == today_str and
                self.state.get("parkinson_rv_pct") is not None and
                today_str == today_ist().isoformat()):
            return self.state["parkinson_rv_pct"], "cached"

        return None, "unavailable"

    def compute_atm_iv(
        self, chain: dict, spot: Optional[float]
    ) -> Optional[float]:
        if not chain or spot is None:
            return None
        step = self.config.nifty_strike_step
        atm = round(spot / step) * step
        if atm not in chain:
            atm = min(chain.keys(), key=lambda k: abs(k - spot))
        leg = chain.get(atm, {})
        call, put = leg.get("call", {}), leg.get("put", {})
        call_iv = call.get("iv", 0.0) or 0.0
        put_iv = put.get("iv", 0.0) or 0.0
        call_oi = call.get("oi", 0) or 0
        put_oi = put.get("oi", 0) or 0

        if call_iv <= 0 and put_iv <= 0:
            return None

        total_oi = call_oi + put_oi
        if total_oi > 0:
            atm_iv = (call_iv * call_oi + put_iv * put_oi) / total_oi
        elif call_iv > 0 and put_iv > 0:
            atm_iv = (call_iv + put_iv) / 2.0
        elif call_iv > 0:
            atm_iv = call_iv
        else:
            atm_iv = put_iv

        if atm_iv < 0.05 or atm_iv > 0.80:
            return None
        try:
            _vix_state = self.state.get("prev_vix")
            if _vix_state and _vix_state > 0:
                vix_decimal = _vix_state / 100.0
                if atm_iv < vix_decimal * 0.60 or atm_iv > vix_decimal * 2.0:
                    self.logger.warning(
                        f"ATM IV {atm_iv*100:.2f}% vs VIX {_vix_state:.2f} — "
                        f"ratio {atm_iv/vix_decimal:.2f} outside 0.60-2.00 range. "
                        f"Chain data may be stale. Treating ATM IV as unavailable."
                    )
                    return None
        except Exception:
            pass
        return atm_iv

    def compute_pcr(
        self, chain: dict, spot: Optional[float] = None
    ) -> Optional[float]:
        if not chain:
            return None
        if spot is not None and spot > 0:
            band = spot * 0.03
            total_put = sum(
                legs.get("put", {}).get("oi", 0) or 0
                for strike, legs in chain.items()
                if (spot - band) <= strike < spot
            )
            total_call = sum(
                legs.get("call", {}).get("oi", 0) or 0
                for strike, legs in chain.items()
                if spot < strike <= (spot + band)
            )
        else:
            total_put = sum(
                legs.get("put", {}).get("oi", 0) or 0 for legs in chain.values()
            )
            total_call = sum(
                legs.get("call", {}).get("oi", 0) or 0 for legs in chain.values()
            )

        if total_call <= 0:
            return None
        pcr = total_put / total_call
        if pcr < 0.3 or pcr > 4.0:
            return None
        return pcr

    def compute_vwap(
        self, bars: pd.DataFrame
    ) -> Tuple[Optional[float], bool]:
        if len(bars) < 30:
            return None, False
        cum_pv, cum_vol = 0.0, 0.0
        for _, row in bars.iterrows():
            typical = (row["high"] + row["low"] + row["close"]) / 3.0
            cum_pv += typical * row["volume"]
            cum_vol += row["volume"]
        if cum_vol > 0:
            return cum_pv / cum_vol, True
        total = len(bars)
        if total < 3:
            return None, False
        avg = sum((r["high"] + r["low"] + r["close"]) / 3.0
                  for _, r in bars.iterrows()) / total
        return avg, False

    def compute_25d_ivs(
        self, chain: dict
    ) -> Tuple[Optional[float], Optional[float]]:
        def find_by_delta(opt_type, target, tol=0.08):
            best_iv, best_diff = None, float("inf")
            for strike, legs in chain.items():
                leg = legs.get(opt_type, {})
                delta = leg.get("delta")
                iv = leg.get("iv", 0.0)
                if delta is None or not iv:
                    continue
                diff = abs(abs(delta) - target)
                if diff < best_diff:
                    best_diff, best_iv = diff, iv
            return best_iv if best_diff <= tol else None

        put_iv = find_by_delta("put", 0.25) or find_by_delta("put", 0.25, 0.10)
        call_iv = find_by_delta("call", 0.25) or find_by_delta("call", 0.25, 0.10)
        return put_iv, call_iv

    def compute_skew_ratio(
        self, put_iv: Optional[float], call_iv: Optional[float]
    ) -> Optional[float]:
        if put_iv is None or call_iv is None or put_iv <= 0.02 or call_iv <= 0.02:
            return None
        skew = put_iv / call_iv
        if skew < 0.5 or skew > 3.0:
            return None
        return skew

    def _check_chain_staleness(self, chain: dict, spot: Optional[float]) -> bool:
        if not chain or spot is None:
            return True
        now = now_ist()
        current_time = now.time()
        market_open = dtime(9, 15) <= current_time <= dtime(15, 30)
        if not market_open:
            return True
        if self._chain_fetch_time is not None:
            fetch_age = (now - self._chain_fetch_time).total_seconds()
            if fetch_age > 480:
                self.logger.warning(
                    f"Chain staleness: chain fetched {fetch_age:.0f}s ago. Treating as stale."
                )
                return True
        step = self.config.nifty_strike_step
        atm = round(spot / step) * step
        if atm not in chain:
            strikes = list(chain.keys())
            if not strikes:
                return True
            atm = min(strikes, key=lambda k: abs(k - spot))
        atm_legs = chain.get(atm, {})
        for opt_type in ("call", "put"):
            leg = atm_legs.get(opt_type, {})
            bid = leg.get("bid", 0) or 0
            ask = leg.get("ask", 0) or 0
            ltp = leg.get("ltp", 0) or 0
            if bid <= 0 and ask <= 0 and ltp <= 0:
                self.logger.warning(
                    f"Chain staleness: ATM {opt_type} has zero bid, ask and ltp."
                )
                return True
        return False

    def compute_otm_skew(
        self, chain: dict, atm_strike: int
    ) -> Tuple[float, float, float]:
        step = self.config.nifty_strike_step
        otm_ce_strike = atm_strike + step
        otm_pe_strike = atm_strike - step

        otm_ce_iv = 0.0
        otm_pe_iv = 0.0

        if otm_ce_strike in chain:
            raw = chain[otm_ce_strike].get("call", {}).get("iv", 0) or 0
            otm_ce_iv = raw * 100.0 if raw < 2.0 else raw

        if otm_pe_strike in chain:
            raw = chain[otm_pe_strike].get("put", {}).get("iv", 0) or 0
            otm_pe_iv = raw * 100.0 if raw < 2.0 else raw

        skew = round(otm_pe_iv - otm_ce_iv, 2) if (otm_pe_iv > 0 and otm_ce_iv > 0) else 0.0
        return otm_ce_iv, otm_pe_iv, skew

    def compute_max_pain(self, chain: dict) -> int:
        if not chain:
            return 0
        strikes = sorted(chain.keys())
        if len(strikes) < 5:
            return 0
        ce_map = {s: chain[s].get("call", {}).get("oi", 0) or 0 for s in strikes}
        pe_map = {s: chain[s].get("put", {}).get("oi", 0) or 0 for s in strikes}
        best_s, best_pain = strikes[0], float("inf")
        for candidate in strikes:
            pain = sum(
                (candidate - s) * ce_map[s] * self.config.lot_size if candidate > s
                else (s - candidate) * pe_map[s] * self.config.lot_size if candidate < s
                else 0
                for s in strikes
            )
            if pain < best_pain:
                best_pain = pain
                best_s = candidate
        return int(best_s)

    def compute_oi_walls(self, chain: dict, spot: float) -> dict:
        empty = {
            "resistance_strike": 0, "resistance_oi": 0, "resistance_strength": 0.0,
            "support_strike": 0, "support_oi": 0, "support_strength": 0.0,
            "range_width_pts": 0, "range_width_pct": 0.0,
            "max_pain_strike": 0, "max_pain_distance": 0.0,
            "total_ce_oi": 0, "total_pe_oi": 0, "pcr": 1.0,
        }
        if not chain:
            return empty

        above, below = [], []
        total_ce = total_pe = 0
        for strike, legs in chain.items():
            ce_oi = legs.get("call", {}).get("oi", 0) or 0
            pe_oi = legs.get("put", {}).get("oi", 0) or 0
            total_ce += ce_oi
            total_pe += pe_oi
            if strike > spot:
                above.append((strike, ce_oi))
            elif strike < spot:
                below.append((strike, pe_oi))

        avg_ce = float(np.mean([v for _, v in above])) if above else 1.0
        avg_pe = float(np.mean([v for _, v in below])) if below else 1.0
        resist = max(above, key=lambda x: x[1], default=(0, 0))
        support = max(below, key=lambda x: x[1], default=(0, 0))
        r_str = resist[1] / avg_ce if avg_ce > 0 else 0.0
        s_str = support[1] / avg_pe if avg_pe > 0 else 0.0
        rng_pts = resist[0] - support[0] if resist[0] and support[0] else 0
        rng_pct = rng_pts / spot * 100 if spot > 0 else 0.0
        mp = self.compute_max_pain(chain)
        mp_dist = abs(spot - mp) if mp else 0.0
        pcr = total_pe / total_ce if total_ce > 0 else 1.0

        return {
            "resistance_strike": int(resist[0]),
            "resistance_oi": int(resist[1]),
            "resistance_strength": round(r_str, 2),
            "support_strike": int(support[0]),
            "support_oi": int(support[1]),
            "support_strength": round(s_str, 2),
            "range_width_pts": rng_pts,
            "range_width_pct": round(rng_pct, 2),
            "max_pain_strike": int(mp) if mp else 0,
            "max_pain_distance": round(mp_dist, 1),
            "total_ce_oi": total_ce,
            "total_pe_oi": total_pe,
            "pcr": round(pcr, 3),
        }

    def compute_oi_change(
        self, atm_strike: int, expiry_str: str,
        current_ce_oi: int, current_pe_oi: int
    ) -> float:
        current_total = current_ce_oi + current_pe_oi
        if current_total <= 0:
            return 0.0
        lookback = self.config.oi_change_lookback_min
        cutoff = (now_ist() - timedelta(minutes=lookback + 5)).isoformat()
        limit_ts = (now_ist() - timedelta(minutes=lookback)).isoformat()
        row = self.db.query_one(
            "SELECT ce_oi, pe_oi FROM options_chain "
            "WHERE strike=? AND expiry_date=? AND timestamp>=? AND timestamp<=? "
            "ORDER BY timestamp ASC LIMIT 1",
            (atm_strike, expiry_str, cutoff, limit_ts),
        )
        if row:
            prior = (row.get("ce_oi") or 0) + (row.get("pe_oi") or 0)
            if prior > 0:
                return (current_total - prior) / prior
        return 0.0

    def _normalize_option_leg(self, raw: dict) -> dict:
        md = raw.get("market_data") or raw or {}
        greeks = raw.get("option_greeks") or raw or {}

        def _f(d, key, default=0.0):
            try:
                v = d.get(key)
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        iv = _f(greeks, "iv", 0.0)
        if iv > 1.0:
            iv = iv / 100.0
        return {
            "instrument_key": raw.get("instrument_key"),
            "bid": _f(md, "bid_price", 0.0),
            "ask": _f(md, "ask_price", 0.0),
            "ltp": _f(md, "ltp", 0.0),
            "oi": int(_f(md, "oi", 0)),
            "volume": int(_f(md, "volume", 0)),
            "iv": iv,
            "delta": _f(greeks, "delta", 0.0),
            "gamma": _f(greeks, "gamma", 0.0),
            "theta": _f(greeks, "theta", 0.0),
            "vega": _f(greeks, "vega", 0.0),
            "timestamp": now_ist().isoformat(),
        }

    def _parse_chain_response(self, raw_list: list) -> dict:
        chain = {}
        for item in raw_list:
            try:
                strike = float(item.get("strike_price"))
            except (TypeError, ValueError):
                continue
            call_raw = item.get("call_options") or item.get("call") or {}
            put_raw = item.get("put_options") or item.get("put") or {}
            chain[strike] = {
                "call": self._normalize_option_leg(call_raw),
                "put": self._normalize_option_leg(put_raw),
            }
        return chain

    def _get_active_expiry(self) -> Tuple[Optional[date], Optional[int]]:
        last_checked = self.state.get("expiry_last_checked")
        cached_expiry = self.state.get("actual_expiry")
        should_refresh = cached_expiry is None

        if last_checked and not should_refresh:
            try:
                elapsed = (now_ist() - datetime.fromisoformat(last_checked)).total_seconds()
                now_t = now_ist().time()
                is_tuesday = today_ist().weekday() == 1
                in_0dte_window = is_tuesday and dtime(12, 0) <= now_t < dtime(14, 0)
                ttl = 300 if in_0dte_window else 1800
                should_refresh = elapsed > ttl
            except Exception:
                should_refresh = True

        if should_refresh:
            try:
                contracts = self.client.get_option_contracts(INSTRUMENT_KEY_NIFTY_SPOT)
                today = today_ist()
                now_time = now_ist().time()
                is_tuesday = today.weekday() == 1
                is_0dte_window = is_tuesday and dtime(12, 30) <= now_time < dtime(14, 0)

                future = []
                seen = set()
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
                    if is_0dte_window:
                        zero_dte = [f for f in future if f[0] == 0]
                        if zero_dte:
                            expiry, dte = zero_dte[0][1], zero_dte[0][0]
                        else:
                            expiry, dte = future[0][1], future[0][0]
                    else:
                        preferred = [f for f in future if f[0] >= 1]
                        expiry, dte = (preferred[0][1], preferred[0][0]) if preferred else (future[0][1], future[0][0])

                    trading_dte = ExpiryCalendar.get_dte(today_ist())
                    self.state["actual_expiry"] = expiry.isoformat()
                    self.state["actual_dte"] = trading_dte
                    self.state["expiry_last_checked"] = now_ist().isoformat()
                    self.logger.info(f"Active expiry: {expiry} (DTE={trading_dte} trading days)")
            except Exception as e:
                self.logger.error(f"Failed to discover active expiry: {e}")

        exp_str = self.state.get("actual_expiry")
        if exp_str is None:
            return None, None
        try:
            expiry_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = ExpiryCalendar.get_dte(today_ist())
            self.state["actual_dte"] = dte
            return expiry_date, dte
        except Exception:
            return None, None

    def fetch_option_chain(self, expiry_date: date) -> dict:
        try:
            raw = self.client.get_option_chain(
                INSTRUMENT_KEY_NIFTY_SPOT, expiry_date.isoformat()
            )
            chain = self._parse_chain_response(raw)
            if len(chain) < 10:
                self.logger.warning(
                    f"Option chain for {expiry_date} has only {len(chain)} strikes"
                )
            return chain
        except Exception as e:
            self.logger.error(f"Failed to fetch option chain for {expiry_date}: {e}")
            return {}

    def _compute_vix_regime(self, vix: float) -> str:
        suppressed_thresh = getattr(self.config, "vix_low", 12.5)
        normal_thresh     = getattr(self.config, "vix_normal", 16.0)
        high_thresh       = getattr(self.config, "vix_high", 22.0)
        extreme_thresh    = getattr(self.config, "vix_extreme_high", 28.0)
        if vix < suppressed_thresh:
            return "SUPPRESSED"
        if vix < normal_thresh:
            return "LOW"
        if vix < high_thresh:
            return "NORMAL"
        if vix < extreme_thresh:
            return "ELEVATED"
        return "HIGH"

    def _compute_regime_with_hysteresis(self, vix: float, prev_regime: str) -> str:
        _sup  = getattr(self.config, "vix_low", 12.5)
        _norm = getattr(self.config, "vix_normal", 16.0)
        _high = getattr(self.config, "vix_high", 22.0)
        _ext  = getattr(self.config, "vix_extreme_high", 28.0)
        _buf  = 0.5
        bands = {
            "SUPPRESSED": (None, _sup + _buf),
            "LOW":        (_sup - _buf, _norm + _buf),
            "NORMAL":     (_norm - _buf, _high + _buf),
            "ELEVATED":   (_high - _buf, _ext + _buf),
            "HIGH":       (_ext - _buf, None),
        }
        if prev_regime in bands:
            lo, hi = bands[prev_regime]
            if (lo is None or vix > lo) and (hi is None or vix < hi):
                return prev_regime
        return self._compute_vix_regime(vix)

    def _maybe_run_pre_session_assessment(self, vix: Optional[float]) -> None:
        last_checked = self.state.get("vix_regime_last_checked")
        should_run = last_checked is None
        if last_checked:
            try:
                should_run = (now_ist() - datetime.fromisoformat(last_checked)).total_seconds() > 1800
            except Exception:
                should_run = True
        if not should_run:
            return

        if vix is None or vix <= 0:
            vix = 14.0

        prev_regime = self.state.get("vix_regime", "UNKNOWN")
        new_regime = self._compute_regime_with_hysteresis(vix, prev_regime)
        if prev_regime != "UNKNOWN" and new_regime != prev_regime:
            self.logger.info(f"VIX_REGIME_CHANGED: {prev_regime} -> {new_regime} at VIX={vix:.1f}")
        self.state["vix_regime"] = new_regime

        today = today_ist()
        events = get_high_impact_events()
        today_str = today.isoformat()
        if today_str in {d.isoformat() for d in events.keys()}:
            self.state["day_mode"] = "EVENT"
        else:
            next_day = ExpiryCalendar.get_next_trading_day(today)
            if next_day and next_day in events:
                self.state["day_mode"] = "PRE_EVENT"
            else:
                self.state["day_mode"] = "NORMAL"

        labels = {0: "MONDAY", 1: "TUESDAY", 2: "WEDNESDAY", 3: "THURSDAY", 4: "FRIDAY"}
        day_label = labels.get(today.weekday(), "WEEKEND")
        self.state["day_label"] = day_label

        vix_size = {
            "SUPPRESSED": 1.0, "LOW": 1.0, "NORMAL": 0.75,
            "ELEVATED": 0.50, "HIGH": 0.25,
        }
        dow_size = {
            "MONDAY": 1.00, "TUESDAY": 0.75, "WEDNESDAY": 0.25,
            "THURSDAY": 0.25, "FRIDAY": 0.25,
        }.get(day_label, 1.0)
        self.state["size_multiplier"] = max(vix_size * dow_size, 0.10)

        dow_stop = {
            "MONDAY": 2.0, "TUESDAY": 1.5, "WEDNESDAY": 2.5,
            "THURSDAY": 2.5, "FRIDAY": 2.5,
        }
        self.state["stop_multiplier"] = dow_stop.get(day_label, 2.0)

        wing_map = {"SUPPRESSED": 150, "LOW": 150, "NORMAL": 150, "ELEVATED": 200, "HIGH": 250}
        self.state["wing_width"] = wing_map.get(new_regime, 150)

        entry_start = self.config.trading_window_start
        last_entry = self.config.trading_window_last_entry
        hard_exit = self.config.hard_exit_time

        if day_label == "TUESDAY" and self.config.tuesday_early_exit_enabled:
            last_entry = min(last_entry, dtime(13, 0))
            hard_exit = min(hard_exit, dtime(15, 25))

        self.state["entry_start"] = entry_start.strftime("%H:%M")
        self.state["entry_end"] = last_entry.strftime("%H:%M")
        self.state["hard_exit_time"] = hard_exit.strftime("%H:%M")
        self.state["vix_regime_last_checked"] = now_ist().isoformat()

    def _compute_wing_from_straddle(
        self, atm_straddle: float, spot: float
    ) -> int:
        step = self.config.nifty_strike_step
        if atm_straddle and atm_straddle > 20:
            raw = atm_straddle * 0.85
            wing = int(round(raw / step) * step)
            return max(100, min(wing, 400))
        return self.state.get("wing_width", 150)

    def _persist_cycle_log(self, s: dict) -> None:
        if ExpiryCalendar.is_holiday(today_ist()):
            return
        open_pos = self.db.query(
            "SELECT position_id FROM positions WHERE trading_date=? AND status='OPEN'",
            (s["trading_date"],),
        )
        open_pos_ids = json.dumps([r["position_id"] for r in open_pos])

        atm_straddle = s.get("atm_straddle_price")
        self.db.insert("cycle_log", {
            "cycle_time": now_ist().isoformat(),
            "trading_date": s["trading_date"],
            "spot": s.get("spot"),
            "vix": s.get("vix"),
            "vrp": s.get("vrp"),
            "atm_iv_pct": (s["atm_iv"] * 100) if s.get("atm_iv") else None,
            "parkinson_rv_pct": (s["parkinson_rv"] * 100) if s.get("parkinson_rv") else None,
            "adx": s.get("adx"),
            "adx_condition": s.get("adx_condition"),
            "vwap": s.get("vwap"),
            "vwap_dist_pct": s.get("vwap_dist_pct"),
            "pcr": s.get("pcr"),
            "pcr_change": s.get("pcr_change"),
            "skew_ratio": s.get("skew_ratio"),
            "or_width": s.get("or_width"),
            "or_condition": s.get("or_condition"),
            "volatility_condition": s.get("volatility_condition"),
            "iv_behavior": s.get("iv_behavior"),
            "trend_condition": s.get("trend_condition"),
            "direction": s.get("direction"),
            "preferred_sell_side": s.get("preferred_sell_side"),
            "final_regime": s.get("final_regime"),
            "confidence": s.get("confidence"),
            "price_regime_15": s.get("price_regime_15"),
            "price_regime_60": s.get("price_regime_60"),
            "mtf_aligned": int(s.get("mtf_aligned", False)),
            "adx_15": s.get("adx_15"),
            "adx_60": s.get("adx_60"),
            "ema_structure": s.get("ema_structure"),
            "oi_change_pct": s.get("oi_change_pct"),
            "skew": s.get("skew"),
            "action_taken": "SIGNAL_ONLY",
            "no_trade_reason": None,
            "conditions_met_json": json.dumps(s.get("conditions_met", {})),
            "conditions_not_met_json": json.dumps(s.get("conditions_not_met", {})),
            "open_positions": 0,
            "daily_pnl_net": s.get("daily_pnl", 0.0),
            "vix_regime": s.get("vix_regime"),
            "day_mode": s.get("day_mode"),
            "open_position_ids": open_pos_ids,
            "vrp_percentile": s.get("vrp_percentile"),
            "max_pain": s.get("max_pain"),
            "atm_straddle_price": atm_straddle,
            "chain_stale": int(s.get("chain_stale", False)),
            "raw_json": json.dumps(
                {k: v for k, v in s.items() if k not in ("atm_greeks", "conditions_met", "conditions_not_met")},
                default=str
            ),
        })

    def _persist_option_chain_snapshot(
        self, chain: dict, expiry: Optional[date], signals: Optional[dict] = None
    ) -> None:
        if not chain or not expiry:
            return
        if ExpiryCalendar.is_holiday(today_ist()):
            return
        capture_time = now_ist().isoformat()
        trading_date = today_ist().isoformat()
        latest_cycle = self.db.query_one(
            "SELECT cycle_id FROM cycle_log WHERE trading_date=? ORDER BY cycle_id DESC LIMIT 1",
            (trading_date,),
        )
        cycle_id_val = latest_cycle["cycle_id"] if latest_cycle else None
        spot = signals.get("spot") if signals else self.state.get("prev_spot")
        vix = signals.get("vix") if signals else self.state.get("prev_vix")
        vrp = signals.get("vrp") if signals else None
        adx = signals.get("adx") if signals else None
        vwap_dist = signals.get("vwap_dist_pct") if signals else None
        trend = signals.get("trend_condition") if signals else None
        vol_cond = signals.get("volatility_condition") if signals else None
        direction = signals.get("direction") if signals else None
        final_regime = signals.get("final_regime") if signals else None
        confidence = signals.get("confidence") if signals else None

        rows = []
        for strike, legs in chain.items():
            for opt_type in ("call", "put"):
                leg = legs.get(opt_type, {})
                if not leg:
                    continue
                rows.append((
                    capture_time, trading_date, expiry.isoformat(),
                    strike, opt_type,
                    leg.get("bid", 0), leg.get("ask", 0), leg.get("ltp", 0),
                    leg.get("oi", 0), leg.get("volume", 0), leg.get("iv", 0),
                    leg.get("delta", 0), leg.get("gamma", 0),
                    leg.get("theta", 0), leg.get("vega", 0),
                    leg.get("timestamp"),
                    cycle_id_val, spot, vix, vrp, adx, vwap_dist,
                    trend, vol_cond, direction, final_regime, confidence,
                ))
        if rows:
            try:
                self.db.executemany(
                    """INSERT INTO option_chain_snapshot
                       (capture_time, trading_date, expiry, strike, option_type,
                        bid, ask, ltp, oi, volume, iv, delta, gamma, theta, vega,
                        data_timestamp, cycle_id, spot_at_capture, vix_at_capture,
                        vrp_at_capture, adx_at_capture, vwap_dist_at_capture,
                        trend_condition_at_capture, volatility_condition_at_capture,
                        direction_at_capture, final_regime_at_capture, confidence_at_capture)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
            except Exception as e:
                self.logger.debug(f"Chain snapshot persist error: {e}")

    def _store_atm_options_chain(
        self, atm_strike: int, expiry_str: str,
        atm_ce: float, atm_ce_iv: float, atm_ce_oi: int, atm_ce_vol: int,
        atm_pe: float, atm_pe_iv: float, atm_pe_oi: int, atm_pe_vol: int,
    ) -> None:
        try:
            self.db.insert("options_chain", {
                "timestamp": now_ist().isoformat(),
                "date": today_ist().isoformat(),
                "time": now_ist().strftime("%H:%M:%S"),
                "expiry_date": expiry_str,
                "strike": atm_strike,
                "ce_ltp": atm_ce, "ce_iv": atm_ce_iv,
                "ce_oi": atm_ce_oi, "ce_volume": atm_ce_vol,
                "pe_ltp": atm_pe, "pe_iv": atm_pe_iv,
                "pe_oi": atm_pe_oi, "pe_volume": atm_pe_vol,
            })
        except Exception as e:
            self.logger.debug(f"ATM options_chain insert error: {e}")

    def _persist_vix_history(self, s: dict) -> None:
        if ExpiryCalendar.is_holiday(today_ist()):
            return
        _ct = now_ist().time()
        if not (dtime(9, 15) <= _ct <= dtime(15, 30)):
            return
        vix = s.get("vix")
        if vix is None or vix <= 0:
            return
        try:
            now = now_ist()
            dte = s.get("actual_dte")
            self.db.insert("vix_history", {
                "timestamp": now.isoformat(),
                "date": today_ist().isoformat(),
                "time": now.strftime("%H:%M:%S"),
                "weekday": today_ist().weekday(),
                "vix_value": vix,
                "dte": dte,
            })
        except Exception as e:
            self.logger.debug(f"vix_history insert error: {e}")

    def _persist_market_snapshot(self, s: dict) -> None:
        if ExpiryCalendar.is_holiday(today_ist()):
            return
        _ct = now_ist().time()
        if not (dtime(9, 15) <= _ct <= dtime(15, 30)):
            return
        try:
            now = now_ist()
            self.db.insert("market_snapshots", {
                "timestamp": now.isoformat(),
                "date": today_ist().isoformat(),
                "time": now.strftime("%H:%M:%S"),
                "spot": s.get("spot"),
                "vix": s.get("vix"),
                "atm_iv": s.get("atm_iv"),
                "skew": s.get("skew"),
                "oi_change_pct": s.get("oi_change_pct"),
                "resistance_oi": s.get("resistance_oi", 0),
                "support_oi": s.get("support_oi", 0),
                "total_ce_oi": s.get("total_ce_oi", 0),
                "total_pe_oi": s.get("total_pe_oi", 0),
                "pcr": s.get("pcr"),
                "adx_15": s.get("adx_15"),
                "vwap_dist_pct": s.get("vwap_dist_pct"),
                "vrp": s.get("vrp"),
                "parkinson_rv": s.get("parkinson_rv"),
                "volatility_condition": s.get("volatility_condition"),
                "trend_condition": s.get("trend_condition"),
                "direction": s.get("direction"),
            })
        except Exception as e:
            self.logger.debug(f"market_snapshot insert error: {e}")

    def finalize_cycle_log(
        self, action_taken: str, no_trade_reason: Optional[str], open_positions: int
    ) -> None:
        latest = self.db.query_one(
            "SELECT cycle_id FROM cycle_log WHERE trading_date=? ORDER BY cycle_id DESC LIMIT 1",
            (today_ist().isoformat(),),
        )
        if latest:
            self.db.update(
                "cycle_log",
                {"action_taken": action_taken, "no_trade_reason": no_trade_reason,
                 "open_positions": open_positions},
                {"cycle_id": latest["cycle_id"]},
            )

    def _is_within_trading_window(self) -> bool:
        t = now_ist().time()
        try:
            entry_start = datetime.strptime(self.state["entry_start"], "%H:%M").time()
            entry_end = datetime.strptime(self.state["entry_end"], "%H:%M").time()
        except Exception:
            entry_start = self.config.trading_window_start
            entry_end = self.config.trading_window_last_entry
        return entry_start <= t <= entry_end

    def _build_conditions_summary(self, s: dict) -> Tuple[dict, dict]:
        checks = {
            "or_computed": bool(self.state.get("or_computed")),
            "session_initialized": bool(self.state.get("session_initialized")),
            "vwap_valid": bool(self.state.get("vwap_valid")),
            "atm_iv_available": s.get("atm_iv") is not None,
            "vrp_available": s.get("vrp") is not None,
            "chain_has_min_strikes": (s.get("chain_size") or 0) >= 10,
            "not_circuit_breaker": not s.get("circuit_breaker_suspected"),
            "not_vix_spike": not s.get("vix_spike_detected"),
            "vix_regime_not_suppressed": s.get("vix_regime") != "SUPPRESSED",
            "within_trading_window": self._is_within_trading_window(),
            "daily_not_halted": not bool(self.state.get("daily_halted")),
        }
        met = {k: v for k, v in checks.items() if v}
        not_met = {k: v for k, v in checks.items() if not v}
        return met, not_met

    def _print_cycle_dashboard(self, s: dict) -> None:
        print_section(f"CYCLE @ {now_ist().strftime('%H:%M:%S')} IST — {s['trading_date']}")
        print_kv_table({
            "Spot": s.get("spot"), "VIX": s.get("vix"),
            "VIX Regime": s.get("vix_regime"), "Day Mode": s.get("day_mode"),
            "Day Label": s.get("day_label"),
            "Active Expiry / DTE": f"{s.get('active_expiry')} / {s.get('actual_dte')}",
            "Event Day": s.get("event_day"), "Event Name": s.get("event_name"),
            "Circuit Breaker": s.get("circuit_breaker_suspected"),
            "VIX Spike": s.get("vix_spike_detected"),
            "Daily P&L": s.get("daily_pnl"), "Capital": s.get("current_capital"),
        }, title="MARKET SNAPSHOT")
        print_kv_table({
            "ATM IV": f"{s['atm_iv']*100:.2f}%" if s.get("atm_iv") else "N/A",
            "Parkinson RV": f"{s['parkinson_rv']*100:.2f}% ({s.get('rv_source')})" if s.get("parkinson_rv") else "N/A",
            "VRP": f"{s['vrp']:.2f}pp" if s.get("vrp") is not None else "N/A",
            "Vol Condition": s.get("volatility_condition"),
            "IV Behavior": s.get("iv_behavior"),
        }, title="VOLATILITY")
        print_kv_table({
            "OR Condition": s.get("or_condition"), "OR Width": s.get("or_width"),
            "ADX-15": f"{s['adx_15']:.1f}" if s.get("adx_15") else "N/A",
            "ADX-60": f"{s['adx_60']:.1f}" if s.get("adx_60") else "N/A",
            "EMA Structure": s.get("ema_structure"),
            "Price Regime 15": s.get("price_regime_15"),
            "Price Regime 60": s.get("price_regime_60"),
            "MTF Aligned": s.get("mtf_aligned"),
            "Trend Condition": s.get("trend_condition"),
        }, title="PRICE / TREND")
        print_kv_table({
            "VWAP Dist": f"{s['vwap_dist_pct']:.2f}%" if s.get("vwap_dist_pct") is not None else "N/A",
            "PCR": s.get("pcr"), "PCR Change": s.get("pcr_change"),
            "Skew Ratio": s.get("skew_ratio"), "OTM Skew": s.get("skew"),
            "OI Change": f"{s['oi_change_pct']:.2%}" if s.get("oi_change_pct") is not None else "N/A",
            "Direction": s.get("direction"),
            "Preferred Side": s.get("preferred_sell_side"),
            "Positioning": s.get("positioning_regime"),
        }, title="POSITIONING")
        if s.get("final_regime"):
            print_kv_table({
                "Final Regime": s.get("final_regime"),
                "Confidence": s.get("confidence"),
                "Raw Size": s.get("raw_size_multiplier"),
                "Final Size": s.get("size_multiplier"),
                "Defined Risk Only": s.get("defined_risk_only"),
                "Calibrated": s.get("is_calibrated"),
                "Calibration Tier": s.get("calibration_tier"),
            }, title="REGIME ENGINE OUTPUT")
        met, not_met = s.get("conditions_met", {}), s.get("conditions_not_met", {})
        print(f"\n  MET ({len(met)}): {', '.join(met.keys()) if met else '(none)'}")
        print(f"  NOT MET ({len(not_met)}): {', '.join(not_met.keys()) if not_met else '(none)'}")
        print()

    def run_cycle(self) -> dict:
        self.reset_if_new_day()
        trading_date = today_ist().isoformat()

        spot, vix = self.fetch_spot_and_vix()
        circuit, vix_spike = self.check_circuit_breaker_and_vix_spike(spot, vix)
        self.state["circuit_breaker_suspected"] = circuit
        self.state["vix_spike_detected"] = vix_spike

        self._maybe_run_pre_session_assessment(vix)

        bars = self.fetch_and_store_intraday_candles()
        vwap, vwap_valid = self.compute_vwap(bars)
        self.state["vwap_valid"] = vwap_valid

        expiry, dte = self._get_active_expiry()
        chain = self.fetch_option_chain(expiry) if expiry else {}
        self.last_chain = chain
        self.last_chain_expiry = expiry
        self._chain_fetch_time = now_ist()

        atm_iv = self.compute_atm_iv(chain, spot)
        pcr = self.compute_pcr(chain, spot)
        put_iv_25d, call_iv_25d = self.compute_25d_ivs(chain)
        skew_ratio = self.compute_skew_ratio(put_iv_25d, call_iv_25d)
        oi_walls = self.compute_oi_walls(chain, spot) if chain and spot else {}
        max_pain = oi_walls.get("max_pain_strike", 0)

        step = self.config.nifty_strike_step
        fut_key = ""
        _fut = None
        _atm_ref = spot or 24000.0
        atm_strike = round(_atm_ref / step) * step

        atm_ce = atm_pe = atm_ce_iv = atm_pe_iv = 0.0
        atm_ce_oi = atm_pe_oi = 0
        otm_ce_iv = otm_pe_iv = skew = 0.0
        atm_straddle = 0.0

        if chain and spot:
            if atm_strike not in chain:
                atm_strike = int(min(chain.keys(), key=lambda k: abs(k - spot)))

            atm_legs = chain.get(atm_strike, {})
            ce_leg = atm_legs.get("call", {})
            pe_leg = atm_legs.get("put", {})

            atm_ce = ce_leg.get("ltp", 0) or 0
            atm_pe = pe_leg.get("ltp", 0) or 0
            _r_ce = ce_leg.get("iv", 0) or 0
            _r_pe = pe_leg.get("iv", 0) or 0
            atm_ce_iv = _r_ce * 100.0 if _r_ce < 2.0 else _r_ce
            atm_pe_iv = _r_pe * 100.0 if _r_pe < 2.0 else _r_pe
            atm_ce_oi = ce_leg.get("oi", 0) or 0
            atm_pe_oi = pe_leg.get("oi", 0) or 0
            atm_straddle = atm_ce + atm_pe

            self._store_atm_options_chain(
                atm_strike, expiry.isoformat() if expiry else "",
                atm_ce, atm_ce_iv, atm_ce_oi, ce_leg.get("volume", 0) or 0,
                atm_pe, atm_pe_iv, atm_pe_oi, pe_leg.get("volume", 0) or 0,
            )

            chain_stale = self._check_chain_staleness(chain, spot)
        if chain_stale:
            if dtime(9, 15) <= now_ist().time() <= dtime(15, 30):
                self.logger.warning("Chain is stale during market hours. Skipping IV/VRP computation.")
            atm_iv = None
        otm_ce_iv, otm_pe_iv, skew = self.compute_otm_skew(chain, atm_strike)

        oi_change = self.compute_oi_change(
            atm_strike, expiry.isoformat() if expiry else "",
            atm_ce_oi, atm_pe_oi
        )

        if atm_straddle > 20 and spot:
            self.state["wing_width"] = self._compute_wing_from_straddle(atm_straddle, spot)

        current_time = now_ist().time()

        if current_time >= dtime(10, 15) and not self.state.get("or_computed"):
            orb_bars = bars[
                (bars["time"] >= "09:15:00") & (bars["time"] < "09:30:00")
            ] if not bars.empty else pd.DataFrame()
            coverage_ok = len(orb_bars) >= 45
            if coverage_ok or current_time >= dtime(10, 45):
                or_result = self.compute_opening_range(bars)
                if or_result:
                    if not or_result.get("partial") or current_time >= dtime(10, 45):
                        self.state["or_high"] = or_result["or_high"]
                        self.state["or_low"] = or_result["or_low"]
                        self.state["or_width"] = or_result["or_width"]
                        self.state["or_condition"] = or_result["or_condition"]
                        self.state["or_computed"] = True
                        self.logger.info(
                            f"ORB: H={or_result['or_high']:.0f} L={or_result['or_low']:.0f} "
                            f"W={or_result['or_width']:.0f} [{or_result['or_condition']}]"
                        )

        if current_time >= dtime(10, 15) and not self.state.get("session_initialized"):
            if atm_iv and not chain_stale:
                self.state["opening_iv"] = atm_iv
                self.state["session_initialized"] = True
                self.logger.info(f"Session initialized: opening_iv={atm_iv*100:.2f}%")

        if current_time >= dtime(10, 0) and not self._pcr_baseline_set and pcr:
            self.state["opening_pcr"] = pcr
            self._pcr_baseline_set = True
            self.logger.info(f"PCR baseline: opening_pcr={pcr:.3f}")

        _day_move_used_pct = 0.0
        _opening_straddle_ref = self.state.get("_straddle_open_for_regime", 0)
        if _opening_straddle_ref > 0 and spot is not None:
            _first_bar_close = None
            if not bars.empty:
                _morning_bars = bars[bars["time"] >= "09:15:00"]
                if not _morning_bars.empty:
                    _first_bar_close = float(_morning_bars["close"].iloc[0])
            if _first_bar_close is None:
                _first_bar_close = spot
            _day_move_used_pct = abs(spot - _first_bar_close) / _opening_straddle_ref * 100.0

        _day_label = self.state.get("day_label")
        _actual_dte = dte if dte is not None else self.state.get("actual_dte")
        if _day_label == "TUESDAY" and _actual_dte == 0:
            _tue_entry_start = "11:00"
            _tue_entry_end = "12:30"
            _tue_hard_exit = "14:30"
            if self.state.get("entry_start") != _tue_entry_start:
                self.state["entry_start"] = _tue_entry_start
                self.state["entry_end"] = _tue_entry_end
                self.state["hard_exit_time"] = _tue_hard_exit
                self.logger.info(
                    f"Tuesday 0DTE: entry window {_tue_entry_start}-{_tue_entry_end} "
                    f"hard exit {_tue_hard_exit} for gamma risk management"
                )

        parkinson_rv, rv_source = self.compute_parkinson_rv(vix, bars)
        vrp = None
        if atm_iv is not None and parkinson_rv is not None:
            vrp = atm_iv * 100.0 - parkinson_rv * 100.0

        iv_behavior = "UNKNOWN"
        iv_change_pct = 0.0
        opening_iv = self.state.get("opening_iv")
        if opening_iv and opening_iv > 0 and atm_iv and atm_iv > 0 and len(bars) >= 6:
            iv_change_pct = (atm_iv - opening_iv) / opening_iv * 100.0
            if iv_change_pct < -12.0:
                iv_behavior = "CRUSHING"
            elif iv_change_pct < -4.0:
                iv_behavior = "DECLINING"
            elif iv_change_pct <= 8.0:
                iv_behavior = "STABLE"
            elif iv_change_pct <= 15.0:
                iv_behavior = "EXPANDING"
            else:
                iv_behavior = "SPIKING"

        orb_high = self.state.get("or_high") or 0.0
        orb_low = self.state.get("or_low") or 0.0
        orb_price_regime = self.classify_orb_price_structure(bars, orb_high, orb_low)

        df15 = TechnicalEngine.resample_bars(bars, self.config.mtf_resample_15)
        df60 = TechnicalEngine.resample_bars(bars, self.config.mtf_resample_60)

        adx_15 = 0.0
        adx_15_mature = False
        if not df15.empty and len(df15) >= self.config.min_bars_for_adx:
            adx_15 = TechnicalEngine.calculate_adx(df15, self.config.adx_period)
            adx_15_mature = len(df15) >= self.config.adx_period * 2

        adx_60 = 0.0
        adx_60_mature = False
        if not df60.empty and len(df60) >= self.config.min_bars_for_adx:
            adx_60 = TechnicalEngine.calculate_adx(df60, self.config.adx_period)
            adx_60_mature = len(df60) >= self.config.adx_period * 2

        ema_structure = TechnicalEngine.classify_ema_structure(
            df15, self.config.ema_fast, self.config.ema_slow
        )
        ema_60 = TechnicalEngine.classify_ema_structure(
            df60, self.config.ema_fast, self.config.ema_slow
        )

        market_df15 = df15[
            df15["datetime"].dt.time >= dtime(10, 15)
        ] if not df15.empty and "datetime" in df15.columns else df15
        hh_hl = TechnicalEngine.detect_hh_hl(market_df15)

        price_regime_15, mtf_aligned = TechnicalEngine.classify_price_from_adx_ema(
            adx_15, adx_60, ema_structure, hh_hl, orb_price_regime,
            self.config.adx_trend_threshold, self.config.adx_strong_threshold,
        )
        price_regime_60, _ = TechnicalEngine.classify_price_from_adx_ema(
            adx_60, 0.0, ema_60, "INSUFFICIENT_DATA", orb_price_regime,
            self.config.adx_trend_threshold, self.config.adx_strong_threshold,
        )

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

        vwap_dist_pct = None
        if vwap and vwap > 0 and spot is not None:
            vwap_dist_pct = (spot - vwap) / vwap * 100.0

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

        pcr_signal = "UNKNOWN"
        pcr_change = None
        opening_pcr = self.state.get("opening_pcr")
        if opening_pcr and opening_pcr > 0 and pcr and pcr > 0:
            pcr_change = pcr - opening_pcr
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

        if skew is not None and skew < -1.5:
            self.logger.warning(
                f"OTM skew={skew:.2f} is negative beyond -1.5 — "
                f"NIFTY structural skew violation. Treating skew as UNKNOWN."
            )
            skew = None
            skew_ratio = None
        if skew_ratio is None:
            skew_signal = "UNKNOWN"
            preferred_side = "BOTH"
        elif skew_ratio > 1.40:
            skew_signal = "EXTREME_FEAR"
            preferred_side = "CALLS"
        elif skew_ratio > 1.25:
            skew_signal = "FEAR"
            preferred_side = "CALLS"
        elif skew_ratio > 1.10:
            skew_signal = "NORMAL"
            preferred_side = "BOTH"
        elif skew_ratio > 0.95:
            skew_signal = "BALANCED"
            preferred_side = "BOTH"
        else:
            skew_signal = "COMPLACENT"
            preferred_side = "PUTS"

        direction_score = float(
            ({"BULLISH_EXTENDED": 1, "BULLISH": 1, "NEUTRAL": 0,
              "BEARISH": -1, "BEARISH_EXTENDED": -1, "UNKNOWN": 0}.get(vwap_signal, 0) * 2.0) +
            ({"EXTREME_FEAR_CONTRARIAN": 1, "GREED_RISING": 1, "STRONG_GREED": 1,
              "STABLE": 0, "FEAR_RISING": -1, "STRONG_FEAR": -1,
              "EXTREME_GREED_CONTRARIAN": -1, "UNKNOWN": 0}.get(pcr_signal, 0) * 0.5) +
            ({"COMPLACENT": 1, "BALANCED": 0, "NORMAL": 0,
              "FEAR": -1, "EXTREME_FEAR": -1, "UNKNOWN": 0}.get(skew_signal, 0) * 1.0)
        )

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

        if vwap_signal == "BULLISH_EXTENDED":
            preferred_side = "PUTS"
        elif vwap_signal == "BEARISH_EXTENDED":
            preferred_side = "CALLS"
        elif direction in ("BULLISH", "MILD_BULLISH"):
            preferred_side = "PUTS"
        elif direction in ("BEARISH", "MILD_BEARISH"):
            preferred_side = "CALLS"

        sell_ok = False
        buy_ok = False
        volatility_condition = "UNKNOWN"

        _cal_vrp_sell = 3.0
        _cal_vrp_fair = 1.5
        try:
            _now = now_ist()
            if (self._cached_calibration is None or
                    self._calibration_cache_time is None or
                    (_now - self._calibration_cache_time).total_seconds() > 3600):
                self._cached_calibration = self.db.get_latest_calibration()
                self._calibration_cache_time = _now
            if self._cached_calibration:
                _cal_vrp_sell = float(self._cached_calibration.get("vrp_sell_threshold") or 3.0)
                _cal_vrp_fair = float(self._cached_calibration.get("vrp_fair_threshold") or 1.5)
        except Exception:
            pass
        _vrp_very_rich = _cal_vrp_sell * 1.25
        if vrp is not None and not vix_spike:
            if vrp > _vrp_very_rich:
                volatility_condition = "VERY_RICH"
                sell_ok = True
            elif vrp > _cal_vrp_sell:
                volatility_condition = "RICH"
                sell_ok = True
            elif vrp > _cal_vrp_fair:
                volatility_condition = "FAIR"
                sell_ok = True
            elif vrp > 0.0:
                volatility_condition = "THIN"
            elif vrp > -2.0:
                volatility_condition = "CHEAP"
                buy_ok = True
            else:
                volatility_condition = "INVERTED"
                buy_ok = True

            vix_regime = self.state.get("vix_regime", "NORMAL")
            if vix_regime == "SUPPRESSED":
                if vrp > 3.0:
                    sell_ok = True
                else:
                    sell_ok = False
                    buy_ok = True
        elif vix_spike:
            volatility_condition = "SPIKING"
            sell_ok = False
            buy_ok = False

        if iv_behavior in ("EXPANDING", "SPIKING"):
            sell_ok = False

        event_day_str = ExpiryCalendar.is_event_day(today_ist())
        event_day = bool(event_day_str)
        event_name = event_day_str

        signals: dict = {
            "trading_date": trading_date,
            "spot": spot, "vix": vix,
            "vix_regime": self.state.get("vix_regime"),
            "day_mode": self.state.get("day_mode"),
            "day_label": self.state.get("day_label"),
            "event_day": event_day, "event_name": event_name,
            "circuit_breaker_suspected": circuit,
            "vix_spike_detected": vix_spike,
            "atm_iv": atm_iv, "parkinson_rv": parkinson_rv,
            "rv_source": rv_source, "vrp": vrp,
            "volatility_condition": volatility_condition,
            "iv_behavior": iv_behavior, "iv_change_pct": iv_change_pct,
            "sell_ok": sell_ok, "buy_ok": buy_ok,
            "or_condition": self.state.get("or_condition"),
            "or_width": self.state.get("or_width"),
            "or_high": self.state.get("or_high"),
            "or_low": self.state.get("or_low"),
            "adx": adx_15, "adx_condition": adx_condition,
            "adx_15": adx_15, "adx_60": adx_60,
            "adx_15_mature": adx_15_mature, "adx_60_mature": adx_60_mature,
            "adx_15_mature": adx_15_mature, "adx_60_mature": adx_60_mature,
            "ema_structure": ema_structure,
            "price_regime_15": price_regime_15,
            "price_regime_60": price_regime_60,
            "mtf_aligned": mtf_aligned,
            "trend_condition": price_regime_15,
            "vwap": vwap, "vwap_dist_pct": vwap_dist_pct,
            "pcr": pcr, "pcr_change": pcr_change,
            "skew_ratio": skew_ratio, "skew": skew,
            "otm_ce_iv": otm_ce_iv, "otm_pe_iv": otm_pe_iv,
            "oi_change_pct": oi_change,
            "direction": direction, "direction_score": direction_score,
            "vwap_signal": vwap_signal, "pcr_signal": pcr_signal,
            "skew_signal": skew_signal, "preferred_sell_side": preferred_side,
            "positioning_regime": None,
            "final_regime": None, "confidence": None,
            "size_multiplier": self.state.get("size_multiplier", 1.0),
            "raw_size_multiplier": self.state.get("size_multiplier", 1.0),
            "defined_risk_only": event_day,
            "is_calibrated": False, "calibration_tier": 0,
            "entry_start": self.state.get("entry_start"),
            "entry_end": self.state.get("entry_end"),
            "hard_exit_time": self.state.get("hard_exit_time"),
            "chain_size": len(chain),
            "day_move_used_pct": _day_move_used_pct,
            "chain_stale": chain_stale,
            "active_expiry": expiry.isoformat() if expiry else None,
            "actual_dte": dte,
            "max_pain": max_pain,
            "atm_straddle_price": atm_straddle,
            "atm_strike": atm_strike,
            "atm_ce_price": atm_ce, "atm_pe_price": atm_pe,
            "atm_ce_iv": atm_ce_iv, "atm_pe_iv": atm_pe_iv,
            "atm_ce_oi": atm_ce_oi, "atm_pe_oi": atm_pe_oi,
            "resistance_strike": oi_walls.get("resistance_strike", 0),
            "resistance_oi": oi_walls.get("resistance_oi", 0),
            "resistance_strength": oi_walls.get("resistance_strength", 0.0),
            "support_strike": oi_walls.get("support_strike", 0),
            "support_oi": oi_walls.get("support_oi", 0),
            "support_strength": oi_walls.get("support_strength", 0.0),
            "total_ce_oi": oi_walls.get("total_ce_oi", 0),
            "total_pe_oi": oi_walls.get("total_pe_oi", 0),
            "daily_pnl": self.state.get("daily_pnl", 0.0),
            "current_capital": self.state.get("current_capital", self.config.starting_capital),
            "wing_width": self.state.get("wing_width", 150),
            "stop_multiplier": self.state.get("stop_multiplier", 2.0),
        }

        met, not_met = self._build_conditions_summary(signals)
        signals["conditions_met"] = met
        signals["conditions_not_met"] = not_met

        self._save_session_state()
        self._persist_cycle_log(signals)
        self._persist_option_chain_snapshot(chain, expiry, signals)
        self._persist_market_snapshot(signals)
        self._persist_vix_history(signals)
        self._print_cycle_dashboard(signals)

        return signals


def _self_test() -> None:
    print_section("NIFTY ALGO — MARKET DATA ENGINE SELF-TEST")
    config = load_config()
    db = Database(config.db_path)
    logger = setup_logging(db, config.log_dir)
    rate_limiter = RateLimiter(config.rate_limits)
    client = UpstoxClient(config, rate_limiter, db, logger)
    engine = MarketDataEngine(config, db, client, rate_limiter, logger)

    if not config.upstox_access_token:
        logger.warning("No UPSTOX_ACCESS_TOKEN — cannot run live cycle test.")
        db.close()
        return

    if not client.validate_token():
        logger.error("Upstox token invalid/expired.")
        db.close()
        return

    signals = engine.run_cycle()
    print_section("SELF-TEST COMPLETE")
    print_kv_table({
        "spot": signals.get("spot"),
        "vix": signals.get("vix"),
        "vix_regime": signals.get("vix_regime"),
        "vrp": signals.get("vrp"),
        "volatility_condition": signals.get("volatility_condition"),
        "price_regime_15": signals.get("price_regime_15"),
        "adx_15": signals.get("adx_15"),
        "ema_structure": signals.get("ema_structure"),
        "direction": signals.get("direction"),
        "chain_size": signals.get("chain_size"),
    })
    db.close()


if __name__ == "__main__":
    _self_test()