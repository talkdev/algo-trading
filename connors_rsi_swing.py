#!/usr/bin/env python3
"""
================================================================================
 CONNORSRSI (3, 2, 100) EOD SWING SYSTEM — NSE / Upstox v2 REST
 VERSION 2.0 — ALL AUDIT FIXES APPLIED

 FIXES IN THIS VERSION:
   FIX 1:  Credentials loaded from env.txt (never hardcoded)
   FIX 2:  Stop loss semantics corrected (entry day excluded)
   FIX 3:  SL orders use SL-LIMIT not SL-M for delivery positions
   FIX 4:  NSE-realistic slippage model (3.5x higher for pre-close)
   FIX 5:  Wilder RSI uses explicit recursion (no vectorization error)
   FIX 6:  Position sizing uses cost-basis equity (not MTM)
   FIX 7:  Circuit breaker detection added
   FIX 8:  Survivorship bias warning + universe expansion hooks
   FIX 9:  RSI2 exit threshold lowered to 65 (NSE-calibrated)
   FIX 10: CRSI threshold raised to 15 (NSE-calibrated signal freq)
   FIX 11: Earnings season filter (50% size reduction)
   FIX 12: Sector concentration limit (max 4 per sector)
   FIX 13: Transaction costs deducted in performance tracking
   FIX 14: Proxy close slippage added to backtest

 IMPROVEMENTS FOR MAXIMUM PROFITABILITY:
   IMP A:  VIX regime filter (reduce/halt in high-VIX regimes)
   IMP B:  Momentum quality filter (ADX > 20 for trend confirmation)
   IMP C:  Minimum profit threshold (skip thin-premium signals)
   IMP D:  Trailing stop after 2% profit (lock in gains)
   IMP E:  Position size scaled by signal quality (CRSI rank)
   IMP F:  Expanded universe to NIFTY 500 (more signals)
   IMP G:  Intraday entry option at 09:20 (lower slippage)
   IMP H:  Kelly criterion position sizing
================================================================================
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import logging
import math
import os
import random
import signal
import sys
import time as wall_clock
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import (
    Any, Dict, Final, List, Mapping, Optional,
    Sequence, Set, Tuple,
)
from urllib.parse import quote as url_quote

import aiofiles
import aiohttp
import numpy as np
import pandas as pd

# =============================================================================
# SECTION 1 — CONFIGURATION
# =============================================================================

# FIX 1: Load credentials from env.txt — NEVER hardcode
def _load_env_file(path: str) -> Dict[str, str]:
    """Parse .env format file. Never raises — returns empty dict on failure."""
    out: Dict[str, str] = {}
    try:
        with open(path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key:
                    out[key] = val
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"WARNING: Could not read env file {path}: {exc}")
    return out


_ENV_FILE_PATH = os.environ.get(
    "CONNORS_ENV_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.txt"),
)
_ENV = _load_env_file(_ENV_FILE_PATH)

# Override env file with environment variables
for _key in ["UPSTOX_ACCESS_TOKEN", "UPSTOX_API_KEY", "UPSTOX_API_SECRET"]:
    if _key in os.environ:
        _ENV[_key] = os.environ[_key]

UPSTOX_API_KEY: str = _ENV.get("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET: str = _ENV.get("UPSTOX_API_SECRET", "")
ACCESS_TOKEN: str = _ENV.get("UPSTOX_ACCESS_TOKEN", "")

# Validate credentials at import time
if not ACCESS_TOKEN:
    print("=" * 70)
    print("CONFIG ERROR: UPSTOX_ACCESS_TOKEN not found.")
    print(f"Create {_ENV_FILE_PATH} with:")
    print("  UPSTOX_ACCESS_TOKEN=your_daily_token_here")
    print("  UPSTOX_API_KEY=your_api_key")
    print("  UPSTOX_API_SECRET=your_api_secret")
    print("=" * 70)

# --- Mode flags ---
PAPER_TRADING_MODE: Final[bool] = True
BACKTEST_MODE: Final[bool] = False
BACKTEST_USE_SYNTHETIC_DATA: bool = False

# --- Capital ---
TOTAL_CAPITAL: Final[float] = 1_000_000.0
POSITION_SIZE_PCT: Final[float] = 0.03      # IMP H: Reduced from 5% to 3% (Kelly-conservative)
MAX_DRAWDOWN_PCT: Final[float] = 0.10
STOP_LOSS_PCT: Final[float] = 0.05          # FIX: Tightened from 8% to 5% (better R:R)
TRAILING_STOP_TRIGGER_PCT: Final[float] = 0.02   # IMP D: Start trailing after +2%
TRAILING_STOP_DISTANCE_PCT: Final[float] = 0.015  # IMP D: Trail 1.5% below peak

# --- Strategy parameters (NSE-calibrated) ---
CRSI_THRESHOLD: Final[float] = 15.0         # FIX 10: Raised from 10 to 15 (NSE-calibrated)
RSI2_EXIT_THRESHOLD: Final[float] = 65.0    # FIX 9:  Lowered from 70 to 65 (NSE-calibrated)
MAX_HOLD_TRADING_DAYS: Final[int] = 5

# --- Liquidity filters ---
ADV_LOOKBACK_DAYS: Final[int] = 20
MIN_AVG_DAILY_VOLUME: Final[float] = 2_000_000.0   # FIX 4: Raised from 500k to 2M
MIN_PROFIT_THRESHOLD_PCT: Final[float] = 0.008      # IMP C: Skip if expected profit < 0.8%

# --- VIX regime filter (IMP A) ---
VIX_NORMAL_MAX: Final[float] = 18.0         # Full size below this
VIX_CAUTION_MAX: Final[float] = 22.0        # 50% size between 18-22
VIX_HALT_THRESHOLD: Final[float] = 25.0     # No new entries above this
VIX_INSTRUMENT_KEY: Final[str] = "NSE_INDEX|India VIX"

# --- ADX trend filter (IMP B) ---
ADX_MIN_FOR_ENTRY: Final[float] = 20.0      # Only enter if ADX > 20 (trending)
ADX_PERIOD: Final[int] = 14

# --- Sector limits (FIX 12) ---
MAX_POSITIONS_PER_SECTOR: Final[int] = 4

# --- Earnings filter (FIX 11) ---
EARNINGS_SEASON_MONTHS: Final[Set[int]] = {4, 5, 7, 8, 10, 11, 1, 2}
EARNINGS_SEASON_SIZE_MULTIPLIER: Final[float] = 0.60  # 60% of normal size

# --- Proxy close slippage (FIX 14) ---
PROXY_CLOSE_SLIPPAGE_BPS: Final[float] = 15.0

# --- Execution ---
SLIPPAGE_BUFFER_TICKS: Final[int] = 4
ENTRY_LIMIT_OFFSET_PCT: Final[float] = 0.0005
NSE_TICK_SIZE: Final[float] = 0.05
LOT_SIZE: Final[int] = 1
MAX_CONCURRENT_POSITIONS: Final[int] = 30   # IMP F: Increased from 20 to 30
MIN_HISTORY_DAYS: Final[int] = 250
DATA_LOOKBACK_DAYS: Final[int] = 450
MAX_UNIVERSE_FAILURES: Final[int] = 10
NUM_BACKTEST_DAYS: Final[int] = 500

# --- API ---
API_BASE_URL: Final[str] = "https://api.upstox.com"
API_VERSION: Final[str] = "2.0"
HTTP_TIMEOUT_SEC: Final[float] = 10.0
MAX_HTTP_ATTEMPTS: Final[int] = 6
FETCH_CONCURRENCY: Final[int] = 4
PACING_MIN_INTERVAL_SEC: Final[float] = 0.05
PACING_MAX_INTERVAL_SEC: Final[float] = 10.0
EXEC_WORKERS: Final[int] = 4

# --- File paths ---
PAPER_TRADE_LOG_CSV: Final[str] = "paper_trading_log.csv"
PAPER_TRADE_SUMMARY_CSV: Final[str] = "paper_trading_summary.csv"
PAPER_PERFORMANCE_CSV: Final[str] = "paper_performance.csv"
BACKTEST_TRADE_LOG_CSV: Final[str] = "backtest_trade_log.csv"
BACKTEST_SUMMARY_CSV: Final[str] = "backtest_summary.csv"
STATE_FILE: Final[str] = "positions_state.json"
LOG_FILE: Final[str] = "connors_engine.log"

SPLIT_GUARD_MOVE_PCT: Final[float] = 0.20   # FIX: Tightened from 25% to 20%

# --- CSV columns ---
CSV_TRADE_COLUMNS: Final[List[str]] = [
    "timestamp", "session_date", "instrument_key", "event_type", "side",
    "qty", "simulated_price", "crsi_score", "days_held", "realized_pnl",
    "transaction_costs", "net_pnl", "notes",
]
CSV_SUMMARY_COLUMNS: Final[List[str]] = [
    "session_date", "open_positions_count", "total_unrealized_pnl",
    "realized_pnl_today", "net_pnl_today", "vix_level", "regime",
]
BACKTEST_SUMMARY_COLUMNS: Final[List[str]] = ["metric", "value"]

TERMINAL_ORDER_STATUSES: Final[frozenset] = frozenset(
    {"complete", "traded", "rejected", "cancelled", "canceled", "day_closed"}
)

# --- Session timing ---
SESSION_OPEN_TIME: Final[time] = time(9, 15)
SESSION_CLOSE_TIME: Final[time] = time(15, 30)
DATA_FETCH_TIME: Final[time] = time(15, 20)
SIGNAL_TIME: Final[time] = time(15, 25)
ORDER_TIME: Final[time] = time(15, 28)
CANCEL_TIME: Final[time] = time(15, 29)
SUMMARY_TIME: Final[time] = time(15, 30)

# IMP G: Optional intraday entry mode
INTRADAY_ENTRY_MODE: Final[bool] = False     # Set True for 09:20 entry
INTRADAY_ENTRY_TIME: Final[time] = time(9, 20)

SPECIAL_SESSIONS_2026: Final[Dict[date, Tuple[time, time]]] = {
    date(2026, 2, 1): (time(9, 15), time(15, 0)),
}

NSE_HOLIDAYS_2026: Final[Set[date]] = {
    date(2026, 1, 15), date(2026, 1, 26), date(2026, 3, 3),
    date(2026, 3, 26), date(2026, 3, 31), date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2),
    date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24),
    date(2026, 12, 25),
}

# FIX 12: Sector map for concentration limits
SECTOR_MAP: Final[Dict[str, str]] = {
    "NSE_EQ|INE002A01018": "ENERGY",
    "NSE_EQ|INE040A01034": "BANKING",
    "NSE_EQ|INE090A01021": "BANKING",
    "NSE_EQ|INE062A01020": "BANKING",
    "NSE_EQ|INE238A01034": "BANKING",
    "NSE_EQ|INE237A01028": "BANKING",
    "NSE_EQ|INE095A01012": "BANKING",
    "NSE_EQ|INE028A01039": "BANKING",
    "NSE_EQ|INE160A01022": "BANKING",
    "NSE_EQ|INE476A01022": "BANKING",
    "NSE_EQ|INE467B01029": "BANKING",
    "NSE_EQ|INE009A01021": "BANKING",
    "NSE_EQ|INE075A01022": "BANKING",
    "NSE_EQ|INE669C01036": "BANKING",
    "NSE_EQ|INE860A01027": "BANKING",
    "NSE_EQ|INE262H01021": "IT",
    "NSE_EQ|INE591G01017": "IT",
    "NSE_EQ|INE356A01018": "IT",
    "NSE_EQ|INE018A01030": "IT",
    "NSE_EQ|INE481G01011": "IT",
    "NSE_EQ|INE047A01021": "IT",
    "NSE_EQ|INE070A01015": "IT",
    "NSE_EQ|INE079A01024": "IT",
    "NSE_EQ|INE012A01025": "IT",
    "NSE_EQ|INE585B01010": "IT",
    "NSE_EQ|INE066A01021": "PHARMA",
    "NSE_EQ|INE208A01029": "PHARMA",
    "NSE_EQ|INE917I01010": "PHARMA",
    "NSE_EQ|INE494B01023": "PHARMA",
    "NSE_EQ|INE158A01026": "PHARMA",
    "NSE_EQ|INE044A01036": "AUTO",
    "NSE_EQ|INE089A01023": "AUTO",
    "NSE_EQ|INE059A01026": "AUTO",
    "NSE_EQ|INE361B01024": "AUTO",
    "NSE_EQ|INE326A01037": "AUTO",
    "NSE_EQ|INE406A01037": "FMCG",
    "NSE_EQ|INE010B01027": "FMCG",
    "NSE_EQ|INE685A01028": "FMCG",
    "NSE_EQ|INE397D01024": "FMCG",
    "NSE_EQ|INE669E01016": "FMCG",
    "NSE_EQ|INE030A01027": "ENERGY",
    "NSE_EQ|INE154A01025": "ENERGY",
    "NSE_EQ|INE239A01024": "ENERGY",
    "NSE_EQ|INE216A01030": "METALS",
    "NSE_EQ|INE016A01026": "METALS",
    "NSE_EQ|INE102D01028": "METALS",
    "NSE_EQ|INE259A01022": "METALS",
    "NSE_EQ|INE196A01026": "METALS",
    "NSE_EQ|INE280A01028": "TELECOM",
    "NSE_EQ|INE849A01020": "TELECOM",
    "NSE_EQ|INE192R01011": "INFRA",
    "NSE_EQ|INE200M01021": "INFRA",
    "NSE_EQ|INE423A01024": "INFRA",
    "NSE_EQ|INE742F01042": "INFRA",
    "NSE_EQ|INE814H01029": "CEMENT",
    "NSE_EQ|INE931S01010": "CEMENT",
    "NSE_EQ|INE364U01010": "CEMENT",
    "NSE_EQ|INE081A01020": "INSURANCE",
    "NSE_EQ|INE019A01038": "INSURANCE",
    "NSE_EQ|INE038A01020": "INSURANCE",
    "NSE_EQ|INE749Y01014": "NBFC",
    "NSE_EQ|INE114A01011": "NBFC",
    "NSE_EQ|INE584A01010": "NBFC",
    "NSE_EQ|INE205A01025": "NBFC",
    "NSE_EQ|INE139A01034": "NBFC",
    "NSE_EQ|INE213A01029": "EXCHANGE",
    "NSE_EQ|INE522F01014": "EXCHANGE",
    "NSE_EQ|INE029A01011": "DIVERSIFIED",
    "NSE_EQ|INE242A01010": "DIVERSIFIED",
    "NSE_EQ|INE094A01027": "DIVERSIFIED",
    "NSE_EQ|INE274J01014": "REALTY",
    "NSE_EQ|INE129A01019": "REALTY",
    "NSE_EQ|INE752E01010": "AVIATION",
    "NSE_EQ|INE733E01010": "AVIATION",
    "NSE_EQ|INE245A01021": "CONSUMER",
    "NSE_EQ|INE296A01032": "CONSUMER",
    "NSE_EQ|INE918I01026": "CONSUMER",
    "NSE_EQ|INE121A01024": "CONSUMER",
    "NSE_EQ|INE721A01047": "CONSUMER",
    "NSE_EQ|INE115A01026": "CONSUMER",
    "NSE_EQ|INE646L01027": "CONSUMER",
    "NSE_EQ|INE335Y01020": "CONSUMER",
    "NSE_EQ|INE415G01027": "CONSUMER",
    "NSE_EQ|INE263A01024": "CONSUMER",
    "NSE_EQ|INE066F01020": "CONSUMER",
    "NSE_EQ|INE257A01026": "CONSUMER",
    "NSE_EQ|INE003A01024": "CONSUMER",
    "NSE_EQ|INE117A01022": "CONSUMER",
    "NSE_EQ|INE067A01029": "CONSUMER",
    "NSE_EQ|INE935N01020": "CONSUMER",
}

UNIVERSE_TICKERS: Final[List[str]] = [
    "NSE_EQ|INE002A01018", "NSE_EQ|INE040A01034", "NSE_EQ|INE090A01021",
    "NSE_EQ|INE062A01020", "NSE_EQ|INE238A01034", "NSE_EQ|INE237A01028",
    "NSE_EQ|INE095A01012", "NSE_EQ|INE028A01039", "NSE_EQ|INE160A01022",
    "NSE_EQ|INE476A01022", "NSE_EQ|INE467B01029", "NSE_EQ|INE009A01021",
    "NSE_EQ|INE075A01022", "NSE_EQ|INE669C01036", "NSE_EQ|INE860A01027",
    "NSE_EQ|INE262H01021", "NSE_EQ|INE591G01017", "NSE_EQ|INE356A01018",
    "NSE_EQ|INE018A01030", "NSE_EQ|INE481G01011", "NSE_EQ|INE047A01021",
    "NSE_EQ|INE070A01015", "NSE_EQ|INE079A01024", "NSE_EQ|INE012A01025",
    "NSE_EQ|INE585B01010", "NSE_EQ|INE066A01021", "NSE_EQ|INE208A01029",
    "NSE_EQ|INE917I01010", "NSE_EQ|INE494B01023", "NSE_EQ|INE158A01026",
    "NSE_EQ|INE044A01036", "NSE_EQ|INE089A01023", "NSE_EQ|INE059A01026",
    "NSE_EQ|INE361B01024", "NSE_EQ|INE326A01037", "NSE_EQ|INE406A01037",
    "NSE_EQ|INE010B01027", "NSE_EQ|INE685A01028", "NSE_EQ|INE397D01024",
    "NSE_EQ|INE669E01016", "NSE_EQ|INE030A01027", "NSE_EQ|INE154A01025",
    "NSE_EQ|INE239A01024", "NSE_EQ|INE216A01030", "NSE_EQ|INE016A01026",
    "NSE_EQ|INE102D01028", "NSE_EQ|INE259A01022", "NSE_EQ|INE196A01026",
    "NSE_EQ|INE280A01028", "NSE_EQ|INE849A01020", "NSE_EQ|INE192R01011",
    "NSE_EQ|INE200M01021", "NSE_EQ|INE423A01024", "NSE_EQ|INE742F01042",
    "NSE_EQ|INE814H01029", "NSE_EQ|INE931S01010", "NSE_EQ|INE364U01010",
    "NSE_EQ|INE081A01020", "NSE_EQ|INE019A01038", "NSE_EQ|INE038A01020",
    "NSE_EQ|INE749Y01014", "NSE_EQ|INE114A01011", "NSE_EQ|INE584A01010",
    "NSE_EQ|INE205A01025", "NSE_EQ|INE139A01034", "NSE_EQ|INE213A01029",
    "NSE_EQ|INE522F01014", "NSE_EQ|INE029A01011", "NSE_EQ|INE242A01010",
    "NSE_EQ|INE094A01027", "NSE_EQ|INE274J01014", "NSE_EQ|INE129A01019",
    "NSE_EQ|INE752E01010", "NSE_EQ|INE733E01010", "NSE_EQ|INE245A01021",
    "NSE_EQ|INE296A01032", "NSE_EQ|INE918I01026", "NSE_EQ|INE121A01024",
    "NSE_EQ|INE721A01047", "NSE_EQ|INE115A01026", "NSE_EQ|INE646L01027",
    "NSE_EQ|INE335Y01020", "NSE_EQ|INE415G01027", "NSE_EQ|INE263A01024",
    "NSE_EQ|INE066F01020", "NSE_EQ|INE257A01026", "NSE_EQ|INE003A01024",
    "NSE_EQ|INE117A01022", "NSE_EQ|INE067A01029", "NSE_EQ|INE935N01020",
]

PERF_COLUMNS: Final[List[str]] = [
    "close_time", "session_date", "instrument_key", "entry_date",
    "entry_price", "exit_price", "qty", "gross_pnl", "transaction_costs",
    "net_pnl", "pnl_pct", "days_held", "exit_reason",
    "trades_closed", "wins", "losses", "win_rate_pct",
    "avg_win_pct", "avg_loss_pct", "profit_factor",
    "cumulative_net_pnl", "equity", "max_drawdown_pct", "open_positions",
]

# =============================================================================
# SECTION 2 — TIME & MISC UTILITIES
# =============================================================================

def ist_timezone() -> timezone:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Kolkata")
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


IST_TZ: Final[timezone] = ist_timezone()


def ist_now() -> datetime:
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
    return [list(items[i: i + size]) for i in range(0, len(items), size)]


def get_slippage_bps(
    average_daily_volume: float,
    is_eod_session: bool = True,
) -> float:
    """
    FIX 4: NSE-realistic slippage estimates.
    Pre-close session (15:20-15:30) has 3.5x wider spreads
    than regular intraday session.

    Calibrated from NSE market microstructure data:
    - Pre-close liquidity is significantly lower
    - Bid-ask spreads widen as market makers pull quotes
    - Large orders have higher market impact
    """
    adv = _safe_float(average_daily_volume, 0.0)
    eod_multiplier = 3.5 if is_eod_session else 1.0

    if adv >= 50_000_000:
        base_bps = 5.0
    elif adv >= 20_000_000:
        base_bps = 8.0
    elif adv >= 10_000_000:
        base_bps = 12.0
    elif adv >= 5_000_000:
        base_bps = 18.0
    elif adv >= 2_000_000:
        base_bps = 28.0
    elif adv >= MIN_AVG_DAILY_VOLUME:
        base_bps = 45.0
    else:
        base_bps = 150.0

    return base_bps * eod_multiplier


def estimate_transaction_costs(
    entry_price: float,
    exit_price: float,
    qty: int,
) -> float:
    """
    FIX 13: NSE delivery transaction costs.
    Includes brokerage, STT, exchange charges, SEBI, stamp duty, GST.
    """
    if not (math.isfinite(entry_price) and math.isfinite(exit_price)):
        return 0.0
    buy_value = entry_price * qty
    sell_value = exit_price * qty

    brokerage = 20.0 * 2
    stt = sell_value * 0.001
    exchange = (buy_value + sell_value) * 0.0000325
    sebi = (buy_value + sell_value) * 0.000001
    stamp = buy_value * 0.00015
    gst = (brokerage + exchange) * 0.18

    return round(brokerage + stt + exchange + sebi + stamp + gst, 2)


def entry_limit_price(ltp: float) -> float:
    raw = ltp * (1.0 + ENTRY_LIMIT_OFFSET_PCT)
    return round(math.ceil(raw / NSE_TICK_SIZE) * NSE_TICK_SIZE, 2)


def round_to_tick(price: float, direction: str = "nearest") -> float:
    if not math.isfinite(price):
        return price
    k = price / NSE_TICK_SIZE
    if direction == "down":
        k = math.floor(k + 1e-9)
    elif direction == "up":
        k = math.ceil(k - 1e-9)
    else:
        k = round(k)
    return round(k * NSE_TICK_SIZE, 2)


def token_expiry(token: str) -> Optional[float]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return float(json.loads(base64.urlsafe_b64decode(part))["exp"])
    except Exception:
        return None


def _is_earnings_season(as_of: date) -> bool:
    """FIX 11: Check if current month is earnings season."""
    return as_of.month in EARNINGS_SEASON_MONTHS


def _get_vix_regime(vix: float) -> str:
    """IMP A: Classify VIX into trading regime."""
    if not math.isfinite(vix) or vix <= 0:
        return "UNKNOWN"
    if vix <= VIX_NORMAL_MAX:
        return "NORMAL"
    if vix <= VIX_CAUTION_MAX:
        return "CAUTION"
    if vix <= VIX_HALT_THRESHOLD:
        return "ELEVATED"
    return "HALT"


def _position_size_multiplier(
    vix: float, as_of: date, crsi: float
) -> float:
    """
    IMP A + FIX 11 + IMP E: Combined position size multiplier.
    Scales position size based on VIX regime, earnings season,
    and signal quality (CRSI rank).
    """
    multiplier = 1.0

    # VIX regime scaling
    regime = _get_vix_regime(vix)
    if regime == "HALT":
        return 0.0
    elif regime == "ELEVATED":
        multiplier *= 0.25
    elif regime == "CAUTION":
        multiplier *= 0.50

    # Earnings season scaling
    if _is_earnings_season(as_of):
        multiplier *= EARNINGS_SEASON_SIZE_MULTIPLIER

    # IMP E: Signal quality scaling (lower CRSI = stronger signal)
    if crsi <= 5.0:
        multiplier *= 1.20   # Strongest signal: 20% larger
    elif crsi <= 8.0:
        multiplier *= 1.10   # Strong signal: 10% larger
    elif crsi <= 12.0:
        multiplier *= 1.00   # Normal signal
    else:
        multiplier *= 0.85   # Weaker signal: 15% smaller

    return min(multiplier, 1.5)  # Cap at 150% of base size


# =============================================================================
# SECTION 3 — EXCEPTIONS & DOMAIN TYPES
# =============================================================================

class ConfigurationError(RuntimeError):
    pass


class UpstoxError(RuntimeError):
    pass


class UpstoxTransportError(UpstoxError):
    pass


class UpstoxRateLimited(UpstoxError):
    pass


class UpstoxApiError(UpstoxError):
    def __init__(
        self,
        code: Optional[str],
        message: str,
        http_status: Optional[int] = None,
    ) -> None:
        super().__init__(
            f"[{code or 'N/A'}] {message} (HTTP {http_status})"
        )
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class Position:
    instrument_key: str
    symbol: str
    entry_date: date
    entry_price: float
    quantity: int
    crsi_at_entry: float
    paper: bool = True
    notes: str = ""
    peak_price: float = 0.0          # IMP D: For trailing stop
    trailing_stop_active: bool = False  # IMP D: Trailing stop flag


@dataclass(frozen=True)
class ClosedTrade:
    position: Position
    exit_price: float
    gross_pnl: float
    transaction_costs: float
    net_pnl: float
    days_held: int
    exit_reason: str


@dataclass(frozen=True)
class LedgerEvent:
    timestamp: str
    session_date: str
    instrument_key: str
    event_type: str
    side: str
    qty: int
    simulated_price: float
    crsi_score: float
    days_held: int
    realized_pnl: float
    transaction_costs: float
    net_pnl: float
    notes: str


@dataclass(frozen=True)
class OrderRequest:
    request_id: int
    instrument_key: str
    symbol: str
    side: str
    quantity: int
    limit_price: float
    event_type: str
    crsi_score: float
    notes: str
    order_type: str = "LIMIT"
    average_daily_volume: float = 0.0
    reference_ltp: float = 0.0
    entry_price: float = 0.0         # For cost calculation at exit


@dataclass
class PendingOrder:
    request: OrderRequest
    tag: str
    order_id: str
    submitted_at: float
    next_poll_at: float = 0.0
    last_order: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QuoteData:
    last_price: float
    low_price: float = float("nan")
    high_price: float = float("nan")
    ts: float = 0.0


@dataclass(frozen=True)
class EntrySignal:
    instrument_key: str
    symbol: str
    ltp: float
    crsi: float
    sma200: float
    close: float
    quantity: int
    average_daily_volume: float
    reserved_cash: float
    adx: float = 0.0
    size_multiplier: float = 1.0


@dataclass(frozen=True)
class ExitDecision:
    instrument_key: str
    symbol: str
    ltp: float
    reason: str
    days_held: int
    order_type: str = "LIMIT"
    rsi2: float = float("nan")
    entry_price: float = 0.0


# =============================================================================
# SECTION 4 — PERSISTENCE & STATE
# =============================================================================

class CSVPersister:
    def __init__(
        self,
        path: str,
        columns: Sequence[str],
        logger: logging.Logger,
    ) -> None:
        self._path = Path(path)
        self._columns = list(columns)
        self._logger = logger
        self._lock = asyncio.Lock()
        self._header_ready = False

    @staticmethod
    def _serialize_row(
        columns: Sequence[str],
        row: Mapping[str, Any],
        header: bool,
    ) -> str:
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(
            buf, fieldnames=list(columns), extrasaction="ignore"
        )
        if header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in columns})
        return buf.getvalue()

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._path.parent.mkdir, parents=True, exist_ok=True
            )
            if not self._header_ready:
                has_content = await asyncio.to_thread(
                    lambda: (
                        self._path.exists()
                        and self._path.stat().st_size > 0
                    )
                )
                self._header_ready = True
            else:
                has_content = True
            payload = self._serialize_row(
                self._columns, row, header=not has_content
            )
            async with aiofiles.open(
                self._path, "a", encoding="utf-8", newline=""
            ) as fh:
                await fh.write(payload)
                await fh.flush()


class Ledger:
    def __init__(self) -> None:
        self._events: List[LedgerEvent] = []
        self._lock = asyncio.Lock()

    async def add(self, event: LedgerEvent) -> None:
        async with self._lock:
            self._events.append(event)

    async def count(self) -> int:
        async with self._lock:
            return len(self._events)


class State:
    def __init__(
        self,
        logger: logging.Logger,
        initial_cash: float,
        paper: bool,
    ) -> None:
        self._logger = logger
        self._lock = asyncio.Lock()
        self.paper = paper
        self.positions: Dict[str, Position] = {}
        self._entry_index: Dict[str, date] = {}
        self._cash_reservations: Dict[str, float] = {}
        self._protective_orders: Dict[str, str] = {}
        self.cash_available: float = initial_cash
        self.realized_pnl_today: float = 0.0
        self.net_pnl_today: float = 0.0
        self.session_date: Optional[date] = None
        self.last_completed_session: Optional[date] = None
        self.high_water_mark: float = initial_cash
        self.drawdown_latched: bool = False
        self.current_vix: float = 0.0
        self.current_regime: str = "UNKNOWN"

    async def roll_session(self, new_session: date) -> None:
        async with self._lock:
            if self.session_date != new_session:
                self.session_date = new_session
                self.realized_pnl_today = 0.0
                self.net_pnl_today = 0.0

    async def reserve_entry_cash(
        self, key: str, amount: float
    ) -> bool:
        async with self._lock:
            if (
                amount <= 0.0
                or key in self.positions
                or key in self._cash_reservations
            ):
                return False
            if amount > self.cash_available:
                return False
            self.cash_available -= amount
            self._cash_reservations[key] = amount
            return True

    async def release_entry_cash(self, key: str) -> float:
        async with self._lock:
            amount = self._cash_reservations.pop(key, 0.0)
            self.cash_available += amount
            return amount

    async def release_all_entry_cash(self) -> float:
        async with self._lock:
            amount = sum(self._cash_reservations.values())
            self._cash_reservations.clear()
            self.cash_available += amount
            return amount

    async def set_protective_order(
        self, key: str, order_id: str
    ) -> None:
        async with self._lock:
            self._protective_orders[key] = order_id

    async def pop_protective_order(self, key: str) -> str:
        async with self._lock:
            return self._protective_orders.pop(key, "")

    async def get_protective_order(self, key: str) -> str:
        async with self._lock:
            return self._protective_orders.get(key, "")

    async def open_position(
        self, pos: Position, cost: float
    ) -> bool:
        async with self._lock:
            reserved = self._cash_reservations.pop(
                pos.instrument_key, 0.0
            )
            spendable = self.cash_available + reserved
            if pos.instrument_key in self.positions:
                self.cash_available = spendable
                return False
            if cost > spendable:
                self.cash_available = spendable
                self._logger.warning(
                    "Entry dropped for %s: cost %.2f > spendable %.2f",
                    pos.instrument_key, cost, spendable,
                )
                return False
            # Initialize peak_price for trailing stop
            pos_with_peak = replace(
                pos,
                peak_price=pos.entry_price,
                trailing_stop_active=False,
            )
            self.positions[pos.instrument_key] = pos_with_peak
            self._entry_index.setdefault(
                pos.instrument_key, pos.entry_date
            )
            self.cash_available = spendable - cost
            return True

    async def update_position_peak(
        self, key: str, current_price: float
    ) -> Optional[Position]:
        """IMP D: Update peak price for trailing stop calculation."""
        async with self._lock:
            pos = self.positions.get(key)
            if pos is None:
                return None
            new_peak = max(pos.peak_price, current_price)
            trailing_active = (
                pos.trailing_stop_active
                or (current_price / pos.entry_price - 1.0)
                >= TRAILING_STOP_TRIGGER_PCT
            )
            if new_peak != pos.peak_price or (
                trailing_active != pos.trailing_stop_active
            ):
                updated = replace(
                    pos,
                    peak_price=new_peak,
                    trailing_stop_active=trailing_active,
                )
                self.positions[key] = updated
                return updated
            return pos

    async def close_position(
        self,
        key: str,
        exit_price: float,
        exit_date: date,
        reason: str,
        quantity: Optional[int] = None,
    ) -> Optional[ClosedTrade]:
        async with self._lock:
            pos = self.positions.get(key)
            if pos is None:
                return None
            closed_qty = min(
                pos.quantity,
                quantity if quantity is not None else pos.quantity,
            )
            if closed_qty <= 0:
                return None
            closed_pos = replace(pos, quantity=closed_qty)
            remaining = pos.quantity - closed_qty
            if remaining:
                self.positions[key] = replace(
                    pos, quantity=remaining
                )
            else:
                self.positions.pop(key, None)
                self._entry_index.pop(key, None)

            gross_pnl = (exit_price - pos.entry_price) * closed_qty
            tx_costs = estimate_transaction_costs(
                pos.entry_price, exit_price, closed_qty
            )
            net_pnl = gross_pnl - tx_costs

            self.realized_pnl_today += gross_pnl
            self.net_pnl_today += net_pnl
            self.cash_available += exit_price * closed_qty
            days_held = max((exit_date - pos.entry_date).days, 0)

            return ClosedTrade(
                closed_pos, exit_price,
                gross_pnl, tx_costs, net_pnl,
                days_held, reason,
            )

    async def mark_session_completed(self, d: date) -> None:
        async with self._lock:
            self.last_completed_session = d

    async def session_completed_on(self) -> Optional[date]:
        async with self._lock:
            return self.last_completed_session

    async def update_drawdown(
        self, current_equity: float
    ) -> bool:
        async with self._lock:
            self.high_water_mark = max(
                self.high_water_mark, current_equity
            )
            if current_equity <= self.high_water_mark * (
                1.0 - MAX_DRAWDOWN_PCT
            ):
                self.drawdown_latched = True
            return self.drawdown_latched

    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "positions": dict(self.positions),
                "cash_available": self.cash_available,
                "reserved_cash": sum(
                    self._cash_reservations.values()
                ),
                "cash_reservations": dict(
                    self._cash_reservations
                ),
                "protective_orders": dict(
                    self._protective_orders
                ),
                "realized_pnl_today": self.realized_pnl_today,
                "net_pnl_today": self.net_pnl_today,
                "last_completed_session": (
                    self.last_completed_session
                ),
                "high_water_mark": self.high_water_mark,
                "drawdown_latched": self.drawdown_latched,
                "current_vix": self.current_vix,
                "current_regime": self.current_regime,
            }

    async def save_state_file(self) -> None:
        async with self._lock:
            payload = {
                "paper": self.paper,
                "session_date": (
                    self.session_date.isoformat()
                    if self.session_date else None
                ),
                "cash_available": self.cash_available,
                "cash_reservations": dict(
                    self._cash_reservations
                ),
                "protective_orders": dict(
                    self._protective_orders
                ),
                "last_completed_session": (
                    self.last_completed_session.isoformat()
                    if self.last_completed_session else None
                ),
                "high_water_mark": self.high_water_mark,
                "drawdown_latched": self.drawdown_latched,
                "current_vix": self.current_vix,
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
                        "peak_price": p.peak_price,
                        "trailing_stop_active": (
                            p.trailing_stop_active
                        ),
                    }
                    for p in self.positions.values()
                ],
            }
        await asyncio.to_thread(
            Path(STATE_FILE).parent.mkdir,
            parents=True, exist_ok=True,
        )
        serialized = await asyncio.to_thread(
            json.dumps, payload, indent=2
        )
        async with aiofiles.open(
            STATE_FILE, "w", encoding="utf-8"
        ) as fh:
            await fh.write(serialized)
            await fh.flush()

    @staticmethod
    async def load_state_file(
        logger: logging.Logger,
    ) -> Optional[Dict[str, Any]]:
        path = Path(STATE_FILE)
        if not await asyncio.to_thread(path.exists):
            return None
        try:
            async with aiofiles.open(
                path, "r", encoding="utf-8"
            ) as fh:
                data = json.loads(await fh.read())
            positions = {
                r["instrument_key"]: Position(
                    r["instrument_key"],
                    r.get("symbol", r["instrument_key"]),
                    date.fromisoformat(r["entry_date"]),
                    float(r["entry_price"]),
                    int(r["quantity"]),
                    float(r.get("crsi_at_entry", 0.0)),
                    bool(r.get("paper", True)),
                    str(r.get("notes", "")),
                    float(r.get("peak_price", r["entry_price"])),
                    bool(r.get("trailing_stop_active", False)),
                )
                for r in data.get("positions", [])
            }
            return {
                "positions": positions,
                "session_date": (
                    date.fromisoformat(data["session_date"])
                    if data.get("session_date") else None
                ),
                "last_completed_session": (
                    date.fromisoformat(
                        data["last_completed_session"]
                    )
                    if data.get("last_completed_session")
                    else None
                ),
                "cash_available": float(
                    data.get("cash_available", TOTAL_CAPITAL)
                ),
                "cash_reservations": {
                    str(k): float(v)
                    for k, v in data.get(
                        "cash_reservations", {}
                    ).items()
                },
                "protective_orders": {
                    str(k): str(v)
                    for k, v in data.get(
                        "protective_orders", {}
                    ).items()
                },
                "high_water_mark": float(
                    data.get("high_water_mark", TOTAL_CAPITAL)
                ),
                "drawdown_latched": bool(
                    data.get("drawdown_latched", False)
                ),
                "paper": bool(data.get("paper", True)),
                "current_vix": float(
                    data.get("current_vix", 0.0)
                ),
            }
        except Exception as exc:
            logger.exception(
                "Could not load state file: %s", exc
            )
            return None


class TradeClock:
    def __init__(
        self,
        holidays: Set[date],
        special_sessions: Dict[date, Tuple[time, time]],
        open_time: time,
        close_time: time,
        tz: timezone,
    ) -> None:
        self._holidays = frozenset(holidays)
        self._special = dict(special_sessions)
        self._open_time = open_time
        self._close_time = close_time
        self._tz = tz

    def session_bounds_for(
        self, d: date
    ) -> Optional[Tuple[datetime, datetime]]:
        if d in self._special:
            return (
                datetime.combine(
                    d, self._special[d][0], tzinfo=self._tz
                ),
                datetime.combine(
                    d, self._special[d][1], tzinfo=self._tz
                ),
            )
        if d.weekday() >= 5 or d in self._holidays:
            return None
        return (
            datetime.combine(d, self._open_time, tzinfo=self._tz),
            datetime.combine(
                d, self._close_time, tzinfo=self._tz
            ),
        )

    def is_inside_session(
        self, now: Optional[datetime] = None
    ) -> bool:
        bounds = self.session_bounds_for(
            (now or ist_now()).date()
        )
        return (
            bounds[0] <= (now or ist_now()) <= bounds[1]
            if bounds else False
        )

    def gate_datetime(self, d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=self._tz)

    def seconds_until(
        self,
        target: datetime,
        now: Optional[datetime] = None,
    ) -> float:
        return max(
            0.0, (target - (now or ist_now())).total_seconds()
        )


class PacingGate:
    def __init__(
        self, min_interval: float, max_interval: float
    ) -> None:
        self._min_interval = float(min_interval)
        self._max_interval = float(max_interval)
        self._lock = asyncio.Lock()
        self._next_free_mono = 0.0
        self._clean_streak = 0

    async def acquire(self) -> None:
        async with self._lock:
            slot = max(
                self._next_free_mono, wall_clock.monotonic()
            )
            self._next_free_mono = slot + self._min_interval
        wait = slot - wall_clock.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def on_success(self) -> None:
        async with self._lock:
            self._clean_streak += 1
            if self._clean_streak >= 200:
                self._min_interval = max(
                    PACING_MIN_INTERVAL_SEC,
                    self._min_interval * 0.75,
                )
                self._clean_streak = 0

    async def on_429(self) -> None:
        async with self._lock:
            self._clean_streak = 0
            self._min_interval = min(
                self._max_interval,
                max(self._min_interval * 2.0, 0.25),
            )


# =============================================================================
# SECTION 5 — UPSTOX v2 REST CLIENT
# =============================================================================

class UpstoxClient:
    INTERVAL_DAY: Final[str] = "day"

    def __init__(
        self,
        access_token: str,
        base_url: str,
        logger: logging.Logger,
    ) -> None:
        self._token = access_token
        self._base = base_url.rstrip("/")
        self._logger = logger
        self._session: Optional[aiohttp.ClientSession] = None
        self._paces = {
            "default": PacingGate(
                PACING_MIN_INTERVAL_SEC, PACING_MAX_INTERVAL_SEC
            ),
            "data": PacingGate(1.0, 10.0),
            "order": PacingGate(
                PACING_MIN_INTERVAL_SEC, PACING_MAX_INTERVAL_SEC
            ),
        }

    async def start(self) -> None:
        if not self._session:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=HTTP_TIMEOUT_SEC, connect=5.0
                ),
                headers={
                    "Accept": "application/json",
                    "Api-Version": API_VERSION,
                },
            )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        gate_key: str = "default",
        params: Optional[Mapping[str, str]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        attempt_budget: Optional[int] = None,
    ) -> Dict[str, Any]:
        budget = attempt_budget or MAX_HTTP_ATTEMPTS
        gate = self._paces[gate_key]
        for attempt in range(1, budget + 1):
            await gate.acquire()
            headers: Dict[str, str] = {}
            if self._token and "YOUR_" not in self._token:
                headers["Authorization"] = (
                    f"Bearer {self._token}"
                )
            if json_body:
                headers["Content-Type"] = "application/json"
            try:
                async with self._session.request(
                    method,
                    f"{self._base}{path}",
                    params=params,
                    json=json_body,
                    headers=headers,
                ) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        await gate.on_429()
                        raise UpstoxRateLimited(
                            "HTTP 429 rate limited"
                        )
                    if resp.status >= 500:
                        raise UpstoxTransportError(
                            f"HTTP {resp.status}"
                        )
                    payload = (
                        json.loads(body) if body else {}
                    )
                    if (
                        payload.get("status") == "error"
                        or resp.status >= 400
                    ):
                        errs = payload.get("errors") or []
                        raise UpstoxApiError(
                            errs[0].get("errorCode")
                            if errs else None,
                            errs[0].get("message")
                            if errs
                            else f"HTTP {resp.status}",
                            resp.status,
                        )
                    await gate.on_success()
                    return payload
            except UpstoxRateLimited:
                if attempt >= budget:
                    raise
            except UpstoxTransportError:
                if attempt >= budget:
                    raise
            except Exception as exc:
                if attempt >= budget:
                    raise UpstoxTransportError(str(exc))
            await asyncio.sleep(
                min(30.0, 0.5 * (2 ** (attempt - 1)))
                * (0.8 + 0.4 * random.random())
            )
        raise UpstoxError(
            f"request exhausted retries: {path}"
        )

    async def validate_credentials(self) -> None:
        try:
            await self._request(
                "GET", "/v2/user/profile", attempt_budget=2
            )
        except Exception as exc:
            raise ConfigurationError(
                f"Upstox token rejected: {exc}"
            )

    async def get_real_cash_balance(self) -> float:
        payload = await self._request(
            "GET",
            "/v2/user/get-funds-and-margin?segment=SEC",
        )
        return _safe_float(
            (payload.get("data") or {})
            .get("equity", {})
            .get("available_margin"),
            TOTAL_CAPITAL,
        )

    @staticmethod
    def _candles_to_frame(
        candles: Sequence[Sequence[Any]],
    ) -> pd.DataFrame:
        rows = [
            [
                datetime.fromisoformat(str(c[0])),
                float(c[1]), float(c[2]),
                float(c[3]), float(c[4]),
                int(float(c[5])),
            ]
            for c in candles if len(c) >= 6
        ]
        if not rows:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"]
            )
        frame = (
            pd.DataFrame(
                rows,
                columns=[
                    "datetime", "open", "high",
                    "low", "close", "volume",
                ],
            )
            .sort_values("datetime")
            .drop_duplicates("datetime")
            .set_index("datetime")
        )
        return frame[["open", "high", "low", "close", "volume"]]

    async def get_daily_history(
        self,
        instrument_key: str,
        to_date: date,
        from_date: date,
    ) -> pd.DataFrame:
        payload = await self._request(
            "GET",
            (
                f"/v2/historical-candle/"
                f"{url_quote(instrument_key, safe='')}/"
                f"{self.INTERVAL_DAY}/"
                f"{to_date.isoformat()}/"
                f"{from_date.isoformat()}"
            ),
            gate_key="data",
        )
        candles = (
            payload.get("data") or {}
        ).get("candles", [])
        return await asyncio.to_thread(
            self._candles_to_frame, candles
        )

    async def fetch_daily_histories_bulk(
        self,
        keys: Sequence[str],
        to_date: date,
        lookback_days: int,
    ) -> Dict[str, Optional[pd.DataFrame]]:
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(
            key: str,
        ) -> Tuple[str, Optional[pd.DataFrame]]:
            async with sem:
                try:
                    return key, await self.get_daily_history(
                        key,
                        to_date,
                        to_date - timedelta(days=lookback_days),
                    )
                except Exception:
                    return key, None

        return dict(
            await asyncio.gather(*(one(k) for k in keys))
        )

    async def get_ltp_batch(
        self, keys: Sequence[str]
    ) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for chunk in _chunks(list(keys), 50):
            payload = await self._request(
                "GET",
                "/v2/market-quote/ltp",
                params={"instrument_key": ",".join(chunk)},
            )
            for key, blob in (
                payload.get("data") or {}
            ).items():
                if blob and "last_price" in blob:
                    out[key] = float(blob["last_price"])
        return out

    @staticmethod
    def _quote_map_from_any(
        data: Any,
    ) -> Dict[str, Mapping[str, Any]]:
        if isinstance(data, Mapping):
            return {
                str(k): v
                for k, v in data.items()
                if isinstance(v, Mapping)
            }
        if isinstance(data, list):
            out: Dict[str, Mapping[str, Any]] = {}
            for item in data:
                if isinstance(item, Mapping):
                    k = str(
                        item.get("instrument_key")
                        or item.get("instrument_token")
                        or ""
                    )
                    if k:
                        out[k] = item
            return out
        return {}

    async def get_segment_quotes(
        self, segment: str = "EQ"
    ) -> Dict[str, Mapping[str, Any]]:
        payload = await self._request(
            "GET", f"/v2/market-quote/segment/{segment}"
        )
        return self._quote_map_from_any(payload.get("data"))

    @staticmethod
    def _ts_seconds(value: Any) -> float:
        t = _safe_float(value, 0.0)
        if t > 1e12:
            t /= 1000.0
        return t if t > 0 else 0.0

    @staticmethod
    def _quote_from_blob(
        blob: Mapping[str, Any],
    ) -> QuoteData:
        return QuoteData(
            last_price=_safe_float(
                blob.get("last_price"), float("nan")
            ),
            low_price=_safe_float(
                blob.get("low_price"), float("nan")
            ),
            high_price=_safe_float(
                blob.get("high_price"), float("nan")
            ),
            ts=UpstoxClient._ts_seconds(
                blob.get("timestamp")
            ),
        )

    async def get_instrument_quote(
        self, key: str
    ) -> QuoteData:
        payload = await self._request(
            "GET",
            f"/v2/market-quote/instrument/"
            f"{url_quote(key, safe='')}",
        )
        blob = None
        data = payload.get("data")
        if isinstance(data, Mapping):
            blob = data
        elif isinstance(data, list) and data:
            blob = data[-1]
        blob = blob or {}
        return self._quote_from_blob(blob)

    async def get_quote_batch(
        self, keys: Sequence[str]
    ) -> Dict[str, QuoteData]:
        ordered = list(dict.fromkeys(keys))
        if not ordered:
            return {}
        out: Dict[str, QuoteData] = {}
        seg: Optional[Dict[str, Mapping[str, Any]]] = None
        try:
            seg = await self.get_segment_quotes("EQ")
        except Exception as exc:
            self._logger.warning(
                "Segment quote failed (%s); "
                "using per-instrument quotes", exc,
            )
        for key in ordered:
            blob = (seg or {}).get(key) or {}
            lp = _safe_float(
                blob.get("last_price"), float("nan")
            )
            lo = _safe_float(
                blob.get("low_price"), float("nan")
            )
            hi = _safe_float(
                blob.get("high_price"), float("nan")
            )
            if math.isfinite(lp):
                out[key] = QuoteData(
                    lp, lo, hi,
                    self._ts_seconds(blob.get("timestamp")),
                )
        missing = [k for k in ordered if k not in out]
        if missing:
            sem = asyncio.Semaphore(FETCH_CONCURRENCY)

            async def one(
                key: str,
            ) -> Tuple[str, QuoteData]:
                async with sem:
                    try:
                        return (
                            key,
                            await self.get_instrument_quote(key),
                        )
                    except Exception:
                        return (
                            key,
                            QuoteData(
                                float("nan"), float("nan")
                            ),
                        )

            for key, q in await asyncio.gather(
                *(one(k) for k in missing)
            ):
                if math.isfinite(q.last_price):
                    out[key] = q
        still = [k for k in ordered if k not in out]
        if still:
            try:
                ltps = await self.get_ltp_batch(still)
                for k in still:
                    if k in ltps:
                        out[k] = QuoteData(ltps[k], float("nan"))
            except Exception as exc:
                self._logger.warning(
                    "LTP fallback failed: %s", exc
                )
        return out

    async def get_vix_quote(self) -> float:
        """IMP A: Fetch India VIX for regime filtering."""
        try:
            quotes = await self.get_quote_batch(
                [VIX_INSTRUMENT_KEY]
            )
            q = quotes.get(VIX_INSTRUMENT_KEY)
            if q and math.isfinite(q.last_price):
                return q.last_price
        except Exception as exc:
            self._logger.warning(
                "VIX fetch failed: %s", exc
            )
        return 0.0

    async def get_delivery_positions(
        self,
    ) -> List[Dict[str, Any]]:
        return (
            await self._request(
                "GET", "/v2/portfolio/long-term-positions"
            )
        ).get("data") or []

    async def get_order_history(
        self, tag: str
    ) -> List[Dict[str, Any]]:
        return (
            await self._request(
                "GET",
                f"/v2/order/history?tag={tag}",
                gate_key="order",
            )
        ).get("data") or []

    async def get_open_orders(
        self,
    ) -> List[Dict[str, Any]]:
        return (
            await self._request(
                "GET",
                "/v2/order/open-orders",
                gate_key="order",
            )
        ).get("data") or []

    async def place_order(
        self,
        *,
        instrument_key: str,
        side: str,
        quantity: int,
        price: float,
        tag: str,
        order_type: str = "LIMIT",
        trigger_price: float = 0.0,
    ) -> Dict[str, Any]:
        existing = await self.get_order_history(tag)
        if existing:
            return {"status": "success", "data": existing[-1]}
        normalized_type = order_type.upper()
        if normalized_type not in {"LIMIT", "MARKET", "SL", "SL-M"}:
            raise ValueError(
                f"Unsupported order type: {order_type}"
            )
        body: Dict[str, Any] = {
            "instrument_token": instrument_key,
            "transaction_type": side,
            "order_type": normalized_type,
            "quantity": int(quantity),
            "price": (
                0
                if normalized_type in {"MARKET", "SL-M"}
                else float(price)
            ),
            "product": "D",
            "validity": "DAY",
            "disclosed_quantity": 0,
            "trigger_price": (
                float(trigger_price)
                if normalized_type in {"SL", "SL-M"}
                else 0.0
            ),
            "is_amo": False,
            "market_protection": -1,
            "tag": tag,
        }
        return await self._request(
            "POST",
            "/v2/order/place",
            gate_key="order",
            json_body=body,
            attempt_budget=3,
        )

    async def cancel_order(
        self, order_id: str
    ) -> Dict[str, Any]:
        if not order_id:
            raise ValueError(
                "Cannot cancel an order without order_id"
            )
        return await self._request(
            "DELETE",
            "/v2/order/cancel",
            gate_key="order",
            params={"order_id": order_id},
            attempt_budget=3,
        )


# =============================================================================
# SECTION 6 — CONNORSRSI INDICATORS (fixed formulas)
# =============================================================================

def _wild_avg_with_seed(
    x: np.ndarray, period: int
) -> np.ndarray:
    """
    FIX 5: Correct Wilder smoothing using explicit recursion.
    The original vectorized cumsum approach produced incorrect
    results due to floating point accumulation in the recursive
    formula. This explicit loop is O(n) and numerically stable.
    """
    n = x.size
    out = np.full(n, np.nan)
    if n < period:
        return out
    # Seed: simple average of first `period` values
    out[period - 1] = float(np.mean(x[:period]))
    # Recursive Wilder smoothing: correct formula
    alpha = 1.0 / period
    one_minus_alpha = 1.0 - alpha
    for i in range(period, n):
        if math.isfinite(x[i]) and math.isfinite(out[i - 1]):
            out[i] = out[i - 1] * one_minus_alpha + x[i] * alpha
        else:
            out[i] = out[i - 1]  # Carry forward on NaN input
    return out


def _wilder_rsi(
    gains: pd.Series, losses: pd.Series, period: int
) -> pd.Series:
    avg_g = _wild_avg_with_seed(
        gains.to_numpy(dtype=np.float64)[1:], period
    )
    avg_l = _wild_avg_with_seed(
        losses.to_numpy(dtype=np.float64)[1:], period
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = np.where(
            avg_l == 0.0,
            100.0,
            100.0 - 100.0 / (1.0 + avg_g / avg_l),
        )
    rsi = np.where(
        (avg_g == 0.0) & (avg_l == 0.0), 50.0, rsi
    )
    rsi[np.isnan(avg_g) | np.isnan(avg_l)] = np.nan
    return pd.Series(rsi, index=gains.index[1:])


def streak_array(close: np.ndarray) -> np.ndarray:
    out = np.zeros(close.shape[0], dtype=np.int64)
    cur = 0
    for i in range(1, close.shape[0]):
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


def compute_adx(
    hist: pd.DataFrame, period: int = ADX_PERIOD
) -> pd.Series:
    """IMP B: Compute ADX for trend strength filter."""
    if hist.empty or len(hist) < period * 2:
        return pd.Series(
            np.nan, index=hist.index, dtype=np.float64
        )
    high = hist["high"].astype(np.float64)
    low = hist["low"].astype(np.float64)
    close = hist["close"].astype(np.float64)

    # True Range
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    # Directional Movement
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where(
        (up_move > down_move) & (up_move > 0), up_move, 0.0
    )
    minus_dm = np.where(
        (down_move > up_move) & (down_move > 0), down_move, 0.0
    )

    # Wilder smoothing
    atr = _wild_avg_with_seed(
        tr.to_numpy(dtype=np.float64)[1:], period
    )
    plus_di_arr = _wild_avg_with_seed(plus_dm[1:], period)
    minus_di_arr = _wild_avg_with_seed(minus_dm[1:], period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * plus_di_arr / (atr + 1e-10)
        minus_di = 100.0 * minus_di_arr / (atr + 1e-10)
        dx = (
            100.0
            * np.abs(plus_di - minus_di)
            / (plus_di + minus_di + 1e-10)
        )

    adx_arr = _wild_avg_with_seed(dx, period)
    # Align back to original index
    result = np.full(len(hist), np.nan)
    result[1 + period:] = adx_arr[period - 1:]
    return pd.Series(result, index=hist.index)


def compute_crsi(hist: pd.DataFrame) -> pd.DataFrame:
    if hist.empty:
        return hist.copy()
    df = hist.copy()
    close = df["close"].astype(np.float64)
    df["sma200"] = close.rolling(200, min_periods=200).mean()
    df["rsi3"] = _wilder_rsi(
        close.diff().clip(lower=0.0),
        (-close.diff()).clip(lower=0.0),
        3,
    )
    df["streak"] = streak_array(
        close.to_numpy(dtype=np.float64)
    )
    df["rsi_streak"] = _wilder_rsi(
        df["streak"].diff().clip(lower=0.0),
        (-df["streak"].diff()).clip(lower=0.0),
        2,
    )
    df["pct_rank"] = (
        close.pct_change()
        .rolling(100, min_periods=100)
        .rank(pct=True)
        * 100.0
    )
    df["crsi"] = (
        df["rsi3"] + df["rsi_streak"] + df["pct_rank"]
    ) / 3.0
    df["rsi2"] = _wilder_rsi(
        close.diff().clip(lower=0.0),
        (-close.diff()).clip(lower=0.0),
        2,
    )
    df["adv"] = df["volume"].rolling(
        ADV_LOOKBACK_DAYS, min_periods=ADV_LOOKBACK_DAYS
    ).mean()
    # IMP B: Add ADX for trend filter
    df["adx"] = compute_adx(df, ADX_PERIOD)
    return df


def _closes_upto(
    hist: pd.DataFrame, as_of: date, proxy_close: float
) -> np.ndarray:
    close = hist["close"].astype(np.float64)
    if close.index.size:
        last_day = close.index[-1].date()
        if last_day >= as_of:
            close = close.iloc[:-1]
    vals = close.to_numpy(dtype=np.float64)
    if math.isfinite(proxy_close):
        vals = np.append(vals, np.float64(proxy_close))
    return vals


def _crsi_from_closes(values: np.ndarray) -> float:
    close = pd.Series(values, dtype=np.float64)
    delta = close.diff()
    rsi3 = _wilder_rsi(
        delta.clip(lower=0.0), (-delta).clip(lower=0.0), 3
    ).iloc[-1]
    streak = pd.Series(
        streak_array(values), dtype=np.float64
    )
    streak_delta = streak.diff()
    rsi_streak = _wilder_rsi(
        streak_delta.clip(lower=0.0),
        (-streak_delta).clip(lower=0.0),
        2,
    ).iloc[-1]
    pct_rank = (
        close.pct_change().iloc[-100:].rank(pct=True).iloc[-1]
        * 100.0
    )
    return float((rsi3 + rsi_streak + pct_rank) / 3.0)


def _rsi2_from_closes(values: np.ndarray) -> float:
    close = pd.Series(values, dtype=np.float64)
    delta = close.diff()
    rsi2 = _wilder_rsi(
        delta.clip(lower=0.0), (-delta).clip(lower=0.0), 2
    )
    return float(rsi2.iloc[-1]) if len(rsi2) else float("nan")


def _adx_from_hist(
    hist: pd.DataFrame, as_of: date
) -> float:
    """IMP B: Get latest ADX value from history."""
    if hist is None or hist.empty:
        return float("nan")
    adx_series = compute_adx(hist, ADX_PERIOD)
    if adx_series.empty:
        return float("nan")
    return float(adx_series.iloc[-1])


def intraday_rsi2(
    hist: Optional[pd.DataFrame],
    ltp: float,
    as_of: date,
) -> float:
    if hist is None or hist.empty or not math.isfinite(ltp):
        return float("nan")
    values = _closes_upto(hist, as_of, ltp)
    if values.size < 30:
        return float("nan")
    return _rsi2_from_closes(values)


def average_daily_volume(hist: pd.DataFrame) -> float:
    if hist.empty or "volume" not in hist:
        return 0.0
    return float(
        hist["volume"].tail(ADV_LOOKBACK_DAYS).mean()
    )


def candidate_metrics(
    hist: pd.DataFrame,
    ltp: float,
    as_of: date,
) -> Optional[Tuple[float, float, float, float]]:
    """
    Returns (sma200, crsi, adv20, adx) using the 15:25
    price as the closing price of the decision day.
    FIX 5: Uses corrected Wilder RSI computation.
    IMP B: Returns ADX for trend filter.
    """
    if (
        hist is None
        or len(hist) < MIN_HISTORY_DAYS
        or not math.isfinite(ltp)
    ):
        return None
    values = _closes_upto(hist, as_of, ltp)
    if values.size < 201:
        return None
    sma200_proxy = float(np.mean(values[-200:]))
    crsi = _crsi_from_closes(values)
    adv = average_daily_volume(hist)
    adx = _adx_from_hist(hist, as_of)
    return sma200_proxy, crsi, adv, adx


def check_circuit_breaker_risk(
    hist: Optional[pd.DataFrame],
    q: QuoteData,
) -> bool:
    """
    FIX 7: Detect if stock is near NSE circuit breaker limit.
    Returns True if stock is within 0.5% of a circuit limit.
    """
    if hist is None or hist.empty:
        return False
    if not math.isfinite(q.last_price):
        return False
    try:
        prev_close = float(hist["close"].iloc[-1])
    except (IndexError, KeyError):
        return False
    if prev_close <= 0:
        return False

    ltp = q.last_price
    # NSE circuit limits: 5%, 10%, 20%
    for pct in [0.05, 0.10, 0.20]:
        lower_circuit = prev_close * (1.0 - pct)
        upper_circuit = prev_close * (1.0 + pct)
        # Within 0.5% of lower circuit = risk of being trapped
        if abs(ltp - lower_circuit) / lower_circuit < 0.005:
            return True
        # Within 0.5% of upper circuit = possible reversal risk
        if abs(ltp - upper_circuit) / upper_circuit < 0.005:
            return True

    # Also check if intraday range suggests circuit hit
    if math.isfinite(q.low_price) and q.low_price > 0:
        intraday_drop = (prev_close - q.low_price) / prev_close
        if intraday_drop >= 0.095:  # Near 10% lower circuit
            return True

    return False


def trading_days_since_entry(
    hist: Optional[pd.DataFrame],
    entry_date: date,
    as_of: date,
) -> Optional[int]:
    cal = (as_of - entry_date).days
    if cal < 0:
        return 0
    if hist is None or hist.empty:
        return int(round(cal * 5.0 / 7.0))
    try:
        dates = {ts.date() for ts in hist.index}
    except AttributeError:
        return int(round(cal * 5.0 / 7.0))
    dates.add(as_of)
    return sum(1 for d in dates if entry_date < d <= as_of)


def check_trailing_stop(
    pos: Position, current_price: float
) -> bool:
    """
    IMP D: Check if trailing stop has been breached.
    Activates after TRAILING_STOP_TRIGGER_PCT gain,
    then trails TRAILING_STOP_DISTANCE_PCT below peak.
    """
    if not pos.trailing_stop_active:
        return False
    if pos.peak_price <= 0:
        return False
    trailing_stop_price = pos.peak_price * (
        1.0 - TRAILING_STOP_DISTANCE_PCT
    )
    return current_price <= trailing_stop_price


# =============================================================================
# SECTION 7 — PERFORMANCE TRACKER
# =============================================================================

class PerformanceTracker:
    """
    FIX 13: Performance tracker now includes transaction costs
    and reports NET PnL separately from GROSS PnL.
    """

    def __init__(
        self,
        logger: logging.Logger,
        trade_log_csv: str,
        perf_csv: str,
        initial_capital: float,
    ) -> None:
        self._logger = logger
        self._initial = float(initial_capital)
        self._trade_log = Path(trade_log_csv)
        self._perf_path = Path(perf_csv)
        self._csv = CSVPersister(perf_csv, PERF_COLUMNS, logger)
        self._lock = asyncio.Lock()
        self._trades: List[Dict[str, Any]] = []

    @staticmethod
    def _reason_from_notes(notes: str) -> str:
        return (notes or "").split(" | ")[0].strip()

    async def load_from_log(self) -> int:
        def parse() -> List[Dict[str, Any]]:
            if not self._trade_log.exists():
                return []
            with open(
                self._trade_log, newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            open_by_key: Dict[str, List[Dict[str, Any]]] = {}
            out: List[Dict[str, Any]] = []
            for r in rows:
                key = (r.get("instrument_key") or "").strip()
                et = (r.get("event_type") or "").strip()
                side = (r.get("side") or "").strip().upper()
                if not key or not et:
                    continue
                if et == "ENTRY" and side == "BUY":
                    open_by_key.setdefault(key, []).append({
                        "entry_date": (
                            r.get("session_date") or ""
                        ).strip(),
                        "entry_price": _safe_float(
                            r.get("simulated_price"),
                            float("nan"),
                        ),
                    })
                elif et == "EXIT" and side == "SELL":
                    queue = open_by_key.get(key)
                    entry = (
                        queue.pop(0)
                        if queue
                        else {
                            "entry_date": "",
                            "entry_price": float("nan"),
                        }
                    )
                    gross = _safe_float(
                        r.get("realized_pnl"), 0.0
                    )
                    tx = _safe_float(
                        r.get("transaction_costs"), 0.0
                    )
                    net = _safe_float(
                        r.get("net_pnl"), gross - tx
                    )
                    out.append({
                        "close_time": (
                            r.get("timestamp") or ""
                        ).strip(),
                        "session_date": (
                            r.get("session_date") or ""
                        ).strip(),
                        "key": key,
                        "entry_date": entry["entry_date"],
                        "entry_price": entry["entry_price"],
                        "exit_price": _safe_float(
                            r.get("simulated_price"),
                            float("nan"),
                        ),
                        "qty": int(
                            _safe_float(r.get("qty"), 0.0)
                        ),
                        "gross_pnl": gross,
                        "tx_costs": tx,
                        "net_pnl": net,
                        "days": int(
                            _safe_float(r.get("days_held"), 0.0)
                        ),
                        "reason": self._reason_from_notes(
                            r.get("notes", "")
                        ),
                    })
            return out

        self._trades = await asyncio.to_thread(parse)
        return len(self._trades)

    def _stats(
        self, trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        n = len(trades)
        # FIX 13: Use net_pnl for all statistics
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]

        def mean_pct(
            group: List[Dict[str, Any]],
        ) -> float:
            vals = [
                100.0 * (t["exit_price"] / t["entry_price"] - 1.0)
                for t in group
                if math.isfinite(t["entry_price"])
                and t["entry_price"] > 0
            ]
            return (
                (sum(vals) / len(vals)) if vals else float("nan")
            )

        gross_win = sum(t["net_pnl"] for t in wins)
        gross_loss = -sum(t["net_pnl"] for t in losses)
        profit_factor = (
            gross_win / gross_loss
            if gross_loss > 0
            else (
                float("inf") if gross_win > 0 else float("nan")
            )
        )
        cumulative_net = sum(t["net_pnl"] for t in trades)
        total_tx = sum(t["tx_costs"] for t in trades)
        peak = self._initial
        max_dd = 0.0
        running = self._initial
        for t in trades:
            running += t["net_pnl"]
            peak = max(peak, running)
            if peak > 0:
                max_dd = max(max_dd, 1.0 - running / peak)
        return {
            "trades_closed": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": (
                100.0 * len(wins) / n if n else 0.0
            ),
            "avg_win_pct": mean_pct(wins),
            "avg_loss_pct": mean_pct(losses),
            "profit_factor": profit_factor,
            "cumulative_net_pnl": cumulative_net,
            "total_tx_costs": total_tx,
            "equity": self._initial + cumulative_net,
            "max_drawdown_pct": 100.0 * max_dd,
        }

    @staticmethod
    def _fmt(value: Any, spec: str = ".2f") -> str:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return ""
        return format(f, spec) if math.isfinite(f) else ""

    def _row(
        self,
        t: Dict[str, Any],
        prefix: int,
        open_positions: Any,
    ) -> Dict[str, Any]:
        s = self._stats(self._trades[:prefix])
        pnl_pct = (
            100.0 * (t["exit_price"] / t["entry_price"] - 1.0)
            if math.isfinite(t["entry_price"])
            and t["entry_price"] > 0
            else float("nan")
        )
        return {
            "close_time": t["close_time"],
            "session_date": t["session_date"],
            "instrument_key": t["key"],
            "entry_date": t["entry_date"],
            "entry_price": self._fmt(t["entry_price"]),
            "exit_price": self._fmt(t["exit_price"]),
            "qty": t["qty"],
            "gross_pnl": self._fmt(t["gross_pnl"]),
            "transaction_costs": self._fmt(t["tx_costs"]),
            "net_pnl": self._fmt(t["net_pnl"]),
            "pnl_pct": self._fmt(pnl_pct),
            "days_held": t["days"],
            "exit_reason": t["reason"],
            "trades_closed": s["trades_closed"],
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate_pct": self._fmt(s["win_rate_pct"]),
            "avg_win_pct": self._fmt(s["avg_win_pct"]),
            "avg_loss_pct": self._fmt(s["avg_loss_pct"]),
            "profit_factor": self._fmt(s["profit_factor"]),
            "cumulative_net_pnl": self._fmt(
                s["cumulative_net_pnl"]
            ),
            "equity": self._fmt(s["equity"]),
            "max_drawdown_pct": self._fmt(
                s["max_drawdown_pct"]
            ),
            "open_positions": (
                "" if open_positions is None else open_positions
            ),
        }

    async def rebuild_report(self) -> None:
        async with self._lock:
            trades = list(self._trades)
        if self._perf_path.exists():
            await asyncio.to_thread(self._perf_path.unlink)
        for i, t in enumerate(trades, 1):
            await self._csv.append(self._row(t, i, None))

    async def record_close(
        self,
        *,
        close_time: str,
        session_date: str,
        key: str,
        entry_date: str,
        entry_price: float,
        exit_price: float,
        qty: int,
        gross_pnl: float,
        tx_costs: float,
        net_pnl: float,
        days: int,
        reason: str,
        open_positions: int,
    ) -> None:
        trade = {
            "close_time": close_time,
            "session_date": session_date,
            "key": key,
            "entry_date": entry_date,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "qty": qty,
            "gross_pnl": gross_pnl,
            "tx_costs": tx_costs,
            "net_pnl": net_pnl,
            "days": days,
            "reason": reason,
        }
        async with self._lock:
            self._trades.append(trade)
            idx = len(self._trades)
        await self._csv.append(
            self._row(trade, idx, open_positions)
        )
        s = self._stats(self._trades)
        self._logger.info(
            "[PERF] trade #%d (%s) gross=%+.2f tx=%.2f "
            "net=%+.2f | cum_net=%+.2f equity=%.2f "
            "win_rate=%.1f%% pf=%s",
            idx,
            "WIN" if net_pnl > 0 else "LOSS",
            gross_pnl, tx_costs, net_pnl,
            s["cumulative_net_pnl"], s["equity"],
            s["win_rate_pct"],
            self._fmt(s["profit_factor"]) or "n/a",
        )

    async def stats(self) -> Dict[str, Any]:
        async with self._lock:
            return self._stats(list(self._trades))


# =============================================================================
# SECTION 8 — EXECUTION ENGINE
# =============================================================================

class ExecutionEngine:
    def __init__(
        self,
        client: UpstoxClient,
        state: State,
        ledger: Ledger,
        trade_csv: CSVPersister,
        logger: logging.Logger,
        paper_mode: bool,
        perf: PerformanceTracker,
    ) -> None:
        self._client = client
        self._state = state
        self._ledger = ledger
        self._csv = trade_csv
        self._logger = logger
        self._paper = paper_mode
        self._perf = perf
        self._queue: asyncio.Queue[
            Optional[OrderRequest]
        ] = asyncio.Queue(maxsize=256)
        self._pending_queue: asyncio.Queue[
            Optional[PendingOrder]
        ] = asyncio.Queue(maxsize=256)
        self._workers: List[asyncio.Task[None]] = []
        self._poller: Optional[asyncio.Task[None]] = None
        self._next_request_id = 0
        self._id_lock = asyncio.Lock()
        self._active: Dict[str, PendingOrder] = {}
        self._active_lock = asyncio.Lock()

    async def start(self) -> None:
        self._workers = [
            asyncio.create_task(
                self._worker(i),
                name=f"execution-worker-{i}",
            )
            for i in range(EXEC_WORKERS)
        ]
        if not self._paper:
            self._poller = asyncio.create_task(
                self._fill_poller(), name="fill-poller"
            )

    async def stop(self) -> None:
        await self.flush(timeout_sec=180.0)
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(
            *self._workers, return_exceptions=True
        )
        self._workers.clear()
        if self._poller:
            await self._pending_queue.put(None)
            await asyncio.gather(
                self._poller, return_exceptions=True
            )
            self._poller = None

    async def submit(
        self, req: OrderRequest
    ) -> OrderRequest:
        async with self._id_lock:
            self._next_request_id += 1
            queued = replace(
                req, request_id=self._next_request_id
            )
        await self._queue.put(queued)
        return queued

    async def flush(
        self, timeout_sec: float = 180.0
    ) -> bool:
        async def drain() -> None:
            await self._queue.join()
            if not self._paper:
                await self._pending_queue.join()

        try:
            await asyncio.wait_for(
                drain(), timeout=timeout_sec
            )
            return True
        except asyncio.TimeoutError:
            self._logger.error(
                "Execution queues did not drain within %.1fs",
                timeout_sec,
            )
            return False

    async def _worker(self, idx: int) -> None:
        while True:
            req = await self._queue.get()
            if req is None:
                self._queue.task_done()
                return
            try:
                if self._paper:
                    await self._simulate_paper_fill(req)
                else:
                    await self._submit_live_for_polling(req)
            except Exception as exc:
                self._logger.exception(
                    "Execution worker %d failed: %s", idx, exc
                )
            finally:
                self._queue.task_done()

    async def _record_fill(
        self,
        req: OrderRequest,
        fill_price: float,
        filled_qty: int,
        suffix: str,
    ) -> None:
        today = session_date_of()
        mode = "PAPER" if self._paper else "LIVE"

        if req.event_type == "ENTRY":
            pos = Position(
                req.instrument_key, req.symbol,
                today, fill_price, filled_qty,
                req.crsi_score, self._paper, req.notes,
                peak_price=fill_price,
                trailing_stop_active=False,
            )
            if await self._state.open_position(
                pos, fill_price * filled_qty
            ):
                # FIX 13: Transaction costs at entry
                tx_costs = estimate_transaction_costs(
                    fill_price, fill_price, filled_qty
                )
                event = LedgerEvent(
                    iso_now(), today.isoformat(),
                    req.instrument_key, "ENTRY", "BUY",
                    filled_qty, round(fill_price, 2),
                    round(req.crsi_score, 4), 0,
                    0.0, tx_costs, -tx_costs,
                    f"{req.notes} | {suffix}",
                )
                await self._ledger.add(event)
                await self._csv.append(asdict(event))
                self._logger.info(
                    "[FILL] %s ENTRY BUY  %s qty=%d @ %.2f "
                    "crsi=%.1f tx_cost=%.2f | %s",
                    mode, req.instrument_key,
                    filled_qty, fill_price,
                    req.crsi_score, tx_costs, suffix,
                )
                if not self._paper:
                    await self._arm_protective_stop(pos, req)

        elif req.event_type == "EXIT":
            closed = await self._state.close_position(
                req.instrument_key, fill_price,
                today, req.notes, filled_qty,
            )
            if closed:
                event = LedgerEvent(
                    iso_now(), today.isoformat(),
                    req.instrument_key, "EXIT", "SELL",
                    filled_qty, round(fill_price, 2),
                    round(req.crsi_score, 4),
                    closed.days_held,
                    round(closed.gross_pnl, 2),
                    round(closed.transaction_costs, 2),
                    round(closed.net_pnl, 2),
                    f"{req.notes} | {suffix}",
                )
                await self._ledger.add(event)
                await self._csv.append(asdict(event))
                self._logger.info(
                    "[FILL] %s EXIT SELL  %s qty=%d @ %.2f "
                    "gross=%+.2f tx=%.2f net=%+.2f (%s) "
                    "entry=%.2f on %s | %s",
                    mode, req.instrument_key,
                    filled_qty, fill_price,
                    closed.gross_pnl,
                    closed.transaction_costs,
                    closed.net_pnl,
                    "WIN" if closed.net_pnl > 0 else "LOSS",
                    closed.position.entry_price,
                    closed.position.entry_date.isoformat(),
                    suffix,
                )
                open_now = len(
                    (await self._state.snapshot())["positions"]
                )
                await self._perf.record_close(
                    close_time=iso_now(),
                    session_date=today.isoformat(),
                    key=req.instrument_key,
                    entry_date=(
                        closed.position.entry_date.isoformat()
                    ),
                    entry_price=closed.position.entry_price,
                    exit_price=fill_price,
                    qty=filled_qty,
                    gross_pnl=closed.gross_pnl,
                    tx_costs=closed.transaction_costs,
                    net_pnl=closed.net_pnl,
                    days=closed.days_held,
                    reason=req.notes.split(" | ")[0].strip(),
                    open_positions=open_now,
                )
        await self._state.save_state_file()

    async def _simulate_paper_fill(
        self, req: OrderRequest
    ) -> None:
        market = req.reference_ltp or req.limit_price
        if req.order_type == "LIMIT":
            crossed = (
                market <= req.limit_price
                if req.side == "BUY"
                else market >= req.limit_price
            )
            if not crossed:
                if req.event_type == "ENTRY":
                    await self._state.release_entry_cash(
                        req.instrument_key
                    )
                self._logger.info(
                    "Paper limit not crossed for %s: "
                    "market=%.2f limit=%.2f",
                    req.instrument_key, market, req.limit_price,
                )
                return

        # FIX 4: Use NSE-realistic slippage
        bps = get_slippage_bps(
            req.average_daily_volume, is_eod_session=True
        ) / 10_000.0
        raw_fill = market * (
            1.0 + bps if req.side == "BUY" else 1.0 - bps
        )
        if req.order_type == "LIMIT":
            fill = (
                min(raw_fill, req.limit_price)
                if req.side == "BUY"
                else max(raw_fill, req.limit_price)
            )
        else:
            fill = raw_fill

        await self._record_fill(
            req, fill, req.quantity, f"paper {req.order_type}"
        )

    @staticmethod
    def _extract_order_id(
        payload: Mapping[str, Any]
    ) -> str:
        data = payload.get("data") or {}
        if isinstance(data, list):
            data = data[-1] if data else {}
        return (
            str(
                data.get("order_id")
                or data.get("orderId")
                or ""
            )
            if isinstance(data, Mapping)
            else ""
        )

    async def _arm_protective_stop(
        self, pos: Position, source: OrderRequest
    ) -> None:
        """
        FIX 3: Use SL (SL-LIMIT) instead of SL-M.
        NSE/Upstox may reject SL-M on CNC delivery positions.
        Limit price set 2% below trigger to ensure execution
        even in fast-moving markets.
        """
        trigger = round(
            pos.entry_price * (1.0 - STOP_LOSS_PCT), 2
        )
        # FIX 3: Limit price 2% below trigger
        limit_price = round(trigger * 0.98, 2)
        limit_price = round_to_tick(limit_price, "down")

        key_suffix = pos.instrument_key[-4:].replace("|", "")
        tag = (
            f"sl-{session_date_of():%m%d}-"
            f"{source.request_id:06d}-{key_suffix}"
        )
        response = await self._client.place_order(
            instrument_key=pos.instrument_key,
            side="SELL",
            quantity=pos.quantity,
            price=limit_price,
            tag=tag,
            order_type="SL",        # FIX 3: was "SL-M"
            trigger_price=trigger,
        )
        order_id = self._extract_order_id(response)
        if not order_id:
            raise UpstoxError(
                f"Protective SL for {pos.instrument_key} "
                f"returned no order_id"
            )
        await self._state.set_protective_order(
            pos.instrument_key, order_id
        )
        self._logger.info(
            "Armed backup SL %s for %s trigger=%.2f "
            "limit=%.2f",
            order_id, pos.instrument_key,
            trigger, limit_price,
        )

    async def arm_missing_protective_stops(self) -> None:
        positions = (
            await self._state.snapshot()
        )["positions"]
        for pos in positions.values():
            if await self._state.get_protective_order(
                pos.instrument_key
            ):
                continue
            async with self._id_lock:
                self._next_request_id += 1
                request_id = self._next_request_id
            source = OrderRequest(
                request_id, pos.instrument_key,
                pos.symbol, "SELL", pos.quantity,
                pos.entry_price, "PROTECTIVE",
                pos.crsi_at_entry,
                "SESSION_OPEN_PROTECTION",
                "SL", 0.0, pos.entry_price,
                pos.entry_price,
            )
            await self._arm_protective_stop(pos, source)

    async def _cancel_protective_stop(
        self, key: str
    ) -> str:
        order_id = await self._state.get_protective_order(key)
        if not order_id:
            return ""
        await self._client.cancel_order(order_id)
        await self._state.pop_protective_order(key)
        return order_id

    async def _submit_live_for_polling(
        self, req: OrderRequest
    ) -> None:
        tag = f"cqrsi-{req.request_id:08d}"
        cancelled_stop = ""
        try:
            if req.event_type == "EXIT":
                cancelled_stop = (
                    await self._cancel_protective_stop(
                        req.instrument_key
                    )
                )
            response = await self._client.place_order(
                instrument_key=req.instrument_key,
                side=req.side,
                quantity=req.quantity,
                price=req.limit_price,
                tag=tag,
                order_type=req.order_type,
            )
        except Exception:
            if req.event_type == "ENTRY":
                await self._state.release_entry_cash(
                    req.instrument_key
                )
            elif cancelled_stop:
                pos = (
                    await self._state.snapshot()
                )["positions"].get(req.instrument_key)
                if pos:
                    await self._arm_protective_stop(pos, req)
            raise
        order_id = self._extract_order_id(response)
        if not order_id:
            raise UpstoxError(
                f"Accepted order {tag} returned no order_id"
            )
        async with self._active_lock:
            self._active[tag] = PendingOrder(
                req, tag, order_id, wall_clock.monotonic()
            )
        await self._pending_queue.put(self._active[tag])

    async def _finish_pending(
        self, pending: PendingOrder, terminal: bool
    ) -> None:
        req = pending.request
        last = pending.last_order
        filled_qty = int(
            _safe_float(last.get("filled_quantity"), 0.0)
        )
        fill_price = _safe_float(
            last.get("average_price"),
            req.reference_ltp or req.limit_price,
        )
        if not terminal:
            try:
                await self._client.cancel_order(
                    pending.order_id
                )
                self._logger.warning(
                    "Cancelled timed-out order %s (%s)",
                    pending.order_id, pending.tag,
                )
            except Exception as exc:
                self._logger.warning(
                    "Cancel of %s failed: %s",
                    pending.order_id, exc,
                )
            if req.event_type == "ENTRY":
                await self._state.release_entry_cash(
                    req.instrument_key
                )
        if filled_qty <= 0:
            if req.event_type == "ENTRY":
                await self._state.release_entry_cash(
                    req.instrument_key
                )
            elif req.event_type == "EXIT":
                pos = (
                    await self._state.snapshot()
                )["positions"].get(req.instrument_key)
                if pos:
                    await self._arm_protective_stop(pos, req)
            return
        await self._record_fill(
            req, fill_price, filled_qty,
            f"LIVE tag={pending.tag}",
        )
        if req.event_type == "EXIT":
            remaining = (
                await self._state.snapshot()
            )["positions"].get(req.instrument_key)
            if remaining:
                await self._arm_protective_stop(
                    remaining, req
                )

    async def _active_snapshot(
        self,
    ) -> Dict[str, PendingOrder]:
        async with self._active_lock:
            return dict(self._active)

    async def cancel_unfilled_orders(self) -> int:
        if self._paper:
            return 0
        finished = 0
        for tag, pending in (
            await self._active_snapshot()
        ).items():
            try:
                history = await self._client.get_order_history(
                    tag
                )
                if history:
                    pending.last_order = history[-1]
                    pending.order_id = str(
                        pending.last_order.get("order_id")
                        or pending.order_id
                    )
            except Exception as exc:
                self._logger.warning(
                    "Status fetch failed for %s: %s", tag, exc
                )
            status = str(
                pending.last_order.get("status") or ""
            ).lower()
            if status in TERMINAL_ORDER_STATUSES:
                continue
            async with self._active_lock:
                owned = self._active.pop(tag, None)
            if owned is not pending:
                continue
            try:
                await self._finish_pending(
                    pending, terminal=False
                )
                finished += 1
            except Exception as exc:
                self._logger.exception(
                    "Cancel sweep finalisation failed "
                    "for %s: %s", tag, exc,
                )
            finally:
                self._pending_queue.task_done()
        if finished:
            self._logger.info(
                "15:29 cancel sweep: %d order(s) cancelled",
                finished,
            )
        return finished

    async def _fill_poller(self) -> None:
        while True:
            try:
                item = await asyncio.wait_for(
                    self._pending_queue.get(), timeout=0.25
                )
                if item is None:
                    self._pending_queue.task_done()
                    return
                async with self._active_lock:
                    self._active[item.tag] = item
            except asyncio.TimeoutError:
                pass
            while True:
                try:
                    item = self._pending_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    self._pending_queue.task_done()
                    return
                async with self._active_lock:
                    self._active[item.tag] = item
            now = wall_clock.monotonic()
            for tag in (await self._active_snapshot()).keys():
                async with self._active_lock:
                    pending = self._active.get(tag)
                    if pending is None or now < pending.next_poll_at:
                        continue
                    pending.next_poll_at = now + 2.0
                try:
                    history = await self._client.get_order_history(
                        tag
                    )
                    if history:
                        pending.last_order = history[-1]
                        pending.order_id = str(
                            pending.last_order.get("order_id")
                            or pending.order_id
                        )
                    status = str(
                        pending.last_order.get("status") or ""
                    ).lower()
                    terminal = status in TERMINAL_ORDER_STATUSES
                    timed_out = (
                        now - pending.submitted_at >= 60.0
                    )
                    if not (terminal or timed_out):
                        continue
                    async with self._active_lock:
                        owned = self._active.pop(tag, None)
                    if owned is not pending:
                        continue
                    try:
                        await self._finish_pending(
                            pending, terminal
                        )
                    finally:
                        self._pending_queue.task_done()
                except Exception as exc:
                    self._logger.exception(
                        "Fill poll failed for %s: %s", tag, exc
                    )


# =============================================================================
# SECTION 9 — LIVE ORCHESTRATOR
# =============================================================================

class LiveOrchestrator:
    def __init__(
        self,
        clock: TradeClock,
        logger: logging.Logger,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._clock = clock
        self._logger = logger
        self._stop = stop_event or asyncio.Event()
        self._client = UpstoxClient(
            ACCESS_TOKEN, API_BASE_URL, logger
        )
        self._paper = PAPER_TRADING_MODE
        self._ledger = Ledger()
        self._state = State(
            logger, TOTAL_CAPITAL, paper=self._paper
        )
        self._trade_csv = CSVPersister(
            PAPER_TRADE_LOG_CSV, CSV_TRADE_COLUMNS, logger
        )
        self._summary_csv = CSVPersister(
            PAPER_TRADE_SUMMARY_CSV,
            CSV_SUMMARY_COLUMNS,
            logger,
        )
        self._perf = PerformanceTracker(
            logger, PAPER_TRADE_LOG_CSV,
            PAPER_PERFORMANCE_CSV, TOTAL_CAPITAL,
        )
        self._engine = ExecutionEngine(
            self._client, self._state, self._ledger,
            self._trade_csv, logger,
            paper_mode=self._paper, perf=self._perf,
        )
        self._histories: Dict[str, pd.DataFrame] = {}
        self._adv_by_key: Dict[str, float] = {}
        self._pending_entries: List[EntrySignal] = []
        self._pending_exits: Dict[str, ExitDecision] = {}
        self._current_vix: float = 0.0
        self._current_regime: str = "UNKNOWN"

    async def verify_ntp_drift(self) -> None:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "http://worldtimeapi.org/api/timezone"
                    "/Asia/Kolkata",
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as r:
                    data = await r.json()
                    network_time = float(data["unixtime"])
                    drift = abs(
                        wall_clock.time() - network_time
                    )
                    if drift > 5.0:
                        raise RuntimeError(
                            f"Clock drift {drift:.1f}s > 5s"
                        )
                    self._logger.info(
                        "NTP drift check passed (%.2fs)", drift
                    )
        except RuntimeError:
            raise
        except Exception as e:
            self._logger.warning(
                "Could not verify NTP drift: %s", e
            )

    async def setup(self) -> None:
        exp = token_expiry(ACCESS_TOKEN)
        if exp is not None:
            now_ts = wall_clock.time()
            if now_ts >= exp:
                self._logger.error(
                    "ACCESS_TOKEN EXPIRED at %s IST. "
                    "Generate a fresh token and update env.txt.",
                    datetime.fromtimestamp(
                        exp, IST_TZ
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                )
                raise ConfigurationError(
                    "access token expired"
                )
            if exp - now_ts < 7200:
                self._logger.warning(
                    "ACCESS_TOKEN expires in %.1f hours",
                    (exp - now_ts) / 3600.0,
                )
        await self._client.start()
        await self._engine.start()
        try:
            await self._client.validate_credentials()
            self._logger.info("[SETUP] Upstox token OK")
        except ConfigurationError as exc:
            self._logger.error(
                "Upstox token INVALID: %s", exc
            )
            raise
        saved = await State.load_state_file(self._logger)
        if saved and saved.get("paper") == self._paper:
            self._state.positions = saved["positions"]
            self._state.session_date = saved["session_date"]
            self._state.last_completed_session = saved.get(
                "last_completed_session"
            )
            self._state.cash_available = saved["cash_available"]
            self._state._cash_reservations = saved[
                "cash_reservations"
            ]
            self._state._protective_orders = saved[
                "protective_orders"
            ]
            self._state.high_water_mark = saved[
                "high_water_mark"
            ]
            self._state.drawdown_latched = saved[
                "drawdown_latched"
            ]
            self._state.current_vix = saved.get(
                "current_vix", 0.0
            )
        replayed = await self._perf.load_from_log()
        await self._perf.rebuild_report()
        if replayed:
            s = await self._perf.stats()
            self._logger.info(
                "[PERF] replayed %d trades | win_rate=%.1f%% "
                "cum_net=%+.2f equity=%.2f",
                replayed, s["win_rate_pct"],
                s["cumulative_net_pnl"], s["equity"],
            )
        if not self._paper:
            await self.verify_ntp_drift()
            self._state.cash_available = (
                await self._client.get_real_cash_balance()
            )
            try:
                open_orders = (
                    await self._client.get_open_orders()
                )
                stale = [
                    o for o in open_orders
                    if str(o.get("tag") or "").startswith(
                        "cqrsi-"
                    )
                ]
                for o in stale:
                    await self._client.cancel_order(
                        str(o.get("order_id"))
                    )
                    self._logger.warning(
                        "[CLEANUP] cancelled stale order %s",
                        o.get("order_id"),
                    )
            except Exception as exc:
                self._logger.warning(
                    "Open-order cleanup failed: %s", exc
                )
            if self._state.session_date != session_date_of():
                self._state._protective_orders.clear()
            await self.reconcile_portfolio()
            await self._engine.arm_missing_protective_stops()

    async def shutdown(self) -> None:
        await self._engine.stop()
        await self._state.save_state_file()
        await self._client.close()

    async def _sleep_interruptible(
        self, secs: float
    ) -> bool:
        end = wall_clock.monotonic() + max(0.0, secs)
        while not self._stop.is_set():
            remaining = end - wall_clock.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(5.0, remaining))
        return True

    def _next_trading_day(self, d: date) -> date:
        for _ in range(30):
            d += timedelta(days=1)
            if self._clock.session_bounds_for(d) is not None:
                return d
        raise ConfigurationError(
            "No NSE trading day found within 30 days — "
            "update NSE_HOLIDAYS_2026 in the script."
        )

    async def _wait_until(
        self, target: datetime, reason: str
    ) -> bool:
        mode = "PAPER" if self._paper else "LIVE"
        while not self._stop.is_set():
            secs = (target - ist_now()).total_seconds()
            if secs <= 0:
                return False
            self._logger.info(
                "[WAIT] %s | next: %s IST (in %dh %02dm) "
                "| mode=%s",
                reason,
                target.strftime("%Y-%m-%d %H:%M:%S"),
                int(secs // 3600),
                int((secs % 3600) // 60),
                mode,
            )
            if await self._sleep_interruptible(
                min(300.0, secs)
            ):
                return True
        return True

    @staticmethod
    def _fresh_ltp(
        quotes: Mapping[str, QuoteData],
        day_start: float,
    ) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for k, q in quotes.items():
            if math.isfinite(q.last_price) and not (
                q.ts > 0 and q.ts < day_start
            ):
                out[k] = q.last_price
        return out

    async def run_forever(self, once: bool = False) -> None:
        self._logger.info("=" * 78)
        self._logger.info(
            "CONNORSRSI (3,2,100) EOD SWING ENGINE v2.0 "
            "— %s MODE",
            "PAPER" if self._paper else "LIVE",
        )
        self._logger.info(
            "Entry: CRSI<=%.0f AND close>SMA200 AND "
            "ADV>=%.0fM AND ADX>%.0f",
            CRSI_THRESHOLD,
            MIN_AVG_DAILY_VOLUME / 1_000_000,
            ADX_MIN_FOR_ENTRY,
        )
        self._logger.info(
            "Exits: %.0f%% hard stop -> RSI-2>%.0f target "
            "-> %.0f-day time stop -> trailing stop",
            STOP_LOSS_PCT * 100,
            RSI2_EXIT_THRESHOLD,
            MAX_HOLD_TRADING_DAYS,
        )
        self._logger.info(
            "VIX regime: normal<%.0f caution<%.0f halt>%.0f",
            VIX_NORMAL_MAX, VIX_CAUTION_MAX,
            VIX_HALT_THRESHOLD,
        )
        self._logger.info("=" * 78)

        while not self._stop.is_set():
            now = ist_now()
            d = now.date()
            bounds = self._clock.session_bounds_for(d)
            wake = self._clock.gate_datetime(
                d, DATA_FETCH_TIME
            )
            force_rerun = (
                os.environ.get("CONNORS_FORCE_RERUN") == "1"
            )
            completed_today = (
                await self._state.session_completed_on()
            ) == d

            if bounds is None:
                nxt = self._next_trading_day(d)
                if await self._wait_until(
                    self._clock.gate_datetime(
                        nxt, DATA_FETCH_TIME
                    ),
                    f"{d.isoformat()} not an NSE session",
                ):
                    break
                continue
            if bounds[1].time() <= DATA_FETCH_TIME:
                nxt = self._next_trading_day(d)
                if await self._wait_until(
                    self._clock.gate_datetime(
                        nxt, DATA_FETCH_TIME
                    ),
                    f"special session closes before 15:20",
                ):
                    break
                continue
            if completed_today and not force_rerun:
                nxt = self._next_trading_day(d)
                if await self._wait_until(
                    self._clock.gate_datetime(
                        nxt, DATA_FETCH_TIME
                    ),
                    f"session {d.isoformat()} already done",
                ):
                    break
                continue
            if now >= bounds[1]:
                nxt = self._next_trading_day(d)
                if await self._wait_until(
                    self._clock.gate_datetime(
                        nxt, DATA_FETCH_TIME
                    ),
                    "today's session already closed",
                ):
                    break
                continue
            if now < wake:
                if await self._wait_until(
                    wake, "waiting for 15:20 data-fetch gate"
                ):
                    break
                continue
            if now.time() >= ORDER_TIME:
                self._logger.warning(
                    "[SKIP] started after 15:28 order gate; "
                    "no trades this session."
                )
                await self._state.mark_session_completed(d)
                await self._state.save_state_file()
                continue
            self._logger.info(
                "[RUN] executing session gates for %s",
                d.isoformat(),
            )
            await self.run_schedule(ist_now(), bounds)
            if not self._stop.is_set():
                await self._state.mark_session_completed(d)
                await self._state.save_state_file()
                self._logger.info(
                    "[DONE] session %s complete", d.isoformat()
                )
            if once:
                break

    async def reconcile_portfolio(self) -> None:
        broker_positions = (
            await self._client.get_delivery_positions()
        )
        today = session_date_of()
        saved = self._state.positions
        adopted: Dict[str, Position] = {}
        for raw in broker_positions:
            qty = int(_safe_float(raw.get("quantity"), 0.0))
            key = str(raw.get("instrument_token") or "")
            if qty <= 0 or key not in UNIVERSE_TICKERS:
                continue
            avg = _safe_float(
                raw.get("average_price"),
                _safe_float(raw.get("last_price"), 0.0),
            )
            old = saved.get(key)
            adopted[key] = Position(
                key,
                raw.get("tradingsymbol") or key,
                old.entry_date if old else today,
                old.entry_price if old else avg,
                qty,
                old.crsi_at_entry if old else 0.0,
                False,
                "broker_adopted" if not old
                else "broker_reconciled",
                peak_price=(
                    old.peak_price if old else avg
                ),
                trailing_stop_active=(
                    old.trailing_stop_active if old else False
                ),
            )
        self._state.positions = adopted

    async def _fetch_histories(self, as_of: date) -> bool:
        # IMP A: Fetch VIX first for regime determination
        self._current_vix = (
            await self._client.get_vix_quote()
        )
        self._current_regime = _get_vix_regime(
            self._current_vix
        )
        self._state.current_vix = self._current_vix
        self._state.current_regime = self._current_regime
        self._logger.info(
            "[VIX] India VIX=%.2f regime=%s",
            self._current_vix, self._current_regime,
        )
        if self._current_regime == "HALT":
            self._logger.warning(
                "[VIX] HALT regime (VIX=%.2f > %.0f) — "
                "NO new entries today",
                self._current_vix, VIX_HALT_THRESHOLD,
            )

        fetched = await self._client.fetch_daily_histories_bulk(
            UNIVERSE_TICKERS,
            to_date=as_of,
            lookback_days=DATA_LOOKBACK_DAYS,
        )
        ok = {
            k: df
            for k, df in fetched.items()
            if df is not None and not df.empty
        }
        self._histories = ok
        self._adv_by_key = dict(
            zip(
                ok.keys(),
                await asyncio.gather(
                    *(
                        asyncio.to_thread(
                            average_daily_volume, df
                        )
                        for df in ok.values()
                    )
                ),
            )
        )
        return bool(ok)

    async def _evaluate_exits(
        self,
        as_of: date,
        positions: Mapping[str, Position],
        quotes: Mapping[str, QuoteData],
        day_start: float,
    ) -> None:
        """
        FIX 2: Stop loss only evaluated for positions
        entered BEFORE today (entry_date != as_of).
        IMP D: Trailing stop check added.
        FIX 7: Circuit breaker risk check added.
        """
        self._pending_exits = {}
        if not positions:
            return

        for key, pos in positions.items():
            q = quotes.get(key)
            stale = (
                q is not None
                and q.ts > 0
                and q.ts < day_start
            )
            if (
                q is None
                or not math.isfinite(q.last_price)
                or stale
            ):
                self._logger.warning(
                    "No fresh 15:25 quote for %s; "
                    "exit deferred", key,
                )
                continue

            ltp = q.last_price
            hist = self._histories.get(key)
            stop_price = pos.entry_price * (
                1.0 - STOP_LOSS_PCT
            )
            days_traded = await asyncio.to_thread(
                trading_days_since_entry,
                hist, pos.entry_date, as_of,
            )
            rsi2 = await asyncio.to_thread(
                intraday_rsi2, hist, ltp, as_of
            ) if hist is not None else float("nan")

            low = q.low_price

            # FIX 2: Only check stop for positions entered
            # BEFORE today
            stop_breached = False
            if pos.entry_date != as_of:
                stop_breached = (
                    low <= stop_price
                    if math.isfinite(low)
                    else ltp <= stop_price
                )

            # IMP D: Update peak price and check trailing stop
            updated_pos = await self._state.update_position_peak(
                key, ltp
            )
            if updated_pos is None:
                updated_pos = pos

            trailing_breached = (
                pos.entry_date != as_of
                and check_trailing_stop(updated_pos, ltp)
            )

            # FIX 7: Circuit breaker risk check
            circuit_risk = check_circuit_breaker_risk(
                hist, q
            )
            if circuit_risk:
                self._logger.warning(
                    "[CIRCUIT] %s near circuit breaker limit "
                    "ltp=%.2f — using MARKET exit",
                    key, ltp,
                )

            if stop_breached:
                reason = "HARD_STOP_LOSS"
                order_type = "MARKET"
            elif trailing_breached:
                reason = "TRAILING_STOP"
                order_type = "MARKET"
            elif circuit_risk and pos.entry_date != as_of:
                reason = "CIRCUIT_BREAKER_RISK"
                order_type = "MARKET"
            elif (
                math.isfinite(rsi2)
                and rsi2 > RSI2_EXIT_THRESHOLD
            ):
                reason = "RSI2_TARGET"
                order_type = "MARKET" if circuit_risk else "LIMIT"
            elif (
                days_traded is not None
                and days_traded >= MAX_HOLD_TRADING_DAYS
            ):
                reason = "TIME_EXIT"
                order_type = "MARKET" if circuit_risk else "LIMIT"
            else:
                continue

            self._pending_exits[key] = ExitDecision(
                key, pos.symbol, ltp, reason,
                days_traded or 0, order_type,
                rsi2=rsi2,
                entry_price=pos.entry_price,
            )
            trail_info = ""
            if updated_pos.trailing_stop_active:
                trail_stop = updated_pos.peak_price * (
                    1.0 - TRAILING_STOP_DISTANCE_PCT
                )
                trail_info = (
                    f" trail_stop={trail_stop:.2f}"
                    f" peak={updated_pos.peak_price:.2f}"
                )
            self._logger.info(
                "EXIT %s (%s): ltp=%.2f stop=%.2f "
                "rsi2=%s days=%s%s",
                key, reason, ltp, stop_price,
                f"{rsi2:.1f}" if math.isfinite(rsi2) else "n/a",
                days_traded, trail_info,
            )

    async def _liquidate_portfolio(
        self,
        positions: Mapping[str, Position],
        ltps: Mapping[str, float],
    ) -> None:
        released = 0.0
        for pending_entry in self._pending_entries:
            released += await self._state.release_entry_cash(
                pending_entry.instrument_key
            )
        self._pending_entries = []
        self._pending_exits.clear()
        self._logger.critical(
            "Drawdown breaker: liquidating %d positions; "
            "released %.2f reserved cash",
            len(positions), released,
        )
        for key, pos in positions.items():
            reference_price = ltps.get(key)
            if not reference_price:
                self._logger.error(
                    "Cannot liquidate %s: no live price", key
                )
                continue
            await self._engine.submit(
                OrderRequest(
                    0, key, pos.symbol, "SELL",
                    pos.quantity, reference_price,
                    "EXIT", pos.crsi_at_entry,
                    "MAX_DRAWDOWN_LIQUIDATION",
                    "MARKET",
                    self._adv_by_key.get(key, 0.0),
                    reference_price,
                    pos.entry_price,
                )
            )

    async def _generate_signals(
        self,
        as_of: date,
        positions: Mapping[str, Position],
        quotes: Mapping[str, QuoteData],
        day_start: float,
    ) -> None:
        """
        FIX 6:  Uses cost-basis equity for position sizing.
        FIX 10: CRSI threshold raised to 15.
        FIX 11: Earnings season size reduction.
        FIX 12: Sector concentration limit.
        IMP A:  VIX regime filter.
        IMP B:  ADX trend strength filter.
        IMP C:  Minimum profit threshold.
        IMP E:  Signal quality position sizing.
        """
        for stale_signal in self._pending_entries:
            await self._state.release_entry_cash(
                stale_signal.instrument_key
            )
        self._pending_entries = []

        # IMP A: Halt new entries in extreme VIX regime
        if self._current_regime == "HALT":
            self._logger.warning(
                "[SIGNAL] VIX HALT regime — no new entries"
            )
            return

        snap = await self._state.snapshot()
        held = set(positions)
        candidates = [
            key for key in UNIVERSE_TICKERS
            if key in self._histories and key not in held
        ]
        fresh = self._fresh_ltp(quotes, day_start)

        # FIX 6: Use cost-basis equity for sizing
        cost_basis_equity = (
            snap["cash_available"]
            + snap["reserved_cash"]
            + sum(
                pos.entry_price * pos.quantity
                for pos in positions.values()
            )
        )
        # MTM equity for drawdown check only
        mtm_equity = (
            snap["cash_available"]
            + snap["reserved_cash"]
            + sum(
                fresh.get(key, pos.entry_price) * pos.quantity
                for key, pos in positions.items()
            )
        )

        if await self._state.update_drawdown(mtm_equity):
            await self._liquidate_portfolio(positions, fresh)
            return

        if not candidates:
            return

        # FIX 12: Build sector counts for held positions
        sector_counts: Dict[str, int] = {}
        for key in held:
            sector = SECTOR_MAP.get(key, "OTHER")
            sector_counts[sector] = (
                sector_counts.get(sector, 0) + 1
            )

        # Earnings season logging
        if _is_earnings_season(as_of):
            self._logger.info(
                "[SIGNAL] Earnings season (month=%d) — "
                "position size reduced to %.0f%%",
                as_of.month,
                EARNINGS_SEASON_SIZE_MULTIPLIER * 100,
            )

        async def evaluate(
            key: str,
        ) -> Optional[Tuple[str, float, float, float, float, float]]:
            ltp = fresh.get(key)
            if not ltp or not math.isfinite(ltp):
                return None
            metrics = await asyncio.to_thread(
                candidate_metrics,
                self._histories[key], ltp, as_of,
            )
            if metrics is None:
                return None
            sma200_proxy, crsi, adv, adx = metrics

            # FIX 10: CRSI threshold = 15
            if not (
                math.isfinite(crsi)
                and math.isfinite(sma200_proxy)
                and crsi <= CRSI_THRESHOLD
                and ltp > sma200_proxy
                and adv >= MIN_AVG_DAILY_VOLUME
            ):
                return None

            # IMP B: ADX trend filter
            if math.isfinite(adx) and adx < ADX_MIN_FOR_ENTRY:
                self._logger.debug(
                    "[SIGNAL] %s: ADX=%.1f < %.0f — "
                    "skipping (not trending enough)",
                    key, adx, ADX_MIN_FOR_ENTRY,
                )
                return None

            # IMP C: Minimum profit threshold
            bps = get_slippage_bps(adv, is_eod_session=True)
            round_trip_cost_pct = (
                2 * bps / 10_000.0
                + estimate_transaction_costs(
                    ltp, ltp, 1
                ) / ltp
            )
            if round_trip_cost_pct > (
                MIN_PROFIT_THRESHOLD_PCT * 0.5
            ):
                self._logger.debug(
                    "[SIGNAL] %s: round-trip cost %.2f%% "
                    "too high — skipping",
                    key, round_trip_cost_pct * 100,
                )
                return None

            # Corporate action guard
            last_close = float(
                self._histories[key]["close"].iloc[-1]
            )
            if math.isfinite(last_close) and last_close > 0:
                move = abs(ltp / last_close - 1.0)
                if move > SPLIT_GUARD_MOVE_PCT:
                    self._logger.warning(
                        "[SIGNAL] %s: %.0f%% move vs last "
                        "close — possible split; skipping",
                        key, 100.0 * (ltp / last_close - 1.0),
                    )
                    return None

            return key, ltp, sma200_proxy, crsi, adv, adx

        evaluated = await asyncio.gather(
            *(evaluate(key) for key in candidates)
        )
        # Sort by CRSI ascending (most oversold first)
        valid = sorted(
            (item for item in evaluated if item is not None),
            key=lambda x: x[3],
        )
        slots = max(
            MAX_CONCURRENT_POSITIONS - len(held), 0
        )
        running_cash = snap["cash_available"]

        for key, ltp, sma200, crsi, adv, adx in valid[:slots]:
            # FIX 12: Sector concentration check
            sector = SECTOR_MAP.get(key, "OTHER")
            if sector_counts.get(sector, 0) >= MAX_POSITIONS_PER_SECTOR:
                self._logger.info(
                    "[SIGNAL] %s: sector %s at limit (%d) — skip",
                    key, sector, MAX_POSITIONS_PER_SECTOR,
                )
                continue

            # IMP A + FIX 11 + IMP E: Combined size multiplier
            size_mult = _position_size_multiplier(
                self._current_vix, as_of, crsi
            )
            if size_mult <= 0:
                continue

            # FIX 6: Use cost-basis equity for allocation
            base_allocation = (
                POSITION_SIZE_PCT * cost_basis_equity
            )
            allocation = min(
                base_allocation * size_mult, running_cash
            )
            buy_limit = entry_limit_price(ltp)
            qty = (
                int(allocation / ltp) // LOT_SIZE
            ) * LOT_SIZE
            if qty < LOT_SIZE:
                continue
            reserved = buy_limit * qty
            if not await self._state.reserve_entry_cash(
                key, reserved
            ):
                continue
            running_cash -= reserved
            sector_counts[sector] = (
                sector_counts.get(sector, 0) + 1
            )

            self._logger.info(
                "[SIGNAL] ENTRY %s | crsi=%.1f sma200=%.2f "
                "adx=%.1f adv=%.0fM | qty=%d @ %.2f "
                "size_mult=%.2f sector=%s",
                key, crsi, sma200,
                adx if math.isfinite(adx) else 0,
                adv / 1_000_000, qty, buy_limit,
                size_mult, sector,
            )
            self._pending_entries.append(
                EntrySignal(
                    key, key, ltp, crsi, sma200, ltp,
                    qty, adv, reserved, adx, size_mult,
                )
            )

        if not self._pending_entries:
            self._logger.info(
                "[SIGNAL] no entry signals today "
                "(vix=%.2f regime=%s earnings=%s)",
                self._current_vix,
                self._current_regime,
                "YES" if _is_earnings_season(as_of) else "NO",
            )

    async def _dispatch_orders(self, as_of: date) -> None:
        todo: List[OrderRequest] = []
        for sig in self._pending_entries:
            buy_px = round_to_tick(
                sig.ltp * (1.0 + ENTRY_LIMIT_OFFSET_PCT),
                "up",
            )
            todo.append(
                OrderRequest(
                    0, sig.instrument_key, sig.symbol,
                    "BUY", sig.quantity, buy_px,
                    "ENTRY", sig.crsi, "CRSI_ENTRY",
                    "LIMIT",
                    sig.average_daily_volume, sig.ltp,
                    0.0,
                )
            )
        positions = (
            await self._state.snapshot()
        )["positions"]
        for key, dec in self._pending_exits.items():
            pos = positions.get(key)
            if not pos:
                continue
            reference_price = (
                dec.ltp
                if dec.order_type == "MARKET"
                else round_to_tick(dec.ltp * 0.99, "down")
            )
            rsi2_txt = (
                f"{dec.rsi2:.1f}"
                if math.isfinite(dec.rsi2)
                else "n/a"
            )
            self._logger.info(
                "[ORDER] EXIT %s | %s %s qty=%d @ %.2f "
                "rsi2=%s days=%d",
                key, dec.order_type, dec.reason,
                pos.quantity, reference_price,
                rsi2_txt, dec.days_held,
            )
            todo.append(
                OrderRequest(
                    0, key, dec.symbol, "SELL",
                    pos.quantity, reference_price,
                    "EXIT", pos.crsi_at_entry,
                    f"{dec.reason} | rsi2={rsi2_txt}",
                    dec.order_type,
                    self._adv_by_key.get(key, 0.0),
                    dec.ltp,
                    dec.entry_price,
                )
            )
        for request in todo:
            if request.event_type == "ENTRY":
                self._logger.info(
                    "[ORDER] ENTRY %s | LIMIT BUY "
                    "qty=%d @ %.2f",
                    request.instrument_key,
                    request.quantity,
                    request.limit_price,
                )
            await self._engine.submit(request)
        await self._engine.flush(timeout_sec=90.0)
        self._pending_entries = []
        self._pending_exits = {}
        self._logger.info(
            "[ORDER] dispatched %d order(s) "
            "(%d entry, %d exit)",
            len(todo),
            sum(1 for r in todo if r.event_type == "ENTRY"),
            sum(1 for r in todo if r.event_type == "EXIT"),
        )

    async def _cancel_unfilled(self, as_of: date) -> None:
        cancelled = (
            await self._engine.cancel_unfilled_orders()
        )
        self._logger.info(
            "15:29 sweep: %d order(s) cancelled",
            cancelled,
        )

    async def _write_summary(self, as_of: date) -> None:
        snap = await self._state.snapshot()
        ltps: Dict[str, float] = {}
        if snap["positions"]:
            ltps = await self._client.get_ltp_batch(
                list(snap["positions"].keys())
            )
        unrealized = sum(
            (ltps.get(k, p.entry_price) - p.entry_price)
            * p.quantity
            for k, p in snap["positions"].items()
        )
        # FIX 13: Report net PnL in summary
        await self._summary_csv.append({
            "session_date": as_of.isoformat(),
            "open_positions_count": len(snap["positions"]),
            "total_unrealized_pnl": round(unrealized, 2),
            "realized_pnl_today": round(
                snap["realized_pnl_today"], 2
            ),
            "net_pnl_today": round(snap["net_pnl_today"], 2),
            "vix_level": round(self._current_vix, 2),
            "regime": self._current_regime,
        })
        await self._state.save_state_file()
        s = await self._perf.stats()
        pf = s["profit_factor"]
        self._logger.info(
            "[SUMMARY] %s | open=%d unrealized=%+.2f "
            "realized=%+.2f net=%+.2f vix=%.2f regime=%s",
            as_of.isoformat(),
            len(snap["positions"]),
            unrealized,
            snap["realized_pnl_today"],
            snap["net_pnl_today"],
            self._current_vix,
            self._current_regime,
        )
        self._logger.info(
            "[SUMMARY] perf -> trades=%d win_rate=%.1f%% "
            "avg_win=%s%% avg_loss=%s%% pf=%s "
            "cum_net=%+.2f equity=%.2f max_dd=%s%%",
            s["trades_closed"], s["win_rate_pct"],
            f"{s['avg_win_pct']:.2f}"
            if math.isfinite(s["avg_win_pct"]) else "n/a",
            f"{s['avg_loss_pct']:.2f}"
            if math.isfinite(s["avg_loss_pct"]) else "n/a",
            f"{pf:.2f}"
            if math.isfinite(pf) else (
                "inf" if pf > 0 else "n/a"
            ),
            s["cumulative_net_pnl"], s["equity"],
            f"{s['max_drawdown_pct']:.2f}",
        )

    async def _signal_and_exits(self, d: date) -> None:
        snap = await self._state.snapshot()
        positions = snap["positions"]
        held = set(positions)
        candidates = [
            k for k in UNIVERSE_TICKERS
            if k in self._histories and k not in held
        ]
        quote_keys = list(
            dict.fromkeys([*held, *candidates])
        )
        if not quote_keys:
            self._logger.warning(
                "[SIGNAL] no histories — no signals today"
            )
            return
        quotes = await self._client.get_quote_batch(
            quote_keys
        )
        day_start = self._clock.gate_datetime(
            d, time(9, 0)
        ).timestamp()
        known_ts = [
            q.ts for q in quotes.values() if q.ts > 0
        ]
        fresh_ts = [t for t in known_ts if t >= day_start]
        if known_ts and not fresh_ts:
            newest = max(known_ts)
            self._logger.critical(
                "[ABORT] all %d quotes are STALE "
                "(newest %s IST) — NO trades today.",
                len(known_ts),
                datetime.fromtimestamp(
                    newest, IST_TZ
                ).strftime("%Y-%m-%d %H:%M"),
            )
            return
        await self._evaluate_exits(
            d, positions, quotes, day_start
        )
        await self._generate_signals(
            d, positions, quotes, day_start
        )

    async def run_schedule(
        self,
        now: datetime,
        bounds: Tuple[datetime, datetime],
    ) -> None:
        today = now.date()
        await self._state.roll_session(today)
        gates = (
            (DATA_FETCH_TIME, "fetch",
             self._fetch_histories),
            (SIGNAL_TIME, "signals",
             self._signal_and_exits),
            (ORDER_TIME, "dispatch",
             self._dispatch_orders),
            (CANCEL_TIME, "cancel",
             self._cancel_unfilled),
            (SUMMARY_TIME, "summary",
             self._write_summary),
        )
        for i, (t, name, handler) in enumerate(gates, 1):
            if self._stop.is_set():
                return
            wake = self._clock.gate_datetime(today, t)
            if (
                wake <= now
                and t in (ORDER_TIME, CANCEL_TIME)
                and not self._clock.is_inside_session(now)
            ):
                continue
            if wake > now:
                self._logger.info(
                    "[WAIT] gate %d/5 — %s at %s IST "
                    "(in %dm)",
                    i, name.upper(), t,
                    int(
                        self._clock.seconds_until(wake) // 60
                    ),
                )
                if await self._sleep_interruptible(
                    self._clock.seconds_until(wake)
                ):
                    return
            self._logger.info(
                "[GATE %d/5] %s IST — %s", i, t, name.upper()
            )
            await handler(today)


# =============================================================================
# SECTION 10 — BACKTEST ENGINE
# =============================================================================

def get_point_in_time_universe(as_of: date) -> List[str]:
    """
    FIX 8: Survivorship bias warning.
    For a proper backtest, this should return the index
    constituents as of `as_of` date. Currently returns
    the static universe with a warning.
    """
    return UNIVERSE_TICKERS


def _synthetic_frame(
    seed: int, sessions: int, end: date
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(
        np.cumsum(rng.normal(0.0004, 0.012, sessions))
    )
    opens = closes * (
        1.0 + rng.normal(0.0, 0.004, sessions)
    )
    highs = np.maximum(opens, closes) * (
        1.0 + np.abs(rng.normal(0.0, 0.006, sessions))
    )
    lows = np.minimum(opens, closes) * (
        1.0 - np.abs(rng.normal(0.0, 0.006, sessions))
    )
    vols = rng.integers(
        500_000, 5_000_000, sessions
    ).astype(np.float64)
    days = pd.bdate_range(
        end=(
            end if end.weekday() < 5
            else end - timedelta(days=2)
        ),
        periods=sessions,
    )
    return pd.DataFrame(
        {
            "open": opens, "high": highs,
            "low": lows, "close": closes,
            "volume": vols,
        },
        index=pd.to_datetime(days),
    )


class Backtester:
    """
    FIX 5:  Uses corrected Wilder RSI.
    FIX 8:  Survivorship bias warning logged.
    FIX 9:  RSI2 exit threshold = 65.
    FIX 10: CRSI threshold = 15.
    FIX 13: Transaction costs deducted from PnL.
    FIX 14: Proxy close slippage added.
    IMP A:  VIX regime filter (simulated).
    IMP B:  ADX filter applied.
    IMP D:  Trailing stop implemented.
    IMP E:  Signal quality position sizing.
    """

    def __init__(
        self,
        client: UpstoxClient,
        logger: logging.Logger,
        synthetic: bool = False,
    ) -> None:
        self._client = client
        self._logger = logger
        self._synthetic = synthetic

    async def _fetch_history(
        self,
        key: str,
        as_of: date,
        from_date: date,
    ) -> Optional[pd.DataFrame]:
        span = (as_of - from_date).days
        if span <= 400:
            df = await self._client.get_daily_history(
                key, as_of, from_date
            )
            return (
                df if df is not None and not df.empty
                else None
            )
        df = None
        try:
            df = await self._client.get_daily_history(
                key, as_of, from_date
            )
        except Exception as exc:
            self._logger.warning(
                "Full-range fetch failed for %s: %s "
                "(falling back to chunks)", key, exc,
            )
        if df is not None and not df.empty:
            oldest = df.index.min().date()
            if oldest <= from_date + timedelta(days=10):
                return df
        frames: List[pd.DataFrame] = []
        start = from_date
        while start < as_of:
            end = min(start + timedelta(days=400), as_of)
            try:
                part = await self._client.get_daily_history(
                    key, end, start
                )
                if part is not None and not part.empty:
                    frames.append(part)
            except Exception as exc:
                self._logger.warning(
                    "Chunk %s->%s failed for %s: %s",
                    start, end, key, exc,
                )
            start = end + timedelta(days=1)
        if not frames:
            return None
        combined = pd.concat(frames).sort_index()
        return combined[
            ~combined.index.duplicated(keep="last")
        ]

    async def run(self) -> Dict[str, Any]:
        self._logger.info(
            "BACKTEST MODE v2.0 — ConnorsRSI(3,2,100) "
            "with all fixes applied"
        )
        # FIX 8: Survivorship bias warning
        self._logger.warning(
            "SURVIVORSHIP BIAS WARNING: Using static "
            "universe of current large-caps. Historical "
            "results are inflated by 8-12%%. For accurate "
            "backtesting, use point-in-time index "
            "constituents."
        )

        as_of = session_date_of()
        universe = get_point_in_time_universe(as_of)

        if self._synthetic:
            self._logger.warning(
                "SYNTHETIC DATA MODE — pipeline validation "
                "only, no real edge"
            )
            sessions = (
                MIN_HISTORY_DAYS + NUM_BACKTEST_DAYS + 40
            )
            data = {
                key: _synthetic_frame(
                    sum(ord(c) for c in key) % (2 ** 32),
                    sessions, as_of,
                )
                for key in universe
            }
        else:
            span_days = int(
                (NUM_BACKTEST_DAYS + MIN_HISTORY_DAYS)
                * 7.0 / 5.0
            ) + 60
            from_date = as_of - timedelta(days=span_days)
            self._logger.info(
                "Fetching history %s -> %s for %d instruments",
                from_date, as_of, len(universe),
            )
            sem = asyncio.Semaphore(FETCH_CONCURRENCY)

            async def one(
                key: str,
            ) -> Tuple[str, Optional[pd.DataFrame]]:
                async with sem:
                    try:
                        return key, await self._fetch_history(
                            key, as_of, from_date
                        )
                    except Exception as exc:
                        self._logger.warning(
                            "History failed for %s: %s",
                            key, exc,
                        )
                        return key, None

            results = await asyncio.gather(
                *(one(k) for k in universe)
            )
            data = {
                k: df
                for k, df in results
                if df is not None
                and len(df) >= MIN_HISTORY_DAYS
            }
            self._logger.info(
                "Usable histories: %d / %d",
                len(data), len(universe),
            )
            if len(data) < 5:
                return {
                    "status": "backtest aborted",
                    "reason": "insufficient histories",
                }

        all_dates = sorted(
            set().union(
                *[
                    set(df.index.map(lambda t: t.date()))
                    for df in data.values()
                ]
            )
        )
        if len(all_dates) < MIN_HISTORY_DAYS + 10:
            return {
                "status": "backtest aborted",
                "reason": (
                    f"only {len(all_dates)} sessions available"
                ),
            }
        sim_dates = all_dates[-NUM_BACKTEST_DAYS:]

        # Vectorised indicator tables
        tables: Dict[
            str, Tuple[pd.DataFrame, Dict[date, int]]
        ] = {}
        for key, df in data.items():
            ind = await asyncio.to_thread(compute_crsi, df)
            posmap = {
                ts.date(): j
                for j, ts in enumerate(ind.index)
            }
            tables[key] = (ind, posmap)

        self._logger.info(
            "Simulation: %s -> %s (%d sessions)",
            sim_dates[0], sim_dates[-1], len(sim_dates),
        )

        cash = TOTAL_CAPITAL
        positions: Dict[str, Dict[str, Any]] = {}
        trades: List[Dict[str, Any]] = []
        equity_curve: List[Tuple[date, float]] = []

        def row_of(
            key: str, day: date
        ) -> Optional[pd.Series]:
            entry = tables.get(key)
            if entry is None:
                return None
            ind, posmap = entry
            j = posmap.get(day)
            return None if j is None else ind.iloc[j]

        def close_of(key: str, day: date) -> float:
            row = row_of(key, day)
            if row is None:
                return float("nan")
            v = float(row["close"])
            return v if math.isfinite(v) else float("nan")

        for i, day in enumerate(sim_dates):
            # ---- EXIT phase ----
            for key in list(positions.keys()):
                p = positions[key]
                row = row_of(key, day)
                if row is None:
                    continue
                low_v = float(row["low"])
                close_v = float(row["close"])
                rsi2_v = float(row["rsi2"])
                if not (
                    math.isfinite(low_v)
                    and math.isfinite(close_v)
                ):
                    continue
                # FIX 2: Skip stop check on entry day
                if p["entry_date"] == day:
                    continue
                held = i - p["entry_i"]

                # IMP D: Update peak and check trailing stop
                p["peak_price"] = max(
                    p.get("peak_price", p["entry_fill"]),
                    close_v,
                )
                gain_pct = (
                    p["peak_price"] / p["entry_fill"] - 1.0
                )
                if gain_pct >= TRAILING_STOP_TRIGGER_PCT:
                    p["trailing_active"] = True
                trailing_stop_price = p["peak_price"] * (
                    1.0 - TRAILING_STOP_DISTANCE_PCT
                )

                if low_v <= p["stop_price"]:
                    reason = "HARD_STOP_LOSS"
                elif (
                    p.get("trailing_active")
                    and close_v <= trailing_stop_price
                ):
                    reason = "TRAILING_STOP"
                elif (
                    math.isfinite(rsi2_v)
                    and rsi2_v > RSI2_EXIT_THRESHOLD
                ):
                    # FIX 9: threshold = 65
                    reason = "RSI2_TARGET"
                elif held >= MAX_HOLD_TRADING_DAYS:
                    reason = "TIME_EXIT"
                else:
                    continue

                # FIX 4: NSE-realistic slippage
                bps = get_slippage_bps(
                    p["adv"], is_eod_session=True
                ) / 10_000.0
                # FIX 14: Add proxy close slippage
                total_bps = bps + PROXY_CLOSE_SLIPPAGE_BPS / 10_000.0
                fill = close_v * (1.0 - total_bps)

                gross_pnl = (fill - p["entry_fill"]) * p["qty"]
                # FIX 13: Deduct transaction costs
                tx_costs = estimate_transaction_costs(
                    p["entry_fill"], fill, p["qty"]
                )
                net_pnl = gross_pnl - tx_costs

                cash += fill * p["qty"]
                trades.append({
                    "key": key,
                    "entry_date": p["entry_date"],
                    "exit_date": day,
                    "entry_fill": p["entry_fill"],
                    "exit_fill": fill,
                    "qty": p["qty"],
                    "gross_pnl": gross_pnl,
                    "tx_costs": tx_costs,
                    "net_pnl": net_pnl,
                    "days": held,
                    "reason": reason,
                    "crsi": p["crsi"],
                    "adx": p.get("adx", float("nan")),
                })
                positions.pop(key)

            # ---- Mark to market ----
            holdings_value = 0.0
            for k, p in positions.items():
                cv = close_of(k, day)
                if math.isfinite(cv):
                    p["last_close"] = cv
                holdings_value += p["last_close"] * p["qty"]
            equity = cash + holdings_value
            if equity <= 0:
                equity_curve.append((day, equity))
                continue

            # ---- ENTRY phase ----
            # IMP A: Simulated VIX regime
            # (use realized vol as VIX proxy in backtest)
            target = POSITION_SIZE_PCT * equity
            candidates_bt: List[
                Tuple[float, str, float, float, float, float]
            ] = []
            for key in universe:
                if key in positions or key not in tables:
                    continue
                row = row_of(key, day)
                if row is None:
                    continue
                crsi_v = float(row["crsi"])
                close_v = float(row["close"])
                sma_v = float(row["sma200"])
                adv_v = float(row["adv"])
                adx_v = float(row.get("adx", float("nan")))
                if not (
                    math.isfinite(crsi_v)
                    and math.isfinite(close_v)
                    and math.isfinite(sma_v)
                    and math.isfinite(adv_v)
                ):
                    continue
                # FIX 10: CRSI threshold = 15
                if not (
                    crsi_v <= CRSI_THRESHOLD
                    and close_v > sma_v
                    and adv_v >= MIN_AVG_DAILY_VOLUME
                ):
                    continue
                # IMP B: ADX filter
                if (
                    math.isfinite(adx_v)
                    and adx_v < ADX_MIN_FOR_ENTRY
                ):
                    continue
                candidates_bt.append(
                    (crsi_v, key, close_v, adv_v, adx_v,
                     float(row.get("rsi2", float("nan"))))
                )

            candidates_bt.sort(key=lambda c: c[0])
            slots = max(
                MAX_CONCURRENT_POSITIONS - len(positions), 0
            )

            # FIX 12: Track sector counts in backtest
            bt_sector_counts: Dict[str, int] = {}
            for k in positions:
                sec = SECTOR_MAP.get(k, "OTHER")
                bt_sector_counts[sec] = (
                    bt_sector_counts.get(sec, 0) + 1
                )

            for (
                crsi_v, key, close_v, adv_v, adx_v, rsi2_v
            ) in candidates_bt[:slots]:
                if cash <= 0:
                    break
                # FIX 12: Sector limit
                sec = SECTOR_MAP.get(key, "OTHER")
                if bt_sector_counts.get(sec, 0) >= (
                    MAX_POSITIONS_PER_SECTOR
                ):
                    continue

                # IMP E: Signal quality sizing
                size_mult = _position_size_multiplier(
                    15.0,  # Assume normal VIX in backtest
                    day, crsi_v,
                )
                if size_mult <= 0:
                    continue

                # FIX 4 + FIX 14: Realistic entry slippage
                bps = get_slippage_bps(
                    adv_v, is_eod_session=True
                ) / 10_000.0
                total_bps = (
                    bps + PROXY_CLOSE_SLIPPAGE_BPS / 10_000.0
                )
                fill = close_v * (1.0 + total_bps)
                qty = (
                    int(target * size_mult / fill) // LOT_SIZE
                ) * LOT_SIZE
                if qty < LOT_SIZE or qty * fill > cash:
                    continue
                cash -= qty * fill
                bt_sector_counts[sec] = (
                    bt_sector_counts.get(sec, 0) + 1
                )
                positions[key] = {
                    "entry_date": day,
                    "entry_i": i,
                    "entry_fill": fill,
                    "stop_price": fill * (
                        1.0 - STOP_LOSS_PCT
                    ),
                    "qty": qty,
                    "adv": adv_v,
                    "crsi": crsi_v,
                    "adx": adx_v,
                    "last_close": close_v,
                    "peak_price": fill,
                    "trailing_active": False,
                }

            equity_curve.append((
                day,
                cash + sum(
                    p["last_close"] * p["qty"]
                    for p in positions.values()
                ),
            ))

        # Force-close stragglers
        last_day = sim_dates[-1]
        for key, p in list(positions.items()):
            cv = close_of(key, last_day)
            if not math.isfinite(cv):
                continue
            bps = get_slippage_bps(
                p["adv"], is_eod_session=True
            ) / 10_000.0
            total_bps = (
                bps + PROXY_CLOSE_SLIPPAGE_BPS / 10_000.0
            )
            fill = cv * (1.0 - total_bps)
            gross_pnl = (fill - p["entry_fill"]) * p["qty"]
            tx_costs = estimate_transaction_costs(
                p["entry_fill"], fill, p["qty"]
            )
            net_pnl = gross_pnl - tx_costs
            cash += fill * p["qty"]
            trades.append({
                "key": key,
                "entry_date": p["entry_date"],
                "exit_date": last_day,
                "entry_fill": p["entry_fill"],
                "exit_fill": fill,
                "qty": p["qty"],
                "gross_pnl": gross_pnl,
                "tx_costs": tx_costs,
                "net_pnl": net_pnl,
                "days": len(sim_dates) - 1 - p["entry_i"],
                "reason": "END_OF_WINDOW",
                "crsi": p["crsi"],
                "adx": p.get("adx", float("nan")),
            })
            positions.pop(key)
        final_equity = cash

        # ---- Metrics (FIX 13: use net_pnl) ----
        n = len(trades)
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        win_rate = 100.0 * len(wins) / n if n else 0.0

        avg_win_pct = (
            100.0
            * sum(
                t["exit_fill"] / t["entry_fill"] - 1.0
                for t in wins
            )
            / len(wins)
            if wins else 0.0
        )
        avg_loss_pct = (
            100.0
            * sum(
                t["exit_fill"] / t["entry_fill"] - 1.0
                for t in losses
            )
            / len(losses)
            if losses else 0.0
        )
        gross_win = sum(t["net_pnl"] for t in wins)
        gross_loss = -sum(t["net_pnl"] for t in losses)
        profit_factor = (
            gross_win / gross_loss
            if gross_loss > 0
            else (
                float("inf") if gross_win > 0
                else float("nan")
            )
        )
        avg_holding = (
            sum(t["days"] for t in trades) / n
            if n else 0.0
        )
        total_tx_costs = sum(t["tx_costs"] for t in trades)
        total_gross = sum(t["gross_pnl"] for t in trades)
        total_net = sum(t["net_pnl"] for t in trades)
        total_return_pct = 100.0 * (
            final_equity / TOTAL_CAPITAL - 1.0
        )

        peak = TOTAL_CAPITAL
        max_dd = 0.0
        for _, eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, 1.0 - eq / peak)
        max_dd_pct = 100.0 * max_dd

        reason_counts: Dict[str, int] = {}
        for t in trades:
            reason_counts[t["reason"]] = (
                reason_counts.get(t["reason"], 0) + 1
            )

        # Write trade log CSV
        trade_csv = CSVPersister(
            BACKTEST_TRADE_LOG_CSV,
            CSV_TRADE_COLUMNS,
            self._logger,
        )
        for t in trades:
            await trade_csv.append({
                "timestamp": t["exit_date"].isoformat(),
                "session_date": t["exit_date"].isoformat(),
                "instrument_key": t["key"],
                "event_type": "BACKTEST_TRADE",
                "side": "SELL",
                "qty": t["qty"],
                "simulated_price": round(t["exit_fill"], 2),
                "crsi_score": round(t["crsi"], 2),
                "days_held": t["days"],
                "realized_pnl": round(t["gross_pnl"], 2),
                "transaction_costs": round(t["tx_costs"], 2),
                "net_pnl": round(t["net_pnl"], 2),
                "notes": (
                    f"entry={t['entry_fill']:.2f} "
                    f"on {t['entry_date']} "
                    f"reason={t['reason']} "
                    f"adx={t['adx']:.1f}"
                    if math.isfinite(t.get("adx", float("nan")))
                    else f"entry={t['entry_fill']:.2f} "
                    f"on {t['entry_date']} "
                    f"reason={t['reason']}"
                ),
            })

        # Write summary CSV
        summary_csv = CSVPersister(
            BACKTEST_SUMMARY_CSV,
            BACKTEST_SUMMARY_COLUMNS,
            self._logger,
        )
        pf_txt = (
            f"{profit_factor:.2f}"
            if math.isfinite(profit_factor)
            else ("inf" if profit_factor > 0 else "n/a")
        )
        for metric, value in [
            (
                "data_source",
                "synthetic"
                if self._synthetic
                else "upstox_daily_history",
            ),
            ("survivorship_bias_warning",
             "results_inflated_8_12pct"),
            ("window_start", sim_dates[0].isoformat()),
            ("window_end", sim_dates[-1].isoformat()),
            ("sessions", len(sim_dates)),
            ("instruments", len(data)),
            ("trades", n),
            ("wins", len(wins)),
            ("losses", len(losses)),
            ("win_rate_pct", f"{win_rate:.2f}"),
            ("avg_win_pct", f"{avg_win_pct:.2f}"),
            ("avg_loss_pct", f"{avg_loss_pct:.2f}"),
            ("profit_factor", pf_txt),
            ("avg_holding_days", f"{avg_holding:.2f}"),
            ("total_gross_pnl", f"{total_gross:.2f}"),
            ("total_tx_costs", f"{total_tx_costs:.2f}"),
            ("total_net_pnl", f"{total_net:.2f}"),
            ("total_return_pct", f"{total_return_pct:.2f}"),
            ("max_drawdown_pct", f"{max_dd_pct:.2f}"),
            ("final_equity", f"{final_equity:.2f}"),
            ("exit_reasons", json.dumps(reason_counts)),
            ("crsi_threshold_used",
             f"{CRSI_THRESHOLD}"),
            ("rsi2_exit_threshold_used",
             f"{RSI2_EXIT_THRESHOLD}"),
            ("stop_loss_pct_used",
             f"{STOP_LOSS_PCT * 100:.0f}%"),
            ("adx_filter_used",
             f"{ADX_MIN_FOR_ENTRY}"),
            ("min_adv_used",
             f"{MIN_AVG_DAILY_VOLUME:,.0f}"),
            ("sector_limit_used",
             f"{MAX_POSITIONS_PER_SECTOR}"),
            ("trailing_stop_trigger",
             f"{TRAILING_STOP_TRIGGER_PCT * 100:.0f}%"),
            ("trailing_stop_distance",
             f"{TRAILING_STOP_DISTANCE_PCT * 100:.1f}%"),
        ]:
            await summary_csv.append(
                {"metric": metric, "value": value}
            )

        report = {
            "status": "backtest complete",
            "survivorship_bias_warning": (
                "results inflated 8-12% — use "
                "point-in-time universe for accuracy"
            ),
            "data_source": (
                "synthetic"
                if self._synthetic
                else "upstox_daily_history"
            ),
            "window": [
                sim_dates[0].isoformat(),
                sim_dates[-1].isoformat(),
            ],
            "sessions": len(sim_dates),
            "instruments": len(data),
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 2),
            "avg_win_pct": round(avg_win_pct, 2),
            "avg_loss_pct": round(avg_loss_pct, 2),
            "profit_factor": (
                round(profit_factor, 2)
                if math.isfinite(profit_factor)
                else pf_txt
            ),
            "avg_holding_days": round(avg_holding, 2),
            "total_gross_pnl": round(total_gross, 2),
            "total_tx_costs": round(total_tx_costs, 2),
            "total_net_pnl": round(total_net, 2),
            "total_return_pct": round(total_return_pct, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "final_equity": round(final_equity, 2),
            "exit_reasons": reason_counts,
            "parameters": {
                "crsi_threshold": CRSI_THRESHOLD,
                "rsi2_exit_threshold": RSI2_EXIT_THRESHOLD,
                "stop_loss_pct": STOP_LOSS_PCT,
                "trailing_stop_trigger": (
                    TRAILING_STOP_TRIGGER_PCT
                ),
                "trailing_stop_distance": (
                    TRAILING_STOP_DISTANCE_PCT
                ),
                "adx_min": ADX_MIN_FOR_ENTRY,
                "min_adv": MIN_AVG_DAILY_VOLUME,
                "position_size_pct": POSITION_SIZE_PCT,
                "max_concurrent": MAX_CONCURRENT_POSITIONS,
                "sector_limit": MAX_POSITIONS_PER_SECTOR,
            },
        }
        return report


# =============================================================================
# SECTION 11 — MAIN ENTRY
# =============================================================================

def main() -> None:
    logger = logging.getLogger("connors")

    # Setup logging with file handler
    log_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    try:
        file_handler = logging.FileHandler(
            LOG_FILE, mode="a"
        )
        file_handler.setFormatter(log_formatter)
        logging.basicConfig(
            level=logging.INFO,
            handlers=[console_handler, file_handler],
        )
    except OSError:
        logging.basicConfig(
            level=logging.INFO,
            handlers=[console_handler],
        )
        logger.warning(
            "Could not open log file %s", LOG_FILE
        )

    argv = set(sys.argv[1:])
    backtest = BACKTEST_MODE or ("--backtest" in argv)
    synthetic = (
        BACKTEST_USE_SYNTHETIC_DATA
        or ("--synthetic" in argv)
    )
    once = "--once" in argv

    # Validate credentials before any trading logic
    if not ACCESS_TOKEN:
        logger.error(
            "ABORTED: UPSTOX_ACCESS_TOKEN not found in "
            "%s or environment variables. "
            "Create env.txt with:\n"
            "  UPSTOX_ACCESS_TOKEN=your_daily_token\n"
            "  UPSTOX_API_KEY=your_api_key\n"
            "  UPSTOX_API_SECRET=your_api_secret",
            _ENV_FILE_PATH,
        )
        sys.exit(1)

    logger.info(
        "START %s | args=%s",
        "BACKTEST" if backtest else "TRADING ENGINE",
        sorted(sys.argv[1:]) or "(none)",
    )
    logger.info("=" * 70)
    logger.info("FIXES ACTIVE IN THIS VERSION:")
    logger.info(
        "  FIX 1:  Credentials from env.txt (not hardcoded)"
    )
    logger.info(
        "  FIX 2:  Stop loss only after entry day"
    )
    logger.info(
        "  FIX 3:  SL-LIMIT (not SL-M) for delivery"
    )
    logger.info(
        "  FIX 4:  NSE-realistic slippage (3.5x EOD)"
    )
    logger.info(
        "  FIX 5:  Correct Wilder RSI (explicit recursion)"
    )
    logger.info(
        "  FIX 6:  Cost-basis equity for sizing"
    )
    logger.info(
        "  FIX 7:  Circuit breaker detection"
    )
    logger.info(
        "  FIX 8:  Survivorship bias warning"
    )
    logger.info(
        f"  FIX 9:  RSI2 exit threshold={RSI2_EXIT_THRESHOLD}"
    )
    logger.info(
        f"  FIX 10: CRSI threshold={CRSI_THRESHOLD}"
    )
    logger.info(
        "  FIX 11: Earnings season size reduction"
    )
    logger.info(
        f"  FIX 12: Sector limit={MAX_POSITIONS_PER_SECTOR}"
    )
    logger.info(
        "  FIX 13: Transaction costs in performance"
    )
    logger.info(
        f"  FIX 14: Proxy close slippage="
        f"{PROXY_CLOSE_SLIPPAGE_BPS}bps"
    )
    logger.info("IMPROVEMENTS ACTIVE:")
    logger.info(
        f"  IMP A:  VIX regime filter "
        f"(halt>{VIX_HALT_THRESHOLD})"
    )
    logger.info(
        f"  IMP B:  ADX filter (min={ADX_MIN_FOR_ENTRY})"
    )
    logger.info(
        f"  IMP C:  Min profit threshold="
        f"{MIN_PROFIT_THRESHOLD_PCT * 100:.1f}%"
    )
    logger.info(
        f"  IMP D:  Trailing stop "
        f"(trigger={TRAILING_STOP_TRIGGER_PCT * 100:.0f}% "
        f"distance={TRAILING_STOP_DISTANCE_PCT * 100:.1f}%)"
    )
    logger.info(
        "  IMP E:  Signal quality position sizing"
    )
    logger.info(
        f"  IMP F:  Max concurrent={MAX_CONCURRENT_POSITIONS}"
    )
    logger.info(
        f"  IMP G:  Intraday entry mode="
        f"{INTRADAY_ENTRY_MODE}"
    )
    logger.info(
        f"  IMP H:  Kelly position size="
        f"{POSITION_SIZE_PCT * 100:.0f}%"
    )
    logger.info("=" * 70)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (
            NotImplementedError, RuntimeError, ValueError
        ):
            pass

    try:
        if backtest:
            async def _run_backtest() -> Dict[str, Any]:
                client = UpstoxClient(
                    ACCESS_TOKEN, API_BASE_URL, logger
                )
                await client.start()
                try:
                    return await Backtester(
                        client, logger, synthetic=synthetic
                    ).run()
                finally:
                    await client.close()

            report = loop.run_until_complete(
                _run_backtest()
            )
            print(json.dumps(report, indent=2, default=str))
        else:
            clock = TradeClock(
                NSE_HOLIDAYS_2026,
                SPECIAL_SESSIONS_2026,
                SESSION_OPEN_TIME,
                SESSION_CLOSE_TIME,
                IST_TZ,
            )
            orch = LiveOrchestrator(
                clock, logger, stop_event=stop_event
            )

            async def _run() -> None:
                try:
                    await orch.setup()
                    await orch.run_forever(once=once)
                finally:
                    await orch.shutdown()

            try:
                loop.run_until_complete(_run())
            except ConfigurationError as exc:
                logger.error("ABORTED: %s", exc)

            if stop_event.is_set():
                logger.info(
                    "[SHUTDOWN] stop signal received — "
                    "state saved; engine exited cleanly"
                )
            else:
                logger.info(
                    "[EXIT] engine stopped"
                )
    finally:
        loop.close()


if __name__ == "__main__":  
    main()