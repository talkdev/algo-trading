"""
════════════════════════════════════════════════════════════════════════════
NIFTY INTRADAY OPTIONS ALGO TRADING ENGINE — 2026 PRODUCTION BUILD
FILE 1 of 5 : CORE INFRASTRUCTURE
════════════════════════════════════════════════════════════════════════════

Save as: nifty_algo_core.py

RESPONSIBILITY OF THIS FILE:
  - Environment / configuration loading (credentials live in env.txt)
  - SQLite persistence layer (ALL state lives in DB, never in bare memory)
  - Upstox API client (thin, rate-limited, fully logged wrapper)
  - Sliding-window API rate limiter (tracks & throttles calls per category)
  - Unified logging: console + daily rotating audit file + SQLite audit table
  - IST timezone utilities
  - Console display helpers (used by later files for the live dashboard)

5-FILE ARCHITECTURE ROADMAP:
  File 1 (this file): core_infra                — config, DB, API client, logging
  File 2 (next)      : market_data_engine        — live data ingestion + VRP/trend/
                                                    direction signal computation
  File 3             : strategy_engine           — strategy selection + strike/param
                                                    computation
  File 4             : execution_engine          — pre-trade validation, paper/live
                                                    order routing, position monitoring
  File 5             : main                      — orchestration loop, console
                                                    dashboard, EOD reporting

╔════════════════════════════════════════════════════════════════════════════╗
║  TRANSPARENCY / VERIFICATION NOTICE — READ BEFORE LIVE DEPLOYMENT          ║
╠════════════════════════════════════════════════════════════════════════════╣
║  1. Upstox endpoint paths (API_ENDPOINTS below) and rate limits           ║
║     (DEFAULT rate_limits in load_config) are based on commonly published  ║
║     Upstox API v2 conventions. VERIFY EVERY ENDPOINT PATH, REQUEST/       ║
║     RESPONSE SCHEMA, AND RATE LIMIT against the live, current Upstox      ║
║     Developer API documentation before trading with real capital.        ║
║     https://upstox.com/developer/api-documentation/                      ║
║  2. STT / brokerage / exchange charge rates below are configurable        ║
║     defaults taken from the strategy spec you provided — verify against  ║
║     your actual broker contract note and current NSE/SEBI circulars.     ║
║  3. NIFTY lot size (65) and strike step (50) are defaults — this engine   ║
║     validates them at runtime against the live instrument master in a   ║
║     later file and will HALT if they mismatch, rather than trusting the  ║
║     hardcoded default blindly.                                           ║
║  4. HIGH_IMPACT_EVENTS calendar is intentionally left EMPTY. You must    ║
║     populate high_impact_events.json yourself with verified event dates  ║
║     (FOMC, RBI MPC, Union Budget, CPI/WPI releases, expiry dates, etc.)   ║
║     — no dates have been fabricated here.                                ║
╚════════════════════════════════════════════════════════════════════════════╝

DEPENDENCIES:
    pip install requests
    # Windows only (if zoneinfo raises "No time zone found"):
    pip install tzdata
"""

from __future__ import annotations

import os
import sys
import json
import time
import sqlite3
import logging
import logging.handlers
import threading
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, date, time as dtime
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────────────────────────────────────────────────────────────────────
# TIMEZONE SETUP
# ─────────────────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz  # fallback for environments without IANA tzdata (e.g. bare Windows)
    IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def parse_ist_timestamp(ts) -> Optional[datetime]:
    """
    Parse a timestamp (ISO8601 string or unix epoch seconds) into an
    IST-aware datetime. Returns None on failure instead of raising —
    this is called continuously on live market data and must never
    crash the engine due to a malformed timestamp from the API.
    """
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=IST)
        if isinstance(ts, str):
            s = ts.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            else:
                dt = dt.astimezone(IST)
            return dt
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────
# PATHS & CONSTANTS
# ─────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "env.txt"
DEFAULT_DB_PATH = BASE_DIR / "data" / "nifty_algo.db"
DEFAULT_LOG_DIR = BASE_DIR / "logs"
DEFAULT_EVENTS_FILE = BASE_DIR / "high_impact_events.json"

# Instrument keys as specified in the original strategy design.
# VERIFY exact spelling/casing against Upstox's live instrument master —
# exact-string lookups are sensitive to this.
INSTRUMENT_KEY_NIFTY_SPOT = "NSE_INDEX|Nifty 50"
INSTRUMENT_KEY_INDIA_VIX = "NSE_INDEX|India VIX"

UPSTOX_BASE_URL = "https://api.upstox.com/v2"

# VERIFY EACH OF THESE PATHS AGAINST CURRENT UPSTOX API DOCS BEFORE LIVE USE.
API_ENDPOINTS = {
    "profile":          "/user/profile",
    "ltp":               "/market-quote/ltp",
    "quotes":            "/market-quote/quotes",
    "ohlc":              "/market-quote/ohlc",
    "historical_candle": "/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}",
    "intraday_candle":   "/historical-candle/intraday/{instrument_key}/{interval}",
    "option_contracts":  "/option/contract",
    "option_chain":      "/option/chain",
    "place_order":       "/order/place",
    "cancel_order":      "/order/cancel",
    "order_details":     "/order/details",
    "positions":         "/portfolio/short-term-positions",
    "funds_margin":      "/user/get-funds-and-margin",
}


# ─────────────────────────────────────────────────────────────────────────
# ENV FILE HANDLING
# ─────────────────────────────────────────────────────────────────────────
ENV_TEMPLATE = """\
# NIFTY Algo Trading Engine — Environment Configuration
# Fill in your Upstox API credentials below.
# NOTE: Upstox access tokens are typically valid only for the current
# trading day and must be regenerated daily via the OAuth login flow.
# Verify current token lifetime policy in Upstox's official documentation.

UPSTOX_API_KEY=
UPSTOX_API_SECRET=
UPSTOX_REDIRECT_URI=
UPSTOX_ACCESS_TOKEN=

# ── SAFETY ──────────────────────────────────────────────────────────────
# Engine runs in PAPER TRADE mode unless this is explicitly set to false.
# There is also a hard-coded safety guard in code that refuses to place a
# live order while this is true, independent of this flag being read
# correctly — defense in depth.
PAPER_TRADE_MODE=true

# ── CAPITAL & RISK ──────────────────────────────────────────────────────
STARTING_CAPITAL=1000000
MAX_DAILY_LOSS_PCT=0.02
MAX_RISK_PER_TRADE_PCT=0.006

# ── CONTRACT SPECS (verify against current NSE circular / broker) ───────
NIFTY_LOT_SIZE=65
NIFTY_STRIKE_STEP=50

# ── TRANSACTION COSTS (verify against current NSE/SEBI circulars) ──────
STT_RATE=0.0015
BROKERAGE_PER_ORDER=20.0
EXCHANGE_TXN_RATE=0.0003552

# ── TRADING WINDOW (per your explicit requirement: 10:00 - 15:00) ──────
TRADING_WINDOW_START=10:00
TRADING_WINDOW_LAST_ENTRY=14:00
HARD_EXIT_TIME=15:00

# ── POSITION LIMITS ──────────────────────────────────────────────────────
MAX_CONCURRENT_POSITIONS=2
MAX_ENTRIES_PER_DAY=2

# ── INFRA ────────────────────────────────────────────────────────────────
DB_PATH=data/nifty_algo.db
LOG_DIR=logs
LOG_LEVEL=INFO
REQUEST_TIMEOUT_SECONDS=10
MAX_RETRIES=3
"""


def ensure_env_file(path: Path) -> None:
    if not path.exists():
        path.write_text(ENV_TEMPLATE, encoding="utf-8")
        print(f"[SETUP] {path} did not exist — a template has been created. "
              f"Please fill in your Upstox credentials before running the engine.")


def load_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _get_bool(env: dict, key: str, default: bool) -> bool:
    val = env.get(key)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(env: dict, key: str, default: float) -> float:
    val = env.get(key)
    try:
        return float(val) if val not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _get_int(env: dict, key: str, default: int) -> int:
    val = env.get(key)
    try:
        return int(val) if val not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _get_time(env: dict, key: str, default: dtime) -> dtime:
    val = env.get(key)
    if not val:
        return default
    try:
        h, m = val.strip().split(":")
        return dtime(int(h), int(m))
    except Exception:
        return default


def load_high_impact_events(path: Path = DEFAULT_EVENTS_FILE) -> dict:
    """
    Loads the high-impact-events calendar from an external JSON file
    (date_str -> event_name). Intentionally NOT hardcoded with any dates —
    populate this file yourself with verified event dates.
    Expected format: {"2026-02-01": "Union Budget", "2026-02-06": "RBI MPC", ...}
    """
    if not path.exists():
        path.write_text("{}\n", encoding="utf-8")
        print(f"[SETUP] {path} did not exist — created empty calendar. "
              f"Populate it with verified event dates (FOMC, RBI MPC, Budget, "
              f"CPI/WPI, expiry dates, etc.) for event-day handling to work.")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[WARNING] {path} is not valid JSON ({e}); treating as empty calendar.")
        return {}


# ─────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, repr=False)
class Config:
    # Upstox credentials
    upstox_api_key: str
    upstox_api_secret: str
    upstox_redirect_uri: str
    upstox_access_token: str

    # Safety
    paper_trade_mode: bool

    # Capital & risk (maps to original spec C03 / C04 / C05)
    starting_capital: float
    max_daily_loss_pct: float
    max_risk_per_trade_pct: float

    # Contract specs (C02 / C06) — validated at runtime in a later file
    lot_size: int
    nifty_strike_step: int

    # Transaction costs (C07 / C08 / C09)
    stt_rate: float
    brokerage_per_order: float
    exchange_txn_rate: float

    # Trading window — per explicit requirement: 10:00 to 15:00
    trading_window_start: dtime
    trading_window_last_entry: dtime
    hard_exit_time: dtime

    # Position limits
    max_concurrent_positions: int
    max_entries_per_day: int

    # High impact events calendar (empty unless user populates it)
    high_impact_events: dict

    # Infra
    db_path: Path
    log_dir: Path
    log_level: str
    request_timeout_seconds: float
    max_retries: int

    # API rate limits — CONSERVATIVE PLACEHOLDERS, verify against Upstox docs
    rate_limits: dict

    def __repr__(self) -> str:
        def mask(s: str) -> str:
            if not s:
                return "<empty>"
            return (s[:4] + "..." + s[-2:]) if len(s) > 8 else "***"
        return (
            f"Config(paper_trade_mode={self.paper_trade_mode}, "
            f"upstox_api_key={mask(self.upstox_api_key)}, "
            f"upstox_access_token={mask(self.upstox_access_token)}, "
            f"starting_capital={self.starting_capital}, "
            f"lot_size={self.lot_size}, "
            f"max_risk_per_trade_pct={self.max_risk_per_trade_pct})"
        )


def load_config(env_file: Path = ENV_FILE) -> Config:
    ensure_env_file(env_file)
    file_env = load_env_file(env_file)
    env = {**os.environ, **file_env}  # env.txt takes precedence over shell env

    if not file_env:
        print(f"[WARNING] {env_file} is empty. Upstox credentials must be "
              f"provided (UPSTOX_API_KEY, UPSTOX_API_SECRET, "
              f"UPSTOX_REDIRECT_URI, UPSTOX_ACCESS_TOKEN).")

    max_daily_loss_pct = _get_float(env, "MAX_DAILY_LOSS_PCT", 0.02)
    max_risk_per_trade_pct = _get_float(env, "MAX_RISK_PER_TRADE_PCT", 0.006)

    # Enforced relationship (original spec FIX-48): 3 consecutive stop-losses
    # at max_risk_per_trade_pct must not exceed max_daily_loss_pct.
    if max_risk_per_trade_pct >= max_daily_loss_pct / 3.0:
        safe_value = round(max_daily_loss_pct / 3.0 - 0.001, 4)
        print(f"[WARNING] MAX_RISK_PER_TRADE_PCT ({max_risk_per_trade_pct}) "
              f"does not satisfy < MAX_DAILY_LOSS_PCT/3 "
              f"({max_daily_loss_pct/3.0:.4f}). Clamping to {safe_value}.")
        max_risk_per_trade_pct = max(safe_value, 0.001)

    # CONSERVATIVE PLACEHOLDER RATE LIMITS — VERIFY AGAINST CURRENT UPSTOX DOCS
    rate_limits = {
        "quote":      {"per_second": 8, "per_minute": 120, "per_30min": 1200},
        "historical": {"per_second": 5, "per_minute": 60,  "per_30min": 600},
        "chain":      {"per_second": 3, "per_minute": 30,  "per_30min": 300},
        "order":      {"per_second": 3, "per_minute": 30,  "per_30min": 200},
        "default":    {"per_second": 5, "per_minute": 60,  "per_30min": 600},
    }

    db_path = Path(env.get("DB_PATH", str(DEFAULT_DB_PATH)))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    log_dir = Path(env.get("LOG_DIR", str(DEFAULT_LOG_DIR)))
    if not log_dir.is_absolute():
        log_dir = BASE_DIR / log_dir

    return Config(
        upstox_api_key=env.get("UPSTOX_API_KEY", ""),
        upstox_api_secret=env.get("UPSTOX_API_SECRET", ""),
        upstox_redirect_uri=env.get("UPSTOX_REDIRECT_URI", ""),
        upstox_access_token=env.get("UPSTOX_ACCESS_TOKEN", ""),

        paper_trade_mode=_get_bool(env, "PAPER_TRADE_MODE", True),

        starting_capital=_get_float(env, "STARTING_CAPITAL", 1_000_000.0),
        max_daily_loss_pct=max_daily_loss_pct,
        max_risk_per_trade_pct=max_risk_per_trade_pct,

        lot_size=_get_int(env, "NIFTY_LOT_SIZE", 65),
        nifty_strike_step=_get_int(env, "NIFTY_STRIKE_STEP", 50),

        stt_rate=_get_float(env, "STT_RATE", 0.0015),
        brokerage_per_order=_get_float(env, "BROKERAGE_PER_ORDER", 20.0),
        exchange_txn_rate=_get_float(env, "EXCHANGE_TXN_RATE", 0.0003552),

        trading_window_start=_get_time(env, "TRADING_WINDOW_START", dtime(10, 0)),
        trading_window_last_entry=_get_time(env, "TRADING_WINDOW_LAST_ENTRY", dtime(14, 0)),
        hard_exit_time=_get_time(env, "HARD_EXIT_TIME", dtime(15, 0)),

        max_concurrent_positions=_get_int(env, "MAX_CONCURRENT_POSITIONS", 2),
        max_entries_per_day=_get_int(env, "MAX_ENTRIES_PER_DAY", 2),

        high_impact_events=load_high_impact_events(),

        db_path=db_path,
        log_dir=log_dir,
        log_level=env.get("LOG_LEVEL", "INFO"),
        request_timeout_seconds=_get_float(env, "REQUEST_TIMEOUT_SECONDS", 10.0),
        max_retries=_get_int(env, "MAX_RETRIES", 3),

        rate_limits=rate_limits,
    )


# ─────────────────────────────────────────────────────────────────────────
# API RATE LIMITER (sliding window, thread-safe)
# ─────────────────────────────────────────────────────────────────────────
class RateLimiter:
    """
    Thread-safe sliding-window rate limiter for Upstox API calls.
    Tracks per-second, per-minute, and per-30-minute windows independently
    per category (quote / historical / chain / order / default).

    IMPORTANT: default limits are conservative placeholders — verify actual
    current limits for your Upstox API tier before relying on these.
    """

    def __init__(self, limits: dict):
        self._limits = limits
        self._calls: dict[str, list] = {}
        self._lock = threading.Lock()

    def _deque_for(self, category: str) -> list:
        if category not in self._calls:
            self._calls[category] = []
        return self._calls[category]

    def wait_if_needed(self, category: str) -> float:
        """
        Blocks (sleeps) if calling `category` right now would exceed any
        configured limit. Returns total seconds waited (0.0 if none).
        """
        cfg = self._limits.get(category, self._limits["default"])
        total_wait = 0.0

        while True:
            with self._lock:
                now = time.monotonic()
                dq = self._deque_for(category)
                # purge anything older than the largest window (30 min)
                dq[:] = [t for t in dq if now - t <= 1800]

                in_1s = [t for t in dq if now - t <= 1]
                in_60s = [t for t in dq if now - t <= 60]
                in_30m = dq

                sleep_needed = 0.0
                if len(in_1s) >= cfg["per_second"]:
                    sleep_needed = max(sleep_needed, 1.0 - (now - min(in_1s)) + 0.01)
                if len(in_60s) >= cfg["per_minute"]:
                    sleep_needed = max(sleep_needed, 60.0 - (now - min(in_60s)) + 0.01)
                if len(in_30m) >= cfg["per_30min"]:
                    sleep_needed = max(sleep_needed, 1800.0 - (now - in_30m[0]) + 0.01)

                if sleep_needed <= 0:
                    dq.append(now)
                    return total_wait

            time.sleep(sleep_needed)
            total_wait += sleep_needed


# ─────────────────────────────────────────────────────────────────────────
# DATABASE SCHEMA
# ─────────────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_state (
    trading_date            TEXT PRIMARY KEY,
    day_mode                TEXT,
    vix_regime               TEXT,
    gap_size                 TEXT,
    gap_direction            TEXT,
    day_label                TEXT,
    or_high                  REAL,
    or_low                   REAL,
    or_width                 REAL,
    or_condition             TEXT,
    entry_start              TEXT,
    entry_end                TEXT,
    hard_exit_time           TEXT,
    stop_multiplier          REAL,
    size_multiplier          REAL,
    wing_width               INTEGER,
    entry_count              INTEGER DEFAULT 0,
    reentry_count            INTEGER DEFAULT 0,
    daily_halted             INTEGER DEFAULT 0,
    consecutive_stops        INTEGER DEFAULT 0,
    last_stop_time           TEXT,
    last_stop_reason         TEXT,
    actual_expiry            TEXT,
    actual_dte               INTEGER,
    opening_iv                REAL,
    opening_pcr               REAL,
    current_capital           REAL,
    daily_pnl                 REAL DEFAULT 0.0,
    circuit_breaker_suspected INTEGER DEFAULT 0,
    vix_spike_detected        INTEGER DEFAULT 0,
    event_announced           INTEGER DEFAULT 0,
    paper_trade_mode          INTEGER DEFAULT 1,
    created_at                TEXT,
    updated_at                TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    position_id           TEXT PRIMARY KEY,
    trading_date           TEXT,
    strategy_name          TEXT,
    strategy_type          TEXT,
    selection_reason       TEXT,
    target_expiry          TEXT,
    actual_dte             INTEGER,
    entry_time             TEXT,
    entry_spot             REAL,
    entry_vix              REAL,
    entry_vrp              REAL,
    entry_credit           REAL,
    entry_debit            REAL,
    gross_credit           REAL,
    total_slippage         REAL,
    entry_costs_rupees     REAL,
    stop_premium           REAL,
    target_premium         REAL,
    stop_value             REAL,
    target_value           REAL,
    price_stop_pts         INTEGER,
    hard_exit_time         TEXT,
    final_lots             INTEGER,
    max_loss_per_lot       REAL,
    total_max_risk         REAL,
    estimated_margin       REAL,
    status                 TEXT DEFAULT 'OPEN',
    exit_time              TEXT,
    exit_reason            TEXT,
    exit_premium           REAL,
    gross_pnl_rupees       REAL,
    exit_costs_rupees      REAL,
    net_pnl_rupees         REAL,
    last_known_premium     REAL,
    stop_at_breakeven      INTEGER DEFAULT 0,
    stop_moved_to_25pct    INTEGER DEFAULT 0,
    paper_trade            INTEGER DEFAULT 1,
    raw_params_json        TEXT,
    created_at             TEXT,
    updated_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_date ON positions(trading_date);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS position_legs (
    leg_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id            TEXT,
    strike                 REAL,
    option_type            TEXT,
    action                 TEXT,
    qty                    INTEGER,
    entry_price            REAL,
    exit_price             REAL,
    entry_bid              REAL,
    entry_ask              REAL,
    entry_delta            REAL,
    entry_gamma            REAL,
    entry_vega             REAL,
    entry_theta            REAL,
    entry_iv               REAL,
    entry_oi               INTEGER,
    exit_delta              REAL,
    broker_order_id_entry   TEXT,
    broker_order_id_exit    TEXT,
    leg_status              TEXT DEFAULT 'OPEN',
    FOREIGN KEY (position_id) REFERENCES positions(position_id)
);

CREATE INDEX IF NOT EXISTS idx_legs_position ON position_legs(position_id);

CREATE TABLE IF NOT EXISTS option_chain_snapshot (
    snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_time      TEXT,
    trading_date      TEXT,
    expiry            TEXT,
    strike            REAL,
    option_type       TEXT,
    bid               REAL,
    ask               REAL,
    ltp               REAL,
    oi                INTEGER,
    volume            INTEGER,
    iv                REAL,
    delta             REAL,
    gamma             REAL,
    theta             REAL,
    vega              REAL,
    data_timestamp    TEXT
);

CREATE INDEX IF NOT EXISTS idx_chain_snapshot_time ON option_chain_snapshot(capture_time);
CREATE INDEX IF NOT EXISTS idx_chain_snapshot_date ON option_chain_snapshot(trading_date);
CREATE INDEX IF NOT EXISTS idx_chain_snapshot_expiry ON option_chain_snapshot(expiry, strike);

CREATE TABLE IF NOT EXISTS cycle_log (
    cycle_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_time               TEXT,
    trading_date             TEXT,
    spot                     REAL,
    vix                      REAL,
    vrp                      REAL,
    atm_iv_pct               REAL,
    parkinson_rv_pct         REAL,
    adx                      REAL,
    adx_condition            TEXT,
    vwap                     REAL,
    vwap_dist_pct            REAL,
    pcr                      REAL,
    pcr_change               REAL,
    skew_ratio               REAL,
    or_width                 REAL,
    or_condition             TEXT,
    volatility_condition     TEXT,
    iv_behavior              TEXT,
    trend_condition          TEXT,
    direction                TEXT,
    preferred_sell_side      TEXT,
    entry_timing             TEXT,
    action_taken             TEXT,
    no_trade_reason          TEXT,
    conditions_met_json      TEXT,
    conditions_not_met_json  TEXT,
    open_positions           INTEGER,
    daily_pnl_net            REAL,
    vix_regime               TEXT,
    day_mode                 TEXT,
    raw_json                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_cycle_log_time ON cycle_log(cycle_time);
CREATE INDEX IF NOT EXISTS idx_cycle_log_date ON cycle_log(trading_date);

CREATE TABLE IF NOT EXISTS trade_entries (
    trade_id             TEXT PRIMARY KEY,
    position_id          TEXT,
    strategy_name         TEXT,
    entry_time            TEXT,
    trading_date          TEXT,
    day_label             TEXT,
    entry_spot            REAL,
    entry_vix             REAL,
    entry_vrp             REAL,
    entry_atm_iv          REAL,
    entry_parkinson_rv    REAL,
    entry_adx             REAL,
    entry_vwap            REAL,
    entry_vwap_dist       REAL,
    entry_pcr             REAL,
    entry_pcr_change      REAL,
    entry_skew_ratio      REAL,
    or_width              REAL,
    or_condition          TEXT,
    volatility_condition  TEXT,
    iv_behavior           TEXT,
    trend_condition       TEXT,
    adx_condition         TEXT,
    direction             TEXT,
    vwap_signal           TEXT,
    pcr_signal            TEXT,
    skew_signal           TEXT,
    preferred_sell_side   TEXT,
    target_expiry         TEXT,
    actual_dte            INTEGER,
    legs_json             TEXT,
    entry_credit          REAL,
    entry_debit           REAL,
    gross_credit          REAL,
    total_slippage        REAL,
    entry_costs_pts       REAL,
    entry_costs_rupees    REAL,
    stop_premium          REAL,
    target_premium        REAL,
    price_stop_pts        INTEGER,
    hard_exit_time        TEXT,
    final_lots            INTEGER,
    max_loss_per_lot      REAL,
    total_max_risk        REAL,
    capital_at_entry      REAL,
    daily_pnl_at_entry    REAL,
    paper_trade           INTEGER DEFAULT 1,
    selection_reason      TEXT,
    created_at            TEXT
);

CREATE TABLE IF NOT EXISTS trade_exits (
    exit_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id              TEXT,
    position_id           TEXT,
    strategy_name         TEXT,
    exit_time             TEXT,
    hold_minutes          REAL,
    exit_reason           TEXT,
    exit_spot             REAL,
    exit_vix              REAL,
    exit_adx              REAL,
    exit_vwap_dist        REAL,
    exit_legs_json        TEXT,
    exit_premium          REAL,
    gross_pnl_pts         REAL,
    gross_pnl_rupees      REAL,
    exit_slippage         REAL,
    exit_costs_pts        REAL,
    exit_costs_rupees     REAL,
    total_costs_rupees    REAL,
    net_pnl_pts           REAL,
    net_pnl_rupees        REAL,
    net_pnl_pct           REAL,
    result                TEXT,
    profit_pct_of_credit  REAL,
    created_at            TEXT
);

CREATE TABLE IF NOT EXISTS daily_summary (
    trading_date              TEXT PRIMARY KEY,
    day_label                 TEXT,
    trades_attempted          INTEGER,
    trades_executed           INTEGER,
    trades_won                INTEGER,
    trades_lost               INTEGER,
    win_rate_pct              REAL,
    gross_pnl_rupees          REAL,
    total_costs_rupees        REAL,
    net_pnl_rupees            REAL,
    net_pnl_pct_capital       REAL,
    max_intraday_drawdown     REAL,
    max_concurrent_positions  INTEGER,
    stops_fired               INTEGER,
    daily_halt_triggered      INTEGER,
    vix_open                  REAL,
    vix_close                 REAL,
    vix_low                   REAL,
    vix_high                  REAL,
    nifty_open                REAL,
    nifty_close                REAL,
    nifty_low                  REAL,
    nifty_high                 REAL,
    or_width                   REAL,
    or_condition                TEXT,
    vrp_mean                    REAL,
    strategies_used_json         TEXT,
    no_trade_reasons_json        TEXT,
    avg_hold_minutes             REAL,
    avg_credit_pts               REAL,
    avg_vrp_at_entry             REAL,
    profit_factor                REAL,
    capital_start                 REAL,
    capital_end                   REAL,
    capital_change_pct            REAL,
    created_at                     TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    log_time        TEXT,
    level           TEXT,
    logger_name     TEXT,
    message         TEXT,
    context_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_time ON audit_log(log_time);
CREATE INDEX IF NOT EXISTS idx_audit_log_level ON audit_log(level);

CREATE TABLE IF NOT EXISTS api_call_log (
    call_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    call_time          TEXT,
    category           TEXT,
    endpoint           TEXT,
    method             TEXT,
    status_code        INTEGER,
    response_time_ms   REAL,
    rate_limited       INTEGER DEFAULT 0,
    error_message      TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_call_time ON api_call_log(call_time);

CREATE TABLE IF NOT EXISTS instrument_master (
    instrument_key    TEXT PRIMARY KEY,
    trading_symbol    TEXT,
    exchange          TEXT,
    instrument_type   TEXT,
    expiry            TEXT,
    strike            REAL,
    option_type       TEXT,
    lot_size          INTEGER,
    tick_size         REAL,
    last_updated      TEXT
);
"""


# ─────────────────────────────────────────────────────────────────────────
# DATABASE LAYER
# ─────────────────────────────────────────────────────────────────────────
class Database:
    """
    Thread-safe SQLite persistence layer. This is the SINGLE SOURCE OF
    TRUTH for all engine state — session state, positions, option chain
    snapshots, calculated values, and audit logs are all written here,
    not held only in Python memory.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, param_list: list) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.executemany(sql, param_list)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def insert(self, table: str, data: dict) -> int:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        with self._lock:
            cur = self._conn.execute(sql, tuple(data.values()))
            self._conn.commit()
            return cur.lastrowid

    def update(self, table: str, data: dict, where: dict) -> int:
        set_clause = ", ".join(f"{k}=?" for k in data)
        where_clause = " AND ".join(f"{k}=?" for k in where)
        sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
        params = tuple(data.values()) + tuple(where.values())
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def upsert(self, table: str, key: dict, data: dict) -> int:
        """
        INSERT if no row matches `key`, else UPDATE. `key` and `data`
        must not share column names.
        """
        where_clause = " AND ".join(f"{k}=?" for k in key)
        existing = self.query_one(f"SELECT 1 FROM {table} WHERE {where_clause}",
                                   tuple(key.values()))
        if existing:
            return self.update(table, data, key)
        return self.insert(table, {**key, **data})

    def log_audit(self, level: str, logger_name: str, message: str,
                  context: Optional[dict] = None) -> None:
        self.insert("audit_log", {
            "log_time": now_ist().isoformat(),
            "level": level,
            "logger_name": logger_name,
            "message": message,
            "context_json": json.dumps(context, default=str) if context else None,
        })

    def log_api_call(self, category: str, endpoint: str, method: str,
                      status_code: Optional[int], response_time_ms: float,
                      rate_limited: bool = False,
                      error_message: Optional[str] = None) -> None:
        self.insert("api_call_log", {
            "call_time": now_ist().isoformat(),
            "category": category,
            "endpoint": endpoint,
            "method": method,
            "status_code": status_code,
            "response_time_ms": response_time_ms,
            "rate_limited": 1 if rate_limited else 0,
            "error_message": error_message,
        })

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ─────────────────────────────────────────────────────────────────────────
# LOGGING: console + daily rotating audit file + SQLite audit table
# ─────────────────────────────────────────────────────────────────────────
class SQLiteAuditHandler(logging.Handler):
    """
    Every log record emitted anywhere in the engine (via logging.getLogger)
    is automatically persisted to the audit_log table. Pass structured
    context via: logger.info("message", extra={"context": {...}})
    """

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

    def emit(self, record: logging.LogRecord) -> None:
        try:
            context = getattr(record, "context", None)
            self.db.log_audit(
                level=record.levelname,
                logger_name=record.name,
                message=record.getMessage(),
                context=context,
            )
        except Exception:
            # Logging failures must never crash the trading engine.
            self.handleError(record)


def setup_logging(db: Database, log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("nifty_algo")
    logger.setLevel(level)
    logger.handlers.clear()  # avoid duplicate handlers on repeated setup

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    logger.addHandler(console)

    # Daily rotating audit file (kept for 90 days) — satisfies the
    # "comprehensive audit log file for EOD analysis" requirement.
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "nifty_algo_audit.log", when="midnight", backupCount=90, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    sqlite_handler = SQLiteAuditHandler(db)
    sqlite_handler.setFormatter(fmt)
    sqlite_handler.setLevel(logging.DEBUG)
    logger.addHandler(sqlite_handler)

    logger.propagate = False
    return logger


# ─────────────────────────────────────────────────────────────────────────
# CONSOLE DISPLAY HELPERS (used heavily by later files' live dashboard)
# ─────────────────────────────────────────────────────────────────────────
def print_section(title: str, char: str = "=", width: int = 78) -> None:
    print(char * width)
    print(title.center(width))
    print(char * width)


def print_kv_table(data: dict, title: Optional[str] = None, width: int = 78) -> None:
    if title:
        print(f"\n--- {title} ---")
    for k, v in data.items():
        if isinstance(v, float):
            v_str = f"{v:,.4f}"
        else:
            v_str = str(v)
        print(f"  {k:<30}: {v_str}")


# ─────────────────────────────────────────────────────────────────────────
# UPSTOX API CLIENT
# ─────────────────────────────────────────────────────────────────────────
class UpstoxAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None,
                 response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class UpstoxClient:
    """
    Thin, rate-limited, fully-logged wrapper around the Upstox v2 REST API.

    SAFETY: place_order()/cancel_order() refuse to execute while
    config.paper_trade_mode is True — this is a hard guard independent of
    whatever logic in later files decides whether to call these methods,
    so a bug elsewhere cannot accidentally place a live order while the
    engine believes it is paper trading.
    """

    def __init__(self, config: Config, rate_limiter: RateLimiter, db: Database, logger: logging.Logger):
        self.config = config
        self.rate_limiter = rate_limiter
        self.db = db
        self.logger = logger

        self.session = requests.Session()
        try:
            retry_strategy = Retry(
                total=config.max_retries, backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "POST", "DELETE"],
                respect_retry_after_header=True,
            )
        except TypeError:
            # older urllib3 compatibility
            retry_strategy = Retry(
                total=config.max_retries, backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                method_whitelist=["GET", "POST", "DELETE"],
            )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {config.upstox_access_token}",
        })

    def _request(self, method: str, endpoint_key: str, category: str = "default",
                  path_params: Optional[dict] = None, params: Optional[dict] = None,
                  json_body: Optional[dict] = None) -> dict:
        path = API_ENDPOINTS[endpoint_key]
        if path_params:
            path = path.format(**path_params)
        url = f"{UPSTOX_BASE_URL}{path}"

        wait_time = self.rate_limiter.wait_if_needed(category)
        if wait_time > 0:
            self.logger.debug(f"Rate limiter delayed '{endpoint_key}' by {wait_time:.2f}s")

        start = time.monotonic()
        status_code = None
        try:
            resp = self.session.request(
                method, url, params=params, json=json_body,
                timeout=self.config.request_timeout_seconds,
            )
            status_code = resp.status_code
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code == 429:
                msg = "Rate limited by Upstox (HTTP 429) after retries exhausted"
                self.logger.warning(f"{endpoint_key}: {msg}")
                self.db.log_api_call(category, endpoint_key, method, status_code,
                                      elapsed_ms, rate_limited=True, error_message=msg)
                raise UpstoxAPIError(msg, status_code=429, response_body=resp.text)

            resp.raise_for_status()
            data = resp.json()
            self.db.log_api_call(category, endpoint_key, method, status_code, elapsed_ms)
            return data

        except requests.exceptions.RequestException as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            error_message = str(e)
            self.logger.error(f"API call failed: {endpoint_key} — {error_message}")
            self.db.log_api_call(category, endpoint_key, method, status_code,
                                  elapsed_ms, error_message=error_message)
            raise UpstoxAPIError(error_message, status_code=status_code) from e

    # ---- Read endpoints ----

    def validate_token(self) -> bool:
        try:
            data = self._request("GET", "profile", category="default")
            user_name = data.get("data", {}).get("user_name", "unknown")
            self.logger.info(f"Upstox token validated. User: {user_name}")
            return True
        except UpstoxAPIError as e:
            self.logger.error(f"Upstox token validation FAILED: {e}")
            return False

    def get_ltp(self, instrument_keys: list[str]) -> dict:
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "ltp", category="quote", params=params)
        return data.get("data", {})

    def get_full_quote(self, instrument_keys: list[str]) -> dict:
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "quotes", category="quote", params=params)
        return data.get("data", {})

    def get_historical_candles(self, instrument_key: str, interval: str,
                                from_date: str, to_date: str) -> list:
        path_params = {"instrument_key": instrument_key, "interval": interval,
                        "to_date": to_date, "from_date": from_date}
        data = self._request("GET", "historical_candle", category="historical",
                              path_params=path_params)
        return data.get("data", {}).get("candles", [])

    def get_intraday_candles(self, instrument_key: str, interval: str) -> list:
        path_params = {"instrument_key": instrument_key, "interval": interval}
        data = self._request("GET", "intraday_candle", category="historical",
                              path_params=path_params)
        return data.get("data", {}).get("candles", [])

    def get_option_contracts(self, instrument_key: str, expiry_date: Optional[str] = None) -> list:
        params = {"instrument_key": instrument_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        data = self._request("GET", "option_contracts", category="chain", params=params)
        return data.get("data", [])

    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list:
        params = {"instrument_key": instrument_key, "expiry_date": expiry_date}
        data = self._request("GET", "option_chain", category="chain", params=params)
        return data.get("data", [])

    def get_positions(self) -> list:
        data = self._request("GET", "positions", category="default")
        return data.get("data", [])

    def get_funds_and_margin(self) -> dict:
        data = self._request("GET", "funds_margin", category="default")
        return data.get("data", {})

    # ---- Write endpoints (LIVE ORDER ROUTING — guarded) ----

    def place_order(self, instrument_token: str, quantity: int, transaction_type: str,
                     order_type: str = "MARKET", product: str = "I", price: float = 0,
                     trigger_price: float = 0, validity: str = "DAY",
                     tag: str = "nifty_algo") -> dict:
        if self.config.paper_trade_mode:
            raise RuntimeError(
                "place_order() was called while PAPER_TRADE_MODE=True. This must "
                "never happen — the execution engine (File 4) must intercept order "
                "routing before it reaches the live Upstox client. Refusing to place "
                "a real order."
            )
        body = {
            "quantity": quantity, "product": product, "validity": validity,
            "price": price, "instrument_token": instrument_token,
            "order_type": order_type, "transaction_type": transaction_type,
            "trigger_price": trigger_price, "is_amo": False, "tag": tag,
        }
        data = self._request("POST", "place_order", category="order", json_body=body)
        return data.get("data", {})

    def cancel_order(self, order_id: str) -> dict:
        if self.config.paper_trade_mode:
            raise RuntimeError(
                "cancel_order() was called while PAPER_TRADE_MODE=True. Refusing."
            )
        params = {"order_id": order_id}
        data = self._request("DELETE", "cancel_order", category="order", params=params)
        return data.get("data", {})

    def get_order_details(self, order_id: str) -> dict:
        params = {"order_id": order_id}
        data = self._request("GET", "order_details", category="order", params=params)
        return data.get("data", {})


# ─────────────────────────────────────────────────────────────────────────
# SELF-TEST (run this file directly to verify the infrastructure layer)
# ─────────────────────────────────────────────────────────────────────────
def _self_test() -> None:
    print_section("NIFTY ALGO — CORE INFRASTRUCTURE SELF-TEST")

    config = load_config()
    print(repr(config))
    print_kv_table({
        "paper_trade_mode": config.paper_trade_mode,
        "starting_capital": config.starting_capital,
        "lot_size": config.lot_size,
        "nifty_strike_step": config.nifty_strike_step,
        "max_risk_per_trade_pct": config.max_risk_per_trade_pct,
        "max_daily_loss_pct": config.max_daily_loss_pct,
        "trading_window_start": config.trading_window_start,
        "trading_window_last_entry": config.trading_window_last_entry,
        "hard_exit_time": config.hard_exit_time,
        "max_concurrent_positions": config.max_concurrent_positions,
        "high_impact_events_loaded": len(config.high_impact_events),
        "db_path": str(config.db_path),
        "log_dir": str(config.log_dir),
    }, title="Configuration Loaded")

    db = Database(config.db_path)
    logger = setup_logging(db, config.log_dir,
                            level=getattr(logging, config.log_level.upper(), logging.INFO))
    logger.info("Core infrastructure self-test starting",
                extra={"context": {"paper_trade": config.paper_trade_mode}})

    rate_limiter = RateLimiter(config.rate_limits)

    print_section("DATABASE SCHEMA CHECK")
    tables = db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    for t in tables:
        print(f"  - {t['name']}")
    logger.info(f"Database schema initialized with {len(tables)} tables at {config.db_path}")

    if not config.upstox_access_token:
        logger.warning(
            "UPSTOX_ACCESS_TOKEN not set in env.txt — skipping live API validation. "
            "The engine will not be able to fetch market data until this is configured."
        )
        print_section("UPSTOX TOKEN: NOT CONFIGURED")
    else:
        client = UpstoxClient(config, rate_limiter, db, logger)
        ok = client.validate_token()
        if ok:
            print_section("UPSTOX TOKEN: VALID")
        else:
            print_section("UPSTOX TOKEN: INVALID / EXPIRED")
            logger.warning(
                "Upstox access tokens are typically valid only for the current "
                "trading day and must be regenerated daily via the OAuth login "
                "flow. Regenerate the token and update env.txt."
            )

    logger.info("Core infrastructure self-test complete.")
    print_section("SELF-TEST COMPLETE — CORE INFRASTRUCTURE READY")
    db.close()


if __name__ == "__main__":
    _self_test()