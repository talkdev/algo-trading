#!/usr/bin/env python3
"""
================================================================================
 CONNORSRSI (3, 2, 100) EOD SWING SYSTEM — NSE / Upstox v2 REST
 Canonical rules per Larry Connors & Cesar Alvarez, "Short Term Trading
 Strategies That Work" (2009) — the >70% win-rate backtested variant.
================================================================================

 ENTRY — checked ONCE per day at the market close (15:25 IST price is used
 as the closing-price proxy), all must hold:
   1. ConnorsRSI(3, 2, 100) <= 10
   2. Close (15:25 proxy) > 200-day simple moving average
   3. 20-day average daily volume >= 500,000 (liquidity filter)

 EXIT — checked ONCE per day at the close, in this exact precedence:
   1. HARD STOP   : the day's LOW <= 8% below entry  -> sell at the close
                    (MARKET order; replicates the backtest which triggers the
                    stop on the daily low and exits at the close).
   2. PROFIT TARGET: 2-period RSI (RSI-2) of the CLOSING prices crosses
                    above 70 -> sell at the close.
   3. TIME STOP   : held 5 TRADING days (not calendar days) -> sell at the
                    close.

 OPERATING CADENCE — this is a BATCH JOB, not a daemon. Run it once per
 trading day at 15:20 IST (cron: `20 15 * * 1-5`):
   15:20  fetch daily history
   15:25  evaluate exits (day's low vs stop, RSI-2 > 70, 5-day time stop)
          and generate entry signals; the 15:25 quote is the close proxy
   15:28  place all entry/exit orders
   15:29  CANCEL anything unfilled — no order is ever carried overnight
   15:30  write daily summary

 CHANGELOG vs previous version (SMA5-exit variant):
   * Exit #2 changed from "LTP > previous 5-day SMA" to "RSI-2 of close
     crosses above 70" (the proven momentum-overbought exit).
   * Stop is now evaluated on the intraday LOW from the Upstox market quote
     (day_low <= entry*0.92 -> MARKET sell at 15:25), instead of relying on
     the LTP or the broker-side DAY SL-M alone. The broker SL-M remains
     armed as a disaster backup only.
   * Time stop changed from 7 calendar days to 5 TRADING days.
   * Run cadence moved to the 15:20 / 15:25 / 15:28 / 15:29 batch schedule
     with an explicit end-of-day cancel sweep.
   * Entry trend filter now compares the 15:25 close proxy against the
     200-day SMA computed through that proxy (exact EOD semantics); CRSI
     uses "<= 10" as in the book.
   * Backtester implemented as a true EOD simulation of the same rules
     (the previous version was a stub).
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
from typing import Any, Dict, Final, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import quote as url_quote

import aiofiles
import aiohttp
import numpy as np
import pandas as pd

# =============================================================================
# SECTION 1 — HARDCODED CONFIGURATION
# =============================================================================

PAPER_TRADING_MODE: Final[bool] = True
BACKTEST_MODE: Final[bool] = False          # or run with:  python3 connors_rsi_swing.py --backtest
BACKTEST_USE_SYNTHETIC_DATA = False         # offline pipeline check:  --backtest --synthetic

# Deployment credentials are intentionally hardcoded per the deployment contract.
# Keep this file mode 0600 and rotate all values whenever the file is shared.
# NOTE: Upstox access tokens are single-session; the ACCESS_TOKEN must be
# refreshed before every day's 15:20 run (the previous token's exp is in the
# JWT payload — check it before each session).
UPSTOX_API_KEY: Final[str] = "ee2e4f16-0691-42d9-9a35-3f841ec29cbc"
UPSTOX_API_SECRET: Final[str] = "d3rr2a0uib"
ACCESS_TOKEN: Final[str] = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIzUkNRNFMiLCJqdGkiOiI2YTkyNzAxMTg3OWE1ZTQzY2Q3YzQyMjMiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzg3OTgxODQxLCJpc3MiOiJ1ZGFwaS1nYXRld2FheS1zZXJ2aWNlIiwiZXhwIjoxNzg4MDQwODAwfQ.Li02a6lopHl1FNykxizmAzfO6QEH78E0cEaHBozyOJE"

TOTAL_CAPITAL: Final[float] = 1_000_000.0
POSITION_SIZE_PCT: Final[float] = 0.05
MAX_DRAWDOWN_PCT: Final[float] = 0.10
STOP_LOSS_PCT: Final[float] = 0.08          # canonical 8% hard stop vs entry price

# --- Canonical strategy parameters (Connors & Alvarez, 2009) ---
CRSI_THRESHOLD: Final[float] = 10.0         # entry: ConnorsRSI(3,2,100) <= 10
RSI2_EXIT_THRESHOLD: Final[float] = 70.0    # exit: 2-period RSI of close > 70
MAX_HOLD_TRADING_DAYS: Final[int] = 5       # exit: 5 TRADING days time stop

ADV_LOOKBACK_DAYS: Final[int] = 20
MIN_AVG_DAILY_VOLUME: Final[float] = 500_000.0
SLIPPAGE_BUFFER_TICKS: Final[int] = 4
ENTRY_LIMIT_OFFSET_PCT: Final[float] = 0.0005  # 5 bps above LTP for buy limits
NSE_TICK_SIZE: Final[float] = 0.05

LOT_SIZE: Final[int] = 1
MAX_CONCURRENT_POSITIONS: Final[int] = 20
MIN_HISTORY_DAYS: Final[int] = 250
DATA_LOOKBACK_DAYS: Final[int] = 450
MAX_UNIVERSE_FAILURES: Final[int] = 10

NUM_BACKTEST_DAYS: Final[int] = 500         # trading days to simulate

API_BASE_URL: Final[str] = "https://api.upstox.com"
API_VERSION: Final[str] = "2.0"
HTTP_TIMEOUT_SEC: Final[float] = 10.0
MAX_HTTP_ATTEMPTS: Final[int] = 6
FETCH_CONCURRENCY: Final[int] = 4
PACING_MIN_INTERVAL_SEC: Final[float] = 0.05
PACING_MAX_INTERVAL_SEC: Final[float] = 10.0
EXEC_WORKERS: Final[int] = 4

PAPER_TRADE_LOG_CSV: Final[str] = "paper_trading_log.csv"
PAPER_TRADE_SUMMARY_CSV: Final[str] = "paper_trading_summary.csv"
PAPER_PERFORMANCE_CSV: Final[str] = "paper_performance.csv"
SPLIT_GUARD_MOVE_PCT: Final[float] = 0.25  # >25% move vs last close -> possible split
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
BACKTEST_SUMMARY_COLUMNS: Final[List[str]] = ["metric", "value"]

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
    "NSE_EQ|INE117A01022", "NSE_EQ|INE067A01029", "NSE_EQ|INE935N01020"
]

SESSION_OPEN_TIME: Final[time] = time(9, 15)
SESSION_CLOSE_TIME: Final[time] = time(15, 30)

SPECIAL_SESSIONS_2026: Final[Dict[date, Tuple[time, time]]] = {
    date(2026, 2, 1): (time(9, 15), time(15, 0)),
}

NSE_HOLIDAYS_2026: Final[Set[date]] = {
    date(2026, 1, 15), date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26),
    date(2026, 3, 31), date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1),
    date(2026, 5, 28), date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2),
    date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25)
}

# --- Daily batch cadence (spec: run once at 15:20 IST; close proxy at 15:25) ---
DATA_FETCH_TIME: Final[time] = time(15, 20)   # fetch daily history
SIGNAL_TIME: Final[time] = time(15, 25)       # 15:25 quote = close proxy; exits + entries decided
ORDER_TIME: Final[time] = time(15, 28)        # place all orders
CANCEL_TIME: Final[time] = time(15, 29)       # cancel anything unfilled — nothing overnight
SUMMARY_TIME: Final[time] = time(15, 30)      # write summary

# Terminal order statuses for the Upstox fill poller / cancel sweep.
TERMINAL_ORDER_STATUSES: Final[frozenset] = frozenset(
    {"complete", "traded", "rejected", "cancelled", "canceled", "day_closed"}
)

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
    return [list(items[i : i + size]) for i in range(0, len(items), size)]

def get_slippage_bps(average_daily_volume: float) -> float:
    """Scale simulated slippage from the instrument's trailing 20-session ADV."""
    adv = _safe_float(average_daily_volume, 0.0)
    if adv >= 10_000_000:
        return 3.0
    if adv >= 5_000_000:
        return 5.0
    if adv >= 2_000_000:
        return 8.0
    if adv >= 1_000_000:
        return 12.0
    if adv >= MIN_AVG_DAILY_VOLUME:
        return 20.0
    return 35.0


def entry_limit_price(ltp: float) -> float:
    """Bid at a marginal five-basis-point offset, rounded up to an NSE tick."""
    raw = ltp * (1.0 + ENTRY_LIMIT_OFFSET_PCT)
    return round(math.ceil(raw / NSE_TICK_SIZE) * NSE_TICK_SIZE, 2)


def round_to_tick(price: float, direction: str = "nearest") -> float:
    """Snap a price onto the 0.05 NSE tick grid (Upstox rejects off-tick
    limit prices). 'up' for buy limits, 'down' for sell limits."""
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
    """Offline validity check: decode the JWT `exp` claim (epoch seconds).
    Returns None when the token shape is unreadable."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return float(json.loads(base64.urlsafe_b64decode(part))["exp"])
    except Exception:
        return None


# =============================================================================
# SECTION 3 — EXCEPTIONS & DOMAIN TYPES
# =============================================================================

class ConfigurationError(RuntimeError): pass
class UpstoxError(RuntimeError): pass
class UpstoxTransportError(UpstoxError): pass
class UpstoxRateLimited(UpstoxError): pass
class UpstoxApiError(UpstoxError):
    def __init__(self, code: Optional[str], message: str, http_status: Optional[int] = None) -> None:
        super().__init__(f"[{code or 'N/A'}] {message} (HTTP {http_status})")
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

@dataclass(frozen=True)
class ClosedTrade:
    position: Position
    exit_price: float
    realized_pnl: float
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
    """EOD proxy quote at 15:25: last price (close proxy) + intraday low.
    `ts` is the quote timestamp in epoch seconds (0 = unknown, e.g. when
    only the LTP fallback endpoint responded); used for stale-quote guards."""
    last_price: float
    low_price: float = float("nan")
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

@dataclass(frozen=True)
class ExitDecision:
    instrument_key: str
    symbol: str
    ltp: float
    reason: str
    days_held: int
    order_type: str = "LIMIT"
    rsi2: float = float("nan")

# =============================================================================
# SECTION 5 — PERSISTENCE & LEDGER & STATE
# =============================================================================

class CSVPersister:
    def __init__(self, path: str, columns: Sequence[str], logger: logging.Logger) -> None:
        self._path = Path(path)
        self._columns = list(columns)
        self._logger = logger
        self._lock = asyncio.Lock()
        self._header_ready = False

    @staticmethod
    def _serialize_row(columns: Sequence[str], row: Mapping[str, Any], header: bool) -> str:
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore")
        if header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in columns})
        return buf.getvalue()

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._path.parent.mkdir, parents=True, exist_ok=True)
            if not self._header_ready:
                has_content = await asyncio.to_thread(
                    lambda: self._path.exists() and self._path.stat().st_size > 0
                )
                self._header_ready = True
            else:
                has_content = True
            payload = self._serialize_row(self._columns, row, header=not has_content)
            async with aiofiles.open(self._path, "a", encoding="utf-8", newline="") as fh:
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
        async with self._lock: return len(self._events)

class State:
    def __init__(self, logger: logging.Logger, initial_cash: float, paper: bool) -> None:
        self._logger = logger
        self._lock = asyncio.Lock()
        self.paper = paper
        self.positions: Dict[str, Position] = {}
        self._entry_index: Dict[str, date] = {}
        self._cash_reservations: Dict[str, float] = {}
        self._protective_orders: Dict[str, str] = {}
        self.cash_available: float = initial_cash
        self.realized_pnl_today: float = 0.0
        self.session_date: Optional[date] = None
        self.last_completed_session: Optional[date] = None
        self.high_water_mark: float = initial_cash
        self.drawdown_latched: bool = False

    async def roll_session(self, new_session: date) -> None:
        async with self._lock:
            if self.session_date != new_session:
                self.session_date = new_session
                self.realized_pnl_today = 0.0
                # Portfolio high-water mark and breaker latch intentionally persist
                # across sessions. Recovery requires an explicit operational reset.

    async def reserve_entry_cash(self, key: str, amount: float) -> bool:
        async with self._lock:
            if amount <= 0.0 or key in self.positions or key in self._cash_reservations:
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

    async def set_protective_order(self, key: str, order_id: str) -> None:
        async with self._lock:
            self._protective_orders[key] = order_id

    async def pop_protective_order(self, key: str) -> str:
        async with self._lock:
            return self._protective_orders.pop(key, "")

    async def get_protective_order(self, key: str) -> str:
        async with self._lock:
            return self._protective_orders.get(key, "")

    async def open_position(self, pos: Position, cost: float) -> bool:
        async with self._lock:
            reserved = self._cash_reservations.pop(pos.instrument_key, 0.0)
            spendable = self.cash_available + reserved
            if pos.instrument_key in self.positions:
                self.cash_available = spendable
                return False
            if cost > spendable:
                self.cash_available = spendable
                self._logger.warning(
                    "Entry dropped for %s: cost %.2f exceeds spendable cash %.2f",
                    pos.instrument_key, cost, spendable,
                )
                return False
            self.positions[pos.instrument_key] = pos
            self._entry_index.setdefault(pos.instrument_key, pos.entry_date)
            self.cash_available = spendable - cost
            return True

    async def close_position(
        self, key: str, exit_price: float, exit_date: date, reason: str,
        quantity: Optional[int] = None,
    ) -> Optional[ClosedTrade]:
        async with self._lock:
            pos = self.positions.get(key)
            if pos is None:
                return None
            closed_qty = min(pos.quantity, quantity if quantity is not None else pos.quantity)
            if closed_qty <= 0:
                return None
            closed_pos = replace(pos, quantity=closed_qty)
            remaining = pos.quantity - closed_qty
            if remaining:
                self.positions[key] = replace(pos, quantity=remaining)
            else:
                self.positions.pop(key, None)
                self._entry_index.pop(key, None)
            realized = (exit_price - pos.entry_price) * closed_qty
            self.realized_pnl_today += realized
            self.cash_available += exit_price * closed_qty
            days_held = max((exit_date - pos.entry_date).days, 0)
            return ClosedTrade(closed_pos, exit_price, realized, days_held, reason)

    async def mark_session_completed(self, d: date) -> None:
        """Latch the finished session so a re-run the same day is a no-op."""
        async with self._lock:
            self.last_completed_session = d

    async def session_completed_on(self) -> Optional[date]:
        async with self._lock:
            return self.last_completed_session

    async def update_drawdown(self, current_equity: float) -> bool:
        async with self._lock:
            self.high_water_mark = max(self.high_water_mark, current_equity)
            if current_equity <= self.high_water_mark * (1.0 - MAX_DRAWDOWN_PCT):
                self.drawdown_latched = True
            return self.drawdown_latched

    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "positions": dict(self.positions),
                "cash_available": self.cash_available,
                "reserved_cash": sum(self._cash_reservations.values()),
                "cash_reservations": dict(self._cash_reservations),
                "protective_orders": dict(self._protective_orders),
                "realized_pnl_today": self.realized_pnl_today,
                "last_completed_session": self.last_completed_session.isoformat() if self.last_completed_session else None,
                "high_water_mark": self.high_water_mark,
                "drawdown_latched": self.drawdown_latched,
            }

    async def save_state_file(self) -> None:
        async with self._lock:
            payload = {
                "paper": self.paper,
                "session_date": self.session_date.isoformat() if self.session_date else None,
                "cash_available": self.cash_available,
                "cash_reservations": dict(self._cash_reservations),
                "protective_orders": dict(self._protective_orders),
                "last_completed_session": self.last_completed_session.isoformat() if self.last_completed_session else None,
                "high_water_mark": self.high_water_mark,
                "drawdown_latched": self.drawdown_latched,
                "positions": [
                    {
                        "instrument_key": p.instrument_key, "symbol": p.symbol,
                        "entry_date": p.entry_date.isoformat(), "entry_price": p.entry_price,
                        "quantity": p.quantity, "crsi_at_entry": p.crsi_at_entry,
                        "paper": p.paper, "notes": p.notes,
                    }
                    for p in self.positions.values()
                ],
            }
        await asyncio.to_thread(Path(STATE_FILE).parent.mkdir, parents=True, exist_ok=True)
        serialized = await asyncio.to_thread(json.dumps, payload, indent=2)
        async with aiofiles.open(STATE_FILE, "w", encoding="utf-8") as fh:
            await fh.write(serialized)
            await fh.flush()

    @staticmethod
    async def load_state_file(logger: logging.Logger) -> Optional[Dict[str, Any]]:
        path = Path(STATE_FILE)
        if not await asyncio.to_thread(path.exists):
            return None
        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as fh:
                data = json.loads(await fh.read())
            positions = {
                r["instrument_key"]: Position(
                    r["instrument_key"], r.get("symbol", r["instrument_key"]),
                    date.fromisoformat(r["entry_date"]), float(r["entry_price"]),
                    int(r["quantity"]), float(r.get("crsi_at_entry", 0.0)),
                    bool(r.get("paper", True)), str(r.get("notes", "")),
                )
                for r in data.get("positions", [])
            }
            return {
                "positions": positions,
                "session_date": date.fromisoformat(data["session_date"]) if data.get("session_date") else None,
                "last_completed_session": date.fromisoformat(data["last_completed_session"]) if data.get("last_completed_session") else None,
                "cash_available": float(data.get("cash_available", TOTAL_CAPITAL)),
                "cash_reservations": {
                    str(k): float(v) for k, v in data.get("cash_reservations", {}).items()
                },
                "protective_orders": {
                    str(k): str(v) for k, v in data.get("protective_orders", {}).items()
                },
                "high_water_mark": float(data.get("high_water_mark", TOTAL_CAPITAL)),
                "drawdown_latched": bool(data.get("drawdown_latched", False)),
                "paper": bool(data.get("paper", True)),
            }
        except Exception as exc:
            logger.exception("Could not load state file: %s", exc)
            return None

class TradeClock:
    def __init__(self, holidays: Set[date], special_sessions: Dict[date, Tuple[time, time]], open_time: time, close_time: time, tz: timezone) -> None:
        self._holidays = frozenset(holidays)
        self._special = dict(special_sessions)
        self._open_time, self._close_time = open_time, close_time
        self._tz = tz

    def session_bounds_for(self, d: date) -> Optional[Tuple[datetime, datetime]]:
        if d in self._special: return (datetime.combine(d, self._special[d][0], tzinfo=self._tz), datetime.combine(d, self._special[d][1], tzinfo=self._tz))
        if d.weekday() >= 5 or d in self._holidays: return None
        return (datetime.combine(d, self._open_time, tzinfo=self._tz), datetime.combine(d, self._close_time, tzinfo=self._tz))

    def is_inside_session(self, now: Optional[datetime] = None) -> bool:
        bounds = self.session_bounds_for((now or ist_now()).date())
        return bounds[0] <= (now or ist_now()) <= bounds[1] if bounds else False
    def gate_datetime(self, d: date, t: time) -> datetime: return datetime.combine(d, t, tzinfo=self._tz)
    def seconds_until(self, target: datetime, now: Optional[datetime] = None) -> float: return max(0.0, (target - (now or ist_now())).total_seconds())

class PacingGate:
    """Minimum-interval pacing for API calls.

    Race/starvation fix: the wait slot is reserved under the lock but the
    SLEEP happens OUTSIDE the lock, so one caller can never block the lock
    (and thus every other caller) for the full pacing interval."""

    def __init__(self, min_interval: float, max_interval: float) -> None:
        self._min_interval, self._max_interval = float(min_interval), float(max_interval)
        self._lock = asyncio.Lock()
        self._next_free_mono = 0.0
        self._clean_streak = 0

    async def acquire(self) -> None:
        async with self._lock:
            slot = max(self._next_free_mono, wall_clock.monotonic())
            self._next_free_mono = slot + self._min_interval
        wait = slot - wall_clock.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def on_success(self) -> None:
        async with self._lock:
            self._clean_streak += 1
            if self._clean_streak >= 200:
                self._min_interval, self._clean_streak = max(PACING_MIN_INTERVAL_SEC, self._min_interval * 0.75), 0

    async def on_429(self) -> None:
        async with self._lock:
            self._clean_streak, self._min_interval = 0, min(self._max_interval, max(self._min_interval * 2.0, 0.25))

# =============================================================================
# SECTION 10 — UPSTOX v2 REST CLIENT
# =============================================================================

class UpstoxClient:
    INTERVAL_DAY: Final[str] = "day"

    def __init__(self, access_token: str, base_url: str, logger: logging.Logger) -> None:
        self._token, self._base, self._logger = access_token, base_url.rstrip("/"), logger
        self._session: Optional[aiohttp.ClientSession] = None
        self._paces = {
            "default": PacingGate(PACING_MIN_INTERVAL_SEC, PACING_MAX_INTERVAL_SEC),
            "data": PacingGate(1.0, 10.0),
            "order": PacingGate(PACING_MIN_INTERVAL_SEC, PACING_MAX_INTERVAL_SEC),
        }

    async def start(self) -> None:
        if not self._session:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SEC, connect=5.0),
                headers={"Accept": "application/json", "Api-Version": API_VERSION},
            )
    async def close(self) -> None:
        if self._session: await self._session.close(); self._session = None

    async def _request(self, method: str, path: str, gate_key: str = "default", params: Optional[Mapping[str, str]] = None, json_body: Optional[Mapping[str, Any]] = None, attempt_budget: Optional[int] = None) -> Dict[str, Any]:
        budget = attempt_budget or MAX_HTTP_ATTEMPTS
        gate = self._paces[gate_key]
        for attempt in range(1, budget + 1):
            await gate.acquire()
            headers = {"Authorization": f"Bearer {self._token}"} if self._token and "YOUR_" not in self._token else {}
            if json_body: headers["Content-Type"] = "application/json"
            try:
                async with self._session.request(method, f"{self._base}{path}", params=params, json=json_body, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        await gate.on_429()
                        raise UpstoxRateLimited("HTTP 429 rate limited")
                    if resp.status >= 500: raise UpstoxTransportError(f"HTTP {resp.status}")
                    payload = json.loads(body) if body else {}
                    if payload.get("status") == "error" or resp.status >= 400:
                        errs = payload.get("errors") or []
                        raise UpstoxApiError(errs[0].get("errorCode") if errs else None, errs[0].get("message") if errs else f"HTTP {resp.status}", resp.status)
                    await gate.on_success()
                    return payload
            except UpstoxRateLimited:
                if attempt >= budget: raise
            except UpstoxTransportError:
                if attempt >= budget: raise
            except Exception as exc:
                if attempt >= budget: raise UpstoxTransportError(str(exc))
            await asyncio.sleep(min(30.0, 0.5 * (2 ** (attempt - 1))) * (0.8 + 0.4 * random.random()))
        raise UpstoxError(f"request exhausted retries: {path}")

    async def validate_credentials(self) -> None:
        try: await self._request("GET", "/v2/user/profile", attempt_budget=2)
        except Exception as exc: raise ConfigurationError(f"Upstox token rejected: {exc}")

    async def get_real_cash_balance(self) -> float:
        payload = await self._request("GET", "/v2/user/get-funds-and-margin?segment=SEC")
        return _safe_float((payload.get("data") or {}).get("equity", {}).get("available_margin"), TOTAL_CAPITAL)

    @staticmethod
    def _candles_to_frame(candles: Sequence[Sequence[Any]]) -> pd.DataFrame:
        rows = [
            [datetime.fromisoformat(str(c[0])), float(c[1]), float(c[2]),
             float(c[3]), float(c[4]), int(float(c[5]))]
            for c in candles if len(c) >= 6
        ]
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = (
            pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
            .sort_values("datetime").drop_duplicates("datetime").set_index("datetime")
        )
        return frame[["open", "high", "low", "close", "volume"]]

    async def get_daily_history(self, instrument_key: str, to_date: date, from_date: date) -> pd.DataFrame:
        payload = await self._request(
            "GET",
            f"/v2/historical-candle/{url_quote(instrument_key, safe='')}/{self.INTERVAL_DAY}/{to_date.isoformat()}/{from_date.isoformat()}",
            gate_key="data",
        )
        candles = (payload.get("data") or {}).get("candles", [])
        return await asyncio.to_thread(self._candles_to_frame, candles)

    async def fetch_daily_histories_bulk(self, keys: Sequence[str], to_date: date, lookback_days: int) -> Dict[str, Optional[pd.DataFrame]]:
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        async def one(key: str) -> Tuple[str, Optional[pd.DataFrame]]:
            async with sem:
                try: return key, await self.get_daily_history(key, to_date, to_date - timedelta(days=lookback_days))
                except Exception: return key, None
        return dict(await asyncio.gather(*(one(k) for k in keys)))

    async def get_ltp_batch(self, keys: Sequence[str]) -> Dict[str, float]:
        out = {}
        for chunk in _chunks(list(keys), 50):
            payload = await self._request("GET", "/v2/market-quote/ltp", params={"instrument_key": ",".join(chunk)})
            for key, blob in (payload.get("data") or {}).items():
                if blob and "last_price" in blob: out[key] = float(blob["last_price"])
        return out

    @staticmethod
    def _quote_map_from_any(data: Any) -> Dict[str, Mapping[str, Any]]:
        """Normalise segment/instrument quote payloads to {instrument_key: blob}."""
        if isinstance(data, Mapping):
            return {str(k): v for k, v in data.items() if isinstance(v, Mapping)}
        if isinstance(data, list):
            out: Dict[str, Mapping[str, Any]] = {}
            for item in data:
                if isinstance(item, Mapping):
                    k = str(item.get("instrument_key") or item.get("instrument_token") or "")
                    if k: out[k] = item
            return out
        return {}

    async def get_segment_quotes(self, segment: str = "EQ") -> Dict[str, Mapping[str, Any]]:
        """Full-market quote in ONE request (last_price, low_price, high_price, ...)."""
        payload = await self._request("GET", f"/v2/market-quote/segment/{segment}")
        return self._quote_map_from_any(payload.get("data"))

    @staticmethod
    def _ts_seconds(value: Any) -> float:
        """Upstox quote timestamps are epoch MILLISECONDS; normalise to seconds."""
        t = _safe_float(value, 0.0)
        if t > 1e12:
            t /= 1000.0
        return t if t > 0 else 0.0

    @staticmethod
    def _quote_from_blob(blob: Mapping[str, Any]) -> QuoteData:
        return QuoteData(
            last_price=_safe_float(blob.get("last_price"), float("nan")),
            low_price=_safe_float(blob.get("low_price"), float("nan")),
            ts=UpstoxClient._ts_seconds(blob.get("timestamp")),
        )

    async def get_instrument_quote(self, key: str) -> QuoteData:
        payload = await self._request("GET", f"/v2/market-quote/instrument/{url_quote(key, safe='')}")
        blob = None
        data = payload.get("data")
        if isinstance(data, Mapping):
            blob = data
        elif isinstance(data, list) and data:
            blob = data[-1]
        blob = blob or {}
        return self._quote_from_blob(blob)

    async def get_quote_batch(self, keys: Sequence[str]) -> Dict[str, QuoteData]:
        """15:25 close-proxy quotes: last price + INTRADAY LOW for stop checks.

        Prefers the single-request segment endpoint, falls back to per-instrument
        quotes, then to the LTP endpoint (low unknown -> LTP used as low proxy).
        """
        ordered = list(dict.fromkeys(keys))
        if not ordered:
            return {}
        out: Dict[str, QuoteData] = {}
        seg: Optional[Dict[str, Mapping[str, Any]]] = None
        try:
            seg = await self.get_segment_quotes("EQ")
        except Exception as exc:
            self._logger.warning("Segment quote failed (%s); using per-instrument quotes", exc)
        for key in ordered:
            blob = (seg or {}).get(key) or {}
            lp = _safe_float(blob.get("last_price"), float("nan"))
            lo = _safe_float(blob.get("low_price"), float("nan"))
            if math.isfinite(lp):
                out[key] = QuoteData(lp, lo, self._ts_seconds(blob.get("timestamp")))
        missing = [k for k in ordered if k not in out]
        if missing:
            sem = asyncio.Semaphore(FETCH_CONCURRENCY)
            async def one(key: str) -> Tuple[str, QuoteData]:
                async with sem:
                    try:
                        return key, await self.get_instrument_quote(key)
                    except Exception:
                        return key, QuoteData(float("nan"), float("nan"))
            for key, q in await asyncio.gather(*(one(k) for k in missing)):
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
                self._logger.warning("LTP fallback failed: %s", exc)
        return out

    async def get_delivery_positions(self) -> List[Dict[str, Any]]:
        return (await self._request("GET", "/v2/portfolio/long-term-positions")).get("data") or []

    async def get_order_history(self, tag: str) -> List[Dict[str, Any]]:
        return (await self._request("GET", f"/v2/order/history?tag={tag}", gate_key="order")).get("data") or []

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Currently resting orders (crash-recovery cleanup at startup)."""
        return (await self._request("GET", "/v2/order/open-orders", gate_key="order")).get("data") or []

    async def place_order(
        self, *, instrument_key: str, side: str, quantity: int, price: float,
        tag: str, order_type: str = "LIMIT", trigger_price: float = 0.0,
    ) -> Dict[str, Any]:
        """Place an idempotent LIMIT, MARKET, or protective SL-M order."""
        existing = await self.get_order_history(tag)
        if existing:
            return {"status": "success", "data": existing[-1]}
        normalized_type = order_type.upper()
        if normalized_type not in {"LIMIT", "MARKET", "SL-M"}:
            raise ValueError(f"Unsupported order type: {order_type}")
        body = {
            "instrument_token": instrument_key,
            "transaction_type": side,
            "order_type": normalized_type,
            "quantity": int(quantity),
            "price": 0 if normalized_type in {"MARKET", "SL-M"} else float(price),
            "product": "D", "validity": "DAY", "disclosed_quantity": 0,
            "trigger_price": float(trigger_price) if normalized_type == "SL-M" else 0.0,
            "is_amo": False, "market_protection": -1,
            "tag": tag,
        }
        return await self._request(
            "POST", "/v2/order/place", gate_key="order", json_body=body,
            attempt_budget=3,
        )

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Mandatory broker-side cleanup for a non-terminal order."""
        if not order_id:
            raise ValueError("Cannot cancel an order without order_id")
        return await self._request(
            "DELETE", "/v2/order/cancel", gate_key="order",
            params={"order_id": order_id}, attempt_budget=3,
        )


# =============================================================================
# SECTION 11 — CONNORSRSI INDICATORS (canonical formulas)
# =============================================================================
# ConnorsRSI = ( RSI(3, close) + RSI(2, streak of close) + 100-day percentile
#               rank of the daily percentage change ) / 3
# All RSIs use Wilder smoothing, exactly as in the book.

def _wild_avg_with_seed(x: np.ndarray, period: int) -> np.ndarray:
    n = x.size; out = np.full(n, np.nan)
    if n < period: return out
    out[period - 1] = float(np.mean(x[:period]))
    if n > period:
        r = 1.0 - 1.0 / period
        j = np.arange(1, n - period + 1)
        out[period:] = (r ** j) * (out[period - 1] + (1.0 / period) * np.cumsum(x[period:] * (r ** -j)))
    return out

def _wilder_rsi(gains: pd.Series, losses: pd.Series, period: int) -> pd.Series:
    avg_g = _wild_avg_with_seed(gains.to_numpy(dtype=np.float64)[1:], period)
    avg_l = _wild_avg_with_seed(losses.to_numpy(dtype=np.float64)[1:], period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = np.where(avg_l == 0.0, 100.0, 100.0 - 100.0 / (1.0 + avg_g / avg_l))
    rsi = np.where((avg_g == 0.0) & (avg_l == 0.0), 50.0, rsi)
    rsi[np.isnan(avg_g) | np.isnan(avg_l)] = np.nan
    return pd.Series(rsi, index=gains.index[1:])

def streak_array(close: np.ndarray) -> np.ndarray:
    out = np.zeros(close.shape[0], dtype=np.int64); cur = 0
    for i in range(1, close.shape[0]):
        prev, val = close[i - 1], close[i]
        if not (math.isfinite(prev) and math.isfinite(val)): cur = 0
        elif val > prev: cur = cur + 1 if cur > 0 else 1
        elif val < prev: cur = cur - 1 if cur < 0 else -1
        else: cur = 0
        out[i] = cur
    return out

def compute_crsi(hist: pd.DataFrame) -> pd.DataFrame:
    """Vectorised indicators over completed daily bars (backtest / warm data)."""
    if hist.empty: return hist.copy()
    df = hist.copy(); close = df["close"].astype(np.float64)
    df["sma200"] = close.rolling(200, min_periods=200).mean()
    df["rsi3"] = _wilder_rsi(close.diff().clip(lower=0.0), (-close.diff()).clip(lower=0.0), 3)
    df["streak"] = streak_array(close.to_numpy(dtype=np.float64))
    df["rsi_streak"] = _wilder_rsi(df["streak"].diff().clip(lower=0.0), (-df["streak"].diff()).clip(lower=0.0), 2)
    df["pct_rank"] = (close.pct_change().rolling(100, min_periods=100).rank(pct=True) * 100.0)
    df["crsi"] = (df["rsi3"] + df["rsi_streak"] + df["pct_rank"]) / 3.0
    # The proven exit indicator: 2-period Wilder RSI of the CLOSING prices.
    df["rsi2"] = _wilder_rsi(close.diff().clip(lower=0.0), (-close.diff()).clip(lower=0.0), 2)
    df["adv"] = df["volume"].rolling(ADV_LOOKBACK_DAYS, min_periods=ADV_LOOKBACK_DAYS).mean()
    return df


def _closes_upto(hist: pd.DataFrame, as_of: date, proxy_close: float) -> np.ndarray:
    """Daily closes through `as_of`, with today's in-progress bar (if the API
    included it) replaced by the 15:25 close proxy. Mirrors EOD semantics: the
    decision day's close is the proxy itself."""
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
    rsi3 = _wilder_rsi(delta.clip(lower=0.0), (-delta).clip(lower=0.0), 3).iloc[-1]
    streak = pd.Series(streak_array(values), dtype=np.float64)
    streak_delta = streak.diff()
    rsi_streak = _wilder_rsi(streak_delta.clip(lower=0.0), (-streak_delta).clip(lower=0.0), 2).iloc[-1]
    pct_rank = close.pct_change().iloc[-100:].rank(pct=True).iloc[-1] * 100.0
    return float((rsi3 + rsi_streak + pct_rank) / 3.0)

def _rsi2_from_closes(values: np.ndarray) -> float:
    close = pd.Series(values, dtype=np.float64)
    delta = close.diff()
    rsi2 = _wilder_rsi(delta.clip(lower=0.0), (-delta).clip(lower=0.0), 2)
    return float(rsi2.iloc[-1]) if len(rsi2) else float("nan")

def intraday_rsi2(hist: Optional[pd.DataFrame], ltp: float, as_of: date) -> float:
    """RSI-2 of the closing prices with the 15:25 price appended as today's
    close proxy (the exit indicator of the canonical strategy)."""
    if hist is None or hist.empty or not math.isfinite(ltp):
        return float("nan")
    values = _closes_upto(hist, as_of, ltp)
    if values.size < 30:
        return float("nan")
    return _rsi2_from_closes(values)

def average_daily_volume(hist: pd.DataFrame) -> float:
    if hist.empty or "volume" not in hist:
        return 0.0
    return float(hist["volume"].tail(ADV_LOOKBACK_DAYS).mean())

def candidate_metrics(
    hist: pd.DataFrame, ltp: float, as_of: date,
) -> Optional[Tuple[float, float, float]]:
    """CPU-bound entry evaluation, called only via to_thread.

    Returns (sma200_through_proxy, crsi_with_proxy, adv20) using the 15:25
    price as the closing price of the decision day.
    """
    if hist is None or len(hist) < MIN_HISTORY_DAYS or not math.isfinite(ltp):
        return None
    values = _closes_upto(hist, as_of, ltp)
    if values.size < 201:
        return None
    sma200_proxy = float(np.mean(values[-200:]))
    crsi = _crsi_from_closes(values)
    adv = average_daily_volume(hist)
    return sma200_proxy, crsi, adv

def trading_days_since_entry(
    hist: Optional[pd.DataFrame], entry_date: date, as_of: date
) -> Optional[int]:
    """Sessions strictly after `entry_date` up to and including `as_of`.

    Counts the union of observed bar dates and the decision day, so the result
    is identical whether or not the API has returned today's in-progress bar.
    Falls back to a 5/7 calendar estimate when history is unavailable.
    """
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

# =============================================================================
# SECTION 12A — CUMULATIVE PERFORMANCE TRACKER
# =============================================================================

PERF_COLUMNS: Final[List[str]] = [
    "close_time", "session_date", "instrument_key", "entry_date",
    "entry_price", "exit_price", "qty", "realized_pnl", "pnl_pct",
    "days_held", "exit_reason", "trades_closed", "wins", "losses",
    "win_rate_pct", "avg_win_pct", "avg_loss_pct", "profit_factor",
    "cumulative_pnl", "equity", "max_drawdown_pct", "open_positions",
]


class PerformanceTracker:
    """Cumulative paper-trading performance for strategy analysis.

    paper_trading_log.csv is the source of truth. At every startup the
    tracker REPLAYS the log to rebuild the closed-trade list (so metrics
    survive restarts), rewrites paper_performance.csv, and then appends one
    row per new closed trade carrying the full running statistics: win
    rate, avg win/loss %, profit factor, cumulative P&L, equity
    (initial capital + realized P&L), max drawdown, open positions.
    """

    def __init__(self, logger: logging.Logger, trade_log_csv: str, perf_csv: str, initial_capital: float) -> None:
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
            with open(self._trade_log, newline="", encoding="utf-8") as fh:
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
                        "entry_date": (r.get("session_date") or "").strip(),
                        "entry_price": _safe_float(r.get("simulated_price"), float("nan")),
                    })
                elif et == "EXIT" and side == "SELL":
                    queue = open_by_key.get(key)
                    entry = queue.pop(0) if queue else {"entry_date": "", "entry_price": float("nan")}
                    out.append({
                        "close_time": (r.get("timestamp") or "").strip(),
                        "session_date": (r.get("session_date") or "").strip(),
                        "key": key,
                        "entry_date": entry["entry_date"],
                        "entry_price": entry["entry_price"],
                        "exit_price": _safe_float(r.get("simulated_price"), float("nan")),
                        "qty": int(_safe_float(r.get("qty"), 0.0)),
                        "pnl": _safe_float(r.get("realized_pnl"), 0.0),
                        "days": int(_safe_float(r.get("days_held"), 0.0)),
                        "reason": self._reason_from_notes(r.get("notes", "")),
                    })
            return out
        self._trades = await asyncio.to_thread(parse)
        return len(self._trades)

    def _stats(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]

        def mean_pct(group: List[Dict[str, Any]]) -> float:
            vals = [
                100.0 * (t["exit_price"] / t["entry_price"] - 1.0)
                for t in group if math.isfinite(t["entry_price"]) and t["entry_price"] > 0
            ]
            return (sum(vals) / len(vals)) if vals else float("nan")

        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in losses)
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else float("nan"))
        cumulative = sum(t["pnl"] for t in trades)
        peak, max_dd, running = self._initial, 0.0, self._initial
        for t in trades:
            running += t["pnl"]
            peak = max(peak, running)
            if peak > 0:
                max_dd = max(max_dd, 1.0 - running / peak)
        return {
            "trades_closed": n, "wins": len(wins), "losses": len(losses),
            "win_rate_pct": 100.0 * len(wins) / n if n else 0.0,
            "avg_win_pct": mean_pct(wins), "avg_loss_pct": mean_pct(losses),
            "profit_factor": profit_factor,
            "cumulative_pnl": cumulative, "equity": self._initial + cumulative,
            "max_drawdown_pct": 100.0 * max_dd,
        }

    @staticmethod
    def _fmt(value: Any, spec: str = ".2f") -> str:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return ""
        return format(f, spec) if math.isfinite(f) else ""

    def _row(self, t: Dict[str, Any], prefix: int, open_positions: Any) -> Dict[str, Any]:
        s = self._stats(self._trades[:prefix])
        pnl_pct = (
            100.0 * (t["exit_price"] / t["entry_price"] - 1.0)
            if math.isfinite(t["entry_price"]) and t["entry_price"] > 0
            else float("nan")
        )
        return {
            "close_time": t["close_time"], "session_date": t["session_date"],
            "instrument_key": t["key"], "entry_date": t["entry_date"],
            "entry_price": self._fmt(t["entry_price"]), "exit_price": self._fmt(t["exit_price"]),
            "qty": t["qty"], "realized_pnl": self._fmt(t["pnl"]),
            "pnl_pct": self._fmt(pnl_pct), "days_held": t["days"],
            "exit_reason": t["reason"], "trades_closed": s["trades_closed"],
            "wins": s["wins"], "losses": s["losses"],
            "win_rate_pct": self._fmt(s["win_rate_pct"]),
            "avg_win_pct": self._fmt(s["avg_win_pct"]),
            "avg_loss_pct": self._fmt(s["avg_loss_pct"]),
            "profit_factor": self._fmt(s["profit_factor"]),
            "cumulative_pnl": self._fmt(s["cumulative_pnl"]),
            "equity": self._fmt(s["equity"]),
            "max_drawdown_pct": self._fmt(s["max_drawdown_pct"]),
            "open_positions": "" if open_positions is None else open_positions,
        }

    async def rebuild_report(self) -> None:
        """Rewrite the derived performance CSV from the replayed history."""
        async with self._lock:
            trades = list(self._trades)
        if self._perf_path.exists():
            await asyncio.to_thread(self._perf_path.unlink)
        for i, t in enumerate(trades, 1):
            await self._csv.append(self._row(t, i, None))

    async def record_close(
        self, *, close_time: str, session_date: str, key: str, entry_date: str,
        entry_price: float, exit_price: float, qty: int, pnl: float,
        days: int, reason: str, open_positions: int,
    ) -> None:
        trade = {
            "close_time": close_time, "session_date": session_date, "key": key,
            "entry_date": entry_date, "entry_price": entry_price,
            "exit_price": exit_price, "qty": qty, "pnl": pnl, "days": days,
            "reason": reason,
        }
        async with self._lock:
            self._trades.append(trade)
            idx = len(self._trades)
        await self._csv.append(self._row(trade, idx, open_positions))
        s = self._stats(self._trades)
        self._logger.info(
            "[PERF] trade #%d closed (%s) | cumulative P&L %+.2f | equity %.2f | "
            "win rate %.1f%% | profit factor %s",
            idx, "WIN" if pnl > 0 else "LOSS", s["cumulative_pnl"], s["equity"],
            s["win_rate_pct"], self._fmt(s["profit_factor"]) or "n/a",
        )

    async def stats(self) -> Dict[str, Any]:
        async with self._lock:
            return self._stats(list(self._trades))


# =============================================================================
# SECTION 12 & 13 — EXECUTION ENGINE
# =============================================================================

class ExecutionEngine:
    def __init__(self, client: UpstoxClient, state: State, ledger: Ledger, trade_csv: CSVPersister, logger: logging.Logger, paper_mode: bool, perf: PerformanceTracker) -> None:
        self._client, self._state, self._ledger, self._csv, self._logger, self._paper, self._perf = client, state, ledger, trade_csv, logger, paper_mode, perf
        self._queue: asyncio.Queue[Optional[OrderRequest]] = asyncio.Queue(maxsize=256)
        self._pending_queue: asyncio.Queue[Optional[PendingOrder]] = asyncio.Queue(maxsize=256)
        self._workers: List[asyncio.Task[None]] = []
        self._poller: Optional[asyncio.Task[None]] = None
        self._next_request_id = 0
        self._id_lock = asyncio.Lock()
        # Shared registry of in-flight live orders (fill poller + cancel sweep).
        self._active: Dict[str, PendingOrder] = {}
        self._active_lock = asyncio.Lock()

    async def start(self) -> None:
        self._workers = [asyncio.create_task(self._worker(i), name=f"execution-worker-{i}") for i in range(EXEC_WORKERS)]
        if not self._paper:
            self._poller = asyncio.create_task(self._fill_poller(), name="fill-poller")

    async def stop(self) -> None:
        await self.flush(timeout_sec=180.0)
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        if self._poller:
            await self._pending_queue.put(None)
            await asyncio.gather(self._poller, return_exceptions=True)
            self._poller = None

    async def submit(self, req: OrderRequest) -> OrderRequest:
        async with self._id_lock:
            self._next_request_id += 1
            queued = replace(req, request_id=self._next_request_id)
        await self._queue.put(queued)
        return queued

    async def flush(self, timeout_sec: float = 180.0) -> bool:
        async def drain() -> None:
            await self._queue.join()
            if not self._paper:
                await self._pending_queue.join()
        try:
            await asyncio.wait_for(drain(), timeout=timeout_sec)
            return True
        except asyncio.TimeoutError:
            self._logger.error("Execution/pending queues did not drain within %.1fs", timeout_sec)
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
                self._logger.exception("Execution worker %d failed: %s", idx, exc)
            finally:
                self._queue.task_done()

    async def _record_fill(self, req: OrderRequest, fill_price: float, filled_qty: int, suffix: str) -> None:
        today = session_date_of()
        mode = "PAPER" if self._paper else "LIVE"
        if req.event_type == "ENTRY":
            pos = Position(req.instrument_key, req.symbol, today, fill_price, filled_qty, req.crsi_score, self._paper, req.notes)
            if await self._state.open_position(pos, fill_price * filled_qty):
                event = LedgerEvent(iso_now(), today.isoformat(), req.instrument_key, "ENTRY", "BUY", filled_qty, round(fill_price, 2), round(req.crsi_score, 4), 0, 0.0, f"{req.notes} | {suffix}")
                await self._ledger.add(event)
                await self._csv.append(asdict(event))
                self._logger.info(
                    "[FILL] %s ENTRY BUY  %s qty=%d @ %.2f | crsi=%.1f | %s",
                    mode, req.instrument_key, filled_qty, fill_price, req.crsi_score, suffix,
                )
                if not self._paper:
                    await self._arm_protective_stop(pos, req)
        elif req.event_type == "EXIT":
            closed = await self._state.close_position(req.instrument_key, fill_price, today, req.notes, filled_qty)
            if closed:
                event = LedgerEvent(iso_now(), today.isoformat(), req.instrument_key, "EXIT", "SELL", filled_qty, round(fill_price, 2), round(req.crsi_score, 4), closed.days_held, round(closed.realized_pnl, 2), f"{req.notes} | {suffix}")
                await self._ledger.add(event)
                await self._csv.append(asdict(event))
                self._logger.info(
                    "[FILL] %s EXIT SELL  %s qty=%d @ %.2f | PnL %+.2f (%s) | entry %.2f on %s | %s",
                    mode, req.instrument_key, filled_qty, fill_price, closed.realized_pnl,
                    "WIN" if closed.realized_pnl > 0 else "LOSS",
                    closed.position.entry_price, closed.position.entry_date.isoformat(), suffix,
                )
                open_now = len((await self._state.snapshot())["positions"])
                await self._perf.record_close(
                    close_time=iso_now(), session_date=today.isoformat(), key=req.instrument_key,
                    entry_date=closed.position.entry_date.isoformat(),
                    entry_price=closed.position.entry_price, exit_price=fill_price,
                    qty=filled_qty, pnl=closed.realized_pnl, days=closed.days_held,
                    reason=req.notes.split(" | ")[0].strip(), open_positions=open_now,
                )
        # Crash resilience: persist state immediately after every fill.
        await self._state.save_state_file()

    async def _simulate_paper_fill(self, req: OrderRequest) -> None:
        market = req.reference_ltp or req.limit_price
        if req.order_type == "LIMIT":
            crossed = market <= req.limit_price if req.side == "BUY" else market >= req.limit_price
            if not crossed:
                if req.event_type == "ENTRY":
                    await self._state.release_entry_cash(req.instrument_key)
                self._logger.info("Paper limit not crossed for %s: market=%.2f limit=%.2f", req.instrument_key, market, req.limit_price)
                return
        bps = get_slippage_bps(req.average_daily_volume) / 10_000.0
        raw_fill = market * (1.0 + bps if req.side == "BUY" else 1.0 - bps)
        if req.order_type == "LIMIT":
            fill = min(raw_fill, req.limit_price) if req.side == "BUY" else max(raw_fill, req.limit_price)
        else:
            fill = raw_fill
        await self._record_fill(req, fill, req.quantity, f"paper {req.order_type}")

    @staticmethod
    def _extract_order_id(payload: Mapping[str, Any]) -> str:
        data = payload.get("data") or {}
        if isinstance(data, list):
            data = data[-1] if data else {}
        return str(data.get("order_id") or data.get("orderId") or "") if isinstance(data, Mapping) else ""

    async def _arm_protective_stop(self, pos: Position, source: OrderRequest) -> None:
        """Disaster backup only — the PRIMARY stop is the 15:25 intraday-low
        check in LiveOrchestrator._evaluate_exits (canonical backtest rule)."""
        trigger = round(pos.entry_price * (1.0 - STOP_LOSS_PCT), 2)
        key_suffix = pos.instrument_key[-4:].replace("|", "")
        tag = f"sl-{session_date_of():%m%d}-{source.request_id:06d}-{key_suffix}"
        response = await self._client.place_order(
            instrument_key=pos.instrument_key, side="SELL", quantity=pos.quantity,
            price=0, tag=tag, order_type="SL-M", trigger_price=trigger,
        )
        order_id = self._extract_order_id(response)
        if not order_id:
            raise UpstoxError(f"Protective SL-M for {pos.instrument_key} returned no order_id")
        await self._state.set_protective_order(pos.instrument_key, order_id)
        self._logger.info("Armed backup SL-M %s for %s at %.2f", order_id, pos.instrument_key, trigger)

    async def arm_missing_protective_stops(self) -> None:
        """Re-arm DAY protective orders before live-session scheduling begins."""
        positions = (await self._state.snapshot())["positions"]
        for pos in positions.values():
            if await self._state.get_protective_order(pos.instrument_key):
                continue
            async with self._id_lock:
                self._next_request_id += 1
                request_id = self._next_request_id
            source = OrderRequest(
                request_id, pos.instrument_key, pos.symbol, "SELL", pos.quantity,
                pos.entry_price, "PROTECTIVE", pos.crsi_at_entry,
                "SESSION_OPEN_PROTECTION", "SL-M", 0.0, pos.entry_price,
            )
            await self._arm_protective_stop(pos, source)

    async def _cancel_protective_stop(self, key: str) -> str:
        order_id = await self._state.get_protective_order(key)
        if not order_id:
            return ""
        await self._client.cancel_order(order_id)
        await self._state.pop_protective_order(key)
        return order_id

    async def _submit_live_for_polling(self, req: OrderRequest) -> None:
        tag = f"cqrsi-{req.request_id:08d}"
        cancelled_stop = ""
        try:
            if req.event_type == "EXIT":
                cancelled_stop = await self._cancel_protective_stop(req.instrument_key)
            response = await self._client.place_order(
                instrument_key=req.instrument_key, side=req.side, quantity=req.quantity,
                price=req.limit_price, tag=tag, order_type=req.order_type,
            )
        except Exception:
            if req.event_type == "ENTRY":
                await self._state.release_entry_cash(req.instrument_key)
            elif cancelled_stop:
                pos = (await self._state.snapshot())["positions"].get(req.instrument_key)
                if pos:
                    await self._arm_protective_stop(pos, req)
            raise
        order_id = self._extract_order_id(response)
        if not order_id:
            raise UpstoxError(f"Accepted order {tag} returned no order_id")
        async with self._active_lock:
            self._active[tag] = PendingOrder(req, tag, order_id, wall_clock.monotonic())
        await self._pending_queue.put(self._active[tag])

    async def _finish_pending(self, pending: PendingOrder, terminal: bool) -> None:
        req, last = pending.request, pending.last_order
        filled_qty = int(_safe_float(last.get("filled_quantity"), 0.0))
        fill_price = _safe_float(last.get("average_price"), req.reference_ltp or req.limit_price)
        if not terminal:
            try:
                await self._client.cancel_order(pending.order_id)
                self._logger.warning("Cancelled timed-out order %s (%s)", pending.order_id, pending.tag)
            except Exception as exc:
                self._logger.warning("Cancel of %s failed (may already be terminal): %s", pending.order_id, exc)
            if req.event_type == "ENTRY":
                await self._state.release_entry_cash(req.instrument_key)
        if filled_qty <= 0:
            if req.event_type == "ENTRY":
                await self._state.release_entry_cash(req.instrument_key)
            elif req.event_type == "EXIT":
                pos = (await self._state.snapshot())["positions"].get(req.instrument_key)
                if pos:
                    await self._arm_protective_stop(pos, req)
            return
        await self._record_fill(req, fill_price, filled_qty, f"LIVE tag={pending.tag}")
        if req.event_type == "EXIT":
            remaining = (await self._state.snapshot())["positions"].get(req.instrument_key)
            if remaining:
                await self._arm_protective_stop(remaining, req)

    async def _active_snapshot(self) -> Dict[str, PendingOrder]:
        async with self._active_lock:
            return dict(self._active)

    async def cancel_unfilled_orders(self) -> int:
        """15:29 sweep — the spec allows NO order to be carried overnight.
        Anything still non-terminal at this point is cancelled and finalised
        (partial fills are recorded; entry cash released; exit stops re-armed)."""
        if self._paper:
            return 0
        finished = 0
        for tag, pending in (await self._active_snapshot()).items():
            try:
                history = await self._client.get_order_history(tag)
                if history:
                    pending.last_order = history[-1]
                    pending.order_id = str(pending.last_order.get("order_id") or pending.order_id)
            except Exception as exc:
                self._logger.warning("Status fetch failed for %s during cancel sweep: %s", tag, exc)
            status = str(pending.last_order.get("status") or "").lower()
            if status in TERMINAL_ORDER_STATUSES:
                continue  # let the fill poller finalise it within its next tick
            async with self._active_lock:
                owned = self._active.pop(tag, None)
            if owned is not pending:
                continue  # the fill poller took ownership
            try:
                await self._finish_pending(pending, terminal=False)
                finished += 1
            except Exception as exc:
                self._logger.exception("Cancel sweep finalisation failed for %s: %s", tag, exc)
            finally:
                self._pending_queue.task_done()
        if finished:
            self._logger.info("15:29 cancel sweep: %d unfilled order(s) cancelled — nothing held overnight", finished)
        return finished

    async def _fill_poller(self) -> None:
        while True:
            try:
                item = await asyncio.wait_for(self._pending_queue.get(), timeout=0.25)
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
                    history = await self._client.get_order_history(tag)
                    if history:
                        pending.last_order = history[-1]
                        pending.order_id = str(pending.last_order.get("order_id") or pending.order_id)
                    status = str(pending.last_order.get("status") or "").lower()
                    terminal = status in TERMINAL_ORDER_STATUSES
                    timed_out = now - pending.submitted_at >= 60.0
                    if not (terminal or timed_out):
                        continue
                    async with self._active_lock:
                        owned = self._active.pop(tag, None)
                    if owned is not pending:
                        continue  # the 15:29 cancel sweep took ownership
                    try:
                        await self._finish_pending(pending, terminal)
                    finally:
                        # Race fix: task_done MUST run even if finalisation
                        # raises, or flush()/join() would stall up to its
                        # 180s timeout on shutdown.
                        self._pending_queue.task_done()
                except Exception as exc:
                    self._logger.exception("Fill poll failed for %s: %s", tag, exc)

# =============================================================================
# SECTION 14 — LIVE ORCHESTRATOR (daily 15:20 batch cadence)
# =============================================================================

class LiveOrchestrator:
    def __init__(self, clock: TradeClock, logger: logging.Logger, stop_event: Optional[asyncio.Event] = None) -> None:
        self._clock, self._logger = clock, logger
        self._stop = stop_event or asyncio.Event()
        self._client = UpstoxClient(ACCESS_TOKEN, API_BASE_URL, logger)
        self._paper = PAPER_TRADING_MODE
        self._ledger = Ledger()
        self._state = State(logger, TOTAL_CAPITAL, paper=self._paper)
        self._trade_csv = CSVPersister(PAPER_TRADE_LOG_CSV, CSV_TRADE_COLUMNS, logger)
        self._summary_csv = CSVPersister(PAPER_TRADE_SUMMARY_CSV, CSV_SUMMARY_COLUMNS, logger)
        self._perf = PerformanceTracker(logger, PAPER_TRADE_LOG_CSV, PAPER_PERFORMANCE_CSV, TOTAL_CAPITAL)
        self._engine = ExecutionEngine(self._client, self._state, self._ledger, self._trade_csv, logger, paper_mode=self._paper, perf=self._perf)
        self._histories: Dict[str, pd.DataFrame] = {}
        self._adv_by_key: Dict[str, float] = {}
        self._pending_entries: List[EntrySignal] = []
        self._pending_exits: Dict[str, ExitDecision] = {}

    async def verify_ntp_drift(self) -> None:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("http://worldtimeapi.org/api/timezone/Asia/Kolkata", timeout=5.0) as r:
                    data = await r.json()
                    network_time = float(data['unixtime'])
                    drift = abs(wall_clock.time() - network_time)
                    if drift > 5.0: raise RuntimeError(f"Local clock drift {drift:.1f}s exceeds max 5s.")
                    self._logger.info("NTP drift check passed (drift: %.2fs)", drift)
        except RuntimeError: raise
        except Exception as e: self._logger.warning("Could not verify NTP drift: %s", e)

    async def setup(self) -> None:
        # Edge case: expired/invalid token — abort BEFORE any trading logic,
        # decoded offline from the JWT so no API call is wasted.
        exp = token_expiry(ACCESS_TOKEN)
        if exp is not None:
            now_ts = wall_clock.time()
            if now_ts >= exp:
                self._logger.error(
                    "ACCESS_TOKEN EXPIRED at %s IST. Generate a fresh token "
                    "(Upstox developer console, single-session tokens) and "
                    "update ACCESS_TOKEN in the script, then start again.",
                    datetime.fromtimestamp(exp, IST_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                )
                raise ConfigurationError("access token expired")
            if exp - now_ts < 7200:
                self._logger.warning("ACCESS_TOKEN expires in %.1f hours (%s IST)", (exp - now_ts) / 3600.0,
                                     datetime.fromtimestamp(exp, IST_TZ).strftime("%H:%M:%S"))
        await self._client.start(); await self._engine.start()
        # Validate the token BEFORE the trading day. Single-session Upstox
        # tokens are revoked at EOD even when the JWT exp is still in the
        # future, so the offline exp check above is not sufficient by itself.
        try:
            await self._client.validate_credentials()
            self._logger.info("[SETUP] Upstox token validated OK")
        except ConfigurationError as exc:
            self._logger.error(
                "Upstox token INVALID: %s — generate a fresh token (single-session "
                "tokens expire at EOD; refresh daily before 15:20) and restart.", exc)
            raise
        saved = await State.load_state_file(self._logger)
        if saved and saved.get("paper") == self._paper:
            self._state.positions = saved["positions"]
            self._state.session_date = saved["session_date"]
            self._state.last_completed_session = saved.get("last_completed_session")
            self._state.cash_available = saved["cash_available"]
            self._state._cash_reservations = saved["cash_reservations"]
            self._state._protective_orders = saved["protective_orders"]
            self._state.high_water_mark = saved["high_water_mark"]
            self._state.drawdown_latched = saved["drawdown_latched"]
        # Rebuild the cumulative performance ledger from the historical log.
        replayed = await self._perf.load_from_log()
        await self._perf.rebuild_report()
        if replayed:
            s = await self._perf.stats()
            self._logger.info(
                "[PERF] replayed %d closed trades from %s | win rate %.1f%% | cumulative P&L %+.2f | equity %.2f",
                replayed, PAPER_TRADE_LOG_CSV, s["win_rate_pct"], s["cumulative_pnl"], s["equity"],
            )
        if not self._paper:
            await self.verify_ntp_drift()
            self._state.cash_available = await self._client.get_real_cash_balance()
            # Crash recovery: no order may ever be carried overnight — cancel
            # stale entry/exit orders left behind by a crashed run.
            try:
                open_orders = await self._client.get_open_orders()
                stale = [o for o in open_orders if str(o.get("tag") or "").startswith("cqrsi-")]
                for o in stale:
                    await self._client.cancel_order(str(o.get("order_id")))
                    self._logger.warning("[CLEANUP] cancelled stale order %s (tag=%s) left by a previous run",
                                         o.get("order_id"), o.get("tag"))
            except Exception as exc:
                self._logger.warning("Open-order cleanup failed: %s", exc)
            if self._state.session_date != session_date_of():
                # Upstox protective SL-M orders use DAY validity and must be renewed.
                self._state._protective_orders.clear()
            await self.reconcile_portfolio()
            await self._engine.arm_missing_protective_stops()

    async def shutdown(self) -> None:
        await self._engine.stop()
        await self._state.save_state_file()
        await self._client.close()

    # ------------------------------------------------------------------
    # Resident-loop helpers: the script can be started at ANY time of day;
    # it visibly waits for the next 15:20 gate, runs the session, prints
    # the waiting message again, and repeats.
    # ------------------------------------------------------------------

    async def _sleep_interruptible(self, secs: float) -> bool:
        """Sleep up to `secs` in small slices. Returns True if a stop
        signal (Ctrl+C / SIGTERM) arrived during the wait."""
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
            "No NSE trading day found within 30 days — the holiday/special-session "
            "calendar in the script is stale; update NSE_HOLIDAYS_2026."
        )

    async def _wait_until(self, target: datetime, reason: str) -> bool:
        """Print a visible countdown until `target`. Returns True if stopped."""
        mode = "PAPER" if self._paper else "LIVE"
        while not self._stop.is_set():
            secs = (target - ist_now()).total_seconds()
            if secs <= 0:
                return False
            self._logger.info(
                "[WAIT] %s | next scheduled action: %s IST (in %dh %02dm) | mode=%s",
                reason, target.strftime("%Y-%m-%d %H:%M:%S"),
                int(secs // 3600), int((secs % 3600) // 60), mode,
            )
            if await self._sleep_interruptible(min(300.0, secs)):
                return True
        return True

    @staticmethod
    def _fresh_ltp(quotes: Mapping[str, QuoteData], day_start: float) -> Dict[str, float]:
        """Instrument -> 15:25 last price, excluding stale (pre-session) quotes."""
        out: Dict[str, float] = {}
        for k, q in quotes.items():
            if math.isfinite(q.last_price) and not (q.ts > 0 and q.ts < day_start):
                out[k] = q.last_price
        return out

    async def run_forever(self, once: bool = False) -> None:
        self._logger.info("=" * 78)
        self._logger.info("CONNORSRSI (3,2,100) EOD SWING ENGINE — %s MODE (default; "
                          "set PAPER_TRADING_MODE=False for live)", "PAPER" if self._paper else "LIVE")
        self._logger.info("Entry: CRSI(3,2,100)<=10 AND close>SMA200 at the 15:25 close proxy")
        self._logger.info("Exits: day-low 8% hard stop -> RSI-2>70 target -> 5-trading-day time stop")
        self._logger.info("Gates (IST): fetch 15:20 | decide 15:25 | order 15:28 | cancel 15:29 | summary 15:30")
        self._logger.info("=" * 78)
        while not self._stop.is_set():
            now = ist_now()
            d = now.date()
            bounds = self._clock.session_bounds_for(d)
            wake = self._clock.gate_datetime(d, DATA_FETCH_TIME)
            force_rerun = os.environ.get("CONNORS_FORCE_RERUN") == "1"
            completed_today = (await self._state.session_completed_on()) == d

            if bounds is None:
                nxt = self._next_trading_day(d)
                if await self._wait_until(self._clock.gate_datetime(nxt, DATA_FETCH_TIME),
                                          f"{d.isoformat()} is not an NSE session (weekend/holiday)"):
                    break
                continue
            if bounds[1].time() <= DATA_FETCH_TIME:
                nxt = self._next_trading_day(d)
                if await self._wait_until(self._clock.gate_datetime(nxt, DATA_FETCH_TIME),
                                          f"{d.isoformat()} is a special session closing {bounds[1].time()} — before the 15:20 gate"):
                    break
                continue
            if completed_today and not force_rerun:
                nxt = self._next_trading_day(d)
                if await self._wait_until(self._clock.gate_datetime(nxt, DATA_FETCH_TIME),
                                          f"session {d.isoformat()} was already completed by this engine (CONNORS_FORCE_RERUN=1 to override)"):
                    break
                continue
            if now >= bounds[1]:
                nxt = self._next_trading_day(d)
                if await self._wait_until(self._clock.gate_datetime(nxt, DATA_FETCH_TIME),
                                          f"today's session already closed at {bounds[1].time()} IST"):
                    break
                continue
            if now < wake:
                if await self._wait_until(wake, "waiting for today's 15:20 data-fetch gate"):
                    break
                continue
            if now.time() >= ORDER_TIME:
                # Too late to trade safely: orders placed now would be cancelled
                # by the 15:29 sweep within seconds. Open positions carry over;
                # exits are re-evaluated tomorrow (backup SL-M still armed live).
                self._logger.warning(
                    "[SKIP] started at %s — after the 15:28 order gate; no trades this session. "
                    "Open positions remain; exits re-evaluate tomorrow.", now.strftime("%H:%M:%S"))
                await self._state.mark_session_completed(d)
                await self._state.save_state_file()
                continue
            # now is within [15:20, 15:28): join the in-progress session.
            if now.time() > DATA_FETCH_TIME:
                self._logger.warning("[JOIN] started mid-session at %s — using the current price as the close proxy",
                                     now.strftime("%H:%M:%S"))
            self._logger.info("[RUN] executing session gates for %s", d.isoformat())
            await self.run_schedule(ist_now(), bounds)
            if not self._stop.is_set():
                await self._state.mark_session_completed(d)
                await self._state.save_state_file()
                self._logger.info("[DONE] session %s complete — open positions carry to tomorrow; "
                                  "backup SL-M stops remain armed (live mode)", d.isoformat())
            if once:
                break
        if not once and not self._stop.is_set():
            self._logger.info("[WAIT] entering idle monitoring for the next trading session...")

    async def reconcile_portfolio(self) -> None:
        broker_positions = await self._client.get_delivery_positions()
        today, saved, adopted, invested = session_date_of(), self._state.positions, {}, 0.0
        for raw in broker_positions:
            qty, key = int(_safe_float(raw.get("quantity"), 0.0)), str(raw.get("instrument_token") or "")
            if qty <= 0 or key not in UNIVERSE_TICKERS: continue
            avg = _safe_float(raw.get("average_price"), _safe_float(raw.get("last_price"), 0.0))
            old = saved.get(key)
            adopted[key] = Position(key, raw.get("tradingsymbol") or key, old.entry_date if old else today, old.entry_price if old else avg, qty, old.crsi_at_entry if old else 0.0, False, "broker_adopted" if not old else "broker_reconciled")
            invested += avg * qty
        self._state.positions = adopted

    async def _fetch_histories(self, as_of: date) -> bool:
        """15:20 — pull daily bars for the universe (450-calendar-day window)."""
        fetched = await self._client.fetch_daily_histories_bulk(
            UNIVERSE_TICKERS, to_date=as_of, lookback_days=DATA_LOOKBACK_DAYS
        )
        ok = {k: df for k, df in fetched.items() if df is not None and not df.empty}
        self._histories = ok
        self._adv_by_key = dict(zip(
            ok.keys(),
            await asyncio.gather(*(asyncio.to_thread(average_daily_volume, df) for df in ok.values())),
        ))
        return bool(ok)

    async def _evaluate_exits(
        self, as_of: date, positions: Mapping[str, Position], quotes: Mapping[str, QuoteData], day_start: float
    ) -> None:
        """15:25 — canonical EOD exit check, exact book precedence:
          1) HARD STOP : the day's LOW <= entry*0.92  -> MARKET sell at close
          2) TARGET    : RSI-2 of close crosses above 70 -> sell at close
          3) TIME      : 5 TRADING days held -> sell at close
        A position opened THIS session is not stop-monitored until the next
        session (the backtest starts stop monitoring the day after entry).
        A stale (pre-session) quote is treated as MISSING: the exit is
        deferred to the next session (the backup SL-M stays armed, live)."""
        self._pending_exits = {}
        if not positions:
            return
        for key, pos in positions.items():
            q = quotes.get(key)
            stale = q is not None and q.ts > 0 and q.ts < day_start
            if q is None or not math.isfinite(q.last_price) or stale:
                self._logger.warning(
                    "No FRESH 15:25 quote for held %s (%s); exit evaluation deferred to next session — "
                    "backup SL-M stays armed", key, "stale quote (suspended?)" if stale else "missing quote",
                )
                continue
            ltp = q.last_price  # close proxy
            hist = self._histories.get(key)
            stop_price = pos.entry_price * (1.0 - STOP_LOSS_PCT)
            days_traded = await asyncio.to_thread(trading_days_since_entry, hist, pos.entry_date, as_of)
            rsi2 = await asyncio.to_thread(intraday_rsi2, hist, ltp, as_of) if hist is not None else float("nan")
            low = q.low_price
            stop_breached = False
            if pos.entry_date != as_of:
                stop_breached = (low <= stop_price) if math.isfinite(low) else (ltp <= stop_price)
            if stop_breached:
                reason, order_type = "HARD_STOP_LOSS", "MARKET"
            elif math.isfinite(rsi2) and rsi2 > RSI2_EXIT_THRESHOLD:
                # Checked once daily, so "RSI-2 > 70 today" == "RSI-2 crossed
                # above 70" (a cross would already have triggered an exit).
                reason, order_type = "RSI2_TARGET", "LIMIT"
            elif days_traded is not None and days_traded >= MAX_HOLD_TRADING_DAYS:
                reason, order_type = "TIME_EXIT", "LIMIT"
            else:
                continue
            self._pending_exits[key] = ExitDecision(
                key, pos.symbol, ltp, reason, days_traded or 0, order_type, rsi2=rsi2
            )
            self._logger.info(
                "EXIT %s (%s): ltp=%.2f stop=%.2f rsi2=%s trading_days=%s",
                key, reason, ltp, stop_price,
                f"{rsi2:.1f}" if math.isfinite(rsi2) else "n/a", days_traded,
            )

    async def _liquidate_portfolio(
        self, positions: Mapping[str, Position], ltps: Mapping[str, float]
    ) -> None:
        released = 0.0
        for pending_entry in self._pending_entries:
            released += await self._state.release_entry_cash(pending_entry.instrument_key)
        self._pending_entries = []
        # Suppress every previously selected exit before queuing
        # portfolio-wide market liquidation; no symbol can receive two sells.
        self._pending_exits.clear()
        self._logger.critical(
            "Drawdown breaker breached: liquidating %d positions; released %.2f reserved cash",
            len(positions), released,
        )
        for key, pos in positions.items():
            reference_price = ltps.get(key)
            if not reference_price:
                self._logger.error("Cannot liquidate %s: no live price", key)
                continue
            await self._engine.submit(OrderRequest(
                0, key, pos.symbol, "SELL", pos.quantity, reference_price,
                "EXIT", pos.crsi_at_entry, "MAX_DRAWDOWN_LIQUIDATION", "MARKET",
                self._adv_by_key.get(key, 0.0), reference_price,
            ))

    async def _generate_signals(
        self, as_of: date, positions: Mapping[str, Position], quotes: Mapping[str, QuoteData], day_start: float
    ) -> None:
        """15:25 — canonical EOD entry check on the 15:25 close proxy:
          CRSI(3,2,100) <= 10 AND close proxy > SMA200 (through the proxy)
          AND ADV20 >= 500k. Ranked most-oversold-first; slots = 20 - held.
        Edge cases: stale quotes skip the symbol; a >25% move vs the last
        close flags a possible split (Upstox candles are not split-adjusted)
        and skips the symbol for entry that day."""
        for stale_signal in self._pending_entries:
            await self._state.release_entry_cash(stale_signal.instrument_key)
        self._pending_entries = []
        snap = await self._state.snapshot()
        held = set(positions)
        candidates = [
            key for key in UNIVERSE_TICKERS
            if key in self._histories and key not in held
        ]
        fresh = self._fresh_ltp(quotes, day_start)
        current_equity = (
            snap["cash_available"] + snap["reserved_cash"]
            + sum(fresh.get(key, pos.entry_price) * pos.quantity for key, pos in positions.items())
        )
        if await self._state.update_drawdown(current_equity):
            await self._liquidate_portfolio(positions, fresh)
            return
        if not candidates:
            return

        async def evaluate(key: str) -> Optional[Tuple[str, float, float, float, float]]:
            ltp = fresh.get(key)
            if not ltp or not math.isfinite(ltp):
                return None
            metrics = await asyncio.to_thread(
                candidate_metrics, self._histories[key], ltp, as_of
            )
            if metrics is None:
                return None
            sma200_proxy, crsi, adv = metrics
            if not (
                math.isfinite(crsi) and math.isfinite(sma200_proxy)
                and crsi <= CRSI_THRESHOLD and ltp > sma200_proxy
                and adv >= MIN_AVG_DAILY_VOLUME
            ):
                return None
            # Corporate-action guard: Upstox daily candles are NOT
            # split-adjusted, so a split would corrupt the stop and RSIs.
            last_close = float(self._histories[key]["close"].iloc[-1])
            if math.isfinite(last_close) and last_close > 0:
                move = abs(ltp / last_close - 1.0)
                if move > SPLIT_GUARD_MOVE_PCT:
                    self._logger.warning(
                        "[SIGNAL] %s: %.0f%% move vs last close %.2f — possible SPLIT/corporate "
                        "action (history not split-adjusted); skipping entry today",
                        key, 100.0 * (ltp / last_close - 1.0), last_close,
                    )
                    return None
            return key, ltp, sma200_proxy, crsi, adv

        evaluated = await asyncio.gather(*(evaluate(key) for key in candidates))
        valid = sorted((item for item in evaluated if item is not None), key=lambda x: x[3])
        slots = max(MAX_CONCURRENT_POSITIONS - len(held), 0)
        target_allocation = POSITION_SIZE_PCT * current_equity
        running_cash = snap["cash_available"]

        # Cash is decremented and atomically reserved as each ranked signal is queued.
        for key, ltp, sma200, crsi, adv in valid[:slots]:
            allocation = min(target_allocation, running_cash)
            buy_limit = entry_limit_price(ltp)
            qty = (int(allocation / ltp) // LOT_SIZE) * LOT_SIZE
            if qty < LOT_SIZE:
                continue
            reserved = buy_limit * qty
            if not await self._state.reserve_entry_cash(key, reserved):
                continue
            running_cash -= reserved
            self._logger.info(
                "[SIGNAL] ENTRY %s | crsi=%.1f (<= %.0f) | close=%.2f > sma200=%.2f | adv=%.0f | qty=%d @ limit %.2f",
                key, crsi, CRSI_THRESHOLD, ltp, sma200, adv, qty, buy_limit,
            )
            self._pending_entries.append(EntrySignal(
                key, key, ltp, crsi, sma200, ltp,
                qty, adv, reserved,
            ))
        if not self._pending_entries:
            self._logger.info("[SIGNAL] no entry signals meet all criteria today")

    async def _dispatch_orders(self, as_of: date) -> None:
        """15:28 — place all entry/exit orders for the session."""
        todo: List[OrderRequest] = []
        for sig in self._pending_entries:
            buy_px = round_to_tick(sig.ltp * (1.0 + ENTRY_LIMIT_OFFSET_PCT), "up")
            todo.append(OrderRequest(
                0, sig.instrument_key, sig.symbol, "BUY", sig.quantity, buy_px,
                "ENTRY", sig.crsi, "CRSI_ENTRY", "LIMIT",
                sig.average_daily_volume, sig.ltp,
            ))
        positions = (await self._state.snapshot())["positions"]
        for key, dec in self._pending_exits.items():
            pos = positions.get(key)
            if not pos:
                continue
            # Hard stop is a MARKET sell (backtest: exit at the close once the
            # day's low breaches the stop). RSI-2/time exits use an aggressive
            # limit 1% inside the market so they cross by the close.
            # Both are snapped to the 0.05 NSE tick (Upstox rejects off-tick prices).
            reference_price = dec.ltp if dec.order_type == "MARKET" else round_to_tick(dec.ltp * 0.99, "down")
            rsi2_txt = f"{dec.rsi2:.1f}" if math.isfinite(dec.rsi2) else "n/a"
            self._logger.info(
                "[ORDER] EXIT %s | %s %s qty=%d @ %s %.2f | rsi2=%s | days=%d",
                key, dec.order_type, dec.reason, pos.quantity,
                "limit" if dec.order_type == "LIMIT" else "market", reference_price, rsi2_txt, dec.days_held,
            )
            todo.append(OrderRequest(
                0, key, dec.symbol, "SELL", pos.quantity, reference_price,
                "EXIT", pos.crsi_at_entry, f"{dec.reason} | rsi2={rsi2_txt}", dec.order_type,
                self._adv_by_key.get(key, 0.0), dec.ltp,
            ))

        for request in todo:
            if request.event_type == "ENTRY":
                self._logger.info(
                    "[ORDER] ENTRY %s | LIMIT BUY qty=%d @ %.2f",
                    request.instrument_key, request.quantity, request.limit_price,
                )
            await self._engine.submit(request)
        await self._engine.flush(timeout_sec=90.0)
        self._pending_entries, self._pending_exits = [], {}
        self._logger.info("[ORDER] dispatched %d order(s) (%d entry, %d exit)",
                          len(todo), sum(1 for r in todo if r.event_type == "ENTRY"),
                          sum(1 for r in todo if r.event_type == "EXIT"))

    async def _cancel_unfilled(self, as_of: date) -> None:
        """15:29 — cancel anything not filled; no order is held overnight."""
        cancelled = await self._engine.cancel_unfilled_orders()
        self._logger.info(
            "15:29 sweep complete: %d order(s) cancelled, session orders fully settled", cancelled
        )

    async def _write_summary(self, as_of: date) -> None:
        snap = await self._state.snapshot()
        ltps: Dict[str, float] = {}
        if snap["positions"]:
            ltps = await self._client.get_ltp_batch(list(snap["positions"].keys()))
        unrealized = sum(
            (ltps.get(k, p.entry_price) - p.entry_price) * p.quantity
            for k, p in snap["positions"].items()
        )
        await self._summary_csv.append({
            "session_date": as_of.isoformat(),
            "open_positions_count": len(snap["positions"]),
            "total_unrealized_pnl": round(unrealized, 2),
            "realized_pnl_today": round(snap["realized_pnl_today"], 2),
        })
        await self._state.save_state_file()
        s = await self._perf.stats()
        pf = s["profit_factor"]
        self._logger.info(
            "[SUMMARY] %s | open positions=%d | unrealized %+.2f | realized today %+.2f",
            as_of.isoformat(), len(snap["positions"]), unrealized, snap["realized_pnl_today"],
        )
        self._logger.info(
            "[SUMMARY] cumulative performance -> trades=%d win_rate=%.2f%% avg_win=%s%% avg_loss=%s%% "
            "profit_factor=%s cumulative_pnl=%+.2f equity=%.2f max_dd=%s%% (see %s)",
            s["trades_closed"], s["win_rate_pct"],
            f"{s['avg_win_pct']:.2f}" if math.isfinite(s["avg_win_pct"]) else "n/a",
            f"{s['avg_loss_pct']:.2f}" if math.isfinite(s["avg_loss_pct"]) else "n/a",
            f"{pf:.2f}" if math.isfinite(pf) else ("inf" if pf > 0 else "n/a"),
            s["cumulative_pnl"], s["equity"],
            f"{s['max_drawdown_pct']:.2f}", PAPER_PERFORMANCE_CSV,
        )

    async def _signal_and_exits(self, d: date) -> None:
        """15:25 — one shared quote fetch (last price = close proxy + day low),
        then exit decisions, then entry signals.

        Staleness guard (edge case: an UNLISTED holiday or a quote-feed outage
        would otherwise serve yesterday's data and fabricate signals): if every
        quote carries a timestamp from before today's session, the whole day is
        aborted with no trades. Stale quotes for individual suspended symbols
        are skipped for that symbol only."""
        snap = await self._state.snapshot()
        positions = snap["positions"]
        held = set(positions)
        candidates = [k for k in UNIVERSE_TICKERS if k in self._histories and k not in held]
        quote_keys = list(dict.fromkeys([*held, *candidates]))
        if not quote_keys:
            self._logger.warning("[SIGNAL] no histories available (15:20 fetch failed) — no signals today")
            return
        quotes = await self._client.get_quote_batch(quote_keys)
        day_start = self._clock.gate_datetime(d, time(9, 0)).timestamp()
        known_ts = [q.ts for q in quotes.values() if q.ts > 0]
        fresh_ts = [t for t in known_ts if t >= day_start]
        if known_ts and not fresh_ts:
            newest = max(known_ts)
            self._logger.critical(
                "[ABORT] all %d quotes are STALE (newest timestamp %s IST, before today's session) — "
                "market is likely closed on an unlisted holiday or the feed is down. NO trades placed today.",
                len(known_ts), datetime.fromtimestamp(newest, IST_TZ).strftime("%Y-%m-%d %H:%M"),
            )
            return
        if known_ts and len(fresh_ts) < len(known_ts):
            self._logger.warning(
                "[SIGNAL] %d/%d quotes stale (suspended instruments) — those symbols are skipped",
                len(known_ts) - len(fresh_ts), len(known_ts),
            )
        if not known_ts:
            self._logger.warning("[SIGNAL] quote feed returned no timestamps (LTP-only fallback) — "
                                 "freshness cannot be verified, proceeding with caution")
        await self._evaluate_exits(d, positions, quotes, day_start)
        await self._generate_signals(d, positions, quotes, day_start)

    async def run_schedule(self, now: datetime, bounds: Tuple[datetime, datetime]) -> None:
        today = now.date()
        await self._state.roll_session(today)
        # Canonical batch cadence: fetch 15:20, decide 15:25, order 15:28,
        # cancel unfilled 15:29, summary 15:30.
        gates = (
            (DATA_FETCH_TIME, "fetch", self._fetch_histories),
            (SIGNAL_TIME, "signals", self._signal_and_exits),
            (ORDER_TIME, "dispatch", self._dispatch_orders),
            (CANCEL_TIME, "cancel", self._cancel_unfilled),
            (SUMMARY_TIME, "summary", self._write_summary),
        )
        for i, (t, name, handler) in enumerate(gates, 1):
            if self._stop.is_set():
                return
            wake = self._clock.gate_datetime(today, t)
            if wake <= now and t in (ORDER_TIME, CANCEL_TIME) and not self._clock.is_inside_session(now):
                continue
            if wake > now:
                self._logger.info("[WAIT] gate %d/5 — %s at %s IST (in %dm)", i, name.upper(), t,
                                  int(self._clock.seconds_until(wake) // 60))
                if await self._sleep_interruptible(self._clock.seconds_until(wake)):
                    return
            self._logger.info("[GATE %d/5] %s IST — %s", i, t, name.upper())
            await handler(today)

# =============================================================================
# SECTION 15 — BACKTEST ENGINE (exact EOD simulation of the same rules)
# =============================================================================

def get_point_in_time_universe(as_of: date) -> List[str]:
    """Backtest Point-in-Time mapping.
    Currently stubs to the static universe; needs a historical NSE index
    constituent DB for full survivorship-bias-free resolution."""
    return UNIVERSE_TICKERS


def _synthetic_frame(seed: int, sessions: int, end: date) -> pd.DataFrame:
    """Neutral random-walk OHLCV for OFFLINE pipeline validation only
    (BACKTEST_USE_SYNTHETIC_DATA / --synthetic). No live edge is implied."""
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, sessions)))
    opens = closes * (1.0 + rng.normal(0.0, 0.004, sessions))
    highs = np.maximum(opens, closes) * (1.0 + np.abs(rng.normal(0.0, 0.006, sessions)))
    lows = np.minimum(opens, closes) * (1.0 - np.abs(rng.normal(0.0, 0.006, sessions)))
    vols = rng.integers(500_000, 5_000_000, sessions).astype(np.float64)
    days = pd.bdate_range(end=end if end.weekday() < 5 else end - timedelta(days=2), periods=sessions)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=pd.to_datetime(days),
    )


class Backtester:
    """Event-driven EOD simulation over daily bars, rule-for-rule identical to
    the live orchestrator:
      entry at the close : CRSI <= 10 AND close > SMA200 AND ADV20 >= 500k
      exit at the close  : (1) day low <= entry*0.92, then (2) RSI-2 > 70,
                           then (3) 5 trading days held
    Fills carry ADV-scaled slippage. Positions are never held across an
    unprocessed session; the window end force-closes stragglers at the close."""

    def __init__(self, client: UpstoxClient, logger: logging.Logger, synthetic: bool = False) -> None:
        self._client, self._logger, self._synthetic = client, logger, synthetic

    async def _fetch_history(self, key: str, as_of: date, from_date: date) -> Optional[pd.DataFrame]:
        span = (as_of - from_date).days
        if span <= 400:
            df = await self._client.get_daily_history(key, as_of, from_date)
            return df if df is not None and not df.empty else None
        df = None
        try:
            df = await self._client.get_daily_history(key, as_of, from_date)
        except Exception as exc:
            self._logger.warning("Full-range fetch failed for %s: %s (falling back to chunks)", key, exc)
        if df is not None and not df.empty:
            oldest = df.index.min().date()
            if oldest <= from_date + timedelta(days=10):
                return df
            self._logger.info("History for %s appears truncated (oldest %s); refetching in chunks", key, oldest)
        frames: List[pd.DataFrame] = []
        start = from_date
        while start < as_of:
            end = min(start + timedelta(days=400), as_of)
            try:
                part = await self._client.get_daily_history(key, end, start)
                if part is not None and not part.empty:
                    frames.append(part)
            except Exception as exc:
                self._logger.warning("Chunk %s->%s failed for %s: %s", start, end, key, exc)
            start = end + timedelta(days=1)
        if not frames:
            return None
        combined = pd.concat(frames).sort_index()
        return combined[~combined.index.duplicated(keep="last")]

    async def run(self) -> Dict[str, Any]:
        self._logger.info("BACKTEST MODE INITIATED (canonical ConnorsRSI 3,2,100 EOD rules)")
        as_of = session_date_of()
        universe = get_point_in_time_universe(as_of)

        if self._synthetic:
            self._logger.warning("SYNTHETIC DATA MODE — pipeline validation only, no real edge")
            sessions = MIN_HISTORY_DAYS + NUM_BACKTEST_DAYS + 40
            data = {
                key: _synthetic_frame(sum(ord(c) for c in key) % (2 ** 32), sessions, as_of)
                for key in universe
            }
        else:
            span_days = int((NUM_BACKTEST_DAYS + MIN_HISTORY_DAYS) * 7.0 / 5.0) + 60
            from_date = as_of - timedelta(days=span_days)
            self._logger.info(
                "Fetching daily history %s -> %s for %d instruments (pacing: allow several minutes)",
                from_date, as_of, len(universe),
            )
            sem = asyncio.Semaphore(FETCH_CONCURRENCY)
            async def one(key: str) -> Tuple[str, Optional[pd.DataFrame]]:
                async with sem:
                    try:
                        return key, await self._fetch_history(key, as_of, from_date)
                    except Exception as exc:
                        self._logger.warning("History failed for %s: %s", key, exc)
                        return key, None
            results = await asyncio.gather(*(one(k) for k in universe))
            data = {
                k: df for k, df in results
                if df is not None and len(df) >= MIN_HISTORY_DAYS
            }
            self._logger.info("Usable histories: %d / %d", len(data), len(universe))
            if len(data) < 5:
                return {"status": "backtest aborted", "reason": "insufficient histories"}

        all_dates = sorted(set().union(*[set(df.index.map(lambda t: t.date())) for df in data.values()]))
        if len(all_dates) < MIN_HISTORY_DAYS + 10:
            return {"status": "backtest aborted", "reason": f"only {len(all_dates)} common sessions available"}
        sim_dates = all_dates[-NUM_BACKTEST_DAYS:]

        # Per-instrument indicator tables (vectorised) + date -> row map.
        tables: Dict[str, Tuple[pd.DataFrame, Dict[date, int]]] = {}
        for key, df in data.items():
            ind = await asyncio.to_thread(compute_crsi, df)
            posmap = {ts.date(): j for j, ts in enumerate(ind.index)}
            tables[key] = (ind, posmap)
        self._logger.info("Simulation window: %s -> %s (%d sessions)", sim_dates[0], sim_dates[-1], len(sim_dates))

        cash = TOTAL_CAPITAL
        positions: Dict[str, Dict[str, Any]] = {}
        trades: List[Dict[str, Any]] = []
        equity_curve: List[Tuple[date, float]] = []

        def row_of(key: str, day: date) -> Optional[pd.Series]:
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
            # ---- EXIT phase (canonical precedence) ----
            for key in list(positions.keys()):
                p = positions[key]
                row = row_of(key, day)
                if row is None:
                    continue  # no bar this session (halt): EOD rules unevaluable
                low_v, close_v = float(row["low"]), float(row["close"])
                rsi2_v = float(row["rsi2"])
                if not (math.isfinite(low_v) and math.isfinite(close_v)):
                    continue
                if p["entry_date"] == day:
                    continue  # entered at this close; stop monitoring starts next session
                held = i - p["entry_i"]
                if low_v <= p["stop_price"]:
                    reason = "HARD_STOP_LOSS"
                elif math.isfinite(rsi2_v) and rsi2_v > RSI2_EXIT_THRESHOLD:
                    reason = "RSI2_TARGET"
                elif held >= MAX_HOLD_TRADING_DAYS:
                    reason = "TIME_EXIT"
                else:
                    continue
                bps = get_slippage_bps(p["adv"]) / 10_000.0
                fill = close_v * (1.0 - bps)
                pnl = (fill - p["entry_fill"]) * p["qty"]
                cash += fill * p["qty"]
                trades.append({
                    "key": key, "entry_date": p["entry_date"], "exit_date": day,
                    "entry_fill": p["entry_fill"], "exit_fill": fill,
                    "qty": p["qty"], "pnl": pnl, "days": held,
                    "reason": reason, "crsi": p["crsi"],
                })
                positions.pop(key)

            # ---- mark to market ----
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

            # ---- ENTRY phase at the close ----
            target = POSITION_SIZE_PCT * equity
            candidates: List[Tuple[float, str, float, float]] = []
            for key in universe:
                if key in positions or key not in tables:
                    continue
                row = row_of(key, day)
                if row is None:
                    continue
                crsi_v = float(row["crsi"]); close_v = float(row["close"])
                sma_v = float(row["sma200"]); adv_v = float(row["adv"])
                if not (
                    math.isfinite(crsi_v) and math.isfinite(close_v)
                    and math.isfinite(sma_v) and math.isfinite(adv_v)
                ):
                    continue
                if crsi_v <= CRSI_THRESHOLD and close_v > sma_v and adv_v >= MIN_AVG_DAILY_VOLUME:
                    candidates.append((crsi_v, key, close_v, adv_v))
            candidates.sort(key=lambda c: c[0])
            slots = max(MAX_CONCURRENT_POSITIONS - len(positions), 0)
            for crsi_v, key, close_v, adv_v in candidates[:slots]:
                if cash <= 0:
                    break
                bps = get_slippage_bps(adv_v) / 10_000.0
                fill = close_v * (1.0 + bps)
                qty = (int(target / fill) // LOT_SIZE) * LOT_SIZE
                if qty < LOT_SIZE or qty * fill > cash:
                    continue
                cash -= qty * fill
                positions[key] = {
                    "entry_date": day, "entry_i": i, "entry_fill": fill,
                    "stop_price": fill * (1.0 - STOP_LOSS_PCT),
                    "qty": qty, "adv": adv_v, "crsi": crsi_v, "last_close": close_v,
                }
            equity_curve.append((day, cash + sum(p["last_close"] * p["qty"] for p in positions.values())))

        # Force-close stragglers at the final window close.
        last_day = sim_dates[-1]
        for key, p in list(positions.items()):
            cv = close_of(key, last_day)
            if not math.isfinite(cv):
                continue
            fill = cv * (1.0 - get_slippage_bps(p["adv"]) / 10_000.0)
            pnl = (fill - p["entry_fill"]) * p["qty"]
            cash += fill * p["qty"]
            trades.append({
                "key": key, "entry_date": p["entry_date"], "exit_date": last_day,
                "entry_fill": p["entry_fill"], "exit_fill": fill,
                "qty": p["qty"], "pnl": pnl,
                "days": len(sim_dates) - 1 - p["entry_i"],
                "reason": "END_OF_WINDOW", "crsi": p["crsi"],
            })
            positions.pop(key)
        final_equity = cash

        # ---- metrics (the numbers to compare against the book) ----
        n = len(trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        win_rate = 100.0 * len(wins) / n if n else 0.0
        avg_win_pct = 100.0 * sum(t["exit_fill"] / t["entry_fill"] - 1.0 for t in wins) / len(wins) if wins else 0.0
        avg_loss_pct = 100.0 * sum(t["exit_fill"] / t["entry_fill"] - 1.0 for t in losses) / len(losses) if losses else 0.0
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in losses)
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else float("nan"))
        avg_holding = sum(t["days"] for t in trades) / n if n else 0.0
        total_return_pct = 100.0 * (final_equity / TOTAL_CAPITAL - 1.0)
        peak, max_dd = TOTAL_CAPITAL, 0.0
        for _, eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, 1.0 - eq / peak)
        max_dd_pct = 100.0 * max_dd

        reason_counts: Dict[str, int] = {}
        for t in trades:
            reason_counts[t["reason"]] = reason_counts.get(t["reason"], 0) + 1

        trade_csv = CSVPersister(BACKTEST_TRADE_LOG_CSV, CSV_TRADE_COLUMNS, self._logger)
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
                "realized_pnl": round(t["pnl"], 2),
                "notes": f"entry={t['entry_fill']:.2f} on {t['entry_date']} reason={t['reason']}",
            })
        summary_csv = CSVPersister(BACKTEST_SUMMARY_CSV, BACKTEST_SUMMARY_COLUMNS, self._logger)
        pf_txt = f"{profit_factor:.2f}" if math.isfinite(profit_factor) else ("inf" if profit_factor > 0 else "n/a")
        for metric, value in [
            ("data_source", "synthetic" if self._synthetic else "upstox_daily_history"),
            ("window_start", sim_dates[0].isoformat()),
            ("window_end", sim_dates[-1].isoformat()),
            ("sessions", len(sim_dates)),
            ("instruments", len(data)),
            ("trades", n),
            ("win_rate_pct", f"{win_rate:.2f}"),
            ("avg_win_pct", f"{avg_win_pct:.2f}"),
            ("avg_loss_pct", f"{avg_loss_pct:.2f}"),
            ("profit_factor", pf_txt),
            ("avg_holding_days", f"{avg_holding:.2f}"),
            ("total_return_pct", f"{total_return_pct:.2f}"),
            ("max_drawdown_pct", f"{max_dd_pct:.2f}"),
            ("final_equity", f"{final_equity:.2f}"),
            ("exit_reasons", json.dumps(reason_counts)),
        ]:
            await summary_csv.append({"metric": metric, "value": value})

        report = {
            "status": "backtest complete",
            "data_source": "synthetic" if self._synthetic else "upstox_daily_history",
            "window": [sim_dates[0].isoformat(), sim_dates[-1].isoformat()],
            "sessions": len(sim_dates),
            "instruments": len(data),
            "trades": n,
            "win_rate_pct": round(win_rate, 2),
            "avg_win_pct": round(avg_win_pct, 2),
            "avg_loss_pct": round(avg_loss_pct, 2),
            "profit_factor": round(profit_factor, 2) if math.isfinite(profit_factor) else pf_txt,
            "avg_holding_days": round(avg_holding, 2),
            "total_return_pct": round(total_return_pct, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "final_equity": round(final_equity, 2),
            "exit_reasons": reason_counts,
        }
        return report

# =============================================================================
# MAIN ENTRY
# =============================================================================

def main() -> None:
    logger = logging.getLogger("connors")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    argv = set(sys.argv[1:])
    backtest = BACKTEST_MODE or ("--backtest" in argv)
    synthetic = BACKTEST_USE_SYNTHETIC_DATA or ("--synthetic" in argv)
    once = "--once" in argv
    logger.info("START %s | args=%s", "BACKTEST" if backtest else "TRADING ENGINE",
                sorted(sys.argv[1:]) or "(none — resident trading mode)")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # non-Unix platform or non-main thread: default handling
    try:
        if backtest:
            async def _run_backtest() -> Dict[str, Any]:
                client = UpstoxClient(ACCESS_TOKEN, API_BASE_URL, logger)
                await client.start()
                try:
                    return await Backtester(client, logger, synthetic=synthetic).run()
                finally:
                    await client.close()
            report = loop.run_until_complete(_run_backtest())
            print(json.dumps(report, indent=2, default=str))
        else:
            clock = TradeClock(NSE_HOLIDAYS_2026, SPECIAL_SESSIONS_2026, SESSION_OPEN_TIME, SESSION_CLOSE_TIME, IST_TZ)
            orch = LiveOrchestrator(clock, logger, stop_event=stop_event)

            async def _run() -> None:
                try:
                    await orch.setup()
                    # Resident by default: wait visibly for the next 15:20 gate
                    # at ANY start time, run the session, wait again, repeat.
                    # `--once` exits after today's session (cron-friendly).
                    await orch.run_forever(once=once)
                finally:
                    # Always runs — shutdown is safe even after a partial setup.
                    await orch.shutdown()

            try:
                loop.run_until_complete(_run())
            except ConfigurationError as exc:
                logger.error("ABORTED: %s", exc)
            if stop_event.is_set():
                logger.info("[SHUTDOWN] stop signal received — state saved; engine exited cleanly")
            else:
                logger.info("[EXIT] engine stopped (once-mode finished or error above)")
    finally:
        loop.close()

if __name__ == "__main__":
    main()
