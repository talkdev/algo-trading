#!/usr/bin/env python3
"""NSE ETF residual-momentum screener: read-only market data, local report history.

ETF adaptation of the NSE stock residual-momentum screener. The ETF universe
(111 ETFs, supplied list with exact categories and sub-categories) is embedded in
this file. Each ETF's SUB-CATEGORY from that CSV is used as its underlying
group, so several ETFs tracking the same index (e.g. eight "Nifty 50" ETFs)
are grouped only for display; no group limits apply.

Long-term model: India FF3 + RF monthly residual momentum (36 complete monthly
observations, scores t-12..t-2 and skips t-1). Every ETF is scored and screened
independently; there is no one-ETF-per-group restriction. By default every
asset class is scored (EQUITY, COMMODITY, DEBT, GLOBAL INDICES, Hybrid). Note
that the India FF3 factors were built for equities: for commodity, debt and
overseas ETFs the residual is a mechanical measure, not an economic one.
Use --long-categories to restrict the scope.

Requires Upstox token in stock-data/env.txt (or environment). Price-history
completeness, last-candle age, liquidity and residual volatility are checked.
Allocates model weights under single-ETF, liquidity and market-beta limits;
unused capital remains cash. Optional
holdings weights permit a read-only, turnover-limited paper rebalance.
Execution prices and transaction costs are not estimated.

No orders are placed and no holdings or portfolio state are changed.
Market-data requests are GETs. Factor/cache artifacts and the SQLite report
history (stock-data/etf_report_state.sqlite3) are written locally.
This is not a profit guarantee.
"""

from __future__ import annotations

import argparse
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
DEFAULT_ENV_FILE = STOCK_DATA_DIR / "env.txt"
LEGACY_ENV_FILE = PROJECT_DIR / "env.txt"
DEFAULT_CACHE_DIR = STOCK_DATA_DIR / "etf_cache"
DEFAULT_FACTOR_CACHE_DIR = STOCK_DATA_DIR / "etf_factors"
DEFAULT_STATE_DB_FILE = STOCK_DATA_DIR / "etf_report_state.sqlite3"
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
DEFAULT_CAPITAL_INR = 1_000_000.0  # illustrative ₹10 lakh
DEFAULT_MIN_TURNOVER_INR = 10_000_000.0  # ₹1 crore median/day; configurable
DEFAULT_MAX_PARTICIPATION_PCT = 2.0  # one day's hypothetical order / median daily traded value
DEFAULT_MAX_NAME_PCT = 5.0
DEFAULT_MIN_SCORE = 0.0  # only ETFs with residual score above this may enter the model
DEFAULT_MAX_REBALANCE_TURNOVER_PCT = 25.0
DEFAULT_MAX_PRICE_AGE_DAYS = 5
# Before 18:00 IST, exclude today. At/after 18:00 request today, but do not
# claim it exists unless the provider actually returns a bar.
EOD_CUTOFF_IST = clock_time(18, 0)
UPSTOX_DAILY_HISTORY_START = date(2000, 1, 1)
DEFAULT_MONTH_END_GAP_DAYS = 7  # conservative calendar proxy, not an NSE holiday calendar
KNOWN_CATEGORIES = frozenset({"EQUITY", "COMMODITY", "DEBT", "GLOBAL INDICES", "HYBRID"})
DEFAULT_LONG_CATEGORIES = ("EQUITY", "COMMODITY", "DEBT", "GLOBAL INDICES", "HYBRID")
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


# ---------------------------------------------------------------------------
# ETF UNIVERSE (embedded). Supplied list: underlying asset, symbol, category,
# sub-category (= underlying group). Edit this list to change the universe; the
# count is checked at start-up.
# ---------------------------------------------------------------------------
_ETF_ROWS: tuple[tuple[str, str, str, str], ...] = (
    ('EBBETF0430', 'Nifty BHARAT Bond', 'DEBT', 'Bond'),
    ('EQUAL200', 'BSE 200 Equal Weight', 'EQUITY', 'BSE 200 Equal Weight'),
    ('HDFCBSE500', 'HDFC S&P BSE 500 ETF', 'EQUITY', 'BSE 500'),
    ('DIVIDEND', 'BSE 500 Dividend Leaders', 'EQUITY', 'BSE 500 Dividend Leaders 50'),
    ('ICICIB22', 'S&P BSE BHARAT 22 index', 'EQUITY', 'BSE BHARAT 22'),
    ('ECAPINSURE', 'BSE Capital Markets & Insurance Total Return Index', 'EQUITY', 'BSE Capital Markets & Insurance'),
    ('MOVALUE', 'Motilal Oswal S&P BSE Enhanced Value ETF', 'EQUITY', 'BSE Enhanced Value'),
    ('MOHEALTH', 'Motilal Oswal S&P BSE Healthcare ETF', 'EQUITY', 'BSE Healthcare'),
    ('GROWWHOSPI', 'BSE Hospitals Index', 'EQUITY', 'BSE Hospitals'),
    ('DEFENCE', 'BSE India Defence', 'EQUITY', 'BSE India Defence'),
    ('MOINFRA', 'BSE India Infrastructure Total Return Index', 'EQUITY', 'BSE India Infrastructure'),
    ('SECLEDBETA', 'BSE India Sector Leaders', 'EQUITY', 'BSE India Sector Leaders TRI'),
    ('INSUREIETF', 'BSE Insurance Index', 'EQUITY', 'BSE Insurance'),
    ('LARGEMID50', 'BSE LargeMid (60:40) Stable Dividend 50 TRI (Total Return Index)', 'EQUITY', 'BSE LargeMid (60:40) Stable Dividend 50'),
    ('MOMMIDCAP', 'BSE Midcap 150 Momentum 30 TRI', 'EQUITY', 'BSE Midcap 150 Momentum 30'),
    ('MIDSELIETF', 'S&P BSE Midcap Select Index', 'EQUITY', 'BSE Midcap Select'),
    ('MIDBANKADD', 'BSE MidSmall Private Banks Index', 'EQUITY', 'BSE MidSmall Private Banks'),
    ('GROWWPOWER', 'BSE Power Index - TRI', 'EQUITY', 'BSE Power'),
    ('SBIBPB', 'BSE PSU Bank Index TRI', 'EQUITY', 'BSE PSU Bank'),
    ('MOQUALITY', 'Motilal Oswal S&P BSE Quality ETF', 'EQUITY', 'BSE Quality'),
    ('SELECTIPO', 'BSE Select IPO Index', 'EQUITY', 'BSE Select IPO'),
    ('HDFCSENSEX', 'SENSEX', 'EQUITY', 'BSE Sensex'),
    ('SNXT30BEES', 'BSE Sensex Next 30 Index', 'EQUITY', 'BSE Sensex Next 30'),
    ('BANK10ADD', 'BSE Top 10 Banks Index (TRI)', 'EQUITY', 'BSE Top 10 Banks'),
    ('HYBRIDETF', 'Nifty200 Momentum 30 Plus 8-13 yr G-Sec 50:50 Total Return Index', 'Hybrid', 'Equity/Debt'),
    ('MAFANG', 'NYSE FANG+ Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MASPTOP50', 'S&P 500 Top 50 Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MON100', 'Nasdaq100', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MONQ50', 'Nasdaq Q-50 Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MAHKTECH', 'Hang Seng TECH Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('HNGSNGBEES', 'Hang Seng Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('TATAGOLD', 'Tata Gold Exchange Traded Fund', 'COMMODITY', 'GOLD'),
    ('LTGILTBEES', 'NIFTY 8-13 yr G-Sec Index', 'DEBT', 'GSECS/GILT'),
    ('MSCI360', 'MSCI India Index', 'EQUITY', 'MSCI India'),
    ('TOP100CASE', 'Zerodha Nifty 100 ETF', 'EQUITY', 'Nifty 100'),
    ('NIFTY100EW', 'Nifty 100 Equal Weight Index', 'EQUITY', 'Nifty 100 Equal Weight'),
    ('ESG', 'NIFTY100 ESG SECTOR LEADERS', 'EQUITY', 'Nifty 100 ESG Sector Leaders'),
    ('LOWVOL1', 'Kotak Nifty 100 Low Vol 30 ETF', 'EQUITY', 'NIFTY 100 Low Volatility 30'),
    ('HDFCQUAL', 'HDFC NIFTY100 Quality 30 ETF', 'EQUITY', 'Nifty 100 Quality 30'),
    ('GROWWN200', 'Nifty 200 Index- Total Return Index', 'EQUITY', 'Nifty 200'),
    ('ALPHAETF', 'Mirae Asset Nifty 200 Alpha 30 ETF', 'EQUITY', 'Nifty 200 Alpha 30'),
    ('MOMENTUM30', 'Nifty 200 Momentum 30 Index', 'EQUITY', 'Nifty 200 Momentum 30'),
    ('NIFTYQLITY', 'Aditya Birla Sun Life Nifty 200 Quality 30 ETF', 'EQUITY', 'Nifty 200 Quality 30'),
    ('VAL30IETF', 'Nifty200 Value 30 Index', 'EQUITY', 'Nifty 200 Value 30'),
    ('NIFTYBEES', 'Nifty 50', 'EQUITY', 'Nifty 50'),
    ('SBINEQWETF', 'Nifty50 Equal Weight', 'EQUITY', 'Nifty 50 Equal Weight'),
    ('SHARIABEES', 'Shariah', 'EQUITY', 'Nifty 50 Shariah'),
    ('NV20IETF', 'Nifty50 Value 20', 'EQUITY', 'Nifty 50 Value 20'),
    ('MONIFTY500', 'Motilal Oswal Nifty 500 ETF', 'EQUITY', 'Nifty 500'),
    ('FLEXIADD', 'Nifty500 Flexicap Quality 30 TRI', 'EQUITY', 'Nifty 500 Flexicap Quality 30'),
    ('HEALTHCARE', 'Nifty 500 Healthcare Index', 'EQUITY', 'Nifty 500 Healthcare'),
    ('GROWWLOVOL', 'Nifty 500 Low Volatility 50 Index TRI', 'EQUITY', 'Nifty 500 Low Volatility 50'),
    ('MOMENTUM50', 'Nifty 500 Momentum 50 Total Return Index', 'EQUITY', 'Nifty 500 Momentum 50'),
    ('MULTICAP', 'Nifty500 Multicap 50:25:25 Index', 'EQUITY', 'Nifty 500 Multicap 50:25:25'),
    ('EMULTIMQ', 'Nifty500 Multicap Momentum Quality 50 Total Return Index', 'EQUITY', 'Nifty 500 Multicap Momentum Quality 50'),
    ('VALUE', 'Nifty 500 Value 50', 'EQUITY', 'Nifty 500 Value 50'),
    ('ALPHA', 'NIFTY Alpha 50 Index', 'EQUITY', 'NIFTY Alpha 50'),
    ('ALPL30IETF', 'Nifty Alpha Low-Volatility 30 Index', 'EQUITY', 'Nifty Alpha Low-Volatility 30'),
    ('AUTOIETF', 'Nifty Auto Index', 'EQUITY', 'Nifty Auto'),
    ('BANKNIFTY1', 'Nifty Bank', 'EQUITY', 'Nifty Bank'),
    ('MOCAPITAL', 'Nifty Capital Market Total Return Index', 'EQUITY', 'Nifty Capital Market'),
    ('CEMNTGROWW', 'Nifty Cements Index TRI', 'EQUITY', 'Nifty Cements'),
    ('CHEMICAL', 'Nifty Chemicals Index (TRI)', 'EQUITY', 'Nifty Chemicals'),
    ('COMMOIETF', 'ICICI Prudential Nifty Commodities ETF', 'EQUITY', 'Nifty Commodities'),
    ('CPSEETF', 'CPSE ETF', 'EQUITY', 'Nifty CPSE'),
    ('DIVOPPBEES', 'Nifty Dividend Opportunities 50 TRI', 'EQUITY', 'Nifty Dividend Opportunities 50'),
    ('MOENERGY', 'Nifty Energy Index', 'EQUITY', 'Nifty Energy'),
    ('EVINDIA', 'Nifty EV and New Age Automotive Total Return Index', 'EQUITY', 'Nifty EV and New Age Automotive'),
    ('BFSI', 'Nifty Financial Services Index', 'EQUITY', 'Nifty Financial Services'),
    ('FINIETF', 'ICICI Prudential Nifty Financial Services Ex-Bank ETF', 'EQUITY', 'Nifty Financial Services Ex-Bank'),
    ('FMCGIETF', 'Nifty FMCG Index', 'EQUITY', 'Nifty FMCG'),
    ('HDFCGROWTH', 'HDFC NIFTY Growth Sectors 15 ETF', 'EQUITY', 'Nifty Growth Sectors 15'),
    ('HEALTHY', 'Nifty Healthcare TRI', 'EQUITY', 'Nifty Healthcare'),
    ('CONS', 'Kotak Nifty India Consumption ETF', 'EQUITY', 'Nifty India Consumption'),
    ('MODEFENCE', 'Nifty India Defence Total Return Index', 'EQUITY', 'Nifty India Defence'),
    ('INFRA', 'Nifty India Infrastructure & Logistics', 'EQUITY', 'Nifty India Infrastructure & Logistics'),
    ('GROWWNET', 'Index', 'EQUITY', 'Nifty India Internet'),
    ('MAKEINDIA', 'Nifty India Manufacturing Total Return Index', 'EQUITY', 'Nifty India Manufacturing'),
    ('CONSUMER', 'Nifty India New AgeConsumption', 'EQUITY', 'Nifty India New Age Consumption'),
    ('GROWWRAIL', 'Nifty India Railways PSU Index', 'EQUITY', 'Nifty India Railways PSU'),
    ('MOTOUR', 'Nifty India Tourism Total Return Index', 'EQUITY', 'Nifty India Tourism'),
    ('INFRAIETF', 'ICICI Prudential Nifty Infrastructure ETF', 'EQUITY', 'Nifty Infrastructure'),
    ('ITBEES', 'Nifty IT TRI', 'EQUITY', 'Nifty IT'),
    ('ELM250', 'Nifty LargeMidcap 250 TRI', 'EQUITY', 'Nifty LargeMidcap 250'),
    ('METALIETF', 'Nifty Metal Index', 'EQUITY', 'Nifty Metal'),
    ('MOM100', 'Nifty Midcap 100', 'EQUITY', 'Nifty Midcap 100'),
    ('MIDCAPETF', 'Mirae Asset Mutual Fund - Mirae Asset Nifty Midcap 150 ETF', 'EQUITY', 'Nifty Midcap 150'),
    ('MOMIDMTM', 'Nifty Midcap 150 Momentum 50 Total Return Index', 'EQUITY', 'Nifty Midcap 150 Momentum 50'),
    ('MIDQ50ADD', 'Nifty Midcap 150 Quality 50 Index', 'EQUITY', 'Nifty Midcap 150 Quality 50'),
    ('MIDCAP', 'Nifty Midcap 50 Index', 'EQUITY', 'NIFTY Midcap 50'),
    ('MIDSMALL', 'Mirae Asset Nifty MidSmallcap400 Momentum Quality 100 ETF', 'EQUITY', 'Nifty MidSmallcap400 Momentum Quality 100'),
    ('MNC', 'Kotak Nifty MNC ETF', 'EQUITY', 'Nifty MNC'),
    ('NXT100CASE', 'Nifty Next 100', 'EQUITY', 'Nifty Next 100'),
    ('NEXT50IETF', 'Nifty Next 50', 'EQUITY', 'Nifty Next 50'),
    ('OILIETF', 'Nifty Oil & Gas Index', 'EQUITY', 'Nifty Oil & Gas'),
    ('PHARMABEES', 'Nifty Pharma TRI', 'EQUITY', 'Nifty Pharma'),
    ('PVTBANIETF', 'Nifty Private Bank Index', 'EQUITY', 'Nifty Private Bank'),
    ('ABSLPSE', 'Aditya Birla Sun Life Nifty PSE ETF', 'EQUITY', 'Nifty PSE'),
    ('PSUBANKADD', 'DSP Nifty PSU Bank ETF', 'EQUITY', 'Nifty PSU Bank'),
    ('MOREALTY', 'Motilal Oswal Nifty Realty ETF', 'EQUITY', 'Nifty Realty'),
    ('MOSERVICE', 'Nifty Services Sector Total Return Index', 'EQUITY', 'Nifty Services Sector'),
    ('HDFCSML250', 'HDFC NIFTY Smallcap 250 ETF', 'EQUITY', 'Nifty Smallcap 250'),
    ('SMALLCAP', 'Mirae Asset Nifty Smallcap 250 Momentum Quality 100 ETF', 'EQUITY', 'Nifty Smallcap 250 Momentum Quality 100'),
    ('SML100CASE', 'Nifty Smallcap 100 Index', 'EQUITY', 'Nifty Smallcap100'),
    ('TOP10ADD', 'Nifty Top 10 Equal Weight Index', 'EQUITY', 'Nifty Top 10 Equal Weight'),
    ('TOP15IETF', 'Nifty Top 15 Equal Weight Index', 'EQUITY', 'Nifty Top 15 Equal Weight'),
    ('TOP20', 'Nifty Top 20 Equal Weight', 'EQUITY', 'Nifty Top 20 Equal Weight'),
    ('AONETOTAL', 'Nifty Total Market Index', 'EQUITY', 'Nifty Total Market'),
    ('AONETMMQ50', 'Nifty Total Market Momentum Quality 50 TRI', 'EQUITY', 'Nifty Total Market Momentum Quality 50'),
    ('LIQUIDCASE', 'Zerodha Nifty 1D Rate Liquid ETF', 'DEBT', 'Overnight ETFs and Liquid ETF'),
    ('TATSILV', 'Tata Silver Exchange Traded Fund', 'COMMODITY', 'SILVER'),
)


@dataclass(frozen=True)
class EtfEntry:
    symbol: str
    name: str
    category: str   # EQUITY, COMMODITY, DEBT, GLOBAL INDICES, Hybrid
    underlying: str  # CSV SUB-CATEGORY; the diversification group

    @property
    def group(self) -> str:
        return self.underlying.strip().upper()


ETF_UNIVERSE: tuple[EtfEntry, ...] = tuple(EtfEntry(*row) for row in _ETF_ROWS)
ETF_UNIVERSE_COUNT = 111


def validate_etf_universe() -> tuple[str, ...]:
    if len(ETF_UNIVERSE) != ETF_UNIVERSE_COUNT:
        raise ScreenerError(f"Embedded ETF universe has {len(ETF_UNIVERSE)} rows; expected {ETF_UNIVERSE_COUNT}.")
    symbols = tuple(e.symbol.strip().upper() for e in ETF_UNIVERSE)
    if len(set(symbols)) != len(symbols):
        raise ScreenerError("Embedded ETF universe contains duplicate symbols.")
    for e in ETF_UNIVERSE:
        if not e.symbol.strip() or not e.name.strip() or not e.underlying.strip():
            raise ScreenerError(f"Blank field in embedded ETF entry {e.symbol!r}.")
        if e.category.strip().upper() not in KNOWN_CATEGORIES:
            raise ScreenerError(f"Unknown category {e.category!r} for {e.symbol}.")
    return symbols


def status(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


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
    sector: str = ""  # underlying group
    median_turnover_inr: float = float("nan")


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
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json", "User-Agent": "etf-residual-momentum-screen/1.0"})
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

    def get_nse_etf_instruments(self, symbols: Sequence[str]) -> list[Instrument]:
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
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("segment") != "NSE_EQ" or row.get("exchange") != "NSE":
                continue
            symbol = str(row.get("trading_symbol", "")).strip().upper()
            if symbol not in requested:
                continue
            all_exact.setdefault(symbol, []).append(row)
            # NSE ETFs are listed in the master as instrument_type EQ / security NORMAL
            # (verified for all universe symbols). BE (trade-to-trade) is excluded.
            if str(row.get("instrument_type", "")).upper() != "EQ":
                continue
            if str(row.get("security_type", "NORMAL")).upper() not in ("NORMAL", ""):
                continue
            key = str(row.get("instrument_key", "")).strip()
            isin = str(row.get("isin", "")).strip()
            if key and isin and key.startswith("NSE_EQ|"):
                by_symbol.setdefault(symbol, []).append(Instrument(symbol, key, isin))
        self.instrument_diagnostics = {}
        for symbol in symbols:
            matches = by_symbol.get(symbol, [])
            if len(matches) == 1:
                continue
            if len(matches) > 1:
                reason = "ambiguous: multiple active ETF/EQ records; skipped"
            elif not all_exact.get(symbol):
                reason = "no exact NSE_EQ ticker in today's BOD master (possibly renamed/delisted; verify manually)"
            else:
                types = sorted({str(r.get("instrument_type", "missing")) for r in all_exact[symbol]})
                securities = sorted({str(r.get("security_type", "missing")) for r in all_exact[symbol]})
                reason = f"exact ticker present but not eligible (series={types}, security={securities})"
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

    def get_daily_candles(self, instrument: Instrument, start: date, end: date) -> tuple[pd.DataFrame, bool]:
        path = self._price_cache_path(instrument)
        metadata_path = path.with_suffix(path.suffix + ".meta.json")
        if not self.refresh and self.ttl_seconds > 0 and path.exists() and metadata_path.exists():
            try:
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (
                    time.time() - path.stat().st_mtime <= self.ttl_seconds
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
    status(f"[Prices] Checking cache coverage or requesting {total} ETF daily OHLCV histories "
           f"({start}..{end}); {workers} workers.")
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


def create_report_tables(connection: sqlite3.Connection) -> None:
    """Create the long-term schema, migrating any legacy long+short tables.

    Legacy tables (from the earlier version) carry a `screen` column and a
    `short_asof_session` column. Their LONG rows are copied into the new schema;
    SHORT rows are discarded because the short screen has been removed. Runs
    on the caller's open transaction.
    """
    legacy_cols = {row[1] for row in connection.execute("PRAGMA table_info(discoveries)")}
    legacy = "screen" in legacy_cols
    if legacy:
        connection.execute("DROP INDEX IF EXISTS ix_report_members_symbol")
        connection.execute("ALTER TABLE report_runs RENAME TO legacy_report_runs")
        connection.execute("ALTER TABLE discoveries RENAME TO legacy_discoveries")
        connection.execute("ALTER TABLE report_members RENAME TO legacy_report_members")
    # Avoid executescript: it would commit a pending transaction implicitly.
    for ddl in (
        """CREATE TABLE IF NOT EXISTS report_runs (
            id INTEGER PRIMARY KEY,
            started_ist TEXT NOT NULL,
            factor_asof_month TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS discoveries (
            symbol TEXT PRIMARY KEY,
            first_seen_ist TEXT NOT NULL,
            first_run_id INTEGER NOT NULL REFERENCES report_runs(id)
        )""",
        """CREATE TABLE IF NOT EXISTS report_members (
            run_id INTEGER NOT NULL REFERENCES report_runs(id),
            symbol TEXT NOT NULL,
            displayed_rank INTEGER NOT NULL,
            PRIMARY KEY (run_id, symbol)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_report_members_symbol ON report_members(symbol, run_id)",
    ):
        connection.execute(ddl)
    if legacy:
        connection.execute(
            "INSERT INTO report_runs(id, started_ist, factor_asof_month) "
            "SELECT id, started_ist, factor_asof_month FROM legacy_report_runs")
        connection.execute(
            "INSERT INTO discoveries(symbol, first_seen_ist, first_run_id) "
            "SELECT symbol, first_seen_ist, first_run_id FROM legacy_discoveries WHERE screen = 'long'")
        connection.execute(
            "INSERT INTO report_members(run_id, symbol, displayed_rank) "
            "SELECT run_id, symbol, displayed_rank FROM legacy_report_members WHERE screen = 'long'")
        connection.execute("DROP TABLE legacy_report_members")
        connection.execute("DROP TABLE legacy_discoveries")
        connection.execute("DROP TABLE legacy_report_runs")


def record_report_discoveries(
    path: Path, started_ist: datetime, factor_asof: pd.Period,
    long_members: Sequence[PortfolioMember],
) -> tuple[dict[str, DiscoveryStamp], int]:
    """Atomically save the long-term list and first-ever dates.

    Every successful run gets a membership snapshot. A returning ETF retains
    its first-ever date even after intervening runs in which it was absent.
    Nothing is written until the report has been calculated successfully.
    """
    symbols = [m.stock.symbol for m in long_members]
    if len(set(symbols)) != len(symbols):
        raise ScreenerError("Duplicate selected long-term ETF symbol; discovery state not written.")
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
                "INSERT INTO report_runs(started_ist, factor_asof_month) VALUES (?, ?)",
                (started_ist.isoformat(timespec="seconds"), str(factor_asof)),
            )
            run_id = int(cursor.lastrowid)
            stamps: dict[str, DiscoveryStamp] = {}
            for member in long_members:
                symbol, rank = member.stock.symbol, member.rank
                added = connection.execute(
                    "INSERT OR IGNORE INTO discoveries(symbol, first_seen_ist, first_run_id) "
                    "VALUES (?, ?, ?)", (symbol, run_date, run_id),
                ).rowcount == 1
                saved = connection.execute(
                    "SELECT first_seen_ist FROM discoveries WHERE symbol=?", (symbol,),
                ).fetchone()
                if saved is None:
                    raise ScreenerError(f"Could not read discovery date for {symbol}.")
                connection.execute(
                    "INSERT INTO report_members(run_id, symbol, displayed_rank) VALUES (?, ?, ?)",
                    (run_id, symbol, rank),
                )
                stamps[symbol] = DiscoveryStamp(date.fromisoformat(saved[0]), added)
        return stamps, run_id
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise ScreenerError(f"Could not save report history to SQLite {path}: {exc}") from None
    finally:
        if connection is not None:
            connection.close()


def discovery_display(stamp: DiscoveryStamp) -> tuple[str, str]:
    return stamp.first_seen.strftime("%d-%m-%y"), ("NEW" if stamp.new_this_run else "")


def short_name(name: str, width: int = 36) -> str:
    return name if len(name) <= width else name[: width - 1] + "…"


def make_ranked_universe(
    instruments: Sequence[Instrument], prices: Mapping[str, pd.DataFrame], factors: pd.DataFrame,
    as_of: pd.Period, volatility_cap: float, meta: Mapping[str, EtfEntry], today: date,
    min_turnover_inr: float, max_price_age_days: int,
    long_categories: frozenset[str],
    month_end_gap_days: int = DEFAULT_MONTH_END_GAP_DAYS,
) -> tuple[list[RankedStock], dict[str, int]]:
    candidates: list[RankedStock] = []
    exclusions: dict[str, int] = {}

    def reject(reason: str) -> None:
        exclusions[reason] = exclusions.get(reason, 0) + 1

    for inst in instruments:
        entry = meta[inst.symbol]
        if entry.category.strip().upper() not in long_categories:
            reject(f"outside long-term FF3 scope (category {entry.category}; see --long-categories)")
            continue
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
            entry.group, turnover,
        ))
    return sorted(candidates, key=lambda stock: (-stock.result.score, stock.symbol)), exclusions


def select_top_candidates(ranked: Sequence[RankedStock], size: int) -> list[RankedStock]:
    """Take the highest-scoring eligible ETFs; no underlying-group limits."""
    return list(ranked[:size])


def constrained_weights(
    selected: Sequence[RankedStock], capital_inr: float, participation: float,
    name_cap: float, beta_cap: float,
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
        beta_used = sum(max(0.0, by_symbol[s].result.beta_market) * w for s, w in weights.items())
        active = [
            s for s in selected
            if name_limits[s.symbol] - weights[s.symbol] > tolerance
            and (s.result.beta_market <= 0 or beta_cap - beta_used > tolerance)
        ]
        if not active or invested >= 1 - tolerance:
            break
        base_sum = sum(base[s.symbol] for s in active)
        proportions = {s.symbol: base[s.symbol] / base_sum for s in active}
        step = 1 - invested
        for s in active:
            step = min(step, (name_limits[s.symbol] - weights[s.symbol]) / proportions[s.symbol])
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
    name_cap: float, beta_cap: float, holdings: Optional[Mapping[str, float]],
    max_rebalance_turnover: float, min_score: float = DEFAULT_MIN_SCORE,
) -> PortfolioPlan:
    # Negative residual momentum is never bought: the ranking is kept for display,
    # but only ETFs whose score is above min_score can be selected.
    selected = select_top_candidates([s for s in ranked if s.result.score > min_score], size)
    model = constrained_weights(selected, capital_inr, participation, name_cap, beta_cap)
    rank = {s.symbol: i for i, s in enumerate(ranked, 1)}
    stocks = {s.symbol: s for s in ranked}
    selected_iv = sum(1 / s.result.residual_volatility for s in selected)
    if holdings is not None:
        missing = sorted(set(holdings) - stocks.keys())
        if missing:
            raise ScreenerError(
                "Cannot safely stage a rebalance: held ETFs lack current eligible scores/betas "
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
            warnings.append("a staged holding remains above the single-ETF cap")
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
    name_pct: float, beta_cap: float,
    holdings: Optional[Mapping[str, float]], max_rebalance_pct: float,
    names: Mapping[str, str], categories: Mapping[str, str],
    long_categories: Sequence[str],
    month_end_gap_days: int = DEFAULT_MONTH_END_GAP_DAYS, requested_start: Optional[date] = None,
    requested_end: Optional[date] = None,
    discoveries: Optional[Mapping[str, DiscoveryStamp]] = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> None:
    def pct(value: float) -> str:
        return f"{100 * value:.2f}%"

    print("\nLONG-TERM NSE ETF RESIDUAL-MOMENTUM EOD MODEL (MONTHLY) — NO ORDERS\n")
    print("1) WHAT WAS SCORED")
    print(f"  Monthly ETF/factor data through {as_of}; factor lag: {factor_lag} completed month(s).")
    print(f"  Factor source: {factor_source}")
    print(f"  Long-term scope: categories {', '.join(long_categories)} only (India FF3 equity-factor model).")
    print(f"  {requested_count} ETFs in embedded universe → {mapped_count} NSE ETF mappings → {len(ranked)} passed enabled checks.")
    if requested_start and requested_end:
        print(f"  Requested daily OHLCV: {requested_start} through {requested_end}, inclusive.")
        print("  Today is eligible only from 18:00 IST; the last available candle may be earlier.")
        print("  A returned same-day candle's finality is not independently verified; inspect its reported session date.")
    if ranked:
        print(f"  Actual latest daily close among eligible ETFs: {min(s.latest_session for s in ranked)} to "
              f"{max(s.latest_session for s in ranked)}; not every ETF traded on the same last date.")
    print(f"  All 37 necessary monthly closes must fall within {month_end_gap_days} days of their calendar month-end.")
    print("  This is a calendar proxy, not an official NSE holiday-calendar check.")
    print("  Score = 11 months' unusual returns added together / their monthly variation.")
    print("  The last month is skipped. A high score is NOT a prediction or a buy signal.")
    print("  ETF CAVEAT: NAV, distributions, expense ratio, tracking error and premium/discount are not modelled.")
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
    print(f"  Each model position ≤ {participation_pct:g}% of its ETF's median daily traded value (₹{capital:,.0f} model capital).")
    print("  EOD data does not establish a future execution price or fill.")
    print("\n3) HOW THE MODEL MONEY IS DIVIDED")
    eligible_n = sum(1 for s in ranked if s.result.score > min_score)
    print(f"  Choose up to {size} by score from the {eligible_n} ETFs with score above {min_score:g}")
    print(f"  (of {len(ranked)} scored). ETFs with score ≤ {min_score:g} are shown in the table but never bought.")
    print("  IV starting weights = bigger shares for ETFs with steadier unusual returns.")
    print(f"  Model cap: each ETF ≤ {name_pct:g}% of capital.")
    print(f"              conservative positive-beta exposure ≤ {beta_cap:g} per ₹1 TOTAL model capital.")
    print("  Unused money stays as CASH: limits are not relaxed just to invest 100%.")
    print(f"  Model invested {pct(1-plan.model_cash)}; cash {pct(plan.model_cash)}; market beta exposure {plan.model_beta:.3f}.")
    if plan.model_cash > 0.005:
        print("  Why cash? Too few diverse ETFs, per-ETF limits, liquidity capacity, and/or beta limit.")
    if holdings is None:
        print("  Initial model only: no holdings supplied; no monthly turnover comparison or rebalance cap applied.")
        print(f"  Initial hypothetical purchases total {pct(plan.staged_turnover)} of capital.")
    else:
        print(f"  Existing weights supplied. Full model change would trade {pct(plan.desired_turnover or 0)} of capital.")
        print(f"  Staged paper change: trade {pct(plan.staged_turnover)} (≤ {max_rebalance_pct:g}% monthly rebalance budget)")
        print(f"  and ≤ {participation_pct:g}% of each ETF's daily traded value per order; ")
        print(f"  {pct(plan.stage_fraction)} of the desired changes this run. No holdings are modified.")
        print(f"  After staging: cash {pct(plan.staged_cash)}, market beta exposure {plan.staged_beta:.3f}.")
        if plan.warning:
            print(f"  MANUAL REVIEW: {plan.warning}; staged holdings are NOT within all risk limits.")
    print("  Trading costs (including brokerage, taxes, STT/stamp, bid-ask and execution effects) are NOT estimated.")
    if not plan.members:
        print("\nNo model ETFs passed the data, liquidity and score requirements.")
    else:
        print("\n4) ETF-BY-ETF MODEL (score rank is NOT an instruction to buy)")
        print("  Band describes RELATIVE rank: even a TOP 10% ETF can have a negative score.")
        print("  Score sign: + means positive past residual score, − means negative; neither predicts returns.")
        print("  IV start is before caps; Model is after caps; Staged is after any holdings-based trade limits.")
        print("  Discovered = first-ever appearance in this LONG model list on the IST run date; "
              "NEW marks first inclusion this run. Re-entry keeps the first date.")
        headers = ("Rank", "Band", "Sign", "Symbol", "Name", "Discovered", "New", "Group", "Score", "Vol/mo", "Beta",
                   "Daily ₹Cr", "IV start", "Model", "Staged", "Model ₹")
        rows: list[tuple[str, ...]] = []
        for member in plan.members:
            stock = member.stock
            if member.percentile_rank <= 10:
                band = "TOP 10%"
            elif member.percentile_rank <= 50:
                band = "UPPER HALF"
            else:
                band = "LOWER HALF"
            stamp = discoveries[stock.symbol] if discoveries is not None else None
            discovered, new_flag = discovery_display(stamp) if stamp else ("n/a", "")
            rows.append((
                str(member.rank), band, "+" if stock.result.score > 0 else ("−" if stock.result.score < 0 else "0"),
                stock.symbol, short_name(names.get(stock.symbol, ""), 30), discovered, new_flag, stock.sector,
                f"{stock.result.score:+.2f}", f"{100*stock.result.residual_volatility:.2f}%",
                f"{stock.result.beta_market:+.2f}", f"{stock.median_turnover_inr/10_000_000:.1f}",
                pct(member.inverse_vol_weight), pct(member.model_weight), pct(member.staged_weight),
                f"₹{member.model_weight*capital:,.0f}",
            ))
        widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
        print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
        print("  " + "  ".join("-" * w for w in widths))
        for row in rows:
            print("  " + "  ".join(x.ljust(w) for x, w in zip(row, widths)))
        if holdings is not None:
            carryovers = {s: w for s, w in plan.staged_weights.items() if s not in {m.stock.symbol for m in plan.members}}
            if carryovers:
                print("  Still held outside the new model because trading is staged:")
                for s, w in sorted(carryovers.items()):
                    print(f"    {s}: {pct(w)} (new model target 0%; manual review if necessary)")
        print("\n  MODEL UNDERLYING GROUPS (cash is shown separately)")
        groups: dict[str, float] = {}
        for m in plan.members:
            groups[m.stock.sector] = groups.get(m.stock.sector, 0) + m.model_weight
        for group, weight in sorted(groups.items(), key=lambda item: (-item[1], item[0])):
            if weight > 1e-10:
                print(f"    {group}: {pct(weight)}")
        print(f"    CASH: {pct(plan.model_cash)}")
    if exclusions:
        print("\n5) WHY ETFs WERE LEFT OUT")
        for reason, count in sorted(exclusions.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {count} — {reason}")
    if price_failures:
        examples = list(price_failures.items())[:12]
        print(f"  {len(price_failures)} price-data request failures; examples: " +
              ", ".join(f"{s} ({reason})" for s, reason in examples))
    print("\nThis is NOT an optimized/proven profitable strategy. It produces no orders or trade instructions.\n")


def print_universe_summary(symbols: Sequence[str], categories: Mapping[str, str]) -> None:
    counts: dict[str, int] = {}
    for symbol in symbols:
        counts[categories[symbol]] = counts.get(categories[symbol], 0) + 1
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda item: -item[1]))
    print(f"ETF universe: {len(symbols)} symbols ({summary}).")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only NSE ETF residual-momentum research screen; no trading.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--state-db-file", type=Path, default=DEFAULT_STATE_DB_FILE,
                        help="SQLite ETF first-discovery and per-run selected-list history "
                             "(default stock-data/etf_report_state.sqlite3)")
    parser.add_argument("--factor-file", type=Path, default=None, help="local monthly India factor CSV")
    parser.add_argument("--factor-provider", choices=("scdlds", "iima"), default=None)
    parser.add_argument("--factor-units", choices=("percent", "decimal"), default=None)
    parser.add_argument("--long-categories", default=",".join(DEFAULT_LONG_CATEGORIES),
                        help="comma-separated CSV categories scored by the long-term model "
                             "(default: all five; options: EQUITY, COMMODITY, DEBT, "
                             "'GLOBAL INDICES', Hybrid)")
    parser.add_argument("--max-residual-vol-pct", type=float, default=None, help="default 15%%")
    parser.add_argument("--portfolio-size", type=int, default=None, help="default 50 for long-term model")
    parser.add_argument("--holdings-file", type=Path, help="optional CSV symbol,weight_pct for paper rebalance only")
    parser.add_argument("--capital-inr", type=float, default=DEFAULT_CAPITAL_INR, help="model capital, default ₹10 lakh")
    parser.add_argument("--min-turnover-inr", type=float, default=DEFAULT_MIN_TURNOVER_INR,
                        help="minimum 63-session median traded value per ETF, default ₹1 crore/day")
    parser.add_argument("--max-price-age-days", type=int, default=DEFAULT_MAX_PRICE_AGE_DAYS)
    parser.add_argument("--month-end-max-gap-days", type=int, default=DEFAULT_MONTH_END_GAP_DAYS,
                        help="last close within this many calendar days of each month-end, default 7")
    parser.add_argument("--max-participation-pct", type=float, default=DEFAULT_MAX_PARTICIPATION_PCT,
                        help="max hypothetical per-ETF order / median daily traded value, default 2%%")
    parser.add_argument("--max-name-pct", type=float, default=DEFAULT_MAX_NAME_PCT, help="default 5%% of capital")
    parser.add_argument("--max-beta", type=float, default=BETA_UPPER_BOUND, help="positive-beta budget, default 1.3")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                        help="only ETFs with residual score above this may enter the model, default 0")
    parser.add_argument("--max-rebalance-turnover-pct", type=float,
                        default=DEFAULT_MAX_REBALANCE_TURNOVER_PCT,
                        help="only when holdings supplied, default 25%% of capital")
    parser.add_argument("--max-factor-lag-months", type=int, default=2,
                        help="stop if factor data is older than this, default 2")
    parser.add_argument("--allow-stale-factors", action="store_true",
                        help="research-only override of factor-lag safety stop")
    parser.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS, help="1..8, default 6")
    parser.add_argument("--refresh", action="store_true", help="ignore cached candles")
    parser.add_argument("--list-universe", action="store_true",
                        help="print the embedded ETF universe (symbol, category, group, name) and exit")
    return parser


def parse_long_categories(text: str) -> frozenset[str]:
    cats = {c.strip().upper() for c in text.split(",") if c.strip()}
    if not cats:
        raise ScreenerError("--long-categories must name at least one category.")
    unknown = cats - KNOWN_CATEGORIES
    if unknown:
        raise ScreenerError(f"Unknown --long-categories value(s): {', '.join(sorted(unknown))}.")
    return frozenset(cats)


def run_screen(args: argparse.Namespace) -> int:
    symbols = validate_etf_universe()
    meta = {e.symbol.strip().upper(): e for e in ETF_UNIVERSE}
    names = {s: meta[s].name for s in symbols}
    categories = {s: meta[s].category.strip().upper() for s in symbols}
    long_categories = parse_long_categories(args.long_categories)
    now_ist = datetime.now(INDIA_TZ)
    today = now_ist.date()
    holdings = read_holdings_weights(args.holdings_file)
    for name, value in (
        ("--capital-inr", args.capital_inr), ("--min-turnover-inr", args.min_turnover_inr),
        ("--max-price-age-days", args.max_price_age_days),
        ("--max-participation-pct", args.max_participation_pct),
        ("--max-name-pct", args.max_name_pct),
        ("--month-end-max-gap-days", args.month_end_max_gap_days),
        ("--max-beta", args.max_beta), ("--max-rebalance-turnover-pct", args.max_rebalance_turnover_pct),
    ):
        if not np.isfinite(value) or value < 0:
            raise ScreenerError(f"{name} must be finite and nonnegative.")
    if (args.capital_inr <= 0 or args.max_participation_pct <= 0 or args.max_name_pct <= 0
        or args.max_beta <= 0
        or args.max_price_age_days < 1 or args.max_factor_lag_months < 0
        or args.month_end_max_gap_days > 15
        or args.max_name_pct > 100
        or args.max_participation_pct > 100 or args.max_rebalance_turnover_pct > 200):
        raise ScreenerError("Invalid capital, age, beta, percentage, or factor-lag limit.")
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
    if not np.isfinite(args.min_score):
        raise ScreenerError("--min-score must be finite.")
    if not np.isfinite(cap_pct) or not 0 < cap_pct <= 100:
        raise ScreenerError("Residual-volatility cap must be finite and in (0,100] percent.")
    size = args.portfolio_size
    if size is None:
        size = optional_int(env_value(values, "ETF_PORTFOLIO_SIZE", env_value(values, "PORTFOLIO_SIZE")),
                            DEFAULT_PORTFOLIO_SIZE, "ETF_PORTFOLIO_SIZE")
    cache_hours = optional_float(env_value(values, "PRICE_CACHE_TTL_HOURS"), DEFAULT_CACHE_TTL_HOURS,
                                 "PRICE_CACHE_TTL_HOURS")
    if size < 1 or not 1 <= args.workers <= 8 or cache_hours < 0:
        raise ScreenerError("Portfolio size must be positive, workers 1..8, and cache TTL nonnegative.")

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

    status("[2/4] Resolving ETF symbols from the Upstox NSE instrument master...")
    client = UpstoxClient(token.strip(), DEFAULT_CACHE_DIR, cache_hours * 3600, args.refresh)
    instruments = client.get_nse_etf_instruments(symbols)
    if not instruments:
        raise ScreenerError("No embedded ETF symbols mapped uniquely to current NSE_EQ instruments.")
    mapped = {inst.symbol for inst in instruments}
    unmapped = [s for s in symbols if s not in mapped]
    status(f"[2/4] Resolved {len(instruments)} of {len(symbols)} ETFs.")
    if unmapped:
        for symbol in unmapped[:20]:
            status(f"[Mapping] {symbol}: {client.instrument_diagnostics.get(symbol, 'unmapped (inspect master)')}")
        if len(unmapped) > 20:
            status(f"[Mapping] ...and {len(unmapped) - 20} more unmapped; no ETF is silently substituted.")

    # 36 monthly returns ending as_of need 37 closes, starting as_of−36.
    first_price_month = as_of - REGRESSION_MONTHS
    start_date = first_price_month.start_time.date()
    end_date = eligible_eod_date(now_ist)
    if end_date < start_date:
        raise DataQualityError("No completed calendar day is available after the first needed price month.")
    status(f"[3/4] Requesting daily OHLCV {start_date}..{end_date} inclusive; "
           "today is included only at/after 18:00 IST. Actual first/last sessions vary by ETF.")
    prices, failures, hits = fetch_prices(instruments, client, start_date, end_date, args.workers)
    status(f"[4/4] Scoring and checking {len(prices)} ETF histories...")
    ranked, exclusions = make_ranked_universe(
        instruments, prices, factors, as_of, cap_pct / 100, meta, today,
        args.min_turnover_inr, args.max_price_age_days, long_categories, args.month_end_max_gap_days,
    )
    plan = build_portfolio_plan(
        ranked, size, args.capital_inr, args.max_participation_pct / 100,
        args.max_name_pct / 100, args.max_beta,
        holdings, args.max_rebalance_turnover_pct / 100, args.min_score,
    )
    long_discoveries, report_id = record_report_discoveries(
        args.state_db_file, now_ist, as_of, plan.members,
    )
    status(f"[State] Committed report #{report_id} to {args.state_db_file} "
           f"(first-ever new: {sum(v.new_this_run for v in long_discoveries.values())}).")
    print_universe_summary(symbols, categories)
    print_screen(
        plan, ranked, len(symbols), len(instruments), as_of, lag, source, cap_pct / 100, size,
        failures, exclusions, hits, args.capital_inr, args.min_turnover_inr,
        args.max_price_age_days, args.max_participation_pct, args.max_name_pct,
        args.max_beta,
        holdings, args.max_rebalance_turnover_pct, names, categories, sorted(long_categories),
        args.month_end_max_gap_days, start_date, end_date, long_discoveries,
        min_score=args.min_score,
    )
    return 0


def print_universe_list() -> None:
    print(f"{'SYMBOL':<14} {'CATEGORY':<14} {'GROUP':<40} NAME")
    for e in ETF_UNIVERSE:
        print(f"{e.symbol:<14} {e.category:<14} {e.group[:40]:<40} {e.name}")
    print(f"\n{len(ETF_UNIVERSE)} ETFs")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        if args.list_universe:
            validate_etf_universe()
            print_universe_list()
            return 0
        return run_screen(args)
    except ScreenerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; no orders were sent.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
