#!/usr/bin/env python3
"""NSE residual-momentum stock screener (selection only; never places orders).

Method implemented:
  * India-aligned FF3 inputs (Mkt-RF, SMB, HML) plus RF from a pinned Indian
    factor provider (SCDLDS by default, IIMA optional); no US factors are silently substituted.
  * 36 complete monthly excess-return observations to estimate OLS alpha/betas.
  * Residual formation months are t-12 through t-2 (11 observations); t-1 is
    skipped in the signal. The estimated intercept is not subtracted when the
    signal residuals are computed, matching the supplied specification.
  * Score = sum(formation residuals) / sample standard deviation of those
    residuals. Stocks above the residual-volatility cap are excluded.
  * Select up to 50 long-side names, use inverse residual-volatility weights,
    and optionally apply entry/exit rank buffers from a user-supplied holdings CSV.
  * Supertrend (ATR period 10, multiplier 3) is computed for every stock with
    price history on BOTH timeframes, and the top-50 table reports whether each
    is currently POSITIVE or NEGATIVE plus the bar it last flipped: weekly bars
    are resampled from the daily OHLCV already downloaded (no extra API calls)
    and daily bars are the downloaded sessions themselves, both using Wilder ATR
    and the standard +/-1 flip rule. The daily flip is the finer-grained trend
    change; the weekly one is the slower confirmation.
  * A daily JMA/DWMA crossover screen runs alongside them on the same daily
    closes: a lag-reduced Jurik-style JMA (length 7, phase -40, power 0.35) is
    the fast line and a double weighted moving average (length 20) is the slow
    line. The first 100 bars are warm-up so the JMA can stabilise; from bar 101
    a day is a POSITIVE (bullish) crossover when JMA[i-1] <= DWMA[i-1] and
    JMA[i] > DWMA[i], and a NEGATIVE (bearish) crossover when JMA[i-1] >=
    DWMA[i-1] and JMA[i] < DWMA[i]. Anything else is no crossover.
  * The selected names are displayed sorted by that crossover day, not by the
    Supertrend flip: --crossover-sort desc (the default) shows the most recent
    JMA/DWMA cross first, asc shows the oldest (longest-running) cross first,
    and score restores the raw momentum ranking.
  * After the main screen, a separate entry-candidate review checks every
    eligible name with residual score >= 6 (not only the displayed basket):
    weekly Supertrend POS, a Positive JMA/DWMA cross on the latest completed
    daily bar, close above EMA(20), volume >= 1.2x the prior 20-session average,
    and close no more than 8% above EMA(20) are required for BUY. A strong-score
    name missing an entry trigger is WATCH; weekly/daily Supertrend NEG, JMA
    below DWMA, or close below EMA(20) is classified AVOID/WAIT as deterioration.
  * Report portfolio beta and an informational gross-exposure scale; never place
    orders or persist/change a real portfolio.

All market-data requests are GETs. This is a console-only informational screener.
"""

from __future__ import annotations

import argparse
import filecmp
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests


PROJECT_DIR = Path(__file__).resolve().parent
STOCK_DATA_DIR = PROJECT_DIR / "stock-data"
UNIVERSE_FILE = STOCK_DATA_DIR / "universe.json"


def load_universe_file(path: Path = UNIVERSE_FILE) -> tuple[str, ...]:
    """Load and validate NSE symbols from the editable stock-data/universe.json."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ValueError(f"Could not read stock universe JSON at {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stock universe JSON is invalid at {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError(f"Stock universe JSON must contain an object: {path}")
    if payload.get("exchange") != "NSE" or payload.get("segment") != "NSE_EQ":
        raise ValueError("Stock universe must declare exchange='NSE' and segment='NSE_EQ'.")
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise ValueError("Stock universe JSON 'symbols' must be a non-empty array.")
    if any(not isinstance(symbol, str) or not symbol.strip() for symbol in raw_symbols):
        raise ValueError("Every stock universe symbol must be a non-empty string.")
    symbols = tuple(symbol.strip().upper() for symbol in raw_symbols)
    if len(set(symbols)) != len(symbols):
        raise ValueError("Stock universe JSON contains duplicate symbols.")
    declared_count = payload.get("count")
    if declared_count != len(symbols):
        raise ValueError(
            f"Stock universe JSON count says {declared_count!r}, but symbols contains {len(symbols)} entries. "
            "Update the count when editing the list."
        )
    return symbols


try:
    UNIVERSE = load_universe_file()
    UNIVERSE_LOAD_ERROR: Optional[str] = None
except ValueError as exc:
    UNIVERSE = ()
    UNIVERSE_LOAD_ERROR = str(exc)

INDIA_TZ = ZoneInfo("Asia/Kolkata")
UPSTOX_API_BASE = "https://api.upstox.com"
NSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
IIMA_LANDING_PAGE = "https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/"
IIMA_DATA_BASE = "https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/DATA/"
SCDLDS_LANDING_PAGE = "https://quantfin.scdlds.com/"
SCDLDS_FF5_FALLBACK_URL = "https://quantfin.scdlds.com/Data/Factor_Data/ff5.csv"
SCDLDS_DOWNLOAD_PATTERN = re.compile(
    r'''downloadFile\(\s*['"]([^'"]*ff5\.csv)['"]\s*\)''',
    flags=re.IGNORECASE,
)
FACTOR_FILENAME_PATTERN = re.compile(
    r"(\d{4}-\d{2}_FourFactors_and_Market_Returns_Monthly_SurvivorshipBiasAdjusted\.csv)",
    flags=re.IGNORECASE,
)

REGRESSION_MONTHS = 36
FORMATION_RESIDUAL_MONTHS = 11  # t-12 through t-2; t-1 is excluded.
MIN_PRICE_INR = 20.0
LIQUIDITY_LOOKBACK_SESSIONS = 63  # approximately three months of NSE sessions.
DEFAULT_MIN_MEDIAN_DAILY_TURNOVER_INR = 500_000_000.0  # ₹50 crore/day.
DEFAULT_MAX_MONTHLY_RESIDUAL_VOLATILITY = 0.15
DEFAULT_PORTFOLIO_SIZE = 50
ENTRY_BUFFER_FRACTION = 0.08
EXIT_BUFFER_FRACTION = 0.15
BETA_LOWER_BOUND = 0.7
BETA_UPPER_BOUND = 1.3
DEFAULT_MAX_FACTOR_STALENESS_MONTHS = 2
DEFAULT_MIN_ELIGIBLE_UNIVERSE = 30
MAX_LAST_DAILY_BAR_AGE_DAYS = 10
DEFAULT_REQUESTS_PER_SECOND = 5.0  # Below the published API limit, with headroom.
DEFAULT_PRICE_HISTORY_EXTRA_MONTHS = 0
DEFAULT_MAX_WORKERS = 6
DEFAULT_CACHE_TTL_HOURS = 8.0
DEFAULT_CACHE_DIR = STOCK_DATA_DIR / "cache"
DEFAULT_FACTOR_CACHE_DIR = STOCK_DATA_DIR / "factors"
DEFAULT_ENV_FILE = STOCK_DATA_DIR / "env.txt"
LEGACY_ENV_FILE = PROJECT_DIR / "env.txt"

# Supertrend settings (the requested 10 & 3). The same ATR period/multiplier is
# applied to weekly bars and to daily bars; only the timeframe differs.
DEFAULT_SUPERTREND_PERIOD = 10
DEFAULT_SUPERTREND_MULTIPLIER = 3.0
WEEKLY_RESAMPLE_RULE = "W-FRI"  # NSE trading week, week ending Friday.
SUPERTREND_TIMEFRAMES = ("weekly", "daily")

# Daily JMA/DWMA crossover settings (the requested 7 / -40 / 0.35 / 20).
DEFAULT_JMA_LENGTH = 7
DEFAULT_JMA_PHASE = -40.0
DEFAULT_JMA_POWER = 0.35
DEFAULT_DWMA_LENGTH = 20
DEFAULT_CROSSOVER_WARMUP_BARS = 100  # JMA warm-up; detection starts on bar 101.
CROSSOVER_FRESH_SESSIONS = 5         # basket summary tally: crosses in the last trading week.

# Separate residual-momentum + technical entry-candidate screen.
ENTRY_MIN_RESIDUAL_SCORE = 6.0
ENTRY_EMA_LENGTH = 20
ENTRY_VOLUME_LOOKBACK = 20
ENTRY_MIN_VOLUME_MULTIPLE = 1.2
ENTRY_MAX_EMA_EXTENSION = 1.08
ENTRY_STATUS_ORDER = ("BUY", "WATCH", "AVOID/WAIT")
JMA_PHASE_LIMIT = 100.0              # phase is only defined on [-100, 100].
JMA_BETA_COEFFICIENT = 0.45          # beta = 0.45*(L-1) / (0.45*(L-1) + 2)
JMA_VOLTY_LAG_BARS = 10              # vsum adds div * (volty - volty[10])
JMA_VOLTY_DIVISOR = 0.1              # the "div" of the volty sum
JMA_AVOLTY_MIN_LOOKBACK = 30         # avolty factor = 2 / (max(4*L, 30) + 1)
JMA_POW1_FLOOR = 0.5                 # pow1 = max(len1 - 2, 0.5)
JMA_LEN1_FLOOR = 0.0                 # len1 = max(log2(sqrt((L-1)/2)) + 2, 0)

# Table order for the selected names, keyed on the daily JMA/DWMA crossover day:
# "desc" = most recent cross first, "asc" = oldest cross first,
# "score" = keep the residual-momentum order.
DEFAULT_CROSSOVER_SORT = "desc"
CROSSOVER_SORT_OPTIONS = ("asc", "desc", "score")
CROSSOVER_SORT_LABELS = {
    "asc": "by daily JMA/DWMA crossover date, ascending (oldest cross first, freshest cross last)",
    "desc": "by daily JMA/DWMA crossover date, descending (most recent cross first)",
    "score": "by residual-momentum score (crossover and Supertrend shown for reference only)",
}
# Legacy sort constants/functions remain available for callers of the old weekly
# Supertrend API, but neither the screen nor the default row order uses them.
DEFAULT_SUPERTREND_SORT = "desc"
SUPERTREND_SORT_OPTIONS = CROSSOVER_SORT_OPTIONS
SUPERTREND_SORT_LABELS = {
    "asc": "by weekly Supertrend change date, ascending (oldest trend first)",
    "desc": "by weekly Supertrend change date, descending (most recent flip first)",
    "score": "by residual-momentum score",
}
# Supertrend no longer drives the table order; the legacy knob is still accepted
# and maps onto CROSSOVER_SORT so old command lines keep working.
LEGACY_SUPERTREND_SORT_ENV = "SUPERTREND_SORT"


class ScreenerError(RuntimeError):
    """Base class for user-facing screening failures."""


class DataQualityError(ScreenerError):
    """Raised when inputs do not support a defensible score."""


class UpstoxAuthenticationError(ScreenerError):
    """Raised when the Upstox access token is rejected."""


class UpstoxRequestError(ScreenerError):
    """Raised when a market-data request fails."""


def console_status(message: str) -> None:
    """Write flushed progress updates to stderr, keeping stdout table-friendly."""
    print(message, file=sys.stderr, flush=True)


def load_env_values(primary: Path, fallback: Path = LEGACY_ENV_FILE) -> dict[str, str]:
    """Read screener env files without moving or writing project root env.txt.

    Primary is normally stock-data/env.txt. If the access token is missing
    there, values from the trading engine's root env.txt are filled in
    read-only — never shutil.move / overwrite of either file.
    """
    values = read_env_file(primary) if primary.exists() else {}
    need_token = not (values.get("UPSTOX_ACCESS_TOKEN") or "").strip()
    if not need_token:
        return values
    try:
        same = primary.resolve() == fallback.resolve()
    except OSError:
        same = False
    if same or not fallback.exists():
        return values
    legacy = read_env_file(fallback)
    for key, value in legacy.items():
        if key not in values or not str(values.get(key) or "").strip():
            values[key] = value
    if (values.get("UPSTOX_ACCESS_TOKEN") or "").strip():
        console_status(
            f"[Setup] Using UPSTOX_ACCESS_TOKEN from {fallback.name} "
            f"(read-only; {fallback.name} was not moved or modified)."
        )
    return values


def migrate_legacy_env_file(
    legacy_path: Path = LEGACY_ENV_FILE,
    destination: Path = DEFAULT_ENV_FILE,
) -> Optional[Path]:
    """Deprecated no-op.

    Older builds moved project-root env.txt into stock-data/, which stole the
    live trading engine's credentials file. Kept as a stub so any external
    caller does not break; it never reads, writes, or moves env files.
    """
    return None


def migrate_legacy_helper_data(
    project_dir: Path = PROJECT_DIR,
    stock_data_dir: Path = STOCK_DATA_DIR,
) -> int:
    """Move old data/cache and data/factors artifacts into stock-data/ safely."""
    legacy_data_dir = project_dir / "data"
    moved = 0
    for category in ("cache", "factors"):
        legacy_dir = legacy_data_dir / category
        if not legacy_dir.exists():
            continue
        target_dir = stock_data_dir / category
        target_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(path for path in legacy_dir.rglob("*") if path.is_file()):
            relative = source.relative_to(legacy_dir)
            target = target_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                try:
                    identical = target.is_file() and filecmp.cmp(source, target, shallow=False)
                except OSError:
                    identical = False
                if identical:
                    source.unlink()
                    continue
                backup = target.with_name(f"{target.stem}.legacy{target.suffix}")
                suffix = 1
                while backup.exists():
                    backup = target.with_name(f"{target.stem}.legacy{suffix}{target.suffix}")
                    suffix += 1
                target = backup
            shutil.move(str(source), str(target))
            moved += 1
        for directory in sorted(
            (path for path in legacy_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            legacy_dir.rmdir()
        except OSError:
            pass
    try:
        legacy_data_dir.rmdir()
    except OSError:
        pass
    if moved:
        console_status(f"[Setup] Moved {moved} legacy cache/factor file(s) into {stock_data_dir}.")
    return moved


@dataclass(frozen=True)
class Instrument:
    symbol: str
    instrument_key: str
    isin: str
    name: str = ""


@dataclass(frozen=True)
class MomentumScore:
    score: float
    residual_sum: float
    residual_volatility: float
    alpha: float
    beta_market: float
    beta_smb: float
    beta_hml: float
    r_squared: float
    regression_observations: int


@dataclass(frozen=True)
class RankedStock:
    symbol: str
    score: float
    residual_sum: float
    residual_volatility: float
    beta_market: float
    beta_smb: float
    beta_hml: float
    r_squared: float
    latest_price: float
    median_daily_turnover_inr: float


@dataclass(frozen=True)
class PortfolioMember:
    stock: RankedStock
    cross_section_rank: int
    percentile_rank: float
    inverse_vol_weight: float
    gross_adjusted_weight: float


@dataclass(frozen=True)
class SupertrendState:
    """Supertrend reading for one stock on one timeframe (weekly or daily).

    ``direction`` is +1 when the Supertrend line sits under price (positive /
    bullish / green) and -1 when it sits above price (negative / bearish / red).
    ``change_date`` is the bar of the most recent flip, dated by that bar's
    session; when the trend is older than the loaded history it is None and
    ``first_resolved_date`` carries the earliest bar the indicator could be
    resolved. ``bars_since_change`` counts bars (weeks or sessions) in the same
    timeframe, so the label suffix matches the timeframe.
    """

    timeframe: str
    direction: int
    value: float
    change_date: Optional[date]
    bars_since_change: Optional[int]
    flip_in_window: bool
    first_resolved_date: Optional[date]
    bars: int

    @property
    def unit(self) -> str:
        """Age suffix for ``since_label``: weeks for weekly bars, days for daily."""
        return "w" if self.timeframe == "weekly" else "d"

    @property
    def weeks_since_change(self) -> Optional[int]:
        """Legacy weekly field name retained as a read-only compatibility alias."""
        return self.bars_since_change if self.timeframe == "weekly" else None

    @property
    def weekly_bars(self) -> int:
        """Legacy weekly field name retained as a read-only compatibility alias."""
        return self.bars if self.timeframe == "weekly" else 0

    @property
    def direction_label(self) -> str:
        if self.direction > 0:
            return "POS"
        if self.direction < 0:
            return "NEG"
        return "n/a"

    def since_label(self) -> str:
        if self.direction == 0:
            return "n/a"
        if self.change_date is None:
            if self.first_resolved_date is None:
                return "n/a"
            # No flip inside the loaded window: the trend is at least this old.
            return f"≤ {self.first_resolved_date.isoformat()}"
        bars = "" if self.bars_since_change is None else f" ({self.bars_since_change}{self.unit})"
        return f"{self.change_date.isoformat()}{bars}"


@dataclass(frozen=True)
class SupertrendReading:
    """Weekly + daily Supertrend for one symbol, with per-timeframe failure reasons."""

    weekly: Optional[SupertrendState] = None
    daily: Optional[SupertrendState] = None
    weekly_reason: Optional[str] = None
    daily_reason: Optional[str] = None

    @property
    def resolved(self) -> bool:
        """True when at least one timeframe produced a reading."""
        return self.weekly is not None or self.daily is not None

    def state(self, timeframe: str) -> Optional[SupertrendState]:
        if timeframe == "weekly":
            return self.weekly
        if timeframe == "daily":
            return self.daily
        raise ValueError(f"unknown Supertrend timeframe: {timeframe!r}")

    def failure_reason(self) -> Optional[str]:
        """Combined reason when neither timeframe could be resolved; else None."""
        if self.resolved:
            return None
        reasons = [reason for reason in (self.weekly_reason, self.daily_reason) if reason]
        return "; ".join(dict.fromkeys(reasons)) or "unavailable"


@dataclass(frozen=True)
class CrossoverState:
    """Daily JMA/DWMA crossover reading for one stock.

    ``direction`` mirrors the current line alignment on the latest scanned bar:
    +1 means JMA is above DWMA, -1 means it is below, 0 means the lines are equal.
    ``change_date`` is the session of the most recent crossover inside the
    scanned window (warm-up bars are not scanned); ``last_crossover_type`` and
    its two values capture the event itself. When there was no crossover,
    ``first_scanned_date`` carries the first bar checked. ``days_since_change``
    counts trading sessions, not calendar days.
    """

    direction: int
    jma_value: float
    dwma_value: float
    difference: float
    change_date: Optional[date]
    days_since_change: Optional[int]
    crossover_in_window: bool
    first_scanned_date: Optional[date]
    scanned_bars: int
    daily_bars: int
    crossover_count: int
    last_crossover_type: Optional[int] = None
    last_crossover_jma_value: Optional[float] = None
    last_crossover_dwma_value: Optional[float] = None

    @property
    def direction_label(self) -> str:
        if self.direction > 0:
            return "POS"
        if self.direction < 0:
            return "NEG"
        return "n/a"

    @property
    def last_crossover_label(self) -> str:
        if self.last_crossover_type is None:
            return "None"
        return "Positive" if self.last_crossover_type > 0 else "Negative"

    def since_label(self) -> str:
        # Keep the event date visible even if today's lines happen to be exactly
        # equal; equality on today's bar is not itself a crossover, but an earlier
        # valid crossover still exists in the scanned window.
        if self.change_date is not None:
            days = "" if self.days_since_change is None else f" ({self.days_since_change}d)"
            return f"{self.change_date.isoformat()}{days}"
        if self.direction == 0 or self.first_scanned_date is None:
            return "n/a"
        # No crossover inside the scanned window: the current side is at least
        # this old (or there was no valid cross after warm-up).
        return f"≤ {self.first_scanned_date.isoformat()}"

    def difference_label(self) -> str:
        """Signed JMA − DWMA spread in ₹ (positive = fast line above slow line)."""
        if not np.isfinite(self.difference):
            return "n/a"
        return f"{self.difference:+,.2f}"


@dataclass(frozen=True)
class EntryCandidate:
    """One residual-score-qualified stock in the post-screen entry review."""

    symbol: str
    residual_score: float
    classification: str
    latest_date: Optional[date]
    close: float
    ema20: float
    volume: float
    average_volume: float
    volume_multiple: float
    ema_extension_pct: float
    weekly_supertrend: str
    daily_supertrend: str
    crossover_type: str
    crossover_date: Optional[date]
    fresh_positive_cross: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BufferSummary:
    applied: bool
    current_holding_count: int
    entry_rank_cutoff: int
    exit_rank_cutoff: int
    retained: tuple[str, ...] = ()
    new_entries: tuple[str, ...] = ()
    exits_not_eligible: tuple[str, ...] = ()
    exits_below_buffer: tuple[str, ...] = ()
    exits_capacity: tuple[str, ...] = ()


class RateLimiter:
    """Thread-safe, process-local request pacer."""

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.interval = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.interval
        if delay:
            time.sleep(delay)


class UpstoxClient:
    """Small read-only client for the Upstox instrument and candle endpoints."""

    def __init__(
        self,
        access_token: str,
        cache_dir: Path,
        cache_ttl_seconds: float,
        refresh: bool = False,
        requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
    ) -> None:
        self.access_token = access_token
        self.cache_dir = cache_dir
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self.refresh = refresh
        self.rate_limiter = RateLimiter(requests_per_second)
        self._thread_local = threading.local()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "Accept": "application/json",
                    "User-Agent": "residual-momentum-screen/1.0",
                }
            )
            self._thread_local.session = session
        return session

    def _get(self, url: str, *, authenticated: bool, timeout: tuple[int, int] = (10, 30)) -> requests.Response:
        headers = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.access_token}"
        last_status: Optional[int] = None
        for attempt in range(4):
            self.rate_limiter.wait()
            try:
                response = self._session().get(url, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise UpstoxRequestError(f"Network error while requesting Upstox data: {type(exc).__name__}") from None
                time.sleep(min(2**attempt, 8))
                continue

            if response.status_code in (401, 403):
                raise UpstoxAuthenticationError(
                    f"Upstox rejected the access token (HTTP {response.status_code}). "
                    "Refresh the token and update env.txt."
                )
            if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
                last_status = response.status_code
                if attempt == 3:
                    break
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(2**attempt, 8)
                except ValueError:
                    delay = min(2**attempt, 8)
                time.sleep(max(0.25, min(delay, 15.0)))
                continue
            if not response.ok:
                raise UpstoxRequestError(f"Upstox request failed (HTTP {response.status_code}).")
            return response

        raise UpstoxRequestError(
            f"Upstox request failed after retries (HTTP {last_status or 'network error'})."
        )

    def get_nse_equity_instruments(self) -> list[Instrument]:
        """Download current NSE BOD master and retain primary NSE cash equities."""
        try:
            response = self._get(NSE_INSTRUMENTS_URL, authenticated=False, timeout=(10, 45))
            raw = response.content
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            payload = json.loads(raw.decode("utf-8"))
        except (UpstoxRequestError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            if isinstance(exc, UpstoxRequestError):
                raise
            raise UpstoxRequestError("Could not parse Upstox's NSE instrument master.") from None

        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise UpstoxRequestError("Upstox NSE instrument master had an unexpected format.")

        by_symbol: dict[str, list[Instrument]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("segment") != "NSE_EQ" or row.get("exchange") != "NSE":
                continue
            # Limit to NSE cash-market equity series. EQ is the normal series;
            # BE is also a cash equity security (often trade-to-trade), not a
            # different asset class. Exclude SME, debt, indices and derivatives.
            if str(row.get("instrument_type", "")).upper() not in {"EQ", "BE"}:
                continue
            security_type = str(row.get("security_type", "NORMAL")).upper()
            if security_type not in ("NORMAL", ""):
                continue
            symbol = str(row.get("trading_symbol", "")).strip().upper()
            instrument_key = str(row.get("instrument_key", "")).strip()
            isin = str(row.get("isin", "")).strip()
            if not symbol or not instrument_key or not isin:
                continue
            by_symbol.setdefault(symbol, []).append(
                Instrument(
                    symbol=symbol,
                    instrument_key=instrument_key,
                    isin=isin,
                    name=str(row.get("name", "")).strip(),
                )
            )

        # If the master has multiple active records for the same requested
        # ticker, omit it rather than make a potentially incorrect mapping.
        result: list[Instrument] = []
        ambiguous: list[str] = []
        for symbol in dict.fromkeys(UNIVERSE):
            matches = by_symbol.get(symbol, [])
            if len(matches) == 1:
                result.append(matches[0])
            elif len(matches) > 1:
                ambiguous.append(symbol)
        if ambiguous:
            print(
                "WARNING: ambiguous NSE instrument mappings omitted: " + ", ".join(ambiguous),
                file=sys.stderr,
            )
        return result

    def _price_cache_path(self, symbol: str, instrument_key: str) -> Path:
        key_hash = hashlib.sha256(instrument_key.encode("utf-8")).hexdigest()[:12]
        safe_symbol = re.sub(r"[^A-Za-z0-9_-]", "_", symbol)
        return self.cache_dir / "upstox_prices" / f"{safe_symbol}_{key_hash}.csv"

    @staticmethod
    def _read_price_cache(path: Path) -> pd.DataFrame:
        frame = pd.read_csv(path)
        if "date" not in frame.columns:
            raise ValueError("cache missing date column")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).set_index("date").sort_index()
        for col in ("open", "high", "low", "close", "volume"):
            if col not in frame.columns:
                frame[col] = np.nan
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        return frame[["open", "high", "low", "close", "volume"]]

    def _write_price_cache(self, path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        out = frame.copy()
        out.index.name = "date"
        out.to_csv(tmp)
        os.replace(tmp, path)

    @staticmethod
    def _parse_candles(candles: Sequence[Sequence[Any]], today: date, start: date) -> pd.DataFrame:
        records: list[tuple[pd.Timestamp, float, float, float, float, float]] = []
        for candle in candles:
            if not isinstance(candle, (list, tuple)) or len(candle) < 6:
                continue
            try:
                ts = pd.to_datetime(candle[0], utc=True, errors="coerce")
                if pd.isna(ts):
                    continue
                local_date = ts.tz_convert(INDIA_TZ).tz_localize(None).normalize()
                if local_date.date() < start or local_date.date() >= today:
                    continue  # Ignore a potentially incomplete current-day candle.
                open_, high, low, close, volume = [float(candle[i]) for i in range(1, 6)]
                if not all(np.isfinite(v) for v in (close, volume)) or close <= 0 or volume < 0:
                    continue
                records.append((local_date, open_, high, low, close, volume))
            except (TypeError, ValueError, OverflowError):
                continue
        if not records:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(
            records, columns=["date", "open", "high", "low", "close", "volume"]
        )
        frame = frame.sort_values("date").drop_duplicates("date", keep="last").set_index("date")
        return frame

    def get_daily_candles(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        today: date,
    ) -> tuple[pd.DataFrame, bool]:
        """Get daily OHLCV history; returns (data, was_cached)."""
        cache_path = self._price_cache_path(instrument.symbol, instrument.instrument_key)
        if not self.refresh and cache_path.exists() and self.cache_ttl_seconds > 0:
            age = time.time() - cache_path.stat().st_mtime
            if age <= self.cache_ttl_seconds:
                try:
                    cached = self._read_price_cache(cache_path)
                    if not cached.empty:
                        cached = cached[
                            (cached.index.date >= start_date) & (cached.index.date < today)
                        ]
                        if not cached.empty:
                            return cached, True
                except (OSError, ValueError, pd.errors.ParserError):
                    pass  # Corrupt/old cache: make a fresh market-data request.

        encoded_key = quote(instrument.instrument_key, safe="")
        url = (
            f"{UPSTOX_API_BASE}/v3/historical-candle/{encoded_key}/days/1/"
            f"{end_date.isoformat()}/{start_date.isoformat()}"
        )
        response = self._get(url, authenticated=True)
        try:
            payload = response.json()
            candles = payload.get("data", {}).get("candles", [])
            if payload.get("status") not in (None, "success"):
                raise ValueError("API status was not success")
            frame = self._parse_candles(candles, today=today, start=start_date)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            raise UpstoxRequestError(
                f"Could not parse historical candles for {instrument.symbol}."
            ) from None
        if not frame.empty:
            self._write_price_cache(cache_path, frame)
        return frame, False


def read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without executing shell syntax."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScreenerError(f"Cannot read env file at {path}: {exc}") from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def env_value(env_file: Mapping[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    """Shell environment takes precedence over env.txt."""
    return os.environ.get(key, env_file.get(key, default))


def read_current_holdings(path: Optional[Path]) -> Optional[set[str]]:
    """Read optional paper-buffer state from a CSV with symbol/ticker column."""
    if path is None:
        return None
    if not path.exists():
        raise ScreenerError(f"Holdings CSV does not exist: {path}")
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as exc:
        raise ScreenerError(f"Could not read holdings CSV {path}: {type(exc).__name__}") from None
    symbol_column = next(
        (column for column in frame.columns if _normalized_column_name(column) in {"symbol", "ticker", "tradingsymbol"}),
        None,
    )
    if symbol_column is None:
        raise ScreenerError(
            "Holdings CSV must have a column named symbol, ticker, or tradingsymbol. "
            "Only the symbols are used; no orders or quantities are read."
        )
    symbols = {
        str(value).strip().upper()
        for value in frame[symbol_column].dropna()
        if str(value).strip()
    }
    return symbols


def _normalized_column_name(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def _parse_month(value: Any) -> pd.Period:
    text = str(value).strip()
    if re.fullmatch(r"\d{6}", text):
        text = f"{text[:4]}-{text[4:6]}"
    # IIMA uses YYYY-MM. The fallbacks support normalized YYYYMM/DD-MM-YYYY CSVs.
    try:
        return pd.Period(text[:7], freq="M")
    except (ValueError, TypeError):
        timestamp = pd.to_datetime(text, errors="coerce")
        if pd.isna(timestamp):
            raise ValueError(f"invalid month: {value!r}")
        return timestamp.to_period("M")


def parse_india_factor_csv(text: str, units: str = "percent") -> pd.DataFrame:
    """Normalize an India FF factor CSV to decimal returns.

    Market-excess aliases include IIMA's MF, SCDLDS's MKT, and Mkt-RF. IIMA's
    CSV is percent-valued; SCDLDS's downloadable ff5.csv is decimal-valued.
    User-supplied files default to percent unless ``units='decimal'`` is set.
    """
    if units not in ("percent", "decimal"):
        raise ValueError("factor units must be 'percent' or 'decimal'")
    try:
        raw = pd.read_csv(io.StringIO(text), encoding="utf-8-sig")
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise DataQualityError("Could not parse the monthly factor CSV.") from None
    if raw.empty or len(raw.columns) < 5:
        raise DataQualityError("The monthly factor CSV is empty or missing required columns.")

    normalized = {_normalized_column_name(col): col for col in raw.columns}
    date_col = next(
        (normalized[name] for name in ("date", "month", "period", "yyyymm") if name in normalized),
        raw.columns[0],
    )
    aliases = {
        "mkt_rf": ("mktrf", "mf", "mkt", "marketpremium", "marketfactor", "marketexcessreturn"),
        "smb": ("smb", "sizeminusbig"),
        "hml": ("hml", "highminuslow"),
        "rf": ("rf", "riskfree", "riskfreerate"),
    }
    columns: dict[str, Any] = {}
    for target, candidates in aliases.items():
        source = next((normalized[candidate] for candidate in candidates if candidate in normalized), None)
        if source is None:
            raise DataQualityError(
                f"Factor CSV is missing {target}. Required inputs: market excess (MF/Mkt-RF), SMB, HML, RF."
            )
        columns[target] = source

    parsed_months: list[pd.Period] = []
    keep_rows: list[int] = []
    for i, value in enumerate(raw[date_col].tolist()):
        try:
            parsed_months.append(_parse_month(value))
            keep_rows.append(i)
        except ValueError:
            # Header notes / annual summary rows in some factor-library exports
            # are not monthly records and are ignored.
            continue
    if not keep_rows:
        raise DataQualityError("No valid YYYY-MM monthly observations found in factor CSV.")

    subset = raw.iloc[keep_rows].copy()
    subset.index = pd.PeriodIndex(parsed_months, freq="M", name="month")
    out = pd.DataFrame(index=subset.index)
    for target, source in columns.items():
        out[target] = pd.to_numeric(subset[source], errors="coerce")
    if units == "percent":
        out = out / 100.0
    out = out.replace([np.inf, -np.inf], np.nan)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    # RF is needed to form the dependent excess return; factor rows with a
    # missing FF3 component cannot be used in a rolling regression.
    out = out.dropna(subset=["mkt_rf", "smb", "hml", "rf"])
    if out.empty:
        raise DataQualityError("No complete monthly market/SMB/HML/RF factor rows found.")
    return out[["mkt_rf", "smb", "hml", "rf"]]


def discover_latest_iima_factor_url(session: Optional[requests.Session] = None) -> str:
    """Find the newest survivorship-bias-adjusted monthly factor CSV on IIMA."""
    session = session or requests.Session()
    try:
        response = session.get(IIMA_LANDING_PAGE, timeout=(10, 30))
        response.raise_for_status()
    except requests.RequestException:
        raise ScreenerError(
            "Could not reach IIMA's Indian factor-data page. Supply --factor-file with a recent India FF3 CSV."
        ) from None
    names = sorted(set(FACTOR_FILENAME_PATTERN.findall(response.text)))
    if not names:
        raise ScreenerError(
            "Could not discover a monthly survivorship-adjusted IIMA factor file. "
            "Supply --factor-file with a recent India FF3 CSV."
        )
    return urljoin(IIMA_DATA_BASE, names[-1])


def discover_scdlds_factor_url(session: Optional[requests.Session] = None) -> str:
    """Resolve SCDLDS's current FF5 download button, with its stable CSV URL as fallback."""
    session = session or requests.Session()
    try:
        response = session.get(SCDLDS_LANDING_PAGE, timeout=(10, 30))
        response.raise_for_status()
        matches = SCDLDS_DOWNLOAD_PATTERN.findall(response.text)
        if matches:
            return urljoin(SCDLDS_LANDING_PAGE, matches[0])
    except requests.RequestException:
        pass
    # The site currently publishes this file at a stable path. If the landing
    # page is temporarily unavailable, try the known CSV and validate its schema.
    return SCDLDS_FF5_FALLBACK_URL


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def _factor_artifact_paths(provider: str, cache_dir: Path) -> tuple[Path, Path, Path]:
    raw = cache_dir / f"{provider}_monthly_raw.csv"
    normalized = cache_dir / f"{provider}_ff3_monthly.csv"
    metadata = cache_dir / f"{provider}_factor_source.json"
    return raw, normalized, metadata


def _persist_factor_data(
    raw_text: str,
    factors: pd.DataFrame,
    provider: str,
    url: str,
    cache_dir: Path,
    source_units: str,
) -> tuple[Path, Path, Path]:
    """Atomically persist raw and normalized factor files plus source metadata."""
    raw_path, normalized_path, metadata_path = _factor_artifact_paths(provider, cache_dir)
    normalized = factors.copy()
    normalized.index = normalized.index.astype(str)
    normalized.index.name = "Month"
    normalized_text = normalized.to_csv(index=True)
    metadata = {
        "provider": provider,
        "source_url": url,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_units": source_units,
        "normalized_units": "decimal returns",
        "first_month": str(factors.index.min()),
        "latest_month": str(factors.index.max()),
        "complete_months": int(len(factors)),
        "raw_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "note": (
            "SCDLDS website describes this library as beta and free for academic use; "
            "confirm permission for other uses."
            if provider == "scdlds"
            else "IIMA Indian Fama-French and Momentum data library."
        ),
    }
    metadata_text = json.dumps(metadata, indent=2) + "\n"
    _atomic_write_text(raw_path, raw_text)
    _atomic_write_text(normalized_path, normalized_text)
    _atomic_write_text(cache_dir / "india_ff3_monthly_latest.csv", normalized_text)
    _atomic_write_text(metadata_path, metadata_text)
    _atomic_write_text(cache_dir / "factor_source.json", metadata_text)
    return raw_path, normalized_path, metadata_path


def _read_cached_factor_data(
    provider: str,
    cache_dir: Path,
) -> Optional[tuple[pd.DataFrame, str]]:
    """Read last known good raw provider file without silently changing sources."""
    raw_path, normalized_path, metadata_path = _factor_artifact_paths(provider, cache_dir)
    if not raw_path.exists():
        return None
    units = "decimal" if provider == "scdlds" else "percent"
    try:
        raw_text = raw_path.read_text(encoding="utf-8-sig")
        factors = parse_india_factor_csv(raw_text, units=units)
        if provider == "scdlds":
            _validate_scdlds_decimal_scale(factors)
    except (OSError, UnicodeDecodeError, DataQualityError, ValueError):
        return None
    fetched_at = "unknown retrieval time"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            fetched_at = str(metadata.get("retrieved_at_utc", fetched_at))
        except (OSError, json.JSONDecodeError):
            pass
    description = (
        f"{provider.upper()} local cache; retrieved {fetched_at}; latest {factors.index.max()} "
        f"(raw: {raw_path}; normalized: {normalized_path})"
    )
    return factors, description


def _validate_scdlds_decimal_scale(factors: pd.DataFrame) -> None:
    """Guard against an upstream units/schema change in the SCDLDS CSV."""
    median_abs_rf = float(factors["rf"].abs().median())
    # The downloadable CSV stores decimal returns (e.g. monthly RF around
    # 0.004), even though the site displays them as percentage returns.
    if not np.isfinite(median_abs_rf) or not (0 < median_abs_rf < 0.05):
        raise DataQualityError(
            "SCDLDS RF scale is unexpected; expected decimal returns (median monthly |RF| < 5%). "
            "The source may have changed units; do not auto-rescale without checking its documentation."
        )


def _fetch_and_store_factor_data(
    provider: str,
    session: requests.Session,
    cache_dir: Path,
) -> tuple[pd.DataFrame, str]:
    if provider == "scdlds":
        url = discover_scdlds_factor_url(session)
        units = "decimal"
    elif provider == "iima":
        url = discover_latest_iima_factor_url(session)
        units = "percent"
    else:
        raise ValueError("provider must be 'scdlds' or 'iima'")

    try:
        response = session.get(url, timeout=(10, 45), headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
    except requests.RequestException:
        raise ScreenerError(f"Could not download the {provider.upper()} monthly factor CSV.") from None

    raw_text = response.content.decode("utf-8-sig")
    factors = parse_india_factor_csv(raw_text, units=units)
    if provider == "scdlds":
        _validate_scdlds_decimal_scale(factors)
    raw_path, normalized_path, _ = _persist_factor_data(
        raw_text=raw_text,
        factors=factors,
        provider=provider,
        url=url,
        cache_dir=cache_dir,
        source_units=units,
    )
    description = (
        f"{provider.upper()} web refresh; latest {factors.index.max()}; "
        f"stored raw at {raw_path} and normalized decimal CSV at {normalized_path}"
    )
    return factors, description


def load_factor_data(
    factor_file: Optional[Path] = None,
    units: str = "percent",
    session: Optional[requests.Session] = None,
    provider: str = "scdlds",
    cache_dir: Path = DEFAULT_FACTOR_CACHE_DIR,
) -> tuple[pd.DataFrame, str]:
    """Fetch, normalize, and persist India factors; use same-provider cache offline.

    Default provider is SCDLDS because its monthly FF file currently extends to
    Aug 2026. Its series is a distinct FF5 construction, not an extension of IIMA;
    we use only MKT, SMB, HML, and RF for the FF3 regression. The provider is
    pinned per run to avoid concatenating two vendors' factor histories.
    """
    if factor_file is not None:
        console_status(f"[Factors] Reading factor CSV from {factor_file}...")
        try:
            text = factor_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise ScreenerError(f"Could not read factor file {factor_file}: {exc}") from None
        return parse_india_factor_csv(text, units=units), str(factor_file)

    provider = provider.strip().lower()
    if provider not in {"scdlds", "iima"}:
        raise ScreenerError("INDIA_FACTOR_PROVIDER/--factor-provider must be 'scdlds' or 'iima'.")
    session = session or requests.Session()
    console_status(f"[Factors] Checking the {provider.upper()} monthly factor source...")
    try:
        factors, description = _fetch_and_store_factor_data(provider, session, cache_dir)
        console_status(
            f"[Factors] Loaded {len(factors)} complete rows through {factors.index.max()}; "
            "raw/normalized copies and source metadata have been stored."
        )
        return factors, description
    except (ScreenerError, DataQualityError, UnicodeDecodeError, ValueError, OSError) as exc:
        # Keep provider provenance consistent: use only that provider's cache,
        # never splice SCDLDS months onto IIMA months (or vice versa).
        console_status(
            f"[Factors] Web refresh unavailable/invalid ({type(exc).__name__}); checking same-provider cache..."
        )
        cached = _read_cached_factor_data(provider, cache_dir)
        if cached is not None:
            factors, description = cached
            console_status(
                f"[Factors] Using cached {provider.upper()} data through {factors.index.max()}."
            )
            return factors, f"{description}; web refresh failed ({type(exc).__name__})"
        raise ScreenerError(
            f"Could not fetch valid {provider.upper()} factor data and no usable local cache exists. "
            f"Check internet access, use --factor-file, or explicitly choose another provider."
        ) from None


def current_india_date() -> date:
    return datetime.now(INDIA_TZ).date()


def last_completed_month(today: date) -> pd.Period:
    return pd.Period(today.strftime("%Y-%m"), freq="M") - 1


def months_between(earlier: pd.Period, later: pd.Period) -> int:
    return later.ordinal - earlier.ordinal


def validate_factor_freshness(
    factors: pd.DataFrame,
    today: date,
    max_staleness_months: int,
    allow_stale: bool,
) -> tuple[pd.DataFrame, pd.Period, int]:
    """Return factors clipped to completed months, latest usable month, and age."""
    completed = last_completed_month(today)
    usable = factors.loc[factors.index <= completed].copy()
    if usable.empty:
        raise DataQualityError(
            f"No complete monthly factor data is available through {completed}."
        )
    latest = usable.index.max()
    age = max(0, months_between(latest, completed))
    trailing_window = pd.period_range(end=latest, periods=REGRESSION_MONTHS, freq="M")
    missing_window = trailing_window.difference(usable.index)
    if len(missing_window):
        missing_text = ", ".join(str(month) for month in missing_window[:6])
        raise DataQualityError(
            f"Factor series has gaps in its last {REGRESSION_MONTHS} months (e.g. {missing_text}); "
            "a complete rolling regression window is required."
        )
    if age > max_staleness_months and not allow_stale:
        raise DataQualityError(
            f"Indian factor data ends at {latest}; the latest completed calendar month is {completed} "
            f"({age} month(s) of lag). Screening stopped rather than mix stale factors with current prices. "
            f"Update the India-specific factor CSV or, only for a deliberately historical screen, rerun with "
            f"--allow-stale-factors."
        )
    return usable, latest, age


def _to_period_series(series: pd.Series) -> pd.Series:
    out = series.copy()
    if not isinstance(out.index, pd.PeriodIndex):
        out.index = pd.PeriodIndex(pd.to_datetime(out.index), freq="M")
    return out


def calculate_residual_momentum(
    monthly_stock_returns: pd.Series,
    factors: pd.DataFrame,
    as_of_month: pd.Period,
    regression_months: int = REGRESSION_MONTHS,
    formation_residual_months: int = FORMATION_RESIDUAL_MONTHS,
) -> MomentumScore:
    """Estimate FF3 betas and compute the supplied 12-1 residual-momentum score.

    ``monthly_stock_returns`` are simple monthly total/price returns in decimal
    units indexed by Period('M'). ``factors`` contains decimal mkt_rf/smb/hml/rf.
    To align with the FF model, the regression dependent variable is R_i - R_f.
    The signal residual is (R_i - R_f) - beta'F; alpha is deliberately NOT
    subtracted, per the user's specification.
    """
    if regression_months < formation_residual_months + 1:
        raise ValueError("regression window must cover formation months plus the skipped month")
    if formation_residual_months != 11:
        # Keep the API flexible for tests/research, but document that the default
        # exactly matches the supplied 12-1 formation period.
        pass
    returns = _to_period_series(monthly_stock_returns)
    factor_frame = factors.copy()
    if not isinstance(factor_frame.index, pd.PeriodIndex):
        factor_frame.index = pd.PeriodIndex(pd.to_datetime(factor_frame.index), freq="M")

    regression_index = pd.period_range(
        end=as_of_month, periods=regression_months, freq="M", name="month"
    )
    needed = ["mkt_rf", "smb", "hml", "rf"]
    if any(col not in factor_frame.columns for col in needed):
        raise DataQualityError("Factor frame must contain mkt_rf, smb, hml, and rf columns.")
    y_raw = pd.to_numeric(returns.reindex(regression_index), errors="coerce")
    f = factor_frame.reindex(regression_index)[needed].apply(pd.to_numeric, errors="coerce")
    if y_raw.isna().any() or f.isna().any().any():
        missing_returns = int(y_raw.isna().sum())
        missing_factor_rows = int(f.isna().any(axis=1).sum())
        raise DataQualityError(
            f"incomplete 36-month window (missing stock returns={missing_returns}, "
            f"missing factor months={missing_factor_rows})"
        )
    if not np.isfinite(y_raw.to_numpy(dtype=float)).all() or not np.isfinite(f.to_numpy(dtype=float)).all():
        raise DataQualityError("non-finite values in monthly returns/factors")

    y_excess = y_raw.to_numpy(dtype=float) - f["rf"].to_numpy(dtype=float)
    x_factors = f[["mkt_rf", "smb", "hml"]].to_numpy(dtype=float)
    design = np.column_stack((np.ones(len(regression_index)), x_factors))
    try:
        coefficients, _, rank, _ = np.linalg.lstsq(design, y_excess, rcond=None)
    except np.linalg.LinAlgError:
        raise DataQualityError("OLS regression failed") from None
    if rank < design.shape[1] or not np.isfinite(coefficients).all():
        raise DataQualityError("FF3 factor design is rank-deficient")
    condition_number = float(np.linalg.cond(design))
    if not np.isfinite(condition_number) or condition_number > 1e8:
        raise DataQualityError("FF3 regression is numerically unstable")

    alpha = float(coefficients[0])
    betas = coefficients[1:]
    formation_index = regression_index[-(formation_residual_months + 1) : -1]
    if len(formation_index) != formation_residual_months:
        raise DataQualityError("formation window is incomplete")
    formation_positions = np.arange(len(regression_index) - formation_residual_months - 1,
                                    len(regression_index) - 1)
    # Deliberately omit alpha from the residual calculation.
    residuals = y_excess[formation_positions] - x_factors[formation_positions] @ betas
    residual_sum = float(np.sum(residuals))
    residual_volatility = float(np.std(residuals, ddof=1))
    if not np.isfinite(residual_volatility) or residual_volatility <= 1e-8:
        raise DataQualityError("residual volatility is zero or too small")
    score = residual_sum / residual_volatility

    fitted = design @ coefficients
    sse = float(np.sum((y_excess - fitted) ** 2))
    centered = y_excess - float(np.mean(y_excess))
    sst = float(np.sum(centered**2))
    r_squared = 1.0 - sse / sst if sst > 0 else 0.0
    if not np.isfinite(score):
        raise DataQualityError("non-finite residual-momentum score")
    return MomentumScore(
        score=score,
        residual_sum=residual_sum,
        residual_volatility=residual_volatility,
        alpha=alpha,
        beta_market=float(betas[0]),
        beta_smb=float(betas[1]),
        beta_hml=float(betas[2]),
        r_squared=r_squared,
        regression_observations=regression_months,
    )


def monthly_closes_from_daily(daily: pd.DataFrame) -> pd.Series:
    """Take the last available daily close in each complete calendar month."""
    if daily.empty or "close" not in daily.columns:
        return pd.Series(dtype=float, name="close")
    frame = daily.copy().sort_index()
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    close = close[close > 0]
    if close.empty:
        return pd.Series(dtype=float, name="close")
    periods = pd.PeriodIndex(close.index, freq="M", name="month")
    monthly = pd.Series(close.to_numpy(), index=periods, name="close")
    return monthly[~monthly.index.duplicated(keep="last")].sort_index()


OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def clean_daily_bars(daily: pd.DataFrame) -> pd.DataFrame:
    """Return sorted, de-duplicated daily OHLCV with finite, positive OHLC prices.

    Weekly/daily Supertrend depends on a complete OHLC bar, so its input drops a
    session with any missing, infinite, zero or negative OHLC value. Bars are
    kept in session order; the index is the session date. JMA/DWMA uses the close
    series separately so an otherwise-valid close is not lost because (for
    example) the provider omitted that day's high or low.
    """
    columns = list(OHLCV_COLUMNS)
    empty = pd.DataFrame(columns=columns)
    if daily is None or daily.empty or not {"open", "high", "low", "close"}.issubset(daily.columns):
        return empty
    frame = daily.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.sort_index(kind="stable")
    for column in columns:
        if column not in frame.columns:
            frame[column] = np.nan
    frame = frame[columns].apply(pd.to_numeric, errors="coerce")
    frame = frame[frame.index.notna()]
    frame = frame[~frame.index.duplicated(keep="last")]
    frame = frame.replace([np.inf, -np.inf], np.nan)
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame[(frame[["open", "high", "low", "close"]] > 0).all(axis=1)]
    if frame.empty:
        return empty
    return frame.sort_index(kind="stable")


def daily_close_series(daily: pd.DataFrame) -> pd.Series:
    """Return adjusted daily closes where supplied, else close, independent of OHLC.

    The current Upstox candle endpoint exposes ``close`` but does not guarantee
    dividend-adjusted values. Accept explicit ``adjusted_close``/``adj_close``
    columns in cached or injected histories when available; otherwise use the
    provider's close while the report states that corporate-action adjustment is
    not assured.
    """
    if daily is None or daily.empty:
        return pd.Series(dtype=float, name="close")
    adjusted_column = next(
        (
            column
            for column in daily.columns
            if _normalized_column_name(column) in {"adjustedclose", "adjclose", "adjclosingprice"}
        ),
        None,
    )
    close_column = adjusted_column or ("close" if "close" in daily.columns else None)
    if close_column is None:
        return pd.Series(dtype=float, name="close")
    frame = daily[[close_column]].copy()
    frame.columns = ["close"]
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.sort_index(kind="stable")
    frame = frame[frame.index.notna()]
    frame = frame[~frame.index.duplicated(keep="last")]
    close = pd.to_numeric(frame["close"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    close = close.dropna()
    close = close[close > 0]
    close.name = "close"
    return close.astype(float)


def resample_daily_to_weekly(
    daily: pd.DataFrame,
    rule: str = WEEKLY_RESAMPLE_RULE,
) -> pd.DataFrame:
    """Aggregate daily OHLCV into NSE trading weeks (week ending Friday).

    Each weekly bar is indexed by the last session that actually traded in that
    week, so an in-progress current week is dated by its latest session rather
    than by a future calendar Friday. Weeks with no session are dropped, so a
    holiday week never creates a synthetic bar.
    """
    columns = list(OHLCV_COLUMNS)
    empty = pd.DataFrame(columns=columns)
    frame = clean_daily_bars(daily)
    if frame.empty:
        return empty
    grouper = pd.Grouper(freq=rule, label="right", closed="right")
    weekly = frame.groupby(grouper).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    weekly = weekly.dropna(subset=["open", "high", "low", "close"])
    if weekly.empty:
        return empty
    last_session = frame.index.to_series().groupby(grouper).max()
    weekly.index = pd.DatetimeIndex(last_session.reindex(weekly.index).to_numpy())
    weekly = weekly[~weekly.index.duplicated(keep="last")].sort_index()
    return weekly


def wilder_atr(frame: pd.DataFrame, period: int) -> pd.Series:
    """Wilder-smoothed ATR (RMA), seeded with the mean of the first `period` true ranges."""
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    close = frame["close"].astype(float).to_numpy()
    n = len(frame)
    out = np.full(n, np.nan)
    if n < period or period < 1:
        return pd.Series(out, index=frame.index, name="atr")
    previous_close = np.empty(n)
    previous_close[0] = np.nan
    previous_close[1:] = close[:-1]
    true_range = np.maximum(
        high - low,
        np.maximum(np.abs(high - previous_close), np.abs(low - previous_close)),
    )
    # First bar has no previous close; true range collapses to high - low.
    true_range[0] = high[0] - low[0]
    out[period - 1] = float(np.mean(true_range[:period]))
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + true_range[i]) / period
    return pd.Series(out, index=frame.index, name="atr")


def calculate_supertrend(
    frame: pd.DataFrame,
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> pd.DataFrame:
    """Classic Supertrend on the supplied OHLC bars (weekly or daily for this screen).

    Convention: bands are hl2 ± multiplier × Wilder ATR(period). The support band
    (hl2 − k·ATR) can only ratchet up while the prior close holds above it, and
    the resistance band (hl2 + k·ATR) can only ratchet down while the prior close
    holds below it. Trend flips to +1 when close exceeds the prior resistance
    band and to −1 when close breaks the prior support band; the plotted line is
    the support band in an uptrend and the resistance band in a downtrend.
    The first `period` bars are needed to seed the ATR and are left unresolved.
    """
    if period < 1:
        raise ValueError("supertrend period must be at least 1")
    if not np.isfinite(multiplier) or multiplier <= 0:
        raise ValueError("supertrend multiplier must be a positive finite number")

    result = frame.copy()
    high = result["high"].astype(float).to_numpy()
    low = result["low"].astype(float).to_numpy()
    close = result["close"].astype(float).to_numpy()
    n = len(result)
    lower_band = np.full(n, np.nan)   # support line, active while trend is +1
    upper_band = np.full(n, np.nan)   # resistance line, active while trend is -1
    trend = np.zeros(n, dtype=int)
    resolved = np.zeros(n, dtype=bool)
    atr = wilder_atr(result, period).to_numpy(dtype=float)
    start = next((i for i in range(n) if np.isfinite(atr[i])), None)
    if start is None:
        result["lower_band"] = lower_band
        result["upper_band"] = upper_band
        result["supertrend"] = np.nan
        result["trend"] = 0
        return result

    hl2 = (high + low) / 2.0
    basic_lower = hl2 - multiplier * atr
    basic_upper = hl2 + multiplier * atr

    for i in range(start, n):
        if i == start:
            # Seed the recursion with this bar's own bands (no prior state).
            previous_lower = basic_lower[i]
            previous_upper = basic_upper[i]
            previous_close = close[i - 1] if i > 0 else close[i]
            previous_trend = 1
        else:
            previous_lower = lower_band[i - 1]
            previous_upper = upper_band[i - 1]
            previous_close = close[i - 1]
            previous_trend = trend[i - 1]

        current_lower = max(basic_lower[i], previous_lower) if previous_close > previous_lower else basic_lower[i]
        current_upper = min(basic_upper[i], previous_upper) if previous_close < previous_upper else basic_upper[i]

        if previous_trend == -1 and close[i] > previous_upper:
            current_trend = 1
        elif previous_trend == 1 and close[i] < previous_lower:
            current_trend = -1
        else:
            current_trend = previous_trend

        lower_band[i] = current_lower
        upper_band[i] = current_upper
        trend[i] = current_trend
        resolved[i] = True

    result["lower_band"] = lower_band
    result["upper_band"] = upper_band
    result["supertrend"] = np.where(trend > 0, lower_band, np.where(trend < 0, upper_band, np.nan))
    result["trend"] = np.where(resolved, trend, 0)
    return result


def _supertrend_state_from_bars(
    bars: pd.DataFrame,
    timeframe: str,
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> tuple[Optional[SupertrendState], Optional[str]]:
    """Return (state, failure reason) for one timeframe's already-aggregated bars.

    ``bars`` must already be the timeframe's OHLC bars (weekly resample or clean
    daily sessions) and ``timeframe`` labels the reading so the "since" suffix is
    weeks or days. ``failure reason`` is None on success; otherwise it explains
    why no reading could be produced.
    """
    if timeframe not in SUPERTREND_TIMEFRAMES:
        raise ValueError(f"unknown Supertrend timeframe: {timeframe!r}")
    bar_word = "weekly" if timeframe == "weekly" else "daily"
    minimum_bars = period + 2  # ATR seed plus one bar to establish direction.
    if bars is None or len(bars) < minimum_bars:
        count = 0 if bars is None else len(bars)
        return None, f"fewer than {minimum_bars} {bar_word} bars ({count} available)"
    try:
        result = calculate_supertrend(bars, period=period, multiplier=multiplier)
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"{bar_word} supertrend calculation failed ({type(exc).__name__})"
    resolved = result.loc[result["trend"] != 0]
    if resolved.empty:
        return None, f"{bar_word} ATR never seeded"

    trends = resolved["trend"].to_numpy(dtype=int)
    bar_dates = [timestamp.date() for timestamp in resolved.index]
    last_index = len(trends) - 1
    direction = int(trends[last_index])
    value = float(resolved["supertrend"].iloc[last_index])

    flip_index: Optional[int] = None
    for i in range(last_index, 0, -1):
        if trends[i] != trends[i - 1]:
            flip_index = i
            break

    if flip_index is not None:
        change_date: Optional[date] = bar_dates[flip_index]
        bars_since: Optional[int] = last_index - flip_index
        flip_in_window = True
    else:
        change_date = None
        bars_since = None
        flip_in_window = False

    return (
        SupertrendState(
            timeframe=timeframe,
            direction=direction,
            value=value,
            change_date=change_date,
            bars_since_change=bars_since,
            flip_in_window=flip_in_window,
            first_resolved_date=bar_dates[0],
            bars=int(len(bars)),
        ),
        None,
    )


def supertrend_state(
    daily: pd.DataFrame,
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> tuple[Optional[SupertrendState], Optional[str]]:
    """Backward-compatible weekly Supertrend state from a stock's daily history.

    The original screener exposed ``supertrend_state(daily, period, multiplier)``
    as its weekly reading helper. Keep that behavior; use ``daily_supertrend_state``
    for the new daily timeframe.
    """
    return _supertrend_state_from_bars(
        resample_daily_to_weekly(daily),
        timeframe="weekly",
        period=period,
        multiplier=multiplier,
    )


def weekly_supertrend_state(
    daily: pd.DataFrame,
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> tuple[Optional[SupertrendState], Optional[str]]:
    """Weekly Supertrend of one stock, resampled from the daily history in memory.

    Weekly bars come from the daily history already downloaded, so this costs no
    additional market-data requests.
    """
    return supertrend_state(daily, period=period, multiplier=multiplier)


def daily_supertrend_state(
    daily: pd.DataFrame,
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> tuple[Optional[SupertrendState], Optional[str]]:
    """Daily Supertrend of one stock — the same ATR settings on daily sessions.

    This is the finer-grained trend change asked for alongside the weekly one:
    it flips on the session the daily close breaks the prior band, so its
    "since" date is normally newer than the weekly flip.
    """
    return _supertrend_state_from_bars(
        clean_daily_bars(daily),
        timeframe="daily",
        period=period,
        multiplier=multiplier,
    )


def compute_supertrend_states(
    prices: Mapping[str, pd.DataFrame],
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> tuple[dict[str, SupertrendReading], dict[str, str]]:
    """Compute weekly + daily Supertrend for every symbol with price history, in memory.

    A symbol lands in ``failures`` only when neither timeframe could be resolved;
    a partial result (for example too few sessions for the weekly ATR seed but
    enough for the daily one) is kept with the missing timeframe set to None and
    its reason recorded on the reading.
    """
    readings: dict[str, SupertrendReading] = {}
    failures: dict[str, str] = {}
    for symbol in sorted(prices):
        daily = prices.get(symbol)
        if daily is None or daily.empty:
            failures[symbol] = "no price history"
            continue
        weekly_state, weekly_reason = weekly_supertrend_state(daily, period=period, multiplier=multiplier)
        daily_state, daily_reason = daily_supertrend_state(daily, period=period, multiplier=multiplier)
        reading = SupertrendReading(
            weekly=weekly_state,
            daily=daily_state,
            weekly_reason=weekly_reason,
            daily_reason=daily_reason,
        )
        if reading.resolved:
            readings[symbol] = reading
        else:
            failures[symbol] = reading.failure_reason() or "unavailable"
    return readings, failures


def compute_weekly_supertrend(
    prices: Mapping[str, pd.DataFrame],
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> tuple[dict[str, SupertrendState], dict[str, str]]:
    """Backward-compatible weekly-only Supertrend calculation helper.

    The main screener uses ``compute_supertrend_states`` for both timeframes;
    this wrapper preserves callers that only consume the older weekly result.
    """
    states: dict[str, SupertrendState] = {}
    failures: dict[str, str] = {}
    for symbol in sorted(prices):
        state, reason = weekly_supertrend_state(prices[symbol], period=period, multiplier=multiplier)
        if state is None:
            failures[symbol] = reason or "unavailable"
        else:
            states[symbol] = state
    return states, failures


@dataclass(frozen=True)
class JmaParameters:
    """Precomputed JMA constants for one (length, phase, power) triple.

    With the screen defaults (length 7, phase -40, power 0.35) these are
    beta 0.5744680851, len1 2.792481, pow1 0.792481, div 0.1, phase_ratio 1.1,
    cap ≈ 3.6542 and an avolty smoothing factor of 2/31 = 0.064516129 — the
    constant table of the supplied algorithm.
    """

    length: int
    phase: float
    power: float
    beta: float
    len1: float
    pow1: float
    div: float
    phase_ratio: float
    cap: float
    avolty_factor: float


def jma_parameters(
    length: int = DEFAULT_JMA_LENGTH,
    phase: float = DEFAULT_JMA_PHASE,
    power: float = DEFAULT_JMA_POWER,
) -> JmaParameters:
    """Derive the JMA constants from length and phase.

    ``power`` is part of the requested parameter triple and is validated, but in
    this JMA variant the adaptive exponent comes from ``length`` alone
    (pow1 = len1 − 2, floored at 0.5), so power does not alter the series — the
    supplied algorithm's recurrence only uses beta, len1/pow1, div, phase_ratio,
    the cap and the avolty factor. Phase outside [-100, 100] is clipped for
    phase_ratio (0.5 below, 2.5 above), exactly as in the reference code.
    """
    if length < 2:
        raise ValueError("JMA length must be at least 2")
    if not np.isfinite(phase):
        raise ValueError("JMA phase must be a finite number")
    if not np.isfinite(power) or power <= 0:
        raise ValueError("JMA power must be a positive finite number")

    clipped_phase = float(min(JMA_PHASE_LIMIT, max(-JMA_PHASE_LIMIT, phase)))
    if phase < -JMA_PHASE_LIMIT:
        phase_ratio = 0.5
    elif phase > JMA_PHASE_LIMIT:
        phase_ratio = 2.5
    else:
        phase_ratio = 1.5 + phase * 0.01

    half_length = 0.5 * (length - 1)
    len1 = max(math.log(math.sqrt(half_length)) / math.log(2.0) + 2.0, JMA_LEN1_FLOOR)
    pow1 = max(len1 - 2.0, JMA_POW1_FLOOR)
    scaled = JMA_BETA_COEFFICIENT * (length - 1)
    return JmaParameters(
        length=int(length),
        phase=clipped_phase,
        power=float(power),
        beta=scaled / (scaled + 2.0),
        len1=len1,
        pow1=pow1,
        div=JMA_VOLTY_DIVISOR,
        phase_ratio=phase_ratio,
        cap=float(len1 ** (1.0 / pow1)),
        avolty_factor=2.0 / (max(4 * length, JMA_AVOLTY_MIN_LOOKBACK) + 1),
    )


def calculate_jma(
    close: pd.Series,
    length: int = DEFAULT_JMA_LENGTH,
    phase: float = DEFAULT_JMA_PHASE,
    power: float = DEFAULT_JMA_POWER,
) -> pd.Series:
    """Lag-reduced Jurik-style JMA of the supplied closes, evaluated bar by bar.

    The recurrence follows the supplied algorithm exactly. Volty is the larger of
    |price − bsmax| and |price − bsmin| against the previous bar's Jurik bands;
    an incremental 10-bar sum of volty is smoothed into avolty, and the ratio
    volty/avolty clamped to [1, cap] is raised to pow1 to drive both the band
    factor kv and the adaptive alpha. The three stages are an adaptive EMA
    (ma1), a Kalman-style detrend (det0, phase-weighted into ma2) and the final
    Jurik filter (det1, accumulated into jma). The first bar seeds jma/bsmax/
    bsmin with its own close, so the early values need the warm-up period before
    they are comparable to a slow reference line.

    ``close`` must already be cleaned (finite, positive, oldest bar first) — see
    ``daily_close_series`` — because the recursion carries every bar forward.
    """
    params = jma_parameters(length, phase, power)
    prices = pd.to_numeric(close, errors="coerce").astype(float).to_numpy()
    n = len(prices)
    jma = np.full(n, np.nan)
    if n == 0:
        return pd.Series(jma, index=close.index, name="jma")

    volty_history: list[float] = []
    vsum = 0.0
    avolty = 0.0
    bsmax = float(prices[0])
    bsmin = float(prices[0])
    ma1 = 0.0
    det0 = 0.0
    e2 = 0.0
    value = float(prices[0])
    one_minus_beta = 1.0 - params.beta

    for i in range(n):
        price = float(prices[i])
        del1 = price - bsmax
        del2 = price - bsmin
        volty = max(abs(del1), abs(del2))
        volty_history.append(volty)
        volty_lag = volty_history[i - JMA_VOLTY_LAG_BARS] if i >= JMA_VOLTY_LAG_BARS else 0.0
        vsum += params.div * (volty - volty_lag)
        avolty += params.avolty_factor * (vsum - avolty)

        d_volty = (volty / avolty) if avolty > 0 else 0.0
        d_volty = min(max(d_volty, 1.0), params.cap)
        pow2 = d_volty ** params.pow1
        kv = params.beta ** math.sqrt(pow2)
        upper_band = price if del1 > 0 else price - kv * del1
        lower_band = price if del2 < 0 else price - kv * del2

        alpha = params.beta ** pow2
        ma1 = (1.0 - alpha) * price + alpha * ma1
        det0 = (price - ma1) * one_minus_beta + params.beta * det0
        ma2 = ma1 + params.phase_ratio * det0
        det1 = (ma2 - value) * (1.0 - alpha) ** 2 + (alpha ** 2) * e2
        value += det1
        e2 = det1
        bsmax = upper_band
        bsmin = lower_band
        jma[i] = value

    return pd.Series(jma, index=close.index, name="jma")


def calculate_dwma(close: pd.Series, length: int = DEFAULT_DWMA_LENGTH) -> pd.Series:
    """Double weighted MA: linear weights 1..length with the oldest bar weighted 1.

    DWMA[i] = (1·C[i−N+1] + 2·C[i−N+2] + … + N·C[i]) / (N·(N+1)/2), so the newest
    close carries the largest weight and the divisor for length 20 is 210. Bars
    before the window is full are NaN, and a NaN inside the window propagates.
    """
    if length < 1:
        raise ValueError("DWMA length must be at least 1")
    prices = pd.to_numeric(close, errors="coerce").astype(float).to_numpy()
    n = len(prices)
    dwma = np.full(n, np.nan)
    if n >= length:
        weights = np.arange(1, length + 1, dtype=float)
        weight_total = length * (length + 1) / 2.0
        windows = np.lib.stride_tricks.sliding_window_view(prices, length)
        dwma[length - 1:] = windows @ weights / weight_total
    return pd.Series(dwma, index=close.index, name="dwma")


CROSSOVER_LABELS = {1: "Positive", -1: "Negative", 0: "None"}


def jma_dwma_crossover_frame(
    close: pd.Series,
    jma_length: int = DEFAULT_JMA_LENGTH,
    jma_phase: float = DEFAULT_JMA_PHASE,
    jma_power: float = DEFAULT_JMA_POWER,
    dwma_length: int = DEFAULT_DWMA_LENGTH,
    warmup_bars: int = DEFAULT_CROSSOVER_WARMUP_BARS,
) -> pd.DataFrame:
    """Per-day JMA vs DWMA comparison from bar warmup+1 onward (the spec output).

    Columns are jma, dwma, jma_prev, dwma_prev, difference (JMA − DWMA) and
    crossover (+1 Positive/bullish, −1 Negative/bearish, 0 None). A Positive day
    needs jma_prev ≤ dwma_prev and jma > dwma; a Negative day is the mirror
    image. Equality on the previous bar therefore resolves in the direction of
    today's move, and equality on both bars is no crossover. Bars whose lines are
    still NaN can never satisfy a comparison, so they are recorded as None.
    """
    if warmup_bars < 1:
        raise ValueError("crossover warm-up must be at least 1 bar")
    jma = calculate_jma(close, length=jma_length, phase=jma_phase, power=jma_power)
    dwma = calculate_dwma(close, length=dwma_length)
    frame = pd.DataFrame({"jma": jma, "dwma": dwma})
    frame["jma_prev"] = frame["jma"].shift(1)
    frame["dwma_prev"] = frame["dwma"].shift(1)
    frame["difference"] = frame["jma"] - frame["dwma"]
    scanned = frame.iloc[warmup_bars:].copy()
    if scanned.empty:
        scanned["crossover"] = pd.Series(dtype=int)
        return scanned
    previous_difference = scanned["jma_prev"] - scanned["dwma_prev"]
    difference = scanned["difference"]
    scanned["crossover"] = np.select(
        [
            (previous_difference <= 0) & (difference > 0),
            (previous_difference >= 0) & (difference < 0),
        ],
        [1, -1],
        default=0,
    ).astype(int)
    return scanned


def crossover_state(
    daily: pd.DataFrame,
    jma_length: int = DEFAULT_JMA_LENGTH,
    jma_phase: float = DEFAULT_JMA_PHASE,
    jma_power: float = DEFAULT_JMA_POWER,
    dwma_length: int = DEFAULT_DWMA_LENGTH,
    warmup_bars: int = DEFAULT_CROSSOVER_WARMUP_BARS,
) -> tuple[Optional[CrossoverState], Optional[str]]:
    """Return (state, failure reason) for one stock's daily JMA/DWMA crossover.

    The scan uses the daily closes already downloaded, so it costs no additional
    market-data requests. ``failure reason`` is None on success; otherwise it
    explains why no reading could be produced (normally too little history for
    the warm-up).
    """
    close = daily_close_series(daily)
    minimum_bars = max(warmup_bars + 1, dwma_length + 1)
    if len(close) < minimum_bars:
        return None, f"fewer than {minimum_bars} daily bars ({len(close)} available)"
    try:
        scanned = jma_dwma_crossover_frame(
            close,
            jma_length=jma_length,
            jma_phase=jma_phase,
            jma_power=jma_power,
            dwma_length=dwma_length,
            warmup_bars=warmup_bars,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"crossover calculation failed ({type(exc).__name__})"
    resolved = scanned.dropna(subset=["jma", "dwma"])
    if resolved.empty:
        return None, f"no bar cleared the {warmup_bars}-bar JMA warm-up with resolved lines"

    last_index = len(resolved) - 1
    last = resolved.iloc[last_index]
    difference = float(last["difference"])
    crosses = resolved.index[resolved["crossover"] != 0]
    crossover_count = int(len(crosses))

    if difference > 0:
        direction = 1
    elif difference < 0:
        direction = -1
    else:
        # Exactly equal on the last bar: keep the side the last crossover set,
        # so the row still reports a date instead of an empty "n/a".
        direction = int(resolved.loc[crosses[-1], "crossover"]) if crossover_count else 0

    if crossover_count:
        last_cross = crosses[-1]
        cross_row = resolved.loc[last_cross]
        change_date: Optional[date] = last_cross.date()
        days_since: Optional[int] = last_index - int(resolved.index.get_loc(last_cross))
        crossover_in_window = True
        last_crossover_type: Optional[int] = int(cross_row["crossover"])
        last_crossover_jma_value: Optional[float] = float(cross_row["jma"])
        last_crossover_dwma_value: Optional[float] = float(cross_row["dwma"])
    else:
        change_date = None
        days_since = None
        crossover_in_window = False
        last_crossover_type = None
        last_crossover_jma_value = None
        last_crossover_dwma_value = None

    return (
        CrossoverState(
            direction=direction,
            jma_value=float(last["jma"]),
            dwma_value=float(last["dwma"]),
            difference=difference,
            change_date=change_date,
            days_since_change=days_since,
            crossover_in_window=crossover_in_window,
            first_scanned_date=resolved.index[0].date(),
            scanned_bars=int(len(resolved)),
            daily_bars=int(len(close)),
            crossover_count=crossover_count,
            last_crossover_type=last_crossover_type,
            last_crossover_jma_value=last_crossover_jma_value,
            last_crossover_dwma_value=last_crossover_dwma_value,
        ),
        None,
    )


def compute_daily_crossovers(
    prices: Mapping[str, pd.DataFrame],
    jma_length: int = DEFAULT_JMA_LENGTH,
    jma_phase: float = DEFAULT_JMA_PHASE,
    jma_power: float = DEFAULT_JMA_POWER,
    dwma_length: int = DEFAULT_DWMA_LENGTH,
    warmup_bars: int = DEFAULT_CROSSOVER_WARMUP_BARS,
) -> tuple[dict[str, CrossoverState], dict[str, str]]:
    """Compute the daily JMA/DWMA crossover for every symbol with price history, in memory."""
    states: dict[str, CrossoverState] = {}
    failures: dict[str, str] = {}
    for symbol in sorted(prices):
        daily = prices.get(symbol)
        if daily is None or daily.empty:
            failures[symbol] = "no price history"
            continue
        state, reason = crossover_state(
            daily,
            jma_length=jma_length,
            jma_phase=jma_phase,
            jma_power=jma_power,
            dwma_length=dwma_length,
            warmup_bars=warmup_bars,
        )
        if state is None:
            failures[symbol] = reason or "unavailable"
        else:
            states[symbol] = state
    return states, failures


def daily_volume_series(daily: pd.DataFrame) -> pd.Series:
    """Return sorted daily volume, preserving missing values for validation."""
    if daily is None or daily.empty:
        return pd.Series(dtype=float, name="volume")
    volume_column = next(
        (column for column in daily.columns if _normalized_column_name(column) == "volume"),
        None,
    )
    if volume_column is None:
        return pd.Series(dtype=float, name="volume")
    frame = daily[[volume_column]].copy()
    frame.columns = ["volume"]
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.sort_index(kind="stable")
    frame = frame[frame.index.notna()]
    frame = frame[~frame.index.duplicated(keep="last")]
    volume = pd.to_numeric(frame["volume"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    volume = volume.where(volume >= 0)
    volume.name = "volume"
    return volume.astype(float)


def classify_entry_candidate(
    stock: RankedStock,
    daily: Optional[pd.DataFrame],
    supertrend: Optional[SupertrendReading],
    crossover: Optional[CrossoverState],
) -> EntryCandidate:
    """Apply the separate BUY/WATCH/AVOID-WAIT rules to one score-qualified stock.

    A "fresh" Positive JMA/DWMA cross means the cross occurred on the latest
    completed daily bar. The volume multiple is today's completed-session volume
    divided by the mean of the *previous* 20 completed daily volumes (today is
    excluded from its own baseline).

    For an explicit deterioration rule, this screen treats weekly Supertrend
    NEGATIVE, daily Supertrend NEGATIVE, current JMA below DWMA, or close below
    EMA(20) as significant deterioration. Overextension or an unmet volume/cross
    trigger alone is WATCH, not AVOID.
    """
    close_series = daily_close_series(daily) if daily is not None else pd.Series(dtype=float, name="close")
    latest_date = pd.Timestamp(close_series.index[-1]).date() if not close_series.empty else None
    close = float(close_series.iloc[-1]) if not close_series.empty else float("nan")
    ema_series = close_series.ewm(span=ENTRY_EMA_LENGTH, adjust=False, min_periods=ENTRY_EMA_LENGTH).mean()
    ema20 = float(ema_series.iloc[-1]) if not ema_series.empty and np.isfinite(ema_series.iloc[-1]) else float("nan")

    volume = average_volume = volume_multiple = float("nan")
    if daily is not None and latest_date is not None:
        aligned_volume = daily_volume_series(daily).reindex(close_series.index)
        volume_window = aligned_volume.tail(ENTRY_VOLUME_LOOKBACK + 1)
        if len(volume_window) == ENTRY_VOLUME_LOOKBACK + 1 and np.isfinite(volume_window.to_numpy(dtype=float)).all():
            volume = float(volume_window.iloc[-1])
            average_volume = float(volume_window.iloc[:-1].mean())
            if average_volume > 0:
                volume_multiple = volume / average_volume

    weekly = supertrend.weekly if supertrend is not None else None
    daily_st = supertrend.daily if supertrend is not None else None
    weekly_direction = weekly.direction if weekly is not None else 0
    daily_direction = daily_st.direction if daily_st is not None else 0
    weekly_label = weekly.direction_label if weekly is not None else "n/a"
    daily_label = daily_st.direction_label if daily_st is not None else "n/a"

    fresh_positive_cross = bool(
        crossover is not None
        and latest_date is not None
        and crossover.last_crossover_type == 1
        and crossover.change_date == latest_date
        and crossover.days_since_change == 0
    )
    crossover_type = crossover.last_crossover_label if crossover is not None else "n/a"
    crossover_date = crossover.change_date if crossover is not None else None

    has_ema = np.isfinite(ema20) and np.isfinite(close)
    extension_pct = (close / ema20 - 1.0) * 100.0 if has_ema and ema20 > 0 else float("nan")
    deteriorations: list[str] = []
    if weekly_direction < 0:
        deteriorations.append("Weekly Supertrend is NEG")
    if daily_direction < 0:
        deteriorations.append("Daily Supertrend is NEG")
    if crossover is not None and crossover.direction < 0:
        deteriorations.append("JMA is below DWMA")
    if has_ema and close < ema20:
        deteriorations.append("Close is below EMA20")

    entry_gaps: list[str] = []
    if weekly_direction != 1:
        entry_gaps.append("Weekly Supertrend is not POS")
    if not fresh_positive_cross:
        entry_gaps.append("No Positive JMA/DWMA cross on the latest session")
    if not has_ema:
        entry_gaps.append("EMA20 unavailable")
    elif close <= ema20:
        entry_gaps.append("Close is not above EMA20")
    if not np.isfinite(volume_multiple):
        entry_gaps.append("20-day volume comparison unavailable")
    elif volume_multiple < ENTRY_MIN_VOLUME_MULTIPLE:
        entry_gaps.append(f"Volume is below {ENTRY_MIN_VOLUME_MULTIPLE:.1f}× prior 20-day average")
    if has_ema and close > ENTRY_MAX_EMA_EXTENSION * ema20:
        entry_gaps.append(f"Close is more than {(ENTRY_MAX_EMA_EXTENSION - 1) * 100:.0f}% above EMA20")

    if deteriorations:
        classification = "AVOID/WAIT"
        reasons = tuple(dict.fromkeys(deteriorations + entry_gaps))
    elif not entry_gaps:
        classification = "BUY"
        reasons = ("All entry rules passed",)
    else:
        classification = "WATCH"
        reasons = tuple(dict.fromkeys(entry_gaps))

    return EntryCandidate(
        symbol=stock.symbol,
        residual_score=float(stock.score),
        classification=classification,
        latest_date=latest_date,
        close=close,
        ema20=ema20,
        volume=volume,
        average_volume=average_volume,
        volume_multiple=volume_multiple,
        ema_extension_pct=extension_pct,
        weekly_supertrend=weekly_label,
        daily_supertrend=daily_label,
        crossover_type=crossover_type,
        crossover_date=crossover_date,
        fresh_positive_cross=fresh_positive_cross,
        reasons=reasons,
    )


def screen_entry_candidates(
    stocks: Sequence[RankedStock],
    prices: Mapping[str, pd.DataFrame],
    supertrend_states: Mapping[str, SupertrendReading],
    crossover_states: Mapping[str, CrossoverState],
) -> tuple[list[EntryCandidate], int]:
    """Screen all main-screen eligible names at residual score >= 6, not only top basket."""
    candidates: list[EntryCandidate] = []
    below_threshold = 0
    for stock in stocks:
        if not np.isfinite(stock.score) or stock.score < ENTRY_MIN_RESIDUAL_SCORE:
            below_threshold += 1
            continue
        candidates.append(
            classify_entry_candidate(
                stock=stock,
                daily=prices.get(stock.symbol),
                supertrend=supertrend_states.get(stock.symbol),
                crossover=crossover_states.get(stock.symbol),
            )
        )
    order = {status: index for index, status in enumerate(ENTRY_STATUS_ORDER)}
    candidates.sort(key=lambda item: (order[item.classification], -item.residual_score, item.symbol))
    return candidates, below_threshold


def residual_volatility_passes(
    residual_volatility: float,
    maximum: float = DEFAULT_MAX_MONTHLY_RESIDUAL_VOLATILITY,
) -> bool:
    """Return true at/below the monthly residual-volatility ceiling."""
    return bool(np.isfinite(residual_volatility) and 0 <= residual_volatility <= maximum)


def median_daily_turnover(
    daily: pd.DataFrame,
    window: int = LIQUIDITY_LOOKBACK_SESSIONS,
) -> float:
    """Median close*volume over the trailing ~three months of daily bars, INR/day."""
    if daily.empty or len(daily) < window or not {"close", "volume"}.issubset(daily.columns):
        return float("nan")
    last = daily.sort_index().tail(window)
    close = pd.to_numeric(last["close"], errors="coerce")
    volume = pd.to_numeric(last["volume"], errors="coerce")
    turnover = (close * volume).replace([np.inf, -np.inf], np.nan).dropna()
    if len(turnover) < window:
        return float("nan")
    return float(turnover.median())


def rank_top_decile(stocks: Iterable[RankedStock], decile_fraction: float = 0.10) -> tuple[list[RankedStock], list[RankedStock]]:
    """Return deterministic ranked list and its top decile (ceil, minimum one)."""
    if not 0 < decile_fraction <= 1:
        raise ValueError("decile_fraction must be in (0, 1]")
    ranked = sorted(stocks, key=lambda item: (-item.score, item.symbol))
    if not ranked:
        return ranked, []
    count = max(1, int(math.ceil(len(ranked) * decile_fraction)))
    return ranked, ranked[:count]


def construct_long_portfolio(
    stocks: Iterable[RankedStock],
    portfolio_size: int = DEFAULT_PORTFOLIO_SIZE,
    current_holdings: Optional[set[str]] = None,
    entry_buffer_fraction: float = ENTRY_BUFFER_FRACTION,
    exit_buffer_fraction: float = EXIT_BUFFER_FRACTION,
) -> tuple[list[PortfolioMember], BufferSummary]:
    """Select a buffered long-only basket and compute inverse-vol model weights.

    With no holdings file, this is an initial screen: take the top ``portfolio_size``.
    When holdings are supplied, retain eligible incumbents through the top-15%
    exit buffer and admit new names only from the top-8% entry buffer. If that
    leaves vacancies, they remain unfilled rather than weakening the entry rule.
    """
    if portfolio_size < 1:
        raise ValueError("portfolio_size must be at least one")
    if not 0 < entry_buffer_fraction <= exit_buffer_fraction <= 1:
        raise ValueError("buffer fractions must satisfy 0 < entry <= exit <= 1")

    ranked = sorted(stocks, key=lambda item: (-item.score, item.symbol))
    count = len(ranked)
    entry_cutoff = max(1, int(math.ceil(count * entry_buffer_fraction))) if count else 0
    exit_cutoff = max(1, int(math.ceil(count * exit_buffer_fraction))) if count else 0
    rank_by_symbol = {stock.symbol: rank for rank, stock in enumerate(ranked, start=1)}
    held = {symbol.strip().upper() for symbol in current_holdings} if current_holdings is not None else None

    if held is None:
        selected = ranked[:portfolio_size]
        buffer_summary = BufferSummary(
            applied=False,
            current_holding_count=0,
            entry_rank_cutoff=entry_cutoff,
            exit_rank_cutoff=exit_cutoff,
        )
    else:
        retained_candidates = [
            stock for rank, stock in enumerate(ranked, start=1)
            if stock.symbol in held and rank <= exit_cutoff
        ]
        selected = retained_candidates[:portfolio_size]
        selected_symbols = {stock.symbol for stock in selected}
        new_candidates = [
            stock for rank, stock in enumerate(ranked, start=1)
            if stock.symbol not in held and rank <= entry_cutoff
        ]
        selected.extend(new_candidates[: max(0, portfolio_size - len(selected))])
        selected_symbols = {stock.symbol for stock in selected}
        held_ranked = held.intersection(rank_by_symbol)
        held_below_buffer = {
            symbol for symbol in held_ranked if rank_by_symbol[symbol] > exit_cutoff
        }
        held_retained_by_rank = {
            stock.symbol for stock in retained_candidates
        }
        held_capacity_exit = held_retained_by_rank - selected_symbols
        buffer_summary = BufferSummary(
            applied=True,
            current_holding_count=len(held),
            entry_rank_cutoff=entry_cutoff,
            exit_rank_cutoff=exit_cutoff,
            retained=tuple(sorted(held.intersection(selected_symbols))),
            new_entries=tuple(sorted(selected_symbols - held)),
            exits_not_eligible=tuple(sorted(held - held_ranked)),
            exits_below_buffer=tuple(sorted(held_below_buffer)),
            exits_capacity=tuple(sorted(held_capacity_exit)),
        )

    selected.sort(key=lambda item: (-item.score, item.symbol))
    if not selected:
        return [], buffer_summary

    inverse_vol = np.asarray([1.0 / stock.residual_volatility for stock in selected], dtype=float)
    if not np.isfinite(inverse_vol).all() or np.any(inverse_vol <= 0):
        raise DataQualityError("Cannot compute finite inverse-residual-volatility weights.")
    normalized = inverse_vol / inverse_vol.sum()
    portfolio_beta = float(sum(weight * stock.beta_market for weight, stock in zip(normalized, selected)))
    # A common gross scale cannot change weighted-average beta; it can only
    # change beta notional. For beta > 1.3, show the hypothetical scale that
    # reduces beta notional toward 1.0. Never lever up a low-beta portfolio.
    gross_scale = 1.0 / portfolio_beta if portfolio_beta > BETA_UPPER_BOUND else 1.0

    members = [
        PortfolioMember(
            stock=stock,
            cross_section_rank=rank_by_symbol[stock.symbol],
            percentile_rank=100.0 * rank_by_symbol[stock.symbol] / count,
            inverse_vol_weight=float(weight),
            gross_adjusted_weight=float(weight * gross_scale),
        )
        for stock, weight in zip(selected, normalized)
    ]
    return members, buffer_summary


def summarize_portfolio_beta(members: Sequence[PortfolioMember]) -> tuple[float, float, str]:
    """Return weighted beta, informational gross scale, and explanation."""
    if not members:
        return float("nan"), 1.0, "unavailable: no selected names"
    beta = float(sum(member.inverse_vol_weight * member.stock.beta_market for member in members))
    if beta > BETA_UPPER_BOUND:
        scale = 1.0 / beta
        return beta, scale, (
            f"above {BETA_UPPER_BOUND:.1f}; indicative gross scale {scale:.3f}x "
            "would bring beta notional toward 1.0 (weighted-average beta itself is unchanged)"
        )
    if beta < BETA_LOWER_BOUND:
        if beta > 0:
            leverage_needed = 1.0 / beta
            return beta, 1.0, (
                f"below {BETA_LOWER_BOUND:.1f}; targeting unit beta notional would require "
                f"{leverage_needed:.3f}x gross exposure; no leverage/scale-up is applied"
            )
        return beta, 1.0, (
            f"below {BETA_LOWER_BOUND:.1f}; positive gross scaling cannot target beta 1.0; "
            "no leverage/scale-up is applied"
        )
    return beta, 1.0, f"within the [{BETA_LOWER_BOUND:.1f}, {BETA_UPPER_BOUND:.1f}] monitor band"


def _has_crossover_reading(state: Optional[CrossoverState]) -> bool:
    # The state exists only after both lines resolved past warm-up. A zero spread
    # is a valid (neutral) reading and must still sort by its last crossover date.
    return state is not None


def _crossover_sort_key(state: Optional[CrossoverState]) -> tuple[date, int]:
    """Sort key for the crossover-since column, for names that do have a reading.

    A stock whose JMA never crossed the DWMA inside the scanned window has the
    *oldest* signal in the table, so it sorts before every dated crossover
    (secondary key -1) and therefore after every dated crossover when the order
    is reversed. Sorting on the date is the same as sorting on "days since the
    crossover" in the opposite direction.
    """
    if state.change_date is not None:
        return (state.change_date, 0)
    if state.first_scanned_date is not None:
        return (state.first_scanned_date, -1)
    return (date.min, -1)


def sort_members_by_crossover(
    members: Sequence[PortfolioMember],
    crossover_states: Mapping[str, CrossoverState],
    order: str = DEFAULT_CROSSOVER_SORT,
) -> list[PortfolioMember]:
    """Return the selected names ordered for display; ties keep the score order.

    The table order is driven by the daily JMA/DWMA crossover day, not by the
    Supertrend flip. Sorting is stable, so two stocks that crossed on the same
    session stay in residual-momentum rank order; the momentum rank itself is
    untouched and is still printed in the Rank/%ile column. Names with no
    crossover reading are held to the end in both directions, so they never
    displace a readable row.
    """
    ordered = list(members)
    if order not in ("asc", "desc"):
        return ordered
    readable = [m for m in ordered if _has_crossover_reading(crossover_states.get(m.stock.symbol))]
    unreadable = [m for m in ordered if not _has_crossover_reading(crossover_states.get(m.stock.symbol))]
    readable.sort(
        key=lambda member: _crossover_sort_key(crossover_states[member.stock.symbol]),
        reverse=(order == "desc"),
    )
    return readable + unreadable


def _has_supertrend_reading(state: Optional[SupertrendState]) -> bool:
    """Legacy weekly-reading predicate retained for downstream callers."""
    return state is not None and state.direction != 0


def _supertrend_sort_key(state: Optional[SupertrendState]) -> tuple[date, int]:
    """Legacy weekly Supertrend sort key; the main screen sorts on crossovers."""
    if state.change_date is not None:
        return (state.change_date, 0)
    if state.first_resolved_date is not None:
        return (state.first_resolved_date, -1)
    return (date.min, -1)


def sort_members_by_supertrend(
    members: Sequence[PortfolioMember],
    supertrend_states: Mapping[str, SupertrendState],
    order: str = DEFAULT_SUPERTREND_SORT,
) -> list[PortfolioMember]:
    """Legacy weekly Supertrend sorting helper, no longer used by the screen."""
    ordered = list(members)
    if order not in ("asc", "desc"):
        return ordered
    readable = [m for m in ordered if _has_supertrend_reading(supertrend_states.get(m.stock.symbol))]
    unreadable = [m for m in ordered if not _has_supertrend_reading(supertrend_states.get(m.stock.symbol))]
    readable.sort(
        key=lambda member: _supertrend_sort_key(supertrend_states[member.stock.symbol]),
        reverse=(order == "desc"),
    )
    return readable + unreadable


def summarize_crossover_counts(
    members: Sequence[PortfolioMember],
    crossover_states: Mapping[str, CrossoverState],
) -> tuple[int, int, int]:
    """Count positive / negative / unavailable JMA-DWMA readings in the table."""
    return _summarize_direction_counts(
        members,
        lambda symbol: crossover_states.get(symbol),
    )


def summarize_supertrend_counts(
    members: Sequence[PortfolioMember],
    supertrend_states: Mapping[str, SupertrendReading],
    timeframe: str = "weekly",
) -> tuple[int, int, int]:
    """Count positive / negative / unavailable Supertrend readings on one timeframe."""
    if timeframe not in SUPERTREND_TIMEFRAMES:
        raise ValueError(f"unknown Supertrend timeframe: {timeframe!r}")

    def lookup(symbol: str) -> Optional[SupertrendState]:
        reading = supertrend_states.get(symbol)
        return reading.state(timeframe) if reading is not None else None

    return _summarize_direction_counts(members, lookup)


def _summarize_direction_counts(
    members: Sequence[PortfolioMember],
    lookup: Callable[[str], Optional[Any]],
) -> tuple[int, int, int]:
    """Shared POS / NEG / unavailable tally for any per-symbol direction reading."""
    positive = negative = unavailable = 0
    for member in members:
        state = lookup(member.stock.symbol)
        if state is None or state.direction == 0:
            unavailable += 1
        elif state.direction > 0:
            positive += 1
        else:
            negative += 1
    return positive, negative, unavailable


def _latest_close_before_today(daily: pd.DataFrame) -> float:
    if daily.empty or "close" not in daily.columns:
        return float("nan")
    close = pd.to_numeric(daily.sort_index()["close"], errors="coerce").dropna()
    close = close[close > 0]
    return float(close.iloc[-1]) if not close.empty else float("nan")


def _asof_month_for_screen(factors: pd.DataFrame, today: date) -> pd.Period:
    completed = last_completed_month(today)
    latest_factor = factors.index.max()
    return min(completed, latest_factor)


def _fetch_prices(
    instruments: Sequence[Instrument],
    client: UpstoxClient,
    start_date: date,
    end_date: date,
    today: date,
    workers: int,
) -> tuple[dict[str, pd.DataFrame], dict[str, str], int]:
    """Fetch/cache candles concurrently and report periodic console progress."""
    prices: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}
    cache_hits = 0
    auth_error: Optional[UpstoxAuthenticationError] = None
    max_workers = max(1, min(int(workers), 8))
    total = len(instruments)
    if total == 0:
        return prices, failures, cache_hits

    progress_every = max(1, min(25, math.ceil(total / 20)))
    started = time.monotonic()
    completed = 0
    console_status(
        f"[Prices] Starting {total} daily-history requests with {max_workers} workers "
        "(request pacing is enabled)."
    )
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(client.get_daily_candles, inst, start_date, end_date, today): inst
            for inst in instruments
        }
        for future in as_completed(futures):
            inst = futures[future]
            try:
                frame, cached = future.result()
                if frame.empty:
                    failures[inst.symbol] = "no candles returned"
                else:
                    prices[inst.symbol] = frame
                    cache_hits += int(cached)
            except UpstoxAuthenticationError as exc:
                auth_error = exc
                console_status("[Prices] Upstox rejected the access token; stopping history downloads.")
                for pending in futures:
                    pending.cancel()
                completed += 1
                break
            except (UpstoxRequestError, requests.RequestException, ValueError, OSError) as exc:
                failures[inst.symbol] = str(exc)
            finally:
                # The auth-error branch increments before breaking above.
                if auth_error is None:
                    completed += 1
                if completed % progress_every == 0 or completed == total:
                    elapsed = time.monotonic() - started
                    percent = 100.0 * completed / total
                    console_status(
                        f"[Prices] {completed}/{total} ({percent:.0f}%) | "
                        f"loaded={len(prices)} | cached={cache_hits} | failed={len(failures)} | "
                        f"elapsed={elapsed:.0f}s"
                    )
    if auth_error is not None:
        raise auth_error

    elapsed = time.monotonic() - started
    console_status(
        f"[Prices] Finished: {len(prices)} histories loaded, {len(failures)} failed, "
        f"{cache_hits} cache hits in {elapsed:.0f}s."
    )
    return prices, failures, cache_hits


def _make_ranked_universe(
    instruments: Sequence[Instrument],
    prices: Mapping[str, pd.DataFrame],
    factors: pd.DataFrame,
    as_of_month: pd.Period,
    min_turnover_inr: float,
    today: date,
    max_residual_volatility: float = DEFAULT_MAX_MONTHLY_RESIDUAL_VOLATILITY,
) -> tuple[list[RankedStock], dict[str, int]]:
    ranked_candidates: list[RankedStock] = []
    reasons: dict[str, int] = {}

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for instrument in instruments:
        daily = prices.get(instrument.symbol)
        if daily is None or daily.empty:
            reject("no price history")
            continue
        latest_bar_date = daily.index.max().date()
        if (today - latest_bar_date).days > MAX_LAST_DAILY_BAR_AGE_DAYS:
            reject("stale last candle (>10 calendar days)")
            continue
        latest_price = _latest_close_before_today(daily)
        if not np.isfinite(latest_price):
            reject("invalid latest price")
            continue
        if latest_price < MIN_PRICE_INR:
            reject(f"price below ₹{MIN_PRICE_INR:.0f}")
            continue

        turnover = median_daily_turnover(daily, window=LIQUIDITY_LOOKBACK_SESSIONS)
        if not np.isfinite(turnover):
            reject(f"fewer than {LIQUIDITY_LOOKBACK_SESSIONS} valid daily bars")
            continue
        if turnover < min_turnover_inr:
            reject("below median-turnover threshold")
            continue

        monthly_close = monthly_closes_from_daily(daily)
        # Reindex to a continuous monthly calendar so a missing month is not
        # silently converted into a zero return by pandas' default fill.
        start_price_month = as_of_month - (REGRESSION_MONTHS + 1)
        required_price_months = pd.period_range(
            start=start_price_month, end=as_of_month, freq="M", name="month"
        )
        monthly_close = monthly_close.reindex(required_price_months)
        monthly_returns = monthly_close.pct_change(fill_method=None)
        monthly_returns = monthly_returns.replace([np.inf, -np.inf], np.nan)
        try:
            result = calculate_residual_momentum(
                monthly_stock_returns=monthly_returns,
                factors=factors,
                as_of_month=as_of_month,
            )
        except (DataQualityError, ValueError) as exc:
            reason = str(exc)
            if reason.startswith("incomplete 36-month window"):
                reason = "incomplete 36-month price/factor window"
            reject(reason)
            continue

        if not residual_volatility_passes(result.residual_volatility, max_residual_volatility):
            reject(
                f"monthly residual volatility above {max_residual_volatility * 100:.1f}%"
            )
            continue

        ranked_candidates.append(
            RankedStock(
                symbol=instrument.symbol,
                score=result.score,
                residual_sum=result.residual_sum,
                residual_volatility=result.residual_volatility,
                beta_market=result.beta_market,
                beta_smb=result.beta_smb,
                beta_hml=result.beta_hml,
                r_squared=result.r_squared,
                latest_price=latest_price,
                median_daily_turnover_inr=turnover,
            )
        )
    return ranked_candidates, reasons


def _format_money_inr(value: float) -> str:
    if not np.isfinite(value):
        return "n/a"
    return f"₹{value:,.2f}"


def print_screen(
    members: Sequence[PortfolioMember],
    eligible_count: int,
    mapped_count: int,
    requested_count: int,
    as_of_month: pd.Period,
    factor_month: pd.Period,
    factor_lag_months: int,
    factor_source: str,
    min_turnover_inr: float,
    max_residual_volatility: float,
    portfolio_size: int,
    buffer_summary: BufferSummary,
    portfolio_beta: float,
    gross_scale: float,
    beta_message: str,
    holdings_file: Optional[Path],
    price_failures: Mapping[str, str],
    filter_reasons: Mapping[str, int],
    cache_hits: int,
    crossover_states: Optional[Mapping[str, CrossoverState]] = None,
    crossover_failures: Optional[Mapping[str, str]] = None,
    jma_length: int = DEFAULT_JMA_LENGTH,
    jma_phase: float = DEFAULT_JMA_PHASE,
    jma_power: float = DEFAULT_JMA_POWER,
    dwma_length: int = DEFAULT_DWMA_LENGTH,
    crossover_warmup_bars: int = DEFAULT_CROSSOVER_WARMUP_BARS,
    crossover_sort: str = DEFAULT_CROSSOVER_SORT,
    supertrend_states: Optional[Mapping[str, SupertrendReading]] = None,
    supertrend_failures: Optional[Mapping[str, str]] = None,
    supertrend_period: int = DEFAULT_SUPERTREND_PERIOD,
    supertrend_multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> None:
    crossover_states = crossover_states or {}
    crossover_failures = crossover_failures or {}
    supertrend_states = supertrend_states or {}
    supertrend_failures = supertrend_failures or {}
    ordered_members = sort_members_by_crossover(members, crossover_states, crossover_sort)
    print("\nRESIDUAL MOMENTUM — NSE STOCK SCREEN (INFORMATIONAL ONLY; NO ORDERS)\n")
    print(f"Signal / return data through : {as_of_month}")
    print(f"Indian factor data through   : {factor_month} (lag to latest full month: {factor_lag_months})")
    print(f"Factor provider/source       : {factor_source}")
    if "SCDLDS" in factor_source.upper():
        print("Factor-use note               : SCDLDS marks its beta library free for academic use; verify rights for other uses.")
    print("Model                         : India FF3 (Mkt-RF, SMB, HML); 36 monthly OLS observations")
    print("Signal                        : 12-1 residual momentum; 11 months t-12..t-2; t-1 skipped; alpha excluded")
    print("Return basis                  : Upstox close-to-close; dividend adjustment is not assured")
    print(f"Minimum price                 : ₹{MIN_PRICE_INR:.2f} at latest completed daily close")
    print(
        f"Liquidity filter              : {LIQUIDITY_LOOKBACK_SESSIONS}-session median close×volume "
        f"≥ ₹{min_turnover_inr / 10_000_000:,.2f} Cr/day"
    )
    print(
        f"Residual-volatility filter    : monthly formation residual volatility ≤ "
        f"{max_residual_volatility * 100:.1f}%"
    )
    print(f"Freshness filter              : latest daily bar no more than {MAX_LAST_DAILY_BAR_AGE_DAYS} calendar days old")
    print(f"Universe                      : {requested_count} requested; {mapped_count} exact NSE EQ mappings")
    print(f"Eligible after filters        : {eligible_count}; cached price histories: {cache_hits}")
    print(f"Long basket                   : up to {portfolio_size}; selected {len(members)}")
    print(f"Table order                   : {CROSSOVER_SORT_LABELS.get(crossover_sort, crossover_sort)}")
    print(
        f"Daily JMA/DWMA crossover      : JMA({jma_length}, phase {jma_phase:g}, power {jma_power:g}) as the fast "
        f"line over DWMA({dwma_length}) as the slow line, on the daily closes already downloaded; the first "
        f"{crossover_warmup_bars} bars are JMA warm-up and scanning starts on bar {crossover_warmup_bars + 1}"
    )
    print(
        "Crossover rules               : POSITIVE when JMA[i-1] ≤ DWMA[i-1] and JMA[i] > DWMA[i]; NEGATIVE when "
        "JMA[i-1] ≥ DWMA[i-1] and JMA[i] < DWMA[i]; otherwise no crossover that day"
    )
    print(
        "Crossover legend              : 'Last cross' = Positive/Negative/None; 'X since' = last crossover session "
        "(Nd = trading sessions since); 'JMA @ X'/'DWMA @ X' are values on that session; 'Δ now' is the current "
        "JMA − DWMA spread"
    )
    print(
        "Crossover caveats             : '≤ date' = no crossover since the first scanned bar (use "
        "--history-extra-months to load more); the supplied recurrence derives pow1 from length, so "
        f"power {jma_power:g} is reported but does not alter the line; Upstox close adjustment is not assured"
    )
    print(
        f"Weekly Supertrend             : ATR({supertrend_period}) × {supertrend_multiplier:g} on weekly bars "
        f"(W-FRI) resampled from the daily history already downloaded"
    )
    print(
        f"Daily Supertrend              : the same ATR({supertrend_period}) × {supertrend_multiplier:g} applied to "
        "daily sessions, so its trend change is dated by the session that flipped it"
    )
    print(
        "Supertrend legend             : POS = line below price (bullish); NEG = line above price (bearish); "
        "'ST W since'/'ST D since' = week/session the trend last flipped, (Nw)/(Nd) = bars since"
    )
    print(
        "Supertrend caveats            : '≤ date' = trend older than the loaded history (use "
        "--history-extra-months to load more); the newest weekly bar may be the in-progress week, "
        "dated by its last session"
    )

    if buffer_summary.applied:
        print(
            f"Entry/exit buffers            : top {ENTRY_BUFFER_FRACTION * 100:.0f}% entry "
            f"(rank ≤ {buffer_summary.entry_rank_cutoff}); retain through top "
            f"{EXIT_BUFFER_FRACTION * 100:.0f}% (rank ≤ {buffer_summary.exit_rank_cutoff})"
        )
        print(f"Holdings input                : {holdings_file} ({buffer_summary.current_holding_count} symbols)")
        print(f"Retained holdings              : {len(buffer_summary.retained)}")
        print(f"New buffer-qualified names     : {len(buffer_summary.new_entries)}")
        if buffer_summary.exits_not_eligible:
            print("Held but no longer eligible   : " + ", ".join(buffer_summary.exits_not_eligible))
        if buffer_summary.exits_below_buffer:
            print("Held beyond exit buffer       : " + ", ".join(buffer_summary.exits_below_buffer))
        if buffer_summary.exits_capacity:
            print("Held beyond basket-size cap   : " + ", ".join(buffer_summary.exits_capacity))
        if len(members) < portfolio_size:
            print("Basket below size cap         : no outside-buffer names added; weights normalize among selected names only")
    else:
        print("Entry/exit buffers            : not applied (no --holdings-file; initial top-score screen)")
        print("Buffer thresholds             : new names top 8%; incumbents retained through top 15%")

    if np.isfinite(portfolio_beta):
        print(f"Portfolio beta (inverse-vol)   : {portfolio_beta:.3f} — {beta_message}")
        print(
            f"Indicative gross multiplier   : {gross_scale:.3f}x; informational only, "
            "not applied as an order or position change"
        )
    else:
        print("Portfolio beta (inverse-vol)   : unavailable (no selected names)")
    print()

    if not members:
        print("No names selected under the active eligibility and entry-buffer rules.")
    else:
        headers = (
            "#",
            "Rank/%ile",
            "Symbol",
            "Score",
            "Residual sum",
            "Residual vol/mo",
            "Beta Mkt",
            "IV weight",
            "Gross weight",
            "Last close",
            "Median turnover/day",
            "Last cross",
            "X since",
            "JMA @ X",
            "DWMA @ X",
            "Δ now",
            f"ST W ({supertrend_period},{supertrend_multiplier:g})",
            "ST W since",
            "ST W level",
            f"ST D ({supertrend_period},{supertrend_multiplier:g})",
            "ST D since",
        )
        rows: list[tuple[str, ...]] = []
        for position, member in enumerate(ordered_members, start=1):
            stock = member.stock
            cross = crossover_states.get(stock.symbol)
            reading = supertrend_states.get(stock.symbol)
            weekly = reading.weekly if reading is not None else None
            daily_st = reading.daily if reading is not None else None
            rows.append(
                (
                    str(position),
                    f"{member.cross_section_rank}/{member.percentile_rank:.1f}%",
                    stock.symbol,
                    f"{stock.score:.3f}",
                    f"{stock.residual_sum * 100:.2f}%",
                    f"{stock.residual_volatility * 100:.2f}%",
                    f"{stock.beta_market:.2f}",
                    f"{member.inverse_vol_weight * 100:.2f}%",
                    f"{member.gross_adjusted_weight * 100:.2f}%",
                    _format_money_inr(stock.latest_price),
                    f"₹{stock.median_daily_turnover_inr / 10_000_000:,.2f} Cr",
                    cross.last_crossover_label if cross is not None else "n/a",
                    cross.since_label() if cross is not None else "n/a",
                    _format_money_inr(cross.last_crossover_jma_value)
                    if cross is not None and cross.last_crossover_jma_value is not None
                    else "n/a",
                    _format_money_inr(cross.last_crossover_dwma_value)
                    if cross is not None and cross.last_crossover_dwma_value is not None
                    else "n/a",
                    cross.difference_label() if cross is not None else "n/a",
                    weekly.direction_label if weekly is not None else "n/a",
                    weekly.since_label() if weekly is not None else "n/a",
                    _format_money_inr(weekly.value) if weekly is not None else "n/a",
                    daily_st.direction_label if daily_st is not None else "n/a",
                    daily_st.since_label() if daily_st is not None else "n/a",
                )
            )
        widths = [
            max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))
        ]
        print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
        print("  ".join("-" * widths[i] for i in range(len(headers))))
        for row in rows:
            print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))

        # Printed whenever there is anything to say, including the case where no
        # symbol had enough history: a silent omission would hide the failure.
        if crossover_states or crossover_failures:
            positive, negative, unavailable = summarize_crossover_counts(members, crossover_states)
            fresh = sum(
                1
                for member in members
                if (state := crossover_states.get(member.stock.symbol)) is not None
                and state.change_date is not None
                and state.days_since_change is not None
                and state.days_since_change <= CROSSOVER_FRESH_SESSIONS
            )
            print(
                f"\nDaily JMA/DWMA in this basket : {positive} POS / {negative} NEG"
                + (f" / {unavailable} unavailable" if unavailable else "")
                + f"; {fresh} crossed within the last {CROSSOVER_FRESH_SESSIONS} session(s)"
                + f"  (computed for {len(crossover_states)} symbol(s) with price history"
                + (f"; {len(crossover_failures)} unavailable" if crossover_failures else "")
                + ")"
            )
        if supertrend_states or supertrend_failures:
            weekly_positive, weekly_negative, weekly_na = summarize_supertrend_counts(
                members, supertrend_states, "weekly"
            )
            daily_positive, daily_negative, daily_na = summarize_supertrend_counts(
                members, supertrend_states, "daily"
            )
            print(
                f"Supertrend in this basket     : weekly {weekly_positive} POS / {weekly_negative} NEG"
                + (f" / {weekly_na} unavailable" if weekly_na else "")
                + f"; daily {daily_positive} POS / {daily_negative} NEG"
                + (f" / {daily_na} unavailable" if daily_na else "")
                + f"  (computed for {len(supertrend_states)} symbol(s) with price history"
                + (f"; {len(supertrend_failures)} unavailable" if supertrend_failures else "")
                + ")"
            )

    if filter_reasons:
        summary = ", ".join(f"{count} {reason}" for reason, count in sorted(filter_reasons.items()))
        print(f"\nExcluded by validation/filters: {summary}")
    if price_failures:
        examples = list(price_failures.items())[:12]
        sample = ", ".join(f"{symbol} ({reason})" for symbol, reason in examples)
        remainder = len(price_failures) - len(examples)
        suffix = f"; and {remainder} more" if remainder > 0 else ""
        print(f"Price-data failures: {len(price_failures)} — {sample}{suffix}")
    print(
        "\nThis is an informational screen/model basket only. Weights and beta scaling are illustrative; "
        "it creates no orders, target quantities, or trade instructions."
    )


def print_entry_candidate_screen(
    candidates: Sequence[EntryCandidate],
    screened_count: int,
    below_score_threshold: int,
) -> None:
    """Print the post-screen buy-candidate review; this is informational only."""
    counts = {status: 0 for status in ENTRY_STATUS_ORDER}
    for candidate in candidates:
        counts[candidate.classification] = counts.get(candidate.classification, 0) + 1

    print("\nENTRY CANDIDATE REVIEW — INFORMATIONAL ONLY; NO ORDERS\n")
    print(
        f"Universe                       : {screened_count} main-screen eligible stocks with residual score ≥ "
        f"{ENTRY_MIN_RESIDUAL_SCORE:g}; {below_score_threshold} eligible stock(s) below the score threshold excluded"
    )
    print(
        f"BUY rules                      : score ≥ {ENTRY_MIN_RESIDUAL_SCORE:g}; weekly Supertrend POS; Positive JMA/DWMA "
        f"cross on latest completed daily bar; close > EMA({ENTRY_EMA_LENGTH}); volume ≥ "
        f"{ENTRY_MIN_VOLUME_MULTIPLE:.1f}× prior {ENTRY_VOLUME_LOOKBACK}-day average; close ≤ "
        f"{ENTRY_MAX_EMA_EXTENSION:.2f}× EMA({ENTRY_EMA_LENGTH})"
    )
    print(
        "Fresh/volume convention        : crossover must be on the latest completed session; volume compares the "
        "latest session with the preceding 20 sessions (latest session excluded from its average)"
    )
    print(
        "AVOID/WAIT deterioration rule  : weekly ST NEG, daily ST NEG, JMA below DWMA, or close below EMA20; "
        "overextension or a missing entry trigger alone is WATCH"
    )
    print(
        "Classification counts         : "
        + " / ".join(f"{status} {counts.get(status, 0)}" for status in ENTRY_STATUS_ORDER)
    )
    if not candidates:
        print("No main-screen eligible stocks met the residual-score threshold of 6.\n")
        return

    headers = (
        "Status",
        "Symbol",
        "Data as of",
        "Residual",
        "ST W",
        "ST D",
        "Last JMA/DWMA cross",
        "Cross date",
        "Close",
        f"EMA{ENTRY_EMA_LENGTH}",
        "Vol today",
        f"Avg vol {ENTRY_VOLUME_LOOKBACK}d",
        "Vol×",
        "Ext%",
        "Reason / unmet rule",
    )
    rows: list[tuple[str, ...]] = []
    for candidate in candidates:
        cross_label = candidate.crossover_type
        if candidate.fresh_positive_cross:
            cross_label += " (fresh)"
        rows.append(
            (
                candidate.classification,
                candidate.symbol,
                candidate.latest_date.isoformat() if candidate.latest_date is not None else "n/a",
                f"{candidate.residual_score:.2f}",
                candidate.weekly_supertrend,
                candidate.daily_supertrend,
                cross_label,
                candidate.crossover_date.isoformat() if candidate.crossover_date is not None else "n/a",
                _format_money_inr(candidate.close),
                _format_money_inr(candidate.ema20),
                f"{candidate.volume:,.0f}" if np.isfinite(candidate.volume) else "n/a",
                f"{candidate.average_volume:,.0f}" if np.isfinite(candidate.average_volume) else "n/a",
                f"{candidate.volume_multiple:.2f}×" if np.isfinite(candidate.volume_multiple) else "n/a",
                f"{candidate.ema_extension_pct:+.2f}%" if np.isfinite(candidate.ema_extension_pct) else "n/a",
                "; ".join(candidate.reasons),
            )
        )
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    print(
        "\nBUY/WATCH/AVOID-WAIT are screening labels only, not personalized advice or orders. "
        "The table uses the same daily candle history as the main screen.\n"
    )


def print_crossover_audit(
    symbol: str,
    daily: pd.DataFrame,
    jma_length: int = DEFAULT_JMA_LENGTH,
    jma_phase: float = DEFAULT_JMA_PHASE,
    jma_power: float = DEFAULT_JMA_POWER,
    dwma_length: int = DEFAULT_DWMA_LENGTH,
    warmup_bars: int = DEFAULT_CROSSOVER_WARMUP_BARS,
) -> None:
    """Print the supplied algorithm's crossover-day output for one symbol.

    Every scanned session (bar warmup+1 onward) is evaluated; only the days that
    actually crossed are printed, with the JMA, the DWMA, both previous-day
    values, the JMA − DWMA difference and Positive/Negative. This is a debugging
    aid for a single name — it changes nothing in the screen and places no orders.
    """
    close = daily_close_series(daily)
    params = jma_parameters(jma_length, jma_phase, jma_power)
    print(f"\nJMA/DWMA CROSSOVER AUDIT — {symbol} (INFORMATIONAL ONLY; NO ORDERS)\n")
    print(f"Daily closes available        : {len(close)}")
    print(
        f"JMA constants                 : length={params.length} phase={params.phase:g} power={params.power:g} "
        f"beta={params.beta:.10f} len1={params.len1:.6f} pow1={params.pow1:.6f} div={params.div:g}"
    )
    print(
        f"                                phase_ratio={params.phase_ratio:g} cap={params.cap:.4f} "
        f"avolty_factor={params.avolty_factor:.9f}"
    )
    print(
        f"DWMA constants                : length={dwma_length}; weights 1..{dwma_length} (oldest bar = 1); "
        f"divisor {dwma_length * (dwma_length + 1) // 2}"
    )
    print(f"Warm-up                       : first {warmup_bars} bar(s) skipped; scanning starts on bar {warmup_bars + 1}")
    if len(close) <= warmup_bars:
        print(
            f"\nNothing to audit: {len(close)} daily close(s) does not exceed the {warmup_bars}-bar warm-up."
        )
        return
    try:
        scanned = jma_dwma_crossover_frame(
            close,
            jma_length=jma_length,
            jma_phase=jma_phase,
            jma_power=jma_power,
            dwma_length=dwma_length,
            warmup_bars=warmup_bars,
        )
    except (ValueError, KeyError, TypeError) as exc:
        print(f"\nCrossover calculation failed: {type(exc).__name__}: {exc}")
        return

    crosses = scanned[scanned["crossover"] != 0]
    print(f"Scanned bars                  : {len(scanned)}; crossover days found: {len(crosses)}")
    state, reason = crossover_state(
        daily,
        jma_length=jma_length,
        jma_phase=jma_phase,
        jma_power=jma_power,
        dwma_length=dwma_length,
        warmup_bars=warmup_bars,
    )
    if state is not None:
        print(
            f"Latest reading                : JMA {state.jma_value:,.4f} vs DWMA {state.dwma_value:,.4f} "
            f"({state.difference_label()}) → {state.direction_label}; last crossover {state.since_label()}"
        )
    else:
        print(f"Latest reading                : unavailable ({reason})")
    if crosses.empty:
        print("\nNo crossover day inside the scanned window; the JMA stayed on one side of the DWMA.")
        return

    headers = ("Date", "JMA", "DWMA", "JMA prev", "DWMA prev", "JMA-DWMA", "Crossover")
    rows = [
        (
            timestamp.date().isoformat(),
            f"{row['jma']:,.4f}",
            f"{row['dwma']:,.4f}",
            f"{row['jma_prev']:,.4f}",
            f"{row['dwma_prev']:,.4f}",
            f"{row['difference']:+,.4f}",
            CROSSOVER_LABELS.get(int(row["crossover"]), "None"),
        )
        for timestamp, row in crosses.iterrows()
    ]
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    # Dates/labels read left-aligned; the numeric columns read right-aligned.
    alignments = ("<", ">", ">", ">", ">", ">", "<")

    def formatted(values: Sequence[str]) -> str:
        return "  ".join(
            value.ljust(widths[i]) if alignments[i] == "<" else value.rjust(widths[i])
            for i, value in enumerate(values)
        )

    print()
    print(formatted(headers))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print(formatted(row))
    print(
        f"\nCrossover days are listed oldest first; {len(rows)} of {len(scanned)} scanned session(s) crossed. "
        "Positive = JMA crossed above DWMA; Negative = JMA crossed below DWMA."
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rank the supplied NSE universe by India-factor residual momentum and report daily JMA/DWMA "
            "crossovers plus weekly/daily Supertrend; no trading."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="credential/config file (default: stock-data/env.txt; "
             "falls back read-only to project env.txt for the access token — never moves/overwrites it)",
    )
    parser.add_argument(
        "--factor-file",
        type=Path,
        default=None,
        help="optional local monthly India FF factor CSV; otherwise fetch and store the selected provider's latest file",
    )
    parser.add_argument(
        "--factor-provider",
        choices=("scdlds", "iima"),
        default=None,
        help="web factor source when --factor-file is omitted (default scdlds; provider is pinned, not spliced)",
    )
    parser.add_argument(
        "--factor-units",
        choices=("percent", "decimal"),
        default=None,
        help="units in --factor-file (default percent; use decimal for SCDLDS/normalized CSVs)",
    )
    parser.add_argument(
        "--allow-stale-factors",
        action="store_true",
        help="allow an old factor file and label the screen as-of that historical factor month",
    )
    parser.add_argument("--min-turnover-inr", type=float, default=None,
                        help="minimum 63-session median daily close×volume, INR/day (default ₹50 crore)")
    parser.add_argument("--max-residual-vol-pct", type=float, default=None,
                        help="exclude monthly residual volatility above this percent (default 15)")
    parser.add_argument("--portfolio-size", type=int, default=None,
                        help="maximum long-side names shown in the table (default 50)")
    parser.add_argument("--holdings-file", type=Path, default=None,
                        help="optional current-holdings CSV with a symbol/ticker column; applies 8%% entry / 15%% exit buffers")
    parser.add_argument("--min-eligible", type=int, default=None,
                        help="minimum screened cross-section required before printing (default 30)")
    parser.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS,
                        help="parallel candle workers, capped at 8 (default 6)")
    parser.add_argument("--supertrend-period", type=int, default=None,
                        help="Supertrend ATR period, applied to both the weekly and the daily bars (default 10)")
    parser.add_argument("--supertrend-multiplier", type=float, default=None,
                        help="Supertrend ATR multiplier, applied to both the weekly and the daily bars (default 3)")
    parser.add_argument("--jma-length", type=int, default=None,
                        help="daily JMA (fast line) length, at least 2 (default 7)")
    parser.add_argument("--jma-phase", type=float, default=None,
                        help="daily JMA phase, -100..100 (default -40)")
    parser.add_argument("--jma-power", type=float, default=None,
                        help="daily JMA power (default 0.35; kept for parity with the supplied parameters — the "
                             "adaptive exponent is derived from the length, so power does not change the series)")
    parser.add_argument("--dwma-length", type=int, default=None,
                        help="daily DWMA (slow line) length, weights 1..length (default 20)")
    parser.add_argument("--crossover-warmup-bars", type=int, default=None,
                        help="daily bars skipped as JMA warm-up before crossover detection (default 100, so "
                             "detection starts on bar 101)")
    parser.add_argument("--crossover-sort", choices=CROSSOVER_SORT_OPTIONS, default=None,
                        help="order of the displayed rows, keyed on the daily JMA/DWMA crossover day: "
                             "desc = most recent cross first (default), asc = oldest cross first, "
                             "score = residual-momentum rank")
    parser.add_argument("--supertrend-sort", choices=CROSSOVER_SORT_OPTIONS, default=None,
                        help="deprecated alias for --crossover-sort: the table order is the crossover day now, "
                             "the Supertrend flip is reported but no longer sorts")
    parser.add_argument("--crossover-audit", metavar="SYMBOL", default=None,
                        help="also print the day-by-day JMA/DWMA crossover output (crossover days only) for one "
                             "symbol from the history already loaded")
    parser.add_argument("--history-extra-months", type=int, default=None,
                        help="extra months of daily history to load before the regression window "
                             "(default 0; only needed to date older weekly Supertrend flips or JMA/DWMA crosses)")
    parser.add_argument("--refresh", action="store_true", help="ignore cached candles and fetch fresh history")
    return parser


def _optional_float(value: Optional[str], default: float, name: str) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except ValueError:
        raise ScreenerError(f"{name} must be numeric.") from None
    if not np.isfinite(number):
        raise ScreenerError(f"{name} must be finite.")
    return number


def _optional_int(value: Optional[str], default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        raise ScreenerError(f"{name} must be an integer.") from None


def run_screen(args: argparse.Namespace) -> int:
    if UNIVERSE_LOAD_ERROR is not None:
        raise ScreenerError(UNIVERSE_LOAD_ERROR)
    migrate_legacy_helper_data()
    env_values = load_env_values(Path(args.env_file))
    token = env_value(env_values, "UPSTOX_ACCESS_TOKEN", "") or ""
    if not token.strip():
        raise ScreenerError(
            f"UPSTOX_ACCESS_TOKEN is missing. Add it to {args.env_file} or "
            f"{LEGACY_ENV_FILE} (read-only fallback) or export it in the shell. "
            "The API key/secret are not needed for these authenticated GET requests when a valid access token exists."
        )

    factor_file = args.factor_file
    if factor_file is None:
        configured_factor_file = env_value(env_values, "INDIA_FF_FACTOR_FILE", "")
        factor_file = Path(configured_factor_file).expanduser() if configured_factor_file else None
    factor_units = args.factor_units or env_value(env_values, "INDIA_FF_FACTOR_UNITS", "percent") or "percent"
    if factor_units not in ("percent", "decimal"):
        raise ScreenerError("INDIA_FF_FACTOR_UNITS/--factor-units must be 'percent' or 'decimal'.")
    factor_provider = (args.factor_provider or env_value(env_values, "INDIA_FACTOR_PROVIDER", "scdlds") or "scdlds").lower()
    if factor_provider not in ("scdlds", "iima"):
        raise ScreenerError("INDIA_FACTOR_PROVIDER/--factor-provider must be 'scdlds' or 'iima'.")

    max_staleness = _optional_int(
        env_value(env_values, "MAX_FACTOR_STALENESS_MONTHS"),
        DEFAULT_MAX_FACTOR_STALENESS_MONTHS,
        "MAX_FACTOR_STALENESS_MONTHS",
    )
    min_turnover = args.min_turnover_inr
    if min_turnover is None:
        min_turnover = _optional_float(
            env_value(env_values, "MIN_MEDIAN_DAILY_TURNOVER_INR"),
            DEFAULT_MIN_MEDIAN_DAILY_TURNOVER_INR,
            "MIN_MEDIAN_DAILY_TURNOVER_INR",
        )
    max_residual_vol_pct = args.max_residual_vol_pct
    if max_residual_vol_pct is None:
        max_residual_vol_pct = _optional_float(
            env_value(env_values, "MAX_MONTHLY_RESIDUAL_VOLATILITY_PCT"),
            DEFAULT_MAX_MONTHLY_RESIDUAL_VOLATILITY * 100,
            "MAX_MONTHLY_RESIDUAL_VOLATILITY_PCT",
        )
    max_residual_volatility = max_residual_vol_pct / 100.0
    portfolio_size = args.portfolio_size
    if portfolio_size is None:
        portfolio_size = _optional_int(
            env_value(env_values, "PORTFOLIO_SIZE"),
            DEFAULT_PORTFOLIO_SIZE,
            "PORTFOLIO_SIZE",
        )
    min_eligible = args.min_eligible
    if min_eligible is None:
        min_eligible = _optional_int(
            env_value(env_values, "MIN_ELIGIBLE_UNIVERSE"),
            DEFAULT_MIN_ELIGIBLE_UNIVERSE,
            "MIN_ELIGIBLE_UNIVERSE",
        )
    cache_hours = _optional_float(
        env_value(env_values, "PRICE_CACHE_TTL_HOURS"),
        DEFAULT_CACHE_TTL_HOURS,
        "PRICE_CACHE_TTL_HOURS",
    )
    supertrend_period = args.supertrend_period
    if supertrend_period is None:
        supertrend_period = _optional_int(
            env_value(env_values, "SUPERTREND_PERIOD"),
            DEFAULT_SUPERTREND_PERIOD,
            "SUPERTREND_PERIOD",
        )
    supertrend_multiplier = args.supertrend_multiplier
    if supertrend_multiplier is None:
        supertrend_multiplier = _optional_float(
            env_value(env_values, "SUPERTREND_MULTIPLIER"),
            DEFAULT_SUPERTREND_MULTIPLIER,
            "SUPERTREND_MULTIPLIER",
        )
    jma_length = args.jma_length
    if jma_length is None:
        jma_length = _optional_int(env_value(env_values, "JMA_LENGTH"), DEFAULT_JMA_LENGTH, "JMA_LENGTH")
    jma_phase = args.jma_phase
    if jma_phase is None:
        jma_phase = _optional_float(env_value(env_values, "JMA_PHASE"), DEFAULT_JMA_PHASE, "JMA_PHASE")
    jma_power = args.jma_power
    if jma_power is None:
        jma_power = _optional_float(env_value(env_values, "JMA_POWER"), DEFAULT_JMA_POWER, "JMA_POWER")
    dwma_length = args.dwma_length
    if dwma_length is None:
        dwma_length = _optional_int(env_value(env_values, "DWMA_LENGTH"), DEFAULT_DWMA_LENGTH, "DWMA_LENGTH")
    crossover_warmup_bars = args.crossover_warmup_bars
    if crossover_warmup_bars is None:
        crossover_warmup_bars = _optional_int(
            env_value(env_values, "CROSSOVER_WARMUP_BARS"),
            DEFAULT_CROSSOVER_WARMUP_BARS,
            "CROSSOVER_WARMUP_BARS",
        )
    if (
        max_staleness < 0 or min_turnover < 0 or min_eligible < 1 or cache_hours < 0
        or portfolio_size < 1 or not 0 < max_residual_vol_pct <= 100
    ):
        raise ScreenerError(
            "Staleness, turnover, cache, and eligible-count settings must be non-negative; "
            "portfolio size must be positive and residual-volatility cap must be in (0, 100] percent."
        )
    # Table order: the crossover day is authoritative; the Supertrend knob is a
    # deprecated alias so older command lines and env files keep working.
    crossover_sort = (args.crossover_sort or "").strip().lower()
    legacy_sort_requested = False
    if not crossover_sort and args.supertrend_sort:
        crossover_sort = args.supertrend_sort.strip().lower()
        legacy_sort_requested = True
    if not crossover_sort:
        configured_sort = env_value(env_values, "CROSSOVER_SORT", "") or ""
        if configured_sort.strip():
            crossover_sort = configured_sort.strip().lower()
        else:
            legacy_sort = env_value(env_values, LEGACY_SUPERTREND_SORT_ENV, "") or ""
            if legacy_sort.strip():
                crossover_sort = legacy_sort.strip().lower()
                legacy_sort_requested = True
    crossover_sort = crossover_sort or DEFAULT_CROSSOVER_SORT
    if crossover_sort not in CROSSOVER_SORT_OPTIONS:
        raise ScreenerError(
            "CROSSOVER_SORT/--crossover-sort (and its legacy SUPERTREND_SORT/--supertrend-sort alias) "
            "must be 'asc', 'desc', or 'score'."
        )
    if legacy_sort_requested:
        print(
            "NOTICE: --supertrend-sort/SUPERTREND_SORT is a deprecated alias; the table order is now the daily "
            "JMA/DWMA crossover day (use --crossover-sort/CROSSOVER_SORT). The Supertrend flip is still reported.",
            file=sys.stderr,
        )
    history_extra_months = args.history_extra_months
    if history_extra_months is None:
        history_extra_months = _optional_int(
            env_value(env_values, "PRICE_HISTORY_EXTRA_MONTHS"),
            DEFAULT_PRICE_HISTORY_EXTRA_MONTHS,
            "PRICE_HISTORY_EXTRA_MONTHS",
        )
    if supertrend_period < 2 or not np.isfinite(supertrend_multiplier) or supertrend_multiplier <= 0:
        raise ScreenerError(
            "Supertrend needs an ATR period of at least 2 and a positive multiplier (both timeframes)."
        )
    if not -JMA_PHASE_LIMIT <= jma_phase <= JMA_PHASE_LIMIT:
        raise ScreenerError(
            f"JMA_PHASE/--jma-phase must be between -{JMA_PHASE_LIMIT:g} and {JMA_PHASE_LIMIT:g}."
        )
    try:
        jma_constants = jma_parameters(jma_length, jma_phase, jma_power)
        if dwma_length < 2:
            raise ValueError("DWMA length must be at least 2")
        if crossover_warmup_bars < dwma_length:
            raise ValueError(
                f"the warm-up ({crossover_warmup_bars} bars) must cover the DWMA length ({dwma_length} bars)"
            )
    except ValueError as exc:
        raise ScreenerError(f"Daily JMA/DWMA crossover settings are invalid: {exc}.") from None
    if history_extra_months < 0 or history_extra_months > 120:
        raise ScreenerError(
            "PRICE_HISTORY_EXTRA_MONTHS/--history-extra-months must be between 0 and 120."
        )
    current_holdings = read_current_holdings(args.holdings_file)
    if args.holdings_file is not None:
        console_status(
            f"[Portfolio] Read {len(current_holdings or set())} current symbol(s) from {args.holdings_file}; "
            "buffers are informational only."
        )

    console_status("[1/4] Loading the latest India-specific monthly factor series...")
    factors, factor_source = load_factor_data(
        factor_file=factor_file,
        units=factor_units,
        provider=factor_provider,
        cache_dir=DEFAULT_FACTOR_CACHE_DIR,
    )
    if factor_file is None and factor_provider == "scdlds":
        print(
            "NOTICE: SCDLDS labels its beta factor library free for academic use; verify permission for other uses.",
            file=sys.stderr,
        )
    today = current_india_date()
    factors, factor_month, factor_lag = validate_factor_freshness(
        factors,
        today=today,
        max_staleness_months=max_staleness,
        allow_stale=args.allow_stale_factors,
    )
    as_of_month = _asof_month_for_screen(factors, today)
    if months_between(factors.index.min(), as_of_month) + 1 < REGRESSION_MONTHS:
        raise DataQualityError(
            f"The factor file has fewer than {REGRESSION_MONTHS} usable monthly rows through {as_of_month}."
        )

    console_status(
        f"[1/4] Factor data ready through {factor_month}; lag={factor_lag} month(s). Source: {factor_source}"
    )
    if factor_lag > max_staleness:
        print(
            f"WARNING: allowing stale factors by request; this screen is historical through {as_of_month}.",
            file=sys.stderr,
        )

    console_status("[2/4] Downloading Upstox NSE instrument master and resolving supplied symbols...")
    client = UpstoxClient(
        access_token=token.strip(),
        cache_dir=DEFAULT_CACHE_DIR,
        cache_ttl_seconds=cache_hours * 3600,
        refresh=args.refresh,
    )
    instruments = client.get_nse_equity_instruments()
    if not instruments:
        raise ScreenerError("None of the requested tickers mapped uniquely to active NSE_EQ instruments.")
    mapped_symbols = {instrument.symbol for instrument in instruments}
    console_status(f"[2/4] Resolved {len(instruments)} of {len(set(UNIVERSE))} symbols; preparing daily history.")
    unmapped = [symbol for symbol in dict.fromkeys(UNIVERSE) if symbol not in mapped_symbols]
    if unmapped:
        sample = ", ".join(unmapped[:20])
        suffix = f" (+{len(unmapped) - 20} more)" if len(unmapped) > 20 else ""
        print(f"WARNING: {len(unmapped)} universe symbols not found in active NSE EQ master: {sample}{suffix}", file=sys.stderr)

    # Keep one extra price month before the 36 regression returns. The range is
    # anchored to the usable signal month (not today's month) so an explicitly
    # stale/historical screen still has enough price history.
    # NOTE: this same daily history feeds every indicator below — it is resampled
    # to weekly bars for the weekly Supertrend, used as-is for the daily
    # Supertrend, and used as-is for the JMA/DWMA crossover — so no separate
    # weekly or indicator request is made. The default window is roughly 800
    # sessions, well past the crossover's 100-bar JMA warm-up;
    # --history-extra-months extends it further back purely to date older weekly
    # Supertrend flips or older crossovers.
    first_needed_month = as_of_month - (REGRESSION_MONTHS + 1)
    start_date = (
        first_needed_month.start_time - pd.DateOffset(months=2 + history_extra_months)
    ).date()
    end_date = today
    console_status(
        f"[3/4] Fetching daily OHLCV from {start_date} through {end_date}; progress updates follow "
        f"(this also feeds the weekly/daily Supertrend and the JMA/DWMA crossover)."
    )
    prices, price_failures, cache_hits = _fetch_prices(
        instruments=instruments,
        client=client,
        start_date=start_date,
        end_date=end_date,
        today=today,
        workers=args.workers,
    )

    console_status(f"[4/4] Computing 36-month regressions and residual scores for {len(prices)} histories...")
    ranked_candidates, filter_reasons = _make_ranked_universe(
        instruments=instruments,
        prices=prices,
        factors=factors,
        as_of_month=as_of_month,
        min_turnover_inr=min_turnover,
        today=today,
        max_residual_volatility=max_residual_volatility,
    )
    console_status(
        f"[4/4] Scoring complete: {len(ranked_candidates)} eligible names; "
        f"{len(filter_reasons)} exclusion reason(s)."
    )
    if len(ranked_candidates) < min_eligible:
        reason_text = ", ".join(
            f"{count} {reason}" for reason, count in sorted(filter_reasons.items())
        ) or "no additional exclusions"
        api_errors = ""
        if price_failures:
            examples = list(price_failures.items())[:8]
            api_errors = " Price-data failures: " + ", ".join(
                f"{symbol} ({reason})" for symbol, reason in examples
            )
            if len(price_failures) > len(examples):
                api_errors += f"; and {len(price_failures) - len(examples)} more."
        raise DataQualityError(
            f"Only {len(ranked_candidates)} stocks passed screening, below the minimum "
            f"cross-section ({min_eligible}). No portfolio screen is printed. Exclusions: {reason_text}."
            f"{api_errors}"
        )

    portfolio_members, buffer_summary = construct_long_portfolio(
        ranked_candidates,
        portfolio_size=portfolio_size,
        current_holdings=current_holdings,
    )

    # Supertrend (both timeframes) and the JMA/DWMA crossover are derived from
    # the daily bars already in memory, so they cost no extra API calls even
    # though they are computed for every priced symbol.
    console_status(
        f"[4/4] Computing Supertrend (ATR {supertrend_period} × {supertrend_multiplier:g}) on weekly and daily "
        f"bars for {len(prices)} histories..."
    )
    supertrend_states, supertrend_failures = compute_supertrend_states(
        prices,
        period=supertrend_period,
        multiplier=supertrend_multiplier,
    )
    console_status(
        f"[4/4] Supertrend ready for {len(supertrend_states)} symbol(s)"
        + (f"; {len(supertrend_failures)} unavailable" if supertrend_failures else "")
        + "."
    )
    console_status(
        f"[4/4] Computing daily JMA({jma_length}, phase {jma_phase:g}, power {jma_power:g}) × DWMA({dwma_length}) "
        f"crossovers for {len(prices)} histories "
        f"(beta={jma_constants.beta:.6f}, pow1={jma_constants.pow1:.6f}, cap={jma_constants.cap:.4f}, "
        f"warm-up {crossover_warmup_bars} bars)..."
    )
    crossover_states, crossover_failures = compute_daily_crossovers(
        prices,
        jma_length=jma_length,
        jma_phase=jma_phase,
        jma_power=jma_power,
        dwma_length=dwma_length,
        warmup_bars=crossover_warmup_bars,
    )
    console_status(
        f"[4/4] Crossovers ready for {len(crossover_states)} symbol(s)"
        + (f"; {len(crossover_failures)} unavailable" if crossover_failures else "")
        + "."
    )
    missing_in_table = [
        member.stock.symbol
        for member in portfolio_members
        if member.stock.symbol not in crossover_states or member.stock.symbol not in supertrend_states
    ]
    if missing_in_table:
        print(
            "WARNING: no JMA/DWMA crossover or no Supertrend for table entries: "
            + ", ".join(missing_in_table[:20]),
            file=sys.stderr,
        )

    if args.crossover_audit:
        audit_symbol = args.crossover_audit.strip().upper()
        audit_daily = prices.get(audit_symbol)
        if audit_daily is None or audit_daily.empty:
            print(
                f"WARNING: --crossover-audit {audit_symbol}: no daily price history was loaded for that symbol.",
                file=sys.stderr,
            )
        else:
            print_crossover_audit(
                audit_symbol,
                audit_daily,
                jma_length=jma_length,
                jma_phase=jma_phase,
                jma_power=jma_power,
                dwma_length=dwma_length,
                warmup_bars=crossover_warmup_bars,
            )

    portfolio_beta, gross_scale, beta_message = summarize_portfolio_beta(portfolio_members)
    console_status(
        f"[4/4] Portfolio screen ready: {len(portfolio_members)} name(s); "
        f"inverse-vol weights; beta monitor={portfolio_beta:.3f} "
        if np.isfinite(portfolio_beta)
        else f"[4/4] Portfolio screen ready: {len(portfolio_members)} name(s); beta unavailable."
    )
    print_screen(
        members=portfolio_members,
        eligible_count=len(ranked_candidates),
        mapped_count=len(instruments),
        requested_count=len(set(UNIVERSE)),
        as_of_month=as_of_month,
        factor_month=factor_month,
        factor_lag_months=factor_lag,
        factor_source=factor_source,
        min_turnover_inr=min_turnover,
        max_residual_volatility=max_residual_volatility,
        portfolio_size=portfolio_size,
        buffer_summary=buffer_summary,
        portfolio_beta=portfolio_beta,
        gross_scale=gross_scale,
        beta_message=beta_message,
        holdings_file=args.holdings_file,
        price_failures=price_failures,
        filter_reasons=filter_reasons,
        cache_hits=cache_hits,
        crossover_states=crossover_states,
        crossover_failures=crossover_failures,
        jma_length=jma_length,
        jma_phase=jma_phase,
        jma_power=jma_power,
        dwma_length=dwma_length,
        crossover_warmup_bars=crossover_warmup_bars,
        crossover_sort=crossover_sort,
        supertrend_states=supertrend_states,
        supertrend_failures=supertrend_failures,
        supertrend_period=supertrend_period,
        supertrend_multiplier=supertrend_multiplier,
    )

    # The separate entry review is deliberately appended after the main screen
    # and covers every eligible score-ranked name, not just the displayed basket.
    entry_candidates, below_score_threshold = screen_entry_candidates(
        stocks=ranked_candidates,
        prices=prices,
        supertrend_states=supertrend_states,
        crossover_states=crossover_states,
    )
    print_entry_candidate_screen(
        candidates=entry_candidates,
        screened_count=len(ranked_candidates),
        below_score_threshold=below_score_threshold,
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        return run_screen(args)
    except ScreenerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; no orders were sent.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
