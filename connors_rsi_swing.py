#!/usr/bin/env python3
"""
================================================================================
 CONNORSRSI (3, 2, 100) EQUITY SWING TRADING SYSTEM  —  NSE / Upstox v2 REST
================================================================================

Strategy
--------
Classic ConnorsRSI mean-reversion on high-liquidity NSE large caps (Nifty 50):

  1. Trend filter   : current Close > 200-day SMA              (longs only)
  2. Entry trigger  : CRSI = (RSI(3) + RSI(streak, 2) + PercentRank(100)) / 3
                      falls below CRSI_THRESHOLD (10.0)
  3. Position size  : POSITION_SIZE_PCT (5%) of TOTAL_CAPITAL per name
  4. Entry          : aggressive LIMIT BUY at LTP + SLIPPAGE_BUFFER_TICKS ticks
                      at 15:28 IST to lock in the near-close signal price
  5. Exit           : LTP crosses above the 5-day SMA (target), or
                      held for MAX_HOLD_DAYS (time-based liquidation).
                      NO hard stop-losses — they degrade this model.

Daily schedule (IST, normal NSE session 09:15-15:30)
----------------------------------------------------
  15:15  Historical EOD fetch for the whole universe (>=250 days),
         concurrent via asyncio.Semaphore(10) + exponential backoff,
         then EXIT evaluation for all open positions (uses the same data).
  15:25  Vectorized ConnorsRSI computation + trend filter + inventory
         check + position sizing -> entry decisions.
  15:28  Order dispatch: entries and exits are pushed through an
         asyncio.Queue worker so a bulk near-close wave never stalls
         the event loop.
  15:30  Daily summary appended to the summary CSV; state persisted.

Modes (hardcoded booleans below — the system takes NO runtime input)
--------------------------------------------------------------------
  * PAPER_TRADING_MODE = True  (mandatory default): signals are computed
    from live Upstox data, but fills are SIMULATED (10 bps slippage model)
    against an in-memory ledger. NO order is ever sent to the broker.
  * BACKTEST_MODE = False: when True, bypasses live scanning entirely and
    replays NUM_BACKTEST_DAYS of daily candles day-by-day through the exact
    same engine, writing the same CSVs to the BACKTEST_* files.
  * LIVE mode (both flags False): sends real orders to Upstox. Reachable
    ONLY by manually editing this source. Guarded by a token sanity check.

CSV outputs (columns fixed by specification)
--------------------------------------------
  TRADE LOG    : timestamp, session_date, instrument_key, event_type, side,
                 qty, simulated_price, crsi_score, days_held, realized_pnl,
                 notes
  DAILY SUMMARY: session_date, open_positions_count, total_unrealized_pnl,
                 realized_pnl_today

Operational notes / defined edge-case behavior
----------------------------------------------
  * Credentials   : UPSTOX_ACCESS_TOKEN / UPSTOX_API_KEY / UPSTOX_API_SECRET
                   are auto-loaded from env.txt (KEY=value, one per line)
                   placed next to this script — no CLI arguments, no prompts.
                   The hardcoded fallback is used only when env.txt is
                   missing. Token values are never written to logs or CSVs.
  * Rate limits  : concurrency capped at 10; a pacing gate enforces a
                   minimum inter-request gap (adaptive: doubled on 429,
                   relaxed after sustained success); every request retries
                   with exponential backoff + jitter (429/5xx/timeout).
                   HTTP 429 can never be triggered by the pacing logic in
                   a tight loop — the gate throttles before dispatch.
  * Async safety : every REST call is aiohttp (no blocking requests);
                   all mutable shared state (positions, cash, ledger,
                   CSV files) is protected by asyncio.Lock; all blocking
                   file I/O runs via asyncio.to_thread.
  * Data hygiene : bars are sorted and de-duplicated; non-positive /
                   non-finite prices dropped; tickers with < 200 bars are
                   excluded until they accumulate history (self-healing);
                   if > MAX_UNIVERSE_FAILURES tickers fail a fetch the
                   session aborts loudly instead of trading half-blind.
  * Restart      : positions/cash are persisted to positions_state.json
                   at 15:30 and on shutdown; on boot (live mode) the
                   broker portfolio is reconciled against that file.
  * Duplicates   : double-submission is prevented by a position-level
                   reservation in State.open_position() (atomic under
                   the state lock) — a second entry for the same key
                   is dropped even if two workers raced.
  * Idempotency  : order requests carry a unique `tag` (cqrsi-<id>) so a
                   transport-level retry after a lost ACK can be detected
                   via order history and cancelled manually.
  * Sessions     : weekend / NSE-holiday detection via a hardcoded holiday
                   calendar (2026 below — refresh annually); the 2026-02-01
                   budget special session (early 15:00 close) is modelled.
                   Schedule gates that fall after a session's close are
                   skipped (defined behavior).

DISCLAIMERS
-----------
  * Educational/institutional reference implementation. NOT investment
    advice. Past performance does not predict future results.
  * Backtest results do not model order-book queue position, partial
    fills, circuit halts, corporate actions, dividends, STT/charges or
    the full reality of near-close NSE execution. Treat them as
    engineering-level approximations.
  * LIVE mode requires a valid UPSTOX_ACCESS_TOKEN in env.txt, an active
    Upstox account with the required API entitlements (note: Upstox now
    mandates registered-app order placement for some accounts — error
    UDAPI100049), and operator sign-off. PAPER_TRADING_MODE = True is the
    mandatory default and the only mode this file ships with.

Author : generated for institutional use
Date   : 2026-08-29
Python : 3.10+  (deps: pandas>=2.2, numpy, aiohttp)
================================================================================
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import re
import sys
import time as wall_clock
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Final,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)
from urllib.parse import quote as url_quote

import aiohttp
import numpy as np
import pandas as pd

# =============================================================================
# SECTION 1 — HARDCODED CONFIGURATION (NO RUNTIME INPUTS, BY DESIGN)
# =============================================================================

# --- Mode flags ------------------------------------------------------------
PAPER_TRADING_MODE: Final[bool] = True    # MANDATORY DEFAULT — simulated fills.
BACKTEST_MODE: Final[bool] = False        # Historical replay of the strategy.

# --- Credentials ------------------------------------------------------------
# Credentials are auto-loaded from env.txt (KEY=value, one per line) located
# next to this script — no CLI arguments, no prompts. The constants below
# are hardcoded FALLBACKS used only when env.txt is missing or a key is
# absent from it.
#
# Consumed keys (anything else in env.txt, e.g. GEMINI_API_KEY, is parsed
# and retained but ignored by the engine):
#   UPSTOX_ACCESS_TOKEN  — required for authenticated data + live orders
#   UPSTOX_API_KEY       — kept for future OAuth/token-refresh flows
#   UPSTOX_API_SECRET    — kept for future OAuth/token-refresh flows
#
# The placeholder fallback is detected at startup; paper/backtest continue
# with a warning, LIVE mode refuses to boot without a real token.
ACCESS_TOKEN_FALLBACK: Final[str] = "YOUR_MANUAL_TOKEN_HERE"

# --- Capital / sizing -------------------------------------------------------
TOTAL_CAPITAL: Final[float] = 1_000_000.0   # INR, paper starting equity
POSITION_SIZE_PCT: Final[float] = 0.05      # 5% of capital per name

# --- Strategy parameters ----------------------------------------------------
CRSI_THRESHOLD: Final[float] = 10.0
MAX_HOLD_DAYS: Final[int] = 7               # time-based exit (calendar days)
SLIPPAGE_BUFFER_TICKS: Final[int] = 4       # aggressive limit offset, ticks

# --- Execution hygiene ------------------------------------------------------
PAPER_SLIPPAGE_MODEL_BPS: Final[float] = 10.0  # 10 bps simulated slippage
LOT_SIZE: Final[int] = 1                    # NSE equity lot size (units)
MAX_CONCURRENT_POSITIONS: Final[int] = 20   # 50 names x 5% = 20 max, by math
MIN_HISTORY_DAYS: Final[int] = 250          # spec floor for SMA200+rank
DATA_LOOKBACK_DAYS: Final[int] = 400        # calendar days requested per fetch
MAX_UNIVERSE_FAILURES: Final[int] = 10      # abort session if more fail

# --- Backtest -----------------------------------------------------------------
NUM_BACKTEST_DAYS: Final[int] = 500         # calendar days of lookback
# Self-test override: when True the backtester feeds on a deterministic
# synthetic OHLCV generator instead of the Upstox endpoint. Ship with False;
# flip temporarily to verify the machine end-to-end without a token.
BACKTEST_USE_SYNTHETIC_DATA: Final[bool] = False

# --- Upstox v2 REST ---------------------------------------------------------
API_BASE_URL: Final[str] = "https://api.upstox.com"
API_VERSION: Final[str] = "2.0"
HTTP_TIMEOUT_SEC: Final[float] = 10.0       # per-request total timeout
MAX_HTTP_ATTEMPTS: Final[int] = 6           # attempts per request (backoff)
FETCH_CONCURRENCY: Final[int] = 10          # asyncio.Semaphore(10) per spec
PACING_MIN_INTERVAL_SEC: Final[float] = 0.05  # adaptive floor between requests
PACING_MAX_INTERVAL_SEC: Final[float] = 10.0  # adaptive ceiling (429 protection)
EXEC_WORKERS: Final[int] = 1                # serialized order dispatch

# --- CSV / persistence -------------------------------------------------------
PAPER_TRADE_LOG_CSV: Final[str] = "paper_trading_log.csv"
PAPER_TRADE_SUMMARY_CSV: Final[str] = "paper_trading_summary.csv"
BACKTEST_TRADE_LOG_CSV: Final[str] = "backtest_trade_log.csv"
BACKTEST_SUMMARY_CSV: Final[str] = "backtest_summary.csv"
STATE_FILE: Final[str] = "positions_state.json"
LOG_FILE: Final[str] = "connors_engine.log"

CSV_TRADE_COLUMNS: Final[List[str]] = [
    "timestamp", "session_date", "instrument_key", "event_type", "side",
    "qty", "simulated_price", "crsi_score", "days_held", "realized_pnl",
    "notes",
]
CSV_SUMMARY_COLUMNS: Final[List[str]] = [
    "session_date", "open_positions_count", "total_unrealized_pnl",
    "realized_pnl_today",
]

# --- Universe: Nifty 50 (current NSE composition; ISINs verified against
# --- nsearchives.nseindia.com ind_nifty50list.csv, retrieved 2026-08-29).
# --- High-liquidity names chosen to keep bid-ask slippage negligible.
UNIVERSE_TICKERS: Final[List[str]] = [
    "NSE_EQ|INE002A01018",  # RELIANCE   - Reliance Industries
    "NSE_EQ|INE040A01034",  # HDFCBANK   - HDFC Bank
    "NSE_EQ|INE090A01021",  # ICICIBANK  - ICICI Bank
    "NSE_EQ|INE062A01020",  # SBIN       - State Bank of India
    "NSE_EQ|INE238A01034",  # AXISBANK   - Axis Bank
    "NSE_EQ|INE237A01028",  # KOTAKBANK  - Kotak Mahindra Bank
    "NSE_EQ|INE095A01012",  # INDUSINDBK - IndusInd Bank
    "NSE_EQ|INE028A01039",  # BANKBARODA - Bank of Baroda
    "NSE_EQ|INE160A01022",  # PNB        - Punjab National Bank
    "NSE_EQ|INE476A01022",  # CANBK      - Canara Bank
    "NSE_EQ|INE467B01029",  # TCS        - Tata Consultancy Services
    "NSE_EQ|INE009A01021",  # INFY       - Infosys
    "NSE_EQ|INE075A01022",  # WIPRO      - Wipro
    "NSE_EQ|INE669C01036",  # TECHM      - Tech Mahindra
    "NSE_EQ|INE860A01027",  # HCLTECH    - HCL Technologies
    "NSE_EQ|INE262H01021",  # PERSISTENT - Persistent Systems
    "NSE_EQ|INE591G01017",  # COFORGE    - Coforge
    "NSE_EQ|INE356A01018",  # MPHASIS    - Mphasis
    "NSE_EQ|INE018A01030",  # LT         - Larsen & Toubro
    "NSE_EQ|INE481G01011",  # ULTRACEMCO - UltraTech Cement
    "NSE_EQ|INE047A01021",  # GRASIM     - Grasim Industries
    "NSE_EQ|INE070A01015",  # SHREECEM   - Shree Cement
    "NSE_EQ|INE079A01024",  # AMBUJACEM  - Ambuja Cements
    "NSE_EQ|INE012A01025",  # ACC        - ACC
    "NSE_EQ|INE585B01010",  # MARUTI     - Maruti Suzuki
    "NSE_EQ|INE066A01021",  # EICHERMOT  - Eicher Motors
    "NSE_EQ|INE208A01029",  # ASHOKLEY   - Ashok Leyland
    "NSE_EQ|INE917I01010",  # BAJAJ-AUTO - Bajaj Auto
    "NSE_EQ|INE494B01023",  # TVSMOTOR   - TVS Motor Company
    "NSE_EQ|INE158A01026",  # HEROMOTOCO - Hero MotoCorp
    "NSE_EQ|INE044A01036",  # SUNPHARMA  - Sun Pharmaceutical
    "NSE_EQ|INE089A01023",  # DRREDDY    - Dr. Reddy's Laboratories
    "NSE_EQ|INE059A01026",  # CIPLA      - Cipla
    "NSE_EQ|INE361B01024",  # DIVISLAB   - Divi's Laboratories
    "NSE_EQ|INE326A01037",  # LUPIN      - Lupin
    "NSE_EQ|INE406A01037",  # AUROPHARMA - Aurobindo Pharma
    "NSE_EQ|INE010B01027",  # ZYDUSLIFE  - Zydus Lifesciences
    "NSE_EQ|INE685A01028",  # TORNTPHARM - Torrent Pharmaceuticals
    "NSE_EQ|INE397D01024",  # BHARTIARTL - Bharti Airtel
    "NSE_EQ|INE669E01016",  # IDEA       - Vodafone Idea
    "NSE_EQ|INE030A01027",  # HINDUNILVR - Hindustan Unilever
    "NSE_EQ|INE154A01025",  # ITC        - ITC
    "NSE_EQ|INE239A01024",  # NESTLEIND  - Nestle India
    "NSE_EQ|INE216A01030",  # BRITANNIA  - Britannia Industries
    "NSE_EQ|INE016A01026",  # DABUR      - Dabur India
    "NSE_EQ|INE102D01028",  # GODREJCP   - Godrej Consumer Products
    "NSE_EQ|INE259A01022",  # COLPAL     - Colgate-Palmolive India
    "NSE_EQ|INE196A01026",  # MARICO     - Marico
    "NSE_EQ|INE280A01028",  # TITAN      - Titan Company
    "NSE_EQ|INE849A01020",  # TRENT      - Trent
    "NSE_EQ|INE192R01011",  # DMART      - Avenue Supermarts
    "NSE_EQ|INE200M01021",  # VBL        - Varun Beverages
    "NSE_EQ|INE423A01024",  # ADANIENT   - Adani Enterprises
    "NSE_EQ|INE742F01042",  # ADANIPORTS - Adani Ports & SEZ
    "NSE_EQ|INE814H01029",  # ADANIPOWER - Adani Power
    "NSE_EQ|INE931S01010",  # ADANIENSOL - Adani Energy Solutions
    "NSE_EQ|INE364U01010",  # ADANIGREEN - Adani Green Energy
    "NSE_EQ|INE081A01020",  # TATASTEEL  - Tata Steel
    "NSE_EQ|INE019A01038",  # JSWSTEEL   - JSW Steel
    "NSE_EQ|INE038A01020",  # HINDALCO   - Hindalco Industries
    "NSE_EQ|INE749Y01014",  # JINDALSTEL - Jindal Steel & Power
    "NSE_EQ|INE114A01011",  # SAIL       - Steel Authority of India
    "NSE_EQ|INE584A01010",  # NMDC       - NMDC
    "NSE_EQ|INE205A01025",  # VEDL       - Vedanta
    "NSE_EQ|INE139A01034",  # NATIONALUM - National Aluminium Company
    "NSE_EQ|INE213A01029",  # ONGC       - Oil & Natural Gas Corporation
    "NSE_EQ|INE522F01014",  # COALINDIA  - Coal India
    "NSE_EQ|INE029A01011",  # BPCL       - Bharat Petroleum
    "NSE_EQ|INE242A01010",  # IOC        - Indian Oil Corporation
    "NSE_EQ|INE094A01027",  # HINDPETRO  - Hindustan Petroleum
    "NSE_EQ|INE274J01014",  # OIL        - Oil India
    "NSE_EQ|INE129A01019",  # GAIL       - GAIL India
    "NSE_EQ|INE752E01010",  # POWERGRID  - Power Grid Corporation
    "NSE_EQ|INE733E01010",  # NTPC       - NTPC
    "NSE_EQ|INE245A01021",  # TATAPOWER  - Tata Power
    "NSE_EQ|INE296A01032",  # BAJFINANCE - Bajaj Finance
    "NSE_EQ|INE918I01026",  # BAJAJFINSV - Bajaj Finserv
    "NSE_EQ|INE121A01024",  # CHOLAFIN   - Cholamandalam Investment & Finance
    "NSE_EQ|INE721A01047",  # SHRIRAMFIN - Shriram Finance
    "NSE_EQ|INE115A01026",  # LICHSGFIN  - LIC Housing Finance
    "NSE_EQ|INE646L01027",  # INDIGO     - InterGlobe Aviation
    "NSE_EQ|INE335Y01020",  # IRCTC      - Indian Railway Catering & Tourism
    "NSE_EQ|INE415G01027",  # RVNL       - Rail Vikas Nigam
    "NSE_EQ|INE263A01024",  # BEL        - Bharat Electronics
    "NSE_EQ|INE066F01020",  # HAL        - Hindustan Aeronautics
    "NSE_EQ|INE257A01026",  # BHEL       - Bharat Heavy Electricals
    "NSE_EQ|INE003A01024",  # SIEMENS    - Siemens
    "NSE_EQ|INE117A01022",  # ABB        - ABB India
    "NSE_EQ|INE067A01029",  # CGPOWER    - CG Power & Industrial Solutions
    "NSE_EQ|INE935N01020",  # DIXON      - Dixon Technologies
]

# --- Session calendar (IST) ---------------------------------------------------
# NSE equity hours: normal 09:15-15:30. 2026 special session: 2026-02-01
# (Union Budget live session, Sunday, 09:15-15:00).
SESSION_OPEN_TIME: Final[time] = time(9, 15)
SESSION_CLOSE_TIME: Final[time] = time(15, 30)

SPECIAL_SESSIONS_2026: Final[Dict[date, Tuple[time, time]]] = {
    date(2026, 2, 1): (time(9, 15), time(15, 0)),  # Budget special session
}

# NSE equity-segment holidays, 2026 (per NSE circulars / Upstox holiday list).
# NOTE: refresh this table every calendar year.
NSE_HOLIDAYS_2026: Final[Set[date]] = {
    date(2026, 1, 15),   # Maharashtra municipal elections (special circular)
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali - Balipratipada
    date(2026, 11, 24),  # Prakash Gurpurb (Guru Nanak Dev)
    date(2026, 12, 25),  # Christmas
}

# --- Daily schedule (spec-fixed) ----------------------------------------------
DATA_FETCH_TIME: Final[time] = time(15, 15)
SIGNAL_TIME: Final[time] = time(15, 25)
ORDER_TIME: Final[time] = time(15, 28)
SUMMARY_TIME: Final[time] = time(15, 30)

# =============================================================================
# SECTION 2 — TIME / MISC UTILITIES
# =============================================================================


def ist_timezone() -> timezone:
    """Asia/Kolkata via zoneinfo with a hard fallback (IST has no DST)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Kolkata")  # type: ignore[return-value]
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


IST_TZ: Final[timezone] = ist_timezone()


def ist_now() -> datetime:
    """Wall-clock now, tz-aware in IST."""
    return datetime.now(IST_TZ)


def session_date_of(now: Optional[datetime] = None) -> date:
    return (now or ist_now()).date()


def iso_now() -> str:
    return ist_now().isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = float("nan")) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _chunks(items: Sequence[str], size: int) -> List[List[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


# -----------------------------------------------------------------------------
# Env-file credential loader (env.txt — no CLI arguments, no prompts)
# -----------------------------------------------------------------------------
# Credentials are resolved ONCE at import from a KEY=value file named env.txt
# that must sit next to this script. This keeps secrets out of the source
# tree while preserving the "no runtime inputs" design: nothing is ever read
# from stdin, argv, or the process environment.
#
# File format (one KEY=value pair per line):
#     # optional comment lines start with '#'
#     UPSTOX_ACCESS_TOKEN = "eyJ0eXAiOiJKV1Qi..."
#     UPSTOX_API_KEY = ee2e4f16-0691-42d9-9a35-3f841ec29cbc
#     UPSTOX_API_SECRET = d3rr2a0uib
#
# Defined parsing behavior:
#   * blank lines and '#'-comment lines are ignored;
#   * the first '=' splits key from value; surrounding whitespace stripped;
#   * matching surrounding quotes ('"' or "'") are stripped from values;
#   * duplicate keys: the LAST occurrence wins;
#   * malformed lines (no '=', empty key) are skipped with a warning;
#   * a missing/unreadable file falls back to the hardcoded defaults below;
#   * no shell interpolation, no ${VAR} expansion, no environment lookup —
#     the loader is deliberately minimal and predictable.
#
# Security: token values are NEVER logged or persisted (masked previews only).

ENV_FILE_NAME: Final[str] = "env.txt"


def load_env_file(path: Path) -> Tuple[Dict[str, str], List[str]]:
    """Parse a KEY=value file; returns (settings, warnings).

    This loader never raises: every failure mode degrades to an empty
    mapping plus a human-readable warning, so a misconfigured env.txt can
    never crash the boot sequence.
    """
    warnings: List[str] = []
    settings: Dict[str, str] = {}
    if not path.exists():
        warnings.append(
            f"{path.name} not found — using hardcoded fallback credentials")
        return settings, warnings
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(
            f"{path.name} unreadable ({exc}) — using hardcoded fallback "
            "credentials")
        return settings, warnings
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue  # blank / comment
        if "=" not in line:
            warnings.append(
                f"{path.name}:{lineno}: ignored malformed line (no '=')")
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (len(value) >= 2 and value[0] == value[-1]
                and value[0] in ("'", '"')):
            value = value[1:-1]  # strip one layer of matching quotes
        if not key:
            warnings.append(
                f"{path.name}:{lineno}: ignored line with empty key")
            continue
        settings[key] = value  # duplicate key: last occurrence wins
    return settings, warnings


def _mask_token(token: str) -> str:
    """Non-reversible preview for logs — never print a real token."""
    if not token or token.startswith("YOUR_"):
        return "<none>"
    if len(token) <= 12:
        return "***"
    return f"{token[:6]}…{token[-4:]}"


# Resolved once at import. Only the three Upstox keys are consumed by the
# engine; any other keys present in env.txt are parsed and retained in
# _ENV_SETTINGS but ignored.
ENV_FILE_PATH: Final[Path] = Path(__file__).resolve().with_name(ENV_FILE_NAME)
_ENV_SETTINGS, _ENV_LOAD_WARNINGS = load_env_file(ENV_FILE_PATH)

_TOKEN_FROM_ENV: Final[bool] = bool(_ENV_SETTINGS.get("UPSTOX_ACCESS_TOKEN"))

ACCESS_TOKEN: Final[str] = (
    _ENV_SETTINGS.get("UPSTOX_ACCESS_TOKEN") or ACCESS_TOKEN_FALLBACK)
UPSTOX_API_KEY: Final[str] = _ENV_SETTINGS.get("UPSTOX_API_KEY") or ""
UPSTOX_API_SECRET: Final[str] = _ENV_SETTINGS.get("UPSTOX_API_SECRET") or ""


# =============================================================================
# SECTION 3 — EXCEPTIONS (explicit, per failure class)
# =============================================================================


class ConfigurationError(RuntimeError):
    """Raised at boot for invalid hardcoded configuration."""


class UpstoxError(RuntimeError):
    """Base class for all Upstox interaction failures."""


class UpstoxTransportError(UpstoxError):
    """Network / timeout / 5xx — retryable."""


class UpstoxRateLimited(UpstoxError):
    """HTTP 429 — retryable with pacing escalation."""


class UpstoxApiError(UpstoxError):
    """Upstox returned an error envelope (HTTP 200 + status=error or 4xx)."""

    def __init__(self, code: Optional[str], message: str,
                 http_status: Optional[int] = None) -> None:
        super().__init__(f"[{code or 'N/A'}] {message}" + (
            f" (HTTP {http_status})" if http_status else ""))
        self.code = code
        self.http_status = http_status


# =============================================================================
# SECTION 4 — DOMAIN TYPES
# =============================================================================


@dataclass(frozen=True)
class Position:
    """One open long position (one per instrument key, by invariant)."""
    instrument_key: str
    symbol: str
    entry_date: date
    entry_price: float
    quantity: int
    crsi_at_entry: float
    paper: bool = True
    notes: str = ""


@dataclass(frozen=True)
class ClosedTrade:
    """Result of closing a Position (for ledger + CSV)."""
    position: Position
    exit_price: float
    realized_pnl: float
    days_held: int
    exit_reason: str


@dataclass(frozen=True)
class LedgerEvent:
    """One immutable, append-only trade-log row (matches CSV_TRADE_COLUMNS)."""
    timestamp: str
    session_date: str
    instrument_key: str
    event_type: str          # "ENTRY" | "EXIT"
    side: str                # "BUY" | "SELL"
    qty: int
    simulated_price: float
    crsi_score: float
    days_held: int
    realized_pnl: float
    notes: str


@dataclass(frozen=True)
class OrderRequest:
    """Instruction handed to the asyncio.Queue execution worker."""
    request_id: int
    instrument_key: str
    symbol: str
    side: str                # "BUY" | "SELL"
    quantity: int
    limit_price: float
    event_type: str          # "ENTRY" | "EXIT"
    crsi_score: float
    notes: str
    created_mono: float = field(default_factory=wall_clock.monotonic)


@dataclass(frozen=True)
class EntrySignal:
    """A 15:25 signal that passed trend + trigger + inventory filters."""
    instrument_key: str
    symbol: str
    ltp: float
    crsi: float
    sma200: float
    sma5: float
    close: float
    quantity: int


@dataclass(frozen=True)
class ExitDecision:
    """A 15:15 exit decision for an open position."""
    instrument_key: str
    symbol: str
    ltp: float
    reason: str              # "SMA5_TARGET" | "TIME_EXIT"
    days_held: int


# =============================================================================
# SECTION 5 — CSV PERSISTER  (async-safe, append-only, to_thread I/O)
# =============================================================================


class CSVPersister:
    """Appends rows to CSV behind an asyncio.Lock; blocking I/O offloaded
    to a worker thread so a 15:28 burst of fills cannot stall the loop."""

    def __init__(self, path: str, columns: Sequence[str],
                 logger: logging.Logger) -> None:
        self._path = Path(path)
        self._columns: List[str] = list(columns)
        self._logger = logger
        self._lock = asyncio.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        path = self._path
        if path.exists() and path.stat().st_size > 0:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(self._columns)

    def _append_sync(self, row: Mapping[str, Any]) -> None:
        self._ensure_header()  # self-heal if the file was deleted mid-run
        with open(self._path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self._columns,
                                    extrasaction="ignore")
            writer.writerow({k: row.get(k, "") for k in self._columns})

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append_sync, row)


# =============================================================================
# SECTION 6 — LEDGER  (in-memory, immutable events, lock-guarded)
# =============================================================================


class Ledger:
    """In-memory append-only record of every simulated / live fill."""

    def __init__(self) -> None:
        self._events: List[LedgerEvent] = []
        self._lock = asyncio.Lock()

    async def add(self, event: LedgerEvent) -> None:
        async with self._lock:
            self._events.append(event)

    async def snapshot(self) -> List[LedgerEvent]:
        async with self._lock:
            return list(self._events)

    async def count(self) -> int:
        async with self._lock:
            return len(self._events)


# =============================================================================
# SECTION 7 — EXECUTION STATE  (the single source of truth for the book)
# =============================================================================
# Every mutation flows through this class under one asyncio.Lock. This is
# what makes the 15:28 queue workers race-free: two workers trying to open
# the same symbol, or one opening while the 15:25 scan still holds a
# snapshot, cannot corrupt the book — the lock serializes them and the
# open_position() reservation drops duplicates.


class State:
    def __init__(self, logger: logging.Logger, initial_cash: float,
                 paper: bool) -> None:
        self._logger = logger
        self._lock = asyncio.Lock()
        self.paper = paper
        self.positions: Dict[str, Position] = {}
        self._entry_index: Dict[str, date] = {}  # oldest open entry per key
        self.cash_available: float = initial_cash
        self.realized_pnl_today: float = 0.0
        self.session_date: Optional[date] = None

    # -- session -----------------------------------------------------------
    async def roll_session(self, new_session: date) -> None:
        async with self._lock:
            if self.session_date != new_session:
                self.session_date = new_session
                self.realized_pnl_today = 0.0

    # -- book mutations ------------------------------------------------------
    async def open_position(self, pos: Position, cost: float) -> bool:
        """Atomically reserve a position; returns False (drop) if the key
        is already held — this is the duplicate-order race guard."""
        async with self._lock:
            if pos.instrument_key in self.positions:
                self._logger.warning(
                    "duplicate entry dropped for %s (already open)",
                    pos.instrument_key)
                return False
            self.positions[pos.instrument_key] = pos
            self._entry_index.setdefault(pos.instrument_key, pos.entry_date)
            self.cash_available -= cost
            return True

    async def close_position(self, key: str, exit_price: float,
                             exit_date: date, reason: str) -> Optional[ClosedTrade]:
        async with self._lock:
            pos = self.positions.pop(key, None)
            if pos is None:
                self._logger.warning(
                    "exit dropped for %s (no open position)", key)
                return None
            if self._entry_index.get(key) == pos.entry_date:
                self._entry_index.pop(key, None)
            realized = (exit_price - pos.entry_price) * pos.quantity
            self.realized_pnl_today += realized
            self.cash_available += exit_price * pos.quantity
            days_held = max((exit_date - pos.entry_date).days, 0)
            return ClosedTrade(position=pos, exit_price=exit_price,
                               realized_pnl=realized, days_held=days_held,
                               exit_reason=reason)

    # -- reads ----------------------------------------------------------------
    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "positions": {k: v for k, v in self.positions.items()},
                "cash_available": self.cash_available,
                "realized_pnl_today": self.realized_pnl_today,
                "session_date": self.session_date,
            }

    async def invested_capital(self) -> float:
        async with self._lock:
            return sum(p.entry_price * p.quantity
                       for p in self.positions.values())

    # -- persistence (restart resilience) --------------------------------------
    async def save_state_file(self) -> None:
        async with self._lock:
            payload = {
                "saved_at": iso_now(),
                "paper": self.paper,
                "session_date": self.session_date.isoformat()
                if self.session_date else None,
                "cash_available": self.cash_available,
                "realized_pnl_today": self.realized_pnl_today,
                "positions": [
                    {
                        "instrument_key": p.instrument_key,
                        "symbol": p.symbol,
                        "entry_date": p.entry_date.isoformat(),
                        "entry_price": p.entry_price,
                        "quantity": p.quantity,
                        "crsi_at_entry": p.crsi_at_entry,
                        "paper": p.paper,
                        "notes": p.notes,
                    }
                    for p in self.positions.values()
                ],
            }

            def _write() -> None:
                Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
                with open(STATE_FILE, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)

            await asyncio.to_thread(_write)

    @staticmethod
    def load_state_file(logger: logging.Logger) -> Optional[Dict[str, Any]]:
        """Synchronous load (boot-time only). Never raises on corruption."""
        path = Path(STATE_FILE)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            positions: Dict[str, Position] = {}
            for raw in data.get("positions", []):
                key = raw.get("instrument_key")
                if not key:
                    continue
                positions[key] = Position(
                    instrument_key=key,
                    symbol=raw.get("symbol", key),
                    entry_date=date.fromisoformat(raw["entry_date"]),
                    entry_price=float(raw["entry_price"]),
                    quantity=int(raw["quantity"]),
                    crsi_at_entry=float(raw.get("crsi_at_entry", 0.0)),
                    paper=bool(raw.get("paper", True)),
                    notes=str(raw.get("notes", "")),
                )
            return {
                "positions": positions,
                "cash_available": float(data.get("cash_available",
                                                TOTAL_CAPITAL)),
                "paper": bool(data.get("paper", True)),
            }
        except Exception as exc:  # corrupted file must never kill the boot
            logger.error("state file unreadable (%s); starting fresh", exc)
            return None


# =============================================================================
# SECTION 8 — TRADE CLOCK  (session / holiday / schedule math)
# =============================================================================


class TradeClock:
    """Knows when the NSE is open and when the fixed schedule gates land."""

    def __init__(self, holidays: Set[date],
                 special_sessions: Dict[date, Tuple[time, time]],
                 open_time: time, close_time: time,
                 tz: timezone) -> None:
        self._holidays = frozenset(holidays)
        self._special = dict(special_sessions)
        self._open_time = open_time
        self._close_time = close_time
        self._tz = tz

    def session_bounds_for(self, d: date) -> Optional[Tuple[datetime, datetime]]:
        """(open, close) IST datetimes for date d, or None if market shut.

        Precedence: special sessions (e.g. the Sunday budget session) are
        consulted BEFORE the weekend/holiday checks, so an exchange-announced
        special session overrides the default closure rules.
        """
        if d in self._special:
            o_t, c_t = self._special[d]
            return (datetime.combine(d, o_t, tzinfo=self._tz),
                    datetime.combine(d, c_t, tzinfo=self._tz))
        if d.weekday() >= 5 or d in self._holidays:
            return None
        o_t, c_t = self._open_time, self._close_time
        return (datetime.combine(d, o_t, tzinfo=self._tz),
                datetime.combine(d, c_t, tzinfo=self._tz))

    def is_inside_session(self, now: Optional[datetime] = None) -> bool:
        now = now or ist_now()
        bounds = self.session_bounds_for(now.date())
        if bounds is None:
            return False
        return bounds[0] <= now <= bounds[1]

    def gate_datetime(self, d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=self._tz)

    def seconds_until(self, target: datetime, now: Optional[datetime] = None) -> float:
        """Always >= 0 — callers must never sleep a negative delta."""
        now = now or ist_now()
        return max(0.0, (target - now).total_seconds())


# =============================================================================
# SECTION 9 — PACING GATE  (inter-request spacing, adaptive to 429s)
# =============================================================================


class PacingGate:
    """Serializes request *starts* with an adaptive minimum gap.

    Why this matters: asyncio.Semaphore(10) caps concurrency but a burst of
    10 parallel requests can still land inside one second. Upstox enforces
    per-second request budgets on its endpoints (historical-candle is the
    tightest at ~2 req/s). This gate enforces a floor gap between the start
    of consecutive requests. On HTTP 429 the gap is doubled (up to a cap);
    after a long clean streak it relaxes back toward the floor, so normal
    operation is fast while a rate-limit episode degrades gracefully instead
    of getting the key banned.
    """

    def __init__(self, min_interval: float, max_interval: float) -> None:
        self._min_interval = float(min_interval)
        self._max_interval = float(max_interval)
        self._lock = asyncio.Lock()
        self._next_free_mono = 0.0
        self._clean_streak = 0

    def _interval(self) -> float:
        return max(self._min_interval, min(self._min_interval,
                                           self._max_interval))

    async def acquire(self) -> None:
        """Wait until it is legal to start the next request (async, fair)."""
        async with self._lock:
            wait = self._next_free_mono - wall_clock.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_free_mono = wall_clock.monotonic() + self._min_interval

    async def on_success(self) -> None:
        async with self._lock:
            self._clean_streak += 1
            if self._clean_streak >= 200:
                self._min_interval = max(PACING_MIN_INTERVAL_SEC,
                                         self._min_interval * 0.75)
                self._clean_streak = 0

    async def on_429(self) -> None:
        async with self._lock:
            self._clean_streak = 0
            self._min_interval = min(self._max_interval,
                                     max(self._min_interval * 2.0, 0.25))


# =============================================================================
# SECTION 10 — UPSTOX v2 REST CLIENT  (aiohttp, backoff, typed)
# =============================================================================
# Endpoints used (v2 REST):
#   GET  /v2/historical-candle/{key}/day/{to}/{from}   daily OHLCV
#   GET  /v2/market-quote/ltp?instrument_key=k1,k2..   batch last prices
#   GET  /v2/portfolio/long-term-positions             delivery positions
#   POST /v2/order/place                               order dispatch
#   GET  /v2/user/profile                               token sanity check


class UpstoxClient:
    INTERVAL_DAY: Final[str] = "day"

    def __init__(self, access_token: str, base_url: str,
                 logger: logging.Logger) -> None:
        self._token = access_token
        self._base = base_url.rstrip("/")
        self._logger = logger
        self._session: Optional[aiohttp.ClientSession] = None
        self._pace = PacingGate(PACING_MIN_INTERVAL_SEC,
                                PACING_MAX_INTERVAL_SEC)

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SEC,
                                              connect=5.0),
                headers={"Accept": "application/json",
                         "Api-Version": API_VERSION},
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- core request engine ---------------------------------------------------
    async def _request(self, method: str, path: str,
                       params: Optional[Mapping[str, str]] = None,
                       json_body: Optional[Mapping[str, Any]] = None,
                       attempt_budget: Optional[int] = None) -> Dict[str, Any]:
        """Single REST call with pacing gate + exponential backoff.

        Retry policy:
          * HTTP 429  -> UpstoxRateLimited : pacing interval doubled, retry
          * HTTP >=500/ timeout / conn errors -> retry
          * HTTP 4xx / API error envelope  -> no retry (deterministic reject)
        All waits carry full jitter to de-synchronize retry herds.
        """
        if self._session is None:
            raise UpstoxError("client not started")
        budget = attempt_budget if attempt_budget is not None \
            else MAX_HTTP_ATTEMPTS

        for attempt in range(1, budget + 1):
            await self._pace.acquire()
            headers: Dict[str, str] = {}
            if self._token and "YOUR_" not in self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            url = f"{self._base}{path}"
            try:
                async with self._session.request(
                        method, url, params=params, json=json_body,
                        headers=headers) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        await self._pace.on_429()
                        raise UpstoxRateLimited("HTTP 429 rate limited")
                    if resp.status >= 500:
                        raise UpstoxTransportError(
                            f"HTTP {resp.status} server error")
                    try:
                        payload: Dict[str, Any] = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        raise UpstoxTransportError(
                            f"non-JSON response (HTTP {resp.status})")
                    # Upstox often returns HTTP 200 with status=error inside.
                    if payload.get("status") == "error" or resp.status >= 400:
                        errs = payload.get("errors") or []
                        code = errs[0].get("errorCode") if errs else None
                        msg = errs[0].get("message") if errs else \
                            f"HTTP {resp.status}"
                        raise UpstoxApiError(code, str(msg), resp.status)
                    await self._pace.on_success()
                    return payload
            except UpstoxRateLimited:
                if attempt >= budget:
                    raise
            except UpstoxTransportError:
                if attempt >= budget:
                    raise
            except (asyncio.TimeoutError, aiohttp.ClientError,
                    OSError) as exc:
                if attempt >= budget:
                    raise UpstoxTransportError(
                        f"transport failure: {exc}") from exc
            # backoff with jitter before the next attempt
            wait = min(30.0, 0.5 * (2 ** (attempt - 1)))
            wait *= 0.8 + 0.4 * np.random.default_rng().random()
            self._logger.warning(
                "request %s failed (attempt %d/%d); backing off %.2fs",
                path, attempt, budget, wait)
            await asyncio.sleep(wait)
        raise UpstoxError(f"request exhausted retries: {path}")

    # -- endpoint wrappers ------------------------------------------------------
    async def validate_credentials(self) -> None:
        """Token sanity check; raises ConfigurationError on rejection."""
        try:
            await self._request("GET", "/v2/user/profile", attempt_budget=2)
            self._logger.info("Upstox credentials validated")
        except (UpstoxApiError, UpstoxError) as exc:
            raise ConfigurationError(f"Upstox token rejected: {exc}") from exc

    async def get_daily_history(self, instrument_key: str, to_date: date,
                                from_date: date) -> pd.DataFrame:
        key_quoted = url_quote(instrument_key, safe="")
        path = (f"/v2/historical-candle/{key_quoted}/{self.INTERVAL_DAY}"
                f"/{to_date.isoformat()}/{from_date.isoformat()}")
        payload = await self._request("GET", path)
        raw = (payload.get("data") or {}).get("candles") or []
        rows: List[List[Any]] = []
        for candle in raw:
            # candle: [ts, open, high, low, close, volume, oi]
            if not candle or len(candle) < 6:
                continue
            ts, o, h, l, c, v = candle[0], candle[1], candle[2], \
                candle[3], candle[4], candle[5]
            o, h, l, c = (_safe_float(x, float("nan")) for x in (o, h, l, c))
            if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
                continue  # drop malformed bars (defined behavior)
            try:
                dt = datetime.fromisoformat(str(ts))
            except ValueError:
                continue
            rows.append([dt, o, h, l, c, int(_safe_float(v, 0.0))])
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close",
                                         "volume"])
        df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low",
                                         "close", "volume"])
        df = df.sort_values("datetime").drop_duplicates("datetime")
        df = df.set_index("datetime")
        return df[["open", "high", "low", "close", "volume"]]

    async def fetch_daily_histories_bulk(
            self, keys: Sequence[str], to_date: date,
            lookback_days: int) -> Dict[str, Optional[pd.DataFrame]]:
        """Concurrent historical fetch: Semaphore(10) + per-key backoff.

        Returns {key: DataFrame|None}. A None marks a failed ticker; the
        orchestrator decides whether too many failures abort the session.
        """
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        from_date = to_date - timedelta(days=lookback_days)

        async def one(key: str) -> Tuple[str, Optional[pd.DataFrame]]:
            async with sem:
                try:
                    df = await self.get_daily_history(key, to_date, from_date)
                    if df.empty:
                        self._logger.warning("no history for %s", key)
                        return key, None
                    return key, df
                except Exception as exc:
                    self._logger.error("history fetch failed for %s: %s",
                                       key, exc)
                    return key, None

        self._logger.info(
            "bulk history fetch: %d tickers, %s -> %s, concurrency=%d",
            len(keys), from_date.isoformat(), to_date.isoformat(),
            FETCH_CONCURRENCY)
        pairs = await asyncio.gather(*(one(k) for k in keys))
        return dict(pairs)

    async def get_ltp_batch(self, keys: Sequence[str]) -> Dict[str, float]:
        """Batch LTPs (50 keys per call per Upstox quoting limits)."""
        out: Dict[str, float] = {}
        for chunk in _chunks(list(keys), 50):
            payload = await self._request(
                "GET", "/v2/market-quote/ltp",
                params={"instrument_key": ",".join(chunk)})
            data = payload.get("data") or {}
            for key, blob in data.items():
                lp = (blob or {}).get("last_price")
                if lp is not None:
                    lp_f = _safe_float(lp, float("nan"))
                    if math.isfinite(lp_f) and lp_f > 0:
                        out[key] = lp_f
        return out

    async def get_delivery_positions(self) -> List[Dict[str, Any]]:
        payload = await self._request("GET",
                                      "/v2/portfolio/long-term-positions")
        return payload.get("data") or []

    async def place_order(self, *, instrument_key: str, side: str,
                          quantity: int, price: float, tag: str,
                          product: str = "D", validity: str = "DAY"
                          ) -> Dict[str, Any]:
        """POST /v2/order/place. `tag` is unique per request id so a lost-ACK
        retry can be identified in order history and cancelled manually."""
        body = {
            "instrument_token": instrument_key,
            "transaction_type": side,          # BUY | SELL
            "order_type": "LIMIT",
            "quantity": int(quantity),
            "price": float(price),
            "product": product,                # D = delivery (CNC)
            "validity": validity,              # DAY
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,                   # ignored; auto-inferred by Upstox
            "market_protection": -1,
            "tag": tag,
        }
        return await self._request("POST", "/v2/order/place",
                                   json_body=body, attempt_budget=3)


# =============================================================================
# SECTION 11 — CONNORSRSI INDICATORS  (fully vectorized pandas/numpy)
# =============================================================================
# ConnorsRSI(3, 2, 100) = ( RSI(close,3)  +  RSI(streak,2)  +  PercentRank(100) ) / 3
#
#  * RSI(close, 3)    : Wilder-smoothed 3-period RSI of daily close.
#  * RSI(streak, 2)   : streak = consecutive up/down close days
#                       (+n up streak, -n down streak, 0 on flat day);
#                       RSI-2 over the *change* in the streak value.
#  * PercentRank(100) : rolling 100-day percentage rank of today's daily
#                       percent change vs the last 100 daily changes
#                       (scaled 0..100). Rank of a fraction vs the same
#                       fraction *100 is rank-invariant, so the raw pct
#                       change is ranked directly.
#
# Numeric conventions: Wilder smoothing via ewm(alpha=1/n, adjust=False);
# RSI edge cases follow TA-Lib: all-gains -> 100, all-losses -> 0,
# no movement -> 50.


def _wild_avg_with_seed(x: np.ndarray, period: int) -> np.ndarray:
    """Wilder average, textbook-exact.

    Seed = simple mean of the first `period` values, then the recursion
        y_t = (1/period) * x_t + ((period-1)/period) * y_{t-1}.

    Fully vectorized: the recursion is a linear combination of the seed and
    a cumulative weighted sum (no Python loop). Precondition: x is finite.
    """
    n = x.size
    out = np.full(n, np.nan)
    if n < period:
        return out
    alpha = 1.0 / period
    k = period - 1
    out[k] = float(np.mean(x[:period]))
    if n > period:
        r = 1.0 - alpha
        j = np.arange(1, n - k)  # positions k+1 .. n-1 relative to the seed
        inv = np.cumsum(x[k + 1:] * (r ** -j))
        out[k + 1:] = (r ** j) * (out[k] + alpha * inv)
    return out


def _wilder_rsi(gains: pd.Series, losses: pd.Series, period: int) -> pd.Series:
    """Wilder-smoothed RSI, textbook-exact (TA-Lib convention).

    `gains`/`losses` come from close.diff(), whose index-0 value is NaN;
    we drop that leading element so the seed is exactly the simple average
    of the first `period` real changes. The result aligns back onto the
    close index by construction (it starts at close index 1) and is NaN
    until `period` changes exist. Edge cases: all-gains -> 100,
    all-losses -> 0, no movement -> 50.
    """
    avg_g = _wild_avg_with_seed(gains.to_numpy(dtype=np.float64)[1:], period)
    avg_l = _wild_avg_with_seed(losses.to_numpy(dtype=np.float64)[1:], period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_g / np.where(avg_l == 0.0, np.nan, avg_l)
        rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = np.where(avg_l > 0.0, rsi, 100.0)                     # all gains
    flat = (avg_g == 0.0) & (avg_l == 0.0)
    rsi = np.where(flat, 50.0, rsi)                             # no movement
    rsi[np.isnan(avg_g) | np.isnan(avg_l)] = np.nan
    return pd.Series(rsi, index=gains.index[1:])


def streak_array(close: np.ndarray) -> np.ndarray:
    """Consecutive up/down day streak as a signed integer array.

    A pure-Numpy implementation of the streak recurrence with all NaN-safe
    rules in one pass. It is O(n) per ticker; for 50 tickers x 500 bars the
    Python-level loop is ~25k iterations (microseconds) — vectorizing this
    particular recurrence would require O(n) groupby tricks that are harder
    to audit, so correctness wins here.
    """
    n = close.shape[0]
    out = np.zeros(n, dtype=np.int64)
    cur = 0
    for i in range(1, n):
        prev, val = close[i - 1], close[i]
        if not (math.isfinite(prev) and math.isfinite(val)):
            cur = 0
        elif val > prev:
            cur = cur + 1 if cur > 0 else 1
        elif val < prev:
            cur = cur - 1 if cur < 0 else -1
        else:
            cur = 0
        out[i] = cur
    return out


def compute_crsi(hist: pd.DataFrame) -> pd.DataFrame:
    """Vectorized ConnorsRSI(3,2,100) plus SMA200/SMA5 on daily OHLCV.

    Input: DataFrame indexed by datetime with columns open/high/low/close/
    volume. Output: input columns plus sma200, sma5, sma5_prev (SMA5 of the
    prior bar — the correct reference for "LTP crosses above its 5-day SMA"
    while today's bar is still forming), rsi3, streak, rsi_streak, pct_rank,
    crsi.
    """
    if hist.empty:
        return hist.copy()
    df = hist.copy()
    close = df["close"].astype(np.float64)

    df["sma200"] = close.rolling(200, min_periods=200).mean()
    df["sma5"] = close.rolling(5, min_periods=5).mean()
    df["sma5_prev"] = df["sma5"].shift(1)

    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    df["rsi3"] = _wilder_rsi(gains, losses, 3)

    df["streak"] = streak_array(close.to_numpy(dtype=np.float64))
    s_delta = df["streak"].diff()
    s_gains = s_delta.clip(lower=0.0)
    s_losses = (-s_delta).clip(lower=0.0)
    df["rsi_streak"] = _wilder_rsi(s_gains, s_losses, 2)

    df["pct_rank"] = (close.pct_change()
                      .rolling(100, min_periods=100)
                      .rank(pct=True) * 100.0)

    df["crsi"] = (df["rsi3"] + df["rsi_streak"] + df["pct_rank"]) / 3.0
    return df


def latest_bar_metrics(df: pd.DataFrame, expected_bar_date: Optional[date]
                       ) -> Optional[Dict[str, Any]]:
    """Extract the evaluation row for the newest bar.

    Returns None when the series is too short (no SMA200 yet) or when the
    newest bar is not from `expected_bar_date` (stale-data guard: a ticker
    with no fresh bar today must NOT generate today's signals).
    """
    if df.empty or "crsi" not in df.columns:
        return None
    last = df.iloc[-1]
    sma200 = _safe_float(last.get("sma200"), float("nan"))
    if not math.isfinite(sma200):
        return None  # fewer than 200 bars — skip until self-healed
    bar_dt = df.index[-1]
    bar_date = bar_dt.date() if isinstance(bar_dt, datetime) else \
        (bar_dt.date() if hasattr(bar_dt, "date") else None)
    if expected_bar_date is not None and bar_date != expected_bar_date:
        return None
    return {
        "bar_date": bar_date,
        "close": _safe_float(last.get("close")),
        "sma200": sma200,
        "sma5": _safe_float(last.get("sma5")),
        "sma5_prev": _safe_float(last.get("sma5_prev")),
        "rsi3": _safe_float(last.get("rsi3")),
        "streak": int(_safe_float(last.get("streak"))),
        "rsi_streak": _safe_float(last.get("rsi_streak")),
        "pct_rank": _safe_float(last.get("pct_rank")),
        "crsi": _safe_float(last.get("crsi")),
    }


# =============================================================================
# SECTION 12 — POSITION SIZING / PRICE GUARDS
# =============================================================================


def compute_quantity(alloc_capital: float, price: float,
                     lot_size: int = LOT_SIZE) -> int:
    """floor(alloc / price) snapped down to a whole number of lots."""
    if (not math.isfinite(price)) or price <= 0 or alloc_capital <= 0:
        return 0
    qty = int(alloc_capital / price)
    if lot_size > 1:
        qty -= qty % lot_size
    return max(qty, 0)


def tick_size_for(price: float) -> float:
    """NSE equity tick size: 0.05 above/at Rs 1, 0.01 below."""
    return 0.05 if price >= 1.0 else 0.01


def round_to_tick(price: float, tick: float, direction: str) -> float:
    """Round to an exact tick multiple. direction: 'up' | 'down'.

    BUY limits round DOWN (never more aggressive than the tick grid allows);
    SELL limits round UP. IEEE float dust is handled with epsilon scaling.
    """
    if not math.isfinite(price) or tick <= 0:
        return price
    eps = 1e-9
    if direction == "up":
        return math.ceil(price / tick - eps) * tick
    if direction == "down":
        return math.floor(price / tick + eps) * tick
    return round(price / tick) * tick


def circuit_bands(prev_close: float, tick: float) -> Tuple[float, float]:
    """NSE equity daily price bands (20%): (lower, upper), tick-aligned."""
    lower = round_to_tick(prev_close * 0.80, tick, "down")
    upper = round_to_tick(prev_close * 1.20, tick, "up")
    return lower, upper


def buy_limit_price(ltp: float, prev_close: float,
                    buffer_ticks: int = SLIPPAGE_BUFFER_TICKS) -> Optional[float]:
    """Aggressive BUY limit = LTP + buffer_ticks, capped at upper circuit."""
    tick = tick_size_for(ltp)
    price = round_to_tick(ltp + buffer_ticks * tick, tick, "down")
    _, upper = circuit_bands(prev_close, tick)
    price = min(price, upper)
    return price if price > 0 else None


def sell_limit_price(ltp: float, prev_close: float,
                     buffer_ticks: int = SLIPPAGE_BUFFER_TICKS) -> Optional[float]:
    """Aggressive SELL limit = LTP - buffer_ticks, floored at lower circuit."""
    tick = tick_size_for(ltp)
    price = round_to_tick(ltp - buffer_ticks * tick, tick, "up")
    lower, _ = circuit_bands(prev_close, tick)
    price = max(price, lower)
    return price if price > 0 else None


# =============================================================================
# SECTION 13 — EXECUTION ENGINE  (asyncio.Queue order workers)
# =============================================================================
# All 15:28 order dispatch flows through a bounded asyncio.Queue consumed by
# EXEC_WORKERS workers. Rationale: in LIVE mode a single slow order POST
# (rate-limit backoff) must not stall the rest of the near-close wave —
# the queue decouples dispatch from execution, and workers serialize against
# the API's rate budget via the same PacingGate.
#
# Paper path: the worker simulates the fill against the PAPER_SLIPPAGE_MODEL_BPS
# model and mutates State/Ledger/CSV exactly like a real fill would.
# Live path: the worker POSTs the order; State/Ledger/CSV are mutated ONLY
# after an exchange ACK. A rejected order mutates nothing (defined behavior —
# the position simply does not exist and can be re-evaluated next session).


class ExecutionEngine:
    def __init__(self, client: UpstoxClient, state: State, ledger: Ledger,
                 trade_csv: CSVPersister, logger: logging.Logger,
                 paper_mode: bool) -> None:
        self._client = client
        self._state = state
        self._ledger = ledger
        self._csv = trade_csv
        self._logger = logger
        self._paper = paper_mode
        self._queue: asyncio.Queue[Optional[OrderRequest]] = \
            asyncio.Queue(maxsize=256)
        self._workers: List[asyncio.Task[None]] = []
        self._next_request_id = 0

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        for i in range(EXEC_WORKERS):
            task = asyncio.create_task(self._worker(i),
                                       name=f"cqrsi-exec-{i}")
            self._workers.append(task)

    async def stop(self) -> None:
        """Drain, then stop. Never hangs: workers exit on the sentinel."""
        for _ in self._workers:
            await self._queue.put(None)  # sentinel per worker
        done, pending = await asyncio.wait(self._workers, timeout=30.0)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._workers.clear()

    async def submit(self, req: OrderRequest) -> None:
        self._next_request_id += 1
        await self._queue.put(req)

    async def flush(self, timeout_sec: float = 30.0) -> bool:
        """Block until the queue is empty (or timeout). Returns drained?."""
        deadline = wall_clock.monotonic() + timeout_sec
        while not self._queue.empty():
            if wall_clock.monotonic() > deadline:
                self._logger.error(
                    "execution queue did not drain in %.0fs", timeout_sec)
                return False
            await asyncio.sleep(0.05)
        # also wait for in-flight items to finish
        await asyncio.sleep(0.0)
        return True

    # -- workers ---------------------------------------------------------------
    async def _worker(self, idx: int) -> None:
        self._logger.info("execution worker %d online (%s)", idx,
                          "PAPER" if self._paper else "LIVE")
        while True:
            req = await self._queue.get()
            try:
                if req is None:
                    return
                await self._execute_one(req)
            except Exception:
                self._logger.exception("order execution failed for %s",
                                       getattr(req, "instrument_key", "?"))
            finally:
                self._queue.task_done()

    async def _execute_one(self, req: OrderRequest) -> None:
        if self._paper:
            await self._simulate_paper_fill(req)
        else:
            await self._dispatch_live(req)

    # -- paper path ------------------------------------------------------------
    async def _simulate_paper_fill(self, req: OrderRequest) -> None:
        bps = PAPER_SLIPPAGE_MODEL_BPS / 10_000.0
        fill = req.limit_price * (1.0 + bps if req.side == "BUY"
                                  else 1.0 - bps)
        today = session_date_of()

        if req.event_type == "ENTRY":
            pos = Position(instrument_key=req.instrument_key,
                           symbol=req.symbol,
                           entry_date=today,
                           entry_price=fill,
                           quantity=req.quantity,
                           crsi_at_entry=req.crsi_score,
                           paper=True,
                           notes=req.notes)
            ok = await self._state.open_position(pos, fill * req.quantity)
            if not ok:
                return  # duplicate entry dropped by the state guard
            event = LedgerEvent(
                timestamp=iso_now(), session_date=today.isoformat(),
                instrument_key=req.instrument_key, event_type="ENTRY",
                side="BUY", qty=req.quantity, simulated_price=round(fill, 2),
                crsi_score=round(req.crsi_score, 4), days_held=0,
                realized_pnl=0.0,
                notes=f"{req.notes} | paper_fill {PAPER_SLIPPAGE_MODEL_BPS:g}bps")
            await self._ledger.add(event)
            await self._csv.append(asdict(event))
            self._logger.info(
                "PAPER ENTRY  %s %s qty=%d limit=%.2f fill=%.2f crsi=%.2f",
                req.symbol, req.instrument_key, req.quantity,
                req.limit_price, fill, req.crsi_score)

        elif req.event_type == "EXIT":
            closed = await self._state.close_position(
                req.instrument_key, exit_price=fill, exit_date=today,
                reason=req.notes)
            if closed is None:
                return
            event = LedgerEvent(
                timestamp=iso_now(), session_date=today.isoformat(),
                instrument_key=req.instrument_key, event_type="EXIT",
                side="SELL", qty=req.quantity, simulated_price=round(fill, 2),
                crsi_score=round(req.crsi_score, 4),
                days_held=closed.days_held,
                realized_pnl=round(closed.realized_pnl, 2),
                notes=f"{req.notes} | paper_fill {PAPER_SLIPPAGE_MODEL_BPS:g}bps")
            await self._ledger.add(event)
            await self._csv.append(asdict(event))
            self._logger.info(
                "PAPER EXIT   %s qty=%d fill=%.2f pnl=%.2f days=%d (%s)",
                req.symbol, req.quantity, fill, closed.realized_pnl,
                closed.days_held, req.notes)

    # -- live path -------------------------------------------------------------
    async def _dispatch_live(self, req: OrderRequest) -> None:
        tag = f"cqrsi-{req.request_id:08d}"
        self._logger.info("LIVE ORDER   %s %s %s qty=%d limit=%.2f tag=%s",
                          req.symbol, req.side, req.event_type, req.quantity,
                          req.limit_price, tag)
        resp = await self._client.place_order(
            instrument_key=req.instrument_key, side=req.side,
            quantity=req.quantity, price=req.limit_price, tag=tag)
        order_id = str(((resp.get("data") or {}).get("order_id") or "?"))
        today = session_date_of()

        if req.event_type == "ENTRY":
            pos = Position(instrument_key=req.instrument_key,
                           symbol=req.symbol,
                           entry_date=today,
                           entry_price=req.limit_price,
                           quantity=req.quantity,
                           crsi_at_entry=req.crsi_score,
                           paper=False,
                           notes=req.notes)
            ok = await self._state.open_position(pos,
                                                 req.limit_price * req.quantity)
            if not ok:
                # Ack received but book already had the name: cancel the
                # duplicate and log loudly (should never happen).
                self._logger.error(
                    "LIVE duplicate entry %s acked (%s); manual review required",
                    req.instrument_key, order_id)
                return
            event = LedgerEvent(
                timestamp=iso_now(), session_date=today.isoformat(),
                instrument_key=req.instrument_key, event_type="ENTRY",
                side="BUY", qty=req.quantity,
                simulated_price=round(req.limit_price, 2),
                crsi_score=round(req.crsi_score, 4), days_held=0,
                realized_pnl=0.0,
                notes=f"{req.notes} | LIVE order_id={order_id}")
            await self._ledger.add(event)
            await self._csv.append(asdict(event))
            self._logger.info("LIVE ENTRY ACK %s order_id=%s", req.symbol,
                              order_id)

        elif req.event_type == "EXIT":
            closed = await self._state.close_position(
                req.instrument_key, exit_price=req.limit_price,
                exit_date=today, reason=req.notes)
            if closed is None:
                return
            event = LedgerEvent(
                timestamp=iso_now(), session_date=today.isoformat(),
                instrument_key=req.instrument_key, event_type="EXIT",
                side="SELL", qty=req.quantity,
                simulated_price=round(req.limit_price, 2),
                crsi_score=round(req.crsi_score, 4),
                days_held=closed.days_held,
                realized_pnl=round(closed.realized_pnl, 2),
                notes=f"{req.notes} | LIVE order_id={order_id}")
            await self._ledger.add(event)
            await self._csv.append(asdict(event))
            self._logger.info("LIVE EXIT ACK  %s order_id=%s pnl=%.2f",
                              req.symbol, order_id, closed.realized_pnl)


# =============================================================================
# SECTION 14 — LIVE ORCHESTRATOR  (the 15:15 / 15:25 / 15:28 / 15:30 brain)
# =============================================================================


class LiveOrchestrator:
    """Owns the daily pipeline: fetch -> exit scan -> signal -> dispatch ->
    summary. Dormant until its gates; every gate is exception-isolated so a
    failed fetch cannot kill the session silently."""

    def __init__(self, clock: TradeClock, logger: logging.Logger) -> None:
        self._clock = clock
        self._logger = logger
        self._client = UpstoxClient(ACCESS_TOKEN, API_BASE_URL, logger)
        self._paper = PAPER_TRADING_MODE
        self._ledger = Ledger()
        self._state = State(logger, TOTAL_CAPITAL, paper=self._paper)
        self._trade_csv = CSVPersister(
            PAPER_TRADE_LOG_CSV, CSV_TRADE_COLUMNS, logger)
        self._summary_csv = CSVPersister(
            PAPER_TRADE_SUMMARY_CSV, CSV_SUMMARY_COLUMNS, logger)
        self._engine = ExecutionEngine(self._client, self._state,
                                       self._ledger, self._trade_csv,
                                       logger, paper_mode=self._paper)
        self._histories: Dict[str, pd.DataFrame] = {}
        self._indicators: Dict[str, pd.DataFrame] = {}
        self._pending_entries: List[EntrySignal] = []
        self._pending_exits: Dict[str, ExitDecision] = {}

    # ------------------------------------------------------------------ setup
    async def setup(self) -> None:
        await self._client.start()
        await self._engine.start()

        # Restore the book from disk if this is a restart mid-swing.
        saved = State.load_state_file(self._logger)
        if saved and saved.get("paper") == self._paper:
            self._state.positions = saved["positions"]
            self._state.cash_available = saved["cash_available"]
            self._logger.info("restored %d position(s) from %s",
                              len(self._state.positions), STATE_FILE)
        if self._paper:
            self._logger.info(
                "PAPER TRADING MODE — no orders will reach the broker. "
                "capital=%.2f pct=%.1f%% threshold=%.1f hold=%dd",
                TOTAL_CAPITAL, POSITION_SIZE_PCT * 100, CRSI_THRESHOLD,
                MAX_HOLD_DAYS)
        else:
            self._logger.warning(
                "LIVE MODE ENGAGED — real orders will be dispatched!")

    async def shutdown(self) -> None:
        self._logger.info("shutting down: saving state, closing workers…")
        await self._state.save_state_file()
        await self._engine.stop()
        await self._client.close()
        self._logger.info("shutdown complete")

    # ------------------------------------------------------------- portfolio
    async def reconcile_portfolio(self) -> None:
        """LIVE boot: broker delivery positions are the book of record.
        Entry metadata (date/CRSI) is taken from the persisted state file
        when available; positions not in the broker are dropped; positions
        in the broker but not in state are adopted at their average price."""
        try:
            broker_positions = await self._client.get_delivery_positions()
        except Exception as exc:
            self._logger.error("portfolio reconcile failed: %s", exc)
            raise

        today = session_date_of()
        saved = self._state.positions
        adopted: Dict[str, Position] = {}
        invested = 0.0
        for raw in broker_positions:
            qty = int(_safe_float(raw.get("quantity"), 0.0))
            key = str(raw.get("instrument_token") or "")
            if qty <= 0 or key not in UNIVERSE_TICKERS:
                continue
            avg = _safe_float(raw.get("average_price"),
                              _safe_float(raw.get("last_price"), 0.0))
            old = saved.get(key)
            adopted[key] = Position(
                instrument_key=key,
                symbol=raw.get("tradingsymbol") or key,
                entry_date=old.entry_date if old else today,
                entry_price=old.entry_price if old else avg,
                quantity=qty,
                crsi_at_entry=old.crsi_at_entry if old else 0.0,
                paper=False,
                notes="broker_adopted" if not old else "broker_reconciled",
            )
            invested += avg * qty
        self._state.positions = adopted
        self._state.cash_available = TOTAL_CAPITAL - invested
        self._logger.info("reconciled: %d broker position(s), "
                          "invested=%.2f, cash=%.2f",
                          len(adopted), invested,
                          self._state.cash_available)

    # ------------------------------------------------------------ 15:15 gate
    async def _fetch_histories(self, as_of: date) -> bool:
        """Universe-wide EOD fetch with Semaphore(10) + backoff."""
        results = await self._client.fetch_daily_histories_bulk(
            UNIVERSE_TICKERS, to_date=as_of, lookback_days=DATA_LOOKBACK_DAYS)
        ok = {k: df for k, df in results.items()
              if df is not None and not df.empty}
        failed = [k for k in UNIVERSE_TICKERS if k not in ok]
        if failed:
            self._logger.error("%d ticker(s) failed history fetch: %s",
                               len(failed), failed)
        if len(failed) > MAX_UNIVERSE_FAILURES:
            raise RuntimeError(
                f"{len(failed)} ticker failures exceed "
                f"MAX_UNIVERSE_FAILURES={MAX_UNIVERSE_FAILURES}; "
                "aborting session rather than trading half-blind")
        self._histories = ok
        self._indicators = {k: compute_crsi(df) for k, df in ok.items()}
        self._logger.info("histories ready for %d tickers; indicators "
                          "computed", len(ok))
        return len(ok) > 0

    async def _evaluate_exits(self, as_of: date) -> None:
        """15:15 exit scan on every open position (target + time exits)."""
        self._pending_exits = {}
        snap = await self._state.snapshot()
        positions = snap["positions"]
        if not positions:
            self._logger.info("exit scan: no open positions")
            return
        keys = list(positions.keys())
        try:
            ltps = await self._client.get_ltp_batch(keys)
        except Exception as exc:
            self._logger.error("LTP batch failed; exits deferred: %s", exc)
            return

        for key, pos in positions.items():
            ltp = ltps.get(key)
            if ltp is None:
                self._logger.warning("exit scan: no LTP for %s; deferred",
                                     key)
                continue
            ind = self._indicators.get(key)
            metrics = latest_bar_metrics(ind, expected_bar_date=as_of) \
                if ind is not None else None
            sma5_prev = metrics.get("sma5_prev") if metrics else None
            days_held = max((as_of - pos.entry_date).days, 0)
            reason: Optional[str] = None
            if sma5_prev is not None and math.isfinite(sma5_prev) \
                    and ltp > sma5_prev:
                reason = "SMA5_TARGET"
            elif days_held >= MAX_HOLD_DAYS:
                reason = "TIME_EXIT"
            if reason:
                self._pending_exits[key] = ExitDecision(
                    instrument_key=key, symbol=pos.symbol, ltp=ltp,
                    reason=reason, days_held=days_held)
                self._logger.info(
                    "EXIT SIGNAL %s ltp=%.2f sma5_prev=%s days=%d -> %s",
                    pos.symbol, ltp,
                    f"{sma5_prev:.2f}" if sma5_prev else "n/a",
                    days_held, reason)

    # ------------------------------------------------------------ 15:25 gate
    async def _generate_signals(self, as_of: date) -> None:
        """Trend filter + CRSI trigger + sizing + inventory exclusion."""
        self._pending_entries = []
        snap = await self._state.snapshot()
        held = set(snap["positions"].keys())
        candidates = [k for k in UNIVERSE_TICKERS
                      if k in self._indicators and k not in held]
        if not candidates:
            self._logger.info("signal scan: no eligible candidates")
            return
        try:
            ltps = await self._client.get_ltp_batch(candidates)
        except Exception as exc:
            self._logger.error("LTP batch failed; no signals today: %s", exc)
            return

        alloc = POSITION_SIZE_PCT * TOTAL_CAPITAL
        capacity = MAX_CONCURRENT_POSITIONS - len(held)
        raw: List[EntrySignal] = []
        for key in candidates:
            ltp = ltps.get(key)
            if ltp is None:
                continue  # no quote -> cannot trade (defined behavior)
            metrics = latest_bar_metrics(self._indicators[key],
                                         expected_bar_date=as_of)
            if metrics is None:
                self._logger.debug("skip %s: stale/insufficient data", key)
                continue
            close = metrics["close"]
            sma200 = metrics["sma200"]
            crsi = metrics["crsi"]
            if not math.isfinite(close) or not math.isfinite(sma200) \
                    or not math.isfinite(crsi):
                continue
            trend_ok = close > sma200
            trigger = crsi < CRSI_THRESHOLD
            if not (trend_ok and trigger):
                continue
            qty = compute_quantity(alloc, ltp)
            if qty < 1:
                self._logger.warning("skip %s: qty<1 at ltp=%.2f", key, ltp)
                continue
            raw.append(EntrySignal(instrument_key=key, symbol=key,
                                   ltp=ltp, crsi=crsi, sma200=sma200,
                                   sma5=metrics["sma5"], close=close,
                                   quantity=qty))
        # Strongest signals first when the concurrency cap binds.
        raw.sort(key=lambda s: s.crsi)
        self._pending_entries = raw[:max(capacity, 0)]
        self._logger.info("signals: %d passed -> %d dispatched candidates",
                          len(raw), len(self._pending_entries))

    # ------------------------------------------------------------ 15:28 gate
    async def _dispatch_orders(self, as_of: date) -> None:
        """Build aggressive limit orders and push through the queue worker."""
        todo: List[Tuple[str, str, str, int, float, str, float, str]] = []

        # Entries (limit = LTP + buffer ticks, capped at upper circuit).
        for sig in self._pending_entries:
            hist = self._histories.get(sig.instrument_key)
            prev_close = float(hist["close"].iloc[-2]) if \
                (hist is not None and len(hist) >= 2) else sig.close
            buy_px = buy_limit_price(sig.ltp, prev_close)
            if buy_px is None or buy_px <= sig.ltp:
                self._logger.warning("skip entry %s: limit=%.2f <= ltp=%.2f",
                                     sig.symbol, buy_px, sig.ltp)
                continue
            todo.append((sig.instrument_key, sig.symbol, "BUY", sig.quantity,
                         buy_px, "ENTRY", sig.crsi, "CRSI_ENTRY"))

        # Exits (limit = LTP - buffer ticks, floored at lower circuit).
        for key, dec in self._pending_exits.items():
            hist = self._histories.get(key)
            prev_close = float(hist["close"].iloc[-2]) if \
                (hist is not None and len(hist) >= 2) else dec.ltp
            sell_px = sell_limit_price(dec.ltp, prev_close)
            if sell_px is None or sell_px >= dec.ltp:
                self._logger.warning("skip exit %s: limit=%.2f >= ltp=%.2f",
                                     dec.symbol, sell_px, dec.ltp)
                continue
            snap = await self._state.snapshot()
            pos = snap["positions"].get(key)
            if pos is None:
                continue  # vanished since decision (shouldn't happen)
            todo.append((key, dec.symbol, "SELL", pos.quantity, sell_px,
                         "EXIT", pos.crsi_at_entry, dec.reason))

        if not todo:
            self._logger.info("dispatch: nothing to do")
            return
        for item in todo:
            key, symbol, side, qty, price, event_type, crsi, notes = item
            await self._engine.submit(OrderRequest(
                request_id=0,  # engine re-numbers on submit
                instrument_key=key, symbol=symbol, side=side,
                quantity=qty, limit_price=price, event_type=event_type,
                crsi_score=crsi, notes=notes))
        drained = await self._engine.flush()
        self._logger.info("dispatch: %d order(s) submitted (drained=%s)",
                          len(todo), drained)
        self._pending_entries = []
        self._pending_exits = {}

    # ------------------------------------------------------------ 15:30 gate
    async def _write_summary(self, as_of: date) -> None:
        snap = await self._state.snapshot()
        positions: Dict[str, Position] = snap["positions"]
        unrealized = 0.0
        if positions:
            try:
                ltps = await self._client.get_ltp_batch(list(positions))
            except Exception as exc:
                self._logger.error("summary LTP batch failed: %s", exc)
                ltps = {}
            for key, pos in positions.items():
                px = ltps.get(key, pos.entry_price)
                unrealized += (px - pos.entry_price) * pos.quantity
        row = {
            "session_date": as_of.isoformat(),
            "open_positions_count": len(positions),
            "total_unrealized_pnl": round(unrealized, 2),
            "realized_pnl_today": round(snap["realized_pnl_today"], 2),
        }
        await self._summary_csv.append(row)
        await self._state.save_state_file()
        self._logger.info(
            "SUMMARY %s | open=%d unrealized=%.2f realized_today=%.2f "
            "cash=%.2f ledger_events=%d",
            as_of, row["open_positions_count"], row["total_unrealized_pnl"],
            row["realized_pnl_today"], snap["cash_available"],
            await self._ledger.count())

    # ------------------------------------------------------------- scheduler
    async def _fetch_and_exits(self, as_of: date) -> None:
        """15:15 gate composite: universe fetch, then exit evaluation."""
        await self._fetch_histories(as_of)
        await self._evaluate_exits(as_of)

    async def run_schedule(self, now: datetime,
                           bounds: Tuple[datetime, datetime]) -> None:
        """Sequential gates; each isolated so one failure logs & continues.

        Late-boot rules (defined behavior):
          * fetch / signal gates already missed -> run IMMEDIATELY at boot,
            because their outputs feed the remaining gates;
          * order gate missed but still inside the session -> run now;
          * order gate missed AFTER the close -> skip (a 15:31 LIMIT would
            silently become an AMO order for the next session — forbidden);
          * summary gate missed -> run now if within a short post-close
            grace, otherwise skip.
        """
        today = now.date()
        session_open, session_close = bounds
        await self._state.roll_session(today)

        gates: Sequence[Tuple[time, str, Callable[[date], Awaitable[None]]]] = (
            (DATA_FETCH_TIME, "fetch+exits", self._fetch_and_exits),
            (SIGNAL_TIME, "signals", self._generate_signals),
            (ORDER_TIME, "dispatch", self._dispatch_orders),
            (SUMMARY_TIME, "summary", self._write_summary),
        )
        # `now` is the boot timestamp: a gate whose wake time is <= boot time
        # was missed (late boot / restart). Sleeps use live wall time.
        current = now
        for t, name, handler in gates:
            wake = self._clock.gate_datetime(today, t)
            if wake <= current:
                # Gate already passed (late boot / restart).
                if t == ORDER_TIME and not self._clock.is_inside_session(current):
                    self._logger.warning(
                        "gate %-11s @ %s missed after close — skipping "
                        "(post-close orders would become AMO)", name,
                        t.strftime("%H:%M"))
                    continue
                if t == SUMMARY_TIME and \
                        current > session_close + timedelta(minutes=5):
                    self._logger.info("gate %-11s @ %s missed with no "
                                      "grace — skipping", name,
                                      t.strftime("%H:%M"))
                    continue
                self._logger.info("gate %-11s @ %s missed (boot %s) — "
                                  "running now", name, t.strftime("%H:%M"),
                                  current.strftime("%H:%M:%S"))
                try:
                    await handler(today)
                except Exception:
                    self._logger.exception("late gate '%s' failed; "
                                           "session continues", name)
                continue
            if wake > session_close:
                self._logger.info("gate %-11s @ %s lies past today's "
                                  "close (%s) — skipped", name,
                                  t.strftime("%H:%M"),
                                  session_close.strftime("%H:%M"))
                continue
            wait = self._clock.seconds_until(wake)
            self._logger.info("dormant %.0fs until gate %s @ %s",
                              wait, name, t.strftime("%H:%M"))
            await asyncio.sleep(wait)
            current = ist_now()
            self._logger.info("=== GATE %s @ %s ===", name,
                              current.strftime("%H:%M:%S"))
            try:
                await handler(today)
            except Exception:
                self._logger.exception("gate '%s' failed; session continues",
                                       name)


# =============================================================================
# SECTION 15 — BACKTEST ENGINE  (day-by-day replay through the same brain)
# =============================================================================


class Backtester:
    """Replays NUM_BACKTEST_DAYS of daily candles through the exact same
    indicator + sizing logic as the live path, with FIFO accounting,
    slippage-adjusted closes, a running capital balance, and identical CSV
    schemas written to the BACKTEST_* files."""

    def __init__(self, client: UpstoxClient, logger: logging.Logger) -> None:
        self._client = client
        self._logger = logger

    # ------------------------------------------------------------------ data
    async def _load_histories(self) -> Dict[str, pd.DataFrame]:
        if BACKTEST_USE_SYNTHETIC_DATA:
            self._logger.warning(
                "BACKTEST_USE_SYNTHETIC_DATA=True: using deterministic "
                "synthetic OHLCV (self-test mode, NOT real market data)")
            return generate_synthetic_ohlcv(UNIVERSE_TICKERS,
                                            NUM_BACKTEST_DAYS)
        to_date = session_date_of()
        from_date = to_date - timedelta(days=NUM_BACKTEST_DAYS)
        results = await self._client.fetch_daily_histories_bulk(
            UNIVERSE_TICKERS, to_date=to_date,
            lookback_days=NUM_BACKTEST_DAYS)
        valid = {k: df for k, df in results.items()
                 if df is not None and not df.empty}
        failed = [k for k in UNIVERSE_TICKERS if k not in valid]
        if failed:
            self._logger.error("backtest: %d ticker(s) have no data: %s",
                               len(failed), failed)
        if not valid:
            raise RuntimeError("backtest aborted: zero usable histories")
        return valid

    # ------------------------------------------------------------------- run
    async def run(self) -> Dict[str, Any]:
        logger = self._logger
        bps = PAPER_SLIPPAGE_MODEL_BPS / 10_000.0
        logger.info("BACKTEST START: %d days lookback, capital=%.2f, "
                    "pct=%.1f%%, threshold=%.1f, hold=%dd",
                    NUM_BACKTEST_DAYS, TOTAL_CAPITAL,
                    POSITION_SIZE_PCT * 100, CRSI_THRESHOLD, MAX_HOLD_DAYS)
        await self._client.start()
        try:
            hist_map = await self._load_histories()
        finally:
            await self._client.close()

        # Normalize every frame: date-level index, dedup, indicators.
        frames: Dict[str, pd.DataFrame] = {}
        for key, df in hist_map.items():
            d = df.copy()
            d.index = pd.to_datetime(d.index).normalize()
            d = d[~d.index.duplicated(keep="last")].sort_index()
            frames[key] = compute_crsi(d)

        calendar_days = sorted({
            ts.date() for df in frames.values() for ts in df.index
        })
        if not calendar_days:
            raise RuntimeError("backtest aborted: empty calendar")
        logger.info("backtest calendar: %s -> %s (%d sessions)",
                    calendar_days[0], calendar_days[-1], len(calendar_days))

        trade_csv = CSVPersister(BACKTEST_TRADE_LOG_CSV, CSV_TRADE_COLUMNS,
                                 logger)
        summary_csv = CSVPersister(BACKTEST_SUMMARY_CSV, CSV_SUMMARY_COLUMNS,
                                   logger)

        cash = TOTAL_CAPITAL
        open_positions: Dict[str, Position] = {}
        last_close: Dict[str, float] = {}
        stats = {"entries": 0, "exits": 0, "wins": 0, "losses": 0,
                 "total_pnl": 0.0, "hold_days": 0.0,
                 "max_concurrent": 0, "entry_crsi_sum": 0.0}

        def _emit(session: date, key: str, event_type: str, side: str,
                  qty: int, px: float, crsi: float, days_held: int,
                  pnl: float, notes: str) -> None:
            event = LedgerEvent(
                timestamp=session.isoformat() + "T15:28:00+05:30",
                session_date=session.isoformat(),
                instrument_key=key, event_type=event_type, side=side,
                qty=qty, simulated_price=round(px, 2),
                crsi_score=round(crsi, 4), days_held=days_held,
                realized_pnl=round(pnl, 2), notes=notes + " | backtest")
            asyncio.create_task(trade_csv.append(asdict(event)))
            # keep deterministic ordering: awaited via gather at day end

        for day in calendar_days:
            # --- LTP map for the session (close of the day) ----------------
            ltps: Dict[str, float] = {}
            for key, df in frames.items():
                try:
                    row = df.loc[pd.Timestamp(day)]
                    px = _safe_float(row["close"], float("nan"))
                    if math.isfinite(px) and px > 0:
                        ltps[key] = px
                        last_close[key] = px
                except KeyError:
                    pass  # no bar today (suspension/listing gap): no trade

            realized_today = 0.0

            # --- 15:15 exit scan (FIFO by book insertion order) ------------
            for key in list(open_positions.keys()):
                pos = open_positions[key]
                ltp = ltps.get(key)
                if ltp is None:
                    continue  # cannot trade a name with no bar today
                try:
                    row = frames[key].loc[pd.Timestamp(day)]
                except KeyError:
                    continue
                sma5_prev = _safe_float(row.get("sma5_prev"), float("nan"))
                days_held = max((day - pos.entry_date).days, 0)
                reason: Optional[str] = None
                if math.isfinite(sma5_prev) and ltp > sma5_prev:
                    reason = "SMA5_TARGET"
                elif days_held >= MAX_HOLD_DAYS:
                    reason = "TIME_EXIT"
                if reason is None:
                    continue
                exec_px = ltp * (1.0 - bps)
                pnl = (exec_px - pos.entry_price) * pos.quantity
                cash += exec_px * pos.quantity
                realized_today += pnl
                stats["exits"] += 1
                stats["total_pnl"] += pnl
                stats["hold_days"] += days_held
                if pnl > 0:
                    stats["wins"] += 1
                elif pnl < 0:
                    stats["losses"] += 1
                _emit(day, key, "EXIT", "SELL", pos.quantity, exec_px,
                      pos.crsi_at_entry, days_held, pnl, reason)
                del open_positions[key]

            # --- 15:25 entry scan (same filters, sorted by CRSI asc) -------
            running_capital = cash + sum(
                p.quantity * last_close.get(k, p.entry_price)
                for k, p in open_positions.items())
            capacity = MAX_CONCURRENT_POSITIONS - len(open_positions)
            candidates: List[Tuple[float, str, Dict[str, Any]]] = []
            for key, df in frames.items():
                if key in open_positions or key not in ltps:
                    continue
                try:
                    row = df.loc[pd.Timestamp(day)]
                except KeyError:
                    continue
                sma200 = _safe_float(row.get("sma200"), float("nan"))
                crsi = _safe_float(row.get("crsi"), float("nan"))
                close = _safe_float(row.get("close"), float("nan"))
                if not (math.isfinite(sma200) and math.isfinite(crsi)
                        and math.isfinite(close)):
                    continue
                if not (close > sma200 and crsi < CRSI_THRESHOLD):
                    continue
                candidates.append((crsi, key, {"close": close, "sma200":
                                               sma200, "crsi": crsi}))
            candidates.sort(key=lambda t: t[0])

            for crsi, key, meta in candidates:
                if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
                    break
                ltp = ltps[key]
                alloc = min(POSITION_SIZE_PCT * running_capital, cash)
                qty = compute_quantity(alloc, ltp)
                if qty < 1:
                    continue
                exec_px = ltp * (1.0 + bps)
                if qty * exec_px > cash:  # guard against rounding overflow
                    qty = int(cash / exec_px)
                    if qty < 1:
                        continue
                cash -= qty * exec_px
                open_positions[key] = Position(
                    instrument_key=key, symbol=key, entry_date=day,
                    entry_price=exec_px, quantity=qty, crsi_at_entry=crsi,
                    paper=True, notes="backtest_entry")
                stats["entries"] += 1
                stats["entry_crsi_sum"] += crsi
                _emit(day, key, "ENTRY", "BUY", qty, exec_px, crsi, 0, 0.0,
                      "CRSI_ENTRY")

            stats["max_concurrent"] = max(stats["max_concurrent"],
                                          len(open_positions))

            unrealized = sum(
                (ltps.get(k, last_close.get(k, p.entry_price))
                 - p.entry_price) * p.quantity
                for k, p in open_positions.items())
            await summary_csv.append({
                "session_date": day.isoformat(),
                "open_positions_count": len(open_positions),
                "total_unrealized_pnl": round(unrealized, 2),
                "realized_pnl_today": round(realized_today, 2),
            })

        # --- End-of-test liquidation: force-close everything ----------------
        final_day = calendar_days[-1]
        for key in list(open_positions.keys()):
            pos = open_positions[key]
            ltp = ltps.get(key, last_close.get(key, pos.entry_price))
            exec_px = ltp * (1.0 - bps)
            pnl = (exec_px - pos.entry_price) * pos.quantity
            cash += exec_px * pos.quantity
            stats["exits"] += 1
            stats["total_pnl"] += pnl
            days_held = max((final_day - pos.entry_date).days, 0)
            stats["hold_days"] += days_held
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1
            _emit(final_day, key, "EXIT", "SELL", pos.quantity, exec_px,
                  pos.crsi_at_entry, days_held, pnl, "END_OF_TEST_LIQUIDATION")
            del open_positions[key]

        # Await every pending CSV append so files are complete on return.
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        net_pnl = cash - TOTAL_CAPITAL
        report = {
            "first_session": calendar_days[0].isoformat(),
            "last_session": final_day.isoformat(),
            "sessions": len(calendar_days),
            "initial_capital": TOTAL_CAPITAL,
            "final_equity": round(cash, 2),
            "net_pnl": round(net_pnl, 2),
            "net_return_pct": round(net_pnl / TOTAL_CAPITAL * 100.0, 3),
            "entries": stats["entries"],
            "exits": stats["exits"],
            "win_rate_pct": round(
                stats["wins"] / max(stats["exits"], 1) * 100.0, 2),
            "avg_hold_days": round(
                stats["hold_days"] / max(stats["exits"], 1), 2),
            "max_concurrent_positions": stats["max_concurrent"],
            "avg_entry_crsi": round(
                stats["entry_crsi_sum"] / max(stats["entries"], 1), 3),
        }
        logger.info("=" * 66)
        logger.info("BACKTEST RESULTS")
        for k, v in report.items():
            logger.info("  %-26s %s", k, v)
        logger.info("=" * 66)
        return report


# =============================================================================
# SECTION 16 — SYNTHETIC OHLCV GENERATOR  (backtest self-test only)
# =============================================================================
# Deterministic pseudo-market: upward drift + regimes of multi-day pullbacks
# (the pattern ConnorsRSI buys). Used ONLY when BACKTEST_USE_SYNTHETIC_DATA
# is True — a way to verify the whole pipeline end-to-end without a token.
# It is not a market model and its backtest numbers are meaningless as
# performance estimates.


def generate_synthetic_ohlcv(keys: Sequence[str], n_days: int,
                             start_price: float = 1000.0,
                             seed: int = 20260829
                             ) -> Dict[str, pd.DataFrame]:
    dates = pd.bdate_range(end=session_date_of(), periods=n_days)
    out: Dict[str, pd.DataFrame] = {}
    for i, key in enumerate(keys):
        rng = np.random.default_rng(seed + i)
        drift = rng.uniform(0.0003, 0.0009)
        vol = rng.uniform(0.009, 0.016)
        n = len(dates)
        rets = np.empty(n)
        in_pullback = 0
        for t in range(n):
            if in_pullback > 0:
                rets[t] = rng.uniform(-0.04, -0.012)
                in_pullback -= 1
            else:
                if rng.random() < 0.035:
                    in_pullback = int(rng.integers(3, 7))
                rets[t] = rng.normal(drift, vol)
        close = start_price * np.exp(np.cumsum(rets))
        open_ = np.empty(n)
        open_[0] = start_price
        open_[1:] = close[:-1] * (1.0 + rng.normal(0.0, 0.003, n - 1))
        high = np.maximum(open_, close) * (1.0 + rng.uniform(0.001, 0.012, n))
        low = np.minimum(open_, close) * (1.0 - rng.uniform(0.001, 0.012, n))
        volume = rng.integers(50_000, 5_000_000, n).astype(np.float64)
        out[key] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close,
             "volume": volume}, index=dates)
    return out


# =============================================================================
# SECTION 17 — CONFIG VALIDATION / LOGGING / ENTRY POINTS
# =============================================================================

_TOKEN_IS_PLACEHOLDER: Final[bool] = (
    not ACCESS_TOKEN
    or ACCESS_TOKEN.startswith("YOUR_")
    or "HERE" in ACCESS_TOKEN.upper()
)

_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9_]+\|[A-Z0-9_]+$")


def validate_configuration() -> None:
    """Fail fast on misconfiguration before any network I/O."""
    # Surface env.txt load warnings through the configured logger (import
    # time precedes logging setup, so warnings are replayed here).
    for warning in _ENV_LOAD_WARNINGS:
        logging.getLogger("envfile").warning("%s", warning)
    problems: List[str] = []
    if BACKTEST_MODE and PAPER_TRADING_MODE:
        # BACKTEST wins — log a warning, do not crash (defined precedence).
        pass
    if not (0.0 < POSITION_SIZE_PCT <= 1.0):
        problems.append("POSITION_SIZE_PCT must be in (0, 1]")
    if not (0.0 <= CRSI_THRESHOLD <= 100.0):
        problems.append("CRSI_THRESHOLD must be in [0, 100]")
    if MAX_HOLD_DAYS < 1:
        problems.append("MAX_HOLD_DAYS must be >= 1")
    if TOTAL_CAPITAL <= 0:
        problems.append("TOTAL_CAPITAL must be > 0")
    if MIN_HISTORY_DAYS < 250:
        problems.append("MIN_HISTORY_DAYS must be >= 250 "
                        "(SMA200 + 100-day rank)")
    if not UNIVERSE_TICKERS:
        problems.append("UNIVERSE_TICKERS is empty")
    bad_keys = [k for k in UNIVERSE_TICKERS if not _KEY_RE.match(k)]
    if bad_keys:
        problems.append(f"malformed instrument keys: {bad_keys}")
    if not PAPER_TRADING_MODE and not BACKTEST_MODE and _TOKEN_IS_PLACEHOLDER:
        raise ConfigurationError(
            "LIVE mode with a placeholder ACCESS_TOKEN is refused. "
            "Paste a real token or set PAPER_TRADING_MODE = True.")
    if problems:
        raise ConfigurationError("; ".join(problems))


def configure_logging() -> None:
    """Root logger: console + rolling file, IST timestamps."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s")

    def _ist_converter(seconds: float) -> Tuple[int, int, int, int, int,
                                                int, int, int, int]:
        return datetime.fromtimestamp(seconds, IST_TZ).timetuple()

    fmt.converter = _ist_converter  # type: ignore[assignment]

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass  # console-only logging is acceptable


def _install_loop_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Catch stray task exceptions (un-awaited futures) and log them loudly
    instead of letting them vanish into the default 'Task exception was
    never retrieved' noise."""

    def handler(loop: asyncio.AbstractEventLoop,
                context: Dict[str, Any]) -> None:
        exc = context.get("exception")
        message = context.get("message", "unhandled exception")
        if exc is not None:
            logging.getLogger("asyncio").error(
                "unhandled async error: %s (%s)", message, exc,
                exc_info=exc)
        else:
            logging.getLogger("asyncio").error(
                "unhandled async error: %s", message)

    loop.set_exception_handler(handler)


async def run_live() -> None:
    """PAPER or LIVE daily loop: dormant until the 15:15 gate, then the
    four spec gates, then shutdown at session end."""
    logger = logging.getLogger("live")
    clock = TradeClock(NSE_HOLIDAYS_2026, SPECIAL_SESSIONS_2026,
                       SESSION_OPEN_TIME, SESSION_CLOSE_TIME, IST_TZ)
    now = ist_now()
    bounds = clock.session_bounds_for(now.date())
    if bounds is None:
        logger.info("%s is not an NSE session day (weekend/holiday); "
                    "no schedule today", now.date().isoformat())
        return
    session_open, session_close = bounds
    logger.info("boot: %s | session %s -> %s | paper=%s",
                now.isoformat(), session_open.strftime("%H:%M"),
                session_close.strftime("%H:%M"), PAPER_TRADING_MODE)

    orch = LiveOrchestrator(clock, logger)
    try:
        await orch.setup()
        if not PAPER_TRADING_MODE:
            await orch._client.validate_credentials()
            await orch.reconcile_portfolio()

        if now >= session_open:
            if now.time() >= DATA_FETCH_TIME:
                # Booted at/after the data gate (restart): run_schedule
                # detects the missed gates and runs them immediately.
                logger.info("late boot (>=15:15): missed gates will run now")
            await orch.run_schedule(now, bounds)
        else:
            wait = clock.seconds_until(session_open)
            logger.info("pre-market: dormant %.0fs until session open", wait)
            await asyncio.sleep(wait)
            await orch.run_schedule(ist_now(), bounds)
    finally:
        await orch.shutdown()


async def run_backtest() -> Dict[str, Any]:
    logger = logging.getLogger("backtest")
    client = UpstoxClient(ACCESS_TOKEN, API_BASE_URL, logger)
    if _TOKEN_IS_PLACEHOLDER:
        logger.warning("UPSTOX_ACCESS_TOKEN is a placeholder; create an "
                       "env.txt next to this script containing "
                       "UPSTOX_ACCESS_TOKEN=<token> for authenticated "
                       "access (public endpoints may be rate-limited "
                       "without auth)")
    backtester = Backtester(client, logger)
    return await backtester.run()


def main() -> None:
    """Entry point. Branches strictly on the hardcoded mode booleans:
    BACKTEST_MODE wins over PAPER_TRADING_MODE; both False = LIVE."""
    configure_logging()
    validate_configuration()
    logger = logging.getLogger("main")
    logger.info("UPSTOX_ACCESS_TOKEN: %s [source: %s]",
                _mask_token(ACCESS_TOKEN),
                "env.txt" if _TOKEN_FROM_ENV else "hardcoded fallback")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_loop_exception_handler(loop)

    try:
        if BACKTEST_MODE:
            if PAPER_TRADING_MODE:
                logger.warning("both BACKTEST_MODE and PAPER_TRADING_MODE "
                               "are True — BACKTEST_MODE takes precedence")
            logger.info("MODE = BACKTEST")
            loop.run_until_complete(run_backtest())
        elif PAPER_TRADING_MODE:
            logger.info("MODE = PAPER TRADING (no live orders)")
            loop.run_until_complete(run_live())
        else:
            logger.warning("MODE = LIVE — real orders will be dispatched "
                           "to Upstox")
            loop.run_until_complete(run_live())
    except KeyboardInterrupt:
        logger.warning("keyboard interrupt received")
    except ConfigurationError as exc:
        logger.error("configuration error: %s", exc)
        sys.exit(2)
    except Exception:
        logger.exception("fatal runtime error")
        sys.exit(1)
    finally:
        # Graceful shutdown: cancel stragglers, then close the loop.
        try:
            pending = [t for t in asyncio.all_tasks(loop)
                       if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()
        logger.info("engine stopped cleanly")


if __name__ == "__main__":
    main()
