#!/usr/bin/env python3
"""Fractional ATR Renko Supertrend — production backtesting engine.

Connects to Upstox API v2, downloads daily + 1-minute Nifty history, locks a
per-session fractional brick size from 5-day True Range, builds synthetic
Renko, applies Supertrend (multiplier = 3), and simulates intraday long/short
execution with slippage, PCR sizing, and a 15:20 IST hard square-off.

Run:
    python backtest_renko.py
    python backtest_renko.py --from-date 2026-08-01 --to-date 2026-09-30
    python backtest_renko.py --synthetic --days 20 --cash-to-lose 2000
    python backtest_renko.py --from-date 2026-08-01 --to-date 2026-09-30
    python backtest_renko.py --self-test

Credentials are read from a local ``env.txt`` (never from OS environment)::

    UPSTOX_ACCESS_TOKEN=...
    UPSTOX_API_KEY=...
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import traceback
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))
try:
    from zoneinfo import ZoneInfo

    IST_ZONE: timezone | ZoneInfo = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - fallback for stripped runtimes
    IST_ZONE = IST

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "env.txt"
CACHE_DIR = BASE_DIR / "data" / "renko_cache"
RESULTS_DIR = BASE_DIR / "backtest_results"
HOLIDAYS_FILE = BASE_DIR / "nse_holidays.json"

UPSTOX_BASE_URL = "https://api.upstox.com/v2"
HISTORICAL_CANDLE_PATH = (
    "/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}"
)

DEFAULT_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
DEFAULT_CASH_TO_LOSE = 2_000.0
DEFAULT_CAPITAL = 500_000.0
SLIPPAGE_POINTS = 1.0  # per side, liquid Nifty futures
SLIPPAGE_PCT = 0.0
SUPERTREND_MULTIPLIER = 5.0
ATR_LOOKBACK = 14
SESSION_MINUTES = 375  # 09:15 → 15:30
TARGET_TREND_MINUTES = 60
FRACTIONAL_DIVISOR = SESSION_MINUTES / TARGET_TREND_MINUTES  # 6.25
SESSION_START = dtime(9, 15)
SESSION_END = dtime(15, 30)
ENTRY_START = dtime(9, 45)
ENTRY_END = dtime(14, 30)
SQUARE_OFF = dtime(15, 20)
STOP_ATR_MULT = 2.0
SMA_FILTER_PERIOD = 20
STOP_COOLDOWN_MINUTES = 15
TRADING_DAYS_PER_YEAR = 252

CANDLE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "oi")

LOG = logging.getLogger("renko_backtest")


# ─────────────────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────────────────────────────────────

class EnvConfigError(RuntimeError):
    """Raised when env.txt cannot be read or required keys are missing."""


class UpstoxDataError(RuntimeError):
    """Raised when the Upstox historical API fails after retries."""


class StrategyError(RuntimeError):
    """Raised when the quantitative engine cannot produce a valid session."""


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG / ENV
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EnvCredentials:
    """Secrets loaded exclusively from env.txt."""

    access_token: str
    api_key: str = ""


@dataclass
class BacktestSettings:
    instrument_key: str = DEFAULT_INSTRUMENT_KEY
    cash_to_lose: float = DEFAULT_CASH_TO_LOSE
    capital: float = DEFAULT_CAPITAL
    slippage_pct: float = SLIPPAGE_PCT
    slippage_points: float = SLIPPAGE_POINTS
    supertrend_multiplier: float = SUPERTREND_MULTIPLIER
    atr_lookback: int = ATR_LOOKBACK
    fractional_divisor: float = FRACTIONAL_DIVISOR
    session_start: dtime = SESSION_START
    session_end: dtime = SESSION_END
    square_off: dtime = SQUARE_OFF
    entry_start: dtime = ENTRY_START
    entry_end: dtime = ENTRY_END
    open_buffer_minutes: int = 0
    entry_cutoff: Optional[dtime] = None
    confirm_bricks: int = 1
    stop_atr_mult: float = STOP_ATR_MULT
    sma_filter_period: int = SMA_FILTER_PERIOD
    stop_cooldown_minutes: int = STOP_COOLDOWN_MINUTES
    lot_size: int = 1
    request_timeout: float = 30.0
    max_retries: int = 4
    min_request_interval: float = 0.35
    cache_dir: Path = CACHE_DIR


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs from a local env file.

    OS environment variables are intentionally ignored. Inline comments
    (``VALUE  # comment``), surrounding quotes, and blank/comment lines
    are stripped. File I/O errors are converted to :class:`EnvConfigError`.
    """
    env: dict[str, str] = {}
    try:
        if not path.exists():
            raise EnvConfigError(
                f"Environment file not found: {path}. "
                "Create it with UPSTOX_ACCESS_TOKEN=... and UPSTOX_API_KEY=..."
            )
        raw = path.read_text(encoding="utf-8")
    except EnvConfigError:
        raise
    except OSError as exc:
        raise EnvConfigError(f"Unable to read {path}: {exc}") from exc

    for lineno, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if not key:
            raise EnvConfigError(f"Empty key on line {lineno} of {path}")
        env[key] = value
    return env


def load_credentials(path: Path = ENV_FILE) -> EnvCredentials:
    """Load Upstox credentials from env.txt and validate the access token."""
    env = load_env_file(path)
    token = (env.get("UPSTOX_ACCESS_TOKEN") or "").strip()
    api_key = (
        env.get("UPSTOX_API_KEY")
        or env.get("UPSTOX_CLIENT_ID")
        or ""
    ).strip()
    if not token or token.lower() in {"your_access_token_here", "changeme", "xxx"}:
        raise EnvConfigError(
            f"UPSTOX_ACCESS_TOKEN missing or still a placeholder in {path}"
        )
    return EnvCredentials(access_token=token, api_key=api_key)


def now_ist() -> datetime:
    return datetime.now(IST_ZONE)


def today_ist() -> date:
    return now_ist().date()


def ensure_ist(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST_ZONE)
    return ts.astimezone(IST_ZONE)


def load_nse_holidays(path: Path = HOLIDAYS_FILE) -> set[date]:
    holidays: set[date] = set()
    if not path.exists():
        return holidays
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Could not parse NSE holidays file %s: %s", path, exc)
        return holidays
    items: Iterable[Any]
    if isinstance(payload, dict):
        items = payload.get("holidays") or payload.get("dates") or payload.values()
    else:
        items = payload
    for item in items:
        try:
            holidays.add(date.fromisoformat(str(item)[:10]))
        except ValueError:
            continue
    return holidays


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def is_trading_day(d: date, holidays: Optional[set[date]] = None) -> bool:
    if not is_weekday(d):
        return False
    if holidays and d in holidays:
        return False
    return True


def daterange_trading_days(
    start: date,
    end: date,
    holidays: Optional[set[date]] = None,
) -> list[date]:
    out: list[date] = []
    cur = start
    while cur <= end:
        if is_trading_day(cur, holidays):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    LOG.setLevel(level)


# ─────────────────────────────────────────────────────────────────────────────
# UPSTOX DATA LOADER
# ─────────────────────────────────────────────────────────────────────────────

class UpstoxDataLoader:
    """Authenticated client for Upstox v2 historical candle endpoints.

    Daily::
        GET /v2/historical-candle/{key}/day/{to}/{from}

    1-minute::
        GET /v2/historical-candle/{key}/1minute/{to}/{from}

    Responses are normalised into IST-indexed pandas DataFrames.
    """

    def __init__(
        self,
        credentials: EnvCredentials,
        settings: Optional[BacktestSettings] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.credentials = credentials
        self.settings = settings or BacktestSettings()
        self._last_request_ts = 0.0
        self.session = session or self._build_session()
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)

    def _build_session(self) -> requests.Session:
        sess = requests.Session()
        try:
            retry = Retry(
                total=self.settings.max_retries,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET",),
                respect_retry_after_header=True,
            )
        except TypeError:  # older urllib3
            retry = Retry(
                total=self.settings.max_retries,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                method_whitelist=("GET",),
            )
        sess.mount("https://", HTTPAdapter(max_retries=retry))
        sess.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.credentials.access_token}",
                "Api-Version": "2.0",
            }
        )
        return sess

    def _throttle(self) -> None:
        gap = self.settings.min_request_interval
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < gap:
            time.sleep(gap - elapsed)

    def _get(self, url: str) -> requests.Response:
        self._throttle()
        try:
            resp = self.session.get(url, timeout=self.settings.request_timeout)
        except requests.RequestException as exc:
            raise UpstoxDataError(f"Network error calling {url}: {exc}") from exc
        self._last_request_ts = time.monotonic()
        return resp

    def _cache_path(
        self, interval: str, instrument_key: str, from_date: date, to_date: date
    ) -> Path:
        safe = (
            instrument_key.replace("|", "_")
            .replace(" ", "_")
            .replace("/", "_")
        )
        name = f"{interval}_{safe}_{from_date.isoformat()}_{to_date.isoformat()}.csv.gz"
        return self.settings.cache_dir / name

    def _get_candles(
        self,
        instrument_key: str,
        interval: str,
        from_date: date,
        to_date: date,
    ) -> list[list[Any]]:
        encoded_key = quote(instrument_key, safe="")
        path = HISTORICAL_CANDLE_PATH.format(
            instrument_key=encoded_key,
            interval=interval,
            to_date=to_date.isoformat(),
            from_date=from_date.isoformat(),
        )
        url = f"{UPSTOX_BASE_URL}{path}"
        resp = self._get(url)
        # Some gateways reject a fully-escaped pipe; retry with `|` left intact.
        if resp.status_code in {400, 404}:
            alt_key = quote(instrument_key, safe="|")
            if alt_key != encoded_key:
                alt_path = HISTORICAL_CANDLE_PATH.format(
                    instrument_key=alt_key,
                    interval=interval,
                    to_date=to_date.isoformat(),
                    from_date=from_date.isoformat(),
                )
                resp = self._get(f"{UPSTOX_BASE_URL}{alt_path}")

        if resp.status_code == 401:
            raise UpstoxDataError(
                "Upstox rejected the access token (HTTP 401). "
                "Refresh UPSTOX_ACCESS_TOKEN in env.txt."
            )
        if resp.status_code >= 400:
            raise UpstoxDataError(
                f"Upstox historical API HTTP {resp.status_code} for "
                f"{interval} {from_date}→{to_date}: {resp.text[:400]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise UpstoxDataError("Upstox returned non-JSON historical payload") from exc
        if str(payload.get("status", "")).lower() not in {"", "success"}:
            raise UpstoxDataError(f"Upstox error payload: {payload}")
        candles = payload.get("data", {}).get("candles") or []
        if not isinstance(candles, list):
            raise UpstoxDataError("Unexpected candle payload shape")
        return candles

    @staticmethod
    def candles_to_frame(candles: Sequence[Sequence[Any]]) -> pd.DataFrame:
        """Normalise Upstox ``[ts, o, h, l, c, vol, oi]`` rows to IST OHLCV."""
        if not candles:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "oi"]
            )
        rows: list[dict[str, Any]] = []
        for raw in candles:
            if not raw or len(raw) < 5:
                continue
            ts_raw = raw[0]
            try:
                ts = pd.Timestamp(ts_raw)
            except (ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.tz_localize(IST_ZONE)
            else:
                ts = ts.tz_convert(IST_ZONE)
            volume = float(raw[5]) if len(raw) > 5 and raw[5] is not None else 0.0
            oi = float(raw[6]) if len(raw) > 6 and raw[6] is not None else 0.0
            rows.append(
                {
                    "timestamp": ts,
                    "open": float(raw[1]),
                    "high": float(raw[2]),
                    "low": float(raw[3]),
                    "close": float(raw[4]),
                    "volume": volume,
                    "oi": oi,
                }
            )
        if not rows:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "oi"]
            )
        df = pd.DataFrame(rows).drop_duplicates(subset=["timestamp"])
        df = df.sort_values("timestamp").set_index("timestamp")
        df.index.name = "timestamp"
        return df

    def _load_or_fetch(
        self,
        interval: str,
        instrument_key: str,
        from_date: date,
        to_date: date,
        use_cache: bool,
    ) -> pd.DataFrame:
        cache_path = self._cache_path(interval, instrument_key, from_date, to_date)
        if use_cache and cache_path.exists():
            LOG.debug("Cache hit %s", cache_path.name)
            df = pd.read_csv(cache_path, parse_dates=["timestamp"])
            if df.empty:
                return self.candles_to_frame([])
            ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df["timestamp"] = ts.dt.tz_convert(IST_ZONE)
            return df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()

        candles = self._get_candles(instrument_key, interval, from_date, to_date)
        df = self.candles_to_frame(candles)
        if use_cache:
            try:
                out = df.reset_index()
                out.to_csv(cache_path, index=False, compression="gzip")
            except OSError as exc:
                LOG.warning("Could not write candle cache %s: %s", cache_path, exc)
        return df

    def fetch_daily(
        self,
        from_date: date,
        to_date: date,
        instrument_key: Optional[str] = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Daily historical candles covering ``[from_date, to_date]``."""
        key = instrument_key or self.settings.instrument_key
        LOG.info("Fetching daily candles %s → %s (%s)", from_date, to_date, key)
        df = self._load_or_fetch("day", key, from_date, to_date, use_cache)
        return df

    def fetch_intraday_1m(
        self,
        from_date: date,
        to_date: date,
        instrument_key: Optional[str] = None,
        use_cache: bool = True,
        chunk_days: int = 7,
    ) -> pd.DataFrame:
        """1-minute candles, chunked to respect Upstox lookback limits.

        Upstox v2 serves ~1 month of 1-minute history per request and only
        for the preceding ~6 months. Chunks are fetched newest-first and
        concatenated, then filtered to the NSE cash session.
        """
        key = instrument_key or self.settings.instrument_key
        if from_date > to_date:
            raise ValueError("from_date must be on or before to_date")

        max_lookback = today_ist() - timedelta(days=200)
        if from_date < max_lookback:
            LOG.warning(
                "Clamping 1-minute from_date %s → %s (Upstox ~6 month limit)",
                from_date,
                max_lookback,
            )
            from_date = max_lookback

        frames: list[pd.DataFrame] = []
        chunk_end = to_date
        while chunk_end >= from_date:
            chunk_start = max(from_date, chunk_end - timedelta(days=chunk_days - 1))
            LOG.info(
                "Fetching 1-minute candles %s → %s (%s)",
                chunk_start,
                chunk_end,
                key,
            )
            try:
                part = self._load_or_fetch(
                    "1minute", key, chunk_start, chunk_end, use_cache
                )
            except UpstoxDataError as exc:
                LOG.error("Chunk %s→%s failed: %s", chunk_start, chunk_end, exc)
                part = self.candles_to_frame([])
            if not part.empty:
                frames.append(part)
            if chunk_start == from_date:
                break
            chunk_end = chunk_start - timedelta(days=1)

        if not frames:
            return self.candles_to_frame([])

        df = (
            pd.concat(frames, axis=0)
            .sort_index()
        )
        df = df[~df.index.duplicated(keep="last")]
        return self.filter_session(df)

    def filter_session(self, df: pd.DataFrame) -> pd.DataFrame:
        """Keep NSE cash-session bars (09:15–15:30 IST)."""
        if df.empty:
            return df
        idx = df.index
        if getattr(idx, "tz", None) is None:
            df = df.copy()
            df.index = pd.DatetimeIndex(idx).tz_localize(IST_ZONE)
            idx = df.index
        minutes = idx.hour * 60 + idx.minute
        start_m = (
            self.settings.session_start.hour * 60 + self.settings.session_start.minute
        )
        end_m = self.settings.session_end.hour * 60 + self.settings.session_end.minute
        mask = (minutes >= start_m) & (minutes <= end_m)
        return df.loc[mask].copy()


# ─────────────────────────────────────────────────────────────────────────────
# SYNTHETIC DATA (offline / demo / unit tests)
# ─────────────────────────────────────────────────────────────────────────────

def generate_synthetic_market(
    start: date,
    n_days: int,
    seed: int = 42,
    start_price: float = 24_500.0,
    holidays: Optional[set[date]] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Deterministic Nifty-like daily + 1-minute series for offline runs."""
    rng = np.random.default_rng(seed)
    days: list[date] = []
    cursor = start
    while len(days) < n_days:
        if is_trading_day(cursor, holidays):
            days.append(cursor)
        cursor += timedelta(days=1)

    daily_rows: list[dict[str, Any]] = []
    minute_rows: list[dict[str, Any]] = []
    price = float(start_price)
    # datetime.time does not accept timedelta — build via a dummy datetime.
    session_times: list[dtime] = []
    base = datetime(2000, 1, 1, 9, 15)
    for i in range(SESSION_MINUTES):
        session_times.append((base + timedelta(minutes=i)).time())

    for i, d in enumerate(days):
        # Daily drift of a few bps, per-minute vol ≈ 1% daily / sqrt(375).
        # Alternate trend / mean-reversion days so Supertrend actually fires.
        day_drift = 0.004 if (i % 4 == 0) else (-0.0035 if i % 4 == 1 else 0.0002)
        drift = day_drift / SESSION_MINUTES
        vol = (0.009 if i % 5 else 0.014) / math.sqrt(SESSION_MINUTES)
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        day_open = price
        for t in session_times:
            shock = drift + float(rng.normal(0.0, vol))
            nxt = max(price * (1.0 + shock), 100.0)
            o, c = price, nxt
            wick = abs(float(rng.normal(0.0, vol * price * 0.35)))
            h = max(o, c) + wick
            l = min(o, c) - wick
            ts = datetime.combine(d, t, tzinfo=IST_ZONE)
            minute_rows.append(
                {
                    "timestamp": ts,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": float(rng.integers(50_000, 250_000)),
                    "oi": 0.0,
                }
            )
            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(c)
            price = c
        daily_rows.append(
            {
                "timestamp": datetime.combine(d, dtime(15, 30), tzinfo=IST_ZONE),
                "open": day_open,
                "high": max(highs),
                "low": min(lows),
                "close": closes[-1],
                "volume": 1.0,
                "oi": 0.0,
            }
        )

    daily = (
        pd.DataFrame(daily_rows)
        .set_index("timestamp")
        .sort_index()
    )
    minutes = (
        pd.DataFrame(minute_rows)
        .set_index("timestamp")
        .sort_index()
    )
    return daily, minutes


# ─────────────────────────────────────────────────────────────────────────────
# QUANTITATIVE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionBaseline:
    trade_date: date
    daily_atr_5: float
    static_brick_size: float
    quantity: int
    risk_per_unit: float
    lookback_days: list[date]
    stop_distance: float = 0.0
    sma20: float = 0.0
    prior_close: float = 0.0
    allow_long: bool = True
    allow_short: bool = True


@dataclass
class DayDiagnostics:
    trade_date: date
    bricks_total: int = 0
    signal_flips: int = 0
    filtered_out_of_window: int = 0
    filtered_regime: int = 0
    filtered_cooldown: int = 0
    filtered_same_pos: int = 0
    entries_taken: int = 0
    stop_hits: int = 0
    square_offs: int = 0
    signal_reversals: int = 0
    signal_exits: int = 0


class FractionalRenkoEngine:
    """Pre-market ATR → locked fractional brick → synthetic Renko Supertrend.

    Path-dependent Renko construction is iterative (unavoidable). Supertrend
    bands after the brick sequence are computed in a tight Python loop over
    typically a few hundred bricks per session — not the 375 minute bars.
    """

    def __init__(self, settings: Optional[BacktestSettings] = None) -> None:
        self.settings = settings or BacktestSettings()

    @staticmethod
    def true_range(
        high: np.ndarray,
        low: np.ndarray,
        prev_close: np.ndarray,
    ) -> np.ndarray:
        """Vectorised True Range: max(H-L, |H-Cprev|, |L-Cprev|)."""
        span = high - low
        up_gap = np.abs(high - prev_close)
        dn_gap = np.abs(low - prev_close)
        return np.maximum(span, np.maximum(up_gap, dn_gap))

    def daily_atr(
        self, daily: pd.DataFrame, asof: date, lookback: Optional[int] = None
    ) -> tuple[float, list[date]]:
        """Mean TR of the last ``lookback`` *completed* sessions before ``asof``."""
        n = lookback or self.settings.atr_lookback
        if daily.empty:
            raise StrategyError("No daily candles available for ATR")
        idx = daily.index
        dates = pd.DatetimeIndex(idx).tz_convert(IST_ZONE).date
        daily = daily.copy()
        daily["_session"] = dates
        completed = daily[daily["_session"] < asof]
        if len(completed) < n + 1:
            raise StrategyError(
                f"Need {n + 1} completed daily candles before {asof} "
                f"to compute ATR{n} (have {len(completed)})"
            )
        window = completed.iloc[-(n + 1) :]
        high = window["high"].to_numpy(dtype=float)
        low = window["low"].to_numpy(dtype=float)
        close = window["close"].to_numpy(dtype=float)
        prev_close = close[:-1]
        tr = self.true_range(high[1:], low[1:], prev_close)
        atr = float(np.mean(tr[-n:]))
        used = list(window["_session"].iloc[1:])
        return atr, used

    def brick_size(self, daily_atr_5: float) -> float:
        if daily_atr_5 <= 0 or not math.isfinite(daily_atr_5):
            raise StrategyError(f"Invalid Daily_ATR_5={daily_atr_5}")
        size = daily_atr_5 / self.settings.fractional_divisor
        if size <= 0 or not math.isfinite(size):
            raise StrategyError(f"Invalid static brick size {size}")
        return float(size)

    def _session_dates(self, daily: pd.DataFrame) -> pd.DataFrame:
        out = daily.copy()
        idx = pd.DatetimeIndex(out.index)
        if idx.tz is None:
            idx = idx.tz_localize(IST_ZONE)
        else:
            idx = idx.tz_convert(IST_ZONE)
        out["_session"] = idx.date
        return out

    def trend_filter(
        self, daily: pd.DataFrame, asof: date
    ) -> tuple[float, float, bool, bool]:
        """Prior close vs SMA of the last ``sma_filter_period`` completed days."""
        period = int(self.settings.sma_filter_period or 0)
        tagged = self._session_dates(daily)
        completed = tagged[tagged["_session"] < asof]
        if period <= 0:
            close = float(completed["close"].iloc[-1]) if len(completed) else 0.0
            return close, close, True, True
        if len(completed) < period:
            raise StrategyError(
                f"Need {period} completed daily closes before {asof} "
                f"for SMA filter (have {len(completed)})"
            )
        sma = float(completed["close"].iloc[-period:].mean())
        prior = float(completed["close"].iloc[-1])
        return prior, sma, prior >= sma, prior <= sma

    def session_baseline(
        self,
        daily: pd.DataFrame,
        trade_date: date,
        cash_to_lose: Optional[float] = None,
    ) -> SessionBaseline:
        atr, used = self.daily_atr(daily, trade_date)
        brick = self.brick_size(atr)
        stop_mult = float(self.settings.stop_atr_mult)
        if stop_mult <= 0 or not math.isfinite(stop_mult):
            raise StrategyError(f"Invalid stop ATR multiplier {stop_mult}")
        stop_distance = stop_mult * atr
        # Size off the stop, not 2×brick — otherwise cash_to_lose is fiction.
        risk = stop_distance
        budget = cash_to_lose if cash_to_lose is not None else self.settings.cash_to_lose
        raw_qty = math.floor(budget / risk) if risk > 0 else 0
        lot = max(int(self.settings.lot_size), 1)
        qty = (raw_qty // lot) * lot
        if qty <= 0:
            raise StrategyError(
                f"{trade_date}: quantity=0 (cash_to_lose={budget:.2f}, R={risk:.4f})"
            )
        prior, sma, allow_long, allow_short = self.trend_filter(daily, trade_date)
        return SessionBaseline(
            trade_date=trade_date,
            daily_atr_5=atr,
            static_brick_size=brick,
            quantity=qty,
            risk_per_unit=risk,
            lookback_days=used,
            stop_distance=stop_distance,
            sma20=sma,
            prior_close=prior,
            allow_long=allow_long,
            allow_short=allow_short,
        )

    def build_renko(
        self,
        minute_bars: pd.DataFrame,
        brick_size: float,
    ) -> pd.DataFrame:
        """Close-based synthetic Renko. Multiple bricks may print on one bar."""
        if minute_bars.empty:
            return pd.DataFrame(
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "direction",
                    "brick_index",
                ]
            )
        closes = minute_bars["close"].to_numpy(dtype=float)
        stamps = minute_bars.index.to_numpy()
        ref = float(closes[0])
        bricks: list[dict[str, Any]] = []
        brick_i = 0
        for ts, px in zip(stamps[1:], closes[1:]):
            if not math.isfinite(px):
                continue
            # Drain as many bricks as the close displacement allows.
            while True:
                if px >= ref + brick_size:
                    new_ref = ref + brick_size
                    bricks.append(
                        {
                            "timestamp": ts,
                            "open": ref,
                            "high": new_ref,
                            "low": ref,
                            "close": new_ref,
                            "direction": 1,
                            "brick_index": brick_i,
                        }
                    )
                    ref = new_ref
                    brick_i += 1
                    continue
                if px <= ref - brick_size:
                    new_ref = ref - brick_size
                    bricks.append(
                        {
                            "timestamp": ts,
                            "open": ref,
                            "high": ref,
                            "low": new_ref,
                            "close": new_ref,
                            "direction": -1,
                            "brick_index": brick_i,
                        }
                    )
                    ref = new_ref
                    brick_i += 1
                    continue
                break
        if not bricks:
            return pd.DataFrame(
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "direction",
                    "brick_index",
                ]
            )
        out = pd.DataFrame(bricks)
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).dt.tz_convert(
            IST_ZONE
        )
        return out

    def apply_supertrend(
        self,
        bricks: pd.DataFrame,
        brick_size: float,
        multiplier: Optional[float] = None,
    ) -> pd.DataFrame:
        """Classic Supertrend on the synthetic Renko sequence.

        ATR is replaced by the locked ``Static_Brick_Size``. Band ratchet:

        * Final upper decreases only, unless the previous close breached it.
        * Final lower increases only, unless the previous close breached it.

        Trend flips to Long when close is strictly above the final upper band
        and to Short when close is strictly below the final lower band.
        """
        if bricks.empty:
            return bricks
        m = float(
            self.settings.supertrend_multiplier if multiplier is None else multiplier
        )
        high = bricks["high"].to_numpy(dtype=float)
        low = bricks["low"].to_numpy(dtype=float)
        close = bricks["close"].to_numpy(dtype=float)
        median = (high + low) * 0.5
        basic_upper = median + m * brick_size
        basic_lower = median - m * brick_size

        n = len(bricks)
        final_upper = np.empty(n, dtype=float)
        final_lower = np.empty(n, dtype=float)
        trend = np.zeros(n, dtype=int)

        final_upper[0] = basic_upper[0]
        final_lower[0] = basic_lower[0]
        if close[0] > final_upper[0]:
            trend[0] = 1
        elif close[0] < final_lower[0]:
            trend[0] = -1
        else:
            trend[0] = 0

        for i in range(1, n):
            prev_fu = final_upper[i - 1]
            prev_fl = final_lower[i - 1]
            prev_c = close[i - 1]
            # Upper: decreases only; reset if previous close breached it.
            if basic_upper[i] < prev_fu or prev_c > prev_fu:
                final_upper[i] = basic_upper[i]
            else:
                final_upper[i] = prev_fu
            # Lower: increases only; reset if previous close breached it.
            if basic_lower[i] > prev_fl or prev_c < prev_fl:
                final_lower[i] = basic_lower[i]
            else:
                final_lower[i] = prev_fl

            if close[i] > final_upper[i]:
                trend[i] = 1
            elif close[i] < final_lower[i]:
                trend[i] = -1
            else:
                trend[i] = trend[i - 1] if trend[i - 1] != 0 else 0

        out = bricks.copy()
        out["median"] = median
        out["basic_upper"] = basic_upper
        out["basic_lower"] = basic_lower
        out["final_upper"] = final_upper
        out["final_lower"] = final_lower
        out["trend"] = trend
        out["supertrend"] = np.where(trend >= 0, final_lower, final_upper)
        return out

    def run_session(
        self,
        minute_bars: pd.DataFrame,
        baseline: SessionBaseline,
    ) -> pd.DataFrame:
        bricks = self.build_renko(minute_bars, baseline.static_brick_size)
        return self.apply_supertrend(bricks, baseline.static_brick_size)


# ─────────────────────────────────────────────────────────────────────────────
# BACKTESTER
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_time: datetime
    exit_time: datetime
    direction: int
    entry_price_raw: float
    exit_price_raw: float
    entry_price: float
    exit_price: float
    quantity: int
    pnl_gross: float
    pnl_net: float
    slippage_impact: float
    return_pct: float
    reason: str
    brick_size: float
    trade_date: date

    def as_row(self) -> dict[str, Any]:
        side = "LONG" if self.direction == 1 else "SHORT"
        return {
            "Entry_Time": self.entry_time,
            "Exit_Time": self.exit_time,
            "Direction": side,
            "Entry_Price": round(self.entry_price, 4),
            "Exit_Price": round(self.exit_price, 4),
            "PnL": round(self.pnl_net, 2),
            "Return_%": round(self.return_pct, 4),
            "Qty": self.quantity,
            "Gross_PnL": round(self.pnl_gross, 2),
            "Slippage": round(self.slippage_impact, 2),
            "Gross_Pts": round(self.direction * (self.exit_price_raw - self.entry_price_raw), 2),
            "Reason": self.reason,
            "Brick_Size": round(self.brick_size, 4),
            "Date": self.trade_date.isoformat(),
            "Hold_Min": round(
                (self.exit_time - self.entry_time).total_seconds() / 60.0, 1
            ),
        }


class Backtester:
    """Map Renko Supertrend flips onto 1-minute closes and simulate fills."""

    def __init__(
        self,
        engine: Optional[FractionalRenkoEngine] = None,
        settings: Optional[BacktestSettings] = None,
    ) -> None:
        self.settings = settings or BacktestSettings()
        self.engine = engine or FractionalRenkoEngine(self.settings)

    def _slip(self, price: float, direction: int, is_entry: bool) -> float:
        """Penalty against the trader on every fill.

        Point slippage (default 1.0 Nifty pt/side) takes precedence. The
        fractional model is retained only for explicit ``slippage_points=0``.
        """
        if self.settings.slippage_points > 0:
            pts = self.settings.slippage_points
            if is_entry:
                return price + pts if direction == 1 else price - pts
            return price - pts if direction == 1 else price + pts
        pct = self.settings.slippage_pct
        if is_entry:
            return price * (1.0 + pct) if direction == 1 else price * (1.0 - pct)
        return price * (1.0 - pct) if direction == 1 else price * (1.0 + pct)

    @staticmethod
    def _bar_time(ts: Any) -> dtime:
        if isinstance(ts, datetime):
            return ensure_ist(ts).time()
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(IST_ZONE)
        else:
            stamp = stamp.tz_convert(IST_ZONE)
        return stamp.time()

    @staticmethod
    def _to_datetime(ts: Any) -> datetime:
        if isinstance(ts, datetime):
            return ensure_ist(ts)
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(IST_ZONE)
        else:
            stamp = stamp.tz_convert(IST_ZONE)
        py = stamp.to_pydatetime()
        return ensure_ist(py)

    def _collapse_signals(self, bricks: pd.DataFrame) -> pd.DataFrame:
        """One row per originating 1-minute timestamp (last brick wins)."""
        if bricks.empty:
            return bricks
        cols = [
            "timestamp",
            "close",
            "trend",
            "direction",
            "final_upper",
            "final_lower",
        ]
        available = [c for c in cols if c in bricks.columns]
        last = bricks.groupby("timestamp", sort=True).tail(1)[available].copy()
        last["trend_prev"] = last["trend"].shift(1).fillna(0).astype(int)
        last["signal"] = 0
        changed = (last["trend"] != last["trend_prev"]) & (last["trend"] != 0)
        last.loc[changed, "signal"] = last.loc[changed, "trend"].astype(int)
        return last.reset_index(drop=True)

    @staticmethod
    def _confirm_trend(trend: np.ndarray, confirm: int) -> np.ndarray:
        """Hold a new Supertrend state until ``confirm`` consecutive bricks agree."""
        n = int(confirm)
        if n <= 1 or len(trend) == 0:
            return trend
        out = np.zeros(len(trend), dtype=int)
        run = 0
        last = 0
        held = 0
        for i, t in enumerate(int(x) for x in trend):
            if t != 0 and t == last:
                run += 1
            else:
                run = 1 if t != 0 else 0
                last = t
            if t != 0 and run >= n:
                held = t
            out[i] = held
        return out

    def _find_stop_hit(
        self,
        day_bars: pd.DataFrame,
        entry_time: datetime,
        until: datetime,
        direction: int,
        stop_px: float,
    ) -> Optional[tuple[datetime, float]]:
        """First 1-minute bar after entry whose range pierces the stop."""
        if day_bars.empty:
            return None
        entry_ts = pd.Timestamp(ensure_ist(entry_time))
        until_ts = pd.Timestamp(ensure_ist(until))
        idx = day_bars.index
        if getattr(idx, "tz", None) is None:
            idx = pd.DatetimeIndex(idx).tz_localize(IST_ZONE)
        window = day_bars.loc[(idx > entry_ts) & (idx <= until_ts)]
        if window.empty:
            return None
        if direction == 1:
            hits = window["low"].astype(float) <= stop_px
        else:
            hits = window["high"].astype(float) >= stop_px
        if not bool(hits.any()):
            return None
        hit_ts = hits.idxmax()
        return self._to_datetime(hit_ts), float(stop_px)

    def _square_off_price(self, minute_bars: pd.DataFrame) -> tuple[datetime, float]:
        if minute_bars.empty:
            raise StrategyError("No 1-minute bars to square off")
        cutoff = self.settings.square_off
        times = minute_bars.index
        eligible = minute_bars[
            (times.hour * 60 + times.minute)
            <= (cutoff.hour * 60 + cutoff.minute)
        ]
        if eligible.empty:
            row = minute_bars.iloc[0]
            return self._to_datetime(minute_bars.index[0]), float(row["close"])
        ts = eligible.index[-1]
        return self._to_datetime(ts), float(eligible.iloc[-1]["close"])

    def _close_trade(
        self,
        *,
        direction: int,
        entry_time: datetime,
        entry_raw: float,
        exit_time: datetime,
        exit_raw: float,
        quantity: int,
        brick_size: float,
        trade_date: date,
        reason: str,
    ) -> Trade:
        entry_px = self._slip(entry_raw, direction, is_entry=True)
        exit_px = self._slip(exit_raw, direction, is_entry=False)
        pnl_gross = direction * (exit_raw - entry_raw) * quantity
        pnl_net = direction * (exit_px - entry_px) * quantity
        notional = abs(entry_px) * quantity
        ret = (pnl_net / notional * 100.0) if notional else 0.0
        return Trade(
            entry_time=entry_time,
            exit_time=exit_time,
            direction=direction,
            entry_price_raw=entry_raw,
            exit_price_raw=exit_raw,
            entry_price=entry_px,
            exit_price=exit_px,
            quantity=quantity,
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
            slippage_impact=pnl_gross - pnl_net,
            return_pct=ret,
            reason=reason,
            brick_size=brick_size,
            trade_date=trade_date,
        )

    def _in_entry_window(self, ts: datetime) -> bool:
        t_min = ts.hour * 60 + ts.minute
        start = self.settings.entry_start
        end = self.settings.entry_end
        floor = start.hour * 60 + start.minute + max(
            int(self.settings.open_buffer_minutes or 0), 0
        )
        cap = end.hour * 60 + end.minute
        if self.settings.entry_cutoff is not None:
            cap = min(
                cap,
                self.settings.entry_cutoff.hour * 60
                + self.settings.entry_cutoff.minute,
            )
        return floor <= t_min <= cap

    def run_day(
        self,
        trade_date: date,
        minute_bars: pd.DataFrame,
        baseline: SessionBaseline,
    ) -> tuple[list[Trade], DayDiagnostics]:
        diag = DayDiagnostics(trade_date=trade_date)
        if minute_bars.empty:
            return [], diag
        day_bars = minute_bars.sort_index().copy()
        bricks = self.engine.run_session(day_bars, baseline)
        if bricks.empty:
            LOG.debug("%s: no Renko bricks formed", trade_date)
            return [], diag
        confirm = max(int(self.settings.confirm_bricks or 1), 1)
        if confirm > 1 and "trend" in bricks.columns:
            bricks = bricks.copy()
            bricks["trend"] = self._confirm_trend(
                bricks["trend"].to_numpy(dtype=int), confirm
            )
        signals = self._collapse_signals(bricks)
        diag.bricks_total = int(len(bricks))
        if not signals.empty and "signal" in signals.columns:
            diag.signal_flips = int((signals["signal"].astype(int) != 0).sum())
        so_ts, so_px = self._square_off_price(day_bars)
        so_minutes = so_ts.hour * 60 + so_ts.minute

        trades: list[Trade] = []
        position = 0
        entry_time: Optional[datetime] = None
        entry_raw = 0.0
        qty = baseline.quantity
        brick = baseline.static_brick_size
        stop_dist = float(baseline.stop_distance)
        cooldown_until: Optional[datetime] = None

        def stop_px_for(direction: int, raw: float) -> float:
            return raw - stop_dist if direction == 1 else raw + stop_dist

        def close_now(ts: datetime, px: float, reason: str) -> None:
            nonlocal position, entry_time, entry_raw, cooldown_until
            if position == 0 or entry_time is None:
                return
            trades.append(
                self._close_trade(
                    direction=position,
                    entry_time=entry_time,
                    entry_raw=entry_raw,
                    exit_time=ts,
                    exit_raw=px,
                    quantity=qty,
                    brick_size=brick,
                    trade_date=trade_date,
                    reason=reason,
                )
            )
            if reason == "STOP":
                diag.stop_hits += 1
                cooldown_until = ts + timedelta(
                    minutes=max(int(self.settings.stop_cooldown_minutes or 0), 0)
                )
            elif reason == "SQUARE_OFF_1520":
                diag.square_offs += 1
            elif reason == "SIGNAL_REVERSE":
                diag.signal_reversals += 1
            elif reason == "SIGNAL_EXIT":
                diag.signal_exits += 1
            position = 0
            entry_time = None

        def try_stop(until: datetime) -> bool:
            if position == 0 or entry_time is None or stop_dist <= 0:
                return False
            hit = self._find_stop_hit(
                day_bars,
                entry_time,
                until,
                position,
                stop_px_for(position, entry_raw),
            )
            if hit is None:
                return False
            close_now(hit[0], hit[1], "STOP")
            return True

        for row in signals.itertuples(index=False):
            ts = self._to_datetime(row.timestamp)
            t_min = ts.hour * 60 + ts.minute
            if t_min >= so_minutes:
                break
            if position != 0:
                if try_stop(ts):
                    # Stopped on this bar — do not reverse on the same print.
                    continue
            signal = int(row.signal)
            if signal == 0:
                continue
            if signal == position:
                diag.filtered_same_pos += 1
                continue
            # Outside the entry window: hold; stop + 15:20 square-off own the exit.
            if not self._in_entry_window(ts):
                diag.filtered_out_of_window += 1
                continue
            raw_px = float(row.close)
            with_trend = (
                (signal == 1 and baseline.allow_long)
                or (signal == -1 and baseline.allow_short)
            )
            # Counter-trend Supertrend flip: flatten, never reverse.
            if position != 0 and entry_time is not None and signal == -position:
                close_now(
                    ts,
                    raw_px,
                    "SIGNAL_EXIT" if not with_trend else "SIGNAL_REVERSE",
                )
                if not with_trend:
                    diag.filtered_regime += 1
                    continue
            if signal not in (1, -1):
                continue
            if not with_trend:
                diag.filtered_regime += 1
                continue
            if cooldown_until is not None and ts < cooldown_until:
                diag.filtered_cooldown += 1
                continue
            position = signal
            entry_time = ts
            entry_raw = raw_px
            diag.entries_taken += 1

        if position != 0 and entry_time is not None:
            if not try_stop(so_ts):
                close_now(so_ts, so_px, "SQUARE_OFF_1520")
        # Flips at/after square-off were never offered to the entry filters.
        if not signals.empty:
            for row in signals.itertuples(index=False):
                if int(row.signal) == 0:
                    continue
                ts = self._to_datetime(row.timestamp)
                t_min = ts.hour * 60 + ts.minute
                if t_min >= so_minutes:
                    diag.filtered_out_of_window += 1
        return trades, diag

    def run(
        self,
        daily: pd.DataFrame,
        minutes: pd.DataFrame,
        start: date,
        end: date,
        holidays: Optional[set[date]] = None,
    ) -> tuple[list[Trade], list[SessionBaseline], list[DayDiagnostics]]:
        if minutes.empty:
            raise StrategyError("No 1-minute history to backtest")
        sessions = daterange_trading_days(start, end, holidays)
        if not sessions:
            raise StrategyError(f"No trading days in {start} → {end}")

        min_idx = pd.DatetimeIndex(minutes.index)
        if min_idx.tz is None:
            minutes = minutes.copy()
            minutes.index = min_idx.tz_localize(IST_ZONE)
            min_idx = minutes.index
        session_dates = pd.Index(min_idx.tz_convert(IST_ZONE).date)

        trades: list[Trade] = []
        baselines: list[SessionBaseline] = []
        diagnostics: list[DayDiagnostics] = []
        for d in sessions:
            day_mask = session_dates == d
            day_bars = minutes.loc[day_mask]
            if day_bars.empty:
                LOG.debug("Skipping %s — no 1-minute bars", d)
                continue
            try:
                baseline = self.engine.session_baseline(
                    daily, d, self.settings.cash_to_lose
                )
            except StrategyError as exc:
                LOG.warning("Skipping %s: %s", d, exc)
                continue
            baselines.append(baseline)
            day_trades, day_diag = self.run_day(d, day_bars, baseline)
            diagnostics.append(day_diag)
            bias = (
                "LONG"
                if baseline.allow_long and not baseline.allow_short
                else "SHORT"
                if baseline.allow_short and not baseline.allow_long
                else "BOTH"
            )
            LOG.info(
                "%s  ATR=%.2f  brick=%.3f  stop=%.1f  qty=%d  SMA%s  trades=%d",
                d,
                baseline.daily_atr_5,
                baseline.static_brick_size,
                baseline.stop_distance,
                baseline.quantity,
                bias,
                len(day_trades),
            )
            trades.extend(day_trades)
        return trades, baselines, diagnostics


# ─────────────────────────────────────────────────────────────────────────────
# PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PerformanceReport:
    total_trades: int
    winning_trades: int
    losing_trades: int
    even_trades: int
    win_rate_pct: float
    gross_pnl: float
    slippage_impact: float
    net_pnl: float
    profit_factor: float
    max_drawdown_rupees: float
    max_drawdown_pct: float
    max_drawdown_points: float
    sharpe_ratio: float
    avg_win: float
    avg_loss: float
    expectancy: float
    trading_days: int
    trade_log: pd.DataFrame
    equity_curve: pd.Series
    daily_pnl: pd.Series
    gross_points: float = 0.0
    avg_slippage: float = 0.0
    scratch_trades: int = 0
    avg_hold_min: float = 0.0
    avg_qty: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "Total Trades": self.total_trades,
            "Winning Trades": self.winning_trades,
            "Losing Trades": self.losing_trades,
            "Win Rate (%)": round(self.win_rate_pct, 2),
            "Gross P&L (₹)": round(self.gross_pnl, 2),
            "Slippage Impact (₹)": round(self.slippage_impact, 2),
            "Net P&L (₹)": round(self.net_pnl, 2),
            "Gross P&L (pts)": round(self.gross_points, 2),
            "Profit Factor": round(self.profit_factor, 3)
            if math.isfinite(self.profit_factor)
            else "n/a",
            "Max Drawdown (₹)": round(self.max_drawdown_rupees, 2),
            "Max Drawdown (%)": round(self.max_drawdown_pct, 2),
            "Max Drawdown (pts)": round(self.max_drawdown_points, 2),
            "Sharpe (ann., 252d)": round(self.sharpe_ratio, 3)
            if math.isfinite(self.sharpe_ratio)
            else "n/a",
            "Avg Win (₹)": round(self.avg_win, 2),
            "Avg Loss (₹)": round(self.avg_loss, 2),
            "Expectancy (₹)": round(self.expectancy, 2),
            "Trading Days": self.trading_days,
        }


class PerformanceReporter:
    """Tear-sheet construction from the closed-trade blotter."""

    def __init__(self, capital: float = DEFAULT_CAPITAL) -> None:
        self.capital = float(capital)

    def build(self, trades: Sequence[Trade]) -> PerformanceReport:
        log_df = pd.DataFrame([t.as_row() for t in trades])
        if log_df.empty:
            empty_eq = pd.Series(dtype=float)
            return PerformanceReport(
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                even_trades=0,
                win_rate_pct=0.0,
                gross_pnl=0.0,
                slippage_impact=0.0,
                net_pnl=0.0,
                profit_factor=0.0,
                max_drawdown_rupees=0.0,
                max_drawdown_pct=0.0,
                max_drawdown_points=0.0,
                sharpe_ratio=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                expectancy=0.0,
                trading_days=0,
                trade_log=log_df,
                equity_curve=empty_eq,
                daily_pnl=empty_eq,
            )

        net = np.array([t.pnl_net for t in trades], dtype=float)
        gross = np.array([t.pnl_gross for t in trades], dtype=float)
        slip = np.array([t.slippage_impact for t in trades], dtype=float)
        pts = np.array(
            [
                t.direction * (t.exit_price_raw - t.entry_price_raw)
                for t in trades
            ],
            dtype=float,
        )
        wins = net[net > 0]
        losses = net[net < 0]
        even = int(np.sum(net == 0))
        gross_gains = float(wins.sum()) if len(wins) else 0.0
        gross_losses = float(-losses.sum()) if len(losses) else 0.0
        if gross_losses > 0:
            pf = gross_gains / gross_losses
        elif gross_gains > 0:
            pf = math.inf
        else:
            pf = 0.0

        # Equity starts at allocated capital so drawdown % is well-defined
        # even when the first trades are losers (peak-of-PnL would be ~0).
        equity = self.capital + np.cumsum(net)
        peak = np.maximum.accumulate(equity)
        dd = peak - equity
        max_dd = float(dd.max()) if len(dd) else 0.0
        with np.errstate(divide="ignore", invalid="ignore"):
            dd_pct_series = np.where(peak > 0, dd / peak * 100.0, 0.0)
        dd_pct = float(np.nanmax(dd_pct_series)) if len(dd_pct_series) else 0.0
        eq_pts = np.cumsum(pts)
        peak_pts = np.maximum.accumulate(eq_pts)
        max_dd_pts = float((peak_pts - eq_pts).max()) if len(eq_pts) else 0.0

        by_day: dict[date, float] = {}
        for t in trades:
            by_day[t.trade_date] = by_day.get(t.trade_date, 0.0) + t.pnl_net
        daily = pd.Series(by_day, name="daily_pnl").sort_index()
        if self.capital > 0 and len(daily) > 1 and float(daily.std(ddof=1) or 0) > 0:
            rets = daily / self.capital
            sharpe = float(rets.mean() / rets.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
        else:
            sharpe = 0.0

        n = len(trades)
        win_n = int(len(wins))
        lose_n = int(len(losses))
        equity_s = pd.Series(
            equity,
            index=pd.to_datetime([t.exit_time for t in trades]),
            name="equity",
        )
        holds = np.array(
            [
                (t.exit_time - t.entry_time).total_seconds() / 60.0
                for t in trades
            ],
            dtype=float,
        )
        rets = np.array([t.return_pct for t in trades], dtype=float)
        # Scratches: losers whose whole return is inside a 0.12% cost band
        # (round-trip 0.05%+0.05% prints at -0.10%).
        scratch = int(np.sum((rets < 0) & (np.abs(rets) <= 0.12)))
        return PerformanceReport(
            total_trades=n,
            winning_trades=win_n,
            losing_trades=lose_n,
            even_trades=even,
            win_rate_pct=(win_n / n * 100.0) if n else 0.0,
            gross_pnl=float(gross.sum()),
            slippage_impact=float(slip.sum()),
            net_pnl=float(net.sum()),
            profit_factor=pf,
            max_drawdown_rupees=max_dd,
            max_drawdown_pct=dd_pct,
            max_drawdown_points=max_dd_pts,
            sharpe_ratio=sharpe,
            avg_win=float(wins.mean()) if win_n else 0.0,
            avg_loss=float(losses.mean()) if lose_n else 0.0,
            expectancy=float(net.mean()) if n else 0.0,
            trading_days=int(daily.shape[0]),
            trade_log=log_df,
            equity_curve=equity_s,
            daily_pnl=daily,
            gross_points=float(pts.sum()),
            avg_slippage=float(slip.mean()) if n else 0.0,
            scratch_trades=scratch,
            avg_hold_min=float(holds.mean()) if n else 0.0,
            avg_qty=float(np.mean([t.quantity for t in trades])) if n else 0.0,
        )

    def print_tearsheet(
        self,
        report: PerformanceReport,
        baselines: Sequence[SessionBaseline] | None = None,
        settings: Optional[BacktestSettings] = None,
        source: str = "",
        diagnostics: Sequence[DayDiagnostics] | None = None,
    ) -> None:
        width = 78
        bar = "═" * width
        thin = "─" * width
        print()
        print(bar)
        print("  FRACTIONAL ATR RENKO SUPERTREND  —  STRATEGY TEAR-SHEET")
        print(bar)
        if settings:
            if settings.slippage_points and settings.slippage_points > 0:
                slip_txt = f"{settings.slippage_points:g} pts/fill"
            else:
                slip_txt = f"{settings.slippage_pct * 100:.3f}% per fill"
            print(
                f"  Instrument     : {settings.instrument_key}\n"
                f"  Cash to lose   : ₹{settings.cash_to_lose:,.2f}    "
                f"Capital: ₹{settings.capital:,.2f}\n"
                f"  Slippage       : {slip_txt}    "
                f"ST multiplier: {settings.supertrend_multiplier:g}\n"
                f"  Brick formula  : Daily_ATR / {settings.fractional_divisor:g}    "
                f"ATR lookback: {settings.atr_lookback}\n"
                f"  Stop           : {settings.stop_atr_mult:g} × ATR    "
                f"SMA filter: {settings.sma_filter_period}d\n"
                f"  Entry window   : {settings.entry_start.strftime('%H:%M')}"
                f"–{settings.entry_end.strftime('%H:%M')} IST    "
                f"Square-off: {settings.square_off.strftime('%H:%M')} IST"
            )
        if source:
            print(f"  Data source    : {source}")
        if baselines:
            bricks = [b.static_brick_size for b in baselines]
            atrs = [b.daily_atr_5 for b in baselines]
            print(
                f"  Sessions       : {len(baselines)}    "
                f"ATR5 μ={np.mean(atrs):.2f}    "
                f"Brick μ={np.mean(bricks):.3f}  "
                f"[{min(bricks):.3f} – {max(bricks):.3f}]"
            )
        if diagnostics:
            print_attrition_summary(diagnostics)
        print(thin)
        metrics = report.to_dict()
        keys = list(metrics.keys())
        for i in range(0, len(keys), 2):
            left_k = keys[i]
            left_v = metrics[left_k]
            left = f"{left_k:<22} {left_v:>14}"
            if i + 1 < len(keys):
                right_k = keys[i + 1]
                right_v = metrics[right_k]
                right = f"{right_k:<22} {right_v:>14}"
                print(f"  {left}    {right}")
            else:
                print(f"  {left}")
        print(thin)
        if report.trade_log.empty:
            print("  Trade log is empty — no signals fired in the window.")
        else:
            print("  TRADE LOG  [Entry_Time, Exit_Time, Direction, Entry, Exit, PnL, Return_%]")
            print(thin)
            show = report.trade_log[
                [
                    "Entry_Time",
                    "Exit_Time",
                    "Direction",
                    "Entry_Price",
                    "Exit_Price",
                    "PnL",
                    "Return_%",
                ]
            ].copy()
            preview_n = 40
            preview = show if len(show) <= preview_n else show.head(preview_n)
            with pd.option_context(
                "display.max_rows", preview_n + 5,
                "display.width", 120,
                "display.max_colwidth", 24,
            ):
                print(preview.to_string(index=False))
            if len(show) > preview_n:
                print(
                    f"  … {len(show) - preview_n} more rows omitted from console "
                    "(full blotter written to CSV)."
                )
        self._print_cost_diagnosis(report, baselines, settings)
        print(bar)
        print()

    def _print_cost_diagnosis(
        self,
        report: PerformanceReport,
        baselines: Sequence[SessionBaseline] | None,
        settings: Optional[BacktestSettings],
    ) -> None:
        if report.total_trades == 0:
            return
        thin = "─" * 78
        print(thin)
        print("  COST DIAGNOSIS")
        mean_px = 0.0
        if not report.trade_log.empty and "Entry_Price" in report.trade_log.columns:
            mean_px = float(report.trade_log["Entry_Price"].mean())
        brick_mu = (
            float(np.mean([b.static_brick_size for b in baselines]))
            if baselines
            else 0.0
        )
        risk_mu = (
            float(np.mean([b.risk_per_unit for b in baselines]))
            if baselines
            else (2.0 * brick_mu if brick_mu else 0.0)
        )
        if settings and settings.slippage_points and settings.slippage_points > 0:
            fill_pts = float(settings.slippage_points)
            model = f"{fill_pts:g} points/fill"
        else:
            pct = settings.slippage_pct if settings else SLIPPAGE_PCT
            fill_pts = mean_px * pct
            model = f"{pct * 100:.3f}% of spot ({fill_pts:.2f} pts/fill @ {mean_px:,.0f})"
        rt_pts = 2.0 * fill_pts
        friction_vs_risk = (rt_pts / risk_mu * 100.0) if risk_mu else 0.0
        print(
            f"  Cost model     : {model}\n"
            f"  Round-trip     : {rt_pts:.2f} Nifty pts   "
            f"Mean brick: {brick_mu:.2f}   PCR R: {risk_mu:.2f} pts\n"
            f"  Friction / R   : {friction_vs_risk:.0f}% of intended per-trade risk\n"
            f"  Avg slippage   : ₹{report.avg_slippage:,.0f}/trade   "
            f"Avg hold: {report.avg_hold_min:.0f} min   "
            f"Avg qty: {report.avg_qty:.0f}\n"
            f"  Gross (pre-cost): ₹{report.gross_pnl:,.0f}  ({report.gross_points:+.1f} pts)    "
            f"Friction: ₹{report.slippage_impact:,.0f}"
        )
        if report.trade_log is not None and not report.trade_log.empty:
            if "Reason" in report.trade_log.columns:
                counts = report.trade_log["Reason"].value_counts().to_dict()
                pretty = ", ".join(f"{k}={v}" for k, v in counts.items())
                print(f"  Exit mix       : {pretty}")
        if (
            settings
            and (not settings.slippage_points)
            and settings.slippage_pct >= 0.0004
            and report.slippage_impact > abs(report.gross_pnl)
        ):
            print(
                "  NOTE           : percent-of-spot slippage is ~12 pts/fill on Nifty.\n"
                "                   Default is 1 pt/fill; pass --slippage-points 1."
            )

    def save(self, report: PerformanceReport, prefix: str = "renko") -> Path:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = now_ist().strftime("%Y%m%d_%H%M%S")
        path = RESULTS_DIR / f"{prefix}_trades_{stamp}.csv"
        report.trade_log.to_csv(path, index=False)
        summary = RESULTS_DIR / f"{prefix}_summary_{stamp}.json"
        serialisable = {
            k: (None if isinstance(v, float) and not math.isfinite(v) else v)
            for k, v in report.to_dict().items()
        }
        summary.write_text(json.dumps(serialisable, indent=2), encoding="utf-8")
        LOG.info("Wrote trade log  → %s", path)
        LOG.info("Wrote summary    → %s", summary)
        return path


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────────────────────────────────────

class _Fail(AssertionError):
    pass


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise _Fail(msg)


def run_self_tests() -> int:
    """Deterministic unit checks — no network, no env.txt required."""
    print("Running Fractional ATR Renko Supertrend self-tests…")
    n_ok = 0

    # 1. True Range
    high = np.array([110.0, 120.0, 115.0])
    low = np.array([100.0, 105.0, 108.0])
    prev = np.array([102.0, 118.0])
    tr = FractionalRenkoEngine.true_range(high[1:], low[1:], prev)
    # max(120-105, |120-102|, |105-102|) = max(15, 18, 3) = 18
    _assert(abs(tr[0] - 18.0) < 1e-9, f"TR[0] expected 18, got {tr[0]}")
    # max(115-108, |115-118|, |108-118|) = max(7, 3, 10) = 10
    _assert(abs(tr[1] - 10.0) < 1e-9, f"TR[1] expected 10, got {tr[1]}")
    n_ok += 1

    # 2. Env loader — missing file
    try:
        load_env_file(Path("/tmp/does-not-exist-renko-env.txt"))
        raise _Fail("missing env.txt should raise")
    except EnvConfigError:
        n_ok += 1

    # 3. Env loader — happy path with comments / quotes
    tmp = BASE_DIR / ".renko_selftest_env.txt"
    try:
        tmp.write_text(
            "# comment\n"
            "UPSTOX_ACCESS_TOKEN='tok_abc'  # inline\n"
            "UPSTOX_API_KEY=key-1\n",
            encoding="utf-8",
        )
        creds = load_credentials(tmp)
        _assert(creds.access_token == "tok_abc", creds.access_token)
        _assert(creds.api_key == "key-1", creds.api_key)
        n_ok += 1
    finally:
        if tmp.exists():
            tmp.unlink()

    # 4. Brick size lock
    engine_legacy = FractionalRenkoEngine(BacktestSettings(fractional_divisor=12.5))
    _assert(abs(engine_legacy.brick_size(125.0) - 10.0) < 1e-12, "125/12.5 must be 10")
    engine = FractionalRenkoEngine()
    _assert(abs(engine.brick_size(125.0) - 20.0) < 1e-12, "125/6.25 must be 20")
    n_ok += 1

    # 5. PCR quantity
    settings = BacktestSettings(cash_to_lose=2_000.0)
    engine = FractionalRenkoEngine(settings)
    # fabricate 6 daily bars
    days = pd.date_range("2026-09-01", periods=8, freq="B", tz=IST_ZONE)
    daily = pd.DataFrame(
        {
            "open": 24_000.0,
            "high": [24100, 24200, 24350, 24280, 24400, 24520, 24480, 24600],
            "low": [23900, 24000, 24100, 24150, 24200, 24300, 24350, 24400],
            "close": [24050, 24150, 24200, 24240, 24380, 24490, 24410, 24550],
            "volume": 1.0,
            "oi": 0.0,
        },
        index=days,
    )
    asof = date(2026, 9, 11)
    atr, used = engine.daily_atr(daily, asof, lookback=5)
    _assert(len(used) == 5, f"lookback days {used}")
    _assert(atr > 0, f"ATR {atr}")
    brick = engine.brick_size(atr)
    qty = math.floor(2_000.0 / (2.0 * brick))
    _assert(qty >= 1, f"qty {qty} brick {brick}")
    n_ok += 1

    # 6. Renko: strictly rising closes produce only up-bricks of exact size
    idx = pd.date_range("2026-09-11 09:15", periods=40, freq="min", tz=IST_ZONE)
    px = 24_000 + np.arange(40) * 3.0
    minutes = pd.DataFrame(
        {"open": px, "high": px, "low": px, "close": px, "volume": 1.0, "oi": 0.0},
        index=idx,
    )
    bricks = engine.build_renko(minutes, brick_size=10.0)
    _assert(not bricks.empty, "expected up-bricks")
    _assert((bricks["direction"] == 1).all(), "all bricks should be up")
    _assert(
        np.allclose(bricks["close"] - bricks["open"], 10.0),
        "brick height must equal brick size",
    )
    n_ok += 1

    # 7. Supertrend eventually goes long on a sustained up-brick tape, then
    #    flips short after a long enough down run.
    up = engine.apply_supertrend(bricks, 10.0, multiplier=3.0)
    _assert(int(up["trend"].iloc[-1]) == 1, f"expected long, got {up['trend'].iloc[-1]}")
    # append a long down sequence
    last_close = float(bricks["close"].iloc[-1])
    last_ts = pd.Timestamp(bricks["timestamp"].iloc[-1])
    down_rows = []
    ref = last_close
    for i in range(20):
        new_ref = ref - 10.0
        last_ts = last_ts + pd.Timedelta(minutes=1)
        down_rows.append(
            {
                "timestamp": last_ts,
                "open": ref,
                "high": ref,
                "low": new_ref,
                "close": new_ref,
                "direction": -1,
                "brick_index": len(bricks) + i,
            }
        )
        ref = new_ref
    mixed = pd.concat([bricks, pd.DataFrame(down_rows)], ignore_index=True)
    st = engine.apply_supertrend(mixed, 10.0, multiplier=3.0)
    _assert(int(st["trend"].iloc[-1]) == -1, "expected short after reverse bricks")
    n_ok += 1

    # 8. Slippage + square-off + blotter identities
    pct_settings = BacktestSettings(
        cash_to_lose=2_000.0, slippage_pct=0.0005, slippage_points=0.0
    )
    bt_pct = Backtester(engine, pct_settings)
    slipped_buy = bt_pct._slip(10_000.0, 1, True)
    slipped_sell = bt_pct._slip(10_000.0, 1, False)
    _assert(abs(slipped_buy - 10_005.0) < 1e-9, slipped_buy)
    _assert(abs(slipped_sell - 9_995.0) < 1e-9, slipped_sell)
    pts_settings = BacktestSettings(slippage_pct=0.0, slippage_points=1.5)
    bt_pts = Backtester(engine, pts_settings)
    _assert(abs(bt_pts._slip(24_500.0, 1, True) - 24_501.5) < 1e-9, "points slip long entry")
    _assert(abs(bt_pts._slip(24_500.0, -1, True) - 24_498.5) < 1e-9, "points slip short entry")
    n_ok += 1

    # 9. End-to-end synthetic day produces a well-formed report
    holidays: set[date] = set()
    daily_s, minute_s = generate_synthetic_market(
        date(2026, 7, 1), n_days=36, seed=7, holidays=holidays
    )
    end = minute_s.index.tz_convert(IST_ZONE).date.max()
    unique_days = sorted(set(pd.DatetimeIndex(minute_s.index).tz_convert(IST_ZONE).date))
    trade_start = unique_days[22]
    bt = Backtester(engine, settings)
    trades, bases, diags = bt.run(daily_s, minute_s, trade_start, end, holidays)
    _assert(len(diags) == len(bases), "diagnostics/session mismatch")
    _assert(sum(d.entries_taken for d in diags) == len(trades), "entries_taken != trades")
    _assert(all(d.signal_flips >= 0 for d in diags), "negative flips")
    reporter = PerformanceReporter(capital=settings.capital)
    report = reporter.build(trades)
    _assert(report.total_trades == len(trades), "trade count mismatch")
    if trades:
        recon = sum(t.pnl_net for t in trades)
        _assert(abs(recon - report.net_pnl) < 1e-6, "net pnl mismatch")
        _assert(
            abs(report.gross_pnl - report.net_pnl - report.slippage_impact) < 1e-4,
            "gross - net != slippage",
        )
        _assert(
            set(report.trade_log.columns)
            >= {
                "Entry_Time",
                "Exit_Time",
                "Direction",
                "Entry_Price",
                "Exit_Price",
                "PnL",
                "Return_%",
            },
            "trade log missing required columns",
        )
        last_exit_times = [t.exit_time.time() for t in trades if t.reason == "SQUARE_OFF_1520"]
        for tm in last_exit_times:
            _assert(tm <= SQUARE_OFF, f"square-off after 15:20: {tm}")
    n_ok += 1

    # 10. Hard stop fires on 1-minute range, not on the next Supertrend flip.
    idx = pd.date_range("2026-09-11 09:45", periods=12, freq="min", tz=IST_ZONE)
    lows = np.full(12, 99.0)
    lows[4] = 90.0
    stop_df = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": lows,
            "close": 99.0,
            "volume": 1.0,
            "oi": 0.0,
        },
        index=idx,
    )
    hit = bt._find_stop_hit(
        stop_df,
        idx[0].to_pydatetime(),
        idx[-1].to_pydatetime(),
        1,
        95.0,
    )
    _assert(hit is not None, "expected a long stop hit")
    _assert(abs(hit[1] - 95.0) < 1e-9, f"stop fill {hit[1]}")
    _assert(hit[0].minute == idx[4].minute, f"stop bar {hit[0]}")
    n_ok += 1

    print(f"Self-tests passed: {n_ok}/10")
    return 0


def print_attrition_summary(diagnostics: Sequence[DayDiagnostics]) -> None:
    flips = sum(d.signal_flips for d in diagnostics)
    out_w = sum(d.filtered_out_of_window for d in diagnostics)
    regime = sum(d.filtered_regime for d in diagnostics)
    cool = sum(d.filtered_cooldown for d in diagnostics)
    same = sum(d.filtered_same_pos for d in diagnostics)
    taken = sum(d.entries_taken for d in diagnostics)

    def pct(n: int) -> str:
        return f"{(n / flips * 100.0):.0f}%" if flips else "n/a"

    print("  Signal attrition (all sessions)")
    print(f"    Flips seen            : {flips}")
    print(f"    └ outside entry window: {out_w}  ({pct(out_w)})")
    print(f"    └ against SMA regime  : {regime}  ({pct(regime)})")
    print(f"    └ inside stop cooldown: {cool}  ({pct(cool)})")
    print(f"    └ same as open pos    : {same}  ({pct(same)})")
    print(f"    Entries taken         : {taken}")


def _exit_counts(report: PerformanceReport) -> dict[str, int]:
    if report.trade_log is None or report.trade_log.empty or "Reason" not in report.trade_log.columns:
        return {
            "STOP": 0,
            "SIGNAL_REVERSE": 0,
            "SIGNAL_EXIT": 0,
            "SQUARE_OFF_1520": 0,
        }
    vc = report.trade_log["Reason"].astype(str).value_counts().to_dict()
    return {
        "STOP": int(vc.get("STOP", 0)),
        "SIGNAL_REVERSE": int(vc.get("SIGNAL_REVERSE", 0)),
        "SIGNAL_EXIT": int(vc.get("SIGNAL_EXIT", 0)),
        "SQUARE_OFF_1520": int(vc.get("SQUARE_OFF_1520", 0)),
    }


IN_SAMPLE_GATES = "IN_SAMPLE"
OOS_GATES = "OOS"


def evaluate_gates(
    report: PerformanceReport,
    diagnostics: Sequence[DayDiagnostics],
    gates: str = IN_SAMPLE_GATES,
) -> dict[str, tuple[bool, Any, str]]:
    """Return {gate_name: (passed, observed, threshold)}."""
    exits = _exit_counts(report)
    n = int(report.total_trades)
    non_so = n - int(exits["SQUARE_OFF_1520"])
    pf = report.profit_factor
    if gates == OOS_GATES:
        specs: list[tuple[str, bool, Any, str]] = [
            ("Total Trades (OOS)", n >= 10, n, ">=10"),
            ("Net P&L (OOS)", report.net_pnl > 0, round(report.net_pnl, 2), ">0"),
            (
                "Profit Factor (OOS)",
                math.isfinite(pf) and pf >= 1.10 or (not math.isfinite(pf) and pf > 0),
                (round(pf, 3) if math.isfinite(pf) else "inf"),
                ">=1.10",
            ),
            (
                "Max Drawdown % (OOS)",
                report.max_drawdown_pct <= 20.0,
                round(report.max_drawdown_pct, 2),
                "<=20",
            ),
        ]
    else:
        hold_ok = 30.0 <= float(report.avg_hold_min) <= 240.0
        pf_ok = (math.isfinite(pf) and pf >= 1.30) or (
            not math.isfinite(pf) and pf > 0
        )
        specs = [
            ("Total Trades", n >= 30, n, ">=30"),
            (
                "Profit Factor",
                pf_ok,
                (round(pf, 3) if math.isfinite(pf) else "inf"),
                ">=1.30",
            ),
            ("Net P&L (₹)", report.net_pnl > 0, round(report.net_pnl, 2), ">0"),
            (
                "Max Drawdown %",
                report.max_drawdown_pct <= 15.0,
                round(report.max_drawdown_pct, 2),
                "<=15.0",
            ),
            (
                "Avg Hold (min)",
                hold_ok,
                round(float(report.avg_hold_min), 1),
                ">=30 and <=240",
            ),
            (
                "Non-square-off exits",
                non_so >= 0.25 * n if n else False,
                non_so,
                ">=0.25 * total_trades",
            ),
        ]
    return {name: (passed, observed, thresh) for name, passed, observed, thresh in specs}


def gates_pass(result: dict[str, tuple[bool, Any, str]]) -> bool:
    return all(v[0] for v in result.values())


def print_gate_table(result: dict[str, tuple[bool, Any, str]], title: str = "In-sample gates") -> None:
    print(f"  {title}")
    for name, (passed, observed, thresh) in result.items():
        mark = "PASS" if passed else "FAIL"
        print(f"    {name:<24} {str(observed):>12}  need {thresh:<22} {mark}")
    print(f"  GATE: {'PASS' if gates_pass(result) else 'FAIL'}")


GRID_SPECS: list[tuple[str, str, dict[str, Any]]] = [
    ("G0", "(baseline)", {}),
    ("G1", "--sma-period 0", {"sma_period": 0}),
    (
        "G2",
        "--sma-period 0 --target-minutes 30",
        {"sma_period": 0, "target_minutes": 30},
    ),
    (
        "G3",
        "--sma-period 0 --target-minutes 20",
        {"sma_period": 0, "target_minutes": 20},
    ),
    (
        "G4",
        "--sma-period 0 --target-minutes 30 --stop-atr-mult 1.0",
        {"sma_period": 0, "target_minutes": 30, "stop_atr_mult": 1.0},
    ),
    (
        "G5",
        "--sma-period 0 --target-minutes 30 --stop-atr-mult 1.5",
        {"sma_period": 0, "target_minutes": 30, "stop_atr_mult": 1.5},
    ),
    (
        "G6",
        "--sma-period 10 --target-minutes 30",
        {"sma_period": 10, "target_minutes": 30},
    ),
    (
        "G7",
        "--sma-period 0 --target-minutes 30 --confirm-bricks 2",
        {"sma_period": 0, "target_minutes": 30, "confirm_bricks": 2},
    ),
    (
        "G8",
        "--sma-period 0 --target-minutes 30 --entry-start 10:00 --entry-end 14:00",
        {
            "sma_period": 0,
            "target_minutes": 30,
            "entry_start": dtime(10, 0),
            "entry_end": dtime(14, 0),
        },
    ),
]


def apply_grid_overrides(base: BacktestSettings, overrides: dict[str, Any]) -> BacktestSettings:
    kw: dict[str, Any] = {}
    if "sma_period" in overrides:
        kw["sma_filter_period"] = int(overrides["sma_period"])
    if "target_minutes" in overrides:
        kw["fractional_divisor"] = SESSION_MINUTES / max(int(overrides["target_minutes"]), 1)
    if "stop_atr_mult" in overrides:
        kw["stop_atr_mult"] = float(overrides["stop_atr_mult"])
    if "confirm_bricks" in overrides:
        kw["confirm_bricks"] = int(overrides["confirm_bricks"])
    if "entry_start" in overrides:
        kw["entry_start"] = overrides["entry_start"]
    if "entry_end" in overrides:
        kw["entry_end"] = overrides["entry_end"]
    return replace(base, **kw) if kw else base


def _run_config(
    settings: BacktestSettings,
    daily: pd.DataFrame,
    minutes: pd.DataFrame,
    from_date: date,
    to_date: date,
    holidays: set[date],
) -> tuple[list[Trade], list[SessionBaseline], list[DayDiagnostics], PerformanceReport, float]:
    t0 = time.monotonic()
    engine = FractionalRenkoEngine(settings)
    bt = Backtester(engine, settings)
    trades, baselines, diags = bt.run(daily, minutes, from_date, to_date, holidays)
    reporter = PerformanceReporter(capital=settings.capital)
    report = reporter.build(trades)
    elapsed = time.monotonic() - t0
    return trades, baselines, diags, report, elapsed


def grid_row_dict(
    gid: str,
    flags: str,
    report: PerformanceReport,
    diags: Sequence[DayDiagnostics],
    elapsed: float,
    gate_label: str,
) -> dict[str, Any]:
    exits = _exit_counts(report)
    pf = report.profit_factor
    return {
        "id": gid,
        "flags": flags,
        "total_trades": report.total_trades,
        "win_rate_pct": round(report.win_rate_pct, 4),
        "gross_pnl": round(report.gross_pnl, 2),
        "net_pnl": round(report.net_pnl, 2),
        "profit_factor": (round(pf, 4) if math.isfinite(pf) else "inf"),
        "max_dd_pct": round(report.max_drawdown_pct, 4),
        "max_dd_rupees": round(report.max_drawdown_rupees, 2),
        "sharpe": (round(report.sharpe_ratio, 4) if math.isfinite(report.sharpe_ratio) else "inf"),
        "avg_hold_min": round(float(report.avg_hold_min), 2),
        "avg_qty": round(float(report.avg_qty), 4),
        "exits_stop": exits["STOP"],
        "exits_signal_reverse": exits["SIGNAL_REVERSE"],
        "exits_signal_exit": exits["SIGNAL_EXIT"],
        "exits_square_off": exits["SQUARE_OFF_1520"],
        "flips_seen": sum(d.signal_flips for d in diags),
        "filtered_out_of_window": sum(d.filtered_out_of_window for d in diags),
        "filtered_regime": sum(d.filtered_regime for d in diags),
        "filtered_cooldown": sum(d.filtered_cooldown for d in diags),
        "run_seconds": round(elapsed, 3),
        "in_sample_gate": gate_label,
    }


def run_grid(
    base_settings: BacktestSettings,
    daily: pd.DataFrame,
    minutes: pd.DataFrame,
    from_date: date,
    to_date: date,
    holidays: set[date],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print()
    print(f"GRID  {from_date} → {to_date}  ({len(GRID_SPECS)} configs, one fetch)")
    for gid, flags, overrides in GRID_SPECS:
        settings = apply_grid_overrides(base_settings, overrides)
        _trades, _base, diags, report, elapsed = _run_config(
            settings, daily, minutes, from_date, to_date, holidays
        )
        gate = evaluate_gates(report, diags, IN_SAMPLE_GATES)
        label = "PASS" if gates_pass(gate) else "FAIL"
        row = grid_row_dict(gid, flags, report, diags, elapsed, label)
        rows.append(row)
        print()
        print(f"  {gid}  {flags}  [{label}]  {elapsed:.1f}s")
        print(
            f"    trades={report.total_trades}  win={report.win_rate_pct:.2f}%  "
            f"net=₹{report.net_pnl:.2f}  PF={row['profit_factor']}  "
            f"DD={report.max_drawdown_pct:.2f}%  hold={report.avg_hold_min:.1f}m"
        )
        print_gate_table(gate)
    df = pd.DataFrame(rows)
    stamp = now_ist().strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"renko_grid_{stamp}.csv"
    df.to_csv(path, index=False)
    print()
    print(f"Grid CSV → {path}")
    print(df.to_string(index=False))
    return df


def run_walk_forward(
    gid: str,
    base_settings: BacktestSettings,
    daily: pd.DataFrame,
    minutes: pd.DataFrame,
    holidays: set[date],
) -> str:
    spec = next((s for s in GRID_SPECS if s[0] == gid), None)
    if spec is None:
        raise StrategyError(f"Unknown grid id {gid}")
    _gid, flags, overrides = spec
    settings = apply_grid_overrides(base_settings, overrides)
    is_from, is_to = date(2026, 4, 1), date(2026, 6, 30)
    oos_from, oos_to = date(2026, 7, 1), date(2026, 9, 30)
    _t, _b, is_diag, is_report, _ = _run_config(
        settings, daily, minutes, is_from, is_to, holidays
    )
    _t, _b, oos_diag, oos_report, _ = _run_config(
        settings, daily, minutes, oos_from, oos_to, holidays
    )
    oos_gate = evaluate_gates(oos_report, oos_diag, OOS_GATES)
    verdict = "PASS" if gates_pass(oos_gate) else "FAIL"
    is_pf = is_report.profit_factor
    oos_pf = oos_report.profit_factor

    def fmt_pf(x: float) -> str:
        return f"{x:.2f}" if math.isfinite(x) else "inf"

    lines = [
        f"  Walk-forward: {gid}",
        f"  flags: {flags}",
        "  ────────────────────────────────────────────────────────",
        f"  {'Metric':<20} {'In-sample':>12} {'Out-of-sample':>16}   Gate",
        f"  {'Total Trades':<20} {is_report.total_trades:>12} {oos_report.total_trades:>16}   >=10",
        f"  {'Net P&L (₹)':<20} {is_report.net_pnl:>12.2f} {oos_report.net_pnl:>16.2f}   >0",
        f"  {'Profit Factor':<20} {fmt_pf(is_pf):>12} {fmt_pf(oos_pf):>16}   >=1.10",
        f"  {'Max DD %':<20} {is_report.max_drawdown_pct:>12.1f} {oos_report.max_drawdown_pct:>16.1f}   <=20",
        "  ────────────────────────────────────────────────────────",
        f"  VERDICT: {verdict}",
    ]
    if verdict == "FAIL":
        lines.append("  Do not re-tune on the out-of-sample window.")
    text = "\n".join(lines)
    print()
    print(text)
    print()
    return text


# ─────────────────────────────────────────────────────────────────────────────
# CLI / MAIN
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


def _parse_hhmm(text: str) -> dtime:
    hh, mm = str(text).strip().split(":")
    return dtime(int(hh), int(mm))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fractional ATR Renko Supertrend backtester (Upstox v2)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--from-date", type=_parse_date, default=None, help="YYYY-MM-DD")
    p.add_argument("--to-date", type=_parse_date, default=None, help="YYYY-MM-DD")
    p.add_argument("--days", type=int, default=15, help="Lookback trading days if dates omitted")
    p.add_argument("--instrument", default=DEFAULT_INSTRUMENT_KEY, help="Upstox instrument_key")
    p.add_argument("--cash-to-lose", type=float, default=DEFAULT_CASH_TO_LOSE)
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    p.add_argument("--lot-size", type=int, default=1)
    p.add_argument("--slippage", type=float, default=SLIPPAGE_PCT, help="Fractional penalty (0 disables)")
    p.add_argument(
        "--slippage-points",
        type=float,
        default=SLIPPAGE_POINTS,
        help="Absolute Nifty points per fill; >0 overrides --slippage %%",
    )
    p.add_argument("--open-buffer", type=int, default=0, help="Extra minutes after entry-start to skip")
    p.add_argument("--entry-start", default="09:45", help="HH:MM IST — first allowed entry")
    p.add_argument("--entry-end", default="14:30", help="HH:MM IST — last allowed entry (exits still fire)")
    p.add_argument("--entry-cutoff", default=None, help="HH:MM IST — optional extra entry cap")
    p.add_argument("--confirm-bricks", type=int, default=1, help="Bricks that must agree before a Supertrend flip trades")
    p.add_argument("--atr-lookback", type=int, default=ATR_LOOKBACK)
    p.add_argument("--target-minutes", type=int, default=TARGET_TREND_MINUTES, help="Renko target TF; brick = ATR / (375/TF)")
    p.add_argument("--stop-atr-mult", type=float, default=STOP_ATR_MULT)
    p.add_argument("--sma-period", type=int, default=SMA_FILTER_PERIOD, help="0 disables the daily SMA filter")
    p.add_argument(
        "--realistic",
        action="store_true",
        help="Force 1 pt/fill (already the default cost model)",
    )
    p.add_argument("--multiplier", type=float, default=SUPERTREND_MULTIPLIER)
    p.add_argument("--env-file", type=Path, default=ENV_FILE)
    p.add_argument("--synthetic", action="store_true", help="Skip Upstox; use generated Nifty-like data")
    p.add_argument("--no-synthetic-fallback", action="store_true", help="Fail if API/token unavailable")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--seed", type=int, default=42, help="Synthetic RNG seed")
    p.add_argument("--grid", action="store_true", help="Run the fixed 9-row diagnostic grid")
    p.add_argument(
        "--walk-forward",
        default=None,
        metavar="ID",
        help="Walk-forward a grid id (G0–G8) on frozen IS/OOS windows",
    )
    return p.parse_args(argv)


def _default_window(n_days: int, holidays: set[date]) -> tuple[date, date]:
    end = today_ist()
    # Prefer yesterday so we never request a partial live session.
    if now_ist().time() < dtime(15, 30):
        end = end - timedelta(days=1)
    collected: list[date] = []
    cursor = end
    guard = 0
    while len(collected) < n_days and guard < 400:
        if is_trading_day(cursor, holidays):
            collected.append(cursor)
        cursor -= timedelta(days=1)
        guard += 1
    if not collected:
        return end - timedelta(days=n_days), end
    return collected[-1], collected[0]


def _load_data(
    args: argparse.Namespace,
    settings: BacktestSettings,
    holidays: set[date],
    from_date: date,
    to_date: date,
) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    if args.synthetic:
        LOG.info("Using synthetic market data (seed=%s)", args.seed)
        # Extra warm-up days so ATR lookback is defined on day 1 of the window.
        warmup = from_date - timedelta(days=55)
        span = (to_date - warmup).days + 2
        daily, minutes = generate_synthetic_market(
            warmup, n_days=max(span, args.days + 30), seed=args.seed, holidays=holidays
        )
        return daily, minutes, "SYNTHETIC"

    creds: Optional[EnvCredentials] = None
    try:
        creds = load_credentials(args.env_file)
    except EnvConfigError as exc:
        if args.no_synthetic_fallback:
            raise
        LOG.warning("%s — falling back to synthetic data", exc)
        warmup = from_date - timedelta(days=55)
        span = (to_date - warmup).days + 2
        daily, minutes = generate_synthetic_market(
            warmup, n_days=max(span, args.days + 30), seed=args.seed, holidays=holidays
        )
        return daily, minutes, "SYNTHETIC (no env.txt token)"

    loader = UpstoxDataLoader(creds, settings)
    daily_from = from_date - timedelta(days=60)
    try:
        daily = loader.fetch_daily(daily_from, to_date, use_cache=not args.no_cache)
        minutes = loader.fetch_intraday_1m(
            from_date, to_date, use_cache=not args.no_cache
        )
    except UpstoxDataError as exc:
        if args.no_synthetic_fallback:
            raise
        LOG.warning("Upstox download failed (%s) — synthetic fallback", exc)
        warmup = from_date - timedelta(days=55)
        span = (to_date - warmup).days + 2
        daily, minutes = generate_synthetic_market(
            warmup, n_days=max(span, args.days + 30), seed=args.seed, holidays=holidays
        )
        return daily, minutes, "SYNTHETIC (Upstox error fallback)"

    if minutes.empty:
        if args.no_synthetic_fallback:
            raise UpstoxDataError("Upstox returned no 1-minute candles")
        LOG.warning("Empty 1-minute payload — synthetic fallback")
        warmup = from_date - timedelta(days=55)
        span = (to_date - warmup).days + 2
        daily, minutes = generate_synthetic_market(
            warmup, n_days=max(span, args.days + 30), seed=args.seed, holidays=holidays
        )
        return daily, minutes, "SYNTHETIC (empty Upstox 1-minute)"
    return daily, minutes, f"UPSTOX {settings.instrument_key}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    setup_logging(args.verbose)
    if args.self_test:
        try:
            return run_self_tests()
        except _Fail as exc:
            print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
            return 1

    holidays = load_nse_holidays()
    if args.walk_forward:
        from_date, to_date = date(2026, 4, 1), date(2026, 9, 30)
    elif args.from_date and args.to_date:
        from_date, to_date = args.from_date, args.to_date
    elif args.from_date:
        from_date, to_date = args.from_date, today_ist()
    elif args.to_date:
        from_date, to_date = _default_window(args.days, holidays)[0], args.to_date
    else:
        from_date, to_date = _default_window(args.days, holidays)
    if from_date > to_date:
        print("from-date must be on or before to-date", file=sys.stderr)
        return 2

    entry_cutoff: Optional[dtime] = None
    if args.entry_cutoff:
        entry_cutoff = _parse_hhmm(args.entry_cutoff)
    slippage_pct = float(args.slippage)
    slippage_points = float(args.slippage_points or 0.0)
    open_buffer = int(args.open_buffer or 0)
    confirm_bricks = max(int(args.confirm_bricks or 1), 1)
    if args.realistic:
        slippage_pct = 0.0
        slippage_points = max(slippage_points, 1.0)
    target_minutes = max(int(args.target_minutes), 1)
    settings = BacktestSettings(
        instrument_key=args.instrument,
        cash_to_lose=float(args.cash_to_lose),
        capital=float(args.capital),
        slippage_pct=slippage_pct,
        slippage_points=slippage_points,
        supertrend_multiplier=float(args.multiplier),
        atr_lookback=max(int(args.atr_lookback), 2),
        fractional_divisor=SESSION_MINUTES / target_minutes,
        lot_size=max(int(args.lot_size), 1),
        entry_start=_parse_hhmm(args.entry_start),
        entry_end=_parse_hhmm(args.entry_end),
        open_buffer_minutes=open_buffer,
        entry_cutoff=entry_cutoff,
        confirm_bricks=confirm_bricks,
        stop_atr_mult=float(args.stop_atr_mult),
        sma_filter_period=max(int(args.sma_period), 0),
    )

    print()
    print("Fractional ATR Renko Supertrend backtester")
    print(f"  Window     : {from_date} → {to_date}")
    print(f"  Instrument : {settings.instrument_key}")
    print(f"  PCR budget : ₹{settings.cash_to_lose:,.0f}   capital ₹{settings.capital:,.0f}")

    try:
        daily, minutes, source = _load_data(args, settings, holidays, from_date, to_date)
    except (EnvConfigError, UpstoxDataError) as exc:
        LOG.error("%s", exc)
        return 2

    LOG.info("Daily bars=%d  1-minute bars=%d  source=%s", len(daily), len(minutes), source)

    if args.grid:
        try:
            run_grid(settings, daily, minutes, from_date, to_date, holidays)
        except StrategyError as exc:
            LOG.error("%s", exc)
            return 3
        return 0

    if args.walk_forward:
        try:
            run_walk_forward(
                str(args.walk_forward).upper(),
                settings,
                daily,
                minutes,
                holidays,
            )
        except StrategyError as exc:
            LOG.error("%s", exc)
            return 3
        return 0

    engine = FractionalRenkoEngine(settings)
    bt = Backtester(engine, settings)
    try:
        trades, baselines, diagnostics = bt.run(
            daily, minutes, from_date, to_date, holidays
        )
    except StrategyError as exc:
        LOG.error("%s", exc)
        return 3

    reporter = PerformanceReporter(capital=settings.capital)
    report = reporter.build(trades)
    reporter.print_tearsheet(
        report, baselines, settings, source=source, diagnostics=diagnostics
    )
    try:
        reporter.save(report)
    except OSError as exc:
        LOG.warning("Could not persist results: %s", exc)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
