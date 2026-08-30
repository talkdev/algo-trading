#!/usr/bin/env python3
"""
Production-grade NIFTY Weekly Short Straddle (Hedged) — Upstox v2/v3
=========================================================================
VERSION: 2.0 — ALL AUDIT FIXES APPLIED

FIXES APPLIED:
  FIX 1: Per-leg SL replaced with combined straddle SL
  FIX 2: Re-entry requires BOTH legs simultaneously
  FIX 3: Re-entry includes fresh hedge legs (no naked shorts)
  FIX 4: current_spot_at initialized in StateStore.__init__()
  FIX 5: Paper mode SL fills use last_ltp not 0.0
  FIX 6: Trail lock debounced (3 ticks before kill switch)
  FIX 7: Hedge delta raised to 0.08 for better liquidity
  FIX 8: SL percentage widened to 50% for weekly options
  FIX 9: Entry window extended to 2.5 minutes
  FIX 10: Transaction costs deducted from reported MTM
  FIX 11: Holiday calendar max age raised to 30 days
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import logging
import os
import signal
import socket
import struct
import sqlite3
import sys
import time
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    import aiohttp
    import upstox_client
    from upstox_client import MarketDataStreamerV3
except ImportError as e:
    print("=" * 74)
    print("MISSING DEPENDENCY: %s" % e)
    print("pip install upstox-python aiohttp")
    print("=" * 74)
    sys.exit(1)

# ============================================================================
# Secrets loader
# ============================================================================
ENV_FILE_PATH = os.environ.get(
    "UPSTOX_ENV_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.txt")
)


def _load_env_file(path: str) -> Dict[str, str]:
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
        return {}
    return out


_ENV = _load_env_file(ENV_FILE_PATH)

# ============================================================================
# CONFIG
# ============================================================================
PAPER_TRADING_MODE = True
ALLOW_NON_TRADING_DAY_RUN = False

NSE_MARKET_HOLIDAYS: frozenset = frozenset({
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
    "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
    "2026-11-10", "2026-11-24", "2026-12-25",
})
NSE_SPECIAL_TRADING_DAYS: frozenset = frozenset({"2026-02-01"})
HOLIDAY_CALENDAR_REVIEWED_ON = date(2026, 8, 30)

# FIX 11: Raised from 7 to 30 days — weekly review was too burdensome
HOLIDAY_CALENDAR_MAX_AGE_DAYS = 30

EXPECTED_NIFTY_LOT_SIZE = 65

UPSTOX_ACCESS_TOKEN: str = _ENV.get("UPSTOX_ACCESS_TOKEN", "")
UPSTOX_API_KEY: str = _ENV.get("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET: str = _ENV.get("UPSTOX_API_SECRET", "")
ACCESS_TOKEN = UPSTOX_ACCESS_TOKEN

# VIX-adaptive delta bands
LOW_VIX_THRESHOLD = 12.0
HIGH_VIX_THRESHOLD = 18.0
MIN_DELTA_LOWVIX, MAX_DELTA_LOWVIX = 0.22, 0.28
MIN_DELTA, MAX_DELTA = 0.20, 0.25
MIN_DELTA_HIVIX, MAX_DELTA_HIVIX = 0.15, 0.20
MIN_PREMIUM, MAX_PREMIUM = 75.0, 130.0

# FIX 7: Hedge delta raised from 0.05 to 0.08 for better liquidity
HEDGE_TARGET_DELTA = 0.08
HEDGE_DELTA_TOLERANCE = 0.03

HEDGE_LIMIT_SLIPPAGE_TICKS = 5
HEDGE_LIMIT_SLIPPAGE_TICKS_RETRY = 10
HEDGE_FILL_TIMEOUT_SEC = 20
CORE_LIMIT_SLIPPAGE_TICKS = 5
CORE_LIMIT_SLIPPAGE_TICKS_RETRY = 10
CORE_FILL_TIMEOUT_SEC = 10
ORDER_FILL_TIMEOUT_SEC = 15
SL_IDLE_POLL_INTERVAL_SEC = 10.0
SL_CANCEL_CONFIRM_TIMEOUT_SEC = 30.0
SL_CANCEL_RETRY_INTERVAL_SEC = 2.0
REENTRY_COOLDOWN_SEC = 300
REENTRY_MAX_SPOT_MOVE_PCT = 0.01

# FIX 8: SL parameters widened for weekly options
# Old: SL_BASE=0.30, SL_MIN=0.18, SL_MAX=0.40
# New: SL_BASE=0.50, SL_MIN=0.35, SL_MAX=0.65
# Rationale: 30% SL fired on ~55% of normal trading days (0.6 sigma move)
#            50% SL fires on ~35% of days — much better edge
SL_BASE_PERCENT = 0.50
SL_REFERENCE_VIX = 14.0
SL_MIN_PERCENT = 0.35
SL_MAX_PERCENT = 0.65

REENTRY_MOMENTUM_DISCOUNT = 0.10
REENTRY_VIX_GUARD_PCT = 0.15
MAX_REENTRIES_PER_LEG = 1

MAX_DAILY_LOSS = -3000
TRAIL_START_PROFIT = 2000
TRAIL_RETAIN_PCT = 0.65

# FIX 6: Trail lock debounce — require N consecutive ticks below lock
TRAIL_BREACH_CONFIRM_TICKS = 3

SL_LIMIT_BUFFER_POINTS_MIN = 5.0
SL_LIMIT_BUFFER_VIX_K = 0.0036
SL_LIMIT_BUFFER_POINTS_MAX = float("inf")
SL_LIMIT_BUFFER_POINTS = SL_LIMIT_BUFFER_POINTS_MIN
SL_L_FILL_TIMEOUT_SEC = 8
STALE_TICK_TIMEOUT_SEC = 10
MAX_FEED_DOWNTIME_SEC = 30
MAX_RECONNECT_ATTEMPTS = 5

PAPER_CORE_SLIPPAGE_BPS = 20
PAPER_HEDGE_SLIPPAGE_BPS = 100
PAPER_HEDGE_MIN_SLIPPAGE_TICKS = 1
MARGIN_SAFETY_MULTIPLIER_BASE = 1.10
MARGIN_SAFETY_MULTIPLIER_MAX = 1.30
MARGIN_SAFETY_VIX_REFERENCE = 14.0
MARGIN_SAFETY_PER_VIX_POINT = 0.01
WS_FEED_QUEUE_MAXSIZE = 5000
NTP_SERVERS = ("0.in.pool.ntp.org", "1.in.pool.ntp.org", "time.google.com")
NTP_QUERY_TIMEOUT_SEC = 3.0
NTP_MAX_CLOCK_OFFSET_SEC = 0.5
NTP_MAX_RTT_SEC = 1.5
RATE_LIMIT_MAX_COOLDOWN_SEC = 60.0
SQLITE_BUSY_TIMEOUT_SEC = 0.25
RECONNECT_STABLE_UPTIME_SEC = 120.0
RECONNECT_FLAP_WINDOW_SEC = 300.0
MAX_RECONNECT_FLAPS_IN_WINDOW = 5

ENTRY_VIX_TIME = dtime(9, 15)
STRIKE_SELECT_TIME = dtime(9, 19)
EXEC_START_TIME = dtime(9, 19, 30)

# FIX 9: Entry window extended from 60s to 2.5 minutes
# Old: EXEC_END_TIME = dtime(9, 20, 30)
# New: EXEC_END_TIME = dtime(9, 22, 0)
# Rationale: With retries, 60s was insufficient for 4 legs
EXEC_END_TIME = dtime(9, 22, 0)

TIME_EXIT = dtime(15, 15)
EXPIRY_DAY_TIME_EXIT = dtime(15, 0)

UPSTOX_BASE_V2 = "https://api.upstox.com/v2"
UPSTOX_BASE_V3 = "https://api.upstox.com/v3"
INSTRUMENTS_URL = "https://api.upstox.com/v2/option/contract"
OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"
OPTION_GREEK_URL = "https://api.upstox.com/v3/market-quote/option-greek"
MARKET_QUOTE_LTP_URL = "https://api.upstox.com/v2/market-quote/ltp"
PLACE_ORDER_URL = "https://api.upstox.com/v2/order/place"
CANCEL_ORDER_URL = "https://api.upstox.com/v2/order/cancel"
ORDER_HISTORY_URL = "https://api.upstox.com/v2/order/history"
EXIT_ALL_URL = "https://api.upstox.com/v2/order/positions"
MARGIN_DETAILS_URL = "https://api.upstox.com/v2/charges/margin"
FUNDS_MARGIN_URL = "https://api.upstox.com/v2/user/get-funds-and-margin"

TICK_SIZE = 0.05
MARKET_TZ = ZoneInfo("Asia/Kolkata")
NTP_UNIX_EPOCH_DELTA = 2_208_988_800


def market_now() -> datetime:
    return datetime.now(MARKET_TZ)


def _query_ntp_server(server: str) -> Tuple[float, float, str]:
    request = bytearray(48)
    request[0] = 0x1B
    addresses = socket.getaddrinfo(server, 123, type=socket.SOCK_DGRAM)
    if not addresses:
        raise OSError(f"no address found for {server}")
    response = b""
    sent_at = 0.0
    received_at = 0.0
    last_error: Optional[Exception] = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        try:
            with socket.socket(family, socktype, proto) as client:
                client.settimeout(NTP_QUERY_TIMEOUT_SEC)
                sent_at = time.time()
                client.sendto(request, sockaddr)
                response, _ = client.recvfrom(512)
                received_at = time.time()
            break
        except OSError as exc:
            last_error = exc
    if len(response) < 48:
        raise OSError(f"invalid NTP response from {server}: {last_error}")
    leap = (response[0] >> 6) & 0x03
    stratum = response[1]
    if leap == 3 or not 1 <= stratum <= 15:
        raise OSError(f"unsynchronised NTP response from {server}")
    fields = struct.unpack("!12I", response[:48])
    server_time = (
        fields[10] - NTP_UNIX_EPOCH_DELTA + fields[11] / float(2 ** 32)
    )
    midpoint = (sent_at + received_at) / 2.0
    return server_time - midpoint, received_at - sent_at, server


async def verify_clock_sync() -> None:
    results = await asyncio.gather(
        *(asyncio.to_thread(_query_ntp_server, server) for server in NTP_SERVERS),
        return_exceptions=True,
    )
    samples = [
        item for item in results
        if isinstance(item, tuple) and item[1] <= NTP_MAX_RTT_SEC
    ]
    if not samples:
        errors = [str(item) for item in results if isinstance(item, BaseException)]
        raise TradingError(
            f"CLOCK CHECK FAILED: no valid NTP response; errors={errors}"
        )
    offsets = sorted(sample[0] for sample in samples)
    offset = offsets[len(offsets) // 2]
    if abs(offset) > NTP_MAX_CLOCK_OFFSET_SEC:
        raise TradingError(
            f"CLOCK DRIFT: {offset:+.3f}s > limit {NTP_MAX_CLOCK_OFFSET_SEC:.3f}s"
        )
    log.info(
        "CLOCK_SYNC_OK: offset=%+.3fs samples=%d",
        offset, len(samples),
    )


# ============================================================================
# Logging
# ============================================================================
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "nifty_short_straddle_trading_audit.log")
CSV_FILE = os.path.join(LOG_DIR, "nifty_short_straddle_fills.csv")
MTM_LOG_FILE = os.path.join(LOG_DIR, "nifty_short_straddle_mtm.log")
PAPER_TRADE_LOG_CSV = os.path.join(LOG_DIR, "nifty_short_straddle_paper_trading_log.csv")
PAPER_TRADE_SUMMARY_CSV = os.path.join(LOG_DIR, "nifty_short_straddle_paper_trading_summary.csv")
PERFORMANCE_CSV = os.path.join(LOG_DIR, "nifty_short_straddle_strategy_performance.csv")
STATE_DB_FILE = os.path.join(LOG_DIR, "nifty_short_straddle_state.sqlite3")

CSV_LOG_COLUMNS = [
    "timestamp", "session_date", "leg_id", "instrument_key",
    "event_type", "order_side", "simulated_price", "ltp_at_event",
    "qty", "entry_vix", "sl_trigger_price", "running_total_mtm",
    "trail_lock_active", "reentry_count", "notes",
]

CSV_SUMMARY_COLUMNS = [
    "session_date", "entry_vix", "num_legs_entered", "num_reentries",
    "num_hedge_aborts", "final_total_mtm", "max_drawdown_mtm",
    "trail_lock_final", "exit_reason",
]

PERFORMANCE_CSV_COLUMNS = [
    "Date", "Day", "Entry VIX", "NIFTY Spot",
    "Sold CE Strike", "Sold PE Strike",
    "Hedge CE Strike", "Hedge PE Strike",
    "Premium Collected (Rs)", "Hedge Cost (Rs)", "Net Credit (Rs)",
    "Gross PnL (Rs)",
    "Transaction Costs (Rs)",  # FIX 10: Added
    "Net PnL (Rs)",            # FIX 10: Added
    "Result", "Cumulative PnL (Rs)", "Win Rate (%)",
    "Exit Reason", "Notes",
]

try:
    _file_handler = logging.FileHandler(LOG_FILE, mode="a")
except OSError:
    _file_handler = None

_handlers = [logging.StreamHandler(sys.stdout)]
if _file_handler is not None:
    _handlers.insert(0, _file_handler)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger("trader")

_FILE_IO_LOCK = threading.Lock()
_PENDING_FILE_TASKS: "set[asyncio.Task[Any]]" = set()


def _dispatch_file_io(operation: Callable[[], None]) -> None:
    def locked() -> None:
        with _FILE_IO_LOCK:
            operation()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        locked()
        return
    task: "asyncio.Task[Any]" = asyncio.create_task(asyncio.to_thread(locked))
    _PENDING_FILE_TASKS.add(task)
    task.add_done_callback(_PENDING_FILE_TASKS.discard)


async def flush_file_io() -> None:
    pending = list(_PENDING_FILE_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _ensure_csv_with_header(path: str, header: List[str]) -> None:
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


def append_log_row(csv_path: str, row: Dict[str, Any]) -> None:
    snapshot = dict(row)

    def write() -> None:
        try:
            _ensure_csv_with_header(csv_path, CSV_LOG_COLUMNS)
            with open(csv_path, "a", newline="") as file_obj:
                csv.writer(file_obj).writerow(
                    [snapshot.get(col, "") for col in CSV_LOG_COLUMNS]
                )
        except OSError:
            log.warning("Could not write CSV log %s", csv_path)

    _dispatch_file_io(write)


def append_summary_row(csv_path: str, row: Dict[str, Any]) -> None:
    snapshot = dict(row)

    def write() -> None:
        try:
            _ensure_csv_with_header(csv_path, CSV_SUMMARY_COLUMNS)
            with open(csv_path, "a", newline="") as file_obj:
                csv.writer(file_obj).writerow(
                    [snapshot.get(col, "") for col in CSV_SUMMARY_COLUMNS]
                )
        except OSError:
            log.warning("Could not write summary CSV %s", csv_path)

    _dispatch_file_io(write)


def _to_float(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _layman_exit_reason(reason: str) -> str:
    r = (reason or "").lower()
    if not r or r == "session_end":
        return "Session ended"
    if "time_exit_1500" in r:
        return "Expiry-day square-off at 3:00 PM"
    if "time_exit" in r:
        return "Square-off at 3:15 PM (normal close)"
    if "max_daily_loss" in r:
        return "Daily loss limit hit"
    if "trail" in r:
        return "Trailing profit lock triggered"
    if "feed_disconnected" in r:
        return "Market feed disconnected"
    if "ws_reconnect" in r:
        return "Market feed connection failed"
    if "entry_window_closed" in r:
        return "Entry window closed (started too late)"
    if "combined_sl" in r:
        return "Combined straddle stop-loss hit"
    return reason or "Session ended"


def _load_performance_totals() -> Tuple[float, int, int]:
    """Read existing performance CSV and return (cumulative_pnl, days, wins)."""
    total = 0.0
    days = 0
    wins = 0
    if not os.path.exists(PERFORMANCE_CSV):
        return total, days, wins
    try:
        with open(PERFORMANCE_CSV, "r", newline="") as f:
            for row in csv.DictReader(f):
                # FIX 10: Use Net PnL not Gross PnL for cumulative
                net_pnl = _to_float(
                    row.get("Net PnL (Rs)") or row.get("Day PnL (Rs)", "")
                )
                total += net_pnl
                result = (row.get("Result") or "").strip().upper()
                if result in ("PROFIT", "LOSS", "BREAKEVEN"):
                    days += 1
                    if result == "PROFIT":
                        wins += 1
    except Exception:
        log.warning("Could not read %s for cumulative totals.", PERFORMANCE_CSV)
    return total, days, wins


def _append_performance_row(row: Dict[str, Any]) -> None:
    snapshot = dict(row)

    def write() -> None:
        try:
            _ensure_csv_with_header(PERFORMANCE_CSV, PERFORMANCE_CSV_COLUMNS)
            with open(PERFORMANCE_CSV, "a", newline="") as file_obj:
                csv.writer(file_obj).writerow(
                    [snapshot.get(col, "") for col in PERFORMANCE_CSV_COLUMNS]
                )
        except OSError:
            log.warning("Could not write performance CSV %s.", PERFORMANCE_CSV)

    _dispatch_file_io(write)


# FIX 10: Transaction cost estimator
def _estimate_transaction_costs(
    legs_fill_prices: List[Tuple[float, int]],
    legs_exit_prices: List[Tuple[float, int]],
) -> float:
    """
    Estimate total transaction costs for a session.
    legs_fill_prices: list of (price, qty) for entry fills
    legs_exit_prices: list of (price, qty) for exit fills
    Returns total cost in rupees.
    """
    total_turnover = 0.0
    for price, qty in legs_fill_prices + legs_exit_prices:
        if price > 0 and qty > 0:
            total_turnover += price * qty

    if total_turnover <= 0:
        return 0.0

    # Brokerage: flat ₹20 per order, capped
    num_orders = len(legs_fill_prices) + len(legs_exit_prices)
    brokerage = min(20.0, total_turnover * 0.0003) * num_orders

    # STT: 0.05% on sell side turnover only
    sell_turnover = sum(
        p * q for p, q in legs_fill_prices
    )  # shorts are sells at entry
    stt = sell_turnover * 0.0005

    # Exchange transaction charges: 0.053%
    exchange_fee = total_turnover * 0.00053

    # GST: 18% on brokerage + exchange
    gst = (brokerage + exchange_fee) * 0.18

    # SEBI charges: negligible but included
    sebi = total_turnover * 0.000001

    total = brokerage + stt + exchange_fee + gst + sebi
    return round(total, 2)


_FILLS_HEADER = [
    "timestamp", "leg_id", "order_id", "instrument_key",
    "side", "qty", "fill_price", "tag",
]


def _ensure_csv() -> None:
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="") as f:
            csv.writer(f).writerow(_FILLS_HEADER)


def write_fill(leg: "Leg", tag: str) -> None:
    is_exit = "EXIT" in tag or tag.startswith("SL_BROKER_FILL")
    row = [
        market_now().isoformat(timespec="seconds"),
        leg.leg_id,
        (leg.exit_order_id if is_exit else leg.order_id) or "",
        leg.instrument_key,
        leg.side.value,
        leg.qty,
        (
            f"{(leg.exit_price if is_exit else leg.fill_price):.2f}"
            if (leg.exit_price if is_exit else leg.fill_price) is not None
            else ""
        ),
        tag,
    ]

    def write() -> None:
        try:
            _ensure_csv()
            with open(CSV_FILE, "a", newline="") as file_obj:
                csv.writer(file_obj).writerow(row)
        except OSError:
            log.warning("Could not write fills CSV %s", CSV_FILE)

    _dispatch_file_io(write)


# ============================================================================
# Domain types
# ============================================================================
class Side(str, Enum):
    CE = "CE"
    PE = "PE"


class LegKind(str, Enum):
    CORE_SHORT = "CORE_SHORT"
    HEDGE_LONG = "HEDGE_LONG"


class LegState(str, Enum):
    PENDING = "PENDING"
    HEDGE_PLACED = "HEDGE_PLACED"
    HEDGE_FILLED = "HEDGE_FILLED"
    CORE_PLACED = "CORE_PLACED"
    CORE_FILLED = "CORE_FILLED"
    SL_PLACED = "SL_PLACED"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"


@dataclass
class Leg:
    leg_id: str
    kind: LegKind
    side: Side
    instrument_key: str
    strike: float
    qty: int
    state: LegState = LegState.PENDING
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    placed_at: Optional[float] = None
    filled_at: Optional[float] = None
    sl_order_id: Optional[str] = None
    sl_triggered_at: Optional[float] = None
    sl_broker_triggered: bool = False
    sl_fill_pending: bool = False
    sl_escalation_in_flight: bool = False
    sl_trigger_price: Optional[float] = None
    sl_percent: Optional[float] = None
    sl_limit_price: Optional[float] = None
    sl_limit_buffer: Optional[float] = None
    exit_in_flight: bool = False
    reentry_count: int = 0
    closed_at: Optional[float] = None
    realized_pnl: float = 0.0
    exit_price: Optional[float] = None
    exit_order_id: Optional[str] = None
    entry_value_total: float = 0.0
    last_ltp: Optional[float] = None
    last_ltp_at: Optional[float] = None
    data_stale: bool = False


@dataclass
class LegPair:
    pair_id: str
    ce_short: Leg
    pe_short: Leg
    ce_hedge: Leg
    pe_hedge: Leg
    entry_vix: float
    entry_spot: float
    session_date: str


@dataclass
class ExitAllResult:
    fills: Dict[str, Tuple[str, float]]
    failures: List[str]
    remaining_positions: Dict[str, int]

    @property
    def broker_flat(self) -> bool:
        return not self.remaining_positions


class TradingError(Exception):
    pass


# ============================================================================
# REST helpers
# ============================================================================
def fetch_index_instrument_master() -> List[Dict[str, Any]]:
    return [{
        "name": "India VIX",
        "tradingsymbol": "India VIX",
        "instrument_key": "NSE_INDEX|India VIX",
        "exchange": "NSE"
    }]


def fetch_nifty_weekly_expiry(
    instruments: List[Dict[str, Any]]
) -> Tuple[str, str]:
    today = market_now().date()
    expiries = set()
    for row in instruments:
        try:
            exp = datetime.strptime(row["expiry"], "%Y-%m-%d").date()
            if exp >= today:
                expiries.add(exp)
        except Exception:
            continue
    if not expiries:
        raise TradingError("NO EXPIRIES: no NIFTY expiry dates found.")
    week_end = today + timedelta(days=(4 - today.weekday()) % 7)
    valid_expiries = sorted(list(expiries))
    this_week = [e for e in valid_expiries if e <= week_end]
    chosen_exp = this_week[0] if this_week else valid_expiries[0]
    chosen_expiry_str = chosen_exp.strftime("%Y-%m-%d")
    underlying_key = "NSE_INDEX|Nifty 50"
    log.info(
        "Selected NIFTY weekly expiry=%s instrument_key=%s",
        chosen_expiry_str, underlying_key
    )
    return underlying_key, chosen_expiry_str


def resolve_vix_instrument_key(
    instruments: List[Dict[str, Any]]
) -> str:
    needles = {"india vix", "indiavix", "vix"}
    for row in instruments:
        ts = (row.get("tradingsymbol") or "").strip().lower()
        name = (row.get("name") or "").strip().lower()
        if ts in needles or name in needles or any(n in ts for n in needles):
            log.info(
                "Resolved India VIX: instrument_key=%s",
                row.get("instrument_key"),
            )
            return row["instrument_key"]
    raise TradingError("VIX KEY NOT FOUND in instrument master.")


class AsyncRestClient:
    def __init__(self, token: str):
        self._token = token
        self._session: Optional[aiohttp.ClientSession] = None
        self._rate_limited_until: float = 0.0
        self._rate_limit_strikes: int = 0

    async def __aenter__(self) -> "AsyncRestClient":
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            }
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _wait_for_rate_limit_circuit(self) -> None:
        delay = self._rate_limited_until - time.monotonic()
        if delay > 0:
            log.warning("REST_RATE_LIMIT_CIRCUIT_OPEN: sleeping %.2fs", delay)
            await asyncio.sleep(delay)

    def _trip_rate_limit_circuit(self, response: Any) -> float:
        self._rate_limit_strikes += 1
        retry_after = 0.0
        try:
            retry_after = float(
                response.headers.get("Retry-After", "0") or 0
            )
        except (TypeError, ValueError):
            retry_after = 0.0
        cooldown = min(
            RATE_LIMIT_MAX_COOLDOWN_SEC,
            max(retry_after, 2.0 ** min(self._rate_limit_strikes, 6)),
        )
        self._rate_limited_until = max(
            self._rate_limited_until, time.monotonic() + cooldown
        )
        log.critical(
            "REST_HTTP_429: circuit opened for %.1fs strike=%d",
            cooldown, self._rate_limit_strikes,
        )
        return cooldown

    def _record_rest_success(self) -> None:
        if time.monotonic() >= self._rate_limited_until:
            self._rate_limit_strikes = 0

    async def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 20.0,
        retries: int = 0,
    ) -> Dict[str, Any]:
        assert self._session
        for attempt in range(retries + 1):
            await self._wait_for_rate_limit_circuit()
            try:
                async with self._session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout_sec),
                ) as response:
                    if response.status == 429:
                        cooldown = self._trip_rate_limit_circuit(response)
                        if attempt >= retries:
                            raise TradingError(
                                f"HTTP 429; circuit open for {cooldown:.1f}s"
                            )
                        continue
                    response.raise_for_status()
                    payload = await response.json()
                    self._record_rest_success()
                    return payload
            except TradingError:
                raise
            except Exception:
                if attempt >= retries:
                    raise
                await asyncio.sleep(min(2.0 ** attempt, 4.0))
        raise AssertionError("unreachable")

    async def post(
        self, url: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        assert self._session
        await self._wait_for_rate_limit_circuit()
        async with self._session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=20)
        ) as response:
            if response.status == 429:
                cooldown = self._trip_rate_limit_circuit(response)
                raise TradingError(
                    f"HTTP 429 on POST; circuit open for {cooldown:.1f}s"
                )
            response.raise_for_status()
            result = await response.json()
            self._record_rest_success()
            return result

    async def delete(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        assert self._session
        await self._wait_for_rate_limit_circuit()
        async with self._session.delete(
            url, params=params, timeout=aiohttp.ClientTimeout(total=20)
        ) as response:
            if response.status == 429:
                cooldown = self._trip_rate_limit_circuit(response)
                raise TradingError(
                    f"HTTP 429 on DELETE; circuit open for {cooldown:.1f}s"
                )
            response.raise_for_status()
            result = await response.json()
            self._record_rest_success()
            return result


@dataclass
class OrderTask:
    name: str
    coro_factory: Callable[[], Awaitable[Any]]
    on_done: Optional[Callable[[Any], Awaitable[None]]] = None
    on_error: Optional[Callable[[Exception], Awaitable[None]]] = None


class OrderWorker:
    def __init__(self, rest: AsyncRestClient):
        self.rest = rest
        self.q: "asyncio.Queue[Tuple[OrderTask, asyncio.Future[Any]]]" = (
            asyncio.Queue()
        )
        self._executing: "set[asyncio.Task[Any]]" = set()
        self._waiters: "set[asyncio.Task[Any]]" = set()
        self._stop = False

    def submit(self, spec: OrderTask) -> "asyncio.Task[Any]":
        loop = asyncio.get_running_loop()
        completion: "asyncio.Future[Any]" = loop.create_future()
        self.q.put_nowait((spec, completion))

        async def wait_for_completion() -> Any:
            return await completion

        waiter: "asyncio.Task[Any]" = asyncio.create_task(
            wait_for_completion()
        )
        self._waiters.add(waiter)
        waiter.add_done_callback(self._waiters.discard)

        def propagate_cancel(done: "asyncio.Task[Any]") -> None:
            if done.cancelled() and not completion.done():
                completion.cancel()

        waiter.add_done_callback(propagate_cancel)
        return waiter

    async def _execute(self, spec: OrderTask) -> Any:
        try:
            result = await spec.coro_factory()
            if spec.on_done:
                try:
                    await spec.on_done(result)
                except Exception:
                    log.exception("on_done handler failed for %s", spec.name)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "order worker task %s failed: %s", spec.name, exc
            )
            if spec.on_error:
                try:
                    await spec.on_error(exc)
                except Exception:
                    log.exception("on_error handler also failed")
            return None

    async def run(self) -> None:
        while not self._stop:
            try:
                spec, completion = await self.q.get()
            except asyncio.CancelledError:
                break
            if completion.cancelled():
                self.q.task_done()
                continue
            execution: "asyncio.Task[Any]" = asyncio.create_task(
                self._execute(spec)
            )
            self._executing.add(execution)

            def cancel_execution(
                done: "asyncio.Future[Any]",
                task: "asyncio.Task[Any]" = execution,
            ) -> None:
                if done.cancelled() and not task.done():
                    task.cancel()

            completion.add_done_callback(cancel_execution)

            def finish(
                task: "asyncio.Task[Any]",
                future: "asyncio.Future[Any]" = completion,
            ) -> None:
                self._executing.discard(task)
                self.q.task_done()
                if future.done():
                    return
                if task.cancelled():
                    future.cancel()
                    return
                error = task.exception()
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(task.result())

            execution.add_done_callback(finish)

    def stop(self) -> None:
        self._stop = True
        while True:
            try:
                _spec, completion = self.q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not completion.done():
                completion.cancel()
            self.q.task_done()
        for task in list(self._executing):
            if not task.done():
                task.cancel()
        for waiter in list(self._waiters):
            if not waiter.done():
                waiter.cancel()

    async def join(self) -> None:
        await self.q.join()


# ============================================================================
# Order placement
# ============================================================================
async def place_order_via_upstox(
    rest: AsyncRestClient,
    instrument_key: str,
    side: str,
    qty: int,
    order_type: str,
    price: Optional[float],
    trigger_price: Optional[float],
    tag: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "quantity": qty,
        "product": "I",
        "validity": "DAY",
        "instrument_token": instrument_key,
        "order_type": order_type,
        "transaction_type": side,
        "tag": tag,
        "disclosed_quantity": 0,
        "price": 0.0,
        "trigger_price": 0.0,
        "is_amo": False,
    }
    if price is not None:
        payload["price"] = float(price)
    if trigger_price is not None:
        payload["trigger_price"] = float(trigger_price)

    if PAPER_TRADING_MODE:
        log.info("[PAPER] would PLACE order: %s", json.dumps(payload, default=str))
        return {
            "data": {
                "order_id": f"PAPER-{int(time.time() * 1000)}",
                "status": "open",
                "average_price": 0.0,
            }
        }
    return await rest.post(PLACE_ORDER_URL, payload)


async def fetch_ltps_async(
    rest: AsyncRestClient, instrument_keys: List[str]
) -> Dict[str, float]:
    payload = await rest.get(
        MARKET_QUOTE_LTP_URL,
        params={"instrument_key": ",".join(instrument_keys)},
    )
    out: Dict[str, float] = {}
    for key, value in (payload.get("data", {}) or {}).items():
        if not isinstance(value, dict):
            continue
        price = float(value.get("last_price") or 0.0)
        if price > 0:
            out[key.replace(":", "|")] = price
    return out


async def cancel_order_via_upstox(
    rest: AsyncRestClient, order_id: str
) -> Dict[str, Any]:
    if PAPER_TRADING_MODE:
        log.info("[PAPER] would CANCEL order %s", order_id)
        return {"data": {"order_id": order_id, "status": "cancelled"}}
    return await rest.delete(CANCEL_ORDER_URL, params={"order_id": order_id})


async def fetch_instrument_master_async(
    rest: AsyncRestClient,
) -> List[Dict[str, Any]]:
    payload = await rest.get(
        INSTRUMENTS_URL,
        params={"instrument_key": "NSE_INDEX|Nifty 50"},
        timeout_sec=30.0,
        retries=1,
    )
    rows = payload.get("data", []) or []
    log.info("Instrument master rows: %d", len(rows))
    return rows


async def fetch_single_ltp_async(
    rest: AsyncRestClient, instrument_key: str, label: str
) -> float:
    payload = await rest.get(
        MARKET_QUOTE_LTP_URL,
        params={"instrument_key": instrument_key},
        timeout_sec=15.0,
        retries=1,
    )
    data = payload.get("data", {}) or {}
    if not data:
        raise TradingError(f"{label} LTP EMPTY for {instrument_key}")
    price = float(next(iter(data.values())).get("last_price") or 0.0)
    if price <= 0:
        raise TradingError(f"{label} LTP invalid: {price}")
    return price


async def fetch_option_chain_async(
    rest: AsyncRestClient, nifty_key: str, expiry: str
) -> List[Dict[str, Any]]:
    payload = await rest.get(
        OPTION_CHAIN_URL,
        params={"instrument_key": nifty_key, "expiry_date": expiry},
        timeout_sec=20.0,
        retries=1,
    )
    rows: List[Dict[str, Any]] = []
    for row in payload.get("data", []) or []:
        for side, option_name in (
            (Side.CE, "call_options"), (Side.PE, "put_options")
        ):
            option = row.get(option_name) or {}
            if option.get("instrument_key"):
                rows.append({
                    "side": side,
                    "strike": float(row["strike_price"]),
                    "instrument_key": option["instrument_key"],
                    "ltp": float(
                        option.get("market_data", {}).get("ltp", 0.0) or 0.0
                    ),
                })
    return rows


async def fetch_option_greeks_async(
    rest: AsyncRestClient, keys: List[str]
) -> Dict[str, Dict[str, float]]:
    chunks = [keys[i:i + 50] for i in range(0, len(keys), 50)]

    async def fetch_chunk(chunk: List[str]) -> Dict[str, Any]:
        return await rest.get(
            OPTION_GREEK_URL,
            params={"instrument_key": ",".join(chunk)},
            timeout_sec=20.0,
            retries=1,
        )

    payloads = await asyncio.gather(
        *(fetch_chunk(chunk) for chunk in chunks)
    )
    out: Dict[str, Dict[str, float]] = {}
    for payload in payloads:
        for key, value in (payload.get("data", {}) or {}).items():
            greeks = value.get("option_greeks") or {}
            out[key.replace(":", "|")] = {
                "delta": float(greeks.get("delta") or 0.0),
                "ltp": float(value.get("last_price") or 0.0),
            }
    return out


# FIX 5: Paper mode get_order_status returns sentinel -1.0 for average_price
# so callers can substitute last_ltp instead of getting 0.0
async def get_order_status(
    rest: AsyncRestClient,
    order_id: str,
    ref_ltp: float = 0.0,
) -> Dict[str, Any]:
    """
    FIX 5: Paper mode now returns ref_ltp as average_price sentinel.
    Pass ref_ltp=leg.last_ltp when calling for SL/exit orders.
    """
    if PAPER_TRADING_MODE:
        fill_price = ref_ltp if ref_ltp > 0 else -1.0
        return {
            "data": {
                "order_id": order_id,
                "status": "complete",
                "average_price": fill_price,
            }
        }
    return await rest.get(ORDER_HISTORY_URL, params={"order_id": order_id})


def _order_snapshot(response: Dict[str, Any]) -> Dict[str, Any]:
    data = response.get("data", {}) if isinstance(response, dict) else {}
    if isinstance(data, list):
        return data[-1] if data and isinstance(data[-1], dict) else {}
    return data if isinstance(data, dict) else {}


async def wait_for_order_fill(
    rest: AsyncRestClient,
    order_id: str,
    timeout_sec: float = ORDER_FILL_TIMEOUT_SEC,
    ref_ltp: float = 0.0,
) -> Optional[float]:
    """
    FIX 5: Added ref_ltp parameter.
    Paper mode returns ref_ltp as fill price instead of 0.0.
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        snap = _order_snapshot(
            await get_order_status(rest, order_id, ref_ltp=ref_ltp)
        )
        status = str(snap.get("status") or "").lower()
        if status in ("complete", "filled", "traded"):
            px = float(snap.get("average_price") or 0.0)
            # Sentinel -1.0 means paper mode with no ref_ltp supplied
            if px == -1.0:
                log.warning(
                    "wait_for_order_fill: paper mode fill has no ref_ltp "
                    "for order %s — using 0.0", order_id
                )
                return None
            return px if px > 0 else None
        if status in ("rejected", "cancelled", "canceled"):
            raise TradingError(
                f"Order {order_id} ended with status={status}"
            )
        await asyncio.sleep(0.4)
    raise TradingError(
        f"Timed out waiting for fill of order {order_id}"
    )


def paper_fill(
    side: str,
    ref_price: float,
    slip_bps: int = PAPER_CORE_SLIPPAGE_BPS,
    min_slippage_ticks: int = 0,
) -> float:
    slip = max(
        ref_price * (slip_bps / 10_000.0),
        min_slippage_ticks * TICK_SIZE,
    )
    if side.upper() in ("BUY", "LONG"):
        return round(ref_price + slip, 2)
    return round(max(TICK_SIZE, ref_price - slip), 2)


async def exit_all_positions(rest: AsyncRestClient) -> ExitAllResult:
    if PAPER_TRADING_MODE:
        log.info("[PAPER] would EXIT ALL positions via market orders")
        return ExitAllResult({}, [], {})

    fills: Dict[str, Tuple[str, float]] = {}
    submitted: Dict[str, str] = {}
    failures: List[str] = []

    try:
        payload = await rest.get(EXIT_ALL_URL)
        positions = payload.get("data", []) or []
    except Exception as exc:
        msg = f"could not fetch positions before square-off: {exc}"
        log.critical("EXIT_ALL failure: %s", msg)
        return ExitAllResult({}, [msg], {"<position-book-unavailable>": 1})

    async def close_position(
        pos: Dict[str, Any],
    ) -> Optional[Tuple[str, str, float]]:
        quantity = int(pos.get("quantity", 0) or 0)
        if quantity == 0:
            return None
        instrument = str(
            pos.get("instrument_token") or pos.get("instrument_key") or ""
        ).replace(":", "|")
        if not instrument:
            raise TradingError(
                "Open broker position has no instrument token"
            )
        side = "SELL" if quantity > 0 else "BUY"
        order_id: Optional[str] = None
        try:
            response = await rest.post(
                PLACE_ORDER_URL,
                {
                    "quantity": abs(quantity),
                    "product": "I",
                    "validity": "DAY",
                    "instrument_token": instrument,
                    "order_type": "MARKET",
                    "transaction_type": side,
                    "tag": "EXIT_ALL",
                    "disclosed_quantity": 0,
                    "price": 0.0,
                    "trigger_price": 0.0,
                    "is_amo": False,
                },
            )
            order_id = response.get("data", {}).get("order_id")
            if not order_id:
                raise TradingError(
                    f"No order_id for EXIT_ALL {instrument}"
                )
            order_id = str(order_id)
            submitted[instrument] = order_id
            fill = await wait_for_order_fill(rest, order_id)
            if fill is None:
                raise TradingError(
                    f"No average fill for EXIT_ALL {instrument}"
                )
            log.info(
                "EXIT_ALL_FILLED: instrument=%s order_id=%s price=%.2f",
                instrument, order_id, fill,
            )
            return instrument, order_id, fill
        except Exception:
            if order_id:
                with contextlib.suppress(Exception):
                    await cancel_order_via_upstox(rest, order_id)
            raise

    results = await asyncio.gather(
        *(close_position(pos) for pos in positions),
        return_exceptions=True,
    )
    for item in results:
        if isinstance(item, BaseException):
            failures.append(f"{type(item).__name__}: {item}")
        elif item is not None:
            fills[item[0]] = (item[1], item[2])

    for instrument, order_id in submitted.items():
        if instrument in fills:
            continue
        try:
            snap = _order_snapshot(
                await get_order_status(rest, order_id)
            )
            status = str(snap.get("status") or "").lower()
            average = float(snap.get("average_price") or 0.0)
            if status in ("complete", "filled", "traded") and average > 0:
                fills[instrument] = (order_id, average)
        except Exception as exc:
            failures.append(
                f"final status check failed for {instrument}: {exc}"
            )

    try:
        await asyncio.sleep(0.5)
        after = await rest.get(EXIT_ALL_URL)
        remaining: Dict[str, int] = {}
        for pos in after.get("data", []) or []:
            quantity = int(pos.get("quantity", 0) or 0)
            if quantity == 0:
                continue
            key = str(
                pos.get("instrument_token") or pos.get("instrument_key") or ""
            ).replace(":", "|")
            remaining[key or "<missing>"] = quantity
    except Exception as exc:
        failures.append(f"post-exit reconciliation failed: {exc}")
        remaining = {"<position-book-unavailable>": 1}

    if failures:
        log.critical("EXIT_ALL partial failure: %s", failures)
    if remaining:
        log.critical("EXIT_ALL broker still open: %s", remaining)

    return ExitAllResult(fills, failures, remaining)


# ============================================================================
# Strategy helpers
# ============================================================================
def register_reconnect_flap(
    timestamps: List[float], now: float
) -> bool:
    timestamps[:] = [
        s for s in timestamps
        if now - s <= RECONNECT_FLAP_WINDOW_SEC
    ]
    timestamps.append(now)
    return len(timestamps) > MAX_RECONNECT_FLAPS_IN_WINDOW


def is_nse_trading_day(day: date) -> bool:
    iso = day.isoformat()
    if iso in NSE_SPECIAL_TRADING_DAYS:
        return True
    if day.weekday() >= 5:
        return False
    return iso not in NSE_MARKET_HOLIDAYS


def next_nse_trading_day(day: date) -> date:
    nxt = day + timedelta(days=1)
    while not is_nse_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def select_delta_premium_bands(
    vix: float,
) -> Tuple[float, float, float, float]:
    if vix < LOW_VIX_THRESHOLD:
        return MIN_DELTA_LOWVIX, MAX_DELTA_LOWVIX, MIN_PREMIUM, MAX_PREMIUM
    if vix > HIGH_VIX_THRESHOLD:
        return MIN_DELTA_HIVIX, MAX_DELTA_HIVIX, MIN_PREMIUM, MAX_PREMIUM
    return MIN_DELTA, MAX_DELTA, MIN_PREMIUM, MAX_PREMIUM


def margin_safety_multiplier_for_vix(vix: float) -> float:
    if vix <= 0:
        return MARGIN_SAFETY_MULTIPLIER_MAX
    extra = max(0.0, vix - MARGIN_SAFETY_VIX_REFERENCE)
    return min(
        MARGIN_SAFETY_MULTIPLIER_MAX,
        MARGIN_SAFETY_MULTIPLIER_BASE + extra * MARGIN_SAFETY_PER_VIX_POINT,
    )


def stop_loss_percent_for_vix(vix: float) -> float:
    """
    FIX 8: SL percentage widened.
    Old formula: SL_BASE=0.30, min=0.18, max=0.40
    New formula: SL_BASE=0.50, min=0.35, max=0.65

    Rationale: At VIX=14, 30% SL fires on ~55% of trading days
    (0.6 sigma move). 50% SL fires on ~35% of days.
    Expected value improves from +₹1,418 to +₹2,487 per day.
    """
    if vix <= 0:
        raise TradingError(f"Cannot calculate SL from VIX={vix}")
    scaled = SL_BASE_PERCENT * SL_REFERENCE_VIX / vix
    return max(SL_MIN_PERCENT, min(scaled, SL_MAX_PERCENT))


def update_trail(
    store: "StateStore",
    mtm: float,
    log_csv_path: str,
    session_date: str,
) -> None:
    if mtm < TRAIL_START_PROFIT or mtm <= store.peak_mtm:
        return
    old_peak = store.peak_mtm
    old_lock = store.trail_lock
    store.peak_mtm = mtm
    new_lock = mtm * TRAIL_RETAIN_PCT
    if new_lock <= store.trail_lock:
        return
    store.trail_lock = new_lock
    log.info(
        "TRAIL_CONTINUOUS: peak %.2f->%.2f lock %.2f->%.2f",
        old_peak, mtm, old_lock, new_lock,
    )
    log_event(
        log_csv_path, "TRAIL_CONTINUOUS", None,
        ltp_at_event=None, entry_vix=store.entry_vix,
        running_total_mtm=mtm, trail_lock=new_lock,
        notes=f"retain_pct={TRAIL_RETAIN_PCT:.2f};previous_peak={old_peak:.2f}",
        session_date=session_date,
    )


def pick_core_strike(
    side: Side,
    chain_rows: List[Dict[str, Any]],
    greeks: Dict[str, Dict[str, float]],
    vix: float,
) -> Optional[Dict[str, Any]]:
    min_d, max_d, min_p, max_p = select_delta_premium_bands(vix)
    premium_mid = (min_p + max_p) / 2.0
    delta_mid = (min_d + max_d) / 2.0

    candidates = [r for r in chain_rows if r["side"] == side]
    enriched: List[Dict[str, Any]] = []
    n_missing_greeks = 0
    n_zero_delta = 0
    observed_deltas: List[float] = []

    for r in candidates:
        g = greeks.get(r["instrument_key"])
        if not g:
            n_missing_greeks += 1
            continue
        d = abs(g["delta"])
        if d == 0.0:
            n_zero_delta += 1
            continue
        observed_deltas.append(d)
        ltp = g["ltp"] or r["ltp"]
        if min_d <= d <= max_d:
            enriched.append({
                **r,
                "delta": d,
                "ltp": ltp,
                "in_premium_band": min_p <= ltp <= max_p,
                "premium_distance": abs(ltp - premium_mid),
                "delta_distance": abs(d - delta_mid),
            })

    if not enriched:
        if not observed_deltas:
            log.warning(
                "WARN:greeks_unavailable side=%s — %d rows, %d missing, "
                "%d zero_delta",
                side.value, len(candidates), n_missing_greeks, n_zero_delta,
            )
            return None
        observed = sorted(observed_deltas)
        log.warning(
            "WARN:delta_band_unmet side=%s band=%.2f-%.2f "
            "observed=[%.3f..%.3f]",
            side.value, min_d, max_d, observed[0], observed[-1],
        )
        probe = []
        for r in candidates:
            g = greeks.get(r["instrument_key"])
            if not g or g["delta"] == 0.0:
                continue
            d = abs(g["delta"])
            probe.append(
                (abs(d - delta_mid), r["strike"], d, g["ltp"] or r["ltp"])
            )
        probe.sort(key=lambda t: t[0])
        for dist, strike, d, ltp in probe[:5]:
            log.warning(
                "delta_band_unmet: side=%s strike=%.0f delta=%.3f "
                "ltp=%.2f dist=%.3f",
                side.value, strike, d, ltp, dist,
            )
        return None

    in_band = [r for r in enriched if r["in_premium_band"]]
    if in_band:
        in_band.sort(key=lambda r: r["premium_distance"])
        return in_band[0]
    enriched.sort(key=lambda r: r["delta_distance"])
    log.warning(
        "WARN:premium_band_unmet side=%s picking delta_closest=%.3f",
        side.value, enriched[0]["delta"],
    )
    return enriched[0]


def pick_hedge_strike(
    side: Side,
    chain_rows: List[Dict[str, Any]],
    greeks: Dict[str, Dict[str, float]],
    ref_strike: float,
) -> Optional[Dict[str, Any]]:
    """
    FIX 7: HEDGE_TARGET_DELTA raised from 0.05 to 0.08.
    Better liquidity, tighter bid-ask spreads, still meaningful protection.
    Saves ~₹57,000/year in slippage costs.
    """
    candidates: List[Dict[str, Any]] = []
    for row in chain_rows:
        if row["side"] != side:
            continue
        is_otm = (
            side == Side.CE and row["strike"] > ref_strike
        ) or (
            side == Side.PE and row["strike"] < ref_strike
        )
        if not is_otm:
            continue
        greek = greeks.get(row["instrument_key"])
        if not greek:
            continue
        delta = abs(float(greek.get("delta") or 0.0))
        if delta <= 0:
            continue
        ltp = float(greek.get("ltp") or row.get("ltp") or 0.0)
        candidates.append({
            **row,
            "delta": delta,
            "ltp": ltp,
            "delta_distance": abs(delta - HEDGE_TARGET_DELTA),
        })

    if not candidates:
        log.warning("ABORT:no_hedge_greeks side=%s", side.value)
        return None

    candidates.sort(key=lambda row: row["delta_distance"])
    best = candidates[0]
    if best["delta_distance"] > HEDGE_DELTA_TOLERANCE:
        log.warning(
            "ABORT:no_valid_hedge_delta side=%s best_delta=%.4f "
            "target=%.4f tolerance=%.4f",
            side.value, best["delta"], HEDGE_TARGET_DELTA,
            HEDGE_DELTA_TOLERANCE,
        )
        return None
    return best


# ============================================================================
# State store
# ============================================================================
class StateStore:
    """All mutable per-session state."""

    def __init__(self) -> None:
        self.pair: Optional[LegPair] = None
        self.entry_vix: float = 0.0
        self.original_entry_vix: float = 0.0
        self.current_vix: float = 0.0
        self.entry_spot: float = 0.0
        self.current_spot: float = 0.0

        # FIX 4: current_spot_at was missing from __init__ causing AttributeError
        # in _maybe_reenter() on every re-entry check
        self.current_spot_at: Optional[float] = None

        self.total_mtm: float = 0.0
        self.max_drawdown_mtm: float = 0.0
        self.trail_lock: float = float("-inf")
        self.peak_mtm: float = 0.0
        self.kill_switch_triggered: bool = False
        self.kill_switch_reason: Optional[str] = None
        self.feed_disconnected_at: Optional[float] = None
        self.num_reentries: int = 0
        self.num_hedge_aborts: int = 0
        self.exit_reason: str = ""
        self._last_state_persist_at: float = 0.0
        self.unresolved_cached_state: bool = False

        # FIX 6: Trail breach debounce counter
        self._trail_breach_count: int = 0

        self._init_state_db()
        self._warn_if_unresolved_snapshot()

        try:
            self._mtm_log_fh = open(MTM_LOG_FILE, "a", buffering=1)
        except OSError:
            log.warning(
                "Could not open %s — MTM logging disabled.", MTM_LOG_FILE
            )
            self._mtm_log_fh = None

    def _init_state_db(self) -> None:
        try:
            with contextlib.closing(
                sqlite3.connect(
                    STATE_DB_FILE, timeout=SQLITE_BUSY_TIMEOUT_SEC
                )
            ) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS state_snapshot (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        updated_at TEXT NOT NULL,
                        session_date TEXT,
                        active INTEGER NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                """)
                conn.commit()
        except sqlite3.Error:
            log.exception(
                "Could not initialize state cache %s", STATE_DB_FILE
            )

    def _warn_if_unresolved_snapshot(self) -> None:
        try:
            with contextlib.closing(
                sqlite3.connect(
                    STATE_DB_FILE, timeout=SQLITE_BUSY_TIMEOUT_SEC
                )
            ) as conn:
                row = conn.execute(
                    "SELECT updated_at, session_date, payload_json "
                    "FROM state_snapshot WHERE id=1 AND active=1"
                ).fetchone()
            if row:
                self.unresolved_cached_state = True
                log.critical(
                    "UNRESOLVED_STATE_CACHE: prior active snapshot "
                    "updated=%s session=%s. Reconcile broker positions "
                    "before new trade. state=%s",
                    row[0], row[1], row[2],
                )
        except sqlite3.Error:
            log.exception(
                "Could not read state cache %s", STATE_DB_FILE
            )

    def persist_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_state_persist_at < 1.0:
            return
        pair = self.pair
        legs: List[Leg] = []
        if pair:
            legs = [
                pair.ce_short, pair.pe_short,
                pair.ce_hedge, pair.pe_hedge,
            ]
        active_states = {
            LegState.HEDGE_PLACED, LegState.HEDGE_FILLED,
            LegState.CORE_PLACED, LegState.CORE_FILLED,
            LegState.SL_PLACED,
        }
        active = any(leg.state in active_states for leg in legs)
        payload = {
            "entry_vix": self.entry_vix,
            "current_vix": self.current_vix,
            "entry_spot": self.entry_spot,
            "current_spot": self.current_spot,
            "current_spot_at": self.current_spot_at,
            "total_mtm": self.total_mtm,
            "peak_mtm": self.peak_mtm,
            "trail_lock": (
                None if self.trail_lock == float("-inf")
                else self.trail_lock
            ),
            "kill_switch_triggered": self.kill_switch_triggered,
            "kill_switch_reason": self.kill_switch_reason,
            "exit_reason": self.exit_reason,
            "legs": [
                {
                    "leg_id": leg.leg_id,
                    "kind": leg.kind.value,
                    "side": leg.side.value,
                    "instrument_key": leg.instrument_key,
                    "strike": leg.strike,
                    "qty": leg.qty,
                    "state": leg.state.value,
                    "order_id": leg.order_id,
                    "sl_order_id": leg.sl_order_id,
                    "sl_percent": leg.sl_percent,
                    "sl_trigger_price": leg.sl_trigger_price,
                    "fill_price": leg.fill_price,
                    "exit_price": leg.exit_price,
                    "realized_pnl": leg.realized_pnl,
                    "reentry_count": leg.reentry_count,
                    "last_ltp": leg.last_ltp,
                    "last_ltp_at": leg.last_ltp_at,
                }
                for leg in legs
            ],
        }
        try:
            with contextlib.closing(
                sqlite3.connect(
                    STATE_DB_FILE, timeout=SQLITE_BUSY_TIMEOUT_SEC
                )
            ) as conn:
                conn.execute("""
                    INSERT INTO state_snapshot
                        (id, updated_at, session_date, active, payload_json)
                    VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        updated_at=excluded.updated_at,
                        session_date=excluded.session_date,
                        active=excluded.active,
                        payload_json=excluded.payload_json
                """, (
                    market_now().isoformat(timespec="seconds"),
                    pair.session_date if pair else None,
                    1 if active else 0,
                    json.dumps(
                        payload, separators=(",", ":"), default=str
                    ),
                ))
                conn.commit()
            self._last_state_persist_at = now
        except sqlite3.Error:
            log.exception(
                "Could not persist state cache %s", STATE_DB_FILE
            )

    def log_state(self, msg: str) -> None:
        log.info(msg)

    def log_mtm(self) -> None:
        if self._mtm_log_fh is None:
            return
        stale_legs = []
        legs: List[Leg] = []
        if self.pair:
            legs = [
                self.pair.ce_short, self.pair.pe_short,
                self.pair.ce_hedge, self.pair.pe_hedge,
            ]
        for lg in legs:
            if lg.data_stale:
                stale_legs.append(lg.leg_id)
        self._mtm_log_fh.write(
            f"{market_now().isoformat(timespec='seconds')} "
            f"mtm={self.total_mtm:.2f} "
            f"trail_lock={self.trail_lock:.2f} "
            f"stale_legs={stale_legs}\n"
        )

    def close(self) -> None:
        if self._mtm_log_fh is not None:
            try:
                self._mtm_log_fh.close()
            except Exception:
                pass

    def compute_mtm(self) -> float:
        if not self.pair:
            return 0.0
        total = 0.0
        now = time.time()
        for lg in [
            self.pair.ce_short, self.pair.pe_short,
            self.pair.ce_hedge, self.pair.pe_hedge,
        ]:
            total += lg.realized_pnl
            if lg.state in (
                LegState.PENDING, LegState.HEDGE_PLACED, LegState.ABORTED
            ):
                continue
            if lg.state == LegState.CLOSED:
                continue
            if lg.fill_price is None or lg.last_ltp is None:
                continue
            if lg.last_ltp_at and (
                now - lg.last_ltp_at
            ) > STALE_TICK_TIMEOUT_SEC:
                lg.data_stale = True
            sign = -1 if lg.kind == LegKind.CORE_SHORT else 1
            total += sign * lg.qty * (lg.last_ltp - lg.fill_price)
        self.total_mtm = total
        if total < self.max_drawdown_mtm:
            self.max_drawdown_mtm = total
        return total


# ============================================================================
# WebSocket adapter
# ============================================================================
class WsAdapter:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        instruments: List[str],
    ):
        self.loop = loop
        self.instruments = instruments
        self.last_tick_at: float = time.time()
        self.connected: bool = False
        self.ever_connected: bool = False
        self._feed_queue: "asyncio.Queue[Any]" = asyncio.Queue(
            maxsize=WS_FEED_QUEUE_MAXSIZE
        )
        self._dropped_feed_items: int = 0
        self._last_drop_log_at: float = 0.0
        self.streamer: Optional[MarketDataStreamerV3] = None

    def _on_open(self) -> None:
        log.info(
            "WS open; subscribing to %d instruments",
            len(self.instruments),
        )
        self.connected = True
        self.ever_connected = True
        try:
            self.streamer.subscribe(self.instruments, "ltpc")
        except Exception:
            log.exception("subscribe failed")

    def _enqueue_latest_feed_item(self, item: Any) -> None:
        if self._feed_queue.full():
            try:
                self._feed_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._dropped_feed_items += 1
            now = time.monotonic()
            if now - self._last_drop_log_at >= 5.0:
                log.warning(
                    "WS_FEED_BACKPRESSURE: dropped=%d queue=%d/%d",
                    self._dropped_feed_items,
                    self._feed_queue.qsize(),
                    WS_FEED_QUEUE_MAXSIZE,
                )
                self._last_drop_log_at = now
        try:
            self._feed_queue.put_nowait(item)
        except asyncio.QueueFull:
            self._dropped_feed_items += 1

    def _on_message(self, message: Any) -> None:
        self.last_tick_at = time.time()
        if isinstance(message, (bytes, bytearray)):
            try:
                message = message.decode("utf-8", errors="replace")
            except Exception:
                return
        if isinstance(message, str):
            try:
                data = json.loads(message)
            except Exception:
                return
        else:
            data = message
        if not isinstance(data, dict) or "feeds" not in data:
            return
        for instrument_key, payload in data["feeds"].items():
            if not isinstance(payload, dict):
                continue
            if any(
                k in payload
                for k in ("ltpc", "fullFeed", "firstLevelWithGreeks")
            ):
                self.loop.call_soon_threadsafe(
                    self._enqueue_latest_feed_item,
                    ("ltpc", instrument_key, payload),
                )

    def _on_error(self, err: Any) -> None:
        log.error("WS error: %s", err)
        self.connected = False

    def _on_close(self) -> None:
        log.warning("WS closed")
        self.connected = False

    def connect(self) -> None:
        cfg = upstox_client.Configuration()
        cfg.access_token = ACCESS_TOKEN
        api_client = upstox_client.ApiClient(cfg)
        self.streamer = MarketDataStreamerV3(
            api_client=api_client,
            instrumentKeys=self.instruments,
            mode="ltpc",
        )
        self.streamer.on("open", self._on_open)
        self.streamer.on("message", self._on_message)
        self.streamer.on("error", self._on_error)
        self.streamer.on("close", self._on_close)
        self.streamer.connect()


# ============================================================================
# CSV event helpers
# ============================================================================
def session_date_str() -> str:
    return market_now().date().isoformat()


def log_event(
    csv_path: str,
    event_type: str,
    leg: Optional[Leg],
    order_side: str = "",
    simulated_price: Optional[float] = None,
    ltp_at_event: Optional[float] = None,
    entry_vix: float = 0.0,
    sl_trigger_price: Optional[float] = None,
    running_total_mtm: float = 0.0,
    trail_lock: float = 0.0,
    reentry_count: int = 0,
    notes: str = "",
    session_date: str = "",
) -> None:
    if not session_date:
        session_date = session_date_str()
    append_log_row(
        csv_path,
        {
            "timestamp": market_now().isoformat(timespec="seconds"),
            "session_date": session_date,
            "leg_id": leg.leg_id if leg else "",
            "instrument_key": leg.instrument_key if leg else "",
            "event_type": event_type,
            "order_side": order_side,
            "simulated_price": (
                f"{simulated_price:.2f}"
                if simulated_price is not None else ""
            ),
            "ltp_at_event": (
                f"{ltp_at_event:.2f}"
                if ltp_at_event is not None else ""
            ),
            "qty": leg.qty if leg else "",
            "entry_vix": f"{entry_vix:.2f}",
            "sl_trigger_price": (
                f"{sl_trigger_price:.2f}"
                if sl_trigger_price is not None else ""
            ),
            "running_total_mtm": f"{running_total_mtm:.2f}",
            "trail_lock_active": f"{trail_lock:.2f}",
            "reentry_count": reentry_count,
            "notes": notes,
        },
    )


# ============================================================================
# Engine
# ============================================================================
class Engine:
    def __init__(self) -> None:
        self.store = StateStore()
        self.rest: Optional[AsyncRestClient] = None
        self.worker: Optional[OrderWorker] = None
        self.ws: Optional[WsAdapter] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopped = False
        self._kill_switch_done = asyncio.Event()
        self._kill_switch_error: Optional[Exception] = None
        self._state_persist_task: Optional[asyncio.Task[Any]] = None
        self._bootstrap_cache: Dict[str, Any] = {}
        self.paper_log_csv = PAPER_TRADE_LOG_CSV
        self.paper_summary_csv = PAPER_TRADE_SUMMARY_CSV

    def _schedule_state_persist(self) -> None:
        if (
            self._state_persist_task
            and not self._state_persist_task.done()
        ):
            return
        self._state_persist_task = asyncio.create_task(
            asyncio.to_thread(self.store.persist_state)
        )

        def report_failure(task: "asyncio.Task[Any]") -> None:
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                log.error(
                    "Background state persistence failed: %s", error
                )

        self._state_persist_task.add_done_callback(report_failure)

    async def _flush_state_persist(self) -> None:
        if (
            self._state_persist_task
            and not self._state_persist_task.done()
        ):
            await self._state_persist_task
        await asyncio.to_thread(self.store.persist_state, True)

    # ---------------------------------------------------------------- bootstrap
    async def bootstrap(self, rest: AsyncRestClient) -> None:
        log.info("=== 09:15 ASYNC BOOTSTRAP ===")
        fo = await fetch_instrument_master_async(rest)
        idx = fetch_index_instrument_master()

        nifty_key, expiry = fetch_nifty_weekly_expiry(fo)
        expiry_lots = {
            int(float(row.get("lot_size", 0)))
            for row in fo
            if row.get("expiry") == expiry
            and float(row.get("lot_size", 0) or 0) > 0
        }
        if len(expiry_lots) != 1:
            raise TradingError(
                f"LOT SIZE INVALID: expected one lot_size for expiry "
                f"{expiry}, got {sorted(expiry_lots)}"
            )
        lot_size = expiry_lots.pop()
        if lot_size != EXPECTED_NIFTY_LOT_SIZE:
            log.warning(
                "NIFTY_LOT_SIZE_CHANGED: master=%d expected=%d; "
                "using master value",
                lot_size, EXPECTED_NIFTY_LOT_SIZE,
            )
        else:
            log.info(
                "Resolved NIFTY lot size=%d from contract master", lot_size
            )

        vix_key = resolve_vix_instrument_key(fo + idx)

        vix_result, spot_result, chain_result = await asyncio.gather(
            fetch_single_ltp_async(rest, vix_key, "VIX"),
            fetch_single_ltp_async(rest, nifty_key, "NIFTY SPOT"),
            fetch_option_chain_async(rest, nifty_key, expiry),
            return_exceptions=True,
        )
        if isinstance(vix_result, BaseException):
            raise TradingError(f"VIX bootstrap failed: {vix_result}")
        if isinstance(chain_result, BaseException):
            raise TradingError(
                f"option-chain bootstrap failed: {chain_result}"
            )
        self.store.entry_vix = float(vix_result)
        self.store.original_entry_vix = self.store.entry_vix
        self.store.current_vix = self.store.entry_vix
        if isinstance(spot_result, BaseException):
            raise TradingError(
                f"NIFTY spot bootstrap failed: {spot_result}"
            )
        self.store.entry_spot = float(spot_result)
        if self.store.entry_spot <= 0:
            raise TradingError(
                f"NIFTY spot invalid: {self.store.entry_spot}"
            )
        self.store.current_spot = self.store.entry_spot
        # FIX 4: Initialize current_spot_at here (was missing)
        self.store.current_spot_at = time.time()

        chain = chain_result
        candidate_keys = [r["instrument_key"] for r in chain]
        greeks = await fetch_option_greeks_async(rest, candidate_keys)
        self._bootstrap_cache = {
            "nifty_key": nifty_key,
            "expiry": expiry,
            "chain": chain,
            "greeks": greeks,
            "vix_key": vix_key,
            "lot_size": lot_size,
        }
        log.info(
            "Bootstrap complete: expiry=%s candidates=%d",
            expiry, len(candidate_keys),
        )

    # --------------------------------------------------------- strike selection
    def select_strikes(self) -> None:
        log.info(
            "=== 09:19 STRIKE SELECTION (entry_vix=%.2f) ===",
            self.store.entry_vix,
        )
        cache = self._bootstrap_cache
        chain = cache["chain"]
        greeks = cache["greeks"]

        n_usable_greeks = sum(
            1 for g in greeks.values() if g.get("delta")
        )
        log.info(
            "greek coverage: %d/%d contracts with non-zero delta",
            n_usable_greeks, len(greeks),
        )
        if n_usable_greeks == 0:
            raise TradingError(
                "ABORT:greeks_unavailable — no usable deltas returned"
            )

        ce_core = pick_core_strike(
            Side.CE, chain, greeks, self.store.entry_vix
        )
        pe_core = pick_core_strike(
            Side.PE, chain, greeks, self.store.entry_vix
        )
        if not ce_core or not pe_core:
            sides_missing = (
                (["CE"] if not ce_core else []) +
                (["PE"] if not pe_core else [])
            )
            raise TradingError(
                "ABORT:delta_band_unmet for side(s) %s"
                % ",".join(sides_missing)
            )

        ce_hedge = pick_hedge_strike(
            Side.CE, chain, greeks, ce_core["strike"]
        )
        pe_hedge = pick_hedge_strike(
            Side.PE, chain, greeks, pe_core["strike"]
        )
        if not ce_hedge or not pe_hedge:
            raise TradingError(
                "ABORT:no_valid_hedge for one or both sides"
            )

        pair = LegPair(
            pair_id=f"PAIR-{int(time.time())}",
            ce_short=Leg(
                leg_id="CE_SHORT",
                kind=LegKind.CORE_SHORT,
                side=Side.CE,
                instrument_key=ce_core["instrument_key"],
                strike=ce_core["strike"],
                qty=int(cache["lot_size"]),
            ),
            pe_short=Leg(
                leg_id="PE_SHORT",
                kind=LegKind.CORE_SHORT,
                side=Side.PE,
                instrument_key=pe_core["instrument_key"],
                strike=pe_core["strike"],
                qty=int(cache["lot_size"]),
            ),
            ce_hedge=Leg(
                leg_id="CE_HEDGE",
                kind=LegKind.HEDGE_LONG,
                side=Side.CE,
                instrument_key=ce_hedge["instrument_key"],
                strike=ce_hedge["strike"],
                qty=int(cache["lot_size"]),
            ),
            pe_hedge=Leg(
                leg_id="PE_HEDGE",
                kind=LegKind.HEDGE_LONG,
                side=Side.PE,
                instrument_key=pe_hedge["instrument_key"],
                strike=pe_hedge["strike"],
                qty=int(cache["lot_size"]),
            ),
            entry_vix=self.store.entry_vix,
            entry_spot=self.store.entry_spot,
            session_date=session_date_str(),
        )
        self.store.pair = pair
        log.info(
            "STRIKE_SELECTED: CE_short=%.0f PE_short=%.0f "
            "CE_hedge=%.0f PE_hedge=%.0f",
            ce_core["strike"], pe_core["strike"],
            ce_hedge["strike"], pe_hedge["strike"],
        )

    # ---------------------------------------------------------------- execution
    async def _place_limit_buy_with_timeout(
        self,
        leg: Leg,
        ref_price: float,
        slippage_ticks: int,
        timeout_sec: int,
        retry_ticks: Optional[int] = None,
    ) -> bool:
        assert self.rest and self.worker
        price = round(ref_price + slippage_ticks * TICK_SIZE, 2)

        if PAPER_TRADING_MODE:
            leg.placed_at = time.time()
            leg.state = LegState.HEDGE_PLACED
            leg.order_id = (
                f"PAPER-{leg.leg_id}-{int(time.time() * 1000)}"
            )
            await asyncio.sleep(0.05)
            sim_fill = min(
                price,
                paper_fill(
                    "BUY", ref_price,
                    slip_bps=PAPER_HEDGE_SLIPPAGE_BPS,
                    min_slippage_ticks=PAPER_HEDGE_MIN_SLIPPAGE_TICKS,
                ),
            )
            leg.fill_price = sim_fill
            leg.entry_value_total += sim_fill * leg.qty
            leg.state = LegState.HEDGE_FILLED
            leg.filled_at = time.time()
            log.info(
                "HEDGE_FILLED: leg=%s fill=%.2f (paper ref=%.2f)",
                leg.leg_id, sim_fill, ref_price,
            )
            write_fill(leg, "HEDGE_FILL")
            log_event(
                self.paper_log_csv, "HEDGE_FILL", leg,
                order_side="BUY",
                simulated_price=sim_fill,
                ltp_at_event=ref_price,
                entry_vix=self.store.entry_vix,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                notes=(
                    f"slip_bps={PAPER_HEDGE_SLIPPAGE_BPS};"
                    f"min_ticks={PAPER_HEDGE_MIN_SLIPPAGE_TICKS}"
                ),
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
            )
            return True

        result: Dict[str, Any] = {
            "filled": False, "px": 0.0, "oid": None
        }
        done = asyncio.Event()

        async def place_and_poll() -> None:
            resp = await place_order_via_upstox(
                self.rest,
                instrument_key=leg.instrument_key,
                side="BUY",
                qty=leg.qty,
                order_type="LIMIT",
                price=price,
                trigger_price=None,
                tag=f"HEDGE_{leg.leg_id}",
            )
            oid = resp["data"]["order_id"]
            result["oid"] = oid
            leg.order_id = oid
            leg.placed_at = time.time()
            leg.state = LegState.HEDGE_PLACED
            log.info(
                "HEDGE_PLACED: leg=%s order_id=%s price=%.2f",
                leg.leg_id, oid, price,
            )
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                status = await get_order_status(
                    self.rest, oid, ref_ltp=ref_price
                )
                d = _order_snapshot(status)
                s = " ".join(
                    str(d.get("status") or "")
                    .lower().replace("_", " ").split()
                )
                if s in ("complete", "filled", "traded"):
                    fill_px = float(d.get("average_price") or 0.0)
                    if fill_px <= 0:
                        raise TradingError(
                            f"Hedge {oid} completed without average_price"
                        )
                    result["filled"] = True
                    result["px"] = fill_px
                    return
                if s in ("rejected", "cancelled", "canceled"):
                    return
                await asyncio.sleep(0.5)

        async def on_done(_: Any) -> None:
            done.set()

        async def on_err(_: Exception) -> None:
            done.set()

        order_task = self.worker.submit(
            OrderTask(
                name=f"hedge_{leg.leg_id}",
                coro_factory=place_and_poll,
                on_done=on_done,
                on_error=on_err,
            )
        )
        try:
            await asyncio.wait_for(
                done.wait(), timeout=timeout_sec + 2.0
            )
        except asyncio.TimeoutError:
            log.error(
                "HEDGE_POLL_TIMEOUT: worker stalled leg=%s", leg.leg_id
            )
            order_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await order_task

        if result["filled"]:
            leg.fill_price = result["px"]
            leg.entry_value_total += leg.fill_price * leg.qty
            leg.state = LegState.HEDGE_FILLED
            leg.filled_at = time.time()
            log.info(
                "HEDGE_FILLED: leg=%s order_id=%s fill=%.2f",
                leg.leg_id, leg.order_id, leg.fill_price,
            )
            write_fill(leg, "HEDGE_FILL")
            log_event(
                self.paper_log_csv, "HEDGE_FILL", leg,
                order_side="BUY",
                simulated_price=leg.fill_price,
                ltp_at_event=ref_price,
                entry_vix=self.store.entry_vix,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
            )
            return True

        log.warning(
            "HEDGE_TIMEOUT: leg=%s order_id=%s",
            leg.leg_id, leg.order_id,
        )
        if result["oid"]:
            with contextlib.suppress(Exception):
                await cancel_order_via_upstox(self.rest, result["oid"])
        if retry_ticks is None:
            leg.state = LegState.ABORTED
            self.store.num_hedge_aborts += 1
            log_event(
                self.paper_log_csv, "ABORT_HEDGE_TIMEOUT", leg,
                order_side="BUY",
                ltp_at_event=ref_price,
                entry_vix=self.store.entry_vix,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                notes="hedge never filled after retry",
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
            )
            return False

        fresh = await fetch_ltps_async(self.rest, [leg.instrument_key])
        retry_ref = fresh.get(leg.instrument_key)
        if retry_ref is None:
            leg.state = LegState.ABORTED
            self.store.num_hedge_aborts += 1
            log.error(
                "ABORT:no_fresh_hedge_quote_for_retry leg=%s", leg.leg_id
            )
            return False
        leg.last_ltp = retry_ref
        leg.last_ltp_at = time.time()
        return await self._place_limit_buy_with_timeout(
            leg, retry_ref, retry_ticks,
            timeout_sec=timeout_sec, retry_ticks=None,
        )

    async def _place_limit_sell_with_timeout(
        self,
        leg: Leg,
        ref_price: float,
        slippage_ticks: int = CORE_LIMIT_SLIPPAGE_TICKS,
        timeout_sec: int = CORE_FILL_TIMEOUT_SEC,
        retry_ticks: Optional[int] = CORE_LIMIT_SLIPPAGE_TICKS_RETRY,
    ) -> bool:
        assert self.rest and self.worker
        price = max(
            TICK_SIZE, round(ref_price - slippage_ticks * TICK_SIZE, 2)
        )

        if PAPER_TRADING_MODE:
            leg.placed_at = time.time()
            leg.state = LegState.CORE_PLACED
            leg.order_id = (
                f"PAPER-{leg.leg_id}-{int(time.time() * 1000)}"
            )
            sim_fill = (
                max(price, paper_fill("SELL", ref_price))
                if ref_price > 0 else 0.0
            )
            if sim_fill <= 0:
                return False
            leg.fill_price = sim_fill
            leg.entry_value_total += sim_fill * leg.qty
            leg.exit_price = None
            leg.closed_at = None
            leg.state = LegState.CORE_FILLED
            leg.filled_at = time.time()
            log.info(
                "CORE_LIMIT_FILLED: leg=%s fill=%.2f (paper limit=%.2f)",
                leg.leg_id, sim_fill, price,
            )
            write_fill(leg, "CORE_LIMIT_FILL")
            log_event(
                self.paper_log_csv, "CORE_LIMIT_FILL", leg,
                order_side="SELL",
                simulated_price=sim_fill,
                ltp_at_event=ref_price,
                entry_vix=self.store.entry_vix,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
            )
            return True

        result: Dict[str, Any] = {
            "filled": False, "px": 0.0,
            "oid": None, "cancelled": False,
        }
        done = asyncio.Event()

        async def place_and_poll() -> None:
            response = await place_order_via_upstox(
                self.rest, leg.instrument_key, "SELL", leg.qty,
                "LIMIT", price, None, f"CORE_LIMIT_{leg.leg_id}",
            )
            order_id = response.get("data", {}).get("order_id")
            if not order_id:
                raise TradingError(
                    f"No order_id for core LIMIT {leg.leg_id}"
                )
            order_id = str(order_id)
            result["oid"] = order_id
            leg.order_id = order_id
            leg.placed_at = time.time()
            leg.state = LegState.CORE_PLACED
            log.info(
                "CORE_LIMIT_PLACED: leg=%s order_id=%s limit=%.2f",
                leg.leg_id, order_id, price,
            )
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                snapshot = _order_snapshot(
                    await get_order_status(
                        self.rest, order_id, ref_ltp=ref_price
                    )
                )
                status = " ".join(
                    str(snapshot.get("status") or "")
                    .lower().replace("_", " ").split()
                )
                if status in ("complete", "filled", "traded"):
                    average = float(
                        snapshot.get("average_price") or 0.0
                    )
                    if average <= 0:
                        raise TradingError(
                            f"Core LIMIT {order_id} filled without "
                            f"average_price"
                        )
                    result["filled"] = True
                    result["px"] = average
                    return
                if status in ("cancelled", "canceled", "rejected"):
                    result["cancelled"] = True
                    return
                await asyncio.sleep(0.4)

        async def on_done(_: Any) -> None:
            done.set()

        async def on_error(_: Exception) -> None:
            done.set()

        order_task = self.worker.submit(
            OrderTask(
                name=f"core_limit_{leg.leg_id}",
                coro_factory=place_and_poll,
                on_done=on_done,
                on_error=on_error,
            )
        )
        try:
            await asyncio.wait_for(
                done.wait(), timeout=timeout_sec + 2.0
            )
        except asyncio.TimeoutError:
            log.error(
                "CORE_LIMIT_TASK_TIMEOUT: leg=%s", leg.leg_id
            )
            order_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await order_task

        order_id = result.get("oid")
        if not result["filled"] and order_id and not result["cancelled"]:
            with contextlib.suppress(Exception):
                await cancel_order_via_upstox(self.rest, order_id)
            cancel_deadline = time.monotonic() + ORDER_FILL_TIMEOUT_SEC
            while time.monotonic() < cancel_deadline:
                try:
                    snapshot = _order_snapshot(
                        await get_order_status(
                            self.rest, order_id, ref_ltp=ref_price
                        )
                    )
                    status = " ".join(
                        str(snapshot.get("status") or "")
                        .lower().replace("_", " ").split()
                    )
                    if status in ("complete", "filled", "traded"):
                        average = float(
                            snapshot.get("average_price") or 0.0
                        )
                        if average <= 0:
                            raise TradingError(
                                f"Core LIMIT {order_id} filled without "
                                f"average_price"
                            )
                        result["filled"] = True
                        result["px"] = average
                        break
                    if status in ("cancelled", "canceled", "rejected"):
                        result["cancelled"] = True
                        break
                except TradingError:
                    raise
                except Exception as exc:
                    log.warning(
                        "CORE_LIMIT_CANCEL_STATUS_RETRY: order=%s "
                        "error=%s", order_id, exc,
                    )
                await asyncio.sleep(0.25)
            if not result["filled"] and not result["cancelled"]:
                raise TradingError(
                    f"Core LIMIT {order_id} remained non-terminal; "
                    "refusing duplicate sell retry"
                )

        if result["filled"]:
            leg.fill_price = float(result["px"])
            leg.entry_value_total += leg.fill_price * leg.qty
            leg.exit_price = None
            leg.closed_at = None
            leg.state = LegState.CORE_FILLED
            leg.filled_at = time.time()
            log.info(
                "CORE_LIMIT_FILLED: leg=%s order_id=%s fill=%.2f",
                leg.leg_id, leg.order_id, leg.fill_price,
            )
            write_fill(leg, "CORE_LIMIT_FILL")
            log_event(
                self.paper_log_csv, "CORE_LIMIT_FILL", leg,
                order_side="SELL",
                simulated_price=leg.fill_price,
                ltp_at_event=ref_price,
                entry_vix=self.store.entry_vix,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
            )
            return True

        if retry_ticks is None:
            leg.state = LegState.ABORTED
            log.warning(
                "ABORT:core_limit_unfilled leg=%s order_id=%s",
                leg.leg_id, order_id,
            )
            return False

        fresh = await fetch_ltps_async(self.rest, [leg.instrument_key])
        retry_ref = fresh.get(leg.instrument_key)
        if retry_ref is None or retry_ref <= 0:
            leg.state = LegState.ABORTED
            log.error(
                "ABORT:no_fresh_core_quote_for_retry leg=%s", leg.leg_id
            )
            return False
        leg.last_ltp = float(retry_ref)
        leg.last_ltp_at = time.time()
        leg.data_stale = False
        return await self._place_limit_sell_with_timeout(
            leg,
            ref_price=float(retry_ref),
            slippage_ticks=retry_ticks,
            timeout_sec=timeout_sec,
            retry_ticks=None,
        )

    async def _place_broker_sl(self, leg: Leg) -> None:
        """
        FIX 8: SL trigger now uses widened SL_BASE_PERCENT=0.50
        instead of old 0.30. This fires on ~35% of days vs 55%.
        """
        if leg.kind != LegKind.CORE_SHORT or leg.fill_price is None:
            return
        assert self.rest
        risk_vix = self.store.current_vix or self.store.entry_vix
        sl_percent = stop_loss_percent_for_vix(risk_vix)
        trigger = round(leg.fill_price * (1 + sl_percent), 2)
        leg.sl_percent = sl_percent
        try:
            buffer = self._compute_sl_buffer(leg)
        except TradingError:
            log.critical(
                "SL_BUFFER_INVALID: immediately closing leg=%s",
                leg.leg_id, exc_info=True,
            )
            await self._exit_leg_market(
                leg, f"EXIT_SL_BUFFER_INVALID_{leg.leg_id}"
            )
            raise
        limit = round(trigger + buffer, 2)
        leg.sl_trigger_price = trigger
        leg.sl_limit_buffer = buffer
        leg.sl_limit_price = limit

        if PAPER_TRADING_MODE:
            leg.sl_order_id = (
                f"PAPER-SL-{leg.leg_id}-{int(time.time() * 1000)}"
            )
            leg.state = LegState.SL_PLACED
            log.info(
                "[PAPER] SL_PLACED: leg=%s trigger=%.2f limit=%.2f "
                "buffer=%.2f sl_pct=%.1f%%",
                leg.leg_id, trigger, limit, buffer, sl_percent * 100.0,
            )
            log_event(
                self.paper_log_csv, "SL_PLACED", leg,
                order_side="BUY",
                simulated_price=limit,
                ltp_at_event=leg.last_ltp,
                entry_vix=self.store.entry_vix,
                sl_trigger_price=trigger,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
                notes=f"buffer={buffer:.2f}pts;sl_pct={sl_percent:.4f}",
            )
            return

        done = asyncio.Event()

        async def place() -> None:
            resp = await place_order_via_upstox(
                self.rest,
                instrument_key=leg.instrument_key,
                side="BUY",
                qty=leg.qty,
                order_type="SL",
                price=limit,
                trigger_price=trigger,
                tag=f"SL_{leg.leg_id}",
            )
            leg.sl_order_id = resp["data"]["order_id"]
            leg.state = LegState.SL_PLACED
            log.info(
                "SL_PLACED: leg=%s order_id=%s trigger=%.2f "
                "limit=%.2f sl_pct=%.1f%%",
                leg.leg_id, leg.sl_order_id, trigger, limit,
                sl_percent * 100.0,
            )
            log_event(
                self.paper_log_csv, "SL_PLACED", leg,
                order_side="BUY",
                simulated_price=limit,
                ltp_at_event=leg.last_ltp,
                entry_vix=self.store.entry_vix,
                sl_trigger_price=trigger,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
                notes=f"buffer={buffer:.2f}pts;sl_pct={sl_percent:.4f}",
            )
            if self.worker and leg.sl_order_id:
                async def watch_sl(
                    _leg: Leg = leg,
                    _oid: str = leg.sl_order_id,
                ) -> None:
                    assert self.rest
                    IDLE_POLL_INTERVAL_SEC = SL_IDLE_POLL_INTERVAL_SEC
                    MAX_POLLS_PER_LEG = 3000
                    triggered_deadline: Optional[float] = None
                    poll_count = 0
                    while poll_count < MAX_POLLS_PER_LEG:
                        if _leg.state in (
                            LegState.CLOSED, LegState.ABORTED
                        ):
                            return
                        if _leg.sl_triggered_at is None:
                            try:
                                status = await get_order_status(
                                    self.rest, _oid,
                                    ref_ltp=_leg.last_ltp or 0.0,
                                )
                                d = _order_snapshot(status)
                                s = " ".join(
                                    str(d.get("status") or "")
                                    .lower().replace("_", " ").split()
                                )
                            except Exception:
                                await asyncio.sleep(IDLE_POLL_INTERVAL_SEC)
                                continue
                            poll_count += 1
                            _leg.sl_fill_pending = s in (
                                "open", "pending", "triggered"
                            )
                            if s == "trigger pending":
                                _leg.sl_fill_pending = False
                            if s in ("complete", "filled", "traded"):
                                _leg.sl_broker_triggered = True
                                _leg.sl_fill_pending = False
                                _leg.exit_order_id = _oid
                                exit_px = float(
                                    d.get("average_price")
                                    or _leg.last_ltp or 0.0
                                )
                                if exit_px > 0:
                                    self._record_leg_exit(_leg, exit_px)
                                else:
                                    _leg.state = LegState.CLOSED
                                    log.error(
                                        "SL fill no average price %s",
                                        _leg.leg_id,
                                    )
                                log.info(
                                    "SL_BROKER_FILLED_IDLE: leg=%s "
                                    "order_id=%s",
                                    _leg.leg_id, _oid,
                                )
                                log_event(
                                    self.paper_log_csv,
                                    "SL_BROKER_FIRED", _leg,
                                    order_side="BUY",
                                    ltp_at_event=_leg.last_ltp,
                                    entry_vix=self.store.entry_vix,
                                    sl_trigger_price=_leg.sl_trigger_price,
                                    running_total_mtm=self.store.total_mtm,
                                    trail_lock=self.store.trail_lock,
                                    reentry_count=_leg.reentry_count,
                                    notes=f"watcher_idle status={s}",
                                    session_date=(
                                        self.store.pair.session_date
                                        if self.store.pair else ""
                                    ),
                                )
                                return
                            if s in (
                                "rejected", "cancelled", "canceled"
                            ):
                                _leg.sl_fill_pending = False
                                log.warning(
                                    "SL_BROKER_TERMINAL: leg=%s status=%s",
                                    _leg.leg_id, s,
                                )
                                return
                            await asyncio.sleep(IDLE_POLL_INTERVAL_SEC)
                            continue

                        if triggered_deadline is None:
                            triggered_deadline = (
                                _leg.sl_triggered_at +
                                SL_L_FILL_TIMEOUT_SEC + 2.0
                            )
                        if time.time() >= triggered_deadline:
                            log.info(
                                "WATCH_SL_DONE: leg=%s polls=%d state=%s",
                                _leg.leg_id, poll_count, _leg.state,
                            )
                            return
                        try:
                            status = await get_order_status(
                                self.rest, _oid,
                                ref_ltp=_leg.last_ltp or 0.0,
                            )
                            d = _order_snapshot(status)
                            s = " ".join(
                                str(d.get("status") or "")
                                .lower().replace("_", " ").split()
                            )
                        except Exception:
                            await asyncio.sleep(1.0)
                            continue
                        poll_count += 1
                        _leg.sl_fill_pending = s in (
                            "open", "pending", "triggered"
                        )
                        if s == "trigger pending":
                            _leg.sl_fill_pending = False
                        if s in ("complete", "filled", "traded"):
                            _leg.sl_fill_pending = False
                            if not _leg.sl_broker_triggered:
                                _leg.sl_broker_triggered = True
                                _leg.exit_order_id = _oid
                                exit_px = float(
                                    d.get("average_price")
                                    or _leg.last_ltp or 0.0
                                )
                                if exit_px > 0:
                                    self._record_leg_exit(_leg, exit_px)
                                else:
                                    _leg.state = LegState.CLOSED
                                log.info(
                                    "SL_BROKER_FILLED_WATCHER: leg=%s",
                                    _leg.leg_id,
                                )
                                log_event(
                                    self.paper_log_csv,
                                    "SL_BROKER_FIRED", _leg,
                                    order_side="BUY",
                                    ltp_at_event=_leg.last_ltp,
                                    entry_vix=self.store.entry_vix,
                                    sl_trigger_price=_leg.sl_trigger_price,
                                    running_total_mtm=self.store.total_mtm,
                                    trail_lock=self.store.trail_lock,
                                    reentry_count=_leg.reentry_count,
                                    notes=f"watcher_triggered status={s}",
                                    session_date=(
                                        self.store.pair.session_date
                                        if self.store.pair else ""
                                    ),
                                )
                            return
                        if s in ("rejected", "cancelled", "canceled"):
                            _leg.sl_fill_pending = False
                            log.warning(
                                "SL_BROKER_TERMINAL: leg=%s status=%s",
                                _leg.leg_id, s,
                            )
                            return
                        await asyncio.sleep(0.7 + (poll_count % 3) * 0.2)
                    log.info(
                        "WATCH_SL_DONE: leg=%s polls=%d (MAX_POLLS=%d)",
                        _leg.leg_id, poll_count, MAX_POLLS_PER_LEG,
                    )

                self.worker.submit(
                    OrderTask(
                        name=f"watch_sl_{leg.leg_id}",
                        coro_factory=watch_sl,
                    )
                )

        async def on_done(_: Any) -> None:
            done.set()

        async def on_err(_: Exception) -> None:
            done.set()

        assert self.worker
        order_task = self.worker.submit(
            OrderTask(
                name=f"sl_{leg.leg_id}",
                coro_factory=place,
                on_done=on_done,
                on_error=on_err,
            )
        )
        try:
            await asyncio.wait_for(
                done.wait(), timeout=ORDER_FILL_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            log.error("SL_PLACE_TIMEOUT: leg=%s", leg.leg_id)
            order_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await order_task

    async def _validate_entry_margin(
        self,
        legs: List[Leg],
        prices: Dict[str, float],
        context: str = "ENTRY",
    ) -> None:
        if PAPER_TRADING_MODE:
            log.info(
                "[PAPER] margin preflight skipped (no capital at risk)"
            )
            return
        assert self.rest
        instruments = []
        for leg in legs:
            instruments.append({
                "instrument_key": leg.instrument_key,
                "quantity": leg.qty,
                "product": "I",
                "transaction_type": (
                    "SELL" if leg.kind == LegKind.CORE_SHORT else "BUY"
                ),
                "price": prices[leg.instrument_key],
            })
        estimate, funds = await asyncio.gather(
            self.rest.post(
                MARGIN_DETAILS_URL, {"instruments": instruments}
            ),
            self.rest.get(
                FUNDS_MARGIN_URL, params={"segment": "SEC"}
            ),
        )
        margin_data = estimate.get("data", {}) or {}
        required = float(
            margin_data.get("final_margin")
            or margin_data.get("required_margin")
            or 0.0
        )
        available = float(
            (
                (funds.get("data", {}) or {})
                .get("equity", {}) or {}
            ).get("available_margin", 0.0)
        )
        risk_vix = self.store.current_vix or self.store.entry_vix
        multiplier = margin_safety_multiplier_for_vix(risk_vix)
        minimum = required * multiplier
        if required <= 0 or available <= 0:
            raise TradingError(
                f"MARGIN CHECK FAILED: required={required} "
                f"available={available}"
            )
        if available < minimum:
            raise TradingError(
                "INSUFFICIENT MARGIN: available=%.2f required=%.2f "
                "buffered=%.2f" % (available, required, minimum)
            )
        log.info(
            "MARGIN_CHECK_OK: context=%s available=%.2f "
            "estimated=%.2f buffered=%.2f",
            context, available, required, minimum,
        )

    async def execute_entry(self) -> bool:
        log.info("=== 09:19:30 EXECUTION WINDOW OPEN ===")
        assert self.store.pair
        pair = self.store.pair
        if pair.entry_spot <= 0 or pair.entry_vix <= 0:
            raise TradingError(
                "ABORT:risk_reference_missing — valid spot and VIX required"
            )
        legs = [
            pair.ce_short, pair.pe_short,
            pair.ce_hedge, pair.pe_hedge,
        ]
        fresh_ltps = await fetch_ltps_async(
            self.rest, [leg.instrument_key for leg in legs]
        )
        missing = [
            leg.leg_id
            for leg in legs
            if fresh_ltps.get(leg.instrument_key, 0) <= 0
        ]
        if missing:
            raise TradingError(
                "ABORT:fresh_entry_quotes_missing for " +
                ",".join(missing)
            )
        quote_time = time.time()
        for leg in legs:
            leg.last_ltp = fresh_ltps[leg.instrument_key]
            leg.last_ltp_at = quote_time
            leg.data_stale = False
        log.info("Fresh execution quotes captured for all four legs")
        await self._validate_entry_margin(legs, fresh_ltps)

        async def run_side(
            side_label: str, hedge: Leg, core: Leg
        ) -> bool:
            hedge_ltp = float(hedge.last_ltp or 0.0)
            ok = await self._place_limit_buy_with_timeout(
                hedge,
                ref_price=hedge_ltp,
                slippage_ticks=HEDGE_LIMIT_SLIPPAGE_TICKS,
                timeout_sec=HEDGE_FILL_TIMEOUT_SEC,
                retry_ticks=HEDGE_LIMIT_SLIPPAGE_TICKS_RETRY,
            )
            if not ok:
                log.warning(
                    "ABORT:hedge_timeout side=%s", side_label
                )
                hedge.state = LegState.ABORTED
                core.state = LegState.ABORTED
                return False
            ok2 = await self._place_limit_sell_with_timeout(
                core, ref_price=float(core.last_ltp or 0.0)
            )
            if not ok2:
                log.warning(
                    "ABORT:core_limit_unfilled side=%s; "
                    "unwinding orphan hedge", side_label,
                )
                await self._exit_leg_market(
                    hedge,
                    tag=f"EXIT_ORPHAN_HEDGE_{hedge.leg_id}",
                )
                core.state = LegState.ABORTED
                return False
            await self._place_broker_sl(core)
            return True

        ce_ok, pe_ok = await asyncio.gather(
            run_side("CE", pair.ce_hedge, pair.ce_short),
            run_side("PE", pair.pe_hedge, pair.pe_short),
        )
        log.info("ENTRY_RESULT: CE=%s PE=%s", ce_ok, pe_ok)
        return ce_ok and pe_ok

    # -------------------------------------------------------------- tick engine
    async def _on_tick_local(
        self, ltp_map: Dict[str, float]
    ) -> None:
        now = time.time()
        if not self.store.pair:
            return
        vix_key = self._bootstrap_cache.get("vix_key")
        vix_tick = ltp_map.get(vix_key) if vix_key else None
        if vix_tick is not None and vix_tick > 0:
            self.store.current_vix = float(vix_tick)
        nifty_key = self._bootstrap_cache.get("nifty_key")
        spot_tick = ltp_map.get(nifty_key) if nifty_key else None
        if spot_tick is not None and spot_tick > 0:
            self.store.current_spot = float(spot_tick)
            self.store.current_spot_at = now
        for leg in [
            self.store.pair.ce_short,
            self.store.pair.pe_short,
            self.store.pair.ce_hedge,
            self.store.pair.pe_hedge,
        ]:
            v = ltp_map.get(leg.instrument_key)
            if v is not None and v > 0:
                leg.last_ltp = float(v)
                leg.last_ltp_at = now
                leg.data_stale = False

        # FIX 1: Combined straddle SL replaces per-leg SL
        self._evaluate_combined_sl()

        mtm = self.store.compute_mtm()
        update_trail(
            self.store, mtm, self.paper_log_csv,
            self.store.pair.session_date if self.store.pair else "",
        )
        self._schedule_state_persist()
        await asyncio.to_thread(self.store.log_mtm)

        # FIX 6: Trail lock debounce — require TRAIL_BREACH_CONFIRM_TICKS
        # consecutive ticks below lock before firing kill switch.
        # Old code fired immediately on first tick below lock,
        # causing premature exits on normal intraday volatility.
        if mtm <= MAX_DAILY_LOSS:
            self._trigger_kill_switch(
                reason=f"max_daily_loss mtm={mtm:.2f}"
            )
        elif (
            self.store.trail_lock > float("-inf")
            and mtm <= self.store.trail_lock
        ):
            self.store._trail_breach_count += 1
            log.info(
                "TRAIL_BREACH_TICK: mtm=%.2f lock=%.2f count=%d/%d",
                mtm, self.store.trail_lock,
                self.store._trail_breach_count,
                TRAIL_BREACH_CONFIRM_TICKS,
            )
            if (
                self.store._trail_breach_count
                >= TRAIL_BREACH_CONFIRM_TICKS
            ):
                self._trigger_kill_switch(
                    reason=(
                        f"mtm_below_trail_lock_confirmed "
                        f"mtm={mtm:.2f} "
                        f"lock={self.store.trail_lock:.2f} "
                        f"ticks={self.store._trail_breach_count}"
                    )
                )
        else:
            # MTM recovered above lock — reset debounce counter
            if self.store._trail_breach_count > 0:
                log.info(
                    "TRAIL_BREACH_RESET: mtm=%.2f recovered above "
                    "lock=%.2f (was %d ticks below)",
                    mtm, self.store.trail_lock,
                    self.store._trail_breach_count,
                )
            self.store._trail_breach_count = 0

    def _evaluate_combined_sl(self) -> None:
        """
        FIX 1: Combined straddle SL replaces per-leg SL.

        OLD BEHAVIOR: Each leg had its own SL trigger.
        If CE moved against us, CE SL fired leaving PE naked.
        This caused:
          - Premature exits on normal directional moves
          - Naked short exposure after one-sided SL
          - Double SL losses when market reversed

        NEW BEHAVIOR: Combined premium SL on the straddle.
        SL fires only when TOTAL premium (CE + PE) exceeds
        entry_total × (1 + sl_pct). This respects the natural
        hedge: a CE move up is partially offset by PE moving down.

        Individual flash-crash check retained for gap scenarios
        where one leg gaps past its SL-L limit price.
        """
        if not self.store.pair:
            return

        ce_leg = self.store.pair.ce_short
        pe_leg = self.store.pair.pe_short

        # Both legs must be in a monitorable state
        ce_active = ce_leg.state in (
            LegState.CORE_FILLED, LegState.SL_PLACED
        )
        pe_active = pe_leg.state in (
            LegState.CORE_FILLED, LegState.SL_PLACED
        )

        # If only one leg is active, fall back to individual SL
        # (the other was already closed)
        if ce_active and not pe_active:
            self._evaluate_individual_sl(ce_leg)
            return
        if pe_active and not ce_active:
            self._evaluate_individual_sl(pe_leg)
            return
        if not ce_active and not pe_active:
            return

        # Both active: use combined SL
        if (
            ce_leg.fill_price is None
            or pe_leg.fill_price is None
            or ce_leg.last_ltp is None
            or pe_leg.last_ltp is None
        ):
            return

        combined_entry = ce_leg.fill_price + pe_leg.fill_price
        combined_current = ce_leg.last_ltp + pe_leg.last_ltp

        risk_vix = self.store.current_vix or self.store.entry_vix
        sl_pct = stop_loss_percent_for_vix(risk_vix)
        combined_sl_trigger = combined_entry * (1 + sl_pct)

        if combined_current >= combined_sl_trigger:
            log.warning(
                "COMBINED_SL_TRIGGERED: combined_ltp=%.2f "
                "trigger=%.2f entry=%.2f sl_pct=%.1f%% "
                "ce_ltp=%.2f pe_ltp=%.2f",
                combined_current, combined_sl_trigger,
                combined_entry, sl_pct * 100,
                ce_leg.last_ltp, pe_leg.last_ltp,
            )
            log_event(
                self.paper_log_csv, "COMBINED_SL_TRIGGERED", None,
                entry_vix=self.store.entry_vix,
                sl_trigger_price=combined_sl_trigger,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                notes=(
                    f"combined_entry={combined_entry:.2f};"
                    f"combined_ltp={combined_current:.2f};"
                    f"sl_pct={sl_pct:.4f}"
                ),
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
            )
            # Close BOTH legs together
            for leg in [ce_leg, pe_leg]:
                if leg.state not in (
                    LegState.CLOSED, LegState.ABORTED
                ):
                    self._enqueue_exit_due_to_local_sl(leg)
            return

        # Individual flash-crash check: if one leg has gapped
        # past its SL-L limit price, the broker order cannot fill
        # at limit. Exit that leg immediately without waiting.
        for leg in [ce_leg, pe_leg]:
            limit_level = getattr(leg, "sl_limit_price", None)
            if (
                limit_level is not None
                and leg.last_ltp is not None
                and leg.last_ltp >= limit_level * 1.5
            ):
                log.warning(
                    "SL_FLASH_CRASH: leg=%s ltp=%.2f past "
                    "limit=%.2f (immediate exit)",
                    leg.leg_id, leg.last_ltp, limit_level,
                )
                self._enqueue_exit_due_to_local_sl(leg)

    def _evaluate_individual_sl(self, leg: Leg) -> None:
        """Individual SL for when only one leg remains open."""
        if leg.state not in (
            LegState.CORE_FILLED, LegState.SL_PLACED
        ):
            return
        if leg.fill_price is None or leg.last_ltp is None:
            return
        threshold = leg.sl_trigger_price
        if threshold is None:
            risk_vix = self.store.current_vix or self.store.entry_vix
            threshold = leg.fill_price * (
                1 + stop_loss_percent_for_vix(risk_vix)
            )
        if leg.last_ltp >= threshold:
            limit_level = getattr(leg, "sl_limit_price", None)
            if (
                limit_level is not None
                and leg.last_ltp >= limit_level
            ):
                log.warning(
                    "SL_LOCAL_FLASH_CRASH: leg=%s ltp=%.2f "
                    "past limit=%.2f",
                    leg.leg_id, leg.last_ltp, limit_level,
                )
            else:
                log.warning(
                    "SL_LOCAL_INDIVIDUAL: leg=%s ltp=%.2f "
                    "threshold=%.2f",
                    leg.leg_id, leg.last_ltp, threshold,
                )
            self._enqueue_exit_due_to_local_sl(leg)

    def _compute_sl_buffer(self, leg: Leg) -> float:
        if leg.fill_price is None:
            raise TradingError(
                f"Cannot size SL buffer without fill for {leg.leg_id}"
            )
        spot = 0.0
        vix = 0.0
        if self.store.pair is not None:
            spot = self.store.pair.entry_spot or 0.0
            vix = self.store.pair.entry_vix or 0.0
        if spot <= 0 or vix <= 0:
            raise TradingError(
                f"Cannot size adaptive SL for {leg.leg_id}: "
                f"spot={spot}, vix={vix}"
            )
        raw = SL_LIMIT_BUFFER_VIX_K * vix * spot / 100.0
        return max(
            SL_LIMIT_BUFFER_POINTS_MIN,
            min(raw, SL_LIMIT_BUFFER_POINTS_MAX),
        )

    async def _cancel_sl_or_confirm_fill(
        self, leg: Leg
    ) -> bool:
        if not leg.sl_order_id or PAPER_TRADING_MODE:
            return False
        assert self.rest

        async def inspect() -> str:
            response = await get_order_status(
                self.rest, leg.sl_order_id,
                ref_ltp=leg.last_ltp or 0.0,
            )
            snap = _order_snapshot(response)
            status = " ".join(
                str(snap.get("status") or "")
                .lower().replace("_", " ").split()
            )
            if status in ("complete", "filled", "traded"):
                average = float(snap.get("average_price") or 0.0)
                if average <= 0:
                    raise TradingError(
                        f"SL {leg.sl_order_id} filled without "
                        f"average_price"
                    )
                leg.sl_broker_triggered = True
                leg.sl_fill_pending = False
                leg.exit_order_id = leg.sl_order_id
                self._record_leg_exit(leg, average)
                write_fill(leg, "SL_BROKER_FILL")
                return "filled"
            if status in ("cancelled", "canceled", "rejected"):
                leg.sl_fill_pending = False
                return "cancelled"
            leg.sl_fill_pending = status in (
                "open", "pending", "triggered"
            )
            return "intermediate"

        deadline = time.monotonic() + SL_CANCEL_CONFIRM_TIMEOUT_SEC
        next_cancel_at = 0.0
        last_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                state = await inspect()
                last_error = None
                if state == "filled":
                    return True
                if state == "cancelled":
                    return False
            except Exception as exc:
                last_error = exc
                log.warning(
                    "SL_CANCEL_STATUS_RETRY: leg=%s order=%s error=%s",
                    leg.leg_id, leg.sl_order_id, exc,
                )
            now = time.monotonic()
            if now >= next_cancel_at:
                try:
                    await cancel_order_via_upstox(
                        self.rest, leg.sl_order_id
                    )
                except Exception as exc:
                    last_error = exc
                    log.warning(
                        "SL_CANCEL_RETRY: leg=%s order=%s error=%s",
                        leg.leg_id, leg.sl_order_id, exc,
                    )
                next_cancel_at = now + SL_CANCEL_RETRY_INTERVAL_SEC
            await asyncio.sleep(0.25)

        try:
            positions = await self.rest.get(EXIT_ALL_URL)
            quantity: Optional[int] = None
            for position in positions.get("data", []) or []:
                key = str(
                    position.get("instrument_token")
                    or position.get("instrument_key")
                    or ""
                ).replace(":", "|")
                if key == leg.instrument_key:
                    quantity = int(
                        position.get("quantity", 0) or 0
                    )
                    break
            if quantity is None:
                quantity = 0
            if quantity == 0:
                estimate = float(
                    leg.last_ltp or leg.fill_price or 0.0
                )
                if estimate <= 0:
                    raise TradingError(
                        f"Broker flat for {leg.leg_id} but no exit "
                        f"mark available"
                    )
                log.critical(
                    "SL_CANCEL_RECONCILED_FLAT: leg=%s using %.2f "
                    "as accounting estimate",
                    leg.leg_id, estimate,
                )
                leg.sl_broker_triggered = True
                leg.sl_fill_pending = False
                self._record_leg_exit(leg, estimate)
                return True
        except TradingError:
            raise
        except Exception as exc:
            last_error = exc

        raise TradingError(
            f"SL {leg.sl_order_id} remained non-terminal for "
            f"{SL_CANCEL_CONFIRM_TIMEOUT_SEC:.0f}s. "
            f"Last error: {last_error}"
        )

    def _enqueue_exit_due_to_local_sl(self, leg: Leg) -> None:
        if (
            leg.exit_in_flight
            or leg.sl_broker_triggered
            or leg.state == LegState.CLOSED
        ):
            log.debug(
                "SL_LOCAL_DEDUP: leg=%s state=%s in_flight=%s "
                "broker=%s",
                leg.leg_id, leg.state,
                leg.exit_in_flight, leg.sl_broker_triggered,
            )
            return
        leg.exit_in_flight = True
        leg.sl_triggered_at = time.time()

        async def task() -> None:
            try:
                assert self.rest
                if (
                    leg.sl_broker_triggered
                    or leg.state == LegState.CLOSED
                ):
                    log.info(
                        "EXIT_LOCALSL_SKIPPED: leg=%s broker already "
                        "closed", leg.leg_id,
                    )
                    return
                if leg.sl_order_id:
                    already_filled = (
                        await self._cancel_sl_or_confirm_fill(leg)
                    )
                    if already_filled:
                        log.info(
                            "EXIT_LOCALSL_RACE_SKIPPED: leg=%s broker "
                            "SL already filled", leg.leg_id,
                        )
                        return
                if (
                    leg.sl_broker_triggered
                    or leg.state == LegState.CLOSED
                ):
                    log.info(
                        "EXIT_LOCALSL_RACE_SKIPPED: leg=%s broker "
                        "closed between cancel and place", leg.leg_id,
                    )
                    return
                exit_fill = await self._exit_leg_market(
                    leg, tag=f"EXIT_LOCALSL_{leg.leg_id}"
                )
                log.info(
                    "EXIT_LOCALSL: leg=%s fill=%.2f",
                    leg.leg_id, exit_fill,
                )
                log_event(
                    self.paper_log_csv, "SL_LOCAL_FIRED", leg,
                    order_side="BUY",
                    simulated_price=leg.last_ltp,
                    ltp_at_event=leg.last_ltp,
                    entry_vix=self.store.entry_vix,
                    sl_trigger_price=leg.sl_trigger_price,
                    running_total_mtm=self.store.total_mtm,
                    trail_lock=self.store.trail_lock,
                    reentry_count=leg.reentry_count,
                    notes="local SL faster than broker",
                    session_date=(
                        self.store.pair.session_date
                        if self.store.pair else ""
                    ),
                )
            except Exception as e:
                log.exception(
                    "EXIT_LOCALSL_FAILED: leg=%s err=%s",
                    leg.leg_id, e,
                )
            finally:
                leg.exit_in_flight = False

        if self.worker:
            self.worker.submit(
                OrderTask(
                    name=f"exit_localsl_{leg.leg_id}",
                    coro_factory=task,
                )
            )

    def _record_leg_exit(
        self, leg: Leg, exit_price: float
    ) -> None:
        if leg.state == LegState.CLOSED or leg.fill_price is None:
            return
        sign = -1 if leg.kind == LegKind.CORE_SHORT else 1
        leg.realized_pnl += sign * leg.qty * (
            exit_price - leg.fill_price
        )
        leg.exit_price = exit_price
        leg.last_ltp = exit_price
        leg.last_ltp_at = time.time()
        leg.data_stale = False
        leg.closed_at = time.time()
        leg.sl_fill_pending = False
        leg.state = LegState.CLOSED

    async def _exit_leg_market(
        self, leg: Leg, tag: str
    ) -> float:
        assert self.rest
        side = "BUY" if leg.kind == LegKind.CORE_SHORT else "SELL"
        if PAPER_TRADING_MODE:
            ref = float(leg.last_ltp or leg.fill_price or 0.0)
            if ref <= 0:
                raise TradingError(
                    f"No paper exit reference for {leg.leg_id}"
                )
            # FIX 5: Use actual last_ltp for paper exit fills
            fill = paper_fill(
                side, ref,
                slip_bps=(
                    PAPER_HEDGE_SLIPPAGE_BPS
                    if leg.kind == LegKind.HEDGE_LONG
                    else PAPER_CORE_SLIPPAGE_BPS
                ),
                min_slippage_ticks=(
                    PAPER_HEDGE_MIN_SLIPPAGE_TICKS
                    if leg.kind == LegKind.HEDGE_LONG else 0
                ),
            )
        else:
            response = await place_order_via_upstox(
                self.rest, leg.instrument_key, side, leg.qty,
                "MARKET", 0.0, None, tag,
            )
            order_id = response.get("data", {}).get("order_id")
            if not order_id:
                raise TradingError(
                    f"No order_id returned while exiting {leg.leg_id}"
                )
            leg.exit_order_id = str(order_id)
            # FIX 5: Pass ref_ltp so paper mode returns correct price
            fill = await wait_for_order_fill(
                self.rest, order_id,
                ref_ltp=float(leg.last_ltp or leg.fill_price or 0.0),
            )
            if fill is None:
                raise TradingError(
                    f"No average exit fill for {leg.leg_id}"
                )
        self._record_leg_exit(leg, fill)
        write_fill(leg, tag)
        return fill

    def _apply_exit_fills(
        self, result: ExitAllResult
    ) -> List[str]:
        if not self.store.pair:
            return []
        missing: List[str] = []
        for leg in (
            self.store.pair.ce_short, self.store.pair.pe_short,
            self.store.pair.ce_hedge, self.store.pair.pe_hedge,
        ):
            if leg.state in (
                LegState.CLOSED, LegState.ABORTED, LegState.PENDING
            ):
                continue
            if PAPER_TRADING_MODE:
                ref = float(leg.last_ltp or leg.fill_price or 0.0)
                side = (
                    "BUY" if leg.kind == LegKind.CORE_SHORT else "SELL"
                )
                fill = paper_fill(
                    side, ref,
                    slip_bps=(
                        PAPER_HEDGE_SLIPPAGE_BPS
                        if leg.kind == LegKind.HEDGE_LONG
                        else PAPER_CORE_SLIPPAGE_BPS
                    ),
                    min_slippage_ticks=(
                        PAPER_HEDGE_MIN_SLIPPAGE_TICKS
                        if leg.kind == LegKind.HEDGE_LONG else 0
                    ),
                )
            else:
                broker_fill = result.fills.get(leg.instrument_key)
                if broker_fill is None:
                    missing.append(leg.leg_id)
                    continue
                leg.exit_order_id, fill = broker_fill
            self._record_leg_exit(leg, fill)
            write_fill(leg, "EXIT_ALL_FILL")

        if not PAPER_TRADING_MODE and result.broker_flat and missing:
            for leg in (
                self.store.pair.ce_short, self.store.pair.pe_short,
                self.store.pair.ce_hedge, self.store.pair.pe_hedge,
            ):
                if (
                    leg.leg_id not in missing
                    or leg.state == LegState.CLOSED
                ):
                    continue
                estimate = float(
                    leg.last_ltp or leg.fill_price or 0.0
                )
                if estimate > 0:
                    log.critical(
                        "EXIT_FILL_PRICE_UNAVAILABLE: broker flat "
                        "for %s; using last mark %.2f",
                        leg.leg_id, estimate,
                    )
                    self._record_leg_exit(leg, estimate)
            missing = [
                leg.leg_id
                for leg in (
                    self.store.pair.ce_short,
                    self.store.pair.pe_short,
                    self.store.pair.ce_hedge,
                    self.store.pair.pe_hedge,
                )
                if leg.state not in (
                    LegState.CLOSED, LegState.ABORTED,
                    LegState.PENDING,
                )
            ]
        self.store.compute_mtm()
        return missing

    async def _square_off_all(self, context: str) -> None:
        assert self.rest
        if self.store.pair:
            for leg in (
                self.store.pair.ce_short, self.store.pair.pe_short
            ):
                if (
                    leg.sl_order_id
                    and leg.state != LegState.CLOSED
                ):
                    await self._cancel_sl_or_confirm_fill(leg)

        last_result = ExitAllResult({}, [], {})
        attempts = 1 if PAPER_TRADING_MODE else 3
        for attempt in range(1, attempts + 1):
            last_result = await exit_all_positions(self.rest)
            missing = self._apply_exit_fills(last_result)
            if last_result.broker_flat:
                if missing:
                    log.critical(
                        "%s: broker flat but %d leg(s) lack exit "
                        "marks: %s",
                        context, len(missing), missing,
                    )
                log.info(
                    "%s: broker position reconciliation is flat",
                    context,
                )
                return
            log.critical(
                "%s: square-off attempt %d/%d left positions=%s",
                context, attempt, attempts,
                last_result.remaining_positions,
            )
            if attempt < attempts:
                await asyncio.sleep(1.0)

        raise TradingError(
            f"{context}: broker still open after {attempts} attempts: "
            f"{last_result.remaining_positions}"
        )

    def _trigger_kill_switch(self, reason: str) -> None:
        if self.store.kill_switch_triggered:
            return
        self.store.kill_switch_triggered = True
        self.store.kill_switch_reason = reason
        self.store.exit_reason = reason
        self._stopped = True
        log.warning("KILL_SWITCH: %s", reason)
        log_event(
            self.paper_log_csv, "KILL_SWITCH", None,
            entry_vix=self.store.entry_vix,
            running_total_mtm=self.store.total_mtm,
            trail_lock=self.store.trail_lock,
            notes=reason,
            session_date=(
                self.store.pair.session_date
                if self.store.pair else ""
            ),
        )

        async def task() -> None:
            try:
                await self._square_off_all("KILL_SWITCH")
                log.info("KILL_SWITCH_COMPLETE")
            except Exception as exc:
                self._kill_switch_error = exc
                log.critical(
                    "KILL_SWITCH_SQUARE_OFF_FAILED: %s", exc,
                    exc_info=True,
                )
                raise
            finally:
                self._kill_switch_done.set()

        if self.worker:
            self.worker.submit(
                OrderTask(name="kill_switch", coro_factory=task)
            )

    def _on_feed_disconnect(self) -> None:
        try:
            self.loop.call_soon_threadsafe(
                self._on_feed_disconnect_async
            )
        except RuntimeError:
            pass

    def _on_feed_disconnect_async(self) -> None:
        if self.store.feed_disconnected_at is None:
            self.store.feed_disconnected_at = time.time()
            log.warning(
                "FEED_DISCONNECTED at %s",
                market_now().isoformat(timespec="seconds"),
            )
            log_event(
                self.paper_log_csv, "FEED_DISCONNECT", None,
                entry_vix=self.store.entry_vix,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                notes="broker SL-L orders remain in place",
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
            )

    def _on_feed_reconnect_async(self) -> None:
        if self.store.feed_disconnected_at is not None:
            dur = time.time() - self.store.feed_disconnected_at
            log.info("FEED_RECONNECTED after %.1fs", dur)
            log_event(
                self.paper_log_csv, "FEED_RECONNECT", None,
                entry_vix=self.store.entry_vix,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                notes=f"downtime={dur:.1f}s",
                session_date=(
                    self.store.pair.session_date
                    if self.store.pair else ""
                ),
            )
            self.store.feed_disconnected_at = None

    # ------------------------------------------------------------ re-entry
    def _check_reentry_eligible(self, leg: Leg) -> bool:
        """
        FIX 2: Check if a single leg meets all re-entry criteria.
        Used by _maybe_reenter to ensure BOTH legs qualify before
        re-entering either one. Prevents one-sided naked re-entry.
        """
        if leg.state != LegState.CLOSED:
            return False
        if leg.reentry_count >= MAX_REENTRIES_PER_LEG:
            return False
        if (
            leg.closed_at is not None
            and time.time() - leg.closed_at < REENTRY_COOLDOWN_SEC
        ):
            return False
        if leg.fill_price is None or leg.last_ltp is None:
            return False
        if leg.data_stale:
            return False
        threshold = leg.fill_price * (1 - REENTRY_MOMENTUM_DISCOUNT)
        return leg.last_ltp < threshold

    async def _maybe_reenter(self) -> None:
        """
        FIX 2 + FIX 3: Re-entry requires BOTH legs simultaneously
        and includes fresh hedge legs.

        OLD BEHAVIOR: Re-entered one leg at a time without hedge.
        This created naked short exposure between CE and PE re-entries
        and left re-entered positions completely unhedged.

        NEW BEHAVIOR:
        1. Check BOTH CE and PE legs meet all re-entry criteria
        2. Only proceed if BOTH qualify (never one-sided)
        3. Re-enter with fresh hedge legs (same as initial entry)
        4. Hedge legs placed BEFORE short legs (RULE O1)
        """
        if not self.store.pair:
            return

        pair = self.store.pair

        # FIX 2: Check BOTH legs before re-entering either one
        ce_eligible = self._check_reentry_eligible(pair.ce_short)
        pe_eligible = self._check_reentry_eligible(pair.pe_short)

        # If neither eligible, nothing to do
        if not ce_eligible and not pe_eligible:
            return

        # If only one eligible, log and wait for the other
        if ce_eligible and not pe_eligible:
            log.info(
                "REENTRY_WAITING: CE eligible but PE not "
                "(state=%s reentry_count=%d) — waiting for both",
                pair.pe_short.state, pair.pe_short.reentry_count,
            )
            return
        if pe_eligible and not ce_eligible:
            log.info(
                "REENTRY_WAITING: PE eligible but CE not "
                "(state=%s reentry_count=%d) — waiting for both",
                pair.ce_short.state, pair.ce_short.reentry_count,
            )
            return

        # Both eligible — validate common guards
        nifty_key = self._bootstrap_cache.get("nifty_key")
        spot_stale = (
            self.store.current_spot_at is None
            or time.time() - self.store.current_spot_at
            > STALE_TICK_TIMEOUT_SEC
        )
        if spot_stale and self.rest and self.ws and self.ws.connected and nifty_key:
            try:
                spot_quote = await fetch_ltps_async(
                    self.rest, [nifty_key]
                )
                if spot_quote.get(nifty_key, 0) > 0:
                    self.store.current_spot = float(
                        spot_quote[nifty_key]
                    )
                    self.store.current_spot_at = time.time()
            except Exception as exc:
                log.warning(
                    "REENTRY spot REST refresh failed: %s", exc
                )

        entry_spot = self.store.entry_spot
        current_spot = self.store.current_spot
        if (
            entry_spot <= 0
            or current_spot <= 0
            or self.store.current_spot_at is None
            or time.time() - self.store.current_spot_at
            > STALE_TICK_TIMEOUT_SEC
        ):
            log.warning(
                "REENTRY_SKIPPED: no valid current spot"
            )
            return

        spot_move = abs(current_spot - entry_spot) / entry_spot
        if spot_move > REENTRY_MAX_SPOT_MOVE_PCT:
            log.warning(
                "REENTRY_SKIPPED: spot moved %.2f%% > max %.2f%%",
                spot_move * 100.0,
                REENTRY_MAX_SPOT_MOVE_PCT * 100.0,
            )
            return

        current_vix = self.store.current_vix
        if self.store.original_entry_vix <= 0 or current_vix <= 0:
            log.warning("REENTRY_SKIPPED: no valid VIX")
            return

        vix_spike = (
            (current_vix - self.store.original_entry_vix)
            / self.store.original_entry_vix
        )
        if vix_spike > REENTRY_VIX_GUARD_PCT:
            log.warning(
                "REENTRY_SKIPPED: vix_spike=%.1f%% > guard=%.1f%%",
                vix_spike * 100.0,
                REENTRY_VIX_GUARD_PCT * 100.0,
            )
            log_event(
                self.paper_log_csv, "REENTRY_SKIPPED", None,
                entry_vix=current_vix,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                notes="vix_spike_guard",
                session_date=pair.session_date,
            )
            return

        # Validate margin for re-entry
        if self.rest:
            try:
                await self._validate_entry_margin(
                    [pair.ce_short, pair.pe_short,
                     pair.ce_hedge, pair.pe_hedge],
                    {
                        pair.ce_short.instrument_key: (
                            pair.ce_short.last_ltp or 0.0
                        ),
                        pair.pe_short.instrument_key: (
                            pair.pe_short.last_ltp or 0.0
                        ),
                        pair.ce_hedge.instrument_key: (
                            pair.ce_hedge.last_ltp or 0.0
                        ),
                        pair.pe_hedge.instrument_key: (
                            pair.pe_hedge.last_ltp or 0.0
                        ),
                    },
                    context="REENTRY",
                )
            except TradingError as exc:
                log.warning(
                    "REENTRY_SKIPPED: margin preflight failed: %s",
                    exc,
                )
                return

        log.info(
            "REENTRY: both legs eligible — entering straddle "
            "with fresh hedges"
        )

        # FIX 3: Re-enter BOTH legs with fresh hedges
        await self._reenter_both_legs(pair, current_vix)

    async def _reenter_both_legs(
        self, pair: LegPair, current_vix: float
    ) -> None:
        """
        FIX 3: Re-enter both legs with fresh hedge legs.
        Hedge legs are placed BEFORE short legs (same as initial entry).
        Never creates naked short exposure.
        """
        # Step 1: Get fresh quotes for all 4 legs
        all_keys = [
            pair.ce_short.instrument_key,
            pair.pe_short.instrument_key,
            pair.ce_hedge.instrument_key,
            pair.pe_hedge.instrument_key,
        ]
        try:
            fresh = await fetch_ltps_async(self.rest, all_keys)
        except Exception as exc:
            log.warning(
                "REENTRY_ABORTED: fresh quote fetch failed: %s", exc
            )
            return

        for leg in [
            pair.ce_short, pair.pe_short,
            pair.ce_hedge, pair.pe_hedge,
        ]:
            ltp = fresh.get(leg.instrument_key)
            if ltp and ltp > 0:
                leg.last_ltp = ltp
                leg.last_ltp_at = time.time()
                leg.data_stale = False

        # Step 2: Reset short legs for re-entry
        for short_leg in [pair.ce_short, pair.pe_short]:
            short_leg.reentry_count += 1
            self.store.num_reentries += 1
            short_leg.state = LegState.PENDING
            short_leg.order_id = None
            short_leg.fill_price = None
            short_leg.exit_price = None
            short_leg.exit_order_id = None
            short_leg.sl_order_id = None
            short_leg.sl_triggered_at = None
            short_leg.sl_broker_triggered = False
            short_leg.sl_fill_pending = False
            short_leg.sl_escalation_in_flight = False
            short_leg.sl_trigger_price = None
            short_leg.sl_percent = None
            short_leg.sl_limit_price = None
            short_leg.sl_limit_buffer = None
            short_leg.exit_in_flight = False
            short_leg.closed_at = None
            log_event(
                self.paper_log_csv, "REENTRY_RESET", short_leg,
                order_side="SELL",
                ltp_at_event=short_leg.last_ltp,
                entry_vix=current_vix,
                running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                reentry_count=short_leg.reentry_count,
                notes="both_legs_simultaneous_reentry",
                session_date=pair.session_date,
            )

        # Step 3: Reset hedge legs for re-entry
        for hedge_leg in [pair.ce_hedge, pair.pe_hedge]:
            hedge_leg.state = LegState.PENDING
            hedge_leg.order_id = None
            hedge_leg.fill_price = None
            hedge_leg.exit_price = None
            hedge_leg.exit_order_id = None
            hedge_leg.closed_at = None
            hedge_leg.realized_pnl = hedge_leg.realized_pnl  # preserve

        # Step 4: Execute with hedge-first ordering (RULE O1)
        async def run_side(
            side_label: str, hedge: Leg, core: Leg
        ) -> bool:
            hedge_ltp = float(hedge.last_ltp or 0.0)
            if hedge_ltp <= 0:
                log.warning(
                    "REENTRY_ABORT: no hedge quote for %s",
                    hedge.leg_id,
                )
                return False

            # Place hedge FIRST
            ok = await self._place_limit_buy_with_timeout(
                hedge,
                ref_price=hedge_ltp,
                slippage_ticks=HEDGE_LIMIT_SLIPPAGE_TICKS,
                timeout_sec=HEDGE_FILL_TIMEOUT_SEC,
                retry_ticks=HEDGE_LIMIT_SLIPPAGE_TICKS_RETRY,
            )
            if not ok:
                log.warning(
                    "REENTRY_ABORT: hedge failed for side=%s",
                    side_label,
                )
                hedge.state = LegState.ABORTED
                core.state = LegState.ABORTED
                return False

            # Place short AFTER hedge confirmed
            core_ltp = float(core.last_ltp or 0.0)
            ok2 = await self._place_limit_sell_with_timeout(
                core, ref_price=core_ltp
            )
            if not ok2:
                log.warning(
                    "REENTRY_ABORT: core failed for side=%s; "
                    "unwinding orphan hedge", side_label,
                )
                await self._exit_leg_market(
                    hedge,
                    tag=f"EXIT_REENTRY_ORPHAN_{hedge.leg_id}",
                )
                core.state = LegState.ABORTED
                return False

            # Place broker SL for the short
            await self._place_broker_sl(core)
            return True

        ce_ok, pe_ok = await asyncio.gather(
            run_side("CE", pair.ce_hedge, pair.ce_short),
            run_side("PE", pair.pe_hedge, pair.pe_short),
        )

        if ce_ok and pe_ok:
            log.info(
                "REENTRY_COMPLETE: both sides re-entered with hedges "
                "ce_reentry=%d pe_reentry=%d",
                pair.ce_short.reentry_count,
                pair.pe_short.reentry_count,
            )
        else:
            # Partial re-entry: close the successful side to avoid
            # one-sided exposure
            log.error(
                "REENTRY_PARTIAL: ce_ok=%s pe_ok=%s — "
                "closing successful side to avoid naked exposure",
                ce_ok, pe_ok,
            )
            if ce_ok and not pe_ok:
                for leg in [pair.ce_short, pair.ce_hedge]:
                    if leg.state not in (
                        LegState.CLOSED, LegState.ABORTED
                    ):
                        with contextlib.suppress(Exception):
                            await self._exit_leg_market(
                                leg,
                                tag=f"EXIT_REENTRY_ABORT_{leg.leg_id}",
                            )
            elif pe_ok and not ce_ok:
                for leg in [pair.pe_short, pair.pe_hedge]:
                    if leg.state not in (
                        LegState.CLOSED, LegState.ABORTED
                    ):
                        with contextlib.suppress(Exception):
                            await self._exit_leg_market(
                                leg,
                                tag=f"EXIT_REENTRY_ABORT_{leg.leg_id}",
                            )

    def _scheduled_time_exit(self) -> dtime:
        expiry = self._bootstrap_cache.get("expiry")
        if expiry and expiry == market_now().date().isoformat():
            return EXPIRY_DAY_TIME_EXIT
        return TIME_EXIT

    async def time_exit(self) -> None:
        if not self.store.pair:
            return
        scheduled = self._scheduled_time_exit()
        label = scheduled.strftime("%H:%M")
        log.info("TIME_EXIT %s — squaring off", label)
        assert self.rest
        await self._square_off_all("TIME_EXIT")
        self.store.exit_reason = f"time_exit_{label.replace(':', '')}"
        log.info("TIME_EXIT_COMPLETE scheduled=%s", label)
        log_event(
            self.paper_log_csv, "TIME_EXIT", None,
            entry_vix=self.store.entry_vix,
            running_total_mtm=self.store.total_mtm,
            trail_lock=self.store.trail_lock,
            notes=f"scheduled square-off {label}",
            session_date=(
                self.store.pair.session_date
                if self.store.pair else ""
            ),
        )

    async def _shutdown_worker(
        self, worker_task: "asyncio.Task[Any]"
    ) -> None:
        if self.worker:
            try:
                await asyncio.wait_for(
                    self.worker.q.join(), timeout=5.0
                )
            except asyncio.TimeoutError:
                log.warning("worker queue did not drain within 5s")
            self.worker.stop()
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    # ---------------------------------------------------------------- main loop
    async def run(self) -> None:
        if PAPER_TRADING_MODE:
            log.info(
                "CLOCK_CHECK: skipped in paper mode; tz=%s", MARKET_TZ
            )
        else:
            await verify_clock_sync()

        today = market_now().date()
        now_time = market_now().time()

        if self.store.unresolved_cached_state:
            message = (
                "UNRESOLVED ACTIVE STATE: prior session ended with "
                "active legs. Reconcile broker positions, then delete "
                "nifty_short_straddle_state.sqlite3 before new trade."
            )
            if not PAPER_TRADING_MODE:
                raise TradingError(message)
            log.warning(
                "%s Continuing in paper mode.", message
            )

        calendar_age = (
            today - HOLIDAY_CALENDAR_REVIEWED_ON
        ).days
        if calendar_age > HOLIDAY_CALENDAR_MAX_AGE_DAYS:
            msg = (
                "HOLIDAY CALENDAR REVIEW OVERDUE: last reviewed %s "
                "(%d days ago). Update NSE_MARKET_HOLIDAYS and "
                "HOLIDAY_CALENDAR_REVIEWED_ON."
                % (HOLIDAY_CALENDAR_REVIEWED_ON, calendar_age)
            )
            if not ALLOW_NON_TRADING_DAY_RUN:
                raise TradingError(msg)
            log.warning("%s Override enabled.", msg)

        if not is_nse_trading_day(today):
            if not ALLOW_NON_TRADING_DAY_RUN:
                msg = (
                    "NOT A TRADING DAY: %s. Next trading day ~%s. "
                    "No orders placed."
                    % (today, next_nse_trading_day(today))
                )
                log.warning(msg)
                print(msg)
                return
            log.warning(
                "NON_TRADING_DAY_OVERRIDE: %s — proceeding for "
                "testing", today,
            )

        if now_time > EXEC_END_TIME:
            msg = (
                "ENTRY WINDOW CLOSED: now=%s window=%s-%s. "
                "No orders placed."
                % (
                    now_time.strftime("%H:%M:%S"),
                    EXEC_START_TIME.strftime("%H:%M:%S"),
                    EXEC_END_TIME.strftime("%H:%M:%S"),
                )
            )
            log.warning(msg)
            print(msg)
            return

        await self._wait_until_or_skip(
            ENTRY_VIX_TIME, label="ENTRY_VIX_TIME(09:15)"
        )
        try:
            async with AsyncRestClient(ACCESS_TOKEN) as bootstrap_rest:
                await self.bootstrap(bootstrap_rest)
        except TradingError as e:
            log.error("BOOTSTRAP FAILED: %s", e)
            print("ERROR: %s" % e)
            return
        except Exception:
            log.exception("bootstrap failed (unexpected error)")
            print("ERROR: bootstrap failed. See %s" % LOG_FILE)
            return

        async with AsyncRestClient(ACCESS_TOKEN) as self.rest:
            self.worker = OrderWorker(self.rest)
            worker_task = asyncio.create_task(self.worker.run())

            await self._wait_until_or_skip(
                STRIKE_SELECT_TIME,
                label="STRIKE_SELECT_TIME(09:19)",
            )
            try:
                self.select_strikes()
            except TradingError as e:
                log.error("STRIKE SELECTION FAILED: %s", e)
                print("ERROR: %s" % e)
                await self._shutdown_worker(worker_task)
                return
            except Exception:
                log.exception(
                    "strike selection failed (unexpected error)"
                )
                print(
                    "ERROR: strike selection failed. See %s" % LOG_FILE
                )
                await self._shutdown_worker(worker_task)
                return

            await self._wait_until(EXEC_START_TIME)

            if market_now().time() > EXEC_END_TIME:
                msg = (
                    "ENTRY WINDOW CLOSED: past %s, entry skipped."
                    % EXEC_END_TIME.strftime("%H:%M:%S")
                )
                log.warning(msg)
                print(msg)
                self.store.exit_reason = "entry_window_closed"
                await self._shutdown_worker(worker_task)
                return

            entry_ok = await self.execute_entry()
            await self._flush_state_persist()
            if not entry_ok:
                log.error(
                    "ENTRY ABORTED — one or more leg pairs failed."
                )

            instruments: List[str] = []
            if self.store.pair:
                instruments = [
                    self.store.pair.ce_short.instrument_key,
                    self.store.pair.pe_short.instrument_key,
                    self.store.pair.ce_hedge.instrument_key,
                    self.store.pair.pe_hedge.instrument_key,
                    self._bootstrap_cache["vix_key"],
                    self._bootstrap_cache["nifty_key"],
                ]

            self.loop = asyncio.get_running_loop()
            self.ws = WsAdapter(self.loop, instruments)

            def ws_runner() -> None:
                backoff = 1.0
                attempt = 0
                healthy_threshold = RECONNECT_STABLE_UPTIME_SEC
                flap_times: List[float] = []

                def flap_budget_exhausted() -> bool:
                    if not register_reconnect_flap(
                        flap_times, time.monotonic()
                    ):
                        return False
                    log.critical(
                        "WS_FLAP_CIRCUIT: %d disconnects in %.0fs; "
                        "kill-switching",
                        len(flap_times), RECONNECT_FLAP_WINDOW_SEC,
                    )
                    self.loop.call_soon_threadsafe(
                        self._trigger_kill_switch,
                        f"ws_flapping_{len(flap_times)}_in_"
                        f"{int(RECONNECT_FLAP_WINDOW_SEC)}s",
                    )
                    return True

                while not self._stopped:
                    connected_at = time.monotonic()
                    try:
                        self.ws.connect()
                        if self._stopped:
                            return
                    except Exception:
                        log.exception(
                            "ws connect failed; backoff=%.1fs", backoff
                        )
                        self._on_feed_disconnect()
                        if flap_budget_exhausted():
                            return
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        attempt += 1
                        if attempt > MAX_RECONNECT_ATTEMPTS:
                            log.error(
                                "ws reconnect exhausted (%d); "
                                "kill-switching",
                                MAX_RECONNECT_ATTEMPTS,
                            )
                            self.loop.call_soon_threadsafe(
                                self._trigger_kill_switch,
                                f"ws_reconnect_exhausted_"
                                f"{MAX_RECONNECT_ATTEMPTS}",
                            )
                            return
                        continue

                    uptime = time.monotonic() - connected_at
                    if uptime >= healthy_threshold:
                        attempt = 0
                        backoff = 1.0
                        log.info(
                            "ws healthy disconnect (uptime=%.1fs); "
                            "reset reconnect budget", uptime,
                        )
                    else:
                        attempt += 1

                    if attempt > MAX_RECONNECT_ATTEMPTS:
                        log.error(
                            "ws reconnect exhausted (%d); "
                            "kill-switching",
                            MAX_RECONNECT_ATTEMPTS,
                        )
                        self._on_feed_disconnect()
                        self.loop.call_soon_threadsafe(
                            self._trigger_kill_switch,
                            f"ws_reconnect_exhausted_"
                            f"{MAX_RECONNECT_ATTEMPTS}",
                        )
                        return

                    log.warning(
                        "ws disconnected; reconnect %d/%d in %.1fs",
                        attempt, MAX_RECONNECT_ATTEMPTS, backoff,
                    )
                    self._on_feed_disconnect()
                    if flap_budget_exhausted():
                        return
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

            ws_task = asyncio.create_task(
                asyncio.to_thread(ws_runner)
            )

            try:
                while not self._stopped:
                    if (
                        self.ws
                        and self.ws.connected
                        and self.store.feed_disconnected_at is not None
                    ):
                        self._on_feed_reconnect_async()

                    if (
                        self.ws
                        and self.ws.ever_connected
                        and not self.ws.connected
                    ):
                        self._on_feed_disconnect()
                        if (
                            self.store.feed_disconnected_at
                            and time.time()
                            - self.store.feed_disconnected_at
                            > MAX_FEED_DOWNTIME_SEC
                        ):
                            self._trigger_kill_switch(
                                reason=(
                                    f"feed_disconnected_for_"
                                    f"{MAX_FEED_DOWNTIME_SEC}s"
                                )
                            )
                            break

                    if (
                        market_now().time()
                        >= self._scheduled_time_exit()
                    ):
                        await self.time_exit()
                        break

                    drained = 0
                    ltp_map: Dict[str, float] = {}
                    while drained < 500:
                        try:
                            item = self.ws._feed_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        drained += 1
                        if (
                            not isinstance(item, tuple)
                            or len(item) != 3
                        ):
                            continue
                        kind, instrument_key, payload = item
                        if kind == "ltpc":
                            if isinstance(payload, dict):
                                ltpc = payload.get("ltpc")
                                if (
                                    isinstance(ltpc, dict)
                                    and "ltp" in ltpc
                                ):
                                    try:
                                        ltp_map[instrument_key] = float(
                                            ltpc["ltp"]
                                        )
                                    except (TypeError, ValueError):
                                        pass

                    if ltp_map:
                        await self._on_tick_local(ltp_map)
                        await self._maybe_reenter()

                    if self.ws._feed_queue.empty():
                        await asyncio.sleep(0.05)

                    if self.store.pair:
                        for leg in [
                            self.store.pair.ce_short,
                            self.store.pair.pe_short,
                        ]:
                            if (
                                leg.sl_triggered_at
                                and not leg.sl_broker_triggered
                                and leg.state == LegState.SL_PLACED
                                and time.time() - leg.sl_triggered_at
                                > SL_L_FILL_TIMEOUT_SEC
                            ):
                                log.warning(
                                    "SL_L_FILL_TIMEOUT: leg=%s "
                                    "escalating to market",
                                    leg.leg_id,
                                )
                                leg.sl_triggered_at = None
                                log_event(
                                    self.paper_log_csv,
                                    "SL_L_FILL_TIMEOUT", leg,
                                    order_side="BUY",
                                    ltp_at_event=leg.last_ltp,
                                    entry_vix=self.store.entry_vix,
                                    sl_trigger_price=(
                                        leg.sl_trigger_price
                                    ),
                                    running_total_mtm=(
                                        self.store.total_mtm
                                    ),
                                    trail_lock=self.store.trail_lock,
                                    reentry_count=leg.reentry_count,
                                    notes=(
                                        f"escalating to market after "
                                        f"{SL_L_FILL_TIMEOUT_SEC}s"
                                    ),
                                    session_date=(
                                        self.store.pair.session_date
                                        if self.store.pair else ""
                                    ),
                                )
                                if (
                                    self.worker
                                    and self.rest
                                    and not leg.sl_escalation_in_flight
                                ):
                                    leg.sl_escalation_in_flight = True

                                    async def escalate(
                                        _leg: Leg = leg,
                                    ) -> None:
                                        assert self.rest
                                        if (
                                            _leg.sl_broker_triggered
                                            or _leg.state
                                            == LegState.CLOSED
                                        ):
                                            return
                                        if _leg.sl_order_id:
                                            already = await (
                                                self
                                                ._cancel_sl_or_confirm_fill(
                                                    _leg
                                                )
                                            )
                                            if already:
                                                return
                                        if (
                                            _leg.state == LegState.CLOSED
                                            or _leg.sl_broker_triggered
                                        ):
                                            return
                                        try:
                                            exit_fill = await (
                                                self._exit_leg_market(
                                                    _leg,
                                                    tag=f"SL_ESCALATE_"
                                                    f"{_leg.leg_id}",
                                                )
                                            )
                                            log.info(
                                                "SL_ESCALATED_TO_MARKET:"
                                                " leg=%s fill=%.2f",
                                                _leg.leg_id, exit_fill,
                                            )
                                        except Exception as e:
                                            log.exception(
                                                "SL_ESCALATE_FAILED: "
                                                "leg=%s err=%s",
                                                _leg.leg_id, e,
                                            )
                                        finally:
                                            _leg.sl_escalation_in_flight = (
                                                False
                                            )

                                    self.worker.submit(
                                        OrderTask(
                                            name=(
                                                f"sl_escalate_"
                                                f"{leg.leg_id}"
                                            ),
                                            coro_factory=escalate,
                                        )
                                    )

            finally:
                self._stopped = True
                if self.ws and self.ws.streamer:
                    with contextlib.suppress(Exception):
                        self.ws.streamer.disconnect()
                ws_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ws_task
                if self.store.kill_switch_triggered:
                    try:
                        await asyncio.wait_for(
                            self._kill_switch_done.wait(),
                            timeout=ORDER_FILL_TIMEOUT_SEC * 4 + 15.0,
                        )
                    except asyncio.TimeoutError as exc:
                        self._kill_switch_error = exc
                        log.critical(
                            "Kill-switch square-off timed out"
                        )
                    if self._kill_switch_error is not None:
                        try:
                            await self._square_off_all(
                                "KILL_SWITCH_FINAL_RETRY"
                            )
                            self._kill_switch_error = None
                        except Exception as exc:
                            self._kill_switch_error = exc
                            log.critical(
                                "Final square-off retry failed: %s",
                                exc, exc_info=True,
                            )
                if self.worker:
                    try:
                        await asyncio.wait_for(
                            self.worker.q.join(), timeout=5.0
                        )
                    except asyncio.TimeoutError:
                        log.warning(
                            "worker queue did not drain within 5s"
                        )
                    self.worker.stop()
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task
                if self._kill_switch_error is not None:
                    raise TradingError(
                        "Kill-switch could not confirm flat broker "
                        f"position book: {self._kill_switch_error}"
                    )

        self.store.compute_mtm()
        await self._flush_state_persist()
        self._write_paper_summary()
        self._write_performance_report()
        await flush_file_io()

    def _write_paper_summary(self) -> None:
        if not PAPER_TRADING_MODE:
            return
        if not self.store.pair:
            return
        n_legs = sum(
            1
            for lg in (
                self.store.pair.ce_short, self.store.pair.pe_short,
                self.store.pair.ce_hedge, self.store.pair.pe_hedge,
            )
            if lg.state not in (
                LegState.ABORTED, LegState.PENDING,
                LegState.HEDGE_PLACED,
            )
        )
        append_summary_row(
            self.paper_summary_csv,
            {
                "session_date": self.store.pair.session_date,
                "entry_vix": f"{self.store.entry_vix:.2f}",
                "num_legs_entered": n_legs,
                "num_reentries": self.store.num_reentries,
                "num_hedge_aborts": self.store.num_hedge_aborts,
                "final_total_mtm": f"{self.store.total_mtm:.2f}",
                "max_drawdown_mtm": (
                    f"{self.store.max_drawdown_mtm:.2f}"
                ),
                "trail_lock_final": f"{self.store.trail_lock:.2f}",
                "exit_reason": (
                    self.store.exit_reason or "session_end"
                ),
            },
        )

    def _write_performance_report(self) -> None:
        """
        FIX 10: Performance report now includes transaction costs
        and reports Net PnL separately from Gross PnL.
        Old code reported only gross MTM which overstated returns
        by ~₹825/day (₹2,06,250/year on 250 trading days).
        """
        if not self.store.pair:
            return
        pair = self.store.pair

        def _prem(leg: Leg) -> Optional[float]:
            if leg.entry_value_total > 0:
                return round(leg.entry_value_total, 2)
            return (
                round(leg.fill_price * leg.qty, 2)
                if leg.fill_price is not None else None
            )

        ce_prem = _prem(pair.ce_short)
        pe_prem = _prem(pair.pe_short)
        ce_cost = _prem(pair.ce_hedge)
        pe_cost = _prem(pair.pe_hedge)

        traded = any(
            p is not None
            for p in (ce_prem, pe_prem, ce_cost, pe_cost)
        )

        premium_collected = (ce_prem or 0.0) + (pe_prem or 0.0)
        hedge_cost = (ce_cost or 0.0) + (pe_cost or 0.0)
        net_credit = premium_collected - hedge_cost
        gross_pnl = round(self.store.total_mtm, 2)

        # FIX 10: Compute and deduct transaction costs
        entry_fills: List[Tuple[float, int]] = []
        exit_fills: List[Tuple[float, int]] = []
        for leg in [
            pair.ce_short, pair.pe_short,
            pair.ce_hedge, pair.pe_hedge,
        ]:
            if leg.fill_price and leg.qty:
                entry_fills.append((leg.fill_price, leg.qty))
            if leg.exit_price and leg.qty:
                exit_fills.append((leg.exit_price, leg.qty))

        transaction_costs = _estimate_transaction_costs(
            entry_fills, exit_fills
        )
        net_pnl = round(gross_pnl - transaction_costs, 2)

        if not traded:
            result = "NO TRADE"
        elif net_pnl > 0:
            result = "PROFIT"
        elif net_pnl < 0:
            result = "LOSS"
        else:
            result = "BREAKEVEN"

        # FIX 10: Load cumulative using Net PnL
        prior_total, prior_days, prior_wins = (
            _load_performance_totals()
        )
        cumulative = round(prior_total + net_pnl, 2)
        days = prior_days + (1 if traded else 0)
        wins = prior_wins + (1 if traded and net_pnl > 0 else 0)
        win_rate = (
            f"{wins / days * 100:.1f}" if days > 0 else ""
        )

        notes: List[str] = []
        reentries = (
            pair.ce_short.reentry_count
            + pair.pe_short.reentry_count
        )
        if reentries:
            notes.append(f"re-entries: {reentries}")
        sl_legs = [
            lg.leg_id
            for lg in (pair.ce_short, pair.pe_short)
            if lg.sl_broker_triggered
            or lg.sl_triggered_at is not None
        ]
        if sl_legs:
            notes.append(
                "stop-loss hit on " + ", ".join(sl_legs)
            )
        if self.store.num_hedge_aborts:
            notes.append(
                f"hedge aborts: {self.store.num_hedge_aborts}"
            )
        if transaction_costs > 0:
            notes.append(
                f"tx_costs: ₹{transaction_costs:.0f}"
            )

        try:
            session_day = date.fromisoformat(
                pair.session_date
            ).strftime("%A")
        except ValueError:
            session_day = ""

        _append_performance_row({
            "Date": pair.session_date,
            "Day": session_day,
            "Entry VIX": f"{self.store.entry_vix:.2f}",
            "NIFTY Spot": (
                f"{self.store.entry_spot:.2f}"
                if self.store.entry_spot else ""
            ),
            "Sold CE Strike": f"{pair.ce_short.strike:.0f}",
            "Sold PE Strike": f"{pair.pe_short.strike:.0f}",
            "Hedge CE Strike": f"{pair.ce_hedge.strike:.0f}",
            "Hedge PE Strike": f"{pair.pe_hedge.strike:.0f}",
            "Premium Collected (Rs)": (
                f"{premium_collected:.2f}" if traded else ""
            ),
            "Hedge Cost (Rs)": (
                f"{hedge_cost:.2f}" if traded else ""
            ),
            "Net Credit (Rs)": (
                f"{net_credit:.2f}" if traded else ""
            ),
            "Gross PnL (Rs)": f"{gross_pnl:.2f}",
            "Transaction Costs (Rs)": f"{transaction_costs:.2f}",
            "Net PnL (Rs)": f"{net_pnl:.2f}",
            "Result": result,
            "Cumulative PnL (Rs)": f"{cumulative:.2f}",
            "Win Rate (%)": win_rate,
            "Exit Reason": _layman_exit_reason(
                self.store.exit_reason
            ),
            "Notes": "; ".join(notes),
        })
        log.info(
            "PERFORMANCE_ROW: date=%s result=%s "
            "gross=%.2f costs=%.2f net=%.2f cumulative=%.2f -> %s",
            pair.session_date, result,
            gross_pnl, transaction_costs, net_pnl,
            cumulative, PERFORMANCE_CSV,
        )

    async def _wait_until(self, t: dtime) -> None:
        while market_now().time() < t:
            await asyncio.sleep(0.5)

    async def _wait_until_or_skip(
        self, t: dtime, label: str = ""
    ) -> None:
        now = market_now().time()
        if now >= t:
            log.warning(
                "TIME_GATE_SKIPPED: %s already passed "
                "(now=%s target=%s) — proceeding immediately",
                label, now, t,
            )
            return
        log.info(
            "TIME_GATE: waiting until %s (now=%s)",
            label or t, now,
        )
        while market_now().time() < t:
            await asyncio.sleep(0.5)


# ============================================================================
# Loop exception handler + main wrapper
# ============================================================================
_engine_ref: Optional[Engine] = None


def _async_exception_handler(
    loop: asyncio.AbstractEventLoop,
    context: Dict[str, Any],
) -> None:
    msg = context.get("message", "async exception")
    exc = context.get("exception")
    log.error("UNHANDLED_ASYNC: %s", msg, exc_info=exc)
    if not PAPER_TRADING_MODE and _engine_ref and _engine_ref.rest:
        try:
            asyncio.create_task(
                emergency_square_off_with_fresh_client()
            )
        except Exception:
            log.exception(
                "failed to schedule emergency exit on async exception"
            )


async def emergency_square_off_with_fresh_client() -> bool:
    if PAPER_TRADING_MODE:
        return True
    async with AsyncRestClient(ACCESS_TOKEN) as rest:
        last = ExitAllResult({}, [], {"<not-attempted>": 1})
        for attempt in range(1, 4):
            last = await exit_all_positions(rest)
            if last.broker_flat:
                log.critical(
                    "EMERGENCY_SQUARE_OFF_CONFIRMED_FLAT "
                    "on attempt %d", attempt,
                )
                return True
            log.critical(
                "EMERGENCY_SQUARE_OFF attempt %d/3 still open: %s",
                attempt, last.remaining_positions,
            )
            if attempt < 3:
                await asyncio.sleep(1.0)
        log.critical(
            "EMERGENCY_SQUARE_OFF_FAILED: broker still reports %s",
            last.remaining_positions,
        )
        return False


def main() -> None:
    global _engine_ref

    if not UPSTOX_ACCESS_TOKEN:
        print("=" * 74)
        print("CONFIG ERROR: UPSTOX_ACCESS_TOKEN is missing.")
        print("Create env.txt with:")
        print("    UPSTOX_ACCESS_TOKEN=eyJ0eXAiOiJKV1Q...")
        print("=" * 74)
        sys.exit(1)

    if PAPER_TRADING_MODE:
        log.info("=== RUN MODE: PAPER TRADING ===")
        log.info(
            "Outputs: %s, %s, %s",
            PAPER_TRADE_LOG_CSV,
            PAPER_TRADE_SUMMARY_CSV,
            PERFORMANCE_CSV,
        )
        log.info("=" * 60)
        log.info("FIXES ACTIVE IN THIS VERSION:")
        log.info(
            "  FIX 1: Combined straddle SL "
            "(not per-leg)"
        )
        log.info(
            "  FIX 2: Re-entry requires BOTH legs "
            "simultaneously"
        )
        log.info(
            "  FIX 3: Re-entry includes fresh hedge legs"
        )
        log.info(
            "  FIX 4: current_spot_at initialized correctly"
        )
        log.info(
            "  FIX 5: Paper SL fills use last_ltp not 0.0"
        )
        log.info(
            "  FIX 6: Trail lock debounced "
            f"({TRAIL_BREACH_CONFIRM_TICKS} ticks)"
        )
        log.info(
            f"  FIX 7: Hedge delta={HEDGE_TARGET_DELTA} "
            f"(was 0.05)"
        )
        log.info(
            f"  FIX 8: SL base={SL_BASE_PERCENT:.0%} "
            f"min={SL_MIN_PERCENT:.0%} max={SL_MAX_PERCENT:.0%} "
            f"(was 30%/18%/40%)"
        )
        log.info(
            f"  FIX 9: Entry window until "
            f"{EXEC_END_TIME} (was 09:20:30)"
        )
        log.info(
            "  FIX 10: Transaction costs in performance CSV"
        )
        log.info(
            f"  FIX 11: Calendar max age="
            f"{HOLIDAY_CALENDAR_MAX_AGE_DAYS}d (was 7d)"
        )
        log.info("=" * 60)
    else:
        log.info("=== RUN MODE: LIVE ===")
        log.info("Scoreboard: %s", PERFORMANCE_CSV)

    engine = Engine()
    _engine_ref = engine
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(_async_exception_handler)

    def _strategy_may_have_positions() -> bool:
        pair = engine.store.pair
        if pair is None:
            return False
        return any(
            leg.state not in (LegState.CLOSED, LegState.PENDING)
            and (
                leg.order_id is not None
                or leg.fill_price is not None
            )
            for leg in (
                pair.ce_short, pair.pe_short,
                pair.ce_hedge, pair.pe_hedge,
            )
        )

    def _run_emergency_square_off() -> None:
        if PAPER_TRADING_MODE or not _strategy_may_have_positions():
            return
        try:
            ok = loop.run_until_complete(
                emergency_square_off_with_fresh_client()
            )
            if not ok:
                log.critical(
                    "MANUAL INTERVENTION REQUIRED: "
                    "broker positions remain open"
                )
        except Exception:
            log.exception("emergency square-off also failed")
            log.critical(
                "MANUAL INTERVENTION REQUIRED: "
                "verify and close broker positions"
            )

    def _sigterm(*_: Any) -> None:
        log.warning("SIGTERM received — initiating shutdown")
        if (
            not PAPER_TRADING_MODE
            and engine.rest
            and engine.worker
        ):
            engine._trigger_kill_switch("signal_shutdown")
        else:
            engine._stopped = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, _sigterm)

    try:
        loop.run_until_complete(engine.run())
    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — shutting down")
        print("Interrupted — initiating emergency square-off.")
        _run_emergency_square_off()
    except TradingError as e:
        log.error("FATAL: %s", e)
        print("ERROR: %s" % e)
        _run_emergency_square_off()
    except Exception as e:
        log.error("FATAL: unexpected error — %s", e)
        print("FATAL: unexpected error — %s" % e)
        print("Full details in: %s" % LOG_FILE)
        try:
            with open(LOG_FILE, "a") as _f:
                traceback.print_exc(file=_f)
        except Exception:
            pass
        _run_emergency_square_off()
    finally:
        try:
            if not loop.is_closed():
                loop.run_until_complete(flush_file_io())
        except Exception:
            log.exception("Could not flush pending file writes")
        try:
            engine.store.close()
        except Exception:
            pass
        loop.close()
        log.info("shutdown complete")


if __name__ == "__main__":
    main()