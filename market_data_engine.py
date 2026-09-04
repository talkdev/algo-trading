"""
════════════════════════════════════════════════════════════════════════════
NIFTY INTRADAY OPTIONS ALGO TRADING ENGINE — 2026 PRODUCTION BUILD
FILE 2 of 5 : MARKET DATA ENGINE
════════════════════════════════════════════════════════════════════════════

Save as: market_data_engine.py  (same directory as nifty_algo_core.py)

Implements (from the original module spec):
    Module 0  — data inputs (fetch + normalize)
    Module 1  — pre-session assessment (VIX regime, gap, day mode) — rechecked
                every 30 minutes, not just once
    Module 2  — opening range (computed once at/after 09:30)
    Module 3  — Parkinson realized volatility (cached once/day) + VRP
    Module 4  — volatility condition classification
    Module 5  — trend condition (ADX on 5-min bars + OR combination)
    Module 6  — directional bias (VWAP / PCR / skew)

ALL state lives in SQLite (session_state table) — this engine can be killed
and restarted mid-day and will resume from the DB rather than losing state.

Every cycle:
    - option_chain_snapshot table gets every strike/leg captured
    - cycle_log table gets every computed value captured
    - full console dashboard is printed

NOTE ON TRADING WINDOW: your instruction was "10 AM till 3 PM" — this is
respected as the base window (Config.trading_window_start/last_entry/
hard_exit_time from File 1). A configurable, loggable RISK OVERRIDE tightens
this further on Tuesday (0DTE gamma risk) unless you disable it — see
TUESDAY_EARLY_EXIT_ENABLED below.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from typing import Optional

from nifty_algo_core import (
    Config, Database, RateLimiter, UpstoxClient,
    now_ist, today_ist, parse_ist_timestamp, IST,
    INSTRUMENT_KEY_NIFTY_SPOT, INSTRUMENT_KEY_INDIA_VIX,
    print_section, print_kv_table,
    load_env_file, ENV_FILE, BASE_DIR,
    load_config, setup_logging,
)

# ─────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL CONFIG (optional extras not in File 1's Config dataclass)
# ─────────────────────────────────────────────────────────────────────────
_extra_env = load_env_file(ENV_FILE)

# NOT hardcoded — supply your own verified GIFT Nifty instrument key here.
# If blank, gap assessment gracefully defaults to "SMALL / FLAT" (matches
# the original spec's own fallback behaviour for unavailable GIFT Nifty data).
GIFT_NIFTY_KEY = _extra_env.get("GIFT_NIFTY_INSTRUMENT_KEY", "").strip()

# Risk override: tighten entry/exit window further on Tuesday (0DTE gamma
# risk) beyond the base 10:00-15:00 window. Set to false in env.txt to
# disable and use the flat window every day including expiry day.
TUESDAY_EARLY_EXIT_ENABLED = _extra_env.get(
    "TUESDAY_EARLY_EXIT_ENABLED", "true"
).strip().lower() in ("1", "true", "yes", "on")

DEFAULT_HOLIDAYS_FILE = BASE_DIR / "nse_holidays.json"


def load_nse_holidays(path: Path = DEFAULT_HOLIDAYS_FILE) -> set:
    """
    NOT hardcoded with any dates. Populate this file yourself with verified
    NSE trading holiday dates (["2026-01-26", "2026-08-15", ...]).
    """
    if not path.exists():
        path.write_text("[]\n", encoding="utf-8")
        print(f"[SETUP] {path} did not exist — created empty holiday list. "
              f"Populate it with verified NSE holiday dates for accurate "
              f"next-trading-day / event-day calculations.")
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        print(f"[WARNING] {path} is not valid JSON — treating as empty.")
        return set()


def ensure_column(db: Database, table: str, column: str, coltype: str) -> None:
    """Idempotent, safe-to-call-every-startup schema migration helper."""
    cols = db.query(f"PRAGMA table_info({table})")
    existing = {c["name"] for c in cols}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


SESSION_STATE_EXTRA_COLUMNS = [
    ("or_computed", "INTEGER DEFAULT 0"),
    ("session_initialized", "INTEGER DEFAULT 0"),
    ("vix_regime_last_checked", "TEXT"),
    ("prev_spot", "REAL"),
    ("prev_vix", "REAL"),
    ("parkinson_rv_pct", "REAL"),
    ("parkinson_rv_computed_date", "TEXT"),
    ("vwap_valid", "INTEGER DEFAULT 0"),
    ("expiry_last_checked", "TEXT"),
]

# ─────────────────────────────────────────────────────────────────────────
# MODULE 5 LOOKUP TABLE (OR condition × ADX condition -> trend classification)
# ─────────────────────────────────────────────────────────────────────────
TREND_LOOKUP = {
    ("VERY_NARROW", "FLAT"): ("RANGE_BOUND", 2), ("VERY_NARROW", "WEAK"): ("RANGE_BOUND", 2),
    ("VERY_NARROW", "MODERATE"): ("MILD_RANGE", 1), ("VERY_NARROW", "STRONG"): ("MILD_RANGE", 1),
    ("VERY_NARROW", "VERY_STRONG"): ("MILD_TREND", 0),
    ("VERY_NARROW", "EARLY_SESSION"): ("RANGE_ASSUMED", 1),
    ("VERY_NARROW", "INSUFFICIENT_DATA"): ("RANGE_ASSUMED", 1),

    ("NARROW", "FLAT"): ("RANGE_BOUND", 2), ("NARROW", "WEAK"): ("RANGE_BOUND", 2),
    ("NARROW", "MODERATE"): ("MILD_RANGE", 1), ("NARROW", "STRONG"): ("MILD_TREND", 0),
    ("NARROW", "VERY_STRONG"): ("TRENDING", -1),
    ("NARROW", "EARLY_SESSION"): ("RANGE_ASSUMED", 1),
    ("NARROW", "INSUFFICIENT_DATA"): ("RANGE_ASSUMED", 1),

    ("MODERATE", "FLAT"): ("MILD_RANGE", 1), ("MODERATE", "WEAK"): ("MILD_RANGE", 1),
    ("MODERATE", "MODERATE"): ("UNCERTAIN", 0), ("MODERATE", "STRONG"): ("MILD_TREND", 0),
    ("MODERATE", "VERY_STRONG"): ("TRENDING", -1),
    ("MODERATE", "EARLY_SESSION"): ("UNCERTAIN", 0),
    ("MODERATE", "INSUFFICIENT_DATA"): ("UNCERTAIN", 0),

    ("WIDE", "FLAT"): ("UNCERTAIN", 0), ("WIDE", "WEAK"): ("MILD_TREND", 0),
    ("WIDE", "MODERATE"): ("TRENDING", -1), ("WIDE", "STRONG"): ("TRENDING", -1),
    ("WIDE", "VERY_STRONG"): ("STRONG_TREND", -2),
    ("WIDE", "EARLY_SESSION"): ("MILD_TREND", 0),
    ("WIDE", "INSUFFICIENT_DATA"): ("MILD_TREND", 0),

    ("VERY_WIDE", "FLAT"): ("MILD_TREND", 0), ("VERY_WIDE", "WEAK"): ("TRENDING", -1),
    ("VERY_WIDE", "MODERATE"): ("STRONG_TREND", -2), ("VERY_WIDE", "STRONG"): ("STRONG_TREND", -2),
    ("VERY_WIDE", "VERY_STRONG"): ("STRONG_TREND", -2),
    ("VERY_WIDE", "EARLY_SESSION"): ("TRENDING", -1),
    ("VERY_WIDE", "INSUFFICIENT_DATA"): ("TRENDING", -1),
}


# ─────────────────────────────────────────────────────────────────────────
# MARKET DATA ENGINE
# ─────────────────────────────────────────────────────────────────────────
class MarketDataEngine:
    """
    Owns: live data ingestion, all signal computation, and persistence of
    every calculated value. Holds NOTHING that matters across a restart
    only in memory — self.state mirrors the session_state DB row and is
    saved back after every mutation.
    """

    def __init__(self, config: Config, db: Database, client: UpstoxClient,
                 rate_limiter: RateLimiter, logger):
        self.config = config
        self.db = db
        self.client = client
        self.rate_limiter = rate_limiter
        self.logger = logger
        self.holidays = load_nse_holidays()

        self._ensure_intraday_candles_table()
        for col, coltype in SESSION_STATE_EXTRA_COLUMNS:
            ensure_column(self.db, "session_state", col, coltype)

        self.state = self._load_or_init_session_state()
        self.last_chain: dict = {}
        self.last_chain_expiry = None

    # ─────────────────────────────────────────────────────────────────
    # SESSION STATE (persisted)
    # ─────────────────────────────────────────────────────────────────
    def _ensure_intraday_candles_table(self) -> None:
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS intraday_candles "
            "(candle_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "trading_date TEXT, candle_time TEXT, interval_min INTEGER DEFAULT 1, "
            "open REAL, high REAL, low REAL, close REAL, volume INTEGER, source TEXT)"
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_intraday_candles_unique "
            "ON intraday_candles(trading_date, candle_time, interval_min)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_intraday_candles_date "
            "ON intraday_candles(trading_date)"
        )

    def _persist_candles_to_db(self, bars_1m: list, trading_date: str) -> None:
        if not bars_1m:
            return
        rows = []
        for b in bars_1m:
            if b.get("timestamp") is None:
                continue
            rows.append((
                trading_date,
                b["timestamp"].isoformat(),
                1,
                b["open"], b["high"], b["low"], b["close"],
                b.get("volume", 0),
                "upstox_intraday",
            ))
        if rows:
            try:
                self.db.executemany(
                    "INSERT OR IGNORE INTO intraday_candles "
                    "(trading_date, candle_time, interval_min, open, high, low, close, volume, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    rows,
                )
            except Exception as e:
                self.logger.warning(f"Could not persist intraday candles: {e}")

    def _load_candles_from_db_today(self, trading_date: str) -> list:
        try:
            rows = self.db.query(
                "SELECT candle_time, open, high, low, close, volume FROM intraday_candles "
                "WHERE trading_date=? AND interval_min=1 ORDER BY candle_time",
                (trading_date,),
            )
            bars = []
            for r in rows:
                ts = parse_ist_timestamp(r["candle_time"])
                if ts is None:
                    continue
                bars.append({
                    "timestamp": ts,
                    "open": r["open"], "high": r["high"],
                    "low": r["low"], "close": r["close"],
                    "volume": r["volume"] or 0,
                })
            return bars
        except Exception as e:
            self.logger.warning(f"Could not load candles from DB: {e}")
            return []

    def _load_historical_candles_from_db(self, n: int = 5) -> dict:
        today = today_ist()
        try:
            date_rows = self.db.query(
                "SELECT DISTINCT trading_date FROM intraday_candles "
                "WHERE trading_date < ? AND interval_min=1 "
                "ORDER BY trading_date DESC LIMIT ?",
                (today.isoformat(), n),
            )
            result = {}
            for dr in date_rows:
                date_str = dr["trading_date"]
                candle_rows = self.db.query(
                    "SELECT candle_time, open, high, low, close, volume "
                    "FROM intraday_candles WHERE trading_date=? AND interval_min=1 "
                    "ORDER BY candle_time",
                    (date_str,),
                )
                bars = []
                for cr in candle_rows:
                    ts = parse_ist_timestamp(cr["candle_time"])
                    if ts is None:
                        continue
                    bars.append({
                        "timestamp": ts,
                        "open": cr["open"], "high": cr["high"],
                        "low": cr["low"], "close": cr["close"],
                        "volume": cr["volume"] or 0,
                    })
                if bars:
                    from datetime import date as _date
                    result[_date.fromisoformat(date_str)] = bars
            return result
        except Exception as e:
            self.logger.warning(f"Could not load historical candles from DB: {e}")
            return {}

    def _load_or_init_session_state(self) -> dict:
        today_str = today_ist().isoformat()
        row = self.db.query_one("SELECT * FROM session_state WHERE trading_date=?", (today_str,))
        if row is not None:
            actual_entries = self.db.query_one(
                "SELECT COUNT(*) as cnt FROM positions WHERE trading_date=? "
                "AND status IN ('OPEN', 'CLOSED')",
                (today_str,),
            )
            if actual_entries:
                db_count = actual_entries["cnt"]
                if row.get("entry_count", 0) != db_count:
                    self.logger.info(
                        f"Correcting entry_count on load: "
                        f"{row.get('entry_count', 0)} -> {db_count} "
                        f"(based on actual OPEN+CLOSED positions in DB)"
                    )
                    row["entry_count"] = db_count
                    self.db.update(
                        "session_state",
                        {"entry_count": db_count},
                        {"trading_date": today_str},
                    )
            for boolcol in ("daily_halted", "circuit_breaker_suspected", "vix_spike_detected",
                             "event_announced", "or_computed", "session_initialized", "vwap_valid",
                             "paper_trade_mode", "stop_at_breakeven", "stop_moved_to_25pct"):
                if boolcol in row and row[boolcol] is not None:
                    row[boolcol] = bool(row[boolcol])
            self.logger.info(f"Loaded existing session_state for {today_str} "
                              f"from DB (mid-day restart recovery).")
            return row

        defaults = {
            "trading_date": today_str, "day_mode": "NORMAL", "vix_regime": "UNKNOWN",
            "gap_size": "SMALL", "gap_direction": "FLAT", "day_label": None,
            "or_high": None, "or_low": None, "or_width": None, "or_condition": None,
            "entry_start": self.config.trading_window_start.strftime("%H:%M"),
            "entry_end": self.config.trading_window_last_entry.strftime("%H:%M"),
            "hard_exit_time": self.config.hard_exit_time.strftime("%H:%M"),
            "stop_multiplier": 2.0, "size_multiplier": 1.0, "wing_width": 150,
            "entry_count": 0, "reentry_count": 0, "daily_halted": False, "consecutive_stops": 0,
            "last_stop_time": None, "last_stop_reason": None, "last_entry_time": None,
            "actual_expiry": None, "actual_dte": None,
            "opening_iv": None, "opening_pcr": None,
            "current_capital": self.config.starting_capital, "daily_pnl": 0.0,
            "circuit_breaker_suspected": False, "vix_spike_detected": False, "event_announced": False,
            "paper_trade_mode": self.config.paper_trade_mode,
            "or_computed": False, "session_initialized": False, "vix_regime_last_checked": None,
            "prev_spot": None, "prev_vix": None, "parkinson_rv_pct": None,
            "parkinson_rv_computed_date": None, "vwap_valid": False, "expiry_last_checked": None,
            "created_at": now_ist().isoformat(), "updated_at": now_ist().isoformat(),
        }
        insert_row = {k: (int(v) if isinstance(v, bool) else v) for k, v in defaults.items()}
        self.db.insert("session_state", insert_row)
        self.logger.info(f"Initialized fresh session_state for {today_str}.")
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
                f"New trading day detected: {today_str} — "
                f"previous day was {self.state.get('trading_date')}. "
                f"Initializing fresh session state for new day."
            )
            self._close_any_stale_prior_day_positions(self.state.get("trading_date"))
            self.state = self._load_or_init_session_state()
            self.last_chain: dict = {}
            self.last_chain_expiry = None

    # ─────────────────────────────────────────────────────────────────
    # RAW DATA FETCH
    # ─────────────────────────────────────────────────────────────────
    def fetch_spot_and_vix(self) -> tuple[Optional[float], Optional[float]]:
        try:
            data = self.client.get_ltp([INSTRUMENT_KEY_NIFTY_SPOT, INSTRUMENT_KEY_INDIA_VIX])
        except Exception as e:
            self.logger.error(f"Failed to fetch spot/VIX: {e}")
            return self.state.get("prev_spot"), self.state.get("prev_vix")

        spot, vix = None, None
        for key, v in data.items():
            ltp = v.get("last_price")
            key_upper = str(key).upper()
            if "NIFTY 50" in key_upper or ("NIFTY" in key_upper and "VIX" not in key_upper):
                spot = float(ltp) if ltp is not None else None
            elif "VIX" in key_upper:
                vix = float(ltp) if ltp is not None else None

        if spot is not None and not (10000 < spot < 50000):
            self.logger.warning(f"Spot {spot} outside valid range [10000,50000] — "
                                 f"discarding, using last known value")
            spot = self.state.get("prev_spot")
        if vix is not None and not (7.0 < vix < 60.0):
            self.logger.warning(f"VIX {vix} outside valid range [5,90] — "
                                 f"discarding, using last known value")
            vix = self.state.get("prev_vix")
        return spot, vix

    def _check_circuit_breaker_and_vix_spike(self, spot, vix) -> tuple[bool, bool]:
        prev_spot = self.state.get("prev_spot")
        prev_vix = self.state.get("prev_vix")
        circuit = False
        vix_spike = self.state.get("vix_spike_detected", False)

        if prev_spot and prev_spot > 0 and spot:
            pct = abs(spot - prev_spot) / prev_spot
            if pct > 0.05:
                circuit = True
                self.logger.warning(f"CIRCUIT BREAKER SUSPECTED: spot moved "
                                     f"{pct*100:.2f}% in one cycle")
        if prev_vix and prev_vix > 0 and vix:
            chg = (vix - prev_vix) / prev_vix * 100.0
            if chg > 15.0:
                vix_spike = True
                self.logger.warning(f"VIX SPIKE: {prev_vix:.1f} -> {vix:.1f} ({chg:.1f}%)")
            elif chg < -10.0:
                vix_spike = False

        self.state["prev_spot"] = spot
        self.state["prev_vix"] = vix
        return circuit, vix_spike

    @staticmethod
    def _normalize_candle_row(row, ts: Optional[datetime] = None) -> dict:
        # Expected Upstox convention: [timestamp, open, high, low, close, volume, oi]
        # VERIFY against live API response — defensive fallback to dict form included.
        try:
            if isinstance(row, (list, tuple)):
                ts = ts or parse_ist_timestamp(row[0])
                return {"timestamp": ts, "open": float(row[1]), "high": float(row[2]),
                        "low": float(row[3]), "close": float(row[4]),
                        "volume": int(row[5]) if len(row) > 5 else 0}
            ts = ts or parse_ist_timestamp(row.get("timestamp"))
            return {"timestamp": ts, "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0), "low": float(row.get("low", 0) or 0),
                    "close": float(row.get("close", 0) or 0), "volume": int(row.get("volume", 0) or 0)}
        except (ValueError, TypeError, IndexError):
            return {"timestamp": None, "open": 0.0, "high": 0.0, "low": 0.0,
                    "close": 0.0, "volume": 0}

    @staticmethod
    def _resample_to_5min(bars_1m: list) -> list:
        if not bars_1m:
            return []
        buckets: dict = {}
        for b in bars_1m:
            ts = b["timestamp"]
            if ts is None:
                continue
            bucket_minute = (ts.minute // 5) * 5
            bucket_key = ts.replace(minute=bucket_minute, second=0, microsecond=0)
            buckets.setdefault(bucket_key, []).append(b)

        out = []
        for bucket_ts in sorted(buckets.keys()):
            group = buckets[bucket_ts]
            out.append({
                "timestamp": bucket_ts,
                "open": group[0]["open"], "close": group[-1]["close"],
                "high": max(g["high"] for g in group), "low": min(g["low"] for g in group),
                "volume": sum(g["volume"] for g in group),
            })
        return out

    def fetch_5min_candles_today(self) -> list:
        try:
            raw = self.client.get_intraday_candles(INSTRUMENT_KEY_NIFTY_SPOT, "1minute")
        except Exception as e:
            self.logger.error(f"Failed to fetch today's intraday candles: {e}")
            return []

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
            if b["volume"] == 0 and b["high"] == b["low"] == b["open"] == b["close"]:
                continue
            bars_1m.append(b)
        bars_1m.sort(key=lambda x: x["timestamp"])
        trading_date = today_ist().isoformat()
        self._persist_candles_to_db(bars_1m, trading_date)
        db_bars = self._load_candles_from_db_today(trading_date)
        merged = {}
        for b in db_bars:
            if b["timestamp"] is not None:
                merged[b["timestamp"]] = b
        for b in bars_1m:
            if b["timestamp"] is not None:
                merged[b["timestamp"]] = b
        session_bars = [
            b for b in sorted(merged.values(), key=lambda x: x["timestamp"])
            if dtime(9, 15) <= b["timestamp"].time() <= dtime(15, 29)
        ]
        return self._resample_to_5min(session_bars)

    def _get_prev_close(self) -> Optional[float]:
        today = today_ist()
        from_date = (today - timedelta(days=10)).isoformat()
        to_date = (today - timedelta(days=1)).isoformat()
        try:
            raw = self.client.get_historical_candles(INSTRUMENT_KEY_NIFTY_SPOT, "day", from_date, to_date)
        except Exception as e:
            self.logger.warning(f"Failed to fetch previous close: {e}")
            return None
        bars = [self._normalize_candle_row(r) for r in raw]
        bars = [b for b in bars if b["timestamp"] is not None]
        if not bars:
            return None
        bars.sort(key=lambda b: b["timestamp"])
        return bars[-1]["close"]

    def _fetch_last_n_complete_sessions_5min(self, n: int = 5) -> dict:
        today = today_ist()
        from_date = (today - timedelta(days=n * 3 + 7)).isoformat()  # buffer for weekends/holidays
        to_date = (today - timedelta(days=1)).isoformat()
        raw = self.client.get_historical_candles(
            INSTRUMENT_KEY_NIFTY_SPOT, "1minute", from_date, to_date
        )
        by_session: dict = {}
        for row in raw:
            b = self._normalize_candle_row(row)
            if b["timestamp"] is None:
                continue
            session_date = b["timestamp"].date()
            if session_date == today or session_date.isoformat() in self.holidays:
                continue
            if b["high"] <= 0 or b["low"] <= 0 or b["high"] < b["low"]:
                continue
            by_session.setdefault(session_date, []).append(b)

        complete_sessions = sorted(by_session.keys(), reverse=True)[:n]
        result = {}
        for sd in complete_sessions:
            bars_1m = sorted(by_session[sd], key=lambda b: b["timestamp"])
            result[sd] = self._resample_to_5min(bars_1m)
        return result

    # ─────────────────────────────────────────────────────────────────
    # MODULE 3: PARKINSON RV (cached once per trading day)
    # ─────────────────────────────────────────────────────────────────
    def compute_intraday_parkinson_rv(self, candles_today: list) -> Optional[float]:
        valid = [b for b in candles_today if b["high"] > b["low"] and b["high"] > 0]
        if len(valid) < 6:
            return None
        log_hl_sq = [math.log(b["high"] / b["low"]) ** 2 for b in valid]
        park_const = 1.0 / (4.0 * math.log(2.0))
        variance_per_bar = park_const * (sum(log_hl_sq) / len(log_hl_sq))
        annual_variance = variance_per_bar * (75.0 * 252.0)
        rv = math.sqrt(annual_variance)
        if rv < 0.02 or rv > 0.80:
            return None
        return rv

    def compute_parkinson_rv(self, vix: Optional[float], candles_today: Optional[list] = None) -> tuple[Optional[float], str]:
        today_str = today_ist().isoformat()
        if candles_today and len(candles_today) >= 12:
            rolling_bars = candles_today[-30:]
            rolling_rv = self.compute_intraday_parkinson_rv(rolling_bars)
            if rolling_rv is not None:
                self.state["parkinson_rv_pct"] = rolling_rv
                self.state["parkinson_rv_computed_date"] = today_str
                return rolling_rv, "rolling_intraday"
        if (self.state.get("parkinson_rv_computed_date") == today_str
                and self.state.get("parkinson_rv_pct") is not None):
            return self.state["parkinson_rv_pct"], "cached"

        rv, source = self._compute_parkinson_rv_fresh(vix)
        self.state["parkinson_rv_pct"] = rv
        self.state["parkinson_rv_computed_date"] = today_str
        return rv, source

    def _compute_parkinson_rv_fresh(self, vix: Optional[float]) -> tuple[float, str]:
        try:
            session_bars = self._fetch_last_n_complete_sessions_5min(n=5)
            if not session_bars:
                self.logger.info("Parkinson RV: API returned no bars, trying DB cache")
                session_bars = self._load_historical_candles_from_db(n=5)
        except Exception as e:
            self.logger.warning(f"Parkinson RV: historical fetch failed ({e}), trying DB cache")
            session_bars = self._load_historical_candles_from_db(n=5)

        all_bars = [b for bars in session_bars.values() for b in bars]
        valid_bars = [b for b in all_bars
                      if b["high"] > 0 and b["low"] > 0 and b["high"] >= b["low"] and b["volume"] > 0]

        if len(valid_bars) < 30:
            return self._parkinson_fallback(vix), "vix_proxy_insufficient_bars"

        log_hl_sq = []
        for b in valid_bars:
            if b["high"] > b["low"]:
                log_hl_sq.append(math.log(b["high"] / b["low"]) ** 2)

        n = len(log_hl_sq)
        if n < 10:
            return self._parkinson_fallback(vix), "vix_proxy_too_few_valid"

        park_const = 1.0 / (4.0 * math.log(2.0))
        variance_per_bar = park_const * (sum(log_hl_sq) / n)
        annual_variance = variance_per_bar * (75.0 * 252.0)
        rv = math.sqrt(annual_variance)

        if rv < 0.03 or rv > 0.50:
            return self._parkinson_fallback(vix), "vix_proxy_out_of_range"

        ratio_check_removed = True
        return rv, f"parkinson_{n}bars_{len(session_bars)}sessions"

    def _parkinson_fallback(self, vix: Optional[float]) -> float:
        if vix and vix > 0:
            return (vix / 100.0) * 0.65
        return 0.065

    # ─────────────────────────────────────────────────────────────────
    # OPTION CHAIN: expiry discovery + fetch + normalization
    # ─────────────────────────────────────────────────────────────────
    def discover_active_expiry(self, prefer_dte_min: int = 4) -> tuple[Optional[date], Optional[int]]:
        try:
            contracts = self.client.get_option_contracts(INSTRUMENT_KEY_NIFTY_SPOT)
        except Exception as e:
            self.logger.error(f"Failed to discover option contracts: {e}")
            return None, None

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

        if not future:
            return None, None

        future.sort(key=lambda x: x[0])

        if is_0dte_window:
            zero_dte = [f for f in future if f[0] == 0]
            if zero_dte:
                return zero_dte[0][1], zero_dte[0][0]

        preferred = [f for f in future if f[0] >= prefer_dte_min]
        if preferred:
            return preferred[0][1], preferred[0][0]

        return future[0][1], future[0][0]

    def _get_active_expiry(self) -> tuple[Optional[date], Optional[int]]:
        last_checked = self.state.get("expiry_last_checked")
        cached_expiry = self.state.get("actual_expiry")
        should_refresh = cached_expiry is None
        if last_checked and not should_refresh:
            try:
                last_dt = datetime.fromisoformat(last_checked)
                _elapsed = (now_ist() - last_dt).total_seconds()
                _now_t = now_ist().time()
                _is_tuesday = today_ist().weekday() == 1
                _in_0dte_window = _is_tuesday and dtime(12, 0) <= _now_t < dtime(14, 0)
                expiry_cache_ttl_tuesday = True
                _ttl = 300 if _in_0dte_window else 1800
                should_refresh = _elapsed > _ttl
            except Exception:
                should_refresh = True

        if should_refresh:
            expiry, dte = self.discover_active_expiry()
            if expiry:
                self.state["actual_expiry"] = expiry.isoformat()
                self.state["actual_dte"] = dte
                self.state["expiry_last_checked"] = now_ist().isoformat()
                self.logger.info(f"Active expiry: {expiry} (DTE={dte})")
            else:
                self.logger.error("Could not discover active expiry from option chain")

        exp_str = self.state.get("actual_expiry")
        if exp_str is None:
            return None, None
        expiry_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (expiry_date - today_ist()).days
        self.state["actual_dte"] = dte
        return expiry_date, dte

    @staticmethod
    def _normalize_option_leg(raw: dict) -> dict:
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
            iv = iv / 100.0  # normalize percentage -> decimal at ingestion
        return {
            "instrument_key": raw.get("instrument_key"),
            "bid": _f(md, "bid_price", 0.0), "ask": _f(md, "ask_price", 0.0),
            "ltp": _f(md, "ltp", 0.0),
            "oi": int(_f(md, "oi", 0)), "volume": int(_f(md, "volume", 0)),
            "iv": iv, "delta": _f(greeks, "delta", 0.0), "gamma": _f(greeks, "gamma", 0.0),
            "theta": _f(greeks, "theta", 0.0), "vega": _f(greeks, "vega", 0.0),
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

    def fetch_option_chain(self, expiry_date: date) -> dict:
        try:
            raw = self.client.get_option_chain(INSTRUMENT_KEY_NIFTY_SPOT, expiry_date.isoformat())
        except Exception as e:
            self.logger.error(f"Failed to fetch option chain for {expiry_date}: {e}")
            return {}
        chain = self._parse_chain_response(raw)
        if len(chain) < 10:
            self.logger.warning(f"Option chain for {expiry_date} has only {len(chain)} strikes")
        return chain

    # ─────────────────────────────────────────────────────────────────
    # MODULE 0 SECTION B: derived computations
    # ─────────────────────────────────────────────────────────────────
    def compute_atm_iv(self, chain: dict, spot: Optional[float]) -> Optional[float]:
        if not chain or spot is None:
            return None
        step = self.config.nifty_strike_step
        atm = round(spot / step) * step
        if atm not in chain:
            atm = min(chain.keys(), key=lambda k: abs(k - spot))
        leg = chain.get(atm, {})
        call, put = leg.get("call", {}), leg.get("put", {})
        call_iv, put_iv = call.get("iv", 0.0), put.get("iv", 0.0)
        call_oi, put_oi = call.get("oi", 0), put.get("oi", 0)
        if call_iv <= 0 and put_iv <= 0:
            return None
        total_oi = call_oi + put_oi
        if total_oi > 0:
            atm_iv = (call_iv * call_oi + put_iv * put_oi) / total_oi
        elif call_iv > 0 and put_iv > 0:
            atm_iv = (call_iv + put_iv) / 2.0
        elif call_iv > 0:
            atm_iv = call_iv
        elif put_iv > 0:
            atm_iv = put_iv
        else:
            atm_iv_zero_oi_fallback = True
            return None
        atm_iv_zero_oi_fallback = False
        if atm_iv < 0.05 or atm_iv > 0.80:
            self.logger.warning(f"ATM IV {atm_iv:.4f} outside valid range [0.05,0.80] — discarding")
            return None
        return atm_iv

    def compute_pcr(self, chain: dict, spot: Optional[float] = None) -> Optional[float]:
        if spot is not None and spot > 0:
            total_put_oi = sum(
                legs.get("put", {}).get("oi", 0)
                for strike, legs in chain.items() if strike > spot
            )
            total_call_oi = sum(
                legs.get("call", {}).get("oi", 0)
                for strike, legs in chain.items() if strike < spot
            )
        else:
            total_put_oi = sum(legs.get("put", {}).get("oi", 0) for legs in chain.values())
            total_call_oi = sum(legs.get("call", {}).get("oi", 0) for legs in chain.values())
        if total_call_oi <= 0:
            return None
        pcr = total_put_oi / total_call_oi
        if pcr < 0.3 or pcr > 4.0:
            self.logger.warning(f"PCR {pcr:.3f} outside valid range [0.3,4.0] — discarding")
            return None
        return pcr

    def compute_vwap(self, candles_today: list) -> tuple[Optional[float], bool]:
        if len(candles_today) < 3:
            return None, False
        cum_pv, cum_vol = 0.0, 0.0
        for b in candles_today:
            typical = (b["high"] + b["low"] + b["close"]) / 3.0
            cum_pv += typical * b["volume"]
            cum_vol += b["volume"]
        if cum_vol > 0:
            return cum_pv / cum_vol, True
        total_bars = len(candles_today)
        if total_bars < 3:
            return None, False
        equal_pv = sum((b["high"] + b["low"] + b["close"]) / 3.0 for b in candles_today)
        return equal_pv / total_bars, True

    def _find_by_delta(self, chain: dict, opt_type: str, target: float, tolerance: float = 0.05) -> Optional[float]:
        best_iv, best_diff = None, float("inf")
        for strike, legs in chain.items():
            leg = legs.get(opt_type, {})
            delta, iv = abs(leg.get("delta", 0.0)), leg.get("iv", 0.0)
            if delta <= 0 or iv <= 0:
                continue
            diff = abs(delta - target)
            if diff < best_diff:
                best_diff, best_iv = diff, iv
        if best_iv is None or best_diff > tolerance:
            return None
        return best_iv

    def compute_25d_ivs(self, chain: dict) -> tuple[Optional[float], Optional[float]]:
        put_iv = self._find_by_delta(chain, "put", 0.25, tolerance=0.05)
        call_iv = self._find_by_delta(chain, "call", 0.25, tolerance=0.05)
        skew_tolerance_fallback = False
        if put_iv is None:
            put_iv = self._find_by_delta(chain, "put", 0.25, tolerance=0.08)
            skew_tolerance_fallback = True
        if call_iv is None:
            call_iv = self._find_by_delta(chain, "call", 0.25, tolerance=0.08)
            skew_tolerance_fallback = True
        return put_iv, call_iv

    def compute_skew_ratio(self, put_iv: Optional[float], call_iv: Optional[float]) -> Optional[float]:
        if put_iv is None or call_iv is None or put_iv <= 0.02 or call_iv <= 0.02:
            return None
        skew = put_iv / call_iv
        if skew < 0.5 or skew > 3.0:
            return None
        return skew

    def _get_atm_greeks(self, chain: dict, spot: Optional[float]) -> dict:
        if not chain or spot is None:
            return {}
        step = self.config.nifty_strike_step
        atm = round(spot / step) * step
        if atm not in chain:
            atm = min(chain.keys(), key=lambda k: abs(k - spot))
        return chain.get(atm, {})

    # ─────────────────────────────────────────────────────────────────
    # MODULE 1: PRE-SESSION ASSESSMENT (re-run every 30 minutes)
    # ─────────────────────────────────────────────────────────────────
    def _compute_base_regime(self, vix: float) -> str:
        if vix < 10.5: return "SUPPRESSED"
        if vix < 14.0: return "LOW"
        if vix < 18.0: return "NORMAL"
        if vix < 24.0: return "ELEVATED"
        return "HIGH"

    def _compute_regime_with_hysteresis(self, vix: float, prev_regime: str) -> str:
        bands = {
            "SUPPRESSED": (None, 11.0), "LOW": (10.0, 14.5), "NORMAL": (13.5, 18.5),
            "ELEVATED": (17.5, 24.5), "HIGH": (23.5, None),
        }
        if prev_regime in bands:
            lo, hi = bands[prev_regime]
            if (lo is None or vix > lo) and (hi is None or vix < hi):
                return prev_regime
        return self._compute_base_regime(vix)

    def _next_trading_day(self, from_date: date) -> Optional[date]:
        d = from_date + timedelta(days=1)
        for _ in range(10):
            if d.weekday() < 5 and d.isoformat() not in self.holidays:
                return d
            d += timedelta(days=1)
        return None

    def _determine_day_mode(self, today: date) -> tuple[str, Optional[str]]:
        events = self.config.high_impact_events
        today_str = today.isoformat()
        if today_str in events:
            return "EVENT", events[today_str]
        next_day = self._next_trading_day(today)
        if next_day and next_day.isoformat() in events:
            return "PRE_EVENT", events[next_day.isoformat()]
        return "NORMAL", None

    def _assess_gap(self) -> None:
        current_time = now_ist().time()
        if current_time >= dtime(9, 30):
            return  # gap assessment only meaningful pre-market

        if not GIFT_NIFTY_KEY:
            self.state["gap_size"], self.state["gap_direction"] = "SMALL", "FLAT"
            if not self.config.paper_trade_mode:
                self.state["daily_halted"] = True
                self.logger.critical("GIFT_NIFTY_INSTRUMENT_KEY is not configured and PAPER_TRADE_MODE is false. Gap risk cannot be assessed before live trading. Halting trading for today.")
            return

        try:
            ltp_data = self.client.get_ltp([GIFT_NIFTY_KEY])
            gift_ltp = None
            for v in ltp_data.values():
                gift_ltp = v.get("last_price")
                break
        except Exception as e:
            self.logger.warning(f"GIFT Nifty fetch failed: {e}")
            gift_ltp = None

        prev_close = self._get_prev_close()
        if gift_ltp is None or not prev_close or prev_close <= 0:
            self.state["gap_size"], self.state["gap_direction"] = "SMALL", "FLAT"
            return

        if abs(gift_ltp - prev_close) / prev_close > 0.05:
            self.logger.warning("GIFT Nifty gap > 5% vs prev close — treating as unreliable")
            self.state["gap_size"], self.state["gap_direction"] = "SMALL", "FLAT"
            return

        gap_pct = (gift_ltp - prev_close) / prev_close * 100.0
        self.state["gap_direction"] = "UP" if gap_pct > 0.05 else ("DOWN" if gap_pct < -0.05 else "FLAT")
        abs_gap = abs(gap_pct)
        if abs_gap < 0.30: self.state["gap_size"] = "SMALL"
        elif abs_gap < 0.70: self.state["gap_size"] = "MODERATE"
        elif abs_gap < 1.00: self.state["gap_size"] = "LARGE"
        else: self.state["gap_size"] = "VERY_LARGE"
        if abs_gap >= 1.00:
            self.state["gap_fade_opportunity"] = True
            self.logger.info(f"GAP FADE OPPORTUNITY: gap={gap_pct:.2f}% — large gap favours mean-reversion condor")
        else:
            self.state["gap_fade_opportunity"] = False

    def _apply_day_of_week_params(self, today: date) -> None:
        labels = {0: "MONDAY", 1: "TUESDAY", 2: "WEDNESDAY", 3: "THURSDAY", 4: "FRIDAY"}
        day_label = labels.get(today.weekday(), "WEEKEND")
        self.state["day_label"] = day_label

        vix_regime = self.state.get("vix_regime", "NORMAL")
        wing_map = {"SUPPRESSED": 150, "LOW": 150, "NORMAL": 150, "ELEVATED": 200, "HIGH": 250}
        self.state["wing_width"] = wing_map.get(vix_regime, 150)

        dow_stop = {"MONDAY": 1.4, "TUESDAY": 1.2, "WEDNESDAY": 1.3, "THURSDAY": 1.4, "FRIDAY": 1.3}
        dow_size = {"MONDAY": 0.50, "TUESDAY": 0.60, "WEDNESDAY": 1.0, "THURSDAY": 1.0, "FRIDAY": 0.90}
        self.state["stop_multiplier"] = dow_stop.get(day_label, 2.0)

        vix_size = {"SUPPRESSED": 0.0, "LOW": 0.75, "NORMAL": 1.0,
                    "ELEVATED": 0.75, "HIGH": 0.50}.get(vix_regime, 1.0)
        combined_size = vix_size * dow_size.get(day_label, 1.0)
        if vix_regime == "ELEVATED":
            combined_size = max(combined_size, 0.50)
            elevated_vix_size_floor = True
        self.state["size_multiplier"] = max(combined_size, 0.25)

        entry_start = self.config.trading_window_start
        last_entry = self.config.trading_window_last_entry
        hard_exit = self.config.hard_exit_time

        if day_label == "TUESDAY" and TUESDAY_EARLY_EXIT_ENABLED:
            last_entry = min(last_entry, dtime(14, 0))
            hard_exit = min(hard_exit, dtime(15, 0))
            self.logger.info(
                f"TUESDAY (0DTE) RISK OVERRIDE: last_entry={last_entry}, "
                f"hard_exit={hard_exit}. Morning entries use next-week contract. "
                f"0DTE entries only 12:30-14:00. "
                f"Set TUESDAY_EARLY_EXIT_ENABLED=false in env.txt to disable."
            )

        gap_size = self.state.get("gap_size", "SMALL")
        if gap_size == "LARGE":
            entry_start = max(entry_start, dtime(10, 30))
        elif gap_size == "VERY_LARGE":
            entry_start = max(entry_start, dtime(11, 0))

        self.state["entry_start"] = entry_start.strftime("%H:%M")
        self.state["entry_end"] = last_entry.strftime("%H:%M")
        self.state["hard_exit_time"] = hard_exit.strftime("%H:%M")

    def _run_pre_session_assessment(self, vix: Optional[float]) -> None:
        if vix is None or vix <= 0:
            vix = 14.0
            self.logger.warning("VIX unavailable at pre-session check, using default 14.0")

        prev_regime = self.state.get("vix_regime", "UNKNOWN")
        new_regime = self._compute_regime_with_hysteresis(vix, prev_regime)
        if prev_regime != "UNKNOWN" and new_regime != prev_regime:
            self.logger.info(f"VIX_REGIME_CHANGED: {prev_regime} -> {new_regime} at VIX={vix:.1f}")
        self.state["vix_regime"] = new_regime

        today = today_ist()
        day_mode, event_name = self._determine_day_mode(today)
        self.state["day_mode"] = day_mode
        if event_name:
            self.logger.info(f"Day mode: {day_mode} ({event_name})")

        self._assess_gap()
        self._apply_day_of_week_params(today)

    def _maybe_run_pre_session_assessment(self, vix: Optional[float]) -> None:
        last_checked = self.state.get("vix_regime_last_checked")
        should_run = last_checked is None
        if last_checked:
            try:
                should_run = (now_ist() - datetime.fromisoformat(last_checked)).total_seconds() > 1800
            except Exception:
                should_run = True
        if should_run:
            self._run_pre_session_assessment(vix)
            self.state["vix_regime_last_checked"] = now_ist().isoformat()

    # ─────────────────────────────────────────────────────────────────
    # MODULE 2: OPENING RANGE
    # ─────────────────────────────────────────────────────────────────
    def compute_opening_range(self, candles_today: list) -> Optional[dict]:
        opening_bars = [b for b in candles_today if dtime(9, 30) <= b["timestamp"].time() <= dtime(9, 44)]
        opening_bars = [b for b in opening_bars if b["high"] > b["low"]]
        opening_bars = [b for b in opening_bars if (b["high"] - b["low"]) <= 1000]
        or_volume_filter_removed = True

        if len(opening_bars) < 2:
            or_flat_open_fallback = True
            spot_now = self.state.get("prev_spot")
            if spot_now and spot_now > 0:
                return {"or_high": spot_now + 10, "or_low": spot_now - 10,
                        "or_width": 20, "or_condition": "VERY_NARROW",
                        "or_score": 2, "partial": True}
            return None

        opening_bars = sorted(opening_bars, key=lambda b: b["timestamp"])[:6]
        or_high = max(b["high"] for b in opening_bars)
        or_low = min(b["low"] for b in opening_bars)
        or_width = or_high - or_low
        if or_width <= 0:
            return None

        _or_mid = (or_high + or_low) / 2.0 if or_high and or_low else 24000.0
        _or_width_pct = (or_width / _or_mid) * 100.0
        if _or_width_pct < 0.20: or_condition, or_score = "VERY_NARROW", 2
        elif _or_width_pct < 0.35: or_condition, or_score = "NARROW", 1
        elif _or_width_pct < 0.55: or_condition, or_score = "MODERATE", 0
        elif _or_width_pct < 0.75: or_condition, or_score = "WIDE", -1
        else: or_condition, or_score = "VERY_WIDE", -2

        return {"or_high": or_high, "or_low": or_low, "or_width": or_width,
                "or_condition": or_condition, "or_score": or_score,
                "partial": len(opening_bars) < 3}

    # ─────────────────────────────────────────────────────────────────
    # MODULE 5: TREND CONDITION (ADX)
    # ─────────────────────────────────────────────────────────────────
    def _compute_adx(self, bars: list, period: int = 14) -> tuple[float, float, float, str]:
        if len(bars) < 3:
            return 0.0, 0.0, 0.0, "UNKNOWN"
        trs, pdms, ndms = [], [], []
        for i in range(1, len(bars)):
            h, l, c, pc = bars[i]["high"], bars[i]["low"], bars[i]["close"], bars[i - 1]["close"]
            trs.append(max(max(h - l, abs(h - pc), abs(l - pc)), 0.01))
            up, down = bars[i]["high"] - bars[i - 1]["high"], bars[i - 1]["low"] - bars[i]["low"]
            pdms.append(up if (up > down and up > 0) else 0.0)
            ndms.append(down if (down > up and down > 0) else 0.0)

        if len(trs) < max(period, 5):
            return 0.0, 0.0, 0.0, "UNKNOWN"

        def wilder_smooth(values, p):
            if len(values) < p:
                return []
            s = [sum(values[:p]) / p]
            for v in values[p:]:
                s.append((s[-1] * (p - 1) + v) / p)
            return s

        atr_s, pdm_s, ndm_s = wilder_smooth(trs, period), wilder_smooth(pdms, period), wilder_smooth(ndms, period)
        if not atr_s or len(atr_s) != len(pdm_s):
            return 0.0, 0.0, 0.0, "UNKNOWN"

        pdi_series, ndi_series = [], []
        for atr, pdm, ndm in zip(atr_s, pdm_s, ndm_s):
            if atr > 1e-10:
                pdi_series.append(100.0 * pdm / atr)
                ndi_series.append(100.0 * ndm / atr)
            else:
                pdi_series.append(0.0)
                ndi_series.append(0.0)

        dx_series = []
        for pdi, ndi in zip(pdi_series, ndi_series):
            s, d = pdi + ndi, abs(pdi - ndi)
            dx_series.append(100.0 * d / s if s > 1e-10 else 0.0)

        adx_series = wilder_smooth(dx_series, period)
        if not adx_series:
            return 0.0, 0.0, 0.0, "UNKNOWN"

        adx_value = max(0.0, min(100.0, adx_series[-1]))
        pdi_last, ndi_last = (pdi_series[-1] if pdi_series else 0.0), (ndi_series[-1] if ndi_series else 0.0)
        direction = "BULLISH" if pdi_last > ndi_last else ("BEARISH" if ndi_last > pdi_last else "NEUTRAL")
        return adx_value, pdi_last, ndi_last, direction

    def _classify_adx(self, adx_value: float) -> tuple[str, int]:
        if adx_value < 20: return "FLAT", 2
        if adx_value < 25: return "WEAK", 1
        if adx_value < 32: return "MODERATE", 0
        if adx_value < 40: return "STRONG", -1
        return "VERY_STRONG", -2

    def _spot_vs_or(self, spot, or_high, or_low) -> tuple[str, int]:
        if or_high is None or or_low is None or spot is None:
            return "UNKNOWN", 0
        or_width = or_high - or_low
        buffer = max(or_width * 0.10, 10)
        if or_low + buffer <= spot <= or_high - buffer:
            return "INSIDE_OR", 1
        if spot > or_high + buffer:
            return f"ABOVE_OR_{spot - or_high:.0f}pts", -1
        if spot < or_low - buffer:
            return f"BELOW_OR_{or_low - spot:.0f}pts", -1
        return "AT_OR_BOUNDARY", 0

    def assess_trend_condition(self, candles_today: list, spot: Optional[float]) -> dict:
        n_bars = len(candles_today)
        if n_bars < 4:
            adx_value, pdi, ndi, adx_dir = 0.0, 0.0, 0.0, "UNKNOWN"
            adx_condition, reliability = "INSUFFICIENT_DATA", "LOW"
        else:
            reliability = "LOW" if n_bars < 12 else ("MEDIUM" if n_bars < 24 else "HIGH")
            adx_value, pdi, ndi, adx_dir = self._compute_adx(candles_today, period=14)
            if reliability == "LOW" and n_bars < 10:
                adx_condition = "EARLY_SESSION"
                adx_dir = "UNKNOWN"
            else:
                adx_condition, _ = self._classify_adx(adx_value)

        or_cond = self.state.get("or_condition")
        or_high, or_low = self.state.get("or_high"), self.state.get("or_low")
        spot_vs_or, spot_or_score = self._spot_vs_or(spot, or_high, or_low)

        if or_cond is None:
            trend_condition, trend_score = "OR_PENDING", 0
        else:
            trend_condition, trend_score = TREND_LOOKUP.get((or_cond, adx_condition), ("UNCERTAIN", 0))
            if adx_condition in ("EARLY_SESSION", "INSUFFICIENT_DATA") and or_cond in ("VERY_NARROW", "NARROW"):
                trend_condition, trend_score = "RANGE_ASSUMED", 1
            elif adx_condition in ("EARLY_SESSION", "INSUFFICIENT_DATA") and or_cond == "MODERATE":
                trend_condition, trend_score = "UNCERTAIN", 0
            if spot_vs_or.startswith("ABOVE_OR") or spot_vs_or.startswith("BELOW_OR"):
                try:
                    breakout_pts = float(spot_vs_or.split("_")[-1].replace("pts", ""))
                except ValueError:
                    breakout_pts = 0.0
                or_width = (or_high - or_low) if (or_high is not None and or_low is not None) else 0.0
                if or_width > 0:
                    if breakout_pts > or_width * 0.50 and trend_condition in ("RANGE_BOUND", "MILD_RANGE"):
                        trend_condition, trend_score = "MILD_TREND", 0
                    if breakout_pts > or_width * 1.00:
                        trend_condition, trend_score = "TRENDING", -1
                    if breakout_pts > or_width * 1.20 and adx_condition in ("MODERATE", "STRONG", "VERY_STRONG"):
                        trend_condition, trend_score = "STRONG_TREND", -2
            or_breakout_threshold_restored = True

        return {"trend_condition": trend_condition, "trend_score": trend_score,
                "adx_condition": adx_condition, "adx_value": adx_value,
                "pdi": pdi, "ndi": ndi, "adx_direction": adx_dir, "adx_reliability": reliability,
                "spot_vs_or": spot_vs_or, "spot_or_score": spot_or_score}

    # ─────────────────────────────────────────────────────────────────
    # MODULE 4: VOLATILITY CONDITION
    # ─────────────────────────────────────────────────────────────────
    def assess_volatility_condition(self, vrp, atm_iv, opening_iv, vix_regime, vix_spike, n_candles: int = 0) -> dict:
        if vrp is None:
            return {"volatility_condition": "UNKNOWN", "vol_score": 0, "sell_ok": False, "buy_ok": False,
                    "sell_size_reduction": 1.0, "iv_behavior": "UNKNOWN", "iv_change_pct": 0.0,
                    "high_quality_sell_day": False}
        if vix_spike:
            return {"volatility_condition": "SPIKING", "vol_score": -3, "sell_ok": False, "buy_ok": False,
                    "sell_size_reduction": 1.0, "iv_behavior": "SPIKING", "iv_change_pct": 0.0,
                    "high_quality_sell_day": False}

        sell_ok, buy_ok, sell_reduction = False, False, 1.0
        if vrp > 4.0: vol_cond, vol_score, sell_ok = "VERY_RICH", 2, True
        elif vrp > 2.0: vol_cond, vol_score, sell_ok = "RICH", 1, True
        elif vrp > 0.8: vol_cond, vol_score, sell_ok, sell_reduction = "FAIR", 0, True, 0.75
        elif vrp > 0.0: vol_cond, vol_score = "THIN", -1
        elif vrp > -2.0: vol_cond, vol_score, buy_ok = "CHEAP", -2, True
        else: vol_cond, vol_score, buy_ok = "INVERTED", -3, True

        if vix_regime == "SUPPRESSED":
            if vrp > 2.0:
                sell_ok = True
                sell_reduction = 0.5
                vol_score = min(vol_score, 0)
            else:
                sell_ok = False
                buy_ok = True
                vol_score = min(vol_score, -1)

        iv_behavior, iv_change_pct, iv_mod = "UNKNOWN", 0.0, 0.0
        if n_candles >= 6 and opening_iv and opening_iv > 0 and atm_iv and atm_iv > 0:
            iv_change_pct = (atm_iv - opening_iv) / opening_iv * 100.0
            if iv_change_pct < -12.0: iv_behavior, iv_mod = "CRUSHING", 0.3
            elif iv_change_pct < -4.0: iv_behavior, iv_mod = "DECLINING", 0.1
            elif iv_change_pct <= 4.0: iv_behavior, iv_mod = "STABLE", 0.0
            elif iv_change_pct <= 12.0:
                iv_behavior, iv_mod = "EXPANDING", -0.2
                sell_reduction = min(sell_reduction, 0.75)
            else:
                iv_behavior, iv_mod = "SPIKING", -1.0
                sell_ok = False

        high_quality = (vol_cond in ("RICH", "VERY_RICH")
                         and iv_behavior in ("CRUSHING", "DECLINING", "STABLE") and sell_ok)

        return {"volatility_condition": vol_cond, "vol_score": vol_score, "sell_ok": sell_ok,
                "buy_ok": buy_ok, "sell_size_reduction": sell_reduction, "iv_behavior": iv_behavior,
                "iv_change_pct": iv_change_pct, "combined_vol_score": vol_score + iv_mod,
                "high_quality_sell_day": high_quality}

    # ─────────────────────────────────────────────────────────────────
    # MODULE 6: DIRECTIONAL BIAS
    # ─────────────────────────────────────────────────────────────────
    def assess_directional_bias(self, vwap_dist_pct, pcr, opening_pcr, put_iv, call_iv, skew,
                                  adx_direction, gap_direction, last_candle_close_val: float = 0.0) -> dict:
        if vwap_dist_pct is None:
            vwap_signal, vwap_score = "UNKNOWN", 0
        elif vwap_dist_pct > 0.50: vwap_signal, vwap_score = "BULLISH_EXTENDED", 1
        elif vwap_dist_pct > 0.15: vwap_signal, vwap_score = "BULLISH", 1
        elif vwap_dist_pct > -0.15: vwap_signal, vwap_score = "NEUTRAL", 0
        elif vwap_dist_pct > -0.50: vwap_signal, vwap_score = "BEARISH", -1
        else: vwap_signal, vwap_score = "BEARISH_EXTENDED", -1

        or_high_val = self.state.get("or_high")
        or_low_val = self.state.get("or_low")
        spot_val = self.state.get("prev_spot")
        or_breakout_score = 0
        if or_high_val and or_low_val and spot_val and self.state.get("or_computed"):
            or_width_val = or_high_val - or_low_val
            confirm_buffer = max(or_width_val * 0.10, 10.0)
            last_candle_close = last_candle_close_val if last_candle_close_val > 0 else (spot_val or 0.0)
            or_breakout_candle_close = True
            if last_candle_close > or_high_val + confirm_buffer:
                or_breakout_score = 1
            elif last_candle_close < or_low_val - confirm_buffer:
                or_breakout_score = -1
        if vwap_signal == "UNKNOWN" and or_breakout_score != 0:
            vwap_score = or_breakout_score

        pcr_signal, pcr_score, pcr_change = "UNKNOWN", 0, None
        if opening_pcr and opening_pcr > 0 and pcr and pcr > 0:
            pcr_change = pcr - opening_pcr
            if pcr_change > 0.20: pcr_signal, pcr_score = "STRONG_FEAR", -1
            elif pcr_change > 0.08: pcr_signal, pcr_score = "FEAR_RISING", -1
            elif pcr_change > -0.08: pcr_signal, pcr_score = "STABLE", 0
            elif pcr_change > -0.20: pcr_signal, pcr_score = "GREED_RISING", 1
            else: pcr_signal, pcr_score = "STRONG_GREED", 1
            if pcr > 1.8: pcr_signal, pcr_score = "EXTREME_FEAR_CONTRARIAN", 1
            elif pcr < 0.60: pcr_signal, pcr_score = "EXTREME_GREED_CONTRARIAN", -1

        skew_thresholds_2026 = True
        if skew is None:
            skew_signal, skew_score, preferred_side = "UNKNOWN", 0, "BOTH"
        elif skew > 1.40: skew_signal, skew_score, preferred_side = "EXTREME_FEAR", -1, "CALLS"
        elif skew > 1.25: skew_signal, skew_score, preferred_side = "FEAR", -1, "CALLS"
        elif skew > 1.10: skew_signal, skew_score, preferred_side = "NORMAL", 0, "BOTH"
        elif skew > 0.95: skew_signal, skew_score, preferred_side = "BALANCED", 0, "BOTH"
        else: skew_signal, skew_score, preferred_side = "COMPLACENT", 1, "PUTS"

        direction_score = float(vwap_score + pcr_score + skew_score)

        current_time = now_ist().time()
        if current_time < dtime(11, 0) and gap_direction not in (None, "FLAT") and direction_score == 0:
            direction_score += 0.5 if gap_direction == "UP" else -0.5

        if adx_direction == "BULLISH" and direction_score > 0: direction_score += 0.3
        elif adx_direction == "BEARISH" and direction_score < 0: direction_score -= 0.3

        if direction_score >= 1.2: direction = "BULLISH"
        elif direction_score >= 0.5: direction = "MILD_BULLISH"
        elif direction_score <= -1.2: direction = "BEARISH"
        elif direction_score <= -0.5: direction = "MILD_BEARISH"
        else: direction = "NEUTRAL"

        if skew_signal in ("FEAR", "EXTREME_FEAR"): preferred_side = "CALLS"
        elif skew_signal == "COMPLACENT": preferred_side = "PUTS"
        elif direction in ("BULLISH", "MILD_BULLISH"): preferred_side = "PUTS"
        elif direction in ("BEARISH", "MILD_BEARISH"): preferred_side = "CALLS"
        else: preferred_side = "BOTH"

        if vwap_signal == "BULLISH_EXTENDED": preferred_side = "PUTS"
        elif vwap_signal == "BEARISH_EXTENDED": preferred_side = "CALLS"

        return {"direction": direction, "direction_score": direction_score,
                "vwap_signal": vwap_signal, "pcr_signal": pcr_signal, "pcr_change": pcr_change,
                "skew_signal": skew_signal, "preferred_sell_side": preferred_side,
                "vwap_score": vwap_score, "pcr_score": pcr_score, "skew_score": skew_score}

    # ─────────────────────────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────────────────────────
    def _persist_option_chain_snapshot(self, chain: dict, expiry: Optional[date]) -> None:
        if not chain or not expiry:
            return
        capture_time, trading_date = now_ist().isoformat(), today_ist().isoformat()
        rows = []
        for strike, legs in chain.items():
            for opt_type in ("call", "put"):
                leg = legs.get(opt_type, {})
                if not leg:
                    continue
                rows.append((
                    capture_time, trading_date, expiry.isoformat(), strike, opt_type,
                    leg.get("bid", 0), leg.get("ask", 0), leg.get("ltp", 0),
                    leg.get("oi", 0), leg.get("volume", 0), leg.get("iv", 0), leg.get("delta", 0),
                    leg.get("gamma", 0), leg.get("theta", 0), leg.get("vega", 0), leg.get("timestamp"),
                ))
        if rows:
            self.db.executemany(
                """INSERT INTO option_chain_snapshot
                   (capture_time, trading_date, expiry, strike, option_type, bid, ask, ltp,
                    oi, volume, iv, delta, gamma, theta, vega, data_timestamp)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            self.logger.debug(f"Persisted {len(rows)} option_chain_snapshot rows for {expiry}")

    def _persist_cycle_log(self, s: dict) -> None:
        self.db.insert("cycle_log", {
            "cycle_time": now_ist().isoformat(), "trading_date": s["trading_date"],
            "spot": s["spot"], "vix": s["vix"], "vrp": s["vrp"],
            "atm_iv_pct": (s["atm_iv"] * 100) if s["atm_iv"] else None,
            "parkinson_rv_pct": (s["parkinson_rv"] * 100) if s["parkinson_rv"] else None,
            "adx": s["adx"], "adx_condition": s["adx_condition"],
            "vwap": s["vwap"], "vwap_dist_pct": s["vwap_dist_pct"],
            "pcr": s["pcr"], "pcr_change": s.get("pcr_change"),
            "skew_ratio": s["skew_ratio"], "or_width": s["or_width"], "or_condition": s["or_condition"],
            "volatility_condition": s["volatility_condition"], "iv_behavior": s["iv_behavior"],
            "trend_condition": s["trend_condition"], "direction": s["direction"],
            "preferred_sell_side": s["preferred_sell_side"], "entry_timing": None,
            "action_taken": "SIGNAL_ONLY", "no_trade_reason": None,
            "conditions_met_json": json.dumps(s.get("conditions_met", {})),
            "conditions_not_met_json": json.dumps(s.get("conditions_not_met", {})),
            "open_positions": 0, "daily_pnl_net": s.get("daily_pnl", 0.0),
            "vix_regime": s["vix_regime"], "day_mode": s["day_mode"],
            "raw_json": json.dumps({k: v for k, v in s.items() if k != "atm_greeks"}, default=str),
        })

    def finalize_cycle_log(self, action_taken: str, no_trade_reason: Optional[str],
                            open_positions: int) -> None:
        """Called by File 3/4/5 after a strategy decision is made, to enrich the
        most recent cycle_log row with the actual action taken this cycle."""
        latest = self.db.query_one(
            "SELECT cycle_id FROM cycle_log WHERE trading_date=? ORDER BY cycle_id DESC LIMIT 1",
            (today_ist().isoformat(),),
        )
        if latest:
            self.db.update("cycle_log",
                            {"action_taken": action_taken, "no_trade_reason": no_trade_reason,
                             "open_positions": open_positions},
                            {"cycle_id": latest["cycle_id"]})

    # ─────────────────────────────────────────────────────────────────
    # CONDITIONS SUMMARY + TRADING WINDOW CHECK
    # ─────────────────────────────────────────────────────────────────
    def _is_within_trading_window(self) -> bool:
        t = now_ist().time()
        try:
            entry_start = datetime.strptime(self.state["entry_start"], "%H:%M").time()
            entry_end = datetime.strptime(self.state["entry_end"], "%H:%M").time()
        except Exception:
            entry_start, entry_end = self.config.trading_window_start, self.config.trading_window_last_entry
        return entry_start <= t <= entry_end

    def _build_conditions_summary(self, s: dict) -> tuple[dict, dict]:
        checks = {
            "or_computed": bool(self.state.get("or_computed")),
            "session_initialized": bool(self.state.get("session_initialized")),
            "vwap_valid": bool(self.state.get("vwap_valid")),
            "atm_iv_available": s["atm_iv"] is not None,
            "vrp_available": s["vrp"] is not None,
            "chain_has_min_strikes": s["chain_size"] >= 10,
            "not_circuit_breaker": not s["circuit_breaker_suspected"],
            "not_vix_spike": not s["vix_spike_detected"],
            "vix_regime_not_suppressed": s["vix_regime"] != "SUPPRESSED",
            "within_trading_window": self._is_within_trading_window(),
            "daily_not_halted": not bool(self.state.get("daily_halted")),
        }
        met = {k: v for k, v in checks.items() if v}
        not_met = {k: v for k, v in checks.items() if not v}
        return met, not_met

    # ─────────────────────────────────────────────────────────────────
    # CONSOLE DASHBOARD
    # ─────────────────────────────────────────────────────────────────
    def _display_cycle_console(self, s: dict) -> None:
        print_section(f"CYCLE @ {now_ist().strftime('%H:%M:%S')} IST — {s['trading_date']}")

        print_kv_table({
            "Spot (NIFTY 50)": s["spot"], "India VIX": s["vix"], "VIX Regime": s["vix_regime"],
            "Day Mode": s["day_mode"], "Day Label": s.get("day_label"),
            "Active Expiry / DTE": f"{s['active_expiry']} / {s['actual_dte']}",
            "Trading Window": f"{s['entry_start']} - {s['entry_end']} (hard exit {s['hard_exit_time']})",
            "Circuit Breaker Suspected": s["circuit_breaker_suspected"],
            "VIX Spike Detected": s["vix_spike_detected"],
            "Current Capital": s["current_capital"], "Daily P&L": s["daily_pnl"],
        }, title="MARKET SNAPSHOT")

        print_kv_table({
            "ATM IV": f"{s['atm_iv']*100:.2f}%" if s["atm_iv"] else "N/A",
            "Parkinson RV": f"{s['parkinson_rv']*100:.2f}% ({s['rv_source']})" if s["parkinson_rv"] else "N/A",
            "VRP": f"{s['vrp']:.2f}pp" if s["vrp"] is not None else "N/A",
            "Volatility Condition": s["volatility_condition"], "IV Behavior": s["iv_behavior"],
            "Sell Strategies Allowed": s["sell_ok"], "Buy Strategies Allowed": s["buy_ok"],
            "High Quality Sell Day": s["high_quality_sell_day"],
        }, title="VOLATILITY / VRP ANALYSIS")

        print_kv_table({
            "OR Condition": s["or_condition"], "OR Width (pts)": s["or_width"],
            "ADX": f"{s['adx']:.1f}" if s["adx"] else "N/A",
            "ADX Condition": s["adx_condition"], "ADX Direction": s["adx_direction"],
            "Trend Condition": s["trend_condition"], "Spot vs OR": s["spot_vs_or"],
        }, title="TREND ANALYSIS")

        print_kv_table({
            "VWAP": s["vwap"],
            "VWAP Distance": f"{s['vwap_dist_pct']:.2f}%" if s["vwap_dist_pct"] is not None else "N/A",
            "VWAP Signal": s["vwap_signal"], "PCR": s["pcr"], "PCR Change (vs open)": s["pcr_change"],
            "PCR Signal": s["pcr_signal"], "Skew Ratio (25d Put/Call IV)": s["skew_ratio"],
            "Skew Signal": s["skew_signal"], "Direction": s["direction"],
            "Preferred Sell Side": s["preferred_sell_side"],
        }, title="DIRECTIONAL BIAS")

        ag = s.get("atm_greeks", {})
        call_g, put_g = ag.get("call", {}), ag.get("put", {})
        print_kv_table({
            "ATM Call  Delta/Gamma/Theta/Vega": f"{call_g.get('delta',0):.3f} / {call_g.get('gamma',0):.4f} / "
                                                  f"{call_g.get('theta',0):.2f} / {call_g.get('vega',0):.2f}",
            "ATM Put   Delta/Gamma/Theta/Vega": f"{put_g.get('delta',0):.3f} / {put_g.get('gamma',0):.4f} / "
                                                  f"{put_g.get('theta',0):.2f} / {put_g.get('vega',0):.2f}",
            "Chain Strikes Loaded": s["chain_size"],
        }, title="ATM GREEKS SNAPSHOT")

        met, not_met = s.get("conditions_met", {}), s.get("conditions_not_met", {})
        print("\n--- READINESS GATES (Module 2-6 level; strategy-level gates arrive in File 3/4) ---")
        print(f"  MET     ({len(met)}): {', '.join(met.keys()) if met else '(none)'}")
        print(f"  NOT MET ({len(not_met)}): {', '.join(not_met.keys()) if not_met else '(none)'}")
        print()

    # ─────────────────────────────────────────────────────────────────
    # MAIN ORCHESTRATION — one full cycle
    # ─────────────────────────────────────────────────────────────────
    def run_cycle(self) -> dict:
        self.reset_if_new_day()
        trading_date = today_ist().isoformat()

        spot, vix = self.fetch_spot_and_vix()
        circuit, vix_spike = self._check_circuit_breaker_and_vix_spike(spot, vix)
        self.state["circuit_breaker_suspected"] = circuit
        self.state["vix_spike_detected"] = vix_spike

        self._maybe_run_pre_session_assessment(vix)

        candles_today = self.fetch_5min_candles_today()
        vwap, vwap_valid = self.compute_vwap(candles_today)
        self.state["vwap_valid"] = vwap_valid

        expiry, dte = self._get_active_expiry()
        chain = self.fetch_option_chain(expiry) if expiry else {}
        self.last_chain = chain
        self.last_chain_expiry = expiry

        atm_iv = self.compute_atm_iv(chain, spot)
        pcr = self.compute_pcr(chain, spot)
        put_iv, call_iv = self.compute_25d_ivs(chain)
        skew = self.compute_skew_ratio(put_iv, call_iv)

        if atm_iv is not None and dte is not None and spot is not None:
            dte_wing_map = {0: 50, 1: 75, 2: 100, 3: 100, 4: 150, 5: 150, 6: 200, 7: 200, 8: 250}
            vix_wing_mult = {"SUPPRESSED": 0.8, "LOW": 1.0, "NORMAL": 1.0,
                             "ELEVATED": 1.3, "HIGH": 1.6}.get(self.state.get("vix_regime", "NORMAL"), 1.0)
            wing_dte_vix_combined = True
            if dte in dte_wing_map:
                base_wing = dte_wing_map[dte]
                step = self.config.nifty_strike_step
                adjusted = int(round((base_wing * vix_wing_mult) / step) * step)
                self.state["wing_width"] = max(50, min(adjusted, 500))
            else:
                expected_move = spot * atm_iv * ((max(dte, 1) / 365.0) ** 0.5)
                computed_wing = int(round((expected_move * 0.60) / self.config.nifty_strike_step) * self.config.nifty_strike_step)
                self.state["wing_width"] = max(100, min(computed_wing, 400))

        current_time = now_ist().time()
        if current_time >= dtime(9, 45) and not self.state.get("or_computed"):
            or_result = self.compute_opening_range(candles_today)
            if or_result:
                self.state["or_high"] = or_result["or_high"]
                self.state["or_low"] = or_result["or_low"]
                self.state["or_width"] = or_result["or_width"]
                self.state["or_condition"] = or_result["or_condition"]
                self.state["or_computed"] = True
                self.logger.info(f"OPENING RANGE COMPUTED: H={or_result['or_high']:.0f} "
                                  f"L={or_result['or_low']:.0f} W={or_result['or_width']:.0f} "
                                  f"[{or_result['or_condition']}]")

        if current_time >= dtime(9, 45) and not self.state.get("session_initialized"):
            if atm_iv:
                self.state["opening_iv"] = atm_iv
            if self.state.get("opening_iv") is not None:
                self.state["session_initialized"] = True
                self.logger.info(f"Session initialized: opening_iv={self.state['opening_iv']*100:.2f}%")
        if current_time >= dtime(10, 0) and not getattr(self, "_pcr_baseline_set", False):
            if pcr:
                self.state["opening_pcr"] = pcr
                self._pcr_baseline_set = True
                self.logger.info(f"PCR baseline set at 10:00: opening_pcr={pcr:.3f}")

        parkinson_rv, rv_source = self.compute_parkinson_rv(vix, candles_today)
        intraday_rv = self.compute_intraday_parkinson_rv(candles_today)
        effective_rv = parkinson_rv
        if intraday_rv is not None and parkinson_rv is not None:
            vrp_blend_weight = min(len(candles_today) / 30.0, 1.0)
            effective_rv = intraday_rv * vrp_blend_weight + parkinson_rv * (1.0 - vrp_blend_weight)
        elif intraday_rv is not None:
            effective_rv = intraday_rv
        vrp = (atm_iv * 100.0 - effective_rv * 100.0) if (atm_iv is not None and effective_rv is not None) else None
        intraday_rv_selling_veto = (
            len(candles_today) >= 18
            and intraday_rv is not None
            and atm_iv is not None
            and intraday_rv > atm_iv * 1.10
        )

        vol_result = self.assess_volatility_condition(
            vrp, atm_iv, self.state.get("opening_iv"), self.state.get("vix_regime"), vix_spike,
            n_candles=len(candles_today)
        )
        trend_result = self.assess_trend_condition(candles_today, spot)

        vwap_dist_pct = ((spot - vwap) / vwap * 100.0) if (vwap and vwap > 0 and spot is not None) else None

        _last_close = candles_today[-1]["close"] if candles_today else (spot or 0.0)
        dir_result = self.assess_directional_bias(
            vwap_dist_pct, pcr, self.state.get("opening_pcr"), put_iv, call_iv, skew,
            trend_result.get("adx_direction"), self.state.get("gap_direction"),
            last_candle_close_val=_last_close,
        )

        atm_greeks = self._get_atm_greeks(chain, spot)

        signals = {
            "trading_date": trading_date, "spot": spot, "vix": vix,
            "intraday_rv_selling_veto": intraday_rv_selling_veto,
            "vix_regime": self.state.get("vix_regime"), "day_mode": self.state.get("day_mode"),
            "day_label": self.state.get("day_label"),
            "circuit_breaker_suspected": circuit, "vix_spike_detected": vix_spike,
            "atm_iv": atm_iv, "parkinson_rv": parkinson_rv, "rv_source": rv_source, "vrp": vrp,
            "volatility_condition": vol_result["volatility_condition"], "iv_behavior": vol_result["iv_behavior"],
            "sell_ok": vol_result["sell_ok"], "buy_ok": vol_result["buy_ok"],
            "sell_size_reduction": vol_result["sell_size_reduction"],
            "high_quality_sell_day": vol_result["high_quality_sell_day"],
            "or_condition": self.state.get("or_condition"), "or_width": self.state.get("or_width"),
            "adx": trend_result["adx_value"], "adx_condition": trend_result["adx_condition"],
            "adx_direction": trend_result["adx_direction"], "trend_condition": trend_result["trend_condition"],
            "spot_vs_or": trend_result["spot_vs_or"],
            "vwap": vwap, "vwap_dist_pct": vwap_dist_pct, "pcr": pcr,
            "pcr_change": dir_result.get("pcr_change"), "skew_ratio": skew,
            "direction": dir_result["direction"], "vwap_signal": dir_result["vwap_signal"],
            "pcr_signal": dir_result["pcr_signal"], "skew_signal": dir_result["skew_signal"],
            "preferred_sell_side": dir_result["preferred_sell_side"],
            "entry_start": self.state.get("entry_start"), "entry_end": self.state.get("entry_end"),
            "hard_exit_time": self.state.get("hard_exit_time"), "atm_greeks": atm_greeks,
            "chain_size": len(chain), "active_expiry": expiry.isoformat() if expiry else None,
            "actual_dte": dte, "daily_pnl": self.state.get("daily_pnl", 0.0),
            "current_capital": self.state.get("current_capital", self.config.starting_capital),
        }

        met, not_met = self._build_conditions_summary(signals)
        signals["conditions_met"], signals["conditions_not_met"] = met, not_met

        self._save_session_state()
        self._persist_option_chain_snapshot(chain, expiry)
        self._persist_cycle_log(signals)
        self._display_cycle_console(signals)

        return signals


# ─────────────────────────────────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────────────────────────────────
def _self_test() -> None:
    print_section("NIFTY ALGO — MARKET DATA ENGINE SELF-TEST")
    config = load_config()
    db = Database(config.db_path)
    logger = setup_logging(db, config.log_dir)
    rate_limiter = RateLimiter(config.rate_limits)
    client = UpstoxClient(config, rate_limiter, db, logger)

    engine = MarketDataEngine(config, db, client, rate_limiter, logger)

    if not config.upstox_access_token:
        logger.warning("No UPSTOX_ACCESS_TOKEN configured — engine constructed but "
                        "cannot run a live cycle test.")
        db.close()
        return

    if not client.validate_token():
        logger.error("Upstox token invalid/expired — cannot run a live cycle test.")
        db.close()
        return

    engine.run_cycle()
    print_section("SELF-TEST COMPLETE")
    db.close()


if __name__ == "__main__":
    _self_test()