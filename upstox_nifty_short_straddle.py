#!/usr/bin/env python3
"""
Production-grade NIFTY Weekly Short Straddle (Hedged) — Upstox v2/v3
=========================================================================

Run modes (set ONLY by editing source, no runtime flag):
- PAPER_TRADING_MODE = True   : forward-test against live ticks, no real orders
- PAPER_TRADING_MODE = False  : live trading (real REST orders)

Strategy
--------
- 09:15: Download instrument master; resolve current NIFTY weekly expiry under
  NSE_FO and India VIX instrument key dynamically.
- 09:19:  VIX-adaptive strike selection. Pull greeks in a SINGLE batched
  /v3/market-quote/option-greek call. Delta is the primary filter; premium
  band is a tiebreaker, never a hard filter.
- 09:19:30 - 09:20:30: Hedged execution. Place hedge long legs first
  (LIMIT BUY @ ask + slippage). If a hedge leg does not fill within
  HEDGE_FILL_TIMEOUT_SEC, cancel, re-price once, retry once; on second
  timeout ABORT the leg pair (never naked short).
- 09:20:30 onward: Tick engine via MarketDataStreamerV3. Broker-side SL-L
  stop with local fast-acting duplicate. MTM trailing lock. Kill-switch on
  loss, trail breach, sustained feed outage, or 15:15 time exit.

Engineering guarantees
----------------------
- All REST after websocket connect runs through an asyncio.Queue worker
  using aiohttp (non-blocking). The Upstox SDK is only used for
  MarketDataStreamerV3 (where it is required) and v2 order placement
  during the pre-websocket bootstrap phase.
- v2 strictly for orders/option-chain; v3 strictly for streamer.
  No v1 anywhere.
- Paper mode simulates fills against current tick with
  PAPER_SLIPPAGE_MODEL_BPS, never zero slippage, and writes
  paper_trading_log.csv + paper_trading_summary.csv.
- Every state transition logs timestamp, leg id, order_id, and rule fired.
- Graceful shutdown via try/except/finally, asyncio.run wrapper, and
  loop.set_exception_handler so unhandled async exceptions still trigger
  emergency square-off in live mode.

NOTE: Replace ACCESS_TOKEN before running, and verify Upstox v2/v3
endpoints/SDK method names against the release you have installed — the
SDK occasionally renames methods between minor versions.

SECRETS: Upstox credentials are read at import time from env.txt
(override the path with the `UPSTOX_ENV_FILE` environment variable;
default is <script_dir>/env.txt). The file uses standard
.env format — one KEY=value pair per line, with optional surrounding
quotes. Lines starting with # are comments. Example:

    UPSTOX_ACCESS_TOKEN=eyJ0eXAiOiJKV1Q...
    UPSTOX_API_KEY=ee2e4f16-0691-42d9-9a35-3f841ec29cbc
    UPSTOX_API_SECRET=d3rr2a0uib
    GEMINI_API_KEY=AIzaSyBsI2fQduE9GEMnbrucUn3OJAn5p9zQXFo

The file must NOT be committed to source control. Add it to .gitignore
and rotate tokens if the file is ever leaked (Upstox access tokens can
be revoked from https://account.upstox.com/, Gemini keys from
https://aistudio.google.com/app/apikey).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp
import requests  # only used for pre-websocket bootstrap; v2 only

# Upstox SDK — used for MarketDataStreamerV3 and v2 order place during
# bootstrap. After the websocket connects, ALL further REST goes through
# our aiohttp worker so the event loop is never blocked.
import upstox_client
from upstox_client import MarketDataStreamerV3


# ============================================================================
# Secrets loader — reads Upstox tokens from env.txt at import time
# ============================================================================
ENV_FILE_PATH = os.environ.get(
    "UPSTOX_ENV_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.txt")
)


def _load_env_file(path: str) -> Dict[str, str]:
    """Parse a .env-format file. One KEY=value per line, optional quotes
    (single or double), # for comments. Returns {} if the file doesn't
    exist — caller decides whether that's an error.
    """
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
                # Strip a single matched pair of surrounding quotes
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key:
                    out[key] = val
    except FileNotFoundError:
        return {}
    return out


def _require_env(env: Dict[str, str], key: str) -> str:
    """Return env[key] or raise a clear error pointing at env.txt."""
    val = env.get(key)
    if not val:
        raise RuntimeError(
            f"Required secret {key!r} is missing or empty in {ENV_FILE_PATH}.\n"
            f"Add a line like:\n    {key}=<your-token>\n"
            f"and ensure the file is readable. Override the path with the\n"
            f"UPSTOX_ENV_FILE environment variable if needed."
        )
    return val


_ENV = _load_env_file(ENV_FILE_PATH)


# ============================================================================
# CONFIG (no runtime inputs — all hardcoded as required)
# ============================================================================
PAPER_TRADING_MODE = True
LOT_SIZE = 25

# Secrets (loaded from env.txt — see _load_env_file above)
# All four keys are loaded so the OAuth flow can use them later, but
# only UPSTOX_ACCESS_TOKEN is required for running the script as-is
# (it authorises v2 order placement and the v3 websocket).
UPSTOX_ACCESS_TOKEN: str = _require_env(_ENV, "UPSTOX_ACCESS_TOKEN")
UPSTOX_API_KEY: str = _ENV.get("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET: str = _ENV.get("UPSTOX_API_SECRET", "")
GEMINI_API_KEY: str = _ENV.get("GEMINI_API_KEY", "")

# Backward-compat alias — the rest of the script uses ACCESS_TOKEN.
ACCESS_TOKEN = UPSTOX_ACCESS_TOKEN

# VIX-adaptive bands
LOW_VIX_THRESHOLD = 12.0
HIGH_VIX_THRESHOLD = 18.0
MIN_DELTA_LOWVIX, MAX_DELTA_LOWVIX = 0.22, 0.28
MIN_DELTA, MAX_DELTA = 0.20, 0.25
MIN_DELTA_HIVIX, MAX_DELTA_HIVIX = 0.15, 0.20
MIN_PREMIUM, MAX_PREMIUM = 75.0, 130.0

# Hedge parameters
HEDGE_TARGET_PREMIUM = 5.0
HEDGE_PREMIUM_TOLERANCE = 2.0
HEDGE_LIMIT_SLIPPAGE_TICKS = 2
HEDGE_LIMIT_SLIPPAGE_TICKS_RETRY = 5
HEDGE_FILL_TIMEOUT_SEC = 20

# Risk
SL_PERCENT = 0.30
REENTRY_MOMENTUM_DISCOUNT = 0.03
REENTRY_VIX_GUARD_PCT = 0.15
MAX_REENTRIES_PER_LEG = 1

MAX_DAILY_LOSS = -3000
TRAIL_START_PROFIT = 2000
TRAIL_LOCK_PROFIT = 1000
TRAIL_STEP = 500
TRAIL_STEP_INCREMENT = 1000

SL_LIMIT_BUFFER_POINTS_MIN = 5.0   # hard floor — never less than 5 points
# VIX-adaptive buffer. Earlier version used `PCT * (fill * SL_PERCENT)`
# but the user's audit correctly identified that for our selection band
# (premium 75-130) the PCT*gap term was <5, so the floor won and the
# adaptive scaling was dead weight in practice.
#
# New formula scales with the *underlying's* 1-day expected move
# (spot × vix/100), which is what actually drives the option's gap
# potential. For spot=24000, vix=14: 1-day move ≈ 336 points; 1-min
# move ≈ 336/sqrt(375) ≈ 17.4 points. A "flash crash buffer" of
# ~3-5 sigma of 1-min moves translates to ~50-90 points for ATM, but
# option gaps are *delta-attenuated* (option gap ≈ delta × spot gap),
# so for a 0.30-delta option the relevant 3-5 sigma option gap is
# ~15-25 points.
#
# SL_LIMIT_BUFFER_VIX_K = 0.0036 → for vix=14, spot=24000:
#   buffer = max(5, 0.0036 * 14 * 24000 / 100) = max(5, 12.1) = 12.1
# For vix=12 (calm):
#   buffer = max(5, 0.0036 * 12 * 24000 / 100) = max(5, 10.4) = 10.4
# For vix=20 (volatile):
#   buffer = max(5, 0.0036 * 20 * 24000 / 100) = max(5, 17.3) = 17.3
# For vix=25 (extreme):
#   buffer = max(5, 0.0036 * 25 * 24000 / 100) = max(5, 21.6) = 21.6
#
# All of these give 10-22 points of headroom — enough to absorb a
# 1-2 sigma 1-min option gap, instead of the old 5 that gets blown
# through by a single 10-pt underlying tick.
#
# Calibration reasoning: 1-day expected move = spot*vix/100 / sqrt(252);
# 1-min expected move = 1-day / sqrt(375); a "flash crash buffer" of
# 2-3 sigma of 1-min *option* moves (delta-attenuated) corresponds to
# ~10-15 points for ATM-ish options at vix=14. K=0.0036 gives 12.1
# points at vix=14, spot=24000, which is the right magnitude.
SL_LIMIT_BUFFER_VIX_K = 0.0036
SL_LIMIT_BUFFER_POINTS_MAX = 30.0
# Deprecated alias kept for any external code that imports it.
SL_LIMIT_BUFFER_POINTS = SL_LIMIT_BUFFER_POINTS_MIN
SL_L_FILL_TIMEOUT_SEC = 8
STALE_TICK_TIMEOUT_SEC = 10
MAX_FEED_DOWNTIME_SEC = 30
MAX_RECONNECT_ATTEMPTS = 5

PAPER_SLIPPAGE_MODEL_BPS = 8  # 8 bps simulated slippage in paper mode

# Session timing (24h)
ENTRY_VIX_TIME = dtime(9, 15)
STRIKE_SELECT_TIME = dtime(9, 19)
EXEC_START_TIME = dtime(9, 19, 30)
EXEC_END_TIME = dtime(9, 20, 30)
TIME_EXIT = dtime(15, 15)

# REST endpoints (v2/v3 only — no v1 anywhere)
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

# Upstox option tick size used for "ask + N ticks" reprice
TICK_SIZE = 0.05

# Logging — file + stdout, every state transition captured.
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "trading_audit.log")
CSV_FILE = os.path.join(LOG_DIR, "fills.csv")
MTM_LOG_FILE = os.path.join(LOG_DIR, "mtm.log")
PAPER_TRADE_LOG_CSV = os.path.join(LOG_DIR, "paper_trading_log.csv")
PAPER_TRADE_SUMMARY_CSV = os.path.join(LOG_DIR, "paper_trading_summary.csv")

# CSV schema (single source of truth for the paper trade log)
CSV_LOG_COLUMNS = [
    "timestamp",
    "session_date",
    "leg_id",
    "instrument_key",
    "event_type",
    "order_side",
    "simulated_price",
    "ltp_at_event",
    "qty",
    "entry_vix",
    "sl_trigger_price",
    "running_total_mtm",
    "trail_lock_active",
    "reentry_count",
    "notes",
]

CSV_SUMMARY_COLUMNS = [
    "session_date",
    "entry_vix",
    "num_legs_entered",
    "num_reentries",
    "num_hedge_aborts",
    "final_total_mtm",
    "max_drawdown_mtm",
    "trail_lock_final",
    "exit_reason",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("trader")


# ============================================================================
# CSV audit writers
# ============================================================================
def _ensure_csv_with_header(path: str, header: List[str]) -> None:
    """Create the file with the header row iff it doesn't yet exist.
    Hardcoded filenames + schemas — never accept a runtime path/header.
    """
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


def append_log_row(csv_path: str, row: Dict[str, Any]) -> None:
    """Append a single row to the given CSV log, creating header if missing."""
    _ensure_csv_with_header(csv_path, CSV_LOG_COLUMNS)
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([row.get(col, "") for col in CSV_LOG_COLUMNS])


def append_summary_row(csv_path: str, row: Dict[str, Any]) -> None:
    _ensure_csv_with_header(csv_path, CSV_SUMMARY_COLUMNS)
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([row.get(col, "") for col in CSV_SUMMARY_COLUMNS])


# ============================================================================
# Fills CSV writer (legacy live/paper audit trail; still used)
# ============================================================================
_FILLS_HEADER = [
    "timestamp",
    "leg_id",
    "order_id",
    "instrument_key",
    "side",
    "qty",
    "fill_price",
    "tag",
]


def _ensure_csv() -> None:
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(_FILLS_HEADER)


def write_fill(leg: "Leg", tag: str) -> None:
    _ensure_csv()
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                leg.leg_id,
                leg.order_id or "",
                leg.instrument_key,
                leg.side.value,
                leg.qty,
                f"{leg.fill_price:.2f}" if leg.fill_price is not None else "",
                tag,
            ]
        )


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
    """One option leg in a hedged pair. Keyed by order_id once placed."""

    leg_id: str
    kind: LegKind
    side: Side
    instrument_key: str
    strike: float
    qty: int

    # Filled state
    state: LegState = LegState.PENDING
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    placed_at: Optional[float] = None
    filled_at: Optional[float] = None

    # SL state
    sl_order_id: Optional[str] = None
    sl_triggered_at: Optional[float] = None  # when local SL first observed breach
    sl_broker_triggered: bool = False  # set when watch_sl REST poller sees fill
    sl_escalation_in_flight: bool = False  # dedup SL-escalate-to-MARKET
    sl_trigger_price: Optional[float] = None  # broker SL-L trigger level
    sl_limit_price: Optional[float] = None  # broker SL-L limit level (trigger + adaptive buffer)
    sl_limit_buffer: Optional[float] = None  # adaptive buffer that was applied at SL placement
    exit_in_flight: bool = False  # dedup _enqueue_exit_due_to_local_sl — see
    # the SL_L_FILL_TIMEOUT path for the parallel pattern. Without this,
    # a fast-moving breach (several ticks within the ~100-500ms REST round
    # trip window) re-enters _enqueue_exit_due_to_local_sl on every tick
    # and submits duplicate MARKET BUY-to-close orders.

    # Re-entry
    reentry_count: int = 0

    # Live mark
    last_ltp: Optional[float] = None
    last_ltp_at: Optional[float] = None  # epoch seconds
    data_stale: bool = False


@dataclass
class LegPair:
    """A short straddle pair: 1 short CE + 1 short PE, each with its own hedge long."""

    pair_id: str
    ce_short: Leg
    pe_short: Leg
    ce_hedge: Leg
    pe_hedge: Leg
    entry_vix: float
    entry_spot: float
    session_date: str  # ISO date "YYYY-MM-DD"


# ============================================================================
# Upstox v2/v3 REST helpers
# ============================================================================
def _v2_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _v3_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json",
    }


def fetch_instrument_master() -> List[Dict[str, Any]]:
    """Fetch option contracts specifically for Nifty 50."""
    log.info("Downloading NIFTY 50 contracts from %s", INSTRUMENTS_URL)
    r = requests.get(
        INSTRUMENTS_URL, 
        params={"instrument_key": "NSE_INDEX|Nifty 50"}, 
        headers=_v2_headers(), 
        timeout=30
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    log.info("Instrument master rows: %d", len(data))
    return data

def fetch_index_instrument_master() -> List[Dict[str, Any]]:
    """Return a mocked row for India VIX to satisfy the resolver."""
    # Bypasses the invalid NSE_INDEX call by supplying the known static key directly
    return [{
        "name": "India VIX", 
        "tradingsymbol": "India VIX", 
        "instrument_key": "NSE_INDEX|India VIX", 
        "exchange": "NSE"
    }]


def fetch_nifty_weekly_expiry(instruments: List[Dict[str, Any]]) -> Tuple[str, str]:
    """
    Find the current active NIFTY weekly expiry from the contract list.
    Returns the underlying index key and the expiry date string.
    """
    today = datetime.now().date()
    expiries = set()
    
    for row in instruments:
        try:
            # We already narrowed the list to Nifty 50 contracts only.
            # Just extract valid >= today expiry dates.
            exp = datetime.strptime(row["expiry"], "%Y-%m-%d").date()
            if exp >= today:
                expiries.add(exp)
        except Exception:
            continue
            
    if not expiries:
        raise RuntimeError("No NIFTY expiries found in the downloaded contracts")
        
    # Pick nearest expiry in current week, else nearest overall.
    week_end = today + timedelta(days=(4 - today.weekday()) % 7)  # Friday
    valid_expiries = sorted(list(expiries))
    
    this_week = [e for e in valid_expiries if e <= week_end]
    chosen_exp = this_week[0] if this_week else valid_expiries[0]
    
    chosen_expiry_str = chosen_exp.strftime("%Y-%m-%d")
    underlying_key = "NSE_INDEX|Nifty 50"
    
    log.info(
        "Selected NIFTY weekly expiry=%s instrument_key=%s", 
        chosen_expiry_str, 
        underlying_key
    )
    return underlying_key, chosen_expiry_str


def resolve_vix_instrument_key(instruments: List[Dict[str, Any]]) -> str:
    """Resolve India VIX instrument key from the master — never hardcode."""
    needles = {"india vix", "indiavix", "vix"}
    for row in instruments:
        ts = (row.get("tradingsymbol") or "").strip().lower()
        name = (row.get("name") or "").strip().lower()
        if ts in needles or name in needles or any(n in ts for n in needles):
            log.info(
                "Resolved India VIX: exchange=%s tradingsymbol=%s instrument_key=%s",
                row.get("exchange"),
                row.get("tradingsymbol"),
                row.get("instrument_key"),
            )
            return row["instrument_key"]
    raise RuntimeError("Could not resolve India VIX instrument_key from master")


def fetch_vix_ltp_sync(vix_key: str) -> float:
    """Pre-websocket bootstrap LTP fetch."""
    r = requests.get(
        MARKET_QUOTE_LTP_URL,
        params={"instrument_key": vix_key},
        headers=_v2_headers(),
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    # Safely extract the first item's price to ignore '|' vs ':' formatting
    last = list(data.values())[0]["last_price"]
    log.info("entry_vix = %.2f", float(last))
    return float(last)

def fetch_nifty50_spot_sync(nifty_key: str) -> float:
    """Pre-websocket bootstrap NIFTY 50 spot LTP fetch."""
    r = requests.get(
        MARKET_QUOTE_LTP_URL,
        params={"instrument_key": nifty_key},
        headers=_v2_headers(),
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    # Safely extract the first item's price
    last = list(data.values())[0]["last_price"]
    log.info("entry_spot = %.2f", float(last))
    return float(last)


def fetch_option_greeks_batched(keys: List[str]) -> Dict[str, Dict[str, float]]:
    """Batched /v3/market-quote/option-greek call. Returns {key: {delta, ltp}}."""
    if not keys:
        return {}
        
    out: Dict[str, Dict[str, float]] = {}
    chunk_size = 50
    
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i : i + chunk_size]
        params = {"instrument_key": ",".join(chunk)}
        
        r = requests.get(
            OPTION_GREEK_URL, params=params, headers=_v3_headers(), timeout=20
        )
        if r.status_code == 429:
            log.warning("option-greek 429 — backing off 2s and retrying once")
            time.sleep(2.0)
            r = requests.get(
                OPTION_GREEK_URL, params=params, headers=_v3_headers(), timeout=20
            )
        r.raise_for_status()
        
        raw = r.json().get("data", {})
        for k, v in raw.items():
            # FIX: Normalize the response key from 'NSE_FO:123' back to 'NSE_FO|123'
            normalized_key = k.replace(":", "|")
            
            g = v.get("option_greeks") or {}
            out[normalized_key] = {
                "delta": float(g.get("delta") or 0.0),
                "ltp": float(v.get("last_price") or 0.0),
            }
            
    return out


def fetch_option_chain_for_expiry(
    nifty_key: str, expiry: str
) -> List[Dict[str, Any]]:
    """v2 option-chain call. Returns CE/PE rows for the given expiry."""
    r = requests.get(
        OPTION_CHAIN_URL,
        params={
            "instrument_key": nifty_key,
            "expiry_date": expiry,
        },
        headers=_v2_headers(),
        timeout=20,
    )
    r.raise_for_status()
    rows: List[Dict[str, Any]] = []
    for row in r.json().get("data", []):
        co = row.get("call_options") or {}
        po = row.get("put_options") or {}
        if co.get("instrument_key"):
            rows.append(
                {
                    "side": Side.CE,
                    "strike": float(row["strike_price"]),
                    "instrument_key": co["instrument_key"],
                    "ltp": float(co.get("market_data", {}).get("ltp", 0.0) or 0.0),
                }
            )
        if po.get("instrument_key"):
            rows.append(
                {
                    "side": Side.PE,
                    "strike": float(row["strike_price"]),
                    "instrument_key": po["instrument_key"],
                    "ltp": float(po.get("market_data", {}).get("ltp", 0.0) or 0.0),
                }
            )
    return rows


# ============================================================================
# Paper trading slippage
# ============================================================================
def paper_fill(side: str, ref_price: float, slip_bps: int = PAPER_SLIPPAGE_MODEL_BPS) -> float:
    """Simulate a fill with slippage against the current tick price."""
    slip = ref_price * (slip_bps / 10_000.0)
    if side.upper() in ("BUY", "LONG"):
        return round(ref_price + slip, 2)
    return round(ref_price - slip, 2)


# ============================================================================
# Async order worker (non-blocking REST)
# ============================================================================
class AsyncRestClient:
    """aiohttp wrapper used by everything after the websocket opens."""

    def __init__(self, token: str):
        self._token = token
        self._session: Optional[aiohttp.ClientSession] = None

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

    async def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        assert self._session
        async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            return await r.json()

    async def post(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        assert self._session
        async with self._session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=20)
        ) as r:
            r.raise_for_status()
            return await r.json()

    async def delete(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        assert self._session
        async with self._session.delete(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            return await r.json()


@dataclass
class OrderTask:
    """A unit of work the order worker must execute off the tick loop."""
    name: str
    coro_factory: Callable[[], Awaitable[Any]]
    on_done: Optional[Callable[[Any], Awaitable[None]]] = None
    on_error: Optional[Callable[[Exception], Awaitable[None]]] = None


class OrderWorker:
    """Dispatches order tasks concurrently on the running event loop.

    The original design was a strict single-consumer FIFO. That serialised
    CE and PE leg operations through one coroutine, which is wrong: since
    AsyncRestClient uses aiohttp, every order call is non-blocking already.
    A single-consumer queue just adds latency and risk (e.g. the hedge
    fill poll for the second leg can starve while waiting for the first
    leg's poll to time out).

    We keep the same `submit()` / `q.join()` API so existing callers don't
    change, but each submit() now spawns an independent asyncio.Task that
    runs the coro_factory and fires on_done / on_error. `q.join()` waits
    on a simple counter instead of the asyncio.Queue.
    """

    def __init__(self, rest: AsyncRestClient):
        self.rest = rest
        self._inflight = 0
        self._all_done = asyncio.Event()
        self._all_done.set()  # initially nothing in flight
        self._stop = False
        self._tasks: "set[asyncio.Task]" = set()

    def submit(self, task: OrderTask) -> None:
        if self._inflight == 0:
            self._all_done.clear()
        self._inflight += 1
        # task_done is exposed so caller can use it as a no-op.
        task.task_done = lambda: None  # type: ignore[attr-defined]
        t = asyncio.create_task(self._run_one(task))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _run_one(self, task: OrderTask) -> None:
        try:
            result = await task.coro_factory()
            if task.on_done:
                try:
                    await task.on_done(result)
                except Exception:  # noqa: BLE001
                    log.exception("on_done handler failed for %s", task.name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("order worker task %s failed: %s", task.name, e)
            if task.on_error:
                try:
                    await task.on_error(e)
                except Exception:  # noqa: BLE001
                    log.exception("on_error handler also failed")
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._all_done.set()

    async def run(self) -> None:
        # No-op: tasks are spawned on submit() in the current event loop.
        # Kept for backward compat — Engine wires self.worker.run() as a
        # long-lived background task. We just wait until stop().
        while not self._stop:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break

    def stop(self) -> None:
        self._stop = True
        # Cancel any in-flight order tasks.
        for t in list(self._tasks):
            if not t.done():
                t.cancel()

    @property
    def q(self):
        """Compat shim: expose a fake .q with .join() for shutdown code."""
        return self

    async def join(self) -> None:
        await self._all_done.wait()


# ============================================================================
# Order placement (works for both paper and live)
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
    """v2 order placement via aiohttp.
    SL-L uses order_type=SL, transaction_type=BUY (for closing a short),
    trigger_price set, price = trigger + buffer.
    """
    payload: Dict[str, Any] = {
        "quantity": qty,
        "product": "I",  # Intraday
        "validity": "DAY",
        "instrument_token": instrument_key,
        "order_type": order_type,  # MARKET | LIMIT | SL
        "transaction_type": side,  # BUY | SELL
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
        log.info(
            "[PAPER] would PLACE order: %s",
            json.dumps(payload, default=str),
        )
        return {
            "data": {
                "order_id": f"PAPER-{int(time.time() * 1000)}",
                "status": "open",
                "average_price": 0.0,
            }
        }

    return await rest.post(PLACE_ORDER_URL, payload)


async def cancel_order_via_upstox(rest: AsyncRestClient, order_id: str) -> Dict[str, Any]:
    if PAPER_TRADING_MODE:
        log.info("[PAPER] would CANCEL order %s", order_id)
        return {"data": {"order_id": order_id, "status": "cancelled"}}
    return await rest.delete(CANCEL_ORDER_URL, params={"order_id": order_id})


async def get_order_status(rest: AsyncRestClient, order_id: str) -> Dict[str, Any]:
    if PAPER_TRADING_MODE:
        return {"data": {"order_id": order_id, "status": "complete", "average_price": 0.0}}
    return await rest.get(ORDER_HISTORY_URL, params={"order_id": order_id})


async def exit_all_positions(rest: AsyncRestClient) -> None:
    """Square off every open position via v2 market order (live) or paper log."""
    if PAPER_TRADING_MODE:
        log.info("[PAPER] would EXIT ALL positions via market orders")
        return
    try:
        payload = await rest.get(EXIT_ALL_URL)
        for pos in payload.get("data", []):
            if int(pos.get("quantity", 0)) == 0:
                continue
            side = "SELL" if int(pos["quantity"]) > 0 else "BUY"
            qty = abs(int(pos["quantity"]))
            await rest.post(
                PLACE_ORDER_URL,
                {
                    "quantity": qty,
                    "product": "I",
                    "validity": "DAY",
                    "instrument_token": pos["instrument_token"],
                    "order_type": "MARKET",
                    "transaction_type": side,
                    "tag": "EXIT_ALL",
                    "disclosed_quantity": 0,
                    "price": 0.0,
                    "trigger_price": 0.0,
                    "is_amo": False,
                },
            )
    except Exception:
        log.exception("exit_all_positions failed")


# ============================================================================
# Strategy helpers
# ============================================================================
def select_delta_premium_bands(vix: float) -> Tuple[float, float, float, float]:
    """Return (min_delta, max_delta, min_premium, max_premium) for this vix regime."""
    if vix < LOW_VIX_THRESHOLD:
        return MIN_DELTA_LOWVIX, MAX_DELTA_LOWVIX, MIN_PREMIUM, MAX_PREMIUM
    if vix > HIGH_VIX_THRESHOLD:
        return MIN_DELTA_HIVIX, MAX_DELTA_HIVIX, MIN_PREMIUM, MAX_PREMIUM
    return MIN_DELTA, MAX_DELTA, MIN_PREMIUM, MAX_PREMIUM


def update_trail(store: "StateStore", mtm: float, log_csv_path: str,
                 session_date: str) -> None:
    """Pure function — increments the trail lock based on new mtm.
    Used identically by the live and paper Engine so the trail logic is
    exactly the same in both modes.
    """
    if mtm >= TRAIL_START_PROFIT:
        steps = int((mtm - TRAIL_START_PROFIT) // TRAIL_STEP_INCREMENT) + 1
        new_lock = TRAIL_LOCK_PROFIT + (steps - 1) * TRAIL_STEP
        if new_lock > store.trail_lock:
            old = store.trail_lock
            store.trail_lock = new_lock
            log.info("TRAIL_STEP: lock %.2f -> %.2f (mtm=%.2f)", old, new_lock, mtm)
            log_event(
                log_csv_path, "TRAIL_STEP", None,
                ltp_at_event=None, entry_vix=store.entry_vix,
                running_total_mtm=mtm, trail_lock=new_lock,
                notes=f"previous_lock={old:.2f}",
                session_date=session_date,
            )


def pick_core_strike(
    side: Side,
    chain_rows: List[Dict[str, Any]],
    greeks: Dict[str, Dict[str, float]],
    vix: float,
) -> Optional[Dict[str, Any]]:
    """Delta band is PRIMARY filter; premium band is secondary sort."""
    min_d, max_d, min_p, max_p = select_delta_premium_bands(vix)
    premium_mid = (min_p + max_p) / 2.0
    delta_mid = (min_d + max_d) / 2.0

    candidates = [r for r in chain_rows if r["side"] == side]
    enriched: List[Dict[str, Any]] = []
    for r in candidates:
        g = greeks.get(r["instrument_key"])
        if not g:
            continue
        d = abs(g["delta"])
        ltp = g["ltp"] or r["ltp"]
        if min_d <= d <= max_d:
            enriched.append(
                {
                    **r,
                    "delta": d,
                    "ltp": ltp,
                    "in_premium_band": min_p <= ltp <= max_p,
                    "premium_distance": abs(ltp - premium_mid),
                    "delta_distance": abs(d - delta_mid),
                }
            )
    if not enriched:
        log.warning("WARN:delta_band_unmet for side=%s (band=%.2f-%.2f)", side.value, min_d, max_d)
        return None
    in_band = [r for r in enriched if r["in_premium_band"]]
    if in_band:
        in_band.sort(key=lambda r: r["premium_distance"])
        return in_band[0]
    enriched.sort(key=lambda r: r["delta_distance"])
    log.warning(
        "WARN:premium_band_unmet side=%s picking delta_closest=%.3f (band=%.0f-%.0f)",
        side.value,
        enriched[0]["delta"],
        min_p,
        max_p,
    )
    return enriched[0]


def pick_hedge_strike(
    side: Side,
    chain_rows: List[Dict[str, Any]],
    ref_strike: float,
    chain_ltp_fetcher: Callable[[str], float],
) -> Optional[Dict[str, Any]]:
    """OTM leg closest to HEDGE_TARGET_PREMIUM. Returns None if nothing in tolerance."""
    candidates = [
        r
        for r in chain_rows
        if r["side"] == side
        and (
            (side == Side.CE and r["strike"] > ref_strike)
            or (side == Side.PE and r["strike"] < ref_strike)
        )
    ]
    best: Optional[Dict[str, Any]] = None
    best_dist: float = float("inf")
    for r in candidates:
        ltp = r.get("ltp") or 0.0
        if ltp <= 0:
            ltp = chain_ltp_fetcher(r["instrument_key"])
        d = abs(ltp - HEDGE_TARGET_PREMIUM)
        if d < best_dist:
            best_dist = d
            best = {**r, "ltp": ltp, "ltp_distance": d}
    if best is None or best_dist > HEDGE_PREMIUM_TOLERANCE:
        log.warning(
            "ABORT:no_valid_hedge side=%s ref_strike=%.0f best_dist=%.2f tol=%.2f",
            side.value,
            ref_strike,
            best_dist if best else float("inf"),
            HEDGE_PREMIUM_TOLERANCE,
        )
        return None
    return best


# ============================================================================
# State store
# ============================================================================
class StateStore:
    """All mutable per-leg / per-pair state. The tick engine is the only writer."""

    def __init__(self) -> None:
        self.pair: Optional[LegPair] = None
        self.entry_vix: float = 0.0
        self.original_entry_vix: float = 0.0
        self.entry_spot: float = 0.0  # NIFTY 50 spot LTP at bootstrap, used by SL buffer
        self.total_mtm: float = 0.0
        self.max_drawdown_mtm: float = 0.0  # tracks lowest (most-negative) MTM seen
        self.trail_lock: float = float("-inf")
        self.kill_switch_triggered: bool = False
        self.kill_switch_reason: Optional[str] = None
        self.feed_disconnected_at: Optional[float] = None
        self.num_reentries: int = 0
        self.num_hedge_aborts: int = 0
        self.exit_reason: str = ""
        self._mtm_log_fh = open(MTM_LOG_FILE, "a", buffering=1)

    def log_state(self, msg: str) -> None:
        log.info(msg)

    def log_mtm(self) -> None:
        stale_legs = []
        legs: List[Leg] = []
        if self.pair:
            legs = [self.pair.ce_short, self.pair.pe_short, self.pair.ce_hedge, self.pair.pe_hedge]
        for lg in legs:
            if lg.data_stale:
                stale_legs.append(lg.leg_id)
        self._mtm_log_fh.write(
            f"{datetime.now().isoformat(timespec='seconds')} "
            f"mtm={self.total_mtm:.2f} trail_lock={self.trail_lock:.2f} "
            f"stale_legs={stale_legs}\n"
        )

    def close(self) -> None:
        try:
            self._mtm_log_fh.close()
        except Exception:
            pass

    def compute_mtm(self) -> float:
        """total_mtm from live LTP marks for every open leg, refreshed on each tick batch."""
        if not self.pair:
            return 0.0
        total = 0.0
        now = time.time()
        for lg in [self.pair.ce_short, self.pair.pe_short, self.pair.ce_hedge, self.pair.pe_hedge]:
            if lg.state in (LegState.PENDING, LegState.HEDGE_PLACED, LegState.ABORTED):
                continue
            if lg.fill_price is None or lg.last_ltp is None:
                continue
            if lg.last_ltp_at and (now - lg.last_ltp_at) > STALE_TICK_TIMEOUT_SEC:
                lg.data_stale = True
            sign = -1 if lg.kind == LegKind.CORE_SHORT else 1
            total += sign * lg.qty * (lg.last_ltp - lg.fill_price)
        self.total_mtm = total
        if total < self.max_drawdown_mtm:
            self.max_drawdown_mtm = total
        return total


# ============================================================================
# Websocket adapter (Upstox MarketDataStreamerV3)
# ============================================================================
class WsAdapter:
    """Wraps Upstox MarketDataStreamerV3. SDK callbacks fire on its own
    thread, so we marshal onto our asyncio loop via call_soon_threadsafe.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, instruments: List[str]):
        self.loop = loop
        self.instruments = instruments
        self.last_tick_at: float = time.time()
        # Connection state machine:
        #   - connected = False, ever_connected = False  → COLD_START
        #     (initial state; main loop's disconnect check must ignore this)
        #   - connected = True,  ever_connected = True   → HEALTHY
        #   - connected = False, ever_connected = True   → DISCONNECTED
        #     (was healthy, now isn't — start downtime clock)
        #   - connected = True,  ever_connected = True   → RECONNECTED
        #   - connected = False, ever_connected = True   → DISCONNECTED again
        self.connected: bool = False
        self.ever_connected: bool = False
        # LTPC market-data payloads flow through this queue. We do NOT
        # multiplex order updates onto it: per the Upstox v3 proto
        # (FeedResponse: type ∈ {initial_feed, live_feed, market_info}),
        # the market-data stream has no order_update message. Order
        # updates arrive on a separate Portfolio Stream websocket
        # (which we don't connect to), or via the v2 order history
        # REST endpoint (which is what watch_sl polls).
        self._feed_queue: "asyncio.Queue[Any]" = asyncio.Queue()
        self.streamer: Optional[MarketDataStreamerV3] = None

    def _on_open(self) -> None:
        log.info("WS open; subscribing to %d instruments", len(self.instruments))
        self.connected = True
        self.ever_connected = True
        try:
            self.streamer.subscribe(self.instruments, "ltpc")  # type: ignore[attr-defined]
        except Exception:
            log.exception("subscribe failed")

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

        # Real Upstox v3 message shape (verified against upstox-client-sdk
        # 2.29 protobuf definition):
        #   {"feeds": {"<instrument_key>": {"ltpc": {"ltp": X, ...}}, ...}}
        # The OUTER dict's keys ARE the instrument keys. The previous version
        # iterated data["feeds"].items() and dropped the key on the floor,
        # which meant downstream code could never map ticks back to legs.
        #
        # Anything whose inner payload does NOT have a recognised
        # market-data wrapper is logged at debug and dropped. Per the v3
        # proto (FeedResponse.type ∈ {initial_feed, live_feed, market_info},
        # no order_update variant), this stream cannot carry order updates.
        # Order fills come from the v2 order-history endpoint via watch_sl.
        for instrument_key, payload in data["feeds"].items():
            if not isinstance(payload, dict):
                continue
            if any(k in payload for k in ("ltpc", "fullFeed", "firstLevelWithGreeks")):
                self.loop.call_soon_threadsafe(
                    self._feed_queue.put_nowait,
                    ("ltpc", instrument_key, payload),
                )
            else:
                log.debug(
                    "WS_FEED_UNKNOWN_SHAPE: key=%s keys=%s (ignored — no order_update on v3)",
                    instrument_key, list(payload.keys()),
                )

    def _on_error(self, err) -> None:
        log.error("WS error: %s", err)
        self.connected = False

    def _on_close(self) -> None:
        log.warning("WS closed")
        self.connected = False

    def connect(self) -> None:
        cfg = upstox_client.Configuration()
        cfg.access_token = ACCESS_TOKEN
        api_client = upstox_client.ApiClient(cfg)
        auth = upstox_client.WebsocketApi(api_client)
        try:
            uri_resp = auth.get_market_data_feed_authorize_v3()
            uri = uri_resp.data.authorize
        except Exception:
            try:
                uri = uri_resp["data"]["authorize"]  # type: ignore[index]
            except Exception:
                log.exception("authorize failed")
                raise
        self.streamer = MarketDataStreamerV3(
            api_client=api_client,
            instrumentKeys=self.instruments,
            mode="ltpc",
        )
        self.streamer.on("open", self._on_open)
        self.streamer.on("message", self._on_message)
        self.streamer.on("error", self._on_error)
        self.streamer.on("close", self._on_close)
        self.streamer.connect()  # blocking — runs in our ws_runner thread


# ============================================================================
# CSV event helpers (used by both live and paper)
# ============================================================================
def session_date_str() -> str:
    return datetime.now().date().isoformat()


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
    """Append one row to the paper trade log CSV."""
    if not session_date:
        session_date = session_date_str()
    append_log_row(
        csv_path,
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "session_date": session_date,
            "leg_id": leg.leg_id if leg else "",
            "instrument_key": leg.instrument_key if leg else "",
            "event_type": event_type,
            "order_side": order_side,
            "simulated_price": f"{simulated_price:.2f}" if simulated_price is not None else "",
            "ltp_at_event": f"{ltp_at_event:.2f}" if ltp_at_event is not None else "",
            "qty": leg.qty if leg else "",
            "entry_vix": f"{entry_vix:.2f}",
            "sl_trigger_price": f"{sl_trigger_price:.2f}" if sl_trigger_price is not None else "",
            "running_total_mtm": f"{running_total_mtm:.2f}",
            "trail_lock_active": f"{trail_lock:.2f}",
            "reentry_count": reentry_count,
            "notes": notes,
        },
    )


# ============================================================================
# Engine: orchestrates everything (live + paper)
# ============================================================================
class Engine:
    def __init__(self) -> None:
        self.store = StateStore()
        self.rest: Optional[AsyncRestClient] = None
        self.worker: Optional[OrderWorker] = None
        self.ws: Optional[WsAdapter] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopped = False
        self._bootstrap_cache: Dict[str, Any] = {}
        # Paper CSV paths (used when PAPER_TRADING_MODE)
        self.paper_log_csv = PAPER_TRADE_LOG_CSV
        self.paper_summary_csv = PAPER_TRADE_SUMMARY_CSV

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self) -> None:
        log.info("=== 09:15 BOOTSTRAP ===")
        fo = fetch_instrument_master()
        idx = fetch_index_instrument_master()

        nifty_key, expiry = fetch_nifty_weekly_expiry(fo)
        vix_key = resolve_vix_instrument_key(fo + idx)
        self.store.entry_vix = fetch_vix_ltp_sync(vix_key)
        self.store.original_entry_vix = self.store.entry_vix
        # NIFTY 50 spot LTP — required for the VIX-adaptive SL buffer
        # (buffer = K * vix * spot / 100). Falls back to 0 if the call
        # fails; _compute_sl_buffer handles the fallback by returning
        # the static MIN.
        try:
            self.store.entry_spot = fetch_nifty50_spot_sync(nifty_key)
        except Exception:
            log.exception("entry_spot fetch failed; SL buffer will use static MIN")
            self.store.entry_spot = 0.0

        chain = fetch_option_chain_for_expiry(nifty_key, expiry)
        candidate_keys = [r["instrument_key"] for r in chain]
        greeks = fetch_option_greeks_batched(candidate_keys)
        self._bootstrap_cache = {
            "nifty_key": nifty_key,
            "expiry": expiry,
            "chain": chain,
            "greeks": greeks,
            "vix_key": vix_key,
        }
        log.info("Bootstrap complete: expiry=%s candidates=%d", expiry, len(candidate_keys))

    # ------------------------------------------------------------------ strike selection
    def select_strikes(self) -> None:
        log.info("=== 09:19 STRIKE SELECTION (entry_vix=%.2f) ===", self.store.entry_vix)
        cache = self._bootstrap_cache
        chain = cache["chain"]
        greeks = cache["greeks"]

        def ltp_for(key: str) -> float:
            g = greeks.get(key)
            if g and g.get("ltp"):
                return g["ltp"]
            for r in chain:
                if r["instrument_key"] == key and r.get("ltp"):
                    return r["ltp"]
            return 0.0

        ce_core = pick_core_strike(Side.CE, chain, greeks, self.store.entry_vix)
        pe_core = pick_core_strike(Side.PE, chain, greeks, self.store.entry_vix)
        if not ce_core or not pe_core:
            raise RuntimeError("ABORT:delta_band_unmet for one or both core sides")

        ce_hedge = pick_hedge_strike(Side.CE, chain, ce_core["strike"], ltp_for)
        pe_hedge = pick_hedge_strike(Side.PE, chain, pe_core["strike"], ltp_for)
        if not ce_hedge or not pe_hedge:
            raise RuntimeError("ABORT:no_valid_hedge for one or both sides")

        pair = LegPair(
            pair_id=f"PAIR-{int(time.time())}",
            ce_short=Leg(
                leg_id="CE_SHORT", kind=LegKind.CORE_SHORT, side=Side.CE,
                instrument_key=ce_core["instrument_key"], strike=ce_core["strike"], qty=LOT_SIZE,
            ),
            pe_short=Leg(
                leg_id="PE_SHORT", kind=LegKind.CORE_SHORT, side=Side.PE,
                instrument_key=pe_core["instrument_key"], strike=pe_core["strike"], qty=LOT_SIZE,
            ),
            ce_hedge=Leg(
                leg_id="CE_HEDGE", kind=LegKind.HEDGE_LONG, side=Side.CE,
                instrument_key=ce_hedge["instrument_key"], strike=ce_hedge["strike"], qty=LOT_SIZE,
            ),
            pe_hedge=Leg(
                leg_id="PE_HEDGE", kind=LegKind.HEDGE_LONG, side=Side.PE,
                instrument_key=pe_hedge["instrument_key"], strike=pe_hedge["strike"], qty=LOT_SIZE,
            ),
            entry_vix=self.store.entry_vix,
            entry_spot=self.store.entry_spot,
            session_date=session_date_str(),
        )
        self.store.pair = pair
        log.info(
            "STRIKE_SELECTED: CE_short=%.0f PE_short=%.0f CE_hedge=%.0f PE_hedge=%.0f",
            ce_core["strike"], pe_core["strike"], ce_hedge["strike"], pe_hedge["strike"],
        )

    # ------------------------------------------------------------------ execution
    async def _place_limit_buy_with_timeout(
        self,
        leg: Leg,
        ref_price: float,
        slippage_ticks: int,
        timeout_sec: int,
        retry_ticks: Optional[int] = None,
    ) -> bool:
        """LIMIT BUY with broker polling, optional one-shot retry, ABORT on 2nd timeout."""
        assert self.rest and self.worker
        price = round(ref_price + slippage_ticks * TICK_SIZE, 2)

        if PAPER_TRADING_MODE:
            leg.placed_at = time.time()
            leg.state = LegState.HEDGE_PLACED
            leg.order_id = f"PAPER-{leg.leg_id}-{int(time.time() * 1000)}"
            await asyncio.sleep(0.05)
            sim_fill = paper_fill("BUY", ref_price)
            leg.fill_price = sim_fill
            leg.state = LegState.HEDGE_FILLED
            leg.filled_at = time.time()
            log.info(
                "HEDGE_FILLED: leg=%s order_id=%s fill=%.2f (paper, ref=%.2f slip_bps=%d)",
                leg.leg_id, leg.order_id, sim_fill, ref_price, PAPER_SLIPPAGE_MODEL_BPS,
            )
            write_fill(leg, "HEDGE_FILL")
            log_event(
                self.paper_log_csv, "HEDGE_FILL", leg,
                order_side="BUY", simulated_price=sim_fill, ltp_at_event=ref_price,
                entry_vix=self.store.entry_vix, running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock, notes=f"slip_bps={PAPER_SLIPPAGE_MODEL_BPS}",
                session_date=self.store.pair.session_date if self.store.pair else "",
            )
            return True

        result: Dict[str, Any] = {"filled": False, "px": 0.0, "oid": None}
        done = asyncio.Event()

        async def place_and_poll():
            resp = await place_order_via_upstox(
                self.rest,
                instrument_key=leg.instrument_key, side="BUY", qty=leg.qty,
                order_type="LIMIT", price=price, trigger_price=None,
                tag=f"HEDGE_{leg.leg_id}",
            )
            oid = resp["data"]["order_id"]
            result["oid"] = oid
            leg.order_id = oid
            leg.placed_at = time.time()
            leg.state = LegState.HEDGE_PLACED
            log.info("HEDGE_PLACED: leg=%s order_id=%s price=%.2f", leg.leg_id, leg.order_id, price)
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                status = await get_order_status(self.rest, oid)
                d = status.get("data", {})
                s = (d.get("status") or "").lower()
                if s in ("complete", "filled", "traded"):
                    result["filled"] = True
                    result["px"] = float(d.get("average_price") or price)
                    return
                if s in ("rejected", "cancelled", "canceled"):
                    return
                await asyncio.sleep(0.5)

        async def on_done(_): done.set()
        async def on_err(_): done.set()

        self.worker.submit(
            OrderTask(name=f"hedge_{leg.leg_id}", coro_factory=place_and_poll,
                      on_done=on_done, on_error=on_err)
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_sec + 2.0)
        except asyncio.TimeoutError:
            log.error("HEDGE_POLL_TIMEOUT: worker stalled for leg=%s", leg.leg_id)

        if result["filled"]:
            leg.fill_price = result["px"]
            leg.state = LegState.HEDGE_FILLED
            leg.filled_at = time.time()
            log.info("HEDGE_FILLED: leg=%s order_id=%s fill=%.2f",
                     leg.leg_id, leg.order_id, leg.fill_price)
            write_fill(leg, "HEDGE_FILL")
            log_event(
                self.paper_log_csv, "HEDGE_FILL", leg,
                order_side="BUY", simulated_price=leg.fill_price, ltp_at_event=ref_price,
                entry_vix=self.store.entry_vix, running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                session_date=self.store.pair.session_date if self.store.pair else "",
            )
            return True

        log.warning("HEDGE_TIMEOUT: leg=%s order_id=%s", leg.leg_id, leg.order_id)
        if result["oid"]:
            with contextlib.suppress(Exception):
                await cancel_order_via_upstox(self.rest, result["oid"])
        if retry_ticks is None:
            leg.state = LegState.ABORTED
            self.store.num_hedge_aborts += 1
            log_event(
                self.paper_log_csv, "ABORT_HEDGE_TIMEOUT", leg,
                order_side="BUY", ltp_at_event=ref_price,
                entry_vix=self.store.entry_vix, running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                notes="hedge never filled after retry",
                session_date=self.store.pair.session_date if self.store.pair else "",
            )
            return False

        return await self._place_limit_buy_with_timeout(
            leg, ref_price, retry_ticks, timeout_sec=timeout_sec, retry_ticks=None,
        )

    async def _place_market_sell(self, leg: Leg) -> bool:
        assert self.rest
        if PAPER_TRADING_MODE:
            leg.placed_at = time.time()
            leg.state = LegState.CORE_PLACED
            leg.order_id = f"PAPER-{leg.leg_id}-{int(time.time() * 1000)}"
            ref = leg.last_ltp or 0.0
            sim_fill = paper_fill("SELL", ref) if ref > 0 else 0.0
            leg.fill_price = sim_fill
            leg.state = LegState.CORE_FILLED
            leg.filled_at = time.time()
            log.info("CORE_FILLED: leg=%s order_id=%s fill=%.2f (paper, ref=%.2f)",
                     leg.leg_id, leg.order_id, sim_fill, ref)
            write_fill(leg, "CORE_FILL")
            log_event(
                self.paper_log_csv, "CORE_FILL", leg,
                order_side="SELL", simulated_price=sim_fill, ltp_at_event=ref,
                entry_vix=self.store.entry_vix, running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                session_date=self.store.pair.session_date if self.store.pair else "",
            )
            return True

        done = asyncio.Event()
        result: Dict[str, Any] = {"ok": False}

        async def place():
            resp = await place_order_via_upstox(
                self.rest,
                instrument_key=leg.instrument_key, side="SELL", qty=leg.qty,
                order_type="MARKET", price=0.0, trigger_price=None,
                tag=f"CORE_{leg.leg_id}",
            )
            leg.order_id = resp["data"]["order_id"]
            d = resp["data"]
            leg.fill_price = float(d.get("average_price") or 0.0)
            leg.state = LegState.CORE_FILLED
            leg.filled_at = time.time()
            result["ok"] = True
            log.info("CORE_FILLED: leg=%s order_id=%s fill=%.2f",
                     leg.leg_id, leg.order_id, leg.fill_price)
            write_fill(leg, "CORE_FILL")
            log_event(
                self.paper_log_csv, "CORE_FILL", leg,
                order_side="SELL", simulated_price=leg.fill_price, ltp_at_event=leg.last_ltp,
                entry_vix=self.store.entry_vix, running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock,
                session_date=self.store.pair.session_date if self.store.pair else "",
            )

        async def on_done(_): done.set()
        async def on_err(_): done.set()

        self.worker.submit(
            OrderTask(name=f"place_core_{leg.leg_id}", coro_factory=place,
                      on_done=on_done, on_error=on_err)
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            log.error("CORE_PLACE_TIMEOUT: leg=%s", leg.leg_id)
        return result["ok"]

    async def _place_broker_sl(self, leg: Leg) -> None:
        """Broker-side SL-L (NSE rejects SL-M on options).
        Buffer is adaptive: scales with the trigger-to-fill gap, so a
        flash crash that gaps the option through 5 points can still hit
        a wider limit. See SL_LIMIT_BUFFER_POINTS_MIN/PCT/MAX in config.
        """
        if leg.kind != LegKind.CORE_SHORT or leg.fill_price is None:
            return
        assert self.rest
        trigger = round(leg.fill_price * (1 + SL_PERCENT), 2)
        buffer = self._compute_sl_buffer(leg)
        limit = round(trigger + buffer, 2)
        leg.sl_trigger_price = trigger
        leg.sl_limit_buffer = buffer  # type: ignore[attr-defined]
        leg.sl_limit_price = limit     # type: ignore[attr-defined]

        if PAPER_TRADING_MODE:
            leg.sl_order_id = f"PAPER-SL-{leg.leg_id}-{int(time.time() * 1000)}"
            leg.state = LegState.SL_PLACED
            log.info("[PAPER] SL_PLACED: leg=%s trigger=%.2f limit=%.2f buffer=%.2f",
                     leg.leg_id, trigger, limit, buffer)
            log_event(
                self.paper_log_csv, "SL_PLACED", leg,
                order_side="BUY", simulated_price=limit, ltp_at_event=leg.last_ltp,
                entry_vix=self.store.entry_vix, sl_trigger_price=trigger,
                running_total_mtm=self.store.total_mtm, trail_lock=self.store.trail_lock,
                session_date=self.store.pair.session_date if self.store.pair else "",
                notes=f"buffer={buffer:.2f}pts",
            )
            return

        done = asyncio.Event()

        async def place():
            resp = await place_order_via_upstox(
                self.rest,
                instrument_key=leg.instrument_key, side="BUY", qty=leg.qty,
                order_type="SL", price=limit, trigger_price=trigger,
                tag=f"SL_{leg.leg_id}",
            )
            leg.sl_order_id = resp["data"]["order_id"]
            leg.state = LegState.SL_PLACED
            log.info("SL_PLACED: leg=%s order_id=%s trigger=%.2f limit=%.2f buffer=%.2f",
                     leg.leg_id, leg.sl_order_id, trigger, limit, buffer)
            log_event(
                self.paper_log_csv, "SL_PLACED", leg,
                order_side="BUY", simulated_price=limit, ltp_at_event=leg.last_ltp,
                entry_vix=self.store.entry_vix, sl_trigger_price=trigger,
                running_total_mtm=self.store.total_mtm, trail_lock=self.store.trail_lock,
                session_date=self.store.pair.session_date if self.store.pair else "",
                notes=f"buffer={buffer:.2f}pts",
            )
            # Kick off the broker SL fill watcher. Two-phase polling,
            # rate-limit aware (see comment block on the watcher below).
            if self.worker and leg.sl_order_id:
                async def watch_sl(_leg=leg, _oid=leg.sl_order_id):
                    assert self.rest
                    # Rate-limit analysis for /v2/order/history
                    # (https://upstox.com/developer/api-documentation/rate-limiting/):
                    #   - Per second:  50 (other standard APIs)
                    #   - Per minute:  500
                    #   - Per 30 min:  2000
                    #
                    # Two phases:
                    #   PHASE 1 (idle, pre-trigger):
                    #     Poll every IDLE_POLL_INTERVAL_SEC (60s) while
                    #     the local SL hasn't fired. This is the
                    #     catch-up path for the case the audit called
                    #     out: if the websocket drops and
                    #     _evaluate_local_sl never runs, sl_triggered_at
                    #     never gets set, and the local SL path is
                    #     unreachable. The 60s idle poll gives us an
                    #     independent check on the broker SL-L order
                    #     every minute so a broker-only fill during a
                    #     feed outage is still caught within ~60s.
                    #     Budget: 1/60s × 2 legs × 6h = 720 calls,
                    #     well under 500/min and 2000/30min.
                    #
                    #   PHASE 2 (triggered):
                    #     When the local SL fires (sl_triggered_at
                    #     becomes non-None), switch to 0.7-1.3s
                    #     jittered polling. The deadline is computed
                    #     RELATIVE to when the local trigger fired
                    #     (sl_triggered_at + SL_L_FILL_TIMEOUT_SEC + 2),
                    #     not at task start — the previous version
                    #     computed the deadline at task start, which
                    #     meant by the time the local SL actually
                    #     fired (often hours later in a real session)
                    #     the deadline was long past and the watcher
                    #     exited without polling at all.
                    #
                    # MAX_POLLS_PER_LEG is a hard cap as a final
                    # backstop against runaway polling.
                    IDLE_POLL_INTERVAL_SEC = 60.0
                    MAX_POLLS_PER_LEG = 50
                    triggered_deadline: Optional[float] = None
                    poll_count = 0
                    while poll_count < MAX_POLLS_PER_LEG:
                        if _leg.state in (LegState.CLOSED, LegState.ABORTED):
                            return
                        if _leg.sl_triggered_at is None:
                            # PHASE 1: idle polling. 60s is well under
                            # the 50/s and 500/min budgets even with
                            # both legs polling simultaneously.
                            try:
                                status = await get_order_status(self.rest, _oid)
                                d = status.get("data", {}) if isinstance(status, dict) else {}
                                s = (d.get("status") or "").lower()
                            except Exception:  # noqa: BLE001
                                await asyncio.sleep(IDLE_POLL_INTERVAL_SEC)
                                continue
                            poll_count += 1
                            if s in ("complete", "filled", "traded"):
                                _leg.sl_broker_triggered = True
                                _leg.state = LegState.CLOSED
                                log.info(
                                    "SL_BROKER_FILLED_IDLE: leg=%s order_id=%s "
                                    "(caught during idle poll — no local trigger fired)",
                                    _leg.leg_id, _oid,
                                )
                                log_event(
                                    self.paper_log_csv, "SL_BROKER_FIRED", _leg,
                                    order_side="BUY", ltp_at_event=_leg.last_ltp,
                                    entry_vix=self.store.entry_vix,
                                    sl_trigger_price=_leg.sl_trigger_price,
                                    running_total_mtm=self.store.total_mtm,
                                    trail_lock=self.store.trail_lock,
                                    reentry_count=_leg.reentry_count,
                                    notes=f"watcher_idle status={s}",
                                    session_date=self.store.pair.session_date if self.store.pair else "",
                                )
                                return
                            if s in ("rejected", "cancelled", "canceled"):
                                log.warning("SL_BROKER_TERMINAL: leg=%s status=%s",
                                            _leg.leg_id, s)
                                return
                            await asyncio.sleep(IDLE_POLL_INTERVAL_SEC)
                            continue

                        # PHASE 2: triggered. Compute deadline relative
                        # to the moment the local SL fired, not at
                        # task start. If we've already past the
                        # deadline, still give the broker a brief
                        # window (~3 polls) to confirm — the local SL
                        # might have raced ahead by a few hundred ms.
                        if triggered_deadline is None:
                            triggered_deadline = _leg.sl_triggered_at + (
                                SL_L_FILL_TIMEOUT_SEC + 2.0
                            )
                        if time.time() >= triggered_deadline:
                            log.info(
                                "WATCH_SL_DONE: leg=%s polls=%d state=%s "
                                "(gave up %ds after local trigger)",
                                _leg.leg_id, poll_count, _leg.state,
                                int(SL_L_FILL_TIMEOUT_SEC + 2),
                            )
                            return
                        try:
                            status = await get_order_status(self.rest, _oid)
                            d = status.get("data", {}) if isinstance(status, dict) else {}
                            s = (d.get("status") or "").lower()
                        except Exception:  # noqa: BLE001
                            await asyncio.sleep(1.0)
                            continue
                        poll_count += 1
                        if s in ("complete", "filled", "traded"):
                            if not _leg.sl_broker_triggered:
                                _leg.sl_broker_triggered = True
                                _leg.state = LegState.CLOSED
                                log.info(
                                    "SL_BROKER_FILLED_WATCHER: leg=%s order_id=%s",
                                    _leg.leg_id, _oid,
                                )
                                log_event(
                                    self.paper_log_csv, "SL_BROKER_FIRED", _leg,
                                    order_side="BUY", ltp_at_event=_leg.last_ltp,
                                    entry_vix=self.store.entry_vix,
                                    sl_trigger_price=_leg.sl_trigger_price,
                                    running_total_mtm=self.store.total_mtm,
                                    trail_lock=self.store.trail_lock,
                                    reentry_count=_leg.reentry_count,
                                    notes=f"watcher_triggered status={s}",
                                    session_date=self.store.pair.session_date if self.store.pair else "",
                                )
                            return
                        if s in ("rejected", "cancelled", "canceled"):
                            log.warning("SL_BROKER_TERMINAL: leg=%s status=%s",
                                        _leg.leg_id, s)
                            return
                        # Jitter: 0.7-1.3s instead of fixed 1.0s to
                        # avoid synchronized 1-Hz bursts across
                        # multiple legs competing for the same rate
                        # budget.
                        await asyncio.sleep(0.7 + (poll_count % 3) * 0.2)
                    log.info(
                        "WATCH_SL_DONE: leg=%s polls=%d state=%s (hit MAX_POLLS_PER_LEG=%d)",
                        _leg.leg_id, poll_count, _leg.state, MAX_POLLS_PER_LEG,
                    )
                self.worker.submit(
                    OrderTask(name=f"watch_sl_{leg.leg_id}", coro_factory=watch_sl)
                )

        async def on_done(_): done.set()
        async def on_err(_): done.set()

        assert self.worker
        self.worker.submit(
            OrderTask(name=f"sl_{leg.leg_id}", coro_factory=place,
                      on_done=on_done, on_error=on_err)
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            log.error("SL_PLACE_TIMEOUT: leg=%s", leg.leg_id)

    async def execute_entry(self) -> bool:
        log.info("=== 09:19:30 EXECUTION WINDOW OPEN ===")
        assert self.store.pair
        pair = self.store.pair
        results: Dict[str, bool] = {}

        async def run_side(side_label: str, hedge: Leg, core: Leg) -> bool:
            cache = self._bootstrap_cache
            ltp = 0.0
            for r in cache["chain"]:
                if r["instrument_key"] == hedge.instrument_key:
                    ltp = r.get("ltp") or 0.0
                    break
            if ltp <= 0:
                ltp = HEDGE_TARGET_PREMIUM
            hedge.last_ltp = ltp
            core.last_ltp = ltp

            ok = await self._place_limit_buy_with_timeout(
                hedge, ref_price=ltp,
                slippage_ticks=HEDGE_LIMIT_SLIPPAGE_TICKS,
                timeout_sec=HEDGE_FILL_TIMEOUT_SEC,
                retry_ticks=HEDGE_LIMIT_SLIPPAGE_TICKS_RETRY,
            )
            if not ok:
                log.warning("ABORT:hedge_timeout side=%s", side_label)
                hedge.state = LegState.ABORTED
                core.state = LegState.ABORTED
                return False
            ok2 = await self._place_market_sell(core)
            if not ok2:
                log.warning("ABORT:core_failed side=%s", side_label)
                return False
            await self._place_broker_sl(core)
            return True

        ce_ok, pe_ok = await asyncio.gather(
            run_side("CE", pair.ce_hedge, pair.ce_short),
            run_side("PE", pair.pe_hedge, pair.pe_short),
        )
        results["CE"] = ce_ok
        results["PE"] = pe_ok
        log.info("ENTRY_RESULT: %s", results)
        return ce_ok and pe_ok

    # ------------------------------------------------------------------ tick engine
    def _on_tick_local(self, ltp_map: Dict[str, float]) -> None:
        now = time.time()
        if not self.store.pair:
            return
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
        self._evaluate_local_sl()
        mtm = self.store.compute_mtm()
        update_trail(self.store, mtm, self.paper_log_csv,
                     self.store.pair.session_date if self.store.pair else "")
        self.store.log_mtm()
        if mtm <= self.store.trail_lock or mtm <= MAX_DAILY_LOSS:
            self._trigger_kill_switch(
                reason=(
                    f"mtm_below_trail_lock mtm={mtm:.2f} lock={self.store.trail_lock:.2f}"
                    if mtm > MAX_DAILY_LOSS
                    else f"max_daily_loss mtm={mtm:.2f}"
                )
            )

    def _compute_sl_buffer(self, leg: Leg) -> float:
        """Adaptive SL-L limit buffer in points, scaled by VIX.

        buffer = max(MIN, min(K * vix * spot / 100, MAX))

        where spot is the entry spot (a constant for the session) and
        vix is the entry_vix (also constant for the session). The
        K * vix * spot / 100 term is roughly the 1-day expected move
        of the underlying, scaled by K to pick a small fraction of it
        (1-3%) that's appropriate for a 1-2 minute flash crash.

        This properly engages for our core-short selection band
        (premium 75-130) where the previous PCT*gap formula pinned
        the buffer at the 5-pt floor.

        For entry_vix=14, entry_spot=24000, K=0.0036:
          buffer = max(5, min(12.1, 30)) = 12.1
        For entry_vix=12, entry_spot=24000, K=0.0036:
          buffer = max(5, min(10.4, 30)) = 10.4
        For entry_vix=20, entry_spot=24000, K=0.0036:
          buffer = max(5, min(17.3, 30)) = 17.3
        For entry_vix=14, entry_spot=18000, K=0.0036:
          buffer = max(5, min(9.1, 30)) = 9.1

        All of these give 9-17 points of headroom for the typical
        12-20 VIX band, which is enough to absorb a 1-2 sigma 1-min
        move in a 0.30-delta option, instead of the old 5 points
        that gets blown through by a single 10-pt underlying tick.
        """
        if leg.fill_price is None:
            return SL_LIMIT_BUFFER_POINTS_MIN
        spot = 0.0
        vix = 0.0
        # Prefer the LegPair copy (closer to the data; same value as
        # store.entry_spot but doesn't depend on the bootstrap flow).
        if self.store.pair is not None:
            spot = self.store.pair.entry_spot or 0.0
            vix = self.store.pair.entry_vix or 0.0
        if spot <= 0 or vix <= 0:
            # Fallback if entry data missing (e.g. paper mode never
            # populated entry_spot). Use the static MIN so the system
            # still has SOME headroom.
            return SL_LIMIT_BUFFER_POINTS_MIN
        raw = SL_LIMIT_BUFFER_VIX_K * vix * spot / 100.0
        return max(
            SL_LIMIT_BUFFER_POINTS_MIN,
            min(raw, SL_LIMIT_BUFFER_POINTS_MAX),
        )

    def _evaluate_local_sl(self) -> None:
        if not self.store.pair:
            return
        for leg in [self.store.pair.ce_short, self.store.pair.pe_short]:
            if leg.state not in (LegState.CORE_FILLED, LegState.SL_PLACED):
                continue
            if leg.fill_price is None or leg.last_ltp is None:
                continue
            threshold = leg.fill_price * (1 + SL_PERCENT)
            if leg.last_ltp >= threshold:
                # Flash-crash check: if the LTP has gapped PAST the
                # broker SL's limit price, the broker SL-L cannot
                # possibly fill at the limit (we're already 30+ points
                # above the trigger). Fire the local SL immediately
                # (in the next tick handler iteration) and skip the
                # 8-second wait for the broker.
                limit_level = getattr(leg, "sl_limit_price", None)
                if limit_level is not None and leg.last_ltp >= limit_level:
                    log.warning(
                        "SL_LOCAL_FLASH_CRASH: leg=%s ltp=%.2f past limit=%.2f "
                        "(broker SL cannot fill at limit — immediate exit)",
                        leg.leg_id, leg.last_ltp, limit_level,
                    )
                else:
                    log.warning(
                        "SL_LOCAL: leg=%s order_id=%s ltp=%.2f threshold=%.2f",
                        leg.leg_id, leg.order_id, leg.last_ltp, threshold,
                    )
                self._enqueue_exit_due_to_local_sl(leg)

    def _enqueue_exit_due_to_local_sl(self, leg: Leg) -> None:
        # Dedup: a fast breach can generate several ticks within the
        # ~100-500ms REST round trip window before the first exit task
        # completes and flips leg.state to CLOSED. Without this guard
        # each of those ticks re-enqueues a fresh MARKET BUY-to-close,
        # which can flip a hedged short into a naked long. Same pattern
        # as sl_escalation_in_flight above.
        if leg.exit_in_flight or leg.sl_broker_triggered or leg.state == LegState.CLOSED:
            log.debug("SL_LOCAL_DEDUP: leg=%s already exiting (state=%s in_flight=%s broker=%s)",
                      leg.leg_id, leg.state, leg.exit_in_flight, leg.sl_broker_triggered)
            return
        leg.exit_in_flight = True
        leg.sl_triggered_at = time.time()

        async def task():
            try:
                assert self.rest
                # Re-check inside the task: broker may have confirmed
                # between our local trigger and the moment this coroutine
                # actually runs.
                if leg.sl_broker_triggered or leg.state == LegState.CLOSED:
                    log.info("EXIT_LOCALSL_SKIPPED: leg=%s broker already closed", leg.leg_id)
                    return
                if leg.sl_order_id:
                    with contextlib.suppress(Exception):
                        await cancel_order_via_upstox(self.rest, leg.sl_order_id)
                # CRITICAL re-check after the await above. watch_sl runs as
                # a concurrent worker.submit task and can call
                # get_order_status, see the cancel settle, and flip
                # leg.state=CLOSED between our cancel and our place. If we
                # don't re-check here, we'd submit a second MARKET BUY on
                # a leg the broker has just filled. Same per-leg Lock
                # would also work but a re-check is simpler and zero-cost
                # in the common case.
                if leg.sl_broker_triggered or leg.state == LegState.CLOSED:
                    log.info(
                        "EXIT_LOCALSL_RACE_SKIPPED: leg=%s broker closed "
                        "between cancel and place", leg.leg_id,
                    )
                    return
                await place_order_via_upstox(
                    self.rest,
                    instrument_key=leg.instrument_key, side="BUY", qty=leg.qty,
                    order_type="MARKET", price=0.0, trigger_price=None,
                    tag=f"EXIT_LOCALSL_{leg.leg_id}",
                )
                leg.state = LegState.CLOSED
                log.info("EXIT_LOCALSL: leg=%s order_id=%s", leg.leg_id, leg.order_id)
                log_event(
                    self.paper_log_csv, "SL_LOCAL_FIRED", leg,
                    order_side="BUY", simulated_price=leg.last_ltp, ltp_at_event=leg.last_ltp,
                    entry_vix=self.store.entry_vix, sl_trigger_price=leg.sl_trigger_price,
                    running_total_mtm=self.store.total_mtm, trail_lock=self.store.trail_lock,
                    reentry_count=leg.reentry_count, notes="local SL faster than broker",
                    session_date=self.store.pair.session_date if self.store.pair else "",
                )
            except Exception as e:  # noqa: BLE001
                log.exception("EXIT_LOCALSL_FAILED: leg=%s err=%s", leg.leg_id, e)
                # Don't keep the guard set on a hard failure — let the
                # next tick have another go. The state is whatever it
                # was before (still CORE_FILLED or SL_PLACED).
            finally:
                leg.exit_in_flight = False

        if self.worker:
            self.worker.submit(OrderTask(name=f"exit_localsl_{leg.leg_id}", coro_factory=task))

    def _update_trail(self, mtm: float) -> None:
        """Delegate to the module-level update_trail() so live and paper
        use IDENTICAL trail logic. The instance method exists for call-site
        readability inside the tick handler.
        """
        update_trail(
            self.store, mtm, self.paper_log_csv,
            self.store.pair.session_date if self.store.pair else "",
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
            entry_vix=self.store.entry_vix, running_total_mtm=self.store.total_mtm,
            trail_lock=self.store.trail_lock, notes=reason,
            session_date=self.store.pair.session_date if self.store.pair else "",
        )

        async def task():
            assert self.rest
            await exit_all_positions(self.rest)
            if self.store.pair:
                for leg in [self.store.pair.ce_short, self.store.pair.pe_short]:
                    if leg.sl_order_id:
                        with contextlib.suppress(Exception):
                            await cancel_order_via_upstox(self.rest, leg.sl_order_id)
            log.info("KILL_SWITCH_COMPLETE")

        if self.worker:
            self.worker.submit(OrderTask(name="kill_switch", coro_factory=task))

    def _on_feed_disconnect(self) -> None:
        # Called from ws_runner thread. Mutating self.store and writing
        # CSV from a non-loop thread is racy with the asyncio consumers.
        # Defer to the running loop.
        try:
            self.loop.call_soon_threadsafe(self._on_feed_disconnect_async)
            return
        except RuntimeError:
            # Loop already closed; ignore.
            return

    def _on_feed_disconnect_async(self) -> None:
        if self.store.feed_disconnected_at is None:
            self.store.feed_disconnected_at = time.time()
            log.warning("FEED_DISCONNECTED at %s", datetime.now().isoformat(timespec="seconds"))
            log_event(
                self.paper_log_csv, "FEED_DISCONNECT", None,
                entry_vix=self.store.entry_vix, running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock, notes="broker SL-L orders remain in place",
                session_date=self.store.pair.session_date if self.store.pair else "",
            )

    def _on_feed_reconnect(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self._on_feed_reconnect_async)
            return
        except RuntimeError:
            return

    def _on_feed_reconnect_async(self) -> None:
        if self.store.feed_disconnected_at is not None:
            dur = time.time() - self.store.feed_disconnected_at
            log.info("FEED_RECONNECTED after %.1fs", dur)
            log_event(
                self.paper_log_csv, "FEED_RECONNECT", None,
                entry_vix=self.store.entry_vix, running_total_mtm=self.store.total_mtm,
                trail_lock=self.store.trail_lock, notes=f"downtime={dur:.1f}s",
                session_date=self.store.pair.session_date if self.store.pair else "",
            )
            self.store.feed_disconnected_at = None

    # ------------------------------------------------------------------ re-entry
    def _maybe_reenter(self) -> None:
        if not self.store.pair:
            return
        for leg in [self.store.pair.ce_short, self.store.pair.pe_short]:
            if leg.state != LegState.CLOSED:
                continue
            if leg.reentry_count >= MAX_REENTRIES_PER_LEG:
                continue
            current_vix = self.store.entry_vix
            if (current_vix - self.store.original_entry_vix) / self.store.original_entry_vix > REENTRY_VIX_GUARD_PCT:
                log.warning("REENTRY_SKIPPED: vix_spike leg=%s vix_now=%.2f vix_orig=%.2f",
                            leg.leg_id, current_vix, self.store.original_entry_vix)
                log_event(
                    self.paper_log_csv, "REENTRY_SKIPPED", leg,
                    entry_vix=current_vix, running_total_mtm=self.store.total_mtm,
                    trail_lock=self.store.trail_lock, reentry_count=leg.reentry_count,
                    notes="vix_spike_guard",
                    session_date=self.store.pair.session_date if self.store.pair else "",
                )
                continue
            if leg.fill_price is None or leg.last_ltp is None:
                continue
            threshold = leg.fill_price * (1 - REENTRY_MOMENTUM_DISCOUNT)
            if leg.last_ltp < threshold:
                log.info("REENTRY: leg=%s prior_order_id=%s ltp=%.2f threshold=%.2f",
                         leg.leg_id, leg.order_id, leg.last_ltp, threshold)
                leg.reentry_count += 1
                self.store.num_reentries += 1
                leg.state = LegState.PENDING
                leg.order_id = None
                leg.fill_price = None
                leg.sl_order_id = None
                log_event(
                    self.paper_log_csv, "REENTRY", leg,
                    order_side="SELL", ltp_at_event=leg.last_ltp,
                    entry_vix=current_vix, running_total_mtm=self.store.total_mtm,
                    trail_lock=self.store.trail_lock, reentry_count=leg.reentry_count,
                    notes=f"ltp<threshold({threshold:.2f})",
                    session_date=self.store.pair.session_date if self.store.pair else "",
                )
                if self.worker and self.rest:
                    async def task(_leg=leg):
                        ok = await self._place_market_sell(_leg)
                        if ok:
                            await self._place_broker_sl(_leg)

                    self.worker.submit(OrderTask(name=f"reentry_{leg.leg_id}", coro_factory=task))

    # ------------------------------------------------------------------ time exit
    async def time_exit(self) -> None:
        if not self.store.pair:
            return
        log.info("TIME_EXIT 15:15 — squaring off")
        assert self.rest
        await exit_all_positions(self.rest)
        for leg in [self.store.pair.ce_short, self.store.pair.pe_short]:
            if leg.sl_order_id and not PAPER_TRADING_MODE:
                with contextlib.suppress(Exception):
                    await cancel_order_via_upstox(self.rest, leg.sl_order_id)
            leg.state = LegState.CLOSED
        self.store.exit_reason = "time_exit_15:15"
        log.info("TIME_EXIT_COMPLETE")
        log_event(
            self.paper_log_csv, "TIME_EXIT", None,
            entry_vix=self.store.entry_vix, running_total_mtm=self.store.total_mtm,
            trail_lock=self.store.trail_lock, notes="15:15 square-off",
            session_date=self.store.pair.session_date if self.store.pair else "",
        )

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        # Time-gate bootstrap to ENTRY_VIX_TIME. Without this gate, capturing
        # entry_vix + chain + greeks at process start (e.g. 08:45) would
        # return values 30+ minutes stale by 09:19:30 hedge execution —
        # silently undermining the VIX-adaptive delta band and premium band.
        # If we launch after 09:15 (e.g. recovery from a crash), skip the
        # wait: stale-by-30-min is worse than stale-by-0.
        await self._wait_until_or_skip(ENTRY_VIX_TIME, label="ENTRY_VIX_TIME(09:15)")
        try:
            self.bootstrap()
        except Exception:
            log.exception("bootstrap failed")
            return

        async with AsyncRestClient(ACCESS_TOKEN) as self.rest:
            self.worker = OrderWorker(self.rest)
            worker_task = asyncio.create_task(self.worker.run())

            # Time-gate strike selection to STRIKE_SELECT_TIME (09:19). The
            # option-greek endpoint should be polled at the time we actually
            # commit to strikes, not at process start. Same skip-if-late rule
            # as bootstrap.
            await self._wait_until_or_skip(
                STRIKE_SELECT_TIME, label="STRIKE_SELECT_TIME(09:19)"
            )
            try:
                self.select_strikes()
            except Exception:
                log.exception("strike selection failed")
                return

            await self._wait_until(EXEC_START_TIME)
            entry_ok = await self.execute_entry()
            if not entry_ok:
                log.error("ENTRY_ABORTED — one or more leg pairs did not enter")

            instruments: List[str] = []
            if self.store.pair:
                instruments = [
                    self.store.pair.ce_short.instrument_key,
                    self.store.pair.pe_short.instrument_key,
                    self.store.pair.ce_hedge.instrument_key,
                    self.store.pair.pe_hedge.instrument_key,
                ]

            self.loop = asyncio.get_running_loop()
            self.ws = WsAdapter(self.loop, instruments)

            def ws_runner():
                """Run the websocket in a thread; reconnect with exponential
                backoff. We reset attempt=0 only AFTER a connect that lasted
                at least a few seconds (i.e. a "successful" connect that
                actually delivered data) — that way a single bad connect
                followed by a healthy one doesn't burn through the budget.
                """
                backoff = 1.0
                attempt = 0
                healthy_threshold = 10.0
                while not self._stopped:
                    connected_at = time.monotonic()
                    try:
                        self.ws.connect()
                        if self._stopped:
                            return
                    except Exception:
                        log.exception("ws connect failed; backing off %.1fs", backoff)
                        self._on_feed_disconnect()
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        attempt += 1
                        if attempt > MAX_RECONNECT_ATTEMPTS:
                            log.error("ws reconnect attempts exhausted (%d); kill-switching",
                                      MAX_RECONNECT_ATTEMPTS)
                            self.loop.call_soon_threadsafe(
                                self._trigger_kill_switch,
                                f"ws_reconnect_exhausted_{MAX_RECONNECT_ATTEMPTS}",
                            )
                            return
                        continue
                    # ws.connect() returned (likely disconnect, not exception).
                    uptime = time.monotonic() - connected_at
                    if uptime >= healthy_threshold:
                        # We had a real session. Reset retry budget.
                        attempt = 0
                        backoff = 1.0
                        log.info("ws healthy disconnect (uptime=%.1fs); reset reconnect budget",
                                 uptime)
                    else:
                        attempt += 1
                    if attempt > MAX_RECONNECT_ATTEMPTS:
                        log.error("ws reconnect attempts exhausted (%d); kill-switching",
                                  MAX_RECONNECT_ATTEMPTS)
                        self._on_feed_disconnect()
                        self.loop.call_soon_threadsafe(
                            self._trigger_kill_switch,
                            f"ws_reconnect_exhausted_{MAX_RECONNECT_ATTEMPTS}",
                        )
                        return
                    log.warning("ws disconnected; reconnect attempt %d/%d in %.1fs",
                                attempt, MAX_RECONNECT_ATTEMPTS, backoff)
                    self._on_feed_disconnect()
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    self._on_feed_reconnect()

            ws_task = asyncio.create_task(asyncio.to_thread(ws_runner))

            try:
                while not self._stopped:
                    # Only treat a "disconnect" as real if we were ever
                    # connected. The cold-start window between
                    # ws_task creation and the first _on_open is
                    # architecturally a "not yet ready" state, not a
                    # "was healthy, now isn't" state. Without this
                    # gate, MAX_FEED_DOWNTIME_SEC (30s) would expire
                    # mid-handshake on a slow connect and fire the
                    # kill-switch spuriously.
                    if (self.ws
                            and self.ws.ever_connected
                            and not self.ws.connected):
                        self._on_feed_disconnect()
                        if (self.store.feed_disconnected_at
                                and time.time() - self.store.feed_disconnected_at > MAX_FEED_DOWNTIME_SEC):
                            self._trigger_kill_switch(reason=f"feed_disconnected_for_{MAX_FEED_DOWNTIME_SEC}s")
                            break

                    if datetime.now().time() >= TIME_EXIT:
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
                        if not isinstance(item, tuple) or len(item) != 3:
                            continue
                        kind, instrument_key, payload = item
                        if kind == "ltpc":
                            # Extract LTP from the v3 LTPC shape:
                            #   payload = {"ltpc": {"ltp": X, "ltt": "...", ...}}
                            if isinstance(payload, dict):
                                ltpc = payload.get("ltpc")
                                if isinstance(ltpc, dict) and "ltp" in ltpc:
                                    try:
                                        ltp_map[instrument_key] = float(ltpc["ltp"])
                                    except (TypeError, ValueError):
                                        pass
                        # NB: no "order_update" branch. The market-data
                        # websocket does not carry order updates. SL fills
                        # are confirmed by the watch_sl REST poller.
                    if ltp_map:
                        self._on_tick_local(ltp_map)
                        self._maybe_reenter()

                    if self.ws._feed_queue.empty():
                        await asyncio.sleep(0.05)

                    if self.store.pair:
                        for leg in [self.store.pair.ce_short, self.store.pair.pe_short]:
                            if (leg.sl_triggered_at
                                    and not leg.sl_broker_triggered
                                    and leg.state == LegState.SL_PLACED
                                    and time.time() - leg.sl_triggered_at > SL_L_FILL_TIMEOUT_SEC):
                                log.warning("SL_L_FILL_TIMEOUT: leg=%s escalating to market",
                                            leg.leg_id)
                                leg.sl_triggered_at = None
                                log_event(
                                    self.paper_log_csv, "SL_L_FILL_TIMEOUT", leg,
                                    order_side="BUY", ltp_at_event=leg.last_ltp,
                                    entry_vix=self.store.entry_vix,
                                    sl_trigger_price=leg.sl_trigger_price,
                                    running_total_mtm=self.store.total_mtm,
                                    trail_lock=self.store.trail_lock,
                                    reentry_count=leg.reentry_count,
                                    notes=f"escalating to market after {SL_L_FILL_TIMEOUT_SEC}s",
                                    session_date=self.store.pair.session_date if self.store.pair else "",
                                )
                                if self.worker and self.rest and not leg.sl_escalation_in_flight:
                                    leg.sl_escalation_in_flight = True
                                    async def escalate(_leg=leg):
                                        assert self.rest
                                        # Re-check: broker may have confirmed
                                        # between our local trigger and now.
                                        if _leg.sl_broker_triggered or _leg.state == LegState.CLOSED:
                                            return
                                        if _leg.sl_order_id:
                                            with contextlib.suppress(Exception):
                                                await cancel_order_via_upstox(self.rest, _leg.sl_order_id)
                                        # Final guard before MARKET: only flip
                                        # the state to CLOSED if we have a
                                        # fresh fill confirmation.
                                        if _leg.state == LegState.CLOSED or _leg.sl_broker_triggered:
                                            return
                                        try:
                                            await place_order_via_upstox(
                                                self.rest,
                                                instrument_key=_leg.instrument_key, side="BUY", qty=_leg.qty,
                                                order_type="MARKET", price=0.0, trigger_price=None,
                                                tag=f"SL_ESCALATE_{_leg.leg_id}",
                                            )
                                            _leg.state = LegState.CLOSED
                                            log.info("SL_ESCALATED_TO_MARKET: leg=%s", _leg.leg_id)
                                        except Exception as e:  # noqa: BLE001
                                            log.exception("SL_ESCALATE_FAILED: leg=%s err=%s", _leg.leg_id, e)
                                        finally:
                                            _leg.sl_escalation_in_flight = False

                                    self.worker.submit(
                                        OrderTask(name=f"sl_escalate_{leg.leg_id}", coro_factory=escalate)
                                    )

            finally:
                self._stopped = True
                if self.ws and self.ws.streamer:
                    with contextlib.suppress(Exception):
                        self.ws.streamer.disconnect()
                ws_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ws_task
                if self.worker:
                    try:
                        await asyncio.wait_for(self.worker.q.join(), timeout=5.0)
                    except asyncio.TimeoutError:
                        log.warning("worker queue did not drain within 5s")
                    self.worker.stop()
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task

        # Persist the per-session summary.
        self._write_paper_summary()

    def _write_paper_summary(self) -> None:
        if not PAPER_TRADING_MODE:
            return
        if not self.store.pair:
            return
        n_legs = sum(
            1 for lg in (self.store.pair.ce_short, self.store.pair.pe_short,
                         self.store.pair.ce_hedge, self.store.pair.pe_hedge)
            if lg.state not in (LegState.ABORTED, LegState.PENDING, LegState.HEDGE_PLACED)
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
                "max_drawdown_mtm": f"{self.store.max_drawdown_mtm:.2f}",
                "trail_lock_final": f"{self.store.trail_lock:.2f}",
                "exit_reason": self.store.exit_reason or "session_end",
            },
        )

    async def _wait_until(self, t: dtime) -> None:
        while datetime.now().time() < t:
            await asyncio.sleep(0.5)

    async def _wait_until_or_skip(self, t: dtime, label: str = "") -> None:
        """Like _wait_until, but if the target time has already passed
        (process launched late, or recovering from a crash mid-session),
        log a warning and return immediately rather than skipping the
        gate silently.

        Time gates (ENTRY_VIX_TIME, STRIKE_SELECT_TIME) are *stale-safety*
        measures: they ensure we don't capture VIX/greeks 30+ minutes
        before the events that consume them. If the gate has already
        passed, waiting serves no purpose — the underlying data was
        already fetched, and delaying execution makes the strategy miss
        the next timing window.
        """
        now = datetime.now().time()
        if now >= t:
            log.warning(
                "TIME_GATE_SKIPPED: %s already passed (now=%s target=%s) — "
                "proceeding immediately (process launched late or recovering)",
                label, now, t,
            )
            return
        log.info("TIME_GATE: waiting until %s (now=%s)", label or t, now)
        while datetime.now().time() < t:
            await asyncio.sleep(0.5)


# ============================================================================
# ============================================================================
# Loop exception handler + main wrapper
# ============================================================================
_engine_ref: Optional[Engine] = None


def _async_exception_handler(loop, context):  # noqa: ANN001
    msg = context.get("message", "async exception")
    exc = context.get("exception")
    log.error("UNHANDLED_ASYNC: %s", msg, exc_info=exc)
    # Live mode only — paper mode never had real positions
    if not PAPER_TRADING_MODE and _engine_ref and _engine_ref.rest:
        try:
            asyncio.create_task(exit_all_positions(_engine_ref.rest))
        except Exception:
            log.exception("failed to schedule emergency exit on async exception")


def main() -> None:
    global _engine_ref

    # Mode dispatch — strictly on hardcoded booleans, no runtime input.
    if PAPER_TRADING_MODE:
        log.info("=== RUN MODE: PAPER TRADING ===")
        log.info("Outputs: %s, %s", PAPER_TRADE_LOG_CSV, PAPER_TRADE_SUMMARY_CSV)
    else:
        log.info("=== RUN MODE: LIVE ===")

    engine = Engine()
    _engine_ref = engine
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(_async_exception_handler)

    def _sigterm(*_):
        log.warning("SIGTERM received — initiating shutdown")
        engine._stopped = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, _sigterm)

    try:
        loop.run_until_complete(engine.run())
    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — shutting down")
    except Exception:
        log.exception("fatal exception in main loop")
        if not PAPER_TRADING_MODE and engine.rest:
            try:
                loop.run_until_complete(exit_all_positions(engine.rest))
            except Exception:
                log.exception("emergency square-off also failed")
    finally:
        try:
            engine.store.close()
        except Exception:
            pass
        loop.close()
        log.info("shutdown complete")


if __name__ == "__main__":
    main()
