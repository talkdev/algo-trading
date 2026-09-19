"""SCREENING ONLY — no trading logic.

Fixed-universe price-return momentum screening against the NIFTY 500 price index.
Own-price, cross-sectional and market-relative metrics remain separate. Frozen
multi-horizon qualifiers and an all-eligible composite produce screening reports.
The aligned-matrix metric interface is pure and suitable for research harnesses.

Credential loading: by default, env.txt is read from beside this Python file,
not from the shell's working directory. Override with --env /path/to/env.txt.
No manual environment-variable setup is necessary when that file is populated.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as wall_time, timedelta, timezone
import gzip
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
from typing import Any, Mapping, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import dotenv_values

# =============================================================================
# Frozen configuration and fixed universe
# =============================================================================
# ---- FROZEN RESEARCH PARAMETERS (do not tune at runtime; change only via code + README changelog) ----
HORIZONS = (21, 63, 126, 252)          # trading days
MIN_HISTORY_DAYS = 253                 # closes needed for a 252-day return (252 returns)
MAX_MISSING_DAYS = 3                   # max gaps in the 253-row window that may be forward-filled
MIN_ELIGIBLE_FRACTION = 0.50           # abort if fewer than this fraction of the universe is eligible
FETCH_CALENDAR_DAYS = 550              # calendar-day lookback requested from Upstox (buffer for holidays)
VOL_ANNUALISE = True                   # Vol_n = std(daily simple returns, ddof=1) * sqrt(252)
RANK_BASIS = "R"                       # cross-sectional ranks computed on raw n-day return R_n ("RAM" = alternative)
P_STRONG = 70.0                        # UNVALIDATED DEFAULT — percentile cutoff for STRONG_XS_MOMENTUM
P_VERY_STRONG = 85.0                   # UNVALIDATED DEFAULT — percentile cutoff for VERY_STRONG_MOMENTUM
POS_COUNT_MIN = 3                      # "3 of 4" multi-horizon confirmation — a screen-design choice
COMPOSITE_WEIGHTS = {                  # FINAL_SCREEN_RANK components (all 0-100), equal weights
    "Rank252": 1.0, "Rank126": 1.0, "pct_MR252": 1.0, "pct_MR126": 1.0, "pct_RAM252": 1.0,
}
CORP_ACTION_JUMP = 0.25                # |daily return| above this in the window → SUSPECT_CORP_ACTION flag
BENCHMARK_NAME = "NIFTY 500"
BENCHMARK_EXPECTED_KEY = "NSE_INDEX|Nifty 500"

UNIVERSE = [
    "RELIANCE","HDFCBANK","ICICIBANK","SBIN","AXISBANK","KOTAKBANK","INDUSINDBK","BANKBARODA","PNB","CANBK",
    "TCS","INFY","WIPRO","TECHM","HCLTECH","PERSISTENT","COFORGE","MPHASIS",
    "LT","ULTRACEMCO","GRASIM","SHREECEM","AMBUJACEM","ACC",
    "MARUTI","EICHERMOT","ASHOKLEY","BAJAJ-AUTO","TVSMOTOR","HEROMOTOCO",
    "SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","LUPIN","AUROPHARMA","ZYDUSLIFE","TORNTPHARM",
    "BHARTIARTL","IDEA",
    "HINDUNILVR","ITC","NESTLEIND","BRITANNIA","DABUR","GODREJCP","COLPAL","MARICO",
    "TITAN","TRENT","DMART","VBL",
    "ADANIENT","ADANIPORTS","ADANIPOWER","ADANIENSOL","ADANIGREEN",
    "TATASTEEL","JSWSTEEL","HINDALCO","JINDALSTEL","SAIL","NMDC","VEDL","NATIONALUM",
    "ONGC","COALINDIA","BPCL","IOC","HINDPETRO","OIL","GAIL",
    "POWERGRID","NTPC","TATAPOWER",
    "BAJFINANCE","BAJAJFINSV","CHOLAFIN","SHRIRAMFIN","LICHSGFIN",
    "INDIGO","IRCTC","RVNL","BEL","HAL","BHEL","SIEMENS","ABB","CGPOWER","DIXON",
]
assert len(UNIVERSE) == len(set(UNIVERSE)) == 90

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = "https://api.upstox.com"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
SCREEN_COLUMNS = (
    "Symbol", "Close", "R21", "R63", "R126", "R252",
    "RAM21", "RAM63", "RAM126", "RAM252",
    "Rank21", "Rank63", "Rank126", "Rank252",
    "MR21", "MR63", "MR126", "MR252", "POS_COUNT", "FINAL_SCREEN_RANK",
)
FLAG_COLUMNS = (
    "MARKET_OUTPERFORMER", "STRONG_TREND", "STRONG_XS_MOMENTUM",
    "VERY_STRONG_MOMENTUM", "CORE_QUALIFIER", "FINAL_QUALIFIER",
)
AUTH_MESSAGE = "Access token rejected/expired — regenerate UPSTOX_ACCESS_TOKEN in env.txt and rerun."
ADJUSTMENT_NOTE = (
    "Upstox daily closes are stated to be split-adjusted; dividend adjustment is not "
    "guaranteed. Returns are price returns against the NIFTY 500 price index, not TRI. "
    "Later vendor revisions or split adjustments can change historical results."
)


def validate_config() -> None:
    """Reject inconsistent frozen parameters before credentials or network access."""
    if tuple(sorted(set(HORIZONS))) != HORIZONS or min(HORIZONS) < 2:
        raise ValueError("HORIZONS must be positive, unique and sorted.")
    if MIN_HISTORY_DAYS != max(HORIZONS) + 1:
        raise ValueError("MIN_HISTORY_DAYS must cover the largest horizon plus one close.")
    if not 0 <= P_STRONG <= P_VERY_STRONG <= 100:
        raise ValueError("Require 0 <= P_STRONG <= P_VERY_STRONG <= 100.")
    expected = {"Rank252", "Rank126", "pct_MR252", "pct_MR126", "pct_RAM252"}
    if set(COMPOSITE_WEIGHTS) != expected:
        raise ValueError("Composite component names do not match the frozen design.")
    if any(not math.isfinite(w) or w < 0 for w in COMPOSITE_WEIGHTS.values()):
        raise ValueError("Composite weights must be finite and nonnegative.")
    if sum(COMPOSITE_WEIGHTS.values()) <= 0:
        raise ValueError("Composite weights must have a positive sum.")
    if RANK_BASIS not in {"R", "RAM"}:
        raise ValueError("RANK_BASIS must be R or RAM.")
    if len(set(UNIVERSE)) != 90 or len(UNIVERSE) != 90:
        raise ValueError("The fixed universe must contain 90 unique symbols.")


# =============================================================================
# Credentials and sanitized failures
# =============================================================================
class ScreenerError(Exception):
    """A user-facing failure whose message must not contain response bodies or secrets."""


class AuthenticationError(ScreenerError):
    """Authentication failure: always fatal, including during individual-stock fetches."""


class APIError(ScreenerError):
    """Sanitized market-data transport or response failure."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Keep only a safe message and an optional HTTP status code."""
        super().__init__(message)
        self.status = status


class CacheError(ScreenerError):
    """Missing offline cache or invalid cache; never silently use different data."""


@dataclass(frozen=True)
class Credentials:
    """Secret values are excluded from dataclass representations."""

    access_token: str = field(repr=False)
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)


def load_credentials(env_path: str | Path) -> Credentials:
    """Read the credential file before any network access; never log its values.

    The CLI defaults to env.txt beside this Python file. Explicit relative
    --env paths are relative to the shell's working directory, as usual.
    UTF-8 files with or without a BOM are supported. File entries take
    precedence; the process environment is consulted only for absent keys.
    """
    path = Path(env_path).expanduser().absolute()
    try:
        # Opening explicitly avoids dotenv_values silently accepting a missing
        # file. utf-8-sig also removes a possible Windows/editor UTF-8 BOM.
        with path.open("r", encoding="utf-8-sig") as handle:
            values = dotenv_values(stream=handle, interpolate=False)
    except FileNotFoundError:
        # Preserve the original environment-only usage, without requiring it.
        if not os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip():
            raise ScreenerError(
                f"Credential file not found: {path}. "
                "Put env.txt beside this Python file, or pass --env /full/path/env.txt. "
                "Check that the filename is not env.txt.txt."
            ) from None
        values = {}
        logging.warning("Credential file not found at %s; using process environment.", path)
    except (OSError, UnicodeError):
        raise ScreenerError(
            f"Cannot read credential file: {path}. "
            "Check permissions and save it as plain UTF-8 text."
        ) from None
    else:
        logging.info("Read credential file: %s (values are not logged).", path)

    def get_value(key: str) -> str | None:
        """Preserve explicit empty file values instead of overriding them."""
        raw = values[key] if key in values else os.environ.get(key)
        return str(raw).strip() if raw is not None else None

    token = get_value("UPSTOX_ACCESS_TOKEN")
    api_key = get_value("UPSTOX_API_KEY")
    api_secret = get_value("UPSTOX_API_SECRET")
    for name, value in (("UPSTOX_API_KEY", api_key), ("UPSTOX_API_SECRET", api_secret)):
        if not value:
            logging.warning("%s is missing; it is not used for market data.", name)
    if not token:
        raise ScreenerError(
            f"UPSTOX_ACCESS_TOKEN is missing or empty in {path} "
            "and no applicable environment fallback is available. "
            "Use UPSTOX_ACCESS_TOKEN=your_complete_current_access_token. "
            "An explicitly empty file entry is not replaced by an environment value."
        )
    if any(character in token for character in ("\r", "\n")):
        raise ScreenerError("UPSTOX_ACCESS_TOKEN has an invalid format; keep it on one line.")
    return Credentials(token, api_key, api_secret)


# =============================================================================
# REST client, rate limiting, instrument resolution and raw caches
# =============================================================================
def atomic_text(path: Path, content: str) -> None:
    """Replace one UTF-8 file atomically; retain no partially written cache files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def empty_series() -> pd.Series:
    """Return an empty, typed daily close series."""
    return pd.Series(index=pd.DatetimeIndex([], name="date"), dtype=float, name="close")


def normalize_series(series: pd.Series) -> pd.Series:
    """Normalize dates to naive IST calendar midnights, sort, keep last duplicates."""
    result = series.copy().astype(float)
    index = pd.DatetimeIndex(result.index)
    if index.hasnans:
        raise ValueError("Daily dates must not contain missing timestamps.")
    if index.tz is not None:
        index = index.tz_convert(IST).tz_localize(None)
    result.index = index.normalize()
    result = result.loc[~result.index.duplicated(keep="last")].sort_index()
    result.index.name = "date"
    result.name = "close"
    return result


class UpstoxClient:
    """Read-only REST client with bounded retries and per-instance rate accounting."""

    def __init__(self, credentials: Credentials, cache_dir: Path,
                 refresh: bool = False, offline: bool = False,
                 today: date | None = None, master_dir: Path = Path("data")) -> None:
        """Build separate authenticated API and unauthenticated asset sessions."""
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {credentials.access_token}",
                                     "Accept": "application/json"})
        self.asset_session = requests.Session()
        self.asset_session.headers.update({"Accept": "application/json"})
        self.cache_dir = Path(cache_dir)
        self.master_dir = Path(master_dir)
        self.refresh = refresh
        self.offline = offline
        self.today = today or datetime.now(IST).date()
        self.request_times: deque[float] = deque()
        self.sources: set[str] = set()
        if refresh and offline:
            raise ScreenerError("--refresh and --offline cannot be combined.")

    def close(self) -> None:
        """Release both HTTP connection pools."""
        self.session.close()
        self.asset_session.close()

    def _throttle(self) -> None:
        """Space calls by 0.21 seconds and enforce rolling minute/30-minute caps."""
        while True:
            now = time.monotonic()
            while self.request_times and now - self.request_times[0] >= 1800:
                self.request_times.popleft()
            delay = 0.0
            if self.request_times:
                delay = max(delay, self.request_times[-1] + 0.21 - now)
            if len(self.request_times) >= 500:
                delay = max(delay, self.request_times[-500] + 60.001 - now)
            if len(self.request_times) >= 2000:
                delay = max(delay, self.request_times[-2000] + 1800.001 - now)
            if delay <= 0:
                self.request_times.append(now)
                return
            time.sleep(delay)

    def _get(self, url: str, authenticated: bool = True) -> requests.Response:
        """GET only known market-data URLs; five total attempts, no body logging."""
        if self.offline:
            raise CacheError("Offline mode forbids network requests.")
        if authenticated:
            if not (url.startswith(BASE_URL + "/v3/historical-candle/") or
                    url.startswith(BASE_URL + "/v2/historical-candle/")):
                raise APIError("Unsupported market-data URL.")
        elif url != MASTER_URL:
            raise APIError("Unsupported instrument-master URL.")
        session = self.session if authenticated else self.asset_session
        backoff = (1, 2, 4, 8, 16)
        for attempt in range(5):
            self._throttle()
            response: requests.Response | None = None
            try:
                response = session.get(url, timeout=(10, 45), allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError):
                failure = "Market-data request timed out or could not connect."
            except requests.RequestException:
                raise APIError("Market-data request failed; check local HTTP configuration.") from None
            else:
                status = response.status_code
                if status == 401:
                    response.close()
                    raise AuthenticationError(AUTH_MESSAGE)
                if status == 200:
                    return response
                response.close()
                if status != 429 and not 500 <= status <= 599:
                    raise APIError(f"Market-data HTTP {status}; not retried.", status)
                failure = f"Market-data HTTP {status}; retry budget exhausted."
            if attempt == 4:
                raise APIError(failure, response.status_code if response is not None else None)
            logging.warning("Transient market-data failure; retry %d of 4 in %ds.",
                            attempt + 1, backoff[attempt])
            time.sleep(backoff[attempt])
        raise APIError("Market-data request failed.")

    @staticmethod
    def _validate_master(payload: Any) -> list[dict[str, Any]]:
        """Validate relevant documented fields without inventing alternate schemas."""
        if not isinstance(payload, list) or not payload:
            raise ScreenerError("Instrument master schema: expected a nonempty JSON array.")
        for row in payload:
            if not isinstance(row, dict) or any(
                not isinstance(row.get(key), str) or not row[key]
                for key in ("segment", "instrument_type")
            ):
                raise ScreenerError("Instrument master schema: missing string segment/instrument_type.")
            if row["segment"] in {"NSE_EQ", "NSE_INDEX"}:
                fields = ["trading_symbol", "instrument_key", "name"]
                if row["segment"] == "NSE_EQ" and row["instrument_type"] == "EQ":
                    fields.append("isin")
                if any(not isinstance(row.get(key), str) or not row[key] for key in fields):
                    raise ScreenerError(
                        "Instrument master schema: expected name, trading_symbol, instrument_key "
                        "and equity isin strings; check current Upstox JSON documentation."
                    )
        return payload

    def instrument_master(self) -> list[dict[str, Any]]:
        """Use today's decompressed NSE master, or download the official gzip array."""
        path = self.master_dir / f"instruments_NSE_{self.today.isoformat()}.json"
        if path.exists() and not self.refresh:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                raise CacheError("Invalid instrument-master cache; refresh it online.") from None
            self.sources.add("instrument_master_cache")
            return self._validate_master(payload)
        if self.offline:
            raise CacheError("Offline instrument-master cache missing for today's IST date.")
        response = self._get(MASTER_URL, authenticated=False)
        try:
            content = response.content
            if content.startswith(b"\x1f\x8b"):
                content = gzip.decompress(content)
            payload = json.loads(content)
        except (OSError, EOFError, ValueError, UnicodeError):
            raise ScreenerError("Instrument master is not valid gzip/JSON.") from None
        finally:
            response.close()
        master = self._validate_master(payload)
        atomic_text(path, json.dumps(master, ensure_ascii=False))
        self.sources.add("instrument_master_download")
        return master

    @staticmethod
    def resolve_equity(master: Sequence[Mapping[str, Any]], symbol: str) -> str | None:
        """Resolve exactly one case-sensitive NSE EQ symbol; absence is reportable."""
        matches = [row["instrument_key"] for row in master
                   if row["segment"] == "NSE_EQ" and row["instrument_type"] == "EQ"
                   and row["trading_symbol"] == symbol]
        if len(matches) > 1:
            raise ScreenerError(f"Ambiguous instrument resolution for {symbol}; inspect the master.")
        return str(matches[0]) if matches else None

    @staticmethod
    def resolve_index(master: Sequence[Mapping[str, Any]]) -> str:
        """Resolve only the fixed benchmark by normalized name or symbol and key."""
        target = " ".join(BENCHMARK_NAME.casefold().split())
        matches = [row["instrument_key"] for row in master
                   if row["segment"] == "NSE_INDEX" and row["instrument_type"] == "INDEX"
                   and any(" ".join(str(row.get(key, "")).casefold().split()) == target
                           for key in ("name", "trading_symbol"))]
        if len(matches) != 1 or matches[0] != BENCHMARK_EXPECTED_KEY:
            raise ScreenerError("NIFTY 500 benchmark missing, ambiguous or unexpected key; aborting.")
        return str(matches[0])

    @staticmethod
    def _parse_candles(payload: Any) -> pd.Series:
        """Parse aware timestamps as IST dates; preserve bad closes for eligibility."""
        try:
            if payload["status"] != "success":
                raise ValueError
            candles = payload["data"]["candles"]
            if not isinstance(candles, list):
                raise ValueError
            dates: list[pd.Timestamp] = []
            closes: list[float] = []
            for row in candles:
                if not isinstance(row, list) or len(row) != 7:
                    raise ValueError
                timestamp = pd.Timestamp(row[0])
                if pd.isna(timestamp) or timestamp.tzinfo is None:
                    raise ValueError
                dates.append(timestamp.tz_convert(IST).tz_localize(None).normalize())
                closes.append(float(row[4]) if row[4] is not None else np.nan)
            if not dates:
                return empty_series()
            return normalize_series(pd.Series(closes, index=pd.DatetimeIndex(dates)))
        except (KeyError, TypeError, ValueError, OverflowError):
            raise APIError("Unexpected candle schema; require seven fields and aware timestamps.") from None

    def _candles_at(self, path: str) -> pd.Series:
        """Decode one sanitized API response and release its connection."""
        response = self._get(BASE_URL + path)
        try:
            try:
                payload = response.json()
            except ValueError:
                raise APIError("Market-data response is not valid JSON.") from None
            return self._parse_candles(payload)
        finally:
            response.close()

    def daily_candles(self, key: str, from_date: date, to_date: date) -> pd.Series:
        """Fetch V3 daily closes; fall back to deprecated V2 only on 404 or 410."""
        encoded = quote(key, safe="")
        try:
            series = self._candles_at(
                f"/v3/historical-candle/{encoded}/days/1/{to_date}/{from_date}"
            )
            self.sources.add("historical_v3")
        except APIError as error:
            if error.status not in {404, 410}:
                raise
            logging.warning("V3 historical endpoint unavailable; using documented V2 fallback.")
            series = self._candles_at(
                f"/v2/historical-candle/{encoded}/day/{to_date}/{from_date}"
            )
            self.sources.add("historical_v2_fallback")
        return series.loc[(series.index >= pd.Timestamp(from_date)) &
                          (series.index <= pd.Timestamp(to_date))].copy()

    def intraday_daily(self, key: str) -> pd.Series:
        """Fetch the current-session V3 daily candle without historical fallback."""
        series = self._candles_at(
            f"/v3/historical-candle/intraday/{quote(key, safe='')}/days/1"
        )
        self.sources.add("intraday_v3")
        return series.loc[series.index == pd.Timestamp(self.today)].copy()

    def cached_candles(self, symbol: str, key: str, from_date: date,
                       to_date: date, intraday: bool = False) -> pd.Series:
        """Cache raw close data, isolating provisional observations from historical ones."""
        suffix = f"intraday__{to_date}" if intraday else str(to_date)
        path = self.cache_dir / f"{symbol}__{suffix}.csv"
        if path.exists() and not self.refresh:
            try:
                frame = pd.read_csv(path, dtype={"instrument_key": str}, float_precision="round_trip")
                if list(frame.columns) != ["date", "close", "instrument_key"]:
                    raise ValueError
                if not frame.empty and not frame["instrument_key"].eq(key).all():
                    raise ValueError
                series = normalize_series(pd.Series(
                    pd.to_numeric(frame["close"], errors="raise").to_numpy(),
                    index=pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
                ))
            except (OSError, ValueError, TypeError, UnicodeError, pd.errors.ParserError):
                raise CacheError(f"Invalid candle cache for {symbol}; refresh it online.") from None
            self.sources.add("intraday_cache" if intraday else "historical_cache")
        else:
            if self.offline:
                raise CacheError(f"Offline candle cache missing for {symbol} ({suffix}).")
            series = self.intraday_daily(key) if intraday else self.daily_candles(key, from_date, to_date)
            frame = series.rename("close").reset_index()
            frame["instrument_key"] = key
            atomic_text(path, frame.to_csv(index=False, date_format="%Y-%m-%d"))
        return series.loc[(series.index >= pd.Timestamp(from_date)) &
                          (series.index <= pd.Timestamp(to_date))].copy()


# =============================================================================
# Data assembly: calendar, alignment and ordered eligibility
# =============================================================================
def intraday_enabled(requested: date, now: datetime, include_intraday: bool) -> bool:
    """Guard optional current-session data; historical dates never use today's candle."""
    local_now = now.astimezone(IST)
    if not include_intraday:
        return False
    if requested != local_now.date():
        logging.warning("Ignoring --include-intraday: --as-of is not today's IST date.")
        return False
    if local_now.time() < wall_time(15, 45):
        logging.warning("Ignoring --include-intraday: the IST time is before 15:45.")
        return False
    logging.warning("PROVISIONAL=True: Upstox may still revise the current-session close.")
    return True


def fetch_series(client: UpstoxClient, symbol: str, key: str,
                 requested: date, provisional: bool) -> pd.Series:
    """Exclude today's historical row; include it only via the guarded separate cache."""
    start = requested - timedelta(days=FETCH_CALENDAR_DAYS)
    historical = client.cached_candles(symbol, key, start, requested)
    historical = historical.loc[historical.index < pd.Timestamp(client.today)]
    if provisional:
        current = client.cached_candles(symbol, key, requested, requested, intraday=True)
        current = current.loc[current.index == pd.Timestamp(requested)]
        historical = pd.concat([historical, current])
    return normalize_series(historical.loc[historical.index <= pd.Timestamp(requested)])


def validate_benchmark(bench: pd.Series) -> pd.Series:
    """Require enough benchmark rows and valid closes in its evaluation window."""
    result = normalize_series(bench)
    if len(result) < MIN_HISTORY_DAYS:
        raise ScreenerError("Benchmark has fewer than MIN_HISTORY_DAYS daily rows; aborting.")
    window = result.iloc[-MIN_HISTORY_DAYS:]
    if not np.isfinite(window.to_numpy()).all() or (window <= 0).any():
        raise ScreenerError("Benchmark contains invalid closes in the evaluation window.")
    return result


def assemble_data(raw: Mapping[str, pd.Series], bench: pd.Series,
                  instrument_keys: Mapping[str, str | None],
                  fetch_failures: Mapping[str, str],
                  symbols: Sequence[str] = tuple(UNIVERSE)) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align to benchmark dates and return eligible windows plus all-symbol diagnostics.

    NaNs caused by absent sessions are gaps. Explicit invalid vendor closes are
    never repaired into eligibility. At most one leading-window gap is seeded
    from the latest earlier aligned observation, after checking total gap count.
    No value from after the benchmark's final date is consulted.
    """
    benchmark = validate_benchmark(bench)
    calendar = benchmark.index
    window_index = calendar[-MIN_HISTORY_DAYS:]
    effective = calendar[-1]
    cleaned: dict[str, pd.Series] = {}
    diagnostics: list[dict[str, Any]] = []
    for symbol in symbols:
        key = instrument_keys.get(symbol)
        source = normalize_series(raw.get(symbol, empty_series()))
        source = source.loc[source.index <= effective]
        aligned = source.reindex(calendar)
        window = aligned.iloc[-MIN_HISTORY_DAYS:].copy()
        missing = int(window.isna().sum())
        record: dict[str, Any] = {
            "Symbol": symbol, "ELIGIBLE": False, "INELIGIBLE_REASON": "",
            "MISSING_DAYS": missing if key and symbol in raw else None,
            "FIRST_DATE": source.index[0].date().isoformat() if len(source) else None,
            "LAST_DATE": source.index[-1].date().isoformat() if len(source) else None,
            "N_ROWS": len(source), "SUSPECT_CORP_ACTION": False,
            "instrument_key": key,
        }
        # Diagnostic only: use observed adjacent calendar prices for excluded rows.
        observed_returns = window.pct_change(fill_method=None).iloc[1:]
        record["SUSPECT_CORP_ACTION"] = bool((observed_returns.abs() > CORP_ACTION_JUMP).any())
        prior = aligned.iloc[:-MIN_HISTORY_DAYS].dropna()
        seed = prior.iloc[-1] if len(prior) else np.nan
        if not key:
            reason = "UNRESOLVED"
        elif symbol in fetch_failures:
            reason = "FETCH_FAILED"
        elif pd.isna(window.iloc[-1]):
            reason = "STALE"
        elif pd.isna(window.iloc[0]) and pd.isna(seed):
            reason = "INSUFFICIENT_HISTORY"
        elif missing > MAX_MISSING_DAYS:
            reason = "TOO_MANY_GAPS"
        else:
            explicit = source.reindex(window_index.intersection(source.index))
            invalid_observed = bool((~np.isfinite(explicit.to_numpy())).any() or (explicit <= 0).any())
            if pd.isna(window.iloc[0]):
                window.iloc[0] = seed
            window = window.ffill()
            bad = invalid_observed or not np.isfinite(window.to_numpy()).all() or bool((window <= 0).any())
            if not bad:
                with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                    vols = [realised_vol(window, n) for n in HORIZONS]
                    rets = [n_day_return(window, n) for n in HORIZONS]
                bad = any(not math.isfinite(v) or v <= 0 for v in vols)
                bad = bad or any(not math.isfinite(r) for r in rets)
                if not bad:
                    bad = any(not math.isfinite(r / v) for r, v in zip(rets, vols))
            reason = "BAD_DATA" if bad else ""
            if not reason:
                cleaned[symbol] = window
                record["SUSPECT_CORP_ACTION"] = bool(
                    (window.pct_change(fill_method=None).iloc[1:].abs() > CORP_ACTION_JUMP).any()
                )
        record["INELIGIBLE_REASON"] = reason
        record["ELIGIBLE"] = not reason
        diagnostics.append(record)
    return (pd.DataFrame(cleaned, index=window_index),
            pd.DataFrame(diagnostics).set_index("Symbol"))


def require_eligible_count(count: int) -> None:
    """Apply the fixed-universe denominator and absolute minimum cross-section."""
    if count < 2 or count / len(UNIVERSE) < MIN_ELIGIBLE_FRACTION:
        raise ScreenerError(
            f"Insufficient eligible universe: {count}/{len(UNIVERSE)}; "
            f"require at least {math.ceil(MIN_ELIGIBLE_FRACTION * len(UNIVERSE))} and at least 2."
        )


# =============================================================================
# Pure metrics, flags and cross-sectional composite (no I/O)
# =============================================================================
def n_day_return(closes: pd.Series, n: int) -> float:
    """Simple return using exactly n return intervals and n+1 terminal closes."""
    if n < 1 or len(closes) < n + 1:
        raise ValueError("Not enough closes for the requested return horizon.")
    return float(closes.iloc[-1] / closes.iloc[-n - 1] - 1.0)


def realised_vol(closes: pd.Series, n: int) -> float:
    """Sample standard deviation of exactly n simple daily returns, annualized if configured."""
    if n < 2 or len(closes) < n + 1:
        raise ValueError("Not enough closes for the requested volatility horizon.")
    values = closes.iloc[-n - 1:].to_numpy(dtype=float)
    daily = values[1:] / values[:-1] - 1.0
    scale = math.sqrt(252) if VOL_ANNUALISE else 1.0
    return float(np.std(daily, ddof=1) * scale)


def pct(values: pd.Series) -> pd.Series:
    """Average-rank percentiles on nonmissing values; fewer than two is undefined."""
    values = values.astype(float)
    count = int(values.notna().sum())
    if count < 2:
        return pd.Series(np.nan, index=values.index, name=values.name, dtype=float)
    return 100.0 * (values.rank(method="average", ascending=True) - 1.0) / (count - 1)


def apply_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Compute every frozen flag on a copy, retaining unrounded numerical boundaries."""
    result = df.copy(deep=True)
    result["MARKET_OUTPERFORMER"] = (result["MR126"] > 0) & (result["MR252"] > 0)
    result["STRONG_TREND"] = ((result["R126"] > 0) & (result["R252"] > 0) &
                              (result["POS_COUNT"] >= POS_COUNT_MIN))
    result["STRONG_XS_MOMENTUM"] = ((result["Rank126"] >= P_STRONG) &
                                    (result["Rank252"] >= P_STRONG))
    result["VERY_STRONG_MOMENTUM"] = ((result["Rank126"] >= P_VERY_STRONG) &
                                      (result["Rank252"] >= P_VERY_STRONG))
    common = result["MARKET_OUTPERFORMER"] & result["STRONG_TREND"]
    result["CORE_QUALIFIER"] = common & result["STRONG_XS_MOMENTUM"]
    result["FINAL_QUALIFIER"] = common & result["VERY_STRONG_MOMENTUM"]
    return result


def composite_score(df: pd.DataFrame) -> pd.DataFrame:
    """Score all eligible rows before filtering any qualifier; never mutate the input."""
    result = df.copy(deep=True)
    for name in ("MR252", "MR126", "RAM252"):
        result[f"pct_{name}"] = pct(result[name])
    total = pd.Series(0.0, index=result.index)
    for name, weight in COMPOSITE_WEIGHTS.items():
        total = total + weight * result[name]
    result["FINAL_SCREEN_RANK"] = total / sum(COMPOSITE_WEIGHTS.values())
    return result


def compute_metrics(closes: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    """Return all metrics/flags/scores from an already eligible, aligned close matrix.

    The caller selects the point-in-time window: closes.loc[:D] and bench.loc[:D].
    Both inputs must have exactly the same sorted unique daily calendar. Only
    their last 253 rows are used. Inputs are never filled, modified or fetched.
    At least two eligible columns are required; the CLI separately enforces 50%.
    """
    if closes.shape[1] < 2 or closes.columns.has_duplicates:
        raise ValueError("Metrics require at least two uniquely named eligible symbols.")
    if (not isinstance(closes.index, pd.DatetimeIndex) or
            not closes.index.equals(bench.index) or
            not closes.index.is_monotonic_increasing or closes.index.has_duplicates or
            closes.index.hasnans or len(closes) < MIN_HISTORY_DAYS):
        raise ValueError("Metrics require matching sorted unique calendars with at least 253 rows.")
    matrix = closes.iloc[-MIN_HISTORY_DAYS:]
    benchmark = bench.iloc[-MIN_HISTORY_DAYS:]
    for values in (matrix.to_numpy(dtype=float), benchmark.to_numpy(dtype=float)):
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError("Metrics require finite positive, gap-free aligned closes.")
    records: list[dict[str, Any]] = []
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        benchmark_returns = {n: n_day_return(benchmark, n) for n in HORIZONS}
    if not all(math.isfinite(value) for value in benchmark_returns.values()):
        raise ValueError("Metrics require finite benchmark returns.")
    for symbol in matrix.columns:
        series = matrix[symbol]
        row: dict[str, Any] = {"Symbol": symbol, "Close": float(series.iloc[-1])}
        for n in HORIZONS:
            r = n_day_return(series, n)
            vol = realised_vol(series, n)
            if not math.isfinite(vol) or vol <= 0 or not math.isfinite(r):
                raise ValueError("Metrics require finite returns and nonzero finite volatility.")
            ram = r / vol
            if not math.isfinite(ram):
                raise ValueError("Metrics require finite RAM values.")
            row.update({f"R{n}": r, f"Vol{n}": vol, f"RAM{n}": ram,
                        f"TS{n}": r, f"TS{n}_POS": r > 0,
                        f"B{n}": benchmark_returns[n], f"MR{n}": r - benchmark_returns[n]})
        row["POS_COUNT"] = sum(bool(row[f"TS{n}_POS"]) for n in HORIZONS)
        records.append(row)
    result = pd.DataFrame(records).set_index("Symbol")
    for n in HORIZONS:
        result[f"Rank{n}"] = pct(result[f"{RANK_BASIS}{n}"])
    return composite_score(apply_flags(result))


# =============================================================================
# Output writers and final console report
# =============================================================================
def frozen_parameters() -> dict[str, Any]:
    """Return a serializable copy of the entire frozen parameter block."""
    return {
        "HORIZONS": list(HORIZONS), "MIN_HISTORY_DAYS": MIN_HISTORY_DAYS,
        "MAX_MISSING_DAYS": MAX_MISSING_DAYS, "MIN_ELIGIBLE_FRACTION": MIN_ELIGIBLE_FRACTION,
        "FETCH_CALENDAR_DAYS": FETCH_CALENDAR_DAYS, "VOL_ANNUALISE": VOL_ANNUALISE,
        "RANK_BASIS": RANK_BASIS, "P_STRONG": P_STRONG, "P_VERY_STRONG": P_VERY_STRONG,
        "POS_COUNT_MIN": POS_COUNT_MIN, "COMPOSITE_WEIGHTS": dict(COMPOSITE_WEIGHTS),
        "CORP_ACTION_JUMP": CORP_ACTION_JUMP, "BENCHMARK_NAME": BENCHMARK_NAME,
        "BENCHMARK_EXPECTED_KEY": BENCHMARK_EXPECTED_KEY,
    }


def final_screen(metrics: pd.DataFrame) -> pd.DataFrame:
    """Select qualifiers, sort using full precision, and enforce the exact CSV schema."""
    rows = metrics.loc[metrics["FINAL_QUALIFIER"]].reset_index()
    rows = rows.sort_values(["FINAL_SCREEN_RANK", "Rank252", "Symbol"],
                            ascending=[False, False, True], kind="mergesort")
    return rows.loc[:, list(SCREEN_COLUMNS)].reset_index(drop=True)


def format_for_file(frame: pd.DataFrame) -> pd.DataFrame:
    """Render floats at declared precision without changing computations or flags."""
    result = frame.copy(deep=True)
    for column in result.columns:
        if column == "Close":
            digits = 2
        elif column.startswith(("Rank", "pct_")) or column == "FINAL_SCREEN_RANK":
            digits = 2
        elif pd.api.types.is_float_dtype(result[column]):
            digits = 6
        else:
            continue
        result[column] = result[column].map(
            lambda value, d=digits: "" if pd.isna(value) else f"{value:.{d}f}"
        )
    return result


def build_universe(metrics: pd.DataFrame, diagnostics: pd.DataFrame,
                   bench: pd.Series, provisional: bool) -> pd.DataFrame:
    """Retain every universe member; excluded metrics/flags remain explicitly unavailable."""
    result = diagnostics.join(metrics, how="left")
    # Benchmark returns exist independently of stock eligibility.
    for n in HORIZONS:
        result[f"B{n}"] = n_day_return(bench, n)
        result[f"TS{n}_POS"] = result[f"TS{n}_POS"].astype("boolean")
    for name in FLAG_COLUMNS:
        result[name] = result[name].astype("boolean")
    for name in ("POS_COUNT", "MISSING_DAYS", "N_ROWS"):
        result[name] = result[name].astype("Int64")
    result["PROVISIONAL"] = provisional
    result = result.reset_index()
    front = list(SCREEN_COLUMNS)
    remaining = [name for name in result.columns if name not in front]
    return result.loc[:, front + remaining]


def build_run_metadata(metrics: pd.DataFrame, diagnostics: pd.DataFrame,
                       bench: pd.Series, requested: date, provisional: bool,
                       sources: Sequence[str], timestamp: datetime) -> dict[str, Any]:
    """Build an auditable run record with no credentials and no nonstandard JSON values."""
    effective = bench.index[-1].date().isoformat()
    exclusions = [{"Symbol": str(symbol), "reason": str(row["INELIGIBLE_REASON"])}
                  for symbol, row in diagnostics.iterrows() if not row["ELIGIBLE"]]
    flag_counts = {name: int(metrics[name].sum()) for name in FLAG_COLUMNS}
    return {
        "as_of_requested": requested.isoformat(), "as_of_effective": effective,
        "provisional": provisional,
        "benchmark": {"name": BENCHMARK_NAME, "instrument_key": BENCHMARK_EXPECTED_KEY,
                      **{f"B{n}": round(n_day_return(bench, n), 6) for n in HORIZONS}},
        "frozen_parameters": frozen_parameters(), "rank_basis": RANK_BASIS,
        "counts": {"universe": len(diagnostics),
                   "resolved": int(diagnostics["instrument_key"].notna().sum()),
                   "eligible": int(diagnostics["ELIGIBLE"].sum()),
                   "per_flag": flag_counts,
                   "core": flag_counts["CORE_QUALIFIER"], "final": flag_counts["FINAL_QUALIFIER"]},
        "unresolved": [item["Symbol"] for item in exclusions if item["reason"] == "UNRESOLVED"],
        "ineligible": exclusions,
        "data_source": {"provider": "Upstox", "base_url": BASE_URL,
                        "instrument_master_url": MASTER_URL, "access_paths": sorted(sources)},
        "adjustment_note": ADJUSTMENT_NOTE,
        "timestamp_utc": timestamp.astimezone(timezone.utc).isoformat(),
        "data_window": {"first_date": bench.index[-MIN_HISTORY_DAYS].date().isoformat(),
                        "last_date": effective, "rows": MIN_HISTORY_DAYS,
                        "master_first_date": bench.index[0].date().isoformat(),
                        "master_rows": len(bench),
                        "fetch_from": (requested - timedelta(days=FETCH_CALENDAR_DAYS)).isoformat(),
                        "fetch_to": requested.isoformat()},
        "output_labels": {
            f"screen_{effective}.csv": {"PROVISIONAL": provisional},
            f"universe_{effective}.csv": {"PROVISIONAL": provisional},
            f"run_{effective}.json": {"PROVISIONAL": provisional},
        },
    }


def write_outputs(out_dir: Path, metrics: pd.DataFrame, diagnostics: pd.DataFrame,
                  bench: pd.Series, metadata: Mapping[str, Any]) -> tuple[Path, Path, Path]:
    """Write both CSVs then the run manifest; each replacement is individually atomic."""
    effective = str(metadata["as_of_effective"])
    screen_path = out_dir / f"screen_{effective}.csv"
    universe_path = out_dir / f"universe_{effective}.csv"
    run_path = out_dir / f"run_{effective}.json"
    screen = format_for_file(final_screen(metrics))
    universe = format_for_file(build_universe(metrics, diagnostics, bench, bool(metadata["provisional"])))
    # Serialize all three before changing any existing report.
    screen_text = screen.to_csv(index=False)
    universe_text = universe.to_csv(index=False, na_rep="")
    run_text = json.dumps(dict(metadata), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    atomic_text(screen_path, screen_text)
    atomic_text(universe_path, universe_text)
    atomic_text(run_path, run_text)
    return screen_path, universe_path, run_path


def console_tables(metrics: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """Build readable display-only tables in the exact full-precision screen order.

    Console R values use percentages and MR values use percentage points; raw
    metrics and machine-readable files remain unchanged decimal fractions.
    Every panel repeats the same numbered stock order for easy comparison.
    """
    rows = final_screen(metrics)
    if rows.empty:
        return []
    display = rows.copy(deep=True)
    display.insert(0, "#", range(1, len(display) + 1))
    display["Close"] = display["Close"].map(lambda value: f"{value:,.2f}")
    display["POS_COUNT"] = display["POS_COUNT"].map(lambda value: f"{int(value)}/4")
    display["FINAL_SCREEN_RANK"] = display["FINAL_SCREEN_RANK"].map(lambda value: f"{value:.2f}")
    for n in HORIZONS:
        display[f"R{n}"] = display[f"R{n}"].map(lambda value: f"{value * 100:+.2f}%")
        display[f"MR{n}"] = display[f"MR{n}"].map(lambda value: f"{value * 100:+.2f} pp")
        display[f"RAM{n}"] = display[f"RAM{n}"].map(lambda value: f"{value:.3f}")
        display[f"Rank{n}"] = display[f"Rank{n}"].map(lambda value: f"{value:.2f}")
    groups = (
        ("1. OVERALL STRENGTH | Close in INR; score out of 100",
         ["Close", "POS_COUNT", "FINAL_SCREEN_RANK"]),
        ("2. PRICE CHANGE | % gain or loss", [f"R{n}" for n in HORIZONS]),
        ("3. RETURN RELATIVE TO PRICE SWINGS | RAM ratio", [f"RAM{n}" for n in HORIZONS]),
        ("4. RANK AMONG ELIGIBLE STOCKS | 0 to 100", [f"Rank{n}" for n in HORIZONS]),
        ("5. COMPARED WITH NIFTY 500 | pp = percentage points", [f"MR{n}" for n in HORIZONS]),
    )
    return [(title, display.loc[:, ["#", "Symbol", *columns]].copy())
            for title, columns in groups]


def console_legend() -> str:
    """Give every screen column a brief plain-language explanation, without I/O."""
    return "\n".join((
        "QUICK COLUMN GUIDE",
        "  Symbol : Stock code.       Close : Latest closing price in INR.",
        "  21 / 63 / 126 / 252 : Trading days; about 1 / 3 / 6 / 12 months.",
        "  R21/R63/R126/R252 : Price gain or loss over each period (shown as %).",
        "  RAM21/RAM63/RAM126/RAM252 : Return divided by price swings (volatility).",
        "  Rank21/Rank63/Rank126/Rank252 : Rank among eligible stocks; higher is stronger.",
        "  MR21/MR63/MR126/MR252 : Stock return minus NIFTY 500 return (shown as pp).",
        "  POS_COUNT : How many of the four periods have a positive return (out of 4).",
        "  FINAL_SCREEN_RANK : Combined strength score, 0-100; higher appears first.",
        "  Example: +5.00 pp means 5 percentage points ahead of NIFTY 500.",
        "  Scores are comparisons, not probabilities or promises of future gains.",
        "  Console: % / pp. CSV and JSON: returns remain decimal fractions.",
    ))


def console_report(metadata: Mapping[str, Any], metrics: pd.DataFrame | None = None,
                   aborted: str | None = None, out_dir: Path | None = None) -> None:
    """The only application print site: readable summary, ranked panels and short guide."""
    border = "=" * 88
    divider = "-" * 88
    print("\n" + border)
    print("MOMENTUM SCREEN | NIFTY 500 | SCREENING ONLY")
    print(border)
    print(f"Requested date : {metadata['as_of_requested']}    "
          f"Data through : {metadata['as_of_effective']}")
    print(f"PROVISIONAL={metadata['provisional']}")
    if metadata["provisional"]:
        print("NOTE: Provisional data - Upstox may still revise the current-session close.")
    else:
        print("Completed-session data only; current-session intraday data not included.")
    benchmark = metadata.get("benchmark", {})
    if benchmark:
        print("\nNIFTY 500 PRICE CHANGE")
        print("  " + "  |  ".join(f"{n} days: {benchmark[f'B{n}'] * 100:+.2f}%" for n in HORIZONS))
    if "counts" in metadata:
        counts = metadata["counts"]
        print(f"\nCOVERAGE   Universe={counts['universe']}  |  Resolved={counts['resolved']}  |  "
              f"Eligible={counts['eligible']}")
        labels = {
            "MARKET_OUTPERFORMER": "Ahead of the market (6 and 12 months)",
            "STRONG_TREND": "Positive own-price trend",
            "STRONG_XS_MOMENTUM": "Strong rank among eligible stocks",
            "VERY_STRONG_MOMENTUM": "Very strong rank among eligible stocks",
            "CORE_QUALIFIER": "Passed core screening rules",
            "FINAL_QUALIFIER": "Passed final screening rules",
        }
        for name, value in counts["per_flag"].items():
            shown = str(value) if value is not None else "not calculated"
            print(f"  {labels.get(name, name):<43} : {shown}")
    exclusions = metadata.get("ineligible", [])
    print(f"\nEXCLUSIONS ({len(exclusions)})")
    if not exclusions:
        print("  None - all universe symbols have usable data.")
    else:
        grouped: dict[str, list[str]] = {}
        for item in exclusions:
            grouped.setdefault(str(item["reason"]), []).append(str(item["Symbol"]))
        descriptions = {
            "UNRESOLVED": "symbol not found",
            "FETCH_FAILED": "could not retrieve data",
            "STALE": "latest session close missing",
            "INSUFFICIENT_HISTORY": "not enough price history",
            "TOO_MANY_GAPS": "too many missing daily closes",
            "BAD_DATA": "invalid prices or unusable volatility",
        }
        for reason, symbols in grouped.items():
            print(f"  {reason} ({len(symbols)}) - {descriptions.get(reason, reason)}")
            print(textwrap.fill(", ".join(symbols), width=88, initial_indent="    ", subsequent_indent="    "))
    print("\n" + divider)
    if aborted:
        print("SCREEN NOT COMPLETED")
        print(textwrap.fill(aborted, width=88))
        print("No new screening CSVs were generated; check the run status before using old files.")
    elif metrics is not None:
        tables = console_tables(metrics)
        print("FINAL QUALIFIERS | STRONGEST FIRST")
        print("Sorted by FINAL_SCREEN_RANK, then Rank252, then stock code for ties.")
        print("Only stocks passing the final rules are shown; #1 has the highest composite score.")
        if not tables:
            print("\nNo stocks passed the final screening rules.")
            print("This is a valid result. See the universe CSV for all stocks and their metrics.")
        for title, table in tables:
            print("\n" + title)
            print(divider)
            print(table.to_string(index=False, justify="right"))
    if out_dir is not None and metadata.get("output_labels"):
        print("\nFILES")
        for filename in metadata["output_labels"]:
            print(f"  {out_dir / filename}")
    print("\n" + divider)
    print(console_legend())
    print(border + "\n")


# =============================================================================
# CLI orchestration
# =============================================================================
def parse_date(value: str) -> date:
    """Require exactly YYYY-MM-DD in argparse rather than permissive date inference."""
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError("Use a date in YYYY-MM-DD format.") from None


def make_parser() -> argparse.ArgumentParser:
    """Expose operational options only, never frozen research parameters."""
    parser = argparse.ArgumentParser(description="SCREENING ONLY — fixed-universe momentum metrics.")
    parser.add_argument(
        "--env", type=Path, default=Path(__file__).resolve().with_name("env.txt"),
        help="Credential file (default: env.txt beside this Python file).",
    )
    parser.add_argument("--as-of", type=parse_date)
    parser.add_argument("--out", type=Path, default=Path("output"))
    parser.add_argument("--cache", type=Path, default=Path("data/cache"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--include-intraday", action="store_true")
    parser.add_argument("--log-level", type=str.upper, default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Fetch, align, screen, report, and stop; return nonzero on every abort condition."""
    args = make_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    # Do not expose HTTP internals even when application-level DEBUG is selected.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    client: UpstoxClient | None = None
    try:
        validate_config()
        now = datetime.now(IST)
        requested = args.as_of or now.date()
        if requested > now.date():
            raise ScreenerError("--as-of cannot be later than today's IST date.")
        if args.refresh and args.offline:
            raise ScreenerError("--refresh and --offline cannot be combined.")
        credentials = load_credentials(args.env)
        provisional = intraday_enabled(requested, now, args.include_intraday)
        client = UpstoxClient(credentials, args.cache, args.refresh, args.offline, now.date())
        master = client.instrument_master()
        benchmark_key = client.resolve_index(master)
        benchmark = validate_benchmark(fetch_series(client, "NIFTY500", benchmark_key, requested, provisional))
        keys = {symbol: client.resolve_equity(master, symbol) for symbol in UNIVERSE}
        raw: dict[str, pd.Series] = {}
        failures: dict[str, str] = {}
        for symbol, key in keys.items():
            if key is None:
                continue
            try:
                raw[symbol] = fetch_series(client, symbol, key, requested, provisional)
            except APIError:
                failures[symbol] = "FETCH_FAILED"
                logging.warning("Candle fetch failed for %s; recorded as FETCH_FAILED.", symbol)
        closes, diagnostics = assemble_data(raw, benchmark, keys, failures)
        try:
            require_eligible_count(closes.shape[1])
        except ScreenerError as error:
            # Emit an explicit unsuccessful run manifest, not success-like CSVs.
            empty_metrics = pd.DataFrame(columns=list(FLAG_COLUMNS))
            metadata = build_run_metadata(empty_metrics, diagnostics, benchmark, requested,
                                          provisional, sorted(client.sources), datetime.now(timezone.utc))
            metadata["status"] = "aborted"
            metadata["error"] = str(error)
            metadata["counts"]["per_flag"] = {name: None for name in FLAG_COLUMNS}
            metadata["counts"]["core"] = None
            metadata["counts"]["final"] = None
            metadata["output_labels"] = {
                f"run_{metadata['as_of_effective']}.json": {"PROVISIONAL": provisional}
            }
            atomic_text(args.out / f"run_{metadata['as_of_effective']}.json",
                        json.dumps(metadata, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            console_report(metadata, aborted=str(error), out_dir=args.out)
            return 1
        aligned_bench = benchmark.reindex(closes.index)
        metrics = compute_metrics(closes, aligned_bench)
        metadata = build_run_metadata(metrics, diagnostics, benchmark, requested,
                                      provisional, sorted(client.sources), datetime.now(timezone.utc))
        metadata["status"] = "success"
        write_outputs(args.out, metrics, diagnostics, benchmark, metadata)
        console_report(metadata, metrics, out_dir=args.out)
        return 0
    except ScreenerError as error:
        logging.error("%s", error)
        return 1
    except (OSError, ValueError, TypeError):
        logging.error("Local file/configuration/data failure; check cache schema, permissions and frozen parameters.")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
