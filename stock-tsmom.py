#!/usr/bin/env python3
"""NSE residual-momentum screener: read-only market data, local report history.

Uses India FF3 inputs (Mkt-RF, SMB, HML, RF) and 36 complete monthly
observations per stock. Scores the 11 formation months t-12 through t-2,
skipping t-1. Formation residuals omit the fitted intercept by design.
Requires a user-supplied sector map and checks price-history completeness,
last-candle age, liquidity and residual volatility. Allocates model weights
under stock, sector, financial-family, liquidity and market-beta limits;
unused capital remains cash. Optional holdings weights permit a read-only,
turnover-limited paper rebalance. Execution prices and transaction costs are
not estimated. Separately, ranks all eligible stocks by their most recent
20 completed trading-session price return, comparing the same sessions with
Nifty 500 as a displayed (not ranking) benchmark. Shows 5/40-session returns
and 20-session daily volatility as context. Two separate status columns flag
whether the latest close is within 2% of the trailing 52-week daily intraday
high or of the high since the current NSE ISIN/security listing. The latter
is NOT the company's all-time high. The attached current-security master is
cross-checked against the current NSE EQ list; Upstox history is requested in
decade chunks and checked against Nifty 500 trading sessions. Missing or
unverifiable coverage is n/a, never silently No. Both screens print on every
successful run. Neither is a profit guarantee.

No orders are placed and no holdings or portfolio state are changed.
Market-data requests are GETs. Factor/cache artifacts and SQLite report
history are written locally. Each screen's first-ever discovery date uses
the IST calendar date on which a report run starts, independent of candle date.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests


PROJECT_DIR = Path(__file__).resolve().parent
STOCK_DATA_DIR = PROJECT_DIR / "stock-data"
UNIVERSE_FILE = STOCK_DATA_DIR / "universe.json"
DEFAULT_ENV_FILE = STOCK_DATA_DIR / "env.txt"
LEGACY_ENV_FILE = PROJECT_DIR / "env.txt"
DEFAULT_CACHE_DIR = STOCK_DATA_DIR / "cache"
DEFAULT_FACTOR_CACHE_DIR = STOCK_DATA_DIR / "factors"
INDIA_TZ = ZoneInfo("Asia/Kolkata")
UPSTOX_API_BASE = "https://api.upstox.com"
NSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
IIMA_LANDING_PAGE = "https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/"
IIMA_DATA_BASE = "https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/DATA/"
SCDLDS_LANDING_PAGE = "https://quantfin.scdlds.com/"
SCDLDS_FF5_FALLBACK_URL = "https://quantfin.scdlds.com/Data/Factor_Data/ff5.csv"
SCDLDS_DOWNLOAD_PATTERN = re.compile(
    r'''downloadFile\(\s*['"]([^'"]*ff5\.csv)['"]\s*\)''', re.IGNORECASE
)
FACTOR_FILENAME_PATTERN = re.compile(
    r"(\d{4}-\d{2}_FourFactors_and_Market_Returns_Monthly_SurvivorshipBiasAdjusted\.csv)",
    re.IGNORECASE,
)

REGRESSION_MONTHS = 36
FORMATION_RESIDUAL_MONTHS = 11
DEFAULT_MAX_MONTHLY_RESIDUAL_VOLATILITY = 0.15
DEFAULT_PORTFOLIO_SIZE = 50
DEFAULT_MAX_WORKERS = 6
DEFAULT_CACHE_TTL_HOURS = 8.0
DEFAULT_REQUESTS_PER_SECOND = 5.0
DEFAULT_SECTOR_FILE = STOCK_DATA_DIR / "sectors.csv"
DEFAULT_CAPITAL_INR = 1_000_000.0  # illustrative ₹10 lakh
DEFAULT_MIN_TURNOVER_INR = 10_000_000.0  # ₹1 crore median/day; configurable
DEFAULT_MAX_PARTICIPATION_PCT = 2.0  # one day's hypothetical order / median daily traded value
DEFAULT_MAX_NAME_PCT = 5.0
DEFAULT_MAX_SECTOR_PCT = 25.0
DEFAULT_MAX_REBALANCE_TURNOVER_PCT = 25.0
DEFAULT_MAX_PRICE_AGE_DAYS = 5
# Before 18:00 IST, exclude today. At/after 18:00 request today, but do not
# claim it exists unless the provider actually returns a bar. Its finality is
# not independently verified by this tool.
EOD_CUTOFF_IST = clock_time(18, 0)
SHORT_LOOKBACK_SESSIONS = 20
SHORT_CONTEXT_SESSIONS = (5, 40)
SHORT_REQUIRED_CLOSES = 41  # t and t-40, covering all 5/20/40-session windows.
DEFAULT_SHORT_SIZE = 50
UPSTOX_DAILY_HISTORY_START = date(2000, 1, 1)
NSE_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
DEFAULT_SECURITY_MASTER_FILE = STOCK_DATA_DIR / "nse_security_master.csv"
DEFAULT_STATE_DB_FILE = STOCK_DATA_DIR / "report_state.sqlite3"
DEFAULT_HIGH_NEAR_PCT = 2.0
DEFAULT_MONTH_END_GAP_DAYS = 7  # conservative calendar proxy, not an NSE holiday calendar
DEFAULT_MAX_FINANCIAL_FAMILY_PCT = 25.0
FINANCIAL_FAMILY = frozenset({"BANKING", "NBFC", "FINANCIAL SERVICES", "ASSET MANAGEMENT", "INSURANCE"})
TURNOVER_SESSIONS = 63
BETA_LOWER_BOUND = 0.7
BETA_UPPER_BOUND = 1.3


class ScreenerError(RuntimeError):
    """User-facing screening failure."""


class DataQualityError(ScreenerError):
    """Insufficient data to compute a defensible score."""


class UpstoxAuthenticationError(ScreenerError):
    """Upstox rejected the access token."""


class UpstoxRequestError(ScreenerError):
    """A data request failed."""


def status(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_universe_file(path: Path = UNIVERSE_FILE) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScreenerError(f"Cannot read valid stock universe JSON at {path}: {exc}") from None
    if not isinstance(payload, dict) or payload.get("exchange") != "NSE" or payload.get("segment") != "NSE_EQ":
        raise ScreenerError("Universe JSON must declare exchange='NSE' and segment='NSE_EQ'.")
    raw = payload.get("symbols")
    if not isinstance(raw, list) or not raw or any(not isinstance(x, str) or not x.strip() for x in raw):
        raise ScreenerError("Universe JSON 'symbols' must be a non-empty list of non-empty strings.")
    symbols = tuple(x.strip().upper() for x in raw)
    if len(set(symbols)) != len(symbols):
        raise ScreenerError("Universe JSON contains duplicate symbols.")
    if payload.get("count") != len(symbols):
        raise ScreenerError(f"Universe JSON count does not match its {len(symbols)} symbols.")
    return symbols


@dataclass(frozen=True)
class Instrument:
    symbol: str
    instrument_key: str
    isin: str


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


@dataclass(frozen=True)
class RankedStock:
    symbol: str
    result: MomentumScore
    latest_close: float
    latest_session: date
    sector: str = ""
    median_turnover_inr: float = float("nan")


@dataclass(frozen=True)
class ShortMomentumStock:
    symbol: str
    sector: str
    return_5: float
    return_20: float
    return_40: float
    benchmark_20: float
    excess_20: float  # simple-return percentage-point difference, NOT alpha
    daily_vol_20: float  # sample std of 20 daily simple returns, unannualized
    latest_session: date
    median_turnover_inr: float
    high_52w: Optional[float] = None
    isin_high: Optional[float] = None
    near_52w: Optional[bool] = None
    near_isin_high: Optional[bool] = None


@dataclass(frozen=True)
class DiscoveryStamp:
    first_seen: date
    new_this_run: bool



@dataclass(frozen=True)
class PortfolioMember:
    stock: RankedStock
    rank: int
    percentile_rank: float
    inverse_vol_weight: float  # unbounded starting allocation, among selected
    model_weight: float        # after risk/tradability limits; remainder is cash
    staged_weight: float       # after optional holdings-based turnover constraints


class RateLimiter:
    """Thread-safe request pacer shared by worker threads."""

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
    """Read-only instrument and daily-candle client with a local CSV cache."""

    def __init__(self, token: str, cache_dir: Path, ttl_seconds: float, refresh: bool) -> None:
        self.token = token
        self.cache_dir = cache_dir
        self.ttl_seconds = max(0.0, ttl_seconds)
        self.refresh = refresh
        self.rate_limiter = RateLimiter(DEFAULT_REQUESTS_PER_SECOND)
        self._local = threading.local()
        self.instrument_diagnostics: dict[str, str] = {}
        self.nifty500_instrument: Optional[Instrument] = None
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json", "User-Agent": "residual-momentum-screen/1.0"})
            self._local.session = session
        return session

    def _get(self, url: str, *, authenticated: bool, timeout: tuple[int, int] = (10, 30)) -> requests.Response:
        headers = {"Authorization": f"Bearer {self.token}"} if authenticated else {}
        last_status: Optional[int] = None
        for attempt in range(4):
            self.rate_limiter.wait()
            try:
                response = self._session().get(url, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise UpstoxRequestError(f"Network error: {type(exc).__name__}") from None
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code in (401, 403) and authenticated:
                raise UpstoxAuthenticationError("Upstox rejected the access token; renew UPSTOX_ACCESS_TOKEN.")
            if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
                last_status = response.status_code
                if attempt == 3:
                    break
                try:
                    delay = float(response.headers.get("Retry-After") or min(2**attempt, 8))
                except ValueError:
                    delay = min(2**attempt, 8)
                time.sleep(max(0.25, min(delay, 15.0)))
                continue
            if not response.ok:
                raise UpstoxRequestError(f"Request failed (HTTP {response.status_code}).")
            return response
        raise UpstoxRequestError(f"Request failed after retries (HTTP {last_status}).")

    def get_nse_equity_instruments(self, symbols: Sequence[str]) -> list[Instrument]:
        try:
            raw = self._get(NSE_INSTRUMENTS_URL, authenticated=False, timeout=(10, 45)).content
            payload = json.loads((gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise UpstoxRequestError("Could not parse Upstox NSE instrument master.") from exc
        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise UpstoxRequestError("Unexpected NSE instrument master format.")
        requested = set(symbols)
        all_exact: dict[str, list[dict[str, Any]]] = {}
        by_symbol: dict[str, list[Instrument]] = {}
        benchmark_keys: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (row.get("segment") == "NSE_INDEX" and row.get("exchange") == "NSE"
                and str(row.get("instrument_type", "")).upper() == "INDEX"
                and str(row.get("name", "")).strip().casefold() == "nifty 500"):
                key = str(row.get("instrument_key", "")).strip()
                if key.startswith("NSE_INDEX|"):
                    benchmark_keys.add(key)
                continue
            if row.get("segment") != "NSE_EQ" or row.get("exchange") != "NSE":
                continue
            symbol = str(row.get("trading_symbol", "")).strip().upper()
            if symbol not in requested:
                continue
            all_exact.setdefault(symbol, []).append(row)
            # BE (trade-to-trade) is intentionally excluded, not silently mapped.
            if str(row.get("instrument_type", "")).upper() != "EQ":
                continue
            if str(row.get("security_type", "NORMAL")).upper() not in ("NORMAL", ""):
                continue
            key = str(row.get("instrument_key", "")).strip()
            isin = str(row.get("isin", "")).strip()
            if key and isin and key.startswith("NSE_EQ|"):
                by_symbol.setdefault(symbol, []).append(Instrument(symbol, key, isin))
        if len(benchmark_keys) != 1:
            raise DataQualityError(
                f"Could not uniquely resolve NSE_INDEX Nifty 500 in the current Upstox master "
                f"({len(benchmark_keys)} keys). Short-term benchmark is required; no index is substituted."
            )
        self.nifty500_instrument = Instrument("NIFTY500", next(iter(benchmark_keys)), "")
        self.instrument_diagnostics = {}
        for symbol in symbols:
            matches = by_symbol.get(symbol, [])
            if len(matches) == 1:
                continue
            if len(matches) > 1:
                reason = "ambiguous: multiple active ordinary EQ records; skipped"
            elif not all_exact.get(symbol):
                reason = "no exact NSE_EQ ticker in today's BOD master (possibly renamed/delisted; verify manually)"
            else:
                types = sorted({str(r.get("instrument_type", "missing")) for r in all_exact[symbol]})
                securities = sorted({str(r.get("security_type", "missing")) for r in all_exact[symbol]})
                reason = f"exact ticker present but not eligible ordinary EQ (series={types}, security={securities})"
            self.instrument_diagnostics[symbol] = reason
        return [by_symbol[s][0] for s in symbols if len(by_symbol.get(s, [])) == 1]

    def _price_cache_path(self, instrument: Instrument) -> Path:
        digest = hashlib.sha256(instrument.instrument_key.encode("utf-8")).hexdigest()[:12]
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", instrument.symbol)
        return self.cache_dir / "upstox_prices" / f"{safe}_{digest}.csv"

    @staticmethod
    def _read_cache(path: Path) -> pd.DataFrame:
        frame = pd.read_csv(path)
        if "date" not in frame:
            raise ValueError("Cache missing date")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).set_index("date").sort_index()
        for col in ("open", "high", "low", "close", "volume"):
            if col not in frame:
                frame[col] = np.nan
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        return frame[["open", "high", "low", "close", "volume"]]

    @staticmethod
    def _parse_candles(candles: Sequence[Any], start: date, end: date) -> pd.DataFrame:
        records: list[tuple[pd.Timestamp, float, float, float, float, float]] = []
        for candle in candles:
            if not isinstance(candle, (list, tuple)) or len(candle) < 6:
                continue
            try:
                timestamp = pd.to_datetime(candle[0], utc=True, errors="coerce")
                if pd.isna(timestamp):
                    continue
                session = timestamp.tz_convert(INDIA_TZ).tz_localize(None).normalize()
                if not start <= session.date() <= end:
                    continue  # reject bars outside the chosen EOD window
                values = tuple(float(candle[i]) for i in range(1, 6))
                if not np.isfinite(values[3]) or values[3] <= 0:
                    continue
                records.append((session, *values))
            except (TypeError, ValueError, OverflowError):
                continue
        if not records:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(records, columns=["date", "open", "high", "low", "close", "volume"])
        return frame.sort_values("date").drop_duplicates("date", keep="last").set_index("date")

    def get_daily_candles(
        self, instrument: Instrument, start: date, end: date, *,
        lifetime_chunk: Optional[str] = None, immutable: bool = False,
    ) -> tuple[pd.DataFrame, bool]:
        # Separate decade-keyed lifetime cache avoids overwriting the normal
        # rolling three-year price cache. Completed decades never expire.
        if lifetime_chunk is None:
            path = self._price_cache_path(instrument)
        else:
            base = self._price_cache_path(instrument)
            path = self.cache_dir / "lifetime_prices" / f"{base.stem}_{lifetime_chunk}.csv"
        metadata_path = path.with_suffix(path.suffix + ".meta.json")
        if not self.refresh and self.ttl_seconds > 0 and path.exists() and metadata_path.exists():
            try:
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (
                    (immutable or time.time() - path.stat().st_mtime <= self.ttl_seconds)
                    and meta.get("instrument_key") == instrument.instrument_key
                    and meta.get("requested_start", "9999") <= start.isoformat()
                    and meta.get("requested_end", "0000") >= end.isoformat()
                ):
                    cached = self._read_cache(path)
                    cached = cached[(cached.index.date >= start) & (cached.index.date <= end)]
                    if not cached.empty:
                        return cached, True
            except (OSError, ValueError, TypeError, pd.errors.ParserError, pd.errors.EmptyDataError):
                pass  # Unknown legacy coverage: refetch instead of silently trusting it.
        key = quote(instrument.instrument_key, safe="")
        url = f"{UPSTOX_API_BASE}/v3/historical-candle/{key}/days/1/{end.isoformat()}/{start.isoformat()}"
        response = self._get(url, authenticated=True)
        try:
            payload = response.json()
            if payload.get("status") not in (None, "success"):
                raise ValueError("API status not success")
            frame = self._parse_candles(payload["data"]["candles"], start, end)
        except (TypeError, KeyError, AttributeError, ValueError, requests.exceptions.JSONDecodeError):
            raise UpstoxRequestError(f"Could not parse historical candles for {instrument.symbol}.") from None
        if not frame.empty:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            out = frame.copy()
            out.index.name = "date"
            out.to_csv(temporary)
            os.replace(temporary, path)
            atomic_write_text(metadata_path, json.dumps({
                "instrument_key": instrument.instrument_key,
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                "first_session": frame.index.min().date().isoformat(),
                "last_session": frame.index.max().date().isoformat(),
            }, indent=2) + "\n")
        return frame, False


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScreenerError(f"Cannot read env file at {path}: {exc}") from None
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_values(primary: Path) -> dict[str, str]:
    values = read_env_file(primary)
    if (values.get("UPSTOX_ACCESS_TOKEN") or "").strip() or primary.resolve() == LEGACY_ENV_FILE.resolve():
        return values
    legacy = read_env_file(LEGACY_ENV_FILE)
    for key, value in legacy.items():
        if not str(values.get(key) or "").strip():
            values[key] = value
    if (values.get("UPSTOX_ACCESS_TOKEN") or "").strip():
        status(f"[Setup] Using {LEGACY_ENV_FILE} as a read-only credential fallback.")
    return values


def env_value(values: Mapping[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(key, values.get(key, default))


def optional_float(value: Optional[str], default: float, name: str) -> float:
    try:
        number = default if value is None or value == "" else float(value)
    except ValueError:
        raise ScreenerError(f"{name} must be numeric.") from None
    if not np.isfinite(number):
        raise ScreenerError(f"{name} must be finite.")
    return number


def optional_int(value: Optional[str], default: int, name: str) -> int:
    try:
        return default if value is None or value == "" else int(value)
    except ValueError:
        raise ScreenerError(f"{name} must be an integer.") from None


def normalized_column_name(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def parse_month(value: Any) -> pd.Period:
    text = str(value).strip()
    if re.fullmatch(r"\d{6}", text):
        text = f"{text[:4]}-{text[4:]}"
    try:
        return pd.Period(text[:7], freq="M")
    except (ValueError, TypeError):
        timestamp = pd.to_datetime(text, errors="coerce")
        if pd.isna(timestamp):
            raise ValueError(f"Invalid month: {value!r}")
        return timestamp.to_period("M")


def parse_india_factor_csv(text: str, units: str = "percent") -> pd.DataFrame:
    """Normalize Indian factor data to decimal returns without provider mixing."""
    if units not in ("percent", "decimal"):
        raise ValueError("Factor units must be percent or decimal")
    try:
        raw = pd.read_csv(io.StringIO(text), encoding="utf-8-sig")
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError):
        raise DataQualityError("Cannot parse monthly factor CSV.") from None
    if raw.empty or len(raw.columns) < 5:
        raise DataQualityError("Monthly factor CSV lacks rows or required columns.")
    normalized = {normalized_column_name(col): col for col in raw.columns}
    date_col = next((normalized[k] for k in ("date", "month", "period", "yyyymm") if k in normalized), raw.columns[0])
    aliases = {
        "mkt_rf": ("mktrf", "mf", "mkt", "marketpremium", "marketfactor", "marketexcessreturn"),
        "smb": ("smb", "sizeminusbig"),
        "hml": ("hml", "highminuslow"),
        "rf": ("rf", "riskfree", "riskfreerate"),
    }
    columns: dict[str, Any] = {}
    for target, candidates in aliases.items():
        source = next((normalized[c] for c in candidates if c in normalized), None)
        if source is None:
            raise DataQualityError(f"Factor CSV is missing {target} (India FF3 and RF are required).")
        columns[target] = source
    keep: list[int] = []
    months: list[pd.Period] = []
    for i, value in enumerate(raw[date_col]):
        try:
            months.append(parse_month(value))
            keep.append(i)
        except ValueError:
            continue
    if not keep:
        raise DataQualityError("Factor CSV contains no valid monthly dates.")
    subset = raw.iloc[keep].copy()
    subset.index = pd.PeriodIndex(months, freq="M", name="month")
    out = pd.DataFrame(index=subset.index)
    for target, source in columns.items():
        out[target] = pd.to_numeric(subset[source], errors="coerce")
    if units == "percent":
        out /= 100.0
    out = out.replace([np.inf, -np.inf], np.nan)
    out = out[~out.index.duplicated(keep="last")].sort_index().dropna(subset=list(columns))
    if out.empty:
        raise DataQualityError("Factor CSV contains no complete India FF3/RF monthly rows.")
    return out[["mkt_rf", "smb", "hml", "rf"]]


def discover_factor_url(provider: str, session: requests.Session) -> str:
    if provider == "scdlds":
        try:
            response = session.get(SCDLDS_LANDING_PAGE, timeout=(10, 30))
            response.raise_for_status()
            matches = SCDLDS_DOWNLOAD_PATTERN.findall(response.text)
            if matches:
                return urljoin(SCDLDS_LANDING_PAGE, matches[0])
        except requests.RequestException:
            pass
        return SCDLDS_FF5_FALLBACK_URL
    try:
        response = session.get(IIMA_LANDING_PAGE, timeout=(10, 30))
        response.raise_for_status()
    except requests.RequestException:
        raise ScreenerError("Cannot reach IIMA factor page; supply --factor-file.") from None
    names = sorted(set(FACTOR_FILENAME_PATTERN.findall(response.text)))
    if not names:
        raise ScreenerError("Cannot discover an IIMA monthly factor CSV; supply --factor-file.")
    return urljoin(IIMA_DATA_BASE, names[-1])


def validate_scdlds_scale(factors: pd.DataFrame) -> None:
    median = float(factors["rf"].abs().median())
    if not np.isfinite(median) or not 0 < median < 0.05:
        raise DataQualityError("SCDLDS RF scale is unexpected; check that the source still uses decimal returns.")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def factor_paths(provider: str, cache_dir: Path) -> tuple[Path, Path, Path]:
    return (
        cache_dir / f"{provider}_monthly_raw.csv",
        cache_dir / f"{provider}_ff3_monthly.csv",
        cache_dir / f"{provider}_factor_source.json",
    )


def load_factor_data(
    factor_file: Optional[Path], units: str, provider: str, cache_dir: Path
) -> tuple[pd.DataFrame, str]:
    if factor_file is not None:
        try:
            text = factor_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise ScreenerError(f"Cannot read factor file {factor_file}: {exc}") from None
        return parse_india_factor_csv(text, units), str(factor_file)

    source_units = "decimal" if provider == "scdlds" else "percent"
    raw_path, normalized_path, metadata_path = factor_paths(provider, cache_dir)
    status(f"[Factors] Fetching {provider.upper()} monthly factors...")
    try:
        session = requests.Session()
        url = discover_factor_url(provider, session)
        response = session.get(url, timeout=(10, 45), headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        raw_text = response.content.decode("utf-8-sig")
        factors = parse_india_factor_csv(raw_text, source_units)
        if provider == "scdlds":
            validate_scdlds_scale(factors)
        normalized = factors.copy()
        normalized.index = normalized.index.astype(str)
        normalized.index.name = "Month"
        metadata = {
            "provider": provider,
            "source_url": url,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_units": source_units,
            "normalized_units": "decimal returns",
            "first_month": str(factors.index.min()),
            "latest_month": str(factors.index.max()),
            "complete_months": len(factors),
            "raw_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        }
        normalized_text = normalized.to_csv()
        metadata_text = json.dumps(metadata, indent=2) + "\n"
        atomic_write_text(raw_path, raw_text)
        atomic_write_text(normalized_path, normalized_text)
        atomic_write_text(cache_dir / "india_ff3_monthly_latest.csv", normalized_text)
        atomic_write_text(metadata_path, metadata_text)
        atomic_write_text(cache_dir / "factor_source.json", metadata_text)
        return factors, f"{provider.upper()} web ({url}); raw: {raw_path}; normalized: {normalized_path}"
    except (requests.RequestException, ScreenerError, ValueError, UnicodeError, OSError) as exc:
        status(f"[Factors] Refresh failed ({type(exc).__name__}); checking same-provider cache...")
        try:
            factors = parse_india_factor_csv(raw_path.read_text(encoding="utf-8-sig"), source_units)
            if provider == "scdlds":
                validate_scdlds_scale(factors)
            retrieved = "unknown"
            if metadata_path.exists():
                retrieved = str(json.loads(metadata_path.read_text(encoding="utf-8")).get("retrieved_at_utc", "unknown"))
            return factors, f"{provider.upper()} local cache (retrieved {retrieved}; raw: {raw_path})"
        except (OSError, ValueError, UnicodeError, ScreenerError, json.JSONDecodeError):
            raise ScreenerError(
                f"Cannot fetch valid {provider.upper()} factors and no usable same-provider cache exists. "
                "Supply --factor-file or check internet access."
            ) from None


def eligible_eod_date(now: datetime) -> date:
    """Allow execution any time; today is only eligible after 18:00 IST.

    The returned date is a calendar limit, NOT a claim that an NSE session
    occurred or that Upstox has published a candle for the date.
    """
    local = now.astimezone(INDIA_TZ)
    return local.date() if local.time() >= EOD_CUTOFF_IST else local.date() - timedelta(days=1)


def prepare_factor_window(factors: pd.DataFrame, today: date) -> tuple[pd.DataFrame, pd.Period, int]:
    completed = pd.Period(today.strftime("%Y-%m"), freq="M") - 1
    usable = factors.loc[factors.index <= completed].copy()
    if usable.empty:
        raise DataQualityError(f"No completed India factor month through {completed}.")
    as_of = usable.index.max()
    required = pd.period_range(end=as_of, periods=REGRESSION_MONTHS, freq="M")
    missing = required.difference(usable.index)
    if len(missing):
        raise DataQualityError(f"Missing factors in the 36-month window (e.g. {', '.join(map(str, missing[:6]))}).")
    return usable, as_of, completed.ordinal - as_of.ordinal


def monthly_closes_from_daily(daily: pd.DataFrame) -> pd.Series:
    if daily.empty or "close" not in daily:
        return pd.Series(dtype=float, name="close")
    close = pd.to_numeric(daily.sort_index()["close"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    close = close.dropna()
    close = close[close > 0]
    if close.empty:
        return pd.Series(dtype=float, name="close")
    periods = pd.PeriodIndex(close.index, freq="M", name="month")
    monthly = pd.Series(close.to_numpy(dtype=float), index=periods, name="close")
    return monthly[~monthly.index.duplicated(keep="last")].sort_index()


def calculate_residual_momentum(
    monthly_stock_returns: pd.Series, factors: pd.DataFrame, as_of_month: pd.Period
) -> MomentumScore:
    """OLS uses an intercept; formation residuals intentionally omit alpha."""
    index = pd.period_range(end=as_of_month, periods=REGRESSION_MONTHS, freq="M", name="month")
    returns = pd.to_numeric(monthly_stock_returns.reindex(index), errors="coerce")
    factor_window = factors.reindex(index)[["mkt_rf", "smb", "hml", "rf"]].apply(pd.to_numeric, errors="coerce")
    if returns.isna().any() or factor_window.isna().any().any():
        raise DataQualityError("incomplete 36-month price/factor window")
    y = returns.to_numpy(dtype=float) - factor_window["rf"].to_numpy(dtype=float)
    x = factor_window[["mkt_rf", "smb", "hml"]].to_numpy(dtype=float)
    if not np.isfinite(y).all() or not np.isfinite(x).all():
        raise DataQualityError("non-finite return/factor values")
    design = np.column_stack((np.ones(len(index)), x))
    try:
        coefficients, _, rank, _ = np.linalg.lstsq(design, y, rcond=None)
        condition = float(np.linalg.cond(design))
    except np.linalg.LinAlgError:
        raise DataQualityError("OLS failed") from None
    if rank < 4 or not np.isfinite(coefficients).all() or not np.isfinite(condition) or condition > 1e8:
        raise DataQualityError("unstable or rank-deficient FF3 regression")
    alpha = float(coefficients[0])
    betas = coefficients[1:]
    # Positions 24..34 of 0..35: t-12 through t-2; position 35 (t-1) is skipped.
    formation = slice(-(FORMATION_RESIDUAL_MONTHS + 1), -1)
    residuals = y[formation] - x[formation] @ betas
    if len(residuals) != FORMATION_RESIDUAL_MONTHS:
        raise DataQualityError("incomplete formation period")
    residual_sum = float(residuals.sum())
    volatility = float(np.std(residuals, ddof=1))
    if not np.isfinite(volatility) or volatility <= 1e-8:
        raise DataQualityError("zero or invalid formation residual volatility")
    score = residual_sum / volatility
    fitted = design @ coefficients
    sse = float(np.sum((y - fitted) ** 2))
    sst = float(np.sum((y - y.mean()) ** 2))
    if not np.isfinite(score):
        raise DataQualityError("non-finite score")
    return MomentumScore(
        score=score,
        residual_sum=residual_sum,
        residual_volatility=volatility,
        alpha=alpha,
        beta_market=float(betas[0]),
        beta_smb=float(betas[1]),
        beta_hml=float(betas[2]),
        r_squared=1.0 - sse / sst if sst > 0 else 0.0,
    )


def fetch_prices(
    instruments: Sequence[Instrument], client: UpstoxClient, start: date, end: date,
    workers: int,
) -> tuple[dict[str, pd.DataFrame], dict[str, str], int]:
    prices: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}
    cache_hits = 0
    auth_error: Optional[UpstoxAuthenticationError] = None
    total = len(instruments)
    progress_every = max(1, min(25, math.ceil(total / 20)))
    started = time.monotonic()
    status(f"[Prices] Checking cache coverage or requesting {total} daily OHLCV histories "
           f"({start}..{end}); {workers} workers. Old caches without range metadata are refreshed once.")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.get_daily_candles, inst, start, end): inst for inst in instruments}
        for completed, future in enumerate(as_completed(futures), start=1):
            inst = futures[future]
            try:
                frame, was_cached = future.result()
                if frame.empty:
                    failures[inst.symbol] = "no daily candles returned"
                else:
                    prices[inst.symbol] = frame
                    cache_hits += int(was_cached)
            except UpstoxAuthenticationError as exc:
                auth_error = exc
                for pending in futures:
                    pending.cancel()
                break
            except (UpstoxRequestError, requests.RequestException, ValueError, OSError) as exc:
                failures[inst.symbol] = str(exc)
            if completed % progress_every == 0 or completed == total:
                status(
                    f"[Prices] {completed}/{total} ({100*completed/total:.0f}%); "
                    f"loaded={len(prices)} cached={cache_hits} failed={len(failures)} "
                    f"elapsed={time.monotonic()-started:.0f}s"
                )
    if auth_error:
        raise auth_error
    return prices, failures, cache_hits


def read_sector_map(path: Path, symbols: Sequence[str]) -> dict[str, str]:
    """Sectors must be supplied explicitly; an unknown sector is never invented."""
    if not path.exists():
        raise ScreenerError(
            f"Missing sector map: {path}. Create a CSV with headers symbol,sector and "
            "one checked sector for EVERY symbol in stock-data/universe.json. "
            "No sectors are guessed from company names."
        )
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise ScreenerError(f"Cannot read sector map {path}: {exc}") from None
    if not {"symbol", "sector"}.issubset(frame.columns):
        raise ScreenerError("Sector CSV must have headers: symbol,sector")
    sectors: dict[str, str] = {}
    for _, row in frame.iterrows():
        symbol, sector = str(row["symbol"]).strip().upper(), str(row["sector"]).strip().upper()
        if not symbol or not sector or symbol in sectors:
            raise ScreenerError("Sector CSV has blank fields or duplicate symbols.")
        sectors[symbol] = sector
    missing = sorted(set(symbols) - sectors.keys())
    if missing:
        raise ScreenerError(
            f"Sector CSV lacks {len(missing)} universe symbol(s): {', '.join(missing[:15])}"
            + (" ..." if len(missing) > 15 else "")
        )
    return sectors


def read_holdings_weights(path: Optional[Path]) -> Optional[dict[str, float]]:
    """Read paper current weights only. Never read quantities or write holdings."""
    if path is None:
        return None
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise ScreenerError(f"Cannot read holdings CSV {path}: {exc}") from None
    if not {"symbol", "weight_pct"}.issubset(frame.columns):
        raise ScreenerError("Holdings CSV needs: symbol,weight_pct (percent of total capital).")
    weights: dict[str, float] = {}
    for _, row in frame.iterrows():
        symbol = str(row["symbol"]).strip().upper()
        try:
            percent = float(row["weight_pct"])
        except ValueError:
            raise ScreenerError(f"Invalid holdings weight for {symbol}.") from None
        if not symbol or symbol in weights or not np.isfinite(percent) or not 0 <= percent <= 100:
            raise ScreenerError(f"Invalid or duplicate holdings entry: {symbol!r}.")
        if percent > 0:
            weights[symbol] = percent / 100
    if sum(weights.values()) > 1 + 1e-8:
        raise ScreenerError("Holdings weights exceed 100% of model capital.")
    return weights


def median_traded_value(daily: pd.DataFrame, sessions: int = TURNOVER_SESSIONS) -> float:
    """Median INR close × shares traded; not a bid–ask or execution guarantee."""
    if daily is None or len(daily) < sessions or not {"close", "volume"}.issubset(daily.columns):
        return float("nan")
    last = daily.sort_index().tail(sessions)
    close = pd.to_numeric(last["close"], errors="coerce").to_numpy(dtype=float)
    volume = pd.to_numeric(last["volume"], errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(close).all() or not np.isfinite(volume).all() or np.any(close <= 0) or np.any(volume < 0):
        return float("nan")
    return float(np.median(close * volume))


def valid_daily_close(daily: pd.DataFrame) -> pd.Series:
    """Use only finite positive closes; preserve missing sessions for exact alignment."""
    if daily is None or daily.empty or "close" not in daily:
        return pd.Series(dtype=float, name="close")
    frame = daily[["close"]].copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    if frame.index.tz is not None:
        frame.index = frame.index.tz_convert(INDIA_TZ).tz_localize(None)
    frame = frame[frame.index.notna()].sort_index(kind="stable")
    frame.index = frame.index.normalize()
    frame = frame[~frame.index.duplicated(keep="last")]
    close = pd.to_numeric(frame["close"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    close = close.where(close > 0).dropna().astype(float)
    close.name = "close"
    return close


def prepare_short_benchmark(
    daily: pd.DataFrame, eligible_end: date, today: date, max_age_days: int,
) -> tuple[pd.Series, date, float]:
    """Require 41 actual Nifty 500 closing sessions, without invented/filled prices."""
    benchmark = valid_daily_close(daily)
    benchmark = benchmark[benchmark.index.date <= eligible_end]
    if len(benchmark) < SHORT_REQUIRED_CLOSES:
        raise DataQualityError(
            f"Nifty 500 has only {len(benchmark)} valid daily closes through {eligible_end}; "
            f"need {SHORT_REQUIRED_CLOSES} for the 40-session context. No market-relative scores shown."
        )
    end = benchmark.index[-1].date()
    if not 0 <= (today - end).days <= max_age_days:
        raise DataQualityError(f"Nifty 500's last available close ({end}) is too old/future-dated.")
    window = benchmark.iloc[-SHORT_REQUIRED_CLOSES:]
    bench_20 = float(window.iloc[-1] / window.iloc[-(SHORT_LOOKBACK_SESSIONS + 1)] - 1.0)
    if not np.isfinite(bench_20):
        raise DataQualityError("Nifty 500 20-session return cannot be computed.")
    return window, end, bench_20


def rank_short_momentum(
    instruments: Sequence[Instrument], prices: Mapping[str, pd.DataFrame],
    sectors: Mapping[str, str], benchmark_window: pd.Series,
    min_turnover_inr: float,
) -> tuple[list[ShortMomentumStock], dict[str, int]]:
    """Rank raw 20-session stock returns; benchmark-relative is DISPLAY ONLY.

    The 5- and 40-session returns and daily sample volatility are context, not
    gates or ingredients of the 20-session ranking. All 41 benchmark sessions
    must have an exact stock close: no forward-fill, session guessing or mixing
    benchmark and stock returns from different end dates.
    """
    if len(benchmark_window) != SHORT_REQUIRED_CLOSES or benchmark_window.isna().any():
        raise DataQualityError("Short benchmark must contain exactly 41 valid closing sessions.")
    end = benchmark_window.index[-1]
    benchmark_20 = float(benchmark_window.iloc[-1] / benchmark_window.iloc[-21] - 1.0)
    reasons: dict[str, int] = {}
    stocks: list[ShortMomentumStock] = []

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for inst in instruments:
        daily = prices.get(inst.symbol)
        if daily is None or daily.empty:
            reject("no daily stock history")
            continue
        # Never borrow a newer stock candle when the benchmark is a day behind.
        earlier = daily.loc[daily.index <= end]
        closes = valid_daily_close(earlier)
        if closes.empty or closes.index[-1] != end:
            reject("no valid close on Nifty 500's latest session")
            continue
        turnover = median_traded_value(earlier)
        if not np.isfinite(turnover):
            reject(f"fewer than {TURNOVER_SESSIONS} valid turnover sessions")
            continue
        if turnover < min_turnover_inr:
            reject("below minimum median daily traded value")
            continue
        aligned = closes.reindex(benchmark_window.index)
        if aligned.isna().any():
            reject("missing a close among the 41 exact Nifty 500 sessions")
            continue
        end_price = float(aligned.iloc[-1])
        returns = {
            n: end_price / float(aligned.iloc[-(n + 1)]) - 1.0
            for n in (5, 20, 40)
        }
        daily_returns = aligned.iloc[-21:].pct_change(fill_method=None).iloc[1:].to_numpy(dtype=float)
        volatility = float(np.std(daily_returns, ddof=1))
        if (not all(np.isfinite(x) for x in returns.values())
            or not np.isfinite(volatility)):
            reject("non-finite recent return or volatility")
            continue
        stocks.append(ShortMomentumStock(
            symbol=inst.symbol,
            sector=sectors[inst.symbol],
            return_5=returns[5],
            return_20=returns[20],
            return_40=returns[40],
            benchmark_20=benchmark_20,
            excess_20=returns[20] - benchmark_20,
            daily_vol_20=volatility,
            latest_session=end.date(),
            median_turnover_inr=turnover,
        ))
    stocks.sort(key=lambda item: (-item.return_20, item.symbol))
    return stocks, reasons



def high_52_week(daily: Optional[pd.DataFrame], end: date,
                 first_listing: Optional[date] = None,
                 calendar: Optional[pd.DatetimeIndex] = None) -> Optional[float]:
    """Raw daily-high maximum in 365 calendar days ending at short cutoff.

    For a current security younger than a year, a corroborated NSE EQ
    security-listing date allows the window to start there. Otherwise partial
    history stays n/a; this is not a company-lifetime claim.
    """
    if daily is None or daily.empty or "high" not in daily:
        return None
    window_start = end - timedelta(days=364)
    coverage_start = max(window_start, first_listing) if first_listing else window_start
    frame = daily.sort_index()
    frame = frame.loc[(frame.index.date >= coverage_start) & (frame.index.date <= end)]
    if frame.empty or frame.index[0].date() > coverage_start + timedelta(days=10) or frame.index[-1].date() != end:
        return None
    if calendar is not None:
        expected = calendar[(calendar.date >= coverage_start) & (calendar.date <= end)]
        if expected.empty or not frame.index.equals(expected):
            return None
    highs = pd.to_numeric(frame["high"], errors="coerce").to_numpy(dtype=float)
    closes = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(frame["low"], errors="coerce").to_numpy(dtype=float)
    if (not np.isfinite(highs).all() or not np.isfinite(closes).all()
        or not np.isfinite(lows).all() or np.any(lows <= 0)
        or np.any(highs < closes) or np.any(highs < lows)):
        return None
    # A long hole may be a suspension, but cannot be certified as 52w coverage.
    days = [coverage_start, *(t.date() for t in frame.index)]
    if any((b-a).days > 14 for a, b in zip(days, days[1:])):
        return None
    return float(np.max(highs))


def current_security_listings(path: Path, instruments: Sequence[Instrument],
                              client: UpstoxClient, end: date) -> dict[str, date]:
    """Match security-master current ISIN start to today's official NSE EQ list.

    These are NOT verified original-company listing dates. For an ISIN reset,
    only the CURRENT security era is eligible, and the result is explicitly
    labeled 'Since ISIN', never 'all-time high'.
    """
    if not path.exists():
        status(f"[Highs] Security master missing at {path}; Since ISIN statuses n/a.")
        return {}
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"nse_symbol", "isin", "listing_date", "active_flag"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError("expected nse_symbol,isin,listing_date,active_flag columns")
            stated: dict[str, tuple[str, date]] = {}
            for row in reader:
                symbol = row["nse_symbol"].strip().upper()
                isin = row["isin"].strip().upper()
                listed = date.fromisoformat(row["listing_date"].strip())
                if not symbol or not isin or symbol in stated:
                    raise ValueError(f"blank/duplicate symbol: {symbol!r}")
                if row["active_flag"].strip().lower() == "true":
                    stated[symbol] = isin, listed
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, csv.Error) as exc:
        status(f"[Highs] Invalid security-master CSV ({exc}); Since ISIN statuses n/a.")
        return {}
    try:
        response = client._get(NSE_EQUITY_LIST_URL, authenticated=False)
        nse_rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        official: dict[tuple[str, str], set[date]] = {}
        for raw in nse_rows:
            row = {k.strip().upper(): (v or "").strip() for k, v in raw.items() if k}
            if row.get("SERIES") != "EQ":
                continue
            try:
                listed = datetime.strptime(row["DATE OF LISTING"], "%d-%b-%Y").date()
            except (KeyError, ValueError):
                continue
            official.setdefault((row.get("SYMBOL", ""), row.get("ISIN NUMBER", "")), set()).add(listed)
    except (UpstoxRequestError, UpstoxAuthenticationError, OSError, UnicodeError, csv.Error) as exc:
        status(f"[Highs] Cannot check the current official NSE EQ listing ({exc}); statuses n/a.")
        return {}
    matched: dict[str, date] = {}
    for inst in instruments:
        item = stated.get(inst.symbol)
        if item is None:
            continue
        isin, listing = item
        if (isin == inst.isin and UPSTOX_DAILY_HISTORY_START <= listing <= end
            and official.get((inst.symbol, inst.isin)) == {listing}):
            matched[inst.symbol] = listing
        else:
            status(f"[Highs] {inst.symbol}: current security date/ISIN disagrees with today's NSE EQ "
                   "list, lies outside Upstox history, or falls after report cutoff; status n/a.")
    return matched


def near_high(close: Optional[float], high: Optional[float], near_pct: float) -> Optional[bool]:
    """Within near_pct percent of the intraday peak using latest EOD close."""
    if (close is None or high is None or not np.isfinite(close) or not np.isfinite(high)
        or close <= 0 or high <= 0):
        return None
    return bool(close >= high * (1.0 - near_pct / 100.0))


def lifetime_decade_ranges(first: date, end: date) -> list[tuple[date, date, str, bool]]:
    """Calendar decades cap each Upstox V3 request to <=10 years (inclusive)."""
    ranges = []
    year = (first.year // 10) * 10
    while year <= end.year:
        begin = max(first, date(year, 1, 1))
        finish = min(end, date(year+9, 12, 31))
        if begin <= finish:
            ranges.append((begin, finish, f"{year}s", finish == date(year+9, 12, 31)))
        year += 10
    return ranges


def fetch_lifetime_calendar(client: UpstoxClient, index_instrument: Instrument,
                            first: date, end: date) -> Optional[pd.DatetimeIndex]:
    """Cross-check every candidate session against the Nifty 500 trading dates."""
    try:
        pieces = [client.get_daily_candles(index_instrument, begin, finish,
                  lifetime_chunk=label, immutable=immutable)[0]
                  for begin, finish, label, immutable in lifetime_decade_ranges(first, end)]
    except (UpstoxRequestError, UpstoxAuthenticationError, ValueError, OSError) as exc:
        status(f"[Highs] Nifty 500 lifetime session-calendar request failed ({exc}); Since ISIN n/a.")
        return None
    if not pieces or any(p.empty for p in pieces):
        return None
    calendar = valid_daily_close(pd.concat(pieces)).index
    if (calendar.empty or calendar[0].date() > first + timedelta(days=10)
        or calendar[-1].date() != end):
        return None
    checkpoints = [first, *(t.date() for t in calendar)]
    if any((b-a).days > 14 for a,b in zip(checkpoints,checkpoints[1:])):
        return None
    return calendar


def verified_lifetime_high(client: UpstoxClient, inst: Instrument, first: date,
                           end: date, recent: pd.DataFrame,
                           calendar: Optional[pd.DatetimeIndex] = None) -> Optional[float]:
    """Fail closed unless every decade, endpoints, gaps, recent overlap check out."""
    if (first > end or first < UPSTOX_DAILY_HISTORY_START or recent is None
        or recent.empty or calendar is None or calendar.empty):
        return None
    expected_sessions = calendar[(calendar.date >= first) & (calendar.date <= end)]
    if expected_sessions.empty or expected_sessions[0].date() > first + timedelta(days=10):
        return None
    parts = []
    try:
        for begin, finish, label, immutable in lifetime_decade_ranges(first, end):
            chunk, _ = client.get_daily_candles(inst, begin, finish,
                                                 lifetime_chunk=label, immutable=immutable)
            if chunk.empty:
                return None
            parts.append(chunk)
    except (UpstoxRequestError, UpstoxAuthenticationError, ValueError, OSError) as exc:
        status(f"[Highs] {inst.symbol}: lifetime history incomplete ({exc}); Since ISIN n/a.")
        return None
    history = pd.concat(parts).sort_index()
    if (history.index.has_duplicates or not history.index.equals(expected_sessions)
        or history.index[0].date() > first + timedelta(days=10) or history.index[-1].date() != end):
        return None  # fail closed on even one missing exchange session
    checkpoints = [first, *(t.date() for t in history.index)]
    if any((b-a).days > 14 for a,b in zip(checkpoints, checkpoints[1:])):
        return None
    high = pd.to_numeric(history["high"], errors="coerce").to_numpy(dtype=float)
    low = pd.to_numeric(history["low"], errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(history["close"], errors="coerce").to_numpy(dtype=float)
    if (not np.isfinite(high).all() or not np.isfinite(low).all()
        or not np.isfinite(close).all() or np.any(low <= 0)
        or np.any(high < close) or np.any(high < low)):
        return None
    # Verify historical pulls agree with the independently requested rolling
    # series for the last 63 observed sessions; a key change cannot be hidden.
    overlap = recent.loc[recent.index.date <= end].sort_index().tail(TURNOVER_SESSIONS)
    shared = history.reindex(overlap.index)
    if len(overlap) < TURNOVER_SESSIONS or shared[["high", "close"]].isna().any().any():
        return None
    for col in ("high", "close"):
        if not np.allclose(shared[col].to_numpy(dtype=float),
                           overlap[col].to_numpy(dtype=float), rtol=1e-6, atol=0.011):
            return None
    return float(np.max(high))


def enrich_short_highs(ranked: Sequence[ShortMomentumStock], size: int,
                       instruments: Sequence[Instrument], prices: Mapping[str, pd.DataFrame],
                       client: UpstoxClient, security_file: Path, end: date,
                       benchmark_daily: Optional[pd.DataFrame] = None,
                       near_pct: float = DEFAULT_HIGH_NEAR_PCT,
                       ) -> tuple[list[ShortMomentumStock], dict[str, int]]:
    """Only displayed short names incur extra history calls; rank is untouched."""
    from dataclasses import replace as dataclass_replace
    chosen = ranked[:size]
    listings = current_security_listings(security_file, instruments, client, end) if chosen else {}
    by_symbol = {inst.symbol: inst for inst in instruments}
    first_dates = [listings[s.symbol] for s in chosen if s.symbol in listings]
    calendar = (fetch_lifetime_calendar(client, client.nifty500_instrument, min(first_dates), end)
                if first_dates and client.nifty500_instrument else None)
    if first_dates and calendar is None:
        status("[Highs] Cannot check all historical trading sessions; Since ISIN n/a for all displayed names.")
    recent_calendar = (valid_daily_close(benchmark_daily).index
                       if benchmark_daily is not None else None)
    result = []
    stats = {"52w": 0, "isin": 0, "uncertain": 0}
    for stock in chosen:
        recent = prices.get(stock.symbol)
        listing = listings.get(stock.symbol)
        one_year = high_52_week(recent, end, listing, recent_calendar)
        if one_year is not None:
            stats["52w"] += 1
        isin_high = (verified_lifetime_high(client, by_symbol[stock.symbol], listing, end, recent, calendar)
                     if listing is not None and calendar is not None else None)
        # If a 52w high predates the current ISIN era, it need not be <= isin_high.
        # No cross-era numeric comparison is implied by the two separate flags.
        if isin_high is None:
            stats["uncertain"] += 1
        else:
            stats["isin"] += 1
        try:
            close = float(recent.loc[pd.Timestamp(end), "close"])
        except (KeyError, TypeError, ValueError, AttributeError):
            close = None
        result.append(dataclass_replace(
            stock, high_52w=one_year, isin_high=isin_high,
            near_52w=near_high(close, one_year, near_pct),
            near_isin_high=near_high(close, isin_high, near_pct),
        ))
    return result, stats



def create_report_tables(connection: sqlite3.Connection) -> None:
    """Create report-state schema inside the caller's SQLite transaction."""
    # Avoid executescript: it would commit a pending transaction implicitly.
    for ddl in (
        """CREATE TABLE IF NOT EXISTS report_runs (
            id INTEGER PRIMARY KEY,
            started_ist TEXT NOT NULL,
            factor_asof_month TEXT NOT NULL,
            short_asof_session TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS discoveries (
            screen TEXT NOT NULL CHECK(screen IN ('long','short')),
            symbol TEXT NOT NULL,
            first_seen_ist TEXT NOT NULL,
            first_run_id INTEGER NOT NULL REFERENCES report_runs(id),
            PRIMARY KEY (screen, symbol)
        )""",
        """CREATE TABLE IF NOT EXISTS report_members (
            run_id INTEGER NOT NULL REFERENCES report_runs(id),
            screen TEXT NOT NULL CHECK(screen IN ('long','short')),
            symbol TEXT NOT NULL,
            displayed_rank INTEGER NOT NULL,
            PRIMARY KEY (run_id, screen, symbol)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_report_members_symbol ON report_members(screen, symbol, run_id)",
    ):
        connection.execute(ddl)


def record_report_discoveries(
    path: Path, started_ist: datetime, factor_asof: pd.Period, short_asof: date,
    long_members: Sequence[PortfolioMember], short_members: Sequence[ShortMomentumStock],
) -> tuple[dict[str, DiscoveryStamp], dict[str, DiscoveryStamp], int]:
    """Atomically save both lists and first-ever dates, separately by screen.

    Every successful run gets a membership snapshot. A returning symbol retains
    its first-ever date even after intervening runs in which it was absent.
    Nothing is written until both reports have been calculated successfully.
    """
    selections = (
        ("long", [(m.stock.symbol, m.rank) for m in long_members]),
        ("short", [(s.symbol, i) for i, s in enumerate(short_members, start=1)]),
    )
    for screen, members in selections:
        symbols = [symbol for symbol, _ in members]
        if len(set(symbols)) != len(symbols):
            raise ScreenerError(f"Duplicate selected {screen} symbol; discovery state not written.")
    if started_ist.tzinfo is None:
        raise ScreenerError("Discovery run time must carry the IST timezone.")
    started_ist = started_ist.astimezone(INDIA_TZ)
    run_date = started_ist.date().isoformat()  # ISO internally; dd-mm-yy ONLY for console display.
    connection: Optional[sqlite3.Connection] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=15)
        with connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            create_report_tables(connection)
            cursor = connection.execute(
                "INSERT INTO report_runs(started_ist, factor_asof_month, short_asof_session) "
                "VALUES (?, ?, ?)",
                (started_ist.isoformat(timespec="seconds"), str(factor_asof), short_asof.isoformat()),
            )
            run_id = int(cursor.lastrowid)
            stamps: dict[str, dict[str, DiscoveryStamp]] = {"long": {}, "short": {}}
            for screen, members in selections:
                for symbol, rank in members:
                    added = connection.execute(
                        "INSERT OR IGNORE INTO discoveries(screen, symbol, first_seen_ist, first_run_id) "
                        "VALUES (?, ?, ?, ?)", (screen, symbol, run_date, run_id),
                    ).rowcount == 1
                    saved = connection.execute(
                        "SELECT first_seen_ist FROM discoveries WHERE screen=? AND symbol=?",
                        (screen, symbol),
                    ).fetchone()
                    if saved is None:
                        raise ScreenerError(f"Could not read discovery date for {screen} {symbol}.")
                    first_seen = date.fromisoformat(saved[0])
                    connection.execute(
                        "INSERT INTO report_members(run_id, screen, symbol, displayed_rank) "
                        "VALUES (?, ?, ?, ?)", (run_id, screen, symbol, rank),
                    )
                    stamps[screen][symbol] = DiscoveryStamp(first_seen, added)
        return stamps["long"], stamps["short"], run_id
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise ScreenerError(f"Could not save both report histories to SQLite {path}: {exc}") from None
    finally:
        if connection is not None:
            connection.close()


def discovery_display(stamp: DiscoveryStamp) -> tuple[str, str]:
    return stamp.first_seen.strftime("%d-%m-%y"), ("NEW" if stamp.new_this_run else "")


def print_short_screen(
    ranked: Sequence[ShortMomentumStock], exclusions: Mapping[str, int],
    benchmark_end: date, benchmark_20: float, size: int,
    mapped_count: int, requested_end: date, min_turnover_inr: float,
    highs_stats: Optional[Mapping[str, int]] = None,
    near_pct: float = DEFAULT_HIGH_NEAR_PCT,
    discoveries: Optional[Mapping[str, DiscoveryStamp]] = None,
) -> None:
    print("\n" + "=" * 78)
    print("SHORT-TERM EOD MOMENTUM SHORTLIST — SEPARATE FROM MONTHLY PORTFOLIO")
    print("=" * 78)
    print(f"20-session Nifty 500 return through {benchmark_end}: {benchmark_20 * 100:+.2f}% "
          "(Upstox index closes; TRI status not verified).")
    if benchmark_end < requested_end:
        print(f"BENCHMARK LAG: requested candles through {requested_end}, but index data ends {benchmark_end}; "
              "all short-term stock calculations end on that same index session.")
    print("Discovered = first-ever appearance on this SHORT list, using the IST report-run date; "
          "NEW marks first inclusion this run. Re-entry retains the original date.")
    print("Rank ONLY by stock's 20-session price return, highest first. Nifty-relative = stock 20D "
          "minus index 20D in percentage points; it does NOT change rank.")
    print("5D/40D and daily volatility are context only. Vol/day = sample standard deviation "
          "of the most recent 20 daily stock returns (not annualized).")
    print(f"Requirements: exact closes on all 41 benchmark sessions; "
          f"{TURNOVER_SESSIONS}-session median traded value ≥ ₹{min_turnover_inr:,.0f}/day.")
    print(f"Independent short-screen eligibility: {len(ranked)} of {mapped_count} mapped equities; "
          "36-month FF3 history and the long-term volatility cap are NOT required here.")
    print(f"Near-high status: Yes when the latest EOD CLOSE is within {near_pct:g}% of the relevant "
          "raw daily INTRADAY high (close ≥ high × (1 − threshold)); otherwise No.")
    print("52W = trailing 365 calendar days (or current-security listing if newer). "
          "Since ISIN = high since the CURRENT NSE security listing date, NOT company ATH.")
    print("n/a means missing/uncorroborated listing or candle coverage; never treat n/a as No. "
          "Both windows end on the same Nifty 500 session.")
    print("CAUTION: prices and highs are not independently split/dividend-adjusted; neither "
          "historical completeness nor corporate actions are independently guaranteed. "
          "Verify both manually; costs are not modeled.")
    if highs_stats:
        print(f"Status coverage for displayed names: 52W {highs_stats['52w']}, "
              f"Since ISIN {highs_stats['isin']}; Since ISIN n/a {highs_stats['uncertain']}.")
    chosen = ranked[:size]
    if not chosen:
        print("No eligible short-term stocks; check the date/coverage exclusions below.")
    else:
        headers = ("Rank", "Symbol", "Discovered", "New", "Sector", "Stock 20D", "N500 20D", "Excess pp",
                   "Stock 5D", "Stock 40D", "Vol/day", "Near 52W?", "Near ISIN high?", "Last close", "Median ₹Cr/day")
        rows = []
        for i, s in enumerate(chosen, start=1):
            stamp = discoveries[s.symbol] if discoveries is not None else None
            discovered, new_flag = discovery_display(stamp) if stamp else ("n/a", "")
            rows.append((
                str(i), s.symbol, discovered, new_flag, s.sector, f"{100*s.return_20:+.2f}%",
                f"{100*s.benchmark_20:+.2f}%", f"{100*s.excess_20:+.2f}",
                f"{100*s.return_5:+.2f}%", f"{100*s.return_40:+.2f}%",
                f"{100*s.daily_vol_20:.2f}%",
                "n/a" if s.near_52w is None else ("Yes" if s.near_52w else "No"),
                "n/a" if s.near_isin_high is None else ("Yes" if s.near_isin_high else "No"),
                s.latest_session.isoformat(), f"{s.median_turnover_inr/10_000_000:,.1f}",
            ))
        widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
        print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
        print("  ".join("-"*w for w in widths))
        for row in rows:
            print("  ".join(x.ljust(w) for x, w in zip(row, widths)))
    if exclusions:
        print("\nShort-screen exclusions (independent of monthly exclusions):")
        for reason, count in sorted(exclusions.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {count} — {reason}")
    print("This is a ranking for manual review, NOT a short-term target portfolio or trade instruction.\n")


def make_ranked_universe(
    instruments: Sequence[Instrument], prices: Mapping[str, pd.DataFrame], factors: pd.DataFrame,
    as_of: pd.Period, volatility_cap: float, sectors: Mapping[str, str], today: date,
    min_turnover_inr: float, max_price_age_days: int,
    month_end_gap_days: int = DEFAULT_MONTH_END_GAP_DAYS,
) -> tuple[list[RankedStock], dict[str, int]]:
    candidates: list[RankedStock] = []
    exclusions: dict[str, int] = {}

    def reject(reason: str) -> None:
        exclusions[reason] = exclusions.get(reason, 0) + 1

    for inst in instruments:
        daily = prices.get(inst.symbol)
        if daily is None or daily.empty:
            reject("no price history")
            continue
        close = pd.to_numeric(daily.sort_index()["close"], errors="coerce").replace([np.inf, -np.inf], np.nan)
        close = close.dropna()
        close = close[close > 0]
        if close.empty:
            reject("no valid close")
            continue
        latest_session = close.index[-1].date()
        if not 0 <= (today - latest_session).days <= max_price_age_days:
            reject("last price too old or future-dated")
            continue
        turnover = median_traded_value(daily)
        if not np.isfinite(turnover):
            reject(f"fewer than {TURNOVER_SESSIONS} valid turnover sessions")
            continue
        if turnover < min_turnover_inr:
            reject("below minimum median daily traded value")
            continue
        # 37 closes (as_of−36 .. as_of) create exactly 36 returns. A random
        # early-month close is NOT silently substituted for a month-end price.
        required_price_months = pd.period_range(
            start=as_of - REGRESSION_MONTHS, end=as_of, freq="M", name="month"
        )
        monthly = monthly_closes_from_daily(daily).reindex(required_price_months)
        last_dates = pd.Series(close.index, index=pd.PeriodIndex(close.index, freq="M"))
        last_dates = last_dates.groupby(level=0).max()
        incomplete = [
            month for month in required_price_months
            if month not in last_dates.index
            or (month.end_time.date() - last_dates.loc[month].date()).days > month_end_gap_days
        ]
        if incomplete:
            reject(f"missing/early month-end close in required 37 months (max gap {month_end_gap_days}d)")
            continue
        monthly_returns = monthly.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
        try:
            result = calculate_residual_momentum(monthly_returns, factors, as_of)
        except DataQualityError as exc:
            reject(str(exc))
            continue
        if result.residual_volatility > volatility_cap:
            reject(f"monthly residual volatility above {volatility_cap * 100:g}%")
            continue
        candidates.append(RankedStock(
            inst.symbol, result, float(close.iloc[-1]), latest_session,
            sectors[inst.symbol], turnover,
        ))
    return sorted(candidates, key=lambda stock: (-stock.result.score, stock.symbol)), exclusions


def select_diversified_candidates(
    ranked: Sequence[RankedStock], size: int, max_sector_weight: float,
    financial_cap: float = DEFAULT_MAX_FINANCIAL_FAMILY_PCT / 100,
) -> list[RankedStock]:
    """Reserve slots for both separate sectors and related financial businesses."""
    slots_per_sector = max(1, math.ceil(size * max_sector_weight))
    financial_slots = max(1, math.ceil(size * financial_cap))
    counts: dict[str, int] = {}
    financial_count = 0
    selected: list[RankedStock] = []
    for stock in ranked:
        if counts.get(stock.sector, 0) >= slots_per_sector:
            continue
        if stock.sector in FINANCIAL_FAMILY and financial_count >= financial_slots:
            continue
        selected.append(stock)
        counts[stock.sector] = counts.get(stock.sector, 0) + 1
        if stock.sector in FINANCIAL_FAMILY:
            financial_count += 1
        if len(selected) == size:
            break
    return selected


def constrained_weights(
    selected: Sequence[RankedStock], capital_inr: float, participation: float,
    name_cap: float, sector_cap: float, beta_cap: float,
    financial_cap: float = DEFAULT_MAX_FINANCIAL_FAMILY_PCT / 100,
) -> dict[str, float]:
    """Water-fill inverse-vol weights under linear caps; leave infeasible funds as cash.

    A conservative positive-beta budget is used: sum(max(beta_i, 0)*w_i)
    <= beta_cap, so negative betas never disguise positive market exposure.
    """
    if not selected:
        return {}
    base = {s.symbol: 1 / s.result.residual_volatility for s in selected}
    name_limits = {
        s.symbol: min(name_cap, participation * s.median_turnover_inr / capital_inr)
        for s in selected
    }
    weights = {s.symbol: 0.0 for s in selected}
    by_symbol = {s.symbol: s for s in selected}
    tolerance = 1e-10
    for _ in range(4 * len(selected) + 10):
        invested = sum(weights.values())
        sector_used: dict[str, float] = {}
        for symbol, weight in weights.items():
            sector = by_symbol[symbol].sector
            sector_used[sector] = sector_used.get(sector, 0.0) + weight
        beta_used = sum(max(0.0, by_symbol[s].result.beta_market) * w for s, w in weights.items())
        financial_used = sum(w for s, w in weights.items() if by_symbol[s].sector in FINANCIAL_FAMILY)
        active = [
            s for s in selected
            if name_limits[s.symbol] - weights[s.symbol] > tolerance
            and sector_cap - sector_used.get(s.sector, 0.0) > tolerance
            and (s.sector not in FINANCIAL_FAMILY or financial_cap - financial_used > tolerance)
            and (s.result.beta_market <= 0 or beta_cap - beta_used > tolerance)
        ]
        if not active or invested >= 1 - tolerance:
            break
        base_sum = sum(base[s.symbol] for s in active)
        proportions = {s.symbol: base[s.symbol] / base_sum for s in active}
        step = 1 - invested
        for s in active:
            step = min(step, (name_limits[s.symbol] - weights[s.symbol]) / proportions[s.symbol])
        for sector in {s.sector for s in active}:
            p = sum(proportions[s.symbol] for s in active if s.sector == sector)
            step = min(step, (sector_cap - sector_used.get(sector, 0.0)) / p)
        financial_share = sum(proportions[s.symbol] for s in active if s.sector in FINANCIAL_FAMILY)
        if financial_share > 0:
            step = min(step, (financial_cap - financial_used) / financial_share)
        positive_beta_per_step = sum(
            proportions[s.symbol] * max(0.0, s.result.beta_market) for s in active
        )
        if positive_beta_per_step > 0:
            step = min(step, max(0.0, beta_cap - beta_used) / positive_beta_per_step)
        if step <= tolerance:
            break
        for s in active:
            weights[s.symbol] += step * proportions[s.symbol]
    return {s: min(w, name_limits[s]) for s, w in weights.items() if w > tolerance}


@dataclass(frozen=True)
class PortfolioPlan:
    members: tuple[PortfolioMember, ...]
    model_weights: Mapping[str, float]
    staged_weights: Mapping[str, float]
    model_beta: float  # beta notional on total capital, including cash
    staged_beta: float
    model_cash: float
    staged_cash: float
    desired_turnover: Optional[float]
    staged_turnover: float
    stage_fraction: float
    warning: str = ""


def build_portfolio_plan(
    ranked: Sequence[RankedStock], size: int, capital_inr: float, participation: float,
    name_cap: float, sector_cap: float, beta_cap: float, holdings: Optional[Mapping[str, float]],
    max_rebalance_turnover: float,
    financial_cap: float = DEFAULT_MAX_FINANCIAL_FAMILY_PCT / 100,
) -> PortfolioPlan:
    selected = select_diversified_candidates(ranked, size, sector_cap, financial_cap)
    model = constrained_weights(selected, capital_inr, participation, name_cap, sector_cap, beta_cap, financial_cap)
    rank = {s.symbol: i for i, s in enumerate(ranked, 1)}
    stocks = {s.symbol: s for s in ranked}
    selected_iv = sum(1 / s.result.residual_volatility for s in selected)
    if holdings is not None:
        missing = sorted(set(holdings) - stocks.keys())
        if missing:
            raise ScreenerError(
                "Cannot safely stage a rebalance: held stocks lack current eligible scores/betas "
                f"({', '.join(missing[:12])}). Review them manually, or omit --holdings-file "
                "to view an initial-model basket (not a rebalance plan)."
            )
        union = set(model) | set(holdings)
        delta = {s: model.get(s, 0.0) - holdings.get(s, 0.0) for s in union}
        desired_turnover = sum(abs(x) for x in delta.values())
        fraction = min(1.0, max_rebalance_turnover / desired_turnover) if desired_turnover > 1e-12 else 1.0
        # Even an incumbent's sell order must fit a single day's participation budget.
        for s, change in delta.items():
            if abs(change) > 1e-12:
                order_cap = participation * stocks[s].median_turnover_inr / capital_inr
                fraction = min(fraction, order_cap / abs(change))
        staged = {
            s: holdings.get(s, 0.0) + fraction * delta[s]
            for s in union
            if holdings.get(s, 0.0) + fraction * delta[s] > 1e-10
        }
        turnover = desired_turnover * fraction
    else:
        desired_turnover = None  # the initial build is not a rebalance
        fraction = 1.0
        staged = dict(model)
        turnover = sum(model.values())

    model_beta = sum(stocks[s].result.beta_market * w for s, w in model.items())
    staged_beta = sum(stocks[s].result.beta_market * w for s, w in staged.items())
    staged_cash = max(0.0, 1 - sum(staged.values()))
    model_cash = max(0.0, 1 - sum(model.values()))
    members = tuple(
        PortfolioMember(
            stock=s, rank=rank[s.symbol], percentile_rank=100 * rank[s.symbol] / len(ranked),
            inverse_vol_weight=(1 / s.result.residual_volatility) / selected_iv,
            model_weight=model.get(s.symbol, 0.0), staged_weight=staged.get(s.symbol, 0.0),
        )
        for s in selected
    )
    warnings: list[str] = []
    # Old holdings can violate risk constraints even when the constrained *model* does not.
    if holdings is not None:
        if any(w > name_cap + 1e-8 for w in staged.values()):
            warnings.append("a staged holding remains above the single-stock cap")
        sector_totals: dict[str, float] = {}
        for symbol, weight in staged.items():
            sector = stocks[symbol].sector
            sector_totals[sector] = sector_totals.get(sector, 0.0) + weight
        if any(w > sector_cap + 1e-8 for w in sector_totals.values()):
            warnings.append("a staged sector remains above its cap")
        if sum(w for sector, w in sector_totals.items() if sector in FINANCIAL_FAMILY) > financial_cap + 1e-8:
            warnings.append("combined staged financial sectors remain above their shared cap")
        if staged_beta > beta_cap + 1e-8:
            warnings.append("staged portfolio market beta exceeds its cap")
    return PortfolioPlan(
        members, model, staged, model_beta, staged_beta, model_cash, staged_cash,
        desired_turnover, turnover, fraction, "; ".join(warnings),
    )


def print_screen(
    plan: PortfolioPlan, ranked: Sequence[RankedStock], requested_count: int, mapped_count: int,
    as_of: pd.Period, factor_lag: int, factor_source: str, cap: float, size: int,
    price_failures: Mapping[str, str], exclusions: Mapping[str, int], cache_hits: int,
    capital: float, min_turnover: float, max_price_age: int, participation_pct: float,
    name_pct: float, sector_pct: float, beta_cap: float,
    holdings: Optional[Mapping[str, float]], max_rebalance_pct: float,
    financial_pct: float = DEFAULT_MAX_FINANCIAL_FAMILY_PCT,
    month_end_gap_days: int = DEFAULT_MONTH_END_GAP_DAYS, requested_start: Optional[date] = None,
    requested_end: Optional[date] = None,
    discoveries: Optional[Mapping[str, DiscoveryStamp]] = None,
) -> None:
    def pct(value: float) -> str:
        return f"{100 * value:.2f}%"

    print("\nLONG-TERM NSE RESIDUAL-MOMENTUM EOD MODEL (MONTHLY) — NO ORDERS\n")
    print("1) WHAT WAS SCORED")
    print(f"  Monthly stock/factor data through {as_of}; factor lag: {factor_lag} completed month(s).")
    print(f"  Factor source: {factor_source}")
    print(f"  {requested_count} requested → {mapped_count} ordinary NSE EQ mappings → {len(ranked)} passed enabled checks.")
    if requested_start and requested_end:
        print(f"  Requested daily OHLCV: {requested_start} through {requested_end}, inclusive.")
        print("  Today is eligible only from 18:00 IST; the last available candle may be earlier.")
        print("  A returned same-day candle's finality is not independently verified; inspect its reported session date.")
    if ranked:
        print(f"  Actual latest daily close among eligible names: {min(s.latest_session for s in ranked)} to "
              f"{max(s.latest_session for s in ranked)}; not every stock traded on the same last date.")
    print(f"  All 37 necessary monthly closes must fall within {month_end_gap_days} days of their calendar month-end.")
    print("  This is a calendar proxy, not an official NSE holiday-calendar check.")
    print("  Score = 11 months' unusual returns added together / their monthly variation.")
    print("  The last month is skipped. A high score is NOT a prediction or a buy signal.")
    if "SCDLDS" in factor_source.upper():
        print("  SCDLDS labels its beta library free for academic use; check usage rights.")
    print("  RESEARCH-ONLY RETURN BASIS: Upstox close-to-close is not verified dividend/split-adjusted.")
    print("  A split, merger or demerger can distort scores; do not interpret this as an executable return forecast.")
    if factor_lag:
        print(f"  CAUTION: score is historical through {as_of}; do not mistake it for a current-month signal.")

    print("\n2) DAILY DATA & LIQUIDITY CHECKS (not an execution-quality test)")
    print(f"  Latest eligible daily candle no more than {max_price_age} calendar days old.")
    print(f"  {TURNOVER_SESSIONS}-session median close × volume at least ₹{min_turnover:,.0f} per day.")
    print(f"  Monthly residual volatility no higher than {100*cap:g}%.")
    print(f"  Each model position ≤ {participation_pct:g}% of its stock's median daily traded value (₹{capital:,.0f} model capital).")
    print("  EOD data does not establish a future execution price or fill.")
    print("\n3) HOW THE MODEL MONEY IS DIVIDED")
    print(f"  Choose up to {size} by score, reserving sector slots for diversification.")
    print(f"  IV starting weights = bigger shares for stocks with steadier unusual returns.")
    print(f"  Model caps: each stock ≤ {name_pct:g}%, each sector ≤ {sector_pct:g}%,")
    print(f"              Banking + NBFC + Financial Services + Asset Management + Insurance ≤ {financial_pct:g}% together.")
    print(f"              conservative positive-beta exposure ≤ {beta_cap:g} per ₹1 TOTAL model capital.")
    print("  Unused money stays as CASH: limits are not relaxed just to invest 100%.")
    print(f"  Model invested {pct(1-plan.model_cash)}; cash {pct(plan.model_cash)}; market beta exposure {plan.model_beta:.3f}.")
    if plan.model_cash > 0.005:
        print("  Why cash? Too few diverse names, per-stock limits, liquidity capacity, and/or beta limit.")
    if holdings is None:
        print(f"  Initial model only: no holdings supplied; no monthly turnover comparison or rebalance cap applied.")
        print(f"  Initial hypothetical purchases total {pct(plan.staged_turnover)} of capital.")
    else:
        print(f"  Existing weights supplied. Full model change would trade {pct(plan.desired_turnover or 0)} of capital.")
        print(f"  Staged paper change: trade {pct(plan.staged_turnover)} (≤ {max_rebalance_pct:g}% monthly rebalance budget)")
        print(f"  and ≤ {participation_pct:g}% of each stock's daily traded value per order; ")
        print(f"  {pct(plan.stage_fraction)} of the desired changes this run. No holdings are modified.")
        print(f"  After staging: cash {pct(plan.staged_cash)}, market beta exposure {plan.staged_beta:.3f}.")
        if plan.warning:
            print(f"  MANUAL REVIEW: {plan.warning}; staged holdings are NOT within all risk limits.")
    print("  Trading costs (including brokerage, taxes and execution effects) are NOT estimated.")
    if not plan.members:
        print("\nNo model names passed the data, liquidity and score requirements.")
    else:
        print("\n4) STOCK-BY-STOCK MODEL (score rank is NOT an instruction to buy)")
        print("  Category describes RELATIVE rank: even a TOP 10% stock can have a negative score.")
        print("  Score sign: + means positive past residual score, − means negative; neither predicts returns.")
        print("  IV start is before caps; Model is after caps; Staged is after any holdings-based trade limits.")
        print("  Discovered = first-ever appearance in this LONG model list on the IST run date; "
              "NEW marks first inclusion this run. Re-entry keeps the first date.")
        headers = ("Rank", "Category", "Sign", "Symbol", "Discovered", "New", "Sector", "Score", "Vol/mo", "Beta",
                   "Daily ₹Cr", "IV start", "Model", "Staged", "Model ₹")
        rows: list[tuple[str, ...]] = []
        for member in plan.members:
            stock = member.stock
            if member.percentile_rank <= 10:
                category = "TOP 10%"
            elif member.percentile_rank <= 50:
                category = "UPPER HALF"
            else:
                category = "LOWER HALF"
            stamp = discoveries[stock.symbol] if discoveries is not None else None
            discovered, new_flag = discovery_display(stamp) if stamp else ("n/a", "")
            rows.append((
                str(member.rank), category, "+" if stock.result.score > 0 else ("−" if stock.result.score < 0 else "0"),
                stock.symbol, discovered, new_flag, stock.sector, f"{stock.result.score:+.2f}",
                f"{100*stock.result.residual_volatility:.2f}%",
                f"{stock.result.beta_market:+.2f}", f"{stock.median_turnover_inr/10_000_000:.1f}",
                pct(member.inverse_vol_weight), pct(member.model_weight), pct(member.staged_weight),
                f"₹{member.model_weight*capital:,.0f}",
            ))
        widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
        print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
        print("  " + "  ".join("-"*w for w in widths))
        for row in rows:
            print("  " + "  ".join(x.ljust(w) for x, w in zip(row, widths)))
        if holdings is not None:
            carryovers = {s: w for s, w in plan.staged_weights.items() if s not in {m.stock.symbol for m in plan.members}}
            if carryovers:
                print("  Still held outside the new model because trading is staged:")
                for s, w in sorted(carryovers.items()):
                    print(f"    {s}: {pct(w)} (new model target 0%; manual review if necessary)")
        print("\n  MODEL SECTORS (cash is shown separately)")
        sectors: dict[str, float] = {}
        for m in plan.members:
            sectors[m.stock.sector] = sectors.get(m.stock.sector, 0) + m.model_weight
        for sector, weight in sorted(sectors.items(), key=lambda item: (-item[1], item[0])):
            if weight > 1e-10:
                print(f"    {sector}: {pct(weight)}")
        print(f"    FINANCIAL FAMILY (combined, not extra investment): "
              f"{pct(sum(w for sector, w in sectors.items() if sector in FINANCIAL_FAMILY))}")
        print(f"    CASH: {pct(plan.model_cash)}")
    if exclusions:
        print("\n5) WHY NAMES WERE LEFT OUT")
        for reason, count in sorted(exclusions.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {count} — {reason}")
    if price_failures:
        examples = list(price_failures.items())[:12]
        print(f"  {len(price_failures)} price-data request failures; examples: " +
              ", ".join(f"{s} ({reason})" for s, reason in examples))
    print("\nThis is NOT an optimized/proven profitable strategy. It produces no orders or trade instructions.\n")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only India FF3 residual-momentum research portfolio; no trading.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--state-db-file", type=Path, default=DEFAULT_STATE_DB_FILE,
                        help="SQLite first-discovery and per-run selected-list history "
                             "(default stock-data/report_state.sqlite3)")
    parser.add_argument("--factor-file", type=Path, default=None, help="local monthly India factor CSV")
    parser.add_argument("--factor-provider", choices=("scdlds", "iima"), default=None)
    parser.add_argument("--factor-units", choices=("percent", "decimal"), default=None)
    parser.add_argument("--max-residual-vol-pct", type=float, default=None, help="default 15%%")
    parser.add_argument("--portfolio-size", type=int, default=None, help="default 50 for long-term model")
    parser.add_argument("--short-size", type=int, default=DEFAULT_SHORT_SIZE,
                        help="number of recent-momentum stocks to display (default 50); not portfolio weights")
    parser.add_argument("--security-master-file", "--listing-dates-file", dest="security_master_file",
                        type=Path, default=DEFAULT_SECURITY_MASTER_FILE,
                        help="current-security CSV nse_symbol,isin,company_name,listing_date,active_flag "
                             "(default stock-data/nse_security_master.csv; NOT original company listing dates)")
    parser.add_argument("--high-near-pct", type=float, default=DEFAULT_HIGH_NEAR_PCT,
                        help="Yes when latest close is within this percent of the relevant intraday high (default 2)")
    parser.add_argument("--sector-file", type=Path, default=DEFAULT_SECTOR_FILE,
                        help="REQUIRED CSV symbol,sector for every universe symbol (default stock-data/sectors.csv)")
    parser.add_argument("--holdings-file", type=Path, help="optional CSV symbol,weight_pct for paper rebalance only")
    parser.add_argument("--capital-inr", type=float, default=DEFAULT_CAPITAL_INR, help="model capital, default ₹10 lakh")
    parser.add_argument("--min-turnover-inr", type=float, default=DEFAULT_MIN_TURNOVER_INR,
                        help="minimum 63-session median traded value per stock, default ₹1 crore/day")
    parser.add_argument("--max-price-age-days", type=int, default=DEFAULT_MAX_PRICE_AGE_DAYS)
    parser.add_argument("--month-end-max-gap-days", type=int, default=DEFAULT_MONTH_END_GAP_DAYS,
                        help="last close within this many calendar days of each month-end, default 7")
    parser.add_argument("--max-participation-pct", type=float, default=DEFAULT_MAX_PARTICIPATION_PCT,
                        help="max hypothetical per-name order / median daily traded value, default 2%%")
    parser.add_argument("--max-name-pct", type=float, default=DEFAULT_MAX_NAME_PCT, help="default 5%% of capital")
    parser.add_argument("--max-sector-pct", type=float, default=DEFAULT_MAX_SECTOR_PCT, help="default 25%% of capital")
    parser.add_argument("--max-financial-family-pct", type=float,
                        default=DEFAULT_MAX_FINANCIAL_FAMILY_PCT,
                        help="combined banking/NBFC/financial services/asset management/insurance, default 25%%")
    parser.add_argument("--max-beta", type=float, default=BETA_UPPER_BOUND, help="positive-beta budget, default 1.3")
    parser.add_argument("--max-rebalance-turnover-pct", type=float,
                        default=DEFAULT_MAX_REBALANCE_TURNOVER_PCT,
                        help="only when holdings supplied, default 25%% of capital")
    parser.add_argument("--max-factor-lag-months", type=int, default=2,
                        help="stop if factor data is older than this, default 2")
    parser.add_argument("--allow-stale-factors", action="store_true",
                        help="research-only override of factor-lag safety stop")
    parser.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS, help="1..8, default 6")
    parser.add_argument("--refresh", action="store_true", help="ignore cached candles")
    return parser


def run_screen(args: argparse.Namespace) -> int:
    symbols = load_universe_file()
    # Fail early and explicitly: sector names cannot be derived from the instrument master.
    sectors = read_sector_map(args.sector_file, symbols)
    now_ist = datetime.now(INDIA_TZ)
    today = now_ist.date()
    holdings = read_holdings_weights(args.holdings_file)
    for name, value in (
        ("--capital-inr", args.capital_inr), ("--min-turnover-inr", args.min_turnover_inr),
        ("--max-price-age-days", args.max_price_age_days),
        ("--max-participation-pct", args.max_participation_pct),
        ("--max-name-pct", args.max_name_pct), ("--max-sector-pct", args.max_sector_pct),
        ("--max-financial-family-pct", args.max_financial_family_pct),
        ("--month-end-max-gap-days", args.month_end_max_gap_days),
        ("--max-beta", args.max_beta), ("--max-rebalance-turnover-pct", args.max_rebalance_turnover_pct),
    ):
        if not np.isfinite(value) or value < 0:
            raise ScreenerError(f"{name} must be finite and nonnegative.")
    if (args.capital_inr <= 0 or args.max_participation_pct <= 0 or args.max_name_pct <= 0
        or args.max_sector_pct <= 0 or args.max_financial_family_pct <= 0
        or args.max_beta <= 0
        or args.max_price_age_days < 1 or args.max_factor_lag_months < 0
        or args.month_end_max_gap_days > 15
        or args.max_name_pct > 100 or args.max_sector_pct > 100
        or args.max_financial_family_pct > 100
        or args.max_participation_pct > 100 or args.max_rebalance_turnover_pct > 200):
        raise ScreenerError("Invalid capital, age, beta, percentage, or factor-lag limit.")
    if args.max_name_pct > args.max_sector_pct:
        raise ScreenerError("Max single-name percent must not exceed max sector percent.")
    values = load_env_values(args.env_file)
    token = env_value(values, "UPSTOX_ACCESS_TOKEN", "") or ""
    if not token.strip():
        raise ScreenerError(f"UPSTOX_ACCESS_TOKEN missing from {args.env_file}, {LEGACY_ENV_FILE}, or environment.")
    factor_file = args.factor_file
    if factor_file is None:
        configured = env_value(values, "INDIA_FF_FACTOR_FILE", "")
        factor_file = Path(configured).expanduser() if configured else None
    units = args.factor_units or env_value(values, "INDIA_FF_FACTOR_UNITS", "percent") or "percent"
    provider = (args.factor_provider or env_value(values, "INDIA_FACTOR_PROVIDER", "scdlds") or "scdlds").lower()
    if units not in ("percent", "decimal") or provider not in ("scdlds", "iima"):
        raise ScreenerError("Factor units must be percent/decimal; provider must be scdlds/iima.")
    cap_pct = args.max_residual_vol_pct
    if cap_pct is None:
        cap_pct = optional_float(env_value(values, "MAX_MONTHLY_RESIDUAL_VOLATILITY_PCT"),
                                 DEFAULT_MAX_MONTHLY_RESIDUAL_VOLATILITY * 100,
                                 "MAX_MONTHLY_RESIDUAL_VOLATILITY_PCT")
    if not np.isfinite(cap_pct) or not 0 < cap_pct <= 100:
        raise ScreenerError("Residual-volatility cap must be finite and in (0,100] percent.")
    size = args.portfolio_size
    if size is None:
        size = optional_int(env_value(values, "PORTFOLIO_SIZE"), DEFAULT_PORTFOLIO_SIZE, "PORTFOLIO_SIZE")
    cache_hours = optional_float(env_value(values, "PRICE_CACHE_TTL_HOURS"), DEFAULT_CACHE_TTL_HOURS,
                                 "PRICE_CACHE_TTL_HOURS")
    if (not np.isfinite(args.high_near_pct) or not 0 <= args.high_near_pct < 100):
        raise ScreenerError("--high-near-pct must be finite and in [0,100).")
    if size < 1 or args.short_size < 1 or not 1 <= args.workers <= 8 or cache_hours < 0:
        raise ScreenerError("Portfolio/short display sizes must be positive, workers 1..8, and cache TTL nonnegative.")

    status("[1/4] Loading Indian monthly factors...")
    factors, source = load_factor_data(factor_file, units, provider, DEFAULT_FACTOR_CACHE_DIR)
    factors, as_of, lag = prepare_factor_window(factors, today)
    status(f"[1/4] Factor data through {as_of}; lag {lag} completed month(s).")
    if lag > args.max_factor_lag_months and not args.allow_stale_factors:
        raise DataQualityError(
            f"Factor data is {lag} months behind, exceeding limit {args.max_factor_lag_months}. "
            "Update it or use --allow-stale-factors for a clearly historical RESEARCH run."
        )
    if lag:
        status(f"WARNING: score is historical through {as_of}; do not call it a current-month signal.")

    status("[2/4] Resolving NSE EQ symbols from the Upstox instrument master...")
    client = UpstoxClient(token.strip(), DEFAULT_CACHE_DIR, cache_hours * 3600, args.refresh)
    instruments = client.get_nse_equity_instruments(symbols)
    if not instruments:
        raise ScreenerError("No universe symbols mapped uniquely to current NSE_EQ instruments.")
    if client.nifty500_instrument is None:
        raise DataQualityError("Nifty 500 index key was not uniquely resolved; cannot display the required benchmark.")
    status(f"[2/4] Nifty 500 benchmark instrument: {client.nifty500_instrument.instrument_key}")
    mapped = {inst.symbol for inst in instruments}
    unmapped = [s for s in symbols if s not in mapped]
    status(f"[2/4] Resolved {len(instruments)} of {len(symbols)} symbols.")
    if unmapped:
        for symbol in unmapped[:20]:
            status(f"[Mapping] {symbol}: {client.instrument_diagnostics.get(symbol, 'unmapped (inspect master)')}")
        if len(unmapped) > 20:
            status(f"[Mapping] ...and {len(unmapped) - 20} more unmapped; no symbol is silently substituted.")

    # 36 monthly returns ending as_of need 37 closes, starting as_of−36.
    # Request exactly the calendar range containing those closes and current
    # daily liquidity observations. Today is requested only after EOD cutoff.
    first_price_month = as_of - REGRESSION_MONTHS
    start_date = first_price_month.start_time.date()
    end_date = eligible_eod_date(now_ist)
    if end_date < start_date:
        raise DataQualityError("No completed calendar day is available after the first needed price month.")
    status(f"[3/4] Requesting daily OHLCV {start_date}..{end_date} inclusive; "
           "today is included only at/after 18:00 IST. Actual first/last sessions vary by stock.")
    prices, failures, hits = fetch_prices(instruments, client, start_date, end_date, args.workers)
    status("[Benchmark] Loading Nifty 500 daily index candles once, on the same EOD date range...")
    benchmark_daily, benchmark_cached = client.get_daily_candles(
        client.nifty500_instrument, start_date, end_date
    )
    benchmark_window, benchmark_end, benchmark_20 = prepare_short_benchmark(
        benchmark_daily, end_date, today, args.max_price_age_days
    )
    status(f"[Benchmark] {len(benchmark_window)} aligned index sessions through {benchmark_end}; "
           f"20-session return {benchmark_20*100:+.2f}%; cached={benchmark_cached}.")

    status(f"[4/4] Scoring and checking {len(prices)} histories for both screens...")
    ranked, exclusions = make_ranked_universe(
        instruments, prices, factors, as_of, cap_pct / 100, sectors, today,
        args.min_turnover_inr, args.max_price_age_days, args.month_end_max_gap_days,
    )
    plan = build_portfolio_plan(
        ranked, size, args.capital_inr, args.max_participation_pct / 100,
        args.max_name_pct / 100, args.max_sector_pct / 100, args.max_beta,
        holdings, args.max_rebalance_turnover_pct / 100,
        args.max_financial_family_pct / 100,
    )
    short_ranked, short_exclusions = rank_short_momentum(
        instruments, prices, sectors, benchmark_window, args.min_turnover_inr
    )
    status(f"[4/4] Long: {len(ranked)} eligible / {len(plan.members)} model candidates; "
           f"short: {len(short_ranked)} eligible. No tradability or profit guarantee.")
    status(f"[Highs] Checking near-52-week and current-ISIN-era highs for {min(len(short_ranked), args.short_size)} "
           "displayed short names; ranking and monthly portfolio remain unchanged.")
    short_display, highs_stats = enrich_short_highs(
        short_ranked, args.short_size, instruments, prices, client,
        args.security_master_file, benchmark_end, benchmark_daily, args.high_near_pct,
    )
    long_discoveries, short_discoveries, report_id = record_report_discoveries(
        args.state_db_file, now_ist, as_of, benchmark_end, plan.members, short_display,
    )
    status(f"[State] Committed report #{report_id} to {args.state_db_file} "
           f"(first-ever new: long {sum(v.new_this_run for v in long_discoveries.values())}, "
           f"short {sum(v.new_this_run for v in short_discoveries.values())}).")
    print_screen(
        plan, ranked, len(symbols), len(instruments), as_of, lag, source, cap_pct / 100, size,
        failures, exclusions, hits, args.capital_inr, args.min_turnover_inr,
        args.max_price_age_days, args.max_participation_pct, args.max_name_pct,
        args.max_sector_pct, args.max_beta,
        holdings, args.max_rebalance_turnover_pct, args.max_financial_family_pct,
        args.month_end_max_gap_days, start_date, end_date, long_discoveries,
    )
    print_short_screen(
        short_display, short_exclusions, benchmark_end, benchmark_20,
        args.short_size, len(instruments), end_date, args.min_turnover_inr,
        highs_stats, args.high_near_pct, short_discoveries,
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argument_parser().parse_args(argv)
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
