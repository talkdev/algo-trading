# core.py
# NIFTY Intraday Options Engine v3.0
# Foundation layer: config, database, API client, calendar, logging
# Evolved from nifty_algo_core.py with new engine requirements

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
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime, timedelta
from typing import Optional, Dict, List, Any, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────────────────────────────────────────
# CONSOLE ENCODING (Windows cp1252 safety)
# ─────────────────────────────────────────────
# Every CLI/backtest harness prints Unicode box-drawing tables; a
# legacy cp1252 console (stock Windows PowerShell/cmd before UTF-8 was
# the default) otherwise crashes the run with UnicodeEncodeError at the
# first banner. core is imported by every entry point (main, the
# engines, backtest_engine), so this one-time reconfigure fixes the
# whole suite. It is a no-op on UTF-8 terminals and where the stream is
# redirected/replaced by a test harness.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass

# ─────────────────────────────────────────────
# TIMEZONE SETUP
# ─────────────────────────────────────────────

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    try:
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
    except Exception:
        IST = None


NIFTY_ENGINE_PROFIT_PATCH_V31 = "3.1"
NIFTY_ENGINE_PROFIT_PATCH_V32 = "3.2"
NIFTY_ENGINE_PROFIT_PATCH_V33 = "3.3"
NIFTY_ENGINE_PROFIT_PATCH_V34 = "3.4"
NIFTY_ENGINE_PROFIT_PATCH_V35 = "3.5"
NIFTY_ENGINE_PROFIT_PATCH_V36 = "3.6"
NIFTY_ENGINE_PROFIT_PATCH_V37 = "3.7"
NIFTY_ENGINE_PROFIT_PATCH_V38 = "3.8"
# v3.9 (2026-09-09): VIX-11 expiry-day profitability pass. See the
# v3.3-tagged blocks in core.py / strategy_engine.py / execution_engine.py.
NIFTY_ENGINE_PROFIT_PATCH_V39 = "3.9"


def now_ist() -> datetime:
    """Return current datetime in IST."""
    if IST is not None:
        return datetime.now(IST)
    from datetime import timezone
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def today_ist() -> date:
    """Return current date in IST."""
    return now_ist().date()


# ── VRP data-error guard (single source of truth) ─────────────────────────
# The VRP anomaly bound used to exist in two copies: data_engine capped the
# smoothed series at max(8pp, 0.70 x ATM IV) while regime_engine blocked at
# a DTE-aware bound (0.92 on the expiry series, 0.70 elsewhere, plus an
# absolute realised-vol floor). v3.4 fixed only the regime copy, so on the
# 2026-09-08 0DTE session data_engine froze vrp_smoothed at its pre-noon
# value all afternoon while raw printed 15-17pp. Both layers now share this
# bound. Semantics stay local: data_engine CAPS (falls back to the previous
# smoothed value so one bad print cannot poison the series), regime_engine
# BLOCKS (treats the cycle as NEUTRAL).
VRP_DATA_ERROR_FRAC      = 0.70
VRP_DATA_ERROR_FRAC_DTE0 = 0.92
VRP_DATA_ERROR_FLOOR_PP  = 8.0
VRP_RV_DEAD_PCT          = 0.5


def vrp_anomaly_limit(atm_iv_pct: Optional[float], dte=None) -> float:
    """Upper bound for a believable raw VRP reading, in variance points.

    A low realised-to-implied ratio is the NORMAL state of the expiry
    series (pin risk + gamma priced into hours of remaining life), so the
    bound is looser on 0DTE. A genuinely dead bar feed is caught by the
    absolute VRP_RV_DEAD_PCT floor on realised vol instead.
    """
    try:
        _dte = int(dte) if dte is not None else None
    except (TypeError, ValueError):
        _dte = None
    _frac = VRP_DATA_ERROR_FRAC_DTE0 if _dte == 0 else VRP_DATA_ERROR_FRAC
    try:
        _iv = float(atm_iv_pct) if atm_iv_pct else 0.0
    except (TypeError, ValueError):
        _iv = 0.0
    return max(VRP_DATA_ERROR_FLOOR_PP, _frac * _iv)


def parse_ist_timestamp(ts) -> Optional[datetime]:
    """Parse a timestamp string or epoch into an IST-aware datetime."""
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            if IST is not None:
                return datetime.fromtimestamp(ts, tz=IST)
            from datetime import timezone
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        if isinstance(ts, str):
            s = ts.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                if IST is not None:
                    dt = dt.replace(tzinfo=IST)
            else:
                if IST is not None:
                    dt = dt.astimezone(IST)
            return dt
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────
# PATH CONSTANTS
# ─────────────────────────────────────────────

BASE_DIR            = Path(__file__).resolve().parent
ENV_FILE            = BASE_DIR / "env.txt"
DEFAULT_DB_PATH     = BASE_DIR / "data" / "nifty_algo_v3.db"
DEFAULT_LOG_DIR     = BASE_DIR / "logs"
DEFAULT_EVENTS_FILE = BASE_DIR / "high_impact_events.json"
DEFAULT_HOLIDAYS_FILE = BASE_DIR / "nse_holidays.json"

# ─────────────────────────────────────────────
# UPSTOX API CONSTANTS
# ─────────────────────────────────────────────

INSTRUMENT_KEY_NIFTY_SPOT = "NSE_INDEX|Nifty 50"
INSTRUMENT_KEY_INDIA_VIX  = "NSE_INDEX|India VIX"
UPSTOX_BASE_URL           = "https://api.upstox.com/v2"

API_ENDPOINTS = {
    "profile":           "/user/profile",
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

# ─────────────────────────────────────────────
# ENV TEMPLATE
# ─────────────────────────────────────────────

ENV_TEMPLATE = """# ─────────────────────────────────────────────
# NIFTY Algo Trading Engine v4.1 — env.txt
# ─────────────────────────────────────────────
# This file holds ONE thing: today's Upstox access token. Nothing else.
#
# v4.1 moved every engine tunable (windows, costs, thresholds, exits,
# calibration, sizing) INTO the code — see the documented defaults in
# `load_config()` in core.py. Those defaults reproduce the previously
# shipped env.txt values exactly, so behaviour is unchanged.
#
# Extra/unknown keys in this file are ignored by the engine. Your API
# key / secret / redirect URI are NOT needed here either: they are only
# used on the Upstox login page when you generate the token below.
#
# Daily ritual: paste today's token after UPSTOX_ACCESS_TOKEN= and run.
# ─────────────────────────────────────────────

UPSTOX_ACCESS_TOKEN=
"""


# ─────────────────────────────────────────────
# ENV FILE HELPERS
# ─────────────────────────────────────────────

def ensure_env_file(path: Path) -> None:
    """Create env.txt template if it does not exist."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ENV_TEMPLATE, encoding="utf-8")
        print(
            f"[SETUP] Created env.txt at {path}. "
            f"Fill in credentials before running."
        )


def load_env_file(path: Path) -> dict:
    """Load key=value pairs from env.txt, ignoring comments and blanks."""
    env: dict = {}
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
        parts = val.strip().split(":")
        return dtime(int(parts[0]), int(parts[1]))
    except Exception:
        return default


# ─────────────────────────────────────────────
# NSE HOLIDAYS & HIGH IMPACT EVENTS
# ─────────────────────────────────────────────

def load_nse_holidays(path: Path = DEFAULT_HOLIDAYS_FILE) -> set:
    """Load NSE holiday dates from JSON file. Returns set of ISO date strings."""
    if not path.exists():
        path.write_text("[]", encoding="utf-8")
        print(f"[SETUP] Created empty {path}. Populate with NSE holiday dates.")
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return set(raw)
        if isinstance(raw, dict):
            return set(raw.keys())
        return set()
    except json.JSONDecodeError:
        print(f"[WARNING] {path} is not valid JSON. Treating as empty.")
        return set()


def load_high_impact_events(path: Path = DEFAULT_EVENTS_FILE) -> Dict[date, str]:
    """
    Load high-impact event dates from JSON file.
    Supports formats:
      {"2026-02-01": "Budget", ...}
      {"Budget": {"dates": ["2026-02-01"], "description": "..."}}
      {"Budget": ["2026-02-01", ...]}
    Returns dict mapping date → description.
    """
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
        print(f"[SETUP] Created empty {path}. Populate with event dates.")
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[WARNING] {path} is not valid JSON ({e}). Treating as empty.")
        return {}
    if not raw:
        return {}

    result: Dict[date, str] = {}

    for key, val in raw.items():
        if isinstance(val, dict) and "dates" in val:
            desc = val.get("description", key)
            for d in val.get("dates", []):
                if isinstance(d, str) and len(d) == 10:
                    try:
                        result[date.fromisoformat(d)] = desc
                    except ValueError:
                        pass
        elif isinstance(val, list):
            for d in val:
                if isinstance(d, str) and len(d) == 10:
                    try:
                        result[date.fromisoformat(d)] = key
                    except ValueError:
                        pass
        elif isinstance(val, str) and len(key) == 10:
            try:
                result[date.fromisoformat(key)] = val
            except ValueError:
                pass

    return result


_NSE_HOLIDAYS_CACHE: Optional[set] = None
_HIGH_IMPACT_EVENTS_CACHE: Optional[Dict[date, str]] = None


def get_nse_holidays() -> set:
    global _NSE_HOLIDAYS_CACHE
    if _NSE_HOLIDAYS_CACHE is None:
        _NSE_HOLIDAYS_CACHE = load_nse_holidays()
    return _NSE_HOLIDAYS_CACHE


def get_high_impact_events() -> Dict[date, str]:
    global _HIGH_IMPACT_EVENTS_CACHE
    if _HIGH_IMPACT_EVENTS_CACHE is None:
        _HIGH_IMPACT_EVENTS_CACHE = load_high_impact_events()
    return _HIGH_IMPACT_EVENTS_CACHE


# ─────────────────────────────────────────────
# EXPIRY CALENDAR
# ─────────────────────────────────────────────

class ExpiryCalendar:
    """
    NIFTY weekly Tuesday expiry calendar for 2026.
    All methods are class methods — no instantiation needed.
    """

    @classmethod
    def is_holiday(cls, d: date) -> bool:
        """Return True if d is a weekend or NSE holiday."""
        if d.weekday() >= 5:
            return True
        return d.isoformat() in get_nse_holidays()

    @classmethod
    def is_event_day(cls, d: Optional[date] = None) -> str:
        """
        Return event description if d is a high-impact event day, else empty string.
        Empty string is falsy so callers can use: if ExpiryCalendar.is_event_day(d):
        """
        if d is None:
            d = today_ist()
        return get_high_impact_events().get(d, "")

    @classmethod
    def get_next_expiry(cls, from_date: Optional[date] = None,
                        after_close: bool = False) -> date:
        """
        Return the next NIFTY weekly expiry (Tuesday).
        If today is Tuesday and market is still open, return today.
        If today is Tuesday and market has closed, return next Tuesday.
        Rolls back to Monday if Tuesday is a holiday.
        """
        if from_date is None:
            from_date = today_ist()
        if not after_close:
            after_close = now_ist().time() > dtime(15, 30)

        # Days until next Tuesday (weekday 1)
        days_ahead = (1 - from_date.weekday()) % 7
        if days_ahead == 0 and after_close:
            days_ahead = 7
        candidate = from_date + timedelta(days=days_ahead)

        # Roll back if holiday
        while cls.is_holiday(candidate):
            candidate -= timedelta(days=1)
        return candidate

    @classmethod
    def get_dte(cls, from_date: Optional[date] = None) -> int:
        """
        Return trading-day DTE to next expiry from from_date.
        DTE 0 = expiry day itself.
        DTE 1 = one trading day before expiry (Monday).
        """
        if from_date is None:
            from_date = today_ist()
        expiry = cls.get_next_expiry(from_date)
        if expiry <= from_date:
            return 0
        count = 0
        d = from_date + timedelta(days=1)
        while d <= expiry:
            if not cls.is_holiday(d):
                count += 1
            d += timedelta(days=1)
        return count

    @classmethod
    def get_day_type(cls, d: Optional[date] = None) -> str:
        """Return a label describing the trading day type."""
        if d is None:
            d = today_ist()
        if cls.is_holiday(d):
            return "NON_TRADING"
        dte = cls.get_dte(d)
        weekday = d.weekday()
        if dte == 0:
            return "EXPIRY_DAY"
        if dte == 1:
            return "PRE_EXPIRY"
        if weekday == 2:
            return "NEW_CYCLE"
        if weekday == 4:
            return "WEEKEND_RISK"
        if weekday == 0:
            return "PRE_EXPIRY"
        return "MID_WEEK"

    @classmethod
    def is_monthly_expiry(cls, d: Optional[date] = None) -> bool:
        """Return True if d is the last Tuesday of the month (monthly expiry)."""
        if d is None:
            d = today_ist()
        if d.weekday() != 1:
            return False
        return (d + timedelta(days=7)).month != d.month

    @classmethod
    def get_next_trading_day(cls, from_date: Optional[date] = None) -> Optional[date]:
        """Return the next trading day after from_date."""
        if from_date is None:
            from_date = today_ist()
        d = from_date + timedelta(days=1)
        for _ in range(14):
            if not cls.is_holiday(d):
                return d
            d += timedelta(days=1)
        return None

    @classmethod
    def get_day_label(cls, d: Optional[date] = None) -> str:
        """Return day label: MONDAY, TUESDAY, ..., FRIDAY, WEEKEND."""
        if d is None:
            d = today_ist()
        labels = {
            0: "MONDAY", 1: "TUESDAY", 2: "WEDNESDAY",
            3: "THURSDAY", 4: "FRIDAY"
        }
        return labels.get(d.weekday(), "WEEKEND")


# ─────────────────────────────────────────────
# CONFIG DATACLASS
# ─────────────────────────────────────────────

@dataclass(frozen=True, repr=False)
class Config:
    """
    Immutable configuration loaded from env.txt.
    All fields have safe defaults. Calibration overrides thresholds at runtime.
    """

    # Upstox credentials
    upstox_api_key:        str
    upstox_api_secret:     str
    upstox_redirect_uri:   str
    upstox_access_token:   str

    # Trading mode
    paper_trade_mode:      bool

    # Capital & risk
    starting_capital:      float
    max_daily_loss_pct:    float
    max_risk_per_trade_pct: float

    # NIFTY contract
    lot_size:              int
    nifty_strike_step:     int

    # Transaction costs
    stt_options_sell:      float
    stt_options_exercise:  float
    brokerage_per_order:   float
    exchange_txn_rate:     float
    sebi_rate:             float
    stamp_duty_buy_options: float

    # Trading windows
    trading_window_start:       dtime
    trading_window_last_entry:  dtime
    hard_exit_time:             dtime
    tuesday_hard_exit:          dtime
    tuesday_last_entry:         dtime

    # Position limits
    max_concurrent_positions:  int
    max_entries_per_day:       int

    # Paths
    db_path:   Path
    log_dir:   Path
    log_level: str

    # API settings
    request_timeout_seconds: float
    max_retries:             int
    rate_limits:             dict

    # Technical analysis
    adx_period:             int
    adx_trend_threshold:    float
    adx_strong_threshold:   float
    ema_fast:               int
    ema_slow:               int
    mtf_resample_15:        str
    mtf_resample_60:        str
    min_bars_for_adx:       int
    min_bars_for_ema_slow:  int

    # VIX regime thresholds
    vix_suppressed:  float   # < this = SUPPRESSED (2026 normal)
    vix_low:         float   # < this = LOW
    vix_normal:      float   # < this = NORMAL
    vix_elevated:    float   # < this = ELEVATED, >= this = HIGH

    # ABORT triggers
    abort_vix_spike_pct:  float   # VIX up this % from prev close = ABORT
    abort_vix_absolute:   float   # VIX above this = ABORT
    vix_fail_limit:       int     # consecutive VIX failures before ABORT

    # VRP thresholds (defaults, overridden by calibration)
    vrp_sell_threshold_default: float
    vrp_fair_threshold_default: float
    vrp_smoothing_cycles:       int

    # Regime settings
    regime_calc_interval_sec:  int
    regime_persistence_cycles: int
    day_move_used_block_pct:   float
    # v3.3: day_move_used_pct divides a high-low RANGE by a straddle,
    # and a straddle prices |displacement|, not range. For a driftless
    # diffusion E[range]/E[|displacement|] = 2.0 in continuous time and
    # 1.933 when sampled at 1-minute bars, which is how this engine
    # observes the session. Without this factor a perfectly ordinary
    # day scores ~193 against a 125 threshold and the volatility gate
    # returns NEUTRAL on every cycle. Env-driven so it can be re-fitted.
    day_move_range_factor:     float

    # OI / positioning thresholds (defaults, overridden by calibration)
    oi_change_lookback_min: int
    oi_buildup_threshold:   float
    oi_unwind_threshold:    float
    oi_wall_strong:         float
    oi_wall_moderate:       float
    pcr_bullish_threshold:  float
    pcr_bearish_threshold:  float
    skew_bearish_threshold: float
    skew_bullish_threshold: float

    # Exit rules
    delta_close_threshold:      float
    spot_proximity_pts:         int
    price_stop_straddle_mult:   float
    profit_lock_pct_dte0:       float
    profit_lock_pct_dte1plus:   float
    cheap_buyback_pts:          float
    cheap_buyback_after_time:   dtime

    # Calibration
    min_trading_days_for_calibration: int
    calibration_interval_sec:         int
    spot_bar_interval_sec:            int
    hv_lookback_days:                 int

    # Event / special day
    event_size_multiplier:       float
    defined_risk_only_on_event:  bool
    tuesday_early_exit_enabled:  bool

    # v3.9: normal (non-event) day-size multipliers per weekday. These are
    # the fallback whenever the calibrator has no valid state yet (startup,
    # tier-0, and every backtest replay). They mirror the calibration
    # dataclass defaults. They must NOT fall back to event_size_multiplier:
    # that is a budget/event-day reducer, and letting it leak into an
    # uncalibrated Tuesday quietly cut every position to 25% of intended
    # size (measured 2026-09-08 replay: size_multiplier 0.25 -> 0.54 lots
    # -> rejected below min_lots_fraction).
    day_size_monday:     float
    day_size_tuesday:    float
    day_size_wednesday:  float
    day_size_thursday:   float
    day_size_friday:     float

    # Straddle settings
    straddle_explosion_pct:  float
    straddle_roc_window_min: int
    straddle_roc_alert_pct:  float

    # Phantom trade tracking
    phantom_trade_tracking: bool

    # Misc
    gift_nifty_instrument_key: str

    # ── v3.1 profitability calibration ────────────────────────────────────
    # Opening-range width is classified as a FRACTION OF SPOT rather than in
    # absolute points, so the classification does not silently drift toward
    # "WIDE" as NIFTY rises. Chosen to reproduce the old 50/100/150/200pt
    # bands at a ~25,000 index and to scale correctly above it.
    or_pct_very_narrow:      float = 0.0020
    or_pct_narrow:           float = 0.0036
    or_pct_moderate:         float = 0.0055
    or_pct_wide:             float = 0.0078
    # Opening range measured against the opening ATM straddle, i.e. against
    # the market's own priced expectation for the day's range. The more
    # conservative of the two classifications wins.
    or_straddle_very_narrow: float = 0.24
    or_straddle_narrow:      float = 0.42
    or_straddle_moderate:    float = 0.62
    or_straddle_wide:        float = 0.86
    # Structural distances as a fraction of spot (replace hardcoded points).
    spot_proximity_pct:      float = 0.0016
    spot_velocity_pct:       float = 0.0014
    # Fraction of the structural (wing) loss that a working stop is assumed
    # to avoid. 0.0 = size on the full wing loss, 1.0 = trust the stop
    # completely. On NIFTY 0DTE the stop is NOT honoured through a gamma gap,
    # so sizing takes only partial credit for it.
    stop_efficacy:           float = 0.55
    # Probability that the stop is jumped and the structure prints toward the
    # wing (gap / gamma tail). Priced explicitly in the EV gate.
    gamma_tail_prob_dte0:    float = 0.055
    gamma_tail_prob_dte1p:   float = 0.025
    # Slippage model, in multiples of the half-spread, per leg.
    entry_slippage_mult:     float = 0.35
    exit_slippage_mult:      float = 2.25
    # Absolute rupee tolerance added to the relative bid/ask gate so that
    # cheap protective wings (Rs 1-3) are not rejected over a 0.10 tick.
    spread_abs_tolerance:    float = 0.85
    # Highest DTE at which the credit structures may be opened.
    max_dte_tradeable:       int   = 4

    # ── v3.2 profitability calibration ────────────────────────────────
    # Premium stop as a multiple of the NET credit, by DTE. The v3.1
    # value (a flat 2.5, i.e. a loss of 1.5x the credit) against a 35%
    # target implied an 81% break-even win rate, which no 0.15-0.22
    # delta NIFTY structure delivers. Paired with the targets below
    # they put the break-even win rate near 55%.
    # v3.9: DTE-0 stop widened 1.40 -> 1.60. A 0.30-delta expiry short
    # with a 45-point gap trades inside a 25-30 point adverse excursion
    # (measured 2026-09-08: 25.3pts, 1.33x credit) and a 1.40x stop
    # leaves less than a point of room once liquidation slippage is
    # charged; 1.60x leaves ~4.5pts. The wider stop is the cost of the
    # gamma-gap that a 0DTE stop is not honoured through.
    stop_mult_dte0:            float = 1.60
    stop_mult_dte1:            float = 1.55
    stop_mult_dte2p:           float = 1.70
    # Profit target as a fraction of the net credit, by DTE. Paired with
    # the stop multiples above: 0.50 against 1.40 is reward/risk 1.25.
    # v3.9: DTE-0 target raised 0.50 -> 0.70. Against the wider 1.60x
    # stop a 50% target would be reward/risk 0.83 (worse than 1:1);
    # 0.70 against the 0.60x loss is reward/risk 1.17, restoring the
    # engine's historical 1.1-1.25 posture on a VIX-11 day where the
    # whole credit is ~18 points.
    target_pct_dte0:           float = 0.70
    target_pct_dte1:           float = 0.45
    target_pct_dte2p:          float = 0.40
    # Short-leg delta at which the engine closes, by DTE.
    delta_close_dte0:          float = 0.45
    delta_close_dte1p:         float = 0.30
    # Spot backstop geometry (replaces 0.42 x opening straddle, which
    # placed the stop far INSIDE the short strike).
    price_stop_wing_frac:      float = 0.30
    price_stop_min_pts:        float = 25.0
    price_stop_max_frac_of_dist: float = 0.40
    # Delta-primary strike selection.
    short_delta_flat:          float = 0.32
    short_delta_trend:         float = 0.30
    short_delta_strong:        float = 0.28
    em_band_lo:                float = 0.80
    em_band_hi:                float = 1.35
    # v3.3: structure-relative proximity defense on expiry day. The exit
    # fires when spot has covered this fraction of the entry gap to the
    # short strike (bounded by the absolute proximity setting), so a
    # delta-0.3 short 45 points away is defended at 70% of the gap -
    # not 5 points after entry by an absolute 40pt band.
    prox_gap_frac_dte0:        float = 0.70
    # Friction discipline.
    max_friction_frac_of_credit:  float = 0.28
    max_brokerage_frac_of_credit: float = 0.15
    min_target_over_friction:     float = 1.25
    # v3.3: DTE-0 credit/risk ladder (VIX-scaled in compute_params).
    credit_risk_ratio_dte0_early: float = 0.16
    credit_risk_ratio_dte0_mid:   float = 0.13
    credit_risk_ratio_dte0_late:  float = 0.10
    credit_ratio_vix_ref:         float = 13.5
    # v3.3: EV-gate honesty bounds. The vendor 0DTE IV stamp may not
    # dominate the straddle-implied sigma, and the ATM IV stamp may not
    # exceed this multiple of the day's cash VIX.
    iv_sigma_cap_ratio:        float = 1.15
    atm_iv_vix_cap:            float = 1.35
    # v3.3: minimum edge for the EV gate.
    min_ev_frac_of_credit:     float = 0.03
    min_ev_frac_of_friction:   float = 0.35
    # v3.3: p_win blend weights (model / empirical prior / market delta).
    ev_blend_model_w:          float = 0.40
    ev_blend_prior_w:          float = 0.30
    ev_blend_market_w:         float = 0.30
    ev_strong_sell_prior_bonus: float = 0.05
    ev_regime_align_bonus:     float = 0.05
    # Minimum economic size, in lots, before a trade is worth doing.
    min_lots_fraction:         float = 0.60
    # Structure economics.
    wing_cost_frac_max:        float = 0.50
    condor_weak_side_min_frac: float = 0.30
    # ── v4.2: fresh-weekly (DTE >= 2) intraday premium selling ──────────
    # A weekly option with 3-4 sessions left carries overnight gap vega,
    # so the professional short-delta is ~16-20, NOT the 0.30 an 0DTE
    # short uses. Measured 2026-09-09/10 (DTE4/DTE3): the intraday-EM
    # strike clamp was forcing the condor shorts to 0.31-0.42 delta on
    # those days, which (a) made the long wing 55-80% of the short and
    # tripped wing_cost_frac_max on every candidate and (b) put the
    # threat line ~100 points out on a day that only moved 100. The
    # wide ~0.18-delta condor cleared its round trip on every tested
    # entry of both sessions, including the CPI two-way chop.
    short_delta_flat_weekly:   float = 0.24
    short_delta_trend_weekly:  float = 0.22
    short_delta_strong_weekly: float = 0.18
    em_band_lo_weekly:         float = 0.55
    em_band_hi_weekly:         float = 2.10
    # Weekly CONDOR shorts are sanity-banded in the weekly chain's own
    # ATM straddle (expiry horizon), not the shrinking intraday EM.
    em_band_hi_condor_weekly:  float = 1.35
    # Weekly wings (multi-day vega) are inherently pricier relative to
    # their shorts than 0DTE wings; 0.50 was calibrated for expiry day.
    wing_cost_frac_max_weekly: float = 0.58
    # On DTE3/4 RANGE sessions with ADX in [trend, strong) the price
    # classifier still says RANGE (mean-reverting, not trending); allow
    # a WIDE condor (shorts forced to the strong-delta target below) up
    # to the strong-ADX cutoff, at a size discount.
    range_adx_wide_max:        float = 28.0
    range_adx_wide_size:       float = 0.80
    # UNCLEAR OI positioning on an otherwise textbook range day
    # (rich VRP, narrow OR, flat ADX, price = RANGE) previously banned
    # the symmetric condor outright on DTE3/4. OI positioning is a
    # confirmation, not a prerequisite, for a delta-neutral structure;
    # trade it at this size discount.
    unclear_range_size_weekly: float = 0.75
    # EV-gate adverse-excursion calibration for fresh weeklies: the
    # greeks-carry "stop severity" assumed an instantaneous move at
    # entry delta with a 1.25 stress factor and ZERO theta credit. The
    # real exit ladder (spot proximity ~40pts inside the short)
    # realised 4-8pt losses on 28-60pt credits across the 08-10 Sep
    # replays, i.e. ~2.5-3x less than the 18-35pt the model charged.
    # Apply this discount to the carry on DTE >= 2 (theta over the
    # intended multi-hour hold). 0DTE keeps the old conservative value.
    ev_carry_discount_dte2p:   float = 0.62
    # Fast intraday trend timeframe (15m ADX cannot mature intraday).
    adx_fast_resample:         str   = "300s"

    def __repr__(self) -> str:
        def mask(s: str) -> str:
            if not s:
                return "<empty>"
            return (s[:4] + "..." + s[-2:]) if len(s) > 8 else "***"
        return (
            f"Config(paper={self.paper_trade_mode}, "
            f"lot={self.lot_size}, "
            f"capital={self.starting_capital:,.0f}, "
            f"api_key={mask(self.upstox_api_key)}, "
            f"token={mask(self.upstox_access_token)})"
        )


# ─────────────────────────────────────────────
# CONFIG LOADER
# ─────────────────────────────────────────────

def load_config(env_file: Path = ENV_FILE) -> Config:
    """
    Load Config from env.txt (file values override OS environment).
    Applies safety checks and clamps dangerous values.

    v4.1: every default below is the authoritative engine tuning — it
    reproduces the previously shipped env.txt values exactly, so a
    token-only env.txt behaves identically to the old full file. Any key
    still present in env.txt (or OS env) overrides its default, so
    deliberate tuning keeps working; unknown keys are ignored.
    """
    ensure_env_file(env_file)
    file_env = load_env_file(env_file)
    # File values override OS env
    env = {**os.environ, **file_env}

    # ── Risk parameter safety ────────────────────────────────────────────────
    # v3.10 (patch_v1): 0.6% per trade capped every defined-risk near-weekly
    # structure at a single lot, below the size where fixed brokerage (per
    # order, not per lot) can be amortised against the slow DTE-3/4 theta —
    # so directionally-correct near-weekly trades still lost to costs. A
    # defined-risk (capped-loss) intraday structure can carry a larger per-
    # trade budget than naked selling; 1.2% per trade / 4.0% daily lets it
    # size to a cost-viable 2+ lots while keeping the daily stop intact.
    max_daily_loss_pct = _get_float(env, "MAX_DAILY_LOSS_PCT", 0.04)
    max_risk_per_trade_pct = _get_float(env, "MAX_RISK_PER_TRADE_PCT", 0.012)

    # Clamp: max risk per trade must be < max daily loss / 3
    safe_max = round(max_daily_loss_pct / 3.0 - 0.001, 4)
    if max_risk_per_trade_pct >= max_daily_loss_pct / 3.0:
        print(
            f"[WARNING] MAX_RISK_PER_TRADE_PCT clamped to {safe_max} "
            f"(must be < MAX_DAILY_LOSS_PCT/3)"
        )
        max_risk_per_trade_pct = max(safe_max, 0.001)

    # ── Paper trade safety ───────────────────────────────────────────────────
    paper_trade_mode = _get_bool(env, "PAPER_TRADE_MODE", True)
    live_rates_verified = _get_bool(env, "LIVE_RATES_VERIFIED", False)
    if not paper_trade_mode and not live_rates_verified:
        print("[SAFETY] LIVE_RATES_VERIFIED=false → forcing PAPER_TRADE_MODE=true")
        paper_trade_mode = True

    # ── Paths ────────────────────────────────────────────────────────────────
    db_path = Path(env.get("DB_PATH", str(DEFAULT_DB_PATH)))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    log_dir = Path(env.get("LOG_DIR", str(DEFAULT_LOG_DIR)))
    if not log_dir.is_absolute():
        log_dir = BASE_DIR / log_dir

    # ── Rate limits ──────────────────────────────────────────────────────────
    rate_limits = {
        "quote":      {"per_second": 8,  "per_minute": 120, "per_30min": 1200},
        "historical": {"per_second": 5,  "per_minute": 60,  "per_30min": 600},
        "chain":      {"per_second": 3,  "per_minute": 30,  "per_30min": 300},
        "order":      {"per_second": 3,  "per_minute": 30,  "per_30min": 200},
        "default":    {"per_second": 5,  "per_minute": 60,  "per_30min": 600},
    }

    return Config(
        # Credentials
        upstox_api_key=env.get("UPSTOX_API_KEY", ""),
        upstox_api_secret=env.get("UPSTOX_API_SECRET", ""),
        upstox_redirect_uri=env.get("UPSTOX_REDIRECT_URI", ""),
        upstox_access_token=env.get("UPSTOX_ACCESS_TOKEN", ""),

        # Mode
        paper_trade_mode=paper_trade_mode,

        # Capital
        starting_capital=_get_float(env, "STARTING_CAPITAL", 1_000_000.0),
        max_daily_loss_pct=max_daily_loss_pct,
        max_risk_per_trade_pct=max_risk_per_trade_pct,

        # Contract
        lot_size=_get_int(env, "NIFTY_LOT_SIZE", 65),
        nifty_strike_step=_get_int(env, "NIFTY_STRIKE_STEP", 50),

        # Costs
        stt_options_sell=_get_float(env, "STT_OPTIONS_SELL", 0.001),
        stt_options_exercise=_get_float(env, "STT_OPTIONS_EXERCISE", 0.00125),
        brokerage_per_order=_get_float(env, "BROKERAGE_PER_ORDER", 20.0),
        exchange_txn_rate=_get_float(env, "EXCHANGE_TXN_RATE", 0.0003553),
        sebi_rate=_get_float(env, "SEBI_RATE", 0.000001),
        stamp_duty_buy_options=_get_float(env, "STAMP_DUTY_BUY_OPTIONS", 0.00003),

        # Windows
        trading_window_start=_get_time(env, "TRADING_WINDOW_START", dtime(9, 45)),
        trading_window_last_entry=_get_time(env, "TRADING_WINDOW_LAST_ENTRY", dtime(14, 0)),
        hard_exit_time=_get_time(env, "HARD_EXIT_TIME", dtime(15, 0)),
        tuesday_hard_exit=_get_time(env, "TUESDAY_HARD_EXIT", dtime(15, 0)),
        tuesday_last_entry=_get_time(env, "TUESDAY_LAST_ENTRY", dtime(12, 30)),

        # Limits
        max_concurrent_positions=_get_int(env, "MAX_CONCURRENT_POSITIONS", 1),
        max_entries_per_day=_get_int(env, "MAX_ENTRIES_PER_DAY", 3),

        # Paths
        db_path=db_path,
        log_dir=log_dir,
        log_level=env.get("LOG_LEVEL", "INFO"),

        # API
        request_timeout_seconds=_get_float(env, "REQUEST_TIMEOUT_SECONDS", 10.0),
        max_retries=_get_int(env, "MAX_RETRIES", 3),
        rate_limits=rate_limits,

        # Technical
        adx_period=_get_int(env, "ADX_PERIOD", 14),
        adx_trend_threshold=_get_float(env, "ADX_TREND_THRESHOLD", 20.0),
        adx_strong_threshold=_get_float(env, "ADX_STRONG_THRESHOLD", 28.0),
        ema_fast=_get_int(env, "EMA_FAST", 9),
        ema_slow=_get_int(env, "EMA_SLOW", 21),
        mtf_resample_15=env.get("MTF_RESAMPLE_15", "900s"),
        mtf_resample_60=env.get("MTF_RESAMPLE_60", "3600s"),
        min_bars_for_adx=_get_int(env, "MIN_BARS_FOR_ADX", 20),
        min_bars_for_ema_slow=_get_int(env, "MIN_BARS_FOR_EMA_SLOW", 25),

        # VIX regimes
        vix_suppressed=_get_float(env, "VIX_SUPPRESSED", 12.5),
        vix_low=_get_float(env, "VIX_LOW", 16.0),
        vix_normal=_get_float(env, "VIX_NORMAL", 22.0),
        vix_elevated=_get_float(env, "VIX_ELEVATED", 28.0),

        # ABORT
        abort_vix_spike_pct=_get_float(env, "ABORT_VIX_SPIKE_PCT", 15.0),
        abort_vix_absolute=_get_float(env, "ABORT_VIX_ABSOLUTE", 24.0),
        vix_fail_limit=_get_int(env, "VIX_FAIL_LIMIT", 5),

        # VRP
        vrp_sell_threshold_default=_get_float(env, "VRP_SELL_THRESHOLD", 2.0),
        vrp_fair_threshold_default=_get_float(env, "VRP_FAIR_THRESHOLD", 1.0),
        vrp_smoothing_cycles=_get_int(env, "VRP_SMOOTHING_CYCLES", 5),

        # Regime
        regime_calc_interval_sec=_get_int(env, "REGIME_CALC_INTERVAL_SEC", 15),
        regime_persistence_cycles=_get_int(env, "REGIME_PERSISTENCE_CYCLES", 3),
        day_move_used_block_pct=_get_float(env, "DAY_MOVE_USED_BLOCK_PCT", 125.0),
        day_move_range_factor=min(max(
            _get_float(env, "DAY_MOVE_RANGE_FACTOR", 1.93), 1.0), 2.5),

        # OI / positioning
        oi_change_lookback_min=_get_int(env, "OI_CHANGE_LOOKBACK_MIN", 30),
        oi_buildup_threshold=_get_float(env, "OI_BUILDUP_THRESHOLD", 0.08),
        oi_unwind_threshold=_get_float(env, "OI_UNWIND_THRESHOLD", -0.08),
        oi_wall_strong=_get_float(env, "OI_WALL_STRONG", 2.5),
        oi_wall_moderate=_get_float(env, "OI_WALL_MODERATE", 1.7),
        pcr_bullish_threshold=_get_float(env, "PCR_BULLISH_THRESHOLD", 0.72),
        pcr_bearish_threshold=_get_float(env, "PCR_BEARISH_THRESHOLD", 1.28),
        skew_bearish_threshold=_get_float(env, "SKEW_BEARISH_THRESHOLD", 3.0),
        skew_bullish_threshold=_get_float(env, "SKEW_BULLISH_THRESHOLD", 0.95),

        # Exit rules
        delta_close_threshold=_get_float(env, "DELTA_CLOSE_THRESHOLD", 0.28),
        spot_proximity_pts=_get_int(env, "SPOT_PROXIMITY_PTS", 40),
        price_stop_straddle_mult=_get_float(env, "PRICE_STOP_STRADDLE_MULT", 0.42),
        profit_lock_pct_dte0=_get_float(env, "PROFIT_LOCK_PCT_DTE0", 0.40),
        profit_lock_pct_dte1plus=_get_float(env, "PROFIT_LOCK_PCT_DTE1PLUS", 0.25),
        cheap_buyback_pts=_get_float(env, "CHEAP_BUYBACK_PTS", 5.0),
        cheap_buyback_after_time=_get_time(env, "CHEAP_BUYBACK_AFTER_TIME", dtime(13, 0)),

        # Calibration
        min_trading_days_for_calibration=_get_int(
            env, "MIN_TRADING_DAYS_FOR_CALIBRATION", 20
        ),
        calibration_interval_sec=_get_int(env, "CALIBRATION_INTERVAL_SEC", 3600),
        spot_bar_interval_sec=_get_int(env, "SPOT_BAR_INTERVAL_SEC", 60),
        hv_lookback_days=_get_int(env, "HV_LOOKBACK_DAYS", 20),

        # Event
        event_size_multiplier=_get_float(env, "EVENT_SIZE_MULTIPLIER", 0.25),
        day_size_monday=min(max(_get_float(env, "DAY_SIZE_MONDAY", 0.60), 0.10), 1.20),
        day_size_tuesday=min(max(_get_float(env, "DAY_SIZE_TUESDAY", 0.85), 0.10), 1.20),
        day_size_wednesday=min(max(_get_float(env, "DAY_SIZE_WEDNESDAY", 0.65), 0.10), 1.20),
        day_size_thursday=min(max(_get_float(env, "DAY_SIZE_THURSDAY", 0.65), 0.10), 1.20),
        day_size_friday=min(max(_get_float(env, "DAY_SIZE_FRIDAY", 0.55), 0.10), 1.20),
        defined_risk_only_on_event=_get_bool(env, "DEFINED_RISK_ONLY_ON_EVENT", True),
        tuesday_early_exit_enabled=_get_bool(env, "TUESDAY_EARLY_EXIT_ENABLED", True),

        # Straddle
        straddle_explosion_pct=_get_float(env, "STRADDLE_EXPLOSION_PCT", 18.0),
        straddle_roc_window_min=_get_int(env, "STRADDLE_ROC_WINDOW_MIN", 15),
        straddle_roc_alert_pct=_get_float(env, "STRADDLE_ROC_ALERT_PCT", 12.0),

        # Phantom
        phantom_trade_tracking=_get_bool(env, "PHANTOM_TRADE_TRACKING", True),

        # Misc
        gift_nifty_instrument_key=env.get("GIFT_NIFTY_INSTRUMENT_KEY", "").strip(),

        # v3.1 profitability calibration
        or_pct_very_narrow=_get_float(env, "OR_PCT_VERY_NARROW", 0.0020),
        or_pct_narrow=_get_float(env, "OR_PCT_NARROW", 0.0036),
        or_pct_moderate=_get_float(env, "OR_PCT_MODERATE", 0.0055),
        or_pct_wide=_get_float(env, "OR_PCT_WIDE", 0.0078),
        or_straddle_very_narrow=_get_float(env, "OR_STRADDLE_VERY_NARROW", 0.24),
        or_straddle_narrow=_get_float(env, "OR_STRADDLE_NARROW", 0.42),
        or_straddle_moderate=_get_float(env, "OR_STRADDLE_MODERATE", 0.62),
        or_straddle_wide=_get_float(env, "OR_STRADDLE_WIDE", 0.86),
        spot_proximity_pct=_get_float(env, "SPOT_PROXIMITY_PCT", 0.0016),
        spot_velocity_pct=_get_float(env, "SPOT_VELOCITY_PCT", 0.0014),
        stop_efficacy=min(max(_get_float(env, "STOP_EFFICACY", 0.55), 0.0), 0.80),
        gamma_tail_prob_dte0=_get_float(env, "GAMMA_TAIL_PROB_DTE0", 0.055),
        gamma_tail_prob_dte1p=_get_float(env, "GAMMA_TAIL_PROB_DTE1P", 0.025),
        entry_slippage_mult=_get_float(env, "ENTRY_SLIPPAGE_MULT", 0.35),
        exit_slippage_mult=_get_float(env, "EXIT_SLIPPAGE_MULT", 2.25),
        spread_abs_tolerance=_get_float(env, "SPREAD_ABS_TOLERANCE", 0.85),
        max_dte_tradeable=_get_int(env, "MAX_DTE_TRADEABLE", 4),

        # v3.2 profitability calibration
        stop_mult_dte0=min(max(_get_float(env, "STOP_MULT_DTE0", 1.60), 1.15), 2.50),
        stop_mult_dte1=min(max(_get_float(env, "STOP_MULT_DTE1", 1.55), 1.15), 2.50),
        stop_mult_dte2p=min(max(_get_float(env, "STOP_MULT_DTE2P", 1.70), 1.15), 3.00),
        target_pct_dte0=min(max(_get_float(env, "TARGET_PCT_DTE0", 0.70), 0.18), 0.85),
        target_pct_dte1=min(max(_get_float(env, "TARGET_PCT_DTE1", 0.45), 0.18), 0.70),
        target_pct_dte2p=min(max(_get_float(env, "TARGET_PCT_DTE2P", 0.40), 0.18), 0.70),
        delta_close_dte0=min(max(_get_float(env, "DELTA_CLOSE_DTE0", 0.45), 0.20), 0.55),
        delta_close_dte1p=min(max(_get_float(env, "DELTA_CLOSE_DTE1P", 0.30), 0.18), 0.50),
        price_stop_wing_frac=min(max(_get_float(env, "PRICE_STOP_WING_FRAC", 0.30), 0.10), 0.80),
        price_stop_min_pts=max(_get_float(env, "PRICE_STOP_MIN_PTS", 25.0), 5.0),
        price_stop_max_frac_of_dist=min(max(_get_float(env, "PRICE_STOP_MAX_FRAC_OF_DIST", 0.40), 0.15), 0.80),
        short_delta_flat=min(max(_get_float(env, "SHORT_DELTA_FLAT", 0.32), 0.08), 0.35),
        short_delta_trend=min(max(_get_float(env, "SHORT_DELTA_TREND", 0.30), 0.07), 0.32),
        short_delta_strong=min(max(_get_float(env, "SHORT_DELTA_STRONG", 0.28), 0.06), 0.30),
        em_band_lo=min(max(_get_float(env, "EM_BAND_LO", 0.80), 0.40), 1.20),
        em_band_hi=min(max(_get_float(env, "EM_BAND_HI", 1.35), 0.90), 2.50),
        prox_gap_frac_dte0=min(max(_get_float(env, "PROX_GAP_FRAC_DTE0", 0.70), 0.50), 0.95),
        max_friction_frac_of_credit=min(max(_get_float(env, "MAX_FRICTION_FRAC_OF_CREDIT", 0.28), 0.05), 0.60),
        max_brokerage_frac_of_credit=min(max(_get_float(env, "MAX_BROKERAGE_FRAC_OF_CREDIT", 0.15), 0.02), 0.40),
        min_target_over_friction=min(max(_get_float(env, "MIN_TARGET_OVER_FRICTION", 1.25), 1.00), 3.00),
        # v3.3: DTE-0 credit/risk ladder + VIX reference for scaling
        credit_risk_ratio_dte0_early=min(max(_get_float(env, "CREDIT_RISK_RATIO_DTE0_EARLY", 0.16), 0.05), 0.40),
        credit_risk_ratio_dte0_mid=min(max(_get_float(env, "CREDIT_RISK_RATIO_DTE0_MID", 0.13), 0.05), 0.40),
        credit_risk_ratio_dte0_late=min(max(_get_float(env, "CREDIT_RISK_RATIO_DTE0_LATE", 0.10), 0.04), 0.40),
        credit_ratio_vix_ref=min(max(_get_float(env, "CREDIT_RATIO_VIX_REF", 13.5), 10.0), 20.0),
        # v3.3: EV-gate honesty bounds and minimum edge
        iv_sigma_cap_ratio=min(max(_get_float(env, "IV_SIGMA_CAP_RATIO", 1.15), 1.00), 2.00),
        atm_iv_vix_cap=min(max(_get_float(env, "ATM_IV_VIX_CAP", 1.35), 1.00), 2.50),
        min_ev_frac_of_credit=min(max(_get_float(env, "MIN_EV_FRAC_OF_CREDIT", 0.03), 0.01), 0.20),
        min_ev_frac_of_friction=min(max(_get_float(env, "MIN_EV_FRAC_OF_FRICTION", 0.35), 0.20), 1.00),
        ev_blend_model_w=min(max(_get_float(env, "EV_BLEND_MODEL_W", 0.40), 0.05), 0.90),
        ev_blend_prior_w=min(max(_get_float(env, "EV_BLEND_PRIOR_W", 0.30), 0.05), 0.90),
        ev_blend_market_w=min(max(_get_float(env, "EV_BLEND_MARKET_W", 0.30), 0.00), 0.90),
        ev_strong_sell_prior_bonus=min(max(_get_float(env, "EV_STRONG_SELL_PRIOR_BONUS", 0.05), 0.0), 0.08),
        ev_regime_align_bonus=min(max(_get_float(env, "EV_REGIME_ALIGN_BONUS", 0.05), 0.0), 0.08),
        min_lots_fraction=min(max(_get_float(env, "MIN_LOTS_FRACTION", 0.60), 0.10), 1.00),
        wing_cost_frac_max=min(max(_get_float(env, "WING_COST_FRAC_MAX", 0.50), 0.10), 0.70),
        condor_weak_side_min_frac=min(max(_get_float(env, "CONDOR_WEAK_SIDE_MIN_FRAC", 0.30), 0.05), 0.50),
        # v4.2 fresh-weekly (DTE >= 2) intraday premium selling
        short_delta_flat_weekly=min(max(_get_float(env, "SHORT_DELTA_FLAT_WEEKLY", 0.24), 0.08), 0.35),
        short_delta_trend_weekly=min(max(_get_float(env, "SHORT_DELTA_TREND_WEEKLY", 0.22), 0.07), 0.30),
        short_delta_strong_weekly=min(max(_get_float(env, "SHORT_DELTA_STRONG_WEEKLY", 0.18), 0.06), 0.25),
        em_band_lo_weekly=min(max(_get_float(env, "EM_BAND_LO_WEEKLY", 0.55), 0.30), 1.20),
        em_band_hi_weekly=min(max(_get_float(env, "EM_BAND_HI_WEEKLY", 2.10), 1.20), 3.00),
        em_band_hi_condor_weekly=min(max(_get_float(env, "EM_BAND_HI_CONDOR_WEEKLY", 1.35), 0.80), 2.00),
        wing_cost_frac_max_weekly=min(max(_get_float(env, "WING_COST_FRAC_MAX_WEEKLY", 0.58), 0.30), 0.80),
        range_adx_wide_max=min(max(_get_float(env, "RANGE_ADX_WIDE_MAX", 28.0), 20.0), 40.0),
        range_adx_wide_size=min(max(_get_float(env, "RANGE_ADX_WIDE_SIZE", 0.80), 0.40), 1.00),
        unclear_range_size_weekly=min(max(_get_float(env, "UNCLEAR_RANGE_SIZE_WEEKLY", 0.75), 0.40), 1.00),
        ev_carry_discount_dte2p=min(max(_get_float(env, "EV_CARRY_DISCOUNT_DTE2P", 0.62), 0.40), 1.00),
        adx_fast_resample=env.get("ADX_FAST_RESAMPLE", "300s").strip() or "300s",
    )


# ─────────────────────────────────────────────
# RATE LIMITER
# ─────────────────────────────────────────────

class RateLimiter:
    """
    Thread-safe rate limiter for Upstox API calls.
    Enforces per-second, per-minute, and per-30-minute limits per category.
    """

    def __init__(self, limits: dict):
        self._limits = limits
        self._calls: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _get_limit(self, category: str) -> dict:
        return self._limits.get(
            category,
            self._limits.get("default", {"per_second": 5, "per_minute": 60, "per_30min": 600})
        )

    def wait_if_needed(self, category: str) -> float:
        """
        Block until the rate limit allows another call.
        Returns total wait time in seconds.
        """
        cfg = self._get_limit(category)
        total_wait = 0.0

        while True:
            with self._lock:
                now = time.monotonic()
                if category not in self._calls:
                    self._calls[category] = []
                dq = self._calls[category]

                # Purge calls older than 30 minutes
                dq[:] = [t for t in dq if now - t <= 1800]

                in_1s  = [t for t in dq if now - t <= 1.0]
                in_60s = [t for t in dq if now - t <= 60.0]
                in_30m = dq

                sleep_needed = 0.0
                ps = cfg.get("per_second", 5)
                pm = cfg.get("per_minute", 60)
                p30 = cfg.get("per_30min", 600)

                if len(in_1s) >= ps and in_1s:
                    sleep_needed = max(sleep_needed, 1.0 - (now - min(in_1s)) + 0.01)
                if len(in_60s) >= pm and in_60s:
                    sleep_needed = max(sleep_needed, 60.0 - (now - min(in_60s)) + 0.01)
                if len(in_30m) >= p30 and in_30m:
                    sleep_needed = max(sleep_needed, 1800.0 - (now - in_30m[0]) + 0.01)

                if sleep_needed <= 0:
                    dq.append(now)
                    return total_wait

            time.sleep(min(sleep_needed, 5.0))
            total_wait += sleep_needed


# ─────────────────────────────────────────────
# DATABASE SCHEMA
# ─────────────────────────────────────────────

SCHEMA_SQL = """
-- ── Session State ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS session_state (
    trading_date                TEXT PRIMARY KEY,
    day_mode                    TEXT DEFAULT 'NORMAL',
    vix_regime                  TEXT DEFAULT 'UNKNOWN',
    day_label                   TEXT,
    or_high                     REAL,
    or_low                      REAL,
    or_width                    REAL,
    or_condition                TEXT,
    or_computed                 INTEGER DEFAULT 0,
    session_initialized         INTEGER DEFAULT 0,
    entry_start                 TEXT,
    entry_end                   TEXT,
    hard_exit_time              TEXT,
    size_multiplier             REAL DEFAULT 1.0,
    wing_width                  INTEGER DEFAULT 150,
    entry_count                 INTEGER DEFAULT 0,
    daily_halted                INTEGER DEFAULT 0,
    consecutive_stops           INTEGER DEFAULT 0,
    last_stop_time              TEXT,
    last_stop_reason            TEXT,
    last_entry_time             TEXT,
    last_stop_signal_combo      TEXT,
    actual_expiry               TEXT,
    actual_dte                  INTEGER,
    opening_iv                  REAL,
    opening_pcr                 REAL,
    opening_straddle_pts        REAL DEFAULT 0,
    current_capital             REAL,
    daily_pnl                   REAL DEFAULT 0.0,
    circuit_breaker_suspected   INTEGER DEFAULT 0,
    vix_spike_detected          INTEGER DEFAULT 0,
    event_announced             INTEGER DEFAULT 0,
    paper_trade_mode            INTEGER DEFAULT 1,
    prev_spot                   REAL,
    prev_vix                    REAL,
    prev_day_vix_close          REAL,
    parkinson_rv_pct            REAL,
    parkinson_rv_computed_date  TEXT,
    vwap_valid                  INTEGER DEFAULT 0,
    expiry_last_checked         TEXT,
    vix_regime_last_checked     TEXT,
    gap_direction               TEXT DEFAULT 'FLAT',
    gap_size_pts                REAL DEFAULT 0,
    gap_fade_opportunity        INTEGER DEFAULT 0,
    first_bar_close             REAL,
    _straddle_open_for_regime   REAL DEFAULT 0,
    _straddle_open_for_summary  REAL DEFAULT 0,
    created_at                  TEXT,
    updated_at                  TEXT
);

-- ── Positions ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    position_id             TEXT PRIMARY KEY,
    trading_date            TEXT NOT NULL,
    strategy_name           TEXT NOT NULL,
    strategy_type           TEXT NOT NULL,
    selection_reason        TEXT,
    target_expiry           TEXT,
    actual_dte              INTEGER,
    entry_time              TEXT,
    entry_spot              REAL,
    entry_vix               REAL,
    entry_vrp               REAL,
    entry_vrp_smoothed      REAL,
    entry_credit            REAL,
    gross_credit            REAL,
    opening_straddle_at_entry REAL,
    total_slippage          REAL,
    entry_costs_rupees      REAL,
    stop_premium            REAL,
    target_premium          REAL,
    price_stop_pts          INTEGER,
    price_stop_level_call   REAL,
    price_stop_level_put    REAL,
    hard_exit_time          TEXT,
    final_lots              INTEGER,
    max_loss_per_lot        REAL,
    total_max_risk          REAL,
    estimated_margin        REAL,
    status                  TEXT DEFAULT 'OPEN',
    exit_time               TEXT,
    exit_reason             TEXT,
    exit_priority           INTEGER,
    exit_premium            REAL,
    gross_pnl_rupees        REAL,
    exit_costs_rupees       REAL,
    net_pnl_rupees          REAL,
    last_known_premium      REAL,
    profit_lock_activated   INTEGER DEFAULT 0,
    profit_lock_stop_level  REAL,
    paper_trade             INTEGER DEFAULT 1,
    raw_params_json         TEXT,
    vol_regime_at_entry     TEXT,
    price_regime_at_entry   TEXT,
    positioning_at_entry    TEXT,
    confidence_at_entry     TEXT,
    confidence_score_at_entry REAL,
    final_regime_at_entry   TEXT,
    event_day               INTEGER DEFAULT 0,
    event_name              TEXT DEFAULT '',
    defined_risk_only       INTEGER DEFAULT 0,
    is_borderline_sell      INTEGER DEFAULT 0,
    created_at              TEXT,
    updated_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_date   ON positions(trading_date);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

-- ── Position Legs ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS position_legs (
    leg_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id             TEXT NOT NULL,
    strike                  REAL NOT NULL,
    option_type             TEXT NOT NULL,
    action                  TEXT NOT NULL,
    qty                     INTEGER NOT NULL,
    entry_price             REAL,
    exit_price              REAL,
    entry_bid               REAL DEFAULT 0,
    entry_ask               REAL DEFAULT 0,
    entry_delta             REAL DEFAULT 0,
    entry_gamma             REAL DEFAULT 0,
    entry_vega              REAL DEFAULT 0,
    entry_theta             REAL DEFAULT 0,
    entry_iv                REAL DEFAULT 0,
    entry_oi                INTEGER DEFAULT 0,
    exit_delta              REAL,
    broker_order_id_entry   TEXT,
    broker_order_id_exit    TEXT,
    quoted_mid_at_entry     REAL,
    quoted_mid_at_exit      REAL,
    leg_status              TEXT DEFAULT 'OPEN',
    FOREIGN KEY (position_id) REFERENCES positions(position_id)
);

CREATE INDEX IF NOT EXISTS idx_legs_position ON position_legs(position_id);

-- ── Intraday Candles ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS intraday_candles (
    candle_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date    TEXT NOT NULL,
    candle_time     TEXT NOT NULL,
    interval_min    INTEGER DEFAULT 1,
    open            REAL NOT NULL,
    high            REAL NOT NULL,
    low             REAL NOT NULL,
    close           REAL NOT NULL,
    volume          INTEGER DEFAULT 0,
    source          TEXT DEFAULT 'upstox_intraday'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candles_unique
    ON intraday_candles(trading_date, candle_time, interval_min);
CREATE INDEX IF NOT EXISTS idx_candles_date
    ON intraday_candles(trading_date);

-- ── Option Chain Snapshot ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS option_chain_snapshot (
    snapshot_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_time            TEXT NOT NULL,
    trading_date            TEXT NOT NULL,
    expiry                  TEXT NOT NULL,
    strike                  REAL NOT NULL,
    option_type             TEXT NOT NULL,
    bid                     REAL DEFAULT 0,
    ask                     REAL DEFAULT 0,
    ltp                     REAL DEFAULT 0,
    oi                      INTEGER DEFAULT 0,
    volume                  INTEGER DEFAULT 0,
    iv                      REAL DEFAULT 0,
    delta                   REAL DEFAULT 0,
    gamma                   REAL DEFAULT 0,
    theta                   REAL DEFAULT 0,
    vega                    REAL DEFAULT 0,
    data_timestamp          TEXT,
    cycle_id                INTEGER,
    spot_at_capture         REAL,
    vix_at_capture          REAL,
    vrp_at_capture          REAL,
    vol_regime_at_capture   TEXT,
    price_regime_at_capture TEXT,
    final_regime_at_capture TEXT
);

CREATE INDEX IF NOT EXISTS idx_chain_date
    ON option_chain_snapshot(trading_date);
CREATE INDEX IF NOT EXISTS idx_chain_expiry
    ON option_chain_snapshot(expiry, strike);

-- ── ATM Options Chain (condensed) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS options_chain (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    date            TEXT NOT NULL,
    time            TEXT NOT NULL,
    expiry_date     TEXT,
    strike          INTEGER,
    ce_ltp          REAL DEFAULT 0,
    ce_iv           REAL DEFAULT 0,
    ce_oi           INTEGER DEFAULT 0,
    ce_volume       INTEGER DEFAULT 0,
    pe_ltp          REAL DEFAULT 0,
    pe_iv           REAL DEFAULT 0,
    pe_oi           INTEGER DEFAULT 0,
    pe_volume       INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_oc_ts     ON options_chain(timestamp);
CREATE INDEX IF NOT EXISTS idx_oc_strike ON options_chain(strike, expiry_date, timestamp);

-- ── Cycle Log ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cycle_log (
    cycle_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_time          TEXT NOT NULL,
    trading_date        TEXT NOT NULL,
    spot                REAL,
    vix                 REAL,
    vrp_raw             REAL,
    vrp_smoothed        REAL,
    atm_iv_pct          REAL,
    parkinson_rv_pct    REAL,
    adx_15              REAL,
    adx_60              REAL,
    adx_condition       TEXT,
    ema_structure       TEXT,
    hh_hl               TEXT,
    vwap                REAL,
    vwap_dist_pct       REAL,
    pcr                 REAL,
    pcr_change          REAL,
    skew_ratio          REAL,
    skew_otm            REAL,
    or_width            REAL,
    or_condition        TEXT,
    iv_behavior         TEXT,
    iv_change_pct_from_open REAL,
    day_move_used_pct   REAL,
    opening_straddle_pts REAL,
    choppy_detected     INTEGER DEFAULT 0,
    gap_fade_opportunity INTEGER DEFAULT 0,
    vol_regime          TEXT,
    price_regime        TEXT,
    positioning_regime  TEXT,
    confidence_level    TEXT,
    confidence_score    REAL,
    final_regime        TEXT,
    final_regime_notes  TEXT,
    size_multiplier     REAL,
    block_new_entries   INTEGER DEFAULT 0,
    action_taken        TEXT,
    no_trade_reason     TEXT,
    open_positions      INTEGER DEFAULT 0,
    daily_pnl_net       REAL DEFAULT 0,
    vix_regime          TEXT,
    day_mode            TEXT,
    atm_straddle_price  REAL,
    max_pain            REAL,
    chain_stale         INTEGER DEFAULT 0,
    oi_change_pct       REAL,
    resistance_strength REAL,
    support_strength    REAL,
    raw_json            TEXT
);

CREATE INDEX IF NOT EXISTS idx_cycle_time ON cycle_log(cycle_time);
CREATE INDEX IF NOT EXISTS idx_cycle_date ON cycle_log(trading_date);

-- ── Trade Entries ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_entries (
    trade_id                    TEXT PRIMARY KEY,
    position_id                 TEXT NOT NULL,
    strategy_name               TEXT NOT NULL,
    entry_time                  TEXT NOT NULL,
    trading_date                TEXT NOT NULL,
    day_label                   TEXT,
    entry_spot                  REAL,
    entry_vix                   REAL,
    entry_vrp_raw               REAL,
    entry_vrp_smoothed          REAL,
    entry_atm_iv                REAL,
    entry_parkinson_rv          REAL,
    entry_adx_15                REAL,
    entry_vwap                  REAL,
    entry_vwap_dist_pct         REAL,
    entry_pcr                   REAL,
    entry_skew_ratio            REAL,
    or_width                    REAL,
    or_condition                TEXT,
    iv_behavior                 TEXT,
    iv_change_pct_from_open     REAL,
    day_move_used_at_entry      REAL,
    opening_straddle_at_entry   REAL,
    vol_regime_at_entry         TEXT,
    price_regime_at_entry       TEXT,
    positioning_at_entry        TEXT,
    confidence_level_at_entry   TEXT,
    confidence_score_at_entry   REAL,
    final_regime_at_entry       TEXT,
    target_expiry               TEXT,
    actual_dte                  INTEGER,
    legs_json                   TEXT,
    entry_credit                REAL,
    gross_credit                REAL,
    total_slippage              REAL,
    entry_costs_pts             REAL,
    entry_costs_rupees          REAL,
    stop_premium                REAL,
    target_premium              REAL,
    price_stop_pts              INTEGER,
    hard_exit_time              TEXT,
    final_lots                  INTEGER,
    max_loss_per_lot            REAL,
    total_max_risk              REAL,
    capital_at_entry            REAL,
    daily_pnl_at_entry          REAL,
    paper_trade                 INTEGER DEFAULT 1,
    selection_reason            TEXT,
    is_borderline_sell          INTEGER DEFAULT 0,
    event_day                   INTEGER DEFAULT 0,
    event_name                  TEXT DEFAULT '',
    calibration_tier_at_entry   INTEGER DEFAULT 0,
    created_at                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_te_date     ON trade_entries(trading_date);
CREATE INDEX IF NOT EXISTS idx_te_position ON trade_entries(position_id);

-- ── Trade Exits ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_exits (
    exit_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                TEXT NOT NULL,
    position_id             TEXT NOT NULL,
    strategy_name           TEXT,
    exit_time               TEXT NOT NULL,
    hold_minutes            REAL,
    exit_reason             TEXT,
    exit_priority           INTEGER,
    exit_priority_name      TEXT,
    exit_spot               REAL,
    exit_vix                REAL,
    exit_legs_json          TEXT,
    exit_premium            REAL,
    gross_pnl_pts           REAL,
    gross_pnl_rupees        REAL,
    exit_slippage           REAL,
    exit_costs_pts          REAL,
    exit_costs_rupees       REAL,
    total_costs_rupees      REAL,
    net_pnl_pts             REAL,
    net_pnl_rupees          REAL,
    net_pnl_pct             REAL,
    result                  TEXT,
    profit_pct_of_credit    REAL,
    pnl_15min_after_exit    REAL,
    created_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_tx_date     ON trade_exits(trade_id);
CREATE INDEX IF NOT EXISTS idx_tx_position ON trade_exits(position_id);

-- ── Daily Summary ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_summary (
    trading_date            TEXT PRIMARY KEY,
    day_label               TEXT,
    trades_attempted        INTEGER DEFAULT 0,
    trades_executed         INTEGER DEFAULT 0,
    trades_won              INTEGER DEFAULT 0,
    trades_lost             INTEGER DEFAULT 0,
    win_rate_pct            REAL DEFAULT 0,
    gross_pnl_rupees        REAL DEFAULT 0,
    total_costs_rupees      REAL DEFAULT 0,
    net_pnl_rupees          REAL DEFAULT 0,
    net_pnl_pct_capital     REAL DEFAULT 0,
    max_intraday_drawdown   REAL DEFAULT 0,
    max_concurrent_positions INTEGER DEFAULT 0,
    stops_fired             INTEGER DEFAULT 0,
    daily_halt_triggered    INTEGER DEFAULT 0,
    vix_open                REAL,
    vix_close               REAL,
    vix_low                 REAL,
    vix_high                REAL,
    nifty_open              REAL,
    nifty_close             REAL,
    nifty_low               REAL,
    nifty_high              REAL,
    or_width                REAL,
    or_condition            TEXT,
    vrp_mean                REAL,
    vrp_smoothed_mean       REAL,
    dominant_vol_regime     TEXT,
    dominant_price_regime   TEXT,
    dominant_final_regime   TEXT,
    strategies_used_json    TEXT,
    no_trade_reasons_json   TEXT,
    avg_hold_minutes        REAL DEFAULT 0,
    avg_credit_pts          REAL DEFAULT 0,
    avg_vrp_at_entry        REAL DEFAULT 0,
    profit_factor           REAL,
    capital_start           REAL,
    capital_end             REAL,
    capital_change_pct      REAL DEFAULT 0,
    event_day               INTEGER DEFAULT 0,
    event_name              TEXT DEFAULT '',
    opening_spot            REAL,
    closing_spot            REAL,
    day_range_points        REAL DEFAULT 0,
    day_range_pct           REAL DEFAULT 0,
    opening_straddle        REAL DEFAULT 0,
    realized_move           REAL DEFAULT 0,
    straddle_ratio          REAL DEFAULT 0,
    phantom_trades_blocked  INTEGER DEFAULT 0,
    phantom_would_have_won  INTEGER DEFAULT 0,
    regime_accuracy_score   REAL,
    created_at              TEXT
);

-- ── VIX History ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vix_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    date        TEXT NOT NULL,
    time        TEXT NOT NULL,
    weekday     INTEGER,
    vix_value   REAL NOT NULL,
    dte         INTEGER,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_vh_ts   ON vix_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_vh_date ON vix_history(date);

-- ── Market Snapshots ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL,
    date                TEXT NOT NULL,
    time                TEXT NOT NULL,
    spot                REAL,
    vix                 REAL,
    atm_iv              REAL,
    parkinson_rv        REAL,
    vrp_raw             REAL,
    vrp_smoothed        REAL,
    skew_ratio          REAL,
    skew_otm            REAL,
    oi_change_pct       REAL,
    resistance_oi       INTEGER DEFAULT 0,
    support_oi          INTEGER DEFAULT 0,
    total_ce_oi         INTEGER DEFAULT 0,
    total_pe_oi         INTEGER DEFAULT 0,
    pcr                 REAL,
    adx_15              REAL,
    vwap_dist_pct       REAL,
    iv_behavior         TEXT,
    day_move_used_pct   REAL,
    vol_regime          TEXT,
    price_regime        TEXT,
    positioning_regime  TEXT,
    final_regime        TEXT,
    confidence_level    TEXT,
    created_at          TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_ms_ts   ON market_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_ms_date ON market_snapshots(date);

-- ── Regime Decisions ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS regime_decisions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp               TEXT NOT NULL,
    date                    TEXT NOT NULL,
    time                    TEXT NOT NULL,
    weekday                 INTEGER,
    dte                     INTEGER,
    day_type                TEXT,
    event_day               INTEGER DEFAULT 0,
    event_name              TEXT DEFAULT '',
    defined_risk_only       INTEGER DEFAULT 0,
    vol_regime              TEXT,
    price_regime            TEXT,
    positioning_regime      TEXT,
    final_regime            TEXT,
    confidence_level        TEXT,
    confidence_score        REAL,
    size_multiplier         REAL,
    block_new_entries       INTEGER DEFAULT 0,
    vix_level               REAL,
    vrp_raw                 REAL,
    vrp_smoothed            REAL,
    atm_iv_pct              REAL,
    parkinson_rv_pct        REAL,
    adx_15                  REAL,
    adx_60                  REAL,
    ema_structure           TEXT,
    pcr                     REAL,
    skew_ratio              REAL,
    oi_change_pct           REAL,
    oi_wall_strength        REAL,
    max_pain_distance       REAL,
    day_move_used_pct       REAL,
    opening_straddle_pts    REAL,
    gap_fade_opportunity    INTEGER DEFAULT 0,
    borderline_sell         INTEGER DEFAULT 0,
    notes                   TEXT,
    is_calibrated           INTEGER DEFAULT 0,
    calibration_tier        INTEGER DEFAULT 0,
    created_at              TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_rd_ts   ON regime_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_rd_date ON regime_decisions(date);

-- ── Calibration State ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS calibration_state (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    calibrated_at               TEXT NOT NULL,
    n_trading_days              INTEGER DEFAULT 0,
    n_tuesday_expiries          INTEGER DEFAULT 0,
    calibration_tier            INTEGER DEFAULT 0,
    is_valid                    INTEGER DEFAULT 0,
    notes                       TEXT,
    -- VIX percentiles
    vix_p25                     REAL,
    vix_p50                     REAL,
    vix_p75                     REAL,
    vix_p90                     REAL,
    -- VRP thresholds
    vrp_sell_threshold          REAL DEFAULT 2.5,
    vrp_fair_threshold          REAL DEFAULT 1.5,
    -- Day size multipliers
    day_size_monday             REAL DEFAULT 0.55,
    day_size_tuesday            REAL DEFAULT 0.80,
    day_size_wednesday          REAL DEFAULT 0.70,
    day_size_thursday           REAL DEFAULT 0.70,
    day_size_friday             REAL DEFAULT 0.60,
    -- OI thresholds
    oi_buildup_threshold        REAL DEFAULT 0.08,
    oi_unwind_threshold         REAL DEFAULT -0.08,
    oi_wall_strong_cal          REAL DEFAULT 2.5,
    oi_wall_moderate_cal        REAL DEFAULT 1.7,
    -- PCR thresholds
    pcr_bullish_threshold       REAL DEFAULT 0.72,
    pcr_bearish_threshold       REAL DEFAULT 1.28,
    -- Skew thresholds
    skew_bearish_threshold      REAL DEFAULT 3.0,
    skew_bullish_threshold      REAL DEFAULT 0.95,
    -- Straddle ratio
    straddle_ratio_sell         REAL DEFAULT 1.10,
    -- Signal weights
    signal_weight_vrp           REAL DEFAULT 1.0,
    signal_weight_price         REAL DEFAULT 1.0,
    signal_weight_positioning   REAL DEFAULT 1.0,
    signal_weight_iv_behavior   REAL DEFAULT 1.0,
    signal_weight_or_condition  REAL DEFAULT 1.0,
    -- Performance metrics
    phantom_false_negative_rate REAL,
    exit_quality_score          REAL,
    regime_accuracy_score       REAL,
    -- Ranges
    tuesday_avg_range           REAL DEFAULT 150,
    monday_avg_range            REAL DEFAULT 150,
    wednesday_avg_range         REAL DEFAULT 150,
    thursday_avg_range          REAL DEFAULT 150,
    friday_avg_range            REAL DEFAULT 150,
    created_at                  TEXT DEFAULT (datetime('now','localtime'))
);

-- ── Strategy Decisions ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS strategy_decisions (
    decision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_time   TEXT NOT NULL,
    trading_date    TEXT NOT NULL,
    action          TEXT NOT NULL,
    strategy_name   TEXT,
    reason          TEXT,
    params_json     TEXT,
    signals_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sd_time ON strategy_decisions(decision_time);
CREATE INDEX IF NOT EXISTS idx_sd_date ON strategy_decisions(trading_date);

-- ── Phantom Trades ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS phantom_trades (
    phantom_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date            TEXT NOT NULL,
    block_time              TEXT NOT NULL,
    block_reason            TEXT,
    strategy_would_be       TEXT,
    strikes_json            TEXT,
    credit_would_be         REAL,
    vrp_at_block            REAL,
    vrp_smoothed_at_block   REAL,
    or_condition            TEXT,
    adx_at_block            REAL,
    positioning_at_block    TEXT,
    dte_at_block            INTEGER,
    simulated_pnl_final     REAL,
    simulated_result        TEXT,
    would_have_been_profitable INTEGER DEFAULT 0,
    created_at              TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_pt_date ON phantom_trades(trading_date);

-- ── Regime Accuracy Scores ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS regime_accuracy_scores (
    score_id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date                TEXT NOT NULL,
    regime_decision_id          INTEGER,
    vol_regime_classified       TEXT,
    price_regime_classified     TEXT,
    positioning_classified      TEXT,
    final_regime_classified     TEXT,
    was_vol_correct             INTEGER,
    was_price_correct           INTEGER,
    was_positioning_correct     INTEGER,
    was_final_correct           INTEGER,
    nifty_move_2hr_pts          REAL,
    straddle_move_2hr_pts       REAL,
    score_value                 REAL,
    created_at                  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_ras_date ON regime_accuracy_scores(trading_date);

-- ── Exit Quality Log ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS exit_quality_log (
    quality_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id             TEXT NOT NULL,
    trading_date            TEXT NOT NULL,
    exit_priority_fired     INTEGER,
    exit_time               TEXT,
    exit_pnl_rupees         REAL,
    pnl_15min_after_exit    REAL,
    pnl_30min_after_exit    REAL,
    pnl_at_hard_exit        REAL,
    was_exit_premature      INTEGER DEFAULT 0,
    was_exit_late           INTEGER DEFAULT 0,
    optimal_exit_pnl        REAL,
    created_at              TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_eql_date ON exit_quality_log(trading_date);

-- ── API Call Log ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_call_log (
    call_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    call_time           TEXT NOT NULL,
    category            TEXT,
    endpoint            TEXT,
    method              TEXT,
    status_code         INTEGER,
    response_time_ms    REAL,
    rate_limited        INTEGER DEFAULT 0,
    error_message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_acl_time ON api_call_log(call_time);

-- ── Audit Log ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    log_time        TEXT NOT NULL,
    level           TEXT NOT NULL,
    logger_name     TEXT,
    message         TEXT,
    context_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_al_time  ON audit_log(log_time);
CREATE INDEX IF NOT EXISTS idx_al_level ON audit_log(level);

-- ── Expiry Results ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS expiry_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    expiry_date         TEXT NOT NULL UNIQUE,
    opening_spot        REAL,
    closing_spot        REAL,
    max_pain_at_open    INTEGER,
    final_settlement    REAL,
    day_range_points    REAL,
    opening_straddle    REAL,
    realized_move       REAL,
    vix_open            REAL,
    regime_at_open      TEXT,
    created_at          TEXT DEFAULT (datetime('now','localtime'))
);
"""

# ─────────────────────────────────────────────
# DATABASE MIGRATIONS
# ─────────────────────────────────────────────

MIGRATION_SQL: List[str] = [
    # Add new columns to existing tables if upgrading from old engine
    "ALTER TABLE cycle_log ADD COLUMN vrp_raw REAL",
    "ALTER TABLE cycle_log ADD COLUMN vrp_smoothed REAL",
    "ALTER TABLE cycle_log ADD COLUMN vol_regime TEXT",
    "ALTER TABLE cycle_log ADD COLUMN price_regime TEXT",
    "ALTER TABLE cycle_log ADD COLUMN positioning_regime TEXT",
    "ALTER TABLE cycle_log ADD COLUMN confidence_level TEXT",
    "ALTER TABLE cycle_log ADD COLUMN confidence_score REAL",
    "ALTER TABLE cycle_log ADD COLUMN block_new_entries INTEGER DEFAULT 0",
    "ALTER TABLE cycle_log ADD COLUMN iv_change_pct_from_open REAL",
    "ALTER TABLE cycle_log ADD COLUMN day_move_used_pct REAL",
    "ALTER TABLE cycle_log ADD COLUMN opening_straddle_pts REAL",
    "ALTER TABLE cycle_log ADD COLUMN choppy_detected INTEGER DEFAULT 0",
    "ALTER TABLE cycle_log ADD COLUMN gap_fade_opportunity INTEGER DEFAULT 0",
    "ALTER TABLE cycle_log ADD COLUMN hh_hl TEXT",
    "ALTER TABLE cycle_log ADD COLUMN resistance_strength REAL",
    "ALTER TABLE cycle_log ADD COLUMN support_strength REAL",
    "ALTER TABLE cycle_log ADD COLUMN final_regime_notes TEXT",
    "ALTER TABLE positions ADD COLUMN vol_regime_at_entry TEXT",
    "ALTER TABLE positions ADD COLUMN price_regime_at_entry TEXT",
    "ALTER TABLE positions ADD COLUMN positioning_at_entry TEXT",
    "ALTER TABLE positions ADD COLUMN confidence_score_at_entry REAL",
    "ALTER TABLE positions ADD COLUMN entry_vrp_smoothed REAL",
    "ALTER TABLE positions ADD COLUMN opening_straddle_at_entry REAL",
    "ALTER TABLE positions ADD COLUMN profit_lock_activated INTEGER DEFAULT 0",
    "ALTER TABLE positions ADD COLUMN profit_lock_stop_level REAL",
    "ALTER TABLE positions ADD COLUMN exit_priority INTEGER",
    "ALTER TABLE positions ADD COLUMN price_stop_level_call REAL",
    "ALTER TABLE positions ADD COLUMN price_stop_level_put REAL",
    "ALTER TABLE positions ADD COLUMN is_borderline_sell INTEGER DEFAULT 0",
    "ALTER TABLE trade_entries ADD COLUMN vol_regime_at_entry TEXT",
    "ALTER TABLE trade_entries ADD COLUMN price_regime_at_entry TEXT",
    "ALTER TABLE trade_entries ADD COLUMN positioning_at_entry TEXT",
    "ALTER TABLE trade_entries ADD COLUMN confidence_level_at_entry TEXT",
    "ALTER TABLE trade_entries ADD COLUMN confidence_score_at_entry REAL",
    "ALTER TABLE trade_entries ADD COLUMN entry_vrp_raw REAL",
    "ALTER TABLE trade_entries ADD COLUMN entry_vrp_smoothed REAL",
    "ALTER TABLE trade_entries ADD COLUMN day_move_used_at_entry REAL",
    "ALTER TABLE trade_entries ADD COLUMN opening_straddle_at_entry REAL",
    "ALTER TABLE trade_entries ADD COLUMN iv_change_pct_from_open REAL",
    "ALTER TABLE trade_entries ADD COLUMN is_borderline_sell INTEGER DEFAULT 0",
    "ALTER TABLE trade_entries ADD COLUMN calibration_tier_at_entry INTEGER DEFAULT 0",
    "ALTER TABLE trade_exits ADD COLUMN exit_priority INTEGER",
    "ALTER TABLE trade_exits ADD COLUMN exit_priority_name TEXT",
    "ALTER TABLE trade_exits ADD COLUMN pnl_15min_after_exit REAL",
    "ALTER TABLE session_state ADD COLUMN opening_straddle_pts REAL DEFAULT 0",
    "ALTER TABLE session_state ADD COLUMN prev_day_vix_close REAL",
    "ALTER TABLE session_state ADD COLUMN gap_direction TEXT DEFAULT 'FLAT'",
    "ALTER TABLE session_state ADD COLUMN gap_size_pts REAL DEFAULT 0",
    "ALTER TABLE session_state ADD COLUMN gap_fade_opportunity INTEGER DEFAULT 0",
    "ALTER TABLE session_state ADD COLUMN first_bar_close REAL",
    "ALTER TABLE session_state ADD COLUMN last_stop_signal_combo TEXT",
    "ALTER TABLE daily_summary ADD COLUMN dominant_vol_regime TEXT",
    "ALTER TABLE daily_summary ADD COLUMN dominant_price_regime TEXT",
    "ALTER TABLE daily_summary ADD COLUMN dominant_final_regime TEXT",
    "ALTER TABLE daily_summary ADD COLUMN vrp_smoothed_mean REAL",
    "ALTER TABLE daily_summary ADD COLUMN phantom_trades_blocked INTEGER DEFAULT 0",
    "ALTER TABLE daily_summary ADD COLUMN phantom_would_have_won INTEGER DEFAULT 0",
    "ALTER TABLE daily_summary ADD COLUMN regime_accuracy_score REAL",
    "ALTER TABLE calibration_state ADD COLUMN signal_weight_vrp REAL DEFAULT 1.0",
    "ALTER TABLE calibration_state ADD COLUMN signal_weight_price REAL DEFAULT 1.0",
    "ALTER TABLE calibration_state ADD COLUMN signal_weight_positioning REAL DEFAULT 1.0",
    "ALTER TABLE calibration_state ADD COLUMN signal_weight_iv_behavior REAL DEFAULT 1.0",
    "ALTER TABLE calibration_state ADD COLUMN signal_weight_or_condition REAL DEFAULT 1.0",
    "ALTER TABLE calibration_state ADD COLUMN phantom_false_negative_rate REAL",
    "ALTER TABLE calibration_state ADD COLUMN exit_quality_score REAL",
    "ALTER TABLE calibration_state ADD COLUMN regime_accuracy_score REAL",
    "ALTER TABLE calibration_state ADD COLUMN pcr_bullish_threshold REAL DEFAULT 0.72",
    "ALTER TABLE calibration_state ADD COLUMN pcr_bearish_threshold REAL DEFAULT 1.28",
    "ALTER TABLE market_snapshots ADD COLUMN vrp_raw REAL",
    "ALTER TABLE market_snapshots ADD COLUMN vrp_smoothed REAL",
    "ALTER TABLE market_snapshots ADD COLUMN vol_regime TEXT",
    "ALTER TABLE market_snapshots ADD COLUMN price_regime TEXT",
    "ALTER TABLE market_snapshots ADD COLUMN positioning_regime TEXT",
    "ALTER TABLE market_snapshots ADD COLUMN final_regime TEXT",
    "ALTER TABLE market_snapshots ADD COLUMN confidence_level TEXT",
    "ALTER TABLE market_snapshots ADD COLUMN iv_behavior TEXT",
    "ALTER TABLE market_snapshots ADD COLUMN day_move_used_pct REAL",
    "ALTER TABLE market_snapshots ADD COLUMN skew_otm REAL",
]


# ─────────────────────────────────────────────
# DATABASE CLASS
# ─────────────────────────────────────────────

class Database:
    """
    Thread-safe SQLite database wrapper.
    Uses WAL mode for concurrent read/write.
    Auto-initialises schema and runs migrations on startup.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.execute("PRAGMA cache_size=-8000;")  # 8MB cache
        self._init_schema()
        self._run_migrations()

    # ── Schema & Migrations ───────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()

    def _run_migrations(self) -> None:
        """Run ALTER TABLE migrations safely, skipping already-applied ones."""
        with self._lock:
            existing_tables = {
                row[0] for row in
                self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for sql in MIGRATION_SQL:
                try:
                    # Parse table name and column name from ALTER TABLE statement
                    parts = sql.upper().split()
                    if len(parts) < 6:
                        continue
                    table_name = sql.split("ALTER TABLE")[1].split("ADD COLUMN")[0].strip()
                    col_name = sql.split("ADD COLUMN")[1].strip().split()[0]
                    if table_name not in existing_tables:
                        continue
                    existing_cols = {
                        row[1] for row in
                        self._conn.execute(
                            f"PRAGMA table_info({table_name})"
                        ).fetchall()
                    }
                    if col_name not in existing_cols:
                        self._conn.execute(sql)
                except Exception:
                    pass
            self._conn.commit()

    # ── Core Query Methods ────────────────────────────────────────────────

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a single SQL statement with commit."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, param_list: list) -> sqlite3.Cursor:
        """Execute a batch SQL statement with commit."""
        with self._lock:
            cur = self._conn.executemany(sql, param_list)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> List[dict]:
        """Return all rows as list of dicts."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """Return first row as dict, or None."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def insert(self, table: str, data: dict) -> int:
        """Insert a row and return the new rowid."""
        if not data:
            return 0
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        with self._lock:
            cur = self._conn.execute(sql, tuple(data.values()))
            self._conn.commit()
            return cur.lastrowid

    def update(self, table: str, data: dict, where: dict) -> int:
        """Update rows matching where clause. Returns number of rows affected."""
        if not data or not where:
            return 0
        set_clause   = ", ".join(f"{k}=?" for k in data)
        where_clause = " AND ".join(f"{k}=?" for k in where)
        sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
        params = tuple(data.values()) + tuple(where.values())
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def upsert(self, table: str, key: dict, data: dict) -> int:
        """Insert or update a row identified by key."""
        where_clause = " AND ".join(f"{k}=?" for k in key)
        existing = self.query_one(
            f"SELECT 1 FROM {table} WHERE {where_clause}",
            tuple(key.values())
        )
        if existing:
            return self.update(table, data, key)
        return self.insert(table, {**key, **data})

    # ── Schema Helpers ────────────────────────────────────────────────────

    def table_exists(self, name: str) -> bool:
        row = self.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,)
        )
        return row is not None

    def column_exists(self, table: str, column: str) -> bool:
        cols = self.query(f"PRAGMA table_info({table})")
        return any(c["name"] == column for c in cols)

    def ensure_column(self, table: str, column: str, coltype: str) -> None:
        """Add column to table if it does not already exist."""
        if self.table_exists(table) and not self.column_exists(table, column):
            try:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            except Exception:
                pass

    def get_connection(self) -> sqlite3.Connection:
        """Return raw connection for pandas read_sql_query."""
        return self._conn

    # ── Logging Helpers ───────────────────────────────────────────────────

    def log_audit(
        self, level: str, logger_name: str,
        message: str, context: Optional[dict] = None
    ) -> None:
        """Write to audit_log table. Never raises."""
        try:
            self.insert("audit_log", {
                "log_time":     now_ist().isoformat(),
                "level":        level,
                "logger_name":  logger_name,
                "message":      message[:2000],
                "context_json": json.dumps(context, default=str) if context else None,
            })
        except Exception:
            pass

    def log_api_call(
        self, category: str, endpoint: str, method: str,
        status_code: Optional[int], response_time_ms: float,
        rate_limited: bool = False,
        error_message: Optional[str] = None
    ) -> None:
        """Write to api_call_log table. Never raises."""
        try:
            self.insert("api_call_log", {
                "call_time":        now_ist().isoformat(),
                "category":         category,
                "endpoint":         endpoint,
                "method":           method,
                "status_code":      status_code,
                "response_time_ms": response_time_ms,
                "rate_limited":     1 if rate_limited else 0,
                "error_message":    error_message,
            })
        except Exception:
            pass

    # ── Calibration Helpers ───────────────────────────────────────────────

    def get_latest_calibration(self) -> Optional[dict]:
        """Return the most recent valid calibration row."""
        return self.query_one(
            "SELECT * FROM calibration_state "
            "WHERE is_valid=1 ORDER BY calibrated_at DESC LIMIT 1"
        )

    def count_tuesday_expiries(self) -> int:
        row = self.query_one(
            "SELECT COUNT(*) as cnt FROM daily_summary WHERE day_label='TUESDAY'"
        )
        return row["cnt"] if row else 0

    def count_trading_days(self) -> int:
        row = self.query_one(
            "SELECT COUNT(DISTINCT trading_date) as cnt FROM daily_summary"
        )
        return row["cnt"] if row else 0

    # ── Historical Data Helpers ───────────────────────────────────────────

    def get_vix_history(
        self, days: int = 365, from_date: Optional[str] = None
    ):
        """Return VIX history as pandas DataFrame."""
        try:
            import pandas as pd
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            if from_date and from_date > cutoff:
                cutoff = from_date
            rows = self.query(
                "SELECT * FROM vix_history WHERE date >= ? ORDER BY timestamp",
                (cutoff,),
            )
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception:
            try:
                import pandas as pd
                return pd.DataFrame()
            except Exception:
                return []

    def get_daily_summary(self, days: int = 365):
        """Return daily_summary as pandas DataFrame with weekday column."""
        try:
            import pandas as pd
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            rows = self.query(
                "SELECT *, CAST(strftime('%w', trading_date) AS INTEGER) as weekday_sql "
                "FROM daily_summary WHERE trading_date >= ? ORDER BY trading_date",
                (cutoff,),
            )
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            # SQLite strftime %w: 0=Sunday, 1=Monday, ..., 6=Saturday
            # Python weekday: 0=Monday, ..., 6=Sunday
            # Convert: Python weekday = (SQLite %w - 1) % 7
            df["weekday_sql"] = pd.to_numeric(df["weekday_sql"], errors="coerce")
            df["weekday"] = df["weekday_sql"].apply(
                lambda w: int((w - 1) % 7) if pd.notna(w) else float("nan")
            )
            return df
        except Exception:
            try:
                import pandas as pd
                return pd.DataFrame()
            except Exception:
                return []

    def get_spot_history(self, days: int = 30):
        """Return intraday candles as pandas DataFrame."""
        try:
            import pandas as pd
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            rows = self.query(
                "SELECT trading_date as date, candle_time as time, "
                "open, high, low, close, volume "
                "FROM intraday_candles WHERE trading_date >= ? "
                "ORDER BY trading_date, candle_time",
                (cutoff,),
            )
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception:
            try:
                import pandas as pd
                return pd.DataFrame()
            except Exception:
                return []

    def get_market_snapshots(self, days: int = 365):
        """Return market_snapshots as pandas DataFrame."""
        try:
            import pandas as pd
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            rows = self.query(
                "SELECT * FROM market_snapshots WHERE date >= ? ORDER BY timestamp",
                (cutoff,),
            )
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception:
            try:
                import pandas as pd
                return pd.DataFrame()
            except Exception:
                return []

    def get_vrp_win_rates(self, days: int = 60) -> List[dict]:
        """
        Return win rate by VRP bucket for calibration.
        Buckets are 0.5pp wide (0.0-0.5, 0.5-1.0, ..., 5.0+).
        """
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        return self.query(
            """
            SELECT
                ROUND(te.entry_vrp_smoothed * 2) / 2.0 AS vrp_bucket,
                COUNT(*) AS n_trades,
                SUM(CASE WHEN tx.result = 'WIN' THEN 1 ELSE 0 END) AS n_wins,
                AVG(tx.net_pnl_rupees) AS avg_pnl,
                SUM(tx.total_costs_rupees) / COUNT(*) AS avg_costs
            FROM trade_entries te
            JOIN trade_exits tx ON te.position_id = tx.position_id
            WHERE te.trading_date >= ?
              AND te.entry_vrp_smoothed IS NOT NULL
              AND te.entry_vrp_smoothed > 0
            GROUP BY vrp_bucket
            ORDER BY vrp_bucket
            """,
            (cutoff,),
        )

    def get_phantom_false_negative_rate(self, days: int = 20) -> float:
        """
        Return percentage of NEUTRAL-blocked phantom trades that would have been profitable.
        Used to determine if NEUTRAL threshold is too tight.
        """
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        row = self.query_one(
            """
            SELECT
                COUNT(*) AS total,
                SUM(would_have_been_profitable) AS would_have_won
            FROM phantom_trades
            WHERE trading_date >= ?
            """,
            (cutoff,),
        )
        if not row or not row.get("total") or row["total"] == 0:
            return 0.0
        return round(row["would_have_won"] / row["total"] * 100, 1)

    def get_exit_quality_summary(self, days: int = 20) -> dict:
        """
        Return exit quality metrics.
        Positive avg_improvement means holding longer would have helped.
        Negative means exit was correct.
        """
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        row = self.query_one(
            """
            SELECT
                COUNT(*) AS total_exits,
                AVG(pnl_15min_after_exit - exit_pnl_rupees) AS avg_improvement_15min,
                SUM(CASE WHEN was_exit_premature = 1 THEN 1 ELSE 0 END) AS premature_count,
                SUM(CASE WHEN was_exit_late = 1 THEN 1 ELSE 0 END) AS late_count
            FROM exit_quality_log
            WHERE trading_date >= ?
            """,
            (cutoff,),
        )
        if not row:
            return {"total_exits": 0, "avg_improvement_15min": 0.0,
                    "premature_count": 0, "late_count": 0}
        return dict(row)

    def get_regime_accuracy(self, days: int = 30) -> dict:
        """Return regime classification accuracy metrics."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        row = self.query_one(
            """
            SELECT
                COUNT(*) AS total,
                AVG(was_vol_correct) AS vol_accuracy,
                AVG(was_price_correct) AS price_accuracy,
                AVG(was_positioning_correct) AS pos_accuracy,
                AVG(was_final_correct) AS final_accuracy,
                AVG(score_value) AS avg_score
            FROM regime_accuracy_scores
            WHERE trading_date >= ?
            """,
            (cutoff,),
        )
        if not row:
            return {}
        return {k: round(v, 3) if v is not None else None for k, v in dict(row).items()}

    def get_signal_predictive_accuracy(self, days: int = 60) -> dict:
        """
        Return how well each signal predicted trade outcomes.
        Used to set signal weights in calibration.
        """
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = self.query(
            """
            SELECT
                te.vol_regime_at_entry,
                te.price_regime_at_entry,
                te.positioning_at_entry,
                te.iv_behavior,
                te.or_condition,
                tx.result
            FROM trade_entries te
            JOIN trade_exits tx ON te.position_id = tx.position_id
            WHERE te.trading_date >= ?
            """,
            (cutoff,),
        )
        if not rows:
            return {}

        total = len(rows)
        if total == 0:
            return {}

        # Count wins per signal value
        vol_wins    = sum(1 for r in rows if r["result"] == "WIN" and
                          r["vol_regime_at_entry"] in ("SELL_PREMIUM", "STRONG_SELL_PREMIUM"))
        price_wins  = sum(1 for r in rows if r["result"] == "WIN" and
                          r["price_regime_at_entry"] not in ("OBSERVING", "CHOPPY", None))
        pos_wins    = sum(1 for r in rows if r["result"] == "WIN" and
                          r["positioning_at_entry"] not in ("UNCLEAR", None))
        iv_wins     = sum(1 for r in rows if r["result"] == "WIN" and
                          r.get("iv_behavior") in ("STABLE", "DECLINING", "CRUSHING"))
        or_wins     = sum(1 for r in rows if r["result"] == "WIN" and
                          r["or_condition"] in ("VERY_NARROW", "NARROW"))

        total_wins = sum(1 for r in rows if r["result"] == "WIN")
        base_rate  = total_wins / total if total > 0 else 0.5

        def lift(wins: int, n: int) -> float:
            if n == 0:
                return 1.0
            return round((wins / n) / max(base_rate, 0.01), 3)

        return {
            "vol_lift":    lift(vol_wins, total),
            "price_lift":  lift(price_wins, total),
            "pos_lift":    lift(pos_wins, total),
            "iv_lift":     lift(iv_wins, total),
            "or_lift":     lift(or_wins, total),
            "base_win_rate": round(base_rate, 3),
            "total_trades":  total,
        }

    def get_vrp_smoothed_history(self, n_cycles: int = 5) -> List[float]:
        """Return last N VRP raw values for smoothing computation."""
        today_str = today_ist().isoformat()
        rows = self.query(
            "SELECT vrp_raw FROM cycle_log "
            "WHERE trading_date=? AND vrp_raw IS NOT NULL "
            "ORDER BY cycle_id DESC LIMIT ?",
            (today_str, n_cycles),
        )
        return [r["vrp_raw"] for r in rows if r.get("vrp_raw") is not None]

    def get_prev_day_vix_close(self) -> Optional[float]:
        """Return yesterday's closing VIX value."""
        today_str = today_ist().isoformat()
        row = self.query_one(
            "SELECT vix_close FROM daily_summary "
            "WHERE trading_date < ? AND vix_close IS NOT NULL AND vix_close > 0 "
            "ORDER BY trading_date DESC LIMIT 1",
            (today_str,),
        )
        if row and row.get("vix_close"):
            return float(row["vix_close"])
        # Fallback: last VIX reading from vix_history before today
        row2 = self.query_one(
            "SELECT vix_value FROM vix_history "
            "WHERE date < ? AND vix_value > 0 "
            "ORDER BY timestamp DESC LIMIT 1",
            (today_str,),
        )
        if row2 and row2.get("vix_value"):
            return float(row2["vix_value"])
        return None

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

class SQLiteAuditHandler(logging.Handler):
    """Logging handler that writes WARNING+ records to the audit_log table."""

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
            self.handleError(record)


def setup_logging(
    db: Database,
    log_dir: Path,
    level: int = logging.INFO
) -> logging.Logger:
    """
    Configure the nifty_algo logger with:
    - Console handler (INFO+)
    - Rotating file handler (DEBUG+, 90-day retention)
    - SQLite audit handler (WARNING+)
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("nifty_algo")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    logger.addHandler(console)

    # Rotating file (daily rotation, 90 days)
    log_file = log_dir / "nifty_algo_audit.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=90, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    # SQLite (WARNING+ only to avoid DB spam)
    sqlite_handler = SQLiteAuditHandler(db)
    sqlite_handler.setFormatter(fmt)
    sqlite_handler.setLevel(logging.WARNING)
    logger.addHandler(sqlite_handler)

    logger.propagate = False
    return logger


# ─────────────────────────────────────────────
# UPSTOX API CLIENT
# ─────────────────────────────────────────────

class UpstoxAPIError(Exception):
    """Raised when the Upstox API returns an error."""
    def __init__(
        self, message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None
    ):
        super().__init__(message)
        self.status_code  = status_code
        self.response_body = response_body


class UpstoxClient:
    """
    Upstox API v2 client with:
    - Automatic rate limiting
    - Retry on 5xx and 429
    - API call logging to database
    - Paper trade mode guard on order placement
    """

    def __init__(
        self,
        config: Config,
        rate_limiter: RateLimiter,
        db: Database,
        logger: logging.Logger
    ):
        self.config       = config
        self.rate_limiter = rate_limiter
        self.db           = db
        self.logger       = logger

        self.session = requests.Session()

        # Retry strategy: 3 retries on 5xx and 429
        try:
            retry_strategy = Retry(
                total=config.max_retries,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "POST", "DELETE"],
                respect_retry_after_header=True,
            )
        except TypeError:
            # Older urllib3 uses method_whitelist
            retry_strategy = Retry(
                total=config.max_retries,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                method_whitelist=["GET", "POST", "DELETE"],
            )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "Accept":        "application/json",
            "Authorization": f"Bearer {config.upstox_access_token}",
        })

    def _request(
        self,
        method: str,
        endpoint_key: str,
        category: str = "default",
        path_params: Optional[dict] = None,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        """
        Make an authenticated API request.
        Handles rate limiting, logging, and error raising.
        """
        path = API_ENDPOINTS[endpoint_key]
        if path_params:
            path = path.format(**path_params)
        url = f"{UPSTOX_BASE_URL}{path}"

        wait_time = self.rate_limiter.wait_if_needed(category)
        if wait_time > 0.5:
            self.logger.debug(
                f"Rate limiter: waited {wait_time:.2f}s for {endpoint_key}"
            )

        start       = time.monotonic()
        status_code = None

        try:
            resp = self.session.request(
                method, url,
                params=params,
                json=json_body,
                timeout=self.config.request_timeout_seconds,
            )
            status_code  = resp.status_code
            elapsed_ms   = (time.monotonic() - start) * 1000

            if resp.status_code == 429:
                msg = "Rate limited by Upstox (HTTP 429)"
                self.logger.warning(f"{endpoint_key}: {msg}")
                self.db.log_api_call(
                    category, endpoint_key, method,
                    status_code, elapsed_ms,
                    rate_limited=True, error_message=msg
                )
                raise UpstoxAPIError(msg, status_code=429, response_body=resp.text)

            resp.raise_for_status()
            data = resp.json()
            self.db.log_api_call(
                category, endpoint_key, method, status_code, elapsed_ms
            )
            return data

        except requests.exceptions.RequestException as e:
            elapsed_ms    = (time.monotonic() - start) * 1000
            error_message = str(e)
            self.logger.error(f"API call failed: {endpoint_key} — {error_message}")
            self.db.log_api_call(
                category, endpoint_key, method,
                status_code, elapsed_ms,
                error_message=error_message
            )
            raise UpstoxAPIError(error_message, status_code=status_code) from e

    # ── Market Data ───────────────────────────────────────────────────────

    def validate_token(self) -> bool:
        """Validate the access token by fetching user profile."""
        try:
            data = self._request("GET", "profile", category="default")
            user_name = data.get("data", {}).get("user_name", "unknown")
            self.logger.info(f"Upstox token valid. User: {user_name}")
            return True
        except UpstoxAPIError as e:
            self.logger.error(f"Upstox token validation FAILED: {e}")
            return False

    def get_ltp(self, instrument_keys) -> dict:
        """Fetch last traded price for one or more instruments."""
        if isinstance(instrument_keys, str):
            instrument_keys = [instrument_keys]
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "ltp", category="quote", params=params)
        return data.get("data", {})

    def get_full_quote(self, instrument_keys: list) -> dict:
        """Fetch full quote (OHLC + depth) for instruments."""
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "quotes", category="quote", params=params)
        return data.get("data", {})

    def get_historical_candles(
        self,
        instrument_key: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> list:
        """Fetch historical OHLCV candles."""
        path_params = {
            "instrument_key": instrument_key,
            "interval":       interval,
            "to_date":        to_date,
            "from_date":      from_date,
        }
        data = self._request(
            "GET", "historical_candle",
            category="historical",
            path_params=path_params,
        )
        return data.get("data", {}).get("candles", [])

    def get_intraday_candles(
        self, instrument_key: str, interval: str
    ) -> list:
        """Fetch today's intraday OHLCV candles."""
        path_params = {
            "instrument_key": instrument_key,
            "interval":       interval,
        }
        data = self._request(
            "GET", "intraday_candle",
            category="historical",
            path_params=path_params,
        )
        return data.get("data", {}).get("candles", [])

    def get_option_contracts(
        self,
        instrument_key: str,
        expiry_date: Optional[str] = None,
    ) -> list:
        """Fetch available option contracts for an underlying."""
        params: dict = {"instrument_key": instrument_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        data = self._request(
            "GET", "option_contracts",
            category="chain",
            params=params,
        )
        return data.get("data", [])

    def get_option_chain(
        self, instrument_key: str, expiry_date: str
    ) -> list:
        """Fetch full option chain for a specific expiry."""
        params = {
            "instrument_key": instrument_key,
            "expiry_date":    expiry_date,
        }
        data = self._request(
            "GET", "option_chain",
            category="chain",
            params=params,
        )
        return data.get("data", [])

    def get_positions(self) -> list:
        """Fetch current open positions from broker."""
        data = self._request("GET", "positions", category="default")
        return data.get("data", [])

    def get_funds_and_margin(self) -> dict:
        """Fetch available funds and margin from broker."""
        data = self._request("GET", "funds_margin", category="default")
        return data.get("data", {})

    def get_order_details(self, order_id: str) -> dict:
        """Fetch details of a specific order."""
        params = {"order_id": order_id}
        data = self._request(
            "GET", "order_details",
            category="order",
            params=params,
        )
        return data.get("data", {})

    # ── Order Placement ───────────────────────────────────────────────────

    def place_order(
        self,
        instrument_token: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "LIMIT",
        product: str = "I",
        price: float = 0.0,
        trigger_price: float = 0.0,
        validity: str = "DAY",
        tag: str = "nifty_algo_v3",
    ) -> dict:
        """
        Place a live order. Raises RuntimeError in paper trade mode.
        transaction_type: 'BUY' or 'SELL'
        order_type: 'LIMIT', 'MARKET', 'SL', 'SL-M'
        product: 'I' (intraday), 'D' (delivery)
        """
        if self.config.paper_trade_mode:
            raise RuntimeError(
                "place_order() called while PAPER_TRADE_MODE=True. "
                "Refusing to place a real order."
            )
        body = {
            "quantity":         quantity,
            "product":          product,
            "validity":         validity,
            "price":            price,
            "instrument_token": instrument_token,
            "order_type":       order_type,
            "transaction_type": transaction_type,
            "trigger_price":    trigger_price,
            "is_amo":           False,
            "tag":              tag,
        }
        data = self._request(
            "POST", "place_order",
            category="order",
            json_body=body,
        )
        return data.get("data", {})

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a live order. Raises RuntimeError in paper trade mode."""
        if self.config.paper_trade_mode:
            raise RuntimeError(
                "cancel_order() called while PAPER_TRADE_MODE=True. Refusing."
            )
        params = {"order_id": order_id}
        data = self._request(
            "DELETE", "cancel_order",
            category="order",
            params=params,
        )
        return data.get("data", {})


# ─────────────────────────────────────────────
# PRINT HELPERS
# ─────────────────────────────────────────────

def print_section(title: str, char: str = "=", width: int = 78) -> None:
    """Print a formatted section header."""
    print(char * width)
    print(title.center(width))
    print(char * width)


def print_kv_table(
    data: dict,
    title: Optional[str] = None,
    width: int = 78
) -> None:
    """Print a key-value table with optional title."""
    if title:
        print(f"\n--- {title} ---")
    for k, v in data.items():
        if isinstance(v, float):
            v_str = f"{v:,.4f}"
        elif isinstance(v, int):
            v_str = f"{v:,}"
        else:
            v_str = str(v) if v is not None else "N/A"
        print(f"  {str(k):<34}: {v_str}")


# ─────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────

def _self_test() -> None:
    """
    Standalone self-test for core.py.
    Tests: config loading, database schema, expiry calendar, API token.
    Run: python core.py
    """
    print_section("NIFTY ALGO v3.0 — CORE SELF-TEST", char="#")

    # ── Config ────────────────────────────────────────────────────────────
    config = load_config()
    print(repr(config))
    print_kv_table({
        "paper_trade_mode":          config.paper_trade_mode,
        "starting_capital":          config.starting_capital,
        "lot_size":                  config.lot_size,
        "nifty_strike_step":         config.nifty_strike_step,
        "max_risk_per_trade_pct":    config.max_risk_per_trade_pct,
        "max_daily_loss_pct":        config.max_daily_loss_pct,
        "trading_window_start":      config.trading_window_start,
        "trading_window_last_entry": config.trading_window_last_entry,
        "hard_exit_time":            config.hard_exit_time,
        "tuesday_hard_exit":         config.tuesday_hard_exit,
        "tuesday_last_entry":        config.tuesday_last_entry,
        "vrp_sell_threshold":        config.vrp_sell_threshold_default,
        "vrp_fair_threshold":        config.vrp_fair_threshold_default,
        "abort_vix_spike_pct":       config.abort_vix_spike_pct,
        "abort_vix_absolute":        config.abort_vix_absolute,
        "delta_close_threshold":     config.delta_close_threshold,
        "spot_proximity_pts":        config.spot_proximity_pts,
        "profit_lock_pct_dte0":      config.profit_lock_pct_dte0,
        "profit_lock_pct_dte1plus":  config.profit_lock_pct_dte1plus,
        "cheap_buyback_pts":         config.cheap_buyback_pts,
        "phantom_trade_tracking":    config.phantom_trade_tracking,
        "db_path":                   str(config.db_path),
        "log_dir":                   str(config.log_dir),
    }, title="Configuration")

    # ── Expiry Calendar ───────────────────────────────────────────────────
    print_section("EXPIRY CALENDAR TEST")
    today = today_ist()
    next_expiry = ExpiryCalendar.get_next_expiry(today)
    dte = ExpiryCalendar.get_dte(today)
    print_kv_table({
        "today":              today,
        "day_label":          ExpiryCalendar.get_day_label(today),
        "is_holiday":         ExpiryCalendar.is_holiday(today),
        "is_event_day":       ExpiryCalendar.is_event_day(today) or "(none)",
        "next_expiry":        next_expiry,
        "dte":                dte,
        "day_type":           ExpiryCalendar.get_day_type(today),
        "is_monthly_expiry":  ExpiryCalendar.is_monthly_expiry(next_expiry),
        "next_trading_day":   ExpiryCalendar.get_next_trading_day(today),
        "nse_holidays_loaded": len(get_nse_holidays()),
        "events_loaded":       len(get_high_impact_events()),
    })

    # ── Database ──────────────────────────────────────────────────────────
    print_section("DATABASE SCHEMA TEST")
    # v3.8: run this self-test against an isolated scratch
    # database, never the production book. The logging test
    # writes audit_log rows; config is left untouched (it may be
    # frozen), so only the scratch Database is handed in.
    import tempfile as _scratch_tmp
    db = Database(Path(_scratch_tmp.mkdtemp(
        prefix="core_selftest_")) / "core_selftest.db")

    logger = setup_logging(
        db, config.log_dir,
        level=getattr(logging, config.log_level.upper(), logging.INFO)
    )

    tables = db.query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    print(f"  Tables created: {len(tables)}")
    for t in tables:
        row_count = db.query_one(f"SELECT COUNT(*) as cnt FROM {t['name']}")
        cnt = row_count["cnt"] if row_count else 0
        print(f"    {t['name']:<40} {cnt:>6} rows")

    # ── Rate Limiter ──────────────────────────────────────────────────────
    print_section("RATE LIMITER TEST")
    rl = RateLimiter(config.rate_limits)
    import time as _t
    start = _t.monotonic()
    for i in range(3):
        wait = rl.wait_if_needed("quote")
    elapsed = _t.monotonic() - start
    print(f"  3 quote calls completed in {elapsed:.3f}s (should be near-instant)")

    # ── API Token ─────────────────────────────────────────────────────────
    print_section("UPSTOX API TOKEN TEST")
    if not config.upstox_access_token:
        print("  UPSTOX_ACCESS_TOKEN not set — skipping live API test.")
        print("  Set token in env.txt to test API connectivity.")
    else:
        client = UpstoxClient(config, rl, db, logger)
        ok = client.validate_token()
        status = "VALID" if ok else "INVALID / EXPIRED"
        print(f"  Token status: {status}")
        if not ok:
            print("  Regenerate token and update UPSTOX_ACCESS_TOKEN in env.txt")

    # ── Logging ───────────────────────────────────────────────────────────
    print_section("LOGGING TEST")
    logger.info("Core self-test: INFO message")
    logger.warning("Core self-test: WARNING message")
    logger.debug("Core self-test: DEBUG message (file only)")
    audit_rows = db.query(
        "SELECT COUNT(*) as cnt FROM audit_log WHERE log_time >= ?",
        ((now_ist() - timedelta(minutes=1)).isoformat(),)
    )
    print(f"  Audit log entries written: {audit_rows[0]['cnt'] if audit_rows else 0}")

    # ── Helper Functions ──────────────────────────────────────────────────
    print_section("HELPER FUNCTIONS TEST")
    ts_test = now_ist()
    parsed  = parse_ist_timestamp(ts_test.isoformat())
    print(f"  now_ist():              {ts_test}")
    print(f"  today_ist():            {today_ist()}")
    print(f"  parse_ist_timestamp():  {parsed}")
    print(f"  IST timezone:           {IST}")

    db.close()
    print_section("SELF-TEST COMPLETE", char="#")
    print(f"  Database: {db.db_path}")
    print(f"  Log file: {config.log_dir / 'nifty_algo_audit.log'}")
    print()


if __name__ == "__main__":
    _self_test()