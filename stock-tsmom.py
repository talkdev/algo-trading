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
  * Weekly Supertrend (ATR period 10, multiplier 3) is computed for every stock
    with price history, and the top-50 table reports whether it is currently
    POSITIVE or NEGATIVE plus the week it last flipped. Weekly bars are
    resampled from the daily OHLCV already downloaded (no extra API calls),
    using Wilder ATR and the standard +/-1 flip rule.
  * The selected names are displayed sorted by that "ST since" date, oldest
    trend first (--supertrend-sort asc, the default); desc shows the most recent
    flips first and score restores the raw momentum ranking.
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
from typing import Any, Iterable, Mapping, Optional, Sequence
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

# Weekly Supertrend settings (the requested 10 & 3).
DEFAULT_SUPERTREND_PERIOD = 10
DEFAULT_SUPERTREND_MULTIPLIER = 3.0
# Table order for the selected names: "desc" = most recent Supertrend flip first,
# "asc"  = oldest trend first, "score"       = keep the residual-momentum order.
DEFAULT_SUPERTREND_SORT = "desc"
SUPERTREND_SORT_OPTIONS = ("asc", "desc", "score")
WEEKLY_RESAMPLE_RULE = "W-FRI"  # NSE trading week, week ending Friday.
SUPERTREND_SORT_LABELS = {
    "asc": "by weekly Supertrend change date, ascending (oldest trend first, most recent flip last)",
    "desc": "by weekly Supertrend change date, descending (most recent flip first)",
    "score": "by residual-momentum score (Supertrend shown for reference only)",
}


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
    """Weekly Supertrend reading for one stock.

    ``direction`` is +1 when the Supertrend line sits under price (positive /
    bullish / green) and -1 when it sits above price (negative / bearish / red).
    ``change_date`` is the week of the most recent flip; when the trend is older
    than the loaded history it is None and ``first_resolved_date`` carries the
    earliest week the indicator could be resolved.
    """

    direction: int
    value: float
    change_date: Optional[date]
    weeks_since_change: Optional[int]
    flip_in_window: bool
    first_resolved_date: Optional[date]
    weekly_bars: int

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
        weeks = "" if self.weeks_since_change is None else f" ({self.weeks_since_change}w)"
        return f"{self.change_date.isoformat()}{weeks}"


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
    columns = ["open", "high", "low", "close", "volume"]
    empty = pd.DataFrame(columns=columns)
    if daily is None or daily.empty or not {"open", "high", "low", "close"}.issubset(daily.columns):
        return empty
    frame = daily.copy().sort_index()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame[columns].apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame[(frame[["open", "high", "low", "close"]] > 0).all(axis=1)]
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
    """Classic Supertrend on the supplied OHLC bars (weekly bars for this screen).

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


def supertrend_state(
    daily: pd.DataFrame,
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> tuple[Optional[SupertrendState], Optional[str]]:
    """Return (state, failure reason) for the weekly Supertrend of one stock.

    Weekly bars are resampled from the daily history already downloaded, so this
    costs no additional market-data requests. ``failure reason`` is None on
    success; otherwise it explains why no reading could be produced.
    """
    weekly = resample_daily_to_weekly(daily)
    minimum_bars = period + 2  # ATR seed plus one bar to establish direction.
    if len(weekly) < minimum_bars:
        return None, f"fewer than {minimum_bars} weekly bars"
    try:
        result = calculate_supertrend(weekly, period=period, multiplier=multiplier)
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"supertrend calculation failed ({type(exc).__name__})"
    resolved = result.loc[result["trend"] != 0]
    if resolved.empty:
        return None, "weekly ATR never seeded"

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
        weeks_since: Optional[int] = last_index - flip_index
        flip_in_window = True
    else:
        change_date = None
        weeks_since = None
        flip_in_window = False

    return (
        SupertrendState(
            direction=direction,
            value=value,
            change_date=change_date,
            weeks_since_change=weeks_since,
            flip_in_window=flip_in_window,
            first_resolved_date=bar_dates[0],
            weekly_bars=int(len(weekly)),
        ),
        None,
    )


def compute_weekly_supertrend(
    prices: Mapping[str, pd.DataFrame],
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> tuple[dict[str, SupertrendState], dict[str, str]]:
    """Compute weekly Supertrend for every symbol with price history, in memory."""
    states: dict[str, SupertrendState] = {}
    failures: dict[str, str] = {}
    for symbol in sorted(prices):
        daily = prices.get(symbol)
        if daily is None or daily.empty:
            failures[symbol] = "no price history"
            continue
        state, reason = supertrend_state(daily, period=period, multiplier=multiplier)
        if state is None:
            failures[symbol] = reason or "unavailable"
        else:
            states[symbol] = state
    return states, failures


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


def _has_supertrend_reading(state: Optional[SupertrendState]) -> bool:
    return state is not None and state.direction != 0


def _supertrend_sort_key(state: Optional[SupertrendState]) -> tuple[date, int]:
    """Sort key for the ST-since column, for names that do have a reading.

    A stock whose trend never flipped inside the loaded history is the *oldest*
    trend in the table, so it sorts before every dated flip (secondary key -1)
    and therefore after every dated flip when the order is reversed.
    """
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
    """Return the selected names ordered for display; ties keep the score order.

    Sorting is stable, so two stocks that flipped in the same week stay in
    residual-momentum rank order. The momentum rank itself is untouched and is
    still printed in the Rank/%ile column. Names with no Supertrend reading are
    held to the end in both directions, so they never displace a readable row.
    """
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


def summarize_supertrend_counts(
    members: Sequence[PortfolioMember],
    supertrend_states: Mapping[str, SupertrendState],
) -> tuple[int, int, int]:
    """Count positive / negative / unavailable Supertrend readings in the table."""
    positive = negative = unavailable = 0
    for member in members:
        state = supertrend_states.get(member.stock.symbol)
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
    supertrend_states: Optional[Mapping[str, SupertrendState]] = None,
    supertrend_failures: Optional[Mapping[str, str]] = None,
    supertrend_period: int = DEFAULT_SUPERTREND_PERIOD,
    supertrend_multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
    supertrend_sort: str = DEFAULT_SUPERTREND_SORT,
) -> None:
    supertrend_states = supertrend_states or {}
    supertrend_failures = supertrend_failures or {}
    ordered_members = sort_members_by_supertrend(members, supertrend_states, supertrend_sort)
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
    print(f"Table order                   : {SUPERTREND_SORT_LABELS.get(supertrend_sort, supertrend_sort)}")
    print(
        f"Weekly Supertrend             : ATR({supertrend_period}) × {supertrend_multiplier:g} on weekly bars "
        f"(W-FRI) resampled from the daily history already downloaded"
    )
    print(
        "Supertrend legend             : POS = line below price (bullish); NEG = line above price (bearish); "
        "'ST since' = week the trend last flipped"
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
            f"ST ({supertrend_period},{supertrend_multiplier:g})",
            "ST since",
            "ST level",
        )
        rows: list[tuple[str, ...]] = []
        for position, member in enumerate(ordered_members, start=1):
            stock = member.stock
            state = supertrend_states.get(stock.symbol)
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
                    state.direction_label if state is not None else "n/a",
                    state.since_label() if state is not None else "n/a",
                    _format_money_inr(state.value) if state is not None else "n/a",
                )
            )
        widths = [
            max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))
        ]
        print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
        print("  ".join("-" * widths[i] for i in range(len(headers))))
        for row in rows:
            print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))

        if supertrend_states:
            positive, negative, unavailable = summarize_supertrend_counts(members, supertrend_states)
            print(
                f"\nWeekly Supertrend in this basket: {positive} POS / {negative} NEG"
                + (f" / {unavailable} unavailable" if unavailable else "")
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


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rank the supplied NSE universe by India-factor residual momentum; no trading."
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
                        help="weekly Supertrend ATR period (default 10)")
    parser.add_argument("--supertrend-multiplier", type=float, default=None,
                        help="weekly Supertrend ATR multiplier (default 3)")
    parser.add_argument("--supertrend-sort", choices=SUPERTREND_SORT_OPTIONS, default=None,
                        help="order of the displayed rows: asc = oldest 'ST since' first (default), "
                             "desc = most recent flip first, score = residual-momentum rank")
    parser.add_argument("--history-extra-months", type=int, default=None,
                        help="extra months of daily history to load before the regression window "
                             "(default 0; only needed to date older weekly Supertrend flips)")
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
    if (
        max_staleness < 0 or min_turnover < 0 or min_eligible < 1 or cache_hours < 0
        or portfolio_size < 1 or not 0 < max_residual_vol_pct <= 100
    ):
        raise ScreenerError(
            "Staleness, turnover, cache, and eligible-count settings must be non-negative; "
            "portfolio size must be positive and residual-volatility cap must be in (0, 100] percent."
        )
    supertrend_sort = (
        args.supertrend_sort
        or env_value(env_values, "SUPERTREND_SORT", DEFAULT_SUPERTREND_SORT)
        or DEFAULT_SUPERTREND_SORT
    ).strip().lower()
    if supertrend_sort not in SUPERTREND_SORT_OPTIONS:
        raise ScreenerError(
            "SUPERTREND_SORT/--supertrend-sort must be 'asc', 'desc', or 'score'."
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
            "Weekly Supertrend needs an ATR period of at least 2 and a positive multiplier."
        )
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
    # NOTE: this same daily history is resampled to weekly bars for Supertrend,
    # so no separate weekly request is made. --history-extra-months extends the
    # range further back purely to date older weekly Supertrend flips.
    first_needed_month = as_of_month - (REGRESSION_MONTHS + 1)
    start_date = (
        first_needed_month.start_time - pd.DateOffset(months=2 + history_extra_months)
    ).date()
    end_date = today
    console_status(
        f"[3/4] Fetching daily OHLCV from {start_date} through {end_date}; progress updates follow "
        f"(this also feeds the weekly Supertrend)."
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

    # Supertrend is derived from the daily bars already in memory, so it costs
    # no extra API calls even though it is computed for every priced symbol.
    console_status(
        f"[4/4] Computing weekly Supertrend (ATR {supertrend_period} × {supertrend_multiplier:g}) "
        f"for {len(prices)} histories..."
    )
    supertrend_states, supertrend_failures = compute_weekly_supertrend(
        prices,
        period=supertrend_period,
        multiplier=supertrend_multiplier,
    )
    missing_in_table = [
        member.stock.symbol for member in portfolio_members if member.stock.symbol not in supertrend_states
    ]
    console_status(
        f"[4/4] Supertrend ready for {len(supertrend_states)} symbol(s)"
        + (f"; {len(supertrend_failures)} unavailable" if supertrend_failures else "")
        + "."
    )
    if missing_in_table:
        print(
            "WARNING: no weekly Supertrend for table entries: " + ", ".join(missing_in_table[:20]),
            file=sys.stderr,
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
        supertrend_states=supertrend_states,
        supertrend_failures=supertrend_failures,
        supertrend_period=supertrend_period,
        supertrend_multiplier=supertrend_multiplier,
        supertrend_sort=supertrend_sort,
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
